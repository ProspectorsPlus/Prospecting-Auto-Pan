"""
prospecting_core.py -- pure perception + decision logic (numpy only).

This module has NO macOS / capture / input dependencies, so every part of it
can be unit-tested anywhere (see `selftest`). The macro (prospecting_macro.py)
feeds it raw pixel regions + scalar readings and executes the actuators it
returns.

Design goals:
  * Region/contrast perception, not single pixels.
  * OTHER-rejection: pixels that match neither terrain reference well (particles,
    players, UI, shadows) are DISCARDED, not forced into a class -- so noise
    can't swing the vote.
  * Confidence on every reading; the decision layer refuses to act when unsure.
  * A tick-based state machine that re-decides every frame and can abort
    instantly, with per-state timeouts and a fail-safe SAFE_STOP.
"""

from dataclasses import dataclass, field
import numpy as np

# ---- position / pixel classes ----
DIRT = "DIRT"
LIQUID = "LIQUID"
UNKNOWN = "UNKNOWN"

# ---- decision intents (the macro executes these) ----
DIG = "DIG"          # keep digging on the dirt
EMPTY = "EMPTY"      # pan is full -> run the timed back/shake/empty maneuver
RECOVER = "RECOVER"  # drifted into the liquid -> walk back to the dirt
SAFE = "SAFE"        # uncertain/stuck -> stop and alert


@dataclass
class Config:
    # --- terrain references (RGB); lists allow two-tone ground ---------------
    dirt_refs: tuple = ((250, 222, 150), (252, 240, 195))
    liquid_refs: tuple = ((238, 130, 50), (242, 167, 60))
    reject_dist2: float = 0.015   # chroma dist^2 beyond which a pixel is OTHER
    dark_sum_min: int = 90        # sum(rgb) below this -> OTHER (shadow/black)
    min_coverage: float = 0.35    # need this frac of pixels to be DIRT/LIQUID
    decide_margin: float = 0.15   # |liquid_ratio-0.5| must exceed this to decide
    min_conf: float = 0.25        # below this, position is treated as UNKNOWN

    # --- text prompt bands (white-pixel fraction) ---------------------------
    text_pan_max: float = 0.024
    text_shake_max: float = 0.040
    text_conf_scale: float = 0.01  # how sharply confidence rises off a band edge

    # --- capacity bar -------------------------------------------------------
    cap_full_ratio: float = 0.85   # yellow fraction along the bar to call "full"

    # --- FSM timing (seconds) -----------------------------------------------
    dig_interval: float = 0.40     # spacing between dig clicks
    dig_timeout: float = 12.0      # max digging with no fill -> SAFE_STOP
    return_timeout: float = 4.0
    unknown_grace: float = 1.5     # tolerate UNKNOWN position this long
    post_shake_settle: float = 0.12

    # --- TIMED empty maneuver (the fast, precise part; detection supervises) -
    empty_back: float = 0.11       # hold S to reach the water (~95-120ms)
    empty_fwd: float = 0.11        # hold W forward; momentum carries onto dirt
    shake_offset: float = 0.01     # into the forward phase, when LMB press starts
    empty_hold_max: float = 2.5    # max LMB hold to empty before retry
    empty_retries: int = 2         # retries before SAFE_STOP if it won't empty

    # --- memory / debounce (transitions need a streak, not one frame) -------
    terrain_confirm: int = 2       # consecutive terrain reads to change state
    cap_confirm: int = 2           # consecutive capacity reads to trust full/empty


# ============================================================================
# Perception
# ============================================================================
def chroma(rgb):
    a = np.asarray(rgb, dtype=np.float64)
    s = a.sum(axis=-1, keepdims=True)
    return a / np.where(s == 0, 1, s)


def ref_chroma(refs):
    return np.array([chroma(r) for r in refs])  # (k, 3)


def classify_region(rgb, cfg, dirt_c=None, liquid_c=None):
    """Classify a region of pixels into DIRT / LIQUID with OTHER-rejection.

    rgb: array shaped (..., 3). Returns a dict:
        position    : DIRT | LIQUID | UNKNOWN
        confidence  : 0..1  (coverage x decisiveness)
        coverage    : fraction of pixels that matched a terrain reference
        liquid_ratio: fraction of matched pixels that were LIQUID
    """
    rgb = np.asarray(rgb, dtype=np.float64).reshape(-1, 3)
    if dirt_c is None:
        dirt_c = ref_chroma(cfg.dirt_refs)
    if liquid_c is None:
        liquid_c = ref_chroma(cfg.liquid_refs)

    sums = rgb.sum(axis=1)
    c = rgb / np.where(sums[:, None] == 0, 1, sums[:, None])
    dd = ((c[:, None, :] - dirt_c[None, :, :]) ** 2).sum(-1).min(1)
    dl = ((c[:, None, :] - liquid_c[None, :, :]) ** 2).sum(-1).min(1)
    nearest = np.minimum(dd, dl)

    is_dark = sums < cfg.dark_sum_min
    is_other = (nearest > cfg.reject_dist2) | is_dark
    keep = ~is_other
    is_liquid = keep & (dl < dd)
    n_total = len(rgb)
    n_liq = int(is_liquid.sum())
    n_keep = int(keep.sum())
    n_dirt = n_keep - n_liq
    coverage = n_keep / n_total if n_total else 0.0

    if n_keep == 0 or coverage < cfg.min_coverage:
        return dict(position=UNKNOWN, confidence=0.0, coverage=coverage,
                    liquid_ratio=0.0, n_dirt=n_dirt, n_liquid=n_liq)

    liquid_ratio = n_liq / n_keep
    if liquid_ratio > 0.5 + cfg.decide_margin:
        pos = LIQUID
    elif liquid_ratio < 0.5 - cfg.decide_margin:
        pos = DIRT
    else:
        pos = UNKNOWN
    conf = coverage * abs(liquid_ratio - 0.5) * 2.0
    return dict(position=pos, confidence=float(conf), coverage=float(coverage),
                liquid_ratio=float(liquid_ratio), n_dirt=n_dirt, n_liquid=n_liq)


def text_state(frac, cfg):
    """Map the white-pixel fraction to a prompt word + confidence."""
    if frac <= cfg.text_pan_max:
        st = "pan"
    elif frac <= cfg.text_shake_max:
        st = "shake"
    else:
        st = "deposit"
    d = min(abs(frac - cfg.text_pan_max), abs(frac - cfg.text_shake_max))
    conf = min(1.0, d / cfg.text_conf_scale)
    return st, conf


def capacity_full(yellow_ratio, cfg):
    """yellow_ratio: fraction of the capacity bar that reads yellow."""
    return bool(yellow_ratio >= cfg.cap_full_ratio)


# ============================================================================
# Decision: "what should I do right now?"  (intents, not actuators)
# The fast back/shake/forward maneuver is run by the macro as ONE proven timed
# routine; here we only choose DIG / EMPTY / RECOVER / SAFE, with a little
# memory so a single flickered frame can't flip the decision.
# ============================================================================
class Decider:
    def __init__(self, cfg):
        self.cfg = cfg
        self.reset(0.0)

    def reset(self, now=0.0):
        self.last_pos = None
        self.pos_streak = 0
        self.last_full = None
        self.full_streak = 0
        self.unknown_since = None
        self.empty_attempts = 0
        self.halted = False
        self.reason = ""

    def _safe(self, reason):
        self.halted = True
        self.reason = reason
        return SAFE

    def decide(self, position, confidence, pan_full, now):
        """position: DIRT/LIQUID/UNKNOWN ; pan_full: True/False/None.
        Returns DIG / EMPTY / RECOVER / SAFE. SAFE latches until reset()."""
        cfg = self.cfg
        if self.halted:
            return SAFE
        pos = position if confidence >= cfg.min_conf else UNKNOWN

        # memory: streaks of consistent reads
        if pos == self.last_pos:
            self.pos_streak += 1
        else:
            self.last_pos, self.pos_streak = pos, 1
        if pan_full == self.last_full:
            self.full_streak += 1
        else:
            self.last_full, self.full_streak = pan_full, 1
        confirmed_liquid = (self.last_pos == LIQUID
                            and self.pos_streak >= cfg.terrain_confirm)
        confirmed_full = (self.last_full is True
                          and self.full_streak >= cfg.cap_confirm)

        if pos == UNKNOWN:
            if self.unknown_since is None:
                self.unknown_since = now
        else:
            self.unknown_since = None

        if confirmed_full:                        # pan full -> empty it
            if self.empty_attempts > cfg.empty_retries:
                return self._safe("pan won't empty after retries")
            self.empty_attempts += 1
            return EMPTY
        if confirmed_liquid:                      # drifted into lava -> recover
            return RECOVER
        if (self.unknown_since is not None
                and now - self.unknown_since > cfg.unknown_grace):
            return self._safe("position unknown too long")
        self.empty_attempts = 0                   # digging cleanly -> reset
        return DIG

    def note_emptied(self):
        """Macro calls this once a maneuver confirms the pan actually emptied."""
        self.empty_attempts = 0
        self.last_full = False
        self.full_streak = self.cfg.cap_confirm
