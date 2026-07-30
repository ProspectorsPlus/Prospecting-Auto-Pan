#!/usr/bin/env python3
"""capacity_tests.py -- pan-capacity calibration validation suite
(reproduction report issue 5; release blocker).

Pure and sim-safe: every test runs against synthetic numpy frames and an
in-memory settings store -- no screen access, no window lookup, no real
config file. The engine module is imported only for its REAL thresholds
(is_yellow / YEL_MIN / YEL_BLUE_GAP) so the tests pin the exact runtime
math the probe and the guard must agree with.

Covers:
  * validate_cap_pair -- the pure endpoint-pair validation rules;
  * Sensing.save_pixels via a MemStore -- a valid pair updates both
    endpoints AND CAP_BAR_WIDTH, an invalid pair writes NOTHING (old
    values byte-intact) and returns ok=False with reasons; the rc.5
    silent-stale-width reproduction (right=[400,894] against
    left=[678,895], stale width 444) now REJECTS instead of saving;
  * Sensing.cap_endpoint_guard on a synthetic BGRA frame with a painted
    gold bar + anti-aliased edge column;
  * Sensing.capacity_probe on the same synthetic frame (runtime math:
    tip is_yellow, cap_fill band fraction, annotated preview);
  * lite_onboarding.calibration_status cap_bar suspicion migration.

Run from the repo root:  python3 capacity_tests.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import numpy as np                             # noqa: E402
from prospector_engine import sensing as sn    # noqa: E402
from prospector_engine import engine as eng    # noqa: E402
import lite_onboarding as lo                   # noqa: E402

FAILS = []


def chk(cond, msg):
    print(("  ok: " if cond else "  FAIL: ") + msg)
    if not cond:
        FAILS.append(msg)


# --------------------------------------------------------------------------
# sim world: scripted screen + in-memory store (no real I/O, no display)
# --------------------------------------------------------------------------

class FakeMSS(object):
    """Scripted po._MSS seam: 'grabs' always return the synthetic frame."""

    def __init__(self, frame):
        self.frame = frame
        self.monitors = [None, {"left": 0, "top": 0,
                                "width": frame.shape[1],
                                "height": frame.shape[0]}]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def grab(self, region):
        return self.frame


class FakePo(object):
    """Minimal engine-module stand-in: the REAL runtime thresholds, a
    scripted screen, a scripted window lookup. Any screen access without
    a scripted frame is an error (sim safety)."""

    def __init__(self, frame=None, win=None):
        self.np = np
        self.is_yellow = eng.is_yellow
        self.is_white = eng.is_white
        self.YEL_MIN = eng.YEL_MIN
        self.YEL_BLUE_GAP = eng.YEL_BLUE_GAP
        self._frame = frame
        self._win = win or {"found": False}

    def _MSS(self):
        if self._frame is None:
            raise AssertionError("screen access in a sim-safe test")
        return FakeMSS(self._frame)

    def find_roblox_window(self):
        return dict(self._win)


class MemStore(object):
    """In-memory settings store with write counting (FileStore shape)."""

    def __init__(self, doc=None):
        self.doc = json.loads(json.dumps(doc or {}))
        self.writes = 0

    def get(self):
        return json.loads(json.dumps(self.doc))

    def write(self, doc, changed_keys, source):
        self.doc = json.loads(json.dumps(doc))
        self.writes += 1


def make_frame(W=900, H=200, x0=200, x1=700, y0=90, y1=111, fill_to=None):
    """BGRA frame: dark background, solid-gold bar columns [x0, fill_to)
    within rows [y0, y1), plus pale anti-aliased edge columns at x0-1 and
    fill_to (they fail BOTH the solid-gold walk-in test and runtime
    is_yellow -- the exact hazard of a manual edge click)."""
    fx1 = x1 if fill_to is None else fill_to
    f = np.zeros((H, W, 4), np.uint8)
    f[:, :, :3] = 30
    f[:, :, 3] = 255
    f[y0:y1, x0:fx1] = (40, 180, 230, 255)          # BGR solid gold
    for edge in (x0 - 1, fx1):
        f[y0:y1, edge] = (140, 170, 200, 255)        # pale AA blend
    return f


def sess(s, frame):
    """The engine sims' trick: install a session frame directly."""
    s._shot = frame
    s._shot_h, s._shot_w = frame.shape[0], frame.shape[1]


# --------------------------------------------------------------------------
# 1. validate_cap_pair (pure)
# --------------------------------------------------------------------------

def t_validate_pair():
    print("[1] validate_cap_pair (pure pair validation)")
    v = sn.validate_cap_pair
    ok, reasons, w = v([1122, 894], [678, 895], 1800, 1126,
                       [0, 39, 1800, 1087])
    chk(ok and not reasons and w == 444,
        "valid pair passes with the correct width (444)")
    ok, reasons, w = v([400, 894], [678, 895], 0, 0)
    chk(not ok and w == -278 and "x=400" in reasons[0]
        and "x=678" in reasons[0] and "swapped" in reasons[0],
        "inverted pair fails naming both x values")
    ok, reasons, _ = v([678, 894], [678, 894], 0, 0)
    chk(not ok and any("0 px apart" in r for r in reasons),
        "same-point pair fails naming the 0 px distance")
    ok, reasons, _ = v([1122, 894], [678, 950], 0, 0)
    chk(not ok and any("y=894" in r and "y=950" in r and "56 px" in r
                       for r in reasons),
        "y-mismatch fails naming both rows and the distance")
    ok, reasons, _ = v([700, 894], [678, 894], 0, 0)
    chk(not ok and any("22 px" in r and "24 px minimum" in r
                       for r in reasons),
        "sub-24 width fails naming the width and the minimum")
    ok, reasons, _ = v([2000, 100], [500, 100], 1440, 900)
    chk(not ok and any("(2000, 100)" in r and "1440x900" in r
                       for r in reasons),
        "out-of-frame point fails naming the point and the frame")
    ok, reasons, _ = v([1400, 100], [10, 100], 1440, 900)
    chk(not ok and any("1390 px" in r and "90%" in r for r in reasons),
        "width above 0.9*frame_w fails naming the numbers")
    ok, reasons, _ = v([1290, 850], [820, 850], 1440, 900,
                       [100, 50, 1200, 800])
    chk(not ok and any("(1290, 850)" in r and "Roblox window" in r
                       for r in reasons),
        "point outside the known window rect fails naming it")
    ok, reasons, _ = v([1290, 100], [80, 100], 0, 0,
                       [100, 50, 1200, 800])
    chk(not ok and any("1210 px" in r and "1200 px" in r
                       for r in reasons),
        "width above the window width fails naming both")
    ok, reasons, _ = v([30, 100], [-10, 100], 0, 0)
    chk(not ok and any("start at x=-10" in r for r in reasons),
        "runtime band start below 0 fails naming the start column")


# --------------------------------------------------------------------------
# 2. save_pixels through a MemStore (validated atomic save path)
# --------------------------------------------------------------------------

RC5 = {"CAP_FULL_PIXEL": [1122, 894], "CAP_LEFT_PIXEL": [678, 895],
       "CAP_BAR_WIDTH": 444}


def t_save_pixels():
    print("[2] save_pixels: validated atomic save (MemStore)")
    # valid pair: both endpoints AND the width update, result carries ok
    st = MemStore({"CAP_BAR_WIDTH": 10})
    s = sn.Sensing(FakePo(), st)
    r = s.save_pixels({"CAP_FULL_PIXEL": [900, 760],
                       "CAP_LEFT_PIXEL": [500, 760]})
    chk(r.get("ok") is True and r.get("width") == 400
        and "CAP_BAR_WIDTH" in r["saved"],
        "valid pair save returns ok + derived width, saved lists width")
    chk(st.doc["CAP_FULL_PIXEL"] == [900, 760]
        and st.doc["CAP_LEFT_PIXEL"] == [500, 760]
        and st.doc["CAP_BAR_WIDTH"] == 400 and st.writes == 1,
        "valid pair persists both endpoints and CAP_BAR_WIDTH=400")
    chk(st.doc.get("AUTO_CALIBRATE") is False,
        "core save still forces AUTO_CALIBRATE off (contract kept)")

    # the rc.5 silent-stale-width reproduction now REJECTS: right=[400,894]
    # against stored left=[678,895] with stale width 444 -- previously
    # this saved the inverted endpoint and kept width 444 silently.
    st = MemStore(RC5)
    before = json.dumps(st.doc, sort_keys=True)
    s = sn.Sensing(FakePo(), st)
    r = s.save_pixels({"CAP_FULL_PIXEL": [400, 894]},
                      colors={"CAP_FULL_PIXEL": "#101010"})
    chk(r.get("ok") is False and r.get("error") == "cap_endpoints"
        and r.get("saved") == [],
        "rc.5 repro: inverted right end is REJECTED (ok=False, saved=[])")
    chk(r.get("right") == [400, 894] and r.get("left") == [678, 895]
        and r.get("width") == -278,
        "rc.5 repro: rejection echoes the offending pair and width")
    chk(any("x=400" in x and "x=678" in x for x in r.get("reasons", [])),
        "rc.5 repro: reasons name the actual numbers")
    chk(json.dumps(st.doc, sort_keys=True) == before and st.writes == 0,
        "rc.5 repro: NOTHING written -- old values byte-intact "
        "(right [1122,894], width 444 preserved)")
    chk(st.doc["CAP_BAR_WIDTH"] == 444
        and st.doc["CAP_FULL_PIXEL"] == [1122, 894],
        "rc.5 repro: stale-width path is dead, previous values retained")

    # left-end save against the stored right validates the merged pair
    # and ALWAYS refreshes the width (no silent stale width)
    st = MemStore(dict(RC5, CAP_BAR_WIDTH=999))
    s = sn.Sensing(FakePo(), st)
    r = s.save_pixels({"CAP_LEFT_PIXEL": [678, 895]})
    chk(r.get("ok") is True and st.doc["CAP_BAR_WIDTH"] == 444,
        "pair-touching save always rewrites the width (999 -> 444)")

    # y-mismatch rejection through the save path
    st = MemStore(RC5)
    s = sn.Sensing(FakePo(), st)
    r = s.save_pixels({"CAP_LEFT_PIXEL": [678, 950]})
    chk(r.get("ok") is False and st.writes == 0
        and any("y=894" in x and "y=950" in x
                for x in r.get("reasons", [])),
        "row-mismatched left end rejected with both rows named")

    # window-rect bounds apply when the stored rect is known
    st = MemStore(dict(RC5, CALIB_WINDOW_RECT=[0, 39, 1800, 1087]))
    s = sn.Sensing(FakePo(), st)
    r = s.save_pixels({"CAP_FULL_PIXEL": [1900, 894]})
    chk(r.get("ok") is False and st.writes == 0
        and any("(1900, 894)" in x for x in r.get("reasons", [])),
        "right end outside the stored window rect rejected")

    # import path (derive_from_window=False + fallback) validates too:
    # a broken import fails loudly, old values retained
    st = MemStore(RC5)
    before = json.dumps(st.doc, sort_keys=True)
    s = sn.Sensing(FakePo(), st)
    r = s.save_pixels({"CAP_FULL_PIXEL": [400, 894],
                       "CAP_LEFT_PIXEL": [678, 895]},
                      cap_bar_width_fallback=444,
                      derive_from_window=False)
    chk(r.get("ok") is False and st.writes == 0
        and json.dumps(st.doc, sort_keys=True) == before,
        "broken import rejected loudly, nothing written")
    # ...while a valid import still lands without touching the flags
    st = MemStore({})
    s = sn.Sensing(FakePo(), st)
    r = s.save_pixels({"CAP_FULL_PIXEL": [1122, 894],
                       "CAP_LEFT_PIXEL": [678, 895]},
                      ratios={"CAP_FULL_PIXEL": [0.62333, 0.78657]},
                      window_rect=[0, 39, 1800, 1087],
                      derive_from_window=False)
    chk(r.get("ok") is True and st.doc["CAP_BAR_WIDTH"] == 444
        and "AUTO_CALIBRATE" not in st.doc,
        "valid import saves pair + width, auto-calibrate flags untouched")

    # a save NOT touching the endpoints is never blocked by a bad stored
    # pair (legacy behavior preserved; the pair is flagged needs_review)
    st = MemStore({"CAP_FULL_PIXEL": [400, 894],
                   "CAP_LEFT_PIXEL": [678, 895], "CAP_BAR_WIDTH": 444})
    s = sn.Sensing(FakePo(), st)
    r = s.save_pixels({"DEPOSIT_PIX": [770, 981]})
    chk("DEPOSIT_PIX" in r["saved"] and st.writes == 1
        and st.doc["CAP_BAR_WIDTH"] == 444,
        "non-capacity save with a bad stored pair still lands (no width "
        "rewrite)")


# --------------------------------------------------------------------------
# 3. cap_endpoint_guard on a synthetic session frame
# --------------------------------------------------------------------------

def t_endpoint_guard():
    print("[3] cap_endpoint_guard (solid-gold walk-in for manual picks)")
    frame = make_frame()                     # gold cols 200..699, AA at 700
    s = sn.Sensing(FakePo(), MemStore())
    sess(s, frame)

    r = s.cap_endpoint_guard("CAP_RIGHT", 700, 100)
    chk(r["ok"] and r["adjusted"] and r["x"] == 699 and r["y"] == 100,
        "click on the pale right edge adjusts inward to solid gold (699)")
    px = frame[r["y"], r["x"]]
    chk(sn.Sensing._solid_gold(int(px[2]), int(px[1]), int(px[0])),
        "the adjusted point itself passes the solid-gold test")
    chk(eng.is_yellow(int(px[2]), int(px[1]), int(px[0])),
        "the adjusted point passes the RUNTIME is_yellow test too")

    r = s.cap_endpoint_guard("CAP_RIGHT", 650, 100)
    chk(r["ok"] and not r["adjusted"] and r["x"] == 650,
        "a click already on solid gold is accepted unadjusted")

    r = s.cap_endpoint_guard("CAP_RIGHT", 650, 89)
    chk(r["ok"] and r["adjusted"] and r["y"] == 90,
        "a 1-px row misclick is tolerated via y+-1")

    r = s.cap_endpoint_guard("CAP_RIGHT", 800, 100)
    chk(not r["ok"] and "not on the gold capacity bar" in r["reason"]
        and "#1e1e1e" in r["reason"],
        "open-background click fails with the not-on-bar reason + the "
        "clicked pixel's hex")

    r = s.cap_endpoint_guard("CAP_LEFT", 199, 100)
    chk(r["ok"] and r["adjusted"] and r["x"] == 200,
        "left-tip guard walks RIGHT off the pale left edge to solid gold")

    try:
        s.cap_endpoint_guard("NOPE", 1, 1)
        chk(False, "unknown kind raises BAD_PARAMS")
    except sn.SensingError as e:
        chk(e.code == "BAD_PARAMS", "unknown kind raises BAD_PARAMS")
    s2 = sn.Sensing(FakePo(), MemStore())
    try:
        s2.cap_endpoint_guard("CAP_RIGHT", 1, 1)
        chk(False, "guard without a session raises BAD_STATE")
    except sn.SensingError as e:
        chk(e.code == "BAD_STATE", "guard without a session raises "
                                   "BAD_STATE")


# --------------------------------------------------------------------------
# 4. capacity_probe (runtime-math test action) on the synthetic frame
# --------------------------------------------------------------------------

def t_capacity_probe():
    print("[4] capacity_probe (runtime math, annotated preview)")
    frame = make_frame()                       # full bar, cols 200..699
    cfg = {"CAP_FULL_PIXEL": [694, 100], "CAP_LEFT_PIXEL": [206, 100],
           "CAP_BAR_WIDTH": 488}
    s = sn.Sensing(FakePo(frame=frame), MemStore(cfg))
    r = s.capacity_probe()
    chk(r["ok"] is True and r["tip_yellow"] is True and not r["reasons"],
        "healthy full-bar calibration probes PASS")
    chk(abs(r["fill_frac"] - 1.0) < 0.02,
        "fill fraction ~1.0 over the exact runtime band (got %s)"
        % r["fill_frac"])
    chk(r["width"] == 488 and r["stored_width"] == 488
        and r["right"] == [694, 100] and r["left"] == [206, 100],
        "probe echoes endpoints + widths")
    chk(str(r["preview"]).startswith("data:image/png;base64,")
        and len(r["preview"]) < 110000,
        "annotated preview is a PNG data URL under ~80 KB")

    # a half-full bar: the painted fraction shows up in fill_frac and the
    # tip honestly fails the runtime gold test
    half = make_frame(fill_to=450)             # gold cols 200..449
    s = sn.Sensing(FakePo(frame=half), MemStore(cfg))
    r = s.capacity_probe()
    chk(abs(r["fill_frac"] - 0.5) < 0.03,
        "fill fraction tracks the painted level (~0.5, got %s)"
        % r["fill_frac"])
    chk(r["ok"] is False and r["tip_yellow"] is False
        and any(r["tip_hex"] in x for x in r["reasons"]),
        "non-gold tip fails with the measured hex in the reason")

    # break the stored pair: probe reports the pair failure with reasons
    bad = dict(cfg, CAP_FULL_PIXEL=[206, 100], CAP_LEFT_PIXEL=[694, 100])
    s = sn.Sensing(FakePo(frame=frame), MemStore(bad))
    r = s.capacity_probe()
    chk(r["ok"] is False and any("swapped" in x for x in r["reasons"]),
        "swapped stored pair probes FAIL with the swap reason")

    # stored width drifted from the tips: the runtime band is wrong
    drift = dict(cfg, CAP_BAR_WIDTH=300)
    s = sn.Sensing(FakePo(frame=frame), MemStore(drift))
    r = s.capacity_probe()
    chk(r["ok"] is False
        and any("300 px" in x and "488 px" in x for x in r["reasons"]),
        "stored-width drift fails naming stored vs derived width")

    # nothing calibrated yet: honest failure, no grab needed
    s = sn.Sensing(FakePo(), MemStore({}))
    r = s.capacity_probe()
    chk(r["ok"] is False and "not calibrated" in r["reasons"][0],
        "missing endpoints probe FAIL without touching the screen")


# --------------------------------------------------------------------------
# 5. calibration_status cap_bar migration (needs_review suspicion)
# --------------------------------------------------------------------------

def t_status_migration():
    print("[5] calibration_status: cap_bar suspicion -> needs_review")
    base = {"AUTO_CALIBRATE": False,
            "CAP_FULL_PIXEL": [1122, 894], "CAP_LEFT_PIXEL": [678, 895],
            "CAP_BAR_WIDTH": 444}
    st = lo.calibration_status(dict(base))
    chk(st["cap_bar"]["status"] == "ok",
        "healthy stored pair stays 'ok'")

    tail = "Run Test capacity calibration or redo the Capacity step."
    cases = [
        (dict(base, CAP_FULL_PIXEL=[400, 894]),
         ["x=400", "x=678"], "inverted stored pair"),
        (dict(base, CAP_BAR_WIDTH=440),
         ["440 px", "444 px"], "stored width off the tips by >2"),
        (dict(base, CAP_LEFT_PIXEL=[678, 950], CAP_BAR_WIDTH=444),
         ["y=894", "y=950"], "stored tips on different rows"),
        (dict(base, CAP_FULL_PIXEL=[700, 894],
              CAP_LEFT_PIXEL=[678, 894], CAP_BAR_WIDTH=22),
         ["22 px"], "stored width below 24"),
    ]
    for cfg, needles, label in cases:
        st = lo.calibration_status(cfg)
        d = st["cap_bar"]
        chk(d["status"] == "needs_review"
            and all(n in d["detail"] for n in needles)
            and d["detail"].endswith(tail),
            "%s -> needs_review naming the numbers + repair hint" % label)

    # values are never modified by status computation
    cfg = dict(base, CAP_FULL_PIXEL=[400, 894])
    snapshot = json.dumps(cfg, sort_keys=True)
    lo.calibration_status(cfg)
    chk(json.dumps(cfg, sort_keys=True) == snapshot,
        "status computation never mutates the config")

    # the suspicion helper is quiet when endpoints are absent/unset
    chk(lo.cap_pair_suspicion({}) is None
        and lo.cap_pair_suspicion({"CAP_FULL_PIXEL": [0, 0],
                                   "CAP_LEFT_PIXEL": [0, 0]}) is None,
        "no suspicion without a stored pair")


def main():
    print("capacity_tests: pan-capacity validation suite (sim-safe)")
    t_validate_pair()
    t_save_pixels()
    t_endpoint_guard()
    t_capacity_probe()
    t_status_migration()
    print()
    if FAILS:
        print("FAILED (%d):" % len(FAILS))
        for f in FAILS:
            print("  - " + f)
        sys.exit(1)
    print("ALL CAPACITY TESTS PASSED")


if __name__ == "__main__":
    main()
