"""commentary.control data channel — outbound publish + inbound dispatch.

Owns the wire format for the LiveKit data channel the Chrome extension
listens on for ``commentary_start`` / ``commentary_end`` (highlight the
right avatar) and ``agent_ready`` (enumerate speakers), and dispatches
inbound packets (``skip``, ``settings``) to handlers registered by the
Director.

In dual-room mode the agent owns one ``rtc.Room`` per persona; the
extension joins every room and would otherwise see each control event
once per room. To keep the UI single-sourced we fan every outbound
event out to all rooms and stamp each event with a ``event_id`` UUID
so the extension can de-dup. Inbound dispatch only listens on the
primary room — the extension publishes ``skip`` / ``settings`` once,
to the room it considers user-facing.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

from livekit import rtc

logger = logging.getLogger("podcast-commentary.control")


class ControlChannel:
    """Bidirectional adapter for the ``commentary.control`` topic."""

    def __init__(self, room: rtc.Room) -> None:
        self._primary_room = room
        self._secondary_rooms: list[rtc.Room] = []
        self._handlers: dict[str, Callable[[dict[str, Any]], None]] = {}

    def add_secondary_room(self, room: rtc.Room) -> None:
        """Register a secondary room for outbound fan-out.

        Secondary rooms are connected dynamically once per-persona
        tokens are minted, so they're added post-construction.
        Inbound dispatch is unaffected — only the primary room receives
        ``skip`` / ``settings`` packets from the extension.
        """
        if room is self._primary_room or room in self._secondary_rooms:
            return
        self._secondary_rooms.append(room)

    def register(self, msg_type: str, handler: Callable[[dict[str, Any]], None]) -> None:
        """Bind a handler for an inbound message type. Last-write-wins."""
        self._handlers[msg_type] = handler

    def attach(self) -> None:
        """Start dispatching incoming ``data_received`` packets."""
        self._primary_room.on("data_received", self._on_data_received)

    def _on_data_received(self, data_packet: Any) -> None:
        msg = self._parse(data_packet)
        if msg is None:
            return
        handler = self._handlers.get(msg.get("type"))
        if handler is None:
            return
        try:
            handler(msg)
        except Exception:
            logger.warning("control handler for %r failed", msg.get("type"), exc_info=True)

    @staticmethod
    def _parse(data_packet: Any) -> dict | None:
        raw = getattr(data_packet, "data", b"")
        try:
            return json.loads(raw.decode())
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            return None

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------
    async def publish_commentary_start(self, speaker: str, *, phase: str = "commentary") -> None:
        await self._publish({"type": "commentary_start", "speaker": speaker, "phase": phase})

    async def publish_commentary_end(self, speaker: str, *, phase: str = "commentary") -> None:
        await self._publish({"type": "commentary_end", "speaker": speaker, "phase": phase})

    async def publish_agent_ready(self, speakers: list[dict[str, str]]) -> None:
        await self._publish({"type": "agent_ready", "speakers": speakers})

    async def _publish(self, payload: dict) -> None:
        # Stamp once, before fan-out, so every room sees the same id.
        # The extension keys de-dup off this.
        payload = {**payload, "event_id": str(uuid.uuid4())}
        body = json.dumps(payload)
        for room in (self._primary_room, *self._secondary_rooms):
            try:
                await room.local_participant.publish_data(
                    body,
                    topic="commentary.control",
                    reliable=True,
                )
            except Exception:
                # Best-effort fan-out: keep going so the other rooms
                # still see the event. Missed events are recovered by
                # the extension's per-event ack pattern.
                logger.warning(
                    "Failed to publish %s to room %r",
                    payload.get("type"),
                    getattr(room, "name", "?"),
                    exc_info=True,
                )


__all__ = ["ControlChannel"]
