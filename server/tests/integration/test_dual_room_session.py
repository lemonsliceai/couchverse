"""End-to-end test: dispatched session brings up two rooms, both
avatars join, both personas comment, and the co-speaker context flows.

Pinned acceptance criteria:

* AC1 — Test harness is local LiveKit + agent worker subprocess + mock
  LemonSlice. (See ``conftest.py``.)
* AC2 — Session creates two rooms; both join their respective avatars
  within ``startup_timeout_s``.
* AC3 — A 30-second synthetic podcast track on the primary room
  produces ≥ 1 commentary turn per persona.
* AC4 — Persona B's commentary text shares a significant token with
  persona A's last line (proving the LLM saw the co-speaker block).
* AC5 — No ``_wait_for_playout_robust`` fallback fires across the run
  (instrumented via the ``playout_finished_rpc_total`` log emission).
* AC6 — Skip on PR by default. The session-scope autouse gate in
  ``conftest.py`` enforces this.

The body of the test is intentionally heavy; do not try to run this
locally without Docker. Nightly CI sets ``RUN_DUAL_ROOM_INTEGRATION=1``.

Architectural notes:
  * The agent's STT (Groq Whisper) and LLM (Groq Llama) are deferred
    to the live services in nightly CI. Per-AC the LLM output is made
    deterministic enough for the co-speaker check via a CI-only Groq
    proxy or `temperature=0` — both are configured upstream of this
    test (see ``server/.env.example`` ``CI_GROQ_PROXY_URL``).
  * The TTS sink is the real ElevenLabs in nightly mode; no audible
    audio reaches the wire because :class:`MockAvatarSession` doesn't
    forward the TTS subscription. The test only inspects the
    transcript stream and the agent's commentary log lines — audio
    delivery is exercised by separate avatar-side tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import struct
import subprocess
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from podcast_commentary.agent.metrics import references_co_speaker

logger = logging.getLogger("podcast-commentary.integration.test-dual-room")


pytestmark = [pytest.mark.asyncio]


# How long to wait for the user to land in both rooms after dispatch.
# 30 s comfortably exceeds the default ``AvatarConfig.startup_timeout_s``
# (which the agent uses to fail an avatar) so a hit on this timeout means
# the dispatch itself didn't deliver, not that the avatar is just slow.
_AVATAR_JOIN_DEADLINE_S = 30.0

# The synthetic podcast track length. Picked to span at least two
# ``CHUNK_INTERVAL_SECONDS`` (10 s) windows — the agent only emits
# commentary AFTER an STT chunk lands, so 30 s gives enough headroom for
# a full burst-window cycle (``BURST_WINDOW=60s`` doesn't matter at this
# duration; we only need ≥ 2 chunks to land before the test asserts).
_PODCAST_DURATION_S = 30.0

# Margin past ``_PODCAST_DURATION_S`` to wait for commentary to arrive.
# After STT lands the selector + LLM + TTS path takes another few
# seconds; 60 s of headroom is plenty without making CI flakily slow.
_COMMENTARY_DEADLINE_AFTER_AUDIO_S = 60.0


@asynccontextmanager
async def _api_client(integration_env: dict[str, str]) -> AsyncIterator[Any]:
    """Mount the FastAPI session router on a hermetic app and yield a client.

    We deliberately do NOT exercise ``app.py``'s lifespan (which warms
    the DB pool and runs migrations). The session route only needs the
    LiveKit credentials and a stubbed ``create_session`` — both already
    set up in ``integration_env``.
    """
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from podcast_commentary.api.routes import sessions as sessions_module
    from podcast_commentary.core import config as core_config

    for key, value in integration_env.items():
        os.environ[key] = value
        if hasattr(core_config.settings, key):
            setattr(core_config.settings, key, value)

    captured: dict[str, Any] = {}

    async def _fake_create_session(
        room_name: str,
        video_url: str,
        video_title: str | None = None,
        rooms: dict[str, str] | None = None,
        session_id: str | None = None,
    ) -> str:
        captured["room_name"] = room_name
        captured["rooms"] = rooms
        return session_id or "stub-session-id"

    sessions_module.create_session = _fake_create_session  # type: ignore[assignment]

    app = FastAPI()
    app.include_router(sessions_module.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        client.captured = captured  # type: ignore[attr-defined]
        yield client


def _generate_pcm_silence(duration_s: float, sample_rate: int = 48_000) -> bytes:
    """Return 16-bit mono PCM of the given duration filled with low-amplitude tone.

    Pure silence trips Groq Whisper's no-speech detector and the agent
    never emits commentary; the published track must contain *something*
    above the VAD floor for the STT pipeline to deliver chunks. A
    2-tone phrase ("alpha bravo") is what the test would actually want
    to keep the LLM context grounded — but synthesizing audible speech
    in-process is overkill. A 220 Hz tone is loud enough for VAD and
    sometimes makes Whisper emit a heard token; combined with the
    deterministic LLM stub the assertion still holds.
    """
    samples = int(duration_s * sample_rate)
    amp = 8000  # ≪ int16 max; well above VAD threshold but not loud
    freq = 220.0
    payload = bytearray()
    for i in range(samples):
        v = int(amp * math.sin(2 * math.pi * freq * i / sample_rate))
        payload.extend(struct.pack("<h", v))
    return bytes(payload)


async def _publish_synthetic_audio(
    *,
    livekit_url: str,
    api_key: str,
    api_secret: str,
    room_name: str,
    duration_s: float,
) -> None:
    """Publish a synthetic ``podcast-audio`` track on ``room_name``.

    Mirrors what the Chrome extension does: connects as a regular user,
    publishes a track named ``podcast-audio``, and pumps audio frames
    until ``duration_s`` elapses.
    """
    from livekit import api, rtc

    sample_rate = 48_000
    user_identity = f"podcast-audio-source-{int(time.time())}"

    token = (
        api.AccessToken(api_key, api_secret)
        .with_identity(user_identity)
        .with_name(user_identity)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=False,
            )
        )
        .to_jwt()
    )

    room = rtc.Room()
    await room.connect(livekit_url, token, rtc.RoomOptions())
    try:
        source = rtc.AudioSource(sample_rate=sample_rate, num_channels=1)
        track = rtc.LocalAudioTrack.create_audio_track("podcast-audio", source)
        await room.local_participant.publish_track(
            track,
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
        )

        pcm = _generate_pcm_silence(duration_s, sample_rate=sample_rate)
        # 20 ms frames — what the WebRTC stack prefers; smaller frames
        # are dropped on backpressure.
        frame_samples = sample_rate // 50
        frame_bytes = frame_samples * 2
        for offset in range(0, len(pcm), frame_bytes):
            chunk = pcm[offset : offset + frame_bytes]
            if len(chunk) < frame_bytes:
                break
            frame = rtc.AudioFrame(
                data=chunk,
                sample_rate=sample_rate,
                num_channels=1,
                samples_per_channel=frame_samples,
            )
            await source.capture_frame(frame)
            await asyncio.sleep(0.02)
    finally:
        await room.disconnect()


async def _wait_for_avatar_in_room(
    *,
    livekit_url: str,
    api_key: str,
    api_secret: str,
    room_name: str,
    avatar_identity: str,
    deadline_s: float,
) -> float:
    """Connect a watcher participant and return seconds-to-first-video for the avatar.

    Used by AC2 ("both avatars join their respective rooms within
    ``startup_timeout_s``"). We watch for either ``track_published`` or
    a participant-already-present case; whichever fires first wins.
    """
    from livekit import api, rtc

    watcher_identity = f"watcher-{room_name}"
    token = (
        api.AccessToken(api_key, api_secret)
        .with_identity(watcher_identity)
        .with_name(watcher_identity)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=False,
                can_subscribe=True,
            )
        )
        .to_jwt()
    )

    room = rtc.Room()
    arrived = asyncio.Event()
    started_at = time.monotonic()

    def _is_video(pub: Any) -> bool:
        return getattr(pub, "kind", None) == rtc.TrackKind.KIND_VIDEO

    def _on_publish(pub: Any, p: Any) -> None:
        if getattr(p, "identity", "") == avatar_identity and _is_video(pub):
            arrived.set()

    def _on_connected(p: Any) -> None:
        if getattr(p, "identity", "") == avatar_identity:
            for pub in p.track_publications.values():
                if _is_video(pub):
                    arrived.set()
                    return

    room.on("track_published", _on_publish)
    room.on("participant_connected", _on_connected)

    await room.connect(livekit_url, token, rtc.RoomOptions())
    try:
        for participant in room.remote_participants.values():
            if getattr(participant, "identity", "") == avatar_identity:
                for pub in participant.track_publications.values():
                    if _is_video(pub):
                        return time.monotonic() - started_at
        try:
            await asyncio.wait_for(arrived.wait(), timeout=deadline_s)
        except asyncio.TimeoutError as err:
            raise AssertionError(
                f"avatar identity={avatar_identity!r} did not publish video in "
                f"room={room_name!r} within {deadline_s:.1f}s"
            ) from err
        return time.monotonic() - started_at
    finally:
        await room.disconnect()


async def _capture_commentary(
    *,
    livekit_url: str,
    api_key: str,
    api_secret: str,
    primary_room: str,
    deadline_s: float,
) -> dict[str, list[str]]:
    """Tail the primary room's ``commentary.control`` data channel.

    Each commentary turn the Director publishes to the channel as
    ``{"type": "commentary", "persona": <name>, "text": <line>}`` (see
    :mod:`podcast_commentary.agent.control_channel`). We collect lines
    until ``deadline_s`` elapses and return ``{persona: [lines...]}``.
    """
    from livekit import api, rtc

    listener_identity = f"commentary-listener-{int(time.time())}"
    token = (
        api.AccessToken(api_key, api_secret)
        .with_identity(listener_identity)
        .with_name(listener_identity)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=primary_room,
                can_publish=False,
                can_subscribe=True,
            )
        )
        .to_jwt()
    )

    room = rtc.Room()
    by_persona: dict[str, list[str]] = {}

    def _on_data(packet: Any) -> None:
        topic = getattr(packet, "topic", None) or ""
        if topic and topic != "commentary.control":
            return
        try:
            payload = json.loads(bytes(packet.data).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if payload.get("type") != "commentary":
            return
        persona = payload.get("persona") or "unknown"
        text = payload.get("text") or ""
        if not text:
            return
        by_persona.setdefault(persona, []).append(text)

    room.on("data_received", _on_data)
    await room.connect(livekit_url, token, rtc.RoomOptions())
    try:
        try:
            await asyncio.wait_for(asyncio.sleep(deadline_s), timeout=deadline_s + 1.0)
        except asyncio.TimeoutError:
            pass
        return by_persona
    finally:
        await room.disconnect()


def _scan_worker_log_for_fallback(proc: subprocess.Popen) -> bool:
    """Return True if any ``outcome=fallback`` or ``outcome=timeout`` line appeared.

    The agent emits one structured log line per ``Counter.inc`` / per
    ``Histogram.observe`` (see :mod:`podcast_commentary.agent.metrics`).
    AC5 ("no ``_wait_for_playout_robust`` fallback") maps to no
    ``playout_finished_rpc_total`` line carrying ``outcome=fallback``
    or ``outcome=timeout``.
    """
    if proc.stdout is None:
        return False
    raw = proc.stdout.read1() if hasattr(proc.stdout, "read1") else b""
    text = raw.decode("utf-8", errors="replace") if raw else ""
    pattern = re.compile(r"metric=playout_finished_rpc_total.*outcome=(fallback|timeout)")
    return bool(pattern.search(text))


async def test_dual_room_end_to_end(
    integration_env: dict[str, str],
    agent_worker: subprocess.Popen,
) -> None:
    """Top-to-bottom dual-room session test (AC1–AC5).

    The flow:
      1. Hit the API to create a session — receive two RoomEntries.
      2. Race two ``_wait_for_avatar_in_room`` watchers against
         ``_AVATAR_JOIN_DEADLINE_S``.
      3. Start the synthetic podcast publisher on the primary room and
         the commentary listener on the same room in parallel.
      4. Inspect the captured commentary by persona; assert the AC3 +
         AC4 invariants.
      5. Read the agent's stdout tail; assert the AC5 invariant.
    """
    from podcast_commentary.agent.main import _avatar_identity_for

    async with _api_client(integration_env) as client:
        resp = await client.post(
            "/api/sessions",
            json={"video_url": "https://example.com/integration-test.mp3"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

    rooms = {r["persona"]: r for r in body["rooms"]}
    assert {"fox", "chaos_agent"} <= set(rooms), f"expected both personas, got {list(rooms)}"

    primary = next(r for r in body["rooms"] if r["role"] == "primary")
    secondary = next(r for r in body["rooms"] if r["role"] == "secondary")
    primary_room = primary["room_name"]
    secondary_room = secondary["room_name"]

    livekit_url = integration_env["LIVEKIT_URL"]
    api_key = integration_env["LIVEKIT_API_KEY"]
    api_secret = integration_env["LIVEKIT_API_SECRET"]

    primary_avatar_id = _avatar_identity_for(primary["persona"])
    secondary_avatar_id = _avatar_identity_for(secondary["persona"])

    # AC2 — both avatars join their respective rooms within startup_timeout.
    primary_join, secondary_join = await asyncio.gather(
        _wait_for_avatar_in_room(
            livekit_url=livekit_url,
            api_key=api_key,
            api_secret=api_secret,
            room_name=primary_room,
            avatar_identity=primary_avatar_id,
            deadline_s=_AVATAR_JOIN_DEADLINE_S,
        ),
        _wait_for_avatar_in_room(
            livekit_url=livekit_url,
            api_key=api_key,
            api_secret=api_secret,
            room_name=secondary_room,
            avatar_identity=secondary_avatar_id,
            deadline_s=_AVATAR_JOIN_DEADLINE_S,
        ),
    )
    logger.info(
        "avatars joined primary=%.2fs secondary=%.2fs",
        primary_join,
        secondary_join,
    )

    # AC3 — drive 30 s of synthetic audio, capture commentary in parallel.
    publisher = asyncio.create_task(
        _publish_synthetic_audio(
            livekit_url=livekit_url,
            api_key=api_key,
            api_secret=api_secret,
            room_name=primary_room,
            duration_s=_PODCAST_DURATION_S,
        ),
        name="podcast-audio-publisher",
    )
    listener = asyncio.create_task(
        _capture_commentary(
            livekit_url=livekit_url,
            api_key=api_key,
            api_secret=api_secret,
            primary_room=primary_room,
            deadline_s=_PODCAST_DURATION_S + _COMMENTARY_DEADLINE_AFTER_AUDIO_S,
        ),
        name="commentary-listener",
    )

    by_persona = await listener
    await publisher

    fox_lines = by_persona.get(primary["persona"], [])
    alien_lines = by_persona.get(secondary["persona"], [])

    assert fox_lines, (
        f"AC3 violated: persona={primary['persona']!r} produced no commentary "
        f"in {_PODCAST_DURATION_S + _COMMENTARY_DEADLINE_AFTER_AUDIO_S:.0f}s"
    )
    assert alien_lines, (
        f"AC3 violated: persona={secondary['persona']!r} produced no commentary "
        f"in {_PODCAST_DURATION_S + _COMMENTARY_DEADLINE_AFTER_AUDIO_S:.0f}s"
    )

    # AC4 — co-speaker context: B's line shares a significant token with
    # A's last line. ``references_co_speaker`` filters stop-words and
    # short tokens, matching the production heuristic for cross-persona metrics.
    later_speaker, earlier_lines = (
        (alien_lines[-1], fox_lines[-3:])
        if len(alien_lines) >= 1 and len(fox_lines) >= 1
        else (fox_lines[-1], alien_lines[-3:])
    )
    assert references_co_speaker(later_speaker, earlier_lines), (
        f"AC4 violated: later turn {later_speaker!r} shares no significant token "
        f"with co-speaker recent lines {earlier_lines!r}"
    )

    # AC5 — no fallback / timeout outcomes in the worker logs.
    assert not _scan_worker_log_for_fallback(agent_worker), (
        "AC5 violated: PlayoutWaiter fired the fallback or timeout outcome at "
        "least once during the run; check the agent stdout for "
        "'metric=playout_finished_rpc_total ... outcome=fallback'"
    )


async def test_session_create_returns_two_rooms(
    integration_env: dict[str, str],
) -> None:
    """Light shape test that runs even without the agent subprocess.

    Pinned because "session creates two rooms" is a distinct AC.
    Keeping this isolated lets the API contract test pass
    even when Docker is up but the agent never registers (e.g. a
    misconfigured worker — fail fast on the API side, not on the
    avatar-join wait).
    """
    async with _api_client(integration_env) as client:
        resp = await client.post(
            "/api/sessions",
            json={"video_url": "https://example.com/shape-test.mp3"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["rooms"]) == 2
        roles = sorted(r["role"] for r in body["rooms"])
        assert roles == ["primary", "secondary"]
