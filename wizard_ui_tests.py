#!/usr/bin/env python3
"""wizard_ui_tests.py -- setup-wizard behavior suite (Python + real DOM).

Two layers:
  1. Pure-Python checks of the progression engine, the Advanced-cue-matching
     requirement and the registry composition (lite_onboarding).
  2. A jsdom run of the REAL embedded UI (build_html) driven by
     wizard_ui_tests.js with canned bridge payloads composed by the REAL
     lite_onboarding.compose_registry -- guarding that guided calibration
     stays inside the wizard, progression renders sequentially, and the
     main tutorial auto-starts exactly once after setup.

Run from the repo root:  python3 wizard_ui_tests.py
Requires node (like tour_check.py) and the repo-local jsdom.
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)

FAILS = []


def chk(cond, msg):
    print(("  ok: " if cond else "  FAIL: ") + msg)
    if not cond:
        FAILS.append(msg)


def masks(cues=("PAN", "DEPOSIT", "SHAKE")):
    return {c: {"ratio": [0.1, 0.2, 0.05, 0.02], "w": 40, "h": 12,
                "bits": "AAAA", "px": 30, "preview": ""} for c in cues}


PIXELS = {"AUTO_CALIBRATE": False,
          "CAP_FULL_PIXEL": [1120, 900], "CAP_LEFT_PIXEL": [680, 900],
          "CAP_BAR_WIDTH": 440, "PAN_PIX": [847, 981],
          "DEPOSIT_PIX": [770, 981], "SHAKE_PIX": [830, 981]}


def t_python_layer():
    print("[1] progression engine + advanced-cue requirement (pure python)")
    import lite_onboarding as lo

    # cue_masks is a REQUIRED registry item with real dependencies
    cue = lo.CAL_BY_ID["cue_masks"]
    chk(cue["required"] is True, "cue_masks is required in the registry")
    chk(cue.get("dependencies") == ["pan_prompt", "deposit_prompt",
                                    "shake_prompt"],
        "cue_masks depends on the three prompt pixels")
    req = sorted(c["id"] for c in lo.CALIBRATION_ITEMS if c["required"])
    chk(req == ["cap_bar", "cue_masks", "deposit_prompt", "pan_prompt",
                "roblox_window", "shake_prompt"],
        "required set = pixels + advanced cue matching")

    # single-pixel-only data is NEVER ready
    st = lo.calibration_status(dict(PIXELS))
    ready, blockers = lo.calibration_ready(st)
    chk(not ready and blockers == ["cue_masks"],
        "single-pixel-only calibration is not ready (cue_masks blocks)")
    # auto-calibration cannot satisfy the mask requirement either
    st = lo.calibration_status({"AUTO_CALIBRATE": True})
    chk(st["cue_masks"]["status"] == "unset",
        "cue_masks never reports 'auto' (masks cannot be auto-placed)")
    ready, _ = lo.calibration_ready(st)
    chk(not ready, "a fresh auto install is not ready without masks")
    # full data IS ready
    cfg = dict(PIXELS, CUE_MASKS=masks())
    ready, blockers = lo.calibration_ready(lo.calibration_status(cfg))
    chk(ready and not blockers, "pixels + all three masks is ready")
    # partial masks are not
    cfg = dict(PIXELS, CUE_MASKS=masks(("PAN",)))
    ready, _ = lo.calibration_ready(lo.calibration_status(cfg))
    chk(not ready, "a single captured mask is not enough")
    # switching the feature off blocks readiness
    cfg = dict(PIXELS, CUE_MASKS=masks(), ADVANCED_CUES=False)
    st = lo.calibration_status(cfg)
    ready, _ = lo.calibration_ready(st)
    chk(not ready and st["cue_masks"]["status"] == "unset",
        "ADVANCED_CUES switched off blocks readiness")
    # migration: a FINISHED install without masks reads NEEDS_REVIEW,
    # old values preserved (status only -- nothing is deleted)
    st = lo.calibration_status(dict(PIXELS), setup_finished=True)
    chk(st["cue_masks"]["status"] == "needs_review",
        "finished single-pixel installs mark cue_masks NEEDS_REVIEW")
    chk(st["cap_bar"]["status"] == "ok",
        "their existing pixel calibration stays valid")

    # progression rules
    p = lo.progression([
        {"id": "a", "required": True, "complete": True, "title": "A"},
        {"id": "b", "required": True, "complete": False, "title": "B"},
        {"id": "c", "required": True, "complete": False, "title": "C"},
        {"id": "o", "required": False, "complete": False, "title": "O"}])
    chk(p["a"]["state"] == "COMPLETE" and p["b"]["state"] == "ACTIVE"
        and p["c"]["state"] == "UPCOMING" and p["o"]["state"] == "OPTIONAL",
        "progression: complete -> active -> upcoming; optional never blocks")
    chk("B first" in p["c"]["reason"],
        "upcoming reason names the gating step")
    chk(p[""]["active"] == "b" and p[""]["total"] == 3,
        "summary reports the active step and totals")
    p = lo.progression([
        {"id": "a", "required": True, "complete": True, "title": "A"},
        {"id": "b", "required": True, "complete": False,
         "needs_review": True, "title": "B"},
        {"id": "c", "required": True, "complete": False, "title": "C"}])
    chk(p["b"]["state"] == "NEEDS_REVIEW" and p["c"]["state"] == "UPCOMING",
        "needs-review holds the active position")

    # composed registry (what the UI actually renders)
    reg = lo.compose_registry({"AUTO_CALIBRATE": True})
    by = {i["id"]: i for i in reg["items"]}
    chk(by["cap_bar"]["prog"]["state"] == "ACTIVE"
        and by["cap_bar"]["prog"]["seq"] == 1,
        "fresh install: Capacity is step 1 and ACTIVE")
    chk(by["cue_masks"]["prog"]["state"] == "UPCOMING"
        and by["cue_masks"]["prog"]["seq"] == 5,
        "cue matching is required step 5 (after its prompt dependencies)")
    chk(not reg["ready"] and reg["blockers"] == ["cue_masks"],
        "composed registry rejects single-pixel-only readiness")
    for i in reg["items"]:
        ins = i["instruction"]
        chk(bool(ins.get("selection_target") and ins.get("exact_action")
                 and ins.get("roblox_setup_steps")
                 and ins.get("correct_result")),
            "structured instructions complete for %s" % i["id"])
    cap = by["cap_bar"]["instruction"]
    chk("RIGHT tip" in cap["selection_target"]
        and "20" in cap["validation"],
        "Capacity instructions are exact (right/left tips, >20px width)")
    for vague in ("click the capacity", "choose the correct point",
                  "follow the screenshot"):
        chk(all(vague not in json.dumps(i["instruction"]).lower()
                for i in reg["items"]),
            "no vague instruction text: '%s'" % vague)


def t_dom_layer():
    print("[2] real-DOM wizard journey (jsdom over build_html)")
    import lite_onboarding as lo
    work = tempfile.mkdtemp(prefix="pp_wizui_")
    data = os.path.join(work, "data")
    os.makedirs(data, exist_ok=True)
    env = dict(os.environ, PP_DATA_DIR=data, PP_NO_HUD="1")
    # render the real page in a child so the import cannot leak state here
    r = subprocess.run([sys.executable, "-c",
                        "import prospecting_app as a;"
                        "open(%r, 'w').write(a.build_html());"
                        "open(%r, 'w').write(a._themed(a._OVERLAY_HTML))"
                        % (os.path.join(work, "page.html"),
                           os.path.join(work, "overlay.html"))],
                       env=env, cwd=ROOT, capture_output=True, text=True,
                       timeout=300)
    chk(r.returncode == 0, "build_html renders (%s)" % (r.stderr[-200:]
                                                        if r.returncode
                                                        else "ok"))
    # canned registries, composed by the REAL composition code
    regs = {
        "reg_fresh.json": lo.compose_registry({"AUTO_CALIBRATE": True}),
        "reg_cap_done.json": lo.compose_registry(
            {"AUTO_CALIBRATE": False, "CAP_FULL_PIXEL": [1120, 900],
             "CAP_LEFT_PIXEL": [680, 900], "CAP_BAR_WIDTH": 440}),
        "reg_review.json": lo.compose_registry(dict(PIXELS),
                                               setup_finished=True),
    }
    for name, reg in regs.items():
        with open(os.path.join(work, name), "w") as f:
            json.dump(reg, f)
    r = subprocess.run([sys.executable, "-c",
                        "import json, prospecting_app as a;"
                        "json.dump(a._tutorial_merged(),"
                        " open(%r, 'w'))"
                        % os.path.join(work, "tours.json")],
                       env=env, cwd=ROOT, capture_output=True, text=True,
                       timeout=300)
    chk(r.returncode == 0, "tutorial content renders")
    r = subprocess.run(["node", os.path.join(ROOT, "wizard_ui_tests.js"),
                        work], cwd=ROOT, capture_output=True, text=True,
                       timeout=600)
    sys.stdout.write(r.stdout)
    if r.returncode != 0:
        sys.stdout.write(r.stderr)
    chk(r.returncode == 0 and "WIZARD-UI: ALL PASS" in r.stdout,
        "jsdom wizard journey passes")


def main():
    t_python_layer()
    t_dom_layer()
    if FAILS:
        print("WIZARD TESTS: %d FAILURE(S)" % len(FAILS))
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("WIZARD TESTS: ALL PASS")


if __name__ == "__main__":
    main()
