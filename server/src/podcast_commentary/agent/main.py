"""Agent entrypoint — wires up N PersonaAgents + a Director per job.

This module is intentionally thin: all conversation behaviour lives in
``PersonaAgent`` (per-persona) and ``Director`` (room-wide
orchestration). Here we only:

  * parse the job metadata (which personas + per-persona avatar URLs)
  * for each persona, build an ``AgentSession`` (STT / LLM / TTS / VAD /
    turn detection from the persona's own ``FoxConfig``)
  * start the LemonSlice avatar with a *unique* participant identity per
    persona so multiple avatars can coexist in the room
  * construct the ``Director``, hand it the personas + the primary
    AgentSession, and wait for both personas' ``ready`` events before
    delivering coordinated intros

Only the *primary* persona (first in ``PERSONAS``) consumes the user
microphone. Secondary personas set ``audio_input=False`` so we don't run
STT twice on the same audio.
"""

import asyncio
import json
import logging
import time
from typing import Any

from dotenv import load_dotenv
from livekit.agents import (
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    cli,
    room_io,
)
from livekit.plugins import elevenlabs, groq, lemonslice, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from podcast_commentary.agent.comedian import PersonaAgent
from podcast_commentary.agent.director import Director
from podcast_commentary.agent.fox_config import FoxConfig, load_config
from podcast_commentary.core.config import settings

logger = logging.getLogger("podcast-commentary.agent")

load_dotenv()
load_dotenv(".env.local", override=True)


server = AgentServer(num_idle_processes=2)


def prewarm(proc: JobProcess) -> None:
    """Preload Silero VAD once per worker process.

    All personas in a job share this single VAD instance — Silero is
    stateless across calls, so sharing is safe and saves the ~80 MB
    per-instance model load.
    """
    proc.userdata["vad"] = silero.VAD.load(activation_threshold=0.5)


server.setup_fnc = prewarm


def _parse_job_metadata(ctx: JobContext) -> dict:
    if not ctx.job.metadata:
        return {}
    try:
        return json.loads(ctx.job.metadata)
    except json.JSONDecodeError:
        logger.warning("Failed to parse job metadata: %s", ctx.job.metadata)
        return {}


def _resolve_personas(metadata: dict) -> list[dict[str, str]]:
    """Return the persona descriptors (name + avatar_url) for this job.

    The API server includes a ``personas`` list in metadata. We fall back
    to building one from ``settings.PERSONAS`` and each preset's own
    ``AvatarConfig.avatar_url`` so a stale API still functions during a
    rolling deploy.
    """
    personas = metadata.get("personas")
    if isinstance(personas, list) and personas:
        return personas

    descriptors: list[dict[str, str]] = []
    for name in (settings.PERSONAS or settings.FOX_CONFIG or "default").split(","):
        name = name.strip()
        if not name:
            continue
        cfg = load_config(name)
        descriptors.append({"name": name, "label": name, "avatar_url": cfg.avatar.avatar_url})
    return descriptors


def _build_session(config: FoxConfig, vad: Any) -> AgentSession:
    """Build one AgentSession from a persona's FoxConfig.

    Notes:
      * ``preemptive_generation=False`` — we control exactly when each
        persona speaks; no speculative generation.
      * ``resume_false_interruption=False`` — the avatar path sets
        ``audio_output=False``, whose audio sink doesn't implement
        ``.can_pause``; resume would log a warning and no-op.
      * Session-level ``allow_interruptions`` stays at its default
        (True). Per-turn we enforce non-interruption via
        ``SpeechGate.speak``.
    """
    return AgentSession(
        stt=groq.STT(model=config.stt.model),
        llm=groq.LLM(
            model=config.llm.model,
            max_completion_tokens=config.llm.max_tokens,
        ),
        tts=elevenlabs.TTS(
            model=config.tts.model,
            voice_id=config.tts.voice_id,
            voice_settings=elevenlabs.VoiceSettings(
                stability=config.tts.stability,
                similarity_boost=config.tts.similarity_boost,
                speed=config.tts.speed,
            ),
        ),
        turn_detection=MultilingualModel(),
        vad=vad,
        preemptive_generation=False,
        resume_false_interruption=False,
    )


async def _start_avatar(
    *,
    config: FoxConfig,
    avatar_url: str | None,
    session: AgentSession,
    ctx: JobContext,
    identity: str,
) -> str | None:
    """Start the LemonSlice avatar for one persona under a unique identity.

    Returns the avatar session id (LemonSlice's internal handle) on
    success, or None if no avatar was configured / startup failed. We
    swallow startup failures so a single broken avatar doesn't kill the
    whole show — the persona can still speak audio-only.
    """
    if not avatar_url:
        logger.info("[%s] No avatar_url — skipping avatar", config.name)
        return None

    avatar = lemonslice.AvatarSession(
        agent_image_url=avatar_url,
        agent_prompt=config.avatar.active_prompt,
        agent_idle_prompt=config.avatar.idle_prompt,
        avatar_participant_identity=identity,
    )
    try:
        t0 = time.perf_counter()
        session_id = await avatar.start(session, room=ctx.room)
        logger.info("[%s] Avatar started in %.2fs", config.name, time.perf_counter() - t0)
        return session_id
    except Exception:
        logger.warning(
            "[%s] Avatar failed to start — continuing audio only",
            config.name,
            exc_info=True,
        )
        return None


async def _wait_for_avatar_participant(room: Any, identity: str, timeout: float) -> bool:
    ready = asyncio.Event()

    def _on_participant(participant: Any) -> None:
        if participant.identity == identity:
            ready.set()

    room.on("participant_connected", _on_participant)
    for p in room.remote_participants.values():
        if p.identity == identity:
            ready.set()

    if ready.is_set():
        return True
    try:
        await asyncio.wait_for(ready.wait(), timeout=timeout)
        return True
    except TimeoutError:
        logger.warning("Avatar %s did not connect within %.0fs", identity, timeout)
        return False


def _avatar_identity_for(persona_name: str) -> str:
    """Per-persona avatar participant identity.

    Each LemonSlice instance must publish under a unique identity or
    LiveKit treats them as the same participant and only one set of
    tracks survives. The Chrome extension routes incoming tracks by
    matching this prefix — see ``sidepanel.js``.
    """
    return f"lemonslice-avatar-{persona_name}"


@server.rtc_session(agent_name=settings.AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    """Per-job entrypoint — called by the LiveKit agent worker."""
    metadata = _parse_job_metadata(ctx)
    persona_descriptors = _resolve_personas(metadata)
    if not persona_descriptors:
        logger.error("No personas resolved for job — aborting")
        return

    # Connect BEFORE starting any session so local_participant is usable
    # inside Director.start() (publish_data needs it).
    await ctx.connect()

    vad = ctx.proc.userdata["vad"]
    session_id = metadata.get("session_id")

    sessions: list[AgentSession] = []
    personas: list[PersonaAgent] = []
    avatar_identities: list[str] = []

    # Build each persona + its session + its avatar. The first persona
    # in the list is the *primary* — it owns user-mic STT.
    for idx, descriptor in enumerate(persona_descriptors):
        name = descriptor["name"]
        avatar_url = descriptor.get("avatar_url")
        config = load_config(name)

        logger.info(
            "[%s] === SYSTEM PROMPT ===\n%s\n=== END SYSTEM PROMPT ===",
            name,
            config.persona.system_prompt,
        )

        session = _build_session(config, vad=vad)
        sessions.append(session)

        identity = _avatar_identity_for(name)
        avatar_identities.append(identity)
        await _start_avatar(
            config=config,
            avatar_url=avatar_url,
            session=session,
            ctx=ctx,
            identity=identity,
        )

        persona = PersonaAgent(config=config, session_id=session_id)
        personas.append(persona)

        # Only the primary owns the user mic — secondaries skip audio_input
        # so we don't run STT twice on the same MediaStreamTrack.
        is_primary = idx == 0
        await session.start(
            agent=persona,
            room=ctx.room,
            room_options=room_io.RoomOptions(
                audio_input=True if is_primary else False,
                # Avatar audio is routed through LemonSlice — disable the
                # session's own audio output so it doesn't double-publish.
                audio_output=False,
            ),
        )

    # Wait for every persona's on_enter to compose its SpeechGate.
    await asyncio.gather(*(p.ready.wait() for p in personas))

    # Director takes over: intros, speaker selection, user PTT routing.
    director = Director(
        personas=personas,
        room=ctx.room,
        primary_session=sessions[0],
        session_id=session_id,
    )

    # Wait briefly for each avatar participant to actually connect so the
    # extension has the video tracks rendered before the intro lands.
    timeout = max(p.config.avatar.startup_timeout_s for p in personas)
    await asyncio.gather(
        *(
            _wait_for_avatar_participant(ctx.room, ident, timeout=timeout)
            for ident in avatar_identities
        )
    )

    await director.start()
    ctx.add_shutdown_callback(director.shutdown)


if __name__ == "__main__":
    cli.run_app(server)
