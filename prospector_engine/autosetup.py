"""Automatic runtime setup: from "Roblox is open" to "the navigator is ready".

This module replaces the guided commissioning window. That window rendered
frozen evidence gates and ran no procedure, so pressing every button in it left
the application in exactly the state it started in. What a user needs instead
is one action - Start Navigator - and a machine that does the work and says
where it got to.

Three separations run through the design, and they are the reason the old
structure could not be made to work by adding buttons to it:

**Offline evidence, runtime checks, and live safety are different things.**
A detector corpus and a profile's E-PROF gate are *build* evidence about the
software. Whether this window exists, is the right size, is delivering fresh
frames, and shows a stable arrow right now are *runtime* checks about this
session. Whether input may be emitted at all is *safety*, and stays exactly
where it was: a physical click on Arm Live and a physical hotkey press. Runtime
checks may reach READY on their own; they never grant the third.

**A stage either produced the evidence its successor needs, or it names the one
thing to do about it.** There is no "probably fine". Every stage is bounded by
an attempt cap *and* a monotonic deadline, and every failure carries a
:class:`~prospector_engine.contracts.SetupFailureKind` and one sentence.

**Nothing here touches the game until the user has armed it.** The observation
phase - find, fit, capture, profile, reference, qualify - emits no input at
all. The two stages that do (control-mode verification and turn
characterization) run inside the live worker, after the physical arm, with the
character stationary and every probe released.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable

from prospector_engine.acceptance import AcceptanceResult
from prospector_engine.contracts import (
    EvidenceStatus,
    Provenance,
    SetupFailure,
    SetupFailureKind,
    SetupProgress,
    SetupStage,
    ViewportFit,
    monotonic_s,
)
from prospector_engine.geometry import ViewportGeometry
from prospector_engine.turning import (
    ControlFingerprint,
    TurnCharacterizer,
    TurnLimits,
    TurnObservation,
    TurnResponse,
)

__all__ = [
    "AutomaticSetup",
    "CaptureSample",
    "ControlModeSample",
    "PerceptionSample",
    "ProfileClassifier",
    "ProfileDecision",
    "ProfileVote",
    "ReferenceCheck",
    "SetupConfig",
    "SetupPort",
    "WindowProbe",
]


# ---------------------------------------------------------------------------
# What the machine is allowed to look at
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowProbe:
    """One look for the Roblox client.

    ``ambiguous`` is a first-class answer rather than a variety of "not found".
    Two Roblox windows is a situation with a specific remedy, and picking the
    larger one silently - which is what the platform port used to do - resizes
    somebody's login prompt.
    """

    found: bool
    detail: str
    identity: tuple[object, ...] | None = None
    ambiguous: bool = False
    permission_denied: bool = False
    fullscreen: bool = False


@dataclass(frozen=True)
class CaptureSample:
    """The newest frame, and whether it belongs to the geometry we adopted."""

    sequence: int
    age_s: float | None
    delivered_px: tuple[int, int] | None
    expected_px: tuple[int, int] | None
    processed_fps: float = 0.0
    error: str | None = None

    @property
    def matches_geometry(self) -> bool:
        if self.delivered_px is None or self.expected_px is None:
            return False
        return self.delivered_px == self.expected_px


@dataclass(frozen=True)
class ProfileVote:
    """One frame's evidence about which arrow profile is in front of us."""

    frame_sequence: int
    #: ``profile_id -> confidence in [0, 1]``. A profile that abstained on this
    #: frame scores zero rather than being absent, so "nothing matched" and
    #: "this profile was not evaluated" cannot be confused.
    scores: Mapping[str, float]


@dataclass(frozen=True)
class PerceptionSample:
    """One frame of perception, as the reference and qualify stages see it."""

    frame_sequence: int
    arrow_valid: bool
    direction_valid: bool
    error_deg: float | None
    confidence: float
    track_id: int | None = None
    processed_fps: float = 0.0
    frame_age_ms: float = 0.0


@dataclass(frozen=True)
class ControlModeSample:
    """Evidence about whether the player is in the locked-camera control mode."""

    verified: bool
    confidence: float
    method: str
    detail: str


@runtime_checkable
class SetupPort(Protocol):
    """Everything automatic setup may do. Injected, so the machine is testable.

    Each method is a genuinely different capability rather than a convenience
    split, and none of them is a raw platform handle: the machine cannot reach
    a window API, an input authority, or a capture source directly.
    """

    def locate_window(self) -> WindowProbe: ...

    def release_all_input(self, reason: str) -> None: ...

    def fit_viewport(self) -> ViewportFit: ...

    def viewport(self) -> ViewportGeometry: ...

    def restart_capture(self, reason: str) -> None: ...

    def heal_viewport(self) -> bool:
        """Re-adopt the client if the guard has lost its pin. No window moves.

        A default so existing ports need not implement it; the real one does.
        """
        return False

    def capture_sample(self) -> CaptureSample: ...

    def profile_vote(self) -> ProfileVote | None: ...

    def lock_profile(self, profile_id: str) -> None: ...

    def perception_sample(self) -> PerceptionSample | None: ...


@runtime_checkable
class ControlSetupPort(Protocol):
    """The extra capabilities the two input-emitting stages need.

    Held only by the live worker, which only exists after a physical arm.
    """

    def input_acceptance(self) -> AcceptanceResult:
        """One bounded forward pulse, and a causal answer about what it did.

        The first thing in the whole system that emits input, and the reason
        the stages after it are worth running at all: characterizing a camera
        turn in a game that is not receiving keys spends thirty seconds proving
        nothing and reports a timeout.
        """
        ...

    def control_mode_sample(self) -> ControlModeSample: ...

    def turn_observation(self) -> TurnObservation | None: ...

    def emit_turn(self, backend_value: str, units: int) -> bool: ...

    def release_turn(self) -> None: ...

    def control_fingerprint(self) -> ControlFingerprint: ...


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SetupConfig:
    """Per-stage bounds. Every one is an attempt cap *and* a deadline.

    The deadlines are generous where another process has to answer (a window
    manager resizing a game) and tight where we are only waiting for our own
    pipeline, because a slow pipeline is a fault and a slow window manager is
    Tuesday.
    """

    poll_interval_s: float = 0.05

    find_window_deadline_s: float = 8.0
    fit_deadline_s: float = 10.0
    fit_max_attempts: int = 2
    capture_restart_deadline_s: float = 6.0
    capture_stable_frames: int = 5
    profile_deadline_s: float = 6.0
    profile_min_frames: int = 8
    #: Mean-score lead the winner needs over the runner-up, absolute.
    profile_min_margin: float = 0.12
    #: Fraction of frames the winner must actually lead.
    profile_min_agreement: float = 0.7
    #: The whole input-acceptance stage: measure idle, pulse, watch, release.
    #: Short on purpose - if the game is not taking keys, waiting longer is
    #: not going to change that, and the user is standing there.
    verify_input_deadline_s: float = 8.0
    reference_deadline_s: float = 8.0
    reference_stable_frames: int = 6
    #: Degrees of frame-to-frame jitter the heading may show and still count as
    #: a stable reference. Wider than the alignment cone on purpose: this is a
    #: check that the reference *works*, not a precision measurement.
    reference_max_jitter_deg: float = 12.0
    qualify_deadline_s: float = 6.0
    qualify_frames: int = 20
    #: Fraction of qualifying frames that must carry a usable arrow.
    qualify_min_hit_rate: float = 0.6
    qualify_min_fps: float = 20.0

    control_mode_deadline_s: float = 8.0
    characterize_deadline_s: float = 30.0

    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="TREASURE_NAVIGATION_PLAN.md section 4; mission section A",
            note="stage budgets are chosen bounds, not measurements",
        )
    )


# ---------------------------------------------------------------------------
# Profile classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileDecision:
    """Which profile the runtime classifier chose, and how confidently."""

    profile_id: str | None
    margin: float
    agreement: float
    frames: int
    detail: str

    @property
    def decided(self) -> bool:
        return self.profile_id is not None


class ProfileClassifier:
    """Chooses the arrow profile from consecutive frames, not from a setting.

    Two independent conditions, because either one alone is fooled:

    * a **mean-score margin**, so a profile that merely ties does not win; and
    * **temporal agreement**, so a profile that wins one lucky frame out of ten
      does not win the session.

    A profile that satisfies both is locked for the run. Anything else is
    reported as ambiguous with the numbers, which is a state a user can act on
    (equip the map, or pick a profile in Advanced) rather than a silent
    mis-selection they will spend an hour debugging.
    """

    def __init__(self, candidates: Sequence[str], config: SetupConfig | None = None) -> None:
        self._candidates = tuple(candidates)
        self._config = config or SetupConfig()
        self._totals: dict[str, float] = dict.fromkeys(self._candidates, 0.0)
        self._wins: dict[str, int] = dict.fromkeys(self._candidates, 0)
        self._frames = 0
        self._seen: set[int] = set()

    @property
    def frames(self) -> int:
        return self._frames

    def observe(self, vote: ProfileVote) -> None:
        if vote.frame_sequence in self._seen:
            return  # one frame, one vote
        self._seen.add(vote.frame_sequence)
        self._frames += 1
        best_id, best_score = None, 0.0
        for profile_id in self._candidates:
            score = float(vote.scores.get(profile_id, 0.0))
            self._totals[profile_id] += score
            if score > best_score:
                best_id, best_score = profile_id, score
        if best_id is not None and best_score > 0.0:
            self._wins[best_id] += 1

    def decide(self) -> ProfileDecision:
        config = self._config
        if self._frames < config.profile_min_frames:
            return ProfileDecision(
                None,
                0.0,
                0.0,
                self._frames,
                f"watching ({self._frames}/{config.profile_min_frames} frames)",
            )
        means = sorted(
            ((total / self._frames, profile_id) for profile_id, total in self._totals.items()),
            reverse=True,
        )
        if not means or means[0][0] <= 0.0:
            return ProfileDecision(None, 0.0, 0.0, self._frames, "no profile matched any frame")
        top_mean, top_id = means[0]
        runner_up = means[1][0] if len(means) > 1 else 0.0
        margin = top_mean - runner_up
        agreement = self._wins[top_id] / self._frames
        if margin < config.profile_min_margin:
            return ProfileDecision(
                None,
                margin,
                agreement,
                self._frames,
                f"{top_id} and {means[1][1]} score within {margin:.2f}",
            )
        if agreement < config.profile_min_agreement:
            return ProfileDecision(
                None,
                margin,
                agreement,
                self._frames,
                f"{top_id} led only {agreement * 100:.0f}% of frames",
            )
        return ProfileDecision(
            top_id,
            margin,
            agreement,
            self._frames,
            f"{top_id} by {margin:.2f} over {agreement * 100:.0f}% of {self._frames} frames",
        )


# ---------------------------------------------------------------------------
# Reference verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenceCheck:
    """Whether the player-to-arrow reference is usable *right now*.

    This is deliberately not a claim that the screen anchor is the avatar's
    true control pivot - that is E-ANCHOR, an offline labelling exercise, and
    it is still PENDING. It is the weaker, checkable claim navigation actually
    depends on: with this anchor, the heading to the arrow is temporally stable
    while the character is standing still. An anchor that were badly wrong
    would show as jitter here, and the left/right consistency check inside turn
    characterization is a second, independent look at the same question.
    """

    stable: bool
    frames: int
    jitter_deg: float
    detail: str
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="runtime reference check; E-ANCHOR / E-FORWARD remain PENDING",
            note="verifies that the reference is stable in this run, not that it is true",
        )
    )


# ---------------------------------------------------------------------------
# The machine
# ---------------------------------------------------------------------------


class SetupCancelled(Exception):
    """Raised inside a stage when the run was cancelled. Never escapes ``run``."""


class AutomaticSetup:
    """The bounded automatic path from IDLE to READY.

    Every stage runs on the caller's thread, publishes its progress, and
    returns a terminal :class:`~prospector_engine.contracts.SetupProgress`. The
    machine owns no threads, no timers and no input; it is a sequence of
    bounded loops over the injected port.
    """

    def __init__(
        self,
        port: SetupPort,
        *,
        config: SetupConfig | None = None,
        publish: Callable[[SetupProgress], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        now: Callable[[], float] = monotonic_s,
        sleep: Callable[[float], None] | None = None,
        candidates: Sequence[str] = (),
    ) -> None:
        self._port = port
        self._config = config or SetupConfig()
        self._publish = publish or (lambda _progress: None)
        self._cancelled = cancelled or (lambda: False)
        self._now = now
        self._sleep = sleep or self._default_sleep
        self._candidates = tuple(candidates)
        self._progress = SetupProgress.idle()
        self._started_s = 0.0
        self._reference: ReferenceCheck | None = None
        self._profile_decision: ProfileDecision | None = None
        self._acceptance: AcceptanceResult | None = None

    @staticmethod
    def _default_sleep(seconds: float) -> None:
        import time

        time.sleep(seconds)

    # -- observable state -------------------------------------------------
    @property
    def progress(self) -> SetupProgress:
        return self._progress

    @property
    def reference(self) -> ReferenceCheck | None:
        return self._reference

    @property
    def profile_decision(self) -> ProfileDecision | None:
        return self._profile_decision

    @property
    def acceptance(self) -> AcceptanceResult | None:
        """What the forward pulse concluded, or ``None`` if it has not run."""
        return self._acceptance

    # -- the run ----------------------------------------------------------
    def run_observation(self) -> SetupProgress:
        """Find, fit, capture, classify, verify. Emits no input whatsoever."""
        self._started_s = self._now()
        stages: tuple[tuple[SetupStage, Callable[[], SetupFailure | None]], ...] = (
            (SetupStage.FIND_ROBLOX, self._find_roblox),
            (SetupStage.FIT_VIEWPORT, self._fit_viewport),
            (SetupStage.RESTART_CAPTURE, self._restart_capture),
            (SetupStage.STABILIZE_CAPTURE, self._stabilize_capture),
            (SetupStage.SELECT_PROFILE, self._select_profile),
            (SetupStage.ESTABLISH_REFERENCE, self._establish_reference),
            (SetupStage.SHADOW_QUALIFY, self._shadow_qualify),
        )
        for stage, run in stages:
            self._enter(stage, "starting")
            try:
                failure = run()
            except SetupCancelled:
                return self._cancel(stage)
            except Exception as exc:  # a stage must never take the process down
                failure = SetupFailure(
                    SetupFailureKind.INTERNAL,
                    stage,
                    f"automatic setup hit an internal error in {stage.value}",
                    "Report this with the diagnostics log; Stop & Release is unaffected.",
                    repr(exc),
                )
            if failure is not None:
                return self._fail(failure)
        return self._succeed()

    def run_control(
        self,
        control: ControlSetupPort,
        *,
        limits: TurnLimits | None = None,
        prior: TurnResponse | None = None,
    ) -> tuple[SetupProgress, TurnResponse | None]:
        """Verify the control mode and measure the turn actuator, under the arm.

        Called by the live worker after a physical arm, with the character
        stationary. Every probe it issues is released before the next
        observation, and every stage is bounded by its own deadline.

        **Input acceptance comes first, and the order is the point.** Verifying
        a camera control mode and measuring a turn actuator both assume the
        game is receiving our input. When it is not - the commonest failure by
        far - those stages spend their whole deadline proving nothing and then
        report a timeout, which names the wrong problem. One forward pulse,
        confirmed against frames captured after its own down edge, answers the
        question they depend on before either of them runs.
        """
        self._started_s = self._now()
        stage = SetupStage.VERIFY_INPUT
        self._enter(stage, "testing whether Roblox accepts a key")
        try:
            failure = self._verify_input(control)
            if failure is not None:
                return (self._fail(failure), None)
            stage = SetupStage.VERIFY_CONTROL_MODE
            self._enter(stage, "checking the camera control mode")
            failure = self._verify_control_mode(control)
            if failure is not None:
                return (self._fail(failure), None)
            stage = SetupStage.CHARACTERIZE_TURN
            self._enter(stage, "measuring the turn actuator")
            response, failure = self._characterize_turn(control, limits=limits, prior=prior)
        except SetupCancelled:
            control.release_turn()
            return (self._cancel(stage), None)
        finally:
            control.release_turn()
        if failure is not None:
            return (self._fail(failure), None)
        assert response is not None
        self._progress = replace(self._progress, turn_backend=response.backend.value)
        return (self._succeed(), response)

    # -- stage: find ------------------------------------------------------
    def _find_roblox(self) -> SetupFailure | None:
        stage = SetupStage.FIND_ROBLOX
        probe = WindowProbe(False, "not looked yet")
        for attempt, _elapsed in self._poll(self._config.find_window_deadline_s):
            probe = self._port.locate_window()
            self._note(stage, attempt, probe.detail)
            if probe.found:
                return None
            if probe.permission_denied or probe.ambiguous or probe.fullscreen:
                break
        if probe.permission_denied:
            return SetupFailure(
                SetupFailureKind.PERMISSION,
                stage,
                "macOS has not granted this app permission to see the Roblox window",
                "Enable Screen Recording and Accessibility for this app in System "
                "Settings > Privacy & Security, then press Start Navigator again.",
                probe.detail,
            )
        if probe.ambiguous:
            return SetupFailure(
                SetupFailureKind.AMBIGUOUS_WINDOW,
                stage,
                "more than one Roblox window is open and none can be identified",
                "Close the extra Roblox window - a login prompt, a crash report, or a "
                "second client - and press Start Navigator again.",
                probe.detail,
            )
        if probe.fullscreen:
            return SetupFailure(
                SetupFailureKind.FULLSCREEN,
                stage,
                "Roblox is in fullscreen, where its window cannot be sized or captured",
                "Leave fullscreen so Roblox is an ordinary window, then press Start "
                "Navigator again.",
                probe.detail,
            )
        return SetupFailure(
            SetupFailureKind.NO_WINDOW,
            stage,
            "no Roblox window was found",
            "Open Roblox in windowed mode with a treasure map equipped, then press "
            "Start Navigator again.",
            probe.detail,
        )

    # -- stage: fit -------------------------------------------------------
    def _fit_viewport(self) -> SetupFailure | None:
        """One serialized transaction: release input, resize, read back, adopt.

        Input is released *before* geometry is touched. A held key across a
        resize is a key held in a window whose coordinates have changed, and
        the release is cheap; the ordering is not negotiable.
        """
        stage = SetupStage.FIT_VIEWPORT
        self._port.release_all_input("automatic setup: fitting the viewport")
        fit = ViewportFit.idle()
        for attempt in range(1, self._config.fit_max_attempts + 1):
            self._check_cancelled()
            self._note(stage, attempt, "asking Roblox for a 1280x720 client")
            fit = self._port.fit_viewport()
            self._progress = replace(
                self._progress,
                requested_client_logical=fit.requested_client_logical,
                achieved_client_logical=fit.achieved_client_logical,
                achieved_client_backing_px=fit.achieved_client_backing_px,
                detail=fit.describe(),
                updated_at_s=self._now(),
            )
            self._publish(self._progress)
            if fit.ok:
                break
        if not fit.ok:
            detail = (fit.detail or "").lower()
            kind = SetupFailureKind.RESIZE_DENIED
            remedy = (
                "Make sure Roblox is an ordinary window - not minimized, not "
                "fullscreen - and press Retry Setup."
            )
            if "accessibility" in detail or "permission" in detail:
                kind = SetupFailureKind.PERMISSION
                remedy = (
                    "Enable Accessibility for this app in System Settings > Privacy & "
                    "Security, then press Retry Setup."
                )
            elif "fullscreen" in detail or "space" in detail:
                kind = SetupFailureKind.FULLSCREEN
                remedy = "Leave fullscreen so Roblox is an ordinary window, then Retry Setup."
            elif "more than one" in detail or "ambiguous" in detail:
                kind = SetupFailureKind.AMBIGUOUS_WINDOW
                remedy = "Close the extra Roblox window, then press Retry Setup."
            return SetupFailure(
                kind, stage, "the Roblox window could not be sized", remedy, fit.detail
            )
        geometry = self._port.viewport()
        if not geometry.valid:
            return SetupFailure(
                SetupFailureKind.VIEWPORT_UNUSABLE,
                stage,
                "the Roblox client could not be read back after resizing",
                "Bring Roblox to the front as an ordinary window and press Retry Setup.",
                geometry.detail,
            )
        return None

    # -- stage: capture ---------------------------------------------------
    def _restart_capture(self) -> SetupFailure | None:
        """Exactly one restart, so a fit cannot cause a cascade of them."""
        self._note(SetupStage.RESTART_CAPTURE, 1, "rebinding capture to the fitted client")
        self._port.restart_capture("automatic setup: viewport adopted")
        return None

    def _stabilize_capture(self) -> SetupFailure | None:
        stage = SetupStage.STABILIZE_CAPTURE
        needed = self._config.capture_stable_frames
        matched = 0
        last_sequence = -1
        sample = CaptureSample(0, None, None, None)
        for attempt, _elapsed in self._poll(self._config.capture_restart_deadline_s):
            sample = self._port.capture_sample()
            if sample.error is not None:
                # One transient bad read can leave the guard UNPINNED with no
                # adopted window, even though the frames are the right size.
                # The capture supervisor heals that on its own poll, but on a
                # slower interval than this stage's deadline - so ask directly
                # rather than time out on a condition that was about to fix
                # itself. Bounded by this loop's own attempt cap and deadline;
                # re-adopting binds to the client and moves nothing.
                self._port.heal_viewport()
                matched = 0
            elif sample.sequence != last_sequence and sample.matches_geometry:
                last_sequence = sample.sequence
                matched += 1
            elif sample.sequence != last_sequence:
                last_sequence = sample.sequence
                matched = 0
            self._note(stage, attempt, f"fresh matching frames {matched}/{needed}")
            if matched >= needed:
                return None
        expected = sample.expected_px or (0, 0)
        delivered = sample.delivered_px or (0, 0)
        return SetupFailure(
            SetupFailureKind.CAPTURE_STALE,
            stage,
            "capture did not settle on the fitted window",
            "Check that Screen Recording is enabled for this app, then press Retry Setup.",
            sample.error
            or f"expected {expected[0]}x{expected[1]}, delivered {delivered[0]}x{delivered[1]}",
        )

    # -- stage: profile ---------------------------------------------------
    def _select_profile(self) -> SetupFailure | None:
        stage = SetupStage.SELECT_PROFILE
        classifier = ProfileClassifier(self._candidates, self._config)
        decision = ProfileDecision(None, 0.0, 0.0, 0, "no frames yet")
        for attempt, _elapsed in self._poll(self._config.profile_deadline_s):
            vote = self._port.profile_vote()
            if vote is not None:
                classifier.observe(vote)
            decision = classifier.decide()
            self._note(stage, attempt, decision.detail)
            if decision.decided:
                assert decision.profile_id is not None
                self._port.lock_profile(decision.profile_id)
                self._profile_decision = decision
                self._progress = replace(self._progress, profile_id=decision.profile_id)
                return None
        self._profile_decision = decision
        return SetupFailure(
            SetupFailureKind.PROFILE_AMBIGUOUS,
            stage,
            "the equipped map could not be identified from the frames",
            "Equip a treasure map so its arrow is on screen, then press Retry Setup. "
            "You can also pick a profile by hand under Advanced.",
            decision.detail,
        )

    # -- stage: reference -------------------------------------------------
    def _establish_reference(self) -> SetupFailure | None:
        stage = SetupStage.ESTABLISH_REFERENCE
        needed = self._config.reference_stable_frames
        headings: list[float] = []
        last_sequence = -1
        for attempt, _elapsed in self._poll(self._config.reference_deadline_s):
            sample = self._port.perception_sample()
            if sample is None or sample.frame_sequence == last_sequence:
                continue
            last_sequence = sample.frame_sequence
            if not (sample.arrow_valid and sample.direction_valid) or sample.error_deg is None:
                headings.clear()
                self._note(stage, attempt, "waiting for a steady arrow reading")
                continue
            headings.append(sample.error_deg)
            if len(headings) > needed:
                headings.pop(0)
            self._note(stage, attempt, f"steady arrow readings {len(headings)}/{needed}")
            if len(headings) >= needed:
                jitter = _jitter_deg(headings)
                if jitter <= self._config.reference_max_jitter_deg:
                    self._reference = ReferenceCheck(
                        True,
                        len(headings),
                        jitter,
                        f"heading stable to {jitter:.1f} degrees over {len(headings)} frames",
                    )
                    return None
                headings.clear()
                self._note(
                    stage, attempt, f"heading jitter {jitter:.1f} degrees; still watching"
                )
        self._reference = ReferenceCheck(False, len(headings), 0.0, "no stable heading")
        return SetupFailure(
            SetupFailureKind.REFERENCE_UNSTABLE,
            stage,
            "the direction to the arrow never held still long enough to trust",
            "Stand still with the map arrow clearly visible, then press Retry Setup.",
            f"{len(headings)} steady frames of the {needed} required",
        )

    # -- stage: qualify ---------------------------------------------------
    def _shadow_qualify(self) -> SetupFailure | None:
        """Prove the whole read-only pipeline before offering to drive it."""
        stage = SetupStage.SHADOW_QUALIFY
        needed = self._config.qualify_frames
        hits = 0
        frames = 0
        last_sequence = -1
        fps = 0.0
        for attempt, _elapsed in self._poll(self._config.qualify_deadline_s):
            sample = self._port.perception_sample()
            if sample is None or sample.frame_sequence == last_sequence:
                continue
            last_sequence = sample.frame_sequence
            frames += 1
            fps = sample.processed_fps
            if sample.arrow_valid and sample.direction_valid:
                hits += 1
            self._note(
                stage, attempt, f"checked {frames}/{needed} frames, {hits} with an arrow"
            )
            if frames >= needed:
                break
        rate = hits / frames if frames else 0.0
        if frames < needed or rate < self._config.qualify_min_hit_rate:
            return SetupFailure(
                SetupFailureKind.REFERENCE_UNSTABLE,
                stage,
                "the arrow was not visible often enough to navigate by",
                "Make sure the treasure map arrow stays on screen, then press Retry Setup.",
                f"{hits} of {frames} frames carried a usable arrow "
                f"({rate * 100:.0f}%, need {self._config.qualify_min_hit_rate * 100:.0f}%)",
            )
        if fps and fps < self._config.qualify_min_fps:
            return SetupFailure(
                SetupFailureKind.CAPTURE_STALE,
                stage,
                f"only {fps:.0f} frames a second are being processed",
                "Close other heavy applications, or lower Roblox's graphics settings, "
                "then press Retry Setup.",
                f"need at least {self._config.qualify_min_fps:.0f} processed fps",
            )
        return None

    # -- stage: control mode ----------------------------------------------
    # -- stage: does the game take our input at all? ----------------------
    def _verify_input(self, control: ControlSetupPort) -> SetupFailure | None:
        """One bounded forward pulse. Released before this returns, always."""
        stage = SetupStage.VERIFY_INPUT
        try:
            result = control.input_acceptance()
        finally:
            control.release_turn()
        self._note(stage, 1, result.describe())
        self._acceptance = result
        if result.ok:
            return None
        return SetupFailure(
            SetupFailureKind.INPUT_NOT_ACCEPTED,
            stage,
            result.describe(),
            result.outcome.remedy,
            f"outcome={result.outcome.value} "
            f"idle_noise={result.idle_noise_norm:.4f} "
            f"threshold={result.threshold_norm:.4f} "
            f"post_edge_frames={result.post_edge_samples} "
            f"loopback={result.loopback} leases={','.join(result.leases_held) or 'none'}",
        )

    def _verify_control_mode(self, control: ControlSetupPort) -> SetupFailure | None:
        """Confirm the locked-camera control mode. Never toggles it.

        Shift Lock is the player's setting. Pressing Shift to "make sure" would
        turn it off for a player who already had it on, so the only thing this
        stage does is look.
        """
        stage = SetupStage.VERIFY_CONTROL_MODE
        sample = ControlModeSample(False, 0.0, "none", "not sampled")
        for attempt, _elapsed in self._poll(self._config.control_mode_deadline_s):
            sample = control.control_mode_sample()
            self._note(stage, attempt, sample.detail)
            if sample.verified:
                return None
        return SetupFailure(
            SetupFailureKind.CONTROL_MODE_UNVERIFIED,
            stage,
            "the camera control mode could not be confirmed",
            "Turn Shift Lock on in Roblox's settings (Camera > Shift Lock Switch) and "
            "press Start Navigator again.",
            sample.detail,
        )

    # -- stage: characterize ----------------------------------------------
    def _characterize_turn(
        self,
        control: ControlSetupPort,
        *,
        limits: TurnLimits | None,
        prior: TurnResponse | None,
    ) -> tuple[TurnResponse | None, SetupFailure | None]:
        stage = SetupStage.CHARACTERIZE_TURN
        characterizer = TurnCharacterizer(
            control.control_fingerprint(), limits=limits, prior=prior
        )
        last_sequence = -1
        for attempt, _elapsed in self._poll(self._config.characterize_deadline_s):
            observation = control.turn_observation()
            if observation is None or observation.frame_sequence == last_sequence:
                continue
            last_sequence = observation.frame_sequence
            probe = characterizer.step(observation)
            self._note(stage, attempt, probe.reason)
            if probe.done and probe.response is not None:
                control.release_turn()
                return (probe.response, None)
            if probe.failed:
                control.release_turn()
                return (
                    None,
                    SetupFailure(
                        SetupFailureKind.ACTUATOR_UNPROVEN,
                        stage,
                        "no way of turning the camera could be proven",
                        "Check that Roblox is focused and that the arrow keys or the "
                        "mouse rotate the camera, then press Start Navigator again.",
                        probe.reason,
                    ),
                )
            if probe.kind.value == "pulse" and probe.backend is not None:
                if not control.emit_turn(probe.backend.value, probe.units):
                    control.release_turn()
                    return (
                        None,
                        SetupFailure(
                            SetupFailureKind.ACTUATOR_UNPROVEN,
                            stage,
                            "the camera probe could not be sent to Roblox",
                            "Focus Roblox and press Start Navigator again.",
                            f"{probe.backend.value} probe refused",
                        ),
                    )
            else:
                control.release_turn()
        control.release_turn()
        return (
            None,
            SetupFailure(
                SetupFailureKind.TIMEOUT,
                stage,
                "measuring the camera turn took longer than allowed",
                "Stand still with the map arrow visible and press Start Navigator again.",
                f"{characterizer.probes_issued} probes issued",
            ),
        )

    # -- plumbing ---------------------------------------------------------
    #: Safety factor over the attempts a deadline can hold at the poll
    #: interval. The deadline is the real bound; the attempt cap exists so a
    #: clock that does not advance - a virtual one in a test, a suspended
    #: laptop - still terminates the loop.
    POLL_ATTEMPT_SLACK = 4

    def _poll(self, deadline_s: float) -> Iterator[tuple[int, float]]:
        """Yield ``(attempt, elapsed)`` until the deadline, honouring cancel.

        Every stage loop goes through here, which is how "every retry loop has
        both an attempt cap and a monotonic deadline" (CLAUDE.md section 6) is
        enforced structurally rather than remembered.
        """
        started = self._now()
        interval = max(self._config.poll_interval_s, 1e-4)
        max_attempts = int(deadline_s / interval) * self.POLL_ATTEMPT_SLACK + 16
        attempt = 0
        while attempt < max_attempts:
            self._check_cancelled()
            elapsed = self._now() - started
            if elapsed > deadline_s:
                return
            attempt += 1
            yield (attempt, elapsed)
            self._sleep(self._config.poll_interval_s)

    def _check_cancelled(self) -> None:
        if self._cancelled():
            raise SetupCancelled

    def _enter(self, stage: SetupStage, detail: str) -> None:
        now = self._now()
        self._progress = replace(
            self._progress,
            stage=stage,
            attempt=0,
            detail=detail,
            started_at_s=self._started_s or now,
            updated_at_s=now,
            failure=None,
        )
        self._publish(self._progress)

    def _note(self, stage: SetupStage, attempt: int, detail: str) -> None:
        if self._progress.stage is stage and self._progress.detail == detail:
            # Publishing an identical packet would wake every consumer for
            # nothing; the setup panel is emit-on-change like the rest.
            return
        self._progress = replace(
            self._progress,
            stage=stage,
            attempt=attempt,
            detail=detail,
            updated_at_s=self._now(),
        )
        self._publish(self._progress)

    def _fail(self, failure: SetupFailure) -> SetupProgress:
        self._progress = replace(
            self._progress,
            stage=SetupStage.FAILED,
            detail=failure.describe(),
            updated_at_s=self._now(),
            failure=failure,
        )
        self._publish(self._progress)
        return self._progress

    def _cancel(self, stage: SetupStage) -> SetupProgress:
        self._progress = replace(
            self._progress,
            stage=SetupStage.CANCELLED,
            detail=f"stopped during {stage.value.replace('_', ' ')}",
            updated_at_s=self._now(),
            failure=SetupFailure(
                SetupFailureKind.CANCELLED,
                stage,
                "automatic setup was stopped",
                "Press Start Navigator when you are ready.",
            ),
        )
        self._publish(self._progress)
        return self._progress

    def _succeed(self) -> SetupProgress:
        self._progress = replace(
            self._progress,
            stage=SetupStage.READY,
            detail="ready",
            updated_at_s=self._now(),
            failure=None,
        )
        self._publish(self._progress)
        return self._progress


def _jitter_deg(headings: Sequence[float]) -> float:
    """Peak-to-peak spread of a short heading window, wrapped correctly.

    Peak-to-peak rather than standard deviation because a single 40-degree
    excursion in an otherwise still window is exactly the thing that must not
    pass, and a standard deviation hides it behind the other five samples.
    """
    if len(headings) < 2:
        return 0.0
    reference = headings[0]
    unwrapped = [((value - reference + 180.0) % 360.0) - 180.0 for value in headings]
    return max(unwrapped) - min(unwrapped)
