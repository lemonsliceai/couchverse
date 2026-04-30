"""IntroSequencer per-persona room wiring.

Pinned regression: in dual-room mode each persona owns its own
``rtc.Room`` and its avatar publishes video into THAT room. The
sequencer must watch the persona's own room when gating on
avatar-readiness — not the primary room. The pre-fix bug had the
sequencer always watching the primary, so the secondary persona's
intro was silently SKIPPED after the 15-second timeout even though
its avatar had already published.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections import defaultdict
from typing import Any

import pytest
from livekit import rtc

from podcast_commentary.agent.comedian import PersonaAgent
from podcast_commentary.agent.intro_sequencer import IntroSequencer, IntroStatus
from podcast_commentary.agent.room_state import RoomState

from ._stub_config import make_stub_config


@pytest.fixture(autouse=True)
def _stub_external_keys(monkeypatch):
    """PersonaAgent construction is cheap, but loading configs for sibling
    tests sometimes pulls in modules that read API keys at import time —
    set stubs so this file is hermetic.
    """
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-used")


class _FakeRoom:
    """Minimal stand-in for ``rtc.Room`` — only what IntroSequencer touches.

    Mirrors the shape used by ``test_director.py``'s ``_FakeRoom``: an
    ``on``/``off`` event-emitter pair plus a mutable
    ``remote_participants`` map. Each persona's avatar-readiness gate
    listens on its own room, so the test gives every persona its own
    instance.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.handlers: dict[str, list[Any]] = defaultdict(list)
        self.remote_participants: dict[str, Any] = {}

    def on(self, event: str, fn=None):
        if fn is not None:
            self.handlers[event].append(fn)
            return fn

        def deco(handler):
            self.handlers[event].append(handler)
            return handler

        return deco

    def off(self, event: str, fn) -> None:
        try:
            self.handlers.get(event, []).remove(fn)
        except ValueError:
            pass

    def emit_track_published(self, publication: Any, participant: Any) -> None:
        for handler in list(self.handlers.get("track_published", [])):
            handler(publication, participant)


class _FakePublication:
    def __init__(self, kind: Any) -> None:
        self.kind = kind


class _FakeParticipant:
    def __init__(self, identity: str, publications: list[_FakePublication]) -> None:
        self.identity = identity
        self.track_publications = {f"pub-{i}": p for i, p in enumerate(publications)}


class _FakeSpeechHandle:
    """``IntroSequencer`` calls ``handle.wait_for_playout`` indirectly via
    ``PlayoutWaiter.wait``; we never await it because we swap the waiter
    for ``_NoopPlayoutWaiter``. Kept as a sentinel so the sequencer's
    ``handle is None`` early-exit doesn't fire.
    """


class _NoopPlayoutWaiter:
    """Resolves immediately so the test isn't gated on real playout RPCs."""

    async def wait(self, persona, handle, *, timeout, label) -> None:  # noqa: ARG002
        return None


class _NoopControlChannel:
    """Captures publish calls without touching a LiveKit data channel."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []

    async def publish_commentary_start(self, speaker: str, *, phase: str) -> None:
        self.events.append(("start", speaker, phase))

    async def publish_commentary_end(self, speaker: str, *, phase: str) -> None:
        self.events.append(("end", speaker, phase))


def _make_persona_with_short_avatar_timeout(name: str, timeout_s: float) -> PersonaAgent:
    """Build a PersonaAgent with ``avatar.startup_timeout_s`` overridden.

    The shipped configs use 15s; a pre-fix run of this test would block
    that long before SKIPPING. Use ``dataclasses.replace`` so the test
    fails fast (~0.5s) when the wiring regresses.
    """
    base = make_stub_config(name)
    short_avatar = dataclasses.replace(base.avatar, startup_timeout_s=timeout_s)
    short_config = dataclasses.replace(base, avatar=short_avatar)
    return PersonaAgent(config=short_config)


def _video_avatar_participant(persona_name: str) -> _FakeParticipant:
    """Build a fake remote participant whose identity matches the
    `lemonslice-avatar-<persona>` convention and is publishing one video
    track — which is what `_wait_for_avatar_ready`'s fast path looks for.
    """
    identity = f"lemonslice-avatar-{persona_name}"
    return _FakeParticipant(identity, [_FakePublication(rtc.TrackKind.KIND_VIDEO)])


@pytest.mark.asyncio
async def test_intro_sequencer_uses_per_persona_room_for_avatar_ready():
    """Regression: a secondary persona's intro must observe its avatar in
    its own room, not the primary room.

    Pre-fix the sequencer always watched the primary room; the secondary
    persona's avatar (in the secondary room) was invisible and the intro
    fell through to SKIPPED after the startup_timeout_s window. With the
    fix, both personas' intros reach DONE because each avatar-ready gate
    looks at the right room.
    """
    primary = _make_persona_with_short_avatar_timeout("persona_a", timeout_s=0.5)
    secondary = _make_persona_with_short_avatar_timeout("persona_b", timeout_s=0.5)

    # Each persona's avatar lives in its OWN room. If the sequencer
    # collapses to one room it will be unable to find the secondary's
    # avatar and the test will SKIPPED-fail.
    primary_room = _FakeRoom("session-persona_a")
    secondary_room = _FakeRoom("session-persona_b")
    primary_avatar = _video_avatar_participant("persona_a")
    secondary_avatar = _video_avatar_participant("persona_b")
    primary_room.remote_participants[primary_avatar.identity] = primary_avatar
    secondary_room.remote_participants[secondary_avatar.identity] = secondary_avatar

    # Stub out the parts of PersonaAgent that need a real AgentSession.
    # `speak_intro` only has to return a non-None sentinel so the
    # sequencer doesn't take the abort branch; the no-op waiter then
    # resolves the playout instantly.
    primary.speak_intro = lambda: _FakeSpeechHandle()  # type: ignore[assignment]
    secondary.speak_intro = lambda: _FakeSpeechHandle()  # type: ignore[assignment]

    sequencer = IntroSequencer(
        personas=[primary, secondary],
        rooms={primary.name: primary_room, secondary.name: secondary_room},
        avatar_identities={
            primary.name: primary_avatar.identity,
            secondary.name: secondary_avatar.identity,
        },
        room_state=RoomState([primary, secondary]),
        control=_NoopControlChannel(),  # type: ignore[arg-type]
        playout_waiter=_NoopPlayoutWaiter(),  # type: ignore[arg-type]
    )

    # Wrap the run in a hard wall-clock cap so a regression that loses
    # the per-persona wiring fails in <1s instead of stalling the suite.
    await asyncio.wait_for(sequencer.run(), timeout=2.0)

    assert sequencer.status(primary.name) is IntroStatus.DONE
    assert sequencer.status(secondary.name) is IntroStatus.DONE


@pytest.mark.asyncio
async def test_intro_sequencer_skips_persona_whose_avatar_never_publishes():
    """Negative path: if a persona's room never sees its avatar publish,
    that persona is SKIPPED but the next persona (and the overall
    sequence) still reaches a terminal state.

    Pins the existing fail-soft behaviour so a future timeout-tightening
    refactor can't accidentally hang the show on a missing avatar.
    """
    primary = _make_persona_with_short_avatar_timeout("persona_a", timeout_s=0.2)
    secondary = _make_persona_with_short_avatar_timeout("persona_b", timeout_s=0.2)

    primary_room = _FakeRoom("session-persona_a")
    secondary_room = _FakeRoom("session-persona_b")
    primary_avatar = _video_avatar_participant("persona_a")
    primary_room.remote_participants[primary_avatar.identity] = primary_avatar
    # secondary_room intentionally empty — its avatar never publishes.

    primary.speak_intro = lambda: _FakeSpeechHandle()  # type: ignore[assignment]
    secondary.speak_intro = lambda: _FakeSpeechHandle()  # type: ignore[assignment]

    sequencer = IntroSequencer(
        personas=[primary, secondary],
        rooms={primary.name: primary_room, secondary.name: secondary_room},
        avatar_identities={
            primary.name: primary_avatar.identity,
            secondary.name: f"lemonslice-avatar-{secondary.name}",
        },
        room_state=RoomState([primary, secondary]),
        control=_NoopControlChannel(),  # type: ignore[arg-type]
        playout_waiter=_NoopPlayoutWaiter(),  # type: ignore[arg-type]
    )

    await asyncio.wait_for(sequencer.run(), timeout=2.0)

    assert sequencer.status(primary.name) is IntroStatus.DONE
    assert sequencer.status(secondary.name) is IntroStatus.SKIPPED


def test_intro_sequencer_rejects_missing_room_mapping():
    """Loud-fail at construction if any persona lacks a room — silently
    falling back to a primary-room watcher is the bug we're fixing, so
    the constructor must refuse to build that footgun.
    """
    primary = _make_persona_with_short_avatar_timeout("persona_a", timeout_s=0.5)
    secondary = _make_persona_with_short_avatar_timeout("persona_b", timeout_s=0.5)

    with pytest.raises(ValueError, match="missing room mapping"):
        IntroSequencer(
            personas=[primary, secondary],
            rooms={primary.name: _FakeRoom("session-persona_a")},  # secondary missing
            avatar_identities={
                primary.name: f"lemonslice-avatar-{primary.name}",
                secondary.name: f"lemonslice-avatar-{secondary.name}",
            },
            room_state=RoomState([primary, secondary]),
            control=_NoopControlChannel(),  # type: ignore[arg-type]
            playout_waiter=_NoopPlayoutWaiter(),  # type: ignore[arg-type]
        )
