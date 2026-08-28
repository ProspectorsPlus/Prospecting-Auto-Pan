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
    FakeCaptureBackend,
    FakeDeadmanClient,
    FakePlatformPort,
    VirtualClock,
    make_rect,
)


class Harness:
    """A fully wired coordinator over fakes, with scriptable workers."""

    def __init__(self) -> None:
        self.clock = VirtualClock()
        self.journal: list[str] = []
        self.port = FakePlatformPort(
            self.clock, rect=make_rect(size_px=(64, 48)), journal=self.journal
        )
        self.deadman = FakeDeadmanClient(journal=self.journal)
        self.guard = ViewportGuard(self.port, requested_size_px=(64, 48))
        self.guard.adopt_current()
        self.backend = FakeCaptureBackend()
        self.registry = EvidenceRegistry("harness")
        self.capture = CaptureService(
            self.guard,
            self.registry,
            config=CaptureConfig(target_interval_ms=5, max_frame_age_ms=5000),
            backend_factory=lambda: self.backend,
        )
        self.capture.capture_once()
        self.authority = InputAuthority(
            self.port,
            deadman=self.deadman,
            health=HealthSources(
                focus=self.port.focus_state,
                client_rect=self.port.find_client_rect,
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
    assert harness.guard.pinned is not None


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
    original = harness.backend.grab_client

    def stalling_grab(rect: Any) -> Any:
        stall.wait(3.0)
        return original(rect)

    harness.backend.grab_client = stalling_grab  # type: ignore[method-assign]
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
