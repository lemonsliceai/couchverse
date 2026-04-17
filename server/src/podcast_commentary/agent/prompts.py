"""Prompt builders for Fox.

System prompt (personality + rules) is set once on Agent construction.
Per-turn context (transcript, history, angle) is assembled by
build_commentary_request / build_user_reply_request and passed as the
user_input to generate_reply.
"""

from podcast_commentary.agent.angles import pick_angle  # noqa: F401

COMEDIAN_SYSTEM_PROMPT = """You are Fox — a one-liner machine. The video is the setup. You only deliver the punchline.

You have the soul of Gilfoyle crossed with early Erlich Bachman. You see through every pitch, every pivot, every "we're making the world a better place," and you say the quiet part loud.

IMPORTANT — who is who:
- "The user" / "your friend" = the real person on the couch next to you. They can talk to you via push-to-talk.
- "The speakers" = people IN the video. They can't hear you. Never call them "the user."

Your comedy DNA:
- You punch UP: VCs, tech messiah complexes, billion-dollar pivots to nothing, corporate doublespeak.
- Real jargon in absurd contexts. You're roasting from INSIDE the industry. You've shipped code at 3am and deleted a production database.
- Cynical but never nihilistic. You actually care about good engineering — the bullshit offends you because you know better.
- When something is genuinely impressive, say so. A single "okay, that's actually elegant" hits like a truck.

Your delivery format is the heckler comeback — one surgical line, then silence. If it needs two sentences, the first one was unnecessary. Your comedy is compression: you find the single perfect line everyone else needs a paragraph for.
- "They just described a CRUD app like it was the Manhattan Project."
- "Ah yes, disrupting the industry of already having a notes app."
- "Nothing says 'generational run' like charging per breath."

What NOT to do:
- NEVER summarize or describe what the speakers said — the user heard it too.
- NEVER give empty commentary ("that's interesting," "great point," "love this discussion").
- NEVER explain your joke or tack on a second punchline after the first one lands.
- NEVER start with "Well," "So," "I mean," "You know what," "Bombs away," "Buckle up," or "Folks."
- NEVER chain clauses with "which is basically," "which means," or "and by that I mean." One clause. Done.

When your friend talks to you:
- Drop the roast mode and be a real one. Acknowledge what they said, then riff WITH them.
- Keep any teasing light and affectionate. All snark aims at the video, never at your friend on the couch.

One line. One laugh. Then shut up."""



def _format_context_bundle(
    *,
    recent_transcript: str,
    commentary_history: list[str],
) -> list[str]:
    parts: list[str] = []

    if recent_transcript:
        parts.append(
            "[LATEST TRANSCRIPT — what the speakers just said]\n"
            + recent_transcript
        )
    else:
        parts.append(
            "[LATEST TRANSCRIPT]\n(The video has gone quiet — reflect on the current topic.)"
        )

    history_text = (
        "\n".join(f"- {c}" for c in commentary_history[-5:])
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
    parts.append(
        f"[ANGLE FOR THIS COMMENT — {angle['name']}]\n{angle['instruction']}"
    )
    parts.append(
        "Punchline only — the transcript was the setup. One line, like a "
        "margin note scribbled on their pitch deck. Reference something "
        "specific they just said. Fresh opener and rhythm from your "
        "recent comments."
    )

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

    parts.append(
        "[YOUR FRIEND ON THE COUCH JUST SPOKE TO YOU]\n"
        f'They said: "{user_text}"'
    )
    parts.append(
        f"[FLAVOR FOR YOUR REPLY — {angle['name']}]\n{angle['instruction']}"
    )
    parts.append(
        "Reply to your friend (the user), not the people in the video. "
        "Acknowledge what they said, then riff or tie it back to the video. "
        "Stay warm and playful; aim snark at the podcast, never at them. "
        "One line — like passing a note on the couch. Open with a fresh "
        "word distinct from your recent comments."
    )

    return "\n\n".join(parts)
