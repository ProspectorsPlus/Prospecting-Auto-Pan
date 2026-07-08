"""Actions — verified verbs. Each action confirms its own outcome against
the perception stream and returns a structured result; the brain owns all
escalation decisions.

THE SHAKE (v2). The proven v1 technique is kept intact — hold W for
momentum and rattle rapid clicks (holds do nothing on macOS), stopping only
when the CAPACITY bar reads empty (cues stick). What v2 adds is
START CONFIRMATION: the old engine only noticed a failed shake after a full
SHAKE_BAIL window + walk cycle (~5-30 s lost, 124 occurrences in the last
40 runs). Now, if neither the Shake cue nor a measurable fill DROP shows
within SHAKE_START_CONFIRM_MS, we tap S slightly deeper (failed starts are
almost always 'too shallow — clicked at the water's edge') and keep
clicking — a ~150 ms micro-retry instead of a lost cycle.

LANDING (v2). The old engine guessed the landing from momentum and then
dig-probed, nudging blindly when digs didn't register (397 nudges in the
last 40 runs). Now landing is CUE-CONFIRMED: after the pan empties we keep
W held until the Collect Deposit cue is debounced-on, so the first dig
almost always lands on dirt. The dig-probe survives as the fallback
verification layer (capacity stays ground truth).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ShakeResult:
    started: bool
    emptied: bool
    on_land: bool        # W glide reached the Deposit cue during the shake
    clicks: int
    retries: int         # start micro-retries used
    stalls: int
    dur_ms: float


@dataclass
class ProbeResult:
    found: bool          # a dig registered (we are on land)
    filled: bool         # capacity reached FULL
    digs: int
    nudges: int
    dur_ms: float


class Actions:
    def __init__(self, cfg, percept, inputs, clock, telem, abort_fn):
        self.cfg = cfg
        self.percept = percept
        self.inputs = inputs
        self.clock = clock
        self.telem = telem
        self.abort = abort_fn
        self.water_trim_ms = 0.0        # adaptive extra depth (bounded)

    def _ok(self):
        return not self.abort()

    # ------------------------------------------------------------------ dig --
    def _register_window_ms(self):
        """How long a dig can take to show on the bar. Scales with the user's
        dig-speed stat so slow builds don't read as 'dig didn't register'
        (per-dig time ~ 190/speed% seconds)."""
        per_dig_ms = 190000.0 / max(1.0, float(self.cfg.DIG_SPEED))
        return max(self.cfg.DIG_REGISTER_MS, 1.25 * per_dig_ms)

    def dig_once(self):
        """One dig click. PERFECT mode (release on the green flash) is kept
        as an option but timed holds are the steady default."""
        cfg = self.cfg
        if not cfg.PERFECT:
            self.inputs.mouse_tap(cfg.DIG_CLICK_MS)
            return
        self.inputs.mouse_down()
        deadline = self.clock.now() + max(cfg.DIG_CLICK_MS, 1500) / 1000.0
        trigger = getattr(self.percept, "perfect_trigger", None)
        while self._ok() and self.clock.now() < deadline:
            if trigger and trigger():
                break
            self.clock.sleep_ms(2)
        self.inputs.mouse_up()

    def fill_to_full(self, digs_done=0):
        """Dig until the bar reads FULL, verifying each dig registered
        (fill rose). Returns (filled, digs_used)."""
        cfg = self.cfg
        digs = digs_done
        reg = self._register_window_ms()
        for _ in range(cfg.MAX_DIGS_TO_FILL):
            if not self._ok():
                return False, digs
            w = self.percept.snapshot()
            if w and w.full:
                return True, digs
            before = w.fill if w else 0.0
            self.dig_once()
            digs += 1
            rose = self.percept.wait(
                lambda x, b=before: x.full or x.fill > b + cfg.CAP_RISE_FRAC,
                reg)
            if rose:
                # give the fill animation a beat to reach FULL before re-digging
                if self.percept.wait(lambda x: x.full, cfg.DIG_FILL_MS):
                    return True, digs
            # not full yet (or didn't register) -> loop re-digs, capped
        w = self.percept.snapshot()
        return bool(w and w.full), digs

    # ------------------------------------------------------- land + probe --
    def land(self):
        """Cue-confirmed landing: hold W until the Deposit cue debounces on.
        Returns True if the cue confirmed (False = probe will decide)."""
        cfg = self.cfg
        w = self.percept.snapshot()
        if w and w.dep:
            return True
        self.inputs.key_down("w")
        reached = self.percept.wait(lambda x: x.dep, cfg.LAND_MAX_MS,
                                    stable=cfg.LAND_CONFIRM_FRAMES)
        if reached and cfg.LAND_SETTLE_MS > 0:
            self.clock.sleep_ms(cfg.LAND_SETTLE_MS)   # sit firmly on dirt
        self.inputs.key_up("w")
        return reached

    def find_land_and_fill(self):
        """Dig-probe: a dig only fills on dirt, so the CAPACITY tells the
        truth about where we are. Re-dig in place before nudging (one lag
        spike must not cause a nudge), nudge W between rounds."""
        cfg = self.cfg
        t0 = self.clock.now()
        digs = nudges = 0
        reg = self._register_window_ms()
        if cfg.PRE_DIG_SETTLE_MS > 0:
            self.clock.sleep_ms(cfg.PRE_DIG_SETTLE_MS)
        for rnd in range(cfg.LAND_DIG_TRIES):
            for _ in range(cfg.DIG_INPLACE_TRIES):
                if not self._ok():
                    return ProbeResult(False, False, digs, nudges,
                                       (self.clock.now() - t0) * 1000)
                w = self.percept.snapshot()
                before = w.fill if w else 0.0
                self.dig_once()
                digs += 1
                hit = self.percept.wait(
                    lambda x, b=before: x.full or x.fill > b + cfg.CAP_RISE_FRAC,
                    reg)
                if hit:
                    filled, digs = self.fill_to_full(digs_done=digs)
                    return ProbeResult(True, filled, digs, nudges,
                                       (self.clock.now() - t0) * 1000)
            # no dig registered this round -> probably off land -> nudge forward
            nudges += 1
            self.telem.event("nudge",
                             "no dig registered — nudging forward to find land")
            self.inputs.key_down("w")
            self.clock.sleep_ms(cfg.LAND_PROBE_NUDGE_MS)
            self.inputs.key_up("w")
            if cfg.PROBE_GAP_MS > 0:
                self.clock.sleep_ms(cfg.PROBE_GAP_MS)
        return ProbeResult(False, False, digs, nudges,
                           (self.clock.now() - t0) * 1000)

    # ----------------------------------------------------------------- water --
    def go_water(self, attempt=0):
        """Hold S until the Pan cue confirms (debounced), then keep holding a
        touch longer (configured extra + adaptive trim) so the shake never
        starts right at the edge. The budget grows with consecutive misses."""
        cfg = self.cfg
        budget = cfg.PAN_BACK_MAX_MS * (1 + min(int(attempt), 4))
        self.inputs.key_down("s")
        reached = self.percept.wait(lambda x: x.pan, budget,
                                    stable=cfg.WATER_CONFIRM_FRAMES)
        if reached:
            extra = cfg.WATER_EXTRA_BACK_MS + self.water_trim_ms
            if extra > 0:
                self.clock.sleep_ms(extra)
        self.inputs.key_up("s")
        return reached

    def tap_deeper(self, ms):
        self.inputs.tap_key("s", ms)

    # ----------------------------------------------------------------- shake --
    def _shake_budget_ms(self):
        """Static cap, stretched by the stats prediction when available so a
        slow-drain build is never cut off mid-shake."""
        from . import config as cfgmod
        cfg = self.cfg
        pred = cfgmod.expected_drain_ms(cfg.STAT_CAPACITY,
                                        cfg.STAT_SHAKE_STRENGTH,
                                        cfg.STAT_SHAKE_SPEED)
        if pred > 0:
            return min(8000.0, max(float(cfg.SHAKE_MAX_MS), 2.5 * pred))
        return float(cfg.SHAKE_MAX_MS)

    def shake(self):
        """Momentum shake v2 — see module docstring. Capacity is the truth:
        we stop on debounced EMPTY, never on the (sticky) Shake cue."""
        cfg = self.cfg
        t0 = self.clock.now()
        w = self.percept.snapshot()
        fill0 = w.fill if w else 1.0
        if cfg.SHAKE_START_DELAY_MS > 0:
            self.clock.sleep_ms(cfg.SHAKE_START_DELAY_MS)

        # Momentum W: engaging it BEFORE the shake is confirmed started means
        # a failed first click gets dragged back toward land at full walk
        # speed — the root cause of the v2 'shake never started' loss. With
        # SHAKE_W_AFTER_START (default) we click first, confirm the start
        # (~2 frames), THEN glide. Costs ~35 ms of glide; saves whole cycles.
        w_down = False
        if cfg.SHAKE_MOMENTUM_W and not cfg.SHAKE_W_AFTER_START:
            self.inputs.key_down("w")
            w_down = True

        started = emptied = on_land = False
        clicks = retries = stalls = 0
        empty_hits = 0
        last_frame = -1
        fixed = cfg.SHAKE_CLICKS > 0
        budget = self._shake_budget_ms()
        end = t0 + budget / 1000.0
        confirm_dl = self.clock.now() + cfg.SHAKE_START_CONFIRM_MS / 1000.0
        last_fill = fill0
        last_drop = self.clock.now()

        while self._ok():
            if fixed:
                if clicks >= cfg.SHAKE_CLICKS:
                    break
            elif self.clock.now() >= end:
                break

            self.inputs.mouse_tap(cfg.SHAKE_CLICK_MS)
            clicks += 1

            w = self.percept.snapshot()
            if w is not None and w.frame != last_frame:
                last_frame = w.frame
                if not started and (w.shk or w.fill <= fill0 - cfg.SHAKE_DROP_FRAC):
                    started = True
                    if (cfg.SHAKE_MOMENTUM_W and cfg.SHAKE_W_AFTER_START
                            and not w_down and not w.dep):
                        self.inputs.key_down("w")   # start confirmed -> glide
                        w_down = True
                if w.fill < last_fill - 0.005:
                    last_fill = w.fill
                    last_drop = self.clock.now()
                if not fixed:
                    if w.empty:
                        empty_hits += 1
                        if empty_hits >= cfg.SHAKE_EMPTY_CONFIRM_FRAMES:
                            emptied = True
                            break
                    else:
                        empty_hits = 0
                if w_down and w.dep:
                    # momentum reached land -> stop gliding, keep clicking
                    self.inputs.key_up("w")
                    w_down = False
                    on_land = True

            # START CONFIRMATION: no cue and no drain inside the window
            if not started and not fixed and self.clock.now() > confirm_dl:
                if retries < cfg.SHAKE_START_RETRIES:
                    retries += 1
                    self.telem.event(
                        "shake_start_retry",
                        "no shake start in %dms — tapping deeper and retrying"
                        % cfg.SHAKE_START_CONFIRM_MS)
                    self.tap_deeper(cfg.SHAKE_RETRY_DEEPER_MS)
                    if cfg.ADAPT_WATER_TRIM:
                        self.water_trim_ms = min(150.0, self.water_trim_ms + 12.0)
                    confirm_dl = (self.clock.now()
                                  + cfg.SHAKE_START_CONFIRM_MS / 1000.0)
                    end = max(end, self.clock.now() + 0.8)   # room for the retry
                else:
                    break                        # genuinely not starting -> fail

            # DRAIN STALL: started but fill frozen (and not near empty)
            if started and not fixed:
                if (self.clock.now() - last_drop) * 1000.0 > cfg.SHAKE_STALL_MS:
                    stalls += 1
                    last_drop = self.clock.now()
                    if stalls >= 2:
                        break                    # two stalls = give up cleanly

            self.clock.sleep_ms(cfg.SHAKE_CLICK_GAP_MS)

        if fixed:
            w = self.percept.snapshot()
            emptied = bool(w and w.empty)
        if w_down:
            self.inputs.key_up("w")
        if emptied and on_land and cfg.POST_SHAKE_SETTLE_MS > 0:
            self.clock.sleep_ms(cfg.POST_SHAKE_SETTLE_MS)   # settle onto dirt
        if emptied and cfg.ADAPT_WATER_TRIM and retries == 0:
            self.water_trim_ms = max(0.0, self.water_trim_ms - 3.0)
        return ShakeResult(started=started, emptied=emptied, on_land=on_land,
                           clicks=clicks, retries=retries, stalls=stalls,
                           dur_ms=(self.clock.now() - t0) * 1000.0)

    # ------------------------------------------------------------- recovery --
    def pulse_until(self, key, pred, budget_ms, confirm=1):
        """Jittered corrective taps — can't sail past the target the way a
        blind hold can. Re-checks the world after every tap."""
        cfg = self.cfg
        deadline = self.clock.now() + budget_ms / 1000.0
        hits = 0
        while self._ok() and self.clock.now() < deadline:
            self.inputs.key_down(key)
            self.clock.sleep_ms(cfg.BURST_ON_MS)
            self.inputs.key_up(key)
            if cfg.BURST_OFF_MS > 0:
                self.clock.sleep_ms(cfg.BURST_OFF_MS)
            w = self.percept.snapshot()
            if w is not None and pred(w):
                hits += 1
                if hits >= confirm:
                    return True
            else:
                hits = 0
        return False

    def breakout_clicks(self, budget_ms):
        """Click out a stuck/movement-locking shake until the bar empties."""
        cfg = self.cfg
        deadline = self.clock.now() + budget_ms / 1000.0
        while self._ok() and self.clock.now() < deadline:
            self.inputs.mouse_tap(cfg.SHAKE_CLICK_MS)
            w = self.percept.snapshot()
            if w is not None and w.empty:
                return True
            self.clock.sleep_ms(cfg.SHAKE_CLICK_GAP_MS)
        return False

    def reposition_forward(self, ms):
        self.inputs.key_down("w")
        self.clock.sleep_ms(ms)
        self.inputs.key_up("w")
