"""The chord recognizer, and both platform adapters driven by real key events.

These tests press and release keys. They never call ``fire()`` with an
``IntentType`` and call that a test of the hotkey system: the interesting
behaviour - rising edges, autorepeat, exact modifier matching, quarantine
across a focus change - lives entirely in the transitions, and a test that
skips them proves only that a callback can be called.

The macOS half drives ``MacHotkeySource._on_event`` with **real CGEvent
objects**, through the exact four-argument signature Quartz uses, because that
contract is what the previous implementation got wrong. Nothing here posts an
event; the events are constructed, decoded and dropped.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from prospector_engine.bindings import (
    BINDINGS,
    ORDINARY_KEYS,
    ChordDisposition,
    ChordRecognizer,
    HotkeyState,
    Modifier,
    binding_for_intent,
    chord_label,
)
from prospector_engine.contracts import FocusState, IntentType, RuntimeIntent

CTRL = frozenset({Modifier.CTRL})
NONE: frozenset[Modifier] = frozenset()


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_the_documented_chords_are_the_ones_that_are_bound() -> None:
    expected = {
        IntentType.START_LIVE: "Ctrl+N",
        IntentType.START_SHADOW: "Ctrl+O",
        IntentType.STOP: "Ctrl+X",
        IntentType.RESET_CHARACTER: "Ctrl+R",
        IntentType.PAN_SWAP_TEST: "Ctrl+P",
        IntentType.DIG_LOOP: "Ctrl+D",
        IntentType.PIXEL_INFO: "Ctrl+I",
    }
    for intent, label in expected.items():
        # Identical on both platforms: no Option, no Alt, no Fn, no F-row.
        assert chord_label(intent, "darwin") == label
        assert chord_label(intent, "win32") == label


def test_every_chord_is_ctrl_only() -> None:
    for binding in BINDINGS:
        assert binding.chord.modifiers == CTRL, binding.intent
        assert len(binding.chord.key) == 1


def test_no_function_key_is_bound_anywhere() -> None:
    """An alias that still fires is not removed, it is hidden."""
    for binding in BINDINGS:
        assert not binding.chord.key.startswith("f") or binding.chord.key == "f"
    assert ORDINARY_KEYS == {"n", "o", "x", "r", "p", "d", "i"}
    assert not any(key.startswith("f") and key[1:].isdigit() for key in ORDINARY_KEYS)


def test_every_chord_is_unique() -> None:
    chords = [(b.chord.modifiers, b.chord.key) for b in BINDINGS]
    assert len(chords) == len(set(chords))


def test_stop_and_observe_never_require_focus() -> None:
    """A stop that needs focus is useless exactly when it is needed."""
    stop = binding_for_intent(IntentType.STOP)
    shadow = binding_for_intent(IntentType.START_SHADOW)
    live = binding_for_intent(IntentType.START_LIVE)
    assert stop is not None and stop.requires_focus is False
    assert shadow is not None and shadow.requires_focus is False
    assert live is not None and live.requires_focus is True


# ---------------------------------------------------------------------------
# The recognizer
# ---------------------------------------------------------------------------


def _press(
    recognizer: ChordRecognizer, name: str, modifiers: frozenset[Modifier] = CTRL
) -> IntentType | None:
    event = recognizer.key_down(name, modifiers)
    return event.binding.intent if event.binding else None


def test_the_key_alone_does_nothing_without_ctrl() -> None:
    recognizer = ChordRecognizer()
    assert _press(recognizer, "n", NONE) is None
    assert _press(recognizer, "x", NONE) is None


def test_the_full_chord_fires_once_on_the_rising_edge() -> None:
    recognizer = ChordRecognizer()
    assert _press(recognizer, "n") is IntentType.START_LIVE


def test_ctrl_option_n_does_not_match_ctrl_n() -> None:
    """Exact match, never subset. This is the whole reason ALT is tracked."""
    recognizer = ChordRecognizer()
    event = recognizer.key_down("n", frozenset({Modifier.CTRL, Modifier.ALT}))
    assert event.disposition is ChordDisposition.NO_CHORD
    assert event.binding is None


@pytest.mark.parametrize("spoiler", [Modifier.ALT, Modifier.SHIFT, Modifier.META])
def test_any_extra_modifier_disqualifies_the_chord(spoiler: Modifier) -> None:
    recognizer = ChordRecognizer()
    assert _press(recognizer, "n", frozenset({Modifier.CTRL, spoiler})) is None


def test_autorepeat_does_not_resubmit() -> None:
    """Holding the chord down must not submit it eighty times a second."""
    recognizer = ChordRecognizer()
    fired = [_press(recognizer, "n") for _ in range(12)]
    assert fired == [IntentType.START_LIVE, *([None] * 11)]
    assert recognizer.key_down("n", CTRL).disposition is ChordDisposition.AUTOREPEAT


def test_releasing_and_pressing_again_fires_again() -> None:
    recognizer = ChordRecognizer()
    assert _press(recognizer, "x") is IntentType.STOP
    recognizer.key_up("x", CTRL)
    assert _press(recognizer, "x") is IntentType.STOP


def test_releasing_ctrl_breaks_the_chord() -> None:
    recognizer = ChordRecognizer()
    _press(recognizer, "n")
    recognizer.key_up("n", NONE)
    assert _press(recognizer, "n", NONE) is None


def test_a_lost_key_up_cannot_strand_a_modifier() -> None:
    """The modifier set is a level reading, so there is nothing to strand."""
    recognizer = ChordRecognizer()
    _press(recognizer, "n")
    recognizer.key_up("n", NONE)
    # Ctrl's key-up was delivered to another application and never seen here.
    # The next event simply reports Ctrl up, and the chord does not match.
    assert _press(recognizer, "n", NONE) is None


def test_a_ctrl_carried_across_a_focus_change_is_quarantined() -> None:
    """Switching into Roblox mid-chord must not arm half of one."""
    recognizer = ChordRecognizer()
    recognizer.quarantine(CTRL, "focus-change")
    event = recognizer.key_down("n", CTRL)
    assert event.disposition is ChordDisposition.NO_CHORD
    assert event.modifiers == NONE
    assert "ctrl" in recognizer.quarantined


def test_quarantine_lifts_only_when_the_key_is_physically_released() -> None:
    recognizer = ChordRecognizer()
    recognizer.quarantine(CTRL, "focus-change")
    assert _press(recognizer, "n") is None
    recognizer.key_up("n", CTRL)  # still held: quarantine survives
    assert _press(recognizer, "n") is None
    recognizer.modifiers_changed(NONE)  # Ctrl physically released
    assert recognizer.quarantined == frozenset()
    recognizer.key_up("n", NONE)
    assert _press(recognizer, "n") is IntentType.START_LIVE


def test_a_bare_movement_key_can_never_fire_an_intent() -> None:
    """What the input authority emits is unmodified W/A/S/D and arrows.

    ``D`` overlaps a chord key, which is exactly why this matters: the
    authority emits it *bare*, and bare it matches nothing. The second line of
    defence is that the ports drop injected events before the recognizer is
    reached at all, so our own D cannot come back as Ctrl+D even if the user
    happens to be holding Ctrl - see ``test_mac_injected_events_are_ignored``.
    """
    recognizer = ChordRecognizer()
    for key in ("w", "a", "s", "d", "left", "right", "space"):
        assert recognizer.key_down(key, NONE).binding is None
        recognizer.key_up(key, NONE)


def test_an_unnamed_key_is_reported_rather_than_swallowed() -> None:
    recognizer = ChordRecognizer()
    assert recognizer.key_down(None, CTRL).disposition is ChordDisposition.UNKNOWN_KEY


# ---------------------------------------------------------------------------
# macOS: the real Quartz event-tap callback contract
# ---------------------------------------------------------------------------

pytestmark_mac = pytest.mark.skipif(
    sys.platform != "darwin", reason="drives real CGEvent objects"
)

_MAC_VK = {"n": 45, "o": 31, "x": 7, "r": 15, "p": 35, "d": 2, "i": 34, "a": 0}


def _mac_source(focus: FocusState = True) -> tuple[Any, list[RuntimeIntent]]:
    from prospector_engine.platform_mac import MacHotkeySource

    submitted: list[RuntimeIntent] = []
    return MacHotkeySource(submitted.append, focus_probe=lambda: focus), submitted


def _cg_key(keycode: int, *, down: bool, ctrl: bool = False, injected: bool = False) -> Any:
    """One CGEvent shaped exactly as the tap receives it."""
    import Quartz

    event = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
    Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskControl if ctrl else 0)
    # Hardware events carry process id 0; a created event carries ours.
    Quartz.CGEventSetIntegerValueField(
        event, Quartz.kCGEventSourceUnixProcessID, 4242 if injected else 0
    )
    return event


def _deliver(source: Any, event: Any, *, down: bool = True) -> Any:
    """Invoke the callback through Quartz's four-argument signature."""
    import Quartz

    kind = Quartz.kCGEventKeyDown if down else Quartz.kCGEventKeyUp
    return source._on_event(None, kind, event, None)


@pytestmark_mac
def test_mac_ctrl_n_is_heard() -> None:
    source, submitted = _mac_source()
    _deliver(source, _cg_key(_MAC_VK["n"], down=True, ctrl=True))
    assert [i.intent_type for i in submitted] == [IntentType.START_LIVE]
    assert submitted[0].source == "hotkey"
    assert source.health().chords_recognized == 1


@pytestmark_mac
def test_mac_an_ordinary_key_cannot_stop_the_listener() -> None:
    """The regression that made every chord vanish.

    The previous adapter returned ``False`` from its callback for any key it
    did not recognize. pynput reads a ``False`` return as "stop listening" and
    raises ``StopException``, so the listener died on the first ordinary
    keypress of the session and no chord after it was ever seen. The callback
    here returns the event and nothing else, whatever it decides.
    """
    source, submitted = _mac_source()
    for _ in range(50):
        _deliver(source, _cg_key(_MAC_VK["a"], down=True))  # a plain "a"
        _deliver(source, _cg_key(_MAC_VK["a"], down=False), down=False)
    _deliver(source, _cg_key(999, down=True))  # a key with no name at all
    # Still hearing: Ctrl+N works after all of that.
    _deliver(source, _cg_key(_MAC_VK["n"], down=True, ctrl=True))
    assert [i.intent_type for i in submitted] == [IntentType.START_LIVE]
    assert source.health().last_error == ""


@pytestmark_mac
def test_mac_a_lone_ctrl_cannot_stop_the_listener() -> None:
    import Quartz

    source, submitted = _mac_source()
    for _ in range(20):
        event = Quartz.CGEventCreateKeyboardEvent(None, 59, True)
        Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskControl)
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGEventSourceUnixProcessID, 0)
        source._on_event(None, Quartz.kCGEventFlagsChanged, event, None)
    _deliver(source, _cg_key(_MAC_VK["n"], down=True, ctrl=True))
    assert [i.intent_type for i in submitted] == [IntentType.START_LIVE]


@pytestmark_mac
def test_mac_injected_events_are_ignored() -> None:
    """Our own synthetic input must never be able to fire a chord back at us."""
    source, submitted = _mac_source()
    _deliver(source, _cg_key(_MAC_VK["n"], down=True, ctrl=True, injected=True))
    assert submitted == []
    assert source.health().events_seen == 0


@pytestmark_mac
def test_mac_autorepeat_is_suppressed() -> None:
    source, submitted = _mac_source()
    for _ in range(12):
        _deliver(source, _cg_key(_MAC_VK["x"], down=True, ctrl=True))
    assert [i.intent_type for i in submitted] == [IntentType.STOP]


@pytestmark_mac
def test_mac_release_then_press_fires_twice() -> None:
    source, submitted = _mac_source()
    _deliver(source, _cg_key(_MAC_VK["x"], down=True, ctrl=True))
    _deliver(source, _cg_key(_MAC_VK["x"], down=False, ctrl=True), down=False)
    _deliver(source, _cg_key(_MAC_VK["x"], down=True, ctrl=True))
    assert [i.intent_type for i in submitted] == [IntentType.STOP, IntentType.STOP]


@pytestmark_mac
def test_mac_stop_fires_without_roblox_focus_and_navigation_does_not() -> None:
    source, submitted = _mac_source(focus=False)
    _deliver(source, _cg_key(_MAC_VK["n"], down=True, ctrl=True))
    assert submitted == []
    # ...and the refusal is recorded rather than dropped on the floor.
    health = source.health()
    assert health.last_chord == "Ctrl+N"
    assert "not focused" in health.last_chord_disposition

    _deliver(source, _cg_key(_MAC_VK["x"], down=True, ctrl=True))
    assert [i.intent_type for i in submitted] == [IntentType.STOP]


@pytestmark_mac
def test_mac_a_disabled_tap_is_re_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS disables a slow tap and never re-enables it on its own.

    pynput's run loop ignores the disable event entirely, so the tap stays dead
    while its thread stays alive - deaf, and reporting itself healthy.
    """
    import Quartz

    enabled: list[tuple[object, bool]] = []
    monkeypatch.setattr(
        Quartz, "CGEventTapEnable", lambda tap, on: enabled.append((tap, on)), raising=True
    )
    source, _ = _mac_source()
    sentinel = object()
    source._tap = sentinel
    source._on_event(None, Quartz.kCGEventTapDisabledByTimeout, None, None)
    assert enabled == [(sentinel, True)]
    assert source.health().restarts == 1
    assert "re-enabled" in source.health().detail


@pytestmark_mac
def test_mac_a_tap_that_keeps_dying_becomes_a_hard_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recover, or say so. Never re-enable forever and call it healthy."""
    import Quartz

    monkeypatch.setattr(Quartz, "CGEventTapEnable", lambda tap, on: None, raising=True)
    source, _ = _mac_source()
    source._tap = object()
    for _ in range(source.MAX_REENABLES + 1):
        source._on_event(None, Quartz.kCGEventTapDisabledByTimeout, None, None)
    assert source.health().state is HotkeyState.FAILED
    assert source.is_running() is False


@pytestmark_mac
def test_mac_a_broken_recognizer_is_journalled_not_raised() -> None:
    """An exception escaping the callback would take the run loop down."""
    source, _ = _mac_source()

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("detector exploded")

    source._recognizer.key_down = boom  # type: ignore[method-assign]
    _deliver(source, _cg_key(_MAC_VK["n"], down=True, ctrl=True))
    assert "detector exploded" in source.health().last_error


@pytestmark_mac
def test_mac_is_running_is_false_until_the_tap_is_proven() -> None:
    source, _ = _mac_source()
    assert source.is_running() is False
    assert source.health().state is HotkeyState.STOPPED


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS listener contract")
def test_pynput_stops_a_listener_whose_callback_returns_false() -> None:
    """Why the macOS listener is a direct Quartz tap and not pynput.

    Kept as a regression note rather than a live dependency: in pynput 1.8.2
    ``AbstractListener`` wraps every callback so that a ``False`` return raises
    ``StopException``, which stops the listener permanently. Any adapter that
    signals "I did not recognize this key" by returning ``False`` therefore
    dies on the first ordinary keypress.
    """
    pynput_keyboard = pytest.importorskip("pynput.keyboard")

    listener = pynput_keyboard.Listener(on_press=lambda _key: False, on_release=None)
    with pytest.raises(listener.StopException):
        listener.on_press(pynput_keyboard.KeyCode.from_char("a"), False)


# ---------------------------------------------------------------------------
# Windows adapter, driven by a fake GetAsyncKeyState
# ---------------------------------------------------------------------------


_WINDOWS_DRIVER = """
from prospector_engine.platform_win import WindowsHotkeySource
from prospector_engine.contracts import IntentType

CTRL_L, CTRL_R, ALT_L, SHIFT_L = 0xA2, 0xA3, 0xA4, 0xA0
N, X = 0x4E, 0x58

def source(focus=True):
    got = []
    return WindowsHotkeySource(got.append, focus_probe=lambda: focus), got

# Ctrl+N assembled across polls fires once, and stays fired while held.
src, got = source()
held = set()
src.poll_once(lambda c: c in held)
held |= {CTRL_L, N}
src.poll_once(lambda c: c in held)
src.poll_once(lambda c: c in held)
src.poll_once(lambda c: c in held)
assert [i.intent_type for i in got] == [IntentType.START_LIVE], got

# Release and re-press fires again, on the right-hand Ctrl.
src, got = source()
held = {CTRL_R}
src.poll_once(lambda c: c in held)
for present in (True, False, True):
    held.add(X) if present else held.discard(X)
    src.poll_once(lambda c: c in held)
assert [i.intent_type for i in got] == [IntentType.STOP, IntentType.STOP], got

# Ctrl+Alt+N is not Ctrl+N, and neither is Ctrl+Shift+N.
for spoiler in (ALT_L, SHIFT_L):
    src, got = source()
    held = {CTRL_L, spoiler, N}
    src.poll_once(lambda c: c in held)
    assert got == [], (spoiler, got)

# Without focus: navigation is refused and recorded, Stop is not refused.
src, got = source(focus=False)
held = {CTRL_L, N}
src.poll_once(lambda c: c in held)
assert got == [], got
assert src.health().last_chord == "Ctrl+N", src.health()
assert "not focused" in src.health().last_chord_disposition
held.discard(N); held.add(X)
src.poll_once(lambda c: c in held)
assert [i.intent_type for i in got] == [IntentType.STOP], got

# A Ctrl physically held across a focus transition is quarantined: it must be
# released and pressed again before a later N can complete a chord.
src, got = source()
held = {CTRL_L}
src.poll_once(lambda c: c in held)
src.clear_held_keys("focus-change")
held.add(N)
src.poll_once(lambda c: c in held)
assert got == [], ("a carried-over Ctrl completed a chord", got)
held.clear()                       # Ctrl and N physically released
src.poll_once(lambda c: c in held)
held |= {CTRL_L, N}                # pressed again, deliberately this time
src.poll_once(lambda c: c in held)
assert [i.intent_type for i in got] == [IntentType.START_LIVE], got

# clear_held_keys() must not manufacture a rising edge on the next poll.
src, got = source()
held = {CTRL_L, N}
src.poll_once(lambda c: c in held)
got.clear()
src.clear_held_keys("focus-change")
src.poll_once(lambda c: c in held)
assert got == [], ("a phantom edge was invented", got)

print("ok")
"""


@pytest.mark.skipif(sys.platform == "win32", reason="this is the from-macOS direction")
def test_the_windows_chord_poller_behaves_the_same_as_the_mac_listener() -> None:
    """Structure and logic only; no Win32 call executes (plan 16.3).

    The recognizer is shared, so this is really checking the *adapter*: that
    polled level transitions become the key-down/key-up events and the
    authoritative modifier readings the shared state machine expects.
    """
    environment = dict(os.environ)
    environment["TREASURE_ALLOW_CROSS_PLATFORM_IMPORT"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", _WINDOWS_DRIVER],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        env=environment,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


@pytest.mark.skipif(sys.platform == "win32", reason="this is the from-macOS direction")
def test_both_platforms_expose_the_same_focus_policy() -> None:
    environment = dict(os.environ)
    environment["TREASURE_ALLOW_CROSS_PLATFORM_IMPORT"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from prospector_engine.platform_win import WindowsHotkeySource\n"
            "from prospector_engine.platform_mac import MacHotkeySource\n"
            "assert MacHotkeySource.ALWAYS_ALLOWED == WindowsHotkeySource.ALWAYS_ALLOWED\n"
            "names = sorted(i.name for i in WindowsHotkeySource.ALWAYS_ALLOWED)\n"
            "print(','.join(names))\n",
        ],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        env=environment,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "STOP" in result.stdout and "START_SHADOW" in result.stdout
