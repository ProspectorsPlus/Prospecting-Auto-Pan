"""Perception: profiles, segmentation, tracking, direction cues, arrival.

Everything here runs on synthetic frames. That is deliberate and also the
limit of what these tests prove: they show the code abstains when it should
and computes what it claims, **not** that any detector works on real Roblox
frames. That is E-PROF / E-DIR-E2E / E-ARRIVE, and those are pending.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from prospector_engine.contracts import CapturedFrame, EvidenceStatus, freeze_array
from prospector_engine.geometry import ViewportState
from prospector_engine.vision import (
    DIRECTION_STRATEGIES,
    ArrivalConfig,
    ArrivalDetector,
    ArrowSegmenter,
    SegmenterConfig,
    WaterConfig,
    WaterGuard,
    angle_between_deg,
    heading_deg,
    load_profiles,
    wrap_deg,
)
from tests.fakes import make_geometry

PROFILES = load_profiles()
YELLOW = PROFILES.get("yellow_map_v0")
assert YELLOW is not None


def _frame_with_shapes(
    shapes: list[tuple[int, int, int, int, tuple[int, int, int]]],
    canonical_px: tuple[int, int] = (1280, 720),
    sequence: int = 1,
    geometry: object | None = None,
) -> CapturedFrame:
    """Paint solid RGB rectangles onto an otherwise dark client frame."""
    if geometry is None:
        geometry = make_geometry(
            size=(float(canonical_px[0]), float(canonical_px[1])), canonical_px=canonical_px
        )
    width, height = geometry.canonical_px  # type: ignore[attr-defined]
    bgr = np.zeros((height, width, 3), dtype=np.uint8)
    bgr[:, :] = (20, 20, 20)
    for x, y, width, height, (r, g, b) in shapes:
        bgr[y : y + height, x : x + width] = (b, g, r)
    return CapturedFrame(
        sequence=sequence,
        captured_at_s=0.0,
        completed_at_s=0.005,
        duration_ms=5.0,
        geometry=geometry,
        bgr=freeze_array(bgr),
    )


YELLOW_RGB = (230, 220, 40)


# ---------------------------------------------------------------------------
# Angles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("degrees", "expected"),
    [(0.0, 0.0), (190.0, -170.0), (-190.0, 170.0), (540.0, 180.0), (-540.0, 180.0)],
)
def test_angles_wrap_correctly_across_the_seam(degrees: float, expected: float) -> None:
    assert wrap_deg(degrees) == pytest.approx(expected)


def test_the_signed_turn_takes_the_short_way_round() -> None:
    assert angle_between_deg(170.0, -170.0) == pytest.approx(20.0)
    assert angle_between_deg(-170.0, 170.0) == pytest.approx(-20.0)


@pytest.mark.parametrize(
    ("vector", "expected"),
    [((0.0, -1.0), 0.0), ((1.0, 0.0), 90.0), ((0.0, 1.0), 180.0), ((-1.0, 0.0), -90.0)],
)
def test_heading_is_measured_from_screen_up_clockwise(
    vector: tuple[float, float], expected: float
) -> None:
    assert heading_deg(vector) == pytest.approx(expected)


def test_a_degenerate_vector_has_no_heading() -> None:
    assert heading_deg((0.0, 0.0)) is None


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


def test_every_bundled_profile_is_pending_and_none_is_auto_selectable() -> None:
    assert len(PROFILES) >= 2
    assert all(profile.status is EvidenceStatus.PENDING for profile in PROFILES.all())
    assert PROFILES.validated() == ()


def test_profiles_load_from_package_data_not_the_working_directory() -> None:
    """A packaged build must find these the same way a source run does."""
    assert {"green_arrow_v1", "yellow_map_v0", "yellow_map_v1", "generic_saturated_v0"} <= set(
        PROFILES.ids()
    )


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------


def test_a_single_plausible_blob_is_acquired() -> None:
    frame = _frame_with_shapes([(600, 300, 60, 90, YELLOW_RGB)])
    observation = ArrowSegmenter(YELLOW).observe(frame)

    assert observation.valid
    assert observation.centroid_px is not None
    assert observation.centroid_px[0] == pytest.approx(629.5, abs=2.0)
    assert observation.axis_unit_xy is not None
    assert 0.0 < observation.confidence <= 1.0


def test_two_similar_candidates_abstain_instead_of_guessing() -> None:
    frame = _frame_with_shapes([(300, 300, 60, 90, YELLOW_RGB), (800, 300, 60, 90, YELLOW_RGB)])
    observation = ArrowSegmenter(YELLOW).observe(frame)

    assert not observation.valid
    assert observation.abstain_reason == "ambiguous-candidates"


def test_a_clearly_dominant_candidate_is_still_accepted() -> None:
    frame = _frame_with_shapes(
        [(300, 300, 120, 180, YELLOW_RGB), (800, 300, 32, 32, YELLOW_RGB)]
    )
    observation = ArrowSegmenter(YELLOW).observe(frame)

    assert observation.valid


def test_no_candidate_abstains_rather_than_returning_a_default() -> None:
    frame = _frame_with_shapes([])
    observation = ArrowSegmenter(YELLOW).observe(frame)

    assert not observation.valid
    assert observation.abstain_reason == "no-candidate"
    assert observation.centroid_px is None


def test_a_clipped_candidate_abstains() -> None:
    frame = _frame_with_shapes([(0, 300, 80, 120, YELLOW_RGB)])
    observation = ArrowSegmenter(YELLOW).observe(frame)

    assert not observation.valid
    assert observation.abstain_reason == "candidate-clipped"


def test_an_unsupported_viewport_size_abstains() -> None:
    frame = _frame_with_shapes([(300, 200, 60, 90, YELLOW_RGB)], canonical_px=(1024, 640))
    observation = ArrowSegmenter(YELLOW).observe(frame)

    assert not observation.valid
    assert observation.abstain_reason == "unsupported-viewport-size"


def test_an_invalid_viewport_abstains() -> None:
    frame = _frame_with_shapes([(600, 300, 60, 90, YELLOW_RGB)])
    broken = CapturedFrame(
        sequence=frame.sequence,
        captured_at_s=frame.captured_at_s,
        completed_at_s=frame.completed_at_s,
        duration_ms=frame.duration_ms,
        geometry=make_geometry(state=ViewportState.INVALID),
        bgr=frame.bgr,
    )
    assert ArrowSegmenter(YELLOW).observe(broken).abstain_reason == "viewport-invalid"


def test_a_capture_error_abstains() -> None:
    frame = _frame_with_shapes([(600, 300, 60, 90, YELLOW_RGB)])
    broken = CapturedFrame(
        sequence=1,
        captured_at_s=0.0,
        completed_at_s=0.0,
        duration_ms=1.0,
        geometry=frame.geometry,
        bgr=frame.bgr,
        capture_error="backend died",
    )
    assert "capture-error" in (ArrowSegmenter(YELLOW).observe(broken).abstain_reason or "")


def test_exclusion_regions_remove_ui_candidates() -> None:
    frame = _frame_with_shapes([(600, 20, 60, 90, YELLOW_RGB)])
    config = SegmenterConfig(exclusion_regions_px=((560, 0, 200, 160),))
    observation = ArrowSegmenter(YELLOW, config).observe(frame)

    assert not observation.valid
    assert observation.abstain_reason == "no-candidate"


# ---------------------------------------------------------------------------
# Tracking
# ---------------------------------------------------------------------------
#
# The three ``ArrowTracker`` tests that lived here went with the class in
# D-094. What they asserted - a track survives small motion, a teleport starts
# a new one, a miss is never fabricated - is asserted against the mechanisms
# that actually run: identity in ``tests/test_arrow.py`` against
# ``ArrowDetector``, and continuity across missed frames in
# ``tests/test_temporal.py`` against ``TemporalBridge``.


# ---------------------------------------------------------------------------
# Direction cues
# ---------------------------------------------------------------------------


def test_every_cue_abstains_without_a_forward_reference() -> None:
    """E-ANCHOR and E-FORWARD are pending, so this is the production path."""
    frame = _frame_with_shapes([(600, 200, 60, 90, YELLOW_RGB)])
    arrow = ArrowSegmenter(YELLOW).observe(frame)
    for name, strategy in DIRECTION_STRATEGIES.items():
        observation = strategy(arrow, (640.0, 500.0), None)
        assert not observation.valid, name
        assert observation.error_deg is None


def test_the_centroid_cue_reports_the_signed_turn() -> None:
    frame = _frame_with_shapes([(900, 200, 60, 90, YELLOW_RGB)])
    arrow = ArrowSegmenter(YELLOW).observe(frame)
    observation = DIRECTION_STRATEGIES["centroid_ray"](arrow, (640.0, 500.0), 0.0)

    assert observation.valid
    assert observation.error_deg is not None
    assert observation.error_deg > 0  # the arrow is to the right of screen-up


def test_a_left_hand_arrow_produces_a_negative_turn() -> None:
    frame = _frame_with_shapes([(300, 200, 60, 90, YELLOW_RGB)])
    arrow = ArrowSegmenter(YELLOW).observe(frame)
    observation = DIRECTION_STRATEGIES["centroid_ray"](arrow, (640.0, 500.0), 0.0)

    assert observation.valid
    assert observation.error_deg is not None
    assert observation.error_deg < 0


def test_the_fusion_cue_abstains_when_position_and_pose_disagree() -> None:
    """Averaging a real disagreement would produce a confident wrong answer."""
    from prospector_engine.contracts import ArrowObservation

    arrow = ArrowObservation(
        profile_id="test",
        track_id=1,
        bbox_px=(600, 200, 60, 90),
        centroid_px=(900.0, 200.0),
        tip_px=(900.0, 200.0),
        axis_unit_xy=(-1.0, 0.0),  # pose says hard left, position says right
        confidence=0.9,
        valid=True,
    )
    observation = DIRECTION_STRATEGIES["fusion"](arrow, (640.0, 500.0), 0.0)

    assert not observation.valid
    assert observation.abstain_reason == "cues disagree"
    assert observation.cue_disagreement_deg is not None


def test_the_fusion_cue_combines_agreeing_components() -> None:
    from prospector_engine.contracts import ArrowObservation

    bearing = math.radians(30.0)
    arrow = ArrowObservation(
        profile_id="test",
        track_id=1,
        bbox_px=(0, 0, 10, 10),
        centroid_px=(640.0 + 200 * math.sin(bearing), 500.0 - 200 * math.cos(bearing)),
        tip_px=(640.0 + 200 * math.sin(bearing), 500.0 - 200 * math.cos(bearing)),
        axis_unit_xy=(math.sin(bearing), -math.cos(bearing)),
        confidence=0.8,
        valid=True,
    )
    observation = DIRECTION_STRATEGIES["fusion"](arrow, (640.0, 500.0), 0.0)

    assert observation.valid
    assert observation.error_deg == pytest.approx(30.0, abs=1.0)


# ---------------------------------------------------------------------------
# Arrival
# ---------------------------------------------------------------------------
#
# The signal is the game's own "X Marks the spot! Dig down!" banner, and the
# frames below reproduce the signature that was *measured* off the owner's real
# client on 2026-08-29: one line of bright glyphs with a dark outline, 205 px
# wide and 15 px tall in canonical pixels, horizontally centred to within a
# pixel, 314 glyph pixels. Seventeen real negative frames - five taken at the
# destination with the banner not showing, twelve captured live while
# navigating - scored exactly zero glyph pixels.
#
# The real frames are not committed: they are screenshots of the owner's own
# session and carry their username, level and balance. What is committed is the
# signature they produced.


def _banner_frame(
    *,
    width: int = 205,
    height: int = 15,
    centre_x: int = 640,
    top: int = 557,
    outlined: bool = True,
) -> CapturedFrame:
    """A canonical frame with a line of outlined text where the banner sits."""
    bgr = np.zeros((720, 1280, 3), dtype=np.uint8)
    bgr[:, :] = (60, 120, 60)  # grass, so the band is not trivially empty
    left = centre_x - width // 2
    if outlined:
        bgr[top - 3 : top + height + 3, left - 3 : left + width + 3] = (10, 10, 10)
    # Sparse strokes rather than a solid bar: the real text fills about a tenth
    # of its own bounding box.
    for column in range(left, left + width, 6):
        bgr[top : top + height, column : column + 2] = (250, 250, 250)
    return CapturedFrame(
        sequence=1,
        captured_at_s=0.0,
        completed_at_s=0.005,
        duration_ms=5.0,
        geometry=make_geometry(),
        bgr=freeze_array(bgr),
    )


def test_the_arrival_detector_is_not_enabled_for_production() -> None:
    """E-ARRIVE has not been run, and one positive frame cannot set a gate."""
    detector = ArrivalDetector()
    assert detector.status is EvidenceStatus.PENDING
    assert not detector.enabled_for_production()


def test_the_measured_banner_is_recognised() -> None:
    detector = ArrivalDetector()

    reading = detector.reading(_banner_frame())
    found, why = detector.matches(reading)

    assert found, why
    assert reading.width_px == pytest.approx(205, abs=8)
    assert reading.height_px == pytest.approx(15, abs=4)
    assert abs(reading.centre_offset_px) < 5


def test_open_terrain_is_not_a_banner() -> None:
    """Twelve live navigating frames scored zero glyph pixels; so does this."""
    bgr = np.zeros((720, 1280, 3), dtype=np.uint8)
    bgr[:, :] = (60, 120, 60)
    frame = CapturedFrame(
        sequence=1,
        captured_at_s=0.0,
        completed_at_s=0.005,
        duration_ms=5.0,
        geometry=make_geometry(),
        bgr=freeze_array(bgr),
    )
    detector = ArrivalDetector()

    reading = detector.reading(frame)

    assert not reading.found
    assert not detector.matches(reading)[0]


def test_bright_text_with_no_dark_outline_is_not_the_banner() -> None:
    """A glyph body alone is a bright patch. The outline is what makes it text."""
    detector = ArrivalDetector()

    reading = detector.reading(_banner_frame(outlined=False))

    assert not detector.matches(reading)[0]


def test_a_player_nametag_is_not_the_banner() -> None:
    """Nametags are also bright outlined text, and are narrow and off-centre -
    which is exactly why width and centring are checked and brightness is not."""
    detector = ArrivalDetector()

    narrow = detector.matches(detector.reading(_banner_frame(width=60)))
    offset = detector.matches(detector.reading(_banner_frame(centre_x=420)))

    assert not narrow[0] and "wide" in narrow[1]
    assert not offset[0] and "centre" in offset[1]


def test_a_tall_block_of_text_is_not_the_banner() -> None:
    """The chat log is many lines; the banner is one."""
    detector = ArrivalDetector()

    assert not detector.matches(detector.reading(_banner_frame(height=60)))[0]


def test_arrival_needs_a_valid_approach_context() -> None:
    detector = ArrivalDetector(ArrivalConfig(required_hits=1))

    observation = detector.observe(_banner_frame(), map_id="m1", approach_valid=False)

    assert not observation.valid
    assert "no-valid-approach" in observation.evidence


def test_arrival_requires_n_of_m_support() -> None:
    """One frame of a banner-shaped thing is not an arrival."""
    detector = ArrivalDetector(ArrivalConfig(support_window=4, required_hits=3))
    frame = _banner_frame()

    first = detector.observe(frame, map_id="m1", approach_valid=True)
    second = detector.observe(frame, map_id="m1", approach_valid=True)
    third = detector.observe(frame, map_id="m1", approach_valid=True)

    assert not first.valid and not second.valid
    assert third.valid
    assert third.latched_map_id == "m1"


def test_one_map_can_latch_arrival_only_once() -> None:
    detector = ArrivalDetector(ArrivalConfig(support_window=2, required_hits=1))
    frame = _banner_frame()

    latched = detector.observe(frame, map_id="m1", approach_valid=True)
    again = detector.observe(frame, map_id="m1", approach_valid=True)

    assert latched.valid
    assert not again.valid
    assert "already-latched" in again.evidence


def test_a_new_map_can_latch_after_a_reset() -> None:
    detector = ArrivalDetector(ArrivalConfig(support_window=2, required_hits=1))
    frame = _banner_frame()
    assert detector.observe(frame, map_id="m1", approach_valid=True).valid

    detector.reset_for_map("m2")
    assert detector.observe(frame, map_id="m2", approach_valid=True).valid


def test_the_band_is_where_the_game_actually_draws_the_banner() -> None:
    """It was the top centre - a patch of sky - and the measured text is at
    y 557..572. A detector looking in the wrong place can never fire."""
    x, y, width, height = ArrivalConfig().prompt_region_px

    assert y <= 557 and y + height >= 572, "the measured text is outside the band"
    assert x <= 537 and x + width >= 742


# ---------------------------------------------------------------------------
# Water
# ---------------------------------------------------------------------------
#
# In the Rotwood Swamp the water has alligators in it and standing in it for a
# second or two is fatal, so "we have arrived, stop here" has to be able to say
# *not here*. Water separates from terrain by hue: measured over ten real
# canonical frames on 2026-08-29, water reads hue 84-99 and grass and dirt read
# hue 15-44, with the configured band comfortably between them.


def _painted(*, water_box: tuple[int, int, int, int] | None = None) -> CapturedFrame:
    """Grass, optionally with a rectangle of swamp water painted into it."""
    bgr = np.zeros((720, 1280, 3), dtype=np.uint8)
    bgr[:, :] = (60, 150, 90)  # BGR grass: hue about 40
    if water_box is not None:
        x, y, width, height = water_box
        bgr[y : y + height, x : x + width] = (150, 120, 60)  # BGR water: hue about 100
    return CapturedFrame(
        sequence=1,
        captured_at_s=0.0,
        completed_at_s=0.005,
        duration_ms=5.0,
        geometry=make_geometry(),
        bgr=freeze_array(bgr),
    )


def test_grass_is_not_water() -> None:
    guard = WaterGuard()

    reading = guard.read(_painted(), anchor_px=(640.0, 430.0))

    assert reading is not None
    assert reading.fraction == 0.0
    assert not reading.unsafe(guard.config)


def test_standing_in_water_is_recognised() -> None:
    guard = WaterGuard()

    reading = guard.read(_painted(water_box=(500, 300, 300, 260)), anchor_px=(640.0, 430.0))

    assert reading is not None
    assert reading.fraction > 0.9
    assert reading.unsafe(guard.config)


def test_the_driest_way_out_points_away_from_the_water() -> None:
    """The whole left of the frame is water; the way out is to the right."""
    guard = WaterGuard()

    reading = guard.read(_painted(water_box=(0, 0, 640, 720)), anchor_px=(640.0, 430.0))

    assert reading is not None
    assert reading.driest_deg is not None
    assert reading.driest_deg > 0.0, f"it pointed into the water ({reading.driest_deg:+.0f})"


def test_a_reading_needs_an_anchor() -> None:
    """Every measurement here is relative to where the character is."""
    assert WaterGuard().read(_painted(), anchor_px=None) is None


def test_the_water_band_does_not_touch_the_measured_terrain() -> None:
    """Grass and dirt measured at hue 15-44; water at 84-99. A band that
    overlapped the terrain would make every route think it was drowning."""
    config = WaterConfig()

    assert config.hue_low > 44, "the band reaches down into terrain hue"
    assert config.hue_low <= 84 <= config.hue_high
    assert config.hue_low <= 99 <= config.hue_high
