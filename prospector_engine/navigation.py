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

from prospector_engine.contracts import (
    ArrivalObservation,
    ArrowObservation,
    CapturedFrame,
    DirectionObservation,
    EvidenceStatus,
    ModeResult,
    ModeResultKind,
    MotionObservation,
    NavigationCommand,
    NavigationPhase,
    Provenance,
    monotonic_s,
)
from prospector_engine.coordinator import WorkerContext
from prospector_engine.motion import ContactMonitor
from prospector_engine.vision import (
    DIRECTION_STRATEGIES,
    ArrivalDetector,
    ArrowSegmenter,
    ArrowTracker,
    wrap_deg,
)

__all__ = [
    "NavigationGates",
    "Navigator",
    "RecoveryLadder",
    "RecoveryLevel",
    "SteeringConfig",
    "SteeringController",
    "make_live_worker",
    "make_shadow_worker",
]


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


@dataclass
class PerceptionPipeline:
    """Segmenter + tracker + direction cue, producing one tick's inputs.

    The player anchor and forward reference are *inputs*, not guesses: with
    E-ANCHOR and E-FORWARD pending, ``forward_deg`` is ``None`` and every
    direction cue abstains, which is the intended behavior (plan 8).
    """

    segmenter: ArrowSegmenter
    tracker: ArrowTracker = field(default_factory=ArrowTracker)
    arrival: ArrivalDetector = field(default_factory=ArrivalDetector)
    strategy: str = "fusion"
    anchor_px: tuple[float, float] | None = None
    forward_deg: float | None = None

    def observe(
        self, frame: CapturedFrame, *, map_id: str, approach_valid: bool
    ) -> NavigationInputs:
        arrow = self.tracker.update(self.segmenter.observe(frame))
        anchor = self.anchor_px
        if anchor is None:
            direction = DirectionObservation(
                error_deg=None,
                confidence=0.0,
                cue_id=self.strategy,
                cue_disagreement_deg=None,
                valid=False,
                abstain_reason="anchor unavailable: E-ANCHOR PENDING",
            )
        else:
            direction = DIRECTION_STRATEGIES[self.strategy](arrow, anchor, self.forward_deg)
        arrival = self.arrival.observe(frame, map_id=map_id, approach_valid=approach_valid)
        return NavigationInputs(
            frame=frame,
            arrow=arrow,
            direction=direction,
            motion=None,
            arrival=arrival,
            forward_commanded=False,
        )


# ---------------------------------------------------------------------------
# Mode workers
# ---------------------------------------------------------------------------


def make_shadow_worker(
    pipeline_factory: Callable[[], PerceptionPipeline],
    gates: NavigationGates,
    *,
    max_ticks: int | None = None,
    tick_interval_s: float = 0.05,
) -> Callable[[WorkerContext], ModeResult]:
    """Shadow: the full decision path through a ``NoInputSession``.

    It records what it *would* have applied and can never reach a raw port.
    """

    def worker(context: WorkerContext) -> ModeResult:
        pipeline = pipeline_factory()
        navigator = Navigator(gates=gates)
        observer = context.observer
        ticks = 0
        proposed = 0
        while not context.cancellation.is_cancelled():
            if max_ticks is not None and ticks >= max_ticks:
                break
            ticks += 1
            envelope = context.frames.latest()
            if envelope is None:
                if context.cancellation.wait(tick_interval_s):
                    break
                continue
            inputs = pipeline.observe(envelope.frame, map_id="shadow", approach_valid=False)
            decision = navigator.decide(
                inputs, generation=context.generation, now_s=monotonic_s()
            )
            context.on_phase(decision.phase)
            if decision.command is not None and observer is not None:
                observer.propose(decision.command)
                proposed += 1
                context.on_status(f"WOULD_APPLY {decision.command.reason}")
            else:
                context.on_status(f"{decision.phase.name}: {decision.reason}")
            if context.cancellation.wait(tick_interval_s):
                break
        return ModeResult(
            ModeResultKind.CANCELLED
            if context.cancellation.is_cancelled()
            else ModeResultKind.COMPLETED,
            f"shadow observed {ticks} ticks, {proposed} proposed commands",
            evidence=(f"ticks={ticks}", f"proposed={proposed}"),
        )

    return worker


def make_live_worker(
    pipeline_factory: Callable[[], PerceptionPipeline],
    gates: NavigationGates,
    *,
    tick_interval_s: float = 0.05,
) -> Callable[[WorkerContext], ModeResult]:
    """Live navigation. Refuses to steer while its gates are pending.

    This is the whole point of the gate structure: the code path exists, is
    reviewable, and is exercised in Shadow, but it will not emit a movement
    command until the evidence for that OS/profile/condition says it may.
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

        pipeline = pipeline_factory()
        navigator = Navigator(gates=gates)
        applied = 0
        while not context.cancellation.is_cancelled():
            envelope = context.frames.latest()
            if envelope is None:
                if context.cancellation.wait(tick_interval_s):
                    break
                continue
            inputs = pipeline.observe(envelope.frame, map_id="live", approach_valid=True)
            decision = navigator.decide(
                inputs, generation=context.generation, now_s=monotonic_s()
            )
            context.on_phase(decision.phase)
            if decision.release or decision.command is None:
                session.release_navigation(decision.reason)
            else:
                result = session.apply_navigation_command(
                    decision.command, envelope.evidence_token
                )
                if result.applied:
                    applied += 1
                else:
                    session.release_navigation(f"apply-rejected:{result.detail}")
            if decision.phase is NavigationPhase.ARRIVED:
                return ModeResult(ModeResultKind.ARRIVED, "arrival confirmed")
            if decision.phase is NavigationPhase.ABANDONED:
                session.release_navigation("abandoned")
                return ModeResult(ModeResultKind.ABANDONED, decision.reason)
            if context.cancellation.wait(tick_interval_s):
                break
        session.release_navigation("worker-exit")
        return ModeResult(ModeResultKind.CANCELLED, f"live cancelled after {applied} commands")

    return worker
