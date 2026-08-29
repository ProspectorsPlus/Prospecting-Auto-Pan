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
    RECOVERY_LADDER,
    NavigationCapabilities,
    NavigationInputs,
    Navigator,
    RecoveryBudget,
    RecoveryLadder,
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


def test_maneuvering_is_permitted_by_capability_and_evidenced_by_the_guard() -> None:
    """Two questions that used to be one, and were answered in the wrong place.

    The capability used to demand a matured locomotion baseline before recovery
    was allowed at all. That baseline only arrives after a dozen clean frames of
    unobstructed walking, so a route that met a bush in its first few seconds
    had recovery switched off and just stopped. Permission lives here; whether
    there is enough evidence *right now* is the progress guard's question, and
    it abstains honestly when there is not.
    """
    no_baseline = replace(
        READY,
        motion_baseline=LocomotionBaseline(
            condition_id="uncalibrated",
            min_forward_speed_norm=None,
            status=EvidenceStatus.PENDING,
            provenance=ContactConfig().provenance,
        ),
    )
    assert no_baseline.steering_enabled
    assert no_baseline.recovery_enabled, "an early bush had no recovery at all"
    assert not no_baseline.progress_enabled, "a frozen baseline must not be claimed"


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


def test_the_ladder_opens_with_a_running_jump_rather_than_letting_go() -> None:
    """The maneuver a player would actually make.

    The ladder this replaces opened with two rungs that released the controls
    and waited - 700 ms of standing still before anything was attempted - and
    then offered a stationary strafe. Nobody gets past a bush that way.
    """
    ladder = RecoveryLadder()
    ladder.begin(0.0, side=1, error_deg=20.0)
    steps = _run_ladder(ladder)

    assert steps[0].rung.name == "R0"
    assert steps[0].forward == 1, "the first thing it did was stop walking"
    assert any(step.jump and step.forward == 1 for step in steps[:5]), (
        "the running jump never had W and SPACE down together"
    )


def test_every_rung_that_moves_keeps_walking_or_deliberately_backs_out() -> None:
    ladder = RecoveryLadder()
    ladder.begin(0.0, side=1, error_deg=20.0)

    for step in _run_ladder(ladder):
        if step.forward < 0:
            assert step.rung.name == "R3", "only the back-out rung may reverse"
        else:
            assert step.forward == 1, f"{step.rung.name} stood still"


def test_a_forward_arc_holds_a_strafe_and_a_walk_at_once() -> None:
    ladder = RecoveryLadder()
    ladder.begin(0.0, side=-1, error_deg=20.0)

    arcs = [s for s in _run_ladder(ladder) if s.forward == 1 and s.strafe != 0]

    assert arcs, "no rung ever arced around anything"
    assert any(s.turn != 0 for s in arcs), "no arc used the camera as well"
    assert all(s.strafe in (-1, 1) for s in arcs)


def test_the_chosen_side_is_sticky_for_the_episode() -> None:
    """Choosing left then right on alternating frames is a wiggle, not a detour."""
    ladder = RecoveryLadder()
    ladder.begin(0.0, side=1, error_deg=20.0)

    assert {step.side for step in _run_ladder(ladder)} == {1}


def test_the_opposite_side_is_reached_by_a_rung_not_by_changing_the_side() -> None:
    """R2 is *about* trying the other way; the episode's own side is untouched."""
    ladder = RecoveryLadder()
    ladder.begin(0.0, side=1, error_deg=20.0)
    strafes = {step.strafe for step in _run_ladder(ladder) if step.strafe}

    assert strafes == {-1, 1}, "the ladder only ever tried one direction"
    assert ladder.side == 1


def test_a_side_flip_is_allowed_once_and_only_once() -> None:
    ladder = RecoveryLadder(RecoveryBudget(side_flips_allowed=1))
    ladder.begin(0.0, side=1, error_deg=0.0)

    assert ladder.flip_side("the sector memory says otherwise")
    assert ladder.side == -1
    assert not ladder.flip_side("again"), "a second flip is a wiggle"


def test_jumps_are_capped_and_never_fire_on_consecutive_frames() -> None:
    """A SPACE press every frame is a held space bar with extra steps, and the
    character never leaves the ground."""
    ladder = RecoveryLadder(RecoveryBudget(max_jumps=2, jump_cooldown_ms=700))
    ladder.begin(0.0, side=1, error_deg=0.0)

    jumps = [step for step in _run_ladder(ladder, ticks=2000, dt=0.01) if step.jump]

    assert len(jumps) <= 2
    assert ladder.jumps <= 2


def test_a_recovery_episode_has_a_total_time_and_input_cap() -> None:
    ladder = RecoveryLadder(RecoveryBudget(total_time_ms=1000, total_input_ms=500))
    ladder.begin(0.0, side=1, error_deg=0.0)

    assert ladder.over_budget(0.5) is None
    assert "time" in (ladder.over_budget(2.0) or "")

    fresh = RecoveryLadder(RecoveryBudget(total_time_ms=100000, total_input_ms=100))
    fresh.begin(0.0, side=1, error_deg=0.0)
    _run_ladder(fresh, ticks=200)
    assert fresh.exhausted
    assert "input budget" in fresh.outcome


def test_reversing_is_capped_separately_from_everything_else() -> None:
    ladder = RecoveryLadder(RecoveryBudget(max_reverse_ms=50))
    ladder.begin(0.0, side=1, error_deg=0.0)
    _run_ladder(ladder, ticks=2000, dt=0.01)

    assert ladder.exhausted
    assert "reversed" in ladder.outcome or ladder.outcome


def test_success_needs_several_fresh_frames_not_one_lucky_one() -> None:
    """One frame of movement during a maneuver is what a maneuver produces -
    the character sliding along the obstacle - and calling that success is how
    a ladder resolves straight back into the same wall."""
    ladder = RecoveryLadder(RecoveryBudget(restore_frames=3))
    ladder.begin(0.0, side=1, error_deg=10.0)

    assert not ladder.note_progress(progressing=True, error_deg=8.0)
    assert not ladder.note_progress(progressing=True, error_deg=8.0)
    assert ladder.note_progress(progressing=True, error_deg=8.0)


def test_a_single_stalled_frame_restarts_the_count() -> None:
    ladder = RecoveryLadder(RecoveryBudget(restore_frames=3))
    ladder.begin(0.0, side=1, error_deg=10.0)
    ladder.note_progress(progressing=True, error_deg=8.0)
    ladder.note_progress(progressing=False, error_deg=8.0)

    assert not ladder.note_progress(progressing=True, error_deg=8.0)


def test_movement_that_threw_the_heading_away_is_not_success() -> None:
    ladder = RecoveryLadder(RecoveryBudget(restore_frames=1))
    ladder.begin(0.0, side=1, error_deg=10.0)

    assert not ladder.note_progress(progressing=True, error_deg=170.0)
    assert ladder.note_progress(progressing=True, error_deg=20.0)


def test_the_ladder_is_finite_and_every_rung_is_bounded() -> None:
    assert RECOVERY_LADDER, "there is no ladder"
    for rung in RECOVERY_LADDER:
        assert rung.moves, f"{rung.name} has no maneuver"
        assert rung.duration_ms > 0
        assert all(move.duration_ms > 0 for move in rung.moves)
        assert rung.max_attempts >= 1


@settings(max_examples=50, deadline=None)
@given(ticks=st.integers(min_value=0, max_value=500), dt=st.floats(0.001, 0.2))
def test_recovery_always_reaches_a_terminal_state(ticks: int, dt: float) -> None:
    ladder = RecoveryLadder()
    ladder.begin(0.0, side=1, error_deg=0.0)
    _run_ladder(ladder, ticks=ticks, dt=dt)
    assert ladder.exhausted or ladder.rung is not None


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


def test_an_abstained_arrow_from_a_standstill_looks_rather_than_releasing() -> None:
    """It has never had a heading, so there is nothing to coast on - but there
    is also nothing to release, and the wait is bounded."""
    navigator = _navigator()

    decision = navigator.decide(_inputs(arrow_valid=False), generation=1, now_s=0.0)

    assert decision.phase is NavigationPhase.ACQUIRE
    assert decision.movement.idle
    assert not decision.release


def test_an_arrow_that_never_appears_ends_the_route_rather_than_waiting() -> None:
    navigator = _navigator()

    final = None
    for index in range(1, 1500):
        now = index * 0.016
        final = navigator.decide(
            _inputs(arrow_valid=False, sequence=index, captured_at_s=now),
            generation=1,
            now_s=now,
        )
        if final.phase is NavigationPhase.ABANDONED:
            break

    assert final is not None
    assert final.phase is NavigationPhase.ABANDONED, "it waited forever with nothing on screen"
    assert final.release and final.movement.idle


def test_a_command_lease_never_outlives_its_evidence() -> None:
    navigator = _navigator(max_evidence_age_ms=100)
    decision = navigator.decide(_inputs(error_deg=30.0), generation=1, now_s=0.0)

    assert decision.command is not None
    assert decision.command.valid_until_s <= decision.command.source_captured_at_s + 0.100001


def test_a_confirmed_arrival_stops_the_route_at_once() -> None:
    """The banner is the game telling us we are standing on the spot, and
    ``ArrivalDetector`` has already applied its own N-of-M before it ever
    reports ``valid``. Counting it three more times here was the same evidence
    divided by three, and every frame of it was spent walking off the spot.
    """
    from prospector_engine.contracts import ArrivalObservation

    navigator = _navigator()
    arrival = ArrivalObservation(1.0, 4, 6, "m1", True, ("latched",))

    decision = navigator.decide(_inputs(arrival=arrival), generation=1, now_s=0.0)

    assert decision.phase is NavigationPhase.ARRIVED
    assert decision.release and decision.command is None
    assert decision.movement.idle


def test_an_arrival_that_is_not_valid_does_not_end_the_route() -> None:
    from prospector_engine.contracts import ArrivalObservation

    navigator = _navigator()
    candidate = ArrivalObservation(0.4, 1, 6, None, False, ("hits=1/6",))

    decision = navigator.decide(_inputs(arrival=candidate), generation=1, now_s=0.0)

    assert decision.phase is not NavigationPhase.ARRIVED
    assert navigator.arrival_latches == 0


def test_an_arrow_in_view_is_walked_towards_at_once() -> None:
    """The controller this replaces wanted three consecutive frames inside an
    eight-degree cone before it would press W. That wait is where the stutter
    came from, and it bought nothing: a wrong heading is corrected while
    walking."""
    navigator = _navigator()

    decision = navigator.decide(_inputs(error_deg=0.5), generation=1, now_s=0.0)

    assert decision.command is not None
    assert decision.command.forward_axis == 1
    assert decision.command.kind is CommandKind.FOLLOW
    assert decision.phase is NavigationPhase.FOLLOW


def test_a_moderate_error_is_corrected_while_still_walking() -> None:
    navigator = _navigator()

    decision = navigator.decide(_inputs(error_deg=45.0), generation=1, now_s=0.0)

    assert decision.command is not None
    assert decision.command.forward_axis == 1, "it stopped walking to turn"
    assert decision.command.kind is CommandKind.FOLLOW
    assert decision.command.yaw_delta_px > 0, "it walked without correcting"
    assert decision.phase is NavigationPhase.CORRECT


def test_a_target_flatly_behind_pivots_at_once() -> None:
    """Past ``pivot_immediate_deg`` there is no reading of the frame in which
    walking forward is right, and the fifth of a second the confirmation costs
    is a fifth of a second spent walking further away."""
    navigator = _navigator()

    phases = []
    for index in range(1, 30):
        now = index * 0.02
        decision = navigator.decide(
            _inputs(error_deg=165.0, sequence=index, captured_at_s=now),
            generation=1,
            now_s=now,
        )
        phases.append(decision.phase)
        if decision.command is not None and decision.command.forward_axis == 0:
            assert decision.command.kind is CommandKind.ALIGN

    assert NavigationPhase.ALIGN in phases, "a target behind us never earned a pivot"
    assert phases[0] is NavigationPhase.ALIGN, (
        "165 degrees is flatly behind; it should not walk another step first"
    )


def test_a_merely_severe_error_is_still_confirmed_before_stopping() -> None:
    """One bad frame must never cost a stop. Between ``strong_band_deg`` and
    ``pivot_immediate_deg`` the confirmation still runs."""
    navigator = _navigator()

    first = navigator.decide(_inputs(error_deg=100.0), generation=1, now_s=0.0)

    assert first.phase is not NavigationPhase.ALIGN
    assert first.command is not None and first.command.forward_axis == 1


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


def _stalled_route(navigator: Navigator, *, ticks: int = 400, dt: float = 0.05) -> list[Any]:
    """Walk into something that never gives, and return every decision."""
    decisions: list[Any] = []
    navigator.note_applied(_applied("w"), now_s=0.0)
    for index in range(1, ticks + 1):
        now = index * dt
        decision = navigator.decide(
            _inputs(error_deg=0.5, motion=_motion(0.0), sequence=index, captured_at_s=now),
            generation=1,
            now_s=now,
        )
        decisions.append(decision)
        # The feedback the live worker supplies, from the actuator's ledger.
        navigator.note_held(sorted(key.value for key in decision.movement.keys), now_s=now)
        if decision.phase in (NavigationPhase.ABANDONED, NavigationPhase.FAILED):
            break
    return decisions


def test_confirmed_contact_starts_a_maneuver_rather_than_a_stop() -> None:
    """The whole ladder used to open with 700 ms of standing still. Meeting a
    bush now costs a running jump, and W never goes up to pay for it."""
    guard = ProgressGuard(
        WALKING, ProgressConfig(suspect_after_ms=100, min_applied_forward_ms=50)
    )
    navigator = _navigator(progress=guard)

    decisions = _stalled_route(navigator, ticks=40)
    recovering = [d for d in decisions if d.phase is NavigationPhase.RECOVERY]

    assert recovering, "walking into a wall never started a recovery"
    first = recovering[0]
    assert not first.release, "entering recovery went through the release floor"
    assert first.command is not None and first.command.forward_axis == 1
    assert first.recovery is not None and first.recovery.rung.name == "R0"


def test_the_running_jump_holds_forward_and_space_at_the_same_time() -> None:
    guard = ProgressGuard(
        WALKING, ProgressConfig(suspect_after_ms=100, min_applied_forward_ms=50)
    )
    navigator = _navigator(progress=guard)

    decisions = _stalled_route(navigator, ticks=60)
    jumps = [
        d
        for d in decisions
        if d.command is not None and d.command.jump and d.phase is NavigationPhase.RECOVERY
    ]

    assert jumps, "no rung ever jumped"
    assert all(d.command.forward_axis == 1 for d in jumps), "it jumped from a standstill"


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


def test_recovery_does_not_wiggle_between_sides() -> None:
    """The ladder changes side twice by design - once for R2's opposite hop and
    once back - and never more. Alternating every frame is a wiggle."""
    guard = ProgressGuard(
        WALKING, ProgressConfig(suspect_after_ms=100, min_applied_forward_ms=50)
    )
    navigator = _navigator(progress=guard)

    lateral = [
        d.command.lateral_axis
        for d in _stalled_route(navigator)
        if d.command is not None and d.command.lateral_axis
    ]

    flips = sum(1 for a, b in pairwise(lateral) if a != b)
    assert lateral, "recovery never stepped to either side"
    assert flips <= 3, f"the detour side changed {flips} times"


def test_a_permanent_wall_abandons_inside_its_budget_and_holds_nothing() -> None:
    guard = ProgressGuard(
        WALKING, ProgressConfig(suspect_after_ms=100, min_applied_forward_ms=50)
    )
    navigator = _navigator(progress=guard)

    decisions = _stalled_route(navigator)
    final = decisions[-1]

    assert final.phase is NavigationPhase.ABANDONED, "recovery looped instead of abandoning"
    assert final.release and final.command is None
    assert final.movement.idle, "a terminal decision still asked for a key"
    assert navigator.recovery.elapsed_ms(999.0) == 0.0, "the episode was left open"


def test_restored_movement_returns_straight_to_pursuit() -> None:
    """Not through a stop, and not through a fresh stationary acquisition:
    that is what made every obstacle cost a restart."""
    guard = ProgressGuard(
        WALKING, ProgressConfig(suspect_after_ms=100, min_applied_forward_ms=50)
    )
    navigator = _navigator(progress=guard)
    navigator.note_applied(_applied("w"), now_s=0.0)

    moving = _motion(0.0)
    resumed = None
    for index in range(1, 200):
        now = index * 0.05
        decision = navigator.decide(
            _inputs(error_deg=0.5, motion=moving, sequence=index, captured_at_s=now),
            generation=1,
            now_s=now,
        )
        navigator.note_held(sorted(key.value for key in decision.movement.keys), now_s=now)
        if decision.phase is NavigationPhase.RECOVERY:
            # The maneuver worked: the world starts moving again.
            moving = _motion(0.5)
        elif moving.forward_speed_norm and decision.phase in (
            NavigationPhase.FOLLOW,
            NavigationPhase.CORRECT,
        ):
            resumed = decision
            break

    assert resumed is not None, "recovery never handed control back to pursuit"
    assert resumed.command is not None and resumed.command.forward_axis == 1
    assert not navigator.recovery.active


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


def test_losing_focus_ends_a_recovery_episode_rather_than_pausing_it() -> None:
    """Safety preempts a maneuver, and a maneuver is the one state that would
    otherwise miss it: the follower's safety checks live in the steering path,
    which recovery does not go through. The actuator and the coordinator both
    release on their own, so this is not the only guard - but the episode has
    to be *ended*, not left armed to resume the moment focus comes back.
    """
    guard = ProgressGuard(
        WALKING, ProgressConfig(suspect_after_ms=100, min_applied_forward_ms=50)
    )
    navigator = _navigator(progress=guard)
    decisions = _stalled_route(navigator, ticks=40)
    assert any(d.phase is NavigationPhase.RECOVERY for d in decisions)
    assert navigator.recovery.active

    navigator.note_health(focus_ok=False, processed_fps=60.0)
    lost = navigator.decide(
        _inputs(error_deg=0.5, motion=_motion(0.0), sequence=999, captured_at_s=2.0),
        generation=1,
        now_s=2.0,
    )

    assert lost.release and lost.movement.idle
    assert lost.phase is NavigationPhase.FAILED
    assert not navigator.recovery.active, "the episode was left armed to resume"


# ---------------------------------------------------------------------------
# Arrival by geometry: walking over the target
# ---------------------------------------------------------------------------


def _pass_watcher(**overrides: Any) -> Any:
    from prospector_engine.navigation import WaypointPass, WaypointPassConfig

    return WaypointPass(WaypointPassConfig(**overrides))


def _approach(watcher: Any, frames: int = 12, error: float = 5.0) -> None:
    for _ in range(frames):
        assert not watcher.observe(error_deg=error, walking=True)


def test_walking_over_the_target_is_an_arrival() -> None:
    """The signal the banner cannot give, because by the time the route has
    overshot the banner has already been and gone."""
    watcher = _pass_watcher(approach_frames=8, passed_frames=4)
    _approach(watcher)

    results = [watcher.observe(error_deg=178.0, walking=True) for _ in range(4)]

    assert results[:3] == [False, False, False]
    assert results[3], "walking over the target was never recognised"
    assert "walked over it" in watcher.detail


def test_a_swing_without_an_approach_is_not_an_arrival() -> None:
    """A route that merely passes near something must not latch on it."""
    watcher = _pass_watcher(approach_frames=8, passed_frames=4)

    results = [watcher.observe(error_deg=178.0, walking=True) for _ in range(20)]

    assert not any(results)


def test_our_own_pivot_is_not_an_arrival() -> None:
    """A hard turn swings the bearing too, and that is us turning."""
    watcher = _pass_watcher(approach_frames=8, passed_frames=4, min_unexplained_swing_deg=90.0)
    _approach(watcher)
    watcher.note_commanded_yaw(120.0)

    results = [watcher.observe(error_deg=178.0, walking=True) for _ in range(10)]

    assert not any(results)
    assert "ourselves" in watcher.detail


def test_a_course_correction_that_comes_back_is_not_an_arrival() -> None:
    """The bearing has to stay behind. Swinging out and back is steering."""
    watcher = _pass_watcher(approach_frames=8, passed_frames=4)
    _approach(watcher)

    assert not watcher.observe(error_deg=150.0, walking=True)
    assert not watcher.observe(error_deg=150.0, walking=True)
    assert not watcher.observe(error_deg=10.0, walking=True)
    results = [watcher.observe(error_deg=150.0, walking=True) for _ in range(3)]

    assert not any(results), "a swing that came back still latched"


def test_an_unreadable_bearing_holds_the_count_rather_than_dropping_it() -> None:
    """The arrow is commonly unreadable for a moment as it passes closest, and
    losing the approach to that would make the signal useless exactly when it
    is needed."""
    watcher = _pass_watcher(approach_frames=8, passed_frames=4)
    _approach(watcher)

    for _ in range(5):
        assert not watcher.observe(error_deg=None, walking=True)
    results = [watcher.observe(error_deg=178.0, walking=True) for _ in range(4)]

    assert results[3]


def test_standing_still_does_not_count_as_approaching() -> None:
    watcher = _pass_watcher(approach_frames=8, passed_frames=4)
    for _ in range(20):
        watcher.observe(error_deg=5.0, walking=False)

    results = [watcher.observe(error_deg=178.0, walking=True) for _ in range(6)]

    assert not any(results)


def test_the_navigator_ends_the_route_when_it_walks_over_the_target() -> None:
    """End to end through the real Navigator, with no banner at all."""
    navigator = _navigator()
    navigator.note_health(focus_ok=True, processed_fps=60.0)

    final = None
    for index in range(1, 60):
        now = index * 0.02
        # Approaching for the first 30 frames, then the target is behind us.
        error = 3.0 if index <= 30 else 176.0
        decision = navigator.decide(
            _inputs(error_deg=error, sequence=index, captured_at_s=now),
            generation=1,
            now_s=now,
        )
        navigator.note_held(sorted(key.value for key in decision.movement.keys), now_s=now)
        if decision.phase is NavigationPhase.ARRIVED:
            final = decision
            break

    assert final is not None, "the route never noticed it had walked over the spot"
    assert final.release and final.movement.idle
    assert "walked over it" in final.reason


def test_arriving_in_water_keeps_walking_instead_of_stopping() -> None:
    """The Rotwood Swamp has alligators in the water; a second or two standing
    in it is fatal, so arrival cannot mean "stop here" unconditionally."""
    from prospector_engine.contracts import ArrivalObservation
    from prospector_engine.vision import WaterReading

    navigator = _navigator()
    arrival = ArrivalObservation(1.0, 4, 6, "m1", True, ("latched",))
    wet = WaterReading(fraction=0.8, driest_deg=90.0)

    decision = navigator.decide(
        replace(_inputs(arrival=arrival), water=wet), generation=1, now_s=0.0
    )

    assert decision.phase is not NavigationPhase.ARRIVED
    assert decision.movement.forward == 1, "it stopped in the water"
    assert decision.movement.turn == 1, "it did not steer toward dry ground"


def test_arriving_on_dry_ground_stops_at_once() -> None:
    from prospector_engine.contracts import ArrivalObservation
    from prospector_engine.vision import WaterReading

    navigator = _navigator()
    arrival = ArrivalObservation(1.0, 4, 6, "m1", True, ("latched",))
    dry = WaterReading(fraction=0.0, driest_deg=0.0)

    decision = navigator.decide(
        replace(_inputs(arrival=arrival), water=dry), generation=1, now_s=0.0
    )

    assert decision.phase is NavigationPhase.ARRIVED
    assert decision.movement.idle


def test_the_walk_out_of_water_is_bounded_and_then_stops_anyway() -> None:
    """Wandering is worse than standing: past the budget it stops regardless,
    because standing somewhere is a state a person can see and fix."""
    from prospector_engine.contracts import ArrivalObservation
    from prospector_engine.vision import WaterReading

    navigator = _navigator()
    arrival = ArrivalObservation(1.0, 4, 6, "m1", True, ("latched",))
    wet = WaterReading(fraction=0.9, driest_deg=45.0)

    final = None
    for index in range(1, 400):
        now = index * 0.05
        decision = navigator.decide(
            replace(_inputs(arrival=arrival, sequence=index, captured_at_s=now), water=wet),
            generation=1,
            now_s=now,
        )
        if decision.phase is NavigationPhase.ARRIVED:
            final = (index, now, decision)
            break

    assert final is not None, "it walked forever looking for dry land"
    _index, now, decision = final
    assert now <= 6.0, f"it kept walking for {now:.0f} s"
    assert decision.movement.idle
