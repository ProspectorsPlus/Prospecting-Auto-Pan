#!/usr/bin/env python3
"""
prospecting_other.py -- Python (macOS) port of the friend's AHK macro
(SandshakingMacro_CLEAN.ahk). Faithful conversion of the logic.

FLOW
    repeat:
      DIG until the pan is FULL:
          - hold LMB for (BASE_DURATION / (DigSpeed/100)) ms per dig
          - if recovery needed (yellow missing at RECOVERY pixel) -> tap S back
      SANDSHAKE:
          - walk forward in 25ms W-bursts until a WHITE pixel appears
          - click, then hold S (walkback), wait, then hold LMB to shake
          - stop when the pan reads EMPTY (gray); retry on timeout

HOTKEYS
    Ctrl+K  start / stop
    Esc     quit

IMPORTANT: the pixel coordinates below are the FRIEND'S screen. Recalibrate
them for YOUR screen with:   python3 prospecting_macro.py calibrate
(hover each spot, read the PIXEL = (x, y), paste it in).

SETUP
    pip3 install pyobjc-framework-Quartz mss numpy pynput --break-system-packages
    Grant Terminal: Accessibility + Screen Recording + Input Monitoring.
"""

import sys
import time
import faulthandler
faulthandler.enable()   # dump a traceback (incl. C frames) on a hard crash

# ============================================================================
# SETTINGS (from the AHK USER SETTINGS)
# ============================================================================
DIG_SPEED          = 100     # percent
BASE_DURATION_MS   = 75      # dig hold (your fast build: ~75ms click)
WALKBACK_MS        = 110     # hold S after the click (your ~95-120ms)
SHAKE_TIMEOUT_MS   = 600     # max one shake-hold before retry
RECOVERY_WALKBACK_MS = 100   # (unused; kept for reference)

# walk-forward search (25ms bursts up to a safety cap)
WALK_BURST_MS      = 25
MAX_WALK_MS        = 2000
FORWARD_EXTRA_MS   = 0     # keep holding W this long AFTER lava is first seen
                            # (raise if it doesn't go far enough for the shake)
PRE_WALKBACK_CLICK_SLEEP_MS = 25   # click; brief settle before walkback
ENTER_PAN_WAIT_MS  = 150     # wait before shaking (faster build)
SHAKE_POLL_MS      = 20      # poll spacing for the empty-check
MAX_RETRIES        = 3

# optional cookie speed boost (AHK image-searched cookie.png; off by default)
COOKIE_ENABLED     = False
COOKIE_SPEED_MULT  = 4.5

# ============================================================================
# DETECTION (reuses YOUR calibrated regions -- nothing new to calibrate)
# ============================================================================
# The friend's "walk forward until a white pixel" cue is replaced by YOUR
# lava-edge detection: walk forward until EDGE_REGION reads lava.
EDGE_REGION   = (864, 548, 70, 50)        # same as your smart version
DIRT_REFS     = ((250, 222, 150), (252, 240, 195))
LIQUID_REFS   = ((238, 130, 50), (242, 167, 60))

# Capacity bar (whole bar). "Full" = right end yellow; "empty" = no yellow.
CAP_REGION    = (675, 876, 452, 39)
CAP_RIGHT_FRAC = 0.12
YEL_MIN       = 140
YEL_BLUE_GAP  = 45
CAP_FULL_RATIO  = 0.40       # right-end yellow fraction >= this -> full
CAP_EMPTY_FRAC  = 0.04       # whole-bar yellow fraction < this -> empty

# --- hotkeys -----------------------------------------------------------------
KEY_W, KEY_S = 13, 1         # macOS virtual keycodes
TOGGLE_VK    = 40            # 'K' -> Ctrl+K
TOGGLE_NAME  = "Ctrl+K"

# ============================================================================
try:
    import numpy as np
    import mss
    import Quartz
    import prospecting_core as core
    from prospecting_core import classify_region, ref_chroma, Config, DIRT, LIQUID
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\n  pip3 install pyobjc-framework-Quartz "
             "mss numpy pynput --break-system-packages")

_MSS = getattr(mss, "MSS", None) or mss.mss
_CFG = Config(dirt_refs=DIRT_REFS, liquid_refs=LIQUID_REFS)
_DIRT_C = ref_chroma(DIRT_REFS)
_LIQ_C = ref_chroma(LIQUID_REFS)


def _cap_right():
    l, t, w, h = CAP_REGION
    sw = max(8, int(w * CAP_RIGHT_FRAC))
    return (l + w - sw, t, sw, h)


CAP_RIGHT_REGION = _cap_right()


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


# ---- region detection (reuses your calibrated regions) ----------------------
def _grab_rgb(sct, region):
    l, t, w, h = region
    img = np.asarray(sct.grab({"left": l, "top": t, "width": w, "height": h}))
    return img[:, :, :3][:, :, ::-1]              # BGRA -> RGB


def _yellow_fraction(sct, region):
    rgb = _grab_rgb(sct, region).reshape(-1, 3).astype(np.int16)
    r, g, b = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    return float(((r >= YEL_MIN) & (g >= YEL_MIN)
                  & (b <= np.minimum(r, g) - YEL_BLUE_GAP)).mean())


def reached_lava(sct):
    """Replaces the friend's white-pixel cue: True once EDGE_REGION is lava."""
    rgb = _grab_rgb(sct, EDGE_REGION)
    return classify_region(rgb, _CFG, _DIRT_C, _LIQ_C)["position"] == LIQUID


def is_pan_full(sct):
    return _yellow_fraction(sct, CAP_RIGHT_REGION) >= CAP_FULL_RATIO


def is_pan_empty(sct):
    return _yellow_fraction(sct, CAP_REGION) < CAP_EMPTY_FRAC


# ---- run state --------------------------------------------------------------
class State:
    running = False
    alive = True


_last_shake_timeout = False


def current_dig_speed():
    return DIG_SPEED * COOKIE_SPEED_MULT if COOKIE_ENABLED else DIG_SPEED


# ---- actions ----------------------------------------------------------------
def do_dig():
    if not State.running:
        return
    hold = BASE_DURATION_MS / (current_dig_speed() / 100.0)
    mouse_down()
    sleep_ms(hold)
    mouse_up()


def sandshaking_sequence(sct):
    global _last_shake_timeout
    if not State.running:
        return

    # walk forward in 25ms W-bursts until you REACH THE LAVA (or 2s safety)
    walk_start = time.perf_counter()
    pan_detected = False
    while (time.perf_counter() - walk_start) * 1000 < MAX_WALK_MS:
        if not State.running:
            return
        key_down(KEY_W); sleep_ms(WALK_BURST_MS); key_up(KEY_W)
        if reached_lava(sct):
            pan_detected = True
            _last_shake_timeout = False
            break
        if _last_shake_timeout and (time.perf_counter() - walk_start) * 1000 >= MAX_WALK_MS:
            pan_detected = True
            break
    if not pan_detected or not State.running:
        return

    # keep walking forward a bit MORE so we're properly into the lava
    if FORWARD_EXTRA_MS > 0:
        key_down(KEY_W); sleep_ms(FORWARD_EXTRA_MS); key_up(KEY_W)
        if not State.running:
            return

    # click BEFORE walking back
    click()
    sleep_ms(PRE_WALKBACK_CLICK_SLEEP_MS)

    # walkback (S) -- undo the base walkback PLUS the extra forward distance
    wb = (WALKBACK_MS * 0.75 if COOKIE_ENABLED else WALKBACK_MS) + FORWARD_EXTRA_MS
    key_down(KEY_S); sleep_ms(wb); key_up(KEY_S)
    if not State.running:
        return

    # wait to enter panning stage (also delayed by the extra forward time)
    sleep_ms(ENTER_PAN_WAIT_MS + FORWARD_EXTRA_MS)

    # shake, retrying on timeout, until the pan reads empty
    for _ in range(MAX_RETRIES):
        if not State.running:
            return
        session_start = time.perf_counter()
        mouse_down()
        while State.running:
            if is_pan_empty(sct):
                mouse_up()
                _last_shake_timeout = False
                return                                  # success
            if (time.perf_counter() - session_start) * 1000 >= SHAKE_TIMEOUT_MS:
                mouse_up()
                _last_shake_timeout = True
                sleep_ms(50)
                break                                   # retry
            sleep_ms(SHAKE_POLL_MS)
    mouse_up()
    _last_shake_timeout = True


def main_cycle(sct):
    """One full AHK MainLoop pass: dig until full, then sandshake."""
    while State.running:
        if is_pan_full(sct):
            break
        do_dig()
    if not State.running:
        return
    sandshaking_sequence(sct)
    release_all()


# ---- hotkeys (poll key STATE only -- no pynput keyboard listener) -----------
# pynput's keyboard Listener touches macOS TIS keyboard-layout APIs that aren't
# thread-safe with our CGEvent key creation (-> hard abort). Reading key state
# with CGEventSourceKeyState does NOT touch TIS, so it's safe alongside input.
import threading

VK_CTRL_L, VK_CTRL_R, VK_K, VK_ESC = 59, 62, 40, 53
_HID = getattr(Quartz, "kCGEventSourceStateHIDSystemState", 1)


def _key_state(vk):
    return bool(Quartz.CGEventSourceKeyState(_HID, vk))


def hotkey_loop():
    prev_combo = False
    while State.alive:
        if _key_state(VK_ESC):
            State.running = False
            State.alive = False
            return
        combo = (_key_state(VK_CTRL_L) or _key_state(VK_CTRL_R)) and _key_state(VK_K)
        if combo and not prev_combo:           # rising edge = one toggle
            State.running = not State.running
            print("\n[RUNNING]" if State.running else "\n[STOPPED]", flush=True)
        prev_combo = combo
        time.sleep(0.03)


def start_hotkeys():
    threading.Thread(target=hotkey_loop, daemon=True).start()


def main():
    print(__doc__.split("SETUP")[0])
    print(f"{TOGGLE_NAME} start/stop, Esc quit. Starts STOPPED.\n")
    start_hotkeys()
    print("Idling -- tab into Roblox and press Ctrl+K to start.", flush=True)
    last_beat = 0.0
    with _MSS() as sct:
        try:
            while State.alive:
                if State.running:
                    try:
                        main_cycle(sct)
                    except Exception as e:
                        import traceback
                        print(f"\n[ERROR] {e}")
                        traceback.print_exc()
                        State.running = False     # pause so you can read it
                else:
                    release_all()
                    if time.time() - last_beat > 2:
                        last_beat = time.time()
                        print("\r(idle - Ctrl+K to start)      ", end="", flush=True)
                    time.sleep(0.05)
        finally:
            release_all()
            print("\nStopped, inputs released.")


if __name__ == "__main__":
    main()
