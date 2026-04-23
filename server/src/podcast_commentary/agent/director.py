"""Director — orchestrates which PersonaAgent speaks each turn.

The Director owns *all the shared concerns* in a multi-persona room:

  * the ``PodcastPipeline`` (one STT stream feeding the rolling transcript)
  * the ``FullTranscript`` (everyone reacts to the same podcast text)
  * the ``CommentaryTimer`` (one cadence — MIN_GAP / burst — for the room
    as a whole, not one per persona; otherwise two personas would each
    fire their own MIN_GAP and the room would feel stacked)
  * the ``commentary.control`` data channel (start/end + ``speaker`` field
    so the extension can highlight the right avatar)
  * intro coordination (Fox first, then Alien — never simultaneous)
  * user push-to-talk — interrupts whoever's speaking, then routes the
    user reply to the persona that was *not* talking (or the primary if
    nobody was) so the human gets a fresh voice

Speaker selection is a small fast LLM call (``DIRECTOR_LLM_MODEL``,
defaults to the same Groq Llama as the comedians). The judge sees the
recent transcript, what each persona said last, who spoke most recently,
and is told to optimise for "what would be funniest right now". It
returns ``{"speaker": "<name>" | "skip", "reason": "..."}``. Safety
rails on top:

  * a hard cap on consecutive same-speaker turns (``DIRECTOR_MAX_CONSECUTIVE``)
  * a fallback to round-robin when the LLM call fails or returns garbage
  * the shared timer's MIN_GAP / burst rules still apply

This file is intentionally the only place that knows there's *more than
one* PersonaAgent in the room. Each PersonaAgent stays oblivious — it
just speaks when ``deliver_commentary`` / ``deliver_user_reply`` is
called on it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from livekit import rtc

from podcast_commentary.agent.comedian import FoxPhase, PersonaAgent
from podcast_commentary.agent.commentary import (
    MIN_GAP,
    SENTENCE_THRESHOLD,
    CommentaryTimer,
    FullTranscript,
)
from podcast_commentary.agent.fox_config import CONFIG
from podcast_commentary.agent.podcast_pipeline import PodcastPipeline
from podcast_commentary.agent.selector import SKIP, SpeakerSelector
from podcast_commentary.agent.user_turn import UserTurnTracker
from podcast_commentary.core.config import settings
from podcast_commentary.core.db import log_conversation_message

logger = logging.getLogger("podcast-commentary.director")


# Pacing knobs sourced from the *primary* persona's timing — one room-wide
# cadence wins over per-persona disagreements, and the primary's values
# are by convention the conservative ones (Fox > Alien on these).
POST_SPEECH_DELAY = CONFIG.timing.post_speech_safety_s
SILENCE_FALLBACK_DELAY = CONFIG.timing.silence_fallback_s
INTRO_PLAYOUT_TIMEOUT = CONFIG.playout.intro_timeout_s
COMMENTARY_PLAYOUT_TIMEOUT = CONFIG.playout.commentary_timeout_s


# Chattiness presets from the UI. Each entry scales MIN_GAP (the cool-down
# between turns) and the silence-fallback delay (how long a quiet stretch
# goes before anyone steps in). "normal" leaves the config-derived defaults
# untouched; the multipliers on either side are deliberately wide so users
# can feel the dial move.
_FREQUENCY_PRESETS: dict[str, tuple[float, float]] = {
    "quiet": (0.45, 0.5),
    "normal": (0.3375, 0.375),
    "chatty": (0.225, 0.25),
}


def _log_task_exception(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Fire-and-forget task %r failed: %s", task.get_name(), exc, exc_info=exc)


# Avatar participants publish under this identity prefix (see main.py
# `_avatar_identity_for`). Everything else disconnecting is the user.
_AVATAR_IDENTITY_PREFIX = "lemonslice-avatar-"


class Director:
    """One Director per room. Lives for the whole job.

    The Director composes itself with the list of ``PersonaAgent``s that
    are about to be ``session.start()``ed. Construction is cheap; real
    work begins on ``start()`` (after the personas have entered their
    sessions and signalled ``ready``).
    """

    def __init__(
        self,
        *,
        personas: list[PersonaAgent],
        room: rtc.Room,
        primary_session: Any,  # AgentSession; the one that owns user-mic STT
        avatar_identities: dict[str, str] | None = None,
        session_id: str | None = None,
        on_user_disconnect: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if not personas:
            raise ValueError("Director needs at least one PersonaAgent")
        self._personas = personas
        self._by_name = {p.name: p for p in personas}
        self._room = room
        self._primary_session = primary_session
        self._session_id = session_id
        # Per-persona LemonSlice avatar participant identities. Used by
        # `_deliver_intros` to gate each intro on the matching avatar having
        # its video track published — otherwise `DataStreamIO.capture_frame`
        # blocks waiting for the track and the 8s playout timeout swallows
        # the intro before audio lands.
        self._avatar_identities: dict[str, str] = dict(avatar_identities or {})
        # Called once, after shutdown, when the *user* (not an avatar) leaves
        # the room. Lets main.py terminate the job so a new call dispatches
        # into a fresh worker instead of inheriting zombie state.
        self._on_user_disconnect = on_user_disconnect

        # Shared state — everyone reacts to the same transcript and one timer.
        self._timer = CommentaryTimer()
        self._full_transcript = FullTranscript()
        self._podcast = PodcastPipeline(on_transcript=self._handle_podcast_transcript)

        # User push-to-talk owned by the primary AgentSession's STT.
        self._user_turn = UserTurnTracker(
            session=primary_session,
            on_committed=self._handle_user_committed,
            on_start=self._on_user_talk_start,
            on_empty=self._on_user_turn_empty,
        )

        # Speaker selection (owns its own LLM and fallback logic).
        self._selector = SpeakerSelector(
            model=settings.DIRECTOR_LLM_MODEL,
            max_consecutive=settings.DIRECTOR_MAX_CONSECUTIVE,
        )

        # Per-room scheduling state.
        self._last_speaker: str | None = None
        self._consecutive_count: int = 0
        self._silence_task: asyncio.Task | None = None
        self._selection_lock = asyncio.Lock()
        self._user_talking: bool = False
        self._user_target_name: str | None = None  # who replies to the user
        self._shutting_down: bool = False
        # Set by `shutdown()` — lets long awaits (avatar-readiness gates on
        # the intro path) short-circuit instead of blocking the full
        # per-persona timeout when the job is already torn down.
        self._shutdown_event: asyncio.Event = asyncio.Event()
        # Every task started via `self._fire_and_forget` is tracked here so
        # `shutdown()` can cancel the lot. The silence-fallback loop used to
        # be the only tracked task; leaving the rest untracked meant a
        # pending `publish_data` or `persist` could fire seconds after the
        # room had already been torn down.
        self._bg_tasks: set[asyncio.Task] = set()
        self._shutdown_task: asyncio.Task | None = None

        # Runtime-tunable pacing. Instance copies of the module defaults so
        # `update_settings` can scale them without mutating global state.
        # The timer owns its own `min_gap` field; we keep the silence-fallback
        # delay here because only the Director consults it.
        self._silence_fallback_delay: float = SILENCE_FALLBACK_DELAY

        # Wire each persona's events back to us.
        for p in personas:
            p._on_speech_start_cb = self._on_persona_speech_start  # type: ignore[attr-defined]
            p._on_speech_end_cb = self._on_persona_speech_end  # type: ignore[attr-defined]
            p._on_turn_finalised_cb = self._on_persona_turn_finalised  # type: ignore[attr-defined]

    # ==================================================================
    # Lifecycle
    # ==================================================================
    async def start(self) -> None:
        """Begin the show: deliver intros, attach STT, kick the silence loop.

        Caller must wait for every persona's ``ready`` Event before calling
        this — otherwise SpeechGate is None and ``deliver_intro`` would
        crash.
        """
        # Podcast pipeline must be started BEFORE we wire the room listener
        # / replay tracks. If the extension's podcast-audio track is already
        # subscribed by the time we get here, `attach_track` needs the
        # frame buffer to already exist — otherwise the track is dropped on
        # the floor and the agent never hears the video.
        self._podcast.start()
        self._wire_room_listeners()
        self._replay_existing_tracks()
        self._attach_playback_listeners()

        await self._deliver_intros()
        await self._publish_agent_ready()
        # Once intros land, the silence-fallback loop carries us through
        # quiet stretches.
        self._schedule_silence_fallback()

    async def shutdown(self) -> None:
        """Tear down all Director-owned work. Idempotent.

        Ordering matters: flip ``_shutting_down`` first so any in-flight
        commentary path sees the flag and early-exits; interrupt live
        speech handles so stale avatar playouts don't linger; then cancel
        every tracked background task and await their settle; finally stop
        the podcast STT pipeline. Called both by the job-level shutdown
        callback and by the user-disconnect handler below (which also
        fires ``_on_user_disconnect`` to terminate the job).
        """
        if self._shutting_down:
            return
        self._shutting_down = True
        self._shutdown_event.set()
        logger.info("Director shutting down")

        # Stop scheduling new work.
        if self._silence_task is not None and not self._silence_task.done():
            self._silence_task.cancel()

        # Interrupt anyone mid-utterance so the framework's
        # `clear_buffer` RPC fires while the room transport is still up.
        for persona in self._personas:
            with contextlib.suppress(Exception):
                persona.interrupt()

        # Cancel tracked fire-and-forget tasks and wait for them to settle.
        pending = [t for t in self._bg_tasks if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        with contextlib.suppress(Exception):
            await self._podcast.shutdown()

    # ------------------------------------------------------------------
    # Task tracking
    # ------------------------------------------------------------------
    def _fire_and_forget(self, coro: Any, *, name: str = "") -> asyncio.Task:
        """Start a background task and track it so ``shutdown()`` can cancel it.

        Tasks started after shutdown has begun are cancelled synchronously
        — the coroutine is still created and closed so callers don't leak
        a "never awaited" warning, but the logic is prevented from running.
        """
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(_log_task_exception)
        task.add_done_callback(self._bg_tasks.discard)
        self._bg_tasks.add(task)
        if self._shutting_down:
            task.cancel()
        return task

    # ==================================================================
    # Intros — sequenced, never simultaneous
    # ==================================================================
    async def _deliver_intros(self) -> None:
        """Each persona introduces itself in declared order, one at a time.

        Intros are sequential on purpose — two avatars talking at once
        sounds broken. Before a persona speaks we wait for *its own* avatar
        to have both joined the room AND published its video track; only
        then will ``DataStreamIO.capture_frame`` be able to push audio
        without stalling on ``wait_for_track_publication``. Each intro is
        then capped by ``INTRO_PLAYOUT_TIMEOUT`` — a separate, inner safety
        net once audio has actually started flowing.

        Edge cases handled by serial-await:
          * Alien's avatar connects mid-Fox-intro → Alien waits because
            Fox's intro is still in-flight; Alien's wait resolves instantly
            afterward.
          * Fox's intro finishes before Alien's avatar has connected →
            we block on Alien's readiness wait, then deliver.
          * An avatar never connects at all → the per-persona timeout fires
            and we skip that intro (log + move on) instead of hanging the
            whole show.
        """
        for persona in self._personas:
            if self._shutting_down:
                return
            identity = self._avatar_identities.get(persona.name)
            if identity is not None:
                timeout = persona.config.avatar.startup_timeout_s
                if not await self._wait_for_avatar_ready(identity, timeout=timeout):
                    if self._shutting_down:
                        return
                    logger.warning(
                        "Skipping %s intro — avatar %s not ready within %.0fs",
                        persona.name,
                        identity,
                        timeout,
                    )
                    continue
            await self._speak_intro_with_timeout(persona)

    async def _wait_for_avatar_ready(self, identity: str, *, timeout: float) -> bool:
        """Wait until an avatar participant has joined AND published video.

        Publication (not subscription) is what ``DataStreamIO._start_task``
        awaits internally — matching that signal here means the avatar's
        audio path will flow the moment we kick off speech. Returns True on
        ready, False on timeout or shutdown.
        """
        if self._shutting_down:
            return False

        def has_video(p: Any) -> bool:
            for publication in p.track_publications.values():
                if getattr(publication, "kind", None) == rtc.TrackKind.KIND_VIDEO:
                    return True
            return False

        ready = asyncio.Event()

        def on_participant_connected(p: Any) -> None:
            if p.identity == identity and has_video(p):
                ready.set()

        def on_track_published(publication: Any, p: Any) -> None:
            if (
                p.identity == identity
                and getattr(publication, "kind", None) == rtc.TrackKind.KIND_VIDEO
            ):
                ready.set()

        self._room.on("participant_connected", on_participant_connected)
        self._room.on("track_published", on_track_published)
        try:
            # Fast path — already joined and published before we attached.
            for p in self._room.remote_participants.values():
                if p.identity == identity and has_video(p):
                    return True

            ready_task = asyncio.create_task(ready.wait())
            shutdown_task = asyncio.create_task(self._shutdown_event.wait())
            try:
                done, _ = await asyncio.wait(
                    {ready_task, shutdown_task},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for t in (ready_task, shutdown_task):
                    if not t.done():
                        t.cancel()
            if self._shutdown_event.is_set():
                return False
            return ready_task in done and not ready_task.cancelled()
        finally:
            self._room.off("participant_connected", on_participant_connected)
            self._room.off("track_published", on_track_published)

    async def _speak_intro_with_timeout(self, persona: PersonaAgent) -> None:
        """Deliver one persona's intro with a hard upper bound.

        Delegates to ``_wait_for_playout_robust`` so a missing vendor
        ``lk.playback_finished`` RPC can't hang the room — see that
        method's docstring for the full recovery strategy.
        """
        await self._publish_commentary_start(persona.name)
        try:
            handle = persona.speak_intro()
            if handle is None:
                return  # session closed before we could speak
            await self._wait_for_playout_robust(
                persona, handle, timeout=INTRO_PLAYOUT_TIMEOUT, label="intro"
            )
        finally:
            await self._publish_commentary_end(persona.name)

    # ==================================================================
    # Robust playout wait — shared by intro / commentary / user-reply
    # ==================================================================
    # Grace window after we synthesize a ``playback_finished`` event, giving
    # the framework a chance to resolve the handle and fire our done-callback
    # before we fall back to ``force_listening``. 2s is enough in practice —
    # the resolve path is purely in-process once the event fires.
    _SYNTHESIS_GRACE_S: float = 2.0

    async def _wait_for_playout_robust(
        self,
        persona: PersonaAgent,
        handle: Any,
        *,
        timeout: float,
        label: str,
    ) -> None:
        """Wait for a ``SpeechHandle``'s playout with avatar-failure recovery.

        Production reality (livekit/agents #3510, #4315): LemonSlice's
        *second* ``AvatarSession`` in a room occasionally does not send
        ``lk.playback_finished`` back over RPC. Without that RPC the
        framework's ``SpeechHandle.wait_for_playout`` blocks forever —
        ``DataStreamAudioOutput.on_playback_finished`` is what sets the
        internal event that wakes the waiter.

        Recovery ladder:
          1. ``await wait_for_playout`` normally for ``timeout`` seconds.
             If the vendor confirms (or the handle resolves any other
             terminal way), we're done.
          2. On timeout, call ``persona.synthesize_playout_complete()`` —
             this calls ``AudioOutput.on_playback_finished`` ourselves
             with the already-pushed audio duration as the position. It
             does NOT interrupt the handle, so any audio already en route
             to the avatar continues playing out. Wait up to
             ``_SYNTHESIS_GRACE_S`` for the handle to resolve via the
             synthesized event.
          3. Only if the handle is *still* not done after synthesis do we
             fall back to ``force_listening`` — the nuclear option that
             cuts audio off. This catches pathological cases where the
             session is truly wedged (not just a missing RPC).

        Timer bookkeeping (``record_speech_end``) fires once, in the
        timeout path, so the shared ``CommentaryTimer`` doesn't get
        double-recorded.
        """
        try:
            await asyncio.wait_for(handle.wait_for_playout(), timeout=timeout)
            return
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            pass
        except Exception:
            # Any other failure: log and fall through to recovery — we'd
            # rather synthesize than leave the room in limbo.
            logger.debug("%s %s wait_for_playout raised", persona.name, label, exc_info=True)

        # --- Recovery path ---
        outer_pushed, inner_pushed = persona.synthesize_playout_complete()
        if inner_pushed > 0.01:
            logger.warning(
                "%s %s playout unconfirmed after %.0fs — synthesized "
                "playback_finished (sync=%.2fs, wire=%.2fs); audio flowed but "
                "vendor RPC missing — Alien should still be audible",
                persona.name,
                label,
                timeout,
                outer_pushed,
                inner_pushed,
            )
        else:
            logger.error(
                "%s %s playout unconfirmed after %.0fs and NO AUDIO reached "
                "the wire (sync=%.2fs, wire=%.2fs) — persona will be silent. "
                "Likely upstream block (TTS, TranscriptSynchronizer barrier, "
                "or data-stream writer never opened)",
                persona.name,
                label,
                timeout,
                outer_pushed,
                inner_pushed,
            )

        # Brief grace for the handle to resolve via the event we just fired.
        try:
            await asyncio.wait_for(handle.wait_for_playout(), timeout=self._SYNTHESIS_GRACE_S)
            return
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            pass
        except Exception:
            logger.debug(
                "%s %s wait_for_playout (post-synthesis) raised",
                persona.name,
                label,
                exc_info=True,
            )

        # Last resort: force the phase and interrupt. This cuts any
        # still-playing audio off, so we only reach here if synthesis
        # didn't wake the waiter — meaning something deeper is wedged.
        logger.error(
            "%s %s handle still not done after synthesis — force_listening",
            persona.name,
            label,
        )
        with contextlib.suppress(Exception):
            persona.force_listening()

    def _attach_playback_listeners(self) -> None:
        """Subscribe to ``playback_finished`` on each persona's audio output.

        Purely observational — it lets operators see in logs whether a
        confirmation came from the LemonSlice RPC (fires without our
        warning log) or from our own synthesis (fires *right after* a
        "synthesized playback_finished" warning). Helps triage vendor
        regressions without having to repro.
        """
        for persona in self._personas:
            audio = persona._audio_output()  # type: ignore[attr-defined]
            if audio is None:
                continue
            name = persona.name

            def _on_playback_finished(ev: Any, name: str = name) -> None:
                logger.info(
                    "[%s] playback_finished event (position=%.2fs, interrupted=%s)",
                    name,
                    float(getattr(ev, "playback_position", 0.0) or 0.0),
                    getattr(ev, "interrupted", False),
                )

            try:
                audio.on("playback_finished", _on_playback_finished)
            except Exception:
                logger.debug(
                    "[%s] failed to attach playback_finished listener",
                    name,
                    exc_info=True,
                )

    # ==================================================================
    # Podcast transcript → commentary
    # ==================================================================
    async def _handle_podcast_transcript(self, text: str) -> None:
        """Called by PodcastPipeline for every podcast STT result."""
        self._persist("podcast", text, None)
        sentence_count = self._full_transcript.add(text)

        if (
            sentence_count >= SENTENCE_THRESHOLD
            and self._room_is_listening()
            and self._timer.can_comment()
        ):
            self._fire_and_forget(
                self._maybe_deliver_commentary(
                    trigger_reason="react to the latest transcript",
                    energy_level="amused",
                ),
                name="sentence_trigger_commentary",
            )

    def _schedule_silence_fallback(self) -> None:
        """Reset the silence-fallback timer.

        Called whenever we transition back to "room is listening". If the
        podcast goes quiet for ``SILENCE_FALLBACK_DELAY`` seconds, the
        Director picks a speaker and steps in with a reflective beat.
        """
        if self._shutting_down:
            return
        if self._silence_task is not None and not self._silence_task.done():
            self._silence_task.cancel()
        self._silence_task = self._fire_and_forget(
            self._silence_fallback_loop(), name="director_silence"
        )

    async def _silence_fallback_loop(self) -> None:
        await asyncio.sleep(self._silence_fallback_delay)
        if self._shutting_down:
            return
        # Self-heal on any transient "can't speak right now" state — a hung
        # avatar can leave a persona stuck in INTRO/COMMENTATING for tens of
        # seconds, and if we let the loop die here `_on_persona_speech_end`
        # may never fire to re-arm us, killing commentary permanently.
        if not self._room_is_listening() or not self._full_transcript.has_content():
            self._schedule_silence_fallback()
            return
        await self._maybe_deliver_commentary(
            trigger_reason="the video has gone quiet — react to what was said",
            energy_level="amused",
        )

    async def _maybe_deliver_commentary(self, *, trigger_reason: str, energy_level: str) -> None:
        """Pick a speaker (or skip) and deliver one commentary turn.

        Single-flight via ``_selection_lock`` so two transcript chunks
        landing back-to-back can't both win the race and produce a
        double-tap.
        """
        if self._shutting_down:
            return
        async with self._selection_lock:
            if (
                self._shutting_down
                or not self._room_is_listening()
                or not self._timer.can_comment()
            ):
                return

            speaker_name = await self._selector.pick(
                personas=self._personas,
                transcript=self._full_transcript.recent_transcript(),
                trigger_reason=trigger_reason,
                last_speaker=self._last_speaker,
                consecutive_count=self._consecutive_count,
            )
            if self._shutting_down:
                return
            if speaker_name == SKIP:
                logger.info("Director skip — no speaker this turn")
                # Reset the silence loop so we evaluate again later.
                self._schedule_silence_fallback()
                return

            persona = self._by_name.get(speaker_name)
            if persona is None:
                logger.warning(
                    "Selector returned unknown speaker %r — defaulting to first persona",
                    speaker_name,
                )
                persona = self._personas[0]

            await self._deliver_commentary_for(
                persona, trigger_reason=trigger_reason, energy_level=energy_level
            )

    async def _deliver_commentary_for(
        self, persona: PersonaAgent, *, trigger_reason: str, energy_level: str
    ) -> None:
        """Run one commentary turn for ``persona``.

        Owns the commentary_start/end signalling, playout timeout safety
        net, and the ``last_speaker`` bookkeeping. The persona only knows
        how to compose its prompt and call ``SpeechGate.speak``.
        """
        if self._shutting_down:
            return
        await self._publish_commentary_start(persona.name)
        co_history, co_label = self._co_speaker_view(persona)

        handle = await persona.deliver_commentary(
            recent_transcript=self._full_transcript.recent_transcript(),
            trigger_reason=trigger_reason,
            energy_level=energy_level,
            co_speaker_history=co_history,
            co_speaker_label=co_label,
        )
        # ``deliver_commentary`` returns None when the session was closed
        # mid-flight (user disconnected while the selector was deliberating).
        # Nothing to wait on — publish the matching commentary_end and bail.
        if handle is None:
            with contextlib.suppress(Exception):
                await self._publish_commentary_end(persona.name)
            return

        # Reset the read cursor AFTER prompt is built (deliver_commentary
        # already read it) so the next persona reacts to *new* podcast text.
        self._full_transcript.reset_sentence_count()
        self._note_speaker(persona.name)

        try:
            await self._wait_for_playout_robust(
                persona, handle, timeout=COMMENTARY_PLAYOUT_TIMEOUT, label="commentary"
            )
        finally:
            self._timer.record_speech_end()

    # ==================================================================
    # User push-to-talk
    # ==================================================================
    def _on_user_talk_start(self) -> None:
        """User pressed PTT — interrupt anyone speaking; remember target."""
        self._user_talking = True
        speaking = [p for p in self._personas if p.is_speaking]
        # Reply target: whoever ISN'T currently mid-turn (so the human
        # gets a fresh voice). If nobody's speaking, default to primary.
        non_speakers = [p for p in self._personas if not p.is_speaking]
        target = non_speakers[0] if non_speakers else self._personas[0]
        self._user_target_name = target.name

        for p in speaking:
            p.interrupt()
        # Move whoever was speaking — and the reply target — into USER_TALKING
        # so phase invariants stay clean while the user holds the button.
        for p in self._personas:
            try:
                p.mark_user_talking()
            except Exception:
                logger.debug("mark_user_talking raised on %s", p.name, exc_info=True)

    def _on_user_turn_empty(self) -> None:
        """STT found nothing — return everyone to LISTENING, resume cadence."""
        self._user_talking = False
        self._user_target_name = None
        # Phase reset — each persona's gate may already have done this if
        # it was speaking, but the non-speakers need a nudge.
        for p in self._personas:
            if p.phase == FoxPhase.USER_TALKING:
                p._set_phase(FoxPhase.LISTENING)  # type: ignore[attr-defined]
        self._schedule_silence_fallback()

    async def _handle_user_committed(self, user_text: str) -> None:
        """User finished — persist + dispatch reply to the chosen persona."""
        if self._shutting_down:
            return
        self._persist("user", user_text, None)
        target_name = self._user_target_name or self._personas[0].name
        persona = self._by_name.get(target_name, self._personas[0])
        self._user_talking = False
        self._user_target_name = None

        # Move every non-target persona out of USER_TALKING so the room
        # phase invariants stay clean — only the target should transition
        # to REPLYING.
        for p in self._personas:
            if p is not persona and p.phase == FoxPhase.USER_TALKING:
                p._set_phase(FoxPhase.LISTENING)  # type: ignore[attr-defined]

        await self._publish_commentary_start(persona.name)
        co_history, co_label = self._co_speaker_view(persona)
        try:
            handle = await persona.deliver_user_reply(
                user_text,
                recent_transcript=self._full_transcript.recent_transcript(),
                co_speaker_history=co_history,
                co_speaker_label=co_label,
            )
            if handle is None:
                with contextlib.suppress(Exception):
                    await self._publish_commentary_end(persona.name)
                return
            self._note_speaker(persona.name)
            await self._wait_for_playout_robust(
                persona, handle, timeout=COMMENTARY_PLAYOUT_TIMEOUT, label="user_reply"
            )
        finally:
            self._timer.record_speech_end()

    # ==================================================================
    # Persona event callbacks (set on each PersonaAgent at construction)
    # ==================================================================
    def _on_persona_speech_start(self, persona: PersonaAgent) -> None:
        """Real audio just started reaching the avatar."""
        self._timer.record_speech_start()

    def _on_persona_speech_end(self, persona: PersonaAgent) -> None:
        """Real audio finished — un-duck client + re-arm silence loop."""
        if self._shutting_down:
            return
        self._fire_and_forget(
            self._publish_commentary_end(persona.name),
            name=f"commentary_end.{persona.name}",
        )
        if not self._user_talking and self._room_is_listening():
            self._schedule_silence_fallback()

    async def _on_persona_turn_finalised(
        self, persona: PersonaAgent, text: str, angle: str | None
    ) -> None:
        """Persona's assistant message landed — log it for the room.

        Every persona already persists its own commentary; here we just
        record it for the speaker-selection log.
        """
        logger.info(
            "Director recorded %s turn (angle=%s, lines_history=%d)",
            persona.name,
            angle,
            len(persona.commentary_history),
        )

    # ==================================================================
    # Room listeners
    # ==================================================================
    def _wire_room_listeners(self) -> None:
        self._room.on("data_received", self._on_data_received)
        self._room.on("track_subscribed", self._on_track_subscribed)
        self._room.on("participant_disconnected", self._on_participant_disconnected)

    def _on_participant_disconnected(self, participant: Any) -> None:
        """End the job when the user leaves.

        The framework auto-closes the ``AgentSession`` on participant
        disconnect (``RoomInputOptions.close_on_disconnect`` is True by
        default) but the *job* keeps running — which means the Director's
        silence loop and any pending fire-and-forget tasks would keep
        firing into a dead session and raise ``AgentSession isn't running``.
        Eagerly shutting the Director down (and asking the job to
        terminate) keeps the worker clean for the next call.

        Avatar participants disconnecting is normal mid-call churn — only
        a user disconnect should tear the room down.
        """
        identity = getattr(participant, "identity", "") or ""
        if identity.startswith(_AVATAR_IDENTITY_PREFIX):
            return
        if self._shutting_down or self._shutdown_task is not None:
            return
        logger.info("User participant %s disconnected — tearing down", identity)
        self._shutdown_task = asyncio.create_task(
            self._shutdown_on_user_disconnect(), name="director_user_disconnect"
        )
        self._shutdown_task.add_done_callback(_log_task_exception)

    async def _shutdown_on_user_disconnect(self) -> None:
        """Shutdown ourselves, then ask main.py to terminate the job."""
        try:
            await self.shutdown()
        finally:
            if self._on_user_disconnect is not None:
                with contextlib.suppress(Exception):
                    await self._on_user_disconnect()

    def _replay_existing_tracks(self) -> None:
        """Replay track_subscribed for tracks present before we subscribed.

        The Chrome extension publishes ``podcast-audio`` as soon as it
        connects — typically before the agent dispatches into the room.
        Without this replay the live event has already fired and our
        handler never runs.
        """
        for participant in list(self._room.remote_participants.values()):
            for publication in list(participant.track_publications.values()):
                track = getattr(publication, "track", None)
                if track is None:
                    continue
                try:
                    self._on_track_subscribed(track, publication, participant)
                except Exception:
                    logger.exception("Replay of track_subscribed failed")

    def _on_track_subscribed(self, track: Any, publication: Any, participant: Any) -> None:
        track_name = getattr(publication, "name", "")
        identity = getattr(participant, "identity", "")
        logger.info(
            "Track subscribed [name=%s from=%s kind=%s]",
            track_name,
            identity,
            getattr(track, "kind", "?"),
        )
        if track_name == "podcast-audio":
            self._podcast.attach_track(track)

    def _on_data_received(self, data_packet: Any) -> None:
        msg = self._parse_data_packet(data_packet)
        if msg is None:
            return
        handler = self._DATA_HANDLERS.get(msg.get("type"))
        if handler is not None:
            handler(self, msg)

    def _handle_user_talk_start(self, _msg: dict) -> None:
        self._user_turn.start()

    def _handle_user_talk_end(self, _msg: dict) -> None:
        self._user_turn.end()

    def _handle_skip(self, _msg: dict) -> None:
        """User hit "Skip commentary" — cut off anyone mid-utterance.

        `interrupt()` is a no-op on idle personas, so it's safe to fire
        at everyone.
        """
        for p in self._personas:
            p.interrupt()

    def _handle_settings(self, msg: dict) -> None:
        self.update_settings(
            frequency=msg.get("frequency"),
            length=msg.get("length"),
        )

    # Dispatch table for `data_received` messages on `podcast.control`.
    # Declared at class level so new message types are a one-line add.
    _DATA_HANDLERS = {
        "user_talk_start": _handle_user_talk_start,
        "user_talk_end": _handle_user_talk_end,
        "skip": _handle_skip,
        "settings": _handle_settings,
    }

    def update_settings(self, *, frequency: str | None = None, length: str | None = None) -> None:
        """Apply a new frequency/length preference from the UI.

        Frequency scales both the per-turn cool-down (``MIN_GAP``) and the
        silence-fallback delay so Quiet/Chatty noticeably change *when* a
        persona steps in. Length is stashed on each PersonaAgent and read
        by the prompt builder on the next turn — no restart needed.
        """
        if frequency in _FREQUENCY_PRESETS:
            gap_mult, silence_mult = _FREQUENCY_PRESETS[frequency]
            self._timer.min_gap = MIN_GAP * gap_mult
            self._silence_fallback_delay = SILENCE_FALLBACK_DELAY * silence_mult
            logger.info(
                "Frequency → %s (min_gap=%.1fs, silence_fallback=%.1fs)",
                frequency,
                self._timer.min_gap,
                self._silence_fallback_delay,
            )
        elif frequency is not None:
            logger.warning("Ignoring unknown frequency setting: %r", frequency)

        if length in ("short", "normal", "long"):
            hint = length if length != "normal" else None
            for p in self._personas:
                p.set_length_hint(hint)
            logger.info("Length → %s", length)
        elif length is not None:
            logger.warning("Ignoring unknown length setting: %r", length)

    @staticmethod
    def _parse_data_packet(data_packet: Any) -> dict | None:
        raw = getattr(data_packet, "data", b"")
        try:
            return json.loads(raw.decode())
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            return None

    # ==================================================================
    # Helpers
    # ==================================================================
    def _room_is_listening(self) -> bool:
        """True iff every persona is in LISTENING (no one mid-turn)."""
        return all(p.phase == FoxPhase.LISTENING for p in self._personas)

    def _co_speaker_view(self, persona: PersonaAgent) -> tuple[list[str] | None, str | None]:
        """Return the most-relevant co-persona's recent lines + label.

        With two personas this is the other one; with one persona it's
        ``(None, None)`` and the prompt builder omits that block.
        """
        others = [p for p in self._personas if p.name != persona.name]
        if not others:
            return None, None
        co = others[0]
        return co.commentary_history, co.label

    def _note_speaker(self, name: str) -> None:
        """Bookkeep consecutive-turn streaks for the cap."""
        if name == self._last_speaker:
            self._consecutive_count += 1
        else:
            self._consecutive_count = 1
        self._last_speaker = name

    # ==================================================================
    # Client signalling — commentary.control
    # ==================================================================
    async def _publish_commentary_start(self, speaker: str) -> None:
        await self._publish_control({"type": "commentary_start", "speaker": speaker})

    async def _publish_commentary_end(self, speaker: str) -> None:
        await self._publish_control({"type": "commentary_end", "speaker": speaker})

    async def _publish_agent_ready(self) -> None:
        speakers = [{"name": p.name, "label": p.label} for p in self._personas]
        await self._publish_control({"type": "agent_ready", "speakers": speakers})

    async def _publish_control(self, payload: dict) -> None:
        try:
            await self._room.local_participant.publish_data(
                json.dumps(payload),
                topic="commentary.control",
                reliable=True,
            )
        except Exception:
            logger.warning("Failed to publish %s", payload.get("type"), exc_info=True)

    # ==================================================================
    # Persistence
    # ==================================================================
    def _persist(self, role: str, content: str, metadata: dict | None) -> None:
        if not self._session_id or not content:
            return
        if self._shutting_down:
            return
        self._fire_and_forget(
            log_conversation_message(self._session_id, role, content, metadata),
            name=f"director.persist.{role}",
        )


# ---------------------------------------------------------------------------
# Helper for main.py to set callbacks before session.start (the Director's
# constructor already does this, but main.py wires PersonaAgent → Director
# in a specific order to keep the wiring obvious).
# ---------------------------------------------------------------------------


def attach_persona_callbacks(director: Director, personas: list[PersonaAgent]) -> None:
    """Idempotent — re-binds callbacks if a persona is replaced post-construction."""
    for p in personas:
        p._on_speech_start_cb = director._on_persona_speech_start  # type: ignore[attr-defined]
        p._on_speech_end_cb = director._on_persona_speech_end  # type: ignore[attr-defined]
        p._on_turn_finalised_cb = director._on_persona_turn_finalised  # type: ignore[attr-defined]


__all__ = [
    "Director",
    "attach_persona_callbacks",
]
