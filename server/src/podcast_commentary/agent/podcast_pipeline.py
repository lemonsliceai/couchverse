"""Podcast STT + player lifecycle.

Bundles the collaborators that together make up "Fox's ears":

  * `groq.STT` — non-streaming Whisper called on fixed-interval audio chunks.
    Podcast speech is near-continuous with few natural pauses, so VAD-based
    segmentation (the previous approach) would buffer 30-60 s before emitting
    a transcript.  Fixed-interval chunking (~10 s) gives predictable,
    frequent delivery regardless of speech patterns.
  * `PodcastPlayer` — an ffmpeg subprocess decoding the YouTube audio URL
    into 16 kHz mono PCM and pushing frames into a buffer.

Two modes:

  * **Server mode** (default): ffmpeg decodes the YouTube audio URL. Used by
    the web app path where the agent extracts audio via yt-dlp.
  * **Browser mode** (``audio_source="browser"``): The Chrome extension
    captures tab audio and publishes it as a LiveKit track named
    ``podcast-audio``. The pipeline subscribes to that track instead of
    running ffmpeg. No yt-dlp, no proxy, no IP-pinning needed.

Factored out of `ComedianAgent` so the pipeline owns its own startup,
recognition loop, and shutdown — the agent just wires up the callback.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from livekit import rtc
from livekit.plugins import groq

from podcast_commentary.agent.fox_config import CONFIG
from podcast_commentary.agent.podcast_player import PodcastPlayer

logger = logging.getLogger("podcast-commentary.podcast_pipeline")

# How often to send accumulated audio to Whisper for transcription.
# 10 s ≈ 2-3 sentences at typical speaking pace; after two chunks Fox
# has enough material (~5 sentences) to trigger commentary. Sourced from
# the active FoxConfig preset.
CHUNK_INTERVAL_SECONDS = CONFIG.timing.transcript_chunk_s


class _FrameBuffer:
    """Collects audio frames for periodic batch STT recognition.

    Exposes ``push_frame`` so PodcastPlayer can treat it identically to a
    ``RecognizeStream`` without knowing about the chunking strategy.
    """

    def __init__(self) -> None:
        self._frames: list[rtc.AudioFrame] = []

    def push_frame(self, frame: rtc.AudioFrame) -> None:
        self._frames.append(frame)

    def drain(self) -> list[rtc.AudioFrame]:
        """Return all buffered frames and reset."""
        frames = self._frames
        self._frames = []
        return frames


class PodcastPipeline:
    """Fixed-interval STT + ffmpeg player + transcript delivery.

    In **browser mode** (``audio_source="browser"``), the ffmpeg player is
    not created.  Instead, ``attach_browser_track()`` subscribes to a
    LiveKit audio track published by the Chrome extension and feeds frames
    directly into the recognition buffer.
    """

    def __init__(
        self,
        *,
        audio_url: str | None = None,
        audio_source: str = "server",
        on_transcript: Callable[[str], Awaitable[None]],
        proxy: str | None = None,
    ) -> None:
        self._audio_url = audio_url
        self._audio_source = audio_source
        self._on_transcript = on_transcript
        self._proxy = proxy
        self._stt: groq.STT | None = None
        self._buffer: _FrameBuffer | None = None
        self._recognition_task: asyncio.Task | None = None
        self._player: PodcastPlayer | None = None
        self._browser_audio_task: asyncio.Task | None = None

    @property
    def is_browser_mode(self) -> bool:
        return self._audio_source == "browser"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Create the STT, frame buffer, player, and recognition loop.

        In server mode, does NOT spawn ffmpeg yet — that happens when the
        client sends its first ``{type:"play", t:...}`` data packet.

        In browser mode, no player is created — audio arrives via
        ``attach_browser_track()`` once the extension publishes its track.
        """
        self._stt = groq.STT(model=CONFIG.stt.model)
        self._buffer = _FrameBuffer()
        self._recognition_task = asyncio.create_task(
            self._recognition_loop(), name="podcast_recognition"
        )

        if self._audio_source == "browser":
            logger.info(
                "Podcast pipeline initialised in BROWSER mode "
                "(awaiting podcast-audio track from extension)"
            )
        else:
            if self._audio_url:
                self._player = PodcastPlayer(
                    self._audio_url,
                    self._buffer,
                    proxy=self._proxy,
                )
            logger.info("Podcast pipeline initialised (awaiting client play)")

    async def shutdown(self) -> None:
        """Tear down ffmpeg / browser audio consumer and recognition loop."""
        if self._player is not None:
            await self._player.close()
        if self._browser_audio_task is not None:
            self._browser_audio_task.cancel()
            try:
                await self._browser_audio_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._recognition_task is not None:
            self._recognition_task.cancel()
            try:
                await self._recognition_task
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------
    # Browser audio mode — subscribe to the extension's LiveKit track
    # ------------------------------------------------------------------
    def attach_browser_track(self, track: rtc.Track) -> None:
        """Start consuming audio frames from a LiveKit audio track.

        Called by the agent when the extension's ``podcast-audio`` track
        is subscribed. Replaces the ffmpeg player entirely.
        """
        if self._browser_audio_task is not None:
            self._browser_audio_task.cancel()
        self._browser_audio_task = asyncio.create_task(
            self._consume_browser_audio(track), name="browser_audio_consumer"
        )
        logger.info("Attached browser audio track — consuming frames for STT")

    async def _consume_browser_audio(self, track: rtc.Track) -> None:
        """Read audio frames from a LiveKit track and push to the STT buffer."""
        assert self._buffer is not None
        audio_stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
        frames_pushed = 0
        try:
            async for event in audio_stream:
                self._buffer.push_frame(event.frame)
                frames_pushed += 1
                if frames_pushed == 1:
                    logger.info("First browser audio frame pushed to STT buffer")
                elif frames_pushed % 500 == 0:
                    logger.info(
                        "Browser audio healthy — %d frames pushed",
                        frames_pushed,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Browser audio consumer crashed after %d frames", frames_pushed)

    # ------------------------------------------------------------------
    # Commands (driven by the client's podcast.control data channel)
    # ------------------------------------------------------------------
    async def play(self, start_sec: float) -> None:
        """Start or restart ffmpeg decoding from ``start_sec``.

        No-op in browser mode — the extension streams audio directly.
        """
        if self._audio_source == "browser":
            logger.debug("play() called in browser mode — no-op")
            return
        if self._player is None:
            logger.warning("Received 'play' but podcast player not initialised")
            return
        logger.info("Dispatching podcast play at t=%.2fs", start_sec)
        await self._player.play(start_sec)

    async def pause(self) -> None:
        """Stop ffmpeg (kills the decode subprocess).

        No-op in browser mode — audio stops when the user pauses YouTube.
        """
        if self._audio_source == "browser":
            logger.debug("pause() called in browser mode — no-op")
            return
        if self._player is None:
            logger.warning("Received 'pause' but podcast player not initialised")
            return
        logger.info("Dispatching podcast pause")
        await self._player.pause()

    # ------------------------------------------------------------------
    # Fixed-interval recognition loop
    # ------------------------------------------------------------------
    async def _recognition_loop(self) -> None:
        """Every CHUNK_INTERVAL_SECONDS, send buffered audio to Whisper.

        This replaces the previous StreamAdapter + Silero VAD consumer.
        Podcast audio is continuous speech — VAD can't reliably detect
        segment boundaries, so fixed-interval chunking gives predictable
        transcript delivery (~10 s per chunk ≈ 2-3 sentences).
        """
        assert self._stt is not None
        assert self._buffer is not None
        try:
            while True:
                await asyncio.sleep(CHUNK_INTERVAL_SECONDS)
                frames = self._buffer.drain()
                if not frames:
                    continue

                try:
                    event = await self._stt.recognize(frames)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Podcast STT recognition failed", exc_info=True)
                    continue

                if not event.alternatives:
                    continue
                text = (event.alternatives[0].text or "").strip()
                if not text:
                    continue

                logger.info("Podcast transcript: %s", text[:120])
                try:
                    await self._on_transcript(text)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Podcast transcript callback crashed")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Podcast recognition loop crashed")
