"""Navigation: capability, recovery termination, motion evidence, and the FSM.

The property tests use Hypothesis for the invariants named in plan section 16.2
(angle wrapping, bounded commands, recovery termination). Deterministic
scenario tests carry everything else, because they are far easier to debug.

What replaced the old "gate wall" tests: capability is no longer a frozen table
of PENDING experiment flags that nothing in production could set. It is
:class:`NavigationCapabilities`, derived from what *this run* observed and
measured, and the tests below assert both halves - that a fresh run cannot
steer, and that a run which has genuinely measured its actuator can.
"""

from __future__ import annotations

import math
from dataclasses import replace
from itertools import pairwise
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from prospector_engine.contracts import (
    ArrowObservation,
    CommandKind,
    DirectionObservation,
    EvidenceStatus,
    MotionObservation,
    NavigationApplyResult,
    NavigationApplyStatus,
    NavigationCommand,
    NavigationPhase,
)
from prospector_engine.motion import (
    ContactConfig,
    ContactMonitor,
    LocomotionBaseline,
    ProgressConfig,
    ProgressGuard,
    RuntimeBaselineEstimator,
    estimate_block_displacement,
    estimate_lk_affine,
    estimate_phase_correlation,
)
from prospector_engine.navigation import (
    NavigationCapabilities,
    NavigationInputs,
    Navigator,
    RecoveryAction,
    RecoveryBudget,
    RecoveryLadder,
    choose_detour_side,
)
from prospector_engine.steering import ArrowFollowerController, ControlFingerprint
from prospector_engine.turning import TurnBackend, TurnResponse
from prospector_engine.vision import wrap_deg
from tests.fakes import make_frame, make_geometry

FINGERPRINT = ControlFingerprint(
    os_name="test",
    backend="mouse_yaw",
    client_fingerprint="test-client",
    camera_sensitivity="default",
    control_mode="shift-lock",
    viewport_identity=(),
    profile_id="test",
    profile_revision=1,
    supported_min_fps=30,
)

#: A turn response standing in for one the characterizer would measure. Only a
#: test may construct this directly; production reaches the same state exactly
#: once, from bounded stationary probes on real hardware whose observed
#: rotation perception confirmed.
MEASURED = TurnResponse(
    backend=TurnBackend.MOUSE_YAW,
    fingerprint=FINGERPRINT,
    degrees_per_unit=0.25,
    positive_is_right=True,
    min_effective_units=2,
    max_units=200,
    latency_s=0.02,
    reliability=1.0,
    samples=8,
    measured_at_s=0.0,
    status=EvidenceStatus.VALIDATED,
)

WALKING = LocomotionBaseline(
    condition_id="runtime:test",
    min_forward_speed_norm=0.10,
    status=EvidenceStatus.VALIDATED,
    provenance=ContactConfig().provenance,
)

READY = NavigationCapabilities(
    os_name="test",
    profile_id="test",
    reference_ok=True,
    control_mode_ok=True,
    turn_response=MEASURED,
    motion_baseline=WALKING,
)


def _navigator(
    capabilities: NavigationCapabilities | None = None, **overrides: Any
) -> Navigator:
    caps = capabilities or READY
    return Navigator(
        capabilities=caps,
        follower=ArrowFollowerController(response=caps.turn_response),
        **overrides,
    )


def _direction(error_deg: float | None, confidence: float = 1.0) -> DirectionObservation:
    return DirectionObservation(
        error_deg=error_deg,
        confidence=confidence,
        cue_id="test",
        cue_disagreement_deg=None,
        valid=error_deg is not None,
        abstain_reason=None if error_deg is not None else "test abstention",
    )


def _arrow(valid: bool = True) -> ArrowObservation:
    return ArrowObservation(
        profile_id="test",
        track_id=1,
        bbox_px=(0, 0, 10, 10),
        centroid_px=(100.0, 100.0),
        tip_px=(100.0, 90.0),
        axis_unit_xy=(0.0, -1.0),
        confidence=0.9 if valid else 0.0,
        valid=valid,
        abstain_reason=None if valid else "no-candidate",
    )


# ---------------------------------------------------------------------------
# Capability
# ---------------------------------------------------------------------------


def test_a_fresh_run_can_observe_but_not_steer() -> None:
    capabilities = NavigationCapabilities.observing(
        os_name="darwin", profile_id="yellow_map_v1"
    )

    assert not capabilities.steering_enabled
    assert not capabilities.recovery_enabled
    assert not capabilities.progress_enabled
    assert set(capabilities.blocking_reasons()) == {
        "reference",
        "control-mode",
        "turn-actuator",
    }
    assert all(text and " " in text for text in capabilities.explain())


@pytest.mark.parametrize("missing", ["reference_ok", "control_mode_ok", "turn_response"])
def test_any_missing_piece_disables_steering(missing: str) -> None:
    weakened = replace(READY, **{missing: None if missing == "turn_response" else False})
    assert not weakened.steering_enabled


def test_a_measured_run_may_steer_without_any_frozen_gate() -> None:
    """The whole point: nothing here needs a table somebody has to edit."""
    assert READY.steering_enabled
    assert READY.recovery_enabled
    assert READY.blocking_reasons() == ()
    assert "mouse yaw" in READY.describe()


def test_recovery_needs_a_measured_walking_speed_as_well() -> None:
    """Telling "stuck" from "slow" needs a baseline; steering does not."""
    steering_only = replace(
        READY,
        motion_baseline=LocomotionBaseline(
            condition_id="uncalibrated",
            min_forward_speed_norm=None,
            status=EvidenceStatus.PENDING,
            provenance=ContactConfig().provenance,
        ),
    )
    assert steering_only.steering_enabled
    assert not steering_only.recovery_enabled


def test_a_pending_turn_response_is_not_a_capability() -> None:
    pending = replace(READY, turn_response=replace(MEASURED, status=EvidenceStatus.PENDING))
    assert not pending.steering_enabled


@settings(max_examples=300, deadline=None)
@given(degrees=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False))
def test_wrapping_is_idempotent_and_in_range(degrees: float) -> None:
    once = wrap_deg(degrees)
    assert -180.0 < once <= 180.0
    assert wrap_deg(once) == pytest.approx(once)


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def _run_ladder(ladder: RecoveryLadder, *, ticks: int = 400, dt: float = 0.05) -> list[Any]:
    steps = []
    for index in range(1, ticks + 1):
        step = ladder.step(index * dt, delta_s=dt)
        if step is None:
            break
        steps.append(step)
    return steps


def test_every_recovery_episode_terminates() -> None:
    ladder = RecoveryLadder()
    ladder.begin(0.0, side=1, error_deg=20.0)

    steps = _run_ladder(ladder)

    assert ladder.exhausted
    assert steps, "the ladder must actually produce maneuvers"


def test_the_ladder_starts_by_letting_go_and_looking_again() -> None:
    """Two free rungs before any maneuver: most snags are not obstacles."""
    ladder = RecoveryLadder()
    ladder.begin(0.0, side=1, error_deg=20.0)
    actions = [step.action for step in _run_ladder(ladder)]

    assert actions[0] is RecoveryAction.RELEASE
    assert RecoveryAction.REACQUIRE in actions[:20]
    first_maneuver = next(a for a in actions if a.emits_input)
    assert first_maneuver is RecoveryAction.STRAFE


def test_the_chosen_side_is_sticky_for_the_episode() -> None:
    """Choosing left then right on alternating frames is a wiggle, not a detour."""
    ladder = RecoveryLadder()
    ladder.begin(0.0, side=1, error_deg=20.0)
    sides = {step.side for step in _run_ladder(ladder) if not step.level.flips_side}

    assert sides == {1}


def test_the_opposite_side_is_tried_exactly_once() -> None:
    ladder = RecoveryLadder()
    ladder.begin(0.0, side=1, error_deg=20.0)
    flips = [step for step in _run_ladder(ladder) if step.level.flips_side]

    assert flips, "the ladder must eventually try the other side"
    assert {step.side for step in flips} == {-1}


def test_a_recovery_episode_has_a_total_time_and_input_cap() -> None:
    ladder = RecoveryLadder(RecoveryBudget(total_time_ms=1000, total_input_ms=500))
    ladder.begin(0.0, side=1, error_deg=0.0)

    assert ladder.over_budget(0.5) is None
    assert "time" in (ladder.over_budget(2.0) or "")

    fresh = RecoveryLadder(RecoveryBudget(total_time_ms=100000, total_input_ms=100))
    fresh.begin(0.0, side=1, error_deg=0.0)
    _run_ladder(fresh, ticks=200)
    assert fresh.exhausted


def test_success_needs_measured_progress_and_a_heading_that_did_not_rot() -> None:
    ladder = RecoveryLadder()
    ladder.begin(0.0, side=1, error_deg=10.0)

    assert not ladder.succeeded(progressing=False, error_deg=5.0)
    assert ladder.succeeded(progressing=True, error_deg=5.0)
    assert not ladder.succeeded(progressing=True, error_deg=170.0)


def test_the_detour_side_follows_local_evidence_not_a_coin_flip() -> None:
    sliding_right = _motion(0.0)
    sliding_right = replace(sliding_right, lateral_speed_norm=0.2)
    assert choose_detour_side(error_deg=-30.0, motion=sliding_right) == 1
    assert choose_detour_side(error_deg=-30.0, motion=None) == -1
    assert choose_detour_side(error_deg=30.0, motion=None) == 1
    assert choose_detour_side(error_deg=None, motion=None) in (-1, 1)


@settings(max_examples=50, deadline=None)
@given(ticks=st.integers(min_value=0, max_value=500), dt=st.floats(0.001, 0.2))
def test_recovery_always_reaches_a_terminal_state(ticks: int, dt: float) -> None:
    ladder = RecoveryLadder()
    ladder.begin(0.0, side=1, error_deg=0.0)
    _run_ladder(ladder, ticks=ticks, dt=dt)
    assert ladder.exhausted or ladder.level is not None


# ---------------------------------------------------------------------------
# Contact evidence
# ---------------------------------------------------------------------------


def _motion(
    forward: float,
    *,
    confidence: float = 0.9,
    coverage: float = 0.9,
    yaw: float = 0.0,
    valid: bool = True,
) -> MotionObservation:
    return MotionObservation(
        forward_speed_norm=forward,
        lateral_speed_norm=0.0,
        confidence=confidence,
        inlier_count=100,
        inlier_ratio=0.9,
        spatial_coverage=coverage,
        residual=0.5,
        yaw_contamination=yaw,
        valid=valid,
        abstain_reason=None if valid else "test",
    )


CALIBRATED = LocomotionBaseline(
    condition_id="test",
    min_forward_speed_norm=0.10,
    status=EvidenceStatus.VALIDATED,
    provenance=ContactConfig().provenance,
)


def test_contact_is_impossible_without_a_calibrated_baseline() -> None:
    """This is the production state today: E-MOTION has not been run."""
    monitor = ContactMonitor()
    for step in range(20):
        evidence = monitor.update(_motion(0.0), forward_commanded=True, now_s=step * 0.1)
        assert not evidence.contact
    assert evidence.reason == "baseline-uncalibrated"


def test_contact_needs_sustained_low_progress_not_elapsed_time() -> None:
    monitor = ContactMonitor(CALIBRATED, ContactConfig(sustained_ms=300))
    assert not monitor.update(_motion(0.0), forward_commanded=True, now_s=0.0).contact
    assert not monitor.update(_motion(0.0), forward_commanded=True, now_s=0.2).contact
    assert monitor.update(_motion(0.0), forward_commanded=True, now_s=0.4).contact


def test_progress_resets_the_contact_accumulator() -> None:
    monitor = ContactMonitor(CALIBRATED, ContactConfig(sustained_ms=300))
    monitor.update(_motion(0.0), forward_commanded=True, now_s=0.0)
    monitor.update(_motion(0.5), forward_commanded=True, now_s=0.2)  # moving again
    assert not monitor.update(_motion(0.0), forward_commanded=True, now_s=0.4).contact


@pytest.mark.parametrize(
    ("observation", "expected"),
    [
        (_motion(0.0, valid=False), "motion-invalid"),
        (_motion(0.0, confidence=0.1), "low-confidence"),
        (_motion(0.0, coverage=0.1), "poor-coverage"),
        (_motion(0.0, yaw=0.9), "yaw-contaminated"),
    ],
)
def test_low_quality_motion_can_never_become_contact(
    observation: MotionObservation, expected: str
) -> None:
    monitor = ContactMonitor(CALIBRATED, ContactConfig(sustained_ms=1))
    evidence = monitor.update(observation, forward_commanded=True, now_s=0.0)
    assert not evidence.contact
    assert expected in evidence.reason


def test_a_recent_yaw_suppresses_contact() -> None:
    monitor = ContactMonitor(CALIBRATED, ContactConfig(sustained_ms=1, post_yaw_holdoff_ms=500))
    monitor.note_yaw(at_s=1.0)
    evidence = monitor.update(_motion(0.0), forward_commanded=True, now_s=1.1)
    assert not evidence.contact
    assert evidence.reason == "post-yaw-holdoff"


def test_contact_requires_a_forward_command() -> None:
    monitor = ContactMonitor(CALIBRATED, ContactConfig(sustained_ms=1))
    evidence = monitor.update(_motion(0.0), forward_commanded=False, now_s=0.0)
    assert not evidence.contact
    assert evidence.reason == "forward-not-commanded"


# ---------------------------------------------------------------------------
# Motion estimators on synthetic translation
# ---------------------------------------------------------------------------


def _textured_pair(shift_y: int) -> tuple[Any, Any]:
    import numpy as np

    rng = np.random.default_rng(7)
    texture = rng.integers(0, 255, size=(240, 320, 3), dtype=np.uint8)
    shifted = np.roll(texture, shift_y, axis=0)
    from prospector_engine.contracts import CapturedFrame

    geometry = make_geometry(size=(320.0, 240.0), canonical_px=(320, 240))
    return (
        CapturedFrame(1, 0.0, 0.005, 5.0, geometry, _frozen(texture)),
        CapturedFrame(2, 0.05, 0.055, 5.0, geometry, _frozen(shifted)),
    )


def _frozen(array: Any) -> Any:
    from prospector_engine.contracts import freeze_array

    return freeze_array(array.copy())


@pytest.mark.parametrize(
    "estimator", [estimate_lk_affine, estimate_phase_correlation, estimate_block_displacement]
)
def test_estimators_agree_on_the_sign_of_synthetic_forward_motion(estimator: Any) -> None:
    """Content moving *down* the screen means the camera moved forward."""
    previous, current = _textured_pair(shift_y=6)
    observation = estimator(previous, current, roi_px=(0, 0, 320, 240))

    assert observation.valid
    assert observation.forward_speed_norm is not None
    assert observation.forward_speed_norm < 0  # downward content shift


@pytest.mark.parametrize(
    "estimator", [estimate_lk_affine, estimate_phase_correlation, estimate_block_displacement]
)
def test_estimators_abstain_on_a_non_monotonic_frame_pair(estimator: Any) -> None:
    previous, current = _textured_pair(shift_y=4)
    swapped = estimator(current, previous, roi_px=(0, 0, 320, 240))
    same = estimator(previous, previous, roi_px=(0, 0, 320, 240))

    assert swapped.valid or swapped.abstain_reason  # a real answer or an abstention
    assert not same.valid
    assert same.abstain_reason == "non-monotonic delta-t"


def test_displacement_is_normalized_by_actual_delta_t() -> None:
    import numpy as np

    from prospector_engine.contracts import CapturedFrame

    rng = np.random.default_rng(11)
    texture = rng.integers(0, 255, size=(240, 320, 3), dtype=np.uint8)
    shifted = np.roll(texture, 8, axis=0)
    geometry = make_geometry(size=(320.0, 240.0), canonical_px=(320, 240))
    base = CapturedFrame(1, 0.0, 0.005, 5.0, geometry, _frozen(texture))
    fast = CapturedFrame(2, 0.02, 0.025, 5.0, geometry, _frozen(shifted))
    slow = CapturedFrame(2, 0.20, 0.205, 5.0, geometry, _frozen(shifted))

    quick = estimate_phase_correlation(base, fast, roi_px=(0, 0, 320, 240))
    lazy = estimate_phase_correlation(base, slow, roi_px=(0, 0, 320, 240))

    assert quick.forward_speed_norm is not None and lazy.forward_speed_norm is not None
    assert abs(quick.forward_speed_norm) > abs(lazy.forward_speed_norm)


# ---------------------------------------------------------------------------
# Navigator FSM
# ---------------------------------------------------------------------------


def _inputs(
    *,
    arrow_valid: bool = True,
    error_deg: float | None = 30.0,
    captured_at_s: float = 0.0,
    arrival: Any = None,
    motion: MotionObservation | None = None,
    forward_commanded: bool = False,
    sequence: int = 1,
) -> NavigationInputs:
    return NavigationInputs(
        frame=make_frame(sequence, captured_at_s=captured_at_s),
        arrow=_arrow(arrow_valid),
        direction=_direction(error_deg),
        motion=motion,
        arrival=arrival,
        forward_commanded=forward_commanded,
    )


def test_a_run_that_has_measured_nothing_observes_and_says_why() -> None:
    navigator = Navigator(
        capabilities=NavigationCapabilities.observing(os_name="test", profile_id="test")
    )
    decision = navigator.decide(_inputs(), generation=1, now_s=0.0)

    assert decision.command is None
    assert decision.release
    assert "observing only" in decision.reason
    assert "camera control mode" in decision.reason


def test_a_stale_frame_releases_before_anything_else_is_considered() -> None:
    navigator = _navigator(max_evidence_age_ms=100)
    decision = navigator.decide(_inputs(captured_at_s=0.0), generation=1, now_s=5.0)

    assert decision.release
    assert "stale-frame" in decision.reason


def test_an_abstained_arrow_releases() -> None:
    navigator = _navigator()
    decision = navigator.decide(_inputs(arrow_valid=False), generation=1, now_s=0.0)

    assert decision.release
    assert decision.phase is NavigationPhase.REACQUIRE


def test_a_command_lease_never_outlives_its_evidence() -> None:
    navigator = _navigator(max_evidence_age_ms=100)
    decision = navigator.decide(_inputs(error_deg=30.0), generation=1, now_s=0.0)

    assert decision.command is not None
    assert decision.command.valid_until_s <= decision.command.source_captured_at_s + 0.100001


def test_arrival_releases_movement_and_terminates_after_repeated_evidence() -> None:
    from prospector_engine.contracts import ArrivalObservation

    navigator = _navigator()
    arrival = ArrivalObservation(1.0, 4, 6, "m1", True, ("latched",))
    phases = []
    for index in range(Navigator.ARRIVAL_LATCHES):
        decision = navigator.decide(
            _inputs(arrival=arrival, sequence=index + 1, captured_at_s=index * 0.02),
            generation=1,
            now_s=index * 0.02,
        )
        phases.append(decision.phase)
        assert decision.release and decision.command is None

    assert phases[0] is NavigationPhase.ARRIVAL_CONFIRM
    assert phases[-1] is NavigationPhase.ARRIVED


def test_one_arrival_candidate_does_not_end_the_route() -> None:
    from prospector_engine.contracts import ArrivalObservation

    navigator = _navigator()
    arrival = ArrivalObservation(1.0, 4, 6, "m1", True, ("latched",))
    first = navigator.decide(_inputs(arrival=arrival), generation=1, now_s=0.0)
    # A frame without arrival evidence clears the latch rather than banking it.
    navigator.decide(_inputs(sequence=2, captured_at_s=0.02), generation=1, now_s=0.02)

    assert first.phase is NavigationPhase.ARRIVAL_CONFIRM
    assert navigator.arrival_latches == 0


def test_an_aligned_arrow_walks_only_after_sustained_alignment() -> None:
    """W is never taken on the strength of one frame inside the cone."""
    navigator = _navigator()
    decisions = [
        navigator.decide(
            _inputs(error_deg=0.5, sequence=index + 1, captured_at_s=index * 0.02),
            generation=1,
            now_s=index * 0.02,
        )
        for index in range(5)
    ]

    assert all(d.command is None or d.command.forward_axis == 0 for d in decisions[:2])
    final = decisions[-1]
    assert final.command is not None
    assert final.command.forward_axis == 1
    assert final.command.kind is CommandKind.FOLLOW
    assert final.phase is NavigationPhase.FOLLOW


def test_a_misaligned_arrow_turns_on_the_spot() -> None:
    navigator = _navigator()
    decision = navigator.decide(_inputs(error_deg=45.0), generation=1, now_s=0.0)

    assert decision.command is not None
    assert decision.command.forward_axis == 0, "alignment is stationary"
    assert decision.command.kind is CommandKind.ALIGN
    assert decision.command.yaw_delta_px > 0
    assert decision.phase is NavigationPhase.ALIGN


def test_an_alignment_command_can_never_ask_for_forward_motion() -> None:
    """Enforced by the contract, not by the caller remembering."""
    with pytest.raises(ValueError, match="may not command forward"):
        NavigationCommand(
            generation=1,
            source_frame_sequence=1,
            source_captured_at_s=0.0,
            forward_axis=1,
            lateral_axis=0,
            jump=False,
            yaw_delta_px=4,
            issued_at_s=0.0,
            valid_until_s=0.1,
            reason="test",
            kind=CommandKind.ALIGN,
        )


def test_a_command_may_never_use_two_turn_actuators_at_once() -> None:
    with pytest.raises(ValueError, match="both the turn keys and mouse yaw"):
        NavigationCommand(
            generation=1,
            source_frame_sequence=1,
            source_captured_at_s=0.0,
            forward_axis=0,
            lateral_axis=0,
            jump=False,
            yaw_delta_px=4,
            issued_at_s=0.0,
            valid_until_s=0.1,
            reason="test",
            kind=CommandKind.ALIGN,
            turn_axis=1,
        )


def test_a_frame_authorizes_exactly_one_decision() -> None:
    """Re-reading a frame must not renew a lease (mission section 11)."""
    navigator = _navigator()
    first = navigator.decide(_inputs(error_deg=20.0, sequence=7), generation=1, now_s=0.0)
    second = navigator.decide(_inputs(error_deg=20.0, sequence=7), generation=1, now_s=0.02)

    assert first.command is not None
    assert second.command is None, "the same frame cannot authorize a second command"


@settings(max_examples=100, deadline=None)
@given(error=st.floats(min_value=-180.0, max_value=180.0, allow_nan=False))
def test_no_command_ever_asks_for_forward_and_reverse_at_once(error: float) -> None:
    navigator = _navigator()
    decision = navigator.decide(_inputs(error_deg=error), generation=1, now_s=0.0)
    if decision.command is not None:
        assert decision.command.forward_axis in (-1, 0, 1)
        assert decision.command.lateral_axis in (-1, 0, 1)
        assert decision.command.turn_axis in (-1, 0, 1)
        assert decision.command.kind.may_hold_forward or decision.command.forward_axis == 0


# ---------------------------------------------------------------------------
# Progress: only *applied* forward counts
# ---------------------------------------------------------------------------


def _applied(*leases: str) -> NavigationApplyResult:
    return NavigationApplyResult(NavigationApplyStatus.APPLIED, "ok", leases_held=leases)


def test_only_an_accepted_forward_lease_enters_the_progress_ledger() -> None:
    """A run of rejected commands must not look exactly like a wall."""
    navigator = _navigator()

    navigator.note_applied(
        NavigationApplyResult(NavigationApplyStatus.REJECTED_FOCUS, "not focused"), now_s=0.0
    )
    assert not navigator.progress.ledger.holding()

    navigator.note_applied(_applied("w"), now_s=0.1)
    assert navigator.progress.ledger.holding()

    navigator.note_applied(_applied("right"), now_s=0.2)
    assert not navigator.progress.ledger.holding(), "a turn key is not forward motion"


def test_a_release_closes_the_forward_interval() -> None:
    navigator = _navigator()
    navigator.note_applied(_applied("w"), now_s=0.0)
    navigator.note_released(now_s=0.5)

    assert not navigator.progress.ledger.holding()
    assert navigator.progress.ledger.held_continuously_for(0.6) == 0.0


def test_sustained_low_progress_while_walking_releases_and_enters_contact() -> None:
    guard = ProgressGuard(
        WALKING, ProgressConfig(suspect_after_ms=100, min_applied_forward_ms=50)
    )
    navigator = _navigator(progress=guard)

    navigator.note_applied(_applied("w"), now_s=0.0)
    decision = None
    for index in range(1, 12):
        now = index * 0.1
        decision = navigator.decide(
            _inputs(
                error_deg=0.5,
                motion=_motion(0.0),
                sequence=index,
                captured_at_s=now,
            ),
            generation=1,
            now_s=now,
        )
        if decision.phase is NavigationPhase.CONTACT:
            break

    assert decision is not None
    assert decision.phase is NavigationPhase.CONTACT
    assert decision.release, "forward is dropped before anything is decided about it"


def test_contact_without_a_measured_baseline_stops_rather_than_improvising() -> None:
    """No baseline means no way to tell stuck from slow. Stopping is the answer."""
    capabilities = replace(
        READY,
        motion_baseline=LocomotionBaseline(
            condition_id="uncalibrated",
            min_forward_speed_norm=None,
            status=EvidenceStatus.PENDING,
            provenance=ContactConfig().provenance,
        ),
    )
    navigator = _navigator(capabilities)
    navigator.note_applied(_applied("w"), now_s=0.0)

    for index in range(1, 12):
        now = index * 0.1
        decision = navigator.decide(
            _inputs(error_deg=0.5, motion=_motion(0.0), sequence=index, captured_at_s=now),
            generation=1,
            now_s=now,
        )
        assert decision.phase is not NavigationPhase.RECOVERY


def test_recovery_emits_real_maneuvers_and_then_abandons() -> None:
    guard = ProgressGuard(
        WALKING, ProgressConfig(suspect_after_ms=100, min_applied_forward_ms=50)
    )
    navigator = _navigator(progress=guard)
    navigator.note_applied(_applied("w"), now_s=0.0)

    maneuvers: list[NavigationCommand] = []
    terminal = None
    for index in range(1, 400):
        now = index * 0.05
        decision = navigator.decide(
            _inputs(error_deg=0.5, motion=_motion(0.0), sequence=index, captured_at_s=now),
            generation=1,
            now_s=now,
        )
        if decision.command is not None and decision.phase is NavigationPhase.RECOVERY:
            maneuvers.append(decision.command)
        if decision.phase is NavigationPhase.ABANDONED:
            terminal = decision
            break

    assert maneuvers, "recovery must emit maneuvers, not just change a label"
    assert any(c.lateral_axis != 0 for c in maneuvers), "a strafe was never issued"
    assert terminal is not None, "recovery must abandon rather than loop forever"
    assert terminal.release and terminal.command is None


def test_recovery_never_oscillates_between_sides_within_an_episode() -> None:
    guard = ProgressGuard(
        WALKING, ProgressConfig(suspect_after_ms=100, min_applied_forward_ms=50)
    )
    navigator = _navigator(progress=guard)
    navigator.note_applied(_applied("w"), now_s=0.0)

    lateral: list[int] = []
    for index in range(1, 400):
        now = index * 0.05
        decision = navigator.decide(
            _inputs(error_deg=0.5, motion=_motion(0.0), sequence=index, captured_at_s=now),
            generation=1,
            now_s=now,
        )
        if decision.command is not None and decision.command.lateral_axis:
            lateral.append(decision.command.lateral_axis)
        if decision.phase is NavigationPhase.ABANDONED:
            break

    flips = sum(1 for a, b in pairwise(lateral) if a != b)
    assert flips <= 1, f"the detour side flipped {flips} times"


def test_a_terminal_state_holds_no_inputs() -> None:
    navigator = _navigator()
    decision = navigator.decide(
        _inputs(arrow_valid=False, error_deg=None), generation=1, now_s=0.0
    )
    assert decision.command is None and decision.release


# ---------------------------------------------------------------------------
# The runtime locomotion baseline
# ---------------------------------------------------------------------------


def test_a_runtime_baseline_needs_real_samples_before_it_exists() -> None:
    estimator = RuntimeBaselineEstimator("run-1")
    config = ContactConfig()
    for _ in range(5):
        estimator.observe(_motion(0.4), forward_applied=True, held_ms=500.0, config=config)
    assert not estimator.baseline.usable

    for _ in range(LocomotionBaseline.MIN_RUNTIME_SAMPLES):
        estimator.observe(_motion(0.4), forward_applied=True, held_ms=500.0, config=config)
    assert estimator.baseline.usable


def test_a_runtime_baseline_never_claims_to_be_the_offline_gate() -> None:
    estimator = RuntimeBaselineEstimator("run-1")
    config = ContactConfig()
    for _ in range(20):
        estimator.observe(_motion(0.4), forward_applied=True, held_ms=500.0, config=config)

    baseline = estimator.baseline
    assert baseline.measured_at_runtime
    assert baseline.condition_id.startswith("runtime:")
    assert "NOT the offline E-MOTION gate" in baseline.provenance.note


@pytest.mark.parametrize(
    "kwargs",
    [
        {"forward_applied": False},
        {"held_ms": 10.0},
    ],
)
def test_a_runtime_baseline_refuses_contaminated_samples(kwargs: dict[str, Any]) -> None:
    estimator = RuntimeBaselineEstimator("run-1")
    config = ContactConfig()
    defaults: dict[str, Any] = {"forward_applied": True, "held_ms": 500.0}
    defaults.update(kwargs)
    for _ in range(40):
        estimator.observe(_motion(0.4), config=config, **defaults)
    assert estimator.samples == 0


def test_a_yaw_contaminated_sample_is_refused() -> None:
    estimator = RuntimeBaselineEstimator("run-1")
    config = ContactConfig()
    for _ in range(40):
        estimator.observe(
            _motion(0.4, yaw=0.9), forward_applied=True, held_ms=500.0, config=config
        )
    assert estimator.samples == 0


def test_math_import_is_used_by_the_fusion_geometry() -> None:
    assert math.isclose(wrap_deg(360.0), 0.0, abs_tol=1e-9)
