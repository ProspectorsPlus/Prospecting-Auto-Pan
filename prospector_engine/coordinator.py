"""The runtime coordinator: sole owner of mode, generation, and the one worker.

The GUI thread and the hotkey thread submit :class:`RuntimeIntent` objects and
nothing else. This thread is the only place that changes ``RunMode``, activates
or invalidates an authority generation, or starts a mode worker (bug B7).

It never runs navigation, dig, reset, pan-swap, or next-map logic inline: those
are cancellable workers, so the event loop stays responsive even when capture
or a native call stalls (bugs B3, B6).
"""

from __future__ import annotations

import contextlib
import heapq
import itertools
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from prospector_engine.capture import CaptureService, EvidenceRegistry, ViewportGuard
from prospector_engine.contracts import (
    Cancellation,
    DiagnosticObservation,
    EvidenceStatus,
    IntentType,
    ModeResult,
    ModeResultKind,
    NavigationPhase,
    Provenance,
    RunMode,
    RuntimeIntent,
    SafetyFault,
    SafetyFaultKind,
    TelemetrySnapshot,
    WorkerCompletion,
    monotonic_s,
)
from prospector_engine.geometry import ViewportState
from prospector_engine.input_authority import (
    InputAuthority,
    NavigationInputSession,
    NoInputSession,
    ServiceInputSession,
)
from prospector_engine.telemetry import (
    AppPaths,
    EventLog,
    LatestSlot,
    TelemetryHub,
    clear_recovery_record,
    read_recovery_record,
    write_recovery_record,
)

__all__ = [
    "CoordinatorConfig",
    "LiveArmToken",
    "Readiness",
    "RuntimeCoordinator",
    "WorkerContext",
    "WorkerFactory",
]


@dataclass(frozen=True)
class CoordinatorConfig:
    arm_ttl_s: float = 30.0
    worker_join_deadline_s: float = 1.0
    component_join_deadline_s: float = 2.0
    idle_poll_s: float = 0.05
    service_deadline_s: float = 60.0
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="TREASURE_NAVIGATION_PLAN.md sections 3.3, 3.4, 3.5",
            note="30 s arm TTL and bounded joins; stop-latency gate is PENDING",
        )
    )


@dataclass(frozen=True)
class LiveArmToken:
    """One-use, process-run-bound proof that a human physically clicked Arm Live.

    It is never persisted, never renewed silently, and is consumed *before* the
    normal transition invalidation so a failed readiness check spends it and
    forces a fresh physical arm (plan 3.4).
    """

    token_id: str
    run_id: str
    generation: int
    created_at_s: float
    expires_at_s: float

    def remaining_s(self, now_s: float) -> float:
        return max(0.0, self.expires_at_s - now_s)

    def expired(self, now_s: float) -> bool:
        return now_s >= self.expires_at_s


@dataclass(frozen=True)
class _ConsumedArmProof:
    token_id: str
    intent_sequence: int


@dataclass(frozen=True)
class Readiness:
    """Why a mode can or cannot start right now.

    ``viewport_state`` is the single authority the detectors, the GUI, and Live
    gating all read, which is what stops "viewport ok" from ever coexisting
    with "unsupported viewport size": a client that is not canonical says so in
    the state, and both consumers see the same value.
    """

    viewport_ok: bool
    viewport_state: ViewportState
    focus_ok: bool
    capture_fresh: bool
    watchdog_ok: bool
    deadman_ok: bool
    ledger_empty: bool
    release_known_safe: bool
    reasons: tuple[str, ...] = ()

    @property
    def shadow_ok(self) -> bool:
        """Shadow needs a valid capture viewport and nothing else (plan 3.3)."""
        return self.viewport_ok

    @property
    def input_ok(self) -> bool:
        return all(
            (
                self.viewport_ok,
                self.focus_ok,
                self.capture_fresh,
                self.watchdog_ok,
                self.deadman_ok,
                self.ledger_empty,
                self.release_known_safe,
            )
        )

    @property
    def calibrated_pixels_apply(self) -> bool:
        return self.viewport_state.supports_calibrated_pixels

    def as_map(self) -> dict[str, str]:
        return {
            "viewport": self.viewport_state.value.replace("_", " "),
            "focus": "ok" if self.focus_ok else "not-focused",
            "capture": "fresh" if self.capture_fresh else "stale",
            "watchdog": "ok" if self.watchdog_ok else "stopped",
            "deadman": "ok" if self.deadman_ok else "unhealthy",
            "ledger": "empty" if self.ledger_empty else "held",
            "release": "known-safe" if self.release_known_safe else "uncertain",
        }


@dataclass
class WorkerContext:
    """The narrow world a mode worker sees. No port, no ledger, no coordinator."""

    generation: int
    mode: RunMode
    worker_id: str
    cancellation: Cancellation
    frames: CaptureService
    observer: NoInputSession | None = None
    navigation: NavigationInputSession | None = None
    service: ServiceInputSession | None = None
    intent: RuntimeIntent | None = None
    #: The live perception pipeline, so a profile change in the UI reaches the
    #: running worker instead of only affecting the next one.
    pipeline: Any = None
    on_status: Callable[[str], None] = lambda _message: None
    on_phase: Callable[[NavigationPhase | None], None] = lambda _phase: None
    on_observation: Callable[[DiagnosticObservation], None] = lambda _observation: None


#: A mode worker is any callable that takes the narrow context and returns a
#: typed result. It is a plain alias rather than a Protocol so ordinary
#: closures satisfy it without matching a parameter name.
WorkerFactory = Callable[[WorkerContext], ModeResult]


@dataclass(order=True)
class _QueueItem:
    priority: int
    order: int
    payload: Any = field(compare=False)


class RuntimeCoordinator:
    """Priority intent loop, mode ownership, and bounded shutdown."""

    #: Priority bands (plan 3.2). Safety faults sit between stop and ordinary
    #: work so they can never be starved by a burst of GUI clicks.
    PRIORITY_STOP = 0
    PRIORITY_SAFETY = 1
    PRIORITY_ORDINARY = 2

    def __init__(
        self,
        *,
        authority: InputAuthority,
        guard: ViewportGuard,
        capture: CaptureService,
        registry: EvidenceRegistry,
        workers: dict[IntentType, WorkerFactory],
        config: CoordinatorConfig | None = None,
        hub: TelemetryHub | None = None,
        events: EventLog | None = None,
        paths: AppPaths | None = None,
        pipeline_provider: Callable[[], Any] | None = None,
    ) -> None:
        self._authority = authority
        self._guard = guard
        self._capture = capture
        self._registry = registry
        self._workers = dict(workers)
        self._config = config or CoordinatorConfig()
        self._hub = hub or TelemetryHub()
        self._events = events or EventLog()
        self._paths = paths
        self._pipeline_provider = pipeline_provider
        # A dedicated channel, deliberately not the emit-on-change hub: a
        # per-frame observation must publish for every processed frame, and
        # change-suppression would silently drop identical consecutive ones
        # (mission section 7).
        self._observations: LatestSlot[DiagnosticObservation] = LatestSlot()
        self._observation_count = 0

        self._queue: list[_QueueItem] = []
        self._queue_lock = threading.Condition()
        self._order = itertools.count()
        self._pending_ordinary: set[tuple[IntentType, str]] = set()
        self._intent_sequence = itertools.count(1)

        self._mode = RunMode.IDLE
        self._generation = 0
        self._phase: NavigationPhase | None = None
        self._arm_token: LiveArmToken | None = None
        self._worker: threading.Thread | None = None
        self._worker_cancel: Cancellation | None = None
        self._worker_id: str | None = None
        self._last_result: ModeResult | None = None
        self._last_stop_reason = ""
        self._stale_completions = 0

        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._shutdown_complete = threading.Event()

    def register_worker(self, key: IntentType, factory: WorkerFactory) -> None:
        """Install or replace the worker for one intent.

        The constructor copies its ``workers`` mapping, so late registration
        goes through here rather than by mutating the caller's dict.
        """
        self._workers[key] = factory

    # -- introspection ----------------------------------------------------
    @property
    def mode(self) -> RunMode:
        return self._mode

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def phase(self) -> NavigationPhase | None:
        return self._phase

    @property
    def hub(self) -> TelemetryHub:
        return self._hub

    @property
    def observations(self) -> LatestSlot[DiagnosticObservation]:
        """Latest per-frame diagnostic. Capacity one, drop-oldest, never suppressed."""
        return self._observations

    @property
    def observation_count(self) -> int:
        return self._observation_count

    def _publish_observation(self, observation: DiagnosticObservation) -> None:
        self._observation_count += 1
        self._observations.publish(observation)

    @property
    def events(self) -> EventLog:
        return self._events

    @property
    def last_result(self) -> ModeResult | None:
        return self._last_result

    @property
    def stale_completions(self) -> int:
        return self._stale_completions

    def arm_token(self) -> LiveArmToken | None:
        token = self._arm_token
        if token is not None and token.expired(monotonic_s()):
            self._arm_token = None
            self._events.add("arm.expired", token.token_id)
            return None
        return token

    # -- submission -------------------------------------------------------
    def next_intent(self, intent_type: IntentType, source: str) -> RuntimeIntent:
        return RuntimeIntent(
            sequence=next(self._intent_sequence),
            intent_type=intent_type,
            source=source,  # type: ignore[arg-type]
            created_at_s=monotonic_s(),
        )

    def submit(self, intent: RuntimeIntent) -> bool:
        """Enqueue an intent. Ordinary duplicates coalesce; STOP never does."""
        priority = (
            self.PRIORITY_STOP if intent.intent_type.priority == 0 else self.PRIORITY_ORDINARY
        )
        key = intent.coalescing_key()
        with self._queue_lock:
            if key is not None:
                if key in self._pending_ordinary:
                    return False
                self._pending_ordinary.add(key)
            heapq.heappush(self._queue, _QueueItem(priority, next(self._order), intent))
            self._queue_lock.notify()
        return True

    def submit_fault(self, fault: SafetyFault) -> None:
        """Safety faults are never dropped and never coalesced."""
        with self._queue_lock:
            heapq.heappush(
                self._queue, _QueueItem(self.PRIORITY_SAFETY, next(self._order), fault)
            )
            self._queue_lock.notify()

    def _submit_completion(self, completion: WorkerCompletion) -> None:
        with self._queue_lock:
            heapq.heappush(
                self._queue, _QueueItem(self.PRIORITY_ORDINARY, next(self._order), completion)
            )
            self._queue_lock.notify()

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._adopt_previous_recovery_record()
        self._running.set()
        self._authority.start_watchdog()
        self._thread = threading.Thread(
            target=self._loop, name="treasure-coordinator", daemon=True
        )
        self._thread.start()

    def _adopt_previous_recovery_record(self) -> None:
        """A previous run that could not confirm its release still blocks Live.

        The record outlives the process that wrote it, so a crash mid-hold does
        not silently become a clean start (plan 4.4).
        """
        if self._paths is None:
            return
        record = read_recovery_record(self._paths)
        if record is None:
            return
        reason = str(record.get("reason", "unknown"))
        self._authority.latch_release_uncertain(f"previous run: {reason}")
        self._events.add("release.recovery-record", reason)

    def _persist_recovery_record(self, reason: str, report: Any) -> None:
        if self._paths is None:
            return
        with contextlib.suppress(Exception):
            write_recovery_record(
                self._paths,
                reason,
                {
                    "failures": list(getattr(report, "failures", ())),
                    "deadman_acknowledged": bool(
                        getattr(report, "deadman_acknowledged", False)
                    ),
                    "ledger_empty": bool(getattr(report, "ledger_empty", False)),
                    "attempted_edges": len(getattr(report, "attempted_edges", ())),
                },
            )

    def _on_recover_release(self, intent: RuntimeIntent) -> None:
        """The explicit release-only handshake. It emits up-edges and nothing else."""
        report = self._authority.recover_release()
        if report.release_known_safe:
            if self._paths is not None:
                clear_recovery_record(self._paths)
            self._events.add("release.recovered", report.reason)
        else:
            self._persist_recovery_record("recovery handshake failed", report)
            self._events.add("release.recovery-failed", ",".join(report.failures) or "unknown")

    def shutdown(self, timeout_s: float | None = None) -> dict[str, str]:
        """Ordered, bounded shutdown (plan 3.5). Records any survivor."""
        deadline = timeout_s or self._config.component_join_deadline_s
        self.submit(self.next_intent(IntentType.SHUTDOWN, "system"))
        self._shutdown_complete.wait(deadline)
        report: dict[str, str] = {}

        self._authority.invalidate("shutdown")
        release = self._authority.release_all("shutdown")
        report["release"] = "known-safe" if release.release_known_safe else "uncertain"
        if not release.release_known_safe:
            self._persist_recovery_record("shutdown released with uncertainty", release)

        report["worker"] = "joined" if self._join_worker(deadline) else "survivor"
        report["capture"] = "stopped" if self._capture.stop(deadline) else "survivor"
        report["watchdog"] = (
            "stopped" if self._authority.stop_watchdog(deadline) else "survivor"
        )

        self._running.clear()
        with self._queue_lock:
            self._queue_lock.notify_all()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(deadline)
            report["coordinator"] = "survivor" if thread.is_alive() else "joined"
        self._events.add("shutdown.complete", ",".join(f"{k}={v}" for k, v in report.items()))
        return report

    # -- main loop --------------------------------------------------------
    def _loop(self) -> None:
        while self._running.is_set():
            item = self._take(self._config.idle_poll_s)
            if item is None:
                self._publish_telemetry()
                continue
            payload = item.payload
            try:
                if isinstance(payload, RuntimeIntent):
                    self._handle_intent(payload)
                elif isinstance(payload, SafetyFault):
                    self._handle_fault(payload)
                elif isinstance(payload, WorkerCompletion):
                    self._handle_completion(payload)
            except Exception as exc:
                # Any uncaught error at this boundary safe-stops (plan 17).
                self._events.add("coordinator.error", repr(exc))
                self._safe_stop(f"coordinator-error:{exc!r}")
            self._publish_telemetry()

    def _take(self, timeout_s: float) -> _QueueItem | None:
        with self._queue_lock:
            if not self._queue:
                self._queue_lock.wait(timeout_s)
            if not self._queue:
                return None
            item = heapq.heappop(self._queue)
            if isinstance(item.payload, RuntimeIntent):
                key = item.payload.coalescing_key()
                if key is not None:
                    self._pending_ordinary.discard(key)
            return item

    # -- intent handling --------------------------------------------------
    def _handle_intent(self, intent: RuntimeIntent) -> None:
        handler = {
            IntentType.STOP: self._on_stop,
            IntentType.SHUTDOWN: self._on_shutdown,
            IntentType.PIN_WINDOW: self._on_pin,
            IntentType.ARM_LIVE_FROM_UI: self._on_arm,
            IntentType.START_SHADOW: self._on_start_shadow,
            IntentType.START_LIVE: self._on_start_live,
            IntentType.RESET_CHARACTER: self._on_service,
            IntentType.PAN_SWAP_TEST: self._on_service,
            IntentType.DIG_LOOP: self._on_service,
            IntentType.PIXEL_INFO: self._on_service,
            IntentType.RECOVER_RELEASE: self._on_recover_release,
        }.get(intent.intent_type)
        if handler is None:
            return
        handler(intent)

    def _on_stop(self, intent: RuntimeIntent) -> None:
        self._events.add("intent.stop", intent.source)
        self._safe_stop(f"stop:{intent.source}")

    def _on_shutdown(self, intent: RuntimeIntent) -> None:
        self._events.add("intent.shutdown", intent.source)
        self._safe_stop("shutdown")
        self._shutdown_complete.set()

    def _on_pin(self, intent: RuntimeIntent) -> None:
        ok, message, _rect = self._guard.pin()
        self._events.add("viewport.pin", f"{ok}: {message}")

    def _on_arm(self, intent: RuntimeIntent) -> None:
        """Create the one-use arm token. Only a physical UI click reaches here."""
        if intent.source != "gui":
            self._events.add("arm.refused", f"source={intent.source}")
            return
        now = monotonic_s()
        token = LiveArmToken(
            token_id=os.urandom(8).hex(),
            run_id=self._authority.run_id,
            generation=self._generation,
            created_at_s=now,
            expires_at_s=now + self._config.arm_ttl_s,
        )
        self._arm_token = token
        self._events.add("arm.created", f"ttl={self._config.arm_ttl_s:g}s")

    def _on_start_shadow(self, intent: RuntimeIntent) -> None:
        if not self._guard.geometry.valid:
            # Shadow only observes, so it adopts the client area as it is
            # rather than moving the user's window. A non-canonical client is
            # letterboxed into the canonical raster and reported as
            # ADOPTED_NONCANONICAL, so nothing downstream mistakes it for a
            # calibrated viewport.
            adopted = self._guard.adopt_current()
            self._events.add("viewport.adopted", adopted.describe())
        readiness = self.readiness()
        if not readiness.shadow_ok:
            self._events.add("shadow.refused", ",".join(readiness.reasons))
            return
        self._begin_mode(RunMode.SHADOW, intent, IntentType.START_SHADOW)

    def _on_start_live(self, intent: RuntimeIntent) -> None:
        """Convert the arm token to a single-use proof, then transition.

        The token is consumed *before* invalidation so that a failed readiness
        check afterwards cannot leave a reusable token behind (plan 3.4).
        """
        token = self.arm_token()
        self._arm_token = None
        if token is None:
            self._events.add("live.refused", "no arm token")
            return
        if intent.source != "hotkey":
            self._events.add("live.refused", f"source={intent.source}")
            return
        if token.run_id != self._authority.run_id:
            self._events.add("live.refused", "token run mismatch")
            return
        proof = _ConsumedArmProof(token_id=token.token_id, intent_sequence=intent.sequence)

        readiness = self.readiness()
        if not readiness.input_ok:
            self._events.add("live.refused", ",".join(readiness.reasons))
            return
        self._events.add("live.armed", proof.token_id)
        self._begin_mode(RunMode.LIVE, intent, IntentType.START_LIVE)

    def _on_service(self, intent: RuntimeIntent) -> None:
        if intent.intent_type is IntentType.PIXEL_INFO:
            # Read-only diagnostic: it never reaches an input session, so it
            # does not transition the mode or disturb an arm token.
            factory = self._workers.get(IntentType.PIXEL_INFO)
            if factory is not None:
                self._run_detached_diagnostic(factory, intent)
            return
        readiness = self.readiness()
        if not readiness.input_ok:
            self._events.add("service.refused", ",".join(readiness.reasons))
            return
        self._begin_mode(RunMode.SERVICE, intent, intent.intent_type)

    def _run_detached_diagnostic(self, factory: WorkerFactory, intent: RuntimeIntent) -> None:
        context = WorkerContext(
            generation=self._generation,
            mode=self._mode,
            worker_id=f"diagnostic-{intent.sequence}",
            cancellation=Cancellation(),
            frames=self._capture,
            intent=intent,
        )

        def _run() -> None:
            with contextlib.suppress(Exception):
                factory(context)

        threading.Thread(target=_run, name="treasure-diagnostic", daemon=True).start()

    # -- transitions ------------------------------------------------------
    def _begin_mode(self, mode: RunMode, intent: RuntimeIntent, key: IntentType) -> None:
        factory = self._workers.get(key)
        if factory is None:
            self._events.add("mode.refused", f"no worker for {key.name}")
            return

        # 1-4: invalidate, cancel, release, join the previous worker.
        self._authority.invalidate(f"transition:{mode.name}")
        if self._worker_cancel is not None:
            self._worker_cancel.cancel()
        release = self._authority.release_all(f"transition:{mode.name}")
        if not self._join_worker(self._config.worker_join_deadline_s):
            self._events.add("mode.worker-survivor", "previous worker did not join")

        # 5: an input mode needs an empty ledger and a known-safe release.
        if mode.emits_input and not (release.ledger_empty and release.release_known_safe):
            self._events.add("mode.refused", f"unsafe release: {release.reason}")
            self._enter(RunMode.SAFE_STOP)
            return

        self._generation += 1
        cancellation = Cancellation()
        self._registry.set_generation(self._generation)
        self._authority.activate_generation(
            self._generation,
            emits_input=mode.emits_input,
            cancellation=cancellation,
            requires_capture=True,
            pinned_rect=self._guard.geometry,
        )

        worker_id = f"{mode.name.lower()}-{self._generation}"
        context = WorkerContext(
            generation=self._generation,
            mode=mode,
            worker_id=worker_id,
            cancellation=cancellation,
            frames=self._capture,
            observer=NoInputSession() if mode is RunMode.SHADOW else None,
            navigation=(
                self._authority.navigation_session(self._generation)
                if mode is RunMode.LIVE
                else None
            ),
            service=(
                self._authority.service_session(self._generation)
                if mode is RunMode.SERVICE
                else None
            ),
            intent=intent,
            pipeline=self._pipeline_provider() if self._pipeline_provider else None,
            on_status=lambda message: self._events.add("worker.status", message),
            on_phase=self._set_phase,
            on_observation=self._publish_observation,
        )
        self._worker_cancel = cancellation
        self._worker_id = worker_id
        self._enter(mode)

        def _run() -> None:
            try:
                result = factory(context)
            except Exception as exc:
                result = ModeResult(ModeResultKind.FAILED, repr(exc))
            self._submit_completion(
                WorkerCompletion(
                    generation=context.generation,
                    mode=mode,
                    worker_id=worker_id,
                    result=result,
                )
            )

        thread = threading.Thread(target=_run, name=f"treasure-{worker_id}", daemon=True)
        self._worker = thread
        thread.start()

    def _join_worker(self, timeout_s: float) -> bool:
        thread = self._worker
        if thread is None:
            return True
        thread.join(timeout_s)
        if thread.is_alive():
            return False
        self._worker = None
        self._worker_cancel = None
        self._worker_id = None
        return True

    def _handle_completion(self, completion: WorkerCompletion) -> None:
        """Only a completion matching generation, mode, and worker id may transition."""
        if (
            completion.generation != self._generation
            or completion.mode is not self._mode
            or completion.worker_id != self._worker_id
        ):
            self._stale_completions += 1
            self._events.add(
                "worker.stale-completion",
                f"gen={completion.generation} mode={completion.mode.name} "
                f"id={completion.worker_id}",
            )
            return
        self._last_result = completion.result
        self._events.add(
            "worker.completed", f"{completion.worker_id}:{completion.result.kind.name}"
        )
        self._worker = None
        self._worker_cancel = None
        self._worker_id = None
        self._set_phase(None)
        if completion.result.kind in (ModeResultKind.FAILED, ModeResultKind.ABANDONED):
            self._safe_stop(f"worker:{completion.result.detail}")
            return
        self._authority.invalidate(f"worker-complete:{completion.result.kind.name}")
        self._authority.release_all(f"worker-complete:{completion.result.kind.name}")
        self._enter(RunMode.IDLE)

    def _handle_fault(self, fault: SafetyFault) -> None:
        if fault.generation is not None and fault.generation != self._generation:
            self._events.add("fault.stale", f"{fault.kind.name}@{fault.generation}")
            return
        self._events.add("fault", f"{fault.kind.name}:{','.join(fault.evidence)}")
        if self._mode is RunMode.SHADOW and fault.kind in (
            SafetyFaultKind.FOCUS_LOST,
            SafetyFaultKind.FOCUS_UNKNOWN,
        ):
            # Shadow holds a NoInputSession, and the user must be able to look
            # at the Tk window while it runs (plan 7.3).
            return
        self._safe_stop(f"fault:{fault.kind.name}")

    def _safe_stop(self, reason: str) -> None:
        self._last_stop_reason = reason
        self._authority.invalidate(reason)
        if self._worker_cancel is not None:
            self._worker_cancel.cancel()
        report = self._authority.release_all(reason)
        if not report.release_known_safe:
            self._persist_recovery_record(reason, report)
        joined = self._join_worker(self._config.worker_join_deadline_s)
        self._set_phase(None)
        if not joined:
            self._events.add("stop.worker-survivor", reason)
        self._enter(RunMode.SAFE_STOP)
        if (report.ledger_empty and report.release_known_safe) or self._mode is RunMode.SHADOW:
            self._enter(RunMode.IDLE)

    def _enter(self, mode: RunMode) -> None:
        if mode is not self._mode:
            self._events.add("mode", f"{self._mode.name}->{mode.name}")
        self._mode = mode

    def _set_phase(self, phase: NavigationPhase | None) -> None:
        self._phase = phase

    def clear_observations(self) -> None:
        self._observations.take()

    # -- readiness and telemetry -----------------------------------------
    def readiness(self) -> Readiness:
        geometry = self._guard.check()
        focus = self._authority._health.focus()
        age_s = self._capture.latest_age_s()
        max_age_s = self._capture.config.max_frame_age_ms / 1000.0
        reasons: list[str] = []
        if not geometry.state.can_capture:
            reasons.append(f"viewport:{geometry.state.value}")
        if focus is not True:
            reasons.append(f"focus:{focus}")
        capture_fresh = age_s is not None and age_s <= max_age_s
        if not capture_fresh:
            reasons.append(f"capture:{'none' if age_s is None else f'{age_s * 1000:.0f}ms'}")
        if not self._authority.watchdog_running:
            reasons.append("watchdog:stopped")
        deadman_ok = self._authority._deadman.healthy
        if not deadman_ok:
            reasons.append("deadman:unhealthy")
        ledger_empty = self._authority.ledger_empty()
        if not ledger_empty:
            reasons.append("ledger:held")
        release_safe = not self._authority.release_uncertain
        if not release_safe:
            reasons.append("release:uncertain")
        return Readiness(
            viewport_ok=geometry.state.can_capture,
            viewport_state=geometry.state,
            focus_ok=focus is True,
            capture_fresh=capture_fresh,
            watchdog_ok=self._authority.watchdog_running,
            deadman_ok=deadman_ok,
            ledger_empty=ledger_empty,
            release_known_safe=release_safe,
            reasons=tuple(reasons),
        )

    def _publish_telemetry(self) -> None:
        readiness = self.readiness()
        envelope = self._capture.latest()
        age_ms = None if envelope is None else envelope.frame.age_s(monotonic_s()) * 1000.0
        warnings: list[str] = []
        if self._authority.release_uncertain:
            warnings.append(f"RELEASE UNCERTAIN: {self._authority.release_uncertain_reason}")
        if self._capture.stalled():
            warnings.append("capture stalled")
        if self._last_stop_reason:
            warnings.append(f"last stop: {self._last_stop_reason}")
        token = self.arm_token()
        readiness_map = readiness.as_map()
        readiness_map["arm"] = (
            "none" if token is None else f"{token.remaining_s(monotonic_s()):.0f}s"
        )
        readiness_map["pixels"] = (
            "PENDING reverification"
            if readiness.calibrated_pixels_apply
            else "N/A (non-canonical viewport)"
        )
        metrics = self._capture.metrics()
        readiness_map["capture"] = (
            f"{metrics.unique_fps:.0f}/s {metrics.backend}"
            if metrics.unique_fps > 0
            else readiness_map.get("capture", "stale")
        )
        if metrics.degraded_reason:
            warnings.append(f"DEGRADED: {metrics.degraded_reason}")
        observation = self._observations.peek()
        self._hub.publish(
            TelemetrySnapshot(
                sequence=0,
                mode=self._mode,
                phase=self._phase,
                viewport=self._guard.geometry,
                arrow=None if observation is None else observation.arrow,
                direction=None if observation is None else observation.direction,
                motion=None if observation is None else observation.motion,
                arrival=None if observation is None else observation.arrival,
                command=None if observation is None else observation.command,
                ledger_empty=readiness.ledger_empty,
                focus=self._authority._health.focus(),
                frame_age_ms=age_ms,
                warnings=tuple(warnings),
                readiness=readiness_map,
                metrics=metrics,
            )
        )

    def snapshot(self) -> TelemetrySnapshot | None:
        return self._hub.latest()


def replace_worker(
    coordinator: RuntimeCoordinator, key: IntentType, factory: WorkerFactory
) -> None:
    """Swap one worker factory. Used by tests and by the app wiring."""
    coordinator._workers[key] = factory
