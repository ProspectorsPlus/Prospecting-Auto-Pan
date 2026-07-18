#!/usr/bin/env python3
"""Phase 04, checkpoint C0 -- characterization goldens for the REAL engine.

Replays every scenario in engine_scenarios/ through the real engine's real
main() loop in LEGACY mode under the engine_sim world, and byte-compares
the captured stdout against engine_goldens/legacy/<scenario>.txt.

A diff here means engine behavior changed. That is a stop-the-line failure
for every later extraction checkpoint: fix the change, or prove the old
behavior was wrong and regenerate deliberately with --update (and say so).

Usage:
  python3 engine_characterization.py             # verify against goldens
  python3 engine_characterization.py --update    # (re)generate goldens
  python3 engine_characterization.py --selfcheck # determinism double-run
"""
import glob
import json
import os
import sys

import engine_sim

ROOT = os.path.dirname(os.path.abspath(__file__))
GOLD = os.path.join(ROOT, "engine_goldens", "legacy")
FAILS = []


def chk(cond, msg):
    if cond:
        print("  [PASS] %s" % msg)
    else:
        FAILS.append(msg)
        print("  [FAIL] %s" % msg)


def scenario_names():
    names = []
    for p in sorted(glob.glob(os.path.join(engine_sim.SCENARIO_DIR,
                                           "*.json"))):
        name = os.path.splitext(os.path.basename(p))[0]
        # interactive scenarios pace the virtual clock against real time
        # for spawned command tests; they are not golden material
        if not engine_sim.load_scenario(name).get("interactive"):
            names.append(name)
    return names


def run_one(name, tag=""):
    transcript, world = engine_sim.run_legacy(name, alias="pold_c0_%s%s"
                                              % (name.replace("-", "_"), tag))
    return transcript, world


def main():
    update = "--update" in sys.argv
    selfcheck = "--selfcheck" in sys.argv
    os.makedirs(GOLD, exist_ok=True)
    names = scenario_names()
    if not names:
        print("no scenarios found"); sys.exit(1)
    for name in names:
        print("[scenario] %s" % name)
        transcript, world = run_one(name)
        # -- world invariants (true in every scenario) --------------------
        chk(world.inputs.all_released(),
            "%s: all inputs released at engine exit" % name)
        scen = engine_sim.load_scenario(name)
        want = (scen.get("expect") or {}).get("webhook_events_include") or []
        got = [e for (_t, e, _m) in world.webhooks]
        for ev in want:
            chk(ev in got, "%s: webhook event '%s' fired (got %s)"
                % (name, ev, got))
        # -- golden compare ------------------------------------------------
        gpath = os.path.join(GOLD, name + ".txt")
        if selfcheck:
            t2, _ = run_one(name, tag="_again")
            chk(t2 == transcript, "%s: deterministic across two runs" % name)
        if update:
            with open(gpath, "w", encoding="utf-8") as f:
                f.write(transcript)
            print("  [gold] wrote %s (%d bytes)" % (gpath, len(transcript)))
            continue
        if not os.path.exists(gpath):
            FAILS.append("%s: golden missing (run --update once)" % name)
            print("  [FAIL] golden missing: %s" % gpath)
            continue
        with open(gpath, encoding="utf-8") as f:
            golden = f.read()
        if transcript == golden:
            print("  [PASS] %s: transcript byte-identical to golden (%d bytes)"
                  % (name, len(golden)))
        else:
            FAILS.append("%s: transcript diverged from golden" % name)
            print("  [FAIL] %s: transcript differs from golden" % name)
            gl = golden.splitlines()
            tl = transcript.splitlines()
            for i in range(max(len(gl), len(tl))):
                a = gl[i] if i < len(gl) else "<missing>"
                b = tl[i] if i < len(tl) else "<missing>"
                if a != b:
                    print("    first diff at line %d" % (i + 1))
                    print("      golden: %s" % a[:160])
                    print("      got:    %s" % b[:160])
                    break
    print()
    if FAILS:
        print("CHARACTERIZATION: %d FAILURES" % len(FAILS))
        sys.exit(1)
    print("CHARACTERIZATION: ALL PASS")


if __name__ == "__main__":
    main()
