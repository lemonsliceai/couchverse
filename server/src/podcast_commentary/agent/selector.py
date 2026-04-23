"""Speaker selection — picks which PersonaAgent speaks each turn.

Previously inlined in ``director.py``; lifted into its own module so the
Director focuses on orchestration (timers, intros, data channel) and the
selection concern (LLM prompt + parsing + fallback) lives in one place.

Public surface is tiny: construct one ``SpeakerSelector`` per room, then
``await selector.pick(...)`` every time you want to route a turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from livekit.agents import llm
from livekit.plugins import groq

if TYPE_CHECKING:
    from podcast_commentary.agent.comedian import PersonaAgent

logger = logging.getLogger("podcast-commentary.selector")


# Sentinel returned when the selector LLM decides nobody should speak (e.g.
# mid-sentence podcast pause). Callers compare against ``SKIP`` rather than
# matching the literal string so the contract stays explicit.
SKIP = "skip"

# Hard cap on the selector call. Missing this window falls back to
# round-robin — better a slightly-wrong pick than dead air.
_PICK_TIMEOUT_S = 2.5

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


class SpeakerSelector:
    """Owns the selector LLM and the filter/fallback rules around it."""

    def __init__(self, *, model: str, max_consecutive: int) -> None:
        self._llm = groq.LLM(model=model, max_completion_tokens=80)
        self._max_consecutive = max_consecutive

    async def pick(
        self,
        *,
        personas: list[PersonaAgent],
        transcript: str,
        trigger_reason: str,
        last_speaker: str | None,
        consecutive_count: int,
    ) -> str:
        """Return a persona name to speak, or ``SKIP``.

        Strategy:
          1. Filter out personas over the consecutive-turn cap.
          2. If only one remains, pick it (LLM is overkill).
          3. Otherwise ask the LLM with a short timeout. Fall back to
             round-robin on any failure.
        """
        eligible = [p for p in personas if self._is_eligible(p, last_speaker, consecutive_count)]
        if not eligible:
            eligible = [p for p in personas if p.name != last_speaker] or personas

        if len(eligible) == 1:
            return eligible[0].name

        try:
            return await asyncio.wait_for(
                self._llm_pick(
                    eligible, transcript, trigger_reason, last_speaker, consecutive_count
                ),
                timeout=_PICK_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.warning("Selector LLM timed out — falling back to round-robin")
        except Exception:
            logger.warning("Selector LLM raised — falling back to round-robin", exc_info=True)
        return self._round_robin(eligible, last_speaker).name

    # ------------------------------------------------------------------
    # Filter + fallback
    # ------------------------------------------------------------------
    def _is_eligible(
        self, persona: PersonaAgent, last_speaker: str | None, consecutive_count: int
    ) -> bool:
        """A persona is eligible unless it just hit the consecutive cap."""
        if persona.name != last_speaker:
            return True
        return consecutive_count < self._max_consecutive

    @staticmethod
    def _round_robin(pool: list[PersonaAgent], last_speaker: str | None) -> PersonaAgent:
        others = [p for p in pool if p.name != last_speaker]
        return others[0] if others else pool[0]

    # ------------------------------------------------------------------
    # LLM call + parse
    # ------------------------------------------------------------------
    async def _llm_pick(
        self,
        eligible: list[PersonaAgent],
        transcript: str,
        trigger_reason: str,
        last_speaker: str | None,
        consecutive_count: int,
    ) -> str:
        prompt = self._build_prompt(
            eligible, transcript, trigger_reason, last_speaker, consecutive_count
        )
        chat_ctx = llm.ChatContext.empty()
        chat_ctx.add_message(role="system", content=_SELECTOR_SYSTEM)
        chat_ctx.add_message(role="user", content=prompt)

        buf: list[str] = []
        async with self._llm.chat(chat_ctx=chat_ctx) as stream:
            async for chunk in stream:
                if chunk.delta and chunk.delta.content:
                    buf.append(chunk.delta.content)
        raw = "".join(buf).strip()
        return self._parse_response(raw, eligible, last_speaker)

    @staticmethod
    def _build_prompt(
        eligible: list[PersonaAgent],
        transcript: str,
        trigger_reason: str,
        last_speaker: str | None,
        consecutive_count: int,
    ) -> str:
        candidates_block: list[str] = []
        for p in eligible:
            recent = p.commentary_history[-3:]
            recent_text = "\n  ".join(f"- {line}" for line in recent) or "(none yet)"
            candidates_block.append(
                f'CANDIDATE name="{p.name}" label="{p.label}"\n  recent lines:\n  {recent_text}'
            )

        last_line = ""
        if last_speaker:
            count_note = f" ({consecutive_count} in a row)" if consecutive_count else ""
            last_line = f"\nLAST SPEAKER: {last_speaker}{count_note}"

        return (
            "Pick which persona speaks next, or skip the turn.\n\n"
            f"RECENT TRANSCRIPT:\n{transcript or '(silent)'}\n\n"
            f"TRIGGER: {trigger_reason}{last_line}\n\n"
            + "\n\n".join(candidates_block)
            + '\n\nRespond with strict JSON: {"speaker":"<name>"|"skip","reason":"..."}'
        )

    def _parse_response(
        self, raw: str, eligible: list[PersonaAgent], last_speaker: str | None
    ) -> str:
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
            logger.warning("Selector LLM produced invalid JSON: %r", raw[:120])
            return self._round_robin(eligible, last_speaker).name

        if speaker == SKIP:
            logger.info("Selector picked SKIP (%s)", reason)
            return SKIP
        if any(p.name == speaker for p in eligible):
            logger.info("Selector picked %s (%s)", speaker, reason)
            return speaker
        logger.warning("Selector picked %r which isn't eligible — falling back", speaker)
        return self._round_robin(eligible, last_speaker).name


__all__ = ["SKIP", "SpeakerSelector"]
