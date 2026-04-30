"""Shared in-memory ``FoxConfig`` stubs for unit tests.

Tests should not load real preset files from ``fox_configs/`` — that
couples the test suite to whichever characters happen to ship today.
``make_stub_config(name)`` builds a minimal, fully-valid ``FoxConfig``
in memory with synthetic identifiers, so tests can name their fixtures
``"persona_a"`` / ``"persona_b"`` without touching the production preset
bank.
"""

from __future__ import annotations

from podcast_commentary.agent.fox_config import (
    AvatarConfig,
    ContextConfig,
    FoxConfig,
    LLMConfig,
    PersonaConfig,
    PlayoutConfig,
    SamplingConfig,
    STTConfig,
    TimingConfig,
    TTSConfig,
    VADConfig,
)


def make_stub_config(name: str, *, label: str | None = None) -> FoxConfig:
    """Build a minimal ``FoxConfig`` for tests.

    Field values are deliberately bland — tests that care about a
    specific value override it via ``dataclasses.replace``.
    """
    return FoxConfig(
        name=name,
        persona=PersonaConfig(
            system_prompt=f"You are {name}, a test persona.",
            intro_lines=(f"Hi, I'm {name}.", f"Hello there, {name} here.",),
            comedic_angles=("angle_one", "angle_two", "angle_three"),
            angle_lookback=1,
            commentary_cta="Deliver a one-line reaction.",
            speaker_label=label or name,
        ),
        timing=TimingConfig(
            min_silence_between_jokes_s=10.0,
            burst_window_s=60.0,
            max_jokes_per_burst=8,
            burst_cooldown_s=8.0,
            sentences_before_joke=5,
            silence_fallback_s=12.0,
            post_speech_safety_s=2.0,
            transcript_chunk_s=10.0,
        ),
        context=ContextConfig(
            comment_memory_size=10,
            comments_shown_in_prompt=5,
        ),
        llm=LLMConfig(model="stub-llm", max_tokens=256),
        stt=STTConfig(model="stub-stt"),
        tts=TTSConfig(
            voice_id="stub-voice",
            model="stub-tts",
            stability=0.5,
            similarity_boost=0.7,
            speed=1.0,
        ),
        vad=VADConfig(activation_threshold=0.6),
        avatar=AvatarConfig(
            active_prompt="a test avatar reacting",
            idle_prompt="a test avatar listening",
            startup_timeout_s=15.0,
            avatar_image="",
        ),
        playout=PlayoutConfig(
            intro_timeout_s=8.0,
            commentary_timeout_s=20.0,
        ),
        sampling=SamplingConfig(num_candidates=1, selection="max_prob"),
    )
