"""Game simulator — a physics-lite model of Prospecting's auto-pan loop,
faithful to the observed glitches, driven by a VIRTUAL clock so the whole
engine (brain + actions + production FilterBank) runs headless and fast.

Modeled, straight from the knowledge base:
  * dig fills the bar in steps of 1.5*DigStrength/Capacity, dig anim time
    190/DigSpeed% s, digs occasionally fail to register (lag)
  * the shake only STARTS if you're deep enough in the water — clicking at
    the edge silently does nothing (the #1 real-world failure), plus a
    random start-failure probability (the true glitch)
  * the Shake cue can STICK on after the drain finishes
  * cues flicker off for single frames; Pan/Deposit have a dead 'limbo' band
  * momentum: you can walk while the shake drains

Geometry (1-D depth axis): y > +0.02 shows 'Collect Deposit' (land),
y < -0.02 shows 'Pan' (water), between is limbo. Digs register at
y >= +0.025; shakes start at y <= start_depth (default -0.035).
"""
from __future__ import annotations

import random

from engine.clock import Clock
from engine.inputs import Input
from engine.percept import FilterBank, Percept
from engine.vision import RawSignals


class SimParams:
    def __init__(self, **kw):
        # player build (defaults = the real user's build)
        self.capacity = 9448.0
        self.dig_strength = 3521.0
        self.dig_speed_pct = 1508.0
        self.shake_strength = 835.0
        self.shake_speed = 1768.0
        self.walk_speed = 0.35            # depth units / s
        # world / glitch model
        self.start_y = 0.05
        self.dep_y = 0.02                 # land cue line
        self.pan_y = -0.02                # water cue line
        self.dig_y = 0.025                # min depth-on-land for digs
        self.shake_start_depth = -0.035   # must be at least this deep
        self.p_shake_fail = 0.03          # random start failure
        self.p_dig_fail = 0.05            # dig doesn't register
        self.p_sticky = 0.30              # shake cue sticks after drain
        self.sticky_s = (0.3, 0.8)
        self.p_flicker = 0.02             # per-frame single-frame cue dropout
        self.click_sustain_s = 0.45       # a click keeps the drain alive this long
        self.fps = 60.0
        for k, v in kw.items():
            if not hasattr(self, k):
                raise ValueError("unknown sim param %s" % k)
            setattr(self, k, v)

    def fill_per_dig(self):
        return 1.5 * self.dig_strength / self.capacity

    def dig_time_s(self):
        return 190.0 / self.dig_speed_pct

    def drain_per_s(self):
        from engine.config import rolls_per_sec
        return rolls_per_sec(self.shake_speed) * self.shake_strength / self.capacity


class SimGame:
    """Owns virtual time. step(dt) advances physics AND produces sensor
    frames through the production FilterBank at params.fps — exactly like
    the real background percept thread, so action-code timing is honest."""

    SUB = 0.004                            # physics sub-step (4 ms)

    def __init__(self, cfg, params, seed=0):
        self.cfg = cfg
        self.p = params
        self.rng = random.Random(seed)
        self.t = 0.0
        self.y = params.start_y
        self.keys = set()
        self.fill = 0.0
        self.dig_until = None
        self.dig_will_register = True
        self.shake_live_until = None       # drain active while t < this
        self.sticky_until = 0.0
        self.filt = FilterBank(cfg)
        self.world = None
        self._next_frame = 0.0
        # counters for assertions
        self.n_digs_done = 0
        self.n_shake_starts = 0
        self.n_edge_misses = 0
        self.n_glitch_misses = 0

    # ------------------------------------------------------------- inputs --
    def set_key(self, key, down):
        k = str(key).lower()
        if down:
            self.keys.add(k)
        else:
            self.keys.discard(k)

    def on_click(self):
        p = self.p
        shaking = self.shake_live_until is not None and self.t < self.shake_live_until
        if shaking:
            # clicks sustain the drain
            self.shake_live_until = max(self.shake_live_until,
                                        self.t + p.click_sustain_s)
            return
        if self.y <= p.pan_y and self.fill > 0.005:
            # attempt to START a shake
            if self.y > p.shake_start_depth:
                self.n_edge_misses += 1          # too shallow: silent nothing
                return
            if self.rng.random() < p.p_shake_fail:
                self.n_glitch_misses += 1        # the real glitch: silent nothing
                return
            self.n_shake_starts += 1
            self.shake_live_until = self.t + p.click_sustain_s
            return
        if (self.y >= p.dig_y and self.fill < 0.999
                and self.dig_until is None):
            self.dig_until = self.t + p.dig_time_s()
            self.dig_will_register = self.rng.random() >= p.p_dig_fail

    # ------------------------------------------------------------- physics --
    def step(self, dt):
        remaining = dt
        while remaining > 1e-9:
            h = min(self.SUB, remaining)
            self._sub_step(h)
            remaining -= h

    def _sub_step(self, h):
        p = self.p
        self.t += h
        v = 0.0
        if "w" in self.keys:
            v += p.walk_speed
        if "s" in self.keys:
            v -= p.walk_speed
        self.y += v * h

        # dig completion
        if self.dig_until is not None and self.t >= self.dig_until:
            self.dig_until = None
            self.n_digs_done += 1
            if self.dig_will_register:
                self.fill = min(1.0, self.fill + p.fill_per_dig())

        # shake drain
        if self.shake_live_until is not None:
            if self.t < self.shake_live_until and self.fill > 0.0:
                self.fill = max(0.0, self.fill - p.drain_per_s() * h)
            if self.fill <= 0.0 or self.t >= self.shake_live_until:
                self.shake_live_until = None
                if self.rng.random() < p.p_sticky:
                    lo, hi = p.sticky_s
                    self.sticky_until = self.t + self.rng.uniform(lo, hi)

        # sensor frames
        while self.t >= self._next_frame:
            self._emit_frame(self._next_frame)
            self._next_frame += 1.0 / p.fps

    def _emit_frame(self, ts):
        p = self.p
        shaking = ((self.shake_live_until is not None
                    and self.t < self.shake_live_until)
                   or self.t < self.sticky_until)
        dep = self.y >= p.dep_y
        pan = self.y <= p.pan_y
        shk = shaking

        def flick(v):
            return False if (v and self.rng.random() < p.p_flicker) else v

        raw = RawSignals(fill=self.fill,
                         dep_frac=0.5 if flick(dep) else 0.0,
                         pan_frac=0.5 if flick(pan) else 0.0,
                         shk_frac=0.5 if flick(shk) else 0.0)
        self.world = self.filt.update(raw, ts)


class SimClock(Clock):
    def __init__(self, game):
        self.game = game

    def now(self):
        return self.game.t

    def sleep_ms(self, ms):
        if ms > 0:
            self.game.step(ms / 1000.0)

    def sleep_until(self, deadline):
        dt = deadline - self.game.t
        if dt > 0:
            self.game.step(dt)


class SimInput(Input):
    def __init__(self, clock, game):
        super().__init__(clock)
        self.game = game

    def _key(self, key, down):
        self.game.set_key(key, down)

    def _mouse(self, down):
        if down:
            self.game.on_click()


class SimPercept(Percept):
    def __init__(self, game, clock):
        self.game = game
        self.clock = clock
        self.abort_fn = lambda: False

    def snapshot(self):
        return self.game.world

    def stale_s(self):
        return 0.0

    def wait(self, pred, timeout_ms, stable=1):
        deadline = self.game.t + timeout_ms / 1000.0
        frame_ms = 1000.0 / self.game.p.fps
        hits = 0
        last_frame = -1
        while True:
            if self.abort_fn():
                return False
            w = self.game.world
            if w is not None and w.frame != last_frame:
                last_frame = w.frame
                if pred(w):
                    hits += 1
                    if hits >= stable:
                        return True
                else:
                    hits = 0
            if self.game.t >= deadline:
                return False
            self.game.step(min(frame_ms / 1000.0,
                               max(1e-4, deadline - self.game.t)))
