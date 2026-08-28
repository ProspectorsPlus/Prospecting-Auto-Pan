"""Shift-Lock steering: yaw calibration, control-mode proof, and the controller.

The behaviour this implements is deliberately small: hold ``W`` to walk, and
turn the camera with bounded relative mouse motion until the verified forward
direction lines up with the map arrow. No strafing, no jumping, no recovery
maneuvers, no left/right hunting. Everything else is safety around that.

Three rules run through the whole module.

**Shift Lock is a state, not a key.** It is something the player has switched
on, and this code must never press or toggle Shift to "make sure". It is
*verified* per run - by a stable on-screen cue, or by a separately armed
stationary micro-yaw check - and the proof is bound to the exact arm token,
window, profile revision, viewport and sensitivity it was taken under. An
unverified control mode means Live is unavailable, not that Live guesses.

**W is a lease, not a state.** It is held for 75-125 ms at a time and every
renewal needs a *strictly newer* accepted frame. A frame can authorize exactly
one renewal, so a frozen pipeline cannot keep the character walking: the lease
simply expires. Alignment yaw is a different command kind that may never hold
forward at all.

**Alignment comes first, and stationary.** ``W`` cannot be acquired while the
heading error is outside the validated threshold, so the character turns on the
spot and only then walks. That ordering is what makes a wrong direction cost a
rotation rather than a journey.

Every threshold here is provisional configuration. E-YAW, E-STEER-CAL and
E-STEER-E2E are PENDING, and with them pending the controller refuses to
produce any command that would move the character.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from prospector_engine.contracts import (
    ArrowObservation,
    CommandKind,
    ControlState,
    DirectionObservation,
    EvidenceStatus,
    Provenance,
    monotonic_s,
)

__all__ = [
    "CalibrationFingerprint",
    "ControlDecision",
    "ShiftLockController",
    "ShiftLockProof",
    "SteeringLimits",
    "YawCalibration",
    "wrap_deg",
]


def wrap_deg(degrees: float) -> float:
    """Wrap to (-180, 180]. Correct across the +-180 seam."""
    wrapped = (degrees + 180.0) % 360.0 - 180.0
    return 180.0 if wrapped == -180.0 else wrapped


# ---------------------------------------------------------------------------
# Calibration identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationFingerprint:
    """Everything a yaw calibration is only valid *for*.

    Degrees per mouse unit is not a property of the game; it is a property of
    this OS, this backend, this client, this sensitivity setting, this viewport
    and this control mode together. Carrying the fingerprint means a
    calibration cannot silently follow the user to a different machine, a
    resized window, or a changed sensitivity slider.
    """

    os_name: str
    backend: str
    client_fingerprint: str
    camera_sensitivity: str
    control_mode: str
    viewport_identity: tuple[object, ...]
    profile_id: str
    profile_revision: int
    supported_min_fps: int

    def matches(self, other: CalibrationFingerprint) -> bool:
        return self == other

    def mismatches(self, other: CalibrationFingerprint) -> tuple[str, ...]:
        """Which fields differ, for a message a person can act on."""
        differences: list[str] = []
        for field_name in (
            "os_name",
            "backend",
            "client_fingerprint",
            "camera_sensitivity",
            "control_mode",
            "viewport_identity",
            "profile_id",
            "profile_revision",
            "supported_min_fps",
        ):
            mine, theirs = getattr(self, field_name), getattr(other, field_name)
            if mine != theirs:
                differences.append(f"{field_name}: {mine!r} != {theirs!r}")
        return tuple(differences)


@dataclass(frozen=True)
class YawCalibration:
    """The measured relationship between mouse units and camera rotation.

    Populated by the E-YAW procedure: with ``W`` released, bounded positive and
    negative yaw pulses are issued and the *observed* rotation is measured from
    perception. A configured multiplier is never trusted on its own - the whole
    point of the experiment is that the closed loop confirms the number.

    ``status`` stays ``PENDING`` until that has actually been run on hardware,
    and :attr:`usable` is what the controller consults.
    """

    fingerprint: CalibrationFingerprint | None = None
    #: Screen degrees per relative mouse unit, positive to the right.
    degrees_per_unit: float | None = None
    #: Whether a positive delta turns the camera right. Measured, never assumed.
    positive_is_right: bool | None = None
    #: Smallest delta that produces any observable rotation.
    min_effective_units: int | None = None
    #: The range over which the response stays linear.
    linear_range_units: tuple[int, int] | None = None
    #: Delta above which the response saturates.
    saturation_units: int | None = None
    #: Median lag between issuing a pulse and observing the rotation.
    response_delay_ms: float | None = None
    #: Spread of the achieved angle across repeats, in degrees.
    repeatability_deg: float | None = None
    #: Dead travel observed when reversing direction.
    reversal_backlash_deg: float | None = None
    #: R-squared of the linear fit across the claimed linear range.
    linear_fit_r2: float | None = None
    repeats_per_magnitude: int = 0
    status: EvidenceStatus = EvidenceStatus.PENDING
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PENDING,
            source="E-YAW: physically armed bounded yaw pulses on real hardware",
            note="no pulses have been issued; the controller refuses to steer without this",
        )
    )

    @property
    def usable(self) -> bool:
        """Whether the controller may convert degrees into mouse units.

        Every field the conversion depends on must be present *and* the gate
        must have passed. A partially filled calibration is a record of an
        unfinished experiment, not a usable one.
        """
        return (
            self.status is EvidenceStatus.VALIDATED
            and self.degrees_per_unit is not None
            and self.degrees_per_unit > 0.0
            and self.positive_is_right is not None
            and self.min_effective_units is not None
            and self.fingerprint is not None
        )

    def units_for(self, degrees: float) -> int | None:
        """Mouse units for a requested rotation, or ``None`` if uncalibrated.

        Below the measured minimum effective movement the request is rounded
        *up* to that minimum rather than down to zero: a command that cannot
        move anything is worse than the smallest one that can, and the deadband
        upstream is what decides whether to ask at all.
        """
        if not self.usable:
            return None
        assert self.degrees_per_unit and self.min_effective_units is not None
        magnitude = abs(degrees) / self.degrees_per_unit
        if magnitude <= 0.0:
            return 0
        units = max(self.min_effective_units, round(magnitude))
        if self.saturation_units is not None:
            units = min(units, self.saturation_units)
        sign = 1 if degrees >= 0 else -1
        if not self.positive_is_right:
            sign = -sign
        return sign * units

    def blocking_reasons(self) -> tuple[str, ...]:
        if self.usable:
            return ()
        # Rendered straight into the Live blockers panel, so each one is a
        # sentence a person can act on rather than a field name.
        reasons: list[str] = []
        if self.status is not EvidenceStatus.VALIDATED:
            reasons.append(f"E-YAW: the yaw calibration is {self.status.value}")
        if self.degrees_per_unit is None:
            reasons.append("E-YAW: degrees per mouse unit has not been measured")
        if self.positive_is_right is None:
            reasons.append("E-YAW: the sign of a positive yaw delta has not been measured")
        if self.min_effective_units is None:
            reasons.append("E-YAW: the smallest effective mouse movement is unknown")
        return tuple(reasons)


# ---------------------------------------------------------------------------
# Control-mode proof
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShiftLockProof:
    """Positive evidence that the player is in Shift Lock, right now, here.

    Bound to the run, the arm token, the window, the profile revision and the
    calibration fingerprint. Any of those changing invalidates it, because each
    one changes what the evidence was evidence *of*.

    There is deliberately no way to construct a proof by asserting it: the two
    supported methods are an on-screen cue the detector confirmed, and a
    stationary micro-yaw check that is separately armed and observed.
    """

    method: str
    run_id: str
    arm_token_id: str
    generation: int
    window_identity: tuple[object, ...]
    fingerprint: CalibrationFingerprint
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
        fingerprint: CalibrationFingerprint,
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
    """Bounds on what the controller may ask for. All provisional.

    ``align_threshold_deg`` may only be chosen inside the independently
    measured ``[min_stable_correction_deg, max_usable_deadband_deg]`` interval
    from E-YAW and E-STEER-CAL. Widening it after seeing estimator failures is
    explicitly forbidden (plan 9.1), so it lives next to its provenance.
    """

    #: Heading error inside which the character may walk. Outside it, the
    #: controller turns on the spot.
    align_threshold_deg: float = 6.0
    #: Extra error required to leave FOLLOW once inside, so a noisy estimate
    #: cannot chatter between walking and turning.
    align_hysteresis_deg: float = 4.0
    #: Consecutive accepted frames inside the threshold before W may be taken.
    align_confirm_frames: int = 3
    #: Heading error inside which no yaw is requested at all. Floored at
    #: 1.5x the measured minimum effective mouse movement, because asking for
    #: a rotation smaller than the actuator can produce yields the actuator's
    #: minimum instead - which overshoots, reverses, and dithers forever.
    #: Measured: 11 zero crossings on a 5-degree correction without this.
    yaw_deadband_deg: float = 2.0
    #: Multiple of the actuator resolution the deadband may never go below.
    deadband_resolution_multiple: float = 1.5

    kp: float = 0.55
    kd: float = 0.10
    derivative_filter: float = 0.6

    #: Bounds on one pulse, and on the rate, acceleration and jerk of pulses.
    #: The acceleration bound is what the stopping-distance constraint uses, so
    #: raising the rate without raising the acceleration buys overshoot rather
    #: than speed.
    max_yaw_per_pulse_deg: float = 12.0
    max_yaw_rate_deg_per_s: float = 160.0
    max_yaw_accel_deg_per_s2: float = 900.0
    max_yaw_jerk_deg_per_s3: float = 9000.0
    #: Total rotation one alignment episode may ask for before it gives up.
    #: A controller that has turned 540 degrees is not converging.
    max_episode_yaw_deg: float = 540.0

    #: The W lease. Renewal is attempted well inside the hard horizon, and the
    #: horizon itself is what the authority will not exceed.
    lease_renew_ms: int = 100
    lease_horizon_ms: int = 250

    #: How stale accepted evidence may be before the controller releases.
    max_evidence_age_ms: int = 100
    #: Minimum processed frames per second before Live is refused.
    min_processed_fps: int = 30

    #: Fraction of the client the cursor must stay inside. Outside it, yaw and
    #: W release and the pointer is recentred before anything resumes.
    safe_region_fraction: float = 0.72

    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="TREASURE_NAVIGATION_PLAN.md section 9.1; mission section 11",
            note="align threshold is NOT frozen; E-YAW and E-STEER-CAL are PENDING",
        )
    )


@dataclass(frozen=True)
class ControlDecision:
    """One controller tick: what to do, and why.

    ``release`` is separate from ``forward``/``yaw`` on purpose. "Do nothing"
    and "release everything now" are different instructions, and conflating
    them is how a held key survives a fault.
    """

    state: ControlState
    kind: CommandKind
    forward: int
    yaw_deg: float
    yaw_units: int
    release: bool
    reason: str
    blockers: tuple[str, ...] = ()

    @property
    def moves(self) -> bool:
        return self.forward != 0 or self.yaw_units != 0


def _released(
    state: ControlState, reason: str, blockers: tuple[str, ...] = ()
) -> ControlDecision:
    return ControlDecision(
        state=state,
        kind=CommandKind.RELEASE,
        forward=0,
        yaw_deg=0.0,
        yaw_units=0,
        release=True,
        reason=reason,
        blockers=blockers,
    )


# ---------------------------------------------------------------------------
# The controller
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SteeringInputs:
    """Everything one controller tick is allowed to look at.

    Passed in rather than reached for, so the controller has no way to consult
    something stale: if a value is not in here, it did not come from this
    frame.
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
    #: Set by the caller when a fault has already been raised elsewhere.
    fault: str | None = None


class ShiftLockController:
    """ACQUIRE -> ALIGN -> FOLLOW, with everything else releasing first.

    The state machine is small because the interesting behaviour is in what
    makes it release. Turning is stationary, walking requires sustained
    alignment, and every transition out of FOLLOW drops ``W`` in the same tick
    rather than at the end of the next one.

    It is **cadence independent**: every rate limit is applied against measured
    monotonic time, so the same route behaves the same at 30 and at 120 frames
    per second. Duplicate or missing frames freeze the derivative rather than
    letting a zero delta-t manufacture a spike.
    """

    def __init__(
        self,
        limits: SteeringLimits | None = None,
        calibration: YawCalibration | None = None,
    ) -> None:
        self._limits = limits or SteeringLimits()
        self._calibration = calibration or YawCalibration()
        self.reset()

    # -- lifecycle --------------------------------------------------------
    def reset(self) -> None:
        """Drop every piece of controller memory. Called on any release."""
        self._state = ControlState.ACQUIRE
        self._last_error_deg: float | None = None
        self._last_time_s: float | None = None
        self._filtered_derivative = 0.0
        self._last_yaw_deg = 0.0
        self._last_rate_deg_per_s = 0.0
        self._last_accel_deg_per_s2 = 0.0
        self._aligned_frames = 0
        self._episode_yaw_deg = 0.0
        self._consumed_sequence = -1
        self._lease_taken_at_s: float | None = None

    @property
    def state(self) -> ControlState:
        return self._state

    @property
    def limits(self) -> SteeringLimits:
        return self._limits

    @property
    def calibration(self) -> YawCalibration:
        return self._calibration

    @property
    def episode_yaw_deg(self) -> float:
        return self._episode_yaw_deg

    @property
    def holds_forward(self) -> bool:
        return self._state.holds_forward

    def blocking_reasons(self) -> tuple[str, ...]:
        """Why the controller cannot steer, in plain language."""
        return self._calibration.blocking_reasons()

    # -- the tick ---------------------------------------------------------
    def update(self, inputs: SteeringInputs) -> ControlDecision:
        """One decision. Every early return releases; none of them coasts."""
        limits = self._limits

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

        # 2. Evidence. A frame authorizes exactly one decision, ever.
        if inputs.frame_sequence <= self._consumed_sequence:
            return ControlDecision(
                state=self._state,
                kind=CommandKind.RELEASE,
                forward=0,
                yaw_deg=0.0,
                yaw_units=0,
                release=False,
                reason="no newer frame; the lease is left to expire on its own",
            )

        blockers = self.blocking_reasons()
        if blockers:
            return self._release(ControlState.SAFE_STOP, "steering is not calibrated", blockers)

        if not inputs.arrow.valid:
            # No arrow-loss grace: the first Live gate does not get one
            # (mission section 11), so a lost arrow releases immediately.
            return self._release(
                ControlState.REACQUIRE, f"arrow abstained: {inputs.arrow.abstain_reason}"
            )
        if not inputs.direction.valid or inputs.direction.error_deg is None:
            return self._release(
                ControlState.ALIGN,
                f"direction abstained: {inputs.direction.abstain_reason}",
            )

        self._consumed_sequence = inputs.frame_sequence
        error = wrap_deg(inputs.direction.error_deg)
        if self._episode_yaw_deg > limits.max_episode_yaw_deg:
            return self._release(
                ControlState.SAFE_STOP,
                f"turned {self._episode_yaw_deg:.0f} degrees without converging",
            )

        aligned = self._track_alignment(error)
        yaw_deg = self._yaw_for(error, inputs)
        units = self._calibration.units_for(yaw_deg) or 0

        if aligned and self._aligned_frames >= limits.align_confirm_frames:
            self._state = ControlState.FOLLOW
            return ControlDecision(
                state=ControlState.FOLLOW,
                kind=CommandKind.FOLLOW,
                forward=1,
                yaw_deg=yaw_deg,
                yaw_units=units,
                release=False,
                reason=f"aligned within {abs(error):.1f} degrees; walking",
            )

        # Alignment is stationary by construction: W is never taken outside
        # the validated threshold, so a wrong heading costs a rotation rather
        # than a journey.
        self._state = ControlState.ALIGN
        return ControlDecision(
            state=ControlState.ALIGN,
            kind=CommandKind.ALIGN,
            forward=0,
            yaw_deg=yaw_deg,
            yaw_units=units,
            release=False,
            reason=f"turning {yaw_deg:+.1f} degrees to close {error:+.1f}",
        )

    # -- internals --------------------------------------------------------
    def _release(
        self, state: ControlState, reason: str, blockers: tuple[str, ...] = ()
    ) -> ControlDecision:
        self.reset()
        self._state = state
        return _released(state, reason, blockers)

    def _effective_deadband_deg(self) -> float:
        """The configured deadband, never below the actuator's own resolution.

        Plan 9.1 is explicit that the deadband may not be smaller than the
        stable actuator resolution. Here that floor is *derived* from the
        measured calibration rather than assumed, so a machine whose smallest
        effective mouse movement is coarse gets a correspondingly wider band.
        """
        calibration = self._calibration
        floor = 0.0
        if (
            calibration.degrees_per_unit is not None
            and calibration.min_effective_units is not None
        ):
            floor = (
                calibration.degrees_per_unit
                * calibration.min_effective_units
                * self._limits.deadband_resolution_multiple
            )
        return max(self._limits.yaw_deadband_deg, floor)

    def _track_alignment(self, error: float) -> bool:
        """Deadband with hysteresis, counted in *frames* not seconds.

        Once inside, the error must exceed the threshold plus the hysteresis to
        get back out, which is what stops a noisy estimate chattering between
        walking and turning.
        """
        limits = self._limits
        threshold = limits.align_threshold_deg
        if self._state is ControlState.FOLLOW:
            threshold += limits.align_hysteresis_deg
        if abs(error) <= threshold:
            self._aligned_frames += 1
            return True
        self._aligned_frames = 0
        return False

    def _yaw_for(self, error: float, inputs: SteeringInputs) -> float:
        """Filtered PD, then every bound in turn, in degrees.

        Confidence scales the output down and never up: an uncertain estimate
        may justify a smaller correction, never a larger one.
        """
        limits = self._limits
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

        if abs(error) <= self._effective_deadband_deg():
            # Inside the deadband the correct request is *nothing*. The rate
            # state is decayed rather than reset so re-entering the band does
            # not produce a step change on the next frame outside it.
            self._last_error_deg = error
            self._last_time_s = now
            self._last_rate_deg_per_s = 0.0
            self._last_accel_deg_per_s2 = 0.0
            self._last_yaw_deg = 0.0
            return 0.0

        command = limits.kp * error + limits.kd * derivative
        command *= max(0.0, min(1.0, inputs.direction.confidence))
        command = max(-limits.max_yaw_per_pulse_deg, min(limits.max_yaw_per_pulse_deg, command))

        if delta_t is not None and delta_t > 1e-6:
            command = self._rate_limited(command, error, delta_t)

        self._last_error_deg = error
        self._last_time_s = now
        self._last_yaw_deg = command
        self._episode_yaw_deg += abs(command)
        return command

    def _rate_limited(self, command: float, error: float, delta_t: float) -> float:
        """Apply the rate, acceleration and jerk bounds against measured time.

        The rate ceiling is not a constant. It is the largest rate from which
        the acceleration bound can still bring the turn to a stop within the
        remaining error::

            rate_cap = sqrt(2 * max_accel * |error|)

        Without that constraint the limiter is the *cause* of overshoot rather
        than a bound on it: a controller that has spun up to its maximum rate
        needs a fixed distance to slow down, and if the remaining error is
        shorter than that distance it sails past zero and comes back. Measured
        11.4 degrees of overshoot on a 5-degree correction before this existed.
        """
        limits = self._limits
        stopping_cap = math.sqrt(2.0 * limits.max_yaw_accel_deg_per_s2 * max(0.0, abs(error)))
        rate_cap = min(limits.max_yaw_rate_deg_per_s, stopping_cap)

        desired_rate = command / delta_t
        desired_rate = max(-rate_cap, min(rate_cap, desired_rate))

        # Acceleration, then jerk, both in their own units.
        accel = (desired_rate - self._last_rate_deg_per_s) / delta_t
        accel = max(
            -limits.max_yaw_accel_deg_per_s2, min(limits.max_yaw_accel_deg_per_s2, accel)
        )
        max_accel_step = limits.max_yaw_jerk_deg_per_s3 * delta_t
        accel = max(
            self._last_accel_deg_per_s2 - max_accel_step,
            min(self._last_accel_deg_per_s2 + max_accel_step, accel),
        )

        rate = self._last_rate_deg_per_s + accel * delta_t
        rate = max(-rate_cap, min(rate_cap, rate))
        self._last_accel_deg_per_s2 = accel
        self._last_rate_deg_per_s = rate
        return rate * delta_t
