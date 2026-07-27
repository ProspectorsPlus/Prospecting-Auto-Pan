#!/usr/bin/env python3
"""lite_onboarding.py -- Prospector Lite's first-run state machine and
calibration registry.

The five-step first run is:

    1 Welcome  ->  2 Trust & Permissions  ->  3 Guided Calibration
                ->  4 Readiness Check      ->  5 the app

State lives in `onboarding_state.json` inside the user's data directory --
NOT in the main config -- written atomically so a crash can never corrupt
setup progress. The wizard resumes wherever it left off, can be re-run any
time from Help / Trust Center, and can be reset without touching builds,
calibration or history.

The calibration registry below is derived from the values the runtime
actually reads (prospector_engine/sensing.py PIXEL_KEYS + engine defaults).
It does not invent settings: every item maps to real config keys, and the
guided wizard drives the SAME sensing engine and the SAME save path as the
Calibrate tab (`sensing.save_pixels`), so there is exactly one calibration
store.
"""

import json
import os
import time

SCHEMA_VERSION = 1
CALIBRATION_SCHEMA = 1

# Ordered wizard states
STATES = ("NOT_STARTED", "WELCOME_COMPLETE", "TRUST_STARTED",
          "TRUST_COMPLETE", "CALIBRATION_STARTED", "CALIBRATION_COMPLETE",
          "READINESS_COMPLETE", "FINISHED")

_STATE_FILE = "onboarding_state.json"


def _default_state(platform_key):
    return {
        "schema": SCHEMA_VERSION,
        "state": "NOT_STARTED",
        "platform": platform_key,
        "product_version": "",
        "calibration_schema": CALIBRATION_SCHEMA,
        "declined_optional": [],
        "completed_at": 0,
        "last_readiness": None,
    }


class Onboarding(object):
    """Load/advance/persist the wizard state. All writes are atomic
    (tmp + os.replace); a torn write can only lose the last transition,
    never the file."""

    def __init__(self, data_dir, platform_key, version=""):
        self.path = os.path.join(data_dir, _STATE_FILE)
        self.platform = platform_key
        self.version = version
        self.state = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and d.get("state") in STATES:
                d.setdefault("schema", SCHEMA_VERSION)
                d.setdefault("declined_optional", [])
                d.setdefault("calibration_schema", CALIBRATION_SCHEMA)
                d.setdefault("last_readiness", None)
                return d
        except (OSError, ValueError):
            pass
        return _default_state(self.platform)

    def migrate_legacy(self, welcome_seen):
        """One-time bridge from the pre-wizard flag: a user who finished the
        old single welcome screen has a working, calibrated install -- they
        are marked FINISHED rather than being forced back through setup.
        The full wizard stays available from Help / Trust Center."""
        if (self.state["state"] == "NOT_STARTED" and welcome_seen
                and not os.path.exists(self.path)):
            self.state["state"] = "FINISHED"
            self.state["migrated_from"] = "WELCOME_SEEN"
            self.state["completed_at"] = int(time.time())
            self._save()
            return True
        return False

    def _save(self):
        self.state["product_version"] = self.version
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def mark(self, state):
        """Advance to `state`. Backward transitions are allowed only via
        rerun()/reset() so a stray call can't un-finish setup."""
        if state not in STATES:
            return self.state
        cur_i = STATES.index(self.state["state"])
        new_i = STATES.index(state)
        if new_i > cur_i:
            self.state["state"] = state
            if state == "FINISHED":
                self.state["completed_at"] = int(time.time())
            self._save()
        return self.state

    def record_readiness(self, result):
        self.state["last_readiness"] = result
        self._save()
        return self.state

    def decline_optional(self, cap_id, declined=True):
        lst = self.state.setdefault("declined_optional", [])
        if declined and cap_id not in lst:
            lst.append(cap_id)
        if not declined and cap_id in lst:
            lst.remove(cap_id)
        self._save()
        return self.state

    def rerun(self):
        """Re-open the wizard from the Trust step without discarding
        completion history (used by 'Rerun setup wizard')."""
        self.state["state"] = "WELCOME_COMPLETE"
        self._save()
        return self.state

    def reset(self):
        """Full reset to a brand-new first run. Touches ONLY the wizard
        state -- builds, calibration, settings and history are untouched."""
        self.state = _default_state(self.platform)
        self._save()
        return self.state

    def finished(self):
        return self.state["state"] == "FINISHED"


# ---------------------------------------------------------------------------
# Calibration registry
# ---------------------------------------------------------------------------
# Every entry maps to config keys the runtime actually reads (see
# docs/trust-and-onboarding/CALIBRATION_REGISTRY.md for the evidence trail).
# required=True items drive every classic cycle; conditional items name the
# setting that activates them; the wizard shows conditional items only as
# optional, with the consequence of skipping spelled out.

CALIBRATION_ITEMS = [
    {
        "id": "roblox_window",
        "title": "Roblox window",
        "purpose": "Everything is calibrated relative to where the Roblox "
                   "window sits. Detection confirms the game is visible on "
                   "the primary display.",
        "keys": [],
        "required": True,
        "modes": ["all"],
        "instructions": "Open Roblox in Prospecting on your main display, "
                        "windowed or full screen, then press Detect.",
        "action": "detect",
        "source_references": [
            ("prospector_engine.platform_mac", "find_roblox_window",
             "macOS window lookup (owner name + bounds only)"),
            ("prospector_engine.platform_win", "find_roblox_window",
             "Windows window lookup"),
        ],
    },
    {
        "id": "cap_bar",
        "title": "Pan capacity bar (right + left ends)",
        "purpose": "The yellow fill bar tells the macro when the pan is "
                   "full, when it's empty and when a dig registered -- the "
                   "heartbeat of every cycle.",
        "keys": ["CAP_FULL_PIXEL", "CAP_LEFT_PIXEL", "CAP_BAR_WIDTH"],
        "required": True,
        "modes": ["all classic modes"],
        "instructions": "Click the RIGHT tip of the pan-fill bar, then the "
                        "LEFT tip. The app derives the bar width and "
                        "verifies it (> 20 px).",
        "action": "wizard",
        "source_references": [
            ("prospector_engine.sensing", "Sensing.save_pixels",
             "the one save path: derives width, re-derives ratios, atomic"),
            ("prospector_engine.engine", "Detector",
             "the run-time reader of these pixels"),
        ],
    },
    {
        "id": "pan_prompt",
        "title": "'Pan' prompt pixel",
        "purpose": "Anchors the walk-back to water: the macro knows it is "
                   "at the water when this white prompt appears.",
        "keys": ["PAN_PIX"],
        "required": True,
        "modes": ["all classic modes"],
        "instructions": "Click the centre of the white 'Pan' prompt text.",
        "action": "wizard",
        "source_references": [
            ("prospector_engine.sensing", "Sensing._detect_cue_px",
             "the auto-detector that proposes this pixel"),
        ],
    },
    {
        "id": "deposit_prompt",
        "title": "'Collect Deposit' prompt pixel",
        "purpose": "Anchors the land side: dig starts when this prompt is "
                    "visible.",
        "keys": ["DEPOSIT_PIX"],
        "required": True,
        "modes": ["all classic modes"],
        "instructions": "Click the centre of the white 'Collect Deposit' "
                        "prompt text.",
        "action": "wizard",
        "source_references": [
            ("prospector_engine.sensing", "Sensing._detect_cue_px",
             "the auto-detector that proposes this pixel"),
        ],
    },
    {
        "id": "shake_prompt",
        "title": "'Shake' prompt pixel",
        "purpose": "Confirms the shake started; a missed shake is retried "
                   "instead of wasting the pan.",
        "keys": ["SHAKE_PIX"],
        "required": True,
        "modes": ["all classic modes"],
        "instructions": "Click the centre of the white 'Shake' prompt text.",
        "action": "wizard",
        "source_references": [
            ("prospector_engine.sensing", "Sensing._detect_cue_px",
             "the auto-detector that proposes this pixel"),
        ],
    },
    {
        "id": "dig_green",
        "title": "Green dig-bar pixel",
        "purpose": "The green zone of the dig bar. Needed by Perfect digs "
                   "and the shards/geode green-confirm nudges.",
        "keys": ["DIG_TRIGGER_PIXEL"],
        "required": False,
        "condition": "PERFECT, SHARDS_GREEN_CONFIRM or GEODE_GREEN_CONFIRM",
        "modes": ["Perfect", "Shards", "Geodes"],
        "instructions": "Click inside the green target zone of the dig "
                        "bar while it is on screen.",
        "action": "pixel",
        "skip_consequence": "Perfect mode and green-confirm nudges won't "
                            "fire; plain digs still work.",
        "source_references": [
            ("prospector_engine.engine", "Detector",
             "dig_region / is_white reader"),
        ],
    },
    {
        "id": "money_region",
        "title": "Money counter box",
        "purpose": "Lets the earnings tracker read your money counter "
                   "locally (OCR on this machine).",
        "keys": ["MONEY_TL_PIXEL", "MONEY_BR_PIXEL"],
        "required": False,
        "condition": "EARN_TRACK",
        "modes": ["Earnings tracker"],
        "instructions": "Drag a tight box around the money number.",
        "action": "region",
        "skip_consequence": "Earnings stats stay empty; nothing else "
                            "changes.",
        "source_references": [
            ("prospector_engine.sensing", "Sensing.test_read",
             "the read test for this region"),
        ],
    },
    {
        "id": "shards_region",
        "title": "Shards counter box",
        "purpose": "Same as money, for the shards counter.",
        "keys": ["SHARDS_TL_PIXEL", "SHARDS_BR_PIXEL"],
        "required": False,
        "condition": "EARN_TRACK",
        "modes": ["Earnings tracker"],
        "instructions": "Drag a tight box around the shards number.",
        "action": "region",
        "skip_consequence": "Shard stats stay empty.",
        "source_references": [
            ("prospector_engine.sensing", "Sensing.test_read",
             "the read test for this region"),
        ],
    },
    {
        "id": "find_region",
        "title": "Finds pop-up box",
        "purpose": "Lets the finds tracker read the item pop-ups locally.",
        "keys": ["FIND_TL_PIXEL", "FIND_BR_PIXEL"],
        "required": False,
        "condition": "FINDS_TRACK",
        "modes": ["Finds tracker"],
        "instructions": "Drag a box over where find pop-ups appear.",
        "action": "region",
        "skip_consequence": "The finds log stays empty.",
        "source_references": [
            ("prospector_engine.sensing", "Sensing.test_read",
             "the read test for this region"),
        ],
    },
    {
        "id": "fortune_river",
        "title": "Fortune River recovery points",
        "purpose": "Recovery clicks for the Fortune River event UI.",
        "keys": ["FR_OPEN_PIXEL", "FR_HOME_PIXEL", "FR_SCAN_X",
                 "FR_BOX_TOP", "FR_BOX_BOTTOM"],
        "required": False,
        "condition": "FR_RECOVERY",
        "modes": ["Fortune River recovery"],
        "instructions": "Use the Fortune River section on the Calibrate "
                        "tab; each button captures one point.",
        "action": "tab",
        "skip_consequence": "Fortune River auto-recovery stays off.",
        "source_references": [
            ("prospector_engine.sensing", "Sensing.save_pixels",
             "the FR group save path"),
        ],
    },
    {
        "id": "autopan_button",
        "title": "Auto Pan button pixel",
        "purpose": "Lets relic tracking verify the Auto Pan toggle state.",
        "keys": ["AUTOPAN_BTN_PIXEL"],
        "required": False,
        "condition": "TRACKER_MODE",
        "modes": ["Relic tracker"],
        "instructions": "Click the centre of the Auto Pan button.",
        "action": "tab",
        "skip_consequence": "The tracker degrades gracefully and logs it.",
        "source_references": [
            ("prospector_engine.sensing", "Sensing.save_pixels",
             "the AUTOPAN group save path"),
        ],
    },
    {
        "id": "cue_masks",
        "title": "Advanced cue masks",
        "purpose": "Pixel-mask matching of the prompt text for tougher "
                   "lighting; optional accuracy upgrade.",
        "keys": ["CUE_MASKS"],
        "required": False,
        "condition": "ADVANCED_CUES",
        "modes": ["Advanced cue matching"],
        "instructions": "Use 'Guided cue capture' on the Calibrate tab.",
        "action": "tab",
        "skip_consequence": "The simpler single-pixel checks are used.",
        "source_references": [
            ("prospector_engine.sensing", "Sensing.cue_save",
             "the mask capture/save path"),
        ],
    },
]

CAL_BY_ID = {c["id"]: c for c in CALIBRATION_ITEMS}


def _pix_set(cfg, key):
    v = cfg.get(key)
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        return False
    if list(v) == [0, 0]:
        return False
    return True


def _required_values_present(cfg, item):
    """When AUTO_CALIBRATE is off, a required item is only 'calibrated' if
    its values actually exist: every pixel key set (non-[0,0]) and, for the
    capacity bar, a plausible derived width. Prevents a hand-edited or
    partially-imported config from showing a false 'User-calibrated'."""
    for k in item["keys"]:
        if k.endswith("_PIXEL") or k.endswith("_PIX"):
            if not _pix_set(cfg, k):
                return False
        elif k == "CAP_BAR_WIDTH":
            try:
                if int(cfg.get(k, 0)) <= 20:
                    return False
            except (TypeError, ValueError):
                return False
    return True


def calibration_status(cfg, health=None, window_found=None):
    """Evaluate every registry item against the live config. Returns
    {item_id: {"status": ..., "detail": ...}}.

    Statuses: ok / auto / stale / unset / off.
    - 'auto'  -- AUTO_CALIBRATE places this from the ratio profile;
                 runnable out of the box.
    - 'ok'    -- user-calibrated: the values really exist AND the window
                 still matches.
    - 'stale' -- user-calibrated but the window moved/resized since.
    - 'unset' -- a needed value is missing (required item with
                 AUTO_CALIBRATE off, or an enabled optional feature
                 without its calibration).
    - 'off'   -- optional item whose activating feature is disabled.
    """
    auto = bool(cfg.get("AUTO_CALIBRATE", True))
    healthy = None
    if isinstance(health, dict):
        healthy = bool(health.get("ok", health.get("healthy", True)))
    out = {}
    for item in CALIBRATION_ITEMS:
        iid = item["id"]
        if iid == "roblox_window":
            if window_found is True:
                out[iid] = {"status": "ok", "detail": "Roblox window found."}
            elif window_found is False:
                out[iid] = {"status": "unset",
                            "detail": "Roblox is not open right now (fine "
                                      "to continue; detect before running)."}
            else:
                out[iid] = {"status": "unset", "detail": "Not checked yet."}
            continue
        if not item["required"]:
            cond = item.get("condition", "")
            flag_on = any(bool(cfg.get(flag.strip()))
                          for flag in cond.replace(" or ", ",").split(",")
                          if flag.strip().isupper())
            keys = item["keys"]
            any_set = any(_pix_set(cfg, k) for k in keys
                          if k.endswith("_PIXEL") or k.endswith("_PIX")) \
                or (keys == ["CUE_MASKS"] and bool(cfg.get("CUE_MASKS")))
            if any_set:
                out[iid] = {"status": "ok", "detail": "Calibrated."}
            elif flag_on:
                out[iid] = {"status": "unset",
                            "detail": "The feature that needs this (%s) is "
                                      "ON but it is not calibrated yet."
                                      % cond}
            else:
                out[iid] = {"status": "off",
                            "detail": "Not needed unless you enable %s."
                                      % (cond or "its feature")}
            continue
        # required items
        if auto:
            out[iid] = {"status": "auto",
                        "detail": "Placed automatically from the built-in "
                                  "profile each run. Calibrating by hand "
                                  "makes it exact for your setup."}
        elif not _required_values_present(cfg, item):
            out[iid] = {"status": "unset",
                        "detail": "Auto-calibration is off but this value "
                                  "is missing -- calibrate it, or turn "
                                  "auto-calibration back on."}
        elif healthy is False:
            out[iid] = {"status": "stale",
                        "detail": "Calibrated, but the Roblox window has "
                                  "moved or resized since -- recalibrate "
                                  "or restore the window."}
        else:
            out[iid] = {"status": "ok", "detail": "User-calibrated."}
    return out


def calibration_ready(statuses):
    """(ready, blockers): required items must be auto/ok; stale or missing
    required calibration blocks a run (the Readiness Check surfaces it)."""
    blockers = []
    for item in CALIBRATION_ITEMS:
        if not item["required"] or item["id"] == "roblox_window":
            continue
        st = statuses.get(item["id"], {}).get("status")
        if st not in ("auto", "ok"):
            blockers.append(item["id"])
    return (not blockers), blockers
