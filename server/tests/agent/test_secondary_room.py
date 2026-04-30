"""Unit tests for ``SecondaryRoomConnector``.

We mock ``rtc.Room`` so the tests don't need the LiveKit FFI binary or a
real LiveKit deployment. The connector only touches a small surface of
``rtc.Room`` (``connect``, ``disconnect``, ``on``) so a hand-rolled fake
captures it cleanly.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

import pytest

from podcast_commentary.agent import secondary_room
from podcast_commentary.agent.secondary_room import (
    SecondaryRoomConnectError,
    SecondaryRoomConnector,
)


class _FakeParticipant:
    def __init__(self, identity: str, kind: str = "standard") -> None:
        self.identity = identity
        self.kind = kind


class _FakeRoom:
    """Minimal stand-in for ``rtc.Room`` capturing the connector's surface."""

    def __init__(self, *, connect_exc: Exception | None = None) -> None:
        self._handlers: dict[str, list[Any]] = defaultdict(list)
        self._connected = False
        self._connect_exc = connect_exc
        self.connect_calls: list[tuple[str, str, Any]] = []
        self.disconnect_calls = 0

    def on(self, event: str):
        def deco(fn):
            self._handlers[event].append(fn)
            return fn

        return deco

    def emit(self, event: str, *args: Any) -> None:
        for fn in self._handlers.get(event, []):
            fn(*args)

    async def connect(self, url: str, token: str, options: Any) -> None:
        self.connect_calls.append((url, token, options))
        if self._connect_exc is not None:
            raise self._connect_exc
        self._connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False


@pytest.fixture
def fake_room_factory(monkeypatch):
    """Patch ``rtc.Room`` inside the connector module to yield FakeRooms."""
    rooms: list[_FakeRoom] = []

    def _make(connect_exc: Exception | None = None):
        def _factory():
            r = _FakeRoom(connect_exc=connect_exc)
            rooms.append(r)
            return r

        monkeypatch.setattr(secondary_room.rtc, "Room", _factory)
        return rooms

    return _make


@pytest.fixture(autouse=True)
def _set_livekit_url(monkeypatch):
    monkeypatch.setattr(secondary_room.settings, "LIVEKIT_URL", "wss://test.livekit.cloud")


@pytest.mark.asyncio
async def test_connect_returns_room_and_exposes_property(fake_room_factory):
    rooms = fake_room_factory()
    connector = SecondaryRoomConnector(room_name="couch-b", agent_token="tok", persona="alien")

    room = await connector.connect()

    assert room is rooms[0]
    assert connector.room is rooms[0]
    assert rooms[0].connect_calls[0][0] == "wss://test.livekit.cloud"
    assert rooms[0].connect_calls[0][1] == "tok"


@pytest.mark.asyncio
async def test_room_property_before_connect_raises():
    connector = SecondaryRoomConnector(room_name="couch-b", agent_token="tok", persona="alien")
    with pytest.raises(RuntimeError):
        _ = connector.room


@pytest.mark.asyncio
async def test_double_connect_raises(fake_room_factory):
    fake_room_factory()
    connector = SecondaryRoomConnector(room_name="couch-b", agent_token="tok", persona="alien")
    await connector.connect()
    with pytest.raises(RuntimeError):
        await connector.connect()


@pytest.mark.asyncio
async def test_connect_failure_raises_secondary_room_connect_error(fake_room_factory):
    # rtc.ConnectError ≈ token-rejected / network failure.
    fake_room_factory(connect_exc=secondary_room.rtc.ConnectError("token rejected"))
    connector = SecondaryRoomConnector(room_name="couch-b", agent_token="bad", persona="alien")

    with pytest.raises(SecondaryRoomConnectError) as exc_info:
        await connector.connect()
    # Original error chained for diagnostics.
    assert isinstance(exc_info.value.__cause__, secondary_room.rtc.ConnectError)


@pytest.mark.asyncio
async def test_connect_unexpected_exception_wrapped(fake_room_factory):
    fake_room_factory(connect_exc=OSError("network down"))
    connector = SecondaryRoomConnector(room_name="couch-b", agent_token="tok", persona="alien")
    with pytest.raises(SecondaryRoomConnectError):
        await connector.connect()


@pytest.mark.asyncio
async def test_connect_without_livekit_url_raises(monkeypatch, fake_room_factory):
    fake_room_factory()
    monkeypatch.setattr(secondary_room.settings, "LIVEKIT_URL", None)
    connector = SecondaryRoomConnector(room_name="couch-b", agent_token="tok", persona="alien")
    with pytest.raises(SecondaryRoomConnectError):
        await connector.connect()


@pytest.mark.asyncio
async def test_aclose_disconnects_room(fake_room_factory):
    rooms = fake_room_factory()
    connector = SecondaryRoomConnector(room_name="couch-b", agent_token="tok", persona="alien")
    await connector.connect()

    await connector.aclose()
    assert rooms[0].disconnect_calls == 1
    # Idempotent.
    await connector.aclose()
    assert rooms[0].disconnect_calls == 1


@pytest.mark.asyncio
async def test_aclose_before_connect_is_noop(fake_room_factory):
    fake_room_factory()
    connector = SecondaryRoomConnector(room_name="couch-b", agent_token="tok", persona="alien")
    # Must not raise even though no room was opened.
    await connector.aclose()


@pytest.mark.asyncio
async def test_aclose_swallows_disconnect_exception(fake_room_factory, caplog):
    rooms = fake_room_factory()
    connector = SecondaryRoomConnector(room_name="couch-b", agent_token="tok", persona="alien")
    await connector.connect()

    async def _boom() -> None:
        raise RuntimeError("disconnect failed")

    rooms[0].disconnect = _boom  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING, logger=secondary_room.logger.name):
        await connector.aclose()

    assert any("disconnect raised" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_disconnect_event_logged_with_persona(fake_room_factory, caplog):
    rooms = fake_room_factory()
    connector = SecondaryRoomConnector(room_name="couch-b", agent_token="tok", persona="alien")
    await connector.connect()

    with caplog.at_level(logging.INFO, logger=secondary_room.logger.name):
        rooms[0].emit("disconnected", "ROOM_CLOSED")

    matched = [rec for rec in caplog.records if "disconnected" in rec.message]
    assert matched, "expected a disconnected log line"
    assert "persona=alien" in matched[-1].message
    assert "room=couch-b" in matched[-1].message


@pytest.mark.asyncio
async def test_reconnect_events_logged(fake_room_factory, caplog):
    rooms = fake_room_factory()
    connector = SecondaryRoomConnector(room_name="couch-b", agent_token="tok", persona="alien")
    await connector.connect()

    with caplog.at_level(logging.INFO, logger=secondary_room.logger.name):
        rooms[0].emit("reconnecting")
        rooms[0].emit("reconnected")

    messages = [rec.message for rec in caplog.records]
    assert any("reconnecting" in m and "persona=alien" in m for m in messages)
    assert any("reconnected" in m and "persona=alien" in m for m in messages)


@pytest.mark.asyncio
async def test_participant_join_leave_logged(fake_room_factory, caplog):
    rooms = fake_room_factory()
    connector = SecondaryRoomConnector(room_name="couch-b", agent_token="tok", persona="alien")
    await connector.connect()

    p = _FakeParticipant(identity="user-42", kind="standard")
    with caplog.at_level(logging.INFO, logger=secondary_room.logger.name):
        rooms[0].emit("participant_connected", p)
        rooms[0].emit("participant_disconnected", p)

    messages = [rec.message for rec in caplog.records]
    assert any("participant joined" in m and "user-42" in m for m in messages)
    assert any("participant left" in m and "user-42" in m for m in messages)


@pytest.mark.asyncio
async def test_forced_disconnect_path_keeps_property_accessible(fake_room_factory):
    """Forced server-side disconnect: the room emits ``disconnected`` but the
    connector hasn't called ``aclose()`` yet. The connector should still
    expose ``.room`` (so the orchestrator can inspect state) until aclose()."""
    rooms = fake_room_factory()
    connector = SecondaryRoomConnector(room_name="couch-b", agent_token="tok", persona="alien")
    await connector.connect()

    rooms[0].emit("disconnected", "SERVER_KICKED")

    assert connector.room is rooms[0]

    await connector.aclose()
    assert rooms[0].disconnect_calls == 1
