"""Agent-side import surface for the ``RoomAgentDispatch`` metadata schema.

The model itself lives in :mod:`podcast_commentary.api.livekit_dispatch`
because the API server *creates* the metadata and the agent *consumes*
it — keeping a single Pydantic model means neither side can drift.

This module re-exports that model under an agent-flavoured path so the
worker doesn't have to reach into the ``api`` package, and so future
moves of the schema (e.g. into a shared ``core`` location) only require
updating this one re-export.
"""

from __future__ import annotations

from podcast_commentary.api.livekit_dispatch import (
    DispatchMetadata,
    PersonaDescriptor,
    SecondaryRoomDispatch,
)

__all__ = [
    "DispatchMetadata",
    "PersonaDescriptor",
    "SecondaryRoomDispatch",
]
