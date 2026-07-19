"""Input recorder — the capture service behind Studio's Input Recorder.

Records the user's real keyboard/mouse events (pynput global listeners)
into a bounded, timestamped event list that Studio turns into editable
PPScript v3 nodes. This module only CAPTURES; interpretation, cleaning,
and node generation live in Studio. Everything stays local: the engine
returns the batch over the PPE1 pipe to its host and keeps nothing.

Safety/consent model (PPSCRIPT_V3.md, Input Recorder section):
- capture starts only on an explicit recorder.start from the host, which
  is expected to show a prominent recording indicator;
- recording is refused while macOS Secure Input is active (a password
  field is focused) and auto-stops if Secure Input engages mid-capture;
- idle-only: never records while a run is active;
- bounded: the event list is capped; hitting the cap stops the capture
  with truncated=True instead of growing without limit.

Coordinates are PHYSICAL pixels (the calibration space): pynput reports
logical points on macOS, so positions are scaled by the same display
scale the sensing session uses.
"""
import ctypes
import ctypes.util
import sys
import threading
import time

MAX_EVENTS = 20000
MOVE_COALESCE_MS = 10.0

# pynput is part of the macOS install set (the hotkey listener needs it);
# on Windows it may be absent — the capability flag says so instead of dying.
try:
    from pynput import keyboard as _kb
    from pynput import mouse as _ms
    _PYNPUT_ERR = None
except ImportError as e:      # pragma: no cover - environment dependent
    _kb = _ms = None
    _PYNPUT_ERR = e


def available():
    return _kb is not None and _ms is not None


def secure_input_active():
    """True when macOS Secure Event Input is engaged (a password field is
    focused): keyboard capture would be blocked by the OS and must not be
    attempted. Non-mac platforms: False."""
    if sys.platform != "darwin":
        return False
    try:
        path = ctypes.util.find_library("Carbon")
        if not path:
            return False
        carbon = ctypes.CDLL(path)
        return bool(carbon.IsSecureEventInputEnabled())
    except Exception:
        return False


# pynput special keys -> canonical v3 names (PPSCRIPT_V3.md key model).
_SPECIAL = {
    "space": "space", "enter": "enter", "tab": "tab", "esc": "escape",
    "backspace": "backspace", "delete": "delete", "home": "home",
    "end": "end", "page_up": "pageup", "page_down": "pagedown",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "cmd": "cmd", "cmd_l": "cmd", "cmd_r": "cmd",
    "ctrl": "ctrl", "ctrl_l": "ctrl", "ctrl_r": "ctrl",
    "alt": "alt", "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt",
    "shift": "shift", "shift_l": "shift", "shift_r": "shift",
}
for _i in range(1, 20):
    _SPECIAL["f%d" % _i] = "f%d" % _i


def key_name(key, keycodes):
    """(canonical name or None, typed char or None) for a pynput key.
    `keycodes` is the platform V3_KEYCODES table — names outside it are
    reported raw so Studio's cleaning pass can show what was dropped."""
    ch = getattr(key, "char", None)
    if ch:
        low = ch.lower()
        return (low if low in keycodes else None), ch
    nm = getattr(key, "name", None)
    if nm and nm in _SPECIAL:
        canon = _SPECIAL[nm]
        return (canon if canon in keycodes else None), None
    return None, None


class Recorder(object):
    """One bounded capture session. Thread-safe event append (pynput
    callbacks arrive on listener threads)."""

    def __init__(self, keycodes, scale=1.0,
                 kb_listener=None, mouse_listener=None):
        self.keycodes = keycodes
        self.scale = float(scale) or 1.0
        self.events = []
        self.recording = False
        self.truncated = False
        self.stop_reason = ""
        self._t0 = 0.0
        self._last_move = -1e9
        self._lock = threading.Lock()
        # injectable for tests; real listeners by default
        self._mk_kb = kb_listener or (lambda: _kb.Listener(
            on_press=self._on_press, on_release=self._on_release))
        self._mk_ms = mouse_listener or (lambda: _ms.Listener(
            on_move=self._on_move, on_click=self._on_click,
            on_scroll=self._on_scroll))
        self._kb = None
        self._ms = None

    # ---- lifecycle ---------------------------------------------------------
    def start(self):
        if self.recording:
            return {"ok": False, "error": "already recording"}
        if not available():
            return {"ok": False,
                    "error": "input capture needs pynput (%s)" % _PYNPUT_ERR}
        if secure_input_active():
            return {"ok": False,
                    "error": "a secure text field is focused (Secure Input); "
                             "click out of the password field and try again"}
        self.events = []
        self.truncated = False
        self.stop_reason = ""
        self._t0 = time.perf_counter()
        self._last_move = -1e9
        self.recording = True
        self._kb = self._mk_kb()
        self._ms = self._mk_ms()
        self._kb.start()
        self._ms.start()
        return {"ok": True}

    def stop(self, reason="host"):
        with self._lock:
            if not self.recording:
                return {"ok": False, "error": "not recording"}
            self.recording = False
            self.stop_reason = reason
        for lst in (self._kb, self._ms):
            try:
                if lst is not None:
                    lst.stop()
            except Exception:
                pass
        self._kb = self._ms = None
        return {"ok": True, "events": list(self.events),
                "truncated": self.truncated,
                "durationMs": self._now_ms(), "reason": reason}

    def status(self):
        # Secure Input engaging mid-capture ends the session (the OS is
        # already blocking keyboard events; keep the truth honest).
        if self.recording and secure_input_active():
            self.stop("secure-input")
        return {"recording": self.recording, "count": len(self.events),
                "secureInput": secure_input_active(),
                "truncated": self.truncated}

    # ---- event capture -----------------------------------------------------
    def _now_ms(self):
        return int(round((time.perf_counter() - self._t0) * 1000.0))

    def _add(self, ev):
        with self._lock:
            if not self.recording:
                return
            if len(self.events) >= MAX_EVENTS:
                self.truncated = True
                self.recording = False
                self.stop_reason = "cap"
                return
            self.events.append(ev)

    def _on_press(self, key):
        name, ch = key_name(key, self.keycodes)
        ev = {"t": self._now_ms(), "kind": "key_down"}
        if name:
            ev["key"] = name
        if ch is not None:
            ev["char"] = ch
        if not name and ch is None:
            ev["raw"] = str(key)
        self._add(ev)

    def _on_release(self, key):
        name, ch = key_name(key, self.keycodes)
        ev = {"t": self._now_ms(), "kind": "key_up"}
        if name:
            ev["key"] = name
        if ch is not None:
            ev["char"] = ch
        if not name and ch is None:
            ev["raw"] = str(key)
        self._add(ev)

    def _scaled(self, x, y):
        return int(round(x * self.scale)), int(round(y * self.scale))

    def _on_move(self, x, y):
        t = self._now_ms()
        if t - self._last_move < MOVE_COALESCE_MS:
            return
        self._last_move = t
        px, py = self._scaled(x, y)
        self._add({"t": t, "kind": "mouse_move", "x": px, "y": py})

    def _on_click(self, x, y, button, pressed):
        px, py = self._scaled(x, y)
        name = getattr(button, "name", str(button))
        if name not in ("left", "right", "middle"):
            name = "left"
        self._add({"t": self._now_ms(),
                   "kind": "mouse_down" if pressed else "mouse_up",
                   "x": px, "y": py, "button": name})

    def _on_scroll(self, x, y, dx, dy):
        px, py = self._scaled(x, y)
        self._add({"t": self._now_ms(), "kind": "scroll",
                   "x": px, "y": py, "dx": int(dx), "dy": int(dy)})
