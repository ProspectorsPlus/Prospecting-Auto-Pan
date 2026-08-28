"""The production arrow detector, on rendered stress frames.

Rendered frames are **training stress, never a held-out split** (plan 7.2):
these tests pin down behaviour on conditions that can be generated exactly -
every heading, every terrain, a clipped arrow, a tie - and the real-frame
corpus in ``test_corpus.py`` measures what the detector does on the game.

Three contracts run through this file:

* an identity is **earned** over several consistent frames, so single-frame
  helpers here feed the detector a short run;
* everything derived from an observation comes from the one **selected**
  candidate, never from the first that cleared the threshold;
* temporal state advances **once per unique frame**, however many proposal
  passes contributed.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from prospector_engine.arrow import (
    ArrowDetector,
    DetectorConfig,
    DirectionEstimator,
    TrackState,
    circular_consensus,
    wrap_deg,
)
from prospector_engine.contracts import CapturedFrame, freeze_array
from prospector_engine.vision import load_profiles
from tests.arrow_fixtures import ARROW_BRIGHT_BGR, render_scene
from tests.fakes import make_geometry

PROFILE = load_profiles().get("green_arrow_v1")
assert PROFILE is not None

#: Frame spacing for the rendered runs. Fifty milliseconds, so the two-frame
#: acquisition floor and the 40 ms time floor are both met on the second frame.
FRAME_S = 0.05


def _frame(scene: object, sequence: int = 1) -> CapturedFrame:
    image = np.ascontiguousarray(scene.bgr)  # type: ignore[attr-defined]
    return CapturedFrame(
        sequence=sequence,
        captured_at_s=float(sequence) * FRAME_S,
        completed_at_s=float(sequence) * FRAME_S + 0.002,
        duration_ms=2.0,
        geometry=make_geometry(),
        bgr=freeze_array(image),
        backend="synthetic",
    )


def _acquire(
    detector: ArrowDetector, scene: object, *, start: int = 1, frames: int = 3
) -> tuple[object, tuple[object, ...]]:
    """Feed the same scene for a short run and return the last observation."""
    arrow, hypotheses = detector.analyze(_frame(scene, start))
    for step in range(1, frames):
        arrow, hypotheses = detector.analyze(_frame(scene, start + step))
    return (arrow, hypotheses)


def _selected(hypotheses: tuple[object, ...]) -> object | None:
    return next((h for h in hypotheses if h.state == "selected"), None)  # type: ignore[attr-defined]


def _read(
    detector: ArrowDetector, estimator: DirectionEstimator, scene: object
) -> tuple[object, object]:
    arrow, hypotheses = _acquire(detector, scene)
    if not arrow.valid:  # type: ignore[attr-defined]
        return (arrow, None)
    selected = _selected(hypotheses)
    assert selected is not None, "a valid observation always names its selected candidate"
    result = estimator.estimate(
        selected.features,  # type: ignore[attr-defined]
        anchor_px=(640.0, 430.0),
        forward_deg=0.0,
        arrow_confidence=arrow.confidence,  # type: ignore[attr-defined]
    )
    return (arrow, result.observation)


def _pair() -> tuple[ArrowDetector, DirectionEstimator]:
    config = DetectorConfig()
    return (ArrowDetector(PROFILE, config), DirectionEstimator(config))


# ---------------------------------------------------------------------------
# The daylight failure
# ---------------------------------------------------------------------------


def test_a_large_matching_terrain_region_does_not_beat_the_small_real_arrow() -> None:
    """The daylight bug, reproduced and required to come out the other way."""
    detector, estimator = _pair()
    scene = render_scene(heading_deg=40.0, terrain="grass", scale_px=95.0, seed=3)

    arrow, direction = _read(detector, estimator, scene)

    assert arrow.valid, f"the arrow was lost: {arrow.abstain_reason}"  # type: ignore[attr-defined]
    assert arrow.centroid_px is not None  # type: ignore[attr-defined]
    assert math.dist(arrow.centroid_px, (640.0, 360.0)) < 60.0  # type: ignore[attr-defined]
    assert direction is not None and direction.valid  # type: ignore[attr-defined]


def test_colour_alone_cannot_separate_the_arrow_from_the_grass() -> None:
    """The premise of the whole design, asserted rather than assumed."""
    detector, _estimator = _pair()
    scene = render_scene(heading_deg=0.0, terrain="grass", scale_px=95.0, seed=4)
    channels = detector._channels(np.asarray(_frame(scene).bgr))
    mask = detector._rule_mask(channels)

    coverage = float(mask.mean())
    assert coverage > 0.5, "if colour alone worked, the rest of the detector would be optional"


def test_a_component_welded_to_terrain_is_split_rather_than_rejected() -> None:
    detector, estimator = _pair()
    scene = render_scene(heading_deg=115.0, terrain="grass", scale_px=110.0, seed=5)

    arrow, direction = _read(detector, estimator, scene)

    assert arrow.valid  # type: ignore[attr-defined]
    assert direction is not None and direction.valid  # type: ignore[attr-defined]
    assert abs(wrap_deg(direction.error_deg - 115.0)) < 10.0  # type: ignore[attr-defined]


def test_confidence_is_a_breakdown_of_independent_evidence_not_an_area_fit() -> None:
    detector, _estimator = _pair()
    scene = render_scene(heading_deg=20.0, terrain="dirt", scale_px=100.0, seed=6)
    arrow, hypotheses = _acquire(detector, scene)

    assert arrow.valid  # type: ignore[attr-defined]
    names = {name for name, _value in arrow.score_terms}  # type: ignore[attr-defined]
    assert {"contrast", "topology", "tip", "solidity", "boundary", "chroma"} <= names
    assert all(0.0 <= value <= 1.0 for _name, value in arrow.score_terms)  # type: ignore[attr-defined]
    assert hypotheses[0].weakest_term in names  # type: ignore[attr-defined]


def test_a_rejected_candidate_records_why_it_was_rejected() -> None:
    detector, _estimator = _pair()
    scene = render_scene(
        heading_deg=0.0, terrain="dirt", scale_px=90.0, distractors=3, arrow=False, seed=7
    )
    proposals = detector.propose(_frame(scene))

    assert proposals.hypotheses, "the discs are proposed, so their rejection is recorded"
    for hypothesis in proposals.hypotheses:
        assert not hypothesis.accepted
        assert hypothesis.reason
        assert hypothesis.state == "proposed"


def test_an_arrow_coloured_disc_field_is_not_an_arrow() -> None:
    """Right colour, right brightness, round: below threshold.

    Topology is no longer a veto on its own - real outlines are nicked by UI
    strokes and the notch pair is misread - so the extent, circularity, tip
    and solidity bands carry this. Overlapping clusters of arrow-coloured
    discs are deliberately *not* tested here: they satisfy every cheap term
    an arrow does, no real frame has produced one, and the honest negative
    evidence is the real corpus (``test_corpus.py``).
    """
    detector, _estimator = _pair()
    scene = render_scene(
        heading_deg=0.0, terrain="dirt", scale_px=90.0, distractors=3, arrow=False, seed=8
    )

    arrow, _hypotheses = _acquire(detector, scene)

    assert not arrow.valid, "same colour, same brightness, round: not an arrow"  # type: ignore[attr-defined]


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

    assert arrow.valid, f"{heading} deg: {arrow.abstain_reason}"  # type: ignore[attr-defined]
    assert direction is not None and direction.valid, f"{heading} deg abstained"  # type: ignore[attr-defined]
    assert abs(wrap_deg(direction.error_deg - heading)) <= 5.0  # type: ignore[attr-defined]


@pytest.mark.parametrize("heading", [175.0, 178.0, 179.5, 180.0, -179.5, -178.0, -175.0])
def test_the_plus_minus_180_seam_is_handled(heading: float) -> None:
    detector, estimator = _pair()
    scene = render_scene(heading_deg=heading, terrain="dirt", scale_px=100.0, seed=9)

    _arrow, direction = _read(detector, estimator, scene)

    assert direction is not None and direction.valid  # type: ignore[attr-defined]
    assert abs(wrap_deg(direction.error_deg - heading)) <= 5.0  # type: ignore[attr-defined]
    assert -180.0 < direction.error_deg <= 180.0  # type: ignore[attr-defined]


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
        if direction is None or not direction.valid:  # type: ignore[attr-defined]
            continue
        error = abs(wrap_deg(direction.error_deg - heading))  # type: ignore[attr-defined]
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
        if direction is None or not direction.valid:  # type: ignore[attr-defined]
            continue
        error = abs(wrap_deg(direction.error_deg - heading))  # type: ignore[attr-defined]
        assert error <= 90.0, f"{label} at {heading} deg flipped by {error:.0f} deg"


# ---------------------------------------------------------------------------
# Abstention
# ---------------------------------------------------------------------------


def test_an_arrow_absent_frame_acquires_nothing() -> None:
    detector, _estimator = _pair()
    for terrain in ("dirt", "grass", "water", "pale"):
        scene = render_scene(heading_deg=0.0, terrain=terrain, arrow=False, seed=11)
        detector.reset()
        arrow, _hypotheses = _acquire(detector, scene)
        assert not arrow.valid, f"{terrain}: acquired an arrow that is not there"  # type: ignore[attr-defined]


def test_a_plausible_tie_abstains_rather_than_guessing() -> None:
    """Two arrows at once is ambiguous. Picking one is the failure mode."""
    detector, _estimator = _pair()
    first = render_scene(heading_deg=30.0, terrain="dirt", scale_px=100.0, seed=12)
    second = render_scene(
        heading_deg=30.0, terrain="dirt", scale_px=100.0, centre_px=(320.0, 200.0), seed=12
    )
    combined = np.maximum(first.bgr, second.bgr)
    scene = type(first)(
        bgr=combined,
        heading_deg=30.0,
        centre_px=first.centre_px,
        scale_px=100.0,
        terrain="dirt",
        clipped=False,
        alpha=1.0,
    )

    arrow, hypotheses = _acquire(detector, scene)

    accepted = [h for h in hypotheses if h.accepted]  # type: ignore[attr-defined]
    if len(accepted) > 1 and accepted[0].score - accepted[1].score < 0.10:  # type: ignore[attr-defined]
        assert not arrow.valid  # type: ignore[attr-defined]
        assert arrow.abstain_reason == "ambiguous-candidates"  # type: ignore[attr-defined]


def test_the_pca_axis_is_refused_when_it_is_ill_conditioned() -> None:
    """The measured elongation of this arrow is 1.3, which is not an axis."""
    detector, estimator = _pair()
    scene = render_scene(heading_deg=60.0, terrain="dirt", scale_px=110.0, seed=13)
    arrow, hypotheses = _acquire(detector, scene)
    assert arrow.valid  # type: ignore[attr-defined]
    selected = _selected(hypotheses)
    assert selected is not None
    result = estimator.estimate(
        selected.features,
        anchor_px=(640.0, 430.0),
        forward_deg=0.0,
        arrow_confidence=0.9,  # type: ignore[attr-defined]
    )

    pca = next((c for c in result.readings if c.cue_id == "pca_axis"), None)
    assert pca is not None
    if selected.features.anisotropy < DetectorConfig().min_anisotropy:  # type: ignore[attr-defined]
        assert not pca.valid
        assert "anisotropy" in pca.note


def test_position_and_pose_are_reported_as_different_things() -> None:
    detector, estimator = _pair()
    scene = render_scene(
        heading_deg=90.0, terrain="dirt", scale_px=100.0, centre_px=(400.0, 250.0), seed=14
    )
    arrow, hypotheses = _acquire(detector, scene)
    selected = _selected(hypotheses)
    assert selected is not None
    result = estimator.estimate(
        selected.features,
        anchor_px=(640.0, 430.0),
        forward_deg=0.0,
        arrow_confidence=0.9,  # type: ignore[attr-defined]
    )

    position = next(c for c in result.readings if c.cue_id == "player_to_arrow")
    pose = next(c for c in result.readings if c.cue_id == "notch_axis")
    assert position.heading_deg != pose.heading_deg
    # Position is reported but never votes: it answers a different question.
    assert position.weight == 0.0 and not position.valid
    assert arrow.valid  # type: ignore[attr-defined]


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
# Identity: earned, held, challenged
# ---------------------------------------------------------------------------


def _moving(
    sequence: int, *, centre: tuple[float, float], heading: float = 30.0, seed: int = 1
) -> object:
    return render_scene(
        heading_deg=heading, terrain="dirt", scale_px=100.0, centre_px=centre, seed=seed
    )


def test_acquisition_needs_several_consistent_frames() -> None:
    """One frame is a proposal. An identity is reported only once it repeats."""
    detector, _estimator = _pair()
    scene = render_scene(heading_deg=30.0, terrain="dirt", scale_px=100.0, seed=1)

    first, _h1 = detector.analyze(_frame(scene, 1))
    assert not first.valid and first.abstain_reason is not None
    assert first.abstain_reason.startswith("acquiring")
    assert detector.state is TrackState.ACQUIRE

    second, _h2 = detector.analyze(_frame(scene, 2))
    assert second.valid
    assert detector.state is TrackState.TRACK
    assert second.track_id == 1


def test_a_track_holds_across_frames_and_reports_its_age() -> None:
    detector, _estimator = _pair()
    identities = []
    for sequence in range(1, 7):
        scene = _moving(sequence, centre=(600.0 + sequence * 6, 360.0), heading=30.0 + sequence)
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

    for sequence in range(5, 18):
        empty = render_scene(heading_deg=0.0, terrain="dirt", arrow=False, seed=2)
        arrow, _hypotheses = detector.analyze(_frame(empty, sequence))
        assert not arrow.valid
    # Over half a second without evidence: the identity is in REACQUIRE and
    # the prediction is withdrawn; nothing is reported as seen.
    assert detector.predicted_centroid() is None, "a track outlived its evidence"
    assert detector.state is not TrackState.TRACK


def test_temporal_state_advances_once_per_unique_frame() -> None:
    """A region pass and a full pass on one screenshot are one transaction."""
    detector, _estimator = _pair()
    scene = render_scene(heading_deg=30.0, terrain="dirt", scale_px=100.0, seed=1)
    frame = _frame(scene, 1)
    region = detector.propose(frame, roi_px=(540, 260, 200, 200))
    whole = detector.propose(frame)

    outcome = detector.commit(frame, [region, whole])

    assert outcome.decision == "acquiring"
    with pytest.raises(ValueError, match="twice"):
        detector.commit(frame, [whole])


def test_a_region_pass_reports_full_frame_coordinates_and_no_false_clipping() -> None:
    """An arrow touching the edge of a search region is not clipped."""
    detector, _estimator = _pair()
    scene = render_scene(heading_deg=30.0, terrain="dirt", scale_px=100.0, seed=1)
    frame = _frame(scene, 1)
    whole = max(detector.propose(frame).hypotheses, key=lambda h: h.score)
    # A region whose right edge cuts through the arrow's bounding box.
    x, y, w, h = whole.features.bbox_px
    region = detector.propose(frame, roi_px=(x - 40, y - 40, w // 2 + 40, h + 80))
    partial = max(region.hypotheses, key=lambda hyp: hyp.score)

    assert partial.features.bbox_px[0] == pytest.approx(x, abs=2)
    assert not partial.features.clipped, "region edges are not frame edges"
    assert partial.features.bbox_px[0] >= x - 40


def test_exclusion_regions_apply_inside_a_search_region() -> None:
    """Exclusions are full-frame rectangles; a region pass translates them."""
    scene = render_scene(heading_deg=30.0, terrain="dirt", scale_px=100.0, seed=1)
    frame = _frame(scene, 1)
    plain = ArrowDetector(PROFILE, DetectorConfig())
    x, y, w, h = max(
        plain.propose(frame).hypotheses, key=lambda hyp: hyp.score
    ).features.bbox_px
    excluding = ArrowDetector(
        PROFILE, DetectorConfig(), exclusion_regions_px=((x - 10, y - 10, w + 20, h + 20),)
    )

    region = excluding.propose(frame, roi_px=(x - 100, y - 100, w + 200, h + 200))

    assert not any(
        hyp.features.bbox_px[0] >= x - 10 and hyp.features.bbox_px[0] <= x + w
        for hyp in region.hypotheses
    ), "the excluded arrow was proposed from inside the region"


def test_a_distractor_outside_the_gate_cannot_take_the_identity_in_one_frame() -> None:
    """An out-of-gate best candidate is a challenger, not a replacement."""
    detector, _estimator = _pair()
    for sequence in range(1, 4):
        scene = _moving(sequence, centre=(400.0, 300.0))
        arrow, _h = detector.analyze(_frame(scene, sequence))
    assert arrow.valid
    held = arrow.track_id
    # A second, larger arrow appears far away and outscores the held one.
    first = _moving(4, centre=(400.0, 300.0))
    rival = render_scene(
        heading_deg=30.0, terrain="dirt", scale_px=150.0, centre_px=(1000.0, 550.0), seed=1
    )
    combined = type(first)(
        bgr=np.maximum(first.bgr, rival.bgr),
        heading_deg=30.0,
        centre_px=first.centre_px,
        scale_px=100.0,
        terrain="dirt",
        clipped=False,
        alpha=1.0,
    )

    arrow, hypotheses = detector.analyze(_frame(combined, 4))

    assert arrow.valid and arrow.track_id == held
    assert arrow.centroid_px is not None and arrow.centroid_px[0] < 600.0
    selected = _selected(hypotheses)
    assert selected is not None and selected.features.centroid_px[0] < 600.0
    assert detector.switches == 0


def test_the_selected_candidate_is_the_tracked_one_not_the_global_best() -> None:
    """Direction, contour and box all come from the held identity."""
    detector, _estimator = _pair()
    for sequence in range(1, 4):
        detector.analyze(_frame(_moving(sequence, centre=(400.0, 300.0)), sequence))
    first = _moving(4, centre=(400.0, 300.0))
    rival = render_scene(
        heading_deg=200.0, terrain="dirt", scale_px=150.0, centre_px=(1000.0, 550.0), seed=1
    )
    combined = type(first)(
        bgr=np.maximum(first.bgr, rival.bgr),
        heading_deg=30.0,
        centre_px=first.centre_px,
        scale_px=100.0,
        terrain="dirt",
        clipped=False,
        alpha=1.0,
    )

    arrow, hypotheses = detector.analyze(_frame(combined, 4))

    selected = [h for h in hypotheses if h.state == "selected"]
    assert len(selected) == 1, "exactly one candidate is selected per observation"
    assert selected[0].features.bbox_px == arrow.bbox_px
    assert selected[0].features.centroid_px == arrow.centroid_px
    assert sum(1 for h in hypotheses if h.accepted) >= 1
    assert not any(
        h.state == "selected" and h.features.centroid_px[0] > 600.0 for h in hypotheses
    )


def test_an_arrow_ranked_below_the_presentation_top_k_stays_trackable() -> None:
    """Presentation truncation happens after association, never before."""
    config = DetectorConfig(top_k=1)
    detector = ArrowDetector(PROFILE, config)
    for sequence in range(1, 4):
        detector.analyze(_frame(_moving(sequence, centre=(400.0, 300.0)), sequence))
    first = _moving(4, centre=(400.0, 300.0))
    rival = render_scene(
        heading_deg=30.0, terrain="dirt", scale_px=150.0, centre_px=(1000.0, 550.0), seed=1
    )
    combined = type(first)(
        bgr=np.maximum(first.bgr, rival.bgr),
        heading_deg=30.0,
        centre_px=first.centre_px,
        scale_px=100.0,
        terrain="dirt",
        clipped=False,
        alpha=1.0,
    )

    arrow, shown = detector.analyze(_frame(combined, 4))

    assert arrow.valid and arrow.centroid_px is not None and arrow.centroid_px[0] < 600.0
    assert any(h.state == "selected" for h in shown), "the selected candidate is always shown"


def test_a_periodic_global_search_challenges_a_healthy_track_without_stealing_it() -> None:
    config = DetectorConfig(reacquire_every_s=0.1)
    detector = ArrowDetector(PROFILE, config)
    identities = set()
    globals_requested = 0
    for sequence in range(1, 12):
        scene = _moving(sequence, centre=(400.0 + sequence * 2, 300.0))
        frame = _frame(scene, sequence)
        if detector.wants_global_search():
            globals_requested += 1
            proposals = detector.propose(frame)
        else:
            predicted = detector.predicted_centroid()
            assert predicted is not None
            region = (int(predicted[0]) - 150, int(predicted[1]) - 150, 300, 300)
            proposals = detector.propose(frame, roi_px=region)
        outcome = detector.commit(frame, [proposals])
        if outcome.observation.valid:
            identities.add(outcome.observation.track_id)
    assert globals_requested >= 3, "the periodic challenge ran"
    assert len(identities) == 1, f"a global search stole the identity: {identities}"
    assert detector.switches == 0


def test_resetting_the_detector_drops_every_piece_of_temporal_state() -> None:
    detector, _estimator = _pair()
    scene = render_scene(heading_deg=25.0, terrain="dirt", scale_px=100.0, seed=1)
    _acquire(detector, scene)
    assert detector.track_id is not None

    detector.reset()

    assert detector.track_id is None
    assert detector.predicted_centroid() is None
    assert detector.track_age == 0
    assert detector.state is TrackState.ACQUIRE


# ---------------------------------------------------------------------------
# Profile contracts
# ---------------------------------------------------------------------------


def test_the_profile_area_and_aspect_bounds_are_enforced() -> None:
    from dataclasses import replace

    scene = render_scene(heading_deg=30.0, terrain="dirt", scale_px=100.0, seed=1)
    frame = _frame(scene, 1)
    baseline = ArrowDetector(PROFILE, DetectorConfig()).propose(frame)
    area = max(baseline.hypotheses, key=lambda h: h.score).features.area_px

    too_small = replace(PROFILE, min_area_px=int(area * 2))
    assert not ArrowDetector(too_small, DetectorConfig()).propose(frame).hypotheses
    too_large = replace(PROFILE, max_area_px=int(area / 2))
    assert not ArrowDetector(too_large, DetectorConfig()).propose(frame).hypotheses
    wrong_aspect = replace(PROFILE, min_aspect=3.0, max_aspect=5.0)
    assert not ArrowDetector(wrong_aspect, DetectorConfig()).propose(frame).hypotheses


def test_an_unsupported_viewport_size_abstains_instead_of_rescaling() -> None:
    from dataclasses import replace

    import cv2

    from prospector_engine.geometry import ViewportState

    detector, _estimator = _pair()
    scene = render_scene(heading_deg=30.0, terrain="dirt", scale_px=100.0, seed=1)
    frame = _frame(scene, 1)
    odd = replace(
        frame,
        geometry=make_geometry(
            size=(1600.0, 900.0),
            canonical_px=(1600, 900),
            state=ViewportState.ADOPTED_NONCANONICAL,
        ),
        bgr=freeze_array(np.ascontiguousarray(cv2.resize(scene.bgr, (1600, 900)))),
    )

    proposals = detector.propose(odd)

    assert proposals.abstain_reason == "unsupported-viewport-size"
    assert not proposals.hypotheses


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
    intact, _h1 = _acquire(detector, whole)
    detector.reset()
    edge, _h2 = _acquire(detector, clipped, start=10)

    assert intact.valid  # type: ignore[attr-defined]
    assert clipped.clipped
    if edge.valid:  # type: ignore[attr-defined]
        assert edge.confidence < intact.confidence  # type: ignore[attr-defined]


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

    assert 0.80 <= solidity <= 0.98
    assert 0.40 <= area / (width * height) <= 0.72
