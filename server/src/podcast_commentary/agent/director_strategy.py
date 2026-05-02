"""Director — LLM-judged speaker pick with round-robin fallback.

Lives in its own file because every other strategy in ``selector.py`` is
under 30 lines; this one carries the prompt, the lazy ``groq.LLM``, the
2.5s timeout, JSON parsing, and the consecutive-turn cap. Keeping the
two simple strategies + facade in ``selector.py`` and the LLM-heavy
variant here keeps each file focused on one level of complexity.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from livekit.agents import llm
from livekit.plugins import groq

from podcast_commentary.core.config import settings

if TYPE_CHECKING:
    from podcast_commentary.agent.comedian import PersonaAgent
    from podcast_commentary.agent.selector import SpeakerSelectorStrategy

logger = logging.getLogger("podcast-commentary.director_strategy")


# Hard cap on the LLM call — better a slightly-wrong pick than dead air.
_PICK_TIMEOUT_S = 2.5

_SYSTEM_PROMPT = (
    "You are the show director for a multi-persona AI commentary track on top "
    "of a podcast. Your only job is to pick which comedian persona should "
    "speak next. Someone ALWAYS speaks — skipping is not an option.\n\n"
    "Optimise for what would be FUNNIEST and MOST VARIED for the audience:\n"
    "- prefer the persona whose voice has been quiet recently\n"
    "- prefer the persona whose comedic lane fits the current transcript\n"
    "- if the same persona just spoke, switch — back-to-back-to-back from "
    "one speaker is dull\n\n"
    "Reply with strict JSON only: "
    '{"speaker":"<persona-name>","reason":"<one short clause>"}.\n'
    "Use the lowercase `name` field from the candidate block, NOT the label. "
    "No prose, no markdown, no extra keys."
)


class DirectorStrategy:
    """LLM-judged pick. Falls back to ``OrderedStrategy`` on any failure."""

    name = "director"

    def __init__(
        self,
        *,
        model: str | None = None,
        max_consecutive: int | None = None,
        fallback: SpeakerSelectorStrategy | None = None,
    ) -> None:
        # Lazy import avoids a circular dependency with ``selector``.
        from podcast_commentary.agent.selector import OrderedStrategy

        self._model = model or settings.DIRECTOR_LLM_MODEL
        self._max_consecutive = (
            max_consecutive if max_consecutive is not None else settings.DIRECTOR_MAX_CONSECUTIVE
        )
        self._fallback: SpeakerSelectorStrategy = fallback or OrderedStrategy()
        self._llm: groq.LLM | None = None
        self._init_lock = asyncio.Lock()
        # Once the LLM constructor has raised, stop retrying for the rest
        # of the session — flailing on every pick masks the real failure.
        self._init_failed: bool = False
        # Streak of consecutive turns by the same speaker observed at
        # pick-time. Cap-triggers fall back to ordered.
        self._consecutive_count: int = 0

    async def pick(
        self,
        *,
        personas: list[PersonaAgent],
        transcript: str,
        trigger_reason: str,
        last_speaker: str | None,
    ) -> str:
        kwargs = {
            "personas": personas,
            "transcript": transcript,
            "trigger_reason": trigger_reason,
            "last_speaker": last_speaker,
        }

        if last_speaker is not None and self._consecutive_count >= self._max_consecutive:
            self._consecutive_count = 0
            return await self._fallback.pick(**kwargs)

        client = await self._ensure_llm()
        if client is None:
            return await self._fallback.pick(**kwargs)

        picked = await self._safe_llm_pick(client, personas, transcript, trigger_reason, last_speaker)
        if picked is None or not any(p.name == picked for p in personas):
            if picked is not None:
                logger.warning("Director picked %r which isn't a persona — fallback", picked)
            return await self._fallback.pick(**kwargs)

        self._consecutive_count = self._consecutive_count + 1 if picked == last_speaker else 1
        return picked

    async def _safe_llm_pick(
        self,
        client: groq.LLM,
        personas: list[PersonaAgent],
        transcript: str,
        trigger_reason: str,
        last_speaker: str | None,
    ) -> str | None:
        try:
            return await asyncio.wait_for(
                self._llm_pick(client, personas, transcript, trigger_reason, last_speaker),
                timeout=_PICK_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.warning("Director LLM timed out — falling back to round-robin")
        except Exception:
            logger.warning("Director LLM raised — falling back to round-robin", exc_info=True)
        return None

    async def _ensure_llm(self) -> groq.LLM | None:
        if self._llm is not None or self._init_failed:
            return self._llm
        async with self._init_lock:
            if self._llm is not None or self._init_failed:
                return self._llm
            try:
                self._llm = groq.LLM(model=self._model, max_completion_tokens=80)
            except Exception:
                logger.warning("Director LLM init failed — staying in fallback", exc_info=True)
                self._init_failed = True
        return self._llm

    async def _llm_pick(
        self,
        client: groq.LLM,
        personas: list[PersonaAgent],
        transcript: str,
        trigger_reason: str,
        last_speaker: str | None,
    ) -> str | None:
        chat_ctx = llm.ChatContext.empty()
        chat_ctx.add_message(role="system", content=_SYSTEM_PROMPT)
        chat_ctx.add_message(
            role="user",
            content=_build_prompt(personas, transcript, trigger_reason, last_speaker),
        )

        buf: list[str] = []
        async with client.chat(chat_ctx=chat_ctx) as stream:
            async for chunk in stream:
                if chunk.delta and chunk.delta.content:
                    buf.append(chunk.delta.content)
        return _parse_response("".join(buf).strip())


def _build_prompt(
    personas: list[PersonaAgent],
    transcript: str,
    trigger_reason: str,
    last_speaker: str | None,
) -> str:
    candidates_block: list[str] = []
    for p in personas:
        recent = p.commentary_history[-3:]
        recent_text = "\n  ".join(f"- {line}" for line in recent) or "(none yet)"
        candidates_block.append(
            f'CANDIDATE name="{p.name}" label="{p.label}"\n  recent lines:\n  {recent_text}'
        )

    last_line = f"\nLAST SPEAKER: {last_speaker}" if last_speaker else ""
    return (
        "Pick which persona speaks next. Someone MUST speak — skipping "
        "is not an option.\n\n"
        f"RECENT TRANSCRIPT:\n{transcript or '(silent)'}\n\n"
        f"TRIGGER: {trigger_reason}{last_line}\n\n"
        + "\n\n".join(candidates_block)
        + '\n\nRespond with strict JSON: {"speaker":"<name>","reason":"..."}'
    )


def _parse_response(raw: str) -> str | None:
    payload = raw.strip()
    if payload.startswith("```"):
        payload = payload.strip("`")
        if payload.lower().startswith("json"):
            payload = payload[4:]
        payload = payload.strip()
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, AttributeError, TypeError):
        logger.warning("Director LLM produced invalid JSON: %r", raw[:120])
        return None
    if not isinstance(data, dict):
        return None
    speaker = (data.get("speaker") or "").strip()
    return speaker or None


__all__ = ["DirectorStrategy"]
