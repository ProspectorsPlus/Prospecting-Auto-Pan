"""Bounded per-frame tracing: where the time goes and where the mistakes are.

The Shadow regression that motivated this module was reported as "15 fps and
264 ms p95" - a symptom. Symptom-level metrics cannot say whether the time went
into capture, into waiting for the worker, into the detector's region pass,
into a full-frame fallback, into direction estimation, or into the preview.
This module records one :class:`FrameTrace` per unique processed frame with
every stage timed separately, plus preview timings and governor transitions,
in fixed-capacity rings that are exported as JSONL on request.

Bounded by construction: every ring is a ``deque(maxlen=...)``; there is no
image queue and no unbounded log. Recording a trace costs one dataclass and one
append under a lock, which is what makes it safe to leave on in production.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "FrameTrace",
    "GovernorTransition",
    "PerceptionTiming",
    "PreviewTrace",
    "TraceRing",
    "TraceSummary",
]


@dataclass(frozen=True)
class PerceptionTiming:
    """What one perception pass did, and how long each part took.

    Produced by :class:`prospector_engine.navigation.PerceptionPipeline`; it is
    the perception half of a :class:`FrameTrace` and is also exposed on the
    diagnostic packet so the dashboard can show it without a second source.
    """

    #: Whether a region-of-interest search was attempted.
    roi_used: bool
    #: Choosing and cropping the region. Zero when no region was used.
    roi_proposal_ms: float
    #: The detector pass over the region. Zero when no region was used.
    roi_detector_ms: float
    #: The detector pass over the full canonical frame. Zero when skipped.
    full_detector_ms: float
    #: Whether the full-frame pass ran *because* the region pass missed.
    fallback: bool
    #: Connected components found before any bound was applied.
    raw_components: int
    #: Components that were actually measured and scored.
    components_evaluated: int
    #: Total pixels of every per-candidate mask allocated in this pass.
    mask_pixels_allocated: int
    #: Direction estimation on the selected candidate.
    direction_ms: float
    #: The tracker's verdict in one word: ``acquire``, ``track``, ``hold``,
    #: ``ambiguous``, ``reacquire``, ``lost``, ``none``.
    tracking_decision: str
    #: Which candidate the observation was built from, or ``None``.
    selected_candidate_id: int | None
    confidence: float
    #: Why the other candidates were not selected, one entry each, bounded.
    rejection_reasons: tuple[str, ...] = ()
    #: The temporal state the detector was in after this frame.
    track_state: str = ""

    @property
    def detector_ms(self) -> float:
        return self.roi_detector_ms + self.full_detector_ms

    @property
    def total_ms(self) -> float:
        return self.roi_proposal_ms + self.detector_ms + self.direction_ms


@dataclass(frozen=True)
class FrameTrace:
    """One unique captured frame, from the backend callback to the observation."""

    frame_sequence: int
    captured_at_s: float
    completed_at_s: float
    source_epoch: int
    cadence_hz: int
    #: Backend callback plus copy into the pooled buffer.
    capture_ms: float
    #: From the frame being published to the worker picking it up.
    scheduling_delay_ms: float
    perception: PerceptionTiming
    decision_ms: float
    #: From capture to the observation being published. The number Live is
    #: judged on; every other field explains it.
    capture_to_observation_ms: float
    #: Set on frames processed during a cadence, source, geometry, or profile
    #: change, so a settling period is visible rather than averaged in.
    settling: bool = False

    def as_row(self) -> dict[str, Any]:
        row = asdict(self)
        perception = row.pop("perception")
        row.update({f"perception_{key}": value for key, value in perception.items()})
        row["perception_rejection_reasons"] = list(self.perception.rejection_reasons)
        return row


@dataclass(frozen=True)
class PreviewTrace:
    """One dashboard render, timed separately from perception.

    The preview runs on the Tk thread and is latest-only, so it has its own
    ring: a slow render must be visible as a slow *render*, never disguised as
    slow perception.
    """

    frame_sequence: int
    at_s: float
    #: Resize, colour conversion and the paste into the PhotoImage.
    paste_ms: float
    #: Everything drawn on top of the image.
    overlay_ms: float
    overlay_mode: str
    #: Whether this render was skipped because the previous one had not
    #: finished (latest-only means skipping, never queueing).
    skipped: bool = False

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GovernorTransition:
    """A cadence tier or state change and the reason the governor gave."""

    at_s: float
    from_hz: int
    to_hz: int
    state: str
    reason: str

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TraceSummary:
    """Percentiles over the ring, for a report line or a test assertion."""

    frames: int
    fields: Mapping[str, tuple[float, float, float, float]]
    fallbacks: int
    tracking_decisions: Mapping[str, int]
    preview_renders: int
    preview_skipped: int
    transitions: int

    def describe(self) -> str:
        lines = [f"frames traced: {self.frames}  fallbacks: {self.fallbacks}"]
        for name, (p50, p95, p99, top) in self.fields.items():
            lines.append(
                f"  {name:<28} p50 {p50:7.2f}  p95 {p95:7.2f}  p99 {p99:7.2f}  max {top:7.2f}"
            )
        decisions = ", ".join(f"{k}={v}" for k, v in sorted(self.tracking_decisions.items()))
        lines.append(f"  tracking: {decisions or 'none'}")
        lines.append(
            f"  preview renders {self.preview_renders} (skipped {self.preview_skipped})"
            f"  governor transitions {self.transitions}"
        )
        return "\n".join(lines)


def _percentiles(values: Sequence[float]) -> tuple[float, float, float, float]:
    if not values:
        return (0.0, 0.0, 0.0, 0.0)
    ordered = sorted(values)
    last = len(ordered) - 1

    def at(fraction: float) -> float:
        return ordered[min(last, max(0, round(fraction * last)))]

    return (at(0.5), at(0.95), at(0.99), ordered[-1])


_TIMING_FIELDS: tuple[str, ...] = (
    "capture_ms",
    "scheduling_delay_ms",
    "perception_roi_proposal_ms",
    "perception_roi_detector_ms",
    "perception_full_detector_ms",
    "perception_direction_ms",
    "decision_ms",
    "capture_to_observation_ms",
)

_COUNT_FIELDS: tuple[str, ...] = (
    "perception_raw_components",
    "perception_components_evaluated",
    "perception_mask_pixels_allocated",
)


class TraceRing:
    """Fixed-capacity rings for frame, preview, and governor traces."""

    def __init__(self, capacity: int = 4096) -> None:
        self._frames: deque[FrameTrace] = deque(maxlen=capacity)
        self._previews: deque[PreviewTrace] = deque(maxlen=capacity)
        self._transitions: deque[GovernorTransition] = deque(maxlen=max(64, capacity // 8))
        self._lock = threading.Lock()
        self._recorded = 0

    @property
    def capacity(self) -> int:
        return self._frames.maxlen or 0

    @property
    def recorded(self) -> int:
        """Frames ever recorded, including those the ring has since evicted."""
        with self._lock:
            return self._recorded

    def record(self, trace: FrameTrace) -> None:
        with self._lock:
            self._frames.append(trace)
            self._recorded += 1

    def record_preview(self, trace: PreviewTrace) -> None:
        with self._lock:
            self._previews.append(trace)

    def record_transition(self, transition: GovernorTransition) -> None:
        with self._lock:
            self._transitions.append(transition)

    def frames(self) -> tuple[FrameTrace, ...]:
        with self._lock:
            return tuple(self._frames)

    def previews(self) -> tuple[PreviewTrace, ...]:
        with self._lock:
            return tuple(self._previews)

    def transitions(self) -> tuple[GovernorTransition, ...]:
        with self._lock:
            return tuple(self._transitions)

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()
            self._previews.clear()
            self._transitions.clear()

    def summary(self, *, exclude_settling: bool = True) -> TraceSummary:
        frames = self.frames()
        if exclude_settling:
            frames = tuple(trace for trace in frames if not trace.settling)
        rows = [trace.as_row() for trace in frames]
        fields: dict[str, tuple[float, float, float, float]] = {}
        for name in _TIMING_FIELDS + _COUNT_FIELDS:
            fields[name] = _percentiles([float(row[name]) for row in rows])
        decisions: dict[str, int] = {}
        for trace in frames:
            decisions[trace.perception.tracking_decision] = (
                decisions.get(trace.perception.tracking_decision, 0) + 1
            )
        previews = self.previews()
        return TraceSummary(
            frames=len(frames),
            fields=fields,
            fallbacks=sum(1 for trace in frames if trace.perception.fallback),
            tracking_decisions=decisions,
            preview_renders=sum(1 for preview in previews if not preview.skipped),
            preview_skipped=sum(1 for preview in previews if preview.skipped),
            transitions=len(self.transitions()),
        )

    def export_jsonl(self, path: Path | str) -> Path:
        """Write every ring as JSON lines, one ``kind`` field per row.

        Bounded by the ring sizes, so the file can never grow past a few
        megabytes however long the session ran.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            for kind, rows in (
                ("frame", self.frames()),
                ("preview", self.previews()),
                ("governor", self.transitions()),
            ):
                for item in rows:
                    handle.write(json.dumps({"kind": kind, **item.as_row()}) + "\n")
        return target


def load_jsonl(path: Path | str) -> Iterable[dict[str, Any]]:
    """Read rows back, for the A/B report and the tests."""
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)
