"""Viewport guard, buffer pool, cadence governor, and the event-driven frame path.

The pipeline is push-shaped, not poll-shaped:

    backend delivers a unique frame
        -> normalized once, into a pooled canonical buffer
        -> published to a capacity-one latest-frame slot
        -> a consumer waiting on that slot wakes immediately

Nothing sleeps on a timer to discover a frame arrived, and nothing accumulates
a backlog: the slot holds exactly one frame and counts what it dropped. When
perception falls behind, the obsolete frame is discarded rather than processed
late, because a stale decision is worse than a skipped one.

Freshness is still computed by the consumer from ``captured_at_s`` - the start
of acquisition - so a frame that waited cannot look young.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import sys
import threading
import weakref
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from prospector_engine.contracts import (
    EVIDENCE_MINT_KEY,
    CadenceMode,
    CadenceReport,
    CapturedFrame,
    CaptureMetrics,
    EvidenceStatus,
    EvidenceToken,
    FitPhase,
    FrameEnvelope,
    GovernorState,
    LatencySummary,
    PerformanceTier,
    Provenance,
    RateSummary,
    RawFrame,
    ViewportFit,
    freeze_array,
    monotonic_s,
)
from prospector_engine.geometry import (
    CANONICAL_SIZE_PX,
    ViewportGeometry,
    ViewportState,
)
from prospector_engine.ports import CaptureSource, PlatformPort
from prospector_engine.trace import GovernorTransition, TraceRing

__all__ = [
    "CadenceGovernor",
    "CaptureConfig",
    "CaptureService",
    "EvidenceRegistry",
    "FrameBufferPool",
    "LatencyTracker",
    "LatestFrameSlot",
    "MssCaptureSource",
    "ProcessSample",
    "ViewportGuard",
    "normalize_into_canonical",
]


def _sleep(seconds: float) -> None:
    """Wall-clock wait used only by the bounded viewport fit machine.

    Isolated here so the fit state machine can be driven by a virtual clock in
    tests without any module-level clock patching.
    """
    import time

    time.sleep(max(0.0, seconds))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaptureConfig:
    """Cadence, freshness, and resource bounds. Provisional configuration."""

    max_frame_age_ms: int = 100
    max_capture_duration_ms: int = 40
    stall_after_ms: int = 500
    duplicate_budget: int = 3
    viewport_recheck_interval_ms: int = 250
    pool_capacity: int = 8
    latency_window: int = 240
    #: The governor may never ask for more than the source can actually deliver.
    max_tier: PerformanceTier = PerformanceTier.MAXIMUM
    start_tier: PerformanceTier = PerformanceTier.STANDARD
    #: Consecutive healthy seconds before the governor tries the next tier up.
    upshift_after_s: float = 4.0
    #: Fraction of the tier's nominal rate that counts as "keeping up".
    downshift_ratio: float = 0.7
    #: Fraction of the tier that must be *saturated* before climbing. Higher
    #: than the downshift ratio on purpose: comfortably inside a tier is not
    #: the same as running out of room in it.
    upshift_saturation: float = 0.95
    #: Consecutive bad polls before a downshift. Four polls at the supervisor
    #: interval is a full second of evidence: a resize, a collection pause or
    #: the first cold frames after start are free, a sustained shortfall is not.
    downshift_polls: int = 4
    #: Polls a probe must stay good before it is believed.
    probe_confirm_polls: int = 2
    #: How long a failed probe suppresses the next one. Long enough that a
    #: capped source stops oscillating; short enough that a machine which
    #: recovers gets its cadence back.
    probe_cooldown_s: float = 20.0
    #: How long a *discovered ceiling* is honoured before the governor is
    #: willing to test it again. A thermally throttled laptop should not be
    #: capped for the rest of the session, so a cap is a measurement with an
    #: expiry rather than a permanent verdict.
    ceiling_retry_after_s: float = 60.0
    #: Good polls before the state machine will call a tier STABLE.
    min_stable_polls: int = 2
    #: Samples before cadence is allowed to *vouch for Live*. Deliberately
    #: larger than the STABLE bar: settling into a tier is a cheap decision,
    #: authorizing keyboard output is not.
    min_tier_samples: int = 8
    #: Processed-to-tier ratio a stable tier must sustain, and the ratio of
    #: the tier below that a shortfall is measured against.
    stable_processed_ratio: float = 0.90
    #: A probe holds only if it processes this much more than the tier it left.
    probe_gain: float = 1.10
    #: Share of frames that may go unobserved while still calling a tier stable.
    max_observation_loss: float = 0.02
    #: Live eligibility: processed-to-tier ratio and superseded share the
    #: pipeline must keep. Looser than the stable bar on purpose: Live is
    #: judged on latency budgets, and a latest-only pipeline that processes
    #: four frames in five at 60 Hz is fresher than one keeping every frame
    #: at 30 Hz. Provisional (E-PERF PENDING).
    live_min_processed_ratio: float = 0.80
    live_max_observation_loss: float = 0.25
    #: Stale frames tolerated inside one supervisor window.
    max_stale_per_window: int = 0
    #: Absolute p95 frame-age ceiling for Live, whatever the tier interval says.
    live_max_age_ms: int = 75
    #: How far back the governor and Live eligibility look at latency. Short
    #: on purpose: readiness is a statement about now.
    recent_window_s: float = 2.0
    #: How long after a cadence, source, geometry or profile change samples
    #: are tagged as settling rather than judged. Long enough for OpenCV's
    #: first passes and a ScreenCaptureKit reconfiguration to land.
    settle_s: float = 1.5
    #: Reacquisition backoff. Bounded and capped so a window that is gone for
    #: good costs a retry every few seconds, not a busy loop.
    reacquire_initial_delay_s: float = 0.25
    reacquire_max_delay_s: float = 4.0
    supervisor_interval_s: float = 0.25
    #: Fit & Lock: stable read-backs required before an achieved client size is
    #: believed, and the bound on the whole attempt.
    fit_required_readbacks: int = 3
    fit_readback_interval_s: float = 0.12
    fit_deadline_s: float = 3.0
    fit_max_attempts: int = 2
    fit_tolerance_logical: float = 1.0
    #: Hard ceiling on how long a geometry transaction may fence mismatch
    #: classification. Generous next to ``fit_deadline_s`` because the fence
    #: also covers the capture restart and the wait for a matching frame, and
    #: it exists to bound a *crashed* transaction, not a slow one.
    fit_transaction_deadline_s: float = 12.0
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="TREASURE_NAVIGATION_PLAN.md section 7.4 E-PERF; DECISIONS.md D-019, D-023",
            note="tier ladder, governor hysteresis and fit bounds chosen from local "
            "measurement; E-PERF and E-VIEW are PENDING",
        )
    )


# ---------------------------------------------------------------------------
# Buffer pool
# ---------------------------------------------------------------------------


class FrameBufferPool:
    """A small, bounded, reusable supply of canonical-raster buffers.

    Without this, capturing 1280x720x3 at 110 Hz churns ~290 MB/s through the
    allocator. The pool keeps at most ``capacity`` buffers alive, so memory is
    flat with respect to frame rate; when it is exhausted the caller is told,
    and the service drops the frame and counts it rather than growing.
    """

    def __init__(self, capacity: int = 8) -> None:
        self._capacity = max(2, capacity)
        self._free: list[NDArray[np.uint8]] = []
        self._live = 0
        self._lock = threading.Lock()
        self.exhausted = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def live(self) -> int:
        with self._lock:
            return self._live

    def acquire(self, height: int, width: int) -> NDArray[np.uint8] | None:
        with self._lock:
            while self._free:
                buffer = self._free.pop()
                if buffer.shape == (height, width, 3):
                    self._live += 1
                    return buffer
            if self._live >= self._capacity:
                self.exhausted += 1
                return None
            self._live += 1
        return np.empty((height, width, 3), dtype=np.uint8)

    def release(self, buffer: NDArray[np.uint8]) -> None:
        with self._lock:
            self._live = max(0, self._live - 1)
            if len(self._free) < self._capacity:
                with contextlib.suppress(ValueError):
                    buffer.flags.writeable = True
                    self._free.append(buffer)

    def clear(self) -> None:
        with self._lock:
            self._free.clear()
            self._live = 0


# ---------------------------------------------------------------------------
# Latest-frame slot
# ---------------------------------------------------------------------------


class LatestFrameSlot:
    """Capacity-one, latest-wins, wake-on-publish.

    ``wait_for_new`` is what makes the pipeline event-driven: a consumer blocks
    until a frame *newer than the one it already saw* exists, so there is no
    polling interval and no backlog to fall behind on.

    A replaced frame is **superseded**, not lost: the design intends it, and
    calling it a "drop" alongside genuine capture failures is what made a
    healthy pipeline read as catastrophic ("drop 7055"). The counter keeps its
    old attribute name for compatibility and is reported under the honest one.
    """

    def __init__(self) -> None:
        self._value: FrameEnvelope | None = None
        self._sequence = 0
        self._taken_sequence = 0
        self._has_consumer = False
        self._lock = threading.Condition()
        self.dropped = 0
        self.lifetime_dropped = 0
        self._on_superseded: Callable[[], None] | None = None

    @property
    def superseded(self) -> int:
        """Frames a consumer wanted and never got, this session."""
        return self.dropped

    def set_supersede_hook(self, hook: Callable[[], None] | None) -> None:
        self._on_superseded = hook

    def reset_session(self) -> None:
        with self._lock:
            self.dropped = 0

    def publish(self, envelope: FrameEnvelope) -> None:
        superseded = False
        with self._lock:
            # A supersede only counts when a *consumer* wanted the frame. With
            # nobody consuming - Shadow not started, only the preview peeking -
            # every frame would otherwise be counted, which reads as a disaster
            # while the pipeline is perfectly healthy.
            if (
                self._has_consumer
                and self._value is not None
                and self._sequence > self._taken_sequence
            ):
                self.dropped += 1
                self.lifetime_dropped += 1
                superseded = True
            self._value = envelope
            self._sequence = envelope.frame.sequence
            self._lock.notify_all()
        if superseded and self._on_superseded is not None:
            with contextlib.suppress(Exception):
                self._on_superseded()

    def peek(self) -> FrameEnvelope | None:
        with self._lock:
            return self._value

    @property
    def has_consumer(self) -> bool:
        """Whether anyone has ever waited on this slot this session."""
        with self._lock:
            return self._has_consumer

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._sequence

    def wait_for_new(self, after_sequence: int, timeout_s: float) -> FrameEnvelope | None:
        """Block until a frame newer than ``after_sequence`` is available."""
        deadline = monotonic_s() + timeout_s
        with self._lock:
            if not self._has_consumer:
                self._has_consumer = True
                self._taken_sequence = self._sequence
            while True:
                if self._value is not None and self._sequence > after_sequence:
                    self._taken_sequence = max(self._taken_sequence, self._sequence)
                    return self._value
                remaining = deadline - monotonic_s()
                if remaining <= 0:
                    return None
                self._lock.wait(remaining)

    def clear(self) -> None:
        with self._lock:
            self._value = None
            self._taken_sequence = self._sequence
            self._has_consumer = False


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class LatencyTracker:
    """A bounded ring of timestamped durations with two views.

    ``summary()`` is the history: the whole ring, for diagnostics.
    ``recent(window_s)`` is what the governor and Live eligibility judge on:
    only samples from the last few seconds *and* from the current epoch. A
    single 274 ms sample from a resize twenty seconds ago stays visible in the
    history and stops counting against the pipeline - the previous design let
    one such sample block Live for as long as the ring took to roll over.
    """

    def __init__(self, label: str, window: int = 240) -> None:
        self._label = label
        self._samples: deque[tuple[float, float]] = deque(maxlen=window)
        self._lock = threading.Lock()
        self._epoch_started_s = 0.0

    def record_ms(self, milliseconds: float, now_s: float | None = None) -> None:
        stamp = monotonic_s() if now_s is None else now_s
        with self._lock:
            self._samples.append((stamp, milliseconds))

    def start_epoch(self, now_s: float | None = None) -> None:
        """Samples before this instant no longer count as recent."""
        with self._lock:
            self._epoch_started_s = monotonic_s() if now_s is None else now_s

    def _summarise(self, values: list[float]) -> LatencySummary:
        values.sort()
        if not values:
            return LatencySummary(self._label, 0, 0.0, 0.0, 0.0, 0.0)

        def at(fraction: float) -> float:
            index = min(len(values) - 1, max(0, round(fraction * (len(values) - 1))))
            return values[index]

        return LatencySummary(self._label, len(values), at(0.5), at(0.95), at(0.99), values[-1])

    def summary(self) -> LatencySummary:
        with self._lock:
            values = [value for _stamp, value in self._samples]
        return self._summarise(values)

    def recent(self, window_s: float, now_s: float | None = None) -> LatencySummary:
        now = monotonic_s() if now_s is None else now_s
        with self._lock:
            floor = max(self._epoch_started_s, now - window_s)
            values = [value for stamp, value in self._samples if stamp >= floor]
        return self._summarise(values)


class _RateCounter:
    """Events per second over a sliding window, plus session and lifetime totals.

    Three numbers, because they answer three different questions: the rate says
    what is happening now, the session total says what happened since this run
    started, and the lifetime total survives an epoch reset so a soak can still
    account for everything.
    """

    def __init__(self, window_s: float = 2.0) -> None:
        self._window_s = window_s
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()
        self.total = 0
        self.lifetime = 0

    def tick(self, now_s: float | None = None, count: int = 1) -> None:
        now = now_s if now_s is not None else monotonic_s()
        with self._lock:
            self.total += count
            self.lifetime += count
            for _ in range(count):
                self._stamps.append(now)
            self._trim(now)

    def _trim(self, now: float) -> None:
        cutoff = now - self._window_s
        while self._stamps and self._stamps[0] < cutoff:
            self._stamps.popleft()

    def rate(self) -> float:
        now = monotonic_s()
        with self._lock:
            self._trim(now)
            if len(self._stamps) < 2:
                return 0.0
            span = self._stamps[-1] - self._stamps[0]
            return (len(self._stamps) - 1) / span if span > 0 else 0.0

    def reset_session(self) -> None:
        """Start a new measurement epoch. Lifetime survives on purpose."""
        with self._lock:
            self.total = 0
            self._stamps.clear()

    def summary(self, label: str) -> RateSummary:
        with self._lock:
            session, lifetime = self.total, self.lifetime
        return RateSummary(label, session, self.rate(), lifetime)


class _MachTaskBasicInfo(ctypes.Structure):
    """``mach_task_basic_info`` - the only macOS source of *current* RSS.

    ``getrusage`` reports ``ru_maxrss``, which is the **peak**. Reporting a
    peak as "memory now" makes a soak test look like it is leaking when it has
    been flat for an hour, so the two are measured separately.
    """

    _fields_ = [
        ("virtual_size", ctypes.c_uint64),
        ("resident_size", ctypes.c_uint64),
        ("resident_size_max", ctypes.c_uint64),
        ("user_time", ctypes.c_uint64),
        ("system_time", ctypes.c_uint64),
        ("policy", ctypes.c_int),
        ("suspend_count", ctypes.c_int),
    ]


class _WindowsCounters(ctypes.Structure):
    """``PROCESS_MEMORY_COUNTERS`` - current and peak working set on Windows."""

    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


@dataclass(frozen=True)
class ProcessSample:
    """One resource reading. ``current`` and ``peak`` are different questions."""

    cpu_percent: float
    rss_current_mb: float
    rss_peak_mb: float


class _ProcessUsage:
    """CPU percent plus current *and* peak RSS, with no extra dependency.

    ``resource`` does not exist on Windows, so it is imported lazily inside the
    POSIX branch: importing this module must succeed on both platforms, which
    is the property the cross-platform contract tests rely on.
    """

    _MACH_TASK_BASIC_INFO = 20
    _COUNT = ctypes.sizeof(_MachTaskBasicInfo) // ctypes.sizeof(ctypes.c_uint32)

    def __init__(self) -> None:
        self._last_cpu_s = self._cpu_seconds()
        self._last_wall_s = monotonic_s()
        self._percent = 0.0
        self._peak_mb = 0.0
        self._libc: Any = None
        if sys.platform == "darwin":
            with contextlib.suppress(Exception):
                self._libc = ctypes.CDLL("/usr/lib/libSystem.dylib", use_errno=True)

    @staticmethod
    def _cpu_seconds() -> float:
        if sys.platform == "win32":  # pragma: no cover - exercised only on Windows
            return float(os.times().user + os.times().system)
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        return usage.ru_utime + usage.ru_stime

    def _rss_mb(self) -> tuple[float, float]:
        """``(current, peak)`` in megabytes; zeros when the OS will not say."""
        if sys.platform == "darwin" and self._libc is not None:
            info = _MachTaskBasicInfo()
            count = ctypes.c_uint32(self._COUNT)
            try:
                task = self._libc.mach_task_self()
                status = self._libc.task_info(
                    task,
                    ctypes.c_uint32(self._MACH_TASK_BASIC_INFO),
                    ctypes.byref(info),
                    ctypes.byref(count),
                )
            except Exception:  # pragma: no cover - defensive
                status = -1
            if status == 0:
                mb = 1024.0 * 1024.0
                return (info.resident_size / mb, info.resident_size_max / mb)
        if sys.platform == "win32":  # pragma: no cover - exercised only on Windows
            counters = _WindowsCounters()
            counters.cb = ctypes.sizeof(_WindowsCounters)
            psapi = ctypes.WinDLL("psapi.dll")  # type: ignore[attr-defined]
            kernel = ctypes.WinDLL("kernel32.dll")  # type: ignore[attr-defined]
            if psapi.GetProcessMemoryInfo(
                kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
            ):
                mb = 1024.0 * 1024.0
                return (counters.WorkingSetSize / mb, counters.PeakWorkingSetSize / mb)
        # POSIX fallback: only the peak is knowable without /proc.
        import resource

        raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_mb = raw / (1024 * 1024) if raw > 1 << 22 else raw / 1024
        return (0.0, peak_mb)

    def sample(self) -> ProcessSample:
        now_wall = monotonic_s()
        now_cpu = self._cpu_seconds()
        elapsed = now_wall - self._last_wall_s
        if elapsed >= 0.25:
            self._percent = 100.0 * (now_cpu - self._last_cpu_s) / elapsed
            self._last_wall_s, self._last_cpu_s = now_wall, now_cpu
        current, peak = self._rss_mb()
        self._peak_mb = max(self._peak_mb, peak, current)
        return ProcessSample(self._percent, current, self._peak_mb)


# ---------------------------------------------------------------------------
# Cadence governor
# ---------------------------------------------------------------------------


class CadenceGovernor:
    """Picks the highest cadence tier the source and machine actually sustain.

    An explicit state machine, because the previous implicit one could not
    remember a failed climb::

        WARMUP -> STABLE -> PROBE -> COOLDOWN
                              \\-> DEGRADED

    * **WARMUP** - no verdict yet. An empty measurement window is not evidence.
    * **STABLE** - the tier is being met. Only from here may a probe start.
    * **PROBE** - one tier up, on trial. Failing returns to the tier below and
      enters COOLDOWN, so a 60 Hz-capped source probes 90 once and then stops
      oscillating instead of retrying every four seconds forever.
    * **COOLDOWN** - a probe failed recently; no climbing. It expires, so a
      machine that becomes healthy later still gets its cadence back.
    * **DEGRADED** - below the 30 Hz Live floor, and saying so.

    A tier is judged on **processed** throughput, not captured: 120 frames
    delivered and 57 turned into decisions is a 60 Hz pipeline wearing a 120 Hz
    label. Frame age, observation loss, stale frames, and pool exhaustion all
    downshift on their own, because a high nominal rate carrying old or
    unprocessed frames is worse than a lower rate that keeps up.
    """

    LADDER: tuple[PerformanceTier, ...] = (
        PerformanceTier.DEGRADED,
        PerformanceTier.MINIMUM,
        PerformanceTier.STANDARD,
        PerformanceTier.HIGH,
        PerformanceTier.MAXIMUM,
    )

    def __init__(self, config: CaptureConfig | None = None) -> None:
        self._config = config or CaptureConfig()
        self._tier = self._config.start_tier
        self._state = GovernorState.WARMUP
        self._healthy_since_s: float | None = None
        self._reason: str | None = None
        self._changes = 0
        self._probes = 0
        self._failed_probes = 0
        self._consecutive_bad = 0
        self._consecutive_good = 0
        self._samples = 0
        self._cooldown_until_s = 0.0
        self._probe_started_s: float | None = None
        self._probe_from: PerformanceTier | None = None
        self._ceiling_hint: PerformanceTier | None = None
        self._ceiling_until_s = 0.0
        self._ceiling = self._config.max_tier
        self._last_processed_ratio = 0.0
        self._last_loss = 0.0
        self._last_p95_age_ms: float | None = None
        self._stable_since_s: float | None = None
        self._on_transition: Callable[[GovernorTransition], None] | None = None
        self._settling_polls = 0

    def set_transition_hook(self, hook: Callable[[GovernorTransition], None] | None) -> None:
        """Receive every tier or state change with the governor's reason."""
        self._on_transition = hook

    @property
    def settling_polls(self) -> int:
        """Polls skipped because the pipeline was settling."""
        return self._settling_polls

    def _announce(
        self, from_tier: PerformanceTier, to_tier: PerformanceTier, reason: str, now_s: float
    ) -> None:
        hook = self._on_transition
        if hook is not None:
            with contextlib.suppress(Exception):
                hook(
                    GovernorTransition(
                        now_s, from_tier.fps, to_tier.fps, self._state.value, reason
                    )
                )

    # -- introspection ----------------------------------------------------
    @property
    def tier(self) -> PerformanceTier:
        return self._tier

    @property
    def state(self) -> GovernorState:
        return self._state

    @property
    def degraded_reason(self) -> str | None:
        return self._reason

    @property
    def changes(self) -> int:
        return self._changes

    @property
    def probes(self) -> int:
        return self._probes

    def set_bounds(self, *, start: PerformanceTier, ceiling: PerformanceTier) -> None:
        """Move the tier and its ceiling together, for a cadence-mode change.

        The discovered ceiling from any earlier failed probe is cleared: the
        user has changed what they are asking for, so a measurement of the old
        request no longer applies.
        """
        self._ceiling = ceiling
        self._ceiling_hint = None
        self._tier = min(start, ceiling, key=lambda tier: tier.fps)
        self.reset_epoch(f"cadence bounds: up to {ceiling.fps} Hz")

    def reset_epoch(self, reason: str = "") -> None:
        """Drop every measurement. Called on start, source replacement,
        reacquisition, and tier change, because a rate averaged across a
        discontinuity describes neither side of it."""
        self._state = GovernorState.WARMUP
        self._samples = 0
        self._consecutive_bad = 0
        self._consecutive_good = 0
        self._healthy_since_s = None
        self._stable_since_s = None
        self._last_processed_ratio = 0.0
        self._last_loss = 0.0
        self._last_p95_age_ms = None
        self._reason = reason or None
        self._announce(
            self._tier,
            self._tier,
            f"epoch reset: {reason}" if reason else "epoch reset",
            monotonic_s(),
        )

    def report(self) -> CadenceReport:
        return CadenceReport(
            state=self._state,
            tier=self._tier,
            requested_hz=self._tier.fps,
            samples=self._samples,
            processed_ratio=round(self._last_processed_ratio, 4),
            observation_loss=round(self._last_loss, 4),
            p95_age_ms=self._last_p95_age_ms,
            live_eligible=self._live_eligible(),
            reason=self._reason or f"{self._state.value} at {self._tier.fps} Hz",
            changes=self._changes,
            probes=self._probes,
            failed_probes=self._failed_probes,
        )

    def _live_eligible(self) -> bool:
        """Cadence permission for Live, and nothing else.

        Deliberately strict: STABLE only, at or above the 30 Hz floor, with the
        loss and age budgets met and enough samples to mean it.
        """
        config = self._config
        return (
            self._state is GovernorState.STABLE
            and self._tier.acceptable
            and self._samples >= config.min_tier_samples
            and self._last_processed_ratio >= config.live_min_processed_ratio
            and self._last_loss <= config.live_max_observation_loss
            and (
                self._last_p95_age_ms is None
                or self._last_p95_age_ms <= self._live_age_ceiling_ms()
            )
        )

    def _live_age_ceiling_ms(self) -> float:
        """Two frame intervals, but never above the absolute Live ceiling."""
        two_intervals = 2000.0 / float(self._tier.fps)
        return min(two_intervals, float(self._config.live_max_age_ms))

    def _index(self) -> int:
        return self.LADDER.index(self._tier)

    def _set_tier(self, tier: PerformanceTier, reason: str, now_s: float | None = None) -> None:
        if tier is self._tier:
            return
        previous = self._tier
        self._tier = tier
        self._changes += 1
        self._reason = reason
        # A tier change is a discontinuity: the old samples describe a
        # different cadence and must not be averaged across it.
        self._samples = 0
        self._consecutive_bad = 0
        self._consecutive_good = 0
        self._healthy_since_s = None
        self._stable_since_s = None
        self._announce(previous, tier, reason, monotonic_s() if now_s is None else now_s)

    # -- the update -------------------------------------------------------
    def update(
        self,
        *,
        unique_fps: float,
        frame_age_ms: float | None,
        now_s: float,
        processed_fps: float | None = None,
        p95_age_ms: float | None = None,
        observation_loss: float = 0.0,
        stale_recent: int = 0,
        pool_exhausted_recent: int = 0,
        settling: bool = False,
    ) -> PerformanceTier:
        """One poll.

        ``processed_fps`` is ``None`` only when there is **no consumer** - a
        capture-only probe, or Shadow not started - and then capture stands
        in for it. With a consumer attached a processed rate of zero is a real
        zero and is judged as one; the previous code let it fall back to the
        capture rate, which is how a stalled worker read as healthy.

        ``settling`` polls are recorded and skipped: the pipeline is inside a
        cadence, source, geometry or profile change, or the backend has not
        acknowledged a reconfiguration yet, and the new tier is not judged on
        frames the old one produced.
        """
        config = self._config
        target = float(self._tier.fps)
        if settling:
            self._settling_polls += 1
            return self._tier
        if unique_fps <= 0.0:
            # No measurement yet - starting up, or between sessions. Judging a
            # tier on an empty window would downshift a healthy pipeline before
            # it has delivered its first frame.
            if self._state is not GovernorState.WARMUP:
                self._state = GovernorState.WARMUP
            return self._tier

        self._samples += 1
        useful_fps = unique_fps if processed_fps is None else min(unique_fps, processed_fps)
        self._last_processed_ratio = useful_fps / target if target else 0.0
        self._last_loss = max(0.0, observation_loss)
        self._last_p95_age_ms = p95_age_ms

        problems: list[str] = []
        # A tier is judged on **processed** throughput against what the tier
        # below could deliver: 52 processed frames a second at 60 Hz with 13%
        # superseded is a better pipeline than 29 at 30 Hz with none, because
        # a latest-only slot supersedes by design. So a shortfall - and
        # observation loss with it - is a problem only when this tier is no
        # longer delivering more than the next one down would.
        index = self._index()
        below = float(self.LADDER[index - 1].fps) if index > 0 else 0.0
        # "Ahead" means this tier processes more frames than the tier below
        # could even deliver: 57 processed at 90 Hz is a 60 Hz pipeline wearing
        # a 90 Hz label, 52 processed at 60 Hz is not a 30 Hz one.
        keeps_ahead = useful_fps > below
        if self._state is GovernorState.PROBE:
            # A probe holds only if it delivers more than the tier it left.
            origin = self._probe_from.fps if self._probe_from is not None else below
            if useful_fps < origin * config.probe_gain:
                problems.append(f"only {useful_fps:.0f} useful fps of {target:.0f}")
        elif useful_fps < target * config.downshift_ratio and not keeps_ahead:
            problems.append(f"only {useful_fps:.0f} useful fps of {target:.0f}")
        if frame_age_ms is not None and frame_age_ms > config.max_frame_age_ms:
            problems.append(f"frame age {frame_age_ms:.0f} ms over budget")
        if p95_age_ms is not None and p95_age_ms > config.max_frame_age_ms:
            problems.append(f"p95 age {p95_age_ms:.0f} ms over budget")
        if self._last_loss > config.max_observation_loss and not keeps_ahead:
            problems.append(f"observation loss {self._last_loss * 100:.0f}%")
        if stale_recent > config.max_stale_per_window:
            problems.append(f"{stale_recent} stale frames")
        if pool_exhausted_recent > 0:
            problems.append(f"buffer pool exhausted {pool_exhausted_recent}x")

        if problems:
            return self._on_bad(problems, now_s)
        return self._on_good(useful_fps, target, now_s)

    def _on_bad(self, problems: list[str], now_s: float) -> PerformanceTier:
        self._healthy_since_s = None
        self._consecutive_good = 0
        self._consecutive_bad += 1
        detail = "; ".join(problems)

        # A probe that does not immediately hold is a failed probe: fall back
        # at once and remember it, rather than spending another whole window.
        if self._state is GovernorState.PROBE and self._consecutive_bad >= 1:
            fallback = self._probe_from or self.LADDER[max(0, self._index() - 1)]
            self._ceiling_hint = fallback
            self._ceiling_until_s = now_s + self._config.ceiling_retry_after_s
            self._failed_probes += 1
            self._set_tier(fallback, f"probe to a higher tier failed: {detail}", now_s)
            self._state = GovernorState.COOLDOWN
            self._cooldown_until_s = now_s + self._config.probe_cooldown_s
            self._probe_from = None
            return self._tier

        # Two consecutive bad polls, so a single transient - a window resize, a
        # garbage collection, the first frames after start - does not cost a tier.
        if self._consecutive_bad >= self._config.downshift_polls:
            self._consecutive_bad = 0
            index = self._index()
            if index > 0:
                self._set_tier(self.LADDER[index - 1], f"downshift: {detail}", now_s)
                self._state = GovernorState.COOLDOWN
                self._cooldown_until_s = now_s + self._config.probe_cooldown_s
                self._reason = None if self._tier.acceptable else detail
                return self._tier

        if not self._tier.acceptable:
            self._state = GovernorState.DEGRADED
            self._reason = detail
        else:
            self._reason = None if self._state is not GovernorState.DEGRADED else detail
        return self._tier

    def _on_good(self, useful_fps: float, target: float, now_s: float) -> PerformanceTier:
        self._consecutive_bad = 0
        self._consecutive_good += 1
        self._reason = None
        if self._healthy_since_s is None:
            self._healthy_since_s = now_s

        if self._state is GovernorState.PROBE:
            # Hold the probe until it has been good for long enough to mean it.
            if self._consecutive_good >= self._config.probe_confirm_polls:
                self._state = GovernorState.STABLE
                self._stable_since_s = now_s
                self._probe_from = None
            return self._tier

        if not self._tier.acceptable:
            # Below the Live floor and healthy at it. DEGRADED is a verdict
            # about the tier, not a trap: with the polls good for long enough
            # the governor probes upward exactly as STABLE would, which is how
            # Auto recovers once the load that pushed it down has cleared.
            self._state = GovernorState.DEGRADED
            self._reason = f"below the {PerformanceTier.MINIMUM.fps} Hz Live floor"
            return self._maybe_probe(useful_fps, target, now_s)
        if self._consecutive_good >= self._config.min_stable_polls:
            cooldown_expired = (
                self._state is GovernorState.COOLDOWN and now_s >= self._cooldown_until_s
            )
            unsettled = self._state in (GovernorState.WARMUP, GovernorState.DEGRADED)
            if cooldown_expired or unsettled:
                self._state = GovernorState.STABLE
                self._stable_since_s = now_s

        if self._state is not GovernorState.STABLE:
            return self._tier
        return self._maybe_probe(useful_fps, target, now_s)

    def _maybe_probe(self, useful_fps: float, target: float, now_s: float) -> PerformanceTier:
        config = self._config
        index = self._index()
        ceiling = self.LADDER.index(self._ceiling)
        if self._ceiling_hint is not None:
            if now_s >= self._ceiling_until_s:
                # The cap has expired. Conditions change - a laptop cools down,
                # another application stops competing - so it is re-measured
                # rather than believed forever.
                self._ceiling_hint = None
            else:
                ceiling = min(ceiling, self.LADDER.index(self._ceiling_hint))
        if index >= ceiling:
            return self._tier
        if now_s < self._cooldown_until_s:
            return self._tier
        healthy_since = self._healthy_since_s
        if healthy_since is None or now_s - healthy_since < config.upshift_after_s:
            return self._tier
        # Only climb when the source is already saturating the current tier, so
        # we never chase a rate the display cannot produce.
        if useful_fps < target * config.upshift_saturation:
            return self._tier
        self._probe_from = self._tier
        self._probes += 1
        self._set_tier(self.LADDER[index + 1], "probing the next tier up", now_s)
        self._state = GovernorState.PROBE
        self._probe_started_s = now_s
        return self._tier

    def allow_retry_upward(self, now_s: float) -> None:
        """Forget a discovered ceiling early, on an explicit request.

        The ceiling expires on its own after ``ceiling_retry_after_s``; this is
        for the cases where the caller *knows* conditions changed, such as a
        source replacement.
        """
        del now_s
        self._ceiling_hint = None


# ---------------------------------------------------------------------------
# Viewport guard
# ---------------------------------------------------------------------------


class ViewportGuard:
    """The one authority on what the viewport is and whether it may be used.

    Detector readiness, coordinator readiness, the GUI, and Live gating all read
    this. That is why "viewport ok" can no longer coexist with "unsupported
    viewport size": there is a single :class:`ViewportState`, and a client that
    is not canonical says so in the state itself.

    Two distinct operations, deliberately not one button:

    ``connect()``
        Bind to the Roblox client exactly as it is. It never moves or resizes
        the user's window, and it is the recommended path - capture must not
        depend on a resize succeeding.

    ``fit_and_lock()``
        Optionally ask the OS for a canonical client, then *verify* it: three
        stable read-backs before the achieved size is believed, a monotonic
        deadline, and a hard attempt cap so a clamping window can never become
        a resize loop. A clamp is reported truthfully and the achieved geometry
        is adopted (plan 4.1, mission section 4).

    ``geometry_revision`` increments on every change of window, display, scale,
    size, or state. Everything derived from a frame - observations, tracker
    state, ROI state, actionable commands - is keyed by it, so a resize cannot
    leave a stale coordinate alive.
    """

    def __init__(
        self,
        port: PlatformPort,
        requested_client_logical: tuple[float, float] = (1280.0, 720.0),
        *,
        config: CaptureConfig | None = None,
        on_revision: Callable[[int, str], None] | None = None,
    ) -> None:
        self._port = port
        self._requested = requested_client_logical
        self._config = config or CaptureConfig()
        self._on_revision = on_revision
        self._lock = threading.Lock()
        self._current = ViewportGeometry.unpinned()
        self._adopted_identity: tuple[object, ...] | None = None
        self._revision = 0
        self._fit = ViewportFit.idle()
        self._fitting = False
        #: While a deliberate geometry change is in flight, mismatch
        #: classification is suspended: every intermediate size an OS reports
        #: mid-resize would otherwise be read as "somebody moved the window".
        #: Bounded by a monotonic deadline so a transaction that dies without
        #: unwinding cannot fence the guard for the rest of the session.
        self._fence_depth = 0
        self._fence_reason = ""
        self._fence_until_s = 0.0

    @property
    def requested_client_logical(self) -> tuple[float, float]:
        return self._requested

    @property
    def geometry(self) -> ViewportGeometry:
        with self._lock:
            return self._current

    @property
    def state(self) -> ViewportState:
        return self.geometry.state

    @property
    def revision(self) -> int:
        """Bumped whenever the coordinate basis changes. Never decreases."""
        with self._lock:
            return self._revision

    @property
    def fit(self) -> ViewportFit:
        with self._lock:
            return self._fit

    @property
    def fitting(self) -> bool:
        with self._lock:
            return self._fitting

    @property
    def fenced(self) -> bool:
        """Whether mismatch classification is currently suspended."""
        with self._lock:
            return self._fence_active_locked(monotonic_s())

    @property
    def fence_reason(self) -> str:
        with self._lock:
            return self._fence_reason if self._fence_active_locked(monotonic_s()) else ""

    def _fence_active_locked(self, now_s: float) -> bool:
        return self._fence_depth > 0 and now_s < self._fence_until_s

    @contextlib.contextmanager
    def transaction(self, reason: str, *, deadline_s: float | None = None) -> Iterator[None]:
        """Fence mismatch classification for one deliberate geometry change.

        ``check()`` and ``confirm_capture()`` are honest reporters: they see a
        window that is not the size we adopted and say so. During a resize that
        is exactly what we asked for, so classifying it as CAPTURE_MISMATCH
        restarts capture, churns the source epoch and blanks the preview for a
        change that was going to succeed (D-035). Inside this block they leave
        the adopted geometry alone; the transaction adopts the settled result
        itself.

        Re-entrant, and bounded by ``fit_transaction_deadline_s`` so a worker
        that dies mid-fit cannot leave the guard permanently blind.
        """
        default = self._config.fit_transaction_deadline_s
        limit = deadline_s if deadline_s is not None else default
        with self._lock:
            self._fence_depth += 1
            self._fence_reason = reason
            self._fence_until_s = max(self._fence_until_s, monotonic_s() + limit)
        try:
            yield
        finally:
            with self._lock:
                self._fence_depth = max(0, self._fence_depth - 1)
                if self._fence_depth == 0:
                    self._fence_reason = ""
                    self._fence_until_s = 0.0

    # -- internal ---------------------------------------------------------
    def _adopt_locked(self, geometry: ViewportGeometry, reason: str) -> bool:
        """Install ``geometry``; return whether the revision advanced.

        Caller holds ``_lock``. The identity comparison is what decides: two
        geometries that describe the same window at the same size on the same
        display are the same basis, however many times they are re-read.
        """
        previous = self._current
        changed = previous.identity() != geometry.identity()
        self._current = geometry
        self._adopted_identity = geometry.identity() if geometry.valid else None
        if changed:
            self._revision += 1
        return changed

    def _publish(self, changed: bool, reason: str) -> None:
        if changed and self._on_revision is not None:
            with contextlib.suppress(Exception):
                self._on_revision(self.revision, reason)

    # -- connect ----------------------------------------------------------
    def connect(self) -> ViewportGeometry:
        """Bind to the Roblox client as it is. Moves nothing, sends nothing.

        Observation and recording must not require resizing someone's game. A
        non-canonical client is normalized by letterboxing into the canonical
        raster and reported as ``ADOPTED_NONCANONICAL`` so nothing downstream
        mistakes it for a calibrated viewport.
        """
        geometry = self._port.window_geometry()
        if geometry.valid and geometry.state is not ViewportState.CANONICAL_VERIFIED:
            geometry = geometry.with_state(
                ViewportState.ADOPTED_NONCANONICAL,
                "adopted the window as-is; calibrated pixel constants do not apply",
            )
        with self._lock:
            changed = self._adopt_locked(geometry, "connect")
        self._publish(changed, "connect")
        return geometry

    #: Retained name for the pre-split call. ``connect`` is what this does.
    adopt_current = connect

    # -- fit and lock -----------------------------------------------------
    def fit_and_lock(
        self,
        *,
        sleep: Callable[[float], None] | None = None,
        now: Callable[[], float] | None = None,
    ) -> ViewportFit:
        """Resize the client to the canonical size and verify what was achieved.

        Bounded on every axis: at most ``fit_max_attempts`` requests, a
        monotonic deadline over the whole thing, and three consecutive
        read-backs that agree before an achieved size is believed. A window
        that clamps returns ``ACHIEVED_CLAMPED`` with the real numbers rather
        than being asked again.

        ``sleep``/``now`` are injectable so the state machine is testable
        without wall-clock time.
        """
        wait = sleep if sleep is not None else _sleep
        clock = now if now is not None else monotonic_s
        config = self._config
        with self._lock:
            if self._fitting:
                return self._fit
            self._fitting = True
        with self.transaction("viewport fit"):
            return self._fit_locked(wait, clock, config)

    def _fit_locked(
        self,
        wait: Callable[[float], None],
        clock: Callable[[], float],
        config: CaptureConfig,
    ) -> ViewportFit:
        started = clock()
        deadline = started + config.fit_deadline_s
        want = self._requested
        try:
            attempt = 0
            last_detail = "no attempt was made"
            while attempt < config.fit_max_attempts and clock() < deadline:
                attempt += 1
                self._set_fit(
                    FitPhase.REQUESTED, attempt, 0, want, None, None, None, "resize requested"
                )
                result = self._port.pin_client_rect(want)
                last_detail = result.message
                if not result.ok:
                    # The request itself was refused - permission, fullscreen,
                    # no window. Reading the size back would only report that
                    # nothing changed, which is not the same answer as a clamp.
                    return self._set_fit(
                        FitPhase.FAILED,
                        attempt,
                        0,
                        want,
                        None,
                        None,
                        result.geometry or self._port.window_geometry(),
                        result.message,
                        started_at_s=started,
                    )
                settled = self._settle(attempt, want, deadline, wait, clock)
                if settled is not None:
                    return settled
            fit = self._set_fit(
                FitPhase.FAILED,
                attempt,
                0,
                want,
                None,
                None,
                self._port.window_geometry(),
                f"the client size never settled: {last_detail}",
                started_at_s=started,
            )
            return fit
        finally:
            with self._lock:
                self._fitting = False

    def _settle(
        self,
        attempt: int,
        want: tuple[float, float],
        deadline: float,
        wait: Callable[[float], None],
        clock: Callable[[], float],
    ) -> ViewportFit | None:
        """Read the client back until it stops moving, or the deadline passes.

        Returns a terminal fit, or ``None`` to let the caller try once more.
        """
        config = self._config
        required = config.fit_required_readbacks
        stable = 0
        previous: tuple[float, float] | None = None
        geometry = self._port.window_geometry()
        while clock() < deadline:
            geometry = self._port.window_geometry()
            client = geometry.client_logical
            if client is None or not geometry.valid:
                stable, previous = 0, None
                self._set_fit(
                    FitPhase.SETTLING,
                    attempt,
                    stable,
                    want,
                    None,
                    None,
                    geometry,
                    f"client not readable: {geometry.detail}",
                )
                wait(config.fit_readback_interval_s)
                continue
            size = (round(client.width, 2), round(client.height, 2))
            stable = stable + 1 if previous is not None and size == previous else 1
            previous = size
            self._set_fit(
                FitPhase.SETTLING,
                attempt,
                stable,
                want,
                size,
                geometry.client_backing_px,
                geometry,
                "waiting for the client size to stop moving",
            )
            if stable >= required:
                return self._finish(attempt, want, size, geometry, stable)
            wait(config.fit_readback_interval_s)
        return None

    def _finish(
        self,
        attempt: int,
        want: tuple[float, float],
        size: tuple[float, float],
        geometry: ViewportGeometry,
        stable: int,
    ) -> ViewportFit:
        """Classify a settled read-back and adopt it."""
        tolerance = self._config.fit_tolerance_logical
        exact = abs(size[0] - want[0]) <= tolerance and abs(size[1] - want[1]) <= tolerance
        if exact:
            adopted = geometry.with_state(
                ViewportState.CANONICAL_VERIFIED,
                f"client verified at {size[0]:g}x{size[1]:g} pt over {stable} read-backs",
            )
            phase, detail = (
                FitPhase.CANONICAL_VERIFIED,
                f"verified {size[0]:g}x{size[1]:g} pt / "
                f"{geometry.client_backing_px[0]}x{geometry.client_backing_px[1]} px",
            )
        else:
            adopted = geometry.with_state(
                ViewportState.ADOPTED_NONCANONICAL,
                "the OS or the game clamped the request; running on the achieved size",
            )
            phase, detail = (
                FitPhase.ACHIEVED_CLAMPED,
                f"clamped to {size[0]:g}x{size[1]:g} pt (asked for "
                f"{want[0]:g}x{want[1]:g}); observation and recording work, "
                f"calibrated pixel constants do not",
            )
        with self._lock:
            changed = self._adopt_locked(adopted, "fit")
        self._publish(changed, f"fit:{phase.value}")
        return self._set_fit(
            phase,
            attempt,
            stable,
            want,
            size,
            adopted.client_backing_px,
            adopted,
            detail,
            settled=True,
        )

    def _set_fit(
        self,
        phase: FitPhase,
        attempt: int,
        stable: int,
        want: tuple[float, float] | None,
        achieved: tuple[float, float] | None,
        backing: tuple[int, int] | None,
        geometry: ViewportGeometry | None,
        detail: str,
        *,
        started_at_s: float = 0.0,
        settled: bool = False,
    ) -> ViewportFit:
        fit = ViewportFit(
            phase=phase,
            attempt=attempt,
            stable_readbacks=stable,
            required_readbacks=self._config.fit_required_readbacks,
            requested_client_logical=want,
            achieved_client_logical=achieved,
            achieved_client_backing_px=backing,
            geometry=geometry,
            detail=detail,
            started_at_s=started_at_s,
            settled_at_s=monotonic_s() if settled else None,
        )
        with self._lock:
            self._fit = fit
        return fit

    def pin(self) -> tuple[bool, str, ViewportGeometry]:
        """Backwards-compatible one-shot fit. Prefer :meth:`fit_and_lock`."""
        fit = self.fit_and_lock()
        geometry = fit.geometry or self.geometry
        return (fit.phase is FitPhase.CANONICAL_VERIFIED, fit.describe(), geometry)

    # -- lifecycle --------------------------------------------------------
    def invalidate(self, reason: str) -> None:
        with self._lock:
            changed = self._adopt_locked(ViewportGeometry.invalid(reason), reason)
        self._publish(changed, f"invalidate:{reason}")

    def check(self) -> ViewportGeometry:
        """Re-read the window and detect replacement, resize, or display change.

        A no-op while a geometry transaction is fenced: the intermediate sizes
        a resize passes through are expected, not evidence of interference.
        """
        with self._lock:
            if self._fence_active_locked(monotonic_s()):
                return self._current
        fresh = self._port.window_geometry()
        with self._lock:
            adopted = self._adopted_identity
            if adopted is None:
                self._current = (
                    fresh
                    if fresh.state is ViewportState.INVALID
                    else ViewportGeometry.unpinned("window found but not adopted")
                )
                return self._current
            if not fresh.valid:
                changed = self._adopt_locked(fresh, "window lost")
                result = self._current
            elif fresh.identity() != adopted:
                mismatch = fresh.with_state(
                    ViewportState.CAPTURE_MISMATCH,
                    f"window changed since adoption: {fresh.describe()}",
                )
                changed = self._adopt_locked(mismatch, "window changed")
                result = self._current
            else:
                changed = False
                self._current = fresh
                result = fresh
        self._publish(changed, "check")
        return result

    def confirm_capture(self, delivered_px: tuple[int, int]) -> ViewportGeometry:
        """Cross-check delivered frame size against the geometry we believe.

        A mismatch here is the signal that a resize or display migration landed
        between geometry and delivery, which is exactly when a stale transform
        would silently corrupt every coordinate.
        """
        with self._lock:
            current = self._current
            if not current.valid:
                return current
            if self._fence_active_locked(monotonic_s()):
                # Frames captured against the pre-resize geometry are still in
                # flight; the transaction restarts capture when it settles.
                return current
            mismatch = current.with_state(
                ViewportState.CAPTURE_MISMATCH,
                f"delivered {delivered_px[0]}x{delivered_px[1]} but expected "
                f"{current.canonical_px[0]}x{current.canonical_px[1]}",
            )
            changed = self._adopt_locked(mismatch, "capture mismatch")
            result = self._current
        self._publish(changed, "capture-mismatch")
        return result


# ---------------------------------------------------------------------------
# Evidence registry
# ---------------------------------------------------------------------------


class EvidenceRegistry:
    """Mints the opaque tokens that authorize acting on one specific frame.

    The mint key lives in :mod:`prospector_engine.contracts` and only this class
    holds it, so feature code cannot construct a token; the authority also checks
    object identity against what was registered.
    """

    def __init__(
        self, run_id: str, on_token: Callable[[EvidenceToken], None] | None = None
    ) -> None:
        self._run_id = run_id
        self._on_token = on_token
        self._generation = 0
        self._lock = threading.Lock()

    def set_generation(self, generation: int) -> None:
        with self._lock:
            self._generation = generation

    def envelope_for(self, frame: CapturedFrame) -> FrameEnvelope:
        with self._lock:
            generation = self._generation
        token = EvidenceToken(
            run_id=self._run_id,
            generation=generation,
            frame_sequence=frame.sequence,
            captured_at_s=frame.captured_at_s,
            duration_ms=frame.duration_ms,
            viewport_identity=frame.geometry.identity(),
            _mint_key=EVIDENCE_MINT_KEY,
        )
        if self._on_token is not None:
            self._on_token(token)
        return FrameEnvelope(frame=frame, evidence_token=token)


# ---------------------------------------------------------------------------
# Portable fallback source
# ---------------------------------------------------------------------------


class MssCaptureSource:
    """Desktop-rectangle fallback, in **logical** coordinates.

    This is the backend the coordinate bug lived in: ``mss`` speaks the display's
    logical space, and it was being handed device pixels. It now takes the client
    rect in logical units and asserts the delivered size, which is what the
    coordinate-space conformance test checks.

    It captures a desktop region rather than a window, so anything overlapping
    Roblox lands in the frame. It is a last resort, used only when no
    window-specific backend is available.
    """

    name = "mss-logical"

    def __init__(self) -> None:
        self._sct: Any = None
        self._geometry: ViewportGeometry | None = None
        self._pool: Any = None
        self._sequence = 0
        self._error: str | None = None

    @property
    def is_pushing(self) -> bool:
        return False

    def start(
        self, geometry: ViewportGeometry, pool: Any, on_frame: Callable[[RawFrame], None]
    ) -> None:
        del on_frame  # pull source
        import mss

        factory = getattr(mss, "MSS", None) or mss.mss
        self._sct = factory()
        self._geometry = geometry
        self._pool = pool
        self._error = None

    def set_target_fps(self, fps: int) -> None:
        del fps

    def stop(self) -> None:
        if self._sct is not None:
            with contextlib.suppress(Exception):
                self._sct.close()
        self._sct = None
        self._geometry = None

    def health(self) -> str | None:
        return self._error

    def poll(self) -> RawFrame | None:
        geometry, sct = self._geometry, self._sct
        if geometry is None or sct is None or geometry.client_logical is None:
            return None
        client = geometry.client_logical
        region = {
            "left": round(client.x),
            "top": round(client.y),
            "width": round(client.width),
            "height": round(client.height),
        }
        started = monotonic_s()
        try:
            shot = sct.grab(region)
        except Exception as exc:
            self._error = f"mss grab failed: {exc!r}"
            return None
        raw = np.asarray(shot)[:, :, :3]
        normalize_started = monotonic_s()
        canonical = normalize_into_canonical(raw, geometry, self._pool)
        if canonical is None:
            self._error = "buffer pool exhausted"
            return None
        normalize_ms = (monotonic_s() - normalize_started) * 1000.0
        self._sequence += 1
        self._error = None
        return RawFrame(
            bgr=canonical,
            geometry=geometry,
            captured_at_s=started,
            presented_at_s=monotonic_s(),
            content_id=None,  # mss has no source-side frame identity
            backend=self.name,
            normalize_ms=normalize_ms,
        )


def normalize_into_canonical(
    source_bgr: NDArray[np.uint8], geometry: ViewportGeometry, pool: FrameBufferPool | None
) -> NDArray[np.uint8] | None:
    """Letterbox an arbitrary client image into the canonical raster, once.

    "Once" is the point: every consumer downstream works in canonical
    coordinates, so no later stage has to resize, and the letterbox rectangle
    recorded on the geometry is the exact inverse of what happens here.

    Returns ``None`` when the buffer pool is exhausted, which the caller reports
    as backpressure rather than growing memory to hide it.

    Shared by every CPU-normalizing backend on both platforms; the
    ScreenCaptureKit path does the equivalent work on the GPU.
    """
    import cv2

    width, height = geometry.canonical_px
    target = (
        pool.acquire(height, width)
        if pool is not None
        else np.empty((height, width, 3), dtype=np.uint8)
    )
    if target is None:
        return None
    inner_x, inner_y, inner_w, inner_h = geometry.canonical_letterbox_px()
    inner_w = max(1, min(inner_w, width - inner_x))
    inner_h = max(1, min(inner_h, height - inner_y))
    if (inner_x, inner_y, inner_w, inner_h) != (0, 0, width, height):
        target[:] = 0
    cv2.resize(
        source_bgr,
        (inner_w, inner_h),
        dst=target[inner_y : inner_y + inner_h, inner_x : inner_x + inner_w],
        interpolation=cv2.INTER_AREA,
    )
    return target


# ---------------------------------------------------------------------------
# Capture service
# ---------------------------------------------------------------------------


class CaptureService:
    """Owns the source, the latest-frame slot, the governor, and the metrics.

    Push backends deliver on their own thread and the service does no polling at
    all. Pull backends get a single paced thread. Either way, exactly one frame
    is live in the slot and consumers wake on publication.
    """

    def __init__(
        self,
        guard: ViewportGuard,
        registry: EvidenceRegistry,
        *,
        config: CaptureConfig | None = None,
        source_factory: Callable[[], CaptureSource] | None = None,
        on_frame: Callable[[FrameEnvelope], None] | None = None,
    ) -> None:
        self._guard = guard
        self._registry = registry
        self._config = config or CaptureConfig()
        self._source_factory = source_factory
        self._on_frame = on_frame

        self._slot = LatestFrameSlot()
        self._pool = FrameBufferPool(self._config.pool_capacity)
        self._governor = CadenceGovernor(self._config)
        self._usage = _ProcessUsage()
        #: Bounded per-frame tracing. Owned here because this is where every
        #: other measurement already lands.
        self.trace = TraceRing()
        self._governor.set_transition_hook(self.trace.record_transition)
        self._settling_until_s = 0.0

        self._capture_latency = LatencyTracker("capture", self._config.latency_window)
        self._normalize_latency = LatencyTracker("normalize", self._config.latency_window)
        self._perception_latency = LatencyTracker("perception", self._config.latency_window)
        self._decision_latency = LatencyTracker("decision", self._config.latency_window)
        self._preview_latency = LatencyTracker("preview", self._config.latency_window)
        self._end_to_end = LatencyTracker("end-to-end", self._config.latency_window)

        self._source_rate = _RateCounter()
        self._unique_rate = _RateCounter()
        self._processed_rate = _RateCounter()
        self._control_rate = _RateCounter()
        self._preview_rate = _RateCounter()
        self._duplicate_rate = _RateCounter()
        self._superseded_rate = _RateCounter()
        self._unobserved_rate = _RateCounter()
        self._stale_rate = _RateCounter()
        self._exhausted_rate = _RateCounter()

        self._lock = threading.Lock()
        self._source: CaptureSource | None = None
        self._sequence = 0
        self._duplicate_run = 0
        self._reacquisitions = 0
        #: Incremented whenever the frame source is created, replaced, or dies.
        #: Every observation is keyed by it, so frames from a dead source can
        #: never be drawn over frames from a live one.
        self._source_epoch = 0
        self._last_content_id: int | None = None
        self._last_signature: int | None = None
        self._last_success_s: float | None = None
        self._last_error: str | None = None
        self._stale_in_window = 0
        self._exhausted_seen = 0
        self._cadence_mode = CadenceMode.AUTO

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._supervisor_thread: threading.Thread | None = None
        #: True from a successful start() call until stop(). Distinguishes a
        #: deliberate stop from `self._source` merely being None because the
        #: last (re)acquisition attempt failed - the latter must keep retrying.
        self._should_run = False
        self._reacquire_delay_s = self._config.reacquire_initial_delay_s
        self._next_reacquire_s = 0.0
        self._reacquiring = False

    # -- accessors --------------------------------------------------------
    @property
    def config(self) -> CaptureConfig:
        return self._config

    @property
    def slot(self) -> LatestFrameSlot:
        return self._slot

    @property
    def tier(self) -> PerformanceTier:
        return self._governor.tier

    @property
    def backend_name(self) -> str:
        source = self._source
        return source.name if source is not None else "none"

    @property
    def running(self) -> bool:
        with self._lock:
            return self._source is not None

    def health(self) -> str | None:
        """The current capture problem, or ``None`` when there is none.

        A description rather than a boolean because "why is there no frame" is
        the question a stalled setup has to answer, and "no" is not an answer.
        """
        with self._lock:
            error = self._last_error
            running = self._source is not None
        if error is not None:
            return error
        if not running:
            return "capture is not running"
        return None

    @property
    def source_epoch(self) -> int:
        with self._lock:
            return self._source_epoch

    @property
    def governor(self) -> CadenceGovernor:
        return self._governor

    @property
    def cadence_mode(self) -> CadenceMode:
        return self._cadence_mode

    def set_cadence_mode(self, mode: CadenceMode) -> None:
        """Change the cadence ceiling, and start a fresh measurement epoch.

        A mode change is a discontinuity like any other: averaging a rate
        across it describes neither side, so the epoch resets and the governor
        re-earns whatever tier it ends up on.
        """
        if mode is self._cadence_mode:
            return
        self._cadence_mode = mode
        self._governor.set_bounds(start=mode.start_tier, ceiling=mode.max_tier)
        self.reset_epoch(f"cadence mode: {mode.value}")
        source = self._source
        if source is not None:
            with contextlib.suppress(Exception):
                source.set_target_fps(self._governor.tier.fps)

    @property
    def settling(self) -> bool:
        """Inside a settling period, or waiting for the backend to acknowledge
        a reconfiguration. Frames observed now are tagged, not judged."""
        if monotonic_s() < self._settling_until_s:
            return True
        source = self._source
        return bool(getattr(source, "reconfiguring", False)) if source is not None else False

    def reset_epoch(self, reason: str) -> None:
        """Start a new measurement epoch across every rate, every latency
        window and the governor, atomically enough that no consumer can read
        a rate from one epoch beside a latency from another.

        Called on start, source replacement, reacquisition, cadence, geometry
        and profile changes: a rate averaged across a discontinuity describes
        neither side of it, and a cumulative counter that survives one is
        unreadable. The latency *history* is kept for diagnostics; only the
        recent window the governor judges on restarts.
        """
        now = monotonic_s()
        for tracker in (
            self._capture_latency,
            self._normalize_latency,
            self._perception_latency,
            self._decision_latency,
            self._preview_latency,
            self._end_to_end,
        ):
            tracker.start_epoch(now)
        self._settling_until_s = now + self._config.settle_s
        for counter in (
            self._source_rate,
            self._unique_rate,
            self._processed_rate,
            self._control_rate,
            self._preview_rate,
            self._duplicate_rate,
            self._superseded_rate,
            self._unobserved_rate,
            self._stale_rate,
            self._exhausted_rate,
        ):
            counter.reset_session()
        self._slot.reset_session()
        with self._lock:
            self._stale_in_window = 0
            self._exhausted_seen = self._pool.exhausted
        self._governor.reset_epoch(reason)

    def latest(self) -> FrameEnvelope | None:
        return self._slot.peek()

    def wait_for_new(self, after_sequence: int, timeout_s: float) -> FrameEnvelope | None:
        return self._slot.wait_for_new(after_sequence, timeout_s)

    @property
    def processed_fps(self) -> float:
        """Perception ticks per second. Cheap enough for the control loop.

        ``metrics()`` builds a full snapshot and is a dashboard call; this is
        the one number the controller's cadence check needs, read straight off
        the rate counter.
        """
        return self._processed_rate.rate()

    def latest_age_s(self) -> float | None:
        envelope = self._slot.peek()
        return None if envelope is None else envelope.frame.age_s(monotonic_s())

    def last_error(self) -> str | None:
        with self._lock:
            error = self._last_error
        source = self._source
        return error or (source.health() if source is not None else None)

    def stalled(self) -> bool:
        with self._lock:
            last = self._last_success_s
        if last is None:
            return self.running
        return (monotonic_s() - last) * 1000.0 > self._config.stall_after_ms

    def duplicate_run(self) -> int:
        with self._lock:
            return self._duplicate_run

    def over_duplicate_budget(self) -> bool:
        return self.duplicate_run() > self._config.duplicate_budget

    # -- consumer instrumentation ----------------------------------------
    def note_perception_ms(self, milliseconds: float) -> None:
        self._perception_latency.record_ms(milliseconds)
        self._processed_rate.tick()

    def note_decision_ms(self, milliseconds: float) -> None:
        self._decision_latency.record_ms(milliseconds)
        self._control_rate.tick()

    def note_preview_ms(self, milliseconds: float) -> None:
        self._preview_latency.record_ms(milliseconds)
        self._preview_rate.tick()

    def note_dropped_observation(self, count: int = 1) -> None:
        """Frames that existed but were never turned into an observation.

        Reported by the perception consumer from gaps in the frame sequence,
        which is the only place that difference is visible: the slot knows what
        it replaced, but only the consumer knows what it never saw.
        """
        if count > 0:
            self._unobserved_rate.tick(count=count)

    def note_end_to_end_ms(self, milliseconds: float) -> None:
        self._end_to_end.record_ms(milliseconds)

    # -- lifecycle --------------------------------------------------------
    def start(self) -> bool:
        # Marked True and the supervisor spun up before the geometry check
        # below can fail: a failed *first* attempt must still get retried by
        # the supervisor's own reacquire loop, not strand the service with no
        # source and nothing ever watching for one to become available.
        self._should_run = True
        self._stop.clear()
        if self._supervisor_thread is None or not self._supervisor_thread.is_alive():
            self._supervisor_thread = threading.Thread(
                target=self._supervisor_loop, name="treasure-cadence", daemon=True
            )
            self._supervisor_thread.start()
        if self._source is not None:
            return True
        geometry = self._guard.geometry
        if not geometry.valid:
            geometry = self._guard.adopt_current()
        if not geometry.valid:
            with self._lock:
                self._last_error = f"viewport {geometry.state.value}: {geometry.detail}"
            return False
        source = self._make_source()
        try:
            source.start(geometry, self._pool, self._on_raw_frame)
        except Exception as exc:
            with self._lock:
                self._last_error = f"source start failed: {exc!r}"
            return False
        source.set_target_fps(self._governor.tier.fps)
        with self._lock:
            self._source = source
            self._source_epoch += 1
            self._last_error = None
        self.reset_epoch("capture started")
        self._slot.set_supersede_hook(self._superseded_rate.tick)
        if not source.is_pushing:
            self._thread = threading.Thread(
                target=self._pull_loop, name="treasure-capture", daemon=True
            )
            self._thread.start()
        self._reacquire_delay_s = self._config.reacquire_initial_delay_s
        return True

    def _make_source(self) -> CaptureSource:
        if self._source_factory is not None:
            return self._source_factory()
        return MssCaptureSource()

    def stop(self, timeout_s: float = 1.0) -> bool:
        self._should_run = False
        self._stop.set()
        source = self._source
        with self._lock:
            self._source = None
        if source is not None:
            with contextlib.suppress(Exception):
                source.stop()
        joined = True
        for thread in (self._thread, self._supervisor_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout_s)
                joined = joined and not thread.is_alive()
        self._thread = None
        self._supervisor_thread = None
        self._slot.clear()
        self._pool.clear()
        return joined

    def restart_source(self, reason: str) -> bool:
        """Tear the source down and build a fresh one against current geometry.

        Called by the supervisor on window replacement, display change, source
        death, or a stall - never in a loop, always behind the backoff.
        """
        with self._lock:
            self._reacquisitions += 1
            self._last_error = f"reacquiring: {reason}"
            self._reacquiring = True
        try:
            source = self._source
            with self._lock:
                self._source = None
            if source is not None:
                with contextlib.suppress(Exception):
                    source.stop()
            pull_thread = self._thread
            self._thread = None
            self._stop.set()
            if pull_thread is not None and pull_thread is not threading.current_thread():
                pull_thread.join(1.0)
            self._stop.clear()
            self._slot.clear()
            self._guard.connect()
            return self.start()
        finally:
            with self._lock:
                self._reacquiring = False

    # -- frame intake -----------------------------------------------------
    def _on_raw_frame(self, raw: RawFrame) -> None:
        """Called on the backend's own thread for push sources. Must be quick."""
        now = monotonic_s()
        self._source_rate.tick(now)

        duplicate = self._is_duplicate(raw)
        with self._lock:
            if duplicate:
                self._duplicate_run += 1
            else:
                self._duplicate_run = 0
            self._last_success_s = now

        if duplicate:
            self._duplicate_rate.tick(now)
            self._pool.release(raw.bgr)
            return

        # Cross-check the delivery against the guard's *authoritative* geometry,
        # not against what the frame says about itself. A resize or display
        # migration that lands between geometry and delivery shows up here, and
        # is exactly when a stale transform would corrupt every coordinate.
        delivered = raw.size_px
        expected = self._guard.geometry
        if tuple(delivered) != tuple(expected.canonical_px) or not raw.geometry.same_source(
            expected
        ):
            self._guard.confirm_capture(delivered)
            self._pool.release(raw.bgr)
            with self._lock:
                self._last_error = (
                    f"delivered {delivered[0]}x{delivered[1]} from "
                    f"{raw.geometry.state.value} but the viewport is "
                    f"{expected.canonical_px[0]}x{expected.canonical_px[1]} "
                    f"{expected.state.value}"
                )
            return
        geometry = raw.geometry

        self._unique_rate.tick(now)
        self._capture_latency.record_ms((raw.presented_at_s - raw.captured_at_s) * 1000.0)
        if raw.normalize_ms:
            self._normalize_latency.record_ms(raw.normalize_ms)

        with self._lock:
            self._sequence += 1
            sequence = self._sequence
            self._last_error = None

        frame = CapturedFrame(
            sequence=sequence,
            captured_at_s=raw.captured_at_s,
            completed_at_s=raw.presented_at_s,
            duration_ms=(raw.presented_at_s - raw.captured_at_s) * 1000.0,
            geometry=geometry,
            bgr=freeze_array(raw.bgr),
            duplicate=False,
            capture_error=None,
            content_id=raw.content_id,
            backend=raw.backend,
        )
        # The pooled buffer returns the moment nothing references the frame any
        # more. Tying it to the frame's lifetime rather than to a fixed lag is
        # what keeps memory flat without ever recycling a buffer a consumer is
        # still reading from.
        weakref.finalize(frame, self._pool.release, raw.bgr)

        envelope = self._registry.envelope_for(frame)
        self._slot.publish(envelope)
        if self._on_frame is not None:
            with contextlib.suppress(Exception):
                self._on_frame(envelope)

    def _is_duplicate(self, raw: RawFrame) -> bool:
        """Source identity when the backend offers it, a cheap digest otherwise.

        Counting a redelivered surface as a new frame would inflate the very
        number the governor and the UI treat as evidence of health.
        """
        if raw.content_id is not None:
            with self._lock:
                same = raw.content_id == self._last_content_id
                self._last_content_id = raw.content_id
            return same
        image = raw.bgr
        step_y = max(1, image.shape[0] // 24)
        step_x = max(1, image.shape[1] // 24)
        signature = int(image[::step_y, ::step_x].sum())
        with self._lock:
            same = signature == self._last_signature
            self._last_signature = signature
        return same

    def _pull_loop(self) -> None:
        source = self._source
        if source is None:
            return
        while not self._stop.is_set():
            started = monotonic_s()
            try:
                raw = source.poll()
            except Exception as exc:
                with self._lock:
                    self._last_error = f"poll failed: {exc!r}"
                raw = None
            if raw is not None:
                self._on_raw_frame(raw)
            elapsed = monotonic_s() - started
            interval = self._governor.tier.interval_s
            if self._stop.wait(max(0.0, interval - elapsed)):
                return

    def _supervisor_loop(self) -> None:
        """Cadence governance and bounded reacquisition, on one timer.

        Two jobs that both need a steady heartbeat and neither of which may
        block the frame path.
        """
        while not self._stop.wait(self._config.supervisor_interval_s):
            now = monotonic_s()
            age_ms = None
            envelope = self._slot.peek()
            stale_now = 0
            if envelope is not None:
                age_ms = envelope.frame.age_s(now) * 1000.0
                if age_ms > self._config.max_frame_age_ms:
                    self._stale_rate.tick(now)
                    stale_now = 1
            with self._lock:
                seen = self._exhausted_seen
                self._exhausted_seen = self._pool.exhausted
                exhausted_now = max(0, self._pool.exhausted - seen)
            if exhausted_now:
                self._exhausted_rate.tick(now, count=exhausted_now)

            before = self._governor.tier
            consumer = self._slot.has_consumer
            recent = self._end_to_end.recent(self._config.recent_window_s, now)
            after = self._governor.update(
                unique_fps=self._unique_rate.rate(),
                frame_age_ms=age_ms,
                now_s=now,
                # A real zero when a consumer exists; capture stands in only
                # when nobody is consuming at all.
                processed_fps=self._processed_rate.rate() if consumer else None,
                p95_age_ms=recent.p95_ms if recent.samples else None,
                observation_loss=self._observation_loss(),
                stale_recent=stale_now,
                pool_exhausted_recent=exhausted_now,
                settling=self.settling,
            )
            if after is not before:
                source = self._source
                if source is not None:
                    with contextlib.suppress(Exception):
                        source.set_target_fps(after.fps)
                # The new tier is judged only after the change has settled
                # and the backend has acknowledged it.
                self._settling_until_s = now + self._config.settle_s

            self._maybe_reacquire(now)

    def _observation_loss(self) -> float:
        """Share of delivered frames that never became an observation.

        Zero while nothing is consuming: with no perception running there is no
        observation to lose, and reporting 100% loss for an idle pipeline is
        exactly the sort of alarming nonsense this rewrite exists to remove.
        """
        processed = self._processed_rate.rate()
        if processed <= 0.0:
            return 0.0
        missed = self._unobserved_rate.rate()
        return missed / (processed + missed)

    def _reacquire_reason(self) -> str | None:
        """Why the source should be rebuilt, or ``None`` when it is fine."""
        if not self._should_run:
            return None  # deliberately stopped
        source = self._source
        if source is None:
            # start() or a previous restart_source() failed to acquire one -
            # not a deliberate stop, so keep retrying behind the backoff.
            return "no source: retrying after a failed acquisition"
        geometry = self._guard.check()
        if geometry.state is ViewportState.CAPTURE_MISMATCH:
            return f"viewport changed: {geometry.detail}"
        if geometry.state is ViewportState.INVALID:
            return f"window lost: {geometry.detail}"
        if geometry.state is ViewportState.UNPINNED:
            # A running source only exists after an earlier successful
            # adopt, so UNPINNED here is check() having lost its adopted
            # identity to one transient bad read, not "never connected."
            # guard.connect() (inside restart_source) re-adopts if the
            # window is actually fine; nothing else ever un-poisons this.
            return f"viewport lost its pin: {geometry.detail}"
        health = source.health()
        if health is not None:
            return f"source unhealthy: {health}"
        if self.stalled():
            return "no frames within the stall budget"
        return None

    def _maybe_reacquire(self, now_s: float) -> None:
        if self._stop.is_set():
            return  # a stop in flight must never be undone by a retry
        with self._lock:
            if self._reacquiring or now_s < self._next_reacquire_s:
                return
        reason = self._reacquire_reason()
        if reason is None:
            with self._lock:
                self._reacquire_delay_s = self._config.reacquire_initial_delay_s
            return
        with self._lock:
            delay = self._reacquire_delay_s
            # Exponential, capped. A window that is gone for good costs one
            # retry every few seconds rather than a busy loop, and the retry
            # structure never grows.
            self._reacquire_delay_s = min(self._config.reacquire_max_delay_s, delay * 2.0)
            self._next_reacquire_s = now_s + delay
        self.restart_source(reason)

    # -- metrics ----------------------------------------------------------
    def metrics(self) -> CaptureMetrics:
        usage = self._usage.sample()
        envelope = self._slot.peek()
        age_ms = None if envelope is None else envelope.frame.age_s(monotonic_s()) * 1000.0
        with self._lock:
            reacquisitions = self._reacquisitions
            epoch = self._source_epoch
        unique = self._unique_rate.rate()
        report = self._governor.report()
        reason = self._governor.degraded_reason
        if reason is None and self.running and 0.0 < unique < PerformanceTier.MINIMUM.fps:
            reason = (
                f"only {unique:.0f} unique fps; below the {PerformanceTier.MINIMUM.fps} minimum"
            )
        return CaptureMetrics(
            backend=self.backend_name,
            tier=report.tier,
            requested_hz=report.requested_hz,
            source_fps=self._source_rate.rate(),
            unique_fps=unique,
            processed_fps=self._processed_rate.rate(),
            control_fps=self._control_rate.rate(),
            preview_fps=self._preview_rate.rate(),
            duplicate_frames=self._duplicate_rate.summary("duplicate frames"),
            superseded_frames=RateSummary(
                "superseded frames",
                self._slot.superseded,
                self._superseded_rate.rate(),
                self._slot.lifetime_dropped,
            ),
            dropped_observations=self._unobserved_rate.summary("unobserved frames"),
            stale_frames=self._stale_rate.summary("stale frames"),
            pool_exhausted=self._exhausted_rate.summary("pool exhausted"),
            slot_depth=1 if envelope is not None else 0,
            reacquisitions=reacquisitions,
            frame_age_ms=age_ms,
            capture=self._capture_latency.summary(),
            normalize=self._normalize_latency.summary(),
            perception=self._perception_latency.summary(),
            decision=self._decision_latency.summary(),
            preview=self._preview_latency.summary(),
            end_to_end=self._end_to_end.summary(),
            cpu_percent=usage.cpu_percent,
            rss_current_mb=usage.rss_current_mb,
            rss_peak_mb=usage.rss_peak_mb,
            governor=report,
            epoch=epoch,
            degraded_reason=reason,
            end_to_end_recent=self._end_to_end.recent(self._config.recent_window_s),
            settling=self.settling,
            consumer_attached=self._slot.has_consumer,
        )

    def export_trace(self, directory: Path | str, *, label: str = "trace") -> Path | None:
        """Write the bounded trace rings as JSONL. Best effort, never raises."""
        try:
            stamp = int(monotonic_s() * 1000.0)
            target = Path(directory) / f"{label}-epoch{self._source_epoch}-{stamp}.jsonl"
            return self.trace.export_jsonl(target)
        except Exception:
            return None


def canonical_size() -> tuple[int, int]:
    return CANONICAL_SIZE_PX
