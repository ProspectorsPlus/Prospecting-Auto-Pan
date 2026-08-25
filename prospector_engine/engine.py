#!/usr/bin/env python3
"""
Treasure macro -- minimal dig loop.

THE IDEA
    Roblox window is pinned to a fixed 1280x720 at the top-left of the screen
    (see platform_mac.pin_window / platform_win.pin_window), so one baked-in
    pixel coordinate set is correct for every machine -- no calibration UI.

    While running: if the dig cue is visible, tap the dig button (a fixed
    1ms hold -- an instant click, not a timed hold). If the capacity bar
    reads full, stop tapping (pan-swap isn't implemented yet -- that's a
    later feature).

Hotkeys:
    F1  -> find/pin the Roblox window and start the loop
    F2  -> stop the loop
    Esc -> quit (always releases the mouse button first)

SETUP
    macOS:   pip3 install pyobjc-framework-Quartz pyobjc-framework-ApplicationServices mss numpy pynput --break-system-packages
             Grant Terminal (or your launcher) both Accessibility and Screen
             Recording in System Settings > Privacy & Security.
    Windows: pip install mss numpy

RUN
    python3 treasure.py                launches the GUI
    python3 treasure.py --calibrate    hand-derive DIG_TRIGGER_PIXEL /
                                        CAP_FULL_PIXEL for a new layout

One engine source serves both platforms: screen capture (mss) and colour
detection are shared here; window lookup/pin, mouse input, and the hotkey
listener live in prospector_engine/platform_mac.py or platform_win.py.
"""
import sys
import time

if sys.platform == "win32":
    from prospector_engine import platform_win as _plat
else:
    from prospector_engine import platform_mac as _plat

np = _plat.np
mss = _plat.mss
_MSS = _plat._MSS
globals().update(_plat.ENGINE_GLOBALS)
globals().update(_plat.PLATFORM_SEAMS)
_plat.bind(sys.modules[__name__])

get_scale = _plat.get_scale
find_window_origin = _plat.find_window_origin
find_roblox_rect = _plat.find_roblox_rect
mouse_down = _plat.mouse_down
mouse_up = _plat.mouse_up
make_listener = _plat.make_listener
pin_window = _plat.pin_window

# The window title/owner substring platform_*.find_window_origin() matches.
ROBLOX_TITLE = "Roblox"

# ============================================================================
# CONFIG
# ============================================================================

# --- Fixed window geometry: pin_window() places Roblox here at launch -------
WINDOW_W = 1280
WINDOW_H = 720

# --- Dig: fixed instant tap, gated by the dig cue ----------------------------
# DIG_TRIGGER_PIXEL/WHITE_MIN are inherited from the old build's dig skill-bar
# calibration -- re-derive with `python3 treasure.py --calibrate` once you've
# found the actual "ready to dig" cue pixel for this game area.
DIG_TRIGGER_PIXEL = (1078, 532)     # (x, y) -- white when the dig cue is up
WHITE_MIN         = 175             # r,g,b must all be >= this to count "white"
DIG_HOLD_MS       = 1               # fixed instant tap -- not meant to change

# --- Capacity: stop digging when the pan reads full --------------------------
CAP_FULL_PIXEL    = (1120, 900)     # (x, y) -- gray empty / yellow full
YEL_MIN           = 140             # r and g must both be >= this
YEL_BLUE_GAP      = 45              # ...and blue must be <= min(r,g) - this

SAMPLE_BOX        = 6               # NxN px box averaged around a watched pixel

# --- Drift insurance: shift the baked pixels if the window moved after pin --
WINDOW_RELATIVE     = True
CALIB_WINDOW_ORIGIN = [0, 0]        # where pin_window() places the window

# --- Hotkeys ------------------------------------------------------------------
# fn+F1/F2 register as plain F1/F2 key codes at the OS level (fn is a hardware
# modifier that swaps media keys for the standard F-key, not a tracked
# modifier key) -- so these binds don't need ctrl/alt/shift.
HOTKEY_START  = {"ctrl": False, "alt": False, "shift": False, "code": "F1"}
HOTKEY_STOP   = {"ctrl": False, "alt": False, "shift": False, "code": "F2"}
HOTKEY_QUIT   = {"ctrl": False, "alt": False, "shift": False, "code": "Escape"}

LOOP_POLL_S = 0.01   # how often the run loop re-checks the screen when idle


# ============================================================================
# Colour tests
# ============================================================================
def is_white(r, g, b):
    return r >= WHITE_MIN and g >= WHITE_MIN and b >= WHITE_MIN


def is_yellow(r, g, b):
    return r >= YEL_MIN and g >= YEL_MIN and b <= min(r, g) - YEL_BLUE_GAP


def apply_window_offset():
    """Shift the baked pixel coords by however far the Roblox window drifted
    from where pin_window() placed it (belt-and-suspenders on top of actively
    pinning the window -- covers a pin landing a few px off, or a mid-run
    drag). No-op if WINDOW_RELATIVE is off or the window can't be found."""
    if not WINDOW_RELATIVE:
        return
    o = find_window_origin()
    if not o:
        print("[window] Roblox window not found; using saved coords as-is")
        return
    dx = o[0] - CALIB_WINDOW_ORIGIN[0]
    dy = o[1] - CALIB_WINDOW_ORIGIN[1]
    if dx == 0 and dy == 0:
        return
    global DIG_TRIGGER_PIXEL, CAP_FULL_PIXEL
    x, y = DIG_TRIGGER_PIXEL
    DIG_TRIGGER_PIXEL = (x + dx, y + dy)
    x, y = CAP_FULL_PIXEL
    CAP_FULL_PIXEL = (x + dx, y + dy)
    print(f"[window] shifted pixels by ({dx},{dy}) for window move")


# ============================================================================
# Capture + detection
# ============================================================================
class Detector:
    def __init__(self, sct):
        self.sct = sct
        self.dig_region = self._box(DIG_TRIGGER_PIXEL)
        self.cap_region = self._box(CAP_FULL_PIXEL)

    @staticmethod
    def _box(pixel):
        x, y = pixel
        h = SAMPLE_BOX
        return {"left": x - h // 2, "top": y - h // 2, "width": h, "height": h}

    def _rgb(self, region):
        img = np.asarray(self.sct.grab(region))[:, :, :3]   # BGRA -> BGR
        b, g, r = img.reshape(-1, 3).mean(0)
        return r, g, b

    def dig_cue(self):
        """True while the dig-trigger pixel reads white -- a diggable state
        is on screen right now."""
        return is_white(*self._rgb(self.dig_region))

    def capacity_full(self):
        return is_yellow(*self._rgb(self.cap_region))


# ============================================================================
# Run state + hotkeys
# ============================================================================
class State:
    running = False
    alive = True


_MOUSE_DOWN = False


def request_start(origin="hotkey"):
    """Find + pin the Roblox window, then start the dig loop. Bound to F1."""
    ok, msg = pin_window(WINDOW_W, WINDOW_H)
    print(f"[{origin}] {msg}")
    if not ok:
        return
    global CALIB_WINDOW_ORIGIN
    CALIB_WINDOW_ORIGIN = [0, 0]
    apply_window_offset()
    State.running = True
    print(f"[{origin}] started")


def request_stop(origin="hotkey"):
    """Stop the dig loop and release the mouse button. Bound to F2."""
    State.running = False
    mouse_up()
    print(f"[{origin}] stopped")


def request_quit(origin="hotkey"):
    State.alive = False
    State.running = False
    if _MOUSE_DOWN:
        mouse_up()
    print(f"[{origin}] quit")
    return False


# ============================================================================
# The loop
# ============================================================================
def dig_tap():
    mouse_down()
    time.sleep(DIG_HOLD_MS / 1000.0)
    mouse_up()


def tick(detector, on_status=None):
    """One poll: tap the dig button while the dig cue is up and the pan isn't
    full. Reports the current state via on_status(str) if given."""
    if detector.capacity_full():
        if on_status:
            on_status("capacity full -- pan swap not implemented yet")
        return
    if detector.dig_cue():
        dig_tap()
        if on_status:
            on_status("digging")
    elif on_status:
        on_status("idle")


def run(on_status=None):
    """Blocking loop -- call from a background thread. Runs until
    State.alive goes False; only taps/checks while State.running is True.
    apply_window_offset() runs in request_start(), right after pin_window(),
    not here -- this loop starts well before the window is ever pinned."""
    with _MSS() as sct:
        detector = Detector(sct)
        while State.alive:
            if State.running:
                tick(detector, on_status)
            elif on_status:
                on_status("stopped")
            time.sleep(LOOP_POLL_S)


# ============================================================================
# Dev tool: hand-derive DIG_TRIGGER_PIXEL / CAP_FULL_PIXEL
# ============================================================================
def calibrate():
    print("CALIBRATE -- hover a target, read PIXEL/RGB, Ctrl+C to quit.")
    print("  the 'ready to dig' cue  -> DIG_TRIGGER_PIXEL")
    print("  RIGHT END of capacity bar -> CAP_FULL_PIXEL (gray empty / yellow full)\n")
    with _MSS() as sct:
        scale = get_scale(sct)
        full = sct.monitors[1]
        try:
            while True:
                p = _cursor_point()
                px = int(p.x * scale)
                py = int(p.y * scale)
                box = {"left": px - 3, "top": py - 3, "width": 6, "height": 6}
                box["left"] = max(full["left"],
                                  min(box["left"], full["left"] + full["width"] - 6))
                box["top"] = max(full["top"],
                                 min(box["top"], full["top"] + full["height"] - 6))
                img = np.asarray(sct.grab(box))[:, :, :3]
                b, g, r = img.reshape(-1, 3).mean(0)
                tags = ""
                if is_white(r, g, b):  tags += " WHITE"
                if is_yellow(r, g, b): tags += " YELLOW"
                print(f"\rPIXEL=({px:>5},{py:>5})  RGB=({int(r):>3},{int(g):>3},"
                      f"{int(b):>3})  scale {scale:.2f} [{tags.strip()}]      ",
                      end="", flush=True)
                time.sleep(0.05)
        except KeyboardInterrupt:
            print("\nDone.")


def _cli_main():
    listener = make_listener()
    listener.start()
    print(f"F1 to find/pin window + start, F2 to stop, Esc to quit.")
    try:
        run(on_status=print)
    except KeyboardInterrupt:
        request_quit("ctrl-c")


if __name__ == "__main__":
    _cli_main()
