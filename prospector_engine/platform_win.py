"""Windows platform layer for the Prospector Engine [Phase 04 C7].

Everything here is Windows-specific I/O: SendInput scancode key/mouse
synthesis, EnumWindows window lookup, DPI awareness, the GetAsyncKeyState
hotkey poller, and the calibration-log labeler. The engine module re-exports
these names into its own namespace; platform code reads engine state and
patchable seams back through ``bind()`` (see platform_mac for the contract).
"""
import sys
import threading
import time

try:
    import numpy as np
    import mss
    import ctypes
    from ctypes import wintypes
except ImportError as e:
    sys.exit(
        f"Missing dependency: {e}\n"
        "Install with:\n"
        "  pip install mss numpy"
    )

# Use the non-deprecated MSS class when available (falls back on old name).
_MSS = getattr(mss, "MSS", None) or mss.mss

# Make the process DPI-aware so screen capture (mss) and cursor coords line up
# in real pixels on high-DPI / scaled Windows displays.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

_user32 = ctypes.windll.user32

_eng = None


def bind(engine_module):
    """Receive the engine module; platform code resolves engine state and
    patchable seams through it at call time."""
    global _eng
    _eng = engine_module


# --- Keys (WINDOWS hardware SCANCODES -- used with SendInput SCANCODE flag, the
#     most reliable way to drive Roblox on Windows). --------------------------
KEY_W = 0x11            # W
KEY_S = 0x1F            # S
KEY_A = 0x1E            # A
KEY_D = 0x20            # D
KEY_SHIFT = 0x2A        # left Shift (open Fast Travel)
KEY_SPACE = 0x39        # Space (jump; Studio scripts)
# Hotbar slot number -> Windows scancode (digit row 1..0).
SLOT_KEYCODES = {1: 0x02, 2: 0x03, 3: 0x04, 4: 0x05, 5: 0x06,
                 6: 0x07, 7: 0x08, 8: 0x09, 9: 0x0A, 0: 0x0B}

# --- Hotkeys (Ctrl+K start/stop, Esc quit) -- polled via GetAsyncKeyState ----
# These are Windows VIRTUAL-KEY codes (a different namespace from the scancodes
# above): Ctrl=0x11, K=0x4B, Esc=0x1B.
VK_CONTROL = 0x11
VK_K       = 0x4B
VK_ESC     = 0x1B
TOGGLE_NAME = "Ctrl+K"


# ---- Display scale: with DPI-awareness set above, capture and cursor are both
#      in physical pixels, so the scale is 1.0 on Windows. --------------------
def get_scale(sct):
    return 1.0


# ---- Window-relative capture (opt-in) ---------------------------------------
def find_roblox_window():
    """[C8] The calibration sensing window lookup (protocol 4.15
    calibration.detectWindow): the Roblox client area in screen pixels,
    as the legacy host dict {found:True,x,y,w,h,title} or
    {found:False,error:...}. Moved verbatim from the windows app's
    _roblox_rect (windows/prospecting_app.py:1629): scans all visible
    top-level windows (class WINDOWSCLIENT, or title containing 'Roblox'
    but not 'Studio') and picks the largest."""
    try:
        u = ctypes.windll.user32
        candidates = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lp):
            try:
                if not u.IsWindowVisible(hwnd):
                    return True
                n = u.GetWindowTextLengthW(hwnd)
                tbuf = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(hwnd, tbuf, n + 1)
                title = tbuf.value or ""
                cbuf = ctypes.create_unicode_buffer(256)
                u.GetClassNameW(hwnd, cbuf, 256)
                cls = cbuf.value or ""
                is_rbx = (cls == "WINDOWSCLIENT" or
                          ("Roblox" in title and "Studio" not in title))
                if not is_rbx:
                    return True
                rc = wintypes.RECT()
                if not u.GetClientRect(hwnd, ctypes.byref(rc)):
                    return True
                w, h = rc.right - rc.left, rc.bottom - rc.top
                if w < 320 or h < 240:           # skip tiny/loading windows
                    return True
                pt = wintypes.POINT(0, 0)
                u.ClientToScreen(hwnd, ctypes.byref(pt))
                candidates.append((w * h, {"found": True, "x": int(pt.x),
                                   "y": int(pt.y), "w": int(w), "h": int(h),
                                   "title": title or cls}))
            except Exception:
                pass
            return True

        u.EnumWindows(_cb, 0)
        if candidates:
            candidates.sort(key=lambda c: c[0], reverse=True)
            return candidates[0][1]
        return {"found": False,
                "error": "Roblox window not found. Open Prospecting in "
                         "Roblox (not minimized) and try again."}
    except Exception as e:
        return {"found": False, "error": "Detection failed: %s" % e}


def find_roblox_rect():
    """Roblox game client area as (x, y, w, h) in physical px, or None. Scans all
    visible top-level windows (class WINDOWSCLIENT, or title containing 'Roblox'
    but not Studio) and picks the largest -- robust to title variations."""
    try:
        from ctypes import wintypes
        u = ctypes.windll.user32
        best = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lp):
            try:
                if not u.IsWindowVisible(hwnd):
                    return True
                n = u.GetWindowTextLengthW(hwnd)
                tb = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(hwnd, tb, n + 1)
                title = tb.value or ""
                cb = ctypes.create_unicode_buffer(256)
                u.GetClassNameW(hwnd, cb, 256)
                cls = cb.value or ""
                if not (cls == "WINDOWSCLIENT" or
                        ("Roblox" in title and "Studio" not in title)):
                    return True
                rc = wintypes.RECT()
                if not u.GetClientRect(hwnd, ctypes.byref(rc)):
                    return True
                w, h = rc.right - rc.left, rc.bottom - rc.top
                if w < 320 or h < 240:
                    return True
                pt = wintypes.POINT(0, 0)
                u.ClientToScreen(hwnd, ctypes.byref(pt))
                best.append((w * h, int(pt.x), int(pt.y), int(w), int(h)))
            except Exception:
                pass
            return True

        u.EnumWindows(_cb, 0)
        if best:
            best.sort(reverse=True)
            return best[0][1:]
        return None
    except Exception as e:
        print(f"[window] lookup failed: {e}")
        return None


def find_window_origin():
    """(x, y) of the Roblox client top-left, or None. (Back-compat wrapper.)"""
    r = find_roblox_rect()
    return (r[0], r[1]) if r else None


# ---- Input engine (Windows SendInput) ---------------------------------------
# Keys are sent as hardware SCANCODES (KEYEVENTF_SCANCODE) and mouse via
# MOUSEEVENTF_* -- the most reliable way to drive Roblox on Windows.
KEYEVENTF_KEYUP     = 0x0002
KEYEVENTF_SCANCODE  = 0x0008
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP   = 0x0004
MOUSEEVENTF_MOVE     = 0x0001
MOUSEEVENTF_WHEEL = 0x0800
INPUT_MOUSE    = 0
INPUT_KEYBOARD = 1
ULONG_PTR = ctypes.wintypes.WPARAM


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.wintypes.WORD), ("wScan", ctypes.wintypes.WORD),
                ("dwFlags", ctypes.wintypes.DWORD), ("time", ctypes.wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.wintypes.LONG), ("dy", ctypes.wintypes.LONG),
                ("mouseData", ctypes.wintypes.DWORD), ("dwFlags", ctypes.wintypes.DWORD),
                ("time", ctypes.wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.wintypes.DWORD), ("u", _INPUTunion)]


def _send(inp):
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def key_down(code):
    _eng._HELD_KEYS.add(code)
    inp = _INPUT(type=INPUT_KEYBOARD,
                 u=_INPUTunion(ki=_KEYBDINPUT(0, code, KEYEVENTF_SCANCODE, 0, 0)))
    _eng._send(inp)


def key_up(code):
    _eng._HELD_KEYS.discard(code)
    inp = _INPUT(type=INPUT_KEYBOARD,
                 u=_INPUTunion(ki=_KEYBDINPUT(0, code,
                              KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP, 0, 0)))
    _eng._send(inp)


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.wintypes.LONG), ("y", ctypes.wintypes.LONG)]


def _cursor_point():
    p = _Point()
    _user32.GetCursorPos(ctypes.byref(p))
    return p           # has .x / .y like the macOS point


def _mouse_btn(flag):
    inp = _INPUT(type=INPUT_MOUSE,
                 u=_INPUTunion(mi=_MOUSEINPUT(0, 0, 0, flag, 0, 0)))
    _eng._send(inp)


def mouse_down():
    _eng._MOUSE_DOWN = True
    _eng._mouse_btn(MOUSEEVENTF_LEFTDOWN)


def mouse_up():
    _eng._MOUSE_DOWN = False
    _eng._mouse_btn(MOUSEEVENTF_LEFTUP)


def drag_alive():
    """Nudge the mouse 0px to keep a held LMB 'alive' (Windows equivalent of the
    macOS drag keep-alive)."""
    inp = _INPUT(type=INPUT_MOUSE,
                 u=_INPUTunion(mi=_MOUSEINPUT(0, 0, 0, MOUSEEVENTF_MOVE, 0, 0)))
    _eng._send(inp)


def move_cursor(x, y):
    _user32.SetCursorPos(int(x), int(y))


def scroll_down(steps=3):
    data = (-120 * abs(int(steps))) & 0xFFFFFFFF
    inp = _INPUT(type=INPUT_MOUSE,
                 u=_INPUTunion(mi=_MOUSEINPUT(0, 0, data, MOUSEEVENTF_WHEEL, 0, 0)))
    _eng._send(inp)


# ---- v3 general input (Studio Scripts with declared caps; PPSCRIPT_V3) ------
# Canonical lowercase key names -> hardware SCANCODES (same injection path as
# key_down above). One shared name model with platform_mac and the Studio VM.
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040

V3_KEYCODES = {
    "a": 0x1E, "b": 0x30, "c": 0x2E, "d": 0x20, "e": 0x12, "f": 0x21,
    "g": 0x22, "h": 0x23, "i": 0x17, "j": 0x24, "k": 0x25, "l": 0x26,
    "m": 0x32, "n": 0x31, "o": 0x18, "p": 0x19, "q": 0x10, "r": 0x13,
    "s": 0x1F, "t": 0x14, "u": 0x16, "v": 0x2F, "w": 0x11, "x": 0x2D,
    "y": 0x15, "z": 0x2C,
    "0": 0x0B, "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
    "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A,
    "f1": 0x3B, "f2": 0x3C, "f3": 0x3D, "f4": 0x3E, "f5": 0x3F, "f6": 0x40,
    "f7": 0x41, "f8": 0x42, "f9": 0x43, "f10": 0x44, "f11": 0x57, "f12": 0x58,
    "f13": 0x64, "f14": 0x65, "f15": 0x66, "f16": 0x67, "f17": 0x68,
    "f18": 0x69, "f19": 0x6A,
    # extended-bit arrows/navigation ride the same scancode path Roblox
    # already accepts for the game keys; extended flags handled below.
    "up": 0x48, "down": 0x50, "left": 0x4B, "right": 0x4D, "space": 0x39,
    "enter": 0x1C, "tab": 0x0F, "escape": 0x01, "backspace": 0x0E,
    "delete": 0x53, "home": 0x47, "end": 0x4F, "pageup": 0x49,
    "pagedown": 0x51,
    "minus": 0x0C, "equal": 0x0D, "comma": 0x33, "period": 0x34,
    "slash": 0x35, "backslash": 0x2B, "semicolon": 0x27, "quote": 0x28,
    "bracketleft": 0x1A, "bracketright": 0x1B, "grave": 0x29,
    "cmd": 0x5B, "ctrl": 0x1D, "alt": 0x38, "shift": 0x2A,
}
V3_PRIMARY = "ctrl"  # the cross-platform "primary" modifier on this platform

# names whose scancodes need the extended-key flag when injected
_V3_EXTENDED = {"up", "down", "left", "right", "delete", "home", "end",
                "pageup", "pagedown", "cmd"}
KEYEVENTF_EXTENDEDKEY = 0x0001
_V3_EXT_CODES = {V3_KEYCODES[n] for n in _V3_EXTENDED}


def v3_keycode(name):
    return V3_KEYCODES.get(name)


def _v3_flags(code, up):
    f = KEYEVENTF_SCANCODE
    if code in _V3_EXT_CODES:
        f |= KEYEVENTF_EXTENDEDKEY
    if up:
        f |= KEYEVENTF_KEYUP
    return f


def type_char(ch):
    """One layout-independent unicode character (down + up) via
    KEYEVENTF_UNICODE. The engine paces characters."""
    cu = ord(ch)
    for up in (False, True):
        flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
        inp = _INPUT(type=INPUT_KEYBOARD,
                     u=_INPUTunion(ki=_KEYBDINPUT(0, cu, flags, 0, 0)))
        _eng._send(inp)


_V3_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def button_down(button, click_state=1):
    _eng._HELD_BUTTONS.add(button)
    _eng._mouse_btn(_V3_BUTTONS[button][0])


def button_up(button, click_state=1):
    _eng._HELD_BUTTONS.discard(button)
    _eng._mouse_btn(_V3_BUTTONS[button][1])


def scroll_lines(amount):
    """Signed line scroll: positive = up, negative = down (one wheel event)."""
    data = (120 * int(amount)) & 0xFFFFFFFF
    inp = _INPUT(type=INPUT_MOUSE,
                 u=_INPUTunion(mi=_MOUSEINPUT(0, 0, data, MOUSEEVENTF_WHEEL, 0, 0)))
    _eng._send(inp)


def set_clipboard(text):
    """Local clipboard write via the standard clip.exe (no new dependencies)."""
    import subprocess
    p = subprocess.Popen(["clip"], stdin=subprocess.PIPE, shell=False)
    p.communicate(str(text).encode("utf-16-le"), timeout=5)


def _rel_move(dx, dy):
    inp = _INPUT(type=INPUT_MOUSE,
                 u=_INPUTunion(mi=_MOUSEINPUT(int(dx), int(dy), 0,
                              MOUSEEVENTF_MOVE, 0, 0)))
    _eng._send(inp)


def fr_move_to(x, y):
    """Move the cursor to (x, y) using RELATIVE deltas from the calibrated home
    (screen centre where the cursor rests with shift-lock off), in small steps
    so pointer acceleration can't throw it off."""
    if _eng.State.fr_cur is None:
        _eng.fr_reset_home()
    tx, ty = int(x), int(y)
    cx, cy = _eng.State.fr_cur
    step = max(1, _eng.FR_MOVE_STEP)
    while cx != tx or cy != ty:
        sx = max(-step, min(step, tx - cx))
        sy = max(-step, min(step, ty - cy))
        _eng._rel_move(sx, sy)
        cx += sx
        cy += sy
        _eng.sleep_ms(4)
    _eng.State.fr_cur = [tx, ty]


# ---- Hotkey listener --------------------------------------------------------
# Hotkeys via GetAsyncKeyState polling (no third-party lib; doesn't conflict with
# our synthetic input). Ctrl+K toggles start/stop, Esc quits. Returns an object
# with .start() so main() can use it just like the macOS pynput listener.
def _key_pressed(vk):
    return bool(_user32.GetAsyncKeyState(vk) & 0x8000)


def _code_to_vk_win(code):
    code = code or ""
    if code.startswith("Key") and len(code) == 4:
        return ord(code[3].upper())
    if code.startswith("Digit") and len(code) == 6:
        return ord(code[5])
    if code == "Escape":
        return 0x1B
    if code == "Space":
        return 0x20
    if code.startswith("F") and code[1:].isdigit():
        n = int(code[1:])
        if 1 <= n <= 12:
            return 0x70 + (n - 1)
    return None


class _HotkeyPoller:
    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        binds = [("toggle", _eng.HOTKEY_TOGGLE), ("soft", _eng.HOTKEY_SOFTSTOP),
             ("quit", _eng.HOTKEY_QUIT), ("popout", _eng.HOTKEY_POPOUT),
             ("pause", _eng.HOTKEY_PAUSE), ("rreset", _eng.HOTKEY_RELIC_RESET)]
        prev = {"toggle": False, "soft": False, "quit": False, "pause": False,
                "rreset": False}
        while _eng.State.alive:
            ctrl = _eng._key_pressed(0x11)
            alt = _eng._key_pressed(0x12)
            shift = _eng._key_pressed(0x10)
            # Ctrl+Shift+1..9 -> reset THAT relic's timer alone
            for _d in range(1, 10):
                _k = "d%d" % _d
                _dn = ctrl and shift and _eng._key_pressed(0x30 + _d)
                if _dn and not prev.get(_k):
                    if _eng.State.relics_ref is not None:
                        _eng.State.relics_ref.reset_one(_d - 1)
                        _eng.EMIT.relic_one(_d - 1, "hotkey")
                prev[_k] = _dn
            for name, spec in binds:
                vk = _eng._code_to_vk_win((spec or {}).get("code", ""))
                if vk is None:
                    prev[name] = False
                    continue
                down = (_eng._key_pressed(vk)
                        and bool(spec.get("ctrl")) == ctrl
                        and bool(spec.get("alt")) == alt
                        and bool(spec.get("shift")) == shift)
                if down and not prev[name]:
                    if name == "quit":
                        _eng.request_quit()
                        return
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
                prev[name] = down
            time.sleep(0.03)


def make_listener():
    return _HotkeyPoller()


def calib_key_labeler(label, stop):
    """Labeler for log_calibration: Ctrl+1..4 tags the sample, Esc stops."""
    LABELS = {0x31: "DIRT", 0x32: "LAVA", 0x33: "SHAKE", 0x34: "DIG"}  # vk 1..4

    def _poll():
        while not stop["v"]:
            if _eng._key_pressed(VK_ESC):
                stop["v"] = True
                return
            if _eng._key_pressed(VK_CONTROL):
                for vk, name in LABELS.items():
                    if _eng._key_pressed(vk) and label["v"] != name:
                        label["v"] = name
                        print(f"\n[label = {name}]")
            time.sleep(0.05)

    threading.Thread(target=_poll, daemon=True).start()


# Names re-exported into the engine module namespace (per-platform values).
ENGINE_GLOBALS = {
    "V3_KEYCODES": V3_KEYCODES, "V3_PRIMARY": V3_PRIMARY,
    "KEY_W": KEY_W, "KEY_S": KEY_S, "KEY_A": KEY_A, "KEY_D": KEY_D,
    "KEY_SHIFT": KEY_SHIFT, "KEY_SPACE": KEY_SPACE,
    "SLOT_KEYCODES": SLOT_KEYCODES,
    "VK_CONTROL": VK_CONTROL, "VK_K": VK_K, "VK_ESC": VK_ESC,
    "TOGGLE_NAME": TOGGLE_NAME,
}

# Leaf seams that were engine-module globals before the fold.
PLATFORM_SEAMS = {
    "_send": _send, "_mouse_btn": _mouse_btn, "_cursor_point": _cursor_point,
    "_rel_move": _rel_move, "_key_pressed": _key_pressed,
    "_code_to_vk_win": _code_to_vk_win,
    "ctypes": ctypes, "wintypes": wintypes, "_user32": _user32,
}
