"""The arrow follower: continuous pursuit, occlusion memory, one control-mode proof.

The behaviour this replaces was *stop-turn-go*. It required three consecutive
frames inside an eight-degree cone before ``W`` could go down, it dropped ``W``
the instant the error left that cone, and it then turned on the spot and waited
out the actuator's own latency before pressing forward again. On this machine
that latency was measured at 322-364 ms, so an ordinary curve in a route cost a
full stop, a turn, a wait and a fresh start - several times a second. The
character walked in stutters, and the controller was doing exactly what it was
written to do.

The mistake was a category error: *currently correcting a heading* was treated
as *must stand still*. They are different facts, and a keyboard can express
both at once. So can this controller.

Five rules run through the module.

**Forward and steering are independent outputs.** A tick decides how much
correction the heading wants and, separately, whether there is any reason to
stop walking. A turn pulse completing does not release ``W``; ``W`` is released
by arrival, by severe misalignment held long enough to confirm, by a recovery
maneuver, or by safety - and by nothing else.

**Occlusion is not obstruction.** The map arrow passes behind foliage
constantly; traces from this repository hold losses of 0.7 to 2.65 seconds on
healthy routes. A lost arrow while the character is moving normally means keep
going on the remembered heading, and then look around while still moving. It
never means stand still, and it is never confused with the separate question of
whether the character is physically stuck, which is measured from motion.

**Every angle goes through one filter.** :class:`~prospector_engine.heading
.HeadingFilter` owns the last stable heading, its rate, its confidence and its
spread, and it is the only thing in the process that remembers a direction. The
controller reads it; it never keeps a second copy that could disagree.

**The controller is level-triggered.** Every tick states the complete desired
keyboard level - forward, strafe, turn, jump - including the ticks where the
answer is "the same as last time". There is no edge in this module, which is
what makes a rattle impossible to express rather than merely unlikely.

**Shift Lock is a state, not a key.** It is something the player switched on,
and this code never presses or toggles Shift to "make sure". It is *verified*
per run - by a stable on-screen cue, or by a bounded stationary micro-yaw check
- and the proof is bound to the exact arm token, window, profile revision and
control fingerprint it was taken under. An unverified control mode means Live
is unavailable, not that Live guesses.
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
from prospector_engine.heading import HeadingConfig, HeadingEstimate, HeadingFilter
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

    The bands are the heart of it. A heading error does not select *whether* to
    walk - that question is answered by arrival, contact and safety - it selects
    how hard to correct while walking:

    ==========================  ============================================
    ``|error|``                 what happens
    ==========================  ============================================
    inside the deadband         ``W`` alone
    up to ``correct_band_deg``  ``W`` plus a small bounded correction
    up to ``strong_band_deg``   ``W`` plus a stronger bounded correction
    beyond, sustained           a stationary pivot, and only then
    ==========================  ============================================

    Every number here is *provisional configuration*: chosen as a starting
    point, carried with provenance, and tuned only from replay or native
    evidence. None of them is a measurement, and the deadband floor is derived
    from the actuator resolution the characterizer actually measured rather
    than typed in at all.
    """

    # -- the pursuit bands ------------------------------------------------
    #: Inside this, no correction is wanted at all beyond the deadband rule.
    follow_band_deg: float = 6.0
    #: Up to this, a small correction runs concurrently with the forward hold.
    correct_band_deg: float = 35.0
    #: Up to this, a stronger correction, still concurrent with forward.
    strong_band_deg: float = 70.0
    #: Widening applied to a band once inside it, so an estimate hovering on a
    #: boundary does not alternate between two behaviours every frame.
    band_hysteresis_deg: float = 4.0
    #: Ceiling on one correction inside ``correct_band_deg``.
    gentle_correction_deg: float = 12.0
    #: Ceiling on one correction inside ``strong_band_deg``.
    strong_correction_deg: float = 25.0
    #: Ceiling on one correction during a stationary pivot. Large on
    #: purpose: see ``TurnLimits.max_pivot_correction_deg``.
    pivot_correction_deg: float = 170.0
    #: How long the error must stay beyond ``strong_band_deg`` before the
    #: character stops walking.
    #:
    #: Short, because it is the *second* line of defence rather than the first.
    #: A single implausible frame is already refused by the heading filter's
    #: outlier gate before it ever reaches a band decision, so this only has to
    #: cover a sustained-but-wrong estimate - and every frame it covers is a
    #: frame spent walking away from the target.
    pivot_confirm_s: float = 0.18
    #: Error beyond which the pivot needs no confirmation at all.
    #:
    #: Past this the target is flatly behind us and there is no reading of the
    #: frame in which walking forward is the right thing to do. The heading
    #: filter's outlier gate has already refused it for two frames if it was a
    #: flyer, so waiting out ``pivot_confirm_s`` on top of that is a fifth of a
    #: second spent walking further from the treasure.
    pivot_immediate_deg: float = 150.0
    #: Error inside which a pivot ends and pursuit resumes. Well below
    #: ``strong_band_deg`` so the two cannot chatter.
    pivot_release_deg: float = 45.0
    #: Rotation the controller may spend **without ever converging** before it
    #: gives up rather than spinning.
    #:
    #: The accumulator resets every time the error comes inside
    #: ``follow_band_deg``, and that reset is the whole meaning of the bound.
    #: Without it the number is a ceiling on total rotation, which under
    #: continuous pursuit is a ceiling on how far a route may bend: a shoreline
    #: curving at a steady 20 degrees a second is converging perfectly, on
    #: course, every single frame, and it accumulated 540 degrees of entirely
    #: correct correction in half a minute and safe-stopped. Measured: FAILED
    #: after 19 s at 35 deg/s, 27 s at 20, 32 s at 10.
    max_episode_yaw_deg: float = 540.0

    # -- the correction itself --------------------------------------------
    #: Heading error inside which no correction is requested. Floored at a
    #: multiple of the measured minimum effective actuator movement, because
    #: asking for a rotation smaller than the actuator can produce yields the
    #: actuator's minimum instead - which overshoots, reverses, and dithers.
    yaw_deadband_deg: float = 2.5
    #: Multiple of the actuator resolution the deadband may never go below.
    deadband_resolution_multiple: float = 1.5
    #: Once a correction is running, it stops when the error falls to this
    #: fraction of the deadband. Hysteresis, so left/right cannot chatter
    #: around zero.
    deadband_exit_fraction: float = 0.6
    #: A correction may only reverse sign once the error exceeds this. Below
    #: it, the controller waits rather than flipping the camera back and forth.
    correction_flip_deg: float = 9.0
    #: Proportional gain on the lead-compensated error.
    #:
    #: There is deliberately no derivative gain beside it. Derivative action is
    #: already in the loop, as the lead term below - and running both is not
    #: belt and braces, it is double compensation. It measurably reversed the
    #: camera on the slow actuator: a settled twenty-four degree error with a
    #: large negative rate produced ``0.6 * 2 - 0.12 * 90``, a command pointing
    #: the wrong way, and the simulated route turned back into its own error.
    kp: float = 0.6
    #: Fraction of the measured actuator latency the controller steers ahead
    #: by. The heading filter reports an angular rate; multiplying it by the
    #: latency answers "where will the error be when this correction lands",
    #: which is the question a lagging actuator actually poses. Zero disables
    #: lead compensation entirely.
    lead_fraction: float = 1.0
    #: Ceiling on the lead term, so a wild rate estimate cannot invent an error.
    max_lead_deg: float = 30.0

    # -- the forward lease -------------------------------------------------
    #: How long one command's evidence justifies the level it asked for. The
    #: actuator is level-triggered and holds until told otherwise, so this is a
    #: freshness statement about the *command*, not a renewal interval.
    lease_renew_ms: int = 100
    lease_horizon_ms: int = 250

    # -- evidence freshness ------------------------------------------------
    #: How stale accepted evidence may be before the controller stops making
    #: new decisions from it.
    max_evidence_age_ms: int = 100
    #: How much longer than that the controller keeps the forward hold while
    #: refusing to create any new turn. A capture gap of one or two frames is
    #: not a reason to let go of ``W`` - and letting go and re-pressing it on
    #: the next frame is the W-up/W-down chatter this bound exists to stop. A
    #: genuine sustained capture failure is released by the actuator heartbeat
    #: and the deadman, neither of which this can extend.
    stale_coast_ms: int = 250
    #: Processed frames per second below which the controller *warns and
    #: adapts*. Deliberately not a release condition: a throughput average
    #: describes the recent past, a frame age describes the evidence being
    #: steered on right now, and only the second can make a decision unsafe.
    min_processed_fps: int = 30
    #: Direction confidence below which a reading is not evidence at all.
    min_direction_confidence: float = 0.35

    # -- occlusion memory ---------------------------------------------------
    #: How long the controller keeps walking on a remembered heading with no
    #: usable arrow reading. Existing traces hold healthy losses of 0.7 to 2.65
    #: seconds behind foliage, so anything shorter stops a good route.
    coast_grace_s: float = 2.0
    #: How long the correction in flight is bled out to neutral once the arrow
    #: is lost. Turning blind on a heading that is going stale is worse than
    #: not turning at all, but cutting the camera dead mid-pulse is a jolt.
    coast_decay_s: float = 0.25
    #: The whole bounded search episode, measured from the loss. Past it the
    #: controller reports a lost target and the navigator stops safely.
    search_budget_s: float = 9.0
    #: One sweep leg: turn for this long...
    search_sweep_ms: int = 400
    #: ...then hold still and look for this long before the next leg.
    search_settle_ms: int = 300
    #: Ceiling on one sweep leg's rotation. Shallow on purpose - an immediate
    #: ninety-degree turn throws away the heading that was working.
    search_step_deg: float = 14.0
    #: Total rotation one search episode may spend before giving up.
    search_max_yaw_deg: float = 200.0
    #: Consecutive frames a *different* arrow identity must be seen before the
    #: controller switches target. Foliage that scores like an arrow for one
    #: frame must not steal the route.
    identity_latch_frames: int = 3
    #: How long the controller may stand in ACQUIRE with nothing to steer by
    #: before it starts searching rather than waiting.
    acquire_grace_s: float = 1.5

    # -- pointer safety -----------------------------------------------------
    #: Fraction of the client the cursor must stay inside. Outside it, yaw and
    #: W release and the pointer is recentred before anything resumes.
    safe_region_fraction: float = 0.72

    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="TREASURE_NAVIGATION_PLAN.md section 9.1; mission sections C and D",
            note=(
                "bands, gains and grace periods are chosen starting points, not "
                "calibrated facts. The deadband floor is derived from the measured "
                "actuator resolution rather than configured."
            ),
        )
    )

    def heading_config(self) -> HeadingConfig:
        """The filter configuration implied by these limits.

        Built here rather than configured separately so the filter's memory
        horizon and the controller's coast grace can never be set to two
        different numbers - which is exactly how a controller ends up steering
        on a heading its own filter has already forgotten.
        """
        return HeadingConfig(
            min_confidence=self.min_direction_confidence,
            max_age_s=max(self.coast_grace_s, self.search_budget_s) + 0.5,
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
    """One controller tick: the complete desired keyboard level, and why.

    Level, not edge. Every field states what should be *held* when this tick is
    over, including on the ticks where the answer is "whatever was held
    before"; the actuator downstream works out which edges that implies. A
    caller cannot request an edge, which is what makes a rattle impossible to
    express.

    ``release`` is separate from the levels on purpose. "Hold nothing" and
    "release everything and forget what you were doing" are different
    instructions, and conflating them is how a controller loses its heading
    memory every time an arrow blinks.
    """

    state: ControlState
    kind: CommandKind
    #: 1 forward, -1 back, 0 neither.
    forward: int
    #: -1 left, 1 right. Strafing, not turning.
    lateral: int
    plan: TurnPlan
    jump: bool
    release: bool
    reason: str
    blockers: tuple[str, ...] = ()
    #: The filtered heading this decision was made from, or ``None`` when the
    #: controller had nothing to steer by.
    heading: HeadingEstimate | None = None
    #: Signed heading error this decision was made from, for the dashboard.
    error_deg: float | None = None
    #: The arrow has been unreadable past its whole bounded search budget. A
    #: flag rather than a phrase the caller has to match on, because "look for
    #: the arrow again" and "something is wrong" are different instructions and
    #: telling them apart by parsing a sentence is how they get confused.
    lost_target: bool = False
    #: How long the current occlusion episode has been running.
    lost_for_s: float = 0.0
    #: This tick restated the level that was already held rather than deciding
    #: a new one - a repeated frame, or one just past its freshness budget. A
    #: hold must never mint a fresh command: the frame that would justify it
    #: has already authorized one, and re-issuing it is how a consumed frame
    #: renews a lease it should not.
    held: bool = False
    #: Things worth telling a person that are explicitly **not** reasons to
    #: stop. Kept apart from ``blockers`` so a degraded cadence cannot be
    #: rendered, counted, or acted on as though it were a refusal.
    advisories: tuple[str, ...] = ()

    @property
    def moves(self) -> bool:
        return self.forward != 0 or self.lateral != 0 or self.jump or self.plan.moves

    @property
    def yaw_deg(self) -> float:
        return self.plan.expected_deg

    def describe_level(self) -> str:
        """The composite level, in the same words the overlay draws."""
        parts: list[str] = []
        if self.forward > 0:
            parts.append("W")
        elif self.forward < 0:
            parts.append("S")
        if self.lateral < 0:
            parts.append("A")
        elif self.lateral > 0:
            parts.append("D")
        if self.plan.turn_axis < 0:
            parts.append("<")
        elif self.plan.turn_axis > 0:
            parts.append(">")
        if self.plan.yaw_delta_px:
            parts.append(f"MOUSE {self.plan.yaw_delta_px:+d}")
        if self.jump:
            parts.append("JUMP")
        return " + ".join(parts) if parts else "nothing"


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
        lateral=0,
        plan=TurnPlan.none(backend),
        jump=False,
        release=True,
        reason=reason,
        blockers=blockers,
    )


# ---------------------------------------------------------------------------
# The controller
# ---------------------------------------------------------------------------


@dataclass
class _Correction:
    """One camera correction in flight: what was asked, and when it can be read."""

    units: int
    axis: int
    error_before_deg: float
    issued_at_s: float
    holds_until_s: float
    observe_until_s: float

    def holding(self, now_s: float) -> bool:
        return now_s < self.holds_until_s

    def observing(self, now_s: float) -> bool:
        return now_s < self.observe_until_s


@dataclass
class _SearchEpisode:
    """A bounded sweep for an arrow that has been gone past its coast grace."""

    started_at_s: float
    bias: int
    yaw_spent_deg: float = 0.0
    legs: int = 0


class ArrowFollowerController:
    """Continuous pursuit: walk toward the arrow, correcting while walking.

    The state machine has nine states and five of them walk. That ratio is the
    whole difference from the controller this replaces, which had one.

    It is **cadence independent**: every rate limit is applied against measured
    monotonic time, so the same route behaves the same at 30 and at 120 frames
    per second. Duplicate or missing frames freeze the estimate rather than
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
        self._heading = HeadingFilter(self._limits.heading_config())
        # Deliberately outside ``reset()``. The loss clock has to survive a
        # release, or every release would restart it and the controller would
        # coast, release, coast again, forever - which is the opposite of a
        # bound.
        self._lost_since_s: float | None = None
        self._search: _SearchEpisode | None = None
        self.reset()

    # -- lifecycle --------------------------------------------------------
    def reset(self) -> None:
        """Drop every piece of controller memory. A hard release, not a stop.

        Called when the world this controller was reasoning about has changed -
        a fault, a new viewport, a new profile - and never for an ordinary
        transition between pursuit states. Losing the heading filter because
        the arrow blinked is the bug this distinction exists to prevent.
        """
        self._state = ControlState.ACQUIRE
        self._entered_state_s: float | None = None
        self._last: ControlDecision | None = None
        self._consumed_sequence = -1
        self._correction: _Correction | None = None
        self._correction_sign = 0
        self._episode_yaw_deg = 0.0
        self._severe_since_s: float | None = None
        self._track_id: int | None = None
        self._pending_track_id: int | None = None
        self._pending_frames = 0
        self._geometry_revision: int | None = None
        self._profile_revision: int | None = None
        self._heading.reset()

    def soften(self) -> None:
        """Stand down without forgetting. Used when another subsystem takes over.

        The recovery ladder drives the keyboard itself for a bounded episode.
        When it hands control back, the target, the filtered heading and the
        rate must all still be there - otherwise every obstacle costs a fresh
        stationary acquisition, which is precisely the behaviour the mission
        calls out. This clears what is in flight and keeps what is known.
        """
        self._correction = None
        self._correction_sign = 0
        self._severe_since_s = None
        self._last = None

    @property
    def state(self) -> ControlState:
        return self._state

    @property
    def limits(self) -> SteeringLimits:
        return self._limits

    @property
    def heading(self) -> HeadingFilter:
        return self._heading

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

    @property
    def track_id(self) -> int | None:
        return self._track_id

    def lost_for_s(self, now_s: float) -> float:
        return 0.0 if self._lost_since_s is None else max(0.0, now_s - self._lost_since_s)

    def search_elapsed_s(self, now_s: float) -> float:
        episode = self._search
        return 0.0 if episode is None else max(0.0, now_s - episode.started_at_s)

    def set_response(self, response: TurnResponse | None) -> None:
        """Adopt a freshly measured turn response and drop stale pulse memory."""
        self._response = response
        self._correction = None
        self._correction_sign = 0

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
        """One decision, with any non-blocking observations attached.

        The split is deliberate. :meth:`_decide` may only return reasons to act
        or to stop; anything that is merely *worth saying* is computed here and
        carried alongside, where no branch can accidentally start treating it
        as a refusal.
        """
        advisories = self._advisories(inputs)
        decision = self._decide(inputs)
        decision = replace(decision, lost_for_s=self.lost_for_s(inputs.now_s))
        if advisories:
            decision = replace(decision, advisories=advisories)
        self._remember(decision, inputs.now_s)
        return decision

    def absorb(self, inputs: SteeringInputs) -> HeadingEstimate | None:
        """Keep the target memory current without deciding anything.

        Called while the recovery ladder owns the keyboard. The filter and the
        identity latch keep running on every frame, so when recovery resolves
        the controller resumes pursuit on a live heading instead of starting
        from nothing.

        It deliberately does **not** consume the frame. Absorbing is not
        deciding, and the tick that resolves an episode has to be able to hand
        the very same frame to :meth:`update` and get a real decision back.
        Consuming it here meant that frame produced a *hold* instead - and with
        no previous level to hold, the character stopped for one frame every
        time it got past an obstacle. Reading one frame twice is harmless: the
        heading filter freezes on a repeated instant rather than folding it in.
        """
        estimate, _ = self._read(inputs)
        if estimate is not None:
            self._lost_since_s = None
            self._search = None
        elif self._lost_since_s is None:
            self._lost_since_s = inputs.now_s
        return estimate

    def _advisories(self, inputs: SteeringInputs) -> tuple[str, ...]:
        """Things worth telling a person. Never reasons to release."""
        limits = self._limits
        advisories: list[str] = []
        # A rate of exactly zero is "not measured yet", not "the pipeline is
        # dead": the counter reports 0.0 until it holds two stamps, and this
        # loop reads it before ticking it, so every healthy run starts there.
        if 0.0 < inputs.processed_fps < limits.min_processed_fps:
            advisories.append(
                f"cadence {inputs.processed_fps:.0f} fps below "
                f"{limits.min_processed_fps}; adapting, not stopping"
            )
        return tuple(advisories)

    def _remember(self, decision: ControlDecision, now_s: float) -> None:
        if decision.state is not self._state:
            self._entered_state_s = now_s
        self._state = decision.state
        self._last = None if decision.release else decision

    # -- the decision -----------------------------------------------------
    def _decide(self, inputs: SteeringInputs) -> ControlDecision:
        limits = self._limits
        backend = self.backend or TurnBackend.MOUSE_YAW

        # 1. Safety. Anything here releases before anything else is considered,
        #    and every one of them forgets the run's memory on the way out.
        if inputs.fault:
            return self._release(ControlState.SAFE_STOP, f"fault: {inputs.fault}")
        if not inputs.viewport_ok:
            return self._release(ControlState.SAFE_STOP, "viewport is not usable")
        if not inputs.focus_ok:
            return self._release(ControlState.SAFE_STOP, "Roblox is not focused")
        if inputs.cursor_safe is False:
            # Release first, recentre second. The pointer leaving the safe
            # region while W is held is how a drag ends up outside the window.
            #
            # ``False`` only, never "unknown". ``cursor_client_px`` returns
            # None for a pointer outside the client rect *and* for any failed
            # read, and the pointer is outside the client rect in the normal
            # case: the user clicked Start Navigator on the dashboard and left
            # it there.
            return self._release(
                ControlState.BLOCKED, "pointer left the safe region; recentring"
            )

        # 2. Evidence freshness. A short gap keeps the forward hold and drops
        #    the camera; a long one releases. See ``stale_coast_ms``.
        age_ms = inputs.frame_age_ms
        if age_ms > limits.max_evidence_age_ms + limits.stale_coast_ms:
            return self._release(ControlState.REACQUIRE, f"evidence is {age_ms:.0f} ms old")
        if age_ms > limits.max_evidence_age_ms:
            return self._hold(
                f"evidence is {age_ms:.0f} ms old; holding course, turning released",
                drop_turn=True,
            )

        # 3. The coordinate basis. A changed geometry or profile means every
        #    remembered angle was measured in a frame that no longer exists.
        if self._geometry_revision is None:
            self._geometry_revision = inputs.geometry_revision
        elif inputs.geometry_revision != self._geometry_revision:
            return self._release(ControlState.REACQUIRE, "the viewport changed")
        if self._profile_revision is None:
            self._profile_revision = inputs.profile_revision
        elif inputs.profile_revision != self._profile_revision:
            return self._release(ControlState.REACQUIRE, "the arrow profile changed")

        # 4. Evidence. A frame authorizes exactly one *new* decision. A repeat
        #    holds the level rather than releasing it: the world did not change,
        #    so neither should the keyboard.
        if inputs.frame_sequence <= self._consumed_sequence:
            return self._hold("no newer frame; holding the level")

        blockers = self.blocking_reasons()
        if blockers:
            return self._release(
                ControlState.SAFE_STOP, "steering is not characterized", blockers
            )
        self._consumed_sequence = inputs.frame_sequence

        if self._episode_yaw_deg > limits.max_episode_yaw_deg:
            return self._release(
                ControlState.SAFE_STOP,
                f"turned {self._episode_yaw_deg:.0f} degrees without converging",
            )

        estimate, why = self._read(inputs)
        if estimate is None:
            return self._occluded(inputs, backend, why)
        self._lost_since_s = None
        self._search = None
        return self._pursue(inputs, estimate, backend)

    # -- reading the frame -------------------------------------------------
    def _read(self, inputs: SteeringInputs) -> tuple[HeadingEstimate | None, str]:
        """This frame's heading, or the reason there isn't one.

        The identity latch lives here. A candidate carrying a *different* track
        id has to survive ``identity_latch_frames`` in a row before the
        controller will steer by it, because on a green map a lit patch of
        foliage outscores the arrow for a frame or two and locking onto it
        immediately is how a route walks into a hedge.
        """
        limits = self._limits
        arrow = inputs.arrow
        if not arrow.valid:
            return (None, arrow.abstain_reason or "no reading")

        incoming = arrow.track_id
        if self._track_id is None:
            self._track_id = incoming
            self._pending_track_id = None
            self._pending_frames = 0
        elif incoming is not None and incoming != self._track_id:
            if incoming == self._pending_track_id:
                self._pending_frames += 1
            else:
                self._pending_track_id = incoming
                self._pending_frames = 1
            if self._pending_frames < limits.identity_latch_frames and self._heading.usable(
                inputs.now_s
            ):
                return (
                    None,
                    f"a different arrow ({incoming}) has been seen "
                    f"{self._pending_frames} of {limits.identity_latch_frames} times",
                )
            # Latched, or there was nothing to protect. Adopt it outright.
            self._track_id = incoming
            self._pending_track_id = None
            self._pending_frames = 0
            self._correction = None
        else:
            self._pending_track_id = None
            self._pending_frames = 0

        direction = inputs.direction
        if not direction.valid or direction.error_deg is None:
            # A rejected *heading* is the same fact as a missing arrow as far
            # as the forward hold is concerned: there is nothing new to steer
            # by this frame.
            return (None, f"direction abstained: {direction.abstain_reason}")
        if direction.confidence < limits.min_direction_confidence:
            return (
                None,
                f"direction confidence collapsed to {direction.confidence:.2f}",
            )

        estimate = self._heading.observe(
            direction.error_deg,
            confidence=direction.confidence,
            track_id=self._track_id,
            now_s=inputs.now_s,
        )
        if estimate is None:
            return (None, "the heading filter has nothing to offer")
        return (estimate, "")

    # -- pursuit -----------------------------------------------------------
    def _pursue(
        self, inputs: SteeringInputs, estimate: HeadingEstimate, backend: TurnBackend
    ) -> ControlDecision:
        """A usable heading. Decide how hard to correct, and keep walking.

        Forward is not conditional on the correction. The only thing in this
        method that can stop the character is a heading error severe enough,
        for long enough, that walking would take it further from the target
        than turning on the spot costs - and even that is confirmed over
        ``pivot_confirm_s`` rather than taken from one frame.
        """
        limits = self._limits
        error = self._lead(estimate)
        if abs(error) <= limits.follow_band_deg:
            # On course. Whatever rotation it took to get here was not a
            # controller failing to converge, so it stops counting against one.
            self._episode_yaw_deg = 0.0
        pivoting = self._pivot_wanted(error, inputs.now_s)
        plan = self._correction_for(error, estimate, now_s=inputs.now_s, pivoting=pivoting)

        if pivoting:
            self._state = ControlState.ALIGN
            reason = (
                f"turning on the spot: {error:+.0f} degrees is past "
                f"{limits.strong_band_deg:.0f}"
            )
            return ControlDecision(
                state=ControlState.ALIGN,
                kind=CommandKind.ALIGN,
                forward=0,
                lateral=0,
                plan=plan,
                jump=False,
                release=False,
                reason=reason,
                heading=estimate,
                error_deg=error,
            )

        state = ControlState.CORRECT if plan.moves else ControlState.FOLLOW
        if plan.moves:
            reason = f"walking and turning {plan.expected_deg:+.1f} to close {error:+.1f}"
        elif abs(error) <= self._band(limits.follow_band_deg):
            reason = f"on course within {abs(error):.1f} degrees"
        else:
            reason = f"walking; waiting out the last correction ({error:+.1f} to close)"
        return ControlDecision(
            state=state,
            kind=CommandKind.FOLLOW,
            forward=1,
            lateral=0,
            plan=plan,
            jump=False,
            release=False,
            reason=reason,
            heading=estimate,
            error_deg=error,
        )

    def _lead(self, estimate: HeadingEstimate) -> float:
        """Where the error will be when a correction issued now lands.

        The actuator was measured at 322-364 ms on this machine. Steering on
        the error as it is *now* means every correction is planned against a
        heading that will be a third of a second out of date by the time the
        camera answers, which is how a controller overshoots and then hunts.
        The filter already reports an angular rate; this is that rate carried
        forward by the measured latency.

        **Lead may shrink a correction and may never reverse one.** The rate
        estimate cannot tell the target's own motion apart from the rotation
        this controller just commanded, so immediately after a large correction
        it reads as though the error is about to shoot past zero. Allowed to
        change the sign, that produced a real reversal on the slow actuator -
        turn twenty-four degrees right, then immediately ask for four degrees
        left, having moved nothing in between. Anticipating arrival is useful;
        inventing an error on the other side of it is the hunt this was meant
        to prevent.
        """
        limits = self._limits
        response = self._response
        if response is None or limits.lead_fraction <= 0.0:
            return estimate.error_deg
        horizon_s = response.latency_s * limits.lead_fraction
        lead = estimate.rate_deg_s * horizon_s
        lead = max(-limits.max_lead_deg, min(limits.max_lead_deg, lead))
        predicted = estimate.error_deg + lead
        if (predicted > 0.0) != (estimate.error_deg > 0.0):
            # The lead says we will arrive. Ask for nothing, never the reverse.
            return 0.0
        return wrap_deg(predicted)

    def _band(self, edge_deg: float) -> float:
        """A band edge, widened while the controller is already inside it."""
        if self._state in (ControlState.FOLLOW, ControlState.CORRECT):
            return edge_deg + self._limits.band_hysteresis_deg
        return edge_deg

    def _pivot_wanted(self, error: float, now_s: float) -> bool:
        """Whether the error is severe enough, for long enough, to stop walking.

        Two guards, because standing still is expensive. The error has to be
        past ``strong_band_deg`` continuously for ``pivot_confirm_s`` before
        the character stops, and once stopped it has to come inside
        ``pivot_release_deg`` before it starts again. One bad frame can
        therefore never cost a stop, and a heading hovering on the boundary
        cannot alternate.
        """
        limits = self._limits
        severity = abs(error)
        if self._state is ControlState.ALIGN:
            if severity <= limits.pivot_release_deg:
                self._severe_since_s = None
                return False
            return True
        if severity <= limits.strong_band_deg:
            self._severe_since_s = None
            return False
        if severity >= limits.pivot_immediate_deg:
            # Flatly behind us. Nothing is gained by walking another step first.
            self._severe_since_s = self._severe_since_s or now_s
            return True
        if self._severe_since_s is None:
            self._severe_since_s = now_s
            return False
        return (now_s - self._severe_since_s) >= limits.pivot_confirm_s

    def _correction_for(
        self,
        error: float,
        estimate: HeadingEstimate,
        *,
        now_s: float,
        pivoting: bool,
    ) -> TurnPlan:
        """The camera command for this tick. Concurrent with forward, always.

        A correction already in flight is honoured rather than restarted: a
        held turn key keeps its key down for the rest of its planned hold, and
        no new correction is planned until the previous one could have been
        *observed*. That is the one-correction-in-flight rule, and it is what
        stops three pulses being issued inside the actuator's own latency and
        the camera overshooting by three times what was asked.

        What is new is that fresh evidence can *end* a correction early. If the
        error has crossed into the deadband or changed sign while the key is
        still down, the key comes up on this tick rather than at the end of a
        plan made from a heading that no longer exists.
        """
        backend = self.backend or TurnBackend.MOUSE_YAW
        limits = self._limits
        deadband = self._effective_deadband_deg()

        in_flight = self._correction
        if in_flight is not None:
            if in_flight.holding(now_s):
                overshot = (error > 0) != (in_flight.axis > 0) and abs(error) > deadband
                if abs(error) <= deadband or overshot:
                    # Fresh evidence says the turn has done its job, or has
                    # started undoing it. Let the key up now.
                    self._correction = None
                    self._correction_sign = 0
                    return TurnPlan.none(backend, requested_deg=error)
                remaining_ms = max(1, int((in_flight.holds_until_s - now_s) * 1000.0))
                return TurnPlan(
                    backend=backend,
                    turn_axis=in_flight.axis if backend is TurnBackend.ARROW_KEYS else 0,
                    yaw_delta_px=0,
                    hold_ms=remaining_ms,
                    requested_deg=error,
                    expected_deg=0.0,
                )
            if in_flight.observing(now_s):
                return TurnPlan.none(backend, requested_deg=error)
            # The correction can now be read back. Fold what the camera
            # actually did into the measured response and plan again.
            observed = -wrap_deg(error - in_flight.error_before_deg)
            if self._response is not None:
                self._response = self._response.with_observation(in_flight.units, observed)
            self._correction = None

        magnitude = abs(error)
        if self._correction_sign == 0 and magnitude <= deadband:
            return TurnPlan.none(backend, requested_deg=error)
        if self._correction_sign != 0 and magnitude <= deadband * limits.deadband_exit_fraction:
            self._correction_sign = 0
            return TurnPlan.none(backend, requested_deg=error)

        sign = 1 if error >= 0 else -1
        if (
            self._correction_sign != 0
            and sign != self._correction_sign
            and magnitude < limits.correction_flip_deg
        ):
            # Reversing the camera for a small residual is chatter, not
            # steering. Wait for the error to earn the flip.
            return TurnPlan.none(backend, requested_deg=error)

        response = self._response
        if response is None or not response.usable:
            return TurnPlan.none(backend, requested_deg=error)

        ceiling = self._correction_ceiling(magnitude, pivoting=pivoting)
        turn_limits = self._turn_limits
        if pivoting:
            # Standing still, and the misalignment has already been confirmed
            # over ``pivot_confirm_s``. The global ceiling exists to stop a bad
            # estimate spinning the camera mid-route; it is not the right bound
            # for a deliberate turn on the spot.
            turn_limits = replace(
                turn_limits, max_correction_deg=turn_limits.max_pivot_correction_deg
            )
        command = limits.kp * abs(error) * max(0.0, min(1.0, estimate.confidence))
        # Never ask for more rotation than remains, and never more than the
        # band allows: a correction bigger than the error is an overshoot by
        # construction, and a gentle band that asks for a hard turn is not a
        # gentle band. The sign comes from the error and from nothing else, so
        # no gain, however it is tuned, can point the camera the wrong way.
        command = math.copysign(min(command, magnitude, ceiling), error)
        plan = response.plan_for(command, turn_limits)
        if not plan.moves:
            return plan

        self._episode_yaw_deg += abs(plan.expected_deg)
        self._correction_sign = sign
        hold_s = plan.hold_ms / 1000.0 if backend is TurnBackend.ARROW_KEYS else 0.0
        self._correction = _Correction(
            units=plan.units,
            axis=plan.turn_axis if backend is TurnBackend.ARROW_KEYS else sign,
            error_before_deg=error,
            issued_at_s=now_s,
            holds_until_s=now_s + hold_s,
            observe_until_s=now_s + hold_s + response.latency_s,
        )
        return plan

    def _correction_ceiling(self, magnitude: float, *, pivoting: bool) -> float:
        """How much rotation this band is allowed to ask for in one correction."""
        limits = self._limits
        if pivoting:
            return limits.pivot_correction_deg
        if magnitude <= self._band(limits.correct_band_deg):
            return limits.gentle_correction_deg
        return limits.strong_correction_deg

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

    # -- occlusion ---------------------------------------------------------
    def _occluded(
        self, inputs: SteeringInputs, backend: TurnBackend, why: str
    ) -> ControlDecision:
        """A frame with no usable heading. Three answers, in order of cost.

        The arrow being unreadable and the character being stuck are separate
        facts, measured separately, and this method only ever answers the
        first. Existing traces from this repository hold arrow losses of 0.7 to
        2.65 seconds on perfectly healthy routes - foliage, a passing player, a
        camera shake - so stopping is the wrong default and was the behaviour
        that made a good route stutter.

        * **COAST**, for the first ``coast_grace_s``: keep walking on the
          remembered heading and bleed the correction out to neutral. Turning
          blind on a heading that is going stale is worse than not turning.
        * **SEARCH**, up to ``search_budget_s``: keep walking and sweep
          shallowly, biased toward where the arrow was last seen. Bounded in
          time and in total rotation.
        * **give up**, past the budget: report a lost target and let the
          navigator stop safely. Standing in REACQUIRE forever with nothing on
          screen explaining it is the state these bounds make unreachable.
        """
        limits = self._limits
        if self._lost_since_s is None:
            self._lost_since_s = inputs.now_s
        lost_s = inputs.now_s - self._lost_since_s
        memory = self._heading.coast(inputs.now_s)
        was_moving = self._state.holds_forward

        if was_moving and lost_s <= limits.coast_grace_s:
            plan = self._decayed_correction(lost_s, backend)
            held = "" if memory is None else f" on {memory.error_deg:+.1f} degrees"
            return ControlDecision(
                state=ControlState.COAST,
                kind=CommandKind.FOLLOW,
                forward=1,
                lateral=0,
                plan=plan,
                jump=False,
                release=False,
                reason=(f"arrow lost for {lost_s * 1000:.0f} ms; holding course{held} ({why})"),
                heading=memory,
                error_deg=None if memory is None else memory.error_deg,
            )

        # Standing still with nothing to steer by has its own short grace, so a
        # run that starts before the map is on screen does not sweep the camera
        # the instant it begins.
        if not was_moving and memory is None and lost_s <= limits.acquire_grace_s:
            return ControlDecision(
                state=ControlState.ACQUIRE,
                kind=CommandKind.ALIGN,
                forward=0,
                lateral=0,
                plan=TurnPlan.none(backend),
                jump=False,
                release=False,
                reason=f"looking for the arrow ({why})",
            )

        if lost_s > limits.search_budget_s:
            return self._abandon(f"the arrow has not been readable for {lost_s:.0f} s ({why})")
        return self._sweep(inputs, backend, lost_s, memory, why)

    def _decayed_correction(self, lost_s: float, backend: TurnBackend) -> TurnPlan:
        """Bleed the correction in flight out to neutral over ``coast_decay_s``.

        Cutting the camera dead mid-pulse is a visible jolt and throws away a
        correction that was, a moment ago, right. Continuing it indefinitely is
        turning blind. So the key stays down for whatever is left of both its
        own plan and the decay window, and then comes up.
        """
        in_flight = self._correction
        limits = self._limits
        if in_flight is None or lost_s > limits.coast_decay_s:
            self._correction = None
            self._correction_sign = 0
            return TurnPlan.none(backend)
        remaining_s = min(
            limits.coast_decay_s - lost_s,
            max(0.0, in_flight.holds_until_s - (in_flight.issued_at_s + lost_s)),
        )
        if remaining_s <= 0.0 or backend is not TurnBackend.ARROW_KEYS:
            self._correction = None
            self._correction_sign = 0
            return TurnPlan.none(backend)
        return TurnPlan(
            backend=backend,
            turn_axis=in_flight.axis,
            yaw_delta_px=0,
            hold_ms=max(1, int(remaining_s * 1000.0)),
            requested_deg=0.0,
            expected_deg=0.0,
        )

    def _sweep(
        self,
        inputs: SteeringInputs,
        backend: TurnBackend,
        lost_s: float,
        memory: HeadingEstimate | None,
        why: str,
    ) -> ControlDecision:
        """A bounded moving search: shallow legs, biased, alternating, capped.

        It keeps walking whenever there is a remembered heading to walk on,
        because the character was going the right way a moment ago and standing
        still throws that away. With no memory at all - a run that began with
        nothing on screen - it sweeps from a standstill instead of walking off
        in an arbitrary direction.

        The first leg goes toward the side the arrow was last on. Legs
        alternate after that, each one shallow: an immediate ninety-degree turn
        is how a search loses a route it could have recovered.
        """
        limits = self._limits
        episode = self._search
        if episode is None:
            bias = 1
            if memory is not None and abs(memory.error_deg) > 1.0:
                bias = 1 if memory.error_deg > 0.0 else -1
            episode = _SearchEpisode(started_at_s=inputs.now_s, bias=bias)
            self._search = episode
            self._correction = None
            self._correction_sign = 0

        if episode.yaw_spent_deg > limits.search_max_yaw_deg:
            return self._abandon(
                f"searched through {episode.yaw_spent_deg:.0f} degrees without "
                f"finding the arrow ({why})"
            )

        elapsed = inputs.now_s - episode.started_at_s
        cycle_s = (limits.search_sweep_ms + limits.search_settle_ms) / 1000.0
        leg = int(elapsed // cycle_s)
        within_s = elapsed - leg * cycle_s
        sweeping = within_s < limits.search_sweep_ms / 1000.0
        # Alternate, and widen every full pair, so the search covers ground
        # without ever making one large blind turn.
        direction = episode.bias if leg % 2 == 0 else -episode.bias
        forward = 1 if memory is not None else 0

        # One correction per leg, not one per frame. Planning a fresh turn on
        # every frame of a 400 ms window spends the whole episode's rotation
        # budget in a quarter of a second and the search abandons before it has
        # looked anywhere - which is exactly what the first version did.
        plan = TurnPlan.none(backend)
        response = self._response
        in_flight = self._correction
        if in_flight is not None and in_flight.holding(inputs.now_s):
            plan = TurnPlan(
                backend=backend,
                turn_axis=in_flight.axis if backend is TurnBackend.ARROW_KEYS else 0,
                yaw_delta_px=0,
                hold_ms=max(1, int((in_flight.holds_until_s - inputs.now_s) * 1000.0)),
                requested_deg=0.0,
                expected_deg=0.0,
            )
        elif sweeping and episode.legs <= leg and response is not None and response.usable:
            if in_flight is not None and not in_flight.observing(inputs.now_s):
                self._correction = None
            step = limits.search_step_deg * (1.0 + 0.5 * (leg // 2))
            issued = response.plan_for(direction * step, self._turn_limits)
            if issued.moves:
                # Counted against the search's own budget and *not* against the
                # convergence guard: a search is bounded by
                # ``search_max_yaw_deg`` already, and charging it twice let a
                # legitimate bounded search trip a rule about not converging.
                episode.yaw_spent_deg += abs(issued.expected_deg)
                episode.legs = leg + 1
                hold_s = issued.hold_ms / 1000.0 if backend is TurnBackend.ARROW_KEYS else 0.0
                self._correction = _Correction(
                    units=issued.units,
                    axis=issued.turn_axis if backend is TurnBackend.ARROW_KEYS else direction,
                    error_before_deg=0.0,
                    issued_at_s=inputs.now_s,
                    holds_until_s=inputs.now_s + hold_s,
                    observe_until_s=inputs.now_s + hold_s + response.latency_s,
                )
                plan = issued
        elif in_flight is not None and not in_flight.observing(inputs.now_s):
            self._correction = None

        side = "right" if direction > 0 else "left"
        action = f"sweeping {side}" if sweeping else "looking"
        return ControlDecision(
            state=ControlState.SEARCH,
            kind=CommandKind.FOLLOW if forward else CommandKind.ALIGN,
            forward=forward,
            lateral=0,
            plan=plan,
            jump=False,
            release=False,
            reason=(
                f"arrow lost for {lost_s:.1f} s; {action} "
                f"(leg {leg + 1}, {episode.yaw_spent_deg:.0f} of "
                f"{limits.search_max_yaw_deg:.0f} degrees spent)"
            ),
            heading=memory,
            error_deg=None if memory is None else memory.error_deg,
        )

    # -- the three ways a tick can end without a new decision ---------------
    def _hold(self, reason: str, *, drop_turn: bool = False) -> ControlDecision:
        """Keep the level that is already held. The level-triggered no-op.

        This is not "do nothing" in the sense of emitting nothing: it restates
        the complete desired level so the actuator's heartbeat is fed and no
        edge is implied. A frame that carries no new information is a reason to
        change nothing, never a reason to let go of ``W``.
        """
        previous = self._last
        backend = self.backend or TurnBackend.MOUSE_YAW
        if previous is None:
            return ControlDecision(
                state=self._state,
                kind=CommandKind.ALIGN,
                forward=0,
                lateral=0,
                plan=TurnPlan.none(backend),
                jump=False,
                release=False,
                reason=reason,
                held=True,
            )
        plan = TurnPlan.none(backend) if drop_turn else previous.plan
        if drop_turn:
            self._correction = None
        return replace(previous, plan=plan, jump=False, reason=reason, held=True)

    def _release(
        self, state: ControlState, reason: str, blockers: tuple[str, ...] = ()
    ) -> ControlDecision:
        """Drop the level *and* the memory. Safety and world changes only."""
        backend = self.backend or TurnBackend.MOUSE_YAW
        response = self._response
        self.reset()
        self._response = response
        self._state = state
        return _released(state, reason, blockers, backend)

    def _abandon(self, reason: str) -> ControlDecision:
        """The search is spent. Stop, and say the target is gone."""
        released = self._release(ControlState.REACQUIRE, reason)
        return replace(released, lost_target=True)
