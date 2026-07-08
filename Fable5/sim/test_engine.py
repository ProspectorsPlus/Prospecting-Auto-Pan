"""Scenario tests for the Fable engine — run headless against the simulator.

    python3 -m sim.test_engine        (from the Fable5 directory)

Each scenario asserts CONSISTENCY properties (cycles completed, clean %,
zero hard stops, no stuck inputs), not just 'it didn't crash'.
"""
from __future__ import annotations

import sys

import numpy as np

from .harness import SimRun

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s %s" % ("PASS" if cond else "FAIL", name,
                          ("— " + str(detail)) if detail else ""))


def scenario(title):
    print("\n== %s ==" % title)


# --------------------------------------------------------------------------
def test_vision_units():
    scenario("vision unit tests (synthetic frames)")
    from engine import vision
    from engine.config import Config
    cfg = Config()
    geo = vision.build_geometry(cfg)
    h, w = geo.height, geo.width
    frame = np.zeros((h, w, 4), dtype=np.uint8)

    # empty bar, no cues
    r = vision.analyze(frame, geo, cfg)
    check("empty frame -> fill 0", r.fill == 0.0, r.fill)
    check("empty frame -> no cues", max(r.dep_frac, r.pan_frac, r.shk_frac) == 0.0)

    # paint the cap band gold (BGR: gold = high R,G low B)
    y0, y1, x0, x1 = geo.cap_sl
    frame2 = frame.copy()
    frame2[y0:y1, x0:x1] = (40, 230, 253, 255)          # B,G,R,A
    r = vision.analyze(frame2, geo, cfg)
    check("gold band -> fill 1.0", r.fill > 0.99, r.fill)

    # half fill (left half gold)
    frame3 = frame.copy()
    mid = x0 + (x1 - x0) // 2
    frame3[y0:y1, x0:mid] = (40, 230, 253, 255)
    r = vision.analyze(frame3, geo, cfg)
    check("half band -> fill ~0.5", 0.45 < r.fill < 0.55, round(r.fill, 3))

    # pale anti-aliased tips only (must NOT read as fill — inset handles tips,
    # and pale gold fails the gold test)
    frame4 = frame.copy()
    frame4[y0:y1, x0:x0 + 2] = (120, 150, 160, 255)
    r = vision.analyze(frame4, geo, cfg)
    check("pale tips -> fill ~0", r.fill < 0.02, round(r.fill, 3))

    # white cue text in the pan box
    py0, py1, px0, px1 = geo.pan_sl
    frame5 = frame.copy()
    frame5[py0:py1, px0:px1] = (250, 250, 250, 255)
    r = vision.analyze(frame5, geo, cfg)
    check("white box -> pan cue frac high", r.pan_frac > 0.9, r.pan_frac)


# --------------------------------------------------------------------------
def run_seeds(n, minutes, cfg=None, sim=None):
    outs = []
    for seed in range(n):
        outs.append(SimRun(cfg_overrides=cfg, sim_kw=sim, seed=seed)
                    .run(minutes=minutes))
    return outs


def agg(outs, key):
    vals = [o[key] for o in outs]
    return sum(vals) / len(vals)


def test_baseline():
    scenario("baseline — mild real-world noise, 10 sim-min x 6 seeds")
    outs = run_seeds(6, 10)
    cyc = agg(outs, "cycles")
    clean = agg(outs, "clean_pct")
    check("throughput >= 280 cycles / 10 min", cyc >= 280, round(cyc, 1))
    check("clean cycles >= 85%", clean >= 85, round(clean, 1))
    check("zero hard stops", not any(o["hard_stopped"] for o in outs))
    check("zero safe pauses", sum(o["safe_pauses"] for o in outs) == 0)
    check("no stuck inputs", all(not o["held_at_end"] for o in outs),
          [o["held_at_end"] for o in outs])
    check("nudges rare (< 2/min avg)", agg(outs, "nudges") < 20,
          round(agg(outs, "nudges"), 1))
    print("   -> pans/hr avg %.0f | shake start-retries avg %.1f | "
          "shake fails avg %.1f"
          % (agg(outs, "pans_per_hr"), agg(outs, "shake_retries"),
             agg(outs, "shake_fails")))


def test_edge_heavy():
    scenario("edge-heavy water (deep start threshold) — micro-retry must save it")
    outs = run_seeds(6, 10, sim={"shake_start_depth": -0.05})
    cyc = agg(outs, "cycles")
    fails = agg(outs, "shake_fails")
    check("throughput holds >= 240 cycles", cyc >= 240, round(cyc, 1))
    check("full shake FAILURES stay low (<= 3 avg)", fails <= 3, round(fails, 1))
    check("zero hard stops", not any(o["hard_stopped"] for o in outs))
    print("   -> retries avg %.1f (cheap saves) | edge misses in sim: %.0f"
          % (agg(outs, "shake_retries"), agg(outs, "sim_edge_misses")))


def test_glitch_heavy():
    scenario("glitchy shake (12% random start failure)")
    outs = run_seeds(6, 10, sim={"p_shake_fail": 0.12})
    check("throughput holds >= 240 cycles", agg(outs, "cycles") >= 240,
          round(agg(outs, "cycles"), 1))
    check("zero hard stops", not any(o["hard_stopped"] for o in outs))


def test_sticky_flicker():
    scenario("sticky Shake cue (80%) + heavy flicker (8%)")
    outs = run_seeds(6, 10, sim={"p_sticky": 0.8, "sticky_s": (0.5, 1.2),
                                 "p_flicker": 0.08})
    check("throughput holds >= 240 cycles", agg(outs, "cycles") >= 240,
          round(agg(outs, "cycles"), 1))
    check("zero hard stops", not any(o["hard_stopped"] for o in outs))


def test_dig_lag():
    scenario("dig lag (20% of digs don't register)")
    outs = run_seeds(6, 10, sim={"p_dig_fail": 0.20})
    check("throughput holds >= 240 cycles", agg(outs, "cycles") >= 240,
          round(agg(outs, "cycles"), 1))
    check("zero hard stops", not any(o["hard_stopped"] for o in outs))
    check("no runaway digging (< 3 digs/cycle avg)",
          agg(outs, "digs") / max(1, agg(outs, "cycles")) < 3,
          round(agg(outs, "digs") / max(1, agg(outs, "cycles")), 2))


def test_zero_thresholds():
    scenario("REGRESSION: zero thresholds mean DISABLED (the v2 0-bug)")
    outs = run_seeds(3, 5, cfg={"SHAKE_GLITCH_LIMIT": 0, "NO_PROGRESS_SEC": 0,
                                "NO_FULL_LIMIT": 0, "SHAKE_FAIL_LIMIT": 0})
    check("still cycles normally", agg(outs, "cycles") >= 120,
          round(agg(outs, "cycles"), 1))
    check("no instant break-outs",
          sum(o["events"].get("no_progress", 0)
              + o["events"].get("shake_glitch", 0) for o in outs) == 0)
    check("zero hard stops", not any(o["hard_stopped"] for o in outs))


def test_dead_shake():
    scenario("dead shake (100% failure) — must degrade LOUDLY and SAFELY")
    r = SimRun(sim_kw={"p_shake_fail": 1.0}).run(minutes=8)
    check("hard-stopped in the end", r["hard_stopped"], r["hard_reason"])
    check("went through safe-stop retries first", r["safe_pauses"] >= 1,
          r["safe_pauses"])
    check("inputs released", not r["held_at_end"], r["held_at_end"])


def test_slow_build():
    scenario("slow build (big pan, weak shake — 100%% dig speed)")
    outs = run_seeds(3, 10,
                     cfg={"STAT_CAPACITY": 4000, "STAT_DIG_STRENGTH": 400,
                          "STAT_SHAKE_STRENGTH": 120, "STAT_SHAKE_SPEED": 300,
                          "DIG_SPEED": 100, "DIG_CLICK_MS": 550},
                     sim={"capacity": 4000, "dig_strength": 400,
                          "shake_strength": 120, "shake_speed": 300,
                          "dig_speed_pct": 100})
    # ~7 digs * ~2s + drain ~4.9s -> ~20 s/cycle -> ~30 cycles / 10 min
    check("cycles in the expected band (>= 22)", agg(outs, "cycles") >= 22,
          round(agg(outs, "cycles"), 1))
    check("zero hard stops", not any(o["hard_stopped"] for o in outs))
    check("dynamic shake budget prevented cutoffs (fails <= 1 avg)",
          agg(outs, "shake_fails") <= 1, round(agg(outs, "shake_fails"), 1))


def main():
    test_vision_units()
    test_baseline()
    test_edge_heavy()
    test_glitch_heavy()
    test_sticky_flicker()
    test_dig_lag()
    test_zero_thresholds()
    test_dead_shake()
    test_slow_build()
    print("\n===== %d passed, %d failed =====" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failed:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
