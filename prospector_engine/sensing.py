"""Engine-owned calibration sensing (Phase 04 C8; protocol section 4.15,
ISS-137).

The calibration capture / pixel-sampling / detection / OCR bodies moved
here VERBATIM from the two byte-identical host apps (prospecting_app.py
and its windows mirror). Hosts no longer read the screen: this module is
the one implementation, reached two ways --

  * in-process: Prospector Lite's Api calls a Sensing bound to a
    FileStore over its own config file (legacy + flag-off behavior is
    byte-compatible with the old app-side bodies);
  * PPE1 command channel: ipc.Server dispatches the calibration.* verbs
    to a Sensing bound to the server's settings document.

The capture session (protocol section 4.15): the engine keeps ONE
full-resolution BGRA frame per Sensing instance, replaced by the next
capture/detect/cueMask-begin, dropped on run.start (ipc mode). All pixel
math runs against that frame at full resolution; hosts only ever hold
the stride-downsampled preview.

Every screen read goes through the engine module's seams (``po._MSS``,
``po.find_roblox_window``, ``po._finds_ocr_array``, ``po.is_white`` /
``po.is_yellow``), so the sim world's patches script this module the
same way they script the run-time Detector.
"""
import base64
import json
import os
import threading


# The single-point calibration surface (matrix row calibration-pixels).
# Source of truth mirrored from prospecting_ui.PIXEL_FIELDS (the engine
# cannot import UI modules); extend HERE first when the UI gains a key.
PIXEL_KEYS = (
    "CAP_FULL_PIXEL", "CAP_LEFT_PIXEL", "DEPOSIT_PIX", "PAN_PIX",
    "SHAKE_PIX", "DIG_TRIGGER_PIXEL",
    "MONEY_TL_PIXEL", "MONEY_BR_PIXEL", "SHARDS_TL_PIXEL",
    "SHARDS_BR_PIXEL", "FIND_TL_PIXEL", "FIND_BR_PIXEL",
)

# The 6 core detection points (sampleSaved + the exporter's key list).
CORE_PIXEL_KEYS = ("CAP_FULL_PIXEL", "CAP_LEFT_PIXEL", "DEPOSIT_PIX",
                   "PAN_PIX", "SHAKE_PIX", "DIG_TRIGGER_PIXEL")

# Built-in auto-calibration ratio profile (matrix row
# profiles-calibration-builtin; moved verbatim from the apps, app:235).
# Seeded from a real calibration: lets brand-new users Auto-calibrate
# with zero clicking.
PIXEL_RATIOS_DEFAULT = {
    "CAP_FULL_PIXEL":    [0.66319, 0.87963],
    "CAP_LEFT_PIXEL":    [0.33681, 0.87963],
    "DEPOSIT_PIX":       [0.42569, 0.95463],
    "PAN_PIX":           [0.47222, 0.95463],
    "SHAKE_PIX":         [0.46111, 0.95463],
    "DIG_TRIGGER_PIXEL": [0.74861, 0.53704],
}

CUE_PIXEL_KEY = {"PAN": "PAN_PIX", "SHAKE": "SHAKE_PIX",
                 "DEPOSIT": "DEPOSIT_PIX"}


class SensingError(Exception):
    """A protocol-mapped sensing failure (the ipc layer turns this into
    a structured NACK; in-process wrappers never see it for soft paths
    because those return today's {'error': ...} dicts instead)."""

    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class FileStore(object):
    """In-process settings access for a host-embedded Sensing: reads the
    config file fresh per call (the app's load_saved() semantics) and
    writes through the engine's single atomic writer. No migration, no
    version stamping -- flag-off Lite behavior stays byte-compatible
    (a v0 file stays v0 until the ipc engine migrates it)."""

    def __init__(self, config_path):
        self.path = config_path

    def get(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                doc = json.load(f)
            return doc if isinstance(doc, dict) else {}
        except (OSError, ValueError):
            return {}

    def write(self, doc, changed_keys, source):
        from . import settings as settings_mod
        settings_mod.atomic_write(self.path, doc)


def _png_data_url(rgb_array):
    """RGB HxWx3 uint8 -> data:image/png;base64,... (mss.tools.to_png is
    a pure encoder -- no screen access, sim-safe)."""
    import mss.tools
    png = mss.tools.to_png(rgb_array.tobytes(),
                           (rgb_array.shape[1], rgb_array.shape[0]))
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


class Sensing(object):
    """The one calibration sensing implementation (protocol 4.15)."""

    def __init__(self, po, store):
        self.po = po            # the engine module (seams + thresholds)
        self.store = store
        self.lock = threading.RLock()
        self._shot = None       # full-res (h, w, 4) BGRA session frame
        self._shot_w = 0
        self._shot_h = 0
        self._cm = None         # cue-mask editor state (dict) or None

    # -- session plumbing --------------------------------------------------

    def drop_session(self):
        with self.lock:
            self._shot = None
            self._shot_w = self._shot_h = 0
            self._cm = None

    def _require_session(self):
        if self._shot is None:
            raise SensingError("BAD_STATE", "no capture session",
                               {"expected": "captureSession"})

    def _grab_full(self):
        """Full primary-monitor grab -> (h, w, 4) BGRA ndarray (verbatim
        Api._grab_full, app:4266)."""
        np = self.po.np
        with self.po._MSS() as sct:
            raw = sct.grab(sct.monitors[1])
        return np.asarray(raw)

    def _set_session(self, arr):
        self._shot = arr
        self._shot_h, self._shot_w = arr.shape[0], arr.shape[1]

    def _preview(self):
        """Stride-downsampled PNG preview of the session frame (verbatim
        start_overlay_calibrate, app:3891-3895: <= ~1600 px wide)."""
        step = max(1, int(round(self._shot_w / 1600.0)))
        disp = self._shot[::step, ::step]
        rgb = disp[:, :, (2, 1, 0)]
        import mss.tools
        png = mss.tools.to_png(rgb.tobytes(), (disp.shape[1], disp.shape[0]))
        image = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        return image, disp.shape[1], disp.shape[0]

    def session_preview(self):
        """{image, imageW, imageH, fullW, fullH} of the CURRENT session
        (no fresh grab) -- used by in-process hosts after detect()/
        cue_begin() so the overlay shows the frame those ops captured."""
        with self.lock:
            self._require_session()
            image, iw, ih = self._preview()
            return {"image": image, "imageW": iw, "imageH": ih,
                    "fullW": self._shot_w, "fullH": self._shot_h}

    # -- 4.15 verbs --------------------------------------------------------

    def detect_window(self):
        """calibration.detectWindow: the shipped per-platform window
        lookup, legacy dict shape ({found, x, y, w, h, title} or
        {found: False, error})."""
        return self.po.find_roblox_window()

    def capture(self):
        """calibration.capture: fresh full grab starts/replaces the
        session; result carries the preview + dimensions."""
        with self.lock:
            self._set_session(self._grab_full())
            self._cm = None
            image, iw, ih = self._preview()
            return {"image": image, "imageW": iw, "imageH": ih,
                    "fullW": self._shot_w, "fullH": self._shot_h}

    def pick(self, fx, fy):
        """calibration.pick: preview fractions -> full-res pixel from the
        STORED session frame (verbatim overlay_pick, app:3985-3993)."""
        with self.lock:
            self._require_session()
            w, h = self._shot_w, self._shot_h
            x = max(0, min(w - 1, int(round(float(fx) * w))))
            y = max(0, min(h - 1, int(round(float(fy) * h))))
            px = self._shot[y, x]
            b, g, r = int(px[0]), int(px[1]), int(px[2])
            return {"x": x, "y": y, "rgb": [r, g, b],
                    "hex": "#%02x%02x%02x" % (r, g, b)}

    def crop(self, rect, zoom=None):
        """calibration.crop: full-res crop of the session frame, zoomed
        (verbatim _region_preview_save crop math, app:4007-4019)."""
        np = self.po.np
        with self.lock:
            self._require_session()
            arr = self._shot
            x0, y0 = int(rect["x"]), int(rect["y"])
            x1, y1 = x0 + int(rect["w"]), y0 + int(rect["h"])
            h, w = arr.shape[0], arr.shape[1]
            x0 = max(0, min(w - 1, x0)); x1 = max(x0 + 1, min(w, x1))
            y0 = max(0, min(h - 1, y0)); y1 = max(y0 + 1, min(h, y1))
            crop = arr[y0:y1, x0:x1, :3]
            cw, ch = int(x1 - x0), int(y1 - y0)
            z = int(zoom) if zoom else max(1, min(6, 520 // max(1, cw)))
            if z > 1:
                crop = crop.repeat(z, 0).repeat(z, 1)
            rgb = np.ascontiguousarray(crop[:, :, ::-1])
            return {"image": _png_data_url(rgb), "w": cw, "h": ch}

    def sample_saved(self):
        """calibration.sampleSaved: live 6x6 box average around the saved
        core points with the ENGINE's own thresholds (verbatim
        sample_pixels, app:2499-2544; is_white/is_yellow are the same
        140/45/175 values the run-time Detector uses)."""
        po = self.po
        np = po.np
        cur = self.store.get()
        out = {}
        try:
            with po._MSS() as sct:
                m = sct.monitors[0]
                L, T = m["left"], m["top"]
                Rg, Bg = L + m["width"], T + m["height"]
                for k in CORE_PIXEL_KEYS:
                    v = cur.get(k)
                    if not (isinstance(v, (list, tuple)) and len(v) == 2):
                        continue
                    x, y = int(v[0]), int(v[1])
                    bx = min(max(x - 3, L), Rg - 6)
                    by = min(max(y - 3, T), Bg - 6)
                    img = np.asarray(sct.grab(
                        {"left": bx, "top": by,
                         "width": 6, "height": 6}))[:, :, :3]
                    b, g, r = [int(c) for c in img.reshape(-1, 3).mean(0)]
                    out[k] = {"r": r, "g": g, "b": b}
        except Exception as e:
            return {"error": str(e)}
        res = {"pixels": out}
        if "CAP_FULL_PIXEL" in out:
            p = out["CAP_FULL_PIXEL"]
            res["capFull"] = bool(po.is_yellow(p["r"], p["g"], p["b"]))
        whites = {}
        for k in ("DEPOSIT_PIX", "PAN_PIX", "SHAKE_PIX"):
            if k in out:
                p = out[k]
                whites[k] = bool(po.is_white(p["r"], p["g"], p["b"]))
        res["whites"] = whites
        return res

    # -- wizard detection (calibration.detect) ----------------------------

    def _detect_capacity_px(self, arr):
        """Verbatim Api._detect_capacity_px (app:4283-4328) MINUS the
        dormant empty-bar-diff branch (wizard_capture_empty had no
        shipped call site; protocol 4.15 drops it from v1.0)."""
        np = self.po.np
        H, W = arr.shape[0], arr.shape[1]
        b = arr[:, :, 0].astype(np.int16)
        g = arr[:, :, 1].astype(np.int16)
        r = arr[:, :, 2].astype(np.int16)
        # bright, saturated GOLD (brighter than sandy beach, not pale)
        gold = (r >= 150) & (g >= 135) & (b <= np.minimum(r, g) - 45)
        # only look in the lower-centre band where the Pan Fill bar lives
        region = np.zeros((H, W), dtype=bool)
        y0, y1 = int(H * 0.70), int(H * 0.91)
        x0, x1 = int(W * 0.20), int(W * 0.80)
        region[y0:y1, x0:x1] = True
        cand = gold & region
        counts = cand.sum(axis=1)
        y = int(counts.argmax())
        if int(counts[y]) < 50:
            return {"ok": False, "error": "no full bar found"}
        xs = np.where(cand[y])[0]
        seg = max(np.split(xs, np.where(np.diff(xs) > 6)[0] + 1), key=len)
        left, right = int(seg[0]), int(seg[-1])
        if right - left < 50:
            return {"ok": False, "error": "bar too short"}

        # The macro's full-bar test (is_yellow) needs SOLID gold; the
        # literal edge pixel is a pale anti-aliased blend that fails it.
        # Walk both ends inward to a solidly-gold pixel, plus a margin.
        def _solid(x):
            pr, pg, pb = int(r[y, x]), int(g[y, x]), int(b[y, x])
            return pr >= 140 and pg >= 140 and pb <= min(pr, pg) - 55
        xr = right
        while xr > left and not _solid(xr):
            xr -= 1
        right = max(left + 4, xr - 6)
        xl = left
        while xl < right and not _solid(xl):
            xl += 1
        left = min(right - 4, xl + 6)
        return {"ok": True, "left": [left, y], "right": [right, y]}

    def _detect_cue_px(self, arr, which):
        """Verbatim Api._detect_cue_px (app:4330-4375)."""
        np = self.po.np
        H, W = arr.shape[0], arr.shape[1]
        b = arr[:, :, 0].astype(np.int16)
        g = arr[:, :, 1].astype(np.int16)
        r = arr[:, :, 2].astype(np.int16)
        lo = np.minimum(np.minimum(r, g), b)
        hi = np.maximum(np.maximum(r, g), b)
        white = (lo >= 225) & ((hi - lo) <= 26)
        # centre-bottom search zone: below the bar, above the hotbar; the
        # right side is trimmed so "Auto Sell" never competes
        mask = np.zeros((H, W), dtype=bool)
        y0, y1 = int(H * 0.68), int(H * 0.86)
        x0, x1 = int(W * 0.25), int(W * 0.62)
        mask[y0:y1, x0:x1] = white[y0:y1, x0:x1]
        rows = mask.sum(axis=1)
        rws = np.where(rows >= 6)[0]
        if len(rws) == 0:
            return {"ok": False, "error": "prompt not visible"}
        # the prompt is the LOWEST white text line in the zone
        bottom = int(rws.max())
        pb0 = max(0, bottom - 60)
        band = np.zeros((H, W), dtype=bool)
        band[pb0:bottom + 1] = mask[pb0:bottom + 1]
        cols = np.where(band.any(axis=0))[0]
        if len(cols) == 0:
            return {"ok": False, "error": "no prompt row"}
        min_x = int(cols[0])
        end = min_x
        for c in cols:                 # first blob from the left = mouse icon
            if int(c) - end <= 12:
                end = int(c)
            else:
                break
        ys, xs = np.where(band)
        sel = (xs >= min_x) & (xs <= end)
        mxs, mys = xs[sel], ys[sel]
        if len(mxs) < 3:
            return {"ok": False, "error": "mouse icon unclear"}
        cx, cy = float(mxs.mean()), float(mys.mean())
        k = int(((mxs - cx) ** 2 + (mys - cy) ** 2).argmin())
        return {"ok": True, "pixel": [int(mxs[k]), int(mys[k])]}

    def detect(self, target, cue=None):
        """calibration.detect: fresh grab (becomes the session) + the
        wizard's detector (wizard_propose detection half, app:4388-4421).
        target: capacityBar | cuePrompt."""
        with self.lock:
            arr = self._grab_full()
            self._set_session(arr)
            self._cm = None
            if target == "capacityBar":
                det = self._detect_capacity_px(arr)
            elif target == "cuePrompt":
                det = self._detect_cue_px(arr, cue)
            else:
                raise SensingError("BAD_PARAMS", "unknown detect target",
                                   {"target": target})
            if not det.get("ok"):
                return {"detected": False, "message": det.get("error", "")}
            proposal = {}
            if target == "capacityBar":
                proposal["left"] = det["left"]
                proposal["right"] = det["right"]
                tx, ty = det["right"]
            else:
                proposal["pixel"] = det["pixel"]
                tx, ty = det["pixel"]
            px = arr[ty, tx]
            r, g, b = int(px[2]), int(px[1]), int(px[0])
            proposal["rgb"] = [r, g, b]
            proposal["hex"] = "#%02x%02x%02x" % (r, g, b)
            return {"detected": True, "proposal": proposal}

    # -- one-shot OCR test reads (calibration.testRead) --------------------

    def _ocr_region_lines(self, x0, y0, x1, y1):
        """One fresh grab of a calibrated region -> the ENGINE's Vision
        OCR line records (po._finds_ocr_array; factor 3 = the app's
        test-read upscale). Raises ImportError where Vision is absent --
        callers preserve today's error strings."""
        po = self.po
        np = po.np
        with po._MSS() as sct:
            img = sct.grab({"left": x0, "top": y0,
                            "width": x1 - x0, "height": y1 - y0})
        arr = np.asarray(img)[:, :, :3][:, :, ::-1]      # BGRA -> RGB
        arr = np.ascontiguousarray(arr.astype(np.uint8))
        return po._finds_ocr_array(arr, factor=3)

    def test_read(self, target):
        """calibration.testRead: one-shot OCR through the engine's Vision
        path; result dicts and every fallback string are verbatim
        test_find_read / test_earn_read (app:2546-2653)."""
        if target == "find":
            return self._test_find_read()
        if target == "earnings":
            return self._test_earn_read()
        raise SensingError("BAD_PARAMS", "unknown testRead target",
                           {"target": target})

    def _test_find_read(self):
        cur = self.store.get()
        tl = cur.get("FIND_TL_PIXEL") or [0, 0]
        br = cur.get("FIND_BR_PIXEL") or [0, 0]
        try:
            x0, y0, x1, y1 = int(tl[0]), int(tl[1]), int(br[0]), int(br[1])
        except Exception:
            return {"error": "bad corners"}
        if x1 - x0 < 20 or y1 - y0 < 10:
            return {"error": "region not calibrated (pick both corners)"}
        try:
            lines = self._ocr_region_lines(x0, y0, x1, y1)
            return {"lines": [d["t"] for d in lines]}
        except ImportError as e:
            return {"error": "missing package: %s" % e}
        except Exception as e:
            return {"error": "error: %r" % (e,)}

    def _test_earn_read(self):
        cur = self.store.get()
        out = {}
        for name, tlk, brk in (("money", "MONEY_TL_PIXEL", "MONEY_BR_PIXEL"),
                               ("shards", "SHARDS_TL_PIXEL",
                                "SHARDS_BR_PIXEL")):
            tl, br = cur.get(tlk) or [0, 0], cur.get(brk) or [0, 0]
            try:
                x0, y0, x1, y1 = (int(tl[0]), int(tl[1]),
                                  int(br[0]), int(br[1]))
            except Exception:
                out[name] = "bad corners"
                continue
            if x1 - x0 < 12 or y1 - y0 < 8:
                out[name] = "region not calibrated (pick both corners)"
                continue
            try:
                lines = self._ocr_region_lines(x0, y0, x1, y1)
                seen, best, best_oy = [], None, None
                for d in lines:
                    s = d["t"]
                    seen.append(s)
                    if "+" in s:
                        continue
                    digs = "".join(c for c in s if c.isdigit())
                    if not digs:
                        continue
                    oy = float(d.get("oy", 1.0 - d["cy"]))
                    if best is None or oy < best_oy:
                        best, best_oy = int(digs), oy
                if best is not None:
                    out[name] = "{:,}".format(best)
                elif seen:
                    out[name] = ("saw text but no number: "
                                 + " | ".join(seen[:3]))
                else:
                    out[name] = "no text found -- widen the region"
            except ImportError as e:
                out[name] = "missing package: %s" % e
            except Exception as e:
                out[name] = "error: %r" % (e,)
        return out

    # -- cue masks (calibration.cueMask) -----------------------------------

    def cue_status(self):
        """op:status -- verbatim cue_mask_status (app:3407-3418)."""
        cur = self.store.get()
        masks = cur.get("CUE_MASKS") or {}
        out = {"advanced": bool(cur.get("ADVANCED_CUES")),
               "masks_only": bool(cur.get("CUE_MASKS_ONLY")), "cues": {}}
        for cue in ("PAN", "SHAKE", "DEPOSIT"):
            m = masks.get(cue) or {}
            out["cues"][cue] = {"has": bool(m.get("bits")),
                                "px": int(m.get("px", 0)),
                                "w": int(m.get("w", 0)),
                                "h": int(m.get("h", 0)),
                                "preview": m.get("preview", "")}
        return out

    def cue_clear(self, cue):
        """op:clear -- verbatim clear_cue_mask (app:3420-3431), through
        the settings writer."""
        cur = self.store.get()
        masks = cur.get("CUE_MASKS") or {}
        if cue in masks:
            del masks[cue]
            cur["CUE_MASKS"] = masks
            try:
                self.store.write(cur, ["CUE_MASKS"], "cmd")
            except OSError as e:
                return {"ok": False, "error": str(e)}
        return {"ok": True}

    def cue_white_grid(self, cx, cy, thresh):
        """The interactive editor's white-grid (verbatim _cm_white_grid,
        app:4135-4156) over the SESSION frame around (cx, cy)."""
        np = self.po.np
        arr = self._shot
        H, W = arr.shape[0], arr.shape[1]
        rect = self.po.find_roblox_window()
        ww = rect["w"] if rect.get("found") else W
        wh = rect["h"] if rect.get("found") else H
        gw = max(110, int(0.24 * ww)); gh = max(30, int(0.075 * wh))
        left = max(0, cx - gw // 2); top = max(0, cy - gh // 2)
        right = min(W, left + gw); bottom = min(H, top + gh)
        box = arr[top:bottom, left:right, :3].astype(np.int16)   # BGR
        b, g, r = box[:, :, 0], box[:, :, 1], box[:, :, 2]
        lo = np.minimum(np.minimum(r, g), b)
        hi = np.maximum(np.maximum(r, g), b)
        white = (lo >= thresh) & ((hi - lo) <= 70)
        return white, {"left": left, "top": top,
                       "w": box.shape[1], "h": box.shape[0]}, box

    def _cm_render(self):
        """Verbatim Api._cm_render (app:4176-4190): dimmed box, GREEN
        where the mask is set, zoomed."""
        np = self.po.np
        cm = self._cm
        box = cm["rgb"].clip(0, 255).astype(np.uint8)            # BGR
        prev = (box.astype(np.float32) * 0.28).astype(np.uint8)
        if cm["mask"] is not None:
            prev[cm["mask"]] = np.array([40, 235, 40], np.uint8)
        h, w = prev.shape[0], prev.shape[1]
        zoom = max(3, min(12, 1000 // max(1, w)))
        big = prev.repeat(zoom, 0).repeat(zoom, 1)
        rgb = np.ascontiguousarray(big[:, :, ::-1])              # BGR->RGB
        return _png_data_url(rgb)

    def cue_begin(self, cue, thresh=None, at=None):
        """op:beginCapture -- enter the cue-mask EDITOR: white-grid on
        the session frame (fresh grab when none / wire path), centered
        at ``at`` (the host's locate click, in-process) or at the SAVED
        cue pixel (the wire path, which has no click). <12 white px ->
        {detected:false, message} (capture_cue_mask's message,
        app:3470-3472)."""
        if cue not in CUE_PIXEL_KEY:
            return {"detected": False, "message": "Unknown cue."}
        with self.lock:
            if at is None or self._shot is None:
                self._set_session(self._grab_full())
            if at is not None:
                cx, cy = int(at[0]), int(at[1])
            else:
                cur = self.store.get()
                px = cur.get(CUE_PIXEL_KEY[cue])
                if not (isinstance(px, (list, tuple)) and len(px) == 2):
                    return {"detected": False,
                            "message": "Calibrate the %s pixel first "
                                       "(click it on the Calibrate page), "
                                       "then capture." % cue}
                cx, cy = int(px[0]), int(px[1])
            t = int(thresh) if thresh else 160
            white, boxrec, rgbbox = self.cue_white_grid(cx, cy, t)
            if white is None or int(white.sum()) < 12:
                self._cm = None
                return {"detected": False,
                        "message": "No white cue text found there. Make "
                                   "sure the %s cue is visible on screen, "
                                   "then capture." % cue}
            self._cm = {"cue": cue, "thresh": t, "white": white,
                        "mask": white.copy(), "box": boxrec, "rgb": rgbbox}
            return {"cueEdit": True, "image": self._cm_render(),
                    "px": int(self._cm["mask"].sum())}

    def cue_toggle(self, fx, fy):
        """op:toggle -- flood-fill letter toggle (verbatim cue_toggle +
        _cm_floodfill, app:4158-4210) on the editor grid."""
        np = self.po.np
        with self.lock:
            if self._cm is None or self._cm["white"] is None:
                return {"error": "not editing"}
            cm = self._cm
            h, w = cm["white"].shape
            px = max(0, min(w - 1, int(round(float(fx) * w))))
            py = max(0, min(h - 1, int(round(float(fy) * h))))
            comp = self._cm_floodfill(py, px)
            if comp is not None:
                if cm["mask"][comp].mean() > 0.5:
                    cm["mask"][comp] = False       # was in -> remove letter
                else:
                    cm["mask"][comp] = True        # was out -> add letter
            return {"image": self._cm_render(),
                    "px": int(cm["mask"].sum())}

    def _cm_floodfill(self, py, px):
        """Verbatim Api._cm_floodfill (app:4158-4174)."""
        np = self.po.np
        white = self._cm["white"]
        if white is None or not white[py, px]:
            return None
        h, w = white.shape
        comp = np.zeros((h, w), bool)
        stack = [(py, px)]
        while stack:
            y, x = stack.pop()
            if (y < 0 or x < 0 or y >= h or x >= w or comp[y, x]
                    or not white[y, x]):
                continue
            comp[y, x] = True
            stack.append((y + 1, x)); stack.append((y - 1, x))
            stack.append((y, x + 1)); stack.append((y, x - 1))
        return comp

    def cue_edit_state(self):
        """Current editor rendering ({image, px}) or None -- used by
        in-process hosts to repaint the overlay (overlay_image)."""
        with self.lock:
            if self._cm is None or self._cm.get("mask") is None:
                return None
            return {"image": self._cm_render(),
                    "px": int(self._cm["mask"].sum())}

    def cue_reset(self):
        """op:reset -- back to the un-toggled grid -> {image, px}."""
        with self.lock:
            if self._cm is None:
                return {"error": "not editing"}
            self._cm["mask"] = self._cm["white"].copy()
            return {"image": self._cm_render(),
                    "px": int(self._cm["mask"].sum())}

    def cue_save(self):
        """op:save -- persist the editor mask (verbatim _cm_save,
        app:4217-4256) through the settings writer -> {px, preview}."""
        np = self.po.np
        with self.lock:
            if self._cm is None:
                return {"error": "not editing"}
            cm = self._cm
            mask = cm["mask"]
            if mask is None or int(mask.sum()) < 8:
                return {"error": "mask too small"}
            rect = self.po.find_roblox_window()
            if not rect.get("found"):
                return {"error": rect.get("error",
                                          "Roblox window not found.")}
            ys, xs = np.where(mask)
            x0, x1 = int(xs.min()), int(xs.max()) + 1
            y0, y1 = int(ys.min()), int(ys.max()) + 1
            tight = mask[y0:y1, x0:x1]
            mw, mh = int(x1 - x0), int(y1 - y0)
            bits = np.packbits(tight.astype(np.uint8).ravel())
            b64 = base64.b64encode(bits.tobytes()).decode("ascii")
            box = cm["box"]
            wx, wy, ww, wh = rect["x"], rect["y"], rect["w"], rect["h"]
            abs_left, abs_top = box["left"] + x0, box["top"] + y0
            ratio = [round((abs_left - wx) / float(ww), 5),
                     round((abs_top - wy) / float(wh), 5),
                     round(mw / float(ww), 6), round(mh / float(wh), 6)]
            rgbbox = cm["rgb"][y0:y1, x0:x1].clip(0, 255).astype(np.uint8)
            prev = (rgbbox.astype(np.float32) * 0.28).astype(np.uint8)
            prev[tight] = np.array([40, 235, 40], np.uint8)
            zoom = max(3, min(10, 360 // max(1, mw)))
            big = prev.repeat(zoom, 0).repeat(zoom, 1)
            rgb = np.ascontiguousarray(big[:, :, ::-1])
            preview = _png_data_url(rgb)
            cur = self.store.get()
            masks = cur.get("CUE_MASKS") or {}
            masks[cm["cue"]] = {"ratio": ratio, "w": mw, "h": mh,
                                "bits": b64, "px": int(tight.sum()),
                                "preview": preview}
            cur["CUE_MASKS"] = masks
            cur["CALIB_WINDOW_RECT"] = [wx, wy, ww, wh]
            self.store.write(cur, ["CUE_MASKS", "CALIB_WINDOW_RECT"], "cmd")
            return {"px": int(tight.sum()), "preview": preview}

    def capture_cue_mask(self, cue, thresh=None):
        """The one-shot auto capture-and-save path (verbatim
        capture_cue_mask, app:3433-3512): grab a generous box around the
        SAVED cue pixel, auto-select every white pixel, persist mask +
        preview immediately. Kept as its own shipped surface; the wire
        reaches masks via the beginCapture/save editor ops instead."""
        po = self.po
        np = po.np
        pk = CUE_PIXEL_KEY.get(cue)
        if not pk:
            return {"ok": False, "error": "Unknown cue."}
        cur = self.store.get()
        px = cur.get(pk)
        if not (isinstance(px, (list, tuple)) and len(px) == 2):
            return {"ok": False,
                    "error": "Calibrate the %s pixel first (click it "
                             "on the Calibrate page), then capture." % cue}
        rect = po.find_roblox_window()
        if not rect.get("found"):
            return {"ok": False,
                    "error": rect.get("error", "Roblox window not found.")}
        x, y, w, h = rect["x"], rect["y"], rect["w"], rect["h"]
        gw = max(90, int(0.20 * w))          # wide enough for the cue word
        gh = max(26, int(0.06 * h))
        cx, cy = int(px[0]), int(px[1])
        left, top = cx - gw // 2, cy - gh // 2
        try:
            with po._MSS() as sct:
                img = np.asarray(sct.grab(
                    {"left": left, "top": top, "width": gw,
                     "height": gh}))[:, :, :3].astype(np.int16)
        except Exception as e:
            return {"ok": False, "error": "Capture failed: %s" % e}
        b, g, r = img[:, :, 0], img[:, :, 1], img[:, :, 2]
        lo = np.minimum(np.minimum(r, g), b)
        hi = np.maximum(np.maximum(r, g), b)
        tmin = int(thresh) if thresh else int(cur.get("CUE_WHITE_MIN", 160))
        white = (lo >= tmin) & ((hi - lo) <= 70)
        ys, xs = np.where(white)
        if len(xs) < 12:
            return {"ok": False,
                    "error": "No white cue text found there. Make sure "
                             "the %s cue is visible on screen, then "
                             "capture." % cue}
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        x0 = max(0, x0 - 1); y0 = max(0, y0 - 1)
        x1 = min(gw, x1 + 1); y1 = min(gh, y1 + 1)
        mask = white[y0:y1, x0:x1]
        mw, mh = int(x1 - x0), int(y1 - y0)
        bits = np.packbits(mask.astype(np.uint8).ravel())
        b64 = base64.b64encode(bits.tobytes()).decode("ascii")
        abs_left, abs_top = left + x0, top + y0
        ratio = [round((abs_left - x) / float(w), 5),
                 round((abs_top - y) / float(h), 5),
                 round(mw / float(w), 6), round(mh / float(h), 6)]
        masks = cur.get("CUE_MASKS") or {}
        masks[cue] = {"ratio": ratio, "w": mw, "h": mh, "bits": b64,
                      "px": int(mask.sum())}
        cur["CUE_MASKS"] = masks
        cur["CALIB_WINDOW_RECT"] = [x, y, w, h]
        try:
            self.store.write(cur, ["CUE_MASKS", "CALIB_WINDOW_RECT"], "cmd")
        except OSError as e:
            return {"ok": False, "error": str(e)}
        # zoomed preview: exact captured region, background DIMMED, every
        # selected pixel painted bright green
        preview = ""
        try:
            tight = img[y0:y1, x0:x1].clip(0, 255).astype(np.uint8)  # BGR
            prev = (tight.astype(np.float32) * 0.30).astype(np.uint8)
            prev[mask] = np.array([40, 235, 40], np.uint8)           # green
            zoom = max(2, min(9, 380 // max(1, mw)))
            big = prev.repeat(zoom, axis=0).repeat(zoom, axis=1)
            rgb = np.ascontiguousarray(big[:, :, ::-1])              # ->RGB
            preview = _png_data_url(rgb)
        except Exception:
            preview = ""
        return {"ok": True, "cue": cue, "px": int(mask.sum()),
                "w": mw, "h": mh, "preview": preview}

    # -- health / auto / savePixels ----------------------------------------

    def health(self):
        """calibration.health -- verbatim calibration_health
        (app:2856-2881): stored CALIB_WINDOW_RECT vs live rect, +-4 px,
        exact message composition."""
        cur = self.store.get()
        cal = cur.get("CALIB_WINDOW_RECT")
        if not (isinstance(cal, (list, tuple)) and len(cal) == 4
                and cal[2] and cal[3]):
            return {"ok": True, "reason": ""}
        rect = self.po.find_roblox_window()
        if not rect.get("found"):
            return {"ok": True, "reason": ""}
        cw, ch = int(cal[2]), int(cal[3])
        if abs(rect["w"] - cw) <= 4 and abs(rect["h"] - ch) <= 4:
            return {"ok": True, "reason": ""}
        adv = bool(cur.get("ADVANCED_CUES"))
        auto = bool(cur.get("AUTO_CALIBRATE", True))
        parts = ["The Roblox window is %d×%d now but you calibrated "
                 "at %d×%d." % (rect["w"], rect["h"], cw, ch)]
        if adv:
            parts.append("Advanced cue masks are OFF until you re-capture "
                         "them (Calibrate → Advanced cue matching).")
        if not auto:
            parts.append("Re-calibrate the pixels so detection lines up.")
        elif not adv:
            parts.append("Pixels auto-adapt, but re-check calibration if "
                         "detection seems off.")
        return {"ok": False, "reason": " ".join(parts)}

    def auto(self, apply=False):
        """calibration.auto -- ratio-profile placement from the live
        window (stored PIXEL_RATIOS, else the baked default profile) with
        the engine's CAP_BAR_WIDTH derivation (apply_auto_calibrate math,
        engine:810-837). apply=True persists the placed pixels through
        the settings writer; never flips AUTO_CALIBRATE (that force is
        manual-save semantics, savePixels)."""
        rect = self.po.find_roblox_window()
        if not rect.get("found"):
            return {"placed": False, "pixels": {}, "window": None,
                    "count": 0}
        cur = self.store.get()
        ratios = cur.get("PIXEL_RATIOS") or PIXEL_RATIOS_DEFAULT
        if not ratios:
            return {"placed": False, "pixels": {},
                    "window": {"x": rect["x"], "y": rect["y"],
                               "w": rect["w"], "h": rect["h"]},
                    "count": 0}
        x, y, w, h = rect["x"], rect["y"], rect["w"], rect["h"]
        pixels = {}
        for key, fr in ratios.items():
            if not (isinstance(fr, (list, tuple)) and len(fr) == 2):
                continue
            pixels[key] = [int(round(x + fr[0] * w)),
                           int(round(y + fr[1] * h))]
        writes = {k: list(v) for k, v in pixels.items()}
        if "CAP_FULL_PIXEL" in ratios and "CAP_LEFT_PIXEL" in ratios:
            bw = int(round((ratios["CAP_FULL_PIXEL"][0]
                            - ratios["CAP_LEFT_PIXEL"][0]) * w))
            if bw > 20:
                writes["CAP_BAR_WIDTH"] = bw
        if apply:
            cur.update(writes)
            self.store.write(cur, sorted(writes), "cmd")
        return {"placed": True, "pixels": pixels,
                "window": {"x": x, "y": y, "w": w, "h": h},
                "count": len(pixels)}

    def save_pixels(self, pixels, colors=None, fr=None, ratios=None,
                    window_rect=None, cap_bar_width_fallback=None,
                    derive_from_window=None):
        """calibration.savePixels -- THE semantic calibration write
        (verbatim save_pixels app:2085-2157 + the import-path derivations
        app:2219-2230). Two shipped modes, both preserved exactly:

        * interactive save (no explicit ratios/windowRect): records the
          live window rect + origin, re-derives PIXEL_RATIOS from it,
          and forces AUTO_CALIBRATE=False / WINDOW_RELATIVE=False (the
          'your screen is now authoritative' rule);
        * import (derive_from_window=False, or explicit ratios/windowRect
          passed): adopts them verbatim and does NOT touch the
          auto-calibrate flags -- import_calibration's shipped semantics
          (a pixels-only import stays an import: the host passes
          derive_from_window=False explicitly; the wire infers it from
          the presence of ratios/windowRect per the protocol)."""
        if derive_from_window is None:
            derive_from_window = ratios is None and window_rect is None
        cur = self.store.get()
        changed = set()
        for key in PIXEL_KEYS:
            if key != "CAP_LEFT_PIXEL" and key in pixels:
                cur[key] = [int(pixels[key][0]), int(pixels[key][1])]
                changed.add(key)
        if "CAP_LEFT_PIXEL" in pixels:
            cur["CAP_LEFT_PIXEL"] = [int(pixels["CAP_LEFT_PIXEL"][0]),
                                     int(pixels["CAP_LEFT_PIXEL"][1])]
            changed.add("CAP_LEFT_PIXEL")
        if "CAP_FULL_PIXEL" in cur and "CAP_LEFT_PIXEL" in cur:
            w = int(cur["CAP_FULL_PIXEL"][0] - cur["CAP_LEFT_PIXEL"][0])
            if w > 20:
                cur["CAP_BAR_WIDTH"] = w
                changed.add("CAP_BAR_WIDTH")
        elif cap_bar_width_fallback:
            cur["CAP_BAR_WIDTH"] = int(cap_bar_width_fallback)
            changed.add("CAP_BAR_WIDTH")
        if isinstance(ratios, dict):
            cur["PIXEL_RATIOS"] = ratios
            changed.add("PIXEL_RATIOS")
        if isinstance(window_rect, (list, tuple)):
            cur["CALIB_WINDOW_RECT"] = list(window_rect)
            changed.add("CALIB_WINDOW_RECT")
        if derive_from_window:
            # interactive path: express every calibrated pixel as a
            # fraction of the LIVE game window (what lets Auto-calibrate
            # place them for anyone, at any window size/position)
            rect = self.po.find_roblox_window()
            if rect.get("found"):
                cur["CALIB_WINDOW_ORIGIN"] = [rect["x"], rect["y"]]
                cur["CALIB_WINDOW_RECT"] = [rect["x"], rect["y"],
                                            rect["w"], rect["h"]]
                changed.update(("CALIB_WINDOW_ORIGIN",
                                "CALIB_WINDOW_RECT"))
                rat = cur.get("PIXEL_RATIOS", {}) or {}
                for key in PIXEL_KEYS:
                    if key in cur and isinstance(cur[key], (list, tuple)):
                        fx = (cur[key][0] - rect["x"]) / float(rect["w"])
                        fy = (cur[key][1] - rect["y"]) / float(rect["h"])
                        rat[key] = [round(fx, 5), round(fy, 5)]
                cur["PIXEL_RATIOS"] = rat
                changed.add("PIXEL_RATIOS")
            else:
                r = self.po.find_roblox_window()
                cur["CALIB_WINDOW_ORIGIN"] = ([r["x"], r["y"]]
                                              if r.get("found") else [0, 0])
                changed.add("CALIB_WINDOW_ORIGIN")
        if colors:
            cur["PIXEL_COLORS"] = {k: str(v) for k, v in colors.items()}
            changed.add("PIXEL_COLORS")
        if fr:
            for k2, cast in (("FR_OPEN_PIXEL", "xy"), ("FR_SCAN_X", "int"),
                             ("FR_TEXT_RGB", "rgb"), ("SR_TEXT_RGB", "rgb"),
                             ("AUTOPAN_BTN_PIXEL", "xy"),
                             ("AUTOPAN_ON_RGB", "rgb"),
                             ("AUTOPAN_OFF_RGB", "rgb"),
                             ("FR_BOX_TOP", "int"), ("FR_BOX_BOTTOM", "int"),
                             ("FR_HOME_PIXEL", "xy")):
                v = fr.get(k2)
                if v is None or (cast != "int" and not v):
                    continue
                if cast == "xy":
                    cur[k2] = [int(v[0]), int(v[1])]
                elif cast == "rgb":
                    cur[k2] = [int(c) for c in v[:3]]
                else:
                    cur[k2] = int(v)
                changed.add(k2)
        if derive_from_window:
            # You have now calibrated for YOUR screen, so the saved pixels
            # are authoritative -- stop the engine re-deriving them from
            # the built-in ratio profile at startup.
            cur["AUTO_CALIBRATE"] = False
            cur["WINDOW_RELATIVE"] = False
            changed.update(("AUTO_CALIBRATE", "WINDOW_RELATIVE"))
        self.store.write(cur, sorted(changed), "cmd")
        return {"saved": sorted(changed)}
