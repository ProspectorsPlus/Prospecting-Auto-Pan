"""Real-time pacing world: the wall-clock analog of engine_sim.World.

engine_sim answers "is the BEHAVIOR identical?" on a dust-free virtual
clock; this module answers "is the REAL PACING identical?" on the wall
clock. Same scenario fixtures, same real engine main(), same scripted
screen — but time is genuine: sleeps really sleep, waits really wait
(the classic busy-waits really spin, paced by a modeled per-grab screen
cost), and every injected input lands in the log with a monotonic
FLOAT-millisecond timestamp plus the op that posted it. A differential
run (native Classic vs a detached program, same entry config, same
scenario) turns "the Studio import feels slower" into per-seam numbers:
which seam, how many milliseconds, which layer.

This is the Level-2 harness of the session-ten pacing-parity work: no
Roblox, no real input — but real time, the real engine main(), and the
real dispatch path down to the (recorded) backend seam.

Dev harness only: not shipped, not in the zip.
"""

import io
import json
import os
import sys
import tempfile
import threading
import time

import engine_sim  # FakeSct/FakeMSS, load_engine, load_scenario, _Popen...


# ---- real clock -------------------------------------------------------------

class RealClock(object):
    """Monotonic wall clock in float ms since epoch(), matching the .ms
    interface FakeSct reads. epoch() re-zeroes it at the run-start commit
    so scenario cue/capacity instants are relative to the run, exactly as
    they are under the sim (virtual 0 == toggle == start commit)."""

    def __init__(self):
        self.t0 = time.perf_counter()

    def epoch(self):
        self.t0 = time.perf_counter()

    @property
    def ms(self):
        return (time.perf_counter() - self.t0) * 1000.0


# ---- input log with op attribution ------------------------------------------

class PacingLog(object):
    """Every injected input with a float-ms wall timestamp and the op that
    posted it (the innermost wrapped handler / classic verb), plus the
    held set so tests can still assert full release."""

    def __init__(self, clock, world):
        self._c = clock
        self._w = world
        self.events = []          # (ms, kind, arg, op_tag)
        self.held_keys = set()
        self.mouse_down_now = False

    def rec(self, kind, arg=None):
        tag = self._w.op_stack[-1] if self._w.op_stack else ""
        self.events.append((self._c.ms, kind, arg, tag))

    def key_down(self, code):
        self.held_keys.add(code)
        self.rec("key_down", code)

    def key_up(self, code):
        self.held_keys.discard(code)
        self.rec("key_up", code)

    def mouse_down(self):
        self.mouse_down_now = True
        self.rec("mouse_down")

    def mouse_up(self):
        self.mouse_down_now = False
        self.rec("mouse_up")

    def move(self, x, y):
        self.rec("mouse_move", (int(x), int(y)))

    def scroll(self, steps):
        self.rec("scroll", steps)

    def all_released(self):
        return not self.held_keys and not self.mouse_down_now


class RealSct(engine_sim.FakeSct):
    """FakeSct plus a modeled per-grab cost: real mss small-region grabs
    take low-single-digit milliseconds on this hardware, and that cost is
    exactly what paces the engine's deliberate busy-waits. Without it a
    hot wait would spin at memory speed — unrepresentative of both the
    native and the detached runner."""

    def __init__(self, po, scen, clock, grab_cost_s):
        engine_sim.FakeSct.__init__(self, po, scen, clock)
        self._grab_cost_s = grab_cost_s

    def grab(self, region):
        if self._grab_cost_s > 0:
            time.sleep(self._grab_cost_s)
        return engine_sim.FakeSct.grab(self, region)


# ---- the world --------------------------------------------------------------

class RealWorld(object):
    """One installed real-time world over a loaded engine module. Patches
    ONLY the input backend, the screen, and the environment seams — time,
    sleeps and waits stay genuine."""

    def __init__(self, po, scen, grab_cost_ms=1.2):
        self.po = po
        self.scen = scen
        self.clock = RealClock()
        self.op_stack = []        # innermost active wrapped op tag
        self.ops = []             # (start_ms, end_ms, op_name, block_id)
        self.inputs = PacingLog(self.clock, self)
        self.webhooks = []
        self.beeps = engine_sim._PopenRecorder()
        self._tmpdir = tempfile.mkdtemp(prefix="ppe-pace-")
        self._grab_cost_s = max(0.0, float(grab_cost_ms)) / 1000.0
        self._sched_thread = None
        self._install()
        self._wrap_ops()

    # -- install ----------------------------------------------------------
    def _install(self):
        po, scen, clock = self.po, self.scen, self.clock
        inputs = self.inputs

        w = tuple(scen["window"])
        po.find_roblox_rect = lambda: w
        po.find_window_origin = lambda: (w[0], w[1])
        po.get_scale = lambda sct=None: 1.0
        po.find_roblox_window = lambda: {
            "found": True, "x": w[0], "y": w[1], "w": w[2], "h": w[3],
            "title": "Roblox"}
        po._cursor_point = lambda: (0.0, 0.0)

        # input backend: record AND mirror the platform registries, so the
        # engine's own release/anti-drift logic behaves exactly as it does
        # over the real platform layer (platform key_down registers before
        # posting; the pacing world does the same).
        def _key_down(code):
            po._HELD_KEYS.add(code)
            inputs.key_down(code)

        def _key_up(code):
            po._HELD_KEYS.discard(code)
            inputs.key_up(code)

        def _mouse_down():
            po._MOUSE_DOWN = True
            inputs.mouse_down()

        def _mouse_up():
            po._MOUSE_DOWN = False
            inputs.mouse_up()

        po.key_down = _key_down
        po.key_up = _key_up
        po.mouse_down = _mouse_down
        po.mouse_up = _mouse_up
        po.move_cursor = inputs.move
        po.scroll_down = inputs.scroll
        po._mouse_event = lambda kind: None

        sct = RealSct(po, scen, clock, self._grab_cost_s)
        po._MSS = engine_sim.FakeMSS(sct)
        po.make_listener = lambda: engine_sim._DummyListener()
        po.subprocess = self.beeps

        def _post_webhook(event, message, stats=None, shot=False):
            self.webhooks.append((clock.ms, event, message))
        po.post_webhook = _post_webhook
        po._grab_screenshot_b64 = lambda *a, **k: ""

        # config: the scenario's own file in the world tmpdir, never the
        # user's real one (same guard rule as engine_sim.World).
        cfg_default = os.path.join(
            getattr(po, "_HOME_DIR",
                    os.path.dirname(os.path.abspath(po.__file__))),
            "prospecting_config.json")
        if po.CONFIG_FILE != cfg_default and getattr(po, "_ENGINE_HOME", ""):
            cfg_path = po.CONFIG_FILE
        else:
            cfg_path = os.path.join(self._tmpdir, "prospecting_config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(scen["config"], f)
        po.CONFIG_FILE = cfg_path

    # -- op attribution wrappers ------------------------------------------
    _CLASSIC_VERBS = (
        "_shards_dig", "do_dig", "go_water", "do_shake", "go_land",
        "empty_sequence", "run_empty", "timed_empty", "fill_to_full",
        "_geode_dig", "return_and_dig", "momentum_and_shake",
    )

    def _wrap_ops(self):
        po, world = self.po, self

        def wrap_handler(name, fn):
            def w(runner, det, p, kids):
                b = runner._cur or {}
                tag = "%s:%s" % (name, b.get("id", ""))
                world.op_stack.append(tag)
                t0 = world.clock.ms
                try:
                    return fn(runner, det, p, kids)
                finally:
                    world.ops.append((t0, world.clock.ms, name,
                                      str(b.get("id", ""))))
                    world.op_stack.pop()
            return w

        for tabname in ("_SCRIPT_HANDLERS", "_SCRIPT_HANDLERS_V2",
                        "_SCRIPT_HANDLERS_V3", "_SCRIPT_HANDLERS_V4"):
            tab = getattr(po, tabname, None)
            if isinstance(tab, dict):
                for k, fn in list(tab.items()):
                    tab[k] = wrap_handler(k, fn)

        def wrap_verb(name, fn):
            def w(*a, **kw):
                world.op_stack.append(name)
                t0 = world.clock.ms
                try:
                    return fn(*a, **kw)
                finally:
                    world.ops.append((t0, world.clock.ms, name, ""))
                    world.op_stack.pop()
            return w

        for name in self._CLASSIC_VERBS:
            fn = getattr(po, name, None)
            if callable(fn):
                setattr(po, name, wrap_verb(name, fn))

    # -- schedule ---------------------------------------------------------
    def start_schedule(self):
        """Fire scenario schedule rows at REAL instants, re-zeroing the
        world clock at the run-start commit so cue/capacity/quit line up
        with the run the way virtual 0 does under the sim."""
        po, scen, clock = self.po, self.scen, self.clock
        ops = {"toggle": po.request_toggle, "pause": po.request_pause_toggle,
               "soft": po.request_soft, "quit": po.request_quit}
        rows = sorted([(float(t), str(a)) for t, a in scen["schedule"]],
                      key=lambda r: r[0])
        rows.append((float(scen["duration_ms"]), "quit"))  # backstop

        def run():
            # 1) anything scheduled at 0 fires now (the start toggle)
            pending = list(rows)
            while pending and pending[0][0] <= 0.0:
                _, action = pending.pop(0)
                if action in ops:
                    ops[action]()
            # 2) wait for the run to actually commit, then re-zero
            deadline = time.perf_counter() + 30.0
            while (not po.State.running
                   and time.perf_counter() < deadline
                   and po.State.alive):
                time.sleep(0.001)
            clock.epoch()
            # 3) the rest at real instants relative to the commit
            for t, action in pending:
                delay = (t - clock.ms) / 1000.0
                if delay > 0:
                    time.sleep(delay)
                if not po.State.alive:
                    return
                if action in ops:
                    ops[action]()

        self._sched_thread = threading.Thread(target=run, daemon=True)
        self._sched_thread.start()


def run_real(scenario, alias=None, program=None, base_config=None,
             grab_cost_ms=1.2):
    """Run one scenario through the REAL engine main() in real time.

    scenario     name (engine_scenarios/) or path (engine_sim rules)
    program      a compiled PPScript document -> run the detached
                 SCRIPT_MODE path instead of the native Classic cycle
    base_config  entry-derived config; the scenario's own keys win on top
                 (same merge rule as scripts/classic_trace_probe.py)

    Returns (transcript, world)."""
    scen = engine_sim.load_scenario(scenario)
    if base_config:
        merged = dict(base_config)
        merged.update(scen["config"])
        scen["config"] = merged
    if program is not None:
        script = program.get("script") if isinstance(program, dict) \
            and "_ppscript" in program else program
        cfg = dict(scen["config"])
        cfg["SCRIPT_MODE"] = True
        cfg["SCRIPT_ACTIVE"] = str(script.get("name") or "Detached build")
        cfg["SCRIPT_JSON"] = json.dumps(script)
        scen["config"] = cfg
    else:
        cfg = dict(scen["config"])
        cfg["SCRIPT_MODE"] = False
        scen["config"] = cfg

    po = engine_sim.load_engine(
        alias or ("pace_%s" % scen["name"].replace("-", "_")))
    world = RealWorld(po, scen, grab_cost_ms=grab_cost_ms)
    world.start_schedule()
    buf = io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(buf):
        po.main()
    return buf.getvalue(), world


# ---- analysis ---------------------------------------------------------------

def key_names(po):
    names = {po.KEY_W: "W", po.KEY_A: "A", po.KEY_S: "S",
             po.KEY_D: "D", po.KEY_SHIFT: "Shift", po.KEY_SPACE: "Space"}
    for d, code in getattr(po, "SLOT_KEYCODES", {}).items():
        names.setdefault(code, "slot%s" % d)
    return names


def normalize(world, until_ms=None):
    """The float-ms twin of classic_trace_probe.normalize_trace: drop the
    release_all floor no-ops, name the keys, keep op attribution."""
    names = key_names(world.po)
    held = set()
    mouse_down = False
    out = []
    for t, kind, arg, tag in world.inputs.events:
        if kind == "key_down":
            held.add(arg)
            out.append({"t": t, "ev": "key_down",
                        "key": names.get(arg, "k%s" % arg), "op": tag})
        elif kind == "key_up":
            if arg not in held:
                continue
            held.discard(arg)
            out.append({"t": t, "ev": "key_up",
                        "key": names.get(arg, "k%s" % arg), "op": tag})
        elif kind == "mouse_down":
            mouse_down = True
            out.append({"t": t, "ev": "mouse_down", "op": tag})
        elif kind == "mouse_up":
            if not mouse_down:
                continue
            mouse_down = False
            out.append({"t": t, "ev": "mouse_up", "op": tag})
        elif kind == "mouse_move":
            out.append({"t": t, "ev": "mouse_move",
                        "x": arg[0], "y": arg[1], "op": tag})
    if until_ms is not None:
        out = [e for e in out if e["t"] < until_ms]
    return out


def floor_noops(world):
    """The raw events normalize() drops: the anti-drift floor. Their COUNT
    is the physical no-op input the game would have received."""
    held = set()
    mouse_down = False
    drops = []
    for t, kind, arg, tag in world.inputs.events:
        if kind == "key_down":
            held.add(arg)
        elif kind == "key_up":
            if arg not in held:
                drops.append((t, kind, arg, tag))
            held.discard(arg)
        elif kind == "mouse_down":
            mouse_down = True
        elif kind == "mouse_up":
            if not mouse_down:
                drops.append((t, kind, arg, tag))
            mouse_down = False
    return drops


def identity(events):
    """The behavior signature of a normalized trace (what happened, in
    order) with timing removed — the cross-mode pairing key."""
    sig = []
    for e in events:
        sig.append((e["ev"], e.get("key", ""), e.get("x"), e.get("y")))
    return sig


def gap_deltas(classic_events, detached_events):
    """Pair the two normalized traces positionally (identical behavior
    signatures required) and compare consecutive-event gaps. Returns
    (deltas_ms, diverged_at): deltas_ms[i] = detached_gap[i] -
    classic_gap[i] for the gap ENTERING event i+1; diverged_at is the
    first index where the behavior signatures differ (None if never)."""
    ca, da = identity(classic_events), identity(detached_events)
    n = min(len(ca), len(da))
    diverged_at = None
    for i in range(n):
        if ca[i] != da[i]:
            diverged_at = i
            break
    upto = diverged_at if diverged_at is not None else n
    deltas = []
    for i in range(1, upto):
        cg = classic_events[i]["t"] - classic_events[i - 1]["t"]
        dg = detached_events[i]["t"] - detached_events[i - 1]["t"]
        deltas.append(dg - cg)
    if diverged_at is None and len(ca) != len(da):
        diverged_at = n
    return deltas, diverged_at


def percentile(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def summarize_deltas(deltas):
    if not deltas:
        return {"n": 0, "median": 0.0, "p95": 0.0, "max": 0.0,
                "max_at": None}
    s = sorted(abs(d) for d in deltas)
    mx = max(range(len(deltas)), key=lambda i: abs(deltas[i]))
    return {"n": len(deltas),
            "median": percentile(s, 0.5),
            "p95": percentile(s, 0.95),
            "max": s[-1],
            "max_at": mx}


def boundary_gaps(world):
    """Detached-runner boundary overhead: wall time between one leaf op
    ending and the next one starting (everything the tick loop does
    between two blocks: polls, emits, releases, dispatch). Container ops
    nest around leaves, so only compare non-overlapping spans."""
    spans = sorted(world.ops, key=lambda o: o[0])
    leaves = []
    for s in spans:
        # drop container spans (a span that fully contains another)
        contains = any(o is not s and s[0] <= o[0] and o[1] <= s[1]
                       for o in spans)
        if not contains:
            leaves.append(s)
    gaps = []
    for a, b in zip(leaves, leaves[1:]):
        if b[0] >= a[1]:
            gaps.append({"ms": b[0] - a[1], "after": a[2], "before": b[2],
                         "after_id": a[3], "before_id": b[3],
                         "at": a[1]})
    return gaps
