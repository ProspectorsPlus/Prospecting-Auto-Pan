"""Scored, explainable arrow detection with one temporal transaction per frame.

The structure is three stages with a hard boundary between the second and
the third::

    propose(frame, roi)   stateless: colour proposes, geometry scores
    fuse(proposals)       one candidate per object, whatever pass found it
    commit(frame, fused)  the ONLY place temporal state advances

The boundary exists because of a measured failure: the previous pipeline ran a
region-of-interest pass and then a full-frame pass on the same screenshot,
each of which aged the track, advanced the global-scan cadence and consumed
hysteresis - twice per frame. Now a frame may contribute any number of
proposal sets, and the track is updated exactly once.

Five other findings from the real-frame corpus (``tests/corpus/real``) shape
the scoring and the tracker:

* **The two-notch signature is evidence, not a precondition.** On real frames
  the outline is nicked by thin foreign structures (UI lines, drawn markers)
  and the notch pair is often unreadable while the arrow is plainly visible.
  Topology carries the largest weight and a missing signature costs a lot; it
  no longer vetoes on its own. A sharp tip vertex, interior colour density and
  a locally-measured boundary are the independent terms that replace the veto.
* **A boundary is judged against its own surround**, not against the frame's
  median gradient. On flat sand the frame median is dominated by HUD texture
  and a real edge read as zero.
* **Coordinates have one basis.** A region pass translates its contour into
  full-frame coordinates *before* any feature is measured, clipping is judged
  against the full frame, and exclusion regions are translated into the region
  before they are applied. An arrow that merely touches the region edge is not
  clipped.
* **The tracked object is protected before presentation truncation.** The best
  candidate inside the track gate is kept whatever its rank, so a larger
  distractor cannot delete it by ranking above it or overlapping it.
* **Identity is earned.** ``ACQUIRE`` needs several consistent frames before
  anything is reported; a challenger must beat the held track by a margin for
  several consecutive frames; a periodic global search *challenges* the track
  and never replaces it in one frame; brief loss is ``REACQUIRE`` with the
  old identity resumable near where it was; ambiguity abstains.

Nothing here is enabled for Live by itself. E-PROF and E-DIR-E2E gate that,
and both are PENDING.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

import numpy as np
from numpy.typing import NDArray

from prospector_engine.contracts import (
    ArrowCandidateRecord,
    ArrowObservation,
    CapturedFrame,
    CueReading,
    DirectionObservation,
    EvidenceStatus,
    Provenance,
    monotonic_s,
)

__all__ = [
    "ArrowDetector",
    "ArrowHypothesis",
    "DetectionOutcome",
    "DetectorConfig",
    "DirectionEstimator",
    "DirectionResult",
    "ProposalSet",
    "ProposalStats",
    "ShapeFeatures",
    "TrackState",
    "circular_consensus",
    "heading_deg",
    "wrap_deg",
]


# ---------------------------------------------------------------------------
# Angles
# ---------------------------------------------------------------------------


def wrap_deg(degrees: float) -> float:
    """Wrap to (-180, 180]. Correct across the +-180 seam."""
    wrapped = (degrees + 180.0) % 360.0 - 180.0
    return 180.0 if wrapped == -180.0 else wrapped


def heading_deg(vector_xy: tuple[float, float]) -> float | None:
    """Screen-space heading: 0 is up, +90 is right. ``None`` if degenerate."""
    dx, dy = vector_xy
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return None
    return wrap_deg(math.degrees(math.atan2(dx, -dy)))


#: Below this resultant length the cues have cancelled each other out and their
#: circular mean is an artefact of ``atan2(0, 0)``, not an estimate.
MIN_RESULTANT = 0.35


def circular_consensus(
    readings: list[tuple[str, float, float]], *, outlier_deg: float
) -> tuple[float | None, float, dict[str, float]]:
    """Robust circular mean with outlier rejection.

    ``readings`` are ``(cue_id, heading_deg, confidence)``. The mean is taken
    on the unit circle, cues more than ``outlier_deg`` from it are dropped, and
    the mean is recomputed once from the survivors. Returns ``(heading,
    spread_deg, weights)``; ``weights`` names every cue, with zero for the ones
    consensus rejected, so a discarded outlier stays visible.
    """
    weights = {cue: 0.0 for cue, _heading, _confidence in readings}
    usable = [(cue, h, max(0.0, c)) for cue, h, c in readings if c > 0.0]
    if not usable:
        return (None, 0.0, weights)

    def _resultant(items: list[tuple[str, float, float]]) -> tuple[float, float]:
        x = sum(c * math.cos(math.radians(h)) for _cue, h, c in items)
        y = sum(c * math.sin(math.radians(h)) for _cue, h, c in items)
        total = sum(c for _cue, _h, c in items)
        length = math.hypot(x, y) / total if total > 1e-9 else 0.0
        return (math.degrees(math.atan2(y, x)), length)

    provisional, _strength = _resultant(usable)
    survivors = [item for item in usable if abs(wrap_deg(item[1] - provisional)) <= outlier_deg]
    if len(survivors) * 2 < len(usable):
        return (None, 180.0, weights)
    consensus, strength = _resultant(survivors)
    if strength < MIN_RESULTANT:
        return (None, 180.0, weights)
    for cue, _heading, confidence in survivors:
        weights[cue] = confidence
    spread = max(abs(wrap_deg(h - consensus)) for _cue, h, _c in survivors)
    return (wrap_deg(consensus), spread, weights)


def _band(value: float, low: float, peak_low: float, peak_high: float, high: float) -> float:
    """Trapezoidal membership in [0, 1], closed interval, zero outside."""
    if value < low or value > high:
        return 0.0
    if peak_low <= value <= peak_high:
        return 1.0
    if value < peak_low:
        return (value - low) / max(1e-9, peak_low - low)
    return (high - value) / max(1e-9, high - peak_high)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DetectorConfig:
    """Scoring bands and temporal bounds. Provisional configuration.

    Shape bands were fitted to the owner's seven measured crops; the tracker
    bounds and the term weights were chosen against the real-frame corpus's
    **tune** split (``tests/corpus/real``, sequences marked ``tune``) and are
    reported against its **eval** split. Nothing here is validated (E-PROF).
    """

    # -- proposals --------------------------------------------------------
    min_area_fraction: float = 0.00012
    max_area_fraction: float = 0.45
    morph_ksize: int = 3
    #: Closing kernel after the opening. Larger than the opening so a thin
    #: foreign line across the arrow (a UI stroke, a drawn marker) is bridged
    #: instead of splitting the outline into two candidates.
    close_ksize: int = 5
    blur_ksize: int = 3
    #: Components measured per pass, after the profile's area/aspect filter
    #: and ordered by area. Bounding *before* any mask is allocated is what
    #: keeps a frame full of same-coloured speckle from costing 50 ms.
    max_candidates: int = 8
    #: Splits attempted per pass, at most.
    max_splits: int = 2

    # -- local contrast ---------------------------------------------------
    ring_fraction: float = 0.35
    contrast_low: float = 1.04
    contrast_full: float = 1.25
    pixel_contrast_ratio: float = 1.08
    background_divisor: int = 8
    background_kernel_fraction: float = 0.28
    max_split_area_fraction: float = 0.25
    duplicate_iou: float = 0.6
    max_ring_radius_px: int = 41
    #: A measurement window larger than this is downscaled to fit before the
    #: ring, erosion and Sobel run over it. Contrast and edge ratios are
    #: scale-tolerant; a 900 000-pixel window dilated with a 41-pixel element
    #: was the tail of the full-frame pass on a view full of yellow.
    max_window_px: int = 120_000

    # -- shape ------------------------------------------------------------
    #: Measured 0.851-0.961 on clean masks; a disc sits at 0.99 and is
    #: deliberately outside the plateau.
    solidity_band: tuple[float, float, float, float] = (0.50, 0.72, 0.975, 1.0)
    #: Measured 0.467-0.686; a disc sits at 0.785.
    extent_band: tuple[float, float, float, float] = (0.25, 0.42, 0.72, 0.85)
    #: Measured 0.510-0.633 on the simplified outline; discs are above 0.85.
    circularity_band: tuple[float, float, float, float] = (0.28, 0.42, 0.75, 0.88)
    vertex_band: tuple[float, float, float, float] = (4.0, 5.0, 9.0, 14.0)
    #: Interior angle of the sharpest convex-hull vertex. The measured arrow
    #: tip is about 67 degrees flat-on; tail corners are 90.
    tip_angle_band: tuple[float, float, float, float] = (25.0, 40.0, 90.0, 108.0)

    # -- the two-notch signature -----------------------------------------
    notch_depth_band: tuple[float, float, float, float] = (0.015, 0.035, 0.22, 0.32)
    notch_ratio_band: tuple[float, float, float, float] = (0.25, 0.45, 1.0, 1.0)
    notch_third_max: float = 0.70

    # -- boundary ---------------------------------------------------------
    #: Contour gradient over the median gradient of the candidate's own
    #: surround. Local, because a frame-wide median reads flat terrain as
    #: "no edge anywhere".
    boundary_low: float = 1.2
    boundary_full: float = 3.0

    # -- term weights -----------------------------------------------------
    weights: tuple[tuple[str, float], ...] = (
        ("contrast", 1.10),
        ("topology", 1.60),
        ("tip", 0.80),
        ("solidity", 0.90),
        ("extent", 0.50),
        ("circularity", 0.50),
        ("vertices", 0.30),
        ("boundary", 0.80),
        ("chroma", 0.40),
        ("scale", 0.30),
        ("barbs", 0.40),
    )
    #: Share of the score a candidate keeps with no local contrast at all.
    contrast_floor: float = 0.45
    #: Chosen on the real corpus's tune split: at 0.58 the barbs term, which
    #: reads 0.2-0.5 on a real arrow, pushed a third of the true arrows under
    #: the bar without removing a single false lock on the eval split.
    accept_threshold: float = 0.55
    ambiguity_margin: float = 0.10

    # -- splitting --------------------------------------------------------
    split_below_score: float = 0.50
    max_split_children: int = 4
    min_split_fraction: float = 0.04

    # -- tracking ---------------------------------------------------------
    #: Temporal bounds are in **seconds of frame time**, with a frame-count
    #: floor, so the same contract holds at 60 fps live and at a corpus
    #: sampled at 2.5 fps: "several consistent frames" is three frames at
    #: 60 Hz and two at a slow replay, never one.
    top_k: int = 3
    #: Association gate: ``gate_base_px + gate_rate_px_s * dt`` since the last
    #: hit, capped at ``gate_fraction`` of the frame diagonal. The arrow
    #: crosses the whole view in about a second at the fastest camera turn.
    gate_base_px: float = 60.0
    gate_rate_px_s: float = 500.0
    gate_fraction: float = 0.35
    #: Permitted scale ratio: ``scale_gate + scale_rate_s * dt``.
    scale_gate: float = 1.25
    scale_rate_s: float = 1.5
    orientation_gate_deg: float = 55.0
    #: Consistent frames before a new identity is reported at all.
    acquire_min_frames: int = 2
    acquire_min_s: float = 0.04
    #: Consistent frames a challenger must sustain before the track switches.
    switch_min_frames: int = 2
    switch_min_s: float = 0.06
    #: How much better it must be, every one of those frames.
    switch_margin: float = 0.12
    #: Time without a hit before TRACK becomes REACQUIRE.
    max_track_age_s: float = 0.5
    #: Time in REACQUIRE before the memory is dropped (LOST).
    lost_after_s: float = 3.0
    #: Force a global search this often while tracking.
    reacquire_every_s: float = 0.75
    #: While **no** identity is held and the last search found nothing, full
    #: searches run at most this often. Acquisition costs at most one extra
    #: frame; a view with nothing to find stops costing a full pass per frame.
    idle_search_interval_s: float = 0.03
    #: Resume-outside-the-gate: how the same arrow is recognised somewhere
    #: else. Position is the identity cue a fast camera turn destroys and the
    #: one foliage cannot fake, so it is neither sufficient nor necessary on
    #: its own. When the positional gate finds nothing, a candidate may resume
    #: the held identity from anywhere in the frame if the three *other* cues
    #: agree and it is the unambiguous global best.
    #:
    #: Without this the gates only widen with elapsed time, and elapsed time
    #: only accumulates while the track is missing - so a perfectly visible
    #: arrow that jumped was refused until the gate crawled out to reach it.
    #: Measured on the rendered families: 100 px cost 67 ms of blindness,
    #: 250 px cost 367 ms, 400 px cost 567 ms and a new identity.
    #:
    #: The scale band is the widest of the three because scale is the identity
    #: cue that legitimately changes fastest - on approach, and whenever the
    #: arrow is clipped or partly occluded so the measured extent jumps.
    resume_scale_gate: float = 3.0
    #: A resume candidate must score at least this fraction of the track's own
    #: smoothed score. A faint blob is not the arrow we were following.
    resume_min_score_fraction: float = 0.75
    #: ...and must beat the runner-up by this much. Two similar candidates is
    #: exactly the same-coloured-foliage case this must never fire on.
    resume_margin: float = 0.2
    #: ...and the track must have been *seen* this recently. This is the bound
    #: that makes the whole rule sound rather than merely convenient: a resume
    #: is the claim "the arrow moved between two consecutive frames", and that
    #: claim is only supported while the frames are close together in time. At
    #: 60 Hz this is about three frames. On a corpus sampled at 5 fps it
    #: disables resume outright, which is correct: two frames 200 ms apart say
    #: nothing about whether a blob elsewhere is the same object, and letting
    #: a resume fire there took the same-coloured sand sequence from zero false
    #: locks to one.
    resume_max_age_s: float = 0.06
    #: Appearance gate: the contrast ratio may drift this much from the
    #: track's slowly adapted signature.
    appearance_gate: float = 0.45
    #: Signature adaptation rate on a confident hit. Slow, so a track cannot
    #: walk its appearance onto terrain one frame at a time.
    appearance_alpha: float = 0.08

    # -- direction --------------------------------------------------------
    min_anisotropy: float = 1.9
    cue_outlier_deg: float = 32.0
    max_cue_spread_deg: float = 28.0
    min_head_ratio: float = 0.45
    #: Minimum agreement between the polarity cues, in [0, 1].
    min_sign_margin: float = 0.30
    #: Minimum total vote weight before agreement counts at all.
    min_sign_evidence: float = 0.5
    #: Margin at which a reversal against the track's remembered polarity is
    #: adopted on the spot. Below it the reversal has to be *sustained* for
    #: ``reversal_latch_frames`` before it is believed - and until then the
    #: reading abstains rather than being inverted.
    reversal_margin: float = 0.55
    #: Consecutive frames a weakly-evidenced reversal must persist before it
    #: is adopted. Walking past the target genuinely reverses the arrow, and
    #: a guard with no way to accept that locks the navigator out of the
    #: whole rear half of the compass - permanently, because the refused
    #: heading is what the next frame is then compared against.
    reversal_latch_frames: int = 3
    #: ...and the same latch in **seconds**, because a frame count alone is a
    #: different promise at every cadence and this was the one temporal bound
    #: in this class that had not been given one. At the corpus's ~5 fps three
    #: frames is 600 ms; at 60 Hz it is 50 ms, which is short enough that a
    #: two-frame PCA flicker satisfied it and a spurious 180-degree flip was
    #: adopted as a sustained reversal. Both must hold, so this is a floor on
    #: the *duration* of the evidence and ``reversal_latch_frames`` stays the
    #: floor on its *quantity*.
    #:
    #: Chosen to be inert at corpus cadence by construction - 3 frames at 5 fps
    #: already exceeds it - so it changes nothing that was measured on the real
    #: frames and only tightens the dense regime the corpus cannot reach.
    reversal_latch_s: float = 0.12

    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="owner crops measured 2026-08-28 (D-024); term weights and "
            "tracker bounds chosen on tests/corpus/real tune split (D-029)",
            note="E-PROF and E-DIR-E2E are PENDING; no threshold here is validated",
        )
    )

    def weight_for(self, term: str) -> float:
        for name, weight in self.weights:
            if name == term:
                return weight
        return 0.0


# ---------------------------------------------------------------------------
# Shape features
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShapeFeatures:
    """Everything geometric a scorer or a direction cue needs from one blob.

    Always expressed in **full-frame canonical pixels**, whichever pass
    produced the contour.
    """

    area_px: float
    bbox_px: tuple[int, int, int, int]
    centroid_px: tuple[float, float]
    solidity: float
    extent: float
    circularity: float
    vertices: int
    diagonal_px: float
    anisotropy: float
    axis_unit_xy: tuple[float, float] | None
    notches: tuple[tuple[tuple[float, float], float], ...]
    third_notch_depth_px: float
    clipped: bool
    contour_px: tuple[tuple[int, int], ...]
    hull_px: tuple[tuple[float, float], ...]
    #: The sharpest convex-hull vertex and its interior angle. The tip of an
    #: arrow is its sharpest corner; a tail corner is a right angle.
    sharpest_px: tuple[float, float] | None = None
    sharpest_angle_deg: float = 180.0
    #: The next-sharpest hull corner. An arrow has one tip and two right-angled
    #: tail corners; a disc's simplified hull has eight corners all alike.
    second_sharpest_angle_deg: float = 180.0
    #: Concavities deeper than 2.5% of the diagonal. An arrow has two.
    deep_defects: int = 0

    @property
    def tip_prominence(self) -> float:
        """How much sharper the sharpest corner is than the next, in [0, 1]."""
        return max(
            0.0, min(1.0, (self.second_sharpest_angle_deg - self.sharpest_angle_deg) / 15.0)
        )

    @property
    def notch_depths_norm(self) -> tuple[float, float]:
        diag = max(1.0, self.diagonal_px)
        depths = [depth / diag for _point, depth in self.notches]
        while len(depths) < 2:
            depths.append(0.0)
        return (depths[0], depths[1])

    @property
    def notch_mid_px(self) -> tuple[float, float] | None:
        if len(self.notches) < 2:
            return None
        (x1, y1), _d1 = self.notches[0]
        (x2, y2), _d2 = self.notches[1]
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    @property
    def notch_separation_px(self) -> float:
        if len(self.notches) < 2:
            return 0.0
        (x1, y1), _d1 = self.notches[0]
        (x2, y2), _d2 = self.notches[1]
        return float(math.hypot(x2 - x1, y2 - y1))


_NOTCH_CANDIDATES = 4
_MIN_NOTCH_DEPTH_FRACTION = 0.015
#: A defect shallower than this many pixels is rasterisation, whatever the
#: diagonal: on a 35-pixel blob the fractional floor is half a pixel.
_MIN_NOTCH_DEPTH_PX = 2.5


def _select_notch_pair(
    defects: list[tuple[tuple[float, float], float]],
    centroid: tuple[float, float],
    diagonal: float,
) -> tuple[tuple[tuple[tuple[float, float], float], ...], float]:
    """Pick the two defects that are actually the arrowhead's notches.

    The real pair has a property nothing else does: the segment joining them
    passes close to the centroid, because it is the shape's own waist.
    """
    floor = max(_MIN_NOTCH_DEPTH_PX, _MIN_NOTCH_DEPTH_FRACTION * diagonal)
    deep = [item for item in defects if item[1] >= floor]
    if len(deep) < 2:
        return (tuple(deep[:2]), 0.0)
    considered = deep[:_NOTCH_CANDIDATES]
    best: tuple[float, int, int] | None = None
    for i in range(len(considered)):
        for j in range(i + 1, len(considered)):
            (ax, ay), depth_a = considered[i]
            (bx, by), depth_b = considered[j]
            span = math.hypot(bx - ax, by - ay)
            if span < 1e-6 or depth_a < 1e-6:
                continue
            offset = abs((bx - ax) * (ay - centroid[1]) - (ax - centroid[0]) * (by - ay)) / span
            waist = 1.0 - min(1.0, offset / max(1e-6, 0.22 * diagonal))
            balance = min(depth_a, depth_b) / max(depth_a, depth_b)
            depth = min(1.0, (depth_a + depth_b) / max(1e-6, 0.10 * diagonal))
            score = 0.4 * waist + 0.25 * balance + 0.35 * depth
            if best is None or score > best[0]:
                best = (score, i, j)
    if best is None:
        return (tuple(defects[:2]), defects[2][1] if len(defects) > 2 else 0.0)
    _score, i, j = best
    pair = (considered[i], considered[j])
    remainder = [d for k, d in enumerate(considered) if k not in (i, j)]
    return (pair, remainder[0][1] if remainder else 0.0)


def _sharpest_vertex(hull: NDArray[Any]) -> tuple[tuple[float, float] | None, float, float]:
    """The hull vertex with the smallest interior angle, and the runner-up angle."""
    points = hull.reshape(-1, 2).astype(np.float64)
    count = len(points)
    if count < 3:
        return (None, 180.0, 180.0)
    angles: list[tuple[float, tuple[float, float]]] = []
    for index in range(count):
        previous = points[index - 1]
        current = points[index]
        following = points[(index + 1) % count]
        a = previous - current
        b = following - current
        norm = float(np.hypot(*a) * np.hypot(*b))
        if norm < 1e-9:
            continue
        cosine = float(np.dot(a, b) / norm)
        angle = math.degrees(math.acos(max(-1.0, min(1.0, cosine))))
        angles.append((angle, (float(current[0]), float(current[1]))))
    if not angles:
        return (None, 180.0, 180.0)
    angles.sort(key=lambda item: item[0])
    second = angles[1][0] if len(angles) > 1 else 180.0
    return (angles[0][1], angles[0][0], second)


def _contour_features(
    contour: NDArray[Any], frame_size: tuple[int, int]
) -> ShapeFeatures | None:
    """Derive every geometric feature from a **full-frame** contour.

    ``frame_size`` is the full canonical size, so clipping means touching the
    real edge of the frame and never the edge of a search region.
    """
    import cv2

    area = float(cv2.contourArea(contour))
    if area < 8.0 or len(contour) < 5:
        return None
    x, y, width, height = cv2.boundingRect(contour)
    if width < 3 or height < 3:
        return None
    hull = cv2.convexHull(contour)
    hull_area = float(cv2.contourArea(hull))
    perimeter = float(cv2.arcLength(contour, True))
    if hull_area <= 0.0 or perimeter <= 0.0:
        return None
    approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
    # The tip angle is read off a simplified hull: on the raw hull two
    # near-collinear vertices a pixel apart read as a 170-degree "corner".
    simple_hull = cv2.approxPolyDP(
        hull, 0.015 * max(1.0, float(cv2.arcLength(hull, True))), True
    )
    # Circularity from the simplified outline: a nick a few pixels deep
    # inflates the raw perimeter and reads a clean arrow as ragged.
    simple_perimeter = max(float(cv2.arcLength(approx, True)), 1e-6)
    moments = cv2.moments(contour)
    if abs(moments["m00"]) < 1e-6:
        return None
    centroid = (moments["m10"] / moments["m00"], moments["m01"] / moments["m00"])

    mu20 = moments["mu20"] / moments["m00"]
    mu02 = moments["mu02"] / moments["m00"]
    mu11 = moments["mu11"] / moments["m00"]
    common = math.sqrt(max(0.0, (mu20 - mu02) ** 2 + 4.0 * mu11**2))
    lambda1 = (mu20 + mu02 + common) / 2.0
    lambda2 = (mu20 + mu02 - common) / 2.0
    anisotropy = math.sqrt(lambda1 / lambda2) if lambda2 > 1e-9 else float("inf")
    axis: tuple[float, float] | None = None
    if lambda1 > 1e-9:
        angle = 0.5 * math.atan2(2.0 * mu11, mu20 - mu02)
        axis = (math.cos(angle), math.sin(angle))

    depths: list[tuple[tuple[float, float], float]] = []
    hull_indices = cv2.convexHull(contour, returnPoints=False)
    if hull_indices is not None and len(hull_indices) > 3:
        defects = cv2.convexityDefects(contour, hull_indices)
        if defects is not None:
            flat = defects.reshape(-1, defects.shape[-1])
            for entry in flat:
                far = contour[int(entry[2])][0]
                depths.append(((float(far[0]), float(far[1])), float(entry[3]) / 256.0))
    depths.sort(key=lambda item: item[1], reverse=True)
    diagonal = float(math.hypot(width, height))
    notches, third = _select_notch_pair(depths, centroid, diagonal)
    deep = sum(
        1 for _point, depth in depths if depth >= max(_MIN_NOTCH_DEPTH_PX, 0.025 * diagonal)
    )
    sharpest, sharpest_angle, second_angle = _sharpest_vertex(
        simple_hull if len(simple_hull) >= 3 else hull
    )

    frame_w, frame_h = frame_size
    return ShapeFeatures(
        area_px=area,
        bbox_px=(x, y, width, height),
        centroid_px=centroid,
        solidity=area / hull_area,
        extent=area / float(width * height),
        circularity=min(1.0, 4.0 * math.pi * area / (simple_perimeter * simple_perimeter)),
        vertices=len(approx),
        diagonal_px=diagonal,
        anisotropy=float(min(anisotropy, 99.0)),
        axis_unit_xy=axis,
        notches=notches,
        third_notch_depth_px=third,
        clipped=x <= 1 or y <= 1 or x + width >= frame_w - 1 or y + height >= frame_h - 1,
        contour_px=tuple((int(p[0][0]), int(p[0][1])) for p in approx),
        hull_px=tuple((float(p[0][0]), float(p[0][1])) for p in hull),
        sharpest_px=sharpest,
        sharpest_angle_deg=sharpest_angle,
        second_sharpest_angle_deg=second_angle,
        deep_defects=deep,
    )


# ---------------------------------------------------------------------------
# Hypotheses and proposal sets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArrowHypothesis:
    """One scored candidate with the breakdown that produced the score.

    ``state`` is assigned by :meth:`ArrowDetector.commit` and is one of
    ``proposed`` (scored, below threshold), ``viable`` (above threshold, not
    chosen), ``selected`` (the one the observation is built from - at most
    one per frame), ``challenger`` (a viable candidate contesting the held
    identity), ``rejected`` (failed a hard constraint, with the reason).
    """

    label: int
    features: ShapeFeatures
    terms: tuple[tuple[str, float], ...]
    score: float
    accepted: bool
    reason: str | None
    source: str = "full"
    state: str = "proposed"
    #: The contrast ratio, which is the appearance signature the tracker gates on.
    signature: float = 0.0

    def term(self, name: str) -> float:
        for key, value in self.terms:
            if key == name:
                return value
        return 0.0

    @property
    def weakest_term(self) -> str:
        if not self.terms:
            return "no terms"
        return min(self.terms, key=lambda item: item[1])[0]

    def as_record(self) -> ArrowCandidateRecord:
        return ArrowCandidateRecord(
            label=self.label,
            area_px=int(self.features.area_px),
            bbox_px=self.features.bbox_px,
            centroid_px=self.features.centroid_px,
            score=round(self.score, 4),
            accepted=self.state == "selected",
            rejected_reason=self.reason if self.state != "selected" else None,
            score_terms=self.terms,
            contour_px=self.features.contour_px,
            state=self.state,
        )


@dataclass(frozen=True)
class ProposalStats:
    """Cost accounting for one proposal pass."""

    source: str
    roi_px: tuple[int, int, int, int] | None
    elapsed_ms: float
    raw_components: int
    evaluated: int
    mask_pixels: int
    splits: int


@dataclass(frozen=True)
class ProposalSet:
    """Every hypothesis one pass produced, in full-frame coordinates."""

    hypotheses: tuple[ArrowHypothesis, ...]
    stats: ProposalStats
    abstain_reason: str | None = None


class TrackState(Enum):
    ACQUIRE = "acquire"
    TRACK = "track"
    AMBIGUOUS = "ambiguous"
    REACQUIRE = "reacquire"
    LOST = "lost"


@dataclass(frozen=True)
class DetectionOutcome:
    """What one commit decided, and from which candidate."""

    observation: ArrowObservation
    selected: ArrowHypothesis | None
    #: Every hypothesis after fusion, with its state assigned.
    hypotheses: tuple[ArrowHypothesis, ...]
    #: One word: acquire, acquiring, track, switch, hold, ambiguous,
    #: reacquire, resume, none.
    decision: str
    state: TrackState


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


class _Scorer:
    """Turns one component into a weighted, explainable score."""

    def __init__(self, config: DetectorConfig, profile: Any = None) -> None:
        self._config = config
        # The profile's area range is a soft band: full credit from twice the
        # minimum to half the maximum, tapering to zero at the bounds, so a
        # blob the size of the smallest admissible arrow is a weak candidate
        # rather than an equal one.
        low = float(getattr(profile, "min_area_px", 0) or 0)
        high = float(getattr(profile, "max_area_px", 0) or 0)
        if high <= low:
            low, high = 0.0, 1e9
        self._scale_band = (low, 2.0 * low, 0.5 * high, high)

    def score(
        self,
        features: ShapeFeatures,
        *,
        contrast: float,
        chroma: float,
        boundary: float,
        barbs: float,
        frame_area: float,
    ) -> tuple[tuple[tuple[str, float], ...], float]:
        config = self._config
        depth_1, depth_2 = features.notch_depths_norm
        ratio = depth_2 / depth_1 if depth_1 > 1e-9 else 0.0
        third_ratio = (
            features.third_notch_depth_px / max(1e-9, features.notches[1][1])
            if len(features.notches) > 1
            else 1.0
        )
        topology = (
            _band(depth_1, *config.notch_depth_band)
            * _band(ratio, *config.notch_ratio_band)
            * (1.0 if third_ratio <= config.notch_third_max else 0.0)
        )
        area_fraction = features.area_px / max(1.0, frame_area)
        in_fraction = config.min_area_fraction <= area_fraction <= config.max_area_fraction
        scale = _band(features.area_px, *self._scale_band) if in_fraction else 0.0
        terms: tuple[tuple[str, float], ...] = (
            (
                "contrast",
                _band(contrast, config.contrast_low, config.contrast_full, 99.0, 100.0),
            ),
            ("topology", topology),
            (
                "tip",
                _band(features.sharpest_angle_deg, *config.tip_angle_band)
                * features.tip_prominence,
            ),
            ("solidity", _band(features.solidity, *config.solidity_band)),
            ("extent", _band(features.extent, *config.extent_band)),
            ("circularity", _band(features.circularity, *config.circularity_band)),
            ("vertices", _band(float(features.vertices), *config.vertex_band)),
            (
                "boundary",
                _band(boundary, config.boundary_low, config.boundary_full, 99.0, 100.0),
            ),
            ("chroma", max(0.0, min(1.0, chroma))),
            ("scale", scale),
            ("barbs", max(0.0, min(1.0, barbs))),
        )
        total_weight = sum(config.weight_for(name) for name, _value in terms)
        weighted = sum(config.weight_for(name) * value for name, value in terms)
        base = weighted / total_weight if total_weight > 0 else 0.0
        # Local contrast is the one property every real view shared: the
        # arrow is brighter than whatever is behind it (measured 1.21-2.65).
        # A flat patch of terrain under a UI label had a fine outline and no
        # contrast at all, and scored 0.76 as a plain weighted sum. So a
        # candidate keeps only ``contrast_floor`` of its score with no
        # contrast, which puts it below acceptance whatever else it has.
        contrast_term = dict(terms)["contrast"]
        score = base * (config.contrast_floor + (1.0 - config.contrast_floor) * contrast_term)
        # No structural veto beyond contrast. Requiring the notch signature,
        # barbs, a prominent tip, or an outline with exactly two concavities
        # as a *precondition* was each measured to cut recall on the real
        # corpus by a third or more, where thin UI strokes nick the outline
        # and the notch pair is misread. Structure is weighted evidence; the
        # temporal machine and the real-frame negatives carry the rest.
        # A clipped arrow is still the arrow; clipping costs confidence and
        # the completeness claim of the shape terms, never the candidate.
        if features.clipped:
            score *= 0.80
        return (terms, round(score, 4))


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------


@dataclass
class _Track:
    """The held identity. Mutable on purpose: it lives inside the detector."""

    track_id: int
    centroid: tuple[float, float]
    velocity: tuple[float, float]
    scale: float
    heading: float | None
    signature: float
    score: float
    last_hit_s: float
    lost_since_s: float | None = None
    hits: int = 1
    misses: int = 0


@dataclass
class _Streak:
    """A candidate being counted toward acquisition or a switch."""

    centroid: tuple[float, float]
    scale: float
    started_s: float
    last_s: float
    frames: int = 1


class ArrowDetector:
    """Proposal, fusion, and one committed temporal transaction per frame."""

    def __init__(
        self,
        profile: Any,
        config: DetectorConfig | None = None,
        *,
        exclusion_regions_px: tuple[tuple[int, int, int, int], ...] = (),
    ) -> None:
        self._profile = profile
        self._config = config or DetectorConfig()
        self._scorer = _Scorer(self._config, profile)
        # The profile's safe exclusions plus any the caller adds.
        self._exclusions = tuple(getattr(profile, "exclusion_regions_px", ()) or ()) + tuple(
            exclusion_regions_px
        )
        self._next_track_id = 0
        self.reset()

    # -- lifecycle --------------------------------------------------------
    def reset(self) -> None:
        """Drop every piece of temporal state. Called on any world change."""
        self._state = TrackState.ACQUIRE
        self._track: _Track | None = None
        self._acquire: _Streak | None = None
        self._challenger: _Streak | None = None
        self._last_global_s: float | None = None
        self._global_due = True
        self._last_sequence: int | None = None
        self._last_outcome: DetectionOutcome | None = None
        self._now_s = 0.0
        self._diagonal = 1468.6
        self.duplicate_commits = 0
        self._last_search_s: float | None = None
        self._last_viable = False
        self._last_decision = "none"
        self.switches = 0
        self.reacquisitions = 0
        self.resumes = 0

    @property
    def profile(self) -> Any:
        return self._profile

    @property
    def config(self) -> DetectorConfig:
        return self._config

    @property
    def state(self) -> TrackState:
        return self._state

    @property
    def track_id(self) -> int | None:
        return self._track.track_id if self._track is not None else None

    @property
    def track_age(self) -> int:
        """Hits on the held identity; zero while nothing is held."""
        return (
            self._track.hits
            if self._track is not None and self._state is TrackState.TRACK
            else 0
        )

    @property
    def last_decision(self) -> str:
        return self._last_decision

    def wants_global_search(self) -> bool:
        """Whether the next pass should cover the full frame.

        True whenever no identity is held, after a region miss, and on the
        periodic challenge cadence. The pipeline reads this instead of
        deciding for itself, so a region miss never turns into a synchronous
        second pass on the same screenshot.
        """
        if self._state is not TrackState.TRACK or self._track is None:
            return True
        if self._global_due or self._last_global_s is None:
            return True
        return self._now_s - self._last_global_s >= self._config.reacquire_every_s

    def search_due(self, now_s: float) -> bool:
        """Whether a full search should run at all on a frame at ``now_s``.

        Always while an identity is held or being reacquired. While nothing
        is held and the previous search found nothing viable, searches are
        spaced by ``idle_search_interval_s`` so an empty view does not cost a
        full pass on every frame.
        """
        if self._last_search_s is None:
            return True
        spaced = now_s - self._last_search_s >= self._config.idle_search_interval_s
        if self._state is TrackState.TRACK and self._track is not None:
            # A held identity with no candidate in its gate: the global
            # challenge runs at the idle spacing, not on every frame.
            return not self._global_due or spaced
        if self._state not in (TrackState.ACQUIRE, TrackState.LOST):
            return True
        if self._acquire is not None or self._last_viable:
            return True
        return spaced

    def note_skipped(self, frame: CapturedFrame) -> DetectionOutcome:
        """Record a frame that was observed without a search. No state moves."""
        self._last_sequence = frame.sequence
        self._now_s = frame.captured_at_s
        return self._outcome(_abstain(self._profile, "search-skipped"), None, (), "skipped")

    def predicted_centroid(self) -> tuple[float, float] | None:
        """Constant-velocity prediction, used only to *prioritize* a search."""
        track = self._track
        if track is None or self._state not in (TrackState.TRACK, TrackState.AMBIGUOUS):
            return None
        elapsed = max(0.0, self._now_s - track.last_hit_s)
        return (
            track.centroid[0] + track.velocity[0] * elapsed,
            track.centroid[1] + track.velocity[1] * elapsed,
        )

    def predicted_scale_px(self) -> float | None:
        return self._track.scale if self._track is not None else None

    # -- channels and masks ----------------------------------------------
    def _channels(self, bgr: NDArray[Any]) -> dict[str, NDArray[Any]]:
        import cv2

        blur = self._config.blur_ksize | 1
        smoothed = cv2.GaussianBlur(bgr, (blur, blur), 0) if blur > 1 else bgr
        # Integer channels for the colour rule; one float array for luminance.
        # Seven full-frame float32 arrays per pass were a measurable share of
        # the old 24 ms full-frame cost, and the rules only need integers.
        blue, green, red = (smoothed[:, :, i].astype(np.int16) for i in range(3))
        luminance = cv2.cvtColor(smoothed, cv2.COLOR_BGR2GRAY).astype(np.float32)
        return {"b": blue, "g": green, "r": red, "luminance": luminance}

    @staticmethod
    def _chroma_fractions(channels: dict[str, NDArray[Any]]) -> dict[str, NDArray[Any]]:
        """Per-channel share of the sum, computed only for rules that need it."""
        blue = channels["b"].astype(np.float32)
        green = channels["g"].astype(np.float32)
        red = channels["r"].astype(np.float32)
        total = np.maximum(blue + green + red, 1.0)
        return {"g_chroma": green / total, "r_chroma": red / total, "b_chroma": blue / total}

    def _local_background(self, luminance: NDArray[Any]) -> NDArray[Any]:
        import cv2

        height, width = luminance.shape[:2]
        divisor = max(1, self._config.background_divisor)
        small = cv2.resize(
            luminance,
            (max(8, width // divisor), max(8, height // divisor)),
            interpolation=cv2.INTER_AREA,
        )
        kernel = max(3, int(small.shape[1] * self._config.background_kernel_fraction)) | 1
        blurred = cv2.blur(small, (kernel, kernel))
        result: NDArray[Any] = cv2.resize(
            blurred, (width, height), interpolation=cv2.INTER_LINEAR
        )
        return result

    def _rule_mask(self, channels: dict[str, NDArray[Any]]) -> NDArray[np.bool_]:
        """The profile's colour rule, as a boolean array. Loose on purpose."""
        import cv2

        parameters = getattr(self._profile, "parameters", {}) or {}
        rule = getattr(self._profile, "rule", "chroma_band")
        if rule == "chroma_band":
            fractions = self._chroma_fractions(channels)
            keep = (
                (fractions["g_chroma"] >= float(parameters.get("min_green_chroma", 0.38)))
                & (fractions["g_chroma"] <= float(parameters.get("max_green_chroma", 0.70)))
                & (fractions["b_chroma"] <= float(parameters.get("max_blue_chroma", 0.30)))
                & (fractions["r_chroma"] >= float(parameters.get("min_red_chroma", 0.20)))
                & (channels["g"] >= int(parameters.get("min_green", 90)))
            )
        elif rule == "channel_relation":
            floor = np.minimum(channels["r"], channels["g"])
            close = np.abs(channels["r"] - channels["g"]) <= int(
                parameters.get("max_rg_delta", 28)
            )
            bright = floor >= int(parameters.get("min_rg", 150))
            suppressed = channels["b"] <= floor - int(parameters.get("min_blue_gap", 60))
            keep = close & bright & suppressed
        elif rule == "hsv_range":
            lower = np.array(parameters["lower_hsv"], dtype=np.uint8)
            upper = np.array(parameters["upper_hsv"], dtype=np.uint8)
            stacked = np.dstack([channels["b"], channels["g"], channels["r"]]).astype(np.uint8)
            hsv = cv2.cvtColor(stacked, cv2.COLOR_BGR2HSV)
            keep = cv2.inRange(hsv, lower, upper) > 0
        elif rule == "bgr_range":
            lower = np.array(parameters["lower_bgr"], dtype=np.int16)
            upper = np.array(parameters["upper_bgr"], dtype=np.int16)
            keep = np.ones(channels["g"].shape, dtype=bool)
            for index, name in enumerate(("b", "g", "r")):
                keep &= (channels[name] >= lower[index]) & (channels[name] <= upper[index])
        else:  # pragma: no cover - guarded by the profile schema
            raise ValueError(f"unknown profile rule {rule!r}")
        result: NDArray[np.bool_] = np.asarray(keep, dtype=bool)
        return result

    def _apply_exclusions(
        self, mask: NDArray[np.uint8], offset: tuple[int, int]
    ) -> NDArray[np.uint8]:
        """Zero the excluded regions, translated into the mask's own basis."""
        offset_x, offset_y = offset
        height, width = mask.shape[:2]
        for x, y, w, h in self._exclusions:
            left, top = max(0, x - offset_x), max(0, y - offset_y)
            right, bottom = min(width, x - offset_x + w), min(height, y - offset_y + h)
            if right > left and bottom > top:
                mask[top:bottom, left:right] = 0
        return mask

    def _clean(self, mask: NDArray[np.uint8]) -> NDArray[np.uint8]:
        import cv2

        size = self._config.morph_ksize | 1
        if size > 1:
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
            mask = np.asarray(cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel), dtype=np.uint8)
        close = self._config.close_ksize | 1
        if close > 1:
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close, close))
            mask = np.asarray(cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel), dtype=np.uint8)
        result: NDArray[np.uint8] = np.asarray(mask, dtype=np.uint8)
        return result

    # -- measurement helpers ---------------------------------------------
    @staticmethod
    def _window(
        bbox: tuple[int, int, int, int], pad: int, shape: tuple[int, int]
    ) -> tuple[int, int, int, int]:
        x, y, w, h = bbox
        top, left = max(0, y - pad), max(0, x - pad)
        bottom, right = min(shape[0], y + h + pad), min(shape[1], x + w + pad)
        return (top, left, bottom, right)

    def _local_measurements(
        self,
        luminance: NDArray[Any],
        component: NDArray[np.uint8],
        local_bbox: tuple[int, int, int, int],
        diagonal_px: float,
        contour_local: NDArray[Any],
    ) -> tuple[float, float]:
        """``(interior/ring luminance ratio, contour gradient over ring gradient)``.

        Everything is measured in a window around the candidate: the ring
        with a bounded kernel, and the gradient with a Sobel over the window
        only. A whole-frame Sobel cost 2-3 ms per pass and a whole-frame
        proposal once asked for a 257-pixel structuring element.
        """
        import cv2

        config = self._config
        radius = max(
            3, min(config.max_ring_radius_px, int(diagonal_px * config.ring_fraction * 0.5)) | 1
        )
        top, left, bottom, right = self._window(local_bbox, radius + 4, luminance.shape[:2])
        window = component[top:bottom, left:right]
        light = luminance[top:bottom, left:right]
        if window.size == 0:
            return (0.0, 0.0)
        shrink = 1
        while window.size / (shrink * shrink) > config.max_window_px:
            shrink *= 2
        if shrink > 1:
            size = (max(8, window.shape[1] // shrink), max(8, window.shape[0] // shrink))
            window = np.asarray(
                cv2.resize(window, size, interpolation=cv2.INTER_NEAREST), dtype=np.uint8
            )
            light = cv2.resize(light, size, interpolation=cv2.INTER_AREA)
            radius = max(3, (radius // shrink) | 1)
            contour_local = contour_local // shrink
            left, top = left // shrink, top // shrink
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (radius, radius))
        near = cv2.dilate(window, np.ones((5, 5), np.uint8))
        ring_mask = cv2.subtract(cv2.dilate(window, kernel), near)
        inner = cv2.erode(window, np.ones((5, 5), np.uint8))
        interior = light[inner > 0] if np.any(inner) else light[window > 0]
        ring = ring_mask > 0
        surround = light[ring]
        if interior.size == 0 or surround.size == 0:
            return (0.0, 0.0)
        contrast = float(interior.mean() / max(1.0, surround.mean()))
        gradient = np.abs(cv2.Sobel(light, cv2.CV_32F, 1, 0, ksize=3)) + np.abs(
            cv2.Sobel(light, cv2.CV_32F, 0, 1, ksize=3)
        )
        reference = float(np.median(gradient[ring])) if ring.any() else 0.0
        xs = np.clip(contour_local[:, 0] - left, 0, gradient.shape[1] - 1)
        ys = np.clip(contour_local[:, 1] - top, 0, gradient.shape[0] - 1)
        samples = gradient[ys, xs] if contour_local.size else np.zeros(0, dtype=np.float32)
        boundary = float(np.mean(samples) / max(reference, 1.0)) if samples.size else 0.0
        return (contrast, boundary)

    @staticmethod
    def _chroma_fit(channels: dict[str, NDArray[Any]], selected: NDArray[np.bool_]) -> float:
        """How green/yellow-dominant the interior is, on a normalized scale."""
        if not np.any(selected):
            return 0.0
        blue = float(channels["b"][selected].mean())
        green = float(channels["g"][selected].mean())
        red = float(channels["r"][selected].mean())
        total = max(1.0, blue + green + red)
        return max(0.0, min(1.0, (green - blue) / total * 2.2))

    # -- the proposal pass ------------------------------------------------
    def propose(
        self, frame: CapturedFrame, *, roi_px: tuple[int, int, int, int] | None = None
    ) -> ProposalSet:
        """Score every plausible component in the frame or a region of it.

        Stateless with respect to the track: the same frame and region give
        the same proposals whatever was tracked before. Everything returned is
        in full-frame canonical coordinates.
        """
        import cv2

        started = monotonic_s()
        source = "roi" if roi_px is not None else "full"

        def empty(reason: str) -> ProposalSet:
            return ProposalSet(
                (),
                ProposalStats(source, roi_px, (monotonic_s() - started) * 1000.0, 0, 0, 0, 0),
                reason,
            )

        if frame.capture_error is not None:
            return empty(f"capture-error:{frame.capture_error}")
        if not frame.geometry.valid:
            return empty("viewport-invalid")
        supported = getattr(self._profile, "supported_client_size_px", None)
        full_w, full_h = frame.canonical_size_px
        if supported is not None and (int(supported[0]), int(supported[1])) != (full_w, full_h):
            return empty("unsupported-viewport-size")

        bgr = np.asarray(frame.bgr)
        offset_x, offset_y = 0, 0
        if roi_px is not None:
            offset_x, offset_y, roi_w, roi_h = roi_px
            bgr = np.ascontiguousarray(
                bgr[offset_y : offset_y + roi_h, offset_x : offset_x + roi_w]
            )
        if bgr.size == 0:
            return empty("empty-region")

        config = self._config
        channels = self._channels(bgr)
        luminance = channels["luminance"]
        rule = self._rule_mask(channels)
        rule_mask = self._apply_exclusions(rule.astype(np.uint8) * 255, (offset_x, offset_y))
        # Two proposal sources, because neither alone covers the real cases.
        # *Colour and locally bright* isolates the arrow from terrain of its
        # own colour (the daylight grass case, where the plain colour mask is
        # one component covering the frame). *Colour alone* is kept because
        # the bright test fails in the opposite situation: an arrow filling a
        # quarter of the view is its own local background. Both are cheap;
        # the object found by both is measured once.
        background = self._local_background(luminance)
        bright = luminance >= background * config.pixel_contrast_ratio
        bright_mask = self._clean(np.where(bright, rule_mask, 0).astype(np.uint8))
        chroma_mask = self._clean(rule_mask)
        frame_area = float(full_w * full_h)

        raw_components = 0
        evaluated = 0
        mask_pixels = 0
        splits = 0
        hypotheses: list[ArrowHypothesis] = []
        seen_boxes: list[tuple[int, int, int, int]] = []

        for mask_source, mask in (("bright", bright_mask), ("chroma", chroma_mask)):
            base = 0 if mask_source == "bright" else 500
            count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
                mask, connectivity=8
            )
            raw_components += max(0, count - 1)
            plausible = self._plausible_components(stats, count, frame_area)
            for rank, (label, area, local_bbox) in enumerate(plausible):
                if evaluated >= config.max_candidates:
                    break
                global_bbox = (
                    local_bbox[0] + offset_x,
                    local_bbox[1] + offset_y,
                    local_bbox[2],
                    local_bbox[3],
                )
                if any(
                    _overlap_ratio(global_bbox, box) >= config.duplicate_iou
                    for box in seen_boxes
                ):
                    continue
                x, y, w, h = local_bbox
                component = np.zeros((h, w), dtype=np.uint8)
                component[labels[y : y + h, x : x + w] == label] = 255
                mask_pixels += w * h
                evaluated += 1
                scored = self._evaluate(
                    base + label,
                    component,
                    local_bbox,
                    channels,
                    rule,
                    luminance,
                    frame_area,
                    (full_w, full_h),
                    (offset_x, offset_y),
                    f"{source}:{mask_source}",
                )
                if scored is None:
                    continue
                seen_boxes.append(global_bbox)
                hypotheses.append(scored)
                # A poor score on a large component is the merged-blob case:
                # the arrow welded to matching terrain by the closing. Bounded
                # to a few of the largest components per pass.
                if (
                    scored.score < config.split_below_score
                    and splits < config.max_splits
                    and area <= config.max_split_area_fraction * frame_area
                    and rank < 3
                ):
                    splits += 1
                    for index, (child, child_bbox) in enumerate(
                        self._split(component, local_bbox, luminance, frame_area)
                    ):
                        mask_pixels += child_bbox[2] * child_bbox[3]
                        evaluated += 1
                        split = self._evaluate(
                            (base + label) * 1000 + index + 1,
                            child,
                            child_bbox,
                            channels,
                            rule,
                            luminance,
                            frame_area,
                            (full_w, full_h),
                            (offset_x, offset_y),
                            f"{source}:{mask_source}-split",
                        )
                        if split is not None:
                            hypotheses.append(split)

        hypotheses.sort(key=lambda h: h.score, reverse=True)
        stats_out = ProposalStats(
            source,
            roi_px,
            (monotonic_s() - started) * 1000.0,
            raw_components,
            evaluated,
            mask_pixels,
            splits,
        )
        return ProposalSet(tuple(hypotheses), stats_out, None)

    def _plausible_components(
        self, stats: NDArray[Any], count: int, frame_area: float
    ) -> list[tuple[int, int, tuple[int, int, int, int]]]:
        """Apply the profile's area and aspect contract, then order by area.

        The profile is the authority on what size and shape the arrow can
        be; the detector's own fractions are a second, looser bound.
        """
        import cv2

        config = self._config
        profile = self._profile
        min_area = max(
            config.min_area_fraction * frame_area, float(getattr(profile, "min_area_px", 0))
        )
        max_area = min(
            config.max_area_fraction * frame_area,
            float(getattr(profile, "max_area_px", frame_area)),
        )
        min_aspect = float(getattr(profile, "min_aspect", 0.0))
        max_aspect = float(getattr(profile, "max_aspect", 1e9))
        found: list[tuple[int, int, tuple[int, int, int, int]]] = []
        for label in range(1, count):
            area = int(stats[label, cv2.CC_STAT_AREA])
            if area < min_area or area > max_area:
                continue
            x = int(stats[label, cv2.CC_STAT_LEFT])
            y = int(stats[label, cv2.CC_STAT_TOP])
            w = int(stats[label, cv2.CC_STAT_WIDTH])
            h = int(stats[label, cv2.CC_STAT_HEIGHT])
            aspect = w / h if h else 0.0
            if not min_aspect <= aspect <= max_aspect:
                continue
            found.append((label, area, (x, y, w, h)))
        found.sort(key=lambda item: item[1], reverse=True)
        return found

    def _split(
        self,
        component: NDArray[np.uint8],
        local_bbox: tuple[int, int, int, int],
        luminance: NDArray[Any],
        frame_area: float,
    ) -> list[tuple[NDArray[np.uint8], tuple[int, int, int, int]]]:
        """Split one component against its own luminance histogram, once.

        This is the daylight fix: a bright arrow welded to matching terrain
        by the closing is bimodal in luminance and Otsu separates it.
        """
        import cv2

        x, y, w, h = local_bbox
        window = luminance[y : y + h, x : x + w]
        selected = component > 0
        values = window[selected]
        if values.size < 64:
            return []
        scaled = np.clip(values, 0, 255).astype(np.uint8)
        threshold, _ = cv2.threshold(scaled, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        bright = np.zeros_like(component)
        bright[selected & (window >= float(threshold))] = 255
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        opened: NDArray[Any] = np.asarray(
            cv2.morphologyEx(bright, cv2.MORPH_OPEN, kernel), dtype=np.uint8
        )
        count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
            opened, connectivity=8
        )
        parent_area = float(selected.sum())
        children: list[tuple[float, NDArray[np.uint8], tuple[int, int, int, int]]] = []
        # The profile's area contract applies to a child exactly as it does
        # to a whole component; a fragment below it is not a candidate.
        floor = max(
            self._config.min_area_fraction * frame_area,
            self._config.min_split_fraction * parent_area,
            float(getattr(self._profile, "min_area_px", 0)),
        )
        for label in range(1, count):
            area = float(stats[label, cv2.CC_STAT_AREA])
            if area < floor:
                continue
            cx = int(stats[label, cv2.CC_STAT_LEFT])
            cy = int(stats[label, cv2.CC_STAT_TOP])
            cw = int(stats[label, cv2.CC_STAT_WIDTH])
            ch = int(stats[label, cv2.CC_STAT_HEIGHT])
            child = np.zeros((ch, cw), dtype=np.uint8)
            child[labels[cy : cy + ch, cx : cx + cw] == label] = 255
            children.append((area, child, (x + cx, y + cy, cw, ch)))
        children.sort(key=lambda item: item[0], reverse=True)
        return [
            (child, bbox) for _area, child, bbox in children[: self._config.max_split_children]
        ]

    def _evaluate(
        self,
        label: int,
        component: NDArray[np.uint8],
        local_bbox: tuple[int, int, int, int],
        channels: dict[str, NDArray[Any]],
        rule: NDArray[np.bool_],
        luminance: NDArray[Any],
        frame_area: float,
        full_size: tuple[int, int],
        offset: tuple[int, int],
        source: str,
    ) -> ArrowHypothesis | None:
        """Measure one component. ``component`` is a window the size of its box."""
        import cv2

        x, y, w, h = local_bbox
        contours, _hierarchy = cv2.findContours(
            component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        if not contours:
            return None
        largest = max(contours, key=cv2.contourArea)
        # One basis: translate into full-frame coordinates *before* measuring,
        # so clipping is judged against the frame and every returned point is
        # canonical whichever pass produced it.
        shift = np.array([[[x + offset[0], y + offset[1]]]], dtype=largest.dtype)
        features = _contour_features(largest + shift, full_size)
        if features is None:
            return None

        # Local measurements index the pass's own arrays, in local coordinates.
        placed = np.zeros(luminance.shape[:2], dtype=np.uint8)
        placed[y : y + h, x : x + w] = component
        contour_local = np.asarray(features.contour_px, dtype=np.int64).reshape(
            -1, 2
        ) - np.array(offset)
        contrast, boundary = self._local_measurements(
            luminance, placed, local_bbox, features.diagonal_px, contour_local
        )
        chroma = self._chroma_fit(
            {
                key: value[y : y + h, x : x + w]
                for key, value in channels.items()
                if key != "luminance"
            },
            rule[y : y + h, x : x + w] & (component > 0),
        )
        decomposed = _decompose(features)
        barbs = decomposed.polarity_margin if decomposed is not None else 0.0
        terms, score = self._scorer.score(
            features,
            contrast=contrast,
            chroma=chroma,
            boundary=boundary,
            barbs=barbs,
            frame_area=frame_area,
        )
        accepted = score >= self._config.accept_threshold
        reason = None if accepted else f"{_weakest(terms)} below threshold"
        return ArrowHypothesis(
            label=label,
            features=features,
            terms=terms,
            score=score,
            accepted=accepted,
            reason=reason,
            source=source,
            state="viable" if accepted else "proposed",
            signature=min(2.0, contrast),
        )

    # -- fusion -----------------------------------------------------------
    def fuse(self, proposals: Sequence[ProposalSet]) -> tuple[ArrowHypothesis, ...]:
        """One hypothesis per object across every pass, best score wins."""
        merged = [h for proposal in proposals for h in proposal.hypotheses]
        merged.sort(key=lambda h: h.score, reverse=True)
        return tuple(_deduplicate(merged, self._config.duplicate_iou))

    # -- the temporal transaction ----------------------------------------
    def commit(
        self, frame: CapturedFrame, proposals: Sequence[ProposalSet]
    ) -> DetectionOutcome:
        """Advance temporal state exactly once for this frame.

        A second call for the same ``frame.sequence`` - a replayed recording
        with a repeated frame, a caller that ran two passes - advances nothing:
        the previous outcome is returned marked ``duplicate`` and counted.
        Aging the track for a screenshot it has already seen was the defect
        this boundary exists to prevent.
        """
        if self._last_sequence is not None and frame.sequence == self._last_sequence:
            self.duplicate_commits += 1
            previous = self._last_outcome
            if previous is not None:
                return replace(previous, decision="duplicate")
            return self._outcome(
                _abstain(self._profile, "duplicate-frame"), None, (), "duplicate"
            )
        self._last_sequence = frame.sequence
        self._now_s = frame.captured_at_s
        if any(p.stats.source == "full" for p in proposals):
            self._last_global_s = self._now_s
            self._global_due = False

        reasons = [p.abstain_reason for p in proposals if p.abstain_reason]
        fused = self.fuse(proposals)
        if not fused and reasons:
            self._register_miss()
            return self._outcome(_abstain(self._profile, reasons[0]), None, (), "none")

        viable = [h for h in fused if h.accepted]
        self._last_search_s = self._now_s
        self._last_viable = bool(viable)
        width, height = frame.canonical_size_px
        self._diagonal = math.hypot(width, height)

        if self._state in (TrackState.TRACK, TrackState.AMBIGUOUS):
            return self._commit_tracking(fused, viable, frame)
        if self._state is TrackState.REACQUIRE:
            return self._commit_reacquire(fused, viable, frame)
        return self._commit_acquire(fused, viable, frame)

    def _gate_px(self, since_s: float | None) -> float:
        """Association radius: grows with the time since the last hit."""
        config = self._config
        elapsed = 0.0 if since_s is None else max(0.0, self._now_s - since_s)
        return min(
            config.gate_fraction * self._diagonal,
            config.gate_base_px + config.gate_rate_px_s * elapsed,
        )

    def _scale_ratio_allowed(self, since_s: float | None) -> float:
        config = self._config
        elapsed = 0.0 if since_s is None else max(0.0, self._now_s - since_s)
        return config.scale_gate + config.scale_rate_s * elapsed

    def _streak_done(self, streak: _Streak, min_frames: int, min_s: float) -> bool:
        return streak.frames >= min_frames and self._now_s - streak.started_s >= min_s

    # -- acquisition ------------------------------------------------------
    def _commit_acquire(
        self,
        fused: tuple[ArrowHypothesis, ...],
        viable: list[ArrowHypothesis],
        frame: CapturedFrame,
    ) -> DetectionOutcome:
        if not viable:
            self._acquire = None
            reason = "no-candidate" if not fused else f"rejected:{fused[0].weakest_term}"
            return self._outcome(_abstain(self._profile, reason), None, fused, "none")
        gate = self._gate_px(self._acquire.started_s if self._acquire else None)
        best, ambiguous = self._pick(viable, None, gate)
        if ambiguous:
            self._acquire = None
            return self._outcome(
                _abstain(self._profile, "ambiguous-candidates"), None, fused, "ambiguous"
            )
        streak = self._advance_streak(self._acquire, best)
        self._acquire = streak
        config = self._config
        if not self._streak_done(streak, config.acquire_min_frames, config.acquire_min_s):
            reason = f"acquiring ({streak.frames}/{config.acquire_min_frames})"
            return self._outcome(_abstain(self._profile, reason), None, fused, "acquiring")
        self._acquire = None
        self._start_track(best)
        self._state = TrackState.TRACK
        return self._outcome(self._observe(best, viable, frame), best, fused, "acquire")

    def _advance_streak(self, streak: _Streak | None, candidate: ArrowHypothesis) -> _Streak:
        """Extend a consistent streak or start a new one at this candidate."""
        if streak is not None and self._consistent(streak, candidate):
            streak.frames += 1
            streak.centroid = candidate.features.centroid_px
            streak.scale = _scale_of(candidate)
            streak.last_s = self._now_s
            return streak
        return _Streak(
            candidate.features.centroid_px, _scale_of(candidate), self._now_s, self._now_s
        )

    # -- tracking ---------------------------------------------------------
    def _commit_tracking(
        self,
        fused: tuple[ArrowHypothesis, ...],
        viable: list[ArrowHypothesis],
        frame: CapturedFrame,
    ) -> DetectionOutcome:
        track = self._track
        assert track is not None
        predicted = self.predicted_centroid() or track.centroid
        gate = self._gate_px(track.last_hit_s)
        # Gate on the whole fused list, not on a top-K: the tracked arrow may
        # rank below a large distractor and must still be found here.
        in_gate: list[ArrowHypothesis] = []
        gated: dict[int, str] = {}
        for h in viable:
            why = self._gate_reason(track, h, predicted, gate)
            if why is None:
                in_gate.append(h)
            else:
                gated[id(h)] = why
        fused = tuple(replace(h, reason=gated[id(h)]) if id(h) in gated else h for h in fused)
        viable = [h for h in fused if h.accepted]
        in_gate = [h for h in viable if id(h) not in gated and h.reason is None]
        if not in_gate:
            # Nothing near where the arrow was. Before holding, ask whether it
            # is unmistakably somewhere else - see ``_resume_outside_gate``.
            resumed = self._resume_outside_gate(track, viable)
            if resumed is not None:
                return self._resume(track, resumed, fused, viable, frame)
            # No evidence for the held identity this frame. It is held - not
            # replaced by whatever else is viable - until ``max_track_age_s``
            # expires; a candidate elsewhere then has to earn a new identity
            # through REACQUIRE. Letting a hold turn into a takeover was
            # measured to lock onto sand and UI in the real corpus.
            self._register_miss()
            self._global_due = True
            if self._state is TrackState.REACQUIRE:
                return self._outcome(
                    _abstain(self._profile, "track-lost"), None, fused, "reacquire"
                )
            held_for = self._now_s - track.last_hit_s
            reason = f"held ({held_for:.2f}s/{self._config.max_track_age_s:.2f}s)"
            return self._outcome(_abstain(self._profile, reason), None, fused, "hold")

        held, ambiguous = self._pick(in_gate, predicted, gate)
        if ambiguous:
            self._state = TrackState.AMBIGUOUS
            self._register_miss()
            return self._outcome(
                _abstain(self._profile, "ambiguous-candidates"), None, fused, "ambiguous"
            )
        self._state = TrackState.TRACK

        # A global best outside the gate contests the identity. It must beat
        # the held candidate by the margin for several consecutive frames at
        # a consistent place before the track moves; one frame never switches.
        challenger = max(
            (h for h in viable if h is not held), key=lambda h: h.score, default=None
        )
        config = self._config
        if (
            challenger is not None
            and math.dist(challenger.features.centroid_px, predicted) > gate
            and challenger.score - held.score >= config.switch_margin
        ):
            streak = self._advance_streak(self._challenger, challenger)
            self._challenger = streak
            if self._streak_done(streak, config.switch_min_frames, config.switch_min_s):
                self.switches += 1
                self._challenger = None
                self._start_track(challenger)
                return self._outcome(
                    self._observe(challenger, viable, frame),
                    challenger,
                    self._label(fused, challenger, None),
                    "switch",
                )
            self._update_track(held)
            return self._outcome(
                self._observe(held, viable, frame),
                held,
                self._label(fused, held, challenger),
                "track",
            )
        self._challenger = None
        self._update_track(held)
        return self._outcome(
            self._observe(held, viable, frame), held, self._label(fused, held, None), "track"
        )

    # -- reacquisition ----------------------------------------------------
    def _commit_reacquire(
        self,
        fused: tuple[ArrowHypothesis, ...],
        viable: list[ArrowHypothesis],
        frame: CapturedFrame,
    ) -> DetectionOutcome:
        track = self._track
        assert track is not None
        lost_since = track.lost_since_s if track.lost_since_s is not None else track.last_hit_s
        if self._now_s - lost_since > self._config.lost_after_s:
            self._track = None
            self._state = TrackState.LOST
            self._acquire = None
            return self._commit_acquire(fused, viable, frame)
        if not viable:
            return self._outcome(
                _abstain(self._profile, "track-lost"), None, fused, "reacquire"
            )
        # Near where it was, at a consistent scale and appearance: the same
        # identity resumes at once. That is what brief occlusion looks like.
        gate = self._gate_px(track.last_hit_s)
        near = [
            h
            for h in viable
            if math.dist(h.features.centroid_px, track.centroid) <= gate
            and self._scale_ok(track, h)
            and self._appearance_ok(track, h)
        ]
        if near:
            best, ambiguous = self._pick(near, track.centroid, gate)
            if ambiguous:
                return self._outcome(
                    _abstain(self._profile, "ambiguous-candidates"), None, fused, "ambiguous"
                )
            track.velocity = (0.0, 0.0)
            track.lost_since_s = None
            self._update_track(best)
            self._state = TrackState.TRACK
            self.reacquisitions += 1
            return self._outcome(
                self._observe(best, viable, frame),
                best,
                self._label(fused, best, None),
                "resume",
            )
        # Elsewhere, but unmistakable: resume at once rather than spending
        # ``acquire_min_frames`` re-earning an identity we already hold.
        resumed = self._resume_outside_gate(track, viable)
        if resumed is not None:
            self.reacquisitions += 1
            return self._resume(track, resumed, fused, viable, frame)
        # Elsewhere and not unmistakable: a new identity has to be earned,
        # exactly like acquisition.
        acquire_gate = self._gate_px(self._acquire.started_s if self._acquire else None)
        best, ambiguous = self._pick(viable, None, acquire_gate)
        if ambiguous:
            self._acquire = None
            return self._outcome(
                _abstain(self._profile, "ambiguous-candidates"), None, fused, "ambiguous"
            )
        streak = self._advance_streak(self._acquire, best)
        self._acquire = streak
        config = self._config
        if not self._streak_done(streak, config.acquire_min_frames, config.acquire_min_s):
            reason = f"reacquiring ({streak.frames}/{config.acquire_min_frames})"
            return self._outcome(_abstain(self._profile, reason), None, fused, "reacquire")
        self._acquire = None
        self.reacquisitions += 1
        self._start_track(best)
        self._state = TrackState.TRACK
        return self._outcome(
            self._observe(best, viable, frame), best, self._label(fused, best, None), "acquire"
        )

    # -- helpers ----------------------------------------------------------
    def _pick(
        self,
        candidates: list[ArrowHypothesis],
        anchor: tuple[float, float] | None,
        gate: float,
    ) -> tuple[ArrowHypothesis, bool]:
        """Best candidate, and whether a rival makes the choice a coin flip.

        Two candidates within the ambiguity margin are a tie unless the tie
        can be broken by temporal evidence: the one nearer the prediction
        wins if the other is clearly further away.
        """
        ordered = sorted(candidates, key=lambda h: h.score, reverse=True)
        best = ordered[0]
        if len(ordered) == 1:
            return (best, False)
        rival = ordered[1]
        if best.score - rival.score >= self._config.ambiguity_margin:
            return (best, False)
        if anchor is None:
            return (best, True)
        best_distance = math.dist(best.features.centroid_px, anchor)
        rival_distance = math.dist(rival.features.centroid_px, anchor)
        if rival_distance > best_distance * 1.5 + 0.1 * gate:
            return (best, False)
        if best_distance > rival_distance * 1.5 + 0.1 * gate:
            return (rival, False)
        return (best, True)

    def _consistent(self, streak: _Streak, candidate: ArrowHypothesis) -> bool:
        if math.dist(streak.centroid, candidate.features.centroid_px) > self._gate_px(
            streak.last_s
        ):
            return False
        scale = _scale_of(candidate)
        ratio = max(scale, streak.scale) / max(1e-6, min(scale, streak.scale))
        return ratio <= self._scale_ratio_allowed(streak.last_s)

    def _resume_outside_gate(
        self, track: _Track, viable: list[ArrowHypothesis]
    ) -> ArrowHypothesis | None:
        """The same arrow, somewhere else: identity without proximity.

        The positional gate exists to stop a same-coloured distractor stealing
        the track, and it does that job. What it cannot do is tell "the arrow
        moved 250 px because the camera swung" from "that is a different
        object", because both look identical to a distance test - so the old
        answer was to wait until the gate had grown far enough to cover the
        distance, which is 367 ms of deliberate blindness with the arrow in
        plain sight the whole time.

        This asks the question the other way round. Three identity cues do not
        depend on position at all: orientation continuity, the appearance
        signature, and scale. When all three agree *and* the candidate is the
        unambiguous global best, it is the arrow - wherever it is. When two
        candidates are close, nothing resumes, because two similar candidates
        is precisely the same-coloured-foliage case this must never fire on.

        Returns the candidate to resume on, or ``None`` to hold as before.
        """
        config = self._config
        if not viable:
            return None
        if self._now_s - track.last_hit_s > config.resume_max_age_s:
            return None
        ordered = sorted(viable, key=lambda h: h.score, reverse=True)
        best = ordered[0]
        if len(ordered) > 1 and best.score - ordered[1].score < config.resume_margin:
            return None
        if best.score < track.score * config.resume_min_score_fraction:
            return None
        if not self._orientation_ok(track, best):
            return None
        if not self._appearance_ok(track, best):
            return None
        if track.scale > 0.0:
            scale = _scale_of(best)
            ratio = max(scale, track.scale) / max(1e-6, min(scale, track.scale))
            if ratio > config.resume_scale_gate:
                return None
        return best

    def _resume(
        self,
        track: _Track,
        chosen: ArrowHypothesis,
        fused: tuple[ArrowHypothesis, ...],
        viable: list[ArrowHypothesis],
        frame: CapturedFrame,
    ) -> DetectionOutcome:
        """Adopt a resumed candidate. The identity is kept; the motion model is not.

        Velocity is zeroed rather than recomputed. A resume means the position
        model just failed, and carrying its failed extrapolation forward would
        throw the next prediction as far past the arrow as it fell short of it.
        """
        self._state = TrackState.TRACK
        self._challenger = None
        self._acquire = None
        track.lost_since_s = None
        track.velocity = (0.0, 0.0)
        self.resumes += 1
        self._update_track(chosen)
        return self._outcome(
            self._observe(chosen, viable, frame),
            chosen,
            self._label(fused, chosen, None),
            "resume",
        )

    def _gate_reason(
        self,
        track: _Track,
        hypothesis: ArrowHypothesis,
        predicted: tuple[float, float],
        gate: float,
    ) -> str | None:
        """Why a viable candidate is not associated with the track, or ``None``."""
        distance = math.dist(hypothesis.features.centroid_px, predicted)
        if distance > gate:
            return f"gate: {distance:.0f} px from the track, limit {gate:.0f}"
        if not self._scale_ok(track, hypothesis):
            return f"gate: scale {_scale_of(hypothesis):.0f} vs tracked {track.scale:.0f}"
        if not self._orientation_ok(track, hypothesis):
            return "gate: axis disagrees with the track"
        if not self._appearance_ok(track, hypothesis):
            return f"gate: contrast {hypothesis.signature:.2f} vs tracked {track.signature:.2f}"
        return None

    def _scale_ok(self, track: _Track, hypothesis: ArrowHypothesis) -> bool:
        if track.scale <= 0.0:
            return True
        scale = _scale_of(hypothesis)
        ratio = max(scale, track.scale) / max(1e-6, min(scale, track.scale))
        return ratio <= self._scale_ratio_allowed(track.last_hit_s)

    def _orientation_ok(self, track: _Track, hypothesis: ArrowHypothesis) -> bool:
        """Unsigned axis agreement, only when both axes are well conditioned."""
        features = hypothesis.features
        if (
            track.heading is None
            or features.axis_unit_xy is None
            or features.anisotropy < self._config.min_anisotropy
        ):
            return True
        heading = heading_deg(features.axis_unit_xy)
        if heading is None:
            return True
        delta = abs(wrap_deg(heading - track.heading))
        delta = min(delta, 180.0 - delta)
        return delta <= self._config.orientation_gate_deg

    def _appearance_ok(self, track: _Track, hypothesis: ArrowHypothesis) -> bool:
        """Contrast signature within tolerance, saturating above the band.

        Saturated, because the ratio depends on what is *behind* the arrow: the
        same arrow reads 1.3 over pink ground and 2.2 over water, and neither
        is evidence that the object changed. What the gate has to catch is a
        drop toward 1.0 - a candidate no brighter than its surround.
        """
        ceiling = self._config.contrast_full + 0.3
        mine = min(ceiling, hypothesis.signature)
        theirs = min(ceiling, track.signature)
        return abs(mine - theirs) <= self._config.appearance_gate

    def _start_track(self, chosen: ArrowHypothesis) -> None:
        self._next_track_id += 1
        features = chosen.features
        heading = (
            heading_deg(features.axis_unit_xy)
            if features.axis_unit_xy is not None
            and features.anisotropy >= self._config.min_anisotropy
            else None
        )
        self._track = _Track(
            track_id=self._next_track_id,
            centroid=features.centroid_px,
            velocity=(0.0, 0.0),
            scale=_scale_of(chosen),
            heading=heading,
            signature=chosen.signature,
            score=chosen.score,
            last_hit_s=self._now_s,
        )
        self._challenger = None

    def _update_track(self, chosen: ArrowHypothesis) -> None:
        track = self._track
        assert track is not None
        features = chosen.features
        centroid = features.centroid_px
        elapsed = max(1e-3, self._now_s - track.last_hit_s)
        # Velocity in pixels per second, damped: one noisy step must not
        # throw the next prediction across the frame.
        raw = (
            (centroid[0] - track.centroid[0]) / elapsed,
            (centroid[1] - track.centroid[1]) / elapsed,
        )
        track.velocity = (
            0.5 * track.velocity[0] + 0.5 * raw[0],
            0.5 * track.velocity[1] + 0.5 * raw[1],
        )
        track.centroid = centroid
        track.scale = _scale_of(chosen)
        if (
            features.axis_unit_xy is not None
            and features.anisotropy >= self._config.min_anisotropy
        ):
            track.heading = heading_deg(features.axis_unit_xy)
        # Slow adaptation, only on hits, only toward what was just accepted:
        # a signature that followed every candidate would follow terrain.
        alpha = self._config.appearance_alpha
        track.signature = track.signature * (1.0 - alpha) + chosen.signature * alpha
        track.score = track.score * 0.7 + chosen.score * 0.3
        track.hits += 1
        track.misses = 0
        track.last_hit_s = self._now_s
        track.lost_since_s = None

    def _register_miss(self) -> None:
        track = self._track
        if track is None:
            return
        track.misses += 1
        if self._now_s - track.last_hit_s > self._config.max_track_age_s and self._state in (
            TrackState.TRACK,
            TrackState.AMBIGUOUS,
        ):
            self._state = TrackState.REACQUIRE
            track.lost_since_s = self._now_s
            self._acquire = None
            self._challenger = None

    def _observe(
        self, chosen: ArrowHypothesis, viable: list[ArrowHypothesis], frame: CapturedFrame
    ) -> ArrowObservation:
        others = [h.score for h in viable if h is not chosen]
        margin = chosen.score - (max(others) if others else 0.0)
        track = self._track
        maturity = 1.0 if track is None else min(1.0, 0.6 + 0.1 * track.hits)
        confidence = min(1.0, chosen.score * (0.75 + 0.25 * min(1.0, margin / 0.3)) * maturity)
        features = chosen.features
        _width, height = frame.canonical_size_px
        return ArrowObservation(
            profile_id=getattr(self._profile, "profile_id", None),
            track_id=self.track_id,
            bbox_px=features.bbox_px,
            centroid_px=features.centroid_px,
            tip_px=None,
            axis_unit_xy=features.axis_unit_xy,
            confidence=round(confidence, 4),
            valid=True,
            abstain_reason=None,
            tail_px=None,
            score_terms=chosen.terms,
            score_margin=round(margin, 4),
            notch_mid_px=features.notch_mid_px,
            notch_px=(
                (features.notches[0][0], features.notches[1][0])
                if len(features.notches) > 1
                else None
            ),
            scale_norm=round(math.sqrt(features.area_px) / max(1, height), 5),
            track_age=self.track_age,
        )

    @staticmethod
    def _label(
        fused: tuple[ArrowHypothesis, ...],
        selected: ArrowHypothesis | None,
        challenger: ArrowHypothesis | None,
    ) -> tuple[ArrowHypothesis, ...]:
        labelled: list[ArrowHypothesis] = []
        for hypothesis in fused:
            if hypothesis is selected:
                labelled.append(replace(hypothesis, state="selected", reason=None))
            elif hypothesis is challenger:
                labelled.append(
                    replace(
                        hypothesis, state="challenger", reason="challenging the held identity"
                    )
                )
            elif hypothesis.accepted and hypothesis.reason is None:
                labelled.append(replace(hypothesis, reason="viable, not selected"))
            else:
                labelled.append(hypothesis)
        return tuple(labelled)

    def _outcome(
        self,
        observation: ArrowObservation,
        selected: ArrowHypothesis | None,
        hypotheses: tuple[ArrowHypothesis, ...],
        decision: str,
    ) -> DetectionOutcome:
        self._last_decision = decision
        if selected is not None and not any(h.state == "selected" for h in hypotheses):
            hypotheses = self._label(hypotheses, selected, None)
        outcome = DetectionOutcome(observation, selected, hypotheses, decision, self._state)
        self._last_outcome = outcome
        return outcome

    # -- compatibility ----------------------------------------------------
    def analyze(
        self, frame: CapturedFrame, *, roi_px: tuple[int, int, int, int] | None = None
    ) -> tuple[ArrowObservation, tuple[ArrowHypothesis, ...]]:
        """One pass and one commit. Kept for callers that want the old shape.

        Returns the observation and the fused hypotheses **bounded to the
        presentation top-K plus the selected one**, so the selected candidate
        is never truncated away.
        """
        outcome = self.commit(frame, [self.propose(frame, roi_px=roi_px)])
        return (outcome.observation, present(outcome, self._config.top_k))


def present(outcome: DetectionOutcome, top_k: int) -> tuple[ArrowHypothesis, ...]:
    """The top-K hypotheses for display, always including the selected one."""
    shown = list(outcome.hypotheses[: max(1, top_k)])
    if outcome.selected is not None and not any(h.state == "selected" for h in shown):
        selected = next((h for h in outcome.hypotheses if h.state == "selected"), None)
        if selected is not None:
            shown.append(selected)
    return tuple(shown)


def _scale_of(hypothesis: ArrowHypothesis) -> float:
    return math.sqrt(max(1.0, hypothesis.features.area_px))


def _weakest(terms: tuple[tuple[str, float], ...]) -> str:
    return min(terms, key=lambda item: item[1])[0] if terms else "score"


def _overlap_ratio(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Intersection over the *smaller* box, so a fragment nests in its whole."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    left, top = max(ax, bx), max(ay, by)
    right, bottom = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if right <= left or bottom <= top:
        return 0.0
    overlap = float((right - left) * (bottom - top))
    return overlap / float(max(1, min(aw * ah, bw * bh)))


def _deduplicate(hypotheses: list[ArrowHypothesis], threshold: float) -> list[ArrowHypothesis]:
    """Keep the best-scoring hypothesis per object."""
    kept: list[ArrowHypothesis] = []
    for candidate in hypotheses:
        if any(
            _overlap_ratio(candidate.features.bbox_px, existing.features.bbox_px) >= threshold
            for existing in kept
        ):
            continue
        kept.append(candidate)
    return kept


def _abstain(profile: Any, reason: str) -> ArrowObservation:
    return ArrowObservation(
        profile_id=getattr(profile, "profile_id", None),
        track_id=None,
        bbox_px=None,
        centroid_px=None,
        tip_px=None,
        axis_unit_xy=None,
        confidence=0.0,
        valid=False,
        abstain_reason=reason,
    )


# ---------------------------------------------------------------------------
# Direction
# ---------------------------------------------------------------------------


def _densify(points: tuple[tuple[float, float], ...], samples: int = 240) -> NDArray[Any]:
    """Resample a closed polygon evenly, so a sparse hull still profiles well."""
    if len(points) < 3:
        return np.asarray(points, dtype=np.float64).reshape(-1, 2)
    loop = np.asarray([*points, points[0]], dtype=np.float64)
    segments = np.diff(loop, axis=0)
    lengths = np.hypot(segments[:, 0], segments[:, 1])
    total = float(lengths.sum())
    if total <= 1e-9:
        return loop[:-1]
    cumulative = np.concatenate([[0.0], np.cumsum(lengths)])
    wanted = np.linspace(0.0, total, samples, endpoint=False)
    index = np.clip(np.searchsorted(cumulative, wanted, side="right") - 1, 0, len(segments) - 1)
    remainder = (wanted - cumulative[index]) / np.maximum(1e-9, lengths[index])
    resampled: NDArray[Any] = loop[index] + segments[index] * remainder[:, None]
    return resampled


@dataclass(frozen=True)
class _HeadTail:
    """The arrow decomposed about its notch line."""

    tail_centre: tuple[float, float]
    head_centre: tuple[float, float]
    tip: tuple[float, float]
    head_far_width: float
    tail_far_width: float
    head_reach: float
    tail_reach: float
    head_taper: float
    tail_taper: float
    #: Lateral width of the outline just beyond the notch line on each side.
    head_near_width: float
    tail_near_width: float

    @property
    def polarity_margin(self) -> float:
        """Confidence that the head is the head, in [0, 1].

        From the **barbs**: immediately beyond the notch line the head is
        wider than the shaft, because the notches *are* the shaft's corners
        and the arrowhead's back edge sticks out past them. That holds under
        any affine view and under a hidden shaft end, which is why it beats
        taper: from a low camera angle the shaft's foreshortened end can be
        the sharpest, most tapered corner of the whole silhouette.
        """
        widest = max(self.head_near_width, self.tail_near_width)
        if widest <= 1e-6:
            return 0.0
        return max(0.0, min(1.0, (self.head_near_width - self.tail_near_width) / widest))


def _decompose(features: ShapeFeatures) -> _HeadTail | None:
    """Split the outline at the notch line and profile both sides."""
    mid = features.notch_mid_px
    if mid is None or len(features.notches) < 2:
        return None
    (x1, y1), _d1 = features.notches[0]
    (x2, y2), _d2 = features.notches[1]
    tangent_length = math.hypot(x2 - x1, y2 - y1)
    if tangent_length < 1e-6:
        return None
    tangent = ((x2 - x1) / tangent_length, (y2 - y1) / tangent_length)
    normal = (-tangent[1], tangent[0])

    outline = _densify(features.hull_px)
    if outline.shape[0] < 4:
        return None
    relative = outline - np.asarray(mid)
    reach = relative[:, 0] * normal[0] + relative[:, 1] * normal[1]
    lateral = relative[:, 0] * tangent[0] + relative[:, 1] * tangent[1]
    positive, negative = reach >= 0.0, reach < 0.0
    if not positive.any() or not negative.any():
        return None

    def _profile(selected: NDArray[Any]) -> tuple[float, float, float, float]:
        side_reach = np.abs(reach[selected])
        side_lateral = lateral[selected]
        extreme = float(side_reach.max())
        if extreme <= 1e-6:
            return (0.0, 0.0, 0.0, 0.0)

        def _width(low: float, high: float) -> float:
            band = side_lateral[(side_reach >= low * extreme) & (side_reach <= high * extreme)]
            return float(band.max() - band.min()) if band.size else 0.0

        near_width = _width(0.05, 0.35)
        far_width = _width(0.72, 1.0)
        widest = max(near_width, far_width)
        taper = (near_width - far_width) / widest if widest > 1e-6 else 0.0
        return (extreme, far_width, max(0.0, taper), near_width)

    pos_reach, pos_width, pos_taper, pos_near = _profile(positive)
    neg_reach, neg_width, neg_taper, neg_near = _profile(negative)
    # The head is the wider side just beyond the notch line (the barbs).
    head_is_positive = pos_near >= neg_near
    head_mask = positive if head_is_positive else negative
    tail_mask = negative if head_is_positive else positive
    head_reach, head_width, head_taper, head_near = (
        (pos_reach, pos_width, pos_taper, pos_near)
        if head_is_positive
        else (neg_reach, neg_width, neg_taper, neg_near)
    )
    tail_reach, tail_width, tail_taper, tail_near = (
        (neg_reach, neg_width, neg_taper, neg_near)
        if head_is_positive
        else (pos_reach, pos_width, pos_taper, pos_near)
    )
    head_points = outline[head_mask]
    tail_points = outline[tail_mask]
    signed = reach[head_mask] if head_is_positive else -reach[head_mask]
    tip_index = int(np.argmax(signed))
    return _HeadTail(
        tail_centre=(float(tail_points[:, 0].mean()), float(tail_points[:, 1].mean())),
        head_centre=(float(head_points[:, 0].mean()), float(head_points[:, 1].mean())),
        tip=(float(head_points[tip_index][0]), float(head_points[tip_index][1])),
        head_far_width=head_width,
        tail_far_width=tail_width,
        head_reach=head_reach,
        tail_reach=tail_reach,
        head_taper=head_taper,
        tail_taper=tail_taper,
        head_near_width=head_near,
        tail_near_width=tail_near,
    )


def _axis_end_widths(features: ShapeFeatures) -> tuple[float, float, float] | None:
    """Outline width in the far fifth of each end of the principal axis.

    Returns ``(width_at_positive_end, width_at_negative_end, axis_heading)``
    where the axis heading points to the positive end. The arrow narrows to a
    point at the tip and ends bluntly at the tail, so the narrower end is the
    tip. Independent of the notch line entirely.
    """
    axis = features.axis_unit_xy
    if axis is None:
        return None
    outline = _densify(features.hull_px)
    if outline.shape[0] < 6:
        return None
    relative = outline - np.asarray(features.centroid_px)
    along = relative[:, 0] * axis[0] + relative[:, 1] * axis[1]
    across = -relative[:, 0] * axis[1] + relative[:, 1] * axis[0]
    top, bottom = float(along.max()), float(along.min())
    if top - bottom < 1e-6:
        return None

    def _width(selected: NDArray[Any]) -> float:
        band = across[selected]
        return float(band.max() - band.min()) if band.size else 0.0

    span = top - bottom
    positive = _width(along >= top - 0.2 * span)
    negative = _width(along <= bottom + 0.2 * span)
    heading = heading_deg(axis)
    if heading is None:
        return None
    return (positive, negative, heading)


@dataclass(frozen=True)
class DirectionResult:
    """A signed direction plus every cue that voted on it."""

    observation: DirectionObservation
    tip_px: tuple[float, float] | None
    tail_px: tuple[float, float] | None
    readings: tuple[CueReading, ...]
    #: Whether the sign was held against the remembered polarity.
    reversal_refused: bool = False


class DirectionEstimator:
    """Signed arrow direction from several independent polarity cues.

    The unsigned axis is well measured; the *sign* is what fails on real
    frames, so it is decided by a weighted vote of independent evidence:

    * taper about the notch line (the head narrows, the shaft does not);
    * the sharpest hull vertex (the tip is the sharpest corner);
    * the widths at the two ends of the principal axis (the tip end is
      narrower);

    The barb asymmetry is the primary vote because it survives perspective
    and a hidden shaft end; the others break ties.

    A vote below ``min_sign_margin`` abstains. A vote that would reverse the
    remembered polarity of a held track needs ``reversal_margin``; below it
    the previous polarity is kept and the refusal is reported, so a one-frame
    flip on a nicked outline cannot turn the player around.
    """

    def __init__(self, config: DetectorConfig | None = None) -> None:
        self._config = config or DetectorConfig()
        #: Consecutive frames whose evidence wanted to reverse the remembered
        #: polarity without being strong enough to do it on its own.
        self._reversal_frames = 0
        #: Frame time the current reversal was first seen at, so the latch can
        #: be a duration as well as a count. ``None`` when nothing is pending,
        #: and ``None`` for a caller that supplies no clock.
        self._reversal_started_s: float | None = None

    def reset(self) -> None:
        """Forget the reversal latch. Called when the target identity changes."""
        self._reversal_frames = 0
        self._reversal_started_s = None

    def estimate(
        self,
        features: ShapeFeatures | None,
        *,
        anchor_px: tuple[float, float] | None,
        forward_deg: float | None,
        arrow_confidence: float,
        previous_heading_deg: float | None = None,
        now_s: float | None = None,
    ) -> DirectionResult:
        config = self._config
        if features is None:
            return _direction_abstain("no-arrow")
        if forward_deg is None:
            return _direction_abstain("no-forward-reference")

        # -- the unsigned axis -------------------------------------------
        axis_candidates: list[tuple[str, float, float]] = []
        details: dict[str, str] = {}
        decomposed = _decompose(features)
        tip: tuple[float, float] | None = None
        tail: tuple[float, float] | None = None
        if decomposed is not None:
            # One cue for the notch line, however it is read: the tail-to-head
            # split and the notch-to-tip ray share the same two notches and
            # are not independent evidence. Counting them twice let a wrong
            # notch pair outvote a well-conditioned principal axis.
            mid = features.notch_mid_px
            bearing = None
            if mid is not None:
                separation = max(1.0, features.notch_separation_px)
                head_ratio = math.dist(decomposed.tip, mid) / separation
                if head_ratio >= config.min_head_ratio:
                    bearing = heading_deg(
                        (decomposed.tip[0] - mid[0], decomposed.tip[1] - mid[1])
                    )
                    details["notch_axis"] = f"notch to tip, head ratio {head_ratio:.2f}"
            if bearing is None:
                bearing = heading_deg(
                    (
                        decomposed.head_centre[0] - decomposed.tail_centre[0],
                        decomposed.head_centre[1] - decomposed.tail_centre[1],
                    )
                )
                details["notch_axis"] = "tail-to-head split"
            if bearing is not None:
                # A notch pair without barbs is probably a nick, not the
                # head/shaft junction, and its line is then no axis at all.
                depth_1, _depth_2 = features.notch_depths_norm
                quality = _band(depth_1, *config.notch_depth_band) * decomposed.polarity_margin
                if quality > 0.05:
                    axis_candidates.append(("notch_axis", bearing, 0.4 + 0.9 * quality))
                else:
                    details["notch_axis"] = "refused: no barbs beyond the notch line"
        if features.axis_unit_xy is not None and features.anisotropy >= config.min_anisotropy:
            bearing = heading_deg(features.axis_unit_xy)
            if bearing is not None:
                # The better conditioned the axis, the more it is trusted; a
                # long sliver has almost nothing else to say about its axis.
                conditioning = min(1.0, (features.anisotropy - config.min_anisotropy) / 1.5)
                axis_candidates.append(("pca_axis", bearing, 0.5 + 0.7 * conditioning))
                details["pca_axis"] = f"anisotropy {features.anisotropy:.2f}"
        else:
            details["pca_axis"] = (
                f"refused: anisotropy {features.anisotropy:.2f} below "
                f"{config.min_anisotropy:.2f}"
            )
        if features.sharpest_px is not None and features.sharpest_angle_deg <= 95.0:
            bearing = heading_deg(
                (
                    features.sharpest_px[0] - features.centroid_px[0],
                    features.sharpest_px[1] - features.centroid_px[1],
                )
            )
            if bearing is not None:
                axis_candidates.append(("centroid_to_sharpest", bearing, 0.7))
                details["centroid_to_sharpest"] = (
                    f"vertex {features.sharpest_angle_deg:.0f} deg"
                )

        if not axis_candidates:
            return _direction_abstain("no-axis-cue")

        # Every axis candidate is folded onto the first one's half-circle, so
        # consensus measures the *axis*, and the sign is decided separately.
        reference = axis_candidates[0][1]
        folded: list[tuple[str, float, float]] = []
        for cue, bearing, weight in axis_candidates:
            flipped = wrap_deg(bearing + 180.0)
            aligned = (
                flipped
                if abs(wrap_deg(flipped - reference)) < abs(wrap_deg(bearing - reference))
                else bearing
            )
            folded.append((cue, aligned, weight * arrow_confidence))
        axis_heading, spread, weights = circular_consensus(
            folded, outlier_deg=config.cue_outlier_deg
        )
        if axis_heading is None or spread > config.max_cue_spread_deg:
            readings = _cue_readings(folded, weights, details)
            return _direction_abstain("cues disagree", readings=readings, spread=spread)

        # -- the sign ----------------------------------------------------
        votes: list[tuple[str, float, float]] = []  # (cue, +1/-1 toward axis_heading, weight)

        def _vote(cue: str, bearing: float | None, strength: float) -> None:
            if bearing is None or strength <= 0.0:
                return
            sign = 1.0 if abs(wrap_deg(bearing - axis_heading)) <= 90.0 else -1.0
            votes.append((cue, sign, strength))

        if decomposed is not None:
            head_bearing = heading_deg(
                (
                    decomposed.head_centre[0] - decomposed.tail_centre[0],
                    decomposed.head_centre[1] - decomposed.tail_centre[1],
                )
            )
            _vote("barbs", head_bearing, 1.0 * decomposed.polarity_margin)
            details["barbs"] = (
                f"near widths head {decomposed.head_near_width:.0f} vs tail "
                f"{decomposed.tail_near_width:.0f} px"
            )
            taper = max(0.0, min(1.0, decomposed.head_taper - decomposed.tail_taper))
            _vote("taper", head_bearing, 0.3 * taper)
        if features.sharpest_px is not None:
            sharpness = max(0.0, min(1.0, (95.0 - features.sharpest_angle_deg) / 45.0))
            bearing = heading_deg(
                (
                    features.sharpest_px[0] - features.centroid_px[0],
                    features.sharpest_px[1] - features.centroid_px[1],
                )
            )
            _vote("sharpest_vertex", bearing, 0.6 * sharpness)
        ends = _axis_end_widths(features)
        if ends is not None:
            positive_width, negative_width, axis_bearing = ends
            widest = max(positive_width, negative_width)
            if widest > 1e-6:
                asymmetry = abs(positive_width - negative_width) / widest
                toward = (
                    axis_bearing
                    if positive_width < negative_width
                    else wrap_deg(axis_bearing + 180.0)
                )
                _vote("end_widths", toward, 0.5 * min(1.0, asymmetry * 1.5))
                details["end_widths"] = f"{positive_width:.0f} vs {negative_width:.0f} px"

        total = sum(weight for _cue, _sign, weight in votes)
        signed = sum(sign * weight for _cue, sign, weight in votes)
        margin = abs(signed) / total if total > 1e-9 else 0.0
        if total < config.min_sign_evidence:
            # Agreement among weak votes is not evidence. A box-like blob with
            # no taper, no sharp corner and symmetric ends must abstain.
            margin = 0.0
        heading = axis_heading if signed >= 0.0 else wrap_deg(axis_heading + 180.0)
        reversal_refused = False
        reversing = (
            previous_heading_deg is not None
            and abs(wrap_deg(heading - previous_heading_deg)) > 135.0
        )
        if not reversing:
            self._reversal_frames = 0
            self._reversal_started_s = None
        elif margin >= config.reversal_margin:
            # Strong evidence needs no waiting.
            self._reversal_frames = 0
            self._reversal_started_s = None
        else:
            self._reversal_frames += 1
            if self._reversal_started_s is None:
                self._reversal_started_s = now_s
            # Quantity *and* duration. Frames alone made this latch mean 50 ms
            # at 60 Hz, where a two-frame flicker in the principal axis clears
            # it and a spurious reversal is adopted. ``now_s`` is optional so a
            # caller with no clock keeps exactly the old frame-counted rule.
            held_long_enough = (
                now_s is None
                or self._reversal_started_s is None
                or (now_s - self._reversal_started_s) >= config.reversal_latch_s
            )
            if self._reversal_frames < config.reversal_latch_frames or not held_long_enough:
                # Weak evidence for a reversal, not yet sustained. Say so, and
                # say nothing else.
                #
                # This used to *invert* the heading and return it as valid -
                # the opposite of what every cue had just said, at the moment
                # the cues were least trustworthy. Walking past the target
                # reverses the arrow for real, so the guard fired exactly when
                # it was wrong, and because the caller remembers the heading it
                # was handed, the next frame compared against the inverted one
                # and refused again. The navigator was locked out of the rear
                # half of the compass for the rest of the run.
                reversal_refused = True
                details["polarity"] = (
                    f"reversal seen {self._reversal_frames} of "
                    f"{config.reversal_latch_frames} times at margin {margin:.2f}"
                    + (
                        ""
                        if held_long_enough
                        else f", held {(now_s or 0.0) - (self._reversal_started_s or 0.0):.2f}"
                        f" of {config.reversal_latch_s:.2f} s"
                    )
                )
                readings = _cue_readings(folded, weights, details)
                return _direction_abstain(
                    "reversal-pending",
                    readings=readings,
                    spread=spread,
                    margin=90.0 * margin,
                    reversal_refused=True,
                )
            details["polarity"] = f"reversal sustained over {self._reversal_frames} frames"
            self._reversal_frames = 0
            self._reversal_started_s = None
        readings = _cue_readings(folded, weights, details)
        readings += tuple(
            CueReading(
                cue_id=f"sign:{cue}",
                heading_deg=round(heading if sign > 0 else wrap_deg(heading + 180.0), 2),
                confidence=round(weight, 4),
                weight=round(weight, 4),
                valid=True,
                note="polarity vote",
            )
            for cue, sign, weight in votes
        )
        if margin < config.min_sign_margin:
            # No ``and not reversal_refused`` here any more. That clause let the
            # weakest evidence in the system through *because* it had just
            # failed a stricter test, which is exactly backwards.
            return _direction_abstain(
                "polarity", readings=readings, spread=spread, margin=90.0 * margin
            )

        if anchor_px is not None:
            bearing = heading_deg(
                (features.centroid_px[0] - anchor_px[0], features.centroid_px[1] - anchor_px[1])
            )
            if bearing is not None:
                readings += (
                    CueReading(
                        "player_to_arrow",
                        round(bearing, 2),
                        0.25,
                        0.0,
                        valid=False,
                        note="position, not pose",
                    ),
                )

        # The tip and tail the overlay draws are the sharpest vertex on the
        # head side and the opposite end of the axis, whichever cue won.
        tip, tail = _shaft_endpoints(features, heading, decomposed)
        agreement = 1.0 - spread / max(1e-6, config.max_cue_spread_deg)
        confidence = arrow_confidence * max(0.0, agreement) * (0.6 + 0.4 * margin)
        return DirectionResult(
            observation=DirectionObservation(
                error_deg=wrap_deg(heading - forward_deg),
                confidence=round(min(1.0, confidence), 4),
                cue_id="topology_consensus",
                cue_disagreement_deg=round(spread, 2),
                valid=True,
                abstain_reason=None,
                sign_confidence=round(min(1.0, margin), 4),
                sign_margin_deg=round(90.0 * margin, 2),
                cues=readings,
                anisotropy=round(features.anisotropy, 3),
            ),
            tip_px=tip,
            tail_px=tail,
            readings=readings,
            reversal_refused=reversal_refused,
        )


def _shaft_endpoints(
    features: ShapeFeatures, heading: float, decomposed: _HeadTail | None
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Tip and tail along the signed heading, from the hull's extremes."""
    radians = math.radians(heading)
    direction = (math.sin(radians), -math.cos(radians))
    outline = _densify(features.hull_px)
    if outline.shape[0] == 0:
        centre = features.centroid_px
        return (centre, centre)
    relative = outline - np.asarray(features.centroid_px)
    along = relative[:, 0] * direction[0] + relative[:, 1] * direction[1]
    tip_point = outline[int(np.argmax(along))]
    tail_point = outline[int(np.argmin(along))]
    if decomposed is not None:
        tip_bearing = heading_deg(
            (
                decomposed.tip[0] - features.centroid_px[0],
                decomposed.tip[1] - features.centroid_px[1],
            )
        )
        if tip_bearing is not None and abs(wrap_deg(tip_bearing - heading)) <= 45.0:
            tip_point = np.asarray(decomposed.tip)
    return (
        (float(tip_point[0]), float(tip_point[1])),
        (float(tail_point[0]), float(tail_point[1])),
    )


def _cue_readings(
    folded: list[tuple[str, float, float]], weights: dict[str, float], details: dict[str, str]
) -> tuple[CueReading, ...]:
    readings = tuple(
        CueReading(
            cue_id=cue,
            heading_deg=round(heading, 2),
            confidence=round(confidence, 4),
            weight=round(weights.get(cue, 0.0), 4),
            valid=weights.get(cue, 0.0) > 0.0,
            note=details.get(cue, ""),
        )
        for cue, heading, confidence in folded
    )
    for cue, note in details.items():
        if not any(reading.cue_id == cue for reading in readings) and not cue.startswith(
            "sign"
        ):
            readings += (CueReading(cue, None, 0.0, 0.0, valid=False, note=note),)
    return readings


def _direction_abstain(
    reason: str,
    *,
    readings: tuple[CueReading, ...] = (),
    spread: float = 0.0,
    margin: float = 0.0,
    reversal_refused: bool = False,
) -> DirectionResult:
    return DirectionResult(
        observation=DirectionObservation(
            error_deg=None,
            confidence=0.0,
            cue_id="topology_consensus",
            cue_disagreement_deg=round(spread, 2) if spread else None,
            valid=False,
            abstain_reason=reason,
            sign_margin_deg=round(margin, 2),
            cues=readings,
        ),
        tip_px=None,
        tail_px=None,
        readings=readings,
        reversal_refused=reversal_refused,
    )
