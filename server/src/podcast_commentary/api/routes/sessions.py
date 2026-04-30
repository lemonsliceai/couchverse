import logging
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from livekit import api
from pydantic import BaseModel

from podcast_commentary.agent.fox_config import _resolve_persona_names, load_config
from podcast_commentary.api.livekit_dispatch import (
    DispatchMetadata,
    PersonaDescriptor,
    SecondaryRoomDispatch,
)
from podcast_commentary.api.livekit_tokens import mint_agent_token
from podcast_commentary.core.config import settings
from podcast_commentary.core.db import (
    create_session,
    end_session,
    get_session,
)

logger = logging.getLogger("podcast-commentary.sessions")
router = APIRouter()


class CreateSessionRequest(BaseModel):
    video_url: str
    video_title: str | None = None


class RoomEntry(BaseModel):
    """One LiveKit room the extension should join for this session.

    Exactly one entry has ``role == "primary"``: its token carries the
    ``RoomAgentDispatch`` that triggers the agent worker. Secondary
    entries are plain participant tokens; the agent self-joins those
    rooms via metadata embedded in the primary dispatch.
    """

    persona: str
    room_name: str
    token: str
    role: Literal["primary", "secondary"]


class CreateSessionResponse(BaseModel):
    session_id: str
    livekit_url: str
    video_url: str
    rooms: list[RoomEntry]


def _persona_room_name(session_id: str, persona: str) -> str:
    """Deterministic per-persona room name. Stable for the lifetime of a session."""
    return f"{session_id}-{persona}"


@router.post("/api/sessions", response_model=CreateSessionResponse)
async def create_session_route(request: CreateSessionRequest):
    persona_names = _resolve_persona_names()
    primary_persona = settings.PRIMARY_PERSONA
    if primary_persona not in persona_names:
        raise HTTPException(
            status_code=500,
            detail=(
                f"PRIMARY_PERSONA={primary_persona!r} is not in PERSONAS={persona_names}. "
                "Update server config."
            ),
        )

    session_id = str(uuid4())
    user_identity = f"user-{uuid4().hex[:8]}"

    persona_to_room: dict[str, str] = {
        persona: _persona_room_name(session_id, persona) for persona in persona_names
    }
    primary_room_name = persona_to_room[primary_persona]

    # Persist the session row with the per-persona room mapping. The
    # legacy ``room_name`` column gets the primary room.
    await create_session(
        primary_room_name,
        request.video_url,
        request.video_title,
        rooms=persona_to_room,
        session_id=session_id,
    )

    # Per-persona startup data the agent uses to spin up an AgentSession
    # in whichever room the persona ends up in. Same shape for primary
    # and secondaries — only the room differs.
    personas_meta: list[PersonaDescriptor] = []
    for name in persona_names:
        cfg = load_config(name)
        personas_meta.append(
            PersonaDescriptor(
                name=name,
                label=cfg.persona.speaker_label or name,
                avatar_url=cfg.avatar.avatar_url,
            )
        )

    # Mint one ``agent: true`` JWT per secondary room so the dispatched
    # agent worker can self-join those rooms without a round-trip back
    # to the API. The primary persona is intentionally omitted — its
    # room is the dispatched job's ``ctx.room`` and the worker is
    # already bound to it.
    secondary_rooms_meta: list[SecondaryRoomDispatch] = [
        SecondaryRoomDispatch(
            persona=name,
            room_name=persona_to_room[name],
            agent_token=mint_agent_token(
                persona_to_room[name],
                f"agent-{name}-{session_id}",
            ),
        )
        for name in persona_names
        if name != primary_persona
    ]

    dispatch_metadata = DispatchMetadata(
        session_id=session_id,
        video_url=request.video_url,
        video_title=request.video_title or "",
        primary_persona=primary_persona,
        all_personas=list(persona_names),
        secondary_rooms=secondary_rooms_meta,
        personas=personas_meta,
    )

    # One entry per persona so the extension spawns a RoomController per
    # room.
    rooms: list[RoomEntry] = []
    for persona in persona_names:
        room_name = persona_to_room[persona]
        is_primary = persona == primary_persona

        builder = (
            api.AccessToken(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
            .with_identity(user_identity)
            .with_name(user_identity)
            .with_grants(api.VideoGrants(room_join=True, room=room_name))
        )
        if is_primary:
            # RoomAgentDispatch lives ONLY on the primary token. Its
            # metadata carries everything the agent needs to spin up
            # secondary rooms.
            builder = builder.with_room_config(
                api.RoomConfiguration(
                    agents=[
                        api.RoomAgentDispatch(
                            agent_name=settings.AGENT_NAME,
                            metadata=dispatch_metadata.to_metadata_json(),
                        )
                    ],
                )
            )

        rooms.append(
            RoomEntry(
                persona=persona,
                room_name=room_name,
                token=builder.to_jwt(),
                role="primary" if is_primary else "secondary",
            )
        )

    logger.info(
        "Created session %s personas=%s primary_room=%s rooms_emitted=%d",
        session_id,
        persona_names,
        primary_room_name,
        len(rooms),
    )

    return CreateSessionResponse(
        session_id=session_id,
        livekit_url=settings.LIVEKIT_URL,
        video_url=request.video_url,
        rooms=rooms,
    )


@router.get("/api/sessions/{session_id}")
async def get_session_route(session_id: str):
    session = await get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.post("/api/sessions/{session_id}/end")
async def end_session_route(session_id: str):
    session = await get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    await end_session(session_id)
    return {"status": "ended"}


@router.get("/health")
async def health():
    return {"status": "ok"}
