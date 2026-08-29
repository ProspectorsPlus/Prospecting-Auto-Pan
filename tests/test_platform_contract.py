"""Platform-port contracts, import purity, and the opposite-OS import test.

An opposite-OS import test is useful and is **never** proof of native behavior
(plan 16.3). What it proves is structural: the module imports without its OS,
the protocol is satisfied, the keycode tables are complete, and the
release-only port genuinely has no way to press anything. Every behavioral
Windows row in the native matrix stays `pending`.
"""

from __future__ import annotations

import importlib
import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

from prospector_engine.contracts import InputKey, IntentType, MouseButton
from prospector_engine.ports import PlatformPort, ReleaseOnlyPort, current_platform_name

ROOT = Path(__file__).resolve().parent.parent


def _run_python(source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=60,
        check=False,
    )


# ---------------------------------------------------------------------------
# Import purity
# ---------------------------------------------------------------------------


def test_importing_the_engine_package_pulls_in_no_os_module() -> None:
    """Bug B1: the shared engine used to import Quartz unconditionally."""
    result = _run_python(
        "import sys\n"
        "import prospector_engine, prospector_engine.contracts, prospector_engine.ports\n"
        "forbidden = [m for m in ('Quartz', 'ApplicationServices', 'AppKit', 'pynput', 'cv2', "
        "'tkinter') if m in sys.modules]\n"
        "print(','.join(forbidden))\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", f"leaked imports: {result.stdout.strip()}"


def test_the_engine_service_module_imports_without_opencv_or_tk() -> None:
    result = _run_python(
        "import sys, prospector_engine.engine\n"
        "print(','.join(m for m in ('cv2', 'tkinter', 'Quartz') if m in sys.modules))\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_the_deadman_helper_does_not_import_tk_opencv_or_capture() -> None:
    """Plan 4.5: the helper must start even if the graphics stack is broken."""
    result = _run_python(
        "import sys, deadman\n"
        "print(','.join(m for m in ('tkinter', 'cv2', 'PIL', "
        "'prospector_engine.capture', 'prospector_engine.coordinator') if m in sys.modules))\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Opposite-OS import
# ---------------------------------------------------------------------------


def test_the_opposite_os_module_refuses_a_plain_import() -> None:
    other = "platform_win" if current_platform_name() == "macos" else "platform_mac"
    result = _run_python(f"import prospector_engine.{other}")
    assert result.returncode != 0
    assert "may only be imported on" in result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="this is the from-macOS direction")
def test_the_windows_port_can_be_imported_and_inspected_from_macos() -> None:
    """Structure only. Nothing here executes a Win32 call (plan 16.3)."""
    environment = dict(os.environ)
    environment["TREASURE_ALLOW_CROSS_PLATFORM_IMPORT"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from prospector_engine.platform_win import ("
            "  WindowsPlatformPort, WindowsReleaseOnlyPort, _WIN_SCANCODES, _WIN_HOTKEY_VK)\n"
            "from prospector_engine.contracts import InputKey\n"
            "from prospector_engine.bindings import ORDINARY_KEYS\n"
            "assert set(_WIN_SCANCODES) == set(InputKey)\n"
            "assert set(_WIN_HOTKEY_VK) == ORDINARY_KEYS\n"
            "port = WindowsPlatformPort()\n"
            "assert port.name == 'windows'\n"
            "assert not hasattr(WindowsReleaseOnlyPort, 'raw_key_down')\n"
            "print('ok')\n",
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=environment,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def _local_port_class() -> type:
    module = importlib.import_module(
        f"prospector_engine.platform_{current_platform_name()[:3]}"
    )
    return (
        module.MacPlatformPort
        if current_platform_name() == "macos"
        else module.WindowsPlatformPort
    )


def test_the_local_port_satisfies_the_platform_protocol() -> None:
    from prospector_engine.ports import create_platform_port

    port = create_platform_port()
    assert isinstance(port, PlatformPort)
    for name in (
        "focus_state",
        "window_geometry",
        "create_capture_source",
        "pin_client_rect",
        "raw_key_down",
        "raw_key_up",
        "raw_button_down",
        "raw_button_up",
        "raw_pointer_move_client",
        "raw_pointer_delta",
        "raw_scroll_lines",
        "cursor_client_px",
        "create_hotkey_source",
    ):
        assert callable(getattr(port, name)), name


def test_the_local_release_only_port_satisfies_its_protocol_and_cannot_press() -> None:
    from prospector_engine.ports import create_release_only_port

    port = create_release_only_port()
    assert isinstance(port, ReleaseOnlyPort)
    for forbidden in (
        "raw_key_down",
        "raw_button_down",
        "raw_pointer_delta",
        "pin_client_rect",
    ):
        assert not hasattr(port, forbidden), forbidden
    assert {member for member in dir(port) if "down" in member} == set()


def test_the_release_only_protocol_declares_no_press_method() -> None:
    members = {name for name, _ in inspect.getmembers(ReleaseOnlyPort)}
    assert not any("down" in name or "press" in name for name in members)


def test_every_vocabulary_key_has_a_native_code_on_this_platform() -> None:
    from prospector_engine.ports import create_platform_port

    port = create_platform_port()
    for key in InputKey:
        assert isinstance(port.key_code(key), int)
    for button in MouseButton:
        assert button.value in ("left", "right", "middle")


# ---------------------------------------------------------------------------
# Hotkeys (bug B4)
# ---------------------------------------------------------------------------


def test_every_advertised_chord_is_bound_on_this_platform() -> None:
    """B4: F5 was advertised in the docs but bound in neither listener.

    There is now one registry, so the failure mode is structurally gone: this
    checks that the platform's keycode table covers exactly what the registry
    names, in both directions.
    """
    from prospector_engine.bindings import BINDINGS, ORDINARY_KEYS

    if current_platform_name() == "macos":
        from prospector_engine.platform_mac import _MAC_HOTKEY_VK as table
    else:  # pragma: no cover - exercised on Windows
        from prospector_engine.platform_win import _WIN_HOTKEY_VK as table

    assert set(table) == ORDINARY_KEYS
    assert {b.intent for b in BINDINGS} == {
        IntentType.START_LIVE,
        IntentType.START_SHADOW,
        IntentType.STOP,
        IntentType.PIXEL_INFO,
        IntentType.RESET_CHARACTER,
        IntentType.PAN_SWAP_TEST,
        IntentType.DIG_LOOP,
    }


def test_input_emitting_hotkeys_require_positive_focus() -> None:
    from prospector_engine.contracts import RuntimeIntent

    if current_platform_name() == "macos":
        from prospector_engine.platform_mac import MacHotkeySource as Source
    else:  # pragma: no cover - exercised on Windows
        from prospector_engine.platform_win import WindowsHotkeySource as Source

    from prospector_engine.bindings import binding_for_intent

    submitted: list[RuntimeIntent] = []
    focus: list[bool | None] = [False]
    source = Source(submitted.append, focus_probe=lambda: focus[0])

    def chord(intent: IntentType) -> bool:
        binding = binding_for_intent(intent)
        assert binding is not None
        return source.dispatch(binding)

    for intent in (
        IntentType.START_LIVE,
        IntentType.RESET_CHARACTER,
        IntentType.PAN_SWAP_TEST,
        IntentType.DIG_LOOP,
    ):
        assert chord(intent) is False
        # Refused, but *heard*: the chord is journalled before policy runs, so
        # "did it even see me press it" always has an answer.
        assert source.health().last_chord == binding_for_intent(intent).label(  # type: ignore[union-attr]
            "darwin" if current_platform_name() == "macos" else "win32"
        )
    assert submitted == []

    focus[0] = True
    assert chord(IntentType.RESET_CHARACTER) is True
    assert submitted[-1].source == "hotkey"


def test_stop_is_accepted_even_without_focus() -> None:
    """A Stop that needed focus would fail exactly when it is needed."""
    from prospector_engine.contracts import RuntimeIntent

    if current_platform_name() == "macos":
        from prospector_engine.platform_mac import MacHotkeySource as Source
    else:  # pragma: no cover - exercised on Windows
        from prospector_engine.platform_win import WindowsHotkeySource as Source

    submitted: list[RuntimeIntent] = []
    source = Source(submitted.append, focus_probe=lambda: None)

    assert source.fire(IntentType.STOP) is True
    assert submitted[0].intent_type is IntentType.STOP


# ---------------------------------------------------------------------------
# macOS geometry (this machine only)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS geometry")
def test_the_mac_port_reports_client_geometry_in_logical_units() -> None:
    """Bug B11 and D-017. Read-only: it inspects metadata and moves nothing.

    Skips cleanly when Roblox is not running, because that is a fact about the
    machine and not a failure of the code.
    """
    from prospector_engine.geometry import ViewportState
    from prospector_engine.platform_mac import TITLE_BAR_FALLBACK_PT, MacPlatformPort

    port = MacPlatformPort()
    geometry = port.window_geometry()
    if geometry.state is ViewportState.INVALID:
        pytest.skip("Roblox is not running; E-VIEW on macOS remains pending")

    assert geometry.valid
    assert geometry.client_logical is not None
    assert geometry.frame_logical is not None
    assert geometry.display is not None

    # The client is the frame minus the title bar, in POINTS.
    inset_left, inset_top, _r, _b = geometry.client_insets_logical
    assert inset_left == pytest.approx(0.0)
    assert 12.0 <= inset_top <= 80.0
    assert geometry.client_logical.height == pytest.approx(
        geometry.frame_logical.height - inset_top
    )
    assert port.title_bar_measured or port.title_bar_pt == TITLE_BAR_FALLBACK_PT

    # Backing pixels are derived, never the unit the API was given.
    scale = geometry.backing_scale
    assert scale >= 1.0
    assert geometry.client_backing_px == (
        round(geometry.client_logical.width * scale),
        round(geometry.client_logical.height * scale),
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS geometry")
def test_the_mac_client_rect_stays_inside_its_display() -> None:
    """The captured region must be a real part of the screen, not past its edge.

    This is the property the coordinate bug violated: physical-pixel values
    handed to a logical-unit API described a rectangle twice the size of the
    display, so the capture ran off the edge into the desktop.
    """
    from prospector_engine.geometry import ViewportState
    from prospector_engine.platform_mac import MacPlatformPort

    geometry = MacPlatformPort().window_geometry()
    if geometry.state is ViewportState.INVALID:
        pytest.skip("Roblox is not running")
    assert geometry.client_logical is not None and geometry.display is not None
    client, bounds = geometry.client_logical, geometry.display.bounds_logical
    assert client.width <= bounds.width + 1.0
    assert client.height <= bounds.height + 1.0
    assert client.x >= bounds.x - 1.0
    assert client.bottom <= bounds.bottom + 1.0


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS scale")
def test_the_mac_port_computes_scale_from_the_display_mode() -> None:
    from prospector_engine.platform_mac import MacPlatformPort

    scale = MacPlatformPort._scale_for_display(0)
    assert scale >= 1.0


@pytest.mark.skipif(sys.platform == "win32", reason="this is the from-macOS direction")
def test_the_windows_turn_keys_carry_the_extended_flag() -> None:
    """The one Windows detail that is either right or sends numpad 4/6.

    Left and Right share their scancodes with the numeric keypad; only the E0
    prefix - `KEYEVENTF_EXTENDEDKEY` - distinguishes them. This checks the
    structure from macOS. It cannot check what Roblox receives; only a Windows
    machine can, and that check is listed as pending in STATUS.md.
    """
    environment = dict(os.environ)
    environment["TREASURE_ALLOW_CROSS_PLATFORM_IMPORT"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from prospector_engine.platform_win import ("
            "  KEYEVENTF_EXTENDEDKEY, _WIN_EXTENDED, _WIN_SCANCODES, _key_input)\n"
            "from prospector_engine.contracts import InputKey\n"
            "assert KEYEVENTF_EXTENDEDKEY == 0x0001\n"
            "turns = {_WIN_SCANCODES[InputKey.LEFT], _WIN_SCANCODES[InputKey.RIGHT]}\n"
            "assert turns == _WIN_EXTENDED, (turns, _WIN_EXTENDED)\n"
            "assert _WIN_SCANCODES[InputKey.LEFT] == 0x4B\n"
            "assert _WIN_SCANCODES[InputKey.RIGHT] == 0x4D\n"
            "for key in InputKey:\n"
            "    code = _WIN_SCANCODES[key]\n"
            "    for up in (False, True):\n"
            "        flags = _key_input(code, up).u.ki.dwFlags\n"
            "        extended = bool(flags & KEYEVENTF_EXTENDEDKEY)\n"
            "        assert extended == (code in _WIN_EXTENDED), (key, up, flags)\n"
            "print('ok')\n",
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=environment,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_the_turn_keys_are_in_the_release_floor_on_every_platform() -> None:
    """Adding a key that can be pressed must add a key that is released."""
    from prospector_engine.contracts import InputKey, InputVocabulary

    vocabulary = InputVocabulary()
    assert InputKey.LEFT in vocabulary.keys
    assert InputKey.RIGHT in vocabulary.keys
    assert InputKey.LEFT.is_turn and InputKey.RIGHT.is_turn
    assert not InputKey.A.is_turn, "strafing is not turning"
    assert not InputKey.W.is_turn


# ---------------------------------------------------------------------------
# The movement primitives, against the implementation already proven in the
# field by Prospector Lite
# ---------------------------------------------------------------------------


#: Prospector Lite's `V3_KEYCODES` table for the keys Treasure shares with it,
#: transcribed here as a literal. Lite has driven a Roblox character with these
#: numbers for months, which makes them the one part of the movement path with
#: real field evidence behind it. Treasure must not drift from them, and a test
#: that imported Lite's table would drift silently the day Lite moved.
_LITE_MAC_KEYCODES = {
    "w": 13,
    "a": 0,
    "s": 1,
    "d": 2,
    "space": 49,
    "shift": 56,
    "escape": 53,
    "1": 18,
    "2": 19,
}


@pytest.mark.skipif(current_platform_name() != "macos", reason="macOS keycodes")
def test_the_mac_keycodes_match_the_implementation_proven_in_the_field() -> None:
    """The keycodes are not where the movement bug was, and must stay so.

    Treasure's forward key was already the same virtual keycode, posted
    through the same Quartz call, as the one that has been walking a character
    in Prospector Lite for months. Recording that here means the next person
    reading "the character does not move" can rule this out in one test run
    rather than by rediscovering it.
    """
    from prospector_engine.contracts import InputKey
    from prospector_engine.platform_mac import _MAC_KEYCODES

    for name, expected in _LITE_MAC_KEYCODES.items():
        key = InputKey(name)
        assert _MAC_KEYCODES[key] == expected, f"{name} drifted from the proven keycode"


@pytest.mark.skipif(current_platform_name() != "macos", reason="macOS event fields")
def test_the_mac_yaw_delta_sets_the_fields_roblox_actually_reads() -> None:
    """A camera delta lives in ``kCGMouseEventDeltaX``, not in the location.

    An absolute move does nothing to a Shift-Locked camera even while a button
    is genuinely held; Roblox reads the HID delta fields. This asserts the
    signed value that reaches ``CGEventSetIntegerValueField`` for both
    directions, with and without a held button, without posting anything.
    """
    import Quartz

    from prospector_engine.contracts import MouseButton
    from prospector_engine.platform_mac import MacPlatformPort

    recorded: list[tuple[int, int]] = []
    posted: list[object] = []

    def fake_set(event: object, field: int, value: int) -> None:
        recorded.append((field, int(value)))

    port = MacPlatformPort()
    original_set = Quartz.CGEventSetIntegerValueField
    original_post = Quartz.CGEventPost
    Quartz.CGEventSetIntegerValueField = fake_set  # type: ignore[assignment]
    Quartz.CGEventPost = lambda *_args: posted.append(_args)  # type: ignore[assignment]
    try:
        for dx in (12, -12):
            for button in (None, MouseButton.RIGHT):
                recorded.clear()
                port.raw_pointer_delta(dx, 0, button)
                fields = dict(recorded)
                assert fields[Quartz.kCGMouseEventDeltaX] == dx, (dx, button, fields)
                assert fields[Quartz.kCGMouseEventDeltaY] == 0
    finally:
        Quartz.CGEventSetIntegerValueField = original_set  # type: ignore[assignment]
        Quartz.CGEventPost = original_post  # type: ignore[assignment]

    assert len(posted) == 4, "one event per request, and no more"


def test_the_windows_yaw_delta_is_relative_and_signed() -> None:
    """``MOUSEEVENTF_MOVE`` without ``ABSOLUTE`` is the relative camera delta.

    Structure only, inspected from macOS. What Roblox does with it can only be
    checked on Windows, and that check is listed as pending in STATUS.md.
    """
    environment = dict(os.environ)
    environment["TREASURE_ALLOW_CROSS_PLATFORM_IMPORT"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from prospector_engine.platform_win import ("
            "  MOUSEEVENTF_MOVE, _mouse_input)\n"
            "assert MOUSEEVENTF_MOVE == 0x0001\n"
            "for dx in (12, -12):\n"
            "    mi = _mouse_input(MOUSEEVENTF_MOVE, dx=dx, dy=0).u.mi\n"
            "    assert mi.dx == dx, (dx, mi.dx)\n"
            "    assert mi.dy == 0\n"
            "    assert mi.dwFlags == MOUSEEVENTF_MOVE, mi.dwFlags\n"
            "    assert not (mi.dwFlags & 0x8000), 'ABSOLUTE would not turn the camera'\n"
            "print('ok')\n",
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=environment,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_the_windows_movement_keys_are_hardware_scancodes() -> None:
    """Scancodes, not virtual keys: a game reading raw input sees these."""
    environment = dict(os.environ)
    environment["TREASURE_ALLOW_CROSS_PLATFORM_IMPORT"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from prospector_engine.platform_win import ("
            "  KEYEVENTF_SCANCODE, _WIN_SCANCODES, _key_input)\n"
            "from prospector_engine.contracts import InputKey\n"
            "assert KEYEVENTF_SCANCODE == 0x0008\n"
            "assert _WIN_SCANCODES[InputKey.W] == 0x11\n"
            "assert _WIN_SCANCODES[InputKey.A] == 0x1E\n"
            "assert _WIN_SCANCODES[InputKey.S] == 0x1F\n"
            "assert _WIN_SCANCODES[InputKey.D] == 0x20\n"
            "for key in InputKey:\n"
            "    flags = _key_input(_WIN_SCANCODES[key], False).u.ki.dwFlags\n"
            "    assert flags & KEYEVENTF_SCANCODE, key\n"
            "print('ok')\n",
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=environment,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
