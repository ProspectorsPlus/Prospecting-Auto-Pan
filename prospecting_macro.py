#!/usr/bin/env python3
"""
Prospecting auto-pan macro for macOS.

THE TWO BARS
    DIG BAR (vertical, right of the player) -- a TIMED skill bar.
        Hold LMB; a white line bounces along the bar. Release when the white
        line is on the GREEN end so it "lands" on green. The macro watches the
        green pixel and releases the instant the white line crosses it.
    CAPACITY BAR (horizontal, gray -> yellow, bottom centre) -- NOT timed.
        Each dig adds yellow. May take one or several digs to fill. When it is
        FULL (all yellow) we run the empty sequence.

ONE FULL LOOP
    repeat:
        dig_once()                 # hold LMB, release when white hits green
        if capacity_full():        # yellow reached the end of the capacity bar
            empty_sequence()       # S back into water, W forward + LMB shake,
                                   # then hold LMB to empty the pan
    # otherwise keep digging until the capacity bar fills

Hotkeys:
    F8   -> start / stop the loop
    Esc  -> quit (always releases keys/mouse first)

SETUP
    1.  pip3 install pyobjc-framework-Quartz mss numpy pynput --break-system-packages
    2.  Grant Terminal BOTH (then quit & reopen Terminal):
          System Settings > Privacy & Security > Accessibility
          System Settings > Privacy & Security > Screen Recording
    3.  Calibrate (prints PIXEL + RGB + GREEN/WHITE/YELLOW tags under cursor):
          python3 prospecting_macro.py calibrate
        - Hover the GREEN end of the dig bar      -> DIG_TRIGGER_PIXEL
        - Hover the RIGHT END of the capacity bar -> CAP_FULL_PIXEL
          (the spot that is gray when not full and YELLOW when full)
    4.  Run:
          python3 prospecting_macro.py
        Tab into Roblox, press F8 to start, F8 to pause, Esc to quit.
"""

import sys
import gc
import time

# ============================================================================
# CONFIG  --  everything you tune lives here
# ============================================================================

# --- DIG BAR: how to dig -----------------------------------------------------
# "timed"  -> just click LMB for DIG_CLICK_MS (best for FAST builds where a
#             single quick click fills the bar perfectly). No detection.
# "color"  -> hold LMB and release when the white line crosses the green pixel
#             (only works if the bar is slow enough to capture in flight).
DIG_MODE          = "timed"
DIG_CLICK_MS      = 75              # timed mode: length of the dig click (tune!)

# Pixel to watch for the white line in "color" mode (= calibrated green pixel).
# The macro releases LMB when this pixel goes WHITE (the line is on it).
DIG_TRIGGER_PIXEL = (1078, 532)     # (x, y)  from calibrate
# "White line present" = all channels bright:
WHITE_MIN         = 175             # r,g,b must all be >= this
# Release earlier/later to fight input lag:
#   If you OVERSHOOT (line passes green before release registers): make the line
#   trigger sooner by moving DIG_TRIGGER_PIXEL a few px toward where the line
#   comes FROM (down the bar), OR leave RELEASE_DELAY_MS at 0.
#   If you UNDERSHOOT (release too early): add a few ms here.
RELEASE_DELAY_MS  = 0
DIG_MAX_MS        = 4000            # safety: release after this even if the
                                   # white line is never detected
BETWEEN_DIGS_MS   = 280            # pause after a dig (lets capacity update /
                                   # dig animation finish) before next action

# --- CAPACITY BAR: full detection --------------------------------------------
# Pixel near the RIGHT END of the capacity bar: gray when not full, yellow full.
CAP_FULL_PIXEL    = (1120, 900)     # (x, y)  calibrated: yellow=full, gray=empty
# "Yellow" = high red & green, low blue:
YEL_MIN           = 140            # r and g must both be >= this
YEL_BLUE_GAP      = 45             # ...and blue must be <= min(r,g) - this

# --- TERRAIN DETECTION (drives S/W by what's under your feet) -----------------
# When True: hold S until you're in the liquid, hold W until back on dirt.
# Self-correcting, no fixed times. When False: use the fixed ms below.
USE_TERRAIN_DETECT = True
# A ground pixel near your feet that reads DIRT on land and the LIQUID in water.
TERRAIN_PIXEL     = (900, 720)      # (x, y)  calibrated
# Reference colours from calibrate. Each terrain can hold ONE colour OR a LIST
# of colours -- add more samples to cover two-tone / gradient ground (e.g. light
# and dark beige, bright and dark lava). Classification picks the nearest match.
DIRT_RGB          = [(250, 222, 150), (252, 240, 195)]   # beige sand (two-tone)
WET_RGB           = [(238, 130, 50), (242, 167, 60)]     # orange lava (two-tone)
# Robustness to the floor tint pulses (green/yellow/white flashes):
#   classification uses colour PROPORTION (chromaticity), so brightness/white
#   pulses are ignored; a pulse that changes hue is filtered by these:
TERRAIN_CONFIRM   = 3     # consecutive agreeing reads required before acting
WALK_BACK_MIN_MS  = 30    # ignore terrain reads for the first N ms of the S move
MOMENTUM_MIN_MS   = 30    # ignore terrain reads for the first N ms of the W move
WALK_BACK_MAX_MS  = 600   # safety cap on the S move (used in detect mode)
MOMENTUM_MAX_MS   = 600   # safety cap on the W move (used in detect mode)
RECOVER_MAX_MS    = 1500  # safety cap when walking back to dirt after drifting

# --- HUD TEXT LAYER (slow but authoritative -- corrects the colour check) -----
# The prompt under the capacity bar reads "Collect Deposit" on dirt, "Pan" in
# the liquid, "Shake" during the shake. We don't OCR it; we measure how much
# white text is present ("Collect Deposit" >> "Shake" > "Pan"). It updates
# slowly, so it ONLY overrides the fast colour terrain check when the two
# clearly disagree (catches dr/ift and "stuck" errors).
USE_TEXT_LAYER  = True
# Region of the prompt text (left, top, width, height). Size it to fit the
# WIDEST prompt, "Collect Deposit". Calibrate with `calibrate-text`.
TEXT_REGION     = (840, 980, 360, 60)
TEXT_WHITE_MIN  = 180     # brightness for a pixel to count as text
# White-pixel fraction bands -- run `calibrate-text`, read the 3 values, set:
TEXT_PAN_MAX    = 0.024   # frac <= this        -> "Pan"   (in liquid)  [meas 0.021]
TEXT_SHAKE_MAX  = 0.040   # this < frac <= ...  -> "Shake" [meas 0.027]; above -> Deposit [0.052]

# --- EMPTY SEQUENCE fixed timings (used only if USE_TERRAIN_DETECT = False) ---
WALK_BACK_MS      = 120    # hold S to back into the water
MOMENTUM_W_MS     = 140    # hold W (forward) so momentum carries you onto dirt
SHAKE_START_MS    = 70    # LMB tap that initiates the shake animation
SHAKE_HOLD_MS     = 3200  # hold LMB to empty the pan (depends on pan size)
GAP_MS            = 40    # tiny settle gap between phases
AFTER_EMPTY_MS    = 120   # settle after emptying, before digging again

# --- Detection sampling ------------------------------------------------------
SAMPLE_BOX        = 6     # NxN px box averaged around a watched pixel

# --- Keys (macOS ANSI virtual keycodes) --------------------------------------
KEY_W = 13
KEY_S = 1

# --- Hotkeys -----------------------------------------------------------------
# Toggle start/stop = Ctrl + (TOGGLE_VK).  Quit = Esc.
# F-keys are unreliable on macOS (media keys), so we use a Ctrl chord.
# TOGGLE_VK is the macOS virtual keycode of the second key: K=40, J=38, P=35.
TOGGLE_VK = 40          # 'K'  -> Ctrl+K toggles
TOGGLE_NAME = "Ctrl+K"

# ============================================================================
# Below here you normally don't need to edit.
# ============================================================================

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


def _as_ref_list(spec):
    """Accept a single (r,g,b) tuple OR a list of them; return a list."""
    if len(spec) and isinstance(spec[0], (int, float)):
        return [tuple(spec)]
    return [tuple(c) for c in spec]


# ---- Retina scale (detection in physical px; CGEvent in points) -------------
def get_scale(sct):
    main = sct.monitors[1]
    bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    logical_w = bounds.size.width
    return main["width"] / logical_w if logical_w else 1.0


# ---- High-precision timing --------------------------------------------------
def sleep_until(deadline):
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return
        if remaining > 0.002:
            time.sleep(remaining - 0.0015)
        else:
            while time.perf_counter() < deadline:
                pass
            return


def sleep_ms(ms):
    sleep_until(time.perf_counter() + ms / 1000.0)


# ---- Input engine (Quartz CGEvent, HID level) -------------------------------
HID = Quartz.kCGHIDEventTap


def _post(ev):
    Quartz.CGEventPost(HID, ev)


def key_down(code):
    _post(Quartz.CGEventCreateKeyboardEvent(None, code, True))


def key_up(code):
    _post(Quartz.CGEventCreateKeyboardEvent(None, code, False))


def _cursor_point():
    return Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))


def _mouse_event(kind):
    p = _cursor_point()
    _post(Quartz.CGEventCreateMouseEvent(None, kind, p, Quartz.kCGMouseButtonLeft))


def mouse_down():
    _mouse_event(Quartz.kCGEventLeftMouseDown)


def mouse_up():
    _mouse_event(Quartz.kCGEventLeftMouseUp)


def mouse_tap(ms):
    mouse_down()
    sleep_ms(ms)
    mouse_up()


# ---- Colour tests -----------------------------------------------------------
def is_white(r, g, b):
    return r >= WHITE_MIN and g >= WHITE_MIN and b >= WHITE_MIN


def is_yellow(r, g, b):
    return r >= YEL_MIN and g >= YEL_MIN and b <= min(r, g) - YEL_BLUE_GAP


# ---- Capture + detection ----------------------------------------------------
class Detector:
    def __init__(self, sct):
        self.sct = sct
        self.dig_region = self._box(DIG_TRIGGER_PIXEL)
        self.cap_region = self._box(CAP_FULL_PIXEL)
        self.terrain_region = self._box(TERRAIN_PIXEL)
        # one or more normalised reference colours per terrain (two-tone support)
        self._dirt = [self._chroma(c) for c in _as_ref_list(DIRT_RGB)]
        self._wet = [self._chroma(c) for c in _as_ref_list(WET_RGB)]
        l, t, w, h = TEXT_REGION
        self.text_region = {"left": l, "top": t, "width": w, "height": h}

    @staticmethod
    def _chroma(rgb):
        a = np.asarray(rgb, dtype=np.float64)
        s = a.sum()
        return a / s if s else a

    @staticmethod
    def _box(pixel):
        x, y = pixel
        h = SAMPLE_BOX
        return {"left": x - h // 2, "top": y - h // 2, "width": h, "height": h}

    def _rgb(self, region):
        img = np.asarray(self.sct.grab(region))[:, :, :3]   # BGRA -> BGR
        b, g, r = img.reshape(-1, 3).mean(0)
        return r, g, b

    def white_on_trigger(self):
        return is_white(*self._rgb(self.dig_region))

    def capacity_full(self):
        return is_yellow(*self._rgb(self.cap_region))

    def on_wet(self):
        """True if the feet pixel's colour proportion is closer to a WET (lava/
        water) reference than to any DIRT reference. Chromaticity ignores
        brightness/white pulses; nearest-of-many handles two-tone ground."""
        c = self._chroma(self._rgb(self.terrain_region))
        dw = min(np.sum((c - w) ** 2) for w in self._wet)
        dd = min(np.sum((c - d) ** 2) for d in self._dirt)
        return dw < dd

    def on_dirt(self):
        return not self.on_wet()

    def wet_stable(self, n=TERRAIN_CONFIRM):
        """Majority vote over n quick reads -> filters single-frame glitches."""
        return sum(1 for _ in range(n) if self.on_wet()) > n // 2

    # ---- HUD text layer (white-pixel fraction -> prompt word) ----------------
    def text_fraction(self):
        img = np.asarray(self.sct.grab(self.text_region))[:, :, :3]  # BGR
        white = np.all(img >= TEXT_WHITE_MIN, axis=2)
        return float(white.mean())

    def text_state(self):
        """'pan' (liquid), 'shake', or 'deposit' (dirt) by amount of text."""
        f = self.text_fraction()
        if f <= TEXT_PAN_MAX:
            return "pan"
        if f <= TEXT_SHAKE_MAX:
            return "shake"
        return "deposit"

    def text_state_stable(self, n=3):
        from collections import Counter
        return Counter(self.text_state() for _ in range(n)).most_common(1)[0][0]


# ---- Global state -----------------------------------------------------------
class State:
    running = False
    alive = True


def release_all():
    try:
        key_up(KEY_W)
        key_up(KEY_S)
        mouse_up()
    except Exception:
        pass


# ---- Cycle parts ------------------------------------------------------------
def dig_once(detector):
    """One dig. Timed mode = fixed click; color mode = release on white-on-green."""
    if DIG_MODE == "timed":
        mouse_down()
        sleep_ms(DIG_CLICK_MS)
        mouse_up()
        return
    # color mode
    mouse_down()
    deadline = time.perf_counter() + DIG_MAX_MS / 1000.0
    while State.running and time.perf_counter() < deadline:
        if detector.white_on_trigger():
            break
        # poll as fast as possible; the line can move quickly
    if RELEASE_DELAY_MS:
        sleep_ms(RELEASE_DELAY_MS)
    mouse_up()


def wait_until(cond, max_ms, confirm=1, min_ms=0):
    """Poll cond() until it is True for `confirm` reads in a row, or max_ms
    elapses. Ignores reads during the first `min_ms` (lets you clear the
    boundary / startup tint pulse before detection counts)."""
    start = time.perf_counter()
    deadline = start + max_ms / 1000.0
    gate = start + min_ms / 1000.0
    hits = 0
    while State.running and time.perf_counter() < deadline:
        if time.perf_counter() < gate:
            continue
        if cond():
            hits += 1
            if hits >= confirm:
                return True
        else:
            hits = 0
    return False


def walk_back(detector):
    key_down(KEY_S)                 # back up
    if USE_TERRAIN_DETECT:
        wait_until(detector.on_wet, WALK_BACK_MAX_MS,
                   confirm=TERRAIN_CONFIRM, min_ms=WALK_BACK_MIN_MS)
    else:
        sleep_ms(WALK_BACK_MS)
    key_up(KEY_S)


def momentum_and_shake(detector):
    key_down(KEY_W)                 # walk forward; momentum carries onto dirt
    mouse_tap(SHAKE_START_MS)       # tap LMB to start the shake animation
    if USE_TERRAIN_DETECT:
        wait_until(detector.on_dirt, MOMENTUM_MAX_MS,
                   confirm=TERRAIN_CONFIRM, min_ms=MOMENTUM_MIN_MS)
    else:
        remaining = MOMENTUM_W_MS - SHAKE_START_MS
        if remaining > 0:
            sleep_ms(remaining)
    key_up(KEY_W)


def shake_empty():
    mouse_down()
    sleep_ms(SHAKE_HOLD_MS)
    mouse_up()


def recover_to_dirt(detector):
    """Drifted into the liquid -> walk forward until back on dirt."""
    key_down(KEY_W)
    wait_until(detector.on_dirt, RECOVER_MAX_MS, confirm=TERRAIN_CONFIRM)
    key_up(KEY_W)


def empty_sequence(detector):
    walk_back(detector)
    if not State.running: return
    sleep_ms(GAP_MS)
    momentum_and_shake(detector)
    if not State.running: return
    sleep_ms(GAP_MS)
    shake_empty()
    sleep_ms(AFTER_EMPTY_MS)


def loop_step(detector):
    # Without terrain detection we can't reason about position: fall back to the
    # simple linear loop (dig, and empty when the capacity bar fills).
    if not USE_TERRAIN_DETECT:
        dig_once(detector)
        if not State.running: return
        sleep_ms(BETWEEN_DIGS_MS)
        if detector.capacity_full():
            empty_sequence(detector)
        return

    # Terrain-aware state machine: decide from (where am I) x (is the pan full).
    wet = detector.wet_stable()
    # Second layer: the HUD prompt is slow but authoritative about position, so
    # let it correct the fast colour read when it clearly says dirt vs liquid.
    if USE_TEXT_LAYER:
        t = detector.text_state_stable()
        if t == "deposit":
            wet = False         # prompt says "Collect Deposit" -> on dirt
        elif t == "pan":
            wet = True          # prompt says "Pan" -> in the liquid
        # "shake" -> mid-animation, keep the colour read
    full = detector.capacity_full()

    if wet and full:
        # In the liquid holding a full pan -> shake it empty, then get to dirt.
        shake_empty()
        sleep_ms(AFTER_EMPTY_MS)
        recover_to_dirt(detector)
    elif wet and not full:
        # Drifted into the liquid with an empty pan -> walk back to dirt.
        recover_to_dirt(detector)
    elif full:
        # On dirt with a full pan -> run the optimized empty sequence. If it
        # doesn't actually empty, next loop sees "full on dirt" again and retries.
        empty_sequence(detector)
    else:
        # On dirt with room in the pan -> dig.
        dig_once(detector)
        if not State.running: return
        sleep_ms(BETWEEN_DIGS_MS)


# ---- Hotkey listener --------------------------------------------------------
# Toggle is a Ctrl chord so it never types into the game and avoids macOS
# media-key F-keys. We track Ctrl state and match the second key by virtual
# keycode (robust even though Ctrl turns the char into a control code).
def make_listener():
    ctrl = {"down": False}
    CTRL_KEYS = {keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r}

    def on_press(key):
        if key in CTRL_KEYS:
            ctrl["down"] = True
            return
        if key == keyboard.Key.esc:
            print("[QUIT]")
            State.running = False
            State.alive = False
            release_all()
            return False
        vk = getattr(key, "vk", None)
        if ctrl["down"] and vk == TOGGLE_VK:
            State.running = not State.running
            print(f"[{'RUNNING' if State.running else 'PAUSED'}]")
            if not State.running:
                release_all()

    def on_release(key):
        if key in CTRL_KEYS:
            ctrl["down"] = False

    return keyboard.Listener(on_press=on_press, on_release=on_release)


# ---- Calibration ------------------------------------------------------------
def calibrate():
    print("CALIBRATE -- hover a target, read PIXEL/RGB, Ctrl+C to quit.")
    print("  GREEN end of dig bar      -> DIG_TRIGGER_PIXEL")
    print("  RIGHT END of capacity bar -> CAP_FULL_PIXEL (gray empty / YELLOW full)")
    print("  GROUND at your feet       -> TERRAIN_PIXEL, then read RGB while")
    print("                               standing on DIRT (->DIRT_RGB) and in")
    print("                               WATER/LAVA (->WET_RGB)\n")
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
                if g >= 110 and (g - r) >= 40 and (g - b) >= 40: tags += " GREEN"
                print(f"\rPIXEL=({px:>5},{py:>5})  RGB=({int(r):>3},{int(g):>3},"
                      f"{int(b):>3})  scale {scale:.2f} [{tags.strip()}]      ",
                      end="", flush=True)
                time.sleep(0.05)
        except KeyboardInterrupt:
            print("\nDone.")


def calibrate_text():
    """Measure the white-text fraction in TEXT_REGION so you can set the bands.
    Stand on dirt (Collect Deposit), in lava (Pan), and mid-shake (Shake);
    record the three FRAC values, then set TEXT_PAN_MAX / TEXT_SHAKE_MAX."""
    l, t, w, h = TEXT_REGION
    region = {"left": l, "top": t, "width": w, "height": h}
    print(f"CALIBRATE TEXT -- region {TEXT_REGION}. Ctrl+C to quit.")
    print("Read FRAC while: on DIRT (Collect Deposit), in LAVA (Pan), mid-SHAKE.")
    print("If the region doesn't sit over the prompt, adjust TEXT_REGION.\n")
    with _MSS() as sct:
        try:
            while True:
                img = np.asarray(sct.grab(region))[:, :, :3]
                frac = float(np.all(img >= TEXT_WHITE_MIN, axis=2).mean())
                if frac <= TEXT_PAN_MAX:
                    guess = "pan"
                elif frac <= TEXT_SHAKE_MAX:
                    guess = "shake"
                else:
                    guess = "deposit"
                print(f"\rFRAC={frac:0.4f}   guess={guess:8}      ",
                      end="", flush=True)
                time.sleep(0.05)
        except KeyboardInterrupt:
            print("\nDone.")


def log_calibration():
    """Log feet colour, capacity colour and text fraction to a CSV while you
    play. Tag what you're doing with global hotkeys so the data is labelled:
        Ctrl+1 = on DIRT      Ctrl+2 = in LAVA/WATER
        Ctrl+3 = mid-SHAKE    Ctrl+4 = DIGGING
        Esc    = stop & save
    Hand me the CSV and I'll read off DIRT_RGB / WET_RGB and the text bands."""
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "prospecting_calib_log.csv")
    label = {"v": "?"}
    LABELS = {18: "DIRT", 19: "LAVA", 20: "SHAKE", 21: "DIG"}  # vk of 1,2,3,4
    ctrl = {"down": False}
    stop = {"v": False}
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

    tl, tt, tw, th = TEXT_REGION
    treg = {"left": tl, "top": tt, "width": tw, "height": th}
    freg = {"left": TERRAIN_PIXEL[0] - 3, "top": TERRAIN_PIXEL[1] - 3,
            "width": 6, "height": 6}
    creg = {"left": CAP_FULL_PIXEL[0] - 3, "top": CAP_FULL_PIXEL[1] - 3,
            "width": 6, "height": 6}

    print("LOGGING to:", path)
    print("Tag while playing: Ctrl+1 DIRT, Ctrl+2 LAVA, Ctrl+3 SHAKE, Ctrl+4 DIG.")
    print("Press Esc to stop & save.\n")
    with _MSS() as sct, open(path, "w") as f:
        f.write("time,label,feet_r,feet_g,feet_b,text_frac,cap_r,cap_g,cap_b\n")
        t0 = time.time()
        while not stop["v"]:
            fb, fg, fr = np.asarray(sct.grab(freg))[:, :, :3].reshape(-1, 3).mean(0)
            cb, cg, cr = np.asarray(sct.grab(creg))[:, :, :3].reshape(-1, 3).mean(0)
            img = np.asarray(sct.grab(treg))[:, :, :3]
            frac = float(np.all(img >= TEXT_WHITE_MIN, axis=2).mean())
            ts = time.time() - t0
            f.write(f"{ts:.2f},{label['v']},{int(fr)},{int(fg)},{int(fb)},"
                    f"{frac:.4f},{int(cr)},{int(cg)},{int(cb)}\n")
            f.flush()
            print(f"\r{ts:6.1f}s {label['v']:6} feet=({int(fr)},{int(fg)},"
                  f"{int(fb)}) text={frac:.4f} cap=({int(cr)},{int(cg)},"
                  f"{int(cb)})    ", end="", flush=True)
            time.sleep(0.1)
    print("\nSaved:", path)


# ---- Main -------------------------------------------------------------------
def main():
    print(__doc__.split("SETUP")[0])
    print(f"Dig trigger pixel {DIG_TRIGGER_PIXEL} (white-line release).")
    print(f"Capacity-full pixel {CAP_FULL_PIXEL} (yellow = full).")
    print(f"Press {TOGGLE_NAME} to start/stop, Esc to quit.\n")

    listener = make_listener()
    listener.start()

    gc.disable()
    with _MSS() as sct:
        detector = Detector(sct)
        for _ in range(5):                  # warm up capture path
            sct.grab(detector.dig_region)
        try:
            while State.alive:
                if State.running:
                    loop_step(detector)
                else:
                    time.sleep(0.02)
        finally:
            release_all()
            gc.enable()
            print("Stopped, all inputs released.")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    # Variant launchers: `python3 prospecting_macro.py old [monitor|...]` runs the
    # sibling file prospecting_<variant>.py (forwarding any remaining args). This
    # is why `old` must be dispatched here -- otherwise it silently fell through
    # to the launcher's own main() and none of the variant's code ran.
    VARIANTS = {"old", "other", "new", "friend"}
    if arg in VARIANTS or arg == "ui":
        import os
        import subprocess
        here = os.path.dirname(os.path.abspath(__file__))
        target = os.path.join(here, "prospecting_ui.py" if arg == "ui"
                              else f"prospecting_{arg}.py")
        if not os.path.exists(target):
            sys.exit(f"File not found: {target}")
        print(f"[launcher] running {os.path.basename(target)} "
              f"{' '.join(sys.argv[2:])}".rstrip())
        raise SystemExit(subprocess.run(
            [sys.executable, target, *sys.argv[2:]]).returncode)
    if arg == "calibrate":
        calibrate()
    elif arg == "calibrate-text":
        calibrate_text()
    elif arg == "log":
        log_calibration()
    else:
        main()
