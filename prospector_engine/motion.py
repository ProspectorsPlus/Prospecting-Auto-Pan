"""Candidate motion estimators and the contact evidence they feed.

Three independent estimators are implemented side by side so E-MOTION can
compare them on the same labelled clips: Lucas-Kanade with a robust affine fit,
phase correlation, and a deliberately simple block-displacement baseline. No
winner is chosen in source (plan 7.4).

Two rules shape everything here:

* Displacement is normalized by the actual monotonic delta-t, never by an
  assumed frame interval.
* There is no time-only fallback for contact. "The forward key has been held
  for a while" is not evidence of a collision; low measured progress with high
  motion confidence and low yaw contamination is (plan 7.4 E-MOTION).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

from prospector_engine.contracts import (
    CapturedFrame,
    EvidenceStatus,
    MotionObservation,
    Provenance,
)

__all__ = [
    "MOTION_ESTIMATORS",
    "AppliedForward",
    "ContactConfig",
    "ContactEvidence",
    "ContactMonitor",
    "ForwardCommandLedger",
    "LocomotionBaseline",
    "MotionEstimator",
    "ProgressConfig",
    "ProgressGuard",
    "ProgressState",
    "ProgressVerdict",
    "TraversabilityObservation",
    "estimate_block_displacement",
    "estimate_lk_affine",
    "estimate_phase_correlation",
]


class MotionEstimator(Protocol):
    def __call__(
        self,
        previous: CapturedFrame,
        current: CapturedFrame,
        *,
        roi_px: tuple[int, int, int, int],
    ) -> MotionObservation: ...


def _abstain(reason: str) -> MotionObservation:
    return MotionObservation(
        forward_speed_norm=None,
        lateral_speed_norm=None,
        confidence=0.0,
        inlier_count=0,
        inlier_ratio=0.0,
        spatial_coverage=0.0,
        residual=float("inf"),
        yaw_contamination=1.0,
        valid=False,
        abstain_reason=reason,
    )


def _delta_t_s(previous: CapturedFrame, current: CapturedFrame) -> float | None:
    delta = current.captured_at_s - previous.captured_at_s
    return delta if delta > 1e-6 else None


def _grey(frame: CapturedFrame, roi_px: tuple[int, int, int, int]) -> NDArray[Any]:
    import cv2

    x, y, width, height = roi_px
    patch = np.asarray(frame.bgr)[y : y + height, x : x + width]
    grey: NDArray[Any] = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    return grey


def _coverage(points: NDArray[Any], shape: tuple[int, ...]) -> float:
    """Fraction of a 4x4 grid of the ROI that actually contains inliers.

    A hundred features clustered in one corner is not evidence about the whole
    scene, so coverage is reported separately from inlier count (plan 7.3).
    """
    if points.size == 0:
        return 0.0
    height, width = shape
    cells = np.zeros((4, 4), dtype=bool)
    for x, y in points.reshape(-1, 2):
        column = min(3, max(0, int(x / max(1.0, width / 4.0))))
        row = min(3, max(0, int(y / max(1.0, height / 4.0))))
        cells[row, column] = True
    return float(cells.sum() / 16.0)


def estimate_lk_affine(
    previous: CapturedFrame, current: CapturedFrame, *, roi_px: tuple[int, int, int, int]
) -> MotionObservation:
    """Lucas-Kanade sparse flow plus a RANSAC-style partial affine fit."""
    import cv2

    delta_t = _delta_t_s(previous, current)
    if delta_t is None:
        return _abstain("non-monotonic delta-t")
    old_grey: NDArray[np.uint8] = np.ascontiguousarray(_grey(previous, roi_px), dtype=np.uint8)
    new_grey: NDArray[np.uint8] = np.ascontiguousarray(_grey(current, roi_px), dtype=np.uint8)
    if old_grey.shape != new_grey.shape or old_grey.size == 0:
        return _abstain("roi mismatch")

    detected = cv2.goodFeaturesToTrack(
        old_grey, maxCorners=200, qualityLevel=0.01, minDistance=8, blockSize=7
    )
    if detected is None or len(detected) < 12:
        return _abstain("insufficient features")
    corners: NDArray[np.float32] = np.asarray(detected, dtype=np.float32)
    tracked, status, _error = cv2.calcOpticalFlowPyrLK(
        old_grey, new_grey, corners, np.zeros_like(corners)
    )
    # The stubs type these as non-optional; OpenCV can still return empty
    # arrays when tracking fails outright, which is what the size check below
    # actually guards.
    good_old = corners[np.asarray(status).ravel() == 1]
    good_new = np.asarray(tracked)[np.asarray(status).ravel() == 1]
    if len(good_old) < 12:
        return _abstain("insufficient tracked features")

    matrix, inliers = cv2.estimateAffinePartial2D(
        good_old, good_new, method=cv2.RANSAC, ransacReprojThreshold=2.0
    )
    if matrix is None or inliers is None:
        return _abstain("affine fit failed")
    inlier_mask = inliers.ravel().astype(bool)
    inlier_count = int(inlier_mask.sum())
    inlier_ratio = inlier_count / max(1, len(good_old))
    if inlier_count < 10:
        return _abstain("insufficient inliers")

    dx = float(matrix[0, 2])
    dy = float(matrix[1, 2])
    # Rotation implied by the similarity part: a large value means the frame
    # pair straddles a camera turn and translation is not trustworthy.
    rotation = float(np.arctan2(matrix[1, 0], matrix[0, 0]))
    yaw_contamination = min(1.0, abs(rotation) / np.deg2rad(3.0))
    residual = float(
        np.mean(np.linalg.norm(good_new[inlier_mask] - good_old[inlier_mask], axis=-1))
    )
    coverage = _coverage(good_old[inlier_mask], old_grey.shape)

    height = max(1, old_grey.shape[0])
    return MotionObservation(
        forward_speed_norm=(-dy / height) / delta_t,
        lateral_speed_norm=(dx / height) / delta_t,
        confidence=round(min(1.0, inlier_ratio) * coverage * (1.0 - yaw_contamination), 4),
        inlier_count=inlier_count,
        inlier_ratio=round(inlier_ratio, 4),
        spatial_coverage=round(coverage, 4),
        residual=round(residual, 4),
        yaw_contamination=round(yaw_contamination, 4),
        valid=True,
    )


def estimate_phase_correlation(
    previous: CapturedFrame, current: CapturedFrame, *, roi_px: tuple[int, int, int, int]
) -> MotionObservation:
    """Global translation by phase correlation - no feature detection at all."""
    import cv2

    delta_t = _delta_t_s(previous, current)
    if delta_t is None:
        return _abstain("non-monotonic delta-t")
    old_grey = _grey(previous, roi_px).astype(np.float32)
    new_grey = _grey(current, roi_px).astype(np.float32)
    if old_grey.shape != new_grey.shape or old_grey.size == 0:
        return _abstain("roi mismatch")
    window = cv2.createHanningWindow((old_grey.shape[1], old_grey.shape[0]), cv2.CV_32F)
    (dx, dy), response = cv2.phaseCorrelate(old_grey, new_grey, window)
    height = max(1, old_grey.shape[0])
    confidence = float(max(0.0, min(1.0, response)))
    return MotionObservation(
        forward_speed_norm=(-float(dy) / height) / delta_t,
        lateral_speed_norm=(float(dx) / height) / delta_t,
        confidence=round(confidence, 4),
        inlier_count=old_grey.size,
        inlier_ratio=1.0,
        spatial_coverage=1.0,
        residual=round(1.0 - confidence, 4),
        yaw_contamination=0.0,  # this estimator cannot separate yaw; see plan 7.4
        valid=confidence > 0.0,
        abstain_reason=None if confidence > 0.0 else "no correlation peak",
    )


def estimate_block_displacement(
    previous: CapturedFrame, current: CapturedFrame, *, roi_px: tuple[int, int, int, int]
) -> MotionObservation:
    """The simple baseline: mean absolute difference over a small search.

    Kept deliberately naive so E-MOTION can show whether the sophisticated
    estimators actually earn their cost.
    """
    delta_t = _delta_t_s(previous, current)
    if delta_t is None:
        return _abstain("non-monotonic delta-t")
    old_grey = _grey(previous, roi_px).astype(np.int16)
    new_grey = _grey(current, roi_px).astype(np.int16)
    if old_grey.shape != new_grey.shape or old_grey.size == 0:
        return _abstain("roi mismatch")
    best = (float("inf"), 0, 0)
    for dy in range(-6, 7, 2):
        for dx in range(-6, 7, 2):
            shifted = np.roll(np.roll(old_grey, dy, axis=0), dx, axis=1)
            score = float(np.abs(shifted[8:-8, 8:-8] - new_grey[8:-8, 8:-8]).mean())
            if score < best[0]:
                best = (score, dx, dy)
    residual, dx_best, dy_best = best
    height = max(1, old_grey.shape[0])
    confidence = float(max(0.0, 1.0 - residual / 64.0))
    return MotionObservation(
        forward_speed_norm=(-dy_best / height) / delta_t,
        lateral_speed_norm=(dx_best / height) / delta_t,
        confidence=round(confidence, 4),
        inlier_count=old_grey.size,
        inlier_ratio=1.0,
        spatial_coverage=1.0,
        residual=round(residual, 4),
        yaw_contamination=0.0,
        valid=confidence > 0.0,
        abstain_reason=None if confidence > 0.0 else "no match",
    )


MOTION_ESTIMATORS: dict[str, Callable[..., MotionObservation]] = {
    "lk_affine": estimate_lk_affine,
    "phase_correlation": estimate_phase_correlation,
    "block_displacement": estimate_block_displacement,
}


# ---------------------------------------------------------------------------
# Progress baselines and contact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocomotionBaseline:
    """Expected unobstructed forward speed for one locomotion condition.

    Seeded from independently labelled, physically armed open-ground trials -
    never from the production estimator's own "unobstructed" decision.
    ``status`` stays ``PENDING`` until those trials exist, and a missing or
    out-of-condition baseline disables Live rather than guessing (plan 7.4).
    """

    condition_id: str
    min_forward_speed_norm: float | None
    status: EvidenceStatus
    provenance: Provenance
    #: The unobstructed speed the threshold was derived from. Carried so a
    #: dashboard can say "38% of walking speed" rather than quoting a
    #: normalized number nobody can interpret. ``None`` for a baseline that
    #: never measured one.
    reference_speed_norm: float | None = None

    #: Samples required before a runtime baseline may be minted at all.
    MIN_RUNTIME_SAMPLES = 12
    #: Fraction of the observed median below which the character has stopped.
    RUNTIME_STALL_FRACTION = 0.35

    @property
    def usable(self) -> bool:
        return (
            self.min_forward_speed_norm is not None and self.status is EvidenceStatus.VALIDATED
        )

    @property
    def measured_at_runtime(self) -> bool:
        """Whether this came from this session rather than a frozen gate."""
        return self.condition_id.startswith("runtime:")

    @classmethod
    def measured_in_run(
        cls, *, speeds: Sequence[float], run_id: str, condition: str
    ) -> LocomotionBaseline:
        """A baseline measured from this session's own unobstructed walking.

        This is not the offline E-MOTION gate and never claims to be. E-MOTION
        is independently labelled open-ground trials that would let a *frozen*
        threshold ship with the software; it is still PENDING. What this is: a
        measurement taken here, on this machine, under the physical arm, from
        frames where forward was genuinely applied, motion confidence was high
        and yaw contamination was low - and it is discarded when the session
        ends (D-040).

        The threshold is a fraction of the observed median rather than the
        median itself, because the question downstream is "has the character
        stopped", not "is it at full speed": a slope or deep water legitimately
        halves the speed and must not read as a wall.
        """
        ordered = sorted(float(speed) for speed in speeds)
        if len(ordered) < cls.MIN_RUNTIME_SAMPLES:
            return UNCALIBRATED_BASELINE
        median = ordered[len(ordered) // 2]
        if median <= 0.0:
            return UNCALIBRATED_BASELINE
        return cls(
            condition_id=f"runtime:{condition}",
            min_forward_speed_norm=median * cls.RUNTIME_STALL_FRACTION,
            reference_speed_norm=median,
            status=EvidenceStatus.VALIDATED,
            provenance=Provenance(
                status=EvidenceStatus.VALIDATED,
                source=f"runtime locomotion sampling in run {run_id}",
                note=(
                    f"median {median:.4f} over {len(ordered)} armed frames with forward "
                    f"applied; threshold is {cls.RUNTIME_STALL_FRACTION:.0%} of it. "
                    "Session-scoped; this is NOT the offline E-MOTION gate."
                ),
            ),
        )


UNCALIBRATED_BASELINE = LocomotionBaseline(
    condition_id="uncalibrated",
    min_forward_speed_norm=None,
    status=EvidenceStatus.PENDING,
    provenance=Provenance(
        status=EvidenceStatus.PENDING,
        source="E-MOTION open-ground trials",
        note="no physically armed trials have been run; Live motion evidence is unavailable",
    ),
)


@dataclass(frozen=True)
class ContactConfig:
    """When low progress becomes contact evidence. All values provisional."""

    min_confidence: float = 0.5
    min_coverage: float = 0.5
    max_yaw_contamination: float = 0.35
    sustained_ms: int = 400
    post_yaw_holdoff_ms: int = 250
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="TREASURE_NAVIGATION_PLAN.md section 7.4 E-MOTION",
            note="E-MOTION has not been run; these gate nothing until it has",
        )
    )


@dataclass(frozen=True)
class ContactEvidence:
    contact: bool
    reason: str
    sustained_ms: float
    observation: MotionObservation | None


class ContactMonitor:
    """Accumulates low-progress evidence while forward is actually commanded.

    Any invalid, low-confidence, or yaw-contaminated observation resets the
    accumulator instead of letting elapsed time carry it forward.
    """

    def __init__(
        self,
        baseline: LocomotionBaseline = UNCALIBRATED_BASELINE,
        config: ContactConfig | None = None,
    ) -> None:
        self._baseline = baseline
        self._config = config or ContactConfig()
        self._since_s: float | None = None
        self._last_yaw_s: float | None = None

    @property
    def baseline(self) -> LocomotionBaseline:
        return self._baseline

    def note_yaw(self, at_s: float) -> None:
        self._last_yaw_s = at_s

    def recently_yawed(self, now_s: float, holdoff_ms: float) -> bool:
        """Whether a camera movement is still contaminating motion evidence."""
        last = self._last_yaw_s
        return last is not None and (now_s - last) * 1000.0 < holdoff_ms

    def reset(self) -> None:
        self._since_s = None

    def update(
        self, observation: MotionObservation, *, forward_commanded: bool, now_s: float
    ) -> ContactEvidence:
        if not self._baseline.usable:
            self.reset()
            return ContactEvidence(False, "baseline-uncalibrated", 0.0, observation)
        if not forward_commanded:
            self.reset()
            return ContactEvidence(False, "forward-not-commanded", 0.0, observation)
        if not observation.valid or observation.forward_speed_norm is None:
            self.reset()
            return ContactEvidence(
                False, f"motion-invalid:{observation.abstain_reason}", 0.0, observation
            )
        if observation.confidence < self._config.min_confidence:
            self.reset()
            return ContactEvidence(False, "low-confidence", 0.0, observation)
        if observation.spatial_coverage < self._config.min_coverage:
            self.reset()
            return ContactEvidence(False, "poor-coverage", 0.0, observation)
        if observation.yaw_contamination > self._config.max_yaw_contamination:
            self.reset()
            return ContactEvidence(False, "yaw-contaminated", 0.0, observation)
        if (
            self._last_yaw_s is not None
            and (now_s - self._last_yaw_s) * 1000.0 < self._config.post_yaw_holdoff_ms
        ):
            self.reset()
            return ContactEvidence(False, "post-yaw-holdoff", 0.0, observation)

        threshold = self._baseline.min_forward_speed_norm
        assert threshold is not None  # guarded by baseline.usable
        if observation.forward_speed_norm >= threshold:
            self.reset()
            return ContactEvidence(False, "progressing", 0.0, observation)

        if self._since_s is None:
            self._since_s = now_s
        sustained_ms = (now_s - self._since_s) * 1000.0
        if sustained_ms >= self._config.sustained_ms:
            return ContactEvidence(True, "low-progress-sustained", sustained_ms, observation)
        return ContactEvidence(False, "low-progress-accumulating", sustained_ms, observation)


# ---------------------------------------------------------------------------
# Applied-forward ledger and the progress guard
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppliedForward:
    """One interval during which ``W`` was genuinely held.

    *Genuinely* is the operative word. "The navigator asked for forward" and
    "the input authority accepted a forward lease" are different facts, and
    only the second one means the character was being told to walk. Judging
    progress against the first is how a run of rejected commands would look
    like a wall.
    """

    started_at_s: float
    ended_at_s: float | None = None

    def duration_s(self, now_s: float) -> float:
        return max(0.0, (self.ended_at_s or now_s) - self.started_at_s)


class ForwardCommandLedger:
    """What forward motion was actually applied, and when.

    Bounded: it keeps only the recent window the guard can reason about, so a
    long session cannot grow it.
    """

    def __init__(self, window_s: float = 8.0) -> None:
        self._window_s = window_s
        self._intervals: deque[AppliedForward] = deque(maxlen=64)

    def note_applied(self, now_s: float, *, forward: bool) -> None:
        """Record what the authority *accepted*, not what was requested."""
        open_interval = (
            self._intervals[-1]
            if self._intervals and self._intervals[-1].ended_at_s is None
            else None
        )
        if forward and open_interval is None:
            self._intervals.append(AppliedForward(started_at_s=now_s))
        elif not forward and open_interval is not None:
            self._intervals[-1] = replace(open_interval, ended_at_s=now_s)
        self._trim(now_s)

    def _trim(self, now_s: float) -> None:
        cutoff = now_s - self._window_s
        while self._intervals and (self._intervals[0].ended_at_s or now_s) < cutoff:
            self._intervals.popleft()

    def held_continuously_for(self, now_s: float) -> float:
        """How long ``W`` has been held without interruption, in seconds."""
        if not self._intervals or self._intervals[-1].ended_at_s is not None:
            return 0.0
        return self._intervals[-1].duration_s(now_s)

    def holding(self) -> bool:
        return bool(self._intervals) and self._intervals[-1].ended_at_s is None

    def clear(self) -> None:
        self._intervals.clear()


class RuntimeBaselineEstimator:
    """Learns what "walking normally" looks like, from this session's frames.

    It only accepts a sample when every reason to distrust one is absent:
    forward genuinely applied for long enough, a valid high-confidence motion
    estimate, good spatial coverage, negligible yaw contamination, and no
    recent turn. That is a narrow gate on purpose - a baseline built from
    contaminated frames would set the stall threshold wherever the noise was.
    """

    def __init__(self, run_id: str, *, condition: str = "open-ground") -> None:
        self._run_id = run_id
        self._condition = condition
        self._speeds: deque[float] = deque(maxlen=120)
        self._baseline = UNCALIBRATED_BASELINE

    @property
    def baseline(self) -> LocomotionBaseline:
        return self._baseline

    @property
    def samples(self) -> int:
        return len(self._speeds)

    def reset(self) -> None:
        self._speeds.clear()
        self._baseline = UNCALIBRATED_BASELINE

    def observe(
        self,
        observation: MotionObservation | None,
        *,
        forward_applied: bool,
        held_ms: float,
        config: ContactConfig,
    ) -> LocomotionBaseline:
        if (
            observation is None
            or not forward_applied
            or held_ms < 250.0
            or not observation.valid
            or observation.forward_speed_norm is None
            or observation.confidence < config.min_confidence
            or observation.spatial_coverage < config.min_coverage
            or observation.yaw_contamination > config.max_yaw_contamination
        ):
            return self._baseline
        speed = observation.forward_speed_norm
        if speed <= 0.0:
            return self._baseline
        self._speeds.append(speed)
        self._baseline = LocomotionBaseline.measured_in_run(
            speeds=self._speeds, run_id=self._run_id, condition=self._condition
        )
        return self._baseline


class ProgressState(Enum):
    """What the guard currently believes about forward progress."""

    UNKNOWN = "unknown"
    """Not enough evidence. The default, and never a reason to act."""

    PROGRESSING = "progressing"
    NO_PROGRESS_SUSPECTED = "no_progress_suspected"
    """Low progress seen, not yet confirmed. Forward releases here."""

    NO_PROGRESS_CONFIRMED = "no_progress_confirmed"
    """Confirmed from fresh evidence collected *after* forward was released."""


@dataclass(frozen=True)
class ProgressVerdict:
    """One guard decision, with the evidence and what to do about it.

    ``recover`` is deliberately not called ``release_forward`` any more, and
    the rename records a behaviour change. The guard used to drop ``W`` on the
    *suspicion* and then confirm from four stationary frames - so every
    ambiguous patch of low-texture ground cost a stop, and the confirmation was
    measured on a character that had already been told to stand still. It now
    confirms from fresh evidence collected while forward is *still held*, and
    what it recommends at the end of that is a recovery maneuver, not a stop.
    The first thing that maneuver does is keep walking and jump.
    """

    state: ProgressState
    #: Begin obstacle recovery. Never merely "let go of W".
    recover: bool
    confidence: float
    reason: str
    sustained_ms: float = 0.0
    observation: MotionObservation | None = None
    #: Measured speed as a fraction of the baseline, when both are known.
    ratio: float | None = None
    #: Whether the judgement came from the relative fallback rather than from a
    #: matured baseline. Carried so a trace can tell the two apart.
    provisional: bool = False

    @property
    def blocked(self) -> bool:
        return self.state is ProgressState.NO_PROGRESS_CONFIRMED


@dataclass(frozen=True)
class TraversabilityObservation:
    """The per-frame contract a future 2.5D traversability grid will consume.

    Deliberately produced and recorded now, and deliberately **not** consumed
    by anything: the grid, the obstacle map and the detour planner are a later
    phase. What exists here is the input side of that boundary - one honest
    record per frame of what was commanded, what motion was observed, and how
    much either can be trusted - so the later work has real data to build on
    rather than a retrofit.

    Nothing in this pass reads it to make a decision. There is no wiggle, no
    detour, and no A/D/S/jump anywhere in the control path.
    """

    at_s: float
    commanded_heading_deg: float | None
    commanded_forward: bool
    motion: MotionObservation | None
    progress_state: ProgressState
    confidence: float
    #: The client-relative point the player was assumed to occupy, so a later
    #: grid can anchor its cells without re-deriving the anchor.
    anchor_px: tuple[float, float] | None = None
    note: str = ""


@dataclass(frozen=True)
class ProgressConfig:
    """When low progress becomes actionable. All values provisional.

    Two bounds rather than one: a *suspicion* window short enough that the
    character does not walk into a wall for a second, and a *confirmation*
    window that only runs after forward has already been released, so the
    confirmation is never contaminated by the motion it is judging.
    """

    #: Sustained low progress before the guard suspects contact. Short on
    #: purpose - but a suspicion no longer stops the character, so it is cheap.
    suspect_after_ms: int = 350
    #: Fresh **in-motion** samples required to turn a suspicion into a
    #: recommendation. Collected while forward is still held, because the
    #: question is whether the character is moving and the only way to observe
    #: that is to still be telling it to.
    confirm_frames: int = 3
    #: Forward must have been genuinely held at least this long before low
    #: progress means anything at all.
    min_applied_forward_ms: int = 250
    #: How long after a yaw pulse motion evidence stays untrustworthy.
    post_yaw_holdoff_ms: int = 250
    #: Consecutive healthy samples that clear a standing suspicion.
    clear_frames: int = 2

    # -- the relative fallback ---------------------------------------------
    #: Samples of held-forward speed the fallback needs before it will judge
    #: anything. Below this it abstains, exactly as the baseline does.
    fallback_min_samples: int = 10
    #: Wall-clock span those samples must cover, so a burst inside one tenth of
    #: a second cannot stand in for a walk.
    fallback_min_span_s: float = 1.2
    #: Fraction of the character's own recent median below which the fallback
    #: calls it a collapse. Deliberately lower than the matured baseline's
    #: stall fraction: an unproven reference earns a stricter test.
    fallback_stall_fraction: float = 0.25
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="TREASURE_NAVIGATION_PLAN.md section 7.4 E-MOTION; mission section 13",
            note="E-MOTION has not been run; the guard abstains without a baseline",
        )
    )


class RelativeProgressFallback:
    """A conservative stall test for a run whose baseline has not matured.

    The alternative was to disable obstacle recovery entirely until the runtime
    estimator had collected its twelve clean samples - and since those samples
    only arrive while the character is walking unobstructed, a route that met a
    bush in its first few seconds had no recovery at all and simply stopped.

    This is not a substitute for the baseline and never claims to be. It asks a
    strictly weaker, strictly local question: *has this character's own speed
    collapsed relative to what it was doing a moment ago?* It needs a real span
    of samples before it will answer, it uses a harsher fraction than the
    matured baseline because its reference is unproven, and every verdict it
    produces is marked ``provisional`` so a trace can tell the two apart.
    """

    def __init__(self, config: ProgressConfig) -> None:
        self._config = config
        self._samples: deque[tuple[float, float]] = deque(maxlen=180)

    @property
    def samples(self) -> int:
        return len(self._samples)

    def reset(self) -> None:
        self._samples.clear()

    def observe(self, speed_norm: float, now_s: float) -> None:
        """Record one trustworthy held-forward speed sample."""
        self._samples.append((now_s, float(speed_norm)))

    def reference(self) -> float | None:
        """The character's own recent median speed, or ``None`` when unproven."""
        config = self._config
        if len(self._samples) < config.fallback_min_samples:
            return None
        span = self._samples[-1][0] - self._samples[0][0]
        if span < config.fallback_min_span_s:
            return None
        ordered = sorted(speed for _, speed in self._samples)
        median = ordered[len(ordered) // 2]
        return median if median > 0.0 else None

    def threshold(self) -> float | None:
        reference = self.reference()
        if reference is None:
            return None
        return reference * self._config.fallback_stall_fraction


class ProgressGuard:
    """Conservative "is the character actually moving" evidence.

    Four rules, and they are the whole design:

    * **Elapsed time can never declare an obstacle.** Holding ``W`` for two
      seconds is not evidence of a wall; measured low displacement with high
      motion confidence and low yaw contamination is.
    * **Ambiguity abstains.** Low-texture scenes, poor spatial coverage and
      yaw-contaminated frames produce ``UNKNOWN``, which is not a reason to do
      anything.
    * **Confirmation happens in motion.** A suspicion no longer drops ``W`` to
      go and look. It used to, and the result was that every ambiguous stretch
      of ground cost a stop *and* the confirming frames were collected from a
      character that had already been told to stand still - which is not
      evidence about whether it can move. The guard now gathers
      ``confirm_frames`` further low-progress samples with forward still held,
      and only then recommends anything.
    * **An unproven baseline is not the same as no evidence.** Until the
      runtime baseline matures the guard falls back to a strictly weaker,
      strictly relative test against the character's own recent speed. That
      keeps recovery available on the first bush of a run instead of switching
      it off for the first twelve clean samples.

    It recommends *recovery*, which is a maneuver, and never merely a stop.
    """

    def __init__(
        self,
        baseline: LocomotionBaseline = UNCALIBRATED_BASELINE,
        config: ProgressConfig | None = None,
        contact: ContactMonitor | None = None,
    ) -> None:
        self._baseline = baseline
        self._config = config or ProgressConfig()
        self._contact = contact or ContactMonitor(
            baseline, ContactConfig(sustained_ms=self._config.suspect_after_ms)
        )
        self._ledger = ForwardCommandLedger()
        self._fallback = RelativeProgressFallback(self._config)
        self._state = ProgressState.UNKNOWN
        self._confirm_frames = 0
        self._clear_frames = 0
        self._suspect_since_s: float | None = None
        self._last_ratio: float | None = None
        self._history: deque[TraversabilityObservation] = deque(maxlen=240)

    @property
    def state(self) -> ProgressState:
        return self._state

    @property
    def ledger(self) -> ForwardCommandLedger:
        return self._ledger

    @property
    def baseline(self) -> LocomotionBaseline:
        return self._baseline

    @property
    def fallback(self) -> RelativeProgressFallback:
        return self._fallback

    @property
    def ratio(self) -> float | None:
        """The last measured speed as a fraction of whichever reference held."""
        return self._last_ratio

    def stall_ms(self, now_s: float) -> float:
        """How long the current suspicion has been accumulating."""
        if self._suspect_since_s is None:
            return 0.0
        return max(0.0, (now_s - self._suspect_since_s) * 1000.0)

    def adopt_baseline(self, baseline: LocomotionBaseline) -> None:
        """Install a baseline measured after the guard was constructed.

        The guard starts with nothing usable and abstains; the runtime
        estimator supplies one a second or two into the first walk. Adopting it
        here rather than rebuilding the guard keeps the contact accumulator and
        the applied-forward ledger intact across the transition.
        """
        self._baseline = baseline

    def history(self) -> tuple[TraversabilityObservation, ...]:
        """The bounded record the traversability memory consumes."""
        return tuple(self._history)

    def reset(self) -> None:
        self._contact.reset()
        self._ledger.clear()
        self._state = ProgressState.UNKNOWN
        self._confirm_frames = 0
        self._clear_frames = 0
        self._suspect_since_s = None
        self._last_ratio = None

    def note_applied(self, now_s: float, *, forward: bool) -> None:
        self._ledger.note_applied(now_s, forward=forward)

    def note_yaw(self, at_s: float) -> None:
        self._contact.note_yaw(at_s)

    def update(
        self,
        observation: MotionObservation | None,
        *,
        now_s: float,
        commanded_heading_deg: float | None = None,
        anchor_px: tuple[float, float] | None = None,
    ) -> ProgressVerdict:
        """One guard tick. Every uncertain path returns ``UNKNOWN``."""
        holding = self._ledger.holding()
        held_ms = self._ledger.held_continuously_for(now_s) * 1000.0

        verdict = self._decide(observation, now_s, holding, held_ms)
        self._last_ratio = verdict.ratio
        self._history.append(
            TraversabilityObservation(
                at_s=now_s,
                commanded_heading_deg=commanded_heading_deg,
                commanded_forward=holding,
                motion=observation,
                progress_state=verdict.state,
                confidence=verdict.confidence,
                anchor_px=anchor_px,
                note=verdict.reason,
            )
        )
        return verdict

    # -- internals ---------------------------------------------------------
    def _trustworthy(self, observation: MotionObservation, now_s: float) -> str | None:
        """Why this sample cannot be judged, or ``None`` when it can be."""
        config = ContactConfig()
        if not observation.valid or observation.forward_speed_norm is None:
            return f"motion-invalid:{observation.abstain_reason}"
        if observation.confidence < config.min_confidence:
            return "low-confidence"
        if observation.spatial_coverage < config.min_coverage:
            return "poor-coverage"
        if observation.yaw_contamination > config.max_yaw_contamination:
            return "yaw-contaminated"
        if self._contact.recently_yawed(now_s, self._config.post_yaw_holdoff_ms):
            return "post-yaw-holdoff"
        return None

    def _threshold(self) -> tuple[float | None, bool]:
        """The stall threshold in force, and whether it is the fallback's."""
        if self._baseline.usable and self._baseline.min_forward_speed_norm is not None:
            return (self._baseline.min_forward_speed_norm, False)
        return (self._fallback.threshold(), True)

    def _decide(
        self,
        observation: MotionObservation | None,
        now_s: float,
        holding: bool,
        held_ms: float,
    ) -> ProgressVerdict:
        config = self._config
        if observation is None:
            self._state = ProgressState.UNKNOWN
            return ProgressVerdict(self._state, False, 0.0, "no motion estimate on this frame")
        if not holding:
            # Nothing is being commanded, so nothing about progress can be
            # concluded. The suspicion is dropped rather than carried into a
            # period the character was not being asked to walk through.
            self._contact.reset()
            self._suspect_since_s = None
            self._confirm_frames = 0
            self._state = ProgressState.UNKNOWN
            return ProgressVerdict(
                self._state, False, 0.0, "forward is not being held", observation=observation
            )

        untrustworthy = self._trustworthy(observation, now_s)
        if untrustworthy is not None:
            # An absence of evidence, not evidence of progress. Neither the
            # suspicion nor its confirmation may advance on it.
            self._state = ProgressState.UNKNOWN
            return ProgressVerdict(
                self._state,
                False,
                observation.confidence,
                f"motion evidence is not conclusive: {untrustworthy}",
                sustained_ms=self.stall_ms(now_s),
                observation=observation,
            )

        speed = float(observation.forward_speed_norm or 0.0)
        threshold, provisional = self._threshold()
        if held_ms >= config.min_applied_forward_ms:
            # Every trustworthy held-forward sample feeds the fallback, whether
            # or not a matured baseline is in force: a baseline can be adopted
            # mid-run, and the fallback has to be ready before that happens.
            self._fallback.observe(speed, now_s)

        if threshold is None:
            self._state = ProgressState.UNKNOWN
            return ProgressVerdict(
                self._state,
                False,
                observation.confidence,
                (
                    "no locomotion reference yet: "
                    f"{self._fallback.samples} of {config.fallback_min_samples} samples"
                ),
                observation=observation,
            )
        reference = (
            self._fallback.reference() if provisional else self._baseline.reference_speed_norm
        )
        ratio = speed / reference if reference else None

        if held_ms < config.min_applied_forward_ms:
            return ProgressVerdict(
                ProgressState.UNKNOWN,
                False,
                observation.confidence,
                f"forward has only been held {held_ms:.0f} ms",
                observation=observation,
                ratio=ratio,
                provisional=provisional,
            )

        if speed >= threshold:
            self._clear_frames += 1
            if (
                self._state is not ProgressState.NO_PROGRESS_SUSPECTED
                or self._clear_frames >= config.clear_frames
            ):
                self._suspect_since_s = None
                self._confirm_frames = 0
                self._state = ProgressState.PROGRESSING
            return ProgressVerdict(
                self._state,
                False,
                observation.confidence,
                "progressing",
                observation=observation,
                ratio=ratio,
                provisional=provisional,
            )

        self._clear_frames = 0
        if self._suspect_since_s is None:
            self._suspect_since_s = now_s
        sustained_ms = self.stall_ms(now_s)
        if sustained_ms < config.suspect_after_ms:
            self._state = ProgressState.UNKNOWN
            return ProgressVerdict(
                self._state,
                False,
                observation.confidence,
                f"low progress accumulating ({sustained_ms:.0f} ms)",
                sustained_ms=sustained_ms,
                observation=observation,
                ratio=ratio,
                provisional=provisional,
            )

        # Confirmation, in motion. Forward is still held, so these frames are
        # evidence about a character that is being told to walk.
        self._confirm_frames += 1
        if self._confirm_frames < config.confirm_frames:
            self._state = ProgressState.NO_PROGRESS_SUSPECTED
            return ProgressVerdict(
                self._state,
                False,
                observation.confidence,
                (
                    f"contact suspected; confirming in motion "
                    f"({self._confirm_frames}/{config.confirm_frames})"
                ),
                sustained_ms=sustained_ms,
                observation=observation,
                ratio=ratio,
                provisional=provisional,
            )
        self._state = ProgressState.NO_PROGRESS_CONFIRMED
        source = "the character's own recent speed" if provisional else "the measured baseline"
        return ProgressVerdict(
            self._state,
            True,
            observation.confidence,
            (
                f"no forward progress confirmed in motion against {source} "
                f"({speed:.3f} vs {threshold:.3f})"
            ),
            sustained_ms=sustained_ms,
            observation=observation,
            ratio=ratio,
            provisional=provisional,
        )
