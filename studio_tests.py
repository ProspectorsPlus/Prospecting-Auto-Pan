#!/usr/bin/env python3
"""Prospector Studio test suite (dev-only; not shipped, not in the zip).

Covers: the schema validator + sanitizer + templates (app side), the
schema<->interpreter drift guard, and the engine ScriptRunner walked against
stubbed input + a scripted detector (order, repeats, conditionals, timeouts,
whitelist, watchdogs, abort). Run from the repo root:  python3 studio_tests.py
Must end with:  STUDIO TESTS: ALL PASS
"""
import importlib.util
import json
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  [PASS] %s" % name)
    else:
        FAILS.append(name)
        print("  [FAIL] %s %s" % (name, detail))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


app = load(os.path.join(ROOT, "prospecting_app.py"), "papp_t")
po = load(os.path.join(ROOT, "prospector_engine", "engine.py"), "pold_t")

# =============================================================================
print("[1] validator + sanitizer + templates")
# =============================================================================
tpls = app._studio_templates()
names = [t["name"] for t in tpls]
check("three templates ship", names == ["Standard loop", "Treasure (Rubble Creek)", "Blank"], names)
for t in tpls:
    r = app._studio_validate(t)
    check("template '%s' has no schema errors" % t["name"], r["ok"], r["errors"])
std, trs = tpls[0], tpls[1]
check("standard template is runnable",
      not app._studio_validate(std)["problems"])
check("treasure template is runnable",
      not app._studio_validate(trs)["problems"])
trs_types = [b["type"] for b in trs["blocks"]]
check("treasure template is dig/strafe/dig/strafe",
      trs_types == ["comment", "dig", "wait", "wait_cue", "dig", "wait", "wait_cue"],
      trs_types)

# a maximal script exercising every type
c = [0]
B = app._studio_tpl_block
maximal = {"format": "ppscript", "version": 1, "name": "Max", "description": "",
           "author": "", "created": 1, "updated": 1, "settings": {},
           "blocks": [
               B(c, "comment"), B(c, "dig"), B(c, "shake"), B(c, "hold_key"),
               B(c, "tap_key"), B(c, "click"), B(c, "wait"), B(c, "relic"),
               B(c, "notify"), B(c, "wait_cue"), B(c, "wait_cap"),
               B(c, "if_cue", children=[B(c, "wait")]),
               B(c, "if_cap", children=[B(c, "wait")]),
               B(c, "if_not", children=[B(c, "wait")]),
               B(c, "repeat", children=[B(c, "wait")]),
               B(c, "group", children=[B(c, "wait")]),
               B(c, "stop")]}
r = app._studio_validate(maximal)
check("maximal script (all 17 types) validates", r["ok"], r["errors"])


def broke(mut, script=None):
    s = json.loads(json.dumps(script or std))
    mut(s)
    return app._studio_validate(s)


check("unknown type rejected",
      not broke(lambda s: s["blocks"][1].update(type="evil"))["ok"])
check("missing param rejected",
      not broke(lambda s: s["blocks"][1]["params"].pop("hold_ms"))["ok"])
check("extra param rejected",
      not broke(lambda s: s["blocks"][1]["params"].update(zz=1))["ok"])
check("out-of-range rejected",
      not broke(lambda s: s["blocks"][1]["params"].update(hold_ms=10 ** 6))["ok"])
check("bool-as-int rejected",
      not broke(lambda s: s["blocks"][1]["params"].update(hold_ms=True))["ok"])
check("float rejected for int param",
      not broke(lambda s: s["blocks"][1]["params"].update(hold_ms=7.5))["ok"])
check("bad choice rejected",
      not broke(lambda s: s["blocks"][3]["params"].update(cue="teleport"))["ok"])
check("non-whitelisted key rejected by schema",
      not broke(lambda s: s["blocks"][3]["params"].update(hold="Q"))["ok"])
check("duplicate id rejected",
      not broke(lambda s: s["blocks"][1].update(id=s["blocks"][0]["id"]))["ok"])
check("children on a leaf rejected",
      not broke(lambda s: s["blocks"][1].update(children=[{"id": "x", "type": "wait",
                                                           "params": {"ms": 10}}]))["ok"])
check("unknown top field rejected",
      not broke(lambda s: s.update(payload="x"))["ok"])
check("bad name rejected", not broke(lambda s: s.update(name=" x "))["ok"])

deep = json.loads(json.dumps(std))
node = {"id": "d0", "type": "group", "params": {"label": "g"}, "children": []}
deep["blocks"] = [node]
cur = node
for i in range(1, 18):
    nxt = {"id": "d%d" % i, "type": "group", "params": {"label": "g"}, "children": []}
    cur["children"].append(nxt)
    cur = nxt
check("nesting depth 17 rejected", not app._studio_validate(deep)["ok"])

big = json.loads(json.dumps(std))
big["blocks"] = [{"id": "n%d" % i, "type": "wait", "params": {"ms": 10}}
                 for i in range(501)]
check("501 blocks rejected", not app._studio_validate(big)["ok"])

empty = {"format": "ppscript", "version": 1, "name": "E", "description": "",
         "author": "", "created": 1, "updated": 1, "settings": {}, "blocks": []}
r = app._studio_validate(empty)
check("empty script: saves (no errors) but flagged", r["ok"] and r["problems"])
noact = json.loads(json.dumps(empty))
noact["blocks"] = [{"id": "c1", "type": "comment", "params": {"text": "hi"}}]
r = app._studio_validate(noact)
check("no-input script flagged as a problem", r["ok"] and r["problems"])
unreach = json.loads(json.dumps(empty))
unreach["blocks"] = [
    {"id": "s1", "type": "dig", "params": {"hold_ms": 75}},
    {"id": "s2", "type": "stop", "params": {"message": "done"}},
    {"id": "s3", "type": "dig", "params": {"hold_ms": 75}}]
r = app._studio_validate(unreach)
check("block after Safe stop flagged unreachable",
      any("never run" in p for p in r["problems"]), r["problems"])

s, e = app._studio_sanitize({"name": "T", "blocks": [
    {"type": "dig", "params": {"hold_ms": "9999", "junk": 1}, "extra": True},
    {"type": "tap_key", "params": {"key": "Escape", "hold_ms": 40}}]})
check("sanitize clamps + drops junk + fixes illegal key", e is None
      and s["blocks"][0]["params"]["hold_ms"] == 600
      and "junk" not in s["blocks"][0]["params"]
      and s["blocks"][1]["params"]["key"] == "1", (s, e))
s, e = app._studio_sanitize({"name": "T", "blocks": [{"type": "rm -rf", "params": {}}]})
check("sanitize rejects unknown type", s is None and "Unknown block type" in e, e)
s, e = app._studio_sanitize("not a dict")
check("sanitize rejects non-dict", s is None)
s, e = app._studio_sanitize({"name": "T", "blocks": [
    {"id": "same", "type": "wait", "params": {"ms": 50}},
    {"id": "same", "type": "wait", "params": {"ms": 50}}]})
check("sanitize regenerates duplicate ids", e is None
      and s["blocks"][0]["id"] != s["blocks"][1]["id"])

# =============================================================================
print("[2] schema <-> interpreter drift guard")
# =============================================================================
ui = load(os.path.join(ROOT, "prospecting_ui.py"), "pui_t")
check("interpreter handles exactly the schema's types",
      set(po._SCRIPT_HANDLERS) == set(ui.STUDIO_BLOCKS),
      set(po._SCRIPT_HANDLERS) ^ set(ui.STUDIO_BLOCKS))
check("runtime whitelist covers the schema whitelist",
      set(ui.STUDIO_KEY_WHITELIST) == set(po._SCRIPT_KEYS),
      set(ui.STUDIO_KEY_WHITELIST) ^ set(po._SCRIPT_KEYS))
check("every whitelist token has a real keycode",
      all(po._SCRIPT_KEYS[k] is not None for k in po._SCRIPT_KEYS))
check("containers agree",
      ui.STUDIO_CONTAINERS == {"if_cue", "if_cap", "if_not", "repeat", "group"})
check("limits agree", ui.STUDIO_MAX_BLOCKS == po._SCRIPT_MAX_BLOCKS
      and ui.STUDIO_MAX_DEPTH == po._SCRIPT_MAX_DEPTH)
check("wait clamp", po._script_clamp_wait(0) == 100
      and po._script_clamp_wait(10 ** 9) == 120000
      and po._script_clamp_wait("junk") == 100)

# =============================================================================
print("[3] interpreter walk (stubbed input + scripted detector)")
# =============================================================================
ACTIONS = []
_real_script_sleep = po._script_sleep


class FakeDet:
    """Scriptable detector: attributes are plain bools the tests flip."""

    def __init__(self):
        self.pan = False
        self.deposit = True
        self.shake = False
        self.full = False
        self.empty = True
        self.cap_moved = False
        self.shake_taps_to_empty = 0   # >0: pan empties after N shake taps

    def on_pan(self):
        return self.pan

    def on_deposit(self):
        return self.deposit

    def on_shake(self):
        return self.shake

    def capacity_full(self):
        return self.full

    def pan_empty(self):
        if self.shake_taps_to_empty > 0:
            return False
        return self.empty

    def cap_start_rgb(self):
        return (0, 0, 0)

    def cap_changed(self, base):
        return self.cap_moved


def fake_mouse_tap(ms):
    ACTIONS.append(("tap", ms))
    det_live[0] and det_live[0].shake_taps_to_empty > 0 and _shake_hit()


def _shake_hit():
    det_live[0].shake_taps_to_empty -= 1


det_live = [None]


def install_stubs():
    po.sleep_ms = lambda ms: None
    po._script_sleep = lambda ms: (ACTIONS.append(("sleep", ms)) or
                                   bool(po.State.running))
    po.key_down = lambda c: ACTIONS.append(("kd", c))
    po.key_up = lambda c: ACTIONS.append(("ku", c))
    po.mouse_down = lambda: ACTIONS.append(("md",))
    po.mouse_up = lambda: ACTIONS.append(("mu",))
    po.mouse_tap = fake_mouse_tap
    po.move_cursor = lambda x, y: ACTIONS.append(("move", x, y))
    po.release_all = lambda: None
    po.post_webhook = lambda ev, msg, stats=None, shot=False: \
        ACTIONS.append(("hook", ev, msg))
    po.emit_event = lambda *a, **k: ACTIONS.append(("event", a[0] if a else ""))
    po.safe_stop = fake_safe_stop
    po.find_roblox_rect = lambda: (100, 100, 800, 600)
    po.RelicScheduler._fire = lambda self, r: ACTIONS.append(
        ("relic", r["slot"], r["clicks"]))
    po.wait_until = fake_wait_until
    po.log = lambda m: None


def fake_safe_stop(reason, hard=False):
    ACTIONS.append(("safe_stop", reason, hard))
    po.State.running = False


def fake_wait_until(cond, max_ms, confirm=1, min_ms=0):
    for _ in range(confirm + 4):
        if cond():
            return True
    return False


def fresh_state():
    po.State.running = True
    po.State.alive = True
    po.State.want_reset = False
    po.State.stats = po.SessionStats()
    po.State.last_cycle_end = 0.0
    po.State.script_runner = None
    del ACTIONS[:]


def runner_for(blocks, ticks=200):
    s = {"format": "ppscript", "version": 1, "name": "T", "blocks": blocks}
    r = po.ScriptRunner(json.dumps(s), "T")
    det = FakeDet()
    det_live[0] = det
    for _ in range(ticks):
        if not po.State.running:
            break
        r.tick(det)
    return r, det


install_stubs()

# ---- order + pans -----------------------------------------------------------
fresh_state()
blocks = [
    {"id": "a", "type": "dig", "params": {"hold_ms": 75}},
    {"id": "b", "type": "wait", "params": {"ms": 500}},
    {"id": "c", "type": "tap_key", "params": {"key": "3", "hold_ms": 40}},
]
r, det = runner_for(blocks, ticks=3)
kinds = [a[0] for a in ACTIONS]
check("sequence order md/mu then sleep then kd/ku",
      kinds == ["md", "sleep", "mu", "sleep", "kd", "sleep", "ku"],
      ACTIONS)
fresh_state()
# a tick that exhausts the top level wraps the pass AND runs the next block,
# so 3 leaf blocks wrap on tick 4 (which also re-runs block one)
r, det = runner_for(blocks, ticks=4)
check("one pan per top-level pass", po.State.stats.cycles == 1,
      po.State.stats.cycles)
check("dig clicks counted", po.State.stats.dig_clicks == 2,
      po.State.stats.dig_clicks)

# ---- repeat -----------------------------------------------------------------
fresh_state()
blocks = [{"id": "r", "type": "repeat", "params": {"times": 3},
           "children": [{"id": "w", "type": "tap_key",
                         "params": {"key": "1", "hold_ms": 40}}]}]
r, det = runner_for(blocks, ticks=5)
check("repeat 3 runs children 3 times",
      len([a for a in ACTIONS if a[0] == "kd"]) == 3, ACTIONS)

# ---- conditionals -----------------------------------------------------------
fresh_state()
blocks = [{"id": "i1", "type": "if_cap", "params": {"state": "full"},
           "children": [{"id": "t1", "type": "tap_key",
                         "params": {"key": "1", "hold_ms": 40}}]},
          {"id": "i2", "type": "if_not", "params": {"check": "full"},
           "children": [{"id": "t2", "type": "tap_key",
                         "params": {"key": "2", "hold_ms": 40}}]}]
r, det = runner_for(blocks, ticks=4)          # det.full = False
k2 = po._SCRIPT_KEYS["2"]
kds = [a for a in ACTIONS if a[0] == "kd"]
check("if_cap false skips, if_not runs", len(kds) == 1 and kds[0][1] == k2, ACTIONS)
fresh_state()
det_live[0] = None
s = {"format": "ppscript", "version": 1, "name": "T", "blocks": blocks}
r = po.ScriptRunner(json.dumps(s), "T")
det = FakeDet()
det.full = True
for _ in range(4):
    r.tick(det)
k1 = po._SCRIPT_KEYS["1"]
kds = [a for a in ACTIONS if a[0] == "kd"]
check("if_cap true runs, if_not skips",
      len(kds) >= 1 and all(a[1] == k1 for a in kds), ACTIONS)

# regression: the sibling AFTER an entered container must still run
fresh_state()
blocks = [{"id": "i1", "type": "if_not", "params": {"check": "full"},
           "children": [{"id": "t1", "type": "tap_key",
                         "params": {"key": "1", "hold_ms": 40}}]},
          {"id": "t2", "type": "tap_key", "params": {"key": "2", "hold_ms": 40}}]
r, det = runner_for(blocks, ticks=3)          # det.full=False -> if_not enters
kds = [a[1] for a in ACTIONS if a[0] == "kd"]
check("sibling after an entered container runs",
      kds == [po._SCRIPT_KEYS["1"], po._SCRIPT_KEYS["2"]], kds)

# ---- wait_cap timeout -> safe stop -------------------------------------------
fresh_state()
blocks = [{"id": "w", "type": "wait_cap",
           "params": {"state": "full", "timeout_ms": 500, "on_timeout": "stop"}}]
r, det = runner_for(blocks, ticks=2)
stops = [a for a in ACTIONS if a[0] == "safe_stop"]
check("wait_cap timeout trips safe stop", len(stops) == 1
      and "never read full" in stops[0][1], stops)

# ---- whitelist at runtime -----------------------------------------------------
fresh_state()
blocks = [{"id": "x", "type": "tap_key", "params": {"key": "Escape", "hold_ms": 40}}]
r, det = runner_for(blocks, ticks=2)
stops = [a for a in ACTIONS if a[0] == "safe_stop"]
keys = [a for a in ACTIONS if a[0] == "kd"]
check("tampered key: safe stop, zero key events",
      len(stops) == 1 and not keys and "not allowed" in stops[0][1], ACTIONS)
fresh_state()
blocks = [{"id": "x", "type": "hold_key", "params": {"key": "1", "ms": 100}}]
r, det = runner_for(blocks, ticks=2)
stops = [a for a in ACTIONS if a[0] == "safe_stop"]
check("hold_key rejects non-movement key",
      len(stops) == 1 and not [a for a in ACTIONS if a[0] == "kd"], ACTIONS)

# ---- stop block ---------------------------------------------------------------
fresh_state()
blocks = [{"id": "s", "type": "stop", "params": {"message": "bag is full"}}]
r, det = runner_for(blocks, ticks=2)
stops = [a for a in ACTIONS if a[0] == "safe_stop"]
check("stop block routes through safe_stop with the message",
      len(stops) == 1 and "bag is full" in stops[0][1], stops)

# ---- do-nothing script guard ---------------------------------------------------
fresh_state()
blocks = [{"id": "c", "type": "comment", "params": {"text": "zzz"}}]
r, det = runner_for(blocks, ticks=200)
stops = [a for a in ACTIONS if a[0] == "safe_stop"]
check("all-comment script stops itself",
      len(stops) == 1 and "without doing anything" in stops[0][1], stops)
check("do-nothing passes count no pans", po.State.stats.cycles == 0)

# ---- malformed SCRIPT_JSON ------------------------------------------------------
fresh_state()
r = po.ScriptRunner("{definitely not json", "Bad")
det = FakeDet()
r.tick(det)
stops = [a for a in ACTIONS if a[0] == "safe_stop"]
check("bad JSON: hard safe stop, engine alive",
      len(stops) == 1 and stops[0][2] is True and po.State.alive, stops)
fresh_state()
r = po.ScriptRunner(json.dumps({"format": "ppscript", "version": 1, "name": "x",
                                "blocks": [{"type": "explode", "params": {}}]}), "x")
r.tick(FakeDet())
stops = [a for a in ACTIONS if a[0] == "safe_stop"]
check("unknown type at runtime: hard safe stop", len(stops) == 1 and stops[0][2] is True,
      stops)

# ---- shake until empty -----------------------------------------------------------
fresh_state()
blocks = [{"id": "sh", "type": "shake",
           "params": {"clicks": 0, "click_ms": 18, "gap_ms": 14,
                      "max_ms": 4000, "momentum_w": True}}]
s = {"format": "ppscript", "version": 1, "name": "T", "blocks": blocks}
r = po.ScriptRunner(json.dumps(s), "T")
det = FakeDet()
det.empty = True
det.shake_taps_to_empty = 3
det_live[0] = det
r.tick(det)
taps = [a for a in ACTIONS if a[0] == "tap"]
kd_w = [a for a in ACTIONS if a[0] == "kd" and a[1] == po.KEY_W]
ku_w = [a for a in ACTIONS if a[0] == "ku" and a[1] == po.KEY_W]
check("shake taps until the pan reads empty, W held then released",
      3 <= len(taps) <= 6 and len(kd_w) == 1 and len(ku_w) == 1, ACTIONS)

# ---- treasure template through the real engine walker -----------------------------
fresh_state()
trs2 = app._studio_templates()[1]
r = po.ScriptRunner(json.dumps(trs2), trs2["name"])
det = FakeDet()
det.deposit = True
det_live[0] = det
for _ in range(20):
    if po.State.stats.cycles >= 2:
        break
    r.tick(det)
seq = [a for a in ACTIONS if a[0] in ("md", "kd")]
KD, KA = po.KEY_D, po.KEY_A
pat = [("md",), ("kd", KD), ("md",), ("kd", KA)]
check("treasure order: dig, strafe D, dig, strafe A (twice)",
      seq[:8] == pat + pat, seq[:8])
check("treasure passes count pans", po.State.stats.cycles >= 2,
      po.State.stats.cycles)
waits = [a for a in ACTIONS if a[0] == "sleep" and a[1] == 12000]
check("treasure waits out the slow dig animation", len(waits) >= 4, len(waits))

# ---- pause/reset semantics ---------------------------------------------------------
fresh_state()
blocks = [{"id": "a", "type": "tap_key", "params": {"key": "1", "hold_ms": 40}},
          {"id": "b", "type": "tap_key", "params": {"key": "2", "hold_ms": 40}}]
s = {"format": "ppscript", "version": 1, "name": "T", "blocks": blocks}
r = po.ScriptRunner(json.dumps(s), "T")
det = FakeDet()
det_live[0] = det
r.tick(det)                       # ran block a
po.State.want_reset = True        # safe-pause retry semantics
r.tick(det)                       # must restart from block a, not b
kds = [a[1] for a in ACTIONS if a[0] == "kd"]
check("want_reset restarts the walk from the top",
      kds == [po._SCRIPT_KEYS["1"], po._SCRIPT_KEYS["1"]], kds)

# ---- real _script_sleep aborts on stop ----------------------------------------------
# The real slicer counts its budget down (exact countdown, no clock reads --
# the detach byte-parity contract), so this check runs it against the REAL
# sleeper: the run flag flips mid-budget and the very next slice boundary
# must abort the sleep.
_stub_sleep_ms = po.sleep_ms
po.sleep_ms = lambda ms: time.sleep(ms / 1000.0)
po.State.running = True
po.State.alive = True
t0 = time.perf_counter()
threading.Timer(0.08, lambda: setattr(po.State, "running", False)).start()
res = _real_script_sleep(5000)
dt = time.perf_counter() - t0
po.sleep_ms = _stub_sleep_ms
check("real _script_sleep aborts the instant the run stops",
      res is False and dt < 1.0, dt)

# =============================================================================
print("[4] v1 review: forward compatibility, storage recovery, caching")
# =============================================================================
oldstyle = {"format": "ppscript", "version": 1, "name": "Old", "description": "",
            "author": "", "created": 1, "updated": 1, "settings": {},
            "blocks": [{"id": "a", "type": "dig", "params": {}},
                       {"id": "r", "type": "repeat", "params": {"times": 2}}]}
norm = app._studio_normalize(oldstyle)
check("normalize fills missing params and children",
      norm["blocks"][0]["params"].get("hold_ms") == 75
      and norm["blocks"][1].get("children") == [])
check("normalized old-style script has no schema errors",
      app._studio_validate(norm)["ok"], app._studio_validate(norm)["errors"])
check("original stored data is never mutated by normalize",
      "hold_ms" not in oldstyle["blocks"][0]["params"])
newer = json.loads(json.dumps(norm)); newer["version"] = 99
r = app._studio_validate(newer)
check("file from a newer schema refused with a clear message",
      not r["ok"] and any("newer version" in e for e in r["errors"]), r["errors"])
check("version 0 refused", not app._studio_validate(dict(norm, version=0))["ok"])

fresh_state()
rn = po.ScriptRunner(json.dumps(norm), "Old")
det = FakeDet(); det_live[0] = det
rn.tick(det); rn.tick(det)
check("engine runs an old-style (normalized) script",
      not rn.dead and po.State.stats.dig_clicks >= 1,
      (rn.dead, po.State.stats.dig_clicks))

# list cache: served, then invalidated by a write
app._STUDIO_LIST_CACHE["key"] = None
api_t = app.Api()
api_t.studio_save(app._studio_templates()[2], None)         # "Blank"
r1 = api_t.studio_list()
check("list cache primed", app._STUDIO_LIST_CACHE["scripts"] is not None and r1["ok"])
r2 = api_t.studio_list()
check("second list call served from cache",
      r2["scripts"] is app._STUDIO_LIST_CACHE["scripts"])
api_t.studio_delete("Blank")
r3 = api_t.studio_list()
check("cache invalidated by a write",
      all(s["name"] != "Blank" for s in r3["scripts"]))

# crash-safe storage: corrupt main file recovers from the rolling .bak
import tempfile
import shutil as _sh
_tdir = tempfile.mkdtemp()
_orig_sf = app.SCRIPTS_FILE
app.SCRIPTS_FILE = os.path.join(_tdir, "prospecting_scripts.json")
app._STUDIO_LIST_CACHE["key"] = None
try:
    d0 = {"active": "", "scripts": {"Keep": app._studio_templates()[0]}, "meta": {}}
    app._studio_write(d0)                       # first write (no .bak yet)
    d0["scripts"]["Keep"]["description"] = "second save"
    app._studio_write(d0)                       # .bak now holds the first state
    with open(app.SCRIPTS_FILE, "w") as f:
        f.write("{definitely corrupted")
    rec = app._studio_load()
    check("corrupt scripts file recovers from .bak",
          "Keep" in rec["scripts"], list(rec["scripts"]))
    with open(app.SCRIPTS_FILE, "w") as f:
        f.write("")                              # empty file (partial write)
    rec2 = app._studio_load()
    check("empty scripts file recovers from .bak", "Keep" in rec2["scripts"])
finally:
    app.SCRIPTS_FILE = _orig_sf
    app._STUDIO_LIST_CACHE["key"] = None
    _sh.rmtree(_tdir, ignore_errors=True)

# =============================================================================
print("[9] CLASSIC | STUDIO top-level mode (Studio launch)")
# =============================================================================
# Everything runs against a scratch home: user config, scripts and the open-
# request file are patched module attributes, restored in the finally.
_mdir = tempfile.mkdtemp()
_saved_attrs = {k: getattr(app, k) for k in
                ("SCRIPTS_FILE", "CONFIG_FILE", "DATA_DIR", "STATUS_FILE",
                 "PUSH_FILE", "STUDIO_LAUNCH", "STUDIO_SCRIPT")}
app.SCRIPTS_FILE = os.path.join(_mdir, "prospecting_scripts.json")
app.CONFIG_FILE = os.path.join(_mdir, "prospecting_config.json")
app.STATUS_FILE = os.path.join(_mdir, "studio_macro_status.json")
app.PUSH_FILE = os.path.join(_mdir, "studio_push.json")
app.DATA_DIR = _mdir
app.STUDIO_LAUNCH = True
app.STUDIO_SCRIPT = "Pushed Build"
app._STUDIO_LIST_CACHE["key"] = None
api_m = None
try:
    api_m = app.Api()
    tpl = json.loads(json.dumps(app._studio_templates()[0]))
    tpl["name"] = "Pushed Build"
    check("mode fixture script saves clean", api_m.studio_save(tpl, None)["ok"])

    # fresh home derives CLASSIC; STUDIO with no build refuses to launch
    r = api_m.studio_mode()
    check("fresh home derives classic", r["ok"] and r["mode"] == "classic", r)
    api_m.studio_set_active("")
    d0 = app._studio_load()
    d0["mode"] = "studio"
    app._studio_write(d0)
    api_m.studio_set_active("")           # studio mode, no active build
    check("launch refuses studio-without-build",
          api_m.launch(None) == "no-studio-build")

    # explicit STUDIO restores the pushed build; CLASSIC remembers it
    r = api_m.studio_mode("studio")
    check("studio switch restores the pushed build",
          r["ok"] and r["active"] == "Pushed Build" and not r["needs_build"], r)
    cfg = app.load_saved()
    check("active build rides into engine config",
          cfg.get("SCRIPT_MODE") is True and cfg.get("SCRIPT_ACTIVE") == "Pushed Build")
    r = api_m.studio_mode("classic")
    check("classic switch clears the active build", r["ok"] and r["active"] == "")
    d = app._studio_load()
    check("classic switch remembers last_active", d["last_active"] == "Pushed Build")
    cfg = app.load_saved()
    check("classic switch clears engine script keys",
          cfg.get("SCRIPT_MODE") is False and cfg.get("SCRIPT_ACTIVE") == "")
    r = api_m.studio_mode("studio")
    check("switching back restores the remembered build",
          r["ok"] and r["active"] == "Pushed Build")

    # classic with a stale active build refuses (invariant, belt and braces)
    d = app._studio_load()
    d["mode"] = "classic"
    app._studio_write(d)
    check("launch refuses classic-with-active-build",
          api_m.launch(None) == "classic-with-active-build")

    # mid-run guard: mode switches are refused while a run is live
    api_m.studio_mode("studio")
    api_m.proc = object()
    r = api_m.studio_mode("classic")
    check("mode switch refused while running",
          not r["ok"] and "Stop the run" in r["error"], r)
    api_m.proc = None

    # studio_run implies STUDIO mode (a grid Run can never wedge the
    # invariant). launch() is stubbed -- no real engine child in tests.
    d = app._studio_load()
    d["mode"] = "classic"
    d["active"] = ""
    app._studio_write(d)
    _orig_launch = api_m.launch
    api_m.launch = lambda data=None: "launched"
    rr = api_m.studio_run("Pushed Build")
    api_m.launch = _orig_launch
    d = app._studio_load()
    check("studio_run implies STUDIO mode and starts",
          rr["ok"] and d["mode"] == "studio" and d["active"] == "Pushed Build",
          (rr, d["mode"], d["active"]))
    api_m.proc = object()
    rr = api_m.studio_run("Pushed Build")
    api_m.proc = None
    check("studio_run mid-run reports a reason", not rr["ok"] and rr["error"], rr)

    # settings ownership: disjoint groups, resets scoped to their owner
    own = api_m.settings_ownership()
    check("ownership groups are disjoint",
          not set(own["classic"]) & set(own["shared"]))
    check("auto-stop and webhook are shared",
          "AUTOSTOP_ENABLED" in own["shared"] and "WEBHOOK_URL" in own["shared"])
    check("cycle tuning is classic",
          "DIG_CLICK_MS" in own["classic"] and "RELICS" in own["classic"])
    api_m.save_config({"DIG_CLICK_MS": 999, "AUTOSTOP_MINUTES": 123})
    api_m.settings_reset("classic")
    cfg = app.load_saved()
    check("reset classic restores classic keys only",
          cfg.get("DIG_CLICK_MS") == app.DEFAULTS["DIG_CLICK_MS"]
          and cfg.get("AUTOSTOP_MINUTES") == 123)
    api_m.settings_reset("shared")
    cfg = app.load_saved()
    check("reset shared restores shared keys",
          cfg.get("AUTOSTOP_MINUTES") == app.DEFAULTS["AUTOSTOP_MINUTES"])
    pk = next(iter(app.PIXEL_DEFAULTS), None)
    if pk:
        cfg2 = dict(app.load_saved())
        cfg2[pk] = [123, 456]
        with open(app.CONFIG_FILE, "w") as f:
            json.dump(cfg2, f)
        api_m.settings_reset("shared", include_calibration=False)
        check("reset shared leaves calibration alone by default",
              list(app.load_saved().get(pk)) == [123, 456])
        api_m.settings_reset("shared", include_calibration=True)
        check("reset shared clears calibration only when asked",
              list(app.load_saved().get(pk)) == list(app.PIXEL_DEFAULTS[pk]))
    api_m.settings_reset("studio")
    d = app._studio_load()
    check("reset studio clears active/mode but keeps scripts",
          d["active"] == "" and d["mode"] == "classic"
          and "Pushed Build" in d["scripts"])

    # open-in-studio request file
    r = api_m.studio_open_in_studio("node-3")
    req = json.load(open(os.path.join(_mdir, "studio_open_request.json")))
    check("open request written with script+node",
          r["ok"] and req["script"] == "Pushed Build"
          and req["node"] == "node-3", req)
    app.STUDIO_LAUNCH = False
    check("open request refused outside a Studio launch",
          not api_m.studio_open_in_studio("")["ok"])
finally:
    if api_m is not None:
        api_m._studio_status_stop.set()
        _t9 = getattr(api_m, "_studio_status_thread", None)
        if _t9 is not None:
            _t9.join(timeout=3.0)
    for k, v in _saved_attrs.items():
        setattr(app, k, v)
    app._STUDIO_LIST_CACHE["key"] = None
    _sh.rmtree(_mdir, ignore_errors=True)

# =============================================================================
print("[10] mode truth: AutoPan Tracking cannot shadow a Studio run")
# =============================================================================
# TRACKER_MODE outranks SCRIPT_MODE inside the engine, so the app must park
# the classic toggle whenever STUDIO owns Start, and hand it back on the way
# out. Also: the live status mirror + the safety-policy ownership move.
_mdir = tempfile.mkdtemp()
_saved_attrs = {k: getattr(app, k) for k in
                ("SCRIPTS_FILE", "CONFIG_FILE", "DATA_DIR", "STATUS_FILE",
                 "PUSH_FILE", "STUDIO_LAUNCH", "STUDIO_SCRIPT")}
app.SCRIPTS_FILE = os.path.join(_mdir, "prospecting_scripts.json")
app.CONFIG_FILE = os.path.join(_mdir, "prospecting_config.json")
app.STATUS_FILE = os.path.join(_mdir, "studio_macro_status.json")
app.PUSH_FILE = os.path.join(_mdir, "studio_push.json")
app.DATA_DIR = _mdir
app.STUDIO_LAUNCH = True
app.STUDIO_SCRIPT = "Pushed Build"
app._STUDIO_LIST_CACHE["key"] = None
api_t = None
try:
    api_t = app.Api()
    tpl = json.loads(json.dumps(app._studio_templates()[0]))
    tpl["name"] = "Pushed Build"
    check("fixture script saves clean", api_t.studio_save(tpl, None)["ok"])

    # ownership: safe-stop policy is SHARED (any run hits safe_stop);
    # the tracker toggle stays CLASSIC (it selects the classic program)
    own = api_t.settings_ownership()
    check("safe-stop policy is shared",
          all(k in own["shared"] for k in
              ("SAFE_STOP_RETRY", "SAFE_STOP_RETRY_SEC",
               "SAFE_STOP_MAX_RETRIES")))
    check("tracker toggle is classic-owned", "TRACKER_MODE" in own["classic"])

    # classic -> studio parks the toggle; the engine config never sees it
    api_t.save_config({"TRACKER_MODE": True})
    r = api_t.studio_mode("studio")
    check("studio switch succeeds with tracker parked",
          r["ok"] and r["mode"] == "studio" and r.get("tracker") is False, r)
    cfg = app.load_saved()
    check("engine config shows tracker OFF in studio mode",
          cfg.get("TRACKER_MODE") is False)
    check("classic preference is parked",
          app._studio_load()["classic_tracker"] is True)

    # studio -> classic hands the choice back and clears the stash
    r = api_t.studio_mode("classic")
    check("classic switch restores the tracker choice",
          r["ok"] and r.get("tracker") is True
          and app.load_saved().get("TRACKER_MODE") is True, r)
    check("stash cleared after restore",
          app._studio_load()["classic_tracker"] is None)

    # launch guard: state written straight into the config (Prospector
    # Studio's publish path) is re-projected before any spawn
    api_t.studio_mode("studio")
    app._config_patch({"TRACKER_MODE": True})
    check("park guard clears an out-of-band toggle",
          api_t._studio_park_tracker() is None
          and app.load_saved().get("TRACKER_MODE") is False
          and app._studio_load()["classic_tracker"] is True)
    check("park guard is a no-op when already clear",
          api_t._studio_park_tracker() is None)

    # Reset Studio returns to CLASSIC and must un-park the choice
    api_t.settings_reset("studio")
    check("reset studio restores the parked tracker choice",
          app.load_saved().get("TRACKER_MODE") is True
          and app._studio_load()["classic_tracker"] is None)

    # Reset Classic clears the toggle AND any stale stash
    api_t.studio_mode("studio")            # parks True again
    api_t.settings_reset("classic")
    check("reset classic clears toggle and stash",
          app.load_saved().get("TRACKER_MODE")
          == app.DEFAULTS["TRACKER_MODE"]
          and app._studio_load()["classic_tracker"] is None)

    # live status mirror: push stamp + snapshot + the 1 Hz writer
    with open(app.PUSH_FILE, "w") as f:
        json.dump({"v": 1, "name": "Pushed Build", "rev": "ab12cd34",
                   "at": 1000}, f)
    api_t.studio_mode("studio")
    api_t._last_stats = {"cycles": 7, "runtime_s": 60, "pans_per_hr": 420,
                         "stop_reason": ""}
    api_t._macro_status = "running"
    snap = api_t._studio_status_snapshot()
    check("snapshot mirrors mode/build/rev/run",
          snap["mode"] == "studio" and snap["active"] == "Pushed Build"
          and snap["rev"] == "ab12cd34" and snap["run"] == "running", snap)
    check("snapshot carries only known headline stats",
          snap["stats"] == {"cycles": 7, "runtime_s": 60,
                            "pans_per_hr": 420}, snap["stats"])
    pi = api_t.studio_push_info()
    check("push info readable by the macro UI",
          pi["ok"] and pi["name"] == "Pushed Build" and pi["rev"] == "ab12cd34")
    deadline = time.time() + 3.0
    body = None
    while time.time() < deadline:
        try:
            body = json.load(open(app.STATUS_FILE))
            break
        except (OSError, ValueError):
            time.sleep(0.1)
    check("status file written atomically with v/seq/ts",
          isinstance(body, dict) and body.get("v") == 1
          and body.get("seq", 0) >= 1 and body.get("ts", 0) > 0, body)
finally:
    if api_t is not None:
        api_t._studio_status_stop.set()
        t = getattr(api_t, "_studio_status_thread", None)
        if t is not None:
            t.join(timeout=3.0)
    for k, v in _saved_attrs.items():
        setattr(app, k, v)
    app._STUDIO_LIST_CACHE["key"] = None
    _sh.rmtree(_mdir, ignore_errors=True)

# =============================================================================
print("[11] PPScript v3 — general input under declared capabilities")
# =============================================================================
# The runner-level contract for docs/PPSCRIPT_V3.md (Studio repo): caps are
# parsed and re-enforced at load, the new handlers drive the platform seams in
# a deterministic order, and every down is released on the abort paths.

install_stubs()
po.type_char = lambda ch: ACTIONS.append(("type", ch))
po.button_down = lambda b, cs=1: ACTIONS.append(("bd", b, cs))
po.button_up = lambda b, cs=1: ACTIONS.append(("bu", b, cs))
po.scroll_lines = lambda n: ACTIONS.append(("scroll", n))
po.set_clipboard = lambda t: ACTIONS.append(("clip", t))


def v3_script(blocks, caps=None, **extra):
    s = {"format": "ppscript", "version": 3, "name": "V3", "blocks": blocks}
    if caps is not None:
        s["caps"] = caps
    s.update(extra)
    return s


def v3_runner(blocks, caps=None, ticks=200, **extra):
    r = po.ScriptRunner(json.dumps(v3_script(blocks, caps, **extra)), "V3")
    det = FakeDet()
    det_live[0] = det
    for _ in range(ticks):
        if not po.State.running:
            break
        r.tick(det)
    return r, det


# ---- caps enforcement at load ----------------------------------------------
fresh_state()
r = po.ScriptRunner(json.dumps(v3_script(
    [{"id": "k", "type": "key_press", "params": {"key": "g"}}])), "V3")
check("undeclared keyboard cap refuses the script",
      "does not declare" in r.dead and "press any key" in r.dead, r.dead)

r = po.ScriptRunner(json.dumps(v3_script(
    [{"id": "k", "type": "key_press", "params": {"key": "g"}}],
    caps=["keyboard"])), "V3")
check("declared keyboard cap loads", r.dead == "", r.dead)
check("runner records its caps", r.caps == ("keyboard",), r.caps)

r = po.ScriptRunner(json.dumps(v3_script(
    [{"id": "w", "type": "wait", "params": {"ms": 200}}],
    caps=["telepathy"])), "V3")
check("unknown cap refuses the script", "unknown capability" in r.dead, r.dead)

r = po.ScriptRunner(json.dumps({
    "format": "ppscript", "version": 2, "name": "V2",
    "blocks": [{"id": "k", "type": "key_press", "params": {"key": "g"}}]}), "V2")
check("v2 does not understand v3 blocks",
      "does not understand" in r.dead, r.dead)

# caps needed inside hooks count too
fresh_state()
r = po.ScriptRunner(json.dumps(v3_script(
    [{"id": "w", "type": "wait", "params": {"ms": 200}}],
    caps=[], hooks={"on_stuck": [
        {"id": "t", "type": "type_text", "params": {"text": "hi"}}]})), "V3")
check("hook-only cap use is still enforced",
      "does not declare" in r.dead and "type text" in r.dead, r.dead)

# ---- combo parsing -----------------------------------------------------------
ok, why = po._script3_parse_combo("cmd+shift+4")
check("combo parses (mods ordered, terminal kept)",
      ok == (["cmd", "shift"], "4"), (ok, why))
bad = [po._script3_parse_combo(t)[0] is None
       for t in ("cmd+shift", "q+w", "cmd+cmd+c", "", "cmd+nosuchkey")]
check("bad combos are all refused", all(bad), bad)
check("primary alias resolves per platform",
      po._script3_key_name("primary") == po.V3_PRIMARY, po.V3_PRIMARY)

# ---- execution order + release safety ---------------------------------------
fresh_state()
G = po.V3_KEYCODES["g"]
CMD = po.V3_KEYCODES["cmd"]
C = po.V3_KEYCODES["c"]
r, det = v3_runner([
    {"id": "p", "type": "key_press", "params": {"key": "g", "hold_ms": 50}},
    {"id": "c", "type": "key_combo", "params": {"combo": "cmd+c"}},
    {"id": "t", "type": "type_text", "params": {"text": "hi", "cps": 10}},
    {"id": "m", "type": "mouse_btn",
     "params": {"button": "right", "action": "double"}},
    {"id": "s", "type": "scroll", "params": {"amount": -3, "steps": 2}},
    {"id": "x", "type": "stop", "params": {"message": "done"}},
], caps=["keyboard", "text", "mouse"], ticks=20)
seq = [a for a in ACTIONS if a[0] in
       ("kd", "ku", "type", "bd", "bu", "scroll", "clip")]
check("v3 executes in authored order with paired downs/ups", seq == [
    ("kd", G), ("ku", G),
    ("kd", CMD), ("kd", C), ("ku", C), ("ku", CMD),
    ("type", "h"), ("type", "i"),
    ("bd", "right", 1), ("bu", "right", 1),
    ("bd", "right", 2), ("bu", "right", 2),
    ("scroll", -3), ("scroll", -3),
], seq)

# key_down persists across nodes and is released by release_all
fresh_state()
held_before = set(po._HELD_KEYS)
r, det = v3_runner([
    {"id": "d", "type": "key_down", "params": {"key": "g"}},
    {"id": "w", "type": "wait", "params": {"ms": 200}},
    {"id": "x", "type": "stop", "params": {"message": "held"}},
], caps=["keyboard"], ticks=10)
kd = [a for a in ACTIONS if a[0] == "kd"]
ku = [a for a in ACTIONS if a[0] == "ku"]
check("key_down holds across nodes (no auto key_up)",
      ("kd", G) in kd and ("ku", G) not in ku, (kd, ku))

# The REAL release_all (install_stubs nulled the one on `po`) must release
# held v3 keys AND buttons — use a fresh module load with recording seams.
_po2 = load(os.path.join(ROOT, "prospector_engine", "engine.py"), "pold_rel")
_po2._HELD_KEYS.clear()
_po2._HELD_BUTTONS.clear()
_rel_calls = []
_po2.key_up = lambda c: _rel_calls.append(("ku", c))
_po2.mouse_up = lambda: _rel_calls.append(("mu",))
_po2.button_up = lambda b, cs=1: _rel_calls.append(("bu", b))
_po2._HELD_KEYS.add(99)
_po2._HELD_BUTTONS.add("right")
_po2.release_all()
check("release_all releases held v3 keys and buttons",
      ("ku", 99) in _rel_calls and ("bu", "right") in _rel_calls
      and ("mu",) in _rel_calls, _rel_calls)

# ---- v3 is additive: a v2 script through the v3 runner is unchanged ---------
fresh_state()
r, det = v3_runner([
    {"id": "a", "type": "dig", "params": {"hold_ms": 75}},
    {"id": "x", "type": "stop", "params": {"message": "done"}},
], caps=[], ticks=5)
check("v1/v2 nodes run under version 3 unchanged",
      any(a[0] == "md" for a in ACTIONS)
      and any(a[0] == "safe_stop" for a in ACTIONS), ACTIONS[:6])

# =============================================================================
print("[12] input recorder — capture service (fake listeners)")
# =============================================================================
from prospector_engine import recorder as R  # noqa: E402


class _FakeListener:
    def start(self):
        pass

    def stop(self):
        pass


class _KC:                       # pynput KeyCode-like
    def __init__(self, char):
        self.char = char


class _KS:                       # pynput special-Key-like
    def __init__(self, name):
        self.name = name
        self.char = None


class _KB:                       # pynput Button-like
    def __init__(self, name):
        self.name = name


_kcodes = {"a": 0, "g": 5, "cmd": 55, "space": 49}
rec = R.Recorder(_kcodes, scale=2.0,
                 kb_listener=_FakeListener, mouse_listener=_FakeListener)
check("recorder starts", rec.start()["ok"] is True)
rec._on_press(_KC("g")); rec._on_release(_KC("g"))
rec._on_press(_KS("cmd")); rec._on_release(_KS("cmd"))
rec._on_press(_KC("!"))
rec._on_press(_KS("f24"))
rec._on_move(10, 20); rec._on_move(11, 21)     # 2nd inside coalesce window
time.sleep(0.02); rec._on_move(12, 22)
rec._on_click(50, 60, _KB("right"), True)
rec._on_click(50, 60, _KB("right"), False)
rec._on_scroll(50, 60, 0, -2)
out = rec.stop()
evs = out["events"]
check("canonical key names + typed chars recorded",
      evs[0]["kind"] == "key_down" and evs[0]["key"] == "g"
      and evs[0]["char"] == "g" and evs[2]["key"] == "cmd", evs[:3])
check("unmapped char keeps the char, unknown special keeps raw",
      evs[4].get("char") == "!" and "key" not in evs[4]
      and "raw" in evs[5], evs[4:6])
_moves = [e for e in evs if e["kind"] == "mouse_move"]
check("mouse moves coalesced and scaled to physical px",
      len(_moves) == 2 and _moves[0]["x"] == 20, _moves)
_clicks = [e for e in evs if e["kind"] in ("mouse_down", "mouse_up")]
check("buttons recorded with physical coords",
      _clicks[0]["button"] == "right" and _clicks[0]["x"] == 100, _clicks)
check("scroll recorded", any(e["kind"] == "scroll" and e["dy"] == -2
                             for e in evs))
check("clean stop reports no truncation", out["truncated"] is False)

rec2 = R.Recorder(_kcodes, kb_listener=_FakeListener,
                  mouse_listener=_FakeListener)
rec2.start()
for _i in range(R.MAX_EVENTS + 5):
    rec2._on_press(_KC("a"))
check("event cap stops the capture with truncated=True",
      rec2.truncated is True and len(rec2.events) == R.MAX_EVENTS
      and rec2.recording is False)

# =============================================================================
print("[13] CLASSIC | STUDIO BUILD | STUDIO SCRIPT (document kinds)")
# =============================================================================
# The three-mode top level: "kind" rides the ppscript file (build unless
# Prospector Studio stamped "script"), the server-owned mode always matches
# the active entry's kind, launch() refuses every mismatch, and the status
# mirror carries kind + live script progress for the Studio window.
import shutil as _sh13
import tempfile as _tf13

_mdir = _tf13.mkdtemp()
_saved_attrs = {k: getattr(app, k) for k in
                ("SCRIPTS_FILE", "CONFIG_FILE", "DATA_DIR", "STATUS_FILE",
                 "PUSH_FILE", "STUDIO_LAUNCH", "STUDIO_SCRIPT")}
app.SCRIPTS_FILE = os.path.join(_mdir, "prospecting_scripts.json")
app.CONFIG_FILE = os.path.join(_mdir, "prospecting_config.json")
app.STATUS_FILE = os.path.join(_mdir, "studio_macro_status.json")
app.PUSH_FILE = os.path.join(_mdir, "studio_push.json")
app.DATA_DIR = _mdir
app.STUDIO_LAUNCH = True
app.STUDIO_SCRIPT = ""
app._STUDIO_LIST_CACHE["key"] = None
api_k = None
try:
    api_k = app.Api()

    # kind validation on the wire format
    v2s = {"format": "ppscript", "version": 2, "name": "A Build",
           "blocks": [{"id": "b1", "type": "wait",
                       "params": {"ms": 500}}]}
    r = app._studio_validate_v2(dict(v2s, kind="script"))
    check("kind=script validates", r["ok"], r["errors"])
    r = app._studio_validate_v2(dict(v2s, kind="build"))
    check("kind=build validates", r["ok"], r["errors"])
    r = app._studio_validate_v2(dict(v2s, kind="banana"))
    check("unknown kind refused", not r["ok"]
          and any("kind" in e for e in r["errors"]), r["errors"])
    check("kind default is build", app._studio_kind(v2s) == "build")
    check("script kind reads back",
          app._studio_kind(dict(v2s, kind="script")) == "script")

    # fixtures: one build, one script
    bld = json.loads(json.dumps(app._studio_templates()[0]))
    bld["name"] = "Kind Build"
    check("build fixture saves", api_k.studio_save(bld, None)["ok"])
    scr = dict(v2s, kind="script", name="Kind Script")
    check("script fixture saves", api_k.studio_save(scr, None)["ok"])
    rows = {s["name"]: s for s in api_k.studio_list()["scripts"]}
    check("list rows carry kinds",
          rows["Kind Build"]["kind"] == "build"
          and rows["Kind Script"]["kind"] == "script", rows)

    # activating an entry flips the top level to its kind
    api_k.studio_set_active("Kind Script")
    r = api_k.studio_mode()
    check("activating a script chooses STUDIO SCRIPT",
          r["mode"] == "script" and r["kind"] == "script", r)
    api_k.studio_set_active("Kind Build")
    r = api_k.studio_mode()
    check("activating a build chooses STUDIO BUILD",
          r["mode"] == "studio" and r["kind"] == "build", r)

    # switching modes restores only entries of the mode's own kind
    r = api_k.studio_mode("script")
    check("script mode never adopts the build",
          r["ok"] and r["mode"] == "script" and r["active"] == ""
          and r["needs_script"], r)
    check("launch refuses script mode without a script",
          api_k.launch(None) == "no-studio-script")
    api_k.studio_set_active("Kind Script")
    r = api_k.studio_mode("classic")
    check("classic remembers the script", r["ok"]
          and app._studio_load()["last_active"] == "Kind Script")
    r = api_k.studio_mode("script")
    check("script mode restores the remembered script",
          r["ok"] and r["active"] == "Kind Script", r)

    # a hand-edited mismatch (mode says build, active is a script) refuses
    d = app._studio_load()
    d["mode"] = "studio"
    app._studio_write(d)
    check("launch refuses a mode/kind mismatch",
          api_k.launch(None) == "mode-kind-mismatch")

    # both Studio modes park the tracker
    api_k.studio_mode("classic")
    api_k.save_config({"TRACKER_MODE": True})
    r = api_k.studio_mode("script")
    check("script mode parks AutoPan Tracking",
          r["ok"] and app.load_saved().get("TRACKER_MODE") is False
          and app._studio_load()["classic_tracker"] is True, r)
    r = api_k.studio_mode("classic")
    check("classic restores the parked choice after script mode",
          r["ok"] and app.load_saved().get("TRACKER_MODE") is True)

    # status mirror: kind + live script step ride the snapshot
    api_k.studio_set_active("Kind Script")
    api_k._on_script_block({"id": "b1", "type": "wait", "pass": 2, "n": 9})
    snap = api_k._studio_status_snapshot()
    check("snapshot carries mode/kind for scripts",
          snap["mode"] == "script" and snap["kind"] == "script", snap)
    check("snapshot carries the live script step",
          snap["script"] == {"id": "b1", "type": "wait", "pass": 2, "n": 9},
          snap)
    api_k.studio_set_active("Kind Build")
    snap = api_k._studio_status_snapshot()
    check("snapshot kind follows the build", snap["kind"] == "build", snap)

    # the legacy embedded editor never appears under a Studio launch
    check("legacy editor refused under Studio launch",
          api_k.open_studio_window() == "studio-owns-editing")

    # studio_run flips to the entry's kind
    _orig_launch = api_k.launch
    api_k.launch = lambda data=None: "launched"
    rr = api_k.studio_run("Kind Script")
    api_k.launch = _orig_launch
    check("studio_run picks STUDIO SCRIPT for a script",
          rr["ok"] and app._studio_load()["mode"] == "script", rr)
finally:
    if api_k is not None:
        api_k._studio_status_stop.set()
        t = getattr(api_k, "_studio_status_thread", None)
        if t is not None:
            t.join(timeout=3.0)
    for k, v in _saved_attrs.items():
        setattr(app, k, v)
    app._STUDIO_LIST_CACHE["key"] = None
    _sh13.rmtree(_mdir, ignore_errors=True)

# =============================================================================
print("[14] vision — image nodes + assets")
# =============================================================================
# The engine-side image capability: the pure-Python PNG decoder, the
# template matcher, the four v3 vision nodes through the one _image_probe
# seam (attribute injection, like on_set_var), the "vision" capability
# gate, and the app-side validator acceptance.
import struct as _struct  # noqa: E402
import zlib as _zlib      # noqa: E402
import mss.tools as _mt   # noqa: E402
from prospector_engine import vision as V  # noqa: E402

np14 = po.np

# ---- PNG decoder: mss round-trip (studio_assets are written by mss/Studio) --
_src = np14.arange(9 * 7 * 3, dtype=np14.uint8).reshape(9, 7, 3)
_dec = V.decode_png_rgb(_mt.to_png(_src.tobytes(), (7, 9)))
check("png round-trip (mss.tools.to_png) decodes to the same pixels",
      _dec.shape == (9, 7, 3) and bool((_dec == _src).all()), _dec.shape)


def _png_chunk(tag, body):
    return (_struct.pack(">I", len(body)) + tag + body
            + _struct.pack(">I", _zlib.crc32(tag + body) & 0xFFFFFFFF))


def _mk_png(arr, filters):
    """Hand-encode a PNG choosing the scanline filter per row (tests the
    decoder's unfilter paths; browsers emit 1-4, mss emits 0)."""
    h, w, ch = arr.shape
    ctype = {1: 0, 3: 2, 4: 6}[ch]
    raw = bytearray()
    prev = bytes(w * ch)
    for y in range(h):
        ft = filters[y % len(filters)]
        line = bytes(arr[y].tobytes())
        enc = bytearray(line)
        for i in range(len(line)):
            a = line[i - ch] if i >= ch else 0
            b = prev[i]
            c2 = prev[i - ch] if i >= ch else 0
            if ft == 1:
                enc[i] = (line[i] - a) & 0xFF
            elif ft == 2:
                enc[i] = (line[i] - b) & 0xFF
            elif ft == 3:
                enc[i] = (line[i] - ((a + b) >> 1)) & 0xFF
            elif ft == 4:
                pa = abs(b - c2)
                pb = abs(a - c2)
                pc = abs(a + b - 2 * c2)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c2)
                enc[i] = (line[i] - pr) & 0xFF
        raw.append(ft)
        raw += enc
        prev = line
    ihdr = _struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", _zlib.compress(bytes(raw)))
            + _png_chunk(b"IEND", b""))


_arr = (np14.arange(6 * 5 * 3, dtype=np14.uint8).reshape(6, 5, 3) * 7 + 3)
check("decoder unfilters scanline filters 0-4",
      bool((V.decode_png_rgb(_mk_png(_arr, [0, 1, 2, 3, 4, 2])) == _arr).all()))
_gray = np14.arange(4 * 5, dtype=np14.uint8).reshape(4, 5, 1) * 12
_dg = V.decode_png_rgb(_mk_png(_gray, [0, 2, 1, 4]))
check("greyscale decodes to HxWx3 (replicated channels)",
      _dg.shape == (4, 5, 3) and bool((_dg[:, :, 0] == _gray[:, :, 0]).all())
      and bool((_dg[:, :, 2] == _gray[:, :, 0]).all()), _dg.shape)
_rgba = np14.zeros((3, 4, 4), np14.uint8)
_rgba[..., :3] = 200
_rgba[..., 3] = 7
_da = V.decode_png_rgb(_mk_png(_rgba, [0, 4]))
check("rgba decodes and drops alpha",
      _da.shape == (3, 4, 3) and bool((_da == 200).all()), _da.shape)
try:
    V.decode_png_rgb(b"\x89PNG\r\n\x1a\n" + b"junk")
    check("truncated png refused with a clear error", False)
except ValueError:
    check("truncated png refused with a clear error", True)
_pal = (b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", _struct.pack(">IIBBBBB", 2, 2, 8, 3, 0, 0, 0))
        + _png_chunk(b"IDAT", _zlib.compress(b"\x00\x00\x00\x00\x00\x00"))
        + _png_chunk(b"IEND", b""))
try:
    V.decode_png_rgb(_pal)
    check("palette png refused (color type named)", False)
except ValueError as e:
    check("palette png refused (color type named)", "color type" in str(e), e)

# ---- matcher ----------------------------------------------------------------
_big = np14.full((60, 80, 3), 20, np14.uint8)
_patch = (np14.arange(10 * 12 * 3, dtype=np14.uint8).reshape(10, 12, 3) + 60)
_big[23:33, 40:52] = _patch
_r = V.match_template(_big, _patch, 0.9)
check("matcher finds the patch at its centre with score 1.0",
      _r["found"] and _r["score"] == 1.0
      and _r["x"] == 40 + 12 // 2 and _r["y"] == 23 + 10 // 2, _r)
_r2 = V.match_template(_big, (255 - _patch), 0.9)
check("matcher rejects below threshold",
      not _r2["found"] and _r2["score"] < 0.9, _r2)
check("template larger than the image is a clean miss",
      not V.match_template(_patch, _big, 0.5)["found"])
_big2 = np14.zeros((200, 300, 3), np14.uint8)
_tpl2 = (np14.arange(48 * 52 * 3, dtype=np14.uint8) * 5 + 1).reshape(48, 52, 3)
_big2[77:125, 133:185] = _tpl2
_r4 = V.match_template(_big2, _tpl2, 0.95)
check("stride-sampled search still lands exactly (score 1.0)",
      _r4["found"] and _r4["score"] == 1.0
      and _r4["x"] == 133 + 26 and _r4["y"] == 77 + 24, _r4)
check("asset id rules ([A-Za-z0-9_-]{1,64})",
      V.asset_id_ok("Btn_ok-2") and not V.asset_id_ok("../evil")
      and not V.asset_id_ok("") and not V.asset_id_ok("x" * 65)
      and not V.asset_id_ok(None) and not V.asset_id_ok("a b"))

# ---- schema drift guards ----------------------------------------------------
check("v3 handler table covers the four vision nodes",
      all(t in po._SCRIPT_HANDLERS_V3 for t in
          ("wait_image", "if_image", "click_image", "move_image")))
check("app schema and engine handler tables agree (v3)",
      set(po._SCRIPT_HANDLERS_V3) == set(ui.STUDIO3_TYPES),
      set(po._SCRIPT_HANDLERS_V3) ^ set(ui.STUDIO3_TYPES))
check("cap tables agree across app and engine",
      po._SCRIPT3_CAP_OF == ui.STUDIO3_CAP_OF
      and po._SCRIPT3_CAP_LABEL == ui.STUDIO3_CAP_LABEL
      and tuple(po._SCRIPT3_CAPS) == tuple(ui.STUDIO3_CAPS))

# ---- runner: the _image_probe seam ------------------------------------------
install_stubs()
po.button_down = lambda b, cs=1: ACTIONS.append(("bd", b, cs))
po.button_up = lambda b, cs=1: ACTIONS.append(("bu", b, cs))

PROBES = []


def v14_runner(blocks, probe, ticks=50, caps=("vision",)):
    r = po.ScriptRunner(json.dumps(v3_script(blocks, list(caps))), "V3")
    r._image_probe = lambda image_id, region, threshold: (
        PROBES.append((image_id, region, threshold))
        or probe(image_id, region, threshold))
    det = FakeDet()
    det_live[0] = det
    for _ in range(ticks):
        if not po.State.running:
            break
        r.tick(det)
    return r


# caps enforcement (same style as the other caps)
fresh_state()
r = po.ScriptRunner(json.dumps(v3_script(
    [{"id": "w", "type": "wait_image", "params": {"image": "btn"}}])), "V3")
check("undeclared vision cap refuses the script",
      "does not declare" in r.dead
      and "read the screen to find images" in r.dead, r.dead)
r = po.ScriptRunner(json.dumps(v3_script(
    [{"id": "w", "type": "wait_image", "params": {"image": "btn"}}],
    caps=["vision"])), "V3")
check("declared vision cap loads", r.dead == "", r.dead)
r = po.ScriptRunner(json.dumps({
    "format": "ppscript", "version": 2, "name": "V2",
    "blocks": [{"id": "w", "type": "wait_image",
                "params": {"image": "btn"}}]}), "V2")
check("v2 does not understand vision blocks",
      "does not understand" in r.dead, r.dead)

# wait_image: found -> continues; the probe gets id + region + threshold
fresh_state()
del PROBES[:]
v14_runner(
    [{"id": "w", "type": "wait_image",
      "params": {"image": "btn", "timeout_ms": 5000, "threshold": 95,
                 "x1": 10, "y1": 20, "x2": 400, "y2": 300}},
     {"id": "s", "type": "stop", "params": {"message": "found it"}}],
    lambda i, reg, thr: (True, 200, 150, 0.99), ticks=5)
stops = [a for a in ACTIONS if a[0] == "safe_stop"]
check("wait_image found continues to the next node",
      len(stops) == 1 and "found it" in stops[0][1], stops)
check("probe receives id + region + threshold",
      PROBES and PROBES[0] == ("btn", (10, 20, 400, 300), 95), PROBES[:1])

# wait_image: timeout -> safe stop naming the node (default on_timeout=stop)
fresh_state()
v14_runner([{"id": "w", "type": "wait_image",
             "params": {"image": "btn", "timeout_ms": 120}}],
           lambda i, reg, thr: (False, 0, 0, 0.0), ticks=3)
stops = [a for a in ACTIONS if a[0] == "safe_stop"]
check("wait_image timeout safe-stops naming image and node",
      len(stops) == 1 and '"btn"' in stops[0][1]
      and "wait_image" in stops[0][1], stops)

# wait_image: on_timeout=continue keeps walking
fresh_state()
v14_runner([{"id": "w", "type": "wait_image",
             "params": {"image": "btn", "timeout_ms": 120,
                        "on_timeout": "continue"}},
            {"id": "k", "type": "tap_key", "params": {"key": "1"}}],
           lambda i, reg, thr: (False, 0, 0, 0.0), ticks=3)
check("wait_image on_timeout=continue keeps running",
      not [a for a in ACTIONS if a[0] == "safe_stop"]
      and [a for a in ACTIONS if a[0] == "kd"], ACTIONS)

# if_image: children vs else branch
_ifb = [{"id": "i", "type": "if_image", "params": {"image": "btn"},
         "children": [{"id": "y", "type": "tap_key", "params": {"key": "1"}}],
         "else": [{"id": "n", "type": "tap_key", "params": {"key": "2"}}]},
        {"id": "s", "type": "stop", "params": {"message": "done"}}]
fresh_state()
v14_runner(_ifb, lambda i, reg, thr: (True, 5, 5, 1.0), ticks=6)
kds = [a[1] for a in ACTIONS if a[0] == "kd"]
check("if_image found runs the children", kds == [po._SCRIPT_KEYS["1"]], kds)
fresh_state()
v14_runner(_ifb, lambda i, reg, thr: (False, 0, 0, 0.0), ticks=6)
kds = [a[1] for a in ACTIONS if a[0] == "kd"]
check("if_image not-found runs the else branch",
      kds == [po._SCRIPT_KEYS["2"]], kds)

# click_image: move to match+offset, settle, paired clicks with click_state
fresh_state()
v14_runner([{"id": "c", "type": "click_image",
             "params": {"image": "btn", "button": "right", "clicks": 2,
                        "dx": 4, "dy": -6}},
            {"id": "s", "type": "stop", "params": {"message": "done"}}],
           lambda i, reg, thr: (True, 300, 200, 0.97), ticks=5)
moves = [a for a in ACTIONS if a[0] == "move"]
bds = [a for a in ACTIONS if a[0] == "bd"]
bus = [a for a in ACTIONS if a[0] == "bu"]
check("click_image moves to match+offset then double right-clicks",
      moves == [("move", 304, 194)]
      and bds == [("bd", "right", 1), ("bd", "right", 2)]
      and bus == [("bu", "right", 1), ("bu", "right", 2)], ACTIONS)

# click_image: miss stops by default, no clicks sent
fresh_state()
v14_runner([{"id": "c", "type": "click_image", "params": {"image": "btn"}}],
           lambda i, reg, thr: (False, 0, 0, 0.0), ticks=3)
stops = [a for a in ACTIONS if a[0] == "safe_stop"]
check("click_image miss safe-stops by default (zero clicks)",
      len(stops) == 1 and "click_image" in stops[0][1]
      and not [a for a in ACTIONS if a[0] == "bd"], ACTIONS)

# click_image: on_miss=continue skips ahead
fresh_state()
v14_runner([{"id": "c", "type": "click_image",
             "params": {"image": "btn", "on_miss": "continue"}},
            {"id": "k", "type": "tap_key", "params": {"key": "3"}}],
           lambda i, reg, thr: (False, 0, 0, 0.0), ticks=3)
check("click_image on_miss=continue skips ahead",
      not [a for a in ACTIONS if a[0] == "safe_stop"]
      and [a for a in ACTIONS if a[0] == "kd"]
      and not [a for a in ACTIONS if a[0] == "bd"], ACTIONS)

# move_image parks the cursor at match+offset
fresh_state()
v14_runner([{"id": "m", "type": "move_image",
             "params": {"image": "btn", "dx": -10, "dy": 5}},
            {"id": "s", "type": "stop", "params": {"message": "done"}}],
           lambda i, reg, thr: (True, 100, 80, 1.0), ticks=4)
check("move_image parks the cursor at match+offset",
      [a for a in ACTIONS if a[0] == "move"] == [("move", 90, 85)], ACTIONS)

# a non-id image name safe-stops before any probe runs
fresh_state()
del PROBES[:]
v14_runner([{"id": "w", "type": "wait_image",
             "params": {"image": "../evil"}}],
           lambda i, reg, thr: (True, 0, 0, 1.0), ticks=3)
stops = [a for a in ACTIONS if a[0] == "safe_stop"]
check("a non-id image name safe-stops before any probe",
      len(stops) == 1 and "reference image" in stops[0][1]
      and not PROBES, (stops, PROBES))

# ---- app-side validator -----------------------------------------------------
_v3v = {"format": "ppscript", "version": 3, "name": "Vision",
        "caps": ["vision"], "blocks": [
            {"id": "w1", "type": "wait_image",
             "params": {"image": "pan_btn", "timeout_ms": 4000,
                        "threshold": 92, "x1": 0, "y1": 0, "x2": 0, "y2": 0,
                        "on_timeout": "stop"}},
            {"id": "i1", "type": "if_image", "params": {"image": "pan_btn"},
             "children": [{"id": "c1", "type": "click_image",
                           "params": {"image": "pan_btn", "button": "left",
                                      "clicks": 1, "dx": 0, "dy": 0,
                                      "on_miss": "continue"}}],
             "else": [{"id": "m1", "type": "move_image",
                       "params": {"image": "pan_btn", "dx": 5, "dy": 5}}]}]}
r = app._studio_validate_v2(_v3v)
check("validator accepts the four vision nodes with the cap declared",
      r["ok"], r["errors"])
r = app._studio_validate_v2(dict(_v3v, caps=[]))
check("validator refuses an undeclared vision cap with the label",
      not r["ok"] and any("read the screen to find images" in e
                          for e in r["errors"]), r["errors"])
r = app._studio_validate_v2(dict(_v3v, version=2))
check("v2 refuses vision nodes as unknown types",
      not r["ok"] and any("Unknown block type" in e for e in r["errors"]),
      r["errors"])
_scr = json.loads(json.dumps(_v3v))
_scr["caps"] = ["vision", "telepathy"]
check("unknown cap refused by the validator",
      not app._studio_validate_v2(_scr)["ok"])

# =============================================================================
print("[15] dynamic settings — declared parameters from the pushed graph")
# =============================================================================
# settings.params (written by Prospector Studio at publish) renders as the
# macro's Studio-mode settings: sanitized against the document, edited
# through studio_set_param (coerce + clamp + whole-document re-validation
# with rollback), re-pushed into the engine config, mirrored to Studio.
import shutil as _sh15
import tempfile as _tf15

_mdir = _tf15.mkdtemp()
_saved_attrs = {k: getattr(app, k) for k in
                ("SCRIPTS_FILE", "CONFIG_FILE", "DATA_DIR", "STATUS_FILE",
                 "PUSH_FILE", "STUDIO_LAUNCH", "STUDIO_SCRIPT")}
app.SCRIPTS_FILE = os.path.join(_mdir, "prospecting_scripts.json")
app.CONFIG_FILE = os.path.join(_mdir, "prospecting_config.json")
app.STATUS_FILE = os.path.join(_mdir, "studio_macro_status.json")
app.PUSH_FILE = os.path.join(_mdir, "studio_push.json")
app.DATA_DIR = _mdir
app.STUDIO_LAUNCH = True
app.STUDIO_SCRIPT = ""
app._STUDIO_LIST_CACHE["key"] = None
api_p = None
try:
    api_p = app.Api()
    doc = {"format": "ppscript", "version": 2, "kind": "script",
           "name": "Param Script",
           "variables": [
               {"name": "walk_ms", "type": "number", "initial": 1200},
               {"name": "announce", "type": "bool", "initial": True},
               {"name": "route", "type": "string", "initial": "east"}],
           "blocks": [
               {"id": "w1", "type": "wait",
                "params": {"ms": {"$expr": {"var": "walk_ms"}}}},
               {"id": "w2", "type": "wait", "params": {"ms": 800}}],
           "settings": {"params": [
               {"name": "walk_ms", "kind": "variable", "type": "number",
                "label": "Walk time", "group": "Timing", "unit": "ms",
                "desc": "How long each leg walks.", "default": 1200,
                "min": 100, "max": 5000, "step": 50},
               {"name": "announce", "kind": "variable", "type": "bool",
                "label": "Announce laps", "default": True},
               {"name": "route", "kind": "variable", "type": "choice",
                "label": "Route", "options": ["east", "west"],
                "default": "east"},
               {"name": "settle", "kind": "node", "node": "w2", "key": "ms",
                "type": "number", "label": "Settle wait", "group": "Timing",
                "unit": "ms", "default": 800, "min": 100, "max": 5000},
               {"name": "ghost", "kind": "variable", "type": "number",
                "label": "No such variable", "default": 1},
               {"name": "badnode", "kind": "node", "node": "nope",
                "key": "ms", "type": "number", "default": 1}]}}
    check("param fixture saves clean", api_p.studio_save(doc, None)["ok"])
    api_p.studio_set_active("Param Script")

    r = api_p.studio_params()
    names = [p["name"] for p in r["params"]]
    check("declared params surface, malformed ones skipped",
          names == ["walk_ms", "announce", "route", "settle"], names)
    walk = r["params"][0]
    check("param carries meta + live value",
          walk["label"] == "Walk time" and walk["unit"] == "ms"
          and walk["current"] == 1200 and walk["default"] == 1200
          and walk["group"] == "Timing", walk)
    settle = r["params"][3]
    check("node-backed param reads the block's live value",
          settle["kind"] == "node" and settle["current"] == 800, settle)

    # numeric edit: clamped, persisted, re-pushed
    r = api_p.studio_set_param("walk_ms", 99999)
    check("number clamps to the declared max",
          r["ok"] and r["value"] == 5000 and r["effective"] == "next-run", r)
    d = app._studio_load()
    check("variable initial persisted",
          d["scripts"]["Param Script"]["variables"][0]["initial"] == 5000)
    cfg = app.load_saved()
    check("edit re-pushed into the engine config",
          '"initial": 5000' in cfg.get("SCRIPT_JSON", "")
          or '"initial":5000' in cfg.get("SCRIPT_JSON", ""))

    # node-backed edit lands in the block
    r = api_p.studio_set_param("settle", 950)
    d = app._studio_load()
    check("node param edit lands in the block",
          r["ok"] and d["scripts"]["Param Script"]["blocks"][1]["params"]["ms"] == 950, r)

    # choice + bool + refusals
    check("choice refuses off-list values",
          not api_p.studio_set_param("route", "north")["ok"])
    check("choice accepts listed values",
          api_p.studio_set_param("route", "west")["ok"])
    check("bool coerces", api_p.studio_set_param("announce", 0)["ok"]
          and app._studio_load()["scripts"]["Param Script"]["variables"][1]["initial"] is False)
    check("unknown param refused with the reason",
          "declares no" in (api_p.studio_set_param("nope", 1)["error"] or ""))

    # low values clamp to the declared min; strings past the schema cap refuse
    r = api_p.studio_set_param("settle", 0)
    check("number clamps to the declared min", r["ok"] and r["value"] == 100, r)
    long_p = api_p.studio_set_param("route", "x" * 500)
    check("too-long strings refused", not long_p["ok"])

    # status mirror carries the live values
    snap = api_p._studio_status_snapshot()
    check("mirror carries the param values",
          snap["params"].get("walk_ms") == 5000
          and snap["params"].get("settle") == 100, snap.get("params"))
finally:
    if api_p is not None:
        api_p._studio_status_stop.set()
        t = getattr(api_p, "_studio_status_thread", None)
        if t is not None:
            t.join(timeout=3.0)
    for k, v in _saved_attrs.items():
        setattr(app, k, v)
    app._STUDIO_LIST_CACHE["key"] = None
    _sh15.rmtree(_mdir, ignore_errors=True)

# =============================================================================
print("[16] classic handoff + run identity (Track E)")
# =============================================================================
# Prospector Studio pushes a CLASSIC preset: the effective build lands in
# prospecting_builds.json and studio_push.json gains kind=="classic". The
# macro applies it at boot + 1 Hz (exactly once per stamp), defers while a
# run is live (push_pending in the mirror), and refuses malformed pushes
# cleanly. Runs gain a shared identity: engine run_id -> history entry id,
# mirror run_id; entries record mode + pushed rev provenance.
import shutil as _sh16
import tempfile as _tf16

_mdir = _tf16.mkdtemp()
_saved_attrs = {k: getattr(app, k) for k in
                ("SCRIPTS_FILE", "CONFIG_FILE", "DATA_DIR", "STATUS_FILE",
                 "PUSH_FILE", "BUILDS_FILE", "STUDIO_LAUNCH", "STUDIO_SCRIPT")}
app.SCRIPTS_FILE = os.path.join(_mdir, "prospecting_scripts.json")
app.CONFIG_FILE = os.path.join(_mdir, "prospecting_config.json")
app.STATUS_FILE = os.path.join(_mdir, "studio_macro_status.json")
app.PUSH_FILE = os.path.join(_mdir, "studio_push.json")
app.BUILDS_FILE = os.path.join(_mdir, "prospecting_builds.json")
app.DATA_DIR = _mdir
app.STUDIO_LAUNCH = True
app.STUDIO_SCRIPT = "Pushed Build"
app._STUDIO_LIST_CACHE["key"] = None
api_c = None
try:
    api_c = app.Api()
    # deterministic: stop the 1 Hz loop and drive the application by hand
    api_c._studio_status_stop.set()
    _t16 = getattr(api_c, "_studio_status_thread", None)
    if _t16 is not None:
        _t16.join(timeout=3.0)

    tpl = json.loads(json.dumps(app._studio_templates()[0]))
    tpl["name"] = "Pushed Build"
    check("handoff fixture saves clean", api_c.studio_save(tpl, None)["ok"])
    api_c.studio_mode("studio")      # active build: the handoff must switch away
    with open(app.BUILDS_FILE, "w") as f:
        json.dump({"Speed Build": {"DIG_CLICK_MS": 777, "TRACKER_MODE": False,
                                   "_meta": {"desc": "", "created": 1,
                                             "updated": 1, "used": 0}}}, f)
    with open(app.PUSH_FILE, "w") as f:
        json.dump({"v": 1, "name": "Speed Build", "rev": "a1b2c3d4",
                   "kind": "classic", "at": 1111, "app": "ProspectorStudio"}, f)
    api_c._apply_classic_push()
    d = app._studio_load()
    cfg = app.load_saved()
    check("classic push switches the top-level mode to CLASSIC",
          app._studio_ui_mode(d) == "classic" and d["active"] == "",
          (d["mode"], d["active"]))
    check("classic push merges the build into the config (load_build)",
          cfg.get("DIG_CLICK_MS") == 777, cfg.get("DIG_CLICK_MS"))
    check("applied stamp recorded in the scripts-store meta",
          d["meta"].get("last_classic_push")
          == {"name": "Speed Build", "rev": "a1b2c3d4", "at": 1111},
          d["meta"].get("last_classic_push"))
    check("nothing pending after a clean apply", api_c._push_pending is False)
    check("toast queued for the UI",
          "Speed Build" in (api_c._classic_toast or ""), api_c._classic_toast)
    used0 = app._read_json(app.BUILDS_FILE, {})["Speed Build"]["_meta"]["used"]
    api_c._apply_classic_push()      # same stamp again
    used1 = app._read_json(app.BUILDS_FILE, {})["Speed Build"]["_meta"]["used"]
    check("the same stamp is applied exactly once", used0 == 1 and used1 == 1,
          (used0, used1))

    # a run is live: the fresh stamp defers, the mirror says push_pending
    with open(app.PUSH_FILE, "w") as f:
        json.dump({"v": 1, "name": "Speed Build", "rev": "55667788",
                   "kind": "classic", "at": 2222}, f)
    api_c.proc = object()
    api_c._apply_classic_push()
    check("push deferred while a run is live", api_c._push_pending is True)
    check("deferred push does not touch the applied stamp",
          app._studio_load()["meta"]["last_classic_push"]["rev"] == "a1b2c3d4")
    api_c._last_stats = {"cycles": 3, "runtime_s": 20, "run_id": "r99-1-2"}
    snap = api_c._studio_status_snapshot()
    check("status mirror carries run_id + push_pending",
          snap.get("run_id") == "r99-1-2"
          and snap.get("push_pending") is True,
          {k: snap.get(k) for k in ("run_id", "push_pending")})
    api_c.proc = None
    api_c._apply_classic_push()      # run over: the deferred push lands
    check("deferred push applies once the run ends",
          app._studio_load()["meta"]["last_classic_push"]["rev"] == "55667788"
          and api_c._push_pending is False)
    check("mirror drops push_pending after the apply",
          api_c._studio_status_snapshot().get("push_pending") is False)

    # malformed pushes: ignored + logged once, never a crash, never applied
    with open(app.PUSH_FILE, "w") as f:
        f.write("{this is not json")
    api_c._apply_classic_push()
    check("garbled push file ignored cleanly",
          api_c._classic_push_warned is True
          and app._studio_load()["meta"]["last_classic_push"]["rev"]
          == "55667788")
    api_c._classic_push_warned = False
    with open(app.PUSH_FILE, "w") as f:
        json.dump({"v": 1, "kind": "classic", "rev": "ffffffff", "at": 9}, f)
    api_c._apply_classic_push()
    check("classic push without a name refused cleanly",
          api_c._classic_push_warned is True)
    api_c._classic_push_warned = False
    with open(app.PUSH_FILE, "w") as f:
        json.dump({"v": 1, "name": "Ghost", "rev": "eeeeeeee",
                   "kind": "classic", "at": 10}, f)
    api_c._apply_classic_push()
    check("classic push naming a missing build refused cleanly",
          api_c._classic_push_warned is True
          and app._studio_load()["meta"]["last_classic_push"]["rev"]
          == "55667788")
    # a build/script publish stamp (no classic kind) is not the handoff's
    with open(app.PUSH_FILE, "w") as f:
        json.dump({"v": 1, "name": "Pushed Build", "rev": "ab12cd34",
                   "at": 3333}, f)
    api_c._classic_push_warned = False
    api_c._apply_classic_push()
    check("non-classic publish stamps are left alone",
          api_c._classic_push_warned is False
          and app._studio_load()["meta"]["last_classic_push"]["rev"]
          == "55667788")

    # run identity + provenance land in the history entry
    api_c._run_active = True
    api_c._run_mode = "studio"
    api_c._run_rev = "ab12cd34"
    api_c._events = []
    api_c._last_stats = {"cycles": 5, "runtime_s": 60,
                         "run_id": "r170000-42-1", "stop_reason": "manual"}
    api_c._save_history("manual")
    hist = app._read_json(os.path.join(_mdir, "run_history.json"), [])
    check("history entry carries id/mode/rev",
          bool(hist) and hist[-1].get("id") == "r170000-42-1"
          and hist[-1].get("mode") == "studio"
          and hist[-1].get("rev") == "ab12cd34",
          hist[-1] if hist else None)
    api_c._run_active = True
    api_c._run_mode = ""
    api_c._run_rev = ""
    api_c._last_stats = {"cycles": 2, "runtime_s": 30}
    api_c._save_history("manual")
    hist = app._read_json(os.path.join(_mdir, "run_history.json"), [])
    check("a legacy run without run_id fabricates no id",
          bool(hist) and "id" not in hist[-1] and "rev" not in hist[-1],
          hist[-1] if hist else None)

    # analytics: the REAL engine keys pass through untouched
    api_c._last_stats = {"cycles": 4, "shake_retries": 3, "shake_fails": 1}
    dd = api_c.analytics_data()
    check("analytics_data passes shake_retries/shake_fails through",
          dd["stats"].get("shake_retries") == 3
          and dd["stats"].get("shake_fails") == 1, dd["stats"])

    # rendered-surface truth (string checks, the way the suite asserts markup)
    html16 = app.build_html()
    check("STUDIO BUILD shows the live step card (mode-studio CSS)",
          'body.studiolaunch.mode-studio #runscriptcard{display:block}'
          in html16)
    check("STUDIO SCRIPT keeps its step card",
          'body.studiolaunch.mode-script .scriptcard{display:block}'
          in html16)
    check("step card label follows the mode",
          "'build step':'current step'" in html16)
    for _t16n in ("hist", "cal"):
        check("both Studio modes keep the %s tab visible" % _t16n,
              ('mode-studio .tab[data-tab="%s"]' % _t16n) not in html16
              and ('mode-script .tab[data-tab="%s"]' % _t16n) not in html16)
    check("analytics shake card honest about absent data",
          "s.shake_retries!=null" in app.ANALYTICS_HTML
          and "s.shake_fails!=null" in app.ANALYTICS_HTML)
    hud16 = app._hud_html()
    check("HUD draws an as-observed strip for BUILD runs",
          "drawObserved" in hud16 and "build phases, as observed" in hud16)
    check("HUD hides the classic diagram for SCRIPT runs",
          "scriptmode" in hud16 and "MODE==='script'" in hud16)
    check("HUD classic diagram models CLASSIC runs only",
          "MODE!=='classic'" in hud16)
finally:
    if api_c is not None:
        api_c._studio_status_stop.set()
        _t16 = getattr(api_c, "_studio_status_thread", None)
        if _t16 is not None:
            _t16.join(timeout=3.0)
    for k, v in _saved_attrs.items():
        setattr(app, k, v)
    app._STUDIO_LIST_CACHE["key"] = None
    _sh16.rmtree(_mdir, ignore_errors=True)

# =============================================================================
print("[17] capability manifest (generated from the live registries)")
# =============================================================================
_caps = po.script_capabilities()
check("capabilities: every v1 handler is advertised",
      _caps["blocks"]["1"] == sorted(po._SCRIPT_HANDLERS),
      _caps["blocks"]["1"])
check("capabilities: every v4 handler is advertised (no drift possible)",
      _caps["blocks"]["4"] == sorted(po._SCRIPT_HANDLERS_V4),
      len(_caps["blocks"]["4"]))
check("capabilities: version tables nest (v1 ⊆ v2 ⊆ v3 ⊆ v4)",
      set(_caps["blocks"]["1"]) <= set(_caps["blocks"]["2"])
      <= set(_caps["blocks"]["3"]) <= set(_caps["blocks"]["4"]), None)
import hashlib as _hl
with open(os.path.join(ROOT, "prospector_engine", "engine.py"), "rb") as _f:
    _fp = _hl.sha256(_f.read()).hexdigest()[:16]
check("capabilities: fingerprint IS the engine source on disk", _caps["fingerprint"] == _fp,
      (_caps["fingerprint"], _fp))
from prospector_engine import protocol as _pr
check("capabilities: protocol matches the live protocol module",
      _caps["protocol"] == {"major": _pr.PROTOCOL_MAJOR, "minor": _pr.PROTOCOL_MINOR},
      _caps["protocol"])
check("capabilities: ops/reads/caps advertised from the live tables",
      _caps["ops"]["4"] == sorted(po._SCRIPT4_OPS)
      and _caps["reads"]["4"] == sorted(po._SCRIPT4_READS)
      and _caps["caps"] == sorted(po._SCRIPT3_CAPS), None)

# =============================================================================
print("[18] runner identity — durable instance GUID + publish acknowledgement")
# =============================================================================
# Protocol 1.5: the app and the engine share ONE instance_id file per data
# dir; the status mirror carries {instance} and {ack} so Prospector Studio
# can verify WHICH runner acknowledged WHICH push; a classic push deferred
# behind a live run is ackPending with the pending rev. Everything added is
# optional to consumers -- older mirrors simply lack the keys.
import shutil as _sh18
import tempfile as _tf18

check("protocol 1.5: minor covers the identity fields",
      _pr.PROTOCOL_MINOR >= 5, _pr.PROTOCOL_MINOR)

_mdir = _tf18.mkdtemp()
_saved_attrs = {k: getattr(app, k) for k in
                ("SCRIPTS_FILE", "CONFIG_FILE", "DATA_DIR", "STATUS_FILE",
                 "PUSH_FILE", "BUILDS_FILE", "STUDIO_LAUNCH", "STUDIO_SCRIPT")}
app.SCRIPTS_FILE = os.path.join(_mdir, "prospecting_scripts.json")
app.CONFIG_FILE = os.path.join(_mdir, "prospecting_config.json")
app.STATUS_FILE = os.path.join(_mdir, "studio_macro_status.json")
app.PUSH_FILE = os.path.join(_mdir, "studio_push.json")
app.BUILDS_FILE = os.path.join(_mdir, "prospecting_builds.json")
app.DATA_DIR = _mdir
app.STUDIO_LAUNCH = True
app.STUDIO_SCRIPT = "Pushed Build"
app._STUDIO_LIST_CACHE["key"] = None
api_i = None
try:
    # -- the durable GUID: file wins, minted once, engine-compatible --------
    app._INSTANCE_ID_CACHE.clear()
    _idf = os.path.join(_mdir, "instance_id")
    with open(_idf, "w") as f:
        f.write("engine-minted-guid-0001\n")
    check("instance GUID: an engine-minted file is honored verbatim",
          app._runner_instance_id() == "engine-minted-guid-0001")
    os.remove(_idf)
    app._INSTANCE_ID_CACHE.clear()
    _g1 = app._runner_instance_id()
    check("instance GUID: minted once when absent", bool(_g1) and len(_g1) >= 8, _g1)
    check("instance GUID: persisted into <data dir>/instance_id",
          os.path.exists(_idf) and open(_idf).read().strip() == _g1)
    app._INSTANCE_ID_CACHE.clear()          # "restart": read path, not cache
    check("instance GUID: a second boot reads the same GUID",
          app._runner_instance_id() == _g1)
    from prospector_engine import ipc as _ipc18
    check("instance GUID: the ENGINE reads the same file for this home",
          _ipc18.instance_identity(_mdir) == _g1)

    api_i = app.Api()
    api_i._studio_status_stop.set()          # deterministic: drive by hand
    _t18 = getattr(api_i, "_studio_status_thread", None)
    if _t18 is not None:
        _t18.join(timeout=3.0)

    # -- mirror instance object: app-side fallback (no engine hello yet) ----
    snap = api_i._studio_status_snapshot()
    inst = snap.get("instance")
    check("mirror instance: present with the durable GUID + data dir",
          isinstance(inst, dict) and inst.get("instanceId") == _g1
          and inst.get("dataDir") == _mdir and bool(inst.get("exePath")), inst)
    check("mirror instance: protocol rides along (minor >= 5)",
          isinstance(inst.get("protocol"), dict)
          and inst["protocol"].get("minor", 0) >= 5, inst.get("protocol"))
    import hashlib as _hl18
    _efp = _hl18.sha256(open(os.path.join(
        ROOT, "prospector_engine", "engine.py"), "rb").read()).hexdigest()[:16]
    check("mirror instance: fingerprint IS the vendored engine source",
          inst.get("fingerprint") == _efp, (inst.get("fingerprint"), _efp))

    # -- mirror instance object: the engine's hello wins once it speaks -----
    api_i._on_engine_event({"t": "ev", "seq": 1, "ts": 0.0,
                            "ev": "engine.hello", "data": {
                                "protocol": {"major": 1, "minor": 5},
                                "engine": {"version": "9.9.9",
                                           "sourceFingerprint": "cafe0123beef4567",
                                           "platform": "mac",
                                           "instance": "hello-guid-42",
                                           "exePath": "/usr/bin/python3",
                                           "frozen": False,
                                           "dataDir": _mdir},
                                "pid": 4242, "home": _mdir,
                                "capabilities": {}, "state": "idle"}})
    inst = api_i._studio_status_snapshot().get("instance")
    check("mirror instance: hello identity mirrored verbatim",
          inst.get("instanceId") == "hello-guid-42" and inst.get("pid") == 4242
          and inst.get("fingerprint") == "cafe0123beef4567"
          and inst.get("dataDir") == _mdir, inst)
    api_i._engine_instance = None            # back to the app-side truth

    # -- classic push: the ack object names WHO applied it, WHEN ------------
    tpl = json.loads(json.dumps(app._studio_templates()[0]))
    tpl["name"] = "Pushed Build"
    check("identity fixture saves clean", api_i.studio_save(tpl, None)["ok"])
    api_i.studio_mode("studio")
    with open(app.BUILDS_FILE, "w") as f:
        json.dump({"Speed Build": {"DIG_CLICK_MS": 777, "TRACKER_MODE": False,
                                   "_meta": {"desc": "", "created": 1,
                                             "updated": 1, "used": 0}}}, f)
    with open(app.PUSH_FILE, "w") as f:
        json.dump({"v": 1, "name": "Speed Build", "rev": "a1b2c3d4",
                   "kind": "classic", "at": 1111, "app": "ProspectorStudio"}, f)
    api_i._apply_classic_push()
    d18 = app._studio_load()
    check("dedup stamp keeps its exact pre-1.5 shape {name, rev, at}",
          d18["meta"].get("last_classic_push")
          == {"name": "Speed Build", "rev": "a1b2c3d4", "at": 1111},
          d18["meta"].get("last_classic_push"))
    _ack = d18["meta"].get("last_classic_push_ack")
    check("applied ack stamped with appliedAt + instanceId",
          isinstance(_ack, dict) and _ack.get("name") == "Speed Build"
          and _ack.get("rev") == "a1b2c3d4" and _ack.get("at") == 1111
          and _ack.get("instanceId") == _g1
          and isinstance(_ack.get("appliedAt"), float), _ack)
    snap = api_i._studio_status_snapshot()
    check("mirror ack echoes the applied classic push",
          snap.get("ack") == _ack and snap.get("ackPending") is False
          and snap.get("pendingRev") == "",
          {k: snap.get(k) for k in ("ack", "ackPending", "pendingRev")})

    # -- deferred classic push: ackPending truth, old ack preserved ---------
    with open(app.PUSH_FILE, "w") as f:
        json.dump({"v": 1, "name": "Speed Build", "rev": "55667788",
                   "kind": "classic", "at": 2222}, f)
    api_i.proc = object()                    # a run is live
    api_i._apply_classic_push()
    snap = api_i._studio_status_snapshot()
    check("deferred push: ackPending with the PENDING rev",
          snap.get("ackPending") is True and snap.get("pendingRev") == "55667788"
          and snap.get("push_pending") is True,
          {k: snap.get(k) for k in ("ackPending", "pendingRev")})
    check("deferred push: the ack still names the OLD applied rev",
          (snap.get("ack") or {}).get("rev") == "a1b2c3d4", snap.get("ack"))
    api_i.proc = None
    api_i._apply_classic_push()              # run over: the deferred push lands
    snap = api_i._studio_status_snapshot()
    check("run over: ack flips to the new rev, nothing pending",
          (snap.get("ack") or {}).get("rev") == "55667788"
          and (snap.get("ack") or {}).get("instanceId") == _g1
          and snap.get("ackPending") is False and snap.get("pendingRev") == "",
          snap.get("ack"))

    # -- script/build push: acknowledged the moment it is the active entry --
    api_i.studio_mode("studio")              # restores "Pushed Build" active
    with open(app.PUSH_FILE, "w") as f:
        json.dump({"v": 1, "name": "Pushed Build", "rev": "beefcafe",
                   "kind": "script", "at": 3333}, f)
    snap = api_i._studio_status_snapshot()
    check("script push: ack carries rev + this runner's GUID",
          (snap.get("ack") or {}).get("rev") == "beefcafe"
          and (snap.get("ack") or {}).get("instanceId") == _g1, snap.get("ack"))
    with open(app.PUSH_FILE, "w") as f:
        json.dump({"v": 1, "name": "Some Other Script", "rev": "beefcafe",
                   "kind": "script", "at": 4444}, f)
    check("script push naming a non-active entry is NOT acknowledged",
          api_i._studio_status_snapshot().get("ack") is None)
finally:
    if api_i is not None:
        api_i._studio_status_stop.set()
        _t18 = getattr(api_i, "_studio_status_thread", None)
        if _t18 is not None:
            _t18.join(timeout=3.0)
    for k, v in _saved_attrs.items():
        setattr(app, k, v)
    app._STUDIO_LIST_CACHE["key"] = None
    app._INSTANCE_ID_CACHE.clear()
    _sh18.rmtree(_mdir, ignore_errors=True)

# =============================================================================
print("[19] mark_rung (session ten): state-only breadcrumb + flow seam")
# =============================================================================
# mark_rung {id: R1..R4} calls the existing _note_rung_fired seam: the flow
# manager's recovery{rung} trigger and meta.recovery see the firing; NOTHING
# is emitted (no event, no input, no sleep) and flow continues.
install_stubs()
fresh_state()


class _RungRT(object):
    def __init__(self):
        self.fired = []

    def note_builtin_fired(self, rid):
        self.fired.append(rid)


def runner_v4_for(blocks, ticks=200, variables=None):
    """runner_for, but a VERSION 4 document (mark_rung is a v4 op)."""
    s = {"format": "ppscript", "version": 4, "name": "T", "blocks": blocks}
    if variables:
        s["variables"] = variables
    r = po.ScriptRunner(json.dumps(s), "T")
    det = FakeDet()
    det_live[0] = det
    for _ in range(ticks):
        if not po.State.running:
            break
        r.tick(det)
    return r, det


_rt = _RungRT()
po.State.recovery = _rt
_r19, _det19 = runner_v4_for([
    {"id": "a", "type": "mark_rung", "params": {"id": "R2"}},
    {"id": "b", "type": "set_var", "params": {"name": "x", "value": 1}},
], ticks=10, variables=[{"name": "x", "type": "number", "initial": 0}])
# the top-level program loops (one firing per pass) -- every firing must
# carry the declared id and nothing else
check("mark_rung feeds _note_rung_fired with its id",
      len(_rt.fired) >= 1 and set(_rt.fired) == {"R2"}, _rt.fired)
check("mark_rung emits nothing (no event/input/sleep actions)",
      not [a for a in ACTIONS if a and a[0] in ("event", "kd", "ku", "md", "mu", "tap", "sleep")],
      ACTIONS[:6])
check("flow continues after mark_rung", po.State.running, ACTIONS[-3:])

fresh_state()
po.State.recovery = None  # no recovery runtime: still safe, still a no-op
_r19b, _ = runner_v4_for([
    {"id": "a", "type": "mark_rung", "params": {"id": "R1"}},
], ticks=5)
check("mark_rung without a recovery runtime is safe", po.State.running)

fresh_state()
_r19c, _ = runner_v4_for([
    {"id": "a", "type": "mark_rung", "params": {"id": "R9"}},
], ticks=5)
check("an unknown rung id safe-stops with the reason named",
      any(a[0] == "safe_stop" and "unknown recovery rung" in a[1] for a in ACTIONS),
      ACTIONS[:4])
check("mark_rung sits in the v4 handler table and the conc table (instant, no effects)",
      "mark_rung" in po._SCRIPT_HANDLERS_V4
      and po._conc_norm("mark_rung") == {
          "kb": "none", "mouse": "none", "screen": False, "stateWrites": [],
          "stats": False, "cancellable": False, "blocking": False,
          "branch": "instant"})
po.State.recovery = None

# =============================================================================
print("[20] flow variable triggers see the live Studio program's variables")
# =============================================================================
# Session ten (adapter attachments): a FLOWS_JSON variable trigger may name
# variables it declares; at runtime the stub borrows the ACTIVE script
# runner's vars, so compiler-generated active-window variables really gate
# attachment flows. No live runner -> false, exactly as before.
import importlib as _importlib19
_flows_mod = _importlib19.import_module("prospector_engine.flows")

_flow_doc = json.dumps({"_flows": 1, "flows": [{
    "id": "att_probe_bg", "name": "att window",
    "trigger": {"type": "variable", "expr": {"var": "aw_win"}},
    "priority": 0, "input": "none",
    "body": [{"id": "f1", "type": "log", "params": {"message": "bg"}}],
    "version": 2,
    "variables": [{"name": "aw_win", "type": "bool", "initial": False}],
    "max_ms": 10000,
}]})
_flows, _ferr = _flows_mod.parse_flows(po, _flow_doc)
check("a variable trigger naming a DECLARED variable parses", _ferr is None, _ferr)

_flow_bad = json.dumps({"_flows": 1, "flows": [{
    "id": "att_bad", "name": "bad",
    "trigger": {"type": "variable", "expr": {"var": "never_declared"}},
    "priority": 0, "input": "none",
    "body": [{"id": "f1", "type": "log", "params": {"message": "bg"}}],
    "version": 2, "max_ms": 10000,
}]})
_flows_b, _ferr_b = _flows_mod.parse_flows(po, _flow_bad)
check("an undeclared variable in the trigger is still refused at parse",
      _ferr_b is not None and "variable expr" in _ferr_b, _ferr_b)

fresh_state()
_mgr = _flows_mod.FlowManager(po, _flows)


class _FakeRunner19(object):
    vars = {"aw_win": False}
    var_decls = {"aw_win": "bool"}


po.State.script_runner = _FakeRunner19()
_det20 = FakeDet()
_run20 = _mgr.runs[0]
_due_a = _mgr._due(_run20, _det20, 0)      # window closed -> not due
_FakeRunner19.vars["aw_win"] = True
_due_b = _mgr._due(_run20, _det20, 0)      # rising edge -> due
_due_c = _mgr._due(_run20, _det20, 0)      # still true -> edge consumed
check("the variable trigger reads the LIVE runner's vars (rising edge)",
      _due_a is False and _due_b is True and _due_c is False,
      (_due_a, _due_b, _due_c))
po.State.script_runner = None
_run20.expr_prev = False
_due_d = _mgr._due(_run20, _det20, 0)      # no live runner -> false as before
check("no live runner evaluates false exactly as before", _due_d is False)

# =============================================================================
print()
if FAILS:
    print("STUDIO TESTS: %d FAILURES" % len(FAILS))
    sys.exit(1)
print("STUDIO TESTS: ALL PASS")
