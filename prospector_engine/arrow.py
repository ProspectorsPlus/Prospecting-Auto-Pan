"""Scored, explainable arrow detection and signed direction estimation.

Why this module exists, in one observed failure: in daylight a large patch of
grass matching the arrow's colour was promoted over the real arrow, because
candidates were ranked by **area** and confidence was mostly "how close is this
blob's area to the middle of the allowed range". Colour was being treated as
object identity, and a big blob of the right colour won.

Measurements from the owner's crops make the problem precise. On grass the
arrow's green chromaticity is **0.518** and the grass behind it is **0.520** -
indistinguishable. What separates them is that the arrow is roughly twice as
bright as its immediate surround, and that it is *shaped like an arrow*.

So colour proposes and geometry disposes:

    chroma band  ->  bounded component split  ->  scored candidates
                 ->  top-K hypotheses  ->  gated track  ->  signed direction

Three design decisions come straight out of the measurements
(see ``DECISIONS.md`` D-024):

**The two-notch signature is the discriminator.** In every supplied crop -
flat-on, foreshortened, huge, clipped - the contour has exactly two deep
convexity defects of comparable depth, and the third-deepest is an order of
magnitude smaller. That pattern is where the arrowhead meets the shaft. It is
invariant to rotation and scale, and terrain does not produce it.

**PCA is not a direction cue for this shape.** The arrow's fitted-ellipse
elongation is 1.27-1.53 flat-on. An axis that weakly conditioned flips sign
readily, which is exactly the 180-degree flip seen in the field. PCA is kept as
an *unsigned* axis and only ever contributes when anisotropy clears a floor.

**Direction comes from topology instead.** The midpoint of the two notches is
the base of the head; the hull vertex farthest from it is the tip. The vector
between them is signed by construction, and it is well conditioned precisely
because the head is long relative to the notch separation.

Nothing here is enabled for Live by itself. E-PROF and E-DIR-E2E gate that, and
both are PENDING.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
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
)

__all__ = [
    "ArrowDetector",
    "ArrowHypothesis",
    "DetectorConfig",
    "DirectionEstimator",
    "ShapeFeatures",
    "circular_consensus",
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

    ``readings`` are ``(cue_id, heading_deg, confidence)``. Averaging headings
    arithmetically is wrong across the +-180 seam, and averaging *all* of them
    is wrong when one cue is a 180-degree outlier - the answer would be a
    confident value that no cue actually reported. So the mean is taken on the
    unit circle, cues more than ``outlier_deg`` from it are dropped, and the
    mean is recomputed once from the survivors.

    Returns ``(heading, spread_deg, weights)``. ``weights`` includes every cue,
    with zero for the ones consensus rejected, so a discarded outlier stays
    visible in the diagnostics instead of silently vanishing.
    """
    weights = {cue: 0.0 for cue, _heading, _confidence in readings}
    usable = [(cue, h, max(0.0, c)) for cue, h, c in readings if c > 0.0]
    if not usable:
        return (None, 0.0, weights)

    def _resultant(items: list[tuple[str, float, float]]) -> tuple[float, float]:
        """``(mean_heading_deg, resultant_length)`` over the unit circle."""
        x = sum(c * math.cos(math.radians(h)) for _cue, h, c in items)
        y = sum(c * math.sin(math.radians(h)) for _cue, h, c in items)
        total = sum(c for _cue, _h, c in items)
        length = math.hypot(x, y) / total if total > 1e-9 else 0.0
        return (math.degrees(math.atan2(y, x)), length)

    # The provisional mean is only a starting point for the outlier filter. It
    # is deliberately not sanity-checked here: two cues agreeing and one
    # pointing backwards give a resultant of 0.33, and rejecting that would
    # throw away the majority the filter exists to find.
    provisional, _strength = _resultant(usable)
    survivors = [item for item in usable if abs(wrap_deg(item[1] - provisional)) <= outlier_deg]
    if len(survivors) * 2 < len(usable):
        # Fewer than half survived: what is left is not a consensus, it is
        # whichever cue happened to sit nearest an arbitrary starting point.
        # Cues at 0, 120 and -120 degrees land here, where they belong -
        # answering 0 would be a fabrication from atan2(0, 0).
        return (None, 180.0, weights)
    consensus, strength = _resultant(survivors)
    if strength < MIN_RESULTANT:
        return (None, 180.0, weights)
    for cue, _heading, confidence in survivors:
        weights[cue] = confidence
    spread = max(abs(wrap_deg(h - consensus)) for _cue, h, _c in survivors)
    return (wrap_deg(consensus), spread, weights)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _band(value: float, low: float, peak_low: float, peak_high: float, high: float) -> float:
    """Trapezoidal membership in [0, 1]. Zero outside ``[low, high]``.

    Soft bands rather than hard gates: a single feature slightly out of range
    should cost a candidate score, not silently delete it, because a rejection
    with a visible reason is worth far more than a missing candidate.

    The interval is **closed**. It has to be: the notch-ratio band's plateau
    ends at exactly 1.0, and a pair of perfectly matched notches - the ideal
    case, which a rendered arrow produces exactly - has a ratio of exactly 1.0.
    With an open bound the best possible evidence scored zero.
    """
    if value < low or value > high:
        return 0.0
    if peak_low <= value <= peak_high:
        return 1.0
    if value < peak_low:
        return (value - low) / max(1e-9, peak_low - low)
    return (high - value) / max(1e-9, high - peak_high)


@dataclass(frozen=True)
class DetectorConfig:
    """Scoring bands, all traceable to a measurement or to a named bound.

    The shape bands come from segmenting the owner's crops: four flat-on views
    on dirt, grass, water and pale terrain, plus a foreshortened one, a
    right-pointing one, and one where the arrow fills a quarter of the frame
    and is partly transparent. Those seven are a *prior*, not a corpus - E-PROF
    is what turns them into a gate.
    """

    # -- proposals --------------------------------------------------------
    #: Minimum share of the frame a candidate must occupy. Very small, because
    #: the arrow is far away for most of a route.
    min_area_fraction: float = 0.00012
    #: Maximum share. Generous, because when the player is under the arrow it
    #: fills a quarter of the view (measured: 25.2%) and is still the arrow.
    max_area_fraction: float = 0.45
    morph_ksize: int = 3
    blur_ksize: int = 3

    # -- local contrast ---------------------------------------------------
    #: Ring thickness for the local background estimate, as a fraction of the
    #: candidate's bounding diagonal.
    ring_fraction: float = 0.35
    #: Interior luminance divided by ring luminance. Measured 1.21 (pale
    #: terrain) to 2.65 (dirt), so the band opens below the weakest observation
    #: and saturates a little above it.
    contrast_low: float = 1.06
    contrast_full: float = 1.30
    #: Pixel-level gate for the *locally bright* proposal source. Deliberately
    #: looser than the per-candidate band above: this only decides what gets
    #: considered, and the score decides what wins.
    pixel_contrast_ratio: float = 1.10
    #: The background estimate is a blur computed on a downscaled copy. The
    #: divisor keeps it cheap; the kernel fraction sets the spatial scale at
    #: which "locally brighter than the surround" is judged.
    background_divisor: int = 8
    background_kernel_fraction: float = 0.28
    #: Components larger than this share of the frame are not split: a global
    #: histogram over most of the image describes the background, not the
    #: object, which is exactly how an Otsu split on grass separated the
    #: terrain's own shading instead of the arrow.
    max_split_area_fraction: float = 0.25
    #: Two proposals overlapping by more than this are the same object.
    duplicate_iou: float = 0.6
    #: Hard cap on the local-contrast ring radius, in pixels. Without it a
    #: colour proposal covering the whole frame asks for a 257-pixel structuring
    #: element and costs most of a second per frame.
    max_ring_radius_px: int = 41

    # -- shape ------------------------------------------------------------
    #: Measured 0.851-0.961 on clean masks across every crop.
    solidity_band: tuple[float, float, float, float] = (0.55, 0.78, 0.98, 1.0)
    #: Measured 0.467-0.686.
    extent_band: tuple[float, float, float, float] = (0.28, 0.45, 0.75, 0.90)
    #: Measured 0.510-0.633.
    circularity_band: tuple[float, float, float, float] = (0.30, 0.45, 0.72, 0.88)
    #: approxPolyDP at 2% of perimeter: measured 5-8 vertices.
    vertex_band: tuple[float, float, float, float] = (4.0, 5.0, 9.0, 13.0)

    # -- the two-notch signature -----------------------------------------
    #: Deepest defect as a fraction of the bounding diagonal. Measured 0.043
    #: (strongly foreshortened) to 0.155 (flat on).
    notch_depth_band: tuple[float, float, float, float] = (0.015, 0.035, 0.22, 0.32)
    #: Second defect over the first. Measured 0.60-0.90: the two notches are a
    #: matched pair, which is what makes the signature specific.
    notch_ratio_band: tuple[float, float, float, float] = (0.25, 0.45, 1.0, 1.0)
    #: Third defect over the second. Measured 0.03-0.55; anything above roughly
    #: 0.7 means the outline is ragged, which terrain is and an arrow is not.
    notch_third_max: float = 0.70

    # -- boundary ---------------------------------------------------------
    #: Contour gradient over the frame's median gradient. A crisp silhouette
    #: scores; a threshold boundary wandering through grass texture does not.
    boundary_low: float = 0.9
    boundary_full: float = 2.5

    # -- term weights -----------------------------------------------------
    weights: tuple[tuple[str, float], ...] = (
        ("contrast", 1.30),
        ("topology", 1.60),
        ("solidity", 0.80),
        ("extent", 0.60),
        ("circularity", 0.60),
        ("vertices", 0.40),
        #: Weighted as heavily as shape: a merged blob's contour cuts through
        #: the flat interior of its own parts, so a weak boundary is the clearest
        #: signal that an outline is an artefact of thresholding.
        ("boundary", 1.00),
        ("chroma", 0.50),
        ("scale", 0.30),
    )
    #: How much of its score a candidate keeps when it has no arrowhead
    #: notches at all. Below the acceptance threshold by construction: the
    #: two-notch signature is the discriminator, so its absence has to be
    #: disqualifying rather than merely expensive.
    topology_floor: float = 0.35
    #: A candidate scoring below this is rejected outright and recorded.
    accept_threshold: float = 0.55
    #: The winner must beat the runner-up by this much, or the frame abstains.
    #: A plausible tie is an abstention, never a coin flip.
    ambiguity_margin: float = 0.12

    # -- splitting --------------------------------------------------------
    #: A component whose score is poor is split once against its own luminance
    #: histogram, which is what separates a bright arrow from the matching
    #: grass a morphological close welded it to.
    split_below_score: float = 0.62
    max_split_children: int = 6
    #: A child must be at least this fraction of its parent to be considered.
    min_split_fraction: float = 0.04

    # -- tracking ---------------------------------------------------------
    top_k: int = 3
    #: Gate radius for associating a candidate with the predicted track, as a
    #: fraction of the frame diagonal.
    gate_fraction: float = 0.18
    #: Permitted per-frame scale change, as a ratio.
    scale_gate: float = 1.8
    #: Permitted per-frame orientation change.
    orientation_gate_deg: float = 55.0
    #: Frames a better candidate must sustain before the track switches to it.
    switch_frames: int = 3
    #: How much better it must be while doing so.
    switch_margin: float = 0.10
    #: Frames a track survives without a measurement.
    max_track_age: int = 6
    #: Force a global search this often, so a track cannot quietly follow the
    #: wrong thing indefinitely.
    reacquire_every: int = 45

    # -- direction --------------------------------------------------------
    #: Eigenvalue ratio below which the PCA axis is refused as a cue. The
    #: arrow measures 1.27-1.53 flat on, which is *below* this on purpose:
    #: PCA is a fallback for foreshortened views, not the primary cue.
    min_anisotropy: float = 1.9
    #: Cues further than this from the consensus are dropped as outliers.
    cue_outlier_deg: float = 32.0
    #: Consensus spread above this is a disagreement, not an answer.
    max_cue_spread_deg: float = 28.0
    #: Minimum tip-over-notch-separation before the head is long enough for its
    #: direction to be well conditioned.
    min_head_ratio: float = 0.45
    #: Minimum polarity margin, in degrees. Below it the taper difference
    #: between the two ends is too small to call and the frame abstains.
    #: Calibrated at 16: the strongly foreshortened real crop measures 2.7 and
    #: must abstain, while the right-pointing one measures 18.9 and must not.
    min_sign_margin_deg: float = 16.0

    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="owner-supplied arrow crops, measured 2026-08-28; DECISIONS.md D-024",
            note="shape bands are fitted to seven real views across four terrains; "
            "E-PROF and E-DIR-E2E are PENDING and no threshold here is validated",
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
    """Everything geometric a scorer or a direction cue needs from one blob."""

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
    #: The two deepest convexity defects, deepest first, as (point, depth_px).
    notches: tuple[tuple[tuple[float, float], float], ...]
    third_notch_depth_px: float
    clipped: bool
    contour_px: tuple[tuple[int, int], ...]
    hull_px: tuple[tuple[float, float], ...]

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


#: How many convexity defects are considered when looking for the notch pair.
#: Four is enough for the real outlines - measured third-deepest defects are an
#: order of magnitude shallower - while keeping the pair search at six options.
_NOTCH_CANDIDATES = 4

#: Defects shallower than this fraction of the bounding diagonal are outline
#: noise. The shallowest real notch measured, on a strongly foreshortened
#: arrow, was 0.043.
_MIN_NOTCH_DEPTH_FRACTION = 0.015


def _select_notch_pair(
    defects: list[tuple[tuple[float, float], float]],
    centroid: tuple[float, float],
    diagonal: float,
) -> tuple[tuple[tuple[tuple[float, float], float], ...], float]:
    """Pick the two defects that are actually the arrowhead's notches.

    Taking the two deepest is wrong under perspective: when the arrow is nearly
    edge-on one notch shrinks below some unrelated nick in the outline, and the
    "notch line" then runs from the head to somewhere along the shaft. That is
    how a strongly foreshortened arrow ended up with a head reaching 121 px on
    one side of its notch line and 301 px on the other.

    The real pair has a property nothing else does: **the segment joining them
    passes close to the centroid**, because it is the shape's own waist. So the
    top few defects are searched pairwise for depth agreement and proximity of
    their connecting line to the centroid.
    """
    # A defect a few pixels deep is a rasterisation artefact, not a notch. Two
    # such artefacts either side of the centroid otherwise beat the real pair
    # on the waist test, which is how an ellipse acquired an arrowhead.
    floor = _MIN_NOTCH_DEPTH_FRACTION * diagonal
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
            # Distance from the centroid to the line through the pair.
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


def _contour_features(
    contour: NDArray[Any], frame_size: tuple[int, int]
) -> ShapeFeatures | None:
    """Derive every geometric feature from one contour, or ``None`` if degenerate."""
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
    moments = cv2.moments(contour)
    if abs(moments["m00"]) < 1e-6:
        return None
    centroid = (moments["m10"] / moments["m00"], moments["m01"] / moments["m00"])

    # Central second moments give the principal axis and, more usefully here,
    # how *badly conditioned* it is.
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

    frame_w, frame_h = frame_size
    return ShapeFeatures(
        area_px=area,
        bbox_px=(x, y, width, height),
        centroid_px=centroid,
        solidity=area / hull_area,
        extent=area / float(width * height),
        circularity=4.0 * math.pi * area / (perimeter * perimeter),
        vertices=len(approx),
        diagonal_px=diagonal,
        anisotropy=float(min(anisotropy, 99.0)),
        axis_unit_xy=axis,
        notches=notches,
        third_notch_depth_px=third,
        clipped=x <= 1 or y <= 1 or x + width >= frame_w - 1 or y + height >= frame_h - 1,
        contour_px=tuple((int(p[0][0]), int(p[0][1])) for p in approx),
        hull_px=tuple((float(p[0][0]), float(p[0][1])) for p in hull),
    )


# ---------------------------------------------------------------------------
# Candidate hypotheses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArrowHypothesis:
    """One scored candidate, with the whole breakdown that produced the score.

    ``terms`` is the diagnostic that matters. "Rejected, score 0.31" is not
    actionable; "shape 0.91, contrast 0.08" says the blob is arrow-shaped but
    invisible against its background, which is a different bug entirely.
    """

    label: int
    features: ShapeFeatures
    terms: tuple[tuple[str, float], ...]
    score: float
    accepted: bool
    reason: str | None
    mask: NDArray[Any] = field(repr=False, default_factory=lambda: np.zeros((1, 1), np.uint8))

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
            accepted=self.accepted,
            rejected_reason=self.reason,
            score_terms=self.terms,
            contour_px=self.features.contour_px,
        )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


class _Scorer:
    """Turns one component into a weighted, explainable score.

    Every term is independent evidence in [0, 1]. That is the whole point: the
    previous confidence was an area-fit number wearing a probability's clothes,
    so a large blob of the right colour could score highly on nothing but being
    the right size.
    """

    def __init__(self, config: DetectorConfig) -> None:
        self._config = config

    def score(
        self,
        features: ShapeFeatures,
        *,
        contrast: float,
        chroma: float,
        boundary: float,
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
        terms: tuple[tuple[str, float], ...] = (
            (
                "contrast",
                _band(contrast, config.contrast_low, config.contrast_full, 99.0, 100.0),
            ),
            ("topology", topology),
            ("solidity", _band(features.solidity, *config.solidity_band)),
            ("extent", _band(features.extent, *config.extent_band)),
            ("circularity", _band(features.circularity, *config.circularity_band)),
            ("vertices", _band(float(features.vertices), *config.vertex_band)),
            (
                "boundary",
                _band(boundary, config.boundary_low, config.boundary_full, 99.0, 100.0),
            ),
            ("chroma", max(0.0, min(1.0, chroma))),
            (
                "scale",
                1.0
                if config.min_area_fraction <= area_fraction <= config.max_area_fraction
                else 0.0,
            ),
        )
        # Topology is necessary, not merely heavy. An arrow-coloured ellipse
        # can satisfy contrast, solidity, extent and scale simultaneously - it
        # did, and it won - so a candidate with no arrowhead notches is scaled
        # below the acceptance threshold however good the rest of it looks. It
        # is still scored and recorded, with its weakest term named.
        others = tuple((name, value) for name, value in terms if name != "topology")
        total_weight = sum(config.weight_for(name) for name, _value in others)
        weighted = sum(config.weight_for(name) * value for name, value in others)
        base = weighted / total_weight if total_weight > 0 else 0.0
        score = base * (config.topology_floor + (1.0 - config.topology_floor) * topology)
        # A clipped arrow is still the arrow - when the player stands under it,
        # it fills the view and touches every edge. Clipping costs confidence
        # and disables the shape terms' claim to completeness; it is never an
        # automatic rejection, which is what the previous detector did.
        if features.clipped:
            score *= 0.72
        return (terms, round(score, 4))


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------


class ArrowDetector:
    """Proposal, bounded split, scoring, top-K hypotheses, and a gated track.

    Colour never decides anything on its own here. It produces proposals; the
    score decides; ties abstain.
    """

    def __init__(
        self,
        profile: Any,
        config: DetectorConfig | None = None,
        *,
        exclusion_regions_px: tuple[tuple[int, int, int, int], ...] = (),
    ) -> None:
        self._profile = profile
        self._config = config or DetectorConfig()
        self._scorer = _Scorer(self._config)
        self._exclusions = exclusion_regions_px
        self.reset()

    # -- lifecycle --------------------------------------------------------
    def reset(self) -> None:
        """Drop every piece of temporal state. Called on any world change."""
        self._track_id = 0
        self._track_centroid: tuple[float, float] | None = None
        self._track_velocity = (0.0, 0.0)
        self._track_scale = 0.0
        self._track_heading: float | None = None
        self._track_age = 0
        self._track_hits = 0
        self._miss_frames = 0
        self._frames_since_global = 0
        self._challenger: tuple[float, float] | None = None
        self._challenger_frames = 0
        self.switches = 0

    @property
    def profile(self) -> Any:
        return self._profile

    @property
    def config(self) -> DetectorConfig:
        return self._config

    @property
    def track_id(self) -> int | None:
        return self._track_id or None

    @property
    def track_age(self) -> int:
        return self._track_hits

    def predicted_centroid(self) -> tuple[float, float] | None:
        """Constant-velocity prediction, used only to *prioritize* a search.

        It never fabricates a measurement: when nothing is detected the track
        ages and drops, and the caller sees an abstention (plan 8).
        """
        if self._track_centroid is None or self._miss_frames > self._config.max_track_age:
            return None
        return (
            self._track_centroid[0] + self._track_velocity[0] * self._miss_frames,
            self._track_centroid[1] + self._track_velocity[1] * self._miss_frames,
        )

    # -- proposals --------------------------------------------------------
    def _channels(self, bgr: NDArray[Any]) -> dict[str, NDArray[Any]]:
        import cv2

        blur = self._config.blur_ksize | 1
        smoothed = cv2.GaussianBlur(bgr, (blur, blur), 0) if blur > 1 else bgr
        blue, green, red = (smoothed[:, :, i].astype(np.float32) for i in range(3))
        total = np.maximum(blue + green + red, 1.0)
        luminance = 0.114 * blue + 0.587 * green + 0.299 * red
        return {
            "b": blue,
            "g": green,
            "r": red,
            "total": total,
            "g_chroma": green / total,
            "r_chroma": red / total,
            "b_chroma": blue / total,
            "luminance": luminance,
        }

    def _local_background(self, luminance: NDArray[Any]) -> NDArray[Any]:
        """A slowly-varying estimate of what is *behind* each pixel.

        Computed on a downscaled copy so a wide kernel costs nothing. Compared
        against, this is the lighting-invariant version of "the arrow is
        brighter than the grass": on grass the two share a chromaticity of
        0.518 against 0.520, but the arrow's luminance is 227 against 130.
        """
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

    def _proposal_masks(
        self, channels: dict[str, NDArray[Any]]
    ) -> tuple[NDArray[np.uint8], NDArray[np.uint8]]:
        """Two proposal sources, because neither alone covers the real cases.

        *Chroma and locally bright* isolates the arrow when the terrain shares
        its colour - the grass case, where colour is worthless on its own.

        *Chroma alone* is kept because the bright test fails in the opposite
        situation: when the arrow fills a quarter of the view it becomes its
        own local background, and the annulus that survives is not an arrow.

        Both are scored. Neither decides anything.
        """
        chroma = self._chroma_mask(channels)
        background = self._local_background(channels["luminance"])
        bright = channels["luminance"] >= background * self._config.pixel_contrast_ratio
        combined = (chroma & (bright.astype(np.uint8) * 255)).astype(np.uint8)
        return (self._clean(combined), self._clean(chroma))

    def _clean(self, mask: NDArray[np.uint8]) -> NDArray[np.uint8]:
        import cv2

        size = self._config.morph_ksize | 1
        if size <= 1:
            return mask
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
        opened = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        closed = cv2.morphologyEx(opened, cv2.MORPH_CLOSE, kernel)
        result: NDArray[np.uint8] = np.asarray(closed, dtype=np.uint8)
        return result

    def _chroma_mask(self, channels: dict[str, NDArray[Any]]) -> NDArray[np.uint8]:
        """The colour proposal. It is *allowed* to include terrain.

        Being loose is deliberate: on grass the arrow and the background have
        the same chromaticity to three decimal places, so a mask tight enough
        to exclude grass would also exclude the arrow.
        """
        import cv2

        parameters = getattr(self._profile, "parameters", {}) or {}
        rule = getattr(self._profile, "rule", "chroma_band")
        if rule == "chroma_band":
            keep = (
                (channels["g_chroma"] >= float(parameters.get("min_green_chroma", 0.38)))
                & (channels["g_chroma"] <= float(parameters.get("max_green_chroma", 0.70)))
                & (channels["b_chroma"] <= float(parameters.get("max_blue_chroma", 0.30)))
                & (channels["r_chroma"] >= float(parameters.get("min_red_chroma", 0.20)))
                & (channels["g"] >= float(parameters.get("min_green", 90.0)))
            )
        elif rule == "channel_relation":
            close = np.abs(channels["r"] - channels["g"]) <= float(
                parameters.get("max_rg_delta", 28)
            )
            bright = np.minimum(channels["r"], channels["g"]) >= float(
                parameters.get("min_rg", 150)
            )
            suppressed = channels["b"] <= np.minimum(channels["r"], channels["g"]) - float(
                parameters.get("min_blue_gap", 60)
            )
            keep = close & bright & suppressed
        elif rule == "hsv_range":
            lower = np.array(parameters["lower_hsv"], dtype=np.uint8)
            upper = np.array(parameters["upper_hsv"], dtype=np.uint8)
            stacked = np.dstack([channels["b"], channels["g"], channels["r"]]).astype(np.uint8)
            hsv = cv2.cvtColor(stacked, cv2.COLOR_BGR2HSV)
            keep = cv2.inRange(hsv, lower, upper) > 0
        elif rule == "bgr_range":
            lower = np.array(parameters["lower_bgr"], dtype=np.float32)
            upper = np.array(parameters["upper_bgr"], dtype=np.float32)
            keep = np.ones(channels["g"].shape, dtype=bool)
            for index, name in enumerate(("b", "g", "r")):
                keep &= (channels[name] >= lower[index]) & (channels[name] <= upper[index])
        else:  # pragma: no cover - guarded by the profile schema
            raise ValueError(f"unknown profile rule {rule!r}")

        mask = (keep.astype(np.uint8)) * 255
        for x, y, width, height in self._exclusions:
            mask[max(0, y) : y + height, max(0, x) : x + width] = 0
        result: NDArray[np.uint8] = mask
        return result

    # -- measurement helpers ---------------------------------------------
    @staticmethod
    def _ring_contrast(
        luminance: NDArray[Any],
        mask: NDArray[np.uint8],
        features: ShapeFeatures,
        ring: float,
        max_radius: int,
    ) -> float:
        """Interior luminance over the luminance of a ring just outside it.

        Local rather than absolute, because the arrow is translucent over sky,
        opaque over dirt, and roughly twice the brightness of grass - only the
        *ratio* to its own surround holds across all three.

        Computed inside a window around the candidate and with a bounded
        kernel. Dilating a full-frame mask with a 257-pixel ellipse - which is
        what an unbounded ring radius asks for when the colour proposal covers
        the whole image - costs most of a second per frame.
        """
        import cv2

        radius = max(3, min(max_radius, int(features.diagonal_px * ring * 0.5)) | 1)
        x, y, width, height = features.bbox_px
        pad = radius + 4
        top = max(0, y - pad)
        left = max(0, x - pad)
        bottom = min(mask.shape[0], y + height + pad)
        right = min(mask.shape[1], x + width + pad)
        window = mask[top:bottom, left:right]
        light = luminance[top:bottom, left:right]
        if window.size == 0:
            return 0.0
        # A rectangular element is separable, so OpenCV runs it in two passes
        # instead of one square one. The ring only has to sample the immediate
        # surround; its shape does not matter.
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (radius, radius))
        near = cv2.dilate(window, np.ones((5, 5), np.uint8))
        ring_mask = cv2.subtract(cv2.dilate(window, kernel), near)
        inner = cv2.erode(window, np.ones((5, 5), np.uint8))
        interior = light[inner > 0] if np.any(inner) else light[window > 0]
        surround = light[ring_mask > 0]
        if interior.size == 0 or surround.size == 0:
            return 0.0
        return float(interior.mean() / max(1.0, surround.mean()))

    @staticmethod
    def _boundary_strength(
        gradient: NDArray[Any], features: ShapeFeatures, reference: float
    ) -> float:
        """Mean image gradient along the contour, over the frame's median.

        A real silhouette has a crisp edge. A colour threshold that wandered
        through grass has a boundary sitting in flat texture, which is what
        distinguishes the two even when their colours agree exactly.
        """
        if not features.contour_px or reference <= 0.0:
            return 0.0
        height, width = gradient.shape[:2]
        samples: list[float] = []
        for x, y in features.contour_px:
            if 0 <= x < width and 0 <= y < height:
                samples.append(float(gradient[y, x]))
        if not samples:
            return 0.0
        return float(np.mean(samples) / reference)

    @staticmethod
    def _chroma_fit(channels: dict[str, NDArray[Any]], mask: NDArray[np.uint8]) -> float:
        """How green-dominant the interior is, on a normalized scale.

        Reported as evidence rather than used as a gate: on grass this term is
        satisfied by the background too, which is precisely why it carries a
        small weight.
        """
        selected = mask > 0
        if not np.any(selected):
            return 0.0
        green = float(channels["g_chroma"][selected].mean())
        blue = float(channels["b_chroma"][selected].mean())
        return max(0.0, min(1.0, (green - blue) * 2.2))

    # -- component extraction --------------------------------------------
    def _components(
        self, mask: NDArray[np.uint8], frame_area: float
    ) -> list[tuple[int, NDArray[np.uint8]]]:
        import cv2

        # Typed as NDArray[Any] because the OpenCV stubs' overloads do not
        # admit an explicit uint8 dtype, which is the only thing this accepts.
        labelled: NDArray[Any] = np.asarray(mask, dtype=np.uint8)
        count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
            labelled, connectivity=8
        )
        found: list[tuple[int, NDArray[np.uint8]]] = []
        minimum = self._config.min_area_fraction * frame_area
        for label in range(1, count):
            if float(stats[label, cv2.CC_STAT_AREA]) < minimum:
                continue
            found.append((label, (labels == label).astype(np.uint8) * 255))
        return found

    def _split(
        self, component: NDArray[np.uint8], luminance: NDArray[Any], frame_area: float
    ) -> list[NDArray[np.uint8]]:
        """Split one component against its own luminance histogram, once.

        This is the daylight fix. When a morphological close welds the arrow to
        the grass behind it, the merged blob is bimodal in luminance - a bright
        arrow and a darker background sharing one chromaticity - and Otsu
        separates them cleanly. Bounded to a single level and a handful of
        children so a textured region cannot fragment without limit.
        """
        import cv2

        selected = component > 0
        values = luminance[selected]
        if values.size < 64:
            return []
        if values.size > self._config.max_split_area_fraction * frame_area:
            # A histogram over most of the image describes the background, not
            # the object. Splitting here separated the grass's own shading.
            return []
        scaled = np.clip(values, 0, 255).astype(np.uint8)
        threshold, _ = cv2.threshold(scaled, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        bright = np.zeros_like(component)
        bright[selected & (luminance >= float(threshold))] = 255
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        opened: NDArray[Any] = np.asarray(
            cv2.morphologyEx(bright, cv2.MORPH_OPEN, kernel), dtype=np.uint8
        )
        count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
            opened, connectivity=8
        )
        parent_area = float(selected.sum())
        children: list[tuple[float, NDArray[np.uint8]]] = []
        for label in range(1, count):
            area = float(stats[label, cv2.CC_STAT_AREA])
            if area < max(
                self._config.min_area_fraction * frame_area,
                self._config.min_split_fraction * parent_area,
            ):
                continue
            children.append((area, (labels == label).astype(np.uint8) * 255))
        children.sort(key=lambda item: item[0], reverse=True)
        return [child for _area, child in children[: self._config.max_split_children]]

    # -- the pass ---------------------------------------------------------
    def analyze(
        self, frame: CapturedFrame, *, roi_px: tuple[int, int, int, int] | None = None
    ) -> tuple[ArrowObservation, tuple[ArrowHypothesis, ...]]:
        """One detection pass. Returns the observation and every hypothesis."""
        import cv2

        if frame.capture_error is not None:
            return (_abstain(self._profile, f"capture-error:{frame.capture_error}"), ())
        if not frame.geometry.valid:
            return (_abstain(self._profile, "viewport-invalid"), ())

        bgr = np.asarray(frame.bgr)
        offset_x, offset_y = 0, 0
        if roi_px is not None:
            offset_x, offset_y, roi_w, roi_h = roi_px
            bgr = np.ascontiguousarray(
                bgr[offset_y : offset_y + roi_h, offset_x : offset_x + roi_w]
            )
        if bgr.size == 0:
            return (_abstain(self._profile, "empty-region"), ())

        channels = self._channels(bgr)
        bright_mask, chroma_mask = self._proposal_masks(channels)
        luminance = channels["luminance"]
        gradient = np.abs(cv2.Sobel(luminance, cv2.CV_32F, 1, 0, ksize=3)) + np.abs(
            cv2.Sobel(luminance, cv2.CV_32F, 0, 1, ksize=3)
        )
        # A strided sample, because an exact median of 900k floats costs more
        # than the rest of the pass put together and is not more informative.
        reference_gradient = float(np.median(gradient[::7, ::7])) + 1e-3
        frame_area = float(frame.bgr.shape[0] * frame.bgr.shape[1])
        frame_size = (bgr.shape[1], bgr.shape[0])

        hypotheses: list[ArrowHypothesis] = []
        for source, mask in (("bright", bright_mask), ("chroma", chroma_mask)):
            base = 0 if source == "bright" else 500
            for label, component in self._components(mask, frame_area):
                area_fraction = float(np.count_nonzero(component)) / max(1.0, frame_area)
                oversize = area_fraction > self._config.max_area_fraction
                scored = (
                    # A component covering half the view cannot be an arrow, and
                    # measuring its local contrast means dilating a full-frame
                    # mask. It is recorded, skipped, and split.
                    _oversize_hypothesis(base + label, area_fraction)
                    if oversize
                    else self._evaluate(
                        base + label,
                        component,
                        channels,
                        luminance,
                        gradient,
                        reference_gradient,
                        frame_area,
                        frame_size,
                    )
                )
                if scored is None:
                    continue
                if not oversize:
                    hypotheses.append(scored)
                # A poor score is the merged-blob case: the arrow welded to
                # matching terrain by a morphological close.
                if scored.score < self._config.split_below_score:
                    children = self._split(component, luminance, frame_area)
                    for index, child in enumerate(children):
                        split = self._evaluate(
                            (base + label) * 1000 + index + 1,
                            child,
                            channels,
                            luminance,
                            gradient,
                            reference_gradient,
                            frame_area,
                            frame_size,
                        )
                        if split is not None:
                            hypotheses.append(split)

        if offset_x or offset_y:
            hypotheses = [_translate(h, offset_x, offset_y) for h in hypotheses]
        hypotheses.sort(key=lambda h: h.score, reverse=True)
        hypotheses = _deduplicate(hypotheses, self._config.duplicate_iou)
        hypotheses = hypotheses[: max(self._config.top_k, 1)]
        return (self._select(hypotheses, frame), tuple(hypotheses))

    def _evaluate(
        self,
        label: int,
        component: NDArray[np.uint8],
        channels: dict[str, NDArray[Any]],
        luminance: NDArray[Any],
        gradient: NDArray[Any],
        reference_gradient: float,
        frame_area: float,
        frame_size: tuple[int, int],
    ) -> ArrowHypothesis | None:
        import cv2

        contours, _hierarchy = cv2.findContours(
            component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        if not contours:
            return None
        features = _contour_features(max(contours, key=cv2.contourArea), frame_size)
        if features is None:
            return None
        contrast = self._ring_contrast(
            luminance,
            component,
            features,
            self._config.ring_fraction,
            self._config.max_ring_radius_px,
        )
        boundary = self._boundary_strength(gradient, features, reference_gradient)
        chroma = self._chroma_fit(channels, component)
        terms, score = self._scorer.score(
            features,
            contrast=contrast,
            chroma=chroma,
            boundary=boundary,
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
            mask=component,
        )

    # -- selection and tracking -------------------------------------------
    def _select(
        self, hypotheses: list[ArrowHypothesis], frame: CapturedFrame
    ) -> ArrowObservation:
        config = self._config
        self._frames_since_global += 1
        viable = [h for h in hypotheses if h.accepted]
        if not viable:
            self._miss()
            reason = (
                "no-candidate" if not hypotheses else f"rejected:{hypotheses[0].weakest_term}"
            )
            return _abstain(self._profile, reason, candidates=hypotheses)

        best = viable[0]
        margin = best.score - (viable[1].score if len(viable) > 1 else 0.0)
        if len(viable) > 1 and margin < config.ambiguity_margin:
            # A plausible tie abstains. Guessing between two arrow-shaped things
            # is the failure this exists to prevent (plan 7.3).
            self._miss()
            return _abstain(self._profile, "ambiguous-candidates", candidates=hypotheses)

        chosen = self._associate(best, viable, frame)
        if chosen is None:
            self._miss()
            return _abstain(self._profile, "track-gate", candidates=hypotheses)

        confidence = min(1.0, chosen.score * (0.75 + 0.25 * min(1.0, margin / 0.3)))
        features = chosen.features
        _width, height = frame.canonical_size_px
        self._commit(chosen, frame)
        return ArrowObservation(
            profile_id=getattr(self._profile, "profile_id", None),
            track_id=self._track_id or None,
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
            track_age=self._track_hits,
        )

    def _associate(
        self, best: ArrowHypothesis, viable: list[ArrowHypothesis], frame: CapturedFrame
    ) -> ArrowHypothesis | None:
        """Choose between the best-scoring candidate and the held track.

        Switching identity is allowed but must be *earned*: a challenger has to
        beat the tracked candidate by a margin, and hold that lead for several
        frames. Equally, a track is never kept merely to look stable - if the
        held candidate stops being viable it is dropped the same frame.
        """
        config = self._config
        predicted = self.predicted_centroid()
        force_global = self._frames_since_global >= config.reacquire_every
        if predicted is None or force_global:
            self._frames_since_global = 0
            self._challenger = None
            self._challenger_frames = 0
            return best

        width, height = frame.canonical_size_px
        gate = config.gate_fraction * math.hypot(width, height)
        in_gate = [
            h
            for h in viable
            if math.dist(h.features.centroid_px, predicted) <= gate
            and self._scale_ok(h)
            and self._orientation_ok(h)
        ]
        if not in_gate:
            # Nothing near the prediction: the track is stale, not the frame.
            self._drop_track()
            return best
        held = max(in_gate, key=lambda h: h.score)
        if held is best:
            self._challenger = None
            self._challenger_frames = 0
            return held
        if best.score - held.score < config.switch_margin:
            self._challenger = None
            self._challenger_frames = 0
            return held
        challenger = best.features.centroid_px
        if self._challenger is not None and math.dist(self._challenger, challenger) <= gate:
            self._challenger_frames += 1
        else:
            self._challenger, self._challenger_frames = challenger, 1
        if self._challenger_frames >= config.switch_frames:
            self.switches += 1
            self._drop_track()
            self._challenger = None
            self._challenger_frames = 0
            return best
        return held

    def _scale_ok(self, hypothesis: ArrowHypothesis) -> bool:
        if self._track_scale <= 0.0:
            return True
        scale = math.sqrt(max(1.0, hypothesis.features.area_px))
        ratio = max(scale, self._track_scale) / max(1e-6, min(scale, self._track_scale))
        return ratio <= self._config.scale_gate

    def _orientation_ok(self, hypothesis: ArrowHypothesis) -> bool:
        if self._track_heading is None or hypothesis.features.axis_unit_xy is None:
            return True
        heading = heading_deg(hypothesis.features.axis_unit_xy)
        if heading is None:
            return True
        # The axis is unsigned, so a 180-degree "change" is the same axis.
        delta = abs(wrap_deg(heading - self._track_heading))
        delta = min(delta, 180.0 - delta)
        return delta <= self._config.orientation_gate_deg

    def _commit(self, chosen: ArrowHypothesis, frame: CapturedFrame) -> None:
        del frame
        centroid = chosen.features.centroid_px
        if self._track_centroid is None:
            self._track_id += 1
            self._track_hits = 1
            self._track_velocity = (0.0, 0.0)
        else:
            self._track_hits += 1
            steps = max(1, self._miss_frames + 1)
            self._track_velocity = (
                (centroid[0] - self._track_centroid[0]) / steps,
                (centroid[1] - self._track_centroid[1]) / steps,
            )
        self._track_centroid = centroid
        self._track_scale = math.sqrt(max(1.0, chosen.features.area_px))
        if chosen.features.axis_unit_xy is not None:
            self._track_heading = heading_deg(chosen.features.axis_unit_xy)
        self._miss_frames = 0

    def _miss(self) -> None:
        self._miss_frames += 1
        if self._miss_frames > self._config.max_track_age:
            self._drop_track()

    def _drop_track(self) -> None:
        self._track_centroid = None
        self._track_velocity = (0.0, 0.0)
        self._track_scale = 0.0
        self._track_heading = None
        self._track_hits = 0
        self._miss_frames = 0


def _weakest(terms: tuple[tuple[str, float], ...]) -> str:
    return min(terms, key=lambda item: item[1])[0] if terms else "score"


def _oversize_hypothesis(label: int, area_fraction: float) -> ArrowHypothesis:
    """A placeholder for a component too large to be an arrow.

    It exists so the split path still runs, and so the diagnostics can say
    "a blob covering 71% of the view was proposed and skipped" instead of
    silently dropping it.
    """
    empty = ShapeFeatures(
        area_px=0.0,
        bbox_px=(0, 0, 0, 0),
        centroid_px=(0.0, 0.0),
        solidity=0.0,
        extent=0.0,
        circularity=0.0,
        vertices=0,
        diagonal_px=0.0,
        anisotropy=0.0,
        axis_unit_xy=None,
        notches=(),
        third_notch_depth_px=0.0,
        clipped=True,
        contour_px=(),
        hull_px=(),
    )
    return ArrowHypothesis(
        label=label,
        features=empty,
        terms=(("scale", 0.0),),
        score=0.0,
        accepted=False,
        reason=f"covers {area_fraction * 100:.0f}% of the view; too large to be an arrow",
    )


def _overlap_ratio(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Intersection over the *smaller* box.

    Not intersection-over-union: the two proposal sources routinely produce a
    whole arrow and a fragment of the same arrow - on pale terrain the shaded
    half of the arrow is darker than its own background, so only the head
    survives the brightness test. Those are nested, not overlapping, and IoU
    scores them as low as two unrelated blobs.
    """
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    left, top = max(ax, bx), max(ay, by)
    right, bottom = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if right <= left or bottom <= top:
        return 0.0
    overlap = float((right - left) * (bottom - top))
    return overlap / float(max(1, min(aw * ah, bw * bh)))


def _deduplicate(hypotheses: list[ArrowHypothesis], threshold: float) -> list[ArrowHypothesis]:
    """Keep the best-scoring hypothesis per object.

    The two proposal sources overlap on purpose, so the same arrow arrives
    twice. Without this the runner-up would be the winner's own fragment, the
    ambiguity margin would collapse, and every frame would abstain on a
    disagreement it had with itself.
    """
    kept: list[ArrowHypothesis] = []
    for candidate in hypotheses:
        if any(
            _overlap_ratio(candidate.features.bbox_px, existing.features.bbox_px) >= threshold
            for existing in kept
        ):
            continue
        kept.append(candidate)
    return kept


def _translate(hypothesis: ArrowHypothesis, dx: int, dy: int) -> ArrowHypothesis:
    """Move a region-of-interest hypothesis back into canonical coordinates."""
    features = hypothesis.features
    x, y, width, height = features.bbox_px
    moved = ShapeFeatures(
        area_px=features.area_px,
        bbox_px=(x + dx, y + dy, width, height),
        centroid_px=(features.centroid_px[0] + dx, features.centroid_px[1] + dy),
        solidity=features.solidity,
        extent=features.extent,
        circularity=features.circularity,
        vertices=features.vertices,
        diagonal_px=features.diagonal_px,
        anisotropy=features.anisotropy,
        axis_unit_xy=features.axis_unit_xy,
        notches=tuple(((p[0] + dx, p[1] + dy), d) for p, d in features.notches),
        third_notch_depth_px=features.third_notch_depth_px,
        clipped=features.clipped,
        contour_px=tuple((px + dx, py + dy) for px, py in features.contour_px),
        hull_px=tuple((px + dx, py + dy) for px, py in features.hull_px),
    )
    return ArrowHypothesis(
        label=hypothesis.label,
        features=moved,
        terms=hypothesis.terms,
        score=hypothesis.score,
        accepted=hypothesis.accepted,
        reason=hypothesis.reason,
        mask=hypothesis.mask,
    )


def _abstain(
    profile: Any, reason: str, *, candidates: list[ArrowHypothesis] | None = None
) -> ArrowObservation:
    best = candidates[0] if candidates else None
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
        score_terms=best.terms if best is not None else (),
    )


# ---------------------------------------------------------------------------
# Direction
# ---------------------------------------------------------------------------


def _densify(points: tuple[tuple[float, float], ...], samples: int = 240) -> NDArray[Any]:
    """Resample a closed polygon evenly, so a sparse hull still profiles well.

    A convex hull of a block arrow has five vertices. Measuring a width profile
    from five points is noise; measuring it from an evenly sampled outline is a
    measurement.
    """
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
    #: Lateral width of the outline in the far quarter of each side. The head
    #: narrows to a point; the shaft ends in a blunt rectangle.
    head_far_width: float
    tail_far_width: float
    head_reach: float
    tail_reach: float
    #: How steeply the outline narrows with distance from the notch line, per
    #: side, in [0, 1]. This is the arrowhead's defining property.
    head_taper: float
    tail_taper: float

    @property
    def polarity_margin(self) -> float:
        """How confidently the head is the head, in [0, 1].

        Driven by **taper**, not by which end reaches further and not by raw
        width. Reach is nearly symmetric for this arrow - 0.75 against 0.60 in
        model units, a margin that noise walks across - and under perspective
        the tail can even reach *further* than the head. What never inverts is
        that the head narrows to a point along its length while the shaft keeps
        a constant width: measured taper is close to 1.0 for the head and close
        to 0.0 for the tail.
        """
        return max(0.0, min(1.0, self.head_taper - self.tail_taper))


def _decompose(features: ShapeFeatures) -> _HeadTail | None:
    """Split the outline at the notch line and profile both sides.

    The line through the two notches is where the arrowhead meets the shaft.
    Everything signed about this arrow follows from which side of it is which.
    """
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

    def _profile(selected: NDArray[Any]) -> tuple[float, float, float]:
        """``(reach, far_width, taper)`` for one side of the notch line.

        Taper compares the outline's width near the notch line with its width
        at the far end. An arrowhead collapses to a point (taper near 1); a
        shaft holds its width (taper near 0).
        """
        side_reach = np.abs(reach[selected])
        side_lateral = lateral[selected]
        extreme = float(side_reach.max())
        if extreme <= 1e-6:
            return (0.0, 0.0, 0.0)

        def _width(low: float, high: float) -> float:
            band = side_lateral[(side_reach >= low * extreme) & (side_reach <= high * extreme)]
            return float(band.max() - band.min()) if band.size else 0.0

        near_width = _width(0.05, 0.35)
        far_width = _width(0.72, 1.0)
        widest = max(near_width, far_width)
        taper = (near_width - far_width) / widest if widest > 1e-6 else 0.0
        return (extreme, far_width, max(0.0, taper))

    pos_reach, pos_width, pos_taper = _profile(positive)
    neg_reach, neg_width, neg_taper = _profile(negative)

    # The head is the side that tapers: it ends in a point rather than an edge.
    head_is_positive = pos_taper >= neg_taper
    head_mask = positive if head_is_positive else negative
    tail_mask = negative if head_is_positive else positive
    head_reach, head_width, head_taper = (
        (pos_reach, pos_width, pos_taper)
        if head_is_positive
        else (neg_reach, neg_width, neg_taper)
    )
    tail_reach, tail_width, tail_taper = (
        (neg_reach, neg_width, neg_taper)
        if head_is_positive
        else (pos_reach, pos_width, pos_taper)
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
    )


@dataclass(frozen=True)
class DirectionResult:
    """A signed direction plus every cue that voted on it."""

    observation: DirectionObservation
    tip_px: tuple[float, float] | None
    tail_px: tuple[float, float] | None
    readings: tuple[CueReading, ...]


class DirectionEstimator:
    """Signed arrow direction from topology, with PCA demoted to a fallback.

    The bug this replaces: an unsigned PCA axis was being reported as a
    direction, so roughly half the time it pointed at the tail. Sign is
    resolved here from *independently verified* arrowhead topology - the two
    notches, the head/tail split, and the tip - and PCA only contributes when
    the shape is elongated enough for its axis to mean anything.

    Every cue is reported with the weight consensus gave it, so a rejected
    180-degree outlier stays visible instead of silently disappearing.
    """

    def __init__(self, config: DetectorConfig | None = None) -> None:
        self._config = config or DetectorConfig()

    def estimate(
        self,
        features: ShapeFeatures | None,
        *,
        anchor_px: tuple[float, float] | None,
        forward_deg: float | None,
        arrow_confidence: float,
    ) -> DirectionResult:
        config = self._config
        if features is None:
            return _direction_abstain("no-arrow")
        if forward_deg is None:
            return _direction_abstain("no-forward-reference")

        readings: list[tuple[str, float, float]] = []
        details: dict[str, str] = {}
        tip: tuple[float, float] | None = None
        tail: tuple[float, float] | None = None
        sign_margin = 0.0

        decomposed = _decompose(features)
        if decomposed is not None:
            tail, tip = decomposed.tail_centre, decomposed.tip
            sign_margin = decomposed.polarity_margin
            details["polarity"] = (
                f"head far-width {decomposed.head_far_width:.1f} px vs tail "
                f"{decomposed.tail_far_width:.1f} px"
            )
            bearing = heading_deg(
                (
                    decomposed.head_centre[0] - decomposed.tail_centre[0],
                    decomposed.head_centre[1] - decomposed.tail_centre[1],
                )
            )
            if bearing is not None:
                readings.append(("tail_to_head", bearing, 1.0 * arrow_confidence))
                details["tail_to_head"] = "notch-line split"

            mid = features.notch_mid_px
            if mid is not None:
                separation = max(1.0, features.notch_separation_px)
                head_ratio = math.dist(tip, mid) / separation
                bearing = heading_deg((tip[0] - mid[0], tip[1] - mid[1]))
                if bearing is not None and head_ratio >= config.min_head_ratio:
                    # The primary cue: well conditioned precisely because the
                    # head is long relative to the notch separation.
                    readings.append(("notch_to_tip", bearing, 1.2 * arrow_confidence))
                    details["notch_to_tip"] = f"head ratio {head_ratio:.2f}"
                elif bearing is not None:
                    details["notch_to_tip"] = f"head too short ({head_ratio:.2f})"

            bearing = heading_deg(
                (tip[0] - features.centroid_px[0], tip[1] - features.centroid_px[1])
            )
            if bearing is not None:
                readings.append(("centroid_to_tip", bearing, 0.8 * arrow_confidence))

        if anchor_px is not None:
            bearing = heading_deg(
                (
                    features.centroid_px[0] - anchor_px[0],
                    features.centroid_px[1] - anchor_px[1],
                )
            )
            if bearing is not None:
                # Where the arrow *is*, not where it points. Kept separate and
                # weighted low: the two are not interchangeable, and treating
                # them as one is how a position cue outvoted a pose cue.
                readings.append(("player_to_arrow", bearing, 0.25 * arrow_confidence))
                details["player_to_arrow"] = "position, not pose"

        axis_reading = self._pca_reading(features, readings, arrow_confidence)
        if axis_reading is not None:
            readings.append(axis_reading)
            details["pca_axis"] = f"anisotropy {features.anisotropy:.2f}"
        else:
            details["pca_axis"] = (
                f"refused: anisotropy {features.anisotropy:.2f} below "
                f"{config.min_anisotropy:.2f}"
            )

        consensus, spread, weights = circular_consensus(
            readings, outlier_deg=config.cue_outlier_deg
        )
        cue_readings = tuple(
            CueReading(
                cue_id=cue,
                heading_deg=round(heading, 2),
                confidence=round(confidence, 4),
                weight=round(weights.get(cue, 0.0), 4),
                valid=weights.get(cue, 0.0) > 0.0,
                note=details.get(cue, ""),
            )
            for cue, heading, confidence in readings
        )
        for cue, note in details.items():
            if not any(reading.cue_id == cue for reading in cue_readings):
                cue_readings += (CueReading(cue, None, 0.0, 0.0, valid=False, note=note),)

        if consensus is None:
            return _direction_abstain("cues disagree", readings=cue_readings, spread=spread)
        if spread > config.max_cue_spread_deg:
            return _direction_abstain("cues disagree", readings=cue_readings, spread=spread)
        sign_margin_deg = 90.0 * sign_margin
        if sign_margin_deg < config.min_sign_margin_deg:
            return _direction_abstain(
                "polarity", readings=cue_readings, spread=spread, margin=sign_margin_deg
            )

        agreement = 1.0 - spread / max(1e-6, config.max_cue_spread_deg)
        confidence = arrow_confidence * max(0.0, agreement)
        return DirectionResult(
            observation=DirectionObservation(
                error_deg=wrap_deg(consensus - forward_deg),
                confidence=round(min(1.0, confidence), 4),
                cue_id="topology_consensus",
                cue_disagreement_deg=round(spread, 2),
                valid=True,
                abstain_reason=None,
                sign_confidence=round(min(1.0, sign_margin), 4),
                sign_margin_deg=round(sign_margin_deg, 2),
                cues=cue_readings,
                anisotropy=round(features.anisotropy, 3),
            ),
            tip_px=tip,
            tail_px=tail,
            readings=cue_readings,
        )

    def _pca_reading(
        self,
        features: ShapeFeatures,
        readings: list[tuple[str, float, float]],
        arrow_confidence: float,
    ) -> tuple[str, float, float] | None:
        """PCA as an *unsigned* axis, signed by the topology cues if it earns it.

        Two refusals: too little anisotropy for the axis to be meaningful, and
        no independent cue to resolve the sign against. Guessing the sign is
        the exact thing that produced silent 180-degree flips.
        """
        axis = features.axis_unit_xy
        if axis is None or features.anisotropy < self._config.min_anisotropy:
            return None
        bearing = heading_deg(axis)
        if bearing is None or not readings:
            return None
        reference = readings[0][1]
        flipped = wrap_deg(bearing + 180.0)
        if abs(wrap_deg(flipped - reference)) < abs(wrap_deg(bearing - reference)):
            bearing = flipped
        return ("pca_axis", bearing, 0.5 * arrow_confidence)


def _direction_abstain(
    reason: str,
    *,
    readings: tuple[CueReading, ...] = (),
    spread: float = 0.0,
    margin: float = 0.0,
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
    )
