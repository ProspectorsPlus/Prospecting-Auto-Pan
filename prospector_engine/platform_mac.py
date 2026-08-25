"""macOS platform layer for the Prospector Engine [Phase 04 C7].

Everything here is macOS-specific I/O: Quartz input synthesis, window lookup,
retina scale, the pynput hotkey listener, and the calibration-log labeler.
The engine module re-exports these names into its own namespace, so tests and
the sim world keep patching the ENGINE module; platform code reads engine
state and patchable seams (config globals, ``_cursor_point``/``_post``,
``request_*`` entries) back through ``bind()`` so those patches intercept
platform behavior too. One engine process binds one engine module at a time
(sequential fresh-module loads, as the harnesses do, are fine).
"""
import sys

try:
    import numpy as np
    import mss
    import Quartz
    import ApplicationServices as _AS
except ImportError as e:
    sys.exit(
        f"Missing dependency: {e}\n"
        "Install with:\n"
        "  pip3 install pyobjc-framework-Quartz pyobjc-framework-ApplicationServices "
        "mss numpy pynput --break-system-packages"
    )
# [C8] pynput is needed only by the ENGINE's hotkey listener. A host app
# embedding the calibration sensing library must not die at import time
# in an install whose pip set lacks pynput (ISS-131): the failure moves
# to make_listener(), which the engine process reaches at startup --
# still loud, same message, before any input is sent.
try:
    from pynput import keyboard
except ImportError as _pynput_err:
    keyboard = None
    _PYNPUT_ERR = _pynput_err

# Use the non-deprecated MSS class when available (falls back on old name).
_MSS = getattr(mss, "MSS", None) or mss.mss

_eng = None


def bind(engine_module):
    """Receive the engine module; platform code resolves engine state and
    patchable seams through it at call time."""
    global _eng
    _eng = engine_module


# --- Keys (macOS ANSI virtual keycodes) --------------------------------------
KEY_W = 13
KEY_S = 1
KEY_A = 0
KEY_D = 2
KEY_SHIFT = 56          # left Shift (open Fast Travel)
KEY_SPACE = 49          # Space (jump; Studio scripts)
# Hotbar slot number -> macOS virtual keycode (digits 1..0).
SLOT_KEYCODES = {1: 18, 2: 19, 3: 20, 4: 21, 5: 23,
                 6: 22, 7: 26, 8: 28, 9: 25, 0: 29}

# --- Hotkeys -----------------------------------------------------------------
# Toggle start/stop = Ctrl + (TOGGLE_VK).  Quit = Esc.
# F-keys are unreliable on macOS (media keys), so we use a Ctrl chord.
# TOGGLE_VK is the macOS virtual keycode of the second key: K=40, J=38, P=35.
TOGGLE_VK = 40          # 'K'  -> Ctrl+K toggles
TOGGLE_NAME = "Ctrl+K"
SOFTSTOP_VK = 38        # 'J' -> Ctrl+J = manual soft-stop (test)
_CODE_VK_MAC = {"KeyA":0,"KeyB":11,"KeyC":8,"KeyD":2,"KeyE":14,"KeyF":3,"KeyG":5,"KeyH":4,"KeyI":34,"KeyJ":38,"KeyK":40,"KeyL":37,"KeyM":46,"KeyN":45,"KeyO":31,"KeyP":35,"KeyQ":12,"KeyR":15,"KeyS":1,"KeyT":17,"KeyU":32,"KeyV":9,"KeyW":13,"KeyX":7,"KeyY":16,"KeyZ":6,"Digit1":18,"Digit2":19,"Digit3":20,"Digit4":21,"Digit5":23,"Digit6":22,"Digit7":26,"Digit8":28,"Digit9":25,"Digit0":29,"Escape":53,"Space":49,"F1":122,"F2":120,"F3":99,"F4":118,"F5":96,"F6":97,"F7":98,"F8":100,"F9":101,"F10":109,"F11":103,"F12":111}


def _code_to_vk(code):
    return _CODE_VK_MAC.get(code or "")


# ---- Retina scale (detection in physical px; CGEvent in points) -------------
def get_scale(sct):
    main = sct.monitors[1]
    bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    logical_w = bounds.size.width
    return main["width"] / logical_w if logical_w else 1.0


# ---- Window-relative capture (opt-in) ---------------------------------------
def _scan_roblox():
    """Shared window scan: largest on-screen window whose owner is 'Roblox'
    (not Studio). Returns (pid, x, y, w, h) in PHYSICAL pixels, or None."""
    try:
        opt = (Quartz.kCGWindowListOptionOnScreenOnly
               | Quartz.kCGWindowListExcludeDesktopElements)
        wins = Quartz.CGWindowListCopyWindowInfo(opt, Quartz.kCGNullWindowID)
        with _MSS() as sct:
            scale = get_scale(sct)
        best, area, pid = None, 0, None
        for w in wins or []:
            owner = str(w.get("kCGWindowOwnerName", ""))
            if "roblox" in owner.lower() and "studio" not in owner.lower():
                b = w.get("kCGWindowBounds", {})
                ww, hh = b.get("Width", 0), b.get("Height", 0)
                if ww * hh > area and ww >= 320 and hh >= 240:
                    area, best, pid = ww * hh, b, w.get("kCGWindowOwnerPID")
        if best:
            return (pid, int(best["X"] * scale), int(best["Y"] * scale),
                    int(best["Width"] * scale), int(best["Height"] * scale))
    except Exception as e:
        print(f"[window] lookup failed: {e}")
    return None


def find_window_origin():
    """(x, y) of the Roblox game viewport's top-left in PHYSICAL pixels, or
    None if not found."""
    r = _scan_roblox()
    return (r[1], r[2]) if r else None


def find_roblox_rect():
    """Roblox game client area as (x, y, w, h) in PHYSICAL pixels, or None."""
    r = _scan_roblox()
    return r[1:5] if r else None


def _roblox_on_another_space():
    """True if a Roblox window exists SOMEWHERE (any Space) but _scan_roblox()
    (on-screen-only) couldn't see it -- the signature of native fullscreen,
    which runs the app on its own Space and hides its window from on-screen
    enumeration entirely."""
    try:
        wins = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID)
        for w in wins or []:
            owner = str(w.get("kCGWindowOwnerName", ""))
            if "roblox" in owner.lower() and "studio" not in owner.lower():
                return True
    except Exception:
        pass
    return False


def pin_window(w=1280, h=720):
    """Move + resize the Roblox window so it's exactly w x h PHYSICAL pixels
    at the top-left of the primary screen. Returns (ok: bool, message: str).
    Uses the Accessibility API (AXUIElementSetAttributeValue) -- the same
    mechanism window-management tools like Rectangle/Moom use to move
    third-party windows. Requires the calling process to be Accessibility-
    trusted (System Settings > Privacy & Security > Accessibility)."""
    if not _AS.AXIsProcessTrusted():
        return False, ("Accessibility permission not granted. System Settings > "
                       "Privacy & Security > Accessibility -- enable it for this "
                       "app/terminal, then try again.")
    found = _scan_roblox()
    if not found or found[0] is None:
        if _roblox_on_another_space():
            return False, ("Roblox is running but its window isn't on this "
                           "screen/Space -- if it's in native fullscreen (the "
                           "green button, not just maximized), exit fullscreen "
                           "and try again. Window detection and resizing can't "
                           "reach a window on a different fullscreen Space.")
        return False, ("Roblox window not found. Open Roblox (not minimized) "
                       "and try again.")
    pid = found[0]

    app = _AS.AXUIElementCreateApplication(int(pid))
    err, axwins = _AS.AXUIElementCopyAttributeValue(app, _AS.kAXWindowsAttribute, None)
    if err != _AS.kAXErrorSuccess or not axwins:
        return False, ("Could not access Roblox's window via Accessibility -- "
                       "make sure Roblox isn't minimized and try again.")
    win = axwins[0]

    ferr, is_fs = _AS.AXUIElementCopyAttributeValue(win, "AXFullScreen", None)
    if ferr == _AS.kAXErrorSuccess and bool(is_fs):
        return False, ("Roblox is in native fullscreen -- exit fullscreen (the "
                       "green-button kind, not just maximized) and try again.")

    try:
        with _MSS() as sct:
            scale = get_scale(sct)
    except Exception:
        scale = 1.0
    # AX position/size are in POINTS, not physical pixels.
    pw, ph = w / scale, h / scale
    pos_val = _AS.AXValueCreate(_AS.kAXValueCGPointType, Quartz.CGPoint(0.0, 0.0))
    size_val = _AS.AXValueCreate(_AS.kAXValueCGSizeType, Quartz.CGSize(pw, ph))
    e1 = _AS.AXUIElementSetAttributeValue(win, _AS.kAXPositionAttribute, pos_val)
    e2 = _AS.AXUIElementSetAttributeValue(win, _AS.kAXSizeAttribute, size_val)
    if e1 != _AS.kAXErrorSuccess or e2 != _AS.kAXErrorSuccess:
        return False, f"Failed to move/resize the Roblox window (AX error {e1}/{e2})."
    return True, f"Roblox window pinned to {w}x{h} at (0,0)."


# ---- Input engine (Quartz CGEvent, HID level) -------------------------------
HID = Quartz.kCGHIDEventTap


def _post(ev):
    Quartz.CGEventPost(HID, ev)


def key_down(code):
    _eng._HELD_KEYS.add(code)
    _eng._post(Quartz.CGEventCreateKeyboardEvent(None, code, True))


def key_up(code):
    _eng._HELD_KEYS.discard(code)
    _eng._post(Quartz.CGEventCreateKeyboardEvent(None, code, False))


def _cursor_point():
    return Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))


def _mouse_event(kind):
    p = _eng._cursor_point()
    _eng._post(Quartz.CGEventCreateMouseEvent(None, kind, p, Quartz.kCGMouseButtonLeft))


def mouse_down():
    _eng._MOUSE_DOWN = True
    _eng._mouse_event(Quartz.kCGEventLeftMouseDown)


def mouse_up():
    _eng._MOUSE_DOWN = False
    _eng._mouse_event(Quartz.kCGEventLeftMouseUp)


def drag_alive():
    """Keep a held LMB 'alive' with a drag event at the current cursor point
    (a static down can get dropped by the game)."""
    p = _eng._cursor_point()
    _eng._post(Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventLeftMouseDragged, p, Quartz.kCGMouseButtonLeft))


def move_cursor(x, y):
    """Warp the cursor to a PHYSICAL-pixel screen coord (calibration space)."""
    s = _eng.State.scale or 1.0
    px, py = x / s, y / s
    try:
        Quartz.CGWarpMouseCursorPosition((px, py))
        _eng._post(Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventMouseMoved, (px, py), Quartz.kCGMouseButtonLeft))
    except Exception:
        pass


def scroll_down(steps=3):
    try:
        _eng._post(Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitLine, 1, -abs(int(steps))))
    except Exception:
        pass


# ---- v3 general input (Studio Scripts with declared caps; PPSCRIPT_V3) ------
# Canonical lowercase key names -> macOS virtual keycodes. One shared name
# model with platform_win and the Studio VM; unknown names are refused at
# script load, never guessed here.
V3_KEYCODES = {
    "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3, "g": 5, "h": 4,
    "i": 34, "j": 38, "k": 40, "l": 37, "m": 46, "n": 45, "o": 31, "p": 35,
    "q": 12, "r": 15, "s": 1, "t": 17, "u": 32, "v": 9, "w": 13, "x": 7,
    "y": 16, "z": 6,
    "0": 29, "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26,
    "8": 28, "9": 25,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
    "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111, "f13": 105,
    "f14": 107, "f15": 113, "f16": 106, "f17": 64, "f18": 79, "f19": 80,
    "up": 126, "down": 125, "left": 123, "right": 124, "space": 49,
    "enter": 36, "tab": 48, "escape": 53, "backspace": 51, "delete": 117,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "minus": 27, "equal": 24, "comma": 43, "period": 47, "slash": 44,
    "backslash": 42, "semicolon": 41, "quote": 39, "bracketleft": 33,
    "bracketright": 30, "grave": 50,
    "cmd": 55, "ctrl": 59, "alt": 58, "shift": 56,
}
V3_PRIMARY = "cmd"   # the cross-platform "primary" modifier on this platform


def v3_keycode(name):
    return V3_KEYCODES.get(name)


def type_char(ch):
    """One layout-independent unicode character (down + up). The engine paces
    characters (cancellable sleeps) — this stays a single primitive."""
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
        Quartz.CGEventKeyboardSetUnicodeString(ev, len(ch), ch)
        _eng._post(ev)


_V3_BUTTONS = {
    "left": (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp,
             Quartz.kCGMouseButtonLeft),
    "right": (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp,
              Quartz.kCGMouseButtonRight),
    "middle": (Quartz.kCGEventOtherMouseDown, Quartz.kCGEventOtherMouseUp,
               Quartz.kCGMouseButtonCenter),
}


def button_down(button, click_state=1):
    kd, _ku, btn = _V3_BUTTONS[button]
    p = _eng._cursor_point()
    ev = Quartz.CGEventCreateMouseEvent(None, kd, p, btn)
    if click_state > 1:
        # the OS recognizes a double-click by the event's click-state field
        Quartz.CGEventSetIntegerValueField(
            ev, Quartz.kCGMouseEventClickState, int(click_state))
    _eng._HELD_BUTTONS.add(button)
    _eng._post(ev)


def button_up(button, click_state=1):
    _kd, ku, btn = _V3_BUTTONS[button]
    p = _eng._cursor_point()
    ev = Quartz.CGEventCreateMouseEvent(None, ku, p, btn)
    if click_state > 1:
        Quartz.CGEventSetIntegerValueField(
            ev, Quartz.kCGMouseEventClickState, int(click_state))
    _eng._HELD_BUTTONS.discard(button)
    _eng._post(ev)


def scroll_lines(amount):
    """Signed line scroll: positive = up, negative = down (one event)."""
    try:
        _eng._post(Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitLine, 1, int(amount)))
    except Exception:
        pass


def set_clipboard(text):
    """Local clipboard write via pbcopy (no new Python dependencies)."""
    import subprocess
    p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
    p.communicate(str(text).encode("utf-8"), timeout=5)


def _applescript_str(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def show_popup(title, message):
    """Native Notification Center bubble -- non-blocking, safe to call from
    the hotkey listener thread (a fresh subprocess, no UI thread involved)."""
    import subprocess
    script = (f"display notification {_applescript_str(message)} "
              f"with title {_applescript_str(title)}")
    try:
        subprocess.run(["osascript", "-e", script], check=False,
                        timeout=5, capture_output=True)
    except Exception as e:
        print(f"[popup] failed: {e}")


def fr_move_to(x, y):
    """Move the cursor toward (x, y) from the calibrated home (screen centre
    where the cursor rests with shift-lock off), in small steps."""
    if _eng.State.fr_cur is None:
        _eng.fr_reset_home()
    tx, ty = int(x), int(y)
    cx, cy = _eng.State.fr_cur
    step = max(1, _eng.FR_MOVE_STEP)
    s = _eng.State.scale or 1.0
    while cx != tx or cy != ty:
        cx += max(-step, min(step, tx - cx))
        cy += max(-step, min(step, ty - cy))
        try:
            Quartz.CGWarpMouseCursorPosition((cx / s, cy / s))
            _eng._post(Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventMouseMoved, (cx / s, cy / s),
                Quartz.kCGMouseButtonLeft))
        except Exception:
            pass
        _eng.sleep_ms(4)
    _eng.State.fr_cur = [tx, ty]


# ---- Hotkey listener --------------------------------------------------------
def make_listener():
    if keyboard is None:
        sys.exit(
            f"Missing dependency: {_PYNPUT_ERR}\n"
            "Install with:\n"
            "  pip3 install pyobjc-framework-Quartz pyobjc-framework-ApplicationServices "
            "mss numpy pynput --break-system-packages"
        )
    mods = {"ctrl": False, "alt": False, "shift": False}
    CTRL = {keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r}
    ALT = {keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r}
    SHIFT = {keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r}
    binds = [("start", _eng.HOTKEY_START), ("stop", _eng.HOTKEY_STOP),
             ("pixel_info", _eng.HOTKEY_PIXEL_INFO), ("quit", _eng.HOTKEY_QUIT)]

    def on_press(key):
        if key in CTRL:
            mods["ctrl"] = True
            return
        if key in ALT:
            mods["alt"] = True
            return
        if key in SHIFT:
            mods["shift"] = True
            return
        # Character keys arrive as KeyCode (has .vk directly). Special keys
        # (F1, Esc, arrows, ...) arrive as a Key enum member, which has no
        # .vk of its own -- the real vk lives on Key.xxx.value instead.
        vk = getattr(key, "vk", None)
        if vk is None:
            vk = getattr(getattr(key, "value", None), "vk", None)
        for name, spec in binds:
            tv = _eng._code_to_vk((spec or {}).get("code", ""))
            if tv is None or vk != tv:
                continue
            if (bool(spec.get("ctrl")) != mods["ctrl"]
                    or bool(spec.get("alt")) != mods["alt"]
                    or bool(spec.get("shift")) != mods["shift"]):
                continue
            if name == "quit":
                _eng.request_quit()
                return False
            if name == "start":
                _eng.request_start()
            elif name == "stop":
                _eng.request_stop()
            elif name == "pixel_info":
                _eng.request_pixel_info()
            return

    def on_release(key):
        if key in CTRL:
            mods["ctrl"] = False
        elif key in ALT:
            mods["alt"] = False
        elif key in SHIFT:
            mods["shift"] = False

    return keyboard.Listener(on_press=on_press, on_release=on_release)


def calib_key_labeler(label, stop):
    """Labeler for log_calibration: Ctrl+1..4 tags the sample, Esc stops."""
    if keyboard is None:
        sys.exit(f"Missing dependency: {_PYNPUT_ERR}\n"
                 "Install with:\n"
                 "  pip3 install pynput --break-system-packages")
    LABELS = {18: "DIRT", 19: "LAVA", 20: "SHAKE", 21: "DIG"}  # vk of 1,2,3,4
    ctrl = {"down": False}
    CTRL_KEYS = {keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r}

    def on_press(key):
        if key in CTRL_KEYS:
            ctrl["down"] = True
            return
        if key == keyboard.Key.esc:
            stop["v"] = True
            return False
        vk = getattr(key, "vk", None)
        if ctrl["down"] and vk in LABELS:
            label["v"] = LABELS[vk]
            print(f"\n[label = {label['v']}]")

    def on_release(key):
        if key in CTRL_KEYS:
            ctrl["down"] = False

    keyboard.Listener(on_press=on_press, on_release=on_release).start()


# Names re-exported into the engine module namespace (per-platform values).
ENGINE_GLOBALS = {
    "V3_KEYCODES": V3_KEYCODES, "V3_PRIMARY": V3_PRIMARY,
    "KEY_W": KEY_W, "KEY_S": KEY_S, "KEY_A": KEY_A, "KEY_D": KEY_D,
    "KEY_SHIFT": KEY_SHIFT, "KEY_SPACE": KEY_SPACE,
    "SLOT_KEYCODES": SLOT_KEYCODES,
    "TOGGLE_VK": TOGGLE_VK, "TOGGLE_NAME": TOGGLE_NAME,
    "SOFTSTOP_VK": SOFTSTOP_VK, "_CODE_VK_MAC": _CODE_VK_MAC,
}

# Leaf seams that were engine-module globals before the fold (the sim world
# and studio_conformance patch some of them on the engine module).
PLATFORM_SEAMS = {
    "_post": _post, "_mouse_event": _mouse_event,
    "_cursor_point": _cursor_point, "_code_to_vk": _code_to_vk,
    "keyboard": keyboard, "Quartz": Quartz, "HID": HID,
}
