#!/usr/bin/env python3
"""wizard_ui_tests.py -- setup-wizard behavior suite (Python + real DOM).

Two layers:
  1. Pure-Python checks of the progression engine, the Advanced-cue-matching
     requirement and the registry composition (lite_onboarding).
  2. A jsdom run of the REAL embedded UI (build_html) driven by
     wizard_ui_tests.js with canned bridge payloads composed by the REAL
     lite_onboarding.compose_registry -- guarding that guided calibration
     stays inside the wizard, progression renders sequentially, and the
     main tutorial auto-opens once per main-app entry (disabled only by
     the TUTORIAL_AUTO_OPEN preference, never by past dismissal).

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

    # ---- Fortune River stays OUT of the wizard registry -------------------
    chk(lo.CAL_BY_ID["fortune_river"].get("wizard") is False,
        "fortune_river is flagged wizard=False in the registry")
    chk(all("wizard" not in c for c in lo.CALIBRATION_ITEMS
            if c["id"] != "fortune_river"),
        "no other registry item carries the wizard key (default True)")
    fresh_ids = [i["id"] for i in
                 lo.compose_registry({"AUTO_CALIBRATE": True})["items"]]
    chk("fortune_river" not in fresh_ids,
        "fresh install: fortune_river absent from the composed wizard items")
    chk("autopan_button" in fresh_ids and "roblox_window" in fresh_ids,
        "the other items (incl. optionals + prerequisite) still compose")
    done_reg = lo.compose_registry(dict(PIXELS, CUE_MASKS=masks()),
                                   setup_finished=True)
    chk("fortune_river" not in [i["id"] for i in done_reg["items"]],
        "finished install: fortune_river absent from the composed items")
    chk(done_reg["ready"] and not done_reg["blockers"],
        "readiness unaffected: optional fortune_river never blocked")
    st = lo.calibration_status(dict(PIXELS))
    chk("fortune_river" in st,
        "calibration_status still computes fortune_river (Calibrate tab)")
    chk(lo.saved_summary({"FR_OPEN_PIXEL": [5, 6]}, "fortune_river") != "",
        "saved_summary still covers fortune_river for the tab/export")


def t_routing_table():
    print("[2] startup routing table (compute_startup_route, full product)")
    import itertools
    import lite_onboarding as lo

    # Independent restatement of the routing policy (the literal table from
    # the spec) -- NOT a call into compute_startup_route.
    def expected(explicit, studio, show_every, skip_auto, wstate, sess):
        if studio:
            return "main"           # studio host owns onboarding
        if explicit:
            return "welcome"        # explicit Welcome always enters wizard
        if sess:
            return "main"           # skipped this session
        if wstate == "FINISHED":
            if skip_auto:
                return "main"
            return "welcome" if show_every else "main"
        if skip_auto:
            return "main"           # auto-skip; warnings stay honest
        if wstate == "NOT_STARTED":
            return "welcome"
        return "welcome" if show_every else "wizard_resume"

    # spot-check literal expectations for the priority order itself
    literals = [
        ((False, True, True, False, "NOT_STARTED", False), "main"),
        ((True, True, True, True, "NOT_STARTED", True), "main"),
        ((True, False, False, True, "FINISHED", False), "welcome"),
        ((True, False, False, True, "TRUST_COMPLETE", True), "welcome"),
        ((False, False, True, False, "TRUST_COMPLETE", True), "main"),
        ((False, False, True, True, "FINISHED", False), "main"),
        ((False, False, True, False, "FINISHED", False), "welcome"),
        ((False, False, False, False, "FINISHED", False), "main"),
        ((False, False, True, True, "TRUST_COMPLETE", False), "main"),
        ((False, False, False, False, "NOT_STARTED", False), "welcome"),
        ((False, False, True, False, "TRUST_COMPLETE", False), "welcome"),
        ((False, False, False, False, "TRUST_COMPLETE", False),
         "wizard_resume"),
    ]
    for args, want in literals:
        chk(expected(*args) == want,
            "literal table row %s -> %s" % (args, want))

    bad = []
    n = 0
    for combo in itertools.product(
            (False, True), (False, True), (False, True), (False, True),
            ("NOT_STARTED", "TRUST_COMPLETE", "FINISHED"), (False, True)):
        explicit, studio, show_every, skip_auto, wstate, sess = combo
        r = lo.compute_startup_route(
            explicit_welcome=explicit, studio_launch=studio,
            show_welcome_every_launch=show_every,
            skip_wizard_automatically=skip_auto,
            wizard_state=wstate, session_skip=sess)
        n += 1
        if r.get("route") != expected(*combo) or not r.get("reason"):
            bad.append((combo, r))
    chk(n == 96 and not bad,
        "all 96 routing combinations match the policy table"
        + ("" if not bad else " :: first bad %s" % (bad[0],)))


def t_completion_via():
    print("[3] mark_completed_via semantics + reset clears the stamp")
    import lite_onboarding as lo
    d = tempfile.mkdtemp(prefix="pp_wizob_")
    ob = lo.Onboarding(d, "mac", version="test")
    chk("completion_via" not in ob.state,
        "default state carries no completion_via key")
    st = ob.mark_completed_via("marked_complete")
    chk(st["state"] == "FINISHED"
        and st.get("completion_via") == "marked_complete",
        "mark_completed_via marks FINISHED and stamps marked_complete")
    ob2 = lo.Onboarding(d, "mac", version="test")
    chk(ob2.state.get("completion_via") == "marked_complete",
        "the stamp persists across a reload")
    ob2.reset()
    chk(ob2.state["state"] == "NOT_STARTED"
        and "completion_via" not in ob2.state,
        "reset() restores the default state (stamp gone)")
    chk("completion_via" not in lo.Onboarding(d, "mac", version="test").state,
        "the cleared stamp persists across a reload too")
    ob3 = lo.Onboarding(d, "mac", version="test")
    ob3.mark("READINESS_COMPLETE")
    st = ob3.mark_completed_via("wizard")
    chk(st["state"] == "FINISHED" and st.get("completion_via") == "wizard",
        "the wizard's own completion stamps completion_via=wizard")


def t_mark_complete_readiness():
    print("[4] wizard_skip('mark_complete') does not fake readiness"
          " (real Api, isolated home)")
    work = tempfile.mkdtemp(prefix="pp_wizskip_")
    env = dict(os.environ, PP_DATA_DIR=work, PP_NO_HUD="1")
    code = (
        "import json, os\n"
        "import prospecting_app as app\n"
        "api = app.Api()\n"
        "before = api.readiness_check()\n"
        "r = api.wizard_skip('mark_complete')\n"
        "assert r.get('ok'), r\n"
        "st = api.onboarding_state()\n"
        "assert st['state'] == 'FINISHED', st\n"
        "assert st.get('completion_via') == 'marked_complete', st\n"
        "after = api.readiness_check()\n"
        "key = lambda rc: (rc['ok'],\n"
        "                  [(i['id'], i['status']) for i in rc['items']])\n"
        "assert key(before) == key(after), (key(before), key(after))\n"
        "assert 'SKIP_WIZARD_AUTOMATICALLY' not in app.load_saved(), \\\n"
        "    'mark_complete must not write the auto-skip pref'\n"
        "r = api.wizard_skip('session')\n"
        "assert r.get('ok'), r\n"
        "r = api.wizard_skip('auto')\n"
        "assert r.get('ok'), r\n"
        "assert app.load_saved().get('SKIP_WIZARD_AUTOMATICALLY') is True\n"
        "assert app.Api().welcome_state()['route'] == 'main'\n"
        "print('MARKOK')\n")
    r = subprocess.run([sys.executable, "-c", code], env=env, cwd=ROOT,
                       capture_output=True, text=True, timeout=600)
    chk(r.returncode == 0 and "MARKOK" in r.stdout,
        "readiness_check unchanged by mark_complete; skip kinds persist "
        "correctly (%s)" % ((r.stderr[-400:] or r.stdout[-400:])
                            if r.returncode else "ok"))


def t_tutorial_v3():
    print("[5] tutorial lifecycle v3: v2 migration, ACTIVE stamping, "
          "auto-open pref (real Api, isolated home)")
    work = tempfile.mkdtemp(prefix="pp_tutv3_")
    env = dict(os.environ, PP_DATA_DIR=work, PP_NO_HUD="1")
    code = (
        "import json, os\n"
        "import prospecting_app as app\n"
        "# a valid v2 file migrates in place: history kept, counters added\n"
        "json.dump({'schema': 2, 'main': 'DISMISSED', 'updated': 111,\n"
        "           'migrated_from': 'localStorage pp_tour_done'},\n"
        "          open(app._TUTORIAL_STATE_FILE, 'w'))\n"
        "d = app._tutorial_lifecycle()\n"
        "assert d['schema'] == 3 and d['main'] == 'DISMISSED', d\n"
        "assert d['updated'] == 111, d\n"
        "assert d['migrated_from'] == 'localStorage pp_tour_done', d\n"
        "assert d['seen_count'] == 1 and d['last_seen_version'] == '', d\n"
        "# a v2 NOT_STARTED file migrates with seen_count 0\n"
        "json.dump({'schema': 2, 'main': 'NOT_STARTED', 'updated': 0},\n"
        "          open(app._TUTORIAL_STATE_FILE, 'w'))\n"
        "d = app._tutorial_lifecycle()\n"
        "assert d['schema'] == 3 and d['seen_count'] == 0, d\n"
        "assert d['main'] == 'NOT_STARTED', d\n"
        "# garbage resets to the v3 default\n"
        "open(app._TUTORIAL_STATE_FILE, 'w').write('{bad')\n"
        "d = app._tutorial_lifecycle()\n"
        "assert d == {'schema': 3, 'main': 'NOT_STARTED', 'updated': 0,\n"
        "             'seen_count': 0, 'last_seen_version': ''}, d\n"
        "api = app.Api()\n"
        "st = api.tutorial_state()\n"
        "assert st['auto_open'] is True, st\n"
        "assert st['seen_count'] == 0 and st['last_seen_version'] == '', st\n"
        "# ACTIVE increments seen_count and stamps last_seen_version\n"
        "r = api.tutorial_mark('ACTIVE')\n"
        "assert r['ok'], r\n"
        "st = api.tutorial_state()\n"
        "assert st['seen_count'] == 1, st\n"
        "assert st['last_seen_version'] == app.VERSION, st\n"
        "api.tutorial_mark('DISMISSED')\n"
        "api.tutorial_mark('ACTIVE')\n"
        "st = api.tutorial_state()\n"
        "assert st['seen_count'] == 2 and st['main'] == 'ACTIVE', st\n"
        "# COMPLETED / DISMISSED never increment the counter\n"
        "api.tutorial_mark('COMPLETED')\n"
        "assert api.tutorial_state()['seen_count'] == 2\n"
        "# the persisted file carries the v3 shape\n"
        "d = json.load(open(app._TUTORIAL_STATE_FILE))\n"
        "assert d['schema'] == 3 and d['seen_count'] == 2, d\n"
        "# the auto-open pref persists like the welcome pref\n"
        "r = api.tutorial_set_auto_open(False)\n"
        "assert r['ok'] and r['value'] is False, r\n"
        "assert app.load_saved().get('TUTORIAL_AUTO_OPEN') is False\n"
        "assert app.Api().tutorial_state()['auto_open'] is False\n"
        "assert api.tutorial_set_auto_open(True)['ok']\n"
        "assert app.Api().tutorial_state()['auto_open'] is True\n"
        "print('TUTV3OK')\n")
    r = subprocess.run([sys.executable, "-c", code], env=env, cwd=ROOT,
                       capture_output=True, text=True, timeout=600)
    chk(r.returncode == 0 and "TUTV3OK" in r.stdout,
        "v2->v3 migration keeps history, ACTIVE stamps count+version, "
        "auto-open pref round-trips (%s)"
        % ((r.stderr[-400:] or r.stdout[-400:]) if r.returncode else "ok"))


def t_dom_layer():
    print("[6] real-DOM wizard journey (jsdom over build_html)")
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
    # canned diagnostics payload for scenario [H], composed by the REAL
    # rule engine (lite_diagnostics.evaluate) + the REAL host summarizer
    # (prospecting_app._diag_summarize): the shipped nudge example (9 in 12
    # cycles), a shake-late finding, and a CRITICAL permission event.
    r = subprocess.run([sys.executable, "-c",
                        "import json, lite_diagnostics as ld;"
                        "import prospecting_app as a;"
                        "ctx={'platform':'mac',"
                        " 'stats':{'cycles':12,'nudges':9,'shake_misses':6},"
                        " 'settings':{'WATER_EXTRA_BACK_MS':400},"
                        " 'capabilities':{'screen_detection':'not_granted'},"
                        " 'run_active':False};"
                        "evs=ld.merge_events([], ld.evaluate(ctx), 1000);"
                        "json.dump({'events':evs,"
                        " 'summary':a._diag_summarize(evs),'when':1000},"
                        " open(%r, 'w'))"
                        % os.path.join(work, "diag.json")],
                       env=env, cwd=ROOT, capture_output=True, text=True,
                       timeout=300)
    chk(r.returncode == 0, "canned diagnostics payload composed by the "
                           "real engine (%s)" % (r.stderr[-200:]
                                                 if r.returncode else "ok"))
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
    t_routing_table()
    t_completion_via()
    t_mark_complete_readiness()
    t_tutorial_v3()
    t_dom_layer()
    if FAILS:
        print("WIZARD TESTS: %d FAILURE(S)" % len(FAILS))
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("WIZARD TESTS: ALL PASS")


if __name__ == "__main__":
    main()
