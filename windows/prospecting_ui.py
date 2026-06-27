#!/usr/bin/env python3
"""
prospecting_ui.py -- settings panel for the Prospecting macro, served in your
BROWSER (no tkinter needed -- works on any Python 3). Edit timings, pick v1/v2,
set how many digs fill the pan, etc. Writes prospecting_config.json, which
prospecting_old.py loads on startup.

Run:
    python3 prospecting_ui.py          (or:  python3 prospecting_macro.py ui)

It starts a tiny local web server and opens the page automatically. Edit fields,
click Save (the macro reads them next time it starts). "Save & Launch" also opens
Terminal running the macro. Close the terminal (Ctrl+C) when done.
"""

import os
import json
import shlex
import socket
import subprocess
import threading
import webbrowser
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "prospecting_config.json")
MACRO_FILE = os.path.join(HERE, "prospecting_old.py")

# ---- settings schema: (key, label, type, default) ; type in {int, bool} ------
SECTIONS = [
    ("Easy tuning", [
        ("EASY_WATER_BACK_MS",        "Go further back into the water (ms)",       "int", 0),
        ("EASY_LAND_FWD_MS",          "Go further onto land (ms)",                 "int", 0),
        ("EASY_SHAKE_DELAY_MS",       "Wait longer before the shake starts (ms)",  "int", 0),
        ("EASY_FIRST_DIG_DELAY_MS",   "Wait longer before the first dig (ms)",     "int", 0),
        ("EASY_WATER_RETURN_DELAY_MS","Wait before going back to water when full (ms)", "int", 0),
    ]),
    ("Mode / Dig", [
        ("PERFECT",            "Perfect dig (release on green) — off = timed hold", "bool", False),
        ("DIG_CLICK_MS",       "Dig hold length (ms)",                    "int", 75),
        ("DIG_SPEED",          "Dig speed (%) — scales the hold",         "int", 100),
        ("MAX_DIGS_TO_FILL",   "Max digs to fill the pan",                "int", 8),
        ("DIG_FILL_MS",        "Wait for FULL after each dig (ms)",        "int", 250),
        ("PRE_DIG_SETTLE_MS",  "Settle before first dig after shake (ms)", "int", 60),
    ]),
    ("Walk back into water", [
        ("PAN_BACK_MAX_MS",    "Max S walk-back (ms)",                    "int", 200),
        ("WATER_EXTRA_BACK_MS","Extra S after Pan cue / go deeper (ms)",  "int", 0),
    ]),
    ("Shake", [
        ("SHAKE_MOMENTUM_W",   "Hold W during shake (glide onto land)",   "bool", True),
        ("SHAKE_CLICK_MS",     "Each shake click length (ms)",            "int", 18),
        ("SHAKE_CLICK_GAP_MS", "Gap between shake clicks (ms)",           "int", 14),
        ("SHAKE_HOLD_MS",      "Shake overall timeout (ms)",              "int", 1500),
        ("SHAKE_BAIL_MS",      "Shake-failed detection (ms)",             "int", 500),
        ("SHAKE_START_DELAY_MS","Delay before shake starts (ms)",         "int", 0),
        ("POST_SHAKE_SETTLE_MS","Settle after pan empties (ms)",          "int", 150),
    ]),
    ("Return to land (dig-probe)", [
        ("DEPOSIT_MAX_MS",     "Max W to find land cue (ms)",             "int", 1200),
        ("LAND_SETTLE_MS",     "Hold W after land cue (ms)",              "int", 45),
        ("DIG_PROBE_MS",       "Wait to detect a probe-dig hit (ms)",     "int", 320),
        ("PROBE_GAP_MS",       "Settle between probe digs (ms)",          "int", 80),
        ("LAND_PROBE_NUDGE_MS","Forward W nudge between probe digs (ms)", "int", 90),
        ("LAND_DIG_TRIES",     "Probe digs before giving up",             "int", 5),
    ]),
    ("Recovery / safety", [
        ("STUCK_TICKS",        "Stuck reads before recovery",             "int", 3),
        ("RECOVER_LIMIT",      "Recoveries before break-out",             "int", 3),
        ("RECOVER_BACK_MS",    "Recovery nudge budget (ms)",              "int", 160),
        ("SHAKE_FAIL_LIMIT",   "Failed shakes before STOP",               "int", 5),
        ("BREAKOUT_LIMIT",     "Break-outs before STOP",                  "int", 2),
        ("BREAKOUT_SHAKE_MS",  "Break-out click-to-finish (ms)",          "int", 700),
        ("BREAKOUT_REPOS_MS",  "Break-out reposition W (ms)",             "int", 160),
        ("SAFE_STOP_RETRY",     "Safe-stop = pause & retry (don't hard-stop)", "bool", True),
        ("SAFE_STOP_RETRY_SEC", "Wait before each retry (seconds)",        "int", 60),
        ("SAFE_STOP_MAX_RETRIES","Hard-stop after this many failed retries", "int", 3),
    ]),
    ("Recovery movement (jitter taps)", [
        ("BURST_ON_MS",        "Tap hold per pulse (ms)",                 "int", 11),
        ("BURST_OFF_MS",       "Tap release per pulse (ms)",             "int", 1),
    ]),
    ("Notifications", [
        ("WEBHOOK_ENABLED",    "DM me on Discord",                        "bool", False),
        ("WEBHOOK_USER",       "Your Discord username",                   "str", ""),
        ("WEBHOOK_STATS_MIN",  "Stats DM every N min (0 = off)",          "int", 60),
        ("NOTIFY_START",       "Notify: started",                        "bool", True),
        ("NOTIFY_STOP",        "Notify: stopped (manual / timer / bag full)", "bool", True),
        ("NOTIFY_STATS",       "Notify: periodic stats",                 "bool", True),
        ("NOTIFY_SAFE_STOP",   "Notify: safe-stop (hit a hazard)",       "bool", True),
        ("NOTIFY_RECOVERIES",  "Notify: recoveries (can be frequent)",   "bool", False),
        ("NOTIFY_ERRORS",      "Notify: errors",                         "bool", True),
    ]),
    ("Auto-stop", [
        ("AUTOSTOP_ENABLED",   "Auto-stop after a set time",             "bool", False),
        ("AUTOSTOP_MINUTES",   "Stop after this many minutes",           "int", 60),
        ("STOP_AFTER_PANS",    "Stop after N pans (0 = off, bag guard)", "int", 0),
    ]),
    ("Window", [
        ("WINDOW_RELATIVE",    "Shift pixels when the Roblox window moves", "bool", False),
    ]),
    ("Smart / experimental", [
        ("SMART_TIMING",   "Auto-tune timing by trial & error",          "bool", False),
        ("ADAPT_MISS_PCT", "Adjust when miss rate exceeds (%)",          "int", 20),
        ("X_PATTERN",      "X pattern: diagonal walk-backs",             "bool", False),
    ]),
]

PRESET_V1 = {"PERFECT": False, "DIG_CLICK_MS": 15, "MAX_DIGS_TO_FILL": 1}
PRESET_V2 = {"PERFECT": False, "DIG_CLICK_MS": 75, "MAX_DIGS_TO_FILL": 8}
DEFAULTS = {k: d for _, items in SECTIONS for (k, _l, _t, d) in items}
TYPES = {k: t for _, items in SECTIONS for (k, _l, t, _d) in items}


def load_saved():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return {}


SECTION_HINT = {
    "Easy tuning": "Plain-language tweaks. Type how much MORE you want of each move "
                   "and the macro adjusts the underlying timings for you.",
    "Mode / Dig": "How each dig works and how many it takes to fill the pan.",
    "Walk back into water": "Getting from land into the water to shake.",
    "Shake": "Emptying the pan; momentum carries you back to land.",
    "Return to land (dig-probe)": "Finding land after a shake by test-digging.",
    "Recovery / safety": "What happens when something goes wrong.",
    "Recovery movement (jitter taps)": "Tiny tap timing used only during recovery.",
    "Notifications": "Get DMs from the Prospectors bot on start, stop and stats. "
                     "Just enter your Discord username.",
    "Auto-stop": "Automatically stop the macro after a set time.",
    "Window": "Make calibration survive the Roblox window being moved.",
    "Smart / experimental": "Experimental auto-tuning and movement patterns. "
                            "Off by default — turn on and test.",
}


TAB_ICON = {
    "Easy tuning": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h9M17 7h3M4 12h3M11 12h9M4 17h11M19 17h1"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="17" r="2"/></svg>',
    "Mode / Dig": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.5c3 3.8 6 6.8 6 9.8a6 6 0 0 1 -12 0c0 -3 3 -6 6 -9.8z"/></svg>',
    "Walk back into water": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v13M6 13l6 6l6 -6"/></svg>',
    "Shake": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8c2 -2 4 -2 6 0s4 2 6 0 4 -2 6 0M3 14c2 -2 4 -2 6 0s4 2 6 0 4 -2 6 0"/></svg>',
    "Return to land (dig-probe)": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V6M6 12l6 -6l6 6"/></svg>',
    "Recovery / safety": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3v5c0 4.5 -3 7.5 -7 8.5c-4 -1 -7 -4 -7 -8.5V6z"/></svg>',
    "Recovery movement (jitter taps)": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="5" cy="12" r="1.5" fill="currentColor"/><circle cx="12" cy="12" r="1.5" fill="currentColor"/><circle cx="19" cy="12" r="1.5" fill="currentColor"/></svg>',
    "Notifications": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9a6 6 0 0 1 12 0c0 4 1.5 5.5 2 6H4c.5 -.5 2 -2 2 -6z"/><path d="M10 20a2 2 0 0 0 4 0"/></svg>',
    "Auto-stop": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="13" r="7.5"/><path d="M12 13V9.5M9.5 2.5h5M18.6 6l1.2 -1.2"/></svg>',
    "Window": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="5" width="16" height="14" rx="2"/><path d="M4 9.5h16"/></svg>',
    "Smart / experimental": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3h6M10 3v6l-5 8a2 2 0 0 0 1.8 3h10.4a2 2 0 0 0 1.8 -3l-5 -8V3"/><path d="M7.5 14h9"/></svg>',
}

# Per-setting explanations (shown as a ? tooltip next to each field).
HELP = {
    "EASY_WATER_BACK_MS": "Walk back into the water this many ms further before shaking. "
                          "Raise if you stop short of the water; lower if you overshoot.",
    "EASY_LAND_FWD_MS": "Walk forward onto land this many ms further after a shake. "
                        "Raise if you keep landing short and needing nudges.",
    "EASY_SHAKE_DELAY_MS": "Wait this many ms longer before the shake begins, in case the "
                           "shake animation needs a moment to start.",
    "EASY_FIRST_DIG_DELAY_MS": "Wait this many ms longer before the first dig after a shake, "
                               "so you're settled on land before digging.",
    "EASY_WATER_RETURN_DELAY_MS": "Pause this many ms after the pan fills before heading "
                                  "back to the water.",
    "PERFECT": "If on, the dig releases when the skill bar hits the green zone. "
               "The bar is usually too fast to catch by pixel, so leave OFF and "
               "use a timed hold instead.",
    "DIG_CLICK_MS": "How long each dig holds the mouse. Auto-filled from Dig "
                    "speed below, or set it manually.",
    "DIG_SPEED": "Your dig-speed stat as a percent. Auto-fills the Dig hold above "
                 "(100% = 550ms, 200% = 275ms; hold = 55000 / speed).",
    "MAX_DIGS_TO_FILL": "Safety cap on how many digs it will do to fill the pan. "
                        "It watches the bar, so set this above your build's need.",
    "DIG_FILL_MS": "After a dig, how long to wait for the bar to read FULL before "
                   "digging again.",
    "PRE_DIG_SETTLE_MS": "Tiny pause after landing before the first dig, so it "
                         "doesn't fire mid-glide and miss.",
    "PAN_BACK_MAX_MS": "Safety cap on the backward (S) walk into the water.",
    "WATER_EXTRA_BACK_MS": "Keep holding S a bit after the Pan cue to go deeper, "
                           "so the shake doesn't start right at the edge.",
    "SHAKE_MOMENTUM_W": "Hold W while shaking so momentum carries you back onto "
                        "land as the pan drains.",
    "SHAKE_CLICK_MS": "Length of each shake click (the shake is a rapid click "
                      "stream, since a held press is dropped on macOS).",
    "SHAKE_CLICK_GAP_MS": "Gap between shake clicks. Lower = faster rattle.",
    "SHAKE_HOLD_MS": "Overall shake time limit; it stops early when the pan empties.",
    "SHAKE_BAIL_MS": "If the pan is STILL completely full after this long, the "
                     "shake didn't start — give up and retry. Keep above a real "
                     "shake's drain time.",
    "SHAKE_START_DELAY_MS": "Pause between reaching the water and starting the "
                            "shake (usually 0).",
    "POST_SHAKE_SETTLE_MS": "Pause after the pan empties so momentum settles you "
                            "onto land before the dig-probe.",
    "DEPOSIT_MAX_MS": "Safety cap on the forward (W) walk while looking for land.",
    "LAND_SETTLE_MS": "Hold W a touch longer after the land cue to sit firmly on "
                      "the dirt (prevents a land↔water flicker).",
    "DIG_PROBE_MS": "How long to wait for a probe-dig to register before calling "
                    "it a miss. Too short = needless extra nudges.",
    "PROBE_GAP_MS": "Settle after a forward nudge before the next probe dig.",
    "LAND_PROBE_NUDGE_MS": "How far forward (ms of W) to nudge between probe digs "
                           "when searching for land.",
    "LAND_DIG_TRIES": "How many nudge-forward rounds to try before giving up on "
                      "finding land (then SAFE STOP).",
    "STUCK_TICKS": "How many identical reads in a row before it triggers recovery.",
    "RECOVER_LIMIT": "How many recoveries on the same spot before the smart "
                     "break-out kicks in.",
    "RECOVER_BACK_MS": "Budget for a recovery nudge (pulsed taps).",
    "SHAKE_FAIL_LIMIT": "Shakes that don't empty (even while moving) before it "
                        "SAFE STOPs.",
    "BREAKOUT_LIMIT": "Break-out attempts before SAFE STOP.",
    "BREAKOUT_SHAKE_MS": "When stuck, click this long to finish a shake that's "
                         "locking your movement.",
    "BREAKOUT_REPOS_MS": "Forward reposition nudge during a break-out.",
    "BURST_ON_MS": "Recovery taps: how long each tap holds the key.",
    "BURST_OFF_MS": "Recovery taps: how long each tap releases before re-checking.",
    "WEBHOOK_ENABLED": "Get a Discord DM from the Prospectors bot on start, stop, "
                       "safe-stop, auto-stop, bag-full, and periodic stats.",
    "WEBHOOK_USER": "Your exact Discord username (the bot DMs this server member). "
                    "You must be in the server and have DMs open.",
    "WEBHOOK_STATS_MIN": "How often to DM a stats update while running (0 = off).",
    "NOTIFY_START": "DM you when a session starts (you press Ctrl+K to run).",
    "NOTIFY_STOP": "DM you when the macro stops — whether you stop it, the auto-stop "
                   "timer fires, or it stops because the bag is full.",
    "NOTIFY_STATS": "DM you a periodic stats summary (cycles, pans/hr, recoveries) "
                    "while it runs. Set the interval above.",
    "NOTIFY_SAFE_STOP": "DM you if the macro safe-stops because it detected a hazard "
                        "(e.g. it would walk into lava/water) and parked itself.",
    "NOTIFY_RECOVERIES": "DM you each time it recovers from being stuck. Off by default "
                         "because recoveries can happen often and get spammy.",
    "NOTIFY_ERRORS": "DM you if the macro hits an unexpected error and stops.",
    "SAFE_STOP_RETRY": "When the macro detects a hazard or gets stuck, pause and retry shortly instead of stopping — so an AFK run heals itself. Off = stop immediately.",
    "SAFE_STOP_RETRY_SEC": "How long to wait before each safe-stop retry.",
    "SAFE_STOP_MAX_RETRIES": "After this many failed retries in a row, hard-stop for real.",
    "AUTOSTOP_ENABLED": "Automatically stop the macro after AUTOSTOP minutes.",
    "AUTOSTOP_MINUTES": "How long to run before auto-stopping.",
    "STOP_AFTER_PANS": "Stop after this many pans emptied — a simple guard so it "
                       "doesn't keep panning into a full inventory. 0 = off.",
    "WINDOW_RELATIVE": "Calibrate, then if you move the Roblox window the macro "
                       "shifts its pixels to match. Re-calibrate to set the "
                       "reference. Default off = absolute screen coordinates.",
    "SMART_TIMING": "Trial-and-error auto-tuning. If shakes miss the water or "
                    "landing needs nudges too often, it nudges the timing and keeps "
                    "the change only if the miss rate drops. Experimental — watch the log.",
    "ADAPT_MISS_PCT": "Smart timing only kicks in once the miss rate is above this "
                      "percentage over a short window.",
    "X_PATTERN": "Walk back into the water on alternating 45° diagonals instead of "
                 "straight back, so each pass covers new ground. Helps when you keep "
                 "falling short on a straight line. Forward to land stays straight.",
}

# Calibratable on-screen pixels: (key, label, description, default [x, y]).
# Defaults are the original values -- shown so you know what to calibrate; only
# the Calibrate button changes them. CAP_LEFT_PIXEL is used to compute the bar
# width (CAP_BAR_WIDTH); the rest map straight to macro pixel settings.
PIXEL_FIELDS = [
    ("CAP_FULL_PIXEL", "Capacity bar — RIGHT end",
     "The right tip of the Pan Fill bar. Gray when empty, YELLOW when the pan is full.",
     [1120, 900]),
    ("CAP_LEFT_PIXEL", "Capacity bar — LEFT end",
     "The left tip of the Pan Fill bar. Used with the right end to measure the bar width.",
     [680, 900]),
    ("DEPOSIT_PIX", "'Collect Deposit' text",
     "A pixel on the white 'Collect Deposit' prompt (shown when you're on land).",
     [770, 981]),
    ("PAN_PIX", "'Pan' text",
     "A pixel on the white 'Pan' prompt (shown when you're in the water).",
     [847, 981]),
    ("SHAKE_PIX", "'Shake' text",
     "A pixel on the white 'Shake' prompt (shown while shaking).",
     [830, 981]),
    ("DIG_TRIGGER_PIXEL", "Green dig pixel (Perfect mode only)",
     "The GREEN target on the dig skill bar. Only needed if you turn Perfect dig on.",
     [1078, 532]),
]
PIXEL_DEFAULTS = {k: list(d) for (k, _l, _desc, d) in PIXEL_FIELDS}


def render(msg=""):
    saved = load_saved()
    navs, panels = [], []
    for idx, (title, items) in enumerate(SECTIONS):
        active = " active" if idx == 0 else ""
        icon = TAB_ICON.get(title, "•")
        navs.append(f'<button type="button" class="tab{active}" data-tab="{idx}">'
                    f'<span class="ti">{icon}</span><span>{title}</span></button>')
        rows = []
        for key, label, typ, default in items:
            val = saved.get(key, default)
            if typ == "bool":
                checked = "checked" if val else ""
                control = (f'<span class="switch"><input type="checkbox" name="{key}" '
                           f'data-type="bool" {checked}>'
                           f'<span class="track"><span class="knob"></span></span></span>')
            elif typ == "str":
                sval = str(val).replace(chr(34), "&quot;")
                control = (f'<input type="text" name="{key}" data-type="str" '
                           f'value="{sval}" style="width:240px;text-align:left">')
            else:
                control = (f'<input type="number" name="{key}" data-type="int" '
                           f'value="{val}">')
            qm = (f'<span class="qm" data-tip="{HELP[key].replace(chr(34), "&quot;")}">?</span>'
                  if HELP.get(key) else "")
            rows.append(f'<label class="row"><span class="lbl">{label}{qm}</span>'
                        f'{control}</label>')
        hint = SECTION_HINT.get(title, "")
        panels.append(
            f'<section class="panel{active}" id="p{idx}">'
            f'<div class="phead"><h2>{title}</h2><p class="chint">{hint}</p></div>'
            f'<div class="rows">{"".join(rows)}</div></section>')
    # Pixels tab (manual x/y entry; the native app has click-to-calibrate)
    navs.append('<button type="button" class="tab" data-tab="pix">'
                '<span class="ti">🎯</span><span>Pixels</span></button>')
    prows = []
    for key, label, desc, default in PIXEL_FIELDS:
        xy = saved.get(key, default)
        prows.append(
            f'<label class="row"><span class="lbl">{label}'
            f'<span class="qm" data-tip="{desc.replace(chr(34), "&quot;")}">?</span></span>'
            f'<input type="number" name="PIX_{key}_x" data-type="pix" value="{xy[0]}" '
            f'style="width:78px"> <input type="number" name="PIX_{key}_y" data-type="pix" '
            f'value="{xy[1]}" style="width:78px"></label>')
    panels.append(
        '<section class="panel" id="ppix"><div class="phead"><h2>Pixels</h2>'
        '<p class="chint">On-screen coordinates the macro reads (x, y). The desktop '
        'app lets you click-to-calibrate these; here you can type them.</p></div>'
        f'<div class="rows">{"".join(prows)}</div></section>')
    banner = f'<div class="ok">{msg}</div>' if msg else ""
    return (PAGE.replace("{{NAV}}", "".join(navs))
                .replace("{{PANELS}}", "".join(panels))
                .replace("{{MSG}}", banner)
                .replace("{{DEFAULTS}}", json.dumps(DEFAULTS))
                .replace("{{V1}}", json.dumps(PRESET_V1))
                .replace("{{V2}}", json.dumps(PRESET_V2)))


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Prospecting Macro — Settings</title>
<style>
 :root{--bg:#0f1115;--panel:#171a21;--head:#1c2029;--line:#262b35;--txt:#e8eaed;
   --mut:#8b94a3;--accent:#3b82f6;--accent2:#10b981;--field:#0c0e12;--nav:#13161c}
 *{box-sizing:border-box}
 html,body{height:100%}
 body{background:var(--bg);color:var(--txt);font:14px/1.45 -apple-system,BlinkMacSystemFont,
   "Segoe UI",Helvetica,Arial,sans-serif;margin:0;display:flex;flex-direction:column}
 form{display:flex;flex-direction:column;height:100vh}
 .topbar{flex:0 0 auto;background:rgba(15,17,21,.96);border-bottom:1px solid var(--line);
   padding:13px 20px;display:flex;align-items:center;gap:12px}
 .brand{font-size:16px;font-weight:700;letter-spacing:.2px} .brand b{color:var(--accent2)}
 .grow{flex:1}
 button{font:inherit;font-weight:600;border:0;border-radius:9px;padding:9px 15px;
   cursor:pointer;transition:transform .04s,filter .15s,background .15s}
 button:active{transform:translateY(1px)}
 .btn{background:var(--accent);color:#fff} .btn:hover{filter:brightness(1.08)}
 .btn2{background:#2a3340;color:#dfe5ee} .btn2:hover{background:#33404f}
 .quit{color:#9aa3b1;text-decoration:none;font-weight:600;font-size:13px;
   padding:9px 12px;border-radius:9px} .quit:hover{color:#ff8585;background:#2a1c1f}
 .body{flex:1 1 auto;display:flex;min-height:0}
 /* left nav */
 .side{flex:0 0 200px;background:var(--nav);border-right:1px solid var(--line);
   padding:12px 10px;overflow-y:auto}
 .tab{display:flex;align-items:center;gap:10px;width:100%;text-align:left;
   background:transparent;color:#b9c1cd;font-weight:600;border-radius:9px;
   padding:10px 11px;margin-bottom:3px}
 .tab .ti{width:18px;text-align:center;opacity:.85}
 .tab:hover{background:#1d222b;color:#e8eaed}
 .tab.active{background:#223049;color:#fff}
 .navsep{height:1px;background:var(--line);margin:10px 4px}
 .pretitle{color:var(--mut);font-size:11.5px;text-transform:uppercase;
   letter-spacing:.6px;margin:4px 8px 6px}
 .chip{display:block;width:100%;text-align:left;background:#1b212b;color:#cdd5e0;
   border:1px solid var(--line);border-radius:8px;padding:8px 11px;margin-bottom:6px;
   font-size:13px;font-weight:600} .chip:hover{background:#222a35;border-color:#3a4757}
 /* content */
 .content{flex:1 1 auto;overflow-y:auto;padding:20px 26px}
 .lead{color:var(--mut);margin:0 0 14px}
 .panel{display:none} .panel.active{display:block;animation:fade .12s ease}
 @keyframes fade{from{opacity:0;transform:translateY(3px)}to{opacity:1}}
 .phead{margin:0 0 12px}
 .phead h2{margin:0;font-size:18px} .chint{margin:3px 0 0;color:var(--mut)}
 .rows{background:var(--panel);border:1px solid var(--line);border-radius:14px;
   padding:4px 16px;max-width:560px}
 .row{display:flex;align-items:center;gap:14px;padding:12px 0;border-bottom:1px solid #20242d}
 .rows .row:last-child{border-bottom:0}
 .lbl{flex:1;color:#d7dce4}
 .qm{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;
   margin-left:7px;border-radius:50%;background:#2c3340;color:#9fb0c4;font-size:11px;
   font-weight:700;cursor:help} .qm:hover{background:var(--accent);color:#fff}
 .tip{position:fixed;display:none;max-width:300px;background:#0b0d12;color:#dfe5ee;
   border:1px solid var(--line);border-radius:9px;padding:9px 11px;font-size:12.5px;
   line-height:1.4;z-index:200;box-shadow:0 8px 24px rgba(0,0,0,.5)}
 input[type=number]{width:104px;background:var(--field);color:#fff;border:1px solid #2c333f;
   border-radius:8px;padding:9px 11px;text-align:right;font-variant-numeric:tabular-nums}
 input[type=number]:focus{outline:0;border-color:var(--accent);
   box-shadow:0 0 0 3px rgba(59,130,246,.25)}
 .switch{position:relative;display:inline-flex} .switch input{display:none}
 .track{width:46px;height:26px;background:#39414e;border-radius:999px;position:relative;
   transition:background .15s;cursor:pointer}
 .knob{position:absolute;top:3px;left:3px;width:20px;height:20px;background:#fff;
   border-radius:50%;transition:left .15s}
 .switch input:checked + .track{background:var(--accent2)}
 .switch input:checked + .track .knob{left:23px}
 .ok{background:#10301f;color:#7fe6b5;border:1px solid #1f6b4a;border-radius:10px;
   padding:10px 13px;margin:0 0 16px;font-size:13px;max-width:560px}
</style></head><body>
<form method="POST" action="/save" id="f">
 <div class="topbar">
   <div class="brand"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" style="vertical-align:-2px"><path d="M6.5 4h11l4 5.2L12 21 2.5 9.2z" fill="#fff"/><path d="M2.5 9.2h19M6.5 4l2.6 5.2L12 21M17.5 4l-2.6 5.2L12 21M9.1 9.2h5.8" stroke="#0a0908" stroke-opacity=".32" stroke-width=".8" stroke-linejoin="round"/></svg> Prospectors <b>Plus</b></div>
   <div class="grow"></div>
   <a class="quit" href="/quit" title="Close the settings app">Quit</a>
   <button class="btn2" type="submit" formaction="/launch">Save &amp; Launch</button>
   <button class="btn" type="submit" formaction="/save">Save</button>
 </div>
 <div class="body">
   <nav class="side">
     {{NAV}}
     <div class="navsep"></div>
     <div class="pretitle">Presets</div>
     <button type="button" class="chip" onclick="preset(V1)">v1 · fast 1-dig</button>
     <button type="button" class="chip" onclick="preset(V2)">v2 · multi-dig</button>
     <button type="button" class="chip" onclick="preset(DEF)">Reset defaults</button>
   </nav>
   <div class="content">
     {{MSG}}
     <p class="lead">Pick a category on the left, edit values, then <b>Save</b>.
       The macro loads these each time it starts (Ctrl+K).</p>
     {{PANELS}}
   </div>
 </div>
</form>
<script>
 const DEF={{DEFAULTS}},V1={{V1}},V2={{V2}};
 document.querySelectorAll('.tab').forEach(b=>b.addEventListener('click',()=>{
   document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
   document.querySelectorAll('.panel').forEach(x=>x.classList.remove('active'));
   b.classList.add('active');
   document.getElementById('p'+b.dataset.tab).classList.add('active');
 }));
 function preset(p){let touched={};
   for(const k in p){const el=document.querySelector('[name="'+k+'"]');
     if(!el)continue; if(el.dataset.type==='bool')el.checked=!!p[k]; else el.value=p[k];}
 }
 // custom tooltip (native title tooltips are unreliable in app windows)
 const _tip=document.createElement('div');_tip.className='tip';document.body.appendChild(_tip);
 document.addEventListener('mouseover',e=>{const q=e.target.closest('.qm');if(!q)return;
   _tip.textContent=q.dataset.tip||'';_tip.style.display='block';
   const r=q.getBoundingClientRect();
   _tip.style.left=Math.max(8,Math.min(r.left,window.innerWidth-308))+'px';
   _tip.style.top=(r.bottom+6)+'px';});
 document.addEventListener('mouseout',e=>{if(e.target.closest('.qm'))_tip.style.display='none';});
 // dig speed -> auto-fill dig hold (100% = 550ms, hold = 55000/speed)
 (function(){const ds=document.querySelector('[name="DIG_SPEED"]'),
   dh=document.querySelector('[name="DIG_CLICK_MS"]');
   if(ds&&dh)ds.addEventListener('input',()=>{const s=parseFloat(ds.value);
     if(s>0)dh.value=Math.round(55000/s);});})();
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, html, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, *a):
        pass  # quiet

    def do_GET(self):
        if self.path == "/quit":
            self._send("<body style='background:#0f1115;color:#cfd5de;"
                       "font:16px -apple-system,sans-serif;padding:48px'>"
                       "<h2>Settings closed.</h2><p>You can close this window.</p>"
                       "</body>")
            threading.Timer(0.3, lambda: os._exit(0)).start()
            return
        self._send(render())

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode("utf-8")
        form = urllib.parse.parse_qs(raw, keep_blank_values=True)
        cur = load_saved()                       # MERGE (preserve relics/pixels)
        for key, typ in TYPES.items():
            if typ == "bool":
                cur[key] = key in form           # checkbox only sent when checked
            elif typ == "str":
                cur[key] = form.get(key, [cur.get(key, DEFAULTS[key])])[0]
            else:
                try:
                    cur[key] = int(form.get(key, [cur.get(key, DEFAULTS[key])])[0])
                except (ValueError, IndexError):
                    cur[key] = DEFAULTS[key]
        # pixel coords (PIX_<KEY>_x / _y)
        for pkey, _l, _d, default in PIXEL_FIELDS:
            try:
                px = int(form.get(f"PIX_{pkey}_x", [default[0]])[0])
                py = int(form.get(f"PIX_{pkey}_y", [default[1]])[0])
                cur[pkey] = [px, py]
            except (ValueError, IndexError):
                pass
        if "CAP_FULL_PIXEL" in cur and "CAP_LEFT_PIXEL" in cur:
            w = int(cur["CAP_FULL_PIXEL"][0] - cur["CAP_LEFT_PIXEL"][0])
            if w > 20:
                cur["CAP_BAR_WIDTH"] = w
        with open(CONFIG_FILE, "w") as f:
            json.dump(cur, f, indent=2)
        data = cur
        msg = f"Saved {len(data)} settings ✓"
        if self.path == "/launch":
            try:
                launch_macro()
                msg += " — launched in Terminal (press Ctrl+K to start)"
            except Exception as e:
                msg += f" — couldn't auto-launch ({e}); run python3 prospecting_old.py"
        self._send(render(msg))


def launch_macro():
    """Open the macro in a console window (Windows): write a .bat and start it."""
    launcher = os.path.join(HERE, "_run_macro.bat")
    with open(launcher, "w") as f:
        f.write("@echo off\r\n"
                f'cd /d "{HERE}"\r\n'
                f'python "{MACRO_FILE}"\r\n'
                "pause\r\n")
    os.startfile(launcher)            # opens in a new console window


def open_window(url):
    """Open the settings page in the default browser (the native app
    prospecting_app.py is the primary UI; this is the fallback)."""
    webbrowser.open(url)
    return "default browser"


def free_port(start=8765):
    for p in range(start, start + 20):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p)); return p
            except OSError:
                continue
    return start


if __name__ == "__main__":
    port = free_port()
    url = f"http://127.0.0.1:{port}/"
    srv = HTTPServer(("127.0.0.1", port), Handler)
    threading.Timer(0.5, lambda: print(f"Opened as {open_window(url)}.")).start()
    print(f"Prospecting settings UI -> {url}\nClose this window / Ctrl+C when done.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nUI closed.")
