"""Director constructor + per-persona room/session plumbing.

These tests exercise just the construction graph and the read-side
helpers; they don't drive the actual show (no avatar, no STT, no LLM
calls). The risk we're protecting is that the per-persona refactor
either silently picks the wrong room for the user-facing data channel,
or misroutes ``RoomState`` so it reports LISTENING with one persona
still mid-utterance.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import pytest

from podcast_commentary.agent.comedian import FoxPhase, PersonaAgent
from podcast_commentary.agent.director import Director, PersonaContext

from ._stub_config import make_stub_config


@pytest.fixture(autouse=True)
def _stub_groq_key(monkeypatch):
    """SpeakerSelector instantiates a ``groq.LLM`` at construction; that
    requires ``GROQ_API_KEY``. Set a stub so tests don't need network.
    """
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-used")


class _FakeRoom:
    """Minimal stand-in for ``rtc.Room`` — only what Director touches.

    Director construction is the surface under test, so we capture the
    handler-attach calls without actually firing any events.
    """

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.handlers: dict[str, list[Any]] = defaultdict(list)
        self.remote_participants: dict[str, Any] = {}
        self.local_participant = None

    def on(self, event: str, fn=None):
        if fn is not None:
            self.handlers[event].append(fn)
            return fn

        def deco(handler):
            self.handlers[event].append(handler)
            return handler

        return deco

    def off(self, event: str, fn) -> None:
        try:
            self.handlers.get(event, []).remove(fn)
        except ValueError:
            pass


class _FakeSession:
    """Stand-in for ``AgentSession``."""


def _make_persona(name: str) -> PersonaAgent:
    return PersonaAgent(config=make_stub_config(name))


def _make_director(*persona_names: str) -> tuple[Director, list[PersonaAgent], list[_FakeRoom]]:
    personas = [_make_persona(n) for n in persona_names]
    rooms = [_FakeRoom(f"{n}-room") for n in persona_names]
    contexts = [
        PersonaContext(persona=p, room=r, session=_FakeSession())
        for p, r in zip(personas, rooms, strict=True)
    ]
    director = Director(personas=contexts)
    return director, personas, rooms


def test_director_constructs_with_two_persona_contexts():
    """Smoke: constructor accepts a list of PersonaContext triples and
    materialises every component. Bare assertion to catch any TypeError
    or NameError introduced by the refactor.
    """
    director, personas, rooms = _make_director("persona_a", "persona_b")
    assert director._personas == personas
    assert {p.name for p in director._personas} == {personas[0].name, personas[1].name}
    # First-context's room is the user-facing primary; verify the helpers
    # surface it for downstream callers.
    assert director._primary_room is rooms[0]


def test_director_rejects_empty_persona_list():
    with pytest.raises(ValueError):
        Director(personas=[])


def test_room_state_listening_only_when_all_personas_listening():
    """RoomState must wait for *every* persona to report LISTENING.

    With two personas the predicate is False if either is mid-utterance —
    regression target: a refactor that read only the primary persona's
    phase would let the silence loop fire while the secondary was still
    speaking.
    """
    director, personas, _rooms = _make_director("persona_a", "persona_b")
    primary, secondary = personas
    state = director._room_state

    # intros_done is the first gate; flip it on so phase becomes the
    # only signal under test.
    state.mark_intros_done()
    # Both default to LISTENING after PersonaAgent construction — the
    # predicate should already be True.
    assert primary.phase is FoxPhase.LISTENING
    assert secondary.phase is FoxPhase.LISTENING
    assert state.is_listening() is True

    # One persona enters COMMENTATING (via a legal transition) — the
    # room is no longer "all listening".
    primary._set_phase(FoxPhase.COMMENTATING)
    assert state.is_listening() is False

    # Bring primary back; secondary now drops out — still not "all listening".
    primary._set_phase(FoxPhase.LISTENING)
    secondary._set_phase(FoxPhase.COMMENTATING)
    assert state.is_listening() is False

    # Both back to LISTENING — predicate flips back to True.
    secondary._set_phase(FoxPhase.LISTENING)
    assert state.is_listening() is True


def test_room_state_blocks_until_intros_done():
    """Even when every persona is LISTENING, the room is not 'listening'
    until ``intros_done`` is set. Pinned because the silence loop and
    sentence trigger gate on ``RoomState.is_listening`` and would fire a
    punchline INSTEAD of the first intro otherwise.
    """
    director, personas, _ = _make_director("persona_a", "persona_b")
    state = director._room_state
    assert all(p.phase is FoxPhase.LISTENING for p in personas)
    assert state.is_listening() is False  # intros_done not yet set
    state.mark_intros_done()
    assert state.is_listening() is True


def test_director_routes_per_persona_room_lookups():
    """Director keeps a per-persona room lookup so dual-room callers
    (intro-readiness, future avatar gates) can resolve the right room.
    """
    director, personas, rooms = _make_director("persona_a", "persona_b")
    primary, secondary = personas

    assert director._room_for(primary) is rooms[0]
    assert director._room_for(secondary) is rooms[1]
    assert director._session_for(primary) is not None
    assert director._session_for(secondary) is not None
    # Different sessions per persona — refactor must not collapse them.
    assert director._session_for(primary) is not director._session_for(secondary)
