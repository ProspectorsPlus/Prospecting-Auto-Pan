"""Headless run harness — wires the production Brain/Actions/FilterBank to
the simulator and reproduces the runtime's safe-stop retry policy."""
from __future__ import annotations

from engine.actions import Actions
from engine.brain import Brain, SafeStop
from engine.config import Config
from engine.telemetry import Telemetry

from .simulator import SimClock, SimGame, SimInput, SimParams, SimPercept


class SimRun:
    def __init__(self, cfg_overrides=None, sim_kw=None, seed=0, quiet=True):
        values = {
            "STAT_CAPACITY": 9448, "STAT_DIG_STRENGTH": 3521,
            "STAT_SHAKE_STRENGTH": 835, "STAT_SHAKE_SPEED": 1768,
            "DIG_SPEED": 1508, "DIG_CLICK_MS": 36,
        }
        values.update(cfg_overrides or {})
        self.cfg = Config(values)
        self.params = SimParams(**(sim_kw or {}))
        self.game = SimGame(self.cfg, self.params, seed=seed)
        self.clock = SimClock(self.game)
        self.lines = []
        sink = (lambda s: self.lines.append(s)) if quiet else None
        self.telem = Telemetry(self.clock, sink=sink)
        self.percept = SimPercept(self.game, self.clock)
        self.running = True
        abort = lambda: not self.running
        self.percept.abort_fn = abort
        self.inputs = SimInput(self.clock, self.game)
        self.actions = Actions(self.cfg, self.percept, self.inputs, self.clock,
                               self.telem, abort_fn=abort)
        self.brain = Brain(self.cfg, self.percept, self.actions, self.telem,
                           self.clock)
        self.hard_stopped = False
        self.hard_reason = ""
        self.safe_pauses = 0

    def run(self, minutes=10.0):
        cfg = self.cfg
        self.game.step(0.2)                       # first frames exist
        self.brain.new_run()
        end = self.game.t + minutes * 60.0
        retries = 0
        guard = 0
        while self.game.t < end and self.running:
            t_before = self.game.t
            try:
                self.brain.tick()
            except SafeStop as e:
                self.inputs.release_all()
                if e.hard or not cfg.SAFE_STOP_RETRY \
                        or retries >= cfg.SAFE_STOP_MAX_RETRIES:
                    self.brain.stats.hard_stops += 1
                    self.hard_stopped = True
                    self.hard_reason = e.reason
                    self.running = False
                    break
                retries += 1
                self.safe_pauses += 1
                self.brain.stats.safe_stops += 1
                self.clock.sleep_ms(cfg.SAFE_STOP_RETRY_SEC * 1000)
                self.brain.reset()
            if self.game.t <= t_before + 1e-9:    # tick consumed no time
                guard += 1
                self.clock.sleep_ms(10)
                if guard > 200000:
                    raise RuntimeError("sim wedged: ticks consume no time")
            else:
                guard = 0
        self.inputs.release_all()
        return self.summary()

    def summary(self):
        s = self.brain.stats.as_dict()
        s.update({
            "sim_edge_misses": self.game.n_edge_misses,
            "sim_glitch_misses": self.game.n_glitch_misses,
            "sim_shake_starts": self.game.n_shake_starts,
            "sim_digs": self.game.n_digs_done,
            "hard_stopped": self.hard_stopped,
            "hard_reason": self.hard_reason,
            "safe_pauses": self.safe_pauses,
            "events": dict(self.telem.counts),
            "held_at_end": sorted(self.inputs._held_keys) +
                           (["mouse"] if self.inputs._mouse_down else []),
        })
        return s
