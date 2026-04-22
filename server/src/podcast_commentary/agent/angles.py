"""Comedic variation angles for each persona.

Each angle is the *name* of a comedic lens defined in the persona's system
prompt (see ``fox_configs/<persona>.py`` SYSTEM_PROMPT). The orchestrator
picks one the persona hasn't used recently and injects it as
``[LENS: name]`` into the per-turn prompt — the LLM looks up the
definition from the system prompt. This rotation is the single biggest
lever for joke variety.

In a multi-persona room each persona has its own angle bank and own
``angle_lookback`` window — Fox's recent angles never block Alien's pick
and vice versa.
"""

import random

from podcast_commentary.agent.fox_config import CONFIG, FoxConfig

# Module-level export for back-compat. New code should pass ``config`` to
# ``pick_angle`` so each persona uses its own lens bank.
COMMENTARY_ANGLES: list[str] = list(CONFIG.persona.comedic_angles)


def pick_angle(recent_angles: list[str], *, config: FoxConfig | None = None) -> str:
    """Pick a commentary lens that wasn't used in the last few comments.

    The number of recent angles to avoid is configured by
    ``persona.angle_lookback``. If that ever makes the pool empty (more
    angles excluded than available), fall back to the full bank.
    """
    cfg = config or CONFIG
    lookback = cfg.persona.angle_lookback
    bank = list(cfg.persona.comedic_angles)
    avoid = set(recent_angles[-lookback:])
    available = [a for a in bank if a not in avoid]
    if not available:
        available = bank
    return random.choice(available)
