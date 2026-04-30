"""Lightweight metrics for the agent.

The fallback path in :class:`PlayoutWaiter` is our airbag for missing
``lk.playback_finished`` RPCs. Counting how often it fires per persona
is what gates removing the workaround: the architecture is considered
validated when ``fallback`` stays under 1% per rolling hour.

There is no Prometheus/OTLP exporter wired into the agent today. To
keep this self-contained we emit one structured log line per
``inc`` / ``observe`` call (``metric=<name> persona=<x> outcome=<y>
...``) so a log-based metrics pipeline (Grafana Cloud / Datadog log
search / Fly.io log-shipper) can scrape the values without us pulling
in a new dependency. Swapping to ``prometheus_client`` later only
requires replacing :class:`Counter` and :class:`Histogram` — call sites
stay put.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Any

logger = logging.getLogger("podcast-commentary.metrics")


class Counter:
    """Label-keyed monotonic counter.

    Thread-safe so callers from agent task supervisors and the LiveKit
    event loop can ``inc`` concurrently without losing increments.
    """

    def __init__(
        self,
        name: str,
        *,
        label_names: Iterable[str],
        description: str = "",
    ) -> None:
        self._name = name
        self._label_names: tuple[str, ...] = tuple(label_names)
        self._description = description
        self._values: dict[tuple[str, ...], float] = defaultdict(float)
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def label_names(self) -> tuple[str, ...]:
        return self._label_names

    def inc(self, *, amount: float = 1.0, **labels: str) -> None:
        """Increment the counter for the given label set.

        Raises ``ValueError`` if ``labels`` doesn't match ``label_names``
        exactly — silently dropping a typo would leave us with an alert
        firing on stale labels and no idea why.
        """
        if set(labels) != set(self._label_names):
            raise ValueError(
                f"counter {self._name} expects labels "
                f"{sorted(self._label_names)}, got {sorted(labels)}"
            )
        key = tuple(labels[name] for name in self._label_names)
        with self._lock:
            self._values[key] += amount
            new_total = self._values[key]
        label_repr = " ".join(f"{k}={v}" for k, v in zip(self._label_names, key))
        logger.info(
            "metric=%s %s amount=%s total=%s",
            self._name,
            label_repr,
            amount,
            new_total,
        )

    def snapshot(self) -> dict[tuple[str, ...], float]:
        """Return a copy of the current label→value map. Test-only."""
        with self._lock:
            return dict(self._values)


class Histogram:
    """Label-keyed observation series.

    The agent has no in-process aggregation: each ``observe`` emits one
    structured log line and the log-based metrics pipeline (Grafana /
    Datadog / Fly log-shipper) computes p50/p95/p99 on the scrape side.
    Storing the raw observations locally would only matter for
    in-process /metrics scraping, which we don't expose today. Snapshots
    are kept for tests.
    """

    def __init__(
        self,
        name: str,
        *,
        label_names: Iterable[str],
        description: str = "",
    ) -> None:
        self._name = name
        self._label_names: tuple[str, ...] = tuple(label_names)
        self._description = description
        self._observations: dict[tuple[str, ...], list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def label_names(self) -> tuple[str, ...]:
        return self._label_names

    def observe(self, value: float, **labels: str) -> None:
        if set(labels) != set(self._label_names):
            raise ValueError(
                f"histogram {self._name} expects labels "
                f"{sorted(self._label_names)}, got {sorted(labels)}"
            )
        key = tuple(labels[name] for name in self._label_names)
        with self._lock:
            self._observations[key].append(value)
        label_repr = " ".join(f"{k}={v}" for k, v in zip(self._label_names, key))
        logger.info(
            "metric=%s %s value=%.4f",
            self._name,
            label_repr,
            value,
        )

    def snapshot(self) -> dict[tuple[str, ...], list[float]]:
        """Return a copy of recorded observations per label set. Test-only."""
        with self._lock:
            return {k: list(v) for k, v in self._observations.items()}


# Outcome of a single ``PlayoutWaiter.wait`` invocation.
#   ok       — vendor ``lk.playback_finished`` RPC arrived; the framework
#              resolved ``SpeechHandle.wait_for_playout`` cleanly. Healthy.
#   fallback — vendor RPC dropped; we synthesized ``playback_finished``
#              after detecting an audio-settle plateau. The airbag fired
#              but audio still played through. Removal of the workaround
#              is gated on this rate staying < 1% per rolling hour.
#   timeout  — neither RPC nor settle inside the per-turn timeout. Worst
#              outcome: we synthesize and may need ``force_listening``,
#              which can cut audio mid-sentence.
PLAYOUT_FINISHED_OUTCOMES: tuple[str, ...] = ("ok", "fallback", "timeout")

playout_finished_rpc_total = Counter(
    name="playout_finished_rpc_total",
    label_names=("persona", "outcome"),
    description=(
        "Per-turn outcome of PlayoutWaiter.wait. Used by the alert that "
        "pages agent eng on-call when fallback rate exceeds 1% of "
        "(ok+fallback+timeout) over a rolling 1-hour window."
    ),
)


# Per-persona avatar startup metrics.
#
#   avatar_startup_seconds — wall-clock from ``_start_avatar`` invocation
#       to first published video frame. Recorded only on success so
#       percentile dashboards aren't pulled toward the timeout ceiling.
#       ``room_role`` distinguishes the primary (RoomAgentDispatch
#       target) room from secondary rooms.
#
#   avatar_startup_total — outcome counter. ``success`` increments after
#       the histogram observation; ``timeout`` when no video publish
#       lands within ``AvatarConfig.startup_timeout_s``; ``error`` when
#       ``AvatarSession.start`` itself raises (LemonSlice 5xx, network
#       blip, etc.). Success rate = success / (success + timeout + error).
AVATAR_STARTUP_OUTCOMES: tuple[str, ...] = ("success", "timeout", "error")

avatar_startup_seconds = Histogram(
    name="avatar_startup_seconds",
    label_names=("persona", "room_role"),
    description=(
        "Wall-clock seconds from _start_avatar invocation to first published "
        "video frame for the avatar participant. Recorded on success only."
    ),
)

avatar_startup_total = Counter(
    name="avatar_startup_total",
    label_names=("persona", "outcome"),
    description=(
        "Avatar startup attempts grouped by persona and outcome "
        "(success | timeout | error). Powers the success-rate KPI."
    ),
)


async def watch_avatar_startup(
    *,
    room: Any,
    identity: str,
    persona: str,
    room_role: str,
    started_at: float,
    timeout: float,
    on_success: Callable[[float], None] | None = None,
) -> None:
    """Wait for ``identity``'s first video publish and record metrics.

    ``started_at`` should be a ``time.perf_counter()`` timestamp captured
    at ``_start_avatar`` invocation — the elapsed value lands on the
    ``avatar_startup_seconds`` histogram and ``avatar_startup_total``
    increments with ``outcome="success"``. On ``timeout`` the histogram
    is left untouched (keeping percentiles bounded by real successes)
    and only ``outcome="timeout"`` is incremented.

    The caller increments ``outcome="error"`` itself when
    ``AvatarSession.start`` raises — this watcher is only launched on
    the success path of that call.

    ``on_success`` receives the elapsed seconds when the success path
    lands so the per-Director session-lifecycle log can
    capture this persona's startup time without re-deriving it from the
    process-wide histogram (which retains observations across jobs).

    Safe to launch as fire-and-forget: any unexpected exception is
    logged and swallowed so a watcher bug can't take down the worker.
    """
    try:
        # Lazy import keeps ``metrics.py`` importable in non-agent
        # contexts (test fixtures, the API process) where pulling in
        # ``livekit.rtc`` is unnecessary overhead.
        from livekit import rtc

        ready = asyncio.Event()

        def _is_video(publication: Any) -> bool:
            return getattr(publication, "kind", None) == rtc.TrackKind.KIND_VIDEO

        def _has_video(participant: Any) -> bool:
            for publication in participant.track_publications.values():
                if _is_video(publication):
                    return True
            return False

        def on_participant_connected(p: Any) -> None:
            if getattr(p, "identity", None) == identity and _has_video(p):
                ready.set()

        def on_track_published(publication: Any, p: Any) -> None:
            if getattr(p, "identity", None) == identity and _is_video(publication):
                ready.set()

        room.on("participant_connected", on_participant_connected)
        room.on("track_published", on_track_published)
        try:
            # Fast path: the avatar may have already joined and
            # published before this watcher attached its listeners. The
            # LemonSlice plugin's ``start()`` returns once it owns the
            # session, which can race the publish event.
            for participant in room.remote_participants.values():
                if getattr(participant, "identity", None) == identity and _has_video(participant):
                    elapsed = time.perf_counter() - started_at
                    avatar_startup_seconds.observe(elapsed, persona=persona, room_role=room_role)
                    avatar_startup_total.inc(persona=persona, outcome="success")
                    if on_success is not None:
                        with contextlib.suppress(Exception):
                            on_success(elapsed)
                    return

            try:
                await asyncio.wait_for(ready.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                avatar_startup_total.inc(persona=persona, outcome="timeout")
                logger.warning(
                    "[avatar-metric] %s did not publish video within %.1fs — recorded timeout",
                    identity,
                    timeout,
                )
                return

            elapsed = time.perf_counter() - started_at
            avatar_startup_seconds.observe(elapsed, persona=persona, room_role=room_role)
            avatar_startup_total.inc(persona=persona, outcome="success")
            if on_success is not None:
                with contextlib.suppress(Exception):
                    on_success(elapsed)
        finally:
            room.off("participant_connected", on_participant_connected)
            room.off("track_published", on_track_published)
    except Exception:
        # Watcher must never crash the agent — a metric drop is
        # preferable to a torn-down session.
        logger.warning(
            "[avatar-metric] watcher for %s crashed — metric skipped",
            identity,
            exc_info=True,
        )


# Pacing + cross-persona reference metrics.
#
#   commentary_inter_gap_seconds — wall-clock gap between consecutive
#       commentary turns (regardless of persona), measured from end of
#       prior turn's playout to start of next turn's delivery. Captures
#       the visible cadence the user experiences: the MIN_GAP=5s floor,
#       BURST_COOLDOWN=8s pauses, and any drift introduced by selector
#       latency or playout-waiter recovery.
#
#   commentary_turn_total — per-persona count of delivered turns. Pure
#       denominator for the reference-rate computation below; the
#       inter-gap histogram has no persona label so we can't derive
#       per-persona turn count from it.
#
#   commentary_co_speaker_referenced_total — turns where the persona's
#       emitted line shared at least one significant token (after
#       stop-word filtering) with the co-speaker's last 3 lines. The
#       per-persona rate (referenced / turn_total) is one of the few
#       proxies we have for whether the personas are still listening to
#       each other across separate LiveKit rooms.
commentary_inter_gap_seconds = Histogram(
    name="commentary_inter_gap_seconds",
    label_names=(),
    description=(
        "Wall-clock seconds between the end of one commentary turn's "
        "playout and the start of the next turn's delivery, regardless "
        "of persona. Baseline target for cadence dashboards."
    ),
)

commentary_turn_total = Counter(
    name="commentary_turn_total",
    label_names=("persona",),
    description=(
        "Per-persona count of delivered commentary turns. Denominator "
        "for the co-speaker reference rate."
    ),
)

commentary_co_speaker_referenced_total = Counter(
    name="commentary_co_speaker_referenced_total",
    label_names=("persona",),
    description=(
        "Turns where the persona's emitted line shares a significant "
        "token (post stop-word filter) with one of the co-speaker's "
        "last 3 lines. Proxy for whether personas are still listening "
        "to each other across separate LiveKit rooms."
    ),
)


# Hand-rolled stop-list: small enough to be obvious, big enough to
# catch the words two comedians will inevitably both use in the same
# minute (pronouns, articles, common verbs/adverbs/conjunctions). Kept
# in metrics.py so the heuristic stays co-located with the counter it
# feeds — the baseline rate would shift if the stop-list changed.
_STOP_WORDS: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "for",
        "but",
        "not",
        "you",
        "are",
        "was",
        "were",
        "this",
        "that",
        "with",
        "from",
        "have",
        "has",
        "had",
        "they",
        "them",
        "their",
        "there",
        "then",
        "than",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "why",
        "how",
        "all",
        "any",
        "some",
        "each",
        "every",
        "just",
        "like",
        "about",
        "into",
        "over",
        "your",
        "yours",
        "our",
        "ours",
        "his",
        "her",
        "hers",
        "him",
        "she",
        "its",
        "itself",
        "myself",
        "yourself",
        "ourselves",
        "themselves",
        "been",
        "being",
        "very",
        "much",
        "more",
        "most",
        "such",
        "only",
        "also",
        "even",
        "still",
        "yet",
        "well",
        "now",
        "here",
        "would",
        "could",
        "should",
        "will",
        "shall",
        "may",
        "might",
        "must",
        "can",
        "one",
        "two",
        "ohh",
        "yeah",
        "okay",
        "right",
        "really",
        "actually",
        "literally",
        "kind",
        "sort",
        "thing",
        "things",
        "stuff",
        "guys",
        "guy",
        "lot",
        "way",
        "good",
        "great",
        "bad",
        "nice",
    }
)

# Token rule: lowercase ASCII letters, length ≥ 4. Length 4 is the
# break-even where common short verbs ("get", "say") drop out but
# domain words ("crypto", "vibes", "drama") survive without us
# maintaining a thousand-line stop-list.
_TOKEN_RE = re.compile(r"[a-z]{4,}")


def _tokens(text: str) -> set[str]:
    """Significant tokens from ``text`` for cross-persona overlap detection."""
    if not text:
        return set()
    return {tok for tok in _TOKEN_RE.findall(text.lower()) if tok not in _STOP_WORDS}


def references_co_speaker(line: str, co_speaker_recent: Iterable[str]) -> bool:
    """True if ``line`` shares a significant token with any of ``co_speaker_recent``.

    The caller is expected to pass the co-speaker's last 3 lines (the
    window the AC names). The function itself doesn't slice — keeping
    the responsibility for "what counts as recent" at the call site
    makes the heuristic auditable from one place and tests don't have
    to seed three identical lines just to exercise the truthy path.
    """
    line_tokens = _tokens(line)
    if not line_tokens:
        return False
    for co_line in co_speaker_recent:
        if line_tokens & _tokens(co_line):
            return True
    return False


__all__ = [
    "Counter",
    "Histogram",
    "PLAYOUT_FINISHED_OUTCOMES",
    "playout_finished_rpc_total",
    "AVATAR_STARTUP_OUTCOMES",
    "avatar_startup_seconds",
    "avatar_startup_total",
    "watch_avatar_startup",
    "commentary_inter_gap_seconds",
    "commentary_turn_total",
    "commentary_co_speaker_referenced_total",
    "references_co_speaker",
]
