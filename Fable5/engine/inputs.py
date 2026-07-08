"""Input abstraction — synthetic keyboard/mouse behind one small interface.

Keys are logical names ('w','a','s','d','shift','1'..'9'); each backend maps
them to platform codes. Held keys/buttons are tracked so release_all() can
always leave the OS clean (anti key-drift after stops/recoveries).

CRITICAL platform fact (from the v2 project, learned the hard way):
on macOS a synthetic HELD mouse press is NOT sustained inside Roblox — a
hold silently does nothing, while rapid clicks DO register. Everything that
needs sustained shaking must therefore be a rapid click stream. Keyboard
holds DO work. Windows can hold both, but we keep the click-stream approach
for parity.
"""
from __future__ import annotations

import sys


class Input:
    """Interface. All timing goes through the injected clock."""

    def __init__(self, clock):
        self.clock = clock
        self._held_keys = set()
        self._mouse_down = False

    # -- keyboard ------------------------------------------------------------
    def key_down(self, key):
        self._held_keys.add(key)
        self._key(key, True)

    def key_up(self, key):
        self._held_keys.discard(key)
        self._key(key, False)

    def tap_key(self, key, ms=40):
        self.key_down(key)
        self.clock.sleep_ms(ms)
        self.key_up(key)

    # -- mouse ---------------------------------------------------------------
    def mouse_down(self):
        self._mouse_down = True
        self._mouse(True)

    def mouse_up(self):
        self._mouse_down = False
        self._mouse(False)

    def mouse_tap(self, ms):
        self.mouse_down()
        self.clock.sleep_ms(ms)
        self.mouse_up()

    # -- hygiene ---------------------------------------------------------------
    def release_all(self):
        for k in list(self._held_keys):
            try:
                self.key_up(k)
            except Exception:
                pass
        self._held_keys.clear()
        if self._mouse_down:
            try:
                self.mouse_up()
            except Exception:
                pass
        self._mouse_down = False

    # -- backend hooks ---------------------------------------------------------
    def _key(self, key, down):
        raise NotImplementedError

    def _mouse(self, down):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# macOS — Quartz CGEvent at HID level (same approach the proven v2 engine used)
# ---------------------------------------------------------------------------
MAC_VK = {
    "a": 0, "s": 1, "d": 2, "w": 13, "shift": 56,
    "1": 18, "2": 19, "3": 20, "4": 21, "5": 23,
    "6": 22, "7": 26, "8": 28, "9": 25,
}


class MacInput(Input):
    def __init__(self, clock):
        super().__init__(clock)
        import Quartz                      # pyobjc
        self.Q = Quartz
        self.HID = Quartz.kCGHIDEventTap

    def _post(self, ev):
        self.Q.CGEventPost(self.HID, ev)

    def _key(self, key, down):
        code = MAC_VK.get(str(key).lower())
        if code is None:
            return
        self._post(self.Q.CGEventCreateKeyboardEvent(None, code, down))

    def _cursor(self):
        return self.Q.CGEventGetLocation(self.Q.CGEventCreate(None))

    def _mouse(self, down):
        kind = (self.Q.kCGEventLeftMouseDown if down
                else self.Q.kCGEventLeftMouseUp)
        self._post(self.Q.CGEventCreateMouseEvent(
            None, kind, self._cursor(), self.Q.kCGMouseButtonLeft))


# ---------------------------------------------------------------------------
# Windows — ctypes SendInput with scancodes (ready for the port; untested here)
# ---------------------------------------------------------------------------
WIN_SCAN = {
    "a": 0x1E, "s": 0x1F, "d": 0x20, "w": 0x11, "shift": 0x2A,
    "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
    "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A,
}


class WinInput(Input):
    KEYEVENTF_SCANCODE = 0x0008
    KEYEVENTF_KEYUP = 0x0002
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004

    def __init__(self, clock):
        super().__init__(clock)
        import ctypes
        self.ctypes = ctypes
        self.user32 = ctypes.windll.user32

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                        ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                        ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                        ("time", ctypes.c_ulong),
                        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", ctypes.c_ulong), ("u", _U)]

        self.INPUT, self.KEYBDINPUT, self.MOUSEINPUT = INPUT, KEYBDINPUT, MOUSEINPUT

    def _send(self, inp):
        self.user32.SendInput(1, self.ctypes.byref(inp),
                              self.ctypes.sizeof(inp))

    def _key(self, key, down):
        scan = WIN_SCAN.get(str(key).lower())
        if scan is None:
            return
        inp = self.INPUT()
        inp.type = 1                       # INPUT_KEYBOARD
        inp.u.ki.wScan = scan
        inp.u.ki.dwFlags = self.KEYEVENTF_SCANCODE | (
            0 if down else self.KEYEVENTF_KEYUP)
        self._send(inp)

    def _mouse(self, down):
        inp = self.INPUT()
        inp.type = 0                       # INPUT_MOUSE
        inp.u.mi.dwFlags = (self.MOUSEEVENTF_LEFTDOWN if down
                            else self.MOUSEEVENTF_LEFTUP)
        self._send(inp)


def make_input(clock):
    if sys.platform == "darwin":
        return MacInput(clock)
    if sys.platform.startswith("win"):
        return WinInput(clock)
    raise RuntimeError("no input backend for platform %s" % sys.platform)
