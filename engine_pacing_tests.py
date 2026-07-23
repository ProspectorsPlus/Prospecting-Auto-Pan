"""REAL-TIME pacing parity: Classic vs the detached Studio program.

The virtual-clock suites (engine_trace_tests, studio_conformance, the
37-build sweep) pin BEHAVIOR byte-for-byte; live session-ten testing
proved that is not enough — the same Shards build ran visibly slower
imported, because the detached runner paid real costs the virtual clock
never sees: a ~16-CGEvent release_all storm between every two blocks
(_tick_release), and whole-25 ms poll sleeps where native Classic
busy-waits paced only by its own screen grabs (hold_key_until,
walk_back_x, the lifted wait_expr proofs). This suite runs the REAL
engine main() twice through engine_pacing.RealWorld — same wall clock,
same scripted screen, same fixture entry config; once native Classic,
once the compiled Shards FR Latest program — and asserts the detached
run is the same run: same inputs, same order, gap-for-gap within
real-hardware jitter, no boundary event storm, micro-scale scheduler
overhead between blocks.

Scenario margins matter: every decision instant in
pace-shards-happy.scenario.json sits >=40 ms from every threshold it
meets, because a choreography that parks a branch 2 ms from a window
edge (as classic-shards-live does with the R2 no-progress window)
measures coin flips, not pacing.

Each mode runs up to two attempts: one clean identical pair proves
parity; two behavior mismatches in a row fail honestly. Wall clock:
about 6-12 s total.

Dev suite only: not shipped, not in the zip. Run: python3
engine_pacing_tests.py -> must end with 'PACING TESTS: ALL PASS'.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_pacing as EP  # noqa: E402

FAILS = []


def chk(ok, msg):
    print("  [%s] %s" % ("PASS" if ok else "FAIL", msg))
    if not ok:
        FAILS.append(msg)


FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "engine_goldens", "pacing")
SCEN = os.path.join(FIX, "pace-shards-happy.scenario.json")
TRIM_MS = 1300          # the scheduled quit: compare the running window

# real-hardware jitter budget (sleep wake error + modeled grab pacing);
# the pre-fix defects sat far outside every one of these: the release
# storm pushed boundary/floor counts two orders of magnitude up, the
# 25 ms poll sleeps put 20-25 ms deltas on every cue seam.
GAP_MEDIAN_MS = 5.0
GAP_P95_MS = 15.0
GAP_MAX_MS = 30.0
BOUNDARY_MEDIAN_MS = 1.0
BOUNDARY_P95_MS = 3.0
DETACHED_FLOOR_MAX = 100     # ~3 full release floors; the storm was 16/block


def load_fixtures():
    with open(os.path.join(FIX, "shards_fr_latest.config.json")) as f:
        cfg = json.load(f)["config"]
    with open(os.path.join(FIX, "shards_fr_latest.program.json")) as f:
        program = json.load(f)
    return cfg, program


def one_pair(cfg, program, tag):
    _, cw = EP.run_real(SCEN, alias="pt_c_%s" % tag, base_config=cfg)
    _, dw = EP.run_real(SCEN, alias="pt_d_%s" % tag, base_config=cfg,
                        program=program)
    ce = EP.normalize(cw, until_ms=TRIM_MS)
    de = EP.normalize(dw, until_ms=TRIM_MS)
    return cw, dw, ce, de


def main():
    cfg, program = load_fixtures()

    # a clean pair: same behavior signature end to end AND within the gap
    # budgets. One full-pair retry absorbs a genuine machine-load spike
    # (parallel builds/tests on the same box push sleep wake-ups past the
    # budgets — observed live in session ten on an untouched tree); two
    # consecutive failures fail honestly.
    cw = dw = ce = de = None
    for attempt in (1, 2):
        cw, dw, ce, de = one_pair(cfg, program, "a%d" % attempt)
        deltas_try, div = EP.gap_deltas(ce, de)
        st_try = EP.summarize_deltas(deltas_try)
        if div is None and st_try["median"] <= GAP_MEDIAN_MS \
                and st_try["p95"] <= GAP_P95_MS and st_try["max"] <= GAP_MAX_MS:
            break
        if attempt == 1:
            print("  [note] attempt 1 %s -- retrying once"
                  % ("diverged at event %d" % div if div is not None
                     else "over gap budget (median %.2f p95 %.2f max %.2f)"
                     % (st_try["median"], st_try["p95"], st_try["max"])))
    deltas, div = EP.gap_deltas(ce, de)

    chk(div is None,
        "identity: detached run IS the classic run (same inputs, same "
        "order, %d events)" % len(ce))
    chk(len(ce) >= 30,
        "identity: the window really exercised the cycle (%d events >= 30)"
        % len(ce))

    st = EP.summarize_deltas(deltas)
    chk(st["median"] <= GAP_MEDIAN_MS,
        "gaps: median |detached-classic| %.2f ms <= %.1f ms"
        % (st["median"], GAP_MEDIAN_MS))
    chk(st["p95"] <= GAP_P95_MS,
        "gaps: p95 %.2f ms <= %.1f ms (pre-fix: 25 ms poll tax on every "
        "cue seam)" % (st["p95"], GAP_P95_MS))
    chk(st["max"] <= GAP_MAX_MS,
        "gaps: max %.2f ms <= %.1f ms" % (st["max"], GAP_MAX_MS))

    # the boundary storm: physical no-op release events the game would see
    cf = len(EP.floor_noops(cw))
    df = len(EP.floor_noops(dw))
    chk(df <= DETACHED_FLOOR_MAX,
        "floor: detached run posts %d no-op release events (<= %d; the "
        "per-block release_all storm was ~16 x every block)"
        % (df, DETACHED_FLOOR_MAX))
    chk(df <= cf,
        "floor: detached (%d) posts no more no-op releases than classic "
        "(%d)" % (df, cf))

    # scheduler overhead between adjacent blocks (tick-loop lap cost)
    bg = EP.boundary_gaps(dw)
    s = sorted(g["ms"] for g in bg)
    med = EP.percentile(s, 0.5)
    p95 = EP.percentile(s, 0.95)
    chk(bool(bg) and med <= BOUNDARY_MEDIAN_MS,
        "boundaries: median block-boundary overhead %.3f ms <= %.1f ms "
        "(%d boundaries)" % (med, BOUNDARY_MEDIAN_MS, len(bg)))
    chk(bool(bg) and p95 <= BOUNDARY_P95_MS,
        "boundaries: p95 %.3f ms <= %.1f ms" % (p95, BOUNDARY_P95_MS))

    # pinned-momentum continuity: W is held across the whole rattle in
    # both modes -- exactly one down..up span per shake, no re-presses.
    def w_spans(events):
        downs = [e for e in events if e["ev"] == "key_down"
                 and e.get("key") == "W"]
        ups = [e for e in events if e["ev"] == "key_up"
               and e.get("key") == "W"]
        return len(downs), len(ups)
    chk(w_spans(ce) == w_spans(de),
        "momentum hold: same W hold structure classic %s vs detached %s"
        % (w_spans(ce), w_spans(de)))

    # cue-seam reaction: the walk-back S release answers the PAN cue at
    # the same instant in both modes (the go_water animation-skip seam).
    def s_up_after(events, t_ms):
        for e in events:
            if e["ev"] == "key_up" and e.get("key") == "S" \
                    and e["t"] >= t_ms:
                return e["t"]
        return None
    c_up = s_up_after(ce, 300)
    d_up = s_up_after(de, 300)
    chk(c_up is not None and d_up is not None
        and abs(c_up - d_up) <= GAP_P95_MS,
        "cue seam: PAN-reach S release classic %.1f ms vs detached %.1f "
        "ms (|delta| <= %.1f ms; pre-fix the poll sleep held S up to "
        "25 ms longer)" % (c_up or -1, d_up or -1, GAP_P95_MS))

    # full release at exit, both worlds (the safety funnel is untouched)
    chk(cw.inputs.all_released() and dw.inputs.all_released(),
        "safety: all inputs released at exit in both modes")

    print()
    if FAILS:
        print("PACING TESTS: %d FAILURES" % len(FAILS))
        raise SystemExit(1)
    print("PACING TESTS: ALL PASS")


if __name__ == "__main__":
    main()
