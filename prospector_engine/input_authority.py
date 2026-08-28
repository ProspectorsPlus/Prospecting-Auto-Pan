"""The single input authority: ledger, leases, capabilities, deadman, release.

Nothing else in the application may call a :class:`~prospector_engine.ports.PlatformPort`
input method. Feature workers receive one of three narrow capability sessions
and never see the port, the ledger, or the generation counter (plan 4.2-4.5).

Lock discipline (deadlock-free by construction):

``_edge_barrier`` -> ``_lock``
    The barrier is always taken *first*. No code path takes the barrier while
    already holding ``_lock``. Native edges are emitted while holding both, so
    a down-edge cannot interleave with the release floor; the already-ACKed
    deadman lease is the fallback if a native call stalls inside the barrier.

Bugs fixed here: B5 (Stop released only LMB), B8 (platform modules kept their
own held-button set).
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from prospector_engine.contracts import (
    CancellationToken,
    EvidenceToken,
    FocusState,
    InputKey,
    LeaseHandle,
    MouseButton,
    NavigationApplyResult,
    NavigationApplyStatus,
    NavigationCommand,
    Provenance,
    ReleaseReport,
    SafetyFault,
    SafetyFaultKind,
    monotonic_s,
)
from prospector_engine.geometry import ViewportGeometry
from prospector_engine.ports import PlatformPort

__all__ = [
    "AuthorityConfig",
    "DeadmanClient",
    "DeadmanUnavailable",
    "HealthSources",
    "InputAuthority",
    "NavigationInputSession",
    "NoInputSession",
    "ServiceInputSession",
]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorityConfig:
    """Timing bounds for the safety machinery.

    Every value here is *provisional configuration* with provenance, not a
    measurement. Plan 3.3/4.5 require these to be met by E-PERF and the native
    release gates before any platform may run Live; they are never quietly
    loosened after testing.
    """

    safety_poll_interval_ms: int = 25
    max_rolling_lease_horizon_ms: int = 250
    max_hold_ms: int = 2000
    max_evidence_age_ms: int = 100
    max_capture_duration_ms: int = 40
    deadman_request_timeout_ms: int = 500
    deadman_start_timeout_ms: int = 4000
    stop_release_budget_ms: int = 250
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=__import__(
                "prospector_engine.contracts", fromlist=["EvidenceStatus"]
            ).EvidenceStatus.PROVISIONAL,
            source="TREASURE_NAVIGATION_PLAN.md sections 3.3, 4.5, 7.4 E-PERF",
            note="engineering budget; E-PERF and per-OS release gates are PENDING",
        )
    )


# ---------------------------------------------------------------------------
# Deadman client
# ---------------------------------------------------------------------------


class DeadmanUnavailable(RuntimeError):
    """The release-only helper could not be started or stopped responding."""


class DeadmanClient:
    """Parent side of the ``TREASURE_DEADMAN_*`` protocol.

    Every request is answered within ``request_timeout_ms`` or the helper is
    considered unhealthy. A down-edge is never emitted without a positive
    registration ACK from this client (plan 4.5).
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        config: AuthorityConfig | None = None,
        argv: list[str] | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        self._config = config or AuthorityConfig()
        self._token = token or os.urandom(16).hex()
        self._argv = argv or self._default_argv()
        self._env_overrides = env_overrides or {}
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._healthy = False
        self._last_error: str | None = None

    @staticmethod
    def _default_argv() -> list[str]:
        """Source and PyInstaller-frozen launch paths (plan 4.5)."""
        if getattr(sys, "frozen", False):
            return [sys.executable, "--deadman"]
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return [sys.executable, os.path.join(root, "treasure.py"), "--deadman"]

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def start(self) -> None:
        env = dict(os.environ)
        env["TREASURE_DEADMAN_TOKEN"] = self._token
        env["TREASURE_DEADMAN_POLL_MS"] = str(self._config.safety_poll_interval_ms)
        env.update(self._env_overrides)
        with self._lock:
            # Fixed argv, no shell: the helper path is computed, never user text.
            self._proc = subprocess.Popen(
                self._argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                text=True,
                bufsize=1,
            )
        reply = self.request({"op": "hello"}, timeout_ms=self._config.deadman_start_timeout_ms)
        if not reply.get("ok"):
            self._healthy = False
            raise DeadmanUnavailable(f"deadman handshake failed: {reply}")
        self._healthy = True

    def request(self, payload: dict[str, Any], timeout_ms: int | None = None) -> dict[str, Any]:
        """Send one request and read one reply, bounded by a deadline.

        A timeout marks the helper unhealthy rather than blocking a caller
        that may be holding an input.
        """
        timeout_s = (timeout_ms or self._config.deadman_request_timeout_ms) / 1000.0
        message = dict(payload)
        message["token"] = self._token
        result: dict[str, Any] = {"ok": False, "error": "no-reply"}

        def _do() -> None:
            nonlocal result
            with self._lock:
                proc = self._proc
                if proc is None or proc.stdin is None or proc.stdout is None:
                    result = {"ok": False, "error": "not-started"}
                    return
                try:
                    proc.stdin.write(json.dumps(message) + "\n")
                    proc.stdin.flush()
                    line = proc.stdout.readline()
                except (OSError, ValueError) as exc:
                    result = {"ok": False, "error": f"io:{exc}"}
                    return
                if not line:
                    result = {"ok": False, "error": "eof"}
                    return
                try:
                    result = json.loads(line)
                except json.JSONDecodeError:
                    result = {"ok": False, "error": "bad-json"}

        worker = threading.Thread(target=_do, name="deadman-request", daemon=True)
        worker.start()
        worker.join(timeout_s)
        if worker.is_alive():
            self._healthy = False
            self._last_error = f"timeout after {timeout_s * 1000:.0f} ms"
            return {"ok": False, "error": "timeout"}
        if not result.get("ok"):
            self._last_error = str(result.get("error"))
            if result.get("error") in {"eof", "not-started"} or str(
                result.get("error", "")
            ).startswith("io:"):
                self._healthy = False
        return result

    def register(self, lease_id: int, generation: int, target: str, expires_in_ms: int) -> bool:
        reply = self.request(
            {
                "op": "register",
                "lease_id": lease_id,
                "gen": generation,
                "target": target,
                "expires_in_ms": expires_in_ms,
            }
        )
        return bool(reply.get("ok"))

    def renew(self, lease_id: int, generation: int, expires_in_ms: int) -> bool:
        reply = self.request(
            {
                "op": "renew",
                "lease_id": lease_id,
                "gen": generation,
                "expires_in_ms": expires_in_ms,
            }
        )
        return bool(reply.get("ok"))

    def forget(self, lease_id: int) -> bool:
        return bool(self.request({"op": "forget", "lease_id": lease_id}).get("ok"))

    def release_all(self, reason: str) -> bool:
        return bool(self.request({"op": "release_all", "reason": reason}).get("ok"))

    def ping(self) -> bool:
        ok = bool(self.request({"op": "ping"}).get("ok"))
        self._healthy = ok
        return ok

    def close(self, timeout_s: float = 1.0) -> None:
        """Bounded shutdown: ask, then close the pipe (EOF releases), then kill."""
        with self._lock:
            proc = self._proc
        if proc is None:
            return
        # A wedged helper must not block parent shutdown.
        with contextlib.suppress(Exception):
            self.request({"op": "shutdown"})
        with self._lock:
            self._proc = None
        with contextlib.suppress(Exception):
            if proc.stdin is not None:
                proc.stdin.close()  # EOF is an independent release trigger
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()  # release-only helper: killing it can never press anything
        self._healthy = False


class NullDeadmanClient(DeadmanClient):
    """A deadman stand-in for Shadow and for tests that emit no input.

    It reports healthy and ACKs everything, which is safe *only* because the
    sessions that use it cannot emit an edge. ``InputAuthority`` refuses to
    hand out an input-emitting session while this client is installed unless
    the caller explicitly opted in (``allow_null_deadman``), which the Live
    path never does.
    """

    def __init__(self) -> None:
        super().__init__(token="null", argv=["-"])
        self._healthy = True

    def start(self) -> None:
        self._healthy = True

    def request(self, payload: dict[str, Any], timeout_ms: int | None = None) -> dict[str, Any]:
        return {"ok": True, "op": payload.get("op")}

    def close(self, timeout_s: float = 1.0) -> None:
        self._healthy = False


# ---------------------------------------------------------------------------
# Health sources
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HealthSources:
    """Everything the watchdog polls, injected so tests need no real OS."""

    focus: Callable[[], FocusState]
    client_rect: Callable[[], ViewportGeometry | None]
    capture_age_s: Callable[[], float | None]


# ---------------------------------------------------------------------------
# Ledger entries
# ---------------------------------------------------------------------------


@dataclass
class _ActiveLease:
    handle: LeaseHandle
    target: str
    committed: bool = False


# ---------------------------------------------------------------------------
# Capability sessions
# ---------------------------------------------------------------------------


class NoInputSession:
    """Shadow's capability. Structurally unable to reach a raw port.

    It has no reference to the authority or the platform port at all; the only
    thing it can do is record what would have been applied (plan 3.4).
    """

    def __init__(self) -> None:
        self._proposed: list[NavigationCommand] = []
        self._lock = threading.Lock()

    def propose(self, command: NavigationCommand) -> None:
        with self._lock:
            self._proposed.append(command)

    def proposed(self) -> tuple[NavigationCommand, ...]:
        with self._lock:
            return tuple(self._proposed)

    def release_navigation(self, reason: str) -> ReleaseReport:
        """Shadow cannot have emitted an edge, so this is always safe."""
        return ReleaseReport(
            attempted_edges=(),
            failures=(),
            deadman_acknowledged=True,
            ledger_empty=True,
            release_known_safe=True,
            reason=f"shadow-noop:{reason}",
        )


class ServiceInputSession:
    """The bounded capability handed to reset / dig / pan-swap / next-map.

    Navigation code never receives these generic methods, and this session can
    never renew a navigation command (plan 5).
    """

    def __init__(self, authority: InputAuthority, generation: int) -> None:
        self._authority = authority
        self._generation = generation

    @property
    def generation(self) -> int:
        return self._generation

    def hold_key(self, key: InputKey, max_hold_ms: int) -> LeaseHandle | None:
        return self._authority.acquire_key(self._generation, key, max_hold_ms)

    def tap_key(self, key: InputKey, hold_ms: int) -> bool:
        return self._authority.tap_key(self._generation, key, hold_ms)

    def hold_button(self, button: MouseButton, max_hold_ms: int) -> LeaseHandle | None:
        return self._authority.acquire_button(self._generation, button, max_hold_ms)

    def tap_button(self, button: MouseButton, hold_ms: int) -> bool:
        return self._authority.tap_button(self._generation, button, hold_ms)

    def pointer_move_client(self, point_px: tuple[int, int]) -> bool:
        return self._authority.pointer_move_client(self._generation, point_px)

    def pointer_delta(self, dx: int, dy: int) -> bool:
        return self._authority.pointer_delta(self._generation, dx, dy)

    def scroll_lines(self, lines: int) -> bool:
        return self._authority.scroll_lines(self._generation, lines)

    def renew(self, lease: LeaseHandle, horizon_ms: int) -> bool:
        return self._authority.renew(self._generation, lease, horizon_ms)

    def release(self, lease: LeaseHandle) -> None:
        self._authority.release_lease(self._generation, lease)

    def release_all(self, reason: str) -> ReleaseReport:
        return self._authority.release_all(reason)


class NavigationInputSession:
    """The *only* navigation input path (plan 4.3).

    ``apply_navigation_command`` atomically validates an authority-issued
    ``EvidenceToken`` before translating the accepted axes/jump/yaw into
    bounded leases. There is no generic hold/tap/pointer method here.
    """

    def __init__(self, authority: InputAuthority, generation: int) -> None:
        self._authority = authority
        self._generation = generation

    @property
    def generation(self) -> int:
        return self._generation

    def apply_navigation_command(
        self, command: NavigationCommand, evidence: EvidenceToken
    ) -> NavigationApplyResult:
        return self._authority.apply_navigation_command(self._generation, command, evidence)

    def release_navigation(self, reason: str) -> ReleaseReport:
        return self._authority.release_navigation(self._generation, reason)


# ---------------------------------------------------------------------------
# The authority
# ---------------------------------------------------------------------------


class InputAuthority:
    """Sole owner of held input state, native edges, and the release floor."""

    def __init__(
        self,
        port: PlatformPort,
        *,
        deadman: DeadmanClient,
        health: HealthSources,
        config: AuthorityConfig | None = None,
        run_id: str | None = None,
        on_safety_fault: Callable[[SafetyFault], None] | None = None,
    ) -> None:
        self._port = port
        self._deadman = deadman
        self._health = health
        self._config = config or AuthorityConfig()
        self._run_id = run_id or os.urandom(8).hex()
        self._on_safety_fault = on_safety_fault

        self._edge_barrier = threading.RLock()
        self._lock = threading.RLock()

        self._epoch = 0
        self._generation = 0
        self._admission_open = False
        self._requires_capture = False
        self._cancellation: CancellationToken | None = None

        self._leases: dict[int, _ActiveLease] = {}
        self._lease_ids = itertools.count(1)

        self._release_uncertain = False
        self._release_uncertain_reason: str | None = None

        self._issued_tokens: dict[int, EvidenceToken] = {}
        self._consumed_sequences: set[int] = set()
        self._last_applied_sequence = -1
        self._pinned_rect: ViewportGeometry | None = None

        self._watchdog: threading.Thread | None = None
        self._watchdog_stop = threading.Event()

    # -- properties -------------------------------------------------------
    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def config(self) -> AuthorityConfig:
        return self._config

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def release_uncertain(self) -> bool:
        with self._lock:
            return self._release_uncertain

    @property
    def release_uncertain_reason(self) -> str | None:
        with self._lock:
            return self._release_uncertain_reason

    def ledger_empty(self) -> bool:
        with self._lock:
            return not self._leases

    def held_targets(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(entry.target for entry in self._leases.values()))

    # -- generation lifecycle --------------------------------------------
    def activate_generation(
        self,
        generation: int,
        *,
        emits_input: bool,
        cancellation: CancellationToken | None = None,
        requires_capture: bool = True,
        pinned_rect: ViewportGeometry | None = None,
    ) -> None:
        """Open a new authority generation.

        Called by the coordinator only after readiness checks pass. Admission
        is opened only for input-emitting modes; Shadow activates a generation
        with ``emits_input=False`` and can never press.
        """
        with self._lock:
            self._epoch += 1
            self._generation = generation
            self._cancellation = cancellation
            self._requires_capture = requires_capture and emits_input
            self._admission_open = emits_input
            self._pinned_rect = pinned_rect
            self._issued_tokens.clear()
            self._consumed_sequences.clear()
            self._last_applied_sequence = -1

    def invalidate(self, reason: str) -> None:
        """Close admission and bump the epoch without emitting edges."""
        with self._edge_barrier, self._lock:
            self._epoch += 1
            self._admission_open = False
            self._cancellation = None
            self._issued_tokens.clear()

    # -- evidence ---------------------------------------------------------
    def register_evidence(self, token: EvidenceToken) -> None:
        """Record a token minted by the capture registry as acceptable.

        Identity registration is what makes forgery pointless: a hand-built
        token with plausible fields is not in this dict.
        """
        with self._lock:
            if token.generation != self._generation:
                return
            self._issued_tokens[id(token)] = token
            # Bound growth: a long Live run must not accumulate every token.
            if len(self._issued_tokens) > 64:
                for key in list(self._issued_tokens)[:-16]:
                    self._issued_tokens.pop(key, None)

    # -- watchdog ---------------------------------------------------------
    def start_watchdog(self) -> None:
        if self._watchdog is not None:
            return
        self._watchdog_stop.clear()
        self._watchdog = threading.Thread(
            target=self._watchdog_loop, name="treasure-safety-watchdog", daemon=True
        )
        self._watchdog.start()

    def stop_watchdog(self, timeout_s: float = 1.0) -> bool:
        self._watchdog_stop.set()
        thread = self._watchdog
        self._watchdog = None
        if thread is None:
            return True
        thread.join(timeout_s)
        return not thread.is_alive()

    @property
    def watchdog_running(self) -> bool:
        thread = self._watchdog
        return thread is not None and thread.is_alive()

    def _watchdog_loop(self) -> None:
        interval_s = self._config.safety_poll_interval_ms / 1000.0
        while not self._watchdog_stop.wait(interval_s):
            try:
                self.poll_safety()
            except Exception as exc:  # a watchdog that dies is worse than a noisy one
                self._emit_fault(SafetyFaultKind.WORKER_ERROR, (f"watchdog:{exc!r}",))

    def poll_safety(self) -> SafetyFault | None:
        """One watchdog pass. Exposed so tests can drive it deterministically.

        Outside an input generation, unhealthy conditions update readiness but
        must not manufacture a releasing fault or invalidate an arm token
        (plan 4.5).
        """
        with self._lock:
            input_generation_live = self._admission_open or bool(self._leases)
            leases = list(self._leases.values())
            requires_capture = self._requires_capture
        now = monotonic_s()

        expired = [entry for entry in leases if entry.handle.expires_at_s <= now]
        if expired:
            fault = SafetyFault(
                generation=self._generation,
                kind=SafetyFaultKind.LEASE_EXPIRED,
                evidence=tuple(f"expired:{entry.target}" for entry in expired),
                observed_at_s=now,
            )
            self._fault_release(fault)
            return fault

        if not input_generation_live:
            return None

        focus = self._safe_call(self._health.focus, None)
        if focus is False:
            return self._fault_release(
                SafetyFault(self._generation, SafetyFaultKind.FOCUS_LOST, ("focus=False",), now)
            )
        if focus is None:
            return self._fault_release(
                SafetyFault(
                    self._generation, SafetyFaultKind.FOCUS_UNKNOWN, ("focus=None",), now
                )
            )

        rect = self._safe_call(self._health.client_rect, None)
        if rect is None or not rect.valid:
            return self._fault_release(
                SafetyFault(
                    self._generation,
                    SafetyFaultKind.VIEWPORT_INVALID,
                    ("viewport=invalid",),
                    now,
                )
            )
        pinned = self._pinned_rect
        if pinned is not None and rect.identity() != pinned.identity():
            return self._fault_release(
                SafetyFault(
                    self._generation,
                    SafetyFaultKind.VIEWPORT_INVALID,
                    (f"viewport-moved:{rect.identity()}",),
                    now,
                )
            )

        if requires_capture:
            age_s = self._safe_call(self._health.capture_age_s, None)
            max_age_s = self._config.max_evidence_age_ms / 1000.0
            if age_s is None or age_s > max_age_s:
                return self._fault_release(
                    SafetyFault(
                        self._generation,
                        SafetyFaultKind.CAPTURE_STALE,
                        (f"capture_age_s={age_s}",),
                        now,
                    )
                )

        if not self._deadman.healthy:
            return self._fault_release(
                SafetyFault(
                    self._generation,
                    SafetyFaultKind.DEADMAN_UNHEALTHY,
                    (str(self._deadman.last_error),),
                    now,
                )
            )
        return None

    @staticmethod
    def _safe_call[T](fn: Callable[[], T], default: T) -> T:
        try:
            return fn()
        except Exception:
            return default

    def _fault_release(self, fault: SafetyFault) -> SafetyFault:
        self.release_all(f"safety:{fault.kind.name}")
        self._emit_fault(fault.kind, fault.evidence, fault.generation)
        return fault

    def _emit_fault(
        self,
        kind: SafetyFaultKind,
        evidence: tuple[str, ...],
        generation: int | None = None,
    ) -> None:
        if self._on_safety_fault is None:
            return
        with contextlib.suppress(Exception):
            self._on_safety_fault(
                SafetyFault(
                    generation=generation if generation is not None else self._generation,
                    kind=kind,
                    evidence=evidence,
                    observed_at_s=monotonic_s(),
                )
            )

    # -- validation -------------------------------------------------------
    def _validate_for_press(self, generation: int) -> str | None:
        """Preconditions for a *new* edge. Caller holds ``_lock``."""
        if generation != self._generation:
            return "stale-generation"
        if not self._admission_open:
            return "admission-closed"
        if self._release_uncertain:
            return "release-uncertain"
        cancellation = self._cancellation
        if cancellation is not None and cancellation.is_cancelled():
            return "cancelled"
        focus = self._safe_call(self._health.focus, None)
        if focus is not True:
            return f"focus={focus}"
        rect = self._safe_call(self._health.client_rect, None)
        if rect is None or not rect.valid:
            return "viewport-invalid"
        pinned = self._pinned_rect
        if pinned is not None and rect.identity() != pinned.identity():
            return "viewport-moved"
        if self._requires_capture:
            age_s = self._safe_call(self._health.capture_age_s, None)
            if age_s is None or age_s > self._config.max_evidence_age_ms / 1000.0:
                return f"capture-stale:{age_s}"
        if not self._deadman.healthy:
            return "deadman-unhealthy"
        return None

    # -- acquisition ------------------------------------------------------
    def acquire_key(
        self, generation: int, key: InputKey, max_hold_ms: int
    ) -> LeaseHandle | None:
        return self._acquire(generation, key=key, button=None, max_hold_ms=max_hold_ms)

    def acquire_button(
        self, generation: int, button: MouseButton, max_hold_ms: int
    ) -> LeaseHandle | None:
        return self._acquire(generation, key=None, button=button, max_hold_ms=max_hold_ms)

    def _acquire(
        self,
        generation: int,
        *,
        key: InputKey | None,
        button: MouseButton | None,
        max_hold_ms: int,
    ) -> LeaseHandle | None:
        """Ordered acquisition (plan 4.5).

        1. validate and create a *pending* lease under the authority lock;
        2. register with the deadman and require a positive ACK (no locks
           held, because that is blocking IO);
        3. enter the native-edge barrier;
        4. revalidate everything after the ACK and immediately before the edge;
        5. emit the native down and atomically commit;
        6. on any failure or epoch mismatch, emit an unconditional up, tell the
           deadman to forget, and roll back.
        """
        horizon_ms = min(max_hold_ms, self._config.max_hold_ms)
        with self._lock:
            reason = self._validate_for_press(generation)
            if reason is not None:
                return None
            epoch = self._epoch
            lease_id = next(self._lease_ids)
            now = monotonic_s()
            handle = LeaseHandle(
                lease_id=lease_id,
                generation=generation,
                key=key,
                button=button,
                acquired_at_s=now,
                expires_at_s=now + horizon_ms / 1000.0,
            )
            target = handle.describe()
            self._leases[lease_id] = _ActiveLease(handle=handle, target=target, committed=False)

        # (2) deadman ACK before any down-edge - outside the locks.
        deadman_horizon = min(horizon_ms, self._config.max_rolling_lease_horizon_ms)
        if not self._deadman.register(lease_id, generation, target, deadman_horizon):
            with self._lock:
                self._leases.pop(lease_id, None)
            return None

        with self._edge_barrier:
            with self._lock:
                if epoch != self._epoch or self._validate_for_press(generation) is not None:
                    self._leases.pop(lease_id, None)
                    rollback = True
                else:
                    rollback = False
            if rollback:
                self._emit_up(key, button)  # unconditional; may be a no-op
                self._deadman.forget(lease_id)
                return None
            try:
                self._emit_down(key, button)
            except Exception:
                with self._lock:
                    self._leases.pop(lease_id, None)
                self._emit_up(key, button)
                self._deadman.forget(lease_id)
                return None
            with self._lock:
                entry = self._leases.get(lease_id)
                if entry is None or epoch != self._epoch:
                    # Stop landed inside the barrier: undo immediately.
                    self._leases.pop(lease_id, None)
                    committed = False
                else:
                    entry.committed = True
                    committed = True
            if not committed:
                self._emit_up(key, button)
                self._deadman.forget(lease_id)
                return None
        return handle

    def _emit_down(self, key: InputKey | None, button: MouseButton | None) -> None:
        if key is not None:
            self._port.raw_key_down(self._port.key_code(key))
        elif button is not None:
            self._port.raw_button_down(button)

    def _emit_up(self, key: InputKey | None, button: MouseButton | None) -> None:
        if key is not None:
            self._port.raw_key_up(self._port.key_code(key))
        elif button is not None:
            self._port.raw_button_up(button)

    # -- renewal ----------------------------------------------------------
    def renew(self, generation: int, lease: LeaseHandle, horizon_ms: int) -> bool:
        """Non-additive renewal (plan 4.5).

        Expiry becomes ``now + min(requested, max_rolling_horizon)`` measured
        from the moment of the *latest acknowledged health check*, so repeated
        renewals can never walk the deadline forward indefinitely.
        """
        with self._lock:
            entry = self._leases.get(lease.lease_id)
            if entry is None or not entry.committed:
                return False
            if self._validate_for_press(generation) is not None:
                return False
            epoch = self._epoch
        capped_ms = min(horizon_ms, self._config.max_rolling_lease_horizon_ms)
        if not self._deadman.renew(lease.lease_id, generation, capped_ms):
            return False
        with self._edge_barrier, self._lock:
            entry = self._leases.get(lease.lease_id)
            if entry is None or epoch != self._epoch:
                self._deadman.forget(lease.lease_id)
                return False
            if self._validate_for_press(generation) is not None:
                return False
            entry.handle = replace(
                entry.handle, expires_at_s=monotonic_s() + capped_ms / 1000.0
            )
            return True

    # -- release ----------------------------------------------------------
    def release_lease(self, generation: int, lease: LeaseHandle) -> None:
        del generation  # a release is never generation- or focus-gated
        with self._edge_barrier:
            with self._lock:
                entry = self._leases.pop(lease.lease_id, None)
            if entry is not None:
                try:
                    self._emit_up(entry.handle.key, entry.handle.button)
                except Exception:
                    self._latch_uncertain(f"release-failed:{entry.target}")
            self._deadman.forget(lease.lease_id)

    def release_all(self, reason: str = "stop") -> ReleaseReport:
        """The complete, idempotent, failure-isolated release floor (plan 4.4)."""
        attempted: list[str] = []
        failures: list[str] = []
        with self._edge_barrier:
            with self._lock:
                self._epoch += 1
                self._admission_open = False
                entries = list(self._leases.values())
                self._leases.clear()
                self._issued_tokens.clear()

            # (2) active leases first, each failure recorded separately.
            for entry in entries:
                attempted.append(entry.target)
                try:
                    self._emit_up(entry.handle.key, entry.handle.button)
                except Exception as exc:
                    failures.append(f"{entry.target}:{exc!r}")

            # (3) the full vocabulary, unconditionally, even after a failure.
            vocabulary = self._port.vocabulary
            for key in vocabulary.keys:
                name = key.value
                attempted.append(name)
                try:
                    self._port.raw_key_up(self._port.key_code(key))
                except Exception as exc:
                    failures.append(f"{name}:{exc!r}")
            for button in vocabulary.buttons:
                name = f"mouse:{button.value}"
                attempted.append(name)
                try:
                    self._port.raw_button_up(button)
                except Exception as exc:
                    failures.append(f"{name}:{exc!r}")

            # (4) deadman release-all with a required positive ACK.
            deadman_ok = False
            try:
                deadman_ok = self._deadman.release_all(reason)
            except Exception as exc:
                failures.append(f"deadman:{exc!r}")

            with self._lock:
                ledger_empty = not self._leases
                safe = ledger_empty and not failures and deadman_ok
                if not safe:
                    self._release_uncertain = True
                    self._release_uncertain_reason = (
                        f"{reason}: failures={failures or 'none'} "
                        f"deadman_ack={deadman_ok} ledger_empty={ledger_empty}"
                    )
                report = ReleaseReport(
                    attempted_edges=tuple(attempted),
                    failures=tuple(failures),
                    deadman_acknowledged=deadman_ok,
                    ledger_empty=ledger_empty,
                    release_known_safe=safe and not self._release_uncertain,
                    reason=reason,
                )
        return report

    def _latch_uncertain(self, reason: str) -> None:
        with self._lock:
            self._release_uncertain = True
            self._release_uncertain_reason = reason

    def latch_release_uncertain(self, reason: str) -> None:
        """Adopt an unsafe-release latch from outside - e.g. a record left by a
        previous run that ended without a confirmed release (plan 4.4)."""
        self._latch_uncertain(reason)

    def recover_release(self) -> ReleaseReport:
        """Release-only recovery handshake that can clear the uncertain latch.

        It emits *only* up-edges. If everything succeeds and the deadman ACKs,
        the latch clears and Live becomes startable again; otherwise the latch
        stays and Live stays blocked (plan 4.4).
        """
        with self._lock:
            self._release_uncertain = False
            self._release_uncertain_reason = None
        report = self.release_all("release-recovery")
        if not report.release_known_safe:
            self._latch_uncertain(f"recovery-failed:{report.failures}")
        return report

    # -- service-level primitives ----------------------------------------
    def tap_key(self, generation: int, key: InputKey, hold_ms: int) -> bool:
        lease = self.acquire_key(generation, key, max(1, hold_ms))
        if lease is None:
            return False
        cancellation = self._cancellation
        deadline_s = hold_ms / 1000.0
        if cancellation is not None:
            cancellation.wait(deadline_s)
        else:
            threading.Event().wait(deadline_s)
        self.release_lease(generation, lease)
        return True

    def tap_button(self, generation: int, button: MouseButton, hold_ms: int) -> bool:
        lease = self.acquire_button(generation, button, max(1, hold_ms))
        if lease is None:
            return False
        cancellation = self._cancellation
        deadline_s = hold_ms / 1000.0
        if cancellation is not None:
            cancellation.wait(deadline_s)
        else:
            threading.Event().wait(deadline_s)
        self.release_lease(generation, lease)
        return True

    def pointer_move_client(self, generation: int, point_px: tuple[int, int]) -> bool:
        with self._lock:
            rect = self._pinned_rect or self._safe_call(self._health.client_rect, None)
            if rect is None or not rect.valid:
                return False
            width, height = rect.canonical_px
            if not (0 <= point_px[0] < width and 0 <= point_px[1] < height):
                return False
        return self._guarded_edge(
            generation, lambda: self._port.raw_pointer_move_client(point_px)
        )

    def pointer_delta(self, generation: int, dx: int, dy: int) -> bool:
        """Bounded relative motion, tagged with any button the ledger holds.

        The ledger is the only place that knows a button is down (bug B8), so
        the drag context is supplied here rather than tracked in the port.
        """
        bounded_dx = max(-512, min(512, int(dx)))
        bounded_dy = max(-512, min(512, int(dy)))
        with self._lock:
            held_button = next(
                (
                    entry.handle.button
                    for entry in self._leases.values()
                    if entry.handle.button is not None and entry.committed
                ),
                None,
            )
        return self._guarded_edge(
            generation,
            lambda: self._port.raw_pointer_delta(bounded_dx, bounded_dy, held_button),
        )

    def scroll_lines(self, generation: int, lines: int) -> bool:
        bounded = max(-32, min(32, int(lines)))
        return self._guarded_edge(generation, lambda: self._port.raw_scroll_lines(bounded))

    def _guarded_edge(self, generation: int, emit: Callable[[], None]) -> bool:
        """Pointer moves and scrolls use the same barrier and pre-edge checks."""
        with self._lock:
            if self._validate_for_press(generation) is not None:
                return False
            epoch = self._epoch
        with self._edge_barrier:
            with self._lock:
                if epoch != self._epoch or self._validate_for_press(generation) is not None:
                    return False
            try:
                emit()
            except Exception:
                return False
        return True

    # -- navigation -------------------------------------------------------
    def apply_navigation_command(
        self, generation: int, command: NavigationCommand, evidence: EvidenceToken
    ) -> NavigationApplyResult:
        """The sole navigation input path.

        Validates the token *by identity* against what the capture registry
        registered, checks provenance and freshness independently of the
        worker, then translates the accepted axes into bounded leases.
        """
        now = monotonic_s()
        with self._lock:
            if generation != self._generation or command.generation != self._generation:
                return NavigationApplyResult(
                    NavigationApplyStatus.REJECTED_GENERATION, "stale generation"
                )
            token = self._issued_tokens.get(id(evidence))
            if token is not evidence:
                return NavigationApplyResult(
                    NavigationApplyStatus.REJECTED_EVIDENCE,
                    "token not issued by this authority",
                )
            if token.run_id != self._run_id or token.generation != self._generation:
                return NavigationApplyResult(
                    NavigationApplyStatus.REJECTED_EVIDENCE, "token provenance mismatch"
                )
            if token.frame_sequence != command.source_frame_sequence:
                return NavigationApplyResult(
                    NavigationApplyStatus.REJECTED_EVIDENCE, "token/command frame mismatch"
                )
            if token.captured_at_s != command.source_captured_at_s:
                return NavigationApplyResult(
                    NavigationApplyStatus.REJECTED_EVIDENCE, "token/command timestamp mismatch"
                )
            if token.duration_ms > self._config.max_capture_duration_ms:
                return NavigationApplyResult(
                    NavigationApplyStatus.REJECTED_EVIDENCE, "capture over duration budget"
                )
            if token.frame_sequence <= self._last_applied_sequence:
                return NavigationApplyResult(
                    NavigationApplyStatus.REJECTED_EVIDENCE, "frame sequence not strictly newer"
                )
            max_age_s = self._config.max_evidence_age_ms / 1000.0
            if now - token.captured_at_s > max_age_s:
                return NavigationApplyResult(
                    NavigationApplyStatus.REJECTED_EVIDENCE, "evidence older than budget"
                )
            if command.valid_until_s > token.captured_at_s + max_age_s:
                return NavigationApplyResult(
                    NavigationApplyStatus.REJECTED_EVIDENCE,
                    "command lease exceeds evidence age",
                )
            if now > command.valid_until_s:
                return NavigationApplyResult(
                    NavigationApplyStatus.REJECTED_EVIDENCE, "command already expired"
                )
            pinned = self._pinned_rect
            if pinned is not None and token.viewport_identity != pinned.identity():
                return NavigationApplyResult(
                    NavigationApplyStatus.REJECTED_VIEWPORT, "token viewport identity mismatch"
                )
            reason = self._validate_for_press(generation)
            if reason is not None:
                status = {
                    "cancelled": NavigationApplyStatus.REJECTED_CANCELLED,
                    "viewport-invalid": NavigationApplyStatus.REJECTED_VIEWPORT,
                    "viewport-moved": NavigationApplyStatus.REJECTED_VIEWPORT,
                }.get(reason)
                if status is None:
                    status = (
                        NavigationApplyStatus.REJECTED_FOCUS
                        if reason.startswith("focus=")
                        else NavigationApplyStatus.REJECTED_HEALTH
                    )
                return NavigationApplyResult(status, reason)
            self._last_applied_sequence = token.frame_sequence
            self._consumed_sequences.add(token.frame_sequence)

        return self._translate_navigation(generation, command)

    def _translate_navigation(
        self, generation: int, command: NavigationCommand
    ) -> NavigationApplyResult:
        """Axes -> leases. Anything not commanded this tick is released.

        A command is **APPLIED only if every required edge succeeded**. If a
        forward lease could not be taken, the yaw that would have accompanied
        it is not emitted either: turning while believing the character is
        walking is a worse state than doing nothing, and reporting success for
        a half-applied command is how the two get confused.
        """
        horizon_ms = min(
            self._config.max_rolling_lease_horizon_ms,
            max(1, int((command.valid_until_s - monotonic_s()) * 1000)),
        )
        wanted: dict[str, InputKey] = {}
        if command.forward_axis == 1:
            wanted["w"] = InputKey.W
        elif command.forward_axis == -1:
            wanted["s"] = InputKey.S
        if command.lateral_axis == -1:
            wanted["a"] = InputKey.A
        elif command.lateral_axis == 1:
            wanted["d"] = InputKey.D
        if command.jump:
            wanted["space"] = InputKey.SPACE

        with self._lock:
            current = {entry.target: entry.handle for entry in self._leases.values()}

        for target, handle in current.items():
            if target not in wanted:
                self.release_lease(generation, handle)

        held: list[str] = []
        failed: list[str] = []
        for target, key in wanted.items():
            existing = current.get(target)
            if existing is not None:
                if self.renew(generation, existing, horizon_ms):
                    held.append(target)
                    continue
                self.release_lease(generation, existing)
            lease = self.acquire_key(generation, key, horizon_ms)
            if lease is None:
                failed.append(target)
            else:
                held.append(target)

        if failed:
            # Release what did land, so the character is not left holding half
            # a command, and say so rather than reporting a success.
            self.release_all(f"navigation:lease-failed:{','.join(sorted(failed))}")
            return NavigationApplyResult(
                NavigationApplyStatus.REJECTED_HEALTH,
                f"could not acquire {', '.join(sorted(failed))}; released and stopped",
            )

        if command.yaw_delta_px and not self.pointer_delta(generation, command.yaw_delta_px, 0):
            self.release_all("navigation:yaw-edge-failed")
            return NavigationApplyResult(
                NavigationApplyStatus.REJECTED_HEALTH,
                "the yaw edge was refused; released and stopped",
            )

        return NavigationApplyResult(
            NavigationApplyStatus.APPLIED, command.reason, leases_held=tuple(sorted(held))
        )

    def release_navigation(self, generation: int, reason: str) -> ReleaseReport:
        del generation
        return self.release_all(f"navigation:{reason}")

    # -- session factories ------------------------------------------------
    def navigation_session(self, generation: int) -> NavigationInputSession:
        return NavigationInputSession(self, generation)

    def service_session(self, generation: int) -> ServiceInputSession:
        return ServiceInputSession(self, generation)

    # -- diagnostics ------------------------------------------------------
    def describe_readiness(self) -> dict[str, str]:
        focus = self._safe_call(self._health.focus, None)
        rect = self._safe_call(self._health.client_rect, None)
        age_s = self._safe_call(self._health.capture_age_s, None)
        with self._lock:
            ledger = (
                "empty"
                if not self._leases
                else ",".join(sorted(entry.target for entry in self._leases.values()))
            )
            return {
                "focus": {True: "ok", False: "not-focused", None: "unknown"}[focus],
                "viewport": "ok" if rect is not None and rect.valid else "invalid",
                "capture_age_ms": "unknown" if age_s is None else f"{age_s * 1000:.0f}",
                "deadman": "ok" if self._deadman.healthy else "unhealthy",
                "watchdog": "ok" if self.watchdog_running else "stopped",
                "ledger": ledger,
                "release": "uncertain" if self._release_uncertain else "known-safe",
            }


def vocabulary_targets(
    keys: Iterable[InputKey], buttons: Iterable[MouseButton]
) -> tuple[str, ...]:
    """Stable target names shared by the authority, the deadman, and the logs."""
    return tuple([key.value for key in keys] + [f"mouse:{b.value}" for b in buttons])
