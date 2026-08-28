"""Windows platform port: SendInput, Per-Monitor V2 client geometry, hotkeys.

Instance based, with no ``_eng`` module binding and no held-input set of its
own (bugs B1, B7, B8). All geometry is the **client** rect in physical pixels,
which is what ``GetClientRect`` + ``ClientToScreen`` already give a
Per-Monitor-V2 process.

**Coordinate discipline.** A Per-Monitor-V2 process receives *device pixels*
from every window API, so on Windows the logical space and the backing space
coincide numerically: ``DisplayInfo.backing_scale`` is 1.0 and the user's UI
scaling is reported separately as ``dpi_scale``. That is the opposite of macOS,
where points and backing pixels differ by the Retina factor - which is exactly
why the two are never mixed in a transform (see
:mod:`prospector_engine.geometry`).

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
from typing import Any

if sys.platform != "win32" and not os.environ.get("TREASURE_ALLOW_CROSS_PLATFORM_IMPORT"):
    raise ImportError(
        "prospector_engine.platform_win may only be imported on Windows. "
        "Set TREASURE_ALLOW_CROSS_PLATFORM_IMPORT=1 with a mocked ctypes.windll "
        "to run the opposite-OS import contract test (plan 16.3)."
    )


import numpy as np

from prospector_engine.contracts import (
    FocusState,
    InputKey,
    InputVocabulary,
    IntentType,
    MouseButton,
    PinResult,
    RawFrame,
    RuntimeIntent,
    monotonic_s,
)
from prospector_engine.geometry import (
    CANONICAL_SIZE_PX,
    DisplayInfo,
    LogicalRect,
    ViewportGeometry,
    ViewportState,
    WindowIdentity,
)

__all__ = [
    "WIN_HOTKEY_BINDINGS",
    "WindowsHotkeySource",
    "WindowsPlatformPort",
    "WindowsPrintWindowSource",
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
        self._geometry = ViewportGeometry.unpinned()
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
    def _scan_roblox(self) -> tuple[WindowIdentity, LogicalRect, LogicalRect] | None:
        """Largest visible Roblox client window.

        Returns ``(identity, frame_rect, client_rect)`` where both rectangles
        are display-absolute device pixels. ``GetClientRect`` gives the client
        size directly and ``ClientToScreen`` its origin, so unlike macOS there
        is no inset to measure - the client area is what the API reports.
        """
        try:
            user32 = ctypes.windll.user32
        except AttributeError:
            # Only reachable from the opposite-OS import test, where the
            # contract is that geometry is reported INVALID, never raised.
            return None
        found: list[tuple[int, WindowIdentity, LogicalRect, LogicalRect]] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _callback(hwnd: int, _lparam: int) -> bool:
            with contextlib.suppress(Exception):
                if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
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
                client = wintypes.RECT()
                if not user32.GetClientRect(hwnd, ctypes.byref(client)):
                    return True
                width = client.right - client.left
                height = client.bottom - client.top
                if width < 320 or height < 240:
                    return True
                origin = wintypes.POINT(0, 0)
                user32.ClientToScreen(hwnd, ctypes.byref(origin))
                outer = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(outer))
                process_id = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
                found.append(
                    (
                        width * height,
                        WindowIdentity(
                            window_id=int(hwnd),
                            process_id=int(process_id.value),
                            owner=class_name or "Roblox",
                            title=title,
                        ),
                        LogicalRect(
                            float(outer.left),
                            float(outer.top),
                            float(outer.right - outer.left),
                            float(outer.bottom - outer.top),
                        ),
                        LogicalRect(
                            float(origin.x), float(origin.y), float(width), float(height)
                        ),
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
            return foreground == found[0].window_id
        return None

    def window_geometry(self) -> ViewportGeometry:
        found = self._scan_roblox()
        if found is None:
            geometry = ViewportGeometry.invalid("Roblox window not found or minimized")
            with self._lock:
                self._geometry = geometry
            return geometry
        identity, frame_logical, client_logical = found
        dpi = self._dpi_for_window(identity.window_id)
        display = DisplayInfo(
            display_id=self._monitor_id(identity.window_id),
            bounds_logical=self._monitor_bounds(identity.window_id),
            # Per-Monitor V2 already hands us device pixels, so there is no
            # further backing conversion; the UI scale is diagnostic only.
            backing_scale=1.0,
            dpi_scale=dpi / 96.0,
        )
        canonical_w, canonical_h = CANONICAL_SIZE_PX
        is_canonical = (
            abs(client_logical.width - canonical_w) <= 1.0
            and abs(client_logical.height - canonical_h) <= 1.0
        )
        geometry = ViewportGeometry(
            state=(
                ViewportState.CANONICAL_VERIFIED
                if is_canonical
                else ViewportState.ADOPTED_NONCANONICAL
            ),
            window=identity,
            display=display,
            frame_logical=frame_logical,
            client_logical=client_logical,
            canonical_px=CANONICAL_SIZE_PX,
            verified_at_s=monotonic_s(),
            detail=(
                f"client matches the canonical size ({self._dpi_mode}, {dpi} DPI)"
                if is_canonical
                else f"client is {client_logical.width:g}x{client_logical.height:g} px, "
                f"not the canonical {canonical_w}x{canonical_h} ({dpi} DPI)"
            ),
        )
        with self._lock:
            self._geometry = geometry
        return geometry

    def _monitor_bounds(self, hwnd: int) -> LogicalRect:
        """Bounds of the monitor showing ``hwnd``; may have a negative origin."""

        class _MONITORINFO(ctypes.Structure):
            _fields_ = (
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            )

        with contextlib.suppress(Exception):
            monitor = ctypes.windll.user32.MonitorFromWindow(hwnd, 2)
            info = _MONITORINFO()
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            if ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                rect = info.rcMonitor
                return LogicalRect(
                    float(rect.left),
                    float(rect.top),
                    float(rect.right - rect.left),
                    float(rect.bottom - rect.top),
                )
        return LogicalRect(0.0, 0.0, 0.0, 0.0)

    def pin_client_rect(self, size_logical: tuple[float, float]) -> PinResult:
        """Resize so the **client area** is ``size_logical`` device pixels.

        ``AdjustWindowRectExForDpi`` converts the desired client rect into the
        outer rect ``SetWindowPos`` needs, which is what keeps the border and
        title bar from eating into the client at non-100% scaling.
        """
        try:
            user32 = ctypes.windll.user32
        except AttributeError:  # opposite-OS import test only
            return PinResult(
                False,
                "Win32 window APIs are unavailable on this OS.",
                self.window_geometry(),
                size_logical,
            )
        found = self._scan_roblox()
        if found is None:
            return PinResult(
                False,
                "Roblox window not found. Open Roblox, not minimized.",
                self.window_geometry(),
                size_logical,
            )
        identity = found[0]
        hwnd = identity.window_id
        with contextlib.suppress(Exception):
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        exstyle = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        rect = wintypes.RECT(0, 0, round(size_logical[0]), round(size_logical[1]))
        dpi = self._dpi_for_window(hwnd)
        adjusted = False
        with contextlib.suppress(Exception):
            adjusted = bool(
                user32.AdjustWindowRectExForDpi(ctypes.byref(rect), style, False, exstyle, dpi)
            )
        if not adjusted and not user32.AdjustWindowRectEx(
            ctypes.byref(rect), style, False, exstyle
        ):
            return PinResult(
                False,
                "AdjustWindowRectEx(ForDpi) failed.",
                self.window_geometry(),
                size_logical,
            )
        outer_w = rect.right - rect.left
        outer_h = rect.bottom - rect.top
        if not user32.SetWindowPos(
            hwnd, 0, rect.left, rect.top, outer_w, outer_h, SWP_NOZORDER | SWP_NOACTIVATE
        ):
            return PinResult(
                False, "SetWindowPos failed.", self.window_geometry(), size_logical
            )

        geometry = self.window_geometry()
        if not geometry.valid or geometry.client_logical is None:
            return PinResult(
                False,
                "Pinned, but the client rect could not be read back.",
                geometry,
                size_logical,
            )
        client = geometry.client_logical
        delta_w = abs(client.width - size_logical[0])
        delta_h = abs(client.height - size_logical[1])
        if delta_w > 1.0 or delta_h > 1.0:
            return PinResult(
                False,
                f"Requested a {size_logical[0]:g}x{size_logical[1]:g} px client but the OS "
                f"gave {client.width:g}x{client.height:g} px (off by {delta_w:g}x{delta_h:g}). "
                f"Running non-canonical: observation and recording work, calibrated pixel "
                f"constants do not.",
                geometry.with_state(
                    ViewportState.ADOPTED_NONCANONICAL, "clamped by the OS or the application"
                ),
                size_logical,
            )
        return PinResult(
            True,
            f"Client pinned to {client.width:g}x{client.height:g} px at "
            f"({client.x:g},{client.y:g}); DPI {dpi} ({self._dpi_mode}).",
            geometry,
            size_logical,
        )

    def create_capture_source(self) -> Any:
        """The window-specific source for this platform.

        ``PrintWindow`` with ``PW_RENDERFULLCONTENT`` asks the window to render
        itself, so an overlapping window cannot contaminate the frame and
        capture continues while the dashboard is frontmost. Windows Graphics
        Capture would be faster and is the intended production backend, but it
        needs a Windows machine to verify and a new runtime dependency, so it
        is deliberately not guessed at here (DECISIONS.md D-018).

        **PENDING native verification.**
        """
        return WindowsPrintWindowSource()

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
        """Move to a point in **canonical** coordinates."""
        with self._lock:
            geometry = self._geometry
        if not geometry.valid:
            geometry = self.window_geometry()
        if not geometry.valid:
            return
        x, y = geometry.display_logical_from_canonical.apply(
            float(point_px[0]), float(point_px[1])
        )
        with contextlib.suppress(Exception):
            ctypes.windll.user32.SetCursorPos(round(x), round(y))

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
        """Cursor position in **canonical** coordinates, or ``None``."""
        geometry = self.window_geometry()
        if not geometry.valid:
            return None
        point = wintypes.POINT(0, 0)
        with contextlib.suppress(Exception):
            ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
        canonical = geometry.display_logical_from_canonical.inverse().apply(
            float(point.x), float(point.y)
        )
        x, y = round(canonical[0]), round(canonical[1])
        width, height = geometry.canonical_px
        if not (0 <= x < width and 0 <= y < height):
            return None
        return (x, y)

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


# ===========================================================================
# Window-specific capture
# ===========================================================================

PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = (
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    )


class _BITMAPINFO(ctypes.Structure):
    _fields_ = (("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3))


class WindowsPrintWindowSource:
    """Window-specific capture through ``PrintWindow``.

    The window renders itself into our device context, so an overlapping window
    cannot contaminate the frame and capture keeps working while the dashboard
    is frontmost - the two properties a desktop-rectangle grab cannot offer.

    It is a *pull* source. The GDI objects are created once for a given client
    size and reused, so the per-frame cost is one ``PrintWindow`` plus one
    ``GetDIBits`` rather than an allocation storm.

    **PENDING native verification**: written against the documented Win32
    contracts and never executed on Windows (DECISIONS.md D-018).
    """

    def __init__(self) -> None:
        self._geometry: ViewportGeometry | None = None
        self._pool: Any = None
        self._error: str | None = None
        self._window_dc: Any = None
        self._memory_dc: Any = None
        self._bitmap: Any = None
        self._size: tuple[int, int] = (0, 0)
        self._buffer: Any = None

    @property
    def name(self) -> str:
        return "printwindow"

    @property
    def is_pushing(self) -> bool:
        return False

    def set_target_fps(self, fps: int) -> None:
        del fps  # paced by the service

    def health(self) -> str | None:
        return self._error

    def start(
        self, geometry: ViewportGeometry, pool: Any, on_frame: Callable[[RawFrame], None]
    ) -> None:
        del on_frame  # pull source
        self.stop()
        self._geometry = geometry
        self._pool = pool
        self._error = None
        self._ensure_surface(geometry)

    def _ensure_surface(self, geometry: ViewportGeometry) -> bool:
        if geometry.window is None or geometry.client_logical is None:
            return False
        width = round(geometry.client_logical.width)
        height = round(geometry.client_logical.height)
        if width <= 0 or height <= 0:
            return False
        if self._bitmap is not None and self._size == (width, height):
            return True
        self._release_surface()
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        self._window_dc = user32.GetWindowDC(geometry.window.window_id)
        if not self._window_dc:
            self._error = "GetWindowDC failed"
            return False
        self._memory_dc = gdi32.CreateCompatibleDC(self._window_dc)
        self._bitmap = gdi32.CreateCompatibleBitmap(self._window_dc, width, height)
        if not self._memory_dc or not self._bitmap:
            self._error = "could not create a compatible bitmap"
            return False
        gdi32.SelectObject(self._memory_dc, self._bitmap)
        self._size = (width, height)
        self._buffer = ctypes.create_string_buffer(width * height * 4)
        return True

    def _release_surface(self) -> None:
        gdi32 = ctypes.windll.gdi32
        with contextlib.suppress(Exception):
            if self._bitmap:
                gdi32.DeleteObject(self._bitmap)
            if self._memory_dc:
                gdi32.DeleteDC(self._memory_dc)
            if self._window_dc and self._geometry and self._geometry.window:
                ctypes.windll.user32.ReleaseDC(self._geometry.window.window_id, self._window_dc)
        self._bitmap = self._memory_dc = self._window_dc = None
        self._size = (0, 0)
        self._buffer = None

    def stop(self) -> None:
        self._release_surface()
        self._geometry = None

    def poll(self) -> RawFrame | None:
        geometry = self._geometry
        if geometry is None or geometry.window is None or geometry.client_logical is None:
            return None
        if not self._ensure_surface(geometry):
            return None
        width, height = self._size
        started = monotonic_s()
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32

        # PW_RENDERFULLCONTENT captures composited content on Windows 10+,
        # including for windows that are occluded.
        if not user32.PrintWindow(
            geometry.window.window_id, self._memory_dc, PW_RENDERFULLCONTENT
        ):
            self._error = "PrintWindow failed"
            return None

        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        # Negative height requests a top-down DIB, matching array row order.
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB
        copied = gdi32.GetDIBits(
            self._memory_dc,
            self._bitmap,
            0,
            height,
            self._buffer,
            ctypes.byref(info),
            DIB_RGB_COLORS,
        )
        if not copied:
            self._error = "GetDIBits failed"
            return None
        bgra = np.frombuffer(self._buffer, dtype=np.uint8, count=width * height * 4)
        source = bgra.reshape(height, width, 4)[:, :, :3]
        normalize_started = monotonic_s()
        canonical = _letterbox_into_canonical(source, geometry, self._pool)
        if canonical is None:
            self._error = "frame buffer pool exhausted"
            return None
        normalize_ms = (monotonic_s() - normalize_started) * 1000.0
        self._error = None
        return RawFrame(
            bgr=canonical,
            geometry=geometry,
            captured_at_s=started,
            presented_at_s=monotonic_s(),
            content_id=None,  # GDI offers no presentation identity
            backend=self.name,
            normalize_ms=normalize_ms,
        )


def _letterbox_into_canonical(
    source_bgr: Any, geometry: ViewportGeometry, pool: Any
) -> Any | None:
    """Resize a client image into the canonical raster exactly once."""
    import cv2

    width, height = geometry.canonical_px
    target = pool.acquire(height, width) if pool is not None else None
    if target is None:
        return None
    inner_x, inner_y, inner_w, inner_h = geometry.canonical_letterbox_px()
    inner_w = max(1, min(inner_w, width - max(0, inner_x)))
    inner_h = max(1, min(inner_h, height - max(0, inner_y)))
    inner_x, inner_y = max(0, inner_x), max(0, inner_y)
    if (inner_x, inner_y, inner_w, inner_h) != (0, 0, width, height):
        target[:] = 0
    cv2.resize(
        source_bgr,
        (inner_w, inner_h),
        dst=target[inner_y : inner_y + inner_h, inner_x : inner_x + inner_w],
        interpolation=cv2.INTER_AREA,
    )
    return target
