"""The movement actuator: the one object that presses a movement key.

Why this module exists
----------------------
The path it replaces could not press a key. That is not a figure of speech. In
every runtime trace taken on the owner's machine, ``OS_EDGE_POSTED`` - the
stage recorded the instant ``CGEventPost`` returns - never once appeared. Not
"appeared and did nothing": never appeared.

The Quartz call was never the problem, and that was measured rather than
assumed. Posting an inert keycode (F13) through the very same
``platform_mac._post``, on this machine, with Roblox frontmost, and reading it
back through ``CGEventSourceKeyState``::

    before   : F13 down = False
    key_down : F13 down = True
    key_up   : F13 down = False

The poster works. Accessibility is granted. The keycodes are byte-identical to
the ones Prospector Lite has used to walk a character on this same laptop for
months.

What sat between the navigator and that call was a **twelve-condition
admission test, re-evaluated on every single frame**: an evidence token matched
by object identity, its run id, its generation, its frame sequence, its capture
timestamp, its capture *duration*, a strictly-increasing sequence check, two
separate age budgets, a viewport identity match, and only then focus, deadman
and cancellation. Each one was individually defensible. Collectively they were
a gate that a real 55 fps pipeline with 40 ms of latency could not reliably
pass, and the character never moved. Three of them were removed one at a time
and a fourth appeared behind each.

So this module keeps the safety properties that are about a human and a
running game, and drops the ones that are about bookkeeping:

**Kept, because a person can be hurt by their absence**

* Stop releases everything, unconditionally, from any state.
* Losing Roblox focus releases everything - keys must never leak into
  another application.
* A held key has a hard ceiling on how long it may stay down.
* If the thread driving movement stops calling, everything releases: a
  stalled worker must not leave a key down.
* If the release-only helper process is unhealthy, nothing new presses.
* If the process dies, the helper releases on its own clock.
* A release is attempted for the *whole* vocabulary and is never focus-gated.

**Dropped, because none of it can make anything safer**

* Evidence tokens, and the object-identity ceremony around them.
* Per-press generation matching, viewport-identity matching, and the
  strictly-newer-frame rule.
* Making the *existence* of an edge contingent on the age of a frame. A key
  that is already down does not become dangerous because the next screenshot
  was slow; it becomes dangerous when nobody is watching it, which is what the
  heartbeat and the hold ceiling are for.

This is the contract Prospector Lite has run on for months: press once on a
rising edge, hold, release once on a falling edge, and let an independent
watchdog lift everything if the world changes.

Nothing here decides *where* to go. It is handed a desired state and it makes
the keyboard match it.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from prospector_engine.contracts import (
    EvidenceStatus,
    InputKey,
    Provenance,
    monotonic_s,
)
from prospector_engine.lifecycle import LifecycleJournal, LifecycleStage

__all__ = [
    "DesiredMovement",
    "MovementActuator",
    "MovementBlock",
    "MovementLimits",
    "MovementOutcome",
]


class MovementBlock(Enum):
    """Why movement is not being sent. ``NONE`` is the only good answer.

    Every value is a *live* condition about the world, not a bookkeeping
    mismatch, and each one is phrased the way it will be shown to a person.
    """

    NONE = ""
    STOPPED = "the navigator is stopped"
    FOCUS_ELSEWHERE = "another window is in front of Roblox"
    NO_WATCHDOG = "the safety watchdog is not running"
    DEADMAN_UNHEALTHY = "the release-only helper is not answering"
    RELEASE_UNCERTAIN = "a previous release could not be confirmed"
    HEARTBEAT_LOST = "nothing has told the keys to stay down recently"
    HOLD_CEILING = "a key reached its maximum hold and was released"

    @property
    def blocking(self) -> bool:
        return self is not MovementBlock.NONE


@dataclass(frozen=True)
class MovementLimits:
    """Bounds on what may be held, and for how long.

    Provisional configuration with provenance, not measurements. Every one is a
    *ceiling* whose job is to end a hold, never to prevent one starting.
    """

    #: How long one continuous press may last before it is lifted regardless.
    #: Lite's equivalent default is 5 s; this is the same order, chosen so an
    #: ordinary walk between two points is never interrupted by it.
    max_hold_ms: int = 8000
    #: How long the actuator may go without a fresh :meth:`MovementActuator.apply`
    #: call before it releases. This is the "the worker died holding W" bound,
    #: and it is the *only* thing that connects a held key to the health of the
    #: perception pipeline. Deliberately generous: one slow frame, one dropped
    #: frame and one garbage collection must all fit inside it.
    heartbeat_timeout_ms: int = 700
    #: How often the independent watchdog checks. It runs on its own thread, so
    #: it still fires while the caller is blocked inside a native screen grab -
    #: which is the whole reason it is a separate thread.
    watchdog_interval_ms: int = 25
    #: Ceiling on one relative yaw delta.
    max_yaw_px: int = 400
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="prospector_engine/movement.py; modelled on Prospector Lite's input_lease",
            note=(
                "chosen bounds, not measurements. They exist to END a hold, never "
                "to prevent one starting - which is the distinction the path this "
                "replaces did not make (D-067)."
            ),
        )
    )


@dataclass(frozen=True)
class DesiredMovement:
    """What the keyboard should look like right now. Level, not edge.

    The caller says what it *wants held*; the actuator works out which edges
    that implies. A caller cannot request an edge, which is what makes a rattle
    impossible to express.
    """

    #: 1 forward, -1 back, 0 neither.
    forward: int = 0
    #: -1 left, 1 right. Strafing, not turning.
    strafe: int = 0
    #: -1 left, 1 right. The camera keys.
    turn: int = 0
    jump: bool = False
    #: Relative mouse yaw to apply once, this tick. Not a held state.
    yaw_px: int = 0
    #: Plain words, for the log a person reads.
    reason: str = ""

    @property
    def keys(self) -> frozenset[InputKey]:
        """The keys this state implies, as a set. Order is never meaningful."""
        wanted: set[InputKey] = set()
        if self.forward > 0:
            wanted.add(InputKey.W)
        elif self.forward < 0:
            wanted.add(InputKey.S)
        if self.strafe < 0:
            wanted.add(InputKey.A)
        elif self.strafe > 0:
            wanted.add(InputKey.D)
        if self.turn < 0:
            wanted.add(InputKey.LEFT)
        elif self.turn > 0:
            wanted.add(InputKey.RIGHT)
        if self.jump:
            wanted.add(InputKey.SPACE)
        return frozenset(wanted)

    @property
    def idle(self) -> bool:
        return not self.keys and not self.yaw_px

    def describe(self) -> str:
        parts = sorted(key.value.upper() for key in self.keys)
        if self.yaw_px:
            parts.append(f"mouse {self.yaw_px:+d}")
        return " + ".join(parts) if parts else "nothing"


#: The idle state, as a named constant so callers stop constructing it.
IDLE = DesiredMovement()


@dataclass(frozen=True)
class MovementOutcome:
    """What actually happened to the keyboard, from the actuator's own ledger."""

    held: frozenset[InputKey]
    pressed: tuple[InputKey, ...] = ()
    released: tuple[InputKey, ...] = ()
    yaw_posted_px: int = 0
    block: MovementBlock = MovementBlock.NONE
    detail: str = ""
    #: How long the longest current hold has been down, in milliseconds. Zero
    #: when nothing is held. The overlay draws this rather than a duration it
    #: worked out for itself, because only the actuator knows when the edge
    #: went out.
    held_ms: float = 0.0
    #: Which mechanism the edges went through. On macOS the same keycode posted
    #: through a different tap reaches a different set of listeners, so "W is
    #: down" is only half a fact without it.
    backend: str = ""

    @property
    def moving(self) -> bool:
        return bool(self.held) or bool(self.yaw_posted_px)

    @property
    def changed(self) -> bool:
        return bool(self.pressed or self.released or self.yaw_posted_px)

    def describe(self) -> str:
        if self.block.blocking:
            return self.block.value
        if not self.held:
            return "nothing held"
        return " + ".join(sorted(key.value.upper() for key in self.held))


class MovementActuator:
    """Owns the physical level state of the movement keys, and nothing else.

    Thread-safe. ``apply`` runs on the navigation worker; ``release_all`` may be
    called from any thread at any time and never waits on the worker.

    Lock discipline: ``_edge_lock`` is taken around every native edge and is the
    innermost lock. Nothing in this class calls out to perception, capture, or
    the coordinator while holding it.
    """

    def __init__(
        self,
        port: Any,
        *,
        deadman: Any = None,
        focus_probe: Callable[[], bool | None] | None = None,
        journal: LifecycleJournal | None = None,
        narrate: Callable[[str, str], None] | None = None,
        limits: MovementLimits | None = None,
    ) -> None:
        self._port = port
        self._deadman = deadman
        self._focus_probe = focus_probe
        #: The mechanism edges are going out through, named in every INPUT
        #: line. "W went down" is not a complete fact on macOS, where the same
        #: keycode posted through three different taps reaches three different
        #: sets of listeners; which tap it went through is half the evidence.
        self._backend = str(getattr(port, "event_backend", "") or "default")
        self._journal = journal or LifecycleJournal()
        #: ``(verdict, sentence)`` for the plain log. Never raises past here.
        self._narrate = narrate or (lambda _verdict, _text: None)
        self._limits = limits or MovementLimits()

        self._edge_lock = threading.RLock()
        self._held: dict[InputKey, float] = {}
        self._armed = False
        self._last_apply_s = 0.0
        self._last_block = MovementBlock.STOPPED
        self._down_edges = 0
        self._up_edges = 0
        self._last_yaw_px = 0
        self._last_yaw_at_s = 0.0
        self._release_uncertain = False
        self._release_uncertain_reason = ""
        self._focus_value: bool | None = None
        self._focus_at_s = -1.0

        self._watchdog: threading.Thread | None = None
        self._watchdog_stop = threading.Event()

    # -- state ------------------------------------------------------------
    @property
    def limits(self) -> MovementLimits:
        return self._limits

    @property
    def backend(self) -> str:
        """The port's current edge mechanism, re-read so a run-time selection
        made by the native probe shows up in the very next INPUT line."""
        return str(getattr(self._port, "event_backend", "") or self._backend)

    @property
    def held(self) -> frozenset[InputKey]:
        with self._edge_lock:
            return frozenset(self._held)

    @property
    def held_targets(self) -> tuple[str, ...]:
        return tuple(sorted(key.value for key in self.held))

    @property
    def empty(self) -> bool:
        return not self.held

    @property
    def edge_counts(self) -> tuple[int, int]:
        return (self._down_edges, self._up_edges)

    @property
    def last_yaw(self) -> tuple[int, float]:
        return (self._last_yaw_px, self._last_yaw_at_s)

    @property
    def release_uncertain(self) -> bool:
        return self._release_uncertain

    @property
    def release_uncertain_reason(self) -> str:
        return self._release_uncertain_reason

    @property
    def block(self) -> MovementBlock:
        return self._last_block

    #: How long one frontmost reading may be reused. The probe is a window-list
    #: scan; taking one per edge cost a scan per frame *and* one per watchdog
    #: tick - about ninety-five a second between them - on the path that decides
    #: whether a key stays down. The watchdog interval is the natural budget.
    FOCUS_CACHE_S = 0.05

    def _focus_reading(self, *, now_s: float) -> bool | None:
        """The frontmost verdict: ``True``, ``False``, or ``None`` for unknown."""
        probe = self._focus_probe
        if probe is None:
            return True
        if now_s - self._focus_at_s < self.FOCUS_CACHE_S:
            return self._focus_value
        try:
            value: bool | None = probe()
        except Exception:
            value = None
        self._focus_value = value
        self._focus_at_s = now_s
        return value

    def held_since_s(self, key: InputKey, now_s: float) -> float:
        """How long ``key`` has been *continuously* down. Zero if it is not."""
        with self._edge_lock:
            began = self._held.get(key)
        return 0.0 if began is None else max(0.0, now_s - began)

    def forward_held_s(self, now_s: float) -> float:
        return self.held_since_s(InputKey.W, now_s)

    def hold_report(self, now_s: float) -> Mapping[str, float]:
        with self._edge_lock:
            return {key.value: max(0.0, now_s - at) for key, at in self._held.items()}

    # -- arming -----------------------------------------------------------
    def arm(self, reason: str = "") -> None:
        """Permit presses. Called when an input mode starts, and never by a worker."""
        with self._edge_lock:
            self._armed = True
            self._last_apply_s = monotonic_s()
            self._last_block = MovementBlock.NONE
        self._narrate("pass", f"Movement enabled{f' ({reason})' if reason else ''}.")

    def disarm(self, reason: str) -> None:
        """Forbid presses and lift what is held. Idempotent.

        Uses :meth:`release_held` rather than the full sweep: every caller of
        this is a mode transition, and the authority's own release floor runs
        beside it.
        """
        with self._edge_lock:
            self._armed = False
        self.release_held(reason)

    @property
    def armed(self) -> bool:
        return self._armed

    # -- the one call a worker makes --------------------------------------
    def apply(self, desired: DesiredMovement) -> MovementOutcome:
        """Make the keyboard match ``desired``. The only entry point that presses.

        Level-triggered by construction:

        * a key in ``desired`` and not held  -> exactly one down edge;
        * a key in ``desired`` and held      -> **no edge at all**;
        * a key held and not in ``desired``  -> exactly one up edge.

        A rattle is therefore not something that can be expressed, let alone
        happen by accident. Renewal is not a concept here: a key that is down
        stays down until something asks for it to stop or the watchdog lifts it.
        """
        blocked = self._blocking_condition()
        if blocked.blocking:
            report = self.release_all(f"blocked:{blocked.name.lower()}")
            self._last_block = blocked
            return MovementOutcome(
                held=frozenset(),
                released=report,
                block=blocked,
                detail=blocked.value,
            )

        wanted = desired.keys
        with self._edge_lock:
            self._last_apply_s = monotonic_s()
            self._last_block = MovementBlock.NONE
            current = frozenset(self._held)

        released = tuple(sorted(current - wanted, key=lambda k: k.value))
        pressed_ok: list[InputKey] = []

        # Release first, always. Holding Left and Right at once, or W and S, is
        # never a state this may pass through even for one edge.
        for key in released:
            self._lift(key, "no longer wanted")

        for key in sorted(wanted - current, key=lambda k: k.value):
            if self._press(key, desired.reason):
                pressed_ok.append(key)

        yaw = 0
        if desired.yaw_px:
            limit = self._limits.max_yaw_px
            bounded = max(-limit, min(limit, int(desired.yaw_px)))
            if self._post_yaw(bounded):
                yaw = bounded

        now = monotonic_s()
        with self._edge_lock:
            longest = max((now - began for began in self._held.values()), default=0.0)
        return MovementOutcome(
            held=self.held,
            pressed=tuple(pressed_ok),
            released=released,
            yaw_posted_px=yaw,
            detail=desired.reason,
            held_ms=longest * 1000.0,
            backend=self.backend,
        )

    # -- edges ------------------------------------------------------------
    def _press(self, key: InputKey, reason: str) -> bool:
        """One down edge. Registers with the helper first, rolls back on failure."""
        deadman = self._deadman
        if deadman is not None:
            # Best effort. ``_blocking_condition`` has already refused to press
            # while the helper is unhealthy; a failed *registration* by an
            # otherwise healthy helper must not silently skip the press,
            # because the in-process watchdog still covers this key.
            with contextlib.suppress(Exception):
                deadman.register(id(key), 0, key.value, self._limits.max_hold_ms)
        with self._edge_lock:
            if not self._armed:
                return False
            try:
                self._port.raw_key_down(self._port.key_code(key))
            except Exception as exc:
                self._journal.note(
                    LifecycleStage.OS_EDGE_POSTED,
                    f"the post call raised for {key.value}",
                    target=key.value,
                    posted=False,
                    error=repr(exc),
                )
                self._narrate("fail", f"Could not press {key.value.upper()}: {exc!r}")
                return False
            self._down_edges += 1
            self._held[key] = monotonic_s()
        self._journal.note(
            LifecycleStage.OS_EDGE_POSTED,
            key.value,
            target=key.value,
            posted=True,
            backend=self.backend,
        )
        self._journal.note(LifecycleStage.LEASE_HELD, key.value, target=key.value)
        self._narrate(
            "input",
            f"backend={self.backend} key={key.value.upper()} DOWN"
            + (f" - {reason}" if reason else ""),
        )
        return True

    def _lift(self, key: InputKey, reason: str) -> None:
        """One up edge. Never focus-gated, never refused, never skipped."""
        with self._edge_lock:
            began = self._held.pop(key, None)
            held_ms = 0.0 if began is None else (monotonic_s() - began) * 1000.0
            try:
                self._port.raw_key_up(self._port.key_code(key))
                self._up_edges += 1
                failed = False
            except Exception as exc:
                failed = True
                self._release_uncertain = True
                self._release_uncertain_reason = f"{key.value}: {exc!r}"
        if failed:
            self._narrate("fail", f"Could not release {key.value.upper()} - stopping.")
            return
        if began is not None:
            if key is InputKey.W:
                self._journal.note(
                    LifecycleStage.W_HOLD_CONFIRMED,
                    f"forward was down for {held_ms:.0f} ms",
                    target=key.value,
                    held_ms=round(held_ms, 1),
                )
            self._journal.note(
                LifecycleStage.W_RELEASE_POSTED,
                key.value,
                target=key.value,
                held_ms=round(held_ms, 1),
            )
            self._narrate(
                "input",
                f"HOLD {key.value.upper()} {held_ms:.0f} ms, then "
                f"backend={self.backend} key={key.value.upper()} UP - {reason}",
            )

    def _post_yaw(self, dx: int) -> bool:
        with self._edge_lock:
            if not self._armed:
                return False
            try:
                self._port.raw_pointer_delta(dx, 0, None)
            except Exception as exc:
                self._narrate("fail", f"Could not turn the camera: {exc!r}")
                return False
            self._last_yaw_px = dx
            self._last_yaw_at_s = monotonic_s()
        self._journal.note(
            LifecycleStage.TURN_POSTED, f"mouse {dx:+d} px", delta_px=dx, backend="mouse_yaw"
        )
        self._narrate(
            "input",
            f"backend={self.backend} camera yaw {dx:+d} px ({'right' if dx > 0 else 'left'})",
        )
        return True

    # -- release ----------------------------------------------------------
    #: Every key this actuator can press, and therefore every key its floor
    #: must lift. Adding a key that can go down without adding it here is the
    #: one edit that could leave something held.
    VOCABULARY: tuple[InputKey, ...] = (
        InputKey.W,
        InputKey.A,
        InputKey.S,
        InputKey.D,
        InputKey.LEFT,
        InputKey.RIGHT,
        InputKey.SPACE,
    )

    def release_held(self, reason: str = "stop") -> tuple[InputKey, ...]:
        """Lift exactly what the ledger says is held. One up edge each.

        Used when a *larger* release floor is about to run anyway - the input
        authority sweeps the whole vocabulary right after calling this - so
        sweeping here too would double every edge and push the measured stop
        latency past its bound for no added safety.
        """
        with self._edge_lock:
            known = tuple(sorted(self._held, key=lambda k: k.value))
        for key in known:
            self._lift(key, reason)
        with self._edge_lock:
            self._held.clear()
        if known:
            self._journal.note(
                LifecycleStage.LEDGER_EMPTY, reason, released=len(known), reason=reason
            )
        return known

    def release_all(self, reason: str = "stop") -> tuple[InputKey, ...]:
        """Lift what is held, then sweep the whole vocabulary. Idempotent.

        The sweep is the floor under the floor: the ledger can be wrong in
        exactly one direction that matters - believing a key is up when the OS
        thinks it is down - and only an unconditional up edge covers that.
        This is the standalone path (the watchdog, the CLI, a bare actuator);
        callers that have their own vocabulary sweep use :meth:`release_held`.
        """
        known = self.release_held(reason)
        for key in self.VOCABULARY:
            try:
                self._port.raw_key_up(self._port.key_code(key))
            except Exception as exc:
                self._release_uncertain = True
                self._release_uncertain_reason = f"{key.value}: {exc!r}"
        deadman = self._deadman
        if deadman is not None:
            with contextlib.suppress(Exception):
                deadman.release_all(reason)
        return known

    def clear_release_uncertainty(self) -> None:
        self._release_uncertain = False
        self._release_uncertain_reason = ""

    # -- the independent watchdog -----------------------------------------
    def start_watchdog(self) -> None:
        if self._watchdog is not None and self._watchdog.is_alive():
            return
        self._watchdog_stop.clear()
        thread = threading.Thread(
            target=self._watchdog_loop, name="treasure-movement-watchdog", daemon=True
        )
        self._watchdog = thread
        thread.start()

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
        interval = self._limits.watchdog_interval_ms / 1000.0
        while not self._watchdog_stop.wait(interval):
            try:
                self.poll()
            except Exception:
                # A watchdog that dies is worse than a noisy one, and a
                # watchdog that dies *holding a key* is the worst outcome here.
                with contextlib.suppress(Exception):
                    self.release_all("watchdog-error")

    def poll(self) -> MovementBlock:
        """One watchdog pass. Exposed so tests can drive it deterministically."""
        if not self.held:
            return MovementBlock.NONE
        blocked = self._blocking_condition()
        if blocked.blocking:
            self._last_block = blocked
            self._narrate("fail", f"Released everything: {blocked.value}.")
            self.release_all(f"watchdog:{blocked.name.lower()}")
        return blocked

    def _blocking_condition(self) -> MovementBlock:
        """The one reason movement may not happen right now, or ``NONE``.

        Ordered so the most actionable answer wins: a person who has not
        started the navigator should be told that, not told about a helper
        process.
        """
        if not self._armed:
            return MovementBlock.STOPPED
        if self._release_uncertain:
            return MovementBlock.RELEASE_UNCERTAIN
        if not self.watchdog_running:
            # Nothing may be held that nothing is watching. This is the one
            # condition Lite raises ``LeaseRefused`` for, and for the same
            # reason: a deadline checked on a blocked thread is not a deadline.
            return MovementBlock.NO_WATCHDOG
        # Only a *positive* "another application is in front" blocks. An
        # unknown reading must not, and this is not a preference - it is the
        # single most expensive mistake in the path this replaces.
        #
        # macOS's frontmost probe is a CGWindowList scan that returns ``None``
        # on any error or ambiguity. The old authority wrote
        # ``if focus is not True: refuse``, which turned every ambiguous scan
        # into a refused keypress. Lite has walked a character on this same
        # machine for months with the opposite rule, stated in its own port:
        # "only a positive 'another app is frontmost' may gate new input, and a
        # safety RELEASE is never gated on this."
        #
        # The asymmetry is the point. Refusing to press on "I don't know" makes
        # a macro that cannot move; releasing on "I don't know" would make one
        # that cannot be trusted - so releases are never focus-gated at all.
        if self._focus_reading(now_s=monotonic_s()) is False:
            return MovementBlock.FOCUS_ELSEWHERE
        deadman = self._deadman
        if deadman is not None and not getattr(deadman, "healthy", True):
            return MovementBlock.DEADMAN_UNHEALTHY
        now = monotonic_s()
        with self._edge_lock:
            held = dict(self._held)
            last_apply = self._last_apply_s
        if held and (now - last_apply) * 1000.0 > self._limits.heartbeat_timeout_ms:
            return MovementBlock.HEARTBEAT_LOST
        ceiling_s = self._limits.max_hold_ms / 1000.0
        if any(now - began > ceiling_s for began in held.values()):
            return MovementBlock.HOLD_CEILING
        return MovementBlock.NONE


def desired_from_command(command: Any) -> DesiredMovement:
    """Translate a :class:`NavigationCommand` into a desired keyboard state.

    Kept as a free function so the navigator keeps producing the command type
    the rest of the application already renders, and only the *actuation* half
    changed.
    """
    if command is None:
        return IDLE
    return DesiredMovement(
        forward=int(getattr(command, "forward_axis", 0)),
        strafe=int(getattr(command, "lateral_axis", 0)),
        turn=int(getattr(command, "turn_axis", 0)),
        jump=bool(getattr(command, "jump", False)),
        yaw_px=int(getattr(command, "yaw_delta_px", 0)),
        reason=str(getattr(command, "reason", "")),
    )
