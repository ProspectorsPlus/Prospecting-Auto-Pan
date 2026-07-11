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
    ("Tracker", [
        ("TRACKER_MODE",   "Tracker: watch-only, count the game's auto-pan", "bool", False),
        ("TRACKER_POLL_MS","Tracker sampling rhythm (ms)",                "int", 30),
        ("TRACKER_RELICS", "Use relic timers during Tracker (toggles Auto Pan)", "bool", False),
        ("AUTOPAN_TOL",    "Auto Pan button colour tolerance",            "int", 40),
        ("AUTOPAN_SETTLE_MS","Wait after clicking Auto Pan (ms)",         "int", 400),
        ("AUTOPAN_GUARD",  "Guard: re-enable Auto Pan if it turns off",   "bool", False),
        ("AUTOPAN_GUARD_SEC","Guard check interval (seconds)",            "int", 5),
        ("AUTOPAN_SHIFTLOCK","Shift-lock dance around Auto Pan clicks",    "bool", False),
        ("AUTOPAN_FAST_RELOCK","Re-lock shift immediately after the click",  "bool", False),
        ("AUTOPAN_STALL_SEC","Restart Auto Pan if idle this long (s, 0=off)","int", 0),
        ("AUTOPAN_RELOCK_DELAY_MS","Click -> shift re-lock gap (ms)",       "int", 0),
    ]),
    ("Relic behaviour", [
        ("RELIC_ON_LAND",   "Place relics only when ON LAND",             "bool", False),
        ("RELIC_LAND_MAX_S","Max wait for land before placing anyway (s)","int", 45),
        ("RELIC_RELATIVE",  "Relative timers (keep counting while paused)","bool", True),
    ]),
    ("Earnings", [
        ("EARN_TRACK",   "Track money & shards (OCR the HUD totals)",   "bool", False),
        ("EARN_OCR_SEC", "Read the totals every (seconds)",             "int", 10),
        ("FINDS_TRACK",  "Track finds (item pop-up OCR)",               "bool", False),
        ("FINDS_FAST_MS","Fast opacity sample every (ms)",              "int", 40),
        ("FINDS_OCR_MS", "Identity OCR at most every (ms)",             "int", 200),
        ("FINDS_STACK_NEWEST", "New find appears at (bottom / top)",       "str", "bottom"),
        ("FINDS_MIN_CONF", "Min OCR confidence for a new find (0-1)",      "float", 0.30),
        ("FINDS_MIN_DWELL","Samples a card must live to count",         "int", 3),
        ("FINDS_EMPTY_MS","Quiet ms before the stack resets",           "int", 700),
        ("FINDS_CARD_SEC","Card on-screen lifetime (s)",                 "int", 5),
        ("FINDS_WHITE_MIN","Card text brightness floor (0-255)",        "int", 170),
        ("FINDS_DARK_MAX","Card backing darkness ceiling (0-255)",      "int", 105),
        ("FINDS_BAND_MIN","Min text row fraction for a card (0-1)",     "float", 0.008),
        ("FINDS_DEBUG",  "Verbose finds tracker diagnostics",           "bool", False),
        ("SELL_BOOST_PCT","Sell boost for loot value (%)",              "int", 100),
        ("FINDS_BANK_RARITY","Value finds at/above rarity (kept loot)",   "str", "Exotic"),
    ]),
    ("Treasure chest", [
        ("TREASURE_MODE",        "Treasure Chest mode (no shake, strafe L/R)", "bool", False),
        ("TREASURE_DIGS",        "Digs per chest before strafing",             "int", 1),
        ("TREASURE_DIG_MS",      "Quick dig click (ms)",                       "int", 8),
        ("TREASURE_DIG_GAP_MS",  "Delay between dig clicks (ms)",               "int", 12000),
        ("TREASURE_MOVE_MAX_MS", "Max strafe before moving on (ms)",           "int", 2500),
    ]),
    ("Shards", [
        ("SHARDS_DIG_CLICKS",      "Exact dig clicks (0 = off)",          "int", 0),
        ("SHARDS_CLICK_CONFIRM_MS","Bar must leave empty within (ms)",    "int", 100),
        ("SHARDS_CLICK_RETRIES",   "Max clicks if the bar never moves",   "int", 3),
        ("SHARDS_ASSUME_FULL",     "Assume full once the bar moves",      "bool", False),
    ]),
    ("Mode / Dig", [
        ("PERFECT",            "Perfect dig (release on green) — off = timed hold", "bool", False),
        ("DIG_CLICK_MS",       "Dig hold length (ms)",                    "int", 75),
        ("DIG_SPEED",          "Dig speed (%) — scales the hold",         "int", 100),
        ("MAX_DIGS_TO_FILL",   "Max digs to fill the pan",                "int", 8),
        ("DIG_FILL_MS",        "Wait for FULL after each dig (ms)",        "int", 250),
        ("PRE_DIG_SETTLE_MS",  "Settle before first dig after shake (ms)", "int", 60),
        ("DIG_FILL_SMART",     "Smart fill wait (watch bar motion, 0 downtime)", "bool", False),
        ("DIG_PIPELINE",       "Pipelined digs (rhythm-fire, learn dig count)", "bool", False),
        ("DIG_PIPELINE_GAP_MS","Pipeline rhythm (ms, 0 = auto from dig speed)", "int", 0),
        ("DIG_PLATEAU_MS",     "Smart: bar still this long = dig again (ms)", "int", 120),
        ("DIG_SMART_CAP_MS",   "Smart: max wait per dig (ms)",              "int", 900),
    ]),
    ("Walk back into water", [
        ("PAN_BACK_MAX_MS",    "Max S walk-back (ms)",                    "int", 200),
        ("WATER_EXTRA_BACK_MS","Extra S after Pan cue / go deeper (ms)",  "int", 0),
    ]),
    ("Shake", [
        ("SHAKE_MOMENTUM_W",   "Hold W during shake (glide onto land)",   "bool", True),
        ("SHAKE_CLICKS",       "Exact shake clicks (0 = auto until empty)", "int", 0),
        ("SHAKE_CLICK_MS",     "Each shake click length (ms)",            "int", 18),
        ("SHAKE_CLICK_GAP_MS", "Gap between shake clicks (ms)",           "int", 14),
        ("SHAKE_HOLD_MS",      "Shake overall timeout (ms)",              "int", 1500),
        ("SHAKE_BAIL_MS",      "Shake-failed detection (ms)",             "int", 500),
        ("SHAKE_START_CONFIRM_MS","Confirm shake started within (ms, 0 = off)", "int", 0),
        ("SHAKE_START_RETRIES", "Deeper-tap retries when it won't start",     "int", 2),
        ("SHAKE_RETRY_DEEPER_MS","Retry deeper S tap (ms)",                   "int", 70),
        ("SHAKE_STALL_MS",     "Drain-stall fail-fast (ms, 0 = off)",       "int", 0),
        ("SHAKE_START_DELAY_MS","Delay before shake starts (ms)",         "int", 0),
        ("SHAKE_W_LEAD_MS","Walk forward before shaking (ms)",         "int", 0),
        ("POST_SHAKE_SETTLE_MS","Settle after pan empties (ms)",          "int", 150),
    ]),
    ("Return to land (dig-probe)", [
        ("LAND_CUE_ASSIST",    "Land assist: confirm Deposit cue before probing", "bool", False),
        ("LAND_ASSIST_MAX_MS", "Land assist W budget (ms)",               "int", 400),
        ("DEPOSIT_MAX_MS",     "Max W to find land cue (ms)",             "int", 1200),
        ("LAND_SETTLE_MS",     "Hold W after land cue (ms)",              "int", 45),
        ("DIG_PROBE_MS",       "Wait to detect a probe-dig hit (ms)",     "int", 320),
        ("PROBE_GAP_MS",       "Settle between probe digs (ms)",          "int", 80),
        ("LAND_PROBE_NUDGE_MS","Forward W nudge between probe digs (ms)", "int", 90),
        ("LAND_DIG_TRIES",     "Probe digs before giving up",             "int", 5),
    ]),
    ("Recovery / safety", [
        ("RECOVER_ENABLED",     "Enable stuck-recovery",                  "bool", True),
        ("SHAKE_RETRY_ENABLED", "Enable shake re-attempt recovery",       "bool", True),
        ("BREAKOUT_ENABLED",    "Enable break-out (escape stuck loops)",  "bool", True),
        ("STUCK_TICKS",        "Stuck reads before recovery",             "int", 3),
        ("RECOVER_LIMIT",      "Recoveries before break-out",             "int", 3),
        ("RECOVER_BACK_MS",    "Recovery nudge budget (ms)",              "int", 160),
        ("SHAKE_FAIL_LIMIT",   "Failed shakes before STOP",               "int", 5),
        ("SHAKE_GLITCH_LIMIT", "Failed shakes before quick click-to-empty", "int", 2),
        ("NO_PROGRESS_SEC",   "Seconds of no progress before click-to-empty", "int", 5),
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
        ("NOTIFY_SCREENSHOT",  "Attach a screenshot to alerts",          "bool", True),
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
        ("X_STRAFE_MS",    "X: diagonal length per pass (ms)",           "int", 220),
        ("X_RECENTER_MS",  "X: drift before auto-recenter (ms)",         "int", 400),
        ("FR_RECOVERY",    "Fortune River recovery (fast-travel on soft stop)", "bool", False),
        ("FR_TEXT_TOL",    "FR: colour match tolerance",                 "int", 55),
        ("FR_SCAN_HOVER_MS","FR: dwell per step while sweeping (ms)",     "int", 12),
        ("FR_MOVE_STEP",   "FR: max px per mouse-move step",             "int", 8),
        ("FR_FIND_TRIES",  "FR: scroll passes before giving up",         "int", 8),
        ("FR_OPEN_TRIES",  "FR: re-open warp device attempts",           "int", 3),
        ("FR_SCROLL_STEPS","FR: wheel notches per scroll",               "int", 3),
        ("FR_DOUBLE_GAP_MS","FR: gap between the two clicks (double-click) (ms)", "int", 120),
        ("FR_OPEN_MS",     "FR: wait for menu to open (ms)",             "int", 600),
        ("FR_CLICK_SETTLE_MS","FR: pause around each click (ms)",         "int", 300),
        ("FR_ACTION_GAP_MS","FR: pause between each step (ms)",           "int", 500),
        ("FR_WARP_MS",     "FR: wait after teleport to load (ms)",       "int", 2500),
        ("FR_STRAFE_MS",   "FR: tiny D strafe after return (ms)",        "int", 10),
        ("FR_WALK_MAX_MS", "FR: max W walk to reach water (ms)",         "int", 6000),
        ("FR_END_A_MS",    "FR: hold A on land before restart (ms)",     "int", 300),
        ("FR_CROSS_CONFIRM","FR: reads to confirm water-cross/arrival",   "int", 3),
        ("SR_RECOVERY",    "Starfall River recovery (fast-travel on soft stop)", "bool", False),
        ("SR_TEXT_TOL",    "SR: colour match tolerance",                 "int", 55),
        ("SR_A_MAX_MS",    "SR: max timed A strafe to water (ms)",       "int", 6000),
        ("SR_D_PCT",       "SR: D back-off (% of the A time)",           "int", 50),
        ("SR_S_MAX_MS",    "SR: max S walk to water (ms)",               "int", 4000),
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
    "Treasure chest": "A no-shake mode for collecting chests: dig, then strafe right/left "
                      "until the Collect cue, dig, repeat. Calibrate the Deposit pixel on the "
                      "Collect prompt.",
    "Shards": "Exact-click digging for shard farming: a known number of dig "
              "clicks (usually ONE), a registration check instead of probe "
              "re-digs, and optionally move on the moment the fill starts.",
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
    "Shards": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l6 6l-6 12l-6 -12z"/><path d="M6 9h12"/></svg>',
    "Treasure chest": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9l1.6-3.2A2 2 0 0 1 7.4 4.5h9.2a2 2 0 0 1 1.8 1.3L20 9"/><rect x="3.5" y="9" width="17" height="10.5" rx="1.5"/><path d="M3.5 13h17"/><rect x="10.5" y="11.5" width="3" height="3.5" rx="1"/></svg>',
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
    "TRACKER_MODE": "Benchmark the game's BUILT-IN auto-pan: the macro sends "
                     "no input at all and just watches the capacity bar, "
                     "counting digs (rise bursts, approximate) and pans "
                     "(high bar to empty, exact). Stats and History work as "
                     "normal and the run is labelled TRACKER, so you can "
                     "compare it to macro runs on the same numbers. Start it, "
                     "then turn on the game's auto-pan and keep hands off.",
    "TRACKER_POLL_MS": "How often the tracker samples the bar. 30ms is plenty.",
    "TRACKER_RELICS": "While tracking the game's Auto Pan, still fire your "
                     "relic timers: it clicks Auto Pan OFF (colour-verified), "
                     "uses the relic, returns to the pan slot, then clicks "
                     "Auto Pan back ON. Needs the Auto Pan button position + "
                     "both state colours calibrated (Calibrate tab).",
    "AUTOPAN_TOL": "Per-channel colour tolerance when deciding whether the "
                     "Auto Pan button reads ON or OFF.",
    "AUTOPAN_SETTLE_MS": "Pause after each Auto Pan button click before "
                     "re-reading its colour.",
    "AUTOPAN_GUARD": "While tracking, re-read the Auto Pan button every few "
                     "seconds; if it reads OFF twice in a row (accidental "
                     "click, game hiccup) it clicks it back ON and logs an "
                     "autopan_guard event.",
    "AUTOPAN_GUARD_SEC": "How often the guard re-reads the button. Worst-case "
                     "downtime after an accidental toggle is about twice this.",
    "AUTOPAN_SHIFTLOCK": "Turn ON if you play with shift-lock: before touching "
                     "the Auto Pan button it taps Shift (frees the cursor), "
                     "clicks, then re-locks. Leave OFF if you don't use "
                     "shift-lock (it would toggle you INTO it).",
    "AUTOPAN_FAST_RELOCK": "Re-lock shift the instant the button click is "
                     "verified, instead of parking the cursor at centre "
                     "first (the lock snaps the cursor to centre anyway). "
                     "Faster; turn off if clicks land oddly.",
    "AUTOPAN_RELOCK_DELAY_MS": "How long to wait before the Shift tap that "
                     "re-enters shift-lock: after the cursor is parked back at "
                     "centre (normal mode), or right after the verified click "
                     "(fast re-lock mode). Raise it if the re-lock ever "
                     "registers too early.",
    "AUTOPAN_STALL_SEC": "Health kick: Auto Pan sometimes wedges while its "
                     "button still shows green. If the capacity bar shows NO "
                     "activity for this many seconds while the button reads "
                     "ON, it gets toggled off and back on. 0 = off. Set it "
                     "comfortably above your slowest normal gap (try 5-8).",
    "RELIC_ON_LAND": "A due relic WAITS until the macro is on land (the "
                     "Collect Deposit cue) before placing — placing from the "
                     "water misplaces it. The normal cycle touches land every "
                     "few seconds, so the wait is short.",
    "RELIC_LAND_MAX_S": "Safety valve: if land never shows for this long "
                     "(stuck in water?), place the relic anyway.",
    "RELIC_RELATIVE": "ON: relic timers follow REAL time — pausing the macro "
                     "doesn't pause them, matching the in-game buff that "
                     "keeps burning while you're paused. OFF: timers freeze "
                     "with the pause and shift on resume.",
    "EARN_TRACK": "Reads the money and shard totals from the bottom-right HUD "
                     "with macOS text recognition and credits the GAINS to the "
                     "run: $/hr, $/pan, shards/hr, shards/pan in live stats and "
                     "History — for macro runs AND Tracker runs, so builds and "
                     "the game's auto-pan compare on real earnings. Calibrate "
                     "the four Money/Shards corners in the Calibrate tab first. "
                     "Spending mid-run is ignored (only gains count).",
    "EARN_OCR_SEC": "How often the totals are read. 10s is plenty — earnings "
                     "are credited as deltas, nothing is missed between reads.",
    "FINDS_TRACK": "Reads the bottom-right item pop-up and logs every find — "
                     "modifier, name, weight, rarity — into the Analytics "
                     "window and History. Kept items get an estimated value "
                     "from prospecting_prices.json x your sell boost, feeding "
                     "loot $/hr and TOTAL $/hr (auto-sold money is already "
                     "measured by the money reader). Counting runs on a fast "
                     "pixel sampler that watches each card fade in, so burst "
                     "drops (several skulls in one pan) all count. Calibrate "
                     "the two 'Find pop-up' corners first.",
    "FINDS_FAST_MS": "How often the fast pixel sampler measures the finds box "
                     "-- no OCR, just how many cards are up and how solid each "
                     "one is (their fade). This is what COUNTS arrivals, so it "
                     "stays quick; 40ms tracks even burst drops.",
    "FINDS_OCR_MS": "Shortest gap between identity OCR passes (name / weight / "
                     "rarity). The fast sampler does the counting, so identity "
                     "can lag safely; 200ms keeps names snappy without pegging "
                     "a core.",
    "FINDS_STACK_NEWEST": "Which end a newly-found item card appears at. The "
                     "list scrolls the other way as older cards fade. 'bottom' "
                     "(default) = new slides in underneath and pushes older "
                     "ones up; set 'top' if yours is the other way.",
    "FINDS_MIN_CONF": "How confident a text read must be for the OCR safety "
                     "net to start a find on its own (0-1). The pixel tracker "
                     "does the main counting; this only gates the net that "
                     "catches cards the sampler never saw.",
    "FINDS_MIN_DWELL": "How many consecutive fast samples a card band must "
                     "survive before it can count. Filters one-frame animation "
                     "flicker; raise it if phantom finds appear, lower it "
                     "(min 1) if very fast cards are missed.",
    "FINDS_EMPTY_MS": "How long the finds box must stay quiet (no card bands) "
                     "before lingering cards are finalised and the stack "
                     "resets.",
    "FINDS_CARD_SEC": "How long one find card stays on screen, animations "
                     "included (~5s). Sets the re-sighting window: a card "
                     "with the same name/weight re-detected within this long "
                     "of vanishing abruptly (camera swing washing out the "
                     "pixel signal) is the SAME card and updates the original "
                     "find instead of counting again. Real enchant duplicates "
                     "coexist on screen, so they still count separately.",
    "FINDS_WHITE_MIN": "How bright a pixel must be (every RGB channel, 0-255) "
                     "to count as card TEXT. Vividly coloured tier text "
                     "(Cosmic and friends) is caught too even though it isn't "
                     "pure white. Lower it if the sampler misses faint cards; "
                     "raise it if bright terrain fakes cards.",
    "FINDS_DARK_MAX": "How dark a nearby pixel must be (every RGB channel, "
                     "0-255) to count as the card's dark backing. Text only "
                     "counts NEXT TO backing -- that's what makes the sampler "
                     "terrain-proof. Raise it if cards are missed over dark "
                     "ground.",
    "FINDS_BAND_MIN": "Minimum fraction of a pixel row that must read as card "
                     "text for the row to join a card band (0-1). The noise "
                     "floor under everything; raise it if ghost cards appear.",
    "FINDS_DEBUG": "Log the tracker's internals to the trace (bands, tracks, "
                     "stack shifts, ghosts, inferred finds). Turn on when "
                     "counts look wrong; leave off for normal runs.",
    "SELL_BOOST_PCT": "Your sell boost as a percent (100 = 1x, 250 = 2.5x). "
                     "Multiplies the estimated value of logged finds.",
    "FINDS_BANK_RARITY": "Only value finds at/above this rarity as KEPT loot "
                     "(Common/Uncommon/Rare/Epic/Legendary/Mythic/Exotic). "
                     "Lower-rarity finds auto-sell and are already in the "
                     "money counter, so valuing them here would double-count. "
                     "Skull build banks Exotic; set it to whatever your "
                     "Auto-Sell keeps.",
    "SHARDS_DIG_CLICKS": "Shards mode master switch: click the dig EXACTLY "
                     "this many times per land visit -- no probe re-digs, no "
                     "double clicks from bar-detection races. Set 1 for a "
                     "one-click-fill build. 0 = off (normal dig logic).",
    "SHARDS_CLICK_CONFIRM_MS": "After the first click, the capacity bar must "
                     "leave EMPTY (left tip no longer gray) within this long "
                     "to prove the click registered. Only a provably-dead "
                     "click is retried, so the animation being slow can never "
                     "cause a double dig.",
    "SHARDS_CLICK_RETRIES": "If the bar never moves, how many total clicks to "
                     "try before deciding you're off land and nudging "
                     "forward. Dead clicks happen (very short dig holds); 3 "
                     "is a good default.",
    "SHARDS_ASSUME_FULL": "One-click-fill builds only: the instant the bar "
                     "starts filling, treat the pan as FULL and move on -- "
                     "the walk back to water happens DURING the fill "
                     "animation instead of after it. The shake clears the "
                     "assumption automatically.",
    "TREASURE_MODE": "Switch to Treasure Chest Collection: NO shaking. It digs briefly, then "
                     "holds D until the Collect cue shows, digs, holds A until the cue, and so on "
                     "-- alternating sides. Uses the Deposit pixel as the Collect cue.",
    "TREASURE_DIGS": "How many digs to do at each chest before strafing on. Raise it if the dig animation is slow, so the dig finishes before it moves.",
    "TREASURE_DIG_MS": "Length of the quick dig click in Treasure mode (5-10 ms).",
    "TREASURE_DIG_GAP_MS": "How long to wait between each dig click. The chest dig animation is slow, so this keeps clicks from stacking. About 12000 ms (12 s) is a good start.",
    "TREASURE_MOVE_MAX_MS": "Safety cap: stop strafing after this long even if the Collect cue "
                            "never appears, so it cannot run sideways forever.",
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
    "DIG_PIPELINE": "Cuts the dead time between digs: instead of waiting for "
                     "the bar animation to settle after EVERY dig, it learns "
                     "how many digs fill your pan (first 3 fills run normally) "
                     "and fires the follow-ups on your dig-animation rhythm, "
                     "checking the bar only after the last. Falls back and "
                     "relearns if a fill comes up short.",
    "DIG_PIPELINE_GAP_MS": "Rhythm between pipelined digs. 0 = derived from "
                     "your Dig speed (190000/speed + 25ms). Raise if pipelined "
                     "digs skip; lower for max speed.",
    "DIG_FILL_SMART": "Fixes wasted dig animations: the fill bar ANIMATES "
                     "after a dig, and the plain wait can time out mid-animation "
                     "and re-dig pointlessly. Smart wait keeps waiting while the "
                     "bar is still rising, and digs again the instant it stops "
                     "short of full. Good for every build.",
    "DIG_PLATEAU_MS": "Smart fill: how long the bar must sit unchanged (below "
                     "full) before we conclude the dig wasn't enough and dig again.",
    "DIG_SMART_CAP_MS": "Smart fill: absolute cap on one wait, in case the bar "
                     "misbehaves. Keep well above your bar's fill-animation time.",
    "PRE_DIG_SETTLE_MS": "Tiny pause after landing before the first dig, so it "
                         "doesn't fire mid-glide and miss.",
    "PAN_BACK_MAX_MS": "Safety cap on the backward (S) walk into the water.",
    "WATER_EXTRA_BACK_MS": "Keep holding S a bit after the Pan cue to go deeper, "
                           "so the shake doesn't start right at the edge.",
    "SHAKE_MOMENTUM_W": "Hold W while shaking so momentum carries you back onto "
                        "land as the pan drains.",
    "SHAKE_CLICKS": "Do EXACTLY this many shake clicks then stop (0 = auto: shake until the pan reads empty). Use a fixed count if an extra auto-click bleeds into the next dig and ruins your perfect digs — set it to the number your build needs to empty.",
    "SHAKE_CLICK_MS": "Length of each shake click (the shake is a rapid click "
                      "stream, since a held press is dropped on macOS).",
    "SHAKE_CLICK_GAP_MS": "Gap between shake clicks. Lower = faster rattle.",
    "SHAKE_HOLD_MS": "Overall shake time limit; it stops early when the pan empties.",
    "SHAKE_BAIL_MS": "If the pan is STILL completely full after this long, the "
                     "shake didn't start — give up and retry. Keep above a real "
                     "shake's drain time.",
    "SHAKE_START_CONFIRM_MS": "Opt-in fast save (0 = off). If the pan is still "
                     "completely full this long after clicking starts, the shake "
                     "never initiated (edge click / glitch) — tap S a touch "
                     "deeper and keep clicking instead of losing the cycle. "
                     "Try 300. Keep it below Shake-failed detection.",
    "SHAKE_START_RETRIES": "How many deeper-tap retries per shake before giving "
                     "up and letting the normal bail/recovery handle it.",
    "SHAKE_RETRY_DEEPER_MS": "Length of the S tap used by a start retry. Bigger "
                     "= walks back deeper into the water each retry.",
    "SHAKE_STALL_MS": "Opt-in fail-fast (0 = off). If a STARTED shake's bar "
                     "freezes for this long mid-drain, the game dropped the "
                     "shake — end the attempt now instead of clicking out the "
                     "whole timeout. Try 600. Keep it well above one drain "
                     "tick so a slow build can't false-trigger.",
    "SHAKE_W_LEAD_MS": "Momentum pre-roll: after reaching the water, hold W "
                     "and glide toward land for EXACTLY this long before the "
                     "first shake click -- built-up speed then carries you "
                     "onto land as the pan drains. Pure time, no screen "
                     "reads: the same glide every cycle (cues are useless "
                     "mid-glide; the Shake cue sticks). Raise it if you "
                     "stall in the water after shaking; lower it if the "
                     "click lands on shore (shake-start retries in the "
                     "trace). Needs 'Hold W during shake' ON. 0 = off.",
    "SHAKE_START_DELAY_MS": "Pause between reaching the water and starting the "
                            "shake (usually 0).",
    "POST_SHAKE_SETTLE_MS": "Pause after the pan empties so momentum settles you "
                            "onto land before the dig-probe.",
    "LAND_CUE_ASSIST": "Before the first probe dig, if 'Collect Deposit' isn't "
                     "on screen yet, hold W briefly until it shows — so the "
                     "probe digs from ON the dirt instead of nudging blindly "
                     "toward it. Targets the #1 wasted-time source (nudges). "
                     "The capacity bar still verifies every dig.",
    "LAND_ASSIST_MAX_MS": "How long the assist may hold W waiting for the cue "
                     "before letting the normal probe take over.",
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
    "NO_PROGRESS_SEC": "If nothing actually completes for this many seconds (no pan emptied and no dig registering -- e.g. the shake-glitch leaves the macro walking back and forth thinking all is fine) it does a quick click-to-empty break-out. Lower = reacts faster but may interrupt a genuinely slow cycle.",
    "SHAKE_GLITCH_LIMIT": "When the game fails to register a shake (the shake-glitch), the macro keeps trying to dig/move and wastes time. After this many failed shakes it immediately does a quick click-to-empty (break-out) instead, which usually clears the glitch and is low-risk. Lower = reacts faster.",
    "SHAKE_FAIL_LIMIT": "Shakes that don't empty (even while moving) before it "
                        "SAFE STOPs.",
    "BREAKOUT_LIMIT": "Break-out attempts before SAFE STOP.",
    "BREAKOUT_SHAKE_MS": "When stuck, click this long to finish a shake that's "
                         "locking your movement.",
    "BREAKOUT_REPOS_MS": "Forward reposition nudge during a break-out.",
    "BURST_ON_MS": "Recovery taps: how long each tap holds the key.",
    "BURST_OFF_MS": "Recovery taps: how long each tap releases before re-checking.",
    "NOTIFY_SCREENSHOT": "Attach a picture of your screen to the safe-stop, recovery, hard-stop and stats DMs, so you can see what happened without going to your computer. Needs the Discord bot updated to forward images.",
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
    "RECOVER_ENABLED": "Master switch for the stuck-recovery step (the jitter taps that nudge you back on track). Turn off if recovery misbehaves; the macro then just retries the normal loop and safe-stops if it genuinely cannot progress.",
    "SHAKE_RETRY_ENABLED": "When stuck full and in the water, re-attempt the shake. Turn off if the re-shake bugs out.",
    "BREAKOUT_ENABLED": "The last-resort move that finishes a stuck shake and repositions. Turn off if you do not want it.",
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
    "FR_RECOVERY": "When a SOFT stop happens, fast-travel back to Fortune River and resume automatically. Press 4, Shift, find the pink Fortune River row in the Fast Travel list (scrolling if needed), click it, then switch to the pan and walk into the water. Calibrate the FR spots in the Calibrate tab. Test it with Ctrl+J (manual soft-stop).",
    "FR_TEXT_TOL": "How close a pixel's colour must be to the calibrated Fortune River pink to count as a match (per channel). Raise it if it never finds the row, lower it if it clicks the wrong one.",
    "FR_MOVE_STEP": "How many pixels the cursor moves per step during recovery. Smaller = smoother and more accurate (less affected by mouse acceleration), but slower.",
    "FR_SCAN_HOVER_MS": "How long the cursor pauses at each step as it sweeps down the list column looking for the pink row. Higher = slower but smoother sweep.",
    "FR_FIND_TRIES": "How many full top-to-bottom sweeps (scrolling down between each) to do looking for Fortune River before re-opening the device.",
    "FR_OPEN_TRIES": "If a full search finds nothing, the warp device probably did not open. This is how many times to re-equip slot 4 and re-open before giving up. Raise it if the device only opens some of the time.",
    "FR_SCROLL_STEPS": "How many mouse-wheel notches to scroll each time Fortune River is not visible in the box.",
    "FR_DOUBLE_GAP_MS": "Gap between the two mouse clicks of the double-click that opens Fast Travel (after switching to hotkey 4). Lower it if the double-click is not registering.",
    "FR_OPEN_MS": "How long to wait for the Fast Travel menu to appear after the double-tap on 4.",
    "FR_CLICK_SETTLE_MS": "Pause after moving the cursor and before/after each click, so the game registers the hover and click. Raise it if clicks are not landing.",
    "FR_ACTION_GAP_MS": "Pause between every step of the recovery (key presses, menu open, returning to the pan). Raise it if things happen too fast for the game to keep up.",
    "FR_WARP_MS": "How long to wait after clicking Fortune River for the teleport and world to finish loading.",
    "FR_STRAFE_MS": "Length of the small D tap used to line up after returning to the pan.",
    "FR_WALK_MAX_MS": "Maximum time to hold W walking forward to reach the water / dig spot before giving up.",
    "FR_END_A_MS": "Once it reaches land (Collect Deposit cue), hold A for this long to line up, then restart the macro loop. Set 0 to skip.",
    "SR_RECOVERY": "Fast-travel to STARFALL RIVER after a soft stop and "
                     "resume: uses the same Fast-Travel menu calibration as "
                     "Fortune River (scan column, list box, home pixel) with "
                     "Starfall's own row colour (calibrate it below). Post-warp "
                     "walk: hold A briefly, then S until the Pan cue. If both "
                     "SR and FR recovery are on, Starfall wins.",
    "SR_TEXT_TOL": "Per-channel colour tolerance when matching the Starfall "
                     "row in the travel list.",
    "SR_A_MAX_MS": "Cap on the timed A strafe: after the warp it holds A "
                     "until the Pan cue shows, measuring how long that took. "
                     "Fails the recovery if the water never appears in time.",
    "SR_D_PCT": "After the timed A finds the water, hold D for this percent "
                     "of the measured time to centre on the strip, then S "
                     "walks into the water at the dig spot. 50 = half.",
    "SR_S_MAX_MS": "Cap on the S walk back to the Pan cue. If it never sees "
                     "water, the recovery reports failure instead of wandering.",
    "FR_CROSS_CONFIRM": "How many cue reads in a row are needed to count as truly in the water, and then truly on the next shore. Higher = it must really cross the water before restarting (stops it from restarting on the shore it spawned on). Lower if it is too slow to commit.",
    "X_PATTERN": "Walk back into the water on alternating 45° diagonals instead of "
                 "straight back, so each pass covers new ground. Helps when you keep "
                 "falling short on a straight line. Forward to land stays straight. The "
                 "diagonal is now a short burst (see below) and auto-recenters so it "
                 "doesn't drift off the strip.",
    "X_STRAFE_MS": "How long the diagonal (sideways) part of the back-walk is held each "
                   "pass before it finishes STRAIGHT back. Lower = straighter, tighter, "
                   "less drift and more consistent depth/landing; higher = covers more "
                   "new ground sideways. 0 = old behaviour (diagonal the whole way).",
    "X_RECENTER_MS": "Once the X pattern has drifted this many ms of sideways travel off "
                     "centre, it strafes straight back toward the middle before the next "
                     "pass — so it can't wander off the good dig strip. 0 = never recenter.",
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
    ("MONEY_TL_PIXEL", "Money counter — TOP-LEFT corner",
     "Top-left corner of the money region (bottom-right HUD). Leave generous "
     "room to the LEFT — the number grows longer as you earn.",
     [0, 0]),
    ("MONEY_BR_PIXEL", "Money counter — BOTTOM-RIGHT corner",
     "Bottom-right corner of the money region, just past the last digit.",
     [0, 0]),
    ("SHARDS_TL_PIXEL", "Shards counter — TOP-LEFT corner",
     "Top-left corner of the shards number (next to the crystal icon). Leave "
     "room to the left for growth.",
     [0, 0]),
    ("SHARDS_BR_PIXEL", "Shards counter — BOTTOM-RIGHT corner",
     "Bottom-right corner of the shards number.",
     [0, 0]),
    ("FIND_TL_PIXEL", "Find stack — TOP-LEFT corner",
     "Top-left of the whole FINDS STACK area (bottom-right HUD). Finds stack "
     "vertically and fade, so make the box TALL enough to cover several cards "
     "at once, and wide enough for the longest item name.",
     [0, 0]),
    ("FIND_BR_PIXEL", "Find stack — BOTTOM-RIGHT corner",
     "Bottom-right of the finds stack area, past the newest card's weight. "
     "The taller the box, the more of the fading stack the tracker can read.",
     [0, 0]),
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
