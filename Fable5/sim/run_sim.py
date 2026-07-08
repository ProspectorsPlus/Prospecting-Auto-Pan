"""Monte-carlo consistency report.

    python3 -m sim.run_sim [--minutes 10] [--seeds 8]
"""
from __future__ import annotations

import argparse

from .harness import SimRun

SCENARIOS = [
    ("baseline (mild noise)", {}, {}),
    ("edge-heavy shoreline", {}, {"shake_start_depth": -0.05}),
    ("glitchy shake 12%", {}, {"p_shake_fail": 0.12}),
    ("sticky cue + flicker", {}, {"p_sticky": 0.8, "p_flicker": 0.08}),
    ("dig lag 20%", {}, {"p_dig_fail": 0.20}),
]


def stats(vals):
    m = sum(vals) / len(vals)
    var = sum((v - m) ** 2 for v in vals) / len(vals)
    return m, var ** 0.5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--seeds", type=int, default=8)
    a = ap.parse_args()
    print("%d seeds x %.0f sim-min per scenario\n" % (a.seeds, a.minutes))
    print("%-26s %14s %8s %8s %8s %6s" % ("scenario", "pans/hr (sd)",
                                          "clean%", "retries", "fails",
                                          "hard"))
    for name, cfg, sim in SCENARIOS:
        outs = [SimRun(cfg_overrides=cfg, sim_kw=sim, seed=s)
                .run(minutes=a.minutes) for s in range(a.seeds)]
        phr, sd = stats([o["pans_per_hr"] for o in outs])
        clean, _ = stats([o["clean_pct"] for o in outs])
        ret, _ = stats([o["shake_retries"] for o in outs])
        fails, _ = stats([o["shake_fails"] for o in outs])
        hard = sum(1 for o in outs if o["hard_stopped"])
        print("%-26s %8.0f (%3.0f) %7.1f%% %8.1f %8.1f %6d"
              % (name, phr, sd, clean, ret, fails, hard))


if __name__ == "__main__":
    main()
