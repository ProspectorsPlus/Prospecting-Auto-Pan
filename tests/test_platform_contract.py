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
            "  WIN_HOTKEY_BINDINGS, WindowsPlatformPort, WindowsReleaseOnlyPort, _WIN_SCANCODES)\n"
            "from prospector_engine.contracts import InputKey\n"
            "assert set(_WIN_SCANCODES) == set(InputKey)\n"
            "assert sorted(WIN_HOTKEY_BINDINGS) == ['f1','f2','f3','f4','f5','f6']\n"
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
        "find_client_rect",
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


def test_all_five_hotkeys_are_bound_on_this_platform() -> None:
    """B4: F5 was advertised in the docs but bound in neither listener."""
    if current_platform_name() == "macos":
        from prospector_engine.platform_mac import MAC_HOTKEY_BINDINGS as bindings
    else:  # pragma: no cover - exercised on Windows
        from prospector_engine.platform_win import WIN_HOTKEY_BINDINGS as bindings

    assert sorted(bindings) == ["f1", "f2", "f3", "f4", "f5", "f6"]
    assert set(bindings.values()) == {
        IntentType.START_LIVE,
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

    submitted: list[RuntimeIntent] = []
    focus: list[bool | None] = [False]
    source = Source(submitted.append, focus_probe=lambda: focus[0])

    assert source.fire(IntentType.START_LIVE) is False
    assert source.fire(IntentType.RESET_CHARACTER) is False
    assert source.fire(IntentType.PAN_SWAP_TEST) is False
    assert source.fire(IntentType.DIG_LOOP) is False
    assert submitted == []

    focus[0] = True
    assert source.fire(IntentType.RESET_CHARACTER) is True
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
def test_the_mac_port_reports_client_geometry_not_window_frame() -> None:
    """Bug B11. Read-only: it inspects window metadata and moves nothing.

    Skips cleanly when Roblox is not running, because that is a fact about the
    machine and not a failure of the code.
    """
    from prospector_engine.platform_mac import TITLE_BAR_FALLBACK_PT, MacPlatformPort

    port = MacPlatformPort()
    rect = port.find_client_rect()
    if rect is None:
        pytest.skip("Roblox is not running; E-VIEW on macOS remains pending")

    assert rect.valid
    assert rect.scale >= 1.0
    assert 12.0 <= port.title_bar_pt <= 80.0
    # A measured inset is the good path; the documented fallback is still legal
    # and the port says which one it used.
    assert port.title_bar_measured or port.title_bar_pt == TITLE_BAR_FALLBACK_PT
    assert rect.origin_px[1] >= 0


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS scale")
def test_the_mac_port_computes_scale_from_the_display_mode() -> None:
    from prospector_engine.platform_mac import MacPlatformPort

    scale = MacPlatformPort._scale_for_display(0)
    assert scale >= 1.0
