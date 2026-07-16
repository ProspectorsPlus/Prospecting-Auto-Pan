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
        ("EARN_TRACK",   "Track money and shards (OCR the HUD totals)", "bool", False),
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
        ("TREASURE_DIGS",        "Digs per spot before strafing",              "int", 1),
        ("TREASURE_DIG_MS",      "Quick dig click (ms)",                       "int", 8),
        ("TREASURE_DIG_GAP_MS",  "Delay between dig clicks (ms)",               "int", 12000),
        ("TREASURE_MOVE_MAX_MS", "Max strafe before moving on (ms)",           "int", 2500),
    ]),
    ("Shards", [
        ("SHARDS_DIG_CLICKS",      "Exact dig clicks (0 = off)",          "int", 0),
        ("SHARDS_CLICK_CONFIRM_MS","Bar must leave empty within (ms)",    "int", 100),
        ("SHARDS_CLICK_RETRIES",   "Max clicks if the bar never moves",   "int", 3),
        ("SHARDS_ASSUME_FULL",     "Assume full once the bar moves",      "bool", False),
        ("SHARDS_GREEN_CONFIRM",   "Green dig-bar confirms the click",    "bool", False),
    ]),
    ("Geodes", [
        ("GEODE_MODE",           "Geode mode (slow-animation dig, normal shake)", "bool", False),
        ("GEODE_DIGS_TO_FILL",   "Digs to fill (0 = auto until full)",  "int", 0),
        ("GEODE_DIG_MS",         "Quick dig hold (ms)",                 "int", 5),
        ("GEODE_DELAY_MS",       "Animation delay per dig (ms)",        "int", 1500),
        ("GEODE_START_MS",       "Wait for the dig to start (ms)",      "int", 800),
        ("GEODE_CONFIRM_FULL",   "Wait for the bar to read full first", "bool", False),
        ("GEODE_SHAKE_HOLD_MS",  "Max shake time (ms)",                 "int", 8000),
    ]),
    ("Mode / Dig", [
        ("PERFECT",            "Perfect dig (release on green), off = timed hold", "bool", False),
        ("DIG_CLICK_MS",       "Dig hold length (ms)",                    "int", 75),
        ("DIG_SPEED",          "Dig speed (%), scales the hold",          "int", 100),
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
        ("CAP_EMPTY_FRAC",     "Pan-empty threshold (lower = wait emptier)", "float", 0.04),
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
        ("SAFE_STOP_RETRY",     "Safe-stop = pause and retry (don't hard-stop)", "bool", True),
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
    ("Advanced tuning", [
        ("SMART_TIMING",   "Auto-tune timing by trial and error",        "bool", False),
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
PRESET_GEODE = {
                "GEODE_MODE": True, "GEODE_DIGS_TO_FILL": 0, "GEODE_DIG_MS": 5,
                "GEODE_DELAY_MS": 12000, "GEODE_START_MS": 800,
                "GEODE_CONFIRM_FULL": True, "GEODE_SHAKE_HOLD_MS": 10000,
                "SHAKE_MOMENTUM_W": True, "SHAKE_CLICKS": 0, "SHAKE_CLICK_MS": 60,
                "SHAKE_CLICK_GAP_MS": 0, "SHAKE_HOLD_MS": 6000, "SHAKE_BAIL_MS": 500,
                "SHAKE_START_CONFIRM_MS": 300, "SHAKE_START_RETRIES": 1,
                "SHAKE_RETRY_DEEPER_MS": 180, "SHAKE_STALL_MS": 0,
                "SHAKE_START_DELAY_MS": 0, "SHAKE_W_LEAD_MS": 50,
                "POST_SHAKE_SETTLE_MS": 150, "PERFECT": False, "DIG_CLICK_MS": 5,
                "DIG_SPEED": 1474, "MAX_DIGS_TO_FILL": 1, "DIG_FILL_MS": 2050,
                "PRE_DIG_SETTLE_MS": 600, "PAN_BACK_MAX_MS": 100,
                "WATER_EXTRA_BACK_MS": 0, "LAND_SETTLE_MS": 0,
                "EASY_WATER_RETURN_DELAY_MS": 0, "SHARDS_DIG_CLICKS": 0}
DEFAULTS = {k: d for _, items in SECTIONS for (k, _l, _t, d) in items}
TYPES = {k: t for _, items in SECTIONS for (k, _l, t, _d) in items}


def load_saved():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return {}


SECTION_HINT = {
    "Treasure chest": "A no-shake mode for farming the two spots at Rubble Creek: stand on "
                      "the deposit, it digs, strafes across to the sands, digs, then strafes "
                      "back, over and over. Calibrate the Deposit pixel on the Collect prompt.",
    "Shards": "Exact-click digging for shard farming: a known number of dig "
              "clicks (usually ONE), a registration check instead of probe "
              "re-digs, and optionally move on the moment the fill starts.",
    "Geodes": "For geode builds with a very slow fill animation. Taps the dig a "
              "set number of times, waits out the animation between each (so it "
              "won't false-nudge while the fill is still catching up), then runs "
              "the normal walk-back + momentum shake -- not treasure's strafe.",
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
    "Advanced tuning": "Experimental auto-tuning and movement patterns. "
                            "Off by default. Turn one on and test it.",
}


TAB_ICON = {
    "Shards": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l6 6l-6 12l-6 -12z"/><path d="M6 9h12"/></svg>',
    "Geodes": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2l7 5.5-2.5 9.5h-9L5 7.5z"/><circle cx="12" cy="11" r="3"/></svg>',
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
    "Advanced tuning": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3h6M10 3v6l-5 8a2 2 0 0 0 1.8 3h10.4a2 2 0 0 0 1.8 -3l-5 -8V3"/><path d="M7.5 14h9"/></svg>',
}

# Per-setting explanations (shown as a ? tooltip next to each field).
HELP = {
    "TRACKER_MODE": "**Watch-only benchmarking.** The macro sends no input at "
                    "all: it reads the capacity bar and counts the game's own "
                    "Auto Pan, so you can compare it with macro runs on the same "
                    "numbers.\n"
                    "when: you want to know if your build out-earns the built-in "
                    "Auto Pan.\n"
                    "steps: start the macro with this on | turn on the game's "
                    "Auto Pan | keep hands off, watch the stats\n"
                    "pairs: Use relic timers during Tracker | Tracker sampling "
                    "rhythm\n"
                    "Note: runs are labelled TRACKER in History. Recovery and "
                    "relics stay off while it watches.",
    "TRACKER_POLL_MS": "**How often Tracker samples the capacity bar** while it "
                       "watches the game's Auto Pan.\n"
                       "raise: your machine struggles; fewer reads is lighter.\n"
                       "lower: very fast pans are being {{missed between samples}}.\n"
                       "Note: 30ms catches every fill and drain on any normal build.",
    "TRACKER_RELICS": "**Keep relic timers firing during Tracker runs.** It clicks "
                      "Auto Pan off, places the relic, then clicks Auto Pan back "
                      "on, colour-checking the button each time.\n"
                      "when: benchmarking long enough that buffs would expire.\n"
                      "steps: calibrate the Auto Pan button ON state | calibrate "
                      "the OFF state | enable this and your relic rows\n"
                      "pairs: Guard: re-enable Auto Pan if it turns off | Auto Pan "
                      "button colour tolerance\n"
                      "Note: needs the button position and both state colours "
                      "calibrated.",
    "AUTOPAN_TOL": "**How far each colour channel may drift** from the "
                   "calibrated ON and OFF colours before the button no longer "
                   "counts as that state.\n"
                   "raise: lighting or effects shift the shade and toggles "
                   "{{misread the button}}.\n"
                   "lower: it confuses ON with OFF.\n"
                   "pairs: Use relic timers during Tracker",
    "AUTOPAN_SETTLE_MS": "**Pause after each Auto Pan click** before the colour is "
                         "re-read to confirm the click worked.\n"
                         "raise: the toggle animation is slow and clicks "
                         "{{double-fire}} on a laggy game.\n"
                         "pairs: Auto Pan button colour tolerance",
    "AUTOPAN_GUARD": "**The Auto Pan babysitter.** While Tracker runs, it re-reads "
                     "the button every few seconds; if it reads OFF twice in a "
                     "row, it clicks it back ON and logs a guard event.\n"
                     "when: unattended Tracker runs, so an accidental toggle "
                     "cannot stall hours of benchmarking.\n"
                     "fixes: {{Auto Pan silently off, run wasted}}\n"
                     "pairs: Guard check interval | Restart Auto Pan if idle this "
                     "long\n"
                     "Note: needs the Auto Pan button calibrated.",
    "AUTOPAN_GUARD_SEC": "**How often the guard re-reads the button.** Worst-case "
                         "downtime after an accidental toggle is about twice this "
                         "value.\n"
                         "lower: you keep seeing long gaps where Auto Pan was off.\n"
                         "raise: you want fewer screen reads.\n"
                         "pairs: Guard: re-enable Auto Pan if it turns off",
    "AUTOPAN_SHIFTLOCK": "**For shift-lock players only.** Before touching the Auto "
                         "Pan button it taps Shift to free the cursor, clicks, then "
                         "re-locks, so the click lands instead of spinning your "
                         "camera.\n"
                         "when: you play with shift-lock on.\n"
                         "fixes: {{clicks spin the camera instead of landing}}\n"
                         "Note: leave OFF without shift-lock, or it toggles you INTO "
                         "it.",
    "AUTOPAN_FAST_RELOCK": "**Re-lock Shift the instant the click is confirmed**, "
                           "instead of parking the cursor at centre first. Slightly "
                           "faster, since the lock snaps the cursor to centre anyway.\n"
                           "when: you use the shift-lock dance and want the toggle "
                           "quicker.\n"
                           "lower: turn it off if clicks land in {{odd places}} after a "
                           "relic toggle.\n"
                           "pairs: Shift-lock dance around Auto Pan clicks",
    "AUTOPAN_RELOCK_DELAY_MS": "**Wait before the Shift tap that re-enters shift-lock**, "
                               "after the cursor is parked (normal) or right after the "
                               "verified click (fast re-lock).\n"
                               "raise: the re-lock registers {{too early}} and eats the "
                               "click.\n"
                               "pairs: Re-lock shift immediately after the click",
    "AUTOPAN_STALL_SEC": "**The idle kick.** Auto Pan sometimes wedges while its "
                         "button still shows green; if the bar shows no activity for "
                         "this long while ON, it gets toggled off and back on.\n"
                         "raise: set it comfortably above your slowest normal gap (try "
                         "5 to 8).\n"
                         "fixes: {{Auto Pan green but nothing happening}}\n"
                         "Note: 0 = off.",
    "RELIC_ON_LAND": "**Place relics only from land.** A due relic waits for the "
                     "Collect Deposit cue before placing, because placing from the "
                     "water misplaces it.\n"
                     "when: relics keep {{landing in the water}}.\n"
                     "pairs: Max wait for land before placing anyway\n"
                     "Note: the loop touches land every few seconds, so the wait "
                     "is short.",
    "RELIC_LAND_MAX_S": "**Safety valve for the on-land wait**: if the land cue never "
                        "shows for this long, the relic is placed anyway so the buff "
                        "is not lost.\n"
                        "raise: your relics are expensive and worth waiting longer "
                        "for the right spot.\n"
                        "lower: a wasted placement matters less than a late buff.\n"
                        "pairs: Place relics only when ON LAND",
    "RELIC_RELATIVE": "**Relic timers follow real time.** Pausing the macro does "
                      "not pause them, which matches the in-game buff that keeps "
                      "burning while you are paused.\n"
                      "when: leave ON so cooldowns stay honest across pauses.\n"
                      "Note: OFF freezes timers with the pause and shifts them on "
                      "resume.",
    "EARN_TRACK": "**Money and shards, measured.** Reads the HUD totals and "
                  "credits the gains to the run: $/hr, $/pan, shards/hr in live "
                  "stats, Analytics and History, for macro AND Tracker runs.\n"
                  "when: you want builds compared on real income, not just pans "
                  "per hour.\n"
                  "steps: draw the Money box on the Calibrate page | draw the "
                  "Shards box | confirm with Test money/shards read\n"
                  "fixes: {{no idea what a build actually earns}}\n"
                  "Note: gains only; spending mid-run is ignored. macOS only.",
    "EARN_OCR_SEC": "**How often the totals are read.** Earnings count as the "
                    "difference between reads, so nothing is missed in between.\n"
                    "lower: you want the live $/hr to react faster.\n"
                    "raise: you want fewer screen reads; accuracy does not "
                    "change.\n"
                    "pairs: Track money and shards",
    "FINDS_TRACK": "**Logs every find.** Watches the item pop-ups and records "
                   "modifier, name, weight and rarity, feeding the ticker, loot "
                   "value and Analytics.\n"
                   "steps: draw the Find box on the Calibrate page | confirm "
                   "with Test find pop-up read\n"
                   "fixes: {{no record of what you dug up}}\n"
                   "pairs: New find appears at (bottom / top) | Sell boost for "
                   "loot value\n"
                   "Note: kept items get a value from your prices times sell "
                   "boost. macOS only.",
    "FINDS_FAST_MS": "**The fast pixel sampler.** No OCR, it just measures how "
                     "many cards are up and how solid each one is; this is what "
                     "counts arrivals, so burst drops all land.\n"
                     "lower: very fast back-to-back drops are being {{missed}}.\n"
                     "raise: you want less CPU; counting gets coarser.\n"
                     "pairs: Identity OCR at most every",
    "FINDS_OCR_MS": "**The identity reader.** The shortest gap between full name, "
                    "weight and rarity reads; the fast sampler does the counting, "
                    "so identity can lag safely.\n"
                    "lower: item names show up slowly in the ticker.\n"
                    "raise: OCR is costing too much CPU.\n"
                    "pairs: Fast opacity sample every",
    "FINDS_STACK_NEWEST": "**Which end a new find card appears at.** The list scrolls "
                          "the other way as older cards fade.\n"
                          "when: bottom (default) = new cards slide in underneath; set "
                          "top if yours is the other way.\n"
                          "fixes: {{find counts doubled or missed}} when the stack "
                          "direction is wrong",
    "FINDS_MIN_CONF": "**The OCR safety net gate** (0 to 1): how confident a text "
                      "read must be to start a find on its own. The pixel tracker "
                      "does the main counting; this only gates the backup.\n"
                      "raise: phantom finds appear from misreads.\n"
                      "lower: genuine finds occasionally slip through.",
    "FINDS_MIN_DWELL": "**Flicker filter**: how many consecutive fast samples a card "
                       "must survive before it counts.\n"
                       "raise: phantom finds appear from one-frame flicker.\n"
                       "lower: (min 1) very fast cards are being missed.",
    "FINDS_EMPTY_MS": "**Stack reset timer**: how long the finds area must stay "
                      "quiet before lingering cards are finalised and the stack "
                      "resets.\n"
                      "raise: fast bursts are being {{merged into one}}.\n"
                      "lower: separate drops are being run together.",
    "FINDS_CARD_SEC": "**How long one card lives on screen**, animations included. "
                      "Sets the re-sighting window: the same name and weight "
                      "re-detected within this long of vanishing abruptly counts as "
                      "the SAME card.\n"
                      "when: leave at the game's real card lifetime (about 5s).\n"
                      "Note: real enchant duplicates coexist on screen, so they "
                      "still count separately.",
    "FINDS_WHITE_MIN": "**Brightness floor for card text** (every RGB channel). "
                       "Coloured tier text is caught too, even though it is not pure "
                       "white.\n"
                       "lower: the sampler misses {{faint cards}}.\n"
                       "raise: bright terrain fakes cards.\n"
                       "pairs: Card backing darkness ceiling",
    "FINDS_DARK_MAX": "**Darkness ceiling for the card backing.** Text only counts "
                      "NEXT TO dark backing; that is what makes the sampler "
                      "terrain-proof.\n"
                      "raise: cards are missed over {{dark ground}}.\n"
                      "lower: bright scenes are faking backing.\n"
                      "pairs: Card text brightness floor",
    "FINDS_BAND_MIN": "**The noise floor** (0 to 1): the fraction of a pixel row "
                      "that must read as card text before the row joins a card "
                      "band.\n"
                      "raise: ghost cards appear over busy backgrounds.\n"
                      "lower: faint cards are missed.\n"
                      "Note: well tuned by default; only touch it if counts are "
                      "clearly off.",
    "FINDS_DEBUG": "**Tracker internals in the trace**: bands, tracks, stack "
                   "shifts, ghosts, inferred finds.\n"
                   "when: your find counts look wrong and you want to see why.\n"
                   "Note: noisy; turn it back off for normal runs.",
    "SELL_BOOST_PCT": "**Your sell boost as a percent** (100 = 1x, 250 = 2.5x). "
                      "Multiplies the estimated value of logged finds so loot value "
                      "matches what your gear actually sells for.\n"
                      "when: set it to your real in-game sell boost.\n"
                      "pairs: Value finds at/above rarity\n"
                      "Note: only affects the loot estimate, not the money read off "
                      "the HUD.",
    "FINDS_BANK_RARITY": "**The kept-loot cutoff.** Only finds at or above this rarity "
                         "are valued as kept loot; below it auto-sells and is already "
                         "in the money counter, so valuing it here would double-count.\n"
                         "when: match it to whatever your Auto-Sell keeps.\n"
                         "pairs: Sell boost for loot value",
    "SHARDS_DIG_CLICKS": "**Shards mode master switch**: click the dig EXACTLY this "
                         "many times per land visit, no probe re-digs, no double "
                         "clicks from bar-detection races.\n"
                         "when: a fixed number of clicks (often ONE) fills your pan.\n"
                         "fixes: {{digs 2 to 3 times before the bar moves}} on very "
                         "fast builds\n"
                         "pairs: Green dig-bar confirms the click | Assume full once "
                         "the bar moves\n"
                         "Note: 0 = off, the normal dig logic runs.",
    "SHARDS_CLICK_CONFIRM_MS": "**Proof window per click**: the bar must leave empty within "
                               "this long, or the click is declared dead and retried. Only a "
                               "provably dead click is retried, so a slow animation can "
                               "never cause a double dig.\n"
                               "raise: good clicks are being {{retried}} because your bar "
                               "reacts slowly.\n"
                               "pairs: Max clicks if the bar never moves",
    "SHARDS_CLICK_RETRIES": "**Dead-click budget**: if the bar never moves, how many "
                            "total clicks to try before deciding you are off land and "
                            "nudging forward.\n"
                            "raise: good digs are abandoned too early.\n"
                            "lower: it wastes clicks on empty air.\n"
                            "pairs: Bar must leave empty within",
    "SHARDS_GREEN_CONFIRM": "**Green-bar proof.** Uses the dig skill-bar popping up as "
                            "proof the click registered, frames before the capacity bar "
                            "moves (which can lag by 2 to 3 whole animations).\n"
                            "when: very fast builds where the capacity bar lags behind "
                            "reality.\n"
                            "steps: calibrate the Green dig pixel on the Calibrate page | "
                            "turn this on\n"
                            "Note: Perfect mode itself can stay OFF. Auto-disarms for the "
                            "visit on green terrain.",
    "SHARDS_ASSUME_FULL": "**Skip the full read.** The instant the bar starts filling, "
                          "treat the pan as FULL and move on; the walk to water happens "
                          "DURING the fill animation.\n"
                          "when: one-click-fill builds only.\n"
                          "fixes: {{dead time waiting out the fill animation}}\n"
                          "Note: the shake clears the assumption automatically.",
    "GEODE_MODE": "**Built for very slow fills.** Geode shovels fill so slowly "
                  "that normal logic thinks the dig failed; this taps the dig, "
                  "WAITS OUT the animation, then runs the normal walk-back and "
                  "momentum shake.\n"
                  "when: 10% dig-speed geode shovels and similar.\n"
                  "fixes: {{false nudges mid-fill on geode gear}}\n"
                  "pairs: Animation delay per dig | Digs to fill\n"
                  "Note: turn Shards and Treasure OFF. The HUD shows the "
                  "animation countdown.",
    "GEODE_DIGS_TO_FILL": "**How many taps fill the pan** on this build, waiting out "
                          "each slow animation.\n"
                          "when: 0 = AUTO, keep digging until the bar actually reads "
                          "full (recommended).\n"
                          "raise: only set a number if the bar reads unreliably on your "
                          "build.\n"
                          "pairs: Animation delay per dig | Wait for the bar to read "
                          "full first",
    "GEODE_DIG_MS": "**The quick tap length** (5 to 10ms). It only needs to "
                    "register; Animation delay per dig does the waiting.\n"
                    "raise: geode digs are {{not registering}} at all.\n"
                    "pairs: Animation delay per dig",
    "GEODE_DELAY_MS": "**The big one**: how long to wait for the slow geode fill "
                      "after each tap before deciding it failed. Ends early the "
                      "moment the bar moves.\n"
                      "raise: set it as high as your slowest geode animation (1500 "
                      "to 3000ms); this is what stops the false nudges.\n"
                      "fixes: {{nudging mid-animation, ruined fills}}\n"
                      "pairs: Wait for the dig to start",
    "GEODE_START_MS": "**Dig-start proof window**: after clicking, how long to wait "
                      "for the dig to actually start (green bar or capacity "
                      "movement) before re-clicking. The animation timer only "
                      "begins once a dig is confirmed running.\n"
                      "raise: your game is {{laggy to start digs}}.\n"
                      "pairs: Animation delay per dig",
    "GEODE_CONFIRM_FULL": "**Double-check the fill.** After the set number of digs, "
                          "also wait for the bar to actually read FULL before walking "
                          "back.\n"
                          "when: your bar reads reliably and an early shake costs more "
                          "than a moment of waiting.\n"
                          "Note: leave OFF to trust the dig count when the bar is very "
                          "laggy.",
    "GEODE_SHAKE_HOLD_MS": "**The geode shake cap.** The shovel slows the SHAKE too, so "
                           "this is the max time to keep shaking; it still stops the "
                           "instant the pan is empty.\n"
                           "raise: shakes are {{cut short with capacity left}} on slow "
                           "gear.\n"
                           "pairs: Exact shake clicks\n"
                           "Note: the bail-if-still-full check is off in geode mode, so "
                           "a slow shake is never abandoned.",
    "TREASURE_MODE": "**The Rubble Creek two-step.** No shaking: stand on the "
                     "deposit, it digs, strafes across to the sands, digs there, "
                     "and strafes back, over and over.\n"
                     "steps: stand on the Rubble Creek deposit | calibrate Collect "
                     "Deposit on the Collect prompt | turn this on and Start\n"
                     "fixes: {{shake loop useless on the two-spot farm}}\n"
                     "pairs: Delay between dig clicks | Max strafe before moving "
                     "on\n"
                     "Note: turn Shards and Geodes OFF when using this.",
    "TREASURE_DIGS": "**Digs at each spot before strafing** to the other one. "
                     "Usually 1: dig once and move on.\n"
                     "raise: a single dig does not finish before it starts moving.\n"
                     "pairs: Quick dig click | Delay between dig clicks",
    "TREASURE_DIG_MS": "**The quick dig click length** in Treasure mode (5 to 10ms). "
                       "It only needs to register; the dig animation does the rest.\n"
                       "raise: treasure digs are {{not registering}} at all.\n"
                       "pairs: Delay between dig clicks",
    "TREASURE_DIG_GAP_MS": "**The wait between dig clicks** while the slow chest-style "
                           "animation plays out, so clicks do not stack.\n"
                           "raise: digs overlap on slow gear (about 12000ms is a good "
                           "start).\n"
                           "lower: fast gear wastes time waiting.\n"
                           "pairs: Digs per spot before strafing",
    "TREASURE_MOVE_MAX_MS": "**The strafe cap**: stop strafing after this long even if "
                            "the Collect cue never appears, so it cannot run sideways "
                            "forever.\n"
                            "raise: the two spots are far apart and it {{gives up before "
                            "arriving}}.\n"
                            "lower: a missed cue wastes less walking.",
    "EASY_WATER_BACK_MS": "**Plain-language tweak**: go this much further back into the "
                          "water before shaking. It quietly adds to the real walk-back "
                          "timings for you.\n"
                          "raise: you stop short of deep enough water and the shake "
                          "clips the shore.\n"
                          "lower: you overshoot and waste time walking back out.\n"
                          "pairs: Max S walk-back | Extra S after Pan cue\n"
                          "Note: 0 = no change.",
    "EASY_LAND_FWD_MS": "**Plain-language tweak**: go this much further onto land "
                        "after a shake. It adds to the underlying landing timings.\n"
                        "raise: you land a touch short and need nudges to reach "
                        "diggable ground.\n"
                        "fixes: {{lands short of the dig strip}}\n"
                        "pairs: Walk forward before shaking | Land assist: confirm "
                        "Deposit cue before probing\n"
                        "Note: if you land fully IN the water, fix the momentum first "
                        "(Walk forward before shaking), not this. 0 = no change.",
    "EASY_SHAKE_DELAY_MS": "**Plain-language tweak**: wait this much longer before the "
                           "shake begins, giving momentum time to build or the animation "
                           "a beat to be ready.\n"
                           "raise: the shake fires before you are set.\n"
                           "pairs: Delay before shake starts | Walk forward before "
                           "shaking\n"
                           "Note: 0 = no change.",
    "EASY_FIRST_DIG_DELAY_MS": "**Plain-language tweak**: wait this much longer before the "
                               "first dig after a shake, so you are settled on land before "
                               "digging.\n"
                               "raise: the first dig of a pan often misses because you are "
                               "still moving.\n"
                               "fixes: {{first dig never registers}}\n"
                               "pairs: Settle before first dig after shake\n"
                               "Note: 0 = no change.",
    "EASY_WATER_RETURN_DELAY_MS": "**Plain-language tweak**: pause this long after the pan "
                                  "fills before heading back to the water.\n"
                                  "raise: the fill animation needs a beat to finish before you "
                                  "turn around.\n"
                                  "Note: most builds leave it at 0.",
    "PERFECT": "**Release exactly on green.** The dig releases when the "
               "skill bar hits its green zone instead of holding for a fixed "
               "time.\n"
               "when: you have the Green dig pixel calibrated and a build "
               "where timing wins.\n"
               "lower: turn it OFF if digs {{miss at high dig speed}}; the "
               "bar is usually too fast to catch by pixel, and a timed hold "
               "is steadier.\n"
               "pairs: Dig hold length | Dig speed\n"
               "Note: most builds should leave this off.",
    "DIG_CLICK_MS": "**The core dig knob**: how long each dig holds the mouse "
                    "down.\n"
                    "raise: digs are {{not registering}}.\n"
                    "lower: every dig lands and you want the time back.\n"
                    "fixes: {{missed digs}} or {{wasted hold time}}\n"
                    "pairs: Dig speed\n"
                    "Note: auto-fills from Dig speed; set it by hand only for odd "
                    "builds.",
    "DIG_SPEED": "**Your dig-speed stat as a percent**, used to derive the "
                 "hold: hold = 55000 / speed, so 100% gives 550ms and 200% "
                 "gives 275ms.\n"
                 "when: set it to match the dig speed on your in-game stat "
                 "panel and the hold takes care of itself.\n"
                 "fixes: {{digs missing or doubling}} because the hold does "
                 "not match your stat\n"
                 "pairs: Dig hold length",
    "MAX_DIGS_TO_FILL": "**The dig budget per pan.** It watches the bar and stops at "
                        "full; this only caps how many digs it will try.\n"
                        "raise: it {{gives up before the pan is full}} on a multi-dig "
                        "build.\n"
                        "lower: a tighter cap fails faster when something is wrong.\n"
                        "pairs: Wait for FULL after each dig | Smart fill wait",
    "DIG_FILL_MS": "**The wait for FULL after each dig.** The fill bar animates, "
                   "so waiting too little re-digs mid-animation and wastes a "
                   "dig.\n"
                   "raise: it {{re-digs while the bar is still rising}}.\n"
                   "lower: pans stall too long between digs.\n"
                   "pairs: Smart fill wait\n"
                   "Note: Smart fill wait handles this automatically and is the "
                   "better fix.",
    "DIG_PIPELINE": "**Rhythm-fire digs.** Learns how many digs fill your pan "
                    "(first 3 fills run normally), then fires the follow-ups on "
                    "your dig-animation rhythm, checking the bar only after the "
                    "last.\n"
                    "when: multi-dig builds chasing maximum speed.\n"
                    "fixes: {{dead time between every dig}}\n"
                    "pairs: Pipeline rhythm | Dig speed\n"
                    "Note: falls back and relearns if a fill comes up short. "
                    "Unreliable on lag.",
    "DIG_PIPELINE_GAP_MS": "**The pipeline rhythm** between fire-and-forget digs.\n"
                           "raise: pipelined digs are being {{skipped or dropped}} by "
                           "the game.\n"
                           "lower: squeeze out more speed on a dialed build.\n"
                           "pairs: Pipelined digs\n"
                           "Note: 0 = derived from your Dig speed (190000 / speed + "
                           "25ms).",
    "DIG_FILL_SMART": "**Watch the bar, not the clock.** Keeps waiting while the "
                      "bar is still rising and digs again the instant it stops "
                      "short of full.\n"
                      "when: good for every build; it kills the dead time a fixed "
                      "wait leaves.\n"
                      "fixes: {{re-digs mid-animation}} and {{wasted wait after "
                      "short digs}}\n"
                      "pairs: Smart: bar still this long = dig again | Smart: max "
                      "wait per dig",
    "DIG_PLATEAU_MS": "**Smart fill's re-dig trigger**: how long the bar must sit "
                      "unchanged, below full, before that dig is judged not enough.\n"
                      "raise: it {{re-digs while the fill is still climbing}}.\n"
                      "lower: it dawdles after short digs.\n"
                      "pairs: Smart fill wait | Smart: max wait per dig",
    "DIG_SMART_CAP_MS": "**Smart fill's hard cap**: the longest it waits on one dig "
                        "before moving on, a safety net for a misbehaving bar.\n"
                        "raise: keep it comfortably above your bar's normal fill "
                        "animation.\n"
                        "pairs: Smart fill wait",
    "PRE_DIG_SETTLE_MS": "**The landing breath**: a tiny pause after landing before "
                         "the first dig, so it does not fire mid-glide and miss.\n"
                         "raise: the first dig after a shake often {{does not "
                         "register}}.\n"
                         "lower: keep it small; it adds to every pan.\n"
                         "pairs: Wait longer before the first dig",
    "PAN_BACK_MAX_MS": "**The walk-back cap.** It normally stops the instant the Pan "
                       "cue shows; this just keeps it from reversing forever if the "
                       "cue never appears.\n"
                       "raise: your spot needs a long walk to reach water.\n"
                       "fixes: {{endless backwards walk}} on a missed cue\n"
                       "pairs: Extra S after Pan cue",
    "WATER_EXTRA_BACK_MS": "**Go deeper.** Keep holding S this long AFTER the Pan cue "
                           "shows.\n"
                           "raise: the shake starts right at the edge and {{clips the "
                           "shore}}.\n"
                           "lower: you are wasting walk time in deep water.\n"
                           "pairs: Go further back into the water\n"
                           "Note: if you land IN the water after shakes, the fix is Walk "
                           "forward before shaking, not this.",
    "SHAKE_MOMENTUM_W": "**The heart of the loop.** Holds W while shaking so built-up "
                        "speed carries you from the water onto land as the pan "
                        "drains.\n"
                        "when: leave ON for every normal build.\n"
                        "fixes: {{stranded in the water when the pan empties}}\n"
                        "pairs: Walk forward before shaking | Delay before shake "
                        "starts",
    "SHAKE_CLICKS": "**Fixed click count.** Do exactly this many shake clicks "
                    "then stop; 0 = auto, shake until the bar reads empty.\n"
                    "when: an extra auto-click {{bleeds into the next dig}} and "
                    "ruins perfect digs; set the number your build needs.\n"
                    "lower: back to 0 for slow-shake gear and most builds.\n"
                    "pairs: Each shake click length | Pan-empty threshold",
    "SHAKE_CLICK_MS": "**Each click's hold time.** The shake is a rapid click "
                      "stream (a held press is dropped on macOS).\n"
                      "raise: shakes register weakly and the pan {{drains slower "
                      "than it should}}.\n"
                      "lower: a faster rattle once every click lands.\n"
                      "pairs: Gap between shake clicks",
    "SHAKE_CLICK_GAP_MS": "**The gap between shake clicks.** Lower means a faster "
                          "rattle and quicker drain.\n"
                          "raise: clicks are being {{dropped by the game}}.\n"
                          "lower: more clicks a second on a build that keeps up.\n"
                          "pairs: Each shake click length",
    "SHAKE_HOLD_MS": "**The shake time limit.** It stops early the instant the pan "
                     "reads empty; this only matters as a cap.\n"
                     "raise: slow-shake gear gets {{cut off with capacity still "
                     "showing}}.\n"
                     "lower: a tighter cap fails faster on a broken shake.\n"
                     "pairs: Exact shake clicks | Pan-empty threshold",
    "CAP_EMPTY_FRAC": "**How empty counts as empty** (0.04 = 4% of the bar left).\n"
                      "lower: shakes end with {{dirt still in the pan}}; try 0.02.\n"
                      "raise: it keeps rattling an already-empty pan.\n"
                      "fixes: {{pans coming back part-full}}\n"
                      "Tip: if it empties too early every time, re-calibrate the "
                      "bar's LEFT end; that is the real fix.",
    "SHAKE_BAIL_MS": "**Shake-failed detection.** If the pan is STILL completely "
                     "full after this long, the shake never started; give up and "
                     "retry instead of clicking at nothing.\n"
                     "raise: real shakes need longer before the drain shows.\n"
                     "lower: dead shakes are detected sooner.\n"
                     "pairs: Confirm shake started within | Deeper-tap retries "
                     "when it won't start",
    "SHAKE_START_CONFIRM_MS": "**The fast save** (0 = off). If the pan is still completely "
                              "full this long after clicking starts, the shake never "
                              "initiated; tap S deeper and keep clicking instead of losing "
                              "the cycle.\n"
                              "when: edge clicks occasionally {{fail to start the shake}}; "
                              "try 300.\n"
                              "pairs: Deeper-tap retries when it won't start | Shake-failed "
                              "detection\n"
                              "Note: keep it below Shake-failed detection.",
    "SHAKE_START_RETRIES": "**Deeper-tap retries per shake** before giving up and "
                           "letting the normal bail handle it. Each retry taps S a "
                           "little deeper and tries again.\n"
                           "raise: shakes often need a couple of tries on your spot.\n"
                           "pairs: Retry deeper S tap | Confirm shake started within",
    "SHAKE_RETRY_DEEPER_MS": "**The retry tap.** How long the S tap is on each shake-start "
                             "retry; bigger walks you deeper into the water before trying "
                             "again.\n"
                             "raise: retries {{still start on the shore}}.\n"
                             "pairs: Deeper-tap retries when it won't start",
    "SHAKE_STALL_MS": "**Drain fail-fast** (0 = off). If a STARTED shake's bar "
                      "freezes this long mid-drain, the game dropped the shake; end "
                      "the attempt now instead of clicking out the timeout.\n"
                      "when: dropped shakes are {{wasting whole timeouts}}; try "
                      "600.\n"
                      "Note: keep it well above one drain tick so slow builds "
                      "cannot false-trigger.",
    "SHAKE_W_LEAD_MS": "**The momentum pre-roll.** W is held this long before the "
                       "first shake click, so built-up speed carries you onto land "
                       "while the pan drains.\n"
                       "raise: you keep {{landing in the water}} after shakes; the "
                       "shake is starting too early, and this is the number one "
                       "cause.\n"
                       "lower: the shake click lands on the shore instead (start "
                       "retries in the trace).\n"
                       "fixes: {{it never makes it back to land}}\n"
                       "pairs: Delay before shake starts | Hold W during shake\n"
                       "Note: needs Hold W during shake ON. 0 = off.",
    "SHAKE_START_DELAY_MS": "**Pause before the shake starts**, after reaching the water. "
                            "Works with Walk forward before shaking: both delay the shake "
                            "so momentum can build.\n"
                            "raise: the shake fires the instant you touch water and you "
                            "{{fall short of land}}.\n"
                            "pairs: Walk forward before shaking | Wait longer before the "
                            "shake starts",
    "POST_SHAKE_SETTLE_MS": "**The landing settle.** Pause after the pan reads empty so "
                            "momentum can put you on land before the dig-probe runs.\n"
                            "raise: the first probe fires while you are {{still sliding}} "
                            "and misses.\n"
                            "lower: it adds dead time to every pan.\n"
                            "pairs: Settle before first dig after shake",
    "LAND_CUE_ASSIST": "**Probe from the dirt, not blind.** Before the first probe "
                       "dig, if Collect Deposit is not on screen yet, hold W briefly "
                       "until it shows.\n"
                       "when: short landings cost you {{wasted nudges}} after every "
                       "shake.\n"
                       "pairs: Land assist W budget | Max W to find land cue\n"
                       "Note: tidies up AFTER the shake; if you land in the water, "
                       "fix Walk forward before shaking first.",
    "LAND_ASSIST_MAX_MS": "**The assist budget**: how long Land assist may hold W "
                          "waiting for the cue before the normal probe takes over.\n"
                          "raise: the cue tends to appear a beat late on your spot.\n"
                          "lower: keep it short; it is a helper, not a walk.\n"
                          "pairs: Land assist: confirm Deposit cue before probing",
    "DEPOSIT_MAX_MS": "**The land hunt cap**: the forward W walk while looking for "
                      "the land cue after a shake.\n"
                      "raise: wide beaches need a longer hunt.\n"
                      "lower: give up faster and let probing handle it.\n"
                      "pairs: Walk forward before shaking | Land assist: confirm "
                      "Deposit cue before probing\n"
                      "Note: a safety net for a short landing, not the cure; the "
                      "cure is momentum.",
    "LAND_SETTLE_MS": "**Sit firmly on the dirt.** Keep holding W this long after "
                      "the land cue shows, preventing a land-water flicker where "
                      "the cue blinks off again.\n"
                      "raise: the Collect Deposit cue {{flickers}} right after you "
                      "arrive.\n"
                      "lower: keep it small; it runs every pan.\n"
                      "pairs: Max W to find land cue",
    "DIG_PROBE_MS": "**Probe patience**: how long to wait for a probe dig to "
                    "register before calling it a miss and nudging forward.\n"
                    "raise: good probes are counted as {{misses}}, causing "
                    "needless nudges.\n"
                    "lower: real misses are detected sooner.\n"
                    "pairs: Settle between probe digs | Probe digs before giving "
                    "up",
    "PROBE_GAP_MS": "**Settle between probes**: the pause after a forward nudge "
                    "before the next probe dig, so the character has actually "
                    "stopped moving.\n"
                    "raise: probes fire {{mid-nudge}} and miss.\n"
                    "pairs: Forward W nudge between probe digs",
    "LAND_PROBE_NUDGE_MS": "**The hunt step**: how far forward to nudge between probe "
                           "digs while looking for diggable ground.\n"
                           "raise: bigger steps search a wide beach faster.\n"
                           "lower: small steps cannot {{overshoot the dig strip}}.\n"
                           "pairs: Probe digs before giving up",
    "LAND_DIG_TRIES": "**The give-up count**: nudge-and-probe rounds before it "
                      "concludes it cannot find land and safe-stops.\n"
                      "raise: wide beaches need more hunting.\n"
                      "lower: fail faster when something is truly wrong.\n"
                      "fixes: {{endless wandering}} when land is unreachable\n"
                      "Note: if this trips a lot, the real cause is landing short; "
                      "fix Walk forward before shaking.",
    "STUCK_TICKS": "**The stuck detector**: this many identical screen reads in "
                   "a row counts as stuck and starts the recovery ladder.\n"
                   "raise: slow builds {{false-trigger}} recovery mid-animation.\n"
                   "lower: react to real wedges faster.\n"
                   "pairs: Recoveries before break-out",
    "RECOVER_LIMIT": "**Nudge patience**: how many gentle recoveries on the same "
                     "spot before escalating to the break-out.\n"
                     "raise: nudges usually do free your spot eventually.\n"
                     "lower: escalate sooner when nudging clearly fails.\n"
                     "pairs: Enable stuck-recovery | Enable break-out",
    "RECOVER_BACK_MS": "**The nudge budget**: movement per recovery nudge, made of "
                       "pulsed taps.\n"
                       "raise: harder wedges need bigger wiggles.\n"
                       "lower: big nudges {{drift you off your spot}}.\n"
                       "pairs: Tap hold per pulse | Tap release per pulse",
    "NO_PROGRESS_SEC": "**The watchdog.** If nothing completes for this many seconds "
                       "(no pan emptied, no dig registered), it forces a "
                       "click-to-empty to shake the state loose.\n"
                       "raise: genuinely slow cycles are being {{interrupted}}.\n"
                       "lower: react faster when the loop is silently wedged.\n"
                       "fixes: {{walking back and forth thinking all is fine}}",
    "SHAKE_GLITCH_LIMIT": "**The quick fix trigger**: after this many failed shakes it "
                          "immediately does a click-to-empty, which usually clears the "
                          "shake glitch cheaply.\n"
                          "lower: react to the glitch faster.\n"
                          "pairs: Failed shakes before STOP",
    "SHAKE_FAIL_LIMIT": "**The shake give-up line**: failed shakes tolerated before a "
                        "safe stop. Earlier rungs usually clear a glitch well before "
                        "this counts up.\n"
                        "raise: be more forgiving on a flaky spot.\n"
                        "Note: if this trips often, the fix is in the Shake stage or "
                        "calibration, not here.",
    "BREAKOUT_LIMIT": "**Break-out patience**: attempts before it gives up and "
                      "safe-stops. If repositioning twice did not free it, "
                      "something real is wrong and pausing is safer.\n"
                      "raise: your spot legitimately needs several tries.\n"
                      "pairs: Enable break-out",
    "BREAKOUT_SHAKE_MS": "**Finish the stuck shake**: during a break-out, click this "
                         "long to complete a shake that is locking your movement, so "
                         "the reposition can actually move you.\n"
                         "raise: break-outs are {{not freeing you}} because the shake "
                         "still locks movement.\n"
                         "pairs: Break-out reposition W",
    "BREAKOUT_REPOS_MS": "**The escape move**: the forward W hold that changes your "
                         "position and breaks the loop.\n"
                         "raise: reposition further to escape harder wedges.\n"
                         "lower: stay closer to your dig spot.\n"
                         "pairs: Break-out click-to-finish",
    "BURST_ON_MS": "**Recovery tap, on half**: how long each jitter tap holds "
                   "the key. Only used while recovering, never in the normal "
                   "loop.\n"
                   "raise: taps are too weak to {{wiggle you free}}.\n"
                   "pairs: Tap release per pulse",
    "BURST_OFF_MS": "**Recovery tap, off half**: how long each tap releases "
                    "before the next check. The on/off pulse is what does the "
                    "wiggling.\n"
                    "raise: jitter is too violent for your spot.\n"
                    "pairs: Tap hold per pulse",
    "NOTIFY_SCREENSHOT": "**A picture with the alert.** Attaches a screenshot to "
                         "safe-stop, recovery, hard-stop and stats DMs so you can see "
                         "what the game looked like.\n"
                         "when: diagnosing stops remotely without walking to the "
                         "computer.\n"
                         "Note: turn off for text-only alerts.",
    "WEBHOOK_ENABLED": "**Discord DMs from the Prospectors bot** for start, stop, "
                       "safe-stop, auto-stop, bag-full and periodic stats.\n"
                       "steps: join the Discord server | enter your Discord username "
                       "below | press Send test notification\n"
                       "when: any run you plan to walk away from.\n"
                       "pairs: Your Discord username | Notify: safe-stop\n"
                       "Note: you must share a server with the bot and have DMs "
                       "open.",
    "WEBHOOK_USER": "**Who the bot DMs.** Your exact Discord username; the bot "
                    "resolves it and sends the alerts there.\n"
                    "fixes: {{test says sent but nothing arrives}}, usually this "
                    "or your DM privacy\n"
                    "pairs: DM me on Discord\n"
                    "Note: you must be in the server with DMs open.",
    "WEBHOOK_STATS_MIN": "**The stats pulse**: how often to DM a stats update while "
                         "running. Sixty gives an hourly read on pans, rate and "
                         "recoveries.\n"
                         "raise: fewer pings on very long runs.\n"
                         "lower: a closer watch on an experimental build.\n"
                         "pairs: Notify: periodic stats\n"
                         "Note: 0 = off, event alerts still send.",
    "NOTIFY_START": "**Ping on session start** (Ctrl+K or Start). Confirmation "
                    "that an unattended run actually kicked off.\n"
                    "when: you start runs remotely or from hotkeys and want the "
                    "receipt.",
    "NOTIFY_STOP": "**Ping on any stop**: manual, the auto-stop timer, or the "
                   "bag-full guard.\n"
                   "when: leave on; this is the DM that tells you a run has "
                   "ended.\n"
                   "pairs: Auto-stop after a set time | Stop after N pans",
    "NOTIFY_STATS": "**The periodic stats DM** (pans, pans/hr, recoveries), on "
                    "your stats interval.\n"
                    "when: checking a long run from your phone.\n"
                    "pairs: Stats DM every N min",
    "NOTIFY_SAFE_STOP": "**The important one.** Pings when the macro pauses on "
                        "trouble and starts retrying, not after the run is dead.\n"
                        "when: every AFK run; it is your early warning.\n"
                        "pairs: Safe-stop = pause and retry\n"
                        "Note: the message names the reason and the retry count.",
    "NOTIFY_RECOVERIES": "**Ping every recovery.** Off by default because a busy spot "
                         "can make this {{spammy}} fast.\n"
                         "when: only while actively debugging how often a build "
                         "wedges.",
    "NOTIFY_ERRORS": "**Ping on unexpected errors** that stop the macro, beyond "
                     "the normal recovery path.\n"
                     "when: leave on; it is the alert of last resort.",
    "RECOVER_ENABLED": "**Rung 2 of the ladder: nudges.** Small pulsed movements "
                       "that wiggle you free when the same screen keeps repeating.\n"
                       "when: leave ON; without it a single wedge idles the run "
                       "until the safe stop.\n"
                       "fixes: {{stuck against a rock forever}}\n"
                       "pairs: Stuck reads before recovery | Recovery nudge budget",
    "SHAKE_RETRY_ENABLED": "**Rung 3: shake re-attempts.** A shake that did not register "
                           "gets tried again, so one glitched shake costs a retry, not a "
                           "cycle.\n"
                           "when: leave ON unless the re-shake itself misbehaves on your "
                           "build.\n"
                           "fixes: {{one glitchy shake ending the run}}\n"
                           "pairs: Failed shakes before quick click-to-empty | Failed "
                           "shakes before STOP",
    "BREAKOUT_ENABLED": "**Rung 4: the break-out.** Finishes a stuck shake with a "
                        "click burst, then repositions to break the loop; this frees "
                        "a wedge that nudges cannot.\n"
                        "when: leave ON so recovery has a real escape hatch.\n"
                        "pairs: Break-out click-to-finish | Break-out reposition W | "
                        "Break-outs before STOP",
    "SAFE_STOP_RETRY": "**Rung 6: pause, do not quit.** When it truly cannot "
                       "proceed, it pauses and retries instead of stopping for good, "
                       "so an AFK run heals itself.\n"
                       "when: keep ON for every overnight run.\n"
                       "fixes: {{run over because of one transient snag}}\n"
                       "pairs: Wait before each retry | Hard-stop after this many "
                       "failed retries\n"
                       "Note: with notifications on you get a DM at every pause.",
    "SAFE_STOP_RETRY_SEC": "**The breather between retries.** A minute gives whatever "
                           "blocked you (a player, a hiccup) time to clear.\n"
                           "raise: blockers on your spot need longer to leave.\n"
                           "lower: momentary stops resume sooner.\n"
                           "pairs: Hard-stop after this many failed retries",
    "SAFE_STOP_MAX_RETRIES": "**The final line**: failed retries in a row before it "
                             "hard-stops for real. The counter resets whenever a retry "
                             "actually gets going.\n"
                             "raise: be more patient on a flaky spot.\n"
                             "lower: give up sooner.\n"
                             "pairs: Safe-stop = pause and retry",
    "AUTOSTOP_ENABLED": "**Stop on a timer.** Ends the run after a set number of "
                        "minutes; the clean cap for overnight sessions.\n"
                        "when: your inventory or buffs have a known lifespan.\n"
                        "pairs: Stop after this many minutes | Notify: stopped",
    "AUTOSTOP_MINUTES": "**The timer length.** How many minutes to run before the "
                        "auto-stop fires.\n"
                        "when: set it to how long you will be away or how long the "
                        "bag lasts.\n"
                        "pairs: Auto-stop after a set time",
    "STOP_AFTER_PANS": "**The bag guard.** Stops after this many pans so it does not "
                       "keep panning into a full inventory.\n"
                       "when: set it near the pans your backpack holds.\n"
                       "fixes: {{hours of panning into a full bag}}\n"
                       "Note: 0 = off.",
    "WINDOW_RELATIVE": "**Calibration follows the window.** If you move the Roblox "
                       "window, every calibrated pixel shifts to match, no "
                       "recalibration needed.\n"
                       "when: you move the game window around between sessions.\n"
                       "Note: re-calibrate once to set the reference. Off = absolute "
                       "screen coordinates.",
    "SMART_TIMING": "**Trial-and-error auto-tuning.** When misses climb, it "
                    "nudges a timing and keeps the change only if the miss rate "
                    "drops.\n"
                    "when: experimental; watch the log while it learns.\n"
                    "lower: turn it off if results wander; hand tuning with the "
                    "Coach is steadier.\n"
                    "pairs: Adjust when miss rate exceeds",
    "ADAPT_MISS_PCT": "**The auto-tuner's trigger**: it only starts adjusting once "
                      "the recent miss rate is above this percentage.\n"
                      "raise: keep it hands-off unless things get bad.\n"
                      "lower: let it chase smaller problems.\n"
                      "pairs: Auto-tune timing by trial and error",
    "FR_RECOVERY": "**Fast-travel home after a soft stop** (Fortune River). "
                   "Presses 4, opens Fast Travel, finds the pink row, clicks it, "
                   "then walks back into the water and resumes.\n"
                   "steps: calibrate the FR spots on the Calibrate page | test "
                   "with Ctrl+J (manual soft-stop)\n"
                   "when: you farm Fortune River and want dead ends to heal "
                   "themselves.\n"
                   "pairs: FR: colour match tolerance | Starfall River recovery\n"
                   "Note: map-specific and advanced; off unless you know the "
                   "map.",
    "FR_TEXT_TOL": "**Pink match tolerance** for the Fortune River row, per "
                   "channel.\n"
                   "raise: recovery {{never finds the row}} (lighting drift).\n"
                   "lower: it clicks the wrong row.\n"
                   "pairs: Fortune River recovery",
    "FR_MOVE_STEP": "**Cursor step size** during the list sweep. Smaller is "
                    "smoother and less affected by mouse acceleration; larger "
                    "scans faster.\n"
                    "raise: the sweep is too slow.\n"
                    "lower: the cursor {{skips past the row}}.",
    "FR_SCAN_HOVER_MS": "**Dwell per sweep step** while hunting the pink row down the "
                        "list column.\n"
                        "raise: a steadier sweep that cannot blow past the row.\n"
                        "lower: a faster scan.",
    "FR_FIND_TRIES": "**Full sweeps before re-opening**: top-to-bottom passes, "
                     "scrolling between each, before it re-opens the warp device.\n"
                     "raise: the row is often {{just past where it stops "
                     "looking}}.",
    "FR_OPEN_TRIES": "**Re-open attempts.** If a full search finds nothing, the "
                     "device probably never opened; this is how many times to "
                     "re-equip slot 4 and retry.\n"
                     "raise: the device only opens {{some of the time}} on your "
                     "machine.",
    "FR_SCROLL_STEPS": "**Wheel notches per scroll** when the row is not visible in "
                       "the list box.\n"
                       "raise: cover the list faster.\n"
                       "lower: smaller jumps that cannot {{scroll the row past the "
                       "view}}.",
    "FR_DOUBLE_GAP_MS": "**The double-click gap** that opens Fast Travel after "
                        "switching to hotkey 4.\n"
                        "lower: the double-click is {{not registering}} as one.\n"
                        "raise: the game needs more time between the two clicks.",
    "FR_OPEN_MS": "**Menu wait**: how long to wait for Fast Travel to appear "
                  "after the double-tap before sweeping.\n"
                  "raise: the menu opens slowly and the sweep {{starts before "
                  "the list exists}}.",
    "FR_CLICK_SETTLE_MS": "**Hover and click settle**: the pause around each cursor "
                          "move and click so the game registers them.\n"
                          "raise: recovery clicks are {{not landing}}.",
    "FR_ACTION_GAP_MS": "**The step spacer**: a pause between every stage of the "
                        "recovery (keys, menu, clicks, returning).\n"
                        "raise: steps happen {{too fast for the game}} and get "
                        "dropped.",
    "FR_WARP_MS": "**Load wait** after clicking Fortune River, for the teleport "
                  "and world to finish loading.\n"
                  "raise: slow loading means it {{starts walking before the "
                  "world exists}}.",
    "FR_STRAFE_MS": "**The line-up tap**: a tiny D strafe after returning to the "
                    "pan.\n"
                    "when: adjust only if you end up slightly off your dig spot "
                    "after recoveries.",
    "FR_WALK_MAX_MS": "**The walk cap** back to the water after a warp, so it "
                      "cannot walk off into nowhere.\n"
                      "raise: your spot is genuinely far from the warp point.",
    "FR_END_A_MS": "**Final alignment**: hold A this long once the land cue "
                   "shows, then restart the loop.\n"
                   "when: 0 skips it if your spot needs no line-up.",
    "SR_RECOVERY": "**Fast-travel home after a soft stop** (Starfall River). "
                   "Shares the Fast Travel calibration with Fortune River, with "
                   "Starfall's own row colour; after the warp it holds A to the "
                   "water, then S to the Pan cue.\n"
                   "when: you farm Starfall River spots.\n"
                   "pairs: SR: colour match tolerance | Fortune River recovery\n"
                   "Note: if both SR and FR are on, Starfall wins. Advanced; off "
                   "unless you know the map.",
    "SR_TEXT_TOL": "**Starfall row match tolerance**, per channel.\n"
                   "raise: recovery {{never finds the row}}.\n"
                   "lower: it clicks the wrong one.",
    "SR_A_MAX_MS": "**The strafe cap**: after the warp it holds A until the Pan "
                   "cue, measuring the time; past this cap the recovery reports "
                   "failure instead of strafing forever.\n"
                   "raise: the water is genuinely far from the warp point.",
    "SR_D_PCT": "**The centring move**: after the timed A finds water, hold D "
                "for this percent of that time to centre on the strip.\n"
                "raise: you end up {{off-centre}} after recoveries.\n"
                "Note: 50 = half the measured time.",
    "SR_S_MAX_MS": "**The walk-in cap** for the final S walk to the Pan cue; "
                   "past it the recovery reports failure instead of wandering.\n"
                   "raise: your spot needs a long walk into the water.",
    "FR_CROSS_CONFIRM": "**Commitment check**: cue reads in a row needed to count as "
                        "truly in the water, then truly on the far shore.\n"
                        "raise: it {{restarts on the shore it spawned on}} instead of "
                        "crossing.\n"
                        "lower: it commits faster.",
    "X_PATTERN": "**Diagonal walk-backs.** Enter the water on alternating 45 "
                 "degree diagonals so each pan covers new ground; forward to "
                 "land stays straight.\n"
                 "when: you keep {{falling short on a straight line}} or want "
                 "to spread digs.\n"
                 "pairs: X: diagonal length per pass | X: drift before "
                 "auto-recenter",
    "X_STRAFE_MS": "**The diagonal length** per pass before the walk finishes "
                   "straight back.\n"
                   "raise: cover more new ground sideways.\n"
                   "lower: straighter and tighter, more consistent depth and "
                   "landing.\n"
                   "Note: 0 = the old behaviour, diagonal the whole way.",
    "X_RECENTER_MS": "**The drift limit.** Once sideways travel adds up to this "
                     "much, it strafes back toward the middle before the next "
                     "pass, so it cannot wander off the dig strip.\n"
                     "lower: stay tighter to centre.\n"
                     "Note: 0 = never recenter.",
}

# Deep hover help for everything that is NOT a settings row: buttons, stats,
# calibration rows, the Cycle stages and graphs. Shown in the Preview panel
# when you hover the element. Keys are stable ids the app maps DOM nodes to.
UI_HELP = {
    "runflow": "**The order of events, every pan.** The prompt word says "
               "where you are; the capacity bar says what is in the pan; "
               "every action verifies itself on the next tick.\n"
               "steps: dig until the bar reads full | hold S into the water "
               "until the Pan prompt | hold W, click to start the shake near "
               "the edge | rapid clicks drain it while momentum slides you "
               "ashore | settle, probe dig, then the real digging\n"
               "Note: if anything wedges, recovery escalates gently: nudges, "
               "shake retry, break-out, then a safe stop that pauses and "
               "retries.",
    # --- Run tab -------------------------------------------------------------
    "startbtn": "**Starts the macro.** Press it, then click into Roblox so "
                "the game has focus; the loop begins on its own.\n"
                "steps: calibrate first (red badge = not yet) | pick a preset "
                "or Build | Start, then click into the game\n"
                "fixes: {{nothing happens after starting}}, the game window "
                "never got focus\n"
                "Note: Ctrl+K starts and stops from inside the game; Esc "
                "quits.",
    "pausebtn": "**Pauses mid-cycle, resumes where it left off**, keeping the "
                "session and every stat.\n"
                "when: you need the mouse for a moment or want to check your "
                "inventory.\n"
                "pairs: Relative timers (keep counting while paused)\n"
                "Note: relic timers keep counting while paused if Relative "
                "timers is on.",
    "stopbtn": "**Ends the session for good** and writes it to History with "
               "full stats and the event timeline.\n"
               "when: done farming, or switching builds and starting clean.\n"
               "Note: use Pause instead if you just need a minute; Stop "
               "zeroes the live counters.",
    "pv1": "**The v1 preset: fast single-dig.** One dig fills the pan, "
           "quick shake, quick loop.\n"
           "when: the right start for most fast money builds.\n"
           "steps: load it | calibrate | Start",
    "pv2": "**The v2 preset: multi-dig.** Several digs fill the pan, so "
           "the timings wait for the bar between digs.\n"
           "when: one dig clearly does not fill your pan.\n"
           "pairs: Max digs to fill the pan",
    "pv3": "**The v3 preset: geodes.** Turns on Geode mode with the long "
           "animation waits slow geode shovels need.\n"
           "when: 10% dig-speed geode gear.\n"
           "pairs: Geode mode | Animation delay per dig\n"
           "Note: pair it with the Geode Farm builds on the Builds page.",
    "pdef": "**Resets every setting to defaults** in one click. Builds "
            "and calibration are untouched.\n"
            "when: a build has drifted into a mess and you want a clean "
            "slate.\n"
            "Note: save your current tuning as a Build first if you might "
            "want it back.",
    "st_run": "**Session runtime**, counting from Start (pauses included). "
              "It is the clock behind every per-hour figure.\n"
              "healthy: give it a few minutes before trusting pans/hr or "
              "$/hr.",
    "st_cyc": "**Pans completed**: one pan = dig, walk back, shake, land. "
              "The number everything else is measured against.\n"
              "healthy: climbing steadily.\n"
              "climbing: stopped moving? the macro is stuck; check "
              "recoveries and the log.",
    "st_digs": "**Total digs**, probe digs included.\n"
               "healthy: close to the pan count on a one-dig build.\n"
               "climbing: far more digs than pans means {{probing or "
               "re-digging too much}}.\n"
               "fixpath: Walk forward before shaking | Smart fill wait",
    "st_rate": "**Pans per hour** at the current pace, the headline speed "
               "number.\n"
               "healthy: settles after a few minutes; early swings are "
               "normal.\n"
               "fixpath: Dig hold length | Each shake click length\n"
               "Note: trim the widest blocks on the Cycle timeline to raise "
               "it.",
    "st_clean": "**Clean pan share**: ran start to finish with no nudges, "
                "retries or recoveries.\n"
                "healthy: high on a dialed build.\n"
                "climbing: a falling clean % tells you which counter to read "
                "next: nudges, misses or recoveries.",
    "st_rec": "**Recovery ladder activations**: stuck, then nudged or "
              "broken out.\n"
              "healthy: a few per session.\n"
              "climbing: it keeps {{getting wedged}}; check calibration "
              "first, then the spot.\n"
              "fixpath: Stuck reads before recovery",
    "st_nud": "**Small corrective movements** while it hunts for land after "
              "a shake.\n"
              "healthy: a handful per session; zero on a dialed build.\n"
              "climbing: you are {{landing short or in the water}}. Fix the "
              "momentum first, not the probing.\n"
              "fixpath: Walk forward before shaking | Delay before shake "
              "starts | Land assist: confirm Deposit cue before probing\n"
              "Note: the yellow Cycle badge fires off this counter after 5 "
              "pans.",
    "st_miss": "**Shakes that did not empty the pan.**\n"
               "healthy: the occasional miss is normal.\n"
               "climbing: the pan is {{not emptying}}.\n"
               "fixpath: Exact shake clicks | Each shake click length | "
               "Pan-empty threshold\n"
               "Note: persistent misses often mean the capacity bar ends "
               "need recalibrating.",
    "st_rel": "**Relics placed automatically** this session by your timers.\n"
              "climbing: stuck at zero? check the master switch, the rows, "
              "and the hotbar slots.\n"
              "fixpath: Enable relic timer",
    "st_mph": "**Money per hour**, read off the HUD and credited as gains "
              "only.\n"
              "when: needs earnings tracking on and the Money box drawn; "
              "dash otherwise.\n"
              "healthy: settles after a few minutes.\n"
              "Note: macOS only.",
    "st_sph": "**Shards per hour**, same idea as money per hour.\n"
              "when: needs earnings tracking on and the Shards box drawn.\n"
              "Note: the number to watch on a shard farm.",
    "st_safe": "**Safe stops**: it paused itself and retried instead of "
               "quitting.\n"
               "healthy: a few is fine; a recovered safe stop costs a "
               "minute, not the run.\n"
               "climbing: it keeps hitting something it cannot handle; check "
               "calibration and the spot.\n"
               "pairs: Safe-stop = pause and retry",
    "st_hard": "**Hard stops**: the retries ran out, or something "
               "unrecoverable hit.\n"
               "climbing: a repeating hard stop usually means "
               "{{calibration}}.\n"
               "fixpath: Capacity bar: RIGHT end | Capacity bar: LEFT end\n"
               "Note: the History timeline names the reason for every stop.",
    # --- toolbar -------------------------------------------------------------
    "buildname": "**Name the build you are about to save** with Save build "
                 "next to it. A build is a full snapshot of every setting plus "
                 "relics.\n"
                 "when: name it something you will recognise in a month, like "
                 "Geode 1-tap.",
    "savebuild": "**Snapshot everything into a named build**: all pages plus "
                 "relic rows.\n"
                 "when: before experimenting, and whenever a setup is worth "
                 "keeping.\n"
                 "Note: manage, share and load them on the Builds page.",
    "coachtoggle": "**The Coach**: describe a problem in plain words and it "
                   "proposes exact setting changes you apply with one click.\n"
                   "when: you know the symptom but not the knob.\n"
                   "steps: open it | type what you see, like it lands in the "
                   "water | apply the suggestion | Save settings\n"
                   "Note: opening the Coach hides the Preview panel; closing it "
                   "brings Preview back.",
    "analyticsbtn": "**The Analytics window**: pans/hr, clean %, cycle time, "
                    "digs, earnings and every find with rarity and value, "
                    "trending over time.\n"
                    "when: comparing builds on real numbers instead of feel.\n"
                    "Note: hover any card in it for what that number means.",
    "popout": "**The pop-out pill**: a tiny always-on-top control showing "
              "stage and key stats, with start and pause.\n"
              "when: single monitor, and you want control without the main "
              "window.",
    "hudbtn": "**The HUD overlay**: a draggable card beside the game with "
              "the current stage, live stats and a find ticker.\n"
              "when: watching the macro work without alt-tabbing.",
    "tourbtn": "**The Tutorials menu**: the full tour plus a short "
               "walkthrough for every page, all replayable.\n"
               "when: first setup, or any time a page needs a refresher.",
    "prevtoggle": "**This panel.** Hover a sidebar tab to preview the page; "
                  "hover any setting, button or stat for the full explanation "
                  "and chart.\n"
                  "Note: shares its space with the Coach; closing the Coach "
                  "brings it back.",
    "savebtn": "**Makes your settings stick** across restarts. Edits already "
               "apply live while the app is open.\n"
               "when: after any tuning you want to keep.\n"
               "Note: calibration and builds save separately, on their own "
               "buttons.",
    "navsearch": "**Jump to any setting by name.** Typing filters the sidebar "
                 "to matching settings and pages.\n"
                 "when: you know roughly what you are looking for, like shake "
                 "or land.",
    # --- calibrate page ------------------------------------------------------
    "wizbtn": "**The guided path.** Walks every detection spot in order and "
              "proposes each one on a screenshot overlay; you confirm or "
              "redo.\n"
              "steps: open Roblox with the HUD visible | follow the steps, "
              "bar full for the capacity ends | Test detection, then Save "
              "calibration\n"
              "fixes: {{nothing works because the macro cannot see}}\n"
              "Note: redo it after any window size, resolution or monitor "
              "change.",
    "dettest": "**Live proof the calibration works**: shows what the macro "
               "currently sees, where you stand and how full the pan reads, "
               "in real time.\n"
               "when: right after calibrating, and any time farming acts "
               "blind.\n"
               "fixes: {{farming on a broken calibration}}",
    "earntest": "**Reads the Money and Shards boxes once**, right now, and "
                "shows what the OCR got.\n"
                "when: after drawing those boxes, before trusting the $/hr "
                "figures.\n"
                "fixes: {{blank or wrong totals}}, redraw with room to the "
                "LEFT of the number\n"
                "Note: macOS only.",
    "findtest": "**Reads the Find box once** and lists the text it sees.\n"
                "when: with a find card actually on screen in the game.\n"
                "fixes: {{empty lines}}, widen or re-draw the box to cover "
                "the whole card\n"
                "Note: macOS only.",
    "savepixels": "**Writes the calibration to disk** so it survives restarts. "
                  "Calibration saves separately from settings and builds.\n"
                  "steps: calibrate | Test detection | save\n"
                  "fixes: {{perfect in testing, wrong after relaunch}}, this "
                  "step was skipped",
    "exportcal": "**Back up your calibration to a file**, or move it to "
                 "another computer.\n"
                 "when: same window size and resolution on both machines, or "
                 "it will not line up.\n"
                 "pairs: Import",
    "importcal": "**Load a calibration file** you exported earlier or got from "
                 "someone with your setup.\n"
                 "Note: only trust one made at the same window size and "
                 "resolution; otherwise run Guided calibration instead.",
    "cuewizbtn": "**Capture the exact letter shapes** of Pan, Collect Deposit "
                 "and Shake, one guided step each. After capture, a cue only "
                 "fires on its exact shape.\n"
                 "steps: get the word on screen in game | click the letters to "
                 "include or exclude, green = kept | confirm; a preview lands "
                 "in the gallery below\n"
                 "fixes: {{a player in white tripping your cues}}\n"
                 "Note: re-capture after any window or resolution change.",
    "advcue": "**Spoof-proof detection on or off.** Normal detection checks "
              "a small white box; advanced matching uses your captured "
              "letter shapes instead.\n"
              "when: white-dressed players near your spot cause false cues.\n"
              "pairs: Guided cue capture\n"
              "Note: any cue you have not captured quietly falls back to "
              "the box.",
    "cueonly": "**Masks only, for testing.** Disables the white-box fallback "
               "so you can prove your captured masks fire on their own.\n"
               "when: right after capturing, to verify.\n"
               "Note: leave off for normal runs so uncaptured cues keep "
               "working.",
    "cuethresh": "**How bright counts as white** when matching cue masks.\n"
                 "lower: more forgiving on a dim screen or dark map area.\n"
                 "raise: bright backgrounds are {{sneaking into the match}}.\n"
                 "Note: if cues stop registering after a change, move it back "
                 "toward the default.",
    "cal:CAP_FULL_PIXEL": "**The right tip of the Pan Fill bar**, half of the macro's "
                          "most important measurement.\n"
                          "steps: dig until the bar is completely full, all yellow | "
                          "Calibrate, the red X should sit on the right tip | confirm, "
                          "then do the LEFT end with the bar still full\n"
                          "wrongif: you see {{capacity mis-calibration stops}} or "
                          "phantom shake misses; redo both ends\n"
                          "pairs: Capacity bar: LEFT end | Test detection (live)",
    "cal:CAP_LEFT_PIXEL": "**The left tip of the Pan Fill bar**, set with the bar still "
                          "full. The two tips define the width every fill and drain is "
                          "measured from.\n"
                          "wrongif: shakes {{end early with dirt left}}; this corner "
                          "usually sits short of the true left edge\n"
                          "pairs: Capacity bar: RIGHT end | Pan-empty threshold",
    "cal:DEPOSIT_PIX": "**The on-land anchor**: a pixel on the white Collect Deposit "
                       "prompt.\n"
                       "steps: step onto diggable land so the prompt shows | "
                       "Calibrate and confirm\n"
                       "wrongif: probing and relic placement {{misbehave on land}}\n"
                       "pairs: 'Pan' text | 'Shake' text",
    "cal:PAN_PIX": "**The in-water anchor**: a pixel on the white Pan prompt, "
                   "which only shows in the water. It is how the macro knows it "
                   "walked back far enough to shake.\n"
                   "steps: stand in the water so the word is up | Calibrate and "
                   "confirm\n"
                   "wrongif: shakes {{start too shallow}} or walk-back "
                   "overshoots",
    "cal:SHAKE_PIX": "**The mid-shake anchor**: a pixel on the white Shake prompt.\n"
                     "steps: begin a shake so the word is up | Calibrate and "
                     "confirm\n"
                     "Note: the capacity bar still decides when the pan is truly "
                     "empty; this word can linger, which is exactly why both get "
                     "checked.",
    "cal:DIG_TRIGGER_PIXEL": "**The green dig-bar target**, the little bar that flashes "
                             "green frames before a dig registers.\n"
                             "when: only for Perfect dig or the green-confirm options in "
                             "Shards and Geodes.\n"
                             "pairs: Perfect dig (release on green), off = timed hold | "
                             "Green dig-bar confirms the click\n"
                             "Note: skip it otherwise; it is not part of the normal loop.",
    "cal:MONEY": "**One drag around the money total** in the bottom-right HUD.\n"
                 "steps: press Draw box | drag corner to corner around the "
                 "number | confirm\n"
                 "wrongif: totals read wrong; leave generous room to the LEFT, "
                 "the number {{grows as you earn}}\n"
                 "pairs: Test money/shards read\n"
                 "Note: only needed for earnings tracking. macOS only.",
    "cal:SHARDS": "**One drag around the shard number** next to the crystal "
                  "icon.\n"
                  "steps: press Draw box | drag around the number with room to "
                  "the left | confirm\n"
                  "wrongif: leading digits get {{cut off}} as the count grows\n"
                  "pairs: Test money/shards read\n"
                  "Note: earnings tracking only. macOS only.",
    "cal:FIND": "**One drag over the whole find pop-up area.** Cards stack "
                "vertically and fade.\n"
                "steps: press Draw box | cover several cards tall and the "
                "longest name wide | confirm\n"
                "wrongif: cards are {{missed or truncated}}; the box is too "
                "small\n"
                "pairs: Test find pop-up read\n"
                "Note: finds tracking only. macOS only.",
    "fr:text": "**The Fortune River row.** Click the pink row in the open "
               "Fast Travel list; it fixes the scan column and records the "
               "pink to match.\n"
               "steps: open Fast Travel in game | Calibrate | click the pink "
               "Fortune River text\n"
               "wrongif: recovery {{clicks the wrong row}} or never finds "
               "it; redo this and adjust the tolerance\n"
               "pairs: FR: colour match tolerance",
    "fr:srtext": "**The Starfall River row.** Click it in the travel list; "
                 "saves only its colour. The scan column and list box are "
                 "shared with Fortune River above, so set those first.\n"
                 "pairs: SR: colour match tolerance",
    "fr:apon": "**Auto Pan, ON state.** With the game's Auto Pan turned ON, "
               "click the button; records its position and ON colour.\n"
               "pairs: Auto Pan button, OFF state | Use relic timers during "
               "Tracker\n"
               "Note: Tracker relics use this to verify every toggle.",
    "fr:apoff": "**Auto Pan, OFF state.** Turn Auto Pan OFF and click the "
                "button again; records the OFF colour, the other half of the "
                "check.\n"
                "pairs: Auto Pan button, ON state",
    "fr:top": "**Top of the travel list.** Click just inside the top edge; "
              "with the bottom edge it bounds the sweep that hunts for the "
              "river row.\n"
              "pairs: List - BOTTOM edge",
    "fr:bottom": "**Bottom of the travel list.** Click just inside the bottom "
                 "edge.\n"
                 "pairs: List - TOP edge",
    "fr:open": "**Optional: a click that opens Fast Travel.** Leave unset if "
               "pressing 4 then Shift already opens the menu, which is the "
               "usual case.",
    "fr:home": "**The cursor's home.** With shift-lock off, click where the "
               "cursor rests (screen centre); every recovery mouse move is "
               "measured from here.\n"
               "wrongif: recovery clicks land in the {{wrong place}}; re-set "
               "this first",
    # --- cycle page ----------------------------------------------------------
    "cyc:diagram": "**Your pan loop as a picture**: dig on land, S-walk to "
                   "water, W-glide, the shake, then landing. The numbers under "
                   "the nodes are live.\n"
                   "when: click a node to jump to that stage's settings below.",
    "cyc:graph": "**One pan on a millisecond ruler.** Every block is a phase; "
                 "its width is the time it takes, so the widest blocks are "
                 "where your pans/hr hides.\n"
                 "when: hover a block for the settings behind it; click to "
                 "jump to the exact slider.\n"
                 "Note: drag any slider and the graph redraws live.",
    "stage:dig": "**Dig: fill the pan.** On land with an empty pan, hold the "
                 "click and watch the bar rise.\n"
                 "owns: Dig hold length | Dig speed | Max digs to fill the pan "
                 "| Smart fill wait\n"
                 "fixes: {{digs 2 to 3 times before the bar moves}}, turn on "
                 "Smart fill wait or use Shards mode",
    "stage:swalk": "**Walk back: get into the water.** Holds S until the Pan "
                   "prompt shows.\n"
                   "owns: Max S walk-back | Extra S after Pan cue\n"
                   "fixes: {{shakes too shallow, half on the shore}}, go deeper",
    "stage:glide": "**Glide and start: where landing problems get fixed.** Holds "
                   "W toward land and starts the shake just before the edge, so "
                   "momentum carries you ashore.\n"
                   "owns: Walk forward before shaking | Delay before shake "
                   "starts | Confirm shake started within\n"
                   "fixes: {{keeps landing in the water}}, the number one "
                   "complaint; a shake that starts too early is the cause",
    "stage:shake": "**Shake and drain: empty it.** Rapid clicks drain the pan "
                   "while momentum slides you in.\n"
                   "owns: Exact shake clicks | Each shake click length | Shake "
                   "overall timeout\n"
                   "fixes: {{frequent shake misses}}, lengthen the click, raise "
                   "the timeout on slow gear, check the capacity calibration",
    "stage:land": "**Land and prove: the arrival.** Settle, find the Collect "
                  "Deposit cue, then a small probe dig proves the ground before "
                  "real digging.\n"
                  "owns: Max W to find land cue | Hold W after land cue | Land "
                  "assist: confirm Deposit cue before probing\n"
                  "Note: these tidy up the arrival; landing in the water is "
                  "fixed one stage back, in Glide and start.",
    "stage:safety": "**Safety nets: when something goes wrong.** Stuck detection, "
                    "nudges, shake retries, break-outs, the watchdog, then the "
                    "safe stop that pauses and retries.\n"
                    "owns: Enable stuck-recovery | Enable break-out | Safe-stop = "
                    "pause and retry\n"
                    "Note: on by default; mostly leave them alone.",
    "stage:other": "**Settings the stages did not claim.** The one that matters "
                   "here:\n"
                   "owns: Pan-empty threshold\n"
                   "fixes: {{shakes ending with dirt left}}, lower it; "
                   "{{rattling an empty pan}}, raise it",
    # --- other pages ---------------------------------------------------------
    "relicsMaster": "**The relic timer master switch.** Every N minutes: pause, "
                    "switch to the item's hotbar slot, double-click to place, "
                    "back to the pan, resume.\n"
                    "steps: turn this on | enable the rows you carry | match the "
                    "hotbar slots\n"
                    "fixes: {{buffs expiring mid-run}}\n"
                    "pairs: Place relics only when ON LAND",
    "relicrow": "**One relic on a timer**: a name for the HUD, minutes "
                "between uses, its hotbar slot, and how many clicks placing "
                "takes.\n"
                "wrongif: the {{wrong item gets used}}; the slot number does "
                "not match your hotbar\n"
                "Note: enable only rows for items you actually carry.",
    "saverelics": "**Saves the relic rows** across restarts.\n"
                  "Note: in game, ctrl+shift+1 to 9 resets one relic timer and "
                  "ctrl+U resets all, for when you place something by hand.",
    "histrefresh": "**Reloads the run list from disk.** Finished sessions save "
                   "automatically; this pulls in one that just ended.",
    "histlist": "**Every finished session**: pans, digs, rate, recoveries, "
                "and a full event timeline naming the reason for every stop.\n"
                "when: comparing runs, or finding out why a run ended.\n"
                "Note: TRACKER labels mark watch-only runs of the game's own "
                "Auto Pan, measured on the same ruler.",
    "bldsearch": "**Filter your builds** by name or description as you type.\n"
                 "when: the shelf has grown past a screenful.",
    "bldsort": "**Reorder the build list**: newest, oldest, most used, "
               "recently used, or alphabetical. Nothing is changed or "
               "removed.",
    "bldname2": "**Name a new build**, then press Save current to snapshot "
                "everything you have set right now.",
    "bldsave2": "**Snapshot the current setup** into a new named build: every "
                "page plus relic rows.\n"
                "when: before experimenting, so you can always come back.",
    "bldimport": "**Load a build file someone sent you.** Renamed "
                 "automatically on a clash, so it never overwrites yours.\n"
                 "Note: if it has an equipment doc attached, a download button "
                 "appears on its card so you can see the gear it was tuned "
                 "for.",
    "keybinds": "**Global hotkeys** that work while the game has focus: "
                "start, stop, pause and more without alt-tabbing.\n"
                "steps: click a box | press the combo you want | Save "
                "keybinds, then stop and start the macro\n"
                "Note: separate from the in-game controls the macro sends.",
    "testnotify": "**Sends a real test notification** through the exact path "
                  "the macro uses.\n"
                  "steps: turn on DM me on Discord | enter your username | "
                  "press this\n"
                  "fixes: {{finding out at 3am that DMs never worked}}\n"
                  "Note: if it says sent but nothing arrives, you are not in "
                  "the server or DMs are closed.",
    "buildcard": "**A build: your whole setup under one name.** Load applies "
                 "every setting and relic at once; Overwrite re-captures your "
                 "current settings into it.\n"
                 "when: switching farms is one click, geodes to money and "
                 "back.\n"
                 "steps: Load to apply | Export to send a friend | Attach a "
                 "gear doc so they know what to build\n"
                 "Note: loading an older build never zeroes newer settings it "
                 "does not know about.",
    "coachsend": "**Send your message to the Coach.** It reads your settings "
                 "and recent run stats, then proposes exact changes with "
                 "one-click apply.",
    "coachin": "**Describe what you see**, like it shakes too early or it "
               "lands in the water. No setting names needed; the Coach finds "
               "the knob.",
}

# Calibratable on-screen pixels: (key, label, description, default [x, y]).
# Defaults are the original values -- shown so you know what to calibrate; only
# the Calibrate button changes them. CAP_LEFT_PIXEL is used to compute the bar
# width (CAP_BAR_WIDTH); the rest map straight to macro pixel settings.
PIXEL_FIELDS = [
    ("CAP_FULL_PIXEL", "Capacity bar: RIGHT end",
     "The right tip of the Pan Fill bar. Gray when empty, YELLOW when the pan is full.",
     [1120, 900]),
    ("CAP_LEFT_PIXEL", "Capacity bar: LEFT end",
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
    ("MONEY_TL_PIXEL", "Money counter: TOP-LEFT corner",
     "Top-left corner of the money region (bottom-right HUD). Leave generous "
     "room to the LEFT: the number grows longer as you earn.",
     [0, 0]),
    ("MONEY_BR_PIXEL", "Money counter: BOTTOM-RIGHT corner",
     "Bottom-right corner of the money region, just past the last digit.",
     [0, 0]),
    ("SHARDS_TL_PIXEL", "Shards counter: TOP-LEFT corner",
     "Top-left corner of the shards number (next to the crystal icon). Leave "
     "room to the left for growth.",
     [0, 0]),
    ("SHARDS_BR_PIXEL", "Shards counter: BOTTOM-RIGHT corner",
     "Bottom-right corner of the shards number.",
     [0, 0]),
    ("FIND_TL_PIXEL", "Find stack: TOP-LEFT corner",
     "Top-left of the whole FINDS STACK area (bottom-right HUD). Finds stack "
     "vertically and fade, so make the box TALL enough to cover several cards "
     "at once, and wide enough for the longest item name.",
     [0, 0]),
    ("FIND_BR_PIXEL", "Find stack: BOTTOM-RIGHT corner",
     "Bottom-right of the finds stack area, past the newest card's weight. "
     "The taller the box, the more of the fading stack the tracker can read.",
     [0, 0]),
]

# The three OCR regions are calibrated by DRAWING A BOX on the screen overlay
# (one drag), which fills the TL + BR pixel pairs above. The pairs stay the
# storage format so configs, builds and the engine are untouched.
REGION_FIELDS = [
    ("MONEY", "Money counter region",
     "Drag a box around the money total (bottom-right HUD). Leave generous "
     "room to the LEFT: the number grows longer as you earn."),
    ("SHARDS", "Shards counter region",
     "Drag a box around the shards number, next to the crystal icon. Leave "
     "room to the left for growth."),
    ("FIND", "Find pop-up region",
     "Drag a box over the whole finds area (bottom-right HUD). Finds stack "
     "vertically and fade, so make it tall enough for several cards and wide "
     "enough for the longest item name."),
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
<title>Prospecting Macro, Settings</title>
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
                msg += ", launched in Terminal (press Ctrl+K to start)"
            except Exception as e:
                msg += f", couldn't auto-launch ({e}); run python3 prospecting_old.py"
        self._send(render(msg))


def launch_macro():
    """Open the macro in Terminal WITHOUT needing AppleScript automation perms:
    write a .command file and `open` it (double-click-equivalent)."""
    launcher = os.path.join(HERE, "_run_macro.command")
    with open(launcher, "w") as f:
        f.write("#!/bin/bash\n"
                f"cd {shlex.quote(HERE)}\n"
                f"exec python3 {shlex.quote(MACRO_FILE)}\n")
    os.chmod(launcher, 0o755)
    subprocess.run(["open", launcher], check=True)


# Open the settings page as a chromeless "app" window using a Chromium browser
# (Chrome / Brave / Edge / Chromium). We prefer whichever one is already RUNNING
# so it uses YOUR browser (e.g. Edge) instead of surprise-launching Chrome.
APP_WINDOW = True
APP_BROWSERS = [
    ("Google Chrome",  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ("Microsoft Edge", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
    ("Brave Browser",  "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
    ("Chromium",       "/Applications/Chromium.app/Contents/MacOS/Chromium"),
]


def _running(appname):
    try:
        return subprocess.run(["pgrep", "-f", appname + ".app"],
                              capture_output=True).returncode == 0
    except Exception:
        return False


def open_window(url):
    """Open a chromeless app window in a Chromium browser, preferring one that's
    already running (so it matches the browser you actually use). Bring it to the
    front so the first click isn't wasted just focusing the window."""
    import time as _t
    if APP_WINDOW:
        installed = [(n, p) for (n, p) in APP_BROWSERS if os.path.exists(p)]
        chosen = next((c for c in installed if _running(c[0])),
                      installed[0] if installed else None)
        if chosen:
            name, path = chosen
            try:
                subprocess.Popen([path, f"--app={url}", "--window-size=860,960"])
                _t.sleep(1.2)
                subprocess.Popen(["open", "-a", name])   # bring it to the front
                return f"{name} app window"
            except Exception:
                pass
    webbrowser.open(url)                     # fallback: default browser tab
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
