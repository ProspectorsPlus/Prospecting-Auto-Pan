"""Cadence governor, metrics honesty, ROI tracking, and live profile switching.

The theme is that the numbers the governor and the UI act on must mean what
they say: a frame rate counts unique useful frames, a drop means a frame nobody
saw, and a tier is only held while it is genuinely being sustained.
"""

from __future__ import annotations

import numpy as np
import pytest

from prospector_engine.capture import CadenceGovernor, CaptureConfig, LatencyTracker
from prospector_engine.contracts import EvidenceStatus, PerformanceTier
from prospector_engine.navigation import PerceptionPipeline, ReferenceFrame
from prospector_engine.vision import ArrowSegmenter, load_profiles
from tests.fakes import make_geometry

PROFILES = load_profiles()
YELLOW = PROFILES.get("yellow_map_v0")
GENERIC = PROFILES.get("generic_saturated_v0")
assert YELLOW is not None and GENERIC is not None


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
    from prospector_engine.contracts import CapturedFrame, freeze_array

    geometry = make_geometry(size=(1280.0, 720.0))
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    image[:, :] = (20, 20, 20)
    x, y = centre
    image[y - 45 : y + 45, x - 30 : x + 30] = (40, 220, 230)  # BGR yellow
    return CapturedFrame(
        sequence=sequence,
        captured_at_s=float(sequence) * 0.01,
        completed_at_s=float(sequence) * 0.01 + 0.002,
        duration_ms=2.0,
        geometry=geometry,
        bgr=freeze_array(image),
    )


def _pipeline(**overrides: object) -> PerceptionPipeline:
    return PerceptionPipeline(segmenter=ArrowSegmenter(YELLOW), **overrides)  # type: ignore[arg-type]


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
    assert roi_result.inputs.arrow.bbox_px == full_result.inputs.arrow.bbox_px
    assert roi_result.inputs.arrow.centroid_px == pytest.approx(
        full_result.inputs.arrow.centroid_px
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


def test_an_roi_miss_falls_back_to_the_full_frame_immediately() -> None:
    """A tracker-induced miss must not be reported as an arrow loss."""
    pipeline = _pipeline(roi_padding_px=60)
    pipeline.analyze(_frame_with_blob(1, (200, 200)), map_id="t", approach_valid=False)  # type: ignore[arg-type]

    # The arrow jumps far outside any ROI around the old position.
    result = pipeline.analyze(
        _frame_with_blob(2, (1000, 500)),  # type: ignore[arg-type]
        map_id="t",
        approach_valid=False,
    )

    assert result.inputs.arrow.valid
    assert result.inputs.arrow.centroid_px is not None
    assert result.inputs.arrow.centroid_px[0] == pytest.approx(999.5, abs=3.0)


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


def test_every_direction_cue_is_evaluated_not_only_the_selected_one() -> None:
    """A fusion abstention is only useful next to the cues that disagreed."""
    pipeline = _pipeline()
    result = pipeline.analyze(
        _frame_with_blob(1, (900, 250)),  # type: ignore[arg-type]
        map_id="t",
        approach_valid=False,
    )

    names = {name for name, _cue in result.cues}
    assert names == {"centroid_ray", "tip_ray", "pca_axis", "fusion"}
    assert dict(result.cues)["fusion"] is result.inputs.direction


def test_the_reference_frame_is_configuration_and_says_so() -> None:
    reference = ReferenceFrame()
    assert not reference.validated
    assert reference.provenance.status is EvidenceStatus.PENDING
    assert "PENDING" in reference.source


def test_the_candidate_record_keeps_rejections_with_reasons() -> None:
    pipeline = _pipeline()
    result = pipeline.analyze(
        _frame_with_blob(1, (640, 360)),  # type: ignore[arg-type]
        map_id="t",
        approach_valid=False,
    )
    assert result.candidates
    accepted = [c for c in result.candidates if c.accepted]
    assert len(accepted) <= 1
    for candidate in result.candidates:
        if not candidate.accepted:
            assert candidate.rejected_reason


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
        assert service.metrics().dropped_frames == 0

        # Once a consumer exists, real drops are counted again.
        first = service.wait_for_new(0, 1.0)
        assert first is not None
        time.sleep(0.3)
        assert service.metrics().dropped_frames >= 1
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
