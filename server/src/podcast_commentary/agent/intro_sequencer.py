"""Sequenced intros — strict per-persona state machine, never simultaneous.

Two avatars talking at once sounds broken so the sequence is strictly
serial. Each persona's intro transitions through an explicit
``IntroStatus`` lifecycle:

    PENDING → WAITING_FOR_PRIOR → WAITING_FOR_AVATAR → SPEAKING → DONE

(Or → SKIPPED when its avatar never readied within the startup window.)

The next persona blocks on the prior persona reaching a terminal status
(``DONE`` or ``SKIPPED``) before its own avatar-readiness gate even
fires, so:

  * A persona joining mid-prior-intro waits until the prior persona is
    DONE before speaking.
  * A persona joining before the prior waits for the prior to join,
    intro, and DONE.
  * A persona joining after the prior is DONE proceeds the moment its
    own avatar publishes video.

Before a persona speaks we wait for *its own* avatar to publish its
video track — without that, ``DataStreamIO.capture_frame`` blocks on
``wait_for_track_publication`` and the playout timeout swallows the
intro before audio lands.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from typing import Any

from livekit import rtc

from podcast_commentary.agent.comedian import PersonaAgent
from podcast_commentary.agent.control_channel import ControlChannel
from podcast_commentary.agent.fox_config import CONFIG
from podcast_commentary.agent.playout_waiter import PlayoutWaiter
from podcast_commentary.agent.room_state import RoomState

logger = logging.getLogger("podcast-commentary.intros")


INTRO_PLAYOUT_TIMEOUT = CONFIG.playout.intro_timeout_s


class IntroStatus(enum.Enum):
    """Per-persona intro lifecycle phases.

    The sequencer only allows one persona at ``SPEAKING`` at a time and
    keeps the next persona in ``WAITING_FOR_PRIOR`` until the previous
    reaches a terminal status. ``WAITING_FOR_AVATAR`` is entered *after*
    the prior is terminal, so the second persona can never overlap the
    first even if its avatar published video first.
    """

    PENDING = "pending"
    WAITING_FOR_PRIOR = "waiting_for_prior"
    WAITING_FOR_AVATAR = "waiting_for_avatar"
    SPEAKING = "speaking"
    DONE = "done"
    SKIPPED = "skipped"


_TERMINAL_STATUSES: frozenset[IntroStatus] = frozenset({IntroStatus.DONE, IntroStatus.SKIPPED})


class IntroSequencer:
    """Delivers each persona's intro in declared order, never simultaneously."""

    def __init__(
        self,
        *,
        personas: list[PersonaAgent],
        rooms: dict[str, rtc.Room],
        avatar_identities: dict[str, str],
        room_state: RoomState,
        control: ControlChannel,
        playout_waiter: PlayoutWaiter,
    ) -> None:
        # Each persona owns its own ``rtc.Room`` (dual-room mode). The
        # avatar-readiness gate has to listen on the persona's *own* room
        # — a non-primary persona's avatar publishes video into its
        # secondary room and is invisible from the primary. Watching the
        # wrong room means the wait times out and the intro is silently
        # skipped.
        missing = [p.name for p in personas if p.name not in rooms]
        if missing:
            raise ValueError(f"IntroSequencer missing room mapping for personas: {missing!r}")
        self._personas = personas
        self._rooms = rooms
        self._avatar_identities = avatar_identities
        self._room_state = room_state
        self._control = control
        self._playout = playout_waiter
        self._status: dict[str, IntroStatus] = {p.name: IntroStatus.PENDING for p in personas}
        # Set when a persona reaches a terminal status (``DONE`` or
        # ``SKIPPED``). The next persona awaits this before considering
        # its own avatar-readiness — eliminates the race where a fast
        # second avatar would start its intro while the first persona is
        # still speaking.
        self._terminal_events: dict[str, asyncio.Event] = {
            p.name: asyncio.Event() for p in personas
        }

    def status(self, persona_name: str) -> IntroStatus:
        """Read the current intro status for a persona (testing/debug)."""
        return self._status.get(persona_name, IntroStatus.PENDING)

    async def run(self) -> None:
        """Deliver every intro and unconditionally mark intros_done.

        A failed intro (avatar never readied, ``speak_intro`` returned
        None) still counts as "we tried" so the show doesn't stall —
        commentary paths gate on ``intros_done`` and would otherwise hang.
        """
        try:
            await self._deliver_all()
        finally:
            for persona in self._personas:
                if self._status[persona.name] not in _TERMINAL_STATUSES:
                    self._set_status(persona, IntroStatus.SKIPPED)
            self._room_state.mark_intros_done()
            logger.info("[director] intros complete — commentary path unblocked")

    async def _deliver_all(self) -> None:
        """Walk personas in declared order, transitioning statuses explicitly.

        Edge cases the state machine handles:
          * The next persona's avatar publishes mid-prior-intro → it
            sits in ``WAITING_FOR_PRIOR`` until the prior hits ``DONE``,
            *then* enters ``WAITING_FOR_AVATAR`` (instant fast-path) and
            speaks.
          * The prior's intro finishes before the next persona's avatar
            joins → next transitions to ``WAITING_FOR_AVATAR`` and blocks
            until the video publish event lands.
          * An avatar never connects at all → the per-persona timeout
            fires, status becomes ``SKIPPED``, and the next persona
            proceeds without waiting on the missing avatar.
        """
        prev: PersonaAgent | None = None
        for persona in self._personas:
            if self._room_state.shutting_down:
                return

            if prev is not None:
                self._set_status(persona, IntroStatus.WAITING_FOR_PRIOR)
                if not await self._wait_for_prior_terminal(prev):
                    return

            if not await self._wait_for_own_avatar(persona):
                # Treat as terminal so a third persona (if any) doesn't
                # block forever on this one's missing avatar.
                self._set_status(persona, IntroStatus.SKIPPED)
                prev = persona
                continue

            self._set_status(persona, IntroStatus.SPEAKING)
            try:
                await self._speak_intro_with_timeout(persona)
            finally:
                self._set_status(persona, IntroStatus.DONE)
            prev = persona

    # ------------------------------------------------------------------
    # State-machine helpers
    # ------------------------------------------------------------------
    def _set_status(self, persona: PersonaAgent, status: IntroStatus) -> None:
        old = self._status[persona.name]
        if old is status:
            return
        self._status[persona.name] = status
        logger.info("[intro] %s status: %s → %s", persona.name, old.value, status.value)
        if status in _TERMINAL_STATUSES:
            self._terminal_events[persona.name].set()

    async def _wait_for_prior_terminal(self, prior: PersonaAgent) -> bool:
        """Block until ``prior`` reaches a terminal intro status.

        Returns False if shutdown overtakes the wait — the caller should
        bail out of the sequence in that case so we don't deliver the
        next intro into a torn-down room.
        """
        event = self._terminal_events[prior.name]
        if event.is_set():
            return not self._room_state.shutting_down

        prior_task = asyncio.create_task(event.wait())
        shutdown_task = asyncio.create_task(self._room_state.shutdown_event.wait())
        try:
            done, _ = await asyncio.wait(
                {prior_task, shutdown_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (prior_task, shutdown_task):
                if not t.done():
                    t.cancel()
        if self._room_state.shutting_down:
            return False
        return prior_task in done

    async def _wait_for_own_avatar(self, persona: PersonaAgent) -> bool:
        """Transition into ``WAITING_FOR_AVATAR`` and block on its publish.

        Audio-only personas (no avatar identity registered) skip the
        wait entirely — their session publishes audio directly so there's
        no video-publish handshake to gate on.
        """
        identity = self._avatar_identities.get(persona.name)
        if identity is None:
            return True

        self._set_status(persona, IntroStatus.WAITING_FOR_AVATAR)
        timeout = persona.config.avatar.startup_timeout_s
        room = self._rooms[persona.name]
        ready = await self._wait_for_avatar_ready(identity, room=room, timeout=timeout)
        if not ready and not self._room_state.shutting_down:
            logger.warning(
                "Skipping %s intro — avatar %s not ready within %.0fs",
                persona.name,
                identity,
                timeout,
            )
        return ready

    async def _wait_for_avatar_ready(
        self, identity: str, *, room: rtc.Room, timeout: float
    ) -> bool:
        """Wait until an avatar participant has joined AND published video.

        Publication (not subscription) is what ``DataStreamIO._start_task``
        awaits internally — matching that signal here means the avatar's
        audio path will flow the moment we kick off speech. Returns True
        on ready, False on timeout or shutdown.

        ``room`` is the persona's own ``rtc.Room`` (primary or secondary).
        Each LemonSlice avatar publishes into its own room only, so we
        must watch the matching one — a primary-room listener never sees
        a secondary-room avatar.
        """
        if self._room_state.shutting_down:
            return False

        def has_video(p: Any) -> bool:
            for publication in p.track_publications.values():
                if getattr(publication, "kind", None) == rtc.TrackKind.KIND_VIDEO:
                    return True
            return False

        ready = asyncio.Event()

        def on_participant_connected(p: Any) -> None:
            if p.identity == identity and has_video(p):
                ready.set()

        def on_track_published(publication: Any, p: Any) -> None:
            if (
                p.identity == identity
                and getattr(publication, "kind", None) == rtc.TrackKind.KIND_VIDEO
            ):
                ready.set()

        room.on("participant_connected", on_participant_connected)
        room.on("track_published", on_track_published)
        start = time.monotonic()
        try:
            # Fast path — already joined and published before we attached.
            for p in room.remote_participants.values():
                if p.identity == identity and has_video(p):
                    logger.info(
                        "[avatar-ready] %s fast-path hit (already published)",
                        identity,
                    )
                    return True

            logger.info("[avatar-ready] %s waiting (timeout=%.1fs)", identity, timeout)
            ready_task = asyncio.create_task(ready.wait())
            shutdown_task = asyncio.create_task(self._room_state.shutdown_event.wait())
            try:
                done, _ = await asyncio.wait(
                    {ready_task, shutdown_task},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for t in (ready_task, shutdown_task):
                    if not t.done():
                        t.cancel()
            elapsed = time.monotonic() - start
            if self._room_state.shutting_down:
                logger.info("[avatar-ready] %s abandoned (shutdown)", identity)
                return False
            if ready_task in done and not ready_task.cancelled():
                logger.info("[avatar-ready] %s ready after %.2fs", identity, elapsed)
                return True
            logger.error(
                "[avatar-ready] %s NOT READY after %.2fs — intro will be skipped",
                identity,
                elapsed,
            )
            return False
        finally:
            room.off("participant_connected", on_participant_connected)
            room.off("track_published", on_track_published)

    async def _speak_intro_with_timeout(self, persona: PersonaAgent) -> None:
        """Deliver one persona's intro with a hard upper bound.

        Tags the start/end packets with ``phase: "intro"`` so the client
        forces its Skip button disabled during intros — belt-and-
        suspenders on top of the server-side ``SkipCoordinator`` filter.
        """
        logger.info("[intro] %s BEGIN", persona.name)
        start = time.monotonic()
        await self._control.publish_commentary_start(persona.name, phase="intro")
        try:
            handle = persona.speak_intro()
            if handle is None:
                logger.warning(
                    "[intro] %s aborted — speak_intro returned None (session closed before speak)",
                    persona.name,
                )
                return
            await self._playout.wait(persona, handle, timeout=INTRO_PLAYOUT_TIMEOUT, label="intro")
        finally:
            elapsed = time.monotonic() - start
            logger.info("[intro] %s END (elapsed=%.2fs)", persona.name, elapsed)
            await self._control.publish_commentary_end(persona.name, phase="intro")


__all__ = ["IntroSequencer", "IntroStatus", "INTRO_PLAYOUT_TIMEOUT"]
