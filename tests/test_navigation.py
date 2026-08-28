"""Navigation FSM, steering, recovery termination, motion, and the gate wall.

The property tests use Hypothesis for the invariants named in plan section 16.2
(angle wrapping, bounded commands, recovery termination). Deterministic
scenario tests carry everything else, because they are far easier to debug.
"""

from __future__ import annotations

import math
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
    NavigationCommand,
    NavigationPhase,
)
from prospector_engine.motion import (
    ContactConfig,
    ContactMonitor,
    LocomotionBaseline,
    estimate_block_displacement,
    estimate_lk_affine,
    estimate_phase_correlation,
)
from prospector_engine.navigation import (
    RECOVERY_LADDER,
    NavigationGates,
    NavigationInputs,
    Navigator,
    RecoveryBudget,
    RecoveryLadder,
    SteeringConfig,
    SteeringController,
)
from prospector_engine.vision import wrap_deg
from tests.fakes import make_frame, make_geometry

ALL_PASSED = NavigationGates(
    os_name="test",
    profile_id="test",
    **{
        field: EvidenceStatus.VALIDATED
        for field in NavigationGates.__dataclass_fields__
        if field.startswith("e_")
    },
)


def _calibrated_navigator(gates: NavigationGates | None = None, **overrides: Any) -> Navigator:
    """A navigator whose yaw calibration is pretended to have passed E-YAW.

    Only a test may do this. Production reaches the same state exactly once:
    after physically armed yaw pulses on real hardware whose observed rotation
    perception confirmed. The point of the fixture is to exercise the control
    law without hardware, not to shortcut the gate.
    """
    from prospector_engine.steering import (
        CalibrationFingerprint,
        ShiftLockController,
        YawCalibration,
    )

    fingerprint = CalibrationFingerprint(
        os_name="test",
        backend="fake",
        client_fingerprint="test-client",
        camera_sensitivity="default",
        control_mode="shift-lock",
        viewport_identity=(),
        profile_id="test",
        profile_revision=1,
        supported_min_fps=30,
    )
    calibration = YawCalibration(
        fingerprint=fingerprint,
        degrees_per_unit=0.25,
        positive_is_right=True,
        min_effective_units=2,
        linear_range_units=(2, 160),
        saturation_units=200,
        response_delay_ms=18.0,
        repeatability_deg=0.7,
        reversal_backlash_deg=0.2,
        linear_fit_r2=0.995,
        repeats_per_magnitude=10,
        status=EvidenceStatus.VALIDATED,
    )
    return Navigator(
        gates=gates or ALL_PASSED,
        controller=ShiftLockController(calibration=calibration),
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
# Gates
# ---------------------------------------------------------------------------


def test_a_fresh_gate_set_disables_everything() -> None:
    gates = NavigationGates(os_name="darwin", profile_id="yellow_map_v0")
    assert not gates.steering_enabled
    assert not gates.recovery_enabled
    assert not gates.arrival_enabled
    assert not gates.next_map_enabled
    assert "E-YAW" in gates.blocking_reasons()


def test_recovery_needs_more_than_steering() -> None:
    """Steering passing is not enough: E-MOTION and E-RECOVERY are separate."""
    from dataclasses import replace

    steering_only = replace(ALL_PASSED, e_motion=EvidenceStatus.PENDING)
    assert steering_only.steering_enabled
    assert not steering_only.recovery_enabled


def test_a_single_pending_gate_disables_steering() -> None:
    from dataclasses import replace

    for field in (
        "e_view",
        "e_anchor",
        "e_forward",
        "e_prof",
        "e_dir_e2e",
        "e_yaw",
        "e_steer_cal",
        "e_steer_e2e",
    ):
        weakened = replace(ALL_PASSED, **{field: EvidenceStatus.PENDING})
        assert not weakened.steering_enabled, field


# ---------------------------------------------------------------------------
# Steering
# ---------------------------------------------------------------------------


def test_an_error_inside_the_deadband_produces_no_turn() -> None:
    controller = SteeringController(SteeringConfig(deadband_deg=6.0, hysteresis_deg=2.0))
    assert controller.update(_direction(3.0), now_s=0.0, frame_is_duplicate=False) == 0


def test_hysteresis_prevents_left_right_chatter() -> None:
    controller = SteeringController(SteeringConfig(deadband_deg=6.0, hysteresis_deg=4.0))
    # 7 deg is past the deadband but inside deadband+hysteresis from rest.
    assert controller.update(_direction(7.0), now_s=0.0, frame_is_duplicate=False) == 0
    # 12 deg breaks out, and then 7 deg keeps steering rather than snapping off.
    assert controller.update(_direction(12.0), now_s=0.1, frame_is_duplicate=False) > 0
    assert controller.update(_direction(7.0), now_s=0.2, frame_is_duplicate=False) > 0


def test_the_turn_sign_follows_the_error_sign() -> None:
    controller = SteeringController()
    right = controller.update(_direction(40.0), now_s=0.0, frame_is_duplicate=False)
    controller.reset()
    left = controller.update(_direction(-40.0), now_s=0.0, frame_is_duplicate=False)
    assert right > 0 and left < 0


def test_lower_confidence_reduces_magnitude_and_never_raises_gain() -> None:
    high = SteeringController().update(
        _direction(40.0, confidence=1.0), now_s=0.0, frame_is_duplicate=False
    )
    low = SteeringController().update(
        _direction(40.0, confidence=0.3), now_s=0.0, frame_is_duplicate=False
    )
    assert 0 < low <= high


def test_an_abstained_direction_releases_yaw_immediately() -> None:
    controller = SteeringController()
    controller.update(_direction(60.0), now_s=0.0, frame_is_duplicate=False)
    assert controller.update(_direction(None), now_s=0.1, frame_is_duplicate=False) == 0


def test_a_duplicate_frame_does_not_manufacture_a_derivative_spike() -> None:
    controller = SteeringController()
    controller.update(_direction(10.0), now_s=0.0, frame_is_duplicate=False)
    spike = controller.update(_direction(80.0), now_s=0.0001, frame_is_duplicate=True)
    limit = controller.config.max_turn_px_per_tick
    assert abs(spike) <= limit


@settings(max_examples=200, deadline=None)
@given(
    error=st.floats(min_value=-720.0, max_value=720.0, allow_nan=False),
    confidence=st.floats(min_value=0.0, max_value=1.0),
)
def test_the_turn_command_is_always_bounded(error: float, confidence: float) -> None:
    controller = SteeringController()
    turn = controller.update(
        _direction(error, confidence=confidence), now_s=0.0, frame_is_duplicate=False
    )
    assert abs(turn) <= controller.config.max_turn_px_per_tick


@settings(max_examples=300, deadline=None)
@given(degrees=st.floats(min_value=-1e6, max_value=1e6, allow_nan=False))
def test_wrapping_is_idempotent_and_in_range(degrees: float) -> None:
    once = wrap_deg(degrees)
    assert -180.0 < once <= 180.0
    assert wrap_deg(once) == pytest.approx(once)


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def test_every_recovery_episode_terminates() -> None:
    ladder = RecoveryLadder()
    ladder.begin(now_s=0.0, side=1)
    steps = 0
    while not ladder.exhausted and steps < 100:
        ladder.escalate()
        steps += 1
    assert ladder.exhausted
    assert steps <= sum(level.max_attempts for level in RECOVERY_LADDER)


def test_the_locked_side_cannot_flip_inside_the_cooldown() -> None:
    ladder = RecoveryLadder(RecoveryBudget(side_lock_cooldown_ms=1000))
    ladder.begin(now_s=0.0, side=1)

    assert ladder.switch_side(now_s=0.5) is False
    assert ladder.side == 1
    assert ladder.switch_side(now_s=1.5) is True
    assert ladder.side == -1


def test_a_recovery_episode_has_a_total_time_and_input_cap() -> None:
    ladder = RecoveryLadder(RecoveryBudget(total_time_ms=1000, total_input_ms=500))
    ladder.begin(now_s=0.0, side=1)

    assert ladder.over_budget(now_s=0.5) is None
    assert ladder.over_budget(now_s=2.0) == "recovery total time cap"

    fresh = RecoveryLadder(RecoveryBudget(total_time_ms=100000, total_input_ms=500))
    fresh.begin(now_s=0.0, side=1)
    fresh.note_input(900)
    assert fresh.over_budget(now_s=0.1) == "recovery total input cap"


@settings(max_examples=50, deadline=None)
@given(
    escalations=st.lists(st.booleans(), min_size=0, max_size=40),
    now=st.floats(min_value=0.0, max_value=60.0),
)
def test_recovery_always_reaches_a_terminal_state(escalations: list[bool], now: float) -> None:
    ladder = RecoveryLadder()
    ladder.begin(now_s=0.0, side=1)
    for switch in escalations:
        if switch:
            ladder.switch_side(now_s=now)
        else:
            ladder.escalate()
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


def test_the_navigator_refuses_to_steer_while_its_gates_are_pending() -> None:
    navigator = Navigator(gates=NavigationGates(os_name="test", profile_id="test"))
    decision = navigator.decide(_inputs(), generation=1, now_s=0.0)

    assert decision.command is None
    assert decision.release
    assert "steering disabled" in decision.reason


def test_a_stale_frame_releases_before_anything_else_is_considered() -> None:
    navigator = Navigator(gates=ALL_PASSED, max_evidence_age_ms=100)
    decision = navigator.decide(_inputs(captured_at_s=0.0), generation=1, now_s=5.0)

    assert decision.release
    assert "stale-frame" in decision.reason


def test_an_abstained_arrow_beyond_the_grace_window_releases() -> None:
    navigator = Navigator(gates=ALL_PASSED)
    decision = navigator.decide(_inputs(arrow_valid=False), generation=1, now_s=0.0)

    assert decision.release
    assert decision.phase is NavigationPhase.REACQUIRE


def test_the_arrow_loss_grace_keeps_forward_but_releases_yaw() -> None:
    navigator = Navigator(gates=ALL_PASSED)
    navigator.decide(_inputs(error_deg=0.0), generation=1, now_s=0.0)  # a valid arrow first

    decision = navigator.decide(_inputs(arrow_valid=False), generation=1, now_s=0.1)

    assert decision.command is not None
    assert decision.command.forward_axis == 1
    assert decision.command.yaw_delta_px == 0
    assert "yaw released" in decision.command.reason


def test_a_command_lease_never_outlives_its_evidence() -> None:
    navigator = _calibrated_navigator(max_evidence_age_ms=100)
    decision = navigator.decide(_inputs(error_deg=30.0), generation=1, now_s=0.0)

    assert decision.command is not None
    assert decision.command.valid_until_s <= decision.command.source_captured_at_s + 0.100001


def test_arrival_preempts_recovery_and_releases_movement() -> None:
    from prospector_engine.contracts import ArrivalObservation

    navigator = Navigator(gates=ALL_PASSED)
    arrival = ArrivalObservation(1.0, 4, 6, "m1", True, ("latched",))
    decision = navigator.decide(
        _inputs(arrival=arrival, motion=_motion(0.0), forward_commanded=True),
        generation=1,
        now_s=0.0,
    )

    assert decision.release
    assert decision.phase is NavigationPhase.ARRIVAL_CONFIRM
    assert navigator.arrival_latches == 1


def test_arrival_evidence_is_ignored_while_e_arrive_is_pending() -> None:
    from dataclasses import replace

    from prospector_engine.contracts import ArrivalObservation

    gates = replace(ALL_PASSED, e_arrive=EvidenceStatus.PENDING)
    navigator = Navigator(gates=gates)
    arrival = ArrivalObservation(1.0, 4, 6, "m1", True, ("latched",))
    decision = navigator.decide(_inputs(arrival=arrival), generation=1, now_s=0.0)

    assert decision.release
    assert "E-ARRIVE PENDING" in decision.reason
    assert navigator.arrival_latches == 0


def test_contact_abandons_while_recovery_is_ungated() -> None:
    from dataclasses import replace

    gates = replace(ALL_PASSED, e_recovery=EvidenceStatus.PENDING)
    monitor = ContactMonitor(CALIBRATED, ContactConfig(sustained_ms=1))
    navigator = _calibrated_navigator(gates=gates, contact=monitor)

    decision = navigator.decide(
        _inputs(error_deg=0.5, motion=_motion(0.0), forward_commanded=True, captured_at_s=0.0),
        generation=1,
        now_s=0.0,
    )
    # A fresh frame each tick: the contact must come from sustained low
    # progress, not from the frame going stale underneath it.
    decision = navigator.decide(
        _inputs(
            error_deg=0.5,
            motion=_motion(0.0),
            forward_commanded=True,
            captured_at_s=0.5,
            sequence=2,
        ),
        generation=1,
        now_s=0.5,
    )

    assert decision.phase is NavigationPhase.ABANDONED
    assert decision.release


def test_steering_is_refused_until_the_yaw_calibration_passes() -> None:
    """Every perception gate passing is still not permission to move a mouse."""
    navigator = Navigator(gates=ALL_PASSED)

    decision = navigator.decide(_inputs(error_deg=0.5), generation=1, now_s=0.0)

    assert decision.command is None
    assert decision.release
    assert "not calibrated" in decision.reason


def test_an_aligned_arrow_walks_only_after_sustained_alignment() -> None:
    """W is never taken on the strength of one frame inside the deadband."""
    navigator = _calibrated_navigator()
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
    navigator = _calibrated_navigator()
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


def test_a_frame_authorizes_exactly_one_decision() -> None:
    """Re-reading a frame must not renew a lease (mission section 11)."""
    navigator = _calibrated_navigator()
    first = navigator.decide(_inputs(error_deg=20.0, sequence=7), generation=1, now_s=0.0)
    second = navigator.decide(_inputs(error_deg=20.0, sequence=7), generation=1, now_s=0.02)

    assert first.command is not None
    assert second.command is None, "the same frame cannot authorize a second command"


@settings(max_examples=100, deadline=None)
@given(error=st.floats(min_value=-180.0, max_value=180.0, allow_nan=False))
def test_no_command_ever_asks_for_forward_and_reverse_at_once(error: float) -> None:
    navigator = _calibrated_navigator()
    decision = navigator.decide(_inputs(error_deg=error), generation=1, now_s=0.0)
    if decision.command is not None:
        assert decision.command.forward_axis in (-1, 0, 1)
        assert decision.command.lateral_axis in (-1, 0, 1)
        assert decision.command.kind.may_hold_forward or decision.command.forward_axis == 0


def test_the_live_worker_refuses_to_start_while_gates_are_pending() -> None:
    from prospector_engine.contracts import Cancellation, ModeResultKind
    from prospector_engine.navigation import PerceptionPipeline, make_live_worker
    from prospector_engine.vision import ArrowSegmenter, load_profiles

    profile = load_profiles().get("yellow_map_v0")
    assert profile is not None
    gates = NavigationGates(os_name="test", profile_id=profile.profile_id)
    worker = make_live_worker(lambda: PerceptionPipeline(ArrowSegmenter(profile)), gates)

    class _Session:
        def __init__(self) -> None:
            self.released: list[str] = []

        def release_navigation(self, reason: str) -> Any:
            self.released.append(reason)

        def apply_navigation_command(self, command: Any, evidence: Any) -> Any:
            raise AssertionError("a pending-gate worker must never apply a command")

    session = _Session()
    context = type(
        "Ctx",
        (),
        {
            "navigation": session,
            "cancellation": Cancellation(),
            "generation": 1,
            "frames": None,
            "on_phase": lambda self, phase: None,
            "on_status": lambda self, message: None,
        },
    )()
    result = worker(context)  # type: ignore[arg-type]

    assert result.kind is ModeResultKind.FAILED
    assert "PENDING" in result.detail
    assert session.released == ["gates-pending"]


def test_math_import_is_used_by_the_fusion_geometry() -> None:
    assert math.isclose(wrap_deg(360.0), 0.0, abs_tol=1e-9)
