#!/usr/bin/env python3
"""diagnostics_tests.py -- suite for lite_diagnostics (pure Python).

Covers: the setting registry (existence, placement, bounds), every rule
A..O + CAL-STALE with a tripping ctx AND a below-threshold ctx, the
shipped nudge example scenario (9 nudges / 12 cycles), suggestion
clamping at both bounds, auto-apply gating, escalation ladders,
merge/dedupe recurrence, the suppression store round-trip, and the FAQ
knowledge base validation.

Run from the repo root:  python3 diagnostics_tests.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import lite_diagnostics as ld
import lite_onboarding as lo
import prospecting_ui as ui

FAILS = []
EVENTS_SEEN = []          # every event produced by a trip test


def chk(cond, msg):
    print(("  ok: " if cond else "  FAIL: ") + msg)
    if not cond:
        FAILS.append(msg)


def ev(ctx, code):
    """evaluate(ctx), record all events, return the one matching code
    (or None)."""
    events = ld.evaluate(ctx)
    EVENTS_SEEN.extend(events)
    for e in events:
        if e["code"] == code:
            return e
    return None


def no_ev(ctx, code):
    return all(e["code"] != code for e in ld.evaluate(ctx))


def rec_by_id(event, rec_id):
    for r in event["recommendations"]:
        if r["id"] == rec_id:
            return r
    return None


def target(event, key):
    for r in event["recommendations"]:
        for t in r["setting_targets"]:
            if t["key"] == key:
                return t
    return None


def cal_targets(event):
    out = []
    for r in event["recommendations"]:
        out.extend(r["calibration_targets"])
    return out


def perm_targets(event):
    out = []
    for r in event["recommendations"]:
        out.extend(r["permission_targets"])
    return out


# ---------------------------------------------------------------------------
def t_registry():
    print("[1] setting registry (SECTIONS + RANGES + HELP + _META)")
    reg = ld.SETTING_REGISTRY
    rule_keys = ["WATER_EXTRA_BACK_MS", "SHAKE_START_DELAY_MS",
                 "LAND_SETTLE_MS", "SHAKE_START_CONFIRM_MS",
                 "EASY_SHAKE_DELAY_MS", "SHAKE_BAIL_MS",
                 "AUTOPAN_SETTLE_MS", "AUTOPAN_TOL", "AUTOPAN_STALL_SEC",
                 "RECOVER_LIMIT", "NO_PROGRESS_SEC", "RECOVER_BACK_MS",
                 "FINDS_MIN_CONF", "FINDS_CARD_SEC", "FINDS_EMPTY_MS",
                 "EARN_OCR_SEC"]
    chk(all(k in ui.DEFAULTS for k in rule_keys),
        "every rule-referenced setting key exists in SECTIONS")
    chk(all(k in reg for k in rule_keys),
        "every rule-referenced key has a registry entry")
    chk(all(k in ui.DEFAULTS for k in ld._META),
        "every curated _META key is a real SECTIONS key")
    chk(len(reg) == len(ui.DEFAULTS),
        "registry covers every SECTIONS key (%d)" % len(reg))
    # placement spot checks (map_settings.md facts)
    chk(reg["WATER_EXTRA_BACK_MS"]["control"] == "cycle"
        and reg["WATER_EXTRA_BACK_MS"]["tab"] == "cycle",
        "WATER_EXTRA_BACK_MS renders on the Cycle page")
    chk(reg["FINDS_MIN_CONF"]["control"] == "tab"
        and reg["FINDS_MIN_CONF"]["tab"] == "Earnings",
        "FINDS_MIN_CONF renders on the 'Earnings' tab")
    chk(reg["AUTOPAN_SETTLE_MS"]["control"] == "tab"
        and reg["AUTOPAN_SETTLE_MS"]["tab"] == "Tracker",
        "AUTOPAN_SETTLE_MS renders on the 'Tracker' tab")
    moved = set(ld._MOVED_SECTIONS)
    ok_cycle = all(e["control"] == ("cycle" if e["section"] in moved
                                    else "tab")
                   for e in reg.values())
    chk(ok_cycle, "control is 'cycle' exactly for the 7 moved sections")
    chk(reg["SHAKE_BAIL_MS"]["lo"] == 200
        and reg["SHAKE_BAIL_MS"]["hi"] == 2500
        and reg["SHAKE_BAIL_MS"]["step"] == 150,
        "registry lo/hi/step come from RANGES (SHAKE_BAIL_MS 200/2500/150)")
    chk(reg["TRACKER_POLL_MS"]["lo"] is None,
        "keys without a RANGES entry carry lo=None")
    chk(reg["WATER_EXTRA_BACK_MS"]["units"] == "ms"
        and reg["EARN_OCR_SEC"]["units"] == "s",
        "units parsed from the shipped labels")
    chk(bool(reg["WATER_EXTRA_BACK_MS"]["help"]),
        "HELP text is attached")
    chk(reg["TRACKER_POLL_MS"]["safe_auto_apply"] is False,
        "safe_auto_apply is False without RANGES bounds")


def t_clamping():
    print("[2] clamping + auto-apply gating")
    chk(ld.clamp_suggestion("WATER_EXTRA_BACK_MS", -50) == 0,
        "int clamp at the low bound (WATER_EXTRA_BACK_MS -> 0)")
    chk(ld.clamp_suggestion("WATER_EXTRA_BACK_MS", 5000) == 1200,
        "int clamp at the high bound (WATER_EXTRA_BACK_MS -> 1200)")
    chk(ld.clamp_suggestion("FINDS_MIN_CONF", -0.2) == 0.0,
        "float clamp at the low bound (FINDS_MIN_CONF -> 0.0)")
    chk(ld.clamp_suggestion("FINDS_MIN_CONF", 1.7) == 1.0,
        "float clamp at the high bound (FINDS_MIN_CONF -> 1.0)")
    chk(ld.clamp_suggestion("TRACKER_POLL_MS", 99999) == 99999,
        "no RANGES entry -> pass-through (never invents bounds)")
    t = ld.setting_target("SHAKE_START_DELAY_MS", current=0, suggested=50)
    chk(t["lo"] == 0 and t["hi"] == 1000 and t["step"] == 50
        and t["delta"] == 50,
        "setting_target carries RANGES bounds and the delta")
    rec = ld.make_recommendation(
        "x", "t", "e",
        setting_targets=[ld.setting_target("TRACKER_POLL_MS",
                                           current=30, suggested=40)],
        auto_apply=True)
    chk(rec["auto_apply"] is False,
        "auto_apply forced False for a key without RANGES")
    rec = ld.make_recommendation("x", "t", "e", auto_apply=True)
    chk(rec["auto_apply"] is False,
        "auto_apply forced False without setting targets")
    # a clamped suggestion landing ON the current value is a no-op: the
    # target must become open-only and the recommendation loses one-click
    t = ld.setting_target("WATER_EXTRA_BACK_MS", current=0, suggested=-80)
    chk(t["suggested"] is None and t["delta"] is None,
        "bound-pinned suggestion (current 0, step down) becomes open-only")
    rec = ld.make_recommendation("x", "t", "e", setting_targets=[t],
                                 auto_apply=True)
    chk(rec["auto_apply"] is False,
        "auto_apply forced False when the clamped suggestion is a no-op")
    t = ld.setting_target("WATER_EXTRA_BACK_MS", current=400, suggested=320)
    chk(t["suggested"] == 320 and t["delta"] == -80,
        "a real reduction away from the bound keeps its suggestion")


def t_rule_nudge_far():
    print("[3] rule A PP-D-NUDGE-FAR (+ the shipped example scenario)")
    ctx = {"stats": {"cycles": 12, "nudges": 9},
           "settings": {"WATER_EXTRA_BACK_MS": 240,
                        "SHAKE_START_DELAY_MS": 0,
                        "LAND_SETTLE_MS": 45}}
    e = ev(ctx, "PP-D-NUDGE-FAR")
    chk(e is not None, "nudges 9/12 cycles trips PP-D-NUDGE-FAR")
    if e:
        chk(e["severity"] == "WARNING", "severity WARNING")
        chk(e["confidence"] == "medium",
            "0.75/cycle is medium confidence (< 0.8)")
        chk("nudges: 9 in 12 cycles (0.75/cycle, threshold 0.6)"
            in e["evidence"], "numeric evidence string is exact")
        chk("past the expected stop point" in e["summary"],
            "summary uses the shipped wording")
        recs = sorted(e["recommendations"], key=lambda r: r["priority"])
        chk(recs[0]["setting_targets"][0]["key"] == "WATER_EXTRA_BACK_MS",
            "WATER_EXTRA_BACK_MS reduce is priority 1")
        chk(recs[0]["setting_targets"][0]["suggested"] == 160
            and recs[0]["setting_targets"][0]["delta"] == -80,
            "reduce by one RANGES step: 240 -> 160")
        chk("reduce Extra walk back first" in recs[0]["explanation"],
            "priority-1 explanation follows the shipped wording")
        chk(recs[1]["setting_targets"][0]["key"] == "SHAKE_START_DELAY_MS"
            and recs[1]["setting_targets"][0]["suggested"] == 50,
            "SHAKE_START_DELAY_MS +one step is priority 2")
        chk("still settling" in recs[1]["explanation"]
            and "Delay before shake starts" in recs[1]["explanation"],
            "priority-2 explanation follows the shipped wording")
        chk(recs[2]["setting_targets"][0]["key"] == "LAND_SETTLE_MS"
            and recs[2]["setting_targets"][0]["suggested"] == 70,
            "LAND_SETTLE_MS increase is priority 3")
        chk(all(r["tradeoff"] for r in recs), "every tradeoff non-empty")
        chk(all(r["auto_apply"] for r in recs),
            "bounded suggestions are auto-appliable")
        chk(e["other_causes"], "other_causes filled")
    ctx = {"stats": {"cycles": 12, "nudges": 10}}
    e = ev(ctx, "PP-D-NUDGE-FAR")
    chk(e is not None and e["confidence"] == "high",
        "0.83/cycle is high confidence (>= 0.8)")
    chk(no_ev({"stats": {"cycles": 12, "nudges": 5}}, "PP-D-NUDGE-FAR"),
        "no trip below threshold (0.42/cycle)")
    chk(no_ev({"stats": {"cycles": 4, "nudges": 4}}, "PP-D-NUDGE-FAR"),
        "no trip under 5 cycles even at a high rate")


def t_rule_shake_early():
    print("[4] rule B PP-D-SHAKE-EARLY")
    e = ev({"event_counts": {"shake_start_retry": 6}}, "PP-D-SHAKE-EARLY")
    chk(e is not None, "shake_start_retry 6 trips")
    if e:
        chk(e["severity"] == "WARNING" and e["confidence"] == "high",
            "WARNING, high at double the threshold")
        chk("shake_start_retry: 6 this run (threshold 3)" in e["evidence"],
            "numeric evidence string")
        chk(target(e, "SHAKE_START_DELAY_MS")["suggested"] == 50
            and target(e, "SHAKE_START_CONFIRM_MS")["suggested"] == 50
            and target(e, "EASY_SHAKE_DELAY_MS")["suggested"] == 60,
            "targets: +one step each (50/50/60)")
    e = ev({"event_counts": {"shake_glitch": 2}}, "PP-D-SHAKE-EARLY")
    chk(e is not None and e["confidence"] == "possible"
        and "may" in e["title"],
        "a barely-tripped threshold is only a possibility")
    chk(no_ev({"event_counts": {"shake_start_retry": 2,
                                "shake_glitch": 1}}, "PP-D-SHAKE-EARLY"),
        "no trip below both thresholds")


def t_rule_shake_late():
    print("[5] rule C PP-D-SHAKE-LATE")
    ctx = {"stats": {"cycles": 10, "shake_misses": 4},
           "settings": {"SHAKE_START_DELAY_MS": 100}}
    e = ev(ctx, "PP-D-SHAKE-LATE")
    chk(e is not None, "shake_misses 4/10 cycles trips")
    if e:
        chk("shake_misses: 4 in 10 cycles (0.40/cycle, threshold 0.4)"
            in e["evidence"], "numeric evidence string")
        chk(e["confidence"] == "possible" and "may" in e["title"],
            "right at the threshold reads as a possibility")
        chk(target(e, "SHAKE_START_DELAY_MS")["suggested"] == 50,
            "delay reduced one step when current > 0")
        chk(target(e, "SHAKE_BAIL_MS")["suggested"] == 650,
            "SHAKE_BAIL_MS +one step (500 -> 650)")
        chk("cue_masks" in cal_targets(e),
            "cue-mask review is recommended")
    ctx = {"stats": {"cycles": 10, "shake_misses": 6},
           "settings": {"SHAKE_START_DELAY_MS": 0}}
    e = ev(ctx, "PP-D-SHAKE-LATE")
    chk(e is not None and target(e, "SHAKE_START_DELAY_MS") is None,
        "no delay-reduction target when the delay is already 0")
    chk(no_ev({"stats": {"cycles": 10, "shake_misses": 3}},
              "PP-D-SHAKE-LATE"), "no trip below threshold (0.3/cycle)")


def t_rules_cue_miss():
    print("[6] rules D/E/F PP-D-CUE-*-MISS")
    for code, item, cue in (
            ("PP-D-CUE-PAN-MISS", "pan_prompt", "Pan"),
            ("PP-D-CUE-DEPOSIT-MISS", "deposit_prompt", "Collect Deposit"),
            ("PP-D-CUE-SHAKE-MISS", "shake_prompt", "Shake")):
        ctx = {"event_counts": {"no_progress": 1},
               "cal_status": {item: {"status": "stale"}}}
        e = ev(ctx, code)
        chk(e is not None, "%s: stale %s + 1 stall trips" % (code, item))
        if e:
            chk(e["severity"] == "NOTICE",
                "%s: 1 isolated stall = NOTICE (ladder)" % code)
            chk("no_progress: 1 this run (threshold 1)" in e["evidence"]
                and ("%s status: stale" % item) in e["evidence"],
                "%s: evidence carries the stall count + status" % code)
            tgts = cal_targets(e)
            chk("cue_masks" in tgts and item in tgts,
                "%s: calibration targets cue_masks + %s" % (code, item))
        ctx["event_counts"]["no_progress"] = 3
        e = ev(ctx, code)
        chk(e is not None and e["severity"] == "WARNING",
            "%s: 3 stalls escalate to WARNING" % code)
        chk(no_ev({"cal_status": {item: {"status": "stale"}}}, code),
            "%s: no trip without a no_progress stall" % code)
        chk(no_ev({"event_counts": {"no_progress": 2},
                   "cal_status": {item: {"status": "ok"},
                                  "cue_masks": {"status": "ok"}}}, code),
            "%s: no trip when calibration and health are fine" % code)
    reason = ("The Roblox window is 1600x900 now but you calibrated at "
              "1800x1087.")
    ctx = {"event_counts": {"no_progress": 1},
           "cal_status": {"pan_prompt": {"status": "needs_review"}},
           "cal_health": {"ok": False, "reason": reason}}
    e = ev(ctx, "PP-D-CUE-PAN-MISS")
    chk(e is not None and reason in e["summary"],
        "window-change reason from cal_health is quoted in the summary")
    chk(e is not None and e["confidence"] == "high",
        "stale item + bad health together read high confidence")


def t_rule_autopan():
    print("[7] rule G PP-D-AUTOPAN-STUCK")
    ctx = {"event_counts": {"autopan_kick": 2},
           "cal_status": {"autopan_button": {"status": "unset"}}}
    e = ev(ctx, "PP-D-AUTOPAN-STUCK")
    chk(e is not None, "autopan_kick 2 trips")
    if e:
        chk("autopan_kick: 2 this run (threshold 2)" in e["evidence"],
            "numeric evidence string")
        chk(target(e, "AUTOPAN_SETTLE_MS")["suggested"] == 450
            and target(e, "AUTOPAN_TOL")["suggested"] == 45,
            "AUTOPAN_SETTLE_MS/AUTOPAN_TOL +one step (450/45)")
        chk(target(e, "AUTOPAN_STALL_SEC")["suggested"] == 5,
            "idle kick enabled (0 -> 5)")
        chk("autopan_button" in cal_targets(e),
            "autopan_button recalibration recommended when not ok")
    ctx = {"event_counts": {"autopan_guard": 3},
           "settings": {"AUTOPAN_STALL_SEC": 10},
           "cal_status": {"autopan_button": {"status": "ok"}}}
    e = ev(ctx, "PP-D-AUTOPAN-STUCK")
    chk(e is not None and target(e, "AUTOPAN_STALL_SEC") is None
        and "autopan_button" not in cal_targets(e),
        "guard-only trip: no idle-kick or calibration rec when unneeded")
    chk(no_ev({"event_counts": {"autopan_kick": 1, "autopan_guard": 2}},
              "PP-D-AUTOPAN-STUCK"), "no trip below both thresholds")


def t_rule_capacity():
    print("[8] rule H PP-D-CAP-SUSPECT / PP-D-CAP-HARDSTOP (contract)")
    chk(lo.CAL_BY_ID["cap_bar"]["related_diagnostics"] ==
        ["PP-D-CAP-SUSPECT", "PP-D-CAP-HARDSTOP"],
        "codes match the chunk-C contract in lite_onboarding cap_bar")
    detail = ("The saved right tip x=400 is not right of the left tip "
              "x=678 - the stored endpoints are swapped.")
    ctx = {"cal_status": {"cap_bar": {"status": "needs_review",
                                      "detail": detail}}}
    e = ev(ctx, "PP-D-CAP-SUSPECT")
    chk(e is not None, "cap_bar needs_review trips PP-D-CAP-SUSPECT")
    if e:
        chk(e["severity"] == "ERROR" and e["confidence"] == "high",
            "ERROR, high confidence")
        chk(detail in e["observed"],
            "observed quotes the stored-pair suspicion detail")
        chk(cal_targets(e) == ["cap_bar"], "calibration target cap_bar")
        chk(e["recommendations"][0].get("repair_action") ==
            "test_capacity", "repair action is test_capacity")
    e = ev({"stats": {"hard_stops": 2}}, "PP-D-CAP-HARDSTOP")
    chk(e is not None, "hard_stops >= 1 trips PP-D-CAP-HARDSTOP")
    if e:
        chk(e["severity"] == "ERROR" and e["confidence"] == "high",
            "ERROR, high confidence")
        chk("2" in e["observed"], "observed names the hard-stop count")
        chk("cap_bar" in cal_targets(e), "calibration target cap_bar")
    chk(no_ev({"cal_status": {"cap_bar": {"status": "ok"}},
               "stats": {"hard_stops": 0}}, "PP-D-CAP-SUSPECT")
        and no_ev({"stats": {"hard_stops": 0}}, "PP-D-CAP-HARDSTOP"),
        "no trip with a healthy pair and zero hard stops")


def t_rule_recovery_loop():
    print("[9] rule I PP-D-RECOVERY-LOOP")
    e = ev({"stats": {"cycles": 10, "recoveries": 5}},
           "PP-D-RECOVERY-LOOP")
    chk(e is not None, "recoveries 5/10 cycles trips")
    if e:
        chk("recoveries: 5 in 10 cycles (0.50/cycle, threshold 0.5)"
            in e["evidence"], "numeric evidence string")
        chk(target(e, "RECOVER_LIMIT")["suggested"] == 2
            and target(e, "NO_PROGRESS_SEC")["suggested"] == 7
            and target(e, "RECOVER_BACK_MS")["suggested"] == 200,
            "targets RECOVER_LIMIT 3->2, NO_PROGRESS_SEC 5->7, "
            "RECOVER_BACK_MS 160->200")
        chk(any("walk-back" in c for c in e["other_causes"])
            and any("misdetection" in c for c in e["other_causes"]),
            "other_causes name walk-back duration + water misdetection")
    e = ev({"event_counts": {"recovery_rung": 3}}, "PP-D-RECOVERY-LOOP")
    chk(e is not None
        and "recovery_rung: 3 this run (threshold 3)" in e["evidence"],
        "recovery_rung 3 trips on its own")
    chk(no_ev({"stats": {"cycles": 10, "recoveries": 4},
               "event_counts": {"recovery_rung": 2}},
              "PP-D-RECOVERY-LOOP"), "no trip below both thresholds")


def t_rule_stall():
    print("[10] rule J PP-D-STALL (cause priority)")
    stall = [{"type": "no_progress", "reason": "", "t": 1.0}]
    ctx = {"run_active": True, "recent_events": list(stall),
           "capabilities": {"screen_detection": "not_granted"}}
    e = ev(ctx, "PP-D-STALL")
    chk(e is not None, "recent no_progress + run_active trips")
    if e:
        chk(perm_targets(e) == ["screen_detection"]
            and e["confidence"] == "high",
            "a missing REQUIRED capability is the primary cause")
        chk(e["faq_id"] == "faq-mac-screen-recording",
            "stall faq follows the missing capability")
        chk(e["severity"] == "WARNING", "single stall = WARNING")
    ctx = {"run_active": True, "recent_events": list(stall),
           "capabilities": {"screen_detection": "granted"},
           "window_found": False}
    e = ev(ctx, "PP-D-STALL")
    chk(e is not None and "roblox_window" in cal_targets(e)
        and e["faq_id"] == "faq-window-detection",
        "with permissions fine, a lost Roblox window is the cause")
    ctx = {"run_active": True, "recent_events": list(stall),
           "window_found": True,
           "cal_status": {"pan_prompt": {"status": "stale"}}}
    e = ev(ctx, "PP-D-STALL")
    chk(e is not None and "pan_prompt" in cal_targets(e),
        "otherwise stale calibration is the cause")
    ctx = {"run_active": True, "recent_events": stall * 3,
           "window_found": True}
    e = ev(ctx, "PP-D-STALL")
    chk(e is not None and e["severity"] == "ERROR",
        "3 recent stalls escalate to ERROR (ladder)")
    chk(no_ev({"run_active": False, "recent_events": list(stall)},
              "PP-D-STALL"), "no trip when no run is active")
    chk(no_ev({"run_active": True, "recent_events": [
        {"type": "nudge", "reason": "", "t": 1.0}]}, "PP-D-STALL"),
        "no trip without a no_progress record")


def t_rule_finds_miss():
    print("[11] rule K PP-D-FINDS-MISS")
    ctx = {"settings": {"FINDS_TRACK": True},
           "cal_status": {"find_region": {"status": "unset"}}}
    e = ev(ctx, "PP-D-FINDS-MISS")
    chk(e is not None, "FINDS_TRACK on + uncalibrated box trips")
    if e:
        chk(e["severity"] == "NOTICE", "region-only trip is a NOTICE")
        chk("find_region" in cal_targets(e), "find_region is targeted")
        chk(abs(target(e, "FINDS_MIN_CONF")["suggested"] - 0.25) < 1e-9,
            "FINDS_MIN_CONF one step down (0.30 -> 0.25, float)")
        chk(target(e, "FINDS_CARD_SEC")["suggested"] == 6
            and target(e, "FINDS_EMPTY_MS")["suggested"] == 750,
            "FINDS_CARD_SEC 5->6, FINDS_EMPTY_MS 700->750")
    ctx = {"settings": {"FINDS_TRACK": True},
           "cal_status": {"find_region": {"status": "ok"}},
           "event_counts": {"finds_ghost": 2, "finds_fork": 1}}
    e = ev(ctx, "PP-D-FINDS-MISS")
    chk(e is not None and e["severity"] == "WARNING"
        and "finds_ghost+finds_fork: 3 this run (threshold 3)"
        in e["evidence"],
        "ghost+fork churn >= 3 trips at WARNING with numeric evidence")
    chk(no_ev({"settings": {"FINDS_TRACK": False},
               "cal_status": {"find_region": {"status": "unset"}}},
              "PP-D-FINDS-MISS"), "no trip with tracking off")
    chk(no_ev({"settings": {"FINDS_TRACK": True},
               "cal_status": {"find_region": {"status": "ok"}},
               "event_counts": {"finds_ghost": 1, "finds_fork": 1}},
              "PP-D-FINDS-MISS"), "no trip below the churn threshold")


def t_rule_analytics():
    print("[12] rule L PP-D-ANALYTICS-STALE")
    ctx = {"settings": {"EARN_TRACK": True, "EARN_OCR_SEC": 90},
           "cal_status": {"money_region": {"status": "stale"},
                          "shards_region": {"status": "ok"}}}
    e = ev(ctx, "PP-D-ANALYTICS-STALE")
    chk(e is not None, "EARN_TRACK on + stale money box trips")
    if e:
        chk(e["severity"] == "NOTICE" and e["confidence"] == "high",
            "NOTICE severity, high confidence")
        chk(cal_targets(e) == ["money_region"],
            "only the bad region is targeted")
        t = target(e, "EARN_OCR_SEC")
        chk(t is not None and t["suggested"] == 10,
            "very high EARN_OCR_SEC (90 s) draws a cadence suggestion")
    ctx["settings"]["EARN_OCR_SEC"] = 10
    ctx["cal_status"]["shards_region"] = {"status": "unset"}
    e = ev(ctx, "PP-D-ANALYTICS-STALE")
    chk(e is not None and "shards_region" in cal_targets(e)
        and target(e, "EARN_OCR_SEC") is None,
        "shards analog trips; sane cadence gets no setting target")
    chk(no_ev({"settings": {"EARN_TRACK": False},
               "cal_status": {"money_region": {"status": "unset"}}},
              "PP-D-ANALYTICS-STALE"), "no trip with tracking off")
    chk(no_ev({"settings": {"EARN_TRACK": True},
               "cal_status": {"money_region": {"status": "ok"},
                              "shards_region": {"status": "auto"}}},
              "PP-D-ANALYTICS-STALE"), "no trip with healthy regions")


def t_rule_permissions():
    print("[13] rules M PP-D-PERM-* and N PP-D-SAFESTOP")
    e = ev({"capabilities": {"screen_detection": "not_granted"}},
           "PP-D-PERM-SCREEN_DETECTION")
    chk(e is not None, "not_granted screen_detection trips")
    if e:
        chk(e["severity"] == "CRITICAL" and e["confidence"] == "high",
            "CRITICAL, high confidence")
        chk(perm_targets(e) == ["screen_detection"],
            "permission target names the capability")
        chk(e["suppressible"] is False and e["dismissible"] is False,
            "a launch blocker is neither suppressible nor dismissible")
    e = ev({"capabilities": {"input_control": "not_granted"}},
           "PP-D-PERM-INPUT_CONTROL")
    chk(e is not None and e["severity"] == "CRITICAL",
        "not_granted input_control trips CRITICAL")
    chk(no_ev({"capabilities": {"screen_detection": "granted",
                                "input_control": "granted"}},
              "PP-D-PERM-SCREEN_DETECTION"),
        "no trip when granted")
    e = ev({"capabilities": {"stop_hotkeys": "not_granted"}},
           "PP-D-SAFESTOP")
    chk(e is not None and e["severity"] == "ERROR"
        and perm_targets(e) == ["stop_hotkeys"],
        "stop_hotkeys not granted trips PP-D-SAFESTOP (ERROR)")
    codes = {x["code"] for x in
             ld.evaluate({"capabilities": {"stop_hotkeys":
                                           "not_granted"}})}
    chk("PP-D-PERM-STOP_HOTKEYS" not in codes,
        "stop_hotkeys is owned by PP-D-SAFESTOP alone (no duplicate)")
    e = ev({"capabilities": {"stop_hotkeys": "test_failed"}},
           "PP-D-SAFESTOP")
    chk(e is not None, "a failed hotkey test also trips PP-D-SAFESTOP")
    chk(no_ev({"capabilities": {"stop_hotkeys": "granted"}},
              "PP-D-SAFESTOP"), "no trip when granted")


def t_rule_build_conflict():
    print("[14] rule O PP-D-BUILD-CONFLICT")
    expects = {"no-studio-build": ("NOTICE", "studio"),
               "no-studio-script": ("NOTICE", "script"),
               "classic-with-active-build": ("WARNING", "studio"),
               "mode-kind-mismatch": ("WARNING", "studio")}
    for refusal, (sev, tab) in expects.items():
        e = ev({"launch_refusal": refusal}, "PP-D-BUILD-CONFLICT")
        chk(e is not None, "refusal '%s' trips" % refusal)
        if e:
            chk(e["severity"] == sev, "%s -> %s" % (refusal, sev))
            chk(e["deep_links"]
                and e["deep_links"][0]["tab_target"] == tab,
                "%s deep-links to the '%s' tab" % (refusal, tab))
            chk(("launch_refusal: %s" % refusal) in e["evidence"],
                "%s: evidence names the refusal" % refusal)
    chk(no_ev({"launch_refusal": "launched"}, "PP-D-BUILD-CONFLICT")
        and no_ev({"launch_refusal": None}, "PP-D-BUILD-CONFLICT"),
        "no trip on 'launched' or no refusal")


def t_rule_cal_stale():
    print("[15] rule PP-D-CAL-STALE")
    reason = ("The Roblox window is 1600x900 now but you calibrated at "
              "1800x1087. Re-run Calibrate or restore the window size.")
    ctx = {"cal_health": {"ok": False, "reason": reason},
           "cal_status": {"pan_prompt": {"status": "stale"},
                          "cap_bar": {"status": "ok"},
                          "shake_prompt": {"status": "stale"}}}
    e = ev(ctx, "PP-D-CAL-STALE")
    chk(e is not None, "cal_health not ok trips")
    if e:
        chk(e["severity"] == "WARNING" and e["confidence"] == "high",
            "WARNING, high confidence")
        chk(e["summary"] == reason,
            "the actual health reason sentence is quoted")
        chk(cal_targets(e) == ["pan_prompt", "shake_prompt"],
            "calibration targets = exactly the stale items")
    chk(no_ev({"cal_health": {"ok": True, "reason": ""}},
              "PP-D-CAL-STALE"), "no trip when health is ok")


def t_escalation_merge():
    print("[16] escalation ladder + merge_events recurrence")
    chk(ld.escalate("PP-D-CUE-PAN-MISS", 1) == "NOTICE"
        and ld.escalate("PP-D-CUE-PAN-MISS", 3) == "WARNING"
        and ld.escalate("PP-D-CUE-PAN-MISS", 6) == "ERROR",
        "cue-miss ladder: 1=NOTICE, 3=WARNING, 6=ERROR")
    chk(ld.escalate("PP-D-STALL", 2) == "WARNING"
        and ld.escalate("PP-D-STALL", 3) == "ERROR",
        "stall ladder: repeat escalates to ERROR")
    chk(ld.escalate("PP-D-NOT-A-CODE", 5) is None,
        "unknown codes return None")
    fresh = ld.evaluate({"event_counts": {"no_progress": 1},
                         "cal_status": {"pan_prompt":
                                        {"status": "stale"}}})
    fresh = [e for e in fresh if e["code"] == "PP-D-CUE-PAN-MISS"]
    prior = [dict(fresh[0], first_seen=100, last_seen=100,
                  recurrence_count=2)]
    merged = ld.merge_events(prior, fresh, 200)
    m = merged[0]
    chk(m["recurrence_count"] == 3 and m["first_seen"] == 100
        and m["last_seen"] == 200,
        "recurrence carried forward and bumped (2 -> 3)")
    chk(m["severity"] == "WARNING",
        "third occurrence escalates NOTICE -> WARNING via the ladder")
    merged = ld.merge_events(prior, [], 300)
    chk(merged == [], "resolved events (absent from fresh) are dropped")
    crit = ld.make_event("PP-D-PERM-SCREEN_DETECTION", "CRITICAL",
                         "permissions", "t", "s", "o", [], "high", [],
                         [], "faq-mac-screen-recording")
    warn = ld.make_event("PP-D-NUDGE-FAR", "WARNING", "movement", "t",
                         "s", "o", [], "high", [], [],
                         "faq-movement-nudges")
    merged = ld.merge_events([], [warn, crit], 1)
    chk(merged[0]["code"] == "PP-D-PERM-SCREEN_DETECTION",
        "sorting puts CRITICAL first")


def t_suppression():
    print("[17] suppression store")
    chk(ld.load_suppressions("/nonexistent/path/sup.json") == {},
        "loading a missing path returns {} (never raises)")
    tmpdir = tempfile.mkdtemp(prefix="ppdiag_")
    path = os.path.join(tmpdir, "suppressions.json")
    data = {"PP-D-NUDGE-FAR": {"until": None, "forever": True},
            "PP-D-SHAKE-EARLY": {"until": 5000, "forever": False}}
    chk(ld.save_suppressions(path, data) is True, "atomic save succeeds")
    chk(ld.load_suppressions(path) == data, "round-trip is exact")
    chk(not os.path.exists(path + ".tmp"), "no tmp file left behind")
    chk(ld.save_suppressions(path, "not-a-dict") is False,
        "non-dict data is refused (returns False)")
    mk = ld.make_event
    events = [
        mk("PP-D-NUDGE-FAR", "WARNING", "movement", "t", "s", "o", [],
           "high", [], [], "faq-movement-nudges"),
        mk("PP-D-SHAKE-EARLY", "WARNING", "shake", "t", "s", "o", [],
           "high", [], [], "faq-shake-timing"),
        mk("PP-D-PERM-SCREEN_DETECTION", "CRITICAL", "permissions", "t",
           "s", "o", [], "high", [], [], "faq-mac-screen-recording"),
    ]
    sup = ld.load_suppressions(path)
    kept = [e["code"] for e in ld.apply_suppressions(events, sup, 1000)]
    chk("PP-D-NUDGE-FAR" not in kept, "'forever' suppresses")
    chk("PP-D-SHAKE-EARLY" not in kept,
        "an unexpired 'until' suppresses (now 1000 < until 5000)")
    chk("PP-D-PERM-SCREEN_DETECTION" in kept,
        "CRITICAL is never suppressible")
    kept = [e["code"] for e in ld.apply_suppressions(events, sup, 9000)]
    chk("PP-D-SHAKE-EARLY" in kept,
        "an expired 'until' no longer suppresses (now 9000 > 5000)")
    unsup = mk("PP-D-SAFESTOP", "ERROR", "permissions", "t", "s", "o",
               [], "high", [], [], "faq-safe-stop-hotkeys",
               suppressible=False)
    kept = ld.apply_suppressions([unsup],
                                 {"PP-D-SAFESTOP": {"forever": True}}, 1)
    chk(len(kept) == 1, "suppressible=False events survive suppression")
    os.remove(path)
    os.rmdir(tmpdir)


def t_faq():
    print("[18] FAQ knowledge base")
    problems = ld.validate_faq()
    for p in problems:
        print("      problem: %s" % p)
    chk(problems == [], "validate_faq() passes")
    chk(len(ld.FAQ_ENTRIES) >= 18,
        "at least 18 FAQ entries (%d)" % len(ld.FAQ_ENTRIES))
    chk(all(f.get("question") and f.get("first_action")
            and f.get("steps") and f.get("verify") and f.get("platforms")
            for f in ld.FAQ_ENTRIES),
        "every entry carries question/first_action/steps/verify/platforms")


def t_global_invariants():
    print("[19] global invariants over every produced event")
    chk(ld.evaluate({}) == [], "evaluate({}) returns [] without raising")
    chk(ld.evaluate(None) == [], "evaluate(None) returns []")
    chk(ld.evaluate({"stats": None, "event_counts": None,
                     "cal_status": None, "capabilities": None,
                     "settings": None, "recent_events": None,
                     "cal_health": None}) == [],
        "all-None ctx values return [] without raising")
    chk(len(EVENTS_SEEN) > 0, "the trip tests produced events")
    bad_faq = [e["code"] for e in EVENTS_SEEN
               if e["faq_id"] not in ld.FAQ_BY_ID]
    chk(not bad_faq, "every event's faq_id resolves %s" % (bad_faq or ""))
    bad_bounds = []
    for e in EVENTS_SEEN:
        for r in e["recommendations"]:
            for t in r["setting_targets"]:
                key, sug = t["key"], t["suggested"]
                if sug is None or key not in ld.RANGES:
                    if r["auto_apply"]:
                        bad_bounds.append("%s auto_apply unbounded" % key)
                    continue
                lo, hi, _s = ld.RANGES[key]
                if not (lo <= sug <= hi):
                    bad_bounds.append("%s=%s outside [%s,%s]"
                                      % (key, sug, lo, hi))
    chk(not bad_bounds,
        "every suggested value is clamped into RANGES %s"
        % (bad_bounds or ""))
    bad_keys = [t["key"] for e in EVENTS_SEEN
                for r in e["recommendations"]
                for t in r["setting_targets"] if t["key"] not in ui.DEFAULTS]
    chk(not bad_keys, "every setting target is a real SECTIONS key")
    bad_cal = [c for e in EVENTS_SEEN for r in e["recommendations"]
               for c in r["calibration_targets"]
               if c not in ld.CALIBRATION_IDS]
    chk(not bad_cal, "every calibration target is a real registry id")
    bad_perm = [p for e in EVENTS_SEEN for r in e["recommendations"]
                for p in r["permission_targets"]
                if p not in ld.CAPABILITY_IDS]
    chk(not bad_perm, "every permission target is a real capability id")
    chk(all(e["severity"] in ld.SEVERITIES
            and e["confidence"] in ld.CONFIDENCES
            and e["observed"] and e["evidence"]
            for e in EVENTS_SEEN),
        "every event carries severity/confidence/observed/evidence")


if __name__ == "__main__":
    t_registry()
    t_clamping()
    t_rule_nudge_far()
    t_rule_shake_early()
    t_rule_shake_late()
    t_rules_cue_miss()
    t_rule_autopan()
    t_rule_capacity()
    t_rule_recovery_loop()
    t_rule_stall()
    t_rule_finds_miss()
    t_rule_analytics()
    t_rule_permissions()
    t_rule_build_conflict()
    t_rule_cal_stale()
    t_escalation_merge()
    t_suppression()
    t_faq()
    t_global_invariants()
    print("")
    if FAILS:
        print("DIAGNOSTICS TESTS: %d FAILURE(S)" % len(FAILS))
        for f in FAILS:
            print("  - " + f)
        sys.exit(1)
    print("DIAGNOSTICS TESTS: ALL PASS")
