"""macOS platform port: Quartz input, AX window geometry, pynput hotkeys.

Instance based, with no module-global engine binding and no authoritative held
input state - both of those belong to :class:`~prospector_engine.input_authority.InputAuthority`
(bugs B1, B7, B8, B10).

The canonical geometry this port publishes is the Roblox **client area**, not
the window frame (bug B11). macOS gives us the outer frame; the title-bar
height is *measured* from the window's own traffic-light geometry rather than
assumed, with a documented provisional fallback when Accessibility cannot see
those elements.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from collections.abc import Callable
from typing import Any

if sys.platform != "darwin" and not os.environ.get("TREASURE_ALLOW_CROSS_PLATFORM_IMPORT"):
    raise ImportError(
        "prospector_engine.platform_mac may only be imported on macOS. "
        "Set TREASURE_ALLOW_CROSS_PLATFORM_IMPORT=1 with mocked Quartz modules "
        "to run the opposite-OS import contract test (plan 16.3)."
    )

try:
    import Quartz
except ImportError as exc:  # pragma: no cover - install-time failure
    raise ImportError(
        "prospector_engine.platform_mac requires pyobjc-framework-Quartz "
        f"({exc}). Install with: pip install -e '.[dev]'"
    ) from exc

from prospector_engine.contracts import (
    ClientRectPhysicalPx,
    FocusState,
    InputKey,
    InputVocabulary,
    IntentType,
    MouseButton,
    PinResult,
    Provenance,
    RuntimeIntent,
    monotonic_s,
)

__all__ = [
    "MAC_HOTKEY_BINDINGS",
    "TITLE_BAR_FALLBACK_PT",
    "MacHotkeySource",
    "MacPlatformPort",
    "MacReleaseOnlyPort",
]

# macOS ANSI virtual keycodes for the whole input vocabulary. These are the
# same numbers the legacy V3_KEYCODES table used; only the lookup moved.
_MAC_KEYCODES: dict[InputKey, int] = {
    InputKey.W: 13,
    InputKey.A: 0,
    InputKey.S: 1,
    InputKey.D: 2,
    InputKey.SPACE: 49,
    InputKey.SHIFT: 56,
    InputKey.ESCAPE: 53,
    InputKey.DIGIT_1: 18,
    InputKey.DIGIT_2: 19,
}

_MAC_BUTTON_EVENTS: dict[MouseButton, tuple[int, int, int, int]] = {
    # (down, up, dragged, CGMouseButton)
    MouseButton.LEFT: (
        Quartz.kCGEventLeftMouseDown,
        Quartz.kCGEventLeftMouseUp,
        Quartz.kCGEventLeftMouseDragged,
        Quartz.kCGMouseButtonLeft,
    ),
    MouseButton.RIGHT: (
        Quartz.kCGEventRightMouseDown,
        Quartz.kCGEventRightMouseUp,
        Quartz.kCGEventRightMouseDragged,
        Quartz.kCGMouseButtonRight,
    ),
    MouseButton.MIDDLE: (
        Quartz.kCGEventOtherMouseDown,
        Quartz.kCGEventOtherMouseUp,
        Quartz.kCGEventOtherMouseDragged,
        Quartz.kCGMouseButtonCenter,
    ),
}

TITLE_BAR_FALLBACK_PT = 28.0
"""Provisional standard-window title-bar height in points.

Only used when Accessibility cannot expose the window's close button. The
measured value for the live Roblox client on the development Mac
(2026-08-27, macOS 25.4, 2x display) was exactly 28.0 pt, derived as
``2 * (close_button_y - frame_y) + close_button_height``.
"""

TITLE_BAR_PROVENANCE = Provenance(
    status=__import__(
        "prospector_engine.contracts", fromlist=["EvidenceStatus"]
    ).EvidenceStatus.PROVISIONAL,
    source="platform_mac.MacPlatformPort._measure_title_bar_pt fallback",
    note="measured 28.0 pt on the dev Mac; E-VIEW on macOS is PENDING",
)

MAC_HOTKEY_BINDINGS: dict[str, IntentType] = {
    "f1": IntentType.START_LIVE,
    "f2": IntentType.STOP,
    "f3": IntentType.PIXEL_INFO,
    "f4": IntentType.RESET_CHARACTER,
    "f5": IntentType.PAN_SWAP_TEST,
    "f6": IntentType.DIG_LOOP,
}
"""F1-F6, all actually bound (bug B4: F5 was advertised but bound nowhere).

F6 is the standalone dig loop (DECISIONS.md D-015).
"""

_MAC_HOTKEY_VK: dict[str, int] = {
    "f1": 122,
    "f2": 120,
    "f3": 99,
    "f4": 118,
    "f5": 96,
    "f6": 97,
}


def _post(event: Any) -> None:
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _cursor_point_pt() -> Any:
    return Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))


class MacReleaseOnlyPort:
    """The strict release-only backend the deadman helper holds.

    Deliberately has no ``down`` method: there is no code path in this class
    that can press anything (plan 4.5).
    """

    def __init__(self) -> None:
        self._vocabulary = InputVocabulary()

    @property
    def vocabulary(self) -> InputVocabulary:
        return self._vocabulary

    def key_code(self, key: InputKey) -> int:
        return _MAC_KEYCODES[key]

    def raw_key_up(self, code: int) -> None:
        _post(Quartz.CGEventCreateKeyboardEvent(None, code, False))

    def raw_button_up(self, button: MouseButton) -> None:
        _, up, _drag, btn = _MAC_BUTTON_EVENTS[button]
        _post(Quartz.CGEventCreateMouseEvent(None, up, _cursor_point_pt(), btn))


class MacPlatformPort:
    """Full macOS port. Only ``InputAuthority`` and capture may hold one."""

    def __init__(self, *, window_owner_substring: str = "roblox") -> None:
        self._vocabulary = InputVocabulary()
        self._owner_substring = window_owner_substring
        self._lock = threading.Lock()
        self._last_rect: ClientRectPhysicalPx | None = None
        self._title_bar_pt: float = TITLE_BAR_FALLBACK_PT
        self._title_bar_measured = False

    # -- identity ---------------------------------------------------------
    @property
    def name(self) -> str:
        return "macos"

    @property
    def vocabulary(self) -> InputVocabulary:
        return self._vocabulary

    def key_code(self, key: InputKey) -> int:
        return _MAC_KEYCODES[key]

    @property
    def title_bar_pt(self) -> float:
        return self._title_bar_pt

    @property
    def title_bar_measured(self) -> bool:
        """True when the inset came from AX geometry, not the fallback."""
        return self._title_bar_measured

    # -- accessibility ----------------------------------------------------
    @staticmethod
    def _app_services() -> Any:
        import ApplicationServices

        return ApplicationServices

    def accessibility_trusted(self) -> bool:
        try:
            return bool(self._app_services().AXIsProcessTrusted())
        except Exception:
            return False

    # -- display ----------------------------------------------------------
    @staticmethod
    def _scale_for_display(display_id: int) -> float:
        mode = Quartz.CGDisplayCopyDisplayMode(display_id)
        if mode is None:
            return 1.0
        points_wide = float(Quartz.CGDisplayModeGetWidth(mode))
        pixels_wide = float(Quartz.CGDisplayModeGetPixelWidth(mode))
        return pixels_wide / points_wide if points_wide else 1.0

    @staticmethod
    def _display_for_point_pt(x: float, y: float) -> int:
        with contextlib.suppress(Exception):
            err, displays, count = Quartz.CGGetDisplaysWithPoint(
                Quartz.CGPoint(x, y), 1, None, None
            )
            if not err and count:
                return int(displays[0])
        return int(Quartz.CGMainDisplayID())

    # -- window lookup ----------------------------------------------------
    def _scan_roblox(self) -> tuple[int, float, float, float, float] | None:
        """Largest on-screen Roblox (not Studio) window frame, in POINTS."""
        try:
            options = (
                Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements
            )
            windows = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)
        except Exception:
            return None
        best: tuple[int, float, float, float, float] | None = None
        best_area = 0.0
        for window in windows or []:
            owner = str(window.get("kCGWindowOwnerName", ""))
            lowered = owner.lower()
            if self._owner_substring not in lowered or "studio" in lowered:
                continue
            bounds = window.get("kCGWindowBounds") or {}
            width = float(bounds.get("Width", 0.0))
            height = float(bounds.get("Height", 0.0))
            if width < 320 or height < 240 or width * height <= best_area:
                continue
            best_area = width * height
            best = (
                int(window.get("kCGWindowOwnerPID") or 0),
                float(bounds.get("X", 0.0)),
                float(bounds.get("Y", 0.0)),
                width,
                height,
            )
        return best

    def roblox_on_another_space(self) -> bool:
        """A Roblox window exists but is not on screen - the fullscreen signature."""
        with contextlib.suppress(Exception):
            windows = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID
            )
            for window in windows or []:
                owner = str(window.get("kCGWindowOwnerName", "")).lower()
                if self._owner_substring in owner and "studio" not in owner:
                    return True
        return False

    def _ax_window(self, pid: int) -> Any | None:
        services = self._app_services()
        app = services.AXUIElementCreateApplication(int(pid))
        err, windows = services.AXUIElementCopyAttributeValue(
            app, services.kAXWindowsAttribute, None
        )
        if err != services.kAXErrorSuccess or not windows:
            return None
        return windows[0]

    def _measure_title_bar_pt(self, window: Any) -> float | None:
        """Derive the title-bar height from the window's own traffic lights.

        ``2 * (close_button_y - frame_y) + close_button_height`` because the
        buttons are vertically centred in the bar. Returns ``None`` when AX
        does not expose them, in which case the caller uses the provisional
        fallback and says so.
        """
        services = self._app_services()
        try:
            err, frame_value = services.AXUIElementCopyAttributeValue(
                window, services.kAXPositionAttribute, None
            )
            if err != services.kAXErrorSuccess:
                return None
            ok, frame_point = services.AXValueGetValue(
                frame_value, services.kAXValueCGPointType, None
            )
            if not ok:
                return None
            err, button = services.AXUIElementCopyAttributeValue(window, "AXCloseButton", None)
            if err != services.kAXErrorSuccess or button is None:
                return None
            err, pos_value = services.AXUIElementCopyAttributeValue(button, "AXPosition", None)
            if err != services.kAXErrorSuccess:
                return None
            err, size_value = services.AXUIElementCopyAttributeValue(button, "AXSize", None)
            if err != services.kAXErrorSuccess:
                return None
            ok_pos, button_point = services.AXValueGetValue(
                pos_value, services.kAXValueCGPointType, None
            )
            ok_size, button_size = services.AXValueGetValue(
                size_value, services.kAXValueCGSizeType, None
            )
            if not (ok_pos and ok_size):
                return None
            inset = float(button_point.y) - float(frame_point.y)
            measured = 2.0 * inset + float(button_size.height)
            if not 12.0 <= measured <= 80.0:
                return None
            return measured
        except Exception:
            return None

    # -- PlatformPort: viewport ------------------------------------------
    def focus_state(self) -> FocusState:
        """True/False/None per plan 4.3; ``None`` means genuinely unknown."""
        try:
            from AppKit import NSWorkspace  # bundled with pyobjc-framework-Cocoa
        except Exception:
            return None
        try:
            frontmost = NSWorkspace.sharedWorkspace().frontmostApplication()
        except Exception:
            return None
        if frontmost is None:
            return None
        name = str(frontmost.localizedName() or "").lower()
        if "studio" in name:
            return False
        return self._owner_substring in name

    def find_client_rect(self) -> ClientRectPhysicalPx | None:
        found = self._scan_roblox()
        if found is None:
            with self._lock:
                self._last_rect = None
            return None
        pid, frame_x_pt, frame_y_pt, frame_w_pt, frame_h_pt = found

        title_bar_pt = TITLE_BAR_FALLBACK_PT
        measured = False
        if pid and self.accessibility_trusted():
            window = self._ax_window(pid)
            if window is not None:
                candidate = self._measure_title_bar_pt(window)
                if candidate is not None:
                    title_bar_pt = candidate
                    measured = True

        display_id = self._display_for_point_pt(frame_x_pt, frame_y_pt)
        scale = self._scale_for_display(display_id)

        client_x_px = round(frame_x_pt * scale)
        client_y_px = round((frame_y_pt + title_bar_pt) * scale)
        client_w_px = round(frame_w_pt * scale)
        client_h_px = round((frame_h_pt - title_bar_pt) * scale)

        invalid_reason: str | None = None
        if client_w_px <= 0 or client_h_px <= 0:
            invalid_reason = "non-positive client size"

        rect = ClientRectPhysicalPx(
            origin_px=(client_x_px, client_y_px),
            size_px=(max(0, client_w_px), max(0, client_h_px)),
            scale=scale,
            verified_at_s=monotonic_s(),
            display_id=str(display_id),
            valid=invalid_reason is None,
            invalid_reason=invalid_reason,
        )
        with self._lock:
            self._title_bar_pt = title_bar_pt
            self._title_bar_measured = measured
            self._last_rect = rect
        return rect

    def pin_client_rect(self, size_px: tuple[int, int]) -> PinResult:
        """Move/resize Roblox so its **client area** is exactly ``size_px``.

        macOS may legally refuse the requested origin (menu-bar safe area), so
        the achieved origin is read back and used everywhere rather than being
        assumed to be (0, 0) (plan 4.1).
        """
        services = self._app_services()
        if not self.accessibility_trusted():
            return PinResult(
                False,
                "Accessibility permission not granted. System Settings > Privacy & "
                "Security > Accessibility - enable it for this app/terminal, then retry.",
            )
        found = self._scan_roblox()
        if found is None or not found[0]:
            if self.roblox_on_another_space():
                return PinResult(
                    False,
                    "Roblox is running but not on this Space - exit native fullscreen "
                    "(the green button) and try again.",
                )
            return PinResult(False, "Roblox window not found. Open Roblox (not minimized).")
        pid = found[0]
        window = self._ax_window(pid)
        if window is None:
            return PinResult(
                False, "Could not reach Roblox's window via Accessibility (minimized?)."
            )

        err, is_fullscreen = services.AXUIElementCopyAttributeValue(
            window, "AXFullScreen", None
        )
        if err == services.kAXErrorSuccess and bool(is_fullscreen):
            return PinResult(
                False, "Roblox is in native fullscreen - exit fullscreen and retry."
            )

        measured = self._measure_title_bar_pt(window)
        title_bar_pt = measured if measured is not None else TITLE_BAR_FALLBACK_PT
        display_id = self._display_for_point_pt(found[1], found[2])
        scale = self._scale_for_display(display_id)

        want_w_pt = size_px[0] / scale
        want_h_pt = size_px[1] / scale + title_bar_pt
        position = services.AXValueCreate(
            services.kAXValueCGPointType, Quartz.CGPoint(0.0, 0.0)
        )
        size = services.AXValueCreate(
            services.kAXValueCGSizeType, Quartz.CGSize(want_w_pt, want_h_pt)
        )
        err_pos = services.AXUIElementSetAttributeValue(
            window, services.kAXPositionAttribute, position
        )
        err_size = services.AXUIElementSetAttributeValue(
            window, services.kAXSizeAttribute, size
        )
        if services.kAXErrorSuccess not in (err_pos,) or err_size != services.kAXErrorSuccess:
            return PinResult(
                False, f"Failed to move/resize Roblox (AX error {err_pos}/{err_size})."
            )

        rect = self.find_client_rect()
        if rect is None or not rect.valid:
            return PinResult(False, "Pinned, but the client rect could not be read back.")
        dw = abs(rect.width_px - size_px[0])
        dh = abs(rect.height_px - size_px[1])
        if dw > 1 or dh > 1:
            return PinResult(
                False,
                f"Client readback {rect.size_px} differs from requested {size_px} "
                f"by ({dw},{dh}) px - outside the one-pixel contract.",
                rect,
            )
        source = "measured" if self._title_bar_measured else "provisional-fallback"
        return PinResult(
            True,
            f"Client area pinned to {rect.width_px}x{rect.height_px} px at "
            f"{rect.origin_px} (scale {rect.scale:g}, "
            f"title bar {title_bar_pt:g} pt, {source}).",
            rect,
        )

    # -- PlatformPort: raw edges -----------------------------------------
    def raw_key_down(self, code: int) -> None:
        _post(Quartz.CGEventCreateKeyboardEvent(None, code, True))

    def raw_key_up(self, code: int) -> None:
        _post(Quartz.CGEventCreateKeyboardEvent(None, code, False))

    def raw_button_down(self, button: MouseButton) -> None:
        down, _up, _drag, btn = _MAC_BUTTON_EVENTS[button]
        _post(Quartz.CGEventCreateMouseEvent(None, down, _cursor_point_pt(), btn))

    def raw_button_up(self, button: MouseButton) -> None:
        _down, up, _drag, btn = _MAC_BUTTON_EVENTS[button]
        _post(Quartz.CGEventCreateMouseEvent(None, up, _cursor_point_pt(), btn))

    def raw_pointer_move_client(self, point_px: tuple[int, int]) -> None:
        with self._lock:
            rect = self._last_rect
        if rect is None:
            rect = self.find_client_rect()
        if rect is None or not rect.valid:
            return
        screen_x_px, screen_y_px = rect.to_screen_px(point_px)
        x_pt, y_pt = screen_x_px / rect.scale, screen_y_px / rect.scale
        with contextlib.suppress(Exception):
            Quartz.CGWarpMouseCursorPosition((x_pt, y_pt))
        _post(
            Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventMouseMoved, (x_pt, y_pt), Quartz.kCGMouseButtonLeft
            )
        )

    def raw_pointer_delta(
        self, dx: int, dy: int, held_button: MouseButton | None = None
    ) -> None:
        """Relative HID motion - the only thing that drives Roblox's camera.

        Roblox reads ``kCGMouseEventDeltaX/Y``; a plain absolute move does
        nothing even while a button is genuinely held. When the authority's
        ledger holds a button, the delta rides that button's *dragged* event,
        which is what makes yaw work (plan 12, native yaw detail).
        """
        if held_button is not None:
            _down, _up, dragged, btn = _MAC_BUTTON_EVENTS[held_button]
            event = Quartz.CGEventCreateMouseEvent(None, dragged, _cursor_point_pt(), btn)
        else:
            event = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventMouseMoved, _cursor_point_pt(), Quartz.kCGMouseButtonLeft
            )
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventDeltaX, int(dx))
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventDeltaY, int(dy))
        _post(event)

    def raw_scroll_lines(self, lines: int) -> None:
        _post(
            Quartz.CGEventCreateScrollWheelEvent(
                None, Quartz.kCGScrollEventUnitLine, 1, int(lines)
            )
        )

    def cursor_client_px(self) -> tuple[int, int] | None:
        rect = self.find_client_rect()
        if rect is None or not rect.valid:
            return None
        point = _cursor_point_pt()
        client_x = round(float(point.x) * rect.scale) - rect.origin_px[0]
        client_y = round(float(point.y) * rect.scale) - rect.origin_px[1]
        if not rect.contains_client_point((client_x, client_y)):
            return None
        return (client_x, client_y)

    # -- PlatformPort: hotkeys -------------------------------------------
    def create_hotkey_source(self, submit: Callable[[RuntimeIntent], None]) -> MacHotkeySource:
        return MacHotkeySource(submit, focus_probe=self.focus_state)


class MacHotkeySource:
    """Global F1-F5 listener that submits intents and nothing else.

    Input-emitting intents are submitted only while Roblox is positively
    focused (plan 11.2). ``STOP`` is always accepted - a stop that needed
    focus would be useless exactly when it matters.
    """

    #: Intents that never require focus.
    ALWAYS_ALLOWED = frozenset({IntentType.STOP, IntentType.SHUTDOWN, IntentType.PIXEL_INFO})

    def __init__(
        self,
        submit: Callable[[RuntimeIntent], None],
        *,
        focus_probe: Callable[[], FocusState],
    ) -> None:
        self._submit = submit
        self._focus_probe = focus_probe
        self._listener: Any | None = None
        self._sequence = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        try:
            from pynput import keyboard
        except ImportError as exc:  # pragma: no cover - install-time failure
            raise ImportError(f"Global hotkeys need pynput ({exc}).") from exc

        vk_to_intent = {
            _MAC_HOTKEY_VK[name]: intent for name, intent in MAC_HOTKEY_BINDINGS.items()
        }

        def on_press(key: Any) -> None:
            vk = getattr(key, "vk", None)
            if vk is None:
                vk = getattr(getattr(key, "value", None), "vk", None)
            if not isinstance(vk, int):
                return
            intent_type = vk_to_intent.get(vk)
            if intent_type is None:
                return
            self.fire(intent_type)

        listener = keyboard.Listener(on_press=on_press)
        listener.daemon = True
        listener.name = "treasure-hotkeys"
        listener.start()
        self._listener = listener

    def fire(self, intent_type: IntentType) -> bool:
        """Submit one hotkey intent if policy allows. Exposed for tests."""
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
        listener = self._listener
        self._listener = None
        if listener is not None:
            with contextlib.suppress(Exception):
                listener.stop()

    def is_running(self) -> bool:
        listener = self._listener
        return bool(listener is not None and listener.is_alive())
