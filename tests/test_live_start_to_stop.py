"""Setup ready -> Ctrl+N -> Live -> W held for many frames -> turn -> Stop.

The whole path, in one test, through the real coordinator, the real input
authority, the real lease ledger and the real release floor. Only two things
are stand-ins, and both are named:

* the **platform port** is a fake that records every edge instead of posting
  it, because a test that pressed W on the developer's machine would be the
  thing the whole repository exists to prevent;
* the **live worker** is a scripted driver rather than the perception loop,
  because what is under test here is the wiring between a physical chord and a
  key that stays down - not the detector, which has its own corpus.

Everything between those two is production code. In particular the chord is
minted by the coordinator's own :class:`ChordAuthority`, so this test cannot
start Live by any route a person could not.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from prospector_engine.capture import (
    CaptureConfig,
    CaptureService,
    EvidenceRegistry,
    ViewportGuard,
)
from prospector_engine.contracts import (
    InputKey,
    IntentType,
    ModeResult,
    ModeResultKind,
    PerformanceTier,
    RunMode,
    monotonic_s,
)
from prospector_engine.coordinator import CoordinatorConfig, RuntimeCoordinator, WorkerContext
from prospector_engine.input_authority import AuthorityConfig, HealthSources, InputAuthority
from prospector_engine.lifecycle import LIVE_ENTRY_PATH, LifecycleStage
from prospector_engine.movement import DesiredMovement
from tests.arrow_fixtures import render_scene
from tests.fakes import (
    FakeCaptureSource,
    FakeDeadmanClient,
    FakePlatformPort,
    VirtualClock,
    make_geometry,
    settle_cadence_for_live,
)


class Rig:
    """The production wiring, with a fake port and a scripted live worker."""

    #: Frames the scripted worker walks for before it turns.
    WALK_FRAMES = 25
    #: Frames it spends turning while still walking. Ordinary corrective yaw
    #: must not drop the forward hold. Enough for several of each sign.
    TURN_FRAMES = 8

    def __init__(self, tmp_path: Any) -> None:
        from prospector_engine.telemetry import resolve_app_paths

        self.port = FakePlatformPort(VirtualClock(), geometry=make_geometry())
        self.guard = ViewportGuard(self.port)
        self.guard.connect()
        self.deadman = FakeDeadmanClient()
        frames = [
            render_scene(heading_deg=float(a), terrain="grass", scale_px=100.0, seed=a).bgr
            for a in (30, 45, 60)
        ]
        self.source = FakeCaptureSource(frames=frames)
        # One registry, built before the capture service and wired to the
        # authority through a late-bound cell. Two registries - one for
        # capture, one for the authority - is exactly how a token comes back
        # "not issued by this authority", and the whole point of this test is
        # that the *production* wiring holds.
        held: list[InputAuthority] = []
        self.registry = EvidenceRegistry(
            "live-e2e-run", on_token=lambda token: held[0].register_evidence(token)
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
            run_id="live-e2e-run",
        )
        held.append(self.authority)
        self.walked = threading.Event()
        self.turned = threading.Event()
        self.applied = 0
        self.rejections: list[str] = []
        self.coordinator = RuntimeCoordinator(
            authority=self.authority,
            guard=self.guard,
            capture=self.capture,
            registry=self.registry,
            workers={IntentType.START_LIVE: self._worker},
            config=CoordinatorConfig(idle_poll_s=0.01),
            paths=resolve_app_paths().ensure(),
        )
        assert self.capture.start()
        self.coordinator.start()

    # -- the scripted live worker -----------------------------------------
    def _worker(self, context: WorkerContext) -> ModeResult:
        """Walk for many frames, then turn while still walking, then hold."""
        session = context.navigation
        assert session is not None, "Live must hand the worker a navigation session"
        context.lifecycle.note(LifecycleStage.LIVE_WORKER_ENTERED, context.worker_id)
        sequence = 0
        issued = 0
        try:
            while not context.cancellation.is_cancelled():
                envelope = context.frames.wait_for_new(sequence, 0.25)
                if envelope is None:
                    continue
                sequence = envelope.frame.sequence
                issued += 1
                turning = self.WALK_FRAMES < issued <= self.WALK_FRAMES + self.TURN_FRAMES
                outcome = session.move(
                    DesiredMovement(
                        forward=1,
                        # Sign alternates so both directions are exercised.
                        yaw_px=(8 if issued % 2 else -8) if turning else 0,
                        reason="turning while walking" if turning else "walking",
                    )
                )
                if outcome.block.blocking:
                    self.rejections.append(outcome.block.name)
                    continue
                self.applied += 1
                if issued >= self.WALK_FRAMES:
                    self.walked.set()
                if turning:
                    self.turned.set()
        finally:
            session.release_navigation("worker-exit")
        return ModeResult(ModeResultKind.CANCELLED, f"{issued} frames")

    # -- helpers -----------------------------------------------------------
    def chord(self, intent: IntentType) -> None:
        if intent is IntentType.START_LIVE:
            # The production cadence gate, satisfied through the production
            # governor rather than skipped.
            settle_cadence_for_live(self.capture)
        self.coordinator.submit(self.coordinator.chord_authority().intent(intent, "Ctrl+N"))

    def yaw_deltas(self) -> list[int]:
        return [args[0] for op, args in self.port.ops() if op == "drag_delta"]

    def both_signs_posted(self) -> bool:
        deltas = self.yaw_deltas()
        return any(d > 0 for d in deltas) and any(d < 0 for d in deltas)

    def edges(self, key: InputKey) -> tuple[int, int]:
        """Down edges for ``key``, and up edges *after the first down*.

        Counting every up would count the release floor: entering an input mode
        lifts the whole vocabulary unconditionally before the worker starts, so
        there is always one up for W before anything has pressed it. That is
        the safety property working, not a rattle.
        """
        code = self.port.key_code(key)
        ops = self.port.ops()
        indices = [
            i for i, (op, args) in enumerate(ops) if op == "key_down" and args == (code,)
        ]
        downs = len(indices)
        after = indices[0] if indices else len(ops)
        ups = sum(1 for op, args in ops[after:] if op == "key_up" and args == (code,))
        return (downs, ups)

    def close(self) -> None:
        self.coordinator.shutdown(2.0)
        self.capture.stop(2.0)


@pytest.fixture
def rig(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("TREASURE_DATA_DIR", str(tmp_path / "data"))
    made = Rig(tmp_path)
    yield made
    made.close()


def _await(predicate: Any, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# The whole path
# ---------------------------------------------------------------------------


def test_one_chord_walks_turns_and_stops_with_an_empty_ledger(rig: Rig) -> None:
    rig.chord(IntentType.START_LIVE)
    assert _await(lambda: rig.coordinator.mode is RunMode.LIVE), "the chord never entered Live"

    assert _await(rig.walked.is_set), "the worker never sustained a walk"
    assert _await(rig.both_signs_posted), "both turn directions were never posted"

    # Held throughout: many frames, one press.
    downs, ups = rig.edges(InputKey.W)
    assert downs == 1, f"{downs} down edges across {rig.applied} applied commands"
    assert ups == 0, "the hold was interrupted mid-walk"
    assert rig.authority.forward_held_s(monotonic_s()) > 0.0

    # Both signed yaw directions actually reached the port, while W stayed down.
    deltas = rig.yaw_deltas()
    assert any(d > 0 for d in deltas), "no positive yaw was posted"
    assert any(d < 0 for d in deltas), "no negative yaw was posted"
    assert rig.edges(InputKey.W)[1] == 0, "corrective yaw dropped the forward hold"
    assert rig.authority.held_targets() == ("w",)
    assert rig.authority.hold_lapses == {}

    # Snapshot before Stop: a command racing the transition is correctly
    # refused with "admission-closed", and counting that as a walking refusal
    # would make this test fail on the safety property it wants.
    while_walking = list(rig.rejections)
    rig.chord(IntentType.STOP)

    assert _await(lambda: rig.coordinator.mode is RunMode.IDLE), "Stop did not land"
    assert rig.authority.ledger_empty()
    assert rig.edges(InputKey.W)[1] >= 1, "forward was never released"
    assert not rig.authority.release_uncertain
    # The one refusal a correct run may contain is the worker's very first
    # command, built from a frame captured before the transition into Live -
    # whose evidence the transition release deliberately invalidated.
    assert len(while_walking) <= 1, f"commands were refused: {while_walking[:3]}"


def test_the_causal_chain_is_recorded_end_to_end(rig: Rig) -> None:
    rig.chord(IntentType.START_LIVE)
    assert _await(lambda: rig.coordinator.mode is RunMode.LIVE)
    assert _await(rig.walked.is_set)
    rig.chord(IntentType.STOP)
    assert _await(lambda: rig.coordinator.mode is RunMode.IDLE)

    stages = [row["stage"] for row in rig.authority.lifecycle.rows()]
    for stage in LIVE_ENTRY_PATH:
        if stage is LifecycleStage.CHORD_RECOGNIZED:
            continue  # the listener's own note; there is no listener here
        assert stage.value in stages, f"{stage.value} never happened"
    for stage in (
        LifecycleStage.OS_EDGE_POSTED,
        LifecycleStage.LEASE_HELD,
        LifecycleStage.W_HOLD_CONFIRMED,
        LifecycleStage.W_RELEASE_POSTED,
        LifecycleStage.LEDGER_EMPTY,
    ):
        assert stage.value in stages, f"{stage.value} never happened"

    held = [
        row
        for row in rig.authority.lifecycle.rows()
        if row["stage"] == LifecycleStage.W_HOLD_CONFIRMED.value
    ]
    assert held and float(held[-1]["held_ms"]) > 100.0, f"the hold was a tap: {held}"


def test_the_dashboard_reports_the_ledger_not_the_plan(rig: Rig) -> None:
    rig.chord(IntentType.START_LIVE)
    assert _await(lambda: rig.coordinator.mode is RunMode.LIVE)
    assert _await(rig.walked.is_set)

    actuator = rig.coordinator.actuator_state()

    assert actuator.forward_held, "the dashboard did not see the key the ledger holds"
    assert actuator.forward_held_ms > 0.0
    assert actuator.down_edges == 1
    assert actuator.hold_lapses == 0
    assert actuator.blocked_reason == ""

    rig.chord(IntentType.STOP)
    assert _await(lambda: rig.coordinator.mode is RunMode.IDLE)

    stopped = rig.coordinator.actuator_state()
    assert stopped.held == ()
    assert not stopped.forward_held
    assert stopped.forward_held_ms == 0.0


def test_losing_focus_mid_walk_releases_within_the_safety_deadline(rig: Rig) -> None:
    rig.chord(IntentType.START_LIVE)
    assert _await(lambda: rig.coordinator.mode is RunMode.LIVE)
    assert _await(rig.walked.is_set)

    rig.port.set_focus(False)
    started = time.monotonic()
    assert _await(rig.authority.ledger_empty, timeout_s=2.0), "focus loss never released"
    elapsed_ms = (time.monotonic() - started) * 1000.0

    budget_ms = (
        AuthorityConfig().stop_release_budget_ms + AuthorityConfig().safety_poll_interval_ms
    )
    assert elapsed_ms < budget_ms * 8, f"release took {elapsed_ms:.0f} ms"
    assert rig.edges(InputKey.W)[1] >= 1


def test_the_event_log_flood_cannot_erase_the_start_sequence(rig: Rig) -> None:
    """Per-frame chatter must never push the lifecycle out of the record."""
    rig.chord(IntentType.START_LIVE)
    assert _await(lambda: rig.coordinator.mode is RunMode.LIVE)

    # The real flood was one repeated sentence, 791 rows of it in an 800-row
    # ring. A *changed* status is never suppressed; a repeated one is.
    for _ in range(5000):
        rig.coordinator.events.add("worker.status", "FOLLOW: aligned within 1.2 degrees")

    milestones = [name for _at, name, _detail in rig.coordinator.events.milestones(2000)]
    assert "live.authorized" in milestones, "the flood erased the authorization"
    assert "mode" in milestones
    assert "worker.status" not in milestones
    assert rig.coordinator.events.suppressed > 0
