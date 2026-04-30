"""``speak_intro`` picks from a pool of variants instead of a fixed line.

Pre-fix every session opened with the same hardcoded sentence per
persona — users noticed because reloading the extension produced bit-for-bit
identical first impressions. The fix is the standard one for "the bot
greets me the same way every time": a hand-authored variant pool plus
``random.choice`` at the call site (no LLM, no added latency, no risk
of off-brand output). This test pins the rotation so a future
"clean it up to one string" refactor fails loudly.
"""

from __future__ import annotations

import dataclasses

import pytest

from podcast_commentary.agent.comedian import PersonaAgent

from ._stub_config import make_stub_config


@pytest.fixture(autouse=True)
def _stub_external_keys(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-used")


class _RecordingGate:
    """Captures the text passed to ``gate.say`` so the test can read back
    which intro variant ``speak_intro`` selected on each call."""

    def __init__(self) -> None:
        self.spoken: list[str] = []

    def say(self, *, text: str) -> object:
        self.spoken.append(text)
        return object()


def test_speak_intro_rotates_through_intro_lines():
    """Across many calls every variant in the pool should be selected.

    With three variants and 200 trials, the probability that any one
    variant is missed is ``(2/3)**200 ≈ 6e-36`` — well below the bar
    where a CI flake is plausible, so this is hermetic without seeding.
    """
    pool = ("alpha-opener", "beta-opener", "gamma-opener")
    base = make_stub_config("persona_a")
    config = dataclasses.replace(
        base,
        persona=dataclasses.replace(base.persona, intro_lines=pool),
    )
    persona = PersonaAgent(config=config)
    gate = _RecordingGate()
    persona._gate = gate  # type: ignore[assignment]

    for _ in range(200):
        persona.speak_intro()

    seen = set(gate.spoken)
    missing = set(pool) - seen
    assert not missing, f"speak_intro never selected: {sorted(missing)}"


def test_speak_intro_handles_single_element_pool():
    """A one-element pool is degenerate but legal — the persona just
    always speaks that one line. We guard against an off-by-one
    ``random.choice`` regression that would IndexError on len == 1.
    """
    base = make_stub_config("persona_a")
    config = dataclasses.replace(
        base,
        persona=dataclasses.replace(base.persona, intro_lines=("only-line",)),
    )
    persona = PersonaAgent(config=config)
    gate = _RecordingGate()
    persona._gate = gate  # type: ignore[assignment]

    persona.speak_intro()

    assert gate.spoken == ["only-line"]
