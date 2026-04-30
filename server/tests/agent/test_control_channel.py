"""Unit tests for ``ControlChannel``.

Pins the dual-room contract:

  * Every published payload carries a UUIDv4 ``event_id``.
  * Publish fans out across the primary room and every registered
    secondary room — each gets one ``publish_data`` call with the SAME
    ``event_id`` so the extension can de-dup.
  * A failure on one room logs a WARNING but does not abort the
    publish on the others (best-effort fan-out).

We mock ``rtc.Room`` so the tests don't need a real LiveKit deployment.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict
from typing import Any

import pytest

from podcast_commentary.agent.control_channel import ControlChannel


class _FakeLocalParticipant:
    def __init__(self, *, raise_on_publish: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raise_on_publish = raise_on_publish

    async def publish_data(self, body: str, *, topic: str, reliable: bool) -> None:
        self.calls.append({"body": body, "topic": topic, "reliable": reliable})
        if self.raise_on_publish is not None:
            raise self.raise_on_publish


class _FakeRoom:
    """Minimal stand-in for ``rtc.Room`` capturing ControlChannel's surface."""

    def __init__(self, name: str = "fake", *, raise_on_publish: Exception | None = None) -> None:
        self.name = name
        self.handlers: dict[str, list[Any]] = defaultdict(list)
        self.local_participant = _FakeLocalParticipant(raise_on_publish=raise_on_publish)

    def on(self, event: str, fn=None):
        if fn is not None:
            self.handlers[event].append(fn)
            return fn

        def deco(handler):
            self.handlers[event].append(handler)
            return handler

        return deco

    def emit(self, event: str, *args: Any) -> None:
        for fn in self.handlers.get(event, []):
            fn(*args)


def _decode_payloads(room: _FakeRoom) -> list[dict[str, Any]]:
    return [json.loads(call["body"]) for call in room.local_participant.calls]


@pytest.mark.asyncio
async def test_publish_stamps_event_id_uuid4():
    room = _FakeRoom("primary")
    channel = ControlChannel(room)

    await channel.publish_commentary_start("persona_a")

    payloads = _decode_payloads(room)
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["type"] == "commentary_start"
    assert payload["speaker"] == "persona_a"
    # UUIDv4 — must parse and report version 4.
    parsed = uuid.UUID(payload["event_id"])
    assert parsed.version == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("publish_call", "expected_type"),
    [
        (lambda c: c.publish_commentary_start("persona_a"), "commentary_start"),
        (lambda c: c.publish_commentary_end("persona_a"), "commentary_end"),
        (lambda c: c.publish_agent_ready([{"name": "persona_a", "label": "Persona A"}]), "agent_ready"),
    ],
)
async def test_every_event_type_carries_event_id(publish_call, expected_type):
    """All existing event types must include the new ``event_id`` field."""
    room = _FakeRoom("primary")
    channel = ControlChannel(room)

    await publish_call(channel)

    payloads = _decode_payloads(room)
    assert payloads[0]["type"] == expected_type
    assert "event_id" in payloads[0]
    uuid.UUID(payloads[0]["event_id"])  # parses cleanly


@pytest.mark.asyncio
async def test_publish_fans_out_across_all_rooms_with_same_event_id():
    """N rooms → N data-channel sends, all sharing one ``event_id``.

    Pins the de-dup contract with the Chrome extension:
    the extension subscribes to all rooms but expects each event to be
    one logical event regardless of how many rooms it arrived through.
    """
    primary = _FakeRoom("primary")
    secondary_a = _FakeRoom("secondary-a")
    secondary_b = _FakeRoom("secondary-b")

    channel = ControlChannel(primary)
    channel.add_secondary_room(secondary_a)
    channel.add_secondary_room(secondary_b)

    await channel.publish_commentary_start("persona_a")

    primary_payloads = _decode_payloads(primary)
    secondary_a_payloads = _decode_payloads(secondary_a)
    secondary_b_payloads = _decode_payloads(secondary_b)

    # One send per room.
    assert len(primary_payloads) == 1
    assert len(secondary_a_payloads) == 1
    assert len(secondary_b_payloads) == 1

    # Same event_id everywhere — the extension keys de-dup off this.
    event_ids = {
        primary_payloads[0]["event_id"],
        secondary_a_payloads[0]["event_id"],
        secondary_b_payloads[0]["event_id"],
    }
    assert len(event_ids) == 1

    # Topic + reliable propagated identically to every room.
    for room in (primary, secondary_a, secondary_b):
        call = room.local_participant.calls[0]
        assert call["topic"] == "commentary.control"
        assert call["reliable"] is True


@pytest.mark.asyncio
async def test_separate_publishes_get_distinct_event_ids():
    room = _FakeRoom("primary")
    channel = ControlChannel(room)

    await channel.publish_commentary_start("persona_a")
    await channel.publish_commentary_end("persona_a")

    payloads = _decode_payloads(room)
    assert payloads[0]["event_id"] != payloads[1]["event_id"]


@pytest.mark.asyncio
async def test_publish_continues_when_one_room_fails(caplog):
    """A failing send on one room must not abort sends to the others."""
    primary = _FakeRoom("primary", raise_on_publish=RuntimeError("primary down"))
    secondary = _FakeRoom("secondary")

    channel = ControlChannel(primary)
    channel.add_secondary_room(secondary)

    with caplog.at_level(logging.WARNING, logger="podcast-commentary.control"):
        await channel.publish_commentary_start("persona_a")

    # Secondary still got the event despite the primary's failure.
    secondary_payloads = _decode_payloads(secondary)
    assert len(secondary_payloads) == 1
    assert secondary_payloads[0]["type"] == "commentary_start"
    # And the failure was logged at WARNING, not silently swallowed.
    assert any("Failed to publish" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_publish_logs_warning_when_secondary_fails(caplog):
    primary = _FakeRoom("primary")
    secondary = _FakeRoom("secondary", raise_on_publish=RuntimeError("secondary down"))

    channel = ControlChannel(primary)
    channel.add_secondary_room(secondary)

    with caplog.at_level(logging.WARNING, logger="podcast-commentary.control"):
        await channel.publish_commentary_end("persona_a")

    assert len(_decode_payloads(primary)) == 1
    assert any("Failed to publish" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_add_secondary_room_dedups():
    """Adding the same room twice — including the primary — is a no-op."""
    primary = _FakeRoom("primary")
    secondary = _FakeRoom("secondary")

    channel = ControlChannel(primary)
    channel.add_secondary_room(secondary)
    channel.add_secondary_room(secondary)
    channel.add_secondary_room(primary)

    await channel.publish_agent_ready([])

    assert len(_decode_payloads(primary)) == 1
    assert len(_decode_payloads(secondary)) == 1


@pytest.mark.asyncio
async def test_attach_listens_only_on_primary_room():
    """Inbound dispatch ignores secondary rooms — the extension only
    publishes ``skip``/``settings`` to the primary room."""
    primary = _FakeRoom("primary")
    secondary = _FakeRoom("secondary")
    channel = ControlChannel(primary)
    channel.add_secondary_room(secondary)
    channel.attach()

    assert len(primary.handlers["data_received"]) == 1
    assert len(secondary.handlers["data_received"]) == 0


@pytest.mark.asyncio
async def test_inbound_dispatch_routes_to_handler():
    primary = _FakeRoom("primary")
    channel = ControlChannel(primary)

    seen: list[dict[str, Any]] = []
    channel.register("skip", seen.append)
    channel.attach()

    class _Packet:
        data = json.dumps({"type": "skip"}).encode()

    primary.emit("data_received", _Packet())

    assert seen == [{"type": "skip"}]


@pytest.mark.asyncio
async def test_inbound_unknown_type_is_ignored():
    primary = _FakeRoom("primary")
    channel = ControlChannel(primary)
    channel.attach()

    class _Packet:
        data = json.dumps({"type": "nope"}).encode()

    # Must not raise even though no handler is registered.
    primary.emit("data_received", _Packet())


@pytest.mark.asyncio
async def test_inbound_handler_exception_is_swallowed_with_warning(caplog):
    primary = _FakeRoom("primary")
    channel = ControlChannel(primary)

    def _raise(_msg: dict) -> None:
        raise RuntimeError("handler boom")

    channel.register("skip", _raise)
    channel.attach()

    class _Packet:
        data = json.dumps({"type": "skip"}).encode()

    with caplog.at_level(logging.WARNING, logger="podcast-commentary.control"):
        primary.emit("data_received", _Packet())

    assert any("control handler" in rec.message for rec in caplog.records)
