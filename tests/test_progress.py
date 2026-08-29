"""The conservative progress guard.

What it is allowed to conclude is deliberately narrow. It may say "forward is
being held and nothing is moving, release it"; it may never say "there is a
wall to the left, go right". Obstacle mapping is a later phase and this pass
builds only the input side of that boundary.

Four properties are asserted throughout:

* elapsed time alone never declares anything;
* ambiguous motion abstains rather than guessing;
* confirmation happens **in motion**, with forward still held, because that is
  the only state in which "can this character move" is an answerable question.
  The version this replaces released ``W`` on the suspicion and then confirmed
  from four stationary frames, which cost a stop on every ambiguous stretch of
  ground and measured the wrong thing when it got there;
* an unproven baseline is not the same as no evidence - the relative fallback
  keeps recovery available on the first bush of a run rather than switching it
  off until twelve clean samples have arrived.
"""

from __future__ import annotations

import pytest

from prospector_engine.contracts import EvidenceStatus, MotionObservation, Provenance
from prospector_engine.motion import (
    UNCALIBRATED_BASELINE,
    LocomotionBaseline,
    ProgressConfig,
    ProgressGuard,
    ProgressState,
)

CALIBRATED = LocomotionBaseline(
    condition_id="test-open-ground",
    min_forward_speed_norm=0.20,
    reference_speed_norm=0.57,
    status=EvidenceStatus.VALIDATED,
    provenance=Provenance(
        status=EvidenceStatus.VALIDATED,
        source="test fixture standing in for E-MOTION open-ground trials",
        note="only a test may assert this; production needs armed trials",
    ),
)


def _motion(
    forward: float, *, confidence: float = 0.9, coverage: float = 0.8, yaw: float = 0.0
) -> MotionObservation:
    return MotionObservation(
        forward_speed_norm=forward,
        lateral_speed_norm=0.0,
        confidence=confidence,
        inlier_count=80,
        inlier_ratio=0.9,
        spatial_coverage=coverage,
        residual=0.4,
        yaw_contamination=yaw,
        valid=True,
    )


def _guard(**config: object) -> ProgressGuard:
    return ProgressGuard(CALIBRATED, ProgressConfig(**config))  # type: ignore[arg-type]


def _hold(guard: ProgressGuard, *, since_s: float = 0.0) -> None:
    guard.note_applied(since_s, forward=True)


def _walk(
    guard: ProgressGuard,
    speed: float,
    *,
    frames: int,
    start_s: float,
    fps: float = 60.0,
    **motion: object,
) -> list[object]:
    """Feed ``frames`` trustworthy samples at one speed and return the verdicts."""
    return [
        guard.update(_motion(speed, **motion), now_s=start_s + index / fps)  # type: ignore[arg-type]
        for index in range(frames)
    ]


# ---------------------------------------------------------------------------
# What the guard may conclude with nothing measured
# ---------------------------------------------------------------------------


def test_without_any_reference_the_guard_can_conclude_nothing() -> None:
    guard = ProgressGuard()
    _hold(guard)

    verdict = guard.update(_motion(0.0), now_s=1.0)

    assert verdict.state is ProgressState.UNKNOWN
    assert not verdict.recover
    assert "reference" in verdict.reason


def test_elapsed_time_alone_never_declares_an_obstacle() -> None:
    guard = _guard()
    _hold(guard)

    verdict = guard.update(None, now_s=30.0)

    assert verdict.state is ProgressState.UNKNOWN
    assert not verdict.recover


def test_forward_that_was_never_applied_cannot_look_like_a_wall() -> None:
    guard = _guard()

    verdict = guard.update(_motion(0.0), now_s=5.0)

    assert verdict.state is ProgressState.UNKNOWN
    assert not verdict.recover
    assert "not being held" in verdict.reason


def test_forward_held_only_briefly_is_not_yet_evidence() -> None:
    guard = _guard(min_applied_forward_ms=250)
    _hold(guard)

    verdict = guard.update(_motion(0.0), now_s=0.1)

    assert verdict.state is ProgressState.UNKNOWN
    assert not verdict.recover


@pytest.mark.parametrize(
    ("label", "motion"),
    [
        ("low confidence", {"confidence": 0.2}),
        ("poor coverage", {"coverage": 0.1}),
        ("yaw contaminated", {"yaw": 0.9}),
    ],
)
def test_ambiguous_motion_abstains_rather_than_declaring_no_progress(
    label: str, motion: dict[str, float]
) -> None:
    guard = _guard(suspect_after_ms=1)
    _hold(guard)

    verdict = guard.update(_motion(0.0, **motion), now_s=5.0)

    assert verdict.state is ProgressState.UNKNOWN, label
    assert not verdict.recover, label


def test_a_recent_yaw_suppresses_the_whole_judgement() -> None:
    guard = _guard(suspect_after_ms=1, post_yaw_holdoff_ms=250)
    _hold(guard)
    guard.note_yaw(4.9)

    verdict = guard.update(_motion(0.0), now_s=5.0)

    assert verdict.state is ProgressState.UNKNOWN
    assert "post-yaw-holdoff" in verdict.reason


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------


def test_progress_is_reported_when_the_character_is_moving() -> None:
    guard = _guard()
    _hold(guard)

    verdict = guard.update(_motion(0.5), now_s=1.0)

    assert verdict.state is ProgressState.PROGRESSING
    assert not verdict.recover
    assert verdict.ratio == pytest.approx(0.5 / 0.57, abs=0.01)


def test_the_ratio_is_reported_against_the_reference_the_threshold_came_from() -> None:
    guard = _guard()
    _hold(guard)

    verdict = guard.update(_motion(0.57), now_s=1.0)

    assert verdict.ratio == pytest.approx(1.0, abs=0.01)
    assert not verdict.provisional


# ---------------------------------------------------------------------------
# Contact, confirmed in motion
# ---------------------------------------------------------------------------


def test_a_suspicion_does_not_stop_the_character() -> None:
    """The change this rename records. A suspicion used to drop W to go and
    look; now it keeps walking and gathers the evidence that would confirm it."""
    guard = _guard(suspect_after_ms=300, confirm_frames=3)
    _hold(guard)

    verdicts = _walk(guard, 0.0, frames=25, start_s=1.0)

    suspected = [v for v in verdicts if v.state is ProgressState.NO_PROGRESS_SUSPECTED]
    assert suspected, "low progress never became a suspicion"
    assert not any(v.recover for v in suspected), "a suspicion recommended recovery"


def test_confirmation_needs_several_fresh_frames_and_then_recommends_recovery() -> None:
    guard = _guard(suspect_after_ms=300, confirm_frames=3)
    _hold(guard)

    verdicts = _walk(guard, 0.0, frames=30, start_s=1.0)
    states = [v.state for v in verdicts]

    assert ProgressState.NO_PROGRESS_SUSPECTED in states
    assert states[-1] is ProgressState.NO_PROGRESS_CONFIRMED
    assert verdicts[-1].recover
    assert states.index(ProgressState.NO_PROGRESS_CONFIRMED) > states.index(
        ProgressState.NO_PROGRESS_SUSPECTED
    )


def test_the_confirming_frames_are_collected_while_forward_is_still_held() -> None:
    """Otherwise the confirmation is measuring a character that has already
    been told to stand still, which answers a different question."""
    guard = _guard(suspect_after_ms=300, confirm_frames=3)
    _hold(guard)
    _walk(guard, 0.0, frames=30, start_s=1.0)

    assert guard.ledger.holding(), "the guard let go of forward to confirm"
    assert guard.state is ProgressState.NO_PROGRESS_CONFIRMED


def test_ambiguous_evidence_cannot_advance_a_confirmation() -> None:
    guard = _guard(suspect_after_ms=300, confirm_frames=3)
    _hold(guard)
    # Twenty frames at 60 Hz is 333 ms: past the suspicion, two of the three
    # confirming frames in.
    _walk(guard, 0.0, frames=20, start_s=1.0)
    assert guard.state is ProgressState.NO_PROGRESS_SUSPECTED

    verdicts = _walk(guard, 0.0, frames=20, start_s=2.0, confidence=0.1)

    assert all(v.state is ProgressState.UNKNOWN for v in verdicts)
    assert not any(v.recover for v in verdicts)


def test_movement_returning_clears_a_standing_suspicion() -> None:
    guard = _guard(suspect_after_ms=300, confirm_frames=4, clear_frames=2)
    _hold(guard)
    _walk(guard, 0.0, frames=20, start_s=1.0)
    assert guard.state is ProgressState.NO_PROGRESS_SUSPECTED

    verdicts = _walk(guard, 0.5, frames=5, start_s=2.0)

    assert verdicts[-1].state is ProgressState.PROGRESSING
    assert not any(v.recover for v in verdicts)


def test_letting_go_of_forward_drops_the_suspicion_entirely() -> None:
    guard = _guard(suspect_after_ms=300, confirm_frames=4)
    _hold(guard)
    _walk(guard, 0.0, frames=20, start_s=1.0)

    guard.note_applied(2.0, forward=False)
    verdict = guard.update(_motion(0.0), now_s=2.1)

    assert verdict.state is ProgressState.UNKNOWN
    assert "not being held" in verdict.reason


# ---------------------------------------------------------------------------
# The relative fallback
# ---------------------------------------------------------------------------


def test_the_fallback_abstains_until_it_has_a_real_span_of_samples() -> None:
    guard = ProgressGuard(UNCALIBRATED_BASELINE, ProgressConfig(fallback_min_samples=10))
    _hold(guard)

    verdicts = _walk(guard, 0.5, frames=5, start_s=1.0)

    assert all(v.state is ProgressState.UNKNOWN for v in verdicts)
    assert all("samples" in v.reason for v in verdicts)


def test_a_burst_inside_a_fraction_of_a_second_is_not_a_walk() -> None:
    guard = ProgressGuard(
        UNCALIBRATED_BASELINE,
        ProgressConfig(fallback_min_samples=10, fallback_min_span_s=1.2),
    )
    _hold(guard)

    verdicts = _walk(guard, 0.5, frames=30, start_s=1.0, fps=600.0)

    assert all(v.state is ProgressState.UNKNOWN for v in verdicts), (
        "the fallback accepted a reference measured over a twentieth of a second"
    )


def test_the_fallback_recommends_recovery_when_the_character_own_speed_collapses() -> None:
    guard = ProgressGuard(
        UNCALIBRATED_BASELINE,
        ProgressConfig(suspect_after_ms=300, confirm_frames=3, fallback_min_samples=10),
    )
    _hold(guard)
    _walk(guard, 0.5, frames=120, start_s=1.0)

    stalled = _walk(guard, 0.0, frames=40, start_s=4.0)

    assert stalled[-1].recover, "a collapse against the character's own speed was ignored"
    assert stalled[-1].provisional, "a fallback verdict must say it is provisional"
    assert "own recent speed" in stalled[-1].reason


def test_the_fallback_is_stricter_than_a_matured_baseline() -> None:
    """Its reference is unproven, so it earns a harsher test."""
    config = ProgressConfig()

    assert config.fallback_stall_fraction < LocomotionBaseline.RUNTIME_STALL_FRACTION


def test_a_matured_baseline_takes_over_from_the_fallback() -> None:
    guard = ProgressGuard(UNCALIBRATED_BASELINE, ProgressConfig(fallback_min_samples=10))
    _hold(guard)
    _walk(guard, 0.5, frames=120, start_s=1.0)
    guard.adopt_baseline(CALIBRATED)

    verdict = guard.update(_motion(0.5), now_s=4.0)

    assert not verdict.provisional
    assert verdict.state is ProgressState.PROGRESSING


# ---------------------------------------------------------------------------
# Bookkeeping
# ---------------------------------------------------------------------------


def test_resetting_the_guard_forgets_everything() -> None:
    guard = _guard(suspect_after_ms=1)
    _hold(guard)
    _walk(guard, 0.0, frames=30, start_s=1.0)

    guard.reset()

    assert guard.state is ProgressState.UNKNOWN
    assert not guard.ledger.holding()
    assert guard.stall_ms(5.0) == 0.0


def test_the_guard_records_a_traversability_contract_for_the_sector_memory() -> None:
    guard = _guard()
    _hold(guard)
    guard.update(_motion(0.5), now_s=1.0, commanded_heading_deg=12.0, anchor_px=(10.0, 20.0))

    record = guard.history()[-1]

    assert record.commanded_forward
    assert record.commanded_heading_deg == 12.0
    assert record.anchor_px == (10.0, 20.0)
    assert record.progress_state is ProgressState.PROGRESSING


def test_the_traversability_record_is_bounded() -> None:
    guard = _guard()
    _hold(guard)
    for index in range(2000):
        guard.update(_motion(0.5), now_s=1.0 + index / 60.0)

    assert len(guard.history()) <= 240


def test_the_guard_never_recommends_a_maneuver() -> None:
    """It says movement has stopped. *Which way to go round* is the recovery
    ladder's decision, taken with the sector memory, and none of that vocabulary
    exists on a verdict."""
    guard = _guard(suspect_after_ms=1)
    _hold(guard)
    verdict = _walk(guard, 0.0, frames=30, start_s=1.0)[-1]

    names = set(vars(verdict))
    assert "recover" in names
    assert not names & {"side", "strafe", "jump", "detour", "rung"}
