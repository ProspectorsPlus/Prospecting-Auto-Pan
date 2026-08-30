"""Rendered stress families for the tracker's recovery latency.

**These frames are training stress, never held-out validation** (plan §7.2,
CLAUDE.md §8). Nothing here passes a gate. What they measure is a property the
real corpus cannot: how many frames the detector stays blind after the arrow
moves, when the arrow is unambiguously visible in every single frame.

The families exist because that latency was measured at 200-570 ms on a clean
60 fps sequence with a perfectly visible arrow, and a navigator that cannot see
a visible arrow for half a second cannot steer at gameplay speed.

Each family renders a settled prefix, applies one step, and counts the frames
until the detector reports a valid observation again. Ground truth is exact,
because the renderer generated it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from prospector_engine.arrow import ArrowDetector, DetectorConfig
from prospector_engine.vision import load_profiles
from tests.arrow_fixtures import render_scene
from tests.fakes import make_frame

#: The live cadence the latency budget is written against.
FRAME_HZ = 60.0
FRAME_S = 1.0 / FRAME_HZ

#: Frames rendered before the step, so the track is settled and its velocity
#: estimate is honest rather than a startup transient.
SETTLE_FRAMES = 8
#: Frames rendered after the step. Long enough to contain the old blackout
#: (570 ms is 34 frames) with room to spare.
RECOVER_FRAMES = 45

JUMP_FAMILY_PX: tuple[float, ...] = (60.0, 100.0, 180.0, 250.0, 400.0)
SCALE_FAMILY_PX: tuple[float, ...] = (85.0, 95.0, 110.0, 130.0, 160.0, 200.0)
BASE_SCALE_PX = 70.0
TURN_FAMILY_DEG: tuple[float, ...] = (25.0, 45.0, 70.0)


@dataclass(frozen=True)
class Recovery:
    """How long the detector was blind, and what it was following afterwards."""

    label: str
    #: Frames between the step and the next valid observation. ``None`` means
    #: it never recovered inside the window.
    blind_frames: int | None
    track_id_before: int | None
    track_id_after: int | None
    #: Heading error on the first recovered frame, against ground truth.
    error_deg: float | None

    @property
    def blind_ms(self) -> float | None:
        return None if self.blind_frames is None else self.blind_frames * FRAME_S * 1000.0

    @property
    def kept_identity(self) -> bool:
        """Whether the same arrow was still the same arrow afterwards.

        A recovery that renumbers the track is a reacquisition, and every
        consumer that keyed off the identity has to start again.
        """
        return self.track_id_before is not None and self.track_id_before == self.track_id_after

    def describe(self) -> str:
        blind = "never" if self.blind_ms is None else f"{self.blind_ms:.0f} ms"
        identity = "same track" if self.kept_identity else "new track"
        error = "-" if self.error_deg is None else f"{self.error_deg:.1f} deg"
        return f"{self.label:>22}  blind {blind:>8}  {identity:>10}  err {error:>8}"


def _detector(config: DetectorConfig | None = None) -> ArrowDetector:
    profile = load_profiles().get("green_arrow_v1")
    assert profile is not None
    return ArrowDetector(profile, config)


def _run(
    detector: ArrowDetector,
    scenes: list[tuple[float, tuple[float, float], float]],
    *,
    start_s: float = 100.0,
) -> list[tuple[bool, int | None, float | None, float]]:
    """Feed a sequence of (heading, centre, scale) and report each verdict."""
    out = []
    for index, (heading, centre, scale) in enumerate(scenes):
        scene = render_scene(
            heading_deg=heading, centre_px=centre, scale_px=scale, terrain="grass", seed=11
        )
        frame = make_frame(index + 1, captured_at_s=start_s + index * FRAME_S, bgr=scene.bgr)
        observation, _ = detector.analyze(frame)
        error = None
        if observation.valid and observation.axis_unit_xy is not None:
            measured = math.degrees(
                math.atan2(observation.axis_unit_xy[0], -observation.axis_unit_xy[1])
            )
            error = abs((measured - heading + 180.0) % 360.0 - 180.0)
        out.append((observation.valid, detector.track_id, error, scale))
    return out


def _recovery(
    label: str,
    before: list[tuple[float, tuple[float, float], float]],
    after: list[tuple[float, tuple[float, float], float]],
    *,
    config: DetectorConfig | None = None,
) -> Recovery:
    detector = _detector(config)
    verdicts = _run(detector, before + after)
    settled = verdicts[: len(before)]
    recovering = verdicts[len(before) :]
    track_before = next((track for valid, track, _e, _s in reversed(settled) if valid), None)
    for offset, (valid, track_after, error, _scale) in enumerate(recovering):
        if valid:
            return Recovery(label, offset, track_before, track_after, error)
    return Recovery(label, None, track_before, None, None)


def jump_recovery(distance_px: float, *, config: DetectorConfig | None = None) -> Recovery:
    """The arrow steps sideways by ``distance_px`` between two frames."""
    centre = (640.0, 360.0)
    moved = (centre[0] + distance_px, centre[1])
    before = [(30.0, centre, 90.0)] * SETTLE_FRAMES
    after = [(30.0, moved, 90.0)] * RECOVER_FRAMES
    return _recovery(f"jump {distance_px:.0f} px", before, after, config=config)


def scale_recovery(to_px: float, *, config: DetectorConfig | None = None) -> Recovery:
    """The arrow steps from ``BASE_SCALE_PX`` to ``to_px`` between two frames."""
    centre = (640.0, 360.0)
    before = [(30.0, centre, BASE_SCALE_PX)] * SETTLE_FRAMES
    after = [(30.0, centre, to_px)] * RECOVER_FRAMES
    return _recovery(f"scale {BASE_SCALE_PX:.0f}->{to_px:.0f}", before, after, config=config)


def sweep_recovery(step_deg: float, *, config: DetectorConfig | None = None) -> Recovery:
    """A camera turn: the arrow swings on an arc and rotates as it goes."""
    centre = (640.0, 360.0)
    radius = 260.0
    before = [(30.0, centre, 90.0)] * SETTLE_FRAMES
    after = []
    for index in range(RECOVER_FRAMES):
        angle = math.radians(step_deg * (index + 1))
        after.append(
            (
                30.0 + step_deg * (index + 1),
                (
                    centre[0] + radius * math.sin(angle),
                    centre[1] - radius * (1 - math.cos(angle)),
                ),
                90.0,
            )
        )
    return _recovery(f"turn {step_deg:.0f} deg/frame", before, after, config=config)


def report(config: DetectorConfig | None = None) -> list[Recovery]:
    """Every family, in one list, for the before/after table."""
    rows = [jump_recovery(px, config=config) for px in JUMP_FAMILY_PX]
    rows += [scale_recovery(px, config=config) for px in SCALE_FAMILY_PX]
    rows += [sweep_recovery(deg, config=config) for deg in TURN_FAMILY_DEG]
    return rows


# ---------------------------------------------------------------------------
# The other half: what resuming must never do
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Distraction:
    """What the detector did once the arrow it was following stopped existing."""

    label: str
    #: Frames after the arrow vanished that were still reported as valid. Any
    #: of these is the detector steering off a blob it decided was the arrow.
    false_valid_frames: int
    frames: int
    #: Whether the identity was handed to something else.
    switched: bool

    def describe(self) -> str:
        return (
            f"{self.label:>22}  {self.false_valid_frames}/{self.frames} false valid  "
            f"{'SWITCHED' if self.switched else 'held identity'}"
        )


def distractor_vanish(
    distractors: int, *, seed: int = 11, config: DetectorConfig | None = None
) -> Distraction:
    """Settle on the arrow, then delete it while same-coloured blobs remain.

    This is the case resuming must never fire on. Every frame after the arrow
    goes has a green thing in it that is not the arrow, and the honest answer
    to every one of them is "I cannot see it".

    The blobs are rendered in the same places throughout: only the arrow
    changes, so any candidate accepted afterwards is a blob the detector had
    already been rejecting while it could see the real thing.
    """
    detector = _detector(config)
    centre = (640.0, 360.0)
    false_valid = 0
    switched = False
    track_before: int | None = None
    after_frames = 12
    for index in range(SETTLE_FRAMES + after_frames):
        vanished = index >= SETTLE_FRAMES
        scene = render_scene(
            heading_deg=30.0,
            centre_px=centre,
            scale_px=90.0,
            terrain="grass",
            seed=seed,
            distractors=distractors,
            arrow=not vanished,
        )
        frame = make_frame(index + 1, captured_at_s=100.0 + index * FRAME_S, bgr=scene.bgr)
        observation, _ = detector.analyze(frame)
        if not vanished:
            if observation.valid:
                track_before = detector.track_id
            continue
        if observation.valid:
            false_valid += 1
            if track_before is not None and detector.track_id != track_before:
                switched = True
    return Distraction(
        f"vanish +{distractors} blobs s{seed}", false_valid, after_frames, switched
    )


@dataclass(frozen=True)
class ClutteredJump:
    """A jump with same-coloured clutter in frame: it must find the arrow."""

    label: str
    blind_frames: int | None
    kept_identity: bool
    #: Distance from the recovered centroid to where the arrow actually is.
    #: A large value means it recovered onto a blob, which is worse than
    #: staying blind.
    position_error_px: float | None

    def describe(self) -> str:
        blind = (
            "never"
            if self.blind_frames is None
            else f"{self.blind_frames * FRAME_S * 1000:.0f} ms"
        )
        error = "-" if self.position_error_px is None else f"{self.position_error_px:.0f} px"
        identity = "same track" if self.kept_identity else "new track"
        return f"{self.label:>22}  blind {blind:>8}  {identity:>10}  off by {error:>7}"


def distractor_jump(
    distractors: int, jump_px: float, *, seed: int = 11, config: DetectorConfig | None = None
) -> ClutteredJump:
    """The arrow jumps while same-coloured blobs are on screen.

    Both halves at once: resuming must fire, and it must fire on the arrow.
    Recovering fast onto a blob is worse than not recovering at all.
    """
    detector = _detector(config)
    centre = (640.0, 360.0)
    moved = (centre[0] + jump_px, centre[1])
    track_before: int | None = None
    for index in range(SETTLE_FRAMES + RECOVER_FRAMES):
        jumped = index >= SETTLE_FRAMES
        where = moved if jumped else centre
        scene = render_scene(
            heading_deg=30.0,
            centre_px=where,
            scale_px=90.0,
            terrain="grass",
            seed=seed,
            distractors=distractors,
        )
        frame = make_frame(index + 1, captured_at_s=100.0 + index * FRAME_S, bgr=scene.bgr)
        observation, _ = detector.analyze(frame)
        if not jumped:
            if observation.valid:
                track_before = detector.track_id
            continue
        if observation.valid and observation.centroid_px is not None:
            return ClutteredJump(
                f"jump {jump_px:.0f} px +{distractors}",
                index - SETTLE_FRAMES,
                track_before is not None and detector.track_id == track_before,
                math.dist(observation.centroid_px, where),
            )
    return ClutteredJump(f"jump {jump_px:.0f} px +{distractors}", None, False, None)


def distractor_report(config: DetectorConfig | None = None) -> list[Distraction]:
    """Several layouts, not one lucky one."""
    return [
        distractor_vanish(count, seed=seed, config=config)
        for count in (1, 3, 6)
        for seed in (11, 23, 47)
    ]


def cluttered_jump_report(config: DetectorConfig | None = None) -> list[ClutteredJump]:
    return [
        distractor_jump(3, px, seed=seed, config=config)
        for px in (100.0, 250.0)
        for seed in (11, 23, 47)
    ]


# ---------------------------------------------------------------------------
# Temporal continuity: what the bridge carries, at a cadence the corpus cannot
# ---------------------------------------------------------------------------
#
# The real corpus is sampled at about 5 fps. Every horizon in
# ``TemporalConfig`` is in seconds, and ``max_step_s`` deliberately makes the
# bridge inert below about 10 fps - so the corpus cannot measure this half of
# perception at all, and D-094 does not claim it does. These families are how
# the dense regime is exercised instead, and CLAUDE.md rule 9 applies to every
# frame of them: rendered stress, never a gate.
#
# What they measure is the one number a steering controller cares about during
# an occlusion: the longest run of frames with *no usable observation*. That is
# what COAST and then SEARCH have to cover, and it is what the bridge exists to
# shorten.

#: The occlusion model: horizontal bars across the arrow, foliage-shaped.
#:
#: Both numbers were swept against the real detector rather than assumed. A
#: single solid bar is the wrong model - at 70 % coverage it defeats the
#: detector, but it also destroys the *appearance*, so no correlation-based
#: method can carry it and the family would only measure that fact. Bars are
#: the realistic case: leaves in front of the arrow break the outline topology
#: the structural detector scores on while leaving most of the arrow's own
#: pixels on screen.
#:
#: Swept: at five bars covering 40 % the detector abstains on every occluded
#: frame after the first, and 60 % of the arrow is still visible. At three bars
#: covering 40-50 % the detector still commits (nothing to bridge); at any
#: count covering 60 % the outline and the appearance both go.
OCCLUSION_BARS = 5
OCCLUSION_COVER = 0.40
#: Close to the grass terrain, so it is a plausible occluder rather than a
#: black rectangle the contrast term would find interesting on its own.
OCCLUDER_BGR = (48, 92, 58)

CONTINUITY_HZ: tuple[float, ...] = (30.0, 60.0, 90.0)
CONTINUITY_OCCLUSION_MS: tuple[float, ...] = (50.0, 100.0, 250.0, 500.0, 1000.0, 2000.0)


@dataclass(frozen=True)
class Continuity:
    """One occlusion, and how much of it perception managed to cover."""

    label: str
    hz: float
    occlusion_ms: float
    frames: int
    global_frames: int
    bridged_frames: int
    predicted_frames: int
    #: Frames during the occlusion with no usable observation at all.
    blind_frames: int
    #: The longest unbroken run of those, in milliseconds. This is the number
    #: the coast grace and the search budget are spent on.
    longest_blind_ms: float
    identity_held: bool
    #: Heading error on the first globally observed frame after the occlusion.
    recovery_error_deg: float | None

    @property
    def covered(self) -> float:
        """Fraction of the occlusion the bridge carried."""
        occluded = self.bridged_frames + self.predicted_frames + self.blind_frames
        return self.bridged_frames / occluded if occluded else 0.0


def _pipeline(bridge: bool = True):
    """A real perception pipeline, optionally with the bridge switched off.

    Switching it off is how the *same* sequence produces a before-and-after
    rather than a bare number: the comparison is against this repository's own
    previous behaviour, on identical frames.
    """
    from prospector_engine.navigation import PerceptionPipeline
    from prospector_engine.temporal import TemporalBridge, TemporalConfig
    from prospector_engine.vision import ArrowSegmenter

    profile = load_profiles().get("green_arrow_v1")
    assert profile is not None
    pipeline = PerceptionPipeline(
        segmenter=ArrowSegmenter(profile), detector=ArrowDetector(profile, DetectorConfig())
    )
    if not bridge:
        # Horizons that nothing can ever be inside, so the bridge abstains on
        # every frame - which is exactly the behaviour before D-094.
        pipeline.bridge = TemporalBridge(
            TemporalConfig(max_bridge_s=1e-9, max_predict_s=1e-9, max_step_s=1e-9)
        )
    return pipeline


def _occlude(bgr, centre: tuple[float, float], scale_px: float):
    """Lay foliage-shaped bars across the arrow. Deterministic by design."""
    import cv2
    import numpy as np

    out = np.array(bgr, dtype=np.uint8, copy=True)
    half = scale_px * 1.1
    top = centre[1] - half
    band = (2.0 * half) / OCCLUSION_BARS
    for index in range(OCCLUSION_BARS):
        y0 = int(top + index * band)
        cv2.rectangle(
            out,
            (int(centre[0] - half * 1.2), y0),
            (int(centre[0] + half * 1.2), int(y0 + band * OCCLUSION_COVER)),
            OCCLUDER_BGR,
            thickness=-1,
        )
    return out


def continuity(
    hz: float, occlusion_ms: float, *, bridge: bool = True, walk_px_s: float = 120.0
) -> Continuity:
    """Walk, occlude for ``occlusion_ms``, walk again. Measure the blind run."""
    from prospector_engine.contracts import EvidenceProvenance

    frame_s = 1.0 / hz
    settle = max(6, int(0.15 * hz))
    occluded = max(1, round(occlusion_ms / 1000.0 * hz))
    trailing = max(6, int(0.15 * hz))
    pipeline = _pipeline(bridge)

    centre_x = 500.0
    provenances: list[EvidenceProvenance] = []
    identity_before: int | None = None
    recovery_error: float | None = None
    identity_after: int | None = None

    total = settle + occluded + trailing
    for index in range(total):
        at_s = 100.0 + index * frame_s
        centre = (centre_x + walk_px_s * index * frame_s, 360.0)
        occluding = settle <= index < settle + occluded
        scene = render_scene(
            heading_deg=30.0,
            centre_px=centre,
            scale_px=90.0,
            terrain="grass",
            seed=11,
        )
        bgr = _occlude(scene.bgr, centre, 90.0) if occluding else scene.bgr
        frame = make_frame(index + 1, captured_at_s=at_s, bgr=bgr)
        result = pipeline.analyze(frame, map_id="continuity", approach_valid=False)
        arrow = result.inputs.arrow
        if index == settle - 1:
            identity_before = arrow.track_id
        if occluding:
            provenances.append(arrow.provenance if arrow.valid else EvidenceProvenance.LOST)
        elif index >= settle + occluded and recovery_error is None and arrow.valid:
            identity_after = arrow.track_id
            direction = result.inputs.direction
            if direction.valid and direction.error_deg is not None:
                recovery_error = abs(direction.error_deg - 30.0)
            else:
                recovery_error = None

    blind = sum(1 for p in provenances if not p.observed)
    run = longest = 0
    for provenance in provenances:
        run = run + 1 if not provenance.observed else 0
        longest = max(longest, run)
    return Continuity(
        label=f"{hz:.0f}Hz/{occlusion_ms:.0f}ms{'' if bridge else '/no-bridge'}",
        hz=hz,
        occlusion_ms=occlusion_ms,
        frames=total,
        global_frames=sum(1 for p in provenances if p.structural),
        bridged_frames=sum(1 for p in provenances if p is EvidenceProvenance.BRIDGED),
        predicted_frames=sum(1 for p in provenances if p is EvidenceProvenance.PREDICTED),
        blind_frames=blind,
        longest_blind_ms=longest * (1000.0 / hz),
        identity_held=(identity_before is not None and identity_before == identity_after),
        recovery_error_deg=recovery_error,
    )


def continuity_report(*, bridge: bool = True) -> list[Continuity]:
    """Every cadence against every occlusion duration."""
    return [
        continuity(hz, occlusion_ms, bridge=bridge)
        for hz in CONTINUITY_HZ
        for occlusion_ms in CONTINUITY_OCCLUSION_MS
    ]
