"""The arrow follower: one controller, one control mode proof, one release rule.

The behaviour is deliberately small: turn on the spot until the heading error
is inside the alignment cone, then hold ``W`` while issuing small bounded
corrections, and release everything the moment the evidence stops supporting
either. Everything else in this module is the safety around that.

Four rules run through it.

**Shift Lock is a state, not a key.** It is something the player switched on,
and this code never presses or toggles Shift to "make sure". It is *verified*
per run - by a stable on-screen cue, or by a bounded stationary micro-yaw check
- and the proof is bound to the exact arm token, window, profile revision and
control fingerprint it was taken under. An unverified control mode means Live
is unavailable, not that Live guesses.

**W is a lease, not a state.** It is held for around a tenth of a second at a
time and every renewal needs a *strictly newer* accepted frame. A frame can
authorize exactly one renewal, so a frozen pipeline cannot keep the character
walking: the lease simply expires. An ``ALIGN`` command is a different kind and
may never hold forward at all.

**One pulse in flight.** A correction is not issued until the previous one has
been observed. The measured response carries its own latency, and issuing three
pulses inside that latency is how a controller overshoots and then hunts
forever. The rule is enforced by the pulse state machine, not by a comment.

**Alignment comes first, and stationary.** ``W`` cannot be acquired while the
heading error is outside the alignment cone, so a wrong direction costs a
rotation rather than a journey.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from enum import Enum

from prospector_engine.contracts import (
    ArrowObservation,
    CommandKind,
    ControlState,
    DirectionObservation,
    EvidenceStatus,
    Provenance,
    monotonic_s,
)
from prospector_engine.turning import (
    ControlFingerprint,
    TurnBackend,
    TurnLimits,
    TurnPlan,
    TurnResponse,
    wrap_deg,
)

__all__ = [
    "ArrowFollowerController",
    "ControlDecision",
    "ControlFingerprint",
    "ShiftLockProof",
    "SteeringInputs",
    "SteeringLimits",
    "wrap_deg",
]


# ---------------------------------------------------------------------------
# Control-mode proof
# ---------------------------------------------------------------------------


class ControlModeMethod(Enum):
    """How the control mode was verified. Both are *observations*.

    There is deliberately no ``ASSERTED``: a proof cannot be constructed by
    claiming one.
    """

    VISUAL_CUE = "visual_cue"
    """A stable on-screen cue the detector confirmed over several frames."""

    MICRO_YAW = "micro_yaw"
    """A bounded stationary yaw probe whose observed rotation matched the
    signature of a locked camera rather than a free one."""


@dataclass(frozen=True)
class ShiftLockProof:
    """Positive evidence that the player is in Shift Lock, right now, here.

    Bound to the run, the arm token, the window, the profile revision and the
    control fingerprint. Any of those changing invalidates it, because each one
    changes what the evidence was evidence *of*.
    """

    method: ControlModeMethod
    run_id: str
    arm_token_id: str
    generation: int
    window_identity: tuple[object, ...]
    fingerprint: ControlFingerprint
    observed_at_s: float
    confidence: float
    evidence: tuple[str, ...] = ()
    status: EvidenceStatus = EvidenceStatus.PENDING

    #: How long a proof stays good without being re-observed. Shift Lock can be
    #: toggled by the player at any moment, so this is short.
    MAX_AGE_S = 20.0

    def valid_for(
        self,
        *,
        run_id: str,
        arm_token_id: str,
        generation: int,
        window_identity: tuple[object, ...],
        fingerprint: ControlFingerprint,
        now_s: float | None = None,
    ) -> tuple[bool, str]:
        """Whether this proof still covers the situation it is being used in."""
        now = now_s if now_s is not None else monotonic_s()
        if self.status is not EvidenceStatus.VALIDATED:
            return (False, f"Shift Lock proof is {self.status.value}")
        if self.run_id != run_id:
            return (False, "Shift Lock proof belongs to a previous run")
        if self.arm_token_id != arm_token_id:
            return (False, "Shift Lock proof belongs to a previous arm")
        if self.generation != generation:
            return (False, "Shift Lock proof belongs to a previous generation")
        if self.window_identity != window_identity:
            return (False, "the Roblox window changed since Shift Lock was verified")
        mismatches = self.fingerprint.mismatches(fingerprint)
        if mismatches:
            return (False, f"conditions changed since verification: {mismatches[0]}")
        if now - self.observed_at_s > self.MAX_AGE_S:
            return (False, "Shift Lock has not been re-observed recently")
        return (True, "verified")


# ---------------------------------------------------------------------------
# Controller configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SteeringLimits:
    """Bounds on what the controller may ask for.

    The alignment cone and the deadband are *provisional bounds*, not tuned
    constants: the deadband is floored at a multiple of the actuator resolution
    the characterizer actually measured, so a machine with a coarse actuator
    gets a correspondingly wider band without anybody typing a number.
    """

    #: Heading error inside which the character may walk. Outside it, the
    #: controller turns on the spot.
    align_threshold_deg: float = 8.0
    #: Extra error required to leave FOLLOW once inside, so a noisy estimate
    #: cannot chatter between walking and turning.
    align_hysteresis_deg: float = 5.0
    #: Consecutive accepted frames inside the cone before W may be taken.
    align_confirm_frames: int = 3
    #: Heading error inside which no correction is requested at all. Floored at
    #: a multiple of the measured minimum effective actuator movement, because
    #: asking for a rotation smaller than the actuator can produce yields the
    #: actuator's minimum instead - which overshoots, reverses, and dithers.
    yaw_deadband_deg: float = 2.5
    #: Multiple of the actuator resolution the deadband may never go below.
    deadband_resolution_multiple: float = 1.5

    kp: float = 0.55
    kd: float = 0.10
    derivative_filter: float = 0.6

    #: Total rotation one alignment episode may ask for before it gives up.
    #: A controller that has turned 540 degrees is not converging.
    max_episode_yaw_deg: float = 540.0
    #: Consecutive observed pulses whose error grew before the controller
    #: abandons the episode rather than continuing to push the wrong way.
    max_growing_pulses: int = 3
    #: Fractional error growth that counts as "the correction made it worse".
    growth_tolerance: float = 1.15

    #: The W lease. Renewal is attempted well inside the hard horizon, and the
    #: horizon itself is what the authority will not exceed.
    lease_renew_ms: int = 100
    lease_horizon_ms: int = 250

    #: How stale accepted evidence may be before the controller releases.
    max_evidence_age_ms: int = 100
    #: Minimum processed frames per second before Live is refused.
    min_processed_fps: int = 30
    #: How long FOLLOW may keep walking with no usable arrow reading.
    #:
    #: It was two *frames*, which is a different quantity at every cadence: at
    #: 60 fps it is 33 ms of tolerance, and the character stopped dead every
    #: time the character model crossed the map. The arrow disappearing behind
    #: something for a moment is not a reason to stop - the heading was right
    #: an instant ago and the character is already walking it - so the grace is
    #: a duration, measured on the monotonic clock like every other rate here.
    #:
    #: Yaw is *not* graced. Turning blind is never justified: a correction
    #: computed from a heading that no longer exists is worse than no
    #: correction at all. Only the forward hold coasts.
    arrow_loss_grace_s: float = 2.0
    #: How long the controller may sit in REACQUIRE with no usable arrow
    #: before it says so and stops rather than waiting forever. Standing still
    #: indefinitely with nothing on screen explaining it is the failure this
    #: bound exists to prevent; the navigator turns it into a bounded
    #: reacquisition episode, and that episode has its own budget.
    arrow_loss_abandon_s: float = 10.0
    #: Direction confidence below which the controller releases rather than
    #: scaling the correction down. Confidence *scaling* is right for an
    #: estimate that is merely uncertain; a collapsed one is not evidence at
    #: all, and steering on it is steering on noise. Set well below the range
    #: accepted readings occupy on the real-frame corpus (0.7-0.9).
    min_direction_confidence: float = 0.35

    #: Fraction of the client the cursor must stay inside. Outside it, yaw and
    #: W release and the pointer is recentred before anything resumes.
    safe_region_fraction: float = 0.72

    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="TREASURE_NAVIGATION_PLAN.md section 9.1; mission section D",
            note=(
                "bounds chosen to be conservative; the deadband floor is derived from "
                "the measured actuator resolution rather than configured"
            ),
        )
    )


@dataclass(frozen=True)
class SteeringInputs:
    """Everything one controller tick is allowed to look at.

    Passed in rather than reached for, so the controller has no way to consult
    something stale: if a value is not in here, it did not come from this frame.
    """

    arrow: ArrowObservation
    direction: DirectionObservation
    frame_sequence: int
    frame_age_ms: float
    now_s: float
    focus_ok: bool
    viewport_ok: bool
    processed_fps: float
    #: Whether the pointer is inside the verified safe region of the client.
    cursor_safe: bool = True
    #: The coordinate basis and profile this frame belongs to. A change in
    #: either invalidates the controller's memory rather than being absorbed.
    geometry_revision: int = 0
    profile_revision: int = 0
    #: Set by the caller when a fault has already been raised elsewhere.
    fault: str | None = None


@dataclass(frozen=True)
class ControlDecision:
    """One controller tick: what to do, and why.

    ``release`` is separate from ``forward``/``plan`` on purpose. "Do nothing"
    and "release everything now" are different instructions, and conflating
    them is how a held key survives a fault.
    """

    state: ControlState
    kind: CommandKind
    forward: int
    plan: TurnPlan
    release: bool
    reason: str
    blockers: tuple[str, ...] = ()
    #: Signed heading error this decision was made from, for the dashboard.
    error_deg: float | None = None
    #: The arrow has been unreadable past its whole bounded grace. A flag
    #: rather than a phrase the caller has to match on, because "look for the
    #: arrow again" and "something is wrong" are different instructions and
    #: telling them apart by parsing a sentence is how they get confused.
    lost_target: bool = False

    @property
    def moves(self) -> bool:
        return self.forward != 0 or self.plan.moves

    @property
    def yaw_deg(self) -> float:
        return self.plan.expected_deg


def _released(
    state: ControlState,
    reason: str,
    blockers: tuple[str, ...] = (),
    backend: TurnBackend = TurnBackend.MOUSE_YAW,
) -> ControlDecision:
    return ControlDecision(
        state=state,
        kind=CommandKind.RELEASE,
        forward=0,
        plan=TurnPlan.none(backend),
        release=True,
        reason=reason,
        blockers=blockers,
    )


# ---------------------------------------------------------------------------
# The controller
# ---------------------------------------------------------------------------


@dataclass
class _Pulse:
    """One correction in flight: what was asked, and when it can be read back."""

    units: int
    error_before_deg: float
    issued_at_s: float
    holds_until_s: float
    observe_until_s: float


class ArrowFollowerController:
    """ACQUIRE -> ALIGN -> FOLLOW, with everything else releasing first.

    The state machine is small because the interesting behaviour is in what
    makes it release. Turning is stationary, walking requires sustained
    alignment, and every transition out of FOLLOW drops ``W`` in the same tick
    rather than at the end of the next one.

    It is **cadence independent**: every rate limit is applied against measured
    monotonic time, so the same route behaves the same at 30 and at 120 frames
    per second. Duplicate or missing frames freeze the derivative rather than
    letting a zero delta-t manufacture a spike.

    It is **actuator agnostic**: it asks for degrees, and the measured
    :class:`~prospector_engine.turning.TurnResponse` turns degrees into either a
    held arrow key or a relative mouse delta. Swapping backends changes nothing
    here, which is the point of measuring the response separately.
    """

    def __init__(
        self,
        limits: SteeringLimits | None = None,
        response: TurnResponse | None = None,
        turn_limits: TurnLimits | None = None,
    ) -> None:
        self._limits = limits or SteeringLimits()
        self._turn_limits = turn_limits or TurnLimits()
        self._response = response
        # Deliberately outside ``reset()``. The loss clock has to survive the
        # release that losing the arrow causes, or every release would restart
        # it and the controller would coast for two seconds, release, coast for
        # two seconds again, forever - which is the *opposite* of a bound.
        self._arrow_lost_since_s: float | None = None
        self._last_stable_error_deg: float | None = None
        self.reset()

    # -- lifecycle --------------------------------------------------------
    def reset(self) -> None:
        """Drop every piece of controller memory. Called on any release."""
        self._state = ControlState.ACQUIRE
        self._last_error_deg: float | None = None
        self._last_time_s: float | None = None
        self._filtered_derivative = 0.0
        self._aligned_frames = 0
        self._episode_yaw_deg = 0.0
        self._consumed_sequence = -1
        self._pulse: _Pulse | None = None
        self._growing_pulses = 0
        self._track_id: int | None = None
        self._geometry_revision: int | None = None
        self._profile_revision: int | None = None

    @property
    def state(self) -> ControlState:
        return self._state

    @property
    def limits(self) -> SteeringLimits:
        return self._limits

    @property
    def response(self) -> TurnResponse | None:
        return self._response

    @property
    def backend(self) -> TurnBackend | None:
        return self._response.backend if self._response is not None else None

    @property
    def episode_yaw_deg(self) -> float:
        return self._episode_yaw_deg

    @property
    def holds_forward(self) -> bool:
        return self._state.holds_forward

    def set_response(self, response: TurnResponse | None) -> None:
        """Adopt a freshly measured turn response and drop stale pulse memory."""
        self._response = response
        self._pulse = None
        self._growing_pulses = 0

    def blocking_reasons(self) -> tuple[str, ...]:
        """Why the controller cannot steer, in plain language."""
        response = self._response
        if response is None:
            return ("the turn actuator has not been characterized yet",)
        if not response.usable:
            return (f"the measured turn response is {response.status.value}",)
        return ()

    # -- the tick ---------------------------------------------------------
    def update(self, inputs: SteeringInputs) -> ControlDecision:
        """One decision. Every early return releases; none of them coasts."""
        limits = self._limits
        backend = self.backend or TurnBackend.MOUSE_YAW

        # 1. Safety. Anything here releases before anything else is considered.
        if inputs.fault:
            return self._release(ControlState.SAFE_STOP, f"fault: {inputs.fault}")
        if not inputs.viewport_ok:
            return self._release(ControlState.SAFE_STOP, "viewport is not usable")
        if not inputs.focus_ok:
            return self._release(ControlState.SAFE_STOP, "Roblox is not focused")
        if inputs.frame_age_ms > limits.max_evidence_age_ms:
            return self._release(
                ControlState.REACQUIRE, f"evidence is {inputs.frame_age_ms:.0f} ms old"
            )
        if inputs.processed_fps < limits.min_processed_fps:
            return self._release(
                ControlState.SAFE_STOP,
                f"only {inputs.processed_fps:.0f} processed fps; Live needs "
                f"{limits.min_processed_fps}",
            )
        if not inputs.cursor_safe:
            # Release first, recentre second. The pointer leaving the safe
            # region while W is held is how a drag ends up outside the window.
            return self._release(
                ControlState.BLOCKED, "pointer left the safe region; recentring"
            )

        # 2. The coordinate basis. A changed geometry or profile means every
        # remembered angle was measured in a frame that no longer exists.
        if self._geometry_revision is None:
            self._geometry_revision = inputs.geometry_revision
        elif inputs.geometry_revision != self._geometry_revision:
            return self._release(ControlState.REACQUIRE, "the viewport changed")
        if self._profile_revision is None:
            self._profile_revision = inputs.profile_revision
        elif inputs.profile_revision != self._profile_revision:
            return self._release(ControlState.REACQUIRE, "the arrow profile changed")

        # 3. Evidence. A frame authorizes exactly one decision, ever.
        if inputs.frame_sequence <= self._consumed_sequence:
            return ControlDecision(
                state=self._state,
                kind=CommandKind.RELEASE,
                forward=0,
                plan=TurnPlan.none(backend),
                release=False,
                reason="no newer frame; the lease is left to expire on its own",
            )

        blockers = self.blocking_reasons()
        if blockers:
            return self._release(
                ControlState.SAFE_STOP, "steering is not characterized", blockers
            )

        if not inputs.arrow.valid:
            return self._on_arrow_lost(
                inputs, backend, inputs.arrow.abstain_reason or "no reading"
            )
        if inputs.arrow.track_id != self._track_id:
            # A different arrow identity is a different target. Drop the pulse
            # in flight rather than crediting its rotation to the new one.
            self._track_id = inputs.arrow.track_id
            self._pulse = None
            self._growing_pulses = 0
        # A rejected *heading* is the same fact as a missing arrow as far as
        # the forward hold is concerned: there is nothing to steer by this
        # frame. Releasing here while the arrow branch coasted meant a walk
        # stopped dead on a single abstaining direction estimate, which is the
        # commoner of the two and was not covered by any grace at all.
        if not inputs.direction.valid or inputs.direction.error_deg is None:
            return self._on_arrow_lost(
                inputs, backend, f"direction abstained: {inputs.direction.abstain_reason}"
            )
        if inputs.direction.confidence < limits.min_direction_confidence:
            return self._on_arrow_lost(
                inputs,
                backend,
                f"direction confidence collapsed to {inputs.direction.confidence:.2f}",
            )

        # A usable reading. The loss clock is cleared here, and only here.
        self._arrow_lost_since_s = None
        self._consumed_sequence = inputs.frame_sequence
        error = wrap_deg(inputs.direction.error_deg)
        if self._episode_yaw_deg > limits.max_episode_yaw_deg:
            return self._release(
                ControlState.SAFE_STOP,
                f"turned {self._episode_yaw_deg:.0f} degrees without converging",
            )

        self._last_stable_error_deg = error
        settled = self._settle_pulse(error, inputs.now_s)
        if settled is not None:
            return settled

        aligned = self._track_alignment(error)
        plan = self._plan_for(error, inputs)

        if aligned and self._aligned_frames >= limits.align_confirm_frames:
            self._state = ControlState.FOLLOW
            return ControlDecision(
                state=ControlState.FOLLOW,
                kind=CommandKind.FOLLOW,
                forward=1,
                plan=plan,
                release=False,
                reason=f"aligned within {abs(error):.1f} degrees; walking",
                error_deg=error,
            )

        # Alignment is stationary by construction: W is never taken outside the
        # cone, so a wrong heading costs a rotation rather than a journey.
        self._state = ControlState.ALIGN
        return ControlDecision(
            state=ControlState.ALIGN,
            kind=CommandKind.ALIGN,
            forward=0,
            plan=plan,
            release=False,
            reason=(
                f"turning {plan.expected_deg:+.1f} degrees to close {error:+.1f}"
                if plan.moves
                else f"waiting out the last correction ({error:+.1f} to close)"
            ),
            error_deg=error,
        )

    # -- internals --------------------------------------------------------
    def _on_arrow_lost(
        self, inputs: SteeringInputs, backend: TurnBackend, why: str
    ) -> ControlDecision:
        """A frame with no usable heading. Yaw stops at once; W gets a grace.

        Turning blind is never justified: the correction was computed from a
        heading that no longer exists. Walking blind for a second or two is,
        because the character is already going the right way and stopping dead
        every time the map arrow passes behind something is what makes a route
        stutter. So the forward hold coasts on the last stable heading and the
        turn plan is empty for the whole grace.

        The grace is bounded twice: by ``arrow_loss_grace_s`` for the coast,
        and by ``arrow_loss_abandon_s`` for how long the controller may then
        sit in REACQUIRE before saying so out loud.
        """
        self._consumed_sequence = inputs.frame_sequence
        self._pulse = None
        if self._arrow_lost_since_s is None:
            self._arrow_lost_since_s = inputs.now_s
        lost_s = inputs.now_s - self._arrow_lost_since_s
        if self._state is ControlState.FOLLOW and lost_s <= self._limits.arrow_loss_grace_s:
            heading = self._last_stable_error_deg
            held = "" if heading is None else f" on {heading:+.1f} degrees"
            return ControlDecision(
                state=ControlState.FOLLOW,
                kind=CommandKind.FOLLOW,
                forward=1,
                plan=TurnPlan.none(backend),
                release=False,
                reason=(
                    f"arrow lost for {lost_s * 1000:.0f} ms; holding course{held}, "
                    "turning released"
                ),
                error_deg=heading,
            )
        if lost_s > self._limits.arrow_loss_abandon_s:
            released = self._release(
                ControlState.REACQUIRE,
                f"the arrow has not been readable for {lost_s:.0f} s ({why})",
            )
            return replace(released, lost_target=True)
        return self._release(ControlState.REACQUIRE, f"arrow abstained: {why}")

    def _release(
        self, state: ControlState, reason: str, blockers: tuple[str, ...] = ()
    ) -> ControlDecision:
        backend = self.backend or TurnBackend.MOUSE_YAW
        response = self._response
        self.reset()
        self._response = response
        self._state = state
        return _released(state, reason, blockers, backend)

    def _effective_deadband_deg(self) -> float:
        """The configured deadband, never below the actuator's own resolution.

        Derived from the *measured* response rather than assumed, so a machine
        whose smallest effective movement is coarse gets a correspondingly
        wider band and does not dither.
        """
        response = self._response
        floor = 0.0
        if response is not None and response.usable:
            floor = (
                response.degrees_per_unit
                * response.min_effective_units
                * self._limits.deadband_resolution_multiple
            )
        return max(self._limits.yaw_deadband_deg, floor)

    def _track_alignment(self, error: float) -> bool:
        """Deadband with hysteresis, counted in *frames* not seconds."""
        limits = self._limits
        threshold = limits.align_threshold_deg
        if self._state is ControlState.FOLLOW:
            threshold += limits.align_hysteresis_deg
        if abs(error) <= threshold:
            self._aligned_frames += 1
            return True
        self._aligned_frames = 0
        return False

    def _settle_pulse(self, error: float, now_s: float) -> ControlDecision | None:
        """Close out the correction in flight, if there is one.

        Returns a decision when the controller must wait; ``None`` when the
        pulse has been observed (or there was none) and a new one may be
        planned. This is the whole of the one-pulse-in-flight rule.
        """
        pulse = self._pulse
        if pulse is None:
            return None
        backend = self.backend or TurnBackend.MOUSE_YAW
        if now_s < pulse.holds_until_s:
            # A held-key pulse still has time to run: keep the same key down
            # rather than releasing and re-pressing it every frame.
            axis = 1 if pulse.units > 0 else -1
            plan = TurnPlan(
                backend=backend,
                turn_axis=axis if backend is TurnBackend.ARROW_KEYS else 0,
                yaw_delta_px=0,
                hold_ms=max(1, int((pulse.holds_until_s - now_s) * 1000.0)),
                requested_deg=0.0,
                expected_deg=0.0,
            )
            forward = 1 if self._state is ControlState.FOLLOW else 0
            return ControlDecision(
                state=self._state,
                kind=CommandKind.FOLLOW if forward else CommandKind.ALIGN,
                forward=forward,
                plan=plan,
                release=False,
                reason="continuing the correction in flight",
                error_deg=error,
            )
        if now_s < pulse.observe_until_s:
            forward = 1 if self._state is ControlState.FOLLOW else 0
            return ControlDecision(
                state=self._state,
                kind=CommandKind.FOLLOW if forward else CommandKind.ALIGN,
                forward=forward,
                plan=TurnPlan.none(backend),
                release=False,
                reason="observing the last correction before the next one",
                error_deg=error,
            )

        observed = -wrap_deg(error - pulse.error_before_deg)
        self._pulse = None
        if self._response is not None:
            self._response = self._response.with_observation(pulse.units, observed)
        if abs(error) > abs(pulse.error_before_deg) * self._limits.growth_tolerance:
            self._growing_pulses += 1
            if self._growing_pulses >= self._limits.max_growing_pulses:
                return self._release(
                    ControlState.REACQUIRE,
                    f"the error grew over {self._growing_pulses} corrections; reacquiring",
                )
        else:
            self._growing_pulses = 0
        return None

    def _plan_for(self, error: float, inputs: SteeringInputs) -> TurnPlan:
        """Filtered PD, every bound in turn, then the measured actuator.

        Confidence scales the output down and never up: an uncertain estimate
        may justify a smaller correction, never a larger one.
        """
        limits = self._limits
        backend = self.backend or TurnBackend.MOUSE_YAW
        response = self._response
        now = inputs.now_s
        delta_t = (
            now - self._last_time_s
            if self._last_time_s is not None and now > self._last_time_s
            else None
        )

        derivative = self._filtered_derivative
        if delta_t is not None and self._last_error_deg is not None and delta_t > 1e-6:
            raw = wrap_deg(error - self._last_error_deg) / delta_t
            alpha = limits.derivative_filter
            derivative = alpha * self._filtered_derivative + (1.0 - alpha) * raw
        self._filtered_derivative = derivative
        self._last_error_deg = error
        self._last_time_s = now

        if abs(error) <= self._effective_deadband_deg():
            return TurnPlan.none(backend, requested_deg=0.0)
        if response is None or not response.usable:
            return TurnPlan.none(backend, requested_deg=error)

        command = limits.kp * error + limits.kd * derivative
        command *= max(0.0, min(1.0, inputs.direction.confidence))
        # Never ask for more rotation than remains: a correction bigger than
        # the error is an overshoot by construction.
        command = math.copysign(min(abs(command), abs(error)), command)
        plan = response.plan_for(command, self._turn_limits)
        if not plan.moves:
            return plan

        self._episode_yaw_deg += abs(plan.expected_deg)
        hold_s = plan.hold_ms / 1000.0 if backend is TurnBackend.ARROW_KEYS else 0.0
        self._pulse = _Pulse(
            units=plan.units,
            error_before_deg=error,
            issued_at_s=now,
            holds_until_s=now + hold_s,
            observe_until_s=now + hold_s + response.latency_s,
        )
        return plan
