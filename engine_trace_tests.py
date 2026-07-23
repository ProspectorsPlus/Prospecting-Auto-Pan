#!/usr/bin/env python3
"""Classic instruction-trace tests (canonical-foundation pass, spec A
deliverable 4).

engine_sim.InputLog already records every injected input with virtual-ms
stamps during headless Classic runs and throws it away; this harness runs
the REAL engine main() through engine_sim.run_legacy and PERSISTS the
normalized instruction trace ({t, ev, key?...} -- the studio_conformance
shape) into engine_goldens/classic_trace/<scenario>.trace.json, then
asserts the semantics the Cycle Plan documents:

  * water legs are ONE key_down(S)/key_up(S) pair each (no pulsing in
    healthy legs);
  * shake is a click RATTLE: mouse pairs held exactly SHAKE_CLICK_MS with
    exactly SHAKE_CLICK_GAP_MS gaps, the whole attempt bounded by
    SHAKE_HOLD_MS (+ the documented retry extension) -- never a hold;
  * no W/S tap is shorter than BURST_ON_MS outside recovery paths;
  * the settle gap between the last shake input and the first dig input
    is exactly POST_SHAKE_SETTLE_MS + PRE_DIG_SETTLE_MS;
  * recovery cadences: stuck-ladder's nudges ride LAND_PROBE_NUDGE_MS +
    PROBE_GAP_MS and its break-outs BREAKOUT_REPOS_MS; the stuck-FULL
    scenario (inline -- deliberately not in engine_scenarios/, so the
    characterization suite is untouched) reaches the stuck-watchdog rung
    whose jitter IS the BURST_ON_MS/BURST_OFF_MS pulse cadence, plus
    break-out click-to-empty rattles at the shake click cadence.
    (stuck-ladder itself never pulses: its situation signature keeps
    changing, so the no-progress fast path preempts the watchdog rung --
    the pulse cadence lives where the plan says it does.)
  * determinism: two runs produce identical traces;
  * plan cross-check: resolve_cycle_plan on the same module predicts the
    measured DIG_CLICK_MS / SHAKE_CLICK_MS / SHAKE_CLICK_GAP_MS /
    POST_SHAKE_SETTLE_MS intervals exactly (virtual clock).

  python3 engine_trace_tests.py            # compare against goldens
  python3 engine_trace_tests.py --update   # regenerate deliberately
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import engine_sim                          # noqa: E402
from prospector_engine import cycleplan    # noqa: E402

GOLD = os.path.join(ROOT, "engine_goldens", "classic_trace")
FAILS = []

# The stuck-FULL scenario: pan permanently FULL on land (Deposit cue held,
# capacity pinned at 1.0), so go_water can never reach the Pan cue and the
# situation signature NEVER changes -- the stuck watchdog rung fires and
# recover() jitters S with pulse_until (BURST_ON/OFF taps), then break-out
# click-rattles. Inline by design: a file in engine_scenarios/ would join
# the characterization golden set; this scenario exists for the trace
# harness only.
STUCK_FULL = {
    "name": "classic-stuck-full",
    "window": [0, 0, 1440, 900],
    "config": {"RELICS_ENABLED": False, "WEBHOOK_URL": "",
               "WEBHOOK_SECRET": ""},
    "schedule": [[0, "toggle"], [30000, "quit"]],
    "cues": {"DEPOSIT": [[0, 600000]]},
    "capacity": [[0, 1.0], [600000, 1.0]],
    "screen": [{"rect": [0, 0, 1, 1], "rgb": [16, 16, 16], "from_ms": 0}],
    "_screen_comment": "1x1 rect flips capacity paint into whole-box mode "
                       "(see classic-standard.json) so FULL is readable",
    "duration_ms": 600000,
}


def chk(cond, msg):
    print("  [%s] %s" % ("PASS" if cond else "FAIL", msg))
    if not cond:
        FAILS.append(msg)


# ---- normalization ----------------------------------------------------------

def _key_names(eng):
    names = {eng.KEY_W: "W", eng.KEY_A: "A", eng.KEY_S: "S",
             eng.KEY_D: "D", eng.KEY_SHIFT: "Shift", eng.KEY_SPACE: "Space"}
    for d, code in getattr(eng, "SLOT_KEYCODES", {}).items():
        names.setdefault(code, "slot%s" % d)
    return names


def normalize_trace(world):
    """world.inputs.events -> [{t, ev, ...}]. Drops the release_all floor
    (key_up/mouse_up for things not actually held -- the engine's per-tick
    anti-drift no-ops), names the key codes, and dict-ifies the tuples.
    What remains is the exact injected-input behavior."""
    names = _key_names(world.po)
    held = set()
    mouse_down = False
    out = []
    for t, kind, arg in world.inputs.events:
        if kind == "key_down":
            held.add(arg)
            out.append({"t": int(t), "ev": "key_down",
                        "key": names.get(arg, "k%s" % arg)})
        elif kind == "key_up":
            if arg not in held:
                continue                       # release_all floor no-op
            held.discard(arg)
            out.append({"t": int(t), "ev": "key_up",
                        "key": names.get(arg, "k%s" % arg)})
        elif kind == "mouse_down":
            mouse_down = True
            out.append({"t": int(t), "ev": "mouse_down"})
        elif kind == "mouse_up":
            if not mouse_down:
                continue                       # release_all floor no-op
            mouse_down = False
            out.append({"t": int(t), "ev": "mouse_up"})
        elif kind == "mouse_move":
            out.append({"t": int(t), "ev": "mouse_move",
                        "x": arg[0], "y": arg[1]})
        elif kind == "scroll":
            out.append({"t": int(t), "ev": "scroll", "steps": arg})
        else:
            out.append({"t": int(t), "ev": str(kind)})
    return out


def summarize(trace):
    counts = {}
    for e in trace:
        counts[e["ev"]] = counts.get(e["ev"], 0) + 1
    return {"events": len(trace), "counts": counts,
            "first_t": trace[0]["t"] if trace else None,
            "last_t": trace[-1]["t"] if trace else None}


def trace_doc(name, trace):
    return {"_trace": 1, "scenario": name, "summary": summarize(trace),
            "trace": trace}


def render(doc):
    return json.dumps(doc, indent=1, sort_keys=True) + "\n"


def run_scenario(name_or_path, alias):
    transcript, world = engine_sim.run_legacy(name_or_path, alias=alias)
    return transcript, world, normalize_trace(world)


def compare_golden(name, doc, update):
    os.makedirs(GOLD, exist_ok=True)
    gp = os.path.join(GOLD, name + ".trace.json")
    text = render(doc)
    if update:
        with open(gp, "w", encoding="utf-8") as f:
            f.write(text)
        print("  [gold] wrote %s (%d bytes)" % (gp, len(text)))
        return
    if not os.path.exists(gp):
        chk(False, "%s: trace golden missing (run --update once)" % name)
        return
    with open(gp, encoding="utf-8") as f:
        golden = f.read()
    chk(text == golden,
        "%s: normalized trace byte-identical to golden (%d events)"
        % (name, doc["summary"]["events"]))
    if text != golden:
        gl, tl = golden.splitlines(), text.splitlines()
        for i in range(max(len(gl), len(tl))):
            a = gl[i] if i < len(gl) else "<missing>"
            b = tl[i] if i < len(tl) else "<missing>"
            if a != b:
                print("    first diff at line %d\n      golden: %s\n"
                      "      got:    %s" % (i + 1, a[:120], b[:120]))
                break


# ---- trace algebra ----------------------------------------------------------

def key_pairs(trace, key):
    """[(down_t, up_t)] for one key, in order."""
    pairs, open_t = [], None
    for e in trace:
        if e["ev"] == "key_down" and e.get("key") == key:
            open_t = e["t"]
        elif e["ev"] == "key_up" and e.get("key") == key:
            if open_t is not None:
                pairs.append((open_t, e["t"]))
                open_t = None
    return pairs


def mouse_pairs(trace):
    pairs, open_t = [], None
    for e in trace:
        if e["ev"] == "mouse_down":
            open_t = e["t"]
        elif e["ev"] == "mouse_up" and open_t is not None:
            pairs.append((open_t, e["t"]))
            open_t = None
    return pairs


def click_runs(pairs, hold_ms, max_gap=200):
    """Group mouse pairs with the given hold into contiguous runs."""
    clicks = [(d, u) for d, u in pairs if u - d == hold_ms]
    runs, cur = [], []
    for d, u in clicks:
        if cur and d - cur[-1][1] > max_gap:
            runs.append(cur)
            cur = []
        cur.append((d, u))
    if cur:
        runs.append(cur)
    return runs


def event_types(transcript):
    types = []
    for line in transcript.splitlines():
        if line.startswith("__EVENT__ "):
            try:
                types.append(json.loads(line[len("__EVENT__ "):])["type"])
            except ValueError:
                pass
    return types


# ---------------------------------------------------------------------------
def test_classic_standard(update):
    print("[trace] classic-standard (full healthy cycle)")
    transcript, world, trace = run_scenario("classic-standard",
                                            "pold_tr_std")
    eng = world.po
    plan = cycleplan.resolve_cycle_plan(eng)
    compare_golden("classic-standard", trace_doc("classic-standard", trace),
                   update)

    chk(world.inputs.all_released(), "std: all inputs released at exit")
    chk(not event_types(transcript),
        "std: healthy run -- zero safety events (no nudge/recover/"
        "break_out/shake_fail)")

    # plan-predicted timings
    p_dig = plan["legs"]["dig"]["steps"][1]["click"]["timing"]["resolved_ms"]
    p_click = plan["legs"]["shake"]["steps"][3]["click"]["resolved_ms"]
    p_gap = plan["legs"]["shake"]["steps"][3]["gap"]["resolved_ms"]
    p_hold = plan["legs"]["shake"]["steps"][3]["attempt_timeout"][
        "resolved_ms"]
    p_settle = plan["legs"]["settle"]["steps"][0]["timing"]["resolved_ms"]
    p_presettle = plan["legs"]["dig"]["steps"][0]["timing"]["resolved_ms"]

    mp = mouse_pairs(trace)
    holds = sorted({u - d for d, u in mp})
    chk(holds == sorted({p_dig, p_click}),
        "std: every mouse hold is a dig (%dms) or a shake click (%dms) "
        "-- got %r" % (p_dig, p_click, holds))
    digs = [(d, u) for d, u in mp if u - d == p_dig]
    chk(len(digs) >= 2, "std: at least two dig clicks (got %d)" % len(digs))
    chk(digs[0][1] - digs[0][0] == p_dig,
        "std: plan cross-check -- measured dig hold == plan DIG_CLICK_MS "
        "(%d)" % p_dig)

    # shake: one rattle run, exact cadence, bounded window
    runs = click_runs(mp, p_click)
    chk(len(runs) == 1, "std: exactly one shake rattle run (got %d)"
        % len(runs))
    run = runs[0]
    chk(len(run) > 10, "std: a real drain -- %d shake clicks" % len(run))
    gaps = {run[i + 1][0] - run[i][1] for i in range(len(run) - 1)}
    chk(gaps == {p_gap},
        "std: plan cross-check -- every shake gap == plan "
        "SHAKE_CLICK_GAP_MS (%d), got %r" % (p_gap, sorted(gaps)))
    chk(all(u - d == p_click for d, u in run),
        "std: plan cross-check -- every shake hold == plan SHAKE_CLICK_MS "
        "(%d)" % p_click)
    window = run[-1][1] - run[0][0]
    retries = event_types(transcript).count("shake_start_retry")
    chk(window <= p_hold + 600 * retries + p_click + p_gap,
        "std: shake attempt window %dms bounded by SHAKE_HOLD_MS %dms "
        "(+%d retry extensions) -- a timeout, never a hold"
        % (window, p_hold, retries))

    # momentum W: held across the whole rattle, no micro-taps
    w = key_pairs(trace, "W")
    chk(len(w) == 1 and w[0][0] == run[0][0] and w[0][1] >= run[-1][1],
        "std: one momentum-W hold spanning the rattle (glide is W, "
        "shake is clicks)")

    # water legs: one S pair per __PHASE__ water
    s_pairs = key_pairs(trace, "S")
    phases_water = transcript.count("__PHASE__ water")
    chk(len(s_pairs) == phases_water,
        "std: exactly one key_down/key_up S pair per water leg "
        "(%d legs, %d pairs)" % (phases_water, len(s_pairs)))
    burst_on = eng.BURST_ON_MS
    chk(all(u - d >= burst_on for d, u in s_pairs + w),
        "std: no W/S tap shorter than BURST_ON_MS outside recovery paths")

    # settle: last shake input -> first dig input
    shake_end = max(run[-1][1], w[0][1])
    nxt = [d for d, u in digs if d > shake_end]
    chk(bool(nxt) and nxt[0] - shake_end == p_settle + p_presettle,
        "std: plan cross-check -- settle gap %s == POST_SHAKE_SETTLE_MS "
        "%d + PRE_DIG_SETTLE_MS %d (virtual-exact)"
        % (nxt[0] - shake_end if nxt else None, p_settle, p_presettle))
    chk(bool(nxt) and nxt[0] - shake_end >= p_settle,
        "std: settle gap >= POST_SHAKE_SETTLE_MS")

    # determinism: a second run produces the identical trace
    _t2, _w2, trace2 = run_scenario("classic-standard", "pold_tr_std2")
    chk(trace == trace2, "std: two runs -> identical instruction traces")
    chk(int(world.webhooks[0][1] == "start") == 1,
        "std: start webhook recorded")


def test_stuck_ladder(update):
    print("[trace] stuck-ladder (recovery: nudges + break-outs)")
    transcript, world, trace = run_scenario("stuck-ladder", "pold_tr_lad")
    eng = world.po
    compare_golden("stuck-ladder", trace_doc("stuck-ladder", trace), update)
    chk(world.inputs.all_released(), "ladder: all inputs released at exit")
    evs = event_types(transcript)
    chk("nudge" in evs and "break_out" in evs and "no_progress" in evs,
        "ladder: nudge/no_progress/break_out events present")

    nudge, repos = eng.LAND_PROBE_NUDGE_MS, eng.BREAKOUT_REPOS_MS
    w = key_pairs(trace, "W")
    holds = sorted({u - d for d, u in w})
    chk(holds == sorted({nudge, repos}),
        "ladder: every W move is a LAND_PROBE_NUDGE_MS (%d) nudge or a "
        "BREAKOUT_REPOS_MS (%d) reposition -- got %r"
        % (nudge, repos, holds))
    chk(len([p for p in w if p[1] - p[0] == nudge]) == evs.count("nudge"),
        "ladder: one W nudge per 'nudge' event")
    # nudge settle: PROBE_GAP_MS to the next probe dig, exactly -- the
    # LAST nudge of a dig leg is followed by a fresh leg instead, which
    # adds its first-round PRE_DIG_SETTLE_MS on top
    digs = [(d, u) for d, u in mouse_pairs(trace)
            if u - d == eng.DIG_CLICK_MS]
    dig_downs = [d for d, _u in digs]
    gaps = []
    for d, u in w:
        if u - d != nudge:
            continue
        nxt = [t for t in dig_downs if t > u]
        if nxt:
            gaps.append(nxt[0] - u)
    leg_ends = transcript.count("no land after")
    exact = sum(1 for g in gaps if g == eng.PROBE_GAP_MS)
    chk(gaps and 0 < len(gaps) - exact <= leg_ends,
        "ladder: every MID-LEG nudge settles exactly PROBE_GAP_MS (%d) "
        "before the next probe dig (%d/%d; the %d leg-boundary nudges "
        "hand off to break-out/safe-pause machinery instead)"
        % (eng.PROBE_GAP_MS, exact, len(gaps), leg_ends))
    chk(all(u - d >= eng.BURST_ON_MS for d, u in w),
        "ladder: recovery taps still respect the BURST_ON_MS floor")


def test_stuck_full(update):
    print("[trace] classic-stuck-full (stuck watchdog: pulse cadence)")
    path = os.path.join(tempfile.mkdtemp(prefix="ppe-trace-"),
                        "classic-stuck-full.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(STUCK_FULL, f)
    transcript, world, trace = run_scenario(path, "pold_tr_full")
    eng = world.po
    compare_golden("classic-stuck-full",
                   trace_doc("classic-stuck-full", trace), update)
    chk(world.inputs.all_released(), "stuck: all inputs released at exit")
    evs = event_types(transcript)
    chk("recover" in evs and "break_out" in evs,
        "stuck: the watchdog rung fired (recover + break_out events)")

    # recover() jitter = pulse_until taps: BURST_ON_MS held, BURST_OFF_MS
    # released, re-checking the cue after every tap
    on, off = eng.BURST_ON_MS, eng.BURST_OFF_MS
    s = key_pairs(trace, "S")
    taps = [(d, u) for d, u in s if u - d == on]
    chk(len(taps) >= 3,
        "stuck: recovery pulses present (%d taps of BURST_ON_MS=%dms)"
        % (len(taps), on))
    gaps = set()
    for i in range(1, len(taps)):
        g = taps[i][0] - taps[i - 1][1]
        if g <= 50:                       # same pulse_until burst
            gaps.add(g)
    chk(gaps == {off},
        "stuck: pulse cadence gap == BURST_OFF_MS (%d), got %r"
        % (off, sorted(gaps)))
    # each pulse burst is bounded by the RECOVER_BACK_MS budget
    budget = eng.RECOVER_BACK_MS
    bursts, cur = [], []
    for d, u in taps:
        if cur and d - cur[-1][1] > 50:
            bursts.append(cur)
            cur = []
        cur.append((d, u))
    if cur:
        bursts.append(cur)
    chk(all(b[-1][1] - b[0][0] <= budget + on + off for b in bursts),
        "stuck: every pulse burst bounded by RECOVER_BACK_MS (%d)"
        % budget)

    # break-out click-to-empty rattle: shake click cadence, bounded by
    # BREAKOUT_SHAKE_MS
    runs = click_runs(mouse_pairs(trace), eng.SHAKE_CLICK_MS)
    chk(len(runs) >= 1, "stuck: break-out click rattle present (%d runs)"
        % len(runs))
    gaps = {r[i + 1][0] - r[i][1] for r in runs for i in range(len(r) - 1)}
    chk(gaps <= {eng.SHAKE_CLICK_GAP_MS},
        "stuck: break-out rattle rides SHAKE_CLICK_MS/SHAKE_CLICK_GAP_MS "
        "(gaps %r)" % sorted(gaps))
    chk(all(r[-1][1] - r[0][0] <= eng.BREAKOUT_SHAKE_MS
            + eng.SHAKE_CLICK_MS + eng.SHAKE_CLICK_GAP_MS for r in runs),
        "stuck: every rattle bounded by BREAKOUT_SHAKE_MS (%d)"
        % eng.BREAKOUT_SHAKE_MS)
    chk(any(u - d == eng.BREAKOUT_REPOS_MS
            for d, u in key_pairs(trace, "W")),
        "stuck: break-out W reposition (BREAKOUT_REPOS_MS=%d) present"
        % eng.BREAKOUT_REPOS_MS)


# ---------------------------------------------------------------------------
def test_classic_shards(update):
    """SHARDS exact-click mode (Studio detach parity companions). The three
    scenarios live in engine_goldens/classic_trace/*.scenario.json (NOT
    engine_scenarios/ -- the characterization suite's auto-discovery must
    not see them) and are vendored to the Studio, whose detach parity gate
    runs its materialized shards program against these same goldens."""
    for name in ("classic-shards", "classic-shards-miss",
                 "classic-shards-assume"):
        print("[trace] %s" % name)
        path = os.path.join(GOLD, name + ".scenario.json")
        transcript, world, trace = run_scenario(path, "pold_tr_" +
                                                name.replace("-", "_"))
        eng = world.po
        compare_golden(name, trace_doc(name, trace), update)
        chk(world.inputs.all_released(),
            "%s: all inputs released at exit" % name)
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)["config"]
        mp = mouse_pairs(trace)
        p_dig = eng.DIG_CLICK_MS
        digs = [(d, u) for d, u in mp if u - d == p_dig]
        gap = int(190000.0 / max(1.0, eng.DIG_SPEED)) + 25
        evs = event_types(transcript)

        if name == "classic-shards":
            chk(not evs, "shards: healthy run -- zero safety events")
            # exactly SHARDS_DIG_CLICKS dig-holds: the confirmed click plus
            # its rhythm-gapped follow-up, and nothing else before the quit.
            chk(len(digs) == cfg["SHARDS_DIG_CLICKS"],
                "shards: exactly SHARDS_DIG_CLICKS dig clicks (got %d)"
                % len(digs))
            # The rhythm-gap sleep starts at proof CONFIRM, which in this
            # choreography lands two 25ms polls after click-up (rise at
            # 200 against the 180/205 poll grid).
            chk(len(digs) >= 2 and digs[1][0] - digs[0][1] == gap + 50,
                "shards: the follow-up rides the dig rhythm gap from the "
                "confirm (190000/DIG_SPEED+25 = %dms, +50ms poll latency; "
                "got %s)"
                % (gap, digs[1][0] - digs[0][1] if len(digs) >= 2 else "n/a"))
        if name == "classic-shards-miss":
            chk("nudge" in evs,
                "shards-miss: the dead round emits the nudge event")
            # dead clicks retry back-to-back: click-up to next click-down is
            # exactly the max(30, SHARDS_CLICK_CONFIRM_MS) proof window.
            win = max(30, eng.SHARDS_CLICK_CONFIRM_MS)
            chk(len(digs) >= 2 and digs[1][0] - digs[0][1] == win,
                "shards-miss: the retry click follows the dead proof window "
                "(%dms; got %s)"
                % (win, digs[1][0] - digs[0][1] if len(digs) >= 2 else "n/a"))
            nudges = [(d, u) for d, u in key_pairs(trace, "W")
                      if u - d == eng.LAND_PROBE_NUDGE_MS]
            chk(len(nudges) >= 1,
                "shards-miss: nudge W rides LAND_PROBE_NUDGE_MS (%d)"
                % eng.LAND_PROBE_NUDGE_MS)
        if name == "classic-shards-assume":
            chk(not evs, "shards-assume: healthy run -- zero safety events")
            chk(len(digs) == 1,
                "shards-assume: ONE dig click, no fill wait (got %d)"
                % len(digs))
            # assume-full returns immediately: the water leg's S press
            # follows the click confirm with no fill wait between.
            s = key_pairs(trace, "S")
            chk(bool(s) and s[0][0] < 400,
                "shards-assume: go_water starts before the fill could have "
                "been awaited (S at %s)" % (s[0][0] if s else "n/a"))


# ---------------------------------------------------------------------------
def test_classic_geode(update):
    """GEODE slow-fill mode (Studio detach parity companions). Same fixture
    home and vendoring rules as the shards scenarios."""
    for name in ("classic-geode", "classic-geode-miss"):
        print("[trace] %s" % name)
        path = os.path.join(GOLD, name + ".scenario.json")
        transcript, world, trace = run_scenario(path, "pold_tr_" +
                                                name.replace("-", "_"))
        eng = world.po
        compare_golden(name, trace_doc(name, trace), update)
        chk(world.inputs.all_released(),
            "%s: all inputs released at exit" % name)
        mp = mouse_pairs(trace)
        hold = max(1, eng.GEODE_DIG_MS)
        taps = [(d, u) for d, u in mp if u - d == hold]
        evs = event_types(transcript)
        if name == "classic-geode":
            chk(not evs, "geode: healthy run -- zero safety events")
            chk(len(taps) == 1,
                "geode: ONE geode tap fills the pan (got %d)" % len(taps))
            # the geode shake rides the standard click cadence but under
            # the GEODE_SHAKE_HOLD window; presence of the rattle proves
            # the geode-flavored shake ran.
            clicks = [(d, u) for d, u in mp
                      if u - d == eng.SHAKE_CLICK_MS]
            chk(len(clicks) >= 3,
                "geode: the shake rattle ran (%d clicks)" % len(clicks))
        if name == "classic-geode-miss":
            chk("nudge" in evs,
                "geode-miss: the dead round emits the nudge event")
            nudges = [(d, u) for d, u in key_pairs(trace, "W")
                      if u - d == eng.LAND_PROBE_NUDGE_MS]
            chk(len(nudges) >= 1,
                "geode-miss: nudge W rides LAND_PROBE_NUDGE_MS (%d)"
                % eng.LAND_PROBE_NUDGE_MS)
            # the full animation wait ran: tap-up to nudge-down spans the
            # start window plus the whole GEODE_DELAY_MS animation.
            w = key_pairs(trace, "W")
            if taps and w:
                span = w[0][0] - taps[0][1]
                lo = max(120, eng.GEODE_START_MS) + max(50, eng.GEODE_DELAY_MS)
                chk(span >= lo,
                    "geode-miss: the dead tap waited the start window + the "
                    "full animation (%dms >= %dms)" % (span, lo))


# ---------------------------------------------------------------------------
def test_classic_x(update):
    """X_PATTERN walk-back (Studio detach parity companion for walk_back_x).
    Same fixture home and vendoring rules as the shards scenarios. The world
    keeps the pan FULL with a late Pan cue, so go_water runs three X legs
    back-to-back: diagonal S+side pairs, the balanced side pick alternating
    via x_dir, and pure-strafe recenter taps once the drift ledger exceeds
    X_RECENTER_MS -- including the int-truncated dusty tap (157ms =
    int(224.99999999999997 * 0.7)) that makes this leg an engine-op
    practical rather than an expression mirror."""
    name = "classic-x"
    print("[trace] %s" % name)
    path = os.path.join(GOLD, name + ".scenario.json")
    transcript, world, trace = run_scenario(path, "pold_tr_x")
    eng = world.po
    compare_golden(name, trace_doc(name, trace), update)
    chk(world.inputs.all_released(), "x: all inputs released at exit")
    evs = event_types(transcript)
    chk(evs == ["recenter", "recenter"],
        "x: exactly the two recenter events, nothing else -- got %r" % evs)

    s_pairs = key_pairs(trace, "S")
    phases_water = transcript.count("__PHASE__ water")
    chk(len(s_pairs) == 3 and phases_water == 3,
        "x: three water legs, one S hold each (%d legs, %d pairs)"
        % (phases_water, len(s_pairs)))

    # each S hold carries EXACTLY one diagonal side pair, opened at the
    # same instant as the S press (key_down S + key_down side) and released
    # before (or with) the S release -- the short-diagonal-then-straight shape
    sides = []
    a_pairs, d_pairs = key_pairs(trace, "A"), key_pairs(trace, "D")
    for sd, su in s_pairs:
        inside = [("A", d, u) for d, u in a_pairs if sd <= d and u <= su] \
            + [("D", d, u) for d, u in d_pairs if sd <= d and u <= su]
        chk(len(inside) == 1 and inside[0][1] == sd,
            "x: one diagonal side pair per water leg, opened with S "
            "(leg at %d: %r)" % (sd, inside))
        if inside:
            sides.append(inside[0][0])
    chk(sides == ["D", "A", "D"],
        "x: balanced side picks ALTERNATE via x_dir (D, then A, then D) "
        "-- got %r" % (sides,))

    # leg 1's diagonal is budget-capped: min(X_STRAFE_MS, PAN_BACK_MAX_MS)
    d1 = [(d, u) for d, u in d_pairs if d == s_pairs[0][0]]
    chk(bool(d1) and d1[0][1] - d1[0][0]
        == min(eng.X_STRAFE_MS, eng.PAN_BACK_MAX_MS),
        "x: leg-1 diagonal capped by the first budget "
        "(min(X_STRAFE_MS %d, PAN_BACK_MAX_MS %d))"
        % (eng.X_STRAFE_MS, eng.PAN_BACK_MAX_MS))

    # recenter strafes: pure A/D taps OUTSIDE any S hold, once drift
    # exceeds X_RECENTER_MS. 140 = int(200*0.7); 157 = int(224.999...*0.7)
    # -- the truncated dusty tap the float trajectory produces.
    def outside_s(p):
        return not any(sd <= p[0] and p[1] <= su for sd, su in s_pairs)
    recenters = sorted([("A", u - d) for d, u in a_pairs
                        if outside_s((d, u))]
                       + [("D", u - d) for d, u in d_pairs
                          if outside_s((d, u))])
    chk(recenters == [("A", 140), ("D", 157)],
        "x: recenter strafes are the pure A 140ms / D 157ms taps "
        "(int-truncated drift * 0.7, dust included) -- got %r"
        % (recenters,))

    # the successful leg still shakes: one momentum-W rattle after leg 3
    w = key_pairs(trace, "W")
    chk(len(w) == 1 and w[0][0] == s_pairs[2][1],
        "x: the reached leg hands off to the momentum-W shake immediately")

    # determinism: a second run produces the identical trace
    _t2, _w2, trace2 = run_scenario(path, "pold_tr_x2")
    chk(trace == trace2, "x: two runs -> identical instruction traces")


# The script-clock grid scenario: SCRIPT_MODE running a tiny v4 program whose
# sleep lengths deliberately leave float-division dust (34/1000 has no exact
# double), followed by a wait whose timeout is a whole multiple of the 25 ms
# poll. The regression this pins: _script_sleep must count its budget down
# exactly (never re-derive the remainder from the clock), or the dust-sized
# final slice bumps the virtual clock +1 us off the native instant grid and
# the poll-aligned wait deadline flips one poll late — the real-world
# "Detached diverged on event 80" (Shards FR Latest, SHARDS_CLICK_CONFIRM_MS
# = 100 on the 25 ms grid).
_GRID_SCRIPT = {
    "format": "ppscript", "version": 4, "name": "grid",
    "variables": [{"name": "x", "type": "bool", "initial": False}],
    "blocks": [
        {"id": "a", "type": "btn_down", "params": {}},
        {"id": "b", "type": "sleep_ms", "params": {"ms": 10}},
        {"id": "c", "type": "btn_up", "params": {}},
        {"id": "d", "type": "sleep_ms", "params": {"ms": 34}},
        {"id": "e", "type": "wait_expr",
         "params": {"timeout_ms": 100, "confirm": 1, "min_ms": 0,
                    "poll_ms": 25, "cond": False,
                    "on_timeout": "continue", "store": "x"}},
        {"id": "f", "type": "btn_down", "params": {}},
        {"id": "g", "type": "sleep_ms", "params": {"ms": 10}},
        {"id": "h", "type": "btn_up", "params": {}},
        {"id": "i", "type": "wait_expr",
         "params": {"timeout_ms": 30000, "confirm": 1, "min_ms": 0,
                    "poll_ms": 25, "cond": False,
                    "on_timeout": "continue", "store": "x"}},
    ]}

SCRIPT_GRID = {
    "name": "script-clock-grid",
    "window": [0, 0, 1440, 900],
    "config": {"RELICS_ENABLED": False, "WEBHOOK_URL": "",
               "WEBHOOK_SECRET": "", "SCRIPT_MODE": True,
               "SCRIPT_ACTIVE": "grid",
               "SCRIPT_JSON": json.dumps(_GRID_SCRIPT)},
    "schedule": [[0, "toggle"], [2000, "quit"]],
    "cues": {},
    "capacity": [[0, 0.0]],
    "duration_ms": 5000,
}


def test_script_clock_grid(update):
    print("[trace] script-clock-grid (v4 sleeps stay on the native grid)")
    path = os.path.join(tempfile.mkdtemp(prefix="ppe-trace-"),
                        "script-clock-grid.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(SCRIPT_GRID, f)
    _transcript, world, trace = run_scenario(path, "pold_tr_grid")
    mp = mouse_pairs(trace)
    chk(len(mp) >= 2, "grid: both script clicks landed (%d)" % len(mp))
    if len(mp) >= 2:
        gap = mp[1][0] - mp[0][1]
        chk(gap == 134,
            "grid: dusty sleep (34ms) + poll-aligned wait (100ms) spans "
            "exactly 134ms, got %dms (159 = the +1us dust bug)" % gap)
    chk(world.inputs.all_released(), "grid: all inputs released at exit")


if __name__ == "__main__":
    update = "--update" in sys.argv
    test_classic_standard(update)
    test_stuck_ladder(update)
    test_stuck_full(update)
    test_classic_shards(update)
    test_classic_geode(update)
    test_classic_x(update)
    test_script_clock_grid(update)
    print()
    if update:
        print("TRACE GOLDENS REGENERATED")
        sys.exit(0)
    if FAILS:
        print("TRACE TESTS: %d FAILURES" % len(FAILS))
        sys.exit(1)
    print("TRACE TESTS: ALL PASS")
