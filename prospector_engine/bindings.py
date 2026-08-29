"""One binding registry, and the chord state machine both platforms drive.

Why this module exists at all: the hotkeys were declared twice - once in
``platform_mac.py`` and once in ``platform_win.py`` - with a virtual-key table
beside each. Two lists that had to agree and nothing that made them, and every
label in the GUI and the README spelled the keys out a third time.

The bindings are **Ctrl-only chords**, identical on macOS and Windows.

Three earlier choices are deliberately gone:

* **Function keys.** F1 opens Help in a great many applications, the row is
  remapped to brightness and volume by default on a Mac, and a single
  unmodified keypress is one slip away from starting a character moving. There
  are no F-key aliases; an alias that still fires is not removed, it is hidden.
* **Option / Alt.** ``Ctrl+Option`` is three keys on a laptop and collides with
  macOS text-navigation chords. Ctrl alone is the same reach on both platforms.
* **Shift.** Roblox binds Shift Lock to it, and a navigator whose start chord
  also toggles the camera mode it depends on would be fighting itself.

Nothing here imports a platform module, touches an OS API, or can press a key.
It is the vocabulary and the recognizer; the ports feed it key events.

**The modifier set is authoritative, not accumulated.** Every key event carries
the set of modifiers the OS says are physically down at that instant - macOS
reads ``CGEventGetFlags``, Windows reads ``GetAsyncKeyState``. A recognizer
that instead accumulated key-down/key-up edges believed Ctrl was held forever
the first time a key-up was delivered to another application, which is a bug
that cannot happen to a level reading.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from enum import Enum

from prospector_engine.contracts import IntentType

__all__ = [
    "BINDINGS",
    "ORDINARY_KEYS",
    "Binding",
    "Chord",
    "ChordDisposition",
    "ChordEvent",
    "ChordRecognizer",
    "HotkeyHealth",
    "HotkeyJournal",
    "HotkeyState",
    "Modifier",
    "binding_for_intent",
    "chord_label",
]


class Modifier(Enum):
    """The modifiers a chord can require, or be spoiled by.

    Only ``CTRL`` appears in a binding. The other three are tracked precisely
    so they can *disqualify* a match: ``Ctrl+Option+N`` must not be mistaken
    for ``Ctrl+N``, and it can only be told apart by a recognizer that knows
    Option is down.
    """

    CTRL = "ctrl"
    ALT = "alt"
    SHIFT = "shift"
    META = "meta"

    def label(self, os_name: str) -> str:
        return {
            Modifier.CTRL: "Ctrl",
            Modifier.ALT: "Option" if os_name == "darwin" else "Alt",
            Modifier.SHIFT: "Shift",
            Modifier.META: "Cmd" if os_name == "darwin" else "Win",
        }[self]


@dataclass(frozen=True)
class Chord:
    """A set of modifiers plus one ordinary key. Matched exactly, never by subset."""

    modifiers: frozenset[Modifier]
    key: str

    def label(self, os_name: str) -> str:
        order = [Modifier.CTRL, Modifier.ALT, Modifier.SHIFT, Modifier.META]
        parts = [m.label(os_name) for m in order if m in self.modifiers]
        return "+".join([*parts, self.key.upper()])


@dataclass(frozen=True)
class Binding:
    """One intent, its chord, and what it does."""

    intent: IntentType
    chord: Chord
    description: str
    #: Whether the intent may only fire while Roblox is focused. Stop is the
    #: one that must not: a stop that needs focus is useless exactly when it
    #: matters, which is when something else has grabbed the screen.
    requires_focus: bool = True

    def label(self, os_name: str) -> str:
        return self.chord.label(os_name)


def _ctrl(key: str) -> Chord:
    return Chord(frozenset({Modifier.CTRL}), key)


#: The whole vocabulary, in the order the GUI lists it.
BINDINGS: tuple[Binding, ...] = (
    Binding(IntentType.START_LIVE, _ctrl("n"), "Start armed navigation"),
    Binding(
        IntentType.START_SHADOW,
        _ctrl("o"),
        "Start observing - no input is sent",
        requires_focus=False,
    ),
    Binding(
        IntentType.STOP,
        _ctrl("x"),
        "Stop and release all input",
        requires_focus=False,
    ),
    Binding(IntentType.RESET_CHARACTER, _ctrl("1"), "Reset the character"),
    Binding(IntentType.PAN_SWAP_TEST, _ctrl("2"), "Pan swap service"),
    Binding(IntentType.DIG_LOOP, _ctrl("3"), "Dig service"),
    Binding(IntentType.WIGGLE_TEST, _ctrl("4"), "Wiggle move test"),
    Binding(
        IntentType.DEGREE_MONITOR,
        _ctrl("5"),
        "Find/resize the Roblox client, then boot up / reset the degree monitor",
        requires_focus=False,
    ),
    Binding(
        IntentType.PIXEL_INFO,
        _ctrl("i"),
        "Read the pixel under the cursor",
        requires_focus=False,
    ),
)

#: Every ordinary (non-modifier) key any binding names. The ports translate
#: keycodes into exactly these names and nothing else, so a key outside this
#: set is normalized to ``None`` and never reaches the recognizer.
ORDINARY_KEYS: frozenset[str] = frozenset(b.chord.key for b in BINDINGS)


def binding_for_intent(intent: IntentType) -> Binding | None:
    return next((b for b in BINDINGS if b.intent is intent), None)


def chord_label(intent: IntentType, os_name: str) -> str:
    """The chord for an intent, spelled for this OS. Empty if unbound."""
    binding = binding_for_intent(intent)
    return "" if binding is None else binding.label(os_name)


class ChordDisposition(Enum):
    """What the recognizer did with one key edge, for the log and the self-test.

    Every edge produces one of these. The self-test renders the sequence
    verbatim, which is what turns "the chord does nothing" from a guess into a
    reading: an edge that never arrives, an edge that arrives and normalizes to
    ``UNKNOWN_KEY``, and an edge that reaches ``RECOGNIZED`` and is then refused
    by the coordinator are three different faults.
    """

    #: A modifier moved. Modifiers never fire anything on their own.
    MODIFIER = "modifier"
    #: Normalized to nothing: a key no binding names. The commonest edge by far.
    UNKNOWN_KEY = "unknown_key"
    #: Held down and repeating. The rising edge already fired.
    AUTOREPEAT = "autorepeat"
    #: Held across a focus transition; must be physically released first.
    QUARANTINED = "quarantined"
    #: A key we know, with a modifier set no binding claims.
    NO_CHORD = "no_chord"
    #: A complete chord, on its rising edge.
    RECOGNIZED = "recognized"
    #: A key-up.
    RELEASED = "released"


@dataclass(frozen=True)
class ChordEvent:
    """One normalized key edge and what became of it."""

    disposition: ChordDisposition
    #: The normalized key name, or ``None`` when nothing was recognized.
    name: str | None
    #: The modifiers that counted, after quarantine was subtracted.
    modifiers: frozenset[Modifier]
    binding: Binding | None = None

    @property
    def recognized(self) -> bool:
        return self.binding is not None

    def describe(self) -> str:
        mods = "+".join(sorted(m.value for m in self.modifiers)) or "none"
        head = f"{self.disposition.value} key={self.name or '-'} mods={mods}"
        return f"{head} -> {self.binding.intent.name}" if self.binding else head


class ChordRecognizer:
    """Turns normalized key edges plus an authoritative modifier set into intents.

    The rules it exists to enforce, none of which a plain "on this keypress,
    fire" callback gets right:

    * **Rising edge only.** An intent fires on the transition into "held", so
      holding the chord down does not submit it eighty times a second. The OS
      autorepeats the key; every repeat arrives as another key-down with no
      intervening key-up, and is reported as ``AUTOREPEAT``.
    * **A release followed by a new press fires again.** ``key_up`` clears the
      held key, so the very next key-down is a fresh rising edge.
    * **Exact modifier match.** ``Ctrl+Option+N`` is not ``Ctrl+N``. The chord
      table is keyed by the *whole* modifier set.
    * **Keys held across a focus transition are quarantined.** A Ctrl the user
      was holding in another application must be physically released and
      pressed again before it can complete a chord here - otherwise switching
      into Roblox mid-chord silently arms half of one.
    * **Synthetic input is never fed back.** The ports drop injected events
      before they reach this, and W, A, S, D and the arrow keys are not in the
      vocabulary here, so even a leak could not fire an intent.
    """

    def __init__(self, bindings: Iterable[Binding] = BINDINGS) -> None:
        self._bindings = tuple(bindings)
        self._chords: dict[tuple[frozenset[Modifier], str], Binding] = {
            (b.chord.modifiers, b.chord.key): b for b in self._bindings
        }
        self._keys_down: set[str] = set()
        self._quarantined_modifiers: set[Modifier] = set()
        self._quarantined_keys: set[str] = set()

    def _effective(self, modifiers: frozenset[Modifier]) -> frozenset[Modifier]:
        """Subtract quarantine, and lift it for anything now physically up."""
        self._quarantined_modifiers &= set(modifiers)
        return frozenset(modifiers) - self._quarantined_modifiers

    def key_down(self, name: str | None, modifiers: frozenset[Modifier]) -> ChordEvent:
        """Feed one key-down. ``name`` is ``None`` for a key we do not name.

        ``modifiers`` is what the OS says is physically down *now*, not a set
        this class accumulated.
        """
        effective = self._effective(modifiers)
        if name is None:
            return ChordEvent(ChordDisposition.UNKNOWN_KEY, None, effective)
        key = name.lower()
        if key in self._quarantined_keys:
            return ChordEvent(ChordDisposition.QUARANTINED, key, effective)
        if key in self._keys_down:
            return ChordEvent(ChordDisposition.AUTOREPEAT, key, effective)
        self._keys_down.add(key)
        binding = self._chords.get((effective, key))
        if binding is None:
            return ChordEvent(ChordDisposition.NO_CHORD, key, effective)
        return ChordEvent(ChordDisposition.RECOGNIZED, key, effective, binding)

    def key_up(self, name: str | None, modifiers: frozenset[Modifier]) -> ChordEvent:
        effective = self._effective(modifiers)
        if name is None:
            return ChordEvent(ChordDisposition.UNKNOWN_KEY, None, effective)
        key = name.lower()
        self._keys_down.discard(key)
        self._quarantined_keys.discard(key)
        return ChordEvent(ChordDisposition.RELEASED, key, effective)

    def modifiers_changed(self, modifiers: frozenset[Modifier]) -> ChordEvent:
        """A modifier-only edge. Reported so the self-test can show it."""
        return ChordEvent(ChordDisposition.MODIFIER, None, self._effective(modifiers))

    @property
    def keys_down(self) -> frozenset[str]:
        return frozenset(self._keys_down)

    @property
    def quarantined(self) -> frozenset[str]:
        return frozenset(m.value for m in self._quarantined_modifiers) | frozenset(
            self._quarantined_keys
        )

    def quarantine(self, modifiers: frozenset[Modifier], reason: str = "") -> None:
        """Require everything currently held to be released before it counts.

        Called on a focus transition. ``modifiers`` is the live reading, so a
        modifier the user has *already* let go of is not quarantined for
        nothing.
        """
        del reason
        self._quarantined_modifiers |= set(modifiers)
        self._quarantined_keys |= self._keys_down
        self._keys_down.clear()

    def clear(self, reason: str = "") -> None:
        """Forget everything, quarantine included. Only for listener teardown."""
        del reason
        self._keys_down.clear()
        self._quarantined_modifiers.clear()
        self._quarantined_keys.clear()


# ---------------------------------------------------------------------------
# Listener health
# ---------------------------------------------------------------------------


class HotkeyState(Enum):
    """Whether the listener is actually hearing the keyboard.

    ``READY`` is the only state that means a chord will be seen, and it is
    deliberately harder to reach than "a thread object exists". The observed
    failure it exists to stop: the dashboard said hotkeys were ready while the
    listener had already died on the first ordinary keypress, so every chord
    after that vanished with no symptom anywhere.
    """

    STOPPED = "stopped"
    #: Started, and has not yet proven it is hearing anything.
    STARTING = "starting"
    #: Started, the event source is enabled, and its loop has ticked.
    READY = "ready"
    #: Started and then broke, or never started. ``detail`` says which.
    FAILED = "failed"

    @property
    def hears_keys(self) -> bool:
        return self is HotkeyState.READY


@dataclass(frozen=True)
class HotkeyHealth:
    """One coherent reading of the listener, for the GUI, preflight and self-test."""

    state: HotkeyState
    backend: str
    detail: str = ""
    #: Monotonic seconds. Zero means "never".
    started_at_s: float = 0.0
    last_event_at_s: float = 0.0
    last_heartbeat_at_s: float = 0.0
    events_seen: int = 0
    chords_recognized: int = 0
    #: The last normalized edge, verbatim, whatever became of it.
    last_edge: str = ""
    last_edge_at_s: float = 0.0
    #: The last chord that completed, and what the coordinator did with it.
    last_chord: str = ""
    last_chord_at_s: float = 0.0
    last_chord_disposition: str = ""
    #: The last exception raised on the listener thread, or in a callback.
    last_error: str = ""
    restarts: int = 0

    @property
    def ready(self) -> bool:
        return self.state.hears_keys

    def describe(self) -> str:
        if self.state is HotkeyState.READY:
            seen = f"{self.events_seen} edges, {self.chords_recognized} chords"
            return f"{self.backend}: hearing the keyboard ({seen})"
        return f"{self.backend}: {self.state.value}{' - ' + self.detail if self.detail else ''}"


class HotkeyJournal:
    """Thread-safe accumulator behind :class:`HotkeyHealth`.

    Lives here rather than in either port so macOS and Windows report the same
    facts by construction instead of by agreement.
    """

    def __init__(self, backend: str, *, now: Callable[[], float] | None = None) -> None:
        self._lock = threading.Lock()
        self._now: Callable[[], float] = now or time.monotonic
        self._health = HotkeyHealth(HotkeyState.STOPPED, backend)

    @property
    def health(self) -> HotkeyHealth:
        with self._lock:
            return self._health

    def _set(self, **fields: object) -> None:
        with self._lock:
            self._health = replace(self._health, **fields)  # type: ignore[arg-type]

    def starting(self, detail: str = "") -> None:
        self._set(
            state=HotkeyState.STARTING,
            detail=detail,
            started_at_s=self._now(),
            last_error="",
        )

    def ready(self, detail: str = "") -> None:
        self._set(state=HotkeyState.READY, detail=detail)

    def failed(self, detail: str) -> None:
        self._set(state=HotkeyState.FAILED, detail=detail)

    def stopped(self, detail: str = "") -> None:
        self._set(state=HotkeyState.STOPPED, detail=detail)

    def heartbeat(self) -> None:
        self._set(last_heartbeat_at_s=self._now())

    def restarted(self, detail: str) -> None:
        with self._lock:
            restarts = self._health.restarts + 1
        self._set(restarts=restarts, detail=detail)

    def error(self, detail: str) -> None:
        self._set(last_error=detail)

    def edge(self, event: ChordEvent) -> None:
        now = self._now()
        with self._lock:
            seen = self._health.events_seen + 1
            chords = self._health.chords_recognized + (1 if event.recognized else 0)
        self._set(
            events_seen=seen,
            chords_recognized=chords,
            last_event_at_s=now,
            last_edge=event.describe(),
            last_edge_at_s=now,
        )

    def chord(self, label: str, disposition: str) -> None:
        self._set(
            last_chord=label,
            last_chord_at_s=self._now(),
            last_chord_disposition=disposition,
        )
