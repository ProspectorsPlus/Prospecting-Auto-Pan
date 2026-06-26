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

# ---- version + update channel (compared to the website's version.json) -------
# >>> EDIT THESE THREE LINES to point at your website <<<
VERSION             = "1.0.0"
UPDATE_MANIFEST_URL = "https://prospectorsplus.github.io/Prospecting-Auto-Pan/version.json"
DOWNLOAD_PAGE_URL   = "https://prospectorsplus.github.io/Prospecting-Auto-Pan/"

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
        try:
            req = urllib.request.Request(UPDATE_MANIFEST_URL,
                                         headers={"User-Agent": "ProspectorsPlus"})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read().decode("utf-8"))
            latest = str(data.get("version", "")).strip()
            if latest and _ver_tuple(latest) > _ver_tuple(VERSION):
                return {"update": True, "version": latest, "current": VERSION,
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
        with open(CONFIG_FILE, "w") as f:
            json.dump(cur, f, indent=2)
        return "saved"

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
        return "launched"

    def stop(self):
        if self.proc is not None:
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    self.proc.send_signal(sig)
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
 :root{--bg:#0f1115;--panel:#171a21;--head:#1c2029;--line:#262b35;--txt:#e8eaed;
  --mut:#8b94a3;--accent:#3b82f6;--accent2:#10b981;--field:#0c0e12;--nav:#13161c}
 *{box-sizing:border-box} html,body{height:100%;margin:0}
 body{background:var(--bg);color:var(--txt);font:14px/1.45 -apple-system,
  BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;display:flex;flex-direction:column}
 .topbar{flex:0 0 auto;background:#12151b;border-bottom:1px solid var(--line);
  padding:12px 18px;display:flex;align-items:center;gap:10px}
 .brand{font-size:16px;font-weight:700} .brand b{color:var(--accent2)} .grow{flex:1}
 button{font:inherit;font-weight:600;border:0;border-radius:9px;padding:9px 14px;cursor:pointer}
 .btn{background:var(--accent);color:#fff} .btn:hover{filter:brightness(1.08)}
 .btn2{background:#2a3340;color:#dfe5ee} .btn2:hover{background:#33404f}
 input,select{font:inherit}
 .topfield{background:var(--field);color:#fff;border:1px solid #2c333f;border-radius:8px;
  padding:8px 10px} .topfield.sm{width:140px}
 .body{flex:1;display:flex;min-height:0}
 .side{flex:0 0 196px;background:var(--nav);border-right:1px solid var(--line);
  padding:12px 10px;overflow-y:auto}
 .tab{display:flex;align-items:center;gap:10px;width:100%;text-align:left;
  background:transparent;color:#b9c1cd;border-radius:9px;padding:10px 11px;margin-bottom:3px}
 .tab .ti{width:18px;text-align:center;opacity:.85}
 .tab:hover{background:#1d222b;color:#fff} .tab.active{background:#223049;color:#fff}
 .navsep{height:1px;background:var(--line);margin:10px 4px}
 .pretitle{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.6px;margin:4px 8px 6px}
 .chip{display:block;width:100%;text-align:left;background:#1b212b;color:#cdd5e0;
  border:1px solid var(--line);border-radius:8px;padding:8px 11px;margin-bottom:6px;font-size:13px}
 .chip:hover{background:#222a35}
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
  box-shadow:0 0 0 3px rgba(59,130,246,.25)}
 .switch{position:relative;display:inline-flex} .switch input{display:none}
 .track{width:46px;height:26px;background:#39414e;border-radius:999px;position:relative;cursor:pointer}
 .knob{position:absolute;top:3px;left:3px;width:20px;height:20px;background:#fff;border-radius:50%;transition:left .15s}
 .switch input:checked + .track{background:var(--accent2)}
 .switch input:checked + .track .knob{left:23px}
 .switch.sm .track{width:38px;height:22px} .switch.sm .knob{width:16px;height:16px}
 .switch.sm input:checked + .track .knob{left:19px}
 .runbtns{display:flex;align-items:center;gap:10px;margin-bottom:14px}
 .big{padding:11px 20px;font-size:15px} .go{background:var(--accent2);color:#06281c}
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
 .calbtn.armed{background:var(--accent2);color:#06281c}
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
   background:linear-gradient(90deg,#1f6feb,#388bfd);color:#fff;font-size:13px}
 .upd b{font-weight:700} .upd .grow{flex:1}
 .upd button{background:#fff;color:#0b3a82;border:0;border-radius:6px;
   padding:5px 12px;font-weight:700;cursor:pointer}
 .upd .x{background:transparent;color:#cfe0ff;font-weight:400;padding:5px 8px}
</style></head><body>
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
 $('#detectwin').onclick=async()=>{const w=await window.pywebview.api.detect_roblox();showWin(w);};
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
 window.addEventListener('pywebviewready',init);
 if(window.pywebview&&window.pywebview.api)init();
</script></body></html>"""


def main():
    global _window
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
