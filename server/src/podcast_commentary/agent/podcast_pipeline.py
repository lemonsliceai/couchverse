"""Podcast STT + player lifecycle.

Bundles the collaborators that together make up "Fox's ears":

  * `groq.STT` — non-streaming Whisper called on fixed-interval audio chunks.
    Podcast speech is near-continuous with few natural pauses, so VAD-based
    segmentation (the previous approach) would buffer 30-60 s before emitting
    a transcript.  Fixed-interval chunking (~10 s) gives predictable,
    frequent delivery regardless of speech patterns.
  * `PodcastPlayer` — an ffmpeg subprocess decoding the YouTube audio URL
    into 16 kHz mono PCM and pushing frames into a buffer.

Factored out of `ComedianAgent` so the pipeline owns its own startup,
recognition loop, and shutdown — the agent just wires up the callback.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from livekit import rtc
from livekit.plugins import groq

from podcast_commentary.agent.podcast_player import PodcastPlayer

logger = logging.getLogger("podcast-commentary.podcast_pipeline")

# How often to send accumulated audio to Whisper for transcription.
# 10 s ≈ 2-3 sentences at typical speaking pace; after two chunks Fox
# has enough material (~5 sentences) to trigger commentary.
CHUNK_INTERVAL_SECONDS = 10.0


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
    """Fixed-interval STT + ffmpeg player + transcript delivery."""

    def __init__(
        self,
        *,
        audio_url: str,
        on_transcript: Callable[[str], Awaitable[None]],
        proxy: str | None = None,
    ) -> None:
        self._audio_url = audio_url
        self._on_transcript = on_transcript
        self._proxy = proxy
        self._stt: groq.STT | None = None
        self._buffer: _FrameBuffer | None = None
        self._recognition_task: asyncio.Task | None = None
        self._player: PodcastPlayer | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Create the STT, frame buffer, player, and recognition loop.

        Does NOT spawn ffmpeg yet — that happens when the client sends its
        first ``{type:"play", t:...}`` data packet.
        """
        self._stt = groq.STT(model="whisper-large-v3-turbo")
        self._buffer = _FrameBuffer()
        self._recognition_task = asyncio.create_task(
            self._recognition_loop(), name="podcast_recognition"
        )
        self._player = PodcastPlayer(self._audio_url, self._buffer, proxy=self._proxy)
        logger.info("Podcast pipeline initialised (awaiting client play)")

    async def shutdown(self) -> None:
        """Tear down ffmpeg and recognition loop."""
        if self._player is not None:
            await self._player.close()
        if self._recognition_task is not None:
            self._recognition_task.cancel()
            try:
                await self._recognition_task
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------
    # Commands (driven by the client's podcast.control data channel)
    # ------------------------------------------------------------------
    async def play(self, start_sec: float) -> None:
        """Start or restart ffmpeg decoding from ``start_sec``."""
        if self._player is None:
            logger.warning(
                "Received 'play' but podcast player not initialised"
            )
            return
        logger.info("Dispatching podcast play at t=%.2fs", start_sec)
        await self._player.play(start_sec)

    async def pause(self) -> None:
        """Stop ffmpeg (kills the decode subprocess)."""
        if self._player is None:
            logger.warning(
                "Received 'pause' but podcast player not initialised"
            )
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
