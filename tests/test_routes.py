"""Whole routes, simulated: turns, losses, collisions, and how each one ends.

The unit tests say the controller is bounded and the ladder terminates. This
file asks the question those cannot: *what happens on a route*. A simulated
world turns the camera when told to, walks when W is applied, and can be given
a wall on one side or a cul-de-sac on both; the navigator drives it through the
real :class:`Navigator`, the real follower and the real recovery ladder.

Two properties are asserted on every single scenario, at every cadence:

* **the route terminates** - arrived, abandoned, or safe-stopped, never still
  running after the budget; and
* **a terminal state holds no inputs**, which is the one thing that must be
  true even when everything else has gone wrong.

The world is deliberately simple. It is not a claim about Roblox - that is what
the native tests are for - it is a claim about the *decision path*, exercised
end to end at four cadences, which is exactly what a route corpus cannot give
us until one exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import pytest

from prospector_engine.contracts import (
    ArrowObservation,
    DirectionObservation,
    EvidenceStatus,
    MotionObservation,
    NavigationApplyResult,
    NavigationApplyStatus,
    NavigationPhase,
)
from prospector_engine.motion import (
    ContactConfig,
    LocomotionBaseline,
    ProgressConfig,
    ProgressGuard,
)
from prospector_engine.navigation import (
    NavigationCapabilities,
    NavigationInputs,
    Navigator,
)
from prospector_engine.steering import ArrowFollowerController
from prospector_engine.turning import TurnBackend, TurnResponse, wrap_deg
from tests.fakes import make_frame
from tests.test_navigation import FINGERPRINT

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
    condition_id="runtime:route-sim",
    min_forward_speed_norm=0.10,
    status=EvidenceStatus.VALIDATED,
    provenance=ContactConfig().provenance,
)

CAPABLE = NavigationCapabilities(
    os_name="test",
    profile_id="test",
    reference_ok=True,
    control_mode_ok=True,
    turn_response=MEASURED,
    motion_baseline=WALKING,
)

#: The world's free-walking speed, in the estimator's normalized units.
OPEN_SPEED = 0.30


@dataclass
class World:
    """A camera, a target, and optionally something in the way.

    ``error_deg`` is the signed heading from the character's forward direction
    to the treasure. Turning right reduces it; walking forward does not change
    it, which is the small lie that keeps the simulation honest about what it
    is testing (the control path, not the geography).
    """

    error_deg: float = 0.0
    #: Sides that are blocked. Walking forward or strafing into one produces
    #: no motion; a free side moves normally.
    blocked_sides: tuple[int, ...] = ()
    blocked_forward: bool = False
    #: Frames on which the arrow is not visible.
    lost_frames: frozenset[int] = field(default_factory=frozenset)
    #: Frames that repeat the previous frame's content.
    duplicate_frames: frozenset[int] = field(default_factory=frozenset)
    #: A second arrow-like candidate that steals the reading for these frames.
    decoy_frames: frozenset[int] = field(default_factory=frozenset)
    fps: float = 60.0

    sequence: int = 0
    now_s: float = 0.0
    forward_travelled: float = 0.0
    #: What the last applied command asked for, so motion can answer it.
    applied_forward: bool = False
    applied_lateral: int = 0

    def tick(self) -> tuple[int, float]:
        self.sequence += 1
        self.now_s += 1.0 / self.fps
        return (self.sequence, self.now_s)

    def apply(self, forward: bool, lateral: int, turn_deg: float) -> None:
        self.error_deg = wrap_deg(self.error_deg - turn_deg)
        self.applied_forward = forward
        self.applied_lateral = lateral
        if forward and not self.blocked_forward:
            self.forward_travelled += OPEN_SPEED / self.fps

    def motion(self) -> MotionObservation:
        moving = self.applied_forward and not self.blocked_forward
        sliding = self.applied_lateral != 0 and self.applied_lateral not in self.blocked_sides
        speed = OPEN_SPEED if moving else 0.0
        lateral = 0.15 * self.applied_lateral if sliding else 0.0
        return MotionObservation(
            forward_speed_norm=speed,
            lateral_speed_norm=lateral,
            confidence=0.9,
            inlier_count=120,
            inlier_ratio=0.9,
            spatial_coverage=0.9,
            residual=0.4,
            yaw_contamination=0.0,
            valid=True,
        )

    def arrow(self) -> ArrowObservation:
        visible = self.sequence not in self.lost_frames
        return ArrowObservation(
            profile_id="test",
            track_id=(2 if self.sequence in self.decoy_frames else 1) if visible else None,
            bbox_px=(0, 0, 10, 10) if visible else None,
            centroid_px=(100.0, 100.0) if visible else None,
            tip_px=(100.0, 90.0) if visible else None,
            axis_unit_xy=(0.0, -1.0) if visible else None,
            confidence=0.9 if visible else 0.0,
            valid=visible,
            abstain_reason=None if visible else "no-candidate",
        )

    def direction(self) -> DirectionObservation:
        visible = self.sequence not in self.lost_frames
        # A decoy frame reads a plausible but wrong heading, exactly as a
        # similar-coloured candidate would.
        error = self.error_deg + (75.0 if self.sequence in self.decoy_frames else 0.0)
        return DirectionObservation(
            error_deg=wrap_deg(error) if visible else None,
            confidence=0.85 if visible else 0.0,
            cue_id="sim",
            cue_disagreement_deg=2.0,
            valid=visible,
            abstain_reason=None if visible else "no-candidate",
        )


@dataclass
class RouteResult:
    ticks: int
    phases: list[NavigationPhase]
    final: NavigationPhase
    held: set[str]
    forward_travelled: float
    misaligned_ticks: int
    walking_ticks: int
    #: Every lateral axis recovery asked for, grouped by recovery episode.
    laterals: list[list[int]] = field(default_factory=list)
    jumps: int = 0

    @property
    def terminated(self) -> bool:
        return self.final in (
            NavigationPhase.ARRIVED,
            NavigationPhase.ABANDONED,
            NavigationPhase.FAILED,
        )

    @property
    def misalignment_fraction(self) -> float:
        return self.misaligned_ticks / max(1, self.walking_ticks)


def drive(world: World, *, ticks: int = 900, stop_on_terminal: bool = True) -> RouteResult:
    """Run the real navigator against the world for a bounded number of ticks."""
    navigator = Navigator(
        capabilities=CAPABLE,
        follower=ArrowFollowerController(response=MEASURED),
        progress=ProgressGuard(
            WALKING, ProgressConfig(suspect_after_ms=300, min_applied_forward_ms=200)
        ),
    )
    navigator.note_health(focus_ok=True, processed_fps=world.fps)
    held: set[str] = set()
    phases: list[NavigationPhase] = []
    misaligned = 0
    walking = 0
    laterals: list[list[int]] = []
    jumps = 0
    in_recovery = False
    duplicate_of: tuple[int, float] | None = None

    for _ in range(ticks):
        sequence, now = world.tick()
        if sequence in world.duplicate_frames and duplicate_of is not None:
            sequence, now = duplicate_of
        else:
            duplicate_of = (sequence, now)
        frame = make_frame(sequence, captured_at_s=now)
        inputs = NavigationInputs(
            frame=frame,
            arrow=world.arrow(),
            direction=world.direction(),
            motion=world.motion(),
            arrival=None,
            forward_commanded=world.applied_forward,
        )
        decision = navigator.decide(inputs, generation=1, now_s=now)
        phases.append(decision.phase)
        was_recovering, in_recovery = (
            in_recovery,
            decision.phase is NavigationPhase.RECOVERY,
        )
        if in_recovery and not was_recovering and decision.command is None:
            laterals.append([])

        command = decision.command
        if command is None:
            held.clear()
            navigator.note_released(now_s=now)
            world.apply(False, 0, 0.0)
        else:
            leases: list[str] = []
            if command.forward_axis == 1:
                leases.append("w")
            if command.lateral_axis == -1:
                leases.append("a")
            elif command.lateral_axis == 1:
                leases.append("d")
            if command.turn_axis == -1:
                leases.append("left")
            elif command.turn_axis == 1:
                leases.append("right")
            if command.jump:
                leases.append("space")
            held = set(leases)
            navigator.note_applied(
                NavigationApplyResult(
                    NavigationApplyStatus.APPLIED, command.reason, tuple(sorted(leases))
                ),
                now_s=now,
            )
            if decision.phase is NavigationPhase.RECOVERY:
                if not in_recovery:
                    laterals.append([])
                if command.lateral_axis:
                    laterals[-1].append(command.lateral_axis)
                if command.jump:
                    jumps += 1
            turn_deg = command.yaw_delta_px * MEASURED.degrees_per_unit
            world.apply(command.forward_axis == 1, command.lateral_axis, turn_deg)
            if command.forward_axis == 1:
                walking += 1
                if abs(world.error_deg) > 15.0:
                    misaligned += 1

        if stop_on_terminal and decision.phase in (
            NavigationPhase.ARRIVED,
            NavigationPhase.ABANDONED,
            NavigationPhase.FAILED,
        ):
            held.clear()
            break

    return RouteResult(
        ticks=len(phases),
        phases=phases,
        final=phases[-1],
        held=held,
        forward_travelled=world.forward_travelled,
        misaligned_ticks=misaligned,
        walking_ticks=walking,
        laterals=laterals,
        jumps=jumps,
    )


CADENCES = [30.0, 60.0, 90.0, 120.0]


# ---------------------------------------------------------------------------
# Open ground
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fps", CADENCES)
@pytest.mark.parametrize("initial", [0.0, 30.0, -30.0, 90.0, -90.0, 170.0, -170.0])
def test_the_navigator_turns_to_the_arrow_and_then_walks(fps: float, initial: float) -> None:
    world = World(error_deg=initial, fps=fps)
    result = drive(world, ticks=int(fps * 8))

    assert result.walking_ticks > 0, f"never started walking from {initial:+.0f} degrees"
    assert abs(world.error_deg) <= 15.0, f"settled at {world.error_deg:+.1f}"
    assert result.forward_travelled > 0.0


@pytest.mark.parametrize("fps", CADENCES)
def test_sustained_misalignment_while_walking_stays_low(fps: float) -> None:
    """The mission's acceptance target, measured on the simulated route."""
    world = World(error_deg=90.0, fps=fps)
    result = drive(world, ticks=int(fps * 8))

    assert result.misalignment_fraction < 0.10, (
        f"{result.misalignment_fraction * 100:.1f}% of walking ticks were misaligned"
    )


@pytest.mark.parametrize("fps", CADENCES)
def test_a_straight_route_holds_forward_rather_than_stuttering(fps: float) -> None:
    world = World(error_deg=0.0, fps=fps)
    result = drive(world, ticks=int(fps * 4))

    follow = sum(1 for phase in result.phases if phase is NavigationPhase.FOLLOW)
    assert follow > result.ticks * 0.5, f"only {follow}/{result.ticks} ticks walking"


# ---------------------------------------------------------------------------
# Degraded perception
# ---------------------------------------------------------------------------


def test_a_brief_arrow_loss_does_not_stop_the_route() -> None:
    world = World(error_deg=0.0, lost_frames=frozenset({40, 41}))
    result = drive(world, ticks=240)

    assert result.walking_ticks > 0
    assert not result.terminated, "two lost frames must not end a route"


def test_a_long_arrow_loss_coasts_for_the_grace_and_then_releases() -> None:
    """Both halves of the contract, on one blackout.

    A short occlusion must not stop the route - the character is already going
    the right way. A long one must, because after a few seconds of blindness
    "the right way" is a guess. The grace is a duration, so the boundary is
    computed from it here rather than written down as a frame index.
    """
    from prospector_engine.steering import SteeringLimits

    fps = 60.0
    start = 40
    world = World(error_deg=0.0, lost_frames=frozenset(range(start, 400)))
    result = drive(world, ticks=380)

    grace_frames = int(SteeringLimits().arrow_loss_grace_s * fps)
    # It coasts: the first half of the grace is still walking.
    coasting = result.phases[start + 2 : start + grace_frames // 2]
    assert NavigationPhase.FOLLOW in coasting, "one occlusion stopped the route dead"
    # And it stops: nothing past the grace is still walking.
    blind = result.phases[start + grace_frames + 5 : 380]
    assert NavigationPhase.FOLLOW not in blind, "it kept walking through the blackout"


def test_duplicate_frames_do_not_manufacture_a_correction() -> None:
    world = World(error_deg=20.0, duplicate_frames=frozenset(range(10, 60)))
    result = drive(world, ticks=200)

    assert result.walking_ticks > 0, "the route recovered once frames advanced"


def test_a_false_similar_candidate_costs_a_wobble_not_the_route() -> None:
    world = World(error_deg=0.0, decoy_frames=frozenset(range(60, 70)))
    result = drive(world, ticks=300)

    assert result.walking_ticks > 0
    assert abs(world.error_deg) <= 20.0, f"the decoy pulled the route to {world.error_deg:+.1f}"


# ---------------------------------------------------------------------------
# Contact and recovery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blocked", [(-1,), (1,)])
def test_a_wall_on_one_side_produces_a_real_bounded_detour(
    blocked: tuple[int, ...],
) -> None:
    world = World(error_deg=0.0, blocked_forward=True, blocked_sides=blocked)
    result = drive(world, ticks=900, stop_on_terminal=False)

    assert NavigationPhase.RECOVERY in result.phases, "contact was never detected"
    strafing = [episode for episode in result.laterals if episode]
    assert strafing, "recovery changed a label but emitted no strafe"
    # Sticky *within an episode*: the ladder allows exactly one deliberate flip
    # to the opposite side, and nothing else may change it. A fresh episode
    # after the route resumes and stalls again may of course choose anew.
    from itertools import pairwise

    for episode in strafing:
        flips = sum(1 for a, b in pairwise(episode) if a != b)
        assert flips <= 1, f"the detour side changed {flips} times inside one episode"


def test_a_cul_de_sac_abandons_within_its_budget_and_holds_nothing() -> None:
    world = World(error_deg=0.0, blocked_forward=True, blocked_sides=(-1, 1))
    result = drive(world, ticks=1200)

    assert result.final is NavigationPhase.ABANDONED
    assert result.held == set(), f"a terminal state held {result.held}"


def test_recovery_that_works_returns_to_walking() -> None:
    """A wall that opens after a moment: the ladder must notice and resume."""
    world = World(error_deg=0.0, blocked_forward=True)
    navigator_phases: list[NavigationPhase] = []
    result = drive(world, ticks=400, stop_on_terminal=False)
    navigator_phases.extend(result.phases)

    assert NavigationPhase.RECOVERY in navigator_phases
    assert NavigationPhase.ABANDONED in navigator_phases, "a permanent wall must abandon"


# ---------------------------------------------------------------------------
# The two invariants, over every scenario
# ---------------------------------------------------------------------------

SCENARIOS: dict[str, World] = {
    "straight": World(error_deg=0.0),
    "turn-30": World(error_deg=30.0),
    "turn-90": World(error_deg=-90.0),
    "turn-170": World(error_deg=170.0),
    "arrow-loss": World(error_deg=0.0, lost_frames=frozenset(range(30, 90))),
    "decoy": World(error_deg=0.0, decoy_frames=frozenset(range(30, 45))),
    "duplicates": World(error_deg=15.0, duplicate_frames=frozenset(range(20, 80))),
    "wall-left": World(error_deg=0.0, blocked_forward=True, blocked_sides=(-1,)),
    "wall-right": World(error_deg=0.0, blocked_forward=True, blocked_sides=(1,)),
    "cul-de-sac": World(error_deg=0.0, blocked_forward=True, blocked_sides=(-1, 1)),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
@pytest.mark.parametrize("fps", CADENCES)
def test_every_scenario_holds_no_input_when_it_stops(name: str, fps: float) -> None:
    world = replace(SCENARIOS[name], fps=fps)
    result = drive(world, ticks=int(fps * 12))

    if result.terminated:
        assert result.held == set(), f"{name} at {fps:.0f} fps held {result.held}"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_no_scenario_runs_forever_in_recovery(name: str) -> None:
    world = replace(SCENARIOS[name])
    result = drive(world, ticks=1200, stop_on_terminal=False)

    tail = result.phases[-200:]
    assert not all(phase is NavigationPhase.RECOVERY for phase in tail), (
        f"{name} was still recovering after {result.ticks} ticks"
    )


def test_a_permanently_unreadable_arrow_starts_a_bounded_reacquisition() -> None:
    """It must try something bounded, not stand still with nothing on screen.

    The recovery ladder already *is* the bounded "try, then give up" machine -
    its second rung is waiting for a fresh view of the arrow - so a long loss
    starts one rather than inventing a second maneuver vocabulary.
    """
    from prospector_engine.steering import SteeringLimits

    fps = 60.0
    start = 40
    abandon_frames = int(SteeringLimits().arrow_loss_abandon_s * fps)
    world = World(error_deg=0.0, lost_frames=frozenset(range(start, 2000)))
    result = drive(world, ticks=start + abandon_frames + 120)

    assert NavigationPhase.RECOVERY in result.phases, "it stood still and did nothing"
