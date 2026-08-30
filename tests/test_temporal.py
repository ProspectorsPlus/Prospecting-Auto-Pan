"""The temporal bridge: what it carries, what it refuses, and what it decays to.

Rendered frames throughout, and CLAUDE.md rule 9 applies to every one of them:
this file is deterministic *stress*, never held-out validation. What it can
honestly establish is behaviour under conditions we control - a bridge fires
only inside its horizon, a rival peak makes it abstain, confidence falls on the
monotonic clock, a world change resets it. What it cannot establish is recall
on a real green map at 60 Hz, and nothing here is reported as though it does.

The cadences (30/60/90 Hz) are real parameters rather than decoration: every
bound in :class:`TemporalConfig` is in seconds, and a bound in seconds that
behaves differently at two frame rates is a bug this file is meant to catch.
"""

from __future__ import annotations

import math

import cv2
import numpy as np
import pytest

from prospector_engine.arrow import heading_deg
from prospector_engine.contracts import (
    ArrowObservation,
    CapturedFrame,
    EvidenceProvenance,
    freeze_array,
)
from prospector_engine.temporal import TemporalBridge, TemporalConfig, _rotate_unit
from tests.fakes import make_geometry

CANVAS = (1280, 720)


# ---------------------------------------------------------------------------
# A rendered arrow we can move and turn on demand
# ---------------------------------------------------------------------------


def _arrow_polygon(
    centre: tuple[float, float], *, scale: float, rotation_deg: float
) -> np.ndarray:
    """A notched arrowhead, centred, pointing up at rotation zero.

    Deliberately asymmetric front-to-back so a correlation peak has something
    to lock onto: a symmetric blob correlates equally well at every rotation
    and would make the rotation assertions vacuous.
    """
    base = np.array(
        [
            (0.0, -1.0),
            (0.62, 0.42),
            (0.24, 0.22),
            (0.18, 0.95),
            (-0.18, 0.95),
            (-0.24, 0.22),
            (-0.62, 0.42),
        ],
        dtype=np.float64,
    )
    radians = math.radians(rotation_deg)
    cos_a, sin_a = math.cos(radians), math.sin(radians)
    # Screen space is y-down, so a *clockwise* screen turn is the positive
    # mathematical rotation of (x, y). Same convention as ``heading_deg``.
    turned = np.stack(
        [
            base[:, 0] * cos_a - base[:, 1] * sin_a,
            base[:, 0] * sin_a + base[:, 1] * cos_a,
        ],
        axis=1,
    )
    return np.round(turned * scale + np.array(centre)).astype(np.int32)


def _render(
    shapes: list[tuple[tuple[float, float], float, float]],
    *,
    sequence: int,
    at_s: float,
    background: int = 40,
    noise: int = 0,
    seed: int = 7,
) -> CapturedFrame:
    """One frame with the given arrows drawn on a textured ground."""
    width, height = CANVAS
    rng = np.random.default_rng(seed)
    canvas = np.full((height, width, 3), background, dtype=np.uint8)
    # A little fixed texture, so the ground is not a zero-variance plane that
    # every normalised correlation would score as a perfect match.
    texture = rng.integers(0, 22, size=(height, width, 1), dtype=np.uint8)
    canvas = np.clip(canvas.astype(np.int16) + texture, 0, 255).astype(np.uint8)
    for centre, scale, rotation in shapes:
        polygon = _arrow_polygon(centre, scale=scale, rotation_deg=rotation)
        cv2.fillPoly(canvas, [polygon], (60, 210, 235))
    if noise:
        grain = rng.integers(-noise, noise + 1, size=canvas.shape, dtype=np.int16)
        canvas = np.clip(canvas.astype(np.int16) + grain, 0, 255).astype(np.uint8)
    return CapturedFrame(
        sequence=sequence,
        captured_at_s=at_s,
        completed_at_s=at_s,
        duration_ms=1.0,
        geometry=make_geometry(size=(float(CANVAS[0]), float(CANVAS[1]))),
        bgr=freeze_array(canvas),
    )


def _observation(
    centre: tuple[float, float],
    *,
    scale: float,
    rotation_deg: float = 0.0,
    track_id: int = 1,
    confidence: float = 0.9,
) -> ArrowObservation:
    """What a global commit for that rendered arrow would have looked like."""
    polygon = _arrow_polygon(centre, scale=scale, rotation_deg=rotation_deg)
    x, y, width, height = cv2.boundingRect(polygon)
    axis = _rotate_unit((0.0, -1.0), rotation_deg)
    return ArrowObservation(
        profile_id="test",
        track_id=track_id,
        bbox_px=(x, y, width, height),
        centroid_px=centre,
        tip_px=None,
        axis_unit_xy=axis,
        confidence=confidence,
        valid=True,
    )


def _anchor(bridge: TemporalBridge, centre=(600.0, 340.0), *, scale=34.0, at_s=0.0):
    frame = _render([(centre, scale, 0.0)], sequence=1, at_s=at_s)
    provenance = bridge.validate(frame, _observation(centre, scale=scale))
    assert provenance is EvidenceProvenance.GLOBAL
    return frame


def _carried(bridge: TemporalBridge, frame: CapturedFrame):
    """The measurement only if pixels were actually correlated for it.

    ``PREDICTED`` is not a refusal *and not evidence*: it is the bounded "no
    measurement, here is where it would be" state, and downstream reads it as
    an abstention with an ROI hint. Tests about what the bridge will *carry*
    have to distinguish the two, so they say which one they mean here rather
    than asserting against ``None`` and quietly passing on a prediction.
    """
    measurement = bridge.bridge(frame)
    if measurement is None or not measurement.measured:
        return None
    return measurement


# ---------------------------------------------------------------------------
# The sign of the rotation, pinned rather than argued
# ---------------------------------------------------------------------------


def test_rotate_unit_turns_clockwise_on_screen_in_the_heading_convention() -> None:
    """+30 degrees of rotation must raise ``heading_deg`` by 30 degrees.

    Getting this backwards would steer the character away from the treasure
    while every other test still passed, so it is asserted against the real
    heading function rather than reasoned about in a comment.
    """
    up = (0.0, -1.0)
    assert heading_deg(up) == pytest.approx(0.0, abs=1e-6)
    turned = _rotate_unit(up, 30.0)
    assert turned is not None
    assert heading_deg(turned) == pytest.approx(30.0, abs=1e-6)
    back = _rotate_unit(up, -45.0)
    assert back is not None
    assert heading_deg(back) == pytest.approx(-45.0, abs=1e-6)


def test_the_bridge_measures_a_rotation_in_the_same_sense() -> None:
    """A rendered arrow turned +18 degrees is reported as roughly +18."""
    bridge = TemporalBridge()
    _anchor(bridge, at_s=0.0)
    frame = _render([((600.0, 340.0), 34.0, 18.0)], sequence=2, at_s=1 / 60)

    measurement = bridge.bridge(frame)

    assert measurement is not None
    assert measurement.provenance is EvidenceProvenance.BRIDGED
    assert measurement.rotation_deg == pytest.approx(18.0, abs=6.0)
    assert measurement.axis_unit_xy is not None
    assert heading_deg(measurement.axis_unit_xy) == pytest.approx(18.0, abs=6.0)


# ---------------------------------------------------------------------------
# What it carries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hz", [30.0, 60.0, 90.0])
def test_a_moving_arrow_is_bridged_at_every_cadence(hz: float) -> None:
    """The same seconds-denominated horizon holds at 30, 60 and 90 Hz."""
    bridge = TemporalBridge()
    dt = 1.0 / hz
    centre = (600.0, 340.0)
    _anchor(bridge, centre, at_s=0.0)

    speed_px_s = 240.0
    carried = 0
    for index in range(1, 4):
        at_s = index * dt
        moved = (centre[0] + speed_px_s * at_s, centre[1])
        frame = _render([(moved, 34.0, 0.0)], sequence=index + 1, at_s=at_s)
        measurement = bridge.bridge(frame)
        assert measurement is not None, f"abstained at {hz} Hz on step {index}"
        assert measurement.provenance is EvidenceProvenance.BRIDGED
        assert measurement.centroid_px[0] == pytest.approx(moved[0], abs=8.0)
        assert measurement.centroid_px[1] == pytest.approx(moved[1], abs=8.0)
        carried += 1
    assert carried == 3


def test_a_partially_occluded_arrow_is_still_bridged() -> None:
    """A bar across a third of the arrow is exactly what the bridge is for."""
    bridge = TemporalBridge()
    centre = (600.0, 340.0)
    _anchor(bridge, centre, at_s=0.0)

    frame = _render([(centre, 34.0, 0.0)], sequence=2, at_s=1 / 60)
    occluded = np.array(frame.bgr, dtype=np.uint8, copy=True)
    cv2.rectangle(occluded, (560, 300), (640, 322), (30, 90, 40), thickness=-1)
    frame = CapturedFrame(
        sequence=2,
        captured_at_s=1 / 60,
        completed_at_s=1 / 60,
        duration_ms=1.0,
        geometry=frame.geometry,
        bgr=freeze_array(occluded),
    )

    measurement = bridge.bridge(frame)

    assert measurement is not None
    assert measurement.provenance is EvidenceProvenance.BRIDGED
    assert measurement.centroid_px[0] == pytest.approx(centre[0], abs=10.0)


# ---------------------------------------------------------------------------
# What it refuses
# ---------------------------------------------------------------------------


def test_an_identical_neighbour_makes_the_bridge_abstain() -> None:
    """Two indistinguishable peaks is the foliage case. It must not pick one.

    Being near the predicted position is the *search bound*, not a tie-break -
    so a second identical arrow inside the window has to produce an abstention
    rather than a confident lock on whichever one happened to be closer.
    """
    bridge = TemporalBridge()
    centre = (600.0, 340.0)
    _anchor(bridge, centre, at_s=0.0)

    frame = _render([(centre, 34.0, 0.0), ((648.0, 340.0), 34.0, 0.0)], sequence=2, at_s=1 / 60)
    measurement = _carried(bridge, frame)

    assert measurement is None
    assert bridge.last_refusal is not None
    assert bridge.last_refusal.startswith("ambiguous")
    assert bridge.stats.refused_ambiguous == 1


def test_an_empty_frame_is_refused_rather_than_matched_to_ground() -> None:
    bridge = TemporalBridge()
    _anchor(bridge, at_s=0.0)

    measurement = _carried(bridge, _render([], sequence=2, at_s=1 / 60))

    assert measurement is None
    assert bridge.stats.bridged == 0
    # Prediction-only may still answer inside its short horizon, and that is
    # the designed behaviour: it is an ROI hint carrying an explicit "nothing
    # was measured", not a claim that an arrow is there.
    assert bridge.provenance in (
        EvidenceProvenance.PREDICTED,
        EvidenceProvenance.STALE,
    )


def test_a_teleport_beyond_the_speed_limit_is_refused() -> None:
    """1400 px/s is the blind-motion ceiling; 700 px in 16 ms is not motion."""
    bridge = TemporalBridge(TemporalConfig(search_radius_max_px=900.0))
    centre = (300.0, 340.0)
    _anchor(bridge, centre, at_s=0.0)

    frame = _render([((1000.0, 340.0), 34.0, 0.0)], sequence=2, at_s=1 / 60)
    measurement = _carried(bridge, frame)

    assert measurement is None
    assert bridge.stats.bridged == 0


# ---------------------------------------------------------------------------
# Horizons, decay, and the blind-motion limit
# ---------------------------------------------------------------------------


def test_confidence_decays_on_the_monotonic_clock() -> None:
    bridge = TemporalBridge()
    centre = (600.0, 340.0)
    _anchor(bridge, centre, at_s=0.0)

    seen: list[float] = []
    for index in range(1, 8):
        at_s = index * (1 / 60)
        frame = _render([(centre, 34.0, 0.0)], sequence=index + 1, at_s=at_s)
        measurement = bridge.bridge(frame)
        if measurement is None:
            break
        seen.append(measurement.confidence)

    assert len(seen) >= 4
    assert seen == sorted(seen, reverse=True), seen
    assert seen[-1] < seen[0]


def test_the_bridge_expires_at_its_horizon_and_says_so() -> None:
    """Past ``max_bridge_s`` the answer is STALE, not a quieter yes."""
    config = TemporalConfig()
    bridge = TemporalBridge(config)
    centre = (600.0, 340.0)
    _anchor(bridge, centre, at_s=0.0)

    late = config.max_bridge_s + 0.05
    frame = _render([(centre, 34.0, 0.0)], sequence=99, at_s=late)
    measurement = bridge.bridge(frame)

    assert measurement is None
    assert bridge.provenance is EvidenceProvenance.STALE
    assert bridge.stats.expired_horizon == 1


def test_prediction_only_is_short_and_visible() -> None:
    """No pixels compared means PREDICTED, a lower confidence, and a short leash."""
    config = TemporalConfig(min_correlation=0.999, peak_margin=0.999)
    bridge = TemporalBridge(config)
    centre = (600.0, 340.0)
    _anchor(bridge, centre, at_s=0.0)

    inside = bridge.bridge(_render([(centre, 34.0, 0.0)], sequence=2, at_s=0.02))
    assert inside is not None
    assert inside.provenance is EvidenceProvenance.PREDICTED
    assert inside.correlation == 0.0

    bridge.reset("test")
    _anchor(bridge, centre, at_s=0.0)
    beyond = bridge.bridge(
        _render([(centre, 34.0, 0.0)], sequence=3, at_s=config.max_predict_s + 0.02)
    )
    assert beyond is None
    assert bridge.provenance is EvidenceProvenance.STALE


def test_cumulative_drift_is_capped_even_while_every_step_is_clean() -> None:
    """Each hop is legal; the run is not. The budget is what ends it."""
    config = TemporalConfig(max_bridge_s=6.0, confidence_half_life_s=60.0)
    bridge = TemporalBridge(config)
    centre = (200.0, 340.0)
    _anchor(bridge, centre, at_s=0.0)

    x = centre[0]
    for index in range(1, 80):
        at_s = index * (1 / 60)
        x += 14.0
        measurement = _carried(
            bridge, _render([((x, 340.0), 34.0, 0.0)], sequence=index + 1, at_s=at_s)
        )
        if measurement is None:
            break
    assert bridge.stats.expired_drift >= 1
    assert bridge.drift_px() >= config.max_drift_px


# ---------------------------------------------------------------------------
# The global half corrects the local one
# ---------------------------------------------------------------------------


def test_a_global_commit_that_agrees_is_reported_as_fused() -> None:
    bridge = TemporalBridge()
    centre = (600.0, 340.0)
    _anchor(bridge, centre, at_s=0.0)

    moved = (612.0, 340.0)
    bridged = _carried(bridge, _render([(moved, 34.0, 0.0)], sequence=2, at_s=1 / 60))
    assert bridged is not None

    settled = (624.0, 340.0)
    frame = _render([(settled, 34.0, 0.0)], sequence=3, at_s=2 / 60)
    provenance = bridge.validate(frame, _observation(settled, scale=34.0))

    assert provenance is EvidenceProvenance.FUSED
    assert bridge.stats.corrections == 0


def test_a_global_commit_that_disagrees_corrects_and_re_anchors() -> None:
    """Global wins outright. It is never averaged with the drifted position."""
    config = TemporalConfig(disagreement_px=20.0)
    bridge = TemporalBridge(config)
    centre = (600.0, 340.0)
    _anchor(bridge, centre, at_s=0.0)
    assert _carried(bridge, _render([((614.0, 340.0), 34.0, 0.0)], sequence=2, at_s=1 / 60))

    truth = (760.0, 400.0)
    frame = _render([(truth, 34.0, 0.0)], sequence=3, at_s=2 / 60)
    provenance = bridge.validate(frame, _observation(truth, scale=34.0))

    assert provenance is EvidenceProvenance.GLOBAL
    assert bridge.stats.corrections == 1
    assert bridge.last_disagreement_px is not None
    assert bridge.last_disagreement_px > config.disagreement_px
    # Re-anchored on the global answer: the drift budget starts again.
    assert bridge.drift_px() == 0.0


def test_a_global_commit_resets_the_horizon() -> None:
    """A validated identity gets its full bridge budget back, and only then."""
    config = TemporalConfig()
    bridge = TemporalBridge(config)
    centre = (600.0, 340.0)
    _anchor(bridge, centre, at_s=0.0)

    late = config.max_bridge_s - 0.02
    frame = _render([(centre, 34.0, 0.0)], sequence=2, at_s=late)
    bridge.validate(frame, _observation(centre, scale=34.0))

    ahead = _render([(centre, 34.0, 0.0)], sequence=3, at_s=late + 0.05)
    assert bridge.bridge(ahead) is not None


# ---------------------------------------------------------------------------
# Resets and boundedness
# ---------------------------------------------------------------------------


def test_a_world_change_drops_the_anchor() -> None:
    bridge = TemporalBridge()
    assert not bridge.note_world(("run", 0, "yellow", 1))
    _anchor(bridge, at_s=0.0)
    assert bridge.anchored

    assert bridge.note_world(("run", 1, "yellow", 1))

    assert not bridge.anchored
    assert bridge.stats.resets == 1
    assert bridge.bridge(_render([((600.0, 340.0), 34.0, 0.0)], sequence=9, at_s=0.1)) is None


def test_a_repeated_frame_advances_nothing() -> None:
    """One temporal commit per unique frame, exactly as the detector requires."""
    bridge = TemporalBridge()
    centre = (600.0, 340.0)
    _anchor(bridge, centre, at_s=0.0)

    frame = _render([((612.0, 340.0), 34.0, 0.0)], sequence=2, at_s=1 / 60)
    first = _carried(bridge, frame)
    assert first is not None
    again = bridge.bridge(frame)

    assert again is None
    assert bridge.stats.bridged == 1


def test_the_bridge_holds_no_history_across_a_long_run() -> None:
    """Bounded by construction: counters are integers and there is one anchor."""
    bridge = TemporalBridge()
    centre = (600.0, 340.0)
    for index in range(400):
        at_s = index * (1 / 60)
        frame = _render([(centre, 34.0, 0.0)], sequence=index + 1, at_s=at_s)
        if index % 3 == 0:
            bridge.validate(frame, _observation(centre, scale=34.0))
        else:
            bridge.bridge(frame)

    payload = bridge.__dict__
    for name, value in payload.items():
        assert not isinstance(value, (list, dict, set)), f"{name} grew a container"
    assert bridge.stats.bridged > 0


def test_a_degenerate_frame_abstains_rather_than_raising() -> None:
    """A tick holding a movement lease must never take an exception from here."""
    bridge = TemporalBridge()
    _anchor(bridge, (600.0, 340.0), at_s=0.0)

    flat = _render([], sequence=2, at_s=1 / 60, background=0, noise=0, seed=1)
    zeroed = np.zeros_like(flat.bgr)
    frame = CapturedFrame(
        sequence=2,
        captured_at_s=1 / 60,
        completed_at_s=1 / 60,
        duration_ms=1.0,
        geometry=flat.geometry,
        bgr=freeze_array(zeroed),
    )

    assert _carried(bridge, frame) is None


def test_an_arrow_too_small_to_template_declines_to_anchor_rather_than_guessing() -> None:
    """Below ``template_min_px`` there is no texture to correlate against.

    Anchoring anyway would produce a template that matches flat ground about
    as well as it matches the arrow, which is the one thing a correlator must
    never be handed.
    """
    bridge = TemporalBridge(TemporalConfig(template_min_px=40))
    frame = _render([((600.0, 340.0), 8.0, 0.0)], sequence=1, at_s=0.0)

    bridge.validate(frame, _observation((600.0, 340.0), scale=8.0))

    assert not bridge.anchored


def test_configuration_that_could_never_fire_raises_where_it_is_written() -> None:
    with pytest.raises(ValueError, match="max_predict_s"):
        TemporalConfig(max_predict_s=1.0, max_bridge_s=0.5)
    with pytest.raises(ValueError, match="template_min_px"):
        TemporalConfig(template_min_px=90, template_max_px=40)


# ---------------------------------------------------------------------------
# The dense-cadence families, as regressions
# ---------------------------------------------------------------------------
#
# Rendered stress, and CLAUDE.md rule 9 still applies: these assert *relative*
# behaviour on frames we generated, never an absolute claim about the game.
# What they protect is the property the real corpus structurally cannot see -
# it is sampled at about 5 fps and ``max_step_s`` makes the bridge inert there
# by construction - so without these the whole temporal half would be covered
# only by unit tests of its own internals.


@pytest.mark.parametrize("hz", [30.0, 60.0, 90.0])
@pytest.mark.parametrize("occlusion_ms", [50.0, 100.0, 250.0, 500.0, 1000.0, 2000.0])
def test_the_bridge_shortens_the_blind_run_at_every_cadence(
    hz: float, occlusion_ms: float
) -> None:
    """The number a steering controller actually spends: the longest blind run.

    That is what COAST covers and then SEARCH covers, and shortening it is the
    entire operational point of the bridge. Asserted as an improvement over
    this repository's own previous behaviour on identical frames, which is the
    only comparison rendered frames can honestly support.
    """
    from tests.tracking_families import continuity

    without = continuity(hz, occlusion_ms, bridge=False)
    with_bridge = continuity(hz, occlusion_ms, bridge=True)

    assert with_bridge.longest_blind_ms <= without.longest_blind_ms
    assert with_bridge.bridged_frames > 0, "the bridge never fired at all"
    # Identity is the thing a bridge must never cost: a shorter blind run
    # bought by switching to a different arrow is worse than the gap.
    assert with_bridge.identity_held


@pytest.mark.parametrize("hz", [30.0, 60.0, 90.0])
def test_a_bridged_occlusion_does_not_poison_the_heading_after_it(hz: float) -> None:
    """Rotation drift, bounded - and it was 71 degrees before it was.

    The pipeline seeds the direction estimator with the heading it was last
    handed, so a heading the bridge drifted during an occlusion does not merely
    produce one bad frame: it biases the first *globally observed* frame after
    the occlusion too. The rotation bank's minimum gain and the cumulative
    rotation bound are what hold this, and both were added because this
    assertion failed at 43 and 71 degrees.
    """
    from tests.tracking_families import continuity

    bridged = continuity(hz, 250.0, bridge=True)

    assert bridged.recovery_error_deg is not None
    assert bridged.recovery_error_deg < 10.0, (
        f"the carried heading drifted {bridged.recovery_error_deg:.1f} degrees"
    )


def test_the_bridge_is_inert_at_corpus_cadence() -> None:
    """The soundness bound, asserted rather than only argued in a comment.

    D-058 made this argument for ``resume_max_age_s`` and it applies here
    unchanged: two frames 200 ms apart say very little about whether a
    similar-looking patch is the same object. The corpus is sampled at about
    5 fps, so the bridge must not fire on it - and when an earlier version did,
    it bought 2.4 points of eval recall and cost a false lock on the
    same-coloured sand sequence.
    """
    bridge = TemporalBridge()
    centre = (600.0, 340.0)
    _anchor(bridge, centre, at_s=0.0)

    # 5 fps: consecutive frames 200 ms apart, arrow perfectly visible.
    carried = 0
    for index in range(1, 5):
        at_s = index * 0.2
        frame = _render([(centre, 34.0, 0.0)], sequence=index + 1, at_s=at_s)
        measurement = bridge.bridge(frame)
        if measurement is not None and measurement.measured:
            carried += 1

    assert carried == 0, "the bridge fired at a cadence it cannot support"
    assert bridge.stats.bridged == 0
