"""SpeakerSelector + strategy tests.

Pin the contract callers rely on:
  * Ordered: strict round-robin in PERSONAS list order.
  * Shuffle: never repeats the last speaker (when there's a choice).
  * Director: LLM-judged with fallback on every failure mode.
  * Facade: lazy Director construction; mode swap is atomic w.r.t. pick.
"""

from __future__ import annotations

import asyncio

import pytest

from podcast_commentary.agent.comedian import PersonaAgent
from podcast_commentary.agent.director_strategy import DirectorStrategy
from podcast_commentary.agent.selector import (
    OrderedStrategy,
    ShuffleStrategy,
    SpeakerSelector,
)

from ._stub_config import make_stub_config


def _make_persona(name: str) -> PersonaAgent:
    return PersonaAgent(config=make_stub_config(name))


@pytest.fixture
def personas() -> list[PersonaAgent]:
    return [_make_persona("alpha"), _make_persona("beta"), _make_persona("gamma")]


# ---------------------------------------------------------------------------
# OrderedStrategy
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ordered_first_turn_returns_first_persona(personas):
    name = await OrderedStrategy().pick(
        personas=personas, transcript="", trigger_reason="kickoff", last_speaker=None,
    )
    assert name == "alpha"


@pytest.mark.asyncio
async def test_ordered_advances_then_wraps(personas):
    s = OrderedStrategy()
    assert await s.pick(personas=personas, transcript="", trigger_reason="t", last_speaker="alpha") == "beta"
    assert await s.pick(personas=personas, transcript="", trigger_reason="t", last_speaker="beta") == "gamma"
    assert await s.pick(personas=personas, transcript="", trigger_reason="t", last_speaker="gamma") == "alpha"


@pytest.mark.asyncio
async def test_ordered_unknown_last_speaker_resets_to_first(personas):
    name = await OrderedStrategy().pick(
        personas=personas, transcript="", trigger_reason="t", last_speaker="ghost",
    )
    assert name == "alpha"


# ---------------------------------------------------------------------------
# ShuffleStrategy
# ---------------------------------------------------------------------------
class _StubRandom:
    def __init__(self, choice_returns: object) -> None:
        self.captured: list[list[PersonaAgent]] = []
        self._returns = choice_returns

    def choice(self, seq):
        self.captured.append(list(seq))
        if callable(self._returns):
            return self._returns(seq)
        return self._returns


@pytest.mark.asyncio
async def test_shuffle_excludes_last_speaker(personas):
    rng = _StubRandom(choice_returns=lambda seq: seq[0])
    name = await ShuffleStrategy(rng=rng).pick(
        personas=personas, transcript="", trigger_reason="t", last_speaker="beta",
    )
    candidates = rng.captured[0]
    assert all(p.name != "beta" for p in candidates)
    assert {p.name for p in candidates} == {"alpha", "gamma"}
    assert name in {"alpha", "gamma"}


@pytest.mark.asyncio
async def test_shuffle_first_turn_includes_all(personas):
    rng = _StubRandom(choice_returns=lambda seq: seq[0])
    await ShuffleStrategy(rng=rng).pick(
        personas=personas, transcript="", trigger_reason="t", last_speaker=None,
    )
    assert {p.name for p in rng.captured[0]} == {"alpha", "beta", "gamma"}


@pytest.mark.asyncio
async def test_shuffle_single_persona_returns_it():
    only = [_make_persona("solo")]
    rng = _StubRandom(choice_returns=lambda seq: seq[0])
    name = await ShuffleStrategy(rng=rng).pick(
        personas=only, transcript="", trigger_reason="t", last_speaker="solo",
    )
    assert name == "solo"


# ---------------------------------------------------------------------------
# DirectorStrategy
# ---------------------------------------------------------------------------
class _FakeChatStream:
    def __init__(self, content: str) -> None:
        self._content = content

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    def __aiter__(self):
        async def gen():
            for ch in self._content:
                yield _StubChunk(ch)
        return gen()


class _StubDelta:
    def __init__(self, content: str) -> None:
        self.content = content


class _StubChunk:
    def __init__(self, content: str) -> None:
        self.delta = _StubDelta(content)


class _FakeLLM:
    def __init__(self, content: str = '{"speaker":"beta","reason":"x"}') -> None:
        self.content = content
        self.calls = 0

    def chat(self, *, chat_ctx):  # noqa: ARG002
        self.calls += 1
        return _FakeChatStream(self.content)


def _attach_llm(strategy: DirectorStrategy, fake: _FakeLLM) -> None:
    strategy._llm = fake  # bypass lazy init for tests


@pytest.mark.asyncio
async def test_director_returns_llm_pick(personas):
    s = DirectorStrategy()
    _attach_llm(s, _FakeLLM('{"speaker":"beta","reason":"witty"}'))
    name = await s.pick(personas=personas, transcript="", trigger_reason="t", last_speaker=None)
    assert name == "beta"


@pytest.mark.asyncio
async def test_director_unknown_speaker_falls_back(personas):
    s = DirectorStrategy()
    _attach_llm(s, _FakeLLM('{"speaker":"nobody","reason":"x"}'))
    name = await s.pick(
        personas=personas, transcript="", trigger_reason="t", last_speaker="alpha",
    )
    # Fallback (Ordered) advances from alpha → beta.
    assert name == "beta"


@pytest.mark.asyncio
async def test_director_invalid_json_falls_back(personas):
    s = DirectorStrategy()
    _attach_llm(s, _FakeLLM("not json at all"))
    name = await s.pick(
        personas=personas, transcript="", trigger_reason="t", last_speaker="beta",
    )
    assert name == "gamma"  # ordered fallback


@pytest.mark.asyncio
async def test_director_consecutive_cap_falls_back_without_llm(personas):
    s = DirectorStrategy(max_consecutive=2)
    fake = _FakeLLM('{"speaker":"alpha","reason":"x"}')
    _attach_llm(s, fake)

    # Two alpha picks in a row land legitimately…
    s._consecutive_count = 2  # simulate the streak
    name = await s.pick(
        personas=personas, transcript="", trigger_reason="t", last_speaker="alpha",
    )
    # …the third call should NOT touch the LLM and must hand off to ordered.
    assert fake.calls == 0
    assert name == "beta"


@pytest.mark.asyncio
async def test_director_lazy_llm_init_not_constructed_at_init():
    s = DirectorStrategy()
    assert s._llm is None  # never constructed without a pick


@pytest.mark.asyncio
async def test_director_init_failure_is_sticky(personas, monkeypatch):
    s = DirectorStrategy()

    def boom(*_a, **_kw):
        raise RuntimeError("no api key")

    from podcast_commentary.agent import director_strategy as ds_mod
    monkeypatch.setattr(ds_mod.groq, "LLM", boom)

    name1 = await s.pick(personas=personas, transcript="", trigger_reason="t", last_speaker=None)
    name2 = await s.pick(personas=personas, transcript="", trigger_reason="t", last_speaker=name1)
    # Both fall back; init_failed flips after the first attempt and the
    # second pick doesn't even try to construct.
    assert s._init_failed is True
    assert name1 == "alpha"  # ordered fallback first turn
    assert name2 == "beta"   # ordered fallback advances


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_selector_default_mode_is_ordered(personas):
    sel = SpeakerSelector()
    assert sel.mode == "ordered"
    name = await sel.pick(
        personas=personas, transcript="", trigger_reason="t", last_speaker=None,
    )
    assert name == "alpha"


@pytest.mark.asyncio
async def test_selector_set_mode_swaps_active_strategy(personas):
    sel = SpeakerSelector()
    assert await sel.set_mode("shuffle") is True
    assert sel.mode == "shuffle"


@pytest.mark.asyncio
async def test_selector_set_mode_unknown_returns_false(personas):
    sel = SpeakerSelector()
    assert await sel.set_mode("bogus") is False
    assert sel.mode == "ordered"


@pytest.mark.asyncio
async def test_selector_director_constructed_lazily_on_set_mode():
    sel = SpeakerSelector()
    assert "director" not in sel._strategies
    await sel.set_mode("director")
    assert "director" in sel._strategies
    # Second activation must reuse the same instance.
    first = sel._strategies["director"]
    await sel.set_mode("ordered")
    await sel.set_mode("director")
    assert sel._strategies["director"] is first


@pytest.mark.asyncio
async def test_selector_set_mode_atomic_with_concurrent_pick(personas):
    sel = SpeakerSelector()
    # Kick off many picks concurrently with mode swaps. None of them
    # should raise — we're guarding against tearing/None-deref of _active.
    async def picker():
        for _ in range(20):
            await sel.pick(
                personas=personas, transcript="", trigger_reason="t", last_speaker="alpha",
            )

    async def swapper():
        for mode in ("shuffle", "ordered", "shuffle", "ordered"):
            await sel.set_mode(mode)
            await asyncio.sleep(0)

    await asyncio.gather(picker(), picker(), swapper())
