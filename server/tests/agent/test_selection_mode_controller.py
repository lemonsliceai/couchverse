"""SelectionModeController tests + Director inbound-event dispatch."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import pytest

from podcast_commentary.agent.comedian import PersonaAgent
from podcast_commentary.agent.director import Director, PersonaContext
from podcast_commentary.agent.selection_mode_controller import SelectionModeController
from podcast_commentary.agent.selector import SpeakerSelector

from ._stub_config import make_stub_config


class _FakeRoom:
    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.handlers: dict[str, list[Any]] = defaultdict(list)
        self.remote_participants: dict[str, Any] = {}
        self.local_participant = None

    def on(self, event, fn=None):
        if fn is not None:
            self.handlers[event].append(fn)
            return fn

        def deco(handler):
            self.handlers[event].append(handler)
            return handler

        return deco

    def off(self, event, fn) -> None:
        try:
            self.handlers.get(event, []).remove(fn)
        except ValueError:
            pass


class _FakeSession:
    pass


def _make_director() -> Director:
    personas = [
        PersonaAgent(config=make_stub_config("alpha")),
        PersonaAgent(config=make_stub_config("beta")),
    ]
    return Director(
        personas=[
            PersonaContext(persona=personas[0], room=_FakeRoom("a"), session=_FakeSession()),
            PersonaContext(persona=personas[1], room=_FakeRoom("b"), session=_FakeSession()),
        ],
    )


# ---------------------------------------------------------------------------
# SelectionModeController
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_set_mode_swaps_selector():
    sel = SpeakerSelector()
    ctrl = SelectionModeController(selector=sel)
    await ctrl.set_mode("shuffle")
    assert sel.mode == "shuffle"


@pytest.mark.asyncio
async def test_set_mode_none_is_noop():
    sel = SpeakerSelector()
    ctrl = SelectionModeController(selector=sel)
    await ctrl.set_mode(None)
    assert sel.mode == "ordered"


@pytest.mark.asyncio
async def test_set_mode_unknown_keeps_current():
    sel = SpeakerSelector()
    ctrl = SelectionModeController(selector=sel)
    await ctrl.set_mode("bogus")
    assert sel.mode == "ordered"


# ---------------------------------------------------------------------------
# Director.handle_selection_mode
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_director_handles_selection_mode_event():
    director = _make_director()
    director._handle_selection_mode({"type": "selection_mode", "mode": "shuffle"})
    # _handle_selection_mode dispatches via fire_and_forget; let the task run.
    pending = [t for t in director._tasks._tasks if not t.done()]
    if pending:
        await pending[0]
    assert director._selector.mode == "shuffle"


@pytest.mark.asyncio
async def test_director_ignores_unknown_mode():
    director = _make_director()
    director._handle_selection_mode({"type": "selection_mode", "mode": "bogus"})
    pending = [t for t in director._tasks._tasks if not t.done()]
    if pending:
        await pending[0]
    assert director._selector.mode == "ordered"


@pytest.mark.asyncio
async def test_director_missing_mode_field_is_safe():
    director = _make_director()
    director._handle_selection_mode({"type": "selection_mode"})
    pending = [t for t in director._tasks._tasks if not t.done()]
    if pending:
        await pending[0]
    assert director._selector.mode == "ordered"
