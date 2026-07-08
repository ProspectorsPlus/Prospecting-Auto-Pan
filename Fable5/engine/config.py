"""Config schema, defaults, validation, persistence, and legacy import.

Every tunable lives in SPEC (single source of truth). The UI renders its
settings page from SPEC; the engine reads values through a Config object;
anything that writes a setting is validated + clamped against SPEC.

Legacy import: pulls calibrated pixels / proven timings / player stats out
of the old app's prospecting_config.json via an explicit WHITELIST — no
secrets (webhooks, API keys, sync URLs, access hashes) ever cross over.
"""
from __future__ import annotations

import json
import os
import threading

CONFIG_NAME = "fable_config.json"

# --------------------------------------------------------------------------
# Schema: (key, section, type, default, lo, hi, label, help)
# type: int | float | bool | str | pix (an [x, y] pixel) | json (free structure)
# --------------------------------------------------------------------------
SPEC = [
    # --- Calibration (pixels are ABSOLUTE physical screen coords) ----------
    ("CAP_LEFT_PIXEL",   "calib", "pix",  [682, 878],  None, None,
     "Capacity bar — left tip",
     "Left end of the pan-fill bar. Gray when empty, gold when full."),
    ("CAP_FULL_PIXEL",   "calib", "pix",  [1119, 878], None, None,
     "Capacity bar — right tip",
     "Right end of the pan-fill bar. Gold here = pan is FULL."),
    ("DEPOSIT_PIX",      "calib", "pix",  [770, 987],  None, None,
     "'Collect Deposit' text",
     "A pixel on the white 'Collect Deposit' prompt (shows on land)."),
    ("PAN_PIX",          "calib", "pix",  [848, 981],  None, None,
     "'Pan' text",
     "A pixel on the white 'Pan' prompt (shows in the water)."),
    ("SHAKE_PIX",        "calib", "pix",  [831, 986],  None, None,
     "'Shake' text",
     "A pixel on the white 'Shake' prompt (shows while shaking)."),
    ("DIG_TRIGGER_PIXEL","calib", "pix",  [1082, 530], None, None,
     "Dig skill bar — green zone",
     "Only used by Perfect dig (release on green). Optional."),

    # --- Player stats (drive predictive budgets; 0 = unknown) --------------
    ("STAT_CAPACITY",       "stats", "int", 0, 0, 10**9,
     "Capacity", "Your in-game Capacity stat."),
    ("STAT_DIG_STRENGTH",   "stats", "int", 0, 0, 10**9,
     "Dig Strength", "Your in-game Dig Strength stat."),
    ("STAT_SHAKE_STRENGTH", "stats", "int", 0, 0, 10**9,
     "Shake Strength", "Your in-game Shake Strength stat."),
    ("STAT_SHAKE_SPEED",    "stats", "int", 0, 0, 10**9,
     "Shake Speed", "Your in-game Shake Speed stat."),

    # --- Dig ----------------------------------------------------------------
    ("DIG_CLICK_MS",     "dig", "int", 75, 1, 5000,
     "Dig hold (ms)", "How long each dig click is held. 55000 / DigSpeed%."),
    ("DIG_SPEED",        "dig", "int", 100, 1, 100000,
     "Dig speed stat (%)", "Your dig-speed stat; used to derive the hold."),
    ("PERFECT",          "dig", "bool", False, None, None,
     "Perfect dig (release on green)",
     "Usually OFF — the skill bar is too fast; timed holds are steadier."),
    ("MAX_DIGS_TO_FILL", "dig", "int", 8, 1, 64,
     "Max digs to fill", "Safety cap on digs per fill (watches the bar)."),
    ("DIG_REGISTER_MS",  "dig", "int", 150, 30, 2000,
     "Dig register window (ms)",
     "How long to watch for the bar to RISE after a dig before calling it a miss."),
    ("DIG_FILL_MS",      "dig", "int", 250, 0, 5000,
     "Post-dig fill wait (ms)", "Wait for FULL after a registered dig."),
    ("PRE_DIG_SETTLE_MS","dig", "int", 60, 0, 2000,
     "Pre-dig settle (ms)", "Settle after landing before the first dig."),
    ("DIG_INPLACE_TRIES","dig", "int", 3, 1, 10,
     "In-place dig tries",
     "Re-dig in place this many times before nudging forward (one lag spike shouldn't cause a nudge)."),
    ("LAND_DIG_TRIES",   "dig", "int", 5, 1, 20,
     "Nudge rounds", "Forward-nudge rounds looking for land before giving up."),
    ("LAND_PROBE_NUDGE_MS","dig","int", 90, 10, 1000,
     "Probe nudge (ms)", "Forward W nudge between probe digs."),
    ("PROBE_GAP_MS",     "dig", "int", 80, 0, 1000,
     "Probe gap (ms)", "Settle after a nudge before the next probe dig."),

    # --- Water --------------------------------------------------------------
    ("PAN_BACK_MAX_MS",  "water", "int", 350, 20, 5000,
     "Walk-back budget (ms)",
     "Cap on the S walk-back to water (grows automatically after misses). "
     "A cap, not a target — S releases the moment the cue confirms."),
    ("WATER_EXTRA_BACK_MS","water","int", 60, 0, 1000,
     "Extra depth (ms)",
     "Keep holding S after the Pan cue — go deeper so the shake can't start "
     "at the edge (edge clicks silently do nothing: the #1 shake failure)."),
    ("WATER_CONFIRM_FRAMES","water","int", 1, 1, 10,
     "Water confirm (frames)",
     "Extra confirm frames for the Pan cue (the perception layer already "
     "debounces cues; raise only if you see false water reads)."),

    # --- Shake (v2 — start-confirmed momentum shake) ------------------------
    ("SHAKE_MOMENTUM_W", "shake", "bool", True, None, None,
     "Momentum W", "Hold W during the shake so momentum glides you to land."),
    ("SHAKE_W_AFTER_START", "shake", "bool", True, None, None,
     "Momentum after start confirm",
     "Engage momentum W only once the shake is CONFIRMED started (~2 frames). "
     "Stops failed first clicks from being dragged back onto land — the root "
     "of most 'shake never started' losses. OFF = v2 behavior (W from click 1)."),
    ("SHAKE_CLICK_MS",   "shake", "int", 18, 1, 200,
     "Shake click (ms)", "Each shake click's held length."),
    ("SHAKE_CLICK_GAP_MS","shake","int", 14, 1, 500,
     "Shake click gap (ms)", "Gap between shake clicks (lower = faster rattle)."),
    ("SHAKE_START_DELAY_MS","shake","int", 10, 0, 2000,
     "Start delay (ms)", "Pause between reaching water and the first click."),
    ("SHAKE_START_CONFIRM_MS","shake","int", 320, 60, 2000,
     "Start confirm window (ms)",
     "If neither the Shake cue nor a fill drop shows within this window, the shake didn't start — micro-retry."),
    ("SHAKE_START_RETRIES","shake","int", 2, 0, 10,
     "Start micro-retries",
     "On a failed start: tap S a touch deeper and keep clicking, this many times, before counting a shake fail."),
    ("SHAKE_RETRY_DEEPER_MS","shake","int", 80, 10, 500,
     "Retry deeper tap (ms)",
     "The S tap used by a start micro-retry (failed starts usually mean 'too shallow')."),
    ("SHAKE_MAX_MS",     "shake", "int", 1500, 200, 20000,
     "Shake budget (ms)",
     "Overall cap on one shake. With stats entered, the engine stretches this to 2.5x the predicted drain time."),
    ("SHAKE_CLICKS",     "shake", "int", 0, 0, 200,
     "Fixed click count", "0 = auto (click until empty). N = exactly N clicks."),
    ("SHAKE_EMPTY_CONFIRM_FRAMES","shake","int", 2, 1, 10,
     "Empty confirm (frames)", "Consecutive frames of 'bar empty' to stop clicking."),
    ("SHAKE_STALL_MS",   "shake", "int", 600, 100, 5000,
     "Drain stall (ms)",
     "If a started shake's fill hasn't dropped for this long, it's stalling; two stalls end the attempt."),
    ("POST_SHAKE_SETTLE_MS","shake","int", 150, 0, 2000,
     "Post-shake settle (ms)", "Settle after the pan empties before landing logic."),

    # --- Landing (cue-confirmed) --------------------------------------------
    ("LAND_MAX_MS",      "land", "int", 1200, 100, 10000,
     "Landing budget (ms)", "Cap on the W walk looking for the Collect Deposit cue."),
    ("LAND_CONFIRM_FRAMES","land","int", 2, 1, 10,
     "Land confirm (frames)", "Consecutive frames of the Deposit cue to count as landed."),
    ("LAND_SETTLE_MS",   "land", "int", 45, 0, 1000,
     "Land settle (ms)", "Keep holding W a touch after the cue — sit firmly on dirt."),

    # --- Recovery / safety ----------------------------------------------------
    ("RECOVER_ENABLED",  "safety", "bool", True, None, None,
     "Stuck recovery", "Master switch for the stuck-recovery ladder."),
    ("STUCK_TICKS",      "safety", "int", 3, 1, 20,
     "Stuck ticks", "Identical situations in a row before recovery triggers."),
    ("RECOVER_LIMIT",    "safety", "int", 3, 1, 20,
     "Recoveries before break-out", "Recovery attempts before escalating."),
    ("RECOVER_BACK_MS",  "safety", "int", 160, 20, 2000,
     "Recovery nudge budget (ms)", "Jittered corrective-tap budget."),
    ("BREAKOUT_ENABLED", "safety", "bool", True, None, None,
     "Break-out", "Last-resort escape (click out a stuck shake + reposition)."),
    ("BREAKOUT_LIMIT",   "safety", "int", 2, 1, 10,
     "Break-outs before safe stop", "Break-out attempts before safe-stopping."),
    ("BREAKOUT_SHAKE_MS","safety", "int", 700, 100, 5000,
     "Break-out click budget (ms)", "Click time to finish a movement-locking shake."),
    ("BREAKOUT_REPOS_MS","safety", "int", 160, 20, 2000,
     "Break-out reposition (ms)", "Forward nudge after the break-out clicks."),
    ("SHAKE_FAIL_LIMIT", "safety", "int", 5, 0, 50,
     "Shake fails before safe stop", "0 = disabled."),
    ("SHAKE_GLITCH_LIMIT","safety","int", 2, 0, 50,
     "Shake glitch fast path", "Failed shakes before a quick click-to-empty. 0 = disabled."),
    ("NO_PROGRESS_SEC",  "safety", "int", 6, 0, 600,
     "No-progress watchdog (s)",
     "Seconds with nothing completing before a click-to-empty. 0 = disabled."),
    ("NO_FULL_LIMIT",    "safety", "int", 8, 0, 100,
     "Never-full limit",
     "Dig cycles where the bar never reads FULL before stopping loudly (mis-calibration guard). 0 = disabled."),
    ("SAFE_STOP_RETRY",  "safety", "bool", True, None, None,
     "Safe-stop auto-retry", "A safe stop pauses and retries instead of hard-stopping."),
    ("SAFE_STOP_RETRY_SEC","safety","int", 60, 5, 3600,
     "Retry wait (s)", "Pause before each safe-stop retry."),
    ("SAFE_STOP_MAX_RETRIES","safety","int", 3, 0, 20,
     "Max retries", "Hard-stop after this many failed retries."),
    ("BURST_ON_MS",      "safety", "int", 11, 1, 100,
     "Jitter tap on (ms)", "Recovery tap hold per pulse."),
    ("BURST_OFF_MS",     "safety", "int", 1, 0, 100,
     "Jitter tap off (ms)", "Recovery tap release per pulse."),

    # --- Adaptive (opt-in) ----------------------------------------------------
    ("ADAPT_WATER_TRIM", "adapt", "bool", False, None, None,
     "Adaptive water depth",
     "Learn extra water depth from shake-start failures (bounded 0-150 ms, decays on clean cycles)."),

    # --- Perception -----------------------------------------------------------
    ("SENSE_FPS",        "sense", "int", 60, 15, 240,
     "Sensor rate (fps)", "Background capture rate."),
    ("CUE_ON_FRAMES",    "sense", "int", 2, 1, 10,
     "Cue on (frames)", "Frames a cue must show before it counts (debounce)."),
    ("CUE_OFF_FRAMES",   "sense", "int", 3, 1, 15,
     "Cue off (frames)", "Frames a cue must be gone before it clears (flicker filter)."),
    ("RATE_WINDOW_MS",   "sense", "int", 150, 50, 1000,
     "Fill-rate window (ms)", "Window for the fill rate-of-change estimate."),
    ("FULL_FRAC",        "sense", "float", 0.98, 0.5, 1.0,
     "Full threshold", "Bar fill fraction that counts as FULL."),
    ("EMPTY_FRAC",       "sense", "float", 0.03, 0.0, 0.5,
     "Empty threshold", "Bar fill fraction that counts as EMPTY."),
    ("CAP_RISE_FRAC",    "sense", "float", 0.03, 0.005, 0.5,
     "Dig-registered rise", "Fill increase that proves a dig registered."),
    ("SHAKE_DROP_FRAC",  "sense", "float", 0.015, 0.005, 0.5,
     "Shake-started drop", "Fill decrease that proves a shake started."),
    ("CAP_BAND_H",       "sense", "int", 12, 4, 40,
     "Bar sample height (px)", "Pixel rows sampled across the capacity bar."),
    ("CAP_INSET_PX",     "sense", "int", 8, 0, 40,
     "Bar end inset (px)",
     "Ignore this many px at each bar end (anti-aliased tips fail the gold test)."),
    ("CUE_BOX",          "sense", "int", 6, 3, 30,
     "Cue sample box (px)", "NxN box sampled around each cue pixel."),
    ("YEL_MIN",          "sense", "int", 140, 50, 255,
     "Gold min r/g", "r and g must both be >= this for 'gold'."),
    ("YEL_BLUE_GAP",     "sense", "int", 45, 5, 200,
     "Gold blue gap", "...and b <= min(r,g) minus this."),
    ("CUE_WHITE_MIN",    "sense", "int", 160, 50, 255,
     "Cue white min", "All channels >= this to count as cue text."),
    ("CUE_WHITE_SPREAD", "sense", "int", 70, 5, 200,
     "Cue white spread", "Max-min channel spread <= this (whitish)."),
    ("CUE_WHITE_FRAC",   "sense", "float", 0.12, 0.01, 1.0,
     "Cue white fraction", "Box fraction of white pixels for a cue to be present."),

    # --- Relics ---------------------------------------------------------------
    ("RELICS_ENABLED",   "relics", "bool", False, None, None,
     "Relic timers", "Press each relic's hotbar slot every N minutes."),
    ("RELICS",           "relics", "json", [], None, None,
     "Relics", "List of {name, slot, minutes, clicks}."),
    ("PAN_SLOT",         "relics", "int", 1, 1, 9,
     "Pan hotbar slot", "Slot to return to after using a relic."),

    # --- Run ------------------------------------------------------------------
    ("AUTOSTOP_MINUTES", "run", "int", 0, 0, 6000,
     "Auto-stop (min)", "Stop after N minutes. 0 = off."),
    ("STOP_AFTER_PANS",  "run", "int", 0, 0, 100000,
     "Stop after pans", "Stop after N pans (bag-full guard). 0 = off."),

    # --- Hotkeys ----------------------------------------------------------------
    ("HOTKEY_TOGGLE",    "keys", "json", {"ctrl": True, "alt": False, "shift": False, "code": "KeyK"},
     None, None, "Start / Stop", "Global start/stop chord."),
    ("HOTKEY_SOFTSTOP",  "keys", "json", {"ctrl": True, "alt": False, "shift": False, "code": "KeyJ"},
     None, None, "Soft stop", "Trigger a safe stop (test recovery)."),
    ("HOTKEY_QUIT",      "keys", "json", {"ctrl": False, "alt": False, "shift": False, "code": "Escape"},
     None, None, "Quit engine", "Kill the engine process."),
]

SECTION_LABELS = {
    "calib":  "Calibration",
    "stats":  "Player stats",
    "dig":    "Digging",
    "water":  "Water",
    "shake":  "Shake",
    "land":   "Landing",
    "safety": "Recovery & safety",
    "adapt":  "Adaptive",
    "sense":  "Perception (advanced)",
    "relics": "Relics",
    "run":    "Run limits",
    "keys":   "Hotkeys",
}

TYPES = {k: t for k, _s, t, _d, _lo, _hi, _l, _h in SPEC}
DEFAULTS = {k: d for k, _s, _t, d, _lo, _hi, _l, _h in SPEC}
RANGES = {k: (lo, hi) for k, _s, _t, _d, lo, hi, _l, _h in SPEC}


def _coerce(key, value):
    """Validate + clamp one value against SPEC. Raises ValueError on junk."""
    t = TYPES.get(key)
    if t is None:
        raise ValueError("unknown setting: %s" % key)
    if t == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if t == "int" or t == "float":
        v = float(value)
        lo, hi = RANGES[key]
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return int(round(v)) if t == "int" else v
    if t == "pix":
        x, y = value
        return [int(x), int(y)]
    if t == "str":
        return str(value)
    return value                      # json: free structure


class Config:
    """Thread-safe settings holder with attribute access.

    The percept thread re-reads geometry only on explicit reload; scalar
    reads are plain dict lookups behind a lock-free snapshot (Python dict
    reads are atomic; writes swap the whole dict)."""

    def __init__(self, values=None, path=None):
        self._values = dict(DEFAULTS)
        if values:
            for k, v in values.items():
                if k in TYPES:
                    try:
                        self._values[k] = _coerce(k, v)
                    except Exception:
                        pass          # keep the default on junk
        self.path = path
        self._lock = threading.Lock()

    def __getattr__(self, key):
        try:
            return self._values[key]
        except KeyError:
            raise AttributeError(key)

    def get(self, key, default=None):
        return self._values.get(key, default)

    def set(self, key, value):
        v = _coerce(key, value)
        with self._lock:
            nv = dict(self._values)
            nv[key] = v
            self._values = nv
        return v

    def update(self, mapping):
        applied = {}
        for k, v in mapping.items():
            try:
                applied[k] = self.set(k, v)
            except Exception:
                pass
        return applied

    def to_dict(self):
        return dict(self._values)

    def save(self, path=None):
        p = path or self.path
        if not p:
            return
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._values, f, indent=2)
        os.replace(tmp, p)

    # -- derived ------------------------------------------------------------
    def cap_bar_width(self):
        return max(8, self.CAP_FULL_PIXEL[0] - self.CAP_LEFT_PIXEL[0])


def load(app_dir):
    """Load (or create) the Fable config. On first run, try a legacy import."""
    path = os.path.join(app_dir, CONFIG_NAME)
    if os.path.exists(path):
        try:
            with open(path) as f:
                return Config(json.load(f), path)
        except Exception:
            pass
    cfg = Config(path=path)
    legacy = find_legacy(app_dir)
    if legacy:
        import_legacy(cfg, legacy)
    cfg.save()
    return cfg


# --------------------------------------------------------------------------
# Legacy import (old Prospectors Plus config)
# --------------------------------------------------------------------------
# Only these keys may cross over. Secrets (WEBHOOK_*, COACH_API_KEY,
# SYNC_URL, ACCESS_*) are deliberately NOT in this list.
LEGACY_DIRECT = [
    "CAP_LEFT_PIXEL", "CAP_FULL_PIXEL", "DEPOSIT_PIX", "PAN_PIX", "SHAKE_PIX",
    "DIG_TRIGGER_PIXEL",
    "DIG_CLICK_MS", "DIG_SPEED", "PERFECT", "MAX_DIGS_TO_FILL",
    "PAN_BACK_MAX_MS", "WATER_EXTRA_BACK_MS",
    "SHAKE_MOMENTUM_W", "SHAKE_CLICK_MS", "SHAKE_CLICK_GAP_MS",
    "SHAKE_START_DELAY_MS", "SHAKE_CLICKS", "POST_SHAKE_SETTLE_MS",
    "LAND_SETTLE_MS", "LAND_PROBE_NUDGE_MS", "PROBE_GAP_MS", "LAND_DIG_TRIES",
    "STUCK_TICKS", "RECOVER_LIMIT", "RECOVER_BACK_MS",
    "BREAKOUT_LIMIT", "BREAKOUT_SHAKE_MS", "BREAKOUT_REPOS_MS",
    "SHAKE_FAIL_LIMIT", "BURST_ON_MS", "BURST_OFF_MS",
    "SAFE_STOP_RETRY", "SAFE_STOP_RETRY_SEC", "SAFE_STOP_MAX_RETRIES",
    "STOP_AFTER_PANS", "RELICS", "RELICS_ENABLED",
    "HOTKEY_TOGGLE", "HOTKEY_SOFTSTOP", "HOTKEY_QUIT",
]
LEGACY_RENAME = {
    "DEPOSIT_MAX_MS": "LAND_MAX_MS",
}
# Safety CAPS (release early on success): never import a value below Fable's
# default — v2 defaults were tuned for undebounced cues and read ~35 ms early.
LEGACY_FLOOR_TO_DEFAULT = {"PAN_BACK_MAX_MS", "LAND_MAX_MS",
                           "WATER_EXTRA_BACK_MS"}
# Old zero-means-broken values we must not import as-is: the v2 engine's
# 0-threshold bug means many users have 0s that meant "disabled" only after
# the fix; Fable's defaults are sane, so skip zeros for these.
LEGACY_SKIP_IF_ZERO = {"SHAKE_CLICK_MS", "SHAKE_CLICK_GAP_MS", "DIG_CLICK_MS",
                       "PAN_BACK_MAX_MS", "BREAKOUT_SHAKE_MS", "LAND_MAX_MS"}
LEGACY_STATS = {          # AUTOBUILD block -> STAT_*
    "ab_cap": "STAT_CAPACITY",
    "ab_ds": "STAT_DIG_STRENGTH",
    "ab_ss": "STAT_SHAKE_STRENGTH",
    "ab_sspeed": "STAT_SHAKE_SPEED",
}


def find_legacy(app_dir):
    """Look for the old config next to this folder (Fable5 sits inside the
    old project folder) and in the folder itself."""
    cands = [
        os.path.join(app_dir, "prospecting_config.json"),
        os.path.join(os.path.dirname(app_dir), "prospecting_config.json"),
    ]
    for c in cands:
        if os.path.exists(c):
            return c
    return None


def import_legacy(cfg, legacy_path):
    """Merge whitelisted legacy values into cfg. Returns list of applied keys."""
    try:
        with open(legacy_path) as f:
            old = json.load(f)
    except Exception:
        return []
    applied = []
    for k in LEGACY_DIRECT:
        if k not in old:
            continue
        v = old[k]
        if k in LEGACY_SKIP_IF_ZERO and (v == 0 or v is None):
            continue
        if k in LEGACY_FLOOR_TO_DEFAULT:
            try:
                v = max(float(v), float(DEFAULTS[k]))
            except Exception:
                pass
        try:
            cfg.set(k, v)
            applied.append(k)
        except Exception:
            pass
    for old_k, new_k in LEGACY_RENAME.items():
        if old_k in old and old[old_k]:
            try:
                cfg.set(new_k, old[old_k])
                applied.append(new_k)
            except Exception:
                pass
    ab = old.get("AUTOBUILD") or {}
    for old_k, new_k in LEGACY_STATS.items():
        v = ab.get(old_k)
        if v:
            try:
                cfg.set(new_k, v)
                applied.append(new_k)
            except Exception:
                pass
    # derive CAP_LEFT from width if the old config only had the width
    if "CAP_LEFT_PIXEL" not in old and old.get("CAP_BAR_WIDTH") and old.get("CAP_FULL_PIXEL"):
        fx, fy = old["CAP_FULL_PIXEL"]
        cfg.set("CAP_LEFT_PIXEL", [int(fx) - int(old["CAP_BAR_WIDTH"]), int(fy)])
        applied.append("CAP_LEFT_PIXEL")
    return applied


# --------------------------------------------------------------------------
# Game formulas (community calculator; used for predictive budgets + UI)
# --------------------------------------------------------------------------
def rolls_per_sec(shake_speed):
    ss = float(shake_speed)
    return 4.03266e-9 * ss**3 - 1.68935e-5 * ss**2 + 0.0255557 * ss + 0.206594


def digs_to_fill(capacity, dig_strength):
    if dig_strength <= 0:
        return 0
    import math
    return int(math.ceil(capacity / (1.5 * dig_strength)))


def dig_hold_ms(dig_speed_pct):
    return 55000.0 / max(1.0, float(dig_speed_pct))


def expected_drain_ms(capacity, shake_strength, shake_speed):
    """Predicted time for a shake to empty a full pan, in ms. 0 = unknown."""
    if capacity <= 0 or shake_strength <= 0 or shake_speed <= 0:
        return 0
    r = rolls_per_sec(shake_speed)
    if r <= 0:
        return 0
    return 1000.0 * capacity / (r * shake_strength)


def expected_cycle_sec(capacity, dig_strength, dig_speed_pct, shake_strength,
                       shake_speed):
    """Full-cycle estimate (the 0.62 s is measured fixed overhead)."""
    if min(capacity, dig_strength, shake_strength, shake_speed) <= 0:
        return 0
    n = digs_to_fill(capacity, dig_strength)
    drain = expected_drain_ms(capacity, shake_strength, shake_speed) / 1000.0
    return drain + 1.5 + (190.0 * n) / max(1.0, dig_speed_pct) + 0.62
