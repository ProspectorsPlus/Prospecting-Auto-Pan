"""Navigation FSM, the follower, bounded recovery, and the mode workers.

Three rules run through this whole module.

* **No transition is justified by elapsed time alone.** Time can expire an
  action; it can never prove collision, movement, arrival, or success.
* **Within one update the event priority is fixed**: safety/cancellation ->
  credible arrival -> contact/recovery -> ordinary steering. The first credible
  arrival candidate releases movement immediately (plan 6.2).
* **Capability is derived from this run, not declared once.**
  :class:`NavigationCapabilities` is computed from what automatic setup
  actually observed and measured - a stable reference, a confirmed control
  mode, a characterized turn actuator, a sampled locomotion baseline. There is
  no frozen table of gates that production code cannot move, because that is
  what made Live unreachable: every gate was PENDING and nothing was allowed to
  set one.

Offline evidence has not gone anywhere; it has moved to where it belongs.
Detector corpus metrics and per-profile qualification are build-time facts
about the software and live in ``--detector-report`` and STATUS.md. What gates
a *session* is what this session can see.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from prospector_engine.acceptance import (
    AcceptanceConfig,
    AcceptanceResult,
    ForwardMotionWitness,
    ForwardRequest,
    InputAcceptanceProbe,
    MotionSample,
)
from prospector_engine.arrow import (
    ArrowDetector,
    DetectorConfig,
    DirectionEstimator,
    ProposalSet,
    ProposalStats,
    TrackState,
    present,
)
from prospector_engine.contracts import (
    ArrivalObservation,
    ArrowCandidateRecord,
    ArrowObservation,
    CapturedFrame,
    CommandKind,
    CommandOutcome,
    CommandVisualization,
    ControlState,
    CueReading,
    DiagnosticObservation,
    DirectionObservation,
    EvidenceStatus,
    InputKey,
    ModeResult,
    ModeResultKind,
    MotionObservation,
    NavigationApplyResult,
    NavigationCommand,
    NavigationPhase,
    Provenance,
    PursuitTelemetry,
    RuntimeKey,
    monotonic_s,
)
from prospector_engine.coordinator import WorkerContext
from prospector_engine.lifecycle import LifecycleStage
from prospector_engine.motion import (
    UNCALIBRATED_BASELINE,
    ContactConfig,
    LocomotionBaseline,
    ProgressGuard,
    ProgressState,
    RuntimeBaselineEstimator,
    estimate_lk_affine,
)
from prospector_engine.movement import IDLE, DesiredMovement, MovementOutcome
from prospector_engine.steering import (
    ArrowFollowerController,
    ControlDecision,
    SteeringInputs,
)
from prospector_engine.trace import FrameTrace, PerceptionTiming
from prospector_engine.traversability import TraversabilityMemory
from prospector_engine.turning import TurnBackend, TurnResponse
from prospector_engine.vision import (
    ArrivalDetector,
    ArrowProfile,
    ArrowSegmenter,
    ArrowTracker,
    ProfileAuthority,
    wrap_deg,
)

__all__ = [
    "MotionConfig",
    "NavigationCapabilities",
    "Navigator",
    "RecoveryBudget",
    "RecoveryLadder",
    "RecoveryMove",
    "RecoveryRung",
    "describe_decision",
    "make_forward_probe_worker",
    "make_live_worker",
    "make_shadow_worker",
]


#: Rejection reasons rendered as something a person can act on. Anything not
#: listed falls back to the raw reason, which is still readable - the point is
#: that the common cases read as English, not that the vocabulary is closed.
_PLAIN_REJECTIONS: dict[str, str] = {
    "no-candidate": "No arrow visible",
    "ambiguous-candidates": "Direction uncertain - two candidates score alike",
    "candidate-clipped": "Arrow is cut off at the edge of the view",
    "viewport-invalid": "Roblox window is not usable right now",
    "unsupported-viewport-size": "This viewport size is not supported by the profile",
    "shape": "Candidate rejected - terrain-like shape",
    "contrast": "Candidate rejected - no contrast against its surroundings",
    "topology": "Candidate rejected - no arrowhead notches",
    "scale": "Candidate rejected - wrong size for an arrow",
    "margin": "Direction uncertain - candidates score too closely",
    "cues disagree": "Direction uncertain - detectors disagree",
    "polarity": "Direction uncertain - cannot tell which end is the tip",
}


def _plain_reason(reason: str | None) -> str:
    if not reason:
        return "No reading"
    head = reason.split(":", 1)[0]
    return _PLAIN_REJECTIONS.get(reason) or _PLAIN_REJECTIONS.get(head) or reason


def describe_decision(inputs: NavigationInputs, decision: NavigationDecision) -> str:
    """One sentence a person can act on, composed where the reasoning lives.

    It is built here rather than in the dashboard so the UI cannot paraphrase a
    decision into a different claim than the one the controller made.
    """
    if decision.phase is NavigationPhase.RECOVERY and decision.recovery is not None:
        return f"Recovering - {decision.recovery.description}"
    if decision.phase is NavigationPhase.CONTACT:
        return "Something is in the way - stopping to look"
    if not inputs.arrow.valid:
        return _plain_reason(inputs.arrow.abstain_reason)
    direction = inputs.direction
    if not direction.valid or direction.error_deg is None:
        return _plain_reason(direction.abstain_reason)
    error = wrap_deg(direction.error_deg)
    command = decision.command
    if command is not None and command.forward_axis == 1 and not command.turns:
        return "Aligned - move forward"
    if abs(error) < 1.0:
        return "Aligned - move forward"
    side = "right" if error > 0 else "left"
    return f"Turn {side} {abs(error):.0f} degrees"


# ---------------------------------------------------------------------------
# Capability
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NavigationCapabilities:
    """What this run has proven it can do, derived from live evidence.

    Every field is set by something that actually happened in this session:
    automatic setup verified the reference, the live prologue confirmed the
    control mode and measured the turn actuator, and the first seconds of
    walking sampled the locomotion baseline. Nothing here is a promise about a
    future experiment, and nothing here survives the session.
    """

    os_name: str
    profile_id: str
    #: The runtime reference check passed: the heading to the arrow held still.
    reference_ok: bool = False
    #: The locked-camera control mode was observed, not assumed.
    control_mode_ok: bool = False
    #: The measured turn actuator, or ``None`` before characterization.
    turn_response: TurnResponse | None = None
    #: Sampled from this session's own unobstructed walking.
    motion_baseline: LocomotionBaseline = UNCALIBRATED_BASELINE

    @classmethod
    def observing(cls, *, os_name: str, profile_id: str) -> NavigationCapabilities:
        """Shadow: the whole perception path, and no authority to steer."""
        return cls(os_name=os_name, profile_id=profile_id)

    @property
    def steering_enabled(self) -> bool:
        response = self.turn_response
        return self.reference_ok and self.control_mode_ok and bool(response and response.usable)

    @property
    def recovery_enabled(self) -> bool:
        """Whether this run may maneuver at all.

        It used to also require a matured locomotion baseline, and that turned
        out to be the wrong place for the question. The baseline only arrives
        after a dozen clean frames of unobstructed walking, so a route that met
        a bush in its first few seconds had recovery switched off and simply
        stopped - and the capability had no way to know that
        :class:`~prospector_engine.motion.ProgressGuard` now has a relative
        fallback that can answer "has this character's own speed collapsed"
        without one.

        So the split is: this says whether maneuvering is *permitted*, and the
        guard says whether there is evidence *right now*. The guard abstains
        honestly when there is not, which is the property that made the extra
        gate here redundant rather than protective.
        """
        return self.steering_enabled

    @property
    def progress_enabled(self) -> bool:
        """Whether a *frozen* baseline is in force, as opposed to a runtime or
        relative reference. Reported, never used to gate a maneuver."""
        return self.motion_baseline.usable

    @property
    def arrival_enabled(self) -> bool:
        """Arrival always *stops*. Stopping needs no gate; digging would."""
        return True

    def blocking_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.reference_ok:
            reasons.append("reference")
        if not self.control_mode_ok:
            reasons.append("control-mode")
        response = self.turn_response
        if response is None or not response.usable:
            reasons.append("turn-actuator")
        return tuple(reasons)

    def explain(self) -> tuple[str, ...]:
        """The missing pieces, in language a person can act on."""
        wording = {
            "reference": "the direction to the arrow has not held still long enough",
            "control-mode": "the camera control mode has not been confirmed",
            "turn-actuator": "no way of turning the camera has been measured yet",
        }
        return tuple(wording[name] for name in self.blocking_reasons())

    def describe(self) -> str:
        if self.steering_enabled:
            response = self.turn_response
            backend = response.backend.label if response else "unknown"
            return f"ready to steer using {backend}"
        return "; ".join(self.explain()) or "not ready"


# ---------------------------------------------------------------------------
# Motion
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MotionConfig:
    """Where forward progress is measured, and how often.

    A central band of the client, avoiding the fixed HUD at the top and bottom
    and the window edges. It deliberately does *not* try to cut the arrow out:
    a rectangle cannot, and the estimator's own spatial-coverage requirement is
    the real defence - a fit whose inliers all sit on one small static object
    cannot reach the coverage floor, so it abstains rather than reporting that
    nothing moved.
    """

    left_fraction: float = 0.14
    right_fraction: float = 0.86
    top_fraction: float = 0.18
    bottom_fraction: float = 0.72
    #: Estimate motion at most this often. Flow is the most expensive thing in
    #: the tick and progress does not need every frame to be honest.
    min_interval_s: float = 0.05
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="TREASURE_NAVIGATION_PLAN.md section 7.4; mission section E",
            note="band chosen to avoid the fixed HUD; not fitted to labelled data",
        )
    )

    def roi_px(self, canonical_size_px: tuple[int, int]) -> tuple[int, int, int, int]:
        width, height = canonical_size_px
        x = int(width * self.left_fraction)
        y = int(height * self.top_fraction)
        right = int(width * self.right_fraction)
        bottom = int(height * self.bottom_fraction)
        return (x, y, max(16, right - x), max(16, bottom - y))


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryMove:
    """One bounded segment of a rung: a keyboard level, and how long to hold it.

    ``strafe`` and ``turn`` are expressed *relative to the episode's chosen
    side*: ``+1`` means the side the ladder locked onto and ``-1`` means the
    other one. The absolute axis is resolved once, at the moment a step is
    handed out, so a rung can be read without holding the side in your head.
    """

    duration_ms: int
    forward: int = 0
    strafe: int = 0
    turn: int = 0
    jump: bool = False
    label: str = ""


@dataclass(frozen=True)
class RecoveryRung:
    """One escalation of the ladder, as a short sequence of moves."""

    name: str
    description: str
    moves: tuple[RecoveryMove, ...]
    max_attempts: int = 1

    @property
    def duration_ms(self) -> int:
        return sum(move.duration_ms for move in self.moves)

    @property
    def jumps(self) -> int:
        return sum(1 for move in self.moves if move.jump)


#: Escalating, finite, and every rung a *maneuver a player would actually make*.
#:
#: The ladder this replaces started with two rungs that released the controls
#: and waited, and then offered ``A`` on its own, ``W`` on its own, and ``SPACE``
#: on its own. None of those is how anybody gets past a bush. A running jump
#: needs ``W`` and ``SPACE`` together; a hedge or a curb needs a forward *arc*,
#: which is ``W`` and a strafe and usually a little camera. Standing still and
#: strafing sideways into the same obstacle is a wiggle, not a detour.
RECOVERY_LADDER: tuple[RecoveryRung, ...] = (
    RecoveryRung(
        "R0",
        "hopping over it without breaking stride",
        (
            RecoveryMove(80, forward=1, jump=True, label="W + SPACE"),
            RecoveryMove(300, forward=1, label="W"),
        ),
    ),
    RecoveryRung(
        "R1",
        "arcing forward around it",
        (RecoveryMove(500, forward=1, strafe=1, turn=1, label="W + side + camera"),),
        max_attempts=2,
    ),
    RecoveryRung(
        "R2",
        "hopping the other way",
        (
            RecoveryMove(80, forward=1, strafe=-1, jump=True, label="W + other side + SPACE"),
            RecoveryMove(560, forward=1, strafe=-1, label="W + other side"),
        ),
    ),
    RecoveryRung(
        "R3",
        "backing out and going round",
        (
            RecoveryMove(300, forward=-1, turn=1, label="S + camera"),
            RecoveryMove(80, forward=1, strafe=1, jump=True, label="W + side + SPACE"),
            RecoveryMove(520, forward=1, strafe=1, label="W + side"),
        ),
    ),
    RecoveryRung(
        "R4",
        "taking the long way round",
        (RecoveryMove(850, forward=1, strafe=1, turn=1, label="W + side + camera"),),
    ),
)


@dataclass(frozen=True)
class RecoveryBudget:
    """Hard caps on one recovery episode. Exhaustion is a safe stop.

    Six separate ceilings rather than one, because they bound six different
    ways an episode can go wrong: running too long, holding keys too long,
    jumping repeatedly, reversing into something behind, flip-flopping between
    sides, and re-entering recovery in a loop.
    """

    total_time_ms: int = 9000
    total_input_ms: int = 6000
    max_jumps: int = 3
    max_reverse_ms: int = 700
    #: Once a side is chosen it is held for the episode. One evidence-backed
    #: flip is allowed; a second is a wiggle.
    side_flips_allowed: int = 1
    #: No two ``SPACE`` presses closer together than this.
    jump_cooldown_ms: int = 700
    #: Fresh frames of restored progress before the episode is called resolved.
    restore_frames: int = 3
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source=(
                "TREASURE_NAVIGATION_PLAN.md section 9.3; "
                "mission section 'advanced composite recovery'"
            ),
            note="budgets are chosen bounds; no route corpus has been used to fit them",
        )
    )


@dataclass(frozen=True)
class RecoveryStep:
    """What recovery wants held this tick, already bounded and side-resolved."""

    rung: RecoveryRung
    move: RecoveryMove
    side: int
    forward: int
    strafe: int
    turn: int
    jump: bool
    remaining_ms: int
    description: str

    @property
    def level(self) -> RecoveryRung:
        """The rung. Named ``level`` as well for the dashboards that say so."""
        return self.rung

    def describe(self) -> str:
        parts: list[str] = []
        if self.forward > 0:
            parts.append("W")
        elif self.forward < 0:
            parts.append("S")
        if self.strafe < 0:
            parts.append("A")
        elif self.strafe > 0:
            parts.append("D")
        if self.turn < 0:
            parts.append("<")
        elif self.turn > 0:
            parts.append(">")
        if self.jump:
            parts.append("JUMP")
        return " + ".join(parts) if parts else "nothing"


class RecoveryLadder:
    """A finite escalation with six caps and one sticky side.

    Three properties matter more than the rungs themselves:

    * **The side is sticky.** Choosing left on one frame and right on the next
      is a wiggle, not a detour. The side is locked when the episode begins and
      may be flipped once, and only on evidence.
    * **Success is measured, never assumed.** An episode resolves when progress
      has been observed again over ``restore_frames`` *fresh* frames and the
      heading has not rotted. Elapsed time only ever ends an episode in failure.
    * **A rung ends early when the evidence says so.** A maneuver that has
      already worked is not run to its planned duration, because the planned
      duration was a guess and the restored motion is a measurement.
    """

    def __init__(self, budget: RecoveryBudget | None = None) -> None:
        self._budget = budget or RecoveryBudget()
        self._index = len(RECOVERY_LADDER)
        self._attempts = 0
        self._started_s: float | None = None
        self._move_index = 0
        self._move_started_s: float | None = None
        self._input_ms = 0.0
        self._reverse_ms = 0.0
        self._jumps = 0
        self._last_jump_s: float | None = None
        self._side = 0
        self._flips = 0
        self._restored_frames = 0
        self._entry_error_deg: float | None = None
        self._entry_reason = ""
        self._outcome = ""

    # -- state ------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self._started_s is not None and not self.exhausted

    @property
    def exhausted(self) -> bool:
        return self._index >= len(RECOVERY_LADDER)

    @property
    def rung(self) -> RecoveryRung | None:
        return None if self.exhausted else RECOVERY_LADDER[self._index]

    @property
    def side(self) -> int:
        return self._side

    @property
    def input_ms(self) -> float:
        return self._input_ms

    @property
    def jumps(self) -> int:
        return self._jumps

    @property
    def budget(self) -> RecoveryBudget:
        return self._budget

    @property
    def entry_reason(self) -> str:
        return self._entry_reason

    @property
    def outcome(self) -> str:
        """Why the last episode ended: resolved, escalated out, or abandoned."""
        return self._outcome

    def elapsed_ms(self, now_s: float) -> float:
        started = self._started_s
        return 0.0 if started is None else max(0.0, (now_s - started) * 1000.0)

    # -- lifecycle --------------------------------------------------------
    def begin(
        self, now_s: float, *, side: int, error_deg: float | None, reason: str = ""
    ) -> None:
        """Start an episode with one locked side. Idempotent while active."""
        if self.active:
            return
        self._index = 0
        self._attempts = 0
        self._started_s = now_s
        self._move_index = 0
        self._move_started_s = now_s
        self._input_ms = 0.0
        self._reverse_ms = 0.0
        self._jumps = 0
        self._last_jump_s = None
        self._side = side if side in (-1, 1) else 1
        self._flips = 0
        self._restored_frames = 0
        self._entry_error_deg = error_deg
        self._entry_reason = reason
        self._outcome = ""

    def resolve(self, outcome: str = "") -> None:
        """End the episode. Called on measured progress, or on exhaustion."""
        self._index = len(RECOVERY_LADDER)
        self._started_s = None
        self._move_started_s = None
        self._restored_frames = 0
        if outcome:
            self._outcome = outcome

    reset = resolve

    def flip_side(self, reason: str) -> bool:
        """Swap the sticky side, at most ``side_flips_allowed`` times."""
        if self._flips >= self._budget.side_flips_allowed:
            return False
        self._flips += 1
        self._side = -self._side
        self._outcome = f"flipped side: {reason}"
        return True

    def over_budget(self, now_s: float) -> str | None:
        if self._started_s is None:
            return None
        budget = self._budget
        if self.elapsed_ms(now_s) > budget.total_time_ms:
            return "recovery ran out of time"
        if self._input_ms > budget.total_input_ms:
            return "recovery ran out of its input budget"
        if self._reverse_ms > budget.max_reverse_ms:
            return "recovery reversed as far as it is allowed to"
        return None

    def note_progress(self, *, progressing: bool, error_deg: float | None) -> bool:
        """Fold one fresh frame in. ``True`` when the episode may end happily.

        Restoration is counted over consecutive frames rather than taken from
        one, because a single frame of movement during a maneuver is exactly
        what a maneuver produces - the character sliding along the obstacle -
        and calling that success is how a ladder resolves into the same wall.
        """
        if not progressing:
            self._restored_frames = 0
            return False
        entry = self._entry_error_deg
        if (
            entry is not None
            and error_deg is not None
            and abs(wrap_deg(error_deg)) > abs(wrap_deg(entry)) + 25.0
        ):
            # Moving, but the maneuver has thrown the heading away.
            self._restored_frames = 0
            return False
        self._restored_frames += 1
        return self._restored_frames >= self._budget.restore_frames

    # -- the tick ---------------------------------------------------------
    def step(self, now_s: float, *, delta_s: float) -> RecoveryStep | None:
        """The maneuver for this tick, or ``None`` when the ladder is spent."""
        over = self.over_budget(now_s)
        if over is not None:
            self.resolve(over)
            self._index = len(RECOVERY_LADDER)
            return None
        rung = self.rung
        if rung is None or self._move_started_s is None:
            return None

        elapsed_ms = (now_s - self._move_started_s) * 1000.0
        move = rung.moves[self._move_index]
        while elapsed_ms >= move.duration_ms:
            elapsed_ms -= move.duration_ms
            if not self._advance(now_s - elapsed_ms / 1000.0):
                return None
            rung = self.rung
            if rung is None:
                return None
            move = rung.moves[self._move_index]

        step_ms = max(0.0, delta_s) * 1000.0
        if move.forward or move.strafe or move.turn or move.jump:
            self._input_ms += step_ms
        if move.forward < 0:
            self._reverse_ms += step_ms

        jump = move.jump and self._jump_allowed(now_s)
        if jump:
            self._jumps += 1
            self._last_jump_s = now_s
        return RecoveryStep(
            rung=rung,
            move=move,
            side=self._side,
            forward=move.forward,
            strafe=move.strafe * self._side,
            turn=move.turn * self._side,
            jump=jump,
            remaining_ms=max(0, int(move.duration_ms - elapsed_ms)),
            description=f"{rung.description} ({move.label or rung.name})",
        )

    def _jump_allowed(self, now_s: float) -> bool:
        """One ``SPACE`` per rung segment, never inside the cooldown.

        A jump that fires every frame is not a jump, it is a held space bar
        with extra steps, and the character never leaves the ground.
        """
        if self._jumps >= self._budget.max_jumps:
            return False
        last = self._last_jump_s
        return last is None or (now_s - last) * 1000.0 >= self._budget.jump_cooldown_ms

    def _advance(self, now_s: float) -> bool:
        """Move to the next segment, or the next rung. ``False`` when spent."""
        rung = self.rung
        if rung is None:
            return False
        self._move_started_s = now_s
        if self._move_index + 1 < len(rung.moves):
            self._move_index += 1
            return True
        self._move_index = 0
        self._attempts += 1
        if self._attempts < rung.max_attempts:
            return True
        self._attempts = 0
        self._index += 1
        self._outcome = f"{rung.name} did not restore movement"
        return not self.exhausted


# ---------------------------------------------------------------------------
# Navigator
# ---------------------------------------------------------------------------


@dataclass
class NavigationInputs:
    """One tick's worth of perception, already derived from one frame."""

    frame: CapturedFrame
    arrow: ArrowObservation
    direction: DirectionObservation
    motion: MotionObservation | None
    arrival: ArrivalObservation | None
    forward_commanded: bool


@dataclass(frozen=True)
class NavigationDecision:
    """What the keyboard should look like when this tick is over, and why.

    ``movement`` is the whole instruction and it is a *level*: the caller hands
    it to the input authority and the actuator works out which edges that
    implies. ``command`` is the same thing in the shape the overlay and the
    trace already speak, present only when this tick had new evidence to
    justify it.

    ``release`` is not "hold nothing". It means *let go and forget* - drop the
    level and the controller's memory of what it was pursuing - and it is
    reserved for safety, terminal states, and world changes. An ordinary
    transition between FOLLOW, CORRECT, COAST, SEARCH and RECOVERY sets a new
    level and keeps every piece of memory, which is the difference between
    stepping round a bush and starting the route again.
    """

    phase: NavigationPhase
    command: NavigationCommand | None
    reason: str
    release: bool = False
    recovery: RecoveryStep | None = None
    movement: DesiredMovement = IDLE
    telemetry: PursuitTelemetry | None = None

    @property
    def holds_forward(self) -> bool:
        return self.movement.forward > 0


#: The navigation FSM and the follower describe the same run from two angles:
#: one in lifecycle terms, one in "what is held right now" terms. The mapping is
#: explicit so the two can never drift into disagreeing.
_CONTROL_TO_PHASE: dict[ControlState, NavigationPhase] = {
    ControlState.ACQUIRE: NavigationPhase.ACQUIRE,
    ControlState.ALIGN: NavigationPhase.ALIGN,
    ControlState.FOLLOW: NavigationPhase.FOLLOW,
    ControlState.CORRECT: NavigationPhase.CORRECT,
    ControlState.COAST: NavigationPhase.COAST,
    ControlState.SEARCH: NavigationPhase.SEARCH,
    ControlState.REACQUIRE: NavigationPhase.REACQUIRE,
    ControlState.BLOCKED: NavigationPhase.CONTACT,
    ControlState.SAFE_STOP: NavigationPhase.FAILED,
}

_PHASE_TO_CONTROL: dict[NavigationPhase, ControlState] = {
    NavigationPhase.ACQUIRE: ControlState.ACQUIRE,
    NavigationPhase.ALIGN: ControlState.ALIGN,
    NavigationPhase.FOLLOW: ControlState.FOLLOW,
    NavigationPhase.CORRECT: ControlState.CORRECT,
    NavigationPhase.COAST: ControlState.COAST,
    NavigationPhase.SEARCH: ControlState.SEARCH,
    NavigationPhase.REACQUIRE: ControlState.REACQUIRE,
    NavigationPhase.CONTACT: ControlState.BLOCKED,
    NavigationPhase.RECOVERY: ControlState.BLOCKED,
    NavigationPhase.ARRIVAL_CONFIRM: ControlState.ACQUIRE,
    NavigationPhase.ARRIVED: ControlState.SAFE_STOP,
    NavigationPhase.ABANDONED: ControlState.SAFE_STOP,
    NavigationPhase.FAILED: ControlState.SAFE_STOP,
}


def _movement_from(decision: ControlDecision) -> DesiredMovement:
    """The controller's level, in the vocabulary the actuator accepts."""
    return DesiredMovement(
        forward=decision.forward,
        strafe=decision.lateral,
        turn=decision.plan.turn_axis,
        jump=decision.jump,
        yaw_px=decision.plan.yaw_delta_px,
        reason=decision.reason,
    )


def _movement_from_recovery(step: RecoveryStep) -> DesiredMovement:
    return DesiredMovement(
        forward=step.forward,
        strafe=step.strafe,
        turn=step.turn,
        jump=step.jump,
        yaw_px=0,
        reason=step.description,
    )


class Navigator:
    """The navigation FSM. Pure decision logic - it emits no input itself.

    Both the Shadow observer and the Live worker drive the same instance of
    this class; the only difference is what they do with the returned level,
    and what :class:`NavigationCapabilities` says this run has proven.

    The event priority inside one update is fixed: safety and evidence
    validity, then a credible arrival, then contact and recovery, then ordinary
    pursuit. What changed from the version this replaces is what happens
    *between* those states. Moving from pursuit into recovery and back no
    longer runs through the release floor, so the heading filter, the target
    identity and the traversability memory all survive an obstacle. Only a
    fault, a terminal state or a changed world resets anything.
    """

    #: Consecutive arrival candidates before the run terminates. One frame of
    #: arrival evidence releases movement; three end the route.
    ARRIVAL_LATCHES = 3

    def __init__(
        self,
        *,
        capabilities: NavigationCapabilities,
        follower: ArrowFollowerController | None = None,
        recovery: RecoveryLadder | None = None,
        progress: ProgressGuard | None = None,
        terrain: TraversabilityMemory | None = None,
        max_evidence_age_ms: int = 100,
    ) -> None:
        self._capabilities = capabilities
        self._follower = follower or ArrowFollowerController(
            response=capabilities.turn_response
        )
        self._recovery = recovery or RecoveryLadder()
        self._progress = progress or ProgressGuard(capabilities.motion_baseline)
        self._terrain = terrain or TraversabilityMemory()
        self._max_evidence_age_ms = max_evidence_age_ms
        self._phase = NavigationPhase.ACQUIRE
        self._arrival_latches = 0
        self._last_tick_s: float | None = None
        self._last_recovery: RecoveryStep | None = None
        self._last_movement = IDLE
        self._held_keys: tuple[str, ...] = ()
        self._forward_held_ms = 0.0
        self._escalation = ""
        #: Runtime health the controller consults. Set by the live worker from
        #: the authority and the capture metrics, so the controller never has
        #: to reach for something that might be stale.
        self._focus_ok = True
        self._processed_fps = 999.0
        self._cursor_safe = True
        self._geometry_revision = 0
        self._profile_revision = 0

    # -- wiring -----------------------------------------------------------
    def note_health(
        self,
        *,
        focus_ok: bool,
        processed_fps: float,
        cursor_safe: bool = True,
        geometry_revision: int = 0,
        profile_revision: int = 0,
    ) -> None:
        """Refresh the runtime health the controller is allowed to see."""
        self._focus_ok = focus_ok
        self._processed_fps = processed_fps
        self._cursor_safe = cursor_safe
        self._geometry_revision = geometry_revision
        self._profile_revision = profile_revision

    def note_held(
        self,
        held: Iterable[Any],
        *,
        now_s: float,
        yaw_posted_px: int = 0,
        held_ms: float = 0.0,
    ) -> None:
        """Feed the *actuator's own ledger* into progress and stuck detection.

        This is the wire that was missing, and its absence is why nothing about
        obstacle recovery could ever work in production. The live worker called
        ``apply_command`` and then told the navigator nothing, so the
        applied-forward ledger stayed empty for the whole session: held
        duration was always zero, the locomotion baseline was never sampled
        from real walking, the progress guard abstained on every frame, and
        recovery was permanently disabled while looking, from the outside, like
        working code. Tests did not catch it because they called
        ``note_applied`` by hand.

        Every path calls this - applied, blocked, released - and every call
        passes what the actuator says is *physically held*, never what was
        requested. "The navigator asked for forward" and "the keyboard has W
        down" are different facts, and only the second one means the character
        is being told to walk.
        """
        keys = tuple(sorted(_key_name(key) for key in held))
        self._held_keys = keys
        self._forward_held_ms = held_ms if "w" in keys else 0.0
        self._progress.note_applied(now_s, forward="w" in keys)
        if yaw_posted_px or "left" in keys or "right" in keys:
            # A camera movement contaminates the next few motion estimates.
            # Told from what went out, not from what was planned.
            self._progress.note_yaw(now_s)

    def note_applied(self, result: NavigationApplyResult, *, now_s: float) -> None:
        """The authority-shaped form of :meth:`note_held`, kept for callers
        that hold a :class:`NavigationApplyResult` rather than an actuator
        outcome."""
        held = result.leases_held if result.applied else ()
        self.note_held(held, now_s=now_s)

    def note_released(self, *, now_s: float) -> None:
        self.note_held((), now_s=now_s)

    def adopt_capabilities(self, capabilities: NavigationCapabilities) -> None:
        """Adopt what the live prologue measured, mid-session."""
        self._capabilities = capabilities
        self._follower.set_response(capabilities.turn_response)
        self._progress.adopt_baseline(capabilities.motion_baseline)

    # -- observable -------------------------------------------------------
    @property
    def capabilities(self) -> NavigationCapabilities:
        return self._capabilities

    @property
    def controller(self) -> ArrowFollowerController:
        return self._follower

    @property
    def progress(self) -> ProgressGuard:
        return self._progress

    @property
    def recovery(self) -> RecoveryLadder:
        return self._recovery

    @property
    def terrain(self) -> TraversabilityMemory:
        return self._terrain

    @property
    def phase(self) -> NavigationPhase:
        return self._phase

    @property
    def control_state(self) -> ControlState:
        return _PHASE_TO_CONTROL.get(self._phase, ControlState.ACQUIRE)

    @property
    def arrival_latches(self) -> int:
        return self._arrival_latches

    @property
    def last_recovery(self) -> RecoveryStep | None:
        return self._last_recovery

    @property
    def held_keys(self) -> tuple[str, ...]:
        return self._held_keys

    # -- the tick ---------------------------------------------------------
    def decide(
        self, inputs: NavigationInputs, *, generation: int, now_s: float
    ) -> NavigationDecision:
        """One update, with the fixed event priority from plan 6.2."""
        frame = inputs.frame
        delta_s = 0.0 if self._last_tick_s is None else max(0.0, now_s - self._last_tick_s)
        self._last_tick_s = now_s
        limits = self._follower.limits

        # 1. Safety / evidence validity. A failed frame releases outright; a
        #    *slightly* stale one does not, because letting go of W and pressing
        #    it again on the next frame is the chatter the coast window exists
        #    to stop. Past the coast window it releases like anything else, and
        #    a genuinely dead pipeline is released by the actuator heartbeat and
        #    the deadman regardless of anything decided here.
        age_ms = frame.age_s(now_s) * 1000.0
        if frame.capture_error is not None:
            return self._release(
                NavigationPhase.REACQUIRE, f"capture-error:{frame.capture_error}"
            )
        if not frame.geometry.valid:
            return self._release(NavigationPhase.REACQUIRE, "viewport-invalid")
        if age_ms > self._max_evidence_age_ms + limits.stale_coast_ms:
            return self._release(NavigationPhase.REACQUIRE, f"stale-frame:{age_ms:.0f}ms")

        # 2. Arrival preempts recovery and steering.
        arrival = inputs.arrival
        if arrival is not None and arrival.valid:
            self._arrival_latches += 1
            if self._arrival_latches >= self.ARRIVAL_LATCHES:
                return self._release(NavigationPhase.ARRIVED, "arrival confirmed")
            return self._release(NavigationPhase.ARRIVAL_CONFIRM, "arrival candidate")
        self._arrival_latches = 0

        # 3. Progress and contact. The guard abstains until it has a reference,
        #    and abstention is never a reason to do anything.
        heading = self._heading_deg(inputs, now_s)
        verdict = self._progress.update(
            inputs.motion,
            now_s=now_s,
            commanded_heading_deg=heading,
        )
        self._learn_terrain(inputs, verdict, now_s, heading)
        if self._recovery.active:
            return self._continue_recovery(inputs, verdict, generation, now_s, delta_s)
        if verdict.recover:
            return self._begin_recovery(inputs, verdict, generation, now_s, delta_s, heading)

        # 4. Ordinary pursuit.
        if not self._capabilities.steering_enabled:
            reason = "; ".join(self._capabilities.explain()) or "not ready to steer"
            return self._release(NavigationPhase.ALIGN, f"observing only: {reason}")
        return self._steer(inputs, generation=generation, now_s=now_s)

    # -- helpers -----------------------------------------------------------
    def _heading_deg(self, inputs: NavigationInputs, now_s: float) -> float | None:
        """The best heading available, filtered first and raw as a fallback.

        The filter is the authority while it has anything, so the traversability
        memory and the recovery side-choice see the same angle the controller
        steered on rather than a second, noisier copy.
        """
        estimate = self._follower.heading.coast(now_s)
        if estimate is not None:
            return estimate.error_deg
        direction = inputs.direction
        if direction.valid and direction.error_deg is not None:
            return wrap_deg(direction.error_deg)
        return None

    def _learn_terrain(
        self,
        inputs: NavigationInputs,
        verdict: Any,
        now_s: float,
        heading: float | None,
    ) -> None:
        """Fold this frame's outcome into the sector memory.

        Only frames where something was genuinely being held count, and only
        the direction that was actually pushed - which is the commanded
        movement's own bearing, not the target's.
        """
        movement = self._last_movement
        if movement.forward <= 0:
            return
        bearing = 0.0
        if movement.strafe:
            bearing += 45.0 * movement.strafe
        if heading is not None and not movement.strafe:
            bearing += max(-30.0, min(30.0, heading))
        if verdict.state is ProgressState.PROGRESSING:
            self._terrain.reward(bearing, now_s)
        elif verdict.state in (
            ProgressState.NO_PROGRESS_SUSPECTED,
            ProgressState.NO_PROGRESS_CONFIRMED,
        ):
            self._terrain.penalize(bearing, now_s)

    # -- recovery ---------------------------------------------------------
    def _begin_recovery(
        self,
        inputs: NavigationInputs,
        verdict: Any,
        generation: int,
        now_s: float,
        delta_s: float,
        heading: float | None,
    ) -> NavigationDecision:
        """Contact is confirmed. Start a bounded episode - without stopping.

        The previous version released forward here and let the ladder open with
        two rungs that did nothing at all, so the visible behaviour of meeting a
        bush was: stop, stand still for 700 ms, and only then try something. The
        first rung is now a running hop, which needs ``W`` to still be down, so
        the transition into recovery deliberately does not go through a release.
        """
        if not self._capabilities.recovery_enabled:
            # Nothing measured can tell "stuck" from "slow". The only safe
            # answer is to stop pushing, not to invent a detour.
            self._escalation = "no way to tell stuck from slow; stopping instead"
            return self._release(NavigationPhase.CONTACT, verdict.reason)
        drift = None
        if inputs.motion is not None and inputs.motion.valid:
            drift = inputs.motion.lateral_speed_norm
        side, why = self._terrain.choose_side(
            now_s=now_s,
            target_error_deg=heading,
            lateral_drift_norm=drift,
            failed_side=0,
        )
        self._recovery.begin(now_s, side=side, error_deg=heading, reason=verdict.reason)
        self._escalation = f"contact confirmed; going {_side_word(side)} because {why}"
        self._follower.soften()
        return self._continue_recovery(inputs, verdict, generation, now_s, delta_s)

    def _continue_recovery(
        self,
        inputs: NavigationInputs,
        verdict: Any,
        generation: int,
        now_s: float,
        delta_s: float,
    ) -> NavigationDecision:
        """Drive one tick of the ladder, and keep the target memory alive.

        The controller is still fed every frame through :meth:`absorb`, so when
        the episode resolves the heading filter and the track identity are
        current and pursuit resumes moving rather than through a fresh
        stationary acquisition.
        """
        estimate = self._follower.absorb(self._steering_inputs(inputs, now_s))
        heading = None if estimate is None else estimate.error_deg
        progressing = verdict.state is ProgressState.PROGRESSING
        if self._recovery.note_progress(progressing=progressing, error_deg=heading):
            rung = self._recovery.rung
            outcome = f"{rung.name if rung else 'recovery'} restored movement"
            self._recovery.resolve(outcome)
            self._last_recovery = None
            self._escalation = outcome
            self._progress.reset()
            if heading is not None:
                self._terrain.reward(heading, now_s)
            # Straight back into pursuit, on the frame that proved it worked.
            # Releasing and re-acquiring here is what made every obstacle cost
            # a stationary restart.
            return self._steer(inputs, generation=generation, now_s=now_s)

        step = self._recovery.step(now_s, delta_s=delta_s)
        if step is None:
            reason = self._recovery.over_budget(now_s) or "recovery ladder exhausted"
            self._recovery.resolve(reason)
            self._escalation = reason
            return self._release(NavigationPhase.ABANDONED, reason)

        self._last_recovery = step
        self._phase = NavigationPhase.RECOVERY
        movement = _movement_from_recovery(step)
        command = self._command(
            generation,
            inputs.frame,
            now_s,
            movement=movement,
            reason=f"recovery {step.rung.name}: {step.description}",
            # Any movement axis at all - including the back-out rung's reverse -
            # is a movement command. ``ALIGN`` forbids every one of them, not
            # just a forward one.
            kind=CommandKind.FOLLOW if movement.forward else CommandKind.ALIGN,
        )
        return self._decision(
            NavigationPhase.RECOVERY,
            command,
            step.description,
            movement=movement,
            recovery=step,
            now_s=now_s,
            heading=estimate,
            verdict=verdict,
        )

    # -- steering ---------------------------------------------------------
    def _steering_inputs(self, inputs: NavigationInputs, now_s: float) -> SteeringInputs:
        frame = inputs.frame
        return SteeringInputs(
            arrow=inputs.arrow,
            direction=inputs.direction,
            frame_sequence=frame.sequence,
            frame_age_ms=frame.age_s(now_s) * 1000.0,
            now_s=now_s,
            focus_ok=self._focus_ok,
            viewport_ok=frame.geometry.valid,
            processed_fps=self._processed_fps,
            # The pointer only matters while it *is* the actuator. Holding an
            # arrow key does not drag anything out of the window, so a pointer
            # parked on the dashboard is not a fault there.
            cursor_safe=(
                True if self._follower.backend is TurnBackend.ARROW_KEYS else self._cursor_safe
            ),
            geometry_revision=self._geometry_revision,
            profile_revision=self._profile_revision,
        )

    def _steer(
        self, inputs: NavigationInputs, *, generation: int, now_s: float
    ) -> NavigationDecision:
        """Hand the frame to the follower and translate its answer.

        The follower decides; this only turns its decision into the level the
        input authority accepts. Splitting them means the control law can be
        exercised with no authority in sight, which is what the deterministic
        steering tests do.
        """
        decision = self._follower.update(self._steering_inputs(inputs, now_s))
        if decision.lost_target:
            return self._lost(inputs, now_s, decision.reason)
        if decision.release:
            return self._release(_CONTROL_TO_PHASE[decision.state], decision.reason)

        movement = _movement_from(decision)
        phase = _CONTROL_TO_PHASE[decision.state]
        self._phase = phase
        # A hold restates a level; it never mints a command. The frame that
        # would justify one has already authorized a command, and issuing a
        # second from it is exactly the lease renewal this forbids.
        command = (
            None
            if movement.idle or decision.held
            else self._command(
                generation,
                inputs.frame,
                now_s,
                movement=movement,
                reason=decision.reason,
                kind=decision.kind,
            )
        )
        return self._decision(
            phase,
            command,
            decision.reason,
            movement=movement,
            now_s=now_s,
            heading=decision.heading,
            verdict=None,
        )

    def _lost(self, inputs: NavigationInputs, now_s: float, reason: str) -> NavigationDecision:
        """The arrow is gone past the whole search budget. Stop, and say so.

        This used to start a recovery episode, on the theory that the ladder's
        second rung was "wait for a fresh view of the arrow". It no longer is:
        the ladder is a set of obstacle maneuvers, and driving into one because
        the *arrow* is missing would answer the wrong question. The controller
        has already searched for as long as its budget allows, so what is left
        is to end the run and name the reason.
        """
        del inputs
        del now_s
        self._escalation = reason
        return self._release(NavigationPhase.ABANDONED, reason)

    # -- assembling a decision ---------------------------------------------
    def _decision(
        self,
        phase: NavigationPhase,
        command: NavigationCommand | None,
        reason: str,
        *,
        movement: DesiredMovement,
        now_s: float,
        heading: Any = None,
        verdict: Any = None,
        recovery: RecoveryStep | None = None,
        release: bool = False,
    ) -> NavigationDecision:
        self._last_movement = movement
        return NavigationDecision(
            phase,
            command,
            reason,
            release=release,
            recovery=recovery,
            movement=movement,
            telemetry=self._telemetry(
                phase, movement, reason, now_s, heading=heading, verdict=verdict
            ),
        )

    def _telemetry(
        self,
        phase: NavigationPhase,
        movement: DesiredMovement,
        reason: str,
        now_s: float,
        *,
        heading: Any,
        verdict: Any,
    ) -> PursuitTelemetry:
        """One flat record of what this tick knew. Emitted every frame, logged
        only when it changes."""
        ladder = self._recovery
        rung = ladder.rung if ladder.active else None
        observation = getattr(verdict, "observation", None)
        return PursuitTelemetry(
            state=_PHASE_TO_CONTROL.get(phase, ControlState.ACQUIRE),
            phase=phase,
            held_keys=tuple(_glyph(name) for name in self._held_keys),
            wanted_keys=tuple(sorted(key.value.upper() for key in movement.keys)),
            reason=reason,
            track_id=self._follower.track_id,
            arrow_age_ms=(
                None if heading is None else float(getattr(heading, "age_s", 0.0)) * 1000.0
            ),
            error_deg=None if heading is None else float(heading.error_deg),
            raw_error_deg=None if heading is None else float(heading.raw_deg),
            heading_rate_deg_s=0.0 if heading is None else float(heading.rate_deg_s),
            heading_confidence=0.0 if heading is None else float(heading.confidence),
            heading_spread_deg=0.0 if heading is None else float(heading.spread_deg),
            speed_norm=(
                None if observation is None else _optional_float(observation.forward_speed_norm)
            ),
            baseline_norm=self._progress.baseline.min_forward_speed_norm,
            progress_ratio=self._progress.ratio,
            stall_ms=self._progress.stall_ms(now_s),
            forward_held_ms=self._forward_held_ms,
            search_elapsed_ms=self._follower.search_elapsed_s(now_s) * 1000.0,
            recovery_rung="" if rung is None else rung.name,
            recovery_side=ladder.side if ladder.active else 0,
            recovery_jumps=ladder.jumps,
            recovery_elapsed_ms=ladder.elapsed_ms(now_s),
            recovery_input_ms=ladder.input_ms,
            escalation=self._escalation,
            sectors=tuple(
                (sector.centre_deg, sector.cost) for sector in self._terrain.sectors(now_s)
            ),
        )

    def _release(self, phase: NavigationPhase, reason: str) -> NavigationDecision:
        """Let go, and forget. Safety, terminal states, and world changes only."""
        self._phase = phase
        self._follower.reset()
        if phase.terminal:
            self._recovery.resolve(reason)
            self._terrain.reset()
        return self._decision(
            phase, None, reason, movement=IDLE, now_s=monotonic_s(), release=True
        )

    def _command(
        self,
        generation: int,
        frame: CapturedFrame,
        now_s: float,
        *,
        movement: DesiredMovement,
        reason: str,
        kind: CommandKind = CommandKind.FOLLOW,
    ) -> NavigationCommand:
        lease_s = self._follower.limits.lease_renew_ms / 1000.0
        max_valid_s = frame.captured_at_s + self._max_evidence_age_ms / 1000.0
        return NavigationCommand(
            generation=generation,
            source_frame_sequence=frame.sequence,
            source_captured_at_s=frame.captured_at_s,
            forward_axis=movement.forward,  # type: ignore[arg-type]
            lateral_axis=movement.strafe,  # type: ignore[arg-type]
            jump=movement.jump,
            yaw_delta_px=movement.yaw_px,
            turn_axis=movement.turn,  # type: ignore[arg-type]
            issued_at_s=now_s,
            valid_until_s=max(now_s, min(now_s + lease_s, max_valid_s)),
            reason=reason,
            kind=kind,
        )


def _side_word(side: int) -> str:
    return {-1: "left", 1: "right"}.get(side, "straight on")


def _key_name(key: Any) -> str:
    """The lower-case target name, from an :class:`InputKey` or a bare string."""
    value = getattr(key, "value", key)
    return str(value).lower()


def _glyph(name: str) -> str:
    """The single symbol the overlay and the log both use for one held key."""
    return {"left": "<", "right": ">", "space": "JUMP"}.get(name, name.upper())


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


# ---------------------------------------------------------------------------
# Perception assembly
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenceFrame:
    """Where the player is on screen and which way "forward" points.

    Under the locked camera the character sits at a fixed place on screen and
    faces up it, which is what makes a screen anchor usable at all. That is a
    *hypothesis about the control mode*, not a labelling of the avatar's pivot,
    so it is never presented as VALIDATED: E-ANCHOR and E-FORWARD are offline
    labelling exercises and remain PENDING.

    What automatic setup does instead is check the weaker claim navigation
    actually depends on - that with this anchor the heading to the arrow holds
    still while the character does - and it records the measured jitter. A
    badly wrong anchor fails that check, and the left/right consistency
    requirement inside turn characterization is a second, independent look at
    the same question.
    """

    anchor_canonical_px: tuple[float, float] = (640.0, 430.0)
    forward_deg: float = 0.0
    source: str = "screen anchor under the locked camera; stability checked each run"
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="runtime reference check; E-ANCHOR / E-FORWARD remain PENDING",
            note="verified stable in this run; never claimed to be the true pivot",
        )
    )
    #: Peak-to-peak heading jitter measured by the runtime reference check.
    measured_jitter_deg: float | None = None

    @property
    def validated(self) -> bool:
        return self.provenance.status is EvidenceStatus.VALIDATED

    @property
    def checked(self) -> bool:
        """Whether this run measured the reference holding still."""
        return self.measured_jitter_deg is not None


@dataclass
class PerceptionResult:
    """One frame's perception, with the timings that produced it."""

    inputs: NavigationInputs
    candidates: tuple[ArrowCandidateRecord, ...]
    contour_px: tuple[tuple[int, int], ...]
    desired_deg: float | None
    cues: tuple[CueReading, ...]
    perception_ms: float
    #: Stage-by-stage cost and the tracker's verdict, for the frame trace.
    timing: PerceptionTiming | None = None


@dataclass
class PerceptionPipeline:
    """Detector, direction estimator, arrival detector for one profile.

    One frame, one temporal transaction: the detector may run a region pass
    and, on a *later* frame, a full-frame pass, but ``commit`` is called once
    per unique frame. A region miss schedules the global search for the next
    frame instead of running it synchronously on the same screenshot.

    Everything derived - direction, contour, tip and tail, score breakdown -
    comes from the single candidate the detector **selected**, never from the
    first candidate that merely cleared the threshold.
    """

    segmenter: ArrowSegmenter
    detector: ArrowDetector | None = None
    estimator: DirectionEstimator | None = None
    tracker: ArrowTracker = field(default_factory=ArrowTracker)
    arrival: ArrivalDetector = field(default_factory=ArrivalDetector)
    strategy: str = "topology_consensus"
    reference: ReferenceFrame = field(default_factory=ReferenceFrame)
    profiles: ProfileAuthority | None = None
    #: Estimate forward motion from consecutive frames. Off in Shadow, where
    #: nothing consumes it and optical flow is the most expensive thing in the
    #: tick; on in Live, where the progress guard depends on it.
    motion_enabled: bool = False
    motion_config: MotionConfig = field(default_factory=MotionConfig)
    #: Padding around a tracked arrow when searching only a region of interest.
    roi_padding_px: int = 220
    #: Force a full-frame pass this often even while a track holds, so a track
    #: cannot quietly follow the wrong thing forever (plan 8).
    full_frame_every: int = 20
    _frames_since_full: int = 0
    _roi_hits: int = 0
    _full_passes: int = 0
    _fallbacks: int = 0
    _skipped: int = 0
    _profile_revision: int = 1
    _geometry_identity: tuple[object, ...] | None = None
    _last_heading_deg: float | None = None
    _last_track_id: int | None = None
    _reversals_refused: int = 0
    _previous_frame: CapturedFrame | None = None
    _last_motion_s: float = 0.0
    _motion_abstentions: int = 0
    #: Per-candidate detectors used only while a profile is being chosen.
    _classifiers: dict[str, ArrowDetector] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.detector is None:
            self.detector = ArrowDetector(self.segmenter.profile, DetectorConfig())
        if self.estimator is None:
            self.estimator = DirectionEstimator(self.detector.config)

    def estimate_motion(self, frame: CapturedFrame) -> MotionObservation | None:
        """Forward/lateral speed between this frame and the previous one.

        Returns ``None`` - not an abstention - when motion is switched off or
        there is no coherent previous frame, so "we did not look" and "we
        looked and could not tell" stay distinguishable downstream.
        """
        if not self.motion_enabled:
            return None
        previous = self._previous_frame
        self._previous_frame = frame
        if previous is None or previous.sequence >= frame.sequence:
            return None
        if frame.captured_at_s - self._last_motion_s < self.motion_config.min_interval_s:
            return None
        self._last_motion_s = frame.captured_at_s
        roi = self.motion_config.roi_px(frame.canonical_size_px)
        try:
            observation = estimate_lk_affine(previous, frame, roi_px=roi)
        except Exception:
            # A flow failure is an abstention, never a crash in the tick that
            # is also holding a movement lease.
            self._motion_abstentions += 1
            return None
        if not observation.valid:
            self._motion_abstentions += 1
        return observation

    @property
    def motion_abstentions(self) -> int:
        return self._motion_abstentions

    #: How much of a profile's runtime score comes from *how clearly* its
    #: chosen candidate beat the runner-up rather than from raw confidence.
    #: Measured on the real-frame corpus: on sand frames the green-grass
    #: profile reaches almost the same confidence as the correct yellow one
    #: (0.60 vs 0.66) and would win half the frames, but its selection margin
    #: is a third of it (0.20 vs 0.56). Confidence says "something arrow-shaped
    #: is here"; margin says "and nothing else looks like it", which is the
    #: question profile identity actually asks (D-039).
    MARGIN_WEIGHT = 0.8

    def score_profiles(
        self, frame: CapturedFrame, candidates: tuple[ArrowProfile, ...]
    ) -> dict[str, float]:
        """One frame's evidence for each candidate profile.

        Each candidate gets its own detector, run over the same frame, and is
        scored on the confidence of the candidate it *selected* weighted by how
        far ahead of the runner-up that candidate scored. A profile that
        abstains scores zero, so "nothing matched" is a score rather than a
        missing key.
        """
        scores: dict[str, float] = {}
        for profile in candidates:
            detector = self._classifiers.get(profile.profile_id)
            if detector is None:
                detector = ArrowDetector(profile, DetectorConfig())
                self._classifiers[profile.profile_id] = detector
            try:
                proposals = detector.propose(frame, roi_px=None)
                outcome = detector.commit(frame, [proposals])
            except Exception:
                scores[profile.profile_id] = 0.0
                continue
            observation = outcome.observation
            if not observation.valid:
                scores[profile.profile_id] = 0.0
                continue
            margin = max(0.0, min(1.0, float(observation.score_margin)))
            clarity = (1.0 - self.MARGIN_WEIGHT) + self.MARGIN_WEIGHT * margin
            scores[profile.profile_id] = float(observation.confidence) * clarity
        return scores

    def forget_classifiers(self) -> None:
        """Drop the per-candidate detectors once a profile has been locked."""
        self._classifiers.clear()

    def set_profile(self, profile: ArrowProfile) -> None:
        """Swap the arrow profile and drop any track built from the old one."""
        config = self.detector.config if self.detector is not None else DetectorConfig()
        self.segmenter = ArrowSegmenter(profile)
        self.detector = ArrowDetector(profile, config)
        self.estimator = DirectionEstimator(config)
        self.tracker = ArrowTracker()
        self.arrival = ArrivalDetector()
        self._frames_since_full = self.full_frame_every  # force a full pass
        self._last_heading_deg = None
        self._last_track_id = None
        self._previous_frame = None

    @property
    def profile_revision(self) -> int:
        """Which profile generation the next observation will belong to."""
        if self.profiles is not None:
            return self.profiles.revision
        return self._profile_revision

    def _sync_profile(self) -> ArrowProfile | None:
        """Apply a staged profile swap. Called at the top of every frame."""
        if self.profiles is None:
            return None
        applied = self.profiles.apply_pending()
        if applied is not None:
            self.set_profile(applied)
        return applied

    def _sync_geometry(self, frame: CapturedFrame) -> bool:
        """Drop temporal state when the coordinate basis changes."""
        identity = frame.geometry.identity()
        if self._geometry_identity is not None and identity != self._geometry_identity:
            self.tracker = ArrowTracker()
            self.arrival = ArrivalDetector()
            if self.detector is not None:
                self.detector.reset()
            self._frames_since_full = self.full_frame_every
            self._geometry_identity = identity
            self._last_heading_deg = None
            self._last_track_id = None
            # Motion is measured between two frames in the *same* basis. A
            # resize invalidates the pair, not just the transform.
            self._previous_frame = None
            return True
        self._geometry_identity = identity
        return False

    @property
    def roi_hits(self) -> int:
        return self._roi_hits

    @property
    def full_passes(self) -> int:
        return self._full_passes

    @property
    def fallbacks(self) -> int:
        """Full-frame passes that ran because the previous region pass missed."""
        return self._fallbacks

    @property
    def reversals_refused(self) -> int:
        return self._reversals_refused

    @property
    def skipped_searches(self) -> int:
        """Frames observed without a search while nothing was held."""
        return self._skipped

    def _roi_for(self, frame: CapturedFrame) -> tuple[int, int, int, int] | None:
        """A search region around the predicted track, or ``None`` for full frame.

        ``None`` whenever the detector asks for a global search - no identity
        held, a region miss on the previous frame, or the periodic challenge -
        and on the pipeline's own full-frame cadence, so acquisition is never
        permanently confined to where the arrow used to be.
        """
        detector = self.detector
        if detector is None or detector.wants_global_search():
            return None
        if self._frames_since_full >= self.full_frame_every:
            return None
        predicted = detector.predicted_centroid()
        if predicted is None:
            return None
        width, height = frame.canonical_size_px
        scale = detector.predicted_scale_px() or 0.0
        pad = max(self.roi_padding_px, int(scale * 2.5))
        x = max(0, int(predicted[0]) - pad)
        y = max(0, int(predicted[1]) - pad)
        right = min(width, int(predicted[0]) + pad)
        bottom = min(height, int(predicted[1]) + pad)
        if right - x < 32 or bottom - y < 32:
            return None
        return (x, y, right - x, bottom - y)

    def set_strategy(self, strategy: str) -> None:
        """Retained for the E-DIR-IDEAL comparison harness."""
        self.strategy = strategy

    @property
    def profile(self) -> ArrowProfile:
        return self.segmenter.profile

    def analyze(
        self, frame: CapturedFrame, *, map_id: str, approach_valid: bool
    ) -> PerceptionResult:
        """Everything derived from one frame, in one temporal transaction."""
        started = monotonic_s()
        self._sync_profile()
        self._sync_geometry(frame)
        assert self.detector is not None and self.estimator is not None
        detector = self.detector

        roi_started = monotonic_s()
        roi = self._roi_for(frame)
        roi_proposal_ms = (monotonic_s() - roi_started) * 1000.0
        # A fallback is a full pass that ran because the detector asked for
        # one while an identity was held or being reacquired - the scheduled
        # replacement for the old synchronous second pass.
        fallback = (
            roi is None
            and detector.wants_global_search()
            and detector.state in (TrackState.TRACK, TrackState.AMBIGUOUS, TrackState.REACQUIRE)
        )
        if roi is None and not detector.search_due(frame.captured_at_s):
            # Nothing is held and the last full search found nothing: this
            # frame is observed, not searched. Latest-only, one frame of
            # acquisition latency at most, and no full pass for an empty view.
            proposals = ProposalSet((), ProposalStats("skipped", None, 0.0, 0, 0, 0, 0), None)
            outcome = detector.note_skipped(frame)
            self._skipped += 1
        else:
            proposals = detector.propose(frame, roi_px=roi)
            outcome = detector.commit(frame, [proposals])
            if roi is None:
                self._frames_since_full = 0
                self._full_passes += 1
                if fallback:
                    self._fallbacks += 1
            else:
                self._frames_since_full += 1
                self._roi_hits += 1

        arrow = outcome.observation
        selected = outcome.selected
        anchor = self.reference.anchor_canonical_px
        forward = self.reference.forward_deg
        # Polarity memory belongs to one identity: a new track id starts with
        # no remembered sign, so a genuinely different arrow is never forced
        # to agree with the last one.
        if arrow.track_id != self._last_track_id:
            self._last_heading_deg = None
        direction_started = monotonic_s()
        result = self.estimator.estimate(
            selected.features if selected is not None else None,
            anchor_px=anchor,
            forward_deg=forward,
            arrow_confidence=arrow.confidence,
            previous_heading_deg=self._last_heading_deg,
        )
        direction_ms = (monotonic_s() - direction_started) * 1000.0
        direction = result.observation
        if result.reversal_refused:
            self._reversals_refused += 1
        if selected is not None:
            arrow = replace(arrow, tip_px=result.tip_px, tail_px=result.tail_px)
        if arrow.valid and direction.valid and direction.error_deg is not None:
            self._last_heading_deg = wrap_deg(forward + direction.error_deg)
            self._last_track_id = arrow.track_id
        elif not arrow.valid:
            self._last_track_id = arrow.track_id

        desired_deg: float | None = None
        if arrow.valid and direction.valid and direction.error_deg is not None:
            desired_deg = wrap_deg(forward + direction.error_deg)

        contour = selected.features.contour_px if selected is not None else ()
        arrival = self.arrival.observe(frame, map_id=map_id, approach_valid=approach_valid)
        motion = self.estimate_motion(frame)
        inputs = NavigationInputs(
            frame=frame,
            arrow=arrow,
            direction=direction,
            motion=motion,
            arrival=arrival,
            forward_commanded=False,
        )
        stats = proposals.stats
        shown = present(outcome, detector.config.top_k)
        timing = PerceptionTiming(
            roi_used=roi is not None,
            roi_proposal_ms=roi_proposal_ms,
            roi_detector_ms=stats.elapsed_ms if roi is not None else 0.0,
            full_detector_ms=stats.elapsed_ms if roi is None else 0.0,
            fallback=fallback,
            raw_components=stats.raw_components,
            components_evaluated=stats.evaluated,
            mask_pixels_allocated=stats.mask_pixels,
            direction_ms=direction_ms,
            tracking_decision=outcome.decision,
            selected_candidate_id=selected.label if selected is not None else None,
            confidence=arrow.confidence,
            rejection_reasons=tuple(
                f"{h.label}:{h.reason}" for h in shown if h.state != "selected" and h.reason
            )[:8],
            track_state=outcome.state.value,
        )
        return PerceptionResult(
            inputs=inputs,
            candidates=tuple(h.as_record() for h in shown),
            contour_px=contour,
            desired_deg=desired_deg,
            cues=result.readings,
            perception_ms=(monotonic_s() - started) * 1000.0,
            timing=timing,
        )

    def observe(
        self, frame: CapturedFrame, *, map_id: str, approach_valid: bool
    ) -> NavigationInputs:
        return self.analyze(frame, map_id=map_id, approach_valid=approach_valid).inputs

    def diagnostic(
        self,
        frame: CapturedFrame,
        result: PerceptionResult,
        decision: NavigationDecision,
        *,
        decision_ms: float,
        key: RuntimeKey | None = None,
        control_state: ControlState | None = None,
        blockers: tuple[str, ...] = (),
        command_view: CommandVisualization | None = None,
    ) -> DiagnosticObservation:
        """Bind the frame, the geometry, and the decision into one value."""
        inputs = result.inputs
        stamped = key or RuntimeKey(
            run_id="local",
            coordinator_generation=0,
            mode_session_id=0,
            source_epoch=0,
            geometry_revision=0,
            profile_revision=self.profile_revision,
            frame_sequence=frame.sequence,
            content_id=frame.content_id,
        )
        return DiagnosticObservation(
            frame=frame,
            processed_at_s=frame.completed_at_s,
            published_at_s=monotonic_s(),
            key=stamped,
            profile_id=self.profile.profile_id,
            profile_status=self.profile.status.value,
            strategy_id=self.strategy,
            arrow=inputs.arrow,
            candidates=result.candidates,
            contour_px=result.contour_px,
            anchor_px=self.reference.anchor_canonical_px,
            forward_deg=self.reference.forward_deg,
            forward_source=self.reference.source,
            desired_deg=result.desired_deg,
            direction=inputs.direction,
            cues=result.cues,
            motion=inputs.motion,
            arrival=inputs.arrival,
            phase=decision.phase,
            command=decision.command,
            abstain_reason=(
                inputs.arrow.abstain_reason
                or inputs.direction.abstain_reason
                or (decision.reason if decision.release else None)
            ),
            command_view=command_view or CommandVisualization.none(),
            capture_ms=frame.duration_ms,
            perception_ms=result.perception_ms,
            decision_ms=decision_ms,
            control_state=control_state,
            plain_summary=describe_decision(inputs, decision),
            blockers=blockers,
            timing=result.timing,
            pursuit=decision.telemetry,
        )


# ---------------------------------------------------------------------------
# Mode workers
# ---------------------------------------------------------------------------


def _run_observer_loop(
    context: WorkerContext,
    pipeline: PerceptionPipeline,
    navigator: Navigator,
    *,
    map_id: str,
    approach_valid: bool,
    max_ticks: int | None,
    apply: Callable[[NavigationDecision, Any, PerceptionResult], CommandVisualization] | None,
) -> tuple[int, int, ModeResultKind, str]:
    """The shared perception loop for Shadow and Live.

    Event-driven: it blocks on the capture slot until a frame *newer than the
    one it already processed* exists, so there is no tick interval, no polling,
    and no way to fall behind into a backlog - a frame that arrives while
    perception is busy simply replaces the one that was waiting.
    """
    capture = context.frames
    last_sequence = 0
    processed = 0
    applied = 0
    terminal: tuple[ModeResultKind, str] | None = None

    # Registered for exactly as long as this loop runs. Outside the scope the
    # governor does not judge the processed rate at all, and entering it
    # restarts the rate window so this mode's first cadence decision is made on
    # this mode's frames.
    consuming = getattr(capture, "consuming", None)
    scope = consuming(map_id) if consuming is not None else contextlib.nullcontext()
    with scope:
        while not context.cancellation.is_cancelled():
            if max_ticks is not None and processed >= max_ticks:
                break
            envelope = capture.wait_for_new(last_sequence, 0.25)
            if envelope is None:
                continue
            picked_at_s = monotonic_s()
            frame = envelope.frame
            if last_sequence and frame.sequence > last_sequence + 1:
                # Frames existed that we never observed: the slot replaced them
                # while perception was busy. That is the designed behaviour, but it
                # has to be counted rather than silently absorbed.
                capture.note_dropped_observation(frame.sequence - last_sequence - 1)
            last_sequence = frame.sequence

            result = pipeline.analyze(frame, map_id=map_id, approach_valid=approach_valid)
            # One coherent read of the world outside this frame, taken once and
            # handed in - so the controller cannot consult a value that changed
            # between two of its own safety checks.
            health = context.health()
            navigator.note_health(
                focus_ok=health.focus_ok,
                processed_fps=float(getattr(capture, "processed_fps", 999.0)),
                cursor_safe=health.cursor_safe,
                geometry_revision=health.geometry_revision,
                profile_revision=health.profile_revision,
            )
            decision_started = monotonic_s()
            decision = navigator.decide(
                result.inputs, generation=context.generation, now_s=monotonic_s()
            )
            decision_ms = (monotonic_s() - decision_started) * 1000.0
            processed += 1

            capture.note_perception_ms(result.perception_ms)
            capture.note_decision_ms(decision_ms)
            capture.note_end_to_end_ms(frame.age_s(monotonic_s()) * 1000.0)

            # The key is stamped from the coordinator's current world, not from
            # anything this worker knows, so a cancelled worker's late frame cannot
            # outrank the session that replaced it.
            key = context.key_for(frame, pipeline.profile_revision)

            # ACT, THEN PUBLISH. The observation is built after the command has
            # been proposed or applied, so it can carry what *happened* rather than
            # what was asked for. Publishing first is what made WOULD_APPLY and
            # APPLIED indistinguishable in the UI: the packet was already gone by
            # the time the authority answered, so the overlay drew every request as
            # though the character were moving.
            command_view = CommandVisualization.none(live=apply is not None)
            if apply is not None:
                command_view = apply(decision, envelope, result)
                if command_view.outcome is CommandOutcome.APPLIED:
                    applied += 1

            # One typed record per frame; the sink writes a line only when
            # something in it changed. Shared by Shadow and Live so the two
            # cannot report the same run in different words.
            if decision.telemetry is not None:
                context.on_navigation(decision.telemetry)

            observation = pipeline.diagnostic(
                frame,
                result,
                decision,
                decision_ms=decision_ms,
                key=key,
                control_state=navigator.control_state,
                blockers=context.blockers,
                command_view=replace(command_view, key=key),
            )
            context.on_observation(observation)
            context.on_phase(decision.phase)
            # The trace ring lives on the capture service; narrower stand-ins used
            # by replay tests do not carry one, and the loop must not care.
            trace = getattr(capture, "trace", None)
            if result.timing is not None and trace is not None:
                tier = getattr(capture, "tier", None)
                trace.record(
                    FrameTrace(
                        frame_sequence=frame.sequence,
                        captured_at_s=frame.captured_at_s,
                        completed_at_s=frame.completed_at_s,
                        source_epoch=int(getattr(capture, "source_epoch", 0)),
                        cadence_hz=int(tier.fps) if tier is not None else 0,
                        capture_ms=frame.duration_ms,
                        scheduling_delay_ms=max(
                            0.0, (picked_at_s - frame.completed_at_s) * 1000.0
                        ),
                        perception=result.timing,
                        decision_ms=decision_ms,
                        capture_to_observation_ms=(
                            (monotonic_s() - frame.captured_at_s) * 1000.0
                        ),
                        settling=bool(getattr(capture, "settling", False)),
                    )
                )

            if decision.phase is NavigationPhase.ARRIVED:
                terminal = (ModeResultKind.ARRIVED, "arrival confirmed")
                break
            if decision.phase is NavigationPhase.ABANDONED:
                terminal = (ModeResultKind.ABANDONED, decision.reason)
                break

    if terminal is not None:
        return (processed, applied, terminal[0], terminal[1])
    kind = (
        ModeResultKind.CANCELLED
        if context.cancellation.is_cancelled()
        else ModeResultKind.COMPLETED
    )
    return (processed, applied, kind, f"{processed} frames processed")


def make_shadow_worker(
    pipeline_factory: Callable[[], PerceptionPipeline],
    capabilities_factory: Callable[[], NavigationCapabilities],
    *,
    max_ticks: int | None = None,
) -> Callable[[WorkerContext], ModeResult]:
    """Shadow: the full decision path through a ``NoInputSession``.

    It records what it *would* have applied and can never reach a raw port.
    Its capabilities are observation-only by construction, so the decision it
    records is the one a live run would make minus the authority to act.
    """

    def worker(context: WorkerContext) -> ModeResult:
        pipeline = context.pipeline or pipeline_factory()
        navigator = Navigator(capabilities=capabilities_factory())
        observer = context.observer

        def record(
            decision: NavigationDecision, envelope: Any, result: PerceptionResult
        ) -> CommandVisualization:
            del envelope, result
            # Shadow has no actuator, so the navigator would otherwise never
            # learn what "is held" - and the progress guard would abstain for
            # the whole run. Feeding it the level Shadow *would* have applied
            # keeps the observation path exercising the same code Live does,
            # while the session it holds remains physically incapable of an edge.
            navigator.note_held(
                sorted(key.value for key in decision.movement.keys),
                now_s=monotonic_s(),
                yaw_posted_px=decision.movement.yaw_px,
            )
            if decision.command is None or observer is None:
                return CommandVisualization.none(detail=decision.reason)
            observer.propose(decision.command)
            # A proposal, and physically incapable of being anything else: the
            # observer holds a NoInputSession with no route to a platform port.
            return CommandVisualization.for_shadow(decision.command)

        processed, proposed, kind, detail = _run_observer_loop(
            context,
            pipeline,
            navigator,
            map_id="shadow",
            approach_valid=False,
            max_ticks=max_ticks,
            apply=record,
        )
        return ModeResult(
            kind,
            f"shadow observed {processed} frames, {proposed} proposed commands ({detail})",
            evidence=(f"frames={processed}", f"proposed={proposed}"),
        )

    return worker


def make_live_worker(
    pipeline_factory: Callable[[], PerceptionPipeline],
    capabilities_factory: Callable[[], NavigationCapabilities],
    *,
    prologue: Callable[[WorkerContext, PerceptionPipeline], LivePrologueResult] | None = None,
) -> Callable[[WorkerContext], ModeResult]:
    """Live navigation, with the two input-emitting setup stages in front of it.

    The prologue is where the character stands still while the control mode is
    confirmed and the turn actuator is measured. It runs *here*, inside the
    live worker, because it is the first thing that may legitimately emit input
    and it may only do so after the physical arm the coordinator already
    required. Nothing about that arming changed: a human clicked Arm Live and
    pressed a hotkey with Roblox focused, and this worker exists because of it.

    If the prologue cannot prove a way to turn the camera, the worker releases
    and reports why. It never falls back to steering blind.
    """

    def worker(context: WorkerContext) -> ModeResult:
        session = context.navigation
        if session is None:
            return ModeResult(ModeResultKind.FAILED, "live worker started without a session")

        pipeline = context.pipeline or pipeline_factory()
        motion_was_enabled = pipeline.motion_enabled
        pipeline.motion_enabled = True
        capabilities = capabilities_factory()
        navigator = Navigator(capabilities=capabilities)
        context.lifecycle.note(
            LifecycleStage.LIVE_WORKER_ENTERED,
            context.worker_id,
            generation=context.generation,
        )
        witness = ForwardMotionWitness()

        if prologue is not None:
            outcome = prologue(context, pipeline)
            # ``stop_moving``, not ``release_navigation``. This line runs on
            # the *success* path of every healthy Live start, and while it used
            # the full release floor it closed admission for the rest of the
            # mode session - so the navigation loop three lines below could
            # never press anything, on any machine, ever (D-067).
            session.stop_moving("prologue-complete")
            if not outcome.ok:
                return ModeResult(
                    ModeResultKind.FAILED,
                    outcome.message,
                    evidence=(outcome.detail,) if outcome.detail else (),
                )
            capabilities = outcome.capabilities or capabilities
            navigator.adopt_capabilities(capabilities)
            measured = capabilities.turn_response
            if measured is not None:
                context.on_movement(turn_backend=measured.backend.label)
            # The prologue measured the idle noise floor moments ago with the
            # character stationary; the witness starts from that rather than
            # from nothing, so the first held frames are judged against a real
            # number instead of an assumption.
            if outcome.idle_noise_norm is not None:
                witness = ForwardMotionWitness(seed_noise_norm=outcome.idle_noise_norm)

        if not capabilities.steering_enabled:
            session.release_navigation("not-ready")
            reasons = "; ".join(capabilities.explain())
            context.on_movement(blocked_reason=reasons)
            return ModeResult(
                ModeResultKind.FAILED,
                f"Live navigation is not ready on {capabilities.os_name}/"
                f"{capabilities.profile_id}: {reasons}.",
                evidence=capabilities.blocking_reasons(),
            )

        baseline = RuntimeBaselineEstimator(context.worker_id)
        contact_config = ContactConfig()

        def apply(
            decision: NavigationDecision, envelope: Any, result: PerceptionResult
        ) -> CommandVisualization:
            now = monotonic_s()
            # Judge the frame we have *before* changing what is held: this
            # frame was captured under the previous level, which is the only
            # thing it can honestly report on.
            confirmed = witness.observe(
                MotionSample(
                    envelope.frame.sequence,
                    envelope.frame.captured_at_s,
                    result.inputs.motion,
                )
            )
            sample = result.inputs.motion
            if confirmed is not None and sample is not None:
                # The *confirmed* displacement, not the requested one. An
                # estimator that abstains reports nothing rather than zero:
                # "holding W against a wall" and "no motion estimate" are
                # different facts and the dashboard must not merge them.
                context.on_movement(displacement_norm=float(sample.forward_speed_norm or 0.0))

            if decision.release:
                # An ordinary stop. It lifts the keys and leaves the session
                # able to press again on the very next frame.
                session.stop_moving(decision.reason)
                _feed_back(now)
                witness.note_command(forward_held=False, at_s=now)
                context.on_movement(blocked_reason=decision.reason)
                return CommandVisualization.released(detail=decision.reason, live=True)

            # One call, level-triggered: the actuator works out the edges. This
            # runs on *every* non-releasing tick, including the ones that carry
            # no new command - a repeated frame, or one slightly past its
            # freshness budget - because restating the level is what keeps a
            # steady walk to a single down edge instead of a rattle.
            moved = session.move(decision.movement)
            held_forward = InputKey.W in moved.held

            # THE FEEDBACK WIRE. What the actuator reports it is *physically
            # holding* goes straight back into the navigator, which is what
            # feeds the applied-forward ledger, the locomotion baseline and the
            # stuck detector. Without this line the ledger is empty for the
            # whole session: held duration is always zero, no baseline is ever
            # sampled from real walking, and obstacle recovery is silently
            # disabled while looking, from the outside, like working code.
            _feed_back(now, moved)

            # Built from what the actuator reports it is holding, never from
            # the level that was requested. motion_confirmed answers a
            # different question from "is the key down", and the two are only
            # the same when nothing is wrong.
            view = (
                CommandVisualization.for_movement(
                    decision.command, moved, motion_confirmed=confirmed
                )
                if decision.command is not None
                else CommandVisualization.for_held(moved, motion_confirmed=confirmed)
            )
            witness.note_command(forward_held=held_forward, at_s=now)
            if moved.block.blocking:
                context.on_movement(blocked_reason=moved.block.value)
                return view
            context.on_movement(blocked_reason="")
            # The baseline is learned from frames where forward was genuinely
            # down - the actuator's answer, never the request - and its held
            # duration now comes from a ledger that is actually being written.
            observed = baseline.observe(
                result.inputs.motion,
                forward_applied=held_forward,
                held_ms=navigator.progress.ledger.held_continuously_for(now) * 1000.0,
                config=contact_config,
            )
            if observed.usable and not navigator.capabilities.motion_baseline.usable:
                navigator.adopt_capabilities(
                    replace(navigator.capabilities, motion_baseline=observed)
                )
                context.on_status(
                    f"walking speed measured over {baseline.samples} frames; "
                    "obstacle detection is now active"
                )
            return view

        def _feed_back(now_s: float, moved: MovementOutcome | None = None) -> None:
            """Tell the navigator what the keyboard actually looks like."""
            outcome = moved if moved is not None else _current_outcome()
            navigator.note_held(
                outcome.held,
                now_s=now_s,
                yaw_posted_px=outcome.yaw_posted_px,
                held_ms=outcome.held_ms,
            )

        def _current_outcome() -> MovementOutcome:
            """The actuator's own ledger, for the paths that pressed nothing."""
            actuator = session.movement
            return MovementOutcome(held=actuator.held, backend=actuator.backend)

        try:
            processed, applied, kind, detail = _run_observer_loop(
                context,
                pipeline,
                navigator,
                map_id="live",
                approach_valid=True,
                max_ticks=None,
                apply=apply,
            )
        finally:
            pipeline.motion_enabled = motion_was_enabled
            session.release_navigation("worker-exit")
        return ModeResult(kind, f"live: {applied} commands over {processed} frames ({detail})")

    return worker


def make_forward_probe_worker(
    pipeline_factory: Callable[[], PerceptionPipeline],
    *,
    fingerprint_factory: Callable[[], Any],
    control_mode_probe: Callable[[CapturedFrame], Any],
    config: AcceptanceConfig | None = None,
) -> Callable[[WorkerContext], ModeResult]:
    """One bounded forward hold against the real client, and a causal answer.

    This is the native acceptance check, and it exists because every earlier
    report of "it does not move" was unfalsifiable: the same sentence covered a
    chord that never reached the coordinator, an edge that never reached the
    OS, a key the OS never registered, and a character that was walking into a
    wall. It walks the whole chain and names the first stage that did not
    happen.

    It presses ``W`` exactly once, holds it by renewal for the configured
    pulse, watches the frames captured *after* the down edge, and releases in a
    ``finally`` on every path including the failures. It is registered only by
    ``treasure.py --forward-probe``; nothing else in the application can reach
    it.
    """

    def worker(context: WorkerContext) -> ModeResult:
        if context.navigation is None:
            return ModeResult(
                ModeResultKind.FAILED, "the forward probe started without a live session"
            )
        pipeline = context.pipeline or pipeline_factory()
        was_enabled = pipeline.motion_enabled
        pipeline.motion_enabled = True
        port = _LiveControlPort(
            context,
            pipeline,
            fingerprint_factory=fingerprint_factory,
            control_mode_probe=control_mode_probe,
        )
        probe = InputAcceptanceProbe(
            port,
            context.lifecycle,
            config=config or AcceptanceConfig(),
            cancelled=context.cancellation.is_cancelled,
            on_progress=context.on_status,
        )
        try:
            result = probe.run()
        finally:
            # Unconditional, on every path. A probe that returned a verdict
            # while still holding W would be the worst possible outcome here.
            port.release_forward("forward-probe-complete")
            context.navigation.release_navigation("forward-probe-exit")
            pipeline.motion_enabled = was_enabled
        return ModeResult(
            ModeResultKind.COMPLETED if result.ok else ModeResultKind.FAILED,
            result.summary_line(),
            evidence=(
                f"outcome={result.outcome.value}",
                f"idle_noise_norm={result.idle_noise_norm}",
                f"threshold_norm={result.threshold_norm}",
                f"moved_speed_norm={result.moved_speed_norm}",
                f"post_edge_frames={result.post_edge_samples}",
                f"leases_held={','.join(result.leases_held) or 'none'}",
            ),
        )

    return worker


@dataclass(frozen=True)
class LivePrologueResult:
    """What the input-emitting setup stages concluded, before navigation began."""

    ok: bool
    message: str
    capabilities: NavigationCapabilities | None = None
    detail: str = ""
    #: The idle motion noise the acceptance probe measured, so the live
    #: witness starts from a number taken on this machine moments ago.
    idle_noise_norm: float | None = None


class _LiveControlPort:
    """Bridges the setup machine's control stages onto a live input session.

    Everything it can do is bounded and released: one probe at a time, a hard
    cap on the lease horizon, and :meth:`release_turn` called by the machine
    after every observation and in its ``finally``.

    Two kinds of edge, and they are deliberately not the same code path. The
    turn probes hold a turn key or emit a bounded yaw delta and can never press
    ``W``; :meth:`input_acceptance` is the one thing here that can, it presses
    it exactly once for a bounded pulse, and it releases in a ``finally``.
    """

    #: A probe key may be held no longer than this, whatever the ladder asks
    #: for. It is no longer one evidence-bound lease: the hold is a renewal
    #: chain, so the ceiling is a deliberate bound rather than an artefact of
    #: how long a single command may live. See ``TurnLimits.key_probe_ms``.
    MAX_PROBE_HOLD_MS = 400
    #: A probe mouse delta may never exceed this many units.
    MAX_PROBE_UNITS = 200
    #: The forward pulse may never be asked to hold longer than this, whatever
    #: the acceptance config says. A cap in the object that owns the edge.
    MAX_FORWARD_PULSE_MS = 700

    def __init__(
        self,
        context: WorkerContext,
        pipeline: PerceptionPipeline,
        *,
        fingerprint_factory: Callable[[], Any],
        control_mode_probe: Callable[[CapturedFrame], Any],
    ) -> None:
        self._context = context
        self._pipeline = pipeline
        self._fingerprint_factory = fingerprint_factory
        self._control_mode_probe = control_mode_probe
        self._sequence = 0
        self._latest: PerceptionResult | None = None
        self._latest_frame: CapturedFrame | None = None
        self._envelope: Any = None
        self._held: str | None = None
        self._hold_until_s = 0.0
        self._forward_held = False
        self._forward_until_s = 0.0

    # -- shared frame plumbing -------------------------------------------
    def _refresh(self) -> PerceptionResult | None:
        envelope = self._context.frames.wait_for_new(self._sequence, 0.2)
        if envelope is None:
            return self._latest
        frame = envelope.frame
        self._sequence = frame.sequence
        self._latest_frame = frame
        self._latest = self._pipeline.analyze(frame, map_id="prologue", approach_valid=False)
        self._envelope = envelope
        return self._latest

    # -- ControlSetupPort: input acceptance -------------------------------
    def next_motion(self, timeout_s: float) -> MotionSample | None:
        """The next frame's motion reading, and the renewal that keeps W down.

        The renewal lives here because this is the only place a *newer* frame
        exists, and a newer frame is exactly what a renewal requires. Without
        it the lease expires inside its evidence budget and the watchdog lifts
        the key while the probe is still watching for the movement it caused.
        """
        envelope = self._context.frames.wait_for_new(self._sequence, timeout_s)
        if envelope is None:
            return None
        frame = envelope.frame
        self._sequence = frame.sequence
        self._latest_frame = frame
        self._envelope = envelope
        if self._forward_held:
            # Nothing to renew: the actuator is holding the key, and will go on
            # holding it until this object says otherwise or its watchdog lifts
            # it. What used to live here was a renewal chain that existed only
            # because no single command could outlive one frame's evidence.
            session = self._context.navigation
            self._forward_held = session is not None and InputKey.W in session.movement.held
        result = self._pipeline.analyze(frame, map_id="acceptance", approach_valid=False)
        self._latest = result
        return MotionSample(frame.sequence, frame.captured_at_s, result.inputs.motion)

    def _issue(
        self, *, forward_axis: int, turn_axis: int, yaw_delta_px: int, reason: str
    ) -> MovementOutcome | None:
        """Set the keyboard to one desired state. ``None`` if there is no session.

        This used to build a whole :class:`NavigationCommand` against the newest
        frame, clamp its lease to that frame's remaining evidence budget, and
        hand it to the evidence machinery - which meant a probe could not
        express a hold longer than about 80 ms, and after the first release in
        a session could not express one at all. It is now a level: say what
        should be down, and the actuator holds it until told otherwise.
        """
        session = self._context.navigation
        if session is None:
            return None
        return session.move(
            DesiredMovement(
                forward=forward_axis,
                turn=turn_axis,
                yaw_px=yaw_delta_px,
                reason=reason,
            )
        )

    def request_forward(self, hold_ms: int) -> ForwardRequest:
        """Press ``W`` and keep it down. The only place this object presses it.

        The hold is a renewal chain, not one long lease: the down edge goes out
        here and :meth:`next_motion` renews it against every newer frame until
        ``hold_ms`` has elapsed. That is the same mechanism ordinary navigation
        uses, and it is the only one that can hold a key longer than a frame's
        evidence budget - about 80 ms, which is a tap the game may not act on
        at all.
        """
        now = monotonic_s()
        outcome = self._issue(
            forward_axis=1,
            turn_axis=0,
            yaw_delta_px=0,
            reason="input acceptance: bounded forward hold",
        )
        if outcome is None:
            return ForwardRequest(False, (), "no live session", now)
        self._forward_held = InputKey.W in outcome.held
        self._forward_until_s = (
            now + min(self.MAX_FORWARD_PULSE_MS, max(1, hold_ms)) / 1000.0
            if self._forward_held
            else 0.0
        )
        return ForwardRequest(
            applied=self._forward_held,
            leases_held=tuple(sorted(key.value for key in outcome.held)),
            detail=outcome.block.value or outcome.detail or "forward is down",
            edge_at_s=now,
        )

    def forward_key_state(self) -> bool | None:
        return self._context.key_state(InputKey.W)

    def release_forward(self, reason: str) -> None:
        """Let go of ``W``. **Level, not lifecycle** - the session stays armed.

        This used to call ``release_navigation``, and that one line is why a
        healthy Live start could not walk. Ordinary success runs through here
        three times - the acceptance probe's ``finally``, ``input_acceptance``'s
        ``finally``, and the prologue's own tail - and each one reached
        ``InputAuthority.release_all``, which closes ``_admission_open`` and
        calls ``MovementActuator.disarm``. Admission is reopened in exactly one
        place, ``activate_generation``, and only on a mode transition. So by
        the time the prologue returned "the camera turns, go and navigate", the
        actuator that would do the navigating had been disarmed by the probe
        that proved it worked, and the follower's first ``apply`` came back
        ``STOPPED`` for the rest of the session.

        The full floor still exists and is still reached by everything that
        should reach it: Stop, worker exit, a terminal safety fault and process
        shutdown. It is no longer reached by *succeeding*.
        """
        session = self._context.navigation
        if session is not None:
            session.stop_moving(reason)
        self._forward_held = False
        self._forward_until_s = 0.0

    def input_acceptance(self) -> AcceptanceResult:
        """Run the bounded pulse. Released before this returns, on every path."""
        probe = InputAcceptanceProbe(
            self,
            self._context.lifecycle,
            cancelled=self._context.cancellation.is_cancelled,
            on_progress=self._context.on_status,
        )
        try:
            return probe.run()
        finally:
            self.release_forward("acceptance-complete")

    # -- ControlSetupPort: camera -----------------------------------------
    def control_mode_sample(self) -> Any:
        from prospector_engine.autosetup import ControlModeSample

        result = self._refresh()
        if result is None or self._latest_frame is None:
            return ControlModeSample(False, 0.0, "none", "waiting for a frame")
        sample: ControlModeSample = self._control_mode_probe(self._latest_frame)
        return sample

    def turn_observation(self) -> Any:
        from prospector_engine.turning import TurnObservation

        result = self._refresh()
        if result is None:
            return None
        direction = result.inputs.direction
        health = self._context.health()
        observation = TurnObservation(
            frame_sequence=result.inputs.frame.sequence,
            now_s=monotonic_s(),
            error_deg=direction.error_deg if direction.valid else None,
            confidence=direction.confidence,
            # The prologue holds no forward lease, so the character is
            # stationary by construction rather than by belief.
            stationary=True,
            focus_ok=health.focus_ok,
        )
        self._context.lifecycle.note(
            LifecycleStage.TURN_OBSERVED,
            "no heading" if observation.error_deg is None else f"{observation.error_deg:+.1f}",
            error_deg=observation.error_deg,
            confidence=round(direction.confidence, 3),
            frame_sequence=observation.frame_sequence,
        )
        return observation

    def emit_turn(self, backend_value: str, units: int) -> bool:
        """Run one camera probe to completion, then release.

        For the arrow keys this **holds the key down for the requested number
        of milliseconds by renewing it against each newer frame**, and only
        then releases. It used to issue one command whose lease was clamped to
        ``frame.captured_at_s + 0.1`` - so whatever the ladder asked for, the
        key was down for at most the evidence budget minus the frame's age, and
        the probe magnitudes above 80 ms were unreachable. A camera that needs
        a real press to move was being asked with a tap, could not answer, and
        the whole backend was then written off as unproven.

        Mouse yaw is a single relative delta and has no hold to renew.
        """
        backend = TurnBackend(backend_value)
        journal = self._context.lifecycle
        journal.note(
            LifecycleStage.TURN_REQUESTED,
            f"{backend.label} {units:+d} {backend.unit_name}",
            backend=backend_value,
            units=int(units),
        )
        if backend is not TurnBackend.ARROW_KEYS:
            delta = max(-self.MAX_PROBE_UNITS, min(self.MAX_PROBE_UNITS, units))
            outcome = self._issue(
                forward_axis=0,
                turn_axis=0,
                yaw_delta_px=delta,
                reason=f"setup probe: {backend.label} {units:+d}",
            )
            if outcome is None or outcome.block.blocking:
                detail = "no frame yet" if outcome is None else outcome.detail
                journal.note(LifecycleStage.TURN_UNAVAILABLE, detail, backend=backend_value)
                self._context.on_status(f"probe refused: {detail}")
                return False
            journal.note(
                LifecycleStage.TURN_POSTED,
                f"{backend.label} {delta:+d} px",
                backend=backend_value,
                delta_px=delta,
            )
            self._held = backend_value
            self._hold_until_s = monotonic_s()
            return True

        axis = 1 if units > 0 else -1
        hold_ms = min(self.MAX_PROBE_HOLD_MS, max(1, abs(units)))
        reason = f"setup probe: {backend.label} {units:+d}"
        outcome = self._issue(forward_axis=0, turn_axis=axis, yaw_delta_px=0, reason=reason)
        if outcome is None or outcome.block.blocking:
            detail = "no frame yet" if outcome is None else outcome.detail
            journal.note(LifecycleStage.TURN_UNAVAILABLE, detail, backend=backend_value)
            self._context.on_status(f"probe refused: {detail}")
            return False
        journal.note(
            LifecycleStage.TURN_POSTED,
            f"{backend.label} held {hold_ms} ms {'right' if axis > 0 else 'left'}",
            backend=backend_value,
            hold_ms=hold_ms,
            axis=axis,
        )
        self._held = backend_value
        deadline = monotonic_s() + hold_ms / 1000.0
        self._hold_until_s = deadline
        # Renew against every newer frame until the requested hold has elapsed.
        # Bounded twice over: by this deadline, and by the authority's rolling
        # horizon, which lifts the key if a renewal ever stops arriving.
        while monotonic_s() < deadline and not self._context.cancellation.is_cancelled():
            if self._refresh_frame(timeout_s=0.05) is None:
                continue
            renewal = self._issue(forward_axis=0, turn_axis=axis, yaw_delta_px=0, reason=reason)
            if renewal is None or renewal.block.blocking:
                break
        self.release_turn()
        return True

    def _refresh_frame(self, *, timeout_s: float) -> CapturedFrame | None:
        """Adopt the next frame without running perception on it.

        A renewal needs a strictly newer frame and nothing else; running the
        detector on every frame of a 300 ms hold would spend the hold's whole
        budget on work the probe does not use.
        """
        envelope = self._context.frames.wait_for_new(self._sequence, timeout_s)
        if envelope is None:
            return None
        frame = envelope.frame
        self._sequence = frame.sequence
        self._latest_frame = frame
        self._envelope = envelope
        return frame

    def release_turn(self) -> None:
        """Let go of the turn key. Level, not lifecycle - see
        :meth:`release_forward` for why this distinction is the whole bug."""
        session = self._context.navigation
        if session is not None and self._held is not None:
            session.stop_moving("setup probe complete")
        self._held = None

    def control_fingerprint(self) -> Any:
        return self._fingerprint_factory()


def make_live_prologue(
    *,
    fingerprint_factory: Callable[[], Any],
    control_mode_probe: Callable[[CapturedFrame], Any],
    capabilities_factory: Callable[[], NavigationCapabilities],
    setup_factory: Callable[[Callable[[], bool]], Any],
    turn_limits: Any = None,
    prior_factory: Callable[[Any], Any] | None = None,
    on_measured: Callable[[Any], None] | None = None,
) -> Callable[[WorkerContext, PerceptionPipeline], LivePrologueResult]:
    """Build the bounded, input-emitting prologue the live worker runs first.

    Assembled here rather than in the GUI so the sequence - verify the control
    mode, then measure the turn actuator, then release - is one reviewable
    thing rather than a set of callbacks scattered across the application.
    """

    def prologue(context: WorkerContext, pipeline: PerceptionPipeline) -> LivePrologueResult:
        port = _LiveControlPort(
            context,
            pipeline,
            fingerprint_factory=fingerprint_factory,
            control_mode_probe=control_mode_probe,
        )
        machine = setup_factory(context.cancellation.is_cancelled)
        prior = prior_factory(fingerprint_factory()) if prior_factory is not None else None
        # Consuming, but explicitly not measured: the prologue drives bounded
        # probes and waits to see their effect, so its frame cadence is a
        # property of the probe schedule and not of the pipeline. Counting it
        # as throughput would let a deliberately slow probe downshift the
        # cadence that Live is about to be judged against.
        consuming = getattr(context.frames, "consuming", None)
        scope = (
            consuming("prologue", measured=False)
            if consuming is not None
            else contextlib.nullcontext()
        )
        with scope:
            progress, response = machine.run_control(port, limits=turn_limits, prior=prior)
        port.release_turn()
        port.release_forward("prologue-complete")
        acceptance = getattr(machine, "acceptance", None)
        noise = acceptance.idle_noise_norm if acceptance is not None else None
        if response is None:
            failure = progress.failure
            context.lifecycle.note(
                LifecycleStage.TURN_UNAVAILABLE,
                failure.summary if failure else "automatic setup could not finish",
                evidence=failure.detail if failure else "",
            )
            return LivePrologueResult(
                False,
                failure.describe() if failure else "automatic setup could not finish",
                detail=failure.detail if failure else "",
                idle_noise_norm=noise,
            )
        context.lifecycle.note(
            LifecycleStage.TURN_BACKEND_SELECTED,
            response.describe(),
            backend=response.backend.value,
            degrees_per_unit=round(response.degrees_per_unit, 5),
            min_effective_units=response.min_effective_units,
            latency_ms=round(response.latency_s * 1000.0, 1),
            reliability=round(response.reliability, 3),
        )
        if on_measured is not None:
            on_measured(response)
        capabilities = replace(
            capabilities_factory(), control_mode_ok=True, turn_response=response
        )
        return LivePrologueResult(
            True, response.describe(), capabilities=capabilities, idle_noise_norm=noise
        )

    return prologue
