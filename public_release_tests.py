#!/usr/bin/env python3
"""Prospector Lite public-release gate.

Static scans over every TRACKED file plus network-denied runtime checks.
Fails when a prohibited brand string, tracking endpoint, access-code path,
bundled secret or unexpected network attempt reappears.

Usage:
    python3 public_release_tests.py            # run everything
    python3 public_release_tests.py --child X  # internal (isolated runtime)

Runtime checks run in child processes with PP_DATA_DIR/PPENGINE_HOME
pointing at throwaway temp dirs and the socket module disabled, so they can
never touch the developer's real config or the network.
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


def tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout
    return [l for l in out.splitlines() if l.strip()]


def read(rel):
    p = os.path.join(ROOT, rel)
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read()
    except (OSError, IsADirectoryError):
        return ""


def region(text, start, end):
    """The slice of `text` between the first `start` and the next `end`."""
    i = text.find(start)
    if i < 0:
        return ""
    j = text.find(end, i)
    return text[i:j if j > i else len(text)]


# --------------------------------------------------------------------------
# 1. Branding: the prohibited public names must not appear in the tree.
#    Allowed: this file, audit/status docs that discuss the removal, the
#    gitignore (it names old local files), and the migration block in the
#    app (it must name the OLD directory to import from it).
# --------------------------------------------------------------------------
BRAND = ["Prospectors Plus", "Prospector's Plus", "Prospectors’ Plus",
         "ProspectorsPlus", "PPLUS-", "PP Analytics", "Plus Macro"]
BRAND_FILE_ALLOW = {
    "public_release_tests.py", ".gitignore", "PUBLIC_RELEASE_STATUS.md",
    "CHANGELOG.md",
}
# Reviewer/evidence documentation: these dirs exist to DESCRIBE the bans
# (naming the removed endpoints, gate tokens and old brand as evidence), so
# the scanners exempt them wholesale -- they contain no code.
BRAND_DIR_ALLOW = ("docs/public-release/", "docs/trust-and-onboarding/")
# The build workflows carry these tokens as their own package-content scan
# patterns -- they exist to REJECT the strings, not to ship them.
SCANNER_FILES = {".github/workflows/build-windows.yml",
                 ".github/workflows/build-macos.yml"}


def _context_ok(text, token, markers):
    """Every line containing `token` must also contain one of `markers`
    (case-insensitive)."""
    return all(any(m in line.lower() for m in markers)
               for line in text.splitlines() if token in line)


def brand_ok(rel, text):
    if rel in BRAND_FILE_ALLOW or rel in SCANNER_FILES             or rel.startswith(BRAND_DIR_ALLOW):
        return True
    hits = [b for b in BRAND if b in text]
    if not hits:
        return True
    if rel in ("prospecting_app.py", "windows/prospecting_app.py"):
        # the ONLY sanctioned mentions live in the legacy-migration block
        legacy = region(text, "def _legacy_data_dir", "def _data_dir")
        outside = text.replace(legacy, "", 1)
        return not any(b in outside for b in BRAND)
    if rel in ("README.md", "PRIVACY.md", "RELEASING.md", "SECURITY.md",
               "SUPPORT.md", "BUILDING.md"):
        # public docs may name the old brand ONLY when explaining the
        # migration / history on that same line
        return all(_context_ok(text, b, ("legacy", "migrat", "histor",
                                         "formerly", "old", "pre-1.0"))
                   for b in hits)
    return False


def scan_branding(files):
    print("[branding]")
    bad = [f for f in files if not brand_ok(f, read(f))]
    chk(not bad, "no prohibited brand strings in tracked files "
        "(bad: %s)" % (bad or "none"))


# --------------------------------------------------------------------------
# 2. Tracking / phone-home endpoints and identifiers must be gone.
# --------------------------------------------------------------------------
TRACKING = ["ip-api.com", "ipify", "ipinfo.io", "icanhazip", "ifconfig.me",
            "geoip", "IOPlatformUUID", "MachineGuid", "fonts.googleapis",
            "fonts.gstatic", "discord.gg/", "prospectorsplus.github.io",
            "SYNC_URL", "_machine_id", "_report_usage",
            "UPDATE_MANIFEST_URL", "TUTORIAL_CONTENT_URL", "do_update",
            "check_update"]
# engine_contract_tests asserts the ABSENCE of SYNC_URL (a negative check),
# so the token legitimately appears there.
TRACK_FILE_ALLOW = {"public_release_tests.py", "PUBLIC_RELEASE_STATUS.md",
                    "CHANGELOG.md", ".gitignore", "engine_contract_tests.py"}


def track_ok(rel, text):
    if rel in TRACK_FILE_ALLOW or rel in SCANNER_FILES             or rel.startswith(BRAND_DIR_ALLOW):
        return True
    hits = [t for t in TRACKING if t in text]
    if not hits:
        return True
    if rel in ("prospecting_app.py", "windows/prospecting_app.py"):
        # SYNC_URL may appear only inside the legacy-key scrub tuple/comments
        other = [t for t in hits if t != "SYNC_URL"]
        if other:
            return False
        return text.count('"SYNC_URL"') == text.count("SYNC_URL")
    if rel in ("README.md", "PRIVACY.md", "RELEASING.md", "SECURITY.md",
               "SUPPORT.md", "BUILDING.md"):
        # docs may name removed endpoints/fields when describing the removal
        return all(_context_ok(text, t, ("legacy", "removed", "stripped",
                                         "never", "no longer", "migrat",
                                         "old", "gone", "deleted"))
                   for t in hits)
    return False


def scan_tracking(files):
    print("[tracking endpoints]")
    bad = [f for f in files if not track_ok(f, read(f))]
    chk(not bad, "no IP/geo/update/analytics endpoints or fingerprint code "
        "(bad: %s)" % (bad or "none"))


# --------------------------------------------------------------------------
# 3. Roblox safety boundary: no process-injection / memory APIs anywhere.
# --------------------------------------------------------------------------
INJECT = ["ReadProcessMemory", "WriteProcessMemory", "CreateRemoteThread",
          "VirtualAllocEx", "task_for_pid", "mach_vm_read", "mach_vm_write",
          "ptrace", "SetWindowsHookEx", "DebugActiveProcess",
          "NtOpenProcess", "dlopen_preflight", "DYLD_INSERT_LIBRARIES"]


def scan_injection(files):
    print("[injection/memory APIs]")
    bad = []
    for f in files:
        if (f == "public_release_tests.py" or f in SCANNER_FILES
                or f.startswith(BRAND_DIR_ALLOW)):
            continue
        t = read(f)
        hits = [a for a in INJECT if a in t]
        if not hits:
            continue
        if f in ("README.md", "SECURITY.md", "PRIVACY.md"):
            # docs may NAME these APIs when stating that none are used or in
            # the verification command a reviewer runs to prove it
            if all(_context_ok(t, a, ("no ", "not ", "never", "absent",
                                      "grep"))
                   for a in hits):
                continue
        bad.append(f)
    chk(not bad, "no process-injection or memory-access APIs in the tree "
        "(bad: %s)" % (bad or "none"))


# --------------------------------------------------------------------------
# 4. Access gate: fully removed; welcome onboarding present instead.
# --------------------------------------------------------------------------
GATE = ["ACCESS_CODES", "verify_access", "access_state", "gateCode",
        "Enter your access code", "invite-only", "codes.json"]


def scan_gate(files):
    print("[access gate removal]")
    bad = []
    for f in files:
        if (f in ("public_release_tests.py", "PUBLIC_RELEASE_STATUS.md",
                  "CHANGELOG.md", ".gitignore",
                  # the acceptance probe QUOTES the private filename in its
                  # own must-not-be-bundled assertion (same class as this
                  # file quoting the tokens it bans)
                  "packaging/packaged_acceptance.command")
                or f.startswith(BRAND_DIR_ALLOW)):
            continue
        t = read(f)
        if any(g in t for g in GATE):
            bad.append(f)
    chk(not bad, "no access-code code/UI/tokens remain (bad: %s)"
        % (bad or "none"))
    for rel in ("prospecting_app.py", "windows/prospecting_app.py"):
        t = read(rel)
        chk("def welcome_state" in t and "def welcome_done" in t,
            "%s: welcome onboarding API present" % rel)
        chk('id="welGo"' in t and "WELCOME_SEEN" in t,
            "%s: welcome screen markup + preference present" % rel)
        legacy = region(t, "_PRIVATE_LEGACY_KEYS = (", ")")
        for k in ("ACCESS_OK", "ACCESS_HASH", "ACCESS_MACHINE",
                  "MACHINE_SALT", "SYNC_URL"):
            chk(k in legacy, "%s: legacy key %s is scrubbed" % (rel, k))
        # privacy contract: packaged builds keep the Coach key in the user
        # DATA dir (one deletable folder), never inside the install dir
        chk("SECRETS_FILE = os.path.join(DATA_DIR if FROZEN else HERE," in t,
            "%s: secrets file lives in the data dir when frozen" % rel)


# --------------------------------------------------------------------------
# 5. Secrets: nothing that smells like a credential in the tracked tree.
# --------------------------------------------------------------------------
# Regexes: a webhook path only counts with a real (numeric) id, so the UI
# placeholder "https://discord.com/api/webhooks/…" never trips the scan.
SECRETS = [r"discord(app)?\.com/api/webhooks/\d", r"hooks\.slack\.com/services/T",
           r"sk-proj-[A-Za-z0-9]", r"sk-ant-[A-Za-z0-9]", r"AKIA[0-9A-Z]{16}",
           r"BEGIN RSA PRIVATE", r"BEGIN OPENSSH PRIVATE",
           r"ghp_[A-Za-z0-9]{20}", r"github_pat_[A-Za-z0-9]"]


def scan_secrets(files):
    print("[secrets]")
    bad = []
    for f in files:
        if f == "public_release_tests.py" or f in SCANNER_FILES:
            continue
        t = read(f)
        if any(re.search(s, t) for s in SECRETS):
            bad.append(f)
    chk(not bad, "no credential-shaped strings in tracked files (bad: %s)"
        % (bad or "none"))


# --------------------------------------------------------------------------
# 6. Subprocess / dynamic-execution hygiene in Python sources.
# --------------------------------------------------------------------------
def scan_subprocess(files):
    print("[subprocess/exec hygiene]")
    bad_shell, bad_exec = [], []
    for f in files:
        if not f.endswith(".py") or f == "public_release_tests.py":
            continue
        t = read(f)
        if "shell=True" in t or re.search(r"[^_a-zA-Z]os\.system\(", t):
            bad_shell.append(f)
        if re.search(r"pickle\.loads?\(", t) or "marshal.loads" in t:
            bad_exec.append(f)
        if f != "finds_sim.py" and re.search(r"[^_a-zA-Z.]exec\(", t) \
                and "compile(" in t:
            bad_exec.append(f)
    chk(not bad_shell, "no shell=True / os.system (bad: %s)"
        % (bad_shell or "none"))
    chk(not bad_exec, "no unpickling / exec-of-content outside the "
        "finds_sim dev harness (bad: %s)" % (bad_exec or "none"))


# --------------------------------------------------------------------------
# 7. Shipped default config is clean and private-by-default.
# --------------------------------------------------------------------------
def scan_default_config():
    print("[shipped default config]")
    cfg = json.loads(read("windows/prospecting_config.json"))
    chk(cfg.get("WEBHOOK_ENABLED") is False, "webhook disabled by default")
    chk(not cfg.get("WEBHOOK_URL"), "no webhook URL shipped")
    chk(not cfg.get("WEBHOOK_SECRET"), "no webhook secret shipped")
    for k in ("SYNC_URL", "ACCESS_OK", "ACCESS_HASH", "ACCESS_MACHINE",
              "MACHINE_SALT", "COACH_API_KEY", "WELCOME_SEEN"):
        chk(k not in cfg, "default config has no %s" % k)


# --------------------------------------------------------------------------
# 8. Version identity: one source of truth.
# --------------------------------------------------------------------------
def scan_version():
    print("[version identity]")
    m = re.search(r'VERSION\s*=\s*"([^"]+)"', read("prospecting_app.py"))
    w = re.search(r'VERSION\s*=\s*"([^"]+)"',
                  read("windows/prospecting_app.py"))
    chk(bool(m and w and m.group(1) == w.group(1)),
        "app copies agree on VERSION")
    ver = m.group(1) if m else "?"
    iss = read("windows/installer.iss")
    if iss:
        chk('#define MyAppVersion "%s"' % ver in iss,
            "installer.iss carries VERSION %s" % ver)
        chk('#define MyAppName "Prospector Lite"' in iss,
            "installer.iss product name is Prospector Lite")
    return ver


# --------------------------------------------------------------------------
# Runtime checks (each in an isolated, network-denied child process).
# --------------------------------------------------------------------------
DENY_PRELUDE = r"""
import socket as _s
def _deny(*a, **k):
    raise AssertionError("NETWORK ATTEMPTED")
class _DenySock(_s.socket):
    def __init__(self, *a, **k):
        raise AssertionError("NETWORK ATTEMPTED")
_s.socket = _DenySock
_s.create_connection = _deny
_s.getaddrinfo = _deny
"""


def run_child(name, env=None):
    e = dict(os.environ)
    e.update(env or {})
    r = subprocess.run([sys.executable, os.path.abspath(__file__),
                        "--child", name], cwd=ROOT, env=e,
                       capture_output=True, text=True, timeout=900)
    ok = r.returncode == 0 and "CHILD-OK" in r.stdout
    if not ok:
        print("    child stdout: %s" % r.stdout[-1500:])
        print("    child stderr: %s" % r.stderr[-1500:])
    return ok


def child_app_offline():
    exec(DENY_PRELUDE, globals())
    home = tempfile.mkdtemp(prefix="pl_test_home_")
    os.environ["PP_DATA_DIR"] = home
    sys.path.insert(0, ROOT)
    import prospecting_app as app
    assert app.DATA_DIR == home, app.DATA_DIR
    api = app.Api()
    st = api.welcome_state()
    assert st["show"] is True, st
    info = api.app_info()
    assert info["name"] == "Prospector Lite" and info["version"], info
    api.welcome_done(False)
    st2 = api.welcome_state()
    assert st2["show"] is False, st2
    # webhook: refused without URL, http:// rejected, https:// accepted
    r = api.test_webhook()
    assert not r.get("ok"), r
    r = api.webhook_set("http://insecure.example")
    assert not r.get("ok"), r
    r = api.webhook_set("https://discord.example/api")
    assert r.get("ok"), r
    assert api.webhook_get()["url"] == "https://discord.example/api"
    r = api.webhook_set("")
    assert r.get("ok"), r
    # doc opener is whitelisted
    assert api.open_doc("../etc/passwd") is False
    assert api.open_external("") is False        # no PROJECT_URL -> no-op
    # nothing was written outside the temp home
    cfg = json.load(open(os.path.join(home, "prospecting_config.json")))
    assert cfg.get("WELCOME_SEEN") is True
    print("CHILD-OK")


def child_engine_offline():
    exec(DENY_PRELUDE, globals())
    sys.path.insert(0, ROOT)
    import engine_sim
    transcript, world = engine_sim.run_legacy(
        "classic-standard", alias="pl_netdeny_classic")
    assert world.inputs.all_released(), "inputs not released"
    assert "NETWORK ATTEMPTED" not in transcript
    print("CHILD-OK")


def child_engine_webhook_default():
    exec(DENY_PRELUDE, globals())
    home = tempfile.mkdtemp(prefix="pl_test_ehome_")
    os.environ["PPENGINE_HOME"] = home
    sys.path.insert(0, ROOT)
    import prospector_engine.engine as eng
    assert eng.WEBHOOK_ENABLED is False
    assert eng.WEBHOOK_URL == ""
    # disabled + no URL -> the notify path must be a pure no-op
    eng.post_webhook("start", "test message")
    print("CHILD-OK")


def child_scrub_and_migration():
    exec(DENY_PRELUDE, globals())
    home = tempfile.mkdtemp(prefix="pl_test_scrub_")
    os.environ["PP_DATA_DIR"] = home
    cfg = {"ACCESS_OK": True, "ACCESS_HASH": "deadbeef",
           "ACCESS_MACHINE": "m", "MACHINE_SALT": "s",
           "SYNC_URL": "https://x.invalid/hook", "DIG_SPEED": 123,
           "WELCOME_SEEN": True}
    with open(os.path.join(home, "prospecting_config.json"), "w") as f:
        json.dump(cfg, f)
    sys.path.insert(0, ROOT)
    import prospecting_app as app
    cur = app.load_saved()
    for k in app._PRIVATE_LEGACY_KEYS:
        assert k not in cur, k
    assert cur["DIG_SPEED"] == 123
    app._scrub_config_file()
    disk = json.load(open(os.path.join(home, "prospecting_config.json")))
    for k in app._PRIVATE_LEGACY_KEYS:
        assert k not in disk, k
    assert disk["DIG_SPEED"] == 123 and disk["WELCOME_SEEN"] is True
    # ---- migration: old dir imported copy-only, private keys stripped ----
    old = tempfile.mkdtemp(prefix="pl_test_old_")
    new = tempfile.mkdtemp(prefix="pl_test_néw_")   # non-ASCII path
    with open(os.path.join(old, "prospecting_config.json"), "w") as f:
        json.dump(dict(cfg, CAP_FULL_PIXEL=[1, 2]), f)
    with open(os.path.join(old, "prospecting_builds.json"), "w") as f:
        json.dump({"My Build": {"DIG_SPEED": 1}}, f)
    before = sorted(os.listdir(old))
    app._legacy_data_dir = lambda: old
    app._migrate_legacy_data(new)
    got = json.load(open(os.path.join(new, "prospecting_config.json")))
    for k in app._PRIVATE_LEGACY_KEYS:
        assert k not in got, k
    assert got["CAP_FULL_PIXEL"] == [1, 2]
    assert os.path.exists(os.path.join(new, "prospecting_builds.json"))
    assert os.path.exists(
        os.path.join(new, ".migrated_from_prospectors_plus"))
    assert sorted(os.listdir(old)) == before, "old dir modified"
    # idempotent: a second run must not rewrite anything
    mt = os.path.getmtime(os.path.join(new, "prospecting_config.json"))
    app._migrate_legacy_data(new)
    assert os.path.getmtime(
        os.path.join(new, "prospecting_config.json")) == mt
    print("CHILD-OK")


CHILDREN = {
    "app_offline": child_app_offline,
    "engine_offline": child_engine_offline,
    "engine_webhook_default": child_engine_webhook_default,
    "scrub_and_migration": child_scrub_and_migration,
}


# --------------------------------------------------------------------------
# 9. Packaged artifacts (only when a release candidate exists).
# --------------------------------------------------------------------------
def scan_artifacts():
    print("[release artifacts]")
    rc = os.path.join(ROOT, "release", "public-candidate")
    if not os.path.isdir(rc):
        print("  [SKIP] release/public-candidate/ not built yet")
        return
    names = os.listdir(rc)
    chk(any(n == "SHA256SUMS.txt" for n in names), "SHA256SUMS.txt present")
    chk(any(n.endswith((".dmg", ".zip")) for n in names),
        "at least one package present")
    for n in names:
        low = n.lower()
        chk("prospectors" not in low and "plus" not in low.replace(
            "plus-", "zzz"), "artifact name is rebranded: %s" % n)


def scan_tls_and_wording(files):
    """rc.2 gate additions: (a) certificate verification can never be
    bypassed -- the unverified-context retry must stay dead; (b) the app
    and top-level docs must not claim 'open source' while no licence
    exists (LICENSE_CHOICE_REQUIRED.md). Doc lines that talk ABOUT the
    licence status are allowed."""
    print("[tls + licence wording]")
    bad = []
    for rel in files:
        if rel in ("public_release_tests.py", "onboarding_trust_tests.py"):
            continue        # the scanners themselves quote the token
        if not rel.endswith((".py", ".spec", ".command", ".bat", ".yml")):
            continue        # docs may NAME the token in verify instructions
        text = read(rel)
        for i, line in enumerate(text.splitlines(), 1):
            if ("_create_unverified_context" in line
                    or "ssl.CERT_NONE" in line):
                bad.append("%s:%d" % (rel, i))
    chk(not bad, "no TLS-verification bypass in any tracked code file %s"
        % bad)
    for rel in ("prospecting_app.py", "windows/prospecting_app.py"):
        hits = [l for l in read(rel).splitlines()
                if re.search(r"open[- ]source", l, re.I)]
        chk(not hits, "%s carries no open-source claim" % rel)
    allow = re.compile(r"licen[cs]e|not |until |no longer|packages|"
                       r"third.party|open.source project[s]? do", re.I)
    for rel in ("README.md", "SUPPORT.md", "windows/README.txt"):
        hits = [l.strip() for l in read(rel).splitlines()
                if re.search(r"open[- ]source", l, re.I)
                and not allow.search(l)]
        chk(not hits, "%s open-source wording only in licence context %s"
            % (rel, hits[:2]))


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--child":
        CHILDREN[sys.argv[2]]()
        return
    files = tracked_files()
    scan_branding(files)
    scan_tracking(files)
    scan_injection(files)
    scan_gate(files)
    scan_secrets(files)
    scan_subprocess(files)
    scan_default_config()
    scan_version()
    scan_tls_and_wording(files)
    print("[runtime, network denied]")
    for name in ("app_offline", "engine_offline",
                 "engine_webhook_default", "scrub_and_migration"):
        chk(run_child(name), "child '%s' passed offline" % name)
    scan_artifacts()
    print()
    if FAILS:
        print("PUBLIC RELEASE TESTS: %d FAILURES" % len(FAILS))
        sys.exit(1)
    print("PUBLIC RELEASE TESTS: ALL PASS")


if __name__ == "__main__":
    main()
