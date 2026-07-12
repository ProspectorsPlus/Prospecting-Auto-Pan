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
import time
import json
import shutil
import signal
import threading
import subprocess
import webbrowser
import urllib.request
import urllib.error
import hashlib

# ---- version + update channel -------------------------------------------------
# VERSION is compared against the "version" field in the JSON the app downloads
# from UPDATE_MANIFEST_URL. If the site reports a newer version the app shows an
# "Update available" banner that opens DOWNLOAD_PAGE_URL in the browser.
# >>> EDIT THESE THREE LINES to point at your website <<<
VERSION             = "4.1.0"
UPDATE_MANIFEST_URL = "https://prospectorsplus.github.io/Prospecting-Auto-Pan/version.json"
DOWNLOAD_PAGE_URL   = "https://prospectorsplus.github.io/Prospecting-Auto-Pan/"
ACCESS_CODES_URL    = "https://prospectorsplus.github.io/Prospecting-Auto-Pan/codes.json"
INSTALLER_URL       = "https://github.com/ProspectorsPlus/Prospecting-Auto-Pan/releases/latest/download/ProspectorsPlusSetup.exe"

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

# Bundled default builds -- these SHIP with the app so every download has a
# ready geode setup on the Builds page (user builds of the same name override
# them; loading one seeds it into the personal builds file).
DEFAULT_BUILDS = {
    "Geode Farm": {
        "GEODE_MODE": True, "GEODE_DIGS_TO_FILL": 0, "GEODE_DIG_MS": 5,
        "GEODE_DELAY_MS": 12000, "GEODE_START_MS": 800, "GEODE_CONFIRM_FULL": True,
        "GEODE_SHAKE_HOLD_MS": 10000, "SHAKE_MOMENTUM_W": True, "SHAKE_CLICKS": 0,
        "SHAKE_CLICK_MS": 60, "SHAKE_CLICK_GAP_MS": 0, "SHAKE_HOLD_MS": 6000,
        "SHAKE_BAIL_MS": 500, "SHAKE_START_CONFIRM_MS": 300,
        "SHAKE_START_RETRIES": 1, "SHAKE_RETRY_DEEPER_MS": 180,
        "SHAKE_STALL_MS": 0, "SHAKE_START_DELAY_MS": 0, "SHAKE_W_LEAD_MS": 50,
        "POST_SHAKE_SETTLE_MS": 150, "PERFECT": False, "DIG_CLICK_MS": 5,
        "DIG_SPEED": 1474, "MAX_DIGS_TO_FILL": 1, "DIG_FILL_MS": 2050,
        "PRE_DIG_SETTLE_MS": 600, "PAN_BACK_MAX_MS": 100, "WATER_EXTRA_BACK_MS": 0,
        "LAND_SETTLE_MS": 0, "EASY_WATER_RETURN_DELAY_MS": 0,
        "SHARDS_DIG_CLICKS": 0, "TREASURE_MODE": False, "CAP_EMPTY_FRAC": 0.04, "RELICS": [], "RELICS_ENABLED": False,
        "_meta": {"desc": "Geode farming \u2014 slow-animation dig then the normal "
                  "momentum shake (not treasure's strafe). Set 'Animation delay "
                  "per dig' to match your geode's fill animation (~12s). Ships "
                  "with Prospectors Plus.",
                  "created": 1752000000, "updated": 1752000000,
                  "used": 0, "builtin": True},
    },
    "Geode Farm 1-Tap": {
        "GEODE_MODE": True, "GEODE_DIGS_TO_FILL": 0, "GEODE_DIG_MS": 15,
        "GEODE_DELAY_MS": 12000, "GEODE_START_MS": 800,
        "GEODE_CONFIRM_FULL": True, "GEODE_SHAKE_HOLD_MS": 10000,
        "SHAKE_MOMENTUM_W": True, "SHAKE_CLICKS": 0, "SHAKE_CLICK_MS": 60,
        "SHAKE_CLICK_GAP_MS": 0, "SHAKE_HOLD_MS": 6000, "SHAKE_BAIL_MS": 500,
        "SHAKE_START_CONFIRM_MS": 300, "SHAKE_START_RETRIES": 1,
        "SHAKE_RETRY_DEEPER_MS": 180, "SHAKE_STALL_MS": 0,
        "SHAKE_START_DELAY_MS": 0, "SHAKE_W_LEAD_MS": 50,
        "POST_SHAKE_SETTLE_MS": 150, "PERFECT": False, "DIG_CLICK_MS": 5,
        "DIG_SPEED": 1474, "MAX_DIGS_TO_FILL": 1, "DIG_FILL_MS": 2050,
        "PRE_DIG_SETTLE_MS": 600, "PAN_BACK_MAX_MS": 100,
        "WATER_EXTRA_BACK_MS": 0, "LAND_SETTLE_MS": 0,
        "EASY_WATER_RETURN_DELAY_MS": 0, "SHARDS_DIG_CLICKS": 0,
        "TREASURE_MODE": False, "CAP_EMPTY_FRAC": 0.04, "RELICS": [], "RELICS_ENABLED": False,
        "_meta": {"desc": "Geode farming, 1-tap variant -- one quick dig fills the "
                  "pan, then the slow momentum shake. Tuned build; calibrate your "
                  "own capacity + dig pixels. Ships with Prospectors Plus.",
                  "created": 1752000000, "updated": 1752000000,
                  "used": 0, "builtin": True},
    },
}


def _builds_all():
    """User builds merged over the bundled defaults (user wins on name clash)."""
    b = {k: json.loads(json.dumps(v)) for k, v in DEFAULT_BUILDS.items()}
    try:
        b.update(_read_json(BUILDS_FILE, {}))
    except Exception:
        pass
    return b
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
    PRESET_GEODE = getattr(_ui, "PRESET_GEODE", {})
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
    PRESET_V1 = PRESET_V2 = PRESET_GEODE = {}
    SECTION_HINT = TAB_ICON = HELP = {}
    PIXEL_FIELDS = []
    PIXEL_DEFAULTS = {}

# Local tuning assistant (offline expert system). Optional: if it fails to load,
# the rest of the app keeps working and the Coach panel just reports it's offline.
try:
    import prospecting_assistant as _coach
except Exception:
    _coach = None

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


# --- Secrets: the personal API key lives in a gitignored, NON-bundled file so it
#     never lands in git or a shipped build. Config keeps only non-secret settings.
SECRETS_FILE = os.path.join(HERE, "prospecting_secrets.json")


def _load_secrets():
    return _read_json(SECRETS_FILE, {}) or {}


def _coach_key():
    """The Coach API key: secrets file first, then legacy config fallback."""
    k = (_load_secrets().get("COACH_API_KEY") or "").strip()
    if k:
        return k
    return (load_saved().get("COACH_API_KEY") or "").strip()


def _save_coach_key(key):
    """Write the API key ONLY to the gitignored secrets file (never the config)."""
    sec = _load_secrets()
    if key == "__CLEAR__":
        sec.pop("COACH_API_KEY", None)
    elif key:
        sec["COACH_API_KEY"] = key.strip()
    try:
        with open(SECRETS_FILE, "w") as f:
            json.dump(sec, f, indent=2)
    except OSError:
        pass


def _coerce(t, v):
    if t == "bool":
        return bool(v)
    if t == "str":
        return str(v)
    if t == "float":
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0
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
_MID_CACHE = None
_REVOKE_CHECKED = False


def _machine_id():
    """Stable per-machine fingerprint (hashed), for the invite machine-lock.
    macOS -> IOPlatformUUID; Windows -> registry MachineGuid; else a random
    salt persisted in config. Never sent raw -- only a sha256 hex is stored /
    beaconed, so it can't identify hardware, only tell two machines apart."""
    global _MID_CACHE
    if _MID_CACHE:
        return _MID_CACHE
    raw = ""
    try:
        if sys.platform == "darwin":
            import subprocess, re as _re
            out = subprocess.check_output(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                timeout=4).decode("utf-8", "ignore")
            m = _re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
            if m:
                raw = m.group(1)
        elif os.name == "nt":
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                               r"SOFTWARE\Microsoft\Cryptography")
            raw, _ = winreg.QueryValueEx(k, "MachineGuid")
    except Exception:
        raw = ""
    if not raw:
        try:
            cur = load_saved()
            raw = cur.get("MACHINE_SALT")
            if not raw:
                raw = hashlib.sha256(os.urandom(16)).hexdigest()
                cur["MACHINE_SALT"] = raw
                with open(CONFIG_FILE, "w") as f:
                    json.dump(cur, f, indent=2)
        except Exception:
            raw = "unknown-machine"
    _MID_CACHE = hashlib.sha256(("PPMID:" + str(raw)).encode("utf-8")).hexdigest()
    return _MID_CACHE



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
              "FR_HOME_PIXEL": list(saved.get("FR_HOME_PIXEL", [0, 0])),
              "SR_TEXT_RGB": list(saved.get("SR_TEXT_RGB", [0, 0, 0])),
              "AUTOPAN_BTN_PIXEL": list(saved.get("AUTOPAN_BTN_PIXEL", [0, 0])),
              "AUTOPAN_ON_RGB": list(saved.get("AUTOPAN_ON_RGB", [0, 0, 0])),
              "AUTOPAN_OFF_RGB": list(saved.get("AUTOPAN_OFF_RGB", [0, 0, 0]))}
        hk = {k: saved.get(k, _HK_DEFAULTS[k]) for k in _HK_DEFAULTS}
        return {"values": merged, "running": self.proc is not None, "fr": fr, "hotkeys": hk,
                "v1": PRESET_V1, "v2": PRESET_V2, "geode": PRESET_GEODE, "defaults": DEFAULTS,
                "relics": relics, "relics_enabled": bool(saved.get("RELICS_ENABLED", False)),
                "builds": self.list_builds(), "pixels": pixels,
                "colors": saved.get("PIXEL_COLORS", {}),
                "autobuild": saved.get("AUTOBUILD", {})}

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
        """Has this PC unlocked with a valid code AND is this the machine the
        code was bound to? (machine-lock: copying the config to another
        computer re-locks it, so a code can't be shared by handing over the
        config.) Also re-checks the published code list once per launch so a
        revoked code actually stops working -- offline / fetch errors NEVER
        lock anyone out (fail open)."""
        global _REVOKE_CHECKED
        cur = load_saved()
        if not cur.get("ACCESS_OK"):
            return {"unlocked": False}
        bound = cur.get("ACCESS_MACHINE")
        mid = _machine_id()
        if bound and bound != mid:
            return {"unlocked": False, "moved": True}
        if not bound:                       # legacy unlock -> bind to this PC
            try:
                cur["ACCESS_MACHINE"] = mid
                with open(CONFIG_FILE, "w") as f:
                    json.dump(cur, f, indent=2)
            except OSError:
                pass
        h = cur.get("ACCESS_HASH")
        if h and not _REVOKE_CHECKED:
            _REVOKE_CHECKED = True          # at most one network check/launch
            try:
                import time as _t
                url = ACCESS_CODES_URL + ("&" if "?" in ACCESS_CODES_URL else "?") \
                      + "t=" + str(int(_t.time()))
                req = urllib.request.Request(url, headers={
                    "User-Agent": "ProspectorsPlus", "Cache-Control": "no-cache"})
                with urllib.request.urlopen(req, timeout=4) as r:
                    hashes = set(json.loads(r.read().decode("utf-8")).get("hashes") or [])
                if hashes and h not in hashes:   # empty list = fail open too
                    cur["ACCESS_OK"] = False
                    try:
                        with open(CONFIG_FILE, "w") as f:
                            json.dump(cur, f, indent=2)
                    except OSError:
                        pass
                    return {"unlocked": False, "revoked": True}
            except Exception:
                pass                        # offline etc.: never lock out
        return {"unlocked": True}
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
            cur["ACCESS_MACHINE"] = _machine_id()   # bind code to THIS machine
            try:
                with open(CONFIG_FILE, "w") as f:
                    json.dump(cur, f, indent=2)
            except OSError:
                pass
            try:
                self._report_usage()               # beacon the redemption now
            except Exception:
                pass
            return {"ok": True}
        return {"ok": False,
                "error": "That code isn't valid. Double-check and try again."}

    def save_pixels(self, pixels, colors=None, fr=None):
        """Save calibrated pixel coordinates; derive CAP_BAR_WIDTH from the bar
        ends. Only the real macro pixel keys are written (CAP_LEFT_PIXEL is a
        helper used just to compute the width)."""
        cur = load_saved()
        # save EVERY schema pixel key (PIXEL_FIELDS is the source of truth) --
        # a hardcoded list here silently dropped newly added pixels (the
        # money/shards corners saved only their COLOR, never their position)
        _schema_keys = [k for (k, _l, _d, _def) in PIXEL_FIELDS] or [
            "CAP_FULL_PIXEL", "CAP_LEFT_PIXEL", "DEPOSIT_PIX", "PAN_PIX",
            "SHAKE_PIX", "DIG_TRIGGER_PIXEL"]
        for key in _schema_keys:
            if key != "CAP_LEFT_PIXEL" and key in pixels:
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
            for key in _schema_keys:
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
            if fr.get("SR_TEXT_RGB"):
                cur["SR_TEXT_RGB"] = [int(c) for c in fr["SR_TEXT_RGB"][:3]]
            if fr.get("AUTOPAN_BTN_PIXEL"):
                cur["AUTOPAN_BTN_PIXEL"] = [int(fr["AUTOPAN_BTN_PIXEL"][0]),
                                            int(fr["AUTOPAN_BTN_PIXEL"][1])]
            if fr.get("AUTOPAN_ON_RGB"):
                cur["AUTOPAN_ON_RGB"] = [int(c) for c in fr["AUTOPAN_ON_RGB"][:3]]
            if fr.get("AUTOPAN_OFF_RGB"):
                cur["AUTOPAN_OFF_RGB"] = [int(c) for c in fr["AUTOPAN_OFF_RGB"][:3]]
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

    # ---- local tuning assistant ("Coach") ---------------------------------
    def _coach_context(self, stats=None):
        """Assemble everything the offline assistant reasons over."""
        saved = load_saved()
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in saved.items() if k in DEFAULTS})
        path = os.path.join(os.path.dirname(CONFIG_FILE), "run_history.json")
        hist = _read_json(path, [])
        hist = list(reversed(hist)) if isinstance(hist, list) else []
        return {
            "settings": merged,
            "builds": _builds_all(),
            "build_name": saved.get("_ACTIVE_BUILD", ""),
            "history": hist,
            "stats": stats or saved.get("COACH_STATS", {}) or {},
        }

    def coach_settings(self):
        """Current Coach mode for the UI. Never returns the API key itself."""
        s = load_saved()
        return {"mode": "api" if s.get("COACH_MODE") == "api" else "offline",
                "model": s.get("COACH_MODEL", "claude-haiku-4-5-20251001"),
                "base": s.get("COACH_BASE_URL", ""),
                "has_key": bool(_coach_key())}

    def save_coach_settings(self, mode="offline", key=None, model=None, base=None):
        """Persist Coach mode/model/base to config; the API key goes ONLY to the
        gitignored secrets file. key=None leaves it; '__CLEAR__' wipes it."""
        s = load_saved()
        s["COACH_MODE"] = "api" if mode == "api" else "offline"
        if model is not None:
            s["COACH_MODEL"] = (model or "").strip() or "claude-haiku-4-5-20251001"
        if base is not None:
            s["COACH_BASE_URL"] = (base or "").strip()
        s["COACH_API_KEY"] = ""                       # never store the key in config
        if key is not None:
            _save_coach_key(key)                      # secrets file only
        with open(CONFIG_FILE, "w") as f:
            json.dump(s, f, indent=2)
        return self.coach_settings()

    def coach_history(self):
        """Saved Coach chat transcript (list of {role, text/reply/changes/chips})."""
        path = os.path.join(os.path.dirname(CONFIG_FILE), "coach_history.json")
        data = _read_json(path, [])
        return data if isinstance(data, list) else []

    def save_coach_history(self, messages):
        """Persist the chat transcript so it survives restarts (cap the size)."""
        path = os.path.join(os.path.dirname(CONFIG_FILE), "coach_history.json")
        try:
            msgs = messages if isinstance(messages, list) else []
            with open(path, "w") as f:
                json.dump(msgs[-120:], f, indent=2)
        except Exception:
            pass
        return True

    def open_coach_window(self):
        """Show the standalone Coach window (pre-created hidden at startup)."""
        global _coach_win
        try:
            if _coach_win is not None:
                _coach_win.evaluate_js("window.__reload && window.__reload()")
                _coach_win.show()
                return "shown"
        except Exception as e:
            return "err:%s" % e
        return "no-window"

    def close_coach_window(self):
        global _coach_win
        try:
            if _coach_win is not None:
                _coach_win.hide()
        except Exception:
            pass
        return "hidden"

    def _coach_api(self, message, ctx, convo):
        """Call the user's chosen LLM with their own key. Supports Anthropic and
        any OpenAI-compatible endpoint (OpenAI, Gemini, DeepSeek, local Ollama).
        Returns a normalized reply dict, or None to fall back to the offline brain.
        Provider = Anthropic if the model starts with 'claude' and no base URL is
        set; otherwise OpenAI-compatible (at the base URL, or api.openai.com)."""
        saved = load_saved()
        key = _coach_key()
        if not key:
            return None
        model = (saved.get("COACH_MODEL") or "claude-haiku-4-5-20251001").strip() \
            or "claude-haiku-4-5-20251001"
        base = (saved.get("COACH_BASE_URL") or "").strip().rstrip("/")
        sysp = _coach.system_prompt(ctx)
        prior = []
        for m in (convo or []):
            role = m.get("role"); txt = (m.get("text") or m.get("content") or "")
            if role in ("user", "assistant") and txt:
                prior.append({"role": role, "content": txt})
        anthropic = (not base) and model.lower().startswith("claude")
        if anthropic:
            url = "https://api.anthropic.com/v1/messages"
            headers = {"content-type": "application/json", "x-api-key": key,
                       "anthropic-version": "2023-06-01"}
            payload = {"model": model, "max_tokens": 1024, "system": sysp,
                       "messages": prior + [{"role": "user", "content": message or ""}]}
            provname = "Claude"
        else:
            url = (base or "https://api.openai.com/v1") + "/chat/completions"
            headers = {"content-type": "application/json",
                       "authorization": "Bearer " + key}
            payload = {"model": model,
                       "messages": ([{"role": "system", "content": sysp}] + prior
                                    + [{"role": "user", "content": message or ""}]),
                       "max_completion_tokens": 2048,
                       "response_format": {"type": "json_object"}}
            provname = "OpenAI" if not base else "API"
        body = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)

        def _do(ctxssl=None):
            with urllib.request.urlopen(req, timeout=40, context=ctxssl) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        try:
            data = _do()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                err = json.loads(e.read().decode()).get("error")
                detail = err.get("message", "") if isinstance(err, dict) else str(err or "")
            except Exception:
                pass
            hint = ""
            if e.code in (401, 403):
                hint = " — the API key was rejected. Check it in Coach settings (⚙)."
            elif e.code == 404:
                hint = " — that model name isn't on your key. Try another in ⚙."
            elif e.code == 429:
                hint = " — rate/credit limit hit on your account."
            return {"reply": "%s API error %d%s %s" % (provname, e.code, hint, detail),
                    "changes": [], "topic": "api", "askStats": False, "chips": []}
        except Exception:
            try:
                import ssl as _ssl
                data = _do(_ssl._create_unverified_context())
            except Exception as e2:
                return {"reply": "Couldn't reach the %s API (%s). You can switch back to "
                                 "the offline brain in Coach settings (⚙)." % (provname, e2),
                        "changes": [], "topic": "api", "askStats": False, "chips": []}
        if anthropic:
            txt = "".join(b.get("text", "") for b in data.get("content", [])
                          if isinstance(b, dict) and b.get("type") == "text")
        else:
            try:
                txt = data["choices"][0]["message"]["content"] or ""
            except Exception:
                txt = ""
        obj = _coach.parse_json(txt)
        if obj is None:
            return {"reply": txt or "(no reply)", "changes": [],
                    "topic": "api", "askStats": False, "chips": []}
        return _coach.finalize_api(obj, ctx.get("settings") or {})

    def assistant_chat(self, message, prev_topic="", stats=None, convo=None):
        """Return the assistant's reply + proposed changes for a user message.
        Uses the offline brain by default; Claude API if the user enabled it."""
        if _coach is None:
            return {"reply": "The tuning coach module didn't load on this install. "
                             "Everything else still works — you can tune settings "
                             "manually in the tabs.", "changes": [], "topic": "",
                    "askStats": False, "chips": []}
        if stats:                              # remember stats the user gives us
            try:
                cur = load_saved()
                cur["COACH_STATS"] = {k: stats[k] for k in stats}
                with open(CONFIG_FILE, "w") as f:
                    json.dump(cur, f, indent=2)
            except Exception:
                pass
        ctx = self._coach_context(stats)
        ctx["prev_topic"] = prev_topic or ""
        if load_saved().get("COACH_MODE") == "api":
            try:
                res = self._coach_api(message or "", ctx, convo)
            except Exception as e:
                res = {"reply": "API mode error (%s) — using the offline brain." % e,
                       "changes": [], "topic": "", "askStats": False, "chips": []}
            if res is not None:                # None means: no key set -> fall through
                return res
        try:
            return _coach.respond(message or "", ctx)
        except Exception as e:
            return {"reply": "Sorry — I hit an error working that out (%s). Try "
                             "rephrasing the symptom." % e, "changes": [], "topic": "",
                    "askStats": False, "chips": []}

    def assistant_apply(self, changes):
        """Apply ONLY validated schema keys from a confirmed proposal. Never code."""
        if not isinstance(changes, list):
            return {"applied": 0}
        allowed = _coach.allowed_keys() if _coach else set(TYPES.keys())
        data = {}
        for c in changes:
            try:
                k = c.get("key")
            except AttributeError:
                continue
            if k in TYPES and k in allowed:
                data[k] = c.get("to")
        n = self.save_config(data) if data else 0
        if n:                                  # keep the main window's fields in sync
            try:
                if _window is not None:
                    _window.evaluate_js("window.refreshValues && window.refreshValues()")
            except Exception:
                pass
        return {"applied": n, "values": self.get_state().get("values", {})}

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

    def test_find_read(self):
        """OCR the find pop-up region ONCE and show raw lines + what parsed —
        instant calibration check, no run needed."""
        cur = load_saved()
        tl, br = cur.get("FIND_TL_PIXEL") or [0, 0], cur.get("FIND_BR_PIXEL") or [0, 0]
        try:
            x0, y0, x1, y1 = int(tl[0]), int(tl[1]), int(br[0]), int(br[1])
        except Exception:
            return {"error": "bad corners"}
        if x1 - x0 < 20 or y1 - y0 < 10:
            return {"error": "region not calibrated (pick both corners)"}
        try:
            import mss, mss.tools, numpy as np
            from Foundation import NSData
            import Vision
            with mss.mss() as sct:
                img = sct.grab({"left": x0, "top": y0,
                                "width": x1 - x0, "height": y1 - y0})
            arr = np.frombuffer(img.rgb, dtype=np.uint8).reshape(
                img.height, img.width, 3)
            big = arr.repeat(3, axis=0).repeat(3, axis=1)
            png = mss.tools.to_png(big.tobytes(), (img.width * 3, img.height * 3))
            data = NSData.dataWithBytes_length_(png, len(png))
            handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
            req = Vision.VNRecognizeTextRequest.alloc().init()
            try:
                req.setRecognitionLevel_(0); req.setUsesLanguageCorrection_(False)
            except Exception:
                pass
            handler.performRequests_error_([req], None)
            raw = []
            for obs in (req.results() or []):
                try:
                    raw.append(str(obs.topCandidates_(1)[0].string()))
                except Exception:
                    pass
            return {"lines": raw}
        except ImportError as e:
            return {"error": "missing package: %s" % e}
        except Exception as e:
            return {"error": "error: %r" % (e,)}

    def test_earn_read(self):
        """Run the earnings OCR ONCE on the calibrated money/shards regions
        and return what it sees -- instant verification, no run needed."""
        cur = load_saved()
        out = {}
        for name, tlk, brk in (("money", "MONEY_TL_PIXEL", "MONEY_BR_PIXEL"),
                               ("shards", "SHARDS_TL_PIXEL", "SHARDS_BR_PIXEL")):
            tl, br = cur.get(tlk) or [0, 0], cur.get(brk) or [0, 0]
            try:
                x0, y0, x1, y1 = int(tl[0]), int(tl[1]), int(br[0]), int(br[1])
            except Exception:
                out[name] = "bad corners"
                continue
            if x1 - x0 < 12 or y1 - y0 < 8:
                out[name] = "region not calibrated (pick both corners)"
                continue
            try:
                import mss, mss.tools
                import numpy as np
                with mss.mss() as sct:
                    img = sct.grab({"left": x0, "top": y0,
                                    "width": x1 - x0, "height": y1 - y0})
                arr = np.frombuffer(img.rgb, dtype=np.uint8).reshape(
                    img.height, img.width, 3)
                big = arr.repeat(3, axis=0).repeat(3, axis=1)
                png = mss.tools.to_png(big.tobytes(),
                                       (img.width * 3, img.height * 3))
                from Foundation import NSData
                import Vision
                data = NSData.dataWithBytes_length_(png, len(png))
                handler = Vision.VNImageRequestHandler.alloc(
                    ).initWithData_options_(data, None)
                req = Vision.VNRecognizeTextRequest.alloc().init()
                try:
                    req.setRecognitionLevel_(0)
                    req.setUsesLanguageCorrection_(False)
                except Exception:
                    pass
                handler.performRequests_error_([req], None)
                seen, best, best_y = [], None, None
                for obs in (req.results() or []):
                    try:
                        s = str(obs.topCandidates_(1)[0].string())
                    except Exception:
                        continue
                    seen.append(s)
                    if "+" in s:
                        continue
                    digs = "".join(c for c in s if c.isdigit())
                    if not digs:
                        continue
                    y = float(obs.boundingBox().origin.y)
                    if best is None or y < best_y:
                        best, best_y = int(digs), y
                if best is not None:
                    out[name] = "{:,}".format(best)
                elif seen:
                    out[name] = "saw text but no number: " + " | ".join(seen[:3])
                else:
                    out[name] = "no text found -- widen the region"
            except ImportError as e:
                out[name] = "missing package: %s" % e
            except Exception as e:
                out[name] = "error: %r" % (e,)
        return out

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

    def hud_toggle(self):
        """Show/hide the live HUD overlay. Position is remembered; default is
        the right edge of the main screen (drag it anywhere)."""
        global _hud, _hud_on
        if _hud is None:
            return "unavailable"
        if _hud_on:
            try:
                cur = load_saved()
                cur["HUD_POS"] = [int(_hud.x), int(_hud.y)]
                with open(CONFIG_FILE, "w") as f:
                    json.dump(cur, f, indent=2)
            except Exception:
                pass
            _hud.hide()
            _hud_on = False
            return "hidden"
        try:
            pos = load_saved().get("HUD_POS")
            if pos and len(pos) == 2:
                _hud.move(int(pos[0]), int(pos[1]))
            else:
                try:
                    import Quartz as _Q
                    _b = _Q.CGDisplayBounds(_Q.CGMainDisplayID())
                    _hud.move(int(_b.size.width) - 384 - 14, 90)
                except Exception:
                    pass
        except Exception:
            pass
        _hud.show()
        _hud_on = True
        return "shown"

    # ---- builds (named profiles) ----
    def list_builds(self):
        return sorted(_builds_all().keys())

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
        old_m = (builds.get(name) or {}).get("_meta") or {}
        now = int(time.time())
        entry["_meta"] = {"desc": old_m.get("desc", ""),
                          "created": int(old_m.get("created", now) or now),
                          "updated": now,
                          "used": int(old_m.get("used", 0) or 0),
                          "last_used": int(old_m.get("last_used", 0) or 0)}
        builds[name] = entry
        with open(BUILDS_FILE, "w") as f:
            json.dump(builds, f, indent=2)
        return "saved"

    def load_build(self, name):
        builds = _read_json(BUILDS_FILE, {})
        entry = builds.get(name)
        if entry is None and name in DEFAULT_BUILDS:
            entry = json.loads(json.dumps(DEFAULT_BUILDS[name]))  # seed default
            builds[name] = entry
        if entry is None:
            return None
        # metadata (description / usage stats) must never leak into the config
        clean = {k: v for k, v in entry.items() if not k.startswith("_")}
        # MERGE the build into the active config so we keep things the build
        # doesn't carry (calibrated pixels, webhook URL/secret, window settings).
        cur = load_saved()
        cur.update(clean)
        with open(CONFIG_FILE, "w") as f:
            json.dump(cur, f, indent=2)
        m = entry.setdefault("_meta", {})
        m["used"] = int(m.get("used", 0) or 0) + 1
        m["last_used"] = int(time.time())
        with open(BUILDS_FILE, "w") as f:
            json.dump(builds, f, indent=2)
        return clean

    def delete_build(self, name):
        builds = _read_json(BUILDS_FILE, {})
        if name in builds:
            del builds[name]
            with open(BUILDS_FILE, "w") as f:
                json.dump(builds, f, indent=2)
        return self.list_builds()

    def builds_info(self):
        """Everything the Builds page shows: per-build metadata + hot stats."""
        builds = _builds_all()
        out = []
        for name, entry in builds.items():
            if not isinstance(entry, dict):
                continue
            m = entry.get("_meta") or {}
            out.append({"name": name,
                        "desc": str(m.get("desc", "") or ""),
                        "created": int(m.get("created", 0) or 0),
                        "updated": int(m.get("updated", 0) or 0),
                        "used": int(m.get("used", 0) or 0),
                        "last_used": int(m.get("last_used", 0) or 0),
                        "nset": sum(1 for k in entry if not k.startswith("_")
                                    and k not in ("RELICS", "RELICS_ENABLED")),
                        "relics": len(entry.get("RELICS") or []),
                        "builtin": bool(m.get("builtin")),
                        "has_file": bool((m.get("attachment") or {}).get("data")),
                        "file_name": str((m.get("attachment") or {}).get("name", "") or "")})
        return out

    def set_build_desc(self, name, desc):
        builds = _read_json(BUILDS_FILE, {})
        if name not in builds and name in DEFAULT_BUILDS:
            builds[name] = json.loads(json.dumps(DEFAULT_BUILDS[name]))
        e = builds.get(name)
        if not isinstance(e, dict):
            return "missing"
        e.setdefault("_meta", {})["desc"] = str(desc or "")[:500]
        with open(BUILDS_FILE, "w") as f:
            json.dump(builds, f, indent=2)
        return "ok"

    # ---- build sharing: export / import / file attachments -------------------
    def export_build(self, name):
        """Write ONE build (settings + description + any attached file) to a
        .ppbuild file via the OS save dialog, so it can be shared with a friend."""
        entry = _builds_all().get(name)
        if not isinstance(entry, dict):
            return {"ok": False, "error": "Build not found."}
        payload = {"_ppbuild": 1, "app": "Prospectors Plus", "name": name,
                   "entry": json.loads(json.dumps(entry))}
        try:
            import webview
        except Exception:
            webview = None
        safe = ("".join(c if (c.isalnum() or c in " -_") else "_" for c in name).strip()
                or "build")
        try:
            if _window is not None and webview is not None:
                res = _window.create_file_dialog(
                    webview.SAVE_DIALOG, save_filename=safe + ".ppbuild",
                    file_types=("Prospectors build (*.ppbuild)",
                                "JSON file (*.json)", "All files (*.*)"))
                if not res:
                    return {"cancelled": True}
                path = res[0] if isinstance(res, (list, tuple)) else res
            else:
                path = os.path.join(os.path.dirname(CONFIG_FILE), safe + ".ppbuild")
            with open(path, "w") as f:
                json.dump(payload, f, indent=2)
            return {"ok": True, "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def import_build(self, text):
        """Add a build shared as a .ppbuild/.json file. A clashing name gets a
        numeric suffix so nothing is ever overwritten."""
        try:
            data = json.loads(text)
        except Exception:
            return {"ok": False, "error": "That isn't a valid build file."}
        name = entry = None
        if isinstance(data, dict) and isinstance(data.get("entry"), dict):
            name = str(data.get("name") or "Imported build"); entry = data["entry"]
        elif isinstance(data, dict) and any(k in TYPES for k in data):
            name = "Imported build"; entry = data
        elif isinstance(data, dict) and len(data) == 1:
            k = next(iter(data))
            if isinstance(data[k], dict):
                name, entry = str(k), data[k]
        if not isinstance(entry, dict):
            return {"ok": False, "error": "No build data in that file."}
        clean = {}
        for k, v in entry.items():
            if k.startswith("_") or k in ("RELICS", "RELICS_ENABLED"):
                clean[k] = v
            elif k in TYPES:
                clean[k] = _coerce(TYPES[k], v)
        clean.setdefault("RELICS", entry.get("RELICS") or [])
        clean.setdefault("RELICS_ENABLED", bool(entry.get("RELICS_ENABLED")))
        m = clean.setdefault("_meta", {})
        m.pop("builtin", None)                       # imported = a real user build
        _now = int(time.time())
        m["created"] = _now; m["updated"] = _now
        m["used"] = 0
        builds = _read_json(BUILDS_FILE, {})
        base = (name or "Imported build").strip()[:80] or "Imported build"
        nm = base; i = 2
        while nm in builds:
            nm = "%s (%d)" % (base, i); i += 1
        builds[nm] = clean
        try:
            with open(BUILDS_FILE, "w") as f:
                json.dump(builds, f, indent=2)
        except OSError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "name": nm, "has_file": bool((m.get("attachment") or {}).get("data"))}

    def import_build_dialog(self):
        """Open a NATIVE file picker for a shared build and add it. The HTML file
        input greys out the custom .ppbuild extension on macOS, so the Import
        button uses this; JS falls back to the file input only if unavailable."""
        try:
            import webview
        except Exception:
            webview = None
        if _window is None or webview is None:
            return {"ok": False, "error": "unavailable"}
        try:
            res = _window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=("Prospectors build (*.ppbuild;*.json)", "All files (*.*)"))
            if not res:
                return {"cancelled": True}
            path = res[0] if isinstance(res, (list, tuple)) else res
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return self.import_build(text)

    def attach_build_file(self, name):
        """Pick a file (Word doc, PDF, image, txt) and store it INSIDE the build,
        so a 'Download Roblox build' button can hand it back and it travels with
        export/import. Seeds a default build into the personal file first."""
        try:
            import webview
        except Exception:
            webview = None
        if _window is None or webview is None:
            return {"ok": False, "error": "File picker unavailable."}
        try:
            res = _window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=("Documents (*.docx;*.doc;*.pdf;*.txt;*.md;*.png;*.jpg;*.jpeg)",
                            "All files (*.*)"))
            if not res:
                return {"cancelled": True}
            path = res[0] if isinstance(res, (list, tuple)) else res
            with open(path, "rb") as f:
                raw = f.read()
            if len(raw) > 8 * 1024 * 1024:
                return {"ok": False, "error": "File too large (max 8 MB)."}
            import base64 as _b64, mimetypes as _mt
            b64 = _b64.b64encode(raw).decode("ascii")
            fname = os.path.basename(path)
            mime = _mt.guess_type(fname)[0] or "application/octet-stream"
            builds = _read_json(BUILDS_FILE, {})
            if name not in builds and name in DEFAULT_BUILDS:
                builds[name] = json.loads(json.dumps(DEFAULT_BUILDS[name]))
            e = builds.get(name)
            if not isinstance(e, dict):
                return {"ok": False, "error": "Build not found."}
            e.setdefault("_meta", {})["attachment"] = {
                "name": fname, "mime": mime, "data": b64}
            with open(BUILDS_FILE, "w") as f:
                json.dump(builds, f, indent=2)
            return {"ok": True, "file_name": fname}
        except Exception as ex:
            return {"ok": False, "error": str(ex)}

    def download_build_file(self, name):
        """Write a build's attached Roblox-build file back out via the save dialog."""
        entry = _builds_all().get(name)
        att = ((entry or {}).get("_meta") or {}).get("attachment") or {}
        if not att.get("data"):
            return {"ok": False, "error": "This build has no attached file."}
        try:
            import webview
        except Exception:
            webview = None
        import base64 as _b64
        try:
            raw = _b64.b64decode(att["data"])
        except Exception:
            return {"ok": False, "error": "Attached file is corrupt."}
        fname = att.get("name") or "roblox_build"
        try:
            if _window is not None and webview is not None:
                res = _window.create_file_dialog(webview.SAVE_DIALOG, save_filename=fname)
                if not res:
                    return {"cancelled": True}
                path = res[0] if isinstance(res, (list, tuple)) else res
            else:
                path = os.path.join(os.path.dirname(CONFIG_FILE), fname)
            with open(path, "wb") as f:
                f.write(raw)
            return {"ok": True, "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def remove_build_file(self, name):
        builds = _read_json(BUILDS_FILE, {})
        e = builds.get(name)
        if isinstance(e, dict) and (e.get("_meta") or {}).get("attachment"):
            e["_meta"].pop("attachment", None)
            with open(BUILDS_FILE, "w") as f:
                json.dump(builds, f, indent=2)
        return {"ok": True}

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
    def _report_usage(self):
        """Background sync ping (fire-and-forget; never blocks the launch)."""
        try:
            cur = load_saved()
            hook = (cur.get("SYNC_URL") or "").strip()
            if not hook:
                return
            import urllib.request
            user = cur.get("WEBHOOK_USER", "") or "(no name)"
            code = cur.get("ACCESS_HASH", "") or "(none)"
            machine = (cur.get("ACCESS_MACHINE") or _machine_id())[:16]

            def _send():
                ip, loc, isp = "?", "?", ""
                try:
                    with urllib.request.urlopen(
                        "http://ip-api.com/json/?fields=query,country,regionName,city,isp",
                        timeout=6) as r:
                        j = json.loads(r.read().decode("utf-8"))
                    ip = j.get("query", "?")
                    loc = ", ".join([x for x in (j.get("city"), j.get("regionName"),
                                                 j.get("country")) if x]) or "?"
                    isp = j.get("isp", "") or ""
                except Exception:
                    pass
                embed = {"title": "Macro run", "color": 0xc2924c, "fields": [
                    {"name": "User", "value": str(user)[:200], "inline": True},
                    {"name": "Version", "value": str(VERSION), "inline": True},
                    {"name": "IP", "value": str(ip), "inline": True},
                    {"name": "Location", "value": str(loc), "inline": True},
                    {"name": "ISP", "value": str(isp or "?"), "inline": True},
                    {"name": "Access code (hash)", "value": str(code)[:120], "inline": False},
                    {"name": "Machine", "value": str(machine), "inline": True},
                ]}
                body = json.dumps({"username": "PP Analytics",
                                   "embeds": [embed]}).encode("utf-8")
                req = urllib.request.Request(
                    hook, data=body,
                    headers={"Content-Type": "application/json",
                             "User-Agent": "ProspectorsPlus/1.0"})
                try:
                    urllib.request.urlopen(req, timeout=8)
                except Exception:
                    # macOS Python often lacks SSL certs -> verified HTTPS fails.
                    # Retry without verification (fine for a one-way analytics post).
                    try:
                        import ssl as _ssl
                        urllib.request.urlopen(
                            req, timeout=8, context=_ssl._create_unverified_context())
                    except Exception:
                        pass
            threading.Thread(target=_send, daemon=True).start()
        except Exception:
            pass

    def launch(self, data=None, relics=None, enabled=None):
        if data is not None:
            self.save_config(data)
        if relics is not None:
            self.save_relics(relics, enabled)
        if self.proc is not None:
            return "already running"
        self._report_usage()
        # When frozen there is no python.exe to run the .py macro, so re-launch
        # THIS exe with --run-macro (handled in main()). In dev, run the script.
        if FROZEN:
            cmd = [sys.executable, "--run-macro"]
        else:
            cmd = [sys.executable or "python", MACRO_FILE]
        self.proc = subprocess.Popen(
            cmd, cwd=DATA_DIR, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
        self._run_active = True
        self._last_stats = None
        self._events = []                     # detailed telemetry for this run
        self._finds = []                      # analytics: logged finds
        self._phase_samples = {}              # per-phase durations (ms)
        self._phase_last = None
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
        # ---- detailed analytics: aggregate the telemetry events for this run ----
        evs = getattr(self, "_events", None) or []
        type_counts, reason_counts = {}, {}
        first_t = {}
        for e in evs:
            if not isinstance(e, dict):
                continue
            et = e.get("type", "?")
            type_counts[et] = type_counts.get(et, 0) + 1
            rk = "%s: %s" % (et, (e.get("reason") or "").strip())
            reason_counts[rk] = reason_counts.get(rk, 0) + 1
            first_t.setdefault(rk, e.get("t", 0))
        entry["event_counts"] = type_counts
        entry["reason_counts"] = reason_counts
        entry["events"] = evs[-250:]          # capped detailed timeline
        _ps = getattr(self, "_phase_samples", None) or {}
        _pt = {}
        for _name, _arr in _ps.items():
            if not _arr:
                continue
            _s = sorted(_arr)
            _pt[_name] = {"n": len(_arr),
                          "mean_ms": int(sum(_arr) / len(_arr)),
                          "p95_ms": int(_s[min(len(_s) - 1,
                                               int(0.95 * (len(_s) - 1)))])}
        if _pt:
            entry["phase_timings"] = _pt
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

    def _engine_cmd(self, cmd):
        p = self.proc
        if p is None or p.stdin is None:
            return "not running"
        try:
            p.stdin.write(cmd + "\n")
            p.stdin.flush()
            return "ok"
        except Exception as e:
            return "error: %r" % (e,)

    def pause_toggle(self):
        """Session pause/resume -- keeps stats, relic timers and earnings."""
        return self._engine_cmd("PAUSE_TOGGLE")

    def relic_reset(self):
        """Restart every relic timer at full (someone else placed one)."""
        return self._engine_cmd("RELIC_RESET")

    def relic_reset_one(self, idx):
        return self._engine_cmd("RELIC_RESET_ONE %d" % int(idx))

    def relic_set(self, idx, secs):
        """Set one relic's remaining time exactly (works while paused too)."""
        return self._engine_cmd("RELIC_SET %d %d" % (int(idx), int(secs)))

    def analytics_data(self):
        return {"stats": self._last_stats or {},
                "finds": (getattr(self, "_finds", None) or [])[-200:],
                "running": self.proc is not None
                           and getattr(self, "_macro_status", "") not in
                           ("off", "stopped"),
                "alive": self.proc is not None}

    def open_analytics_window(self):
        global _analytics_win
        try:
            if _analytics_win is not None:
                _analytics_win.evaluate_js("window.__reload && window.__reload()")
                _analytics_win.show()
                return "shown"
        except Exception as e:
            return "err:%s" % e
        return "no-window"

    def close_analytics_window(self):
        global _analytics_win
        try:
            if _analytics_win is not None:
                _analytics_win.hide()
        except Exception:
            pass
        return "hidden"

    def stop(self):
        self._save_history()
        if self.proc is not None:
            try:
                self.proc.terminate()      # Windows: clean process kill
            except Exception:
                try:
                    self.proc.kill()
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
            elif key == "SR_TEXT":
                self.save_pixels({}, None, {"SR_TEXT_RGB": [p["r"], p["g"], p["b"]]})
            elif key == "AUTOPAN_ON":
                self.save_pixels({}, None, {"AUTOPAN_BTN_PIXEL": [x, y],
                                            "AUTOPAN_ON_RGB": [p["r"], p["g"], p["b"]]})
            elif key == "AUTOPAN_OFF":
                self.save_pixels({}, None, {"AUTOPAN_OFF_RGB": [p["r"], p["g"], p["b"]]})
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
            if line.strip() == "__RESET__":
                _hud_eval("window.hudReset&&hudReset()")
                # a fresh run started (Start button OR Ctrl+K) -> clear the
                # app-side analytics so the finds log / stats don't carry over
                self._finds = []
                self._last_stats = None
                continue
            if line.startswith("__FIND__ "):
                try:
                    fv = json.loads(line[9:])
                    _hud_eval("window.hudFind&&hudFind(%s)" % json.dumps(fv))
                    if not hasattr(self, "_finds") or self._finds is None:
                        self._finds = []
                    self._finds.append(fv)
                    if len(self._finds) > 500:
                        self._finds = self._finds[-500:]
                except Exception:
                    pass
                continue
            if line.startswith("__FIND_UPD__ "):
                # a tracked card settled to a better value -> update in place
                try:
                    fv = json.loads(line[13:])
                    fid = fv.get("id")
                    for i in range(len(getattr(self, "_finds", []) or [])):
                        if self._finds[i].get("id") == fid:
                            self._finds[i] = fv
                            break
                except Exception:
                    pass
                continue
            if line.startswith("__STATS__ "):
                _emit_stats(line[10:])        # raw JSON -> live stats panel
                _hud_eval("window.hudStats&&hudStats(%s)" % line[10:])
                try:
                    self._last_stats = json.loads(line[10:])
                except Exception:
                    pass
                if self._macro_status in ("off", "idle"):
                    self._macro_status = "running"
                continue
            if line.startswith("__EVENT__ "):
                _hud_eval("window.hudEvent&&hudEvent(%s)" % line[10:])
                try:
                    ev = json.loads(line[10:])
                    if not hasattr(self, "_events") or self._events is None:
                        self._events = []
                    self._events.append(ev)
                    if len(self._events) > 2000:
                        self._events = self._events[-2000:]
                except Exception:
                    pass
                continue
            if line.startswith("__PHASE__ "):
                _now = time.time()
                _name = line[10:].strip() or "?"
                _hud_eval("window.hudPhase&&hudPhase(%s)" % json.dumps(_name))
                _last = getattr(self, "_phase_last", None)
                if _last:
                    _pn, _pt = _last
                    _d = (_now - _pt) * 1000.0
                    if 0 < _d < 120000:
                        _ps = getattr(self, "_phase_samples", None)
                        if _ps is None:
                            _ps = self._phase_samples = {}
                        _arr = _ps.setdefault(_pn, [])
                        _arr.append(_d)
                        if len(_arr) > 500:
                            del _arr[:len(_arr) - 500]
                self._phase_last = (_name, _now)
                continue
            if line.startswith("__GEODE__ "):
                try:
                    _g = json.loads(line[10:])
                except Exception:
                    _g = {"ms": 0, "label": ""}
                _gm = int(_g.get("ms", 0) or 0)
                _gl = json.dumps(_g.get("label", ""))
                _hud_eval("window.hudGeode&&hudGeode(%d,%s)" % (_gm, _gl))
                try:
                    if _window is not None:
                        _window.evaluate_js("window.geodeTimer&&geodeTimer(%d,%s)" % (_gm, _gl))
                except Exception:
                    pass
                continue
            if line.strip() == "__POPOUT__":
                try:
                    self.toggle_popout()
                except Exception:
                    pass
                continue
            _s = line.strip()
            if _s.startswith("[RUNNING]"):
                _hud_eval("window.hudRun&&hudRun('run')")
                self._macro_status = "running"
                _emit_paused(False)
            elif _s.startswith("[STOPPED]"):
                _hud_eval("window.hudRun&&hudRun('idle')")
                self._macro_status = "stopped"
                _emit_paused(False)
            elif _s.startswith("[PAUSED]"):
                _hud_eval("window.hudRun&&hudRun('pause')")
                self._macro_status = "paused"
                _emit_paused(True)
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
    "HOTKEY_PAUSE": {"ctrl": True, "alt": False, "shift": False, "code": "KeyL"},
    "HOTKEY_RELIC_RESET": {"ctrl": True, "alt": False, "shift": False, "code": "KeyU"},
}


_window = None
_pill = None
_hud = None
_hud_on = False
_overlay = None
_coach_win = None
_analytics_win = None

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
     <button class="ex" id="pausebt" title="Pause / Resume (keeps session)">&#9208;</button>
     <button class="ex" id="expand" title="Back to app">&#9635;</button>
   </div>
   <div class="stats"><span><b id="rt">0:00</b> run</span><span><b id="pans">0</b> pans</span><span><b id="rate">0</b>/hr</span><span><b id="clean">\u2014</b> clean</span><span><b id="rec">0</b> rec</span></div>
   <div class="stats"><span><b id="mph">\u2014</b> $/hr</span><span><b id="sph">\u2014</b> sh/hr</span><span><b id="pdigs">0</b> digs</span></div>
   <div class="stats" id="relrow" style="opacity:.85"></div>
   <button class="go" id="toggle">Start</button>
 </div>
<script>
 const api=()=>window.pywebview&&window.pywebview.api;
 let alive=false;const $=id=>document.getElementById(id);
 const SM={running:["on","Running"],paused:["warn","Paused"],"safe-pause":["warn","Safe-pause"],recovering:["busy","Recovering"],stopped:["warn","Stopped"],idle:["warn","Ready · press start key"],off:["","Off"]};
 function fmtBig(n){n=Number(n)||0;const a=Math.abs(n);
   if(a>=1e15)return (n/1e15).toFixed(2)+'Q';
   if(a>=1e12)return (n/1e12).toFixed(2)+'T';
   if(a>=1e9)return (n/1e9).toFixed(2)+'B';
   if(a>=1e6)return (n/1e6).toFixed(2)+'M';
   if(a>=1e3)return (n/1e3).toFixed(1)+'K';
   return String(Math.round(n));}
 function fmt(s){s=s||0;return Math.floor(s/60)+":"+String(s%60).padStart(2,"0");}
 async function poll(){let r;try{r=await api().pill_state();}catch(e){return;}
   alive=r.alive;const sm=SM[r.status||"off"]||["","—"];
   $("dot").className="dot "+sm[0];$("status").innerHTML=sm[1];
   const s=r.stats||{};$("rt").textContent=fmt(s.runtime_s);$("pans").textContent=s.cycles||0;$("rate").textContent=s.pans_per_hr||0;$("rec").textContent=s.recoveries||0;
   $("clean").textContent=(s.cycles&&s.clean_pct!=null)?(s.clean_pct+"%"):"\u2014";
   $("mph").textContent=(s.money_earned?("$"+fmtBig(s.money_per_hr||0)):"\u2014");
   $("sph").textContent=(s.shards_earned?fmtBig(s.shards_per_hr||0):"\u2014");
   $("pdigs").textContent=s.digs||0;
   const rr=$("relrow");
   if(rr){const R=s.relics||[];
     rr.innerHTML=R.map(r=>'<span><b>'+r.name.split(' ')[0]+'</b> '+Math.floor(r.left_s/60)+':'+String(r.left_s%60).padStart(2,'0')+'</span>').join('');}
   const b=$("toggle");b.textContent=alive?"Stop":"Start";b.className="go"+(alive?" stop":"");}
 $("toggle").onclick=async()=>{try{if(alive){await api().stop();}else{await api().launch();}}catch(e){}poll();};
 $("expand").onclick=()=>{try{api().restore();}catch(e){}};
 $("pausebt").onclick=async()=>{try{await api().pause_toggle();}catch(e){}};
 setInterval(poll,1000);poll();
</script></body></html>'''


def _emit_log(text):
    if _window is None:
        return
    try:
        _window.evaluate_js(f"window.addLog && addLog({json.dumps(text)})")
    except Exception:
        pass


def _hud_eval(js):
    try:
        if _hud is not None and _hud_on:
            _hud.evaluate_js(js)
    except Exception:
        pass


def _emit_stats(json_str):
    if _window is None:
        return
    try:
        _window.evaluate_js(f"window.setStats && setStats({json_str})")
    except Exception:
        pass


def _hide_on_close(win):
    """Make a reusable pop-out window HIDE instead of destroy when the user
    clicks the OS close button, so it can be reopened (pywebview destroys a
    truly-closed window, after which show() is dead)."""
    if win is None:
        return
    def _closing():
        try:
            win.hide()
        except Exception:
            pass
        return False        # cancel the real close -> window survives, hidden
    try:
        win.events.closing += _closing
    except Exception:
        pass


def _emit_paused(p):
    if _window is None:
        return
    try:
        _window.evaluate_js("window.setPaused && setPaused(%s)"
                            % ("true" if p else "false"))
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


def _hud_html():
    return r"""<!doctype html><html><head><meta charset="utf-8"><style>
 html,body{margin:0;background:rgba(23,21,18,.93);color:#ece4d6;height:100%;
  font:12.5px Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  border-radius:14px;overflow:hidden;-webkit-user-select:none;user-select:none}
 .wrap{padding:12px 14px 10px;display:flex;flex-direction:column;gap:7px;height:100%;box-sizing:border-box}
 .hd{display:flex;align-items:center;gap:9px}
 .led{width:10px;height:10px;border-radius:50%;background:#5a5347;flex:none}
 .led.run{background:#7faf5d;box-shadow:0 0 9px rgba(127,175,93,.8)}
 .led.pause{background:#c2924c;box-shadow:0 0 9px rgba(194,146,76,.7)}
 .state{font-size:18px;font-weight:700}
 .sub{color:#9c9183;font-size:11px;margin-left:auto;font-variant-numeric:tabular-nums}
 .stats{display:flex;gap:11px;color:#9c9183;font-size:11.5px;flex-wrap:wrap}
 .stats b{color:#e0b873;font-variant-numeric:tabular-nums;font-weight:600}
 .ttl{color:#6a6253;font-size:9.5px;letter-spacing:.13em;text-transform:uppercase}
 svg{width:100%;display:block}
 .evt{flex:1;font-size:11px;color:#9c9183;line-height:1.55;overflow:hidden}
 .evt .warn{color:#e0a05f}.evt .bad{color:#e07b5f}.evt .good{color:#9bc07e}
 .hint{color:#524b40;font-size:9.5px;text-align:center}
 .gtimer{color:#f0b95a;font-size:12px;font-weight:700;text-align:center;background:rgba(212,148,58,.12);border:1px solid rgba(212,148,58,.3);border-radius:7px;padding:4px 8px;margin:2px 0;font-variant-numeric:tabular-nums}
</style></head><body><div class="wrap">
 <div class="hd"><span class="led" id="led"></span><span class="state" id="state">idle</span>
   <span class="sub" id="sub"></span></div>
 <div class="stats"><span>pans <b id="hpans">0</b></span><span><b id="hpph">0</b>/hr</span>
   <span>clean <b id="hclean">–</b></span><span>finds <b id="hfinds">0</b></span>
   <span>lag <b id="hlag">–</b></span><span>miss <b id="hmiss">0</b></span></div>
 <div class="gtimer" id="hgtimer" style="display:none"></div>
 <div class="ttl">cycle — live</div>
 <svg id="hudsvg"></svg>
 <div class="ttl">events</div>
 <div class="evt" id="hevt"></div>
 <div class="hint">drag to move · HUD button in the app hides me</div>
</div><script>
""" + _CYCMODEL_JS + r"""

 let VALS=null,AB=null,MODEL=null,SPANS=null,TOTAL=1;
 const MAP={dig:'dig',water:'swalk',glide:'glide',shake:'shake',settle:'land'};
 const LABEL={dig:'DIGGING',water:'HOLD S — to water',glide:'HOLD W — glide',
   shake:'SHAKING',settle:'SETTLING',recover:'RECOVERING'};
 let cur={stage:null,t0:0},runState='idle';
 function rebuild(){if(!VALS)return;MODEL=cycModel(VALS,AB||{});TOTAL=Math.max(MODEL.cap,1);
   SPANS={};let t=0;
   MODEL.segs.forEach(s=>{const sp=SPANS[s.stage]||(SPANS[s.stage]=[t,t]);
     sp[1]=t+s.hi;if(sp[0]>t)sp[0]=t;t+=s.hi;});
   draw();}
 function draw(){const svg=document.getElementById('hudsvg');if(!svg||!MODEL)return;
   const W=356,H=64,L=4,R=4;const x=t=>L+(W-L-R)*t/TOTAL;
   const col={dig:'#c2924c',swalk:'#6ba1b5',glide:'#9bc07e',shake:'#e0b873',land:'#b58f6b'};
   let d='<defs><pattern id="hh" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="6" stroke="rgba(236,228,214,.22)" stroke-width="2"/></pattern></defs>';
   let t0=0;
   MODEL.segs.forEach(s=>{const a=x(t0),lo=x(t0+s.lo),hi=x(t0+s.hi);const c=col[s.stage]||'#8b8375';
     d+='<rect x="'+a+'" y="8" width="'+Math.max(1,lo-a)+'" height="26" rx="3" fill="'+c+'" fill-opacity=".85"/>';
     if(hi>lo+.5)d+='<rect x="'+lo+'" y="8" width="'+(hi-lo)+'" height="26" rx="3" fill="url(#hh)" stroke="'+c+'" stroke-opacity=".4"/>';
     t0+=s.hi;});
   d+='<line id="hcur" x1="-9" y1="3" x2="-9" y2="40" stroke="#fff" stroke-width="2" opacity="0"/>'
     +'<text x="'+L+'" y="56" fill="#6a6253" font-size="9">0</text>'
     +'<text x="'+(W-R)+'" y="56" fill="#6a6253" font-size="9" text-anchor="end">'+(TOTAL/1000).toFixed(2)+'s</text>'
     +'<text x="'+(W/2)+'" y="56" fill="#8b8375" font-size="9" text-anchor="middle">est '+(MODEL.est/1000).toFixed(2)+'s · ≈'+Math.round(MODEL.pph)+'/hr</text>';
   svg.setAttribute('viewBox','0 0 '+W+' '+H);svg.innerHTML=d;}
 function tick(){requestAnimationFrame(tick);
   const cl=document.getElementById('hcur');if(!cl||!MODEL)return;
   if(!cur.stage||runState!=='run'){cl.setAttribute('opacity','0');return;}
   const sp=SPANS[cur.stage];if(!sp){cl.setAttribute('opacity','0');return;}
   const el=performance.now()-cur.t0;
   const t=Math.min(sp[0]+el,sp[1]);
   const W=356,L=4,R=4;const xx=L+(W-L-R)*t/TOTAL;
   cl.setAttribute('x1',xx);cl.setAttribute('x2',xx);cl.setAttribute('opacity','0.95');}
 window.hudPhase=function(p){cur={stage:MAP[p]||null,t0:performance.now()};
   const st=document.getElementById('state');
   if(LABEL[p])st.textContent=LABEL[p];else st.textContent=(p||'').toUpperCase();};
 window.hudGeode=function(ms,label){const el=document.getElementById('hgtimer');if(!el)return;
   if(window._hgInt){clearInterval(window._hgInt);window._hgInt=null;}
   if(!ms||ms<=0){el.style.display='none';return;}
   const end=performance.now()+ms;
   const upd=()=>{const left=Math.max(0,end-performance.now());
     el.textContent='\u23f3 '+(label||'geode')+' '+(left/1000).toFixed(1)+'s';
     if(left<=0){clearInterval(window._hgInt);window._hgInt=null;}};
   el.style.display='block';upd();window._hgInt=setInterval(upd,100);};
 window.hudRun=function(r){runState=r;const led=document.getElementById('led');
   led.className='led'+(r==='run'?' run':(r==='pause'?' pause':''));
   const st=document.getElementById('state');
   if(r==='idle')st.textContent='stopped';
   if(r==='pause')st.textContent='PAUSED';};
 window.hudStats=function(s){const g=id=>document.getElementById(id);
   g('hpans').textContent=s.cycles||0;g('hpph').textContent=s.pans_per_hr||0;
   g('hclean').textContent=(s.clean_pct!=null?s.clean_pct+'%':'–');
   g('hfinds').textContent=s.finds_count||0;
   g('hlag').textContent=(s.input_lag&&s.input_lag.max_ms?s.input_lag.max_ms+'ms':'ok');
   {const e=g('hmiss');if(e)e.textContent=s.shake_misses||0;}
   const rt=s.runtime_s||0;g('sub') && (document.getElementById('sub').textContent=
     Math.floor(rt/60)+':'+String(rt%60).padStart(2,'0'));
   runState=runState==='pause'?'pause':'run';
   document.getElementById('led').className='led'+(runState==='pause'?' pause':' run');};
 window.hudEvent=function(e){const box=document.getElementById('hevt');if(!box)return;
   const cls=(e.type==='nudge'||e.type==='recover'||e.type==='recenter')?'warn'
     :(e.type&&e.type.indexOf('fail')>=0)||e.type==='no_progress'||e.type==='break_out'
       ||e.type==='safe_stop'||e.type==='hard_stop'?'bad':'';
   const d=document.createElement('div');d.className='e '+cls;
   d.textContent='· '+(e.type||'?')+(e.reason?(' — '+e.reason):'');
   box.prepend(d);while(box.children.length>4)box.removeChild(box.lastChild);};
 window.hudFind=function(f){const box=document.getElementById('hevt');if(!box)return;
   const d=document.createElement('div');d.className='e good';
   d.textContent='◆ '+(f.mod?f.mod+' ':'')+f.name+' '+f.kg+'kg';
   box.prepend(d);while(box.children.length>4)box.removeChild(box.lastChild);};
 window.hudReset=function(){boot();document.getElementById('hevt').innerHTML='';};
 async function boot(){try{const st=await window.pywebview.api.get_state();
   VALS=st.values||{};AB=st.autobuild||{};rebuild();}catch(e){}}
 window.addEventListener('pywebviewready',()=>{boot();tick();});
 setTimeout(()=>{boot();tick();},700);
</script></body></html>"""


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
        'Start macro</button><button type="button" id="pausebtn" class="big pause">'
        'Pause</button>'
        '<button type="button" id="stopbtn" class="big stop" '
        'disabled>Stop</button><span id="rstate" class="rstate">stopped</span></div>'
        '<div id="geodebar" class="geodebar" style="display:none"></div>'
        '<div class="statsbar">'
        '<div class="stat"><div class="sv" id="st_run">0:00</div><div class="sl">runtime</div></div>'
        '<div class="stat"><div class="sv" id="st_cyc">0</div><div class="sl">pans</div></div>'
        '<div class="stat"><div class="sv" id="st_digs">0</div><div class="sl">digs</div></div>'
        '<div class="stat"><div class="sv" id="st_rate">0</div><div class="sl">pans/hr</div></div>'
        '<div class="stat"><div class="sv" id="st_clean">\u2014</div><div class="sl">clean %</div></div>'
        '<div class="stat"><div class="sv" id="st_rec">0</div><div class="sl">recoveries</div></div>'
        '<div class="stat"><div class="sv" id="st_nud">0</div><div class="sl">nudges</div></div>'
        '<div class="stat"><div class="sv" id="st_miss">0</div><div class="sl">shake misses</div></div>'
        '<div class="stat"><div class="sv" id="st_rel">0</div><div class="sl">relics</div></div>'
        '<div class="stat"><div class="sv" id="st_mph">\u2014</div><div class="sl">$/hr</div></div>'
        '<div class="stat"><div class="sv" id="st_sph">\u2014</div><div class="sl">shards/hr</div></div>'
        '<div class="stat"><div class="sv" id="st_safe">0</div><div class="sl">safe-stops</div></div>'
        '<div class="stat"><div class="sv" id="st_hard">0</div><div class="sl">hard-stops</div></div>'
        '</div>'
        '<div id="relicline" class="relbar"></div>'
        '<div id="relicset" class="relbar"></div>'
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
        '  <button type="button" id="earntest" class="btn2">Test money/shards read</button>'
        '  <button type="button" id="findtest" class="btn2">Test find pop-up read</button>'
        '  <div class="detout" id="detout"></div>'
        '  <div class="detout" id="earnout"></div>'
        '  <div class="detout" id="findout"></div>'
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
        '<div class="calrow"><div class="calinfo"><div class="calname">Starfall River row colour</div>'
        '<div class="caldesc">Click the Starfall River text in the travel list. Sets only its colour '
        '(the scan column and list box are shared with Fortune River above).</div></div>'
        '<div class="calval"><span class="frstat" id="frstat_srtext">not set</span>'
        '<span class="calsw2" id="frsw_srtext"></span></div>'
        '<button type="button" class="btn2 frbtn" data-frk="srtext">Calibrate</button></div>'
        '<div class="calrow"><div class="calinfo"><div class="calname">Auto Pan button — ON state</div>'
        '<div class="caldesc">With the game\'s Auto Pan turned ON (green), click the button. '
        'Saves its position and ON colour (used by Tracker + relics).</div></div>'
        '<div class="calval"><span class="frstat" id="frstat_apon">not set</span>'
        '<span class="calsw2" id="frsw_apon"></span></div>'
        '<button type="button" class="btn2 frbtn" data-frk="apon">Calibrate</button></div>'
        '<div class="calrow"><div class="calinfo"><div class="calname">Auto Pan button — OFF state</div>'
        '<div class="caldesc">Turn Auto Pan OFF in-game, then click the button again. Saves its OFF colour.</div></div>'
        '<div class="calval"><span class="frstat" id="frstat_apoff">not set</span>'
        '<span class="calsw2" id="frsw_apoff"></span></div>'
        '<button type="button" class="btn2 frbtn" data-frk="apoff">Calibrate</button></div>'
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

    # Builds (dedicated page)
    nav("builds", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M12 12l8-4.5M12 12v9M12 12L4 7.5"/></svg>', "Builds")
    panels.append(
        '<section class="panel" id="pbuilds"><div class="phead"><h2>Builds</h2>'
        '<p class="chint">Every build is a FULL settings profile (all tabs + '
        'relics). Load applies everything; Overwrite captures your current '
        'settings into that build; click a description to edit it.</p></div>'
        '<div class="bldbar">'
        '<input id="bldsearch" placeholder="Search builds…" spellcheck="false">'
        '<select id="bldsort">'
        '<option value="new">Newest</option><option value="old">Oldest</option>'
        '<option value="used">Most used</option>'
        '<option value="recent">Recently used</option>'
        '<option value="az">A–Z</option></select>'
        '<span class="grow"></span>'
        '<input id="bldname2" placeholder="save current as…" spellcheck="false">'
        '<button type="button" class="btn" id="bldsave2">Save current</button>'
        '<button type="button" class="btn2" id="bldimport">Import build\u2026</button>'
        '<input type="file" id="bldimportfile" style="display:none">'
        '</div><div id="bldgrid" class="bldgrid"></div></section>')


    # History
    nav("hist", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 9 -9a9 9 0 0 0 -7 3.3L3 8"/><path d="M3 3v5h5"/><path d="M12 8v4l3 2"/></svg>', "History")
    panels.append(
        '<section class="panel" id="phist"><div class="phead"><h2>Run history</h2>'
        '<p class="chint">Past runs and their stats, kept across restarts.</p></div>'
        '<button type="button" id="histrefresh" class="btn2" style="margin-bottom:12px">Refresh</button>'
        '<div id="histbox" class="histbox"></div></section>')

    nav("keys", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6 9.5h.01M9.5 9.5h.01M13 9.5h.01M16.5 9.5h.01M6.5 13.5h11"/></svg>', "Keybinds")
    _kbrows = [("HOTKEY_TOGGLE", "Start / Stop"), ("HOTKEY_PAUSE", "Pause / Resume (keeps session)"),
               ("HOTKEY_RELIC_RESET", "Reset relic timers to full"),
               ("HOTKEY_SOFTSTOP", "Soft-stop (test)"),
               ("HOTKEY_QUIT", "Quit macro"), ("HOTKEY_POPOUT", "Toggle pop-out pill")]
    panels.append(
        '<section class="panel" id="pkeys"><div class="phead"><h2>Keybinds</h2>'
        '<p class="chint">Click a box, then press the key combo you want. They work globally while the macro is running. Stop &amp; Start the macro to apply.</p></div>'
        '<div class="rows">'
        + "".join(f'<label class="row"><span class="lbl">{lbl}</span>'
                  f'<button type="button" class="btn2 kb" data-kb="{key}" id="kb_{key}">…</button></label>'
                  for key, lbl in _kbrows)
        + '</div><div class="calactions"><button type="button" class="btn" id="savekeys">Save keybinds</button></div></section>')

    # ---- Cycle: the pan loop as a visual, tunable diagram ----------------
    # The engine-tuning sections stop being tabs and become STAGES of the
    # loop the player actually runs; every numeric setting renders as a
    # slider (bounds from the Coach's known-safe RANGES) synced to a precise
    # number box. Keys keep their data-key identity, so builds/config/Coach
    # are untouched. NOTHING is dropped: keys no stage claims fall into an
    # automatic "Other tuning" stage.
    nav("cycle", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M20 12a8 8 0 1 1-3-6.2"/><path d="M17 2.5l.3 3.6 3.6.3"/></svg>', "Cycle")
    _byname = {}
    for _t, _items in SECTIONS:
        for _k, _l, _ty, _d in _items:
            _byname[_k] = (_l, _ty, _d)
    _MOVED = ["Mode / Dig", "Walk back into water", "Shake",
              "Return to land (dig-probe)", "Recovery / safety",
              "Recovery movement (jitter taps)", "Easy tuning"]
    _moved_keys = [k for t, items in SECTIONS if t in _MOVED
                   for k, _l, _ty, _d in items]
    try:
        _RNG = dict(_coach.RANGES)
    except Exception:
        _RNG = {}
    _RNG.setdefault("SAFE_STOP_RETRY_SEC", (10, 600, 10))
    _RNG.setdefault("SAFE_STOP_MAX_RETRIES", (0, 10, 1))
    _STAGES = [
        ("dig", "Dig", "On land, pan empty: hold the click, fill the pan.",
         TAB_ICON.get("Mode / Dig", ""),
         ["PERFECT", "DIG_CLICK_MS", "DIG_SPEED", "MAX_DIGS_TO_FILL",
          "DIG_FILL_MS", "PRE_DIG_SETTLE_MS", "DIG_FILL_SMART",
          "DIG_PLATEAU_MS", "DIG_SMART_CAP_MS", "DIG_PIPELINE",
          "DIG_PIPELINE_GAP_MS", "EASY_FIRST_DIG_DELAY_MS"]),
        ("swalk", "Walk back", "Pan full: hold S until the Pan cue shows.",
         TAB_ICON.get("Walk back into water", ""),
         ["PAN_BACK_MAX_MS", "WATER_EXTRA_BACK_MS", "EASY_WATER_BACK_MS",
          "EASY_WATER_RETURN_DELAY_MS"]),
        ("glide", "Glide & start",
         "Hold W, build speed toward land — click RIGHT before the edge.",
         '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8h11a3 3 0 1 0-3-3M3 13h15a3 3 0 1 1-3 3M3 18h7"/></svg>',
         ["SHAKE_MOMENTUM_W", "SHAKE_W_LEAD_MS", "SHAKE_START_DELAY_MS",
          "EASY_SHAKE_DELAY_MS", "SHAKE_START_CONFIRM_MS",
          "SHAKE_START_RETRIES", "SHAKE_RETRY_DEEPER_MS", "SHAKE_BAIL_MS"]),
        ("shake", "Shake & drain",
         "Rapid clicks empty the pan while momentum carries you.",
         TAB_ICON.get("Shake", ""),
         ["SHAKE_CLICKS", "SHAKE_CLICK_MS", "SHAKE_CLICK_GAP_MS",
          "SHAKE_HOLD_MS", "SHAKE_STALL_MS"]),
        ("land", "Land & prove",
         "Slide onto land as it empties; settle, then prove the dig registered.",
         TAB_ICON.get("Return to land (dig-probe)", ""),
         ["POST_SHAKE_SETTLE_MS", "DEPOSIT_MAX_MS", "LAND_SETTLE_MS",
          "LAND_CUE_ASSIST", "LAND_ASSIST_MAX_MS", "DIG_PROBE_MS",
          "PROBE_GAP_MS", "LAND_PROBE_NUDGE_MS", "LAND_DIG_TRIES",
          "EASY_LAND_FWD_MS"]),
        ("safety", "Safety nets",
         "When something wedges: retries, nudges, break-outs, safe stops.",
         TAB_ICON.get("Recovery / safety", ""),
         ["RECOVER_ENABLED", "SHAKE_RETRY_ENABLED", "BREAKOUT_ENABLED",
          "STUCK_TICKS", "RECOVER_LIMIT", "RECOVER_BACK_MS",
          "SHAKE_FAIL_LIMIT", "SHAKE_GLITCH_LIMIT", "NO_PROGRESS_SEC",
          "BREAKOUT_LIMIT", "BREAKOUT_SHAKE_MS", "BREAKOUT_REPOS_MS",
          "SAFE_STOP_RETRY", "SAFE_STOP_RETRY_SEC", "SAFE_STOP_MAX_RETRIES",
          "BURST_ON_MS", "BURST_OFF_MS"]),
    ]
    _claimed = set()
    for _sid, _sn, _tag, _ic, _keys in _STAGES:
        _claimed.update(k for k in _keys if k in _byname)
    _left = [k for k in _moved_keys if k not in _claimed]
    if _left:
        _STAGES.append(("other", "Other tuning",
                        "Settings the stages didn't claim (nothing is ever "
                        "dropped).", TAB_ICON.get("Easy tuning", ""), _left))

    def _crow(key):
        lab, ty, _d = _byname[key]
        if ty == "bool":
            ctl = (f'<span class="switch"><input type="checkbox" data-key="{key}" '
                   f'data-type="bool"><span class="track"><span class="knob">'
                   f'</span></span></span>')
        elif ty == "str":
            ctl = (f'<input type="text" data-key="{key}" data-type="str" '
                   f'style="width:220px;text-align:left">')
        elif ty == "float":
            ctl = (f'<input type="number" data-key="{key}" data-type="float" '
                   f'step="0.005" min="0" style="width:110px">')
        else:
            r = _RNG.get(key)
            if r:
                ctl = (f'<span class="ctl"><input type="range" class="crng" '
                       f'data-for="{key}" min="{r[0]}" max="{r[1]}" '
                       f'step="{r[2]}">'
                       f'<input type="number" data-key="{key}" '
                       f'data-type="int" class="cnum"></span>')
            else:
                ctl = f'<input type="number" data-key="{key}" data-type="int">'
        return (f'<label class="row crow"><span class="lbl">{lab}{_qm(key)}'
                f'</span>{ctl}</label>')

    _cards = []
    for _sid, _sn, _tag, _ic, _keys in _STAGES:
        _rows = "".join(_crow(k) for k in _keys if k in _byname)
        _cards.append(
            f'<div class="cstage" id="cs_{_sid}">'
            f'<div class="cshdr"><span class="ti">{_ic}</span>'
            f'<div><h3>{_sn}</h3><p>{_tag}</p></div></div>'
            f'<div class="rows">{_rows}</div></div>')
    _cyc_svg = (
        '<div class="cycwrap"><svg viewBox="0 0 960 168" id="cycsvg">'
        '<defs><marker id="arw" viewBox="0 0 10 10" refX="8" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0 0L10 5L0 10z" fill="currentColor"/></marker></defs>'
        '<rect x="8" y="30" width="196" height="86" rx="14" class="cyland"/>'
        '<rect x="212" y="30" width="536" height="86" rx="14" class="cywater"/>'
        '<rect x="756" y="30" width="196" height="86" rx="14" class="cyland"/>'
        '<text x="106" y="24" class="cyzone">LAND</text>'
        '<text x="480" y="24" class="cyzone">WATER</text>'
        '<text x="854" y="24" class="cyzone">LAND</text>'
        + "".join(
            f'<g class="cnode" data-stage="{sid}" transform="translate({x},73)">'
            f'<circle r="21" class="cnc"/>'
            f'<g class="cni" transform="translate(-8.5,-8.5) scale(0.71)">'
            f'{ic.replace("<svg ", "<svg width=24 height=24 ", 1)}</g>'
            f'<text y="38" class="cnn">{nm}</text>'
            f'<text y="53" class="cnv" id="cyv_{sid}"></text></g>'
            for sid, nm, ic, x in [
                ("dig", "dig", TAB_ICON.get("Mode / Dig", ""), 106),
                ("swalk", "S — walk back", TAB_ICON.get("Walk back into water", ""), 288),
                ("glide", "W — glide", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8h11a3 3 0 1 0-3-3M3 13h15a3 3 0 1 1-3 3M3 18h7"/></svg>', 470),
                ("shake", "click! shake", TAB_ICON.get("Shake", ""), 648),
                ("land", "land & prove", TAB_ICON.get("Return to land (dig-probe)", ""), 854)])
        + '<path d="M133 73h128" class="cya"/><path d="M315 73h128" class="cya"/>'
          '<path d="M497 73h124" class="cya"/><path d="M675 73h152" class="cya"/>'
          '<path d="M854 100v34H150" class="cya cyaback"/>'
          '<text x="500" y="150" class="cyloop">momentum carries you out — the loop restarts</text>'
        '</svg></div>')
    panels.append(
        '<section class="panel" id="pcycle"><div class="phead"><h2>Cycle</h2>'
        '<p class="chint">This IS your pan loop. Click any stage in the '
        'diagram to jump to its knobs; the numbers on the diagram are live. '
        'Safety nets sit below the loop.</p></div>'
        + _cyc_svg
        + '<div class="cygraph"><div class="cyghd"><b>Cycle timeline</b>'
          '<span class="cygtot" id="cygtotals"></span><span class="grow"></span>'
          '<span class="cyghint">hover a bar for the settings behind it · '
          'click to jump</span></div>'
          '<svg id="cygsvg"></svg>'
          '<div class="cygnotes" id="cygnotes"></div>'
          '<div id="cygtip"></div></div>'
        + "".join(_cards) + '</section>')

    # Settings tabs (the engine-tuning sections live on the Cycle page now)
    for title, items in SECTIONS:
        if title in _MOVED:
            continue
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
            elif typ == "float":
                ctl = (f'<input type="number" data-key="{key}" data-type="float" '
                       f'step="0.005" min="0" style="width:130px">')
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
    PINNED = ["run", "cycle", "builds", "cal", "relics", "hist", "keys"]
    GROUPS = [
        ("Setup", ["Window"]),
        ("Modes", ["Treasure chest", "Shards", "Geodes"]),
        ("Recovery", ["Smart / experimental"]),
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
    return (HTML.replace("{{NAV}}", "".join(side))
            .replace("{{PANELS}}", "".join(panels))
            .replace("{{CYCMODEL}}", _CYCMODEL_JS))


_CYCMODEL_JS = r''' function cycModel(V,AB){
   const n=k=>{const x=parseInt(V[k]||0,10);return isNaN(x)?0:x;};
   const b=k=>!!V[k];
   const A=AB||{};
   const st={cap:+A.ab_cap||0,ds:+A.ab_ds||0,ss:+A.ab_ss||0,sspeed:+A.ab_sspeed||0};
   const anim=190000/Math.max(1,n('DIG_SPEED'));
   const rolls=ss=>4.03266e-9*ss*ss*ss-1.68935e-5*ss*ss+0.0255557*ss+0.206594;
   const drain=(st.cap&&st.ss&&st.sspeed)?1000*st.cap/(rolls(st.sspeed)*st.ss):null;
   const digsNeed=(st.cap&&st.ds)?Math.max(1,Math.ceil(st.cap/(1.5*st.ds))):null;
   const PAN_BACK=n('PAN_BACK_MAX_MS')+n('EASY_WATER_BACK_MS');
   const XTRA=n('WATER_EXTRA_BACK_MS')+n('EASY_WATER_BACK_MS');
   const SDLY=n('SHAKE_START_DELAY_MS')+n('EASY_SHAKE_DELAY_MS');
   const PRED=n('PRE_DIG_SETTLE_MS')+n('EASY_FIRST_DIG_DELAY_MS');
   const POST=n('POST_SHAKE_SETTLE_MS')+n('EASY_FIRST_DIG_DELAY_MS');
   const S=[],notes=[];
   const seg=(stage,name,lo,hi,o)=>{lo=Math.max(0,lo);hi=Math.max(lo,hi);
     if(hi<=0)return;S.push(Object.assign({stage,name,lo,hi,parts:[]},o||{}));};
   if(PRED>0)seg('dig','settle before dig',PRED,PRED,
     {parts:[['PRE_DIG_SETTLE_MS',n('PRE_DIG_SETTLE_MS')],['EASY_FIRST_DIG_DELAY_MS',n('EASY_FIRST_DIG_DELAY_MS')]]});
   const hold=b('PERFECT')?anim:n('DIG_CLICK_MS');
   const holdParts=b('PERFECT')?[['PERFECT','on: hold rides the dig-bar sweep'],['DIG_SPEED',n('DIG_SPEED')]]
                               :[['DIG_CLICK_MS',n('DIG_CLICK_MS')]];
   if(n('SHARDS_DIG_CLICKS')>0){
     seg('dig','dig click',hold,hold,{parts:holdParts,click:true});
     const win=Math.max(30,n('SHARDS_CLICK_CONFIRM_MS'));
     const pLo=b('SHARDS_GREEN_CONFIRM')?Math.min(40,win):Math.min(anim*0.5,win);
     seg('dig',b('SHARDS_GREEN_CONFIRM')?'prove click (green bar)':'prove click (bar moves)',
       pLo,win,{parts:[['SHARDS_CLICK_CONFIRM_MS',n('SHARDS_CLICK_CONFIRM_MS')],
                       ['SHARDS_GREEN_CONFIRM',b('SHARDS_GREEN_CONFIRM')?'on':'off']]});
     for(let k=1;k<n('SHARDS_DIG_CLICKS');k++){
       seg('dig','dig rhythm',anim+25,anim+25,{parts:[['DIG_SPEED',n('DIG_SPEED')]]});
       seg('dig','dig click',hold,hold,{parts:holdParts,click:true});}
     if(!b('SHARDS_ASSUME_FULL'))
       seg('dig','wait for FULL',Math.max(0,anim-pLo),Math.max(600,n('DIG_FILL_MS')),
         {parts:[['DIG_FILL_MS',n('DIG_FILL_MS')],['SHARDS_ASSUME_FULL','off']]});
     else notes.push('assume-full: the walk to water happens DURING the fill animation');
   }else{
     seg('dig','probe dig',hold,hold,{parts:holdParts,click:true});
     seg('dig','prove rise',Math.min(anim,n('DIG_PROBE_MS')),n('DIG_PROBE_MS'),
       {parts:[['DIG_PROBE_MS',n('DIG_PROBE_MS')]]});
     const nd=Math.min(Math.max(digsNeed||n('MAX_DIGS_TO_FILL'),1),Math.max(1,n('MAX_DIGS_TO_FILL')));
     if(nd>1&&b('DIG_PIPELINE')){
       const gap=n('DIG_PIPELINE_GAP_MS')>0?n('DIG_PIPELINE_GAP_MS'):anim+25;
       for(let k=1;k<nd;k++){seg('dig','pipeline gap',gap,gap,
           {parts:[['DIG_PIPELINE_GAP_MS',n('DIG_PIPELINE_GAP_MS')],['DIG_SPEED',n('DIG_SPEED')]]});
         seg('dig','dig click',hold,hold,{parts:holdParts,click:true});}
       seg('dig','wait for FULL',Math.min(anim,n('DIG_FILL_MS')),Math.max(n('DIG_FILL_MS'),anim),
         {parts:[['DIG_FILL_MS',n('DIG_FILL_MS')]]});
     }else{
       for(let k=1;k<nd;k++){
         if(b('DIG_FILL_SMART'))seg('dig','smart fill watch',anim,anim+n('DIG_PLATEAU_MS'),
           {jump:'DIG_PLATEAU_MS',parts:[['DIG_FILL_SMART','on'],['DIG_PLATEAU_MS',n('DIG_PLATEAU_MS')]]});
         else seg('dig','wait FULL',Math.min(anim,n('DIG_FILL_MS')),n('DIG_FILL_MS'),
           {parts:[['DIG_FILL_MS',n('DIG_FILL_MS')]]});
         seg('dig','dig click',hold,hold,{parts:holdParts,click:true});}
       if(b('DIG_FILL_SMART'))seg('dig','last fill',anim,anim+n('DIG_PLATEAU_MS'),
         {jump:'DIG_PLATEAU_MS',parts:[['DIG_FILL_SMART','on'],['DIG_PLATEAU_MS',n('DIG_PLATEAU_MS')]]});
       else seg('dig','last fill',Math.min(anim,n('DIG_FILL_MS')),n('DIG_FILL_MS'),
         {parts:[['DIG_FILL_MS',n('DIG_FILL_MS')]]});
     }
     if(digsNeed===null)notes.push('digs-to-fill unknown (save your stats in Auto-build) — showing Max digs');
   }
   seg('swalk','S — until the Pan cue',PAN_BACK,PAN_BACK,
     {parts:[['PAN_BACK_MAX_MS',n('PAN_BACK_MAX_MS')],['EASY_WATER_BACK_MS',n('EASY_WATER_BACK_MS')]],
      note:'budget — the cue usually fires sooner',cue:true});
   if(XTRA>0)seg('swalk','deeper (extra S)',XTRA,XTRA,
     {parts:[['WATER_EXTRA_BACK_MS',n('WATER_EXTRA_BACK_MS')],['EASY_WATER_BACK_MS',n('EASY_WATER_BACK_MS')]]});
   if(SDLY>0)seg('glide','start delay',SDLY,SDLY,
     {parts:[['SHAKE_START_DELAY_MS',n('SHAKE_START_DELAY_MS')],['EASY_SHAKE_DELAY_MS',n('EASY_SHAKE_DELAY_MS')]]});
   if(b('SHAKE_MOMENTUM_W')){
     if(n('SHAKE_W_LEAD_MS')>0)seg('glide','W — momentum glide',n('SHAKE_W_LEAD_MS'),n('SHAKE_W_LEAD_MS'),
       {parts:[['SHAKE_W_LEAD_MS',n('SHAKE_W_LEAD_MS')]]});
   }else notes.push('Hold W during shake is OFF — no momentum, no glide');
   const cad=Math.max(1,n('SHAKE_CLICK_MS')+n('SHAKE_CLICK_GAP_MS'));
   if(n('SHAKE_CLICKS')>0){
     const d=cad*n('SHAKE_CLICKS');
     seg('shake','shake — exactly '+n('SHAKE_CLICKS')+' clicks',d,d,
       {parts:[['SHAKE_CLICKS',n('SHAKE_CLICKS')],['SHAKE_CLICK_MS',n('SHAKE_CLICK_MS')],
               ['SHAKE_CLICK_GAP_MS',n('SHAKE_CLICK_GAP_MS')]],ticks:cad});
   }else{
     const lo=drain!==null?Math.min(drain,n('SHAKE_HOLD_MS')):n('SHAKE_HOLD_MS');
     seg('shake',drain!==null?'shake — drain (est. from your stats)':'shake — until empty (cap)',
       lo,n('SHAKE_HOLD_MS'),
       {parts:[['SHAKE_CLICK_MS',n('SHAKE_CLICK_MS')],['SHAKE_CLICK_GAP_MS',n('SHAKE_CLICK_GAP_MS')],
               ['SHAKE_HOLD_MS',n('SHAKE_HOLD_MS')],['SHAKE_BAIL_MS',n('SHAKE_BAIL_MS')]],
        ticks:cad,bail:n('SHAKE_BAIL_MS')});
     if(drain===null)notes.push('drain time unknown (save your stats in Auto-build) — showing the cap');
   }
   if(POST>0)seg('land','settle onto land',POST,POST,
     {parts:[['POST_SHAKE_SETTLE_MS',n('POST_SHAKE_SETTLE_MS')],['EASY_FIRST_DIG_DELAY_MS',n('EASY_FIRST_DIG_DELAY_MS')]]});
   let est=0,cap=0;
   S.forEach(x=>{est+=x.lo;cap+=x.hi;});
   return {segs:S,est,cap,notes,pph:est>0?3600000/est:0,drain,digsNeed};
 }/*CYCMODEL-END*/'''


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
 .big.pause{background:#6d5836;color:#f4ead6} .big.pause:hover{filter:brightness(1.08)}
 .big.pause.on{background:var(--accent);color:#241a02}
 .relbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:10px 2px 0;min-height:0}
 .relbar:empty{display:none}
 .relbar .lblx{color:var(--dim);font-size:10px;text-transform:uppercase;letter-spacing:.06em;font-weight:700}
 .relchip{display:inline-flex;gap:7px;align-items:center;background:var(--panel);
   border:1px solid var(--line2);border-radius:9px;padding:5px 11px;font-size:12px;
   color:var(--mut);font-weight:600}
 .relchip b{color:var(--accent-lit);font-variant:tabular-nums;font-weight:700}
 .relbar select,.relbar input{background:var(--bg2);border:1px solid var(--line2);
   border-radius:7px;color:var(--txt);padding:6px 9px;font:inherit;font-size:12px}
 .relbar select:focus,.relbar input:focus{outline:none;border-color:var(--accent)}
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
 .cycwrap{max-width:980px;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:10px 12px 4px;margin-bottom:16px}
 #cycsvg{width:100%;height:auto;display:block}
 .cyland{fill:rgba(194,146,76,.10);stroke:rgba(194,146,76,.35)}
 .cywater{fill:rgba(107,161,181,.10);stroke:rgba(107,161,181,.35)}
 .cyzone{fill:var(--dim);font-size:10px;letter-spacing:.14em;text-anchor:middle;font-weight:700}
 .cya{stroke:var(--dim);stroke-width:1.6;fill:none;marker-end:url(#arw);color:var(--dim)}
 .cyaback{stroke-dasharray:4 5;opacity:.6}
 .cyloop{fill:var(--dim);font-size:10.5px;text-anchor:middle;font-style:italic}
 .cnode{cursor:pointer} .cnc{fill:var(--bg2);stroke:var(--line2);stroke-width:1.4;transition:stroke .15s}
 .cnode:hover .cnc{stroke:var(--accent)} .cni{color:var(--accent-lit)}
 .cni svg{width:24px;height:24px;overflow:visible}
 .cnn{fill:var(--txt);font-size:11px;text-anchor:middle;font-weight:600}
 .cnv{fill:var(--accent-lit);font-size:10.5px;text-anchor:middle;font-variant-numeric:tabular-nums}
 .cstage{max-width:980px;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin-bottom:14px;scroll-margin-top:16px}
 .cstage.pulse{border-color:var(--accent);box-shadow:0 0 0 2px var(--sand-dim)}
 .cshdr{display:flex;gap:11px;align-items:flex-start;margin-bottom:8px}
 .cshdr .ti{color:var(--accent-lit);margin-top:2px} .cshdr .ti svg{width:17px;height:17px}
 .cshdr h3{margin:0;font-size:14px} .cshdr p{margin:2px 0 0;color:var(--mut);font-size:12px}
 .crow .ctl{display:flex;gap:10px;align-items:center}
 .crng{width:190px;accent-color:var(--accent)}
 .cnum{width:78px}
 .cygraph{max-width:980px;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin-bottom:16px;position:relative}
 .cyghd{display:flex;gap:8px;align-items:baseline;margin-bottom:6px}
 .cyghd b{font-size:13px} .cygtot{color:var(--accent-lit);font-size:12px;font-variant-numeric:tabular-nums}
 .cyghint{color:var(--dim);font-size:11px}
 #cygsvg{width:100%;height:auto;display:block}
 .cygax{fill:var(--dim);font-size:9.5px;text-anchor:middle}
 .cyglab{fill:#241a02;font-size:10.5px;text-anchor:middle;font-weight:700;pointer-events:none}
 .cyglane{fill:var(--dim);font-size:9px;font-weight:700}
 .cygbail{fill:#e07b5f;font-size:9px;text-anchor:middle}
 .cygseg{cursor:pointer}
 .cygnotes{color:var(--mut);font-size:11.5px;margin-top:4px;min-height:14px}
 .hlrow{background:var(--sand-glow)!important;outline:1.5px solid var(--accent);border-radius:9px;transition:background .3s,outline .3s}
 #cygtip{display:none;position:fixed;z-index:60;background:var(--bg2);border:1px solid var(--line2);border-radius:9px;padding:8px 11px;font-size:11.5px;color:var(--txt);max-width:240px;pointer-events:none;line-height:1.5}
 .bldbar{display:flex;gap:9px;align-items:center;margin-bottom:14px;flex-wrap:wrap;max-width:980px}
 .bldbar input,.bldbar select{background:var(--field);color:var(--txt);border:1px solid var(--line2);border-radius:8px;padding:8px 10px;font:inherit}
 .bldbar input:focus,.bldbar select:focus{outline:none;border-color:var(--accent)}
 .bfile{display:flex;align-items:center;gap:8px;margin:6px 0 2px;font-size:12px;color:var(--mut);flex-wrap:wrap}
 .bfile .bfn{color:var(--txt);font-weight:600;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .bfile .bxfile{background:none;border:none;color:var(--dim);cursor:pointer;font-size:13px;padding:0 2px}
 .bfile .bxfile:hover{color:var(--txt)}
 #bldsearch{width:220px} #bldname2{width:190px}
 .bldgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px;max-width:980px}
 .bcard{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 15px;display:flex;flex-direction:column;gap:8px}
 .bcard:hover{border-color:var(--line2)}
 .bhead{display:flex;align-items:center;gap:8px} .bhead h3{margin:0;font-size:14.5px;flex:1;color:var(--accent-lit);font-weight:600}
 .bdel{background:transparent;color:var(--dim);padding:2px 9px;border-radius:7px;font-size:12px} .bdel:hover{background:#3a201c;color:#f0c0b0}
 .bdesc{color:var(--mut);font-size:12.5px;line-height:1.45;cursor:text;min-height:18px}
 .bdesc.empty{color:var(--dim);font-style:italic} .bdesc:hover{color:var(--txt)}
 .bta{width:100%;min-height:52px;background:var(--field);color:var(--txt);border:1px solid var(--line2);border-radius:8px;padding:7px 9px;font:inherit;font-size:12.5px;resize:vertical;margin-bottom:7px}
 .bstats{color:var(--dim);font-size:11.5px;font-variant:tabular-nums}
 .bbtns{display:flex;gap:8px} .bbtns .btn,.bbtns .btn2{padding:7px 12px;font-size:12.5px}
 .bldempty{color:var(--dim);padding:16px 4px}

 .asec{color:var(--accent-lit);font-weight:700;font-size:11px;letter-spacing:.05em;text-transform:uppercase;margin:16px 2px 8px}
 .agrid{display:grid;grid-template-columns:repeat(4,1fr);gap:9px}
 .acard{background:var(--panel);border:1px solid var(--line2);border-radius:11px;padding:11px 13px}
 .acard .al{color:var(--dim);font-size:10px;text-transform:uppercase;letter-spacing:.05em;font-weight:700}
 .acard .av{font-size:21px;font-weight:800;color:var(--txt);margin-top:3px;font-variant:tabular-nums}
 .acard .as{color:var(--mut);font-size:11px;margin-top:3px}
 .arow{display:flex;flex-wrap:wrap;gap:7px;align-items:center;margin:0 2px 6px}
 .arow .albl{color:var(--dim);font-size:10px;text-transform:uppercase;letter-spacing:.05em;font-weight:700;width:66px}
 .achip{background:var(--panel);border:1px solid var(--line2);border-radius:8px;padding:4px 9px;font-size:12px;color:var(--mut);font-weight:600}
 .achip b{color:var(--accent-lit)}
 .adim{color:var(--dim);font-size:12px}
 .atbl{width:100%;border-collapse:collapse;margin-top:4px}
 .atbl th{color:var(--dim);text-transform:uppercase;font-size:9px;letter-spacing:.05em;text-align:left;padding:5px 8px;border-bottom:1px solid var(--line)}
 .atbl td{font-size:12px;padding:5px 8px;border-bottom:1px solid rgba(51,47,42,.5);font-variant:tabular-nums;color:var(--txt)}

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
 .tab.active .ti{opacity:1}
 .tab.active:before{content:"";position:absolute;left:0;top:7px;bottom:7px;width:2px;border-radius:2px;background:var(--accent)}
 .navsep{height:1px;background:var(--line);margin:10px 4px}
 .pretitle{color:var(--dim);font-size:10.5px;font-weight:600;text-transform:uppercase;letter-spacing:.8px;margin:14px 8px 7px}
 .chip{display:block;width:100%;text-align:left;background:var(--panel);color:#cfc6b4;
  border:1px solid var(--line);border-radius:8px;padding:8px 11px;margin-bottom:6px;font-size:13px;
  transition:border-color .15s,color .15s}
 .chip:hover{border-color:var(--line2);color:var(--txt)}
 .content{flex:1;overflow-y:auto;padding:28px 32px}
 /* ---- Coach (tuning assistant) ---- */
 .coach{flex:0 0 360px;display:none;flex-direction:column;min-height:0;background:var(--bg2);border-left:1px solid var(--line);position:relative}
 body.coach-on .coach{display:flex}
 body.coach-expand .coach{position:fixed;left:0;right:0;bottom:0;top:53px;z-index:60;flex:none;border-left:0;box-shadow:0 0 40px rgba(0,0,0,.4)}
 #coachtoggle.on{background:var(--accent);color:#241a02}
 .coach-head{flex:0 0 auto;display:flex;align-items:center;gap:9px;padding:12px 10px 11px 14px;border-bottom:1px solid var(--line)}
 .coach-mark{color:var(--accent-lit);font-size:15px;line-height:1;flex:0 0 auto}
 .coach-titlewrap{min-width:0;flex:1}
 .coach-title{font-weight:700;font-size:14px;line-height:1.1}
 .coach-sub{display:block;font-weight:600;font-size:9.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.07em;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .coach-hbtns{display:flex;gap:2px;flex:0 0 auto}
 .coach-hbtns button{background:transparent;color:var(--mut);border:0;border-radius:7px;width:28px;height:28px;cursor:pointer;font-size:14px;display:flex;align-items:center;justify-content:center;padding:0;line-height:1}
 .coach-hbtns button:hover{background:#2a2418;color:var(--txt)}
 body.coach-expand #coachexpand{background:var(--accent);color:#241a02}
 .coach-msgs{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:12px;width:100%;box-sizing:border-box}
 body.coach-expand .coach-msgs{padding:22px 16px}
 body.coach-expand .coach-msgs,body.coach-expand .coach-chips,body.coach-expand .coach-input,body.coach-expand .coach-cfg{max-width:840px;margin-left:auto;margin-right:auto}
 .cmsg{font-size:13.5px;line-height:1.55;max-width:100%;word-wrap:break-word;overflow-wrap:anywhere}
 .cmsg.user{align-self:flex-end;background:var(--accent);color:#241a02;padding:9px 13px;border-radius:14px 14px 4px 14px;max-width:85%;font-weight:500}
 .cmsg.bot{align-self:flex-start;background:var(--panel);border:1px solid var(--line);color:var(--txt);padding:11px 14px;border-radius:14px 14px 14px 4px;max-width:90%}
 .cmsg.bot b{color:var(--accent-lit);font-weight:600} .cmsg.bot i{color:var(--mut);font-style:normal}
 .cmsg.bot code{background:#15140f;padding:1px 5px;border-radius:4px;font-size:12px;font-family:ui-monospace,Menlo,monospace}
 .cmsg.typing{color:var(--dim);font-style:italic;animation:cpulse 1.2s ease-in-out infinite}
 @keyframes cpulse{0%,100%{opacity:.55}50%{opacity:1}}
 .cdiff{margin-top:11px;border:1px solid var(--line2);border-radius:11px;overflow:hidden;background:#1b1a17}
 .cdiff-h{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--accent-lit);font-weight:700;padding:9px 12px 2px}
 .cdiff-row{padding:5px 12px} .cdiff-row+.cdiff-row{border-top:1px solid var(--line)}
 .cdiff-top{display:flex;align-items:baseline;gap:8px;font-size:12.5px}
 .cdiff-k{flex:1;color:var(--txt);min-width:0;word-wrap:break-word} .cdiff-v{font-variant:tabular-nums;font-weight:700;white-space:nowrap;flex:0 0 auto}
 .cdiff-v s{color:var(--dim);text-decoration:none;margin-right:6px} .cdiff-v b{color:var(--teal-lit)}
 .cdiff-why{font-size:11px;color:var(--mut);margin-top:2px;line-height:1.4}
 .cdiff-act{display:flex;gap:8px;padding:10px 12px;background:#161512;margin-top:4px}
 .cdiff-act button{flex:1;border:0;border-radius:8px;padding:9px;font-weight:700;cursor:pointer;font-size:12.5px}
 .capply{background:var(--accent2);color:#14260f} .capply:hover{filter:brightness(1.08)}
 .cdismiss{background:#2a2418;color:#cdbfa5} .cdismiss:hover{background:#352d1c}
 .cdiff.done{opacity:.7} .cdiff.done .cdiff-act{display:none}
 .cdiff-state{font-size:11px;font-weight:700;padding:9px 12px;display:none}
 .cdiff.applied .cdiff-state.ok{display:block;color:var(--accent2)} .cdiff.skipped .cdiff-state.no{display:block;color:var(--dim)}
 .coach-chips{flex:0 0 auto;display:flex;flex-wrap:wrap;gap:6px;padding:0 14px 10px;width:100%;box-sizing:border-box}
 .cchip{background:#2a2418;color:#d8cdb3;border:1px solid var(--line2);border-radius:14px;padding:5px 11px;font-size:11.5px;cursor:pointer}
 .cchip:hover{background:#352d1c;color:#fff}
 .coach-input{flex:0 0 auto;display:flex;gap:8px;padding:12px 14px;border-top:1px solid var(--line);width:100%;box-sizing:border-box}
 .coach-input textarea{flex:1;resize:none;background:var(--panel);border:1px solid var(--line2);border-radius:11px;color:var(--txt);padding:10px 12px;font:inherit;font-size:13.5px;max-height:140px;line-height:1.4}
 .coach-input textarea:focus{outline:0;border-color:var(--accent);box-shadow:0 0 0 3px rgba(194,146,76,.18)}
 .coach-input button{background:var(--accent);color:#241a02;border:0;border-radius:11px;padding:0 16px;font-weight:700;cursor:pointer}
 .coach-input button:hover{filter:brightness(1.06)} .coach-input button:disabled{opacity:.5;cursor:default}
 .cstats{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:11px}
 .cstats label{display:flex;flex-direction:column;font-size:10px;color:var(--mut);gap:3px;text-transform:uppercase;letter-spacing:.04em}
 .cstats input{background:var(--bg2);border:1px solid var(--line2);border-radius:7px;color:var(--txt);padding:7px 8px;font:inherit;font-size:13px}
 .cstats input:focus{outline:0;border-color:var(--accent)}
 .cstats .cgo{grid-column:1/3;background:var(--accent2);color:#14260f;border:0;border-radius:8px;padding:9px;font-weight:700;cursor:pointer;margin-top:2px}
 .coach-cfg{display:none;flex-direction:column;gap:11px;padding:14px;border-bottom:1px solid var(--line);background:#1c1b18}
 body.coach-cfg-on .coach-cfg{display:flex}
 .ccfg-field{display:flex;flex-direction:column;gap:5px}
 .ccfg-lab{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);font-weight:700}
 .coach-cfg select,.coach-cfg input{background:var(--bg2);border:1px solid var(--line2);border-radius:9px;color:var(--txt);padding:9px 11px;font:inherit;font-size:13px;width:100%;box-sizing:border-box;-webkit-appearance:none;appearance:none}
 .coach-cfg select{background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' fill='none' stroke='%239c9183' stroke-width='2'><path d='M2 4l4 4 4-4'/></svg>");background-repeat:no-repeat;background-position:right 11px center;padding-right:30px;cursor:pointer}
 .coach-cfg select:focus,.coach-cfg input:focus{outline:0;border-color:var(--accent);box-shadow:0 0 0 3px rgba(194,146,76,.18)}
 #ccfgcloud{display:flex;flex-direction:column;gap:10px}
 .ccfg-act{display:flex;gap:8px;margin-top:2px}
 .ccfg-save{flex:1;background:var(--accent2);color:#14260f;border:0;border-radius:9px;padding:9px;font-weight:700;cursor:pointer}
 .ccfg-save:hover{filter:brightness(1.07)}
 .ccfg-clear{background:#2a2418;color:#cdbfa5;border:0;border-radius:9px;padding:9px 12px;font-weight:700;cursor:pointer}
 .ccfg-clear:hover{background:#352d1c}
 .ccfg-note{font-size:10.5px;color:var(--dim);line-height:1.5} .ccfg-note b{color:var(--mut)}
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
 .geodebar{display:flex;align-items:center;gap:12px;background:rgba(212,148,58,.12);border:1px solid rgba(212,148,58,.32);border-radius:10px;padding:10px 14px;margin:0 0 12px}
 .geodebar .gtl{font-size:12px;font-weight:600;color:#f0b95a;text-transform:uppercase;letter-spacing:.05em;white-space:nowrap}
 .geodebar .gtv{font-size:18px;font-weight:700;font-variant-numeric:tabular-nums;min-width:58px}
 .geodebar .gtbar{flex:1;height:8px;background:rgba(255,255,255,.06);border-radius:4px;overflow:hidden}
 .geodebar .gtbar i{display:block;height:100%;background:#d4943a}
 .histbox{max-width:680px;display:flex;flex-direction:column;gap:8px}
 .hrow{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:11px 13px}
 .hr-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
 .hr-top b{font-weight:600;color:var(--txt)}
 .hr-reason{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--accent-lit);
  background:var(--sand-dim);border:1px solid var(--sand-glow);border-radius:999px;padding:2px 9px}
 .hr-stats{color:var(--mut);font-size:12.5px;font-family:ui-monospace,Menlo,monospace}
 .hr-why{font-size:11.5px;color:var(--mut);margin-top:7px;line-height:1.55} .hr-why b{color:var(--accent-lit);font-weight:600}
 .hr-det{margin-top:8px;font-size:11.5px}
 .hr-det summary{cursor:pointer;color:var(--accent-lit);font-weight:600;list-style:none;user-select:none}
 .hr-det summary::-webkit-details-marker{display:none} .hr-det summary:before{content:"\25B8 "} .hr-det[open] summary:before{content:"\25BE "}
 .hr-det .ev{font:11.5px ui-monospace,Menlo,monospace;color:var(--mut);padding:3px 0;border-top:1px solid var(--line)}
 .hr-det .ev:first-of-type{margin-top:5px}
 .hr-det .evt{color:var(--dim);display:inline-block;min-width:46px} .hr-det .evk{color:var(--teal-lit)}
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
 .upd b{font-weight:700}
 .upd .grow{flex:1}
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
   <button class="btn2" id="buildsbtn" title="All saved builds — load, organise, describe">⛏ Builds</button>
   <button class="btn2" id="coachtoggle" title="Open the offline tuning coach">✦ Coach</button>
   <button class="btn2" id="analyticsbtn" title="Open the analytics dashboard">☷ Analytics</button>
   <button class="btn2" id="popout" title="Pop out a floating control">⤢ Pop out</button>
   <button class="btn2" id="hudbtn" title="Live overlay HUD beside the game">⬒ HUD</button>
   <button class="btn" id="savebtn">Save settings</button>
 </div>
 <div class="body">
   <nav class="side">
     {{NAV}}
     <div class="navsep"></div>
     <div class="pretitle">Quick presets</div>
     <button type="button" class="chip" id="pv1">v1 · fast 1-dig</button>
     <button type="button" class="chip" id="pv2">v2 · multi-dig</button>
     <button type="button" class="chip" id="pv3">v3 · geode</button>
     <button type="button" class="chip" id="pdef">Reset defaults</button>
   </nav>
   <div class="content">{{PANELS}}</div>
   <aside class="coach" id="coach">
     <div class="coach-head">
       <span class="coach-mark">✦</span>
       <div class="coach-titlewrap"><div class="coach-title">Coach</div><span class="coach-sub" id="coachsub">offline</span></div>
       <div class="coach-hbtns">
         <button id="coachnew" title="New chat">⊕</button>
         <button id="coachcfg" title="Settings (offline / API)">⚙</button>
         <button id="coachexpand" title="Open in a separate window">⤢</button>
         <button id="coachclose" title="Close">✕</button>
       </div>
     </div>
     <div class="coach-cfg" id="coachcfgpanel">
       <div class="ccfg-field"><span class="ccfg-lab">Engine</span>
         <select id="cprovider">
           <option value="offline">Offline brain — free, instant</option>
           <option value="anthropic">Claude (Anthropic)</option>
           <option value="openai">OpenAI (GPT)</option>
           <option value="gemini">Google Gemini</option>
           <option value="deepseek">DeepSeek</option>
           <option value="custom">Custom / local</option>
         </select></div>
       <div id="ccfgcloud">
         <div class="ccfg-field"><span class="ccfg-lab">Model</span>
           <select id="cmodelsel"></select></div>
         <div class="ccfg-field" id="ccfgmodelrow" style="display:none"><span class="ccfg-lab">Model id</span>
           <input id="cmodel" type="text" autocomplete="off" placeholder="exact model id"></div>
         <div class="ccfg-field" id="ccfgbaserow" style="display:none"><span class="ccfg-lab">Base URL</span>
           <input id="cbase" type="text" autocomplete="off" placeholder="https://…/v1"></div>
         <div class="ccfg-field"><span class="ccfg-lab">API key</span>
           <input id="ckey" type="password" autocomplete="off" placeholder="paste key — stays on this PC"></div>
       </div>
       <div class="ccfg-act"><button class="ccfg-save" id="ccfgsave">Save</button><button class="ccfg-clear" id="ccfgclear">Clear key</button></div>
       <div class="ccfg-note" id="ccfgnote">Offline is free and needs nothing. Cloud engines use <b>your own key</b> (it stays on this PC) and cost a fraction of a cent per message.</div>
     </div>
     <div class="coach-msgs" id="coachmsgs"></div>
     <div class="coach-chips" id="coachchips"></div>
     <div class="coach-input">
       <textarea id="coachin" rows="1" placeholder="Describe a problem… e.g. it shakes late"></textarea>
       <button id="coachsend">Send</button>
     </div>
   </aside>
 </div>
 <div class="ok" id="toast"></div>
<script>
 let DEF={},V1={},V2={},GEODE={};
 const $=s=>document.querySelector(s), $$=s=>document.querySelectorAll(s);
 const fields=()=>$$('[data-key]');
 function setVals(v){fields().forEach(el=>{const k=el.dataset.key;
   if(!(k in v))return; // key not in the loaded build/config -> KEEP the current
   // value (old builds must never zero settings added after they were saved)
   if(el.dataset.type==='bool')el.checked=!!v[k]; else el.value=(v[k]??'');});
   if(window.syncCycle)syncCycle();}
 function collect(){const o={};fields().forEach(el=>{const k=el.dataset.key,t=el.dataset.type;
   o[k]=(t==='bool')?el.checked:(t==='str')?el.value:(t==='float')?parseFloat(el.value||'0'):parseInt(el.value||'0',10);});return o;}
 function preset(p){fields().forEach(el=>{const k=el.dataset.key; if(!(k in p))return;
   if(el.dataset.type==='bool')el.checked=!!p[k]; else el.value=p[k];});
   if(window.syncCycle)syncCycle();}
 function toast(t){const e=$('#toast');e.textContent=t;e.classList.add('show');
   clearTimeout(window._tt);window._tt=setTimeout(()=>e.classList.remove('show'),1800);}
 window.addLog=t=>{const l=$('#log');if(!l)return;let s=l.textContent+t+"\n";
   if(s.length>80000)s=s.slice(-60000).replace(/^[^\n]*\n/,'');
   l.textContent=s;l.scrollTop=l.scrollHeight;};
 window.refreshValues=async function(){try{const s=await window.pywebview.api.get_state();setVals(s.values);}catch(e){}};
 window.setRunning=r=>{$('#startbtn').disabled=r;$('#stopbtn').disabled=!r;
   $('#rstate').textContent=r?'running':'stopped';};
 window.fmtBig=window.fmtBig||function(n){n=Number(n)||0;const a=Math.abs(n);
   if(a>=1e15)return (n/1e15).toFixed(2)+'Q';
   if(a>=1e12)return (n/1e12).toFixed(2)+'T';
   if(a>=1e9)return (n/1e9).toFixed(2)+'B';
   if(a>=1e6)return (n/1e6).toFixed(2)+'M';
   if(a>=1e3)return (n/1e3).toFixed(1)+'K';
   return String(Math.round(n));};
 const fmtBig=window.fmtBig;
 window.setPaused=p=>{const b=$('#pausebtn');
   if(b){b.textContent=p?'Resume':'Pause';b.classList.toggle('on',!!p);}
   const r=$('#rstate');if(r&&p)r.textContent='paused';};
 window.geodeTimer=function(ms,label){const el=document.getElementById('geodebar');if(!el)return;
   if(window._ggInt){clearInterval(window._ggInt);window._ggInt=null;}
   if(!ms||ms<=0){el.style.display='none';return;}
   const end=performance.now()+ms,tot=ms;
   const upd=()=>{const left=Math.max(0,end-performance.now());
     el.innerHTML='<span class="gtl">'+(label||'geode fill')+'</span>'
       +'<span class="gtv">'+(left/1000).toFixed(1)+'s</span>'
       +'<span class="gtbar"><i style="width:'+(100*left/tot).toFixed(1)+'%"></i></span>';
     if(left<=0){clearInterval(window._ggInt);window._ggInt=null;}};
   el.style.display='flex';upd();window._ggInt=setInterval(upd,100);};
 window.setStats=s=>{if(!s)return;const m=Math.floor((s.runtime_s||0)/60),
   sec=String((s.runtime_s||0)%60).padStart(2,'0');
   $('#st_run').textContent=m+':'+sec; $('#st_cyc').textContent=s.cycles||0;
   $('#st_rate').textContent=s.pans_per_hr||0; $('#st_rec').textContent=s.recoveries||0;
   const _ce=$('#st_clean'); if(_ce)_ce.textContent=(s.cycles&&s.clean_pct!=null)?(s.clean_pct+'%'):'\u2014';
   const _de=$('#st_digs'); if(_de)_de.textContent=s.digs||0;
   const _mp=$('#st_mph'); if(_mp)_mp.textContent=(s.money_earned?('$'+fmtBig(s.money_per_hr||0)):'\u2014');
   const _sp=$('#st_sph'); if(_sp)_sp.textContent=(s.shards_earned?fmtBig(s.shards_per_hr||0):'\u2014');
   const _set=(id,v)=>{const e=document.getElementById(id);if(e)e.textContent=v;};
   _set('st_nud',s.nudges||0);_set('st_miss',s.shake_misses||0);_set('st_rel',s.relics_used||0);
   _set('st_safe',s.safe_stops||0);_set('st_hard',s.hard_stops||0);
   const _rl=document.getElementById('relicline');
   const R=s.relics||[];
   if(_rl){_rl.innerHTML=R.length?('<span class="lblx">\u23f3 relics</span>'+
     R.map(r=>'<span class="relchip">'+r.name+' <b>'+Math.floor(r.left_s/60)+':'+String(r.left_s%60).padStart(2,'0')+'</b></span>').join('')):'';}
   _relicSetUI(R);};
 window._relNames='';
 window._relicSetUI=function(R){const box=document.getElementById('relicset');
   if(!box)return;
   const names=(R||[]).map(r=>r.name).join('|');
   if(names===window._relNames)return;   // rebuild only when the set changes
   window._relNames=names;
   if(!R||!R.length){box.innerHTML='';return;}
   box.innerHTML='<span class="lblx">set timer</span>'+
     '<select id="rsi" style="max-width:170px">'+
     R.map((r,i)=>'<option value="'+i+'">'+r.name+'</option>').join('')+
     '</select><input id="rst" placeholder="3m 4s" style="width:90px">'+
     '<button type="button" class="btn2" id="rsb">Set</button>'+
     '<button type="button" class="btn2" id="rsr">Reset to full</button>'+
     '<span class="lblx" style="margin-left:auto">ctrl+shift+1\u20139 one \u00b7 ctrl+U all</span>';
   const parse=t=>{t=(t||'').trim().toLowerCase();if(!t)return null;
     let m=t.match(/^(\d+):(\d{1,2})$/);if(m)return (+m[1])*60+(+m[2]);
     let tot=0,ok=false;
     m=t.match(/(\d+)\s*m/);if(m){tot+=(+m[1])*60;ok=true;}
     m=t.match(/(\d+)\s*s/);if(m){tot+=(+m[1]);ok=true;}
     if(ok)return tot;
     if(/^\d+$/.test(t))return +t;
     return null;};
   const rsb=document.getElementById('rsb');
   if(rsb)rsb.onclick=async()=>{const i=document.getElementById('rsi').value;
     const secs=parse(document.getElementById('rst').value);
     if(secs==null){toast('Time like: 3m 4s, 3:04, or 184');return;}
     try{await window.pywebview.api.relic_set(i,secs);toast('Timer set');}catch(e){}};
   const rsr=document.getElementById('rsr');
   if(rsr)rsr.onclick=async()=>{const i=document.getElementById('rsi').value;
     try{await window.pywebview.api.relic_reset_one(i);toast('Timer reset to full');}catch(e){}};};
 const EVLBL={safe_stop:'Safe-stops',hard_stop:'Hard-stops',nudge:'Nudges',recover:'Recoveries',break_out:'Break-outs',shake_fail:'Failed shakes',shake_glitch:'Shake-glitch',no_progress:'No-progress',fr_recover:'FR recovery',recenter:'X recenter',relic:'Relics'};
 const EVORDER=['safe_stop','hard_stop','no_progress','shake_fail','shake_glitch','recover','break_out','nudge','recenter','fr_recover','relic'];
 const _esc=s=>(s==null?'':String(s)).replace(/&/g,'&amp;').replace(/</g,'&lt;');
 function _whyLine(r){const rc=r.reason_counts||{};const g={};
   Object.keys(rc).forEach(k=>{const i=k.indexOf(': ');const t=i<0?k:k.slice(0,i);const w=i<0?'':k.slice(i+2);(g[t]=g[t]||[]).push([w,rc[k]]);});
   const parts=[];EVORDER.forEach(t=>{if(!g[t])return;g[t].sort((a,b)=>b[1]-a[1]);
     const top=g[t].slice(0,3).map(x=>x[1]+'\u00d7 '+_esc(x[0])).join(', ');parts.push('<b>'+(EVLBL[t]||t)+':</b> '+top);});
   return parts.length?('<div class="hr-why">'+parts.join(' \u00b7 ')+'</div>'):'';}
 function _timeline(r){const ev=r.events||[];if(!ev.length)return '';
   const rows=ev.slice(-150).map(e=>'<div class="ev"><span class="evt">'+(e.t||0)+'s</span> <span class="evk">'+(EVLBL[e.type]||e.type)+'</span> '+_esc(e.reason||'')+'</div>').join('');
   return '<details class="hr-det"><summary>Detailed timeline ('+ev.length+' events)</summary>'+rows+'</details>';}
 async function loadHistory(){let list=[];try{list=await window.pywebview.api.run_history();}catch(e){}
   const box=document.getElementById('histbox');if(!box)return;
   if(!list||!list.length){box.innerHTML='<div class="hempty">No runs yet \u2014 finish a session and it shows up here.</div>';return;}
   box.innerHTML=list.map(r=>{const m=Math.floor((r.runtime_s||0)/60);
     return '<div class="hrow"><div class="hr-top"><b>'+(r.ended||'run')+'</b>'+
       (r.tracker?'<span style="color:#7d9b63;font-weight:700;margin-left:6px">TRACKER</span>':'')+
       '<span class="hr-reason">'+_esc(r.reason||r.stop_reason||'')+'</span></div>'+
       '<div class="hr-stats">'+(r.cycles||0)+' pans \u00b7 '+(r.digs!=null?(r.digs+' digs \u00b7 '):'')+(r.pans_per_hr||0)+'/hr \u00b7 '+m+' min \u00b7 '+
       (r.recoveries||0)+' rec \u00b7 '+(r.nudges||0)+' nudges \u00b7 '+(r.relics_used||0)+' relics \u00b7 '+
       (r.safe_stops||0)+' safe \u00b7 '+(r.hard_stops||0)+' hard'+
       ((r.clean_pct!=null&&r.cycles)?(' \u00b7 '+r.clean_pct+'% clean'):'')+'</div>'+
       _phases(r)+_earn(r)+_whyLine(r)+_timeline(r)+'</div>';}).join('');}
 function _earn(r){const m=r.money_earned||0,s=r.shards_earned||0;
   if(!m&&!s)return '';
   return '<div class="hr-stats" style="color:#9ec98a">earned: $'+fmtBig(m)+
     ' ($'+fmtBig(r.money_per_hr||0)+'/hr, $'+fmtBig(r.money_per_pan||0)+'/pan)'+
     ' \u00b7 '+fmtBig(s)+' shards ('+fmtBig(r.shards_per_hr||0)+'/hr, '+
     (r.shards_per_pan||0)+'/pan)</div>';}
 function _phases(r){const p=r.phase_timings;if(!p)return '';
   const ks=Object.keys(p);if(!ks.length)return '';
   const parts=ks.map(k=>k+' '+(p[k].mean_ms/1000).toFixed(2)+'s (p95 '+(p[k].p95_ms/1000).toFixed(2)+'s)');
   return '<div class="hr-stats" style="opacity:.75">phases: '+parts.join(' \u00b7 ')+'</div>';}
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
   const id=b.dataset.tab; const pid=(id==='run'||id==='cycle'||id==='builds'||id==='cal'||id==='relics'||id==='hist'||id==='keys')?('p'+id):('p_'+id);
   document.getElementById(pid).classList.add('active');
   const _g=b.closest('.navgroup');if(_g)_g.classList.remove('collapsed');
   if(id==='hist')loadHistory();
   if(id==='builds')loadBuildsPage();});
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
     const ap=document.getElementById('frstat_apon');
     if(ap&&f.AUTOPAN_BTN_PIXEL&&(f.AUTOPAN_BTN_PIXEL[0]||f.AUTOPAN_BTN_PIXEL[1])){
       ap.textContent='('+f.AUTOPAN_BTN_PIXEL[0]+', '+f.AUTOPAN_BTN_PIXEL[1]+')';
       const w1=document.getElementById('frsw_apon');
       if(w1&&f.AUTOPAN_ON_RGB)w1.style.background='rgb('+f.AUTOPAN_ON_RGB.join(',')+')';}
     const af=document.getElementById('frstat_apoff');
     if(af&&f.AUTOPAN_OFF_RGB&&(f.AUTOPAN_OFF_RGB[0]||f.AUTOPAN_OFF_RGB[1]||f.AUTOPAN_OFF_RGB[2])){
       af.textContent='saved';
       const w2=document.getElementById('frsw_apoff');
       if(w2)w2.style.background='rgb('+f.AUTOPAN_OFF_RGB.join(',')+')';}
     const q=document.getElementById('frstat_srtext');
     if(q&&f.SR_TEXT_RGB&&(f.SR_TEXT_RGB[0]||f.SR_TEXT_RGB[1]||f.SR_TEXT_RGB[2])){
       q.textContent='rgb('+f.SR_TEXT_RGB.join(',')+')';
       const w=document.getElementById('frsw_srtext');
       if(w)w.style.background='rgb('+f.SR_TEXT_RGB.join(',')+')';}
     const sw=document.getElementById('frsw_text');if(sw&&f.FR_TEXT_RGB)sw.style.background='rgb('+f.FR_TEXT_RGB.join(',')+')';}
   if(f.FR_BOX_TOP){fr.top=f.FR_BOX_TOP;const s=document.getElementById('frstat_top');if(s)s.textContent='y='+f.FR_BOX_TOP;}
   if(f.FR_BOX_BOTTOM){fr.bottom=f.FR_BOX_BOTTOM;const s=document.getElementById('frstat_bottom');if(s)s.textContent='y='+f.FR_BOX_BOTTOM;}
   if(f.FR_OPEN_PIXEL&&(f.FR_OPEN_PIXEL[0]||f.FR_OPEN_PIXEL[1])){fr.open=f.FR_OPEN_PIXEL;
     const s=document.getElementById('frstat_open');if(s)s.textContent='('+f.FR_OPEN_PIXEL[0]+', '+f.FR_OPEN_PIXEL[1]+')';}
   if(f.FR_HOME_PIXEL&&(f.FR_HOME_PIXEL[0]||f.FR_HOME_PIXEL[1])){fr.home=f.FR_HOME_PIXEL;
     const s=document.getElementById('frstat_home');if(s)s.textContent='('+f.FR_HOME_PIXEL[0]+', '+f.FR_HOME_PIXEL[1]+')';}}
 const FRKEY={text:'FR_TEXT',srtext:'SR_TEXT',apon:'AUTOPAN_ON',apoff:'AUTOPAN_OFF',top:'FR_BOX_TOP',bottom:'FR_BOX_BOTTOM',open:'FR_OPEN_PIXEL',home:'FR_HOME_PIXEL'};
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
 // detect window
 // one-click auto-calibrate from the detected window
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
 (function(){const b=document.getElementById('findtest');if(b)b.onclick=async()=>{
     const box=document.getElementById('findout');
     box.innerHTML='<div class="detrow">reading\u2026</div>';
     let r;try{r=await window.pywebview.api.test_find_read();}catch(e){box.innerHTML='<div class="detrow det-no">failed: '+e+'</div>';return;}
     if(r.error){box.innerHTML='<div class="detrow det-no">'+r.error+'</div>';return;}
     const lines=(r.lines||[]);
     box.innerHTML='<div class="detrow"><b>OCR lines:</b> '+(lines.length?lines.map(x=>'\u201c'+x+'\u201d').join(' · '):'(nothing — widen/aim the region while a find is showing)')+'</div>';};})();
 (function(){const b=document.getElementById('earntest');if(!b)return;
   b.onclick=async()=>{const box=document.getElementById('earnout');
     box.innerHTML='<div class="detrow">reading\u2026</div>';
     let r;try{r=await window.pywebview.api.test_earn_read();}catch(e){
       box.innerHTML='<div class="detrow det-no">failed: '+e+'</div>';return;}
     box.innerHTML='<div class="detrow"><b>money:</b> '+(r.money||'?')+'</div>'+
                   '<div class="detrow"><b>shards:</b> '+(r.shards||'?')+'</div>';};})();
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
 $('#pv1').onclick=()=>preset(V1); $('#pv2').onclick=()=>preset(V2); $('#pv3').onclick=()=>preset(GEODE); $('#pdef').onclick=()=>preset(DEF);
 // save / run
 $('#savebtn').onclick=async()=>{const n=await window.pywebview.api.save_config(collect());toast('Saved '+n+' settings');};
 $('#popout').onclick=()=>{try{window.pywebview.api.popout();}catch(e){}};
 $('#analyticsbtn').onclick=async()=>{try{await window.pywebview.api.open_analytics_window();}catch(e){}};
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
 $('#saverelics').onclick=async()=>{const n=await window.pywebview.api.save_relics(collectRelics(),$('#relicsMaster').checked);toast('Saved '+n+' relic(s)');};
 $('#startbtn').onclick=async()=>{await window.pywebview.api.launch(collect(),collectRelics(),$('#relicsMaster').checked);setRunning(true);toast('Launched — Ctrl+K to start');};
 $('#stopbtn').onclick=async()=>{await window.pywebview.api.stop();setRunning(false);};
 (function(){const b=$('#hudbtn');if(b)b.onclick=async()=>{
   try{const r=await window.pywebview.api.hud_toggle();
     toast(r==='shown'?'HUD on — drag it beside Roblox':'HUD hidden');}catch(e){}};})();
 (function(){const b=$('#pausebtn');if(b)b.onclick=async()=>{
   try{await window.pywebview.api.pause_toggle();}catch(e){}};})();
 // builds — a build saves ALL settings + relics; loading applies them all.
 // The dedicated Builds PAGE owns listing/search/sort/descriptions.
 async function loadBuild(name){if(!name)return;
   const e=await window.pywebview.api.load_build(name);if(!e)return;
   setVals(e); setRelics(e.RELICS||[], e.RELICS_ENABLED); toast('Loaded build "'+name+'"');}
 let BLD={list:[],q:'',sort:'new'};
 function bldDate(ts){if(!ts)return '—';return new Date(ts*1000).toLocaleDateString();}
 async function loadBuildsPage(){try{BLD.list=await window.pywebview.api.builds_info()||[];}
   catch(e){BLD.list=[];}
   renderBuildsPage();}
 function renderBuildsPage(){const g=$('#bldgrid');if(!g)return;
   const q=BLD.q.toLowerCase();
   const L=BLD.list.filter(b=>!q||b.name.toLowerCase().includes(q)||(b.desc||'').toLowerCase().includes(q));
   const s=BLD.sort;
   L.sort((a,b)=> s==='new'?(b.created-a.created)||a.name.localeCompare(b.name)
     : s==='old'?(a.created-b.created)||a.name.localeCompare(b.name)
     : s==='used'?(b.used-a.used)||a.name.localeCompare(b.name)
     : s==='recent'?(b.last_used-a.last_used)||a.name.localeCompare(b.name)
     : a.name.localeCompare(b.name));
   g.innerHTML='';
   if(!L.length){const d=document.createElement('div');d.className='bldempty';
     d.textContent=BLD.list.length?'No builds match that search.':'No builds yet — save your current settings above.';
     g.appendChild(d);return;}
   L.forEach(b=>{const c=document.createElement('div');c.className='bcard';
     c.innerHTML='<div class="bhead"><h3></h3><button type="button" class="bdel" title="Delete this build">✕</button></div>'
       +'<div class="bdesc" title="Click to edit the description"></div>'
       +'<div class="bfile"></div>'
       +'<div class="bstats"></div>'
       +'<div class="bbtns"><button type="button" class="btn bload">Load</button>'
       +'<button type="button" class="btn2 bover" title="Overwrite this build with the CURRENT settings">Overwrite</button>'
       +'<button type="button" class="btn2 bexport" title="Save this build to a file you can send to a friend">Export</button>'
       +'<button type="button" class="btn2 battach" title="Attach a Roblox-build doc (Word/PDF/image) to this build">Attach doc</button></div>';
     c.querySelector('h3').textContent=b.name;
     const de=c.querySelector('.bdesc');
     de.textContent=b.desc||'Add a description…';
     if(!b.desc)de.classList.add('empty');
     c.querySelector('.bstats').textContent=[(b.nset||0)+' settings',
       (b.relics?b.relics+' relic'+(b.relics>1?'s':''):'no relics'),
       'used '+(b.used||0)+'×','created '+bldDate(b.created),
       (b.last_used?'last used '+bldDate(b.last_used):'never used')].join(' · ');
     c.querySelector('.bload').onclick=async()=>{await loadBuild(b.name);loadBuildsPage();};
     c.querySelector('.bover').onclick=async()=>{
       await window.pywebview.api.save_build(b.name,collect(),collectRelics(),$('#relicsMaster').checked);
       toast('"'+b.name+'" overwritten with current settings');loadBuildsPage();};
     const bf=c.querySelector('.bfile');
     if(b.has_file){
       bf.innerHTML='<span class="bfn" title="'+(b.file_name||'')+'">\ud83d\udcce '+(b.file_name||'attached file')+'</span>'
         +'<button type="button" class="btn2 bdl">Download Roblox build</button>'
         +'<button type="button" class="bxfile" title="Remove attachment">✕</button>';
       bf.querySelector('.bdl').onclick=async()=>{const r=await window.pywebview.api.download_build_file(b.name);
         if(r&&r.ok)toast('Saved '+(b.file_name||'file')); else if(r&&!r.cancelled)toast((r&&r.error)||'Download failed');};
       bf.querySelector('.bxfile').onclick=async()=>{await window.pywebview.api.remove_build_file(b.name);toast('Attachment removed');loadBuildsPage();};
     }
     c.querySelector('.bexport').onclick=async()=>{const r=await window.pywebview.api.export_build(b.name);
       if(r&&r.ok)toast('Exported \u2014 send the .ppbuild file to a friend'); else if(r&&!r.cancelled)toast((r&&r.error)||'Export failed');};
     c.querySelector('.battach').onclick=async()=>{const r=await window.pywebview.api.attach_build_file(b.name);
       if(r&&r.ok){toast('Attached '+r.file_name);loadBuildsPage();} else if(r&&!r.cancelled)toast((r&&r.error)||'Attach failed');};
     const del=c.querySelector('.bdel');
     if(b.builtin){del.style.display='none';}
     del.onclick=async()=>{if(!del.dataset.arm){del.dataset.arm='1';del.textContent='sure?';
         setTimeout(()=>{del.dataset.arm='';del.textContent='✕';},2500);return;}
       await window.pywebview.api.delete_build(b.name);toast('Deleted "'+b.name+'"');loadBuildsPage();};
     de.onclick=()=>{if(de.querySelector('textarea'))return;
       de.classList.remove('empty');
       de.innerHTML='<textarea class="bta"></textarea>'
         +'<div class="bbtns"><button type="button" class="btn bdsave">Save</button>'
         +'<button type="button" class="btn2 bdcancel">Cancel</button></div>';
       const ta=de.querySelector('textarea');ta.value=b.desc||'';ta.focus();
       de.querySelector('.bdsave').onclick=async ev=>{ev.stopPropagation();
         await window.pywebview.api.set_build_desc(b.name,ta.value.trim());loadBuildsPage();};
       de.querySelector('.bdcancel').onclick=ev=>{ev.stopPropagation();renderBuildsPage();};};
     g.appendChild(c);});}
 (function(){const s=$('#bldsearch');if(s)s.addEventListener('input',()=>{BLD.q=s.value.trim();renderBuildsPage();});
   const o=$('#bldsort');if(o)o.onchange=()=>{BLD.sort=o.value;renderBuildsPage();};
   const n=$('#bldname2'),sv=$('#bldsave2');
   if(sv)sv.onclick=async()=>{const name=(n.value||'').trim();
     if(!name){toast('Enter a build name');return;}
     await window.pywebview.api.save_build(name,collect(),collectRelics(),$('#relicsMaster').checked);
     n.value='';toast('Build "'+name+'" saved (all settings)');loadBuildsPage();};
   const imp=$('#bldimport'),impf=$('#bldimportfile');
   const impDone=r=>{if(!r)return;if(r.cancelled)return;
     if(r.ok){toast('Imported "'+r.name+'"'+(r.has_file?' (with doc)':''));loadBuildsPage();}
     else toast(r.error||'Import failed');};
   if(imp){imp.onclick=async()=>{let r=null;
     try{r=await window.pywebview.api.import_build_dialog();}catch(_){r=null;}
     if(!r||r.error==='unavailable'){if(impf)impf.click();return;}
     impDone(r);};}
   if(impf){impf.onchange=async ev=>{const f=ev.target.files[0];if(!f)return;const text=await f.text();impf.value='';
     let r;try{r=await window.pywebview.api.import_build(text);}catch(_){r={ok:false,error:'could not read file'};}
     impDone(r);};}
   const bb=$('#buildsbtn');
   if(bb)bb.onclick=()=>{const t=document.querySelector('.tab[data-tab="builds"]');if(t)t.click();};})();
 $('#savebuild').onclick=async()=>{const name=$('#buildname').value.trim();
   if(!name){toast('Enter a build name');return;}
   await window.pywebview.api.save_build(name,collect(),collectRelics(),$('#relicsMaster').checked);
   toast('Build "'+name+'" saved (all settings)');loadBuildsPage();};
 // ==== Cycle timeline: an accurate model of ONE clean cycle ====
 // Every duration mirrors the ENGINE's own laws: the EASY-offset folding
 // load_config performs, the mode branches (Shards exact-click, dig
 // pipeline, smart fill, fixed-click shake), the community stat formulas
 // (dig animation = 190000/DIG_SPEED ms; shake drain = capacity/(rolls x
 // shake strength)). Segments carry [lo,hi]: lo = best estimate, hi = the
 // engine's hard budget/cap; the hatched tail is the uncertain part.

 {{CYCMODEL}}

 function cygJump(key){if(!key)return;
   const el=document.querySelector('#pcycle [data-key="'+key+'"]');
   if(!el)return;
   const row=el.closest('.row')||el;
   row.scrollIntoView({behavior:'smooth',block:'center'});
   row.classList.add('hlrow');
   setTimeout(()=>row.classList.remove('hlrow'),1900);
   if(el.type==='number'){try{el.focus({preventScroll:true});el.select();}catch(e){}}
   else if(el.type==='checkbox'){try{el.focus({preventScroll:true});}catch(e){}}}
 let _cygRAF=0;
 function renderCycGraph(){if(_cygRAF)return;
   _cygRAF=requestAnimationFrame(()=>{_cygRAF=0;_cygDraw();});}
 function _cygDraw(){
   const svg=document.getElementById('cygsvg');if(!svg)return;
   const M=cycModel(collect(),window._AB||{});
   const W=920,H=134,L=10,R=8,axisY=96;
   const total=Math.max(M.cap,1);
   const x=t=>L+(W-L-R)*t/total;
   const col={dig:'#c2924c',swalk:'#6ba1b5',glide:'#9bc07e',shake:'#e0b873',land:'#b58f6b'};
   let d='<defs><pattern id="cyghatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="6" stroke="rgba(236,228,214,.30)" stroke-width="2"/></pattern></defs>';
   const step=total>4000?1000:total>2000?500:total>800?250:100;
   for(let t=0;t<=total;t+=step){const xx=x(t);
     d+='<line x1="'+xx+'" y1="16" x2="'+xx+'" y2="'+axisY+'" stroke="rgba(255,255,255,.05)"/>'
       +'<text x="'+xx+'" y="'+(axisY+12)+'" class="cygax">'+(t>=1000?(t/1000)+'s':t+'ms')+'</text>';}
   let t0=0;
   M.segs.forEach((s,i)=>{
     const a=x(t0),lo=x(t0+s.lo),hi=x(t0+s.hi);
     const c=col[s.stage]||'#8b8375';
     d+='<rect class="cygseg" data-i="'+i+'" x="'+a+'" y="20" width="'+Math.max(1.2,lo-a)+'" height="30" rx="3" fill="'+c+'" fill-opacity="0.85"/>';
     if(hi>lo+0.5)d+='<rect class="cygseg" data-i="'+i+'" x="'+lo+'" y="20" width="'+(hi-lo)+'" height="30" rx="3" fill="url(#cyghatch)" stroke="'+c+'" stroke-opacity=".5" stroke-dasharray="3 3"/>';
     if(s.ticks){const lim=Math.min(70,Math.floor(s.lo/s.ticks));
       for(let k=1;k<=lim;k++){const tx=x(t0+k*s.ticks);
         d+='<line x1="'+tx+'" y1="23" x2="'+tx+'" y2="47" stroke="rgba(20,15,8,.45)" stroke-width="1"/>';}}
     if(s.bail){const bx=x(t0+s.bail);
       d+='<line x1="'+bx+'" y1="15" x2="'+bx+'" y2="52" stroke="#e07b5f" stroke-width="1.4" stroke-dasharray="3 2"/>'
         +'<text x="'+bx+'" y="12" class="cygbail">bail</text>';}
     if((hi-a)>s.name.length*6.4+12)d+='<text x="'+((a+hi)/2)+'" y="39" class="cyglab">'+s.name+'</text>';
     t0+=s.hi;});
   d+='<text x="'+L+'" y="60" class="cyglane">S</text>'
     +'<text x="'+L+'" y="72" class="cyglane">W</text>'
     +'<text x="'+L+'" y="84" class="cyglane">CLK</text>';
   t0=0;const spans={S:[],W:[],C:[]};
   M.segs.forEach(s=>{const a=t0,bx=t0+s.hi;
     if(s.stage==='swalk')spans.S.push([a,bx]);
     if(s.stage==='glide'&&s.name.indexOf('W')===0)spans.W.push([a,bx]);
     if(s.stage==='shake'){spans.W.push([a,bx]);spans.C.push([a,bx]);}
     if(s.click)spans.C.push([a,bx]);
     t0+=s.hi;});
   const drawSpans=(arr,y,c)=>arr.forEach(sp=>{
     d+='<rect x="'+(x(sp[0])+16)+'" y="'+y+'" width="'+Math.max(1,x(sp[1])-x(sp[0])-16)+'" height="7" rx="2" fill="'+c+'" fill-opacity=".75"/>';});
   drawSpans(spans.S,54,'#6ba1b5');drawSpans(spans.W,66,'#9bc07e');drawSpans(spans.C,78,'#e0b873');
   svg.setAttribute('viewBox','0 0 '+W+' '+H);
   svg.innerHTML=d;
   const tot=document.getElementById('cygtotals');
   if(tot)tot.textContent=' — est '+(M.est/1000).toFixed(2)+'s · budget '+(M.cap/1000).toFixed(2)
     +'s · ≈'+Math.round(M.pph)+' pans/hr at est';
   const nt=document.getElementById('cygnotes');
   if(nt)nt.textContent=M.notes.join('  ·  ');
   const tip=document.getElementById('cygtip');
   svg.querySelectorAll('.cygseg').forEach(r=>{
     const s=M.segs[+r.dataset.i];
     r.addEventListener('mousemove',e=>{if(!tip)return;
       tip.style.display='block';
       tip.innerHTML='<b>'+s.name+'</b> — '+(s.lo===s.hi?Math.round(s.lo)+'ms'
           :Math.round(s.lo)+'–'+Math.round(s.hi)+'ms')
         +(s.note?'<br><i>'+s.note+'</i>':'')
         +'<br>'+s.parts.map(p=>p[0]+' = '+p[1]).join('<br>')
         +'<br><i>click → edit '+(s.jump||(s.parts[0]&&s.parts[0][0])||'')+'</i>';
       tip.style.left=Math.min(e.clientX+12,window.innerWidth-260)+'px';
       tip.style.top=(e.clientY+14)+'px';});
     r.addEventListener('mouseleave',()=>{if(tip)tip.style.display='none';});
     r.addEventListener('click',()=>cygJump(s.jump||(s.parts[0]&&s.parts[0][0])));});
 }
 // ---- Cycle page: sliders <-> numbers, live diagram values, stage jump ----
 function _cyv(k){const el=document.querySelector('[data-key="'+k+'"]');
   if(!el)return 0;
   return el.dataset.type==='bool'?(el.checked?1:0):parseInt(el.value||'0',10);}
 window.syncCycle=function(){
   document.querySelectorAll('#pcycle .crng').forEach(r=>{
     const n=document.querySelector('[data-key="'+r.dataset.for+'"]');
     if(n&&document.activeElement!==r)r.value=n.value||0;});
   const set=(id,t)=>{const e=document.getElementById(id);if(e)e.textContent=t;};
   set('cyv_dig',_cyv('DIG_CLICK_MS')+'ms · ×'+_cyv('MAX_DIGS_TO_FILL'));
   set('cyv_swalk',_cyv('PAN_BACK_MAX_MS')+(_cyv('WATER_EXTRA_BACK_MS')?'+'+_cyv('WATER_EXTRA_BACK_MS'):'')+'ms');
   set('cyv_glide',_cyv('SHAKE_MOMENTUM_W')?(_cyv('SHAKE_W_LEAD_MS')+'ms W'):'W off!');
   set('cyv_shake',_cyv('SHAKE_CLICK_MS')+'/'+_cyv('SHAKE_CLICK_GAP_MS')+'ms');
   set('cyv_land',_cyv('POST_SHAKE_SETTLE_MS')+'ms settle');
   renderCycGraph();
 };
 (function(){const p=document.getElementById('pcycle');if(!p)return;
   p.addEventListener('input',e=>{const t=e.target;
     if(t&&t.classList&&t.classList.contains('crng')){
       const n=document.querySelector('[data-key="'+t.dataset.for+'"]');
       if(n)n.value=t.value;}});
   document.addEventListener('input',e=>{const t=e.target;
     if(t&&((t.dataset&&t.dataset.key)||(t.classList&&t.classList.contains('crng'))))syncCycle();});
   p.querySelectorAll('.cnode').forEach(g=>{g.addEventListener('click',()=>{
     const c=document.getElementById('cs_'+g.dataset.stage);
     if(c){c.scrollIntoView({behavior:'smooth',block:'start'});
       c.classList.add('pulse');setTimeout(()=>c.classList.remove('pulse'),1200);}});});
   syncCycle();})();
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
   DEF=s.defaults;V1=s.v1;V2=s.v2;GEODE=s.geode||{};window._AB=s.autobuild||{};setVals(s.values);setRunning(s.running);
   setRelics(s.relics||[],s.relics_enabled);loadBuildsPage();setPixels(s.pixels||{});setColors(s.colors||{});setFR(s.fr||{});setHotkeys(s.hotkeys||{});
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
   if(a&&a.unlocked){init();}else{gateShow();
     const err=document.getElementById('gateErr');
     if(err&&a&&a.moved){err.textContent='This copy was activated on another computer. Enter your code to use it here.';}
     else if(err&&a&&a.revoked){err.textContent='This access code has been deactivated by the owner.';}}}
 (function(){const f=document.getElementById('gateForm');if(!f)return;
   f.addEventListener('submit',async e=>{e.preventDefault();
     const inp=document.getElementById('gateCode'),btn=document.getElementById('gateGo'),err=document.getElementById('gateErr');
     const code=(inp.value||'').trim();if(!code){err.textContent='Enter your access code.';return;}
     btn.disabled=true;btn.textContent='Checking…';err.textContent='';
     let r={};try{r=await _api().verify_access(code);}catch(e){r={ok:false,error:'Something went wrong. Try again.'};}
     btn.disabled=false;btn.textContent='Unlock';
     if(r&&r.ok){gateHide();init();}else{err.textContent=(r&&r.error)||'That code did not work.';inp.select();}});})();

 // ---- Coach: offline tuning assistant ----
 (function(){
   const body=document.body, tgl=document.getElementById('coachtoggle'),
     clo=document.getElementById('coachclose'), msgs=document.getElementById('coachmsgs'),
     chipsEl=document.getElementById('coachchips'), inp=document.getElementById('coachin'),
     sendBtn=document.getElementById('coachsend'),
     newBtn=document.getElementById('coachnew'), expBtn=document.getElementById('coachexpand');
   if(!tgl||!msgs) return;
   let prevTopic='', greeted=false, sending=false, hist=[];
   const capi=()=>window.pywebview&&window.pywebview.api;
   const esc=s=>(s==null?'':String(s)).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
   function md(s){s=esc(s);
     s=s.replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/`([^`]+?)`/g,'<code>$1</code>');
     s=s.replace(/(^|[^*])\*(?!\*)([^*\n]+?)\*(?!\*)/g,'$1<i>$2</i>');
     s=s.replace(/^\s*[-•]\s+/gm,'• ');
     return s.replace(/\n/g,'<br>');}
   const scroll=()=>{msgs.scrollTop=msgs.scrollHeight;};
   const fmtVal=v=>(typeof v==='boolean')?(v?'on':'off'):v;
   function persist(){try{capi().save_coach_history(hist);}catch(e){}}
   function addUser(t){const d=document.createElement('div');d.className='cmsg user';d.textContent=t;msgs.appendChild(d);scroll();}
   function diffCard(ch, live){
     const wrap=document.createElement('div');wrap.className='cdiff'+(live?'':' done');
     const rows=ch.map(c=>`<div class="cdiff-row"><div class="cdiff-top"><span class="cdiff-k">${esc(c.label||c.key)}</span><span class="cdiff-v"><s>${esc(fmtVal(c.from))}</s><b>${esc(fmtVal(c.to))}</b></span></div>${c.reason?('<div class="cdiff-why">'+esc(c.reason)+'</div>'):''}</div>`).join('');
     wrap.innerHTML='<div class="cdiff-h">Suggested change'+(ch.length>1?'s':'')+'</div>'+rows
       +'<div class="cdiff-state ok">✓ Applied — press Save settings to keep</div><div class="cdiff-state no">Not applied</div>'
       +(live?('<div class="cdiff-act"><button class="capply">Implement '+ch.length+' change'+(ch.length>1?'s':'')+'</button><button class="cdismiss">Don’t apply</button></div>'):'');
     if(live){
       wrap.querySelector('.capply').onclick=async()=>{
         let r={};try{r=await capi().assistant_apply(ch);}catch(e){r={};}
         wrap.classList.add('done','applied');
         if(r&&r.values&&window.setVals)setVals(r.values);
         if(window.toast)toast('Applied '+((r&&r.applied)||ch.length)+' change(s)');};
       wrap.querySelector('.cdismiss').onclick=()=>wrap.classList.add('done','skipped');
     }
     return wrap;
   }
   function statsForm(){
     const fields=[['capacity','Capacity'],['dig_strength','Dig strength'],['dig_speed','Dig speed %'],['shake_strength','Shake strength'],['shake_speed','Shake speed'],['luck','Luck'],['walk_speed','Walk speed']];
     const f=document.createElement('div');f.className='cstats';
     f.innerHTML=fields.map(x=>`<label>${x[1]}<input type="number" data-st="${x[0]}" step="any"></label>`).join('')+'<button class="cgo">Analyze with these stats</button>';
     f.querySelector('.cgo').onclick=()=>{
       const st={};f.querySelectorAll('input[data-st]').forEach(i=>{const v=parseFloat(i.value);if(!isNaN(v))st[i.dataset.st]=v;});
       if(!Object.keys(st).length){if(window.toast)toast('Enter at least your capacity & dig strength');return;}
       const lbl='My stats: '+Object.entries(st).map(e=>e[0]+'='+e[1]).join(', ');
       addUser(lbl);hist.push({role:'user',text:lbl});persist();ask('analyze my stats and make it faster', st);};
     return f;
   }
   function addBot(res, live){
     const d=document.createElement('div');d.className='cmsg bot';d.innerHTML=md(res.reply||'');
     if(res.changes&&res.changes.length)d.appendChild(diffCard(res.changes, live!==false));
     if(res.askStats&&live!==false)d.appendChild(statsForm());
     msgs.appendChild(d);scroll();
     if(live!==false)setChips(res.chips||[]);
   }
   function setChips(ch){chipsEl.innerHTML='';(ch||[]).forEach(c=>{const b=document.createElement('button');b.className='cchip';b.textContent=c;b.onclick=()=>{inp.value=c;doSend();};chipsEl.appendChild(b);});}
   function buildConvo(){const out=[];hist.slice(-14).forEach(t=>{if(t.role==='user')out.push({role:'user',text:t.text});else if(t.role==='bot'&&t.reply)out.push({role:'assistant',text:t.reply});});return out;}
   async function ask(text,stats){
     sending=true;if(sendBtn)sendBtn.disabled=true;
     const typing=document.createElement('div');typing.className='cmsg bot typing';typing.textContent='Coach is thinking…';msgs.appendChild(typing);scroll();
     let res;try{res=await capi().assistant_chat(text, prevTopic, stats||null, buildConvo());}catch(e){res={reply:'I could not reach the engine — try reopening the app.',changes:[],chips:[]};}
     typing.remove();sending=false;if(sendBtn)sendBtn.disabled=false;
     prevTopic=(res&&res.topic)||'';
     addBot(res||{reply:'(no response)'}, true);
     hist.push({role:'bot',reply:(res&&res.reply)||'',changes:(res&&res.changes)||[],chips:(res&&res.chips)||[]});persist();
   }
   function doSend(){const t=(inp.value||'').trim();if(!t||sending)return;
     addUser(t);hist.push({role:'user',text:t});persist();
     inp.value='';inp.style.height='auto';ask(t);}
   sendBtn.onclick=doSend;
   inp.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();doSend();}});
   inp.addEventListener('input',()=>{inp.style.height='auto';inp.style.height=Math.min(140,inp.scrollHeight)+'px';});
   function greet(){if(greeted)return;greeted=true;
     (async()=>{let h=[];try{h=await capi().coach_history();}catch(e){}
       if(Array.isArray(h)&&h.length){hist=h;
         h.forEach(t=>{if(t.role==='user')addUser(t.text);else if(t.role==='bot')addBot({reply:t.reply,changes:t.changes,chips:t.chips},false);});
         const lb=[...h].reverse().find(t=>t.role==='bot');setChips((lb&&lb.chips)||[]);scroll();
       }else{ask('hello');}})();}
   function newChat(){hist=[];msgs.innerHTML='';chipsEl.innerHTML='';persist();greeted=true;ask('hello');if(inp)inp.focus();}
   function openCoach(){body.classList.add('coach-on');tgl.classList.add('on');greet();setTimeout(()=>inp.focus(),60);}
   function closeCoach(){body.classList.remove('coach-on');body.classList.remove('coach-expand');tgl.classList.remove('on');}
   tgl.onclick=()=>{body.classList.contains('coach-on')?closeCoach():openCoach();};
   if(clo)clo.onclick=closeCoach;
   if(newBtn)newBtn.onclick=newChat;
   if(expBtn)expBtn.onclick=async()=>{let r='';try{r=await capi().open_coach_window();}catch(e){r='';}
     if(r==='no-window'||r===''||(typeof r==='string'&&r.indexOf('err')===0)){body.classList.toggle('coach-expand');setTimeout(scroll,30);}};
   // ---- Coach settings (offline / API) ----
   const sub=document.getElementById('coachsub'), cfgBtn=document.getElementById('coachcfg'),
     provSel=document.getElementById('cprovider'), modelSel=document.getElementById('cmodelsel'),
     modelRow=document.getElementById('ccfgmodelrow'), modelIn=document.getElementById('cmodel'),
     baseRow=document.getElementById('ccfgbaserow'), baseIn=document.getElementById('cbase'),
     keyIn=document.getElementById('ckey'), cloud=document.getElementById('ccfgcloud');
   const PROV={
     anthropic:{base:'',models:[['claude-haiku-4-5-20251001','Haiku 4.5 — cheapest'],['claude-sonnet-5','Sonnet 5 — smartest']]},
     openai:{base:'',models:[['gpt-5.4-mini','GPT-5.4-mini — cheap'],['gpt-5.4','GPT-5.4'],['gpt-4o-mini','GPT-4o-mini — safe fallback']]},
     gemini:{base:'https://generativelanguage.googleapis.com/v1beta/openai',models:[['gemini-2.5-flash-lite','2.5 Flash-Lite — cheapest'],['gemini-2.5-flash','2.5 Flash']]},
     deepseek:{base:'https://api.deepseek.com/v1',models:[['deepseek-chat','deepseek-chat']]},
     custom:{base:'',models:[]}};
   function setSub(mode){if(sub)sub.textContent=(mode==='api')?'AI API mode':'offline tuning assistant';}
   function fillModels(prov,sel){
     if(!modelSel)return; modelSel.innerHTML='';
     ((PROV[prov]||{}).models||[]).forEach(m=>{const o=document.createElement('option');o.value=m[0];o.textContent=m[1];modelSel.appendChild(o);});
     const oth=document.createElement('option');oth.value='__other__';oth.textContent='Other model id…';modelSel.appendChild(oth);
     const has=((PROV[prov]||{}).models||[]).some(m=>m[0]===sel);
     if(sel&&has)modelSel.value=sel;
     else if(sel&&prov!=='custom'){modelSel.value='__other__';if(modelIn)modelIn.value=sel;}}
   function applyProvUI(){const p=provSel.value;
     if(cloud)cloud.style.display=(p==='offline')?'none':'flex';
     if(baseRow)baseRow.style.display=(p==='custom')?'flex':'none';
     if(modelRow)modelRow.style.display=(p!=='offline'&&(modelSel.value==='__other__'||p==='custom'))?'flex':'none';}
   if(provSel)provSel.onchange=()=>{fillModels(provSel.value,'');applyProvUI();};
   if(modelSel)modelSel.onchange=applyProvUI;
   async function loadCfg(){let c={};try{c=await capi().coach_settings();}catch(e){c={mode:'offline',model:'',base:''};}
     let prov='offline';
     if(c.mode==='api'){const b=(c.base||'');
       if(/deepseek/.test(b))prov='deepseek'; else if(/googleapis/.test(b))prov='gemini';
       else if(b)prov='custom'; else if(((c.model||'').toLowerCase()).indexOf('claude')===0)prov='anthropic'; else prov='openai';}
     if(provSel)provSel.value=prov;
     fillModels(prov,c.model||'');
     if(baseIn)baseIn.value=c.base||'';
     if(prov==='custom'&&modelIn)modelIn.value=c.model||'';
     if(keyIn)keyIn.placeholder=c.has_key?'•••••• key saved — type to replace':'paste key — stays on this PC';
     applyProvUI(); setSub(c.mode);}
   if(cfgBtn)cfgBtn.onclick=()=>{const on=body.classList.toggle('coach-cfg-on');if(on)loadCfg();};
   const saveBtn=document.getElementById('ccfgsave'), clrBtn=document.getElementById('ccfgclear');
   if(saveBtn)saveBtn.onclick=async()=>{
     const p=provSel.value, mode=(p==='offline')?'offline':'api';
     let model='', base='';
     if(mode==='api'){model=(modelSel.value==='__other__'||p==='custom')?(modelIn.value.trim()):modelSel.value;
       base=(p==='custom')?baseIn.value.trim():((PROV[p]||{}).base||'');}
     const key=(keyIn&&keyIn.value.trim())?keyIn.value.trim():null;
     try{await capi().save_coach_settings(mode,key,model||null,base);}catch(e){}
     if(keyIn)keyIn.value='';
     setSub(mode);body.classList.remove('coach-cfg-on');
     if(window.toast)toast(mode==='api'?('Coach: '+p+(model?' · '+model:'')):'Coach: offline mode');};
   if(clrBtn)clrBtn.onclick=async()=>{try{await capi().save_coach_settings('offline','__CLEAR__');}catch(e){}
     if(keyIn)keyIn.value='';loadCfg();if(window.toast)toast('API key cleared — back to offline');};
   // open by default (don't steal focus from the access-code gate)
   body.classList.add('coach-on');tgl.classList.add('on');
   function boot(){greet();loadCfg();}
   if(window.pywebview&&window.pywebview.api)boot();
   else window.addEventListener('pywebviewready',boot);
 })();
</script></body></html>"""


ANALYTICS_HTML = r'''<!doctype html><html><head><meta charset="utf-8"><style>
 :root{--bg:#1a1816;--bg2:#1f1d1a;--panel:#232120;--line:#332f2a;--line2:#423d35;
   --txt:#ece4d6;--mut:#9c9183;--dim:#6a6253;--accent:#c2924c;--accent-lit:#e0b873;--accent2:#7d9b63;}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--txt);font:13px -apple-system,"Segoe UI",sans-serif}
 .ahead{display:flex;align-items:center;gap:10px;padding:13px 18px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:2}
 .ahead .t{font-size:15px;font-weight:700}.ahead .t b{color:var(--accent-lit)}
 .ahead .rs{margin-left:auto;color:var(--mut);font-size:12px}
 .awrap{padding:6px 18px 30px}

 .asec{color:var(--accent-lit);font-weight:700;font-size:11px;letter-spacing:.05em;text-transform:uppercase;margin:16px 2px 8px}
 .agrid{display:grid;grid-template-columns:repeat(4,1fr);gap:9px}
 .acard{background:var(--panel);border:1px solid var(--line2);border-radius:11px;padding:11px 13px}
 .acard .al{color:var(--dim);font-size:10px;text-transform:uppercase;letter-spacing:.05em;font-weight:700}
 .acard .av{font-size:21px;font-weight:800;color:var(--txt);margin-top:3px;font-variant:tabular-nums}
 .acard .as{color:var(--mut);font-size:11px;margin-top:3px}
 .arow{display:flex;flex-wrap:wrap;gap:7px;align-items:center;margin:0 2px 6px}
 .arow .albl{color:var(--dim);font-size:10px;text-transform:uppercase;letter-spacing:.05em;font-weight:700;width:66px}
 .achip{background:var(--panel);border:1px solid var(--line2);border-radius:8px;padding:4px 9px;font-size:12px;color:var(--mut);font-weight:600}
 .achip b{color:var(--accent-lit)}
 .adim{color:var(--dim);font-size:12px}
 .atbl{width:100%;border-collapse:collapse;margin-top:4px}
 .atbl th{color:var(--dim);text-transform:uppercase;font-size:9px;letter-spacing:.05em;text-align:left;padding:5px 8px;border-bottom:1px solid var(--line)}
 .atbl td{font-size:12px;padding:5px 8px;border-bottom:1px solid rgba(51,47,42,.5);font-variant:tabular-nums;color:var(--txt)}
 </style></head><body>
 <div class="ahead"><div class="t">Prospectors <b>Analytics</b></div><span class="rs" id="ars">idle</span></div>
 <div class="awrap"><div id="aroot"></div></div>
 <script>
 window.fmtBig=window.fmtBig||function(n){n=Number(n)||0;const a=Math.abs(n);
   if(a>=1e15)return (n/1e15).toFixed(2)+'Q';if(a>=1e12)return (n/1e12).toFixed(2)+'T';
   if(a>=1e9)return (n/1e9).toFixed(2)+'B';if(a>=1e6)return (n/1e6).toFixed(2)+'M';
   if(a>=1e3)return (n/1e3).toFixed(1)+'K';return String(Math.round(n));};

window._analyticsFinds=window._analyticsFinds||[];
window.renderAnalytics=function(root,s,finds){
  if(!root)return;
  const B=window.fmtBig||(x=>String(x));
  const fmtS=v=>{v=Math.max(0,Math.round(v||0));return Math.floor(v/60)+':'+String(v%60).padStart(2,'0');};
  s=s||{};finds=finds||[];
  const card=(l,v,sub)=>'<div class="acard"><div class="al">'+l+'</div><div class="av">'+v+'</div>'+(sub?'<div class="as">'+sub+'</div>':'')+'</div>';
  let h='';
  h+='<div class="asec">Throughput</div><div class="agrid">';
  h+=card('Pans',s.cycles||0,(s.clean_pct!=null?s.clean_pct+'% clean':''));
  h+=card('Pans / hr',s.pans_per_hr||0,'runtime '+fmtS(s.runtime_s));
  h+=card('Cycle time',(s.cyc_mean_s||0)+'s','p50 '+(s.cyc_p50_s||0)+' Â· p95 '+(s.cyc_p95_s||0)+' Â· last '+(s.cyc_last_s||0));
  h+=card('Digs (registered)',s.digs||0,(s.digs_per_pan||0)+'/pan Â· '+(s.dig_clicks||0)+' clicks');
  h+='</div>';
  h+='<div class="asec">Earnings</div><div class="agrid">';
  h+=card('Money','$'+B(s.money_earned||0),'$'+B(s.money_per_hr||0)+'/hr Â· $'+B(s.money_per_pan||0)+'/pan');
  h+=card('Shards',B(s.shards_earned||0),B(s.shards_per_hr||0)+'/hr Â· '+(s.shards_per_pan||0)+'/pan');
  h+=card('Loot value (kept)','$'+B(s.loot_value||0),'$'+B(s.loot_per_hr||0)+'/hr est');
  h+=card('TOTAL / hr','$'+B(s.total_per_hr||0),'money + kept loot');
  h+='</div>';
  h+='<div class="asec">Finds â '+(s.finds_count||0)+' items Â· '+B(s.find_kg||0)+' kg Â· best '+B(s.best_kg||0)+' kg'+(s.finds_stack?' · stack '+s.finds_stack:'')+(s.finds_lowconf?' · '+s.finds_lowconf+' low-conf':'')+'</div>';
  const rar=s.by_rarity||{},mod=s.by_mod||{};
  const chips=o=>Object.keys(o).length?Object.entries(o).sort((a,b)=>b[1]-a[1]).map(e=>'<span class="achip">'+e[0]+' <b>'+e[1]+'</b></span>').join(''):'<span class="adim">none yet</span>';
  h+='<div class="arow"><span class="albl">rarity</span>'+chips(rar)+'</div>';
  h+='<div class="arow"><span class="albl">modifier</span>'+chips(mod)+'</div>';
  h+='<div class="asec">Reliability</div><div class="agrid">';
  h+=card('Clean cycles',(s.clean_pct!=null?s.clean_pct+'%':'â'),(s.clean_cycles||0)+' of '+(s.cycles||0));
  h+=card('Recoveries',s.recoveries||0,(s.nudges||0)+' nudges · '+(s.shake_misses||0)+' shake misses');
  h+=card('Shake retries',s.shake_retries||0,(s.shake_fails||0)+' fails');
  h+=card('Stops / pauses',(s.safe_stops||0)+' / '+(s.hard_stops||0),(s.pauses||0)+' pauses Â· '+(s.relics_used||0)+' relics');
  h+='</div>';
  h+='<div class="asec">Find log ('+finds.length+')</div>';
  if(finds.length){h+='<table class="atbl"><tr><th>t</th><th>item</th><th>kg</th><th>rarity</th><th>~value</th></tr>';
    finds.slice(-120).reverse().forEach(f=>{h+='<tr><td>'+fmtS(f.t)+'</td><td>'+((f.mod?f.mod+' ':'')+f.name)+(f.conf!=null&&f.conf<0.3?' <span class="adim">?</span>':'')+(f.mmul&&f.mmul>1?' <span class="adim">x'+f.mmul+'</span>':'')+'</td><td>'+B(f.kg)+'</td><td>'+(f.rarity||'')+'</td><td>'+(f.value?'$'+B(f.value):'')+'</td></tr>';});
    h+='</table>';}
  else h+='<div class="adim" style="padding:4px 2px">No finds logged yet. Enable Finds tracking + calibrate the pop-up corners.</div>';
  root.innerHTML=h;
};

 async function tick(){let d={};try{d=await window.pywebview.api.analytics_data();}catch(e){}
   const s=d.stats||{},f=d.finds||[];
   document.getElementById('ars').textContent=d.running?'running':(d.alive?'idle':'engine off');
   renderAnalytics(document.getElementById('aroot'),s,f);
   setTimeout(tick,1500);}
 window.addEventListener('pywebviewready',tick);setTimeout(tick,600);
 window.__reload=()=>tick();
 </script></body></html>'''


COACH_HTML = r'''<!doctype html><html><head><meta charset="utf-8"><style>
 :root{--bg2:#1f1d1a;--panel:#232120;--line:#332f2a;--line2:#423d35;--txt:#ece4d6;--mut:#9c9183;--dim:#6a6253;--accent:#c2924c;--accent-lit:#e0b873;--accent2:#7d9b63;--teal-lit:#9bc07e}
 *{box-sizing:border-box}
 html,body{margin:0;height:100%;background:var(--bg2);color:var(--txt);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;-webkit-user-select:none;user-select:none}
 .wrap{display:flex;flex-direction:column;height:100%}
 .head{flex:0 0 auto;display:flex;align-items:center;gap:10px;padding:13px 12px 12px 18px;border-bottom:1px solid var(--line)}
 .mark{color:var(--accent-lit);font-size:17px}
 .ttl{font-weight:700;font-size:15px;flex:1}.ttl span{display:block;font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.07em;font-weight:600;margin-top:1px}
 .hb{display:flex;gap:3px}
 .hb button{background:transparent;color:var(--mut);border:0;border-radius:8px;width:32px;height:32px;cursor:pointer;font-size:15px}
 .hb button:hover{background:#2a2418;color:var(--txt)}
 .cfg{display:none;flex-direction:column;gap:11px;padding:14px 18px;border-bottom:1px solid var(--line);background:#1c1b18;max-width:840px;margin:0 auto;width:100%}
 body.cfgon .cfg{display:flex}
 .fld{display:flex;flex-direction:column;gap:5px}.lab{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);font-weight:700}
 .cfg select,.cfg input{background:var(--bg2);border:1px solid var(--line2);border-radius:9px;color:var(--txt);padding:9px 11px;font:inherit;font-size:13px;width:100%;-webkit-appearance:none;appearance:none}
 .cfg select:focus,.cfg input:focus{outline:0;border-color:var(--accent)}
 .act{display:flex;gap:8px}.sv{flex:1;background:var(--accent2);color:#14260f;border:0;border-radius:9px;padding:9px;font-weight:700;cursor:pointer}.clr{background:#2a2418;color:#cdbfa5;border:0;border-radius:9px;padding:9px 12px;font-weight:700;cursor:pointer}
 .note{font-size:11px;color:var(--dim);line-height:1.5}
 .msgs{flex:1;overflow-y:auto;padding:22px 18px;display:flex;flex-direction:column;gap:13px}
 .msgs>*{max-width:760px;width:100%;margin:0 auto}
 .m{font-size:14px;line-height:1.55;word-wrap:break-word;overflow-wrap:anywhere}
 .m.user{align-self:flex-end;background:var(--accent);color:#241a02;padding:10px 14px;border-radius:15px 15px 4px 15px;max-width:80%;font-weight:500;margin-right:0}
 .m.bot{align-self:flex-start;background:var(--panel);border:1px solid var(--line);padding:12px 15px;border-radius:15px 15px 15px 4px;max-width:84%;margin-left:0}
 .m.bot b{color:var(--accent-lit)}.m.bot i{color:var(--mut);font-style:normal}.m.bot code{background:#15140f;padding:1px 5px;border-radius:4px;font-size:12.5px;font-family:ui-monospace,Menlo,monospace}
 .m.typing{color:var(--dim);font-style:italic}
 .diff{margin-top:12px;border:1px solid var(--line2);border-radius:12px;overflow:hidden;background:#1b1a17}
 .dh{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--accent-lit);font-weight:700;padding:10px 13px 2px}
 .dr{padding:5px 13px}.dr+.dr{border-top:1px solid var(--line)}
 .dt{display:flex;align-items:baseline;gap:8px;font-size:13px}.dk{flex:1}.dv{font-variant-numeric:tabular-nums;font-weight:700;white-space:nowrap}.dv s{color:var(--dim);text-decoration:none;margin-right:6px}.dv b{color:var(--teal-lit)}
 .dw{font-size:11.5px;color:var(--mut);margin-top:2px}
 .da{display:flex;gap:8px;padding:11px 13px;background:#161512}.da button{flex:1;border:0;border-radius:8px;padding:10px;font-weight:700;cursor:pointer;font-size:13px}
 .ap{background:var(--accent2);color:#14260f}.dm{background:#2a2418;color:#cdbfa5}
 .diff.done{opacity:.7}.diff.done .da{display:none}
 .ds{font-size:11.5px;font-weight:700;padding:10px 13px;display:none}.diff.applied .ds.ok{display:block;color:var(--accent2)}.diff.skipped .ds.no{display:block;color:var(--dim)}
 .stats{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px}.stats label{display:flex;flex-direction:column;font-size:10px;color:var(--mut);gap:3px;text-transform:uppercase}.stats input{background:var(--bg2);border:1px solid var(--line2);border-radius:7px;color:var(--txt);padding:7px 8px;font:inherit;font-size:13px}.stats .go{grid-column:1/3;background:var(--accent2);color:#14260f;border:0;border-radius:8px;padding:9px;font-weight:700;cursor:pointer}
 .chips{flex:0 0 auto;display:flex;flex-wrap:wrap;gap:7px;padding:0 18px 12px;max-width:760px;margin:0 auto;width:100%}
 .chip{background:#2a2418;color:#d8cdb3;border:1px solid var(--line2);border-radius:15px;padding:6px 12px;font-size:12px;cursor:pointer}.chip:hover{background:#352d1c;color:#fff}
 .inp{flex:0 0 auto;display:flex;gap:9px;padding:14px 18px;border-top:1px solid var(--line);max-width:840px;margin:0 auto;width:100%}
 .inp textarea{flex:1;resize:none;background:var(--panel);border:1px solid var(--line2);border-radius:12px;color:var(--txt);padding:11px 13px;font:inherit;font-size:14px;max-height:160px;line-height:1.4}
 .inp textarea:focus{outline:0;border-color:var(--accent)}
 .inp button{background:var(--accent);color:#241a02;border:0;border-radius:12px;padding:0 18px;font-weight:700;cursor:pointer}.inp button:disabled{opacity:.5}
 #toast{position:fixed;bottom:84px;left:50%;transform:translateX(-50%);background:#14130f;border:1px solid var(--line2);color:var(--txt);padding:9px 15px;border-radius:10px;font-size:13px;opacity:0;transition:opacity .2s;pointer-events:none}#toast.show{opacity:1}
</style></head><body>
 <div class="wrap">
   <div class="head"><span class="mark">&#10022;</span>
     <div class="ttl">Coach<span id="sub">offline tuning assistant</span></div>
     <div class="hb"><button id="new" title="New chat">&#8853;</button><button id="cfgb" title="Settings">&#9881;</button><button id="cls" title="Close">&#10005;</button></div></div>
   <div class="cfg" id="cfg">
     <div class="fld"><span class="lab">Engine</span><select id="prov">
       <option value="offline">Offline brain — free, instant</option><option value="anthropic">Claude (Anthropic)</option><option value="openai">OpenAI (GPT)</option><option value="gemini">Google Gemini</option><option value="deepseek">DeepSeek</option><option value="custom">Custom / local</option></select></div>
     <div id="cloud"><div class="fld"><span class="lab">Model</span><select id="msel"></select></div>
       <div class="fld" id="mrow" style="display:none"><span class="lab">Model id</span><input id="mid" type="text" placeholder="exact model id"></div>
       <div class="fld" id="brow" style="display:none"><span class="lab">Base URL</span><input id="base" type="text" placeholder="https://…/v1"></div>
       <div class="fld"><span class="lab">API key</span><input id="key" type="password" placeholder="paste key — stays on this PC"></div></div>
     <div class="act"><button class="sv" id="save">Save</button><button class="clr" id="clr">Clear key</button></div>
     <div class="note">Offline is free. Cloud engines use <b>your own key</b> (stays on this PC) and cost a fraction of a cent per message.</div>
   </div>
   <div class="msgs" id="msgs"></div>
   <div class="chips" id="chips"></div>
   <div class="inp"><textarea id="in" rows="1" placeholder="Describe a problem… e.g. it shakes late"></textarea><button id="send">Send</button></div>
 </div>
 <div id="toast"></div>
<script>
 const api=()=>window.pywebview&&window.pywebview.api;
 const $=s=>document.getElementById(s);
 const msgs=$('msgs'),chips=$('chips'),inp=$('in'),sendBtn=$('send'),sub=$('sub');
 let prevTopic='',hist=[],sending=false,greeted=false;
 const esc=s=>(s==null?'':String(s)).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
 function md(s){s=esc(s);s=s.replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/`([^`]+?)`/g,'<code>$1</code>').replace(/(^|[^*])\*(?!\*)([^*\n]+?)\*(?!\*)/g,'$1<i>$2</i>').replace(/^\s*[-•]\s+/gm,'• ');return s.replace(/\n/g,'<br>');}
 const scroll=()=>{msgs.scrollTop=msgs.scrollHeight;};
 const fmtVal=v=>(typeof v==='boolean')?(v?'on':'off'):v;
 function toast(t){const e=$('toast');e.textContent=t;e.classList.add('show');clearTimeout(window._t);window._t=setTimeout(()=>e.classList.remove('show'),1800);}
 function persist(){try{api().save_coach_history(hist);}catch(e){}}
 function addUser(t){const d=document.createElement('div');d.className='m user';d.textContent=t;msgs.appendChild(d);scroll();}
 function diffCard(ch,live){const w=document.createElement('div');w.className='diff'+(live?'':' done');
   const rows=ch.map(c=>`<div class="dr"><div class="dt"><span class="dk">${esc(c.label||c.key)}</span><span class="dv"><s>${esc(fmtVal(c.from))}</s><b>${esc(fmtVal(c.to))}</b></span></div>${c.reason?('<div class="dw">'+esc(c.reason)+'</div>'):''}</div>`).join('');
   w.innerHTML='<div class="dh">Suggested change'+(ch.length>1?'s':'')+'</div>'+rows+'<div class="ds ok">✓ Applied — press Save settings in the app to keep</div><div class="ds no">Not applied</div>'+(live?('<div class="da"><button class="ap">Implement '+ch.length+' change'+(ch.length>1?'s':'')+'</button><button class="dm">Don’t apply</button></div>'):'');
   if(live){w.querySelector('.ap').onclick=async()=>{let r={};try{r=await api().assistant_apply(ch);}catch(e){}w.classList.add('done','applied');toast('Applied '+((r&&r.applied)||ch.length)+' change(s)');};
     w.querySelector('.dm').onclick=()=>w.classList.add('done','skipped');}
   return w;}
 function statsForm(){const fields=[['capacity','Capacity'],['dig_strength','Dig strength'],['dig_speed','Dig speed %'],['shake_strength','Shake strength'],['shake_speed','Shake speed'],['luck','Luck'],['walk_speed','Walk speed']];
   const f=document.createElement('div');f.className='stats';f.innerHTML=fields.map(x=>`<label>${x[1]}<input type="number" data-st="${x[0]}" step="any"></label>`).join('')+'<button class="go">Analyze with these stats</button>';
   f.querySelector('.go').onclick=()=>{const st={};f.querySelectorAll('input[data-st]').forEach(i=>{const v=parseFloat(i.value);if(!isNaN(v))st[i.dataset.st]=v;});if(!Object.keys(st).length){toast('Enter at least capacity & dig strength');return;}const lbl='My stats: '+Object.entries(st).map(e=>e[0]+'='+e[1]).join(', ');addUser(lbl);hist.push({role:'user',text:lbl});persist();ask('analyze my stats and make it faster',st);};return f;}
 function addBot(res,live){const d=document.createElement('div');d.className='m bot';d.innerHTML=md(res.reply||'');if(res.changes&&res.changes.length)d.appendChild(diffCard(res.changes,live!==false));if(res.askStats&&live!==false)d.appendChild(statsForm());msgs.appendChild(d);scroll();if(live!==false)setChips(res.chips||[]);}
 function setChips(ch){chips.innerHTML='';(ch||[]).forEach(c=>{const b=document.createElement('button');b.className='chip';b.textContent=c;b.onclick=()=>{inp.value=c;doSend();};chips.appendChild(b);});}
 function buildConvo(){const out=[];hist.slice(-14).forEach(t=>{if(t.role==='user')out.push({role:'user',text:t.text});else if(t.role==='bot'&&t.reply)out.push({role:'assistant',text:t.reply});});return out;}
 async function ask(text,stats){sending=true;sendBtn.disabled=true;const ty=document.createElement('div');ty.className='m bot typing';ty.textContent='Coach is thinking…';msgs.appendChild(ty);scroll();
   let res;try{res=await api().assistant_chat(text,prevTopic,stats||null,buildConvo());}catch(e){res={reply:'I could not reach the engine.',changes:[],chips:[]};}
   ty.remove();sending=false;sendBtn.disabled=false;prevTopic=(res&&res.topic)||'';addBot(res||{reply:'(no response)'},true);hist.push({role:'bot',reply:(res&&res.reply)||'',changes:(res&&res.changes)||[],chips:(res&&res.chips)||[]});persist();}
 function doSend(){const t=(inp.value||'').trim();if(!t||sending)return;addUser(t);hist.push({role:'user',text:t});persist();inp.value='';inp.style.height='auto';ask(t);}
 sendBtn.onclick=doSend;
 inp.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();doSend();}});
 inp.addEventListener('input',()=>{inp.style.height='auto';inp.style.height=Math.min(160,inp.scrollHeight)+'px';});
 async function loadHist(){let h=[];try{h=await api().coach_history();}catch(e){}msgs.innerHTML='';
   if(Array.isArray(h)&&h.length){hist=h;h.forEach(t=>{if(t.role==='user')addUser(t.text);else if(t.role==='bot')addBot({reply:t.reply,changes:t.changes,chips:t.chips},false);});const lb=[...h].reverse().find(t=>t.role==='bot');setChips((lb&&lb.chips)||[]);scroll();}
   else{hist=[];ask('hello');}}
 $('new').onclick=()=>{hist=[];msgs.innerHTML='';chips.innerHTML='';persist();ask('hello');inp.focus();};
 $('cls').onclick=()=>{try{api().close_coach_window();}catch(e){}};
 // ---- settings ----
 const provSel=$('prov'),msel=$('msel'),mrow=$('mrow'),mid=$('mid'),brow=$('brow'),base=$('base'),keyIn=$('key'),cloud=$('cloud');
 const PROV={anthropic:{base:'',models:[['claude-haiku-4-5-20251001','Haiku 4.5 — cheapest'],['claude-sonnet-5','Sonnet 5 — smartest']]},
   openai:{base:'',models:[['gpt-5.4-mini','GPT-5.4-mini — cheap'],['gpt-5.4','GPT-5.4'],['gpt-4o-mini','GPT-4o-mini — safe fallback']]},
   gemini:{base:'https://generativelanguage.googleapis.com/v1beta/openai',models:[['gemini-2.5-flash-lite','2.5 Flash-Lite — cheapest'],['gemini-2.5-flash','2.5 Flash']]},
   deepseek:{base:'https://api.deepseek.com/v1',models:[['deepseek-chat','deepseek-chat']]},custom:{base:'',models:[]}};
 function setSub(m){sub.textContent=(m==='api')?'AI API mode':'offline tuning assistant';}
 function fillModels(p,sel){msel.innerHTML='';((PROV[p]||{}).models||[]).forEach(m=>{const o=document.createElement('option');o.value=m[0];o.textContent=m[1];msel.appendChild(o);});const o=document.createElement('option');o.value='__other__';o.textContent='Other model id…';msel.appendChild(o);const has=((PROV[p]||{}).models||[]).some(m=>m[0]===sel);if(sel&&has)msel.value=sel;else if(sel&&p!=='custom'){msel.value='__other__';mid.value=sel;}}
 function applyUI(){const p=provSel.value;cloud.style.display=(p==='offline')?'none':'block';brow.style.display=(p==='custom')?'flex':'none';mrow.style.display=(p!=='offline'&&(msel.value==='__other__'||p==='custom'))?'flex':'none';}
 provSel.onchange=()=>{fillModels(provSel.value,'');applyUI();};msel.onchange=applyUI;
 async function loadCfg(){let c={};try{c=await api().coach_settings();}catch(e){c={mode:'offline',model:'',base:''};}
   let p='offline';if(c.mode==='api'){const b=(c.base||'');if(/deepseek/.test(b))p='deepseek';else if(/googleapis/.test(b))p='gemini';else if(b)p='custom';else if(((c.model||'').toLowerCase()).indexOf('claude')===0)p='anthropic';else p='openai';}
   provSel.value=p;fillModels(p,c.model||'');base.value=c.base||'';if(p==='custom')mid.value=c.model||'';keyIn.placeholder=c.has_key?'•••••• key saved — type to replace':'paste key — stays on this PC';applyUI();setSub(c.mode);}
 $('cfgb').onclick=()=>{const on=document.body.classList.toggle('cfgon');if(on)loadCfg();};
 $('save').onclick=async()=>{const p=provSel.value,mode=(p==='offline')?'offline':'api';let model='',b='';if(mode==='api'){model=(msel.value==='__other__'||p==='custom')?mid.value.trim():msel.value;b=(p==='custom')?base.value.trim():((PROV[p]||{}).base||'');}const key=(keyIn.value.trim())?keyIn.value.trim():null;try{await api().save_coach_settings(mode,key,model||null,b);}catch(e){}keyIn.value='';setSub(mode);document.body.classList.remove('cfgon');toast(mode==='api'?('Coach: '+p):'Coach: offline');};
 $('clr').onclick=async()=>{try{await api().save_coach_settings('offline','__CLEAR__');}catch(e){}keyIn.value='';loadCfg();toast('API key cleared');};
 function boot(){loadHist();loadCfg();setTimeout(()=>inp.focus(),80);}
 window.__reload=function(){boot();};
 window.addEventListener('pywebviewready',boot);
 if(window.pywebview&&window.pywebview.api)boot();
</script></body></html>'''


def _quit_everything(api):
    """Fully terminate the app AND the engine subprocess. The app owns several
    hidden helper windows (pill/HUD/overlay/coach/analytics); on macOS the
    Cocoa app will NOT quit while any window still exists, so closing just the
    main window used to leave the process running windowless -- macOS then
    refuses to relaunch it, and the engine kept panning. Wiring this to the
    main window's close (and to the post-start path) guarantees a clean exit."""
    try:
        if api is not None and getattr(api, "proc", None) is not None:
            try:
                api._save_history()          # log the run that was cut short
            except Exception:
                pass
            p = api.proc
            api.proc = None
            for step in (lambda: p.send_signal(signal.SIGINT),   # engine's clean stop
                         lambda: p.terminate()):                 # cross-platform kill
                try:
                    step()
                except Exception:
                    pass
    except Exception:
        pass
    os._exit(0)                              # drop any lingering threads/windows


def main():
    global _window, _pill, _overlay, _coach_win, _analytics_win, _hud
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
    print("[boot] building main window…", flush=True)
    _window = webview.create_window("Prospectors Plus", html=build_html(),
                                    js_api=api, width=1340, height=900,
                                    min_size=(980, 680))
    try:
        _window.events.closed += lambda: _quit_everything(api)
    except Exception:
        pass
    global _pill, _overlay
    try:
        import ctypes as _ct
        _sw = int(_ct.windll.user32.GetSystemMetrics(0))
        _sh = int(_ct.windll.user32.GetSystemMetrics(1))
    except Exception:
        _sw, _sh = 1920, 1080
    print("[boot] main ok -> pill", flush=True)
    try:
        _pill = webview.create_window(
            "Prospectors Plus", html=PILL_HTML, js_api=api,
            width=272, height=178, frameless=True, on_top=True,
            easy_drag=True, resizable=False, hidden=True)
    except Exception as _e:
        print("[pill] precreate failed: %s" % _e)
    print("[boot] pill ok -> hud (PP_NO_HUD=1 skips it)", flush=True)
    try:
        if os.environ.get("PP_NO_HUD"):
            raise RuntimeError("skipped by PP_NO_HUD")
        _hud = webview.create_window(
            "HUD — Prospectors Plus", html=_hud_html(), js_api=api,
            width=384, height=470, frameless=True, on_top=True,
            easy_drag=True, resizable=False, hidden=True)
    except Exception as _e:
        print("[hud] precreate failed: %s" % _e)
    print("[boot] hud ok -> calibrate overlay", flush=True)
    try:
        _overlay = webview.create_window(
            "Calibrate", html=_OVERLAY_HTML, js_api=api,
            x=0, y=0, width=_sw, height=_sh,
            frameless=True, on_top=True, hidden=True)
    except Exception as _e:
        print("[overlay] precreate failed: %s" % _e)
    print("[boot] overlay ok -> coach", flush=True)
    try:
        _coach_win = webview.create_window(
            "Coach — Prospectors Plus", html=COACH_HTML, js_api=api,
            width=900, height=820, min_size=(560, 560), hidden=True)
        _hide_on_close(_coach_win)
    except Exception as _e:
        print("[coach] precreate failed: %s" % _e)
    print("[boot] coach ok -> analytics", flush=True)
    try:
        _analytics_win = webview.create_window(
            "Analytics — Prospectors Plus", html=ANALYTICS_HTML, js_api=api,
            width=980, height=860, min_size=(620, 560), hidden=True)
        _hide_on_close(_analytics_win)
    except Exception as _e:
        print("[analytics] precreate failed: %s" % _e)
    # Ctrl+C / kill: a Python signal handler can NOT run while the native GUI
    # loop owns the main thread, so a custom handler here would never fire
    # (that is why Ctrl+C used to just hang). Use the OS default disposition
    # instead -> the process is terminated immediately at the C level. On a
    # terminal Ctrl+C the engine child shares the foreground process group and
    # receives the same SIGINT, so the macro stops cleanly too. The clean
    # in-app quit (history save + engine terminate) is handled by the main
    # window's close event -> _quit_everything.
    try:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
    except Exception:
        pass
    print("[boot] all windows created -> starting GUI loop", flush=True)
    webview.start()
    _quit_everything(api)        # normal close path -> kill engine + hard exit


if __name__ == "__main__":
    main()
