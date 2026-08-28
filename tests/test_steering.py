"""Shift-Lock steering: calibration, control-mode proof, and the control law.

The controller is exercised against a **simulated camera** rather than a real
one, so the closed-loop behaviour the mission specifies - converge from both
sides, settle without overshooting, never take W outside the alignment
threshold - can be measured deterministically. That is a design check, not a
gate: E-YAW, E-STEER-CAL and E-STEER-E2E all require physically armed pulses on
real hardware, and none of them has been run.

The most important tests here are the refusals. A controller that steers
without a measured yaw calibration, or without positive proof that the player
is in Shift Lock, is the failure this whole module exists to prevent.
"""

from __future__ import annotations

import math
from itertools import pairwise

import pytest

from prospector_engine.contracts import (
    ArrowObservation,
    CommandKind,
    ControlState,
    DirectionObservation,
    EvidenceStatus,
)
from prospector_engine.steering import (
    CalibrationFingerprint,
    ShiftLockController,
    ShiftLockProof,
    SteeringInputs,
    SteeringLimits,
    YawCalibration,
    wrap_deg,
)

FINGERPRINT = CalibrationFingerprint(
    os_name="test",
    backend="fake",
    client_fingerprint="client-1",
    camera_sensitivity="default",
    control_mode="shift-lock",
    viewport_identity=("canonical", 1280, 720),
    profile_id="green_arrow_v1",
    profile_revision=1,
    supported_min_fps=30,
)

#: A calibration standing in for one E-YAW would produce. Only a test may
#: assert this; production reaches it through physically armed pulses whose
#: observed rotation perception confirmed.
CALIBRATED = YawCalibration(
    fingerprint=FINGERPRINT,
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


def _arrow(valid: bool = True) -> ArrowObservation:
    return ArrowObservation(
        profile_id="green_arrow_v1",
        track_id=1 if valid else None,
        bbox_px=(100, 100, 50, 50) if valid else None,
        centroid_px=(125.0, 125.0) if valid else None,
        tip_px=(125.0, 100.0) if valid else None,
        axis_unit_xy=(0.0, -1.0) if valid else None,
        confidence=0.9 if valid else 0.0,
        valid=valid,
        abstain_reason=None if valid else "no-candidate",
    )


def _direction(error: float | None, confidence: float = 1.0) -> DirectionObservation:
    return DirectionObservation(
        error_deg=error,
        confidence=confidence,
        cue_id="topology_consensus",
        cue_disagreement_deg=2.0,
        valid=error is not None,
        abstain_reason=None if error is not None else "cues disagree",
        sign_margin_deg=30.0,
    )


def _inputs(
    error: float | None = 30.0,
    *,
    sequence: int = 1,
    now_s: float = 0.0,
    age_ms: float = 10.0,
    focus_ok: bool = True,
    viewport_ok: bool = True,
    processed_fps: float = 60.0,
    cursor_safe: bool = True,
    fault: str | None = None,
    arrow_valid: bool = True,
    confidence: float = 1.0,
) -> SteeringInputs:
    return SteeringInputs(
        arrow=_arrow(arrow_valid),
        direction=_direction(error, confidence),
        frame_sequence=sequence,
        frame_age_ms=age_ms,
        now_s=now_s,
        focus_ok=focus_ok,
        viewport_ok=viewport_ok,
        processed_fps=processed_fps,
        cursor_safe=cursor_safe,
        fault=fault,
    )


def _controller(**limits: object) -> ShiftLockController:
    return ShiftLockController(SteeringLimits(**limits), CALIBRATED)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_an_uncalibrated_controller_refuses_to_steer() -> None:
    controller = ShiftLockController(SteeringLimits(), YawCalibration())

    decision = controller.update(_inputs(45.0))

    assert decision.release
    assert decision.yaw_units == 0 and decision.forward == 0
    assert decision.blockers
    assert any("E-YAW" in reason for reason in decision.blockers)


def test_a_partially_measured_calibration_is_not_usable() -> None:
    """An unfinished experiment is a record, not a permission."""
    partial = YawCalibration(
        fingerprint=FINGERPRINT,
        degrees_per_unit=0.25,
        positive_is_right=True,
        status=EvidenceStatus.VALIDATED,
    )
    assert not partial.usable
    assert partial.units_for(10.0) is None
    assert any("smallest effective" in reason for reason in partial.blocking_reasons())


def test_a_validated_status_without_measurements_is_still_refused() -> None:
    assert not YawCalibration(status=EvidenceStatus.VALIDATED).usable


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("focus_ok", False),
        ("viewport_ok", False),
        ("cursor_safe", False),
    ],
)
def test_every_safety_condition_releases_before_anything_else(
    field: str, value: object
) -> None:
    controller = _controller()
    decision = controller.update(_inputs(45.0, **{field: value}))  # type: ignore[arg-type]

    assert decision.release
    assert decision.forward == 0 and decision.yaw_units == 0


def test_stale_evidence_releases() -> None:
    controller = _controller(max_evidence_age_ms=100)
    decision = controller.update(_inputs(45.0, age_ms=180.0))

    assert decision.release
    assert decision.state is ControlState.REACQUIRE


def test_low_processed_throughput_releases() -> None:
    controller = _controller(min_processed_fps=30)
    decision = controller.update(_inputs(45.0, processed_fps=18.0))

    assert decision.release
    assert "processed fps" in decision.reason


def test_a_lost_arrow_releases_immediately_with_no_grace() -> None:
    """The first Live gate gets no arrow-loss grace (mission section 11)."""
    controller = _controller()
    controller.update(_inputs(5.0, sequence=1))

    decision = controller.update(_inputs(5.0, sequence=2, arrow_valid=False))

    assert decision.release
    assert decision.forward == 0


def test_an_abstaining_direction_releases_yaw() -> None:
    controller = _controller()
    decision = controller.update(_inputs(None, sequence=1))

    assert decision.release
    assert decision.yaw_units == 0


# ---------------------------------------------------------------------------
# Evidence binding
# ---------------------------------------------------------------------------


def test_a_frame_authorizes_exactly_one_decision() -> None:
    """Re-reading a frame must never renew a lease."""
    controller = _controller()
    first = controller.update(_inputs(45.0, sequence=9, now_s=0.0))
    second = controller.update(_inputs(45.0, sequence=9, now_s=0.02))

    assert first.yaw_units != 0
    assert second.yaw_units == 0
    assert not second.release, "an old frame is not a fault; the lease just expires"


def test_an_older_frame_cannot_renew_either() -> None:
    controller = _controller()
    controller.update(_inputs(45.0, sequence=20))
    decision = controller.update(_inputs(45.0, sequence=19))

    assert decision.yaw_units == 0


# ---------------------------------------------------------------------------
# Alignment before movement
# ---------------------------------------------------------------------------


def test_w_is_never_taken_outside_the_alignment_threshold() -> None:
    controller = _controller(align_threshold_deg=6.0)
    for index, error in enumerate([90.0, 60.0, 40.0, 25.0, 12.0, 8.0]):
        decision = controller.update(_inputs(error, sequence=index + 1, now_s=index * 0.02))
        assert decision.forward == 0, f"walked at {error} degrees of error"
        assert decision.kind is not CommandKind.FOLLOW


def test_walking_requires_sustained_alignment_not_one_lucky_frame() -> None:
    controller = _controller(align_threshold_deg=6.0, align_confirm_frames=3)
    decisions = [
        controller.update(_inputs(1.0, sequence=index + 1, now_s=index * 0.02))
        for index in range(4)
    ]

    assert [d.forward for d in decisions[:2]] == [0, 0]
    assert decisions[-1].forward == 1
    assert decisions[-1].kind is CommandKind.FOLLOW
    assert decisions[-1].state is ControlState.FOLLOW


def test_leaving_the_threshold_drops_forward_in_the_same_tick() -> None:
    controller = _controller(align_threshold_deg=6.0, align_hysteresis_deg=4.0)
    for index in range(4):
        controller.update(_inputs(1.0, sequence=index + 1, now_s=index * 0.02))
    assert controller.state is ControlState.FOLLOW

    decision = controller.update(_inputs(40.0, sequence=9, now_s=0.10))

    assert decision.forward == 0
    assert decision.state is ControlState.ALIGN


def test_hysteresis_prevents_chatter_at_the_boundary() -> None:
    controller = _controller(align_threshold_deg=6.0, align_hysteresis_deg=4.0)
    for index in range(4):
        controller.update(_inputs(1.0, sequence=index + 1, now_s=index * 0.02))
    assert controller.state is ControlState.FOLLOW

    # Just outside the threshold but inside the hysteresis band: keep walking.
    decision = controller.update(_inputs(8.0, sequence=9, now_s=0.10))
    assert decision.forward == 1


# ---------------------------------------------------------------------------
# The control law
# ---------------------------------------------------------------------------


def test_the_turn_sign_follows_the_error_sign() -> None:
    right = _controller().update(_inputs(40.0))
    left = _controller().update(_inputs(-40.0))

    assert right.yaw_units > 0
    assert left.yaw_units < 0


def test_a_measured_inverted_axis_is_honoured() -> None:
    """If a positive delta turns left on this machine, so must the command."""
    from dataclasses import replace

    inverted = replace(CALIBRATED, positive_is_right=False)
    controller = ShiftLockController(SteeringLimits(), inverted)

    decision = controller.update(_inputs(40.0))

    assert decision.yaw_deg > 0, "the requested rotation is still to the right"
    assert decision.yaw_units < 0, "but the mouse delta that achieves it is negative"


def test_lower_confidence_reduces_magnitude_and_never_raises_it() -> None:
    full = _controller().update(_inputs(40.0, confidence=1.0))
    half = _controller().update(_inputs(40.0, confidence=0.5))

    assert abs(half.yaw_deg) < abs(full.yaw_deg)


@pytest.mark.parametrize("error", [-180.0, -179.0, -90.0, -1.0, 1.0, 90.0, 179.0, 180.0])
def test_the_pulse_is_always_bounded(error: float) -> None:
    limits = SteeringLimits()
    decision = ShiftLockController(limits, CALIBRATED).update(_inputs(error))

    assert abs(decision.yaw_deg) <= limits.max_yaw_per_pulse_deg + 1e-6


def test_the_rate_limit_is_measured_against_real_time_not_frames() -> None:
    """The same route must behave the same at 30 and at 120 fps."""
    slow = _controller()
    fast = _controller()
    slow_total = 0.0
    fast_total = 0.0
    for index in range(1, 13):
        slow_total += abs(
            slow.update(_inputs(90.0, sequence=index, now_s=index / 30.0)).yaw_deg
        )
        fast_total += abs(
            fast.update(_inputs(90.0, sequence=index, now_s=index / 120.0)).yaw_deg
        )

    # Twelve frames at 120 fps span a quarter of the time, so they may ask for
    # roughly a quarter of the rotation.
    assert fast_total < slow_total


def test_a_runaway_episode_gives_up_rather_than_spinning() -> None:
    """An arrow that never converges must cost a bounded rotation, not a spin."""
    controller = _controller(max_episode_yaw_deg=90.0)
    turned = 0.0
    for index in range(1, 200):
        # The simulated camera never moves, so the error never closes: exactly
        # the situation where a controller without a budget spins forever.
        decision = controller.update(_inputs(170.0, sequence=index, now_s=index * 0.02))
        if decision.release and "without converging" in decision.reason:
            assert turned >= 90.0
            # The budget is cleared with the rest of the controller state, so
            # a fresh episode starts from zero rather than instantly giving up.
            assert controller.episode_yaw_deg == 0.0
            return
        turned += abs(decision.yaw_deg)
    raise AssertionError(f"the controller never gave up; turned {turned:.0f} degrees")


# ---------------------------------------------------------------------------
# Closed-loop convergence against a simulated camera
# ---------------------------------------------------------------------------


def _simulate(
    initial_error: float, *, fps: float = 60.0, limits: SteeringLimits | None = None
) -> tuple[list[float], ShiftLockController]:
    """Run the loop against a camera that turns exactly as commanded.

    Deliberately ideal: this measures the *controller*, not the actuator. The
    actuator's real behaviour is what E-YAW and E-STEER-CAL are for.
    """
    controller = ShiftLockController(limits or SteeringLimits(), CALIBRATED)
    error = initial_error
    trace = [error]
    for index in range(1, int(fps * 2) + 1):
        decision = controller.update(_inputs(error, sequence=index, now_s=index / fps))
        if decision.release:
            break
        error = wrap_deg(error - decision.yaw_deg)
        trace.append(error)
    return (trace, controller)


@pytest.mark.parametrize("initial", [5.0, -5.0, 15.0, -15.0, 30.0, -30.0, 45.0, -45.0])
def test_the_loop_converges_from_both_sides(initial: float) -> None:
    trace, controller = _simulate(initial)

    assert abs(trace[-1]) <= 5.0, f"settled at {trace[-1]:.2f} degrees"
    assert controller.state is ControlState.FOLLOW


@pytest.mark.parametrize("initial", [5.0, 15.0, 30.0, 45.0])
def test_the_loop_settles_within_the_time_budget(initial: float) -> None:
    fps = 60.0
    trace, _controller = _simulate(initial, fps=fps)
    settled = next(
        (index for index, value in enumerate(trace) if abs(value) <= 5.0), len(trace)
    )

    assert settled / fps <= 0.5, f"took {settled / fps:.2f} s to settle"


@pytest.mark.parametrize("initial", [5.0, -15.0, 30.0, -45.0])
def test_the_loop_does_not_overshoot_or_oscillate(initial: float) -> None:
    trace, _controller = _simulate(initial)
    sign = math.copysign(1.0, initial)
    overshoot = max((-sign * value for value in trace), default=0.0)
    crossings = sum(
        1
        for a, b in pairwise(trace)
        if a != 0 and b != 0 and math.copysign(1, a) != math.copysign(1, b)
    )

    assert overshoot <= 10.0, f"overshot by {overshoot:.1f} degrees"
    assert crossings <= 2, f"{crossings} zero crossings"


def test_no_forward_command_is_issued_outside_the_threshold_during_convergence() -> None:
    """The acceptance criterion: zero W acquisitions outside alignment."""
    limits = SteeringLimits()
    controller = ShiftLockController(limits, CALIBRATED)
    error = 45.0
    for index in range(1, 120):
        decision = controller.update(_inputs(error, sequence=index, now_s=index / 60.0))
        if decision.release:
            break
        if decision.forward == 1:
            assert abs(error) <= limits.align_threshold_deg + limits.align_hysteresis_deg
        error = wrap_deg(error - decision.yaw_deg)


# ---------------------------------------------------------------------------
# Shift-Lock proof
# ---------------------------------------------------------------------------


def _proof(**overrides: object) -> ShiftLockProof:
    defaults: dict[str, object] = {
        "method": "stationary-micro-yaw",
        "run_id": "run-1",
        "arm_token_id": "token-1",
        "generation": 4,
        "window_identity": ("roblox", 42),
        "fingerprint": FINGERPRINT,
        "observed_at_s": 100.0,
        "confidence": 0.95,
        "evidence": ("observed 3.1 deg of yaw with no translation",),
        "status": EvidenceStatus.VALIDATED,
    }
    defaults.update(overrides)
    return ShiftLockProof(**defaults)  # type: ignore[arg-type]


def _check(proof: ShiftLockProof, **overrides: object) -> tuple[bool, str]:
    defaults: dict[str, object] = {
        "run_id": "run-1",
        "arm_token_id": "token-1",
        "generation": 4,
        "window_identity": ("roblox", 42),
        "fingerprint": FINGERPRINT,
        "now_s": 105.0,
    }
    defaults.update(overrides)
    return proof.valid_for(**defaults)  # type: ignore[arg-type]


def test_a_verified_proof_covers_the_situation_it_was_taken_in() -> None:
    ok, reason = _check(_proof())
    assert ok and reason == "verified"


def test_an_unverified_proof_is_never_accepted() -> None:
    ok, reason = _check(_proof(status=EvidenceStatus.PENDING))
    assert not ok and "pending" in reason


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("run_id", "run-2", "previous run"),
        ("arm_token_id", "token-2", "previous arm"),
        ("generation", 5, "previous generation"),
        ("window_identity", ("roblox", 99), "window changed"),
    ],
)
def test_a_proof_does_not_survive_a_change_in_what_it_proved(
    key: str, value: object, expected: str
) -> None:
    ok, reason = _check(_proof(), **{key: value})
    assert not ok
    assert expected in reason


def test_a_proof_does_not_survive_a_sensitivity_change() -> None:
    from dataclasses import replace

    changed = replace(FINGERPRINT, camera_sensitivity="raised")
    ok, reason = _check(_proof(), fingerprint=changed)

    assert not ok
    assert "camera_sensitivity" in reason


def test_a_proof_expires_because_shift_lock_can_be_toggled_at_any_time() -> None:
    ok, reason = _check(_proof(), now_s=100.0 + ShiftLockProof.MAX_AGE_S + 1.0)
    assert not ok
    assert "re-observed" in reason


def test_the_calibration_fingerprint_names_what_differs() -> None:
    from dataclasses import replace

    other = replace(FINGERPRINT, os_name="windows", profile_revision=9)
    differences = FINGERPRINT.mismatches(other)

    assert any("os_name" in text for text in differences)
    assert any("profile_revision" in text for text in differences)
    assert FINGERPRINT.matches(FINGERPRINT)


# ---------------------------------------------------------------------------
# Unit conversion
# ---------------------------------------------------------------------------


def test_a_request_below_the_minimum_effective_movement_is_raised_to_it() -> None:
    """A command that cannot move anything is worse than the smallest that can."""
    units = CALIBRATED.units_for(0.1)
    assert units is not None
    assert abs(units) == CALIBRATED.min_effective_units


def test_a_request_above_saturation_is_capped() -> None:
    units = CALIBRATED.units_for(10_000.0)
    assert units == CALIBRATED.saturation_units


def test_zero_degrees_asks_for_nothing() -> None:
    assert CALIBRATED.units_for(0.0) == 0
