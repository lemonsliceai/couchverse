"""Transcript state and commentary timing for Fox.

Tracks the podcast transcript and records when commentary fires so we
can enforce a minimum gap and burst cooldown.
"""

import logging
import re
import time

from podcast_commentary.agent.fox_config import CONFIG

logger = logging.getLogger("podcast-commentary.timing")
transcript_logger = logging.getLogger("podcast-commentary.transcript")

# Timing parameters (seconds) — sourced from the active FoxConfig preset.
# MIN_GAP measures from *speech end* (avatar playback_finished), not speech
# start, so the gate reflects the listener's experience of silence.
MIN_GAP = CONFIG.timing.min_silence_between_jokes_s
BURST_WINDOW = CONFIG.timing.burst_window_s
BURST_MAX = CONFIG.timing.max_jokes_per_burst
BURST_COOLDOWN = CONFIG.timing.burst_cooldown_s

# Number of sentence-ending punctuation marks (from Whisper output) that
# must accumulate before Fox triggers commentary. ~5 sentences ≈ 25-35s
# of podcast speech at typical speaking pace.
SENTENCE_THRESHOLD = CONFIG.timing.sentences_before_joke


def count_sentences(text: str) -> int:
    """Count sentence-ending punctuation marks in Whisper output.

    Whisper-large-v3-turbo reliably punctuates transcripts, so counting
    occurrences of . ? ! is a reasonable proxy for sentence boundaries.
    """
    return len(re.findall(r"[.!?]", text))


class CommentaryTimer:
    """Tracks commentary timing and enforces rules.

    Two timestamps matter:
      * `_last_speech_end_time` — when Fox most recently *finished*
        playing audio (driven by `AudioOutput.playback_finished`, the
        authoritative "avatar stopped talking" signal). ``MIN_GAP`` counts
        from here.
      * ``_speech_start_times`` — when each speaking turn began
        (``playback_started``). Used for the burst window / cooldown.

    The timer never consults "is Fox currently speaking?" — that gate
    lives in ``ComedianAgent.is_speaking`` (authoritative, SpeechHandle-
    backed). The timer only enforces *post-speech* pacing rules.

    Failed commentary generations (LLM produced nothing, or the turn was
    preempted before audio started) never fire ``playback_started`` — so
    they don't burn the gap budget, and they can't block real reactions.
    """

    def __init__(self):
        self._speech_start_times: list[float] = []
        self._last_speech_end_time: float = 0
        self._session_start: float = time.time()
        self._in_cooldown: bool = False
        self._cooldown_end: float = 0
        # Effective MIN_GAP for this session. Defaults to the config-derived
        # constant; the Director scales this (up for Quiet, down for Chatty)
        # in response to the extension's settings message.
        self.min_gap: float = MIN_GAP

    def time_since_last_comment(self) -> float:
        # Before the first turn ever lands, measure silence from session
        # start so the intro gate still works.
        if self._last_speech_end_time == 0:
            return time.time() - self._session_start
        return time.time() - self._last_speech_end_time

    def can_comment(self) -> bool:
        now = time.time()

        # Enforce minimum gap (measured from end-of-speech).
        if self.time_since_last_comment() < self.min_gap:
            return False

        # Enforce burst cooldown
        if self._in_cooldown and now < self._cooldown_end:
            return False
        elif self._in_cooldown:
            self._in_cooldown = False

        # Check burst limit — only count turns that actually produced audio.
        recent = [t for t in self._speech_start_times if now - t < BURST_WINDOW]
        if len(recent) >= BURST_MAX:
            self._in_cooldown = True
            self._cooldown_end = now + BURST_COOLDOWN
            logger.info("Burst limit hit — entering %ds cooldown", BURST_COOLDOWN)
            return False

        return True

    def record_speech_start(self) -> None:
        """Called on ``AudioOutput.playback_started``."""
        now = time.time()
        self._speech_start_times.append(now)
        # Prune entries older than BURST_WINDOW so the list stays bounded.
        cutoff = now - BURST_WINDOW
        self._speech_start_times = [t for t in self._speech_start_times if t >= cutoff]

    def record_speech_end(self) -> None:
        """Called on ``AudioOutput.playback_finished``."""
        self._last_speech_end_time = time.time()

    def stats(self) -> dict:
        return {
            "total_comments": len(self._speech_start_times),
            "time_since_last": round(self.time_since_last_comment(), 1),
            "in_cooldown": self._in_cooldown,
        }


class FullTranscript:
    """Accumulates the podcast transcript and exposes what Fox should react to.

    After each commentary, the read cursor advances so Fox only sees
    transcript that arrived since his last comment.
    """

    def __init__(self):
        self._parts: list[tuple[float, str]] = []  # (timestamp, text)
        # Cursor: index of the first part Fox hasn't reacted to yet.
        # Advanced by reset_sentence_count() when commentary fires.
        self._cursor: int = 0
        self._sentence_count_since_reset: int = 0

    def add(self, text: str) -> int:
        """Add a new transcribed utterance. Returns total sentences since reset."""
        text = text.strip()
        if not text:
            return self._sentence_count_since_reset
        self._parts.append((time.time(), text))
        self._sentence_count_since_reset += count_sentences(text)
        transcript_logger.info(
            "TRANSCRIPT [%d] (sentences_since_reset=%d): %s",
            len(self._parts),
            self._sentence_count_since_reset,
            text,
        )
        return self._sentence_count_since_reset

    @property
    def sentences_since_reset(self) -> int:
        return self._sentence_count_since_reset

    def reset_sentence_count(self) -> None:
        """Reset the sentence counter and advance the read cursor."""
        self._sentence_count_since_reset = 0
        self._cursor = len(self._parts)

    def recent_transcript(self) -> str:
        """Transcript since Fox's last comment — what he's reacting to."""
        if not self._parts:
            return ""
        return " ".join(txt for _, txt in self._parts[self._cursor :])

    def seconds_since_last_utterance(self) -> float | None:
        if not self._parts:
            return None
        return time.time() - self._parts[-1][0]

    @property
    def part_count(self) -> int:
        return len(self._parts)

    def has_content(self) -> bool:
        return len(self._parts) > 0
