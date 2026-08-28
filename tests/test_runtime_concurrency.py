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
from tests.fakes import (
    FakeCaptureSource,
    FakeDeadmanClient,
    FakePlatformPort,
    VirtualClock,
    make_geometry,
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
# Live arming
# ---------------------------------------------------------------------------


def test_live_needs_a_token_and_the_token_is_one_use(harness: Harness) -> None:
    harness.register(IntentType.START_LIVE, "live", _cancellable_worker())
    harness.start()

    # No token: refused, and no worker ever starts.
    harness.submit(IntentType.START_LIVE, source="hotkey")
    time.sleep(0.1)
    assert harness.started == []

    harness.submit(IntentType.ARM_LIVE_FROM_UI, source="gui")
    assert harness.wait_for(lambda: harness.coordinator.arm_token() is not None)
    harness.submit(IntentType.START_LIVE, source="hotkey")
    assert harness.wait_for(lambda: harness.coordinator.mode is RunMode.LIVE)
    assert harness.coordinator.arm_token() is None

    # A second start with no fresh arm must be refused.
    started_count = len(harness.started)
    harness.submit(IntentType.START_LIVE, source="hotkey")
    time.sleep(0.1)
    assert len(harness.started) == started_count


def test_an_arm_token_expires(harness: Harness) -> None:
    harness.start()
    harness.submit(IntentType.ARM_LIVE_FROM_UI, source="gui")
    assert harness.wait_for(lambda: harness.coordinator.arm_token() is not None)

    time.sleep(0.5)  # arm_ttl_s is 0.4 in this harness

    assert harness.coordinator.arm_token() is None


def test_arming_is_refused_from_a_hotkey(harness: Harness) -> None:
    """Arming must be a physical UI click; a hotkey cannot stand in for it."""
    harness.start()
    harness.submit(IntentType.ARM_LIVE_FROM_UI, source="hotkey")
    time.sleep(0.1)
    assert harness.coordinator.arm_token() is None


def test_start_live_is_refused_from_the_gui(harness: Harness) -> None:
    """Clicking Tk removes Roblox focus, so a GUI Start Live is meaningless."""
    harness.register(IntentType.START_LIVE, "live", _cancellable_worker())
    harness.start()
    harness.submit(IntentType.ARM_LIVE_FROM_UI, source="gui")
    assert harness.wait_for(lambda: harness.coordinator.arm_token() is not None)

    harness.submit(IntentType.START_LIVE, source="gui")
    time.sleep(0.1)

    assert harness.started == []
    assert harness.coordinator.arm_token() is None  # the attempt still spends it


def test_failed_readiness_spends_the_token(harness: Harness) -> None:
    harness.register(IntentType.START_LIVE, "live", _cancellable_worker())
    harness.start()
    harness.submit(IntentType.ARM_LIVE_FROM_UI, source="gui")
    assert harness.wait_for(lambda: harness.coordinator.arm_token() is not None)
    harness.port.set_focus(False)  # readiness will now fail

    harness.submit(IntentType.START_LIVE, source="hotkey")
    time.sleep(0.15)

    assert harness.started == []
    assert harness.coordinator.arm_token() is None
    assert harness.coordinator.mode is not RunMode.LIVE


def test_an_unsafe_release_latch_blocks_live_but_not_shadow(harness: Harness) -> None:
    harness.register(IntentType.START_SHADOW, "shadow", _cancellable_worker())
    harness.register(IntentType.START_LIVE, "live", _cancellable_worker())
    harness.start()
    harness.port.fail("key_up")
    harness.authority.release_all("injected")
    harness.port.fail_ops.clear()
    assert harness.authority.release_uncertain

    harness.submit(IntentType.ARM_LIVE_FROM_UI, source="gui")
    assert harness.wait_for(lambda: harness.coordinator.arm_token() is not None)
    harness.submit(IntentType.START_LIVE, source="hotkey")
    time.sleep(0.15)
    assert "live" not in harness.started

    harness.submit(IntentType.START_SHADOW)
    assert harness.wait_for(lambda: harness.coordinator.mode is RunMode.SHADOW)


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

        rig.submit(IntentType.ARM_LIVE_FROM_UI, source="gui")
        assert rig.wait_for(lambda: rig.coordinator.arm_token() is not None)
        rig.submit(IntentType.START_LIVE, source="hotkey")
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
        (None, False),
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


def test_a_probe_that_raises_is_unsafe_rather_than_fatal(
    tmp_path: Any, monkeypatch: Any
) -> None:
    from prospector_engine.capture import CaptureService, EvidenceRegistry, ViewportGuard
    from prospector_engine.coordinator import RuntimeCoordinator
    from prospector_engine.input_authority import AuthorityConfig, HealthSources, InputAuthority
    from tests.fakes import FakeDeadmanClient, FakePlatformPort, VirtualClock, make_geometry

    monkeypatch.setenv("TREASURE_DATA_DIR", str(tmp_path / "data"))
    port = FakePlatformPort(VirtualClock(), geometry=make_geometry())
    guard = ViewportGuard(port)
    guard.connect()
    registry = EvidenceRegistry("cursor")
    capture = CaptureService(guard, registry)

    def boom() -> tuple[int, int] | None:
        raise OSError("scripted")

    coordinator = RuntimeCoordinator(
        authority=InputAuthority(
            port,
            deadman=FakeDeadmanClient(),
            health=HealthSources(
                focus=port.focus_state,
                client_rect=lambda: guard.geometry,
                capture_age_s=capture.latest_age_s,
            ),
            config=AuthorityConfig(),
            run_id="cursor",
        ),
        guard=guard,
        capture=capture,
        registry=registry,
        workers={},
        cursor_probe=boom,
    )

    assert coordinator._cursor_safe() is False
