"""Automatic setup: the happy path, and a bounded failure at every stage.

The point of these tests is that the *user-visible* answer is right. A stage
that cannot finish must stop with a typed failure kind and one sentence naming
the next action - not spin, not silently continue, and not leave the machine in
a state where pressing the button again does nothing. That was precisely the
behaviour of the commissioning window this replaces.

The port is a fake, so no window, no capture, no input and no game is involved.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from prospector_engine.acceptance import AcceptanceOutcome, AcceptanceResult
from prospector_engine.autosetup import (
    AutomaticSetup,
    CaptureSample,
    ControlModeSample,
    PerceptionSample,
    ProfileClassifier,
    ProfileVote,
    SetupConfig,
)
from prospector_engine.contracts import (
    EvidenceStatus,
    FitPhase,
    SetupFailureKind,
    SetupStage,
    ViewportFit,
)
from prospector_engine.geometry import ViewportGeometry
from prospector_engine.lifecycle import LifecycleStage
from prospector_engine.turning import ControlFingerprint, TurnBackend, TurnObservation
from tests.fakes import make_geometry

FAST = SetupConfig(
    poll_interval_s=0.0,
    find_window_deadline_s=1.0,
    fit_deadline_s=1.0,
    capture_restart_deadline_s=1.0,
    capture_stable_frames=3,
    profile_deadline_s=1.0,
    profile_min_frames=4,
    reference_deadline_s=1.0,
    reference_stable_frames=3,
    qualify_deadline_s=1.0,
    qualify_frames=5,
    control_mode_deadline_s=1.0,
    characterize_deadline_s=5.0,
)

FINGERPRINT = ControlFingerprint(
    os_name="macos",
    backend="unset",
    client_fingerprint="roblox-test",
    camera_sensitivity="default",
    control_mode="shift_lock",
    viewport_identity=(1280, 720),
    profile_id="green_arrow_v1",
    profile_revision=1,
    supported_min_fps=30,
)


class Clock:
    """A virtual clock the machine's sleeps advance. No wall-clock waiting."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(seconds, 0.01)


def _fit(
    phase: FitPhase = FitPhase.CANONICAL_VERIFIED, detail: str = "verified"
) -> ViewportFit:
    return ViewportFit(
        phase=phase,
        attempt=1,
        stable_readbacks=3,
        required_readbacks=3,
        requested_client_logical=(1280.0, 720.0),
        achieved_client_logical=(1280.0, 720.0),
        achieved_client_backing_px=(2560, 1440),
        geometry=make_geometry(),
        detail=detail,
    )


@dataclass
class FakePort:
    """A cooperative Roblox. Every failure mode is a field, not a monkeypatch."""

    window: Any = field(default=None)
    fit: ViewportFit = field(default_factory=_fit)
    geometry: ViewportGeometry = field(default_factory=make_geometry)
    delivered_px: tuple[int, int] | None = (1280, 720)
    capture_error: str | None = None
    winner: str = "green_arrow_v1"
    runner_up: float = 0.1
    arrow_valid: bool = True
    heading_deg: float = 12.0
    heading_jitter: float = 0.0
    heals: int = 0
    #: Whether asking to re-adopt actually fixes it, as it does natively
    #: when the guard lost its pin to one bad read.
    heal_clears_error: bool = False
    processed_fps: float = 60.0
    sequence: int = 0
    released: list[str] = field(default_factory=list)
    restarts: list[str] = field(default_factory=list)
    locked: list[str] = field(default_factory=list)
    fits: int = 0

    def __post_init__(self) -> None:
        from prospector_engine.autosetup import WindowProbe

        if self.window is None:
            self.window = WindowProbe(True, "Roblox found", identity=(1,))

    def locate_window(self) -> Any:
        return self.window

    def release_all_input(self, reason: str) -> None:
        self.released.append(reason)

    def fit_viewport(self) -> ViewportFit:
        self.fits += 1
        return self.fit

    def viewport(self) -> ViewportGeometry:
        return self.geometry

    def restart_capture(self, reason: str) -> None:
        self.restarts.append(reason)

    def heal_viewport(self) -> bool:
        """Re-adopt after a transient unpin. Records, and clears the error once."""
        self.heals += 1
        if self.heal_clears_error:
            self.capture_error = None
        return True

    def capture_sample(self) -> CaptureSample:
        self.sequence += 1
        return CaptureSample(
            sequence=self.sequence,
            age_s=0.01,
            delivered_px=self.delivered_px,
            expected_px=(1280, 720),
            processed_fps=self.processed_fps,
            error=self.capture_error,
        )

    def profile_vote(self) -> ProfileVote:
        self.sequence += 1
        scores = {"green_arrow_v1": self.runner_up, "yellow_map_v1": self.runner_up}
        scores[self.winner] = 0.9
        return ProfileVote(self.sequence, scores)

    def lock_profile(self, profile_id: str) -> None:
        self.locked.append(profile_id)

    def perception_sample(self) -> PerceptionSample:
        self.sequence += 1
        drift = self.heading_jitter * (1 if self.sequence % 2 else -1)
        return PerceptionSample(
            frame_sequence=self.sequence,
            arrow_valid=self.arrow_valid,
            direction_valid=self.arrow_valid,
            error_deg=self.heading_deg + drift if self.arrow_valid else None,
            confidence=0.9,
            processed_fps=self.processed_fps,
        )


def _setup(port: FakePort, **kwargs: Any) -> AutomaticSetup:
    clock = Clock()
    published: list[Any] = []
    machine = AutomaticSetup(
        port,
        config=FAST,
        publish=published.append,
        now=clock.now,
        sleep=clock.sleep,
        candidates=("green_arrow_v1", "yellow_map_v1"),
        **kwargs,
    )
    machine.published = published  # type: ignore[attr-defined]
    return machine


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_a_cooperative_roblox_reaches_ready_with_no_manual_step() -> None:
    port = FakePort()
    machine = _setup(port)
    progress = machine.run_observation()
    assert progress.stage is SetupStage.READY
    assert progress.failure is None
    assert port.locked == ["green_arrow_v1"]
    assert port.restarts, "capture must be rebound after the fit"


def test_input_is_released_before_the_window_is_ever_resized() -> None:
    port = FakePort()
    _setup(port).run_observation()
    assert port.released, "a held key across a resize is a key in a moved window"


def test_capture_is_restarted_exactly_once() -> None:
    port = FakePort()
    _setup(port).run_observation()
    assert len(port.restarts) == 1


def test_every_stage_is_published_in_order() -> None:
    port = FakePort()
    machine = _setup(port)
    machine.run_observation()
    order = [p.stage for p in machine.published]  # type: ignore[attr-defined]
    for stage in (
        SetupStage.FIND_ROBLOX,
        SetupStage.FIT_VIEWPORT,
        SetupStage.RESTART_CAPTURE,
        SetupStage.STABILIZE_CAPTURE,
        SetupStage.SELECT_PROFILE,
        SetupStage.ESTABLISH_REFERENCE,
        SetupStage.SHADOW_QUALIFY,
        SetupStage.READY,
    ):
        assert stage in order, stage


def test_the_reference_check_reports_a_measured_jitter_not_a_claim() -> None:
    port = FakePort(heading_jitter=1.0)
    machine = _setup(port)
    machine.run_observation()
    reference = machine.reference
    assert reference is not None and reference.stable
    assert reference.provenance.status is EvidenceStatus.PROVISIONAL
    assert reference.jitter_deg > 0.0


# ---------------------------------------------------------------------------
# Bounded failure at every stage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "kind", "needle"),
    [
        (
            lambda p: setattr(p, "window", _probe(found=False)),
            SetupFailureKind.NO_WINDOW,
            "windowed",
        ),
        (
            lambda p: setattr(p, "window", _probe(found=False, ambiguous=True)),
            SetupFailureKind.AMBIGUOUS_WINDOW,
            "Close the extra",
        ),
        (
            lambda p: setattr(p, "window", _probe(found=False, permission_denied=True)),
            SetupFailureKind.PERMISSION,
            "Privacy",
        ),
        (
            lambda p: setattr(p, "window", _probe(found=False, fullscreen=True)),
            SetupFailureKind.FULLSCREEN,
            "fullscreen",
        ),
        (
            lambda p: setattr(p, "fit", _fit(FitPhase.FAILED, "Accessibility refused")),
            SetupFailureKind.PERMISSION,
            "Accessibility",
        ),
        (
            lambda p: setattr(p, "fit", _fit(FitPhase.FAILED, "the client never settled")),
            SetupFailureKind.RESIZE_DENIED,
            "ordinary window",
        ),
        (
            lambda p: setattr(p, "delivered_px", (1024, 768)),
            SetupFailureKind.CAPTURE_STALE,
            "Screen Recording",
        ),
        (
            lambda p: setattr(p, "capture_error", "source stopped"),
            SetupFailureKind.CAPTURE_STALE,
            "Screen Recording",
        ),
        (
            lambda p: setattr(p, "runner_up", 0.88),
            SetupFailureKind.PROFILE_AMBIGUOUS,
            "Equip a treasure map",
        ),
        (
            lambda p: setattr(p, "arrow_valid", False),
            SetupFailureKind.REFERENCE_UNSTABLE,
            "Stand still",
        ),
        (
            lambda p: setattr(p, "heading_jitter", 45.0),
            SetupFailureKind.REFERENCE_UNSTABLE,
            "Stand still",
        ),
    ],
)
def test_each_stage_fails_closed_with_a_typed_kind_and_one_action(
    mutate: Any, kind: SetupFailureKind, needle: str
) -> None:
    port = FakePort()
    mutate(port)
    progress = _setup(port).run_observation()
    assert progress.stage is SetupStage.FAILED
    assert progress.failure is not None
    assert progress.failure.kind is kind, progress.failure
    assert needle.lower() in progress.failure.remedy.lower(), progress.failure.remedy


def _probe(**kwargs: Any) -> Any:
    from prospector_engine.autosetup import WindowProbe

    defaults: dict[str, Any] = {"found": True, "detail": "probe"}
    defaults.update(kwargs)
    return WindowProbe(**defaults)


def test_a_clamped_but_usable_viewport_still_reaches_ready() -> None:
    """A clamp is a truthful answer, not a failure (plan 4.1)."""
    port = FakePort(fit=_fit(FitPhase.ACHIEVED_CLAMPED, "clamped to 1024x768"))
    progress = _setup(port).run_observation()
    assert progress.stage is SetupStage.READY


def test_a_low_frame_rate_stops_setup_with_an_actionable_message() -> None:
    port = FakePort(processed_fps=4.0)
    progress = _setup(port).run_observation()
    assert progress.stage is SetupStage.FAILED
    assert progress.failure is not None
    assert progress.failure.kind is SetupFailureKind.CAPTURE_STALE
    assert "graphics" in progress.failure.remedy


def test_cancellation_stops_setup_rather_than_failing_it() -> None:
    port = FakePort()
    machine = _setup(port, cancelled=lambda: True)
    progress = machine.run_observation()
    assert progress.stage is SetupStage.CANCELLED
    assert progress.failure is not None
    assert progress.failure.kind is SetupFailureKind.CANCELLED


def test_an_exploding_port_becomes_a_typed_internal_failure() -> None:
    class Exploding(FakePort):
        def capture_sample(self) -> CaptureSample:
            raise RuntimeError("boom")

    progress = _setup(Exploding()).run_observation()
    assert progress.stage is SetupStage.FAILED
    assert progress.failure is not None
    assert progress.failure.kind is SetupFailureKind.INTERNAL


# ---------------------------------------------------------------------------
# Profile classification
# ---------------------------------------------------------------------------


def test_a_clear_winner_over_enough_frames_is_locked() -> None:
    classifier = ProfileClassifier(("a", "b"), FAST)
    for sequence in range(8):
        classifier.observe(ProfileVote(sequence, {"a": 0.9, "b": 0.2}))
    decision = classifier.decide()
    assert decision.profile_id == "a"
    assert decision.margin > 0.5


def test_two_profiles_that_score_alike_are_reported_as_ambiguous() -> None:
    classifier = ProfileClassifier(("a", "b"), FAST)
    for sequence in range(8):
        classifier.observe(ProfileVote(sequence, {"a": 0.80, "b": 0.79}))
    decision = classifier.decide()
    assert not decision.decided
    assert "within" in decision.detail


def test_a_profile_that_wins_only_occasionally_does_not_win_the_session() -> None:
    classifier = ProfileClassifier(("a", "b"), FAST)
    for sequence in range(10):
        # 'a' wins big but rarely; the *mean* margin alone would elect it.
        scores = {"a": 0.95, "b": 0.4} if sequence < 4 else {"a": 0.3, "b": 0.45}
        classifier.observe(ProfileVote(sequence, scores))
    decision = classifier.decide()
    assert not decision.decided


def test_one_frame_votes_once_however_often_it_is_offered() -> None:
    classifier = ProfileClassifier(("a", "b"), FAST)
    for _ in range(20):
        classifier.observe(ProfileVote(7, {"a": 0.9, "b": 0.1}))
    assert classifier.frames == 1


def test_no_profile_matching_anything_is_ambiguous_not_a_coin_flip() -> None:
    classifier = ProfileClassifier(("a", "b"), FAST)
    for sequence in range(8):
        classifier.observe(ProfileVote(sequence, {"a": 0.0, "b": 0.0}))
    assert not classifier.decide().decided


# ---------------------------------------------------------------------------
# The control phase
# ---------------------------------------------------------------------------


@dataclass
class FakeControl:
    verified: bool = True
    gain: float = 0.3
    responds: bool = True
    sequence: int = 0
    error_deg: float = 30.0
    releases: int = 0
    emitted: list[tuple[str, int]] = field(default_factory=list)
    refuse_emit: bool = False
    _pending: int = 0

    #: What the bounded forward pulse concluded. The default is the happy path,
    #: so a test about the *camera* does not have to describe the keyboard.
    accepts_input: bool = True

    def input_acceptance(self) -> AcceptanceResult:
        if self.accepts_input:
            return AcceptanceResult(
                AcceptanceOutcome.MOVED,
                "walked",
                idle_noise_norm=0.002,
                threshold_norm=0.02,
                moved_speed_norm=0.4,
                post_edge_samples=4,
                loopback=True,
                leases_held=("w",),
            )
        return AcceptanceResult(
            AcceptanceOutcome.NO_MOTION,
            "held forward and nothing moved",
            first_missing=LifecycleStage.GAME_MOTION_CONFIRMED,
            idle_noise_norm=0.002,
            threshold_norm=0.02,
            post_edge_samples=4,
            loopback=True,
            leases_held=("w",),
        )

    def control_mode_sample(self) -> ControlModeSample:
        return ControlModeSample(
            self.verified, 0.9, "visual_cue", "Shift Lock cue" if self.verified else "no cue"
        )

    def turn_observation(self) -> TurnObservation:
        self.sequence += 1
        if self._pending:
            rotation = self._pending * self.gain
            self.error_deg -= rotation
            self._pending = 0
        return TurnObservation(
            frame_sequence=self.sequence,
            now_s=self.sequence / 60.0,
            error_deg=self.error_deg,
            confidence=0.9,
            stationary=True,
        )

    def emit_turn(self, backend_value: str, units: int) -> bool:
        self.emitted.append((backend_value, units))
        if self.refuse_emit:
            return False
        if self.responds:
            self._pending = units
        return True

    def release_turn(self) -> None:
        self.releases += 1

    def control_fingerprint(self) -> ControlFingerprint:
        return FINGERPRINT


def test_the_control_phase_measures_a_turn_response_automatically() -> None:
    control = FakeControl()
    machine = _setup(FakePort())
    progress, response = machine.run_control(control)
    assert progress.stage is SetupStage.READY, progress.detail
    assert response is not None and response.usable
    assert response.backend in set(TurnBackend)
    assert control.releases > 0, "every probe must be released"


def test_an_unverified_control_mode_stops_before_any_probe() -> None:
    control = FakeControl(verified=False)
    progress, response = _setup(FakePort()).run_control(control)
    assert progress.stage is SetupStage.FAILED
    assert progress.failure is not None
    assert progress.failure.kind is SetupFailureKind.CONTROL_MODE_UNVERIFIED
    assert "Shift Lock" in progress.failure.remedy
    assert response is None
    assert control.emitted == []


def test_a_camera_that_never_moves_fails_closed_and_releases() -> None:
    control = FakeControl(responds=False)
    progress, response = _setup(FakePort()).run_control(control)
    assert progress.stage is SetupStage.FAILED
    assert progress.failure is not None
    assert progress.failure.kind in (
        SetupFailureKind.ACTUATOR_UNPROVEN,
        SetupFailureKind.TIMEOUT,
    )
    assert response is None
    assert control.releases > 0


def test_a_refused_probe_edge_stops_rather_than_pretending_it_landed() -> None:
    control = FakeControl(refuse_emit=True)
    progress, response = _setup(FakePort()).run_control(control)
    assert progress.stage is SetupStage.FAILED
    assert progress.failure is not None
    assert progress.failure.kind is SetupFailureKind.ACTUATOR_UNPROVEN
    assert response is None


def test_the_measured_response_carries_its_own_provenance() -> None:
    _progress, response = _setup(FakePort()).run_control(FakeControl())
    assert response is not None
    assert response.provenance.status is EvidenceStatus.VALIDATED
    assert "this run" in response.provenance.source
    assert "stationary" in response.provenance.note


def test_the_control_phase_does_not_reuse_a_stale_fingerprint() -> None:
    _progress, response = _setup(FakePort()).run_control(FakeControl())
    assert response is not None
    assert response.fingerprint.backend == response.backend.value
    assert response.fingerprint.matches_except_backend(replace(FINGERPRINT, backend="x"))


def test_a_stage_loop_terminates_even_if_the_clock_never_advances() -> None:
    """The deadline is the real bound; the attempt cap catches a frozen clock."""
    port = FakePort(delivered_px=(1024, 768))
    machine = AutomaticSetup(
        port,
        config=replace(FAST, poll_interval_s=0.0),
        now=lambda: 0.0,  # a clock that never moves
        sleep=lambda _s: None,
        candidates=("green_arrow_v1", "yellow_map_v1"),
    )

    progress = machine.run_observation()

    assert progress.stage is SetupStage.FAILED
    assert progress.failure is not None


def test_a_transient_unpinned_viewport_is_healed_rather_than_timed_out() -> None:
    """Observed natively, intermittently, right after a successful fit.

    The frames really are the fitted 1280x720 and the guard reports UNPINNED
    with no adopted window, so every delivery is rejected as a mismatch and
    setup used to fail with ``capture_stale`` on a condition the capture
    supervisor heals a moment later - just on a slower poll than this stage's
    deadline. Setup asks directly instead of timing out; re-adopting binds to
    the client and moves no window.
    """
    port = FakePort()
    port.capture_error = (
        "delivered 1280x720 from canonical_verified but the viewport is unpinned"
    )
    port.heal_clears_error = True

    progress = _setup(port).run_observation()

    assert port.heals >= 1, "setup never asked the guard to re-adopt"
    assert progress.stage is SetupStage.READY, progress.failure


def test_a_viewport_that_does_not_heal_still_fails_closed() -> None:
    """Healing is bounded, not a licence to spin: a real fault still stops."""
    port = FakePort()
    port.capture_error = "source stopped"
    port.heal_clears_error = False

    progress = _setup(port).run_observation()

    assert progress.stage is SetupStage.FAILED
    assert progress.failure is not None
    assert progress.failure.kind is SetupFailureKind.CAPTURE_STALE
