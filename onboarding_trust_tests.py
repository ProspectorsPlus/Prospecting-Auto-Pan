#!/usr/bin/env python3
"""Prospector Lite onboarding + trust suite.

Covers the first-run wizard, the capability registry, the trust manifest,
the mandatory-TLS policy, the calibration registry, readiness probes and
the local-data actions. Complements public_release_tests.py (the release
gate): that file scans the tree, this file exercises the trust machinery.

Usage:
    python3 onboarding_trust_tests.py            # run everything
    python3 onboarding_trust_tests.py --child X  # internal (isolated runtime)

Runtime checks run in child processes with PP_DATA_DIR pointing at a
throwaway temp dir and the socket module disabled, so they can never touch
the developer's real config or the network.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
FAILS = []


def chk(cond, msg):
    if cond:
        print("  [PASS] %s" % msg)
    else:
        FAILS.append(msg)
        print("  [FAIL] %s" % msg)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8",
              errors="replace") as f:
        return f.read()


def tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout
    return [l for l in out.splitlines() if l.strip()]


# --------------------------------------------------------------------------
# 1. Capability registry sanity (pure static import; no OS calls)
# --------------------------------------------------------------------------

def t_registry():
    print("[capability registry]")
    sys.path.insert(0, ROOT)
    import lite_trust
    caps = lite_trust.CAPABILITIES
    ids = [c["id"] for c in caps]
    chk(len(ids) == len(set(ids)), "capability ids are unique")
    required_fields = ("id", "title", "short_description",
                       "detailed_explanation", "platforms", "required_level",
                       "features_enabled", "data_accessed", "data_retained",
                       "network_behaviour", "permission_category",
                       "operating_system_label", "request_strategy",
                       "detection_strategy", "test_strategy",
                       "revoke_instructions", "declined_behaviour",
                       "source_references", "local_document")
    ok = all(all(f in c for f in required_fields) for c in caps)
    chk(ok, "every capability carries the full definition schema")
    levels = {c["id"]: c["required_level"] for c in caps}
    for never in ("microphone", "camera", "location", "admin_privileges",
                  "full_disk_access"):
        chk(levels.get(never) == "NOT_REQUIRED",
            "%s is explicitly NOT_REQUIRED" % never)
    mac_perms = sorted(set((c["permission_category"] or {}).get("mac")
                           for c in caps
                           if (c["permission_category"] or {}).get("mac")))
    chk(mac_perms == ["Accessibility", "Input Monitoring",
                      "Screen Recording"],
        "exactly three macOS permission categories: %s" % mac_perms)
    chk(all(set(c["platforms"]) <= {"mac", "win"} for c in caps),
        "platforms limited to mac/win")
    core = [c["id"] for c in caps
            if c["required_level"] == "REQUIRED_FOR_CORE"]
    chk(sorted(core) == ["input_control", "screen_detection",
                         "stop_hotkeys"],
        "REQUIRED_FOR_CORE is exactly screen/input/hotkeys")
    opt = [c["id"] for c in caps if c["required_level"] == "OPTIONAL"]
    chk(sorted(opt) == ["coach_ai", "discord_notifications"],
        "OPTIONAL is exactly the two network features")
    for c in caps:
        if c["required_level"] == "OPTIONAL":
            chk("network" in (c["network_behaviour"] or "").lower()
                or "https" in (c["network_behaviour"] or "").lower(),
                "%s is documented as a network feature" % c["id"])
    docs = {c["local_document"] for c in caps}
    for d in docs:
        chk(os.path.isfile(os.path.join(ROOT, d)),
            "local document %s exists in the repo" % d)
    # settings deep links only for the three OS permissions
    chk(set(lite_trust._MAC_SETTINGS_LINKS) ==
        {"screen_detection", "input_control", "stop_hotkeys"},
        "settings deep links exist for exactly the three permissions")


# --------------------------------------------------------------------------
# 2. Trust manifest: every reference resolves; exact-commit links only
# --------------------------------------------------------------------------

def t_manifest():
    print("[trust manifest]")
    import lite_trust
    try:
        man = lite_trust.generate_manifest(
            version="test", project_url="https://example.invalid/owner/repo")
        err = None
    except Exception as e:          # noqa: BLE001 - report, don't crash
        man, err = None, e
    chk(err is None, "manifest generates with zero dead references (%s)"
        % (err or "ok"))
    if not man:
        return
    refs = [r for c in man["capabilities"] for r in c["references"]]
    chk(len(refs) >= 20, "manifest carries %d source references" % len(refs))
    chk(all(r["line_start"] for r in refs if r["symbol"] != "(module)"),
        "every symbol reference resolved to a line range")
    commit = man["generated_from"]
    chk(bool(re.fullmatch(r"[0-9a-f]{40}", commit or "")),
        "manifest pins a full commit hash")
    urls = [r["url"] for r in refs if r["url"]]
    chk(len(urls) == len(refs),
        "with a project URL configured, every reference gets a URL")
    chk(all(("/blob/%s/" % commit) in u for u in urls),
        "every URL pins the exact commit")
    chk(not any("/blob/main/" in u or "/blob/master/" in u for u in urls),
        "no URL points at a moving branch")
    man2 = lite_trust.generate_manifest(version="test", project_url="")
    refs2 = [r for c in man2["capabilities"] for r in c["references"]]
    chk(all(not r["url"] for r in refs2),
        "without a project URL, no URL is invented (local fallback)")
    for f in {r["file"] for r in refs}:
        chk(os.path.isfile(os.path.join(ROOT, f)),
            "referenced file exists: %s" % f)


# --------------------------------------------------------------------------
# 3. Static bans: no TLS bypass, no overstated licence wording
# --------------------------------------------------------------------------

def t_static():
    print("[static bans]")
    files = tracked_files()
    # CODE must carry zero TLS bypasses. Docs may NAME the token -- that is
    # exactly the "verify with git grep" instruction we want users to run.
    bad_tls = []
    for rel in files:
        if rel in (os.path.basename(__file__), "public_release_tests.py"):
            continue        # the scanners themselves quote the token
        if not rel.endswith((".py", ".spec", ".command", ".bat", ".yml")):
            continue
        text = read(rel)
        for tok in ("_create_unverified_context", "ssl.CERT_NONE"):
            for i, line in enumerate(text.splitlines(), 1):
                if tok in line:
                    bad_tls.append("%s:%d %s" % (rel, i, tok))
    chk(not bad_tls, "no TLS-verification bypass in code: %s" % bad_tls)
    for rel in ("prospecting_app.py", "windows/prospecting_app.py"):
        t = read(rel)
        hits = [l for l in t.splitlines()
                if re.search(r"open[- ]source", l, re.I)]
        chk(not hits, "%s makes no open-source claim (%d hits)"
            % (rel, len(hits)))
    allow = re.compile(r"licen[cs]e|not |until |no longer|packages|"
                       r"third.party", re.I)
    for rel, banned in (("README.md", "open-source desktop macro"),
                        ("SUPPORT.md", "open-source project"),
                        ("windows/README.txt", "open-source")):
        hits = [l.strip() for l in read(rel).splitlines()
                if banned in l and not allow.search(l)]
        chk(not hits,
            "%s uses %r only in licence-status context" % (rel, banned))
    # verified-TLS helpers actually exist where the senders live
    chk("_tls_context" in read("prospecting_app.py"),
        "app TLS helper present")
    chk("_webhook_tls_context" in read("prospector_engine/engine.py"),
        "engine TLS helper present")
    chk("NOTIFY_SCREENSHOT  = False" in read("prospector_engine/engine.py"),
        "webhook screenshot attachment defaults OFF")


# --------------------------------------------------------------------------
# 4. TLS context objects really verify
# --------------------------------------------------------------------------

def t_tls():
    print("[tls contexts]")
    import ssl
    sys.path.insert(0, ROOT)
    from prospector_engine import engine as eng
    ctx = eng._webhook_tls_context()
    chk(ctx.verify_mode == ssl.CERT_REQUIRED,
        "engine webhook context requires certificates")
    chk(ctx.check_hostname is True,
        "engine webhook context checks hostnames")


# --------------------------------------------------------------------------
# 5. Onboarding state machine (temp dir, direct)
# --------------------------------------------------------------------------

def t_state_machine():
    print("[onboarding state machine]")
    import lite_onboarding as lo
    with tempfile.TemporaryDirectory() as d:
        ob = lo.Onboarding(d, "mac", version="t")
        chk(ob.state["state"] == "NOT_STARTED", "starts NOT_STARTED")
        ob.mark("TRUST_COMPLETE")
        chk(ob.state["state"] == "TRUST_COMPLETE", "forward mark works")
        ob.mark("WELCOME_COMPLETE")
        chk(ob.state["state"] == "TRUST_COMPLETE",
            "backward mark is refused")
        ob.mark("FINISHED")
        chk(ob.finished() and ob.state["completed_at"] > 0,
            "FINISHED stamps completion time")
        ob2 = lo.Onboarding(d, "mac", version="t")
        chk(ob2.finished(), "state survives reload")
        ob2.rerun()
        chk(ob2.state["state"] == "WELCOME_COMPLETE",
            "rerun reopens at the trust step")
        ob2.reset()
        chk(ob2.state["state"] == "NOT_STARTED", "reset returns to start")
        ob2.decline_optional("discord_notifications")
        chk("discord_notifications" in ob2.state["declined_optional"],
            "optional decline recorded")
        ob2.decline_optional("discord_notifications", False)
        chk("discord_notifications" not in ob2.state["declined_optional"],
            "optional decline reversible")
        # corrupt file recovers to default
        with open(os.path.join(d, "onboarding_state.json"), "w") as f:
            f.write("{not json")
        ob3 = lo.Onboarding(d, "mac", version="t")
        chk(ob3.state["state"] == "NOT_STARTED",
            "corrupt state file recovers safely")
    with tempfile.TemporaryDirectory() as d:
        ob = lo.Onboarding(d, "mac", version="t")
        chk(os.path.isfile(os.path.join(d, "onboarding_state.json")),
            "state file is persisted eagerly on first construction "
            "(bridge-liveness marker + honest migrate guard)")
        chk(ob.migrate_legacy(True) and ob.finished(),
            "legacy WELCOME_SEEN migrates straight to FINISHED")
        ob4 = lo.Onboarding(d, "mac", version="t")
        chk(not ob4.migrate_legacy(True),
            "migration is one-time (idempotent)")
        chk(not lo.Onboarding(d, "mac", version="t").migrate_legacy(True),
            "a state file on disk at construction always blocks migration")


# --------------------------------------------------------------------------
# 6. Calibration registry (static + status evaluation)
# --------------------------------------------------------------------------

def t_cal_registry():
    print("[calibration registry]")
    import lite_onboarding as lo
    ids = [c["id"] for c in lo.CALIBRATION_ITEMS]
    chk(len(ids) == len(set(ids)), "calibration ids unique")
    req = sorted(c["id"] for c in lo.CALIBRATION_ITEMS if c["required"])
    chk(req == ["cap_bar", "deposit_prompt", "pan_prompt", "roblox_window",
                "shake_prompt"],
        "required set matches the engine's every-cycle needs")
    engine_src = read("prospector_engine/engine.py") + \
        read("prospector_engine/sensing.py")
    for c in lo.CALIBRATION_ITEMS:
        for k in c["keys"]:
            chk(k in engine_src,
                "config key %s (item %s) is read by the engine"
                % (k, c["id"]))
    # status semantics
    st = lo.calibration_status({"AUTO_CALIBRATE": True})
    chk(all(st[i]["status"] == "auto" for i in req if i != "roblox_window"),
        "AUTO_CALIBRATE=default => required items report 'auto'")
    ready, blockers = lo.calibration_ready(st)
    chk(ready and not blockers, "auto state is runnable (matches engine)")
    manual = {"AUTO_CALIBRATE": False,
              "CAP_FULL_PIXEL": [1120, 900], "CAP_LEFT_PIXEL": [680, 900],
              "CAP_BAR_WIDTH": 440, "PAN_PIX": [847, 981],
              "DEPOSIT_PIX": [770, 981], "SHAKE_PIX": [830, 981]}
    st = lo.calibration_status(dict(manual), health={"ok": False})
    chk(all(st[i]["status"] == "stale" for i in req
            if i != "roblox_window"),
        "manual values + moved window => 'stale'")
    ready, blockers = lo.calibration_ready(st)
    chk(not ready and blockers, "stale required calibration blocks a run")
    st = lo.calibration_status(dict(manual), health={"ok": True})
    ready, _ = lo.calibration_ready(st)
    chk(ready, "manual + real values + matching window is ready")
    # a hand-edited config with auto OFF but no real values must never
    # show a false 'User-calibrated' green
    st = lo.calibration_status({"AUTO_CALIBRATE": False,
                                "CAP_FULL_PIXEL": [0, 0]})
    chk(all(st[i]["status"] == "unset" for i in req
            if i != "roblox_window"),
        "manual WITHOUT values => 'unset', never a fake 'ok'")
    ready, _ = lo.calibration_ready(st)
    chk(not ready, "missing manual values block a run")
    st = lo.calibration_status({"EARN_TRACK": True})
    chk(st["money_region"]["status"] == "unset",
        "enabled feature without its region reports 'unset'")
    st = lo.calibration_status({})
    chk(st["money_region"]["status"] == "off",
        "disabled feature reports 'off', never a fake green")
    # example-asset manifest honesty
    man = json.loads(read("assets/onboarding/calibration/manifest.json"))
    chk(sorted(man["items"].keys()) == sorted(ids),
        "example-asset manifest covers every calibration item")
    for iid, e in man["items"].items():
        if e.get("approved"):
            p = os.path.join(ROOT, "assets/onboarding/calibration",
                             e.get("file") or "")
            chk(e.get("file") and os.path.isfile(p),
                "approved example %s has a real file" % iid)
        chk(bool(e.get("alt")), "example %s carries alt text" % iid)


# --------------------------------------------------------------------------
# 7. UI wiring markers (both app copies stay in lockstep on the new UI)
# --------------------------------------------------------------------------

def t_ui_markers():
    print("[ui markers]")
    for rel in ("prospecting_app.py", "windows/prospecting_app.py"):
        t = read(rel)
        for marker, why in (
                ('id="setup"', "setup wizard overlay"),
                ('id="supRail"', "step rail"),
                ('data-step="ready"', "readiness step in the rail"),
                ('id="ptrust"', "Trust Center panel"),
                ('id="tmtrust"', "Trust Center menu item"),
                ("perm:", "launch permission-gate result handling"),
                ("cal:", "launch calibration-gate result handling"),
                ('role="tablist" aria-label="Platform"', "platform tabs"),
                ("__hotkeyResult", "safe-stop test callback"),
                ("__keyTestResult", "input-test worker-result callback"),
                ("webhook_payload_preview", "payload preview"),
                ("trust_view_code", "view-code plumbing"),
                ('id="welAgain" checked', "welcome checkbox defaults ON"),
                ("welcome_set_always_show", "immediate checkbox persistence"),
                ('id="trustRefresh"', "manual refresh button"),
                ('data-act="recheck"', "check-again affordance"),
                ('data-act="relaunch"', "restart-to-apply affordance"),
                ("requires_restart", "restart-required state plumbing"),
                ("updateCards", "non-destructive status refresh"),
                ("document.hasFocus()", "focus-transition refresh watcher"),
                ("log_js_error", "JS error forwarding to the local log")):
            chk(marker in t, "%s has %s" % (rel, why))
        # every config rewrite must be atomic AND routed through the one
        # fsync'd writer: the truncate pattern is banned, and the inline
        # tmp+replace pattern may appear exactly once (the helper itself)
        chk('with open(CONFIG_FILE, "w")' not in t,
            "%s has no non-atomic CONFIG_FILE writes" % rel)
        chk(t.count('CONFIG_FILE + ".tmp"') == 1,
            "%s routes every config write through _save_config_atomic"
            % rel)


# --------------------------------------------------------------------------
# 8. Engine calibration write semantics (stabilization pass)
# --------------------------------------------------------------------------

def t_engine_cal_writes():
    print("[engine calibration writes]")
    import importlib
    sys.path.insert(0, ROOT)
    from prospector_engine import sensing as sn
    from prospector_engine import settings as st
    from prospector_engine import engine as eng
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "cfg.json")
        # atomic_write keeps a live file at ALL times: .bak is a COPY of
        # the previous content, never a rename-away of the live config
        st.atomic_write(cfg, {"A": 1})
        st.atomic_write(cfg, {"A": 2})
        chk(json.load(open(cfg))["A"] == 2, "atomic_write lands new doc")
        chk(json.load(open(cfg + ".bak"))["A"] == 1,
            "rolling .bak holds the previous content")
        # optional-only interactive save must NOT flip AUTO_CALIBRATE...
        s = sn.Sensing(eng, sn.FileStore(cfg))
        st.atomic_write(cfg, {"AUTO_CALIBRATE": True})
        s.save_pixels({"DIG_TRIGGER_PIXEL": [500, 400]})
        doc = json.load(open(cfg))
        chk(doc.get("AUTO_CALIBRATE") is True,
            "optional-only save keeps AUTO_CALIBRATE on")
        chk(all(k in (doc.get("PIXEL_RATIOS") or {})
                for k in sn.AUTHORITATIVE_PIXEL_KEYS),
            "optional-only save seeds required ratios (no starved auto)")
        # ...while a core save still flips it (the pinned contract)
        s.save_pixels({"CAP_FULL_PIXEL": [900, 760],
                       "CAP_LEFT_PIXEL": [500, 760]})
        doc = json.load(open(cfg))
        chk(doc.get("AUTO_CALIBRATE") is False,
            "core save still forces AUTO_CALIBRATE off (contract kept)")
    # one auto-calibration profile: engine constant == app constant ==
    # the shipped default config (a stale engine copy once placed the
    # capacity bar ~4%/9% off for ratio-less configs)
    import prospecting_app as papp
    shipped = json.loads(read("windows/prospecting_config.json"))
    chk(sn.PIXEL_RATIOS_DEFAULT == papp.PIXEL_RATIOS_DEFAULT,
        "engine ratio profile matches the app profile")
    chk(sn.PIXEL_RATIOS_DEFAULT == shipped.get("PIXEL_RATIOS"),
        "engine ratio profile matches the shipped default config")


# --------------------------------------------------------------------------
# runtime children (isolated home, sockets denied)
# --------------------------------------------------------------------------

def _deny_network():
    """Monkeypatch the socket module so any network attempt in a child
    test raises instead of connecting."""
    import socket as _s

    def _deny(*a, **k):
        raise AssertionError("network attempt during onboarding/trust test")

    class _DenySock(_s.socket):
        def connect(self, *a, **k):
            _deny()

        def connect_ex(self, *a, **k):
            _deny()

    _s.socket = _DenySock
    _s.create_connection = _deny
    _s.getaddrinfo = _deny


def run_child(name):
    env = dict(os.environ)
    home = tempfile.mkdtemp(prefix="pp_trust_")
    env["PP_DATA_DIR"] = home
    env["PPENGINE_HOME"] = home
    env["PP_NO_HUD"] = "1"
    r = subprocess.run([sys.executable, os.path.abspath(__file__),
                        "--child", name], env=env, cwd=ROOT,
                       capture_output=True, text=True, timeout=900)
    ok = r.returncode == 0
    chk(ok, "child '%s' passed offline" % name)
    if not ok:
        print(r.stdout[-3000:])
        print(r.stderr[-3000:])


def child_api_flow():
    _deny_network()
    sys.path.insert(0, ROOT)
    import prospecting_app as app
    api = app.Api()
    w = api.welcome_state()
    assert w["setup_needed"] is True, "fresh home needs setup"
    api.welcome_done()
    assert api.onboarding_state()["state"] == "WELCOME_COMPLETE"
    api.onboarding_mark("TRUST_COMPLETE")
    api.onboarding_mark("CALIBRATION_COMPLETE")
    api.onboarding_mark("READINESS_COMPLETE")
    api.onboarding_mark("FINISHED")
    w2 = api.welcome_state()
    assert w2["setup_needed"] is False, "finished wizard stays finished"
    # trust state
    ts = api.trust_state()
    assert len(ts["capabilities"]) == 11
    assert ts["identity"]["version"] == app.VERSION
    assert ts["data_dir"] == app.DATA_DIR
    assert all("live" in c for c in ts["capabilities"])
    # payload preview parity with the engine builder
    pv = api.webhook_payload_preview()
    assert pv["ok"], pv
    from prospector_engine import engine as eng
    expected = eng._webhook_payload("stats", "Example: 120 pans, 96/hr",
                                    {"cycles": 120, "pans_per_hr": 96,
                                     "runtime_s": 4500, "recoveries": 1})
    assert pv["payload"] == expected, "preview differs from engine payload"
    assert pv["enabled"] is False and pv["screenshot_optin"] is False
    assert "secret" not in json.dumps(pv["payload"]).lower()
    # readiness probes that must pass in an isolated home
    rc = api.readiness_check()
    by = {i["id"]: i for i in rc["items"]}
    assert by["datadir"]["status"] == "pass", by["datadir"]
    assert by["settings"]["status"] == "pass", by["settings"]
    assert by["network"]["status"] == "pass", by["network"]
    assert "_READINESS_PROBE" not in app.load_saved(), "probe key cleaned"
    # data manifest + scoped deletion
    dm = api.data_manifest()
    names = [f["name"] for f in dm["files"]]
    assert "prospecting_config.json" in names
    with open(os.path.join(app.DATA_DIR, "run_history.json"), "w") as f:
        f.write("[]")
    api.delete_local_data("history")
    assert not os.path.exists(os.path.join(app.DATA_DIR,
                                           "run_history.json"))
    assert os.path.exists(app.CONFIG_FILE), "config untouched by deletion"
    # wizard reset leaves config alone
    api.delete_local_data("wizard")
    assert api.onboarding_state()["state"] == "NOT_STARTED"
    assert os.path.exists(app.CONFIG_FILE)
    # owner example tools refuse for non-owners
    r = api.owner_example_capture("cap_bar")
    assert not r.get("ok"), "owner capture must refuse non-owners"
    # example lookup is honest
    ex = api.calibration_example("cap_bar")
    assert ex.get("placeholder") is True and "img" not in ex
    print("child_api_flow ok")


def child_launch_gate():
    _deny_network()
    sys.path.insert(0, ROOT)
    import prospecting_app as app
    import lite_trust
    real = lite_trust.capability_statuses
    lite_trust.capability_statuses = lambda *a, **k: {
        "screen_detection": {"status": "not_granted", "detail": ""},
        "input_control": {"status": "granted", "detail": ""},
        "stop_hotkeys": {"status": "granted", "detail": ""}}
    try:
        api = app.Api()
        if lite_trust.platform_key() == "mac":
            r = api.launch(None)
            assert isinstance(r, str) and r.startswith("perm:"), r
            assert "screen_detection" in r
            print("launch gate blocks on missing permission:", r)
        else:
            print("launch gate: non-mac platform, gate not applicable")
    finally:
        lite_trust.capability_statuses = real
    # and with everything granted the gate must NOT trigger (we stop before
    # any real spawn by pointing the studio store at a mode mismatch-free
    # empty world and not asserting the spawn result -- covered elsewhere)
    print("child_launch_gate ok")


def child_migration_bridge():
    _deny_network()
    sys.path.insert(0, ROOT)
    home = os.environ["PP_DATA_DIR"]
    with open(os.path.join(home, "prospecting_config.json"), "w") as f:
        json.dump({"WELCOME_SEEN": True}, f)
    import prospecting_app as app
    api = app.Api()
    w = api.welcome_state()
    assert w["setup_needed"] is False, \
        "existing users (WELCOME_SEEN) are not forced through the wizard"
    st = api.onboarding_state()
    assert st["state"] == "FINISHED" and \
        st.get("migrated_from") == "WELCOME_SEEN"
    # legacy inverse key migrated to the one positive key and removed
    assert w["show_every_launch"] is False, w
    cfg = json.load(open(os.path.join(home, "prospecting_config.json")))
    assert cfg.get("SHOW_WELCOME_EVERY_LAUNCH") is False, cfg
    assert "WELCOME_SEEN" not in cfg, cfg
    print("child_migration_bridge ok")


def child_welcome_pref():
    """The 'show at every launch' preference: default ON, immediate
    persistence, restart survival, corrupt-config recovery, and the
    welcome_done-first ordering that used to mark fresh installs
    FINISHED (the migrate_legacy self-trip)."""
    _deny_network()
    sys.path.insert(0, ROOT)
    home = os.environ["PP_DATA_DIR"]
    import prospecting_app as app
    api = app.Api()
    # ordering: welcome_done on a FRESH install, before any welcome_state
    r = api.welcome_done()
    assert r["ok"], r
    st = api.onboarding_state()
    assert st["state"] == "WELCOME_COMPLETE", \
        "self-trip: fresh install must never migrate to FINISHED (%s)" % st
    # default ON
    w = api.welcome_state()
    assert w["show"] is True and w["show_every_launch"] is True, w
    # toggle OFF persists immediately and atomically
    r = api.welcome_set_always_show(False)
    assert r["ok"] and r["value"] is False, r
    cfg = json.load(open(app.CONFIG_FILE))
    assert cfg["SHOW_WELCOME_EVERY_LAUNCH"] is False
    # simulated restart: a NEW Api against the same home reads it back
    w2 = app.Api().welcome_state()
    assert w2["show"] is False and w2["show_every_launch"] is False, w2
    # toggle back ON persists too
    assert api.welcome_set_always_show(True)["ok"]
    assert app.Api().welcome_state()["show_every_launch"] is True
    # rapid toggling ends on the last value
    for v in (False, True, False, True, False):
        api.welcome_set_always_show(v)
    assert app.Api().welcome_state()["show_every_launch"] is False
    # corrupt config -> defaults ON again (never crashes)
    with open(app.CONFIG_FILE, "w") as f:
        f.write("{corrupt json")
    w3 = app.Api().welcome_state()
    assert w3["show_every_launch"] is True, w3
    # wizard reset routes back through the welcome gate (resume state)
    api.welcome_set_always_show(False)
    api.onboarding_reset()
    w4 = api.welcome_state()
    assert w4["setup_needed"] is True and w4["resume"] == "NOT_STARTED", w4
    print("child_welcome_pref ok")


def child_trust_model():
    """The authoritative capability state model: serializable snapshots,
    monotonic seq, requested tracking, restart inference from the launch
    preflight snapshot + session tests, and hotkey single-flight."""
    _deny_network()
    sys.path.insert(0, ROOT)
    import prospecting_app as app
    import lite_trust
    api = app.Api()
    ts = api.trust_state()
    json.dumps(ts)                       # fully serializable
    assert ts["seq"] >= 1 and ts["checked_at"] > 0
    caps = {c["id"]: c for c in ts["capabilities"]}
    for cid in ("screen_detection", "input_control", "stop_hotkeys"):
        live = caps[cid]["live"]
        assert "requested" in live and "test" in live, live
        if ts["platform"] == "mac":
            assert "requires_restart" in live, live
    ts2 = api.trust_state()
    assert ts2["seq"] > ts["seq"], "seq is monotonic"
    # requested: recorded from open_settings without any OS request
    real_open = lite_trust.open_settings
    lite_trust.open_settings = lambda cid: {"ok": True}
    try:
        api.trust_open_settings("screen_detection")
    finally:
        lite_trust.open_settings = real_open
    live = {c["id"]: c for c in api.trust_state()["capabilities"]}[
        "screen_detection"]["live"]
    assert live["requested"] is True, live
    # restart inference: launch snapshot False + live True -> restart
    # required until a real test passes (mac semantics, injected states)
    if lite_trust.platform_key() == "mac":
        real_pre = lite_trust._mac_preflights
        real_launch = lite_trust._LAUNCH_PREFLIGHTS
        lite_trust._LAUNCH_PREFLIGHTS = {"screen_detection": False,
                                         "input_control": True,
                                         "stop_hotkeys": True}
        lite_trust._mac_preflights = lambda: {"screen_detection": True,
                                              "input_control": True,
                                              "stop_hotkeys": True}
        try:
            live = {c["id"]: c
                    for c in api.trust_state()["capabilities"]}[
                        "screen_detection"]["live"]
            assert live["status"] == "granted" and \
                live["requires_restart"] is True, live
            # a PASSING real test clears the restart flag
            api._record_test("screen_detection", True, "capture ok")
            live = {c["id"]: c
                    for c in api.trust_state()["capabilities"]}[
                        "screen_detection"]["live"]
            assert live["requires_restart"] is False, live
            # granted + FAILED test -> restart required again
            api._record_test("screen_detection", False, "blank frame")
            live = {c["id"]: c
                    for c in api.trust_state()["capabilities"]}[
                        "screen_detection"]["live"]
            assert live["requires_restart"] is True, live
        finally:
            lite_trust._mac_preflights = real_pre
            lite_trust._LAUNCH_PREFLIGHTS = real_launch
    # hotkey single-flight: second arm while armed is refused, and the
    # armed flag always clears afterwards
    real_await = lite_trust.await_stop_hotkey
    import time as _t
    lite_trust.await_stop_hotkey = (
        lambda timeout=8: (_t.sleep(0.4), {"ok": True, "heard": None})[1])
    try:
        r1 = api.trust_test_hotkey(1, 1)
        r2 = api.trust_test_hotkey(1, 2)
        assert r1["armed"] is True, r1
        assert r2.get("busy") is True and r2["error_code"], r2
        _t.sleep(0.8)
        assert api._hotkey_armed is False, "armed flag must clear"
    finally:
        lite_trust.await_stop_hotkey = real_await
    # welcome_state/trust bridge results carry no raw exception ever:
    # every trust_* entry point returns a dict even when the layer throws
    real_req = lite_trust.request_permission
    lite_trust.request_permission = lambda cid: (_ for _ in ()).throw(
        RuntimeError("boom"))
    try:
        r = api.trust_request("screen_detection")
        assert r["ok"] is False and "boom" in r["error"], r
    finally:
        lite_trust.request_permission = real_req
    print("child_trust_model ok")


def child_cal_guards():
    """Calibration no-crash guards: overlay-missing is an honest error,
    a not-found Roblox window is never reported found, the launch
    calibration gate fires, and the empty sample explains itself."""
    _deny_network()
    sys.path.insert(0, ROOT)
    import prospecting_app as app
    import lite_trust
    api = app.Api()
    # overlay window absent (module global is None outside main())
    r = api.start_overlay_calibrate("PAN_PIX")
    assert r.get("error") and r.get("error_code") == "PP-CAL-OVERLAY", r
    r = api.start_overlay_region("MONEY")
    assert r.get("error"), r
    r = api.wizard_propose("CAP")
    assert r.get("error") and r.get("error_code") == "PP-CAL-OVERLAY", r
    r = api.start_cue_mask_capture("PAN")
    assert r.get("ok") is False and r.get("error"), r
    # detect_window returning {found: False, error: ...} must NOT read as
    # found (bool of a non-empty dict) anywhere
    s = app._sensing()
    if s is not None:
        real_dw = s.detect_window
        s.detect_window = lambda: {"found": False, "error": "not open"}
        try:
            reg = api.calibration_registry()
            rb = {i["id"]: i for i in reg["items"]}["roblox_window"]
            assert rb["live"]["status"] == "unset", rb["live"]
            rc = api.readiness_check()
            rob = {i["id"]: i for i in rc["items"]}["roblox"]
            assert rob["status"] == "info", rob
        finally:
            s.detect_window = real_dw
    # launch calibration gate: auto off + no values -> cal: refusal
    # (permission gate mocked all-granted so we reach the cal gate)
    real_caps = lite_trust.capability_statuses
    lite_trust.capability_statuses = lambda *a, **k: {
        "screen_detection": {"status": "granted", "detail": ""},
        "input_control": {"status": "granted", "detail": ""},
        "stop_hotkeys": {"status": "granted", "detail": ""}}
    try:
        with open(app.CONFIG_FILE, "w") as f:
            json.dump({"AUTO_CALIBRATE": False}, f)
        r = api.launch(None)
        assert isinstance(r, str) and r.startswith("cal:"), r
        assert "cap_bar" in r, r
    finally:
        lite_trust.capability_statuses = real_caps
    # welcome/save-failure honesty: a read-only data dir reports the
    # failed write instead of pretending it saved
    os.chmod(app.DATA_DIR, 0o500)
    try:
        r = api.welcome_set_always_show(False)
        assert r["ok"] is False and r["error"], r
    finally:
        os.chmod(app.DATA_DIR, 0o700)
    print("child_cal_guards ok")


CHILDREN = {"api_flow": child_api_flow,
            "launch_gate": child_launch_gate,
            "migration_bridge": child_migration_bridge,
            "welcome_pref": child_welcome_pref,
            "trust_model": child_trust_model,
            "cal_guards": child_cal_guards}


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--child":
        CHILDREN[sys.argv[2]]()
        return
    t_registry()
    t_manifest()
    t_static()
    t_tls()
    t_state_machine()
    t_cal_registry()
    t_ui_markers()
    t_engine_cal_writes()
    print("[runtime, network denied]")
    for name in CHILDREN:
        run_child(name)
    print()
    if FAILS:
        print("ONBOARDING/TRUST TESTS: %d FAILURES" % len(FAILS))
        for f in FAILS:
            print("  - %s" % f)
        sys.exit(1)
    print("ONBOARDING/TRUST TESTS: ALL PASS")


if __name__ == "__main__":
    main()
