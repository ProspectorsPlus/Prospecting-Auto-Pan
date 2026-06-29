#!/usr/bin/env python3
"""
prospecting_app.py -- native desktop app (its own window) for the Prospecting
macro, built on pywebview. Tabbed settings, Start/Stop + live trace, a live
calibrate readout, a relic timer editor, saved builds, and ? help on every field.

First time:   pip3 install pywebview --break-system-packages
Run (or double-click "Prospectors Plus.app"):   python3 prospecting_app.py

If pywebview isn't installed it falls back to the browser settings UI.
"""

import os
import sys
import json
import signal
import threading
import subprocess
import webbrowser
import urllib.request
import urllib.error
import hashlib

# ---- version + update channel (compared to the website's version.json) -------
# >>> EDIT THESE THREE LINES to point at your website <<<
VERSION             = "2.2.3"
UPDATE_MANIFEST_URL = "https://prospectorsplus.github.io/Prospecting-Auto-Pan/version.json"
DOWNLOAD_PAGE_URL   = "https://prospectorsplus.github.io/Prospecting-Auto-Pan/"
ACCESS_CODES_URL    = "https://prospectorsplus.github.io/Prospecting-Auto-Pan/codes.json"
INSTALLER_URL       = "https://github.com/ProspectorsPlus/Prospecting-Auto-Pan/releases/latest/download/ProspectorsPlusSetup.exe"

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "prospecting_config.json")
BUILDS_FILE = os.path.join(HERE, "prospecting_builds.json")
MACRO_FILE = os.path.join(HERE, "prospecting_old.py")


def _ver_tuple(s):
    out = []
    for part in str(s).strip().split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out) or (0,)

# reuse the settings schema + help from the browser UI so they never drift apart.
# Tolerant import: a missing name (e.g. an older prospecting_ui.py without the
# new pixel schema) must NOT blank everything -- load each piece independently.
try:
    import prospecting_ui as _ui
    SECTIONS = getattr(_ui, "SECTIONS", [])
    DEFAULTS = getattr(_ui, "DEFAULTS", {})
    TYPES = getattr(_ui, "TYPES", {})
    PRESET_V1 = getattr(_ui, "PRESET_V1", {})
    PRESET_V2 = getattr(_ui, "PRESET_V2", {})
    SECTION_HINT = getattr(_ui, "SECTION_HINT", {})
    TAB_ICON = getattr(_ui, "TAB_ICON", {})
    HELP = getattr(_ui, "HELP", {})
    PIXEL_FIELDS = getattr(_ui, "PIXEL_FIELDS", [])
    PIXEL_DEFAULTS = getattr(_ui, "PIXEL_DEFAULTS", {})
except Exception:
    import traceback
    traceback.print_exc()
    SECTIONS = []
    DEFAULTS = TYPES = {}
    PRESET_V1 = PRESET_V2 = {}
    SECTION_HINT = TAB_ICON = HELP = {}
    PIXEL_FIELDS = []
    PIXEL_DEFAULTS = {}

# Built-in calibration profile: each pixel as a fraction of the Roblox game
# window. When populated, users Auto-calibrate with zero clicking. Seeded by
# calibrating once with Roblox open, then baking the result into the config.
PIXEL_RATIOS_DEFAULT = {
    "CAP_FULL_PIXEL": [0.62333, 0.78657], "CAP_LEFT_PIXEL": [0.37667, 0.78749],
    "DEPOSIT_PIX": [0.42667, 0.87121], "PAN_PIX": [0.46889, 0.86845],
    "SHAKE_PIX": [0.46167, 0.87029], "DIG_TRIGGER_PIXEL": [0.60111, 0.4517],
}

MAX_RELIC_ROWS = 4
DEFAULT_RELICS = [
    {"name": "Solar Magnifier", "minutes": 10, "slot": 5, "clicks": 2},
    {"name": "Infernal Idol",   "minutes": 10, "slot": 6, "clicks": 2},
]


def _read_json(path, fallback):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return fallback


def load_saved():
    return _read_json(CONFIG_FILE, {})


def _coerce(t, v):
    if t == "bool":
        return bool(v)
    if t == "str":
        return str(v)
    try:
        return int(v)
    except (ValueError, TypeError):
        return 0


def _roblox_rect():
    """Find the Roblox GAME window and return its area in PHYSICAL pixels (so it
    matches the pixels the calibrator captures). Returns {found:True,x,y,w,h,title}
    or {found:False,error:"..."}. macOS / Quartz. Scans on-screen windows for an
    owner named like 'Roblox' (not Studio) and picks the largest."""
    try:
        import Quartz
        import mss
        opt = (Quartz.kCGWindowListOptionOnScreenOnly
               | Quartz.kCGWindowListExcludeDesktopElements)
        wins = Quartz.CGWindowListCopyWindowInfo(opt, Quartz.kCGNullWindowID)
        with mss.mss() as sct:
            mainw = sct.monitors[1]["width"]
        b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        scale = mainw / b.size.width if b.size.width else 1.0
        best, area, title = None, 0, ""
        for w in wins or []:
            owner = str(w.get("kCGWindowOwnerName", ""))
            if "roblox" in owner.lower() and "studio" not in owner.lower():
                bb = w.get("kCGWindowBounds", {})
                ww, hh = bb.get("Width", 0), bb.get("Height", 0)
                if ww * hh > area and ww >= 320 and hh >= 240:
                    area, best, title = ww * hh, bb, owner
        if best:
            return {"found": True,
                    "x": int(best["X"] * scale), "y": int(best["Y"] * scale),
                    "w": int(best["Width"] * scale), "h": int(best["Height"] * scale),
                    "title": title}
        return {"found": False,
                "error": "Roblox window not found. Open Prospecting in Roblox "
                         "(not minimized) and try again."}
    except Exception as e:
        return {"found": False, "error": "Detection failed: %s" % e}


def _window_origin():
    """Roblox client top-left [x,y] in physical px, or [0,0]. (Back-compat.)"""
    r = _roblox_rect()
    return [r["x"], r["y"]] if r.get("found") else [0, 0]


# ============================================================================
# JS <-> Python bridge
# ============================================================================
class Api:
    def __init__(self):
        self.proc = None
        self._sct = None
        self._scale = 1.0
        self._last_stats = None   # latest stats from the macro (for history)
        self._run_active = False  # a run is in progress (write history once)
        self._macro_status = "off"  # off/idle/running/paused/safe-pause/recovering/stopped

    # ---- settings ----
    def get_state(self):
        saved = load_saved()
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in saved.items() if k in DEFAULTS})
        relics = saved.get("RELICS", DEFAULT_RELICS)
        pixels = {}
        for key in PIXEL_DEFAULTS:
            pixels[key] = list(saved.get(key, PIXEL_DEFAULTS[key]))
        fr = {"FR_OPEN_PIXEL": list(saved.get("FR_OPEN_PIXEL", [0, 0])),
              "FR_SCAN_X": int(saved.get("FR_SCAN_X", 0)),
              "FR_TEXT_RGB": list(saved.get("FR_TEXT_RGB", [232, 120, 200])),
              "FR_BOX_TOP": int(saved.get("FR_BOX_TOP", 0)),
              "FR_BOX_BOTTOM": int(saved.get("FR_BOX_BOTTOM", 0)),
              "FR_HOME_PIXEL": list(saved.get("FR_HOME_PIXEL", [0, 0]))}
        hk = {k: saved.get(k, _HK_DEFAULTS[k]) for k in _HK_DEFAULTS}
        return {"values": merged, "running": self.proc is not None, "fr": fr, "hotkeys": hk,
                "autobuild": saved.get("AUTOBUILD", {}),
                "v1": PRESET_V1, "v2": PRESET_V2, "defaults": DEFAULTS,
                "relics": relics, "relics_enabled": bool(saved.get("RELICS_ENABLED", False)),
                "builds": self.list_builds(), "pixels": pixels,
                "colors": saved.get("PIXEL_COLORS", {})}

    # ---- updates ----
    def app_version(self):
        return VERSION

    def check_update(self):
        try:
            req = urllib.request.Request(UPDATE_MANIFEST_URL,
                                         headers={"User-Agent": "ProspectorsPlus"})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read().decode("utf-8"))
            latest = str(data.get("version", "")).strip()
            if latest and _ver_tuple(latest) > _ver_tuple(VERSION):
                return {"update": True, "version": latest, "current": VERSION,
                        "url": data.get("url") or DOWNLOAD_PAGE_URL,
                        "installer": data.get("installer") or INSTALLER_URL,
                        "critical": bool(data.get("critical")),
                        "notes": data.get("notes", "")}
            return {"update": False, "version": VERSION}
        except Exception as e:
            return {"update": False, "error": str(e)}

    def open_external(self, url):
        try:
            webbrowser.open(url or DOWNLOAD_PAGE_URL)
            return True
        except Exception:
            return False

    def do_update(self, url=None):
        """One-click update for the installed Windows app: download the latest
        installer and run it silently (it upgrades in place over the same install
        and relaunches), then quit so the files can be replaced. On macOS / when
        running from source we can't self-install, so we open the download page."""
        target = url or INSTALLER_URL
        frozen = bool(globals().get("FROZEN", False))
        if not (frozen and sys.platform.startswith("win")):
            try:
                webbrowser.open(DOWNLOAD_PAGE_URL)
            except Exception:
                pass
            return {"ok": False, "manual": True}
        try:
            import tempfile
            tmp = os.path.join(tempfile.gettempdir(), "ProspectorsPlusSetup.exe")
            req = urllib.request.Request(
                target, headers={"User-Agent": "ProspectorsPlus"})
            with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
            # /SILENT shows a small progress bar; the installer's [Run] entry
            # relaunches the app afterwards. We exit so the .exe can be replaced.
            subprocess.Popen([tmp, "/SILENT", "/NORESTART", "/SUPPRESSMSGBOXES"],
                             close_fds=True)

            def _bye():
                import time as _t
                _t.sleep(1.3)
                try:
                    if _window is not None:
                        _window.destroy()
                except Exception:
                    pass
                os._exit(0)
            threading.Thread(target=_bye, daemon=True).start()
            return {"ok": True}
        except Exception as e:
            try:
                webbrowser.open(DOWNLOAD_PAGE_URL)
            except Exception:
                pass
            return {"ok": False, "error": str(e)}

    # ---- access gate ----
    def access_state(self):
        """Has this PC already unlocked with a valid code?"""
        return {"unlocked": bool(load_saved().get("ACCESS_OK"))}

    def verify_access(self, code):
        """Check an access code against the GitHub-hosted hashed list. On success
        remember it on this PC so we don't ask again."""
        norm = "".join((code or "").split()).upper()
        if not norm:
            return {"ok": False, "error": "Enter your access code."}
        h = hashlib.sha256(norm.encode("utf-8")).hexdigest()
        try:
            import time as _t
            url = ACCESS_CODES_URL + ("&" if "?" in ACCESS_CODES_URL else "?") \
                  + "t=" + str(int(_t.time()))   # bust the GitHub Pages CDN cache
            req = urllib.request.Request(url, headers={
                "User-Agent": "ProspectorsPlus", "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode("utf-8"))
            hashes = set(data.get("hashes") or [])
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {"ok": False, "error": "The access list isn't published "
                        "yet. Ask the owner to publish codes, then try again."}
            return {"ok": False, "error": "Code server error (%s)." % e.code}
        except Exception:
            return {"ok": False, "error": "Couldn't reach the code server. "
                    "Check your internet and try again."}
        if h in hashes:
            cur = load_saved()
            cur["ACCESS_OK"] = True
            cur["ACCESS_HASH"] = h
            try:
                with open(CONFIG_FILE, "w") as f:
                    json.dump(cur, f, indent=2)
            except OSError:
                pass
            return {"ok": True}
        return {"ok": False,
                "error": "That code isn't valid. Double-check and try again."}

    def save_pixels(self, pixels, colors=None, fr=None):
        """Save calibrated pixel coordinates; derive CAP_BAR_WIDTH from the bar
        ends. Only the real macro pixel keys are written (CAP_LEFT_PIXEL is a
        helper used just to compute the width)."""
        cur = load_saved()
        for key in ("CAP_FULL_PIXEL", "DEPOSIT_PIX", "PAN_PIX", "SHAKE_PIX",
                    "DIG_TRIGGER_PIXEL"):
            if key in pixels:
                cur[key] = [int(pixels[key][0]), int(pixels[key][1])]
        # remember the left end (for the UI) and compute the bar width
        if "CAP_LEFT_PIXEL" in pixels:
            cur["CAP_LEFT_PIXEL"] = [int(pixels["CAP_LEFT_PIXEL"][0]),
                                     int(pixels["CAP_LEFT_PIXEL"][1])]
        if "CAP_FULL_PIXEL" in cur and "CAP_LEFT_PIXEL" in cur:
            w = int(cur["CAP_FULL_PIXEL"][0] - cur["CAP_LEFT_PIXEL"][0])
            if w > 20:
                cur["CAP_BAR_WIDTH"] = w
        # If Roblox is open, record the window rect AND express every calibrated
        # pixel as a fraction of the game window so Auto-calibrate can replace it.
        rect = _roblox_rect()
        if rect.get("found"):
            cur["CALIB_WINDOW_ORIGIN"] = [rect["x"], rect["y"]]
            cur["CALIB_WINDOW_RECT"] = [rect["x"], rect["y"], rect["w"], rect["h"]]
            ratios = cur.get("PIXEL_RATIOS", {}) or {}
            for key in ("CAP_FULL_PIXEL", "CAP_LEFT_PIXEL", "DEPOSIT_PIX",
                        "PAN_PIX", "SHAKE_PIX", "DIG_TRIGGER_PIXEL"):
                if key in cur and isinstance(cur[key], (list, tuple)):
                    fx = (cur[key][0] - rect["x"]) / float(rect["w"])
                    fy = (cur[key][1] - rect["y"]) / float(rect["h"])
                    ratios[key] = [round(fx, 5), round(fy, 5)]
            cur["PIXEL_RATIOS"] = ratios
        else:
            cur["CALIB_WINDOW_ORIGIN"] = _window_origin()
        if colors:
            cur["PIXEL_COLORS"] = {k: str(v) for k, v in colors.items()}
        if fr:
            if fr.get("FR_OPEN_PIXEL"):
                cur["FR_OPEN_PIXEL"] = [int(fr["FR_OPEN_PIXEL"][0]),
                                        int(fr["FR_OPEN_PIXEL"][1])]
            if fr.get("FR_SCAN_X") is not None:
                cur["FR_SCAN_X"] = int(fr["FR_SCAN_X"])
            if fr.get("FR_TEXT_RGB"):
                cur["FR_TEXT_RGB"] = [int(c) for c in fr["FR_TEXT_RGB"][:3]]
            if fr.get("FR_BOX_TOP") is not None:
                cur["FR_BOX_TOP"] = int(fr["FR_BOX_TOP"])
            if fr.get("FR_BOX_BOTTOM") is not None:
                cur["FR_BOX_BOTTOM"] = int(fr["FR_BOX_BOTTOM"])
            if fr.get("FR_HOME_PIXEL"):
                cur["FR_HOME_PIXEL"] = [int(fr["FR_HOME_PIXEL"][0]),
                                        int(fr["FR_HOME_PIXEL"][1])]
        # You have now calibrated for YOUR screen, so the saved pixels are
        # authoritative -- stop the macro re-deriving them from the built-in
        # ratio profile at startup (that profile is the dev's screen and is what
        # made digs miss after a good calibration).
        cur["AUTO_CALIBRATE"] = False
        cur["WINDOW_RELATIVE"] = False
        with open(CONFIG_FILE, "w") as f:
            json.dump(cur, f, indent=2)
        return "saved"

    # ---- calibration export / import ----
    def _calibration_dict(self):
        cur = load_saved()
        keys = ["CAP_FULL_PIXEL", "CAP_LEFT_PIXEL", "DEPOSIT_PIX", "PAN_PIX",
                "SHAKE_PIX", "DIG_TRIGGER_PIXEL"]
        return {"app": "Prospectors Plus", "kind": "calibration",
                "version": VERSION,
                "pixels": {k: cur[k] for k in keys if k in cur},
                "PIXEL_RATIOS": cur.get("PIXEL_RATIOS", {}),
                "CALIB_WINDOW_RECT": cur.get("CALIB_WINDOW_RECT"),
                "CAP_BAR_WIDTH": cur.get("CAP_BAR_WIDTH"),
                "PIXEL_COLORS": cur.get("PIXEL_COLORS", {})}

    def export_calibration(self):
        """Write the calibration to a real .json file via the OS save dialog
        (blob downloads don't work inside the desktop webview). Falls back to a
        file next to the app data if no dialog is available."""
        data = self._calibration_dict()
        try:
            import webview
        except Exception:
            webview = None
        try:
            path = None
            if _window is not None and webview is not None:
                res = _window.create_file_dialog(
                    webview.SAVE_DIALOG,
                    save_filename="prospectors_calibration.json",
                    file_types=("JSON file (*.json)", "All files (*.*)"))
                if not res:
                    return {"cancelled": True}
                path = res[0] if isinstance(res, (list, tuple)) else res
            else:
                path = os.path.join(os.path.dirname(CONFIG_FILE),
                                    "prospectors_calibration.json")
            if not str(path).lower().endswith(".json"):
                path = str(path) + ".json"
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            return {"ok": True, "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def import_calibration(self, text):
        try:
            data = json.loads(text)
        except Exception:
            return {"ok": False, "error": "Not a valid calibration file."}
        cur = load_saved()
        px = data.get("pixels") or {}
        keys = ["CAP_FULL_PIXEL", "CAP_LEFT_PIXEL", "DEPOSIT_PIX", "PAN_PIX",
                "SHAKE_PIX", "DIG_TRIGGER_PIXEL"]
        applied = {}
        for k in keys:
            v = px.get(k)
            if isinstance(v, (list, tuple)) and len(v) == 2:
                cur[k] = [int(v[0]), int(v[1])]
                applied[k] = cur[k]
        if not applied:
            return {"ok": False, "error": "No pixel data in that file."}
        if isinstance(data.get("PIXEL_RATIOS"), dict):
            cur["PIXEL_RATIOS"] = data["PIXEL_RATIOS"]
        if isinstance(data.get("CALIB_WINDOW_RECT"), (list, tuple)):
            cur["CALIB_WINDOW_RECT"] = list(data["CALIB_WINDOW_RECT"])
        if isinstance(data.get("PIXEL_COLORS"), dict):
            cur["PIXEL_COLORS"] = {k: str(v) for k, v in data["PIXEL_COLORS"].items()}
        if "CAP_FULL_PIXEL" in cur and "CAP_LEFT_PIXEL" in cur:
            w = int(cur["CAP_FULL_PIXEL"][0] - cur["CAP_LEFT_PIXEL"][0])
            if w > 20:
                cur["CAP_BAR_WIDTH"] = w
        elif data.get("CAP_BAR_WIDTH"):
            cur["CAP_BAR_WIDTH"] = int(data["CAP_BAR_WIDTH"])
        with open(CONFIG_FILE, "w") as f:
            json.dump(cur, f, indent=2)
        return {"ok": True, "pixels": applied,
                "colors": cur.get("PIXEL_COLORS", {})}

    def run_history(self):
        path = os.path.join(os.path.dirname(CONFIG_FILE), "run_history.json")
        data = _read_json(path, [])
        return list(reversed(data)) if isinstance(data, list) else []

    # ---- window detection + auto-calibrate ----
    def detect_roblox(self):
        return _roblox_rect()

    def auto_calibrate(self):
        """Place every pixel automatically from the detected Roblox window using
        the saved ratio profile. No clicking required."""
        rect = _roblox_rect()
        if not rect.get("found"):
            return {"ok": False, "error": rect.get("error", "Roblox not found")}
        cur = load_saved()
        ratios = cur.get("PIXEL_RATIOS") or PIXEL_RATIOS_DEFAULT
        if not ratios:
            return {"ok": False, "needs_manual": True, "window": rect,
                    "error": "No calibration profile yet. Calibrate the spots "
                             "once with Roblox open (just this first time) and "
                             "Auto-calibrate will handle it from then on."}
        pixels = {}
        for key, (fx, fy) in ratios.items():
            pixels[key] = [int(round(rect["x"] + fx * rect["w"])),
                           int(round(rect["y"] + fy * rect["h"]))]
        self.save_pixels(pixels)
        return {"ok": True, "pixels": pixels, "window": rect,
                "count": len(pixels)}

    def sample_pixels(self):
        """Live readout for the Calibrate 'Test detection' tool: sample every
        calibrated pixel and say whether the capacity reads FULL and whether the
        cue pixels are visible -- using the SAME thresholds as the macro so what
        you see here is what the macro sees."""
        try:
            import mss
            import numpy as np
        except Exception as e:
            return {"error": str(e)}
        cur = load_saved()
        keys = ["CAP_FULL_PIXEL", "CAP_LEFT_PIXEL", "DEPOSIT_PIX", "PAN_PIX",
                "SHAKE_PIX", "DIG_TRIGGER_PIXEL"]
        out = {}
        try:
            with mss.mss() as sct:
                m = sct.monitors[0]
                L, T = m["left"], m["top"]
                Rg, Bg = L + m["width"], T + m["height"]
                for k in keys:
                    v = cur.get(k)
                    if not (isinstance(v, (list, tuple)) and len(v) == 2):
                        continue
                    x, y = int(v[0]), int(v[1])
                    bx = min(max(x - 3, L), Rg - 6)
                    by = min(max(y - 3, T), Bg - 6)
                    img = np.asarray(sct.grab(
                        {"left": bx, "top": by, "width": 6, "height": 6}))[:, :, :3]
                    b, g, r = [int(c) for c in img.reshape(-1, 3).mean(0)]
                    out[k] = {"r": r, "g": g, "b": b}
        except Exception as e:
            return {"error": str(e)}

        def white(p):
            return p["r"] >= 175 and p["g"] >= 175 and p["b"] >= 175

        def yellow(p):
            return (p["r"] >= 140 and p["g"] >= 140
                    and p["b"] <= min(p["r"], p["g"]) - 45)
        res = {"pixels": out}
        if "CAP_FULL_PIXEL" in out:
            res["cap_full"] = yellow(out["CAP_FULL_PIXEL"])
        for k in ("DEPOSIT_PIX", "PAN_PIX", "SHAKE_PIX"):
            if k in out:
                res[k + "_white"] = white(out[k])
        return res

    def save_config(self, data):
        """Write scalar settings, PRESERVING relic keys already in the file."""
        cur = load_saved()
        for k, t in TYPES.items():
            if k in data:
                cur[k] = _coerce(t, data[k])
        with open(CONFIG_FILE, "w") as f:
            json.dump(cur, f, indent=2)
        return len([k for k in TYPES if k in data])

    def save_relics(self, relics, enabled):
        cur = load_saved()
        clean = []
        for r in (relics or []):
            try:
                clean.append({"name": str(r.get("name", "Relic")),
                              "minutes": max(1, int(r.get("minutes", 10))),
                              "slot": int(r.get("slot", 0)),
                              "clicks": max(1, int(r.get("clicks", 2)))})
            except (ValueError, TypeError):
                continue
        cur["RELICS"] = clean
        cur["RELICS_ENABLED"] = bool(enabled)
        with open(CONFIG_FILE, "w") as f:
            json.dump(cur, f, indent=2)
        return len(clean)

    # ---- builds (named profiles) ----
    def list_builds(self):
        return sorted(_read_json(BUILDS_FILE, {}).keys())

    def save_build(self, name, data, relics, enabled):
        name = (name or "").strip()
        if not name:
            return "name required"
        builds = _read_json(BUILDS_FILE, {})
        entry = {}
        for k, t in TYPES.items():
            if k in data:
                entry[k] = _coerce(t, data[k])
        entry["RELICS"] = relics or []
        entry["RELICS_ENABLED"] = bool(enabled)
        builds[name] = entry
        with open(BUILDS_FILE, "w") as f:
            json.dump(builds, f, indent=2)
        return "saved"

    def load_build(self, name):
        builds = _read_json(BUILDS_FILE, {})
        entry = builds.get(name)
        if entry is None:
            return None
        # MERGE the build into the active config so we keep things the build
        # doesn't carry (calibrated pixels, webhook URL/secret, window settings).
        cur = load_saved()
        cur.update(entry)
        with open(CONFIG_FILE, "w") as f:
            json.dump(cur, f, indent=2)
        return entry

    def delete_build(self, name):
        builds = _read_json(BUILDS_FILE, {})
        if name in builds:
            del builds[name]
            with open(BUILDS_FILE, "w") as f:
                json.dump(builds, f, indent=2)
        return self.list_builds()

    # ---- calibrate: wait for the user to CLICK a spot, capture its x/y + colour
    def calibrate_capture(self):
        """Wait for the user to mark a spot, then return its position + colour.

        Two ways to mark: LEFT-CLICK the spot, or hover it and press ENTER. Press
        ESC to cancel. Returns {x,y,r,g,b}, {error:...} or {cancelled:True}.
        (macOS / Quartz.)"""
        try:
            import Quartz
        except Exception as e:
            return {"error": "Quartz unavailable: %s" % e}
        try:
            import numpy as np
        except Exception:
            return {"error": "Image library (numpy) missing."}
        try:
            import mss
        except Exception:
            return {"error": "Screen-capture library (mss) missing."}
        import time as _t
        try:
            bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
            STATE = getattr(Quartz, "kCGEventSourceStateCombinedSessionState", 0)
            LBTN = getattr(Quartz, "kCGMouseButtonLeft", 0)
            KEY_RETURN, KEY_ENTER, KEY_ESC = 36, 76, 53  # macOS virtual key codes

            def down():
                return bool(Quartz.CGEventSourceButtonState(STATE, LBTN))

            def key(kc):
                return bool(Quartz.CGEventSourceKeyState(STATE, kc))

            def enter():
                return key(KEY_RETURN) or key(KEY_ENTER)
            # release whatever started this, then wait for the NEXT mark
            t0 = _t.perf_counter()
            while (down() or enter()) and _t.perf_counter() - t0 < 0.6:
                _t.sleep(0.01)
            t0 = _t.perf_counter()
            while True:
                if key(KEY_ESC):
                    return {"cancelled": True}
                if down() or enter():
                    break
                if _t.perf_counter() - t0 > 30:
                    return {"error": "Timed out — no click or Enter within 30s. "
                                     "Click the spot in-game, or hover it and press Enter."}
                _t.sleep(0.008)
            loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
            try:
                with mss.mss() as sct:
                    main = sct.monitors[1]
                    lw = bounds.size.width
                    scale = main["width"] / lw if lw else 1.0
                    x = int(loc.x * scale)
                    y = int(loc.y * scale)
                    vm = sct.monitors[0]
                    L, T = vm["left"], vm["top"]
                    R, B = L + vm["width"], T + vm["height"]
                    bx = min(max(x - 3, L), R - 6)
                    by = min(max(y - 3, T), B - 6)
                    img = np.asarray(sct.grab(
                        {"left": bx, "top": by, "width": 6, "height": 6}))[:, :, :3]
                b, g, r = [int(v) for v in img.reshape(-1, 3).mean(0)]
            except Exception as e:
                return {"error": "Couldn't read that pixel's colour: %s" % e}
            t0 = _t.perf_counter()
            while (down() or enter()) and _t.perf_counter() - t0 < 1.0:
                _t.sleep(0.008)
            return {"x": x, "y": y, "r": r, "g": g, "b": b}
        except Exception as e:
            return {"error": str(e)}

    # ---- run control ----
    def launch(self, data=None, relics=None, enabled=None):
        if data is not None:
            self.save_config(data)
        if relics is not None:
            self.save_relics(relics, enabled)
        if self.proc is not None:
            return "already running"
        py = sys.executable or "python3"
        self.proc = subprocess.Popen(
            [py, MACRO_FILE], cwd=HERE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1)
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
        self._run_active = True
        self._last_stats = None
        self._macro_status = "idle"
        return "launched"

    def _save_history(self, reason=None):
        """Append the just-finished run to run_history.json. The APP writes this
        (not the macro) because Stop kills the macro before it could save. Guarded
        so each run is recorded exactly once."""
        if not getattr(self, "_run_active", False):
            return
        self._run_active = False
        st = self._last_stats or {}
        if not st or not (st.get("runtime_s") or st.get("cycles")):
            return                      # nothing actually ran -> don't log
        import datetime
        entry = dict(st)
        entry["reason"] = reason or st.get("stop_reason") or "manual"
        entry["ended"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        path = os.path.join(os.path.dirname(CONFIG_FILE), "run_history.json")
        hist = _read_json(path, [])
        if not isinstance(hist, list):
            hist = []
        hist.append(entry)
        try:
            with open(path, "w") as f:
                json.dump(hist[-100:], f, indent=2)
        except OSError:
            pass

    def stop(self):
        self._save_history()
        if self.proc is not None:
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    self.proc.send_signal(sig)
                except Exception:
                    pass
            self.proc = None
        self._macro_status = "off"
        return "stopped"

    def is_running(self):
        return self.proc is not None

    def pill_state(self):
        return {"alive": self.proc is not None,
                "status": getattr(self, "_macro_status", "off"),
                "stats": self._last_stats or {}}

    def popout(self):
        """Condense into the always-on-top pill; hide the main window."""
        global _pill, _window
        try:
            if _pill is not None:
                _pill.show()
            if _window is not None:
                _window.hide()
            self._popped = True
        except Exception as e:
            print("[pill] popout failed: %s" % e)
            return "err"
        return "ok"

    def save_autobuild(self, stats):
        cur = load_saved()
        if isinstance(stats, dict):
            cur["AUTOBUILD"] = {str(k): stats[k] for k in stats}
            with open(CONFIG_FILE, "w") as f:
                json.dump(cur, f, indent=2)
        return "saved"

    def save_hotkeys(self, hk):
        cur = load_saved()
        for k in _HK_DEFAULTS:
            if isinstance(hk, dict) and isinstance(hk.get(k), dict):
                cur[k] = {"ctrl": bool(hk[k].get("ctrl")),
                          "alt": bool(hk[k].get("alt")),
                          "shift": bool(hk[k].get("shift")),
                          "code": str(hk[k].get("code", ""))}
        with open(CONFIG_FILE, "w") as f:
            json.dump(cur, f, indent=2)
        return "saved"

    def start_overlay_calibrate(self, key, label=""):
        """Open a full-screen overlay (a snapshot of your screen). Click the
        target pixel, see a marker + colour + coords, Confirm to save."""
        global _overlay
        try:
            import mss
            import mss.tools
            import base64
            import numpy as np
        except Exception as e:
            return {"error": str(e)}
        try:
            with mss.mss() as sct:
                raw = sct.grab(sct.monitors[1])
            self._shot = np.asarray(raw)
            self._shot_h, self._shot_w = self._shot.shape[0], self._shot.shape[1]
            step = max(1, int(round(self._shot_w / 1600.0)))
            disp = self._shot[::step, ::step]
            rgb = disp[:, :, (2, 1, 0)].tobytes()
            png = mss.tools.to_png(rgb, (disp.shape[1], disp.shape[0]))
            self._shot_b64 = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        except Exception as e:
            return {"error": str(e)}
        self._overlay_key = key
        self._overlay_label = label or key
        self._overlay_pending = None
        self._overlay_proposed = None
        try:
            if _overlay is not None:
                _overlay.evaluate_js("window.__reload && window.__reload()")
                _overlay.show()
        except Exception as e:
            return {"error": str(e)}
        return {"ok": True}

    def overlay_image(self):
        d = {"src": getattr(self, "_shot_b64", ""),
             "label": getattr(self, "_overlay_label", "")}
        p = getattr(self, "_overlay_proposed", None)
        w = getattr(self, "_shot_w", 0)
        h = getattr(self, "_shot_h", 0)
        if p and w and h:
            d["proposed"] = {"fx": p["x"] / float(w), "fy": p["y"] / float(h),
                             "hex": p["hex"], "x": p["x"], "y": p["y"]}
        return d

    def overlay_pick(self, fx, fy):
        try:
            w, h = self._shot_w, self._shot_h
            x = max(0, min(w - 1, int(round(float(fx) * w))))
            y = max(0, min(h - 1, int(round(float(fy) * h))))
            px = self._shot[y, x]
            b, g, r = int(px[0]), int(px[1]), int(px[2])
            hexv = "#%02x%02x%02x" % (r, g, b)
            self._overlay_pending = {"x": x, "y": y, "r": r, "g": g, "b": b, "hex": hexv}
            return self._overlay_pending
        except Exception as e:
            return {"error": str(e)}

    def overlay_confirm(self):
        p = getattr(self, "_overlay_pending", None)
        key = getattr(self, "_overlay_key", None)
        if p and key:
            x, y, hexv = p["x"], p["y"], p["hex"]
            if key in ("CAP", "CAP_RIGHT"):
                px = {"CAP_FULL_PIXEL": [x, y]}
                cl = getattr(self, "_overlay_cap_left", None)
                if key == "CAP" and cl:
                    px["CAP_LEFT_PIXEL"] = cl
                self.save_pixels(px, {"CAP_FULL_PIXEL": hexv})
            elif key == "CAP_LEFT":
                self.save_pixels({"CAP_LEFT_PIXEL": [x, y]})
            elif key == "FR_TEXT":
                self.save_pixels({}, None, {"FR_SCAN_X": x,
                                            "FR_TEXT_RGB": [p["r"], p["g"], p["b"]]})
            elif key in ("FR_BOX_TOP", "FR_BOX_BOTTOM"):
                self.save_pixels({}, None, {key: y})
            elif key in ("FR_OPEN_PIXEL", "FR_HOME_PIXEL"):
                self.save_pixels({}, None, {key: [x, y]})
            else:
                self.save_pixels({key: [x, y]}, {key: hexv})
        self._close_overlay()
        try:
            if _window is not None:
                _window.evaluate_js("window.__calRefresh&&window.__calRefresh()")
        except Exception:
            pass
        return {"ok": True}

    def overlay_cancel(self):
        self._close_overlay()
        return {"ok": True}

    def _close_overlay(self):
        global _overlay
        try:
            if _overlay is not None:
                _overlay.hide()
        except Exception:
            pass

    def _grab_full(self):
        import mss
        import numpy as np
        with mss.mss() as sct:
            raw = sct.grab(sct.monitors[1])
        return np.asarray(raw)            # (h, w, 4) BGRA

    def wizard_capture_empty(self):
        """Remember a snapshot of the screen with the capacity bar EMPTY. The
        full step compares against it: whatever turns gold is the bar."""
        try:
            import numpy as np
            self._cap_empty = self._grab_full().astype(np.int16)
            return {"ok": True, "msg": "Captured the empty bar"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _detect_capacity_px(self, arr):
        import numpy as np
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
        emp = getattr(self, "_cap_empty", None)
        if emp is not None and emp.shape == arr.shape:
            diff = (np.abs(r - emp[:, :, 2]) + np.abs(g - emp[:, :, 1])
                    + np.abs(b - emp[:, :, 0]))
            changed = cand & (diff > 60)
            if int(changed.sum(axis=1).max() if changed.any() else 0) >= 50:
                cand = changed
        counts = cand.sum(axis=1)
        y = int(counts.argmax())
        if int(counts[y]) < 50:
            return {"ok": False, "error": "no full bar found"}
        xs = np.where(cand[y])[0]
        seg = max(np.split(xs, np.where(np.diff(xs) > 6)[0] + 1), key=len)
        left, right = int(seg[0]), int(seg[-1])
        if right - left < 50:
            return {"ok": False, "error": "bar too short"}

        # The macro's full-bar test (is_yellow) needs SOLID gold; the literal
        # edge pixel is a pale anti-aliased blend that fails it. Walk both ends
        # inward to a solidly-gold pixel, plus a small margin off the edge.
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
        """Locate the prompt by finding the BOTTOM-MOST block of centred white
        text (the Pan / Shake / Collect Deposit word, which sits just below the
        Pan Fill bar and above the hotbar), then take the LEFTMOST white blob on
        that line -- the mouse icon to the left of the word."""
        import numpy as np
        H, W = arr.shape[0], arr.shape[1]
        b = arr[:, :, 0].astype(np.int16)
        g = arr[:, :, 1].astype(np.int16)
        r = arr[:, :, 2].astype(np.int16)
        lo = np.minimum(np.minimum(r, g), b)
        hi = np.maximum(np.maximum(r, g), b)
        white = (lo >= 225) & ((hi - lo) <= 26)
        # centre-bottom search zone: below the bar, above the hotbar; the right
        # side is trimmed so "Auto Sell" never competes with the prompt
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
        for c in cols:                       # first blob from the left = mouse icon
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

    def wizard_propose(self, kind, label=""):
        """Auto-detect a spot, then OPEN THE OVERLAY pre-marked with a red X at
        the guess so the user can Confirm or Redo. Nothing is saved until they
        confirm. If detection fails, the overlay opens for a plain manual pick."""
        global _overlay
        try:
            import base64
            import mss.tools
            import numpy as np
        except Exception as e:
            return {"error": str(e)}
        try:
            arr = self._grab_full()
        except Exception as e:
            return {"error": str(e)}
        if kind in ("CAP", "CAP_RIGHT", "CAP_LEFT"):
            det = self._detect_capacity_px(arr)
        elif kind in ("PAN_PIX", "SHAKE_PIX", "DEPOSIT_PIX"):
            det = self._detect_cue_px(arr, kind)
        else:
            det = {"ok": False, "error": "unknown target"}
        self._shot = arr
        self._shot_h, self._shot_w = arr.shape[0], arr.shape[1]
        step = max(1, int(round(self._shot_w / 1600.0)))
        disp = arr[::step, ::step]
        png = mss.tools.to_png(disp[:, :, (2, 1, 0)].tobytes(),
                               (disp.shape[1], disp.shape[0]))
        self._shot_b64 = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        self._overlay_key = kind
        self._overlay_label = label or kind
        self._overlay_pending = None
        self._overlay_proposed = None
        self._overlay_cap_left = None
        if det.get("ok"):
            if kind in ("CAP", "CAP_RIGHT"):
                tx, ty = det["right"]
                self._overlay_cap_left = det.get("left")
            elif kind == "CAP_LEFT":
                tx, ty = det["left"]
            else:
                tx, ty = det["pixel"]
            px = arr[ty, tx]
            self._overlay_pending = {"x": tx, "y": ty, "r": int(px[2]),
                                     "g": int(px[1]), "b": int(px[0]),
                                     "hex": "#%02x%02x%02x" % (int(px[2]), int(px[1]), int(px[0]))}
            self._overlay_proposed = dict(self._overlay_pending)
        try:
            if _overlay is not None:
                _overlay.evaluate_js("window.__reload && window.__reload()")
                _overlay.show()
        except Exception as e:
            return {"error": str(e)}
        return {"ok": True, "detected": bool(self._overlay_proposed), "msg": det.get("error")}

    def restore(self):
        """Show the main window again and hide the pill."""
        global _pill, _window
        try:
            if _window is not None:
                _window.show()
        except Exception:
            pass
        try:
            if _pill is not None:
                _pill.hide()
        except Exception:
            pass
        self._popped = False
        return "ok"

    def toggle_popout(self):
        """Flip between the pill and the full window (used by the hotkey)."""
        if getattr(self, "_popped", False):
            self.restore()
        else:
            self.popout()
        return "ok"

    def _pump(self, proc):
        for line in iter(proc.stdout.readline, ""):
            line = line.rstrip("\n")
            if line.startswith("__STATS__ "):
                _emit_stats(line[10:])        # raw JSON -> live stats panel
                try:
                    self._last_stats = json.loads(line[10:])
                except Exception:
                    pass
                if self._macro_status in ("off", "idle"):
                    self._macro_status = "running"
                continue
            if line.strip() == "__POPOUT__":
                try:
                    self.toggle_popout()
                except Exception:
                    pass
                continue
            _s = line.strip()
            if _s.startswith("[RUNNING]"):
                self._macro_status = "running"
            elif _s.startswith("[PAUSED]"):
                self._macro_status = "paused"
            elif "SAFE PAUSE" in _s:
                self._macro_status = "safe-pause"
            elif "FR-recover" in _s or "RECOVERY" in _s:
                self._macro_status = "recovering"
            elif "HARD STOP" in _s:
                self._macro_status = "stopped"
            _emit_log(line)
        rc = proc.wait()
        _emit_log(f"[macro exited, code {rc}]")
        self.proc = None
        self._macro_status = "off"
        self._save_history()              # macro ended on its own (Esc / self-stop)
        _emit_state(False)


_HK_DEFAULTS = {
    "HOTKEY_TOGGLE": {"ctrl": True, "alt": False, "shift": False, "code": "KeyK"},
    "HOTKEY_SOFTSTOP": {"ctrl": True, "alt": False, "shift": False, "code": "KeyJ"},
    "HOTKEY_QUIT": {"ctrl": False, "alt": False, "shift": False, "code": "Escape"},
    "HOTKEY_POPOUT": {"ctrl": True, "alt": False, "shift": False, "code": "KeyP"},
}


_window = None
_pill = None
_overlay = None

PILL_HTML = r'''<!doctype html><html><head><meta charset="utf-8"><style>
 html,body{margin:0;height:100%;background:#1a1816;color:#ece4d6;font:13px/1.3 -apple-system,"Segoe UI",sans-serif;overflow:hidden;-webkit-user-select:none;user-select:none}
 .card{display:flex;flex-direction:column;gap:7px;height:100%;padding:9px 11px;border:1px solid #423d35;border-radius:14px;background:#1f1d1a;box-sizing:border-box}
 .top{display:flex;align-items:center;gap:8px}
 .drag{flex:1;display:flex;align-items:center;gap:8px;cursor:move;min-width:0}
 .dot{width:9px;height:9px;border-radius:50%;background:#6a6253;flex:0 0 auto}
 .dot.on{background:#7faf5d;animation:pulse 1.6s infinite}
 .dot.warn{background:#c2924c} .dot.busy{background:#7d9b63}
 @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(127,175,93,.5)}70%{box-shadow:0 0 0 6px rgba(127,175,93,0)}100%{box-shadow:0 0 0 0 rgba(127,175,93,0)}}
 .st{font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis} .st b{color:#e0b873}
 .ex{background:#2a2418;color:#e9e0cf;border:0;border-radius:8px;padding:6px 9px;cursor:pointer;font:inherit;flex:0 0 auto}
 .stats{display:flex;gap:12px;font-variant:tabular-nums;color:#9c9183;font-weight:600;font-size:12px}
 .stats span b{color:#ece4d6;font-weight:700}
 .go{width:100%;border:0;border-radius:9px;padding:9px;cursor:pointer;font:inherit;font-weight:700;color:#241a02;background:#7faf5d}
 .go.stop{background:#c2924c}
</style></head><body>
 <div class="card">
   <div class="top">
     <div class="drag"><span class="dot" id="dot"></span><span class="st" id="status">Off</span></div>
     <button class="ex" id="expand" title="Back to app">&#9635;</button>
   </div>
   <div class="stats"><span><b id="rt">0:00</b> run</span><span><b id="pans">0</b> pans</span><span><b id="rate">0</b>/hr</span><span><b id="rec">0</b> rec</span></div>
   <button class="go" id="toggle">Start</button>
 </div>
<script>
 const api=()=>window.pywebview&&window.pywebview.api;
 let alive=false;const $=id=>document.getElementById(id);
 const SM={running:["on","Running"],paused:["warn","Paused"],"safe-pause":["warn","Safe-pause"],recovering:["busy","Recovering"],stopped:["warn","Stopped"],idle:["warn","Ready · press start key"],off:["","Off"]};
 function fmt(s){s=s||0;return Math.floor(s/60)+":"+String(s%60).padStart(2,"0");}
 async function poll(){let r;try{r=await api().pill_state();}catch(e){return;}
   alive=r.alive;const sm=SM[r.status||"off"]||["","—"];
   $("dot").className="dot "+sm[0];$("status").innerHTML=sm[1];
   const s=r.stats||{};$("rt").textContent=fmt(s.runtime_s);$("pans").textContent=s.cycles||0;$("rate").textContent=s.pans_per_hr||0;$("rec").textContent=s.recoveries||0;
   const b=$("toggle");b.textContent=alive?"Stop":"Start";b.className="go"+(alive?" stop":"");}
 $("toggle").onclick=async()=>{try{if(alive){await api().stop();}else{await api().launch();}}catch(e){}poll();};
 $("expand").onclick=()=>{try{api().restore();}catch(e){}};
 setInterval(poll,1000);poll();
</script></body></html>'''


def _emit_log(text):
    if _window is None:
        return
    try:
        _window.evaluate_js(f"window.addLog && addLog({json.dumps(text)})")
    except Exception:
        pass


def _emit_stats(json_str):
    if _window is None:
        return
    try:
        _window.evaluate_js(f"window.setStats && setStats({json_str})")
    except Exception:
        pass


def _emit_state(running):
    if _window is None:
        return
    try:
        _window.evaluate_js(f"window.setRunning && setRunning({json.dumps(running)})")
    except Exception:
        pass


# ============================================================================
# HTML
# ============================================================================
def _qm(key):
    if not HELP.get(key):
        return ""
    tip = HELP[key].replace('"', "&quot;")
    return f'<span class="qm" data-tip="{tip}">?</span>'


_OVERLAY_HTML = '<!doctype html><html><head><meta charset="utf-8"><style>\n html,body{margin:0;height:100%;overflow:hidden;cursor:crosshair;background:#000;font:600 13px -apple-system,"Segoe UI",sans-serif;color:#ece4d6;-webkit-user-select:none;user-select:none}\n #shot{position:fixed;inset:0;width:100vw;height:100vh;object-fit:fill;display:block}\n .bar{position:fixed;top:14px;left:50%;transform:translateX(-50%);background:rgba(31,29,26,.93);border:1px solid #423d35;border-radius:12px;padding:9px 16px;z-index:9}\n .bar b{color:#e0b873}\n #marker{position:fixed;width:24px;height:24px;transform:translate(-50%,-50%);display:none;z-index:8;pointer-events:none}\n #marker::before,#marker::after{content:"";position:absolute;left:50%;top:50%;width:24px;height:3px;background:#ff5b5b;border-radius:2px;box-shadow:0 0 4px #000,0 0 1px #000;transform:translate(-50%,-50%) rotate(45deg)}\n #marker::after{transform:translate(-50%,-50%) rotate(-45deg)}\n #loupe{position:fixed;width:124px;height:124px;border-radius:50%;border:2px solid #e0b873;box-shadow:0 6px 20px rgba(0,0,0,.55);display:none;z-index:8;pointer-events:none;background-repeat:no-repeat;image-rendering:pixelated}\n #loupe::after{content:"";position:absolute;left:50%;top:50%;width:11px;height:11px;transform:translate(-50%,-50%);border:1px solid #e0b873;border-radius:50%}\n #tip{position:fixed;z-index:9;background:rgba(31,29,26,.96);border:1px solid #423d35;border-radius:12px;padding:12px;display:none;min-width:200px}\n #tip .sw{width:100%;height:30px;border-radius:7px;border:1px solid rgba(0,0,0,.25);margin-bottom:8px}\n #tip .meta{font-variant:tabular-nums;margin-bottom:10px;line-height:1.6} #tip .meta s{text-decoration:none;color:#9c9183}\n #tip .row{display:flex;gap:8px}\n button{font:inherit;font-weight:700;border:0;border-radius:9px;padding:8px 12px;cursor:pointer}\n .go{background:#7faf5d;color:#241a02;flex:1}.re{background:#2a2418;color:#e9e0cf}.cn{background:#3a201c;color:#f0c0b0}\n</style></head><body>\n <img id="shot" alt="">\n <div class="bar">Calibrate: <b id="lab"></b> &nbsp;&middot;&nbsp; the red &#10005; is the detected spot &mdash; Confirm or Redo &nbsp;&middot;&nbsp; Esc cancels</div>\n <div id="loupe"></div><div id="marker"></div>\n <div id="tip"><div class="sw" id="sw"></div>\n   <div class="meta"><s>colour</s> <b id="hex">&mdash;</b><br><s>at</s> <b id="xy">&mdash;</b></div>\n   <div class="row"><button class="go" id="ok">Confirm</button><button class="re" id="redo">Redo</button><button class="cn" id="cancel">&#10005;</button></div></div>\n<script>\n const api=()=>window.pywebview&&window.pywebview.api;\n const shot=document.getElementById(\'shot\'),loupe=document.getElementById(\'loupe\'),marker=document.getElementById(\'marker\'),tip=document.getElementById(\'tip\');\n let picked=false,natW=0,natH=0,prop=null;\n function reset(){picked=false;marker.style.display=\'none\';tip.style.display=\'none\';loupe.style.display=\'none\';prop=null;}\n function showTipAt(cx,cy,hex,x,y){marker.style.display=\'block\';marker.style.left=cx+\'px\';marker.style.top=cy+\'px\';\n   document.getElementById(\'sw\').style.background=hex;document.getElementById(\'hex\').textContent=hex;document.getElementById(\'xy\').textContent=x+\', \'+y;\n   let tx=cx+24,ty=cy+24;if(tx>innerWidth-236)tx=cx-220;if(ty>innerHeight-160)ty=cy-160;\n   tip.style.left=tx+\'px\';tip.style.top=ty+\'px\';tip.style.display=\'block\';picked=true;loupe.style.display=\'none\';}\n function placeProposed(){if(!prop||!natW)return;const r=shot.getBoundingClientRect();\n   showTipAt(r.left+prop.fx*r.width, r.top+prop.fy*r.height, prop.hex, prop.x, prop.y);}\n async function boot(){try{const d=await api().overlay_image();if(d&&d.src){shot.src=d.src;document.getElementById(\'lab\').textContent=d.label||\'\';}prop=(d&&d.proposed)||null;}catch(e){}}\n shot.onload=()=>{natW=shot.naturalWidth;natH=shot.naturalHeight;loupe.style.backgroundImage=\'url(\'+shot.src+\')\';if(prop)placeProposed();};\n function frac(e){const r=shot.getBoundingClientRect();return [(e.clientX-r.left)/r.width,(e.clientY-r.top)/r.height];}\n document.addEventListener(\'mousemove\',e=>{if(picked||!natW)return;loupe.style.display=\'block\';\n   loupe.style.left=(e.clientX+20)+\'px\';loupe.style.top=(e.clientY+20)+\'px\';\n   const z=9,bw=natW*z,bh=natH*z;loupe.style.backgroundSize=bw+\'px \'+bh+\'px\';\n   const f=frac(e);loupe.style.backgroundPosition=(-(f[0]*bw)+62)+\'px \'+(-(f[1]*bh)+62)+\'px\';});\n document.addEventListener(\'click\',async e=>{if(picked||tip.contains(e.target))return;\n   const f=frac(e);let r;try{r=await api().overlay_pick(f[0],f[1]);}catch(_){return;}\n   if(!r||r.error)return;showTipAt(e.clientX,e.clientY,r.hex,r.x,r.y);});\n document.getElementById(\'redo\').onclick=()=>{reset();};\n document.getElementById(\'cancel\').onclick=()=>{try{api().overlay_cancel();}catch(e){}};\n document.getElementById(\'ok\').onclick=()=>{try{api().overlay_confirm();}catch(e){}};\n document.addEventListener(\'keydown\',e=>{if(e.key===\'Escape\'){try{api().overlay_cancel();}catch(_){}}});\n window.__reload=function(){reset();boot();};\n window.addEventListener(\'pywebviewready\',boot);\n boot();\n</script></body></html>'


def build_html():
    navs, panels = [], []

    def nav(tabid, icon, label, active=False, search=""):
        a = " active" if active else ""
        ds = f' data-search="{search}"' if search else ""
        navs.append({"id": tabid, "html": (
            f'<button type="button" class="tab{a}" data-tab="{tabid}"{ds}>'
            f'<span class="ti">{icon}</span><span>{label}</span></button>')})

    # Run
    nav("run", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M7 5l12 7l-12 7z"/></svg>', "Run", True)
    panels.append(
        '<section class="panel active" id="prun"><div class="phead"><h2>Run</h2>'
        '<p class="chint">Start the macro, tab into Roblox. Ctrl+K also '
        'starts/stops; Esc quits.</p></div>'
        '<div class="runbtns"><button type="button" id="startbtn" class="big go">'
        'Start macro</button><button type="button" id="stopbtn" class="big stop" '
        'disabled>Stop</button><span id="rstate" class="rstate">stopped</span></div>'
        '<div class="statsbar">'
        '<div class="stat"><div class="sv" id="st_run">0:00</div><div class="sl">runtime</div></div>'
        '<div class="stat"><div class="sv" id="st_cyc">0</div><div class="sl">pans</div></div>'
        '<div class="stat"><div class="sv" id="st_rate">0</div><div class="sl">pans/hr</div></div>'
        '<div class="stat"><div class="sv" id="st_rec">0</div><div class="sl">recoveries</div></div>'
        '<div class="stat"><div class="sv" id="st_nud">0</div><div class="sl">nudges</div></div>'
        '<div class="stat"><div class="sv" id="st_rel">0</div><div class="sl">relics</div></div>'
        '<div class="stat"><div class="sv" id="st_safe">0</div><div class="sl">safe-stops</div></div>'
        '<div class="stat"><div class="sv" id="st_hard">0</div><div class="sl">hard-stops</div></div>'
        '</div>'
        '<pre id="log" class="log"></pre></section>')

    # Calibrate
    nav("cal", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.2"/></svg>', "Calibrate")
    calrows = []
    for key, label, desc, _d in PIXEL_FIELDS:
        calrows.append(
            f'<div class="calrow" data-pkey="{key}">'
            f'<div class="calinfo"><div class="calname">{label}</div>'
            f'<div class="caldesc">{desc}</div></div>'
            f'<div class="calval">'
            f'<input class="cnum cx" id="cx_{key}" type="number" placeholder="x">'
            f'<input class="cnum cy" id="cy_{key}" type="number" placeholder="y">'
            f'<input class="chex" id="cc_{key}" type="text" placeholder="#color" maxlength="7" spellcheck="false">'
            f'<span class="calsw2" id="cs_{key}"></span></div>'
            f'<button type="button" class="btn2 calbtn" data-pkey="{key}">Calibrate</button>'
            f'</div>')
    panels.append(
        '<section class="panel" id="pcal"><div class="phead"><h2>Calibrate pixels</h2>'
        '<p class="chint">Open Prospecting in Roblox with the HUD visible, then run '
        '<b>Guided calibration</b> below. <b>Test detection</b> shows live whether '
        'the macro sees everything correctly.</p></div>'
        '<div class="autocal">'
        '  <button type="button" id="wizbtn" class="btn">✨ Guided calibration (recommended)</button>'
        '  <button type="button" id="dettest" class="btn2">Test detection (live)</button>'
        '  <div class="detout" id="detout"></div>'
        '</div>'
        '<div class="caldiv"><span>or calibrate manually</span></div>'
        '<p class="chint" style="margin:0 0 10px">Click a <b>Calibrate</b> button, then '
        'either click the exact spot in-game, or hover it and press <b>Enter</b>. '
        'Press <b>Esc</b> to cancel. Do them all, then <b>Save calibration</b>.</p>'
        f'<div class="calrows">{"".join(calrows)}</div>'
        '<div class="caldiv"><span>Fortune River recovery (optional)</span></div>'
        '<p class="chint" style="margin:0 0 10px">Only for <b>Fortune River recovery</b> '
        '(Smart tab). Open the Fast Travel menu in-game first, then calibrate each spot.</p>'
        '<div class="calrows">'
        '<div class="calrow"><div class="calinfo"><div class="calname">Fortune River row (pink text)</div>'
        '<div class="caldesc">Click the pink Fortune River text. Sets the scan column and the colour to match.</div></div>'
        '<div class="calval"><span class="frstat" id="frstat_text">not set</span>'
        '<span class="calsw2" id="frsw_text"></span></div>'
        '<button type="button" class="btn2 frbtn" data-frk="text">Calibrate</button></div>'
        '<div class="calrow"><div class="calinfo"><div class="calname">List - TOP edge</div>'
        '<div class="caldesc">Click just inside the top of the travel list box.</div></div>'
        '<div class="calval"><span class="frstat" id="frstat_top">not set</span></div>'
        '<button type="button" class="btn2 frbtn" data-frk="top">Calibrate</button></div>'
        '<div class="calrow"><div class="calinfo"><div class="calname">List - BOTTOM edge</div>'
        '<div class="caldesc">Click just inside the bottom of the travel list box.</div></div>'
        '<div class="calval"><span class="frstat" id="frstat_bottom">not set</span></div>'
        '<button type="button" class="btn2 frbtn" data-frk="bottom">Calibrate</button></div>'
        '<div class="calrow"><div class="calinfo"><div class="calname">Open Fast Travel (optional)</div>'
        '<div class="caldesc">A spot to click to open the menu. Leave unset if 4 + Shift already opens it.</div></div>'
        '<div class="calval"><span class="frstat" id="frstat_open">not set</span></div>'
        '<button type="button" class="btn2 frbtn" data-frk="open">Calibrate</button></div>'
        '<div class="calrow"><div class="calinfo"><div class="calname">Screen centre / cursor home</div>'
        '<div class="caldesc">With shift-lock OFF, where the cursor rests (middle of the screen). All FR mouse moves are measured from here.</div></div>'
        '<div class="calval"><span class="frstat" id="frstat_home">not set</span></div>'
        '<button type="button" class="btn2 frbtn" data-frk="home">Calibrate</button></div>'
        '</div>'
        '<div class="calactions">'
        '<button type="button" id="savepixels" class="btn">Save calibration</button>'
        '<button type="button" id="exportcal" class="btn2">Export\u2026</button>'
        '<button type="button" id="importcal" class="btn2">Import\u2026</button>'
        '<input type="file" id="importfile" accept="application/json,.json" style="display:none">'
        '</div></section>')

    # Relics
    nav("relics", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/></svg>', "Relics")
    rrows = []
    for i in range(MAX_RELIC_ROWS):
        rrows.append(
            f'<div class="rrow" data-i="{i}">'
            f'<label class="switch sm"><input type="checkbox" class="renable">'
            f'<span class="track"><span class="knob"></span></span></label>'
            f'<input class="rname" placeholder="Relic name">'
            f'<span class="rlab">every</span><input type="number" class="rmin" min="1">'
            f'<span class="rlab">min · slot</span><input type="number" class="rslot" min="0" max="9">'
            f'<span class="rlab">·</span><input type="number" class="rclicks" min="1">'
            f'<span class="rlab">clicks</span></div>')
    panels.append(
        '<section class="panel" id="prelics"><div class="phead"><h2>Relics</h2>'
        '<p class="chint">Every N minutes the macro pauses, switches to the slot, '
        'double-clicks to use the item, switches back to slot 1 (pan), and resumes. '
        'Enable a row to use it.</p></div>'
        '<div class="rows" style="max-width:620px">'
        '<label class="row"><span class="lbl">Enable relic timer</span>'
        '<span class="switch"><input type="checkbox" id="relicsMaster">'
        '<span class="track"><span class="knob"></span></span></span></label></div>'
        f'<div class="relicwrap">{"".join(rrows)}</div>'
        '<button type="button" id="saverelics" class="btn" style="margin-top:12px">'
        'Save relics</button></section>')

    # History
    nav("hist", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 9 -9a9 9 0 0 0 -7 3.3L3 8"/><path d="M3 3v5h5"/><path d="M12 8v4l3 2"/></svg>', "History")
    panels.append(
        '<section class="panel" id="phist"><div class="phead"><h2>Run history</h2>'
        '<p class="chint">Past runs and their stats, kept across restarts.</p></div>'
        '<button type="button" id="histrefresh" class="btn2" style="margin-bottom:12px">Refresh</button>'
        '<div id="histbox" class="histbox"></div></section>')

    nav("keys", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6 9.5h.01M9.5 9.5h.01M13 9.5h.01M16.5 9.5h.01M6.5 13.5h11"/></svg>', "Keybinds")
    _kbrows = [("HOTKEY_TOGGLE", "Start / Stop"), ("HOTKEY_SOFTSTOP", "Soft-stop (test)"),
               ("HOTKEY_QUIT", "Quit macro"), ("HOTKEY_POPOUT", "Toggle pop-out pill")]
    panels.append(
        '<section class="panel" id="pkeys"><div class="phead"><h2>Keybinds</h2>'
        '<p class="chint">Click a box, then press the key combo you want. They work globally while the macro is running. Stop &amp; Start the macro to apply.</p></div>'
        '<div class="rows">'
        + "".join(f'<label class="row"><span class="lbl">{lbl}</span>'
                  f'<button type="button" class="btn2 kb" data-kb="{key}" id="kb_{key}">…</button></label>'
                  for key, lbl in _kbrows)
        + '</div><div class="calactions"><button type="button" class="btn" id="savekeys">Save keybinds</button></div></section>')

    nav("auto", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M5 19l9-9"/><path d="M14 4l.8 1.7 1.7.8-1.7.8L14 9l-.8-1.7L11.5 6.5 13.2 5.7z"/><path d="M18.5 11l.6 1.3 1.3.6-1.3.6-.6 1.3-.6-1.3-1.3-.6 1.3-.6z"/></svg>', "Auto-build")
    _abf = [("ab_luck", "Luck (in-game)"), ("ab_cap", "Pan Capacity"),
            ("ab_ds", "Dig Strength"), ("ab_dspeed", "Dig Speed %"),
            ("ab_ss", "Shake Strength"), ("ab_sspeed", "Shake Speed %"),
            ("ab_ws", "Walk Speed (optional)"), ("ab_n", "Digs to fill (blank = auto)")]
    panels.append(
        '<section class="panel" id="pauto"><div class="phead"><h2>Auto-build</h2>'
        '<p class="chint">Enter your in-game stats (Settings &rarr; Stats menu) and generate a build tuned to them. It works out how many digs fill your pan, the dig hold, how many shakes empty it, the shake click timing, the settles and walk-back &mdash; then applies them to your settings.</p></div>'
        '<div class="abgrid">'
        + "".join(f'<label class="abf"><span>{lbl}</span>'
                  f'<input type="number" id="{fid}" placeholder="\u2014"></label>' for fid, lbl in _abf)
        + '</div>'
        '<div class="calactions"><button type="button" class="btn" id="abgen">\u2728 Generate &amp; apply build</button></div>'
        '<div class="abreadout" id="abreadout"></div></section>')

    # Settings tabs
    for title, items in SECTIONS:
        icon = TAB_ICON.get(title, "•")
        _search = (title + " " + " ".join(l for _k, l, _t, _d in items)).lower().replace('"', "")
        nav(title, icon, title, search=_search)
        rows = []
        for key, label, typ, default in items:
            if typ == "bool":
                ctl = (f'<span class="switch"><input type="checkbox" data-key="{key}" '
                       f'data-type="bool"><span class="track"><span class="knob">'
                       f'</span></span></span>')
            elif typ == "str":
                ctl = (f'<input type="text" data-key="{key}" data-type="str" '
                       f'style="width:240px;text-align:left">')
            else:
                ctl = f'<input type="number" data-key="{key}" data-type="int">'
            rows.append(f'<label class="row"><span class="lbl">{label}{_qm(key)}</span>'
                        f'{ctl}</label>')
        hint = SECTION_HINT.get(title, "")
        panels.append(
            f'<section class="panel" id="p_{title}"><div class="phead"><h2>{title}</h2>'
            f'<p class="chint">{hint}</p></div>'
            f'<div class="rows">{"".join(rows)}</div></section>')

    panels.append(
        '<div id="wizard" class="wizwrap"><div class="wiz">'
        '<div class="wizhd"><span class="wizstep" id="wizstep"></span>'
        '<button type="button" class="btn2" id="wizx">✕</button></div>'
        '<h2 id="wiztitle"></h2><p class="chint" id="wizbody"></p>'
        '<div class="wizres" id="wizresult"></div>'
        '<div class="wizact"><button type="button" class="btn" id="wizdetect">Detect</button>'
        '<button type="button" class="btn2" id="wizmanual">Pick manually</button>'
        '<button type="button" class="btn2" id="wiznext">Skip ›</button></div>'
        '<div class="wizdots" id="wizdots"></div></div></div>')
    nav_html = {n["id"]: n["html"] for n in navs}
    PINNED = ["run", "cal", "relics", "hist", "keys", "auto"]
    GROUPS = [
        ("Setup", ["Easy tuning", "Window"]),
        ("Modes", ["Treasure chest"]),
        ("Engine tuning", ["Mode / Dig", "Walk back into water", "Shake",
                           "Return to land (dig-probe)"]),
        ("Recovery", ["Recovery / safety", "Recovery movement (jitter taps)",
                      "Smart / experimental"]),
        ("Alerts & limits", ["Notifications", "Auto-stop"]),
    ]
    side = ['<input id="navsearch" class="navsearch" type="text" '
            'placeholder="Search settings…" spellcheck="false">']
    side.append('<div class="navpinned">'
                + "".join(nav_html.get(t, "") for t in PINNED) + '</div>')
    used = set(PINNED)
    for gname, titles in GROUPS:
        kids = "".join(nav_html.get(t, "") for t in titles if t in nav_html)
        used.update(titles)
        if not kids:
            continue
        side.append(
            f'<div class="navgroup collapsed" data-group="{gname}">'
            f'<button type="button" class="grouphdr">'
            f'<span>{gname}</span><span class="chev">›</span></button>'
            f'<div class="groupkids">{kids}</div></div>')
    leftover = "".join(n["html"] for n in navs if n["id"] not in used)
    if leftover:
        side.append('<div class="navgroup collapsed" data-group="Other">'
                    '<button type="button" class="grouphdr"><span>Other</span>'
                    '<span class="chev">›</span></button>'
                    f'<div class="groupkids">{leftover}</div></div>')
    return HTML.replace("{{NAV}}", "".join(side)).replace("{{PANELS}}", "".join(panels))


HTML = r"""<!doctype html><html><head><meta charset="utf-8"><link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet"><style>
 :root{--bg:#1a1816;--bg2:#1f1d1a;--panel:#232120;--head:#1b1a18;--line:#332f2a;
  --line2:#423d35;--txt:#ece4d6;--mut:#9c9183;--dim:#6a6253;--accent:#c2924c;
  --accent-lit:#e0b873;--accent2:#7d9b63;--teal-lit:#9bc07e;--green:#7faf5d;
  --field:#191714;--nav:#161412;--sand-dim:rgba(194,146,76,.13);
  --sand-glow:rgba(194,146,76,.26);--ease:cubic-bezier(.22,1,.36,1)}
 *{box-sizing:border-box} html,body{height:100%;margin:0}
 body{background:var(--bg);color:var(--txt);font:13.5px/1.5 "Inter",-apple-system,
  BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;display:flex;flex-direction:column}
 .topbar{flex:0 0 auto;background:var(--head);border-bottom:1px solid var(--line);
  padding:12px 18px;display:flex;align-items:center;gap:10px}
 .brand{font-size:15px;font-weight:600;letter-spacing:-.01em} .brand b{color:var(--accent-lit);font-weight:600} .grow{flex:1}
 button{font:inherit;font-weight:600;border:0;border-radius:9px;padding:9px 14px;cursor:pointer;transition:transform .12s var(--ease),filter .15s,background .15s}
 .btn{background:var(--accent);color:#241a02} .btn:hover{filter:brightness(1.06);transform:translateY(-1px)}
 .btn2{background:#2a2418;color:#e9e0cf} .btn2:hover{background:#352d1c}
 input,select{font:inherit}
 .topfield{background:var(--field);color:var(--txt);border:1px solid var(--line2);border-radius:8px;
  padding:8px 10px;transition:border-color .15s,box-shadow .15s} .topfield.sm{width:140px}
 .body{flex:1;display:flex;min-height:0}
 .side{flex:0 0 208px;background:var(--nav);border-right:1px solid var(--line);
  padding:16px 12px;overflow-y:auto}
 .tab{display:flex;align-items:center;gap:11px;width:100%;text-align:left;
  background:transparent;color:var(--mut);border-radius:8px;padding:8px 11px;margin-bottom:1px;
  font-weight:500;position:relative;transition:color .15s,background .15s}
 .tab .ti{width:17px;display:flex;align-items:center;justify-content:center;opacity:.65}
 .tab .ti svg{width:16px;height:16px}
 .tab:hover{color:var(--txt)} .tab:hover .ti{opacity:1}
 .tab.active{background:var(--sand-dim);color:var(--accent-lit)}
 .navsearch{width:100%;background:var(--field);color:var(--txt);border:1px solid var(--line2);border-radius:8px;padding:7px 10px;margin-bottom:12px;font:inherit}
 .navsearch::placeholder{color:var(--dim)}
 .navsearch:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 2px var(--sand-dim)}
 .navpinned{margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid var(--line)}
 .navgroup{margin-bottom:1px}
 .grouphdr{display:flex;align-items:center;justify-content:space-between;width:100%;background:transparent;color:var(--dim);text-transform:uppercase;letter-spacing:.05em;font-size:10.5px;font-weight:700;padding:9px 11px 4px;border-radius:8px}
 .grouphdr:hover{color:var(--mut)}
 .grouphdr .chev{transition:transform .15s var(--ease);opacity:.55;font-size:15px;font-weight:400}
 .navgroup:not(.collapsed) .grouphdr .chev{transform:rotate(90deg)}
 .navgroup.collapsed .groupkids{display:none}
 .tab.hidden,.navgroup.hidden{display:none}
 .kb{min-width:120px;text-align:center;font-variant:tabular-nums} .kb.armed{background:var(--accent);color:#241a02}
 .wizwrap{position:fixed;inset:0;background:rgba(8,7,6,.62);display:none;align-items:center;justify-content:center;z-index:50}
 .wiz{background:var(--panel);border:1px solid var(--line2);border-radius:16px;padding:22px;width:440px;max-width:92vw;box-shadow:0 20px 60px rgba(0,0,0,.5)}
 .wizhd{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
 .wizstep{color:var(--accent-lit);font-weight:700;font-size:11px;letter-spacing:.05em;text-transform:uppercase}
 .wiz h2{margin:.15em 0 .35em} .wiz .chint{font-size:14px;line-height:1.55}
 .wizres{min-height:22px;margin:10px 0;font-weight:600}
 .wizres .ok{color:var(--teal-lit)} .wizres .no{color:#e0a07a}
 .wizact{display:flex;gap:8px;margin-top:6px} .wizact .btn{flex:1}
 .wizdots{display:flex;gap:6px;justify-content:center;margin-top:14px}
 .wizdots i{width:7px;height:7px;border-radius:50%;background:var(--line2)} .wizdots i.on{background:var(--accent-lit)}
 .abgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px;max-width:560px}
 .abf{display:flex;flex-direction:column;gap:5px} .abf span{color:var(--mut);font-size:12px}
 .abf input{background:var(--field);color:var(--txt);border:1px solid var(--line2);border-radius:8px;padding:9px 10px;font:inherit}
 .abf input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 2px var(--sand-dim)}
 .abreadout{margin-top:14px;background:var(--field);border:1px solid var(--line);border-radius:10px;padding:14px;display:none;line-height:1.6;font-size:13px;max-width:560px}
 .abreadout.show{display:block} .abreadout h4{margin:.1em 0 .5em;color:var(--accent-lit)}
 .abreadout code{color:var(--teal-lit);font-variant:tabular-nums}
 .abreadout table{width:100%;border-collapse:collapse;margin-top:10px}
 .abreadout td{padding:4px 8px;border-top:1px solid var(--line)}
 .abreadout td:first-child{color:var(--mut)} .abreadout td:last-child{text-align:right;color:var(--txt)}
 .tab.active .ti{opacity:1}
 .tab.active:before{content:"";position:absolute;left:0;top:7px;bottom:7px;width:2px;border-radius:2px;background:var(--accent)}
 .navsep{height:1px;background:var(--line);margin:10px 4px}
 .pretitle{color:var(--dim);font-size:10.5px;font-weight:600;text-transform:uppercase;letter-spacing:.8px;margin:14px 8px 7px}
 .chip{display:block;width:100%;text-align:left;background:var(--panel);color:#cfc6b4;
  border:1px solid var(--line);border-radius:8px;padding:8px 11px;margin-bottom:6px;font-size:13px;
  transition:border-color .15s,color .15s}
 .chip:hover{border-color:var(--line2);color:var(--txt)}
 .content{flex:1;overflow-y:auto;padding:28px 32px}
 .panel{display:none} .panel.active{display:block}
 .phead{margin:0 0 18px} .phead h2{margin:0;font-size:19px;font-weight:600;letter-spacing:-.01em} .chint{margin:5px 0 0;color:var(--mut);font-size:13px;max-width:620px}
 .rows{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:2px 18px;max-width:580px}
 .row{display:flex;align-items:center;gap:14px;padding:13px 0;border-bottom:1px solid var(--line)}
 .rows .row:last-child{border-bottom:0} .lbl{flex:1;color:var(--txt)}
 .qm{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;
  margin-left:7px;border-radius:50%;background:var(--line2);color:var(--mut);font-size:11px;
  font-weight:600;cursor:help;transition:background .15s,color .15s} .qm:hover{background:var(--accent);color:#241a02}
 .tip{position:fixed;display:none;max-width:300px;background:var(--panel);color:var(--txt);
  border:1px solid var(--line2);border-radius:9px;padding:9px 11px;font-size:12.5px;
  line-height:1.45;z-index:300;box-shadow:0 10px 30px rgba(0,0,0,.6)}
 input[type=number]{width:104px;background:var(--field);color:var(--txt);border:1px solid var(--line2);
  border-radius:8px;padding:9px 11px;text-align:right;transition:border-color .15s,box-shadow .15s}
 input[type=number]:focus,input[type=text]:focus,.rname:focus{outline:0;border-color:var(--accent);
  box-shadow:0 0 0 3px rgba(194,146,76,.25)}
 .switch{position:relative;display:inline-flex} .switch input{display:none}
 .track{width:46px;height:26px;background:#39342a;border-radius:999px;position:relative;cursor:pointer}
 .knob{position:absolute;top:3px;left:3px;width:20px;height:20px;background:#fff;border-radius:50%;transition:left .15s}
 .switch input:checked + .track{background:var(--accent2)}
 .switch input:checked + .track .knob{left:23px}
 .switch.sm .track{width:38px;height:22px} .switch.sm .knob{width:16px;height:16px}
 .switch.sm input:checked + .track .knob{left:19px}
 .runbtns{display:flex;align-items:center;gap:10px;margin-bottom:14px}
 .big{padding:11px 20px;font-size:15px} .go{background:var(--accent2);color:#14260f}
 .stop{background:#3a2330;color:#ffb4b4} .stop:disabled,.go:disabled{opacity:.5;cursor:default}
 .rstate{color:var(--mut);margin-left:6px}
 .statsbar{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:0 0 12px}
 .histbox{max-width:680px;display:flex;flex-direction:column;gap:8px}
 .hrow{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:11px 13px}
 .hr-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
 .hr-top b{font-weight:600;color:var(--txt)}
 .hr-reason{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--accent-lit);
  background:var(--sand-dim);border:1px solid var(--sand-glow);border-radius:999px;padding:2px 9px}
 .hr-stats{color:var(--mut);font-size:12.5px;font-family:ui-monospace,Menlo,monospace}
 .hempty{color:var(--mut);font-size:13px}
 .stat{flex:1;background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:10px 12px;text-align:center}
 .sv{font-size:20px;font-weight:600;color:var(--txt);font-variant-numeric:tabular-nums}
 .sl{font-size:11.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.5px;margin-top:2px}
 .log{background:#080706;border:1px solid var(--line);border-radius:12px;padding:13px 15px;
  height:calc(100vh - 320px);overflow-y:auto;white-space:pre-wrap;font:12px ui-monospace,
  Menlo,monospace;color:#c2c8ba;margin:0}
 .calrows{max-width:720px}
 .frstat{font-size:12px;opacity:.75;margin-right:8px}
 .calrow{display:flex;align-items:center;gap:14px;background:var(--panel);
  border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:8px}
 .calinfo{flex:1} .calname{font-weight:600;color:var(--txt)}
 .caldesc{color:var(--mut);font-size:12.5px;margin-top:2px}
 .calval{display:flex;align-items:center;gap:6px;min-width:236px;justify-content:flex-end}
 .cnum{width:62px;background:var(--field);color:var(--txt);border:1px solid var(--line2);border-radius:7px;padding:7px 8px;text-align:right;font-size:13px}
 .chex{width:82px;background:var(--field);color:var(--txt);border:1px solid var(--line2);border-radius:7px;padding:7px 8px;font:12px ui-monospace,Menlo,monospace}
 .cnum:focus,.chex:focus{outline:0;border-color:var(--accent);box-shadow:0 0 0 3px rgba(194,146,76,.2)}
 .calactions{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap}
 .calxy{font:13px ui-monospace,Menlo,monospace;color:var(--accent-lit)}
 .calsw2{width:22px;height:22px;border-radius:6px;border:1px solid var(--line);background:#000}
 .calbtn.armed{background:var(--accent2);color:#14260f}
 .autocal{display:flex;align-items:center;gap:12px;flex-wrap:wrap;max-width:720px;margin-bottom:14px}
 .detout{flex-basis:100%;display:flex;flex-direction:column;gap:5px;margin-top:4px}
 .detrow{display:flex;align-items:center;gap:9px;font:12.5px ui-monospace,Menlo,monospace;color:var(--mut)}
 .detsw{width:18px;height:18px;border-radius:5px;border:1px solid var(--line2);flex-shrink:0}
 .det-ok{color:#7faf5d;font-weight:700} .det-no{color:#e08a5a;font-weight:700}
 .winstat{flex-basis:100%;font:12.5px ui-monospace,Menlo,monospace;color:var(--mut);
  background:#0a0c10;border:1px solid var(--line);border-radius:9px;padding:8px 11px}
 .winstat.ok{color:#7fe8c0;border-color:#1f5b44} .winstat.bad{color:#f2b8b8;border-color:#5b1f1f}
 .caldiv{display:flex;align-items:center;gap:10px;color:var(--mut);font-size:12px;
  max-width:720px;margin:6px 0 12px;text-transform:uppercase;letter-spacing:.5px}
 .caldiv:before,.caldiv:after{content:"";flex:1;height:1px;background:var(--line)}
 .relicwrap{max-width:760px}
 .rrow{display:flex;align-items:center;gap:8px;background:var(--panel);border:1px solid var(--line);
  border-radius:12px;padding:10px 12px;margin-bottom:8px}
 .rrow .rname{flex:1;min-width:120px;background:var(--field);color:var(--txt);border:1px solid var(--line2);
  border-radius:8px;padding:8px 10px} .rrow .rlab{color:var(--mut);font-size:13px}
 .rrow input[type=number]{width:64px}
 .ok{position:fixed;right:16px;bottom:14px;background:#10301f;color:#7fe6b5;
  border:1px solid #1f6b4a;border-radius:9px;padding:8px 12px;font-size:13px;opacity:0;
  transition:opacity .2s} .ok.show{opacity:1}
 .upd{display:none;align-items:center;gap:10px;padding:8px 14px;
   background:linear-gradient(90deg,#8a6a35,#c2924c);color:#241a02;font-size:13px}
 .upd b{font-weight:700} .upd .grow{flex:1}
 .upd button{background:#fff;color:#5a3d0a;border:0;border-radius:6px;
   padding:5px 12px;font-weight:700;cursor:pointer}
 .upd .x{background:transparent;color:#f3e3c5;font-weight:400;padding:5px 8px}
 .upd.crit{background:linear-gradient(90deg,#7a1f1f,#c23b3b);color:#fff}
 .upd.crit button{color:#5a0f0f}

 /* sleek refinements */
 .calrow,.rrow,.stat{transition:border-color .15s,background .15s}
 .calrow:hover,.rrow:hover{border-color:var(--line2)}
 .topfield:focus{outline:0;border-color:var(--accent);box-shadow:0 0 0 3px rgba(194,146,76,.2)}
 ::selection{background:rgba(194,146,76,.3)}
 ::-webkit-scrollbar{width:10px;height:10px}
 ::-webkit-scrollbar-thumb{background:var(--line2);border-radius:6px;border:3px solid transparent;background-clip:padding-box}
 ::-webkit-scrollbar-thumb:hover{background:#4a4230;background-clip:padding-box}
 ::-webkit-scrollbar-track{background:transparent}
 /* ---- entrance + splash + access gate ---- */
 .panel.active{animation:fadeIn .34s var(--ease)}
 @keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
 #splash{position:fixed;inset:0;z-index:1000;background:var(--bg);display:flex;
  flex-direction:column;align-items:center;justify-content:center;gap:16px;
  transition:opacity .5s var(--ease)}
 #splash.hide{opacity:0;pointer-events:none}
 .sp-logo{font-size:30px;font-weight:800;letter-spacing:-.5px;color:var(--txt)}
 .sp-logo b{color:var(--accent-lit)}
 .sp-sub{color:var(--mut);font-size:13px;letter-spacing:.3px}
 .sp-bar{width:180px;height:3px;border-radius:3px;background:var(--line2);overflow:hidden}
 .sp-bar i{display:block;height:100%;width:38%;border-radius:3px;
  background:linear-gradient(90deg,var(--accent),var(--accent-lit));animation:spbar 1.05s var(--ease) infinite}
 @keyframes spbar{0%{transform:translateX(-120%)}100%{transform:translateX(330%)}}
 #gate{position:fixed;inset:0;z-index:990;display:none;background:var(--bg);grid-template-columns:1.05fr .95fr}
 #gate.show{display:grid;animation:fadeIn .4s var(--ease)}
 @media(max-width:820px){#gate.show{grid-template-columns:1fr}.gate-left{display:none}}
 .gate-left{position:relative;overflow:hidden;border-right:1px solid var(--line);
  padding:40px;display:flex;flex-direction:column;background:var(--bg2)}
 .gate-left .gl-top{position:relative;z-index:2;display:flex;align-items:center;gap:11px;font-weight:700;font-size:18px}
 .gl-pk{width:32px;height:32px;border-radius:9px;background:var(--sand-dim);border:1px solid var(--sand-glow);
  display:flex;align-items:center;justify-content:center;font-size:16px}
 .pp-gem{width:1em;height:1em;vertical-align:-.16em}
 .gl-pk .pp-gem{width:18px;height:18px} .gc-logo .gl-pk .pp-gem{width:15px;height:15px}
 .sp-logo{display:inline-flex;align-items:center;gap:10px} .sp-logo .pp-gem{width:30px;height:30px}
 .brand{display:inline-flex;align-items:center;gap:8px} .brand .pp-gem{width:18px;height:18px}
 .gate-left .gl-quote{position:relative;z-index:2;margin-top:auto}
 .gate-left .gl-quote p{font-size:19px;line-height:1.55;color:var(--txt);max-width:30ch}
 .gate-left .gl-quote footer{margin-top:12px;color:var(--mut);font-size:13px;font-family:ui-monospace,Menlo,monospace}
 .gate-fade{position:absolute;inset:0;z-index:1;background:linear-gradient(to top,var(--bg2),transparent 60%)}
 .gate-paths{position:absolute;inset:0;z-index:0;pointer-events:none}
 .gate-paths svg{width:100%;height:100%}
 .gate-paths path{fill:none;stroke:var(--accent);vector-effect:non-scaling-stroke}
 @keyframes flow{0%{stroke-dashoffset:300}100%{stroke-dashoffset:0}}
 @keyframes drift{0%{transform:translate(-2.5%,-1.5%)}100%{transform:translate(2.5%,1.5%)}}
 .gate-paths svg g{animation:drift 24s ease-in-out infinite alternate;transform-origin:center}
 .gate-right{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:32px}
 .gate-card{width:100%;max-width:360px;animation:fadeIn .55s var(--ease) .1s both}
 .gate-card .gc-logo{display:flex;align-items:center;gap:10px;font-weight:700;font-size:16px;margin-bottom:28px}
 .gate-card .gc-logo .gl-pk{width:28px;height:28px;font-size:14px}
 .gate-card h1{font-size:24px;letter-spacing:-.3px;margin:0 0 6px;color:var(--txt)}
 .gate-card .gc-sub{color:var(--mut);font-size:14.5px;margin-bottom:22px;line-height:1.55}
 .gate-field{position:relative;margin-bottom:12px}
 .gate-field input{width:100%;background:var(--field);color:var(--txt);border:1px solid var(--line2);
  border-radius:11px;padding:13px 14px 13px 40px;font:inherit;font-size:15px;letter-spacing:2px;
  text-transform:uppercase;transition:border-color .2s,box-shadow .2s}
 .gate-field input::placeholder{letter-spacing:1px;text-transform:none;color:var(--dim)}
 .gate-field input:focus{outline:0;border-color:var(--accent);box-shadow:0 0 0 3px rgba(194,146,76,.22)}
 .gate-field .ic{position:absolute;left:12px;top:50%;transform:translateY(-50%);font-size:15px;opacity:.8}
 #gateGo{width:100%;background:var(--accent);color:#241a02;font-size:15px;padding:13px;border-radius:11px;margin-top:2px}
 #gateGo:hover{filter:brightness(1.05);transform:translateY(-1px)}
 #gateGo[disabled]{opacity:.6;transform:none;cursor:default}
 .gate-err{min-height:18px;margin-top:11px;color:#f0a6a6;font-size:13px}
 .gate-foot{margin-top:24px;color:var(--dim);font-size:12.5px;line-height:1.6}
</style></head><body>
 <div id="splash">
   <div class="sp-logo"><svg class="pp-gem" viewBox="0 0 24 24" fill="none"><path d="M6.5 4h11l4 5.2L12 21 2.5 9.2z" fill="#fff"/><path d="M2.5 9.2h19M6.5 4l2.6 5.2L12 21M17.5 4l-2.6 5.2L12 21M9.1 9.2h5.8" stroke="#0a0908" stroke-opacity=".32" stroke-width=".8" stroke-linejoin="round"/></svg> Prospectors <b>Plus</b></div>
   <div class="sp-sub">loading&hellip;</div>
   <div class="sp-bar"><i></i></div>
 </div>
 <div id="gate">
   <div class="gate-left">
     <div class="gate-paths" id="gatePaths"></div>
     <div class="gate-fade"></div>
     <div class="gl-top"><span class="gl-pk"><svg class="pp-gem" viewBox="0 0 24 24" fill="none"><path d="M6.5 4h11l4 5.2L12 21 2.5 9.2z" fill="#fff"/><path d="M2.5 9.2h19M6.5 4l2.6 5.2L12 21M17.5 4l-2.6 5.2L12 21M9.1 9.2h5.8" stroke="#0a0908" stroke-opacity=".32" stroke-width=".8" stroke-linejoin="round"/></svg></span> Prospectors Plus</div>
     <div class="gl-quote">
       <p>&ldquo;Set it up once, hand it a code, and let it dig. Prospectors Plus runs the whole loop while you&rsquo;re away.&rdquo;</p>
       <footer>~ Prospectors Plus</footer>
     </div>
   </div>
   <div class="gate-right">
     <div class="gate-card">
       <div class="gc-logo"><span class="gl-pk"><svg class="pp-gem" viewBox="0 0 24 24" fill="none"><path d="M6.5 4h11l4 5.2L12 21 2.5 9.2z" fill="#fff"/><path d="M2.5 9.2h19M6.5 4l2.6 5.2L12 21M17.5 4l-2.6 5.2L12 21M9.1 9.2h5.8" stroke="#0a0908" stroke-opacity=".32" stroke-width=".8" stroke-linejoin="round"/></svg></span> Prospectors Plus</div>
       <h1>Enter your access code</h1>
       <div class="gc-sub">Prospectors Plus is invite-only. Enter the access code you were given to unlock it.</div>
       <form id="gateForm">
         <div class="gate-field">
           <span class="ic">&#128273;</span>
           <input id="gateCode" placeholder="PPLUS-XXXX-XXXX" autocomplete="off" spellcheck="false">
         </div>
         <button type="submit" id="gateGo" class="btn">Unlock</button>
         <div class="gate-err" id="gateErr"></div>
       </form>
       <div class="gate-foot">Don&rsquo;t have a code? Ask whoever shared Prospectors Plus with you.</div>
     </div>
   </div>
 </div>

 <div class="upd" id="upd">
   <span id="updtext"></span><span class="grow"></span>
   <button id="upddl">Update now</button>
   <button class="x" id="updx">Later</button>
 </div>
 <div class="topbar">
   <div class="brand"><svg class="pp-gem" viewBox="0 0 24 24" fill="none"><path d="M6.5 4h11l4 5.2L12 21 2.5 9.2z" fill="#fff"/><path d="M2.5 9.2h19M6.5 4l2.6 5.2L12 21M17.5 4l-2.6 5.2L12 21M9.1 9.2h5.8" stroke="#0a0908" stroke-opacity=".32" stroke-width=".8" stroke-linejoin="round"/></svg> Prospectors <b>Plus</b></div>
   <div class="grow"></div>
   <input class="topfield sm" id="buildname" placeholder="build name">
   <button class="btn2" id="savebuild">Save build</button>
   <select class="topfield sm" id="buildlist"><option value="">Load build…</option></select>
   <button class="btn2" id="delbuild" title="Delete selected build">✕</button>
   <button class="btn2" id="popout" title="Pop out a floating control">⤢ Pop out</button>
   <button class="btn" id="savebtn">Save settings</button>
 </div>
 <div class="body">
   <nav class="side">
     {{NAV}}
     <div class="navsep"></div>
     <div class="pretitle">Quick presets</div>
     <button type="button" class="chip" id="pv1">v1 · fast 1-dig</button>
     <button type="button" class="chip" id="pv2">v2 · multi-dig</button>
     <button type="button" class="chip" id="pdef">Reset defaults</button>
     <div class="pretitle" style="margin-top:10px">My builds (full)</div>
     <div id="buildchips"></div>
   </nav>
   <div class="content">{{PANELS}}</div>
 </div>
 <div class="ok" id="toast"></div>
<script>
 let DEF={},V1={},V2={};
 const $=s=>document.querySelector(s), $$=s=>document.querySelectorAll(s);
 const fields=()=>$$('[data-key]');
 function setVals(v){fields().forEach(el=>{const k=el.dataset.key;
   if(el.dataset.type==='bool')el.checked=!!v[k]; else el.value=(v[k]??'');});}
 function collect(){const o={};fields().forEach(el=>{const k=el.dataset.key,t=el.dataset.type;
   o[k]=(t==='bool')?el.checked:(t==='str')?el.value:parseInt(el.value||'0',10);});return o;}
 function preset(p){fields().forEach(el=>{const k=el.dataset.key; if(!(k in p))return;
   if(el.dataset.type==='bool')el.checked=!!p[k]; else el.value=p[k];});}
 function toast(t){const e=$('#toast');e.textContent=t;e.classList.add('show');
   clearTimeout(window._tt);window._tt=setTimeout(()=>e.classList.remove('show'),1800);}
 window.addLog=t=>{const l=$('#log');l.textContent+=t+"\n";l.scrollTop=l.scrollHeight;};
 window.setRunning=r=>{$('#startbtn').disabled=r;$('#stopbtn').disabled=!r;
   $('#rstate').textContent=r?'running':'stopped';};
 window.setStats=s=>{if(!s)return;const m=Math.floor((s.runtime_s||0)/60),
   sec=String((s.runtime_s||0)%60).padStart(2,'0');
   $('#st_run').textContent=m+':'+sec; $('#st_cyc').textContent=s.cycles||0;
   $('#st_rate').textContent=s.pans_per_hr||0; $('#st_rec').textContent=s.recoveries||0;
   const _set=(id,v)=>{const e=document.getElementById(id);if(e)e.textContent=v;};
   _set('st_nud',s.nudges||0);_set('st_rel',s.relics_used||0);
   _set('st_safe',s.safe_stops||0);_set('st_hard',s.hard_stops||0);};
 async function loadHistory(){let list=[];try{list=await window.pywebview.api.run_history();}catch(e){}
   const box=document.getElementById('histbox');if(!box)return;
   if(!list||!list.length){box.innerHTML='<div class="hempty">No runs yet \u2014 finish a session and it shows up here.</div>';return;}
   box.innerHTML=list.map(r=>{const m=Math.floor((r.runtime_s||0)/60);
     return '<div class="hrow"><div class="hr-top"><b>'+(r.ended||'run')+'</b>'+
       '<span class="hr-reason">'+(r.reason||r.stop_reason||'')+'</span></div>'+
       '<div class="hr-stats">'+(r.cycles||0)+' pans \u00b7 '+(r.pans_per_hr||0)+'/hr \u00b7 '+m+' min \u00b7 '+
       (r.recoveries||0)+' rec \u00b7 '+(r.nudges||0)+' nudges \u00b7 '+(r.relics_used||0)+' relics \u00b7 '+
       (r.safe_stops||0)+' safe \u00b7 '+(r.hard_stops||0)+' hard</div></div>';}).join('');}
 (function(){const b=document.getElementById('histrefresh');if(b)b.onclick=loadHistory;})();
 // relics
 function relicRows(){return $$('.rrow');}
 function setRelics(list,enabled){$('#relicsMaster').checked=!!enabled;
   relicRows().forEach((row,i)=>{const r=list[i];
     row.querySelector('.renable').checked=!!r;
     row.querySelector('.rname').value=r?r.name:'';
     row.querySelector('.rmin').value=r?r.minutes:'';
     row.querySelector('.rslot').value=r?r.slot:'';
     row.querySelector('.rclicks').value=r?r.clicks:'';});}
 function collectRelics(){const out=[];relicRows().forEach(row=>{
   if(!row.querySelector('.renable').checked)return;
   const slot=parseInt(row.querySelector('.rslot').value||'0',10);
   if(!slot)return;
   out.push({name:row.querySelector('.rname').value||'Relic',
     minutes:parseInt(row.querySelector('.rmin').value||'10',10),
     slot:slot, clicks:parseInt(row.querySelector('.rclicks').value||'2',10)});});
   return out;}
 // tabs
 $$('.tab').forEach(b=>b.onclick=()=>{$$('.tab').forEach(x=>x.classList.remove('active'));
   $$('.panel').forEach(x=>x.classList.remove('active'));b.classList.add('active');
   const id=b.dataset.tab; const pid=(id==='run'||id==='cal'||id==='relics'||id==='hist'||id==='keys'||id==='auto')?('p'+id):('p_'+id);
   document.getElementById(pid).classList.add('active');
   const _g=b.closest('.navgroup');if(_g)_g.classList.remove('collapsed');
   if(id==='hist')loadHistory();});
 document.querySelectorAll('.grouphdr').forEach(h=>h.onclick=()=>h.closest('.navgroup').classList.toggle('collapsed'));
 (function(){const ns=document.getElementById('navsearch');if(!ns)return;
   ns.addEventListener('input',()=>{const q=ns.value.trim().toLowerCase();
     if(!q){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('hidden'));
       document.querySelectorAll('.navgroup').forEach(g=>{g.classList.remove('hidden');g.classList.add('collapsed');});return;}
     document.querySelectorAll('.navgroup').forEach(g=>{let any=false;
       g.querySelectorAll('.tab').forEach(t=>{const hit=((t.dataset.search||'')+' '+t.textContent.toLowerCase()).includes(q);
         t.classList.toggle('hidden',!hit);if(hit)any=true;});
       g.classList.toggle('hidden',!any);g.classList.toggle('collapsed',!any);});
     document.querySelectorAll('.navpinned .tab').forEach(t=>t.classList.toggle('hidden',!t.textContent.toLowerCase().includes(q)));});})();
 // calibrate: click a button, then click the spot in-game
 let pixels={};
 function setPixels(px){pixels=px||{};
   for(const k in pixels){const xy=pixels[k];
     const cx=document.getElementById('cx_'+k); if(cx)cx.value=xy[0];
     const cy=document.getElementById('cy_'+k); if(cy)cy.value=xy[1];}}
 function setColors(colors){for(const k in (colors||{})){
     const cc=document.getElementById('cc_'+k); if(cc)cc.value=colors[k];
     const cs=document.getElementById('cs_'+k); if(cs&&colors[k])cs.style.background=colors[k];}}
 let capturing=false;
 function showWin(w){const el=$('#winstat');if(!el)return;
   if(w&&w.found){el.className='winstat ok';
     el.textContent='✓ Roblox found · '+w.w+'×'+w.h+' at ('+w.x+', '+w.y+')'+(w.title?(' · '+w.title):'');}
   else{el.className='winstat bad';el.textContent='✕ '+((w&&w.error)||'Roblox window not found');}}
 $$('.calbtn').forEach(btn=>btn.onclick=()=>{
   const key=btn.dataset.pkey;
   const lab=((btn.closest('.calrow')||document).querySelector('.calname')||{}).textContent||key;
   try{window.pywebview.api.start_overlay_calibrate(key,lab);}catch(e){toast('Overlay failed');}});
 window.__calRefresh=async()=>{try{const s=await window.pywebview.api.get_state();
   setPixels(s.pixels||{});setColors(s.colors||{});setFR(s.fr||{});toast('Calibrated ✓');}catch(e){}};
 (function(){const A=()=>window.pywebview.api;
   const WIZ=[
    {t:'Guided calibration',b:'This sets up every detection spot for you. Keep Roblox open with the HUD visible, then click Detect to find the game window.',d:async()=>{const w=await A().detect_roblox();return (w&&w.found)?{ok:true,msg:'Found Roblox '+w.w+'×'+w.h}:{ok:false,error:'Roblox not found — open the game, then Detect.'};},m:null},
    {t:'Capacity bar — RIGHT end',b:'Dig until your capacity bar is <b>completely full</b> (all yellow). Detect — the red ✕ should sit on the <b>right tip</b>. Confirm (or Redo).',d:()=>A().wizard_propose('CAP_RIGHT','Capacity right end'),m:null},
    {t:'Capacity bar — LEFT end',b:'Keep the bar full. Detect — the red ✕ should sit on the <b>left tip</b> (where the bar starts). Confirm (or Redo). This sets the bar width the macro watches to register digs.',d:()=>A().wizard_propose('CAP_LEFT','Capacity left end'),m:null},
    {t:'\u201cPan\u201d prompt',b:'Stand in the <b>water</b> so the white \u201cPan\u201d prompt shows at the bottom. Then Detect.',d:()=>A().wizard_propose('PAN_PIX','Pan'),m:null},
    {t:'\u201cCollect Deposit\u201d prompt',b:'Step onto <b>land</b> so \u201cCollect Deposit\u201d shows. Then Detect.',d:()=>A().wizard_propose('DEPOSIT_PIX','Collect Deposit'),m:null},
    {t:'\u201cShake\u201d prompt',b:'Begin a <b>shake</b> so the \u201cShake\u201d prompt shows. Then Detect.',d:()=>A().wizard_propose('SHAKE_PIX','Shake'),m:null},
    {t:'All set \u2713',b:'Calibration saved. Re-run anytime, or fine-tune any single spot manually in the list below. Use <b>Test detection</b> to confirm.',d:null,m:null}];
   let i=0;const wrap=document.getElementById('wizard');const W=id=>document.getElementById(id);
   function render(){const s=WIZ[i];W('wizstep').textContent='Step '+(i+1)+' / '+WIZ.length;
     W('wiztitle').textContent=s.t;W('wizbody').innerHTML=s.b;W('wizresult').innerHTML='';
     W('wizdetect').style.display=s.d?'':'none';W('wizmanual').style.display=s.m?'':'none';
     W('wiznext').textContent=(i===WIZ.length-1)?'Done':'Skip ›';
     W('wizdots').innerHTML=WIZ.map((_,k)=>'<i class="'+(k===i?'on':'')+'"></i>').join('');}
   function openW(){i=0;render();wrap.style.display='flex';}function closeW(){wrap.style.display='none';}
   W('wizx').onclick=closeW;
   W('wiznext').onclick=()=>{if(i>=WIZ.length-1){closeW();return;}i++;render();};
   W('wizdetect').onclick=async()=>{const s=WIZ[i];const btn=W('wizdetect');btn.disabled=true;const o=btn.textContent;btn.textContent='Detecting…';
     let r;try{r=await s.d();}catch(e){r={ok:false,error:String(e)};}btn.disabled=false;btn.textContent=o;
     if(r&&r.ok){let msg;
       if('detected' in r){msg=r.detected?'Found a spot — check the red ✕ in the overlay, then Confirm (or Redo to pick it yourself).':'Could not auto-find it — click the spot in the overlay, then Confirm.';}
       else{msg=r.msg||'Done';}
       W('wizresult').innerHTML='<span class="ok">\u2713 '+msg+'</span>';try{window.__calRefresh&&window.__calRefresh();}catch(e){}
       W('wiznext').textContent=(i===WIZ.length-1)?'Done':'Next ›';}
     else{W('wizresult').innerHTML='<span class="no">'+((r&&r.error)||'Could not detect')+'</span>';}};
   W('wizmanual').onclick=()=>{const s=WIZ[i];if(s.m){try{A().start_overlay_calibrate(s.m,s.t);}catch(e){}}};
   const wb=document.getElementById('wizbtn');if(wb)wb.onclick=openW;})();
 let fr={text:null,top:null,bottom:null,open:null,home:null};
 function setFR(f){if(!f)return;
   if(f.FR_SCAN_X){fr.text={scan_x:f.FR_SCAN_X,rgb:f.FR_TEXT_RGB};
     const s=document.getElementById('frstat_text');if(s)s.textContent='x='+f.FR_SCAN_X;
     const sw=document.getElementById('frsw_text');if(sw&&f.FR_TEXT_RGB)sw.style.background='rgb('+f.FR_TEXT_RGB.join(',')+')';}
   if(f.FR_BOX_TOP){fr.top=f.FR_BOX_TOP;const s=document.getElementById('frstat_top');if(s)s.textContent='y='+f.FR_BOX_TOP;}
   if(f.FR_BOX_BOTTOM){fr.bottom=f.FR_BOX_BOTTOM;const s=document.getElementById('frstat_bottom');if(s)s.textContent='y='+f.FR_BOX_BOTTOM;}
   if(f.FR_OPEN_PIXEL&&(f.FR_OPEN_PIXEL[0]||f.FR_OPEN_PIXEL[1])){fr.open=f.FR_OPEN_PIXEL;
     const s=document.getElementById('frstat_open');if(s)s.textContent='('+f.FR_OPEN_PIXEL[0]+', '+f.FR_OPEN_PIXEL[1]+')';}
   if(f.FR_HOME_PIXEL&&(f.FR_HOME_PIXEL[0]||f.FR_HOME_PIXEL[1])){fr.home=f.FR_HOME_PIXEL;
     const s=document.getElementById('frstat_home');if(s)s.textContent='('+f.FR_HOME_PIXEL[0]+', '+f.FR_HOME_PIXEL[1]+')';}}
 const FRKEY={text:'FR_TEXT',top:'FR_BOX_TOP',bottom:'FR_BOX_BOTTOM',open:'FR_OPEN_PIXEL',home:'FR_HOME_PIXEL'};
 $$('.frbtn').forEach(btn=>btn.onclick=()=>{
   const key=FRKEY[btn.dataset.frk];
   const lab=((btn.closest('.calrow')||document).querySelector('.calname')||{}).textContent||key;
   try{window.pywebview.api.start_overlay_calibrate(key,lab);}catch(e){toast('Overlay failed');}});
 function collectFR(){const o={};
   if(fr.text){o.FR_SCAN_X=fr.text.scan_x;o.FR_TEXT_RGB=fr.text.rgb;}
   if(fr.top!=null)o.FR_BOX_TOP=fr.top;
   if(fr.bottom!=null)o.FR_BOX_BOTTOM=fr.bottom;
   if(fr.open)o.FR_OPEN_PIXEL=fr.open;
   if(fr.home)o.FR_HOME_PIXEL=fr.home;
   return o;}
 function collectPixels(){const o={};document.querySelectorAll('.calrow').forEach(row=>{
   const k=row.dataset.pkey,x=row.querySelector('.cx').value,y=row.querySelector('.cy').value;
   if(x!==''&&y!=='')o[k]=[parseInt(x,10),parseInt(y,10)];});return o;}
 function collectColors(){const o={};document.querySelectorAll('.calrow').forEach(row=>{
   const k=row.dataset.pkey,c=(row.querySelector('.chex').value||'').trim();if(c)o[k]=c;});return o;}
 document.querySelectorAll('.chex').forEach(inp=>inp.addEventListener('input',()=>{
   const cs=inp.closest('.calrow').querySelector('.calsw2');if(cs)cs.style.background=inp.value;}));
 $('#savepixels').onclick=async()=>{await window.pywebview.api.save_pixels(collectPixels(),collectColors(),collectFR());toast('Calibration saved');};
 let _detT=null;
 function _sw(c){return c?('rgb('+c.r+','+c.g+','+c.b+')'):'#000';}
 function _drow(label,c,verdict){return '<div class="detrow"><span class="detsw" style="background:'+_sw(c)+'"></span>'
   +'<b>'+label+'</b> '+(c?('rgb('+c.r+','+c.g+','+c.b+')'):'(not set)')+' '+(verdict||'')+'</div>';}
 (function(){const b=document.getElementById('dettest');if(!b)return;
   b.onclick=async()=>{
     if(_detT){clearInterval(_detT);_detT=null;b.textContent='Test detection (live)';document.getElementById('detout').innerHTML='';return;}
     b.textContent='Stop test';
     const tick=async()=>{let r;try{r=await window.pywebview.api.sample_pixels();}catch(e){return;}
       const box=document.getElementById('detout');if(!box)return;
       if(!r||r.error){box.innerHTML='<div class="detrow det-no">Test failed: '+((r&&r.error)||'')+'</div>';return;}
       const p=r.pixels||{};let h='';
       h+=_drow('Capacity', p.CAP_FULL_PIXEL, r.cap_full?'<span class="det-ok">FULL \u2713</span>':'<span class="det-no">not full</span>');
       h+=_drow('Pan cue', p.PAN_PIX, r.PAN_PIX_white?'<span class="det-ok">visible</span>':'');
       h+=_drow('Shake cue', p.SHAKE_PIX, r.SHAKE_PIX_white?'<span class="det-ok">visible</span>':'');
       h+=_drow('Deposit cue', p.DEPOSIT_PIX, r.DEPOSIT_PIX_white?'<span class="det-ok">visible</span>':'');
       h+=_drow('Dig green', p.DIG_TRIGGER_PIXEL, '');
       box.innerHTML=h;};
     tick();_detT=setInterval(tick,300);};})();
 $('#exportcal').onclick=async()=>{const r=await window.pywebview.api.export_calibration();
   if(r&&r.ok){toast('Saved: '+r.path);}else if(r&&r.cancelled){}else{toast('Export failed: '+((r&&r.error)||''));}};
 $('#importcal').onclick=()=>$('#importfile').click();
 $('#importfile').onchange=async ev=>{const f=ev.target.files[0];if(!f)return;
   const text=await f.text();let r;try{r=await window.pywebview.api.import_calibration(text);}catch(_){r={ok:false,error:'could not read file'};}
   if(r&&r.ok){setPixels(r.pixels||{});setColors(r.colors||{});toast('Imported calibration \u2713');}else{toast('Import failed: '+((r&&r.error)||'invalid'));}
   ev.target.value='';};
 // presets
 $('#pv1').onclick=()=>preset(V1); $('#pv2').onclick=()=>preset(V2); $('#pdef').onclick=()=>preset(DEF);
 // save / run
 $('#savebtn').onclick=async()=>{const n=await window.pywebview.api.save_config(collect());toast('Saved '+n+' settings');};
 $('#popout').onclick=()=>{try{window.pywebview.api.popout();}catch(e){}};
 let hotkeys={};
 function hkLabel(s){if(!s||!s.code)return 'unset';let p=[];if(s.ctrl)p.push('Ctrl');if(s.alt)p.push('Alt');if(s.shift)p.push('Shift');
   let c=s.code;if(c.indexOf('Key')===0)p.push(c.slice(3));else if(c.indexOf('Digit')===0)p.push(c.slice(5));else p.push(c);return p.join('+');}
 function setHotkeys(hk){hotkeys=hk||{};for(const k in hotkeys){const b=document.getElementById('kb_'+k);if(b)b.textContent=hkLabel(hotkeys[k]);}}
 document.querySelectorAll('.kb').forEach(b=>{b.onclick=()=>{
   if(b._arm){return;} b._arm=true; const prev=b.textContent; b.textContent='press keys…'; b.classList.add('armed');
   const onk=(e)=>{e.preventDefault();e.stopPropagation();
     if(['Control','Alt','Shift','Meta'].indexOf(e.key)>=0)return;
     const spec={ctrl:e.ctrlKey,alt:e.altKey,shift:e.shiftKey,code:e.code};
     hotkeys[b.dataset.kb]=spec;b.textContent=hkLabel(spec);b.classList.remove('armed');b._arm=false;
     window.removeEventListener('keydown',onk,true);};
   window.addEventListener('keydown',onk,true);};});
 $('#savekeys').onclick=async()=>{try{await window.pywebview.api.save_hotkeys(hotkeys);toast('Keybinds saved — Stop & Start the macro to apply');}catch(e){toast('Save failed');}};
 (function(){
   function rOf(x){x=Math.max(0,x);return Math.max(0,4.03266e-9*x*x*x-1.68935e-5*x*x+0.0255557*x+0.206594);}
   function cl(v,a,b){return Math.max(a,Math.min(b,Math.round(v)));}
   function gv(id){const el=document.getElementById(id);return el?Math.max(0,Number(el.value)||0):0;}
   function build(){
     const L=gv('ab_luck'),C=Math.max(1,gv('ab_cap')),DS=Math.max(1,gv('ab_ds')),
       d=Math.max(1,gv('ab_dspeed')),s=Math.max(1,gv('ab_ss')),ss=gv('ab_sspeed'),
       wsR=gv('ab_ws'),ws=wsR>0?wsR:16,nOv=gv('ab_n');
     const r=rOf(ss);
     const n=nOv>0?Math.round(nOv):Math.max(1,Math.ceil(C/DS));
     const shToEmpty=C/s, shakeClicks=Math.max(1,Math.ceil(shToEmpty));
     const cyc=1000/Math.max(0.1,r);
     const clickMs=cl(cyc*0.55,10,140), gapMs=cl(cyc*0.45,6,140);
     const shDur=C/(r*s);
     const holdMs=cl(Math.max(shDur*1000,shakeClicks*(clickMs+gapMs))+350,300,6000);
     const digHold=cl(55000/d,8,600);
     const digPer=190/d, fillMs=cl(digPer*1000+90,120,1500);
     const wf=Math.max(0.45,Math.min(1.4,16/Math.max(8,ws)));
     const panBack=cl(220*wf,90,420), depMax=cl(1300*wf,500,2500),
       landSet=cl(60*wf,20,160), landNudge=cl(95*wf,40,220);
     const cycleSec=C/(r*s)+1.5+190*n/d, ppm=cycleSec>0?60/cycleSec:0;
     const settings={PERFECT:false,DIG_CLICK_MS:digHold,DIG_SPEED:Math.round(d),
       MAX_DIGS_TO_FILL:n+1,DIG_FILL_MS:fillMs,PRE_DIG_SETTLE_MS:60,
       PAN_BACK_MAX_MS:panBack,WATER_EXTRA_BACK_MS:0,SHAKE_MOMENTUM_W:true,
       SHAKE_CLICKS:shakeClicks,SHAKE_CLICK_MS:clickMs,SHAKE_CLICK_GAP_MS:gapMs,
       SHAKE_HOLD_MS:holdMs,SHAKE_BAIL_MS:500,SHAKE_START_DELAY_MS:0,POST_SHAKE_SETTLE_MS:150,
       DEPOSIT_MAX_MS:depMax,LAND_SETTLE_MS:landSet,DIG_PROBE_MS:320,PROBE_GAP_MS:80,
       LAND_PROBE_NUDGE_MS:landNudge,LAND_DIG_TRIES:5};
     const metrics={r:r,shToEmpty:shToEmpty,shakeClicks:shakeClicks,n:n,shDur:shDur,
       digPer:digPer,cycleSec:cycleSec,ppm:ppm,rolls:L*Math.sqrt(C)};
     return {settings:settings,metrics:metrics};
   }
   function render(b){const m=b.metrics,S=b.settings,f=(x,p)=>Number(x).toFixed(p===undefined?2:p);
     let h='<h4>Computed for your stats</h4>';
     h+='<div>Effective rolls/pan <code>'+Math.round(m.rolls)+'</code> &middot; shakes/sec <code>'+f(m.r)+'</code> &middot; cycle <code>'+f(m.cycleSec)+'s</code> &middot; pans/min <code>'+f(m.ppm)+'</code></div>';
     const rows=[
       ['Digs to fill pan', m.n+'  (capacity ÷ dig strength)'],
       ['Dig hold', S.DIG_CLICK_MS+' ms  (55000 ÷ dig speed)'],
       ['Wait for fill after dig', S.DIG_FILL_MS+' ms  (190÷speed/dig)'],
       ['Shakes to empty pan', m.shakeClicks+'  (capacity ÷ shake strength = '+f(m.shToEmpty)+')'],
       ['Each shake click', S.SHAKE_CLICK_MS+' ms + '+S.SHAKE_CLICK_GAP_MS+' ms gap  (1000 ÷ '+f(m.r)+'/s)'],
       ['Shake hold timeout', S.SHAKE_HOLD_MS+' ms'],
       ['Walk back into water', S.PAN_BACK_MAX_MS+' ms  (scaled by walk speed)'],
       ['Walk forward to land', S.DEPOSIT_MAX_MS+' ms'],
       ['Settle after landing', S.LAND_SETTLE_MS+' ms'],
     ];
     h+='<table>';for(let i=0;i<rows.length;i++)h+='<tr><td>'+rows[i][0]+'</td><td>'+rows[i][1]+'</td></tr>';h+='</table>';
     const el=document.getElementById('abreadout');if(el){el.innerHTML=h;el.classList.add('show');}
   }
   window.setAutobuild=function(a){if(!a)return;for(const k in a){const el=document.getElementById(k);if(el&&a[k])el.value=a[k];}};
   const gen=document.getElementById('abgen');
   if(gen)gen.onclick=async()=>{
     const b=build();
     preset(b.settings);
     try{await window.pywebview.api.save_config(collect());}catch(e){}
     try{await window.pywebview.api.save_autobuild({ab_luck:gv('ab_luck'),ab_cap:gv('ab_cap'),
       ab_ds:gv('ab_ds'),ab_dspeed:gv('ab_dspeed'),ab_ss:gv('ab_ss'),ab_sspeed:gv('ab_sspeed'),
       ab_ws:gv('ab_ws'),ab_n:gv('ab_n')});}catch(e){}
     render(b);
     toast('Build generated & applied ✓ — Save settings keeps it');
   };
 })();
 $('#saverelics').onclick=async()=>{const n=await window.pywebview.api.save_relics(collectRelics(),$('#relicsMaster').checked);toast('Saved '+n+' relic(s)');};
 $('#startbtn').onclick=async()=>{await window.pywebview.api.launch(collect(),collectRelics(),$('#relicsMaster').checked);setRunning(true);toast('Launched — Ctrl+K to start');};
 $('#stopbtn').onclick=async()=>{await window.pywebview.api.stop();setRunning(false);};
 // builds — a build saves ALL settings + relics; loading applies them all
 async function loadBuild(name){if(!name)return;
   const e=await window.pywebview.api.load_build(name);if(!e)return;
   setVals(e); setRelics(e.RELICS||[], e.RELICS_ENABLED); toast('Loaded build "'+name+'"');}
 function renderBuildChips(list){const box=$('#buildchips');box.innerHTML='';
   if(!list||!list.length){box.innerHTML='<div style="color:#6b7280;font-size:12px;'+
     'padding:2px 4px">none yet — type a name + Save build</div>';return;}
   list.forEach(n=>{const b=document.createElement('button');b.type='button';
     b.className='chip';b.textContent=n;b.onclick=()=>loadBuild(n);box.appendChild(b);});}
 function fillBuilds(list){const sel=$('#buildlist');sel.innerHTML='<option value="">Load build…</option>';
   list.forEach(n=>{const o=document.createElement('option');o.value=n;o.textContent=n;sel.appendChild(o);});
   renderBuildChips(list);}
 $('#savebuild').onclick=async()=>{const name=$('#buildname').value.trim();
   if(!name){toast('Enter a build name');return;}
   await window.pywebview.api.save_build(name,collect(),collectRelics(),$('#relicsMaster').checked);
   const st=await window.pywebview.api.get_state();fillBuilds(st.builds);$('#buildlist').value=name;
   toast('Build "'+name+'" saved (all settings)');};
 $('#buildlist').onchange=()=>loadBuild($('#buildlist').value);
 $('#delbuild').onclick=async()=>{const name=$('#buildlist').value;if(!name){toast('Pick a build to delete');return;}
   const list=await window.pywebview.api.delete_build(name);fillBuilds(list);toast('Deleted "'+name+'"');};
 // custom tooltip (native title tooltips don't show in the app window)
 const _tip=document.createElement('div');_tip.className='tip';document.body.appendChild(_tip);
 document.addEventListener('mouseover',e=>{const q=e.target.closest('.qm');if(!q)return;
   _tip.textContent=q.dataset.tip||'';_tip.style.display='block';
   const r=q.getBoundingClientRect();
   _tip.style.left=Math.max(8,Math.min(r.left,window.innerWidth-308))+'px';
   _tip.style.top=(r.bottom+6)+'px';});
 document.addEventListener('mouseout',e=>{if(e.target.closest('.qm'))_tip.style.display='none';});
 // dig speed -> auto-fill dig hold (100% = 550ms, hold = 55000/speed)
 (function(){const ds=document.querySelector('[data-key="DIG_SPEED"]'),
   dh=document.querySelector('[data-key="DIG_CLICK_MS"]');
   if(ds&&dh)ds.addEventListener('input',()=>{const s=parseFloat(ds.value);
     if(s>0)dh.value=Math.round(55000/s);});})();
 let _updUrl='',_updInstaller='';
 async function checkUpdate(){try{const u=await window.pywebview.api.check_update();
   if(u&&u.update){_updUrl=u.url;_updInstaller=u.installer||'';
     $('#updtext').innerHTML='<b>'+(u.critical?'Critical update required':'Update available')+
       '</b> — v'+u.version+(u.notes?(' · '+u.notes):'')+' (you have v'+u.current+')';
     if(u.critical){$('#upd').classList.add('crit');const x=$('#updx');if(x)x.style.display='none';}
     $('#upd').style.display='flex';}}catch(e){}}
 (function(){const dl=$('#upddl'),x=$('#updx');
   if(dl)dl.onclick=async()=>{dl.disabled=true;dl.textContent='Updating…';
     let r={};try{r=await window.pywebview.api.do_update(_updInstaller);}catch(e){r={ok:false};}
     if(r&&r.ok){$('#updtext').innerHTML='<b>Updating…</b> the app will reinstall and reopen automatically.';}
     else{dl.disabled=false;dl.textContent='Update now';
       toast(r&&r.manual?'Opened the download page':'Update failed — opening download page');}};
   if(x)x.onclick=()=>{$('#upd').style.display='none';};})();
 async function init(){const s=await window.pywebview.api.get_state();
   DEF=s.defaults;V1=s.v1;V2=s.v2;setVals(s.values);setRunning(s.running);
   setRelics(s.relics||[],s.relics_enabled);fillBuilds(s.builds||[]);setPixels(s.pixels||{});setColors(s.colors||{});setFR(s.fr||{});setHotkeys(s.hotkeys||{});setAutobuild(s.autobuild||{});
   checkUpdate();loadHistory();
}
 window.addEventListener('pywebviewready',boot);
 if(window.pywebview&&window.pywebview.api)boot();

 // ---- splash + access gate ----
 function _api(){return window.pywebview&&window.pywebview.api;}
 function genPaths(){const box=document.getElementById('gatePaths');if(!box||box.dataset.done)return;
   let s='<svg viewBox="0 0 696 316" preserveAspectRatio="xMidYMid slice"><g>';
   [1,-1].forEach(position=>{for(let i=0;i<44;i++){
     const d='M-'+(380-i*5*position)+' -'+(189+i*6)+'C-'+(380-i*5*position)+' -'+(189+i*6)+' -'+(312-i*5*position)+' '+(216-i*6)+' '+(152-i*5*position)+' '+(343-i*6)+'C'+(616-i*5*position)+' '+(470-i*6)+' '+(684-i*5*position)+' '+(875-i*6)+' '+(684-i*5*position)+' '+(875-i*6);
     const w=(0.4+i*0.02).toFixed(2),op=(0.06+i*0.01).toFixed(3),dur=(6+Math.random()*7).toFixed(1),del=(Math.random()*-13).toFixed(1),dir=position>0?'normal':'reverse';
     s+='<path d="'+d+'" pathLength="300" stroke-width="'+w+'" style="stroke-dasharray:150 150;stroke-opacity:'+op+';animation:flow '+dur+'s linear '+del+'s infinite;animation-direction:'+dir+'"/>';}});
   s+='</g></svg>';box.innerHTML=s;box.dataset.done='1';}
 function splashHide(){const s=document.getElementById('splash');if(!s)return;
   s.classList.add('hide');setTimeout(()=>{s.style.display='none';},520);}
 function gateShow(){genPaths();const g=document.getElementById('gate');if(g)g.classList.add('show');
   setTimeout(()=>{const c=document.getElementById('gateCode');if(c)c.focus();},140);}
 function gateHide(){const g=document.getElementById('gate');if(g)g.classList.remove('show');}
 async function boot(){let a={unlocked:false};try{a=await _api().access_state();}catch(e){}
   await new Promise(r=>setTimeout(r,650));splashHide();
   if(a&&a.unlocked){init();}else{gateShow();}}
 (function(){const f=document.getElementById('gateForm');if(!f)return;
   f.addEventListener('submit',async e=>{e.preventDefault();
     const inp=document.getElementById('gateCode'),btn=document.getElementById('gateGo'),err=document.getElementById('gateErr');
     const code=(inp.value||'').trim();if(!code){err.textContent='Enter your access code.';return;}
     btn.disabled=true;btn.textContent='Checking…';err.textContent='';
     let r={};try{r=await _api().verify_access(code);}catch(e){r={ok:false,error:'Something went wrong. Try again.'};}
     btn.disabled=false;btn.textContent='Unlock';
     if(r&&r.ok){gateHide();init();}else{err.textContent=(r&&r.error)||'That code did not work.';inp.select();}});})();
</script></body></html>"""


def main():
    global _window, _pill, _overlay
    try:
        import webview
    except ImportError:
        print("pywebview not installed; opening the browser settings UI instead.\n"
              "For the native app:  pip3 install pywebview --break-system-packages")
        import runpy
        runpy.run_path(os.path.join(HERE, "prospecting_ui.py"), run_name="__main__")
        return
    api = Api()
    _window = webview.create_window("Prospectors Plus", html=build_html(),
                                    js_api=api, width=980, height=880,
                                    min_size=(860, 660))
    global _pill, _overlay
    try:
        import Quartz as _Q
        _b = _Q.CGDisplayBounds(_Q.CGMainDisplayID())
        _sw, _sh = int(_b.size.width), int(_b.size.height)
    except Exception:
        _sw, _sh = 1440, 900
    try:
        _pill = webview.create_window(
            "Prospectors Plus", html=PILL_HTML, js_api=api,
            width=272, height=132, frameless=True, on_top=True,
            easy_drag=True, resizable=False, hidden=True)
    except Exception as _e:
        print("[pill] precreate failed: %s" % _e)
    try:
        _overlay = webview.create_window(
            "Calibrate", html=_OVERLAY_HTML, js_api=api,
            x=0, y=0, width=_sw, height=_sh,
            frameless=True, on_top=True, hidden=True)
    except Exception as _e:
        print("[overlay] precreate failed: %s" % _e)
    webview.start()
    try:
        api._save_history()      # window closed while running -> log it
    except Exception:
        pass


if __name__ == "__main__":
    main()
