"""Apply the side panel's selection-mode toggle onto the live SpeakerSelector.

Thin wrapper around ``SpeakerSelector.set_mode`` that lives on the same
seam as ``SettingsController`` — Director registers it as the inbound
handler for ``selection_mode`` packets on the commentary.control channel.
"""

from __future__ import annotations

import logging

from podcast_commentary.agent.selector import SELECTION_MODES, SpeakerSelector

logger = logging.getLogger("podcast-commentary.selection_mode")


class SelectionModeController:
    def __init__(self, *, selector: SpeakerSelector) -> None:
        self._selector = selector

    async def set_mode(self, mode: str | None) -> None:
        if mode is None:
            return
        if mode not in SELECTION_MODES:
            logger.warning("Ignoring unknown selection mode: %r", mode)
            return
        ok = await self._selector.set_mode(mode)
        if ok:
            logger.info("Selection mode → %s", mode)


__all__ = ["SelectionModeController"]
