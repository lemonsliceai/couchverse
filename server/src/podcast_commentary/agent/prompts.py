"""Prompt builders for Fox.

System prompt (personality + rules) is set once on Agent construction.
Per-turn context (transcript, history, angle) is assembled by
build_commentary_request / build_user_reply_request and passed as the
user_input to generate_reply.

All prompt text is sourced from the active FoxConfig preset — see
``fox_config.py`` and ``fox_configs/default.py``.
"""

from podcast_commentary.agent.angles import pick_angle  # noqa: F401
from podcast_commentary.agent.fox_config import CONFIG

# Re-exported so callers can ``from prompts import COMEDIAN_SYSTEM_PROMPT``
# without reaching into the config object.
COMEDIAN_SYSTEM_PROMPT = CONFIG.persona.system_prompt


def _format_context_bundle(
    *,
    recent_transcript: str,
    commentary_history: list[str],
) -> list[str]:
    parts: list[str] = []

    if recent_transcript:
        parts.append("[LATEST TRANSCRIPT — what the speakers just said]\n" + recent_transcript)
    else:
        parts.append(
            "[LATEST TRANSCRIPT]\n(The video has gone quiet — reflect on the current topic.)"
        )

    shown = CONFIG.context.comments_shown_in_prompt
    history_text = (
        "\n".join(f"- {c}" for c in commentary_history[-shown:])
        if commentary_history
        else "(none yet)"
    )
    parts.append(
        "[YOUR RECENT COMMENTS — use a FRESH structure, opener, and joke format each time]\n"
        + history_text
    )

    return parts


def build_commentary_request(
    *,
    recent_transcript: str,
    commentary_history: list[str],
    trigger_reason: str,
    energy_level: str = "amused",
    angle: dict[str, str] | None = None,
) -> str:
    """Assemble the per-turn prompt for unsolicited commentary."""
    if angle is None:
        angle = pick_angle([])

    parts = _format_context_bundle(
        recent_transcript=recent_transcript,
        commentary_history=commentary_history,
    )

    parts.append(f"[WHY YOU'RE SPEAKING NOW]\n{trigger_reason}")
    parts.append(f"[ENERGY] {energy_level}")
    parts.append(f"[ANGLE FOR THIS COMMENT — {angle['name']}]\n{angle['instruction']}")
    parts.append(CONFIG.persona.commentary_cta)

    return "\n\n".join(parts)


def build_user_reply_request(
    *,
    user_text: str,
    recent_transcript: str,
    commentary_history: list[str],
    angle: dict[str, str] | None = None,
) -> str:
    """Assemble the per-turn prompt for a push-to-talk reply."""
    if angle is None:
        angle = pick_angle([])

    parts = _format_context_bundle(
        recent_transcript=recent_transcript,
        commentary_history=commentary_history,
    )

    parts.append(f'[YOUR FRIEND ON THE COUCH JUST SPOKE TO YOU]\nThey said: "{user_text}"')
    parts.append(f"[FLAVOR FOR YOUR REPLY — {angle['name']}]\n{angle['instruction']}")
    parts.append(CONFIG.persona.user_reply_cta)

    return "\n\n".join(parts)
