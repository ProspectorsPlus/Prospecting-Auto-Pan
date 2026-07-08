"""Vision — pure functions from a captured frame to raw signals.

All color tests reuse the PROVEN v2 thresholds (gold: r,g >= 140 and
b <= min(r,g) - 45; cue text: all channels >= 160 with spread <= 70,
counting white-pixel FRACTION so thin text isn't diluted by background).

The capacity bar is read as a fill FRACTION over an inset band —
anti-aliased tip pixels fail the gold test even when the bar is full,
which is why the ends are inset (a real v2 bug: digs 'not registering'
right after calibration).

Everything here is stateless and numpy-only so it can be unit-tested with
synthetic frames in any sandbox.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class RawSignals:
    fill: float          # 0..1 capacity-bar gold fraction (inset band)
    dep_frac: float      # white fraction in the Collect Deposit cue box
    pan_frac: float      # white fraction in the Pan cue box
    shk_frac: float      # white fraction in the Shake cue box


@dataclass(frozen=True)
class Geometry:
    """The single combined capture region + slices for each signal.

    One grab per frame (instead of v2's five) keeps 60 fps cheap and every
    signal sampled at the same instant."""
    left: int
    top: int
    width: int
    height: int
    cap_sl: tuple        # (y0, y1, x0, x1) relative slices into the frame
    dep_sl: tuple
    pan_sl: tuple
    shk_sl: tuple

    def region(self):
        return {"left": self.left, "top": self.top,
                "width": self.width, "height": self.height}


def build_geometry(cfg):
    """Compute the combined region from calibrated pixels."""
    lx, ly = cfg.CAP_LEFT_PIXEL
    rx, ry = cfg.CAP_FULL_PIXEL
    band = int(cfg.CAP_BAND_H)
    box = int(cfg.CUE_BOX)
    inset = int(cfg.CAP_INSET_PX)

    boxes = []                                   # (x0, y0, x1, y1) in screen px
    cap_y = (ly + ry) // 2
    cap_box = (min(lx, rx) + inset, cap_y - band // 2,
               max(lx, rx) - inset, cap_y - band // 2 + band)
    boxes.append(cap_box)

    def cue_box(pix):
        x, y = pix
        b = (x - box // 2, y - box // 2, x - box // 2 + box, y - box // 2 + box)
        boxes.append(b)
        return b

    dep_b = cue_box(cfg.DEPOSIT_PIX)
    pan_b = cue_box(cfg.PAN_PIX)
    shk_b = cue_box(cfg.SHAKE_PIX)

    left = min(b[0] for b in boxes)
    top = min(b[1] for b in boxes)
    right = max(b[2] for b in boxes)
    bottom = max(b[3] for b in boxes)

    def rel(b):
        return (b[1] - top, b[3] - top, b[0] - left, b[2] - left)

    return Geometry(left=int(left), top=int(top),
                    width=int(right - left), height=int(bottom - top),
                    cap_sl=rel(cap_box), dep_sl=rel(dep_b),
                    pan_sl=rel(pan_b), shk_sl=rel(shk_b))


def _slice(frame, sl):
    y0, y1, x0, x1 = sl
    return frame[y0:y1, x0:x1, :]


def gold_fraction(px_bgr, yel_min, blue_gap):
    """Fraction of pixels that read 'gold' (the bar's fill color)."""
    a = px_bgr.astype(np.int16)
    b, g, r = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    gold = (r >= yel_min) & (g >= yel_min) & (b <= np.minimum(r, g) - blue_gap)
    return float(gold.mean()) if gold.size else 0.0


def white_fraction(px_bgr, white_min, spread):
    """Fraction of pixels that read 'cue text white'."""
    a = px_bgr.astype(np.int16)
    b, g, r = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    lo = np.minimum(np.minimum(r, g), b)
    hi = np.maximum(np.maximum(r, g), b)
    white = (lo >= white_min) & ((hi - lo) <= spread)
    return float(white.mean()) if white.size else 0.0


def analyze(frame_bgra, geo, cfg):
    """One frame (H,W,4 BGRA from mss) -> RawSignals."""
    f = np.asarray(frame_bgra)[:, :, :3]
    return RawSignals(
        fill=gold_fraction(_slice(f, geo.cap_sl), cfg.YEL_MIN, cfg.YEL_BLUE_GAP),
        dep_frac=white_fraction(_slice(f, geo.dep_sl), cfg.CUE_WHITE_MIN,
                                cfg.CUE_WHITE_SPREAD),
        pan_frac=white_fraction(_slice(f, geo.pan_sl), cfg.CUE_WHITE_MIN,
                                cfg.CUE_WHITE_SPREAD),
        shk_frac=white_fraction(_slice(f, geo.shk_sl), cfg.CUE_WHITE_MIN,
                                cfg.CUE_WHITE_SPREAD),
    )


def is_white_mean(rgb, white_min=175):
    """Perfect-dig trigger test on a mean RGB (green-zone flash to white)."""
    r, g, b = rgb
    return r >= white_min and g >= white_min and b >= white_min
