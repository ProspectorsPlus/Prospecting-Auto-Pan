"""The arrow follower: refusals, the control law, and the control-mode proof.

The controller is exercised against a **simulated camera** rather than a real
one, so the closed-loop behaviour the mission specifies - converge from both
sides, settle without overshooting, never take W outside the alignment cone -
can be measured deterministically. That is a design check, not a field result:
what a real camera does is what the turn characterizer measures at runtime, and
what a real route does is a native test.

The most important tests here are the refusals. A controller that steers
without a measured turn response, or that keeps pushing after the error starts
growing, is the failure this module exists to prevent.
"""

from __future__ import annotations

import math
from dataclasses import replace
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
    ArrowFollowerController,
    ControlFingerprint,
    ControlModeMethod,
    ShiftLockProof,
    SteeringInputs,
    SteeringLimits,
    wrap_deg,
)
from prospector_engine.turning import TurnBackend, TurnLimits, TurnResponse

FINGERPRINT = ControlFingerprint(
    os_name="test",
    backend="mouse_yaw",
    client_fingerprint="client-1",
    camera_sensitivity="default",
    control_mode="shift-lock",
    viewport_identity=("canonical", 1280, 720),
    profile_id="green_arrow_v1",
    profile_revision=1,
    supported_min_fps=30,
)

#: A response standing in for one the characterizer would measure. Only a test
#: may construct this directly; production reaches it through bounded
#: stationary probes whose observed rotation perception confirmed.
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


def _arrow(valid: bool = True, track_id: int = 1) -> ArrowObservation:
    return ArrowObservation(
        profile_id="green_arrow_v1",
        track_id=track_id if valid else None,
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
    track_id: int = 1,
    geometry_revision: int = 3,
    profile_revision: int = 1,
) -> SteeringInputs:
    return SteeringInputs(
        arrow=_arrow(arrow_valid, track_id),
        direction=_direction(error, confidence),
        frame_sequence=sequence,
        frame_age_ms=age_ms,
        now_s=now_s,
        focus_ok=focus_ok,
        viewport_ok=viewport_ok,
        processed_fps=processed_fps,
        cursor_safe=cursor_safe,
        geometry_revision=geometry_revision,
        profile_revision=profile_revision,
        fault=fault,
    )


def _controller(
    response: TurnResponse | None = MEASURED, **limits: object
) -> ArrowFollowerController:
    return ArrowFollowerController(SteeringLimits(**limits), response)  # type: ignore[arg-type]


def _drive(
    controller: ArrowFollowerController, error: float, frames: int, *, fps: float = 60.0
) -> list[object]:
    """Feed a constant error for ``frames`` frames and return every decision."""
    return [
        controller.update(_inputs(error, sequence=index, now_s=index / fps))
        for index in range(1, frames + 1)
    ]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_an_uncharacterized_controller_refuses_to_steer() -> None:
    decision = ArrowFollowerController(SteeringLimits(), None).update(_inputs(45.0))

    assert decision.release
    assert not decision.plan.moves and decision.forward == 0
    assert decision.blockers
    assert any("characterized" in reason for reason in decision.blockers)


def test_a_pending_response_is_not_a_permission() -> None:
    """An unfinished measurement is a record, not a licence to steer."""
    pending = replace(MEASURED, status=EvidenceStatus.PENDING)

    decision = ArrowFollowerController(SteeringLimits(), pending).update(_inputs(45.0))

    assert decision.release
    assert any("pending" in reason for reason in decision.blockers)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"fault": "watchdog"},
        {"viewport_ok": False},
        {"focus_ok": False},
        {"cursor_safe": False},
    ],
)
def test_every_safety_condition_releases_before_anything_else(
    kwargs: dict[str, object],
) -> None:
    decision = _controller().update(_inputs(1.0, **kwargs))  # type: ignore[arg-type]
    assert decision.release
    assert decision.forward == 0 and not decision.plan.moves


def test_stale_evidence_releases() -> None:
    decision = _controller().update(_inputs(1.0, age_ms=500.0))
    assert decision.release and "ms old" in decision.reason


def test_low_processed_throughput_releases() -> None:
    decision = _controller().update(_inputs(1.0, processed_fps=5.0))
    assert decision.release and "processed fps" in decision.reason


def test_a_changed_viewport_releases_rather_than_reinterpreting_angles() -> None:
    controller = _controller()
    controller.update(_inputs(10.0, sequence=1, geometry_revision=3))

    decision = controller.update(_inputs(10.0, sequence=2, geometry_revision=4))

    assert decision.release and "viewport changed" in decision.reason


def test_a_changed_profile_releases() -> None:
    controller = _controller()
    controller.update(_inputs(10.0, sequence=1, profile_revision=1))

    decision = controller.update(_inputs(10.0, sequence=2, profile_revision=2))

    assert decision.release and "profile changed" in decision.reason


def test_a_lost_arrow_releases_turning_immediately() -> None:
    controller = _controller()
    controller.update(_inputs(40.0, sequence=1))

    decision = controller.update(_inputs(None, sequence=2, arrow_valid=False))

    assert decision.release
    assert not decision.plan.moves


def test_a_lost_arrow_while_walking_holds_course_briefly_but_never_turns() -> None:
    """Turning blind is never justified; walking blind for a frame or two is."""
    controller = _controller(align_confirm_frames=2, arrow_loss_grace_frames=2)
    _drive(controller, 0.5, 6)
    assert controller.state is ControlState.FOLLOW

    first = controller.update(_inputs(None, sequence=20, now_s=1.0, arrow_valid=False))
    second = controller.update(_inputs(None, sequence=21, now_s=1.02, arrow_valid=False))
    third = controller.update(_inputs(None, sequence=22, now_s=1.04, arrow_valid=False))

    assert first.forward == 1 and not first.plan.moves
    assert second.forward == 1 and not second.plan.moves
    assert third.release and third.forward == 0


def test_an_abstaining_direction_releases() -> None:
    decision = _controller().update(_inputs(None))
    assert decision.release
    assert decision.state is ControlState.ALIGN


def test_a_frame_authorizes_exactly_one_decision() -> None:
    controller = _controller()
    first = controller.update(_inputs(40.0, sequence=7, now_s=0.0))
    repeat = controller.update(_inputs(40.0, sequence=7, now_s=0.05))

    assert first.plan.moves
    assert not repeat.plan.moves and not repeat.release
    assert "no newer frame" in repeat.reason


def test_an_older_frame_cannot_renew_either() -> None:
    controller = _controller()
    controller.update(_inputs(40.0, sequence=7, now_s=0.0))
    older = controller.update(_inputs(40.0, sequence=3, now_s=0.05))

    assert not older.plan.moves and not older.release


# ---------------------------------------------------------------------------
# Alignment before movement
# ---------------------------------------------------------------------------


def test_w_is_never_taken_outside_the_alignment_cone() -> None:
    controller = _controller(align_threshold_deg=8.0)
    for index, error in enumerate([90.0, 60.0, 40.0, 25.0, 12.0, 10.0]):
        decision = controller.update(_inputs(error, sequence=index + 1, now_s=index * 0.05))
        assert decision.forward == 0, f"walked at {error} degrees of error"
        assert decision.kind is not CommandKind.FOLLOW


def test_walking_requires_sustained_alignment_not_one_lucky_frame() -> None:
    controller = _controller(align_threshold_deg=8.0, align_confirm_frames=3)
    decisions = _drive(controller, 1.0, 4)

    assert [d.forward for d in decisions[:2]] == [0, 0]  # type: ignore[attr-defined]
    assert decisions[-1].forward == 1  # type: ignore[attr-defined]
    assert decisions[-1].kind is CommandKind.FOLLOW  # type: ignore[attr-defined]
    assert decisions[-1].state is ControlState.FOLLOW  # type: ignore[attr-defined]


def test_leaving_the_cone_drops_forward_in_the_same_tick() -> None:
    controller = _controller(align_threshold_deg=8.0, align_hysteresis_deg=5.0)
    _drive(controller, 1.0, 4)
    assert controller.state is ControlState.FOLLOW

    decision = controller.update(_inputs(40.0, sequence=9, now_s=0.30))

    assert decision.forward == 0
    assert decision.state is ControlState.ALIGN


def test_hysteresis_prevents_chatter_at_the_boundary() -> None:
    controller = _controller(align_threshold_deg=8.0, align_hysteresis_deg=5.0)
    _drive(controller, 1.0, 4)
    assert controller.state is ControlState.FOLLOW

    # Just outside the cone but inside the hysteresis band: keep walking.
    decision = controller.update(_inputs(11.0, sequence=9, now_s=0.30))
    assert decision.forward == 1


# ---------------------------------------------------------------------------
# The control law
# ---------------------------------------------------------------------------


def test_the_turn_sign_follows_the_error_sign() -> None:
    right = _controller().update(_inputs(40.0))
    left = _controller().update(_inputs(-40.0))

    assert right.plan.units > 0
    assert left.plan.units < 0


def test_a_measured_inverted_axis_is_honoured() -> None:
    """If a positive delta turns left on this machine, so must the command."""
    inverted = replace(MEASURED, positive_is_right=False)

    decision = ArrowFollowerController(SteeringLimits(), inverted).update(_inputs(40.0))

    assert decision.plan.expected_deg > 0, "the requested rotation is still to the right"
    assert decision.plan.units < 0, "but the delta that achieves it is negative"


def test_lower_confidence_reduces_magnitude_and_never_raises_it() -> None:
    full = _controller().update(_inputs(40.0, confidence=1.0))
    half = _controller().update(_inputs(40.0, confidence=0.35))

    assert abs(half.plan.expected_deg) < abs(full.plan.expected_deg)


@pytest.mark.parametrize("error", [-180.0, -179.0, -90.0, -1.0, 1.0, 90.0, 179.0, 180.0])
def test_the_correction_is_always_bounded(error: float) -> None:
    decision = _controller().update(_inputs(error))
    assert abs(decision.plan.expected_deg) <= TurnLimits().max_correction_deg + 1e-6


def test_a_correction_never_exceeds_the_error_that_remains() -> None:
    """Asking for more rotation than is left is an overshoot by construction."""
    decision = _controller().update(_inputs(3.5))
    assert abs(decision.plan.expected_deg) <= 3.5 + MEASURED.degrees_per_unit


def test_only_one_pulse_is_in_flight_at_a_time() -> None:
    """A second correction issued inside the first one's latency is blind."""
    controller = _controller()
    first = controller.update(_inputs(40.0, sequence=1, now_s=0.0))
    during = controller.update(_inputs(40.0, sequence=2, now_s=0.005))
    after = controller.update(_inputs(26.0, sequence=3, now_s=0.05))

    assert first.plan.moves
    assert not during.plan.moves and "observing" in during.reason
    assert after.plan.moves


def test_a_correction_that_makes_things_worse_gives_up_rather_than_pushing() -> None:
    controller = _controller(max_growing_pulses=2)
    error = 40.0
    for index in range(1, 60):
        decision = controller.update(_inputs(error, sequence=index, now_s=index * 0.05))
        if decision.release and "grew" in decision.reason:
            return
        if decision.plan.moves:
            error = wrap_deg(error + abs(decision.plan.expected_deg))  # the wrong way
    raise AssertionError("the controller never noticed the error growing")


def test_a_runaway_episode_gives_up_rather_than_spinning() -> None:
    """An arrow that never converges must cost a bounded rotation, not a spin."""
    controller = _controller(max_episode_yaw_deg=90.0)
    turned = 0.0
    for index in range(1, 400):
        # The simulated camera never moves, so the error never closes: exactly
        # the situation where a controller without a budget spins forever.
        decision = controller.update(_inputs(170.0, sequence=index, now_s=index * 0.05))
        if decision.release and (
            "without converging" in decision.reason or "grew" in decision.reason
        ):
            # The budget is cleared with the rest of the controller state, so
            # a fresh episode starts from zero rather than instantly giving up.
            assert controller.episode_yaw_deg == 0.0
            return
        turned += abs(decision.plan.expected_deg)
    raise AssertionError(f"the controller never gave up; turned {turned:.0f} degrees")


def test_a_new_arrow_identity_drops_the_pulse_in_flight() -> None:
    """A different arrow is a different target, not a continuation of this one."""
    controller = _controller()
    controller.update(_inputs(40.0, sequence=1, now_s=0.0, track_id=1))

    decision = controller.update(_inputs(40.0, sequence=2, now_s=0.005, track_id=2))

    assert "observing" not in decision.reason


# ---------------------------------------------------------------------------
# Closed-loop convergence against a simulated camera
# ---------------------------------------------------------------------------


def _simulate(
    initial_error: float, *, fps: float = 60.0, limits: SteeringLimits | None = None
) -> tuple[list[float], ArrowFollowerController]:
    """Run the loop against a camera that turns exactly as commanded.

    Deliberately ideal: this measures the *controller*, not the actuator. What
    the actuator really does is what the turn characterizer measures at
    runtime, on the machine that is about to be steered.
    """
    controller = ArrowFollowerController(limits or SteeringLimits(), MEASURED)
    error = initial_error
    trace = [error]
    for index in range(1, int(fps * 3) + 1):
        decision = controller.update(_inputs(error, sequence=index, now_s=index / fps))
        if decision.release:
            break
        error = wrap_deg(error - decision.plan.expected_deg)
        trace.append(error)
    return (trace, controller)


@pytest.mark.parametrize("initial", [5.0, -5.0, 15.0, -15.0, 30.0, -30.0, 45.0, -45.0])
def test_the_loop_converges_from_both_sides(initial: float) -> None:
    trace, controller = _simulate(initial)

    assert abs(trace[-1]) <= 8.0, f"settled at {trace[-1]:.2f} degrees"
    assert controller.state is ControlState.FOLLOW


@pytest.mark.parametrize("initial", [5.0, 15.0, 30.0, 45.0])
def test_the_loop_settles_within_the_time_budget(initial: float) -> None:
    fps = 60.0
    trace, _controller = _simulate(initial, fps=fps)
    settled = next(
        (index for index, value in enumerate(trace) if abs(value) <= 8.0), len(trace)
    )

    assert settled / fps <= 1.0, f"took {settled / fps:.2f} s to settle"


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

    assert overshoot <= 6.0, f"overshot by {overshoot:.1f} degrees"
    assert crossings <= 2, f"{crossings} zero crossings"


@pytest.mark.parametrize("fps", [30.0, 60.0, 120.0])
def test_the_route_is_the_same_at_every_cadence(fps: float) -> None:
    """One pulse in flight makes the loop cadence independent by construction."""
    trace, controller = _simulate(45.0, fps=fps)
    assert abs(trace[-1]) <= 8.0
    assert controller.state is ControlState.FOLLOW


def test_no_forward_command_is_issued_outside_the_cone_during_convergence() -> None:
    """The acceptance criterion: zero W acquisitions outside alignment."""
    limits = SteeringLimits()
    controller = ArrowFollowerController(limits, MEASURED)
    error = 45.0
    for index in range(1, 200):
        decision = controller.update(_inputs(error, sequence=index, now_s=index / 60.0))
        if decision.release:
            break
        if decision.forward == 1:
            assert abs(error) <= limits.align_threshold_deg + limits.align_hysteresis_deg
        error = wrap_deg(error - decision.plan.expected_deg)


def test_arrow_key_plans_use_the_turn_axis_and_hold_it_out() -> None:
    """A held-key correction is renewed until its duration elapses, not re-pressed."""
    keys = replace(
        MEASURED,
        backend=TurnBackend.ARROW_KEYS,
        degrees_per_unit=0.08,
        min_effective_units=20,
        max_units=220,
    )
    controller = ArrowFollowerController(SteeringLimits(), keys)

    first = controller.update(_inputs(40.0, sequence=1, now_s=0.0))
    assert first.plan.turn_axis == 1 and first.plan.hold_ms > 0

    during = controller.update(_inputs(40.0, sequence=2, now_s=0.01))
    assert during.plan.turn_axis == 1, "the key stays down for the whole pulse"
    assert "in flight" in during.reason


# ---------------------------------------------------------------------------
# Control-mode proof
# ---------------------------------------------------------------------------


def _proof(**overrides: object) -> ShiftLockProof:
    defaults: dict[str, object] = {
        "method": ControlModeMethod.MICRO_YAW,
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


def test_a_proof_cannot_be_constructed_by_asserting_one() -> None:
    """There are two methods, and both are observations."""
    assert set(ControlModeMethod) == {
        ControlModeMethod.VISUAL_CUE,
        ControlModeMethod.MICRO_YAW,
    }


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
    changed = replace(FINGERPRINT, camera_sensitivity="raised")
    ok, reason = _check(_proof(), fingerprint=changed)

    assert not ok
    assert "camera_sensitivity" in reason


def test_a_proof_expires_because_shift_lock_can_be_toggled_at_any_time() -> None:
    ok, reason = _check(_proof(), now_s=100.0 + ShiftLockProof.MAX_AGE_S + 1.0)
    assert not ok
    assert "re-observed" in reason


def test_the_control_fingerprint_names_what_differs() -> None:
    other = replace(FINGERPRINT, os_name="windows", profile_revision=9)
    differences = FINGERPRINT.mismatches(other)

    assert any("os_name" in text for text in differences)
    assert any("profile_revision" in text for text in differences)
    assert FINGERPRINT.matches(FINGERPRINT)


# ---------------------------------------------------------------------------
# The deadband floor
# ---------------------------------------------------------------------------


def test_the_deadband_never_goes_below_the_measured_actuator_resolution() -> None:
    """Asking for less than the actuator can do yields its minimum, which dithers."""
    coarse = replace(MEASURED, degrees_per_unit=4.0, min_effective_units=2)
    controller = ArrowFollowerController(SteeringLimits(yaw_deadband_deg=1.0), coarse)

    decision = controller.update(_inputs(5.0))

    assert not decision.plan.moves, "a 5 degree request must not fire a 8 degree actuator"


def test_a_collapsed_direction_confidence_releases_rather_than_creeping() -> None:
    """Scaling is right for an uncertain estimate, not for a collapsed one."""
    controller = _controller()
    controller.update(_inputs(30.0, sequence=1, confidence=0.9))

    decision = controller.update(_inputs(30.0, sequence=2, now_s=0.1, confidence=0.05))

    assert decision.release
    assert decision.state is ControlState.REACQUIRE
    assert "confidence collapsed" in decision.reason


def test_a_merely_uncertain_direction_still_steers_but_smaller() -> None:
    confident = _controller().update(_inputs(30.0, confidence=0.95))
    unsure = _controller().update(_inputs(30.0, confidence=0.45))

    assert unsure.plan.moves and not unsure.release
    assert abs(unsure.plan.expected_deg) < abs(confident.plan.expected_deg)
