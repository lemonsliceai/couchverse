"""Agent entrypoint — wires up N PersonaAgents + a Director per job.

This module is intentionally thin: all conversation behaviour lives in
``PersonaAgent`` (per-persona) and ``Director`` (room-wide
orchestration). Here we only:

  * parse the dispatch metadata (which personas + per-persona avatar
    URLs + secondary-room JWTs)
  * for each persona, build an ``AgentSession`` (STT / LLM / TTS / VAD /
    turn detection from the persona's own ``FoxConfig``)
  * start the LemonSlice avatar with a *unique* participant identity per
    persona, in its own ``rtc.Room`` so the second-avatar RPC drops are
    avoided
  * construct the ``Director``, hand it the personas, and wait for every
    persona's ``ready`` event before delivering coordinated intros

Each persona owns its own ``rtc.Room``: ``ctx.room`` for the primary
persona, plus one ``SecondaryRoomConnector`` per non-primary persona,
so each room has at most one avatar.
"""

import asyncio
import contextlib
import logging
import time
from typing import Any

from dotenv import load_dotenv
from livekit import rtc
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
from podcast_commentary.agent.director import Director, PersonaContext
from podcast_commentary.agent.dispatch_metadata import DispatchMetadata
from podcast_commentary.agent.fox_config import FoxConfig, load_config
from podcast_commentary.agent.metrics import (
    avatar_startup_seconds,
    avatar_startup_total,
    playout_finished_rpc_total,
    watch_avatar_startup,
)
from podcast_commentary.agent.secondary_room import SecondaryRoomConnector
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
    # Surface registered metrics in worker boot logs so an operator can
    # confirm log-based metric scraping is wired up before any traffic.
    for metric in (
        playout_finished_rpc_total,
        avatar_startup_seconds,
        avatar_startup_total,
    ):
        logger.info(
            "metric registered: %s{%s} — %s",
            metric.name,
            ",".join(metric.label_names),
            metric.description,
        )


# Stable label values for ``room_role``. ``primary`` is the room the
# RoomAgentDispatch landed on (``ctx.room``); each non-primary persona
# lives in a "secondary" room joined via ``SecondaryRoomConnector``.
_ROOM_ROLE_PRIMARY = "primary"
_ROOM_ROLE_SECONDARY = "secondary"


server.setup_fnc = prewarm


def _build_session(config: FoxConfig, vad: Any) -> AgentSession:
    """Build one AgentSession from a persona's FoxConfig.

    Notes:
      * ``preemptive_generation=False`` — we control exactly when each
        persona speaks; no speculative generation.
      * ``resume_false_interruption=False`` — the avatar path sets
        ``audio_output=False``, whose audio sink doesn't implement
        ``.can_pause``; resume would log a warning and no-op.
      * ``allow_interruptions=False`` at the session level so VAD-triggered
        interruption never fires. The ONLY way a persona mid-utterance
        gets silenced is the user pressing "Skip commentary" — that routes
        through ``Director._handle_skip`` which calls
        ``SpeechGate.interrupt(force=True)`` explicitly.
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
        allow_interruptions=False,
    )


async def _start_avatar(
    *,
    config: FoxConfig,
    avatar_url: str | None,
    session: AgentSession,
    room: rtc.Room,
    identity: str,
    room_role: str,
    avatar_startup_ms: dict[str, float] | None = None,
) -> str | None:
    """Start the LemonSlice avatar for one persona under a unique identity.

    Returns the avatar session id (LemonSlice's internal handle) on
    success, or None if no avatar was configured / startup failed. We
    swallow startup failures so a single broken avatar doesn't kill the
    whole show — the persona can still speak audio-only.

    Records ``avatar_startup_seconds{persona, room_role}`` and
    ``avatar_startup_total{persona, outcome}`` for the ``error``,
    ``success`` and ``timeout`` outcomes. The histogram observation and
    success/timeout counters land asynchronously via
    ``watch_avatar_startup`` because the LemonSlice ``start()`` RPC
    returns before the avatar's video track is published, and we want
    the metric to reflect time-to-first-video, not time-to-RPC-ack.
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
    t0 = time.perf_counter()
    try:
        session_id = await avatar.start(session, room=room)
    except Exception:
        avatar_startup_total.inc(persona=config.name, outcome="error")
        logger.warning(
            "[%s] Avatar failed to start — continuing audio only",
            config.name,
            exc_info=True,
        )
        return None

    logger.info("[%s] Avatar started in %.2fs", config.name, time.perf_counter() - t0)

    # Spawn a background watcher: it records the histogram and the
    # success/timeout counters once the avatar's first video track
    # publishes (or the persona's startup_timeout_s elapses). Done as
    # fire-and-forget so ``_start_avatar`` doesn't block the entrypoint
    # waiting on a publish event the IntroSequencer is independently
    # gating on.
    # When caller hands a dict we surface the per-persona startup
    # elapsed (seconds) into it on success. The Director picks the
    # dict up at construction and renders it into ``avatar_startup_ms`` on
    # the session-lifecycle log at teardown.
    persona_name = config.name

    def _record(elapsed: float) -> None:
        if avatar_startup_ms is not None:
            avatar_startup_ms[persona_name] = elapsed

    watcher = asyncio.create_task(
        watch_avatar_startup(
            room=room,
            identity=identity,
            persona=config.name,
            room_role=room_role,
            started_at=t0,
            timeout=config.avatar.startup_timeout_s,
            on_success=_record,
        ),
        name=f"avatar_startup_metric:{config.name}",
    )
    watcher.add_done_callback(_log_metric_task_exception)
    return session_id


def _log_metric_task_exception(task: asyncio.Task) -> None:
    """Surface watcher exceptions so a metric bug isn't silent."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "Avatar startup metric watcher %r raised: %s",
            task.get_name(),
            exc,
            exc_info=exc,
        )


def _avatar_identity_for(persona_name: str) -> str:
    """Per-persona avatar participant identity.

    Each LemonSlice instance must publish under a unique identity or
    LiveKit treats them as the same participant and only one set of
    tracks survives. The Chrome extension routes incoming tracks by
    matching this prefix — see ``sidepanel.js``.
    """
    return f"lemonslice-avatar-{persona_name}"


# Track-name prefix the extension uses to peel a persona off a direct-publish
# audio track. Each audio-only persona publishes under ``persona-<name>``
# so the side-panel can route every persona's track separately even though
# they share one ``local_participant``.
_PERSONA_TRACK_PREFIX = "persona-"


def _persona_track_name(persona_name: str) -> str:
    return f"{_PERSONA_TRACK_PREFIX}{persona_name}"


@server.rtc_session(agent_name=settings.AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    """Per-job entrypoint — one ``AgentSession`` + room per persona.

    Flow:
      1. Parse the dispatch metadata (Pydantic round-trip).
      2. Connect ``ctx.room`` (the primary persona's room).
      3. Open every secondary persona's room in parallel — fail-fast if
         any connector fails.
      4. For each persona, in ``all_personas`` order, build an
         ``AgentSession`` + ``AvatarSession`` bound to *its own* room.
      5. Hand the personas to the Director and start.

    The IntroSequencer iterates ``personas`` in the order we append them
    here, so ``all_personas`` (the canonical list) drives intro ordering
    deterministically.
    """
    try:
        meta = DispatchMetadata.from_metadata_json(ctx.job.metadata)
    except ValueError as err:
        # Misconfiguration: the API and agent disagree on the wire shape.
        # Refuse to run — the alternative is half-broken sessions whose
        # failure mode varies with whichever required field is missing.
        logger.error("Invalid dispatch metadata — aborting: %s", err)
        return

    log_prefix = f"[{meta.primary_persona}]"
    logger.info(
        "%s Job dispatched: primary=%s all_personas=%s secondary_rooms=%d",
        log_prefix,
        meta.primary_persona,
        meta.all_personas,
        len(meta.secondary_rooms),
    )

    # Connect primary BEFORE building any session so ``ctx.room.local_participant``
    # exists when Director.start() and avatar.start() reach for it.
    await ctx.connect()

    secondary_connectors: list[SecondaryRoomConnector] = [
        SecondaryRoomConnector(
            room_name=s.room_name,
            agent_token=s.agent_token,
            persona=s.persona,
        )
        for s in meta.secondary_rooms
    ]

    if secondary_connectors:
        try:
            # Fail-fast: ``return_exceptions=False`` propagates the first
            # error immediately. A single broken connector means the show
            # can't run as designed, so we'd rather abort the job cleanly
            # than ship a degraded experience the user can't see is broken.
            await asyncio.gather(
                *(c.connect() for c in secondary_connectors),
                return_exceptions=False,
            )
        except Exception as err:
            logger.error(
                "%s Secondary room connect failed — failing job fast: %s",
                log_prefix,
                err,
                exc_info=True,
            )
            # Best-effort cleanup of any connectors that did open before
            # the failure; ``aclose`` is a no-op for connectors that never
            # connected.
            await asyncio.gather(
                *(c.aclose() for c in secondary_connectors),
                return_exceptions=True,
            )
            raise

    connector_by_persona: dict[str, SecondaryRoomConnector] = {
        c.persona: c for c in secondary_connectors
    }
    descriptor_by_name = {p.name: p for p in meta.personas}

    vad = ctx.proc.userdata["vad"]

    personas: list[PersonaAgent] = []
    avatar_identities: dict[str, str] = {}
    # Live mutable dict shared with the avatar startup watcher and
    # handed to the Director for the session-lifecycle log.
    avatar_startup_ms: dict[str, float] = {}
    # Per-persona ``PersonaContext`` triples handed to the Director.
    # Built in ``meta.all_personas`` order so the primary
    # (user-facing) persona is the first context, matching the Director's
    # primary-room convention.
    persona_contexts: list[PersonaContext] = []
    # Diagnostic snapshot of each persona's runtime — kept for
    # logging and to make the avatar/session linkage greppable.
    persona_runtimes: list[dict[str, Any]] = []

    for persona_name in meta.all_personas:
        is_primary = persona_name == meta.primary_persona
        if is_primary:
            persona_room: rtc.Room = ctx.room
        else:
            connector = connector_by_persona.get(persona_name)
            if connector is None:
                # all_personas listed a non-primary persona that has no
                # secondary_rooms entry. The Pydantic model already enforces
                # the inverse (no duplicates, primary not in secondaries),
                # but this catches a stray all_personas drift.
                logger.error(
                    "[%s] Persona has no primary or secondary room — aborting job",
                    persona_name,
                )
                await asyncio.gather(
                    *(c.aclose() for c in secondary_connectors),
                    return_exceptions=True,
                )
                return
            persona_room = connector.room

        descriptor = descriptor_by_name.get(persona_name)
        avatar_url = descriptor.avatar_url if descriptor else None
        config = load_config(persona_name)

        logger.info(
            "[%s] === SYSTEM PROMPT ===\n%s\n=== END SYSTEM PROMPT ===",
            persona_name,
            config.persona.system_prompt,
        )

        session = _build_session(config, vad=vad)
        identity = _avatar_identity_for(persona_name)
        avatar_session_id = await _start_avatar(
            config=config,
            avatar_url=avatar_url,
            session=session,
            room=persona_room,
            identity=identity,
            room_role=_ROOM_ROLE_PRIMARY if is_primary else _ROOM_ROLE_SECONDARY,
            avatar_startup_ms=avatar_startup_ms,
        )
        if avatar_session_id is not None:
            avatar_identities[persona_name] = identity

        persona = PersonaAgent(config=config, session_id=meta.session_id)
        personas.append(persona)

        # Each persona owns its room outright, so a name collision between
        # persona audio tracks is impossible — but we keep the per-persona
        # track name anyway so the extension's routing logic stays uniform.
        has_avatar = avatar_session_id is not None
        if has_avatar:
            audio_output: room_io.AudioOutputOptions | bool = False
        else:
            audio_output = room_io.AudioOutputOptions(
                track_name=_persona_track_name(persona_name),
            )
        await session.start(
            agent=persona,
            room=persona_room,
            room_options=room_io.RoomOptions(
                audio_input=False,
                audio_output=audio_output,
                close_on_disconnect=False,
            ),
        )

        persona_contexts.append(PersonaContext(persona=persona, room=persona_room, session=session))
        persona_runtimes.append(
            {
                "persona": persona,
                "session": session,
                "room": persona_room,
                "is_primary": is_primary,
                "avatar_session_id": avatar_session_id,
                "avatar_identity": identity if avatar_session_id is not None else None,
            }
        )
        logger.info(
            "[%s] AgentSession started in room=%s (primary=%s avatar=%s)",
            persona_name,
            getattr(persona_room, "name", "?"),
            is_primary,
            avatar_session_id is not None,
        )

    # Wait for every persona's on_enter to compose its SpeechGate.
    await asyncio.gather(*(p.ready.wait() for p in personas))

    async def _end_job_on_user_disconnect() -> None:
        logger.info("%s User disconnect → requesting job shutdown", log_prefix)
        try:
            shutdown = getattr(ctx, "shutdown", None)
            if shutdown is None:
                return
            result = shutdown(reason="user_disconnected")
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.warning("ctx.shutdown raised", exc_info=True)

    # Director consumes the per-persona PersonaContext triples — the first
    # context (primary persona) supplies the user-facing room used for
    # the ``commentary.control`` data channel, podcast-audio listener,
    # and participant-disconnected detection.
    #
    # Hand the Director the secondary connectors so its shutdown
    # latch (user disconnect / heartbeat watchdog) can ``aclose()`` them
    # in parallel before tearing the show down. The post-shutdown
    # callback below remains as a defensive backup for non-latch shutdown
    # paths (worker crash, dispatched-room close); ``aclose()`` is
    # idempotent so the double-call is safe.
    director = Director(
        personas=persona_contexts,
        avatar_identities=avatar_identities,
        session_id=meta.session_id,
        on_user_disconnect=_end_job_on_user_disconnect,
        secondary_connectors=secondary_connectors,
        avatar_startup_ms=avatar_startup_ms,
    )

    async def _close_secondary_connectors() -> None:
        if not secondary_connectors:
            return
        with contextlib.suppress(Exception):
            await asyncio.gather(
                *(c.aclose() for c in secondary_connectors),
                return_exceptions=True,
            )

    # Order matters: Director.shutdown first so live speech handles get
    # interrupted while the rooms are still up; then close secondary
    # rooms. LiveKit runs shutdown callbacks in registration order.
    ctx.add_shutdown_callback(director.shutdown)
    ctx.add_shutdown_callback(_close_secondary_connectors)

    await director.start()


if __name__ == "__main__":
    cli.run_app(server)
