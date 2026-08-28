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
    "ArrivalDetector",
    "ArrowProfile",
    "ArrowSegmenter",
    "ArrowTracker",
    "ProfileAuthority",
    "ProfileLibrary",
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

    @property
    def selectable_automatically(self) -> bool:
        """Automatic classification requires a passed E-PROF gate (plan 7.4)."""
        return self.status is EvidenceStatus.VALIDATED


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
        return tuple(p for p in self._profiles.values() if p.selectable_automatically)

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


class ArrowTracker:
    """Bounded constant-velocity track used to *prioritize* search only.

    It never fabricates a missing measurement: when the detector abstains the
    track ages and eventually drops, and the caller sees the abstention
    (plan 8).
    """

    def __init__(self, max_age_frames: int = 5, max_speed_px: float = 120.0) -> None:
        self._max_age = max_age_frames
        self._max_speed_px = max_speed_px
        self._track_id = 0
        self._last: tuple[float, float] | None = None
        self._velocity = (0.0, 0.0)
        self._age = 0
        self._switches = 0

    @property
    def track_id(self) -> int | None:
        return self._track_id or None

    @property
    def switches(self) -> int:
        return self._switches

    def predicted(self) -> tuple[float, float] | None:
        if self._last is None or self._age > self._max_age:
            return None
        return (
            self._last[0] + self._velocity[0] * self._age,
            self._last[1] + self._velocity[1] * self._age,
        )

    def update(self, observation: ArrowObservation) -> ArrowObservation:
        if not observation.valid or observation.centroid_px is None:
            self._age += 1
            if self._age > self._max_age:
                self._last = None
                self._velocity = (0.0, 0.0)
            return observation
        centroid = observation.centroid_px
        if self._last is None:
            self._track_id += 1
        else:
            dx = centroid[0] - self._last[0]
            dy = centroid[1] - self._last[1]
            if math.hypot(dx, dy) > self._max_speed_px:
                self._track_id += 1
                self._switches += 1
                self._velocity = (0.0, 0.0)
            else:
                self._velocity = (dx, dy)
        self._last = centroid
        self._age = 0
        return ArrowObservation(
            profile_id=observation.profile_id,
            track_id=self._track_id,
            bbox_px=observation.bbox_px,
            centroid_px=observation.centroid_px,
            tip_px=observation.tip_px,
            axis_unit_xy=observation.axis_unit_xy,
            confidence=observation.confidence,
            valid=True,
            abstain_reason=None,
            tail_px=observation.tail_px,
        )


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
class ArrivalConfig:
    """N-of-M temporal rule plus lifecycle context. All values provisional."""

    support_window: int = 6
    required_hits: int = 4
    min_response: float = 0.55
    banner_region_px: tuple[int, int, int, int] = (340, 90, 600, 120)
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PENDING,
            source="one supplied positive screenshot",
            note="E-ARRIVE has not been run; a single frame cannot set a threshold",
        )
    )


class ArrivalDetector:
    """Banner evidence with an N-of-M rule and exactly one latch per map.

    ``status`` stays ``PENDING`` until E-ARRIVE runs, and
    :meth:`enabled_for_production` gates any automatic use of it.
    """

    def __init__(self, config: ArrivalConfig | None = None) -> None:
        self._config = config or ArrivalConfig()
        self._window: deque[float] = deque(maxlen=self._config.support_window)
        self._latched_map_id: str | None = None

    @property
    def status(self) -> EvidenceStatus:
        return self._config.provenance.status

    def enabled_for_production(self) -> bool:
        return self.status is EvidenceStatus.VALIDATED

    def reset_for_map(self, map_id: str) -> None:
        self._window.clear()
        if self._latched_map_id != map_id:
            self._latched_map_id = None

    def response(self, frame: CapturedFrame) -> float:
        """Edge/gradient response inside the banner region, normalized 0..1."""
        import cv2

        x, y, width, height = self._config.banner_region_px
        patch = np.asarray(frame.bgr)[y : y + height, x : x + width]
        if patch.size == 0:
            return 0.0
        grey = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(grey, 60, 180)
        return float(edges.mean() / 255.0) * 4.0

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
        response = self.response(frame)
        self._window.append(response)
        hits = sum(1 for value in self._window if value >= self._config.min_response)
        evidence.append(f"response={response:.3f}")
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
