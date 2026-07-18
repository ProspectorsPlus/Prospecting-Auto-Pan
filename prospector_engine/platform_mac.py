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
    from pynput import keyboard
except ImportError as e:
    sys.exit(
        f"Missing dependency: {e}\n"
        "Install with:\n"
        "  pip3 install pyobjc-framework-Quartz mss numpy pynput --break-system-packages"
    )

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
def find_window_origin():
    """(x, y) of the Roblox game viewport's top-left in PHYSICAL pixels, or None.
    macOS best-effort via the window list; returns None if not found."""
    try:
        opt = (Quartz.kCGWindowListOptionOnScreenOnly
               | Quartz.kCGWindowListExcludeDesktopElements)
        wins = Quartz.CGWindowListCopyWindowInfo(opt, Quartz.kCGNullWindowID)
        with _MSS() as sct:
            scale = get_scale(sct)
        best, area = None, 0
        for w in wins or []:
            owner = str(w.get("kCGWindowOwnerName", ""))
            if _eng.ROBLOX_TITLE.lower() in owner.lower():
                b = w.get("kCGWindowBounds", {})
                a = b.get("Width", 0) * b.get("Height", 0)
                if a > area:
                    area, best = a, b
        if best:
            return (int(best["X"] * scale), int(best["Y"] * scale))
    except Exception as e:
        print(f"[window] lookup failed: {e}")
    return None


def find_roblox_rect():
    """Roblox game client area as (x, y, w, h) in PHYSICAL pixels, or None.
    macOS: largest on-screen window whose owner is 'Roblox' (not Studio)."""
    try:
        opt = (Quartz.kCGWindowListOptionOnScreenOnly
               | Quartz.kCGWindowListExcludeDesktopElements)
        wins = Quartz.CGWindowListCopyWindowInfo(opt, Quartz.kCGNullWindowID)
        with _MSS() as sct:
            scale = get_scale(sct)
        best, area = None, 0
        for w in wins or []:
            owner = str(w.get("kCGWindowOwnerName", ""))
            if "roblox" in owner.lower() and "studio" not in owner.lower():
                b = w.get("kCGWindowBounds", {})
                ww, hh = b.get("Width", 0), b.get("Height", 0)
                if ww * hh > area and ww >= 320 and hh >= 240:
                    area, best = ww * hh, b
        if best:
            return (int(best["X"] * scale), int(best["Y"] * scale),
                    int(best["Width"] * scale), int(best["Height"] * scale))
    except Exception as e:
        print(f"[window] lookup failed: {e}")
    return None


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
    _eng._mouse_event(Quartz.kCGEventLeftMouseDown)


def mouse_up():
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
_MAC_DIGIT_IDX = {18: 0, 19: 1, 20: 2, 21: 3, 23: 4, 22: 5, 26: 6, 28: 7,
                  25: 8}          # macOS vk for 1..9 -> relic index


def make_listener():
    mods = {"ctrl": False, "alt": False, "shift": False}
    CTRL = {keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r}
    ALT = {keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r}
    SHIFT = {keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r}
    binds = [("toggle", _eng.HOTKEY_TOGGLE), ("soft", _eng.HOTKEY_SOFTSTOP),
             ("quit", _eng.HOTKEY_QUIT), ("popout", _eng.HOTKEY_POPOUT),
             ("pause", _eng.HOTKEY_PAUSE), ("rreset", _eng.HOTKEY_RELIC_RESET)]

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
        vk = getattr(key, "vk", None)
        if key == keyboard.Key.esc and vk is None:
            vk = 53
        # Ctrl+Shift+1..9 -> reset THAT relic's timer alone
        if mods["ctrl"] and mods["shift"] and vk in _MAC_DIGIT_IDX:
            if _eng.State.relics_ref is not None:
                _eng.State.relics_ref.reset_one(_MAC_DIGIT_IDX[vk])
                _eng.EMIT.relic_one(_MAC_DIGIT_IDX[vk], "hotkey")
            return
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
            if name == "toggle":
                _eng.request_toggle()
            elif name == "pause":
                _eng.request_pause_toggle()
            elif name == "rreset":
                if _eng.State.relics_ref is not None:
                    _eng.State.relics_ref.reset()
                _eng.EMIT.relic_reset(False)
            elif name == "soft":
                _eng.request_soft()
            elif name == "popout":
                _eng.EMIT.popout()
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
