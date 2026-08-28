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
import resource
import threading
import weakref
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from prospector_engine.contracts import (
    EVIDENCE_MINT_KEY,
    CapturedFrame,
    CaptureMetrics,
    EvidenceStatus,
    EvidenceToken,
    FrameEnvelope,
    LatencySummary,
    PerformanceTier,
    Provenance,
    RawFrame,
    freeze_array,
    monotonic_s,
)
from prospector_engine.geometry import (
    CANONICAL_SIZE_PX,
    ViewportGeometry,
    ViewportState,
)
from prospector_engine.ports import CaptureSource, PlatformPort

__all__ = [
    "CadenceGovernor",
    "CaptureConfig",
    "CaptureService",
    "EvidenceRegistry",
    "FrameBufferPool",
    "LatencyTracker",
    "LatestFrameSlot",
    "MssCaptureSource",
    "ViewportGuard",
]


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
    #: Reacquisition backoff. Bounded and capped so a window that is gone for
    #: good costs a retry every few seconds, not a busy loop.
    reacquire_initial_delay_s: float = 0.25
    reacquire_max_delay_s: float = 4.0
    supervisor_interval_s: float = 0.25
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="TREASURE_NAVIGATION_PLAN.md section 7.4 E-PERF; DECISIONS.md D-019",
            note="tier ladder and hysteresis chosen from local measurement; E-PERF is PENDING",
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
    """Capacity-one, drop-oldest, wake-on-publish.

    ``wait_for_new`` is what makes the pipeline event-driven: a consumer blocks
    until a frame *newer than the one it already saw* exists, so there is no
    polling interval and no backlog to fall behind on.
    """

    def __init__(self) -> None:
        self._value: FrameEnvelope | None = None
        self._sequence = 0
        self._taken_sequence = 0
        self._lock = threading.Condition()
        self.dropped = 0

    def publish(self, envelope: FrameEnvelope) -> None:
        with self._lock:
            # A drop is a frame that was replaced before any consumer saw it -
            # not merely a slot that was occupied, which is the normal state.
            if self._value is not None and self._sequence > self._taken_sequence:
                self.dropped += 1
            self._value = envelope
            self._sequence = envelope.frame.sequence
            self._lock.notify_all()

    def peek(self) -> FrameEnvelope | None:
        with self._lock:
            return self._value

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._sequence

    def wait_for_new(self, after_sequence: int, timeout_s: float) -> FrameEnvelope | None:
        """Block until a frame newer than ``after_sequence`` is available."""
        deadline = monotonic_s() + timeout_s
        with self._lock:
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


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class LatencyTracker:
    """A bounded ring of durations with percentiles. No unbounded history."""

    def __init__(self, label: str, window: int = 240) -> None:
        self._label = label
        self._samples: deque[float] = deque(maxlen=window)
        self._lock = threading.Lock()

    def record_ms(self, milliseconds: float) -> None:
        with self._lock:
            self._samples.append(milliseconds)

    def summary(self) -> LatencySummary:
        with self._lock:
            values = sorted(self._samples)
        if not values:
            return LatencySummary(self._label, 0, 0.0, 0.0, 0.0, 0.0)

        def at(fraction: float) -> float:
            index = min(len(values) - 1, max(0, round(fraction * (len(values) - 1))))
            return values[index]

        return LatencySummary(self._label, len(values), at(0.5), at(0.95), at(0.99), values[-1])


class _RateCounter:
    """Events per second over a sliding window, for genuinely-unique counting."""

    def __init__(self, window_s: float = 2.0) -> None:
        self._window_s = window_s
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()
        self.total = 0

    def tick(self, now_s: float | None = None) -> None:
        now = now_s if now_s is not None else monotonic_s()
        with self._lock:
            self.total += 1
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


class _ProcessUsage:
    """CPU percent and RSS from ``resource``, so no extra dependency is needed."""

    def __init__(self) -> None:
        self._last_cpu_s = self._cpu_seconds()
        self._last_wall_s = monotonic_s()
        self._percent = 0.0

    @staticmethod
    def _cpu_seconds() -> float:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return usage.ru_utime + usage.ru_stime

    def sample(self) -> tuple[float, float]:
        now_wall = monotonic_s()
        now_cpu = self._cpu_seconds()
        elapsed = now_wall - self._last_wall_s
        if elapsed >= 0.25:
            self._percent = 100.0 * (now_cpu - self._last_cpu_s) / elapsed
            self._last_wall_s, self._last_cpu_s = now_wall, now_cpu
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # macOS reports bytes, Linux kilobytes.
        rss_mb = rss / (1024 * 1024) if rss > 1 << 22 else rss / 1024
        return (self._percent, rss_mb)


# ---------------------------------------------------------------------------
# Cadence governor
# ---------------------------------------------------------------------------


class CadenceGovernor:
    """Picks the highest cadence tier the source and machine actually sustain.

    Rules that matter more than the ladder itself:

    * A tier is only kept if *unique* frames arrive at a useful fraction of it.
      Redelivered surfaces never justify staying high.
    * Downshift is immediate on sustained shortfall; upshift needs a quiet
      period, so the two cannot oscillate against each other.
    * A high nominal rate with old frames is always worse than a lower rate
      with fresh ones, so frame age downshifts regardless of throughput.
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
        self._healthy_since_s: float | None = None
        self._reason: str | None = None
        self._changes = 0
        self._consecutive_bad = 0

    @property
    def tier(self) -> PerformanceTier:
        return self._tier

    @property
    def degraded_reason(self) -> str | None:
        return self._reason

    @property
    def changes(self) -> int:
        return self._changes

    def _index(self) -> int:
        return self.LADDER.index(self._tier)

    def update(
        self, *, unique_fps: float, frame_age_ms: float | None, now_s: float
    ) -> PerformanceTier:
        target = float(self._tier.fps)
        if unique_fps <= 0.0:
            # No measurement yet - starting up, or between sessions. Judging a
            # tier on an empty window would downshift a healthy pipeline before
            # it has delivered its first frame.
            return self._tier
        keeping_up = unique_fps >= target * self._config.downshift_ratio
        fresh = frame_age_ms is None or frame_age_ms <= self._config.max_frame_age_ms

        if not keeping_up or not fresh:
            self._healthy_since_s = None
            self._consecutive_bad += 1
            # Two consecutive bad polls, so a single transient - a window
            # resize, a garbage collection, the first frames after start - does
            # not knock a healthy pipeline down a tier.
            if self._consecutive_bad >= 2:
                self._consecutive_bad = 0
                index = self._index()
                if index > 0:
                    self._tier = self.LADDER[index - 1]
                    self._changes += 1
            self._reason = (
                f"unique {unique_fps:.0f}/s below {target:.0f}/s"
                if not keeping_up
                else f"frame age {frame_age_ms:.0f} ms over budget"
            )
            if self._tier.acceptable:
                self._reason = None
            return self._tier

        self._consecutive_bad = 0
        self._reason = None
        if self._healthy_since_s is None:
            self._healthy_since_s = now_s
        index = self._index()
        ceiling = self.LADDER.index(self._config.max_tier)
        if (
            index < ceiling
            and now_s - self._healthy_since_s >= self._config.upshift_after_s
            # Only climb when the source is already saturating the current tier,
            # so we never chase a rate the display cannot produce.
            and unique_fps >= target * 0.95
        ):
            self._tier = self.LADDER[index + 1]
            self._changes += 1
            self._healthy_since_s = now_s
        return self._tier


# ---------------------------------------------------------------------------
# Viewport guard
# ---------------------------------------------------------------------------


class ViewportGuard:
    """The one authority on what the viewport is and whether it may be used.

    Detector readiness, coordinator readiness, the GUI, and Live gating all read
    this. That is why "viewport ok" can no longer coexist with "unsupported
    viewport size": there is a single :class:`ViewportState`, and a client that
    is not canonical says so in the state itself.
    """

    def __init__(
        self,
        port: PlatformPort,
        requested_client_logical: tuple[float, float] = (1280.0, 720.0),
    ) -> None:
        self._port = port
        self._requested = requested_client_logical
        self._lock = threading.Lock()
        self._current = ViewportGeometry.unpinned()
        self._adopted_identity: tuple[object, ...] | None = None

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

    def pin(self) -> tuple[bool, str, ViewportGeometry]:
        """Ask the OS for a canonical client. Reports what was actually achieved."""
        result = self._port.pin_client_rect(self._requested)
        geometry = result.geometry or self._port.window_geometry()
        with self._lock:
            self._current = geometry
            self._adopted_identity = geometry.identity() if geometry.valid else None
        return (result.ok, result.message, geometry)

    def adopt_current(self) -> ViewportGeometry:
        """Take the client area as it is, without moving the user's window.

        Observation and recording should not require resizing someone's game.
        A non-canonical client is normalized by letterboxing into the canonical
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
            self._current = geometry
            self._adopted_identity = geometry.identity() if geometry.valid else None
        return geometry

    def invalidate(self, reason: str) -> None:
        with self._lock:
            self._current = ViewportGeometry.invalid(reason)
            self._adopted_identity = None

    def check(self) -> ViewportGeometry:
        """Re-read the window and detect replacement, resize, or display change."""
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
                self._current = fresh
                return self._current
            if fresh.identity() != adopted:
                self._current = fresh.with_state(
                    ViewportState.CAPTURE_MISMATCH,
                    f"window changed since adoption: {fresh.describe()}",
                )
                return self._current
            self._current = fresh
            return self._current

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
            self._current = current.with_state(
                ViewportState.CAPTURE_MISMATCH,
                f"delivered {delivered_px[0]}x{delivered_px[1]} but expected "
                f"{current.canonical_px[0]}x{current.canonical_px[1]}",
            )
            return self._current


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
        canonical = _normalize_to_canonical(raw, geometry, self._pool)
        if canonical is None:
            self._error = "buffer pool exhausted"
            return None
        self._sequence += 1
        self._error = None
        return RawFrame(
            bgr=canonical,
            geometry=geometry,
            captured_at_s=started,
            presented_at_s=monotonic_s(),
            content_id=None,  # mss has no source-side frame identity
            backend=self.name,
        )


def _normalize_to_canonical(
    source_bgr: NDArray[np.uint8], geometry: ViewportGeometry, pool: FrameBufferPool | None
) -> NDArray[np.uint8] | None:
    """Letterbox an arbitrary client image into the canonical raster, once.

    "Once" is the point: every consumer downstream works in canonical
    coordinates, so no later stage has to resize, and the transform stored on
    the geometry is the exact inverse of what happened here.
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

        self._capture_latency = LatencyTracker("capture", self._config.latency_window)
        self._normalize_latency = LatencyTracker("normalize", self._config.latency_window)
        self._perception_latency = LatencyTracker("perception", self._config.latency_window)
        self._decision_latency = LatencyTracker("decision", self._config.latency_window)
        self._preview_latency = LatencyTracker("preview", self._config.latency_window)
        self._end_to_end = LatencyTracker("end-to-end", self._config.latency_window)

        self._source_rate = _RateCounter()
        self._unique_rate = _RateCounter()
        self._processed_rate = _RateCounter()
        self._preview_rate = _RateCounter()

        self._lock = threading.Lock()
        self._source: CaptureSource | None = None
        self._sequence = 0
        self._duplicates = 0
        self._duplicate_run = 0
        self._stale = 0
        self._dropped_observations = 0
        self._reacquisitions = 0
        self._last_content_id: int | None = None
        self._last_signature: int | None = None
        self._last_success_s: float | None = None
        self._last_error: str | None = None

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._supervisor_thread: threading.Thread | None = None
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

    def latest(self) -> FrameEnvelope | None:
        return self._slot.peek()

    def wait_for_new(self, after_sequence: int, timeout_s: float) -> FrameEnvelope | None:
        return self._slot.wait_for_new(after_sequence, timeout_s)

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

    def note_preview_ms(self, milliseconds: float) -> None:
        self._preview_latency.record_ms(milliseconds)
        self._preview_rate.tick()

    def note_dropped_observation(self) -> None:
        with self._lock:
            self._dropped_observations += 1

    def note_end_to_end_ms(self, milliseconds: float) -> None:
        self._end_to_end.record_ms(milliseconds)

    # -- lifecycle --------------------------------------------------------
    def start(self) -> bool:
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
            self._last_error = None
        self._stop.clear()
        if not source.is_pushing:
            self._thread = threading.Thread(
                target=self._pull_loop, name="treasure-capture", daemon=True
            )
            self._thread.start()
        self._reacquire_delay_s = self._config.reacquire_initial_delay_s
        if self._supervisor_thread is None or not self._supervisor_thread.is_alive():
            self._supervisor_thread = threading.Thread(
                target=self._supervisor_loop, name="treasure-cadence", daemon=True
            )
            self._supervisor_thread.start()
        return True

    def _make_source(self) -> CaptureSource:
        if self._source_factory is not None:
            return self._source_factory()
        return MssCaptureSource()

    def stop(self, timeout_s: float = 1.0) -> bool:
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
            self._guard.adopt_current()
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
                self._duplicates += 1
                self._duplicate_run += 1
            else:
                self._duplicate_run = 0
            self._last_success_s = now

        if duplicate:
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
            if envelope is not None:
                age_ms = envelope.frame.age_s(now) * 1000.0
                if age_ms > self._config.max_frame_age_ms:
                    with self._lock:
                        self._stale += 1

            before = self._governor.tier
            after = self._governor.update(
                unique_fps=self._unique_rate.rate(), frame_age_ms=age_ms, now_s=now
            )
            if after is not before:
                source = self._source
                if source is not None:
                    with contextlib.suppress(Exception):
                        source.set_target_fps(after.fps)

            self._maybe_reacquire(now)

    def _reacquire_reason(self) -> str | None:
        """Why the source should be rebuilt, or ``None`` when it is fine."""
        source = self._source
        if source is None:
            return None  # deliberately stopped
        geometry = self._guard.check()
        if geometry.state is ViewportState.CAPTURE_MISMATCH:
            return f"viewport changed: {geometry.detail}"
        if geometry.state is ViewportState.INVALID:
            return f"window lost: {geometry.detail}"
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
        cpu, rss = self._usage.sample()
        envelope = self._slot.peek()
        age_ms = None if envelope is None else envelope.frame.age_s(monotonic_s()) * 1000.0
        with self._lock:
            duplicates = self._duplicates
            stale = self._stale
            dropped_observations = self._dropped_observations
            reacquisitions = self._reacquisitions
        unique = self._unique_rate.rate()
        reason = self._governor.degraded_reason
        if reason is None and self.running and unique < PerformanceTier.MINIMUM.fps:
            reason = (
                f"only {unique:.0f} unique fps; below the {PerformanceTier.MINIMUM.fps} minimum"
            )
        return CaptureMetrics(
            backend=self.backend_name,
            tier=self._governor.tier,
            source_fps=self._source_rate.rate(),
            unique_fps=unique,
            processed_fps=self._processed_rate.rate(),
            preview_fps=self._preview_rate.rate(),
            duplicate_frames=duplicates,
            dropped_frames=self._slot.dropped,
            dropped_observations=dropped_observations,
            stale_frames=stale,
            slot_depth=1 if envelope is not None else 0,
            reacquisitions=reacquisitions,
            frame_age_ms=age_ms,
            capture=self._capture_latency.summary(),
            normalize=self._normalize_latency.summary(),
            perception=self._perception_latency.summary(),
            decision=self._decision_latency.summary(),
            preview=self._preview_latency.summary(),
            end_to_end=self._end_to_end.summary(),
            cpu_percent=cpu,
            rss_mb=rss,
            degraded_reason=reason,
        )


def canonical_size() -> tuple[int, int]:
    return CANONICAL_SIZE_PX
