#!/usr/bin/env python3
"""Engine self-tests for the v4 `parallel` op (docs/PARALLEL_IR.md in the
Studio repo). Dev-only; not shipped, not in the zip.

Covers the engine-side guarantees the cross-language goldens deliberately
avoid (errors, deadlocks, policy contrasts) plus the load-time admission
refusals and determinism:

  * double-run byte-equality of every v4_parallel_* / v4_gate_* golden
    through the REAL interpreter under the virtual clock;
  * effect-model admission refusals (blocking op, no-walker op, guard,
    nested parallel, split/read-only device rules derived from declared
    ownership, serialize-required practical conflicts) — session ten's
    semantic replacement for the name whitelist;
  * the effect table (_SCRIPT4_CONC) agreeing byte for byte with the
    Studio registry via shared/goldens/v4_conc_table.json;
  * practical-in-branch execution: ledger-arbitrated holds/taps, queue
    handoff, cancellation stripping a practical's holds at its seam;
  * input arbitration corners: exclusive conflict error, queue vs priority
    waiter order, share merge, deadlock detection, fail-policy op failure;
  * stop-inside-a-branch event order (holds release BEFORE the stop row);
  * first_terminal swallowing a branch raise as completion;
  * the generated capability manifest advertising `parallel`.

Run from the repo root:  python3 engine_parallel_tests.py
Must end with:  PARALLEL: ALL PASS
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import studio_conformance as conf                    # noqa: E402

po = conf.po
GOLDENS = conf.GOLDENS
FAILS = []


def chk(cond, msg):
    print("  [%s] %s" % ("PASS" if cond else "FAIL", msg))
    if not cond:
        FAILS.append(msg)


def run_script(blocks, variables=None, scenario=None, name="par-test"):
    """Run a v4 script through the conformance harness machinery."""
    golden = {
        "script": {
            "format": "ppscript", "version": 4, "name": name,
            "description": "", "author": "", "created": 1, "updated": 1,
            "stats": "manual",
            **({"variables": variables} if variables else {}),
            "blocks": blocks, "settings": {},
        },
        "scenario": scenario or {"capacity": [[0, 1.0], [600000, 1.0]]},
        "run": {"seed": 1, "maxPasses": 1},
    }
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(golden, f)
        return conf.run_golden(path)
    finally:
        os.unlink(path)


def par(branches, **params):
    p = {"join": "all", "n": 2, "input": "exclusive", "store": ""}
    p.update(params)
    return {"id": "p1", "type": "parallel", "params": p,
            "branches": branches}


def sleep(nid, ms):
    return {"id": nid, "type": "sleep_ms", "params": {"ms": ms}}


def pin(nid, key):
    return {"id": nid, "type": "key_pin", "params": {"key": key}}


def unpin(nid, key):
    return {"id": nid, "type": "key_unpin", "params": {"key": key}}


def tap(nid, key, hold):
    return {"id": nid, "type": "tap_key",
            "params": {"key": key, "hold_ms": hold}}


def key_events(got):
    return ["%s:%s@%d" % (e["ev"], e["key"], e["t"])
            for e in got["trace"] if e["ev"] in ("key_down", "key_up")]


def dead_of(blocks, version=4):
    raw = json.dumps({"format": "ppscript", "version": version, "name": "x",
                      "blocks": blocks})
    return po.ScriptRunner(raw, "x").dead


# ---- 1. golden determinism --------------------------------------------------

def test_golden_determinism():
    print("[1] parallel goldens: double-run byte-equality")
    names = sorted(n for n in os.listdir(GOLDENS)
                   if n.startswith(("v4_parallel", "v4_gate"))
                   and n.endswith(".json"))
    chk(len(names) >= 7, "found %d parallel/gate goldens" % len(names))
    for n in names:
        path = os.path.join(GOLDENS, n)
        a, ea = conf.run_golden(path)
        b, eb = conf.run_golden(path)
        chk(ea is None and eb is None, "%s: both runs completed" % n)
        if ea or eb:
            continue
        chk(json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True),
            "%s: two runs byte-identical" % n)


# ---- 2. effect-model admission refusals -------------------------------------

def test_whitelist():
    print("[2] load-time admission refusals (effect-model reasons)")
    # Session ten: the practicals are ADMITTED now — the old refusal case
    # inverts into an acceptance check (compatibility widening).
    d = dead_of([par([[{"id": "r1", "type": "rattle_until", "params": {}}],
                      [sleep("b1", 10)]], input="share")])
    chk(not d, "rattle_until inside a branch now loads clean (dead=%r)" % d)

    d = dead_of([par([[{"id": "s1", "type": "shake", "params": {}}],
                      [sleep("b1", 10)]])])
    chk("'shake' inside a Fork branch" in (d or "")
        and "holds the walker" in (d or ""),
        "a blocking op is refused with the honest reason: %r" % d)

    d = dead_of([par([[{"id": "h1", "type": "hud_text",
                        "params": {"text": "x"}}], [sleep("b1", 10)]])])
    chk("'hud_text' inside a Fork branch" in (d or "")
        and "no interleave-safe branch walker" in (d or ""),
        "a no-walker op is refused with the honest reason: %r" % d)

    d = dead_of([par([[{"id": "g1", "type": "guard", "params": {},
                        "children": [sleep("x", 10)]}], [sleep("b1", 10)]])])
    chk("'guard' inside a Fork branch" in (d or ""),
        "guard refused: %r" % d)

    d = dead_of([par([[par([[sleep("x", 10)], [sleep("y", 10)]])],
                      [sleep("b1", 10)]])])
    chk("nested Fork is not supported" in (d or ""),
        "nested parallel refused: %r" % d)

    d = dead_of([par([[sleep("a", 10)]])])
    chk(bool(d), "1 branch refused: %r" % d)

    d = dead_of([par([[sleep("a", 10)], [sleep("b", 10)],
                      [sleep("c", 10)]], input="split_kb_mouse")])
    chk("exactly" in (d or ""), "split with 3 branches refused: %r" % d)

    d = dead_of([par([[{"id": "m1", "type": "btn_down", "params": {}}],
                      [sleep("b", 10)]], input="split_kb_mouse")])
    chk("keyboard branch" in (d or ""),
        "mouse op in the keyboard branch refused: %r" % d)

    # Derived device rules cover the practicals: probe_dig owns the mouse,
    # so it can never sit in the keyboard half of a split.
    d = dead_of([par([[{"id": "p1", "type": "probe_dig", "params": {}}],
                      [sleep("b", 10)]], input="split_kb_mouse")])
    chk("'probe_dig'" in (d or "") and "keyboard branch" in (d or ""),
        "a mouse-owning practical is refused in the keyboard half: %r" % d)

    d = dead_of([par([[tap("k", "1", 40)], [sleep("b", 10)]],
                     input="read_only")])
    chk("read-only" in (d or ""), "input in a read-only Fork refused: %r" % d)

    d = dead_of([par([[{"id": "h1", "type": "hold_key_until",
                        "params": {"key": "S"}}], [sleep("b", 10)]],
                     input="read_only")])
    chk("'hold_key_until'" in (d or "") and "read-only" in (d or ""),
        "a key-owning practical is refused in a read-only Fork: %r" % d)

    # SERIALIZE REQUIRED: two branches statically owning one token under
    # exclusive input refuse at load WHEN a practical is involved… (the
    # FIRST conflicting token is named — for two rattles that is the W
    # momentum key, registered before their mouse ownership; the Studio
    # classifier orders identically)
    d = dead_of([par([
        [{"id": "r1", "type": "rattle_until", "params": {}}],
        [{"id": "r2", "type": "rattle_until", "params": {}}]])])
    chk("both drive the W key" in (d or "")
        and "queue, share or priority" in (d or ""),
        "two exclusive rattles refuse on their first shared token: %r" % d)
    # momentum off on both sides: the conflict falls to the mouse.
    d = dead_of([par([
        [{"id": "r1", "type": "rattle_until",
          "params": {"momentum_w": False}}],
        [{"id": "r2", "type": "rattle_until",
          "params": {"momentum_w": False}}]])])
    chk("both drive the left mouse button" in (d or ""),
        "two exclusive mouse-owning practicals refuse with the fix: %r" % d)

    # …but a legacy-only conflict still LOADS (its arbitration is a pinned
    # runtime error — the goldens-must-not-shift compatibility rule).
    d = dead_of([par([[pin("a1", "W")], [tap("b2", "W", 40)]])])
    chk(not d, "a legacy-only exclusive conflict still loads (dead=%r)" % d)

    # queue arbitrates the same practical pair: loads clean.
    d = dead_of([par([
        [{"id": "r1", "type": "rattle_until", "params": {}}],
        [{"id": "r2", "type": "rattle_until", "params": {}}]],
        input="queue")])
    chk(not d, "the same pair under queue loads clean (dead=%r)" % d)

    d = dead_of([{"id": "w", "type": "wait", "params": {"ms": 500},
                  "branches": [[sleep("x", 10)], [sleep("y", 10)]]}])
    chk(bool(d), "branches on a non-parallel node refused: %r" % d)

    d = dead_of([par([[sleep("a", 10)], [sleep("b", 10)]])], version=3)
    chk(bool(d), "parallel under version 3 refused: %r" % d)

    d = dead_of([par([[sleep("a", 10)], [sleep("b", 10)]])])
    chk(not d, "a well-formed Fork loads clean (dead=%r)" % d)


# ---- 3. arbitration corners -------------------------------------------------

def test_arbitration():
    print("[3] input arbitration corners")
    # exclusive conflict = deterministic named error -> stop row
    got, err = run_script([par([
        [pin("a1", "W"), sleep("a2", 300), unpin("a3", "W")],
        [sleep("b1", 100), tap("b2", "W", 40)]], input="exclusive")])
    stops = [e for e in got["trace"] if e["ev"] == "stop"]
    chk(stops and "two Fork branches both drive the W key under exclusive "
        "input; use share or queue" in stops[0]["reason"],
        "exclusive conflict names the key: %r"
        % (stops[0]["reason"] if stops else None))

    # queue vs priority: waiter order at the release
    def contest(policy):
        got, _ = run_script([par([
            [pin("a1", "W"), sleep("a2", 200), unpin("a3", "W")],
            [sleep("b1", 100), tap("b2", "W", 40)],
            [sleep("c1", 50), tap("c2", "W", 60)]], input=policy)])
        return key_events(got)

    chk(contest("queue") == [
        "key_down:W@0", "key_up:W@200", "key_down:W@200", "key_up:W@260",
        "key_down:W@260", "key_up:W@300"],
        "queue hands the token in arrival order")
    chk(contest("priority") == [
        "key_down:W@0", "key_up:W@200", "key_down:W@200", "key_up:W@240",
        "key_down:W@240", "key_up:W@300"],
        "priority hands the token to the lowest branch index")

    # share: merged holds, one down, last holder releases
    got, _ = run_script([par([
        [pin("a1", "W"), sleep("a2", 100), unpin("a3", "W")],
        [pin("b1", "W"), sleep("b2", 200), unpin("b3", "W")]],
        input="share")])
    chk(key_events(got) == ["key_down:W@0", "key_up:W@200"],
        "share merges same-direction holds (down once, last release)")

    # deadlock: completed branch never released its pin
    got, _ = run_script([par([
        [pin("a1", "W"), sleep("a2", 50)],
        [sleep("b1", 100), tap("b2", "W", 40)]], input="queue")])
    tr = got["trace"]
    up_i = next((i for i, e in enumerate(tr) if e["ev"] == "key_up"), -1)
    stop_i = next((i for i, e in enumerate(tr) if e["ev"] == "stop"), -1)
    chk(stop_i >= 0 and "the Fork deadlocked" in tr[stop_i]["reason"],
        "deadlock detected with the honest reason")
    chk(0 <= up_i < stop_i, "deadlock releases holds BEFORE the stop row")

    # fail policy: op failure, run continues, store=false
    got, _ = run_script([par([
        [pin("a1", "W"), sleep("a2", 300), unpin("a3", "W")],
        [sleep("b1", 100), tap("b2", "W", 40)]],
        input="fail", store="ok"),
        {"id": "after", "type": "log", "params": {"message": "after"}}],
        variables=[{"name": "ok", "type": "bool", "initial": True}])
    chk(got["finalVars"].get("ok") is False, "fail policy stores false")
    chk(any(e["ev"] == "log" and e.get("msg") == "after"
            for e in got["trace"]), "the run continues after a fail-policy op")
    chk(key_events(got) == ["key_down:W@0", "key_up:W@100"],
        "fail policy releases every hold at the conflict instant")


# ---- 4. stop and terminal semantics ----------------------------------------

def test_stop_semantics():
    print("[4] stop / terminal semantics inside branches")
    got, _ = run_script([par([
        [sleep("a1", 60), {"id": "a2", "type": "stop",
                           "params": {"message": "bail"}}],
        [pin("b1", "W"), sleep("b2", 500), unpin("b3", "W")]])])
    tr = got["trace"]
    up_i = next((i for i, e in enumerate(tr) if e["ev"] == "key_up"), -1)
    stop_i = next((i for i, e in enumerate(tr) if e["ev"] == "stop"), -1)
    chk(stop_i >= 0 and tr[stop_i]["reason"] == "script: bail",
        "branch stop routes through the stop funnel")
    chk(0 <= up_i < stop_i and tr[up_i]["t"] == 60,
        "sibling holds release at the stop instant, BEFORE the stop row")

    got, _ = run_script([par([
        [sleep("a1", 100), {"id": "a2", "type": "raise_error",
                            "params": {"message": "boom"}}],
        [pin("b1", "S"), sleep("b2", 500), unpin("b3", "S")]],
        join="first_terminal", store="won"),
        {"id": "after", "type": "log", "params": {"message": "after"}}],
        variables=[{"name": "won", "type": "bool", "initial": False}])
    chk(got["finalVars"].get("won") is True,
        "first_terminal treats a branch raise as completion (store=true)")
    chk(key_events(got) == ["key_down:S@0", "key_up:S@100"],
        "first_terminal cancels the sibling at the terminal instant")
    chk(any(e["ev"] == "log" and e.get("msg") == "after"
            for e in got["trace"]), "the run continues after first_terminal")

    got, _ = run_script([par([
        [sleep("a1", 100), {"id": "a2", "type": "raise_error",
                            "params": {"message": "boom"}}],
        [pin("b1", "S"), sleep("b2", 500), unpin("b3", "S")]])])
    tr = got["trace"]
    up_i = next((i for i, e in enumerate(tr) if e["ev"] == "key_up"), -1)
    stop_i = next((i for i, e in enumerate(tr) if e["ev"] == "stop"), -1)
    chk(stop_i >= 0 and "Custom script error: boom" in tr[stop_i]["reason"],
        "a raise under join=all propagates as a script error")
    chk(0 <= up_i < stop_i, "error unwind releases sibling holds first")


# ---- 5. capability manifest -------------------------------------------------

def test_capabilities():
    print("[5] generated capability manifest")
    caps = po.script_capabilities()
    chk("parallel" in caps["blocks"]["4"],
        "the v4 block table auto-advertises `parallel`")
    chk("parallel" not in caps["blocks"]["3"],
        "v3 does not advertise `parallel`")
    from prospector_engine import flows
    chk("parallel" in flows.INPUT_NODE_TYPES,
        "flows.INPUT_NODE_TYPES counts `parallel` as input-producing")


# ---- 6. effect table agreement ----------------------------------------------

def test_conc_table():
    """The engine's _SCRIPT4_CONC must agree with the Studio registry's
    NODE_CONC byte for byte — both sides pin to the same golden JSON
    (Studio asserts registry -> golden in tests/parallel-validate.test.ts;
    this asserts engine -> golden), so the admission rule can never drift
    between the two schedulers."""
    print("[6] effect table agrees with the Studio registry (golden JSON)")
    path = os.path.join(GOLDENS, "v4_conc_table.json")
    chk(os.path.isfile(path), "v4_conc_table.json exists in the goldens")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        studio = json.load(f)
    engine = {t: po._conc_norm(t) for t in po._SCRIPT4_CONC}
    chk(sorted(studio) == sorted(engine),
        "both tables declare the same %d ops" % len(engine))
    diff = [t for t in sorted(studio)
            if t in engine and studio[t] != engine[t]]
    chk(not diff, "per-op records byte-identical (diff: %r)" % diff[:5])
    # Admission invariants: never narrower than the legacy whitelist, and
    # exactly the legacy 27 + the seven session-ten practicals + mark_rung
    # (the adapter-wave state-only breadcrumb: instant, no effects).
    chk(po._SCRIPT4_PARALLEL_SAFE <= po._SCRIPT4_PAR_ADMISSIBLE,
        "admission is a superset of the legacy whitelist")
    new = sorted(po._SCRIPT4_PAR_ADMISSIBLE - po._SCRIPT4_PARALLEL_SAFE)
    chk(new == ["fill_to_full", "fill_wait", "hold_key_until", "mark_rung",
                "probe_dig", "pulse_key", "rattle_until", "walk_back_x"],
        "the widening is the seven practicals + mark_rung: %r" % (new,))


# ---- 7. practicals inside branches ------------------------------------------

def test_practicals_in_branch():
    """Branch-walker behavior the goldens deliberately avoid: queue handoff
    against a practical's held key, and cancellation stripping a
    practical's ledger holds at its suspension seam (policy contrast to
    the race golden)."""
    print("[7] practical branch walkers (arbitration + cancellation)")
    # hold_key_until holds S through the ledger; a sibling tap of S queues
    # until the hold releases at the condition hit.
    got, err = run_script([par([
        [{"id": "a1", "type": "hold_key_until",
          "params": {"key": "S", "cond": {"$expr": {"read": "cue_pan"}},
                     "timeout_ms": 400, "confirm": 1, "extra_ms": 0,
                     "store": "reached"}}],
        [sleep("b1", 50), tap("b2", "S", 40)]], input="queue")],
        variables=[{"name": "reached", "type": "bool", "initial": False}],
        scenario={"cues": [["pan", 100, 9000]],
                  "capacity": [[0, 1.0], [600000, 1.0]]})
    chk(err is None, "queue-contested hold_key_until runs (err=%r)" % err)
    if err is None:
        chk(key_events(got) == [
            "key_down:S@0", "key_up:S@100",
            "key_down:S@100", "key_up:S@140"],
            "the queued tap acquires S exactly at the practical's release")
        chk(got["finalVars"].get("reached") is True,
            "hold_key_until stores the hit")

    # any-join: the timer wins while pulse_key is mid-cadence — the loser
    # is abandoned at its seam, no store write, taps stop at the instant.
    got, err = run_script([par([
        [{"id": "a1", "type": "pulse_key",
          "params": {"key": "W", "cond": {"$expr": {"lit": False}},
                     "max_ms": 5000, "on_ms": 11, "off_ms": 1,
                     "confirm": 1, "store": "caught"}}],
        [sleep("b1", 30)]], join="any", store="won")],
        variables=[{"name": "caught", "type": "bool", "initial": False},
                   {"name": "won", "type": "bool", "initial": False}])
    chk(err is None, "any-join cancels pulse_key mid-cadence (err=%r)" % err)
    if err is None:
        chk(got["finalVars"].get("won") is True
            and got["finalVars"].get("caught") is False,
            "the cancelled practical never writes its store")
        ev = key_events(got)
        chk(ev and ev[-1].startswith("key_up:W@"),
            "a mid-tap W hold is stripped on cancellation: %r" % (ev[-2:],))
        chk(not any(int(e.rsplit("@", 1)[1]) > 30 and e.startswith("key_down")
                    for e in ev),
            "no W press happens after the cancelling instant")


def main():
    test_golden_determinism()
    test_whitelist()
    test_arbitration()
    test_stop_semantics()
    test_capabilities()
    test_conc_table()
    test_practicals_in_branch()
    print()
    print("PARALLEL:", "ALL PASS" if not FAILS
          else "%d FAILURES" % len(FAILS))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
