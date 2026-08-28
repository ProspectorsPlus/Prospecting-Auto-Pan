"""One binding registry, and the chord state machine both platforms drive.

Why this module exists at all: the hotkeys were F1-F6, declared twice - once in
``platform_mac.py`` and once in ``platform_win.py`` - with a virtual-key table
beside each. Two lists that had to agree and nothing that made them, and every
label in the GUI and the README spelled the keys out a third time.

Function keys were also the wrong choice. F1 opens Help in a great many
applications, the row is remapped to brightness and volume by default on a Mac,
and a single unmodified keypress is one slip away from starting a character
moving. The primary bindings are now **Ctrl+Option** chords on macOS and
**Ctrl+Alt** on Windows.

Shift is deliberately absent from every chord: Roblox's Shift Lock is bound to
it, and a navigator whose start chord also toggles the camera mode it depends
on would be fighting itself.

Nothing here imports a platform module, touches an OS API, or can press a key.
It is the vocabulary and the recognizer; the ports feed it key events.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from prospector_engine.contracts import IntentType

__all__ = [
    "BINDINGS",
    "Binding",
    "Chord",
    "ChordRecognizer",
    "Modifier",
    "binding_for_intent",
    "chord_label",
    "legacy_bindings",
]


class Modifier(Enum):
    """The two modifiers the chords use.

    ``ALT`` is the physical key macOS prints as Option and Windows as Alt. One
    enum member rather than two, because it is one key: only the *label*
    differs, and labels are derived at the edge.
    """

    CTRL = "ctrl"
    ALT = "alt"

    def label(self, os_name: str) -> str:
        if self is Modifier.CTRL:
            return "Ctrl"
        return "Option" if os_name == "darwin" else "Alt"


@dataclass(frozen=True)
class Chord:
    """A set of modifiers plus one ordinary key."""

    modifiers: frozenset[Modifier]
    key: str

    def label(self, os_name: str) -> str:
        # Ctrl first, then Alt/Option, then the key: the order people say it.
        order = [Modifier.CTRL, Modifier.ALT]
        parts = [m.label(os_name) for m in order if m in self.modifiers]
        return "+".join([*parts, self.key.upper()])


@dataclass(frozen=True)
class Binding:
    """One intent, its chord, its legacy alias, and what it does."""

    intent: IntentType
    chord: Chord
    #: The pre-chord function key. Kept working so an existing muscle-memory
    #: workflow is not broken by an upgrade, but never advertised as primary.
    legacy_key: str | None
    description: str
    #: Whether the intent may only fire while Roblox is focused. Stop is the
    #: one that must not: a stop that needs focus is useless exactly when it
    #: matters, which is when something else has grabbed the screen.
    requires_focus: bool = True

    def label(self, os_name: str) -> str:
        return self.chord.label(os_name)


def _chord(key: str) -> Chord:
    return Chord(frozenset({Modifier.CTRL, Modifier.ALT}), key)


#: The whole vocabulary, in the order the GUI lists it.
BINDINGS: tuple[Binding, ...] = (
    Binding(
        IntentType.START_LIVE,
        _chord("n"),
        "f1",
        "Start armed navigation",
    ),
    Binding(
        IntentType.START_SHADOW,
        _chord("o"),
        None,
        "Start observing - no input is sent",
        requires_focus=False,
    ),
    Binding(
        IntentType.STOP,
        _chord("x"),
        "f2",
        "Stop and release all input",
        requires_focus=False,
    ),
    Binding(
        IntentType.RESET_CHARACTER,
        _chord("r"),
        "f4",
        "Reset the character",
    ),
    Binding(
        IntentType.PAN_SWAP_TEST,
        _chord("p"),
        "f5",
        "Pan swap service",
    ),
    Binding(
        IntentType.DIG_LOOP,
        _chord("d"),
        "f6",
        "Dig service",
    ),
    Binding(
        IntentType.PIXEL_INFO,
        _chord("i"),
        "f3",
        "Read the pixel under the cursor",
        requires_focus=False,
    ),
)


def binding_for_intent(intent: IntentType) -> Binding | None:
    return next((b for b in BINDINGS if b.intent is intent), None)


def chord_label(intent: IntentType, os_name: str) -> str:
    """The chord for an intent, spelled for this OS. Empty if unbound."""
    binding = binding_for_intent(intent)
    return "" if binding is None else binding.label(os_name)


def legacy_bindings() -> dict[str, IntentType]:
    """The F-key aliases, for the ports that still accept them."""
    return {b.legacy_key: b.intent for b in BINDINGS if b.legacy_key is not None}


#: Normalized names for the modifier keys, left and right. Both sides of the
#: keyboard are real keys people actually use, and a recognizer that only knew
#: the left one would silently ignore half of them.
MODIFIER_KEYS: dict[str, Modifier] = {
    "ctrl_l": Modifier.CTRL,
    "ctrl_r": Modifier.CTRL,
    "alt_l": Modifier.ALT,
    "alt_r": Modifier.ALT,
}


class ChordRecognizer:
    """Turns normalized key-down/key-up events into intents.

    The rules it exists to enforce, none of which a plain "on this keypress,
    fire" callback gets right:

    * **Rising edge only.** An intent fires on the transition into "held", so
      holding the chord down does not submit it eighty times a second. The OS
      autorepeats the non-modifier key; every repeat arrives as another
      key-down with no intervening key-up, and is ignored.
    * **Both modifier sides count**, and a chord stays satisfied while *either*
      Ctrl is down.
    * **State is cleared on focus changes.** A modifier released while another
      application had the keyboard never delivers its key-up, which would
      otherwise leave the recognizer believing Ctrl is held forever.
    * **Synthetic input is never fed back.** The ports do not route what the
      input authority emits into this; W, A, S, D and the arrow keys are not in
      the vocabulary here, so even a leak could not fire an intent.
    """

    def __init__(self, bindings: Iterable[Binding] = BINDINGS) -> None:
        self._bindings = tuple(bindings)
        self._chords: dict[tuple[frozenset[Modifier], str], Binding] = {
            (b.chord.modifiers, b.chord.key): b for b in self._bindings
        }
        self._legacy: dict[str, Binding] = {
            b.legacy_key: b for b in self._bindings if b.legacy_key is not None
        }
        self._modifiers_down: set[str] = set()
        self._keys_down: set[str] = set()

    @property
    def held_modifiers(self) -> frozenset[Modifier]:
        return frozenset(MODIFIER_KEYS[name] for name in self._modifiers_down)

    def key_down(self, name: str) -> Binding | None:
        """Feed one key-down. Returns the binding to fire, or ``None``.

        ``name`` is already normalized: ``ctrl_l``, ``alt_r``, ``n``, ``f1``.
        """
        key = name.lower()
        if key in MODIFIER_KEYS:
            self._modifiers_down.add(key)
            return None
        if key in self._keys_down:
            return None  # autorepeat, or a duplicate from a second listener
        self._keys_down.add(key)

        legacy = self._legacy.get(key)
        if legacy is not None and not self.held_modifiers:
            # A bare function key. Still honoured, never advertised.
            return legacy
        return self._chords.get((self.held_modifiers, key))

    def key_up(self, name: str) -> None:
        key = name.lower()
        self._modifiers_down.discard(key)
        self._keys_down.discard(key)

    def clear(self, reason: str = "") -> None:
        """Forget every held key. Called on focus loss and on listener stop."""
        del reason
        self._modifiers_down.clear()
        self._keys_down.clear()
