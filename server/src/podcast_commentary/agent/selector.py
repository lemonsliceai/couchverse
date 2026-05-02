"""Speaker selection — picks which PersonaAgent speaks each turn.

Three strategies, swappable at runtime from the side panel:

  * ``OrderedStrategy``  — strict round-robin in PERSONAS-list order (default).
  * ``ShuffleStrategy``  — uniform random pick excluding the last speaker.
  * ``DirectorStrategy`` — LLM-judged pick (lives in ``director_strategy.py``
    because it carries its own LLM client, prompt, timeout, and fallback).

The pipeline always calls ``SpeakerSelector.pick(...)``; the active
strategy is held behind a swap lock so a mode change between turns lands
atomically without tearing a pick mid-call.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from podcast_commentary.agent.comedian import PersonaAgent

logger = logging.getLogger("podcast-commentary.selector")


SELECTION_MODES: tuple[str, ...] = ("ordered", "shuffle", "director")
DEFAULT_SELECTION_MODE = "ordered"


class SpeakerSelectorStrategy(Protocol):
    """Common surface every strategy implements."""

    name: str

    async def pick(
        self,
        *,
        personas: list[PersonaAgent],
        transcript: str,
        trigger_reason: str,
        last_speaker: str | None,
    ) -> str: ...


class OrderedStrategy:
    """Strict round-robin in PERSONAS list order."""

    name = "ordered"

    async def pick(
        self,
        *,
        personas: list[PersonaAgent],
        transcript: str,  # noqa: ARG002
        trigger_reason: str,  # noqa: ARG002
        last_speaker: str | None,
    ) -> str:
        if last_speaker is None:
            return personas[0].name
        for i, p in enumerate(personas):
            if p.name == last_speaker:
                return personas[(i + 1) % len(personas)].name
        return personas[0].name


class ShuffleStrategy:
    """Uniform random pick, never repeating the last speaker."""

    name = "shuffle"

    def __init__(self, *, rng: random.Random | None = None) -> None:
        # SystemRandom by default; tests opt in to a seeded RNG.
        self._rng = rng or random.SystemRandom()

    async def pick(
        self,
        *,
        personas: list[PersonaAgent],
        transcript: str,  # noqa: ARG002
        trigger_reason: str,  # noqa: ARG002
        last_speaker: str | None,
    ) -> str:
        candidates = [p for p in personas if p.name != last_speaker] or personas
        return self._rng.choice(candidates).name


class SpeakerSelector:
    """Holds the active strategy and serializes mode swaps with picks."""

    def __init__(self, *, default_mode: str = DEFAULT_SELECTION_MODE) -> None:
        if default_mode not in SELECTION_MODES:
            raise ValueError(f"Unknown default_mode: {default_mode!r}")
        # Director is constructed lazily on first activation so an
        # Ordered-only session never instantiates ``groq.LLM``.
        self._strategies: dict[str, SpeakerSelectorStrategy] = {
            "ordered": OrderedStrategy(),
            "shuffle": ShuffleStrategy(),
        }
        if default_mode == "director":
            self._strategies["director"] = self._build_director()
        self._active = self._strategies[default_mode]
        self._swap_lock = asyncio.Lock()

    @property
    def mode(self) -> str:
        return self._active.name

    async def pick(
        self,
        *,
        personas: list[PersonaAgent],
        transcript: str,
        trigger_reason: str,
        last_speaker: str | None,
    ) -> str:
        # Snapshot under the swap lock so a concurrent ``set_mode``
        # can't tear ``_active`` mid-pick. The pick itself runs unlocked
        # so a slow Director call doesn't block subsequent mode swaps.
        async with self._swap_lock:
            strategy = self._active
        return await strategy.pick(
            personas=personas,
            transcript=transcript,
            trigger_reason=trigger_reason,
            last_speaker=last_speaker,
        )

    async def set_mode(self, mode: str) -> bool:
        """Switch the active strategy. Returns False on unknown mode."""
        if mode not in SELECTION_MODES:
            return False
        async with self._swap_lock:
            if mode not in self._strategies:
                self._strategies[mode] = self._build_director()
            self._active = self._strategies[mode]
        return True

    @staticmethod
    def _build_director() -> SpeakerSelectorStrategy:
        # Local import keeps Director's livekit/groq dependencies out of
        # the import path for Ordered-only sessions.
        from podcast_commentary.agent.director_strategy import DirectorStrategy

        return DirectorStrategy()


__all__ = [
    "DEFAULT_SELECTION_MODE",
    "OrderedStrategy",
    "SELECTION_MODES",
    "ShuffleStrategy",
    "SpeakerSelector",
    "SpeakerSelectorStrategy",
]
