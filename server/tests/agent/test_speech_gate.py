"""Unit tests for ``SpeechGate`` — graceful no-op when the session is gone.

The framework raises a bare ``RuntimeError`` from ``generate_reply()`` /
``say()`` in two distinct lifecycle states:

  * ``AgentSession isn't running``         (not started yet)
  * ``AgentSession is closing, cannot ...`` (mid-shutdown)

Both must be swallowed by the gate so the Director's silence loop and the
intro sequencer don't crash on a benign "session already gone" race — the
canonical case being a dev-mode hot reload that tears down the worker
between "decide to speak" and "session.say".
"""

from __future__ import annotations

import pytest

from podcast_commentary.agent.speech_gate import SpeechGate


class _ExplodingSession:
    """``AgentSession`` stand-in whose entry points raise a configured error."""

    def __init__(self, message: str) -> None:
        self._message = message

    def generate_reply(self, **_kwargs: object) -> object:
        raise RuntimeError(self._message)

    def say(self, *_args: object, **_kwargs: object) -> object:
        raise RuntimeError(self._message)


@pytest.mark.parametrize(
    "message",
    [
        "AgentSession isn't running",
        "AgentSession is closing, cannot use say()",
        "AgentSession is closing, cannot use generate_reply()",
    ],
)
def test_speak_returns_none_when_session_unavailable(message: str) -> None:
    gate = SpeechGate(_ExplodingSession(message), name="cat_girl")  # type: ignore[arg-type]

    assert gate.speak(prompt="hi") is None
    assert gate.is_speaking is False


@pytest.mark.parametrize(
    "message",
    [
        "AgentSession isn't running",
        "AgentSession is closing, cannot use say()",
        "AgentSession is closing, cannot use generate_reply()",
    ],
)
def test_say_returns_none_when_session_unavailable(message: str) -> None:
    gate = SpeechGate(_ExplodingSession(message), name="david_sacks")  # type: ignore[arg-type]

    assert gate.say(text="hello") is None
    assert gate.is_speaking is False


def test_speak_propagates_unrelated_runtime_error() -> None:
    gate = SpeechGate(_ExplodingSession("something else entirely"), name="alien")  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="something else entirely"):
        gate.speak(prompt="hi")


def test_say_propagates_unrelated_runtime_error() -> None:
    gate = SpeechGate(_ExplodingSession("something else entirely"), name="alien")  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="something else entirely"):
        gate.say(text="hello")
