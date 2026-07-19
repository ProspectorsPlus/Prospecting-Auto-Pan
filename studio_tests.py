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
po.State.running = True
po.State.alive = True
t0 = time.perf_counter()
threading.Timer(0.08, lambda: setattr(po.State, "running", False)).start()
res = _real_script_sleep(5000)
dt = time.perf_counter() - t0
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
                ("SCRIPTS_FILE", "CONFIG_FILE", "DATA_DIR",
                 "STUDIO_LAUNCH", "STUDIO_SCRIPT")}
app.SCRIPTS_FILE = os.path.join(_mdir, "prospecting_scripts.json")
app.CONFIG_FILE = os.path.join(_mdir, "prospecting_config.json")
app.DATA_DIR = _mdir
app.STUDIO_LAUNCH = True
app.STUDIO_SCRIPT = "Pushed Build"
app._STUDIO_LIST_CACHE["key"] = None
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
    for k, v in _saved_attrs.items():
        setattr(app, k, v)
    app._STUDIO_LIST_CACHE["key"] = None
    _sh.rmtree(_mdir, ignore_errors=True)

# =============================================================================
print()
if FAILS:
    print("STUDIO TESTS: %d FAILURES" % len(FAILS))
    sys.exit(1)
print("STUDIO TESTS: ALL PASS")
