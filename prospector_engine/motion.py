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

from collections.abc import Callable
from dataclasses import dataclass, field
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
    "ContactConfig",
    "ContactEvidence",
    "ContactMonitor",
    "LocomotionBaseline",
    "MotionEstimator",
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

    @property
    def usable(self) -> bool:
        return (
            self.min_forward_speed_norm is not None and self.status is EvidenceStatus.VALIDATED
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
