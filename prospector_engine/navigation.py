"""Navigation FSM, steering controller, bounded recovery, and the mode workers.

Two rules run through this whole module:

* **No transition is justified by elapsed time alone.** Time can expire an
  action; it can never prove collision, movement, arrival, or success.
* **Within one update the event priority is fixed**: safety/cancellation ->
  credible arrival -> contact/recovery -> ordinary steering. The first credible
  arrival candidate releases movement immediately (plan 6.2).

Live steering is gated on evidence that does not exist yet. ``NavigationGates``
carries the per-OS/profile status, and with everything ``PENDING`` the live
worker refuses to steer and safe-stops with an explanation instead of guessing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from prospector_engine.contracts import (
    ArrivalObservation,
    ArrowCandidateRecord,
    ArrowObservation,
    CapturedFrame,
    ControlState,
    DiagnosticObservation,
    DirectionObservation,
    EvidenceStatus,
    ModeResult,
    ModeResultKind,
    MotionObservation,
    NavigationCommand,
    NavigationPhase,
    Provenance,
    RuntimeKey,
    monotonic_s,
)
from prospector_engine.coordinator import WorkerContext
from prospector_engine.motion import ContactMonitor
from prospector_engine.vision import (
    DIRECTION_STRATEGIES,
    ArrivalDetector,
    ArrowProfile,
    ArrowSegmenter,
    ArrowTracker,
    ProfileAuthority,
    wrap_deg,
)

__all__ = [
    "NavigationGates",
    "Navigator",
    "RecoveryLadder",
    "RecoveryLevel",
    "SteeringConfig",
    "SteeringController",
    "describe_decision",
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
    decision into a different claim than the one the controller made
    (mission section 10).
    """
    if not inputs.arrow.valid:
        return _plain_reason(inputs.arrow.abstain_reason)
    direction = inputs.direction
    if not direction.valid or direction.error_deg is None:
        return _plain_reason(direction.abstain_reason)
    error = wrap_deg(direction.error_deg)
    command = decision.command
    if command is not None and command.forward_axis == 1 and command.yaw_delta_px == 0:
        return "Aligned - move forward"
    if abs(error) < 1.0:
        return "Aligned - move forward"
    side = "right" if error > 0 else "left"
    return f"Turn {side} {abs(error):.0f} degrees"


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NavigationGates:
    """Which experiments have passed for this exact OS / profile / condition.

    Nothing here defaults to enabled. ``steering_enabled`` requires the whole
    perception chain plus the actuator characterization; ``recovery_enabled``
    additionally requires both E-MOTION and E-RECOVERY (plan 15, Phase 4).
    """

    os_name: str
    profile_id: str
    e_view: EvidenceStatus = EvidenceStatus.PENDING
    e_anchor: EvidenceStatus = EvidenceStatus.PENDING
    e_forward: EvidenceStatus = EvidenceStatus.PENDING
    e_dir_e2e: EvidenceStatus = EvidenceStatus.PENDING
    e_prof: EvidenceStatus = EvidenceStatus.PENDING
    e_arrive: EvidenceStatus = EvidenceStatus.PENDING
    e_motion: EvidenceStatus = EvidenceStatus.PENDING
    e_yaw: EvidenceStatus = EvidenceStatus.PENDING
    e_steer_cal: EvidenceStatus = EvidenceStatus.PENDING
    e_steer_e2e: EvidenceStatus = EvidenceStatus.PENDING
    e_recovery: EvidenceStatus = EvidenceStatus.PENDING
    e_next_map: EvidenceStatus = EvidenceStatus.PENDING

    def _passed(self, *statuses: EvidenceStatus) -> bool:
        return all(status is EvidenceStatus.VALIDATED for status in statuses)

    @property
    def steering_enabled(self) -> bool:
        return self._passed(
            self.e_view,
            self.e_anchor,
            self.e_forward,
            self.e_prof,
            self.e_dir_e2e,
            self.e_yaw,
            self.e_steer_cal,
            self.e_steer_e2e,
        )

    @property
    def recovery_enabled(self) -> bool:
        return self.steering_enabled and self._passed(self.e_motion, self.e_recovery)

    @property
    def arrival_enabled(self) -> bool:
        return self._passed(self.e_arrive)

    @property
    def next_map_enabled(self) -> bool:
        return self._passed(self.e_next_map)

    def blocking_reasons(self) -> tuple[str, ...]:
        pending = [
            name
            for name, status in (
                ("E-VIEW", self.e_view),
                ("E-ANCHOR", self.e_anchor),
                ("E-FORWARD", self.e_forward),
                ("E-PROF", self.e_prof),
                ("E-DIR-E2E", self.e_dir_e2e),
                ("E-YAW", self.e_yaw),
                ("E-STEER-CAL", self.e_steer_cal),
                ("E-STEER-E2E", self.e_steer_e2e),
            )
            if status is not EvidenceStatus.VALIDATED
        ]
        return tuple(pending)


# ---------------------------------------------------------------------------
# Steering
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SteeringConfig:
    """Bounded PD control. Every number is provisional until frozen.

    ``deadband_deg`` may only be selected inside the independently measured
    ``[min_stable_correction_deg, max_usable_deadband_deg]`` interval from
    E-YAW / E-STEER-CAL. Widening it after seeing estimator failures is
    explicitly forbidden (plan 9.1), so it is stored with its source.
    """

    kp: float = 0.9
    kd: float = 0.12
    deadband_deg: float = 6.0
    hysteresis_deg: float = 2.0
    max_turn_px_per_tick: int = 40
    max_turn_accel_px: int = 12
    derivative_filter: float = 0.6
    command_lease_ms: int = 100
    arrow_loss_grace_ms: int = 300
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="TREASURE_NAVIGATION_PLAN.md section 9.1",
            note="deadband is NOT frozen; E-YAW and E-STEER-CAL are PENDING",
        )
    )


class SteeringController:
    """Proportional control with filtered derivative damping and hysteresis.

    Lower confidence only ever *reduces* command magnitude - it never raises
    gain - and a duplicate or missing frame freezes the derivative rather than
    letting a zero delta-t manufacture a spike.
    """

    def __init__(self, config: SteeringConfig | None = None) -> None:
        self._config = config or SteeringConfig()
        self._last_error_deg: float | None = None
        self._last_time_s: float | None = None
        self._filtered_derivative = 0.0
        self._last_turn_px = 0
        self._active_side: int = 0

    @property
    def config(self) -> SteeringConfig:
        return self._config

    def reset(self) -> None:
        self._last_error_deg = None
        self._last_time_s = None
        self._filtered_derivative = 0.0
        self._last_turn_px = 0
        self._active_side = 0

    def update(
        self, direction: DirectionObservation, *, now_s: float, frame_is_duplicate: bool
    ) -> int:
        """Return a bounded yaw delta in pixels. Zero means "do not turn"."""
        if not direction.valid or direction.error_deg is None:
            self._last_turn_px = 0
            self._active_side = 0
            return 0

        error = wrap_deg(direction.error_deg)
        # Deadband with hysteresis: once inside, stay inside until the error
        # exceeds deadband + hysteresis, which is what stops left/right chatter.
        threshold = self._config.deadband_deg
        if self._active_side == 0:
            threshold += self._config.hysteresis_deg
        if abs(error) < threshold:
            self._active_side = 0
            self._last_error_deg = error
            self._last_time_s = now_s
            self._last_turn_px = 0
            return 0

        derivative = 0.0
        if (
            not frame_is_duplicate
            and self._last_error_deg is not None
            and self._last_time_s is not None
        ):
            delta_t = now_s - self._last_time_s
            if delta_t > 1e-6:
                raw = wrap_deg(error - self._last_error_deg) / delta_t
                alpha = self._config.derivative_filter
                derivative = alpha * self._filtered_derivative + (1.0 - alpha) * raw
        self._filtered_derivative = derivative

        raw_turn = self._config.kp * error + self._config.kd * derivative
        confidence_scale = max(0.0, min(1.0, direction.confidence))
        turn = raw_turn * confidence_scale

        limit = self._config.max_turn_px_per_tick
        turn = max(-limit, min(limit, turn))
        accel_limit = self._config.max_turn_accel_px
        turn = max(
            self._last_turn_px - accel_limit, min(self._last_turn_px + accel_limit, int(turn))
        )

        self._last_error_deg = error
        self._last_time_s = now_s
        self._last_turn_px = int(turn)
        self._active_side = 1 if error > 0 else -1
        return int(turn)


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryLevel:
    name: str
    description: str
    max_input_ms: int
    max_attempts: int
    forward_axis: int
    lateral_axis: int
    jump: bool


RECOVERY_LADDER: tuple[RecoveryLevel, ...] = (
    RecoveryLevel("R1", "bounded tangent bias, forward intent preserved", 800, 2, 1, 1, False),
    RecoveryLevel("R2", "jump while continuing the locked tangent", 600, 2, 1, 1, True),
    RecoveryLevel("R3", "release forward, bounded reverse, rotate away", 700, 1, -1, 0, False),
    RecoveryLevel("R4", "mark first side failed, try the opposite once", 800, 1, 1, -1, False),
    RecoveryLevel("R5", "release movement, normalize camera, reacquire", 400, 1, 0, 0, False),
)


@dataclass(frozen=True)
class RecoveryBudget:
    total_time_ms: int = 12000
    total_input_ms: int = 6000
    side_lock_cooldown_ms: int = 1500
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="TREASURE_NAVIGATION_PLAN.md section 9.3",
            note="E-RECOVERY has not been run; recovery stays disabled until it has",
        )
    )


class RecoveryLadder:
    """A finite escalation with a total time cap and a total input cap.

    Every level has an attempt cap and a deadline; exhaustion returns
    ``ABANDONED``. Success requires restored-progress evidence - elapsed time
    is never success (plan 9.3).
    """

    def __init__(self, budget: RecoveryBudget | None = None) -> None:
        self._budget = budget or RecoveryBudget()
        self._index = 0
        self._attempts = 0
        self._started_s: float | None = None
        self._input_ms = 0
        self._side = 0
        self._side_locked_at_s: float | None = None
        self._failed_sides: set[int] = set()

    @property
    def level(self) -> RecoveryLevel | None:
        if self._index >= len(RECOVERY_LADDER):
            return None
        return RECOVERY_LADDER[self._index]

    @property
    def side(self) -> int:
        return self._side

    @property
    def exhausted(self) -> bool:
        return self._index >= len(RECOVERY_LADDER)

    def begin(self, now_s: float, side: int) -> None:
        self._index = 0
        self._attempts = 0
        self._started_s = now_s
        self._input_ms = 0
        self._side = side
        self._side_locked_at_s = now_s
        self._failed_sides.clear()

    def reset(self) -> None:
        self._index = len(RECOVERY_LADDER)
        self._started_s = None

    def may_switch_side(self, now_s: float) -> bool:
        """The chosen side locks for the episode; only an explicit failure
        predicate outside the cooldown may flip it (plan 9.2)."""
        if self._side_locked_at_s is None:
            return True
        return (now_s - self._side_locked_at_s) * 1000.0 >= self._budget.side_lock_cooldown_ms

    def switch_side(self, now_s: float) -> bool:
        if not self.may_switch_side(now_s):
            return False
        self._failed_sides.add(self._side)
        self._side = -self._side if self._side else 1
        self._side_locked_at_s = now_s
        return True

    def over_budget(self, now_s: float) -> str | None:
        if self._started_s is None:
            return None
        if (now_s - self._started_s) * 1000.0 > self._budget.total_time_ms:
            return "recovery total time cap"
        if self._input_ms > self._budget.total_input_ms:
            return "recovery total input cap"
        return None

    def note_input(self, milliseconds: int) -> None:
        self._input_ms += milliseconds

    def escalate(self) -> RecoveryLevel | None:
        level = self.level
        if level is None:
            return None
        self._attempts += 1
        if self._attempts >= level.max_attempts:
            self._index += 1
            self._attempts = 0
        return self.level


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
    phase: NavigationPhase
    command: NavigationCommand | None
    reason: str
    release: bool = False


#: The navigation FSM and the Shift-Lock controller describe the same run from
#: two angles: one in lifecycle terms, one in "is W held right now" terms. The
#: mapping is explicit so the two can never drift into disagreeing.
_PHASE_TO_CONTROL: dict[NavigationPhase, ControlState] = {
    NavigationPhase.ACQUIRE: ControlState.ACQUIRE,
    NavigationPhase.ALIGN: ControlState.ALIGN,
    NavigationPhase.FOLLOW: ControlState.FOLLOW,
    NavigationPhase.REACQUIRE: ControlState.REACQUIRE,
    NavigationPhase.CONTACT: ControlState.BLOCKED,
    NavigationPhase.RECOVERY: ControlState.BLOCKED,
    NavigationPhase.ARRIVAL_CONFIRM: ControlState.ACQUIRE,
    NavigationPhase.ARRIVED: ControlState.SAFE_STOP,
    NavigationPhase.ABANDONED: ControlState.SAFE_STOP,
    NavigationPhase.FAILED: ControlState.SAFE_STOP,
}


class Navigator:
    """The navigation FSM. Pure decision logic - it emits no input itself.

    Both the Shadow observer and the Live worker drive the same instance of
    this class; the only difference is what they do with the returned command.
    """

    def __init__(
        self,
        *,
        gates: NavigationGates,
        steering: SteeringController | None = None,
        recovery: RecoveryLadder | None = None,
        contact: ContactMonitor | None = None,
        max_evidence_age_ms: int = 100,
    ) -> None:
        self._gates = gates
        self._steering = steering or SteeringController()
        self._recovery = recovery or RecoveryLadder()
        self._contact = contact or ContactMonitor()
        self._max_evidence_age_ms = max_evidence_age_ms
        self._phase = NavigationPhase.ACQUIRE
        self._last_valid_arrow_s: float | None = None
        self._arrival_latches = 0

    @property
    def phase(self) -> NavigationPhase:
        return self._phase

    @property
    def control_state(self) -> ControlState:
        """The Shift-Lock controller's view of the same decision."""
        return _PHASE_TO_CONTROL.get(self._phase, ControlState.ACQUIRE)

    @property
    def arrival_latches(self) -> int:
        return self._arrival_latches

    def decide(
        self, inputs: NavigationInputs, *, generation: int, now_s: float
    ) -> NavigationDecision:
        """One update, with the fixed event priority from plan 6.2."""
        frame = inputs.frame

        # 1. Safety / evidence validity. A stale or failed frame releases.
        age_ms = frame.age_s(now_s) * 1000.0
        if frame.capture_error is not None:
            return self._release(
                NavigationPhase.REACQUIRE, f"capture-error:{frame.capture_error}"
            )
        if not frame.geometry.valid:
            return self._release(NavigationPhase.REACQUIRE, "viewport-invalid")
        if age_ms > self._max_evidence_age_ms:
            return self._release(NavigationPhase.REACQUIRE, f"stale-frame:{age_ms:.0f}ms")

        # 2. Arrival preempts recovery and steering.
        arrival = inputs.arrival
        if arrival is not None and arrival.valid:
            if self._gates.arrival_enabled:
                self._arrival_latches += 1
                return self._release(NavigationPhase.ARRIVAL_CONFIRM, "arrival candidate")
            return self._release(
                NavigationPhase.REACQUIRE, "arrival evidence but E-ARRIVE PENDING"
            )

        # 3. Contact / recovery.
        if inputs.motion is not None:
            evidence = self._contact.update(
                inputs.motion, forward_commanded=inputs.forward_commanded, now_s=now_s
            )
            if evidence.contact:
                if not self._gates.recovery_enabled:
                    return self._release(
                        NavigationPhase.ABANDONED,
                        "contact detected but E-MOTION/E-RECOVERY PENDING",
                    )
                over = self._recovery.over_budget(now_s)
                if over is not None or self._recovery.exhausted:
                    return self._release(
                        NavigationPhase.ABANDONED, over or "recovery exhausted"
                    )
                self._phase = NavigationPhase.RECOVERY
                return NavigationDecision(
                    NavigationPhase.RECOVERY, None, "recovery ladder step (gated)"
                )

        # 4. Ordinary reacquisition / steering.
        if not inputs.arrow.valid:
            grace_ms = self._steering.config.arrow_loss_grace_ms
            if (
                self._last_valid_arrow_s is not None
                and (now_s - self._last_valid_arrow_s) * 1000.0 <= grace_ms
            ):
                # Yaw releases immediately; only the previously safe forward
                # command may persist through the grace window (plan 7.3).
                return NavigationDecision(
                    NavigationPhase.REACQUIRE,
                    self._command(
                        generation,
                        frame,
                        now_s,
                        forward=1,
                        lateral=0,
                        jump=False,
                        yaw=0,
                        reason="arrow-loss grace: forward only, yaw released",
                    ),
                    "arrow-loss grace",
                )
            return self._release(
                NavigationPhase.REACQUIRE, f"arrow abstained: {inputs.arrow.abstain_reason}"
            )

        self._last_valid_arrow_s = now_s
        if not inputs.direction.valid:
            return self._release(
                NavigationPhase.ALIGN, f"direction abstained: {inputs.direction.abstain_reason}"
            )
        if not self._gates.steering_enabled:
            return self._release(
                NavigationPhase.ALIGN,
                "steering disabled: " + ",".join(self._gates.blocking_reasons()) + " PENDING",
            )

        yaw = self._steering.update(
            inputs.direction, now_s=now_s, frame_is_duplicate=frame.duplicate
        )
        aligned = yaw == 0
        self._phase = NavigationPhase.FOLLOW if aligned else NavigationPhase.ALIGN
        return NavigationDecision(
            self._phase,
            self._command(
                generation,
                frame,
                now_s,
                forward=1 if aligned else 0,
                lateral=0,
                jump=False,
                yaw=yaw,
                reason="follow" if aligned else "align",
            ),
            "steering",
        )

    def _release(self, phase: NavigationPhase, reason: str) -> NavigationDecision:
        self._phase = phase
        self._steering.reset()
        self._contact.reset()
        return NavigationDecision(phase, None, reason, release=True)

    def _command(
        self,
        generation: int,
        frame: CapturedFrame,
        now_s: float,
        *,
        forward: int,
        lateral: int,
        jump: bool,
        yaw: int,
        reason: str,
    ) -> NavigationCommand:
        lease_s = self._steering.config.command_lease_ms / 1000.0
        max_valid_s = frame.captured_at_s + self._max_evidence_age_ms / 1000.0
        return NavigationCommand(
            generation=generation,
            source_frame_sequence=frame.sequence,
            source_captured_at_s=frame.captured_at_s,
            forward_axis=forward,  # type: ignore[arg-type]
            lateral_axis=lateral,  # type: ignore[arg-type]
            jump=jump,
            yaw_delta_px=yaw,
            issued_at_s=now_s,
            valid_until_s=min(now_s + lease_s, max_valid_s),
            reason=reason,
        )


# ---------------------------------------------------------------------------
# Perception assembly
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenceFrame:
    """Where the player is on screen and which way "forward" points.

    Both values are **provisional configuration, not estimates**. E-ANCHOR and
    E-FORWARD have not been run, so the reference arm the diagnostics draw is
    labelled as assumed everywhere it appears, and the controller stays gated
    regardless of how convincing the picture looks.

    Screen-up as forward is the hypothesis plan 7.4 sets out to test after a
    deterministic camera reset; it is drawn here so a human can judge it, which
    is precisely what Shadow is for.
    """

    anchor_canonical_px: tuple[float, float] = (640.0, 430.0)
    forward_deg: float = 0.0
    source: str = "assumed: screen-up after camera reset (E-FORWARD PENDING)"
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PENDING,
            source="TREASURE_NAVIGATION_PLAN.md section 7.4 E-ANCHOR / E-FORWARD",
            note="drawn for human judgement in Shadow; never treated as validated",
        )
    )

    @property
    def validated(self) -> bool:
        return self.provenance.status is EvidenceStatus.VALIDATED


@dataclass
class PerceptionResult:
    """One frame's perception, with the timings that produced it."""

    inputs: NavigationInputs
    candidates: tuple[ArrowCandidateRecord, ...]
    contour_px: tuple[tuple[int, int], ...]
    desired_deg: float | None
    cues: tuple[tuple[str, DirectionObservation], ...]
    perception_ms: float


@dataclass
class PerceptionPipeline:
    """Segmenter, tracker, arrival detector, and direction cue for one profile.

    The profile can be swapped at runtime through :meth:`set_profile`, which the
    dashboard's selector uses; the profile actually used is recorded on every
    observation so the picture and the label cannot disagree.
    """

    segmenter: ArrowSegmenter
    tracker: ArrowTracker = field(default_factory=ArrowTracker)
    arrival: ArrivalDetector = field(default_factory=ArrivalDetector)
    strategy: str = "fusion"
    reference: ReferenceFrame = field(default_factory=ReferenceFrame)
    #: The one authority on the active profile. When present, the pipeline
    #: applies its staged swap at the top of every frame, which is what makes a
    #: profile change land on a frame boundary rather than mid-observation.
    profiles: ProfileAuthority | None = None
    #: Padding around a tracked arrow when searching only a region of interest.
    roi_padding_px: int = 220
    #: Force a full-frame pass this often even while a track holds, so a track
    #: cannot quietly follow the wrong thing forever (plan 8).
    full_frame_every: int = 20
    _frames_since_full: int = 0
    _roi_hits: int = 0
    _full_passes: int = 0
    _profile_revision: int = 1
    _geometry_identity: tuple[object, ...] | None = None

    def set_profile(self, profile: ArrowProfile) -> None:
        """Swap the arrow profile and drop any track built from the old one."""
        self.segmenter = ArrowSegmenter(profile)
        self.tracker = ArrowTracker()
        self.arrival = ArrivalDetector()
        self._frames_since_full = self.full_frame_every  # force a full pass

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
        """Drop temporal state when the coordinate basis changes.

        A tracker prediction, an ROI, and a candidate history are all expressed
        in canonical pixels of one specific viewport. After a resize, a display
        migration, or a window replacement they describe a place that no longer
        exists, so they are discarded rather than reinterpreted.
        """
        identity = frame.geometry.identity()
        if self._geometry_identity is not None and identity != self._geometry_identity:
            self.tracker = ArrowTracker()
            self.arrival = ArrivalDetector()
            self._frames_since_full = self.full_frame_every
            self._geometry_identity = identity
            return True
        self._geometry_identity = identity
        return False

    @property
    def roi_hits(self) -> int:
        return self._roi_hits

    @property
    def full_passes(self) -> int:
        return self._full_passes

    def _roi_for(self, frame: CapturedFrame) -> tuple[int, int, int, int] | None:
        """A search region around the predicted track, or ``None`` for full frame.

        Returns ``None`` whenever the track is stale or the periodic full-frame
        pass is due, so acquisition is never permanently confined to where the
        arrow used to be.
        """
        if self._frames_since_full >= self.full_frame_every:
            return None
        predicted = self.tracker.predicted()
        if predicted is None:
            return None
        width, height = frame.canonical_size_px
        pad = self.roi_padding_px
        x = max(0, int(predicted[0]) - pad)
        y = max(0, int(predicted[1]) - pad)
        right = min(width, int(predicted[0]) + pad)
        bottom = min(height, int(predicted[1]) + pad)
        if right - x < 32 or bottom - y < 32:
            return None
        return (x, y, right - x, bottom - y)

    def set_strategy(self, strategy: str) -> None:
        if strategy in DIRECTION_STRATEGIES:
            self.strategy = strategy

    @property
    def profile(self) -> ArrowProfile:
        return self.segmenter.profile

    def analyze(
        self, frame: CapturedFrame, *, map_id: str, approach_valid: bool
    ) -> PerceptionResult:
        """Everything derived from one frame, in one pass."""
        started = monotonic_s()
        # Both of these run *before* any pixel is read, so a profile swap or a
        # geometry change can never land halfway through an observation.
        self._sync_profile()
        self._sync_geometry(frame)
        roi = self._roi_for(frame)
        raw_arrow, candidates, contour = self.segmenter.observe_detailed(frame, roi)
        if roi is not None and not raw_arrow.valid:
            # An ROI miss is not an abstention: fall back to the full frame
            # immediately rather than reporting a loss the tracker caused.
            roi = None
            raw_arrow, candidates, contour = self.segmenter.observe_detailed(frame, None)
        if roi is None:
            self._frames_since_full = 0
            self._full_passes += 1
        else:
            self._frames_since_full += 1
            self._roi_hits += 1
        arrow = self.tracker.update(raw_arrow)

        anchor = self.reference.anchor_canonical_px
        forward = self.reference.forward_deg
        # Every cue, every frame: the selected one drives the controller, and
        # the rest are what make a disagreement legible instead of a silent
        # abstention (plan 7.4 E-DIR-IDEAL).
        cues = tuple(
            (name, strategy(arrow, anchor, forward))
            for name, strategy in DIRECTION_STRATEGIES.items()
        )
        direction = dict(cues)[self.strategy]

        desired_deg: float | None = None
        if arrow.valid and direction.valid and direction.error_deg is not None:
            desired_deg = wrap_deg(forward + direction.error_deg)

        arrival = self.arrival.observe(frame, map_id=map_id, approach_valid=approach_valid)
        inputs = NavigationInputs(
            frame=frame,
            arrow=arrow,
            direction=direction,
            motion=None,
            arrival=arrival,
            forward_commanded=False,
        )
        return PerceptionResult(
            inputs=inputs,
            candidates=candidates,
            contour_px=contour,
            desired_deg=desired_deg,
            cues=cues,
            perception_ms=(monotonic_s() - started) * 1000.0,
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
    ) -> DiagnosticObservation:
        """Bind the frame, the geometry, and the decision into one value.

        The preview and the controller both read this, so an overlay can never
        be drawn from observation N over frame N+1. ``key`` stamps the packet
        with the identity every consumer compares before drawing or acting.
        """
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
            capture_ms=frame.duration_ms,
            perception_ms=result.perception_ms,
            decision_ms=decision_ms,
            control_state=control_state,
            plain_summary=describe_decision(inputs, decision),
            blockers=blockers,
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
    apply: Callable[[NavigationDecision, Any], bool] | None,
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

    while not context.cancellation.is_cancelled():
        if max_ticks is not None and processed >= max_ticks:
            break
        envelope = capture.wait_for_new(last_sequence, 0.25)
        if envelope is None:
            continue
        frame = envelope.frame
        if last_sequence and frame.sequence > last_sequence + 1:
            # Frames existed that we never observed: the slot replaced them
            # while perception was busy. That is the designed behaviour, but it
            # has to be counted rather than silently absorbed.
            capture.note_dropped_observation(frame.sequence - last_sequence - 1)
        last_sequence = frame.sequence

        result = pipeline.analyze(frame, map_id=map_id, approach_valid=approach_valid)
        decision_started = monotonic_s()
        decision = navigator.decide(
            result.inputs, generation=context.generation, now_s=monotonic_s()
        )
        decision_ms = (monotonic_s() - decision_started) * 1000.0
        processed += 1

        capture.note_perception_ms(result.perception_ms)
        capture.note_decision_ms(decision_ms)
        capture.note_end_to_end_ms(frame.age_s(monotonic_s()) * 1000.0)

        observation = pipeline.diagnostic(
            frame,
            result,
            decision,
            decision_ms=decision_ms,
            # The key is stamped from the coordinator's current world, not from
            # anything this worker knows, so a cancelled worker's late frame
            # cannot outrank the session that replaced it.
            key=context.key_for(frame, pipeline.profile_revision),
            control_state=navigator.control_state,
            blockers=context.blockers,
        )
        context.on_observation(observation)
        context.on_phase(decision.phase)

        if apply is not None and apply(decision, envelope):
            applied += 1
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
    gates: NavigationGates,
    *,
    max_ticks: int | None = None,
) -> Callable[[WorkerContext], ModeResult]:
    """Shadow: the full decision path through a ``NoInputSession``.

    It records what it *would* have applied and can never reach a raw port.
    """

    def worker(context: WorkerContext) -> ModeResult:
        pipeline = context.pipeline or pipeline_factory()
        navigator = Navigator(gates=gates)
        observer = context.observer

        def record(decision: NavigationDecision, envelope: Any) -> bool:
            del envelope
            if decision.command is None or observer is None:
                context.on_status(f"{decision.phase.name}: {decision.reason}")
                return False
            observer.propose(decision.command)
            context.on_status(f"WOULD_APPLY {decision.command.reason}")
            return True

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
    gates: NavigationGates,
) -> Callable[[WorkerContext], ModeResult]:
    """Live navigation. Refuses to steer while its gates are pending.

    This is the point of the gate structure: the path exists, is reviewable,
    and is exercised in Shadow, but it will not emit a movement command until
    the evidence for that OS, profile, and condition says it may.
    """

    def worker(context: WorkerContext) -> ModeResult:
        session = context.navigation
        if session is None:
            return ModeResult(ModeResultKind.FAILED, "live worker started without a session")
        if not gates.steering_enabled:
            session.release_navigation("gates-pending")
            reasons = ",".join(gates.blocking_reasons())
            return ModeResult(
                ModeResultKind.FAILED,
                f"Live navigation is not enabled for {gates.os_name}/{gates.profile_id}: "
                f"{reasons} PENDING. Run Shadow and collect evidence first.",
                evidence=gates.blocking_reasons(),
            )

        pipeline = context.pipeline or pipeline_factory()
        navigator = Navigator(gates=gates)

        def apply(decision: NavigationDecision, envelope: Any) -> bool:
            if decision.release or decision.command is None:
                session.release_navigation(decision.reason)
                return False
            outcome = session.apply_navigation_command(
                decision.command, envelope.evidence_token
            )
            if outcome.applied:
                return True
            session.release_navigation(f"apply-rejected:{outcome.detail}")
            return False

        processed, applied, kind, detail = _run_observer_loop(
            context,
            pipeline,
            navigator,
            map_id="live",
            approach_valid=True,
            max_ticks=None,
            apply=apply,
        )
        session.release_navigation("worker-exit")
        return ModeResult(kind, f"live: {applied} commands over {processed} frames ({detail})")

    return worker
