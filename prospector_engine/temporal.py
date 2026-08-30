"""The short-horizon temporal bridge: a second, independent look at one arrow.

`prospector_engine.arrow` is the *global* authority. It proposes candidates
from the whole frame (or a predicted region), scores them structurally, and
owns identity: acquisition, association, switching, reacquisition, loss. That
half is not changed here and is not challenged by anything in this module.

What it could not do is carry an arrow through a frame it could not *segment*.
A candidate that fails the colour rule for two frames because a leaf crossed
it, or whose outline is nicked by a UI stroke, is simply absent, and absence is
an abstention however confident the previous frame was. Prediction helped
choose where to look next; it never produced evidence.

This module adds the missing half: a **bounded local measurement** that answers
"where did that exact patch of pixels go" without re-segmenting anything.

    global (structural)  ->  identity, acceptance, reacquisition, correction
    local  (this module) ->  continuity across frames the global path missed

Three rules keep the second half from becoming a way to be confidently wrong:

**The template is only ever cut on a global commit.** A bridged frame updates
where the track *is*; it never updates what the track *looks like*. That is the
whole of the anti-drift argument: an appearance that can only be rewritten by
the structural detector cannot walk onto terrain one frame at a time, however
many bridged frames run in between.

**A bridged measurement must be unambiguous, not merely good.** The correlation
peak has to clear a floor *and* beat the best rival peak outside its own
suppression radius by a margin, and the matched patch has to still stand out
from its surround. Same-coloured foliage near the predicted position is exactly
the case this must never fire on, and "near the prediction" is deliberately not
evidence - it is the search bound, not a score.

**Every bridged claim is bounded and decays.** Time since the last *global*
validation, cumulative bridged displacement since it, and per-frame speed are
all capped, and confidence decays on the monotonic clock. Past the horizon the
memory goes STALE and then LOST, and the caller sees an abstention. A bridge is
a bridge, not a licence to walk blind.

Provenance is carried out to the diagnostics rather than smoothed away, because
"the detector saw it" and "we correlated a patch that used to be it" are
different claims and a person reading a trace has to be able to tell them
apart (:class:`EvidenceProvenance`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

from prospector_engine.contracts import (
    ArrowObservation,
    CapturedFrame,
    EvidenceProvenance,
    EvidenceStatus,
    Provenance,
)

__all__ = [
    "BridgeMeasurement",
    "BridgeStats",
    "TemporalBridge",
    "TemporalConfig",
]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemporalConfig:
    """Bounds for the local tracker. Provisional configuration throughout.

    Every horizon is in **seconds**, not frames, so the same contract holds at
    30, 60 and 90 Hz and on a corpus sampled at 5. A frame-counted bridge is a
    different promise at every cadence, and the one thing a bridge must not be
    is cadence-dependent: it is a claim about how long the world can be assumed
    to have continued, and the world does not know the frame rate.
    """

    # -- the anchored template -------------------------------------------
    #: Longest side of the stored template after downscaling. The template is
    #: correlated once per bridged frame; keeping it small is what keeps the
    #: whole path inside a 60 Hz budget.
    template_max_px: int = 44
    #: Smallest usable template side. Below this there is not enough texture
    #: for a correlation peak to mean anything and the bridge declines to
    #: anchor at all.
    template_min_px: int = 12
    #: The template is the arrow's bounding box grown by this fraction, so it
    #: carries a little of the surround - which is what makes the correlation
    #: peak sharp rather than a plateau over a uniform blob.
    template_pad_fraction: float = 0.34

    # -- the search window ------------------------------------------------
    #: Search radius around the prediction: ``base + rate * dt`` since the last
    #: measurement, capped. It widens with elapsed time for the same reason the
    #: detector's association gate does, and is capped so the cost of a bridged
    #: frame cannot grow without bound.
    #: Widened from 30 with ``suppression_fraction``: below about 46 the
    #: response map is smaller than the suppression box and the peak-margin
    #: guard cannot fire at all.
    search_radius_base_px: float = 46.0
    search_radius_rate_px_s: float = 420.0
    search_radius_max_px: float = 170.0

    # -- acceptance -------------------------------------------------------
    #: Normalised cross-correlation floor. Below this the patch in front of us
    #: is not the patch we stored, whatever else is true.
    min_correlation: float = 0.62
    #: How far the winning peak must beat the best rival peak outside its own
    #: suppression radius. Two similar peaks is the same-coloured-foliage case,
    #: and the answer there is to abstain and let the global path decide.
    peak_margin: float = 0.09
    #: Suppression radius around the winner, as a fraction of the template
    #: side, when looking for the rival peak.
    #:
    #: Measured, because the first two values chosen for this made the whole
    #: ambiguity test dead code. ``cv2.matchTemplate`` returns a response of
    #: ``window - template + 1``, and with a tight search window that is only a
    #: few tens of pixels across; a suppression radius of a whole template side
    #: then masked the *entire* response, so the rival peak was always -1 and
    #: every peak passed the margin unchallenged. At 0.30, with
    #: ``search_radius_base_px`` widened to match, a same-scale distractor 40
    #: to 55 px from the arrow is correctly refused as ambiguous while one far
    #: enough away not to corrupt the peak is correctly carried.
    #:
    #: The honest limit: this guard only reaches inside the search window. A
    #: distractor further out is the *global* path's problem, which is where
    #: full-frame proposal scoring already handles it.
    suppression_fraction: float = 0.30
    #: The matched patch must still be brighter than its own surround by this
    #: ratio. Independent of correlation on purpose: a correlation peak says
    #: "this looks like what we stored", contrast says "and it is still an
    #: object rather than a patch of flat ground that happens to correlate".
    min_contrast: float = 1.02

    # -- blind-motion limits ----------------------------------------------
    #: Apparent speed ceiling for a bridged step. D-058 measured the arrow
    #: crossing the whole client in about a second under the fastest camera
    #: turn, and the canonical diagonal is 1468 px, so anything under about
    #: 1400 px/s is motion the arrow really does manage. A correlation peak
    #: implying more than that is a different object, not a fast one. The first
    #: value here was 900, which refused legitimate 60 Hz motion.
    max_speed_px_s: float = 1400.0
    #: How long bridged measurements may carry a track since the last **global**
    #: validation. Past this the memory is STALE: the local path stops
    #: producing evidence and the global path is on its own.
    max_bridge_s: float = 0.60
    #: Cumulative bridged displacement allowed since the last global
    #: validation. A bridge that has moved the track this far without the
    #: structural detector ever confirming it has earned a challenge, not more
    #: rope.
    max_drift_px: float = 260.0
    #: Longest gap between two **consecutive frames** across which a
    #: correlation still means "the arrow moved between them".
    #:
    #: Measured against the previous frame the bridge looked at, not against
    #: the last frame it successfully matched. The first version compared
    #: against the last *success*, which made a run of refusals disable the
    #: bridge permanently: three failed correlations at 60 Hz put the gap over
    #: 100 ms, and from then on every frame was refused with ``step`` even
    #: though frames were still arriving 16 ms apart. This is a statement about
    #: the *cadence* - is this stream dense enough for patch matching to mean
    #: continuity - and cadence does not change because a match failed. How far
    #: the arrow may have travelled since the last success is a different
    #: question, and ``search_radius_rate_px_s`` and ``max_bridge_s`` are what
    #: answer it.
    #:
    #: Separate from ``max_bridge_s`` because they answer different questions.
    #: ``max_bridge_s`` asks how long we may go without the structural
    #: detector confirming the identity. This asks whether *this pair of
    #: frames* is close enough together for a patch match to be evidence of
    #: continuity at all. Two frames 200 ms apart say very little about
    #: whether a similar-looking patch is the same object - the arrow could
    #: have crossed a third of the screen in between - which is the argument
    #: D-058 made for ``resume_max_age_s`` and it applies here unchanged.
    #:
    #: The consequence is deliberate: this makes the bridge inert below about
    #: 10 fps, and therefore inert on the real corpus, which is sampled at
    #: about 5. The bridge cannot be measured there and is not claimed to have
    #: been.
    max_step_s: float = 0.10
    #: Prediction-only horizon: no correlation, no measurement, just constant
    #: velocity. Deliberately much shorter than ``max_bridge_s`` - this is the
    #: state with no evidence in it at all.
    max_predict_s: float = 0.14
    #: Half-life of bridged confidence, on the monotonic clock.
    confidence_half_life_s: float = 0.32
    #: Floor below which a decayed confidence is not worth reporting.
    min_confidence: float = 0.12

    # -- rotation ---------------------------------------------------------
    #: The rotation bank is searched over ``+-(rate * dt)``, clamped. Scaling
    #: with elapsed time keeps the angular speed it can follow constant across
    #: cadences instead of the per-frame step.
    rotation_rate_deg_s: float = 900.0
    rotation_span_min_deg: float = 8.0
    rotation_span_max_deg: float = 46.0
    rotation_step_deg: float = 4.0
    #: How much better a rotated template must correlate than the unrotated one
    #: before the rotation is believed.
    #:
    #: Measured, and it matters more than it looks. Without it the bank simply
    #: returns its best angle, and "best" beats "zero" by a rounding error on
    #: an occluded patch whose texture is mostly occluder. Those errors are
    #: signed at random and they *accumulate*: across twenty bridged frames the
    #: carried heading drifted by up to 71 degrees on the rendered occlusion
    #: families, and because the pipeline seeds the direction estimator with
    #: the heading it was last handed, that drift then poisoned the first
    #: globally observed frame after the occlusion too. This is the same
    #: "unambiguous, not merely good" rule the translation peak already has.
    rotation_min_gain: float = 0.02
    #: Cumulative rotation the bridge may accumulate since the last **global**
    #: validation. A second, independent bound on the same failure: even
    #: individually-justified rotations must not add up to an unvalidated
    #: claim that the arrow has swung a long way round.
    max_rotation_deg: float = 50.0

    # -- global validation -------------------------------------------------
    #: Disagreement between the bridge's own position and the global commit
    #: beyond which the bridge is judged to have drifted. The global answer
    #: always wins either way; this only decides whether the episode is
    #: recorded as a correction.
    disagreement_px: float = 70.0

    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="D-094; bounds chosen against tests/corpus/real tune split and the "
            "rendered temporal families in tests/tracking_families.py",
            note="no bound here is validated; the dense-cadence gates need real recordings",
        )
    )

    def __post_init__(self) -> None:
        # An unsatisfiable configuration should raise where it is written, not
        # silently produce a bridge that can never fire.
        if self.template_min_px > self.template_max_px:
            raise ValueError(
                f"template_min_px={self.template_min_px} exceeds "
                f"template_max_px={self.template_max_px}: no template could be cut"
            )
        if self.max_predict_s > self.max_bridge_s:
            raise ValueError(
                f"max_predict_s={self.max_predict_s} exceeds max_bridge_s="
                f"{self.max_bridge_s}: prediction-only would outlive measurement"
            )
        if self.max_step_s <= 0.0:
            raise ValueError("max_step_s must be positive")
        if self.rotation_span_min_deg > self.rotation_span_max_deg:
            raise ValueError("rotation_span_min_deg exceeds rotation_span_max_deg")


# ---------------------------------------------------------------------------
# What one bridged frame produced
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BridgeMeasurement:
    """One local answer, with everything needed to judge how much to trust it."""

    provenance: EvidenceProvenance
    centroid_px: tuple[float, float]
    bbox_px: tuple[int, int, int, int]
    axis_unit_xy: tuple[float, float] | None
    #: Cumulative screen rotation since the anchor, in degrees, positive in the
    #: same sense as :func:`prospector_engine.arrow.heading_deg`.
    rotation_deg: float
    #: Peak normalised cross-correlation, or 0.0 for a prediction-only step.
    correlation: float
    #: How far the winner beat the best rival peak.
    peak_margin: float
    #: Local contrast of the matched patch against its surround.
    contrast: float
    #: Decayed confidence in [0, 1].
    confidence: float
    #: Seconds since the last **global** validation.
    age_s: float
    #: Cumulative bridged displacement since the last global validation.
    drift_px: float
    #: Bridged frames since the last global validation.
    bridged_frames: int

    @property
    def measured(self) -> bool:
        """Whether pixels were actually correlated, as opposed to extrapolated."""
        return self.provenance is EvidenceProvenance.BRIDGED


@dataclass(frozen=True)
class BridgeStats:
    """Bounded counters. Every one is a small integer that never grows a list."""

    anchors: int = 0
    bridged: int = 0
    predicted: int = 0
    refused_correlation: int = 0
    refused_ambiguous: int = 0
    refused_speed: int = 0
    refused_contrast: int = 0
    expired_horizon: int = 0
    expired_drift: int = 0
    corrections: int = 0
    resets: int = 0

    def describe(self) -> str:
        return (
            f"anchors {self.anchors}, bridged {self.bridged}, predicted {self.predicted}, "
            f"refused {self.refused_correlation}/corr {self.refused_ambiguous}/amb "
            f"{self.refused_speed}/speed {self.refused_contrast}/contrast, "
            f"expired {self.expired_horizon}/time {self.expired_drift}/drift, "
            f"corrections {self.corrections}, resets {self.resets}"
        )


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------


@dataclass
class _Anchor:
    """What the last global commit left behind. Mutable; lives in the bridge."""

    track_id: int | None
    template: NDArray[np.float32]
    template_side_px: int
    #: Scale factor from frame pixels to template pixels.
    scale: float
    #: Half-extent of the anchored bounding box, in frame pixels.
    half_px: tuple[float, float]
    centroid_px: tuple[float, float]
    axis_unit_xy: tuple[float, float] | None
    confidence: float
    anchored_at_s: float
    #: Mean luminance of the anchored patch, for the contrast comparison.
    patch_mean: float


class TemporalBridge:
    """A bounded local tracker that carries one arrow across missed frames.

    The bridge holds at most one anchor, one template, and a handful of
    scalars. It allocates a search window per bridged frame and nothing else,
    so a long run costs the same as a short one and there is no history to
    grow. It owns no threads and no clock: every method is handed the frame it
    is to reason about, and time is that frame's ``captured_at_s``.

    Failure behaviour: every refusal is an abstention. :meth:`bridge` returns
    ``None`` and the caller reports whatever the global path decided, which for
    a missed frame is the detector's own abstention. The bridge never raises
    into a tick that is holding a movement lease - an OpenCV failure on a
    degenerate window is counted and refused like any other.
    """

    def __init__(self, config: TemporalConfig | None = None) -> None:
        self._config = config or TemporalConfig()
        self._stats = BridgeStats()
        self._world: tuple[object, ...] | None = None
        self.reset("constructed")

    # -- lifecycle --------------------------------------------------------
    def reset(self, reason: str) -> None:
        """Drop the anchor and every derived scalar. Called on any world change."""
        del reason
        self._anchor: _Anchor | None = None
        self._centroid_px: tuple[float, float] | None = None
        self._velocity_px_s: tuple[float, float] = (0.0, 0.0)
        self._rotation_deg = 0.0
        self._last_measured_s: float | None = None
        #: The previous frame this bridge looked at, successfully or not. The
        #: cadence bound is measured against this; the search radius and the
        #: prediction are measured against ``_last_measured_s``.
        self._last_frame_s: float | None = None
        self._drift_px = 0.0
        self._bridged_frames = 0
        self._last_sequence: int | None = None
        self._provenance = EvidenceProvenance.NONE
        self._last_disagreement_px: float | None = None
        self._last_refusal: str | None = None

    def note_world(self, fingerprint: tuple[object, ...]) -> bool:
        """Reset when the world the anchor was cut from is no longer the world.

        The fingerprint is whatever the caller considers to invalidate a stored
        patch of pixels: the run, the capture source epoch, the coordinate
        basis, the active profile. Returns whether a reset happened, so the
        caller can record it.
        """
        if self._world is not None and fingerprint != self._world:
            self._world = fingerprint
            self.reset("world-changed")
            self._stats = replace(self._stats, resets=self._stats.resets + 1)
            return True
        self._world = fingerprint
        return False

    # -- observable state -------------------------------------------------
    @property
    def config(self) -> TemporalConfig:
        return self._config

    @property
    def stats(self) -> BridgeStats:
        return self._stats

    @property
    def provenance(self) -> EvidenceProvenance:
        """What the most recent call to this bridge concluded."""
        return self._provenance

    @property
    def anchored(self) -> bool:
        return self._anchor is not None

    @property
    def track_id(self) -> int | None:
        return self._anchor.track_id if self._anchor is not None else None

    @property
    def last_disagreement_px(self) -> float | None:
        """Distance between the bridge and the last global commit, if any."""
        return self._last_disagreement_px

    @property
    def last_refusal(self) -> str | None:
        """Why the most recent bridged attempt was refused, for the trace."""
        return self._last_refusal

    def age_s(self, now_s: float) -> float | None:
        """Seconds since the last global validation, or ``None`` with no anchor."""
        if self._anchor is None:
            return None
        return max(0.0, now_s - self._anchor.anchored_at_s)

    def drift_px(self) -> float:
        return self._drift_px

    # -- the global half --------------------------------------------------
    def validate(
        self,
        frame: CapturedFrame,
        observation: ArrowObservation,
        *,
        contour_px: tuple[tuple[float, float], ...] = (),
    ) -> EvidenceProvenance:
        """Take a global commit as truth, and re-anchor on it.

        Returns :attr:`EvidenceProvenance.FUSED` when the bridge had been
        carrying this same identity across missed frames and the global answer
        agrees with where it thought the arrow was - that is the case where two
        independent methods corroborated each other, and it is worth being able
        to see. Otherwise :attr:`EvidenceProvenance.GLOBAL`: the structural
        detector is simply the only evidence there was.

        The global answer wins unconditionally either way. A disagreement is
        recorded and the anchor is re-cut from the global position; it is never
        averaged with the bridge's own, because averaging a correct measurement
        with a drifted one is how a corrected track stays slightly wrong.
        """
        del contour_px  # reserved: the template is cut from pixels, not outline
        centroid = observation.centroid_px
        bbox = observation.bbox_px
        if centroid is None or bbox is None:
            # A valid observation with no geometry cannot be anchored on. Keep
            # whatever the bridge already had rather than destroying it.
            return EvidenceProvenance.GLOBAL

        was_bridging = self._bridged_frames > 0 and self._anchor is not None
        corroborated = False
        if self._centroid_px is not None and was_bridging:
            gap = math.dist(self._centroid_px, centroid)
            self._last_disagreement_px = gap
            if gap > self._config.disagreement_px:
                self._stats = replace(self._stats, corrections=self._stats.corrections + 1)
            else:
                corroborated = True
        else:
            self._last_disagreement_px = None

        previous_centroid = self._centroid_px
        previous_s = self._last_measured_s
        self._anchor_from(frame, observation)
        # Velocity survives the re-anchor: it is a property of the arrow's
        # motion, not of the template, and throwing it away on every global
        # commit would leave prediction-only steps with nothing to extrapolate.
        if previous_centroid is not None and previous_s is not None:
            dt = max(1e-3, frame.captured_at_s - previous_s)
            self._velocity_px_s = (
                (centroid[0] - previous_centroid[0]) / dt,
                (centroid[1] - previous_centroid[1]) / dt,
            )
        self._provenance = (
            EvidenceProvenance.FUSED if corroborated else EvidenceProvenance.GLOBAL
        )
        return self._provenance

    # -- the local half ---------------------------------------------------
    def bridge(self, frame: CapturedFrame) -> BridgeMeasurement | None:
        """Try to carry the anchored track onto this frame. ``None`` abstains.

        One call per unique frame. A repeated ``frame.sequence`` returns
        ``None`` without advancing anything, for the same reason the detector
        refuses a duplicate commit: ageing a track for a screenshot it has
        already seen is a lie about elapsed time.
        """
        self._last_refusal = None
        anchor = self._anchor
        if anchor is None:
            self._provenance = EvidenceProvenance.NONE
            return None
        if self._last_sequence is not None and frame.sequence <= self._last_sequence:
            return None
        self._last_sequence = frame.sequence

        now_s = frame.captured_at_s
        age_s = max(0.0, now_s - anchor.anchored_at_s)
        config = self._config
        if age_s > config.max_bridge_s:
            self._stats = replace(self._stats, expired_horizon=self._stats.expired_horizon + 1)
            self._provenance = EvidenceProvenance.STALE
            self._last_refusal = f"horizon:{age_s * 1000:.0f}ms"
            return None
        if self._drift_px > config.max_drift_px:
            self._stats = replace(self._stats, expired_drift=self._stats.expired_drift + 1)
            self._provenance = EvidenceProvenance.STALE
            self._last_refusal = f"drift:{self._drift_px:.0f}px"
            return None

        step_s = max(1e-3, now_s - (self._last_frame_s or anchor.anchored_at_s))
        self._last_frame_s = now_s
        since_s = max(1e-3, now_s - (self._last_measured_s or anchor.anchored_at_s))
        if step_s > config.max_step_s:
            # This stream is too sparse for a patch match to mean continuity.
            # Prediction may still answer inside its own much shorter horizon;
            # correlation may not.
            self._last_refusal = f"step:{step_s * 1000:.0f}ms"
            return self._predict(anchor, since_s=since_s, now_s=now_s, age_s=age_s)
        measurement = self._correlate(frame, anchor, since_s=since_s, now_s=now_s)
        if measurement is not None:
            return measurement
        return self._predict(anchor, since_s=since_s, now_s=now_s, age_s=age_s)

    # -- anchoring --------------------------------------------------------
    def _anchor_from(self, frame: CapturedFrame, observation: ArrowObservation) -> None:
        """Cut a fresh template. The **only** path that writes the appearance."""
        centroid = observation.centroid_px
        bbox = observation.bbox_px
        assert centroid is not None and bbox is not None
        config = self._config
        x, y, width, height = bbox
        pad_x = width * config.template_pad_fraction
        pad_y = height * config.template_pad_fraction
        left = x - pad_x
        top = y - pad_y
        right = x + width + pad_x
        bottom = y + height + pad_y
        patch = self._crop_gray(frame, left, top, right, bottom)
        if patch is None:
            # No template could be cut - the arrow is clipped hard against an
            # edge, or the box is degenerate. Forget the old anchor rather than
            # keeping one that no longer describes what the detector selected.
            self._anchor = None
            self._provenance = EvidenceProvenance.NONE
            return
        side = max(patch.shape[0], patch.shape[1])
        if side < config.template_min_px:
            self._anchor = None
            self._provenance = EvidenceProvenance.NONE
            return
        scale = min(1.0, config.template_max_px / float(side))
        if scale < 1.0:
            target = (
                max(1, round(patch.shape[1] * scale)),
                max(1, round(patch.shape[0] * scale)),
            )
            patch = cv2.resize(patch, target, interpolation=cv2.INTER_AREA)
        template = np.asarray(patch, dtype=np.float32)
        self._anchor = _Anchor(
            track_id=observation.track_id,
            template=template,
            template_side_px=max(template.shape[0], template.shape[1]),
            scale=scale,
            half_px=((right - left) / 2.0, (bottom - top) / 2.0),
            centroid_px=centroid,
            axis_unit_xy=observation.axis_unit_xy,
            confidence=float(observation.confidence),
            anchored_at_s=frame.captured_at_s,
            patch_mean=float(np.mean(template)),
        )
        self._centroid_px = centroid
        self._last_frame_s = frame.captured_at_s
        self._rotation_deg = 0.0
        self._drift_px = 0.0
        self._bridged_frames = 0
        self._last_measured_s = frame.captured_at_s
        self._last_sequence = frame.sequence
        self._stats = replace(self._stats, anchors=self._stats.anchors + 1)

    # -- correlation ------------------------------------------------------
    def _correlate(
        self, frame: CapturedFrame, anchor: _Anchor, *, since_s: float, now_s: float
    ) -> BridgeMeasurement | None:
        """One bounded normalised cross-correlation, then a rotation refinement."""
        config = self._config
        predicted = self._predicted_centroid(since_s)
        radius = min(
            config.search_radius_max_px,
            config.search_radius_base_px + config.search_radius_rate_px_s * since_s,
        )
        # The window has to hold the template plus the search radius on each
        # side, in *template* pixels once downscaled.
        half_w = anchor.half_px[0] + radius
        half_h = anchor.half_px[1] + radius
        window = self._crop_gray(
            frame,
            predicted[0] - half_w,
            predicted[1] - half_h,
            predicted[0] + half_w,
            predicted[1] + half_h,
            origin_out=True,
        )
        if window is None:
            self._last_refusal = "window"
            return None
        patch, origin = window
        if anchor.scale < 1.0:
            target = (
                max(1, round(patch.shape[1] * anchor.scale)),
                max(1, round(patch.shape[0] * anchor.scale)),
            )
            patch = cv2.resize(patch, target, interpolation=cv2.INTER_AREA)
        template = anchor.template
        if patch.shape[0] <= template.shape[0] or patch.shape[1] <= template.shape[1]:
            self._last_refusal = "window-small"
            return None

        try:
            response = cv2.matchTemplate(
                np.asarray(patch, dtype=np.float32), template, cv2.TM_CCOEFF_NORMED
            )
        except cv2.error:
            # A degenerate window - a uniform patch, a zero-variance template -
            # is an abstention, never an exception into a tick holding a lease.
            self._last_refusal = "correlate-failed"
            return None

        _min_val, peak, _min_loc, raw_loc = cv2.minMaxLoc(response)
        # ``minMaxLoc`` is typed as returning a plain sequence; everything
        # downstream indexes it as a point, so it becomes one here rather than
        # at four call sites.
        peak_loc: tuple[int, int] = (int(raw_loc[0]), int(raw_loc[1]))
        if peak < config.min_correlation:
            self._stats = replace(
                self._stats, refused_correlation=self._stats.refused_correlation + 1
            )
            self._last_refusal = f"correlation:{peak:.2f}"
            return None
        rival = self._rival_peak(response, peak_loc, anchor.template_side_px)
        margin = peak - rival
        if margin < config.peak_margin:
            # Two peaks that cannot be told apart. Being near the prediction is
            # the search bound, not a tie-break - so this abstains and lets the
            # global path decide which one is the arrow.
            self._stats = replace(
                self._stats, refused_ambiguous=self._stats.refused_ambiguous + 1
            )
            self._last_refusal = f"ambiguous:{margin:.2f}"
            return None

        # Back to frame pixels: the response is indexed in downscaled window
        # coordinates whose origin is the window's top-left corner.
        inv_scale = 1.0 / anchor.scale if anchor.scale > 0 else 1.0
        centre_x = (peak_loc[0] + template.shape[1] / 2.0) * inv_scale + origin[0]
        centre_y = (peak_loc[1] + template.shape[0] / 2.0) * inv_scale + origin[1]
        centroid = (centre_x, centre_y)

        previous = self._centroid_px or anchor.centroid_px
        step_px = math.dist(previous, centroid)
        if step_px / since_s > config.max_speed_px_s:
            self._stats = replace(self._stats, refused_speed=self._stats.refused_speed + 1)
            self._last_refusal = f"speed:{step_px / since_s:.0f}px/s"
            return None

        contrast = self._contrast(patch, peak_loc, template.shape)
        if contrast < config.min_contrast:
            self._stats = replace(
                self._stats, refused_contrast=self._stats.refused_contrast + 1
            )
            self._last_refusal = f"contrast:{contrast:.2f}"
            return None

        rotation_delta = self._refine_rotation(patch, peak_loc, anchor, since_s=since_s)
        rotated = _wrap(self._rotation_deg + rotation_delta)
        if abs(rotated) > config.max_rotation_deg:
            # The accumulated turn is larger than anything a bridge may claim
            # without the structural detector confirming it. Refuse the step
            # rather than carrying a heading nothing has validated: a wrong
            # heading is worse than no heading, because the controller acts on
            # one and coasts through the other.
            self._stats = replace(self._stats, expired_drift=self._stats.expired_drift + 1)
            self._provenance = EvidenceProvenance.STALE
            self._last_refusal = f"rotation:{rotated:+.0f}deg"
            return None
        self._rotation_deg = rotated

        age_s = max(0.0, now_s - anchor.anchored_at_s)
        self._drift_px += step_px
        if self._drift_px > config.max_drift_px:
            # The step that crossed the budget is still refused, not adopted.
            self._stats = replace(self._stats, expired_drift=self._stats.expired_drift + 1)
            self._provenance = EvidenceProvenance.STALE
            self._last_refusal = f"drift:{self._drift_px:.0f}px"
            return None

        self._velocity_px_s = (
            (centroid[0] - previous[0]) / since_s,
            (centroid[1] - previous[1]) / since_s,
        )
        self._centroid_px = centroid
        self._last_measured_s = now_s
        self._bridged_frames += 1
        self._stats = replace(self._stats, bridged=self._stats.bridged + 1)
        self._provenance = EvidenceProvenance.BRIDGED
        return BridgeMeasurement(
            provenance=EvidenceProvenance.BRIDGED,
            centroid_px=centroid,
            bbox_px=self._bbox_for(centroid, anchor),
            axis_unit_xy=_rotate_unit(anchor.axis_unit_xy, self._rotation_deg),
            rotation_deg=self._rotation_deg,
            correlation=float(peak),
            peak_margin=float(margin),
            contrast=float(contrast),
            confidence=self._decayed_confidence(anchor, age_s, peak),
            age_s=age_s,
            drift_px=self._drift_px,
            bridged_frames=self._bridged_frames,
        )

    def _predict(
        self, anchor: _Anchor, *, since_s: float, now_s: float, age_s: float
    ) -> BridgeMeasurement | None:
        """Constant velocity, no measurement, and a much shorter horizon.

        This state exists so the caller can *say* it has no evidence rather
        than reporting a stale measurement as a fresh one. It is deliberately
        the weakest thing the bridge can produce: no pixels were compared, so
        its confidence decays from a lower start and expires four times sooner
        than a measured bridge.
        """
        del now_s
        if since_s > self._config.max_predict_s:
            self._provenance = EvidenceProvenance.STALE
            self._last_refusal = self._last_refusal or f"predict-horizon:{since_s * 1000:.0f}ms"
            return None
        previous = self._centroid_px or anchor.centroid_px
        centroid = (
            previous[0] + self._velocity_px_s[0] * since_s,
            previous[1] + self._velocity_px_s[1] * since_s,
        )
        confidence = self._decayed_confidence(anchor, age_s, 0.0) * 0.5
        if confidence < self._config.min_confidence:
            self._provenance = EvidenceProvenance.STALE
            return None
        self._stats = replace(self._stats, predicted=self._stats.predicted + 1)
        self._provenance = EvidenceProvenance.PREDICTED
        return BridgeMeasurement(
            provenance=EvidenceProvenance.PREDICTED,
            centroid_px=centroid,
            bbox_px=self._bbox_for(centroid, anchor),
            axis_unit_xy=_rotate_unit(anchor.axis_unit_xy, self._rotation_deg),
            rotation_deg=self._rotation_deg,
            correlation=0.0,
            peak_margin=0.0,
            contrast=0.0,
            confidence=confidence,
            age_s=age_s,
            drift_px=self._drift_px,
            bridged_frames=self._bridged_frames,
        )

    # -- helpers ----------------------------------------------------------
    def _predicted_centroid(self, since_s: float) -> tuple[float, float]:
        base = self._centroid_px
        if base is None:
            assert self._anchor is not None
            base = self._anchor.centroid_px
        return (
            base[0] + self._velocity_px_s[0] * since_s,
            base[1] + self._velocity_px_s[1] * since_s,
        )

    def _decayed_confidence(self, anchor: _Anchor, age_s: float, peak: float) -> float:
        """Monotonic-time decay, floored at zero, scaled by the correlation.

        Two independent reasons to trust a bridged frame less: it has been a
        while since anything structural confirmed the identity (the half-life),
        and this particular correlation was weak (the peak). Both are applied,
        because a strong correlation onto a two-second-old template is still a
        claim about a track nothing has validated.
        """
        half_life = max(1e-3, self._config.confidence_half_life_s)
        decay = float(0.5 ** (age_s / half_life))
        quality = 1.0 if peak <= 0.0 else min(1.0, max(0.0, peak))
        value = float(anchor.confidence) * decay * (0.55 + 0.45 * quality)
        return round(max(0.0, min(1.0, value)), 4)

    def _bbox_for(
        self, centroid: tuple[float, float], anchor: _Anchor
    ) -> tuple[int, int, int, int]:
        """The anchored extent, moved. A bridge never re-measures size."""
        half_w = anchor.half_px[0] / (1.0 + self._config.template_pad_fraction * 2.0)
        half_h = anchor.half_px[1] / (1.0 + self._config.template_pad_fraction * 2.0)
        return (
            round(centroid[0] - half_w),
            round(centroid[1] - half_h),
            max(1, round(half_w * 2.0)),
            max(1, round(half_h * 2.0)),
        )

    def _rival_peak(
        self,
        response: NDArray[Any],
        peak_loc: tuple[int, int],
        template_side_px: int,
    ) -> float:
        """Best response outside a suppression box around the winner.

        Masking rather than sorting: the second-best *pixel* is almost always
        the winner's own neighbour, which says nothing about ambiguity. What
        matters is whether a genuinely separate location scores nearly as well.
        """
        radius = max(2, int(template_side_px * self._config.suppression_fraction))
        masked = response.copy()
        x0 = max(0, peak_loc[0] - radius)
        y0 = max(0, peak_loc[1] - radius)
        x1 = min(masked.shape[1], peak_loc[0] + radius + 1)
        y1 = min(masked.shape[0], peak_loc[1] + radius + 1)
        masked[y0:y1, x0:x1] = -1.0
        return max(-1.0, float(masked.max()))

    @staticmethod
    def _contrast(
        patch: NDArray[Any], peak_loc: tuple[int, int], template_shape: tuple[int, ...]
    ) -> float:
        """Matched-patch luminance over the luminance of the ring around it.

        Independent of correlation on purpose. A correlation peak says "this
        looks like what we stored"; this says "and it is still an object".
        """
        height, width = template_shape[0], template_shape[1]
        y0, x0 = peak_loc[1], peak_loc[0]
        inner = patch[y0 : y0 + height, x0 : x0 + width]
        if inner.size == 0:
            return 0.0
        pad_y = max(2, height // 3)
        pad_x = max(2, width // 3)
        ry0 = max(0, y0 - pad_y)
        rx0 = max(0, x0 - pad_x)
        ry1 = min(patch.shape[0], y0 + height + pad_y)
        rx1 = min(patch.shape[1], x0 + width + pad_x)
        outer = patch[ry0:ry1, rx0:rx1]
        if outer.size <= inner.size:
            return 1.0
        inner_sum = float(np.sum(inner, dtype=np.float64))
        outer_sum = float(np.sum(outer, dtype=np.float64))
        ring_sum = outer_sum - inner_sum
        ring_count = outer.size - inner.size
        if ring_count <= 0:
            return 1.0
        ring_mean = ring_sum / ring_count
        inner_mean = inner_sum / inner.size
        if ring_mean <= 1e-6:
            return 1.0 if inner_mean <= 1e-6 else 99.0
        return float(inner_mean / ring_mean)

    def _refine_rotation(
        self,
        patch: NDArray[Any],
        peak_loc: tuple[int, int],
        anchor: _Anchor,
        *,
        since_s: float,
    ) -> float:
        """In-plane rotation of the matched patch against the anchored template.

        Only at the winning translation, and only over a bank whose span scales
        with elapsed time - so the angular speed the bridge can follow is the
        same at 30 Hz and at 90, and the *cost* is a handful of correlations on
        a 44-pixel patch rather than a second full search per angle.

        The sign is the same as :func:`prospector_engine.arrow.heading_deg`:
        positive is clockwise on screen. It is pinned by a test rather than by
        argument, because getting it backwards would steer the character away
        from the treasure and would look exactly like a working bridge.
        """
        config = self._config
        span = min(
            config.rotation_span_max_deg,
            max(config.rotation_span_min_deg, config.rotation_rate_deg_s * since_s),
        )
        template = anchor.template
        height, width = template.shape[0], template.shape[1]
        y0, x0 = peak_loc[1], peak_loc[0]
        window = patch[y0 : y0 + height, x0 : x0 + width]
        if window.shape[0] != height or window.shape[1] != width:
            return 0.0
        target = np.asarray(window, dtype=np.float32)
        centre = (width / 2.0 - 0.5, height / 2.0 - 0.5)
        steps = max(1, int(span / max(1e-6, config.rotation_step_deg)))
        best_angle = 0.0
        # The unrotated template is the incumbent, and it has to be *beaten* by
        # a margin rather than merely tied. On an occluded patch the bank's
        # best angle beats zero by a rounding error whose sign is random, and
        # those errors accumulate across a bridged run into tens of degrees.
        zero_score = _ncc(target, template)
        best_score = zero_score + config.rotation_min_gain
        for index in range(1, steps + 1):
            for angle in (index * config.rotation_step_deg, -index * config.rotation_step_deg):
                if abs(angle) > span:
                    continue
                # ``getRotationMatrix2D`` takes a counter-clockwise angle in a
                # y-down image, so rotating the template by ``-angle`` produces
                # the template as it would look after turning ``+angle``
                # clockwise on screen - which is the positive sense of
                # ``heading_deg``.
                matrix = cv2.getRotationMatrix2D(centre, -angle, 1.0)
                rotated = cv2.warpAffine(
                    template,
                    matrix,
                    (width, height),
                    flags=cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_REPLICATE,
                )
                score = _ncc(target, rotated)
                if score > best_score:
                    best_score, best_angle = score, angle
        return best_angle

    @staticmethod
    def _crop_gray(
        frame: CapturedFrame,
        left: float,
        top: float,
        right: float,
        bottom: float,
        *,
        origin_out: bool = False,
    ) -> Any:
        """A clipped greyscale crop, or ``None`` when nothing usable remains."""
        bgr = frame.bgr
        height, width = bgr.shape[0], bgr.shape[1]
        x0 = max(0, math.floor(left))
        y0 = max(0, math.floor(top))
        x1 = min(width, math.ceil(right))
        y1 = min(height, math.ceil(bottom))
        if x1 - x0 < 4 or y1 - y0 < 4:
            return None
        region = bgr[y0:y1, x0:x1]
        gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
        if origin_out:
            return (gray, (float(x0), float(y0)))
        return gray


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------


def _wrap(degrees: float) -> float:
    wrapped = (degrees + 180.0) % 360.0 - 180.0
    return 180.0 if wrapped == -180.0 else wrapped


def _ncc(a: NDArray[Any], b: NDArray[Any]) -> float:
    """Zero-mean normalised cross-correlation of two same-shaped patches."""
    a_centred = a - float(np.mean(a))
    b_centred = b - float(np.mean(b))
    denominator = float(np.linalg.norm(a_centred) * np.linalg.norm(b_centred))
    if denominator <= 1e-9:
        return 0.0
    return float(np.sum(a_centred * b_centred) / denominator)


def _rotate_unit(
    axis: tuple[float, float] | None, rotation_deg: float
) -> tuple[float, float] | None:
    """Turn a unit axis clockwise on screen by ``rotation_deg``.

    Screen space is y-down, so a clockwise turn is the *positive* mathematical
    rotation of ``(x, y)`` - which is the opposite of the intuition a reader
    brings from a y-up basis, and is why this is a named function with a test
    rather than two lines inlined at the call site.
    """
    if axis is None:
        return None
    radians = math.radians(rotation_deg)
    cos_a, sin_a = math.cos(radians), math.sin(radians)
    x, y = axis
    turned = (x * cos_a - y * sin_a, x * sin_a + y * cos_a)
    length = math.hypot(*turned)
    if length <= 1e-9:
        return None
    return (turned[0] / length, turned[1] / length)
