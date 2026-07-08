"""Brain — the tick-based supervisor.

Decision core (unchanged from the proven v2 design — capacity is primary,
cues glitch, only the Pan cue is trusted for 'in water'):

    FULL + in water  -> shake
    FULL + elsewhere -> walk back to the water
    NOT full         -> land (cue-confirmed) + dig-probe + fill

Watchdog ladder per tick:
    fast paths first —
      * shake-glitch: N failed shakes -> immediate click-to-empty break-out
      * no-progress:  nothing completed for N s -> click-to-empty break-out
      * sensor stall: perception stopped delivering frames -> safe stop
    then the same-situation ladder —
      * STUCK_TICKS identical reads -> recover (jitter taps), RECOVER_LIMIT×
      * still stuck -> break-out, BREAKOUT_LIMIT×
      * still stuck -> safe stop (pause + auto-retry, then hard stop)

INVARIANT (the v2 '0-threshold bug' that broke every build): every
"N failures / N seconds before X" check is guarded with `> 0` — zero always
means DISABLED, never 'fire instantly'.
"""
from __future__ import annotations


class SafeStop(Exception):
    def __init__(self, reason, hard=False):
        super().__init__(reason)
        self.reason = reason
        self.hard = hard


class SessionStats:
    def __init__(self, clock):
        self.clock = clock
        self.start = clock.now()
        self.cycles = 0
        self.clean_cycles = 0        # cycles with zero retries/recoveries
        self.digs = 0
        self.nudges = 0
        self.shake_retries = 0       # start micro-retries (cheap saves)
        self.shake_fails = 0         # full shake failures (expensive)
        self.recoveries = 0
        self.breakouts = 0
        self.safe_stops = 0
        self.hard_stops = 0
        self.relics_used = 0
        self.stop_reason = ""

    def runtime_s(self):
        return self.clock.now() - self.start

    def as_dict(self):
        rt = self.runtime_s()
        hrs = rt / 3600.0
        return {
            "cycles": self.cycles,
            "clean_cycles": self.clean_cycles,
            "clean_pct": round(100.0 * self.clean_cycles / self.cycles, 1)
                         if self.cycles else 0,
            "digs": self.digs,
            "nudges": self.nudges,
            "shake_retries": self.shake_retries,
            "shake_fails": self.shake_fails,
            "recoveries": self.recoveries,
            "breakouts": self.breakouts,
            "safe_stops": self.safe_stops,
            "hard_stops": self.hard_stops,
            "relics_used": self.relics_used,
            "runtime_s": int(rt),
            "pans_per_hr": round(self.cycles / hrs, 1) if hrs > 0.0008 else 0,
            "stop_reason": self.stop_reason,
        }


class Brain:
    def __init__(self, cfg, percept, actions, telem, clock):
        self.cfg = cfg
        self.percept = percept
        self.act = actions
        self.telem = telem
        self.clock = clock
        self.stats = SessionStats(clock)
        self.reset()

    # ------------------------------------------------------------------ state --
    def reset(self):
        self.last_sig = None
        self.same = 0
        self.recoveries = 0
        self.shake_fails = 0
        self.water_fails = 0
        self.land_fails = 0
        self.breakouts = 0
        self.no_full_rounds = 0
        self.cycle_dirty = False
        self.last_progress = self.clock.now()

    def new_run(self):
        self.stats = SessionStats(self.clock)
        self.reset()

    def _progress(self):
        self.last_progress = self.clock.now()
        self.breakouts = 0
        self.recoveries = 0

    def _cycle_done(self):
        self.stats.cycles += 1
        if not self.cycle_dirty:
            self.stats.clean_cycles += 1
        self.cycle_dirty = False
        self.shake_fails = 0
        self.water_fails = 0
        self._progress()

    # ------------------------------------------------------------------- tick --
    def tick(self):
        cfg = self.cfg
        self.act.inputs.release_all()              # anti key-drift, every tick

        # sensor watchdog: perception must be alive before we move at all
        if self.percept.stale_s() > 1.0:
            raise SafeStop("screen capture stalled — no fresh frames "
                           "(screen locked? permission lost?)")
        w = self.percept.snapshot()
        if w is None:
            raise SafeStop("perception has produced no frames yet")

        sig = (w.where, w.contents)
        if w.full:
            self.no_full_rounds = 0
        if sig == self.last_sig:
            self.same += 1
        else:
            self.same, self.last_sig, self.recoveries = 0, sig, 0

        # ---- fast path: shake glitch (0 = disabled) -------------------------
        if (cfg.SHAKE_GLITCH_LIMIT > 0
                and self.shake_fails >= cfg.SHAKE_GLITCH_LIMIT):
            if cfg.BREAKOUT_ENABLED and self.breakouts < cfg.BREAKOUT_LIMIT:
                self.breakouts += 1
                self.stats.breakouts += 1
                self.telem.event("shake_glitch",
                                 "shake didn't register x%d — click-to-empty "
                                 "recovery" % self.shake_fails)
                if self.act.breakout_clicks(cfg.BREAKOUT_SHAKE_MS):
                    self.breakouts = 0
                else:
                    self.act.reposition_forward(cfg.BREAKOUT_REPOS_MS)
                self.shake_fails = 0
                self.same, self.recoveries, self.last_sig = 0, 0, None
                return
            raise SafeStop("shake not emptying after %d tries"
                           % self.shake_fails)

        # ---- fast path: no progress (0 = disabled) ---------------------------
        if (cfg.BREAKOUT_ENABLED and cfg.NO_PROGRESS_SEC > 0
                and self.clock.now() - self.last_progress > cfg.NO_PROGRESS_SEC):
            self.breakouts += 1
            self.stats.breakouts += 1
            self.telem.event("no_progress",
                             "nothing completed for %ds — click-to-empty "
                             "recovery" % cfg.NO_PROGRESS_SEC)
            if self.breakouts > cfg.BREAKOUT_LIMIT:
                raise SafeStop("no progress — stuck (shake glitch?)")
            self.act.breakout_clicks(cfg.BREAKOUT_SHAKE_MS)
            self.last_progress = self.clock.now()
            self.shake_fails = 0
            self.same, self.recoveries, self.last_sig = 0, 0, None
            return

        # ---- hard failure guard: too many failed shakes (0 = disabled) -------
        if cfg.SHAKE_FAIL_LIMIT > 0 and self.shake_fails >= cfg.SHAKE_FAIL_LIMIT:
            raise SafeStop("%d consecutive shakes failed" % self.shake_fails)

        # ---- normal / recovery / break-out ladder ------------------------------
        if self.same < cfg.STUCK_TICKS:
            self._act(w)
        elif cfg.RECOVER_ENABLED and self.recoveries < cfg.RECOVER_LIMIT:
            self.recoveries += 1
            self.stats.recoveries += 1
            self.cycle_dirty = True
            self._recover(w)
            self.same = 0
        elif cfg.BREAKOUT_ENABLED:
            self.breakouts += 1
            self.stats.breakouts += 1
            self.cycle_dirty = True
            self.telem.event("break_out", "stuck at %s/%s — forcing an escape"
                             % (w.where, w.contents))
            if self.breakouts > cfg.BREAKOUT_LIMIT:
                raise SafeStop("stuck at %s/%s after break-outs"
                               % (w.where, w.contents))
            if w.full and self.act.breakout_clicks(cfg.BREAKOUT_SHAKE_MS):
                self.breakouts = 0
            else:
                self.act.reposition_forward(cfg.BREAKOUT_REPOS_MS)
            self.same, self.recoveries, self.last_sig = 0, 0, None
        else:
            self._act(w)               # recovery disabled -> plain retry
            self.same = 0

    # ------------------------------------------------------------------ verbs --
    def _act(self, w):
        if w.full:
            if w.pan:
                self._do_shake()
            else:
                self._do_go_water()
        else:
            self._do_land_and_fill()

    def _do_shake(self):
        self.telem.phase("shake")
        res = self.act.shake()
        self.stats.shake_retries += res.retries
        if res.retries:
            self.cycle_dirty = True
        if res.emptied:
            self._cycle_done()
            if not res.on_land:
                self.telem.phase("land")
                self.act.land()        # cue-confirmed landing right away
        else:
            self.shake_fails += 1
            self.cycle_dirty = True
            self.telem.event("shake_fail",
                             "shake never started (glitch)" if not res.started
                             else "shake ran but pan didn't empty",
                             clicks=res.clicks, retries=res.retries,
                             stalls=res.stalls)

    def _do_go_water(self):
        self.telem.phase("to_water")
        reached = self.act.go_water(attempt=self.water_fails)
        if reached:
            self.water_fails = 0
        else:
            # benign by design: the next tick re-senses and self-corrects
            # (often we ARE in the water, just past the confirm window)
            self.water_fails += 1

    def _do_land_and_fill(self):
        cfg = self.cfg
        w = self.percept.snapshot()
        if w is not None and not w.dep:
            self.telem.phase("land")
            self.act.land()            # cue-confirmed; probe still verifies
        self.telem.phase("dig")
        res = self.act.find_land_and_fill()
        self.stats.digs += res.digs
        self.stats.nudges += res.nudges
        if res.nudges:
            self.cycle_dirty = True
        if res.found:
            self.land_fails = 0
            self._progress()
            if res.filled:
                self.no_full_rounds = 0
            else:
                self.no_full_rounds += 1
                self._check_no_full()
        else:
            self.land_fails += 1
            self.no_full_rounds += 1
            self.cycle_dirty = True
            self._check_no_full()
            if self.land_fails >= 4:
                raise SafeStop("dig-probe can't find land after shaking")

    def _check_no_full(self):
        cfg = self.cfg
        if cfg.NO_FULL_LIMIT > 0 and self.no_full_rounds >= cfg.NO_FULL_LIMIT:
            raise SafeStop("the pan never reads FULL — the capacity-bar "
                           "calibration looks wrong. Open Calibrate and "
                           "re-set the bar pixels.", hard=True)

    def _recover(self, w):
        cfg = self.cfg
        if w.full and w.pan:
            self.telem.event("recover",
                             "full & in water — pan won't empty, re-shaking")
            self._do_shake()
        elif w.full:
            self.telem.event("recover",
                             "full on land — jitter back to the water")
            self.act.pulse_until("s", lambda x: x.pan, cfg.RECOVER_BACK_MS)
        else:
            self.telem.event("recover",
                             "empty — can't find land, jitter forward")
            self.act.pulse_until("w", lambda x: x.dep, cfg.RECOVER_BACK_MS)
