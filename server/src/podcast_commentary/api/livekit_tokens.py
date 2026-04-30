"""LiveKit token minting helpers.

The session-creation route in :mod:`podcast_commentary.api.routes.sessions`
mints user-facing tokens inline. This module hosts the tokens that the
*server* needs to mint on behalf of the agent worker — specifically the
``agent: true`` JWTs the worker uses to self-join secondary rooms.

These tokens are NEVER returned to the extension. They are embedded in the
primary room's ``RoomAgentDispatch`` metadata so the dispatched agent
worker, already bound to the primary room, can call ``rtc.Room.connect()``
against each secondary room without a round trip back to the API.
"""

from __future__ import annotations

from datetime import timedelta

from livekit import api

from podcast_commentary.core.config import settings

# Hard cap on a single Couchverse session. The agent token must outlive
# this; otherwise a long-running session would see secondary rooms drop
# at the JWT expiry. 4 h is well above any plausible "couch session" but
# leaves the budget under LiveKit's 6 h SDK default.
SESSION_MAX_DURATION = timedelta(hours=4)

# Buffer on top of the session cap. Absorbs clock skew between the API
# server and LiveKit Cloud plus any orchestrator-side delay between
# minting the token and the agent actually using it.
AGENT_TOKEN_TTL_BUFFER = timedelta(minutes=30)

# Final TTL applied to every secondary-room agent token.
AGENT_TOKEN_TTL = SESSION_MAX_DURATION + AGENT_TOKEN_TTL_BUFFER


def mint_agent_token(room_name: str, agent_identity: str) -> str:
    """Mint a LiveKit JWT that lets the agent worker join one secondary room.

    The grants are exercised end-to-end against LiveKit Cloud
    (``agent: true`` + the standard publish/subscribe trio), scoped to
    ``room_name`` so this token can never be used to join any other
    room.

    Args:
        room_name: LiveKit room the token is scoped to. The token's
            ``room`` claim is set to this exact value.
        agent_identity: Participant identity the agent should use when
            connecting. Should be deterministic per (persona, session)
            so log lines correlate cleanly across the API server, the
            agent worker, and the LiveKit dashboard.

    Returns:
        A signed JWT ready to be passed to ``rtc.Room.connect()``.
    """
    grants = api.VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
        agent=True,
    )
    return (
        api.AccessToken(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
        .with_identity(agent_identity)
        .with_name(agent_identity)
        .with_kind("agent")
        .with_grants(grants)
        .with_ttl(AGENT_TOKEN_TTL)
        .to_jwt()
    )
