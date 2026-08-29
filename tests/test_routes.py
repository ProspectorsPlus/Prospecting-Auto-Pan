"""Whole routes, simulated: turns, occlusions, obstacles, and how each one ends.

The unit tests say the controller is bounded and the ladder terminates. This
file asks the question those cannot: *what happens on a route*. A simulated
world turns the camera when told to - after the measured latency, not
instantly - walks when ``W`` is applied, and can be given an obstacle that a
running jump clears, one that only a detour clears, and one that nothing
clears. The navigator drives it through the real :class:`Navigator`, the real
follower, the real progress guard and the real recovery ladder.

Three properties are asserted on every single scenario, at every cadence:

* **the route terminates** - arrived, abandoned, or safe-stopped, never still
  running after the budget;
* **a terminal state holds no inputs**, which is the one thing that must be
  true even when everything else has gone wrong; and
* **the forward hold is not rattled** - a route that presses and releases W
  thirty times is the failure this whole pass exists to remove, and it is
  invisible to any test that only looks at where the character ended up.

The world is deliberately simple. It is not a claim about Roblox - that is what
the native tests are for - it is a claim about the *decision path*, exercised
end to end at four cadences, which is exactly what a route corpus cannot give
us until one exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from itertools import pairwise

import pytest

from prospector_engine.contracts import (
    ArrowObservation,
    DirectionObservation,
    EvidenceStatus,
    MotionObservation,
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
from prospector_engine.steering import ArrowFollowerController, SteeringLimits
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
    #: The measured latency on the owner's machine was 322-364 ms. The world
    #: below delays every commanded rotation by it, so a controller that only
    #: behaves on an instantaneous camera cannot pass anything here.
    latency_s=0.34,
    reliability=1.0,
    samples=8,
    measured_at_s=0.0,
    status=EvidenceStatus.VALIDATED,
)

WALKING = LocomotionBaseline(
    condition_id="runtime:route-sim",
    min_forward_speed_norm=0.10,
    reference_speed_norm=0.30,
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

#: One raster, shared by every frame in every route.
#:
#: ``make_frame`` allocates and zeroes a full canonical image, and a route
#: simulation runs tens of thousands of ticks: building one per tick spends the
#: entire runtime of this file in ``numpy.zeros``. Nothing here looks at a
#: pixel - perception is supplied directly by :class:`World` - and the buffer is
#: frozen non-writeable, so sharing it is safe as well as fast.
_TEMPLATE = make_frame(0)


def _frame(sequence: int, captured_at_s: float) -> object:
    return replace(
        _TEMPLATE,
        sequence=sequence,
        captured_at_s=captured_at_s,
        completed_at_s=captured_at_s + 0.005,
    )


@dataclass
class Obstacle:
    """Something in the way, and what actually gets past it.

    Three shapes, because they are the three the mission names and they need
    three different maneuvers: a curb a running jump clears, a bush a forward
    arc goes round, and a wall that nothing clears and the route must abandon.
    """

    #: A running jump clears it - and only a *running* one: ``SPACE`` from a
    #: standstill leaves the character exactly where it was.
    clears_with_jump: bool = False
    #: A forward arc to this side gets round it, after ``side_ms`` of arcing.
    clears_with_side: int | None = None
    side_ms: float = 300.0
    cleared: bool = False
    _side_held_ms: float = 0.0

    def attempt(self, *, forward: int, strafe: int, jump: bool, delta_ms: float) -> None:
        if self.cleared:
            return
        if jump and forward > 0 and self.clears_with_jump:
            self.cleared = True
            return
        if (
            self.clears_with_side is not None
            and forward > 0
            and strafe == self.clears_with_side
        ):
            self._side_held_ms += delta_ms
            if self._side_held_ms >= self.side_ms:
                self.cleared = True
        else:
            self._side_held_ms = 0.0


@dataclass
class World:
    """A camera with latency, a target, and optionally something in the way.

    ``error_deg`` is the signed heading from the character's forward direction
    to the treasure. Turning right reduces it; walking forward does not change
    it, which is the small lie that keeps the simulation honest about what it
    is testing (the control path, not the geography).
    """

    error_deg: float = 0.0
    #: Sides that are blocked. Strafing into one produces no lateral motion.
    blocked_sides: tuple[int, ...] = ()
    obstacle: Obstacle | None = None
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
    applied_forward: int = 0
    applied_lateral: int = 0
    #: Commanded rotations that have not landed yet: ``(lands_at_s, degrees)``.
    pending: list[tuple[float, float]] = field(default_factory=list)

    def tick(self) -> tuple[int, float]:
        self.sequence += 1
        self.now_s += 1.0 / self.fps
        return (self.sequence, self.now_s)

    @property
    def blocked_forward(self) -> bool:
        return self.obstacle is not None and not self.obstacle.cleared

    def apply(self, *, forward: int, lateral: int, jump: bool, turn_deg: float) -> None:
        if turn_deg:
            self.pending.append((self.now_s + MEASURED.latency_s, turn_deg))
        landed = [entry for entry in self.pending if entry[0] <= self.now_s]
        self.pending = [entry for entry in self.pending if entry[0] > self.now_s]
        for _, degrees in landed:
            self.error_deg = wrap_deg(self.error_deg - degrees)
        self.applied_forward = forward
        self.applied_lateral = lateral
        if self.obstacle is not None:
            self.obstacle.attempt(
                forward=forward,
                strafe=lateral,
                jump=jump,
                delta_ms=1000.0 / self.fps,
            )
        if forward > 0 and not self.blocked_forward:
            self.forward_travelled += OPEN_SPEED / self.fps

    def motion(self) -> MotionObservation:
        moving = self.applied_forward > 0 and not self.blocked_forward
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
    forward_down_edges: int
    forward_up_edges: int
    errors_while_walking: list[float] = field(default_factory=list)
    #: Every lateral axis recovery asked for, grouped by recovery episode.
    laterals: list[list[int]] = field(default_factory=list)
    jumps: int = 0
    obstacle_cleared: bool = False

    @property
    def terminated(self) -> bool:
        return self.final.terminal

    @property
    def misalignment_fraction(self) -> float:
        return self.misaligned_ticks / max(1, self.walking_ticks)

    @property
    def duty_cycle(self) -> float:
        return self.walking_ticks / max(1, self.ticks)

    def alignment_percentile(self, fraction: float) -> float:
        values = sorted(self.errors_while_walking)
        if not values:
            return 0.0
        return values[min(len(values) - 1, int(fraction * len(values)))]


def drive(
    world: World,
    *,
    ticks: int = 900,
    stop_on_terminal: bool = True,
    limits: SteeringLimits | None = None,
) -> RouteResult:
    """Run the real navigator against the world for a bounded number of ticks.

    The feedback loop is the production one: whatever the decision says should
    be held is fed straight back through :meth:`Navigator.note_held`, exactly
    as the live worker feeds it the actuator's own ledger. Nothing in here
    primes the progress guard by hand.
    """
    navigator = Navigator(
        capabilities=CAPABLE,
        follower=ArrowFollowerController(limits, MEASURED),
        progress=ProgressGuard(
            WALKING, ProgressConfig(suspect_after_ms=300, min_applied_forward_ms=200)
        ),
    )
    navigator.note_health(focus_ok=True, processed_fps=world.fps)
    held: set[str] = set()
    phases: list[NavigationPhase] = []
    misaligned = 0
    walking = 0
    down_edges = 0
    up_edges = 0
    errors: list[float] = []
    laterals: list[list[int]] = []
    jumps = 0
    in_recovery = False
    holding_forward = False
    duplicate_of: tuple[int, float] | None = None

    for _ in range(ticks):
        sequence, now = world.tick()
        if sequence in world.duplicate_frames and duplicate_of is not None:
            sequence, now = duplicate_of
        else:
            duplicate_of = (sequence, now)
        frame = _frame(sequence, now)
        inputs = NavigationInputs(
            frame=frame,
            arrow=world.arrow(),
            direction=world.direction(),
            motion=world.motion(),
            arrival=None,
            forward_commanded=world.applied_forward > 0,
        )
        decision = navigator.decide(inputs, generation=1, now_s=now)
        phases.append(decision.phase)
        was_recovering, in_recovery = (
            in_recovery,
            decision.phase is NavigationPhase.RECOVERY,
        )
        if in_recovery and not was_recovering:
            laterals.append([])

        movement = decision.movement
        held = {key.value for key in movement.keys}
        # The production feedback wire: what is *held*, not what was asked for.
        navigator.note_held(sorted(held), now_s=now, yaw_posted_px=movement.yaw_px)

        forward_now = movement.forward > 0
        if forward_now and not holding_forward:
            down_edges += 1
        if holding_forward and not forward_now:
            up_edges += 1
        holding_forward = forward_now

        if in_recovery:
            if movement.strafe:
                laterals[-1].append(movement.strafe)
            if movement.jump:
                jumps += 1

        turn_deg = movement.yaw_px * MEASURED.degrees_per_unit
        world.apply(
            forward=movement.forward,
            lateral=movement.strafe,
            jump=movement.jump,
            turn_deg=turn_deg,
        )
        if forward_now:
            walking += 1
            errors.append(abs(world.error_deg))
            if abs(world.error_deg) > 15.0:
                misaligned += 1

        if stop_on_terminal and decision.phase.terminal:
            held = set()
            break

    return RouteResult(
        ticks=len(phases),
        phases=phases,
        final=phases[-1],
        held=held,
        forward_travelled=world.forward_travelled,
        misaligned_ticks=misaligned,
        walking_ticks=walking,
        forward_down_edges=down_edges,
        forward_up_edges=up_edges,
        errors_while_walking=errors,
        laterals=laterals,
        jumps=jumps,
        obstacle_cleared=world.obstacle.cleared if world.obstacle else False,
    )


CADENCES = [30.0, 60.0, 90.0, 120.0]


# ---------------------------------------------------------------------------
# Open ground
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fps", CADENCES)
@pytest.mark.parametrize("initial", [0.0, 30.0, -30.0, 90.0, -90.0, 170.0, -170.0])
def test_the_navigator_reaches_the_arrow_from_any_heading(fps: float, initial: float) -> None:
    world = World(error_deg=initial, fps=fps)
    result = drive(world, ticks=int(fps * 10))

    assert result.walking_ticks > 0, f"never started walking from {initial:+.0f} degrees"
    assert abs(world.error_deg) <= 15.0, f"settled at {world.error_deg:+.1f}"
    assert result.forward_travelled > 0.0


@pytest.mark.parametrize("fps", CADENCES)
def test_a_straight_route_is_one_press_and_no_rattle(fps: float) -> None:
    """The mission's first acceptance target, and the one the old controller
    could not meet: thirty seconds of straight walking, one W down, one W up."""
    world = World(error_deg=0.0, fps=fps)
    result = drive(world, ticks=int(fps * 30))

    assert result.forward_down_edges == 1, (
        f"{result.forward_down_edges} presses on a straight route"
    )
    assert result.forward_up_edges == 0, "the hold was interrupted"
    assert result.duty_cycle > 0.99


@pytest.mark.parametrize("fps", CADENCES)
@pytest.mark.parametrize("initial", [0.0, 25.0, -40.0, 60.0])
def test_an_ordinary_route_keeps_the_forward_duty_cycle_high(
    fps: float, initial: float
) -> None:
    """At least 85-90% during valid follow periods."""
    world = World(error_deg=initial, fps=fps)
    result = drive(world, ticks=int(fps * 10))

    assert result.duty_cycle >= 0.90, f"walked only {result.duty_cycle:.0%} of the time"
    assert result.forward_down_edges <= 2


@pytest.mark.parametrize("fps", CADENCES)
def test_a_moderate_turn_never_lets_go_of_the_forward_hold(fps: float) -> None:
    world = World(error_deg=55.0, fps=fps)
    result = drive(world, ticks=int(fps * 8))

    assert result.forward_up_edges == 0, "a moderate turn dropped W"
    assert NavigationPhase.CORRECT in result.phases, "it never corrected while walking"
    assert NavigationPhase.ALIGN not in result.phases, "a moderate turn stood still"


@pytest.mark.parametrize("fps", CADENCES)
def test_only_a_target_behind_the_character_earns_a_stationary_pivot(
    fps: float,
) -> None:
    world = World(error_deg=175.0, fps=fps)
    result = drive(world, ticks=int(fps * 10))

    assert NavigationPhase.ALIGN in result.phases, "a target behind us never pivoted"
    assert result.phases[0] is not NavigationPhase.ALIGN, "one frame stopped it dead"
    assert abs(world.error_deg) <= 15.0


@pytest.mark.parametrize("fps", CADENCES)
def test_alignment_stays_inside_the_acceptance_targets(fps: float) -> None:
    """Median under ten degrees, p95 under twenty-five, once the opening hard
    turn is past - which is the exclusion the mission states."""
    world = World(error_deg=45.0, fps=fps)
    result = drive(world, ticks=int(fps * 12))
    settled = RouteResult(
        **{
            **vars(result),
            "errors_while_walking": result.errors_while_walking[int(fps * 3) :],
        }
    )

    assert settled.alignment_percentile(0.5) < 10.0
    assert settled.alignment_percentile(0.95) < 25.0


@pytest.mark.parametrize("fps", CADENCES)
def test_open_ground_is_never_mistaken_for_an_obstacle(fps: float) -> None:
    """The mission's false-stuck target: under one per cent of open-ground
    ticks may enter contact or recovery."""
    world = World(error_deg=10.0, fps=fps)
    result = drive(world, ticks=int(fps * 20))

    false_stuck = sum(
        1
        for phase in result.phases
        if phase in (NavigationPhase.CONTACT, NavigationPhase.RECOVERY)
    )
    assert false_stuck / result.ticks < 0.01, (
        f"{false_stuck} of {result.ticks} open-ground ticks looked stuck"
    )


# ---------------------------------------------------------------------------
# Degraded perception
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loss_ms", [100, 500, 1000, 2000])
@pytest.mark.parametrize("fps", CADENCES)
def test_an_occlusion_inside_the_grace_costs_no_forward_releases(
    loss_ms: int, fps: float
) -> None:
    """Traces from this repository hold healthy losses of 0.7 to 2.65 seconds
    behind foliage. Every one of them used to stop the character."""
    start = int(fps * 2)
    frames = int(loss_ms / 1000.0 * fps)
    world = World(error_deg=5.0, fps=fps, lost_frames=frozenset(range(start, start + frames)))
    result = drive(world, ticks=int(fps * 8))

    assert result.forward_up_edges == 0, f"{loss_ms} ms of occlusion released the walk"
    assert NavigationPhase.COAST in result.phases


@pytest.mark.parametrize("fps", CADENCES)
def test_a_loss_past_the_grace_searches_while_still_moving(fps: float) -> None:
    limits = SteeringLimits()
    start = int(fps * 2)
    world = World(error_deg=5.0, fps=fps, lost_frames=frozenset(range(start, 100_000)))
    result = drive(world, ticks=int(fps * (2 + limits.coast_grace_s + 3)))

    assert NavigationPhase.SEARCH in result.phases, "it stopped instead of looking"
    searching = [
        index for index, phase in enumerate(result.phases) if phase is NavigationPhase.SEARCH
    ]
    assert searching, "no search frames at all"


@pytest.mark.parametrize("fps", CADENCES)
def test_a_search_always_terminates_inside_its_budget(fps: float) -> None:
    limits = SteeringLimits()
    start = int(fps * 1)
    world = World(error_deg=5.0, fps=fps, lost_frames=frozenset(range(start, 100_000)))
    budget_ticks = int(fps * (1 + limits.search_budget_s + 3))
    result = drive(world, ticks=budget_ticks)

    assert result.final is NavigationPhase.ABANDONED, "the search never gave up"
    assert result.held == set()


@pytest.mark.parametrize("fps", CADENCES)
def test_a_reappearing_arrow_resumes_without_a_stationary_reacquisition(
    fps: float,
) -> None:
    start = int(fps * 2)
    end = start + int(fps * 3)
    world = World(error_deg=5.0, fps=fps, lost_frames=frozenset(range(start, end)))
    result = drive(world, ticks=int(fps * 10))

    after = result.phases[end + 3 :]
    assert NavigationPhase.FOLLOW in after or NavigationPhase.CORRECT in after, (
        "the route never came back after the arrow did"
    )
    assert NavigationPhase.ACQUIRE not in after, "it re-acquired a target it never lost"


def test_duplicate_frames_do_not_manufacture_a_correction() -> None:
    world = World(error_deg=20.0, duplicate_frames=frozenset(range(10, 60)))
    result = drive(world, ticks=200)

    assert result.walking_ticks > 0, "the route recovered once frames advanced"
    assert result.forward_up_edges == 0, "a repeated frame dropped the walk"


def test_a_false_similar_candidate_costs_nothing() -> None:
    """A latch, not a lock: ten frames of a decoy must not steal the route."""
    world = World(error_deg=0.0, decoy_frames=frozenset(range(60, 70)))
    result = drive(world, ticks=300)

    assert result.walking_ticks > 0
    assert abs(world.error_deg) <= 20.0, f"the decoy pulled the route to {world.error_deg:+.1f}"
    assert result.forward_up_edges == 0


# ---------------------------------------------------------------------------
# Contact and recovery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fps", CADENCES)
def test_a_curb_is_cleared_by_a_running_jump(fps: float) -> None:
    world = World(error_deg=0.0, fps=fps, obstacle=Obstacle(clears_with_jump=True))
    result = drive(world, ticks=int(fps * 12), stop_on_terminal=False)

    assert result.obstacle_cleared, "the running jump never got over the curb"
    assert result.jumps >= 1
    assert result.final is not NavigationPhase.ABANDONED
    assert NavigationPhase.FOLLOW in result.phases[-int(fps * 2) :], (
        "it cleared the curb and never went back to walking"
    )


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("fps", [30.0, 60.0, 90.0])
def test_a_bush_is_cleared_by_a_forward_arc(side: int, fps: float) -> None:
    world = World(
        error_deg=0.0,
        fps=fps,
        obstacle=Obstacle(clears_with_side=side, side_ms=250.0),
        blocked_sides=(-side,),
    )
    result = drive(world, ticks=int(fps * 14), stop_on_terminal=False)

    assert result.obstacle_cleared, f"a bush passable on the {side} side was never cleared"
    assert result.final is not NavigationPhase.ABANDONED


@pytest.mark.parametrize("fps", CADENCES)
def test_a_wall_abandons_within_its_budget_and_holds_nothing(fps: float) -> None:
    world = World(error_deg=0.0, fps=fps, obstacle=Obstacle(), blocked_sides=(-1, 1))
    result = drive(world, ticks=int(fps * 30))

    assert result.final is NavigationPhase.ABANDONED
    assert result.held == set(), f"a terminal state held {result.held}"


def test_recovery_maneuvers_keep_walking_rather_than_standing_still() -> None:
    world = World(error_deg=0.0, obstacle=Obstacle(), blocked_sides=(-1, 1))
    result = drive(world, ticks=1200)

    recovering = [
        index for index, phase in enumerate(result.phases) if phase is NavigationPhase.RECOVERY
    ]
    assert recovering, "contact was never detected"
    # Every rung but the deliberate back-out walks, so the vast majority of
    # recovery ticks hold W. The old ladder held it on none of them.
    assert result.duty_cycle > 0.7


def test_recovery_does_not_wiggle_within_an_episode() -> None:
    world = World(error_deg=0.0, obstacle=Obstacle(), blocked_sides=(-1,))
    result = drive(world, ticks=1200, stop_on_terminal=False)

    episodes = [episode for episode in result.laterals if episode]
    assert episodes, "recovery changed a label but emitted no strafe"
    for episode in episodes:
        runs = sum(1 for a, b in pairwise(episode) if a != b)
        assert runs <= 3, f"the detour side changed {runs} times inside one episode"


def test_no_recovery_episode_exceeds_its_jump_budget() -> None:
    from prospector_engine.navigation import RecoveryBudget

    world = World(error_deg=0.0, obstacle=Obstacle(), blocked_sides=(-1, 1))
    result = drive(world, ticks=1200)

    assert result.jumps <= RecoveryBudget().max_jumps * 4, (
        f"{result.jumps} jumps across the route"
    )


# ---------------------------------------------------------------------------
# The invariants, over every scenario
# ---------------------------------------------------------------------------

SCENARIOS: dict[str, World] = {
    "straight": World(error_deg=0.0),
    "turn-30": World(error_deg=30.0),
    "turn-90": World(error_deg=-90.0),
    "turn-170": World(error_deg=170.0),
    "arrow-loss": World(error_deg=0.0, lost_frames=frozenset(range(30, 90))),
    "arrow-gone": World(error_deg=0.0, lost_frames=frozenset(range(30, 100_000))),
    "decoy": World(error_deg=0.0, decoy_frames=frozenset(range(30, 45))),
    "duplicates": World(error_deg=15.0, duplicate_frames=frozenset(range(20, 80))),
    "curb": World(error_deg=0.0, obstacle=Obstacle(clears_with_jump=True)),
    "bush-left": World(
        error_deg=0.0, obstacle=Obstacle(clears_with_side=-1), blocked_sides=(1,)
    ),
    "bush-right": World(
        error_deg=0.0, obstacle=Obstacle(clears_with_side=1), blocked_sides=(-1,)
    ),
    "wall": World(error_deg=0.0, obstacle=Obstacle(), blocked_sides=(-1, 1)),
}


def _scenario(name: str, fps: float) -> World:
    world = SCENARIOS[name]
    return replace(
        world,
        fps=fps,
        obstacle=replace(world.obstacle) if world.obstacle is not None else None,
    )


@pytest.mark.parametrize("name", sorted(SCENARIOS))
@pytest.mark.parametrize("fps", CADENCES)
def test_every_scenario_holds_no_input_when_it_stops(name: str, fps: float) -> None:
    result = drive(_scenario(name, fps), ticks=int(fps * 20))

    if result.terminated:
        assert result.held == set(), f"{name} at {fps:.0f} fps held {result.held}"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_no_scenario_runs_forever_in_recovery(name: str) -> None:
    result = drive(_scenario(name, 60.0), ticks=2400, stop_on_terminal=False)

    tail = result.phases[-300:]
    assert not all(phase is NavigationPhase.RECOVERY for phase in tail), (
        f"{name} was still recovering after {result.ticks} ticks"
    )


@pytest.mark.parametrize("name", sorted(SCENARIOS))
@pytest.mark.parametrize("fps", CADENCES)
def test_no_scenario_rattles_the_forward_hold(name: str, fps: float) -> None:
    """The failure that is invisible to every other assertion here: a route
    that arrives, holds nothing at the end, and stuttered the whole way."""
    result = drive(_scenario(name, fps), ticks=int(fps * 20))

    seconds = result.ticks / fps
    assert result.forward_down_edges <= max(4, seconds / 2), (
        f"{name} at {fps:.0f} fps pressed W {result.forward_down_edges} times "
        f"in {seconds:.0f} seconds"
    )
