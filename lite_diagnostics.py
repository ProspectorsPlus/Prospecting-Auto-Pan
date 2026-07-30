#!/usr/bin/env python3
"""lite_diagnostics.py -- pure diagnostic/recommendation engine.

Architecture (chunk D1 of the release pass; the host/UI wiring is D2):

  *  SETTING_REGISTRY  -- derived at import from prospecting_ui.SECTIONS
     (labels/sections/types/defaults) + prospecting_assistant.RANGES
     (lo/hi/step) + HELP (descriptions) + a curated _META layer
     (effects / tradeoffs / related diagnostics / auto-apply safety).
     Placement mirrors the shipped UI: the seven engine-tuning sections
     render on the Cycle page (control='cycle'), every other section is
     a tab whose id IS the section title (control='tab').
  *  evaluate(ctx)     -- the rule engine. ctx is a plain dict the host
     assembles (stats, event counts, calibration status, capabilities,
     settings, launch refusal...). Rules A..O + CAL-STALE below emit
     DiagnosticEvent dicts with real numeric evidence, bounded clamped
     setting suggestions and FAQ links. Missing ctx keys never raise.
  *  escalate / merge_events / apply_suppressions / load+save_suppressions
     -- pure helpers the host store uses for recurrence, severity
     ladders and the tiny JSON suppression file (atomic write).
  *  FAQ_ENTRIES + validate_faq -- the knowledge base every event links
     into; validation asserts every referenced setting/calibration/
     permission/faq id is real.

This module is PURE: no screen access, no engine import, no file I/O
except the two suppression-store helpers, which take explicit paths.
"""

import json
import os

# --------------------------------------------------------------------------
# Tolerant imports -- exactly like prospecting_app.py: a missing or broken
# schema module must never crash the app; diagnostics degrade to empty
# metadata instead.
# --------------------------------------------------------------------------
try:
    import prospecting_ui as _ui
    SECTIONS = getattr(_ui, "SECTIONS", [])
    DEFAULTS = getattr(_ui, "DEFAULTS", {})
    TYPES = getattr(_ui, "TYPES", {})
    HELP = getattr(_ui, "HELP", {})
except Exception:                        # pragma: no cover - broken install
    SECTIONS, DEFAULTS, TYPES, HELP = [], {}, {}, {}

try:
    import prospecting_assistant as _coach
    RANGES = dict(getattr(_coach, "RANGES", {}))
except Exception:                        # pragma: no cover - broken install
    RANGES = {}
# the app adds these two bounds for its sliders (prospecting_app.py:7636-7637);
# mirror them so suggestions for the safe-stop policy stay bounded too.
RANGES.setdefault("SAFE_STOP_RETRY_SEC", (10, 600, 10))
RANGES.setdefault("SAFE_STOP_MAX_RETRIES", (0, 10, 1))

try:
    import lite_onboarding as _onb
    CALIBRATION_IDS = tuple(c.get("id") for c in
                            getattr(_onb, "CALIBRATION_ITEMS", [])
                            if isinstance(c, dict) and c.get("id"))
except Exception:                        # pragma: no cover - broken install
    CALIBRATION_IDS = ()
if not CALIBRATION_IDS:                  # pragma: no cover - fallback only
    CALIBRATION_IDS = ("roblox_window", "cap_bar", "pan_prompt",
                       "deposit_prompt", "shake_prompt", "cue_masks",
                       "dig_green", "money_region", "shards_region",
                       "find_region", "fortune_river", "autopan_button")

try:
    import lite_trust as _trust
    CAPABILITY_IDS = tuple(c.get("id") for c in
                           getattr(_trust, "CAPABILITIES", [])
                           if isinstance(c, dict) and c.get("id"))
except Exception:                        # pragma: no cover - broken install
    CAPABILITY_IDS = ()
if not CAPABILITY_IDS:                   # pragma: no cover - fallback only
    CAPABILITY_IDS = ("screen_detection", "input_control", "stop_hotkeys",
                      "discord_notifications", "coach_ai", "sound_alerts",
                      "microphone", "camera", "location", "admin_privileges")

# The launch gate blocks on these three (Api.launch 'perm:' path).
REQUIRED_CAPABILITIES = ("screen_detection", "input_control", "stop_hotkeys")

_CAPABILITY_TITLES = {
    "screen_detection": "Screen detection (Screen Recording)",
    "input_control": "Keyboard & mouse control (Accessibility)",
    "stop_hotkeys": "Safe Stop & global hotkeys (Input Monitoring)",
}

# --------------------------------------------------------------------------
# D1.1 Severities and models
# --------------------------------------------------------------------------
SEVERITIES = ("INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL")
CONFIDENCES = ("possible", "medium", "high")

_SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}
_CONF_RANK = {c: i for i, c in enumerate(CONFIDENCES)}

# The seven engine-tuning sections the Cycle page absorbs as stage cards
# (prospecting_app.py _MOVED, :7627-7629). Everything else renders on a
# section tab whose data-tab IS the section title.
_MOVED_SECTIONS = ("Easy tuning", "Mode / Dig", "Walk back into water",
                   "Shake", "Return to land (dig-probe)", "Recovery / safety",
                   "Recovery movement (jitter taps)")

# Calibration statuses that mean "was calibrated, needs another look".
_STALE_STATUSES = ("stale", "needs_review")


def _units_from_label(label):
    """Best-effort unit string parsed from the shipped label text."""
    lab = label or ""
    if "(ms" in lab:
        return "ms"
    if "(seconds" in lab or "(s)" in lab or "(s," in lab:
        return "s"
    if "(%" in lab:
        return "%"
    if "(0-1)" in lab or "(0 to 1)" in lab:
        return "fraction"
    return ""


# --------------------------------------------------------------------------
# D1.2 SETTING_REGISTRY
# --------------------------------------------------------------------------
# Curated metadata for every key a rule targets. effects/tradeoffs are the
# per-knob story a diagnostic card shows; related_diagnostics are the rule
# codes that may point here; safe_auto_apply marks keys where a one-step
# bounded change is safe to offer as a one-click apply.
_META = {
    "WATER_EXTRA_BACK_MS": {
        "effects": "Extra S held after the Pan cue -- how much deeper the "
                   "walk-back goes past the detected water edge.",
        "tradeoffs": "Too high overshoots the stop point and forces "
                     "corrective nudges; too low can leave the character "
                     "short of deeper water.",
        "related_diagnostics": ["PP-D-NUDGE-FAR"],
        "safe_auto_apply": True,
    },
    "SHAKE_START_DELAY_MS": {
        "effects": "Pause between reaching the water and starting the "
                   "shake, letting movement momentum settle.",
        "tradeoffs": "Higher is steadier but each cycle takes longer; "
                     "lower risks shaking while still moving.",
        "related_diagnostics": ["PP-D-NUDGE-FAR", "PP-D-SHAKE-EARLY",
                                "PP-D-SHAKE-LATE"],
        "safe_auto_apply": True,
    },
    "LAND_SETTLE_MS": {
        "effects": "How long W is held after the land cue so the "
                   "character sits firmly on the dirt before digging.",
        "tradeoffs": "Higher prevents slide-back nudges; too high wastes "
                     "time each cycle.",
        "related_diagnostics": ["PP-D-NUDGE-FAR"],
        "safe_auto_apply": True,
    },
    "SHAKE_START_CONFIRM_MS": {
        "effects": "The fast save (0 = off): if the pan is still full "
                   "this long after the shake click, retry immediately.",
        "tradeoffs": "Too low retries a shake that was about to start; "
                     "0 disables the early confirmation entirely.",
        "related_diagnostics": ["PP-D-SHAKE-EARLY"],
        "safe_auto_apply": True,
    },
    "EASY_SHAKE_DELAY_MS": {
        "effects": "Plain-language layer: waits this much longer before "
                   "the shake begins (added onto the Shake delay at bind "
                   "time).",
        "tradeoffs": "Slightly slower cycles.",
        "related_diagnostics": ["PP-D-SHAKE-EARLY"],
        "safe_auto_apply": True,
    },
    "SHAKE_BAIL_MS": {
        "effects": "Shake-failed detection: if the pan is still "
                   "completely full after this long the shake counts as "
                   "failed and is retried.",
        "tradeoffs": "Too low marks slow-starting shakes as failed; too "
                     "high wastes time on genuinely failed shakes.",
        "related_diagnostics": ["PP-D-SHAKE-LATE"],
        "safe_auto_apply": True,
    },
    "AUTOPAN_SETTLE_MS": {
        "effects": "Pause after each Auto Pan click before the button "
                   "colour is re-read to confirm the click worked.",
        "tradeoffs": "Higher tolerates UI lag; too high slows every "
                     "Auto Pan interaction.",
        "related_diagnostics": ["PP-D-AUTOPAN-STUCK"],
        "safe_auto_apply": True,
    },
    "AUTOPAN_TOL": {
        "effects": "How far each colour channel may drift from the "
                   "calibrated ON/OFF colours before the button state "
                   "reads as unknown.",
        "tradeoffs": "Too tight misreads the button under lighting "
                     "shifts; too loose can confuse ON with OFF.",
        "related_diagnostics": ["PP-D-AUTOPAN-STUCK"],
        "safe_auto_apply": True,
    },
    "AUTOPAN_STALL_SEC": {
        "effects": "The idle kick: restart Auto Pan if the bar has been "
                   "idle this long (0 = off).",
        "tradeoffs": "A kick during a legitimate pause re-clicks the "
                     "button unnecessarily.",
        "related_diagnostics": ["PP-D-AUTOPAN-STUCK"],
        "safe_auto_apply": True,
    },
    "RECOVER_LIMIT": {
        "effects": "How many gentle recovery nudges on the same spot "
                   "before escalating to a break-out.",
        "tradeoffs": "Lower escalates out of loops sooner but may "
                     "break-out when one more nudge would have worked.",
        "related_diagnostics": ["PP-D-RECOVERY-LOOP"],
        "safe_auto_apply": True,
    },
    "NO_PROGRESS_SEC": {
        "effects": "The watchdog: seconds without progress before "
                   "click-to-empty recovery fires.",
        "tradeoffs": "Too low fires during normal slow cycles; too high "
                     "leaves real stalls running longer.",
        "related_diagnostics": ["PP-D-RECOVERY-LOOP", "PP-D-STALL"],
        "safe_auto_apply": True,
    },
    "RECOVER_BACK_MS": {
        "effects": "The nudge budget: movement per recovery nudge, made "
                   "of pulsed taps.",
        "tradeoffs": "Bigger nudges free harder wedges but overshoot "
                     "more when the spot was nearly right.",
        "related_diagnostics": ["PP-D-RECOVERY-LOOP"],
        "safe_auto_apply": True,
    },
    "FINDS_MIN_CONF": {
        "effects": "OCR confidence gate (0-1) a text read must clear to "
                   "start a find on its own.",
        "tradeoffs": "Lower catches more finds but admits more OCR "
                     "misreads; higher misses faint cards.",
        "related_diagnostics": ["PP-D-FINDS-MISS"],
        "safe_auto_apply": True,
    },
    "FINDS_CARD_SEC": {
        "effects": "How long one find card lives on screen, animations "
                   "included -- the re-sighting window.",
        "tradeoffs": "Too long merges distinct finds; too short splits "
                     "one find into ghosts.",
        "related_diagnostics": ["PP-D-FINDS-MISS"],
        "safe_auto_apply": True,
    },
    "FINDS_EMPTY_MS": {
        "effects": "Stack reset timer: quiet ms before lingering cards "
                   "are considered gone.",
        "tradeoffs": "Too short resets mid-animation and forks cards; "
                     "too long delays fresh finds.",
        "related_diagnostics": ["PP-D-FINDS-MISS"],
        "safe_auto_apply": True,
    },
    "EARN_OCR_SEC": {
        "effects": "How often the money/shards totals are read; earnings "
                   "count as the difference between reads.",
        "tradeoffs": "Reading more often keeps totals fresh but costs "
                     "more OCR work.",
        "related_diagnostics": ["PP-D-ANALYTICS-STALE"],
        "safe_auto_apply": True,
    },
}


def build_setting_registry():
    """{key: entry} derived from SECTIONS + RANGES + HELP + _META.

    Entry: key, label, section, type, default, control ('cycle'|'tab'),
    tab ('cycle' or the section title), lo/hi/step (None without a RANGES
    entry), units, help, effects, tradeoffs, related_diagnostics,
    safe_auto_apply. safe_auto_apply is False whenever the key has no
    RANGES bounds, regardless of _META."""
    reg = {}
    for section, items in SECTIONS:
        on_cycle = section in _MOVED_SECTIONS
        for (key, label, typ, default) in items:
            lo, hi, step = RANGES.get(key, (None, None, None))
            meta = _META.get(key, {})
            reg[key] = {
                "key": key,
                "label": label,
                "section": section,
                "type": typ,
                "default": default,
                "control": "cycle" if on_cycle else "tab",
                "tab": "cycle" if on_cycle else section,
                "lo": lo, "hi": hi, "step": step,
                "units": _units_from_label(label),
                "help": HELP.get(key, ""),
                "effects": meta.get("effects", ""),
                "tradeoffs": meta.get("tradeoffs", ""),
                "related_diagnostics": list(
                    meta.get("related_diagnostics", [])),
                "safe_auto_apply": bool(meta.get("safe_auto_apply"))
                                   and key in RANGES,
            }
    return reg


SETTING_REGISTRY = build_setting_registry()


def clamp_suggestion(key, value):
    """Clamp a suggested numeric value into the key's RANGES [lo, hi].
    Keys without a RANGES entry pass through unchanged (their targets can
    never be auto-applied). Ints stay ints; floats are tidied."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return value
    lo, hi, _step = RANGES.get(key, (None, None, None))
    if lo is not None:
        v = max(float(lo), min(float(hi), v))
    if TYPES.get(key, "int") == "int":
        return int(round(v))
    return round(v, 4)


def setting_target(key, current=None, suggested=None, reason=""):
    """Build one setting-target dict for a recommendation. lo/hi/step come
    from RANGES; suggested is clamped; label/tab/section/control come from
    the registry."""
    entry = SETTING_REGISTRY.get(key, {})
    lo, hi, step = RANGES.get(key, (None, None, None))
    if current is None:
        current = DEFAULTS.get(key)
    if suggested is not None:
        suggested = clamp_suggestion(key, suggested)
    delta = None
    if isinstance(current, (int, float)) and isinstance(suggested,
                                                        (int, float)):
        delta = round(suggested - current, 4)
        if TYPES.get(key, "int") == "int":
            delta = int(delta)
    return {
        "key": key,
        "label": entry.get("label", key),
        "tab": entry.get("tab", ""),
        "section": entry.get("section", ""),
        "control": entry.get("control", "tab"),
        "current": current,
        "suggested": suggested,
        "delta": delta,
        "units": entry.get("units", ""),
        "lo": lo, "hi": hi, "step": step,
        "reason": reason,
    }


def make_recommendation(rec_id, title, explanation, setting_targets=None,
                        calibration_targets=None, permission_targets=None,
                        expected_effect="", tradeoff="", verify="",
                        priority=1, auto_apply=False, **extra):
    """DiagnosticRecommendation dict. auto_apply is forced False unless
    every setting target carries a bounded (RANGES-backed) suggested
    value -- an Open-setting-only recommendation is never one-click."""
    st = list(setting_targets or [])
    if auto_apply:
        for t in st:
            if t.get("key") not in RANGES or t.get("suggested") is None:
                auto_apply = False
                break
        if not st:
            auto_apply = False
    rec = {
        "id": rec_id,
        "title": title,
        "explanation": explanation,
        "setting_targets": st,
        "calibration_targets": list(calibration_targets or []),
        "permission_targets": list(permission_targets or []),
        "expected_effect": expected_effect,
        "tradeoff": tradeoff,
        "verify": verify,
        "priority": int(priority),
        "auto_apply": bool(auto_apply),
    }
    rec.update(extra)
    return rec


def _links_from_recs(recommendations):
    """Derive deep_links from a recommendation list: one link per distinct
    setting / calibration item / permission. Each carries tab_target (the
    D2 host resolves it to a real tab click)."""
    links, seen = [], set()
    for rec in recommendations or []:
        for t in rec.get("setting_targets", []):
            sig = ("setting", t.get("key"))
            if sig in seen:
                continue
            seen.add(sig)
            links.append({"kind": "setting", "key": t.get("key"),
                          "control": t.get("control", "tab"),
                          "tab_target": t.get("tab", "")})
        for c in rec.get("calibration_targets", []):
            sig = ("calibration", c)
            if sig in seen:
                continue
            seen.add(sig)
            links.append({"kind": "calibration", "item": c,
                          "tab_target": "cal"})
        for p in rec.get("permission_targets", []):
            sig = ("permission", p)
            if sig in seen:
                continue
            seen.add(sig)
            links.append({"kind": "permission", "capability": p,
                          "tab_target": "trust"})
    return links


def make_event(code, severity, category, title, summary, observed,
               evidence, confidence, recommendations, other_causes,
               faq_id, deep_links=None, dismissible=True, suppressible=True,
               source="stats", context=None, priority=100):
    """DiagnosticEvent dict. id is stable per code+context. deep_links
    default to links derived from the recommendations. Recurrence and
    first_seen/last_seen are added by the host store (D2), not here."""
    if severity not in SEVERITIES:
        severity = "NOTICE"
    if confidence not in CONFIDENCES:
        confidence = "possible"
    recs = list(recommendations or [])
    return {
        "id": code if not context else "%s@%s" % (code, context),
        "code": code,
        "severity": severity,
        "category": category,
        "title": title,
        "summary": summary,
        "observed": observed,
        "evidence": list(evidence or []),
        "confidence": confidence,
        "recommendations": recs,
        "other_causes": list(other_causes or []),
        "faq_id": faq_id,
        "deep_links": (list(deep_links) if deep_links is not None
                       else _links_from_recs(recs)),
        "dismissible": bool(dismissible),
        "suppressible": bool(suppressible),
        "source": source,
        "priority": int(priority),
    }


def sort_events(events):
    """Severity desc, confidence desc, priority asc (stable)."""
    return sorted(events or [],
                  key=lambda e: (-_SEV_RANK.get(e.get("severity"), 0),
                                 -_CONF_RANK.get(e.get("confidence"), 0),
                                 e.get("priority", 100)))


# --------------------------------------------------------------------------
# ctx accessors -- every rule reads through these so a missing / None /
# odd-typed ctx key can never raise.
# --------------------------------------------------------------------------

def _dictget(ctx, key):
    v = ctx.get(key) if isinstance(ctx, dict) else None
    return v if isinstance(v, dict) else {}


def _num(d, key):
    try:
        return float(d.get(key, 0) or 0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _stats(ctx):
    return _dictget(ctx, "stats")


def _cal_state(ctx, item):
    """('status', 'detail') for a calibration item; tolerant of the host
    passing either {'status':..,'detail':..} dicts or bare strings."""
    st = _dictget(ctx, "cal_status").get(item)
    if isinstance(st, dict):
        return str(st.get("status", "")), str(st.get("detail", ""))
    if isinstance(st, str):
        return st, ""
    return "", ""


def _cal_health(ctx):
    h = _dictget(ctx, "cal_health")
    return bool(h.get("ok", True)), str(h.get("reason", ""))


def _setting(ctx, key):
    settings = _dictget(ctx, "settings")
    if key in settings:
        return settings.get(key)
    return DEFAULTS.get(key)


def _num_setting(ctx, key):
    v = _setting(ctx, key)
    try:
        return float(v)
    except (TypeError, ValueError):
        try:
            return float(DEFAULTS.get(key, 0))
        except (TypeError, ValueError):
            return 0.0


def _step(key, fallback=1):
    s = RANGES.get(key, (None, None, None))[2]
    return s if s is not None else fallback


def confidence_from_ratio(ratio):
    """Shared do-not-overstate policy: right at the threshold is only a
    possibility; comfortably over is medium; double or more is high."""
    if ratio >= 2.0:
        return "high"
    if ratio >= 1.34:
        return "medium"
    return "possible"


# --------------------------------------------------------------------------
# D1.3 Rules
# --------------------------------------------------------------------------

def _rule_nudge_far(ctx):
    """A -- PP-D-NUDGE-FAR: corrective nudges >= 0.6/cycle over >= 5
    cycles. Movement continues past the expected stop point."""
    st = _stats(ctx)
    cycles = _num(st, "cycles")
    nudges = _num(st, "nudges")
    if cycles < 5 or nudges / cycles < 0.6:
        return []
    rate = nudges / cycles
    conf = "high" if rate >= 0.8 else "medium"
    cur_back = _num_setting(ctx, "WATER_EXTRA_BACK_MS")
    cur_delay = _num_setting(ctx, "SHAKE_START_DELAY_MS")
    cur_settle = _num_setting(ctx, "LAND_SETTLE_MS")
    recs = [
        make_recommendation(
            "nudge-far-reduce-extra-walk-back",
            "Reduce Extra walk back",
            "Movement continued past the expected stop point in %d of %d "
            "cycles; reduce Extra walk back first -- extra S held after "
            "the Pan cue is the usual overshoot."
            % (int(nudges), int(cycles)),
            setting_targets=[setting_target(
                "WATER_EXTRA_BACK_MS", current=cur_back,
                suggested=cur_back - _step("WATER_EXTRA_BACK_MS", 80),
                reason="one step less extra walk back")],
            expected_effect="Fewer corrective nudges per cycle; the "
                            "walk-back stops at the Pan cue instead of "
                            "past it.",
            tradeoff="Too little extra walk back can leave the character "
                     "short of the deeper water some spots need.",
            verify="Run at least 5 cycles and check the nudge rate falls "
                   "below 0.6 per cycle.",
            priority=1, auto_apply=True),
        make_recommendation(
            "nudge-far-delay-shake-start",
            "Increase Delay before shake starts",
            "If Shake begins while movement is still settling, increase "
            "Delay before shake starts so momentum has fully stopped "
            "before the shake.",
            setting_targets=[setting_target(
                "SHAKE_START_DELAY_MS", current=cur_delay,
                suggested=cur_delay + _step("SHAKE_START_DELAY_MS", 50),
                reason="one step more settle before the shake")],
            expected_effect="The shake starts from a standstill, so the "
                            "engine stops correcting overshoot afterward.",
            tradeoff="Each cycle takes slightly longer.",
            verify="Watch one cycle: the character should be stationary "
                   "when the shake begins.",
            priority=2, auto_apply=True),
        make_recommendation(
            "nudge-far-settle-on-land",
            "Hold W after the land cue slightly longer",
            "If the nudges happen on the land side, a longer settle after "
            "the land cue keeps the character firmly on the dirt before "
            "digging.",
            setting_targets=[setting_target(
                "LAND_SETTLE_MS", current=cur_settle,
                suggested=cur_settle + _step("LAND_SETTLE_MS", 25),
                reason="one step more settle on land")],
            expected_effect="Less slide-back at the dig spot.",
            tradeoff="A little extra time on land each cycle.",
            verify="Run 5 cycles and compare the nudge count.",
            priority=3, auto_apply=True),
    ]
    return [make_event(
        "PP-D-NUDGE-FAR", "WARNING", "movement",
        "Movement overshoots the stop point",
        "Movement continued past the expected stop point in %d of %d "
        "cycles. Reduce Extra walk back first; if Shake begins while "
        "movement is still settling, increase Delay before shake starts."
        % (int(nudges), int(cycles)),
        "The engine issued %d corrective nudges over %d cycles "
        "(%.2f per cycle; the warning threshold is 0.6)."
        % (int(nudges), int(cycles), rate),
        ["nudges: %d in %d cycles (%.2f/cycle, threshold 0.6)"
         % (int(nudges), int(cycles), rate)],
        conf, recs,
        ["A lag spike can carry movement past the stop point even with "
         "correct settings.",
         "A stale Pan prompt calibration makes the water anchor "
         "unreliable -- re-check it on the Calibrate tab."],
        "faq-movement-nudges", source="stats")]


def _rule_shake_early(ctx):
    """B -- PP-D-SHAKE-EARLY: shake_start_retry >= 3 or shake_glitch >= 2
    this run -- the shake is clicked before the game is ready."""
    ec = _dictget(ctx, "event_counts")
    retries = _num(ec, "shake_start_retry")
    glitches = _num(ec, "shake_glitch")
    if retries < 3 and glitches < 2:
        return []
    ratio = max(retries / 3.0, glitches / 2.0)
    conf = confidence_from_ratio(ratio)
    evidence = []
    if retries:
        evidence.append("shake_start_retry: %d this run (threshold 3)"
                        % int(retries))
    if glitches:
        evidence.append("shake_glitch: %d this run (threshold 2)"
                        % int(glitches))
    cur_delay = _num_setting(ctx, "SHAKE_START_DELAY_MS")
    cur_confirm = _num_setting(ctx, "SHAKE_START_CONFIRM_MS")
    cur_easy = _num_setting(ctx, "EASY_SHAKE_DELAY_MS")
    recs = [
        make_recommendation(
            "shake-early-delay",
            "Increase Delay before shake starts",
            "Repeated shake-start retries mean the shake click landed "
            "before the game was ready; one more step of delay lets the "
            "walk-back finish first.",
            setting_targets=[setting_target(
                "SHAKE_START_DELAY_MS", current=cur_delay,
                suggested=cur_delay + _step("SHAKE_START_DELAY_MS", 50),
                reason="give the game time before the first shake click")],
            expected_effect="Fewer shake_start_retry events.",
            tradeoff="Slightly longer cycles.",
            verify="Run a few cycles; the retry count on the Run tab "
                   "should stop climbing.",
            priority=1, auto_apply=True),
        make_recommendation(
            "shake-early-confirm",
            "Confirm the shake started",
            "With the start confirmation on, a shake that did not begin "
            "is retried within this window instead of wasting the pan.",
            setting_targets=[setting_target(
                "SHAKE_START_CONFIRM_MS", current=cur_confirm,
                suggested=cur_confirm + _step("SHAKE_START_CONFIRM_MS", 50),
                reason="enable / widen the fast start check")],
            expected_effect="A missed start is caught and retried fast.",
            tradeoff="Too small a window can retry a shake that was "
                     "about to start.",
            verify="Watch a cycle where the shake misses: it should "
                   "retry within the confirm window.",
            priority=2, auto_apply=True),
        make_recommendation(
            "shake-early-easy-delay",
            "Use the Easy shake delay",
            "The plain-language layer adds onto the Shake delay at bind "
            "time -- the simplest one-knob fix.",
            setting_targets=[setting_target(
                "EASY_SHAKE_DELAY_MS", current=cur_easy,
                suggested=cur_easy + _step("EASY_SHAKE_DELAY_MS", 60),
                reason="one easy-tuning bump")],
            expected_effect="Same settling effect via Easy tuning.",
            tradeoff="Slightly longer cycles.",
            verify="Run a few cycles and compare retry counts.",
            priority=3, auto_apply=True),
    ]
    title = ("Shake may be starting too early"
             if conf == "possible" else "Shake starts too early")
    return [make_event(
        "PP-D-SHAKE-EARLY", "WARNING", "shake", title,
        "The shake click is landing before the game is ready to shake, "
        "so the engine retries with deeper taps.",
        "This run recorded %d shake-start retries and %d shake glitches."
        % (int(retries), int(glitches)),
        evidence, conf, recs,
        ["Frame drops in Roblox delay the shake prompt itself.",
         "A stale Shake prompt calibration can misread the start."],
        "faq-shake-timing", source="events")]


def _rule_shake_late(ctx):
    """C -- PP-D-SHAKE-LATE: shake_misses/cycles >= 0.4 over >= 5
    cycles -- shakes are being marked missed/failed."""
    st = _stats(ctx)
    cycles = _num(st, "cycles")
    misses = _num(st, "shake_misses")
    if cycles < 5 or misses / cycles < 0.4:
        return []
    rate = misses / cycles
    conf = confidence_from_ratio(rate / 0.4)
    cur_delay = _num_setting(ctx, "SHAKE_START_DELAY_MS")
    cur_bail = _num_setting(ctx, "SHAKE_BAIL_MS")
    recs = []
    if cur_delay > 0:
        recs.append(make_recommendation(
            "shake-late-reduce-delay",
            "Reduce Delay before shake starts",
            "The delay before the shake is currently %d ms; reducing it "
            "starts the shake sooner so it is not marked missed."
            % int(cur_delay),
            setting_targets=[setting_target(
                "SHAKE_START_DELAY_MS", current=cur_delay,
                suggested=cur_delay - _step("SHAKE_START_DELAY_MS", 50),
                reason="start the shake one step sooner")],
            expected_effect="The shake begins inside the detection "
                            "window.",
            tradeoff="Too little delay risks shaking while movement is "
                     "still settling (the opposite problem).",
            verify="Run 5+ cycles; the shake-miss rate should fall "
                   "below 0.4 per cycle.",
            priority=1, auto_apply=True))
    recs.append(make_recommendation(
        "shake-late-bail-window",
        "Give shake-failed detection more time",
        "A slow-starting shake gets marked failed when the pan is still "
        "full at the bail deadline; one step more time tolerates the "
        "late start.",
        setting_targets=[setting_target(
            "SHAKE_BAIL_MS", current=cur_bail,
            suggested=cur_bail + _step("SHAKE_BAIL_MS", 150),
            reason="tolerate a late-starting shake")],
        expected_effect="Fewer false shake-failed verdicts.",
        tradeoff="Genuinely failed shakes waste a little more time "
                 "before the retry.",
        verify="Run 5+ cycles and compare the shake-miss rate.",
        priority=2, auto_apply=True))
    recs.append(make_recommendation(
        "shake-late-cue-masks",
        "Review the Shake cue capture",
        "If the Shake prompt mask no longer matches the screen, real "
        "shakes read as missed. Re-check the Advanced cue captures.",
        calibration_targets=["cue_masks"],
        expected_effect="The shake prompt is detected reliably again.",
        tradeoff="A minute of re-capture.",
        verify="Use the cue check on the Calibrate tab: the Shake mask "
               "should clear the match threshold.",
        priority=3))
    return [make_event(
        "PP-D-SHAKE-LATE", "WARNING", "shake",
        ("Shakes may be registering late"
         if conf == "possible" else "Shakes register late or not at all"),
        "A large share of shakes is being marked missed -- the shake "
        "starts (or is detected) too late.",
        "This run recorded %d shake misses over %d cycles (%.2f per "
        "cycle; the warning threshold is 0.4)."
        % (int(misses), int(cycles), rate),
        ["shake_misses: %d in %d cycles (%.2f/cycle, threshold 0.4)"
         % (int(misses), int(cycles), rate)],
        conf, recs,
        ["Roblox frame drops can delay the shake past the detection "
         "window.",
         "A moved or resized window makes the Shake prompt calibration "
         "stale."],
        "faq-shake-timing", source="stats")]


def _cue_miss_rule(code, item, cue_word):
    """Shared shape for rules D/E/F -- a prompt cue is being missed while
    the run visibly stalls (no_progress events exist)."""
    def rule(ctx):
        stalls = int(_num(_dictget(ctx, "event_counts"), "no_progress"))
        if stalls < 1:
            return []
        item_status, item_detail = _cal_state(ctx, item)
        masks_status, _md = _cal_state(ctx, "cue_masks")
        health_ok, health_reason = _cal_health(ctx)
        cal_bad = (item_status in _STALE_STATUSES
                   or masks_status in _STALE_STATUSES)
        if not cal_bad and health_ok:
            return []
        severity = escalate(code, stalls) or "NOTICE"
        if cal_bad and not health_ok:
            conf = "high"
        elif cal_bad:
            conf = "medium" if stalls >= 3 else "possible"
        else:
            conf = "possible"
        evidence = ["no_progress: %d this run (threshold 1)" % stalls]
        if item_status:
            evidence.append("%s status: %s" % (item, item_status))
        if masks_status:
            evidence.append("cue_masks status: %s" % masks_status)
        summary = ("The run stalled while the '%s' prompt detection "
                   "looks unreliable -- its calibration needs another "
                   "look." % cue_word)
        low = health_reason.lower()
        if not health_ok and ("window" in low or "resiz" in low
                              or "scale" in low):
            summary += (" The window or UI scale changed since "
                        "calibration: %s" % health_reason)
        recs = [make_recommendation(
            "%s-recapture" % item,
            "Re-capture the '%s' prompt" % cue_word,
            "The mask capture is the primary detector and the prompt "
            "pixel is its fallback; re-capturing both restores reliable "
            "detection.",
            calibration_targets=["cue_masks", item],
            expected_effect="The '%s' prompt is detected again and the "
                            "stalls stop." % cue_word,
            tradeoff="A minute on the Calibrate tab.",
            verify="Run the cue check on the Calibrate tab, then a "
                   "short run: no_progress events should stop.",
            priority=1)]
        return [make_event(
            code, severity, "calibration",
            "The '%s' prompt may be getting missed" % cue_word,
            summary,
            "%d no-progress stall(s) this run while %s status is '%s' "
            "and cue_masks status is '%s'."
            % (stalls, item, item_status or "unknown",
               masks_status or "unknown"),
            evidence, conf, recs,
            ["The stall may have a different cause entirely -- a "
             "blocked path or an in-game popup.",
             "Very heavy lag can stall progress with calibration "
             "perfectly fine."],
            "faq-missed-prompts", source="calibration")]
    return rule


_rule_cue_pan_miss = _cue_miss_rule("PP-D-CUE-PAN-MISS", "pan_prompt", "Pan")
_rule_cue_deposit_miss = _cue_miss_rule("PP-D-CUE-DEPOSIT-MISS",
                                        "deposit_prompt", "Collect Deposit")
_rule_cue_shake_miss = _cue_miss_rule("PP-D-CUE-SHAKE-MISS",
                                      "shake_prompt", "Shake")


def _rule_autopan_stuck(ctx):
    """G -- PP-D-AUTOPAN-STUCK: autopan_kick >= 2 or autopan_guard >= 3
    -- the Auto Pan button needs repeated kicks/guard re-enables."""
    ec = _dictget(ctx, "event_counts")
    kicks = _num(ec, "autopan_kick")
    guards = _num(ec, "autopan_guard")
    if kicks < 2 and guards < 3:
        return []
    conf = confidence_from_ratio(max(kicks / 2.0, guards / 3.0))
    evidence = []
    if kicks:
        evidence.append("autopan_kick: %d this run (threshold 2)"
                        % int(kicks))
    if guards:
        evidence.append("autopan_guard: %d this run (threshold 3)"
                        % int(guards))
    cur_settle = _num_setting(ctx, "AUTOPAN_SETTLE_MS")
    cur_tol = _num_setting(ctx, "AUTOPAN_TOL")
    cur_stall = _num_setting(ctx, "AUTOPAN_STALL_SEC")
    recs = [
        make_recommendation(
            "autopan-settle",
            "Wait longer after each Auto Pan click",
            "If the button colour is re-read before the game reacts, a "
            "successful click looks failed and gets clicked again.",
            setting_targets=[setting_target(
                "AUTOPAN_SETTLE_MS", current=cur_settle,
                suggested=cur_settle + _step("AUTOPAN_SETTLE_MS", 50),
                reason="give the click time to register")],
            expected_effect="Fewer redundant Auto Pan clicks.",
            tradeoff="Slightly slower Auto Pan interactions.",
            verify="Watch the tracker: guard/kick events should stop "
                   "repeating.",
            priority=1, auto_apply=True),
        make_recommendation(
            "autopan-tolerance",
            "Loosen the button colour tolerance",
            "Lighting shifts move the button colour; a wider tolerance "
            "keeps the ON/OFF read stable.",
            setting_targets=[setting_target(
                "AUTOPAN_TOL", current=cur_tol,
                suggested=cur_tol + _step("AUTOPAN_TOL", 5),
                reason="tolerate colour drift")],
            expected_effect="The button state reads correctly despite "
                            "lighting drift.",
            tradeoff="Too loose can confuse ON with OFF.",
            verify="Toggle Auto Pan in game and watch the tracker read "
                   "follow it.",
            priority=2, auto_apply=True),
    ]
    if cur_stall == 0:
        recs.append(make_recommendation(
            "autopan-idle-kick",
            "Enable the idle kick",
            "Auto Pan sometimes wedges while its button still shows "
            "green; the idle kick restarts it after a quiet spell.",
            setting_targets=[setting_target(
                "AUTOPAN_STALL_SEC", current=cur_stall, suggested=5,
                reason="turn the idle kick on (5 s)")],
            expected_effect="A wedged Auto Pan recovers on its own.",
            tradeoff="A kick during a legitimate pause re-clicks the "
                     "button unnecessarily.",
            verify="Let the tracker idle: a stall should self-recover "
                   "within the kick window.",
            priority=3, auto_apply=True))
    ap_status, _d = _cal_state(ctx, "autopan_button")
    if ap_status not in ("ok", "auto"):
        recs.append(make_recommendation(
            "autopan-calibrate-button",
            "Re-calibrate the Auto Pan button pixel",
            "The Auto Pan button pixel status is '%s' -- the tracker "
            "may be reading the wrong spot entirely."
            % (ap_status or "unknown"),
            calibration_targets=["autopan_button"],
            expected_effect="The ON/OFF state is read from the right "
                            "pixel again.",
            tradeoff="A quick recalibration on the Calibrate tab.",
            verify="Toggle Auto Pan in game and confirm the tracker "
                   "follows.",
            priority=4))
    return [make_event(
        "PP-D-AUTOPAN-STUCK", "WARNING", "tracking",
        ("Auto Pan may be sticking" if conf == "possible"
         else "Auto Pan keeps needing kicks"),
        "The Auto Pan button needed repeated guard re-enables or idle "
        "kicks this run.",
        "This run recorded %d idle kicks and %d guard re-enables."
        % (int(kicks), int(guards)),
        evidence, conf, recs,
        ["The game genuinely turning Auto Pan off (e.g. after a "
         "teleport) also produces guard events.",
         "A moved window can put the button pixel on the wrong spot."],
        "faq-autopan-stuck", source="events")]


def _rule_capacity(ctx):
    """H -- PP-D-CAP-SUSPECT (stored capacity pair failed today's
    validation) and PP-D-CAP-HARDSTOP (hard stops happened). The codes
    are the contract pinned in lite_onboarding.CALIBRATION_ITEMS
    cap_bar.related_diagnostics."""
    out = []
    cap_status, cap_detail = _cal_state(ctx, "cap_bar")
    if cap_status == "needs_review":
        out.append(make_event(
            "PP-D-CAP-SUSPECT", "ERROR", "capacity",
            "The saved capacity-bar endpoints look wrong",
            "The stored capacity pair could not pass today's validation; "
            "the runtime may be reading the wrong span of the bar.",
            cap_detail or ("cap_bar status is 'needs_review' -- the "
                           "stored endpoint pair failed validation."),
            ["cap_bar status: needs_review"],
            "high",
            [make_recommendation(
                "cap-suspect-test-and-redo",
                "Test capacity, then redo the Capacity step if it fails",
                "Run Test capacity calibration first -- it verifies the "
                "live reading; if it fails, redo the right + left tip "
                "capture.",
                calibration_targets=["cap_bar"],
                expected_effect="The pan full/empty reads become "
                                "trustworthy again.",
                tradeoff="A minute on the Calibrate tab.",
                verify="Test capacity reports a valid live reading.",
                priority=1, repair_action="test_capacity")],
            ["An imported or hand-edited config can carry endpoints "
             "from another machine or resolution."],
            "faq-capacity-calibration", source="calibration"))
    hard_stops = int(_num(_stats(ctx), "hard_stops"))
    if hard_stops >= 1:
        out.append(make_event(
            "PP-D-CAP-HARDSTOP", "ERROR", "capacity",
            "Hard stops happened",
            "The engine hard-stopped; a mis-set capacity bar RIGHT end "
            "is the most common cause (the bar never reads empty/full "
            "correctly).",
            "This run recorded %d hard stop(s)." % hard_stops,
            ["hard_stops: %d this run (threshold 1)" % hard_stops],
            "high",
            [make_recommendation(
                "cap-hardstop-recheck",
                "Re-check the capacity bar calibration",
                "Run Test capacity calibration; if the live reading is "
                "wrong, redo the RIGHT tip then LEFT tip capture.",
                calibration_targets=["cap_bar"],
                expected_effect="Hard stops from misread capacity stop.",
                tradeoff="A minute on the Calibrate tab.",
                verify="Test capacity passes and a short run finishes "
                       "without a hard stop.",
                priority=1, repair_action="test_capacity")],
            ["Safe-stop retries exhausting (SAFE_STOP_MAX_RETRIES) also "
             "ends in a hard stop -- check the run log's stop reason."],
            "faq-capacity-calibration", source="stats"))
    return out


def _rule_recovery_loop(ctx):
    """I -- PP-D-RECOVERY-LOOP: recoveries/cycles >= 0.5 (cycles >= 5)
    or recovery_rung >= 3 -- recovery is firing constantly."""
    st = _stats(ctx)
    cycles = _num(st, "cycles")
    recoveries = _num(st, "recoveries")
    rungs = _num(_dictget(ctx, "event_counts"), "recovery_rung")
    rate = recoveries / cycles if cycles >= 5 else 0.0
    if rate < 0.5 and rungs < 3:
        return []
    conf = confidence_from_ratio(max(rate / 0.5 if rate else 0.0,
                                     rungs / 3.0))
    evidence = []
    if cycles >= 5:
        evidence.append("recoveries: %d in %d cycles (%.2f/cycle, "
                        "threshold 0.5)"
                        % (int(recoveries), int(cycles), rate))
    if rungs:
        evidence.append("recovery_rung: %d this run (threshold 3)"
                        % int(rungs))
    cur_limit = _num_setting(ctx, "RECOVER_LIMIT")
    cur_watchdog = _num_setting(ctx, "NO_PROGRESS_SEC")
    cur_back = _num_setting(ctx, "RECOVER_BACK_MS")
    recs = [
        make_recommendation(
            "recovery-loop-limit",
            "Escalate to a break-out sooner",
            "Fewer gentle recoveries on the same spot before break-out "
            "stops the nudge-fail-nudge loop.",
            setting_targets=[setting_target(
                "RECOVER_LIMIT", current=cur_limit,
                suggested=cur_limit - _step("RECOVER_LIMIT", 1),
                reason="break out of the loop one recovery earlier")],
            expected_effect="Loops end in a decisive break-out instead "
                            "of repeating.",
            tradeoff="Occasionally breaks out when one more nudge would "
                     "have worked.",
            verify="Run 5+ cycles: the recovery rate should drop below "
                   "0.5 per cycle.",
            priority=1, auto_apply=True),
        make_recommendation(
            "recovery-loop-watchdog",
            "Give slow cycles more time before the watchdog",
            "If the no-progress watchdog is set very tight, normal slow "
            "cycles get treated as stuck and recovered needlessly.",
            setting_targets=[setting_target(
                "NO_PROGRESS_SEC", current=cur_watchdog,
                suggested=cur_watchdog + _step("NO_PROGRESS_SEC", 2),
                reason="stop recovering healthy-but-slow cycles")],
            expected_effect="Recovery fires only on real stalls.",
            tradeoff="Real stalls run a couple of seconds longer before "
                     "recovery.",
            verify="Run 5+ cycles and compare the recovery count.",
            priority=2, auto_apply=True),
        make_recommendation(
            "recovery-loop-nudge-budget",
            "Give each recovery nudge more movement",
            "If each nudge is too small to free the character, the same "
            "recovery repeats; a bigger budget frees the wedge in one "
            "go.",
            setting_targets=[setting_target(
                "RECOVER_BACK_MS", current=cur_back,
                suggested=cur_back + _step("RECOVER_BACK_MS", 40),
                reason="one bigger, effective nudge")],
            expected_effect="One recovery succeeds instead of several "
                            "failing.",
            tradeoff="Harder nudges overshoot when the spot was nearly "
                     "right.",
            verify="Watch one recovery: the character should visibly "
                   "clear the stuck spot.",
            priority=3, auto_apply=True),
    ]
    return [make_event(
        "PP-D-RECOVERY-LOOP", "WARNING", "recovery",
        ("Recovery may be looping" if conf == "possible"
         else "Recovery keeps firing"),
        "Stuck-recovery is running far more often than a healthy cycle "
        "needs.",
        "This run recorded %d recoveries over %d cycles and %d "
        "recovery-ladder rungs."
        % (int(recoveries), int(cycles), int(rungs)),
        evidence, conf, recs,
        ["The walk-back duration may be wrong for this spot (Max S "
         "walk-back / Extra walk back), parking the character short.",
         "Water misdetection -- a stale Pan prompt makes the engine "
         "think it never arrived."],
        "faq-recovery-loops", source="stats")]


def _rule_stall(ctx):
    """J -- PP-D-STALL: recent no_progress telemetry while a run is
    active. Cause priority: missing REQUIRED permission > Roblox window
    lost > stale calibration."""
    if not ctx.get("run_active"):
        return []
    recent = ctx.get("recent_events")
    if not isinstance(recent, (list, tuple)):
        recent = []
    stalls = [e for e in recent
              if isinstance(e, dict) and e.get("type") == "no_progress"]
    if not stalls:
        return []
    n = len(stalls)
    severity = escalate("PP-D-STALL", n) or "WARNING"
    caps = _dictget(ctx, "capabilities")
    missing = [c for c in REQUIRED_CAPABILITIES
               if str(caps.get(c, "")) == "not_granted"]
    evidence = ["no_progress: %d in recent telemetry (threshold 1)" % n]
    if missing:
        cap = missing[0]
        faq = {"screen_detection": "faq-mac-screen-recording",
               "input_control": "faq-mac-input-monitoring",
               "stop_hotkeys": "faq-safe-stop-hotkeys"}[cap]
        evidence.append("capability %s: not_granted" % cap)
        recs = [make_recommendation(
            "stall-grant-permission",
            "Grant %s" % _CAPABILITY_TITLES.get(cap, cap),
            "A required capability is not granted, so the engine cannot "
            "%s -- the run stalls without it."
            % ("see the screen" if cap == "screen_detection"
               else "press keys or the mouse" if cap == "input_control"
               else "hear the stop hotkeys"),
            permission_targets=[cap],
            expected_effect="The engine regains its required capability "
                            "and progress resumes.",
            tradeoff="",
            verify="Run the capability Test in the Trust Center, then "
                   "start a short run.",
            priority=1)]
        return [make_event(
            "PP-D-STALL", severity, "stall",
            "The run is stalling: a required permission is missing",
            "No-progress stalls are happening and %s is not granted -- "
            "that permission is the primary cause."
            % _CAPABILITY_TITLES.get(cap, cap),
            "%d no-progress event(s) in recent telemetry while "
            "capability '%s' reads not_granted." % (n, cap),
            evidence, "high", recs,
            ["An in-game obstruction can stall progress independently."],
            faq, source="events")]
    if ctx.get("window_found") is False:
        evidence.append("window_found: False")
        recs = [make_recommendation(
            "stall-find-window",
            "Bring the Roblox window back",
            "The Roblox window cannot be found on the primary display; "
            "detection has nothing to read, so the run stalls.",
            calibration_targets=["roblox_window"],
            expected_effect="Detection sees the game again.",
            tradeoff="",
            verify="Press Detect on the Calibrate tab -- it should "
                   "report the window size and position.",
            priority=1)]
        return [make_event(
            "PP-D-STALL", severity, "stall",
            "The run is stalling: the Roblox window was lost",
            "No-progress stalls are happening and the Roblox window is "
            "not found -- restore it on the primary display.",
            "%d no-progress event(s) in recent telemetry with "
            "window_found False." % n,
            evidence, "high", recs,
            ["The window may be minimised or on a secondary display."],
            "faq-window-detection", source="events")]
    stale = sorted(k for k in _dictget(ctx, "cal_status")
                   if _cal_state(ctx, k)[0] in _STALE_STATUSES)
    targets = stale or ["cue_masks"]
    evidence += ["%s status: %s" % (k, _cal_state(ctx, k)[0])
                 for k in stale]
    recs = [make_recommendation(
        "stall-recalibrate",
        "Re-check the stale calibration",
        ("Calibration item(s) %s read stale -- the detectors may be "
         "looking at the wrong spots." % ", ".join(targets))
        if stale else
        "No permission or window problem was found; re-checking the "
        "cue captures is the next most likely fix.",
        calibration_targets=targets,
        expected_effect="Detection lines back up with the screen.",
        tradeoff="A few minutes on the Calibrate tab.",
        verify="Re-run a short cycle: no_progress events should stop.",
        priority=1)]
    return [make_event(
        "PP-D-STALL", severity, "stall",
        "The run is stalling",
        "No-progress stalls are happening; permissions and the window "
        "look fine, so stale calibration is the most likely cause.",
        "%d no-progress event(s) in recent telemetry; %d calibration "
        "item(s) read stale." % (n, len(stale)),
        evidence, "medium" if stale else "possible", recs,
        ["An in-game obstruction or popup can stall progress with "
         "calibration perfectly fine."],
        "faq-roblox-window-changes", source="events")]


def _rule_finds_miss(ctx):
    """K -- PP-D-FINDS-MISS: finds tracking is on and either the finds
    box is not calibrated/stale or ghost+fork churn is high."""
    if not _setting(ctx, "FINDS_TRACK"):
        return []
    region_status, _d = _cal_state(ctx, "find_region")
    region_bad = region_status not in ("ok", "auto")
    ec = _dictget(ctx, "event_counts")
    ghosts = _num(ec, "finds_ghost")
    forks = _num(ec, "finds_fork")
    churn = ghosts + forks
    if not region_bad and churn < 3:
        return []
    evidence = []
    if region_bad:
        evidence.append("find_region status: %s"
                        % (region_status or "unset"))
    if churn:
        evidence.append("finds_ghost+finds_fork: %d this run "
                        "(threshold 3)" % int(churn))
    if region_bad and churn >= 3:
        conf = "high"
    elif region_bad:
        conf = "medium"
    else:
        conf = confidence_from_ratio(churn / 3.0)
    severity = "WARNING" if churn >= 3 else "NOTICE"
    cur_conf = _num_setting(ctx, "FINDS_MIN_CONF")
    cur_card = _num_setting(ctx, "FINDS_CARD_SEC")
    cur_empty = _num_setting(ctx, "FINDS_EMPTY_MS")
    recs = []
    prio = 1
    if region_bad:
        recs.append(make_recommendation(
            "finds-miss-region",
            "Re-draw the finds pop-up box",
            "The finds box status is '%s'; the OCR is reading the wrong "
            "part of the screen." % (region_status or "unset"),
            calibration_targets=["find_region"],
            expected_effect="Find cards are read where they actually "
                            "appear.",
            tradeoff="A quick drag on the Calibrate tab.",
            verify="Trigger one find and watch it appear in the log.",
            priority=prio))
        prio += 1
    recs += [
        make_recommendation(
            "finds-miss-min-conf",
            "Lower the OCR confidence gate",
            "Faint or animated cards fall under the current gate and "
            "never start a find.",
            setting_targets=[setting_target(
                "FINDS_MIN_CONF", current=cur_conf,
                suggested=cur_conf - _step("FINDS_MIN_CONF", 0.05),
                reason="admit slightly fainter reads")],
            expected_effect="More real cards clear the gate.",
            tradeoff="More OCR misreads can slip through as finds.",
            verify="Trigger a few finds and compare the log to what "
                   "you saw.",
            priority=prio, auto_apply=True),
        make_recommendation(
            "finds-miss-card-life",
            "Widen the card re-sighting window",
            "A longer card lifetime keeps one animated card from being "
            "split into ghost/fork copies.",
            setting_targets=[setting_target(
                "FINDS_CARD_SEC", current=cur_card,
                suggested=cur_card + _step("FINDS_CARD_SEC", 1),
                reason="one step longer card life")],
            expected_effect="Fewer ghost/fork records.",
            tradeoff="Two identical finds close together may merge.",
            verify="Watch the finds log across a burst of finds.",
            priority=prio + 1, auto_apply=True),
        make_recommendation(
            "finds-miss-quiet-window",
            "Lengthen the stack-reset quiet time",
            "Resetting the stack mid-animation forks cards; a longer "
            "quiet window lets animations finish.",
            setting_targets=[setting_target(
                "FINDS_EMPTY_MS", current=cur_empty,
                suggested=cur_empty + _step("FINDS_EMPTY_MS", 50),
                reason="one step more quiet before reset")],
            expected_effect="Fewer mid-animation resets.",
            tradeoff="Fresh finds after a quiet spell register a touch "
                     "later.",
            verify="Watch the finds log across a burst of finds.",
            priority=prio + 2, auto_apply=True),
    ]
    return [make_event(
        "PP-D-FINDS-MISS", severity, "analytics",
        ("Finds may be getting missed or duplicated"
         if conf == "possible" else "Finds are being missed or "
         "duplicated"),
        "The finds tracker is on but its readings look unreliable.",
        "find_region status is '%s'; %d ghost and %d fork records this "
        "run." % (region_status or "unset", int(ghosts), int(forks)),
        evidence, conf, recs,
        ["Very fast find bursts can outpace the OCR cadence.",
         "A moved window shifts the finds box off the pop-ups."],
        "faq-finds-popup-box", source="events")]


def _rule_analytics_stale(ctx):
    """L -- PP-D-ANALYTICS-STALE: earnings tracking is on but a counter
    region is not calibrated/stale (money or shards)."""
    if not _setting(ctx, "EARN_TRACK"):
        return []
    bad = []
    for item in ("money_region", "shards_region"):
        status, _d = _cal_state(ctx, item)
        if status not in ("ok", "auto"):
            bad.append((item, status or "unset"))
    if not bad:
        return []
    evidence = ["%s status: %s" % (item, status) for item, status in bad]
    names = {"money_region": "money counter box",
             "shards_region": "shards counter box"}
    recs = [make_recommendation(
        "analytics-stale-regions",
        "Re-draw the %s" % " and ".join(names[i] for i, _s in bad),
        "Earnings OCR reads these boxes; while they are missing or "
        "stale the totals cannot update.",
        calibration_targets=[i for i, _s in bad],
        expected_effect="Money/shards totals track again.",
        tradeoff="A quick drag per box on the Calibrate tab.",
        verify="Use the region read test on the Calibrate tab: it "
               "should return the on-screen number.",
        priority=1)]
    cur_ocr = _num_setting(ctx, "EARN_OCR_SEC")
    if cur_ocr > 60:
        recs.append(make_recommendation(
            "analytics-stale-cadence",
            "Read the totals more often",
            "The totals are read every %d s -- earnings count as the "
            "difference between reads, so long gaps make stats look "
            "frozen." % int(cur_ocr),
            setting_targets=[setting_target(
                "EARN_OCR_SEC", current=cur_ocr, suggested=10,
                reason="the schema default cadence")],
            expected_effect="Totals refresh at the default 10 s rhythm.",
            tradeoff="Slightly more OCR work.",
            verify="Watch the Run tab totals update within ~10 s of an "
                   "in-game change.",
            priority=2, auto_apply=True))
    return [make_event(
        "PP-D-ANALYTICS-STALE", "NOTICE", "analytics",
        "Earnings tracking cannot read its counters",
        "Earnings tracking is on, but %s -- the stats will stay empty "
        "or stale until recalibrated."
        % "; ".join("the %s reads '%s'" % (names[i], s) for i, s in bad),
        "EARN_TRACK is on while %s."
        % " and ".join("%s status is '%s'" % (i, s) for i, s in bad),
        evidence, "high", recs,
        ["A moved or resized window shifts the boxes off the HUD "
         "counters."],
        "faq-analytics-regions", source="calibration")]


def _rule_permissions(ctx):
    """M -- PP-D-PERM-<ID>: a launch-blocking capability is definitively
    not granted. stop_hotkeys is owned by rule N (PP-D-SAFESTOP) so the
    safety story is told once, not twice."""
    caps = _dictget(ctx, "capabilities")
    out = []
    for cap in ("screen_detection", "input_control"):
        if str(caps.get(cap, "")) != "not_granted":
            continue
        faq = ("faq-mac-screen-recording" if cap == "screen_detection"
               else "faq-mac-input-monitoring")
        title = _CAPABILITY_TITLES.get(cap, cap)
        out.append(make_event(
            "PP-D-PERM-%s" % cap.upper(), "CRITICAL", "permissions",
            "%s is not granted" % title,
            "This capability is required for the core macro; Start is "
            "blocked until it is granted.",
            "The OS reports capability '%s' as not_granted." % cap,
            ["capability %s: not_granted" % cap],
            "high",
            [make_recommendation(
                "grant-%s" % cap,
                "Grant %s" % title,
                "The read-only preflight says the permission is "
                "definitively missing; grant it from the Trust Center "
                "(Request opens the system prompt) or the System "
                "Settings pane.",
                permission_targets=[cap],
                expected_effect="Start unblocks and the capability "
                                "test passes.",
                tradeoff="",
                verify="Run the capability Test in the Trust Center.",
                priority=1)],
            ["On macOS a freshly granted permission can need a full "
             "app restart to apply."],
            faq, dismissible=False, suppressible=False,
            source="permissions"))
    return out


def _rule_safestop(ctx):
    """N -- PP-D-SAFESTOP: the Safe Stop hotkey capability is missing or
    its session test failed. The panic key must always work."""
    status = str(_dictget(ctx, "capabilities").get("stop_hotkeys", ""))
    if status not in ("not_granted", "test_failed", "failed"):
        return []
    return [make_event(
        "PP-D-SAFESTOP", "ERROR", "permissions",
        "Safe Stop hotkeys are not working",
        "The global stop hotkeys (%s) cannot be heard while Roblox has "
        "focus -- you would have to find the app window to stop a run."
        % "Esc / Ctrl+K",
        "Capability 'stop_hotkeys' reads '%s'." % status,
        ["capability stop_hotkeys: %s" % status],
        "high",
        [make_recommendation(
            "grant-stop-hotkeys",
            "Fix the Safe Stop hotkeys",
            "Grant Input Monitoring (macOS) or re-run the hotkey test; "
            "a failed test can also mean another app owns the chord.",
            permission_targets=["stop_hotkeys"],
            expected_effect="The panic keys work even while Roblox has "
                            "focus.",
            tradeoff="",
            verify="Use the hotkey test in the Trust Center, then press "
                   "the stop chord during a short run.",
            priority=1)],
        ["Another app may already own the configured chord "
         "(PP-HOTKEY-BUSY in the wizard log)."],
        "faq-safe-stop-hotkeys", suppressible=False,
        source="permissions")]


_BUILD_CONFLICTS = {
    "no-studio-build": (
        "NOTICE", "studio",
        "Studio Build mode has no build selected",
        "The mode is STUDIO BUILD but no build is active; pick one on "
        "the Studio tab and Start will work."),
    "no-studio-script": (
        "NOTICE", "script",
        "Studio Script mode has no script selected",
        "The mode is STUDIO SCRIPT but no script is active; pick one on "
        "the Script tab and Start will work."),
    "classic-with-active-build": (
        "WARNING", "studio",
        "A Studio entry is still active in Classic mode",
        "Classic mode must have no active Studio entry -- the app "
        "refuses rather than silently running the wrong program. Switch "
        "to the entry's Studio mode, or clear the active entry on the "
        "Studio tab."),
    "mode-kind-mismatch": (
        "WARNING", "studio",
        "The active Studio entry does not match the selected mode",
        "The active entry's kind (build vs script) does not match the "
        "selected mode; re-pick the entry on its own tab so the mode "
        "and entry agree."),
}


def _rule_build_conflict(ctx):
    """O -- PP-D-BUILD-CONFLICT: launch() returned one of the typed
    Studio invariant refusals."""
    refusal = ctx.get("launch_refusal")
    info = _BUILD_CONFLICTS.get(refusal) if isinstance(refusal, str) \
        else None
    if not info:
        return []
    severity, tab, title, explanation = info
    recs = [make_recommendation(
        "build-conflict-open-tab",
        "Open the %s tab" % tab.capitalize(),
        explanation,
        calibration_targets=[],
        expected_effect="The mode and active entry agree; Start "
                        "launches.",
        tradeoff="",
        verify="Press Start again -- it should launch (or name the "
               "next blocker).",
        priority=1)]
    return [make_event(
        "PP-D-BUILD-CONFLICT", severity, "launch", title,
        explanation,
        "launch() refused with '%s'." % refusal,
        ["launch_refusal: %s" % refusal],
        "high", recs,
        [],
        "faq-importing-builds",
        deep_links=[{"kind": "tab", "tab_target": tab,
                     "label": "Open the %s tab" % tab.capitalize()}],
        source="launch", context=refusal)]


def _rule_cal_stale(ctx):
    """PP-D-CAL-STALE: calibration health says the window changed since
    calibration. Quotes the health reason verbatim."""
    ok, reason = _cal_health(ctx)
    if ok:
        return []
    stale = sorted(k for k in _dictget(ctx, "cal_status")
                   if _cal_state(ctx, k)[0] == "stale")
    return [make_event(
        "PP-D-CAL-STALE", "WARNING", "calibration",
        "Re-calibration needed",
        reason or "The Roblox window no longer matches the calibrated "
                  "size.",
        reason or "calibration health reports not ok",
        (["calibration health: not ok -- %s" % reason]
         if reason else ["calibration health: not ok"])
        + ["%s status: stale" % k for k in stale],
        "high",
        [make_recommendation(
            "cal-stale-recalibrate",
            "Re-run the affected calibration",
            "Calibration is stored relative to the window; once the "
            "window moves or resizes, the saved points no longer line "
            "up with the screen.",
            calibration_targets=stale,
            expected_effect="Detection lines back up with the screen.",
            tradeoff="A few minutes on the Calibrate tab (or restore "
                     "the window to its calibrated size/position).",
            verify="The red calibration banner clears once health "
                   "passes again.",
            priority=1)],
        ["If you restored the window exactly, health recovers on its "
         "own -- no recalibration needed."],
        "faq-roblox-window-changes", source="calibration")]


_RULES = (
    _rule_permissions,          # M (CRITICAL blockers first)
    _rule_safestop,             # N
    _rule_capacity,             # H (both capacity codes)
    _rule_cal_stale,            # CAL-STALE
    _rule_stall,                # J
    _rule_nudge_far,            # A
    _rule_shake_early,          # B
    _rule_shake_late,           # C
    _rule_cue_pan_miss,         # D
    _rule_cue_deposit_miss,     # E
    _rule_cue_shake_miss,       # F
    _rule_autopan_stuck,        # G
    _rule_recovery_loop,        # I
    _rule_finds_miss,           # K
    _rule_analytics_stale,      # L
    _rule_build_conflict,       # O
)


def evaluate(ctx):
    """Run every rule over the host-assembled ctx dict. Never raises:
    a bad ctx yields fewer events, not a crash. Returns events sorted by
    (severity desc, confidence desc, priority)."""
    if not isinstance(ctx, dict):
        ctx = {}
    events = []
    for idx, rule in enumerate(_RULES):
        try:
            got = rule(ctx) or []
        except Exception:
            # A diagnostics bug must never take the app down; the tests
            # exercise every rule's trip path so real regressions still
            # surface there.
            continue
        for ev in got:
            if ev.get("priority", 100) == 100:
                ev["priority"] = idx
            events.append(ev)
    return sort_events(events)


# --------------------------------------------------------------------------
# D1.4 Escalation, dedupe, suppression (pure helpers for the D2 host store)
# --------------------------------------------------------------------------
# Each rule's severity ladder: ((min_occurrences, severity), ...) in
# ascending order. escalate() picks the highest rung reached.
LADDERS = {
    "PP-D-CUE-PAN-MISS": ((1, "NOTICE"), (3, "WARNING"), (6, "ERROR")),
    "PP-D-CUE-DEPOSIT-MISS": ((1, "NOTICE"), (3, "WARNING"), (6, "ERROR")),
    "PP-D-CUE-SHAKE-MISS": ((1, "NOTICE"), (3, "WARNING"), (6, "ERROR")),
    "PP-D-STALL": ((1, "WARNING"), (3, "ERROR")),
    "PP-D-NUDGE-FAR": ((1, "WARNING"),),
    "PP-D-SHAKE-EARLY": ((1, "WARNING"),),
    "PP-D-SHAKE-LATE": ((1, "WARNING"),),
    "PP-D-AUTOPAN-STUCK": ((1, "WARNING"),),
    "PP-D-RECOVERY-LOOP": ((1, "WARNING"), (4, "ERROR")),
    "PP-D-FINDS-MISS": ((1, "NOTICE"), (3, "WARNING")),
    "PP-D-ANALYTICS-STALE": ((1, "NOTICE"),),
    "PP-D-CAP-SUSPECT": ((1, "ERROR"),),
    "PP-D-CAP-HARDSTOP": ((1, "ERROR"),),
    "PP-D-CAL-STALE": ((1, "WARNING"),),
    "PP-D-SAFESTOP": ((1, "ERROR"),),
    "PP-D-BUILD-CONFLICT": ((1, "NOTICE"),),
}


def escalate(code, occurrences):
    """Severity for a code at a given occurrence count, per the rule's
    declared ladder. Unknown codes return None (caller keeps its
    severity)."""
    ladder = LADDERS.get(code)
    if not ladder:
        return None
    try:
        occ = int(occurrences)
    except (TypeError, ValueError):
        occ = 1
    sev = ladder[0][1]
    for min_occ, s in ladder:
        if occ >= min_occ:
            sev = s
    return sev


def merge_events(prior, fresh, now):
    """Dedupe fresh events against the prior list by code: carry
    first_seen forward, bump recurrence_count, stamp last_seen, escalate
    severity via the code's ladder (never downgrade below the fresh
    severity), and sort. Prior codes absent from fresh are dropped
    (resolved)."""
    by_code = {}
    for e in prior or []:
        if isinstance(e, dict) and e.get("code"):
            by_code[e["code"]] = e
    out = []
    for ev in fresh or []:
        if not isinstance(ev, dict):
            continue
        e = dict(ev)
        old = by_code.get(e.get("code"))
        if old:
            e["first_seen"] = old.get("first_seen", now)
            try:
                e["recurrence_count"] = int(
                    old.get("recurrence_count", 1)) + 1
            except (TypeError, ValueError):
                e["recurrence_count"] = 2
        else:
            e["first_seen"] = now
            e["recurrence_count"] = 1
        e["last_seen"] = now
        esc = escalate(e.get("code"), e["recurrence_count"])
        if esc and _SEV_RANK.get(esc, 0) > _SEV_RANK.get(
                e.get("severity"), 0):
            e["severity"] = esc
        out.append(e)
    return sort_events(out)


def load_suppressions(path):
    """Read the tiny suppression store: {code: {'until': epoch|None,
    'forever': bool}}. Any problem yields {} -- never raises."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_suppressions(path, data):
    """Atomic write (tmp + os.replace) of the suppression store. Returns
    True on success; never raises."""
    if not isinstance(data, dict):
        return False
    tmp = "%s.tmp" % path
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def apply_suppressions(events, sup, now):
    """Filter suppressed events. CRITICAL is never suppressible, nor is
    any event whose suppressible flag is False. 'forever' beats 'until';
    an expired 'until' no longer suppresses."""
    if not isinstance(sup, dict):
        return list(events or [])
    out = []
    for e in events or []:
        if not isinstance(e, dict):
            continue
        if e.get("severity") == "CRITICAL" or not e.get("suppressible",
                                                        True):
            out.append(e)
            continue
        s = sup.get(e.get("code"))
        if isinstance(s, dict):
            if s.get("forever"):
                continue
            until = s.get("until")
            try:
                if until is not None and float(now) < float(until):
                    continue
            except (TypeError, ValueError):
                pass
        out.append(e)
    return out


# --------------------------------------------------------------------------
# D1.5 FAQ knowledge base
# --------------------------------------------------------------------------
FAQ_ENTRIES = [
    {
        "id": "faq-mac-permission-identity",
        "question": "I granted the permission, but after an update or "
                    "reinstall macOS still says it is not granted.",
        "symptoms": ["The System Settings pane shows Prospector Lite "
                     "enabled, but the in-app test fails",
                     "Start is blocked with 'perm:' naming a permission "
                     "you already granted",
                     "Screen tests return black frames after replacing "
                     "the app"],
        "likely_causes": ["macOS applies Screen Recording (and sometimes "
                          "Input Monitoring) only on the app's next "
                          "launch",
                          "A replaced app bundle can leave the pane's "
                          "old grant entry pointing at the previous "
                          "copy of the app"],
        "first_action": "Quit Prospector Lite fully (Cmd+Q) and reopen "
                        "it -- most 'granted but failing' states are "
                        "just the pending-restart case.",
        "steps": ["Quit the app fully and reopen it; macOS may offer to "
                  "relaunch it for you.",
                  "If the test still fails: open the System Settings "
                  "pane, remove the Prospector Lite entry (the minus "
                  "button), then click Request on the capability card "
                  "so the fresh copy registers itself.",
                  "Run the capability Test in the Trust Center to "
                  "confirm."],
        "related_settings": [],
        "related_calibrations": [],
        "related_permissions": ["screen_detection", "input_control",
                                "stop_hotkeys"],
        "verify": "All three capability cards read granted and their "
                  "Tests pass.",
        "platforms": ["mac"],
    },
    {
        "id": "faq-mac-screen-recording",
        "question": "Why does the app need Screen Recording, and why "
                    "does detection return black frames?",
        "symptoms": ["Calibration tests report a denied capture",
                     "Start blocked naming screen_detection",
                     "The screen test shows a black frame"],
        "likely_causes": ["Screen Recording is not granted",
                          "It was granted but the app has not been "
                          "restarted since (macOS applies it on next "
                          "launch)"],
        "first_action": "Open the Trust Center and press Request on the "
                        "Screen detection card.",
        "steps": ["System Settings -> Privacy & Security -> Screen "
                  "Recording -> switch on Prospector Lite (or use the "
                  "in-app Request button).",
                  "Quit and reopen the app so macOS applies the grant.",
                  "Run the in-app Test: it captures one small patch, "
                  "shows it once, and discards it."],
        "related_settings": [],
        "related_calibrations": [],
        "related_permissions": ["screen_detection"],
        "verify": "The Screen detection Test reports a non-blank "
                  "capture.",
        "platforms": ["mac"],
    },
    {
        "id": "faq-mac-input-monitoring",
        "question": "What are Accessibility and Input Monitoring for, "
                    "and is the app reading my keystrokes?",
        "symptoms": ["Start blocked naming input_control or "
                     "stop_hotkeys",
                     "The input test fails or nothing gets pressed in "
                     "game"],
        "likely_causes": ["Accessibility (posting keys/clicks) or Input "
                          "Monitoring (hearing the stop hotkeys) is not "
                          "granted"],
        "first_action": "Grant the missing pane from the Trust Center "
                        "capability card.",
        "steps": ["Accessibility covers PRESSING keys/mouse (output "
                  "only -- it cannot read your input).",
                  "Input Monitoring covers HEARING the Safe Stop chords "
                  "while Roblox has focus.",
                  "Grant each from its System Settings pane or the "
                  "in-app Request button, then run the in-app tests."],
        "related_settings": [],
        "related_calibrations": [],
        "related_permissions": ["input_control", "stop_hotkeys"],
        "verify": "The input test posts one keystroke into the app's "
                  "own test field and sees both key-down and key-up.",
        "platforms": ["mac"],
    },
    {
        "id": "faq-win-smartscreen",
        "question": "Windows says 'Windows protected your PC' "
                    "(SmartScreen) -- is the download unsafe?",
        "symptoms": ["SmartScreen blocks the installer or portable exe "
                     "on first run"],
        "likely_causes": ["The build is not yet Authenticode-signed, so "
                          "SmartScreen shows its reputation warning -- "
                          "'unsigned/uncommon', not 'malicious'"],
        "first_action": "Verify the download's checksum first "
                        "(VERIFY_DOWNLOAD.md), then choose More info -> "
                        "Run anyway.",
        "steps": ["Verify the SHA-256 checksum against the published "
                  "value.",
                  "Click More info, then Run anyway.",
                  "Never disable SmartScreen or Defender -- no "
                  "legitimate instruction ever asks for that."],
        "related_settings": [],
        "related_calibrations": [],
        "related_permissions": [],
        "verify": "The checksum matches and the app starts normally "
                  "afterwards.",
        "platforms": ["win"],
    },
    {
        "id": "faq-safe-stop-hotkeys",
        "question": "My Safe Stop hotkey does nothing while Roblox has "
                    "focus.",
        "symptoms": ["Esc / Ctrl+K does not stop the run",
                     "The hotkey test fails in the Trust Center"],
        "likely_causes": ["Input Monitoring not granted (macOS)",
                          "Another app owns the configured chord "
                          "(PP-HOTKEY-BUSY in the wizard log)"],
        "first_action": "Run the hotkey test in the Trust Center.",
        "steps": ["macOS: grant Input Monitoring, restart the app if "
                  "the test still fails.",
                  "Check the wizard log for PP-HOTKEY-BUSY -- pick a "
                  "different chord on the Keys tab if another app owns "
                  "it.",
                  "Defaults: Esc quits, Ctrl+K start/stop, Ctrl+J soft "
                  "stop, Ctrl+L pause."],
        "related_settings": [],
        "related_calibrations": [],
        "related_permissions": ["stop_hotkeys"],
        "verify": "Press the stop chord during a short run with Roblox "
                  "focused -- the run stops.",
        "platforms": ["mac", "win"],
    },
    {
        "id": "faq-window-detection",
        "question": "The app cannot find the Roblox window.",
        "symptoms": ["Detect reports no window", "window_found is False",
                     "Runs stall immediately"],
        "likely_causes": ["Roblox is on a secondary display",
                          "Roblox is minimised",
                          "Roblox is not running at all"],
        "first_action": "Put Roblox on the primary display (the one "
                        "with the menu bar on macOS) and press Detect.",
        "steps": ["Open Roblox in Prospecting on the primary display, "
                  "windowed or full screen.",
                  "Restore it if minimised.",
                  "Press Detect on the Calibrate tab -- it reads the "
                  "live window list each time and never gives up "
                  "permanently."],
        "related_settings": ["WINDOW_RELATIVE"],
        "related_calibrations": ["roblox_window"],
        "related_permissions": ["screen_detection"],
        "verify": "Detect reports the window size and position, e.g. "
                  "'Found: 1800x1087 at (0, 39)'.",
        "platforms": ["mac", "win"],
    },
    {
        "id": "faq-capacity-calibration",
        "question": "Capacity calibration 'succeeds' but runs misread "
                    "the pan (or hard-stop). What is the right-end "
                    "story?",
        "symptoms": ["Hard stops during runs",
                     "The pan never reads full or empty",
                     "cap_bar shows needs_review"],
        "likely_causes": ["The RIGHT tip click landed on the pale "
                          "anti-aliased edge pixel (the auto-detector "
                          "deliberately walks ~6 px inward to solid "
                          "gold; a manual click has no such guard)",
                          "The stored endpoints are swapped, "
                          "vertically misaligned, or the stored width "
                          "is stale from an earlier calibration"],
        "first_action": "Run Test capacity calibration -- it verifies "
                        "the live reading without saving anything.",
        "steps": ["Open the Capacity step: click the RIGHT tip of the "
                  "pan-fill bar, then the LEFT tip; the app derives the "
                  "width and verifies it (> 20 px).",
                  "Save-time validation now rejects a right tip left "
                  "of (or within 20 px of) the left tip, tips more "
                  "than 8 px apart vertically, and a width under "
                  "24 px or more than 2 px off the tips' distance.",
                  "Run Test capacity to confirm the live read."],
        "related_settings": ["CAP_EMPTY_FRAC"],
        "related_calibrations": ["cap_bar"],
        "related_permissions": ["screen_detection"],
        "verify": "Test capacity reports a valid live reading and a "
                  "short run finishes without hard stops.",
        "platforms": ["mac", "win"],
    },
    {
        "id": "faq-advanced-cues",
        "question": "What is Advanced Cue Matching (the masks), and why "
                    "did it stop matching after I resized the window?",
        "symptoms": ["Prompts stop being detected after a resize",
                     "cue_masks shows stale or needs_review",
                     "Runs stall at the water or land edge"],
        "likely_causes": ["Masks are size-exact letter-shape bitmaps of "
                          "the Pan / Collect Deposit / Shake prompts; "
                          "they disable themselves when the window size "
                          "drifts more than 2 px",
                          "A capture included the white mouse cursor or "
                          "the wrong prompt"],
        "first_action": "Re-capture the three prompts after any window "
                        "resize.",
        "steps": ["Masks are the primary detector: they match exact "
                  "letter shapes so a random white texture cannot "
                  "trigger them; the single-pixel checks remain only as "
                  "a fallback.",
                  "At run time a mask must clear a 0.85 white-fraction "
                  "match over its letter pixels.",
                  "The drift disable is deliberate: more than 2 px of "
                  "window-size drift and the mask no longer lines up, "
                  "so it turns itself off -- re-capture, don't fight "
                  "it."],
        "related_settings": [],
        "related_calibrations": ["cue_masks", "pan_prompt",
                                 "deposit_prompt", "shake_prompt"],
        "related_permissions": ["screen_detection"],
        "verify": "The cue check on the Calibrate tab clears the match "
                  "threshold for all three prompts.",
        "platforms": ["mac", "win"],
    },
    {
        "id": "faq-autopan-stuck",
        "question": "Auto Pan keeps turning off, or the tracker keeps "
                    "kicking it.",
        "symptoms": ["Repeated autopan_guard / autopan_kick events",
                     "Auto Pan visibly off while the tracker thinks "
                     "it is on"],
        "likely_causes": ["The click is re-read before the game "
                          "reacts (settle too short)",
                          "Colour tolerance too tight for current "
                          "lighting",
                          "The Auto Pan button pixel is stale or "
                          "uncalibrated"],
        "first_action": "Increase the wait after clicking Auto Pan by "
                        "one step.",
        "steps": ["Raise 'Wait after clicking Auto Pan (ms)' one step.",
                  "If the button state misreads, widen the colour "
                  "tolerance.",
                  "Enable the idle kick ('Restart Auto Pan if idle "
                  "this long') if a wedged Auto Pan should self-heal.",
                  "Re-calibrate the Auto Pan button pixel if the "
                  "window moved."],
        "related_settings": ["AUTOPAN_SETTLE_MS", "AUTOPAN_TOL",
                             "AUTOPAN_STALL_SEC", "AUTOPAN_GUARD",
                             "AUTOPAN_GUARD_SEC"],
        "related_calibrations": ["autopan_button"],
        "related_permissions": ["screen_detection", "input_control"],
        "verify": "Toggle Auto Pan in game: the tracker follows, and "
                  "guard/kick events stop repeating.",
        "platforms": ["mac", "win"],
    },
    {
        "id": "faq-movement-nudges",
        "question": "The character keeps getting nudged back -- why so "
                    "many corrective nudges?",
        "symptoms": ["High nudge count on the Run tab",
                     "The yellow Cycle warning flags the land stage"],
        "likely_causes": ["Extra walk back carries movement past the "
                          "expected stop point",
                          "The shake starts while movement is still "
                          "settling",
                          "Too little settle after the land cue"],
        "first_action": "Reduce 'Extra S after Pan cue / go deeper "
                        "(ms)' by one step.",
        "steps": ["Reduce Extra walk back first.",
                  "If Shake begins while movement is still settling, "
                  "increase Delay before shake starts.",
                  "If nudges happen on land, increase Hold W after "
                  "land cue."],
        "related_settings": ["WATER_EXTRA_BACK_MS",
                             "SHAKE_START_DELAY_MS", "LAND_SETTLE_MS",
                             "PAN_BACK_MAX_MS"],
        "related_calibrations": ["pan_prompt", "cue_masks"],
        "related_permissions": [],
        "verify": "Run at least 5 cycles: the nudge rate should fall "
                  "below 0.6 per cycle.",
        "platforms": ["mac", "win"],
    },
    {
        "id": "faq-shake-timing",
        "question": "Shakes start too early, retry, or get marked as "
                    "missed -- how do I tune shake timing?",
        "symptoms": ["shake_start_retry / shake_glitch events",
                     "High shake-miss rate", "Pans wasted on failed "
                     "shakes"],
        "likely_causes": ["The shake click lands before the game is "
                          "ready (too early)",
                          "The shake starts after the detection window "
                          "(too late)",
                          "The Shake prompt calibration is stale"],
        "first_action": "For retries: increase Delay before shake "
                        "starts one step. For misses with a delay set: "
                        "reduce it one step.",
        "steps": ["Early symptoms (retries/glitches): raise the shake "
                  "delay, or enable the start confirmation window so a "
                  "missed start is retried fast.",
                  "Late symptoms (misses): lower the delay, or give "
                  "shake-failed detection more time.",
                  "Re-check the Shake cue capture if timing changes "
                  "don't help."],
        "related_settings": ["SHAKE_START_DELAY_MS",
                             "SHAKE_START_CONFIRM_MS", "SHAKE_BAIL_MS",
                             "EASY_SHAKE_DELAY_MS", "SHAKE_HOLD_MS"],
        "related_calibrations": ["shake_prompt", "cue_masks"],
        "related_permissions": [],
        "verify": "Across 5+ cycles both the retry count and the miss "
                  "rate stay low.",
        "platforms": ["mac", "win"],
    },
    {
        "id": "faq-recovery-loops",
        "question": "The macro keeps 'recovering' over and over instead "
                    "of panning.",
        "symptoms": ["Recovery rate at or above 0.5 per cycle",
                     "recovery_rung events climbing",
                     "The safety stage badge lights up"],
        "likely_causes": ["The watchdog fires on healthy-but-slow "
                          "cycles",
                          "Each nudge is too small to free the "
                          "character",
                          "The walk-back parks the character short "
                          "(water misdetection)"],
        "first_action": "Lower 'Recoveries before break-out' one step "
                        "so loops escalate to a break-out sooner.",
        "steps": ["Reduce the recovery limit (escalate sooner).",
                  "Raise the no-progress watchdog if it is set very "
                  "tight.",
                  "Raise the recovery nudge budget so one nudge frees "
                  "the wedge.",
                  "If it always happens at the water edge, re-check "
                  "the Pan prompt calibration."],
        "related_settings": ["RECOVER_LIMIT", "NO_PROGRESS_SEC",
                             "RECOVER_BACK_MS", "STUCK_TICKS",
                             "BREAKOUT_LIMIT"],
        "related_calibrations": ["pan_prompt", "cue_masks"],
        "related_permissions": [],
        "verify": "Run 5+ cycles: recoveries drop below 0.5 per cycle.",
        "platforms": ["mac", "win"],
    },
    {
        "id": "faq-missed-prompts",
        "question": "The Pan / Collect Deposit / Shake prompt is on "
                    "screen but the macro does not react.",
        "symptoms": ["Runs stall at the water or land edge",
                     "no_progress events while a prompt is visible"],
        "likely_causes": ["The prompt masks or pixels are stale (window "
                          "moved/resized since capture)",
                          "The capture was taken on the wrong prompt or "
                          "included the cursor"],
        "first_action": "Run the cue check on the Calibrate tab for the "
                        "affected prompt.",
        "steps": ["Check the cue masks first -- they are the primary "
                  "detector; the prompt pixels are only the fallback.",
                  "Re-capture the affected prompt(s) with the window at "
                  "its playing size.",
                  "If the window changed size, expect the masks to have "
                  "disabled themselves (the 2 px drift rule)."],
        "related_settings": [],
        "related_calibrations": ["pan_prompt", "deposit_prompt",
                                 "shake_prompt", "cue_masks"],
        "related_permissions": ["screen_detection"],
        "verify": "The cue check clears the match threshold and a short "
                  "run passes the affected stage.",
        "platforms": ["mac", "win"],
    },
    {
        "id": "faq-ui-scaling",
        "question": "I changed my display scaling / resolution / "
                    "monitor arrangement and everything broke.",
        "symptoms": ["Red 'Re-calibration needed' banner",
                     "Every detector misses at once"],
        "likely_causes": ["All calibration is stored relative to the "
                          "window rectangle captured at calibration "
                          "time; scaling or monitor changes move every "
                          "point at once"],
        "first_action": "Either restore the previous display setup, or "
                        "re-run calibration at the new one.",
        "steps": ["Calibration health compares the stored window size "
                  "to the live one within +-4 px -- a scale change "
                  "trips it immediately.",
                  "Cue masks are stricter still: more than 2 px of "
                  "size drift disables them.",
                  "Re-run the Capacity and prompt steps at the new "
                  "layout (or restore the old one exactly)."],
        "related_settings": ["WINDOW_RELATIVE"],
        "related_calibrations": ["roblox_window", "cap_bar",
                                 "cue_masks"],
        "related_permissions": [],
        "verify": "The calibration health banner clears and the cue "
                  "check passes.",
        "platforms": ["mac", "win"],
    },
    {
        "id": "faq-roblox-window-changes",
        "question": "I moved or resized the Roblox window -- do I have "
                    "to recalibrate?",
        "symptoms": ["'The Roblox window is WxH now but you calibrated "
                     "at WxH' banner", "Items flip to stale"],
        "likely_causes": ["The live window rectangle no longer matches "
                          "the one stored at calibration time (the "
                          "health check allows +-4 px)"],
        "first_action": "If you can, restore the window to its "
                        "calibrated size and position -- health "
                        "recovers on its own.",
        "steps": ["A pure MOVE with 'Shift pixels when the Roblox "
                  "window moves' enabled can be compensated; a RESIZE "
                  "cannot.",
                  "Otherwise re-run the affected calibration steps at "
                  "the new geometry.",
                  "Leave the window where you intend to play before "
                  "calibrating."],
        "related_settings": ["WINDOW_RELATIVE"],
        "related_calibrations": ["roblox_window", "cue_masks"],
        "related_permissions": [],
        "verify": "The red banner clears (health ok) and prompts are "
                  "detected again.",
        "platforms": ["mac", "win"],
    },
    {
        "id": "faq-analytics-regions",
        "question": "Earnings tracking shows nothing (money/shards "
                    "stay empty or frozen).",
        "symptoms": ["Empty earnings stats with EARN_TRACK on",
                     "Totals that never change"],
        "likely_causes": ["The money/shards counter boxes were never "
                          "drawn or went stale",
                          "The read interval is set very long, so "
                          "totals only refresh rarely"],
        "first_action": "Re-draw the money and shards boxes on the "
                        "Calibrate tab (tight around the numbers).",
        "steps": ["Drag a tight box around the money number, and one "
                  "around the shards number.",
                  "Use the region read test to confirm the OCR returns "
                  "the on-screen value.",
                  "Earnings count as the difference between reads -- "
                  "keep 'Read the totals every (seconds)' reasonable "
                  "(default 10)."],
        "related_settings": ["EARN_TRACK", "EARN_OCR_SEC"],
        "related_calibrations": ["money_region", "shards_region"],
        "related_permissions": ["screen_detection"],
        "verify": "The region read test returns the number you see on "
                  "screen and totals move within one interval.",
        "platforms": ["mac", "win"],
    },
    {
        "id": "faq-finds-popup-box",
        "question": "The finds log misses finds, or shows "
                    "ghost/duplicate entries.",
        "symptoms": ["Finds you saw never appear in the log",
                     "finds_ghost / finds_fork events",
                     "One find logged several times"],
        "likely_causes": ["The finds pop-up box is off the cards "
                          "(never drawn, or the window moved)",
                          "The OCR confidence gate rejects faint "
                          "cards",
                          "Card lifetime / quiet-window timing splits "
                          "one animated card into copies"],
        "first_action": "Re-draw the finds pop-up box over where the "
                        "cards actually appear.",
        "steps": ["Re-draw the box on the Calibrate tab.",
                  "Missing finds: lower the confidence gate one step.",
                  "Ghost/fork spam: widen the card lifetime and the "
                  "quiet window one step each."],
        "related_settings": ["FINDS_TRACK", "FINDS_MIN_CONF",
                             "FINDS_CARD_SEC", "FINDS_EMPTY_MS",
                             "FINDS_MIN_DWELL"],
        "related_calibrations": ["find_region"],
        "related_permissions": ["screen_detection"],
        "verify": "Trigger a few finds: each appears exactly once in "
                  "the log.",
        "platforms": ["mac", "win"],
    },
    {
        "id": "faq-reading-logs",
        "question": "Where are the logs and diagnostics, and how do I "
                    "share them when asking for help?",
        "symptoms": ["You were asked for logs", "You want to see why a "
                     "run stopped"],
        "likely_causes": [],
        "first_action": "Use 'Export diagnostics' on the wizard "
                        "Readiness page or in the Trust Center.",
        "steps": ["onboarding.log -- the structured wizard/ops log "
                  "with PP-* codes (256 KB rotation, no secrets).",
                  "run_history.json -- the last 100 runs with stats, "
                  "event counts and stop reasons.",
                  "run_logs/run-*.log -- full per-run engine "
                  "diagnostic lines.",
                  "'Copy summary' produces a short capability + "
                  "readiness text; 'Export diagnostics' saves the full "
                  "JSON via a save dialog."],
        "related_settings": [],
        "related_calibrations": [],
        "related_permissions": [],
        "verify": "The exported JSON opens and contains readiness, "
                  "capabilities and the log tail.",
        "platforms": ["mac", "win"],
    },
    {
        "id": "faq-updating",
        "question": "How do I update the app safely, and what survives "
                    "an update?",
        "symptoms": ["A new release is out", "Fresh install over an old "
                     "one"],
        "likely_causes": [],
        "first_action": "Verify the new download's checksum before "
                        "replacing anything (VERIFY_DOWNLOAD.md).",
        "steps": ["Verify the checksum, then replace the app.",
                  "Your data folder (settings, calibration, builds, "
                  "history) lives outside the app and persists -- a "
                  "reinstall picks up exactly where you left off.",
                  "macOS: if permission tests fail after the update, "
                  "see the stale-permission entry -- quit/reopen "
                  "first, then re-grant if needed."],
        "related_settings": [],
        "related_calibrations": [],
        "related_permissions": ["screen_detection", "input_control",
                                "stop_hotkeys"],
        "verify": "The app opens, the version matches the release, and "
                  "readiness passes.",
        "platforms": ["mac", "win"],
    },
    {
        "id": "faq-importing-builds",
        "question": "I imported or activated a Studio build/script and "
                    "now Start refuses to launch.",
        "symptoms": ["'no build selected' / 'no script selected' "
                     "toasts",
                     "'A Studio entry is still active' in Classic "
                     "mode",
                     "'The active entry does not match the selected "
                     "mode'"],
        "likely_causes": ["The top-level mode invariant: STUDIO BUILD "
                          "and STUDIO SCRIPT each need an active entry "
                          "of their own kind, and CLASSIC must have "
                          "none -- the app refuses rather than "
                          "silently running the wrong program"],
        "first_action": "Open the tab the refusal names (Studio or "
                        "Script) and make the active entry match the "
                        "mode.",
        "steps": ["Studio Build mode: pick a build on the Studio tab.",
                  "Studio Script mode: pick a script on the Script "
                  "tab.",
                  "Classic mode: clear/deactivate the Studio entry "
                  "(or switch to its Studio mode).",
                  "Running an entry from its own tab flips the mode "
                  "for you."],
        "related_settings": [],
        "related_calibrations": [],
        "related_permissions": [],
        "verify": "Start launches (or names the next, different "
                  "blocker).",
        "platforms": ["mac", "win"],
    },
]

FAQ_BY_ID = {f["id"]: f for f in FAQ_ENTRIES}

# Every faq_id any rule can emit (including the dynamic PP-D-STALL ones).
_RULE_FAQ_IDS = frozenset([
    "faq-movement-nudges", "faq-shake-timing", "faq-missed-prompts",
    "faq-autopan-stuck", "faq-capacity-calibration", "faq-recovery-loops",
    "faq-mac-screen-recording", "faq-mac-input-monitoring",
    "faq-safe-stop-hotkeys", "faq-window-detection",
    "faq-roblox-window-changes", "faq-finds-popup-box",
    "faq-analytics-regions", "faq-importing-builds",
])


def validate_faq():
    """Cross-check the knowledge base against the real registries.
    Returns a list of problem strings; empty means valid."""
    problems = []
    seen = set()
    if len(FAQ_ENTRIES) < 18:
        problems.append("only %d FAQ entries (need >= 18)"
                        % len(FAQ_ENTRIES))
    for f in FAQ_ENTRIES:
        fid = f.get("id", "")
        if fid in seen:
            problems.append("duplicate FAQ id %s" % fid)
        seen.add(fid)
        for k in f.get("related_settings", []):
            if k not in DEFAULTS:
                problems.append("%s: unknown setting %s" % (fid, k))
        for c in f.get("related_calibrations", []):
            if c not in CALIBRATION_IDS:
                problems.append("%s: unknown calibration %s" % (fid, c))
        for p in f.get("related_permissions", []):
            if p not in CAPABILITY_IDS:
                problems.append("%s: unknown permission %s" % (fid, p))
    for fid in sorted(_RULE_FAQ_IDS):
        if fid not in seen:
            problems.append("rule-referenced faq id missing: %s" % fid)
    return problems
