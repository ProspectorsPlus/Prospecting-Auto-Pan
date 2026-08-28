"""The chord recognizer, and both platform adapters driven by real key events.

These tests press and release keys. They never call ``fire()`` with an
``IntentType`` and call that a test of the hotkey system: the interesting
behaviour - rising edges, autorepeat, both modifier sides, state cleared on a
focus change - lives entirely in the transitions, and a test that skips them
proves only that a callback can be called.
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
    ChordRecognizer,
    Modifier,
    binding_for_intent,
    chord_label,
    legacy_bindings,
)
from prospector_engine.contracts import FocusState, IntentType, RuntimeIntent

# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_the_documented_chords_are_the_ones_that_are_bound() -> None:
    expected = {
        IntentType.START_LIVE: "Ctrl+Option+N",
        IntentType.START_SHADOW: "Ctrl+Option+O",
        IntentType.STOP: "Ctrl+Option+X",
        IntentType.RESET_CHARACTER: "Ctrl+Option+R",
        IntentType.PAN_SWAP_TEST: "Ctrl+Option+P",
        IntentType.DIG_LOOP: "Ctrl+Option+D",
        IntentType.PIXEL_INFO: "Ctrl+Option+I",
    }
    for intent, label in expected.items():
        assert chord_label(intent, "darwin") == label
        assert chord_label(intent, "win32") == label.replace("Option", "Alt")


def test_no_chord_uses_shift_because_roblox_binds_it_to_shift_lock() -> None:
    for binding in BINDINGS:
        assert binding.chord.modifiers == frozenset({Modifier.CTRL, Modifier.ALT})
        assert "shift" not in binding.chord.key.lower()


def test_every_chord_is_unique() -> None:
    chords = [(b.chord.modifiers, b.chord.key) for b in BINDINGS]
    assert len(chords) == len(set(chords))


def test_stop_and_observe_never_require_focus() -> None:
    """A stop that needs focus is useless exactly when it is needed."""
    assert binding_for_intent(IntentType.STOP).requires_focus is False
    assert binding_for_intent(IntentType.START_SHADOW).requires_focus is False
    assert binding_for_intent(IntentType.START_LIVE).requires_focus is True


def test_the_legacy_f_keys_still_map_to_what_they_always_did() -> None:
    assert legacy_bindings() == {
        "f1": IntentType.START_LIVE,
        "f2": IntentType.STOP,
        "f3": IntentType.PIXEL_INFO,
        "f4": IntentType.RESET_CHARACTER,
        "f5": IntentType.PAN_SWAP_TEST,
        "f6": IntentType.DIG_LOOP,
    }


# ---------------------------------------------------------------------------
# The recognizer
# ---------------------------------------------------------------------------


def _press(recognizer: ChordRecognizer, *names: str) -> list[IntentType]:
    fired = []
    for name in names:
        binding = recognizer.key_down(name)
        if binding is not None:
            fired.append(binding.intent)
    return fired


def test_the_key_alone_does_nothing_without_its_modifiers() -> None:
    recognizer = ChordRecognizer()
    assert _press(recognizer, "n") == []
    assert _press(recognizer, "x") == []


def test_a_partial_chord_does_nothing() -> None:
    recognizer = ChordRecognizer()
    assert _press(recognizer, "ctrl_l", "n") == []


def test_the_full_chord_fires_once_on_the_rising_edge() -> None:
    recognizer = ChordRecognizer()
    assert _press(recognizer, "ctrl_l", "alt_l", "n") == [IntentType.START_LIVE]


@pytest.mark.parametrize("ctrl", ["ctrl_l", "ctrl_r"])
@pytest.mark.parametrize("alt", ["alt_l", "alt_r"])
def test_either_side_of_each_modifier_works(ctrl: str, alt: str) -> None:
    recognizer = ChordRecognizer()
    assert _press(recognizer, ctrl, alt, "x") == [IntentType.STOP]


def test_autorepeat_does_not_resubmit() -> None:
    """Holding the chord down must not submit it eighty times a second."""
    recognizer = ChordRecognizer()
    _press(recognizer, "ctrl_l", "alt_l")
    fired = _press(recognizer, "n", "n", "n", "n")
    assert fired == [IntentType.START_LIVE]


def test_releasing_and_pressing_again_fires_again() -> None:
    recognizer = ChordRecognizer()
    _press(recognizer, "ctrl_l", "alt_l")
    assert _press(recognizer, "n") == [IntentType.START_LIVE]
    recognizer.key_up("n")
    assert _press(recognizer, "n") == [IntentType.START_LIVE]


def test_releasing_a_modifier_breaks_the_chord() -> None:
    recognizer = ChordRecognizer()
    _press(recognizer, "ctrl_l", "alt_l", "n")
    recognizer.key_up("n")
    recognizer.key_up("alt_l")
    assert _press(recognizer, "n") == []


def test_clear_forgets_modifiers_lost_to_another_window() -> None:
    """A key-up delivered to another app would otherwise hold Ctrl forever."""
    recognizer = ChordRecognizer()
    _press(recognizer, "ctrl_l", "alt_l")
    assert recognizer.held_modifiers == frozenset({Modifier.CTRL, Modifier.ALT})
    recognizer.clear("focus-change")
    assert recognizer.held_modifiers == frozenset()
    assert _press(recognizer, "n") == []


def test_a_legacy_f_key_fires_bare_and_is_not_stolen_by_a_held_modifier() -> None:
    recognizer = ChordRecognizer()
    assert _press(recognizer, "f2") == [IntentType.STOP]
    recognizer.key_up("f2")
    # With the chord modifiers held, F2 is not one of the chords, so nothing
    # fires - it must not silently behave as though it were.
    _press(recognizer, "ctrl_l", "alt_l")
    assert _press(recognizer, "f2") == []


def test_a_bare_movement_key_can_never_fire_an_intent() -> None:
    """What the input authority emits is unmodified W/A/S/D and arrows.

    Even if a synthetic edge were somehow observed by the listener, none of
    those keys fires anything without its modifiers - and the ports do not
    route emitted input back into the recognizer in the first place.
    """
    recognizer = ChordRecognizer()
    for key in ("w", "a", "s", "d", "left", "right", "space"):
        assert recognizer.key_down(key) is None
        recognizer.key_up(key)


# ---------------------------------------------------------------------------
# macOS adapter, driven with pynput-shaped key objects
# ---------------------------------------------------------------------------


class _FakeKey:
    """What pynput hands a listener: a named modifier or a virtual keycode."""

    def __init__(self, *, name: str | None = None, vk: int | None = None) -> None:
        self.name = name
        self.vk = vk


def _mac_source(focus: FocusState = True) -> tuple[Any, list[RuntimeIntent]]:
    from prospector_engine.platform_mac import MacHotkeySource

    submitted: list[RuntimeIntent] = []
    source = MacHotkeySource(submitted.append, focus_probe=lambda: focus)
    return source, submitted


_MAC_VK = {"n": 45, "o": 31, "x": 7, "r": 15, "p": 35, "d": 2, "i": 34, "f2": 120}


def test_mac_chord_from_real_key_events(monkeypatch: pytest.MonkeyPatch) -> None:
    source, submitted = _mac_source()
    source.on_press(_FakeKey(name="ctrl_l"))
    source.on_press(_FakeKey(name="alt_l"))
    source.on_press(_FakeKey(vk=_MAC_VK["n"]))
    assert [i.intent_type for i in submitted] == [IntentType.START_LIVE]
    assert submitted[0].source == "hotkey"


def test_mac_autorepeat_is_suppressed() -> None:
    source, submitted = _mac_source()
    source.on_press(_FakeKey(name="ctrl_r"))
    source.on_press(_FakeKey(name="alt_r"))
    for _ in range(12):
        source.on_press(_FakeKey(vk=_MAC_VK["x"]))
    assert [i.intent_type for i in submitted] == [IntentType.STOP]


def test_mac_release_then_press_fires_twice() -> None:
    source, submitted = _mac_source()
    source.on_press(_FakeKey(name="ctrl_l"))
    source.on_press(_FakeKey(name="alt_l"))
    source.on_press(_FakeKey(vk=_MAC_VK["x"]))
    source.on_release(_FakeKey(vk=_MAC_VK["x"]))
    source.on_press(_FakeKey(vk=_MAC_VK["x"]))
    assert [i.intent_type for i in submitted] == [IntentType.STOP, IntentType.STOP]


def test_mac_stop_fires_without_roblox_focus_and_navigation_does_not() -> None:
    source, submitted = _mac_source(focus=False)
    for name in ("ctrl_l", "alt_l"):
        source.on_press(_FakeKey(name=name))

    source.on_press(_FakeKey(vk=_MAC_VK["n"]))  # start navigation - needs focus
    assert submitted == []

    source.on_press(_FakeKey(vk=_MAC_VK["x"]))  # stop - always allowed
    assert [i.intent_type for i in submitted] == [IntentType.STOP]


def test_mac_clearing_held_keys_breaks_a_stale_chord() -> None:
    source, submitted = _mac_source()
    source.on_press(_FakeKey(name="ctrl_l"))
    source.on_press(_FakeKey(name="alt_l"))
    source.clear_held_keys("focus-change")
    source.on_press(_FakeKey(vk=_MAC_VK["n"]))
    assert submitted == []


def test_mac_legacy_f_key_still_works() -> None:
    source, submitted = _mac_source()
    source.on_press(_FakeKey(vk=_MAC_VK["f2"]))
    assert [i.intent_type for i in submitted] == [IntentType.STOP]


def test_mac_ignores_keys_it_does_not_know() -> None:
    source, submitted = _mac_source()
    source.on_press(_FakeKey(vk=999))
    source.on_press(_FakeKey(name="shift"))
    assert submitted == []


# ---------------------------------------------------------------------------
# Windows adapter, driven by a fake GetAsyncKeyState
# ---------------------------------------------------------------------------


_WINDOWS_DRIVER = """
from prospector_engine.platform_win import WindowsHotkeySource
from prospector_engine.contracts import IntentType

CTRL_L, CTRL_R, ALT_L, ALT_R = 0xA2, 0xA3, 0xA4, 0xA5
N, X, F2 = 0x4E, 0x58, 0x71

def source(focus=True):
    got = []
    return WindowsHotkeySource(got.append, focus_probe=lambda: focus), got

# A chord assembled across polls fires once, and stays fired while held.
src, got = source()
held = set()
src.poll_once(lambda c: c in held)
held |= {CTRL_L, ALT_L, N}
src.poll_once(lambda c: c in held)
src.poll_once(lambda c: c in held)
src.poll_once(lambda c: c in held)
assert [i.intent_type for i in got] == [IntentType.START_LIVE], got

# Release and re-press fires again, on the right-hand modifiers.
src, got = source()
held = {CTRL_R, ALT_R}
src.poll_once(lambda c: c in held)
for present in (True, False, True):
    held.add(X) if present else held.discard(X)
    src.poll_once(lambda c: c in held)
assert [i.intent_type for i in got] == [IntentType.STOP, IntentType.STOP], got

# Without focus: navigation is refused, Stop is not.
src, got = source(focus=False)
held = {CTRL_L, ALT_L}
src.poll_once(lambda c: c in held)
held.add(N)
src.poll_once(lambda c: c in held)
assert got == [], got
held.discard(N); held.add(X)
src.poll_once(lambda c: c in held)
assert [i.intent_type for i in got] == [IntentType.STOP], got

# The legacy bare F-key still works.
src, got = source()
held = {F2}
src.poll_once(lambda c: c in held)
assert [i.intent_type for i in got] == [IntentType.STOP], got

# A modifier released while another window had the keyboard is not a hold.
# The poller re-reads physical state every pass, so a lost key-up cannot
# strand it: with Ctrl and Option genuinely up, N alone fires nothing.
src, got = source()
held = {CTRL_L, ALT_L}
src.poll_once(lambda c: c in held)
held.clear()
src.poll_once(lambda c: c in held)
held.add(N)
src.poll_once(lambda c: c in held)
assert got == [], got

# clear_held_keys() forgets the recognizer's state without inventing edges.
src, got = source()
held = {CTRL_L, ALT_L}
src.poll_once(lambda c: c in held)
src.clear_held_keys("focus-change")
assert src._recognizer.held_modifiers == frozenset(), "a phantom modifier survived"

print("ok")
"""


@pytest.mark.skipif(sys.platform == "win32", reason="this is the from-macOS direction")
def test_the_windows_chord_poller_behaves_the_same_as_the_mac_listener() -> None:
    """Structure and logic only; no Win32 call executes (plan 16.3).

    The recognizer is shared, so this is really checking the *adapter*: that
    polled level transitions become the key-down/key-up events the shared state
    machine expects, in both directions and on both modifier sides.
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
