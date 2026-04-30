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
from podcast_commentary.agent.fox_config import load_config
from podcast_commentary.agent.prompts import build_commentary_request


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


def _make_persona(name: str) -> PersonaAgent:
    """Build a PersonaAgent without going through ``session.start``.

    The Agent base class only needs ``instructions=...``; we feed it
    ``config.persona.system_prompt`` and call ``__init__`` directly so
    no LiveKit session, room, or model is required.
    """
    config = load_config(name)
    return PersonaAgent(config=config)


def test_co_speaker_view_returns_other_personas_history():
    """``CommentaryPipeline._co_speaker_view(p)`` must return the OTHER persona's
    history + label, not ``p``'s own. Regression target: a refactor that
    looked up ``persona.commentary_history`` instead of the co-speaker's
    would silently feed each persona its own context.
    """
    fox = _make_persona("fox")
    alien = _make_persona("chaos_agent")

    fox._commentary_history.extend(["fox A", "fox B", "fox C"])
    alien._commentary_history.extend(["alien X", "alien Y", "alien Z"])

    director = Director(
        personas=[
            PersonaContext(persona=fox, room=_FakeRoom("fox-room"), session=_FakeSession()),
            PersonaContext(persona=alien, room=_FakeRoom("alien-room"), session=_FakeSession()),
        ],
    )

    fox_view = director._pipeline._co_speaker_view(fox)
    alien_view = director._pipeline._co_speaker_view(alien)

    assert fox_view == (["alien X", "alien Y", "alien Z"], alien.label)
    assert alien_view == (["fox A", "fox B", "fox C"], fox.label)


def test_fox_prompt_includes_aliens_last_three_lines():
    """End-to-end pin of the co-speaker prompt block.

    Pulls the co-speaker history from a 2-persona Director and feeds it
    into ``build_commentary_request``. Fox's prompt must contain Alien's
    last 3 lines; Alien's must contain Fox's. ``prompts.py`` defines the
    label header (``[WHAT <NAME> JUST SAID...]``); we assert against it
    so a renamed/dropped block fails this test loudly.
    """
    fox = _make_persona("fox")
    alien = _make_persona("chaos_agent")

    fox._commentary_history.extend(["fox-old line", "fox A", "fox B", "fox C"])
    alien._commentary_history.extend(["alien-old line", "alien X", "alien Y", "alien Z"])

    director = Director(
        personas=[
            PersonaContext(persona=fox, room=_FakeRoom(), session=_FakeSession()),
            PersonaContext(persona=alien, room=_FakeRoom(), session=_FakeSession()),
        ],
    )

    fox_co_history, fox_co_label = director._pipeline._co_speaker_view(fox)
    alien_co_history, alien_co_label = director._pipeline._co_speaker_view(alien)

    fox_prompt = build_commentary_request(
        config=fox.config,
        recent_transcript="something happened in the video",
        commentary_history=fox.commentary_history,
        trigger_reason="test trigger",
        angle="test_angle",
        co_speaker_history=fox_co_history,
        co_speaker_label=fox_co_label,
    )
    alien_prompt = build_commentary_request(
        config=alien.config,
        recent_transcript="something happened in the video",
        commentary_history=alien.commentary_history,
        trigger_reason="test trigger",
        angle="test_angle",
        co_speaker_history=alien_co_history,
        co_speaker_label=alien_co_label,
    )

    # Fox sees Alien's most-recent lines, NOT his own, in the co-speaker block.
    assert f"[WHAT {alien.label.upper()} JUST SAID" in fox_prompt
    for expected in ("alien X", "alien Y", "alien Z"):
        assert expected in fox_prompt, f"missing {expected!r} in Fox prompt"
    # And vice versa for Alien.
    assert f"[WHAT {fox.label.upper()} JUST SAID" in alien_prompt
    for expected in ("fox A", "fox B", "fox C"):
        assert expected in alien_prompt, f"missing {expected!r} in Alien prompt"
