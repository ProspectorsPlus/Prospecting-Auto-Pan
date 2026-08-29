"""Coordinator ownership, Stop responsiveness, arm-token lifecycle, shutdown.

These tests run on real time (not the virtual clock) because what they assert
is wall-clock responsiveness under injected stalls: a Stop that only works when
nothing else is blocked is not a Stop.
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
    Cancellation,
    IntentType,
    ModeResult,
    ModeResultKind,
    PerformanceTier,
    RunMode,
    RuntimeIntent,
    SafetyFault,
    SafetyFaultKind,
    WorkerCompletion,
    monotonic_s,
)
from prospector_engine.coordinator import (
    CoordinatorConfig,
    RuntimeCoordinator,
    WorkerContext,
)
from prospector_engine.input_authority import AuthorityConfig, HealthSources, InputAuthority
from prospector_engine.lifecycle import LifecycleStage
from tests.fakes import (
    FakeCaptureSource,
    FakeDeadmanClient,
    FakePlatformPort,
    VirtualClock,
    make_geometry,
    settle_cadence_for_live,
)


class Harness:
    """A fully wired coordinator over fakes, with scriptable workers."""

    def __init__(self) -> None:
        self.clock = VirtualClock()
        self.journal: list[str] = []
        self.port = FakePlatformPort(
            self.clock, geometry=make_geometry(size=(64.0, 48.0)), journal=self.journal
        )
        self.deadman = FakeDeadmanClient(journal=self.journal)
        self.guard = ViewportGuard(self.port, requested_client_logical=(64.0, 48.0))
        self.guard.adopt_current()
        self.source = FakeCaptureSource()
        self.registry = EvidenceRegistry("harness")
        self.capture = CaptureService(
            self.guard,
            self.registry,
            config=CaptureConfig(max_frame_age_ms=5000, start_tier=PerformanceTier.MINIMUM),
            source_factory=lambda: self.source,
        )
        self.authority = InputAuthority(
            self.port,
            deadman=self.deadman,
            health=HealthSources(
                focus=self.port.focus_state,
                client_rect=self.port.window_geometry,
                capture_age_s=self.capture.latest_age_s,
            ),
            config=AuthorityConfig(safety_poll_interval_ms=10),
        )
        self.registry = EvidenceRegistry(
            self.authority.run_id, on_token=self.authority.register_evidence
        )
        self.started: list[str] = []
        self.finished: list[str] = []
        self.workers: dict[IntentType, Any] = {}
        self.coordinator = RuntimeCoordinator(
            authority=self.authority,
            guard=self.guard,
            capture=self.capture,
            registry=self.registry,
            workers=self.workers,
            config=CoordinatorConfig(
                arm_ttl_s=0.4,
                worker_join_deadline_s=0.5,
                component_join_deadline_s=0.5,
                idle_poll_s=0.01,
            ),
        )

    def worker(self, name: str, body: Any) -> Any:
        def factory(context: WorkerContext) -> ModeResult:
            self.started.append(name)
            try:
                return body(context)
            finally:
                self.finished.append(name)

        return factory

    def register(self, key: IntentType, name: str, body: Any) -> None:
        self.coordinator.register_worker(key, self.worker(name, body))

    def submit(self, intent_type: IntentType, source: str = "gui") -> RuntimeIntent:
        intent = self.coordinator.next_intent(intent_type, source)
        self.coordinator.submit(intent)
        return intent

    def settle_cadence(self) -> None:
        """Satisfy the Live cadence gate through the production governor."""
        settle_cadence_for_live(self.capture)

    def chord(self, intent_type: IntentType) -> RuntimeIntent:
        """Submit through the physical-chord capability, as a listener does.

        Cadence is settled first, because a real machine has to satisfy that
        gate before a chord is accepted and a rig that skipped it would be
        testing a path production does not have.

        This is the *only* way a test may start Live, and it stands in for one
        real key edge. Nothing here pre-authorizes anything: the coordinator
        still runs every readiness check before it mints an authorization.
        """
        if intent_type is IntentType.START_LIVE:
            self.settle_cadence()
        intent = self.coordinator.chord_authority().intent(intent_type, "Ctrl+N")
        self.coordinator.submit(intent)
        return intent

    def wait_for(self, predicate: Any, timeout_s: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return False

    def start(self) -> None:
        self.guard.adopt_current()
        self.capture.start()
        self.coordinator.start()

    def close(self) -> dict[str, str]:
        return self.coordinator.shutdown()


@pytest.fixture
def harness() -> Any:
    rig = Harness()
    yield rig
    rig.close()


def _blocking_worker(release: threading.Event) -> Any:
    def body(context: WorkerContext) -> ModeResult:
        # Ignores cancellation on purpose: the coordinator must stay responsive
        # and bound its join anyway (plan 3.5).
        release.wait(5.0)
        return ModeResult(ModeResultKind.COMPLETED, "eventually")

    return body


def _cancellable_worker() -> Any:
    def body(context: WorkerContext) -> ModeResult:
        while not context.cancellation.wait(0.01):
            pass
        return ModeResult(ModeResultKind.CANCELLED, "cancelled")

    return body


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


def test_shadow_adopts_the_current_viewport_instead_of_moving_the_window(
    harness: Harness,
) -> None:
    """Observation must not require moving the user's Roblox window."""
    harness.register(IntentType.START_SHADOW, "shadow", _cancellable_worker())
    harness.guard.invalidate("test: nothing pinned")
    harness.start()

    harness.submit(IntentType.START_SHADOW)

    assert harness.wait_for(lambda: harness.coordinator.mode is RunMode.SHADOW)
    assert harness.port.pin_calls == 0
    assert harness.guard.geometry.valid


def test_exactly_one_mode_worker_runs_at_a_time(harness: Harness) -> None:
    harness.register(IntentType.START_SHADOW, "shadow", _cancellable_worker())
    harness.start()

    harness.submit(IntentType.START_SHADOW)
    assert harness.wait_for(lambda: harness.coordinator.mode is RunMode.SHADOW)
    first_generation = harness.coordinator.generation

    harness.submit(IntentType.START_SHADOW)
    assert harness.wait_for(lambda: harness.coordinator.generation > first_generation)

    # Two starts, two workers - but the first was cancelled and joined before
    # the second was allowed to begin.
    assert harness.started == ["shadow", "shadow"]
    assert harness.finished[0] == "shadow"
    assert harness.authority.ledger_empty()


def test_stop_is_responsive_while_a_worker_ignores_cancellation(harness: Harness) -> None:
    release = threading.Event()
    harness.register(IntentType.START_SHADOW, "stuck", _blocking_worker(release))
    harness.start()
    harness.submit(IntentType.START_SHADOW)
    assert harness.wait_for(lambda: harness.coordinator.mode is RunMode.SHADOW)

    started = time.monotonic()
    harness.submit(IntentType.STOP)
    assert harness.wait_for(lambda: harness.coordinator.mode is RunMode.IDLE, timeout_s=3.0)
    elapsed_ms = (time.monotonic() - started) * 1000.0

    assert elapsed_ms < 1500, f"stop took {elapsed_ms:.0f} ms with a wedged worker"
    assert harness.authority.ledger_empty()
    release.set()


def test_stop_is_responsive_while_capture_stalls(harness: Harness) -> None:
    harness.register(IntentType.START_SHADOW, "shadow", _cancellable_worker())
    harness.start()
    harness.submit(IntentType.START_SHADOW)
    assert harness.wait_for(lambda: harness.coordinator.mode is RunMode.SHADOW)

    stall = threading.Event()
    original = harness.source.poll

    def stalling_poll() -> Any:
        stall.wait(3.0)
        return original()

    harness.source.poll = stalling_poll  # type: ignore[method-assign]
    started = time.monotonic()
    harness.submit(IntentType.STOP)
    responsive = harness.wait_for(
        lambda: harness.coordinator.mode is RunMode.IDLE, timeout_s=2.0
    )
    elapsed_ms = (time.monotonic() - started) * 1000.0
    stall.set()

    assert responsive, "coordinator blocked behind a stalled capture backend"
    assert elapsed_ms < 1000


def test_stop_outranks_a_burst_of_ordinary_intents(harness: Harness) -> None:
    """STOP sits in its own priority band and is never coalesced (plan 3.2)."""
    coordinator = harness.coordinator
    for _ in range(50):
        coordinator.submit(coordinator.next_intent(IntentType.PIN_WINDOW, "gui"))
    stop = coordinator.next_intent(IntentType.STOP, "hotkey")
    coordinator.submit(stop)

    item = coordinator._take(0.1)
    assert item is not None
    assert isinstance(item.payload, RuntimeIntent)
    assert item.payload.intent_type is IntentType.STOP


def test_duplicate_ordinary_intents_coalesce_but_stops_do_not(harness: Harness) -> None:
    coordinator = harness.coordinator
    first = coordinator.submit(coordinator.next_intent(IntentType.PIN_WINDOW, "gui"))
    second = coordinator.submit(coordinator.next_intent(IntentType.PIN_WINDOW, "gui"))
    assert first is True and second is False

    assert coordinator.submit(coordinator.next_intent(IntentType.STOP, "gui")) is True
    assert coordinator.submit(coordinator.next_intent(IntentType.STOP, "gui")) is True


# ---------------------------------------------------------------------------
# Stale completions and faults
# ---------------------------------------------------------------------------


def test_a_late_completion_from_an_old_generation_cannot_transition(harness: Harness) -> None:
    harness.register(IntentType.START_SHADOW, "shadow", _cancellable_worker())
    harness.start()
    harness.submit(IntentType.START_SHADOW)
    assert harness.wait_for(lambda: harness.coordinator.mode is RunMode.SHADOW)
    generation = harness.coordinator.generation

    harness.coordinator._submit_completion(
        WorkerCompletion(
            generation=generation - 1,
            mode=RunMode.SHADOW,
            worker_id="ghost",
            result=ModeResult(ModeResultKind.FAILED, "should be ignored"),
        )
    )
    assert harness.wait_for(lambda: harness.coordinator.stale_completions >= 1)
    assert harness.coordinator.mode is RunMode.SHADOW


def test_a_stale_generation_fault_cannot_perturb_a_newer_mode(harness: Harness) -> None:
    harness.register(IntentType.START_SHADOW, "shadow", _cancellable_worker())
    harness.start()
    harness.submit(IntentType.START_SHADOW)
    assert harness.wait_for(lambda: harness.coordinator.mode is RunMode.SHADOW)
    generation = harness.coordinator.generation

    harness.coordinator.submit_fault(
        SafetyFault(generation - 1, SafetyFaultKind.FOCUS_LOST, ("stale",), monotonic_s())
    )
    time.sleep(0.15)
    assert harness.coordinator.mode is RunMode.SHADOW


def test_shadow_survives_focus_loss_but_a_viewport_fault_stops_it(harness: Harness) -> None:
    """Shadow must keep running while the user looks at Tk (plan 7.3)."""
    harness.register(IntentType.START_SHADOW, "shadow", _cancellable_worker())
    harness.start()
    harness.submit(IntentType.START_SHADOW)
    assert harness.wait_for(lambda: harness.coordinator.mode is RunMode.SHADOW)

    harness.coordinator.submit_fault(
        SafetyFault(
            harness.coordinator.generation, SafetyFaultKind.FOCUS_LOST, ("tk",), monotonic_s()
        )
    )
    time.sleep(0.15)
    assert harness.coordinator.mode is RunMode.SHADOW

    harness.coordinator.submit_fault(
        SafetyFault(
            harness.coordinator.generation,
            SafetyFaultKind.VIEWPORT_INVALID,
            ("resized",),
            monotonic_s(),
        )
    )
    assert harness.wait_for(lambda: harness.coordinator.mode is RunMode.IDLE)


# ---------------------------------------------------------------------------
# Starting Live: one gesture, and it is the physical chord
# ---------------------------------------------------------------------------


def test_a_genuine_chord_enters_live_with_no_separate_arm_click(harness: Harness) -> None:
    """The regression. One physical Ctrl+N, and the character may move.

    Reproduces the reported failure exactly: a ready runtime, a genuine chord,
    and - before D-062 - ``live.refused: no arm token`` because a second
    gesture nobody had been told about was never made.
    """
    harness.register(IntentType.START_LIVE, "live", _cancellable_worker())
    harness.start()

    harness.chord(IntentType.START_LIVE)

    assert harness.wait_for(lambda: harness.coordinator.mode is RunMode.LIVE)
    assert harness.started == ["live"]
    assert harness.coordinator.live_authorization.startswith("granted")
    assert "no arm token" not in " ".join(_event_details(harness))


def test_a_ready_chord_never_ends_in_no_arm_token(harness: Harness) -> None:
    """Whatever else can refuse a chord, the absent second gesture cannot."""
    harness.register(IntentType.START_LIVE, "live", _cancellable_worker())
    harness.start()

    harness.chord(IntentType.START_LIVE)
    assert harness.wait_for(lambda: harness.coordinator.mode is RunMode.LIVE)

    refusals = [d for n, d in _events(harness) if n == "live.refused"]
    assert refusals == []


def test_the_authorization_is_created_and_consumed_in_one_transaction(
    harness: Harness,
) -> None:
    from prospector_engine.lifecycle import LIVE_ENTRY_PATH

    harness.register(IntentType.START_LIVE, "live", _cancellable_worker())
    harness.start()
    harness.chord(IntentType.START_LIVE)
    assert harness.wait_for(lambda: harness.coordinator.mode is RunMode.LIVE)

    stages = [row["stage"] for row in harness.authority.lifecycle.rows()]
    # CHORD_RECOGNIZED is the listener's own note and LIVE_WORKER_ENTERED is
    # the real live worker's; this harness has neither. Everything between
    # them belongs to the coordinator, and all of it must have happened.
    outside = {LifecycleStage.CHORD_RECOGNIZED, LifecycleStage.LIVE_WORKER_ENTERED}
    for stage in LIVE_ENTRY_PATH:
        if stage in outside:
            continue
        assert stage.value in stages, f"{stage.value} never happened"
    created = stages.index(LifecycleStage.LIVE_AUTHORIZATION_CREATED.value)
    consumed = stages.index(LifecycleStage.LIVE_AUTHORIZATION_CONSUMED.value)
    entered = stages.index(LifecycleStage.LIVE_ENTERED.value)
    assert created < consumed < entered
    # And nothing survives the transaction that made it.
    assert harness.coordinator.live_authorization.startswith("granted")


def test_a_fabricated_hotkey_intent_cannot_start_live(harness: Harness) -> None:
    """``source="hotkey"`` is a label anything can write. The proof is not."""
    harness.register(IntentType.START_LIVE, "live", _cancellable_worker())
    harness.start()

    harness.submit(IntentType.START_LIVE, source="hotkey")
    time.sleep(0.15)

    assert harness.started == []
    assert harness.coordinator.mode is not RunMode.LIVE
    assert "not the physical chord" in harness.coordinator.live_authorization


def test_a_gui_start_live_is_refused(harness: Harness) -> None:
    """Clicking Tk removes Roblox focus, so a GUI Start Live is meaningless."""
    harness.register(IntentType.START_LIVE, "live", _cancellable_worker())
    harness.start()

    harness.submit(IntentType.START_LIVE, source="gui")
    time.sleep(0.15)

    assert harness.started == []
    assert harness.coordinator.mode is not RunMode.LIVE


def test_a_proof_from_another_run_is_refused(harness: Harness) -> None:
    """A nonce is per run. One carried across cannot authorize this one."""
    from dataclasses import replace as _replace

    from prospector_engine.contracts import PhysicalChordProof

    harness.register(IntentType.START_LIVE, "live", _cancellable_worker())
    harness.start()

    forged = _replace(
        harness.coordinator.next_intent(IntentType.START_LIVE, "hotkey"),
        proof=PhysicalChordProof(nonce="not-this-run", chord="Ctrl+N", minted_at_s=0.0),
    )
    harness.coordinator.submit(forged)
    time.sleep(0.15)

    assert harness.started == []
    assert harness.coordinator.mode is not RunMode.LIVE


def test_a_refused_chord_says_exactly_why_and_never_silently_does_nothing(
    harness: Harness,
) -> None:
    harness.register(IntentType.START_LIVE, "live", _cancellable_worker())
    harness.start()
    harness.port.set_focus(False)

    harness.chord(IntentType.START_LIVE)
    assert harness.wait_for(
        lambda: harness.coordinator.live_authorization.startswith("refused")
    )

    assert harness.started == []
    detail = harness.coordinator.live_refusal_detail
    assert "frontmost" in detail, detail
    assert "again" in detail, "a transient refusal must say it is worth retrying"
    stages = [row["stage"] for row in harness.authority.lifecycle.rows()]
    assert LifecycleStage.LIVE_REFUSED.value in stages
    assert LifecycleStage.LIVE_ENTERED.value not in stages

    # ...and the same chord works once the transient condition clears.
    harness.port.set_focus(True)
    harness.chord(IntentType.START_LIVE)
    assert harness.wait_for(lambda: "live" in harness.started)


def test_a_real_readiness_fault_refuses_and_does_not_invite_a_retry(
    harness: Harness,
) -> None:
    """An unconfirmed release is not something a second press should retry into."""
    harness.register(IntentType.START_LIVE, "live", _cancellable_worker())
    harness.start()
    harness.port.fail("key_up")
    harness.authority.release_all("injected")
    harness.port.fail_ops.clear()
    assert harness.authority.release_uncertain

    harness.chord(IntentType.START_LIVE)
    assert harness.wait_for(
        lambda: harness.coordinator.live_authorization.startswith("refused")
    )

    assert harness.started == []
    assert harness.coordinator.mode is not RunMode.LIVE
    assert "again" not in harness.coordinator.live_refusal_detail


def test_an_unsafe_release_latch_blocks_live_but_not_shadow(harness: Harness) -> None:
    harness.register(IntentType.START_SHADOW, "shadow", _cancellable_worker())
    harness.register(IntentType.START_LIVE, "live", _cancellable_worker())
    harness.start()
    harness.port.fail("key_up")
    harness.authority.release_all("injected")
    harness.port.fail_ops.clear()
    assert harness.authority.release_uncertain

    harness.chord(IntentType.START_LIVE)
    time.sleep(0.15)
    assert "live" not in harness.started

    harness.submit(IntentType.START_SHADOW)
    assert harness.wait_for(lambda: harness.coordinator.mode is RunMode.SHADOW)


def test_a_second_chord_while_live_is_refused_rather_than_restarting(
    harness: Harness,
) -> None:
    harness.register(IntentType.START_LIVE, "live", _cancellable_worker())
    harness.start()
    harness.chord(IntentType.START_LIVE)
    assert harness.wait_for(lambda: harness.coordinator.mode is RunMode.LIVE)

    harness.chord(IntentType.START_LIVE)
    time.sleep(0.15)

    assert harness.started == ["live"], "the chord restarted a running session"


def test_stop_always_releases_from_every_mode(harness: Harness) -> None:
    """Ctrl+X is unconditional. Not "usually", and not "once Live has started".

    A stop that only works from the state you expected to be in is useless
    exactly when it matters, so this walks the states a session actually passes
    through - including a refusal, which is a state the old contract could sit
    in indefinitely.
    """
    harness.register(IntentType.START_SHADOW, "shadow", _cancellable_worker())
    harness.register(IntentType.START_LIVE, "live", _cancellable_worker())
    harness.start()

    # From IDLE, having done nothing at all.
    harness.chord(IntentType.STOP)
    assert harness.wait_for(lambda: harness.coordinator.stopped_by_user)
    assert harness.authority.ledger_empty()

    # From SHADOW.
    harness.submit(IntentType.START_SHADOW)
    assert harness.wait_for(lambda: harness.coordinator.mode is RunMode.SHADOW)
    harness.chord(IntentType.STOP)
    assert harness.wait_for(lambda: harness.coordinator.mode is RunMode.IDLE)
    assert harness.authority.ledger_empty()

    # From a refusal - the state the old contract could sit in forever.
    harness.port.set_focus(False)
    harness.chord(IntentType.START_LIVE)
    assert harness.wait_for(
        lambda: harness.coordinator.live_authorization.startswith("refused")
    )
    harness.chord(IntentType.STOP)
    # The ledger was already empty, so waiting on *that* would pass before the
    # stop was even handled. Wait for the thing the stop actually changes.
    assert harness.wait_for(lambda: harness.coordinator.live_authorization == "none")
    assert harness.authority.ledger_empty()

    # From LIVE.
    harness.port.set_focus(True)
    harness.chord(IntentType.START_LIVE)
    assert harness.wait_for(lambda: harness.coordinator.mode is RunMode.LIVE)
    harness.chord(IntentType.STOP)
    assert harness.wait_for(lambda: harness.coordinator.mode is RunMode.IDLE)
    assert harness.authority.ledger_empty()
    assert harness.coordinator.stopped_by_user
    assert not harness.authority.release_uncertain


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


def test_shutdown_is_bounded_with_a_permanently_stalled_worker() -> None:
    rig = Harness()
    release = threading.Event()
    rig.register(IntentType.START_SHADOW, "stuck", _blocking_worker(release))
    rig.start()
    rig.submit(IntentType.START_SHADOW)
    assert rig.wait_for(lambda: rig.coordinator.mode is RunMode.SHADOW)

    started = time.monotonic()
    report = rig.close()
    elapsed_s = time.monotonic() - started
    release.set()

    assert elapsed_s < 5.0, f"shutdown took {elapsed_s:.1f}s"
    assert set(report) >= {"release", "worker", "capture", "watchdog"}
    assert rig.authority.ledger_empty()


def test_shutdown_records_a_survivor_rather_than_waiting_forever() -> None:
    rig = Harness()
    release = threading.Event()
    rig.register(IntentType.START_SHADOW, "stuck", _blocking_worker(release))
    rig.start()
    rig.submit(IntentType.START_SHADOW)
    assert rig.wait_for(lambda: rig.coordinator.mode is RunMode.SHADOW)

    report = rig.close()
    release.set()

    assert report["worker"] == "survivor"
    assert report["capture"] == "stopped"


def test_an_uncaught_worker_error_safe_stops_instead_of_escaping(harness: Harness) -> None:
    def exploding(context: WorkerContext) -> ModeResult:
        raise RuntimeError("boom")

    harness.register(IntentType.START_SHADOW, "boom", exploding)
    harness.start()
    harness.submit(IntentType.START_SHADOW)

    assert harness.wait_for(lambda: harness.coordinator.last_result is not None)
    assert harness.coordinator.last_result is not None
    assert "boom" in harness.coordinator.last_result.detail
    assert harness.authority.ledger_empty()


def test_cancellation_is_cooperative_and_immediate() -> None:
    cancellation = Cancellation()
    assert cancellation.wait(0.01) is False
    cancellation.cancel()
    started = time.monotonic()
    assert cancellation.wait(5.0) is True
    assert time.monotonic() - started < 0.5


# ---------------------------------------------------------------------------
# Unsafe-release recovery across launches
# ---------------------------------------------------------------------------


def test_a_recovery_record_from_a_previous_run_blocks_live_at_startup(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Plan 4.4: a run that could not confirm its release still blocks the next one."""
    from prospector_engine.telemetry import (
        read_recovery_record,
        resolve_app_paths,
        write_recovery_record,
    )

    monkeypatch.setenv("TREASURE_DATA_DIR", str(tmp_path / "data"))
    paths = resolve_app_paths().ensure()
    write_recovery_record(paths, "previous run left a key held", {"failures": ["w"]})

    rig = Harness()
    rig.coordinator._paths = paths
    rig.register(IntentType.START_LIVE, "live", _cancellable_worker())
    rig.start()
    try:
        assert rig.authority.release_uncertain

        rig.chord(IntentType.START_LIVE)
        time.sleep(0.15)
        assert rig.started == []

        rig.submit(IntentType.RECOVER_RELEASE, source="gui")
        assert rig.wait_for(lambda: not rig.authority.release_uncertain, timeout_s=2.0)
        assert read_recovery_record(paths) is None
    finally:
        rig.close()


def test_a_failed_recovery_handshake_rewrites_the_record(
    tmp_path: Any, monkeypatch: Any
) -> None:
    from prospector_engine.telemetry import read_recovery_record, resolve_app_paths

    monkeypatch.setenv("TREASURE_DATA_DIR", str(tmp_path / "data"))
    paths = resolve_app_paths().ensure()

    rig = Harness()
    rig.coordinator._paths = paths
    rig.start()
    try:
        rig.port.fail("key_up")
        rig.authority.release_all("injected")
        assert rig.authority.release_uncertain

        rig.submit(IntentType.RECOVER_RELEASE, source="gui")
        assert rig.wait_for(lambda: read_recovery_record(paths) is not None, timeout_s=2.0)

        record = read_recovery_record(paths)
        assert record is not None
        assert "recovery handshake failed" in record["reason"]
        assert rig.authority.release_uncertain
    finally:
        rig.port.fail_ops.clear()
        rig.close()


def test_a_stop_that_cannot_confirm_release_writes_a_record(
    tmp_path: Any, monkeypatch: Any
) -> None:
    from prospector_engine.telemetry import read_recovery_record, resolve_app_paths

    monkeypatch.setenv("TREASURE_DATA_DIR", str(tmp_path / "data"))
    paths = resolve_app_paths().ensure()

    rig = Harness()
    rig.coordinator._paths = paths
    rig.register(IntentType.START_SHADOW, "shadow", _cancellable_worker())
    rig.start()
    try:
        rig.submit(IntentType.START_SHADOW)
        assert rig.wait_for(lambda: rig.coordinator.mode is RunMode.SHADOW)
        rig.port.fail("key_up")

        rig.submit(IntentType.STOP)
        assert rig.wait_for(lambda: read_recovery_record(paths) is not None, timeout_s=2.0)
    finally:
        rig.port.fail_ops.clear()
        rig.close()


# ---------------------------------------------------------------------------
# The pointer probe is read-only, and unknown means unsafe
# ---------------------------------------------------------------------------


def test_the_cursor_probe_is_a_callable_not_a_port() -> None:
    """The coordinator must never hold something that can emit input."""
    import inspect

    from prospector_engine.coordinator import RuntimeCoordinator

    signature = inspect.signature(RuntimeCoordinator.__init__)
    assert "cursor_probe" in signature.parameters
    assert "port" not in signature.parameters
    source = inspect.getsource(RuntimeCoordinator._cursor_safe)
    assert "raw_" not in source and "acquire" not in source


@pytest.mark.parametrize(
    ("point", "expected"),
    [
        ((640, 360), True),
        ((200, 360), True),
        ((10, 360), False),
        ((640, 10), False),
        # Unknown is *not* unsafe. ``cursor_client_px`` returns None both for a
        # failed read and for a pointer outside the client rect, and outside
        # the client rect is the normal case: the user clicked Start Navigator
        # on the dashboard and left the pointer there. Treating that as unsafe
        # released on every frame of a mouse-yaw run (D-067).
        (None, True),
    ],
)
def test_the_safe_region_is_the_middle_of_the_client(
    point: Any, expected: bool, tmp_path: Any, monkeypatch: Any
) -> None:
    from prospector_engine.capture import (
        CaptureConfig,
        CaptureService,
        EvidenceRegistry,
        ViewportGuard,
    )
    from prospector_engine.coordinator import CoordinatorConfig, RuntimeCoordinator
    from prospector_engine.input_authority import AuthorityConfig, HealthSources, InputAuthority
    from tests.fakes import FakeDeadmanClient, FakePlatformPort, VirtualClock, make_geometry

    monkeypatch.setenv("TREASURE_DATA_DIR", str(tmp_path / "data"))
    port = FakePlatformPort(VirtualClock(), geometry=make_geometry())
    guard = ViewportGuard(port)
    guard.connect()
    registry = EvidenceRegistry("cursor")
    capture = CaptureService(guard, registry, config=CaptureConfig())
    authority = InputAuthority(
        port,
        deadman=FakeDeadmanClient(),
        health=HealthSources(
            focus=port.focus_state,
            client_rect=lambda: guard.geometry,
            capture_age_s=capture.latest_age_s,
        ),
        config=AuthorityConfig(),
        run_id="cursor",
    )
    coordinator = RuntimeCoordinator(
        authority=authority,
        guard=guard,
        capture=capture,
        registry=registry,
        workers={},
        config=CoordinatorConfig(),
        cursor_probe=lambda: point,
    )

    assert coordinator._cursor_safe() is expected


def test_a_probe_that_raises_is_not_treated_as_unsafe(tmp_path: Any, monkeypatch: Any) -> None:
    """A window-list read that fails must not become a macro that cannot move.

    It must also not crash the coordinator, which is the half of this that has
    not changed.
    """

    def angry() -> Any:
        raise OSError("CGWindowList failed")

    from prospector_engine.capture import (
        CaptureConfig,
        CaptureService,
        EvidenceRegistry,
        ViewportGuard,
    )
    from prospector_engine.coordinator import CoordinatorConfig, RuntimeCoordinator
    from prospector_engine.input_authority import AuthorityConfig, HealthSources, InputAuthority
    from tests.fakes import FakeDeadmanClient, FakePlatformPort, VirtualClock, make_geometry

    monkeypatch.setenv("TREASURE_DATA_DIR", str(tmp_path / "data"))
    port = FakePlatformPort(VirtualClock(), geometry=make_geometry())
    guard = ViewportGuard(port)
    guard.connect()
    registry = EvidenceRegistry("cursor")
    capture = CaptureService(guard, registry, config=CaptureConfig())
    authority = InputAuthority(
        port,
        deadman=FakeDeadmanClient(),
        health=HealthSources(
            focus=port.focus_state,
            client_rect=lambda: guard.geometry,
            capture_age_s=capture.latest_age_s,
        ),
        config=AuthorityConfig(),
        run_id="cursor",
    )
    coordinator = RuntimeCoordinator(
        authority=authority,
        guard=guard,
        capture=capture,
        registry=registry,
        workers={},
        config=CoordinatorConfig(),
        cursor_probe=angry,
    )

    assert coordinator._cursor_safe() is True


def _events(harness: Harness) -> list[tuple[str, str]]:
    return [(name, detail) for _at, name, detail in harness.coordinator.events.verbatim(500)]


def _event_details(harness: Harness) -> list[str]:
    return [detail for _name, detail in _events(harness)]


def test_a_chord_is_refused_while_the_cadence_is_not_live_eligible(
    harness: Harness,
) -> None:
    """The window and this method must not disagree about the same blocker.

    The dashboard showed **BLOCKED - CADENCE** while ``_on_start_live`` checked
    only ``Readiness``, which has no cadence term - so the chord would have
    started Live against a pipeline the window had just said was not ready.
    That is the D-062 disagreement running the other way.
    """
    harness.register(IntentType.START_LIVE, "live", _cancellable_worker())
    harness.start()

    # Deliberately *not* settled: this is the state a real machine is in for
    # the first seconds after Start Navigator.
    assert not harness.capture.metrics().live_eligible
    intent = harness.coordinator.chord_authority().intent(IntentType.START_LIVE, "Ctrl+N")
    harness.coordinator.submit(intent)
    assert harness.wait_for(
        lambda: harness.coordinator.live_authorization.startswith("refused")
    )

    assert harness.started == []
    assert "cadence" in harness.coordinator.live_authorization
    assert "observe" in harness.coordinator.live_refusal_detail

    # ...and once the cadence settles, the same chord works.
    harness.chord(IntentType.START_LIVE)
    assert harness.wait_for(lambda: harness.coordinator.mode is RunMode.LIVE)


def test_the_window_and_the_coordinator_agree_about_cadence(harness: Harness) -> None:
    """Whatever the dashboard calls blocking, the chord must refuse - and vice versa."""
    harness.register(IntentType.START_LIVE, "live", _cancellable_worker())
    harness.start()

    def cadence_blocking() -> bool:
        return any(
            b.code == "CADENCE" and b.status == "blocking"
            for b in harness.coordinator.blockers()
        )

    assert cadence_blocking(), "the window would show BLOCKED here"
    harness.settle_cadence()
    assert not cadence_blocking(), "the window would still show BLOCKED here"

    harness.chord(IntentType.START_LIVE)
    assert harness.wait_for(lambda: harness.coordinator.mode is RunMode.LIVE)
