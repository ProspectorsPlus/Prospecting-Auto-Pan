#!/usr/bin/env python3
"""
prospecting_friend.py -- faithful Python (macOS) port of the friend's AHK
"Sandshaking Macro" (SandshakingMacro_CLEAN). Same logic, same flow:

  MainLoop:
    DIG until the pan is FULL (yellow at the bar). While digging, if the pan
    isn't registering (recovery check), tap S back.
  SandshakingSequence:
    walk forward in 25ms W-bursts until the WHITE prompt pixel shows -> click ->
    hold S (walkback) -> wait -> hold LMB to shake, checking EMPTY (gray); on
    timeout (ShakeTimeout) release & retry, up to 3 times.

HOTKEYS
    Ctrl+K  start / stop          Esc  quit

Coordinates below are set to YOUR calibrated cue pixels + capacity bar. Verify
with the live readout (idle) and `python3 prospecting_macro.py calibrate`.

SETUP
    pip3 install pyobjc-framework-Quartz mss numpy --break-system-packages
    Grant Terminal: Accessibility + Screen Recording + Input Monitoring.
"""

import sys
import time
import threading

# ============================================================================
# USER SETTINGS (from the AHK)
# ============================================================================
DIG_SPEED            = 100    # percent
BASE_DURATION_MS     = 600    # dig hold = BASE_DURATION / (DigSpeed/100)
WALKBACK_MS          = 300    # hold S after the click, before shaking
SHAKE_TIMEOUT_MS     = 250    # max one shake-hold before retry
RECOVERY_WALKBACK_MS = 100    # tap S back when the dig didn't register

WALK_BURST_MS        = 25     # forward W burst length
MAX_WALK_MS          = 2000   # walk-forward cap
PRE_WALKBACK_CLICK_SLEEP_MS = 25
ENTER_PAN_WAIT_MS    = 350    # wait before shaking
SHAKE_POLL_MS        = 50     # empty-check spacing during the shake
MAX_RETRIES          = 3

COOKIE_ENABLED       = False  # cookie speed boost (image search) -- off
COOKIE_MULT          = 4.5

# ============================================================================
# DETECTION ZONES  (YOUR calibrated pixels)
# ============================================================================
# DetectWhitePixel: the white PROMPT text. Your cues (white when shown):
#   Collect Deposit (770,981) | Shake (830,981) | Pan (847,981)
# Set WHITE_PIX to whichever prompt the walk-forward should stop on.
WHITE_PIX     = (847, 981)                 # default: "Pan"
WHITE_OFFSETS = [(0, 0), (-5, 0), (5, 0), (0, -5), (0, 5)]
WHITE_MIN     = 200                        # r,g,b all > this -> white
WHITE_NEED    = 3                          # at least this many of the 5 points

# Capacity bar (yellow = full, gray = empty). Vertical scan range Y1..Y2.
PAN_FULL_X, PAN_FULL_Y1, PAN_FULL_Y2 = 1115, 890, 910     # right end -> full
YELLOW_RGB, YELLOW_TOL = (253, 230, 112), 20
PAN_EMPTY_X, PAN_EMPTY_Y1, PAN_EMPTY_Y2 = 690, 890, 910   # left -> empty (gray)
GRAY_VAL, GRAY_TOL = 140, 15
RECOVERY_X, RECOVERY_Y1, RECOVERY_Y2 = 700, 890, 910      # yellow present?
SCAN_STEP = 5

# --- keys / hotkeys ----------------------------------------------------------
KEY_W, KEY_S = 13, 1
VK_CTRL_L, VK_CTRL_R, VK_K, VK_ESC = 59, 62, 40, 53
TOGGLE_NAME = "Ctrl+K"

# ============================================================================
try:
    import numpy as np
    import mss
    import Quartz
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\n  pip3 install pyobjc-framework-Quartz "
             "mss numpy --break-system-packages")

_MSS = getattr(mss, "MSS", None) or mss.mss
_HID = getattr(Quartz, "kCGEventSourceStateHIDSystemState", 1)


# ---- timing -----------------------------------------------------------------
def sleep_ms(ms):
    end = time.perf_counter() + ms / 1000.0
    while True:
        rem = end - time.perf_counter()
        if rem <= 0:
            return
        if rem > 0.002:
            time.sleep(rem - 0.0015)
        else:
            while time.perf_counter() < end:
                pass
            return


# ---- Quartz input -----------------------------------------------------------
HID = Quartz.kCGHIDEventTap


def _post(ev):
    Quartz.CGEventPost(HID, ev)


def key_down(c): _post(Quartz.CGEventCreateKeyboardEvent(None, c, True))
def key_up(c):   _post(Quartz.CGEventCreateKeyboardEvent(None, c, False))


def _cursor():
    return Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))


def _mouse(kind):
    _post(Quartz.CGEventCreateMouseEvent(None, kind, _cursor(),
                                         Quartz.kCGMouseButtonLeft))


def mouse_down(): _mouse(Quartz.kCGEventLeftMouseDown)
def mouse_up():   _mouse(Quartz.kCGEventLeftMouseUp)


def click():
    mouse_down(); sleep_ms(15); mouse_up()


def release_all():
    key_up(KEY_W); key_up(KEY_S); mouse_up()


# ---- run state --------------------------------------------------------------
class State:
    running = False
    alive = True


_last_shake_timeout = False
_cookie_active = False


# ---- pixel detection (faithful ports) --------------------------------------
def pixel_rgb(sct, x, y):
    px = np.asarray(sct.grab({"left": x, "top": y, "width": 1, "height": 1}))[0, 0]
    return int(px[2]), int(px[1]), int(px[0])     # BGRA -> R,G,B


def _near(rgb, target, tol):
    return (abs(rgb[0] - target[0]) < tol and abs(rgb[1] - target[1]) < tol
            and abs(rgb[2] - target[2]) < tol)


def detect_white(sct):
    x0, y0 = WHITE_PIX
    n = 0
    for dx, dy in WHITE_OFFSETS:
        r, g, b = pixel_rgb(sct, x0 + dx, y0 + dy)
        if r > WHITE_MIN and g > WHITE_MIN and b > WHITE_MIN:
            n += 1
    return n >= WHITE_NEED


def _scan(sct, x, y1, y2, predicate):
    y = y1
    while y <= y2:
        if predicate(pixel_rgb(sct, x, y)):
            return True
        y += SCAN_STEP
    return False


def is_pan_full(sct):
    return _scan(sct, PAN_FULL_X, PAN_FULL_Y1, PAN_FULL_Y2,
                 lambda c: _near(c, YELLOW_RGB, YELLOW_TOL))


def is_pan_empty(sct):
    g = (GRAY_VAL, GRAY_VAL, GRAY_VAL)
    return _scan(sct, PAN_EMPTY_X, PAN_EMPTY_Y1, PAN_EMPTY_Y2,
                 lambda c: _near(c, g, GRAY_TOL))


def is_recovery_needed(sct):
    yellow = _scan(sct, RECOVERY_X, RECOVERY_Y1, RECOVERY_Y2,
                   lambda c: _near(c, YELLOW_RGB, YELLOW_TOL))
    return not yellow


def current_dig_speed():
    return DIG_SPEED * COOKIE_MULT if (COOKIE_ENABLED and _cookie_active) else DIG_SPEED


# ---- actions (faithful ports) ----------------------------------------------
def do_dig():
    if not State.running:
        return
    hold = BASE_DURATION_MS / (current_dig_speed() / 100.0)
    mouse_down(); sleep_ms(hold); mouse_up()


def sandshaking_sequence(sct):
    global _last_shake_timeout
    if not State.running:
        return
    # walk forward in 25ms bursts until the white prompt (or 2s)
    walk_start = time.perf_counter()
    pan_detected = False
    while (time.perf_counter() - walk_start) * 1000 < MAX_WALK_MS:
        if not State.running:
            return
        key_down(KEY_W); sleep_ms(WALK_BURST_MS); key_up(KEY_W)
        if detect_white(sct):
            pan_detected = True
            _last_shake_timeout = False
            break
        if _last_shake_timeout and (time.perf_counter() - walk_start) * 1000 >= MAX_WALK_MS:
            pan_detected = True
            break
    if not pan_detected or not State.running:
        return

    click()                                         # click before walking back
    sleep_ms(PRE_WALKBACK_CLICK_SLEEP_MS)

    wb = WALKBACK_MS * 0.75 if (COOKIE_ENABLED and _cookie_active) else WALKBACK_MS
    key_down(KEY_S); sleep_ms(wb); key_up(KEY_S)    # walkback
    if not State.running:
        return

    sleep_ms(ENTER_PAN_WAIT_MS)                      # wait to enter panning stage

    for _ in range(MAX_RETRIES):                     # shake, retry on timeout
        if not State.running:
            return
        session_start = time.perf_counter()
        mouse_down()
        while State.running:
            if is_pan_empty(sct):
                mouse_up(); _last_shake_timeout = False
                return                               # success
            if (time.perf_counter() - session_start) * 1000 >= SHAKE_TIMEOUT_MS:
                mouse_up(); _last_shake_timeout = True
                sleep_ms(50)
                break                                # retry
            sleep_ms(SHAKE_POLL_MS)
    mouse_up()
    _last_shake_timeout = True


def main_cycle(sct):
    # dig until full; recovery-walkback if the dig isn't registering
    while State.running:
        if is_pan_full(sct):
            break
        do_dig()
        if is_recovery_needed(sct):
            key_down(KEY_S); sleep_ms(RECOVERY_WALKBACK_MS); key_up(KEY_S)
            sleep_ms(100)
            continue
    if not State.running:
        return
    sandshaking_sequence(sct)
    release_all()                                    # stability: no stuck keys


# ---- hotkeys (poll key state -- no pynput, no abort) -----------------------
def _key_state(vk):
    return bool(Quartz.CGEventSourceKeyState(_HID, vk))


def hotkey_loop():
    prev = False
    while State.alive:
        if _key_state(VK_ESC):
            State.running = False
            State.alive = False
            return
        combo = (_key_state(VK_CTRL_L) or _key_state(VK_CTRL_R)) and _key_state(VK_K)
        if combo and not prev:
            State.running = not State.running
            print("\n[RUNNING]" if State.running else "\n[STOPPED]", flush=True)
        prev = combo
        time.sleep(0.03)


def main():
    print(__doc__.split("SETUP")[0])
    print(f"white={WHITE_PIX}  full=x{PAN_FULL_X}  empty=x{PAN_EMPTY_X}")
    print(f"{TOGGLE_NAME} start/stop, Esc quit. Starts STOPPED.\n")
    threading.Thread(target=hotkey_loop, daemon=True).start()
    last = 0.0
    with _MSS() as sct:
        try:
            while State.alive:
                if State.running:
                    main_cycle(sct)
                else:
                    release_all()
                    if time.time() - last > 1.5:
                        last = time.time()
                        w = "W" if detect_white(sct) else "-"
                        f = "FULL" if is_pan_full(sct) else "----"
                        e = "EMPTY" if is_pan_empty(sct) else "fill"
                        print(f"\r(idle - Ctrl+K) white[{w}] cap={f}/{e}     ",
                              end="", flush=True)
                    time.sleep(0.05)
        finally:
            release_all()
            print("\nStopped, inputs released.")


if __name__ == "__main__":
    main()
