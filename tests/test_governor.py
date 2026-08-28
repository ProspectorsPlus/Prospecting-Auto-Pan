"""Cadence governor traces: the five behaviours the rebuild had to produce.

The bug being defended against is a pipeline that reports a cadence it is not
achieving. A tier is a claim about *processed* frames, so 120 delivered and 57
turned into decisions is a 60 Hz pipeline wearing a 120 Hz label, and the
governor has to say so.

Each test is one named trace from the mission's acceptance list.
"""

from __future__ import annotations

import pytest

from prospector_engine.capture import CadenceGovernor, CaptureConfig
from prospector_engine.contracts import GovernorState, PerformanceTier


def _governor(**overrides: object) -> CadenceGovernor:
    return CadenceGovernor(CaptureConfig(**overrides))  # type: ignore[arg-type]


def _run(
    governor: CadenceGovernor,
    *,
    polls: int,
    unique: float,
    processed: float | None = None,
    age_ms: float | None = 5.0,
    p95_ms: float | None = 8.0,
    loss: float = 0.0,
    start_s: float = 0.0,
    step_s: float = 0.5,
) -> PerformanceTier:
    now = start_s
    for _ in range(polls):
        governor.update(
            unique_fps=unique,
            processed_fps=processed,
            frame_age_ms=age_ms,
            p95_age_ms=p95_ms,
            observation_loss=loss,
            now_s=now,
        )
        now += step_s
    return governor.tier


# ---------------------------------------------------------------------------
# The five required traces
# ---------------------------------------------------------------------------


def test_120_captured_but_57_processed_converges_to_60() -> None:
    """The exact case the old governor got wrong: it stayed at 120."""
    governor = _governor(start_tier=PerformanceTier.MAXIMUM)

    tier = _run(governor, polls=20, unique=120.0, processed=57.0)

    assert tier is PerformanceTier.STANDARD
    assert governor.report().processed_ratio == pytest.approx(57 / 60, abs=0.02)


def test_a_genuine_120_hz_workload_may_stay_at_120() -> None:
    governor = _governor(start_tier=PerformanceTier.MAXIMUM)

    tier = _run(governor, polls=20, unique=120.0, processed=112.0, p95_ms=12.0)

    assert tier is PerformanceTier.MAXIMUM
    assert governor.state is GovernorState.STABLE


def test_a_60_capped_source_probes_once_returns_to_60_then_stops_oscillating() -> None:
    """A failed climb has to be *remembered*, or it repeats every few seconds."""
    governor = _governor(start_tier=PerformanceTier.STANDARD, upshift_after_s=2.0)
    now = 0.0
    tiers: list[int] = []
    for _ in range(60):
        # The source is hard-capped at 60: asking for 90 still yields 60.
        delivered = 60.0
        governor.update(
            unique_fps=delivered,
            processed_fps=delivered,
            frame_age_ms=5.0,
            p95_age_ms=8.0,
            now_s=now,
        )
        tiers.append(governor.tier.fps)
        now += 0.5

    assert governor.tier is PerformanceTier.STANDARD
    assert governor.probes == 1, f"probed {governor.probes} times: {tiers}"
    assert tiers.count(90) <= 2, "one brief probe, not an oscillation"


def test_a_250_ms_transient_does_not_cause_a_permanent_downshift() -> None:
    governor = _governor(start_tier=PerformanceTier.STANDARD)
    _run(governor, polls=6, unique=60.0, processed=59.0)

    # One bad poll: a garbage collection, a resize, a scheduling hiccup.
    governor.update(unique_fps=8.0, processed_fps=8.0, frame_age_ms=250.0, now_s=3.5)
    _run(governor, polls=6, unique=60.0, processed=59.0, start_s=4.0)

    assert governor.tier is PerformanceTier.STANDARD


def test_sustained_over_age_evidence_immediately_blocks_live() -> None:
    governor = _governor(start_tier=PerformanceTier.STANDARD)
    _run(governor, polls=8, unique=60.0, processed=59.0)
    assert governor.report().live_eligible

    governor.update(
        unique_fps=60.0, processed_fps=59.0, frame_age_ms=5.0, p95_age_ms=400.0, now_s=9.0
    )

    assert not governor.report().live_eligible


# ---------------------------------------------------------------------------
# What "stable" is allowed to mean
# ---------------------------------------------------------------------------


def test_a_stable_tier_requires_processed_loss_age_and_sample_count_together() -> None:
    governor = _governor(start_tier=PerformanceTier.STANDARD)
    _run(governor, polls=8, unique=60.0, processed=59.0, loss=0.0)
    report = governor.report()

    assert report.state is GovernorState.STABLE
    assert report.live_eligible
    assert report.processed_ratio >= 0.90
    assert report.observation_loss <= 0.02
    assert report.samples >= 8


def test_observation_loss_alone_can_disqualify_a_tier() -> None:
    governor = _governor(start_tier=PerformanceTier.STANDARD)
    _run(governor, polls=10, unique=60.0, processed=59.0, loss=0.10)

    assert not governor.report().live_eligible


def test_pool_exhaustion_downshifts_even_when_the_rate_looks_fine() -> None:
    governor = _governor(start_tier=PerformanceTier.MAXIMUM)
    for step in range(4):
        governor.update(
            unique_fps=120.0,
            processed_fps=118.0,
            frame_age_ms=5.0,
            pool_exhausted_recent=3,
            now_s=float(step),
        )

    assert governor.tier is not PerformanceTier.MAXIMUM


def test_a_degraded_tier_is_never_live_eligible() -> None:
    governor = _governor(start_tier=PerformanceTier.MINIMUM)
    _run(governor, polls=6, unique=5.0, processed=5.0)

    assert governor.tier is PerformanceTier.DEGRADED
    assert not governor.tier.acceptable
    assert not governor.report().live_eligible
    assert governor.degraded_reason


def test_a_healthy_lower_tier_eventually_retries_upward() -> None:
    """A cap is a measurement, not a life sentence."""
    governor = _governor(start_tier=PerformanceTier.STANDARD, upshift_after_s=1.0)
    now = 0.0
    # Fail one probe: the source is capped at 60 for a while.
    for _ in range(10):
        governor.update(unique_fps=60.0, processed_fps=60.0, frame_age_ms=5.0, now_s=now)
        now += 0.5
    assert governor.probes == 1
    assert governor.tier is PerformanceTier.STANDARD

    # Much later the machine is faster and can genuinely feed 90.
    now += 60.0
    for _ in range(20):
        governor.update(unique_fps=90.0, processed_fps=88.0, frame_age_ms=5.0, now_s=now)
        now += 0.5

    assert governor.tier is PerformanceTier.HIGH
    assert governor.probes >= 2


def test_an_epoch_reset_discards_measurements_rather_than_averaging_across_them() -> None:
    governor = _governor(start_tier=PerformanceTier.STANDARD)
    _run(governor, polls=8, unique=60.0, processed=59.0)
    assert governor.report().samples >= 8

    governor.reset_epoch("source replaced")

    assert governor.state is GovernorState.WARMUP
    assert governor.report().samples == 0
    assert not governor.report().live_eligible


def test_the_warmup_state_never_produces_a_verdict() -> None:
    governor = _governor(start_tier=PerformanceTier.STANDARD)
    for step in range(10):
        governor.update(unique_fps=0.0, frame_age_ms=None, now_s=float(step))

    assert governor.state is GovernorState.WARMUP
    assert governor.tier is PerformanceTier.STANDARD
    assert not governor.report().live_eligible
