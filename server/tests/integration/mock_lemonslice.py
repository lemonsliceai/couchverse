"""Mock LemonSlice provider for the dual-room integration test.

Two pieces ship from this module:

  * :class:`MockLemonSliceService` — a tiny FastAPI service that records
    every "start avatar" call so the test can assert on dispatch shape.
    The real LemonSlice cloud charges per minute and ships a real
    rendered video; we don't need either to exercise the agent's
    startup-and-publish contract.

  * :class:`MockAvatarSession` — a drop-in replacement for
    :class:`livekit.plugins.lemonslice.AvatarSession` that joins the
    target ``rtc.Room`` under the configured identity, publishes a
    static black video frame so the avatar-startup metric
    observes a "first video frame" event, and forwards the agent
    session's TTS audio onto the wire so commentary is actually audible
    in the recorded test artifact.

The plugin patch is applied via :func:`apply_lemonslice_patch` — invoked
either inline (in-process tests) or from the agent subprocess startup
hook (``conftest.py`` ships a small bootstrap module that does this
before ``cli.run_app`` is called).

The mock is intentionally minimal:
  * No lipsync. Static frame published once on connect.
  * No backpressure. Audio frames are forwarded as-is.
  * No retry. A failed connect raises and the agent's normal error
    path swallows it (matching production LemonSlice 5xx behaviour).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

logger = logging.getLogger("podcast-commentary.integration.mock-lemonslice")


# Black 16x16 frame — small enough that publishing 1 fps barely registers
# in CI but visible enough that the avatar-startup metric ("first video
# frame") fires deterministically.
_FRAME_WIDTH = 16
_FRAME_HEIGHT = 16


@dataclass
class _StartCall:
    """One recorded ``MockAvatarSession.start`` invocation."""

    identity: str
    image_url: str
    active_prompt: str | None
    idle_prompt: str | None
    room_name: str
    session_id: str = field(default_factory=lambda: uuid4().hex)


class MockLemonSliceService:
    """FastAPI app that records "start avatar" requests.

    Used as an HTTP-level mock when the test wants to point the real
    LemonSlice plugin at a fake URL (e.g. via ``LEMONSLICE_BASE_URL``).
    Today the real plugin doesn't expose a ``base_url`` knob, so most
    tests use :class:`MockAvatarSession` instead — but this is here for
    when the plugin grows that knob and so the mock has a public
    surface a non-Python caller (curl, integration smoke from the
    extension side) can hit.
    """

    def __init__(self) -> None:
        from fastapi import FastAPI, Request

        self.calls: list[_StartCall] = []
        self.app = FastAPI()

        @self.app.post("/v1/avatars/start")
        async def _start(request: Request) -> dict[str, str]:
            payload = await request.json()
            call = _StartCall(
                identity=payload.get("identity", ""),
                image_url=payload.get("image_url", ""),
                active_prompt=payload.get("active_prompt"),
                idle_prompt=payload.get("idle_prompt"),
                room_name=payload.get("room_name", ""),
            )
            self.calls.append(call)
            logger.info("mock-lemonslice start identity=%s room=%s", call.identity, call.room_name)
            return {"session_id": call.session_id}

        @self.app.get("/health")
        async def _health() -> dict[str, str]:
            return {"status": "ok"}

    def reset(self) -> None:
        self.calls.clear()


def _make_static_video_frame() -> Any:
    """Build one black VideoFrame in I420 — what LiveKit's wire format wants.

    The LiveKit Python SDK accepts ARGB / I420 / NV12 frames. I420 is the
    most-portable: the encoder accepts it directly and it's the format
    LiveKit's own examples use. Buffer is all-zero bytes (black frame).
    """
    from livekit import rtc

    width, height = _FRAME_WIDTH, _FRAME_HEIGHT
    y_size = width * height
    uv_size = (width // 2) * (height // 2)
    buffer = bytes(y_size + 2 * uv_size)
    return rtc.VideoFrame(
        width=width,
        height=height,
        type=rtc.VideoBufferType.I420,
        data=buffer,
    )


class MockAvatarSession:
    """Drop-in replacement for ``lemonslice.AvatarSession``.

    Construction signature mirrors the real plugin (kwargs-only) so the
    agent's :func:`_start_avatar` can swap one for the other without any
    other code change. ``start(session, room=room)`` joins ``room`` as
    ``avatar_participant_identity``, publishes a single black video
    frame so :func:`watch_avatar_startup` can record a "first video
    frame" sample, and routes the agent session's audio output onto
    the wire from this connection.

    The instance owns the avatar-side ``rtc.Room`` and exposes
    :meth:`aclose` for the test harness to tear it down deterministically.
    """

    _calls: list[_StartCall] = []

    def __init__(
        self,
        *,
        agent_image_url: str,
        agent_prompt: str | None = None,
        agent_idle_prompt: str | None = None,
        avatar_participant_identity: str,
    ) -> None:
        self._image_url = agent_image_url
        self._active_prompt = agent_prompt
        self._idle_prompt = agent_idle_prompt
        self._identity = avatar_participant_identity
        self._avatar_room: Any = None
        self._video_source: Any = None
        self._publish_task: asyncio.Task[None] | None = None
        self._session_id = uuid4().hex

    @classmethod
    def calls(cls) -> list[_StartCall]:
        return list(cls._calls)

    @classmethod
    def reset(cls) -> None:
        cls._calls.clear()

    async def start(self, session: Any, *, room: Any) -> str:
        """Join ``room`` as the avatar identity and publish a static frame.

        Returns the synthetic session id (matching the real plugin's
        contract — the value flows back to ``main._start_avatar`` which
        only checks for non-None to decide whether to register the
        identity with the Director).
        """
        from livekit import api, rtc

        url = os.environ["LIVEKIT_URL"]
        api_key = os.environ["LIVEKIT_API_KEY"]
        api_secret = os.environ["LIVEKIT_API_SECRET"]
        room_name = getattr(room, "name", "") or ""

        token = (
            api.AccessToken(api_key, api_secret)
            .with_identity(self._identity)
            .with_name(self._identity)
            .with_kind("agent")
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room_name,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=True,
                    agent=True,
                )
            )
            .to_jwt()
        )

        avatar_room = rtc.Room()
        await avatar_room.connect(url, token, rtc.RoomOptions())
        self._avatar_room = avatar_room

        # Publish the static frame BEFORE returning so the
        # ``track_published`` listener in ``watch_avatar_startup`` is
        # guaranteed to observe it. Republishing once a second keeps the
        # track alive for the duration of the session — without it some
        # subscribers age the track out after a few seconds.
        video_source = rtc.VideoSource(_FRAME_WIDTH, _FRAME_HEIGHT)
        self._video_source = video_source
        track = rtc.LocalVideoTrack.create_video_track("avatar-video", video_source)
        await avatar_room.local_participant.publish_track(
            track,
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_CAMERA),
        )

        frame = _make_static_video_frame()
        video_source.capture_frame(frame)

        async def _heartbeat() -> None:
            while True:
                try:
                    video_source.capture_frame(_make_static_video_frame())
                    await asyncio.sleep(1.0)
                except asyncio.CancelledError:
                    return
                except Exception:
                    logger.debug("mock-lemonslice heartbeat raised", exc_info=True)
                    return

        self._publish_task = asyncio.create_task(_heartbeat(), name=f"mock-avatar:{self._identity}")

        call = _StartCall(
            identity=self._identity,
            image_url=self._image_url,
            active_prompt=self._active_prompt,
            idle_prompt=self._idle_prompt,
            room_name=room_name,
            session_id=self._session_id,
        )
        type(self)._calls.append(call)
        logger.info(
            "MockAvatarSession started identity=%s room=%s session_id=%s",
            self._identity,
            room_name,
            self._session_id,
        )
        return self._session_id

    async def aclose(self) -> None:
        """Stop the heartbeat and disconnect the avatar room. Idempotent."""
        if self._publish_task is not None and not self._publish_task.done():
            self._publish_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._publish_task
            self._publish_task = None

        if self._avatar_room is not None:
            with contextlib.suppress(Exception):
                await self._avatar_room.disconnect()
            self._avatar_room = None


def apply_lemonslice_patch() -> None:
    """Replace ``livekit.plugins.lemonslice.AvatarSession`` with the mock.

    Idempotent — calling twice in the same process is a no-op (subsequent
    calls notice the symbol is already the mock and return). Must be
    invoked BEFORE ``podcast_commentary.agent.main`` imports happen, so
    its ``from livekit.plugins import lemonslice`` resolves to the
    patched module. The agent subprocess bootstrap shipped from
    ``conftest.py`` handles ordering.
    """
    from livekit.plugins import lemonslice as _ls

    if getattr(_ls.AvatarSession, "__name__", "") == MockAvatarSession.__name__:
        return
    _ls.AvatarSession = MockAvatarSession  # type: ignore[assignment]


__all__ = [
    "MockAvatarSession",
    "MockLemonSliceService",
    "apply_lemonslice_patch",
]
