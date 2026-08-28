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
from dataclasses import dataclass, field, replace
from typing import Any

from prospector_engine.capture import CaptureService, EvidenceRegistry, ViewportGuard
from prospector_engine.contracts import (
    BlockerScope,
    Cancellation,
    CapturedFrame,
    CaptureMetrics,
    ControlState,
    DiagnosticObservation,
    EvidenceStatus,
    FitCompletion,
    IntentType,
    LiveBlocker,
    ModeResult,
    ModeResultKind,
    NavigationPhase,
    PacketKind,
    Provenance,
    RunMode,
    RuntimeIntent,
    RuntimeKey,
    SafetyFault,
    SafetyFaultKind,
    SetupProgress,
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


#: Stop reasons that are simply how a session ends. Rendering these in red
#: taught users to ignore red, which is the opposite of what a fault colour is
#: for: red is reserved for a fault happening *now* (mission section 10).
_NEUTRAL_STOPS = ("stop:", "shutdown", "worker-complete", "transition:", "return-to-shadow")


def _describe_stop(reason: str) -> str:
    """Turn an internal stop reason into a neutral historical sentence."""
    if any(reason.startswith(prefix) for prefix in _NEUTRAL_STOPS):
        return "Previous session ended normally"
    if reason.startswith("fault:"):
        return f"Previous session ended on a safety check ({reason.split(':', 1)[1]})"
    if reason.startswith("geometry-changed"):
        return "Previous session ended because the Roblox window changed"
    if reason.startswith("profile-changed"):
        return "Previous session ended because the arrow profile changed"
    return f"Previous session ended: {reason}"


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


@dataclass(frozen=True)
class WorkerHealth:
    """What a worker may know about the world outside its own frames."""

    focus_ok: bool = True
    cursor_safe: bool = True
    geometry_revision: int = 0
    profile_revision: int = 0


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
    #: Stamps one frame with the identity every consumer compares before it
    #: draws or acts. Supplied by the coordinator so a worker cannot invent a
    #: key that outranks a newer session.
    key_for: Callable[[CapturedFrame, int], RuntimeKey] = lambda frame, revision: RuntimeKey(
        "local", 0, 0, 0, 0, revision, frame.sequence
    )
    #: Live blockers in plain language, for the packet the UI renders.
    blockers: tuple[str, ...] = ()
    #: Runtime health the controller is allowed to see: focus, the coordinate
    #: basis, the profile generation. Supplied by the coordinator so a worker
    #: cannot read a value that has already been superseded.
    health: Callable[[], WorkerHealth] = lambda: WorkerHealth()
    on_status: Callable[[str], None] = lambda _message: None
    on_phase: Callable[[NavigationPhase | None], None] = lambda _phase: None
    on_observation: Callable[[DiagnosticObservation], None] = lambda _observation: None


#: Runs one automatic-setup pass. Takes a "should I stop" predicate and a
#: progress sink, and returns the terminal progress. A plain alias rather than
#: a Protocol so the application can supply a closure over its own objects and
#: the coordinator stays ignorant of what setup actually touches.
SetupRunner = Callable[[Callable[[], bool], Callable[[SetupProgress], None]], SetupProgress]


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
        profiles: Any = None,
        setup_runner: SetupRunner | None = None,
        cursor_probe: Callable[[], tuple[int, int] | None] | None = None,
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
        self._profiles = profiles
        self._setup_runner = setup_runner
        # A read-only pointer probe, not a port: the coordinator must never
        # hold something that could emit input (plan 4.2).
        self._cursor_probe = cursor_probe
        self._setup_progress = SetupProgress.idle()
        self._setup_thread: threading.Thread | None = None
        self._setup_cancel = threading.Event()
        # A dedicated channel, deliberately not the emit-on-change hub: a
        # per-frame observation must publish for every processed frame, and
        # change-suppression would silently drop identical consecutive ones
        # (mission section 7).
        self._observations: LatestSlot[DiagnosticObservation] = LatestSlot()
        self._observation_count = 0
        self._stale_packets = 0

        self._queue: list[_QueueItem] = []
        self._queue_lock = threading.Condition()
        self._order = itertools.count()
        self._pending_ordinary: set[tuple[IntentType, str]] = set()
        self._intent_sequence = itertools.count(1)

        self._mode = RunMode.IDLE
        self._generation = 0
        #: Increments on every mode entry *and* every safe stop, so a terminal
        #: packet always outranks the frames of the session it ends.
        self._mode_session = 0
        self._phase: NavigationPhase | None = None
        self._control_state: ControlState | None = None
        self._arm_token: LiveArmToken | None = None
        self._fit_thread: threading.Thread | None = None
        self._fit_generation = 0
        self._stale_fits = 0
        self._last_session_note = ""
        self._recording = "off"
        self._extra_blockers: tuple[LiveBlocker, ...] = ()
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
        """Publish one packet, refusing anything an older world produced.

        A worker that is being cancelled can still be mid-frame when a new
        session starts. Comparing the key here - rather than trusting arrival
        order - is what makes a mixed-key dashboard impossible.
        """
        current = self._observations.peek()
        if current is not None and not observation.key.supersedes(current.key):
            self._stale_packets += 1
            return
        self._observation_count += 1
        self._observations.publish(observation)

    # -- packet identity --------------------------------------------------
    def runtime_key(self, frame: CapturedFrame, profile_revision: int) -> RuntimeKey:
        """Stamp one frame with the identity of the world that produced it."""
        return RuntimeKey(
            run_id=self._authority.run_id,
            coordinator_generation=self._generation,
            mode_session_id=self._mode_session,
            source_epoch=self._capture.source_epoch,
            geometry_revision=self._guard.revision,
            profile_revision=profile_revision,
            frame_sequence=frame.sequence,
            content_id=frame.content_id,
        )

    @property
    def stale_packets(self) -> int:
        """Packets refused because a newer world already exists."""
        return self._stale_packets

    def publish_transition(
        self, reason: str, *, terminal: bool = False
    ) -> DiagnosticObservation | None:
        """Announce a lifecycle edge as a packet, not as an absent update.

        Stop, a profile swap, a window change, and a source replacement all
        produce one. It carries the last real frame so the view does not go
        blank, but ``command`` is ``None`` and the kind says it is frozen -
        which is what stops a stale picture from ever looking actionable
        (mission section 6).
        """
        previous = self._observations.peek()
        if previous is None:
            return None
        kind = PacketKind.TERMINAL if terminal else PacketKind.TRANSITION
        packet = replace(
            previous,
            key=replace(previous.key, mode_session_id=self._mode_session),
            packet_kind=kind,
            command=None,
            # Frozen, so the action layer is cleared rather than left showing
            # the last thing that was held. A stopped navigator that still
            # draws an ACTIVE key is indistinguishable from a running one.
            command_view=previous.command_view.freeze(),
            phase=None,
            control_state=None,
            plain_summary=reason,
            published_at_s=monotonic_s(),
        )
        self._observations.publish(packet)
        self._observation_count += 1
        return packet

    @property
    def events(self) -> EventLog:
        return self._events

    @property
    def last_result(self) -> ModeResult | None:
        return self._last_result

    @property
    def stale_completions(self) -> int:
        return self._stale_completions

    @property
    def armed(self) -> bool:
        """Whether a live arm token is currently held and unexpired."""
        return self.arm_token() is not None

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
        # Persist on *this* release's own evidence, not on release_known_safe,
        # which also refuses while an earlier run's latch is still set. Using
        # the latched value made the record self-perpetuating: one uncertain
        # shutdown wrote a record, every later run inherited it, and every
        # later shutdown re-wrote it from a release that had actually gone
        # perfectly - a positive deadman ACK, an empty ledger and no failures.
        # The machine could then never leave the state on its own.
        if not release.evidence_clean:
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
                elif isinstance(payload, FitCompletion):
                    self._handle_fit_completion(payload)
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
            IntentType.CONNECT_WINDOW: self._on_connect,
            IntentType.FIT_VIEWPORT: self._on_fit,
            IntentType.SELECT_PROFILE: self._on_select_profile,
            IntentType.RETURN_TO_SHADOW: self._on_return_to_shadow,
            IntentType.ARM_LIVE_FROM_UI: self._on_arm,
            IntentType.START_SHADOW: self._on_start_shadow,
            IntentType.START_LIVE: self._on_start_live,
            IntentType.RESET_CHARACTER: self._on_service,
            IntentType.PAN_SWAP_TEST: self._on_service,
            IntentType.DIG_LOOP: self._on_service,
            IntentType.PIXEL_INFO: self._on_service,
            IntentType.RECOVER_RELEASE: self._on_recover_release,
            IntentType.START_NAVIGATOR: self._on_start_navigator,
            IntentType.RETRY_SETUP: self._on_start_navigator,
        }.get(intent.intent_type)
        if handler is None:
            return
        handler(intent)

    def _on_stop(self, intent: RuntimeIntent) -> None:
        self._events.add("intent.stop", intent.source)
        # Setup is cancelled before the safe stop, so a stage that is mid-poll
        # unwinds instead of finishing into a runtime that has already stopped.
        self._setup_cancel.set()
        self._safe_stop(f"stop:{intent.source}")
        self._export_trace("stop")

    def _export_trace(self, label: str) -> None:
        """Write the bounded frame trace beside the logs. Best effort."""
        if self._paths is None:
            return
        written = self._capture.export_trace(self._paths.logs, label=label)
        if written is not None:
            self._events.add("trace.exported", str(written))

    def _on_shutdown(self, intent: RuntimeIntent) -> None:
        self._events.add("intent.shutdown", intent.source)
        self._safe_stop("shutdown")
        self._shutdown_complete.set()

    # -- automatic setup ---------------------------------------------------
    @property
    def setup_progress(self) -> SetupProgress:
        return self._setup_progress

    @property
    def setup_active(self) -> bool:
        thread = self._setup_thread
        return thread is not None and thread.is_alive()

    def _publish_setup(self, progress: SetupProgress) -> None:
        """Adopt a stage transition and let the dashboard see it immediately."""
        self._setup_progress = progress
        self.publish_transition(f"Setup: {progress.detail}")

    def _on_start_navigator(self, intent: RuntimeIntent) -> None:
        """Run automatic setup off the event loop, then start observing.

        Off the loop because finding, resizing and re-binding to another
        process's window takes seconds, and Stop must stay responsive for every
        one of them. The thread owns nothing: it publishes progress and submits
        an ordinary intent when it is done.
        """
        if self._setup_runner is None:
            self._events.add("setup.unavailable", "no setup runner is installed")
            return
        if self.setup_active:
            self._events.add("setup.busy", "automatic setup is already running")
            return
        self._setup_cancel = threading.Event()
        cancel = self._setup_cancel
        runner = self._setup_runner
        self._events.add("intent.start-navigator", intent.source)

        def _run() -> None:
            try:
                progress = runner(cancel.is_set, self._publish_setup)
            except Exception as exc:  # never take the process down
                self._events.add("setup.error", repr(exc))
                return
            self._setup_progress = progress
            if progress.ok and not cancel.is_set():
                self.submit(self.next_intent(IntentType.START_SHADOW, "system"))

        self._setup_thread = threading.Thread(target=_run, name="treasure-setup", daemon=True)
        self._setup_thread.start()

    def _on_connect(self, intent: RuntimeIntent) -> None:
        """Bind to the Roblox client without touching it (mission section 4)."""
        del intent
        before = self._guard.revision
        geometry = self._guard.connect()
        self._events.add("viewport.connect", geometry.describe())
        if self._guard.revision != before:
            self._on_geometry_change("connected to the Roblox client")

    def _on_fit(self, intent: RuntimeIntent) -> None:
        """Run the bounded fit machine off the event loop.

        A resize is a request to another process; waiting for it inline would
        make Stop unresponsive for as long as the game took to answer, which is
        exactly the wrong thing to trade away.
        """
        del intent
        if self.fit_active:
            self._events.add("viewport.fit", "already fitting")
            return
        self._fit_generation += 1
        generation = self._fit_generation
        revision_before = self._guard.revision
        guard = self._guard

        def _run() -> None:
            # The thread owns nothing. It submits a typed completion and the
            # coordinator loop applies it, so no worker thread ever mutates
            # coordinator, capture or arm state.
            fit = guard.fit_and_lock()
            self._submit_fit_completion(FitCompletion(generation, fit, revision_before))

        self._fit_thread = threading.Thread(target=_run, name="treasure-fit", daemon=True)
        self._fit_thread.start()

    @property
    def fit_active(self) -> bool:
        thread = self._fit_thread
        return thread is not None and thread.is_alive()

    @property
    def stale_fits(self) -> int:
        return self._stale_fits

    def _submit_fit_completion(self, completion: FitCompletion) -> None:
        with self._queue_lock:
            heapq.heappush(
                self._queue,
                _QueueItem(self.PRIORITY_ORDINARY, next(self._order), completion),
            )
            self._queue_lock.notify()

    def _handle_fit_completion(self, completion: FitCompletion) -> None:
        """Apply a finished fit on the coordinator's own thread.

        A completion from an earlier fit generation is ignored: it describes a
        request that was superseded. Geometry is invalidated only if the guard's
        revision actually moved, so a fit that changed nothing costs nothing.
        """
        if completion.generation != self._fit_generation:
            self._stale_fits += 1
            self._events.add("viewport.fit-stale", completion.fit.describe())
            return
        self._events.add("viewport.fit", completion.fit.describe())
        if self._guard.revision != completion.revision_before:
            self._on_geometry_change(f"viewport fit: {completion.fit.phase.value}")
        else:
            self.publish_transition(f"Viewport fit finished: {completion.fit.phase.value}")

    def _on_geometry_change(self, reason: str) -> None:
        """A new coordinate basis invalidates everything derived from the old one.

        Live releases first and is blocked from pressing again until the new
        basis is verified; stale frames, observations, tracker state and any
        actionable command are dropped rather than reinterpreted.
        """
        if self._mode is RunMode.LIVE:
            self._events.add("viewport.live-released", reason)
            self._safe_stop(f"geometry-changed:{reason}")
        self._arm_token = None
        self._capture.slot.clear()
        self._capture.reset_epoch(reason)
        self.publish_transition(f"Viewport changed - {reason}")

    def _on_select_profile(self, intent: RuntimeIntent) -> None:
        """Stage a profile swap; the pipeline applies it at a frame boundary.

        Changing profile while armed spends the arm, and changing it during
        Live releases input and safe-stops: the arm token is bound to a
        specific profile revision, so continuing under a new one would be
        acting on a permission nobody gave (mission section 7).
        """
        authority = self._profiles
        if authority is None:
            return
        pending = authority.pending_id
        if pending is None:
            return
        if self._mode is RunMode.LIVE:
            self._events.add("profile.live-released", pending)
            self._safe_stop(f"profile-changed:{pending}")
        if self._arm_token is not None:
            self._events.add("arm.invalidated", "profile changed")
            self._arm_token = None
        self._events.add("profile.requested", pending)
        self.publish_transition(f"Profile changing to {pending}")

    def _on_return_to_shadow(self, intent: RuntimeIntent) -> None:
        """Leave Live but keep observing: release movement, keep perception."""
        if self._mode is not RunMode.LIVE:
            return
        self._events.add("live.return-to-shadow", intent.source)
        self._safe_stop("return-to-shadow")
        self.submit(self.next_intent(IntentType.START_SHADOW, "system"))

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
            # Shadow only observes, so it connects to the client area as it is
            # rather than moving the user's window. A non-canonical client is
            # letterboxed into the canonical raster and reported as
            # ADOPTED_NONCANONICAL, so nothing downstream mistakes it for a
            # calibrated viewport.
            adopted = self._guard.connect()
            self._events.add("viewport.adopted", adopted.describe())
        readiness = self.readiness()
        if not readiness.shadow_ok:
            self._events.add("shadow.refused", ",".join(readiness.reasons))
            return
        self._begin_mode(RunMode.SHADOW, intent, IntentType.START_SHADOW)

    #: Readiness reasons that a second attempt could plausibly satisfy. The
    #: user clicks into Roblox, or one late frame arrives, and the same arm is
    #: good again. Everything else - a lost window, a held lease, an
    #: unconfirmed release - burns the token, because retrying into it would be
    #: retrying into a fault.
    TRANSIENT_READINESS_PREFIXES = ("focus:", "capture:")

    def _on_start_live(self, intent: RuntimeIntent) -> None:
        """Check readiness and consume the arm token in one atomic step.

        Ordering matters twice over, in opposite directions:

        * A token must never survive a *real* failure, or a stale arm could be
          replayed into a broken world (plan 3.4).
        * A token must not be burned by a *transient* one. Pressing the chord a
          fraction of a second before Roblox comes frontmost is the single most
          likely way to use this, and spending the arm on it - forcing another
          trip to the button - made the feature feel broken.

        So one readiness snapshot is taken, bound to this token, and the token
        is consumed unless the only thing wrong is transient. The arm stays
        single-use and still expires on its own TTL either way.
        """
        token = self.arm_token()
        if token is None:
            self._events.add("live.refused", "no arm token")
            return
        if intent.source != "hotkey":
            # Not a physical chord. Burn it: something is submitting on the
            # arm's behalf, which is exactly what the token exists to stop.
            self._arm_token = None
            self._events.add("live.refused", f"source={intent.source}")
            return
        if token.run_id != self._authority.run_id:
            self._arm_token = None
            self._events.add("live.refused", "token run mismatch")
            return

        # One snapshot, read once, for every decision below.
        readiness = self.readiness()
        if not readiness.input_ok:
            transient = all(
                reason.startswith(self.TRANSIENT_READINESS_PREFIXES)
                for reason in readiness.reasons
            )
            if transient and readiness.reasons:
                self._events.add(
                    "live.not-yet",
                    f"{','.join(readiness.reasons)} (arm kept; press the chord again)",
                )
                return
            self._arm_token = None
            self._events.add("live.refused", ",".join(readiness.reasons))
            return

        self._arm_token = None
        proof = _ConsumedArmProof(token_id=token.token_id, intent_sequence=intent.sequence)
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
            key_for=self.runtime_key,
            blockers=self.live_blockers(),
            health=self._worker_health,
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

    #: Fraction of the client the pointer must stay inside while mouse yaw is
    #: the actuator. Matches ``SteeringLimits.safe_region_fraction``; kept here
    #: as a number rather than an import so the coordinator does not depend on
    #: the steering module.
    CURSOR_SAFE_FRACTION = 0.72

    def _worker_health(self) -> WorkerHealth:
        """One coherent read of focus, geometry, profile and pointer safety."""
        focus = self._authority.describe_readiness().get("focus")
        profiles = self._profiles
        return WorkerHealth(
            focus_ok=focus == "ok",
            cursor_safe=self._cursor_safe(),
            geometry_revision=self._guard.revision,
            profile_revision=profiles.revision if profiles is not None else 0,
        )

    def _cursor_safe(self) -> bool:
        """Whether the pointer is inside the safe region of the client.

        A pointer drifting to the edge while a yaw drag is in flight is how a
        drag ends up outside the window entirely. Unknown is *unsafe*: the
        follower releases and the pointer is recentred before anything resumes.
        A backend that does not use the pointer ignores this (see
        ``Navigator._steer``).
        """
        probe = self._cursor_probe
        if probe is None:
            return True
        try:
            point = probe()
        except Exception:
            return False
        if point is None:
            return False
        width, height = self._guard.geometry.canonical_px
        margin = (1.0 - self.CURSOR_SAFE_FRACTION) / 2.0
        return width * margin <= point[0] <= width * (
            1.0 - margin
        ) and height * margin <= point[1] <= height * (1.0 - margin)

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
        detail = completion.result.detail
        self._events.add(
            "worker.completed",
            f"{completion.worker_id}:{completion.result.kind.name}"
            + (f" - {detail}" if detail else ""),
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
        self.set_control_state(None)
        self._arm_token = None
        if not joined:
            self._events.add("stop.worker-survivor", reason)
        self._enter(RunMode.SAFE_STOP)
        if (report.ledger_empty and report.release_known_safe) or self._mode is RunMode.SHADOW:
            self._enter(RunMode.IDLE)
        # After Stop the actionable command is None *immediately*: the packet
        # goes out here rather than waiting for the next frame, because a stale
        # command on screen during a stop is exactly the wrong thing to show.
        self._last_session_note = _describe_stop(reason)
        self.publish_transition(f"Stopped - {self._last_session_note}", terminal=True)

    def _enter(self, mode: RunMode) -> None:
        if mode is not self._mode:
            self._events.add("mode", f"{self._mode.name}->{mode.name}")
            # Every mode edge is a new world. Bumping the session id here is
            # what lets a terminal packet outrank the frames it terminates.
            self._mode_session += 1
        self._mode = mode

    def _set_phase(self, phase: NavigationPhase | None) -> None:
        self._phase = phase

    def set_control_state(self, state: ControlState | None) -> None:
        self._control_state = state

    def set_recording(self, description: str) -> None:
        """Recorder status, for the header. Diagnostics only; never a gate."""
        self._recording = description

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

    def blockers(self, metrics: CaptureMetrics | None = None) -> tuple[LiveBlocker, ...]:
        """Every reason Live cannot start right now, keyed and scoped.

        Recomputed on every call from the live readiness, the capture
        metrics, and the installed commissioning gates. One entry per code:
        a condition that stops being true disappears, and details that belong
        to one gate live under that gate rather than as separate rows.
        ``metrics`` lets a caller that already sampled them avoid a second
        sample; the telemetry publisher runs several times a second.
        """
        found: list[LiveBlocker] = []
        readiness = self.readiness()
        if not readiness.viewport_ok:
            found.append(
                LiveBlocker(
                    "VIEWPORT",
                    BlockerScope.SHADOW,
                    "blocking",
                    f"Roblox window is not usable ({readiness.viewport_state.value})",
                    "Capture is not bound to a usable Roblox client area.",
                    "Press Start Navigator with Roblox open and windowed.",
                )
            )
        if not readiness.focus_ok:
            found.append(
                LiveBlocker(
                    "FOCUS",
                    BlockerScope.RUNTIME,
                    "expected",
                    "Roblox is not the frontmost window",
                    "Expected while you look at this dashboard. Shadow keeps running; "
                    "Live only starts when Roblox has focus at the moment F1 is pressed.",
                    "Focus Roblox before pressing F1.",
                )
            )
        if not readiness.capture_fresh:
            found.append(
                LiveBlocker(
                    "CAPTURE",
                    BlockerScope.SHADOW,
                    "blocking",
                    "No fresh frame from the capture pipeline",
                    "The newest frame is older than the freshness budget, or none has arrived.",
                    "Press Start Navigator; if set up, check Screen Recording permission.",
                )
            )
        if not readiness.watchdog_ok:
            found.append(
                LiveBlocker(
                    "WATCHDOG",
                    BlockerScope.LIVE,
                    "blocking",
                    "The safety watchdog is not running",
                    "Held input cannot be released automatically without it.",
                    "Restart the application.",
                )
            )
        if not readiness.deadman_ok:
            found.append(
                LiveBlocker(
                    "DEADMAN",
                    BlockerScope.LIVE,
                    "blocking",
                    "The release-only deadman helper is unhealthy",
                    "The helper process that releases input if this one dies is not answering.",
                    "Restart the application.",
                )
            )
        if not readiness.ledger_empty:
            found.append(
                LiveBlocker(
                    "LEDGER",
                    BlockerScope.LIVE,
                    "blocking",
                    "An input is still held from an earlier session",
                    "The input ledger is not empty.",
                    "Press Stop & Release All Input.",
                )
            )
        if not readiness.release_known_safe:
            found.append(
                LiveBlocker(
                    "RELEASE",
                    BlockerScope.LIVE,
                    "blocking",
                    "A previous release could not be confirmed safe",
                    "The last release did not complete every edge; nothing new is pressed "
                    "until an explicit release-only handshake succeeds.",
                    "Press Recover Release, then Stop & Release All Input.",
                )
            )
        if metrics is None:
            metrics = self._capture.metrics()
        if not metrics.live_eligible:
            found.append(
                LiveBlocker(
                    "CADENCE",
                    BlockerScope.LIVE,
                    "blocking",
                    "Capture cadence is not Live-eligible",
                    f"{metrics.governor.reason}. Judged on the last "
                    f"{self._capture.config.recent_window_s:g} s of processed frames.",
                    "Let the navigator observe for a few seconds while the cadence settles.",
                )
            )
        found.extend(self._setup_blockers())
        found.extend(self._extra_blockers)
        deduped: dict[str, LiveBlocker] = {}
        for blocker in found:
            deduped.setdefault(blocker.code, blocker)
        return tuple(deduped.values())

    def live_blockers(self) -> tuple[str, ...]:
        """The blockers as one line each, for the header and the event log."""
        return tuple(blocker.describe() for blocker in self.blockers())

    def _setup_blockers(self) -> tuple[LiveBlocker, ...]:
        """Automatic setup's own state, as a blocker the UI already knows how to
        render.

        One row, derived from live state, replacing the fourteen frozen
        commissioning gates. A gate that only a physical procedure could set
        and that no production code ever set is not a blocker a user can clear
        - it is a wall - and that is what this is not.
        """
        progress = self._setup_progress
        if progress.ok:
            return ()
        if progress.running:
            return (
                LiveBlocker(
                    "SETUP",
                    BlockerScope.RUNTIME,
                    "expected",
                    f"Automatic setup is running ({progress.stage.value.replace('_', ' ')})",
                    progress.detail,
                    "Wait for setup to finish.",
                ),
            )
        failure = progress.failure
        if failure is not None:
            return (
                LiveBlocker(
                    "SETUP",
                    BlockerScope.RUNTIME,
                    "blocking",
                    failure.summary,
                    failure.detail or failure.summary,
                    failure.remedy,
                ),
            )
        return (
            LiveBlocker(
                "SETUP",
                BlockerScope.RUNTIME,
                "blocking",
                "The navigator has not been set up yet",
                "Automatic setup finds Roblox, sizes its window, and checks the arrow.",
                "Press Start Navigator.",
            ),
        )

    def set_extra_blockers(self, blockers: tuple[LiveBlocker, ...]) -> None:
        """Install additional application-supplied blockers.

        Kept as an extension point for a caller that knows something the
        coordinator cannot - a licensing state, say. It is deliberately no
        longer how evidence gates reach the UI.
        """
        self._extra_blockers = tuple(blockers)

    def _publish_telemetry(self) -> None:
        readiness = self.readiness()
        envelope = self._capture.latest()
        age_ms = None if envelope is None else envelope.frame.age_s(monotonic_s()) * 1000.0
        warnings: list[str] = []
        if self._authority.release_uncertain:
            warnings.append(f"RELEASE UNCERTAIN: {self._authority.release_uncertain_reason}")
        if self._capture.stalled():
            warnings.append("capture stalled")
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
        blockers = self.blockers(metrics)
        observation = self._observations.peek()
        actionable = observation is not None and self._mode.emits_input
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
                # A command is only ever reported while a mode that can emit
                # one is running. After Stop it is None immediately, without
                # waiting for another frame to overwrite it.
                command=observation.command if actionable and observation else None,
                ledger_empty=readiness.ledger_empty,
                focus=self._authority._health.focus(),
                frame_age_ms=age_ms,
                warnings=tuple(warnings),
                readiness=readiness_map,
                metrics=metrics,
                fit=self._guard.fit,
                fit_active=self.fit_active,
                blockers=blockers,
                live_blockers=tuple(blocker.describe() for blocker in blockers),
                last_session_note=self._last_session_note,
                control_state=self._control_state,
                setup=self._setup_progress,
                arm_state=readiness_map.get("arm", "none"),
                recording=self._recording,
            )
        )

    def snapshot(self) -> TelemetrySnapshot | None:
        return self._hub.latest()


def replace_worker(
    coordinator: RuntimeCoordinator, key: IntentType, factory: WorkerFactory
) -> None:
    """Swap one worker factory. Used by tests and by the app wiring."""
    coordinator._workers[key] = factory
