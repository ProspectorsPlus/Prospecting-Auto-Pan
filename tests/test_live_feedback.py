"""The wire between what the keyboard is holding and what the navigator knows.

This file exists because of one missing line, and because nothing in the test
suite could see it was missing.

``Navigator.note_held`` (formerly ``note_applied``) is what fills the
applied-forward ledger. The ledger is what tells the locomotion baseline how
long ``W`` has genuinely been down, and it is what tells the progress guard
whether "no motion" means an obstacle or means nothing was being commanded. The
live worker called ``session.apply_command(...)`` and then told the navigator
nothing at all. So for the whole of every Live session:

* held duration was zero, on every frame;
* the runtime locomotion baseline was never sampled, because its gate is
  ``held_ms >= 250``;
* the progress guard saw ``holding() == False`` forever and abstained;
* and obstacle recovery, all of which is downstream of that, could not
  activate - while looking, from the outside, like working code.

Every existing test passed because every existing test called ``note_applied``
by hand. So the rule this file enforces is: **the worker is the real one, the
authority is the real one, the actuator is the real one, and nothing in here
primes the navigator's internal state.** The only stand-ins are the platform
port, which records edges instead of posting them, and the perception pipeline,
which supplies scripted observations instead of running the detector - and the
detector has its own corpus.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from prospector_engine.capture import (
    CaptureConfig,
    CaptureService,
    EvidenceRegistry,
    ViewportGuard,
)
from prospector_engine.contracts import (
    ArrowObservation,
    ControlState,
    DiagnosticObservation,
    DirectionObservation,
    EvidenceStatus,
    InputKey,
    MotionObservation,
    NavigationPhase,
    PerformanceTier,
    PursuitTelemetry,
    monotonic_s,
)
from prospector_engine.coordinator import WorkerContext
from prospector_engine.input_authority import AuthorityConfig, HealthSources, InputAuthority
from prospector_engine.motion import UNCALIBRATED_BASELINE
from prospector_engine.navigation import (
    NavigationCapabilities,
    NavigationDecision,
    NavigationInputs,
    PerceptionResult,
    make_live_worker,
)
from prospector_engine.turning import TurnBackend, TurnResponse
from tests.fakes import (
    FakeCaptureSource,
    FakeDeadmanClient,
    FakePlatformPort,
    VirtualClock,
    make_frame,
    make_geometry,
    settle_cadence_for_live,
)
from tests.test_navigation import FINGERPRINT

#: Test-constructed, as CLAUDE.md rule 7 permits and requires this note: only a
#: test may build a measured turn response directly. Production reaches this
#: state exactly once, from bounded stationary probes on real hardware whose
#: observed rotation perception confirmed.
MEASURED = TurnResponse(
    backend=TurnBackend.ARROW_KEYS,
    fingerprint=replace(FINGERPRINT, backend="arrow_keys"),
    degrees_per_unit=0.09,
    positive_is_right=True,
    min_effective_units=40,
    max_units=900,
    latency_s=0.34,
    reliability=1.0,
    samples=8,
    measured_at_s=0.0,
    status=EvidenceStatus.VALIDATED,
)

#: Steering is proven; the walking speed deliberately is **not**. The point of
#: this file is that the baseline matures from ordinary Live operation, so
#: starting with one would hide the bug it exists to catch.
CAPABLE = NavigationCapabilities(
    os_name="test",
    profile_id="test",
    reference_ok=True,
    control_mode_ok=True,
    turn_response=MEASURED,
    motion_baseline=UNCALIBRATED_BASELINE,
)


@dataclass
class ScriptedPipeline:
    """Perception, scripted. Everything downstream of it is production code.

    It answers exactly the calls the observer loop makes, and it is the *only*
    thing in this test that a real Live session would do differently.
    """

    error_deg: float = 4.0
    #: Set to zero to simulate walking into something.
    forward_speed_norm: float = 0.30
    motion_enabled: bool = True
    profile_revision: int = 1
    frames_seen: int = 0
    observed: list[MotionObservation] = field(default_factory=list)

    @property
    def profile(self) -> Any:
        return _Profile()

    def analyze(self, frame: Any, *, map_id: str, approach_valid: bool) -> PerceptionResult:
        del map_id, approach_valid
        self.frames_seen += 1
        motion = MotionObservation(
            forward_speed_norm=self.forward_speed_norm,
            lateral_speed_norm=0.0,
            confidence=0.9,
            inlier_count=120,
            inlier_ratio=0.9,
            spatial_coverage=0.9,
            residual=0.4,
            yaw_contamination=0.0,
            valid=True,
        )
        self.observed.append(motion)
        return PerceptionResult(
            inputs=NavigationInputs(
                frame=frame,
                arrow=ArrowObservation(
                    profile_id="test",
                    track_id=1,
                    bbox_px=(0, 0, 10, 10),
                    centroid_px=(100.0, 100.0),
                    tip_px=(100.0, 90.0),
                    axis_unit_xy=(0.0, -1.0),
                    confidence=0.9,
                    valid=True,
                    abstain_reason=None,
                ),
                direction=DirectionObservation(
                    error_deg=self.error_deg,
                    confidence=0.85,
                    cue_id="scripted",
                    cue_disagreement_deg=1.0,
                    valid=True,
                    abstain_reason=None,
                ),
                motion=motion,
                arrival=None,
                forward_commanded=False,
            ),
            candidates=(),
            contour_px=(),
            desired_deg=self.error_deg,
            cues=(),
            perception_ms=1.0,
        )

    def diagnostic(
        self,
        frame: Any,
        result: PerceptionResult,
        decision: NavigationDecision,
        *,
        decision_ms: float,
        key: Any = None,
        control_state: ControlState | None = None,
        blockers: tuple[str, ...] = (),
        command_view: Any = None,
    ) -> DiagnosticObservation:
        del decision_ms, blockers
        return DiagnosticObservation(
            frame=frame,
            processed_at_s=frame.completed_at_s,
            published_at_s=monotonic_s(),
            key=key,
            profile_id="test",
            profile_status="validated",
            strategy_id="scripted",
            arrow=result.inputs.arrow,
            candidates=(),
            contour_px=(),
            anchor_px=(640.0, 400.0),
            forward_deg=0.0,
            forward_source="scripted",
            desired_deg=result.desired_deg,
            direction=result.inputs.direction,
            cues=(),
            motion=result.inputs.motion,
            arrival=None,
            phase=decision.phase,
            command=decision.command,
            abstain_reason=None,
            command_view=command_view,
            control_state=control_state,
            pursuit=decision.telemetry,
        )


@dataclass
class _Profile:
    profile_id: str = "test"
    status: EvidenceStatus = EvidenceStatus.VALIDATED


class Rig:
    """Real authority, real actuator, real live worker; fake port and pixels."""

    def __init__(self) -> None:
        self.port = FakePlatformPort(VirtualClock(), geometry=make_geometry())
        self.guard = ViewportGuard(self.port)
        self.guard.connect()
        self.deadman = FakeDeadmanClient()
        self.source = FakeCaptureSource(frames=[make_frame(1).bgr])
        held: list[InputAuthority] = []
        self.registry = EvidenceRegistry(
            "feedback-run", on_token=lambda token: held[0].register_evidence(token)
        )
        self.capture = CaptureService(
            self.guard,
            self.registry,
            config=CaptureConfig(start_tier=PerformanceTier.STANDARD, max_frame_age_ms=100_000),
            source_factory=lambda: self.source,
        )
        self.authority = InputAuthority(
            self.port,
            deadman=self.deadman,
            health=HealthSources(
                focus=self.port.focus_state,
                client_rect=lambda: self.guard.geometry if self.guard.geometry.valid else None,
                capture_age_s=self.capture.latest_age_s,
            ),
            config=AuthorityConfig(),
            run_id="feedback-run",
        )
        held.append(self.authority)
        assert self.capture.start()
        # The production admission step the coordinator performs on entering an
        # input-emitting mode. Nothing here is a shortcut: it opens a generation
        # and arms the actuator exactly as ``_start_worker`` does, and the
        # cadence gate below is satisfied by the real governor's own
        # measurements rather than assigned.
        settle_cadence_for_live(self.capture)
        self.authority.activate_generation(1, emits_input=True, requires_capture=False)
        self.authority.movement.start_watchdog()
        self.pipeline = ScriptedPipeline()
        self.phases: list[NavigationPhase] = []
        #: Only the telemetry, never the observation. A ``DiagnosticObservation``
        #: holds its own ``CapturedFrame``, and a frame holds a buffer out of the
        #: capture pool - so keeping every observation pins every buffer and the
        #: pipeline stops after eight frames with "pool exhausted". The
        #: production consumers keep the latest packet only, for exactly this
        #: reason; a test rig that keeps them all is not modelling one.
        self.pursuit: list[PursuitTelemetry] = []
        self.statuses: list[str] = []
        self._cancelled = threading.Event()
        self.session = self.authority.navigation_session(self.authority.generation)
        self.context = WorkerContext(
            generation=self.authority.generation,
            mode=__import__("prospector_engine.contracts", fromlist=["RunMode"]).RunMode.LIVE,
            worker_id="feedback-worker",
            cancellation=_Cancel(self._cancelled),
            frames=self.capture,
            navigation=self.session,
            pipeline=self.pipeline,
            on_phase=self.phases.append,
            on_observation=self._note_observation,
            on_status=self.statuses.append,
        )
        self.worker = make_live_worker(lambda: self.pipeline, lambda: CAPABLE, prologue=None)
        self.result: Any = None
        self._thread: threading.Thread | None = None

    def _note_observation(self, observation: DiagnosticObservation) -> None:
        if observation.pursuit is not None:
            self.pursuit.append(observation.pursuit)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self.result = self.worker(self.context)

    def stop(self) -> None:
        self._cancelled.set()
        self.authority.movement.stop_watchdog(1.0)
        if self._thread is not None:
            self._thread.join(5.0)
        self.capture.stop(2.0)
        self.authority.release_all("test-teardown")

    def edges(self, key: InputKey) -> tuple[int, int]:
        code = self.port.key_code(key)
        ops = self.port.ops()
        downs = [i for i, (op, args) in enumerate(ops) if op == "key_down" and args == (code,)]
        after = downs[0] if downs else len(ops)
        ups = sum(1 for op, args in ops[after:] if op == "key_up" and args == (code,))
        return (len(downs), ups)


class _Cancel:
    def __init__(self, event: threading.Event) -> None:
        self._event = event

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    monkeypatch.setenv("TREASURE_DATA_DIR", str(tmp_path / "data"))
    made = Rig()
    yield made
    made.stop()


def _await(predicate: Any, timeout_s: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# The wire
# ---------------------------------------------------------------------------


def test_the_real_live_worker_presses_forward_and_keeps_it_down(rig: Rig) -> None:
    rig.start()

    assert _await(lambda: rig.edges(InputKey.W)[0] >= 1), "the live worker never pressed W"
    assert _await(lambda: rig.pipeline.frames_seen > 40), "perception never ran"

    downs, ups = rig.edges(InputKey.W)
    assert downs == 1, f"{downs} presses over {rig.pipeline.frames_seen} frames"
    assert ups == 0, "the hold was interrupted"


def test_the_held_key_reaches_the_navigator_without_anyone_calling_note_applied(
    rig: Rig,
) -> None:
    """The P0. Nothing in this test primes the ledger; the only thing that can
    fill it is the worker reporting the actuator's own answer."""
    rig.start()
    assert _await(lambda: rig.edges(InputKey.W)[0] >= 1)

    assert _await(lambda: any("W" in telemetry.held_keys for telemetry in list(rig.pursuit))), (
        "the navigator was never told what the keyboard is holding"
    )

    latest = list(rig.pursuit)[-1]
    assert latest.held_keys, "the telemetry reported an empty keyboard while W was down"
    assert _await(
        lambda: any(telemetry.forward_held_ms > 200.0 for telemetry in list(rig.pursuit))
    ), "the applied-forward ledger never accumulated a real hold"


def test_the_locomotion_baseline_matures_from_ordinary_walking(rig: Rig) -> None:
    """It could not, before. The estimator's gate is ``held_ms >= 250``, the
    held duration came from the applied-forward ledger, and the ledger was
    never written - so no Live session on any machine ever measured one."""
    rig.start()

    assert _await(
        lambda: any("walking speed measured" in line for line in list(rig.statuses)),
        timeout_s=20.0,
    ), "no Live run could ever measure a walking speed"

    assert _await(
        lambda: any(telemetry.baseline_norm is not None for telemetry in list(rig.pursuit))
    )


def test_a_stall_reaches_recovery_with_nothing_primed_by_hand(rig: Rig) -> None:
    """Walk normally until the baseline exists, then stop the world moving.

    Every step from there is production code: the guard notices, confirms in
    motion, the capability permits a maneuver, and the ladder runs one. The
    test's only intervention is to make the scene stop moving.
    """
    rig.start()
    assert _await(
        lambda: any("walking speed measured" in line for line in list(rig.statuses)),
        timeout_s=20.0,
    ), "the baseline never matured, so a stall could not mean anything"

    rig.pipeline.forward_speed_norm = 0.0

    assert _await(lambda: NavigationPhase.RECOVERY in list(rig.phases), timeout_s=20.0), (
        "a confirmed stall never reached recovery"
    )

    recovery = next(
        telemetry for telemetry in reversed(list(rig.pursuit)) if telemetry.recovery_rung
    )
    assert recovery.recovery_rung == "R0", "the ladder did not start at the running hop"
    assert recovery.recovery_side in (-1, 1), "no side was locked for the episode"


def test_the_first_recovery_rung_jumps_without_letting_go_of_forward(rig: Rig) -> None:
    rig.start()
    assert _await(
        lambda: any("walking speed measured" in line for line in list(rig.statuses)),
        timeout_s=20.0,
    )
    rig.pipeline.forward_speed_norm = 0.0
    assert _await(lambda: NavigationPhase.RECOVERY in list(rig.phases), timeout_s=20.0)

    assert _await(lambda: rig.edges(InputKey.SPACE)[0] >= 1, timeout_s=10.0), (
        "the running hop never pressed SPACE"
    )
    # W went down once, at the start of the walk, and recovery did not lift it
    # to jump. That combination is the whole point of the rung.
    assert rig.edges(InputKey.W)[1] == 0, "the running hop released the walk to jump"


def test_stopping_releases_everything_and_the_ledger_is_empty(rig: Rig) -> None:
    rig.start()
    assert _await(lambda: rig.edges(InputKey.W)[0] >= 1)

    rig.stop()

    assert rig.authority.ledger_empty()
    assert rig.edges(InputKey.W)[1] >= 1, "forward was never released"
    assert not rig.authority.release_uncertain
