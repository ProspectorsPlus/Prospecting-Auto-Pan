"""Windows platform port: SendInput, Per-Monitor V2 client geometry, hotkeys.

Instance based, with no ``_eng`` module binding and no held-input set of its
own (bugs B1, B7, B8). All geometry is the **client** rect in physical pixels,
which is what ``GetClientRect`` + ``ClientToScreen`` already give a
Per-Monitor-V2 process.

**Native status: PENDING.** Nothing in this module has been executed on
Windows during this implementation - the development machine is macOS. It is
written against the documented Win32 contracts and is covered locally only by
the mocked-ctypes import/contract test. Plan 16.3's Windows column stays
``pending`` until the owner runs the native gates.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes

if sys.platform != "win32" and not os.environ.get("TREASURE_ALLOW_CROSS_PLATFORM_IMPORT"):
    raise ImportError(
        "prospector_engine.platform_win may only be imported on Windows. "
        "Set TREASURE_ALLOW_CROSS_PLATFORM_IMPORT=1 with a mocked ctypes.windll "
        "to run the opposite-OS import contract test (plan 16.3)."
    )

from prospector_engine.contracts import (
    ClientRectPhysicalPx,
    FocusState,
    InputKey,
    InputVocabulary,
    IntentType,
    MouseButton,
    PinResult,
    RuntimeIntent,
    monotonic_s,
)

__all__ = [
    "WIN_HOTKEY_BINDINGS",
    "WindowsHotkeySource",
    "WindowsPlatformPort",
    "WindowsReleaseOnlyPort",
    "declare_per_monitor_v2",
]

# Hardware scancodes - the injection path Roblox actually honours on Windows.
_WIN_SCANCODES: dict[InputKey, int] = {
    InputKey.W: 0x11,
    InputKey.A: 0x1E,
    InputKey.S: 0x1F,
    InputKey.D: 0x20,
    InputKey.SPACE: 0x39,
    InputKey.SHIFT: 0x2A,
    InputKey.ESCAPE: 0x01,
    InputKey.DIGIT_1: 0x02,
    InputKey.DIGIT_2: 0x03,
}

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_WHEEL = 0x0800
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

_WIN_BUTTON_FLAGS: dict[MouseButton, tuple[int, int]] = {
    MouseButton.LEFT: (0x0002, 0x0004),
    MouseButton.RIGHT: (0x0008, 0x0010),
    MouseButton.MIDDLE: (0x0020, 0x0040),
}

GWL_STYLE = -16
GWL_EXSTYLE = -20
SW_RESTORE = 9
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

WIN_HOTKEY_BINDINGS: dict[str, IntentType] = {
    "f1": IntentType.START_LIVE,
    "f2": IntentType.STOP,
    "f3": IntentType.PIXEL_INFO,
    "f4": IntentType.RESET_CHARACTER,
    "f5": IntentType.PAN_SWAP_TEST,
    "f6": IntentType.DIG_LOOP,
}
_WIN_HOTKEY_VK: dict[str, int] = {
    "f1": 0x70,
    "f2": 0x71,
    "f3": 0x72,
    "f4": 0x73,
    "f5": 0x74,
    "f6": 0x75,
}

ULONG_PTR = wintypes.WPARAM


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT))


class _INPUT(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD), ("u", _INPUTUNION))


def declare_per_monitor_v2() -> str:
    """Opt this process into Per-Monitor V2 DPI awareness (plan 4.1).

    Returns which mechanism succeeded so the UI can show it. The packaged
    build additionally declares awareness in its manifest; this call is the
    source-run equivalent and is harmless when the manifest already applied.
    """
    with contextlib.suppress(Exception):
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(
            DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        ):
            return "per-monitor-v2"
    with contextlib.suppress(Exception):
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per-monitor"
    with contextlib.suppress(Exception):
        ctypes.windll.user32.SetProcessDPIAware()
        return "system"
    # Reached when no DPI API answered - including the cross-platform import
    # test, where ``ctypes.windll`` does not exist at all.
    return "none"


def _send(inp: _INPUT) -> None:
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def _key_input(scancode: int, up: bool) -> _INPUT:
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    return _INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=_KEYBDINPUT(0, scancode, flags, 0, 0)))


def _mouse_input(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> _INPUT:
    return _INPUT(type=INPUT_MOUSE, u=_INPUTUNION(mi=_MOUSEINPUT(dx, dy, data, flags, 0, 0)))


class WindowsReleaseOnlyPort:
    """Release-only backend for the deadman helper. No down-edge exists here."""

    def __init__(self) -> None:
        self._vocabulary = InputVocabulary()

    @property
    def vocabulary(self) -> InputVocabulary:
        return self._vocabulary

    def key_code(self, key: InputKey) -> int:
        return _WIN_SCANCODES[key]

    def raw_key_up(self, code: int) -> None:
        _send(_key_input(code, up=True))

    def raw_button_up(self, button: MouseButton) -> None:
        _send(_mouse_input(_WIN_BUTTON_FLAGS[button][1]))


class WindowsPlatformPort:
    """Full Windows port. Only ``InputAuthority`` and capture may hold one."""

    def __init__(self, *, window_title_substring: str = "Roblox") -> None:
        self._vocabulary = InputVocabulary()
        self._title_substring = window_title_substring
        self._lock = threading.Lock()
        self._last_rect: ClientRectPhysicalPx | None = None
        self._last_hwnd: int | None = None
        self._dpi_mode = declare_per_monitor_v2()

    @property
    def name(self) -> str:
        return "windows"

    @property
    def vocabulary(self) -> InputVocabulary:
        return self._vocabulary

    @property
    def dpi_mode(self) -> str:
        return self._dpi_mode

    def key_code(self, key: InputKey) -> int:
        return _WIN_SCANCODES[key]

    # -- window lookup ----------------------------------------------------
    def _scan_roblox(self) -> tuple[int, int, int, int, int] | None:
        """Largest visible Roblox client window: ``(hwnd, x, y, w, h)`` physical px."""
        user32 = ctypes.windll.user32
        found: list[tuple[int, int, int, int, int, int]] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _callback(hwnd: int, _lparam: int) -> bool:
            with contextlib.suppress(Exception):
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                title_buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, title_buffer, length + 1)
                title = title_buffer.value or ""
                class_buffer = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, class_buffer, 256)
                class_name = class_buffer.value or ""
                is_client = class_name == "WINDOWSCLIENT" or (
                    self._title_substring in title and "Studio" not in title
                )
                if not is_client:
                    return True
                rect = wintypes.RECT()
                if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
                    return True
                width = rect.right - rect.left
                height = rect.bottom - rect.top
                if width < 320 or height < 240:
                    return True
                point = wintypes.POINT(0, 0)
                user32.ClientToScreen(hwnd, ctypes.byref(point))
                found.append(
                    (
                        width * height,
                        int(hwnd),
                        int(point.x),
                        int(point.y),
                        int(width),
                        int(height),
                    )
                )
            return True

        with contextlib.suppress(Exception):
            user32.EnumWindows(_callback, 0)
        if not found:
            return None
        found.sort(key=lambda entry: entry[0], reverse=True)
        return found[0][1:]

    def _dpi_for_window(self, hwnd: int) -> int:
        with contextlib.suppress(Exception):
            return int(ctypes.windll.user32.GetDpiForWindow(hwnd))
        return 96

    def _monitor_id(self, hwnd: int) -> str:
        with contextlib.suppress(Exception):
            return str(int(ctypes.windll.user32.MonitorFromWindow(hwnd, 2)))  # NEAREST
        return "unknown"

    # -- PlatformPort: viewport ------------------------------------------
    def focus_state(self) -> FocusState:
        with contextlib.suppress(Exception):
            foreground = int(ctypes.windll.user32.GetForegroundWindow())
            if not foreground:
                return None
            found = self._scan_roblox()
            if found is None:
                return False
            return foreground == found[0]
        return None

    def find_client_rect(self) -> ClientRectPhysicalPx | None:
        found = self._scan_roblox()
        if found is None:
            with self._lock:
                self._last_rect = None
                self._last_hwnd = None
            return None
        hwnd, x_px, y_px, w_px, h_px = found
        # A Per-Monitor-V2 process already receives physical pixels, so this
        # scale is reported for diagnostics only and is never used to convert
        # a coordinate on this platform (contrast platform_mac, where CGEvent
        # takes points).
        scale = self._dpi_for_window(hwnd) / 96.0
        rect = ClientRectPhysicalPx(
            origin_px=(x_px, y_px),
            size_px=(w_px, h_px),
            scale=scale,
            verified_at_s=monotonic_s(),
            display_id=self._monitor_id(hwnd),
            valid=w_px > 0 and h_px > 0,
            invalid_reason=None if w_px > 0 and h_px > 0 else "non-positive client size",
        )
        with self._lock:
            self._last_rect = rect
            self._last_hwnd = hwnd
        return rect

    def pin_client_rect(self, size_px: tuple[int, int]) -> PinResult:
        user32 = ctypes.windll.user32
        found = self._scan_roblox()
        if found is None:
            return PinResult(False, "Roblox window not found. Open Roblox (not minimized).")
        hwnd = found[0]
        with contextlib.suppress(Exception):
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        exstyle = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        rect = wintypes.RECT(0, 0, size_px[0], size_px[1])
        dpi = self._dpi_for_window(hwnd)
        adjusted = False
        with contextlib.suppress(Exception):
            adjusted = bool(
                user32.AdjustWindowRectExForDpi(ctypes.byref(rect), style, False, exstyle, dpi)
            )
        if not adjusted and not user32.AdjustWindowRectEx(
            ctypes.byref(rect), style, False, exstyle
        ):
            return PinResult(False, "AdjustWindowRectEx(ForDpi) failed.")
        outer_w = rect.right - rect.left
        outer_h = rect.bottom - rect.top
        if not user32.SetWindowPos(
            hwnd, 0, rect.left, rect.top, outer_w, outer_h, SWP_NOZORDER | SWP_NOACTIVATE
        ):
            return PinResult(False, "SetWindowPos failed.")

        readback = self.find_client_rect()
        if readback is None or not readback.valid:
            return PinResult(False, "Pinned, but the client rect could not be read back.")
        dw = abs(readback.width_px - size_px[0])
        dh = abs(readback.height_px - size_px[1])
        if dw > 1 or dh > 1:
            return PinResult(
                False,
                f"Client readback {readback.size_px} differs from requested {size_px} "
                f"by ({dw},{dh}) px - outside the one-pixel contract.",
                readback,
            )
        return PinResult(
            True,
            f"Client area pinned to {readback.width_px}x{readback.height_px} px at "
            f"{readback.origin_px} (DPI {dpi}, {self._dpi_mode}).",
            readback,
        )

    # -- PlatformPort: raw edges -----------------------------------------
    def raw_key_down(self, code: int) -> None:
        _send(_key_input(code, up=False))

    def raw_key_up(self, code: int) -> None:
        _send(_key_input(code, up=True))

    def raw_button_down(self, button: MouseButton) -> None:
        _send(_mouse_input(_WIN_BUTTON_FLAGS[button][0]))

    def raw_button_up(self, button: MouseButton) -> None:
        _send(_mouse_input(_WIN_BUTTON_FLAGS[button][1]))

    def raw_pointer_move_client(self, point_px: tuple[int, int]) -> None:
        with self._lock:
            rect = self._last_rect
        if rect is None:
            rect = self.find_client_rect()
        if rect is None or not rect.valid:
            return
        screen_x_px, screen_y_px = rect.to_screen_px(point_px)
        with contextlib.suppress(Exception):
            ctypes.windll.user32.SetCursorPos(int(screen_x_px), int(screen_y_px))

    def raw_pointer_delta(
        self, dx: int, dy: int, held_button: MouseButton | None = None
    ) -> None:
        """Relative motion. ``held_button`` is accepted for API symmetry with
        macOS but is not needed here: ``MOUSEEVENTF_MOVE`` already delivers a
        relative delta that Roblox's camera reads while a button is held."""
        del held_button
        _send(_mouse_input(MOUSEEVENTF_MOVE, dx=int(dx), dy=int(dy)))

    def raw_scroll_lines(self, lines: int) -> None:
        data = (120 * int(lines)) & 0xFFFFFFFF
        _send(_mouse_input(MOUSEEVENTF_WHEEL, data=data))

    def cursor_client_px(self) -> tuple[int, int] | None:
        rect = self.find_client_rect()
        if rect is None or not rect.valid:
            return None
        point = wintypes.POINT(0, 0)
        with contextlib.suppress(Exception):
            ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
        client_x = int(point.x) - rect.origin_px[0]
        client_y = int(point.y) - rect.origin_px[1]
        if not rect.contains_client_point((client_x, client_y)):
            return None
        return (client_x, client_y)

    # -- PlatformPort: hotkeys -------------------------------------------
    def create_hotkey_source(
        self, submit: Callable[[RuntimeIntent], None]
    ) -> WindowsHotkeySource:
        return WindowsHotkeySource(submit, focus_probe=self.focus_state)


class WindowsHotkeySource:
    """``GetAsyncKeyState`` edge-detecting poller for F1-F5 (bug B4).

    Polling avoids conflicting with our own synthetic input and needs no third
    party library. It submits intents only; it never touches engine state.
    """

    ALWAYS_ALLOWED = frozenset({IntentType.STOP, IntentType.SHUTDOWN, IntentType.PIXEL_INFO})
    POLL_INTERVAL_S = 0.03

    def __init__(
        self,
        submit: Callable[[RuntimeIntent], None],
        *,
        focus_probe: Callable[[], FocusState],
    ) -> None:
        self._submit = submit
        self._focus_probe = focus_probe
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._sequence = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="treasure-hotkeys", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        user32 = ctypes.windll.user32
        previous = dict.fromkeys(WIN_HOTKEY_BINDINGS, False)
        while not self._stop.wait(self.POLL_INTERVAL_S):
            for name, intent_type in WIN_HOTKEY_BINDINGS.items():
                pressed = bool(user32.GetAsyncKeyState(_WIN_HOTKEY_VK[name]) & 0x8000)
                if pressed and not previous[name]:
                    self.fire(intent_type)
                previous[name] = pressed

    def fire(self, intent_type: IntentType) -> bool:
        if intent_type not in self.ALWAYS_ALLOWED and self._focus_probe() is not True:
            return False
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        self._submit(
            RuntimeIntent(
                sequence=sequence,
                intent_type=intent_type,
                source="hotkey",
                created_at_s=monotonic_s(),
            )
        )
        return True

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=1.0)

    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())
