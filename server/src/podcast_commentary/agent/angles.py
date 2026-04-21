"""Comedic variation angles for Fox.

Each angle is a comedic lens — a specific WAY to be funny. The orchestrator
picks one Fox hasn't used recently and injects its instruction into the
per-turn prompt. This is the single biggest lever for joke variety.

The angle bank and lookback window both live in the active FoxConfig preset
(see ``fox_configs/default.py``). To change Fox's sense of humor, edit the
preset's ``comedic_angles`` tuple.
"""

import random

from podcast_commentary.agent.fox_config import CONFIG

# Module-level alias so existing imports of ``COMMENTARY_ANGLES`` still work.
# List-typed for compatibility with any caller that iterates or filters.
COMMENTARY_ANGLES: list[dict[str, str]] = list(CONFIG.persona.comedic_angles)


def pick_angle(recent_angles: list[str]) -> dict[str, str]:
    """Pick a commentary angle that wasn't used in the last few comments.

    The number of recent angles to avoid is configured by
    ``persona.angle_lookback``. If that ever makes the pool empty (more
    angles excluded than available), fall back to the full bank.
    """
    lookback = CONFIG.persona.angle_lookback
    avoid = set(recent_angles[-lookback:])
    available = [a for a in COMMENTARY_ANGLES if a["name"] not in avoid]
    if not available:
        available = COMMENTARY_ANGLES
    return random.choice(available)
