"""Bounded tracing, and the governor's honesty about what it measures.

Every case here is a defect that was observed on the dashboard:

* processed FPS of zero fell back to the capture rate and a stalled worker
  read as healthy;
* a single 274 ms sample from a resize sat in a 240-sample ring and blocked
  Live for as long as the ring took to roll over;
* one transient cascaded Auto from 90 Hz to 15 Hz and, once DEGRADED, the
  governor never probed upward again;
* a new tier was judged on frames the old interval produced.
"""

from __future__ import annotations

import json
from pathlib import Path

from prospector_engine.capture import CadenceGovernor, CaptureConfig, LatencyTracker
from prospector_engine.contracts import GovernorState, PerformanceTier
from prospector_engine.trace import (
    FrameTrace,
    GovernorTransition,
    PerceptionTiming,
    PreviewTrace,
    TraceRing,
    load_jsonl,
)


def _timing(**overrides: object) -> PerceptionTiming:
    base: dict[str, object] = {
        "roi_used": True,
        "roi_proposal_ms": 0.1,
        "roi_detector_ms": 3.0,
        "full_detector_ms": 0.0,
        "fallback": False,
        "raw_components": 4,
        "components_evaluated": 2,
        "mask_pixels_allocated": 9000,
        "direction_ms": 0.4,
        "tracking_decision": "track",
        "selected_candidate_id": 7,
        "confidence": 0.9,
    }
    base.update(overrides)
    return PerceptionTiming(**base)  # type: ignore[arg-type]


def _frame(sequence: int, *, settling: bool = False, latency_ms: float = 12.0) -> FrameTrace:
    return FrameTrace(
        frame_sequence=sequence,
        captured_at_s=sequence * 0.016,
        completed_at_s=sequence * 0.016 + 0.003,
        source_epoch=1,
        cadence_hz=60,
        capture_ms=3.0,
        scheduling_delay_ms=0.5,
        perception=_timing(),
        decision_ms=0.1,
        capture_to_observation_ms=latency_ms,
        settling=settling,
    )


# ---------------------------------------------------------------------------
# The ring
# ---------------------------------------------------------------------------


def test_the_trace_ring_is_bounded_and_counts_what_it_evicted() -> None:
    ring = TraceRing(capacity=8)
    for sequence in range(1, 21):
        ring.record(_frame(sequence))
    assert len(ring.frames()) == 8
    assert ring.recorded == 20
    assert ring.frames()[0].frame_sequence == 13


def test_the_summary_excludes_settling_frames_by_default() -> None:
    ring = TraceRing()
    for sequence in range(1, 11):
        ring.record(_frame(sequence, latency_ms=10.0))
    ring.record(_frame(11, settling=True, latency_ms=300.0))
    summary = ring.summary()
    assert summary.frames == 10
    assert summary.fields["capture_to_observation_ms"][3] == 10.0
    everything = ring.summary(exclude_settling=False)
    assert everything.frames == 11
    assert everything.fields["capture_to_observation_ms"][3] == 300.0


def test_export_writes_every_ring_as_jsonl_and_reads_back(tmp_path: Path) -> None:
    ring = TraceRing()
    ring.record(_frame(1))
    ring.record_preview(PreviewTrace(1, 0.02, 2.5, 0.7, "minimal"))
    ring.record_transition(GovernorTransition(0.5, 60, 90, "probe", "probing the next tier up"))
    written = ring.export_jsonl(tmp_path / "trace.jsonl")
    rows = list(load_jsonl(written))
    kinds = [row["kind"] for row in rows]
    assert kinds == ["frame", "preview", "governor"]
    assert rows[0]["perception_tracking_decision"] == "track"
    assert rows[0]["perception_selected_candidate_id"] == 7
    assert json.loads(written.read_text().splitlines()[2])["to_hz"] == 90


# ---------------------------------------------------------------------------
# Latency: recent window versus history
# ---------------------------------------------------------------------------


def test_one_old_outlier_stays_in_history_and_leaves_the_recent_window() -> None:
    tracker = LatencyTracker("end-to-end", window=240)
    tracker.record_ms(274.0, now_s=0.0)
    for step in range(1, 120):
        tracker.record_ms(12.0, now_s=step * 0.1)
    assert tracker.summary().max_ms == 274.0, "history keeps the outlier for diagnostics"
    recent = tracker.recent(2.0, now_s=12.0)
    assert recent.samples > 0
    assert recent.max_ms == 12.0, "readiness is judged on the last two seconds"


def test_an_epoch_reset_hides_earlier_samples_from_the_recent_window() -> None:
    tracker = LatencyTracker("end-to-end")
    tracker.record_ms(250.0, now_s=10.0)
    tracker.start_epoch(10.5)
    tracker.record_ms(9.0, now_s=10.6)
    recent = tracker.recent(5.0, now_s=10.7)
    assert recent.samples == 1 and recent.max_ms == 9.0
    assert tracker.summary().samples == 2


# ---------------------------------------------------------------------------
# Governor honesty
# ---------------------------------------------------------------------------


def _governor(**overrides: object) -> CadenceGovernor:
    return CadenceGovernor(CaptureConfig(**overrides))  # type: ignore[arg-type]


def test_zero_processed_fps_with_a_consumer_is_a_real_zero() -> None:
    """A stalled worker must not read as a healthy 60 Hz pipeline."""
    governor = _governor(start_tier=PerformanceTier.STANDARD)
    for step in range(8):
        governor.update(unique_fps=60.0, frame_age_ms=5.0, now_s=float(step), processed_fps=0.0)
    assert governor.tier is PerformanceTier.MINIMUM or governor.tier is PerformanceTier.DEGRADED
    assert not governor.report().live_eligible


def test_without_a_consumer_capture_stands_in_for_processed() -> None:
    governor = _governor(start_tier=PerformanceTier.STANDARD)
    for step in range(4):
        governor.update(
            unique_fps=60.0, frame_age_ms=5.0, now_s=float(step), processed_fps=None
        )
    assert governor.tier is PerformanceTier.STANDARD


def test_settling_polls_are_skipped_not_judged() -> None:
    governor = _governor(start_tier=PerformanceTier.HIGH)
    for step in range(6):
        governor.update(
            unique_fps=10.0,
            frame_age_ms=400.0,
            now_s=float(step),
            processed_fps=8.0,
            settling=True,
        )
    assert governor.tier is PerformanceTier.HIGH, (
        "frames from the old interval do not downshift"
    )
    assert governor.settling_polls == 6
    assert governor.state is GovernorState.WARMUP


def test_degraded_recovers_upward_once_the_load_clears() -> None:
    """Predictably: after the probe cooldown plus the quiet period, not never.

    The previous governor stayed DEGRADED for the rest of the session because
    the DEGRADED state could not reach the probe at all.
    """
    governor = _governor(
        start_tier=PerformanceTier.MINIMUM, upshift_after_s=1.0, probe_cooldown_s=5.0
    )
    # Load: only 15 useful fps against the 30 Hz tier for a full second -> DEGRADED.
    for step in range(4):
        governor.update(
            unique_fps=15.0, frame_age_ms=5.0, now_s=step * 0.25, processed_fps=15.0
        )
    assert governor.tier is PerformanceTier.DEGRADED
    # The load clears: whatever tier is requested, the source now saturates it.
    probed_at = None
    for step in range(2, 60):
        rate = float(governor.tier.fps)
        governor.update(
            unique_fps=rate, frame_age_ms=5.0, now_s=1.0 + step * 0.5, processed_fps=rate
        )
        if governor.probes >= 1 and probed_at is None:
            probed_at = step * 0.5
    assert probed_at is not None, "DEGRADED never probed upward"
    assert probed_at <= 1.0 + 5.0 + 1.0 + 1.0, f"recovery took {probed_at} s"
    assert governor.tier.fps >= PerformanceTier.MINIMUM.fps
    assert governor.state is not GovernorState.DEGRADED


def test_transitions_are_recorded_with_their_reasons() -> None:
    governor = _governor(start_tier=PerformanceTier.MAXIMUM)
    seen: list[GovernorTransition] = []
    governor.set_transition_hook(seen.append)
    for step in range(4):
        governor.update(
            unique_fps=40.0, frame_age_ms=5.0, now_s=1.0 + step * 0.25, processed_fps=40.0
        )
    assert seen, "the downshift was announced"
    assert seen[-1].from_hz == 120 and seen[-1].to_hz == 90
    assert "downshift" in seen[-1].reason


def test_an_epoch_reset_clears_the_last_verdict() -> None:
    governor = _governor(start_tier=PerformanceTier.STANDARD)
    governor.update(
        unique_fps=60.0, frame_age_ms=5.0, now_s=1.0, processed_fps=60.0, p95_age_ms=264.0
    )
    governor.reset_epoch("geometry changed")
    report = governor.report()
    assert report.p95_age_ms is None
    assert report.samples == 0
    assert report.state is GovernorState.WARMUP
