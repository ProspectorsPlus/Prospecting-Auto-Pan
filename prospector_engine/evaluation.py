"""Offline detector evaluation: per-stratum metrics with confidence bounds.

This is the instrument, not the verdict. It computes exactly the quantities the
mission's acceptance list names - bad-angle rate, median/p90/p95 absolute
error, signed bias, polarity flips, accepted coverage, accepted precision,
distractor-only false acquisition, reacquisition latency and wrong-track dwell
- and it reports them **per stratum**, because an aggregate that hides a
failing condition is worse than no measurement at all (plan 7.5).

Two rules the arithmetic here enforces:

* **Coverage and precision are always reported together.** A detector that
  abstains on every frame has perfect precision and is useless, so neither
  number is meaningful alone.
* **Frames are not independent samples.** Sixty consecutive frames of one route
  are one observation of that route wearing sixty hats. Bounds are therefore
  computed over **episodes** by default, and the frame count is reported
  beside them so a reader can see the difference.

Nothing here can pass a gate. E-PROF and E-DIR-E2E require a labelled corpus
from real sessions with held-out splits; this module is what will be run
against one when it exists.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from prospector_engine.contracts import CapturedFrame

__all__ = [
    "DatasetSplit",
    "EvaluationReport",
    "LabelledFrame",
    "StratumMetrics",
    "evaluate",
    "rule_of_three_upper",
    "wilson_interval",
]


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Chosen over the normal approximation because the interesting cases here are
    exactly the ones it handles badly: proportions near 0 and near 1, with
    small n. Returns ``(lower, upper)``.
    """
    if trials <= 0:
        return (0.0, 1.0)
    phat = successes / trials
    denominator = 1.0 + z * z / trials
    centre = phat + z * z / (2 * trials)
    spread = z * math.sqrt(phat * (1 - phat) / trials + z * z / (4 * trials * trials))
    return (
        max(0.0, (centre - spread) / denominator),
        min(1.0, (centre + spread) / denominator),
    )


def rule_of_three_upper(trials: int) -> float:
    """One-sided 95% upper bound on a rate after observing **zero** events.

    Only valid for plausibly independent, homogeneous exposure; with clustered
    or session-correlated data it is optimistic, which is why the caller is
    made to pass an episode count rather than a frame count (plan 7.5).
    """
    return 1.0 if trials <= 0 else min(1.0, 3.0 / trials)


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LabelledFrame:
    """One evaluation frame and the label a reviewer gave it.

    ``heading_deg`` is ``None`` for an arrow-absent frame, which is not the
    same as an unlabelled one: absence is a positive label and is what the
    false-acquisition rate is measured on. ``unknown`` frames are excluded
    from every rate and reported separately rather than dropped silently.
    """

    frame: CapturedFrame
    heading_deg: float | None
    stratum: str
    session_id: str
    episode_id: str
    arrow_present: bool = True
    unknown: bool = False


@dataclass(frozen=True)
class DatasetSplit:
    """A named split. Splitting is by whole session, never by adjacent frame."""

    name: str
    frames: tuple[LabelledFrame, ...]

    @property
    def sessions(self) -> tuple[str, ...]:
        return tuple(sorted({frame.session_id for frame in self.frames}))

    @property
    def episodes(self) -> tuple[str, ...]:
        return tuple(sorted({frame.episode_id for frame in self.frames}))


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StratumMetrics:
    """Everything the mission's acceptance list asks for, for one stratum."""

    stratum: str
    frames: int
    sessions: int
    episodes: int
    eligible: int
    accepted: int
    unknown: int

    coverage: float
    coverage_lower: float
    precision: float
    precision_lower: float

    bad_angle_rate: float
    bad_angle_upper: float
    median_abs_deg: float
    p90_abs_deg: float
    p95_abs_deg: float
    signed_bias_deg: float

    polarity_flips: int
    polarity_flip_upper: float

    absent_frames: int
    false_acquisitions: int
    false_acquisition_upper: float

    reacquire_p95_frames: float
    wrong_track_max_frames: int

    def passes_provisional_targets(self) -> tuple[bool, tuple[str, ...]]:
        """Check against the mission's provisional production targets.

        Returns ``(passed, failures)``. A ``True`` here is **not** a gate pass:
        gates additionally require real sessions, frozen thresholds, and a
        held-out split evaluated once (plan 7.2).
        """
        failures: list[str] = []
        if self.bad_angle_upper >= 0.10:
            failures.append(f"bad-angle upper bound {self.bad_angle_upper:.3f} >= 0.10")
        if self.median_abs_deg > 3.0:
            failures.append(f"median {self.median_abs_deg:.2f} deg > 3.0")
        if self.p90_abs_deg > 8.0:
            failures.append(f"p90 {self.p90_abs_deg:.2f} deg > 8.0")
        if self.p95_abs_deg > 10.0:
            failures.append(f"p95 {self.p95_abs_deg:.2f} deg > 10.0")
        if abs(self.signed_bias_deg) > 2.0:
            failures.append(f"bias {self.signed_bias_deg:+.2f} deg exceeds 2.0")
        if self.polarity_flip_upper >= 0.005:
            failures.append(
                f"polarity-flip upper bound {self.polarity_flip_upper:.4f} >= 0.005"
            )
        if self.coverage_lower < 0.85:
            failures.append(f"coverage lower bound {self.coverage_lower:.3f} < 0.85")
        if self.precision_lower < 0.99:
            failures.append(f"precision lower bound {self.precision_lower:.3f} < 0.99")
        if self.false_acquisition_upper >= 0.01:
            failures.append(
                f"false-acquisition upper bound {self.false_acquisition_upper:.4f} >= 0.01"
            )
        if self.wrong_track_max_frames > 1:
            failures.append(f"wrong-track dwell {self.wrong_track_max_frames} frames > 1")
        return (not failures, tuple(failures))

    def describe(self) -> str:
        passed, failures = self.passes_provisional_targets()
        head = (
            f"{self.stratum:22s} n={self.frames:5d} ep={self.episodes:3d} "
            f"cov {self.coverage * 100:5.1f}% (lo {self.coverage_lower * 100:5.1f}) "
            f"prec {self.precision * 100:5.1f}% (lo {self.precision_lower * 100:5.1f}) "
            f"med {self.median_abs_deg:5.2f} p90 {self.p90_abs_deg:5.2f} "
            f"p95 {self.p95_abs_deg:5.2f} bias {self.signed_bias_deg:+5.2f} "
            f"bad>10 {self.bad_angle_rate * 100:4.1f}% (hi {self.bad_angle_upper * 100:4.1f}) "
            f"flips {self.polarity_flips}"
        )
        return head + ("  MEETS TARGETS" if passed else f"  MISSES: {'; '.join(failures)}")


@dataclass(frozen=True)
class EvaluationReport:
    """Per-stratum results plus the aggregate, which is never a substitute."""

    split: str
    strata: tuple[StratumMetrics, ...]
    detector_config: dict[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def worst_stratum(self) -> StratumMetrics | None:
        """The stratum that fails the most targets, then the least covered.

        Ranked by failure count rather than by coverage, because a stratum can
        have perfect coverage and still be the worst one in the report - a
        confident wrong answer is a worse outcome than an abstention.
        """
        failing = [s for s in self.strata if not s.passes_provisional_targets()[0]]
        if not failing:
            return None
        return max(
            failing,
            key=lambda s: (len(s.passes_provisional_targets()[1]), -s.coverage_lower),
        )

    def passes(self) -> bool:
        """Every stratum must pass. An aggregate pass never covers a failure."""
        return bool(self.strata) and all(s.passes_provisional_targets()[0] for s in self.strata)

    def to_json(self) -> str:
        return json.dumps(
            {
                "split": self.split,
                "passes_provisional_targets": self.passes(),
                "detector_config": self.detector_config,
                "notes": list(self.notes),
                "strata": [asdict(stratum) for stratum in self.strata],
                "caveat": (
                    "Provisional targets only. E-PROF and E-DIR-E2E additionally "
                    "require real multi-session data, frozen thresholds, and a "
                    "held-out split evaluated once."
                ),
            },
            indent=2,
        )

    def describe(self) -> str:
        lines = [f"split: {self.split}", ""]
        lines.extend(stratum.describe() for stratum in self.strata)
        lines.append("")
        lines.append(
            "ALL STRATA MEET THE PROVISIONAL TARGETS"
            if self.passes()
            else "AT LEAST ONE STRATUM MISSES ITS PROVISIONAL TARGETS"
        )
        lines.append(
            "This is a measurement, not a gate: E-PROF and E-DIR-E2E need real "
            "sessions, frozen thresholds, and a held-out split evaluated once."
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _wrap(degrees: float) -> float:
    wrapped = (degrees + 180.0) % 360.0 - 180.0
    return 180.0 if wrapped == -180.0 else wrapped


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


@dataclass
class _Accumulator:
    errors: list[float] = field(default_factory=list)
    signed: list[float] = field(default_factory=list)
    frames: int = 0
    eligible: int = 0
    accepted: int = 0
    unknown: int = 0
    absent: int = 0
    false_acquisitions: int = 0
    sessions: set[str] = field(default_factory=set)
    episodes: set[str] = field(default_factory=set)
    reacquire_gaps: list[int] = field(default_factory=list)
    wrong_track_run: int = 0
    wrong_track_max: int = 0
    gap: int = 0


#: A prediction is "correct" for polarity purposes below this. The mission
#: defines a flip as an accepted answer more than 90 degrees from the truth.
FLIP_DEG = 90.0
#: The mission's definition of a bad angle, given the detector accepted.
BAD_ANGLE_DEG = 10.0


def evaluate(
    split: DatasetSplit,
    predict: Callable[[CapturedFrame], tuple[float | None, bool]],
    *,
    detector_config: dict[str, Any] | None = None,
    notes: Iterable[str] = (),
) -> EvaluationReport:
    """Run ``predict`` over a split and produce per-stratum metrics.

    ``predict`` returns ``(heading_deg_or_None, accepted)``. Keeping acceptance
    separate from the value is deliberate: "accepted with no heading" is a
    detector bug, and folding the two together would hide it.
    """
    buckets: dict[str, _Accumulator] = defaultdict(_Accumulator)
    for labelled in split.frames:
        bucket = buckets[labelled.stratum]
        bucket.frames += 1
        bucket.sessions.add(labelled.session_id)
        bucket.episodes.add(labelled.episode_id)
        if labelled.unknown:
            bucket.unknown += 1
            continue

        heading, accepted = predict(labelled.frame)

        if not labelled.arrow_present:
            bucket.absent += 1
            if accepted:
                bucket.false_acquisitions += 1
            continue

        bucket.eligible += 1
        if not accepted or heading is None or labelled.heading_deg is None:
            bucket.gap += 1
            # A miss ends any wrong-track run: nothing is being followed.
            bucket.wrong_track_max = max(bucket.wrong_track_max, bucket.wrong_track_run)
            bucket.wrong_track_run = 0
            continue

        if bucket.gap:
            bucket.reacquire_gaps.append(bucket.gap)
            bucket.gap = 0
        bucket.accepted += 1
        error = _wrap(heading - labelled.heading_deg)
        bucket.signed.append(error)
        bucket.errors.append(abs(error))
        if abs(error) > FLIP_DEG:
            bucket.wrong_track_run += 1
            bucket.wrong_track_max = max(bucket.wrong_track_max, bucket.wrong_track_run)
        else:
            bucket.wrong_track_run = 0

    strata = tuple(_summarise(name, bucket) for name, bucket in sorted(buckets.items()))
    return EvaluationReport(
        split=split.name,
        strata=strata,
        detector_config=dict(detector_config or {}),
        notes=tuple(notes),
    )


def _summarise(name: str, bucket: _Accumulator) -> StratumMetrics:
    bucket.wrong_track_max = max(bucket.wrong_track_max, bucket.wrong_track_run)
    errors = bucket.errors
    bad = sum(1 for value in errors if value > BAD_ANGLE_DEG)
    flips = sum(1 for value in errors if value > FLIP_DEG)
    # Bounds are computed over episodes, not frames: consecutive frames of one
    # route are clustered evidence, and pooling them manufactures a bound the
    # data cannot support (plan 7.5).
    episodes = max(1, len(bucket.episodes))
    coverage_lo, _ = wilson_interval(bucket.accepted, max(1, bucket.eligible))
    precision_correct = bucket.accepted - flips
    precision_lo, _ = wilson_interval(precision_correct, max(1, bucket.accepted))
    _, bad_upper = wilson_interval(bad, max(1, bucket.accepted))
    flip_upper = (
        rule_of_three_upper(episodes)
        if flips == 0
        else wilson_interval(flips, max(1, bucket.accepted))[1]
    )
    false_upper = (
        rule_of_three_upper(episodes)
        if bucket.false_acquisitions == 0
        else wilson_interval(bucket.false_acquisitions, max(1, bucket.absent))[1]
    )
    return StratumMetrics(
        stratum=name,
        frames=bucket.frames,
        sessions=len(bucket.sessions),
        episodes=len(bucket.episodes),
        eligible=bucket.eligible,
        accepted=bucket.accepted,
        unknown=bucket.unknown,
        coverage=bucket.accepted / bucket.eligible if bucket.eligible else 0.0,
        coverage_lower=coverage_lo,
        precision=precision_correct / bucket.accepted if bucket.accepted else 0.0,
        precision_lower=precision_lo,
        bad_angle_rate=bad / bucket.accepted if bucket.accepted else 0.0,
        bad_angle_upper=bad_upper,
        median_abs_deg=_percentile(errors, 0.5),
        p90_abs_deg=_percentile(errors, 0.9),
        p95_abs_deg=_percentile(errors, 0.95),
        signed_bias_deg=sum(bucket.signed) / len(bucket.signed) if bucket.signed else 0.0,
        polarity_flips=flips,
        polarity_flip_upper=flip_upper,
        absent_frames=bucket.absent,
        false_acquisitions=bucket.false_acquisitions,
        false_acquisition_upper=false_upper,
        reacquire_p95_frames=_percentile([float(gap) for gap in bucket.reacquire_gaps], 0.95)
        if bucket.reacquire_gaps
        else 0.0,
        wrong_track_max_frames=bucket.wrong_track_max,
    )
