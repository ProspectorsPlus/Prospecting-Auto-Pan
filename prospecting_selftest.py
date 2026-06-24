"""
prospecting_selftest.py -- offline tests for the perception + decision core.

Run anywhere numpy is installed:
    python3 prospecting_selftest.py

Regression harness: every weird situation we hit should get added here so
fixes never silently break old behaviour.
"""

import numpy as np
import prospecting_core as core
from prospecting_core import (Config, Decider, classify_region, capacity_full,
                              DIRT, LIQUID, UNKNOWN, DIG, EMPTY, RECOVER, SAFE)

CFG = Config()
_fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


def patch(rgbs, n=400):
    rng = np.random.default_rng(0)
    base = np.array(rgbs, dtype=np.float64)
    idx = rng.integers(0, len(base), n)
    return np.clip(base[idx] + rng.normal(0, 4, (n, 3)), 0, 255)


# ---- real logged feet samples (volcano) ------------------------------------
DIRT_SAMP = [(250, 224, 175), (249, 222, 150), (247, 209, 130), (248, 213, 137),
             (249, 217, 142), (253, 242, 196), (250, 226, 177), (254, 251, 213)]
LAVA_SAMP = [(242, 167, 59), (238, 132, 50), (237, 112, 45), (238, 125, 48)]
GREEN_PARTICLE = [(191, 253, 80), (158, 233, 71), (203, 253, 80)]


def test_perception():
    print("perception:")
    check("pure dirt -> DIRT", classify_region(patch(DIRT_SAMP), CFG)["position"] == DIRT)
    check("pure lava -> LIQUID", classify_region(patch(LAVA_SAMP), CFG)["position"] == LIQUID)
    r = classify_region(patch(GREEN_PARTICLE), CFG)
    check("green particles -> rejected (UNKNOWN/low cov)",
          r["position"] == UNKNOWN or r["coverage"] < 0.4, r)
    mixed = np.vstack([patch(DIRT_SAMP, 300), patch(GREEN_PARTICLE, 60),
                       patch([(20, 20, 25)], 40)])
    check("dirt + particles + dark blob -> DIRT", classify_region(mixed, CFG)["position"] == DIRT)
    half = np.vstack([patch(DIRT_SAMP, 200), patch(LAVA_SAMP, 200)])
    check("50/50 boundary -> UNKNOWN", classify_region(half, CFG)["position"] == UNKNOWN)
    occ = np.vstack([patch(DIRT_SAMP, 80), patch([(10, 8, 8)], 320)])
    check("occluded -> UNKNOWN", classify_region(occ, CFG)["position"] == UNKNOWN)


def test_capacity():
    print("capacity:")
    check("0.95 yellow -> full", capacity_full(0.95, CFG))
    check("0.10 yellow -> not full", not capacity_full(0.10, CFG))


def feed(dec, position, conf, full, n, t, dt=0.05):
    out = None
    for _ in range(n):
        out = dec.decide(position, conf, full, t)
        t += dt
    return out, t


def test_decider():
    print("decider:")
    dec = Decider(CFG); dec.reset(0.0)
    # digging on dirt
    check("on dirt, not full -> DIG", dec.decide(DIRT, 0.9, False, 0.0) == DIG)
    # confirmed full -> EMPTY (needs cap_confirm reads)
    out, t = feed(dec, DIRT, 0.9, True, CFG.cap_confirm, 0.1)
    check("confirmed full -> EMPTY", out == EMPTY)
    # after a successful empty, note_emptied resets -> DIG
    dec.note_emptied()
    check("post-empty -> DIG", dec.decide(DIRT, 0.9, False, 1.0) == DIG)
    # confirmed drift into lava (not full) -> RECOVER
    dec2 = Decider(CFG); dec2.reset(0.0)
    out, t = feed(dec2, LIQUID, 0.9, False, CFG.terrain_confirm, 0.0)
    check("confirmed drift -> RECOVER", out == RECOVER)
    # single liquid flicker while digging is ignored (memory)
    dec3 = Decider(CFG); dec3.reset(0.0)
    dec3.decide(DIRT, 0.9, False, 0.0)
    check("single liquid flicker -> still DIG", dec3.decide(LIQUID, 0.9, False, 0.05) == DIG)


def test_failsafe():
    print("fail-safe:")
    # sustained UNKNOWN -> SAFE, and it latches
    dec = Decider(CFG); dec.reset(0.0)
    out = None; t = 0.0
    for _ in range(8):
        out = dec.decide(UNKNOWN, 0.0, False, t); t += 0.4
    check("unknown too long -> SAFE", out == SAFE)
    check("SAFE latches", dec.decide(DIRT, 0.9, False, t) == SAFE)
    check("reset resumes", (dec.reset(0.0), dec.decide(DIRT, 0.9, False, 0.0))[1] == DIG)
    # low-confidence reads are ignored (treated UNKNOWN) -> not acted on
    dec2 = Decider(CFG); dec2.reset(0.0)
    check("low-conf LIQUID -> DIG (ignored)", dec2.decide(LIQUID, 0.1, False, 0.0) == DIG)
    # pan keeps reading full after repeated maneuvers -> SAFE
    dec3 = Decider(CFG); dec3.reset(0.0)
    feed(dec3, DIRT, 0.9, True, CFG.cap_confirm, 0.0)   # build full streak
    out = None; t = 1.0
    for _ in range(CFG.empty_retries + 5):
        out = dec3.decide(DIRT, 0.9, True, t); t += 0.1
    check("won't-empty after retries -> SAFE", out == SAFE)


if __name__ == "__main__":
    print("=== prospecting core self-test ===")
    test_perception()
    test_capacity()
    test_decider()
    test_failsafe()
    print()
    if _fails:
        print(f"FAILED ({len(_fails)}): " + ", ".join(_fails))
        raise SystemExit(1)
    print("ALL TESTS PASSED")
