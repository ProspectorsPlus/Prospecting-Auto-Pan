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
# 2: Advanced cue matching (cue_masks) became a REQUIRED calibration; a
#    state file recorded under schema 1 by a FINISHED install marks the
#    cue_masks item NEEDS_REVIEW instead of silently un-finishing setup.
CALIBRATION_SCHEMA = 2

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
        "requested": [],
        "completed_at": 0,
        "last_readiness": None,
    }


class Onboarding(object):
    """Load/advance/persist the wizard state. All writes are atomic
    (tmp + os.replace); a torn write can only lose the last transition,
    never the file. The state file is written eagerly on first
    construction so (a) the packaged first-boot probe has a real
    bridge-liveness marker to watch and (b) migrate_legacy's
    "no prior state" guard tests what was on disk BEFORE this process,
    not a file this same process just created."""

    def __init__(self, data_dir, platform_key, version=""):
        self.path = os.path.join(data_dir, _STATE_FILE)
        self.platform = platform_key
        self.version = version
        self._existed = os.path.isfile(self.path)
        self.last_save_error = ""
        self.state = self._load()
        if not self._existed:
            self._save()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and d.get("state") in STATES:
                d.setdefault("schema", SCHEMA_VERSION)
                d.setdefault("declined_optional", [])
                d.setdefault("requested", [])
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
        The full wizard stays available from Help / Trust Center.
        Guarded by the AT-CONSTRUCTION file check (self._existed), never a
        live os.path.exists: a state file this process wrote can not make
        migration fire, and a WELCOME_SEEN flag written later this session
        can not either (the state has left NOT_STARTED by then)."""
        if (self.state["state"] == "NOT_STARTED" and welcome_seen
                and not self._existed):
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
            self.last_save_error = ""
            return True
        except OSError as e:
            self.last_save_error = str(e)
            return False

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

    def note_request(self, cap_id):
        """Record that the user explicitly requested this OS permission at
        least once. Lets the UI distinguish 'Not requested yet' (macOS was
        never asked, so no System Settings row exists) from a real
        'Not granted' after a request."""
        lst = self.state.setdefault("requested", [])
        if cap_id and cap_id not in lst:
            lst.append(cap_id)
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
        "id": "cue_masks",
        "title": "Advanced cue matching",
        "purpose": "The primary prompt detector: it matches the exact "
                   "letter shapes of the Pan / Collect Deposit / Shake "
                   "prompts instead of a single small white box, so a "
                   "player or texture that happens to be white cannot "
                   "trigger it. More reliable than single-pixel matching; "
                   "the pixel checks remain only as a supported fallback.",
        "keys": ["CUE_MASKS"],
        "required": True,
        "modes": ["all classic modes"],
        "cues": ["PAN", "DEPOSIT", "SHAKE"],
        "instructions": "Capture each of the three prompts once: make the "
                        "prompt visible in Roblox, click Capture, click the "
                        "prompt word in the view that opens, then Confirm.",
        "action": "cues",
        "dependencies": ["pan_prompt", "deposit_prompt", "shake_prompt"],
        "source_references": [
            ("prospector_engine.sensing", "Sensing.cue_save",
             "the mask capture/save path"),
            ("prospector_engine.engine", "Detector._cue_mask_match",
             "the run-time mask matcher"),
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
]

CAL_BY_ID = {c["id"]: c for c in CALIBRATION_ITEMS}


# ---------------------------------------------------------------------------
# Structured calibration instructions
# ---------------------------------------------------------------------------
# One entry per registry item, rendered on the guided detail page. Every
# claim below is grounded in the shipped detector code (the source_references
# on the registry entries); nothing is invented. Fields with empty lists are
# deliberately empty, not missing.

CALIBRATION_INSTRUCTIONS = {
    "roblox_window": {
        "title": "Roblox window",
        "purpose": "Every calibration is stored relative to the Roblox "
                   "window. Detection confirms the game is visible on the "
                   "primary display and records its position and size.",
        "affected_modes": ["Everything -- all detection is window-relative."],
        "prerequisites": ["Roblox is installed and Prospecting is loaded."],
        "roblox_setup_steps": [
            "Open Roblox and join Prospecting.",
            "Put the game on your PRIMARY display (the one with the menu "
            "bar on macOS), windowed or full screen.",
            "Leave the window where you intend to play -- moving or "
            "resizing it later makes calibration stale."],
        "player_position": "Anywhere -- only the window is detected here.",
        "camera_setup": "No camera requirement for this step.",
        "required_visible_elements": ["The Roblox game window itself."],
        "close_or_hide": [],
        "selection_target": "Nothing to click in the game -- press Detect "
                            "and the app looks the window up by its owner "
                            "name and bounds.",
        "exact_action": "Press 'Detect Roblox window'.",
        "correct_result": "The result line reports the window size and "
                          "position, e.g. 'Found: 1800x1087 at (0, 39)'.",
        "common_mistakes": [
            "Roblox is on a secondary display -- move it to the primary "
            "one.",
            "Roblox is minimised -- restore it before detecting."],
        "captured_data": "Only the window's owner name and bounds. No "
                         "screenshot is taken by this step.",
        "retention": "The window rectangle is saved to the local "
                     "calibration file only.",
        "validation": "The lookup must return found=true with a non-empty "
                      "rectangle.",
        "retry_help": "Open Roblox on the primary display and press Detect "
                      "again. Detection never gives up permanently -- it "
                      "reads the live window list each time.",
        "unavailable_without": "Runs can start, but the app cannot warn "
                               "you when the window moves and "
                               "auto-placement has nothing to anchor to.",
    },
    "cap_bar": {
        "title": "Pan capacity bar",
        "purpose": "The yellow pan-fill bar is the heartbeat of every "
                   "cycle: its right tip tells the macro the pan is full, "
                   "the yellow fraction of the whole bar detects digs "
                   "registering and the pan emptying.",
        "affected_modes": ["All classic modes (standard, Perfect, Shards, "
                           "Geodes, Treasure)."],
        "prerequisites": ["Roblox window detected on the primary display.",
                          "Screen-detection permission granted."],
        "roblox_setup_steps": [
            "Walk to the water and dig until your pan capacity bar is "
            "COMPLETELY full -- every segment yellow.",
            "The bar sits in the lower-centre of the screen; keep the "
            "default HUD so it is not covered by other UI."],
        "player_position": "In the water (so you can keep digging until "
                           "the bar is full).",
        "camera_setup": "Any camera angle -- the bar is screen-space UI "
                        "and does not move with the camera.",
        "required_visible_elements": ["The pan capacity bar, completely "
                                      "full (all yellow)."],
        "close_or_hide": ["Any menu, chat window or overlay covering the "
                          "lower-centre of the screen."],
        "selection_target": "Two single pixels on the bar: first the "
                            "extreme RIGHT tip of the yellow fill, then "
                            "the extreme LEFT tip where the fill begins.",
        "exact_action": "Press Start: the app auto-detects the widest "
                        "solid-gold segment in the lower-centre band and "
                        "opens a full-screen view with a red X on its "
                        "proposal. Check the X sits on the RIGHT tip, then "
                        "Confirm (or click the correct spot yourself). "
                        "Repeat for the LEFT tip.",
        "correct_result": "Both tips saved and a derived bar width greater "
                          "than 20 pixels -- the checklist row flips to "
                          "Complete and shows 'User-calibrated'.",
        "common_mistakes": [
            "The bar is not fully full, so the right tip is picked "
            "mid-bar and digs stop registering near the top.",
            "Clicking the decorative frame instead of the yellow fill -- "
            "the saved colour must be the gold fill itself.",
            "Calibrating while a popup covers the bar."],
        "captured_data": "Two screen coordinates plus the colour under "
                         "them; the screenshot shown during selection is "
                         "kept only in memory for that selection.",
        "retention": "Coordinates, derived width and colours are saved to "
                     "the local calibration file. The capture image is "
                     "discarded.",
        "validation": "The derived width (right.x - left.x) must exceed "
                      "20 px or the save is rejected; the runtime "
                      "re-checks the saved points every cycle.",
        "retry_help": "Press Start again any time -- each run replaces "
                      "the previous points atomically. If auto-detect "
                      "cannot find the bar, click the tips yourself in "
                      "the full-screen view.",
        "unavailable_without": "No classic mode can run: full/empty/dig "
                               "detection all read this bar.",
    },
    "pan_prompt": {
        "title": "'Pan' prompt",
        "purpose": "Anchors the water side of the walk cycle: the macro "
                   "knows it is back at the water when this white prompt "
                   "appears.",
        "affected_modes": ["All classic modes."],
        "prerequisites": ["Roblox window detected.",
                          "Screen-detection permission granted."],
        "roblox_setup_steps": [
            "Walk into the WATER so the white 'Pan' prompt text shows "
            "near the bottom of the screen.",
            "Stay still while capturing."],
        "player_position": "Standing in the water, close enough that the "
                           "'Pan' prompt is on screen.",
        "camera_setup": "Any -- the prompt is screen-space UI.",
        "required_visible_elements": ["The white 'Pan' prompt text."],
        "close_or_hide": ["Chat or menus overlapping the prompt."],
        "selection_target": "One pixel at the CENTRE of the white 'Pan' "
                            "word (the runtime checks a small box around "
                            "it for white text).",
        "exact_action": "Press Start: auto-detect proposes the prompt "
                        "pixel and opens the full-screen view with a red "
                        "X. Confirm it, or click the centre of the word "
                        "yourself.",
        "correct_result": "The saved pixel reads as white text whenever "
                          "the prompt shows -- 'Test detection' on the "
                          "Calibrate tab shows 'visible' while you stand "
                          "in water.",
        "common_mistakes": [
            "Clicking the edge of a letter -- aim mid-stroke on a thick "
            "letter.",
            "Capturing while the prompt is fading in or out."],
        "captured_data": "One screen coordinate and its colour; the "
                         "selection screenshot is memory-only.",
        "retention": "Saved to the local calibration file only.",
        "validation": "The runtime requires at least 12% white pixels in "
                      "the sample box around the point when the prompt "
                      "is on screen.",
        "retry_help": "Re-run any time; the previous value is replaced "
                      "atomically.",
        "unavailable_without": "The macro cannot confirm it reached the "
                               "water; classic cycles will not run "
                               "reliably.",
    },
    "deposit_prompt": {
        "title": "'Collect Deposit' prompt",
        "purpose": "Anchors the land side: digging starts when this "
                   "prompt is visible, and its width also guards the "
                   "other two prompts against false positives.",
        "affected_modes": ["All classic modes."],
        "prerequisites": ["Roblox window detected.",
                          "Screen-detection permission granted."],
        "roblox_setup_steps": [
            "Step onto LAND at the dig spot so the white 'Collect "
            "Deposit' prompt shows.",
            "Stay still while capturing."],
        "player_position": "On land, at a deposit, prompt visible.",
        "camera_setup": "Any -- screen-space UI.",
        "required_visible_elements": ["The white 'Collect Deposit' "
                                      "prompt text."],
        "close_or_hide": ["Anything overlapping the prompt."],
        "selection_target": "One pixel at the CENTRE of the white "
                            "'Collect Deposit' text.",
        "exact_action": "Press Start: confirm the auto-detected red X on "
                        "the prompt, or click the centre of the text "
                        "yourself.",
        "correct_result": "'Test detection' shows the Deposit cue "
                          "'visible' while you stand at a deposit.",
        "common_mistakes": [
            "Capturing the wrong prompt (e.g. a shop prompt) -- it must "
            "be 'Collect Deposit'.",
            "Clicking between two words where the background shows."],
        "captured_data": "One screen coordinate and its colour.",
        "retention": "Saved to the local calibration file only.",
        "validation": "Same white-text box check as the Pan prompt.",
        "retry_help": "Re-run any time; atomic replace.",
        "unavailable_without": "Digs never start -- the land anchor is "
                               "missing.",
    },
    "shake_prompt": {
        "title": "'Shake' prompt",
        "purpose": "Confirms a shake actually started, so a missed shake "
                   "is retried instead of wasting the pan.",
        "affected_modes": ["All classic modes."],
        "prerequisites": ["Roblox window detected.",
                          "Screen-detection permission granted."],
        "roblox_setup_steps": [
            "Fill the pan, walk to the water and BEGIN a shake so the "
            "white 'Shake' prompt shows."],
        "player_position": "In the water, mid-shake.",
        "camera_setup": "Any -- screen-space UI.",
        "required_visible_elements": ["The white 'Shake' prompt text."],
        "close_or_hide": ["Anything overlapping the prompt."],
        "selection_target": "One pixel at the CENTRE of the white "
                            "'Shake' word.",
        "exact_action": "Press Start: confirm the auto-detected red X, "
                        "or click the centre of the word yourself.",
        "correct_result": "'Test detection' shows the Shake cue "
                          "'visible' during a shake.",
        "common_mistakes": [
            "Waiting too long -- the prompt disappears once the shake "
            "completes; start the capture as the shake begins."],
        "captured_data": "One screen coordinate and its colour.",
        "retention": "Saved to the local calibration file only.",
        "validation": "Same white-text box check as the other prompts.",
        "retry_help": "Re-run any time; atomic replace.",
        "unavailable_without": "Missed shakes go unnoticed and pans are "
                               "wasted.",
    },
    "cue_masks": {
        "title": "Advanced cue matching",
        "purpose": "The REQUIRED primary detector for the three prompts. "
                   "Instead of one small white box, it stores the exact "
                   "letter shapes of 'Pan', 'Collect Deposit' and 'Shake' "
                   "and requires 85% of those letter pixels to read white "
                   "-- so a white shirt or bright texture behind the "
                   "prompt area cannot fake a prompt. It tolerates minor "
                   "visual variation better than a single exact pixel "
                   "because it looks at the whole word shape.",
        "affected_modes": ["All classic modes -- every prompt read uses "
                           "the mask first; the single-pixel check "
                           "remains only as a supported fallback."],
        "prerequisites": [
            "The three prompt pixels above are calibrated (the masks "
            "anchor to the same prompts).",
            "Roblox window detected; screen-detection permission "
            "granted."],
        "roblox_setup_steps": [
            "You will capture three masks, one per prompt, each while "
            "its prompt is on screen:",
            "1. Stand in the WATER for 'Pan'.",
            "2. Step onto LAND at a deposit for 'Collect Deposit'.",
            "3. Begin a shake for 'Shake'."],
        "player_position": "Wherever the current prompt shows (water / "
                           "land / mid-shake).",
        "camera_setup": "Any -- prompts are screen-space UI. Do NOT "
                        "resize the Roblox window between captures.",
        "required_visible_elements": ["The current prompt's white text, "
                                      "fully visible."],
        "close_or_hide": ["Anything overlapping the prompt text."],
        "selection_target": "The prompt WORD itself: click anywhere on "
                            "the white letters in the capture view.",
        "exact_action": "Press Capture for the listed prompt: a "
                        "full-screen view opens over the game. Click the "
                        "prompt word; every white letter lights up green. "
                        "Click any stray white blob (like the mouse "
                        "cursor) to remove it from the selection, then "
                        "Confirm. Repeat for all three prompts.",
        "correct_result": "Each prompt shows a green letter-shape "
                          "preview and a pixel count; the step is "
                          "Complete when all three masks exist.",
        "common_mistakes": [
            "Capturing with the window later resized -- masks are "
            "size-exact and disable themselves if the window size "
            "drifts more than 2 px (re-capture after any resize).",
            "Leaving the white mouse cursor inside the selection -- "
            "click it to exclude it.",
            "Capturing the wrong prompt for the slot."],
        "captured_data": "A small black-and-white letter-shape bitmap "
                         "per prompt plus its position as a fraction of "
                         "the window, and a small preview image.",
        "retention": "Masks and previews live in the local calibration "
                     "file only. Nothing leaves this computer.",
        "validation": "A mask must contain at least 8 letter pixels to "
                      "save; at run start each mask is re-placed from "
                      "its stored window fractions and disabled (with a "
                      "recalibration warning) if the window size "
                      "changed; at run time a prompt counts as visible "
                      "only when 85% of its mask pixels read white.",
        "retry_help": "Re-capture any single prompt any time; each "
                      "capture replaces only that prompt's mask. Clear a "
                      "mask from the Calibrate tab's cue gallery.",
        "unavailable_without": "Setup is not complete and Start stays "
                               "blocked for classic modes: single-pixel "
                               "data alone no longer counts as ready.",
    },
    "dig_green": {
        "title": "Green dig-bar zone",
        "purpose": "The green target zone of the dig bar, used by "
                   "Perfect digs and the green-confirm nudges.",
        "affected_modes": ["Perfect", "Shards (green confirm)",
                           "Geodes (green confirm)"],
        "prerequisites": ["Roblox window detected."],
        "roblox_setup_steps": [
            "Start a dig so the dig bar with its green zone is on "
            "screen."],
        "player_position": "On land, mid-dig.",
        "camera_setup": "Any.",
        "required_visible_elements": ["The dig bar's green zone."],
        "close_or_hide": [],
        "selection_target": "One pixel INSIDE the green zone.",
        "exact_action": "Press Start and click inside the green zone in "
                        "the full-screen view, then Confirm.",
        "correct_result": "The saved pixel reads green while the bar "
                          "shows its green zone.",
        "common_mistakes": ["Clicking the moving marker instead of the "
                            "static green zone."],
        "captured_data": "One screen coordinate and its colour.",
        "retention": "Local calibration file only.",
        "validation": "Used live during Perfect digs; a wrong pixel "
                      "simply never triggers.",
        "retry_help": "Re-run any time.",
        "unavailable_without": "Perfect mode and green-confirm nudges "
                               "will not fire; plain digs still work.",
    },
    "money_region": {
        "title": "Money counter box",
        "purpose": "Lets the earnings tracker read your money counter "
                   "with on-device OCR.",
        "affected_modes": ["Earnings tracker"],
        "prerequisites": ["Roblox window detected."],
        "roblox_setup_steps": ["Make sure the money counter is visible "
                               "in its usual corner."],
        "player_position": "Anywhere.",
        "camera_setup": "Any.",
        "required_visible_elements": ["The money number."],
        "close_or_hide": ["Menus covering the counter."],
        "selection_target": "A tight rectangle around the money number "
                            "(at least 8x6 px).",
        "exact_action": "Press Start and drag a box around the number, "
                        "then Confirm.",
        "correct_result": "'Test read' on the Calibrate tab returns the "
                          "current number.",
        "common_mistakes": ["Boxing the currency icon too -- keep the "
                            "box tight around the digits."],
        "captured_data": "Two corner coordinates; a small preview crop "
                         "is stored locally so you can see what is "
                         "boxed.",
        "retention": "Coordinates and the preview stay in the local "
                     "calibration file. OCR runs on this machine only.",
        "validation": "The drag must be at least 8x6 px; the read test "
                      "proves the OCR result.",
        "retry_help": "Re-drag any time.",
        "unavailable_without": "Earnings stats stay empty; nothing else "
                               "changes.",
    },
    "shards_region": {
        "title": "Shards counter box",
        "purpose": "Same as the money box, for the shards counter.",
        "affected_modes": ["Earnings tracker"],
        "prerequisites": ["Roblox window detected."],
        "roblox_setup_steps": ["Make sure the shards counter is "
                               "visible."],
        "player_position": "Anywhere.",
        "camera_setup": "Any.",
        "required_visible_elements": ["The shards number."],
        "close_or_hide": ["Menus covering the counter."],
        "selection_target": "A tight rectangle around the shards number.",
        "exact_action": "Press Start and drag a box around the number, "
                        "then Confirm.",
        "correct_result": "'Test read' returns the current shards "
                          "value.",
        "common_mistakes": ["Loose boxes that catch neighbouring UI."],
        "captured_data": "Two corner coordinates plus a local preview "
                         "crop.",
        "retention": "Local calibration file only; OCR is on-device.",
        "validation": "Minimum drag size + read test.",
        "retry_help": "Re-drag any time.",
        "unavailable_without": "Shard stats stay empty.",
    },
    "find_region": {
        "title": "Finds pop-up box",
        "purpose": "Lets the finds tracker read item pop-ups with "
                   "on-device OCR.",
        "affected_modes": ["Finds tracker"],
        "prerequisites": ["Roblox window detected."],
        "roblox_setup_steps": ["Note where find pop-ups appear (usually "
                               "after a shake completes)."],
        "player_position": "Anywhere.",
        "camera_setup": "Any.",
        "required_visible_elements": ["The area where find pop-ups "
                                      "appear (a pop-up on screen helps "
                                      "aim but is not required)."],
        "close_or_hide": [],
        "selection_target": "A rectangle over the pop-up area.",
        "exact_action": "Press Start and drag the box, then Confirm.",
        "correct_result": "'Test read' while a find is showing returns "
                          "its text lines.",
        "common_mistakes": ["A box too small to contain a full pop-up "
                            "line."],
        "captured_data": "Two corner coordinates plus a local preview "
                         "crop.",
        "retention": "Local calibration file only; OCR is on-device.",
        "validation": "Minimum drag size + read test.",
        "retry_help": "Re-drag any time.",
        "unavailable_without": "The finds log stays empty.",
    },
    "fortune_river": {
        "title": "Fortune River recovery points",
        "purpose": "Recovery clicks for the Fortune River event UI, so "
                   "the macro can recover if the event interrupts a "
                   "run.",
        "affected_modes": ["Fortune River recovery"],
        "prerequisites": ["The Fortune River event UI is on screen for "
                          "the points you capture."],
        "roblox_setup_steps": ["Open the Fortune River UI in Roblox "
                               "before capturing its points."],
        "player_position": "Wherever the event UI is visible.",
        "camera_setup": "Any.",
        "required_visible_elements": ["The event UI element for the "
                                      "point being captured."],
        "close_or_hide": [],
        "selection_target": "One point (or scan line) per button below "
                            "-- each opens the same full-screen picker.",
        "exact_action": "Capture each point with its own button; every "
                        "capture opens the picker, you click the spot, "
                        "then Confirm.",
        "correct_result": "Each captured point shows its saved "
                          "coordinates.",
        "common_mistakes": ["Capturing with the event UI closed."],
        "captured_data": "Screen coordinates and colours for the "
                         "captured points.",
        "retention": "Local calibration file only.",
        "validation": "Values are used only when Fortune River recovery "
                      "is enabled.",
        "retry_help": "Re-capture any single point any time.",
        "unavailable_without": "Fortune River auto-recovery stays off.",
    },
    "autopan_button": {
        "title": "Auto Pan button",
        "purpose": "Lets relic tracking verify the Auto Pan toggle "
                   "state from its button colour.",
        "affected_modes": ["Relic tracker"],
        "prerequisites": ["The Auto Pan button is visible."],
        "roblox_setup_steps": ["Make sure the Auto Pan button is on "
                               "screen in its normal position."],
        "player_position": "Anywhere.",
        "camera_setup": "Any.",
        "required_visible_elements": ["The Auto Pan button."],
        "close_or_hide": [],
        "selection_target": "The CENTRE of the Auto Pan button.",
        "exact_action": "Press Start and click the button centre in the "
                        "picker, then Confirm (capture once with Auto "
                        "Pan ON; the OFF colour can be captured from "
                        "the Calibrate tab).",
        "correct_result": "The saved pixel and colour identify the "
                          "toggle state.",
        "common_mistakes": ["Capturing while a tooltip covers the "
                            "button."],
        "captured_data": "One coordinate plus the button colour.",
        "retention": "Local calibration file only.",
        "validation": "The tracker compares the live colour against the "
                      "saved ON/OFF colours.",
        "retry_help": "Re-capture any time.",
        "unavailable_without": "The relic tracker degrades gracefully "
                               "and logs that it cannot verify Auto "
                               "Pan.",
    },
}


def _pix_set(cfg, key):
    v = cfg.get(key)
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        return False
    if list(v) == [0, 0]:
        return False
    return True


REQUIRED_CUES = ("PAN", "DEPOSIT", "SHAKE")


def cue_masks_state(cfg):
    """(captured, missing) for the three required advanced-cue masks. A mask
    counts only when it carries everything place_cue_masks needs to actually
    place it at run time (bits + a 4-element ratio + positive w/h) -- a
    hand-edited entry the runtime would silently skip must not read as
    captured."""
    masks = cfg.get("CUE_MASKS")
    masks = masks if isinstance(masks, dict) else {}
    captured, missing = [], []
    for cue in REQUIRED_CUES:
        m = masks.get(cue)
        ok = False
        if isinstance(m, dict) and m.get("bits"):
            ratio = m.get("ratio")
            try:
                ok = (isinstance(ratio, (list, tuple)) and len(ratio) == 4
                      and int(m.get("w", 0)) > 0 and int(m.get("h", 0)) > 0)
            except (TypeError, ValueError):
                ok = False
        if ok:
            captured.append(cue)
        else:
            missing.append(cue)
    return captured, missing


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
        elif k == "CUE_MASKS":
            if cue_masks_state(cfg)[1]:
                return False
    return True


def calibration_status(cfg, health=None, window_found=None,
                       setup_finished=False):
    """Evaluate every registry item against the live config. Returns
    {item_id: {"status": ..., "detail": ...}}.

    Statuses: ok / auto / stale / unset / needs_review / off.
    - 'auto'  -- AUTO_CALIBRATE places this from the ratio profile;
                 runnable out of the box.
    - 'ok'    -- user-calibrated: the values really exist AND the window
                 still matches.
    - 'stale' -- user-calibrated but the window moved/resized since.
    - 'unset' -- a needed value is missing (required item with
                 AUTO_CALIBRATE off, or an enabled optional feature
                 without its calibration).
    - 'needs_review' -- an install that finished setup before Advanced
                 cue matching became required: the old single-pixel
                 values are preserved and keep working as a fallback,
                 but the required masks are missing and must be
                 captured before the setup counts as ready again.
    - 'off'   -- optional item whose activating feature is disabled.

    Advanced cue matching (cue_masks) is required and can NEVER report
    'auto': auto-calibration places pixels from the ratio profile, but a
    letter-shape mask can only come from a real capture on this machine.
    """
    auto = bool(cfg.get("AUTO_CALIBRATE", True))
    healthy = None
    if isinstance(health, dict):
        healthy = bool(health.get("ok", health.get("healthy", True)))
    out = {}
    for item in CALIBRATION_ITEMS:
        iid = item["id"]
        if iid == "cue_masks":
            captured, missing = cue_masks_state(cfg)
            if not bool(cfg.get("ADVANCED_CUES", True)):
                out[iid] = {"status": "unset",
                            "detail": "Advanced cue matching is switched "
                                      "OFF. It is required -- turn it back "
                                      "on (Calibrate tab) and capture any "
                                      "missing prompt masks."}
            elif not missing:
                if healthy is False:
                    out[iid] = {"status": "stale",
                                "detail": "Masks captured, but the Roblox "
                                          "window has moved or resized "
                                          "since -- re-capture the three "
                                          "prompts or restore the window."}
                else:
                    out[iid] = {"status": "ok",
                                "detail": "All three prompt masks are "
                                          "captured (%s)."
                                          % ", ".join(captured)}
            elif setup_finished:
                out[iid] = {"status": "needs_review",
                            "detail": "Advanced cue matching is now "
                                      "required. Your existing calibration "
                                      "is preserved and still works as a "
                                      "fallback, but the %s mask%s must be "
                                      "captured before setup is complete "
                                      "again."
                                      % (", ".join(missing),
                                         "s" if len(missing) > 1 else "")}
            else:
                out[iid] = {"status": "unset",
                            "detail": "Not captured yet: %s. Capture each "
                                      "prompt once from the guided step."
                                      % ", ".join(missing)}
            continue
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


def compose_registry(cfg, health=None, window_found=None,
                     setup_finished=False, owner=False):
    """The full guided-calibration payload (items + live statuses +
    sequential progression + structured instructions) composed purely from
    the registry and the inputs -- shared by Api.calibration_registry and
    the UI regression tests so both surfaces render EXACTLY the same
    data. The Roblox window is a live PREREQUISITE check (its own banner,
    re-checked inside every detail page), not a numbered step: numbering
    starts at Capacity. Progress-completeness means USER-calibrated
    ('ok'): 'auto' keeps runs working, but the guided checklist walks the
    user through making each value exact."""
    statuses = calibration_status(cfg, health=health,
                                  window_found=window_found,
                                  setup_finished=setup_finished)
    steps = []
    for item in CALIBRATION_ITEMS:
        iid = item["id"]
        if iid == "roblox_window":
            continue
        st = statuses.get(iid, {}).get("status")
        steps.append({"id": iid, "required": item["required"],
                      "complete": st == "ok",
                      "needs_review": st == "needs_review",
                      "review_reason": statuses.get(iid, {}).get(
                          "detail", ""),
                      "title": item["title"]})
    prog = progression(steps)
    summary = prog.pop("", {})
    items = []
    for item in CALIBRATION_ITEMS:
        it = dict(item)
        it["refs"] = [{"module": m, "symbol": sym or "(module)",
                       "why": w}
                      for (m, sym, w) in item["source_references"]]
        it.pop("source_references", None)
        it["live"] = statuses.get(item["id"], {})
        it["prog"] = prog.get(item["id"],
                              {"state": "CHECK", "seq": 0,
                               "reason": "Live prerequisite check."})
        it["instruction"] = CALIBRATION_INSTRUCTIONS.get(item["id"], {})
        items.append(it)
    ready, blockers = calibration_ready(statuses)
    return {"items": items, "ready": ready, "blockers": blockers,
            "auto_calibrate": bool(cfg.get("AUTO_CALIBRATE", True)),
            "advanced_cues": bool(cfg.get("ADVANCED_CUES", True)),
            "owner": bool(owner),
            "progress": summary, "window_found": window_found,
            "setup_finished": bool(setup_finished),
            "schema": CALIBRATION_SCHEMA}


def calibration_ready(statuses):
    """(ready, blockers): required items must be auto/ok; stale, missing or
    needs-review required calibration blocks a run (the Readiness Check
    surfaces it, and launch() enforces the same condition). Single-pixel-only
    data can never be ready: cue_masks is required and never reports auto."""
    blockers = []
    for item in CALIBRATION_ITEMS:
        if not item["required"] or item["id"] == "roblox_window":
            continue
        st = statuses.get(item["id"], {}).get("status")
        if st not in ("auto", "ok"):
            blockers.append(item["id"])
    return (not blockers), blockers


# ---------------------------------------------------------------------------
# Sequential progression
# ---------------------------------------------------------------------------
# One engine for every setup checklist (guided calibration AND the trust /
# permissions step): ordered steps, exactly one ACTIVE required step, later
# required steps UPCOMING, optional steps never blocking. States are derived
# from the REAL statuses passed in -- the UI layer only renders them.

STEP_STATES = ("COMPLETE", "ACTIVE", "UPCOMING", "OPTIONAL", "BLOCKED",
               "FAILED", "NEEDS_REVIEW")


def progression(steps):
    """steps: ordered list of dicts with
         id, required(bool), complete(bool),
         needs_review(bool, optional), blocked_reason(str, optional),
         title(str, optional -- used in generated reasons).
    Returns {id: {"state": ..., "seq": n, "reason": str}} plus a summary
    under the "" key: {"total": required count, "done": complete count,
    "active": id or None}.

    Rules: required steps are numbered in order; completed ones are
    COMPLETE and stay reopenable; the FIRST incomplete required step is
    ACTIVE (or NEEDS_REVIEW at the same position, or BLOCKED when a
    blocked_reason is given); every later incomplete required step is
    UPCOMING with a 'Complete X first' reason; optional steps are OPTIONAL
    and never block. FAILED is a UI-session overlay on the active step (a
    failed attempt stays visible in place), not derived here."""
    out = {}
    seq = 0
    total = done = 0
    active_id = None
    gate_title = None      # the step the user must finish first
    for s in steps:
        sid = s["id"]
        if not s.get("required"):
            out[sid] = {"state": "OPTIONAL", "seq": 0,
                        "reason": "Optional -- never blocks setup."}
            continue
        seq += 1
        total += 1
        if s.get("complete"):
            done += 1
            out[sid] = {"state": "COMPLETE", "seq": seq,
                        "reason": "Complete -- open it any time to review "
                                  "or redo it."}
            continue
        if active_id is None:
            active_id = sid
            if s.get("blocked_reason"):
                out[sid] = {"state": "BLOCKED", "seq": seq,
                            "reason": s["blocked_reason"]}
            elif s.get("needs_review"):
                out[sid] = {"state": "NEEDS_REVIEW", "seq": seq,
                            "reason": s.get("review_reason",
                                            "Existing data needs a review "
                                            "before this step counts as "
                                            "complete.")}
            else:
                out[sid] = {"state": "ACTIVE", "seq": seq,
                            "reason": "Do this next."}
            gate_title = s.get("title") or sid
        else:
            out[sid] = {"state": "UPCOMING", "seq": seq,
                        "reason": "Complete %s first." % gate_title}
    out[""] = {"total": total, "done": done, "active": active_id}
    return out
