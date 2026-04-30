"""Co-speaker context tests — core regression risk.

The co-speaker context (a persona's prompt sees the OTHER persona's
recent commentary) is the bit most likely to silently break when the
Director is refactored to use per-persona PersonaContext triples. These
tests pin the data flow:

  * Director must build a CommentaryPipeline whose ``_co_speaker_view``
    returns the *other* persona's last lines.
  * The assembled commentary prompt (``build_commentary_request``) must
    include the co-speaker's last 3 lines under a ``[WHAT <NAME> JUST
    SAID...]`` block. The prompt-assembly code in ``prompts.py`` is
    held constant; only the data-source plumbing changes.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import pytest

from podcast_commentary.agent.comedian import PersonaAgent
from podcast_commentary.agent.director import Director, PersonaContext
from podcast_commentary.agent.prompts import build_commentary_request

from ._stub_config import make_stub_config


@pytest.fixture(autouse=True)
def _stub_groq_key(monkeypatch):
    """SpeakerSelector instantiates ``groq.LLM`` at construction time —
    that requires ``GROQ_API_KEY`` even though the test never makes a
    network call. Stub a value so the constructor succeeds.
    """
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-used")


class _FakeRoom:
    """Stand-in for ``rtc.Room`` — only needs to be passable as an arg."""

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self._handlers: dict[str, list[Any]] = defaultdict(list)
        self.remote_participants: dict[str, Any] = {}
        self.local_participant = None

    def on(self, event: str, fn=None):
        if fn is not None:
            self._handlers[event].append(fn)
            return fn

        def deco(handler):
            self._handlers[event].append(handler)
            return handler

        return deco

    def off(self, event: str, fn) -> None:
        try:
            self._handlers.get(event, []).remove(fn)
        except ValueError:
            pass


class _FakeSession:
    """Stand-in for ``AgentSession`` — Director never reaches into it."""


def _make_persona(name: str, *, label: str | None = None) -> PersonaAgent:
    """Build a PersonaAgent without going through ``session.start``.

    The Agent base class only needs ``instructions=...``; we feed it
    a stub ``FoxConfig`` with no ties to any shipped preset.
    """
    return PersonaAgent(config=make_stub_config(name, label=label))


def test_co_speaker_view_returns_other_personas_history():
    """``CommentaryPipeline._co_speaker_view(p)`` must return the OTHER persona's
    history + label, not ``p``'s own. Regression target: a refactor that
    looked up ``persona.commentary_history`` instead of the co-speaker's
    would silently feed each persona its own context.
    """
    a = _make_persona("persona_a")
    b = _make_persona("persona_b")

    a._commentary_history.extend(["a-1", "a-2", "a-3"])
    b._commentary_history.extend(["b-1", "b-2", "b-3"])

    director = Director(
        personas=[
            PersonaContext(persona=a, room=_FakeRoom("a-room"), session=_FakeSession()),
            PersonaContext(persona=b, room=_FakeRoom("b-room"), session=_FakeSession()),
        ],
    )

    a_view = director._pipeline._co_speaker_view(a)
    b_view = director._pipeline._co_speaker_view(b)

    assert a_view == (["b-1", "b-2", "b-3"], b.label)
    assert b_view == (["a-1", "a-2", "a-3"], a.label)


def test_persona_prompt_includes_co_speakers_last_three_lines():
    """End-to-end pin of the co-speaker prompt block.

    Pulls the co-speaker history from a 2-persona Director and feeds it
    into ``build_commentary_request``. Each persona's prompt must contain
    the other's last 3 lines. ``prompts.py`` defines the label header
    (``[WHAT <NAME> JUST SAID...]``); we assert against it so a
    renamed/dropped block fails this test loudly.
    """
    a = _make_persona("persona_a")
    b = _make_persona("persona_b")

    a._commentary_history.extend(["a-old", "a-1", "a-2", "a-3"])
    b._commentary_history.extend(["b-old", "b-1", "b-2", "b-3"])

    director = Director(
        personas=[
            PersonaContext(persona=a, room=_FakeRoom(), session=_FakeSession()),
            PersonaContext(persona=b, room=_FakeRoom(), session=_FakeSession()),
        ],
    )

    a_co_history, a_co_label = director._pipeline._co_speaker_view(a)
    b_co_history, b_co_label = director._pipeline._co_speaker_view(b)

    a_prompt = build_commentary_request(
        config=a.config,
        recent_transcript="something happened in the video",
        commentary_history=a.commentary_history,
        trigger_reason="test trigger",
        angle="test_angle",
        co_speaker_history=a_co_history,
        co_speaker_label=a_co_label,
    )
    b_prompt = build_commentary_request(
        config=b.config,
        recent_transcript="something happened in the video",
        commentary_history=b.commentary_history,
        trigger_reason="test trigger",
        angle="test_angle",
        co_speaker_history=b_co_history,
        co_speaker_label=b_co_label,
    )

    # A sees B's most-recent lines, not its own, in the co-speaker block.
    assert f"[WHAT {b.label.upper()} JUST SAID" in a_prompt
    for expected in ("b-1", "b-2", "b-3"):
        assert expected in a_prompt, f"missing {expected!r} in persona_a prompt"
    # And vice versa.
    assert f"[WHAT {a.label.upper()} JUST SAID" in b_prompt
    for expected in ("a-1", "a-2", "a-3"):
        assert expected in b_prompt, f"missing {expected!r} in persona_b prompt"
