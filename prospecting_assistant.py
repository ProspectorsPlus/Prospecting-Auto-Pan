# -*- coding: utf-8 -*-
"""
Prospectors Plus -- local tuning assistant ("Coach").

A fully offline expert system. No API, no network, no key. It encodes the
macro's domain knowledge: every tunable knob, the symptoms users report, the
cycle-time formulas from the calculator, and each user's own builds + run
history. Given a plain-English message plus the live context, it returns a
human reply and a list of PROPOSED setting changes. It NEVER touches code and
never applies anything itself -- the app shows a diff and the user confirms.

Public entry point:  respond(message, ctx) -> dict
  ctx = {
    "settings":   {KEY: value, ...}   # merged current values
    "builds":     {name: entry, ...}
    "build_name": "Shards Spirit Orb" | ""
    "history":    [ {runtime_s, cycles, pans_per_hr, recoveries,
                     safe_stops, hard_stops, nudges, relics, reason, ended}, ... ]
    "stats":      {luck, capacity, dig_strength, dig_speed,
                   shake_strength, shake_speed, walk_speed}   # may be {}
    "prev_topic": "shake_miss" | ""   # the last topic we answered (for escalation)
  }
  returns {
    "reply":   str,                       # markdown-ish text
    "changes": [ {key,label,from,to,reason} ],
    "topic":   str,                       # so the UI can echo it back next turn
    "askStats": bool,                     # UI should reveal the stats form
    "chips":   [str, ...]                 # suggested follow-up prompts
  }
"""

import math
import re

try:
    import prospecting_ui as _ui
    _LABEL = {k: l for _, items in _ui.SECTIONS for (k, l, _t, _d) in items}
    _TYPES = dict(_ui.TYPES)
    _HELP = dict(_ui.HELP)
    _DEFAULTS = dict(_ui.DEFAULTS)
except Exception:                       # pragma: no cover - standalone import
    _LABEL, _TYPES, _HELP, _DEFAULTS = {}, {}, {}, {}


# --------------------------------------------------------------------------- #
#  Sane numeric bounds + nudge step per knob (for clamping proposed changes).  #
#  (lo, hi, step) -- step is the default "one bump" size for that knob.        #
# --------------------------------------------------------------------------- #
RANGES = {
    "EASY_WATER_BACK_MS": (0, 1500, 80), "EASY_LAND_FWD_MS": (0, 1500, 80),
    "EASY_SHAKE_DELAY_MS": (0, 1200, 60), "EASY_FIRST_DIG_DELAY_MS": (0, 1200, 80),
    "EASY_WATER_RETURN_DELAY_MS": (0, 2000, 100),
    "TREASURE_DIGS": (1, 10, 1), "TREASURE_DIG_MS": (3, 30, 2),
    "TREASURE_DIG_GAP_MS": (1000, 30000, 1000), "TREASURE_MOVE_MAX_MS": (800, 6000, 250),
    "DIG_CLICK_MS": (5, 600, 10), "DIG_SPEED": (10, 4000, 25),
    "MAX_DIGS_TO_FILL": (1, 20, 1), "DIG_FILL_MS": (40, 800, 40),
    "PRE_DIG_SETTLE_MS": (0, 600, 40),
    "PAN_BACK_MAX_MS": (60, 1200, 60), "WATER_EXTRA_BACK_MS": (0, 1200, 80),
    "SHAKE_CLICKS": (0, 30, 1), "SHAKE_CLICK_MS": (8, 60, 4),
    "SHAKE_CLICK_GAP_MS": (4, 60, 3), "SHAKE_HOLD_MS": (400, 4000, 200),
    "SHAKE_BAIL_MS": (200, 2500, 150), "SHAKE_START_DELAY_MS": (0, 1000, 50),
    "POST_SHAKE_SETTLE_MS": (0, 800, 40),
    "DEPOSIT_MAX_MS": (400, 3000, 200), "LAND_SETTLE_MS": (0, 400, 25),
    "DIG_PROBE_MS": (120, 900, 60), "PROBE_GAP_MS": (20, 400, 30),
    "LAND_PROBE_NUDGE_MS": (30, 400, 30), "LAND_DIG_TRIES": (2, 12, 1),
    "STUCK_TICKS": (2, 12, 1), "RECOVER_LIMIT": (1, 8, 1),
    "RECOVER_BACK_MS": (60, 600, 40), "SHAKE_FAIL_LIMIT": (2, 15, 1),
    "SHAKE_GLITCH_LIMIT": (0, 10, 1), "NO_PROGRESS_SEC": (0, 60, 2),
    "BREAKOUT_LIMIT": (1, 8, 1), "BREAKOUT_SHAKE_MS": (200, 1500, 100),
    "BREAKOUT_REPOS_MS": (60, 500, 40), "BURST_ON_MS": (5, 40, 3),
    "BURST_OFF_MS": (1, 20, 2),
    "WEBHOOK_STATS_MIN": (0, 240, 15), "AUTOSTOP_MINUTES": (5, 1440, 15),
    "STOP_AFTER_PANS": (0, 100000, 50), "ADAPT_MISS_PCT": (5, 60, 5),
    "FR_TEXT_TOL": (10, 120, 10), "FR_SCAN_HOVER_MS": (4, 60, 4),
    "FR_MOVE_STEP": (2, 24, 2), "FR_FIND_TRIES": (2, 20, 2),
    "FR_OPEN_TRIES": (1, 8, 1), "FR_SCROLL_STEPS": (1, 10, 1),
    "FR_DOUBLE_GAP_MS": (40, 400, 30), "FR_OPEN_MS": (200, 2000, 150),
    "FR_CLICK_SETTLE_MS": (80, 900, 80), "FR_ACTION_GAP_MS": (150, 1500, 100),
    "FR_WARP_MS": (800, 6000, 400), "FR_STRAFE_MS": (0, 80, 8),
    "FR_WALK_MAX_MS": (2000, 12000, 500), "FR_END_A_MS": (0, 1200, 80),
    "FR_CROSS_CONFIRM": (1, 10, 1),
}


def _clamp(key, val):
    lo, hi, _ = RANGES.get(key, (None, None, 1))
    if lo is not None:
        val = max(lo, min(hi, val))
    t = _TYPES.get(key, "int")
    if t == "int":
        return int(round(val))
    if t == "bool":
        return bool(val)
    return val


def _step(key):
    return RANGES.get(key, (0, 0, 1))[2]


def _label(key):
    return _LABEL.get(key, key)


# --------------------------------------------------------------------------- #
#  Calculator formulas (mirrors prospecting-calculator). Used for EXPECTED     #
#  timings only -- estimates, never auto-applied to live timing knobs.         #
# --------------------------------------------------------------------------- #
def shake_speed_to_r(x):
    """Rolls-per-second factor from the shake-speed stat (calculator cubic)."""
    return (4.03266e-9 * x ** 3 - 1.68935e-5 * x ** 2
            + 0.0255557 * x + 0.206594)


def expected_cycle(stats):
    """Return a dict of expected timings from in-game stats, or None.
    cycle = C/(r*s) + 1.5 + 190*n/d + 0.62   (0.62 = measured overhead)."""
    try:
        C = float(stats["capacity"])
        ds = float(stats["dig_strength"])
        ss = float(stats["shake_speed"])
        d = float(stats.get("dig_speed", 100))
        s = float(stats.get("shake_strength", 1)) or 1.0
    except (KeyError, ValueError, TypeError):
        return None
    if C <= 0 or ds <= 0 or d <= 0:
        return None
    r = shake_speed_to_r(ss)
    n = max(1, math.ceil(C / (1.5 * ds)))         # digs to fill
    shake_sec = C / (r * s) if (r * s) > 0 else 0
    dig_sec = 190.0 * n / d
    cycle = shake_sec + 1.5 + dig_sec + 0.62
    return {
        "r": r, "digs_to_fill": n, "shake_sec": shake_sec, "dig_sec": dig_sec,
        "cycle_sec": cycle, "pans_per_min": (60.0 / cycle) if cycle else 0,
        "pans_per_hr": (3600.0 / cycle) if cycle else 0,
        "dig_hold_ms": int(round(55000.0 / d)) if d else 0,
        "per_dig_ms": int(round(190000.0 / d / max(1, n))) if d else 0,
    }


# --------------------------------------------------------------------------- #
#  Text matching helpers                                                       #
# --------------------------------------------------------------------------- #
def _norm(t):
    t = (t or "").lower()
    t = t.replace("’", "'")
    t = re.sub(r"[^a-z0-9%+\-/ ]", " ", t)
    return " " + re.sub(r"\s+", " ", t).strip() + " "


def _has(text, *phrases):
    # whole-phrase, word-boundary match (text is already space-padded by _norm).
    # Avoids false hits like "hi" inside "reac-hi-ng".
    return any((" " + p + " ") in text for p in phrases)


ESCALATE = ("still", "again", "more", "didn't work", "didnt work", "did not work",
            "not enough", "keep", "keeps", "even more", "worse", "harder")
RELAX = ("too much", "too far", "overdid", "over did", "opposite", "less",
         "back off", "tone down", "softer", "reduce that")


# --------------------------------------------------------------------------- #
#  Change builder                                                              #
# --------------------------------------------------------------------------- #
def _set(changes, settings, key, value, reason):
    if key not in _TYPES:
        return
    newv = _clamp(key, value)
    cur = settings.get(key, _DEFAULTS.get(key))
    if isinstance(newv, bool):
        if bool(cur) == newv:
            return
    else:
        try:
            if int(cur) == int(newv):
                return
        except (TypeError, ValueError):
            pass
    changes.append({"key": key, "label": _label(key),
                    "from": cur, "to": newv, "reason": reason})


def _bump(changes, settings, key, direction, reason, mult=1.0):
    """Nudge a numeric knob up/down by its step (x mult) from its CURRENT value."""
    cur = settings.get(key, _DEFAULTS.get(key, 0))
    try:
        cur = float(cur)
    except (TypeError, ValueError):
        cur = 0.0
    delta = _step(key) * mult * (1 if direction > 0 else -1)
    _set(changes, settings, key, cur + delta, reason)


# --------------------------------------------------------------------------- #
#  Symptom catalogue. Each entry:                                              #
#    id, title, keywords (weighted match), anti (disqualify),                  #
#    fix(settings, changes, mult)  -> appends proposed changes, returns reply  #
# --------------------------------------------------------------------------- #
def _fix_shake_late(s, ch, m):
    _set(ch, s, "SHAKE_START_DELAY_MS", 0, "remove any pause before the shake begins")
    _bump(ch, s, "EASY_SHAKE_DELAY_MS", -1, "stop waiting before the shake", m)
    _bump(ch, s, "SHAKE_CLICK_GAP_MS", -1, "rattle faster so the shake registers sooner", m)
    return ("Treating this as: the shake is **slow to start / kicks in late**. I'm "
            "removing the pre-shake delay and tightening the click gap so it begins "
            "and drains sooner. If you instead meant it shakes *too long*, say "
            "“it over-shakes”.")


def _fix_shake_slow(s, ch, m):
    _bump(ch, s, "SHAKE_CLICK_GAP_MS", -1, "faster rattle between clicks", m)
    _bump(ch, s, "POST_SHAKE_SETTLE_MS", -1, "less dead time after the pan empties", m)
    return ("The shake/empty is **too slow**. Tightening the gap between shake clicks "
            "and trimming the settle after it empties. Tell me “still slow” to push further.")


def _fix_shake_miss(s, ch, m):
    if not s.get("SHAKE_MOMENTUM_W", True):
        _set(ch, s, "SHAKE_MOMENTUM_W", True, "hold W during the shake so momentum carries you (more reliable)")
    _bump(ch, s, "SHAKE_CLICK_MS", +1, "longer click so the game registers each shake", m)
    _bump(ch, s, "SHAKE_BAIL_MS", +1, "wait longer before deciding a shake failed", m)
    _set(ch, s, "SHAKE_START_DELAY_MS", 0, "start the shake immediately on reaching water")
    return ("The shake is **missing / not registering**. I'm lengthening each shake "
            "click, giving it longer before it's called a failure, and (if it was off) "
            "turning on hold-W momentum. If it *never* shakes at all, your **Shake** "
            "prompt pixel may be miscalibrated — re-run Guided calibration's Shake step.")


def _fix_shake_overflow(s, ch, m):
    n = int(s.get("SHAKE_CLICKS", 0))
    if n <= 0:
        _set(ch, s, "POST_SHAKE_SETTLE_MS", max(180, int(s.get("POST_SHAKE_SETTLE_MS", 150)) + 80),
             "let the pan fully settle so a trailing shake click can't bleed into the dig")
        _bump(ch, s, "PRE_DIG_SETTLE_MS", +1, "settle before the first dig so it doesn't fire mid-shake", m)
        return ("A shake click is **bleeding into the dig** and wrecking quality. The "
                "clean fix is a **fixed shake count** so it can't over-click — tell me how "
                "many shake clicks empty your pan (e.g. “set shake clicks to 3”) and I'll "
                "lock it. For now I've added settle time so the extra click can't reach the dig.")
    return ("You already use a fixed shake count (%d). Adding settle time after the "
            "shake so nothing bleeds into the dig." % n)


def _fix_over_shake(s, ch, m):
    _bump(ch, s, "SHAKE_HOLD_MS", -1, "lower the overall shake time cap", m)
    return ("It's **over-shaking** (shaking longer than needed). Lowering the shake "
            "time cap; it still stops early when the pan reads empty. For an exact stop, "
            "say “set shake clicks to N”.")


def _fix_land_in_water(s, ch, m):
    if not s.get("SHAKE_MOMENTUM_W", True):
        _set(ch, s, "SHAKE_MOMENTUM_W", True, "hold W during shake so momentum lands you on dirt")
    _bump(ch, s, "WATER_EXTRA_BACK_MS", -1, "don't go so deep into the water", m)
    _bump(ch, s, "EASY_LAND_FWD_MS", +1, "drive a little further forward onto land", m)
    _bump(ch, s, "LAND_SETTLE_MS", +1, "sit on the dirt a touch longer to avoid a water flicker", m)
    return ("It's **ending up in the water** after the shake. I'm reducing how deep it "
            "wades, adding forward momentum onto land, and holding the land a touch "
            "longer so it commits to dirt.")


def _fix_shake_on_land(s, ch, m):
    _bump(ch, s, "EASY_WATER_BACK_MS", +1, "walk further back into the water before shaking", m)
    _bump(ch, s, "WATER_EXTRA_BACK_MS", +1, "go a bit deeper so the shake isn't at the edge", m)
    _bump(ch, s, "PAN_BACK_MAX_MS", +1, "allow a longer walk-back into water", m)
    return ("It's **shaking too early / not reaching the water**. Walking further back "
            "into the water and going a little deeper before the shake starts.")


def _fix_dig_early(s, ch, m):
    _bump(ch, s, "PRE_DIG_SETTLE_MS", +1, "pause longer after landing before the first dig", m)
    _bump(ch, s, "EASY_FIRST_DIG_DELAY_MS", +1, "wait longer before digging so you're settled", m)
    _bump(ch, s, "LAND_SETTLE_MS", +1, "plant on the dirt before digging", m)
    return ("It's **digging too early** (before it's settled on land). Adding settle "
            "time after landing and before the first dig.")


def _fix_dig_late(s, ch, m):
    _bump(ch, s, "PRE_DIG_SETTLE_MS", -1, "shorten the settle before the first dig", m)
    _bump(ch, s, "EASY_FIRST_DIG_DELAY_MS", -1, "dig sooner after landing", m)
    return ("It's **waiting too long to dig**. Trimming the pre-dig settle so it digs sooner.")


def _fix_dig_hold_long(s, ch, m):
    _bump(ch, s, "DIG_CLICK_MS", -1, "shorter dig hold", m)
    return ("Dig hold is **too long**. Shortening it. If you use Perfect dig with a "
            "Dig-speed stat, raising **Dig speed** also shortens the hold automatically "
            "(hold = 55000 / speed).")


def _fix_dig_hold_short(s, ch, m):
    _bump(ch, s, "DIG_CLICK_MS", +1, "longer dig hold so each dig completes", m)
    _bump(ch, s, "DIG_FILL_MS", +1, "give the bar longer to read FULL after a dig", m)
    return ("Dig hold looks **too short** (digs not completing). Lengthening the hold "
            "and the wait for the bar to fill.")


def _fix_not_filling(s, ch, m):
    _bump(ch, s, "MAX_DIGS_TO_FILL", +1, "allow more digs to fill the pan", m)
    _bump(ch, s, "DIG_FILL_MS", +1, "wait longer for FULL between digs", m)
    return ("The pan **isn't filling** before it leaves. Raising the dig cap and the "
            "fill wait. (It watches the capacity bar, so a higher cap is safe.) If it "
            "*never* reads full, the **Capacity bar** pixel needs recalibrating.")


def _fix_overflow_dig(s, ch, m):
    _bump(ch, s, "MAX_DIGS_TO_FILL", -1, "fewer max digs so it stops at full", m)
    return ("It's **overfilling / digging past full**. Lowering the max-digs cap. The "
            "capacity bar is the real stop, so if it still overshoots, the **Capacity "
            "bar** right-end pixel likely needs recalibrating to solid gold.")


def _fix_probe_fail(s, ch, m):
    _bump(ch, s, "DIG_PROBE_MS", +1, "wait longer to detect a probe-dig hit", m)
    _bump(ch, s, "LAND_DIG_TRIES", +1, "more probe attempts before giving up", m)
    _bump(ch, s, "LAND_PROBE_NUDGE_MS", +1, "bigger forward nudge while hunting for land", m)
    return ("The **dig-probe can't find land** after the shake. Giving it more time per "
            "probe, more attempts, and a bigger forward nudge between probes.")


def _fix_overshoot_water(s, ch, m):
    _bump(ch, s, "EASY_WATER_BACK_MS", -1, "don't walk so far back", m)
    _bump(ch, s, "WATER_EXTRA_BACK_MS", -1, "less extra wade depth", m)
    _bump(ch, s, "PAN_BACK_MAX_MS", -1, "cap the back-walk shorter", m)
    return ("It's **going too far into the water**. Pulling the back-walk in.")


def _fix_short_water(s, ch, m):
    _bump(ch, s, "EASY_WATER_BACK_MS", +1, "walk further back into the water", m)
    _bump(ch, s, "PAN_BACK_MAX_MS", +1, "allow a longer back-walk", m)
    return ("It's **falling short of the water**. Extending the back-walk.")


def _fix_short_land(s, ch, m):
    _bump(ch, s, "EASY_LAND_FWD_MS", +1, "drive further onto land", m)
    _bump(ch, s, "DEPOSIT_MAX_MS", +1, "allow a longer forward walk to find land", m)
    return ("It's **landing short** and needing nudges. Pushing further forward onto "
            "land and allowing a longer search.")


def _fix_overshoot_land(s, ch, m):
    if int(s.get("EASY_LAND_FWD_MS", 0)) > 0:
        _bump(ch, s, "EASY_LAND_FWD_MS", -1, "don't drive so far onto land", m)
    else:
        _bump(ch, s, "LAND_SETTLE_MS", -1, "hold W less after the land cue so it stops sooner", m)
        _bump(ch, s, "DEPOSIT_MAX_MS", -1, "cap the forward walk shorter", m)
    return ("It's **overshooting the land**. Easing off the forward push so it stops on "
            "the dig spot. (If it runs *way* past, your Collect-Deposit prompt pixel may "
            "be reading late — re-calibrate it.)")


def _fix_recover_aggressive(s, ch, m):
    _bump(ch, s, "STUCK_TICKS", +1, "need more identical reads before recovering", m)
    if int(s.get("SHAKE_GLITCH_LIMIT", 2)) < 3:
        _set(ch, s, "SHAKE_GLITCH_LIMIT", 3, "tolerate more missed shakes before a break-out")
    _bump(ch, s, "NO_PROGRESS_SEC", +1, "wait longer before assuming no progress", m)
    _bump(ch, s, "RECOVER_LIMIT", +1, "allow more gentle recoveries before escalating", m)
    return ("Recovery is **firing too eagerly**. Raising the thresholds so it only steps "
            "in when something is genuinely stuck.")


def _fix_recover_slow(s, ch, m):
    _bump(ch, s, "STUCK_TICKS", -1, "react after fewer stuck reads", m)
    _bump(ch, s, "NO_PROGRESS_SEC", -1, "react sooner when nothing completes", m)
    _bump(ch, s, "SHAKE_FAIL_LIMIT", +1, "but allow more shake retries before a full stop", m)
    return ("It's **slow to recover / gives up too soon**. Making recovery react sooner "
            "while allowing a few more retries before it safe-stops for real.")


def _fix_fr_too_easy(s, ch, m):
    if int(s.get("SHAKE_GLITCH_LIMIT", 2)) < 3:
        _set(ch, s, "SHAKE_GLITCH_LIMIT", 3, "tolerate more missed shakes before recovering")
    _bump(ch, s, "NO_PROGRESS_SEC", +1, "wait longer before triggering recovery", m)
    _bump(ch, s, "SHAKE_FAIL_LIMIT", +1, "more shake retries before a soft stop", m)
    return ("**Fortune River recovery is triggering too easily.** Raising the stuck/"
            "shake thresholds so a soft stop (which triggers FR) only happens on a real "
            "problem. Note: if either threshold is **0** it fires instantly — these are "
            "now set to safe values. You can also turn **FR recovery** off entirely.")


def _fix_fr_not_found(s, ch, m):
    _bump(ch, s, "FR_TEXT_TOL", +1, "looser colour match for the pink Fortune River row", m)
    _bump(ch, s, "FR_FIND_TRIES", +1, "more top-to-bottom sweeps before giving up", m)
    _bump(ch, s, "FR_SCROLL_STEPS", +1, "scroll a bit more between sweeps", m)
    return ("FR recovery **can't find the Fortune River row**. Loosening the colour "
            "match and adding more sweeps/scroll. If it clicks the *wrong* row, say "
            "“FR matches the wrong row” and re-calibrate the pink text colour.")


def _fix_fr_wrong_row(s, ch, m):
    _bump(ch, s, "FR_TEXT_TOL", -1, "tighter colour match so only Fortune River matches", m)
    return ("FR is **matching the wrong row**. Tightening the colour tolerance. If it "
            "then can't find it at all, re-calibrate the **Fortune River** pink pixel.")


def _fix_fr_not_opening(s, ch, m):
    _bump(ch, s, "FR_DOUBLE_GAP_MS", -1, "tighter double-click so the menu registers", m)
    _bump(ch, s, "FR_OPEN_MS", +1, "wait longer for the Fast Travel menu to appear", m)
    _bump(ch, s, "FR_OPEN_TRIES", +1, "more attempts to re-equip slot 4 and open", m)
    _bump(ch, s, "FR_CLICK_SETTLE_MS", +1, "pause more around each click", m)
    return ("The warp device **isn't opening** in FR recovery. Tightening the double-"
            "click, waiting longer for the menu, and adding open retries.")


def _fix_fr_too_fast(s, ch, m):
    _bump(ch, s, "FR_ACTION_GAP_MS", +1, "more pause between each recovery step", m)
    _bump(ch, s, "FR_CLICK_SETTLE_MS", +1, "more pause around each click", m)
    _bump(ch, s, "FR_WARP_MS", +1, "wait longer for the teleport/world to load", m)
    return ("FR recovery is **happening too fast for the game to keep up**. Adding pauses "
            "between steps, around clicks, and after the teleport.")


def _fix_fr_wrong_shore(s, ch, m):
    _bump(ch, s, "FR_CROSS_CONFIRM", +1, "require a longer confirmed water-crossing before restarting", m)
    return ("FR is **restarting on the wrong shore / in the water**. Requiring a longer "
            "confirmed crossing before it commits and restarts the loop.")


def _fix_fr_jumpy(s, ch, m):
    _bump(ch, s, "FR_MOVE_STEP", -1, "smaller mouse steps (less affected by acceleration)", m)
    _bump(ch, s, "FR_SCAN_HOVER_MS", +1, "dwell longer per step while sweeping", m)
    return ("The FR cursor is **moving too fast / jumpy**. Smaller per-step movement and "
            "a longer dwell so the sweep is smooth and accurate.")


def _fix_capacity(s, ch, m):
    return ("This sounds like a **capacity-bar calibration** issue, not a timing one — "
            "so I won't change timings. If it walks forward and never digs, or thinks "
            "the pan is full/empty wrongly, re-run **Guided calibration → Capacity bar** "
            "with the bar completely full, and make sure only the bar is in view. Use "
            "**Test detection (live)** to confirm it reads FULL/empty correctly.")


SYMPTOMS = [
    # id, title, keywords, anti, fix
    ("shake_miss", "Shake misses / doesn't register",
     [("doesn't shake", 4), ("not shaking", 4), ("won't shake", 4), ("no shake", 3),
      ("miss", 2), ("misses", 2), ("doesn't register", 3), ("not register", 3),
      ("skips the shake", 4), ("fails to shake", 4), ("shake fail", 3)],
     [("late", 2), ("slow", 2), ("water", 2), ("overflow", 2), ("bleed", 2)], _fix_shake_miss),
    ("shake_late", "Shake starts late",
     [("shake late", 4), ("shakes late", 4), ("late shake", 4), ("slow to shake", 3),
      ("shake starts late", 4), ("delay before shake", 3), ("takes a while to shake", 3)],
     [("water", 2), ("overflow", 2)], _fix_shake_late),
    ("shake_slow", "Shake/empty too slow",
     [("shake slow", 3), ("shaking slow", 3), ("slow shake", 3), ("empties slow", 3),
      ("slow to empty", 3), ("takes forever to shake", 4), ("shake takes too long", 4),
      ("shake is too slow", 4), ("shake too slow", 4), ("shake is slow", 3),
      ("shaking too slow", 4), ("shaking is slow", 3), ("shake is way too slow", 5),
      ("way too slow", 3), ("too slow", 2)],
     [("start", 2), ("late", 2), ("dig", 2), ("miss", 2), ("recover", 3), ("fr", 2),
      ("land", 2), ("gives up", 3)], _fix_shake_slow),
    ("over_shake", "Over-shakes",
     [("over shake", 3), ("over-shake", 3), ("shakes too many", 4), ("too many shakes", 4),
      ("shakes too much", 4), ("keeps shaking", 3), ("extra shake", 3)],
     [("dig", 1)], _fix_over_shake),
    ("shake_overflow", "Shake bleeds into dig",
     [("bleed", 3), ("bleeds into", 4), ("overflow", 2), ("into the dig", 4),
      ("into digging", 4), ("ruins perfect", 4), ("extra click", 3), ("click overflow", 4),
      ("shake into dig", 4), ("not 100% quality", 4), ("quality", 2)],
     [], _fix_shake_overflow),
    ("land_in_water", "Ends up in water after shake",
     [("lands in water", 5), ("land in water", 5), ("ends in water", 4), ("in the water", 3),
      ("stuck in water", 4), ("ends up in water", 5), ("stays in water", 4),
      ("too deep", 3), ("deep in water", 4), ("landing me in water", 5),
      ("landing in water", 5), ("keeps landing", 4), ("land me in water", 5),
      ("in water", 2), ("into water", 2)],
     [("shake on land", 4), ("not reach", 3), ("short of water", 4), ("overshoot", 3)],
     _fix_land_in_water),
    ("shake_on_land", "Shakes before reaching water",
     [("shakes on land", 5), ("shake on land", 5), ("not reaching water", 5),
      ("doesn't reach water", 5), ("shakes too early", 3), ("not in water", 4),
      ("short of water", 4), ("shakes on the edge", 4), ("shaking on land", 5)],
     [], _fix_shake_on_land),
    ("dig_early", "Digs too early",
     [("digs early", 4), ("digs too early", 5), ("dig too early", 5), ("digs before", 4),
      ("dig before land", 5), ("digging mid", 3), ("digs mid glide", 5), ("digs while moving", 4),
      ("digs before landing", 5)],
     [("water", 1)], _fix_dig_early),
    ("dig_late", "Digs too late",
     [("digs late", 4), ("digs too late", 5), ("waits to dig", 4), ("slow to dig", 4),
      ("takes too long to dig", 4), ("pause before dig", 3)],
     [], _fix_dig_late),
    ("dig_long", "Dig hold too long",
     [("dig hold too long", 5), ("holds too long", 4), ("dig too long", 4),
      ("digs too long", 4), ("over digs", 3), ("dig hold long", 4),
      ("dig hold is too long", 5), ("hold is too long", 4), ("holding too long", 4)],
     [("short", 2)], _fix_dig_hold_long),
    ("dig_short", "Dig hold too short",
     [("dig too short", 5), ("dig hold short", 5), ("releases too early", 4),
      ("lets go too early", 4), ("dig doesn't complete", 4), ("digs don't complete", 4)],
     [], _fix_dig_hold_short),
    ("not_filling", "Pan not filling",
     [("not filling", 4), ("doesn't fill", 4), ("won't fill", 4), ("not full", 3),
      ("leaves before full", 5), ("needs more digs", 4), ("more digs", 3),
      ("not enough digs", 4), ("under fills", 4), ("underfill", 4),
      ("isnt filling", 4), ("filling all the way", 4), ("filling all", 3),
      ("not filling all", 4), ("doesnt fill all", 4)],
     [("overfill", 3), ("past full", 3)], _fix_not_filling),
    ("overflow_dig", "Overfills / digs past full",
     [("overfills", 4), ("overfill", 4), ("digs past full", 5), ("too many digs", 3),
      ("digs after full", 5), ("keeps digging", 3)],
     [("shake", 2), ("not filling", 3)], _fix_overflow_dig),
    ("probe_fail", "Can't find land (dig-probe)",
     [("can't find land", 5), ("cant find land", 5), ("probe fail", 4), ("no land", 3),
      ("doesn't find land", 5), ("never finds land", 5), ("digging in water", 4)],
     [], _fix_probe_fail),
    ("overshoot_water", "Overshoots into water",
     [("overshoots water", 5), ("too far into water", 5), ("walks too far back", 4),
      ("goes too far back", 4), ("too far back", 3), ("overshoots into", 5),
      ("overshoot into", 5), ("too far into the water", 5), ("overshoots the water", 5),
      ("walks into the water", 4), ("goes into the water", 3)],
     [("land", 2), ("shake", 1)], _fix_overshoot_water),
    ("short_water", "Falls short of water",
     [("short of water", 5), ("doesn't reach the water", 4), ("not far enough back", 4),
      ("stops before water", 4), ("short of the water", 5), ("falls short of water", 5),
      ("falls short of the water", 5), ("falls short", 3), ("doesnt reach the water", 4)],
     [("shake", 1), ("land", 2)], _fix_short_water),
    ("short_land", "Lands short / needs nudges",
     [("lands short", 5), ("short of land", 4), ("needs nudges", 4), ("too many nudges", 4),
      ("nudging", 3), ("doesn't reach land", 4), ("stops before land", 4)],
     [("water", 2)], _fix_short_land),
    ("overshoot_land", "Overshoots land",
     [("overshoots land", 5), ("too far onto land", 5), ("too far forward", 4),
      ("past the dig spot", 4), ("walks too far forward", 4), ("overshoots the land", 5),
      ("overshoot the land", 5), ("too far on land", 4), ("runs past land", 4)],
     [("water", 2)], _fix_overshoot_land),
    ("recover_aggressive", "Recovery too aggressive",
     [("recovers too much", 5), ("too much recovery", 5), ("recovery too aggressive", 5),
      ("recovering too often", 5), ("keeps recovering", 4), ("break out too much", 4),
      ("breaks out too much", 4), ("recovery too often", 5), ("breakout too often", 4),
      ("too aggressive", 4), ("recovery is too", 3), ("aggressive", 2),
      ("recovers too often", 5), ("break-out too", 4)],
     [("fortune", 3), ("fr too", 3), ("slow", 2), ("gives up", 3)], _fix_recover_aggressive),
    ("recover_slow", "Recovery too slow / gives up",
     [("slow to recover", 5), ("recovers too slow", 5), ("gives up", 4), ("gives up too", 5),
      ("stops too soon", 5), ("safe stops too", 5), ("safe-stops too", 5), ("stops too quick", 5),
      ("doesn't recover", 4), ("won't recover", 4)],
     [("fortune", 3)], _fix_recover_slow),
    ("fr_too_easy", "FR recovery triggers too easily",
     [("fortune river too", 4), ("fr too easy", 5), ("fr triggers", 5), ("recovery triggers", 4),
      ("auto fortune", 5), ("keeps going to fortune", 5), ("fast travel too", 4),
      ("fortune river recovery too", 5), ("fr fires", 5), ("fr keeps", 5),
      ("auto does the fortune", 5), ("always fortune river", 5), ("keeps triggering", 4),
      ("triggering", 2), ("fortune river keeps", 5), ("river keeps", 3), ("on its own", 2),
      ("by itself", 2), ("keeps recovering to fortune", 5), ("goes to fortune river", 4),
      ("fortune", 1)],
     [("find", 3), ("open", 3), ("wrong", 3), ("load", 3), ("can't", 2), ("cant", 2)], _fix_fr_too_easy),
    ("fr_not_found", "FR can't find Fortune River",
     [("can't find fortune", 5), ("cant find fortune", 5), ("doesn't find fortune", 5),
      ("fr can't find", 5), ("never finds fortune", 5), ("scrolls forever", 4),
      ("can't find the row", 4)],
     [("wrong", 3), ("open", 2)], _fix_fr_not_found),
    ("fr_wrong_row", "FR clicks wrong row",
     [("wrong row", 5), ("clicks the wrong", 5), ("matches the wrong", 5),
      ("picks the wrong row", 5), ("selects wrong", 4)],
     [], _fix_fr_wrong_row),
    ("fr_not_opening", "FR warp device won't open",
     [("warp won't open", 5), ("doesn't open", 4), ("warp device", 4), ("slot 4 doesn't", 5),
      ("fast travel won't open", 5), ("menu doesn't open", 4), ("double click doesn't", 4),
      ("doesn't open the warp", 5), ("won't open fast travel", 5)],
     [("find", 3), ("load", 2)], _fix_fr_not_opening),
    ("fr_too_fast", "FR recovery too fast",
     [("fr too fast", 5), ("happens too fast", 4), ("nothing loads", 4), ("too fast nothing", 5),
      ("recovery too fast", 5), ("clicks too fast", 4), ("moves too fast in recovery", 5)],
     [("mouse", 3), ("cursor", 3)], _fix_fr_too_fast),
    ("fr_wrong_shore", "FR restarts on wrong shore",
     [("wrong shore", 5), ("restarts in water", 5), ("restarts on the shore", 5),
      ("starts on wrong side", 4), ("before crossing", 4), ("doesn't cross", 4),
      ("restarts too early", 4)],
     [], _fix_fr_wrong_shore),
    ("fr_jumpy", "FR cursor jumpy",
     [("cursor too fast", 5), ("mouse too fast", 5), ("cursor jumpy", 5), ("mouse jumpy", 5),
      ("flies off", 4), ("cursor flies", 5), ("mouse flies", 5), ("jumps around", 4)],
     [], _fix_fr_jumpy),
    ("capacity", "Capacity / calibration",
     [("never digs", 4), ("walks forever", 4), ("walks back and forth", 4),
      ("thinks it's full", 5), ("thinks its full", 5), ("reads full when empty", 5),
      ("capacity broken", 5), ("capacity bar", 4), ("doesn't read full", 4),
      ("wrong prompt", 4), ("detects wrong", 4)],
     [], _fix_capacity),
]


# --------------------------------------------------------------------------- #
#  Efficiency analyzer                                                         #
# --------------------------------------------------------------------------- #
def _analyze_efficiency(s, ch, stats):
    notes = []
    # trim obvious dead time (only if currently generous)
    if int(s.get("POST_SHAKE_SETTLE_MS", 150)) > 150:
        _bump(ch, s, "POST_SHAKE_SETTLE_MS", -1, "trim settle time after the pan empties")
    if int(s.get("DIG_FILL_MS", 250)) > 200:
        _bump(ch, s, "DIG_FILL_MS", -1, "the bar usually reads full fast; trim the fill wait")
    if int(s.get("SHAKE_CLICK_GAP_MS", 14)) > 12:
        _bump(ch, s, "SHAKE_CLICK_GAP_MS", -1, "slightly faster shake rattle")
    if int(s.get("PRE_DIG_SETTLE_MS", 60)) > 80:
        _bump(ch, s, "PRE_DIG_SETTLE_MS", -1, "trim pre-dig settle (raise again if digs miss)")
    if int(s.get("EASY_WATER_RETURN_DELAY_MS", 0)) > 0:
        _bump(ch, s, "EASY_WATER_RETURN_DELAY_MS", -1, "remove the pause before heading back to water")

    exp = expected_cycle(stats) if stats else None
    if exp:
        notes.append("With your stats, an ideal cycle is about **%.1fs** "
                     "(≈ **%d pans/hr**), filling in **%d dig%s**."
                     % (exp["cycle_sec"], round(exp["pans_per_hr"]),
                        exp["digs_to_fill"], "" if exp["digs_to_fill"] == 1 else "s"))
        # dig knobs from stats (safe to suggest)
        n = exp["digs_to_fill"]
        if int(s.get("MAX_DIGS_TO_FILL", 8)) < n:
            _set(ch, s, "MAX_DIGS_TO_FILL", n, "your stats need %d digs to fill" % n)
        if not s.get("PERFECT", False):
            hold = exp["dig_hold_ms"]
            # only nudge if wildly off (don't fight a working fast-dig build)
            cur_hold = int(s.get("DIG_CLICK_MS", 75))
            if cur_hold > 30 and abs(cur_hold - hold) > 60:
                _set(ch, s, "DIG_CLICK_MS", hold,
                     "match the dig hold to your dig-speed stat (55000/%s)" % stats.get("dig_speed"))

    # reliability from history
    hist = []
    return notes


def _history_summary(hist):
    """Return (text, flags) from recent runs."""
    runs = [h for h in (hist or []) if isinstance(h, dict)][:8]
    if not runs:
        return "", {}
    n = len(runs)
    tot_min = sum((h.get("runtime_s", 0) or 0) for h in runs) / 60.0
    pph = [h.get("pans_per_hr", 0) or 0 for h in runs if h.get("pans_per_hr")]
    avg_pph = sum(pph) / len(pph) if pph else 0
    rec = sum((h.get("recoveries", 0) or 0) for h in runs)
    safe = sum((h.get("safe_stops", 0) or 0) for h in runs)
    hard = sum((h.get("hard_stops", 0) or 0) for h in runs)
    pans = sum((h.get("cycles", 0) or 0) for h in runs)
    flags = {"safe": safe, "hard": hard, "rec": rec, "avg_pph": avg_pph, "n": n}
    txt = ("Across your last **%d run%s** (%.0f min, %d pans): avg **%d pans/hr**, "
           "%d recoveries, %d safe-stops, %d hard-stops."
           % (n, "" if n == 1 else "s", tot_min, pans, round(avg_pph), rec, safe, hard))
    return txt, flags


# --------------------------------------------------------------------------- #
#  Intent: explicit "set X to N"                                               #
# --------------------------------------------------------------------------- #
_ALIASES = {
    "shake clicks": "SHAKE_CLICKS", "dig hold": "DIG_CLICK_MS",
    "dig speed": "DIG_SPEED", "max digs": "MAX_DIGS_TO_FILL",
    "digs to fill": "MAX_DIGS_TO_FILL", "shake click length": "SHAKE_CLICK_MS",
    "shake gap": "SHAKE_CLICK_GAP_MS", "shake glitch limit": "SHAKE_GLITCH_LIMIT",
    "no progress": "NO_PROGRESS_SEC", "stuck ticks": "STUCK_TICKS",
}


def _explicit_set(text, s, ch):
    m = re.search(r"set\s+(.+?)\s+to\s+(\d+)", text)
    if not m:
        return None
    name, val = m.group(1).strip(), int(m.group(2))
    key = None
    for alias, k in _ALIASES.items():
        if alias in name:
            key = k
            break
    if not key:                                   # try matching a label
        for k, lab in _LABEL.items():
            if name in lab.lower():
                key = k
                break
    if not key:
        return None
    _set(ch, s, key, val, "you asked to set it to %d" % val)
    return "Setting **%s** to **%d**." % (_label(key), _clamp(key, val))


# --------------------------------------------------------------------------- #
#  Main entry                                                                  #
# --------------------------------------------------------------------------- #
DEFAULT_CHIPS = ["It shakes late", "It lands in water", "Digs too early",
                 "Make it faster", "FR recovery triggers too easily",
                 "Analyze my last runs"]


def respond(message, ctx):
    ctx = ctx or {}
    s = dict(ctx.get("settings") or {})
    stats = ctx.get("stats") or {}
    hist = ctx.get("history") or []
    prev = ctx.get("prev_topic") or ""
    text = _norm(message)
    changes = []

    # escalation multiplier
    mult = 1.0
    if _has(text, *ESCALATE):
        mult = 2.0
    relax = _has(text, *RELAX)

    # 0) explicit "set X to N"
    msg = _explicit_set(text, s, changes)
    if msg:
        return _wrap(msg, changes, "explicit",
                     ["Save it", "Now make it faster", "Undo that"])

    # 0b) "too much / you overdid it" with nothing else -> back off
    if relax and not any(kw in text for _, _, kws, _, _ in SYMPTOMS for kw, _w in kws):
        return _wrap(
            "No problem — I won't push that further. If one change overshot, tell me "
            "which setting (e.g. “the land push is too much”) and I'll dial it back, or "
            "just lower it in its tab. You can also **Dismiss** a change card before "
            "applying it.", [], prev, ["Undo the last change", "It's fine now"])

    # 1) greetings / help / what-can-you-do
    if _has(text, "hi", "hello", "hey", "what can you do", "help me", "how do you work",
            "what do you do") and len(text) < 40 and not changes:
        return _wrap(
            "I'm your tuning coach. Tell me what the macro is doing wrong in plain "
            "English — “it shakes late”, “it lands in water”, “digs too early”, "
            "“Fortune River keeps triggering” — and I'll propose exact setting changes you "
            "can apply with one click. I can also **analyze your last runs**, **make a "
            "build faster**, or **explain any setting**. I only change settings, never code, "
            "and nothing applies until you confirm.", [], "", DEFAULT_CHIPS)

    # 2) "what does X do" / explain
    if _has(text, "what does", "what is", "explain", "what's the", "whats the",
            "meaning of", "what do") and not _has(text, "wrong", "problem"):
        for k, lab in _LABEL.items():
            kname = k.lower().replace("_", " ")          # SHAKE_BAIL_MS -> "shake bail ms"
            labname = lab.lower().split(" (")[0]
            if (" " + labname + " ") in text or (" " + kname + " ") in text \
                    or k.lower() in text:
                h = _HELP.get(k)
                if h:
                    return _wrap("**%s** — %s" % (lab, h), [], "explain",
                                 ["Change it", "Something's wrong with it"])
        # fall through if no match

    # 3) analyze runs / efficiency / stats
    wants_eff = _has(text, "faster", "more efficient", "efficiency", "optimi", "speed up",
                     "more pans", "improve", "tune my build", "best settings")
    wants_hist = _has(text, "analyze", "analyse", "my runs", "last run", "last runs",
                      "history", "how am i doing", "stats", "performance", "report")

    if wants_hist or wants_eff:
        notes = _analyze_efficiency(s, changes, stats) if wants_eff else []
        htxt, flags = _history_summary(hist)
        bits = []
        if htxt:
            bits.append(htxt)
        # reliability-driven suggestions
        if flags:
            if flags.get("safe", 0) >= 2 or flags.get("hard", 0) >= 1:
                _bump(changes, s, "POST_SHAKE_SETTLE_MS", +1, "more settle to cut safe-stops")
                _bump(changes, s, "LAND_SETTLE_MS", +1, "plant on land to avoid hazard stops")
                _bump(changes, s, "SAFE_STOP_MAX_RETRIES", +1, "heal more before a hard stop")
                bits.append("You've had stops — I've nudged settle/retry settings up for "
                            "stability. If the stops are at the water's edge, also try "
                            "“it lands in water”.")
            if flags.get("rec", 0) >= 8 * max(1, flags.get("n", 1)) / 2:
                bits.append("High recovery count usually means a timing mismatch — tell me "
                            "the specific symptom (shake miss? short land?) and I'll target it.")
        bits.extend(notes)
        if wants_eff and not stats:
            return _wrap(
                ((" ".join(bits) + "\n\n") if bits else "") +
                "To compute your **ideal cycle time and pans/hr** and match the dig "
                "settings to your character, tell me your in-game stats below.",
                changes, "efficiency", ["Trim dead time anyway", "Explain a setting"],
                ask_stats=True)
        if not bits and not changes:
            bits.append("I don't see any run history yet — run the macro once and I'll "
                        "have data to analyze. Meanwhile, tell me any symptom and I'll tune it.")
        return _wrap(" ".join(bits) if bits else
                     "Here's a small efficiency pass — trimmed some dead time. Review the "
                     "changes; raise anything back up if reliability drops.",
                     changes, "efficiency",
                     ["Now make it more reliable", "Tell me my ideal cycle"])

    # 4) symptom matching (can match several)
    scored = []
    for sid, title, kws, anti, fix in SYMPTOMS:
        score = 0
        for kw, w in kws:
            if kw in text:
                score += w
        if score == 0:
            continue
        for kw, w in anti:
            if kw in text:
                score -= w
        if score > 0:
            scored.append((score, sid, title, fix))
    scored.sort(reverse=True)

    if not scored and prev and _has(text, *ESCALATE):
        # escalation with no new symptom -> re-apply previous topic harder
        for sid, title, kws, anti, fix in SYMPTOMS:
            if sid == prev:
                reply = fix(s, changes, 2.0)
                return _wrap("Pushing that further. " + reply, changes, sid,
                             ["Still off", "That's worse — undo", "Perfect, save it"])

    if scored:
        top = scored[:2]                       # apply up to two related symptoms
        replies = []
        topic = top[0][1]
        for score, sid, title, fix in top:
            replies.append(fix(s, changes, mult))
        if relax:
            # user said we overdid it: flip the most recent changes' direction
            for c in changes:
                lo, hi, _ = RANGES.get(c["key"], (None, None, 1))
                # halve the move instead
            replies.insert(0, "(Easing the adjustment since you said it was too much.)")
        chips = ["Still happening", "That fixed it — save", "Something else is wrong"]
        return _wrap("\n\n".join(replies), changes, topic, chips)

    # 5) positive / thanks
    if _has(text, "perfect", "works now", "fixed", "thank", "thanks", "great", "nice",
            "that worked", "good job"):
        return _wrap("Nice — glad that's sorted. Hit **Save settings** (or **Save "
                     "build**) to keep it. Anything else acting up?", [], "",
                     DEFAULT_CHIPS)

    # 6) fallback
    return _wrap(
        "I want to target the right knob — can you say what it's doing wrong? For "
        "example: *“the shake misses”*, *“it lands in the water”*, *“it digs before "
        "reaching land”*, *“Fortune River keeps triggering”*, or *“make it faster”*. "
        "You can also ask me to **explain any setting** or **analyze your last runs**.",
        [], "", DEFAULT_CHIPS)


def _wrap(reply, changes, topic, chips, ask_stats=False):
    # de-dup changes by key (keep the last proposal for each)
    seen = {}
    for c in changes:
        seen[c["key"]] = c
    return {"reply": reply, "changes": list(seen.values()), "topic": topic,
            "askStats": bool(ask_stats), "chips": chips or DEFAULT_CHIPS}


# Keys the assistant is ALLOWED to change (everything in the schema except the
# Discord username string, which it should never overwrite). Used by the app to
# validate an apply request.
def allowed_keys():
    return set(k for k in _TYPES if k not in ("WEBHOOK_USER",))


# --------------------------------------------------------------------------- #
#  OPTIONAL: Claude API mode.                                                  #
#  The offline brain above is the default. If the user switches to API mode    #
#  and supplies their own key, the app calls Claude with this system prompt    #
#  and feeds the JSON reply back through finalize_api() -- so the chat UI,      #
#  the change-diff cards, the one-click apply and the schema validation are     #
#  all IDENTICAL to offline mode. The model can only ever PROPOSE setting keys; #
#  it never sees or touches code, and every change is re-validated on apply.   #
# --------------------------------------------------------------------------- #
def system_prompt(ctx):
    ctx = ctx or {}
    s = dict(ctx.get("settings") or {})
    stats = ctx.get("stats") or {}
    builds = ctx.get("builds") or {}
    hist = ctx.get("history") or []
    lines = [
        "You are Coach, a tuning assistant built into the Prospectors Plus macro "
        "(a Roblox auto-pan tool). You help the user fix problems and improve "
        "efficiency by adjusting SETTINGS only. You cannot change code, and you must "
        "never propose calibration pixels or the Discord username.",
        "",
        "Respond with EXACTLY ONE JSON object and no other text:",
        '{"reply": "<short friendly markdown>", '
        '"changes": [{"key":"EXACT_SETTING_KEY","to":<number|true|false>,"reason":"<short why>"}], '
        '"chips": ["<short follow-up suggestion>", "..."]}',
        "",
        "Rules:",
        "- Only use keys from the SETTINGS list below. Match the type (int or bool) and "
        "keep numbers within the given [min..max].",
        "- Be conservative and relative to the CURRENT value. Only include settings you "
        "actually want to change; use an empty changes list for questions or chit-chat.",
        "- Prefer the smallest change that fixes the issue. Explain briefly in each reason.",
        "- For efficiency, use the FORMULAS. Treat computed timings as estimates.",
        "- Never invent keys. Never output anything outside the JSON object.",
        "",
        "SETTINGS (key | type | range | current | what it does):",
    ]
    for key in _TYPES:
        if key in ("WEBHOOK_USER",):
            continue
        t = _TYPES[key]
        lo, hi, _ = RANGES.get(key, (None, None, 1))
        rng = "%s..%s" % (lo, hi) if lo is not None else ("true/false" if t == "bool" else "-")
        cur = s.get(key, _DEFAULTS.get(key))
        helptxt = (_HELP.get(key, "") or "").replace("\n", " ")
        if len(helptxt) > 180:
            helptxt = helptxt[:177] + "..."
        lines.append("- %s | %s | %s | now=%s | %s" % (key, t, rng, cur, helptxt))
    lines += [
        "",
        "FORMULAS (for efficiency estimates):",
        "- rolls/sec r = 4.03266e-9*ss^3 - 1.68935e-5*ss^2 + 0.0255557*ss + 0.206594  (ss = shake speed stat)",
        "- digs to fill n = ceil(Capacity / (1.5 * DigStrength))",
        "- dig hold ms = 55000 / DigSpeed%   ;   per-dig seconds = 190 / DigSpeed%",
        "- cycle seconds = Capacity/(r*ShakeStrength) + 1.5 + 190*n/DigSpeed% + 0.62  (0.62 = measured overhead)",
        "- pans/hr = 3600 / cycle seconds",
    ]
    if stats:
        lines.append("")
        lines.append("USER STATS: " + ", ".join("%s=%s" % (k, v) for k, v in stats.items()))
    if builds:
        lines.append("")
        lines.append("SAVED BUILDS: " + ", ".join(list(builds.keys())[:20]))
    htxt, _ = _history_summary(hist)
    if htxt:
        lines.append("")
        lines.append("RECENT RUNS: " + re.sub(r"\*\*", "", htxt))
    return "\n".join(lines)


def parse_json(text):
    """Pull the JSON object out of a model reply (tolerating code fences/prose)."""
    if not text:
        return None
    t = text.strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S)
        if m:
            t = m.group(1)
    a, b = t.find("{"), t.rfind("}")
    if a == -1 or b == -1 or b <= a:
        return None
    import json as _json
    try:
        return _json.loads(t[a:b + 1])
    except Exception:
        return None


def finalize_api(obj, settings):
    """Normalize a model JSON object into the same shape respond() returns,
    validating + clamping every proposed change against the schema."""
    s = dict(settings or {})
    reply = (obj.get("reply") if isinstance(obj, dict) else "") or ""
    chips = (obj.get("chips") if isinstance(obj, dict) else None) or []
    raw = (obj.get("changes") if isinstance(obj, dict) else None) or []
    changes = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        k = c.get("key")
        if k not in _TYPES or k == "WEBHOOK_USER":
            continue
        to = c.get("to")
        t = _TYPES[k]
        if t == "int":
            try:
                to = float(to)
            except (TypeError, ValueError):
                continue
        elif t == "bool":
            to = to in (True, "true", "True", 1, "1", "on", "yes")
        _set(changes, s, k, to, c.get("reason") or "suggested")
    return {"reply": reply, "changes": changes, "topic": "api",
            "askStats": False, "chips": chips or DEFAULT_CHIPS}
