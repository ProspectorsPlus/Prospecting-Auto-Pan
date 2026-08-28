"""Cadence governor, metrics honesty, ROI tracking, and live profile switching.

The theme is that the numbers the governor and the UI act on must mean what
they say: a frame rate counts unique useful frames, a drop means a frame nobody
saw, and a tier is only held while it is genuinely being sustained.
"""

from __future__ import annotations

import pytest

from prospector_engine.capture import CadenceGovernor, CaptureConfig, LatencyTracker
from prospector_engine.contracts import EvidenceStatus, PerformanceTier
from prospector_engine.navigation import PerceptionPipeline, ReferenceFrame
from prospector_engine.vision import ArrowSegmenter, load_profiles
from tests.fakes import make_geometry

PROFILES = load_profiles()
YELLOW = PROFILES.get("yellow_map_v0")
GENERIC = PROFILES.get("generic_saturated_v0")
GREEN = PROFILES.get("green_arrow_v1")
assert YELLOW is not None and GENERIC is not None and GREEN is not None


# ---------------------------------------------------------------------------
# Cadence governor
# ---------------------------------------------------------------------------


def _governor(**overrides: object) -> CadenceGovernor:
    config = CaptureConfig(**overrides)  # type: ignore[arg-type]
    return CadenceGovernor(config)


def test_a_sustained_shortfall_downshifts_one_tier() -> None:
    governor = _governor(start_tier=PerformanceTier.MAXIMUM)
    assert governor.tier is PerformanceTier.MAXIMUM

    governor.update(unique_fps=40.0, frame_age_ms=5.0, now_s=1.0)
    assert governor.tier is PerformanceTier.MAXIMUM, "one bad poll must not downshift"
    governor.update(unique_fps=40.0, frame_age_ms=5.0, now_s=1.5)

    assert governor.tier is PerformanceTier.HIGH


def test_a_single_transient_does_not_knock_a_healthy_pipeline_down() -> None:
    """A window resize or a collection pause must not cost a tier."""
    governor = _governor(start_tier=PerformanceTier.STANDARD)
    governor.update(unique_fps=58.0, frame_age_ms=8.0, now_s=1.0)
    governor.update(unique_fps=10.0, frame_age_ms=8.0, now_s=1.5)  # transient
    governor.update(unique_fps=58.0, frame_age_ms=8.0, now_s=2.0)

    assert governor.tier is PerformanceTier.STANDARD


def test_an_empty_measurement_window_is_not_a_verdict() -> None:
    """Before the first frame there is nothing to judge a tier on."""
    governor = _governor(start_tier=PerformanceTier.STANDARD)
    for step in range(6):
        governor.update(unique_fps=0.0, frame_age_ms=None, now_s=float(step))
    assert governor.tier is PerformanceTier.STANDARD


def test_stale_frames_downshift_even_when_throughput_looks_fine() -> None:
    """A high rate of old frames is worse than a lower rate of fresh ones."""
    governor = _governor(start_tier=PerformanceTier.MAXIMUM, max_frame_age_ms=100)
    governor.update(unique_fps=120.0, frame_age_ms=500.0, now_s=1.0)
    governor.update(unique_fps=120.0, frame_age_ms=500.0, now_s=1.5)

    assert governor.tier is PerformanceTier.HIGH


def test_upshift_needs_a_quiet_period_and_a_saturated_tier() -> None:
    governor = _governor(start_tier=PerformanceTier.STANDARD, upshift_after_s=2.0)

    governor.update(unique_fps=59.0, frame_age_ms=5.0, now_s=0.0)
    governor.update(unique_fps=59.0, frame_age_ms=5.0, now_s=1.0)
    assert governor.tier is PerformanceTier.STANDARD, "too soon"

    governor.update(unique_fps=59.0, frame_age_ms=5.0, now_s=3.0)
    assert governor.tier is PerformanceTier.HIGH


def test_the_governor_never_chases_a_rate_the_source_cannot_produce() -> None:
    """Comfortably inside a tier is not the same as saturating it."""
    governor = _governor(start_tier=PerformanceTier.STANDARD, upshift_after_s=1.0)
    for step in range(10):
        # 45 fps clears the 0.7 downshift ratio but not the 0.95 upshift bar.
        governor.update(unique_fps=45.0, frame_age_ms=5.0, now_s=float(step))
    assert governor.tier is PerformanceTier.STANDARD


def test_the_governor_respects_its_ceiling() -> None:
    governor = _governor(start_tier=PerformanceTier.STANDARD, max_tier=PerformanceTier.STANDARD)
    for step in range(20):
        governor.update(unique_fps=60.0, frame_age_ms=4.0, now_s=float(step))
    assert governor.tier is PerformanceTier.STANDARD


def test_falling_below_the_minimum_is_reported_as_degraded() -> None:
    """Below 30 unique fps the application says so instead of looking healthy."""
    governor = _governor(start_tier=PerformanceTier.MINIMUM)
    governor.update(unique_fps=5.0, frame_age_ms=5.0, now_s=1.0)
    governor.update(unique_fps=5.0, frame_age_ms=5.0, now_s=1.5)

    assert governor.tier is PerformanceTier.DEGRADED
    assert not governor.tier.acceptable
    assert governor.degraded_reason is not None


def test_the_tier_ladder_is_the_declared_one() -> None:
    assert [tier.fps for tier in CadenceGovernor.LADDER] == [15, 30, 60, 90, 120]
    assert PerformanceTier.MINIMUM.acceptable
    assert not PerformanceTier.DEGRADED.acceptable
    assert PerformanceTier.STANDARD.interval_s == pytest.approx(1 / 60)


# ---------------------------------------------------------------------------
# Latency accounting
# ---------------------------------------------------------------------------


def test_latency_percentiles_are_bounded_and_ordered() -> None:
    tracker = LatencyTracker("test", window=100)
    for value in range(1, 101):
        tracker.record_ms(float(value))
    summary = tracker.summary()

    assert summary.samples == 100
    assert summary.p50_ms <= summary.p95_ms <= summary.p99_ms <= summary.max_ms
    assert summary.max_ms == 100.0


def test_the_latency_window_is_bounded() -> None:
    """History must not grow with runtime."""
    tracker = LatencyTracker("test", window=8)
    for value in range(1000):
        tracker.record_ms(float(value))
    assert tracker.summary().samples == 8


def test_an_empty_tracker_reports_zeroes_rather_than_failing() -> None:
    summary = LatencyTracker("empty").summary()
    assert summary.samples == 0
    assert summary.p95_ms == 0.0


# ---------------------------------------------------------------------------
# ROI tracking
# ---------------------------------------------------------------------------


def _frame_with_blob(sequence: int, centre: tuple[int, int]) -> object:
    """A real arrow, rendered where the test wants it.

    A painted rectangle used to be enough, because the detector ranked
    candidates by area. It is not enough now, and that is the point: the
    production detector requires the arrowhead topology it was built to find,
    so a fixture without it would be testing nothing.
    """
    from prospector_engine.contracts import CapturedFrame, freeze_array
    from tests.arrow_fixtures import render_scene

    scene = render_scene(
        heading_deg=35.0,
        centre_px=(float(centre[0]), float(centre[1])),
        scale_px=70.0,
        terrain="dirt",
        seed=sequence,
    )
    # Fifty milliseconds apart: the identity is earned on the second frame.
    return CapturedFrame(
        sequence=sequence,
        captured_at_s=float(sequence) * 0.05,
        completed_at_s=float(sequence) * 0.05 + 0.002,
        duration_ms=2.0,
        geometry=make_geometry(size=(1280.0, 720.0)),
        bgr=freeze_array(scene.bgr),
    )


def _pipeline(**overrides: object) -> PerceptionPipeline:
    return PerceptionPipeline(segmenter=ArrowSegmenter(GREEN), **overrides)  # type: ignore[arg-type]


def test_the_first_frame_always_uses_a_full_pass() -> None:
    pipeline = _pipeline()
    pipeline.analyze(_frame_with_blob(1, (640, 360)), map_id="t", approach_valid=False)  # type: ignore[arg-type]
    assert pipeline.full_passes == 1
    assert pipeline.roi_hits == 0


def test_a_held_track_lets_later_frames_search_only_a_region() -> None:
    pipeline = _pipeline()
    for sequence in range(1, 6):
        pipeline.analyze(
            _frame_with_blob(sequence, (640 + sequence * 3, 360)),  # type: ignore[arg-type]
            map_id="t",
            approach_valid=False,
        )
    assert pipeline.roi_hits >= 3
    assert pipeline.full_passes >= 1


def test_the_roi_result_matches_the_full_frame_result() -> None:
    """An ROI is an optimization; it must not change what is detected."""
    roi_pipeline = _pipeline()
    full_pipeline = _pipeline(full_frame_every=1)
    centre = (700, 380)
    for sequence in range(1, 5):
        roi_result = roi_pipeline.analyze(
            _frame_with_blob(sequence, centre),  # type: ignore[arg-type]
            map_id="t",
            approach_valid=False,
        )
        full_result = full_pipeline.analyze(
            _frame_with_blob(sequence, centre),  # type: ignore[arg-type]
            map_id="t",
            approach_valid=False,
        )
    assert roi_result.inputs.arrow.valid == full_result.inputs.arrow.valid
    # The local-background estimate is computed over the pass's own pixels,
    # so a region pass may differ from the full pass by a pixel at the mask
    # edge. What must not differ is which object was found and where.
    assert (
        roi_result.inputs.arrow.bbox_px is not None
        and full_result.inputs.arrow.bbox_px is not None
    )
    for mine, theirs in zip(
        roi_result.inputs.arrow.bbox_px, full_result.inputs.arrow.bbox_px, strict=True
    ):
        assert abs(mine - theirs) <= 2
    assert roi_result.inputs.arrow.centroid_px == pytest.approx(
        full_result.inputs.arrow.centroid_px, abs=2.0
    )


def test_a_full_frame_pass_happens_periodically_even_while_tracking() -> None:
    """A track must not be able to follow the wrong thing forever.

    ``full_frame_every=N`` bounds the run of consecutive ROI frames at N, so
    twelve frames give at least ``12 // (N + 1)`` full passes.
    """
    pipeline = _pipeline(full_frame_every=3)
    for sequence in range(1, 13):
        pipeline.analyze(
            _frame_with_blob(sequence, (640, 360)),  # type: ignore[arg-type]
            map_id="t",
            approach_valid=False,
        )
    assert pipeline.full_passes >= 12 // 4
    assert pipeline.roi_hits + pipeline.full_passes == 12


def test_an_roi_miss_schedules_a_global_search_on_the_next_frame() -> None:
    """A region miss is one transaction; the full frame runs on the next one.

    The previous pipeline ran the full pass synchronously on the same
    screenshot, aging the track and the global-scan cadence twice per frame.
    """
    pipeline = _pipeline(roi_padding_px=60)
    for sequence in (1, 2, 3):
        pipeline.analyze(
            _frame_with_blob(sequence, (200, 200)), map_id="t", approach_valid=False
        )  # type: ignore[arg-type]
    assert pipeline.roi_hits >= 1

    # The arrow jumps far outside any region around the old position.
    jumped = pipeline.analyze(
        _frame_with_blob(4, (1000, 500)), map_id="t", approach_valid=False
    )  # type: ignore[arg-type]
    assert jumped.timing is not None
    assert jumped.timing.roi_used and jumped.timing.full_detector_ms == 0.0, (
        "no second pass on this frame"
    )
    assert not jumped.inputs.arrow.valid, "a region miss is reported as a hold, not as a lock"

    # The held identity is protected for ``max_track_age_s``; only then does
    # the arrow at its new place earn a new identity, over consistent frames.
    reported = None
    for sequence in range(5, 22):
        result = pipeline.analyze(
            _frame_with_blob(sequence, (1000, 500)), map_id="t", approach_valid=False
        )  # type: ignore[arg-type]
        assert result.timing is not None
        assert not (result.timing.roi_used and result.timing.full_detector_ms > 0.0)
        if result.inputs.arrow.valid:
            reported = result
            break
    assert reported is not None, "the arrow at its new place was never reported"
    assert (
        reported.inputs.arrow.track_id != jumped.inputs.arrow.track_id
        or jumped.inputs.arrow.track_id is None
    )
    assert reported.inputs.arrow.centroid_px is not None
    assert reported.inputs.arrow.centroid_px[0] == pytest.approx(999.5, abs=3.0)
    assert pipeline.fallbacks >= 1


def test_changing_the_profile_forces_a_full_pass_and_drops_the_track() -> None:
    pipeline = _pipeline()
    for sequence in range(1, 5):
        pipeline.analyze(
            _frame_with_blob(sequence, (640, 360)),  # type: ignore[arg-type]
            map_id="t",
            approach_valid=False,
        )
    before = pipeline.full_passes

    pipeline.set_profile(GENERIC)
    pipeline.analyze(_frame_with_blob(9, (640, 360)), map_id="t", approach_valid=False)  # type: ignore[arg-type]

    assert pipeline.profile.profile_id == GENERIC.profile_id
    assert pipeline.full_passes == before + 1


# ---------------------------------------------------------------------------
# The diagnostic observation
# ---------------------------------------------------------------------------


def test_every_direction_cue_is_reported_with_the_weight_consensus_gave_it() -> None:
    """A consensus abstention is only useful next to the cues that disagreed."""
    pipeline = _pipeline()
    for sequence in (1, 2):
        result = pipeline.analyze(
            _frame_with_blob(sequence, (900, 250)),  # type: ignore[arg-type]
            map_id="t",
            approach_valid=False,
        )

    names = {reading.cue_id for reading in result.cues}
    assert "notch_axis" in names or "pca_axis" in names
    assert any(name.startswith("sign:") for name in names), "the polarity votes are reported"
    assert "player_to_arrow" in names, "position is reported, and kept distinct from pose"
    # Every cue carries the weight consensus gave it, so a rejected outlier is
    # visible at weight zero rather than silently missing.
    assert all(reading.weight >= 0.0 for reading in result.cues)
    assert result.inputs.direction.cues == result.cues


def test_the_reference_frame_is_configuration_and_says_so() -> None:
    reference = ReferenceFrame()
    assert not reference.validated
    assert reference.provenance.status is EvidenceStatus.PENDING
    assert "PENDING" in reference.source


def test_the_candidate_record_keeps_rejections_with_reasons() -> None:
    pipeline = _pipeline()
    for sequence in (1, 2):
        result = pipeline.analyze(
            _frame_with_blob(sequence, (640, 360)),  # type: ignore[arg-type]
            map_id="t",
            approach_valid=False,
        )
    assert result.candidates
    selected = [c for c in result.candidates if c.state == "selected"]
    assert len(selected) == 1, "exactly one candidate is selected per observation"
    assert [c for c in result.candidates if c.accepted] == selected
    for candidate in result.candidates:
        if not candidate.accepted:
            assert candidate.rejected_reason
            assert candidate.state in ("proposed", "viable", "challenger", "rejected")


# ---------------------------------------------------------------------------
# Bounded reacquisition
# ---------------------------------------------------------------------------


def _service(source: object, **config: object) -> object:
    from prospector_engine.capture import CaptureService, EvidenceRegistry, ViewportGuard
    from tests.fakes import FakePlatformPort, VirtualClock

    clock = VirtualClock()
    port = FakePlatformPort(
        clock, geometry=make_geometry(size=(64.0, 48.0), canonical_px=(64, 48))
    )
    guard = ViewportGuard(port, requested_client_logical=(64.0, 48.0))
    guard.adopt_current()
    service = CaptureService(
        guard,
        EvidenceRegistry("reacquire-test"),
        config=CaptureConfig(
            start_tier=PerformanceTier.MINIMUM,
            max_frame_age_ms=100000,
            **config,  # type: ignore[arg-type]
        ),
        source_factory=lambda: source,  # type: ignore[arg-type,return-value]
    )
    return (service, port, guard)


def test_a_lost_window_triggers_a_bounded_reacquisition() -> None:
    import time

    from prospector_engine.geometry import ViewportGeometry
    from tests.fakes import FakeCaptureSource

    source = FakeCaptureSource()
    service, port, _guard = _service(  # type: ignore[misc]
        source, supervisor_interval_s=0.02, reacquire_initial_delay_s=0.01
    )
    assert service.start()
    try:
        deadline = time.monotonic() + 2.0
        while service.latest() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert service.latest() is not None

        port.set_geometry(ViewportGeometry.invalid("window closed"))
        time.sleep(0.4)

        assert service.metrics().reacquisitions >= 1
    finally:
        service.stop()


def test_reacquisition_backs_off_instead_of_busy_looping() -> None:
    """A window that is gone for good must not cost a retry per poll."""
    import time

    from prospector_engine.geometry import ViewportGeometry
    from tests.fakes import FakeCaptureSource

    source = FakeCaptureSource()
    service, port, _guard = _service(  # type: ignore[misc]
        source,
        supervisor_interval_s=0.01,
        reacquire_initial_delay_s=0.02,
        reacquire_max_delay_s=0.2,
    )
    assert service.start()
    try:
        port.set_geometry(ViewportGeometry.invalid("window closed"))
        time.sleep(0.8)
        attempts = service.metrics().reacquisitions
        # 80 supervisor polls in that window; exponential backoff to a 0.2 s cap
        # allows roughly a handful of attempts, never one per poll.
        assert 1 <= attempts <= 12, attempts
    finally:
        service.stop()


def test_a_healthy_source_is_never_reacquired() -> None:
    import time

    from tests.fakes import FakeCaptureSource

    source = FakeCaptureSource()
    service, _port, _guard = _service(source, supervisor_interval_s=0.02)  # type: ignore[misc]
    assert service.start()
    try:
        time.sleep(0.4)
        assert service.metrics().reacquisitions == 0
    finally:
        service.stop()


def test_frames_are_not_counted_as_dropped_before_anyone_consumes() -> None:
    """With Shadow not started, "dropped" would otherwise read as a disaster."""
    import time

    from tests.fakes import FakeCaptureSource

    source = FakeCaptureSource()
    service, _port, _guard = _service(source, supervisor_interval_s=0.05)  # type: ignore[misc]
    assert service.start()
    try:
        time.sleep(0.4)  # frames flowing, nobody consuming
        assert service.latest() is not None
        assert service.metrics().superseded_frames.session_total == 0

        # Once a consumer exists, real supersedes are counted again.
        first = service.wait_for_new(0, 1.0)
        assert first is not None
        time.sleep(0.3)
        assert service.metrics().superseded_frames.session_total >= 1
    finally:
        service.stop()


def test_consumer_visible_gaps_match_the_slot_drop_count() -> None:
    """The two counters describe the same events from opposite ends."""
    from prospector_engine.capture import LatestFrameSlot

    slot = LatestFrameSlot()

    class _Frame:
        def __init__(self, sequence: int) -> None:
            self.sequence = sequence

    class _Envelope:
        def __init__(self, sequence: int) -> None:
            self.frame = _Frame(sequence)

    slot.wait_for_new(0, 0.0)  # register a consumer
    slot.publish(_Envelope(1))  # type: ignore[arg-type]
    taken = slot.wait_for_new(0, 0.0)
    assert taken is not None and taken.frame.sequence == 1

    for sequence in range(2, 6):
        slot.publish(_Envelope(sequence))  # type: ignore[arg-type]
    latest = slot.wait_for_new(1, 0.0)

    assert latest is not None and latest.frame.sequence == 5
    assert slot.dropped == 3  # frames 2, 3 and 4 were never seen
