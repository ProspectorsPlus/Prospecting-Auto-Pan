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
import shutil
import signal
import threading
import subprocess
import webbrowser
import urllib.request
import hashlib

# ---- version + update channel -------------------------------------------------
# VERSION is compared against the "version" field in the JSON the app downloads
# from UPDATE_MANIFEST_URL. If the site reports a newer version the app shows an
# "Update available" banner that opens DOWNLOAD_PAGE_URL in the browser.
# >>> EDIT THESE THREE LINES to point at your website <<<
VERSION             = "1.0.1"
UPDATE_MANIFEST_URL = "https://prospectorsplus.github.io/Prospecting-Auto-Pan/version.json"
DOWNLOAD_PAGE_URL   = "https://prospectorsplus.github.io/Prospecting-Auto-Pan/"
ACCESS_CODES_URL    = "https://prospectorsplus.github.io/Prospecting-Auto-Pan/codes.json"

FROZEN = getattr(sys, "frozen", False)        # True when bundled by PyInstaller
HERE = (os.path.dirname(sys.executable) if FROZEN
        else os.path.dirname(os.path.abspath(__file__)))


def _data_dir():
    """Where read/write files (config, builds) live. When frozen we can't write
    next to the .exe (Program Files is read-only for normal users), so use
    %LOCALAPPDATA%\\Prospectors Plus. In dev it's just the script folder."""
    if FROZEN:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "Prospectors Plus")
    else:
        d = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(d, exist_ok=True)
    return d


def _resource(name):
    """Path to a bundled read-only resource (works frozen via _MEIPASS)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


DATA_DIR = _data_dir()
CONFIG_FILE = os.path.join(DATA_DIR, "prospecting_config.json")
BUILDS_FILE = os.path.join(DATA_DIR, "prospecting_builds.json")
MACRO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prospecting_old.py")

# First run when frozen: seed the writable config from the bundled default so the
# baked-in webhook URL/secret are present without the user touching anything.
if FROZEN and not os.path.exists(CONFIG_FILE):
    try:
        shutil.copyfile(_resource("prospecting_config.json"), CONFIG_FILE)
    except OSError:
        pass


def _ver_tuple(s):
    """'1.2.3' -> (1,2,3); tolerant of junk so a bad manifest never crashes."""
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
# window (x_fraction, y_fraction). When this is populated, brand-new users can
# Auto-calibrate with zero clicking. It's seeded by calibrating once with Roblox
# open (save records the ratios) and baking the result into prospecting_config.json.
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


def _dpi_aware():
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _roblox_rect():
    """Find the Roblox GAME window and return its client area in screen pixels.

    Returns {found:True, x, y, w, h, title} (client top-left + size), or
    {found:False, error:"..."}. We scan all visible top-level windows instead of
    matching an exact title, because the title isn't always exactly "Roblox" and
    FindWindow is brittle. The Roblox player's window class is WINDOWSCLIENT; we
    also accept any visible window whose title contains "Roblox" (but not
    "Roblox Studio"), and pick the largest such window."""
    try:
        import ctypes
        from ctypes import wintypes
        _dpi_aware()
        u = ctypes.windll.user32
        candidates = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lp):
            try:
                if not u.IsWindowVisible(hwnd):
                    return True
                n = u.GetWindowTextLengthW(hwnd)
                tbuf = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(hwnd, tbuf, n + 1)
                title = tbuf.value or ""
                cbuf = ctypes.create_unicode_buffer(256)
                u.GetClassNameW(hwnd, cbuf, 256)
                cls = cbuf.value or ""
                is_rbx = (cls == "WINDOWSCLIENT" or
                          ("Roblox" in title and "Studio" not in title))
                if not is_rbx:
                    return True
                rc = wintypes.RECT()
                if not u.GetClientRect(hwnd, ctypes.byref(rc)):
                    return True
                w, h = rc.right - rc.left, rc.bottom - rc.top
                if w < 320 or h < 240:           # skip tiny/loading windows
                    return True
                pt = wintypes.POINT(0, 0)
                u.ClientToScreen(hwnd, ctypes.byref(pt))
                candidates.append((w * h, {"found": True, "x": int(pt.x),
                                   "y": int(pt.y), "w": int(w), "h": int(h),
                                   "title": title or cls}))
            except Exception:
                pass
            return True

        u.EnumWindows(_cb, 0)
        if candidates:
            candidates.sort(key=lambda c: c[0], reverse=True)
            return candidates[0][1]
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

    # ---- settings ----
    def get_state(self):
        saved = load_saved()
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in saved.items() if k in DEFAULTS})
        relics = saved.get("RELICS", DEFAULT_RELICS)
        pixels = {}
        for key in PIXEL_DEFAULTS:
            pixels[key] = list(saved.get(key, PIXEL_DEFAULTS[key]))
        return {"values": merged, "running": self.proc is not None,
                "v1": PRESET_V1, "v2": PRESET_V2, "defaults": DEFAULTS,
                "relics": relics, "relics_enabled": bool(saved.get("RELICS_ENABLED", False)),
                "builds": self.list_builds(), "pixels": pixels}

    # ---- updates ----
    def app_version(self):
        return VERSION

    def check_update(self):
        """Fetch the version manifest from the website and compare. Never raises
        -- if offline or the URL isn't set yet, just reports no update."""
        try:
            req = urllib.request.Request(UPDATE_MANIFEST_URL,
                                         headers={"User-Agent": "ProspectorsPlus"})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read().decode("utf-8"))
            latest = str(data.get("version", "")).strip()
            if latest and _ver_tuple(latest) > _ver_tuple(VERSION):
                return {"update": True, "version": latest,
                        "current": VERSION,
                        "url": data.get("url") or DOWNLOAD_PAGE_URL,
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
            req = urllib.request.Request(ACCESS_CODES_URL,
                                         headers={"User-Agent": "ProspectorsPlus"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode("utf-8"))
            hashes = set(data.get("hashes") or [])
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

    def save_pixels(self, pixels):
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
        # pixel as a fraction of the game window. Those ratios are what let
        # Auto-calibrate place the pixels for anyone, at any window size/position.
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
        with open(CONFIG_FILE, "w") as f:
            json.dump(cur, f, indent=2)
        return "saved"

    # ---- window detection + auto-calibrate ----
    def detect_roblox(self):
        """For the UI: report whether the Roblox window is found and where."""
        return _roblox_rect()

    def auto_calibrate(self):
        """Place every pixel automatically from the detected Roblox window using
        the saved ratio profile. No clicking required. Returns a result the UI
        can show, including a clear message if something's missing."""
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
        # persist them exactly like a manual save (incl. bar width)
        self.save_pixels(pixels)
        return {"ok": True, "pixels": pixels, "window": rect,
                "count": len(pixels)}

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

    # ---- calibrate: wait for the user to mark a spot, capture its x/y + colour
    def calibrate_capture(self):
        """Wait for the user to mark a spot, then return its position + colour.

        Two ways to mark: LEFT-CLICK the spot, or hover it and press ENTER (handy
        when clicking the game is fiddly). Press ESC to cancel. Returns
        {x,y,r,g,b} on success, or {error:"..."} / {cancelled:True}."""
        try:
            import ctypes
        except Exception as e:
            return {"error": "ctypes unavailable: %s" % e}
        try:
            import numpy as np
        except Exception:
            return {"error": "Image library (numpy) missing from this build."}
        try:
            import mss
        except Exception:
            return {"error": "Screen-capture library (mss) missing from this build."}
        import time as _t
        try:
            _dpi_aware()
            u = ctypes.windll.user32
            VK_LBUTTON, VK_RETURN, VK_ESCAPE = 0x01, 0x0D, 0x1B

            def k(vk):
                return bool(u.GetAsyncKeyState(vk) & 0x8000)
            # release whatever started this, then wait for the NEXT mark
            t0 = _t.perf_counter()
            while (k(VK_LBUTTON) or k(VK_RETURN)) and _t.perf_counter() - t0 < 0.6:
                _t.sleep(0.01)
            t0 = _t.perf_counter()
            while True:
                if k(VK_ESCAPE):
                    return {"cancelled": True}
                if k(VK_LBUTTON) or k(VK_RETURN):
                    break
                if _t.perf_counter() - t0 > 30:
                    return {"error": "Timed out — no click or Enter within 30s. "
                                     "Click the spot in-game, or hover it and press Enter."}
                _t.sleep(0.008)

            class _P(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
            pt = _P()
            u.GetCursorPos(ctypes.byref(pt))
            x, y = int(pt.x), int(pt.y)
            try:
                # FRESH mss in THIS thread (mss/GDI is thread-bound on Windows).
                with mss.mss() as sct:
                    m = sct.monitors[0]
                    L, T = m["left"], m["top"]
                    R, B = L + m["width"], T + m["height"]
                    bx = min(max(x - 3, L), R - 6)
                    by = min(max(y - 3, T), B - 6)
                    img = np.asarray(sct.grab(
                        {"left": bx, "top": by, "width": 6, "height": 6}))[:, :, :3]
                b, g, r = [int(v) for v in img.reshape(-1, 3).mean(0)]
            except Exception as e:
                return {"error": "Couldn't read that pixel's colour: %s" % e}
            t0 = _t.perf_counter()
            while (k(VK_LBUTTON) or k(VK_RETURN)) and _t.perf_counter() - t0 < 1.0:
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
        # When frozen there is no python.exe to run the .py macro, so re-launch
        # THIS exe with --run-macro (handled in main()). In dev, run the script.
        if FROZEN:
            cmd = [sys.executable, "--run-macro"]
        else:
            cmd = [sys.executable or "python", MACRO_FILE]
        self.proc = subprocess.Popen(
            cmd, cwd=DATA_DIR, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
        return "launched"

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.terminate()      # Windows: clean process kill
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
        return "stopped"

    def _pump(self, proc):
        for line in iter(proc.stdout.readline, ""):
            line = line.rstrip("\n")
            if line.startswith("__STATS__ "):
                _emit_stats(line[10:])        # raw JSON -> live stats panel
                continue
            _emit_log(line)
        rc = proc.wait()
        _emit_log(f"[macro exited, code {rc}]")
        self.proc = None
        _emit_state(False)


_window = None


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


def build_html():
    navs, panels = [], []

    def nav(tabid, icon, label, active=False):
        a = " active" if active else ""
        navs.append(f'<button type="button" class="tab{a}" data-tab="{tabid}">'
                    f'<span class="ti">{icon}</span><span>{label}</span></button>')

    # Run
    nav("run", "▶", "Run", True)
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
        '</div>'
        '<pre id="log" class="log"></pre></section>')

    # Calibrate
    nav("cal", "🎯", "Calibrate")
    calrows = []
    for key, label, desc, _d in PIXEL_FIELDS:
        calrows.append(
            f'<div class="calrow" data-pkey="{key}">'
            f'<div class="calinfo"><div class="calname">{label}</div>'
            f'<div class="caldesc">{desc}</div></div>'
            f'<div class="calval"><span class="calxy" id="cv_{key}">—</span>'
            f'<span class="calsw2" id="cs_{key}"></span></div>'
            f'<button type="button" class="btn2 calbtn" data-pkey="{key}">Calibrate</button>'
            f'</div>')
    panels.append(
        '<section class="panel" id="pcal"><div class="phead"><h2>Calibrate pixels</h2>'
        '<p class="chint">Easiest: open Prospecting in Roblox, then click '
        '<b>Auto-calibrate</b> below — it finds the game window and places every '
        'spot for you. Manual calibration is only a fallback.</p></div>'
        '<div class="autocal">'
        '  <button type="button" id="autocal" class="btn">⚡ Auto-calibrate (detect Roblox)</button>'
        '  <button type="button" id="detectwin" class="btn2">Detect Roblox window</button>'
        '  <div class="winstat" id="winstat">Roblox window: not checked yet</div>'
        '</div>'
        '<div class="caldiv"><span>or calibrate manually</span></div>'
        '<p class="chint" style="margin:0 0 10px">Click a <b>Calibrate</b> button, then '
        'either click the exact spot in-game, or hover it and press <b>Enter</b>. '
        'Press <b>Esc</b> to cancel. Do them all, then <b>Save calibration</b>.</p>'
        f'<div class="calrows">{"".join(calrows)}</div>'
        '<button type="button" id="savepixels" class="btn" style="margin-top:14px">'
        'Save calibration</button></section>')

    # Relics
    nav("relics", "⏱", "Relics")
    rrows = []
    for i in range(MAX_RELIC_ROWS):
        rrows.append(
            f'<div class="rrow" data-i="{i}">'
            f'<span class="switch sm"><input type="checkbox" class="renable">'
            f'<span class="track"><span class="knob"></span></span></span>'
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

    # Settings tabs
    for title, items in SECTIONS:
        icon = TAB_ICON.get(title, "•")
        nav(title, icon, title)
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

    return HTML.replace("{{NAV}}", "".join(navs)).replace("{{PANELS}}", "".join(panels))


HTML = r"""<!doctype html><html><head><meta charset="utf-8"><style>
 :root{--bg:#0c0b09;--bg2:#111009;--panel:#161410;--head:#1b1814;--line:#272318;
  --line2:#373020;--txt:#f0ebe0;--mut:#a09585;--dim:#68604f;--accent:#d4943a;
  --accent-lit:#f0b95a;--accent2:#2cb8c8;--teal-lit:#52d4e2;--green:#3db87a;
  --field:#0f0d08;--nav:#100e09;--sand-dim:rgba(212,148,58,.14);
  --sand-glow:rgba(212,148,58,.24);--ease:cubic-bezier(.22,1,.36,1)}
 *{box-sizing:border-box} html,body{height:100%;margin:0}
 body{background:var(--bg);color:var(--txt);font:14px/1.45 -apple-system,
  BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;display:flex;flex-direction:column}
 .topbar{flex:0 0 auto;background:var(--head);border-bottom:1px solid var(--line);
  padding:12px 18px;display:flex;align-items:center;gap:10px}
 .brand{font-size:16px;font-weight:700} .brand b{color:var(--accent-lit)} .grow{flex:1}
 button{font:inherit;font-weight:600;border:0;border-radius:9px;padding:9px 14px;cursor:pointer;transition:transform .12s var(--ease),filter .15s,background .15s}
 .btn{background:var(--accent);color:#241a02} .btn:hover{filter:brightness(1.06);transform:translateY(-1px)}
 .btn2{background:#2a2418;color:#e9e0cf} .btn2:hover{background:#352d1c}
 input,select{font:inherit}
 .topfield{background:var(--field);color:#fff;border:1px solid #2c333f;border-radius:8px;
  padding:8px 10px} .topfield.sm{width:140px}
 .body{flex:1;display:flex;min-height:0}
 .side{flex:0 0 196px;background:var(--nav);border-right:1px solid var(--line);
  padding:12px 10px;overflow-y:auto}
 .tab{display:flex;align-items:center;gap:10px;width:100%;text-align:left;
  background:transparent;color:#b9c1cd;border-radius:9px;padding:10px 11px;margin-bottom:3px}
 .tab .ti{width:18px;text-align:center;opacity:.85}
 .tab:hover{background:#1c180f;color:var(--txt)} .tab.active{background:var(--sand-dim);color:var(--accent-lit)}
 .navsep{height:1px;background:var(--line);margin:10px 4px}
 .pretitle{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.6px;margin:4px 8px 6px}
 .chip{display:block;width:100%;text-align:left;background:#1a160f;color:#d8cdb8;
  border:1px solid var(--line);border-radius:8px;padding:8px 11px;margin-bottom:6px;font-size:13px}
 .chip:hover{background:#221d12}
 .content{flex:1;overflow-y:auto;padding:18px 22px}
 .panel{display:none} .panel.active{display:block}
 .phead{margin:0 0 12px} .phead h2{margin:0;font-size:18px} .chint{margin:3px 0 0;color:var(--mut)}
 .rows{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:4px 16px;max-width:560px}
 .row{display:flex;align-items:center;gap:14px;padding:12px 0;border-bottom:1px solid #20242d}
 .rows .row:last-child{border-bottom:0} .lbl{flex:1;color:#d7dce4}
 .qm{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;
  margin-left:7px;border-radius:50%;background:#2c3340;color:#9fb0c4;font-size:11px;
  font-weight:700;cursor:help} .qm:hover{background:var(--accent);color:#fff}
 .tip{position:fixed;display:none;max-width:300px;background:#0b0d12;color:#dfe5ee;
  border:1px solid var(--line);border-radius:9px;padding:9px 11px;font-size:12.5px;
  line-height:1.4;z-index:300;box-shadow:0 8px 24px rgba(0,0,0,.5)}
 input[type=number]{width:104px;background:var(--field);color:#fff;border:1px solid #2c333f;
  border-radius:8px;padding:9px 11px;text-align:right}
 input[type=number]:focus,input[type=text]:focus,.rname:focus{outline:0;border-color:var(--accent);
  box-shadow:0 0 0 3px rgba(212,148,58,.25)}
 .switch{position:relative;display:inline-flex} .switch input{display:none}
 .track{width:46px;height:26px;background:#39342a;border-radius:999px;position:relative;cursor:pointer}
 .knob{position:absolute;top:3px;left:3px;width:20px;height:20px;background:#fff;border-radius:50%;transition:left .15s}
 .switch input:checked + .track{background:var(--accent2)}
 .switch input:checked + .track .knob{left:23px}
 .switch.sm .track{width:38px;height:22px} .switch.sm .knob{width:16px;height:16px}
 .switch.sm input:checked + .track .knob{left:19px}
 .runbtns{display:flex;align-items:center;gap:10px;margin-bottom:14px}
 .big{padding:11px 20px;font-size:15px} .go{background:var(--accent2);color:#04222a}
 .stop{background:#3a2330;color:#ffb4b4} .stop:disabled,.go:disabled{opacity:.5;cursor:default}
 .rstate{color:var(--mut);margin-left:6px}
 .statsbar{display:flex;gap:10px;margin:0 0 12px}
 .stat{flex:1;background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:10px 12px;text-align:center}
 .sv{font-size:20px;font-weight:700;color:#e8eaed;font-variant-numeric:tabular-nums}
 .sl{font-size:11.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.5px;margin-top:2px}
 .log{background:#0a0c10;border:1px solid var(--line);border-radius:12px;padding:12px 14px;
  height:calc(100vh - 320px);overflow-y:auto;white-space:pre-wrap;font:12px ui-monospace,
  Menlo,monospace;color:#bfe3d2;margin:0}
 .calrows{max-width:720px}
 .calrow{display:flex;align-items:center;gap:14px;background:var(--panel);
  border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:8px}
 .calinfo{flex:1} .calname{font-weight:600;color:#e8eaed}
 .caldesc{color:var(--mut);font-size:12.5px;margin-top:2px}
 .calval{display:flex;align-items:center;gap:8px;min-width:130px;justify-content:flex-end}
 .calxy{font:13px ui-monospace,Menlo,monospace;color:#bfe3d2}
 .calsw2{width:22px;height:22px;border-radius:6px;border:1px solid var(--line);background:#000}
 .calbtn.armed{background:var(--accent2);color:#04222a}
 .autocal{display:flex;align-items:center;gap:12px;flex-wrap:wrap;max-width:720px;margin-bottom:14px}
 .winstat{flex-basis:100%;font:12.5px ui-monospace,Menlo,monospace;color:var(--mut);
  background:#0a0c10;border:1px solid var(--line);border-radius:9px;padding:8px 11px}
 .winstat.ok{color:#7fe8c0;border-color:#1f5b44} .winstat.bad{color:#f2b8b8;border-color:#5b1f1f}
 .caldiv{display:flex;align-items:center;gap:10px;color:var(--mut);font-size:12px;
  max-width:720px;margin:6px 0 12px;text-transform:uppercase;letter-spacing:.5px}
 .caldiv:before,.caldiv:after{content:"";flex:1;height:1px;background:var(--line)}
 .relicwrap{max-width:760px}
 .rrow{display:flex;align-items:center;gap:8px;background:var(--panel);border:1px solid var(--line);
  border-radius:12px;padding:10px 12px;margin-bottom:8px}
 .rrow .rname{flex:1;min-width:120px;background:var(--field);color:#fff;border:1px solid #2c333f;
  border-radius:8px;padding:8px 10px} .rrow .rlab{color:var(--mut);font-size:13px}
 .rrow input[type=number]{width:64px}
 .ok{position:fixed;right:16px;bottom:14px;background:#10301f;color:#7fe6b5;
  border:1px solid #1f6b4a;border-radius:9px;padding:8px 12px;font-size:13px;opacity:0;
  transition:opacity .2s} .ok.show{opacity:1}
 .upd{display:none;align-items:center;gap:10px;padding:8px 14px;
   background:linear-gradient(90deg,#b07d28,#d4943a);color:#241a02;font-size:13px}
 .upd b{font-weight:700}
 .upd .grow{flex:1}
 .upd button{background:#fff;color:#5a3d0a;border:0;border-radius:6px;
   padding:5px 12px;font-weight:700;cursor:pointer}
 .upd .x{background:transparent;color:#f3e3c5;font-weight:400;padding:5px 8px}

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
 .gate-left .gl-quote{position:relative;z-index:2;margin-top:auto}
 .gate-left .gl-quote p{font-size:19px;line-height:1.55;color:var(--txt);max-width:30ch}
 .gate-left .gl-quote footer{margin-top:12px;color:var(--mut);font-size:13px;font-family:ui-monospace,Menlo,monospace}
 .gate-fade{position:absolute;inset:0;z-index:1;background:linear-gradient(to top,var(--bg2),transparent 60%)}
 .gate-paths{position:absolute;inset:0;z-index:0;pointer-events:none}
 .gate-paths svg{width:100%;height:100%}
 .gate-paths path{fill:none;stroke:var(--accent)}
 @keyframes flow{0%{stroke-dashoffset:var(--L)}100%{stroke-dashoffset:calc(var(--L) * -1)}}
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
 .gate-field input:focus{outline:0;border-color:var(--accent);box-shadow:0 0 0 3px rgba(212,148,58,.22)}
 .gate-field .ic{position:absolute;left:12px;top:50%;transform:translateY(-50%);font-size:15px;opacity:.8}
 #gateGo{width:100%;background:var(--accent);color:#241a02;font-size:15px;padding:13px;border-radius:11px;margin-top:2px}
 #gateGo:hover{filter:brightness(1.05);transform:translateY(-1px)}
 #gateGo[disabled]{opacity:.6;transform:none;cursor:default}
 .gate-err{min-height:18px;margin-top:11px;color:#f0a6a6;font-size:13px}
 .gate-foot{margin-top:24px;color:var(--dim);font-size:12.5px;line-height:1.6}
</style></head><body>
 <div id="splash">
   <div class="sp-logo">&#9935; Prospectors <b>Plus</b></div>
   <div class="sp-sub">loading&hellip;</div>
   <div class="sp-bar"><i></i></div>
 </div>
 <div id="gate">
   <div class="gate-left">
     <div class="gate-paths" id="gatePaths"></div>
     <div class="gate-fade"></div>
     <div class="gl-top"><span class="gl-pk">&#9935;</span> Prospectors Plus</div>
     <div class="gl-quote">
       <p>&ldquo;Set it up once, hand it a code, and let it dig. Prospectors Plus runs the whole loop while you&rsquo;re away.&rdquo;</p>
       <footer>~ Prospectors Plus</footer>
     </div>
   </div>
   <div class="gate-right">
     <div class="gate-card">
       <div class="gc-logo"><span class="gl-pk">&#9935;</span> Prospectors Plus</div>
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
   <button id="upddl">Download update</button>
   <button class="x" id="updx">Later</button>
 </div>
 <div class="topbar">
   <div class="brand">⛏ Prospectors <b>Plus</b></div>
   <div class="grow"></div>
   <input class="topfield sm" id="buildname" placeholder="build name">
   <button class="btn2" id="savebuild">Save build</button>
   <select class="topfield sm" id="buildlist"><option value="">Load build…</option></select>
   <button class="btn2" id="delbuild" title="Delete selected build">✕</button>
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
   $('#st_rate').textContent=s.pans_per_hr||0; $('#st_rec').textContent=s.recoveries||0;};
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
   const id=b.dataset.tab; const pid=(id==='run'||id==='cal'||id==='relics')?('p'+id):('p_'+id);
   document.getElementById(pid).classList.add('active');});
 // calibrate: click a button, then click the spot in-game
 let pixels={};
 function setPixels(px){pixels=px||{};
   for(const k in pixels){const xy=pixels[k];
     const cv=document.getElementById('cv_'+k); if(cv)cv.textContent='('+xy[0]+', '+xy[1]+')';}}
 let capturing=false;
 function showWin(w){const el=$('#winstat');if(!el)return;
   if(w&&w.found){el.className='winstat ok';
     el.textContent='✓ Roblox found · '+w.w+'×'+w.h+' at ('+w.x+', '+w.y+')'+(w.title?(' · '+w.title):'');}
   else{el.className='winstat bad';el.textContent='✕ '+((w&&w.error)||'Roblox window not found');}}
 $$('.calbtn').forEach(btn=>btn.onclick=async()=>{
   if(capturing)return; capturing=true;
   const key=btn.dataset.pkey; btn.classList.add('armed'); btn.textContent='Click or press Enter…';
   const r=await window.pywebview.api.calibrate_capture();
   btn.classList.remove('armed'); btn.textContent='Calibrate'; capturing=false;
   if(r.cancelled){toast('Cancelled');return;}
   if(r.error){toast('Calibrate failed: '+r.error);return;}
   pixels[key]=[r.x,r.y];
   const cv=document.getElementById('cv_'+key); if(cv)cv.textContent='('+r.x+', '+r.y+')';
   const cs=document.getElementById('cs_'+key); if(cs)cs.style.background=`rgb(${r.r},${r.g},${r.b})`;
   toast(key+' set to ('+r.x+', '+r.y+')');});
 // detect window
 $('#detectwin').onclick=async()=>{const w=await window.pywebview.api.detect_roblox();showWin(w);};
 // one-click auto-calibrate from the detected window
 $('#autocal').onclick=async()=>{const b=$('#autocal');const old=b.textContent;
   b.disabled=true;b.textContent='Detecting…';
   const r=await window.pywebview.api.auto_calibrate();
   b.disabled=false;b.textContent=old;
   showWin(r.window||{found:false,error:r.error});
   if(r.ok){setPixels(r.pixels);toast('Auto-calibrated '+r.count+' spots ✓ — saved');}
   else{toast(r.error||'Auto-calibrate failed');}};
 $('#savepixels').onclick=async()=>{await window.pywebview.api.save_pixels(pixels);
   toast('Calibration saved');};
 // presets
 $('#pv1').onclick=()=>preset(V1); $('#pv2').onclick=()=>preset(V2); $('#pdef').onclick=()=>preset(DEF);
 // save / run
 $('#savebtn').onclick=async()=>{const n=await window.pywebview.api.save_config(collect());toast('Saved '+n+' settings');};
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
 // update check (silent; shows a banner only if a newer version exists)
 let _updUrl='';
 async function checkUpdate(){try{const u=await window.pywebview.api.check_update();
   if(u&&u.update){_updUrl=u.url;
     $('#updtext').innerHTML='<b>Update available</b> — v'+u.version+
       (u.notes?(' · '+u.notes):'')+' (you have v'+u.current+')';
     $('#upd').style.display='flex';}}catch(e){}}
 (function(){const dl=$('#upddl'),x=$('#updx');
   if(dl)dl.onclick=()=>window.pywebview.api.open_external(_updUrl);
   if(x)x.onclick=()=>{$('#upd').style.display='none';};})();
 async function init(){const s=await window.pywebview.api.get_state();
   DEF=s.defaults;V1=s.v1;V2=s.v2;setVals(s.values);setRunning(s.running);
   setRelics(s.relics||[],s.relics_enabled);fillBuilds(s.builds||[]);setPixels(s.pixels||{});
   checkUpdate();
   try{window.pywebview.api.detect_roblox().then(showWin);}catch(e){}}
 window.addEventListener('pywebviewready',boot);
 if(window.pywebview&&window.pywebview.api)boot();

 // ---- splash + access gate ----
 function _api(){return window.pywebview&&window.pywebview.api;}
 function genPaths(){const box=document.getElementById('gatePaths');if(!box||box.dataset.done)return;
   let svg='<svg viewBox="0 0 696 316" preserveAspectRatio="xMidYMid slice">';
   [1,-1].forEach(position=>{for(let i=0;i<36;i++){
     const d='M-'+(380-i*5*position)+' -'+(189+i*6)+'C-'+(380-i*5*position)+' -'+(189+i*6)+' -'+(312-i*5*position)+' '+(216-i*6)+' '+(152-i*5*position)+' '+(343-i*6)+'C'+(616-i*5*position)+' '+(470-i*6)+' '+(684-i*5*position)+' '+(875-i*6)+' '+(684-i*5*position)+' '+(875-i*6);
     const w=(0.5+i*0.06).toFixed(2),op=(0.10+i*0.02).toFixed(2),dur=(18+Math.random()*12).toFixed(1),del=(Math.random()*-22).toFixed(1);
     svg+='<path d="'+d+'" stroke-width="'+w+'" style="stroke-opacity:'+op+';animation:flow '+dur+'s linear '+del+'s infinite"/>';}});
   svg+='</svg>';box.innerHTML=svg;box.dataset.done='1';
   box.querySelectorAll('path').forEach(p=>{const L=p.getTotalLength();
     p.style.setProperty('--L',L+'px');p.style.strokeDasharray=L;p.style.strokeDashoffset=L;});}
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
    global _window
    # Frozen macro mode: the bundled exe re-invokes itself with --run-macro to
    # run the actual macro in-process (there is no separate python.exe).
    if FROZEN and "--run-macro" in sys.argv:
        import runpy
        runpy.run_path(_resource("prospecting_old.py"), run_name="__main__")
        return
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
    webview.start()


if __name__ == "__main__":
    main()
