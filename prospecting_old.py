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
import os
import json
import time
import threading
import subprocess
import urllib.request
from collections import namedtuple

# Settings written by the UI (prospecting_ui.py). Loaded at startup to override
# the defaults below, so you can tune everything without editing this file.
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "prospecting_config.json")


def load_config():
    """Override module config globals from prospecting_config.json (if present)."""
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return
    g = globals()
    applied = 0
    for k, v in data.items():
        if k in g and not callable(g[k]):
            if k in ("WEBHOOK_URL", "WEBHOOK_SECRET") and not v:
                continue          # a blank config must NOT wipe the baked default
            g[k] = v
            applied += 1
    # --- Easy tuning: layer the friendly offsets on top of the real knobs. Each
    #     maps to ALL the knobs that actually drive that move, so the effect shows.
    g["WATER_EXTRA_BACK_MS"]  = g.get("WATER_EXTRA_BACK_MS", 0) + EASY_WATER_BACK_MS
    g["PAN_BACK_MAX_MS"]      = g.get("PAN_BACK_MAX_MS", 0) + EASY_WATER_BACK_MS
    g["LAND_SETTLE_MS"]       = g.get("LAND_SETTLE_MS", 0) + EASY_LAND_FWD_MS
    g["LAND_PROBE_NUDGE_MS"]  = g.get("LAND_PROBE_NUDGE_MS", 0) + EASY_LAND_FWD_MS
    g["DEPOSIT_MAX_MS"]       = g.get("DEPOSIT_MAX_MS", 0) + EASY_LAND_FWD_MS
    g["SHAKE_START_DELAY_MS"] = g.get("SHAKE_START_DELAY_MS", 0) + EASY_SHAKE_DELAY_MS
    g["PRE_DIG_SETTLE_MS"]    = g.get("PRE_DIG_SETTLE_MS", 0) + EASY_FIRST_DIG_DELAY_MS
    g["POST_SHAKE_SETTLE_MS"] = g.get("POST_SHAKE_SETTLE_MS", 0) + EASY_FIRST_DIG_DELAY_MS
    if any((EASY_WATER_BACK_MS, EASY_LAND_FWD_MS, EASY_SHAKE_DELAY_MS,
            EASY_FIRST_DIG_DELAY_MS, EASY_WATER_RETURN_DELAY_MS)):
        print(f"[easy] water_back+{EASY_WATER_BACK_MS} land_fwd+{EASY_LAND_FWD_MS} "
              f"shake_delay+{EASY_SHAKE_DELAY_MS} first_dig+{EASY_FIRST_DIG_DELAY_MS} "
              f"water_return+{EASY_WATER_RETURN_DELAY_MS} -> "
              f"WATER_EXTRA_BACK_MS={g['WATER_EXTRA_BACK_MS']} "
              f"LAND_PROBE_NUDGE_MS={g['LAND_PROBE_NUDGE_MS']} "
              f"SHAKE_START_DELAY_MS={g['SHAKE_START_DELAY_MS']} "
              f"PRE_DIG_SETTLE_MS={g['PRE_DIG_SETTLE_MS']}")
    # pixel coords may arrive as [x, y] lists from the calibrator -> tuples
    for pk in ("CAP_FULL_PIXEL", "DEPOSIT_PIX", "PAN_PIX", "SHAKE_PIX",
               "DIG_TRIGGER_PIXEL", "TERRAIN_PIXEL"):
        if isinstance(g.get(pk), list):
            g[pk] = tuple(g[pk])
    # CAP_START_PIXEL is DERIVED -> recompute after any calibration change
    g["CAP_START_PIXEL"] = (g["CAP_FULL_PIXEL"][0] - g["CAP_BAR_WIDTH"] + 12,
                            g["CAP_FULL_PIXEL"][1])
    print(f"[config] applied {applied} setting(s) from "
          f"{os.path.basename(CONFIG_FILE)}")

# ============================================================================
# CONFIG  --  everything you tune lives here
# ============================================================================

# --- DIG BEHAVIOR TOGGLES ----------------------------------------------------
# MANUAL_DIG: if True, do a FIXED number of digs (DIGS_PER_CYCLE) then empty,
#   instead of digging until the capacity bar reads full. Use when you KNOW how
#   many digs fill the pan (e.g. 1 on this build) -- skips capacity detection.
MANUAL_DIG        = True
DIGS_PER_CYCLE    = 1               # how many digs before emptying (MANUAL_DIG)
# PERFECT: if True, watch the green pixel and release on it. On this build the
#   skill-bar line moves TOO FAST to catch by pixel, so we leave this OFF and use
#   a fixed-length hold (DIG_CLICK_MS) that reliably lands on green instead.
PERFECT           = False
# v2: dig DYNAMICALLY until the capacity bar reads FULL (don't assume 1 dig). This
# is the max digs we'll do to fill before proceeding anyway (safety cap).
MAX_DIGS_TO_FILL  = 8
DIG_CLICK_MS      = 75              # dig hold length -- tuned so the skill bar
                                   # lands on GREEN for this build's dig speed
                                   # (the green pixel is too fast to detect live)
# DIG SPEED: your dig-speed stat (percent). The UI uses it to AUTO-FILL
# DIG_CLICK_MS above (hold = 55000 / DIG_SPEED, i.e. 100% = 550ms, 200% = 275ms).
# Stored for reference; the macro acts on DIG_CLICK_MS, so it's not scaled twice.
DIG_SPEED         = 100            # percent

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
DIG_MAX_MS        = 1500            # per-dig cap: release after this even if green
                                   # was never seen. Long enough for the line to
                                   # reach green on land; also caps how long a dig
                                   # that landed in WATER (no bar) holds before we
                                   # detect the miss and recover.
BETWEEN_DIGS_MS   = 120            # pause between digs when the pan is NOT full
CAP_CHECK_MS      = 30             # tiny settle after a dig before checking full
                                   # (lower = start the walk-back sooner)

# --- CAPACITY BAR: full detection --------------------------------------------
# Pixel near the RIGHT END of the capacity bar: gray when not full, yellow full.
CAP_FULL_PIXEL    = (1120, 900)     # (x, y)  calibrated: yellow=full, gray=empty
# "Yellow" = high red & green, low blue:
YEL_MIN           = 140            # r and g must both be >= this
YEL_BLUE_GAP      = 45             # ...and blue must be <= min(r,g) - this
# Width of the whole bar (left of CAP_FULL_PIXEL) -- used to detect FULLY empty
# so the shake-hold can stop the instant the pan is done, no dead pause.
CAP_BAR_WIDTH     = 440
CAP_EMPTY_FRAC    = 0.04           # yellow fraction below this -> pan is empty
# "Cap STARTED filling" detector: the START (left end) of the bar goes from gray
# to coloured the instant the dig registers. We move as soon as it STARTS (the
# bar is a slider; no need to wait for it to finish). gray = flat (low spread),
# filled = coloured (high spread).
CAP_START_PIXEL   = (CAP_FULL_PIXEL[0] - CAP_BAR_WIDTH + 12, CAP_FULL_PIXEL[1])
CAP_START_DELTA   = 22            # start pixel must change by > this vs its empty
                                  # colour (captured pre-dig) to count as started
# How long to wait after a dig for the cap bar to start filling (so we move at
# the right moment). Proceeds anyway after this.
CAP_START_MS      = 250

# --- DRIFT FIX: "Collect Deposit" cue anchors the forward leg ----------------
# Instead of riding W back for a time-matched duration (which drifts), ride W
# until the "Collect Deposit" prompt appears = you've reached the land edge.
# Re-anchors to the same spot every cycle. Needs the cue pixel calibrated.
DEPOSIT_ANCHOR    = True
DEPOSIT_PIX       = (770, 981)    # white when "Collect Deposit" shows (on land)
PAN_PIX           = (847, 981)    # white when "Pan" shows (in the water)
SHAKE_PIX         = (830, 981)    # white when "Shake" shows (shake initiated)
CUE_WHITE_MIN     = 160           # a pixel is "cue text" if all channels >= this
CUE_WHITE_SPREAD  = 70            # ...and max-min spread <= this (whitish)
CUE_WHITE_FRAC    = 0.12          # cue present if >= this FRACTION of the box is
                                  # white text (counts pixels, doesn't average)
DEPOSIT_MAX_MS    = 1200          # safety cap riding W to find the land cue
SHAKE_INIT_MS     = 280           # window to confirm the "Shake" cue after the
                                  # start-click; if it never shows (and you read
                                  # Collect Deposit + full) the shake failed
PAN_BACK_MAX_MS   = 200           # safety cap walking S back (also = the back
                                  # distance if the Pan cue never fires)

# --- TERRAIN DETECTION (drives S/W by what's under your feet) -----------------
# When True: hold S until you're in the liquid, hold W until back on dirt.
# Self-correcting, no fixed times. When False: use the fixed ms below.
USE_TERRAIN_DETECT = False
# A ground pixel near your feet that reads DIRT on land and the LIQUID in water.
TERRAIN_PIXEL     = (900, 720)      # (x, y)  calibrated
# Reference colours from calibrate. Each terrain can hold ONE colour OR a LIST
# of colours -- add more samples to cover two-tone / gradient ground (e.g. light
# and dark beige, bright and dark lava). Classification picks the nearest match.
DIRT_RGB          = [(120, 195, 70), (150, 210, 95)]    # GREEN land (estimate)
WET_RGB           = [(70, 195, 200), (95, 205, 205)]    # BLUE water (estimate)
# Robustness to the floor tint pulses (green/yellow/white flashes):
#   classification uses colour PROPORTION (chromaticity), so brightness/white
#   pulses are ignored; a pulse that changes hue is filtered by these:
TERRAIN_CONFIRM   = 3     # consecutive agreeing reads required before acting
WALK_BACK_CONFIRM = 1     # reads to confirm WATER on the walk-back (1 = instant)
WALK_BACK_MIN_MS  = 0     # ignore terrain reads for the first N ms of the S move
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
# DRIFT FIX: anchor the walk-back to the lava. Instead of holding S for a fixed
# time (which drifts), hold S until you actually touch the lava (re-anchors to
# the edge every cycle). Forward stays timed, so the dig spot stays put.
# Needs TERRAIN_PIXEL/WET_RGB to read the lava when you back in (verify in
# calibrate). Falls back to the fixed WALK_BACK_MS if set False.
ANCHOR_WALKBACK   = True
# OVERSHOOT FIX: the game shows the "Pan" prompt a beat AFTER you cross into the
# water, and the character keeps gliding back after S is released -- so by the
# time the cue fires you've drifted deep. The moment Pan is detected we tap W
# forward this many ms to cancel the backward glide and stop at the water edge.
# Raise if it still goes too far back; lower (or 0) if the brake pushes you onto
# land before the shake.
WALK_BACK_BRAKE_MS = 70
# After the Pan cue fires, KEEP holding S this long to walk a little FARTHER into
# the water before shaking. The cue can trigger right at the edge; shaking there
# sometimes misses, so going a touch deeper makes the shake land reliably. This
# is the "delay between Pan detected and shake start". Raise to go deeper.
# (0 = disabled -- this delay was found to break the timing, so it's off.)
WATER_EXTRA_BACK_MS = 0
# Pause between releasing S and the first shake click. (0 = disabled.)
SHAKE_START_DELAY_MS = 0
WALK_BACK_MS      = 95     # hold S to back into the water (fallback / if anchor off)
WALK_BACK_EXTRA_MS = 0     # keep holding S this long AFTER touching water (go a
                          # bit deeper); the forward move adds the same so you
                          # still return to the dig spot
MOMENTUM_W_MS     = 95     # (no longer drives forward distance -- W now matches
                          #  the measured S time; kept for reference)
# Forward W is held for EXACTLY the measured S time + this trim (ms). NEGATIVE
# = less forward (if you overshoot PAST the start); POSITIVE = more forward (if
# you land SHORT, e.g. the shake animation ate movement).
SHAKE_FWD_COMP_MS = 0
# --- SHAKE: momentum technique with rapid CLICKS (no held press) -------------
# Your build empties so fast you don't need to HOLD -- just rattle off clicks.
# And we start holding W the moment the shake begins so momentum carries you
# back ONTO THE LAND while the pan drains (the "special technique"). We stop
# walking W when the Collect Deposit cue shows (we're on land) but keep clicking
# until the CAPACITY reads empty (capacity is the truth; the Shake cue sticks).
SHAKE_MOMENTUM_W   = True   # hold W during the shake -> glide onto land
SHAKE_CLICK_MS     = 18     # length of each shake click (short, fast build)
SHAKE_CLICKS       = 0      # EXACT number of shake clicks (0 = auto: shake until
                            # the pan reads empty). Set a fixed count to stop the
                            # shake cleanly so no extra click bleeds into the dig.
SHAKE_CLICK_GAP_MS = 14     # gap between shake clicks
SHAKE_HOLD_MS      = 1500   # overall shake timeout (stops early when empty)
SHAKE_INIT_GAP_MS    = 10   # (legacy) unused now
SHAKE_PLAIN_HOLD     = False # (legacy) unused now
SHAKE_PRESS_DELAY_MS = 25   # (legacy) unused now
SHAKE_HOLD_DELAY_MS  = 30   # (legacy) unused now
SHAKE_MOMENTUM_MS    = 120  # (legacy) unused now
SHAKE_START_MS    = 70    # (unused in timed path; kept for the detect path)
GAP_MS            = 25    # tiny settle gap between phases
POST_DIG_MS       = 60    # slight settle after a dig, before walking back
AFTER_EMPTY_MS    = 120   # settle after emptying, before digging again
RETRY_GAP_MS      = 25    # gap before RE-trying an empty when the pan stayed full

# --- ERROR HANDLING (capacity is the ground truth) ---------------------------
EMPTY_RETRIES     = 3     # re-attempt the empty (backing up further) if the pan
                         # didn't actually empty (e.g. didn't reach the water)
RETRY_BACK_MS     = 120   # extra S walk-back per retry to reach the water
STUCK_LIMIT       = 4     # consecutive failed empties -> STOP and alert
ALERT_SOUND       = "/System/Library/Sounds/Sosumi.aiff"

# --- ROBUST SUPERVISOR (fused cue + capacity state machine) ------------------
# The control core re-senses every tick and CROSS-CHECKS two independent layers
# before trusting anything:
#     WHERE am I   -> the HUD prompt cue  (Collect Deposit / Pan / Shake)
#     WHAT's in it -> the capacity bar    (full / empty / partial)
# Capacity is the ground truth for "did it empty" (the Shake cue can stick), the
# cue is the ground truth for "where am I". Every action verifies its own result
# on the next tick; a deadlock (same situation N ticks) escalates through
# recovery nudges, and only then a SAFE STOP. This self-heals from ANY start
# state and from missed clicks, under/overshoots, stuck cues and drift.
SENSE_VOTES        = 1    # sensor reads fused per decision (>1 votes out flicker)
MOVE_CONFIRM_MS    = 140  # after a move, wait this long for the target cue to settle
DIG_FILL_MS        = 250  # after a dig, wait up to this for the bar to read FULL
MAX_DIGS_PER_VISIT = 4    # dig at most this many times to fill before giving up
LAND_SETTLE_MS     = 45   # after the land cue fires, hold W a touch longer to land
                          # FIRMLY on the dirt (prevents a land<->water flicker).
                          # Forward over-travel onto land is harmless; braking
                          # BACK toward the water is what caused the oscillation.
SHAKE_POLL_MS      = 30   # poll spacing while holding the shake
STUCK_TICKS        = 3    # identical (place, pan) reads in a row -> escalate
RECOVER_LIMIT      = 3    # recovery attempts on a deadlock before SAFE STOP
SHAKE_FAIL_LIMIT   = 5    # shakes that didn't empty (even if you keep MOVING
                          # between water/land) before SAFE STOP -- catches the
                          # case where the shake silently never works
# Shake bail is CAPACITY-based: give up on a shake only if the pan is STILL
# COMPLETELY FULL after this long. A REAL shake has already drained to PARTIAL by
# now (so it reads not-full and is safe from this bail) -- only a shake that never
# started stays full. So this can be fairly short to react FAST when a shake fails
# to initiate (e.g. you clicked on land), without killing a real in-progress shake.
SHAKE_BAIL_MS      = 500
# SMART BREAK-OUT: when the watchdog has recovered RECOVER_LIMIT times in a row and
# we're still stuck, escalate -- a shake is probably ACTIVE and locking movement
# (you can't walk mid-shake, but CLICKS still finish it). So click to finish it,
# then reposition, then let normal logic resume. Only SAFE STOP if break-out also
# fails BREAKOUT_LIMIT times.
BREAKOUT_LIMIT     = 2    # break-outs before SAFE STOP
BREAKOUT_SHAKE_MS  = 700  # when stuck + FULL, click this long to finish an active
                          # shake that's locking movement
BREAKOUT_REPOS_MS  = 160  # forward W reposition nudge during a break-out (gets you
                          # off the water edge if you keep landing too shallow)
# Each recovery SYSTEM can be turned off independently (in case one misbehaves or
# a player just doesn't want it). With all off, the macro only does the normal
# loop and still safe-stops via the per-situation guards if it truly can't progress.
RECOVER_ENABLED     = True   # the stuck-recovery jitter step
SHAKE_RETRY_ENABLED = True   # re-attempt a shake when stuck full+in-water
BREAKOUT_ENABLED    = True   # the last-resort break-out to escape a stuck loop
NO_FULL_LIMIT       = 8      # if the pan never reads FULL this many cycles in a row,
                             # stop and tell the user to re-calibrate the capacity pixel

# --- Treasure Chest Collection mode ------------------------------------------
# A separate, simple loop: NO shake. Dig briefly, then strafe sideways (right,
# then left, alternating) until the Collect/Deposit cue shows again, then dig.
TREASURE_MODE        = False
TREASURE_DIGS        = 1     # how many digs per chest before strafing on
TREASURE_DIG_MS      = 8     # quick dig click length (5-10 ms)
TREASURE_DIG_GAP_MS  = 12000 # delay between each dig click (slow chest dig animation)
TREASURE_MOVE_MAX_MS = 2500  # safety cap on the strafe before moving on
# Post-shake landing by DIG-PROBE (not by cue). After a shake the "Shake" cue can
# STICK and never flip to "Collect Deposit", so we don't wait for that cue. We
# trust the W-momentum carried us toward land and just DIG: a dig only fills on
# dirt, so the CAPACITY bar tells us if we're on land. Hit -> on land (pan now
# filling). Miss -> nudge forward and retry.
LAND_DIG_TRIES      = 5   # nudge-forward ROUNDS while searching for land
# Per round, re-dig IN PLACE this many times before deciding we're off-land and
# nudging W. A dig that simply didn't register (timing) is NOT the same as being
# in the water, so we don't walk away on the first miss -- this stops the jittery
# "nudge between digs" on multi-dig builds.
DIG_INPLACE_TRIES   = 3
# A dig "registered" (we're on land) if the capacity bar's FILL LEVEL RISES by at
# least this fraction. This works even when the pan was already PARTIAL (the old
# start-pixel check missed that, so it false-nudged W after the first dig).
CAP_RISE_FRAC       = 0.03
DIG_PROBE_MS        = 320 # wait this long for a probe-dig to register before
                          # calling it a MISS (a working dig sometimes fills the
                          # bar slowly -- too short and we nudge again too soon)
LAND_PROBE_NUDGE_MS = 90  # forward W nudge between failed probe-digs
PROBE_GAP_MS        = 80  # settle after a forward nudge, before the next dig
                          # (stops it clicking W twice in a row too fast)
PRE_DIG_SETTLE_MS   = 60  # VERY slight settle before the FIRST dig after a shake,
                          # so the dig doesn't fire slightly early (miss -> needless
                          # recovery nudge). Small; raise a touch if it still misses.
POST_SHAKE_SETTLE_MS = 150 # pause after the pan empties (let the shake animation
                          # + momentum settle you onto land) BEFORE the dig-probe.
                          # Without it the first dig often fires mid-glide and
                          # misses, needing an extra forward nudge. Tune up/down.
RECOVER_BACK_MS    = 160  # corrective nudge BUDGET during recovery (pulsed)
LIMBO_NUDGE_MS     = 200  # forward (toward land) probe BUDGET when NO cue (pulsed)
# Pulsed movement: forward/recovery moves are short taps with a cue check after
# EACH tap, so they stop the instant the target shows (no long blind holds).
BURST_ON_MS        = 11   # hold the key this long per tap...
BURST_OFF_MS       = 1    # ...release this long, then re-check the cue
# Live terminal log: print every decision + sub-step with timing so you can SEE
# what the macro is thinking (where it is, what it plans, how long each step
# took). Set False for a quiet run.
LOG_LIVE           = True

# --- RELICS (timed item use: solar mags, idols, etc.) ------------------------
# Every relic's "minutes" the macro PAUSES, switches to its hotbar slot, double-
# clicks (uses it), switches back to RELIC_RETURN_SLOT (the pan), and resumes.
# Add/remove/edit entries (the UI has a Relics tab). RELICS_ENABLED master toggle.
RELICS_ENABLED     = False        # off by default (turn on in the UI / config)
RELICS = [
    {"name": "Solar Magnifier", "minutes": 10, "slot": 5, "clicks": 2},
    {"name": "Infernal Idol",   "minutes": 10, "slot": 6, "clicks": 2},
]
RELIC_RETURN_SLOT  = 1     # hotbar slot to return to after using a relic (pan)
RELIC_SWITCH_MS    = 160   # wait after pressing a slot key before clicking
RELIC_CLICK_MS     = 20    # each relic click length
RELIC_CLICK_GAP_MS = 90    # gap between the relic clicks
RELIC_PRE_MS       = 120   # settle after releasing movement, before switching slot

# --- DISCORD WEBHOOK / NOTIFICATIONS -----------------------------------------
# Posts JSON to WEBHOOK_URL on events (start, safe-stop, bag full, periodic
# stats). Works as a plain Discord webhook (shows "content") AND carries
# structured fields ("event","user","stats") so a custom bot can DM people.
WEBHOOK_ENABLED    = False
WEBHOOK_URL        = "http://159.203.22.31:3001/notify"  # baked default (config can override)
WEBHOOK_SECRET     = "23690760157cb3ab092e5d5ce2b10ddae7bd5d669a70da13"  # baked default
WEBHOOK_USER       = ""     # a name/id your bot uses to know who to DM
WEBHOOK_STATS_MIN  = 60     # also send a stats update every N minutes (0 = off)
# Per-event notification toggles (which DMs you actually get). Recoveries are
# OFF by default because they can fire often; the rest are on.
NOTIFY_START       = True
NOTIFY_STOP        = True   # manual stop, auto-stop timer, bag-full
NOTIFY_STATS       = True
NOTIFY_SAFE_STOP   = True
NOTIFY_RECOVERIES  = False
NOTIFY_ERRORS      = True

# --- AUTO-STOP TIMER ---------------------------------------------------------
AUTOSTOP_ENABLED   = False
AUTOSTOP_MINUTES   = 60     # stop the macro after this many minutes of running
# Bag-full guard: stop after this many pans (0 = off). A simple proxy for a full
# inventory until proper count/colour reading is calibrated.
STOP_AFTER_PANS    = 0

# --- SAFE-STOP behaviour -----------------------------------------------------
# On a hazard/stuck, PAUSE and retry instead of hard-stopping, so an AFK run can
# heal itself. Hard-stop only after this many failed retries in a row.
SAFE_STOP_RETRY       = True
SAFE_STOP_RETRY_SEC   = 60
SAFE_STOP_MAX_RETRIES = 3

# --- Smart / experimental ----------------------------------------------------
# SMART_TIMING: trial-and-error auto-tuning. If shakes miss the water (or landing
# needs nudges) more than ADAPT_MISS_PCT of the time over a window, it nudges the
# relevant timing and KEEPS the change only if the miss rate drops next window.
SMART_TIMING       = False
ADAPT_WINDOW       = 12     # attempts per evaluation window
ADAPT_MISS_PCT     = 20     # only adjust above this miss %
ADAPT_STEP_MS      = 25     # how far to nudge a timing each step
# X_PATTERN: walk BACK on alternating 45-degree diagonals (S+A, then S+D, ...)
# instead of straight S, so each pass covers new ground -- helps when you keep
# falling short of the water on a straight line. Forward (to land) stays straight.
X_PATTERN          = False

# --- Easy tuning (plain-language offsets; 0 = no change). The backend maps each
#     of these onto the real timing knobs in load_config(), so users can tune by
#     intent without learning every setting.
EASY_WATER_BACK_MS         = 0   # go this many ms FURTHER BACK into the water
EASY_LAND_FWD_MS           = 0   # go this many ms FURTHER FORWARD onto land
EASY_SHAKE_DELAY_MS        = 0   # wait this many ms longer before the shake starts
EASY_FIRST_DIG_DELAY_MS    = 0   # wait this many ms longer before the first dig
EASY_WATER_RETURN_DELAY_MS = 0   # wait this many ms before heading back to water

# --- WINDOW-RELATIVE CAPTURE (opt-in) ----------------------------------------
# When on, pixels are shifted by how far the Roblox window moved since you
# calibrated, so calibration survives the window being in a different spot.
# Default OFF = identical absolute-coordinate behaviour. (macOS: best-effort.)
WINDOW_RELATIVE     = False
ROBLOX_TITLE        = "Roblox"
CALIB_WINDOW_ORIGIN = [0, 0]   # window top-left captured at calibration time
CALIB_WINDOW_RECT   = [0, 0, 0, 0]   # [x,y,w,h] of the game window at calibration
# Auto-calibrate from the live Roblox window at start using the ratio profile
# (each pixel as a fraction of the game window). ON by default: if PIXEL_RATIOS
# is present and Roblox is found, the macro positions every pixel itself.
AUTO_CALIBRATE      = True
PIXEL_RATIOS        = {}

# --- Detection sampling ------------------------------------------------------
SAMPLE_BOX        = 6     # NxN px box averaged around a watched pixel

# --- Keys (macOS ANSI virtual keycodes) --------------------------------------
KEY_W = 13
KEY_S = 1
KEY_A = 0
KEY_D = 2
# Hotbar slot number -> macOS virtual keycode (digits 1..0).
SLOT_KEYCODES = {1: 18, 2: 19, 3: 20, 4: 21, 5: 23,
                 6: 22, 7: 26, 8: 28, 9: 25, 0: 29}

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


# ---- Window-relative capture (opt-in) ---------------------------------------
def find_window_origin():
    """(x, y) of the Roblox game viewport's top-left in PHYSICAL pixels, or None.
    macOS best-effort via the window list; returns None if not found."""
    try:
        opt = (Quartz.kCGWindowListOptionOnScreenOnly
               | Quartz.kCGWindowListExcludeDesktopElements)
        wins = Quartz.CGWindowListCopyWindowInfo(opt, Quartz.kCGNullWindowID)
        with _MSS() as sct:
            scale = get_scale(sct)
        best, area = None, 0
        for w in wins or []:
            owner = str(w.get("kCGWindowOwnerName", ""))
            if ROBLOX_TITLE.lower() in owner.lower():
                b = w.get("kCGWindowBounds", {})
                a = b.get("Width", 0) * b.get("Height", 0)
                if a > area:
                    area, best = a, b
        if best:
            return (int(best["X"] * scale), int(best["Y"] * scale))
    except Exception as e:
        print(f"[window] lookup failed: {e}")
    return None


def find_roblox_rect():
    """Roblox game client area as (x, y, w, h) in PHYSICAL pixels, or None.
    macOS: largest on-screen window whose owner is 'Roblox' (not Studio)."""
    try:
        opt = (Quartz.kCGWindowListOptionOnScreenOnly
               | Quartz.kCGWindowListExcludeDesktopElements)
        wins = Quartz.CGWindowListCopyWindowInfo(opt, Quartz.kCGNullWindowID)
        with _MSS() as sct:
            scale = get_scale(sct)
        best, area = None, 0
        for w in wins or []:
            owner = str(w.get("kCGWindowOwnerName", ""))
            if "roblox" in owner.lower() and "studio" not in owner.lower():
                b = w.get("kCGWindowBounds", {})
                ww, hh = b.get("Width", 0), b.get("Height", 0)
                if ww * hh > area and ww >= 320 and hh >= 240:
                    area, best = ww * hh, b
        if best:
            return (int(best["X"] * scale), int(best["Y"] * scale),
                    int(best["Width"] * scale), int(best["Height"] * scale))
    except Exception as e:
        print(f"[window] lookup failed: {e}")
    return None


def apply_auto_calibrate():
    """Position every watched pixel from the LIVE Roblox window using the ratio
    profile. Runs at start when AUTO_CALIBRATE is on and PIXEL_RATIOS exist, so
    calibration works with zero clicking and survives window moves / different
    resolutions. Returns True if it placed the pixels."""
    if not AUTO_CALIBRATE or not PIXEL_RATIOS:
        return False
    rect = find_roblox_rect()
    if not rect:
        print("[autocal] Roblox window not found; using saved coordinates")
        return False
    x, y, w, h = rect
    g = globals()
    placed = 0
    for pk, fr in PIXEL_RATIOS.items():
        if pk in g and isinstance(fr, (list, tuple)) and len(fr) == 2:
            g[pk] = (int(round(x + fr[0] * w)), int(round(y + fr[1] * h)))
            placed += 1
    if "CAP_FULL_PIXEL" in PIXEL_RATIOS and "CAP_LEFT_PIXEL" in PIXEL_RATIOS:
        bw = int(round((PIXEL_RATIOS["CAP_FULL_PIXEL"][0]
                        - PIXEL_RATIOS["CAP_LEFT_PIXEL"][0]) * w))
        if bw > 20:
            g["CAP_BAR_WIDTH"] = bw
    g["CAP_START_PIXEL"] = (g["CAP_FULL_PIXEL"][0] - g["CAP_BAR_WIDTH"] + 12,
                            g["CAP_FULL_PIXEL"][1])
    print(f"[autocal] placed {placed} pixels from Roblox window "
          f"{w}x{h} at ({x},{y})")
    return True


def apply_window_offset():
    """If WINDOW_RELATIVE, shift the calibrated pixels by how far the Roblox
    window moved since calibration. No-op (current behaviour) when off, when the
    window isn't found, or when it's in the calibrated position."""
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
    g = globals()
    for pk in ("CAP_FULL_PIXEL", "DEPOSIT_PIX", "PAN_PIX", "SHAKE_PIX",
               "DIG_TRIGGER_PIXEL", "TERRAIN_PIXEL"):
        x, y = g[pk]
        g[pk] = (x + dx, y + dy)
    g["CAP_START_PIXEL"] = (g["CAP_FULL_PIXEL"][0] - g["CAP_BAR_WIDTH"] + 12,
                            g["CAP_FULL_PIXEL"][1])
    print(f"[window] shifted pixels by ({dx},{dy}) for window move")


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


# ---- live logging (timestamped, scrolling) ----------------------------------
_log_t0 = None


def log(msg):
    """Print a timestamped line so you can watch the macro's reasoning live."""
    if not LOG_LIVE:
        return
    global _log_t0
    now = time.perf_counter()
    if _log_t0 is None:
        _log_t0 = now
    print(f"[{now - _log_t0:7.2f}s] {msg}", flush=True)


# ---- Input engine (Quartz CGEvent, HID level) -------------------------------
HID = Quartz.kCGHIDEventTap


def _post(ev):
    Quartz.CGEventPost(HID, ev)


def key_down(code):
    _post(Quartz.CGEventCreateKeyboardEvent(None, code, True))


def key_up(code):
    _post(Quartz.CGEventCreateKeyboardEvent(None, code, False))


def tap_key(code, ms=40):
    """Press and release a key (e.g. a hotbar number)."""
    if code is None:
        return
    key_down(code)
    sleep_ms(ms)
    key_up(code)


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


def hold_mouse(ms, stop_fn=None, confirm=2, min_ms=200):
    """Hold LMB up to ms, keeping the press 'alive' with periodic drag events so
    the game registers a sustained hold (a static down can get dropped).
    If stop_fn is given, release early once it returns True for `confirm` reads
    in a row (but never before min_ms)."""
    mouse_down()
    start = time.perf_counter()
    end = start + ms / 1000.0
    hits = 0
    stopped = False
    while time.perf_counter() < end and State.running:
        p = _cursor_point()
        _post(Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventLeftMouseDragged, p, Quartz.kCGMouseButtonLeft))
        if stop_fn is not None and (time.perf_counter() - start) * 1000 >= min_ms:
            hits = hits + 1 if stop_fn() else 0
            if hits >= confirm:
                stopped = True
                break
        sleep_ms(16)
    mouse_up()
    return stopped     # True if stop_fn fired (pan emptied), False if timed out


def _drag_for(ms, stop_fn=None):
    """Keep an ALREADY-held LMB alive with drag events for up to ms. Returns
    True if stop_fn fired. (Caller does mouse_down/up around this.)"""
    end = time.perf_counter() + ms / 1000.0
    while time.perf_counter() < end and State.running:
        _post(Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventLeftMouseDragged, _cursor_point(),
            Quartz.kCGMouseButtonLeft))
        if stop_fn is not None and stop_fn():
            return True
        sleep_ms(16)
    return False


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
        self.cap_start_region = self._box(CAP_START_PIXEL)
        self.deposit_region = self._box(DEPOSIT_PIX)
        self.pan_region = self._box(PAN_PIX)
        self.shake_region = self._box(SHAKE_PIX)
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

    def snapshot(self):
        """All sensors in ONE pass (5 grabs). Returns raw booleans:
        (on_land, in_water, shaking, pan_full, pan_empty). Position cues are
        mutually exclusive via the deposit-pixel guard, so the wide 'Collect
        Deposit' text can't masquerade as 'Pan'/'Shake'."""
        dep = self._cue_white(self.deposit_region)
        pan = self._cue_white(self.pan_region) and not dep
        shk = self._cue_white(self.shake_region) and not dep
        full = self.capacity_full()
        empty = self.pan_empty()
        return dep, pan, shk, full, empty

    def _cue_white(self, region):
        # count white-text pixels in the box (don't average -- thin text would
        # get diluted by the dark background and never pass the threshold).
        img = np.asarray(self.sct.grab(region))[:, :, :3].astype(np.int16)  # BGR
        b, g, r = img[:, :, 0], img[:, :, 1], img[:, :, 2]
        lo = np.minimum(np.minimum(r, g), b)
        hi = np.maximum(np.maximum(r, g), b)
        white = (lo >= CUE_WHITE_MIN) & ((hi - lo) <= CUE_WHITE_SPREAD)
        return float(white.mean()) >= CUE_WHITE_FRAC

    def on_deposit(self):
        """True when the 'Collect Deposit' cue shows (white) = on the land edge."""
        return self._cue_white(self.deposit_region)

    def on_pan(self):
        """True when the 'Pan' cue shows (white) = in the water. Excludes the
        wide 'Collect Deposit' text (its leftmost pixel) to avoid confusion."""
        return self._cue_white(self.pan_region) and not self._cue_white(self.deposit_region)

    def on_shake(self):
        """True when the 'Shake' cue shows (white) = the shake has initiated."""
        return self._cue_white(self.shake_region) and not self._cue_white(self.deposit_region)

    def cap_start_rgb(self):
        """Current colour at the start of the bar (for baseline comparison)."""
        return self._rgb(self.cap_start_region)

    def cap_changed(self, baseline):
        """True once the start of the bar differs from `baseline` (its empty
        colour captured just before the dig) -> the bar started filling."""
        r, g, b = self._rgb(self.cap_start_region)
        r0, g0, b0 = baseline
        return max(abs(r - r0), abs(g - g0), abs(b - b0)) > CAP_START_DELTA

    def cap_fill(self):
        """Fraction (0..1) of the WHOLE capacity bar that reads yellow = how full
        the pan is. Used to detect a dig REGISTERING (fill rises) even when the
        pan was already partially full (so the start-pixel didn't change)."""
        x, y = CAP_FULL_PIXEL
        region = {"left": x - CAP_BAR_WIDTH, "top": y - 10,
                  "width": CAP_BAR_WIDTH, "height": 20}
        img = np.asarray(self.sct.grab(region))[:, :, :3].astype(np.int16)
        b, g, r = img[:, :, 0], img[:, :, 1], img[:, :, 2]
        yellow = (r >= YEL_MIN) & (g >= YEL_MIN) & (b <= np.minimum(r, g) - YEL_BLUE_GAP)
        return float(yellow.mean())

    def pan_empty(self):
        """True when the WHOLE capacity bar has essentially no yellow left."""
        return self.cap_fill() < CAP_EMPTY_FRAC

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
    empty_fails = 0          # consecutive cycles the pan wouldn't empty
    shake_fails = 0          # consecutive shakes that didn't empty the pan
    land_fails = 0           # consecutive times the dig-probe couldn't find land
    breakouts = 0            # consecutive smart break-outs (escape a stuck loop)
    safe_retries = 0         # consecutive safe-stop pauses (retry, not hard-stop)
    want_reset = False       # supervisor resets itself after a safe-pause
    stop_reason = ""         # latest stop reason (manual/safe/auto/bag)
    water_fails = 0          # consecutive go_water tries that didn't reach water
    x_dir = 0                # X-pattern: which diagonal side to use next
    x_balance = 0.0          # X-pattern: running lateral balance (ms; +right/-left)
    no_full = 0              # consecutive dig cycles where the pan never read FULL
    t_side = 0               # Treasure mode: 0 -> strafe D (right), 1 -> A (left)
    ad_shake_n = 0           # adaptive: shake attempts in the current window
    ad_shake_miss = 0
    ad_land_n = 0            # adaptive: land attempts in the current window
    ad_land_miss = 0
    ad_water_dir = 1         # hill-climb direction for the water-reach knob
    ad_water_prev = None
    ad_land_dir = 1          # hill-climb direction for the land-reach knob
    ad_land_prev = None
    stats = None             # SessionStats while running


class SessionStats:
    """Live counters for the current run (cycles, recoveries, runtime, ...)."""
    def __init__(self):
        self.start = time.perf_counter()
        self.cycles = 0       # pans emptied = completed cycles
        self.recoveries = 0
        self.safe_stops = 0
        self.hard_stops = 0
        self.relics_used = 0
        self.nudges = 0
        self.stop_reason = ""

    def runtime(self):
        return time.perf_counter() - self.start

    def as_dict(self):
        rt = self.runtime()
        hrs = rt / 3600.0
        return {"cycles": self.cycles, "recoveries": self.recoveries,
                "runtime_s": int(rt),
                "pans_per_hr": round(self.cycles / hrs, 1) if hrs > 0.0008 else 0,
                "safe_stops": self.safe_stops, "hard_stops": self.hard_stops,
                "relics_used": self.relics_used, "nudges": self.nudges,
                "stop_reason": self.stop_reason}


# which per-event toggle gates each event name
_EVENT_FLAG = {
    "start": "NOTIFY_START",
    "stop": "NOTIFY_STOP", "autostop": "NOTIFY_STOP", "bag_full": "NOTIFY_STOP",
    "stats": "NOTIFY_STATS",
    "safe_stop": "NOTIFY_SAFE_STOP",
    "recovery": "NOTIFY_RECOVERIES",
    "error": "NOTIFY_ERRORS",
}


def post_webhook(event, message, stats=None):
    """Fire a JSON notification to WEBHOOK_URL (non-blocking). Plain Discord
    webhooks render 'content'; a custom bot can read event/user/stats. Honours
    the per-event NOTIFY_* toggles so users get only the DMs they want."""
    if not WEBHOOK_ENABLED or not WEBHOOK_URL:
        return
    flag = _EVENT_FLAG.get(event)
    if flag and not globals().get(flag, True):
        return
    payload = {"username": "Prospectors Plus", "content": message,
               "event": event, "user": WEBHOOK_USER, "stats": stats or {}}

    def _send():
        try:
            headers = {"Content-Type": "application/json"}
            if WEBHOOK_SECRET:
                headers["x-macro-secret"] = WEBHOOK_SECRET
            req = urllib.request.Request(
                WEBHOOK_URL, data=json.dumps(payload).encode("utf-8"),
                headers=headers)
            urllib.request.urlopen(req, timeout=8)
        except Exception as e:
            print(f"[webhook] failed: {e}")

    threading.Thread(target=_send, daemon=True).start()


def release_all():
    try:
        key_up(KEY_W)
        key_up(KEY_S)
        mouse_up()
    except Exception:
        pass


def safe_stop(reason, hard=False):
    """A hazard/stuck was detected. By DEFAULT pause and retry shortly instead of
    hard-stopping (so an AFK run recovers itself); only hard-stop after
    SAFE_STOP_MAX_RETRIES failed retries in a row."""
    release_all()
    State.empty_fails = State.shake_fails = State.land_fails = State.breakouts = 0
    if State.stats:
        State.stats.safe_stops += 1

    def _beep(hand=False):
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONHAND if hand
                                 else winsound.MB_ICONASTERISK)
        except Exception:
            try:
                subprocess.Popen(["afplay", globals().get("ALERT_SOUND", "")])
            except Exception:
                print("\a", end="", flush=True)

    if SAFE_STOP_RETRY and State.safe_retries < SAFE_STOP_MAX_RETRIES and not hard:
        State.safe_retries += 1
        msg = (f"{reason} - retrying in {SAFE_STOP_RETRY_SEC}s "
               f"(attempt {State.safe_retries}/{SAFE_STOP_MAX_RETRIES})")
        print(f"\n*** SAFE PAUSE: {msg} ***")
        post_webhook("safe_stop", f"⚠️ Safe-paused: {msg}",
                     State.stats.as_dict() if State.stats else None)
        _beep(False)
        end = time.perf_counter() + SAFE_STOP_RETRY_SEC
        while time.perf_counter() < end and State.running and State.alive:
            time.sleep(0.2)
        State.want_reset = True
        return
    State.running = False
    State.safe_retries = 0
    State.stop_reason = "safe-stop"
    if State.stats:
        State.stats.hard_stops += 1
        State.stats.stop_reason = "safe-stop"
    print(f"\n*** HARD STOP: {reason} ***  (Ctrl+K to resume, Esc to quit)")
    post_webhook("stop", f"🛑 Hard-stopped: {reason}",
                 State.stats.as_dict() if State.stats else None)
    _beep(True)


def append_history(stats, reason):
    """Append a finished run's summary to run_history.json (kept to last 100) so
    the user can review past runs after the app/macro closes."""
    try:
        import datetime
        path = os.path.join(_DATA_DIR, "run_history.json")
        hist = []
        if os.path.exists(path):
            try:
                hist = json.load(open(path))
            except Exception:
                hist = []
        d = stats.as_dict() if stats else {}
        d["reason"] = reason
        d["ended"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        hist.append(d)
        with open(path, "w") as f:
            json.dump(hist[-100:], f, indent=2)
    except Exception as e:
        print("[history] save failed:", e)


# ---- Smart adaptive timing (trial & error; opt-in) --------------------------
def smart_adapt():
    """Hill-climb two timings from real miss rates. Direction defaults to MORE
    distance (go deeper into the water / further onto land); if a step makes the
    miss rate worse next window, it reverses. No-op unless SMART_TIMING."""
    if not SMART_TIMING:
        return
    g = globals()
    if State.ad_shake_n >= ADAPT_WINDOW:
        rate = 100.0 * State.ad_shake_miss / max(1, State.ad_shake_n)
        if rate > ADAPT_MISS_PCT:
            if State.ad_water_prev is not None and rate > State.ad_water_prev:
                State.ad_water_dir *= -1
            nv = max(0, min(600, g["WATER_EXTRA_BACK_MS"]
                            + State.ad_water_dir * ADAPT_STEP_MS))
            g["WATER_EXTRA_BACK_MS"] = nv
            log(f"[smart] shake miss {rate:.0f}% -> WATER_EXTRA_BACK_MS={nv}ms")
        State.ad_water_prev = rate
        State.ad_shake_n = State.ad_shake_miss = 0
    if State.ad_land_n >= ADAPT_WINDOW:
        rate = 100.0 * State.ad_land_miss / max(1, State.ad_land_n)
        if rate > ADAPT_MISS_PCT:
            if State.ad_land_prev is not None and rate > State.ad_land_prev:
                State.ad_land_dir *= -1
            nv = max(0, min(600, g["LAND_SETTLE_MS"]
                            + State.ad_land_dir * ADAPT_STEP_MS))
            g["LAND_SETTLE_MS"] = nv
            log(f"[smart] land miss {rate:.0f}% -> LAND_SETTLE_MS={nv}ms")
        State.ad_land_prev = rate
        State.ad_land_n = State.ad_land_miss = 0


def _adapt_shake(emptied):
    if not SMART_TIMING:
        return
    State.ad_shake_n += 1
    if not emptied:
        State.ad_shake_miss += 1
    smart_adapt()


def _adapt_land(missed):
    if not SMART_TIMING:
        return
    State.ad_land_n += 1
    if missed:
        State.ad_land_miss += 1
    smart_adapt()


# ---- Cycle parts ------------------------------------------------------------
def dig_once(detector):
    """One dig. PERFECT=False -> quick click; PERFECT=True -> release on green.
    The hold length is DIG_CLICK_MS, which the UI auto-fills from DIG_SPEED
    (so the scaling is done once, in the UI, not again here)."""
    if not PERFECT:
        mouse_down()
        sleep_ms(DIG_CLICK_MS)
        mouse_up()
        return
    # PERFECT: hold LMB and release when the white line crosses the green pixel
    mouse_down()
    deadline = time.perf_counter() + DIG_MAX_MS / 1000.0
    while State.running and time.perf_counter() < deadline:
        if detector.white_on_trigger():
            break
        # poll as fast as possible; the line can move quickly
    if RELEASE_DELAY_MS:
        sleep_ms(RELEASE_DELAY_MS)
    mouse_up()


def dig_until_started(detector):
    """Dig once, then wait until the cap bar STARTS filling so we move at the
    right moment. Proceeds anyway after CAP_START_MS. (No nudging.)"""
    baseline = detector.cap_start_rgb()             # empty-bar colour pre-dig
    dig_once(detector)
    wait_until(lambda: detector.cap_changed(baseline), CAP_START_MS, confirm=1)
    return True


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


def pulse_until(key, cond, max_ms, confirm=1):
    """Move in short TAPS (BURST_ON_MS held, BURST_OFF_MS released) and re-check
    cond() after every tap. Stops the instant cond is True for `confirm` taps in
    a row, or when max_ms of total budget is spent. No long blind key-holds, so
    it can't sail past the target between sensor reads."""
    deadline = time.perf_counter() + max_ms / 1000.0
    hits = 0
    while State.running and time.perf_counter() < deadline:
        key_down(key); sleep_ms(BURST_ON_MS); key_up(key)
        sleep_ms(BURST_OFF_MS)
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


def timed_empty(detector=None):
    """CUE-DRIVEN empty (no fixed-time movement):
       1) hold S until the PAN cue shows (you're in the water),
       2) CLICK to initiate -> short gap -> HOLD to shake until the CAPACITY is
          empty (the cue stays stuck on 'Shake', so empty is the 'done' signal),
       3) hold W until the COLLECT DEPOSIT cue shows (back on land).
    Returns False if the shake never initiated (Collect Deposit + full)."""
    if detector is None:
        return True
    # 1) walk back until the PAN cue (in the water), then BRAKE the backward
    #    glide with a short forward W tap so we stop at the water edge.
    key_down(KEY_S)
    reached = wait_until(detector.on_pan, PAN_BACK_MAX_MS, confirm=WALK_BACK_CONFIRM)
    key_up(KEY_S)
    if reached and WALK_BACK_BRAKE_MS > 0:
        key_down(KEY_W); sleep_ms(WALK_BACK_BRAKE_MS); key_up(KEY_W)
    if not State.running: return True
    # 2) shake: CLICK to initiate -> gap -> HOLD until the capacity empties
    mouse_tap(SHAKE_CLICK_MS)
    sleep_ms(SHAKE_INIT_GAP_MS)
    mouse_down()
    started = False
    init_deadline = time.perf_counter() + SHAKE_INIT_MS / 1000.0
    end = time.perf_counter() + SHAKE_HOLD_MS / 1000.0
    while time.perf_counter() < end and State.running:
        if not SHAKE_PLAIN_HOLD:
            p = _cursor_point()
            _post(Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventLeftMouseDragged, p, Quartz.kCGMouseButtonLeft))
        if not started and detector.on_shake():
            started = True
        if detector.pan_empty():
            break
        if (not started and time.perf_counter() > init_deadline
                and detector.on_deposit() and detector.capacity_full()):
            mouse_up()
            return False                    # shake never initiated -> retry
        sleep_ms(40)
    mouse_up()
    if not State.running: return True
    # 3) walk forward until the COLLECT DEPOSIT cue (back on land)
    key_down(KEY_W)
    wait_until(detector.on_deposit, DEPOSIT_MAX_MS, confirm=1)
    key_up(KEY_W)
    return True


def run_empty(detector):
    """Empty the pan and VERIFY it actually emptied (capacity = ground truth).
    If not (e.g. the shake didn't reach the water), back up further and retry.
    After EMPTY_RETRIES failures, count it; STUCK_LIMIT in a row -> SAFE STOP.
    Returns True on a confirmed empty."""
    for attempt in range(EMPTY_RETRIES + 1):
        if not State.running:
            return False
        if attempt > 0:
            # previous try didn't empty -> walk back further to reach the water
            key_down(KEY_S); sleep_ms(RETRY_BACK_MS * attempt); key_up(KEY_S)
            sleep_ms(GAP_MS)
        ok = timed_empty(detector)          # False = shake never initiated
        if ok and (detector is None or detector.pan_empty()):
            State.empty_fails = 0
            return True                     # confirmed empty
    # exhausted retries -> the pan still reads full
    State.empty_fails += 1
    if State.empty_fails >= STUCK_LIMIT:
        safe_stop("pan won't empty -- not reaching the water?")
    return False


def empty_sequence(detector):
    if not USE_TERRAIN_DETECT:
        timed_empty(detector)               # pure-timed; stops when pan empties
        return
    walk_back(detector)
    if not State.running: return
    sleep_ms(GAP_MS)
    momentum_and_shake(detector)
    if not State.running: return
    sleep_ms(GAP_MS)
    shake_empty()
    sleep_ms(AFTER_EMPTY_MS)


# ============================================================================
# ROBUST SUPERVISOR  --  fused cue + capacity state machine
# ============================================================================
# Places (from the HUD cue) and pan contents (from the capacity bar).
LAND, WATER, SHAKING, LIMBO = "LAND", "WATER", "SHAKING", "LIMBO"
P_FULL, P_EMPTY, P_PARTIAL = "FULL", "EMPTY", "PARTIAL"
Situation = namedtuple("Situation", "where contents dep pan shk full empty")


def sense_stable(det):
    """Fuse the two sensor layers into one Situation, voting out single-frame
    flicker when SENSE_VOTES > 1. Cheap (5 grabs per vote)."""
    dc = pc = sc = fc = ec = 0
    for _ in range(SENSE_VOTES):
        dep, pan, shk, full, empty = det.snapshot()
        dc += dep; pc += pan; sc += shk; fc += full; ec += empty
    half = SENSE_VOTES / 2.0
    dep, pan, shk = dc > half, pc > half, sc > half
    full, empty = fc > half, ec > half
    where = LAND if dep else WATER if pan else SHAKING if shk else LIMBO
    contents = P_FULL if full else (P_EMPTY if empty else P_PARTIAL)
    return Situation(where, contents, dep, pan, shk, full, empty)


def plan_label(s):
    """The action the supervisor WOULD take for situation s (for logs/monitor).
    Capacity is primary. FULL + in-water(Pan cue) -> shake; FULL elsewhere -> go
    to the water. NOT full -> DIG-PROBE to find land (we ignore the stuck cue)."""
    if s.full:
        return "SHAKE" if s.pan else "go WATER (S back)"
    return "DIG-probe (find land)"


# ---- verified action primitives (each confirms its own result) --------------
def do_dig(det):
    """Dig until the capacity bar reads FULL (your build: ~1 dig). Verifies the
    dig REGISTERED (bar started) and re-digs if it didn't. Bails after
    MAX_DIGS_PER_VISIT so a non-registering dig can't loop forever."""
    t0 = time.perf_counter()
    for i in range(MAX_DIGS_PER_VISIT):
        if not State.running:
            return
        baseline = det.cap_start_rgb()
        dig_once(det)
        started = wait_until(lambda: det.cap_changed(baseline),
                             CAP_START_MS, confirm=1)
        if det.capacity_full() or wait_until(det.capacity_full, DIG_FILL_MS,
                                             confirm=1):
            log(f"    dig#{i+1}: started={started} -> FULL "
                f"({(time.perf_counter()-t0)*1000:.0f}ms)")
            return                       # full -> done
        log(f"    dig#{i+1}: started={started} not full yet, re-dig")
    log("    dig: left PARTIAL after max digs")
    # left partial -> the supervisor will take us to water and shake it out


def go_water(det):
    """HOLD S until the PAN cue (in the water), then keep holding a touch longer
    (WATER_EXTRA_BACK_MS) to walk a little FARTHER in before the shake -- the cue
    can fire right at the edge, where the shake sometimes misses."""
    t0 = time.perf_counter()
    # If we overshot far onto land (dig carried us forward), a fixed short S
    # budget can't reach the water -> walk back FARTHER on each consecutive miss.
    budget = PAN_BACK_MAX_MS * (1 + min(State.water_fails, 4))
    side = sgn = None
    if X_PATTERN:                        # ONE smooth diagonal (S+side held together);
        if State.x_balance > 0:          # pick the side that corrects drift to centre
            side, sgn = KEY_A, -1
        elif State.x_balance < 0:
            side, sgn = KEY_D, +1
        else:
            side, sgn = (KEY_D, +1) if State.x_dir == 0 else (KEY_A, -1)
            State.x_dir ^= 1
    tb = time.perf_counter()
    key_down(KEY_S)
    if side is not None:
        key_down(side)
    reached = wait_until(det.on_pan, budget, confirm=WALK_BACK_CONFIRM)
    if reached and WATER_EXTRA_BACK_MS > 0:
        sleep_ms(WATER_EXTRA_BACK_MS)    # keep holding S -> a bit deeper in
    if side is not None:
        key_up(side)                     # released WITH S -> no mid-walk kink
        State.x_balance += sgn * (time.perf_counter() - tb) * 1000.0
    key_up(KEY_S)
    State.water_fails = 0 if reached else State.water_fails + 1
    log(f"    S back: reached_PAN={reached} (+{WATER_EXTRA_BACK_MS}ms deeper, "
        f"budget {budget}ms) ({(time.perf_counter()-t0)*1000:.0f}ms)")


def do_shake(det):
    """THE MOMENTUM TECHNIQUE. Hold W (glide toward land) and rattle RAPID CLICKS
    to shake. macOS does NOT sustain a synthetic held press in Roblox, so a hold
    silently does nothing -- clicks DO register, so we click fast and keep going
    until the pan empties (a slower shake just takes more clicks). W carries you
    onto land WHILE the pan drains; we drop W when the Collect Deposit cue shows.
    Stop when the CAPACITY reads empty (capacity is the truth; the Shake cue
    sticks). Bails only if the pan stays COMPLETELY FULL past SHAKE_BAIL_MS."""
    t0 = time.perf_counter()
    if SHAKE_START_DELAY_MS > 0:
        sleep_ms(SHAKE_START_DELAY_MS)       # start later (we walked farther back)
    w_down = False
    if SHAKE_MOMENTUM_W:
        key_down(KEY_W); w_down = True       # momentum toward land
    started = emptied = bailed = on_land = False
    clicks = 0
    fixed = SHAKE_CLICKS > 0                  # exact-count mode (no extra click)
    end = time.perf_counter() + SHAKE_HOLD_MS / 1000.0
    while State.running and (clicks < SHAKE_CLICKS if fixed
                             else time.perf_counter() < end):
        mouse_tap(SHAKE_CLICK_MS)            # one shake click (rattle)
        clicks += 1
        if not started and det.on_shake():
            started = True
            log(f"    shake STARTED ({(time.perf_counter()-t0)*1000:.0f}ms)")
        if not fixed and det.pan_empty():    # auto mode: stop the instant it empties
            emptied = True
            break
        if w_down and det.on_deposit():      # reached land -> stop gliding...
            key_up(KEY_W); w_down = False     # ...but keep clicking to finish
            on_land = True
        # CAPACITY-based bail (auto mode only): give up if the pan is STILL FULL
        # well past a real shake's duration (no drain at all = no shake).
        if (not fixed and time.perf_counter() > t0 + SHAKE_BAIL_MS / 1000.0
                and det.capacity_full()):
            bailed = True
            break                            # truly not shaking
        sleep_ms(SHAKE_CLICK_GAP_MS)
    if fixed:
        emptied = det.pan_empty()            # did exactly N clicks -> read result
    if w_down:
        key_up(KEY_W)
    if emptied:
        State.shake_fails = 0
        State.breakouts = 0              # healthy progress -> clear escalation
        State.safe_retries = 0
        State.water_fails = 0
        if State.stats:
            State.stats.cycles += 1      # a pan emptied = one completed cycle
        sleep_ms(POST_SHAKE_SETTLE_MS)   # let momentum/animation settle onto land
    else:
        State.shake_fails += 1
    _adapt_shake(emptied)
    dur = (time.perf_counter() - t0) * 1000
    log(f"    shake done: started={started} emptied={emptied} "
        f"reached_land={on_land} bail={bailed} fails={State.shake_fails} "
        f"({dur:.0f}ms)")


def go_land(det):
    """HOLD W forward (smooth) until the COLLECT DEPOSIT cue shows, then hold a
    touch longer (LAND_SETTLE_MS) to sit FIRMLY on the dirt. No S brake -- braking
    back toward the water is what made it bounce land<->water instead of digging."""
    t0 = time.perf_counter()
    key_down(KEY_W)
    reached = wait_until(det.on_deposit, DEPOSIT_MAX_MS, confirm=1)
    if reached and LAND_SETTLE_MS > 0:
        sleep_ms(LAND_SETTLE_MS)         # settle firmly onto land (forward)
    key_up(KEY_W)
    log(f"    W fwd to land: reached_DEPOSIT={reached} "
        f"({(time.perf_counter()-t0)*1000:.0f}ms)")


def fill_to_full(det):
    """Once we're confirmed on land, dig until the capacity bar reads FULL. We do
    NOT assume a dig count -- we watch the bar, so builds that need 2, 3, ... digs
    all work. The probe already did dig #1; we wait for it to fill, then top up
    with more PERFECT digs as needed (capped by MAX_DIGS_TO_FILL)."""
    digs = 1                                  # the probe dig already happened
    for i in range(MAX_DIGS_TO_FILL):
        if det.capacity_full():
            log(f"    filled in {digs} dig(s)")
            return True
        if not State.running:
            return False
        if i > 0:
            dig_once(det)                     # another PERFECT dig (release on green)
            digs += 1
        wait_until(det.capacity_full, DIG_FILL_MS, confirm=1)
    log(f"    fill: still not full after {digs} digs -> proceed anyway")
    return det.capacity_full()


def return_and_dig(det):
    """Post-shake landing WITHOUT trusting the cue (it can stick on 'Shake').
    We trust the W-momentum put us near land and DIG as a probe -- a dig only
    fills on dirt, so the CAPACITY tells us the truth. A dig registers if the bar
    FILL RISES. We re-dig IN PLACE a few times before assuming we're off-land and
    nudging W (so a single non-registering dig can't cause a jittery nudge)."""
    State.no_full += 1
    if State.no_full >= NO_FULL_LIMIT:
        # We keep digging/nudging but the capacity bar NEVER reads full -- almost
        # always a mis-calibrated capacity pixel (common when auto-calibrate's
        # ratios don't match this PC's Roblox GUI scale). Stop loudly instead of
        # walking forward forever, and tell the user exactly what to fix.
        safe_stop("the pan never reads FULL -- the Capacity bar pixel looks "
                  "mis-calibrated. Open Calibrate and re-set the Capacity bar "
                  "(or Auto-calibrate with Roblox open).", hard=True)
        return False
    for rnd in range(LAND_DIG_TRIES):
        if not State.running:
            return False
        if rnd == 0 and PRE_DIG_SETTLE_MS > 0:
            sleep_ms(PRE_DIG_SETTLE_MS)      # let the landing settle before dig #1
        for t in range(DIG_INPLACE_TRIES):
            if not State.running:
                return False
            before = det.cap_fill()          # fill level BEFORE the dig
            dig_once(det)
            hit = wait_until(lambda: det.cap_fill() > before + CAP_RISE_FRAC
                             or det.capacity_full(), DIG_PROBE_MS, confirm=1)
            if hit:
                State.land_fails = 0
                State.breakouts = 0          # healthy progress -> clear escalation
                State.safe_retries = 0
                log(f"    dig-probe HIT (round {rnd+1}.{t+1}) -> on land, filling")
                _adapt_land(rnd > 0)         # needed nudges to land = a miss
                fill_to_full(det)            # dig until FULL (dynamic # of digs)
                return True
        # none of the in-place digs registered -> probably off land -> nudge fwd
        log(f"    no dig registered (round {rnd+1}) -> nudge W fwd")
        if State.stats: State.stats.nudges += 1
        key_down(KEY_W); sleep_ms(LAND_PROBE_NUDGE_MS); key_up(KEY_W)
        sleep_ms(PROBE_GAP_MS)               # settle before the next round
    State.land_fails += 1
    _adapt_land(True)
    log(f"    dig-probe: no land after {LAND_DIG_TRIES} rounds "
        f"(land_fails={State.land_fails})")
    if State.land_fails >= STUCK_LIMIT:
        safe_stop("dig-probe can't find land after shaking")
    return False


def act(det, s):
    """Capacity-primary decision. The cue is only trusted for 'am I in the water'
    (Pan), since the Shake/Deposit cues glitch. FULL + Pan -> shake; FULL else ->
    go to water; NOT full -> dig-probe to find land + refill."""
    if s.full:
        if s.pan:
            do_shake(det)                # FULL and in the water -> shake it out
        else:
            if EASY_WATER_RETURN_DELAY_MS > 0:
                sleep_ms(EASY_WATER_RETURN_DELAY_MS)
            go_water(det)                # FULL on land -> walk back to the water
    else:
        return_and_dig(det)              # empty -> DIG (probe) to find land


def recover(det, s):
    """Stronger corrective when stuck on the SAME situation for STUCK_TICKS.
    Recovery moves are JITTERED (short taps) so they can't sail past the target,
    even though the normal legs hold smoothly."""
    if State.stats:
        State.stats.recoveries += 1
        post_webhook("recovery", "🛠️ Recovering (got stuck, correcting)",
                     State.stats.as_dict())
    if s.full and s.pan:
        if SHAKE_RETRY_ENABLED:
            do_shake(det)                # full in water, won't empty -> shake again
    elif s.full:
        # full but not in water -> jitter S back toward the water
        if State.stats: State.stats.nudges += 1
        pulse_until(KEY_S, det.on_pan, RECOVER_BACK_MS)
    else:
        # empty, can't land -> jitter forward, then re-probe with a dig next tick
        if State.stats: State.stats.nudges += 1
        pulse_until(KEY_W, det.capacity_full, RECOVER_BACK_MS)


def break_out(det, s):
    """Last-resort escape when normal recovery keeps failing on the SAME spot.
    The usual cause is a shake that's ACTUALLY animating (so movement is locked --
    every 'go water' reports it couldn't move). You can't walk mid-shake, but
    HOLDING the mouse still finishes it, so we hold to drain it, then reposition
    forward to get off the water edge. After this we reset the counters and let
    normal logic try again ('do as if it was normal'). Returns True if it emptied."""
    log("    ** BREAK-OUT: click to finish any active shake, then reposition **")
    if s.full:
        end = time.perf_counter() + BREAKOUT_SHAKE_MS / 1000.0
        while time.perf_counter() < end and State.running:
            mouse_tap(SHAKE_CLICK_MS)
            if det.pan_empty():
                log("    break-out: clicks finished the shake -> pan empty")
                return True
            sleep_ms(SHAKE_CLICK_GAP_MS)
    # still stuck -> reposition forward (off the edge / onto land) for next try
    log("    break-out: reposition W fwd")
    key_down(KEY_W); sleep_ms(BREAKOUT_REPOS_MS); key_up(KEY_W)
    return False


class RelicScheduler:
    """Timed relic use (solar mags, idols, ...). Every relic's interval, PAUSE
    the cycle, switch to its hotbar slot, double-click to use it, switch back to
    the pan slot, and resume. Checked between supervisor ticks (a safe boundary);
    the self-healing supervisor re-senses afterwards, so this can't corrupt the
    cycle. Entirely additive: does nothing unless RELICS_ENABLED."""
    def __init__(self):
        self.reset()

    def reset(self):
        now = time.perf_counter()
        # schedule each relic's first fire one interval from the start
        self.next = {}
        for i, r in enumerate(RELICS):
            self.next[i] = now + r.get("minutes", 10) * 60.0

    def maybe_fire(self):
        if not RELICS_ENABLED or not State.running:
            return
        now = time.perf_counter()
        for i, r in enumerate(RELICS):
            due = self.next.get(i)
            if due is not None and now >= due:
                self._fire(r)
                self.next[i] = time.perf_counter() + r.get("minutes", 10) * 60.0

    def _fire(self, r):
        slot = int(r.get("slot", 0))
        clicks = int(r.get("clicks", 2))
        log(f"=== RELIC: use {r.get('name','relic')} (slot {slot}) ===")
        release_all()
        sleep_ms(RELIC_PRE_MS)
        tap_key(SLOT_KEYCODES.get(slot))         # switch to the relic slot
        sleep_ms(RELIC_SWITCH_MS)
        for _ in range(max(1, clicks)):          # use it (double-click)
            if not State.running:
                break
            mouse_tap(RELIC_CLICK_MS)
            sleep_ms(RELIC_CLICK_GAP_MS)
        sleep_ms(RELIC_SWITCH_MS)
        tap_key(SLOT_KEYCODES.get(RELIC_RETURN_SLOT))   # back to the pan
        sleep_ms(RELIC_SWITCH_MS)
        if State.stats: State.stats.relics_used += 1
        log("=== RELIC done -> resuming ===")


def treasure_tick(det):
    """TREASURE CHEST mode (no shake): dig briefly, then strafe sideways -- D,
    then A, alternating -- until the Collect/Deposit cue appears again, then dig.
    Reuses the Deposit pixel (calibrate it on the 'Collect' prompt)."""
    release_all()
    for _i in range(max(1, TREASURE_DIGS)):   # do N digs (let the slow dig finish)
        if not State.running:
            return
        mouse_tap(TREASURE_DIG_MS)            # quick dig / open the chest
        if TREASURE_DIG_GAP_MS > 0:
            sleep_ms(TREASURE_DIG_GAP_MS)
    if State.stats:
        State.stats.cycles += 1
    if not State.running:
        return
    side = KEY_D if State.t_side == 0 else KEY_A
    State.t_side ^= 1                         # alternate L/R each chest
    # Hold the side key: first move OFF this spot (until the Pan cue), then KEEP
    # holding until the next Collect cue -- so we can't re-trigger on the same spot.
    key_down(side)
    left = wait_until(det.on_pan, TREASURE_MOVE_MAX_MS, confirm=1)
    reached = wait_until(det.on_deposit, TREASURE_MOVE_MAX_MS, confirm=1)
    key_up(side)
    log(f"    treasure: {TREASURE_DIGS} dig(s), held {'D' if side == KEY_D else 'A'} "
        f"(pan={left} -> collect={reached})")


class Supervisor:
    """Tick-based brain. Re-senses, acts, and watches for deadlock. Reset on
    every start so a fresh run can't inherit a stale stuck-count."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.last_sig = None
        self.same = 0
        self.recoveries = 0
        State.shake_fails = 0
        State.land_fails = 0
        State.breakouts = 0
        State.x_balance = 0.0
        State.no_full = 0

    def tick(self, det):
        if State.want_reset:           # fresh slate after a safe-pause
            self.reset()
            State.want_reset = False
        release_all()                  # clear any stray held key/mouse (anti-drift)
        s = sense_stable(det)
        sig = (s.where, s.contents)
        if s.full:
            State.no_full = 0            # capacity 'full' detected -> detection OK
        if sig == self.last_sig:
            self.same += 1
        else:
            self.same, self.last_sig, self.recoveries = 0, sig, 0
        cue = f"{'D' if s.dep else '-'}{'P' if s.pan else '-'}{'S' if s.shk else '-'}"
        cap = f"{'F' if s.full else '-'}{'E' if s.empty else '-'}"
        # oscillation guard: if shakes keep failing even while we move around
        # (so the per-situation watchdog never trips), stop after a hard cap.
        if State.shake_fails > SHAKE_FAIL_LIMIT:
            safe_stop(f"shake not emptying after {State.shake_fails} tries")
            return
        if self.same < STUCK_TICKS:
            log(f"{s.where:7}/{s.contents:7} cue[{cue}] cap[{cap}] "
                f"-> {plan_label(s)}")
            act(det, s)
        elif RECOVER_ENABLED and self.recoveries < RECOVER_LIMIT:
            self.recoveries += 1
            log(f"{s.where:7}/{s.contents:7} cue[{cue}] cap[{cap}] "
                f"** STUCK x{self.same}, RECOVER #{self.recoveries} **")
            recover(det, s)
            self.same = 0
        elif BREAKOUT_ENABLED:
            # recovered enough and still stuck -> SMART BREAK-OUT: finish any active
            # (movement-locking) shake by clicking + reposition, then reset and let
            # normal logic try again. SAFE STOP only if the break-out keeps failing.
            State.breakouts += 1
            log(f"{s.where:7}/{s.contents:7} cue[{cue}] cap[{cap}] "
                f"** BREAK-OUT #{State.breakouts} (recovery loop) **")
            if State.breakouts > BREAKOUT_LIMIT:
                safe_stop(f"stuck at {s.where}/{s.contents} after break-outs")
                return
            if break_out(det, s):
                State.breakouts = 0          # progress -> clear the escalation
            # reset the watchdog so normal logic gets a fresh attempt next tick
            self.same = 0
            self.recoveries = 0
            self.last_sig = None
        else:
            # recovery systems are all disabled -> just retry the normal action.
            # The per-situation guards (shake/land fail limits) still safe-stop if
            # it genuinely can't make progress.
            log(f"{s.where:7}/{s.contents:7} cue[{cue}] cap[{cap}] "
                f"** STUCK x{self.same} (recovery off) -> retry **")
            act(det, s)
            self.same = 0


def loop_step(detector):
    # Without terrain detection we can't reason about position: fall back to the
    # simple linear loop (dig, and empty when the capacity bar fills).
    if not USE_TERRAIN_DETECT:
        # MANUAL: do a fixed number of digs, then empty. No capacity detection
        # for the dig phase -- you told it how many digs fill the pan.
        if MANUAL_DIG:
            # ERROR CHECK: if the pan is ALREADY full (e.g. a previous empty
            # failed), don't dig (it does nothing) -- empty it first.
            if detector.capacity_full():
                run_empty(detector)
                return
            # dig the required number of times
            for i in range(DIGS_PER_CYCLE):
                if not State.running: return
                # dig, then move the moment the bar STARTS (no wait for full).
                dig_until_started(detector)
                if i < DIGS_PER_CYCLE - 1:
                    sleep_ms(BETWEEN_DIGS_MS)
            run_empty(detector)             # empty + VERIFY (retries / safe-stop)
            return
        # AUTO: dig until the capacity bar reads full (uses detection).
        if detector.capacity_full():        # already full (incl. failed empty)
            empty_sequence(detector)
            sleep_ms(RETRY_GAP_MS)
            return
        dig_once(detector)
        if not State.running: return
        sleep_ms(CAP_CHECK_MS)              # brief settle so the dig registers
        if detector.capacity_full():
            empty_sequence(detector)        # full -> walk back NOW (no extra wait)
        else:
            sleep_ms(BETWEEN_DIGS_MS)       # not full -> normal between-dig pause
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
    load_config()                 # apply UI overrides from prospecting_config.json
    if not apply_auto_calibrate():  # place pixels from the live window (if profile)
        apply_window_offset()       # else shift pixels if the window moved (opt-in)
    print(__doc__.split("SETUP")[0])
    print(f"Dig trigger pixel {DIG_TRIGGER_PIXEL} (white-line release).")
    print(f"Capacity-full pixel {CAP_FULL_PIXEL} (yellow = full).")
    print(f"Press {TOGGLE_NAME} to start/stop, Esc to quit.\n")

    listener = make_listener()
    listener.start()

    gc.enable()   # keep GC ON: disabling it grew memory + slowed long runs
    with _MSS() as sct:
        detector = Detector(sct)
        for _ in range(5):                  # warm up capture path
            sct.grab(detector.dig_region)
        sup = Supervisor()
        relics = RelicScheduler()
        was_running = False
        last_emit = 0.0
        last_wh_stats = 0.0
        try:
            while State.alive:
                if State.running:
                    if not was_running:     # fresh start -> clear stuck-counters
                        sup.reset()
                        State.t_side = 0
                        relics.reset()      # start relic timers from now
                        State.stats = SessionStats()
                        last_emit = last_wh_stats = time.perf_counter()
                        was_running = True
                        log("=== RUNNING (live trace below) ===")
                        post_webhook("start", "▶️ Macro started",
                                     State.stats.as_dict())
                    relics.maybe_fire()     # timed relic use (no-op unless enabled)
                    if TREASURE_MODE:
                        treasure_tick(detector)
                    else:
                        sup.tick(detector)
                    now = time.perf_counter()
                    # live stats line for the app (parsed there, also harmless log)
                    if now - last_emit >= 2.0:
                        last_emit = now
                        print("__STATS__ " + json.dumps(State.stats.as_dict()),
                              flush=True)
                    # periodic webhook stats update
                    if (WEBHOOK_STATS_MIN > 0
                            and now - last_wh_stats >= WEBHOOK_STATS_MIN * 60):
                        last_wh_stats = now
                        s = State.stats.as_dict()
                        post_webhook("stats",
                                     f"📊 {s['cycles']} pans · {s['pans_per_hr']}/hr "
                                     f"· {s['runtime_s']//60} min", s)
                    # auto-stop timer
                    if (AUTOSTOP_ENABLED and State.stats
                            and State.stats.runtime() >= AUTOSTOP_MINUTES * 60):
                        log(f"=== AUTO-STOP after {AUTOSTOP_MINUTES} min ===")
                        State.stop_reason = "auto"
                        if State.stats: State.stats.stop_reason = "auto"
                        post_webhook("autostop", f"⏱️ Auto-stopped after "
                                     f"{AUTOSTOP_MINUTES} min", State.stats.as_dict())
                        State.running = False
                        release_all()
                    # bag-full guard (stop after N pans)
                    if (STOP_AFTER_PANS > 0 and State.stats
                            and State.stats.cycles >= STOP_AFTER_PANS):
                        log(f"=== STOP after {STOP_AFTER_PANS} pans (bag-full guard) ===")
                        State.stop_reason = "bag-full"
                        if State.stats: State.stats.stop_reason = "bag-full"
                        post_webhook("bag_full", f"🎒 Stopped after "
                                     f"{STOP_AFTER_PANS} pans (bag likely full)",
                                     State.stats.as_dict())
                        State.running = False
                        release_all()
                else:
                    if was_running:
                        reason = ((State.stats.stop_reason if State.stats
                                   and State.stats.stop_reason else State.stop_reason)
                                  or "manual")
                        if State.stats:
                            State.stats.stop_reason = reason
                        log(f"=== STOPPED ({reason}) ===")
                        if reason == "manual" and State.stats:
                            post_webhook("stop", "⏹️ Macro stopped (manual)",
                                         State.stats.as_dict())
                        State.stop_reason = ""
                        State.safe_retries = 0
                    was_running = False
                    time.sleep(0.02)
        except Exception as _e:
            post_webhook("error", f"❌ Macro error: {_e}",
                         State.stats.as_dict() if State.stats else None)
            raise
        finally:
            release_all()
            gc.enable()
            print("Stopped, all inputs released.")


def monitor():
    """Live sensor readout, NO input sent. Verify the cues/capacity read right
    on land, in the water, and mid-shake. Ctrl+C to quit."""
    load_config()                 # apply UI overrides from prospecting_config.json
    if not apply_auto_calibrate():
        apply_window_offset()
    print("MONITOR -- no input. Watch the values on land / in water / mid-shake.\n")
    with _MSS() as sct:
        det = Detector(sct)
        try:
            while True:
                s = sense_stable(det)
                dep = "DEP " if s.dep else "----"
                pan = "PAN " if s.pan else "----"
                shk = "SHAKE" if s.shk else "-----"
                full = "FULL" if s.full else "----"
                empt = "EMPTY" if s.empty else "fill "
                print(f"\rcue:[{dep}{pan}{shk}] cap:[{full} {empt}]  "
                      f"=> {s.where:7}/{s.contents:7} plan: {plan_label(s):18}",
                      end="", flush=True)
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\nDone.")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "calibrate":
        calibrate()
    elif arg == "calibrate-text":
        calibrate_text()
    elif arg == "log":
        log_calibration()
    elif arg == "monitor":
        monitor()
    else:
        main()
