"""Perception — continuous background sensing with temporal filtering.

The v2 engine sensed synchronously: every check was its own screen grab, and
during sleeps/holds nothing was sensed at all. Fable senses CONTINUOUSLY on
a thread (one combined grab per frame at ~60 fps) and publishes an immutable
WorldState. Actions subscribe via wait(): event-driven, frame-accurate, and
they never pay capture latency themselves.

Filtering (the flicker killer):
  * Cues pass through asymmetric hysteresis — ON after CUE_ON_FRAMES
    consecutive frames, OFF after CUE_OFF_FRAMES. Single-frame glitches
    (the land<->water cue flicker) simply disappear.
  * The Pan/Shake cues are gated by 'not Deposit' BEFORE filtering (the wide
    'Collect Deposit' text can bleed into their boxes — a proven v2 guard).
  * fill is a median-of-3; fill_rate is measured over RATE_WINDOW_MS, which
    is what lets the shake PROVE it started (fill dropping) and digs PROVE
    they registered (fill rising).

The FilterBank is shared verbatim by the simulator's percept, so tests
exercise the exact production filtering.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass

from . import vision


@dataclass(frozen=True)
class World:
    ts: float            # clock time of the frame
    frame: int           # frame counter
    fill: float          # filtered 0..1 capacity fill
    rate: float          # fill change per second (+digging / -draining)
    full: bool
    empty: bool
    dep: bool            # on land (Collect Deposit cue, debounced)
    pan: bool            # in water (Pan cue, debounced) — the trusted one
    shk: bool            # shake animating (cue known to stick — treat as hint)
    raw: tuple           # (fill, dep_frac, pan_frac, shk_frac) for debug/UI

    @property
    def where(self):
        if self.dep:
            return "LAND"
        if self.pan:
            return "WATER"
        if self.shk:
            return "SHAKING"
        return "LIMBO"

    @property
    def contents(self):
        if self.full:
            return "FULL"
        if self.empty:
            return "EMPTY"
        return "PARTIAL"


class _Hyst:
    """Asymmetric debounce: N frames to turn on, M to turn off."""

    def __init__(self, n_on, n_off):
        self.n_on = max(1, int(n_on))
        self.n_off = max(1, int(n_off))
        self.state = False
        self._run = 0

    def update(self, raw):
        if raw == self.state:
            self._run = 0
            return self.state
        self._run += 1
        if self.state and self._run >= self.n_off:
            self.state, self._run = False, 0
        elif not self.state and self._run >= self.n_on:
            self.state, self._run = True, 0
        return self.state


class FilterBank:
    """RawSignals stream -> World stream. Pure state machine, no I/O."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.dep = _Hyst(cfg.CUE_ON_FRAMES, cfg.CUE_OFF_FRAMES)
        self.pan = _Hyst(cfg.CUE_ON_FRAMES, cfg.CUE_OFF_FRAMES)
        self.shk = _Hyst(cfg.CUE_ON_FRAMES, cfg.CUE_OFF_FRAMES)
        self.fills = deque(maxlen=3)
        self.hist = deque()          # (ts, fill) for the rate window
        self.n = 0

    def update(self, raw, ts):
        cfg = self.cfg
        self.n += 1
        frac = cfg.CUE_WHITE_FRAC
        dep_raw = raw.dep_frac >= frac
        pan_raw = raw.pan_frac >= frac and not dep_raw
        shk_raw = raw.shk_frac >= frac and not dep_raw
        dep = self.dep.update(dep_raw)
        pan = self.pan.update(pan_raw)
        shk = self.shk.update(shk_raw)

        self.fills.append(raw.fill)
        s = sorted(self.fills)
        fill = s[len(s) // 2]

        self.hist.append((ts, fill))
        win = cfg.RATE_WINDOW_MS / 1000.0
        while len(self.hist) > 2 and ts - self.hist[0][0] > win:
            self.hist.popleft()
        t0, f0 = self.hist[0]
        rate = (fill - f0) / (ts - t0) if ts > t0 else 0.0

        return World(ts=ts, frame=self.n, fill=fill, rate=rate,
                     full=fill >= cfg.FULL_FRAC, empty=fill <= cfg.EMPTY_FRAC,
                     dep=dep, pan=pan, shk=shk,
                     raw=(raw.fill, raw.dep_frac, raw.pan_frac, raw.shk_frac))


class Percept:
    """Interface actions/brain talk to. wait() is the workhorse:
    block until pred(world) holds for `stable` consecutive NEW frames, the
    timeout expires, or the abort fires. Returns the final bool."""

    def snapshot(self):
        raise NotImplementedError

    def wait(self, pred, timeout_ms, stable=1):
        raise NotImplementedError

    def stale_s(self):
        """Seconds since the last good frame (sensor watchdog input)."""
        return 0.0


class RealPercept(Percept):
    def __init__(self, cfg, clock, abort_fn=None, on_error=None, hot_fn=None):
        self.cfg = cfg
        self.clock = clock
        self.abort_fn = abort_fn or (lambda: False)
        self.on_error = on_error
        self.hot_fn = hot_fn           # False -> idle: capture at 1/4 rate
        self._cond = threading.Condition()
        self._world = None
        self._alive = False
        self._thread = None
        self._geo = None
        self.errors = 0
        self.fps = 0.0
        self._recalib = threading.Event()
        self._dig_white = False        # perfect-dig trigger (sampled if PERFECT)

    def perfect_trigger(self):
        return self._dig_white

    # -- lifecycle -------------------------------------------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._alive = True
        self._thread = threading.Thread(target=self._run, name="percept",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._alive = False
        with self._cond:
            self._cond.notify_all()

    def reload_geometry(self):
        """Call after calibration changes; the thread rebuilds its region."""
        self._recalib.set()

    # -- capture loop ------------------------------------------------------------
    def _run(self):
        import mss
        filt = FilterBank(self.cfg)
        geo = vision.build_geometry(self.cfg)
        period = 1.0 / max(15, int(self.cfg.SENSE_FPS))
        frames = 0
        fps_t0 = self.clock.now()
        with mss.mss() as sct:
            nxt = self.clock.now()
            while self._alive:
                if self._recalib.is_set():
                    self._recalib.clear()
                    geo = vision.build_geometry(self.cfg)
                    filt = FilterBank(self.cfg)
                try:
                    frame = sct.grab(geo.region())
                    raw = vision.analyze(frame, geo, self.cfg)
                    world = filt.update(raw, self.clock.now())
                    if self.cfg.PERFECT:           # optional 2nd tiny grab
                        import numpy as np
                        x, y = self.cfg.DIG_TRIGGER_PIXEL
                        box = sct.grab({"left": x - 3, "top": y - 3,
                                        "width": 6, "height": 6})
                        b, g, r = np.asarray(box)[:, :, :3].reshape(
                            -1, 3).mean(0)
                        self._dig_white = vision.is_white_mean((r, g, b))
                    with self._cond:
                        self._world = world
                        self._cond.notify_all()
                    self.errors = 0
                except Exception as e:                     # screen lock, etc.
                    self.errors += 1
                    if self.on_error and self.errors in (1, 50):
                        try:
                            self.on_error(e)
                        except Exception:
                            pass
                    self.clock.sleep_ms(100)
                frames += 1
                now = self.clock.now()
                if now - fps_t0 >= 2.0:
                    self.fps = frames / (now - fps_t0)
                    frames, fps_t0 = 0, now
                p = period
                if self.hot_fn is not None and not self.hot_fn():
                    p = period * 4                         # idle: save CPU
                nxt += p
                if nxt < now:                              # fell behind: resync
                    nxt = now + p
                self.clock.sleep_until(nxt)

    # -- API ---------------------------------------------------------------------
    def snapshot(self):
        with self._cond:
            return self._world

    def stale_s(self):
        w = self._world
        if w is None:
            return 999.0
        return max(0.0, self.clock.now() - w.ts)

    def wait(self, pred, timeout_ms, stable=1):
        deadline = self.clock.now() + timeout_ms / 1000.0
        hits = 0
        last_frame = -1
        with self._cond:
            while True:
                if self.abort_fn():
                    return False
                w = self._world
                if w is not None and w.frame != last_frame:
                    last_frame = w.frame
                    if pred(w):
                        hits += 1
                        if hits >= stable:
                            return True
                    else:
                        hits = 0
                rem = deadline - self.clock.now()
                if rem <= 0:
                    return False
                self._cond.wait(timeout=min(rem, 0.05))
