"""Prompt builders for each persona.

System prompt (personality + rules) is set once on Agent construction.
Per-turn context (transcript, history, angle) is assembled by
``build_commentary_request`` / ``build_user_reply_request`` and passed as
the ``user_input`` to ``generate_reply``.

All prompt text is sourced from each persona's FoxConfig — see
``fox_config.py`` and ``fox_configs/<persona>.py``. The functions below
accept the config explicitly so the same builder works for Fox and Alien
in the same process.
"""

from podcast_commentary.agent.angles import pick_angle
from podcast_commentary.agent.fox_config import CONFIG, FoxConfig

# Sentinel that ``PersonaAgent.llm_node`` scans for to decide whether to
# buffer the full LLM response and parse candidates. Persona-neutral so any
# preset that enables verbalized sampling gets selection for free.
SAMPLING_SENTINEL = "[[VS_CANDIDATES]]"

# Re-exported for back-compat. New code should call ``build_system_prompt``
# with a specific persona's config — there is no "the" system prompt when
# multiple personas share the room.
COMEDIAN_SYSTEM_PROMPT = CONFIG.persona.system_prompt


def build_system_prompt(config: FoxConfig) -> str:
    """Each persona's system prompt — handed to its Agent at construction."""
    return config.persona.system_prompt


def _sampling_instruction(config: FoxConfig) -> str | None:
    """Pipeline-level output-format directive appended when VS is enabled.

    Persona-neutral: never says "joke" or "punchline" — each preset's own
    system prompt + CTA decide what a ``line`` is. Returns None when VS
    is off so the block is omitted from the prompt entirely.
    """
    n = config.sampling.num_candidates
    if n <= 1:
        return None
    return (
        f"{SAMPLING_SENTINEL}\n"
        f"[OUTPUT FORMAT — pipeline spec, not creative direction]\n"
        f"Return strict JSON only — no prose, no markdown fences: "
        f'{{"candidates":[{{"line":"...","p":0.0}}]}}\n'
        f"Produce exactly {n} candidates. Each `line` is a complete response "
        f"written to the rules above. `p` is your own confidence (0.0-1.0) "
        f"that this candidate lands best. Stay in character across all of them."
    )


def _format_context_bundle(
    config: FoxConfig,
    *,
    recent_transcript: str,
    commentary_history: list[str],
    co_speaker_history: list[str] | None = None,
    co_speaker_label: str | None = None,
) -> list[str]:
    parts: list[str] = []

    if recent_transcript:
        parts.append("[LATEST TRANSCRIPT — what the speakers just said]\n" + recent_transcript)
    else:
        parts.append(
            "[LATEST TRANSCRIPT]\n(The video has gone quiet — reflect on the current topic.)"
        )

    shown = config.context.comments_shown_in_prompt
    history_text = (
        "\n".join(f"- {c}" for c in commentary_history[-shown:])
        if commentary_history
        else "(none yet)"
    )
    parts.append(
        "[YOUR RECENT COMMENTS — use a FRESH structure, opener, and joke format each time]\n"
        + history_text
    )

    # Show what the OTHER persona has said recently so this one can avoid
    # stepping on their bit / can react with awareness instead of bumping
    # into them. Don't address them by name — both personas talk past each
    # other to the audience (system prompts enforce this).
    if co_speaker_history and co_speaker_label:
        co_text = (
            "\n".join(f"- {c}" for c in co_speaker_history[-shown:])
            if co_speaker_history
            else "(none yet)"
        )
        parts.append(
            f"[WHAT {co_speaker_label.upper()} JUST SAID — don't repeat their angle, "
            f"don't address them directly, find your own way in]\n" + co_text
        )

    return parts


def build_commentary_request(
    *,
    config: FoxConfig,
    recent_transcript: str,
    commentary_history: list[str],
    trigger_reason: str,
    energy_level: str = "amused",
    angle: str | None = None,
    co_speaker_history: list[str] | None = None,
    co_speaker_label: str | None = None,
) -> str:
    """Assemble the per-turn prompt for unsolicited commentary."""
    if angle is None:
        angle = pick_angle([], config=config)

    parts = _format_context_bundle(
        config,
        recent_transcript=recent_transcript,
        commentary_history=commentary_history,
        co_speaker_history=co_speaker_history,
        co_speaker_label=co_speaker_label,
    )

    parts.append(f"[WHY YOU'RE SPEAKING NOW]\n{trigger_reason}")
    parts.append(f"[ENERGY] {energy_level}")
    parts.append(f"[LENS: {angle}]")
    parts.append(config.persona.commentary_cta)

    sampling = _sampling_instruction(config)
    if sampling:
        parts.append(sampling)

    return "\n\n".join(parts)


def build_user_reply_request(
    *,
    config: FoxConfig,
    user_text: str,
    recent_transcript: str,
    commentary_history: list[str],
    angle: str | None = None,
    co_speaker_history: list[str] | None = None,
    co_speaker_label: str | None = None,
) -> str:
    """Assemble the per-turn prompt for a push-to-talk reply."""
    if angle is None:
        angle = pick_angle([], config=config)

    parts = _format_context_bundle(
        config,
        recent_transcript=recent_transcript,
        commentary_history=commentary_history,
        co_speaker_history=co_speaker_history,
        co_speaker_label=co_speaker_label,
    )

    parts.append(f'[YOUR FRIEND ON THE COUCH JUST SPOKE TO YOU]\nThey said: "{user_text}"')
    parts.append(f"[LENS: {angle}]")
    parts.append(config.persona.user_reply_cta)

    sampling = _sampling_instruction(config)
    if sampling:
        parts.append(sampling)

    return "\n\n".join(parts)
