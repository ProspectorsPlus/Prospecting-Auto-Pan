#!/usr/bin/env python3
"""
prospecting_new.py -- CUE-driven prospecting macro (macOS).

Instead of detecting terrain colour, it reads the on-screen PROMPT cues under
the Pan Fill bar ("Collect Deposit" = on land, "Pan" = in the water, "Shake").

FLOW (one cycle):
    1. hold W until the "Collect Deposit" cue shows (you're on land)
    2. fast dig (one quick click)
    3. hold S until the "Pan" cue shows (you're in the water)
    4. CLICK LMB to start the shake AND simultaneously hold W to ride back
       onto the land -- hold W until "Collect Deposit" shows again
    -> repeat

HOTKEYS
    Ctrl+K  start / stop
    Esc     quit

Cue pixels are white-ish (~226) when that prompt's text is present.
Recalibrate with:  python3 prospecting_macro.py calibrate

SETUP
    pip3 install pyobjc-framework-Quartz mss numpy --break-system-packages
    Grant Terminal: Accessibility + Screen Recording + Input Monitoring.
"""

import sys
import time
import threading

# ============================================================================
# CUE PIXELS (white when that prompt shows)  -- from calibrate
# ============================================================================
DEPOSIT_PIX = (770, 981)     # "Collect Deposit" -> on land
PAN_PIX     = (847, 981)     # "Pan"             -> in the water
SHAKE_PIX   = (830, 981)     # "Shake"           -> mid-shake
CUE_BOX     = 3              # NxN px sampled around each pixel
# Cue is white-ish text. ~5% tolerance (was tighter) -> detects faster.
WHITE_MIN   = 160            # all channels >= this  AND
WHITE_SPREAD = 65            # max-min channel spread <= this  -> white text

# Capacity bar pixel: GRAY when the pan is empty, coloured when it has fill.
# Used to know the shake finished (the cue gets stuck on "Shake" until you move,
# so we confirm "empty" here instead and just start digging).
CAP_PIX     = (690, 900)     # <-- a point at the start of the Pan Fill bar
GRAY_SPREAD = 22             # max-min <= this  AND
GRAY_LO, GRAY_HI = 95, 190   # brightness in this range  -> empty (gray)

# ============================================================================
# TIMINGS
# ============================================================================
DIG_CLICK_MS  = 15           # quick 1-click dig (no timing needed)
SETTLE_MS     = 180          # pause after reaching a cue, before acting (SLOW)
POST_DIG_MS   = 180          # settle after digging, before walking back
SHAKE_CLICK_MS = 40          # CLICK to start the shake
MOMENTUM_MS    = 150         # brief W held WHILE shaking -> momentum onto land
SHAKE_HOLD_MS  = 1000        # keep holding LMB to finish the pan as you glide in
RECOVER_ON_PAN = True        # if you still read "Pan" after, walk fwd to recover
GAP_MS        = 180          # settle between cycles
POLL_MS       = 5            # cue poll spacing
FWD_MAX_MS    = 1500         # safety cap getting onto land
BACK_MAX_MS   = 1500         # safety cap walking back to the water
RETURN_MAX_MS = 1500         # safety cap riding back onto land after the shake

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


def click(ms):
    mouse_down(); sleep_ms(ms); mouse_up()


def drag_for(ms):
    """Keep LMB 'alive' (held) for ms by streaming drag events at the cursor."""
    end = time.perf_counter() + ms / 1000.0
    while time.perf_counter() < end and State.running:
        _post(Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventLeftMouseDragged, _cursor(),
            Quartz.kCGMouseButtonLeft))
        sleep_ms(16)


def release_all():
    key_up(KEY_W); key_up(KEY_S); mouse_up()


# ---- run state --------------------------------------------------------------
class State:
    running = False
    alive = True


# ---- cue detection ----------------------------------------------------------
def _box(p):
    x, y = p
    h = CUE_BOX
    return {"left": x - h // 2, "top": y - h // 2, "width": h, "height": h}


def _rgb(sct, p):
    img = np.asarray(sct.grab(_box(p)))[:, :, :3]      # BGRA -> BGR
    b, g, r = img.reshape(-1, 3).mean(0)
    return r, g, b


def _white(rgb):
    r, g, b = rgb
    return min(r, g, b) >= WHITE_MIN and (max(r, g, b) - min(r, g, b)) <= WHITE_SPREAD


def cue_deposit(sct):
    return _white(_rgb(sct, DEPOSIT_PIX))


def cue_pan(sct):
    # Pan's pixel white, but NOT the (leftmost) Collect-Deposit pixel -- so the
    # wide "Collect Deposit" text can't be mistaken for "Pan".
    return _white(_rgb(sct, PAN_PIX)) and not _white(_rgb(sct, DEPOSIT_PIX))


def cue_shake(sct):
    return _white(_rgb(sct, SHAKE_PIX)) and not _white(_rgb(sct, DEPOSIT_PIX))


def pan_empty(sct):
    """True when the start of the Pan Fill bar reads GRAY (no fill) = emptied."""
    r, g, b = _rgb(sct, CAP_PIX)
    return ((max(r, g, b) - min(r, g, b)) <= GRAY_SPREAD
            and GRAY_LO <= (r + g + b) / 3 <= GRAY_HI)


# ---- helpers ----------------------------------------------------------------
def wait_for(sct, cond, max_ms):
    """Poll cond(sct) until True or max_ms. Returns True if seen."""
    deadline = time.perf_counter() + max_ms / 1000.0
    while State.running and time.perf_counter() < deadline:
        if cond(sct):
            return True
        sleep_ms(POLL_MS)
    return False


def hold_until(sct, key, cond, max_ms):
    """Hold a movement key until cond(sct) is True (or max_ms). Returns seen."""
    key_down(key)
    seen = wait_for(sct, cond, max_ms)
    key_up(key)
    return seen


# ---- hotkeys (poll key STATE only -- no pynput, no TIS abort) ---------------
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


# ---- one cycle --------------------------------------------------------------
def cycle(sct):
    # 1. fast dig (we're on land, re-anchored at the land edge each cycle)
    click(DIG_CLICK_MS)
    sleep_ms(POST_DIG_MS)
    if not State.running: return
    # 2. walk back until "Pan" (in the water)
    hold_until(sct, KEY_S, cue_pan, BACK_MAX_MS)
    if not State.running: return
    sleep_ms(SETTLE_MS)             # settle in the water before the shake
    # 3. THE SPECIAL TECHNIQUE: click to start the shake AND hold W at the same
    #    time, so the momentum rides you back onto the land WHILE the pan
    #    animates -- you land on dirt as it finishes (no walking back-and-forth).
    click(SHAKE_CLICK_MS)           # start the shake...
    key_down(KEY_W)                 # ...and momentum back toward land
    mouse_down()                    # hold LMB to actually shake
    drag_for(MOMENTUM_MS)           # brief W + shake -> build momentum
    key_up(KEY_W)                   # release W; the glide carries you onto land
    drag_for(SHAKE_HOLD_MS)         # keep shaking as you glide in
    mouse_up()
    if not State.running: return
    # 4. RECOVERY ONLY: if you accidentally panned on the water (still reading
    #    "Pan"), walk forward until "Collect Deposit" to get back onto the land.
    if RECOVER_ON_PAN and cue_pan(sct):
        hold_until(sct, KEY_W, cue_deposit, FWD_MAX_MS)
    sleep_ms(GAP_MS)


def main():
    print(__doc__.split("SETUP")[0])
    print(f"Cues  deposit={DEPOSIT_PIX}  pan={PAN_PIX}  shake={SHAKE_PIX}")
    print(f"{TOGGLE_NAME} start/stop, Esc quit. Starts STOPPED.\n")
    threading.Thread(target=hotkey_loop, daemon=True).start()
    last = 0.0
    with _MSS() as sct:
        try:
            while State.alive:
                if not State.running:
                    release_all()
                    if time.time() - last > 1.5:
                        last = time.time()
                        d = "D" if cue_deposit(sct) else "-"
                        p = "P" if cue_pan(sct) else "-"
                        s = "S" if cue_shake(sct) else "-"
                        e = "EMPTY" if pan_empty(sct) else "fill"
                        print(f"\r(idle - Ctrl+K) cues [{d}{p}{s}]  cap={e}   ",
                              end="", flush=True)
                    time.sleep(0.05)
                    continue
                cycle(sct)
        finally:
            release_all()
            print("\nStopped, inputs released.")


if __name__ == "__main__":
    main()
