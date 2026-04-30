"""Wait for a SpeechHandle's playout to finish.

With one ``AvatarSession`` per room, the framework's
``lk.playback_finished`` RPC arrives reliably and
``SpeechHandle.wait_for_playout`` resolves on its own. We just await it
with a hard upper bound so a misbehaving vendor can't stall a turn
indefinitely.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from podcast_commentary.agent.comedian import PersonaAgent
from podcast_commentary.agent.metrics import playout_finished_rpc_total

logger = logging.getLogger("podcast-commentary.playout")


class PlayoutWaiter:
    """Stateless helper used by intros + commentary turns.

    Construct once per Director and reuse across personas — the persona
    is passed in per-call so a single waiter handles all of them.
    """

    def __init__(self) -> None:
        self._timeout_count: int = 0

    @property
    def timeout_count(self) -> int:
        """Turns that exceeded the per-turn timeout this session."""
        return self._timeout_count

    async def wait(
        self,
        persona: PersonaAgent,
        handle: Any,
        *,
        timeout: float,
        label: str,
    ) -> None:
        """Await ``handle.wait_for_playout`` with a hard upper bound."""
        tag = f"[{persona.name}|{label}]"
        logger.info("%s playout wait begin (timeout=%.1fs)", tag, timeout)

        try:
            await asyncio.wait_for(handle.wait_for_playout(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(
                "%s playout TIMEOUT after %.1fs — handle did not resolve",
                tag,
                timeout,
            )
            playout_finished_rpc_total.inc(persona=persona.name, outcome="timeout")
            self._timeout_count += 1
            return

        logger.info("%s playout CONFIRMED by vendor RPC", tag)
        playout_finished_rpc_total.inc(persona=persona.name, outcome="ok")

    @staticmethod
    def attach_observers(personas: list[PersonaAgent]) -> None:
        """Subscribe to ``playback_finished`` on each persona's audio output.

        Purely observational — the log line lets operators correlate
        framework playback events with our turn-level metrics during
        triage. No behavior depends on it.
        """
        for persona in personas:
            audio = persona._audio_output()
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


__all__ = ["PlayoutWaiter"]
