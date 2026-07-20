#!/usr/bin/env python3
"""Background-flow + recovery-program tests (canonical-foundation pass,
spec C deliverable 4). Sim-based: every behavior test runs the REAL engine
main() under the engine_sim world with FLOWS_JSON / RECOVERY_JSON pushed
through the normal config path.

Coverage:
  * interval read-only flow ticks DURING a classic run -- flow emissions
    interleave with the supervisor's phase stream mid-cycle;
  * exclusive flow at the tick boundary: input only at the boundary, all
    keys released before/after, supervisor re-senses next tick;
  * priority + every on_conflict policy (queue, skip, cancel_lower, fail);
  * input:"none" flow with an input node refused at validation;
  * flows suspended during recovery + safe-pause; canceled + released on
    stop; pause/resume semantics; flow.state lifecycle sequences;
  * authored recovery rung: trigger fires, authored IR actions execute,
    resume honored, safety.event recovery_rung emitted, limit + cooldown;
  * builtin rung disable + parameter override vs default;
  * manual verbs recovery.trigger / flow.trigger (in-process seam + PPE1).

  python3 engine_flow_tests.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import engine_sim                          # noqa: E402
import engine_contract_tests as C          # noqa: E402

FAILS = []


def chk(cond, msg):
    print("  [%s] %s" % ("PASS" if cond else "FAIL", msg))
    if not cond:
        FAILS.append(msg)


# ---- scenario builders ------------------------------------------------------

def classic_scen(name, config, quit_ms=5000):
    """The classic-standard timeline (one full healthy cycle then idle
    water retries) with extra config keys."""
    cfg = {"RELICS_ENABLED": False, "WEBHOOK_URL": "", "WEBHOOK_SECRET": ""}
    cfg.update(config)
    return {
        "name": name,
        "window": [0, 0, 1440, 900],
        "config": cfg,
        "schedule": [[0, "toggle"], [quit_ms, "quit"]],
        "cues": {"PAN": [[550, 2350]]},
        "capacity": [[0, 0.0], [250, 0.0], [450, 1.0], [900, 1.0],
                     [1900, 0.0], [2450, 0.0], [2650, 1.0], [600000, 1.0]],
        "screen": [{"rect": [0, 0, 1, 1], "rgb": [16, 16, 16],
                    "from_ms": 0}],
        "duration_ms": 600000,
    }


def stuck_scen(name, config, quit_ms=12000, schedule=None):
    """Permanently FULL on land (Deposit cue held, capacity pinned) --
    go_water can never reach the Pan cue; nothing ever progresses."""
    cfg = {"RELICS_ENABLED": False, "WEBHOOK_URL": "", "WEBHOOK_SECRET": "",
           "BREAKOUT_ENABLED": False, "RECOVER_ENABLED": False}
    cfg.update(config)
    return {
        "name": name,
        "window": [0, 0, 1440, 900],
        "config": cfg,
        "schedule": schedule or [[0, "toggle"], [quit_ms, "quit"]],
        "cues": {"DEPOSIT": [[0, 600000]]},
        "capacity": [[0, 1.0], [600000, 1.0]],
        "screen": [{"rect": [0, 0, 1, 1], "rgb": [16, 16, 16],
                    "from_ms": 0}],
        "duration_ms": 600000,
    }


def run_scen(scen, alias, extra_ops=None):
    """Run one inline scenario through the REAL engine main(). extra_ops:
    [(ms, fn(po))] scheduled on the virtual clock (manual-verb seam)."""
    path = os.path.join(tempfile.mkdtemp(prefix="ppe-flow-"),
                        scen["name"] + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(scen, f)
    loaded = engine_sim.load_scenario(path)
    po = engine_sim.load_engine(alias)
    world = engine_sim.World(po, loaded)
    for ms, fn in (extra_ops or []):
        world.clock.schedule(ms, lambda fn=fn: fn(po))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        po.main()
    return buf.getvalue(), world, po


# ---- transcript algebra -----------------------------------------------------

def flow_events(transcript):
    out = []
    for i, line in enumerate(transcript.splitlines()):
        if line.startswith("__FLOW__ "):
            try:
                out.append((i, json.loads(line[len("__FLOW__ "):])))
            except ValueError:
                pass
    return out


def events(transcript, etype=None):
    out = []
    for i, line in enumerate(transcript.splitlines()):
        if line.startswith("__EVENT__ "):
            try:
                rec = json.loads(line[len("__EVENT__ "):])
            except ValueError:
                continue
            if etype is None or rec.get("type") == etype:
                out.append((i, rec))
    return out


def stats_lines(transcript):
    out = []
    for line in transcript.splitlines():
        if line.startswith("__STATS__ "):
            try:
                out.append(json.loads(line[len("__STATS__ "):]))
            except ValueError:
                pass
    return out


def line_index(transcript, needle, start=0):
    for i, line in enumerate(transcript.splitlines()):
        if i >= start and needle in line:
            return i
    return -1


def states_of(transcript, fid):
    return [p.get("state") for _i, p in flow_events(transcript)
            if p.get("id") == fid]


def key_pairs_for(world, code):
    pairs, open_t = [], None
    for t, kind, arg in world.inputs.events:
        if kind == "key_down" and arg == code:
            open_t = t
        elif kind == "key_up" and arg == code and open_t is not None:
            pairs.append((open_t, t))
            open_t = None
    return pairs


# ---- flow / recovery document builders -------------------------------------

def flows_doc(*flows):
    return json.dumps({"_flows": 1, "flows": list(flows)})


def rec_doc(rungs=None, authored=None):
    doc = {"_recovery": 1}
    if rungs:
        doc["rungs"] = rungs
    if authored:
        doc["authored"] = authored
    return json.dumps(doc)


COUNTER_FLOW = {
    "id": "senser", "trigger": {"type": "interval", "every_s": 1},
    "priority": 0, "input": "none",
    "variables": [{"name": "n", "type": "number", "initial": 0}],
    "body": [
        {"id": "f1", "type": "set_var",
         "params": {"name": "n", "value": {"$expr": {
             "op": "add", "args": [{"var": "n"}, {"lit": 1}]}}},
         "children": []},
        {"id": "f2", "type": "log", "params": {"message": "flow-tick"},
         "children": []},
        {"id": "f3", "type": "hud_text", "params": {"text": "bg"},
         "children": []},
    ],
    "max_ms": 5000,
}


def tap_flow(fid, key, trigger, priority=0, policy="queue", body_extra=None):
    body = [{"id": "%s_t" % fid, "type": "tap_key",
             "params": {"key": key, "hold_ms": 40}, "children": []}]
    body += (body_extra or [])
    return {"id": fid, "trigger": trigger, "priority": priority,
            "input": {"exclusive": {"on_conflict": policy}},
            "body": body, "max_ms": 8000}


def wait_flow(fid, trigger, priority=0, policy="queue", waits=5,
              wait_ms=300, exclusive=True):
    body = [{"id": "%s_w%d" % (fid, i), "type": "wait",
             "params": {"ms": wait_ms}, "children": []}
            for i in range(waits)]
    inp = {"exclusive": {"on_conflict": policy}} if exclusive else "none"
    return {"id": fid, "trigger": trigger, "priority": priority,
            "input": inp, "body": body, "max_ms": 30000}


# ---------------------------------------------------------------------------
def test_readonly_interleave():
    print("[flows] read-only interval flow interleaves with the cycle")
    scen = classic_scen("flow-interleave",
                        {"FLOWS_JSON": flows_doc(COUNTER_FLOW)})
    t, world, po = run_scen(scen, "pfl_inter")
    chk(world.inputs.all_released(), "interleave: all inputs released")
    sts = states_of(t, "senser")
    chk(sts.count("started") >= 2 and sts.count("finished") >= 1,
        "interleave: interval flow fired at least twice, completing at "
        "least once before quit (%r)" % sts[:8])
    chk(sts[0] == "started" and "finished" in sts,
        "interleave: lifecycle started -> finished")
    # interleaving: a flow emission sits strictly INSIDE the supervisor's
    # phase stream (after the first dig leg, before a later phase line)
    lines = t.splitlines()
    first_dig = line_index(t, "__PHASE__ dig")
    flow_line = line_index(t, "flow-tick", start=first_dig)
    later_phase = -1
    for i in range(flow_line + 1, len(lines)):
        if lines[i].startswith("__PHASE__ "):
            later_phase = i
            break
    chk(0 <= first_dig < flow_line < later_phase,
        "interleave: flow log line between phase lines mid-run "
        "(dig@%d < flow@%d < phase@%d)" % (first_dig, flow_line,
                                           later_phase))
    chk("__SCRIPTHUD__" in t,
        "interleave: hud_text from the flow body reached the HUD seam")
    # zero input from the read-only flow: every key press is a supervisor
    # movement key (key_ups include the release_all floor -- ignore them)
    used = {arg for _t, kind, arg in world.inputs.events
            if kind == "key_down"}
    chk(used <= {po.KEY_W, po.KEY_S, po.KEY_A, po.KEY_D},
        "interleave: read-only flow pressed no keys (used=%r)" % used)
    meta = [s for s in stats_lines(t) if "flows" in s]
    chk(bool(meta) and "senser" in meta[-1]["flows"],
        "interleave: stats meta.flows carries the flow state (%r)"
        % (meta[-1]["flows"] if meta else None))
    chk(bool(meta) and meta[-1].get("recovery", "missing") is None,
        "interleave: stats meta.recovery is null outside recovery")
    rid = meta[-1].get("run_id") if meta else ""
    chk(isinstance(rid, str) and rid.startswith("r") and rid.count("-") == 2,
        "interleave: stats meta.run_id is r<epoch>-<pid>-<n> (%r)" % rid)


def test_input_none_refused():
    print("[flows] input:none flow with an input node is refused")
    po = engine_sim.load_engine("pfl_refuse")
    from prospector_engine import flows as flows_mod
    bad = {"id": "sneaky", "trigger": {"type": "interval", "every_s": 5},
           "input": "none",
           "body": [{"id": "b", "type": "tap_key",
                     "params": {"key": "1", "hold_ms": 40}, "children": []}]}
    got, err = flows_mod.parse_flows(po, flows_doc(bad))
    chk(got is None and "input" in (err or ""),
        "refuse: tap_key inside input:none -> validation error (%r)" % err)
    bad2 = dict(bad)
    bad2["body"] = [{"id": "b", "type": "wait_cue",
                     "params": {"cue": "pan", "hold": "S",
                                "timeout_ms": 1000}, "children": []}]
    got, err = flows_mod.parse_flows(po, flows_doc(bad2))
    chk(got is None and "wait_cue" in (err or ""),
        "refuse: wait_cue with a movement hold refused in input:none")
    ok_flow = dict(bad)
    ok_flow["body"] = [{"id": "b", "type": "wait",
                        "params": {"ms": 500}, "children": []}]
    got, err = flows_mod.parse_flows(po, flows_doc(ok_flow))
    chk(err is None and len(got) == 1,
        "refuse: the same flow without input nodes validates")
    # engine-level: an invalid FLOWS_JSON is rejected loudly, run continues
    scen = classic_scen("flow-badjson",
                        {"FLOWS_JSON": flows_doc(bad)}, quit_ms=1500)
    t, world, _po = run_scen(scen, "pfl_badrun")
    chk("[flows] rejected" in t and "__PHASE__ dig" in t,
        "refuse: engine logs the rejection and the classic run proceeds")


def test_exclusive_boundary():
    print("[flows] exclusive flow acts only at the tick boundary")
    fl = tap_flow("slotter", "9", {"type": "timer", "at_s": 1})
    scen = classic_scen("flow-exclusive", {"FLOWS_JSON": flows_doc(fl)})
    t, world, po = run_scen(scen, "pfl_excl")
    chk(world.inputs.all_released(), "exclusive: all inputs released")
    slot9 = po.SLOT_KEYCODES[9]
    pairs = key_pairs_for(world, slot9)
    chk(len(pairs) == 1, "exclusive: exactly one slot-9 tap (%d)"
        % len(pairs))
    sts = states_of(t, "slotter")
    chk(sts == ["started", "finished"],
        "exclusive: lifecycle started -> finished (%r)" % sts)
    # nothing else held while the flow owned input (released before/after)
    if pairs:
        d, u = pairs[0]
        held = set()
        mouse = False
        for tt, kind, arg in world.inputs.events:
            if tt >= d:
                break
            if kind == "key_down":
                held.add(arg)
            elif kind == "key_up":
                held.discard(arg)
            elif kind == "mouse_down":
                mouse = True
            elif kind == "mouse_up":
                mouse = False
        chk(not held and not mouse,
            "exclusive: all keys/mouse released BEFORE the flow's input "
            "(held=%r mouse=%r)" % (held, mouse))
    # the supervisor kept running afterwards (re-senses next tick)
    after = [ln for ln in t.splitlines()[line_index(t, "slot"):]
             if "cue[" in ln]
    chk(bool(pairs) and bool(after),
        "exclusive: supervisor decision ticks continue after the flow")


def test_priority_policies():
    print("[flows] priority + on_conflict policies")
    both = {"type": "timer", "at_s": 1}
    # queue: both run, higher priority first
    a = tap_flow("lowq", "8", both, priority=1, policy="queue")
    b = tap_flow("highq", "9", both, priority=5, policy="queue")
    scen = classic_scen("flow-queue", {"FLOWS_JSON": flows_doc(a, b)})
    t, world, po = run_scen(scen, "pfl_queue")
    fe = [(p["id"], p["state"]) for _i, p in flow_events(t)]
    started = [fid for fid, st in fe if st == "started"]
    chk(started[:2] == ["highq", "lowq"],
        "queue: both due -> higher priority first, lower queued after "
        "(%r)" % started[:4])
    chk(("lowq", "finished") in fe and ("highq", "finished") in fe,
        "queue: both flows ran to completion")
    chk(len(key_pairs_for(world, po.SLOT_KEYCODES[9])) == 1
        and len(key_pairs_for(world, po.SLOT_KEYCODES[8])) == 1,
        "queue: both taps landed")
    # skip: the lower-priority conflicting firing is dropped
    a = tap_flow("lows", "8", both, priority=1, policy="skip")
    b = tap_flow("highs", "9", both, priority=5, policy="queue")
    scen = classic_scen("flow-skip", {"FLOWS_JSON": flows_doc(a, b)})
    t, world, po = run_scen(scen, "pfl_skip")
    sts = states_of(t, "lows")
    chk(sts == ["skipped"],
        "skip: conflicting firing dropped with flow.state skipped (%r)"
        % sts)
    chk(not key_pairs_for(world, po.SLOT_KEYCODES[8]),
        "skip: the skipped flow never touched input")
    # fail: the conflicting flow is marked error and never runs again
    a = tap_flow("lowf", "8", both, priority=1, policy="fail")
    b = tap_flow("highf", "9", both, priority=5, policy="queue")
    scen = classic_scen("flow-fail", {"FLOWS_JSON": flows_doc(a, b)})
    t, world, po = run_scen(scen, "pfl_fail")
    sts = states_of(t, "lowf")
    chk(sts == ["failed"],
        "fail: conflict -> flow.state failed (%r)" % sts)
    meta = [s for s in stats_lines(t) if "flows" in s]
    chk(bool(meta) and meta[-1]["flows"].get("lowf") == "error",
        "fail: meta.flows shows the errored flow")
    # cancel_lower: a higher-priority arrival cancels the running flow
    low = wait_flow("slowpoke", {"type": "timer", "at_s": 1}, priority=1,
                    policy="queue", waits=8, wait_ms=300)
    hi = tap_flow("urgent", "9", {"type": "timer", "at_s": 2}, priority=9,
                  policy="cancel_lower")
    scen = classic_scen("flow-cancel", {"FLOWS_JSON": flows_doc(low, hi)},
                        quit_ms=7000)
    t, world, po = run_scen(scen, "pfl_cancel")
    chk(states_of(t, "slowpoke") == ["started", "canceled"],
        "cancel_lower: running lower flow canceled mid-body (%r)"
        % states_of(t, "slowpoke"))
    chk(states_of(t, "urgent") == ["started", "finished"]
        and len(key_pairs_for(world, po.SLOT_KEYCODES[9])) == 1,
        "cancel_lower: the higher flow ran to completion")


def test_pause_stop_semantics():
    print("[flows] pause suspends, stop cancels + releases")
    fl = wait_flow("longbody", {"type": "timer", "at_s": 1}, waits=5,
                   wait_ms=300, exclusive=False)
    scen = classic_scen("flow-pause", {"FLOWS_JSON": flows_doc(fl)},
                        quit_ms=12000)
    scen["schedule"] = [[0, "toggle"], [2000, "pause"], [3000, "pause"],
                        [12000, "quit"]]
    t, world, _po = run_scen(scen, "pfl_pause")
    sts = states_of(t, "longbody")
    chk(sts[:1] == ["started"] and "paused" in sts and "resumed" in sts,
        "pause: flow.state paused + resumed around the engine pause (%r)"
        % sts)
    chk(sts.index("paused") < sts.index("resumed"),
        "pause: paused precedes resumed")
    chk(sts[-1] == "finished", "pause: flow completed after resume (%r)"
        % sts[-1:])
    # stop mid-body cancels + releases
    fl = wait_flow("stopped_flow", {"type": "timer", "at_s": 1}, waits=20,
                   wait_ms=300, exclusive=True)
    scen = classic_scen("flow-stop", {"FLOWS_JSON": flows_doc(fl)},
                        quit_ms=9000)
    scen["schedule"] = [[0, "toggle"], [2000, "toggle"], [4000, "quit"]]
    t, world, _po = run_scen(scen, "pfl_stop")
    sts = states_of(t, "stopped_flow")
    chk(sts == ["started", "canceled"],
        "stop: in-flight flow canceled on the stop edge (%r)" % sts)
    chk(world.inputs.all_released(), "stop: all inputs released")


def test_flows_suspended_during_recovery_and_safe_pause():
    print("[flows] flows suspended during recovery + safe-pause")
    rung = {"id": "unstick", "trigger": {"type": "no_progress", "s": 2},
            "actions": [{"id": "a1", "type": "tap_key",
                         "params": {"key": "8", "hold_ms": 40},
                         "children": []}],
            "resume": "retry_tick", "cooldown_s": 30, "limit": 1,
            "max_ms": 5000}
    # the rung (no_progress s=2, strict >) and the flow (interval 3 s)
    # are both due at the ~3 s boundary; recovery is polled first and the
    # flow must be deferred to the NEXT boundary
    fl = tap_flow("bgtap", "9", {"type": "interval", "every_s": 3})
    scen = stuck_scen("flow-recovery",
                      {"RECOVERY_JSON": rec_doc(authored=[rung]),
                       "FLOWS_JSON": flows_doc(fl)}, quit_ms=8000)
    t, world, po = run_scen(scen, "pfl_rec")
    rungs = events(t, "recovery_rung")
    chk(len(rungs) == 1 and rungs[0][1].get("id") == "unstick"
        and rungs[0][1].get("authored") is True,
        "suspend: authored rung fired once with recovery_rung "
        "{id, authored:true}")
    fstarts = [i for i, p in flow_events(t)
               if p.get("id") == "bgtap" and p.get("state") == "started"]
    chk(bool(rungs) and bool(fstarts) and fstarts[0] > rungs[0][0],
        "suspend: the flow due at the same boundary ran only AFTER the "
        "recovery rung (rung@%d < flow@%d)"
        % (rungs[0][0] if rungs else -1, fstarts[0] if fstarts else -1))
    between = t.splitlines()[rungs[0][0] + 1:fstarts[0]] if rungs and \
        fstarts else []
    chk(any("cue[" in ln for ln in between),
        "suspend: a supervisor tick separates the rung from the flow "
        "(deferred to the NEXT boundary)")
    # safe-pause: flows must not fire inside the safe-pause window
    fl = COUNTER_FLOW
    scen = classic_scen("flow-safepause",
                        {"FLOWS_JSON": flows_doc(fl),
                         "SAFE_STOP_RETRY": True,
                         "SAFE_STOP_RETRY_SEC": 3,
                         "SR_RECOVERY": False, "FR_RECOVERY": False},
                        quit_ms=9000)
    scen["schedule"] = [[0, "toggle"], [2500, "soft"], [9000, "quit"]]
    t, world, _po = run_scen(scen, "pfl_sp")
    pi = line_index(t, "SAFE PAUSE")
    ri = line_index(t, "cue[", start=pi)     # first tick after the pause
    chk(pi >= 0 and ri > pi,
        "safe-pause: pause window found (lines %d..%d)" % (pi, ri))
    inside = [i for i, p in flow_events(t) if pi < i < ri]
    chk(not inside,
        "safe-pause: zero flow.state emissions inside the safe-pause "
        "window")
    after = [i for i, p in flow_events(t) if i > ri]
    chk(bool(after), "safe-pause: flows resume firing after the retry")


def test_authored_rung_limit_cooldown_resume():
    print("[recovery] authored rung: trigger, actions, cooldown, limit")
    rung = {"id": "kicker", "trigger": {"type": "no_progress", "s": 2},
            "actions": [{"id": "a1", "type": "tap_key",
                         "params": {"key": "8", "hold_ms": 40},
                         "children": []}],
            "resume": "retry_tick", "cooldown_s": 4, "limit": 2,
            "max_ms": 5000}
    scen = stuck_scen("rung-basic",
                      {"RECOVERY_JSON": rec_doc(authored=[rung])},
                      quit_ms=14000)
    t, world, po = run_scen(scen, "prc_basic")
    chk(world.inputs.all_released(), "rung: all inputs released")
    pairs = key_pairs_for(world, po.SLOT_KEYCODES[8])
    chk(len(pairs) == 2,
        "rung: limit=2 -> the authored action ran exactly twice (%d)"
        % len(pairs))
    if len(pairs) == 2:
        gap = pairs[1][0] - pairs[0][1]
        chk(gap >= 4000,
            "rung: cooldown_s=4 respected between fires (gap=%dms)" % gap)
    rungs = events(t, "recovery_rung")
    chk(len(rungs) == 2
        and all(r.get("id") == "kicker" and r.get("authored") is True
                and r.get("action") == "ir_actions" for _i, r in rungs),
        "rung: two recovery_rung events {id, authored:true, action}")
    # resume: safe_stop escalation from an authored rung
    rung2 = {"id": "stopper", "trigger": {"type": "no_progress", "s": 2},
             "actions": [{"id": "a1", "type": "wait",
                          "params": {"ms": 200}, "children": []}],
             "resume": "safe_stop", "cooldown_s": 60, "limit": 1,
             "max_ms": 5000}
    scen = stuck_scen("rung-safestop",
                      {"RECOVERY_JSON": rec_doc(authored=[rung2]),
                       "SAFE_STOP_RETRY": False,
                       "SR_RECOVERY": False, "FR_RECOVERY": False},
                      quit_ms=10000)
    t, world, _po = run_scen(scen, "prc_stop")
    ss = events(t, "safe_stop")
    chk(any("recovery rung stopper" in r.get("reason", "")
            for _i, r in ss),
        "rung: resume=safe_stop routes through safe_stop with the rung "
        "reason")
    chk("HARD STOP" in t,
        "rung: retry disabled -> the safe stop hard-stops (funnel intact)")


def test_rung_override_and_disable():
    print("[recovery] builtin rung parameter override + disable")
    # default: R2 (no-progress fast path) fires at NO_PROGRESS_SEC=5
    base = {"BREAKOUT_ENABLED": True, "RECOVER_ENABLED": False,
            "STUCK_TICKS": 9999}
    scen = stuck_scen("rung-default", dict(base), quit_ms=9000)
    t, _w, _p = run_scen(scen, "prc_dflt")
    d_np = events(t, "no_progress")
    chk(bool(d_np) and d_np[0][1]["t"] >= 5.0,
        "override: default R2 fires at NO_PROGRESS_SEC=5 (t=%r)"
        % (d_np[0][1]["t"] if d_np else None))
    # override: NO_PROGRESS_SEC=2 through the program (config untouched)
    cfg = dict(base)
    cfg["RECOVERY_JSON"] = rec_doc(
        rungs={"R2": {"params": {"NO_PROGRESS_SEC": 2}}})
    scen = stuck_scen("rung-override", cfg, quit_ms=9000)
    t, _w, _p = run_scen(scen, "prc_ovr")
    o_np = events(t, "no_progress")
    chk(bool(o_np) and 2.0 <= o_np[0][1]["t"] < 5.0,
        "override: R2 param NO_PROGRESS_SEC=2 fires earlier (t=%r)"
        % (o_np[0][1]["t"] if o_np else None))
    chk(bool(o_np) and "for 2s" in o_np[0][1]["reason"],
        "override: the event narrates the overridden threshold")
    # disable: R2 off -> no no_progress events at all
    cfg = dict(base)
    cfg["RECOVERY_JSON"] = rec_doc(rungs={"R2": {"enabled": False}})
    scen = stuck_scen("rung-disable", cfg, quit_ms=9000)
    t, _w, _p = run_scen(scen, "prc_dis")
    chk(not events(t, "no_progress"),
        "disable: R2 disabled -> zero no_progress events")
    # validation: R5 cannot be disabled; unknown params refused
    po = engine_sim.load_engine("prc_val")
    from prospector_engine import recovery as rec_mod
    _p2, err = rec_mod.parse_program(
        po, rec_doc(rungs={"R5": {"enabled": False}}))
    chk(_p2 is None and "R5" in (err or ""),
        "validate: disabling the safe-stop rung R5 is refused (%r)" % err)
    _p3, err = rec_mod.parse_program(
        po, rec_doc(rungs={"R1": {"params": {"DIG_CLICK_MS": 5}}}))
    chk(_p3 is None and "cannot override" in (err or ""),
        "validate: a rung may only override its own vocabulary (%r)" % err)


def test_manual_triggers_inprocess():
    print("[manual] in-process manual seams (the verb handlers' target)")
    fl = tap_flow("mflow", "9", {"type": "manual"})
    scen = classic_scen("manual-flow",
                        {"FLOWS_JSON": flows_doc(fl),
                         "SAFE_STOP_RETRY": False,
                         "SR_RECOVERY": False, "FR_RECOVERY": False},
                        quit_ms=6000)
    results = {}

    def fire_flow(po):
        results["flow"] = po.flow_manual_trigger("mflow")
        results["flow_bad"] = po.flow_manual_trigger("nope")

    def fire_r5(po):
        results["rung"] = po.recovery_manual_trigger("R5")
        results["rung_bad"] = po.recovery_manual_trigger("bogus")

    t, world, po = run_scen(scen, "pman_1",
                            extra_ops=[(1500, fire_flow), (3500, fire_r5)])
    chk(results.get("flow") is True and results.get("flow_bad") is False,
        "manual: flow_manual_trigger validates the id")
    chk(results.get("rung") is True and results.get("rung_bad") is False,
        "manual: recovery_manual_trigger validates the id")
    chk(states_of(t, "mflow") == ["started", "finished"]
        and len(key_pairs_for(world, po.SLOT_KEYCODES[9])) == 1,
        "manual: flow.trigger fired a manual-trigger flow exactly once")
    rungs = events(t, "recovery_rung")
    chk(len(rungs) == 1 and rungs[0][1].get("id") == "R5"
        and rungs[0][1].get("authored") is False
        and rungs[0][1].get("action") == "safe_stop",
        "manual: recovery.trigger R5 -> recovery_rung "
        "{id:R5, authored:false, action:safe_stop}")
    chk("HARD STOP" in t,
        "manual: the manual R5 walked the real safe-stop ladder")


def test_wire_verbs():
    print("[wire] recovery.trigger / flow.trigger over PPE1")
    evs = []
    cli = C._make_client("idle-command", on_event=evs.append).spawn()
    try:
        chk(cli.wait_ready(), "wire: engine ready")
        a = cli.request("recovery.trigger", {"id": "R1"})
        chk(a.get("ok") is False and a["error"]["code"] == "BAD_STATE",
            "wire: recovery.trigger refused while idle (running only)")
        a = cli.request("flow.trigger", {})
        chk(a.get("ok") is False and a["error"]["code"] == "BAD_PARAMS",
            "wire: flow.trigger without id -> BAD_PARAMS")
        a = cli.request("run.start", {"mode": "auto"})
        chk(a.get("ok") is True, "wire: run started")
        import time as _t
        deadline = _t.time() + 10
        while _t.time() < deadline and not any(
                e["ev"] == "run.started" for e in evs):
            _t.sleep(0.05)
        a = cli.request("recovery.trigger", {"id": "bogus"})
        chk(a.get("ok") is False and a["error"]["code"] == "BAD_PARAMS",
            "wire: unknown rung id -> BAD_PARAMS")
        a = cli.request("recovery.trigger", {"id": "R1"})
        chk(a.get("ok") is True and a["result"].get("queued") is True,
            "wire: recovery.trigger R1 queued while running")
        deadline = _t.time() + 15
        while _t.time() < deadline and not any(
                e["ev"] == "safety.event"
                and e["data"].get("type") == "recovery_rung" for e in evs):
            _t.sleep(0.05)
        hits = [e for e in evs if e["ev"] == "safety.event"
                and e["data"].get("type") == "recovery_rung"]
        chk(bool(hits) and hits[0]["data"].get("id") == "R1"
            and hits[0]["data"].get("authored") is False,
            "wire: safety.event recovery_rung {id:R1, authored:false} "
            "emitted on the tick thread")
        a = cli.request("flow.trigger", {"id": "anything"})
        chk(a.get("ok") is False and a["error"]["code"] == "BAD_PARAMS",
            "wire: flow.trigger with no flows configured -> unknown id")
        a = cli.request("run.stop")
        chk(a.get("ok") is True, "wire: run stopped")
        cli.shutdown()
    finally:
        cli.kill()


if __name__ == "__main__":
    test_readonly_interleave()
    test_input_none_refused()
    test_exclusive_boundary()
    test_priority_policies()
    test_pause_stop_semantics()
    test_flows_suspended_during_recovery_and_safe_pause()
    test_authored_rung_limit_cooldown_resume()
    test_rung_override_and_disable()
    test_manual_triggers_inprocess()
    test_wire_verbs()
    print()
    if FAILS:
        print("FLOW TESTS: %d FAILURES" % len(FAILS))
        sys.exit(1)
    print("FLOW TESTS: ALL PASS")
