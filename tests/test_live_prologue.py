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
from typing import Any, ClassVar

import numpy as np

from prospector_engine.autosetup import AutomaticSetup, ControlModeSample, SetupConfig
from prospector_engine.contracts import (
    Cancellation,
    CommandKind,
    EvidenceStatus,
    ModeResultKind,
    NavigationApplyResult,
    NavigationApplyStatus,
    NavigationCommand,
)
from prospector_engine.coordinator import WorkerContext
from prospector_engine.lifecycle import LifecycleJournal, LifecycleStage
from prospector_engine.navigation import (
    NavigationCapabilities,
    PerceptionPipeline,
    _LiveControlPort,
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
    """Records what the prologue asked the authority to do.

    It notes ``OS_EDGE_POSTED`` and ``LEASE_HELD`` into the journal because the
    real ``InputAuthority`` does, and the acceptance probe reads the journal to
    tell "the post never happened" from "the game ignored it".
    """

    commands: list[NavigationCommand] = field(default_factory=list)
    releases: list[str] = field(default_factory=list)
    refuse: bool = False
    posts: bool = True
    key_state_down: bool | None = True
    on_applied: Any = None
    on_released: Any = None
    lifecycle: LifecycleJournal = field(default_factory=LifecycleJournal)
    _down: set[str] = field(default_factory=set)
    _edges: int = 0

    #: The authority's own budget. The fake enforces it because not enforcing
    #: it hid a real defect: a 160 ms pulse built from one command is refused
    #: outright by the real authority - "command lease exceeds evidence age" -
    #: so the probe never pressed anything, and this fake happily reported it
    #: applied. A fake that is laxer than the thing it stands in for is worse
    #: than no fake at all.
    evidence_budget_s: float = 0.1

    def apply_navigation_command(self, command: NavigationCommand, evidence: Any) -> Any:
        del evidence
        self.commands.append(command)
        if self.refuse:
            return NavigationApplyResult(NavigationApplyStatus.REJECTED_FOCUS, "not focused")
        if command.valid_until_s > command.source_captured_at_s + self.evidence_budget_s:
            over_ms = (
                command.valid_until_s - command.source_captured_at_s - self.evidence_budget_s
            ) * 1000.0
            return NavigationApplyResult(
                NavigationApplyStatus.REJECTED_EVIDENCE,
                f"command lease exceeds evidence age by {over_ms:.0f} ms",
            )
        held = []
        if command.turn_axis == -1:
            held.append("left")
        elif command.turn_axis == 1:
            held.append("right")
        if command.forward_axis == 1:
            held.append("w")
        if self.posts:
            for target in held:
                # Only the *first* command for a target is a down edge; the
                # rest renew it, exactly as the real authority does.
                if target not in self._down:
                    self._down.add(target)
                    self._edges += 1
                    self.lifecycle.note(LifecycleStage.OS_EDGE_POSTED, target, target=target)
                    self.lifecycle.note(LifecycleStage.LEASE_HELD, target, target=target)
        if self.on_applied is not None:
            self.on_applied(command)
        return NavigationApplyResult(NavigationApplyStatus.APPLIED, command.reason, tuple(held))

    @property
    def down_edges(self) -> int:
        """Physical key presses, as opposed to commands. A hold is one press."""
        return self._edges

    def release_navigation(self, reason: str) -> Any:
        self.releases.append(reason)
        self._down.clear()
        if self.on_released is not None:
            self.on_released(reason)


@dataclass
class Capture:
    """Rendered frames whose arrow rotates when the camera is told to.

    The frames are wiring stress, exactly as plan 7.2 permits: nothing here
    judges the detector and no evidence gate is passed on their output. What
    they provide is a *real* heading reading, so the characterizer has
    something to observe and the probes are not vacuously skipped.
    """

    #: Degrees per millisecond of held key. Sized for the probe ladder as it
    #: now stands (60-320 ms) at this fake's frame rate: the smallest rung must
    #: clear ``TurnLimits.min_observable_deg`` and the largest must stay well
    #: under ``max_probe_deg``.
    gain_deg_per_unit: float = 0.03
    responds: bool = True
    sequence: int = 0
    heading_deg: float = 40.0
    pending: int = 0
    probes_seen: int = 0
    #: Whether holding forward makes the ground flow. ``False`` is the game
    #: that takes the key and does nothing with it.
    walks: bool = True
    walking: bool = False
    scroll_px: int = 0
    held_turn: int = 0
    _next_frame_s: float | None = None

    #: The fake's frame interval, in milliseconds - both the pace frames are
    #: delivered at and the time a held key is charged for per frame.
    FRAME_MS: ClassVar[float] = 16.0

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
        # Paced. A fake that returns instantly has no frame rate, so a hold
        # bounded by wall clock consumes as many frames as the machine can
        # spin - hundreds on a fast one, a handful on a loaded one - and every
        # measurement taken across it becomes a measurement of the machine.
        now = time.monotonic()
        if self._next_frame_s is not None and now < self._next_frame_s:
            time.sleep(self._next_frame_s - now)
        self._next_frame_s = time.monotonic() + self.FRAME_MS / 1000.0
        self.sequence += 1
        if self.pending:
            if self.responds:
                # Turning the camera right reduces the heading to the arrow.
                self.heading_deg -= self.pending * self.gain_deg_per_unit
            self.pending = 0
        if self.held_turn and self.responds:
            # A held key rotates for as long as it is down - the whole
            # difference between a press and a tap - and here that is charged
            # per *frame the key was down for*, at a fixed simulated interval.
            #
            # Deliberately not wall clock. The hold is bounded by real time, so
            # a fake charging real elapsed milliseconds is a function of how
            # loaded the machine running the test happens to be: on a busy one
            # a single stalled frame rotates the camera past
            # ``TurnLimits.max_probe_deg`` and the probe is discarded as
            # contaminated. Per frame, any number of frames from one upward
            # gives a measurable rotation and a bounded one.
            self.heading_deg -= self.held_turn * self.gain_deg_per_unit * self.FRAME_MS
        if self.walking and self.walks:
            # The whole scene flows past: what walking forward looks like to a
            # flow estimator. Rendered stress for wiring only (plan 7.2); no
            # gate is passed on it.
            self.scroll_px += 9
        step = int(round(self.heading_deg / 5.0) * 5)
        step = max(-180, min(175, step))
        scene = self._scenes[step]
        if self.scroll_px:
            scene = np.roll(scene, self.scroll_px % scene.shape[0], axis=0)
        frame = make_frame(
            self.sequence, captured_at_s=time.monotonic(), bgr=np.ascontiguousarray(scene)
        )
        return self._registry.envelope_for(frame)

    def absorb(self, command: NavigationCommand) -> None:
        """Adopt what is *held* now. A hold is a state, not an impulse.

        The earlier fake added rotation per accepted command, which made a
        renewal chain look like a burst of separate presses and rotated the
        camera by the whole hold on every renewal. A real key rotates for as
        long as it is down, once - so that is what this models.
        """
        self.walking = command.forward_axis == 1
        if command.yaw_delta_px:
            self.pending += command.yaw_delta_px
        if command.turn_axis and not self.held_turn:
            self.probes_seen += 1
        self.held_turn = command.turn_axis

    def released(self, _reason: str) -> None:
        self.walking = False
        self.held_turn = 0

    def note_perception_ms(self, value: float) -> None: ...

    def note_decision_ms(self, value: float) -> None: ...

    def note_end_to_end_ms(self, value: float) -> None: ...


def _context(session: Session, capture: Capture) -> WorkerContext:
    session.on_applied = capture.absorb
    session.on_released = capture.released
    return WorkerContext(
        generation=1,
        mode=None,  # type: ignore[arg-type]
        worker_id="live-test",
        cancellation=Cancellation(),
        frames=capture,  # type: ignore[arg-type]
        navigation=session,  # type: ignore[arg-type]
        pipeline=PerceptionPipeline(segmenter=ArrowSegmenter(PROFILE)),
        lifecycle=session.lifecycle,
        # The OS agrees the key is down exactly when the fake authority says a
        # lease is held; ``key_state_down`` scripts the case where it does not.
        key_state=lambda _key: session.key_state_down,
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

    def heal_viewport(self) -> bool:
        return True

    def capture_sample(self) -> Any: ...
    def profile_vote(self) -> Any: ...
    def lock_profile(self, profile_id: str) -> None: ...
    def perception_sample(self) -> Any: ...


# ---------------------------------------------------------------------------
# The prologue itself
# ---------------------------------------------------------------------------


def test_an_unverified_control_mode_stops_before_a_single_camera_probe() -> None:
    session, capture = Session(), Capture()
    worker = make_live_worker(
        lambda: PerceptionPipeline(segmenter=ArrowSegmenter(PROFILE)),
        lambda: NavigationCapabilities.observing(os_name="test", profile_id="green_arrow_v1"),
        prologue=_prologue(verified=False),
    )

    result = worker(_context(session, capture))

    assert result.kind is ModeResultKind.FAILED
    assert "Shift Lock" in result.detail
    # The acceptance pulse ran and passed - that is the stage before this one -
    # and no *camera* probe was issued after the mode failed to confirm.
    assert capture.probes_seen == 0, "a camera probe was issued before the mode was confirmed"
    assert all(command.turn_axis == 0 for command in session.commands)
    assert all(command.yaw_delta_px == 0 for command in session.commands)
    assert session.releases, "the worker released on the way out"


def test_the_prologue_presses_forward_once_and_holds_it_by_renewing() -> None:
    """One press, many renewals - and that distinction is the whole defect.

    No command may outlive its own evidence, so the longest hold a *single*
    command can express is the evidence budget minus the age the frame already
    had: about 80 ms, and a 160 ms request is not shortened, it is refused
    outright. A hold is therefore the same command re-issued against each newer
    frame, which the authority turns into a renewal with no second down edge.

    So the assertion is on *edges*, not on commands: exactly one W press, held
    across many commands, released before the prologue returns.
    """
    session, capture = Session(), Capture()
    worker = make_live_worker(
        lambda: PerceptionPipeline(segmenter=ArrowSegmenter(PROFILE)),
        lambda: NavigationCapabilities.observing(os_name="test", profile_id="green_arrow_v1"),
        prologue=_prologue(),
    )

    worker(_context(session, capture))

    assert session.commands, "the prologue never probed at all"
    assert capture.probes_seen > 0, "no probe reached the simulated camera"
    forward = [command for command in session.commands if command.forward_axis == 1]
    assert forward, "forward was never pressed at all"
    assert all(command.kind is CommandKind.FOLLOW for command in forward)
    assert session.down_edges >= 1

    # Every one of them fits inside its own frame's evidence budget, which is
    # what makes the chain acceptable to the real authority rather than
    # rejected as "command lease exceeds evidence age". How *many* there are
    # depends on how fast frames arrive, so the length of the chain is pinned
    # deterministically in ``test_forward_is_held_by_renewing_not_by_one_lease``
    # rather than here, where it would be measuring the machine.
    for command in forward:
        span_s = command.valid_until_s - command.source_captured_at_s
        assert span_s <= session.evidence_budget_s + 1e-9, f"{span_s * 1000:.0f} ms lease"

    assert capture.walking is False, "forward was still held when the prologue returned"
    assert all(command.jump is False for command in session.commands)
    assert all(command.lateral_axis == 0 for command in session.commands)


def test_forward_is_held_by_renewing_not_by_one_lease() -> None:
    """The renewal chain, pinned where the machine cannot affect the answer.

    A single command's lease may not outlive its evidence, so the longest hold
    one command can express is the budget minus the age the frame already had -
    about 80 ms in production, and a 160 ms request is not shortened but
    *refused*. Driven directly here, with the port's own paced frames, so the
    length of the chain is a property of the code and not of the load.
    """
    from prospector_engine.navigation import _LiveControlPort

    session, capture = Session(), Capture()
    context = _context(session, capture)
    port = _LiveControlPort(
        context,
        PerceptionPipeline(segmenter=ArrowSegmenter(PROFILE)),
        fingerprint_factory=lambda: FINGERPRINT,
        control_mode_probe=lambda _frame: ControlModeSample(True, 0.9, "pointer", "test"),
    )
    port.next_motion(0.05)  # adopt a frame to build the first command from

    request = port.request_forward(250)
    assert request.holds_forward, request.detail
    assert session.down_edges == 1, "the press should be one edge"

    # Pumping frames is what renews it; each is a fresh evidence token.
    for _ in range(8):
        port.next_motion(0.1)

    forward = [command for command in session.commands if command.forward_axis == 1]
    assert len(forward) > 1, "forward was tapped once, not held by renewal"
    assert session.down_edges == 1, "a renewal must not press the key again"
    for command in forward:
        span_s = command.valid_until_s - command.source_captured_at_s
        assert span_s <= session.evidence_budget_s + 1e-9, f"{span_s * 1000:.0f} ms lease"

    # The chain spans longer than any single command's lease could have.
    held_s = forward[-1].issued_at_s - forward[0].issued_at_s
    assert held_s > session.evidence_budget_s, "the chain held nothing extra"

    port.release_forward("test")
    assert capture.walking is False


def test_a_renewal_that_is_refused_stops_claiming_the_key_is_down() -> None:
    """A refused renewal means the key is coming up. Saying otherwise would
    have the probe measure a hold that had already ended."""
    from prospector_engine.navigation import _LiveControlPort

    session, capture = Session(), Capture()
    context = _context(session, capture)
    port = _LiveControlPort(
        context,
        PerceptionPipeline(segmenter=ArrowSegmenter(PROFILE)),
        fingerprint_factory=lambda: FINGERPRINT,
        control_mode_probe=lambda _frame: ControlModeSample(True, 0.9, "pointer", "test"),
    )
    port.next_motion(0.05)
    assert port.request_forward(400).holds_forward

    session.refuse = True
    port.next_motion(0.1)
    assert port._forward_held is False

    before = len([c for c in session.commands if c.forward_axis == 1])
    port.next_motion(0.1)
    after = len([c for c in session.commands if c.forward_axis == 1])
    assert after == before, "it kept renewing a hold that had been refused"


def test_a_camera_probe_is_held_for_the_duration_the_ladder_asked_for() -> None:
    """The probes were taps because a command cannot outlive its evidence.

    ``key_probe_ms`` climbs to 320 ms, and none of it was reachable: the lease
    was clamped to the frame's budget, so every rung above about 80 ms sent the
    same short press. A camera that needs a real press to move could not answer
    any of them, and the backend was written off as unproven.

    Driven directly rather than through the characterizer, because the ladder
    stops as soon as the camera answers and a test that measured the number of
    probes would be measuring how cooperative the fake is.
    """

    session, capture = Session(), Capture()
    context = _context(session, capture)
    port = _LiveControlPort(
        context,
        PerceptionPipeline(segmenter=ArrowSegmenter(PROFILE)),
        fingerprint_factory=lambda: FINGERPRINT,
        control_mode_probe=lambda _frame: ControlModeSample(True, 0.9, "pointer", "test"),
    )
    port.next_motion(0.05)  # adopt a frame to build the first command from

    hold_ms = 200
    assert port.emit_turn(TurnBackend.ARROW_KEYS.value, hold_ms) is True

    turns = [command for command in session.commands if command.turn_axis]
    assert len(turns) > 1, "the probe was one command - it was tapped, not held"
    assert all(command.turn_axis == turns[0].turn_axis for command in turns)

    # Every command fits its own frame's budget, which is what the authority
    # requires; the *chain* is what holds the key past it.
    for command in turns:
        span_s = command.valid_until_s - command.source_captured_at_s
        assert span_s <= session.evidence_budget_s + 1e-9, f"{span_s * 1000:.0f} ms lease"

    held_s = turns[-1].issued_at_s - turns[0].issued_at_s
    assert held_s >= (hold_ms / 1000.0) * 0.5, f"held only {held_s * 1000:.0f} ms of {hold_ms}"
    assert session.releases, "the probe returned without releasing the key"
    assert capture.held_turn == 0


def test_a_game_that_takes_the_key_and_does_nothing_stops_before_the_camera() -> None:
    """The stage that made the thirty-second timeout unnecessary.

    Characterizing a camera turn assumes the game is acting on our input. When
    it is not, that stage spends its whole deadline proving nothing and reports
    a timeout - which names the wrong problem. One pulse answers it first.
    """
    session, capture = Session(), Capture(walks=False)
    worker = make_live_worker(
        lambda: PerceptionPipeline(segmenter=ArrowSegmenter(PROFILE)),
        lambda: NavigationCapabilities.observing(os_name="test", profile_id="green_arrow_v1"),
        prologue=_prologue(),
    )

    result = worker(_context(session, capture))

    assert result.kind is ModeResultKind.FAILED
    assert "not acting on the key" in result.detail
    assert capture.probes_seen == 0, "the camera was probed anyway"
    assert session.lifecycle.reached(LifecycleStage.GAME_MOTION_NOT_CONFIRMED)
    assert capture.walking is False


def test_an_edge_the_os_never_registered_is_named_as_such() -> None:
    session, capture = Session(key_state_down=False), Capture()
    worker = make_live_worker(
        lambda: PerceptionPipeline(segmenter=ArrowSegmenter(PROFILE)),
        lambda: NavigationCapabilities.observing(os_name="test", profile_id="green_arrow_v1"),
        prologue=_prologue(),
    )

    result = worker(_context(session, capture))

    assert result.kind is ModeResultKind.FAILED
    assert "does not report the key as down" in result.detail
    assert session.lifecycle.reached(LifecycleStage.OS_EDGE_LOOPBACK_MISSING)
    assert capture.probes_seen == 0


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
    for gain, sink in ((0.03, fast), (0.010, slow)):
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
