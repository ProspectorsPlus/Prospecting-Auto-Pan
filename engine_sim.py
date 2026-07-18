#!/usr/bin/env python3
"""Prospector Engine sim world (Phase 04, checkpoint C0).

Formalizes the seams studio_conformance.py already monkeypatches into one
reusable, scenario-driven world: virtual clock, recorded input injection,
scripted screen, silenced webhook/beep, scheduled operator actions (the
hotkey flips a human would make). The REAL engine module runs its REAL
main() loop against this world -- nothing engine-side is reimplemented
except the 3-line flag flips the pynput listener would perform, which the
harness mirrors verbatim (the listener closure is not importable).

Used two ways:
  * in-process by engine_characterization.py (legacy golden transcripts)
  * later by the engine's own --sim flag (contract tests spawn the real
    process; the world installs itself before main() runs)

Scenario files live in engine_scenarios/*.json:
  {
    "name":     "stuck-ladder",
    "window":   [0, 0, 1440, 900],          # simulated Roblox rect
    "config":   {"AUTOSTOP_ENABLED": true}, # written to a temp config file
    "schedule": [[0, "toggle"], [400000, "quit"]],   # [virtual_ms, action]
    "cues":     {"PAN": [[1000, 2500]]},    # cue -> [start_ms, end_ms) windows
    "capacity": [[0, 0.0], [5000, 1.0]],    # keyframes, linear interpolation
    "duration_ms": 600000                   # hard safety stop for the loop
  }
Actions: toggle | pause | soft | quit  (exactly the listener's vocabulary).
"""
import io
import json
import os
import sys
import tempfile

SIM_DIR = os.path.dirname(os.path.abspath(__file__))
SCENARIO_DIR = os.path.join(SIM_DIR, "engine_scenarios")


def load_scenario(name_or_path):
    p = name_or_path
    if not os.path.exists(p):
        p = os.path.join(SCENARIO_DIR, name_or_path + ".json")
    with open(p, encoding="utf-8") as f:
        scen = json.load(f)
    scen.setdefault("window", [0, 0, 1440, 900])
    scen.setdefault("config", {})
    scen.setdefault("schedule", [])
    scen.setdefault("cues", {})
    scen.setdefault("capacity", [[0, 0.0]])
    scen.setdefault("duration_ms", 600000)
    return scen


class Clock(object):
    """Virtual monotonic clock in milliseconds. Advancing it fires any
    scheduled world actions whose time was crossed (in order)."""

    def __init__(self):
        self.ms = 0.0
        self._sched = []      # [(ms, callable)] sorted
        self._safety = None   # hard ceiling: raise if the loop runs away

    def schedule(self, ms, fn):
        self._sched.append((float(ms), fn))
        self._sched.sort(key=lambda x: x[0])

    def adv(self, ms):
        target = self.ms + max(0.0, float(ms))
        while self._sched and self._sched[0][0] <= target:
            t, fn = self._sched.pop(0)
            self.ms = max(self.ms, t)
            fn()
        self.ms = target
        if self._safety is not None and self.ms > self._safety:
            raise RuntimeError("sim safety ceiling exceeded "
                               "(%d ms)" % int(self.ms))


class TimeShim(object):
    """Replaces the engine's `time` module. perf_counter derives from the
    virtual clock; sleep advances it; time() is pinned (same constant the
    conformance suite uses)."""

    def __init__(self, clock, real_pace=False):
        self._c = clock
        self._real_pace = real_pace

    def perf_counter(self):
        return self._c.ms / 1000.0

    def time(self):
        return 1752600000.0

    def sleep(self, s):
        self._c.adv(float(s) * 1000.0)
        if self._real_pace:
            import time as _rt
            _rt.sleep(min(float(s), 0.05))


class InputLog(object):
    """Records every injected input with its virtual time and tracks the
    held set (keys + mouse), so tests can assert full release."""

    def __init__(self, clock):
        self._c = clock
        self.events = []
        self.held_keys = set()
        self.mouse_down_now = False

    def rec(self, kind, arg=None):
        self.events.append((int(round(self._c.ms)), kind, arg))

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


def _cue_on(scen, cue, ms):
    for a, b in scen["cues"].get(cue, []):
        if a <= ms < b:
            return True
    return False


def _cap_at(scen, ms):
    kf = scen["capacity"]
    if not kf:
        return 0.0
    prev_t, prev_v = kf[0]
    if ms <= prev_t:
        return float(prev_v)
    for t, v in kf[1:]:
        if ms < t:
            span = float(t - prev_t) or 1.0
            return prev_v + (v - prev_v) * ((ms - prev_t) / span)
        prev_t, prev_v = t, v
    return float(kf[-1][1])


class FakeSct(object):
    """Stand-in for one mss screen-capture handle: paints the pixels the
    real Detector samples, from the scenario state at the current virtual
    time. Everything not scripted is dark (16,16,16)."""

    def __init__(self, po, scen, clock):
        self._po = po
        self._scen = scen
        self._c = clock

    # mss context-manager protocol
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def monitors(self):
        x, y, w, h = self._scen["window"]
        full = {"left": 0, "top": 0,
                "width": max(x + w, 1440), "height": max(y + h, 900)}
        return [full, dict(full)]

    def _rgb_for(self, px, py):
        po, scen, ms = self._po, self._scen, self._c.ms
        white = (255, 255, 255)
        for cue, key in (("DEPOSIT", "DEPOSIT_PIX"), ("PAN", "PAN_PIX"),
                         ("SHAKE", "SHAKE_PIX")):
            cx, cy = po.__dict__.get(key, (0, 0))
            if abs(px - cx) <= 8 and abs(py - cy) <= 8 and _cue_on(scen, cue, ms):
                return white
        dx, dy = po.__dict__.get("DIG_TRIGGER_PIXEL", (0, 0))
        if abs(px - dx) <= 8 and abs(py - dy) <= 8 and _cue_on(scen, "DIG", ms):
            return white
        # capacity strip: yellow from the left end for cap_fill() fraction
        fx, fy = po.__dict__.get("CAP_FULL_PIXEL", (0, 0))
        bw = int(po.__dict__.get("CAP_BAR_WIDTH", 440))
        if abs(py - fy) <= 10 and fx - bw <= px <= fx:
            frac = _cap_at(scen, ms)
            if px <= (fx - bw) + bw * frac:
                return (255, 200, 60)
        return (16, 16, 16)

    def grab(self, region):
        import numpy as np
        left = int(region["left"]); top = int(region["top"])
        w = max(1, int(region["width"])); h = max(1, int(region["height"]))
        arr = np.zeros((h, w, 4), dtype=np.uint8)
        arr[:, :, 3] = 255
        base = self._rgb_for(left + w // 2, top + h // 2)
        arr[:, :, 0] = base[2]   # B
        arr[:, :, 1] = base[1]   # G
        arr[:, :, 2] = base[0]   # R
        # capacity strip needs per-column detail inside one grab
        po = self._po
        fx, fy = po.__dict__.get("CAP_FULL_PIXEL", (0, 0))
        if top <= fy < top + h:
            for col in range(w):
                r, g, b = self._rgb_for(left + col, fy)
                arr[:, col, 0] = b; arr[:, col, 1] = g; arr[:, col, 2] = r
        return arr


class FakeMSS(object):
    """Callable standing in for mss.mss / mss.MSS."""

    _current = None

    def __call__(self):
        return FakeMSS._current

    def __init__(self, sct=None):
        if sct is not None:
            FakeMSS._current = sct


class _DummyListener(object):
    def start(self):
        pass

    def stop(self):
        pass


class _PopenRecorder(object):
    """Replaces the engine's `subprocess` module: records instead of
    spawning (safe_stop's nested _beep uses subprocess.Popen(afplay))."""

    def __init__(self):
        self.calls = []

    def Popen(self, argv, **kw):
        self.calls.append(list(argv))

        class _P(object):
            def wait(self_inner, *a, **k):
                return 0
        return _P()


class World(object):
    """One installed sim world over a loaded engine module."""

    def __init__(self, po, scen):
        self.po = po
        self.scen = scen
        self.clock = Clock()
        self.clock._safety = float(scen["duration_ms"]) * 4 + 7200000
        self.inputs = InputLog(self.clock)
        self.webhooks = []
        self.beeps = _PopenRecorder()
        self._tmpdir = tempfile.mkdtemp(prefix="ppe-sim-")
        self._install()
        self._schedule_ops()

    # -- operator actions: exactly what the hotkey listener does, through
    # the same request_* entries the listener uses since checkpoint C4
    # (legacy mode: verbatim today's listener bodies; ipc mode: the
    # server-serialized CAS transitions) ----------------------------------
    def _op_toggle(self):
        self.po.request_toggle()

    def _op_pause(self):
        self.po.request_pause_toggle()

    def _op_soft(self):
        self.po.request_soft()

    def _op_quit(self):
        self.po.request_quit()

    def _op_rreset(self):
        po = self.po
        if po.State.relics_ref is not None:
            po.State.relics_ref.reset()
        po.EMIT.relic_reset(False)

    def _op_rrone(self, idx):
        po = self.po
        if po.State.relics_ref is not None:
            po.State.relics_ref.reset_one(idx)
            po.EMIT.relic_one(idx, "hotkey")

    def _schedule_ops(self):
        ops = {"toggle": self._op_toggle, "pause": self._op_pause,
               "soft": self._op_soft, "quit": self._op_quit,
               "rreset": self._op_rreset}
        for ms, action in self.scen["schedule"]:
            action = str(action)
            if action.startswith("rrone:"):
                idx = int(action.split(":", 1)[1])
                self.clock.schedule(ms, lambda i=idx: self._op_rrone(i))
                continue
            self.clock.schedule(ms, ops[action])
        # backstop: if the scenario forgot to quit, end the loop at the
        # duration ceiling so main() always returns
        self.clock.schedule(float(self.scen["duration_ms"]), self._op_quit)

    # -- install ----------------------------------------------------------
    def _install(self):
        po, scen, clock, inputs = self.po, self.scen, self.clock, self.inputs
        po.time = TimeShim(clock, real_pace=bool(scen.get("interactive")))
        po.sleep_ms = lambda ms: clock.adv(ms)

        def _sleep_until(target):
            now = po.time.perf_counter()
            if target > now:
                clock.adv((target - now) * 1000.0)
        po.sleep_until = _sleep_until

        # wait_until is a deliberate busy-wait in the real engine (screen
        # grabs pace it on hardware); under a virtual clock that never
        # advances, so the world supplies the same virtual poller
        # studio_conformance.py already established for this seam (25 ms
        # polls, matching the Studio VM's waitUntil model).
        POLL_MS = 25.0

        def _wait_until(cond, max_ms, confirm=1, min_ms=0):
            start = po.time.perf_counter()
            deadline = start + max_ms / 1000.0
            gate = start + min_ms / 1000.0
            hits = 0
            while po.State.running and po.time.perf_counter() < deadline:
                if po.time.perf_counter() >= gate:
                    if cond():
                        hits += 1
                        if hits >= confirm:
                            return True
                    else:
                        hits = 0
                clock.adv(POLL_MS)
            return False
        po.wait_until = _wait_until

        po.key_down = inputs.key_down
        po.key_up = inputs.key_up
        po.mouse_down = inputs.mouse_down
        po.mouse_up = inputs.mouse_up
        po.move_cursor = inputs.move
        po.scroll_down = inputs.scroll
        po._post = lambda ev: None
        po._mouse_event = lambda kind: None
        po._cursor_point = lambda: (0.0, 0.0)

        w = tuple(scen["window"])
        po.find_roblox_rect = lambda: w
        po.find_window_origin = lambda: (w[0], w[1])
        po.get_scale = lambda sct=None: 1.0

        sct = FakeSct(po, scen, clock)
        po._MSS = FakeMSS(sct)
        po.make_listener = lambda: _DummyListener()
        po.subprocess = self.beeps

        def _post_webhook(event, message, stats=None, shot=False):
            self.webhooks.append((int(round(clock.ms)), event, message))
        po.post_webhook = _post_webhook
        po._grab_screenshot_b64 = lambda *a, **k: ""

        # config: the scenario's own file, never the user's real one. When
        # the engine was spawned with --home (its CONFIG_FILE already points
        # away from the repo default), write the scenario config THERE so
        # the ipc settings surface and the sim world agree on one file.
        # [C6 hardening] The engine's default home is the USER'S real config
        # directory (post-C6 the launcher-shim dir, exposed as _HOME_DIR --
        # NOT the package dir of __file__). The sim may write a config ONLY
        # to a spawner-provided --home (argv-parsed _ENGINE_HOME non-empty
        # AND CONFIG_FILE moved off the default); anything else goes to the
        # sim's own tmpdir. Never write the user's real file.
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


def load_engine(alias):
    """Load a fresh copy of the real engine module under a unique alias
    (module globals mutate at run time; goldens need a clean module)."""
    import importlib.util
    path = os.path.join(SIM_DIR, "prospector_engine", "engine.py")
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


def normalize_transcript(text):
    """Legacy transcripts are fully deterministic under the virtual clock
    (log() stamps perf_counter deltas, not wall time), so normalization is
    the identity. Kept as the single hook should a future scenario need
    one -- widen it consciously, never silently."""
    return text


def run_legacy(scenario_name, alias=None):
    """Run one scenario through the REAL engine main() in legacy mode with
    stdout captured. Returns (normalized_transcript, world)."""
    scen = load_scenario(scenario_name)
    po = load_engine(alias or ("pold_sim_%s" % scen["name"].replace("-", "_")))
    world = World(po, scen)
    buf = io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(buf):
        po.main()
    return normalize_transcript(buf.getvalue()), world
