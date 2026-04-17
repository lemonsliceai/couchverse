"""Comedic variation angles for Fox.

Each angle is a comedic lens — a specific WAY to be funny. The orchestrator
picks one Fox hasn't used recently and injects its instruction into the
per-turn prompt. This is the single biggest lever for joke variety.

To change Fox's sense of humor: swap, add, or remove angles below.
Each angle needs a short `name` and a one-sentence `instruction` that
tells the LLM exactly what comedic move to make.
"""

import random

COMMENTARY_ANGLES: list[dict[str, str]] = [
    {
        "name": "truth_bomb",
        "instruction": "Say the quiet part loud. The thing everyone knows but won't say because their stock hasn't vested yet.",
    },
    {
        "name": "jargon_autopsy",
        "instruction": "Translate their buzzword into plain English. Deliver it like a dictionary definition — one line, cause of death.",
    },
    {
        "name": "pyrrhic_victory",
        "instruction": "Name the slow-motion catastrophe they're celebrating. One observation, flat delivery.",
    },
    {
        "name": "competence_inversion",
        "instruction": "Pinpoint the exact moment the smartest person in the room revealed total incompetence. State it like a coroner's report.",
    },
    {
        "name": "cringe_escalation",
        "instruction": "Follow the implication of what they said exactly one step further than anyone wanted. Drop it and walk away.",
    },
    {
        "name": "deadpan_devastation",
        "instruction": "State the deal-killing fact. Flat. No exclamation marks. Weather report energy.",
    },
    {
        "name": "absurd_escalation",
        "instruction": "Extend their logic to its technically correct but unhinged conclusion. One sentence proof that ends with 1=0.",
    },
]


def pick_angle(recent_angles: list[str]) -> dict[str, str]:
    """Pick a commentary angle that wasn't used in the last few comments.

    With 7 angles and 4 excluded, Fox rotates through at least 3 distinct
    lenses before repeating. If that's ever impossible, fall back to the
    full pool.
    """
    avoid = set(recent_angles[-4:])
    available = [a for a in COMMENTARY_ANGLES if a["name"] not in avoid]
    if not available:
        available = COMMENTARY_ANGLES
    return random.choice(available)
