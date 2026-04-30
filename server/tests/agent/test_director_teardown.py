"""Director shutdown latch + heartbeat watchdog.

Covers the four invariants of the per-Director ``session_shutdown`` flag:

  * a user-identity ``participant_disconnected`` from ANY room (primary
    OR secondary) trips the latch and runs the teardown sequence
  * an avatar ``participant_disconnected`` does NOT trip the latch
  * the latch closes every secondary connector in parallel before
    asking main.py to terminate the job
  * the heartbeat watchdog force-trips the latch after the timeout
    when no user is present in any room

Construction follows ``test_director.py`` — fake rooms / fake sessions
so we don't need a LiveKit deployment or the FFI binary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Any

import pytest

from podcast_commentary.agent import director as director_module
from podcast_commentary.agent.comedian import PersonaAgent
from podcast_commentary.agent.director import Director, PersonaContext
from podcast_commentary.agent.fox_config import load_config


@pytest.fixture(autouse=True)
def _stub_groq_key(monkeypatch):
    """SpeakerSelector instantiates a ``groq.LLM`` at construction; that
    requires ``GROQ_API_KEY``. Set a stub so tests don't need network.
    """
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-used")


class _FakeParticipant:
    def __init__(self, identity: str) -> None:
        self.identity = identity


class _FakeRoom:
    """Minimal stand-in for ``rtc.Room``.

    Captures registered handlers and exposes ``emit`` so tests can fire
    LiveKit events synchronously without going through the real SDK.
    """

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.handlers: dict[str, list[Any]] = defaultdict(list)
        self.remote_participants: dict[str, Any] = {}
        self.local_participant = None

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

    def emit(self, event: str, *args: Any) -> None:
        for fn in list(self.handlers.get(event, [])):
            fn(*args)


class _FakeSession:
    """Stand-in for ``AgentSession``."""


class _FakeConnector:
    """SecondaryRoomConnector stub recording aclose() calls.

    Subclassing isn't required: Director only ever calls ``aclose()`` on
    these. We deliberately do NOT inherit from the real connector so the
    test stays decoupled from its construction-time LIVEKIT_URL guard.
    """

    def __init__(self, persona: str, *, room: _FakeRoom | None = None) -> None:
        self.persona = persona
        self.room = room or _FakeRoom(f"secondary-{persona}")
        self.aclose_calls: int = 0
        self.aclose_after: asyncio.Event = asyncio.Event()

    async def aclose(self) -> None:
        self.aclose_calls += 1
        self.aclose_after.set()


def _make_persona(name: str) -> PersonaAgent:
    return PersonaAgent(config=load_config(name))


def _build_director(
    *,
    secondary_connectors: list[_FakeConnector] | None = None,
    user_heartbeat_timeout_s: float = 30.0,
    on_user_disconnect=None,
) -> tuple[Director, list[PersonaAgent], list[_FakeRoom], list[_FakeConnector]]:
    """Build a Director wired against fake rooms and fake connectors.

    Returns the Director plus the per-persona personas, rooms, and the
    connector list (one per non-primary persona) so tests can drive
    events at any layer.
    """
    persona_names = ["fox", "chaos_agent"]
    personas = [_make_persona(n) for n in persona_names]

    connectors = list(secondary_connectors or [])
    if not connectors:
        # Default: one fake connector for the non-primary persona.
        connectors = [_FakeConnector(persona=persona_names[1])]

    rooms = [_FakeRoom(f"{persona_names[0]}-room")]
    rooms.append(connectors[0].room)

    contexts = [
        PersonaContext(persona=p, room=r, session=_FakeSession())
        for p, r in zip(personas, rooms, strict=True)
    ]
    avatar_identities = {
        name: f"{director_module._AVATAR_IDENTITY_PREFIX}{name}" for name in persona_names
    }
    director = Director(
        personas=contexts,
        avatar_identities=avatar_identities,
        on_user_disconnect=on_user_disconnect,
        secondary_connectors=connectors,  # type: ignore[arg-type]
        user_heartbeat_timeout_s=user_heartbeat_timeout_s,
    )
    director._wire_room_listeners()
    return director, personas, rooms, connectors


# ---------------------------------------------------------------------------
# Construction surface
# ---------------------------------------------------------------------------


def test_director_exposes_session_shutdown_event_unset_by_default():
    """``session_shutdown`` is the public latch other components consult."""
    director, *_ = _build_director()
    assert isinstance(director.session_shutdown, asyncio.Event)
    assert not director.session_shutdown.is_set()


def test_director_wires_participant_disconnected_on_every_room():
    """Latch must fire on disconnects from primary OR secondary rooms.

    Pinned because the prior implementation only listened on the
    primary; with N secondary rooms the user could disappear from one
    without the show winding down.
    """
    _, _personas, rooms, _ = _build_director()
    for room in rooms:
        assert room.handlers["participant_disconnected"], (
            f"room {room.name} missing participant_disconnected handler"
        )


# ---------------------------------------------------------------------------
# User-disconnect → latch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_disconnect_from_secondary_room_trips_latch():
    """Integration target — secondary disconnect within 5s → primary tears down.

    Mirrors the acceptance test: simulate the user identity leaving
    a secondary room and verify the latch trips, the secondary connector
    closes, and ``on_user_disconnect`` is invoked.
    """
    teardown_called = asyncio.Event()

    async def _on_disconnect() -> None:
        teardown_called.set()

    director, _personas, rooms, connectors = _build_director(on_user_disconnect=_on_disconnect)
    secondary_room = rooms[1]

    secondary_room.emit("participant_disconnected", _FakeParticipant("user-42"))

    await asyncio.wait_for(director.session_shutdown.wait(), timeout=5.0)
    await asyncio.wait_for(connectors[0].aclose_after.wait(), timeout=5.0)
    await asyncio.wait_for(teardown_called.wait(), timeout=5.0)

    assert connectors[0].aclose_calls == 1


@pytest.mark.asyncio
async def test_user_disconnect_from_primary_room_trips_latch():
    director, _personas, rooms, connectors = _build_director()
    primary_room = rooms[0]

    primary_room.emit("participant_disconnected", _FakeParticipant("user-42"))

    await asyncio.wait_for(director.session_shutdown.wait(), timeout=5.0)
    await asyncio.wait_for(connectors[0].aclose_after.wait(), timeout=5.0)


@pytest.mark.asyncio
async def test_avatar_disconnect_does_not_trip_latch(caplog):
    """LemonSlice rendering pod restarts must NOT tear the show down.

    The known-avatar identity set is the load-bearing signal here; the
    handler logs at INFO and lets the SDK reconnect.
    """
    director, _personas, rooms, connectors = _build_director()
    primary_room = rooms[0]

    avatar_identity = f"{director_module._AVATAR_IDENTITY_PREFIX}fox"
    with caplog.at_level(logging.INFO, logger="podcast-commentary.director"):
        primary_room.emit("participant_disconnected", _FakeParticipant(avatar_identity))

    # Give any (incorrectly) spawned shutdown task a chance to run.
    await asyncio.sleep(0.05)
    assert not director.session_shutdown.is_set()
    assert connectors[0].aclose_calls == 0
    assert any("letting LemonSlice reconnect" in rec.message for rec in caplog.records), (
        "expected an INFO log noting the avatar disconnect was ignored"
    )


@pytest.mark.asyncio
async def test_user_disconnect_is_idempotent_across_rooms():
    """Two rooms emitting back-to-back disconnects must spawn ONE teardown.

    Without idempotence the secondary connector would aclose twice and
    the on_user_disconnect callback would fire twice — the second call
    races with the first's job-shutdown side effect.
    """
    call_count = 0

    async def _on_disconnect() -> None:
        nonlocal call_count
        call_count += 1

    director, _personas, rooms, connectors = _build_director(on_user_disconnect=_on_disconnect)

    rooms[0].emit("participant_disconnected", _FakeParticipant("user-42"))
    rooms[1].emit("participant_disconnected", _FakeParticipant("user-42"))

    await asyncio.wait_for(director.session_shutdown.wait(), timeout=5.0)
    # Allow the teardown task to finish.
    assert director._shutdown_task is not None
    await director._shutdown_task

    assert connectors[0].aclose_calls == 1
    assert call_count == 1


# ---------------------------------------------------------------------------
# Heartbeat watchdog
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_watchdog_trips_when_no_user_present(monkeypatch):
    """No user across any room for the full timeout → force-trip the latch.

    Uses a tiny timeout (0.2 s) plus a short poll interval so the test
    finishes in well under the 5-second integration budget.
    """
    monkeypatch.setattr(director_module, "_HEARTBEAT_POLL_INTERVAL_S", 0.05)

    director, _personas, _rooms, connectors = _build_director(
        user_heartbeat_timeout_s=0.2,
    )
    # No remote_participants on either room ⇒ user is absent from start.
    director._last_user_seen = time.monotonic()

    watchdog = asyncio.create_task(director._heartbeat_watchdog())
    try:
        await asyncio.wait_for(director.session_shutdown.wait(), timeout=2.0)
    finally:
        watchdog.cancel()
        try:
            await watchdog
        except asyncio.CancelledError:
            pass

    assert director._shutdown_task is not None
    await director._shutdown_task
    assert connectors[0].aclose_calls == 1


@pytest.mark.asyncio
async def test_heartbeat_watchdog_holds_off_when_user_is_present(monkeypatch):
    """A real user in any room refreshes the heartbeat clock indefinitely."""
    monkeypatch.setattr(director_module, "_HEARTBEAT_POLL_INTERVAL_S", 0.05)

    director, _personas, rooms, _ = _build_director(
        user_heartbeat_timeout_s=0.3,
    )
    rooms[0].remote_participants["user-42"] = _FakeParticipant("user-42")

    watchdog = asyncio.create_task(director._heartbeat_watchdog())
    try:
        # Run the watchdog for several timeout cycles. The latch must
        # stay un-set the whole time.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(director.session_shutdown.wait(), timeout=1.0)
    finally:
        watchdog.cancel()
        try:
            await watchdog
        except asyncio.CancelledError:
            pass

    assert not director.session_shutdown.is_set()


@pytest.mark.asyncio
async def test_heartbeat_watchdog_ignores_avatar_only_rooms(monkeypatch):
    """An avatar in remote_participants is NOT a user heartbeat.

    Otherwise a secondary room with only its avatar would keep the show
    alive even after the user's tab dies, defeating the safety net.
    """
    monkeypatch.setattr(director_module, "_HEARTBEAT_POLL_INTERVAL_S", 0.05)

    director, _personas, rooms, _ = _build_director(
        user_heartbeat_timeout_s=0.2,
    )
    avatar_identity = f"{director_module._AVATAR_IDENTITY_PREFIX}chaos_agent"
    rooms[1].remote_participants[avatar_identity] = _FakeParticipant(avatar_identity)
    director._last_user_seen = time.monotonic()

    watchdog = asyncio.create_task(director._heartbeat_watchdog())
    try:
        await asyncio.wait_for(director.session_shutdown.wait(), timeout=2.0)
    finally:
        watchdog.cancel()
        try:
            await watchdog
        except asyncio.CancelledError:
            pass

    assert director.session_shutdown.is_set()


# ---------------------------------------------------------------------------
# Session-lifecycle log line
# ---------------------------------------------------------------------------


def _capture_lifecycle_payload(records: list[logging.LogRecord]) -> dict | None:
    """Pull the JSON blob off the ``session_lifecycle`` INFO line."""
    for rec in records:
        if rec.name != "podcast-commentary.director":
            continue
        msg = rec.getMessage()
        if msg.startswith("session_lifecycle "):
            return json.loads(msg.split(" ", 1)[1])
    return None


@pytest.mark.asyncio
async def test_shutdown_emits_session_lifecycle_log_with_expected_fields(caplog):
    """One INFO line per session at teardown, JSON-formatted, no PII."""
    avatar_startup_ms: dict[str, float] = {"fox": 1.234, "chaos_agent": 2.5}
    persona_names = ["fox", "chaos_agent"]
    personas = [_make_persona(n) for n in persona_names]
    primary = _FakeRoom("room-primary")
    secondary = _FakeRoom("room-secondary")
    contexts = [
        PersonaContext(persona=personas[0], room=primary, session=_FakeSession()),
        PersonaContext(persona=personas[1], room=secondary, session=_FakeSession()),
    ]
    director = Director(
        personas=contexts,
        session_id="sess-abc",
        avatar_startup_ms=avatar_startup_ms,
    )
    director._wire_room_listeners()
    director._total_turns = 7
    director._end_reason = "user_disconnect"

    with caplog.at_level(logging.INFO, logger="podcast-commentary.director"):
        await director.shutdown()

    payload = _capture_lifecycle_payload(caplog.records)
    assert payload is not None, "expected a session_lifecycle INFO log"

    assert payload["session_id"] == "sess-abc"
    assert payload["primary_persona"] == "fox"
    assert payload["secondary_personas"] == ["chaos_agent"]
    assert sorted(payload["room_names"]) == ["room-primary", "room-secondary"]
    assert payload["avatar_startup_ms"] == {"fox": 1234.0, "chaos_agent": 2500.0}
    assert payload["total_turns"] == 7
    assert payload["end_reason"] == "user_disconnect"


@pytest.mark.asyncio
async def test_shutdown_lifecycle_log_defaults_end_reason_to_error(caplog):
    """No user-disconnect / no timeout ⇒ end_reason falls back to "error"."""
    director, *_ = _build_director()

    with caplog.at_level(logging.INFO, logger="podcast-commentary.director"):
        await director.shutdown()

    payload = _capture_lifecycle_payload(caplog.records)
    assert payload is not None
    assert payload["end_reason"] == "error"


@pytest.mark.asyncio
async def test_user_disconnect_lifecycle_log_records_user_disconnect(caplog):
    """User leaving the room ⇒ end_reason="user_disconnect" on the log line."""
    director, _personas, rooms, _ = _build_director()
    primary_room = rooms[0]

    with caplog.at_level(logging.INFO, logger="podcast-commentary.director"):
        primary_room.emit("participant_disconnected", _FakeParticipant("user-42"))
        await asyncio.wait_for(director.session_shutdown.wait(), timeout=5.0)
        assert director._shutdown_task is not None
        await director._shutdown_task

    payload = _capture_lifecycle_payload(caplog.records)
    assert payload is not None
    assert payload["end_reason"] == "user_disconnect"


@pytest.mark.asyncio
async def test_heartbeat_timeout_lifecycle_log_records_timeout(caplog, monkeypatch):
    """Heartbeat-watchdog trip ⇒ end_reason="timeout" on the log line."""
    monkeypatch.setattr(director_module, "_HEARTBEAT_POLL_INTERVAL_S", 0.05)

    director, *_ = _build_director(user_heartbeat_timeout_s=0.2)
    director._last_user_seen = time.monotonic()

    with caplog.at_level(logging.INFO, logger="podcast-commentary.director"):
        watchdog = asyncio.create_task(director._heartbeat_watchdog())
        try:
            await asyncio.wait_for(director.session_shutdown.wait(), timeout=2.0)
        finally:
            watchdog.cancel()
            try:
                await watchdog
            except asyncio.CancelledError:
                pass
        assert director._shutdown_task is not None
        await director._shutdown_task

    payload = _capture_lifecycle_payload(caplog.records)
    assert payload is not None
    assert payload["end_reason"] == "timeout"


@pytest.mark.asyncio
async def test_lifecycle_log_contains_no_transcript_or_title(caplog):
    """PII guard: no podcast title or transcript content in the log payload."""
    director, *_ = _build_director()

    with caplog.at_level(logging.INFO, logger="podcast-commentary.director"):
        await director.shutdown()

    payload = _capture_lifecycle_payload(caplog.records)
    assert payload is not None
    # Whitelist the keys we expect — anything else would be a PII risk.
    expected_keys = {
        "session_id",
        "primary_persona",
        "secondary_personas",
        "room_names",
        "avatar_startup_ms",
        "total_turns",
        "end_reason",
        "duration_s",
    }
    assert set(payload.keys()) == expected_keys, (
        f"unexpected fields on lifecycle log (PII risk): {set(payload.keys()) - expected_keys}"
    )


@pytest.mark.asyncio
async def test_total_turns_increments_on_persona_turn_finalised():
    """Each finalised assistant turn nudges the per-session counter."""
    director, personas, _rooms, _ = _build_director()
    assert director._total_turns == 0

    await director._on_persona_turn_finalised(personas[0], "line one", angle="snark")
    await director._on_persona_turn_finalised(personas[1], "line two", angle=None)

    assert director._total_turns == 2
