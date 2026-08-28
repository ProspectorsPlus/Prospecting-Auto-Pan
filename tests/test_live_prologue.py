"""The armed half: the prologue that runs before the first movement command.

Automatic setup can reach READY on its own. It cannot arm, and it cannot
measure the camera, because measuring the camera means moving it. Those two
stages therefore run *inside* the live worker, after a human has clicked Arm
Live and pressed a hotkey with Roblox focused, with the character stationary.

What is asserted here is the shape of that: the prologue confirms the control
mode, measures a turn response, releases after every probe, never presses `W`,
and - when it cannot prove a way to turn - releases and reports rather than
steering blind.

The session is a fake that records every command. No authority, no port, no
game.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from prospector_engine.autosetup import AutomaticSetup, ControlModeSample, SetupConfig
from prospector_engine.contracts import (
    Cancellation,
    EvidenceStatus,
    ModeResultKind,
    NavigationApplyResult,
    NavigationApplyStatus,
    NavigationCommand,
)
from prospector_engine.coordinator import WorkerContext
from prospector_engine.navigation import (
    NavigationCapabilities,
    PerceptionPipeline,
    make_live_prologue,
    make_live_worker,
)
from prospector_engine.turning import ControlFingerprint, TurnBackend, TurnLimits
from prospector_engine.vision import ArrowSegmenter, load_profiles
from tests.fakes import make_frame

PROFILE = load_profiles().get("green_arrow_v1")
assert PROFILE is not None

FINGERPRINT = ControlFingerprint(
    os_name="test",
    backend="unset",
    client_fingerprint="client",
    camera_sensitivity="default",
    control_mode="shift_lock",
    viewport_identity=(1280, 720),
    profile_id="green_arrow_v1",
    profile_revision=1,
    supported_min_fps=30,
)

FAST = SetupConfig(
    poll_interval_s=0.0, control_mode_deadline_s=0.5, characterize_deadline_s=3.0
)


@dataclass
class Session:
    """Records what the prologue asked the authority to do."""

    commands: list[NavigationCommand] = field(default_factory=list)
    releases: list[str] = field(default_factory=list)
    refuse: bool = False
    on_applied: Any = None

    def apply_navigation_command(self, command: NavigationCommand, evidence: Any) -> Any:
        del evidence
        self.commands.append(command)
        if self.refuse:
            return NavigationApplyResult(NavigationApplyStatus.REJECTED_FOCUS, "not focused")
        if self.on_applied is not None:
            self.on_applied(command)
        held = []
        if command.turn_axis == -1:
            held.append("left")
        elif command.turn_axis == 1:
            held.append("right")
        return NavigationApplyResult(NavigationApplyStatus.APPLIED, command.reason, tuple(held))

    def release_navigation(self, reason: str) -> Any:
        self.releases.append(reason)


@dataclass
class Capture:
    """Rendered frames whose arrow rotates when the camera is told to.

    The frames are wiring stress, exactly as plan 7.2 permits: nothing here
    judges the detector and no evidence gate is passed on their output. What
    they provide is a *real* heading reading, so the characterizer has
    something to observe and the probes are not vacuously skipped.
    """

    gain_deg_per_unit: float = 0.25
    responds: bool = True
    sequence: int = 0
    heading_deg: float = 40.0
    pending: int = 0
    probes_seen: int = 0

    def __post_init__(self) -> None:
        from tests.arrow_fixtures import render_scene

        # Pre-rendered at five-degree steps: rendering per frame would dominate
        # the test, and the characterizer only needs the heading to move.
        self._scenes = {
            step: render_scene(
                heading_deg=float(step), terrain="grass", scale_px=100.0, seed=7
            ).bgr
            for step in range(-180, 180, 5)
        }
        from prospector_engine.capture import EvidenceRegistry

        self._registry = EvidenceRegistry("prologue")

    def wait_for_new(self, after_sequence: int, timeout_s: float) -> Any:
        del timeout_s, after_sequence
        self.sequence += 1
        if self.pending:
            self.probes_seen += 1
            if self.responds:
                # Turning the camera right reduces the heading to the arrow.
                self.heading_deg -= self.pending * self.gain_deg_per_unit
            self.pending = 0
        step = int(round(self.heading_deg / 5.0) * 5)
        step = max(-180, min(175, step))
        frame = make_frame(
            self.sequence, captured_at_s=time.monotonic(), bgr=self._scenes[step]
        )
        return self._registry.envelope_for(frame)

    def absorb(self, command: NavigationCommand) -> None:
        """Turn an accepted command into camera motion on the next frame."""
        if command.yaw_delta_px:
            self.pending += command.yaw_delta_px
        elif command.turn_axis:
            # A held key rotates for as long as it is down; the prologue holds
            # it for one lease, so the hold in milliseconds is the magnitude.
            span_s = command.valid_until_s - command.issued_at_s
            hold_ms = max(1, round(span_s * 1000))
            self.pending += command.turn_axis * hold_ms

    def note_perception_ms(self, value: float) -> None: ...

    def note_decision_ms(self, value: float) -> None: ...

    def note_end_to_end_ms(self, value: float) -> None: ...


def _context(session: Session, capture: Capture) -> WorkerContext:
    session.on_applied = capture.absorb
    return WorkerContext(
        generation=1,
        mode=None,  # type: ignore[arg-type]
        worker_id="live-test",
        cancellation=Cancellation(),
        frames=capture,  # type: ignore[arg-type]
        navigation=session,  # type: ignore[arg-type]
        pipeline=PerceptionPipeline(segmenter=ArrowSegmenter(PROFILE)),
    )


def _prologue(*, verified: bool = True, on_measured: Any = None) -> Any:
    return make_live_prologue(
        fingerprint_factory=lambda: FINGERPRINT,
        control_mode_probe=lambda _frame: ControlModeSample(
            verified, 0.9 if verified else 0.0, "pointer", "test"
        ),
        capabilities_factory=lambda: NavigationCapabilities.observing(
            os_name="test", profile_id="green_arrow_v1"
        ),
        setup_factory=lambda cancelled: AutomaticSetup(
            _NullPort(), config=FAST, cancelled=cancelled, sleep=lambda _s: None
        ),
        turn_limits=TurnLimits(
            settle_frames=1, observe_frames=1, repeats_per_direction=1, max_probes=12
        ),
        on_measured=on_measured,
    )


class _NullPort:
    """The observation-phase port the control phase never calls."""

    def locate_window(self) -> Any: ...
    def release_all_input(self, reason: str) -> None: ...
    def fit_viewport(self) -> Any: ...
    def viewport(self) -> Any: ...
    def restart_capture(self, reason: str) -> None: ...
    def capture_sample(self) -> Any: ...
    def profile_vote(self) -> Any: ...
    def lock_profile(self, profile_id: str) -> None: ...
    def perception_sample(self) -> Any: ...


# ---------------------------------------------------------------------------
# The prologue itself
# ---------------------------------------------------------------------------


def test_an_unverified_control_mode_stops_before_a_single_probe() -> None:
    session, capture = Session(), Capture()
    worker = make_live_worker(
        lambda: PerceptionPipeline(segmenter=ArrowSegmenter(PROFILE)),
        lambda: NavigationCapabilities.observing(os_name="test", profile_id="green_arrow_v1"),
        prologue=_prologue(verified=False),
    )

    result = worker(_context(session, capture))

    assert result.kind is ModeResultKind.FAILED
    assert "Shift Lock" in result.detail
    assert session.commands == [], "a probe was issued before the mode was confirmed"
    assert session.releases, "the worker released on the way out"


def test_the_prologue_never_presses_forward() -> None:
    """Every probe is a stationary rotation. `W` belongs to FOLLOW."""
    session, capture = Session(), Capture()
    worker = make_live_worker(
        lambda: PerceptionPipeline(segmenter=ArrowSegmenter(PROFILE)),
        lambda: NavigationCapabilities.observing(os_name="test", profile_id="green_arrow_v1"),
        prologue=_prologue(),
    )

    worker(_context(session, capture))

    assert session.commands, "the prologue never probed at all"
    assert capture.probes_seen > 0, "no probe reached the simulated camera"
    assert all(command.forward_axis == 0 for command in session.commands)
    assert all(command.jump is False for command in session.commands)
    assert all(command.lateral_axis == 0 for command in session.commands)


def test_a_probe_that_is_refused_stops_rather_than_pretending_it_landed() -> None:
    session, capture = Session(refuse=True), Capture()
    worker = make_live_worker(
        lambda: PerceptionPipeline(segmenter=ArrowSegmenter(PROFILE)),
        lambda: NavigationCapabilities.observing(os_name="test", profile_id="green_arrow_v1"),
        prologue=_prologue(),
    )

    result = worker(_context(session, capture))

    assert result.kind is ModeResultKind.FAILED
    assert session.releases


def test_a_camera_that_never_moves_fails_closed_and_releases() -> None:
    session, capture = Session(), Capture(responds=False)
    worker = make_live_worker(
        lambda: PerceptionPipeline(segmenter=ArrowSegmenter(PROFILE)),
        lambda: NavigationCapabilities.observing(os_name="test", profile_id="green_arrow_v1"),
        prologue=_prologue(),
    )

    result = worker(_context(session, capture))

    assert result.kind is ModeResultKind.FAILED
    assert session.releases
    assert "turn" in result.detail.lower() or "camera" in result.detail.lower()


def test_a_probe_command_never_uses_two_turn_actuators() -> None:
    session, capture = Session(), Capture()
    worker = make_live_worker(
        lambda: PerceptionPipeline(segmenter=ArrowSegmenter(PROFILE)),
        lambda: NavigationCapabilities.observing(os_name="test", profile_id="green_arrow_v1"),
        prologue=_prologue(),
    )

    worker(_context(session, capture))

    assert session.commands
    for command in session.commands:
        assert not (command.turn_axis != 0 and command.yaw_delta_px != 0)


def test_a_key_probe_is_never_longer_than_one_lease() -> None:
    """A hold nobody renews is cut short, and the measured gain would be wrong."""
    from prospector_engine.navigation import _LiveControlPort

    assert max(TurnLimits().key_probe_ms) <= _LiveControlPort.MAX_PROBE_HOLD_MS


def test_a_measured_response_is_handed_back_and_remembered() -> None:
    """The one output of the prologue: a response the follower can steer with."""
    measured: list[Any] = []
    session, capture = Session(), Capture()
    worker = make_live_worker(
        lambda: PerceptionPipeline(segmenter=ArrowSegmenter(PROFILE)),
        lambda: NavigationCapabilities.observing(os_name="test", profile_id="green_arrow_v1"),
        prologue=_prologue(on_measured=measured.append),
    )

    result = worker(_context(session, capture))

    assert measured, f"nothing was measured: {result.detail}"
    response = measured[0]
    assert response.status is EvidenceStatus.VALIDATED
    assert response.backend in set(TurnBackend)
    assert response.usable
    assert response.positive_is_right is True, "the simulated camera turns right"
    assert response.degrees_per_unit > 0.0
    assert response.samples >= 2, "the sign is confirmed, not inferred from one probe"


def test_a_slower_camera_is_measured_as_slower_not_as_broken() -> None:
    """The gain is measured per machine; a coarse camera gets a wider deadband."""
    fast: list[Any] = []
    slow: list[Any] = []
    for gain, sink in ((0.25, fast), (0.05, slow)):
        session, capture = Session(), Capture(gain_deg_per_unit=gain)
        worker = make_live_worker(
            lambda: PerceptionPipeline(segmenter=ArrowSegmenter(PROFILE)),
            lambda: NavigationCapabilities.observing(
                os_name="test", profile_id="green_arrow_v1"
            ),
            prologue=_prologue(on_measured=sink.append),
        )
        worker(_context(session, capture))

    assert fast and slow
    assert fast[0].degrees_per_unit > slow[0].degrees_per_unit
    assert slow[0].min_effective_units > fast[0].min_effective_units
