"""Unit tests for ``PlayoutWaiter`` — simple ``wait_for_playout`` path.

* handle resolves before the timeout → ``outcome=ok``
* handle hangs past the timeout → ``outcome=timeout`` and the per-call
  counter increments
* no other outcome series is ever incremented.
"""

from __future__ import annotations

import asyncio

import pytest

from podcast_commentary.agent.metrics import playout_finished_rpc_total
from podcast_commentary.agent.playout_waiter import PlayoutWaiter


pytestmark = [pytest.mark.asyncio]


class _FakePersona:
    """Minimal persona stand-in — only ``name`` is read by ``PlayoutWaiter``."""

    def __init__(self, name: str = "fox") -> None:
        self.name = name


class _ResolvingHandle:
    """SpeechHandle stand-in that resolves after ``delay`` seconds."""

    def __init__(self, *, delay: float = 0.0) -> None:
        self._delay = delay

    async def wait_for_playout(self) -> None:
        if self._delay:
            await asyncio.sleep(self._delay)


class _HangingHandle:
    """SpeechHandle stand-in whose ``wait_for_playout`` never resolves."""

    async def wait_for_playout(self) -> None:
        await asyncio.Event().wait()


def _ok_count(persona: str) -> float:
    return playout_finished_rpc_total.snapshot().get((persona, "ok"), 0.0)


def _timeout_count(persona: str) -> float:
    return playout_finished_rpc_total.snapshot().get((persona, "timeout"), 0.0)


def _fallback_count(persona: str) -> float:
    return playout_finished_rpc_total.snapshot().get((persona, "fallback"), 0.0)


async def test_wait_returns_when_handle_resolves() -> None:
    waiter = PlayoutWaiter()
    persona = _FakePersona("fox-ok")
    before_ok = _ok_count(persona.name)
    before_timeout = _timeout_count(persona.name)

    await waiter.wait(persona, _ResolvingHandle(), timeout=1.0, label="commentary")

    assert _ok_count(persona.name) == before_ok + 1
    assert _timeout_count(persona.name) == before_timeout
    assert waiter.timeout_count == 0


async def test_wait_records_timeout_when_handle_hangs() -> None:
    waiter = PlayoutWaiter()
    persona = _FakePersona("fox-timeout")
    before_timeout = _timeout_count(persona.name)
    before_ok = _ok_count(persona.name)

    await waiter.wait(persona, _HangingHandle(), timeout=0.05, label="intro")

    assert _timeout_count(persona.name) == before_timeout + 1
    assert _ok_count(persona.name) == before_ok
    assert waiter.timeout_count == 1


async def test_no_fallback_outcome_is_ever_emitted() -> None:
    """Neither success nor timeout should increment the
    ``outcome=fallback`` series — only ``ok`` and ``timeout`` are valid
    outcomes."""
    waiter = PlayoutWaiter()
    persona = _FakePersona("fox-no-fallback")
    before = _fallback_count(persona.name)

    await waiter.wait(persona, _ResolvingHandle(), timeout=1.0, label="commentary")
    await waiter.wait(persona, _HangingHandle(), timeout=0.05, label="commentary")

    assert _fallback_count(persona.name) == before


async def test_attach_observers_handles_missing_audio_output() -> None:
    """Personas with no live audio output are skipped silently."""

    class _PersonaNoAudio:
        name = "no-audio"

        def _audio_output(self) -> None:
            return None

    PlayoutWaiter.attach_observers([_PersonaNoAudio()])  # must not raise


async def test_attach_observers_subscribes_when_audio_output_present() -> None:
    """When a persona exposes an audio output, the ``playback_finished``
    handler is registered on it via ``on``."""

    class _Audio:
        def __init__(self) -> None:
            self.handlers: dict[str, list] = {}

        def on(self, event: str, fn) -> None:
            self.handlers.setdefault(event, []).append(fn)

    class _Persona:
        name = "fox"

        def __init__(self, audio: _Audio) -> None:
            self._audio = audio

        def _audio_output(self) -> _Audio:
            return self._audio

    audio = _Audio()
    PlayoutWaiter.attach_observers([_Persona(audio)])

    assert "playback_finished" in audio.handlers
    assert len(audio.handlers["playback_finished"]) == 1
