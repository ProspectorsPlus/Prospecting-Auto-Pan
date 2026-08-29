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
    EvidenceStatus,
    InputKey,
    ModeResultKind,
    NavigationCommand,
    monotonic_s,
)
from prospector_engine.coordinator import WorkerContext
from prospector_engine.lifecycle import LifecycleJournal
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
    """A **real** :class:`MovementActuator` over a port that records edges.

    It used to reimplement the authority's admission rules by hand - the
    evidence budget, which target counts as a down edge, when a renewal is a
    renewal - and it drifted from them, which hid a real defect: a 160 ms pulse
    was refused outright by the real authority while this fake reported it
    applied. A fake laxer than the thing it stands in for is worse than no fake
    at all.

    There is no longer enough machinery to be worth faking. The actuator is
    small, so this uses the real one and fakes only the platform underneath it.
    """

    releases: list[str] = field(default_factory=list)
    refuse: bool = False
    posts: bool = True
    key_state_down: bool | None = True
    on_applied: Any = None
    on_released: Any = None
    lifecycle: LifecycleJournal = field(default_factory=LifecycleJournal)
    #: Kept because the navigator still refuses to *decide* on a stale frame.
    #: It is no longer a precondition on a key edge (D-067).
    evidence_budget_s: float = 0.1
    _port: Any = None
    _actuator: Any = None

    def __post_init__(self) -> None:
        from prospector_engine.movement import MovementActuator

        self._port = _EdgeRecorder(posts=self.posts)
        self._actuator = MovementActuator(
            self._port,
            deadman=None,
            focus_probe=(lambda: not self.refuse),
            journal=self.lifecycle,
        )
        self._actuator.start_watchdog()
        self._actuator.arm("prologue test")

    @property
    def movement(self) -> Any:
        return self._actuator

    def move(self, desired: Any) -> Any:
        self._port.posts = self.posts
        self._port.requests.append(desired)
        outcome = self._actuator.apply(desired)
        if self.on_applied is not None:
            self.on_applied(desired)
        return outcome

    def apply_command(self, command: NavigationCommand | None) -> Any:
        from prospector_engine.movement import desired_from_command

        return self.move(desired_from_command(command))

    @property
    def down_edges(self) -> int:
        """Physical key presses, as opposed to requests. A hold is one press."""
        return self._actuator.edge_counts[0]

    @property
    def commands(self) -> list[Any]:
        return self._port.requests

    def stop_moving(self, reason: str) -> Any:
        self.releases.append(reason)
        released = self._actuator.release_held(reason)
        if self.on_released is not None:
            self.on_released(reason)
        return released

    def release_navigation(self, reason: str) -> Any:
        self.releases.append(reason)
        self._actuator.release_all(reason)
        if self.on_released is not None:
            self.on_released(reason)


class _EdgeRecorder:
    """The smallest platform a real actuator can drive. Records, never posts."""

    def __init__(self, posts: bool = True) -> None:
        self.posts = posts
        self.edges: list[tuple[str, str]] = []
        self.requests: list[Any] = []
        self.deltas: list[int] = []

    def key_code(self, key: Any) -> int:
        return {"w": 13, "a": 0, "s": 1, "d": 2, "left": 123, "right": 124, "space": 49}[
            key.value
        ]

    def _name(self, code: int) -> str:
        return {13: "w", 0: "a", 1: "s", 2: "d", 123: "left", 124: "right", 49: "space"}[code]

    def raw_key_down(self, code: int) -> None:
        if not self.posts:
            # The game that never receives the edge: the post "succeeds" and
            # nothing downstream ever sees the key.
            return
        self.edges.append(("down", self._name(code)))

    def raw_key_up(self, code: int) -> None:
        self.edges.append(("up", self._name(code)))

    def raw_pointer_delta(self, dx: int, dy: int, held: Any = None) -> None:
        self.deltas.append(dx)


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

    def absorb(self, desired: Any) -> None:
        """Adopt what is *held* now. A hold is a state, not an impulse.

        It takes a :class:`~prospector_engine.movement.DesiredMovement`, which
        is the same shape this always wanted: a level, not an impulse. An
        earlier fake added rotation per accepted *command*, which made a
        renewal chain look like a burst of separate presses and rotated the
        camera by the whole hold on every renewal. A real key rotates for as
        long as it is down, once - so that is what this models.
        """
        self.walking = desired.forward == 1
        if desired.yaw_px:
            self.pending += desired.yaw_px
        if desired.turn and not self.held_turn:
            self.probes_seen += 1
        self.held_turn = desired.turn

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
    assert all(desired.turn == 0 for desired in session.commands)
    assert all(desired.yaw_px == 0 for desired in session.commands)
    assert session.releases, "the worker released on the way out"


def test_the_prologue_presses_forward_once_and_holds_it() -> None:
    """One press, and no second edge for as long as it is held.

    This used to say "one press, many renewals", because no command could
    outlive its own evidence and a hold had to be re-issued against every newer
    frame. The renewal chain is gone (D-067): the actuator is told what should
    be down and holds it until told otherwise, so the assertion is simply that
    forward went down once and came up once.
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
    assert any(desired.forward == 1 for desired in session.commands), "forward never pressed"
    downs = [key for kind, key in session._port.edges if kind == "down" and key == "w"]
    assert len(downs) == 1, f"forward was pressed {len(downs)} times for one hold"
    assert session.movement.empty, "the prologue returned still holding a key"


def test_forward_stays_down_across_frames_without_a_second_edge() -> None:
    """The hold is a level, not a chain, and pumping frames adds no edges.

    The old shape needed one command per frame to keep W down, and the test
    asserted there were *many* of them. Needing a fresh command every frame to
    keep a key down is the defect, not the feature: it is what made a hold as
    short as the pipeline was slow.
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
    port.next_motion(0.05)

    request = port.request_forward(250)
    assert request.holds_forward, request.detail
    assert session.down_edges == 1, "the press should be one edge"

    for _ in range(8):
        port.next_motion(0.1)

    assert session.down_edges == 1, "pumping frames re-pressed a key that was already down"
    assert InputKey.W in session.movement.held, "the hold did not survive eight frames"
    port.release_forward("done")
    assert session.movement.empty


def test_a_hold_the_actuator_dropped_stops_being_claimed() -> None:
    """If the key is no longer down, the probe must stop saying it is.

    Otherwise it measures a hold that had already ended, and reports the
    absence of movement as the game ignoring a key that was not being pressed.
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
    port.next_motion(0.05)
    assert port.request_forward(400).holds_forward

    # Another window comes to the front: the watchdog lifts everything.
    # The frontmost reading is cached for a beat, so wait past it rather than
    # racing the very caching that keeps the probe off the hot path.
    session.refuse = True
    time.sleep(session.movement.FOCUS_CACHE_S * 2)
    session.movement.poll()
    port.next_motion(0.1)

    assert port._forward_held is False
    assert session.movement.empty


def test_a_camera_probe_is_held_for_the_duration_the_ladder_asked_for() -> None:
    """The probes were taps because a command could not outlive its evidence.

    ``key_probe_ms`` climbs to 320 ms and none of it was reachable: the lease
    was clamped to the frame's budget, so every rung above about 80 ms sent the
    same short press, and a camera that needs a real press to move could not
    answer any of them. Now it is one press held for the asked-for duration.
    """
    session, capture = Session(), Capture()
    context = _context(session, capture)
    port = _LiveControlPort(
        context,
        PerceptionPipeline(segmenter=ArrowSegmenter(PROFILE)),
        fingerprint_factory=lambda: FINGERPRINT,
        control_mode_probe=lambda _frame: ControlModeSample(True, 0.9, "pointer", "test"),
    )
    port.next_motion(0.05)

    started = monotonic_s()
    assert port.emit_turn(TurnBackend.ARROW_KEYS.value, 200) is True
    elapsed_ms = (monotonic_s() - started) * 1000.0

    turn_downs = [
        key for kind, key in session._port.edges if kind == "down" and key in ("left", "right")
    ]
    assert len(turn_downs) == 1, f"the probe pressed {len(turn_downs)} times, not once"
    assert elapsed_ms >= 150, f"the probe only lasted {elapsed_ms:.0f} ms of the 200 asked for"
    assert session.movement.empty, "the probe returned still holding the key"


def test_a_probe_command_never_uses_two_turn_actuators() -> None:
    session, capture = Session(), Capture()
    worker = make_live_worker(
        lambda: PerceptionPipeline(segmenter=ArrowSegmenter(PROFILE)),
        lambda: NavigationCapabilities.observing(os_name="test", profile_id="green_arrow_v1"),
        prologue=_prologue(),
    )

    worker(_context(session, capture))

    assert session.commands
    for desired in session.commands:
        assert not (desired.turn != 0 and desired.yaw_px != 0)


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
