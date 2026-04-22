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
import json
import logging
from typing import Any

from livekit import rtc
from livekit.agents import llm
from livekit.plugins import groq

from podcast_commentary.agent.comedian import FoxPhase, PersonaAgent
from podcast_commentary.agent.commentary import (
    SENTENCE_THRESHOLD,
    CommentaryTimer,
    FullTranscript,
)
from podcast_commentary.agent.fox_config import CONFIG
from podcast_commentary.agent.podcast_pipeline import PodcastPipeline
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


def _fire_and_forget(coro: Any, *, name: str = "") -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(_log_task_exception)
    return task


def _log_task_exception(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Fire-and-forget task %r failed: %s", task.get_name(), exc, exc_info=exc)


# Sentinel value the speaker-selection LLM returns when it judges nobody
# should speak yet (e.g. mid-sentence pause that doesn't warrant a beat).
_SKIP = "skip"


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
        session_id: str | None = None,
    ) -> None:
        if not personas:
            raise ValueError("Director needs at least one PersonaAgent")
        self._personas = personas
        self._by_name = {p.name: p for p in personas}
        self._room = room
        self._primary_session = primary_session
        self._session_id = session_id

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

        # Selector LLM (separate instance from the comedians' LLMs so its
        # config can drift independently — temperature, max tokens, model).
        self._selector_llm = groq.LLM(
            model=settings.DIRECTOR_LLM_MODEL,
            max_completion_tokens=80,
        )

        # Per-room scheduling state.
        self._last_speaker: str | None = None
        self._consecutive_count: int = 0
        self._silence_task: asyncio.Task | None = None
        self._selection_lock = asyncio.Lock()
        self._user_talking: bool = False
        self._user_target_name: str | None = None  # who replies to the user
        self._shutting_down: bool = False

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

        await self._deliver_intros()
        await self._publish_agent_ready()
        # Once intros land, the silence-fallback loop carries us through
        # quiet stretches.
        self._schedule_silence_fallback()

    async def shutdown(self) -> None:
        """Tear down the podcast pipeline and cancel timers."""
        self._shutting_down = True
        if self._silence_task is not None:
            self._silence_task.cancel()
        await self._podcast.shutdown()

    # ==================================================================
    # Intros — sequenced, never simultaneous
    # ==================================================================
    async def _deliver_intros(self) -> None:
        """Each persona introduces itself in declared order, one at a time.

        Synchronous gate close + ``wait_for_playout`` with a timeout so a
        hung avatar never blocks the second intro. We publish a
        ``commentary_start`` / ``commentary_end`` pair around each so the
        extension can highlight + un-highlight the right slot.
        """
        for persona in self._personas:
            await self._publish_commentary_start(persona.name)
            handle = persona.speak_intro()
            try:
                await asyncio.wait_for(handle.wait_for_playout(), timeout=INTRO_PLAYOUT_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(
                    "%s intro playout timed out after %.0fs — moving on",
                    persona.name,
                    INTRO_PLAYOUT_TIMEOUT,
                )
            except Exception:
                logger.debug("Intro wait_for_playout raised", exc_info=True)
            await self._publish_commentary_end(persona.name)

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
            _fire_and_forget(
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
        if self._silence_task is not None:
            self._silence_task.cancel()
        self._silence_task = _fire_and_forget(
            self._silence_fallback_loop(), name="director_silence"
        )

    async def _silence_fallback_loop(self) -> None:
        await asyncio.sleep(SILENCE_FALLBACK_DELAY)
        if self._shutting_down or not self._room_is_listening():
            return
        if not self._full_transcript.has_content():
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
            if not self._room_is_listening() or not self._timer.can_comment():
                return

            speaker_name = await self._pick_speaker(trigger_reason)
            if speaker_name == _SKIP:
                logger.info("Director skip — no speaker this turn")
                # Reset the silence loop so we evaluate again later.
                self._schedule_silence_fallback()
                return

            persona = self._by_name.get(speaker_name)
            if persona is None:
                logger.warning(
                    "Director picked unknown speaker %r — falling back to round-robin",
                    speaker_name,
                )
                persona = self._round_robin_pick()

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
        await self._publish_commentary_start(persona.name)
        co_history, co_label = self._co_speaker_view(persona)

        handle = await persona.deliver_commentary(
            recent_transcript=self._full_transcript.recent_transcript(),
            trigger_reason=trigger_reason,
            energy_level=energy_level,
            co_speaker_history=co_history,
            co_speaker_label=co_label,
        )
        # Reset the read cursor AFTER prompt is built (deliver_commentary
        # already read it) so the next persona reacts to *new* podcast text.
        self._full_transcript.reset_sentence_count()
        self._note_speaker(persona.name)

        try:
            await asyncio.wait_for(handle.wait_for_playout(), timeout=COMMENTARY_PLAYOUT_TIMEOUT)
            self._timer.record_speech_end()
        except asyncio.TimeoutError:
            logger.warning(
                "%s commentary playout timed out after %.0fs",
                persona.name,
                COMMENTARY_PLAYOUT_TIMEOUT,
            )
            self._timer.record_speech_end()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("wait_for_playout raised — continuing", exc_info=True)

    # ==================================================================
    # Speaker selection — small fast LLM
    # ==================================================================
    async def _pick_speaker(self, trigger_reason: str) -> str:
        """Return persona name to speak, or ``"skip"``.

        Strategy:
          1. Filter out personas that are over the consecutive-turn cap.
          2. If only one remains, pick it (LLM is overkill).
          3. Otherwise ask the selector LLM. Fall back to round-robin on
             any failure — better a slightly wrong pick than dead air.
        """
        eligible = [p for p in self._personas if self._eligible(p)]
        if not eligible:
            # Everyone's been forced off — fallback: rotate to whoever
            # spoke least recently.
            eligible = [p for p in self._personas if p.name != self._last_speaker] or self._personas

        if len(eligible) == 1:
            return eligible[0].name

        try:
            return await asyncio.wait_for(
                self._llm_pick_speaker(eligible, trigger_reason), timeout=2.5
            )
        except asyncio.TimeoutError:
            logger.warning("Director LLM pick timed out — falling back to round-robin")
        except Exception:
            logger.warning("Director LLM pick raised — falling back to round-robin", exc_info=True)
        return self._round_robin_pick(eligible).name

    def _eligible(self, persona: PersonaAgent) -> bool:
        """A persona is eligible unless it just hit the consecutive cap."""
        if persona.name != self._last_speaker:
            return True
        return self._consecutive_count < settings.DIRECTOR_MAX_CONSECUTIVE

    def _round_robin_pick(self, pool: list[PersonaAgent] | None = None) -> PersonaAgent:
        pool = pool or self._personas
        # Prefer anyone other than the last speaker; if that filter empties
        # the pool, just fall back to the first persona.
        others = [p for p in pool if p.name != self._last_speaker]
        return others[0] if others else pool[0]

    async def _llm_pick_speaker(self, eligible: list[PersonaAgent], trigger_reason: str) -> str:
        """Ask Groq Llama which persona should speak.

        We hand it labels + recent lines for each candidate, the recent
        transcript, and the trigger. The system prompt is short and
        prescriptive — this is a routing call, not a creative one.
        """
        prompt = self._build_selector_prompt(eligible, trigger_reason)
        chat_ctx = llm.ChatContext.empty()
        chat_ctx.add_message(role="system", content=_SELECTOR_SYSTEM)
        chat_ctx.add_message(role="user", content=prompt)

        buf: list[str] = []
        async with self._selector_llm.chat(chat_ctx=chat_ctx) as stream:
            async for chunk in stream:
                if chunk.delta and chunk.delta.content:
                    buf.append(chunk.delta.content)
        raw = "".join(buf).strip()
        return self._parse_selector_response(raw, eligible)

    def _build_selector_prompt(self, eligible: list[PersonaAgent], trigger_reason: str) -> str:
        candidates_block: list[str] = []
        for p in eligible:
            recent = p.commentary_history[-3:]
            recent_text = "\n  ".join(f"- {line}" for line in recent) or "(none yet)"
            candidates_block.append(
                f'CANDIDATE name="{p.name}" label="{p.label}"\n  recent lines:\n  {recent_text}'
            )

        last_line = ""
        if self._last_speaker:
            count_note = f" ({self._consecutive_count} in a row)" if self._consecutive_count else ""
            last_line = f"\nLAST SPEAKER: {self._last_speaker}{count_note}"

        return (
            "Pick which persona speaks next, or skip the turn.\n\n"
            f"RECENT TRANSCRIPT:\n{self._full_transcript.recent_transcript() or '(silent)'}\n\n"
            f"TRIGGER: {trigger_reason}{last_line}\n\n"
            + "\n\n".join(candidates_block)
            + '\n\nRespond with strict JSON: {"speaker":"<name>"|"skip","reason":"..."}'
        )

    def _parse_selector_response(self, raw: str, eligible: list[PersonaAgent]) -> str:
        payload = raw.strip()
        # Strip markdown fences if the model added them.
        if payload.startswith("```"):
            payload = payload.strip("`")
            if payload.lower().startswith("json"):
                payload = payload[4:]
            payload = payload.strip()
        try:
            data = json.loads(payload)
            speaker = (data.get("speaker") or "").strip()
            reason = (data.get("reason") or "").strip()
        except (json.JSONDecodeError, AttributeError, TypeError):
            logger.warning("Director LLM produced invalid JSON: %r", raw[:120])
            return self._round_robin_pick(eligible).name

        if speaker == _SKIP:
            logger.info("Director LLM picked SKIP (%s)", reason)
            return _SKIP
        if any(p.name == speaker for p in eligible):
            logger.info("Director LLM picked %s (%s)", speaker, reason)
            return speaker
        logger.warning("Director LLM picked %r which isn't eligible — falling back", speaker)
        return self._round_robin_pick(eligible).name

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
            self._note_speaker(persona.name)
            try:
                await asyncio.wait_for(
                    handle.wait_for_playout(), timeout=COMMENTARY_PLAYOUT_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning("%s reply playout timed out", persona.name)
            except Exception:
                logger.debug("reply wait_for_playout raised", exc_info=True)
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
        _fire_and_forget(
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
        msg_type = msg.get("type")
        if msg_type == "user_talk_start":
            self._user_turn.start()
        elif msg_type == "user_talk_end":
            self._user_turn.end()

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
        _fire_and_forget(
            log_conversation_message(self._session_id, role, content, metadata),
            name=f"director.persist.{role}",
        )


# ---------------------------------------------------------------------------
# Selector LLM — short, prescriptive system prompt
# ---------------------------------------------------------------------------

_SELECTOR_SYSTEM = (
    "You are the show director for a multi-persona AI commentary track on top "
    "of a podcast. Your only job is to pick which comedian persona should "
    "speak next, or to skip the turn entirely.\n\n"
    "Optimise for what would be FUNNIEST and MOST VARIED for the audience:\n"
    "- prefer the persona whose voice has been quiet recently\n"
    "- prefer the persona whose comedic lane fits the current transcript\n"
    "- if the same persona just spoke and the moment doesn't *demand* their "
    "voice again, switch — back-to-back-to-back from one speaker is dull\n"
    "- if the transcript is mid-thought or doesn't give either persona "
    "anything to land on, return skip — silence is funnier than a stretch\n\n"
    "Reply with strict JSON only: "
    '{"speaker":"<persona-name>"|"skip","reason":"<one short clause>"}.\n'
    "Use the lowercase `name` field from the candidate block, NOT the label. "
    "No prose, no markdown, no extra keys."
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
