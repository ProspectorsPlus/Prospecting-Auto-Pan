"""Arrow profiles, segmentation, tracking, direction cues, and arrival detection.

Nothing in this module is enabled by default. Every profile and every detector
ships as ``EvidenceStatus.PENDING`` until its experiment (E-PROF, E-DIR-IDEAL,
E-DIR-E2E, E-ARRIVE) has been run and frozen, because a detector that merely
looks plausible on one screenshot is not evidence (plan 7).

The candidates live here together so they can be compared on the same frames.
Selection is an evaluation result, not a code edit: no candidate is marked
"the" strategy in source.

Angle convention used throughout: degrees measured from **screen up**,
positive **clockwise** (to the right), wrapped to (-180, 180].
"""

from __future__ import annotations

import contextlib
import json
import math
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from prospector_engine.contracts import (
    ArrivalObservation,
    ArrowCandidateRecord,
    ArrowObservation,
    CapturedFrame,
    DirectionObservation,
    EvidenceStatus,
    Provenance,
)

__all__ = [
    "DIRECTION_STRATEGIES",
    "ArrivalConfig",
    "ArrivalDetector",
    "ArrowProfile",
    "ArrowSegmenter",
    "ProfileAuthority",
    "ProfileLibrary",
    "PromptReading",
    "WaterConfig",
    "WaterGuard",
    "WaterReading",
    "angle_between_deg",
    "heading_deg",
    "load_profiles",
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


def angle_between_deg(a_deg: float, b_deg: float) -> float:
    """Signed turn from ``a`` to ``b``, wrapped."""
    return wrap_deg(b_deg - a_deg)


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArrowProfile:
    """A per-map colour/contrast model with its own evidence status.

    ``status`` is what production reads. A profile whose E-PROF gate has not
    run stays ``PENDING`` and cannot be selected automatically; the user may
    still choose it explicitly for Shadow observation and recording.
    """

    profile_id: str
    display_name: str
    #: Inclusive BGR bounds, or an HSV/Lab rule name plus parameters.
    rule: Literal["bgr_range", "hsv_range", "channel_relation"]
    parameters: dict[str, Any]
    min_area_px: int
    max_area_px: int
    min_aspect: float
    max_aspect: float
    status: EvidenceStatus
    provenance: Provenance
    supported_client_size_px: tuple[int, int] = (1280, 720)
    evaluation_spec_id: str | None = None
    #: Canonical-pixel rectangles the arrow is never looked for: fixed HUD
    #: bands whose yellow bars and banners were measured to acquire as
    #: arrows on a live scene. A profile that lists none searches everywhere.
    exclusion_regions_px: tuple[tuple[int, int, int, int], ...] = ()
    #: Whether the runtime classifier may consider this profile at all.
    runtime_selectable: bool = True

    @property
    def selectable_automatically(self) -> bool:
        """Whether the *runtime* classifier may consider this profile.

        This is a runtime check, not an offline gate. Which map the user has
        equipped is decided from the frames in front of us - temporal agreement
        plus a clear score margin over consecutive frames - and that evidence
        is available whether or not a held-out E-PROF corpus exists for the
        profile. ``status`` still records the offline evidence honestly and is
        what the dashboard labels the lock with (D-036).

        A profile opts out by declaring ``runtime_selectable: false`` in the
        bundled JSON - for a profile kept only as a comparison baseline.
        """
        return self.runtime_selectable


class ProfileLibrary:
    """Bundled profiles, read with ``importlib.resources``.

    Never resolved relative to the current working directory or to a reference
    worktree, so a packaged build loads the same data a source run does
    (plan 11.4).
    """

    def __init__(self, profiles: dict[str, ArrowProfile]) -> None:
        self._profiles = dict(profiles)

    def __len__(self) -> int:
        return len(self._profiles)

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._profiles))

    def get(self, profile_id: str) -> ArrowProfile | None:
        return self._profiles.get(profile_id)

    def validated(self) -> tuple[ArrowProfile, ...]:
        """Profiles whose *offline* evidence gate has passed. Reporting only."""
        return tuple(p for p in self._profiles.values() if p.status is EvidenceStatus.VALIDATED)

    def selectable(self) -> tuple[ArrowProfile, ...]:
        """Profiles the runtime classifier is allowed to choose between."""
        return tuple(p for p in self.all() if p.selectable_automatically)

    def all(self) -> tuple[ArrowProfile, ...]:
        return tuple(self._profiles[key] for key in self.ids())


class ProfileAuthority:
    """The single source of truth for which arrow profile is active.

    Three problems this exists to make impossible, all of them observed:

    * The selector showed ``generic_saturated_v0`` while the pipeline was
      running ``yellow_map_v0``. The UI now renders *from* this object, so it
      cannot start out of step.
    * A profile was recovered by splitting a display label on a space. IDs here
      are stable strings and labels are derived from them, never the reverse.
    * A swap landed in the middle of an in-flight frame. A request is staged
      and applied at a frame boundary by :meth:`apply_pending`, which bumps
      ``revision`` exactly once; every key, track, and ROI is rebuilt from it.
    """

    def __init__(
        self,
        library: ProfileLibrary,
        active_id: str,
        *,
        on_change: Callable[[ArrowProfile, int], None] | None = None,
    ) -> None:
        profile = library.get(active_id)
        if profile is None:
            available = library.all()
            if not available:
                raise ValueError("the profile library is empty")
            profile = available[0]
        self._library = library
        self._active = profile
        self._pending: ArrowProfile | None = None
        self._revision = 1
        self._on_change = on_change
        self._lock = threading.Lock()

    @property
    def library(self) -> ProfileLibrary:
        return self._library

    @property
    def active(self) -> ArrowProfile:
        with self._lock:
            return self._active

    @property
    def active_id(self) -> str:
        return self.active.profile_id

    @property
    def revision(self) -> int:
        """Bumped once per applied swap. Part of every dashboard packet key."""
        with self._lock:
            return self._revision

    @property
    def pending_id(self) -> str | None:
        with self._lock:
            return None if self._pending is None else self._pending.profile_id

    def choices(self) -> tuple[tuple[str, str], ...]:
        """``(stable_id, display_label)`` pairs, for a selector to render.

        The label is built here from the id and status so no caller ever has to
        parse one back into the other.
        """
        return tuple(
            (profile.profile_id, f"{profile.display_name} - {profile.status.value}")
            for profile in self._library.all()
        )

    def label_for(self, profile_id: str) -> str:
        for stable_id, label in self.choices():
            if stable_id == profile_id:
                return label
        return profile_id

    def request(self, profile_id: str) -> bool:
        """Stage a swap by **stable id**. Returns False for an unknown id."""
        profile = self._library.get(profile_id)
        if profile is None:
            return False
        with self._lock:
            if profile.profile_id == self._active.profile_id:
                self._pending = None
                return True
            self._pending = profile
        return True

    def apply_pending(self) -> ArrowProfile | None:
        """Apply a staged swap at a frame boundary. Returns the new profile.

        Called by the perception pipeline before it touches a frame, so a
        profile never changes underneath an observation that is half built.
        """
        with self._lock:
            profile = self._pending
            if profile is None:
                return None
            self._pending = None
            self._active = profile
            self._revision += 1
            revision = self._revision
        if self._on_change is not None:
            with contextlib.suppress(Exception):
                self._on_change(profile, revision)
        return profile


def load_profiles(raw: str | None = None) -> ProfileLibrary:
    if raw is None:
        resource = resources.files("prospector_engine") / "profiles" / "arrow_profiles.json"
        raw = resource.read_text(encoding="utf-8")
    document = json.loads(raw)
    profiles: dict[str, ArrowProfile] = {}
    for entry in document["profiles"]:
        provenance = Provenance(
            status=EvidenceStatus(entry["provenance"]["status"]),
            source=entry["provenance"]["source"],
            note=entry["provenance"].get("note", ""),
        )
        profiles[entry["profile_id"]] = ArrowProfile(
            profile_id=entry["profile_id"],
            display_name=entry["display_name"],
            rule=entry["rule"],
            parameters=entry["parameters"],
            min_area_px=entry["min_area_px"],
            max_area_px=entry["max_area_px"],
            min_aspect=entry["min_aspect"],
            max_aspect=entry["max_aspect"],
            status=EvidenceStatus(entry["status"]),
            provenance=provenance,
            supported_client_size_px=(
                int(entry["supported_client_size_px"][0]),
                int(entry["supported_client_size_px"][1]),
            ),
            evaluation_spec_id=entry.get("evaluation_spec_id"),
            exclusion_regions_px=tuple(
                (int(r[0]), int(r[1]), int(r[2]), int(r[3]))
                for r in entry.get("exclusion_regions_px", ())
            ),
            runtime_selectable=bool(entry.get("runtime_selectable", True)),
        )
    return ProfileLibrary(profiles)


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArrowCandidate:
    label: int
    area_px: int
    bbox_px: tuple[int, int, int, int]
    centroid_px: tuple[float, float]
    mask: NDArray[Any]
    touches_exclusion: bool
    clipped: bool


@dataclass(frozen=True)
class SegmenterConfig:
    """Bounds that make an implausible blob abstain instead of being accepted."""

    ambiguity_margin: float = 0.25
    exclusion_regions_px: tuple[tuple[int, int, int, int], ...] = ()
    blur_ksize: int = 3
    morph_ksize: int = 3
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="TREASURE_NAVIGATION_PLAN.md section 8",
            note="E-PROF has not been run; these are starting values only",
        )
    )


class ArrowSegmenter:
    """Colour/contrast candidate extraction plus geometric plausibility.

    Produces candidates and an explicit abstention when two candidates are
    within the ambiguity margin of each other - guessing between them is the
    failure mode this exists to prevent (plan 7.3).
    """

    def __init__(self, profile: ArrowProfile, config: SegmenterConfig | None = None) -> None:
        self._profile = profile
        self._config = config or SegmenterConfig()

    @property
    def profile(self) -> ArrowProfile:
        return self._profile

    def mask_for(
        self, frame: CapturedFrame, roi_px: tuple[int, int, int, int] | None = None
    ) -> NDArray[Any]:
        """Build the candidate mask, optionally over a region of interest only.

        Restricting to an ROI around a confident track is the single largest
        saving in the perception budget: a quarter-area ROI costs roughly a
        quarter of the blur, threshold, morphology, and labelling work. The
        caller is responsible for periodically going back to the full frame so
        a track cannot quietly follow the wrong thing forever.
        """
        import cv2

        bgr: NDArray[Any] = np.asarray(frame.bgr)
        if roi_px is not None:
            x, y, width, height = roi_px
            bgr = np.ascontiguousarray(bgr[y : y + height, x : x + width])
        if self._config.blur_ksize > 1:
            k = self._config.blur_ksize | 1
            bgr = np.asarray(cv2.GaussianBlur(bgr, (k, k), 0))
        rule = self._profile.rule
        parameters = self._profile.parameters
        if rule == "bgr_range":
            lower_bgr = np.array(parameters["lower_bgr"], dtype=np.uint8)
            upper_bgr = np.array(parameters["upper_bgr"], dtype=np.uint8)
            mask = cv2.inRange(bgr, lower_bgr, upper_bgr)
        elif rule == "hsv_range":
            hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
            lower_hsv = np.array(parameters["lower_hsv"], dtype=np.uint8)
            upper_hsv = np.array(parameters["upper_hsv"], dtype=np.uint8)
            mask = cv2.inRange(hsv, lower_hsv, upper_hsv)
        elif rule == "channel_relation":
            # e.g. the yellow map's R ~= G with suppressed B. One candidate
            # among several, never assumed to generalise (plan 2.2).
            b, g, r = (bgr[:, :, i].astype(np.int16) for i in range(3))
            close = np.abs(r - g) <= int(parameters["max_rg_delta"])
            bright = np.minimum(r, g) >= int(parameters["min_rg"])
            suppressed = b <= np.minimum(r, g) - int(parameters["min_blue_gap"])
            mask = np.asarray((close & bright & suppressed), dtype=np.uint8) * 255
        else:  # pragma: no cover - guarded by the profile schema
            raise ValueError(f"unknown profile rule {rule!r}")

        if roi_px is None:
            for x, y, width, height in self._config.exclusion_regions_px:
                mask[y : y + height, x : x + width] = 0
        else:
            offset_x, offset_y = roi_px[0], roi_px[1]
            for x, y, width, height in self._config.exclusion_regions_px:
                local_x, local_y = x - offset_x, y - offset_y
                mask[max(0, local_y) : local_y + height, max(0, local_x) : local_x + width] = 0
        if self._config.morph_ksize > 1:
            k = self._config.morph_ksize | 1
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        result: NDArray[Any] = mask.astype(np.uint8)
        return result

    def candidates(
        self, frame: CapturedFrame, roi_px: tuple[int, int, int, int] | None = None
    ) -> list[ArrowCandidate]:
        import cv2

        mask = self.mask_for(frame, roi_px)
        count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
        height, width = mask.shape[:2]
        offset_x, offset_y = (roi_px[0], roi_px[1]) if roi_px else (0, 0)
        full_w, full_h = frame.canonical_size_px
        found: list[ArrowCandidate] = []
        for label in range(1, count):
            area = int(stats[label, cv2.CC_STAT_AREA])
            if not self._profile.min_area_px <= area <= self._profile.max_area_px:
                continue
            x = int(stats[label, cv2.CC_STAT_LEFT])
            y = int(stats[label, cv2.CC_STAT_TOP])
            w = int(stats[label, cv2.CC_STAT_WIDTH])
            h = int(stats[label, cv2.CC_STAT_HEIGHT])
            aspect = w / h if h else 0.0
            if not self._profile.min_aspect <= aspect <= self._profile.max_aspect:
                continue
            # Clipping is judged against the *full* frame: touching an ROI
            # edge is expected and says nothing about the arrow.
            global_x, global_y = x + offset_x, y + offset_y
            clipped = (
                global_x <= 0
                or global_y <= 0
                or global_x + w >= full_w
                or global_y + h >= full_h
            )
            component = (labels == label).astype(np.uint8) * 255
            if roi_px is not None:
                placed = np.zeros((full_h, full_w), dtype=np.uint8)
                placed[offset_y : offset_y + height, offset_x : offset_x + width] = component
                component = placed
            found.append(
                ArrowCandidate(
                    label=label,
                    area_px=area,
                    bbox_px=(global_x, global_y, w, h),
                    centroid_px=(
                        float(centroids[label][0]) + offset_x,
                        float(centroids[label][1]) + offset_y,
                    ),
                    mask=component,
                    touches_exclusion=False,
                    clipped=clipped,
                )
            )
        found.sort(key=lambda candidate: candidate.area_px, reverse=True)
        return found

    def contour_of(self, candidate: ArrowCandidate) -> tuple[tuple[int, int], ...]:
        """The accepted blob's outline, decimated for drawing.

        The overlay draws the real detected shape rather than a bounding box,
        because a box cannot show *why* a candidate was accepted or what the
        mask actually latched onto.
        """
        import cv2

        contours, _hierarchy = cv2.findContours(
            candidate.mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        if not contours:
            return ()
        largest = max(contours, key=cv2.contourArea)
        epsilon = 0.005 * cv2.arcLength(largest, True)
        simplified = cv2.approxPolyDP(largest, max(1.0, epsilon), True)
        return tuple((int(point[0][0]), int(point[0][1])) for point in simplified)

    def observe_detailed(
        self, frame: CapturedFrame, roi_px: tuple[int, int, int, int] | None = None
    ) -> tuple[ArrowObservation, tuple[ArrowCandidateRecord, ...], tuple[tuple[int, int], ...]]:
        """Acquisition plus the record of everything considered and rejected.

        "No arrow" and "four arrows, all plausible" look identical in a boolean
        but need opposite fixes, so the rejected candidates and their reasons
        come out alongside the decision - derived from the *same* segmentation
        pass, not a second one.
        """
        observation, found = self._analyze(frame, roi_px)
        records: list[ArrowCandidateRecord] = []
        contour: tuple[tuple[int, int], ...] = ()
        accepted_bbox = observation.bbox_px if observation.valid else None
        for candidate in found:
            is_accepted = accepted_bbox is not None and candidate.bbox_px == accepted_bbox
            records.append(
                ArrowCandidateRecord(
                    label=candidate.label,
                    area_px=candidate.area_px,
                    bbox_px=candidate.bbox_px,
                    centroid_px=candidate.centroid_px,
                    score=_confidence_for(candidate, self._profile),
                    accepted=is_accepted,
                    rejected_reason=(
                        None
                        if is_accepted
                        else (observation.abstain_reason or "not the best candidate")
                    ),
                )
            )
            if is_accepted:
                contour = self.contour_of(candidate)
        return (observation, tuple(records), contour)

    def observe(
        self, frame: CapturedFrame, roi_px: tuple[int, int, int, int] | None = None
    ) -> ArrowObservation:
        """Strict global acquisition with an explicit ambiguity abstention."""
        return self._analyze(frame, roi_px)[0]

    def _analyze(
        self, frame: CapturedFrame, roi_px: tuple[int, int, int, int] | None
    ) -> tuple[ArrowObservation, list[ArrowCandidate]]:
        """One segmentation pass, shared by every caller."""
        if frame.capture_error is not None:
            return (
                _abstain(self._profile.profile_id, f"capture-error:{frame.capture_error}"),
                [],
            )
        if not frame.geometry.valid:
            return (_abstain(self._profile.profile_id, "viewport-invalid"), [])
        if tuple(frame.canonical_size_px) != tuple(self._profile.supported_client_size_px):
            return (_abstain(self._profile.profile_id, "unsupported-viewport-size"), [])

        found = self.candidates(frame, roi_px)
        if not found:
            return (_abstain(self._profile.profile_id, "no-candidate"), found)
        best = found[0]
        if len(found) > 1:
            runner_up = found[1]
            ratio = runner_up.area_px / best.area_px if best.area_px else 1.0
            if ratio > 1.0 - self._config.ambiguity_margin:
                return (_abstain(self._profile.profile_id, "ambiguous-candidates"), found)
        if best.clipped:
            return (_abstain(self._profile.profile_id, "candidate-clipped"), found)

        axis, tip, tail = _principal_axis_and_tip(best)
        return (
            ArrowObservation(
                profile_id=self._profile.profile_id,
                track_id=None,
                bbox_px=best.bbox_px,
                centroid_px=best.centroid_px,
                tip_px=tip,
                axis_unit_xy=axis,
                confidence=_confidence_for(best, self._profile),
                valid=True,
                abstain_reason=None,
                tail_px=tail,
            ),
            found,
        )


def _abstain(profile_id: str | None, reason: str) -> ArrowObservation:
    return ArrowObservation(
        profile_id=profile_id,
        track_id=None,
        bbox_px=None,
        centroid_px=None,
        tip_px=None,
        axis_unit_xy=None,
        confidence=0.0,
        valid=False,
        abstain_reason=reason,
    )


def _confidence_for(candidate: ArrowCandidate, profile: ArrowProfile) -> float:
    """Confidence falls with clipping, exclusion contact, and implausible size."""
    span = max(1, profile.max_area_px - profile.min_area_px)
    size_fit = 1.0 - abs(candidate.area_px - (profile.min_area_px + span / 2)) / span
    confidence = max(0.0, min(1.0, 0.5 + 0.5 * size_fit))
    if candidate.clipped:
        confidence *= 0.4
    if candidate.touches_exclusion:
        confidence *= 0.6
    return round(confidence, 4)


def _principal_axis_and_tip(
    candidate: ArrowCandidate,
) -> tuple[tuple[float, float] | None, tuple[float, float] | None, tuple[float, float] | None]:
    """PCA axis plus the farthest mask point along it, as a tip estimate.

    The sign of a PCA axis is arbitrary, so it is resolved by taking whichever
    end carries more mass *beyond* the centroid - the arrowhead. E-DIR-IDEAL
    decides whether this beats the other candidates; nothing here presumes it.
    """
    points = np.column_stack(np.nonzero(candidate.mask))  # (row, col)
    if points.shape[0] < 8:
        return None, None, None
    xy = np.column_stack((points[:, 1], points[:, 0])).astype(np.float64)
    mean = xy.mean(axis=0)
    centred = xy - mean
    _u, _s, vh = np.linalg.svd(centred, full_matrices=False)
    axis = vh[0]
    projections = centred @ axis
    positive = projections[projections > 0]
    negative = projections[projections < 0]
    if positive.size == 0 or negative.size == 0:
        return None, None, None
    # The arrowhead end is narrower but reaches farther; pick the longer reach.
    if abs(positive.max()) < abs(negative.min()):
        axis = -axis
        projections = -projections
    norm = float(np.hypot(axis[0], axis[1]))
    if norm < 1e-9:
        return None, None, None
    unit = (float(axis[0] / norm), float(axis[1] / norm))
    tip_index = int(np.argmax(projections))
    tail_index = int(np.argmin(projections))
    tip = (float(xy[tip_index][0]), float(xy[tip_index][1]))
    tail = (float(xy[tail_index][0]), float(xy[tail_index][1]))
    return unit, tip, tail


# ---------------------------------------------------------------------------
# Tracking
# ---------------------------------------------------------------------------
#
# ``ArrowTracker`` used to live here: a constant-velocity track with its own
# identity horizon, meant to prioritise the next search. It was retired in
# D-094 because it had stopped being a second opinion and become a third
# wheel. ``ArrowDetector`` grew its own internal ``_Track`` - association
# gates, streaks, switching, reacquisition, the resume rule - and owns
# identity outright; ``PerceptionPipeline`` constructed an ``ArrowTracker``,
# reset it on every profile and geometry change, and never once called
# ``update`` on it. Dead code that looks like a tracker is worse than no
# tracker, because the next person to read the pipeline has to work out which
# of the two is authoritative.
#
# What the pipeline holds instead is ``prospector_engine.temporal
# .TemporalBridge``: explicitly *not* an identity authority - it never
# acquires and never switches - but a bounded local measurement for the frames
# the detector could not segment. One identity owner, one continuity
# mechanism, and the difference between them is a type
# (``EvidenceProvenance``) rather than a convention.


# ---------------------------------------------------------------------------
# Direction cues
# ---------------------------------------------------------------------------


def _direction_abstain(cue_id: str, reason: str) -> DirectionObservation:
    return DirectionObservation(
        error_deg=None,
        confidence=0.0,
        cue_id=cue_id,
        cue_disagreement_deg=None,
        valid=False,
        abstain_reason=reason,
    )


def centroid_ray(
    arrow: ArrowObservation, anchor_px: tuple[float, float], forward_deg: float | None
) -> DirectionObservation:
    """Player-to-centroid ray."""
    if not arrow.valid or arrow.centroid_px is None or forward_deg is None:
        return _direction_abstain("centroid_ray", "missing arrow or forward reference")
    bearing = heading_deg(
        (arrow.centroid_px[0] - anchor_px[0], arrow.centroid_px[1] - anchor_px[1])
    )
    if bearing is None:
        return _direction_abstain("centroid_ray", "degenerate ray")
    return DirectionObservation(
        error_deg=angle_between_deg(forward_deg, bearing),
        confidence=arrow.confidence,
        cue_id="centroid_ray",
        cue_disagreement_deg=None,
        valid=True,
    )


def tip_ray(
    arrow: ArrowObservation, anchor_px: tuple[float, float], forward_deg: float | None
) -> DirectionObservation:
    """Player-to-detected-tip ray."""
    if not arrow.valid or arrow.tip_px is None or forward_deg is None:
        return _direction_abstain("tip_ray", "missing tip or forward reference")
    bearing = heading_deg((arrow.tip_px[0] - anchor_px[0], arrow.tip_px[1] - anchor_px[1]))
    if bearing is None:
        return _direction_abstain("tip_ray", "degenerate ray")
    return DirectionObservation(
        error_deg=angle_between_deg(forward_deg, bearing),
        confidence=arrow.confidence,
        cue_id="tip_ray",
        cue_disagreement_deg=None,
        valid=True,
    )


def pca_axis(
    arrow: ArrowObservation, anchor_px: tuple[float, float], forward_deg: float | None
) -> DirectionObservation:
    """Arrow pose rather than arrow position."""
    del anchor_px
    if not arrow.valid or arrow.axis_unit_xy is None or forward_deg is None:
        return _direction_abstain("pca_axis", "missing axis or forward reference")
    bearing = heading_deg(arrow.axis_unit_xy)
    if bearing is None:
        return _direction_abstain("pca_axis", "degenerate axis")
    return DirectionObservation(
        error_deg=angle_between_deg(forward_deg, bearing),
        confidence=arrow.confidence * 0.9,
        cue_id="pca_axis",
        cue_disagreement_deg=None,
        valid=True,
    )


def position_pose_fusion(
    arrow: ArrowObservation, anchor_px: tuple[float, float], forward_deg: float | None
) -> DirectionObservation:
    """Fuse position and pose, and abstain when they materially disagree.

    Disagreement is a real signal that one of the two is wrong; averaging it
    away would produce a confident wrong answer, which is the one outcome the
    controller cannot tolerate (plan 7.3).
    """
    position = tip_ray(arrow, anchor_px, forward_deg)
    if not position.valid:
        position = centroid_ray(arrow, anchor_px, forward_deg)
    pose = pca_axis(arrow, anchor_px, forward_deg)
    if not position.valid or not pose.valid:
        return _direction_abstain("fusion", "component cue abstained")
    assert position.error_deg is not None and pose.error_deg is not None
    disagreement = abs(angle_between_deg(position.error_deg, pose.error_deg))
    if disagreement > FUSION_MAX_DISAGREEMENT_DEG:
        return DirectionObservation(
            error_deg=None,
            confidence=0.0,
            cue_id="fusion",
            cue_disagreement_deg=disagreement,
            valid=False,
            abstain_reason="cues disagree",
        )
    weight = position.confidence + pose.confidence
    if weight <= 0:
        return _direction_abstain("fusion", "zero confidence")
    fused = (
        position.error_deg * position.confidence + pose.error_deg * pose.confidence
    ) / weight
    return DirectionObservation(
        error_deg=wrap_deg(fused),
        confidence=min(position.confidence, pose.confidence),
        cue_id="fusion",
        cue_disagreement_deg=disagreement,
        valid=True,
    )


FUSION_MAX_DISAGREEMENT_DEG = 25.0
"""Provisional. E-DIR-IDEAL decides the real value; see plan 7.4."""

DIRECTION_STRATEGIES = {
    "centroid_ray": centroid_ray,
    "tip_ray": tip_ray,
    "pca_axis": pca_axis,
    "fusion": position_pose_fusion,
}
"""Every candidate, side by side. No winner is declared in source (plan 7.4)."""


# ---------------------------------------------------------------------------
# Arrival
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WaterConfig:
    """Where water is, in colour, and how much of it makes standing still unsafe.

    In the Rotwood Swamp the water has alligators in it: standing in or beside
    it for a second or two is fatal, so "we have arrived, stop here" is the one
    place the navigator must be able to say *not here*.

    Water separates from terrain cleanly by hue. Measured over ten real
    canonical frames on 2026-08-29: the water reads hue 84-99 with saturation
    around 100-120, and grass and dirt read hue 15-44. The band below is wider
    than both measurements and still does not touch the terrain.

    **What has not been measured**: how much water around the character means
    the character is *in* it. None of the ten frames has the character on or
    near water - every one reads 0% around the player - so ``max_fraction`` is
    a chosen bound, not a fitted one, and it is deliberately generous. Erring
    towards "keep moving" is free; erring the other way is a death.
    """

    #: OpenCV hue is 0..179. Water sits near cyan, terrain well below it.
    hue_low: int = 75
    hue_high: int = 112
    min_saturation: int = 45
    min_value: int = 55
    #: The box around the player anchor that counts as "where we are standing".
    radius_px: int = 70
    #: Fraction of it that has to be water before standing still is unsafe.
    max_fraction: float = 0.20
    #: Directions probed when choosing which way is driest.
    sectors: int = 8
    #: How far out from the anchor each sector looks.
    look_px: int = 170
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="ten real canonical frames, 2026-08-29; owner report of alligators",
            note=(
                "the hue band is measured and the terrain is well clear of it. "
                "max_fraction is chosen: no frame yet shows the character in water."
            ),
        )
    )


@dataclass(frozen=True)
class WaterReading:
    """How much water is under the character, and which way is driest."""

    fraction: float
    #: Bearing of the driest sector, in the same screen convention as headings.
    driest_deg: float | None
    #: Water fraction per sector, clockwise from straight up.
    sectors: tuple[tuple[float, float], ...] = ()

    def unsafe(self, config: WaterConfig) -> bool:
        return self.fraction > config.max_fraction

    def describe(self) -> str:
        way = "nowhere drier" if self.driest_deg is None else f"driest {self.driest_deg:+.0f}"
        return f"water {self.fraction:.0%} under the character, {way}"


class WaterGuard:
    """Reads how much water is around the player anchor. Decides nothing."""

    def __init__(self, config: WaterConfig | None = None) -> None:
        self._config = config or WaterConfig()

    @property
    def config(self) -> WaterConfig:
        return self._config

    def mask(self, bgr: NDArray[np.uint8]) -> NDArray[np.bool_]:
        import cv2

        config = self._config
        hsv = cv2.cvtColor(np.asarray(bgr), cv2.COLOR_BGR2HSV)
        hue, saturation, value = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
        return (
            (hue >= config.hue_low)
            & (hue <= config.hue_high)
            & (saturation >= config.min_saturation)
            & (value >= config.min_value)
        )

    def read(
        self, frame: CapturedFrame, anchor_px: tuple[float, float] | None
    ) -> WaterReading | None:
        """Water under the character, and the driest way out. ``None`` without
        an anchor, because every measurement here is relative to one."""
        if anchor_px is None:
            return None
        config = self._config
        water = self.mask(np.asarray(frame.bgr))
        height, width = water.shape[:2]
        anchor_x, anchor_y = int(anchor_px[0]), int(anchor_px[1])
        radius = config.radius_px
        patch = water[
            max(0, anchor_y - radius) : min(height, anchor_y + radius),
            max(0, anchor_x - radius) : min(width, anchor_x + radius),
        ]
        if patch.size == 0:
            return None
        fraction = float(patch.mean())

        sectors: list[tuple[float, float]] = []
        for index in range(config.sectors):
            bearing = -180.0 + index * (360.0 / config.sectors)
            radians = math.radians(bearing)
            probe_x = int(anchor_x + math.sin(radians) * config.look_px)
            probe_y = int(anchor_y - math.cos(radians) * config.look_px)
            half = max(8, radius // 2)
            window = water[
                max(0, probe_y - half) : min(height, probe_y + half),
                max(0, probe_x - half) : min(width, probe_x + half),
            ]
            sectors.append((bearing, float(window.mean()) if window.size else 1.0))
        driest = min(sectors, key=lambda entry: entry[1]) if sectors else None
        return WaterReading(
            fraction=round(fraction, 4),
            driest_deg=None if driest is None else driest[0],
            sectors=tuple((b, round(f, 4)) for b, f in sectors),
        )


@dataclass(frozen=True)
class PromptReading:
    """What the arrival band contains, measured rather than scored.

    Carried in full - not collapsed to one number - because when a route does
    not notice it has arrived, "the text was 140 px wide and we want 150" is a
    thing a person can act on and "response 0.48" is not.
    """

    found: bool
    glyph_pixels: int = 0
    width_px: int = 0
    height_px: int = 0
    centre_offset_px: float = 0.0
    aspect: float = 0.0
    detail: str = ""

    @classmethod
    def none(cls, detail: str) -> PromptReading:
        return cls(found=False, detail=detail)

    def describe(self) -> str:
        return (
            f"prompt {self.width_px}x{self.height_px} px, "
            f"{self.glyph_pixels} glyphs, offset {self.centre_offset_px:+.0f}"
        )


@dataclass(frozen=True)
class ArrivalConfig:
    """The game's own "you are here" prompt, and how sure we have to be.

    **Why the prompt and not the arrow.** The obvious signal is the map arrow
    swinging down to point at the ground, and it cannot be used: measured on
    six real frames taken at a destination, the detector found no usable arrow
    on five of them. At the spot the arrow turns transparent, bobs, and is seen
    down its own axis, so the detector reports ``ambiguous-candidates`` or
    ``rejected:topology`` - it fails exactly where arrival needs it. The
    banner, by contrast, is opaque high-contrast text in a fixed place.

    **What was measured** (canonical 1280x720, one positive frame,
    2026-08-29): the prompt occupies x 537..742, y 557..572 - one line 205 px
    wide and 15 px tall, horizontally centred to within a pixel, 314 glyph
    pixels at a fill ratio of 0.095 inside its own box. Seventeen negative
    frames - five at the destination without the prompt showing, twelve
    captured live while navigating - scored exactly zero on every statistic
    here.

    That is **one** positive frame, so every number below is provisional and
    the region is deliberately far wider than the text. E-ARRIVE has not been
    run and this does not stand in for it.
    """

    #: Where the prompt is drawn, in canonical pixels: a band across the lower
    #: middle of the client, generous around the measured 205x15 text.
    #:
    #: The previous value was (340, 90, 600, 120) - the *top* centre - which is
    #: not where this game draws it, so the detector was reading a patch of sky.
    prompt_region_px: tuple[int, int, int, int] = (340, 535, 600, 80)

    #: A glyph body is bright; the outline around it is dark. Terrain has
    #: neither next to the other, which is what makes this specific.
    min_glyph_value: int = 205
    max_outline_value: int = 90
    outline_radius_px: int = 7

    #: The prompt is one wide, centred line. A player nametag is also bright
    #: text with a dark outline, and is none of those things - it is narrow,
    #: off-centre and it moves.
    min_glyph_pixels: int = 90
    min_glyph_width_px: int = 150
    max_glyph_height_px: int = 34
    min_aspect: float = 6.0
    max_centre_offset_px: int = 90

    #: N of M consecutive observations. Short, because the prompt is transient:
    #: of six frames taken at a destination it was showing in one.
    support_window: int = 5
    required_hits: int = 3
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PENDING,
            source="one positive and seventeen negative canonical frames, 2026-08-29",
            note=(
                "E-ARRIVE has not been run. One positive frame cannot set a "
                "threshold; the geometry here is measured, the margins are chosen."
            ),
        )
    )


class ArrivalDetector:
    """Banner evidence with an N-of-M rule and exactly one latch per map.

    ``status`` stays ``PENDING`` until E-ARRIVE runs, and
    :meth:`enabled_for_production` gates any automatic use of it.
    """

    def __init__(self, config: ArrivalConfig | None = None) -> None:
        self._config = config or ArrivalConfig()
        self._window: deque[bool] = deque(maxlen=self._config.support_window)
        self._latched_map_id: str | None = None

    @property
    def config(self) -> ArrivalConfig:
        return self._config

    @property
    def status(self) -> EvidenceStatus:
        return self._config.provenance.status

    def enabled_for_production(self) -> bool:
        return self.status is EvidenceStatus.VALIDATED

    def reset_for_map(self, map_id: str) -> None:
        self._window.clear()
        if self._latched_map_id != map_id:
            self._latched_map_id = None

    def reading(self, frame: CapturedFrame) -> PromptReading:
        """Look for one wide, centred, outlined line of text in the band.

        Deliberately not an edge-density score. Edge density in a band of
        terrain is a number that goes up when there is grass in it, and the
        threshold that separates grass from a banner is exactly the threshold
        nobody has the data to set. This measures the *shape* of the thing
        instead - bright glyph bodies with dark outline against them, forming
        one wide centred line - and every one of those is a property the text
        has and terrain does not.
        """
        import cv2

        config = self._config
        x, y, width, height = config.prompt_region_px
        patch = np.asarray(frame.bgr)[y : y + height, x : x + width]
        if patch.size == 0:
            return PromptReading.none("region outside the frame")

        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        value = hsv[:, :, 2].astype(np.int16)
        bright = value > config.min_glyph_value
        outline = (value < config.max_outline_value).astype(np.uint8)
        radius = max(1, config.outline_radius_px)
        near_outline = cv2.dilate(outline, np.ones((radius, radius), np.uint8)) > 0
        glyph = bright & near_outline

        pixels = int(glyph.sum())
        if pixels < config.min_glyph_pixels:
            return PromptReading.none(f"only {pixels} glyph pixels")
        rows, columns = np.nonzero(glyph)
        glyph_width = int(columns.max() - columns.min())
        glyph_height = int(rows.max() - rows.min())
        centre_offset = float((columns.min() + columns.max()) / 2.0 - patch.shape[1] / 2.0)
        aspect = glyph_width / max(1, glyph_height)
        return PromptReading(
            found=True,
            glyph_pixels=pixels,
            width_px=glyph_width,
            height_px=glyph_height,
            centre_offset_px=centre_offset,
            aspect=aspect,
            detail="",
        )

    def matches(self, reading: PromptReading) -> tuple[bool, str]:
        """Whether a reading has the prompt's shape, and why not when it does not."""
        config = self._config
        if not reading.found:
            return (False, reading.detail)
        if reading.width_px < config.min_glyph_width_px:
            return (False, f"only {reading.width_px} px wide")
        if reading.height_px > config.max_glyph_height_px:
            return (False, f"{reading.height_px} px tall; not one line")
        if reading.aspect < config.min_aspect:
            return (False, f"aspect {reading.aspect:.1f}; not a line of text")
        if abs(reading.centre_offset_px) > config.max_centre_offset_px:
            return (False, f"off centre by {reading.centre_offset_px:+.0f} px")
        return (True, "")

    def observe(
        self, frame: CapturedFrame, *, map_id: str, approach_valid: bool
    ) -> ArrivalObservation:
        """One observation. Outside the expected lifecycle it abstains.

        ``approach_valid`` is the lifecycle context: a banner-shaped response
        with no recent valid approach is not an arrival (plan 7.3).
        """
        evidence: list[str] = []
        if self._latched_map_id == map_id:
            return ArrivalObservation(
                0.0, 0, self._config.support_window, map_id, False, ("already-latched",)
            )
        reading = self.reading(frame)
        found, why = self.matches(reading)
        self._window.append(found)
        hits = sum(1 for value in self._window if value)
        evidence.append(reading.describe() if found else f"no prompt: {why}")
        evidence.append(f"hits={hits}/{len(self._window)}")
        if not approach_valid:
            evidence.append("no-valid-approach")
            return ArrivalObservation(
                0.0, hits, self._config.support_window, None, False, tuple(evidence)
            )
        latched = hits >= self._config.required_hits
        if latched:
            self._latched_map_id = map_id
            evidence.append("latched")
        confidence = min(1.0, hits / max(1, self._config.required_hits))
        return ArrivalObservation(
            confidence=confidence,
            support_hits=hits,
            support_window=self._config.support_window,
            latched_map_id=map_id if latched else None,
            valid=latched,
            evidence=tuple(evidence),
        )
