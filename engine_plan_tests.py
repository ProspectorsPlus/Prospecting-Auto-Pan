#!/usr/bin/env python3
"""Cycle Plan tests (canonical-foundation pass, spec A deliverable 3).

Proves prospector_engine/cycleplan.py resolves the plan from the SAME
globals the Supervisor executes with, that plan.describe (PPE1 1.4) serves
it over the wire with settings.set values reflected (next-run bind), and
that the fingerprint tracks live behavior only:

  * in-process plan-vs-code pinning: the resolved constants equal the
    engine module globals (the way engine_sim loads the module);
  * mode selection follows the engine's own dispatch precedence
    (TRACKER > GEODE > TREASURE > shards > standard; SCRIPT_MODE is not
    a plan mode);
  * EASY_* additive layering surfaces as provenance "easy-layered";
  * geode overrides swap the shake attempt timeout and halve the empty
    threshold; dead knobs are marked dead and excluded from the
    fingerprint (a dead-knob edit never changes it);
  * determinism: same globals -> byte-identical JSON.

  python3 engine_plan_tests.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import engine_sim                              # noqa: E402
import engine_contract_tests as C              # noqa: E402
from prospector_engine import cycleplan        # noqa: E402

FAILS = []


def chk(cond, msg):
    print("  [%s] %s" % ("PASS" if cond else "FAIL", msg))
    if not cond:
        FAILS.append(msg)


def _fresh(alias):
    """A fresh engine module with the config seam pointed at an empty
    temp home (never the user's real file)."""
    eng = engine_sim.load_engine(alias)
    home = tempfile.mkdtemp(prefix="ppe-plan-")
    eng.CONFIG_FILE = os.path.join(home, "prospecting_config.json")
    return eng


def _canon(plan):
    return json.dumps(plan, sort_keys=True, separators=(",", ":"))


def _timing(step):
    return step.get("timing") or step.get("budget") or {}


# ---------------------------------------------------------------------------
def test_inprocess_pinning():
    """Plan constants == the module globals the Supervisor executes with."""
    print("[plan] in-process plan-vs-code pinning")
    eng = _fresh("pold_plan_pin")
    plan = cycleplan.resolve_cycle_plan(eng)
    chk(plan["_plan"] == 1, "plan.version: _plan == 1")
    chk(plan["mode"] == "standard", "plan.mode: baked globals -> standard")

    legs = plan["legs"]
    dig, water = legs["dig"], legs["water"]
    shake, settle = legs["shake"], legs["settle"]
    pins = [
        (_timing(dig["steps"][0]), "PRE_DIG_SETTLE_MS"),
        (dig["steps"][1]["click"]["timing"], "DIG_CLICK_MS"),
        (dig["steps"][1]["registered_when"]["within"], "DIG_PROBE_MS"),
        (_timing(dig["steps"][2]), "LAND_PROBE_NUDGE_MS"),
        (dig["steps"][2]["settle"], "PROBE_GAP_MS"),
        (dig["steps"][3]["wait_per_dig"], "DIG_FILL_MS"),
        (_timing(water["steps"][0]), "PAN_BACK_MAX_MS"),
        (_timing(water["steps"][1]), "WATER_EXTRA_BACK_MS"),
        (_timing(shake["steps"][0]), "SHAKE_START_DELAY_MS"),
        (_timing(shake["steps"][2]), "SHAKE_W_LEAD_MS"),
        (shake["steps"][3]["click"], "SHAKE_CLICK_MS"),
        (shake["steps"][3]["gap"], "SHAKE_CLICK_GAP_MS"),
        (shake["steps"][3]["attempt_timeout"], "SHAKE_HOLD_MS"),
        (shake["failure_layers"]["bail"]["window"], "SHAKE_BAIL_MS"),
        (_timing(settle["steps"][0]), "POST_SHAKE_SETTLE_MS"),
    ]
    for rec, key in pins:
        chk(rec.get("setting") == key
            and rec.get("resolved_ms") == getattr(eng, key),
            "plan.pin.%s: %r == module global %r"
            % (key, rec.get("resolved_ms"), getattr(eng, key)))
    counts = [
        (dig["steps"][1]["rounds"], "LAND_DIG_TRIES"),
        (dig["steps"][1]["in_place_tries"], "DIG_INPLACE_TRIES"),
        (dig["steps"][1]["registered_when"]["cap_rise_frac"],
         "CAP_RISE_FRAC"),
        (dig["steps"][3]["max_digs"], "MAX_DIGS_TO_FILL"),
        (water["steps"][0]["confirm"], "WALK_BACK_CONFIRM"),
    ]
    for rec, key in counts:
        chk(rec.get("setting") == key
            and rec.get("value") == getattr(eng, key),
            "plan.pin.%s: value == module global" % key)
    chk(shake["steps"][3]["until"]["cap_below"]["value"]
        == eng.CAP_EMPTY_FRAC,
        "plan.pin.CAP_EMPTY_FRAC: auto shake-until-empty threshold")

    # policy: the act() dispatch order (capacity-primary)
    rules = [r for r in plan["policy"] if r.get("order", 0) > 0]
    chk([r["leg"] for r in sorted(rules, key=lambda r: r["order"])]
        == ["shake", "water", "dig"],
        "plan.policy: act() order FULL+Pan->shake, FULL->water, else dig")
    chk(water["steps"][0].get("budget", {}).get("formula")
        == "PAN_BACK_MAX_MS * (1 + min(water_fails, 4))",
        "plan.water: escalating budget carried as a formula")
    chk("never a mouse hold"
        in shake["steps"][3]["attempt_timeout"]["semantics"],
        "plan.shake: SHAKE_HOLD_MS documented as per-attempt timeout, "
        "never a hold")
    chk(shake["failure_layers"]["start_confirm"]["deadline_extension_ms"]
        == 600,
        "plan.shake: retry deadline extension 0.6 s documented")

    # recovery ladder shape + resolved rungs
    rungs = {r["id"]: r for r in plan["recovery"]["rungs"]}
    chk(sorted(rungs) == ["R1", "R2", "R3", "R4", "R5"],
        "plan.recovery: five rungs R1-R5")
    chk(rungs["R1"]["trigger"]["at_least"]["value"]
        == eng.SHAKE_GLITCH_LIMIT
        and rungs["R2"]["trigger"]["no_progress_for_s"]["value"]
        == eng.NO_PROGRESS_SEC
        and rungs["R3"]["trigger"]["same_situation_ticks"]["value"]
        == eng.STUCK_TICKS
        and rungs["R3"]["limit"]["max"]["value"] == eng.RECOVER_LIMIT,
        "plan.recovery: rung triggers/limits pinned to module globals")
    pulse = rungs["R3"]["actions"][0]["by_situation"]["FULL+other"]
    chk(pulse["tap_on"]["resolved_ms"] == eng.BURST_ON_MS
        and pulse["tap_off"]["resolved_ms"] == eng.BURST_OFF_MS,
        "plan.recovery: pulse cadence BURST_ON/OFF pinned")
    r5 = [a for a in rungs["R5"]["actions"]
          if a["op"] == "safe_pause_retry"][0]
    chk(r5["retry_wait_s"]["value"] == eng.SAFE_STOP_RETRY_SEC
        and r5["max_retries"]["value"] == eng.SAFE_STOP_MAX_RETRIES,
        "plan.recovery: safe-pause retry ladder pinned")

    # background + stops + hud
    bg = plan["background"]
    chk(set(bg) == {"earn_tracker", "finds_watcher", "relic_scheduler",
                    "lag_probe"},
        "plan.background: the four classic flows (no autopan outside "
        "tracker mode)")
    chk(bg["earn_tracker"]["input_policy"] == "read_only"
        and bg["relic_scheduler"]["input_policy"]
        == "takes_input_at_tick_boundary",
        "plan.background: input policies (read-only trackers, relic at "
        "tick boundary)")
    chk(plan["stops"]["autostop"]["minutes"]["value"]
        == eng.AUTOSTOP_MINUTES
        and plan["stops"]["bag_full_guard"]["stop_after_pans"]["value"]
        == eng.STOP_AFTER_PANS,
        "plan.stops: autostop + bag-full guard pinned")
    chk(plan["hud"]["phases"] == ["dig", "water", "shake", "glide",
                                  "settle"],
        "plan.hud: phase vocabulary")
    chk(plan["hud"]["stats_keys"]
        == sorted(eng.SessionStats().as_dict().keys()),
        "plan.hud: stats keys == SessionStats.as_dict keys")

    # dead knobs
    for k in cycleplan.DEAD_KNOBS:
        chk(plan["settings"].get(k, {}).get("dead") is True,
            "plan.dead.%s: marked dead:true" % k)
    chk(plan["settings"]["SHAKE_CLICK_MS"].get("dead") is None,
        "plan.dead: live knobs are not marked dead")

    # provenance: nothing loaded -> everything default
    provs = {v["provenance"] for v in plan["settings"].values()}
    chk(provs == {"default"},
        "plan.provenance: fresh module, no config -> all default (%r)"
        % provs)

    # determinism + fingerprint self-consistency
    plan2 = cycleplan.resolve_cycle_plan(eng)
    chk(_canon(plan) == _canon(plan2),
        "plan.determinism: same globals -> byte-identical JSON")
    chk(plan["fingerprint"] == cycleplan.plan_fingerprint(plan)
        and len(plan["fingerprint"]) == 64,
        "plan.fingerprint: sha256 over the key-sorted plan minus itself")


def test_inprocess_easy_layering():
    """EASY_* offsets applied by load_config surface as easy-layered."""
    print("[plan] EASY_* layering provenance (in-process load_config)")
    eng = _fresh("pold_plan_easy")
    base_fp = cycleplan.resolve_cycle_plan(eng)["fingerprint"]
    with open(eng.CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"EASY_WATER_BACK_MS": 40, "EASY_FIRST_DIG_DELAY_MS": 25,
                   "PAN_BACK_MAX_MS": 200, "WATER_EXTRA_BACK_MS": 0,
                   "PRE_DIG_SETTLE_MS": 60, "POST_SHAKE_SETTLE_MS": 150},
                  f)
    eng.load_config()
    plan = cycleplan.resolve_cycle_plan(eng)
    s = plan["settings"]
    chk(s["PAN_BACK_MAX_MS"]["value"] == 240
        and s["PAN_BACK_MAX_MS"]["provenance"] == "easy-layered"
        and s["PAN_BACK_MAX_MS"]["easy_offset"]
        == {"setting": "EASY_WATER_BACK_MS", "value": 40},
        "plan.easy: PAN_BACK_MAX_MS = 200 + 40, provenance easy-layered "
        "with the offset surfaced")
    chk(s["WATER_EXTRA_BACK_MS"]["value"] == 40
        and s["WATER_EXTRA_BACK_MS"]["provenance"] == "easy-layered",
        "plan.easy: WATER_EXTRA_BACK_MS gets the same offset")
    chk(s["PRE_DIG_SETTLE_MS"]["value"] == 85
        and s["POST_SHAKE_SETTLE_MS"]["value"] == 175
        and s["PRE_DIG_SETTLE_MS"]["provenance"] == "easy-layered",
        "plan.easy: EASY_FIRST_DIG_DELAY_MS layers onto both settle knobs")
    chk(s["EASY_WATER_BACK_MS"]["value"] == 40
        and s["EASY_WATER_BACK_MS"]["provenance"] == "config",
        "plan.easy: the EASY offset itself is a consulted config setting")
    chk(plan["legs"]["water"]["steps"][0]["budget"]["resolved_ms"] == 240,
        "plan.easy: the water leg budget carries the layered value")
    chk(plan["fingerprint"] != base_fp,
        "plan.easy: layering changes the fingerprint")


def test_inprocess_modes():
    """Mode selection == the engine's dispatch precedence."""
    print("[plan] mode selection precedence (in-process)")
    eng = _fresh("pold_plan_modes")
    eng.SCRIPT_MODE = True
    plan = cycleplan.resolve_cycle_plan(eng)
    chk(plan["mode"] == "standard",
        "plan.mode: SCRIPT_MODE is not a plan mode (still standard)")
    eng.SHARDS_DIG_CLICKS = 1
    plan = cycleplan.resolve_cycle_plan(eng)
    chk(plan["mode"] == "shards"
        and plan["legs"]["dig"]["variant"]["name"] == "shards"
        and plan["legs"]["dig"]["variant"]["clicks"]["value"] == 1,
        "plan.mode: SHARDS_DIG_CLICKS>0 -> shards with the dig variant")
    eng.TREASURE_MODE = True
    plan = cycleplan.resolve_cycle_plan(eng)
    chk(plan["mode"] == "treasure" and "treasure" in plan["legs"],
        "plan.mode: TREASURE outranks shards")
    eng.GEODE_MODE = True
    plan = cycleplan.resolve_cycle_plan(eng)
    chk(plan["mode"] == "geode", "plan.mode: GEODE outranks treasure")
    shake = plan["legs"]["shake"]
    chk(shake["steps"][3]["attempt_timeout"]["setting"]
        == "GEODE_SHAKE_HOLD_MS"
        and shake["steps"][3]["attempt_timeout"]["resolved_ms"]
        == eng.GEODE_SHAKE_HOLD_MS,
        "plan.geode: attempt timeout swaps to GEODE_SHAKE_HOLD_MS")
    chk(shake["steps"][3]["until"]["cap_below"]["value"]
        == eng.CAP_EMPTY_FRAC * 0.5,
        "plan.geode: CAP_EMPTY_FRAC halved in the until-empty threshold")
    chk(shake["geode_overrides"]["bail_suppressed"] is True
        and shake["geode_overrides"]["sense_every_clicks"]["value"]
        == eng.GEODE_SHAKE_CHECK,
        "plan.geode: overrides block (bail suppressed, GEODE_SHAKE_CHECK)")
    chk(plan["legs"]["dig"]["variant"]["name"] == "geode",
        "plan.geode: geode dig variant")
    eng.TRACKER_MODE = True
    plan = cycleplan.resolve_cycle_plan(eng)
    chk(plan["mode"] == "tracker" and plan["legs"] == {}
        and plan["policy"][0]["leg"] == "watch_only",
        "plan.mode: TRACKER outranks everything; watch-only, no legs")
    chk("autopan_guard" in plan["background"],
        "plan.tracker: autopan_guard flow present in tracker mode only")


def test_effective_recovery_and_flows():
    """spec C: plan.describe reflects the EFFECTIVE recovery program
    (overrides / disabled / authored rungs) and authored flows."""
    print("[plan] effective recovery program + authored flows")
    eng = _fresh("pold_plan_eff")
    base = cycleplan.resolve_cycle_plan(eng)
    chk("program" not in base["recovery"]
        and "authored_flows" not in base["background"],
        "plan.eff: default mode -> no program/flow sections (plan shape "
        "unchanged)")
    chk(base["settings"]["RECOVERY_JSON"]["value"] == ""
        and base["settings"]["FLOWS_JSON"]["value"] == "",
        "plan.eff: RECOVERY_JSON/FLOWS_JSON are consulted settings")
    eng.RECOVERY_JSON = json.dumps({
        "_recovery": 1,
        "rungs": {"R1": {"enabled": False},
                  "R2": {"params": {"NO_PROGRESS_SEC": 2}}},
        "authored": [{
            "id": "kicker",
            "trigger": {"type": "no_progress", "s": 3},
            "actions": [{"id": "a1", "type": "tap_key",
                         "params": {"key": "8", "hold_ms": 40},
                         "children": []}],
            "resume": "restart_cycle", "cooldown_s": 5, "limit": 2,
            "max_ms": 4000}],
    })
    eng.FLOWS_JSON = json.dumps({
        "_flows": 1,
        "flows": [{
            "id": "poller",
            "trigger": {"type": "interval", "every_s": 5},
            "priority": 2, "input": "none",
            "body": [{"id": "b1", "type": "log",
                      "params": {"message": "hi"}, "children": []}],
            "max_ms": 3000}],
    })
    plan = cycleplan.resolve_cycle_plan(eng)
    prog = plan["recovery"].get("program")
    chk(prog == {"overrides": {"NO_PROGRESS_SEC": 2}, "disabled": ["R1"],
                 "authored": ["kicker"]},
        "plan.eff: program summary {overrides, disabled, authored} (%r)"
        % (prog,))
    rungs = {r["id"]: r for r in plan["recovery"]["rungs"]}
    chk(rungs["R1"].get("disabled") is True,
        "plan.eff: R1 marked disabled")
    chk(rungs["R2"].get("overrides") == {"NO_PROGRESS_SEC": 2},
        "plan.eff: R2 carries its override")
    k = rungs.get("kicker")
    chk(bool(k) and k["authored"] is True and k["enabled"] is True
        and k["trigger"] == {"type": "no_progress", "s": 3}
        and k["actions"] == {"ir_blocks": 1, "version": 2, "max_ms": 4000}
        and k["resume"] == "restart_cycle"
        and k["cooldown_s"] == 5 and k["limit"] == 2,
        "plan.eff: authored rung fully described (%r)" % (k,))
    fl = plan["background"].get("authored_flows")
    chk(isinstance(fl, list) and len(fl) == 1 and fl[0]["id"] == "poller"
        and fl[0]["input"] == "none" and fl[0]["priority"] == 2
        and fl[0]["body"] == {"ir_blocks": 1, "version": 2,
                              "max_ms": 3000},
        "plan.eff: authored flow described in background (%r)" % (fl,))
    chk(plan["fingerprint"] != base["fingerprint"],
        "plan.eff: configuring a program changes the fingerprint")
    plan2 = cycleplan.resolve_cycle_plan(eng)
    chk(_canon(plan) == _canon(plan2),
        "plan.eff: deterministic with a program configured")
    eng.RECOVERY_JSON = "{not json"
    bad = cycleplan.resolve_cycle_plan(eng)
    chk(bad["recovery"]["program"] == {
            "error": "RECOVERY_JSON is not valid JSON"},
        "plan.eff: invalid program surfaces a deterministic error")


def test_wire_effective_program():
    """plan.describe over PPE1 reflects a pushed program (next-run bind)."""
    print("[plan] plan.describe reflects RECOVERY_JSON/FLOWS_JSON (wire)")
    cli = C._make_client("idle-command").spawn()
    try:
        chk(cli.wait_ready(), "wire-eff: engine ready")
        rec = json.dumps({"_recovery": 1,
                          "rungs": {"R2": {"params":
                                           {"NO_PROGRESS_SEC": 2}}}})
        flw = json.dumps({"_flows": 1, "flows": [{
            "id": "poller",
            "trigger": {"type": "interval", "every_s": 5},
            "input": "none",
            "body": [{"id": "b1", "type": "log",
                      "params": {"message": "hi"}, "children": []}]}]})
        a = cli.request("settings.set", {"values": {"RECOVERY_JSON": rec,
                                                    "FLOWS_JSON": flw}})
        chk(a.get("ok") is True,
            "wire-eff: RECOVERY_JSON/FLOWS_JSON are settings keys")
        p = cli.request("plan.describe")["result"]["plan"]
        chk(p["recovery"].get("program", {}).get("overrides")
            == {"NO_PROGRESS_SEC": 2},
            "wire-eff: plan.describe reflects the rung override")
        fl = p["background"].get("authored_flows")
        chk(isinstance(fl, list) and fl and fl[0]["id"] == "poller",
            "wire-eff: plan.describe lists the authored flow")
        cli.shutdown()
    finally:
        cli.kill()


def test_wire_plan_describe():
    """plan.describe over PPE1: settings.set values are reflected (the
    next-run bind), fingerprint tracks live knobs only."""
    print("[plan] plan.describe over the wire (idle-command)")
    cli = C._make_client("idle-command").spawn()
    try:
        chk(cli.wait_ready(), "wire: engine ready")
        chk(cli.hello["protocol"]["minor"] >= 4,
            "wire: hello protocol minor is >= 4 (1.4 adds plan.describe; "
            "1.5 adds durable instance identity)")
        a = cli.request("plan.describe")
        chk(a.get("ok") is True and a["result"]["plan"]["_plan"] == 1,
            "wire: plan.describe acked with a _plan:1 document")
        p0 = a["result"]["plan"]
        chk(p0["mode"] == "standard",
            "wire: idle-command config (SCRIPT_MODE on) still resolves "
            "standard -- scripts are not a plan mode")

        a = cli.request("settings.set", {"values": {
            "SHAKE_CLICK_MS": 21, "DIG_CLICK_MS": 80,
            "EASY_WATER_BACK_MS": 40}})
        chk(a.get("ok") is True, "wire: settings.set live knobs acked")
        a = cli.request("plan.describe")
        p1 = a["result"]["plan"]
        sh = p1["legs"]["shake"]["steps"][3]
        chk(sh["click"]["resolved_ms"] == 21
            and p1["settings"]["SHAKE_CLICK_MS"]["provenance"] == "config",
            "wire: SHAKE_CLICK_MS=21 resolved with provenance config "
            "(idle plan == next-run bind)")
        chk(p1["legs"]["dig"]["steps"][1]["click"]["timing"]["resolved_ms"]
            == 80,
            "wire: DIG_CLICK_MS=80 resolved in the dig leg")
        chk(p1["settings"]["PAN_BACK_MAX_MS"]["value"] == 240
            and p1["settings"]["PAN_BACK_MAX_MS"]["provenance"]
            == "easy-layered",
            "wire: EASY_WATER_BACK_MS=40 layers PAN_BACK_MAX_MS to 240")
        chk(p1["fingerprint"] != p0["fingerprint"],
            "wire: live-knob change changed the fingerprint")

        b = cli.request("plan.describe")
        chk(b["result"]["plan"]["fingerprint"] == p1["fingerprint"]
            and json.dumps(b["result"]["plan"], sort_keys=True)
            == json.dumps(p1, sort_keys=True),
            "wire: fingerprint (and whole plan) stable across two calls")

        a = cli.request("settings.set", {"values": {"SHAKE_POLL_MS": 99}})
        chk(a.get("ok") is True, "wire: settings.set dead knob acked")
        p2 = cli.request("plan.describe")["result"]["plan"]
        chk(p2["settings"]["SHAKE_POLL_MS"]["value"] == 99
            and p2["settings"]["SHAKE_POLL_MS"]["dead"] is True,
            "wire: dead knob value visible and marked dead:true")
        chk(p2["fingerprint"] == p1["fingerprint"],
            "wire: dead-knob change did NOT change the fingerprint")

        a = cli.request("settings.set", {"values": {"GEODE_MODE": True}})
        p3 = cli.request("plan.describe")["result"]["plan"]
        chk(a.get("ok") is True and p3["mode"] == "geode"
            and p3["legs"]["shake"]["steps"][3]["attempt_timeout"]
            ["setting"] == "GEODE_SHAKE_HOLD_MS",
            "wire: GEODE_MODE=true -> geode plan with the geode shake "
            "timeout")
        a = cli.request("settings.set", {"values": {"TRACKER_MODE": True}})
        p4 = cli.request("plan.describe")["result"]["plan"]
        chk(a.get("ok") is True and p4["mode"] == "tracker",
            "wire: TRACKER_MODE outranks geode in the plan mode")
        cli.shutdown()
    finally:
        cli.kill()


if __name__ == "__main__":
    test_inprocess_pinning()
    test_inprocess_easy_layering()
    test_inprocess_modes()
    test_effective_recovery_and_flows()
    test_wire_plan_describe()
    test_wire_effective_program()
    print()
    if FAILS:
        print("PLAN TESTS: %d FAILURES" % len(FAILS))
        sys.exit(1)
    print("PLAN TESTS: ALL PASS")
