"""The arrow follower: refusals, continuous pursuit, occlusion, and the proof.

The controller is exercised against a **simulated camera** rather than a real
one, so the closed-loop behaviour the mission specifies - correct while
walking, coast through an occlusion, search within a budget, stop only when the
target is genuinely behind us - can be measured deterministically. That is a
design check, not a field result: what a real camera does is what the turn
characterizer measures at runtime, and what a real route does is a native test.

The most important tests here fall into two groups.

The **refusals**: a controller that steers without a measured turn response, or
keeps walking with a stale frame in front of it, is the failure this module
exists to prevent.

The **continuity**: a controller that drops ``W`` to turn, or drops it because
a leaf crossed the arrow, is the failure this module was rewritten to fix. Two
measured turn responses are used throughout - a fast mouse actuator and one
with the 340 ms latency measured on the owner's machine - because a policy that
is only smooth on a fast actuator is not the policy that shipped.
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
    ControlDecision,
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

#: The held-key actuator, with the latency actually measured on this machine
#: (322-364 ms). Also test-constructed, and it is the response every claim
#: about "no stutter" has to survive: it is slow enough that a stop-turn-go
#: controller is visibly choppy on it, which is how this rewrite started.
SLOW_KEYS = TurnResponse(
    backend=TurnBackend.ARROW_KEYS,
    fingerprint=replace(FINGERPRINT, backend="arrow_keys"),
    degrees_per_unit=0.09,
    positive_is_right=True,
    min_effective_units=40,
    max_units=900,
    latency_s=0.34,
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
    controller: ArrowFollowerController,
    error: float,
    frames: int,
    *,
    fps: float = 60.0,
    start: int = 1,
) -> list[ControlDecision]:
    """Feed a constant error for ``frames`` frames and return every decision."""
    return [
        controller.update(_inputs(error, sequence=index, now_s=index / fps))
        for index in range(start, start + frames)
    ]


class Camera:
    """A world that answers the controller: turning rotates, walking does not.

    Deliberately the smallest thing that closes the loop. It models the one
    property the control law has to survive - that a correction takes measured
    time to land - and nothing about geography.
    """

    def __init__(self, response: TurnResponse, error_deg: float, fps: float = 60.0) -> None:
        self.response = response
        self.error_deg = error_deg
        self.fps = fps
        self.forward_frames = 0
        self.w_down_edges = 0
        self.w_up_edges = 0
        self.errors_while_walking: list[float] = []
        self.states: list[ControlState] = []
        self.turns: list[float] = []
        self._holding = False

    def run(
        self,
        controller: ArrowFollowerController,
        frames: int,
        *,
        hidden: frozenset[int] = frozenset(),
        confidence: float = 0.9,
        start: int = 1,
    ) -> None:
        for index in range(start, start + frames):
            now = index / self.fps
            visible = index not in hidden
            decision = controller.update(
                _inputs(
                    self.error_deg if visible else None,
                    sequence=index,
                    now_s=now,
                    processed_fps=self.fps,
                    arrow_valid=visible,
                    confidence=confidence,
                )
            )
            self.states.append(decision.state)
            if decision.plan.expected_deg:
                self.turns.append(decision.plan.expected_deg)
            walking = decision.forward > 0
            if walking and not self._holding:
                self.w_down_edges += 1
            if self._holding and not walking:
                self.w_up_edges += 1
            self._holding = walking
            if walking:
                self.forward_frames += 1
                self.errors_while_walking.append(abs(self.error_deg))
            plan = decision.plan
            if plan.turn_axis:
                rotated = plan.turn_axis * self.response.degrees_per_unit * 1000.0 / self.fps
                self.error_deg = wrap_deg(self.error_deg - rotated)
            elif plan.yaw_delta_px:
                self.error_deg = wrap_deg(
                    self.error_deg - plan.yaw_delta_px * self.response.degrees_per_unit
                )

    @property
    def duty_cycle(self) -> float:
        return self.forward_frames / max(1, len(self.states))

    def turn_reversals(self, min_deg: float = 0.0) -> int:
        """How many times a correction of real size changed direction.

        ``min_deg`` matters. Near convergence the controller asks for the
        actuator's own minimum step, and the sign of a half-degree nudge
        alternating is the deadband floor working, not a hunt. A hunt is a
        *visible* correction that keeps changing its mind.
        """
        signs = [1 if turn > 0 else -1 for turn in self.turns if abs(turn) >= min_deg]
        return sum(1 for a, b in pairwise(signs) if a != b)

    def percentile(self, fraction: float) -> float:
        values = sorted(self.errors_while_walking)
        if not values:
            return math.nan
        return values[min(len(values) - 1, int(fraction * len(values)))]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_an_uncharacterized_controller_refuses_to_steer() -> None:
    decision = ArrowFollowerController(SteeringLimits(), None).update(_inputs(45.0))

    assert decision.release
    assert decision.state is ControlState.SAFE_STOP
    assert decision.blockers == ("the turn actuator has not been characterized yet",)


def test_a_pending_response_is_not_a_permission() -> None:
    pending = replace(MEASURED, status=EvidenceStatus.PENDING)

    decision = ArrowFollowerController(SteeringLimits(), pending).update(_inputs(45.0))

    assert decision.release
    assert decision.blockers == ("the measured turn response is pending",)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"fault": "deadman-unhealthy"},
        {"viewport_ok": False},
        {"focus_ok": False},
        {"cursor_safe": False},
    ],
)
def test_every_safety_condition_releases_before_anything_else(kwargs: object) -> None:
    decision = _controller().update(_inputs(2.0, **kwargs))  # type: ignore[arg-type]

    assert decision.release
    assert decision.forward == 0
    assert not decision.plan.moves


def test_a_changed_viewport_releases_rather_than_reinterpreting_angles() -> None:
    controller = _controller()
    controller.update(_inputs(4.0, sequence=1, geometry_revision=3))

    decision = controller.update(_inputs(4.0, sequence=2, geometry_revision=4))

    assert decision.release
    assert decision.state is ControlState.REACQUIRE


def test_a_changed_profile_releases() -> None:
    controller = _controller()
    controller.update(_inputs(4.0, sequence=1, profile_revision=1))

    decision = controller.update(_inputs(4.0, sequence=2, profile_revision=2))

    assert decision.release


def test_a_runaway_episode_gives_up_rather_than_spinning() -> None:
    controller = _controller(max_episode_yaw_deg=30.0)
    decisions = [
        controller.update(_inputs(170.0, sequence=index, now_s=index / 60.0))
        for index in range(1, 200)
    ]

    assert any(d.release and d.state is ControlState.SAFE_STOP for d in decisions)


# ---------------------------------------------------------------------------
# Evidence freshness: the difference between a gap and a failure
# ---------------------------------------------------------------------------


def test_a_frame_well_past_its_freshness_budget_releases() -> None:
    decision = _controller().update(_inputs(2.0, age_ms=900.0))

    assert decision.release
    assert decision.state is ControlState.REACQUIRE


def test_a_frame_slightly_late_keeps_walking_and_lets_go_of_the_camera() -> None:
    """A single slow frame is not a reason for a W-up/W-down rattle.

    The actuator heartbeat and the deadman still release on a genuinely dead
    pipeline; this window only covers a gap short enough that the character was
    walking correctly a moment ago and still is.
    """
    controller = _controller()
    walking = _drive(controller, 20.0, 12)
    assert walking[-1].forward == 1

    decision = controller.update(_inputs(20.0, sequence=99, now_s=1.0, age_ms=180.0))

    assert not decision.release
    assert decision.forward == 1, "one late frame let go of the forward hold"
    assert not decision.plan.moves, "it made a new correction from a stale heading"


def test_a_repeated_frame_holds_the_level_rather_than_releasing_it() -> None:
    controller = _controller()
    _drive(controller, 20.0, 10)

    repeat = controller.update(_inputs(20.0, sequence=5, now_s=1.0))

    assert not repeat.release
    assert repeat.forward == 1, "a duplicate frame dropped the walk"


# ---------------------------------------------------------------------------
# Continuous pursuit
# ---------------------------------------------------------------------------


def test_walking_starts_without_waiting_for_a_cone() -> None:
    """The old controller wanted three consecutive frames inside eight degrees
    before it would press W. Waiting is not free: it is where the stutter came
    from."""
    decision = _controller().update(_inputs(20.0))

    assert decision.forward == 1
    assert decision.kind is CommandKind.FOLLOW


@pytest.mark.parametrize("error", [8.0, -15.0, 30.0, -34.0])
def test_a_moderate_error_corrects_while_still_walking(error: float) -> None:
    controller = _controller()
    decisions = _drive(controller, error, 6)

    correcting = [d for d in decisions if d.plan.moves]
    assert correcting, f"no correction was issued for {error:+.0f} degrees"
    assert all(d.forward == 1 for d in correcting), "it stopped walking to turn"
    assert any(d.state is ControlState.CORRECT for d in decisions)


@pytest.mark.parametrize("error", [45.0, -60.0])
def test_a_strong_error_corrects_harder_and_still_walks(error: float) -> None:
    gentle = _controller()
    strong = _controller()
    small = max(abs(d.plan.expected_deg) for d in _drive(gentle, 20.0, 6))
    large = max(abs(d.plan.expected_deg) for d in _drive(strong, error, 6))

    assert large > small, "the strong band asked for no more than the gentle one"
    assert all(d.forward == 1 for d in _drive(_controller(), error, 6))


def test_a_target_flatly_behind_pivots_without_waiting() -> None:
    """Past ``pivot_immediate_deg`` there is no reading of the frame in which
    walking on is right, and the confirmation is a fifth of a second spent
    walking further from the target."""
    controller = _controller()
    decisions = _drive(controller, 170.0, 40)

    assert decisions[0].state is ControlState.ALIGN
    pivots = [d for d in decisions if d.state is ControlState.ALIGN]
    assert all(p.forward == 0 for p in pivots)
    assert all(p.kind is CommandKind.ALIGN for p in pivots)


def test_a_merely_severe_error_is_confirmed_before_it_stops_the_character() -> None:
    """One bad frame must never cost a stop. Between ``strong_band_deg`` and
    ``pivot_immediate_deg`` the confirmation still runs."""
    limits = SteeringLimits()
    error = (limits.strong_band_deg + limits.pivot_immediate_deg) / 2.0
    controller = _controller()
    decisions = _drive(controller, error, 40)

    assert decisions[0].forward == 1, "one frame of a severe error stopped it dead"
    assert any(d.state is ControlState.ALIGN for d in decisions), "it never pivoted"


def test_a_pivot_ends_well_inside_the_band_that_started_it() -> None:
    """Hysteresis, so a heading hovering on the boundary cannot alternate."""
    limits = SteeringLimits()
    controller = _controller()
    # A few frames, so the pivot is entered; short enough that the
    # runaway-episode guard - which this constant error would eventually trip -
    # has not fired.
    _drive(controller, 170.0, 6)
    assert controller.state is ControlState.ALIGN

    # A few frames, not one: a 130-degree jump in a single frame is exactly
    # what the heading filter's outlier gate exists to refuse, and it is right
    # to refuse it. The gate yields after ``max_consecutive_outliers``.
    inside = limits.pivot_release_deg - 5.0
    for index in range(20, 30):
        resumed = controller.update(_inputs(inside, sequence=index, now_s=index / 60.0))

    assert resumed.forward == 1
    assert resumed.state is not ControlState.ALIGN


def test_a_turn_pulse_completing_does_not_release_the_forward_hold() -> None:
    """The single most important property in this file."""
    camera = Camera(SLOW_KEYS, error_deg=50.0)
    camera.run(_controller(SLOW_KEYS), 240)

    assert camera.w_up_edges == 0, "a completing correction dropped W"
    assert camera.w_down_edges == 1


@pytest.mark.parametrize("fps", [30.0, 60.0, 90.0])
def test_a_straight_route_is_one_press_and_no_rattle(fps: float) -> None:
    camera = Camera(MEASURED, error_deg=0.0, fps=fps)
    camera.run(_controller(), int(fps * 30))

    assert camera.w_down_edges == 1, f"{camera.w_down_edges} presses on a straight route"
    assert camera.w_up_edges == 0
    assert camera.duty_cycle == 1.0


@pytest.mark.parametrize("response", [MEASURED, SLOW_KEYS])
@pytest.mark.parametrize("initial", [10.0, -25.0, 55.0])
def test_an_ordinary_route_keeps_a_high_forward_duty_cycle(
    response: TurnResponse, initial: float
) -> None:
    camera = Camera(response, error_deg=initial)
    camera.run(_controller(response), 480)

    assert camera.duty_cycle >= 0.90, f"walked only {camera.duty_cycle:.0%} of the time"
    assert camera.w_down_edges == 1


@pytest.mark.parametrize("response", [MEASURED, SLOW_KEYS])
@pytest.mark.parametrize("initial", [12.0, -30.0, 50.0, -65.0])
def test_alignment_settles_inside_the_acceptance_targets(
    response: TurnResponse, initial: float
) -> None:
    """Median under ten degrees and p95 under twenty-five, once the first
    second of convergence - the mission's "hard turn" exclusion - is past."""
    camera = Camera(response, error_deg=initial)
    camera.run(_controller(response), 480)
    settled = camera.errors_while_walking[60:]
    settled.sort()

    p50 = settled[len(settled) // 2]
    p95 = settled[int(0.95 * len(settled))]
    assert p50 < 10.0, f"median alignment {p50:.1f} degrees"
    assert p95 < 25.0, f"p95 alignment {p95:.1f} degrees"


@pytest.mark.parametrize("fps", [30.0, 60.0, 90.0])
def test_the_route_is_the_same_at_every_cadence(fps: float) -> None:
    camera = Camera(MEASURED, error_deg=60.0, fps=fps)
    camera.run(_controller(), int(fps * 4))

    assert abs(camera.error_deg) < 8.0, f"settled at {camera.error_deg:+.1f} at {fps} fps"
    assert camera.duty_cycle > 0.95


def test_the_turn_sign_follows_the_error_sign() -> None:
    right = _controller().update(_inputs(30.0))
    left = _controller().update(_inputs(-30.0))

    assert right.plan.yaw_delta_px > 0
    assert left.plan.yaw_delta_px < 0


def test_a_measured_inverted_axis_is_honoured() -> None:
    inverted = replace(MEASURED, positive_is_right=False)

    decision = ArrowFollowerController(SteeringLimits(), inverted).update(_inputs(40.0))

    assert decision.plan.yaw_delta_px < 0


def test_lower_confidence_reduces_magnitude_and_never_raises_it() -> None:
    confident = _controller().update(_inputs(40.0, confidence=1.0))
    unsure = _controller().update(_inputs(40.0, confidence=0.5))

    assert abs(unsure.plan.expected_deg) <= abs(confident.plan.expected_deg)


def test_a_correction_never_exceeds_the_error_that_remains() -> None:
    for error in (3.0, 9.0, 25.0, 60.0, 120.0):
        decision = _controller().update(_inputs(error))
        assert abs(decision.plan.expected_deg) <= abs(error) + 1e-6


def test_the_correction_is_always_bounded() -> None:
    """Two ceilings, because a pivot and a correction-while-walking are
    different things: a controller walking through its own correction must not
    out-turn the route, and one standing still deliberately turning round must
    not be limited to thirty degrees a go."""
    limits = TurnLimits()
    for error in (-179.0, -90.0, 15.0, 120.0, 179.0):
        decision = _controller().update(_inputs(error))
        ceiling = (
            limits.max_pivot_correction_deg
            if decision.state is ControlState.ALIGN
            else limits.max_correction_deg
        )
        assert abs(decision.plan.expected_deg) <= ceiling + 1e-6
        assert abs(decision.plan.expected_deg) <= limits.max_pivot_correction_deg + 1e-6


def test_the_deadband_never_goes_below_the_measured_actuator_resolution() -> None:
    coarse = replace(MEASURED, degrees_per_unit=4.0, min_effective_units=2)
    controller = ArrowFollowerController(SteeringLimits(yaw_deadband_deg=1.0), coarse)

    decision = controller.update(_inputs(6.0))

    assert not decision.plan.moves, "it asked for a rotation finer than the actuator can make"
    assert decision.forward == 1, "the deadband stopped the walk as well as the turn"


def test_detector_jitter_does_not_make_the_camera_chatter() -> None:
    """The failure this is about: a converged route whose heading estimate
    wobbles a few degrees either side of zero, and a controller that answers
    every wobble with a correction in the opposite direction."""
    controller = _controller()
    camera = Camera(MEASURED, error_deg=20.0)
    camera.run(controller, 120)
    assert abs(camera.error_deg) < 4.0, "the route never converged"

    jitter = [3.0, -3.0, 2.5, -2.8, 3.2, -2.6] * 5
    following = [
        controller.update(_inputs(value, sequence=index, now_s=index / 60.0))
        for index, value in enumerate(jitter, start=121)
    ]

    assert not any(d.plan.moves for d in following), "jitter reached the camera"
    assert all(d.forward == 1 for d in following), "jitter stopped the walk"


@pytest.mark.parametrize("response", [MEASURED, SLOW_KEYS])
@pytest.mark.parametrize("initial", [45.0, -45.0])
def test_a_converging_route_does_not_hunt(response: TurnResponse, initial: float) -> None:
    """Hunting is the visible symptom of a controller correcting against its own
    actuator latency: turn, overshoot, turn back, overshoot again. At most one
    change of direction is a settle; more than that is a hunt."""
    camera = Camera(response, error_deg=initial)
    camera.run(_controller(response), 300)
    reversals = camera.turn_reversals(min_deg=3.0)

    assert reversals <= 1, (
        f"the camera changed direction {reversals} times converging from {initial:+.0f}"
    )
    settled = camera.errors_while_walking[150:]
    assert max(settled) < 8.0, f"it never settled: peak error {max(settled):.1f} degrees"


def test_only_one_correction_is_in_flight_at_a_time() -> None:
    controller = _controller(SLOW_KEYS)
    decisions = _drive(controller, 40.0, 8)

    issued = [d for d in decisions if d.plan.expected_deg != 0.0]
    assert len(issued) == 1, "corrections were stacked inside the actuator's own latency"


def test_fresh_evidence_can_end_a_correction_early() -> None:
    """A correction is not run to a duration planned from a heading that has
    since stopped existing."""
    controller = _controller(SLOW_KEYS)
    _drive(controller, 40.0, 2)
    assert controller.update(_inputs(40.0, sequence=3, now_s=3 / 60)).plan.turn_axis != 0

    settled = controller.update(_inputs(0.5, sequence=4, now_s=4 / 60))

    assert settled.plan.turn_axis == 0, "it kept turning after the error closed"
    assert settled.forward == 1


# ---------------------------------------------------------------------------
# Occlusion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loss_ms", [100.0, 500.0, 1000.0, 2000.0])
def test_an_occlusion_inside_the_grace_costs_no_forward_releases(loss_ms: float) -> None:
    """Existing traces hold healthy losses of 0.7 to 2.65 seconds behind
    foliage. Every one of them used to stop the character."""
    fps = 60.0
    hidden = frozenset(range(61, 61 + int(loss_ms / 1000.0 * fps)))
    camera = Camera(MEASURED, error_deg=8.0, fps=fps)
    camera.run(_controller(), 300, hidden=hidden)

    assert camera.w_up_edges == 0, f"{loss_ms:.0f} ms of occlusion released the walk"
    assert ControlState.COAST in camera.states


def test_coasting_holds_the_course_and_lets_go_of_the_camera() -> None:
    controller = _controller()
    _drive(controller, 25.0, 20)

    coasting = [
        controller.update(_inputs(None, sequence=index, now_s=index / 60.0, arrow_valid=False))
        for index in range(21, 60)
    ]

    assert all(d.state is ControlState.COAST for d in coasting)
    assert all(d.forward == 1 for d in coasting)
    late = coasting[-1]
    assert not late.plan.moves, "it kept turning blind on a heading that no longer exists"
    assert late.error_deg is not None, "it forgot the heading it was holding"


def test_an_abstaining_direction_is_graced_exactly_like_a_hidden_arrow() -> None:
    """The commoner of the two, and it used to stop the character dead."""
    controller = _controller()
    _drive(controller, 25.0, 20)

    decision = controller.update(_inputs(None, sequence=21, now_s=21 / 60.0))

    assert decision.forward == 1
    assert decision.state is ControlState.COAST


def test_a_reappearing_arrow_resumes_pursuit_without_a_stationary_reacquisition() -> None:
    controller = _controller()
    _drive(controller, 25.0, 20)
    for index in range(21, 80):
        controller.update(_inputs(None, sequence=index, now_s=index / 60.0, arrow_valid=False))

    resumed = controller.update(_inputs(25.0, sequence=80, now_s=80 / 60.0))

    assert resumed.forward == 1, "it stopped to re-acquire a target it never lost"
    assert resumed.state in (ControlState.FOLLOW, ControlState.CORRECT)


def test_past_the_grace_it_searches_while_still_moving() -> None:
    controller = _controller()
    _drive(controller, 25.0, 20)

    searching = [
        controller.update(_inputs(None, sequence=index, now_s=index / 60.0, arrow_valid=False))
        for index in range(21, 300)
    ]
    sweeps = [d for d in searching if d.state is ControlState.SEARCH]

    assert sweeps, "it never started searching"
    assert all(d.forward == 1 for d in sweeps), "the search stood still"
    assert any(d.plan.moves for d in sweeps), "the search never looked anywhere"


def test_the_search_sweeps_both_ways_and_starts_toward_the_last_known_side() -> None:
    controller = _controller()
    _drive(controller, 40.0, 20)
    axes: list[int] = []
    for index in range(21, 700):
        decision = controller.update(
            _inputs(None, sequence=index, now_s=index / 60.0, arrow_valid=False)
        )
        if decision.state is ControlState.SEARCH and decision.plan.yaw_delta_px:
            axes.append(1 if decision.plan.yaw_delta_px > 0 else -1)

    assert axes, "the search never turned"
    assert axes[0] == 1, "the first sweep went away from where the arrow was"
    assert -1 in axes, "the search only ever looked one way"


def test_the_search_always_terminates_inside_its_budget() -> None:
    limits = SteeringLimits()
    controller = _controller()
    _drive(controller, 25.0, 20)

    lost: ControlDecision | None = None
    for index in range(21, 2000):
        decision = controller.update(
            _inputs(None, sequence=index, now_s=index / 60.0, arrow_valid=False)
        )
        if decision.lost_target:
            lost = decision
            break

    assert lost is not None, "the search never gave up"
    assert lost.release
    assert lost.lost_for_s <= limits.search_budget_s + 0.1


def test_a_run_that_starts_with_nothing_on_screen_does_not_walk_off_blindly() -> None:
    controller = _controller()

    early = controller.update(_inputs(None, sequence=1, now_s=0.0, arrow_valid=False))
    later = [
        controller.update(_inputs(None, sequence=i, now_s=i / 60.0, arrow_valid=False))
        for i in range(2, 300)
    ]

    assert early.forward == 0 and early.state is ControlState.ACQUIRE
    searching = [d for d in later if d.state is ControlState.SEARCH]
    assert searching, "it waited forever instead of looking"
    assert all(d.forward == 0 for d in searching), "it walked with no heading at all"


# ---------------------------------------------------------------------------
# Target identity
# ---------------------------------------------------------------------------


def test_a_new_candidate_has_to_earn_the_target_before_it_is_followed() -> None:
    """One frame of foliage that scores like an arrow must not steal a route."""
    limits = SteeringLimits()
    controller = _controller()
    _drive(controller, 5.0, 10)

    decoys = [
        controller.update(_inputs(80.0, sequence=index, now_s=index / 60.0, track_id=2))
        for index in range(11, 11 + limits.identity_latch_frames - 1)
    ]

    assert all(d.state is ControlState.COAST for d in decoys), "it switched target at once"
    assert all(not d.plan.moves for d in decoys), "it turned toward a candidate on one frame"


def test_a_candidate_that_persists_is_adopted() -> None:
    limits = SteeringLimits()
    controller = _controller()
    _drive(controller, 5.0, 10)

    for index in range(11, 11 + limits.identity_latch_frames + 1):
        decision = controller.update(
            _inputs(80.0, sequence=index, now_s=index / 60.0, track_id=2)
        )

    assert controller.track_id == 2
    assert decision.forward == 1, "adopting a new target cost the walk"


def test_a_collapsed_direction_confidence_is_treated_as_an_occlusion() -> None:
    controller = _controller()
    _drive(controller, 20.0, 10)

    decision = controller.update(_inputs(20.0, sequence=11, now_s=11 / 60, confidence=0.05))

    assert decision.state is ControlState.COAST
    assert decision.forward == 1
    assert not decision.plan.moves


def test_a_merely_uncertain_direction_still_steers_but_smaller() -> None:
    sure = _controller().update(_inputs(30.0, confidence=0.95))
    unsure = _controller().update(_inputs(30.0, confidence=0.45))

    assert unsure.plan.moves
    assert abs(unsure.plan.expected_deg) < abs(sure.plan.expected_deg)


# ---------------------------------------------------------------------------
# Advisories: things worth saying that are not reasons to stop
# ---------------------------------------------------------------------------


def test_an_unmeasured_frame_rate_does_not_stop_the_run() -> None:
    decision = _controller().update(_inputs(4.0, processed_fps=0.0))

    assert not decision.release
    assert decision.advisories == ()


def test_a_genuinely_slow_pipeline_is_reported_and_not_obeyed() -> None:
    decision = _controller().update(_inputs(4.0, processed_fps=12.0))

    assert not decision.release
    assert decision.forward == 1
    assert any("adapting, not stopping" in note for note in decision.advisories)
    assert decision.blockers == ()


def test_an_unknown_pointer_position_does_not_stop_the_run() -> None:
    decision = _controller().update(_inputs(4.0, cursor_safe=True))

    assert not decision.release


def test_memory_survives_a_soften_and_not_a_reset() -> None:
    controller = _controller()
    _drive(controller, 25.0, 12)

    controller.soften()
    assert controller.heading.usable(0.3), "handing control to recovery forgot the target"

    controller.reset()
    assert not controller.heading.usable(0.3)


# ---------------------------------------------------------------------------
# The control-mode proof
# ---------------------------------------------------------------------------


def _proof(**overrides: object) -> ShiftLockProof:
    base = {
        "method": ControlModeMethod.VISUAL_CUE,
        "run_id": "run-1",
        "arm_token_id": "arm-1",
        "generation": 4,
        "window_identity": ("roblox", 1),
        "fingerprint": FINGERPRINT,
        "observed_at_s": 100.0,
        "confidence": 0.95,
        "status": EvidenceStatus.VALIDATED,
    }
    base.update(overrides)
    return ShiftLockProof(**base)  # type: ignore[arg-type]


def _situation(**overrides: object) -> dict[str, object]:
    base = {
        "run_id": "run-1",
        "arm_token_id": "arm-1",
        "generation": 4,
        "window_identity": ("roblox", 1),
        "fingerprint": FINGERPRINT,
        "now_s": 105.0,
    }
    base.update(overrides)
    return base


def test_a_verified_proof_covers_the_situation_it_was_taken_in() -> None:
    ok, why = _proof().valid_for(**_situation())  # type: ignore[arg-type]

    assert ok and why == "verified"


def test_an_unverified_proof_is_never_accepted() -> None:
    ok, why = _proof(status=EvidenceStatus.PENDING).valid_for(**_situation())  # type: ignore[arg-type]

    assert not ok and "pending" in why


def test_a_proof_cannot_be_constructed_by_asserting_one() -> None:
    assert not hasattr(ControlModeMethod, "ASSERTED")
    assert {method.value for method in ControlModeMethod} == {"visual_cue", "micro_yaw"}


@pytest.mark.parametrize(
    "change",
    [
        {"run_id": "run-2"},
        {"arm_token_id": "arm-2"},
        {"generation": 5},
        {"window_identity": ("roblox", 2)},
    ],
)
def test_a_proof_does_not_survive_a_change_in_what_it_proved(change: object) -> None:
    ok, _ = _proof().valid_for(**_situation(**change))  # type: ignore[arg-type]

    assert not ok


def test_a_proof_does_not_survive_a_sensitivity_change() -> None:
    changed = replace(FINGERPRINT, camera_sensitivity="high")

    ok, why = _proof().valid_for(**_situation(fingerprint=changed))  # type: ignore[arg-type]

    assert not ok and "camera_sensitivity" in why


def test_a_proof_expires_because_shift_lock_can_be_toggled_at_any_time() -> None:
    ok, why = _proof().valid_for(**_situation(now_s=100.0 + ShiftLockProof.MAX_AGE_S + 1))  # type: ignore[arg-type]

    assert not ok and "re-observed" in why


def test_the_control_fingerprint_names_what_differs() -> None:
    other = replace(FINGERPRINT, profile_revision=2, camera_sensitivity="high")

    mismatches = FINGERPRINT.mismatches(other)

    assert any(note.startswith("profile_revision") for note in mismatches)
    assert any(note.startswith("camera_sensitivity") for note in mismatches)
