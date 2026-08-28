"""The real-frame corpus: labelled Roblox frames, split by contiguous sequence.

Rendered fixtures (``tests/arrow_fixtures.py``) are training stress and may
never pass a gate (plan 7.2). This module is the other half: frames taken from
real sessions, labelled by a reviewer, split **by contiguous sequence** so a
tuning frame and an evaluation frame are never neighbours, and evaluated with
the bounding box as well as the heading - because "confident lock on the wrong
object" is the failure this corpus exists to measure and a heading-only
evaluator cannot see it.

Layout of a corpus directory::

    labels.json
    <sequence-id>/<frame>.webp

``labels.json`` records provenance (what the frames were extracted from and
how), every sequence with its split and stratum, and per-frame labels in
**canonical 1280x720 coordinates**. Frames are stored at their extracted
resolution and letterboxed into the canonical raster on load, the same way the
capture service normalizes a live client.

Honesty rules baked in:

* an arrow-absent frame is a positive label, not a missing one;
* a frame the reviewer could not label is ``unknown`` and excluded from every
  rate, and counted separately so it cannot disappear;
* labels that were interpolated between reviewed keyframes say so;
* a frame where a previous build's overlay touched the arrow says so, because
  a drawn outline is a favourable bias on same-colour terrain.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from prospector_engine.contracts import CapturedFrame, freeze_array
from prospector_engine.geometry import (
    CANONICAL_SIZE_PX,
    DisplayInfo,
    LogicalRect,
    ViewportGeometry,
    ViewportState,
    WindowIdentity,
)

__all__ = [
    "ArrowLabel",
    "Corpus",
    "CorpusFrame",
    "CorpusSequence",
    "FramePrediction",
    "SequenceMetrics",
    "canonicalize",
    "corpus_geometry",
    "evaluate_corpus",
    "load_corpus",
]

SplitName = Literal["tune", "eval"]

#: A prediction overlaps the label when the intersection over the smaller box
#: reaches this. Loose on purpose: the label boxes come from a reference mask
#: and the detector's own box can legitimately be tighter or looser.
MATCH_OVERLAP = 0.30
#: Heading error above this is a wrong direction, not a noisy one.
BAD_ANGLE_DEG = 10.0
#: Sign is wrong when the error exceeds a right angle.
SIGN_FLIP_DEG = 90.0


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArrowLabel:
    """Where the arrow is and where it points, in canonical pixels."""

    bbox_px: tuple[int, int, int, int]
    #: Screen heading (0 up, +90 right), or ``None`` when the reviewer could
    #: read the position but not the direction.
    heading_deg: float | None
    occluded: bool = False
    near_edge: bool = False
    #: A previous build drew its detection outline on this arrow.
    overlay_contact: bool = False

    @property
    def centroid_px(self) -> tuple[float, float]:
        x, y, w, h = self.bbox_px
        return (x + w / 2.0, y + h / 2.0)


@dataclass(frozen=True)
class CorpusFrame:
    sequence_id: str
    file: str
    source_time_s: float
    source_frame: int
    #: ``None`` means the arrow is absent - a positive label.
    arrow: ArrowLabel | None
    #: The reviewer could not label this frame. Excluded from every rate.
    unknown: bool = False
    #: The label was interpolated between reviewed keyframes.
    interpolated: bool = False
    note: str = ""

    @property
    def arrow_present(self) -> bool:
        return self.arrow is not None


@dataclass(frozen=True)
class CorpusSequence:
    """A contiguous run of frames. Splitting happens here, never per frame."""

    sequence_id: str
    split: SplitName
    stratum: str
    lighting: str
    notes: str
    frames: tuple[CorpusFrame, ...]
    #: Which timeline ``source_time_s`` belongs to. Sequences from different
    #: sources never overlap in time however their clocks read.
    source: str = "recording"


@dataclass(frozen=True)
class Corpus:
    root: Path
    profile_id: str
    provenance: dict[str, Any]
    sequences: tuple[CorpusSequence, ...]
    #: How stored frames map into the canonical raster.
    stored_size_px: tuple[int, int]

    def split(self, name: SplitName) -> tuple[CorpusSequence, ...]:
        return tuple(sequence for sequence in self.sequences if sequence.split == name)

    def frames(self, name: SplitName | None = None) -> tuple[CorpusFrame, ...]:
        sequences = self.sequences if name is None else self.split(name)
        return tuple(frame for sequence in sequences for frame in sequence.frames)

    def load_bgr(self, frame: CorpusFrame) -> NDArray[np.uint8]:
        import cv2

        image = cv2.imread(str(self.root / frame.file), cv2.IMREAD_COLOR)
        if image is None:
            raise FileNotFoundError(self.root / frame.file)
        return canonicalize(np.asarray(image, dtype=np.uint8))

    def load_frame(self, frame: CorpusFrame, sequence: int) -> CapturedFrame:
        """A corpus frame as the pipeline would have received it."""
        return CapturedFrame(
            sequence=sequence,
            captured_at_s=frame.source_time_s,
            completed_at_s=frame.source_time_s + 0.004,
            duration_ms=4.0,
            geometry=corpus_geometry(),
            bgr=freeze_array(np.ascontiguousarray(self.load_bgr(frame))),
            backend="corpus",
        )


def corpus_geometry() -> ViewportGeometry:
    """The stored frames *are* the canonical raster."""
    client = LogicalRect(0.0, 0.0, float(CANONICAL_SIZE_PX[0]), float(CANONICAL_SIZE_PX[1]))
    return ViewportGeometry(
        state=ViewportState.CANONICAL_VERIFIED,
        window=WindowIdentity(0, 0, "corpus"),
        display=DisplayInfo("corpus", client, 1.0),
        frame_logical=client,
        client_logical=client,
        canonical_px=CANONICAL_SIZE_PX,
        detail="real-frame corpus",
    )


def canonicalize(bgr: NDArray[np.uint8]) -> NDArray[np.uint8]:
    """Letterbox a stored frame into the canonical raster with a uniform scale.

    The same rule the capture service applies to a non-canonical client: scale
    uniformly so angles survive, centre, and pad with black.
    """
    import cv2

    target_w, target_h = CANONICAL_SIZE_PX
    height, width = bgr.shape[:2]
    scale = min(target_w / width, target_h / height)
    content_w = max(1, round(width * scale))
    content_h = max(1, round(height * scale))
    resized = cv2.resize(bgr, (content_w, content_h), interpolation=cv2.INTER_CUBIC)
    canvas = np.zeros((target_h, target_w, 3), dtype=np.uint8)
    x0 = (target_w - content_w) // 2
    y0 = (target_h - content_h) // 2
    canvas[y0 : y0 + content_h, x0 : x0 + content_w] = resized
    return canvas


def stored_to_canonical(
    bbox: tuple[int, int, int, int], stored_size_px: tuple[int, int]
) -> tuple[int, int, int, int]:
    """Map a box in stored-image pixels to canonical pixels, matching
    :func:`canonicalize` exactly."""
    target_w, target_h = CANONICAL_SIZE_PX
    width, height = stored_size_px
    scale = min(target_w / width, target_h / height)
    content_w = round(width * scale)
    content_h = round(height * scale)
    x0 = (target_w - content_w) // 2
    y0 = (target_h - content_h) // 2
    x, y, w, h = bbox
    return (
        round(x * scale) + x0,
        round(y * scale) + y0,
        max(1, round(w * scale)),
        max(1, round(h * scale)),
    )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _label_from(entry: dict[str, Any] | None) -> ArrowLabel | None:
    if entry is None:
        return None
    bbox = entry["bbox_px"]
    return ArrowLabel(
        bbox_px=(int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])),
        heading_deg=None if entry.get("heading_deg") is None else float(entry["heading_deg"]),
        occluded=bool(entry.get("occluded", False)),
        near_edge=bool(entry.get("near_edge", False)),
        overlay_contact=bool(entry.get("overlay_contact", False)),
    )


def load_corpus(root: Path | str) -> Corpus:
    """Read ``labels.json``. Raises on a malformed split or a missing file."""
    base = Path(root)
    raw = json.loads((base / "labels.json").read_text(encoding="utf-8"))
    if int(raw.get("schema", 0)) != 1:
        raise ValueError(f"unsupported corpus schema {raw.get('schema')!r}")
    stored = raw["stored_size_px"]
    sequences: list[CorpusSequence] = []
    for entry in raw["sequences"]:
        split = entry["split"]
        if split not in ("tune", "eval"):
            raise ValueError(f"sequence {entry['id']!r} has split {split!r}")
        frames = tuple(
            CorpusFrame(
                sequence_id=entry["id"],
                file=item["file"],
                source_time_s=float(item["source_time_s"]),
                source_frame=int(item["source_frame"]),
                arrow=_label_from(item.get("arrow")),
                unknown=bool(item.get("unknown", False)),
                interpolated=bool(item.get("interpolated", False)),
                note=str(item.get("note", "")),
            )
            for item in entry["frames"]
        )
        for frame in frames:
            if not (base / frame.file).exists():
                raise FileNotFoundError(base / frame.file)
        sequences.append(
            CorpusSequence(
                sequence_id=entry["id"],
                split=split,
                stratum=entry["stratum"],
                lighting=entry.get("lighting", ""),
                notes=entry.get("notes", ""),
                frames=frames,
                source=str(entry.get("source", "recording")),
            )
        )
    return Corpus(
        root=base,
        profile_id=raw["profile_id"],
        provenance=dict(raw.get("provenance", {})),
        sequences=tuple(sequences),
        stored_size_px=(int(stored[0]), int(stored[1])),
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FramePrediction:
    """What a detector said about one frame. ``accepted`` with no box is a bug
    the evaluator reports rather than tolerates."""

    accepted: bool
    bbox_px: tuple[int, int, int, int] | None
    heading_deg: float | None
    track_id: int | None
    #: The tracker's one-word verdict, for the state histogram.
    decision: str = ""


@dataclass
class SequenceMetrics:
    """Counts first, rates second. Rates over a handful of frames mislead."""

    sequence_id: str
    stratum: str
    split: str
    frames: int = 0
    unknown: int = 0
    present: int = 0
    absent: int = 0
    #: Arrow present and the detector accepted a box overlapping the label.
    hits: int = 0
    #: Arrow present and nothing accepted.
    misses: int = 0
    #: Accepted a box that does not overlap the labelled arrow.
    false_locks: int = 0
    #: Arrow absent and something accepted.
    false_acquisitions: int = 0
    #: Track id changed between two consecutive hits.
    identity_switches: int = 0
    #: A switch that lasted exactly one frame before switching back.
    single_frame_replacements: int = 0
    heading_errors: list[float] = field(default_factory=list)
    sign_flips: int = 0
    decisions: dict[str, int] = field(default_factory=dict)

    @property
    def recall(self) -> float | None:
        return self.hits / self.present if self.present else None

    @property
    def absent_precision(self) -> float | None:
        return 1.0 - self.false_acquisitions / self.absent if self.absent else None

    @property
    def false_lock_rate(self) -> float | None:
        judged = self.present + self.absent
        return (self.false_locks + self.false_acquisitions) / judged if judged else None

    @property
    def p95_error_deg(self) -> float | None:
        return _percentile(self.heading_errors, 0.95) if self.heading_errors else None

    @property
    def median_error_deg(self) -> float | None:
        return _percentile(self.heading_errors, 0.5) if self.heading_errors else None

    @property
    def bad_angle_frames(self) -> int:
        return sum(1 for error in self.heading_errors if error > BAD_ANGLE_DEG)

    @property
    def sign_accuracy(self) -> float | None:
        judged = len(self.heading_errors)
        return (judged - self.sign_flips) / judged if judged else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence_id": self.sequence_id,
            "stratum": self.stratum,
            "split": self.split,
            "frames": self.frames,
            "unknown": self.unknown,
            "present": self.present,
            "absent": self.absent,
            "hits": self.hits,
            "misses": self.misses,
            "false_locks": self.false_locks,
            "false_acquisitions": self.false_acquisitions,
            "identity_switches": self.identity_switches,
            "single_frame_replacements": self.single_frame_replacements,
            "sign_flips": self.sign_flips,
            "heading_judged": len(self.heading_errors),
            "bad_angle_frames": self.bad_angle_frames,
            "recall": self.recall,
            "absent_precision": self.absent_precision,
            "false_lock_rate": self.false_lock_rate,
            "median_error_deg": self.median_error_deg,
            "p95_error_deg": self.p95_error_deg,
            "sign_accuracy": self.sign_accuracy,
            "decisions": dict(self.decisions),
        }

    def merge(self, other: SequenceMetrics) -> SequenceMetrics:
        merged = SequenceMetrics(self.sequence_id, self.stratum, self.split)
        for name in (
            "frames",
            "unknown",
            "present",
            "absent",
            "hits",
            "misses",
            "false_locks",
            "false_acquisitions",
            "identity_switches",
            "single_frame_replacements",
            "sign_flips",
        ):
            setattr(merged, name, getattr(self, name) + getattr(other, name))
        merged.heading_errors = [*self.heading_errors, *other.heading_errors]
        merged.decisions = dict(self.decisions)
        for key, value in other.decisions.items():
            merged.decisions[key] = merged.decisions.get(key, 0) + value
        return merged


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Intersection over the smaller box."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    left, top = max(ax, bx), max(ay, by)
    right, bottom = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if right <= left or bottom <= top:
        return 0.0
    return float((right - left) * (bottom - top)) / float(max(1, min(aw * ah, bw * bh)))


def _wrap(degrees: float) -> float:
    return (degrees + 180.0) % 360.0 - 180.0


def evaluate_corpus(
    corpus: Corpus,
    predict: Callable[[CapturedFrame], FramePrediction],
    *,
    split: SplitName | None = "eval",
    reset: Callable[[], None] | None = None,
    sequences: Iterable[str] | None = None,
) -> dict[str, SequenceMetrics]:
    """Run ``predict`` sequence by sequence and score every frame.

    ``reset`` is called before each sequence so temporal state never crosses a
    cut. Returns metrics keyed by sequence id plus ``"__all__"``; the caller
    aggregates per stratum from the per-sequence values.
    """
    wanted = None if sequences is None else set(sequences)
    results: dict[str, SequenceMetrics] = {}
    total = SequenceMetrics("__all__", "all", split or "all")
    for sequence in corpus.sequences if split is None else corpus.split(split):
        if wanted is not None and sequence.sequence_id not in wanted:
            continue
        if reset is not None:
            reset()
        metrics = SequenceMetrics(sequence.sequence_id, sequence.stratum, sequence.split)
        previous_track: int | None = None
        switch_pending = False
        previous_hit = False
        for index, frame in enumerate(sequence.frames):
            metrics.frames += 1
            captured = corpus.load_frame(frame, index + 1)
            prediction = predict(captured)
            metrics.decisions[prediction.decision] = (
                metrics.decisions.get(prediction.decision, 0) + 1
            )
            if frame.unknown:
                metrics.unknown += 1
                previous_hit = False
                continue
            label = frame.arrow
            if label is None:
                metrics.absent += 1
                if prediction.accepted:
                    metrics.false_acquisitions += 1
                previous_hit = False
                continue

            metrics.present += 1
            if not prediction.accepted or prediction.bbox_px is None:
                metrics.misses += 1
                previous_hit = False
                continue
            if _overlap(prediction.bbox_px, label.bbox_px) < MATCH_OVERLAP:
                metrics.false_locks += 1
                previous_hit = False
                continue
            metrics.hits += 1
            if (
                previous_hit
                and previous_track is not None
                and prediction.track_id != previous_track
            ):
                metrics.identity_switches += 1
                if switch_pending:
                    metrics.single_frame_replacements += 1
                switch_pending = True
            else:
                switch_pending = False
            previous_track = prediction.track_id
            previous_hit = True
            if label.heading_deg is not None and prediction.heading_deg is not None:
                error = abs(_wrap(prediction.heading_deg - label.heading_deg))
                metrics.heading_errors.append(error)
                if error > SIGN_FLIP_DEG:
                    metrics.sign_flips += 1
        results[sequence.sequence_id] = metrics
        total = total.merge(metrics)
    results["__all__"] = total
    return results


def by_stratum(results: dict[str, SequenceMetrics]) -> dict[str, SequenceMetrics]:
    grouped: dict[str, SequenceMetrics] = {}
    for key, metrics in results.items():
        if key == "__all__":
            continue
        current = grouped.get(metrics.stratum)
        grouped[metrics.stratum] = metrics if current is None else current.merge(metrics)
        grouped[metrics.stratum].sequence_id = metrics.stratum
    return grouped


def describe(results: dict[str, SequenceMetrics]) -> str:
    """A fixed-width table with counts beside every rate."""

    def rate(value: float | None) -> str:
        return "   n/a" if value is None else f"{value * 100:5.1f}%"

    def degrees(value: float | None) -> str:
        return "  n/a" if value is None else f"{value:5.1f}"

    lines = [
        f"{'sequence':<26}{'frm':>5}{'unk':>4}{'pres':>5}{'abs':>4}{'hit':>4}{'miss':>5}"
        f"{'flock':>6}{'facq':>5}{'sw':>3}{'1f':>3}{'recall':>8}{'absP':>8}"
        f"{'fl%':>8}{'med':>6}{'p95':>6}{'>10':>4}{'sign':>8}"
    ]
    for key, m in results.items():
        lines.append(
            f"{key:<26}{m.frames:>5}{m.unknown:>4}{m.present:>5}{m.absent:>4}{m.hits:>4}"
            f"{m.misses:>5}{m.false_locks:>6}{m.false_acquisitions:>5}{m.identity_switches:>3}"
            f"{m.single_frame_replacements:>3}{rate(m.recall):>8}{rate(m.absent_precision):>8}"
            f"{rate(m.false_lock_rate):>8}{degrees(m.median_error_deg):>6}"
            f"{degrees(m.p95_error_deg):>6}{m.bad_angle_frames:>4}{rate(m.sign_accuracy):>8}"
        )
    return "\n".join(lines)


def heading_from_axis(
    tip_px: tuple[float, float] | None, tail_px: tuple[float, float] | None
) -> float | None:
    """Screen heading of a tail-to-tip shaft, or ``None``."""
    if tip_px is None or tail_px is None:
        return None
    dx, dy = tip_px[0] - tail_px[0], tip_px[1] - tail_px[1]
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return None
    return math.degrees(math.atan2(dx, -dy)) % 360.0
