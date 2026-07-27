"""Canonical Cycle Plan resolver (docs/CANONICAL_CYCLE_PLAN.md section 2.1).

`resolve_cycle_plan(eng)` reads the SAME module globals the Supervisor
executes with (bound by load_config at run start) and returns one JSON-able
document describing exactly what the engine will do: mode, the act()
dispatch policy, the legs with per-step semantics and resolved timings,
the five-rung recovery ladder, the background flows, the stop verbs, every
consulted setting with provenance, the HUD vocabulary, and a fingerprint.

Design rules (this module is descriptive ONLY -- it never mutates the
engine module and never performs I/O beyond reading eng.CONFIG_FILE):

* Every timing is carried as {"setting": KEY, "resolved_ms": value} (plus
  "formula" where the engine computes a budget), so hosts can bind edits
  back to the real knob the engine acts on.
* SHAKE_HOLD_MS is surfaced as the PER-ATTEMPT TIMEOUT of the click-rattle
  loop (engine.py do_shake: end = perf_counter() + hold/1000) -- never a
  mouse hold. Only dead legacy paths (shake_empty/timed_empty) hold LMB.
* Dead knobs (defined but never read by the live path) are included in
  `settings` with dead: true and are EXCLUDED from the fingerprint, so a
  dead-knob edit can never masquerade as a behavior change.
* provenance: "easy-layered" for the eight knobs the EASY_* offsets touch
  (when the offset is non-zero), else "config" when the key is present in
  the engine's config file (build entries merge into that file, so entry
  provenance is indistinguishable from config at engine level -- by
  design), else "default".
* Determinism: same globals -> byte-identical JSON (construction order is
  fixed; fingerprint is over the key-sorted canonical encoding).
"""
import hashlib
import json

from . import protocol

PLAN_VERSION = 1

# Knobs defined in the engine but never read by any live execution path
# (superseded or legacy-loop-only). See map classic-engine q2/q3.
DEAD_KNOBS = (
    "SHAKE_FAIL_LIMIT",      # superseded by SHAKE_GLITCH_LIMIT fast path
    "SHAKE_POLL_MS",         # no live references
    "SHAKE_INIT_GAP_MS",     # legacy shake path only
    "SHAKE_PLAIN_HOLD",      # legacy shake path only
    "SHAKE_PRESS_DELAY_MS",  # legacy shake path only
    "SHAKE_HOLD_DELAY_MS",   # legacy shake path only
    "SHAKE_MOMENTUM_MS",     # legacy shake path only
    "SHAKE_START_MS",        # dead timed path (momentum_and_shake)
    "WALK_BACK_MS",          # dead walk_back(); live path is go_water
    "MOMENTUM_W_MS",         # dead momentum_and_shake(); live is do_shake
)

# EASY_* additive layering targets (engine.load_config): real knob -> the
# EASY offset load_config adds onto it at bind time.
EASY_TOUCH = {
    "WATER_EXTRA_BACK_MS": "EASY_WATER_BACK_MS",
    "PAN_BACK_MAX_MS": "EASY_WATER_BACK_MS",
    "LAND_SETTLE_MS": "EASY_LAND_FWD_MS",
    "LAND_PROBE_NUDGE_MS": "EASY_LAND_FWD_MS",
    "DEPOSIT_MAX_MS": "EASY_LAND_FWD_MS",
    "SHAKE_START_DELAY_MS": "EASY_SHAKE_DELAY_MS",
    "PRE_DIG_SETTLE_MS": "EASY_FIRST_DIG_DELAY_MS",
    "POST_SHAKE_SETTLE_MS": "EASY_FIRST_DIG_DELAY_MS",
}

EASY_KEYS = ("EASY_WATER_BACK_MS", "EASY_LAND_FWD_MS", "EASY_SHAKE_DELAY_MS",
             "EASY_FIRST_DIG_DELAY_MS", "EASY_WATER_RETURN_DELAY_MS")

# Emission vocabulary of a Classic run (map classic-engine q7).
PHASES = ("dig", "water", "shake", "glide", "settle")

_SCALARS = {bool: "bool", int: "int", float: "float", str: "str"}


def _type_of(v):
    t = _SCALARS.get(type(v))
    if t is None:
        t = "list" if isinstance(v, (list, tuple)) else "dict"
    return t


def _jsonable(v):
    if isinstance(v, tuple):
        return list(v)
    return v


class _Ctx(object):
    """Resolution context: reads globals off the live engine module and
    registers every consulted key with value/type/provenance."""

    def __init__(self, eng):
        self.eng = eng
        self.consulted = {}
        try:
            with open(eng.CONFIG_FILE, encoding="utf-8") as f:
                doc = json.load(f)
            self.cfg = doc if isinstance(doc, dict) else {}
        except (OSError, ValueError):
            self.cfg = {}

    def g(self, key, default=None):
        return getattr(self.eng, key, default)

    def _provenance(self, key):
        easy = EASY_TOUCH.get(key)
        if easy and self.g(easy, 0):
            return "easy-layered"
        if key in self.cfg:
            return "config"
        return "default"

    def note(self, key, dead=False):
        """Register a consulted setting; returns its resolved value."""
        v = self.g(key)
        if key not in self.consulted:
            rec = {"value": _jsonable(v), "type": _type_of(v),
                   "provenance": self._provenance(key)}
            easy = EASY_TOUCH.get(key)
            if easy:
                off = self.g(easy, 0)
                rec["easy_offset"] = {"setting": easy, "value": off}
            if dead:
                rec["dead"] = True
                rec["provenance"] = "default" if key not in self.cfg \
                    else "config"
            self.consulted[key] = rec
        return v

    def t(self, key, formula=None, note=None):
        """A timing reference: {"setting", "resolved_ms", "formula"?}."""
        v = self.note(key)
        rec = {"setting": key, "resolved_ms": v}
        if formula:
            rec["formula"] = formula
        if note:
            rec["note"] = note
        return rec

    def v(self, key, note=None):
        """A non-ms setting reference: {"setting", "value"}."""
        v = self.note(key)
        rec = {"setting": key, "value": _jsonable(v)}
        if note:
            rec["note"] = note
        return rec


def resolve_mode(eng):
    """The engine's own run-mode dispatch precedence (main loop:
    TRACKER_MODE > SCRIPT_MODE > TREASURE_MODE > sup.tick). SCRIPT_MODE is
    not a plan mode (scripts run the ScriptRunner, not this plan), so the
    plan describes the Classic mode that would run with scripts ignored:
    TRACKER > GEODE > TREASURE > shards > standard -- the same labels
    FrameEmit._mode_label puts on the wire."""
    if getattr(eng, "TRACKER_MODE", False):
        return "tracker"
    if getattr(eng, "GEODE_MODE", False):
        return "geode"
    if getattr(eng, "TREASURE_MODE", False):
        return "treasure"
    if int(getattr(eng, "SHARDS_DIG_CLICKS", 0) or 0) > 0:
        return "shards"
    return "standard"


# ---- legs -------------------------------------------------------------------

def _leg_dig(ctx, mode):
    perfect = bool(ctx.note("PERFECT"))
    if perfect:
        click = {"op": "mouse_hold_until_cue", "cue": "dig-bar green pixel",
                 "max": ctx.t("DIG_MAX_MS"),
                 "release_delay": ctx.t("RELEASE_DELAY_MS")}
    else:
        click = {"op": "mouse_hold", "timing": ctx.t("DIG_CLICK_MS")}
    leg = {
        "verb": "return_and_dig",
        "phase": "dig",
        "semantics": "post-shake landing by DIG-PROBE, never by cue: a dig "
                     "only fills on dirt, so the capacity bar is the land "
                     "truth (the Shake cue can stick)",
        "steps": [
            {"op": "sleep", "timing": ctx.t("PRE_DIG_SETTLE_MS"),
             "note": "first probe round only"},
            {"op": "probe_dig",
             "rounds": ctx.v("LAND_DIG_TRIES"),
             "in_place_tries": ctx.v("DIG_INPLACE_TRIES"),
             "click": click,
             "registered_when": {
                 "cap_rise_frac": ctx.v("CAP_RISE_FRAC"),
                 "within": ctx.t("DIG_PROBE_MS")}},
            {"op": "nudge", "on": "no dig registered this round",
             "key": "W", "timing": ctx.t("LAND_PROBE_NUDGE_MS"),
             "settle": ctx.t("PROBE_GAP_MS"),
             "emits": "nudge"},
            {"op": "fill_to_full",
             "max_digs": ctx.v("MAX_DIGS_TO_FILL"),
             "wait_per_dig": ctx.t("DIG_FILL_MS")},
        ],
        "give_up": {"counter": "land_fails",
                    "limit": ctx.v("STUCK_LIMIT"),
                    "then": "safe_stop"},
    }
    if bool(ctx.note("LAND_CUE_ASSIST")):
        leg["steps"].insert(0, {
            "op": "pulse_until_cue", "key": "W", "cue": "DEPOSIT",
            "budget": ctx.t("LAND_ASSIST_MAX_MS"),
            "tap_on": ctx.t("BURST_ON_MS"), "tap_off": ctx.t("BURST_OFF_MS"),
            "note": "positioning only; the dig-probe still verifies land"})
    if mode == "shards":
        leg["variant"] = {
            "name": "shards",
            "semantics": "exact-click dig: SHARDS_DIG_CLICKS clicks; the "
                         "first click must prove it registered before "
                         "follow-ups ride the dig rhythm",
            "clicks": ctx.v("SHARDS_DIG_CLICKS"),
            "click_confirm": ctx.t("SHARDS_CLICK_CONFIRM_MS"),
            "click_retries": ctx.v("SHARDS_CLICK_RETRIES"),
            "assume_full": ctx.v("SHARDS_ASSUME_FULL"),
            "green_confirm": ctx.v("SHARDS_GREEN_CONFIRM"),
            "rhythm_gap": {"setting": "DIG_SPEED",
                           "resolved_ms": int(190000.0 / max(
                               1.0, ctx.note("DIG_SPEED"))) + 25,
                           "formula": "190000 / max(1, DIG_SPEED) + 25"},
        }
    elif mode == "geode":
        green = bool(ctx.note("GEODE_GREEN_CONFIRM"))
        leg["variant"] = {
            "name": "geode",
            "semantics": (
                "slow-fill dig: tap, wait the whole fill animation, judge "
                "by capacity; the green dig-bar proves the tap STARTED a "
                "dig, so a tap with no green is re-tapped at once and a "
                "dead tap budget ends the round WITHOUT paying the "
                "animation"
                if green else
                "slow-fill dig: tap, wait the whole fill animation, judge "
                "by capacity; a missing green never nudges on its own"),
            "digs_to_fill": ctx.v("GEODE_DIGS_TO_FILL"),
            "dig_hold": ctx.t("GEODE_DIG_MS"),
            "per_dig_wait": ctx.t("GEODE_DELAY_MS"),
            "start_window": ctx.t("GEODE_START_MS"),
            # start_tries only bites under green confirm: without that proof
            # a re-tap cannot tell a dead tap from a slow one.
            "start_tries": ctx.v(
                "GEODE_START_TRIES",
                note=None if green else
                "green confirm is off, so the leg taps ONCE and lets the "
                "capacity bar judge after the animation"),
            "green_confirm": ctx.v("GEODE_GREEN_CONFIRM"),
            "confirm_full": ctx.v("GEODE_CONFIRM_FULL"),
        }
    return leg


def _leg_water(ctx):
    leg = {
        "verb": "go_water",
        "phase": "water",
        "semantics": "ONE key_down(S) + ONE key_up(S) around a "
                     "cue-terminated wait -- never pulsed in a healthy leg; "
                     "the budget escalates after consecutive misses",
        "steps": [
            {"op": "key_hold_until_cue", "key": "S", "cue": "PAN",
             "budget": ctx.t(
                 "PAN_BACK_MAX_MS",
                 formula="PAN_BACK_MAX_MS * (1 + min(water_fails, 4))"),
             "confirm": ctx.v("WALK_BACK_CONFIRM")},
            {"op": "key_hold_extra", "key": "S",
             "timing": ctx.t("WATER_EXTRA_BACK_MS"),
             "note": "keep holding S this long AFTER the Pan cue "
                     "(deeper into the water); only on a reached cue"},
        ],
    }
    if bool(ctx.note("X_PATTERN")):
        leg["x_pattern"] = {
            "enabled": True,
            "strafe": ctx.t("X_STRAFE_MS"),
            "recenter_budget": ctx.t("X_RECENTER_MS"),
            "semantics": "bounded A/D diagonal with a drift ledger; "
                         "recenter strafe once |balance| exceeds the budget "
                         "(emits 'recenter')",
        }
    return leg


def _leg_shake(ctx, mode):
    geode = mode == "geode" and ctx.note("GEODE_SHAKE_HOLD_MS") > 0
    hold_key = "GEODE_SHAKE_HOLD_MS" if geode else "SHAKE_HOLD_MS"
    bail_key = "GEODE_SHAKE_HOLD_MS" if geode else "SHAKE_BAIL_MS"
    fixed = int(ctx.note("SHAKE_CLICKS") or 0) > 0
    empty_frac = ctx.note("CAP_EMPTY_FRAC")
    check = ctx.note("GEODE_SHAKE_CHECK") if geode else 1
    if not (geode and check > 1):
        check = 1
    until = ({"clicks": ctx.v("SHAKE_CLICKS")} if fixed else
             {"cap_below": {"setting": "CAP_EMPTY_FRAC",
                            "value": (empty_frac * 0.5) if geode
                            else empty_frac}})
    if geode and not fixed:
        until["cap_below"]["note"] = "halved in geode mode"
    attempt_timeout = ctx.t(hold_key)
    attempt_timeout["semantics"] = ("PER-ATTEMPT TIMEOUT of the click "
                                    "loop -- never a mouse hold")
    if fixed:
        attempt_timeout["ignored"] = "fixed-count mode (SHAKE_CLICKS > 0) " \
                                     "ignores the timeout"
    leg = {
        "verb": "do_shake",
        "phase": "shake",
        "semantics": "momentum technique: hold W toward land and rattle "
                     "rapid clicks until the CAPACITY reads empty "
                     "(capacity is the truth; the Shake cue sticks)",
        "steps": [
            {"op": "sleep", "timing": ctx.t("SHAKE_START_DELAY_MS")},
            {"op": "key_down", "key": "W",
             "enabled": ctx.v("SHAKE_MOMENTUM_W"),
             "release_on_cue": "DEPOSIT",
             "note": "momentum toward land; released the moment the "
                     "Collect Deposit cue shows, else at loop end"},
            {"op": "glide", "phase": "glide",
             "timing": ctx.t("SHAKE_W_LEAD_MS"),
             "note": "pure-clockwork W pre-roll BEFORE the first click "
                     "(no screen reads, no early exit)"},
            {"op": "click_rattle_until",
             "click": ctx.t("SHAKE_CLICK_MS"),
             "gap": ctx.t("SHAKE_CLICK_GAP_MS"),
             "until": until,
             "attempt_timeout": attempt_timeout,
             "sense_every_clicks": check},
        ],
        "failure_layers": {
            "start_confirm": {
                "window": ctx.t("SHAKE_START_CONFIRM_MS",
                                note="0 disables the layer"),
                "retries": ctx.v("SHAKE_START_RETRIES"),
                "deeper_tap": ctx.t("SHAKE_RETRY_DEEPER_MS"),
                "deadline_extension_ms": 600,
                "semantics": "still COMPLETELY FULL past the window: drop "
                             "W, tap S deeper, re-hold W, keep clicking; "
                             "each retry extends the attempt deadline by "
                             "0.6 s and re-arms the bail window "
                             "(emits 'shake_start_retry')"},
            "stall": {
                "window": ctx.t("SHAKE_STALL_MS",
                                note="0 disables the layer"),
                "semantics": "a STARTED shake whose bar freezes this long "
                             "mid-drain fails fast" +
                             (" (x4 in geode mode)" if geode else "")},
            "bail": {
                "window": ctx.t(bail_key),
                "semantics": "still COMPLETELY FULL past this -> the shake "
                             "never started; give the attempt up" +
                             (" (geode: suppressed -- bail equals the "
                              "geode hold)" if geode else "")},
        },
        "on_fail": {"counter": "shake_fails", "emits": "shake_fail",
                    "stat": "shake_misses"},
    }
    if geode:
        leg["geode_overrides"] = {
            "attempt_timeout": ctx.t("GEODE_SHAKE_HOLD_MS"),
            "cap_empty_frac_halved": True,
            "sense_every_clicks": ctx.v("GEODE_SHAKE_CHECK"),
            "bail_suppressed": True,
        }
    return leg


def _leg_settle(ctx):
    return {
        "verb": "post_shake_settle",
        "phase": "settle",
        "on": "pan emptied (one completed cycle; counters cleared)",
        "steps": [
            {"op": "sleep", "timing": ctx.t("POST_SHAKE_SETTLE_MS"),
             "note": "momentum-W keeps carrying the character onto land "
                     "while this settle runs (the glide IS the land "
                     "approach; there is no go_land walk)"},
        ],
    }


def _leg_treasure(ctx):
    return {
        "verb": "treasure_tick",
        "semantics": "linear chest loop, NO shake: dig briefly, strafe "
                     "sideways (D then A, alternating) to the next chest",
        "steps": [
            {"op": "mouse_hold", "timing": ctx.t("TREASURE_DIG_MS"),
             "repeat": ctx.v("TREASURE_DIGS")},
            {"op": "sleep", "timing": ctx.t("TREASURE_DIG_GAP_MS"),
             "note": "per dig (slow chest dig animation)"},
            {"op": "key_hold_until_cue", "key": "D|A (alternating)",
             "cue": "PAN then DEPOSIT",
             "budget": ctx.t("TREASURE_MOVE_MAX_MS",
                             note="applied to each cue wait")},
        ],
    }


# ---- recovery ---------------------------------------------------------------

def _recovery(ctx, mode):
    supervised = mode in ("standard", "shards", "geode")
    rungs = []
    if supervised:
        rungs.append({
            "id": "R1", "name": "shake-glitch fast path",
            "trigger": {"counter": "shake_fails",
                        "at_least": ctx.v("SHAKE_GLITCH_LIMIT")},
            "limit": {"counter": "breakouts",
                      "max": ctx.v("BREAKOUT_LIMIT")},
            "enabled_by": ctx.v("BREAKOUT_ENABLED"),
            "actions": [
                {"op": "break_out",
                 "click_to_empty": ctx.t("BREAKOUT_SHAKE_MS"),
                 "click": ctx.t("SHAKE_CLICK_MS"),
                 "gap": ctx.t("SHAKE_CLICK_GAP_MS"),
                 "reposition": ctx.t("BREAKOUT_REPOS_MS"),
                 "emits": "shake_glitch, break_out"},
            ],
            "escalate": "safe_stop when break-outs are exhausted or "
                        "disabled",
            "resume": "watchdog reset; normal act() next tick",
        })
        rungs.append({
            "id": "R2", "name": "no-progress fast path",
            "trigger": {"no_progress_for_s": ctx.v("NO_PROGRESS_SEC"),
                        "meaning": "no pan emptied AND no dig registered"},
            "limit": {"counter": "breakouts",
                      "max": ctx.v("BREAKOUT_LIMIT")},
            "enabled_by": ctx.v("BREAKOUT_ENABLED"),
            "actions": [{"op": "break_out", "emits": "no_progress"}],
            "escalate": "safe_stop when breakouts exceed the limit",
            "resume": "progress clock refreshed; watchdog reset",
        })
        rungs.append({
            "id": "R3", "name": "stuck watchdog",
            "trigger": {"same_situation_ticks": ctx.v("STUCK_TICKS")},
            "limit": {"counter": "recoveries",
                      "max": ctx.v("RECOVER_LIMIT")},
            "enabled_by": ctx.v("RECOVER_ENABLED"),
            "actions": [
                {"op": "recover", "emits": "recover",
                 "by_situation": {
                     "FULL+WATER": {"op": "re_shake",
                                    "enabled_by":
                                        ctx.v("SHAKE_RETRY_ENABLED")},
                     "FULL+other": {"op": "pulse_until_cue", "key": "S",
                                    "cue": "PAN",
                                    "budget": ctx.t("RECOVER_BACK_MS"),
                                    "tap_on": ctx.t("BURST_ON_MS"),
                                    "tap_off": ctx.t("BURST_OFF_MS")},
                     "not-FULL": {"op": "pulse_until", "key": "W",
                                  "until": "capacity FULL",
                                  "budget": ctx.t("RECOVER_BACK_MS"),
                                  "tap_on": ctx.t("BURST_ON_MS"),
                                  "tap_off": ctx.t("BURST_OFF_MS")}}},
            ],
            "escalate": "break_out (R1 machinery) when recoveries exhaust; "
                        "safe_stop when breakouts exceed the limit; with "
                        "all recovery toggles off, act() simply retries "
                        "and the R4 guards remain",
            "resume": "watchdog cleared on any situation change",
        })
        rungs.append({
            "id": "R4", "name": "per-situation guards",
            "trigger": {"any_of": [
                {"counter": "land_fails",
                 "at_least": ctx.v("STUCK_LIMIT"),
                 "source": "dig-probe never found land"},
                {"counter": "no_full_cycles",
                 "at_least": ctx.v("NO_FULL_LIMIT"),
                 "source": "the pan never reads FULL -- capacity pixel "
                           "looks mis-calibrated", "hard": True},
            ]},
            "actions": [{"op": "safe_stop",
                         "note": "the NO_FULL guard stops hard=True "
                                 "(no retry ladder)"}],
            "resume": "counters cleared by healthy progress",
        })
    rungs.append({
        "id": "R5", "name": "safe_stop",
        "trigger": {"source": "guard exhaustion or manual soft-stop"},
        "actions": [
            {"op": "release_all"},
            {"op": "river_recovery", "order": ["starfall", "fortune"],
             "starfall": {
                 "enabled_by": ctx.v("SR_RECOVERY"),
                 "steps": "fast-travel menu scan by row colour, warp, "
                          "timed A-strafe to water, D back a fraction, "
                          "S to the Pan cue",
                 "a_max": ctx.t("SR_A_MAX_MS"),
                 "d_back_pct": ctx.v("SR_D_PCT"),
                 "s_max": ctx.t("SR_S_MAX_MS"),
                 "cross_confirm": ctx.v("FR_CROSS_CONFIRM"),
                 "emits": "sr_recover"},
             "fortune": {
                 "enabled_by": ctx.v("FR_RECOVERY"),
                 "steps": "fast-travel scan for the row colour, warp, W "
                          "across the water on the Pan-then-Deposit cue",
                 "warp_wait": ctx.t("FR_WARP_MS"),
                 "walk_max": ctx.t("FR_WALK_MAX_MS"),
                 "cross_confirm": ctx.v("FR_CROSS_CONFIRM"),
                 "emits": "fr_recover"},
             "on_success": "run CONTINUES (want_reset; supervisor resets "
                           "next tick)"},
            {"op": "safe_pause_retry",
             "enabled_by": ctx.v("SAFE_STOP_RETRY"),
             "retry_wait_s": ctx.v("SAFE_STOP_RETRY_SEC"),
             "max_retries": ctx.v("SAFE_STOP_MAX_RETRIES"),
             "semantics": "beep + safety.safePaused; block (skippable via "
                          "run.resume), then want_reset and the run "
                          "continues",
             "emits": "safe_stop"},
            {"op": "hard_stop",
             "when": "retries exhausted, retry disabled, or hard=True",
             "sets": {"stop_reason": "safe-stop"},
             "emits": "hard_stop"},
        ],
        "resume": "safe_retries cleared by any healthy shake/dig",
    })
    return {
        "rungs": rungs,
        "de_escalation": {
            "pan_emptied_clears": ["shake_fails", "breakouts",
                                   "safe_retries", "no_full", "water_fails"],
            "dig_registered_clears": ["land_fails", "breakouts",
                                      "safe_retries"],
            "note": "progress de-escalates the whole ladder; want_reset "
                    "makes Supervisor.reset() run on the next tick",
        },
    }


# ---- background / stops -----------------------------------------------------

def _background(ctx, mode):
    flows = {
        "earn_tracker": {
            "enabled_by": ctx.v("EARN_TRACK"),
            "trigger": "run start (needs calibrated money+shards regions "
                       "and Vision OCR)",
            "cadence": {"setting": "EARN_OCR_SEC",
                        "resolved_s": ctx.note("EARN_OCR_SEC")},
            "input_policy": "read_only",
            "lifecycle": "daemon thread; exits when the session ends "
                         "(neither running nor paused)",
            "semantics": "OCR the HUD money/shard totals; two identical "
                         "reads 250 ms apart; credit positive deltas only",
        },
        "finds_watcher": {
            "enabled_by": ctx.v("FINDS_TRACK"),
            "trigger": "run start (needs the calibrated find region)",
            "cadence": {"fast_sampler": ctx.t("FINDS_FAST_MS"),
                        "ocr_pass": ctx.t("FINDS_OCR_MS")},
            "input_policy": "read_only",
            "lifecycle": "TWO daemon threads (pixel sampler + Vision OCR "
                         "identity pass); exit when the session ends",
            "semantics": "fade-direction card tracker; mints finds and "
                         "emits find.new / find.updated",
        },
        "relic_scheduler": {
            "enabled_by": ctx.v("RELICS_ENABLED"),
            "trigger": "per-relic interval (minutes), polled before each "
                       "tick",
            "cadence": {"relics": [
                {"name": str(r.get("name", "relic %d" % (i + 1))),
                 "minutes": r.get("minutes", 10),
                 "slot": r.get("slot", 0),
                 "clicks": r.get("clicks", 2)}
                for i, r in enumerate(ctx.note("RELICS") or [])]},
            "input_policy": "takes_input_at_tick_boundary",
            "lifecycle": "NOT a thread: maybe_fire() runs synchronously on "
                         "the engine tick thread (a safe boundary); the "
                         "supervisor re-senses next tick",
            "fire": {
                "pre_settle": ctx.t("RELIC_PRE_MS"),
                "slot_switch": ctx.t("RELIC_SWITCH_MS"),
                "click": ctx.t("RELIC_CLICK_MS"),
                "click_gap": ctx.t("RELIC_CLICK_GAP_MS"),
                "return_slot": ctx.v("RELIC_RETURN_SLOT"),
                "wait_for_land": ctx.v("RELIC_ON_LAND"),
                "land_wait_max_s": ctx.v("RELIC_LAND_MAX_S"),
                "timers_follow_real_time": ctx.v("RELIC_RELATIVE"),
            },
        },
        "lag_probe": {
            "enabled_by": True,
            "trigger": "always on (passive counters)",
            "cadence": "fed by every timed sleep's wake lateness; "
                       "snapshotted into the 2 s stats line",
            "input_policy": "read_only",
            "lifecycle": "in-process counters, reset per run",
        },
    }
    if mode == "tracker":
        flows["autopan_guard"] = {
            "enabled_by": ctx.v("AUTOPAN_GUARD"),
            "trigger": "tracker tick, every AUTOPAN_GUARD_SEC "
                       "(two consecutive OFF reads required)",
            "cadence": {"setting": "AUTOPAN_GUARD_SEC",
                        "resolved_s": ctx.note("AUTOPAN_GUARD_SEC")},
            "input_policy": "takes_input_at_tick_boundary",
            "lifecycle": "runs inside tracker_tick on the tick thread",
            "semantics": "re-enables the game's Auto Pan button if it "
                         "reads OFF (emits autopan_guard); the health "
                         "kick cycles a wedged-but-green button after "
                         "AUTOPAN_STALL_SEC of a dead bar (emits "
                         "autopan_kick)",
            "stall_kick_s": ctx.v("AUTOPAN_STALL_SEC"),
        }
    return flows


def _stops(ctx):
    return {
        "toggle": {
            "verbs": ["hotkey Ctrl+K", "run.start/run.stop"],
            "semantics": "flips running; every primitive checks running "
                         "constantly so a stop aborts within one poll; "
                         "release_all on every stop edge",
            "default_reason": "manual",
        },
        "pause": {
            "verbs": ["hotkey Ctrl+L", "run.pause/run.resume"],
            "semantics": "paused=True and running=False -- verbs abort "
                         "instantly but stats, relic timers, earnings and "
                         "finds all survive; resume shifts relic deadlines "
                         "when timers don't follow real time",
        },
        "soft": {
            "verbs": ["hotkey Ctrl+J", "run.softStop"],
            "semantics": "want_safe_stop -> the R5 safe_stop ladder (a "
                         "self-healing pause, not a stop)",
        },
        "quit": {
            "verbs": ["hotkey Esc", "engine.shutdown"],
            "semantics": "running=False, alive=False, release_all; the "
                         "process exits",
        },
        "autostop": {
            "enabled_by": ctx.v("AUTOSTOP_ENABLED"),
            "minutes": ctx.v("AUTOSTOP_MINUTES"),
            "reason": "auto",
        },
        "bag_full_guard": {
            "stop_after_pans": ctx.v("STOP_AFTER_PANS",
                                     note="0 disables the guard"),
            "reason": "bag-full",
        },
    }


# ---- policy -----------------------------------------------------------------

def _policy(ctx, mode):
    if mode == "tracker":
        return [{
            "order": 1, "when": {"always": True}, "leg": "watch_only",
            "source": "tracker_tick: reads the capacity bar, counts the "
                      "game's own digs/pans, sends ZERO input",
            "poll": ctx.t("TRACKER_POLL_MS"),
        }]
    if mode == "treasure":
        return [{
            "order": 1, "when": {"always": True}, "leg": "treasure",
            "source": "treasure_tick: linear dig-then-strafe loop "
                      "(no sensing dispatch, no shake)",
        }]
    rules = [
        {"order": 1,
         "when": {"contents": "FULL", "where": "WATER", "cue": "PAN"},
         "leg": "shake",
         "source": "act(): FULL + Pan cue -> do_shake"},
        {"order": 2,
         "when": {"contents": "FULL", "where": "LAND|SHAKING|LIMBO"},
         "leg": "water",
         "pre": [{"op": "sleep",
                  "timing": ctx.t("EASY_WATER_RETURN_DELAY_MS"),
                  "skipped_when_zero": True}],
         "source": "act(): FULL elsewhere -> go_water"},
        {"order": 3,
         "when": {"contents": "EMPTY|PARTIAL"},
         "leg": "dig",
         "source": "act(): not full -> return_and_dig"},
    ]
    note = ("stage order is EMERGENT: every tick re-senses "
            "(Situation = cue x capacity) and dispatches; there is no "
            "fixed stage sequencer")
    if mode == "shards" and bool(ctx.note("SHARDS_ASSUME_FULL")):
        note += ("; shards assume-full: a started one-click fill is "
                 "treated as FULL until the animation deadline passes")
    if mode == "geode":
        note += ("; geode: a counted fill holds 'full' briefly while the "
                 "bar still lags (assume_full_until)")
    rules.append({"order": 0, "note": note,
                  "sensing": {"votes": ctx.v("SENSE_VOTES")}})
    return rules


# ---- hud --------------------------------------------------------------------

def _hud(eng):
    try:
        stats_keys = sorted(eng.SessionStats().as_dict().keys())
    except Exception:
        stats_keys = []
    return {
        "stats_keys": stats_keys,
        "stats_injected": ["input_lag", "relics"],
        "event_types": sorted(protocol.SAFETY_EVENT_TYPES),
        "phases": list(PHASES),
    }


# ---- effective recovery program / background flows -------------------------

def _apply_effective_program(eng, ctx, plan):
    """Overlay the EFFECTIVE recovery program + authored flows onto the
    plan (spec C: plan.describe reflects the program that will actually
    run). Default mode (no RECOVERY_JSON / FLOWS_JSON) adds only the two
    consulted settings entries -- rung/flow sections stay untouched."""
    rec_text = ctx.note("RECOVERY_JSON")
    flow_text = ctx.note("FLOWS_JSON")
    from . import recovery as recovery_mod
    from . import flows as flows_mod
    eff = recovery_mod.describe_effective(eng, rec_text)
    if eff is not None:
        rec = plan["recovery"]
        rec["program"] = ({"error": eff["error"]} if "error" in eff else {
            "overrides": eff["overrides"],
            "disabled": eff["disabled"],
            "authored": [a["id"] for a in eff["authored"]],
        })
        if "error" not in eff:
            for rung in rec["rungs"]:
                rid = rung.get("id")
                if rid in eff["disabled"]:
                    rung["disabled"] = True
                allowed = recovery_mod.RUNG_PARAMS.get(rid, ())
                ov = {k: eff["overrides"][k] for k in allowed
                      if k in eff["overrides"]}
                if ov:
                    rung["overrides"] = ov
            rec["rungs"].extend(eff["authored"])
    flows_eff = flows_mod.describe_effective(eng, flow_text)
    if flows_eff is not None:
        if isinstance(flows_eff, dict) and "error" in flows_eff:
            plan["background"]["authored_flows"] = flows_eff
        else:
            plan["background"]["authored_flows"] = flows_eff


# ---- fingerprint ------------------------------------------------------------

def plan_fingerprint(plan):
    """SHA-256 over the key-sorted canonical JSON of the plan, excluding
    the fingerprint field itself and every dead-knob settings entry (a
    dead knob can never change behavior, so it must never change the
    fingerprint)."""
    doc = {k: v for k, v in plan.items() if k != "fingerprint"}
    live = {k: v for k, v in (doc.get("settings") or {}).items()
            if not v.get("dead")}
    doc["settings"] = live
    blob = json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---- entry ------------------------------------------------------------------

def resolve_cycle_plan(eng):
    """Resolve the canonical Cycle Plan from the live engine module's
    globals (the same values the Supervisor executes with). Pure read;
    deterministic: same globals -> byte-identical JSON."""
    ctx = _Ctx(eng)
    mode = resolve_mode(eng)
    # register the mode-precedence flags as consulted settings
    ctx.note("TRACKER_MODE")
    ctx.note("GEODE_MODE")
    ctx.note("TREASURE_MODE")
    ctx.note("SHARDS_DIG_CLICKS")
    ctx.note("SCRIPT_MODE")

    legs = {}
    if mode == "tracker":
        pass                       # watch-only: no input legs
    elif mode == "treasure":
        legs["treasure"] = _leg_treasure(ctx)
    else:
        legs["dig"] = _leg_dig(ctx, mode)
        legs["water"] = _leg_water(ctx)
        legs["shake"] = _leg_shake(ctx, mode)
        legs["settle"] = _leg_settle(ctx)

    plan = {
        "_plan": PLAN_VERSION,
        "mode": mode,
        "policy": _policy(ctx, mode),
        "legs": legs,
        "recovery": _recovery(ctx, mode),
        "background": _background(ctx, mode),
        "stops": _stops(ctx),
        "hud": _hud(eng),
    }
    _apply_effective_program(eng, ctx, plan)
    # EASY offsets are always part of the consulted surface (they are
    # already layered inside the resolved globals; surface the offsets)
    for k in EASY_KEYS:
        ctx.note(k)
    for k in DEAD_KNOBS:
        ctx.note(k, dead=True)
    plan["settings"] = {k: ctx.consulted[k] for k in sorted(ctx.consulted)}
    plan["fingerprint"] = plan_fingerprint(plan)
    return plan
