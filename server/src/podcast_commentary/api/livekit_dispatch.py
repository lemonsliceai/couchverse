"""Schema for the JSON metadata embedded in the primary room's
``RoomAgentDispatch``.

Single source of truth for the dispatch payload — the API server fills
this in on session creation, and the agent worker parses it back out
inside the dispatched job's entrypoint. Both ends round-trip through the
same Pydantic model so neither side has to hand-roll the JSON shape.

The shape::

    {
      "session_id": "...",
      "video_url": "...",
      "primary_persona": "<primary>",
      "all_personas": ["<primary>", "<secondary>", ...],
      "secondary_rooms": [
        { "persona": "<secondary>", "room_name": "...", "agent_token": "..." }
      ]
    }

Only the *primary* room's dispatch carries this payload. The dispatched
agent worker is bound to that room (``ctx.room``) and uses
``secondary_rooms`` to open additional ``rtc.Room`` connections for the
co-host personas.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field, model_validator


class PersonaDescriptor(BaseModel):
    """Per-persona startup data the agent needs to spin up an
    ``AgentSession`` in whichever room the persona lives in.
    """

    name: str
    label: str
    avatar_url: str = ""


class SecondaryRoomDispatch(BaseModel):
    """One secondary room the agent worker should self-join.

    Each entry maps a non-primary persona to the LiveKit room it lives in
    plus an ``agent: true`` JWT scoped to that room (minted server-side
    via :func:`podcast_commentary.api.livekit_tokens.mint_agent_token`).

    The primary persona is intentionally NOT included here — its room is
    the dispatched job's ``ctx.room`` and that connection is owned by the
    LiveKit agent worker, not by us.
    """

    persona: str
    room_name: str
    agent_token: str


class DispatchMetadata(BaseModel):
    """JSON payload embedded in the primary room's ``RoomAgentDispatch``.

    Round-trip schema. The API server constructs one of these and emits
    it via :meth:`to_metadata_json`; the agent worker parses it back via
    :meth:`from_metadata_json` against ``ctx.job.metadata``.

    Field semantics:

    * ``session_id`` / ``video_url`` / ``video_title`` — used for
      transcript logging and telemetry.
    * ``primary_persona`` — the persona whose room is ``ctx.room``.
    * ``all_personas`` — source-of-truth ordered list of every persona
      the agent should bring up, primary first. The agent must NOT
      reconstruct this from ``[primary_persona] + [s.persona for s in
      secondary_rooms]``; this field is the canonical list.
    * ``secondary_rooms`` — every persona OTHER than ``primary_persona``,
      with the room name and ``agent: true`` token the worker uses to
      connect. Required and must cover the full non-primary persona
      set.
    * ``personas`` — per-persona startup data (label, avatar_url). Kept
      so the agent can build each ``AgentSession`` without a round-trip
      back to the API server.
    """

    session_id: str
    video_url: str
    video_title: str = ""
    primary_persona: str
    all_personas: list[str]
    secondary_rooms: list[SecondaryRoomDispatch]
    personas: list[PersonaDescriptor] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_invariants(self) -> "DispatchMetadata":
        if self.primary_persona not in self.all_personas:
            raise ValueError(
                f"primary_persona={self.primary_persona!r} not in all_personas="
                f"{self.all_personas!r}"
            )
        secondary_personas = [s.persona for s in self.secondary_rooms]
        if self.primary_persona in secondary_personas:
            raise ValueError(
                f"primary_persona={self.primary_persona!r} must not appear in "
                f"secondary_rooms (its room is ctx.room)"
            )
        if len(set(secondary_personas)) != len(secondary_personas):
            raise ValueError(f"secondary_rooms has duplicate personas: {secondary_personas!r}")
        # Every non-primary persona must have a secondary room.
        expected_secondaries = {p for p in self.all_personas if p != self.primary_persona}
        actual_secondaries = set(secondary_personas)
        if expected_secondaries != actual_secondaries:
            missing = expected_secondaries - actual_secondaries
            extra = actual_secondaries - expected_secondaries
            raise ValueError(
                "secondary_rooms must cover every non-primary persona; "
                f"missing={sorted(missing)!r} extra={sorted(extra)!r}"
            )
        return self

    def to_metadata_json(self) -> str:
        """Serialize to the JSON string that goes into ``RoomAgentDispatch.metadata``."""
        return self.model_dump_json()

    @classmethod
    def from_metadata_json(cls, raw: str | None) -> "DispatchMetadata":
        """Parse ``ctx.job.metadata`` back into a :class:`DispatchMetadata`.

        Raises ``ValueError`` if ``raw`` is empty or not valid JSON for
        this schema — callers in the agent worker should treat that as a
        fatal misconfiguration, not a recoverable error.
        """
        if not raw:
            raise ValueError("dispatch metadata is empty")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as err:
            raise ValueError(f"dispatch metadata is not valid JSON: {err}") from err
        return cls.model_validate(payload)
