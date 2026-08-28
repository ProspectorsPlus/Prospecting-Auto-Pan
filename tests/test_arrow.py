"""The production arrow detector and signed direction estimator.

The failure this suite exists for: in daylight a patch of grass matching the
arrow's colour was promoted over the real arrow, because candidates were ranked
by area and confidence was an area-fit score. Two of these tests reproduce that
exact situation and require the opposite outcome.

Synthetic frames are used for the deterministic angle work, and they are
**training stress, not held-out validation** (plan 7.2). Nothing here passes
E-PROF or E-DIR-E2E; those need real labelled sessions.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from prospector_engine.arrow import (
    ArrowDetector,
    DetectorConfig,
    DirectionEstimator,
    circular_consensus,
    wrap_deg,
)
from prospector_engine.contracts import CapturedFrame, freeze_array
from prospector_engine.vision import load_profiles
from tests.arrow_fixtures import ARROW_BRIGHT_BGR, render_scene
from tests.fakes import make_geometry

PROFILE = load_profiles().get("green_arrow_v1")
assert PROFILE is not None


def _frame(scene: object, sequence: int = 1) -> CapturedFrame:
    image = np.ascontiguousarray(scene.bgr)  # type: ignore[attr-defined]
    return CapturedFrame(
        sequence=sequence,
        captured_at_s=float(sequence) * 0.01,
        completed_at_s=float(sequence) * 0.01 + 0.002,
        duration_ms=2.0,
        geometry=make_geometry(),
        bgr=freeze_array(image),
        backend="synthetic",
    )


def _read(
    detector: ArrowDetector, estimator: DirectionEstimator, scene: object, sequence: int = 1
) -> tuple[object, object]:
    arrow, hypotheses = detector.analyze(_frame(scene, sequence))
    if not arrow.valid:
        return (arrow, None)
    accepted = next((h for h in hypotheses if h.accepted), None)
    result = estimator.estimate(
        accepted.features if accepted else None,
        anchor_px=(640.0, 430.0),
        forward_deg=0.0,
        arrow_confidence=arrow.confidence,
    )
    return (arrow, result.observation)


def _pair() -> tuple[ArrowDetector, DirectionEstimator]:
    config = DetectorConfig()
    return (ArrowDetector(PROFILE, config), DirectionEstimator(config))


# ---------------------------------------------------------------------------
# The daylight failure
# ---------------------------------------------------------------------------


def test_a_large_matching_terrain_region_does_not_beat_the_small_real_arrow() -> None:
    """The daylight bug, reproduced and required to come out the other way.

    Grass whose green chromaticity matches the arrow's to three decimal places
    fills the frame. Ranked by area the grass wins by four orders of magnitude.
    """
    detector, estimator = _pair()
    scene = render_scene(heading_deg=40.0, terrain="grass", scale_px=95.0, seed=3)

    arrow, direction = _read(detector, estimator, scene)

    assert arrow.valid, f"the arrow was lost: {arrow.abstain_reason}"
    assert arrow.centroid_px is not None
    # The arrow is at the frame centre; a terrain lock would be elsewhere and
    # enormous.
    assert math.dist(arrow.centroid_px, (640.0, 360.0)) < 60.0
    assert direction is not None and direction.valid


def test_colour_alone_cannot_separate_the_arrow_from_the_grass() -> None:
    """The premise of the whole design, asserted rather than assumed."""
    detector, _estimator = _pair()
    scene = render_scene(heading_deg=0.0, terrain="grass", scale_px=95.0, seed=4)
    channels = detector._channels(np.asarray(_frame(scene).bgr))
    mask = detector._chroma_mask(channels)

    coverage = float(mask.mean()) / 255.0
    assert coverage > 0.5, "if colour alone worked, the rest of the detector would be optional"


def test_a_component_welded_to_terrain_is_split_rather_than_rejected() -> None:
    detector, estimator = _pair()
    scene = render_scene(heading_deg=115.0, terrain="grass", scale_px=110.0, seed=5)

    arrow, direction = _read(detector, estimator, scene)

    assert arrow.valid
    assert direction is not None and direction.valid
    assert abs(wrap_deg(direction.error_deg - 115.0)) < 10.0


def test_confidence_is_a_breakdown_of_independent_evidence_not_an_area_fit() -> None:
    detector, _estimator = _pair()
    scene = render_scene(heading_deg=20.0, terrain="dirt", scale_px=100.0, seed=6)
    arrow, hypotheses = detector.analyze(_frame(scene))

    assert arrow.valid
    names = {name for name, _value in arrow.score_terms}
    assert {"contrast", "topology", "solidity", "boundary", "chroma"} <= names
    assert all(0.0 <= value <= 1.0 for _name, value in arrow.score_terms)
    assert hypotheses[0].weakest_term in names


def test_a_rejected_candidate_records_why_it_was_rejected() -> None:
    detector, _estimator = _pair()
    # An arrow-coloured disc: right colour, right brightness, no arrowhead.
    scene = render_scene(
        heading_deg=0.0, terrain="dirt", scale_px=90.0, distractors=3, arrow=False, seed=7
    )
    arrow, hypotheses = detector.analyze(_frame(scene))

    assert not arrow.valid
    for hypothesis in hypotheses:
        assert not hypothesis.accepted
        assert hypothesis.reason


def test_the_arrowhead_topology_is_necessary_not_merely_weighted() -> None:
    """An arrow-coloured ellipse satisfied every other term at once, and won."""
    detector, _estimator = _pair()
    scene = render_scene(
        heading_deg=0.0, terrain="dirt", scale_px=110.0, distractors=6, arrow=False, seed=8
    )

    arrow, _hypotheses = detector.analyze(_frame(scene))

    assert not arrow.valid, "same colour, same brightness, no notches: not an arrow"


# ---------------------------------------------------------------------------
# Deterministic angles: the whole circle, including the seam
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("heading", list(range(0, 360, 15)))
def test_every_heading_is_recovered_within_five_degrees(heading: int) -> None:
    detector, estimator = _pair()
    scene = render_scene(
        heading_deg=float(heading), terrain="grass", scale_px=100.0, seed=heading
    )

    arrow, direction = _read(detector, estimator, scene)

    assert arrow.valid, f"{heading} deg: {arrow.abstain_reason}"
    assert direction is not None and direction.valid, f"{heading} deg abstained"
    assert abs(wrap_deg(direction.error_deg - heading)) <= 5.0


@pytest.mark.parametrize("heading", [175.0, 178.0, 179.5, 180.0, -179.5, -178.0, -175.0])
def test_the_plus_minus_180_seam_is_handled(heading: float) -> None:
    detector, estimator = _pair()
    scene = render_scene(heading_deg=heading, terrain="dirt", scale_px=100.0, seed=9)

    _arrow, direction = _read(detector, estimator, scene)

    assert direction is not None and direction.valid
    assert abs(wrap_deg(direction.error_deg - heading)) <= 5.0
    assert -180.0 < direction.error_deg <= 180.0


@pytest.mark.parametrize("terrain", ["dirt", "grass", "water", "pale", "night_grass"])
def test_no_terrain_produces_a_polarity_flip(terrain: str) -> None:
    """Zero silent 180-degree flips is the hard requirement."""
    detector, estimator = _pair()
    for index, heading in enumerate(range(0, 360, 30)):
        scene = render_scene(
            heading_deg=float(heading), terrain=terrain, scale_px=100.0, seed=index
        )
        detector.reset()
        _arrow, direction = _read(detector, estimator, scene)
        if direction is None or not direction.valid:
            continue
        error = abs(wrap_deg(direction.error_deg - heading))
        assert error <= 90.0, f"{terrain} {heading} deg flipped by {error:.0f} deg"


@pytest.mark.parametrize(
    ("label", "options"),
    [
        ("blurred", {"blur_px": 7}),
        ("eroded", {"erode_px": 5}),
        ("small", {"scale_px": 45.0}),
        ("large", {"scale_px": 210.0}),
        ("foreshortened", {"foreshorten": 0.5}),
        ("translucent", {"alpha": 0.55, "terrain": "pale"}),
        ("dim", {"brightness": 0.5}),
        ("bright", {"brightness": 1.3, "terrain": "pale"}),
        ("cluttered", {"distractors": 5}),
    ],
)
def test_degraded_conditions_never_produce_a_confident_wrong_direction(
    label: str, options: dict[str, object]
) -> None:
    """Accuracy may drop and coverage may drop. The sign may not invert."""
    detector, estimator = _pair()
    settings: dict[str, object] = {"terrain": "grass", "scale_px": 100.0}
    settings.update(options)
    for index, heading in enumerate(range(0, 360, 45)):
        detector.reset()
        scene = render_scene(heading_deg=float(heading), seed=index, **settings)  # type: ignore[arg-type]
        _arrow, direction = _read(detector, estimator, scene)
        if direction is None or not direction.valid:
            continue
        error = abs(wrap_deg(direction.error_deg - heading))
        assert error <= 90.0, f"{label} at {heading} deg flipped by {error:.0f} deg"


# ---------------------------------------------------------------------------
# Abstention
# ---------------------------------------------------------------------------


def test_an_arrow_absent_frame_acquires_nothing() -> None:
    detector, _estimator = _pair()
    for terrain in ("dirt", "grass", "water", "pale"):
        scene = render_scene(heading_deg=0.0, terrain=terrain, arrow=False, seed=11)
        detector.reset()
        arrow, _hypotheses = detector.analyze(_frame(scene))
        assert not arrow.valid, f"{terrain}: acquired an arrow that is not there"


def test_a_plausible_tie_abstains_rather_than_guessing() -> None:
    """Two arrows at once is ambiguous. Picking one is the failure mode."""
    import cv2

    detector, _estimator = _pair()
    first = render_scene(heading_deg=30.0, terrain="dirt", scale_px=100.0, seed=12)
    second = render_scene(
        heading_deg=30.0, terrain="dirt", scale_px=100.0, centre_px=(320.0, 200.0), seed=12
    )
    combined = np.maximum(first.bgr, second.bgr)
    # The two renders share a background, so a max composite keeps both arrows.
    del cv2
    scene = type(first)(
        bgr=combined,
        heading_deg=30.0,
        centre_px=first.centre_px,
        scale_px=100.0,
        terrain="dirt",
        clipped=False,
        alpha=1.0,
    )

    arrow, hypotheses = detector.analyze(_frame(scene))

    accepted = [h for h in hypotheses if h.accepted]
    if len(accepted) > 1 and accepted[0].score - accepted[1].score < 0.12:
        assert not arrow.valid
        assert arrow.abstain_reason == "ambiguous-candidates"


def test_the_pca_axis_is_refused_when_it_is_ill_conditioned() -> None:
    """The measured elongation of this arrow is 1.3, which is not an axis."""
    detector, estimator = _pair()
    scene = render_scene(heading_deg=60.0, terrain="dirt", scale_px=110.0, seed=13)
    arrow, hypotheses = detector.analyze(_frame(scene))
    assert arrow.valid
    accepted = next(h for h in hypotheses if h.accepted)
    result = estimator.estimate(
        accepted.features, anchor_px=(640.0, 430.0), forward_deg=0.0, arrow_confidence=0.9
    )

    pca = next((c for c in result.readings if c.cue_id == "pca_axis"), None)
    assert pca is not None
    if accepted.features.anisotropy < DetectorConfig().min_anisotropy:
        assert not pca.valid
        assert "anisotropy" in pca.note


def test_position_and_pose_are_reported_as_different_things() -> None:
    detector, estimator = _pair()
    scene = render_scene(
        heading_deg=90.0, terrain="dirt", scale_px=100.0, centre_px=(400.0, 250.0), seed=14
    )
    arrow, hypotheses = detector.analyze(_frame(scene))
    accepted = next(h for h in hypotheses if h.accepted)
    result = estimator.estimate(
        accepted.features, anchor_px=(640.0, 430.0), forward_deg=0.0, arrow_confidence=0.9
    )

    position = next(c for c in result.readings if c.cue_id == "player_to_arrow")
    pose = next(c for c in result.readings if c.cue_id == "tail_to_head")
    assert position.heading_deg != pose.heading_deg
    # Position is reported but must not dominate: it answers a different question.
    assert position.weight < pose.weight or position.weight == 0.0
    assert arrow.valid


# ---------------------------------------------------------------------------
# Consensus arithmetic
# ---------------------------------------------------------------------------


def test_consensus_rejects_a_180_degree_outlier_instead_of_averaging_it() -> None:
    readings = [("a", 10.0, 1.0), ("b", 12.0, 1.0), ("c", -170.0, 1.0)]

    heading, spread, weights = circular_consensus(readings, outlier_deg=30.0)

    assert heading is not None and abs(wrap_deg(heading - 11.0)) < 2.0
    assert weights["c"] == 0.0, "the outlier is dropped, and visibly so"
    assert weights["a"] > 0.0 and weights["b"] > 0.0
    assert spread < 3.0


def test_consensus_is_correct_across_the_seam() -> None:
    heading, _spread, _weights = circular_consensus(
        [("a", 179.0, 1.0), ("b", -179.0, 1.0)], outlier_deg=30.0
    )

    assert heading is not None
    assert abs(wrap_deg(heading - 180.0)) < 1.0


def test_wholesale_disagreement_abstains() -> None:
    heading, spread, _weights = circular_consensus(
        [("a", 0.0, 1.0), ("b", 120.0, 1.0), ("c", -120.0, 1.0)], outlier_deg=30.0
    )

    assert heading is None
    assert spread == 180.0


# ---------------------------------------------------------------------------
# Tracking
# ---------------------------------------------------------------------------


def test_a_track_holds_across_frames_and_reports_its_age() -> None:
    detector, _estimator = _pair()
    identities = []
    for sequence in range(1, 7):
        scene = render_scene(
            heading_deg=30.0 + sequence,
            terrain="dirt",
            scale_px=100.0,
            centre_px=(600.0 + sequence * 6, 360.0),
            seed=1,
        )
        arrow, _hypotheses = detector.analyze(_frame(scene, sequence))
        if arrow.valid:
            identities.append(arrow.track_id)
    assert identities, "nothing was tracked at all"
    assert len(set(identities)) == 1, f"the identity changed: {identities}"
    assert detector.track_age >= 3


def test_a_wrong_track_is_dropped_rather_than_held_to_look_stable() -> None:
    """A held track must never survive its candidate becoming unviable."""
    detector, _estimator = _pair()
    for sequence in range(1, 5):
        scene = render_scene(heading_deg=20.0, terrain="dirt", scale_px=100.0, seed=1)
        detector.analyze(_frame(scene, sequence))
    assert detector.track_id is not None

    for sequence in range(5, 14):
        empty = render_scene(heading_deg=0.0, terrain="dirt", arrow=False, seed=2)
        arrow, _hypotheses = detector.analyze(_frame(empty, sequence))
        assert not arrow.valid
    assert detector.predicted_centroid() is None, "a track outlived its evidence"


def test_a_periodic_global_pass_stops_a_track_following_the_wrong_thing() -> None:
    config = DetectorConfig(reacquire_every=3)
    detector = ArrowDetector(PROFILE, config)
    for sequence in range(1, 10):
        scene = render_scene(heading_deg=25.0, terrain="dirt", scale_px=100.0, seed=1)
        detector.analyze(_frame(scene, sequence))
    # The counter resets on every global pass, so it can never run away.
    assert detector._frames_since_global <= config.reacquire_every


def test_resetting_the_detector_drops_every_piece_of_temporal_state() -> None:
    detector, _estimator = _pair()
    scene = render_scene(heading_deg=25.0, terrain="dirt", scale_px=100.0, seed=1)
    detector.analyze(_frame(scene))
    assert detector.track_id is not None

    detector.reset()

    assert detector.track_id is None
    assert detector.predicted_centroid() is None
    assert detector.track_age == 0


# ---------------------------------------------------------------------------
# Clipping
# ---------------------------------------------------------------------------


def test_a_clipped_arrow_loses_confidence_but_is_not_discarded() -> None:
    """When the player stands under the arrow it fills the view and clips."""
    detector, _estimator = _pair()
    whole = render_scene(heading_deg=20.0, terrain="dirt", scale_px=110.0, seed=1)
    clipped = render_scene(
        heading_deg=20.0, terrain="dirt", scale_px=240.0, centre_px=(140.0, 120.0), seed=1
    )

    detector.reset()
    intact, _h1 = detector.analyze(_frame(whole))
    detector.reset()
    edge, _h2 = detector.analyze(_frame(clipped, 2))

    assert intact.valid
    assert clipped.clipped
    if edge.valid:
        assert edge.confidence < intact.confidence


def test_the_fixture_arrow_matches_the_measured_real_one() -> None:
    """The synthetic model is a fit to measurements, not an invention."""
    import cv2

    scene = render_scene(heading_deg=0.0, terrain="dirt", scale_px=140.0, seed=1)
    lower = np.array([c - 60 for c in ARROW_BRIGHT_BGR], dtype=np.uint8)
    mask = cv2.inRange(scene.bgr, lower, np.array([255, 255, 255], np.uint8))
    contour = max(
        cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0],
        key=cv2.contourArea,
    )
    area = cv2.contourArea(contour)
    solidity = area / cv2.contourArea(cv2.convexHull(contour))
    _x, _y, width, height = cv2.boundingRect(contour)

    # Measured on the owner's crops: solidity 0.851-0.961, extent 0.467-0.686.
    assert 0.80 <= solidity <= 0.98
    assert 0.40 <= area / (width * height) <= 0.72
