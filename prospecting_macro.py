#!/usr/bin/env python3
"""
prospecting_macro.py -- robust auto-pan for macOS (Prospecting).

This is the IO layer. All perception + decision logic lives in
prospecting_core.py (pure, unit-tested by prospecting_selftest.py). Here we:
  * capture screen regions (mss),
  * classify terrain by region voting with noise rejection,
  * read the HUD prompt + capacity bar,
  * fuse them and run the tick-based state machine,
  * reconcile actuators (idempotent key/mouse holds, precise dig click),
  * FAIL SAFE: on any uncertainty/stuck state, stop everything and alert you,
  * log telemetry every tick for debugging.

HOTKEYS
    Ctrl+K  start / pause (also clears a SAFE_STOP and resumes)
    Esc     quit (always releases inputs first)

MODES
    python3 prospecting_macro.py            # run (new supervised version)
    python3 prospecting_macro.py old        # run the older pure-timed version
    python3 prospecting_macro.py other      # run the AHK-port (friend's) version
    python3 prospecting_macro.py new        # run the cue-driven version
    python3 prospecting_macro.py friend     # run the friend's AHK macro (ported)
    python3 prospecting_macro.py monitor    # live perception readout, no input
    python3 prospecting_macro.py calibrate      # hover -> pixel + RGB + tags
    python3 prospecting_macro.py grid           # visual grid calibrator (browser)
    python3 prospecting_macro.py log            # labelled CSV capture

SETUP
    pip3 install pyobjc-framework-Quartz mss numpy pynput --break-system-packages
    Grant Terminal: Accessibility + Screen Recording + Input Monitoring
    (System Settings > Privacy & Security), then reopen Terminal.
"""

import sys
import os
import gc
import math
import time
import subprocess

import prospecting_core as core
from prospecting_core import (Config, Decider, classify_region,
                              capacity_full, ref_chroma,
                              DIG, EMPTY, RECOVER, SAFE,
                              DIRT, LIQUID, UNKNOWN)

# ============================================================================
# IO / CALIBRATION CONFIG  (perception + FSM tuning lives in core.Config)
# ============================================================================

# --- geometry from the grid calibrator --------------------------------------
PLAYER_ANCHOR  = (899, 618)              # your character's centre (top-down)
BOUNDARY_LINE  = [(857, 643), (966, 648)]  # dirt/lava edge (just below you)
# You FACE the lava (W walks down into it, S returns up to dirt). EDGE_REGION
# sits on the DIRT side (above the anchor, clear of your body): it reads DIRT
# while you're on dirt and flips to LIQUID once you've stepped onto the lava.
# It drives BOTH the fast maneuver and the position sensing.
EDGE_REGION    = (864, 548, 70, 50)      # (l, t, w, h)  <-- tune via monitor

# CAPACITY: full bar from calibrate; we auto-use its RIGHT END for "full".
CAP_REGION     = (675, 876, 452, 39)
CAP_RIGHT_FRAC = 0.12                    # rightmost fraction = the full detector

# --- colour thresholds (only used by IO-side sensors) -----------------------
TEXT_WHITE_MIN = 180                      # brightness to count a text pixel
YEL_MIN        = 140                      # capacity "yellow": r,g >= this
YEL_BLUE_GAP   = 45                       #              and  b <= min(r,g)-this

# --- dig ---------------------------------------------------------------------
DIG_CLICK_MS    = 75      # length of one dig click (your build)
CAP_CHECK_MS    = 40      # settle after a dig before checking if full
BETWEEN_DIGS_MS = 200     # pause between digs while the pan is NOT full

# --- DYNAMIC empty maneuver --------------------------------------------------
# Face the LAVA: W walks INTO it, S returns to the dirt. We poll ONE small
# region as fast as possible, so the forward leg is dynamic (stop the instant
# lava appears) -- no fixed time to drift.
EDGE_REGION    = (PLAYER_ANCHOR[0] - 30, PLAYER_ANCHOR[1] - 10, 60, 50)
EDGE_POLL_MS   = 3        # poll spacing on the dynamic legs (fast reaction)
FWD_MAX_MS     = 400      # SAFETY: never hold W into lava longer than this
BACK_MAX_MS    = 600      # SAFETY: max time to return to dirt
SHAKE_CLICK_MS      = 60   # click that starts the shake animation
SHAKE_HOLD_DELAY_MS = 40   # let-go gap after the click, before the empty-hold
SHAKE_HOLD_MS       = 3000 # max hold to empty (stops early when pan reads empty)
AFTER_EMPTY_MS      = 80   # settle after emptying
CAP_EMPTY_FRAC = 0.04      # whole-bar yellow fraction below this -> pan empty

# --- keys / hotkeys ----------------------------------------------------------
KEY_W, KEY_S   = 13, 1                    # macOS virtual keycodes
TOGGLE_VK      = 40                       # 'K' -> Ctrl+K toggles
TOGGLE_NAME    = "Ctrl+K"

# --- fail-safe alert ---------------------------------------------------------
ALERT_SOUND    = "/System/Library/Sounds/Sosumi.aiff"

# --- telemetry ---------------------------------------------------------------
LOG_PATH       = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "prospecting_run.log.csv")
LOG_MIN_INTERVAL = 0.25                   # also always logs on intent change

# --- perception / decision tuning (core defaults for the rest) ---------------
CFG = Config(
    dirt_refs=((250, 222, 150), (252, 240, 195)),
    liquid_refs=((238, 130, 50), (242, 167, 60)),
    decide_margin=0.10,    # commit to DIRT/LIQUID more readily (less UNKNOWN)
    cap_full_ratio=0.40,   # full reads ~0.95 yellow, empty ~0
)

# ============================================================================
try:
    import numpy as np
    import mss
    import Quartz
    from pynput import keyboard
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\n  pip3 install pyobjc-framework-Quartz "
             "mss numpy pynput --break-system-packages")

_MSS = getattr(mss, "MSS", None) or mss.mss
_DIRT_C = ref_chroma(CFG.dirt_refs)
_LIQ_C = ref_chroma(CFG.liquid_refs)


# ---- timing -----------------------------------------------------------------
def sleep_until(deadline):
    while True:
        rem = deadline - time.perf_counter()
        if rem <= 0:
            return
        if rem > 0.002:
            time.sleep(rem - 0.0015)
        else:
            while time.perf_counter() < deadline:
                pass
            return


def sleep_ms(ms):
    sleep_until(time.perf_counter() + ms / 1000.0)


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


def mouse_tap(ms):
    mouse_down(); sleep_ms(ms); mouse_up()


def dig_click():
    mouse_down(); sleep_ms(DIG_CLICK_MS); mouse_up()


def release_all():
    """Safety: lift W, S and LMB unconditionally."""
    key_up(KEY_W); key_up(KEY_S); mouse_up()


def hold_mouse(ms, stop_fn=None, confirm=2, min_ms=200):
    """Hold LMB up to ms, kept alive with drag events so a long hold registers.
    If stop_fn is given, release early once it's True for `confirm` reads in a
    row (never before min_ms). Returns True if it stopped early (pan emptied)."""
    mouse_down()
    start = time.perf_counter()
    end = start + ms / 1000.0
    hits = 0
    stopped = False
    while time.perf_counter() < end and State.running:
        _post(Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventLeftMouseDragged, _cursor(),
            Quartz.kCGMouseButtonLeft))
        if stop_fn is not None and (time.perf_counter() - start) * 1000 >= min_ms:
            hits = hits + 1 if stop_fn() else 0
            if hits >= confirm:
                stopped = True
                break
        sleep_ms(16)
    mouse_up()
    return stopped


# ---- capture + IO-side sensors ---------------------------------------------
def grab_rgb(sct, region):
    l, t, w, h = region
    img = np.asarray(sct.grab({"left": l, "top": t, "width": w, "height": h}))
    return img[:, :, :3][:, :, ::-1]          # BGRA -> RGB


def _cap_right_region():
    l, t, w, h = CAP_REGION
    sw = max(8, int(w * CAP_RIGHT_FRAC))
    return (l + w - sw, t, sw, h)


CAP_RIGHT_REGION = _cap_right_region()


def terrain_obs(sct):
    # the dirt-side EDGE_REGION: DIRT while you're on dirt, LIQUID once you've
    # stepped onto the lava (drives both the Decider and the maneuver).
    rgb = grab_rgb(sct, EDGE_REGION)
    return classify_region(rgb, CFG, _DIRT_C, _LIQ_C)


def edge_position(sct):
    """FAST single-region read for the dynamic maneuver (one capture, ~2-4ms)."""
    return terrain_obs(sct)["position"]


def _yellow_fraction(sct, region):
    rgb = grab_rgb(sct, region).reshape(-1, 3).astype(np.int16)
    r, g, b = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    return float(((r >= YEL_MIN) & (g >= YEL_MIN)
                  & (b <= np.minimum(r, g) - YEL_BLUE_GAP)).mean())


def capacity_obs(sct):
    ratio = _yellow_fraction(sct, CAP_RIGHT_REGION)   # right end -> "full"
    return ratio, capacity_full(ratio, CFG)


def pan_empty(sct):
    """Whole capacity bar has essentially no yellow -> fully empty."""
    return _yellow_fraction(sct, CAP_REGION) < CAP_EMPTY_FRAC


def _walk_until(sct, key, target, max_ms, confirm=2):
    """Hold a movement key, fast-polling EDGE_REGION, until it reads `target`
    for `confirm` reads, or max_ms (safety). Returns True if target reached."""
    key_down(key)
    start = time.perf_counter()
    hits = 0
    reached = False
    while (time.perf_counter() - start) * 1000 < max_ms and State.running:
        if edge_position(sct) == target:
            hits += 1
            if hits >= confirm:
                reached = True
                break
        else:
            hits = 0
        sleep_ms(EDGE_POLL_MS)
    key_up(key)
    return reached


def edge_empty(sct):
    """Dynamic maneuver (face the lava): hold W INTO the lava until lava is
    seen at the character (no drift), CLICK to start the shake, hold S back
    until on dirt, then hold LMB to empty. Returns True if the pan emptied."""
    # 1) forward (W) into the lava, stop the instant lava appears
    _walk_until(sct, KEY_W, LIQUID, FWD_MAX_MS)
    if not State.running: return False
    # 2) click to start the shake animation, let go
    mouse_tap(SHAKE_CLICK_MS)
    # 3) hold S back onto the dirt (dynamic, symmetric)
    _walk_until(sct, KEY_S, DIRT, BACK_MAX_MS)
    if not State.running: return False
    # 4) let-go gap, then hold LMB to empty (stops the instant the pan is empty)
    sleep_ms(SHAKE_HOLD_DELAY_MS)
    emptied = hold_mouse(SHAKE_HOLD_MS, stop_fn=lambda: pan_empty(sct))
    sleep_ms(AFTER_EMPTY_MS)
    return emptied


def recover_to_dirt(sct):
    """Drifted into the lava -> walk S until back on dirt (safety-capped)."""
    _walk_until(sct, KEY_S, DIRT, BACK_MAX_MS)


# ---- fail-safe alert --------------------------------------------------------
def alert(reason):
    print(f"\n*** SAFE STOP: {reason} ***  (Ctrl+K to resume, Esc to quit)")
    try:
        subprocess.Popen(["afplay", ALERT_SOUND])
    except Exception:
        print("\a", end="", flush=True)
    try:
        subprocess.Popen(["osascript", "-e",
                          f'display notification "{reason}" with title '
                          '"Prospecting macro stopped"'])
    except Exception:
        pass


# ---- telemetry --------------------------------------------------------------
class Telemetry:
    def __init__(self, path):
        self.f = open(path, "w")
        self.f.write("time,intent,position,confidence,coverage,pan_full,yellow\n")
        self.last = 0.0
        self.last_intent = None

    def log(self, now, intent, terr, full, yellow):
        if intent == self.last_intent and now - self.last < LOG_MIN_INTERVAL:
            return
        self.last, self.last_intent = now, intent
        self.f.write(f"{now:.2f},{intent},{terr['position']},"
                     f"{terr['confidence']:.2f},{terr['coverage']:.2f},"
                     f"{full},{yellow:.2f}\n")
        self.f.flush()

    def close(self):
        try: self.f.close()
        except Exception: pass


# ---- shared run state -------------------------------------------------------
class State:
    running = False
    alive = True


def make_listener(decider):
    ctrl = {"down": False}
    CTRL = {keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r}

    def on_press(key):
        if key in CTRL:
            ctrl["down"] = True
            return
        if key == keyboard.Key.esc:
            State.running = False
            State.alive = False
            return False
        if ctrl["down"] and getattr(key, "vk", None) == TOGGLE_VK:
            State.running = not State.running
            if State.running:
                decider.reset(time.perf_counter())   # clear any SAFE latch
                print("\n[RUNNING]")
            else:
                print("\n[PAUSED]")

    def on_release(key):
        if key in CTRL:
            ctrl["down"] = False

    return keyboard.Listener(on_press=on_press, on_release=on_release)


# ---- main loop --------------------------------------------------------------
def main():
    print(__doc__.split("SETUP")[0])
    print(f"Anchor {PLAYER_ANCHOR}  edge={EDGE_REGION}  "
          f"cap-right={CAP_RIGHT_REGION}")
    print(f"Logging to {LOG_PATH}")
    print(f"{TOGGLE_NAME} start/pause, Esc quit. Starts PAUSED.\n")

    decider = Decider(CFG)
    tel = Telemetry(LOG_PATH)
    listener = make_listener(decider)
    listener.start()

    gc.disable()
    with _MSS() as sct:
        for _ in range(5):                      # warm up capture
            terrain_obs(sct)
        alerted = False
        last_print = 0.0
        try:
            while State.alive:
                if not State.running:
                    release_all()
                    time.sleep(0.02)
                    continue
                now = time.perf_counter()
                terr = terrain_obs(sct)
                yellow, full = capacity_obs(sct)
                intent = decider.decide(terr["position"], terr["confidence"],
                                        full, now)

                if now - last_print > 0.08:     # live readout
                    last_print = now
                    print(f"\r{intent:8} pos={terr['position']:7} "
                          f"c={terr['confidence']:.2f} cov={terr['coverage']:.2f} "
                          f"liq={terr.get('liquid_ratio', 0):.2f} "
                          f"cap={'FULL' if full else 'open'}({yellow:.2f})    ",
                          end="", flush=True)
                tel.log(now, intent, terr, full, yellow)

                if intent == DIG:
                    dig_click()
                    sleep_ms(CAP_CHECK_MS)
                    if not capacity_obs(sct)[1]:        # not full -> pace digs
                        sleep_ms(BETWEEN_DIGS_MS)
                elif intent == EMPTY:
                    if edge_empty(sct):
                        decider.note_emptied()
                elif intent == RECOVER:
                    recover_to_dirt(sct)
                elif intent == SAFE:
                    release_all()
                    if not alerted:
                        alert(decider.reason)
                        alerted = True
                        State.running = False
                    time.sleep(0.02)
                if intent != SAFE:
                    alerted = False
        finally:
            release_all()
            tel.close()
            gc.enable()
            print("\nStopped, inputs released.")


# ---- monitor (no input; live perception readout) ---------------------------
def monitor():
    print("MONITOR -- live perception, NO input sent. Ctrl+C to quit.\n")
    with _MSS() as sct:
        try:
            while True:
                terr = terrain_obs(sct)
                yellow, full = capacity_obs(sct)
                edge = edge_position(sct)
                empty = pan_empty(sct)
                print(f"\rpos={terr['position']:7} conf={terr['confidence']:.2f} "
                      f"cov={terr['coverage']:.2f} liq%={terr.get('liquid_ratio',0):.2f} "
                      f"| edge={edge:7} | cap={'FULL' if full else 'open'}({yellow:.2f}) "
                      f"empty={empty}    ", end="", flush=True)
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\nDone.")


# ---- calibration helpers (ported) ------------------------------------------
def _get_scale(sct):
    b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    return sct.monitors[1]["width"] / b.size.width if b.size.width else 1.0


def calibrate():
    print("CALIBRATE -- hover a target; read PIXEL/RGB. Ctrl+C to quit.")
    print("Use this to place TERRAIN_REGION, CAP_REGION, TEXT_REGION.\n")
    with _MSS() as sct:
        scale = _get_scale(sct)
        full = sct.monitors[1]
        try:
            while True:
                p = _cursor()
                px, py = int(p.x * scale), int(p.y * scale)
                box = {"left": max(full["left"], px - 3),
                       "top": max(full["top"], py - 3), "width": 6, "height": 6}
                b, g, r = np.asarray(sct.grab(box))[:, :, :3].reshape(-1, 3).mean(0)
                print(f"\rPIXEL=({px:>5},{py:>5}) RGB=({int(r):>3},{int(g):>3},"
                      f"{int(b):>3}) scale {scale:.2f}    ", end="", flush=True)
                time.sleep(0.05)
        except KeyboardInterrupt:
            print("\nDone.")


def log_calibration():
    """Labelled CSV capture: tag with Ctrl+1 DIRT, 2 LAVA, 3 SHAKE, 4 DIG; Esc stops."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "prospecting_calib_log.csv")
    label = {"v": "?"}
    LABELS = {18: "DIRT", 19: "LAVA", 20: "SHAKE", 21: "DIG"}
    ctrl = {"down": False}
    stop = {"v": False}
    CTRL = {keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r}

    def on_press(key):
        if key in CTRL:
            ctrl["down"] = True; return
        if key == keyboard.Key.esc:
            stop["v"] = True; return False
        vk = getattr(key, "vk", None)
        if ctrl["down"] and vk in LABELS:
            label["v"] = LABELS[vk]; print(f"\n[label = {label['v']}]")

    def on_release(key):
        if key in CTRL:
            ctrl["down"] = False

    keyboard.Listener(on_press=on_press, on_release=on_release).start()
    print("LOGGING to:", path, "\nCtrl+1 DIRT, 2 LAVA, 3 SHAKE, 4 DIG; Esc stop.\n")
    with _MSS() as sct, open(path, "w") as f:
        f.write("time,label,position,confidence,coverage,yellow\n")
        t0 = time.time()
        while not stop["v"]:
            terr = terrain_obs(sct)
            yellow, _ = capacity_obs(sct)
            ts = time.time() - t0
            f.write(f"{ts:.2f},{label['v']},{terr['position']},"
                    f"{terr['confidence']:.2f},{terr['coverage']:.2f},"
                    f"{yellow:.2f}\n")
            f.flush()
            print(f"\r{ts:6.1f}s {label['v']:6} pos={terr['position']:7} "
                  f"cov={terr['coverage']:.2f} cap={yellow:.2f}   ",
                  end="", flush=True)
            time.sleep(0.1)
    print("\nSaved:", path)


GRID_HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Prospecting grid calibrator</title>
<style>
 body{margin:0;background:#111;color:#eee;font:14px system-ui;display:flex}
 #left{flex:1;overflow:auto;padding:10px}
 #side{width:340px;background:#1b1b1b;padding:14px;box-sizing:border-box}
 .wrap{position:relative;display:inline-block}
 #img{display:block;max-width:none}
 #cv{position:absolute;left:0;top:0}
 button{display:block;width:100%;margin:4px 0;padding:8px;border:0;border-radius:6px;
   background:#2d2d2d;color:#eee;cursor:pointer;text-align:left}
 button.on{background:#3b6ea5}
 h3{margin:14px 0 4px}
 pre{white-space:pre-wrap;background:#000;padding:10px;border-radius:6px;font-size:12px}
 label{font-size:12px;color:#aaa}
 .sw{display:inline-block;width:12px;height:12px;border-radius:2px;margin-right:6px;vertical-align:middle}
</style></head><body>
<div id="left"><div class="wrap"><img id="img" src="__IMG__"><canvas id="cv"></canvas></div></div>
<div id="side">
 <b>Tool</b>
 <button data-t="terrain" class="on"><span class="sw" style="background:#39d353"></span>Terrain box (drag)</button>
 <button data-t="cap"><span class="sw" style="background:#f1c40f"></span>Capacity box (drag)</button>
 <button data-t="text"><span class="sw" style="background:#e74c3c"></span>Text box (drag)</button>
 <button data-t="anchor"><span class="sw" style="background:#ffffff"></span>Player anchor (click)</button>
 <button data-t="line"><span class="sw" style="background:#9b59b6"></span>Dirt/Lava edge (click 2 pts)</button>
 <h3>Grid</h3>
 <label>spacing px <input id="gs" type="number" value="50" style="width:70px"></label>
 <button id="gtoggle">toggle grid</button>
 <button id="clear">clear all</button>
 <h3>Output (capture pixels)</h3>
 <pre id="out"></pre>
 <button id="copy">copy snippet</button>
</div>
<script>
const img=document.getElementById('img'),cv=document.getElementById('cv'),ctx=cv.getContext('2d');
const out=document.getElementById('out');
let tool='terrain',grid=true,boxes={},anchor=null,line=[],drag=null,scale=1;
const COL={terrain:'#39d353',cap:'#f1c40f',text:'#e74c3c',anchor:'#fff',line:'#9b59b6'};
function fit(){scale=img.naturalWidth/img.clientWidth;cv.width=img.clientWidth;cv.height=img.clientHeight;draw();}
img.onload=fit;window.onresize=fit;if(img.complete)fit();
document.querySelectorAll('button[data-t]').forEach(b=>b.onclick=()=>{
  tool=b.dataset.t;document.querySelectorAll('button[data-t]').forEach(x=>x.classList.remove('on'));
  b.classList.add('on');});
document.getElementById('gtoggle').onclick=()=>{grid=!grid;draw();};
document.getElementById('gs').oninput=draw;
document.getElementById('clear').onclick=()=>{boxes={};anchor=null;line=[];draw();};
document.getElementById('copy').onclick=()=>navigator.clipboard.writeText(out.textContent);
function pos(e){const r=cv.getBoundingClientRect();return{x:(e.clientX-r.left),y:(e.clientY-r.top)};}
cv.onmousedown=e=>{const p=pos(e);
  if(tool==='anchor'){anchor=p;draw();emit();return;}
  if(tool==='line'){if(line.length>=2)line=[];line.push(p);draw();emit();return;}
  drag={x0:p.x,y0:p.y,x1:p.x,y1:p.y};};
cv.onmousemove=e=>{if(!drag)return;const p=pos(e);drag.x1=p.x;drag.y1=p.y;draw();};
cv.onmouseup=e=>{if(!drag)return;const b={left:Math.min(drag.x0,drag.x1),top:Math.min(drag.y0,drag.y1),
  w:Math.abs(drag.x1-drag.x0),h:Math.abs(drag.y1-drag.y0)};
  if(b.w>3&&b.h>3)boxes[tool]=b;drag=null;draw();emit();};
function draw(){ctx.clearRect(0,0,cv.width,cv.height);
  if(grid){const g=(+document.getElementById('gs').value)/scale;ctx.strokeStyle='rgba(255,255,255,.18)';ctx.lineWidth=1;
    for(let x=0;x<cv.width;x+=g){ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,cv.height);ctx.stroke();}
    for(let y=0;y<cv.height;y+=g){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(cv.width,y);ctx.stroke();}}
  for(const k in boxes){const b=boxes[k];ctx.strokeStyle=COL[k];ctx.lineWidth=2;ctx.strokeRect(b.left,b.top,b.w,b.h);
    ctx.fillStyle=COL[k];ctx.fillText(k,b.left+2,b.top-3);}
  if(drag){ctx.strokeStyle=COL[tool];ctx.lineWidth=2;
    ctx.strokeRect(Math.min(drag.x0,drag.x1),Math.min(drag.y0,drag.y1),Math.abs(drag.x1-drag.x0),Math.abs(drag.y1-drag.y0));}
  if(anchor){ctx.fillStyle='#fff';ctx.beginPath();ctx.arc(anchor.x,anchor.y,5,0,7);ctx.fill();}
  if(line.length){ctx.strokeStyle=COL.line;ctx.lineWidth=3;ctx.beginPath();
    ctx.moveTo(line[0].x,line[0].y);for(const p of line)ctx.lineTo(p.x,p.y);ctx.stroke();
    line.forEach(p=>{ctx.fillStyle=COL.line;ctx.beginPath();ctx.arc(p.x,p.y,4,0,7);ctx.fill();});}}
function S(v){return Math.round(v*scale);}
function emit(){let s='';
  if(boxes.terrain){const b=boxes.terrain;s+=`TERRAIN_REGION = (${S(b.left)}, ${S(b.top)}, ${S(b.w)}, ${S(b.h)})\n`;}
  if(boxes.cap){const b=boxes.cap;s+=`CAP_REGION     = (${S(b.left)}, ${S(b.top)}, ${S(b.w)}, ${S(b.h)})\n`;}
  if(boxes.text){const b=boxes.text;s+=`TEXT_REGION    = (${S(b.left)}, ${S(b.top)}, ${S(b.w)}, ${S(b.h)})\n`;}
  if(anchor)s+=`PLAYER_ANCHOR  = (${S(anchor.x)}, ${S(anchor.y)})\n`;
  if(line.length===2)s+=`BOUNDARY_LINE  = [(${S(line[0].x)}, ${S(line[0].y)}), (${S(line[1].x)}, ${S(line[1].y)})]\n`;
  out.textContent=s||'(mark something)';}
emit();
</script></body></html>"""


def grid_calibrate():
    """Capture the screen and write an interactive HTML grid editor."""
    here = os.path.dirname(os.path.abspath(__file__))
    png = os.path.join(here, "prospecting_screen.png")
    html = os.path.join(here, "prospecting_calibrate.html")
    with _MSS() as sct:
        shot = sct.grab(sct.monitors[1])
    try:
        import mss.tools
        mss.tools.to_png(shot.rgb, shot.size, output=png)
    except Exception:
        from PIL import Image
        Image.frombytes("RGB", shot.size, shot.rgb).save(png)
    with open(html, "w") as f:
        f.write(GRID_HTML.replace("__IMG__", "prospecting_screen.png"))
    print("Grid calibrator ready.")
    print("  screenshot:", png)
    print("  editor    :", html)
    print("\nOpening it in your browser...")
    try:
        subprocess.Popen(["open", html])
    except Exception:
        print("  (open it manually)")
    print("\nIn the page: pick a tool, drag boxes over the terrain/capacity/text,")
    print("click your player anchor, click 2 points along the dirt/lava edge.")
    print("Then copy the snippet and paste it back to me.")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "old":
        # run the older pure-timed version (prospecting_old.py), passing through
        # any extra args e.g. `old calibrate`
        here = os.path.dirname(os.path.abspath(__file__))
        subprocess.run([sys.executable, os.path.join(here, "prospecting_old.py")]
                       + sys.argv[2:])
    elif arg == "other":
        # run the AHK-port version (prospecting_other.py)
        here = os.path.dirname(os.path.abspath(__file__))
        subprocess.run([sys.executable, os.path.join(here, "prospecting_other.py")]
                       + sys.argv[2:])
    elif arg == "new":
        # run the cue-driven version (prospecting_new.py)
        here = os.path.dirname(os.path.abspath(__file__))
        subprocess.run([sys.executable, os.path.join(here, "prospecting_new.py")]
                       + sys.argv[2:])
    elif arg == "friend":
        # run the faithful port of the friend's AHK macro (prospecting_friend.py)
        here = os.path.dirname(os.path.abspath(__file__))
        subprocess.run([sys.executable, os.path.join(here, "prospecting_friend.py")]
                       + sys.argv[2:])
    elif arg == "monitor":
        monitor()
    elif arg == "calibrate":
        calibrate()
    elif arg == "grid":
        grid_calibrate()
    elif arg == "log":
        log_calibration()
    else:
        main()
