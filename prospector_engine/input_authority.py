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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from prospector_engine.contracts import (
    CancellationToken,
    EvidenceToken,
    FocusState,
    InputKey,
    LeaseHandle,
    MouseButton,
    NavigationCommand,
    Provenance,
    ReleaseReport,
    SafetyFault,
    SafetyFaultKind,
    monotonic_s,
)
from prospector_engine.geometry import ViewportGeometry
from prospector_engine.lifecycle import LifecycleJournal, LifecycleStage
from prospector_engine.movement import (
    DesiredMovement,
    MovementActuator,
    MovementBlock,
    MovementLimits,
    MovementOutcome,
    desired_from_command,
)
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
    #: The watchdog bound on one held edge, and - since D-063 - the horizon a
    #: navigation lease is actually granted. It used to be a ceiling that
    #: nothing reached: the horizon was derived from how much of the *evidence*
    #: budget a frame had left, so a command built from a 70 ms-old frame asked
    #: for a 30 ms lease, the next frame arrived after 33 ms, the lease had
    #: already expired, the watchdog lifted the key and the following command
    #: pressed it again. A continuous hold came out as a rattle, and the
    #: rattle was in the timing, not in the Quartz call.
    max_rolling_lease_horizon_ms: int = 250
    max_hold_ms: int = 2000
    #: How stale a frame may be and still *authorize a new decision*. This is
    #: an evidence rule and it has not changed: a command is still built from
    #: one frame no older than this, and still may not outlive it.
    max_evidence_age_ms: int = 100
    #: How long a *physical hold already in progress* may survive with no
    #: fresh frame at all before everything is released. Deliberately larger
    #: than the evidence budget and equal to the lease horizon, because these
    #: are different questions: "may this frame decide something" is not "must
    #: the key come up now". One late, dropped or slowly-rendered frame is not
    #: a safety event; a quarter second of blindness is.
    max_capture_stall_ms: int = 250
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
            note=(
                "engineering budget; E-PERF and per-OS release gates are PENDING. "
                "max_capture_stall_ms is a chosen bound equal to the plan's own "
                "max_rolling_lease_horizon_ms, not a measurement (D-063)."
            ),
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

    :meth:`move` is the whole interface: the caller says what it wants held and
    the :class:`~prospector_engine.movement.MovementActuator` makes the keyboard
    match. There is no generic hold/tap/pointer method here.

    **What replaced ``apply_navigation_command``, and why (D-067).** That method
    validated an authority-issued ``EvidenceToken`` by object identity and then
    ten further properties of it - run id, generation, frame sequence, capture
    timestamp, capture *duration*, strict ordering, two age budgets, viewport
    identity - before a key could go down, and re-ran a nine-condition
    ``_validate_for_press`` twice around a synchronous round-trip to a separate
    process. All of it per frame, per key.

    It never once succeeded on the owner's machine. Not "succeeded and the
    character did not move": ``OS_EDGE_POSTED`` - recorded the instant
    ``CGEventPost`` returns - never appeared in a single runtime trace. The
    Quartz call underneath was measured to work perfectly with an inert
    keycode, so the whole of the failure was in the admission test above it.

    The safety that mattered is kept, and it is kept where it belongs: as
    conditions that *release*, checked by an independent watchdog thread, rather
    than as conditions that refuse a press. See :mod:`prospector_engine.movement`.
    """

    def __init__(self, authority: InputAuthority, generation: int) -> None:
        self._authority = authority
        self._generation = generation

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def evidence_budget_s(self) -> float:
        """How stale a frame may be and still justify a *decision*.

        Still meaningful, and still enforced - by the navigator, which refuses
        to steer on a stale frame. It is no longer a precondition on the
        existence of a key edge, because a key that is already down does not
        become dangerous when the next screenshot is slow.
        """
        return self._authority.config.max_evidence_age_ms / 1000.0

    @property
    def movement(self) -> MovementActuator:
        return self._authority.movement

    @property
    def current(self) -> bool:
        """Whether this session still belongs to the running mode.

        One comparison of two integers, taken once per call. It is deliberately
        **not** the machinery D-067 removed: that validated an evidence token
        by object identity and then ten properties of the *frame* - sequence,
        capture instant, capture duration, two age budgets, viewport identity -
        on every key, every tick, and a healthy 55 fps pipeline could not pass
        it. This asks a different question, about the *session* rather than
        about a frame: is the worker holding me the worker the coordinator is
        running? That answer changes only on a mode transition, never on a slow
        screenshot, so it cannot refuse a press for being late.
        """
        return self._generation == self._authority.generation

    def move(self, desired: DesiredMovement) -> MovementOutcome:
        """Make the keyboard match ``desired``. Level-triggered; see D-067.

        A session from a superseded generation presses nothing. A cancelled
        worker can outlive its cancellation by one tick - it is blocked in a
        native screen grab when the transition happens - and without this it
        would land that tick's keys on top of the mode that replaced it.
        """
        if not self.current:
            return MovementOutcome(
                held=self._authority.movement.held,
                block=MovementBlock.STOPPED,
                detail=f"session generation {self._generation} has been superseded",
            )
        return self._authority.movement.apply(desired)

    def apply_command(self, command: NavigationCommand | None) -> MovementOutcome:
        """``move`` for callers that already hold a :class:`NavigationCommand`."""
        return self.move(desired_from_command(command))

    def stop_moving(self, reason: str) -> tuple[InputKey, ...]:
        """Let go of the movement keys. Does **not** disarm the session.

        The ordinary "stop walking" verb: an arrival candidate, a deadband
        hold, one occluded frame. It lifts what is held and nothing else, so
        the next frame can press again - which is the whole difference between
        this and the release path it replaces (D-067).

        A superseded session lifts nothing: the keys it might have been holding
        were released by the transition that superseded it, and anything down
        now belongs to the mode that replaced it.
        """
        if not self.current:
            return ()
        return self._authority.movement.release_held(reason)

    def release_navigation(self, reason: str) -> ReleaseReport:
        """The full floor. For worker exit and safety, not for ordinary stops."""
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
        lifecycle: LifecycleJournal | None = None,
        movement_limits: MovementLimits | None = None,
        narrate: Callable[[str, str], None] | None = None,
    ) -> None:
        self._port = port
        self._deadman = deadman
        self._health = health
        self._config = config or AuthorityConfig()
        self._run_id = run_id or os.urandom(8).hex()
        self._on_safety_fault = on_safety_fault
        # Optional so tests and the deadman path need no journal. When one
        # is present, the authority is the only thing that can honestly say
        # an OS edge went out and a lease is held, so it says both.
        self._lifecycle = lifecycle or LifecycleJournal()

        self._edge_barrier = threading.RLock()
        self._lock = threading.RLock()

        self._epoch = 0
        self._generation = 0
        self._admission_open = False
        self._requires_capture = False
        self._cancellation: CancellationToken | None = None

        self._leases: dict[int, _ActiveLease] = {}
        self._lease_ids = itertools.count(1)
        #: target -> when its lease last ended. A hold that is meant to be
        #: continuous and is re-pressed instead is the difference between
        #: walking and shuffling, and it is invisible unless counted.
        self._released_at_s: dict[str, float] = {}
        self._hold_lapses: dict[str, int] = {}
        #: target -> when its *current, unbroken* hold began. A renewal never
        #: touches it, so this is the physical hold duration and not the age
        #: of the last lease object.
        self._held_since_s: dict[str, float] = {}
        #: Every OS edge this run posted, so "one down, one up" is a count and
        #: not an impression.
        self._down_edges = 0
        self._up_edges = 0
        self._last_yaw_px = 0
        self._last_yaw_at_s = 0.0

        self._release_uncertain = False
        self._release_uncertain_reason: str | None = None

        self._issued_tokens: dict[int, EvidenceToken] = {}
        self._consumed_sequences: set[int] = set()
        self._last_applied_sequence = -1
        self._pinned_rect: ViewportGeometry | None = None

        self._watchdog: threading.Thread | None = None
        self._watchdog_stop = threading.Event()
        #: How many safety polls read an ambiguous frontmost state. Counted
        #: rather than acted on, so "the focus probe is flaky" stays a visible
        #: fact without being a reason to stop.
        self._unknown_focus_polls = 0

        #: The navigation actuator. It shares this object's port, helper,
        #: focus probe and journal, so there is still exactly one thing that
        #: can press a key and exactly one release floor - but the *movement*
        #: path through it is Lite's proven one rather than the evidence
        #: machinery that never posted an edge (D-067).
        self._movement = MovementActuator(
            port,
            deadman=deadman,
            focus_probe=lambda: self._safe_call(self._health.focus, None),
            journal=self._lifecycle,
            narrate=narrate,
            limits=movement_limits,
        )

    # -- properties -------------------------------------------------------
    @property
    def movement(self) -> MovementActuator:
        """The navigation actuator. Services do not go through it."""
        return self._movement

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def lifecycle(self) -> LifecycleJournal:
        """The named-stage record. The authority is the only honest source for
        ``OS_EDGE_POSTED``, ``LEASE_HELD`` and the release stages."""
        return self._lifecycle

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
        """Nothing held, by *either* path. Services lease; navigation actuates."""
        with self._lock:
            leases_empty = not self._leases
        return leases_empty and self._movement.empty

    def held_targets(self) -> tuple[str, ...]:
        """Everything down right now, from both the lease ledger and the actuator."""
        with self._lock:
            leased = {entry.target for entry in self._leases.values()}
        return tuple(sorted(leased | set(self._movement.held_targets)))

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
        # The actuator arms with the generation and disarms with it. Shadow
        # activates with ``emits_input=False`` and therefore cannot press,
        # which is the same guarantee the ``NoInputSession`` gives - now
        # enforced in the one object that owns the keys as well.
        if emits_input:
            self._movement.arm(f"generation {generation}")
        else:
            self._movement.disarm(f"observing at generation {generation}")

    def invalidate(self, reason: str) -> None:
        """Close admission and bump the epoch. Disarms the actuator with it."""
        with self._edge_barrier, self._lock:
            self._epoch += 1
            self._admission_open = False
            self._cancellation = None
            self._issued_tokens.clear()
        self._movement.disarm(reason)

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
        self._movement.start_watchdog()
        if self._watchdog is not None:
            return
        self._watchdog_stop.clear()
        self._watchdog = threading.Thread(
            target=self._watchdog_loop, name="treasure-safety-watchdog", daemon=True
        )
        self._watchdog.start()

    def stop_watchdog(self, timeout_s: float = 1.0) -> bool:
        movement_stopped = self._movement.stop_watchdog(timeout_s)
        self._watchdog_stop.set()
        thread = self._watchdog
        self._watchdog = None
        if thread is None:
            return movement_stopped
        thread.join(timeout_s)
        return movement_stopped and not thread.is_alive()

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

        # Positive focus loss releases at once: another application is in
        # front and our keys must not land in it.
        focus = self._safe_call(self._health.focus, None)
        if focus is False:
            return self._fault_release(
                SafetyFault(self._generation, SafetyFaultKind.FOCUS_LOST, ("focus=False",), now)
            )
        # An *unknown* reading does not, and this is the second half of the
        # asymmetry ``MovementActuator._blocking_condition`` states: only a
        # positive "another application is frontmost" may stop input, and a
        # release is never gated on this probe at all.
        #
        # Two owners disagreeing about one word is how a Live run became a
        # zombie. macOS's frontmost probe is a window-list scan that returns
        # ``None`` on any error or ambiguity - a Space change, a scan racing a
        # window update. The actuator treats that as "carry on"; this watchdog
        # treated it as a terminal fault, called the full release floor, and
        # disarmed the actuator the actuator believed was fine. Whichever ran
        # first won, which is not a safety property, it is a race.
        #
        # ``FOCUS_UNKNOWN`` therefore no longer releases. It is still a fault
        # kind, still raised by anything that positively knows better, and
        # still reported by ``describe_readiness``.
        if focus is None:
            self._unknown_focus_polls += 1

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
            # The *stall* budget, not the evidence budget. The watchdog is
            # asking "must everything come up now", which is a different
            # question from "may this frame authorize a new command" - and
            # answering both with 100 ms is what turned one late frame into a
            # released key and a safe stop (D-063).
            age_s = self._safe_call(self._health.capture_age_s, None)
            max_age_s = self._config.max_capture_stall_ms / 1000.0
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
            # Guards the *existence* of an edge - a new down, or a renewal of
            # one already down - so it uses the stall budget. The evidence rule
            # for a new decision is enforced separately and unchanged, in
            # ``apply_navigation_command``.
            age_s = self._safe_call(self._health.capture_age_s, None)
            if age_s is None or age_s > self._config.max_capture_stall_ms / 1000.0:
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
            except Exception as exc:
                with self._lock:
                    self._leases.pop(lease_id, None)
                self._emit_up(key, button)
                self._deadman.forget(lease_id)
                self._lifecycle.note(
                    LifecycleStage.OS_EDGE_POSTED,
                    f"the post call raised for {target}",
                    target=target,
                    posted=False,
                    error=repr(exc),
                )
                return None
            # The post call returned. That is *all* this stage means: it is not
            # evidence that anything received the event, and the loopback and
            # motion stages exist because it is not.
            self._lifecycle.note(
                LifecycleStage.OS_EDGE_POSTED, target, target=target, posted=True
            )
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
        self._lifecycle.note(
            LifecycleStage.LEASE_HELD, target, target=target, lease_id=lease_id
        )
        # The physical hold starts here and is *not* touched by renewal, so
        # ``held_since_s`` measures the key being down rather than the age of
        # whichever lease object currently covers it.
        self._held_since_s[target] = monotonic_s()
        self._note_press(target, now)
        return handle

    #: A re-press this soon after a release is a hold that lapsed, not a new
    #: intention. Comfortably longer than one frame at the slowest cadence the
    #: navigator will steer at, so an ordinary stop-and-start is not counted.
    HOLD_LAPSE_WINDOW_S = 0.5

    def _note_press(self, target: str, now_s: float) -> None:
        """Count a down edge that re-presses a key which had just been held.

        The lease window a single command may ask for is the evidence budget
        minus the age the frame already had. If frames arrive further apart
        than that, the lease expires before its renewal arrives, the watchdog
        lifts the key, and the next command presses it again - so a hold that
        is meant to be continuous comes out as a rattle. That is not something
        to fix by lengthening the lease, which is a safety bound; it is
        something to *see*, so it is counted and named here.
        """
        released = self._released_at_s.pop(target, None)
        if released is None or now_s - released > self.HOLD_LAPSE_WINDOW_S:
            return
        with self._lock:
            self._hold_lapses[target] = self._hold_lapses.get(target, 0) + 1
            count = self._hold_lapses[target]
        self._lifecycle.note(
            LifecycleStage.HOLD_LAPSED,
            f"{target} was re-pressed {(now_s - released) * 1000.0:.0f} ms after it came up",
            target=target,
            gap_ms=round((now_s - released) * 1000.0, 1),
            lapses=count,
        )

    @property
    def hold_lapses(self) -> Mapping[str, int]:
        """Per target, how often a continuous hold came up and was re-pressed."""
        with self._lock:
            return dict(self._hold_lapses)

    def describe_holds(self) -> str:
        lapses = self.hold_lapses
        if not lapses:
            return "no held key has lapsed"
        worst = ", ".join(f"{target} x{count}" for target, count in sorted(lapses.items()))
        return f"re-pressed after lapsing: {worst}"

    def _emit_down(self, key: InputKey | None, button: MouseButton | None) -> None:
        self._down_edges += 1
        if key is not None:
            self._port.raw_key_down(self._port.key_code(key))
        elif button is not None:
            self._port.raw_button_down(button)

    def _emit_up(self, key: InputKey | None, button: MouseButton | None) -> None:
        self._up_edges += 1
        if key is not None:
            self._port.raw_key_up(self._port.key_code(key))
        elif button is not None:
            self._port.raw_button_up(button)

    # -- what is physically happening -------------------------------------
    def edge_counts(self) -> tuple[int, int]:
        """Down and up edges posted this run, across both paths."""
        moved_down, moved_up = self._movement.edge_counts
        return (self._down_edges + moved_down, self._up_edges + moved_up)

    def last_yaw(self) -> tuple[int, float]:
        """The last relative yaw actually posted, and when.

        Whichever path posted it more recently: services steer the pointer
        directly, navigation goes through the actuator.
        """
        moved_px, moved_at = self._movement.last_yaw
        if moved_at >= self._last_yaw_at_s:
            return (moved_px, moved_at)
        return (self._last_yaw_px, self._last_yaw_at_s)

    def held_since_s(self, target: str, now_s: float) -> float:
        """How long ``target`` has been *continuously* down. Zero if it is not."""
        for key in InputKey:
            if key.value == target:
                held = self._movement.held_since_s(key, now_s)
                if held > 0.0:
                    return held
                break
        began = self._held_since_s.get(target)
        return 0.0 if began is None else max(0.0, now_s - began)

    def forward_held_s(self, now_s: float) -> float:
        """How long forward has been continuously down, in seconds."""
        return self.held_since_s(InputKey.W.value, now_s)

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
                held_ms = self.held_since_s(entry.target, monotonic_s()) * 1000.0
                self._held_since_s.pop(entry.target, None)
                try:
                    self._emit_up(entry.handle.key, entry.handle.button)
                    self._released_at_s[entry.target] = monotonic_s()
                    if entry.handle.key is InputKey.W:
                        self._lifecycle.note(
                            LifecycleStage.W_HOLD_CONFIRMED,
                            f"forward was down for {held_ms:.0f} ms",
                            target=entry.target,
                            held_ms=round(held_ms, 1),
                        )
                    self._lifecycle.note(
                        LifecycleStage.W_RELEASE_POSTED,
                        entry.target,
                        target=entry.target,
                        held_ms=round(held_ms, 1),
                    )
                except Exception:
                    self._latch_uncertain(f"release-failed:{entry.target}")
            self._deadman.forget(lease.lease_id)

    def release_all(self, reason: str = "stop") -> ReleaseReport:
        """The complete, idempotent, failure-isolated release floor (plan 4.4).

        The movement actuator is lifted **first** and by name, so a Stop that
        arrives while the navigator is holding W releases it through the object
        that knows it is held, before the blind vocabulary sweep below runs.
        Both happen; the sweep is the floor under the floor.
        """
        attempted: list[str] = []
        failures: list[str] = []
        movement_held = self._movement.held_targets
        # Disarm, not merely release: the actuator has its own armed flag and
        # never consults ``_admission_open``, so a Stop that only lifted its
        # keys would leave the very next frame free to press again. ``disarm``
        # uses ``release_held`` rather than the actuator's own vocabulary
        # sweep, because the sweep below already covers every movement key and
        # doing it twice doubles the edge count the stop-latency bound is
        # measured in.
        self._movement.disarm(reason)
        attempted.extend(movement_held)
        if self._movement.release_uncertain:
            failures.append(f"movement:{self._movement.release_uncertain_reason}")
        with self._edge_barrier:
            with self._lock:
                self._epoch += 1
                # This *is* a disarm, and it must stay one: a Stop racing a
                # press has to stop the press, and closing admission plus
                # bumping the epoch is what makes the in-flight acquire roll
                # back instead of landing.
                #
                # The bug (D-067) was never that ``release_all`` disarms. It
                # was that ``release_navigation`` - which the navigator calls
                # on every ordinary "stop walking": an arrival candidate, a
                # deadband hold, one occluded frame - was wired to it. Since
                # ``_admission_open`` is set True in exactly one place,
                # ``activate_generation``, and only on a mode transition, the
                # first benign stop muted the session for good. Ordinary stops
                # now go to ``NavigationInputSession.stop_moving``, which lifts
                # the keys and leaves the session able to press again.
                self._admission_open = False
                entries = list(self._leases.values())
                self._leases.clear()
                self._issued_tokens.clear()

            # (2) active leases first, each failure recorded separately.
            for entry in entries:
                attempted.append(entry.target)
                held_ms = self.held_since_s(entry.target, monotonic_s()) * 1000.0
                self._held_since_s.pop(entry.target, None)
                try:
                    self._emit_up(entry.handle.key, entry.handle.button)
                    self._released_at_s[entry.target] = monotonic_s()
                    if entry.handle.key is InputKey.W:
                        self._lifecycle.note(
                            LifecycleStage.W_HOLD_CONFIRMED,
                            f"forward was down for {held_ms:.0f} ms",
                            target=entry.target,
                            held_ms=round(held_ms, 1),
                        )
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
        self._lifecycle.note(
            LifecycleStage.W_RELEASE_POSTED,
            f"release-all: {reason}",
            edges=len(attempted),
            failures=len(failures),
        )
        if ledger_empty:
            self._lifecycle.note(
                LifecycleStage.LEDGER_EMPTY,
                reason,
                deadman_acknowledged=deadman_ok,
                failures=len(failures),
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
        # The horizon carries a poll-interval margin on top of the intended
        # hold: ``expires_at_s`` is stamped before the deadman ACK/native-edge
        # round trip below, so a horizon equal to ``hold_ms`` raced that fixed
        # overhead on every tap and the watchdog eventually caught a lease
        # that was already released, just not yet recorded as such.
        horizon_ms = max(1, hold_ms) + self._config.safety_poll_interval_ms
        lease = self.acquire_key(generation, key, horizon_ms)
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
        # See tap_key: horizon needs margin over the intended hold, not to
        # equal it, or the watchdog's periodic poll can catch a lease whose
        # release is merely running slightly behind the acquire+edge overhead.
        horizon_ms = max(1, hold_ms) + self._config.safety_poll_interval_ms
        lease = self.acquire_button(generation, button, horizon_ms)
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
        posted = self._guarded_edge(
            generation,
            lambda: self._port.raw_pointer_delta(bounded_dx, bounded_dy, held_button),
        )
        if posted:
            # What was *posted*, not what was asked for. The dashboard reads
            # this, so a refused yaw must not show up there as a delta.
            self._last_yaw_px = bounded_dx
            self._last_yaw_at_s = monotonic_s()
            self._lifecycle.note(
                LifecycleStage.OS_EDGE_POSTED,
                f"yaw {bounded_dx:+d} px",
                target="pointer",
                posted=True,
                dx=bounded_dx,
                dy=bounded_dy,
            )
        return posted

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
    def release_navigation(self, generation: int, reason: str) -> ReleaseReport:
        """The navigation release floor, refused for a superseded generation.

        It used to ``del generation`` - the argument was accepted and thrown
        away - so a straggling worker's ``finally: release_navigation()`` ran
        the *whole* floor against whatever mode was running by then. Shadow
        blocks inside a native screen grab, the coordinator cancels it, joins
        for its bounded deadline, gives up, starts Live; the straggler wakes,
        unwinds, and disarms the Live actuator that had just been armed.
        Nothing in the trace says which worker did it, because the call had
        already forgotten which worker it came from.

        Refusing is the safe answer, not merely the correct one: the transition
        that superseded this generation ran its own ``release_all`` before the
        new mode started, so there is nothing of this worker's left to lift,
        and anything held now belongs to the mode that replaced it.
        """
        with self._lock:
            current = self._generation
        if generation != current:
            self._lifecycle.note(
                LifecycleStage.LEDGER_EMPTY,
                f"refused a release from superseded generation {generation}",
                reason=reason,
                generation=generation,
                current_generation=current,
            )
            return ReleaseReport(
                attempted_edges=(),
                failures=(),
                deadman_acknowledged=True,
                ledger_empty=self.ledger_empty(),
                release_known_safe=self.ledger_empty() and not self.release_uncertain,
                reason=f"superseded-generation:{generation}:{reason}",
            )
        return self.release_all(f"navigation:{reason}")

    # -- session factories ------------------------------------------------
    def navigation_session(self, generation: int) -> NavigationInputSession:
        return NavigationInputSession(self, generation)

    def service_session(self, generation: int) -> ServiceInputSession:
        return ServiceInputSession(self, generation)

    # -- diagnostics ------------------------------------------------------
    @property
    def unknown_focus_polls(self) -> int:
        """Safety polls that could not tell whether Roblox was frontmost."""
        return self._unknown_focus_polls

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
