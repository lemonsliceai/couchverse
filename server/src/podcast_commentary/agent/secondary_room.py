"""Lifecycle wrapper for a secondary ``rtc.Room`` opened from inside an
already-dispatched agent job.

The dual-room architecture has the agent worker — already bound to its
dispatched room — open a *second* ``rtc.Room``
for the co-host couch session. This class encapsulates only the connection
lifecycle (connect / event logging / close). It deliberately knows nothing
about avatars, sessions, or commentary — those live in the orchestrator.
"""

from __future__ import annotations

import logging

from livekit import rtc

from podcast_commentary.core.config import settings

logger = logging.getLogger(__name__)


class SecondaryRoomConnectError(RuntimeError):
    """Raised when the secondary ``rtc.Room`` cannot be established.

    Wraps the underlying ``rtc.ConnectError`` (or any other transport-level
    failure) so the orchestrator can fail the job cleanly without depending
    on LiveKit-specific exception types.
    """


class SecondaryRoomConnector:
    """Open and manage one secondary ``rtc.Room`` for a single persona.

    Usage:
        connector = SecondaryRoomConnector(room_name, token, persona="alien")
        room = await connector.connect()
        ...
        await connector.aclose()
    """

    def __init__(self, room_name: str, agent_token: str, persona: str) -> None:
        self._room_name = room_name
        self._agent_token = agent_token
        self._persona = persona
        self._room: rtc.Room | None = None

    @property
    def room(self) -> rtc.Room:
        if self._room is None:
            raise RuntimeError("SecondaryRoomConnector.connect() not awaited yet")
        return self._room

    @property
    def persona(self) -> str:
        return self._persona

    async def connect(self) -> rtc.Room:
        """Open the secondary room and wire up lifecycle logging.

        Uses the SDK's default ``RoomOptions()``; reconnection,
        keepalives, and timeouts are left at the LiveKit-recommended
        values.
        """
        if not settings.LIVEKIT_URL:
            raise SecondaryRoomConnectError(
                "LIVEKIT_URL is not configured; cannot open secondary room"
            )
        if self._room is not None:
            raise RuntimeError("SecondaryRoomConnector already connected")

        room = rtc.Room()
        self._wire_logging(room)
        try:
            await room.connect(settings.LIVEKIT_URL, self._agent_token, rtc.RoomOptions())
        except rtc.ConnectError as err:
            raise SecondaryRoomConnectError(
                f"connect failed for room={self._room_name} persona={self._persona}: {err}"
            ) from err
        except Exception as err:
            raise SecondaryRoomConnectError(
                f"connect failed for room={self._room_name} persona={self._persona}: {err}"
            ) from err

        self._room = room
        logger.info(
            "secondary room connected room=%s persona=%s",
            self._room_name,
            self._persona,
        )
        return room

    async def aclose(self) -> None:
        """Disconnect the room. Idempotent."""
        room = self._room
        if room is None:
            return
        self._room = None
        try:
            await room.disconnect()
        except Exception:
            logger.warning(
                "secondary room disconnect raised room=%s persona=%s",
                self._room_name,
                self._persona,
                exc_info=True,
            )

    def _wire_logging(self, room: rtc.Room) -> None:
        persona = self._persona
        room_name = self._room_name

        @room.on("disconnected")
        def _on_disconnected(reason) -> None:  # type: ignore[no-untyped-def]
            logger.info(
                "secondary room disconnected room=%s persona=%s reason=%s",
                room_name,
                persona,
                reason,
            )

        @room.on("reconnecting")
        def _on_reconnecting() -> None:
            logger.info(
                "secondary room reconnecting room=%s persona=%s",
                room_name,
                persona,
            )

        @room.on("reconnected")
        def _on_reconnected() -> None:
            logger.info(
                "secondary room reconnected room=%s persona=%s",
                room_name,
                persona,
            )

        @room.on("participant_connected")
        def _on_participant_connected(p: rtc.RemoteParticipant) -> None:
            logger.info(
                "secondary room participant joined room=%s persona=%s identity=%s kind=%s",
                room_name,
                persona,
                p.identity,
                p.kind,
            )

        @room.on("participant_disconnected")
        def _on_participant_disconnected(p: rtc.RemoteParticipant) -> None:
            logger.info(
                "secondary room participant left room=%s persona=%s identity=%s",
                room_name,
                persona,
                p.identity,
            )
