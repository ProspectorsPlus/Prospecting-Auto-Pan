#!/usr/bin/env python3
"""
prospecting_app.py -- Prospector Lite: native desktop app (its own window)
for the Prospecting macro, built on pywebview. Tabbed settings, Start/Stop +
live trace, a live calibrate readout, a relic timer editor, saved builds, and
? help on every field.

First time:   pip3 install pywebview --break-system-packages
Run:          python3 prospecting_app.py

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

try:
    import lite_trust
    import lite_onboarding
except ImportError:      # windows/ dev checkout: the modules live one level up
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.abspath(__file__)), os.pardir))
    import lite_trust
    import lite_onboarding
try:
    import lite_diagnostics
except Exception:        # diagnostics degrade to "no events", never a crash
    lite_diagnostics = None

# ---- identity ---------------------------------------------------------------
# One version source of truth for the app, the packages and the About panel.
# PROJECT_URL is the public source repository (source available for
# inspection; licence status in LICENSE_CHOICE_REQUIRED.md); while it is
# empty every "view source / releases" control hides itself and the app never
# invents a URL. Prospector Lite makes NO automatic network request: no
# update check, no analytics, no remote content fetch (see PRIVACY.md /
# NETWORK_BEHAVIOR).
APP_NAME    = "Prospector Lite"
VERSION     = "5.0.0"
PROJECT_URL = "https://github.com/ProspectorsPlus/Prospecting-Auto-Pan"

FROZEN = getattr(sys, "frozen", False)        # True when bundled by PyInstaller
HERE = (os.path.dirname(sys.executable) if FROZEN
        else os.path.dirname(os.path.abspath(__file__)))


def _legacy_data_dir():
    """Pre-1.0 ("Prospectors Plus") install location. Read-only source for the
    one-time migration below; never written to and never deleted."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Prospectors Plus")
    return os.path.join(os.path.expanduser("~"), "Library",
                        "Application Support", "Prospectors Plus")


# Config keys the old invite-gate / tracking builds wrote. Prospector Lite has
# no access code and no tracking: these are never read, never migrated, and
# are scrubbed from any config this app loads or copies.
_PRIVATE_LEGACY_KEYS = ("ACCESS_OK", "ACCESS_HASH", "ACCESS_MACHINE",
                        "MACHINE_SALT", "SYNC_URL")

_MIGRATE_SUMMARY = ""   # shown once on the welcome screen after an upgrade


def _migrate_legacy_data(d):
    """One-time import of user data from a "Prospectors Plus" install.
    Copy-only (the old directory is never modified), idempotent (a marker
    file records completion, and an already-populated new directory is left
    alone), and private fields are stripped: the old access-gate and tracking
    keys are dropped, never carried into Prospector Lite."""
    global _MIGRATE_SUMMARY
    marker = os.path.join(d, ".migrated_from_prospectors_plus")
    if os.path.exists(marker) or os.path.exists(
            os.path.join(d, "prospecting_config.json")):
        return
    old = _legacy_data_dir()
    if not os.path.isdir(old) or os.path.realpath(old) == os.path.realpath(d):
        return
    copied = []
    for name in ("prospecting_config.json", "prospecting_builds.json",
                 "prospecting_scripts.json", "run_history.json",
                 "tutorial_content.json"):
        src = os.path.join(old, name)
        if not os.path.isfile(src):
            continue
        try:
            if name == "prospecting_config.json":
                with open(src, encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    continue
                for k in _PRIVATE_LEGACY_KEYS:
                    data.pop(k, None)
                tmp = os.path.join(d, name + ".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp, os.path.join(d, name))
            else:
                shutil.copyfile(src, os.path.join(d, name + ".tmp"))
                os.replace(os.path.join(d, name + ".tmp"),
                           os.path.join(d, name))
            copied.append(name)
        except (OSError, ValueError):
            continue
    try:
        with open(marker, "w", encoding="utf-8") as f:
            f.write("from: %s\ncopied: %s\n"
                    % (old, ", ".join(copied) or "nothing"))
    except OSError:
        pass
    if copied:
        _MIGRATE_SUMMARY = ("Your Prospectors Plus data was imported (%s). "
                            "The old folder was not modified."
                            % ", ".join(copied))


def _data_dir():
    """Where read/write files (config, builds) live. Packaged builds use the
    platform-native per-user data directory; running from source keeps them
    next to the scripts (unchanged dev behaviour), EXCEPT the windows/
    mirror checkout, whose script directory doubles as the home of the
    tracked sanitized default config -- a source run there must never
    overwrite the file both installers ship."""
    d = os.environ.get("PP_DATA_DIR")
    if d:
        # Embedded launches (Prospector Studio bundles this app) choose the
        # data folder explicitly so config/builds/history live where the
        # host decided -- never inside the bundle.
        os.makedirs(d, exist_ok=True)
        return d
    if "--capabilities" in sys.argv:
        # Pure query mode: print the engine manifest and exit. It must not
        # create, migrate or seed the real user data directory -- and the
        # throwaway home removes itself when the process ends.
        import atexit
        import tempfile
        d = tempfile.mkdtemp(prefix="pplite_caps_")
        atexit.register(shutil.rmtree, d, ignore_errors=True)
        return d
    if FROZEN:
        if sys.platform == "darwin":
            d = os.path.join(os.path.expanduser("~"), "Library",
                             "Application Support", "Prospector Lite")
        elif os.name == "nt":
            base = (os.environ.get("APPDATA")
                    or os.environ.get("LOCALAPPDATA")
                    or os.path.expanduser("~"))
            d = os.path.join(base, "Prospector Lite")
        else:
            d = os.path.join(os.path.expanduser("~"), ".prospector-lite")
        os.makedirs(d, exist_ok=True)
        _migrate_legacy_data(d)
        return d
    d = os.path.dirname(os.path.abspath(__file__))
    if (os.path.basename(d).lower() == "windows"
            and os.path.isfile(os.path.join(os.path.dirname(d), "packaging",
                                            "sync_windows_app.py"))):
        # windows/ mirror inside the repo: live user data goes to a
        # gitignored subfolder so windows/prospecting_config.json (the
        # sanitized default every installer bundles) stays pristine.
        d = os.path.join(d, ".devdata")
    os.makedirs(d, exist_ok=True)
    return d


def _resource(name):
    """Path to a bundled read-only resource (works frozen via _MEIPASS)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


DATA_DIR = _data_dir()
# The engine module resolves its own home when imported (in-process for
# fingerprints, and in spawned children): pin it to the SAME folder now.
# Without this, a frozen import falls back to the per-user default dir and
# creates it at import time even when PP_DATA_DIR points elsewhere -- a
# data leak outside the documented single data folder. An explicitly
# spawner-provided PPENGINE_HOME (Prospector Studio host) still wins.
os.environ.setdefault("PPENGINE_HOME", DATA_DIR)
CONFIG_FILE = os.path.join(DATA_DIR, "prospecting_config.json")
BUILDS_FILE = os.path.join(DATA_DIR, "prospecting_builds.json")
TUTORIAL_FILE = os.path.join(DATA_DIR, "tutorial_content.json")        # owner edits
DIAG_FILE = os.path.join(DATA_DIR, "diagnostics_state.json")  # warnings store
SCRIPTS_FILE = os.path.join(DATA_DIR, "prospecting_scripts.json")   # Studio scripts
# Studio-launch cross-window state (both ignored outside PP_STUDIO_LAUNCH):
#   STATUS_FILE  -- written by THIS app (atomic, seq-numbered): mode, active
#                   build + revision, run state, and headline stats, so the
#                   Prospector Studio window can mirror the macro live.
#   PUSH_FILE    -- written by Prospector Studio at publish time (this app
#                   only reads it): the name + content revision of the build
#                   it pushed, so both windows can show the same revision.
STATUS_FILE = os.path.join(DATA_DIR, "studio_macro_status.json")
PUSH_FILE = os.path.join(DATA_DIR, "studio_push.json")

# ---- Prospector Studio embedded launch --------------------------------------
# Studio bundles this exact app and opens it as "the macro" window. The env
# contract (all optional; standalone Lite sets none of these and behaves
# byte-for-byte the same):
#   PP_DATA_DIR       where config/builds/scripts/history live
#   PP_THEME=studio   recolour every window to Studio's Assayer's Bench palette
#   PP_STUDIO_LAUNCH=1  show the Classic | Studio-build switch on the Run tab
#   PP_STUDIO_SCRIPT  the script name Studio published for this launch
STUDIO_LAUNCH = os.environ.get("PP_STUDIO_LAUNCH") == "1"
STUDIO_SCRIPT = os.environ.get("PP_STUDIO_SCRIPT", "")
APP_THEME = os.environ.get("PP_THEME", "")

_STUDIO_THEME_CSS = """<style id="pps-studio-theme">
:root{--bg:#0e1312;--bg2:#0b100f;--panel:#131917;--head:#0e1312;--line:#1e2624;
 --line2:#2a3431;--txt:#eae7dc;--mut:#8b948b;--dim:#6a746c;--accent:#d9a441;
 --accent-lit:#efc063;--accent2:#45c4a9;--teal-lit:#45c4a9;--green:#3fae94;
 --field:#0b100f;--nav:#0b100f;--sand-dim:rgba(217,164,65,.14);
 --sand-glow:rgba(217,164,65,.26)}
body,.cwrap,.topbar,.side,.content,.awrap{background-image:none !important}
</style>"""


def _themed(html):
    """Inject the Studio palette override when launched by Prospector Studio
    (PP_THEME=studio). Only the shared CSS variables are re-mapped; layout,
    markup, and behavior are untouched. Standalone Lite returns html as-is."""
    if APP_THEME != "studio" or "</head>" not in html:
        return html
    return html.replace("</head>", _STUDIO_THEME_CSS + "</head>", 1)

# Bundled default builds -- these SHIP with the app so every download has a
# ready geode setup on the Builds page (user builds of the same name override
# them; loading one seeds it into the personal builds file).
DEFAULT_BUILDS = {
    "Geode Farm": {
        "GEODE_MODE": True, "GEODE_DIGS_TO_FILL": 0, "GEODE_DIG_MS": 5,
        "GEODE_DELAY_MS": 12000, "GEODE_START_MS": 800, "GEODE_CONFIRM_FULL": True,
        "GEODE_GREEN_CONFIRM": True, "GEODE_START_TRIES": 3,
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
                  "per dig' to match your geode's fill animation (~12s). The "
                  "green dig-bar confirms each tap, so a tap that misses the "
                  "land nudges at once instead of burning that whole animation "
                  "-- calibrate the Green dig pixel. Ships with Prospectors "
                  "Plus.",
                  "created": 1752000000, "updated": 1752000000,
                  "used": 0, "builtin": True},
    },
    "Geode Farm 1-Tap": {
        "GEODE_MODE": True, "GEODE_DIGS_TO_FILL": 0, "GEODE_DIG_MS": 15,
        "GEODE_DELAY_MS": 12000, "GEODE_START_MS": 800,
        "GEODE_GREEN_CONFIRM": True, "GEODE_START_TRIES": 3,
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
                  "pan, then the slow momentum shake. The green dig-bar confirms "
                  "the tap, so a miss nudges at once instead of costing the whole "
                  "~12s animation. Tuned build; calibrate your own capacity + dig "
                  "pixels (the Green dig pixel included). Ships with Prospectors "
                  "Plus.",
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

# Prospector Engine ipc client (Phase 04 C3). Optional: with the ENGINE_IPC
# config flag off (the default) the app never touches it, and a missing
# package (an older frozen build) simply pins the flag off.
for _pr in (HERE, os.path.dirname(HERE)):
    if _pr not in sys.path:
        sys.path.append(_pr)
try:
    from prospector_engine.client import EngineClient as _EngineClient
except Exception:
    _EngineClient = None

# Calibration sensing (Phase 04 C8, protocol 4.15 / ISS-137): capture,
# pixel sampling, detection, OCR test reads and the semantic calibration
# writes all live in the ENGINE's sensing module now -- this host never
# reads the screen or derives calibration values itself. The app embeds
# the one implementation in-process (both ENGINE_IPC states -- the wire
# verbs serve external hosts and the contract/parity suites); a missing
# engine dependency degrades to per-call errors instead of killing the
# app (the legacy per-handler import-guard behavior).
_SENSING = None
_SENSING_ERR = None


def _sensing():
    global _SENSING, _SENSING_ERR
    if _SENSING is None and _SENSING_ERR is None:
        try:
            from prospector_engine import engine as _ppe_engine
            from prospector_engine import sensing as _ppe_sensing
            _SENSING = _ppe_sensing.Sensing(
                _ppe_engine, _ppe_sensing.FileStore(CONFIG_FILE))
        except (Exception, SystemExit) as e:
            _SENSING_ERR = "engine sensing unavailable: %s" % e
    if _SENSING is not None:
        _SENSING.store.path = CONFIG_FILE
    return _SENSING


_ONBOARD = None


def _onboarding():
    """The first-run wizard state machine (lite_onboarding.Onboarding),
    created lazily against the live DATA_DIR. Users who completed the old
    single welcome screen migrate straight to FINISHED -- their working
    install is never forced back through setup."""
    global _ONBOARD
    if _ONBOARD is None:
        _ONBOARD = lite_onboarding.Onboarding(
            DATA_DIR, lite_trust.platform_key(), version=VERSION)
        try:
            _ONBOARD.migrate_legacy(bool(load_saved().get("WELCOME_SEEN")))
        except Exception:
            pass
    return _ONBOARD

# First run when frozen: seed the writable config from the bundled sanitized
# default (sane settings, webhook empty + disabled -- see the tracked
# windows/prospecting_config.json and public_release_tests.scan_default_config).
if FROZEN and not os.path.exists(CONFIG_FILE):
    try:
        shutil.copyfile(_resource("prospecting_config.json"), CONFIG_FILE)
    except OSError:
        pass


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
    UI_HELP = getattr(_ui, "UI_HELP", {})
    PIXEL_FIELDS = getattr(_ui, "PIXEL_FIELDS", [])
    PIXEL_DEFAULTS = getattr(_ui, "PIXEL_DEFAULTS", {})
    REGION_FIELDS = getattr(_ui, "REGION_FIELDS", [])
    STUDIO_BLOCKS = getattr(_ui, "STUDIO_BLOCKS", {})
    STUDIO_GROUPS = getattr(_ui, "STUDIO_GROUPS", [])
    STUDIO_CONTAINERS = getattr(_ui, "STUDIO_CONTAINERS", set())
    STUDIO_KEY_WHITELIST = getattr(_ui, "STUDIO_KEY_WHITELIST", [])
    STUDIO_MAX_BLOCKS = getattr(_ui, "STUDIO_MAX_BLOCKS", 500)
    STUDIO_MAX_DEPTH = getattr(_ui, "STUDIO_MAX_DEPTH", 16)
    STUDIO_SCHEMA_VERSION = getattr(_ui, "STUDIO_SCHEMA_VERSION", 1)
    STUDIO_SCHEMA_V2 = getattr(_ui, "STUDIO_SCHEMA_V2", 2)
    STUDIO2_MAX_BLOCKS = getattr(_ui, "STUDIO2_MAX_BLOCKS", 2000)
    STUDIO2_MAX_DEPTH = getattr(_ui, "STUDIO2_MAX_DEPTH", 32)
    STUDIO2_MAX_VARS = getattr(_ui, "STUDIO2_MAX_VARS", 64)
    STUDIO2_MAX_HOOK_BLOCKS = getattr(_ui, "STUDIO2_MAX_HOOK_BLOCKS", 200)
    STUDIO2_STR_MAX = getattr(_ui, "STUDIO2_STR_MAX", 400)
    STUDIO2_TYPES = getattr(_ui, "STUDIO2_TYPES", set())
    STUDIO2_CONTAINERS = getattr(_ui, "STUDIO2_CONTAINERS", set())
    STUDIO2_ELSE_TYPES = getattr(_ui, "STUDIO2_ELSE_TYPES", ())
    STUDIO2_HOOKS = getattr(_ui, "STUDIO2_HOOKS", ())
    STUDIO2_STOP_SAFE = getattr(_ui, "STUDIO2_STOP_SAFE", ())
    STUDIO2_READS = getattr(_ui, "STUDIO2_READS", ())
    STUDIO2_OPS = getattr(_ui, "STUDIO2_OPS", ())
    STUDIO_SCHEMA_V3 = getattr(_ui, "STUDIO_SCHEMA_V3", 3)
    STUDIO3_TYPES = getattr(_ui, "STUDIO3_TYPES", set())
    STUDIO3_CAPS = getattr(_ui, "STUDIO3_CAPS", ())
    STUDIO3_CAP_OF = getattr(_ui, "STUDIO3_CAP_OF", {})
    STUDIO3_CAP_LABEL = getattr(_ui, "STUDIO3_CAP_LABEL", {})
except Exception:
    import traceback
    traceback.print_exc()
    SECTIONS = []
    DEFAULTS = TYPES = {}
    PRESET_V1 = PRESET_V2 = PRESET_GEODE = {}
    SECTION_HINT = TAB_ICON = HELP = UI_HELP = {}
    PIXEL_FIELDS = []
    PIXEL_DEFAULTS = {}
    REGION_FIELDS = []
    STUDIO_BLOCKS = {}
    STUDIO_GROUPS = []
    STUDIO_CONTAINERS = set()
    STUDIO_KEY_WHITELIST = []
    STUDIO_MAX_BLOCKS = 500
    STUDIO_MAX_DEPTH = 16
    STUDIO_SCHEMA_VERSION = 1
    STUDIO_SCHEMA_V2 = 2
    STUDIO2_MAX_BLOCKS = 2000
    STUDIO2_MAX_DEPTH = 32
    STUDIO2_MAX_VARS = 64
    STUDIO2_MAX_HOOK_BLOCKS = 200
    STUDIO2_STR_MAX = 400
    STUDIO2_TYPES = set()
    STUDIO2_CONTAINERS = set()
    STUDIO2_ELSE_TYPES = ()
    STUDIO2_HOOKS = ()
    STUDIO2_STOP_SAFE = ()
    STUDIO2_READS = ()
    STUDIO2_OPS = ()
    STUDIO_SCHEMA_V3 = 3
    STUDIO3_TYPES = set()
    STUDIO3_CAPS = ()
    STUDIO3_CAP_OF = {}
    STUDIO3_CAP_LABEL = {}

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


# ---- diagnostics host store (chunk D2) --------------------------------------
# DIAG_FILE holds {suppressions, history (last 50 dismiss/suppress records),
# applied (last 20 apply records for undo)}. The pure rule engine lives in
# lite_diagnostics; this is only the host-side persistence + badge routing.

def _diag_store_load():
    d = _read_json(DIAG_FILE, {})
    if not isinstance(d, dict):
        d = {}
    if not isinstance(d.get("suppressions"), dict):
        d["suppressions"] = {}
    if not isinstance(d.get("history"), list):
        d["history"] = []
    if not isinstance(d.get("applied"), list):
        d["applied"] = []
    return d


def _diag_store_save(d):
    """Atomic write (tmp + fsync + os.replace), bounded lists. Never
    raises."""
    try:
        d["history"] = list(d.get("history") or [])[-50:]
        d["applied"] = list(d.get("applied") or [])[-20:]
        tmp = DIAG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, DIAG_FILE)
        return True
    except Exception:
        return False


def _diag_badge_tab(ev):
    """Which sidebar tab owns this event's badge. Permission events own the
    trust tab; engine-tuning events (cycle-page setting targets, or plain
    stats/telemetry findings) own the cycle tab; calibration events own the
    cal tab; launch conflicts point at their own tab. Mirrors the shipped
    targets (cal red, cycle yellow) and adds the trust badge the old system
    never had."""
    if not isinstance(ev, dict):
        return ""
    links = [l for l in (ev.get("deep_links") or []) if isinstance(l, dict)]
    kinds = set(l.get("kind") for l in links)
    if ev.get("source") == "permissions" or "permission" in kinds:
        return "trust"
    for l in links:
        if l.get("kind") == "setting" and l.get("control") == "cycle":
            return "cycle"
    if "calibration" in kinds or ev.get("source") == "calibration":
        return "cal"
    if "setting" in kinds or ev.get("source") in ("stats", "events"):
        return "cycle"
    for l in links:
        if l.get("kind") == "tab" and l.get("tab_target"):
            return l.get("tab_target")
    return ""


def _diag_summarize(events):
    """{red, yellow, top_red_title, top_yellow_title, tabs:{tab:{red,
    yellow, top_red_title, top_yellow_title, top_red_id, top_yellow_id}}}.
    Also annotates each event with its badge_tab. Events arrive sorted by
    severity desc, so the first hit per bucket is the top one."""
    red_all, yellow_all, tabs = [], [], {}
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        tab = _diag_badge_tab(ev)
        ev["badge_tab"] = tab
        sev = ev.get("severity")
        if sev in ("ERROR", "CRITICAL"):
            red_all.append(ev)
        elif sev in ("WARNING", "NOTICE"):
            yellow_all.append(ev)
        else:
            continue
        if not tab:
            continue
        b = tabs.setdefault(tab, {"red": 0, "yellow": 0,
                                  "top_red_title": "", "top_yellow_title": "",
                                  "top_red_id": "", "top_yellow_id": ""})
        if sev in ("ERROR", "CRITICAL"):
            b["red"] += 1
            if not b["top_red_id"]:
                b["top_red_id"] = ev.get("id", "")
                b["top_red_title"] = ev.get("title", "")
        else:
            b["yellow"] += 1
            if not b["top_yellow_id"]:
                b["top_yellow_id"] = ev.get("id", "")
                b["top_yellow_title"] = ev.get("title", "")
    return {"red": len(red_all), "yellow": len(yellow_all),
            "top_red_title": red_all[0].get("title", "") if red_all else "",
            "top_yellow_title": (yellow_all[0].get("title", "")
                                 if yellow_all else ""),
            "tabs": tabs}


def _diag_blocker_event(cal_status):
    """Host-synthesized PP-D-CAL-REQUIRED: required calibration items block
    Start (the same condition launch() enforces). This is what makes the
    cal badge honest right after 'Mark wizard complete' with missing
    requirements -- no run telemetry exists yet, so no D1 rule can fire."""
    if lite_diagnostics is None:
        return None
    try:
        ready, blockers = lite_onboarding.calibration_ready(cal_status or {})
    except Exception:
        return None
    if ready or not blockers:
        return None
    names = ", ".join(blockers)
    rec = lite_diagnostics.make_recommendation(
        "cal-required-finish",
        "Finish the required calibration",
        "Start stays disabled until every required calibration item is "
        "set; guided calibration walks each one in order.",
        calibration_targets=list(blockers),
        expected_effect="Start unblocks once the required items pass.",
        tradeoff="",
        verify="The Calibrate tab (or the wizard Readiness Check) reports "
               "the required items as set.",
        priority=1)
    return lite_diagnostics.make_event(
        "PP-D-CAL-REQUIRED", "ERROR", "calibration",
        "Required calibration is incomplete",
        "Start is blocked: required calibration item(s) need attention: "
        "%s." % names,
        "calibration_ready reports blocker(s): %s." % names,
        ["blockers: %s" % names],
        "high", [rec],
        ["A brand-new install simply has not calibrated yet -- run the "
         "guided calibration once."],
        "faq-advanced-cues", source="calibration")


def _tls_context():
    """A VERIFYING TLS context, always. Bundled Pythons sometimes ship with an
    empty default trust store; when that happens the certifi CA bundle (packaged
    with the app) is loaded instead. There is deliberately no unverified mode:
    a request whose certificate cannot be checked fails, it is never retried
    with verification off."""
    import ssl
    ctx = ssl.create_default_context()
    try:
        if ctx.cert_store_stats().get("x509_ca", 0) == 0:
            import certifi
            ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx


def load_saved():
    cfg = _read_json(CONFIG_FILE, {})
    if isinstance(cfg, dict):
        # Old invite-gate / tracking fields are dead: never surfaced, never
        # honoured, and _scrub_config_file() removes them from disk once.
        for _k in _PRIVATE_LEGACY_KEYS:
            cfg.pop(_k, None)
    return cfg


def _save_config_atomic(cfg):
    """THE host-side config writer: tmp + fsync + os.replace, so a crash
    mid-write can never truncate the shared settings/calibration file.
    Returns (ok, error). Every host path that rewrites CONFIG_FILE must go
    through here (the engine has its own atomic writer)."""
    try:
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_FILE)
        return True, ""
    except OSError as e:
        return False, str(e)


def _config_write(cfg):
    """Atomic CONFIG_FILE write that raises OSError on failure -- the
    drop-in replacement for the legacy open('w')+json.dump sites (which
    could truncate the whole config on a crash mid-write and relied on
    OSError propagating)."""
    ok, err = _save_config_atomic(cfg)
    if not ok:
        raise OSError(err)


# ---- Welcome-screen preference ----------------------------------------------
# One positive key: SHOW_WELCOME_EVERY_LAUNCH (True = show the welcome screen
# at every launch). Default for a brand-new user: True. The legacy inverse
# flag WELCOME_SEEN is migrated once (after the onboarding state machine has
# had its look at it) and then removed, so exactly one key exists.
_WELCOME_KEY = "SHOW_WELCOME_EVERY_LAUNCH"


def _welcome_pref():
    """The stored 'show welcome at every launch' preference, migrating the
    legacy inverse key on first read. Missing/corrupt settings -> True
    (the professional default: a new user sees the welcome screen)."""
    cur = load_saved()
    if _WELCOME_KEY in cur:
        return bool(cur[_WELCOME_KEY])
    # Ensure the onboarding legacy bridge (which reads WELCOME_SEEN) runs
    # against the pre-migration value BEFORE the key is removed.
    try:
        _onboarding()
    except Exception:
        pass
    cur = load_saved()
    val = (not bool(cur["WELCOME_SEEN"])) if "WELCOME_SEEN" in cur else True
    cur[_WELCOME_KEY] = bool(val)
    cur.pop("WELCOME_SEEN", None)
    _save_config_atomic(cur)
    return bool(val)


def _set_welcome_pref(flag):
    """Persist the checkbox immediately and atomically. Returns (ok, err);
    a failed write is REPORTED to the UI, never silently reverted."""
    cur = load_saved()
    cur[_WELCOME_KEY] = bool(flag)
    cur.pop("WELCOME_SEEN", None)
    return _save_config_atomic(cur)


# ---- Skip-wizard preference --------------------------------------------------
# SKIP_WIZARD_AUTOMATICALLY (default False): launches go straight to the main
# app; explicitly opening Welcome still shows the wizard, and readiness
# warnings stay live-computed. Stored in the main config exactly like
# SHOW_WELCOME_EVERY_LAUNCH. No legacy key to migrate.
_SKIPWIZ_KEY = "SKIP_WIZARD_AUTOMATICALLY"


def _skip_wizard_pref():
    """The stored 'skip the setup wizard automatically on launch'
    preference. Missing/corrupt settings -> False (a new user goes
    through the wizard)."""
    try:
        return bool(load_saved().get(_SKIPWIZ_KEY, False))
    except Exception:
        return False


def _set_skip_wizard_pref(flag):
    """Persist the auto-skip checkbox immediately and atomically.
    Returns (ok, err); a failed write is REPORTED, never hidden."""
    cur = load_saved()
    cur[_SKIPWIZ_KEY] = bool(flag)
    return _save_config_atomic(cur)


# ---- Tutorial auto-open preference -------------------------------------------
# TUTORIAL_AUTO_OPEN (default True): the main tutorial opens on every fresh
# main-app entry (launch, or wizard visit -> return). Stored in the main
# config exactly like SHOW_WELCOME_EVERY_LAUNCH. No legacy key to migrate.
_TUTAUTO_KEY = "TUTORIAL_AUTO_OPEN"


def _tutorial_auto_open_pref():
    """The stored 'open the tutorial whenever the app opens' preference.
    Missing/corrupt settings -> True (a new user gets the tutorial)."""
    try:
        return bool(load_saved().get(_TUTAUTO_KEY, True))
    except Exception:
        return True


def _set_tutorial_auto_open(flag):
    """Persist the auto-open checkbox immediately and atomically.
    Returns (ok, err); a failed write is REPORTED, never hidden."""
    cur = load_saved()
    cur[_TUTAUTO_KEY] = bool(flag)
    return _save_config_atomic(cur)


# ---- Main-tutorial state -----------------------------------------------------
# The MAIN tutorial (how to use the app) is distinct from the SETUP wizard
# (permissions + calibration). Its lifecycle lives in its own atomically
# written file inside the data dir -- NOT in WebKit localStorage, which is
# invisible to the Trust Center data manifest, fails closed, and does not
# survive a webview-profile change. Since schema 3 the lifecycle is HISTORY
# (last outcome + seen_count + last_seen_version): it no longer gates the
# auto-open, which happens on every fresh main-app entry unless the
# TUTORIAL_AUTO_OPEN preference turns it off.
_TUTORIAL_STATE_FILE = os.path.join(DATA_DIR, "tutorial_state.json")
# 3: adds seen_count / last_seen_version, lifecycle becomes history only
# 2: first Python-side schema (1 = the legacy localStorage pp_tour_done era)
TUTORIAL_SCHEMA = 3
_TUT_STATES = ("NOT_STARTED", "ACTIVE", "COMPLETED", "DISMISSED")


def _tutorial_lifecycle():
    d = _read_json(_TUTORIAL_STATE_FILE, {})
    if (isinstance(d, dict) and d.get("schema") == 2
            and d.get("main") in _TUT_STATES):
        # v2 -> v3 migrates in place: main/updated/migrated_from are kept
        # as history; the one viewing v2 could have recorded counts once.
        d["schema"] = 3
        d["seen_count"] = 1 if d.get("main") != "NOT_STARTED" else 0
        d["last_seen_version"] = ""
    if (not isinstance(d, dict) or d.get("schema") != TUTORIAL_SCHEMA
            or d.get("main") not in _TUT_STATES):
        d = {"schema": TUTORIAL_SCHEMA, "main": "NOT_STARTED",
             "updated": 0, "seen_count": 0, "last_seen_version": ""}
    d.setdefault("seen_count", 0)
    d.setdefault("last_seen_version", "")
    return d


def _tutorial_lifecycle_save(d):
    tmp = _TUTORIAL_STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, _TUTORIAL_STATE_FILE)
        return True, ""
    except OSError as e:
        return False, str(e)


# ---- onboarding / wizard diagnostics log ------------------------------------
# A small append-only log of every wizard-relevant operation so a user report
# ("the test did nothing") can be matched to what actually ran. Contains NO
# secrets, NO screenshots, NO keystroke content -- operation names, status
# strings and error codes only. Rotates at ~256 KB (one .1 kept).
_WIZLOG_FILE = os.path.join(DATA_DIR, "onboarding.log")
_WIZLOG_LOCK = threading.Lock()


def _wlog(op, cap="", status="", code="", detail=""):
    try:
        line = "%s | %s | %s | %s | %s | %s\n" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), op, cap or "-",
            status or "-", code or "-",
            str(detail or "").replace("\n", " ")[:300])
        with _WIZLOG_LOCK:
            try:
                if (os.path.isfile(_WIZLOG_FILE)
                        and os.path.getsize(_WIZLOG_FILE) > 262144):
                    os.replace(_WIZLOG_FILE, _WIZLOG_FILE + ".1")
            except OSError:
                pass
            with open(_WIZLOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


def _wizlog_tail(max_bytes=16384):
    try:
        with open(_WIZLOG_FILE, encoding="utf-8", errors="replace") as f:
            data = f.read()
        return data[-max_bytes:]
    except OSError:
        return ""


def _scrub_config_file():
    """One-time on-disk cleanup: rewrite the config without the legacy
    access-gate / tracking keys. Atomic, best-effort, run at startup."""
    raw = _read_json(CONFIG_FILE, {})
    if not isinstance(raw, dict):
        return
    if not any(k in raw for k in _PRIVATE_LEGACY_KEYS):
        return
    for k in _PRIVATE_LEGACY_KEYS:
        raw.pop(k, None)
    try:
        _config_write(raw)
    except OSError:
        pass


# --- Secrets: the personal Coach API key lives in its own file so it never
#     lands in git, a shipped build, or an exported config. Packaged builds
#     AND explicitly-homed launches (PP_DATA_DIR -- the Studio embedded
#     contract) keep it in the user DATA dir (the install dir may be
#     read-only and is removed on uninstall, and the privacy contract is
#     "one data folder"); plain dev keeps it next to the scripts, where it
#     is gitignored.
SECRETS_FILE = os.path.join(DATA_DIR if FROZEN or os.environ.get("PP_DATA_DIR")
                            else HERE, "prospecting_secrets.json")
_LEGACY_SECRETS_FILE = os.path.join(HERE, "prospecting_secrets.json")


def _load_secrets():
    sec = _read_json(SECRETS_FILE, {})
    if sec:
        return sec
    if FROZEN and _LEGACY_SECRETS_FILE != SECRETS_FILE:
        # read-only fallback: a key an older packaged build saved next to
        # the executable still works, but new saves go to the data dir
        return _read_json(_LEGACY_SECRETS_FILE, {}) or {}
    return {}


def _coach_base_ok(url):
    """A Coach base URL may be https://, or plain http:// ONLY to localhost
    (the documented local-Ollama case). Anything else would put the user's
    API key on the wire unencrypted."""
    u = str(url or "").strip().lower()
    if u.startswith("https://"):
        return True
    if u.startswith("http://"):
        host = u[len("http://"):].split("/", 1)[0].split(":", 1)[0]
        return host in ("localhost", "127.0.0.1", "[::1]", "::1")
    return False


def _coach_key():
    """The Coach API key from the restricted secrets file. A key that an
    old build left in the main config is migrated into the secrets file
    once (and scrubbed from the config) so the 'key lives only in the
    restricted file' contract really holds."""
    k = (_load_secrets().get("COACH_API_KEY") or "").strip()
    if k:
        return k
    legacy = (load_saved().get("COACH_API_KEY") or "").strip()
    if legacy:
        _save_coach_key(legacy)
        try:
            cur = load_saved()
            cur["COACH_API_KEY"] = ""
            _config_write(cur)
        except OSError:
            pass
    return legacy


def _save_coach_key(key):
    """Write the API key ONLY to the gitignored secrets file (never the
    config), owner-read/write only (0600)."""
    sec = _load_secrets()
    if key == "__CLEAR__":
        sec.pop("COACH_API_KEY", None)
    elif key:
        sec["COACH_API_KEY"] = key.strip()
    try:
        with open(SECRETS_FILE, "w") as f:
            json.dump(sec, f, indent=2)
        try:
            os.chmod(SECRETS_FILE, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def _is_owner():
    """Owner gate for the tutorial/help editor. True only on a machine whose
    gitignored secrets file carries "OWNER": true. Users never see edit
    controls, and nothing about this flag ever leaves the machine."""
    sec = _load_secrets()
    return bool(sec.get("OWNER") is True)


# ---- Tutorial + help content ------------------------------------------------
# Every tutorial card ships here as the default. The owner can edit any card
# (and any setting/button explanation) in-app; edits land in TUTORIAL_FILE as
# overrides keyed by the card id (or "help:<KEY>" / "ui:<id>" for hover help).
# Content is local-only: Prospector Lite never fetches remote tutorial text.
# Merge order: defaults < local owner edits.
TOUR_DEFAULTS = {
 "main": [
  {"id": "main.welcome", "tab": "run", "center": True,
   "title": "Welcome to Prospector Lite",
   "body": "This walks the whole macro: what each page does and how to fix the "
           "common problems. Use <b>Next</b> and <b>Back</b> (arrow keys work "
           "too); Esc closes it. Each big page also has its own short "
           "walkthrough that offers itself the first time you open the page, "
           "and you can replay anything from the <b>❓ Tutorial</b> menu "
           "up top."},
  {"id": "main.how", "tab": "run", "center": True,
   "title": "How the macro runs",
   "body": "<b>It plays the panning loop by reading your screen, the way you "
           "would.</b> Every tick it checks two things: the white prompt at "
           "the bottom (<b>Pan</b> in water, <b>Collect Deposit</b> on land, "
           "<b>Shake</b> mid-shake) tells it where it is, and the <b>capacity "
           "bar</b> tells it what is in the pan. One cycle: dig until full, "
           "hold S back into the water, hold W and start the shake near the "
           "edge, rapid clicks drain the pan while momentum slides you "
           "ashore, then a small probe dig proves you are on diggable land. "
           "If anything wedges, recovery takes over: nudges, then a "
           "break-out, then a safe pause and retry."},
  {"id": "main.nav", "sel": ".side",
   "title": "Everything lives in the sidebar",
   "body": "<b>Run</b>, <b>Cycle</b> for tuning, <b>Builds</b>, "
           "<b>Calibrate</b>, <b>Relics</b>, <b>History</b>, plus grouped "
           "sections for Modes, Tracking, Alerts and Setup. The search box "
           "up top jumps straight to any setting by name."},
  {"id": "main.cal", "tab": "cal", "sel": "#wizbtn",
   "title": "Recalibrate here whenever the window changes",
   "body": "Setup already walked you through calibration step by step; "
           "this tab is where you maintain it afterwards. It edits the "
           "SAME saved values as the setup wizard. Redo a spot whenever "
           "the window size, resolution or monitor changes; a red badge "
           "on Calibrate warns you when that is needed. <b>Advanced cue "
           "matching</b> (the three prompt masks) is required and lives "
           "at the bottom of this page; re-capture the masks after any "
           "resize. You can also re-run the full guided setup any time "
           "from the Tutorial menu."},
  {"id": "main.caltest", "tab": "cal", "sel": "#dettest",
   "title": "Prove it worked",
   "body": "Press <b>Test detection (live)</b> right after calibrating. It "
           "shows what the macro currently sees: where you are standing and "
           "how full the pan reads. If either reads wrong, farming will go "
           "wrong, so fix calibration before you start."},
  {"id": "main.howpanel", "tab": "run", "sel": "#runflow",
   "title": "Your cheat sheet stays here",
   "body": "Hover <b>how it runs</b> next to the Start controls any time and the "
           "full order of events opens in the Preview panel on the right, so you "
           "never have to remember this tour."},
  {"id": "main.start", "tab": "run", "sel": "#startbtn",
   "title": "Starting the macro",
   "body": "Press <b>Start macro</b>, then click into Roblox so the game has "
           "focus. It runs the whole loop on its own. <b>Ctrl+K</b> also "
           "starts and stops from inside the game, and <b>Esc</b> quits."},
  {"id": "main.stats", "tab": "run", "sel": ".statsbar",
   "title": "Live stats, and what the health counters mean",
   "body": "Pans, digs and pans/hr show speed. The rest show struggle: "
           "climbing <b>nudges</b> usually means you land short or in the "
           "water, and the real fix is starting the shake later (raise "
           "<b>Walk forward before shaking</b> on the Cycle page). <b>Shake "
           "misses</b> means the pan is not emptying, so look at Shake and "
           "drain. <b>Recoveries</b> climbing means it keeps getting wedged, "
           "so check calibration first."},
  {"id": "main.warnings", "sel": ".side",
   "title": "The warning system watches for you",
   "body": "When something looks off, a badge appears on the owning sidebar "
           "tab: <b>yellow</b> means tuning may be off, <b>red</b> means "
           "something needs fixing before or during a run (calibration, a "
           "permission, capacity). The number is how many warnings that tab "
           "has. <b>Click a badge or a banner</b> to open the warning "
           "details: what was observed, the most likely cause, and the "
           "exact settings to review with one-click deep links. <b>Apply "
           "suggested value</b> makes the recommended change for you and "
           "<b>Undo</b> reverts it. Every warning also links into the "
           "<b>FAQ and troubleshooting</b> browser, which you can open any "
           "time from the ❓ Tutorial menu."},
  {"id": "main.presets", "tab": "run", "sel": "#pv1",
   "title": "Quick presets",
   "body": "One click loads a tuned starting point. <b>v1</b> is a fast "
           "single-dig build, <b>v2</b> multi-dig, <b>v3</b> geode. Not "
           "sure? Try v1, or load a saved Build."},
  {"id": "main.modes", "tab": "Treasure chest", "center": True,
   "title": "Modes: Standard, Treasure, Shards, Geodes",
   "body": "Most builds run <b>Standard</b>, which is no mode toggled at "
           "all. <b>Treasure</b> farms the two Rubble Creek spots with no "
           "shaking. <b>Shards</b> digs with an exact click count for "
           "one-click fills. <b>Geodes</b> waits out the very slow geode "
           "fill animation, then shakes normally. Turn one on and its "
           "settings appear. Run only one mode at a time."},
  {"id": "main.cycle", "tab": "cycle", "sel": ".cycwrap",
   "title": "The Cycle page is your loop as a picture",
   "body": "Each node is a stage of one pan and the numbers are live. The "
           "timeline under it draws where the time goes; drag a slider and "
           "it redraws. Click any node or bar to jump to the setting behind "
           "it."},
  {"id": "main.safety", "tab": "cycle", "sel": "#cs_safety",
   "title": "Recovery and safety nets",
   "body": "These decide what happens when something goes wrong, so one "
           "hiccup does not ruin an overnight run. They are on by default "
           "and escalate gently: nudges, then a shake retry, then a "
           "break-out, then a <b>safe stop</b> that pauses and retries "
           "instead of quitting. You rarely touch these. <a class='tlink' "
           "data-tourlink='recovery'>Walk the ladder step by step</a>"},
  {"id": "main.tracking", "tab": "Earnings",
   "sel": "[data-key=\"EARN_TRACK\"]", "row": True,
   "title": "Tracking",
   "body": "Three separate things. <b>Earnings</b> reads the money and shard "
           "totals so Analytics can show $/hr. <b>Finds</b> logs every item "
           "you dig up and its rarity. <b>Tracker</b> sends no input at all "
           "and counts the game's own Auto Pan, so you can compare it "
           "against the macro on the same numbers. Earnings and finds "
           "reading currently work on macOS only."},
  {"id": "main.alerts", "tab": "Notifications",
   "sel": "[data-key=\"WEBHOOK_ENABLED\"]", "row": True,
   "title": "Get pinged instead of babysitting",
   "body": "Add your own Discord webhook on this page and turn on <b>Discord "
           "notifications</b>. The macro then posts to your channel when it "
           "starts, stops, pauses on trouble, or on a stats schedule, with a "
           "screenshot if you want. <b>Send test</b> proves it works before "
           "a long run. Until you set this up, nothing is ever sent."},
  {"id": "main.builds", "tab": "builds", "sel": ".bldbar",
   "title": "Builds save your whole setup",
   "body": "A build is a snapshot of every setting plus relics. Load one to "
           "apply everything at once. <b>Export</b> writes a build to a "
           "file you can send a friend, <b>Import</b> loads theirs, and you "
           "can attach an equipment doc so they know exactly what gear to "
           "make. Two geode builds come ready-made."},
  {"id": "main.coach", "tab": "run", "sel": "#coachtoggle",
   "title": "Coach turns problems into settings",
   "body": "Describe what you see, like “it shakes too early” or "
           "“it lands in the water”, and the Coach proposes exact "
           "changes you apply with one click. Use it whenever you are not "
           "sure which knob to turn."},
  {"id": "main.preview", "tab": "run", "sel": "#prevtoggle",
   "title": "The Preview panel",
   "body": "Hover a sidebar tab and the panel on the right shows that page "
           "before you open it. Hover any setting, button or stat and it "
           "explains what it is, what it is for, and the problem it fixes. "
           "This button hides or brings back the panel."},
  {"id": "main.windows", "tab": "run", "sel": "#analyticsbtn",
   "title": "Analytics, HUD and Pop out",
   "body": "<b>Analytics</b> opens the dashboard of your sessions. <b>HUD</b> "
           "is a small always-on-top card with the current stage and live "
           "stats to park next to the game. <b>Pop out</b> is a mini control "
           "pill. Hover each button to preview it."},
  {"id": "main.trust", "tab": "trust", "sel": "#tcbody",
   "title": "The Trust Center is your control room",
   "body": "Everything about permissions and your data lives here: live "
           "permission status with real tests (including the <b>Safe "
           "Stop</b> hotkey test), exactly what this build can touch, the "
           "local data folder with export and delete controls, and a "
           "re-run of the full setup wizard. Nothing leaves this computer "
           "unless you turn on a feature that says so."},
  {"id": "main.save", "tab": "run", "sel": "#savebtn",
   "title": "Save your settings",
   "body": "Changes apply live while the app is open. <b>Save settings</b> "
           "makes them stick for next launch. Calibration and builds save "
           "separately, so they carry over on their own."},
  {"id": "main.done", "tab": "run", "center": True,
   "title": "Ready to farm",
   "body": "Calibrate, pick a preset or build, press Start, then click into "
           "the game. If something looks off, check the health counters, "
           "hover the setting in question, or ask the Coach. Replay any "
           "walkthrough from the <b>❓ Tutorial</b> menu."},
 ],
 "calibrate": [
  {"id": "cal.why", "tab": "cal", "center": True,
   "title": "Why calibration comes first",
   "body": "The macro never touches the game's memory. It reads your screen, "
           "so it has to know exactly where the capacity bar and the prompt "
           "words sit on YOUR setup. If a spot is wrong, everything "
           "downstream misreads. Redo calibration after any window size, "
           "resolution or monitor change; the red badge tells you when."},
  {"id": "cal.wizard", "tab": "cal", "sel": "#wizbtn",
   "title": "Guided calibration does it all",
   "body": "It walks every spot in order: find the Roblox window, both "
           "capacity bar ends, then the Pan, Collect Deposit and Shake "
           "prompts. For each one a full-screen overlay proposes the spot "
           "with a red ✕. Confirm it, or click the exact pixel "
           "yourself. Esc cancels."},
  {"id": "cal.cap", "tab": "cal", "sel": ".calrow[data-pkey=\"CAP_FULL_PIXEL\"]",
   "title": "The capacity bar, your most important spots",
   "body": "Dig until the bar is completely full and yellow, then set the "
           "RIGHT end, then the LEFT end. The two tips define the bar the "
           "macro measures every fill and drain from. If you ever see a "
           "capacity mis-calibration stop or phantom shake misses, redo "
           "these two."},
  {"id": "cal.pan", "tab": "cal", "sel": ".calrow[data-pkey=\"PAN_PIX\"]",
   "title": "Pan, the in-water anchor",
   "body": "A pixel on the white Pan prompt, which only shows while you "
           "stand in the water. Stand in the water so the word is up, then "
           "detect. This is how it knows it walked back far enough to "
           "shake."},
  {"id": "cal.dep", "tab": "cal", "sel": ".calrow[data-pkey=\"DEPOSIT_PIX\"]",
   "title": "Collect Deposit, the on-land anchor",
   "body": "Shows when you are on diggable land. Step onto land so it is "
           "visible, then detect. The probe digs and relic placement rely "
           "on it."},
  {"id": "cal.shake", "tab": "cal", "sel": ".calrow[data-pkey=\"SHAKE_PIX\"]",
   "title": "Shake, the mid-shake anchor",
   "body": "Shows while a shake runs. Start a shake so the word is up, then "
           "detect. The capacity bar still decides when the pan is truly "
           "empty, because this word can linger on screen."},
  {"id": "cal.green", "tab": "cal",
   "sel": ".calrow[data-pkey=\"DIG_TRIGGER_PIXEL\"]",
   "title": "Green dig pixel, only for Perfect dig",
   "body": "The green target on the dig skill bar. You only need it for "
           "Perfect dig or the green-confirm options in Shards and Geodes. "
           "Otherwise skip it."},
  {"id": "cal.regions", "tab": "cal", "sel": ".calrow[data-regionkey=\"MONEY\"]",
   "title": "Tracking regions are one drag each",
   "body": "For money, shards and finds tracking you draw a box instead of "
           "clicking corners: press Calibrate, then drag a rectangle around "
           "the number or the pop-up area. Leave room to the LEFT of a "
           "number, it grows as you earn. Skip these if you do not use "
           "tracking."},
  {"id": "cal.test", "tab": "cal", "sel": "#dettest",
   "title": "Test everything",
   "body": "<b>Test detection (live)</b> shows what the macro sees right "
           "now. <b>Test money/shards read</b> and <b>Test find pop-up "
           "read</b> print exactly what the OCR reads from your boxes. Ten "
           "seconds here saves an overnight run."},
  {"id": "cal.save", "tab": "cal", "sel": "#savepixels",
   "title": "Save it, move it",
   "body": "<b>Save calibration</b> keeps it across restarts. <b>Export</b> "
           "backs it up to a file and <b>Import</b> loads one, handy for a "
           "second PC with the same resolution."},
  {"id": "cal.masks", "tab": "cal", "sel": "#advcue", "row": True,
   "title": "Advanced cue matching, the spoof-proof option",
   "body": "Normal detection checks a small white box where each prompt "
           "sits. A player standing there in white can occasionally trip "
           "it. Advanced matching stores the exact letter shape of each cue "
           "and only fires on that shape. Capture each cue once with "
           "<b>Guided cue capture</b>; anything not captured falls back to "
           "the box. Re-capture after a resize."},
  {"id": "cal.done", "tab": "cal", "center": True,
   "title": "You are calibrated",
   "body": "If Test detection reads right on land, in water and mid-shake, "
           "you are set. When the red badge shows up later, just run Guided "
           "calibration again."},
 ],
 "cycle": [
  {"id": "cyc.map", "tab": "cycle", "sel": ".cycwrap",
   "title": "This is your pan loop",
   "body": "Each node is a stage: dig, walk back, glide, shake, land. The "
           "numbers under the nodes are your live timings. Click a node to "
           "jump to its settings."},
  {"id": "cyc.graph", "tab": "cycle", "sel": ".cygraph",
   "title": "The timeline shows where time goes",
   "body": "One pan drawn to scale in milliseconds. Wide blocks are where "
           "your pans per hour is hiding. Hover a block to see the settings "
           "behind it, click to jump to the exact slider, and drag any "
           "slider to watch the graph redraw."},
  {"id": "cyc.dig", "tab": "cycle", "sel": "#cs_dig",
   "title": "Dig: fill the pan",
   "body": "<b>Dig hold length</b> and <b>Dig speed</b> set each dig. <b>Max "
           "digs to fill</b> caps how many one pan takes. <b>Smart fill "
           "wait</b> watches the bar move so it never re-digs "
           "mid-animation. Digging two or three times before the bar moves? "
           "Turn on Smart fill wait, or use Shards mode on one-click "
           "builds."},
  {"id": "cyc.swalk", "tab": "cycle", "sel": "#cs_swalk",
   "title": "Walk back: get into the water",
   "body": "It holds S until the Pan prompt shows. If the shake starts too "
           "shallow and clips the shore, raise <b>Extra S after Pan cue</b> "
           "to go deeper."},
  {"id": "cyc.glide", "tab": "cycle", "sel": "#cs_glide",
   "title": "Glide and start: where landing problems get fixed",
   "body": "It holds W toward land and starts the shake just before the "
           "edge, so momentum carries you ashore while the pan drains. "
           "<b>If you keep landing in the water, this is the fix</b>: raise "
           "<b>Walk forward before shaking</b>, or add <b>Delay before "
           "shake starts</b>. A shake that begins too early is the number "
           "one reason you fall short of land."},
  {"id": "cyc.shake", "tab": "cycle", "sel": "#cs_shake",
   "title": "Shake and drain: empty it",
   "body": "Rapid clicks drain the pan. Leave <b>Exact shake clicks</b> at 0 "
           "to shake until the bar reads empty, best for most gear. "
           "Frequent misses? Lengthen <b>Each shake click length</b>, raise "
           "<b>Shake overall timeout</b> on slow gear, and check the "
           "capacity bar calibration."},
  {"id": "cyc.land", "tab": "cycle", "sel": "#cs_land",
   "title": "Land and prove: the arrival",
   "body": "After the pan empties it settles, finds the land cue, then a "
           "small <b>probe dig</b> proves the ground is diggable before "
           "real digging starts. These knobs tidy up the arrival and cut "
           "wasted nudges. They do not fix landing in the water; that root "
           "cause lives one stage back, in Glide and start."},
  {"id": "cyc.safety", "tab": "cycle", "sel": "#cs_safety",
   "title": "Safety nets",
   "body": "Everything that runs when a step goes wrong: stuck detection, "
           "nudges, shake retries, break-outs, the watchdog and the safe "
           "stop. On by default. <a class='tlink' "
           "data-tourlink='recovery'>Walk the ladder step by step</a>"},
  {"id": "cyc.other", "tab": "cycle", "sel": "#cs_other",
   "title": "Other tuning: the pan-empty threshold",
   "body": "<b>Pan-empty threshold</b> decides how empty the bar must read "
           "before the shake counts as done. Shakes ending with dirt still "
           "in the pan? Lower it. Shaking an already empty pan? Raise it a "
           "touch."},
  {"id": "cyc.badge", "tab": "cycle", "center": True,
   "title": "Let the yellow badge point you",
   "body": "After a few pans the app rates the run. Lots of nudges points at "
           "momentum (Glide and start). Frequent shake misses points at "
           "Shake and drain. Frequent recoveries points at calibration or "
           "the spot. The badge tooltip on the Cycle tab says which one it "
           "is, and the Coach can do the tuning for you."},
 ],
 "recovery": [
  {"id": "rec.intro", "tab": "cycle", "sel": "#cs_safety",
   "title": "Seat belts, not settings",
   "body": "This is what happens when something goes wrong: a missed click, "
           "a snag on terrain, a wedge. Everything here is on by default "
           "and escalates gently, so you almost never touch it. This short "
           "walk shows the ladder so the Run counters make sense."},
  {"id": "rec.stuck", "tab": "cycle", "sel": "[data-key=\"STUCK_TICKS\"]",
   "row": True, "title": "First: noticing it is stuck",
   "body": "If the macro sees the exact same situation this many reads in a "
           "row, it calls it stuck and steps onto the ladder. Nothing "
           "dramatic happens yet."},
  {"id": "rec.nudge", "tab": "cycle", "sel": "[data-key=\"RECOVER_ENABLED\"]",
   "row": True, "title": "Nudges",
   "body": "Small pulsed movements to wiggle free, the gentlest fix. Each "
           "one ticks the nudges counter on the Run tab. After the recovery "
           "limit it stops nudging and escalates."},
  {"id": "rec.retry", "tab": "cycle", "sel": "[data-key=\"SHAKE_RETRY_ENABLED\"]",
   "row": True, "title": "Shake retries",
   "body": "A shake that did not register gets tried again, so one glitched "
           "shake costs a retry instead of a cycle. After a couple of bad "
           "ones it does a quick click-to-empty; only after the fail limit "
           "does it give up."},
  {"id": "rec.breakout", "tab": "cycle", "sel": "[data-key=\"BREAKOUT_ENABLED\"]",
   "row": True, "title": "Break-out",
   "body": "For real stuck loops: finish the locking shake with a click "
           "burst, then reposition to break the pattern. Limited by "
           "Break-outs before STOP."},
  {"id": "rec.watchdog", "tab": "cycle", "sel": "[data-key=\"NO_PROGRESS_SEC\"]",
   "row": True, "title": "The watchdog",
   "body": "If nothing completes for this many seconds, it forces a "
           "click-to-empty to shake the state loose. The quiet last resort "
           "before stopping."},
  {"id": "rec.safestop", "tab": "Auto-stop", "sel": "[data-key=\"SAFE_STOP_RETRY\"]",
   "row": True, "title": "Safe stop: pause, do not quit",
   "body": "When it truly cannot proceed it pauses, waits, and retries, up "
           "to the max. Only then does it stop for real. Keep this on for "
           "overnight runs; with notifications on you get a DM the moment "
           "it pauses."},
  {"id": "rec.read", "tab": "cycle", "center": True,
   "title": "Reading the Run counters",
   "body": "Nudges climbing: you are landing short, fix the momentum in "
           "Glide and start. Shake misses: the pan is not emptying, look at "
           "Shake and drain. Recoveries: it keeps wedging, check "
           "calibration. The pro tools under Advanced tuning (Fortune River "
           "and Starfall River fast-travel recovery, the X pattern) are off "
           "by default and map-specific; leave them off unless you know the "
           "map."},
 ],
 "modes": [
  {"id": "mod.intro", "tab": "Treasure chest", "center": True,
   "title": "Standard is the default, modes are for special farms",
   "body": "No mode toggled means the full dig, walk, shake, land loop, "
           "right for most builds. A mode rewires that loop for one "
           "specific farm. Turn one on and its settings appear. Run only "
           "one at a time."},
  {"id": "mod.treasure", "tab": "Treasure chest",
   "sel": "[data-key=\"TREASURE_MODE\"]", "row": True,
   "title": "Treasure: the Rubble Creek two-step",
   "body": "Stand on the Rubble Creek deposit and turn this on. It digs, "
           "strafes across to the sands, digs there, and strafes back, over "
           "and over. No shaking at all. On slow dig builds raise <b>Delay "
           "between dig clicks</b> (around 12000 ms) so a dig finishes "
           "before it moves. Calibrate the Collect Deposit pixel on the "
           "Collect prompt."},
  {"id": "mod.shards", "tab": "Shards",
   "sel": "[data-key=\"SHARDS_DIG_CLICKS\"]", "row": True,
   "title": "Shards: exact clicks",
   "body": "For farms where a fixed number of clicks fills the pan, often "
           "just one. It clicks exactly that many times and proves each one "
           "registered, using the capacity bar or the green dig bar, which "
           "reacts frames earlier. <b>Assume full</b> starts the walk back "
           "the moment the fill begins. This is the cure for fast builds "
           "double-digging."},
  {"id": "mod.geodes", "tab": "Geodes", "sel": "[data-key=\"GEODE_MODE\"]",
   "row": True, "title": "Geodes: built for slow fills",
   "body": "Geode shovels fill so slowly that normal logic thinks the dig "
           "failed and nudges mid-animation. Geode mode taps the dig, waits "
           "out the animation, confirms the dig started, then runs the "
           "normal walk-back and momentum shake until the pan is truly "
           "empty. The HUD shows the animation countdown while it waits."},
  {"id": "mod.ready", "tab": "Geodes", "center": True,
   "title": "Geode builds come ready-made",
   "body": "The Builds page ships Geode Farm and Geode Farm 1-Tap, and the "
           "v3 preset loads geode timings. Start from those instead of "
           "tuning geodes by hand."},
 ],
 "tracking": [
  {"id": "trk.intro", "tab": "Earnings", "center": True,
   "title": "Three trackers, three jobs",
   "body": "Earnings reads your money and shard totals. Finds logs every "
           "item and its rarity. Tracker benchmarks the game's own Auto Pan "
           "without touching your input. They are independent; turn on only "
           "what you want. Earnings and finds reading currently work on "
           "macOS only."},
  {"id": "trk.earn", "tab": "Earnings", "sel": "[data-key=\"EARN_TRACK\"]",
   "row": True, "title": "Earnings",
   "body": "Reads the HUD totals every few seconds and credits the gains to "
           "the run, giving $/hr and shards/hr in live stats, Analytics and "
           "History. Draw the Money and Shards boxes on the Calibrate page "
           "first, then confirm with Test money/shards read."},
  {"id": "trk.finds", "tab": "Earnings", "sel": "[data-key=\"FINDS_TRACK\"]",
   "row": True, "title": "Finds",
   "body": "Watches the item pop-ups and logs each find with its rarity, "
           "feeding the ticker and loot value. Draw the Find box on the "
           "Calibrate page. The defaults are good; the one setting worth "
           "checking is which end new cards appear at, bottom or top."},
  {"id": "trk.tracker", "tab": "Tracker", "sel": "[data-key=\"TRACKER_MODE\"]",
   "row": True, "title": "Tracker: measure the game's Auto Pan",
   "body": "Watch-only. It sends zero input and counts the game's own Auto "
           "Pan off the capacity bar, exactly how it counts the macro, so "
           "the comparison is fair. Runs get labelled TRACKER in History. "
           "Recovery and relics stay off while it watches."},
  {"id": "trk.relics", "tab": "Tracker", "sel": "[data-key=\"TRACKER_RELICS\"]",
   "row": True, "title": "Relics while the game pans",
   "body": "Optional: keep your relic timers firing during Tracker runs. It "
           "clicks Auto Pan off, places the relic, and clicks Auto Pan back "
           "on, colour-checking the button each time. Calibrate the Auto "
           "Pan button with its ON and OFF states first."},
  {"id": "trk.verify", "tab": "Tracker", "center": True,
   "title": "Verify before a long run",
   "body": "On the Calibrate page, Test money/shards read and Test find "
           "pop-up read show exactly what the OCR sees. Empty lines mean "
           "widen or re-draw the box."},
 ],
 "builds": [
  {"id": "bld.intro", "tab": "builds", "center": True,
   "title": "A build is your whole setup",
   "body": "Every setting on every page plus relics, saved under one name. "
           "Load one and everything applies at once. Geode Farm and Geode "
           "Farm 1-Tap ship with the app."},
  {"id": "bld.save", "tab": "builds", "sel": "#bldname2",
   "title": "Save and overwrite",
   "body": "Type a name and press Save current to snapshot what you have "
           "now. On a card, Load applies it, Overwrite re-captures your "
           "current settings into it, and clicking the description edits "
           "it."},
  {"id": "bld.find", "tab": "builds", "sel": "#bldsearch",
   "title": "Find the one you want",
   "body": "Search by name or description, and sort by newest, most used or "
           "recently used."},
  {"id": "bld.share", "tab": "builds", "sel": "#bldimport",
   "title": "Share builds with the gear doc inside",
   "body": "Export on a card writes one file you can send a friend. Import "
           "loads theirs, renamed on a clash so it never overwrites yours. "
           "Attach an equipment doc (Word, PDF or image) and it travels "
           "inside the file, so they know exactly what gear to make."},
  {"id": "bld.safe", "tab": "builds", "center": True,
   "title": "Old builds never break",
   "body": "Loading a build made on an older version keeps your current "
           "values for any settings it does not know about. Nothing gets "
           "zeroed. Save your own build before experimenting and you can "
           "always come back."},
 ],
 "relics": [
  {"id": "rel.intro", "tab": "relics", "sel": "#relicsMaster", "row": True,
   "title": "Auto-use timed items",
   "body": "Every N minutes the macro pauses, switches to the item's hotbar "
           "slot, double-clicks to place it, returns to the pan and "
           "resumes. Turn on the master switch and the rows you carry."},
  {"id": "rel.row", "tab": "relics", "sel": ".rrow",
   "title": "One row per relic",
   "body": "A name for the HUD, the minutes between uses, the hotbar slot "
           "it sits in, and how many clicks placing it takes."},
  {"id": "rel.behaviour", "tab": "Relic behaviour",
   "sel": "[data-key=\"RELIC_ON_LAND\"]", "row": True,
   "title": "Placing safely",
   "body": "Place relics only when ON LAND waits for a safe moment so the "
           "relic does not drop in the water; the max wait places it anyway "
           "if land never comes. Relative timers keep counting while "
           "paused, matching how the buffs burn in game."},
  {"id": "rel.save", "tab": "relics", "sel": "#saverelics",
   "title": "Save, and the hotkeys",
   "body": "Save relics keeps the rows. In game, ctrl+shift+1 through 9 "
           "resets one timer and ctrl+U resets all, for when you place "
           "something by hand."},
 ],
 "alerts": [
  {"id": "alr.discord", "tab": "Notifications",
   "sel": "[data-key=\"WEBHOOK_ENABLED\"]", "row": True,
   "title": "Discord notifications",
   "body": "Paste your own Discord webhook URL on the Notifications page and "
           "turn on Discord notifications. That is the whole setup: the "
           "macro posts run updates to your channel so you do not babysit "
           "it. Off by default; nothing is sent until you configure it."},
  {"id": "alr.events", "tab": "Notifications",
   "sel": "[data-key=\"NOTIFY_SAFE_STOP\"]", "row": True,
   "title": "Pick your events",
   "body": "Started, stopped, periodic stats, safe stop (it paused on "
           "trouble, the important one) and errors. Recoveries is off by "
           "default because it can get chatty. Attach a screenshot shows "
           "what the game looked like at that moment. Use <b>Send test "
           "notification</b> below to prove the pipeline works."},
  {"id": "alr.timer", "tab": "Auto-stop",
   "sel": "[data-key=\"AUTOSTOP_ENABLED\"]", "row": True,
   "title": "Stop on a timer",
   "body": "Ends the run after a set number of minutes, a clean cap for "
           "overnight sessions. You get the stopped DM if notifications are "
           "on."},
  {"id": "alr.bag", "tab": "Auto-stop", "sel": "[data-key=\"STOP_AFTER_PANS\"]",
   "row": True, "title": "The bag-full guard",
   "body": "Stops after N pans so you do not keep panning into a full "
           "inventory. Set it near what your backpack holds. 0 is off."},
 ],
 "studio": [
  {"id": "st.intro", "tab": "studio", "center": True,
   "title": "Studio: build your own mode",
   "body": "Studio is your own version of the built-in modes. You snap "
           "Prospecting blocks together (dig, walk until a prompt shows, "
           "shake, wait) into a script, and the macro runs it with the same "
           "calibration, stats and safety nets as Standard or Treasure. No "
           "code anywhere."},
  {"id": "st.library", "tab": "studio", "sel": "#stgrid",
   "title": "Your script library",
   "body": "Every script you save or import lives here: set one active, run "
           "it, open it in the editor, duplicate it to experiment, export it "
           "as a .ppscript file for a friend, or delete it. The green chip "
           "marks the active script, the one the Start button runs."},
  {"id": "st.open", "tab": "studio", "sel": "#stopen",
   "title": "The Studio window",
   "body": "Open Studio opens the editor in its own window, like Roblox "
           "Studio next to Roblox: block palette on the left, your script in "
           "the middle, the selected block's settings on the right. Its own "
           "short walkthrough offers itself the first time and rebuilds the "
           "Treasure script with you, step by step."},
  {"id": "st.new", "tab": "studio", "sel": "#stnew",
   "title": "Start from a template",
   "body": "New script starts you from a template: the Standard loop or "
           "Treasure rebuilt from blocks, or a blank canvas. Templates are "
           "the fastest way to learn how the blocks snap together."},
  {"id": "st.import", "tab": "studio", "sel": "#stimport",
   "title": "Share scripts as one file",
   "body": "Import script reads a friend's .ppscript file; Export on any "
           "card writes yours. Imported files are checked block by block "
           "before they can ever run, so a broken or tampered file is "
           "refused with a clear reason instead of wrecking a run."},
  {"id": "st.runtab", "tab": "run", "sel": "#scriptsel",
   "title": "Running a script",
   "body": "The Mode picker on the Run tab switches between the built-in "
           "modes and your scripts. While a script is active it supersedes "
           "the Treasure, Shards and Geodes toggles, and runs, stats and "
           "History behave exactly like any other run. Esc and Ctrl+K "
           "always stop it."},
 ],
 "studio_editor": [
  {"id": "ste.hello", "center": True,
   "title": "Build Treasure mode from blocks",
   "body": "This two minute walkthrough rebuilds the real Treasure mode: "
           "dig a Rubble Creek deposit, strafe to the sands until Collect "
           "shows, dig, strafe back, repeat. By the end you will know every "
           "part of Studio."},
  {"id": "ste.new", "sel": "#stnewbtn",
   "title": "Start from a template",
   "body": "Press <b>New script</b> and pick <b>Treasure (Rubble Creek)</b> "
           "to get the finished version to study, or <b>Blank</b> to build "
           "it yourself as we go. You can keep this walkthrough open while "
           "you click around."},
  {"id": "ste.palette", "sel": "#pal",
   "title": "The palette",
   "body": "Every block the macro understands, in three families. "
           "<b>Actions</b> send input: Dig click, Shake clicks, Wait. "
           "<b>Sensing</b> reads the screen through your calibration: Wait "
           "for cue, Wait for capacity, the If blocks. <b>Flow</b> "
           "organises: Repeat, Group, Safe stop. Click a block to add it, "
           "or drag it exactly where it belongs. Hover one and the help "
           "panel on the right explains it."},
  {"id": "ste.canvas", "sel": "#canvasWrap",
   "title": "The canvas is your script",
   "body": "Blocks run top to bottom, then the whole script repeats "
           "forever; one full lap counts one pan. Treasure needs five "
           "working blocks: <b>Dig click</b> at 8 ms, <b>Wait</b> 12000 ms "
           "for the slow dig animation, <b>Wait for cue</b> holding D until "
           "Collect Deposit shows, then the same dig and wait again, and a "
           "final <b>Wait for cue</b> holding A to strafe back. Drag to "
           "reorder; drop onto an If or Repeat to put a block inside."},
  {"id": "ste.inspector", "sel": "#insp",
   "title": "The inspector",
   "body": "Select a block and its settings appear here: sliders with "
           "known-safe ranges, dropdowns for prompts and keys, and a plain "
           "sentence saying exactly what the block will do. For the two "
           "strafe blocks, turn on <b>Leave the current prompt first</b> so "
           "the spot you are still standing on does not count instantly."},
  {"id": "ste.validate", "sel": "#valbtn",
   "title": "Validate as you go",
   "body": "The dot up here stays green while the script is runnable. "
           "Problems show on the block and in a list at the bottom, each "
           "naming the fix: empty containers, steps stuck after a Safe "
           "stop, scripts that never send input. Everything is checked "
           "again before a run ever starts."},
  {"id": "ste.save", "sel": "#stsave",
   "title": "Save it",
   "body": "Save keeps the script in your library (Ctrl+S works too). "
           "Drafts with problems save fine, they just cannot run until the "
           "list is clear, so you never lose work."},
  {"id": "ste.run", "sel": "#strun",
   "title": "Run it",
   "body": "Run saves, sets this script as the active mode and starts the "
           "macro, exactly like the Start button. Click into Roblox so the "
           "game has focus. Esc or Ctrl+K stops it, pans and digs count on "
           "the Run tab, and the run lands in History under this script's "
           "name. Have fun out there."},
 ],
}


def _tutorial_local():
    return _read_json(TUTORIAL_FILE, {}) or {}


def _tutorial_merged():
    """Defaults + local owner edits, merged per entry id. Overrides live
    flat: {"<card id>": {...}, "help:<KEY>": {...}, "ui:<id>": {...}}.
    A card override may replace title/body and add img (a data url or web
    url) and vid (a YouTube link)."""
    over = {}
    for src in (_tutorial_local(),):
        o = src.get("overrides") if isinstance(src.get("overrides"), dict) else src
        for k, v in (o or {}).items():
            if isinstance(v, dict):
                over.setdefault(k, {}).update(v)
    tours = {}
    for name, steps in TOUR_DEFAULTS.items():
        out = []
        for st in steps:
            st = dict(st)
            ov = over.get(st["id"]) or {}
            for fld in ("title", "body", "img", "vid"):
                if ov.get(fld):
                    st[fld] = ov[fld]
            out.append(st)
        tours[name] = out
    # Help lookup keys: settings live under "help:<KEY>"; UI help (buttons,
    # stats, stages, calibration rows) keep the semantic keys authored in
    # UI_HELP (e.g. "startbtn", "stage:dig", "cal:MONEY", "cyc:graph").
    helps = {}
    for k, v in HELP.items():
        helps["help:" + k] = {"body": v}
    for k, v in UI_HELP.items():
        helps[k] = {"body": v}
    for _sbt, _sbd in STUDIO_BLOCKS.items():
        helps["studio:" + _sbt] = {"body": _sbd.get("help", "")}
    tour_ids = {st["id"] for steps in tours.values() for st in steps}
    for k, ov in over.items():
        if k in tour_ids:
            continue                       # already applied to the tour step
        helps.setdefault(k, {}).update(
            {f: ov[f] for f in ("body", "img", "vid") if ov.get(f)})
    return {"tours": tours, "help": helps, "owner": _is_owner()}


# ============================================================================
# PROSPECTOR STUDIO -- script model, validation, templates, persistence
# ============================================================================
# A script is DATA, never code: a versioned JSON tree of blocks the engine
# interpreter walks. Everything here validates/sanitizes against the single
# schema source of truth (STUDIO_BLOCKS in prospecting_ui.py). Files are
# written two-phase (tmp + os.replace) so a crash can never truncate them.

def _studio_load():
    """prospecting_scripts.json, tolerant of a missing/garbled file. A file
    that exists but cannot be parsed falls back to the .bak written on every
    save, so a crash mid-write or a bad hand-edit never loses the library."""
    d = _read_json(SCRIPTS_FILE, None)
    if not isinstance(d, dict):
        if os.path.exists(SCRIPTS_FILE):
            d = _read_json(SCRIPTS_FILE + ".bak", None)
            if isinstance(d, dict):
                print("[studio] scripts file unreadable; recovered the "
                      "previous state from its backup")
        if not isinstance(d, dict):
            d = {}
    scripts = d.get("scripts")
    if not isinstance(scripts, dict):
        scripts = {}
    meta = d.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    active = d.get("active")
    if not isinstance(active, str) or active not in scripts:
        active = ""
    mode = d.get("mode")
    if mode not in ("classic", "studio", "script"):
        mode = ""                      # unset -> derived from active
    last_active = d.get("last_active")
    if not isinstance(last_active, str) or last_active not in scripts:
        last_active = ""
    # STUDIO mode parks the classic AutoPan-Tracking preference here (see
    # studio_mode): the engine must never see TRACKER_MODE while a Studio
    # build is meant to run (tracker outranks scripts in the engine), and
    # switching back to CLASSIC must restore the user's choice untouched.
    ct = d.get("classic_tracker")
    ct = ct if isinstance(ct, bool) else None
    return {"active": active, "scripts": scripts, "meta": meta,
            "mode": mode, "last_active": last_active, "classic_tracker": ct}


_STUDIO_LIST_CACHE = {"key": None, "scripts": None, "active": ""}


def _studio_write(data):
    """Two-phase write plus a rolling .bak of the previous state: neither a
    crash mid-write nor a bad save can lose more than the very last change."""
    tmp = SCRIPTS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    if os.path.exists(SCRIPTS_FILE):
        try:
            shutil.copyfile(SCRIPTS_FILE, SCRIPTS_FILE + ".bak")
        except OSError:
            pass
    os.replace(tmp, SCRIPTS_FILE)
    _STUDIO_LIST_CACHE["key"] = None


def _config_patch(patch):
    """Merge a few keys into the config file atomically (tmp + replace).
    Only for idle-time bookkeeping (mode switches, launch projection) --
    live-run settings still go through the engine ack path."""
    cur = load_saved()
    cur.update(patch)
    _config_write(cur)


def _studio_normalize(script):
    """Forward compatibility without migrations: fill anything an OLDER
    schema did not know about (new params, new optional fields) with the
    current defaults. Runs at every LOAD boundary (open, list validation,
    engine push); the editor always writes complete data, so saving stays
    strict. Returns a deep copy; never mutates stored data."""
    if not isinstance(script, dict):
        return script
    s = json.loads(json.dumps(script))
    s.setdefault("description", "")
    s.setdefault("author", "")
    s.setdefault("settings", {})
    now = int(time.time())
    s.setdefault("created", now)
    s.setdefault("updated", now)

    def fill(lst):
        for b in lst or []:
            if not isinstance(b, dict):
                continue
            d = STUDIO_BLOCKS.get(b.get("type"))
            if d is None:
                continue
            p = b.get("params")
            if not isinstance(p, dict):
                p = b["params"] = {}
            for spec in d["params"]:
                p.setdefault(spec["key"], spec["default"])
            if b.get("type") in STUDIO_CONTAINERS:
                if not isinstance(b.get("children"), list):
                    b["children"] = []
                fill(b["children"])
    if isinstance(s.get("blocks"), list):
        fill(s["blocks"])
    return s


def _studio_name_ok(name):
    if not isinstance(name, str):
        return False
    n = name.strip()
    if not (1 <= len(n) <= 60) or n != name:
        return False
    return all(ch.isprintable() for ch in n)


def _studio_kind(script):
    """The document kind Prospector Studio stamps on published files:
    "build" is a prospecting cycle (STUDIO BUILD), "script" is general
    automation (STUDIO SCRIPT). Every pre-kind file was a build, so
    anything else -- absent, v1, malformed -- reads as "build"."""
    if isinstance(script, dict) and script.get("kind") == "script":
        return "script"
    return "build"


def _studio_ui_mode(d):
    """Server-derived top-level mode for a Studio launch: an explicit,
    whitelisted choice wins; otherwise the active entry's kind decides
    (script-kind -> STUDIO SCRIPT, build-kind -> STUDIO BUILD) and no
    active entry means CLASSIC."""
    m = d.get("mode")
    if m in ("classic", "studio", "script"):
        return m
    if d["active"]:
        s = d["scripts"].get(d["active"])
        return "script" if _studio_kind(s) == "script" else "studio"
    return "classic"


def _studio_count_blocks(blocks):
    n = 0
    for b in blocks or []:
        n += 1
        if isinstance(b, dict) and isinstance(b.get("children"), list):
            n += _studio_count_blocks(b["children"])
    return n


def _studio_find_block(blocks, bid):
    """The block with this id anywhere in the tree (children + else)."""
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        if b.get("id") == bid:
            return b
        for branch in ("children", "else"):
            kid = _studio_find_block(b.get(branch) or [], bid)
            if kid is not None:
                return kid
    return None


def _studio_params(script):
    """The macro-adjustable parameters a published script DECLARES in
    settings.params (Prospector Studio writes them at publish time). Each
    entry is sanitized against the script itself: a variable-backed param
    must name a declared variable, a node-backed param must name a real
    block, and the current value is read from the live document — never
    invented. Unknown/malformed entries are skipped, not errors."""
    out = []
    if not isinstance(script, dict):
        return out
    settings = script.get("settings")
    params = settings.get("params") if isinstance(settings, dict) else None
    if not isinstance(params, list):
        return out
    var_by_name = {v.get("name"): v for v in script.get("variables") or []
                   if isinstance(v, dict)}
    for p in params[:64]:
        if not isinstance(p, dict):
            continue
        name = p.get("name")
        if not isinstance(name, str) or not name or len(name) > 64:
            continue
        kind = "node" if p.get("kind") == "node" else "variable"
        ptype = p.get("type")
        if ptype not in ("number", "bool", "string", "choice"):
            continue
        current = None
        node_id, node_key = "", ""
        if kind == "variable":
            v = var_by_name.get(name)
            if not isinstance(v, dict):
                continue
            current = v.get("initial")
        else:
            node_id = str(p.get("node") or "")
            node_key = str(p.get("key") or "")
            b = _studio_find_block(script.get("blocks"), node_id)
            if b is None or not node_key:
                continue
            current = (b.get("params") or {}).get(node_key)
        entry = {"name": name, "kind": kind, "type": ptype,
                 "label": str(p.get("label") or name)[:60],
                 "group": str(p.get("group") or "Project settings")[:40],
                 "unit": str(p.get("unit") or "")[:12],
                 "desc": str(p.get("desc") or "")[:200],
                 "default": p.get("default"),
                 "current": current,
                 "node": node_id, "key": node_key}
        for k in ("min", "max", "step"):
            v = p.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                entry[k] = v
        opts = p.get("options")
        if ptype == "choice":
            if not (isinstance(opts, list) and opts
                    and all(isinstance(o, str) for o in opts)):
                continue
            entry["options"] = opts[:20]
        out.append(entry)
    return out


def _studio_block_title(b, idx_path):
    d = STUDIO_BLOCKS.get((b or {}).get("type") or "", {})
    nm = d.get("name") or str((b or {}).get("type") or "block")
    return "Block %s (%s)" % (".".join(str(i + 1) for i in idx_path), nm)


def _studio_expr_ok(e, var_names, depth=0):
    """Static well-formedness walk of one wired-expression tree (v2)."""
    if depth > 32 or not isinstance(e, dict):
        return False
    if "lit" in e:
        v = e["lit"]
        if isinstance(v, bool):
            return True
        if isinstance(v, (int, float)):
            return v == v and v != float("inf") and v != float("-inf")
        return isinstance(v, str) and len(v) <= STUDIO2_STR_MAX
    if "var" in e:
        return isinstance(e["var"], str) and e["var"] in var_names
    if "read" in e:
        return e["read"] in STUDIO2_READS
    if "op" in e:
        args = e.get("args")
        return (e["op"] in STUDIO2_OPS and isinstance(args, list)
                and len(args) <= 4
                and all(_studio_expr_ok(x, var_names, depth + 1)
                        for x in args))
    return False


def _studio2_count(lst):
    n = 0
    for b in lst:
        if not isinstance(b, dict):
            continue
        n += 1
        for key in ("children", "else"):
            kids = b.get(key)
            if isinstance(kids, list):
                n += _studio2_count(kids)
    return n


def _studio2_stop_safe(lst):
    for b in lst:
        if not isinstance(b, dict) or b.get("type") not in STUDIO2_STOP_SAFE:
            return False
        for key in ("children", "else"):
            kids = b.get(key)
            if isinstance(kids, list) and not _studio2_stop_safe(kids):
                return False
    return True


def _studio_validate_v2(script):
    """Structural check for version 2/3 programs (made in the standalone
    Prospector Studio desktop app). The engine re-validates everything
    again at runtime behind its own hard rails, so this focuses on shape,
    limits, and clear messages. The embedded editor never edits these.
    v3 adds general input; its capability declaration is checked here so
    the library refuses an undeclared file with the same message the
    engine would give at run time."""
    errors, problems = [], []
    is_v3 = script.get("version") == STUDIO_SCHEMA_V3
    types_tab = STUDIO3_TYPES if is_v3 else STUDIO2_TYPES
    if script.get("format") != "ppscript":
        errors.append("Not a Prospector Studio script (missing the "
                      "ppscript marker).")
    if not _studio_name_ok(script.get("name")):
        errors.append("The script needs a name: 1 to 60 printable "
                      "characters, no leading or trailing spaces.")
    desc = script.get("description", "")
    if not isinstance(desc, str) or len(desc) > 300:
        errors.append("The description must be text, at most 300 characters.")
    known = {"format", "version", "name", "description", "author",
             "created", "updated", "blocks", "settings", "variables",
             "hooks", "kind"}
    kind = script.get("kind")
    if kind is not None and kind not in ("build", "script"):
        errors.append("Unknown document kind %r (expected build or "
                      "script)." % (kind,))
    if is_v3:
        known.add("caps")
        caps = script.get("caps", [])
        if caps is None:
            caps = []
        if (not isinstance(caps, list)
                or any(c not in STUDIO3_CAPS for c in caps)):
            errors.append("The script declares an unknown capability.")
            caps = [c for c in caps if c in STUDIO3_CAPS] \
                if isinstance(caps, list) else []
    extra = [k for k in script if k not in known]
    if extra:
        errors.append("Unknown fields in the script file: %s."
                      % ", ".join(sorted(str(k) for k in extra)[:5]))
    var_names = set()
    vs = script.get("variables", [])
    if vs is None:
        vs = []
    if not isinstance(vs, list) or len(vs) > STUDIO2_MAX_VARS:
        errors.append("The variables list is malformed or too long.")
        vs = []
    for d in vs:
        nm = d.get("name") if isinstance(d, dict) else None
        ty = d.get("type") if isinstance(d, dict) else None
        if (not isinstance(nm, str) or not nm or len(nm) > 32
                or not (nm[0].isalpha() and all(ch.isalnum() or ch == "_"
                                                for ch in nm))
                or nm in var_names
                or ty not in ("number", "bool", "string")):
            errors.append("A variable declaration is malformed.")
            continue
        var_names.add(nm)
    blocks = script.get("blocks")
    if not isinstance(blocks, list):
        errors.append("The blocks field must be a list.")
        return {"ok": False, "errors": errors, "problems": problems}
    total = _studio2_count(blocks)
    if total > STUDIO2_MAX_BLOCKS:
        errors.append("Too many blocks (%d; the limit is %d)."
                      % (total, STUDIO2_MAX_BLOCKS))
        return {"ok": False, "errors": errors, "problems": problems}
    if not blocks:
        problems.append("The script is empty; publish it again from "
                        "Prospector Studio with at least one step.")
    seen_ids = set()
    need_caps = set()

    def walk(lst, depth):
        if depth > STUDIO2_MAX_DEPTH:
            errors.append("Blocks are nested deeper than %d levels."
                          % STUDIO2_MAX_DEPTH)
            return
        for b in lst:
            if not isinstance(b, dict):
                errors.append("A block is not an object.")
                continue
            bid = b.get("id")
            if not isinstance(bid, str) or not (1 <= len(bid) <= 64):
                errors.append("A block has a bad or missing id.")
            elif bid in seen_ids:
                errors.append("Duplicate block id '%s'." % bid)
            else:
                seen_ids.add(bid)
            t = b.get("type")
            if t not in types_tab:
                errors.append("Unknown block type %r." % (t,))
                continue
            if is_v3:
                need_caps.update(
                    c for c in (STUDIO3_CAP_OF.get(t),) if c)
            params = b.get("params")
            if params is None:
                params = {}
            if not isinstance(params, dict):
                errors.append("Block '%s': params must be an object."
                              % (bid,))
                params = {}
            for k, v in params.items():
                if isinstance(v, dict):
                    if "$expr" not in v                             or not _studio_expr_ok(v.get("$expr"), var_names):
                        errors.append("Block '%s': the wired value on %r "
                                      "is malformed." % (bid, k))
                elif not isinstance(v, (int, float, str, bool)):
                    errors.append("Block '%s': parameter %r has a bad "
                                  "value." % (bid, k))
                elif isinstance(v, str) and len(v) > STUDIO2_STR_MAX:
                    errors.append("Block '%s': parameter %r is too long."
                                  % (bid, k))
            kids = b.get("children")
            if kids is not None and not isinstance(kids, list):
                errors.append("Block '%s': children must be a list." % (bid,))
                kids = []
            if kids and t not in STUDIO2_CONTAINERS:
                errors.append("Block '%s' (%s) cannot contain steps."
                              % (bid, t))
            els = b.get("else")
            if els is not None:
                if not isinstance(els, list) or t not in STUDIO2_ELSE_TYPES:
                    errors.append("Block '%s' cannot have a No branch."
                                  % (bid,))
                    els = []
            if isinstance(kids, list) and kids:
                walk(kids, depth + 1)
            if isinstance(els, list) and els:
                walk(els, depth + 1)

    walk(blocks, 1)
    hk = script.get("hooks", {})
    if hk is None:
        hk = {}
    if not isinstance(hk, dict):
        errors.append("The hooks field must be an object.")
        hk = {}
    for k, body in hk.items():
        if k not in STUDIO2_HOOKS or not isinstance(body, list):
            errors.append("Unknown or malformed hook %r." % (k,))
            continue
        if _studio2_count(body) > STUDIO2_MAX_HOOK_BLOCKS:
            errors.append("The %s hook has too many blocks." % k)
            continue
        if k == "on_stop" and body and not _studio2_stop_safe(body):
            errors.append("The on_stop hook may only set variables, log, "
                          "show HUD text, notify, or branch.")
        walk(body, 1)
    if is_v3:
        missing = sorted(need_caps - set(caps))
        if missing:
            errors.append("The script uses abilities it "
                          "does not declare (%s); re-export "
                          "it from Prospector Studio."
                          % ", ".join(STUDIO3_CAP_LABEL.get(m, m)
                                      for m in missing))
    return {"ok": not errors, "errors": errors, "problems": problems}


def _studio_sanitize_v2(script):
    """Import path for version 2 files: strict validation, no repair. A v2
    program is compiled output; a malformed one must be re-exported from
    Prospector Studio, not silently patched here."""
    chk = _studio_validate_v2(script)
    if not chk["ok"]:
        return None, ("Could not import this Prospector Studio script: "
                      + " ".join(chk["errors"][:3])
                      + " Re-export it from Prospector Studio.")
    out = {"format": "ppscript", "version": STUDIO_SCHEMA_V2}
    out["name"] = "".join(ch for ch in str(script.get("name", "")).strip()
                          if ch.isprintable())[:60] or "Imported script"
    out["description"] = ("".join(ch for ch in str(script.get("description")
                                                   or "")
                                  if ch.isprintable()))[:300]
    out["author"] = ("".join(ch for ch in str(script.get("author") or "")
                             if ch.isprintable()))[:60]
    now = int(time.time())
    created = script.get("created")
    out["created"] = created if isinstance(created, int)         and not isinstance(created, bool) and created >= 0 else now
    out["updated"] = now
    out["settings"] = {}
    out["blocks"] = script.get("blocks") or []
    vs = script.get("variables")
    if isinstance(vs, list) and vs:
        out["variables"] = vs
    hk = script.get("hooks")
    if isinstance(hk, dict):
        hk = {k: v for k, v in hk.items()
              if k in STUDIO2_HOOKS and isinstance(v, list) and v}
        if hk:
            out["hooks"] = hk
    return out, None


def _studio_validate(script):
    """STRICT schema + runnability check. Returns
    {"ok": bool, "errors": [str], "problems": [str]} where errors mean the
    script is malformed (reject) and problems mean it saves fine but cannot
    run or be set active until fixed. Every message names the block."""
    errors, problems = [], []
    if not isinstance(script, dict):
        return {"ok": False, "errors": ["That is not a script."], "problems": []}
    if script.get("format") != "ppscript":
        errors.append("Not a Prospector Studio script (missing the ppscript marker).")
    v = script.get("version")
    if not isinstance(v, int) or isinstance(v, bool) or v < 1:
        errors.append("Unsupported script version.")
    elif v in (STUDIO_SCHEMA_V2, STUDIO_SCHEMA_V3):
        # Standalone Prospector Studio program: its own validation path
        # (v3 = v2 shape + general input under declared capabilities).
        return _studio_validate_v2(script)
    elif v > STUDIO_SCHEMA_VERSION:
        errors.append("This script was made with a newer version of "
                      "Prospector Lite; update the app to open it.")
    if not _studio_name_ok(script.get("name")):
        errors.append("The script needs a name: 1 to 60 printable characters, "
                      "no leading or trailing spaces.")
    desc = script.get("description", "")
    if not isinstance(desc, str) or len(desc) > 300:
        errors.append("The description must be text, at most 300 characters.")
    author = script.get("author", "")
    if not isinstance(author, str) or len(author) > 60:
        errors.append("The author must be text, at most 60 characters.")
    for fld in ("created", "updated"):
        v = script.get(fld, 0)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            errors.append("The %s stamp must be a whole number." % fld)
    st = script.get("settings", {})
    if not isinstance(st, dict):
        errors.append("The settings field must be an object.")
    known = {"format", "version", "name", "description", "author",
             "created", "updated", "blocks", "settings"}
    extra = [k for k in script if k not in known]
    if extra:
        errors.append("Unknown fields in the script file: %s."
                      % ", ".join(sorted(str(k) for k in extra)[:5]))
    blocks = script.get("blocks")
    if not isinstance(blocks, list):
        errors.append("The blocks field must be a list.")
        return {"ok": False, "errors": errors, "problems": problems}
    total = _studio_count_blocks(blocks)
    if total > STUDIO_MAX_BLOCKS:
        errors.append("Too many blocks (%d; the limit is %d)."
                      % (total, STUDIO_MAX_BLOCKS))
        return {"ok": False, "errors": errors, "problems": problems}
    seen_ids = set()
    has_action = []

    def walk(lst, idx_path, depth):
        if depth > STUDIO_MAX_DEPTH:
            errors.append("Blocks are nested deeper than %d levels."
                          % STUDIO_MAX_DEPTH)
            return
        stopped = False
        for i, b in enumerate(lst):
            path = idx_path + [i]
            if not isinstance(b, dict):
                errors.append("Block %s is not an object."
                              % ".".join(str(x + 1) for x in path))
                continue
            title = _studio_block_title(b, path)
            bid = b.get("id")
            if (not isinstance(bid, str) or not (1 <= len(bid) <= 24)
                    or not all(c.isalnum() or c in "_-" for c in bid)):
                errors.append("%s: bad or missing block id." % title)
            elif bid in seen_ids:
                errors.append("%s: duplicate block id '%s'." % (title, bid))
            else:
                seen_ids.add(bid)
            t = b.get("type")
            d = STUDIO_BLOCKS.get(t) if isinstance(t, str) else None
            if d is None:
                errors.append("%s: unknown block type %r." % (title, t))
                continue
            extra_f = [k for k in b if k not in ("id", "type", "params", "children")]
            if extra_f:
                errors.append("%s: unknown fields %s."
                              % (title, ", ".join(sorted(str(k) for k in extra_f)[:4])))
            params = b.get("params")
            if not isinstance(params, dict):
                errors.append("%s: params must be an object." % title)
                params = {}
            spec = {p["key"]: p for p in d["params"]}
            for k in params:
                if k not in spec:
                    errors.append("%s: unknown parameter %r." % (title, k))
            for k, p in spec.items():
                if k not in params:
                    errors.append("%s: missing \"%s\"." % (title, p["label"]))
                    continue
                v = params[k]
                if p["type"] == "int":
                    if isinstance(v, bool) or not isinstance(v, int):
                        errors.append("%s: \"%s\" must be a whole number."
                                      % (title, p["label"]))
                    else:
                        lo, hi, _s = p["range"]
                        if not (lo <= v <= hi):
                            errors.append("%s: \"%s\" must be between %d and %d."
                                          % (title, p["label"], lo, hi))
                elif p["type"] == "bool":
                    if not isinstance(v, bool):
                        errors.append("%s: \"%s\" must be on or off."
                                      % (title, p["label"]))
                elif p["type"] == "choice":
                    if v not in [c[0] for c in p["choices"]]:
                        errors.append("%s: \"%s\" has an invalid choice %r."
                                      % (title, p["label"], v))
                else:  # str
                    if not isinstance(v, str) or len(v) > p.get("max", 200):
                        errors.append("%s: \"%s\" must be text, at most %d "
                                      "characters." % (title, p["label"],
                                                       p.get("max", 200)))
                    elif any((not ch.isprintable()) for ch in v):
                        errors.append("%s: \"%s\" contains characters that "
                                      "cannot be typed." % (title, p["label"]))
            kids = b.get("children")
            if t in STUDIO_CONTAINERS:
                if kids is None:
                    kids = []
                if not isinstance(kids, list):
                    errors.append("%s: children must be a list." % title)
                    kids = []
                if not kids:
                    problems.append("%s is empty; give it at least one step "
                                    "inside, or remove it." % title)
                walk(kids, path, depth + 1)
            else:
                if kids not in (None, []):
                    errors.append("%s: this block cannot hold steps inside."
                                  % title)
            if stopped:
                problems.append("%s can never run: it sits after a Safe stop "
                                "in the same list." % title)
            if t == "stop":
                stopped = True
            if t in ("dig", "shake", "hold_key", "tap_key", "click", "relic"):
                has_action.append(True)

    walk(blocks, [], 1)
    if not blocks:
        problems.append("The script is empty; add blocks from the palette, "
                        "or start from a template.")
    elif not has_action and not errors:
        problems.append("This script never sends any input (no dig, shake, "
                        "key, click or relic block), so it would do nothing.")
    return {"ok": not errors, "errors": errors, "problems": problems}


def _studio_sanitize(script):
    """Best-effort repair of an IMPORTED (untrusted) script: coerce types,
    clamp ranges, drop unknown fields, regenerate every id. Anything that
    cannot be repaired safely (unknown block type, too many blocks, too
    deep) returns an error instead. Never executes anything."""
    if not isinstance(script, dict):
        return None, "That file does not contain a script."
    if script.get("version") == STUDIO_SCHEMA_V2:
        return _studio_sanitize_v2(script)
    out = {"format": "ppscript", "version": 1}
    nm = script.get("name")
    out["name"] = (str(nm).strip()[:60] if isinstance(nm, (str, int, float))
                   and str(nm).strip() else "Imported script")
    out["name"] = "".join(ch for ch in out["name"] if ch.isprintable()) or "Imported script"
    out["description"] = ("".join(ch for ch in str(script.get("description") or "")
                                  if ch.isprintable()))[:300]
    out["author"] = ("".join(ch for ch in str(script.get("author") or "")
                             if ch.isprintable()))[:60]
    now = int(time.time())
    out["created"] = script.get("created") if isinstance(script.get("created"), int) \
        and not isinstance(script.get("created"), bool) and script.get("created") >= 0 else now
    out["updated"] = now
    out["settings"] = {}
    blocks = script.get("blocks")
    if not isinstance(blocks, list):
        return None, "That file has no blocks list."
    if _studio_count_blocks(blocks) > STUDIO_MAX_BLOCKS:
        return None, ("That script has more than %d blocks; refusing it."
                      % STUDIO_MAX_BLOCKS)
    counter = [0]
    bad = []

    def fix(lst, depth):
        if depth > STUDIO_MAX_DEPTH:
            bad.append("Blocks are nested deeper than %d levels."
                       % STUDIO_MAX_DEPTH)
            return []
        res = []
        for b in lst:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            d = STUDIO_BLOCKS.get(t) if isinstance(t, str) else None
            if d is None:
                bad.append("Unknown block type %r." % (t,))
                continue
            counter[0] += 1
            nb = {"id": "b%d" % counter[0], "type": t, "params": {}}
            src = b.get("params") if isinstance(b.get("params"), dict) else {}
            for p in d["params"]:
                v = src.get(p["key"], p["default"])
                if p["type"] == "int":
                    try:
                        v = int(v)
                    except (TypeError, ValueError):
                        v = p["default"]
                    if isinstance(src.get(p["key"]), bool):
                        v = p["default"]
                    lo, hi, _s = p["range"]
                    v = max(lo, min(hi, v))
                elif p["type"] == "bool":
                    v = bool(v)
                elif p["type"] == "choice":
                    if v not in [c[0] for c in p["choices"]]:
                        v = p["default"]
                else:
                    v = "".join(ch for ch in str(v if v is not None else "")
                                if ch.isprintable())[:p.get("max", 200)]
                nb["params"][p["key"]] = v
            if t in STUDIO_CONTAINERS:
                kids = b.get("children") if isinstance(b.get("children"), list) else []
                nb["children"] = fix(kids, depth + 1)
            res.append(nb)
        return res

    out["blocks"] = fix(blocks, 1)
    if bad:
        return None, ("Could not import this script: " + " ".join(bad[:3])
                      + " It may be from a newer version, or hand-edited.")
    chk = _studio_validate(out)
    if not chk["ok"]:
        return None, "Could not import this script: " + " ".join(chk["errors"][:3])
    return out, None


def _studio_tpl_block(counter, t, params=None, children=None):
    counter[0] += 1
    d = STUDIO_BLOCKS[t]
    b = {"id": "b%d" % counter[0], "type": t,
         "params": {p["key"]: p["default"] for p in d["params"]}}
    if params:
        b["params"].update(params)
    if t in STUDIO_CONTAINERS:
        b["children"] = children or []
    return b


def _studio_templates():
    """The shipped starting points. Fresh dicts every call (callers mutate)."""
    now = int(time.time())

    def mk(name, desc, blocks):
        return {"format": "ppscript", "version": 1, "name": name,
                "description": desc, "author": "", "created": now,
                "updated": now, "blocks": blocks, "settings": {}}
    c = [0]
    standard = mk(
        "Standard loop",
        "The macro's default cycle, rebuilt from blocks: dig until the pan "
        "is full, walk back into the water, glide and shake it empty, land, "
        "repeat.",
        [_studio_tpl_block(c, "comment",
                           {"text": "One pan per lap: dig, walk back, shake, land."}),
         _studio_tpl_block(c, "dig", {"hold_ms": 75}),
         _studio_tpl_block(c, "wait_cap",
                           {"state": "full", "timeout_ms": 1500,
                            "on_timeout": "continue"}),
         _studio_tpl_block(c, "wait_cue",
                           {"cue": "pan", "hold": "S", "fresh": False,
                            "timeout_ms": 1500, "on_timeout": "continue"}),
         _studio_tpl_block(c, "shake",
                           {"clicks": 0, "click_ms": 18, "gap_ms": 14,
                            "max_ms": 4000, "momentum_w": True}),
         _studio_tpl_block(c, "wait", {"ms": 150}),
         _studio_tpl_block(c, "wait_cue",
                           {"cue": "deposit", "hold": "W", "fresh": False,
                            "timeout_ms": 1500, "on_timeout": "continue"})])
    c = [0]
    treasure = mk(
        "Treasure (Rubble Creek)",
        "The Treasure mode two-step: dig the Rubble Creek deposit, strafe "
        "right to the sands until Collect shows, dig, strafe back. No "
        "shaking. Both spots run each lap.",
        [_studio_tpl_block(c, "comment",
                           {"text": "Stand on a Rubble Creek deposit with the "
                                    "Collect prompt showing before you start."}),
         _studio_tpl_block(c, "dig", {"hold_ms": 8}),
         _studio_tpl_block(c, "wait", {"ms": 12000}),
         _studio_tpl_block(c, "wait_cue",
                           {"cue": "deposit", "hold": "D", "fresh": True,
                            "timeout_ms": 6000, "on_timeout": "continue"}),
         _studio_tpl_block(c, "dig", {"hold_ms": 8}),
         _studio_tpl_block(c, "wait", {"ms": 12000}),
         _studio_tpl_block(c, "wait_cue",
                           {"cue": "deposit", "hold": "A", "fresh": True,
                            "timeout_ms": 6000, "on_timeout": "continue"})])
    blank = mk("Blank", "An empty canvas. Add blocks from the palette.", [])
    return [standard, treasure, blank]


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


# _roblox_rect / _window_origin moved engine-side at Phase 04 C8: the
# window lookup is prospector_engine.platform_mac/platform_win
# .find_roblox_window() (protocol 4.15 calibration.detectWindow). Zero
# app-side callers remained after the sensing delegation below.


# ============================================================================
# JS <-> Python bridge
# ============================================================================
# ---- runner identity (protocol 1.5) -----------------------------------------
# One durable GUID per data dir, shared with the engine: both read/mint the
# SAME <data dir>/instance_id file, so the publish acknowledgement this app
# stamps and the instanceId the engine carries in run.started are the same
# identity. Prospector Studio verifies them against each other.
_INSTANCE_ID_CACHE = {}


def _runner_instance_id():
    """Read-or-mint the durable instance GUID for the CURRENT data dir.
    The file always wins (the engine may have minted it first); an
    unwritable dir still gets a stable in-process GUID -- identity beats
    durability, and every consumer treats the field as optional."""
    d = DATA_DIR
    path = os.path.join(d, "instance_id")
    try:
        with open(path, encoding="utf-8") as f:
            val = f.read().strip()
        if val:
            _INSTANCE_ID_CACHE[d] = val
            return val
    except OSError:
        pass
    cached = _INSTANCE_ID_CACHE.get(d)
    if cached:
        return cached
    import uuid
    val = str(uuid.uuid4())
    _INSTANCE_ID_CACHE[d] = val
    try:
        os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(val + "\n")
        os.replace(tmp, path)
    except OSError:
        pass
    return val


_ENGINE_FP_CACHE = {}


def _engine_fingerprint():
    """The vendored engine's source fingerprint (sha256[:16] of
    prospector_engine/engine.py) -- the same value the engine reports as
    engine.sourceFingerprint in hello. Best-effort "" when the source is
    not readable (exotic frozen layouts)."""
    try:
        import prospector_engine
        path = os.path.join(os.path.dirname(prospector_engine.__file__),
                            "engine.py")
        st = os.stat(path)
        key = (path, st.st_mtime, st.st_size)
        hit = _ENGINE_FP_CACHE.get("fp")
        if hit and hit[0] == key:
            return hit[1]
        fp = hashlib.sha256(open(path, "rb").read()).hexdigest()[:16]
        _ENGINE_FP_CACHE["fp"] = (key, fp)
        return fp
    except Exception:
        return ""


class Api:
    def __init__(self):
        self.proc = None
        self.engine = None        # PPE1 EngineClient when ENGINE_IPC is on
        self._ipc = False         # current launch runs the --ipc protocol
        self._engine_paused = False
        self._synthetic_stop = False
        self._sct = None
        self._scale = 1.0
        self._last_stats = None   # latest stats from the macro (for history)
        self._script_block = None  # latest script.block step (script runs)
        self._script_hud = ""      # latest script hud_text line
        self._run_active = False  # a run is in progress (write history once)
        self._macro_status = "off"  # off/idle/running/paused/safe-pause/recovering/stopped
        self._run_mode = ""       # provenance: mode the current run started under
        self._run_rev = ""        # provenance: pushed revision it started from
        self._push_pending = False   # classic push deferred while a run is live
        self._engine_instance = None  # last engine.hello identity (1.5 mirror)
        self._classic_push_warned = False  # malformed push: log once, not per loop
        self._classic_toast = None   # applied-push toast queued until the UI is up
        self._studio_status_stop = threading.Event()
        # Trust wizard session state: real capability-test outcomes recorded
        # this session (never persisted -- an OS change would silently
        # invalidate them), a monotonic snapshot counter so the UI can drop
        # out-of-order refresh results, and a single-flight guard for the
        # Safe Stop listener.
        self._cap_tests = {}            # cap_id -> {status, detail, when}
        self._trust_seq = 0
        self._trust_lock = threading.Lock()
        # Diagnostics host state (chunk D2): rolling telemetry ctx, the
        # merged event store (recurrence), session dismissals, the 2 s
        # debounce cache and the cached window/health probe (populated by
        # the surfaces that already probe -- never a fresh grab here).
        self._diag_ctx_events = []      # rolling last-100 safety.event recs
        self._diag_event_counts = {}    # per-run safety.event type counts
        self._diag_launch_refusal = None
        self._diag_prior = []           # merged events (session store)
        self._diag_dismissed = {}       # code -> True (session dismissals)
        self._diag_cache = None         # (when, result)
        self._diag_health_cache = None  # {ok, reason, found, when}
        self._hotkey_armed = False
        self._welcome_lock = threading.Lock()
        # Prime the launch-time preflight snapshot NOW: if it were first
        # read lazily when a trust surface opens, a grant made before that
        # moment would be indistinguishable from a launch-time grant and
        # the restart-required inference would stay silent.
        try:
            lite_trust.launch_preflights()
        except Exception:
            pass
        if STUDIO_LAUNCH:
            # Live state mirror for the Prospector Studio window. A leftover
            # file from a previous session must never be read as current.
            try:
                os.remove(STATUS_FILE)
            except OSError:
                pass
            # Classic handoff: apply a pending Studio classic push at boot
            # (the 1 Hz loop keeps applying later ones).
            try:
                self._apply_classic_push()
            except Exception:
                pass
            self._studio_status_thread = threading.Thread(
                target=self._studio_status_loop, daemon=True)
            self._studio_status_thread.start()

    # ---- Studio-launch status mirror ----
    def _studio_push_info(self):
        """The build stamp Prospector Studio wrote at publish time (read-
        only here). {} when absent/unreadable."""
        info = _read_json(PUSH_FILE, None)
        return info if isinstance(info, dict) else {}

    def _studio_instance_info(self):
        """The runner-identity object for the status mirror (1.5): the
        live engine's hello identity when one has spoken this session,
        else the app-side truth -- the same durable GUID file, this
        process, the vendored engine source. Consumers must treat every
        field (and the whole object) as optional."""
        info = getattr(self, "_engine_instance", None)
        if isinstance(info, dict) and info.get("instanceId"):
            return dict(info)
        out = {"instanceId": _runner_instance_id(), "pid": os.getpid(),
               "exePath": sys.executable or "", "dataDir": DATA_DIR}
        fp = _engine_fingerprint()
        if fp:
            out["fingerprint"] = fp
        try:
            from prospector_engine import protocol as _proto
            out["protocol"] = {"major": _proto.PROTOCOL_MAJOR,
                               "minor": _proto.PROTOCOL_MINOR}
        except Exception:
            pass
        return out

    def _studio_push_ack(self, d, push):
        """The publish acknowledgement for the mirror (1.5): what this
        runner last APPLIED. Classic pushes come from the scripts-store
        meta (stamped by _apply_classic_push; a pre-1.5 stamp without
        appliedAt/instanceId is echoed as-is). Script/build publishes are
        applied by construction the moment the pushed entry is the active
        one -- acknowledge those with this runner's durable GUID. None
        when nothing was ever applied/acknowledged."""
        if push.get("kind") == "classic" or not push:
            a = (d["meta"].get("last_classic_push_ack")
                 or d["meta"].get("last_classic_push"))
            return dict(a) if isinstance(a, dict) and a.get("name") else None
        name = push.get("name")
        if isinstance(name, str) and name and name == d["active"]:
            return {"name": name, "rev": str(push.get("rev") or ""),
                    "at": push.get("at"),
                    "instanceId": _runner_instance_id()}
        return None

    # ---- classic handoff: Studio pushes a classic preset -------------------
    def _apply_classic_push(self):
        """Prospector Studio's classic-preset handoff (Track E, contract
        section 2). Studio writes the effective build into
        prospecting_builds.json and stamps studio_push.json with
        kind=="classic"; this app applies the stamp at boot and at 1 Hz:
        switch the top-level mode to CLASSIC, load_build (the existing
        merge semantics), record the applied stamp in the scripts-store
        meta, and toast. While a run is live the application is DEFERRED
        (push_pending in the status mirror) and re-checked each loop. A
        malformed push file is ignored and logged once -- never a retry
        storm, never a crash."""
        if not STUDIO_LAUNCH:
            return
        raw = _read_json(PUSH_FILE, None)
        if raw is None:
            if os.path.exists(PUSH_FILE) and not self._classic_push_warned:
                self._classic_push_warned = True
                print("[studio] push file unreadable; ignoring it")
            self._push_pending = False
            return
        if not isinstance(raw, dict) or raw.get("kind") != "classic":
            # a build/script publish stamp (or pre-kind file): not ours
            self._push_pending = False
            return
        name = raw.get("name")
        rev = raw.get("rev")
        if not isinstance(name, str) or not name.strip() \
                or not isinstance(rev, str) or not rev:
            if not self._classic_push_warned:
                self._classic_push_warned = True
                print("[studio] classic push stamp malformed; ignoring it")
            self._push_pending = False
            return
        stamp = {"name": name, "rev": str(rev), "at": raw.get("at")}
        d = _studio_load()
        if d["meta"].get("last_classic_push") == stamp:
            self._push_pending = False       # already applied, nothing new
            return
        if self.proc is not None:
            # a run is live: never yank its config -- surface the pending
            # update and re-check when the run ends (each loop pass)
            self._push_pending = True
            return
        builds = _read_json(BUILDS_FILE, {})
        if not isinstance(builds, dict) or (name not in builds
                                            and name not in DEFAULT_BUILDS):
            if not self._classic_push_warned:
                self._classic_push_warned = True
                print("[studio] classic push names a build that is not in "
                      "the library (%r); ignoring it" % name)
            self._push_pending = False
            return
        r = self.studio_mode("classic")      # clears active, restores tracker
        if not isinstance(r, dict) or not r.get("ok"):
            return                           # re-checked next loop pass
        if self.load_build(name) is None:
            return
        d = _studio_load()
        d["meta"]["last_classic_push"] = stamp
        # 1.5 publish acknowledgement: WHO applied it and WHEN, kept
        # BESIDE the dedup stamp (which stays exactly {name, rev, at} --
        # the applied-once comparison and older readers depend on that
        # shape). The mirror echoes this as its `ack` object.
        d["meta"]["last_classic_push_ack"] = dict(
            stamp, appliedAt=time.time(), instanceId=_runner_instance_id())
        try:
            _studio_write(d)
        except OSError:
            return
        self._push_pending = False
        self._classic_push_warned = False
        self._classic_toast = ('Loaded "%s" from Prospector Studio '
                               '— press Start (Ctrl+K)' % name)
        self._flush_classic_toast()

    def _flush_classic_toast(self):
        """The applied-push toast waits until the main window's JS is real:
        evaluate_js returns True only once window.toast exists, so a toast
        can never be dropped into a not-yet-loaded page."""
        t = getattr(self, "_classic_toast", None)
        if not t or _window is None:
            return
        try:
            ok = _window.evaluate_js(
                "(function(){if(!window.toast)return false;"
                "toast(%s);"
                "window.modeRefresh&&modeRefresh();"
                "window.stRefresh&&stRefresh();"
                "return true})()" % json.dumps(t))
            if ok:
                self._classic_toast = None
        except Exception:
            pass

    def _studio_status_snapshot(self):
        """Everything the Studio window mirrors, cheap enough for 1 Hz."""
        d = _studio_load()
        mode = _studio_ui_mode(d)
        push = self._studio_push_info()
        rev = str(push.get("rev") or "") if (
            d["active"] and push.get("name") == d["active"]) else ""
        st = self._last_stats or {}
        stats = {k: st[k] for k in ("cycles", "digs", "runtime_s",
                                    "pans_per_hr", "money_earned",
                                    "shards_earned", "recoveries")
                 if k in st}
        kind = (_studio_kind(d["scripts"].get(d["active"]))
                if d["active"] else "")
        # Script-run progress (the engine's script.block event) so the
        # Studio window can highlight the live node without polling.
        sb = getattr(self, "_script_block", None)
        # Declared-parameter values, so both windows show the same numbers
        # whichever side last edited them.
        pvals = {p["name"]: p["current"]
                 for p in _studio_params(
                     _studio_normalize(d["scripts"].get(d["active"])))} \
            if d["active"] else {}
        pending = bool(getattr(self, "_push_pending", False))
        return {"mode": mode, "active": d["active"], "rev": rev,
                "kind": kind,
                "run": getattr(self, "_macro_status", "off"),
                "stop_reason": str(st.get("stop_reason") or ""),
                "script": dict(sb) if isinstance(sb, dict) else None,
                "params": pvals,
                # shared run identity (engine meta run_id, Track C) and the
                # classic-handoff defer flag: Studio can show "update
                # pending" instead of wondering why nothing switched.
                "run_id": str(st.get("run_id") or ""),
                "push_pending": pending,
                # 1.5 runner identity + publish acknowledgement. Consumers
                # MUST treat these as optional (older mirrors lack them):
                # instance = which runner install answers, ack = the last
                # APPLIED push, ackPending/pendingRev = a deferred classic
                # push waiting for the live run to end.
                "instance": self._studio_instance_info(),
                "ack": self._studio_push_ack(d, push),
                "ackPending": pending,
                "pendingRev": str(push.get("rev") or "") if pending else "",
                "stats": stats}

    def _studio_status_loop(self):
        """Write STATUS_FILE atomically whenever the snapshot changes. The
        Studio window watches the file; the seq number lets it drop
        out-of-order or replayed reads, and ts marks freshness."""
        seq = 0
        last = None
        while not self._studio_status_stop.is_set():
            # Classic handoff (contract section 2): each pass applies a new
            # Studio classic push when idle, or re-checks a deferred one the
            # moment the run ends; the toast waits for the UI to exist.
            try:
                self._apply_classic_push()
                self._flush_classic_toast()
            except Exception:
                pass
            try:
                snap = self._studio_status_snapshot()
            except Exception:
                snap = None
            if snap is not None and snap != last:
                seq += 1
                body = dict(snap)
                body["v"] = 1
                body["seq"] = seq
                body["ts"] = time.time()
                tmp = STATUS_FILE + ".tmp"
                try:
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(body, f)
                    os.replace(tmp, STATUS_FILE)
                    last = snap
                except OSError:
                    pass
            self._studio_status_stop.wait(1.0)

    # ---- script-run telemetry fan-out (both transports) ----
    def _on_script_block(self, payload):
        """One script.block step from the engine: remember it (status
        mirror; late window loads) and fan it out to the Run-tab script
        card, the HUD window, and -- standalone Lite only -- the embedded
        editor's live block highlight."""
        if not isinstance(payload, dict):
            return
        self._script_block = payload
        js = json.dumps(payload)
        try:
            if _window is not None:
                _window.evaluate_js(
                    "window.setScriptStep&&setScriptStep(%s)" % js)
        except Exception:
            pass
        _hud_eval("window.hudScript&&hudScript(%s)" % js)
        _studio_eval("window.scriptStep&&scriptStep(%s)" % js)

    def _on_script_hud(self, text):
        """The script's own hud_text line: Run-tab script card + HUD."""
        self._script_hud = str(text or "")
        js = json.dumps(self._script_hud)
        try:
            if _window is not None:
                _window.evaluate_js(
                    "window.setScriptHud&&setScriptHud(%s)" % js)
        except Exception:
            pass
        _hud_eval("window.hudScriptHud&&hudScriptHud(%s)" % js)

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
        sd = _studio_load()
        ui_mode = _studio_ui_mode(sd)
        return {"values": merged, "running": self.proc is not None, "fr": fr, "hotkeys": hk,
                "v1": PRESET_V1, "v2": PRESET_V2, "geode": PRESET_GEODE, "defaults": DEFAULTS,
                "relics": relics, "relics_enabled": bool(saved.get("RELICS_ENABLED", False)),
                "builds": self.list_builds(), "pixels": pixels,
                "colors": saved.get("PIXEL_COLORS", {}),
                "region_previews": saved.get("REGION_PREVIEWS", {}),
                "autobuild": saved.get("AUTOBUILD", {}),
                "studio_launch": STUDIO_LAUNCH, "studio_script": STUDIO_SCRIPT,
                "studio_mode": ui_mode,
                "studio_kind": (_studio_kind(sd["scripts"].get(sd["active"]))
                                if sd["active"] else "")}

    # ---- tutorial + help content (owner-editable) ----
    def tutorial_content(self):
        """Everything the tour and the deep hover help render: the tour steps,
        per-setting help, media, and whether this machine may edit them."""
        try:
            return _tutorial_merged()
        except Exception as e:
            return {"tours": {}, "help": {}, "owner": False, "error": str(e)}

    # ---- main-tutorial lifecycle (history + auto-open on every entry) ----
    def tutorial_state(self):
        """The main tutorial's lifecycle history + everything the
        auto-open decision needs: `auto_open` (the TUTORIAL_AUTO_OPEN
        preference), `seen_count` / `last_seen_version` (viewing history),
        and `setup_finished` (informational -- since schema 3 it no
        longer gates the auto-open; `main` is the last outcome only)."""
        d = dict(_tutorial_lifecycle())
        try:
            d["setup_finished"] = _onboarding().finished()
        except Exception:
            d["setup_finished"] = False
        try:
            d["auto_open"] = _tutorial_auto_open_pref()
        except Exception:
            d["auto_open"] = True
        return d

    def tutorial_mark(self, state, legacy=False):
        """Persist a lifecycle transition (ACTIVE / COMPLETED / DISMISSED).
        NOT_STARTED is never accepted from the bridge -- the file records
        history, never a fabricated fresh install. ACTIVE increments
        seen_count and stamps last_seen_version (a viewing started).
        `legacy=True` records a migration from the pre-schema localStorage
        flag (honoured only from NOT_STARTED, so a live tour can never be
        overwritten by a fabricated migration). Atomic; failures are
        reported."""
        state = str(state or "")
        if state not in _TUT_STATES or state == "NOT_STARTED":
            return {"ok": False, "error": "Unknown tutorial state."}
        d = _tutorial_lifecycle()
        if legacy and d.get("main") != "NOT_STARTED":
            return {"ok": False,
                    "error": "Legacy migration only applies before the "
                             "tutorial ever ran."}
        d["main"] = state
        d["updated"] = int(time.time())
        if state == "ACTIVE":
            d["seen_count"] = int(d.get("seen_count", 0) or 0) + 1
            d["last_seen_version"] = VERSION
        if legacy:
            d["migrated_from"] = "localStorage pp_tour_done"
        ok, err = _tutorial_lifecycle_save(d)
        _wlog("tutorial_mark", status=state if ok else "error",
              detail=err)
        if not ok:
            return {"ok": False, "error": err,
                    "error_code": "PP-TUT-SAVE"}
        return {"ok": True, "main": state}

    def tutorial_set_auto_open(self, want):
        """Persist the 'open the tutorial whenever Prospector Lite opens'
        checkbox the moment it is toggled -- same engine-routed
        single-writer pattern as welcome_set_always_show, same
        report-and-revert contract on failure."""
        val = bool(want)
        with self._welcome_lock:
            try:
                ack = self._engine_settings_set(
                    {_TUTAUTO_KEY: val}, opaque=True)
            except Exception:
                ack = None
            if ack is not None:
                ok, err = bool(ack.get("ok")), ("engine refused the write"
                                                if not ack.get("ok") else "")
            else:
                ok, err = _set_tutorial_auto_open(val)
        _wlog("tutorial_auto_open", status="ok" if ok else "fail",
              code="" if ok else "PP-TUT-AUTO", detail=err)
        return {"ok": ok, "value": val,
                "error": err or None,
                "error_code": None if ok else "PP-TUT-AUTO"}

    def save_tutorial_entry(self, tid, patch):
        """Owner only: store an override for one tutorial card or help entry.
        Empty strings clear that field back to the default."""
        if not _is_owner():
            return {"ok": False, "error": "Not the owner machine."}
        if not isinstance(tid, str) or not tid.strip():
            return {"ok": False, "error": "Bad id."}
        if not isinstance(patch, dict):
            return {"ok": False, "error": "Bad patch."}
        data = _tutorial_local()
        over = data.setdefault("overrides", {})
        cur = over.setdefault(tid, {})
        for fld in ("title", "body", "img", "vid"):
            if fld in patch:
                v = str(patch.get(fld) or "").strip()
                if v:
                    cur[fld] = v
                else:
                    cur.pop(fld, None)
        if not cur:
            over.pop(tid, None)
        try:
            with open(TUTORIAL_FILE, "w") as f:
                json.dump(data, f, indent=1)
        except OSError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "content": _tutorial_merged()}

    def reset_tutorial_entry(self, tid):
        """Owner only: drop the override so the entry shows its default again."""
        if not _is_owner():
            return {"ok": False, "error": "Not the owner machine."}
        data = _tutorial_local()
        (data.get("overrides") or {}).pop(str(tid), None)
        try:
            with open(TUTORIAL_FILE, "w") as f:
                json.dump(data, f, indent=1)
        except OSError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "content": _tutorial_merged()}

    def pick_tutorial_image(self):
        """Owner only: pick an image file and return it as a data url the
        override can embed (ships inside the content, no hosting needed)."""
        if not _is_owner():
            return {"ok": False, "error": "Not the owner machine."}
        try:
            import webview
            import base64
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if _window is None:
            return {"ok": False, "error": "unavailable"}
        try:
            res = _window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=("Images (*.png;*.jpg;*.jpeg;*.gif;*.webp)",
                            "All files (*.*)"))
            if not res:
                return {"cancelled": True}
            path = res[0] if isinstance(res, (list, tuple)) else res
            with open(path, "rb") as f:
                raw = f.read()
            if len(raw) > 4 * 1024 * 1024:
                return {"ok": False,
                        "error": "Image is over 4 MB. Use a smaller screenshot."}
            ext = os.path.splitext(str(path))[1].lower().lstrip(".") or "png"
            if ext == "jpg":
                ext = "jpeg"
            b64 = base64.b64encode(raw).decode("ascii")
            return {"ok": True, "img": "data:image/%s;base64,%s" % (ext, b64)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def export_tutorials(self):
        """Owner only: write the combined overrides to a file that can ship
        as the packaged tutorial_content.json default. Local content only --
        Prospector Lite has no remote tutorial fetch."""
        if not _is_owner():
            return {"ok": False, "error": "Not the owner machine."}
        over = {}
        for src in (_tutorial_local(),):
            o = (src.get("overrides")
                 if isinstance(src.get("overrides"), dict) else src)
            for k, v in (o or {}).items():
                if isinstance(v, dict):
                    over.setdefault(k, {}).update(v)
        payload = {"overrides": over}
        try:
            import webview
        except Exception:
            webview = None
        try:
            if _window is not None and webview is not None:
                res = _window.create_file_dialog(
                    webview.SAVE_DIALOG, save_filename="tutorial_content.json",
                    file_types=("JSON file (*.json)", "All files (*.*)"))
                if not res:
                    return {"cancelled": True}
                path = res if isinstance(res, str) else res[0]
                with open(path, "w") as f:
                    json.dump(payload, f, indent=1)
                return {"ok": True, "path": str(path)}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": False, "error": "unavailable"}

    def test_webhook(self):
        """Send a real test notification through the exact path the engine
        uses (the user's own WEBHOOK_URL; nothing is baked into the app), so
        users can prove their Discord pings work before a long run."""
        saved = load_saved()
        url = str(saved.get("WEBHOOK_URL") or "").strip()
        user = str(saved.get("WEBHOOK_USER") or "").strip()
        if not url:
            return {"ok": False,
                    "error": "Add your own Discord webhook URL first (in the "
                             "box below), then send a test."}
        if not url.lower().startswith("https://"):
            return {"ok": False,
                    "error": "The webhook URL must start with https://"}
        msg = "🔔 Test notification: this is what a Prospector Lite alert looks like."
        fields = [{"name": "User", "value": (user or "(not set)")[:100],
                   "inline": True},
                  {"name": "Event", "value": "test", "inline": True}]
        payload = {"username": APP_NAME, "content": msg,
                   "embeds": [{"title": APP_NAME,
                               "description": msg, "color": 0xC2924C,
                               "fields": fields}],
                   "event": "test", "user": user, "stats": {}}
        try:
            import ssl as _ssl
            hdrs = {"Content-Type": "application/json",
                    "User-Agent": "ProspectorLite/1.0"}
            _sec = str(saved.get("WEBHOOK_SECRET") or "").strip()
            if _sec:
                hdrs["x-macro-secret"] = _sec
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"), headers=hdrs)
            try:
                # Certificate verification is mandatory: there is no retry
                # with verification off (see docs/trust-and-onboarding/
                # DISCORD_NOTIFICATIONS.md).
                urllib.request.urlopen(req, timeout=8, context=_tls_context())
            except Exception as e1:
                if getattr(e1, "code", None):
                    if e1.code == 401:
                        return {"ok": False, "error": "The bot rejected the notify "
                                "secret. The WEBHOOK_SECRET in this build must match "
                                "the bot's secret."}
                    return {"ok": False, "error": "HTTP %s from the notify bot" % e1.code}
                if isinstance(e1, _ssl.SSLError) or isinstance(
                        getattr(e1, "reason", None), _ssl.SSLError):
                    return {"ok": False, "error":
                            "The webhook server's TLS certificate could not be "
                            "verified, so nothing was sent. Check the URL; "
                            "Prospector Lite never disables certificate checks."}
                raise
            return {"ok": True, "user": user}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- app identity ----
    # There is deliberately NO auto-update mechanism: the app never contacts
    # any server on its own. Users update by downloading a new release
    # themselves (the welcome/About surfaces link to PROJECT_URL when set).
    def app_version(self):
        return VERSION

    def app_info(self):
        """Identity for the About/welcome surfaces. No network: version and
        build facts only, plus the public repository URL when configured."""
        info = {"name": APP_NAME, "version": VERSION,
                "platform": sys.platform, "frozen": FROZEN,
                "project_url": PROJECT_URL,
                "engine_fp": _engine_fingerprint(),
                "migrated": _MIGRATE_SUMMARY or ""}
        try:
            ident = lite_trust.build_identity(version=VERSION,
                                              project_url=PROJECT_URL)
            info["commit"] = ident["commit_short"]
            info["build_date"] = ident["date"]
            info["identity"] = ident
            if ident.get("project_url"):
                info["project_url"] = ident["project_url"]
        except Exception:
            pass
        return info

    def open_external(self, url):
        """Open a URL in the OS browser. Only ever called with a URL the
        user clicked; with no PROJECT_URL configured and no URL given it
        does nothing at all."""
        try:
            if not url:
                url = PROJECT_URL
            if not url:
                return False
            webbrowser.open(url)
            return True
        except Exception:
            return False

    def open_doc(self, name):
        """Open a bundled documentation file (privacy/security/readme) in
        the OS default viewer. Whitelisted names only; JS can never open an
        arbitrary path."""
        allowed = {"PRIVACY.md", "SECURITY.md", "README.md",
                   "THIRD_PARTY_NOTICES.md", "PERMISSIONS.md",
                   "CALIBRATION_GUIDE.md", "TRUST_CENTER.md",
                   "VERIFY_DOWNLOAD.md", "LICENSE_CHOICE_REQUIRED.md"}
        if name not in allowed:
            return False
        for base in (getattr(sys, "_MEIPASS", None), HERE,
                     os.path.dirname(HERE)):
            if not base:
                continue
            p = os.path.join(base, name)
            if os.path.isfile(p):
                try:
                    webbrowser.open("file://" + p)
                    return True
                except Exception:
                    return False
        return False

    # ---- welcome / onboarding ----
    # Prospector Lite has NO access code, licence check or account. The
    # welcome screen is onboarding only: it explains what the app does, what
    # it stores locally and which OS permissions it needs, then continues.
    def welcome_state(self):
        """Everything the welcome gate needs, including the persisted
        'show at every launch' preference so the checkbox always renders
        the stored value (never a template default)."""
        setup_needed = False
        try:
            setup_needed = not _onboarding().finished()
        except Exception:
            pass
        show_every = True
        try:
            show_every = _welcome_pref()
        except Exception:
            pass
        # `show` is the checkbox preference alone. A user mid-setup who
        # turned the welcome screen off resumes the wizard directly (boot()
        # routes on setup_needed/resume); a full wizard reset routes back
        # through the welcome gate via resume == NOT_STARTED.
        show = bool(show_every)
        if STUDIO_LAUNCH:
            show = False        # the Studio host owns its own onboarding
            setup_needed = False
        skip_auto = _skip_wizard_pref()
        wstate = "NOT_STARTED"
        try:
            wstate = _onboarding().state.get("state") or "NOT_STARTED"
        except Exception:
            pass
        # `route` is the single startup-routing authority (boot() acts on
        # it verbatim). The legacy show/setup_needed/resume fields stay
        # byte-compatible for older consumers and the packaged probes.
        route = lite_onboarding.compute_startup_route(
            explicit_welcome=False, studio_launch=STUDIO_LAUNCH,
            show_welcome_every_launch=bool(show_every),
            skip_wizard_automatically=bool(skip_auto),
            wizard_state=wstate, session_skip=False)
        _wlog("welcome_state", status="show=%s pref=%s setup=%s route=%s"
              % (show, show_every, setup_needed, route["route"]))
        return {"show": show, "show_every_launch": bool(show_every),
                "setup_needed": setup_needed,
                "resume": (_onboarding().state.get("state")
                           if setup_needed else ""),
                "route": route["route"],
                "skip_wizard_automatically": bool(skip_auto),
                "info": self.app_info()}

    def welcome_set_always_show(self, flag):
        """Persist the 'show this screen at every launch' checkbox the
        moment it is toggled. A failed write comes back as ok=False with
        the reason -- the UI shows it and reverts the checkbox instead of
        pretending the save happened. Serialized so rapid toggles resolve
        last-write-wins, and routed through the live ipc engine when one
        owns the config (C5 single-writer rule) so the engine's next full
        rewrite can not silently revert the preference."""
        want = bool(flag)
        with self._welcome_lock:
            try:
                ack = self._engine_settings_set(
                    {_WELCOME_KEY: want}, opaque=True)
            except Exception:
                ack = None
            if ack is not None:
                ok, err = bool(ack.get("ok")), ("engine refused the write"
                                                if not ack.get("ok") else "")
            else:
                ok, err = _set_welcome_pref(want)
        _wlog("welcome_set_always_show", status="ok" if ok else "fail",
              code="" if ok else "PP-WEL-SAVE", detail=err)
        return {"ok": ok, "value": want,
                "error": err or None,
                "error_code": None if ok else "PP-WEL-SAVE"}

    def wizard_skip_pref(self, flag):
        """Persist the 'skip the setup wizard automatically on launch'
        checkbox the moment it is toggled -- same engine-routed
        single-writer pattern as welcome_set_always_show, same
        report-and-revert contract on failure."""
        want = bool(flag)
        with self._welcome_lock:
            try:
                ack = self._engine_settings_set(
                    {_SKIPWIZ_KEY: want}, opaque=True)
            except Exception:
                ack = None
            if ack is not None:
                ok, err = bool(ack.get("ok")), ("engine refused the write"
                                                if not ack.get("ok") else "")
            else:
                ok, err = _set_skip_wizard_pref(want)
        _wlog("wizard_skip_pref", status="ok" if ok else "fail",
              code="" if ok else "PP-SKIP-SAVE", detail=err)
        return {"ok": ok, "value": want,
                "error": err or None,
                "error_code": None if ok else "PP-SKIP-SAVE"}

    def welcome_done(self, always_show=None):
        """Continue past the welcome screen. `always_show` is optional: the
        checkbox persists itself on toggle (welcome_set_always_show), so a
        plain Continue no longer rewrites the preference. The onboarding
        state machine is touched FIRST so the legacy WELCOME_SEEN bridge can
        never mis-read a value this same call wrote."""
        errs = []
        try:
            ob = _onboarding()
            ob.mark("WELCOME_COMPLETE")
            if ob.last_save_error:
                errs.append("state: %s" % ob.last_save_error)
        except Exception as e:
            errs.append("state: %s" % e)
        if always_show is not None:
            ok, err = _set_welcome_pref(bool(always_show))
            if not ok:
                errs.append("preference: %s" % err)
        _wlog("welcome_done", status="ok" if not errs else "fail",
              code="" if not errs else "PP-WEL-DONE",
              detail="; ".join(errs))
        if errs:
            return {"ok": False, "error": "; ".join(errs),
                    "error_code": "PP-WEL-DONE"}
        return {"ok": True}

    def wizard_skip(self, kind):
        """The Skip-wizard modal's three persistence contracts:
        'session' logs only (the skip lives in JS for this session);
        'mark_complete' records the wizard as reviewed via
        mark_completed_via (readiness warnings stay live-computed --
        nothing here fakes readiness); 'auto' sets the auto-skip
        preference through the engine-routed path and does NOT mark
        complete. launch()'s own calibration/permission gates are
        untouched by every branch."""
        k = str(kind or "")
        try:
            if k == "session":
                _wlog("wizard_skip", status="session")
                return {"ok": True}
            if k == "mark_complete":
                ob = _onboarding()
                st = ob.mark_completed_via("marked_complete")
                err = ob.last_save_error
                _wlog("wizard_skip", status="mark_complete",
                      code="" if not err else "PP-SKIP", detail=err)
                if err:
                    return {"ok": False, "error": err,
                            "error_code": "PP-SKIP"}
                return {"ok": True, "state": st.get("state")}
            if k == "auto":
                r = self.wizard_skip_pref(True)
                ok = bool(r.get("ok"))
                _wlog("wizard_skip", status="auto",
                      code="" if ok else "PP-SKIP",
                      detail=r.get("error") or "")
                if not ok:
                    return {"ok": False, "error": r.get("error"),
                            "error_code": "PP-SKIP"}
                return {"ok": True}
            _wlog("wizard_skip", status="fail", code="PP-SKIP",
                  detail="unknown kind: %s" % k)
            return {"ok": False, "error": "unknown skip kind",
                    "error_code": "PP-SKIP"}
        except Exception as e:
            _wlog("wizard_skip", status="fail", code="PP-SKIP",
                  detail=str(e))
            return {"ok": False, "error": str(e), "error_code": "PP-SKIP"}

    # ---- trust & permissions (registry-driven; see lite_trust.py) --------
    # Every status below comes from a real check; every test runs the real
    # capability; nothing here can trigger an OS prompt except the explicit
    # trust_request() the user clicks.

    def _record_test(self, cap_id, passed, detail=""):
        """Record a REAL capability-test outcome for this session. Session-
        scoped on purpose: an OS permission change would silently invalidate
        a persisted result, and 'status is never faked' includes never
        showing yesterday's pass as today's truth."""
        self._cap_tests[cap_id] = {
            "status": "passed" if passed else "failed",
            "detail": str(detail or "")[:300],
            "when": int(time.time()),
        }
        _wlog("capability_test", cap=cap_id,
              status="passed" if passed else "failed", detail=detail)

    def _restart_needed(self, cid, pre, at_launch):
        """Honest restart inference for one macOS capability. True only
        when evidence says the grant exists but can not work in THIS
        process: (a) the preflight flipped false->true after launch
        (Screen Recording / Input Monitoring apply at process start), or
        (b) the preflight says granted but the real capability test failed
        this session. A later PASSING test clears it."""
        t = self._cap_tests.get(cid)
        if t and t["status"] == "passed":
            return False
        if pre is True and at_launch is False and cid in (
                "screen_detection", "stop_hotkeys"):
            return True
        if pre is True and t and t["status"] == "failed":
            return True
        return False

    def trust_state(self):
        """Everything the Trust & Permissions screen and the Trust Center
        render: capability definitions + live statuses, platform, build
        identity, onboarding state and the data directory.

        Each OS capability additionally carries the authoritative state
        model: `requested` (the user explicitly asked at least once),
        `requires_restart` (grant exists but can not apply to this
        process), `test` (the real test outcome this session), and the
        snapshot carries `seq`/`checked_at` so the UI can drop stale
        refresh results."""
        try:
            saved = load_saved()
        except Exception:
            saved = {}
        caps = []
        statuses = lite_trust.capability_statuses(saved)
        at_launch = lite_trust.launch_preflights()
        try:
            requested = list(_onboarding().state.get("requested") or [])
        except Exception:
            requested = []
        plat = lite_trust.platform_key()
        for cap in lite_trust.CAPABILITIES:
            cid = cap["id"]
            c = dict(cap)
            c.pop("source_references", None)
            c["refs"] = [{"module": m, "symbol": s or "(module)", "why": w}
                         for (m, s, w) in cap["source_references"]]
            live = dict(statuses.get(cid,
                                     {"status": "unknown", "detail": ""}))
            if cid in ("screen_detection", "input_control", "stop_hotkeys"):
                live["requested"] = cid in requested
                live["test"] = self._cap_tests.get(cid)
                if plat == "mac":
                    pre = (live.get("status") == "granted")
                    live["requires_restart"] = self._restart_needed(
                        cid, pre, at_launch.get(cid))
                else:
                    live["requires_restart"] = False
            c["live"] = live
            caps.append(c)
        ob = _onboarding()
        with self._trust_lock:
            self._trust_seq += 1
            seq = self._trust_seq
        return {
            "platform": plat,
            "capabilities": caps,
            "identity": lite_trust.build_identity(version=VERSION,
                                                  project_url=PROJECT_URL),
            "onboarding": ob.state,
            "data_dir": DATA_DIR,
            "frozen": FROZEN,
            "seq": seq,
            "checked_at": int(time.time()),
            "dev_note": ("" if FROZEN else
                         "Running from source: macOS attributes these "
                         "permissions to your terminal/IDE, not to a "
                         "Prospector Lite app bundle."),
        }

    def trust_request(self, cap_id):
        """Trigger the real OS permission request -- called only from the
        clearly-labelled button on the capability card."""
        cap_id = str(cap_id or "")
        _wlog("trust_request", cap=cap_id, status="start")
        try:
            res = lite_trust.request_permission(cap_id)
        except Exception as e:
            res = {"ok": False, "error": str(e),
                   "error_code": "PP-TRUST-REQ"}
        try:
            ob = _onboarding()
            ob.note_request(cap_id)
            ob.mark("TRUST_STARTED")
        except Exception:
            pass
        _wlog("trust_request", cap=cap_id,
              status=("granted" if res.get("granted") else
                      ("ok" if res.get("ok") else "error")),
              detail=res.get("error", ""))
        return res

    def trust_open_settings(self, cap_id):
        """Open the exact System Settings pane for the capability. Also
        records the capability as 'requested': from the user's point of
        view they are now actively granting it, so a later false preflight
        must read as 'Not granted', never 'Not requested yet'."""
        cap_id = str(cap_id or "")
        try:
            _onboarding().note_request(cap_id)
        except Exception:
            pass
        res = lite_trust.open_settings(cap_id)
        _wlog("trust_open_settings", cap=cap_id,
              status="ok" if res.get("ok") else "error",
              detail=res.get("error", ""))
        return res

    def trust_test_screen(self):
        """The real screen-detection test: one small centre grab, size +
        non-blankness reported, preview shown once in-app, frame discarded.
        On macOS the first call also registers the app in the Screen
        Recording pane. The outcome is recorded as this session's test
        result and feeds the restart-required inference."""
        try:
            res = lite_trust.test_screen_capture(with_preview=True)
        except Exception as e:
            res = {"ok": False, "error": str(e)}
        passed = bool(res.get("ok") and res.get("nonblank"))
        self._record_test(
            "screen_detection", passed,
            res.get("note") or res.get("error") or "")
        return res

    def trust_test_key(self, request_id=0):
        """Arm the sandbox keyboard test: after a short delay one harmless
        key press+release is posted so the focused in-app field can observe
        both edges (down AND up = clean release proven). The worker's REAL
        result -- posted / refused-not-frontmost / error -- is delivered to
        the page via window.__keyTestResult, so a refusal is reported as
        itself instead of being mis-blamed on Accessibility."""
        rid = int(request_id or 0)

        def _run():
            try:
                res = lite_trust.post_test_key()
            except Exception as e:
                res = {"ok": False, "posted": False,
                       "error_code": "EXCEPTION", "error": str(e)}
            _wlog("input_key_post", cap="input_control",
                  status="posted" if res.get("posted") else "refused",
                  code=res.get("error_code", ""),
                  detail=res.get("error", ""))
            try:
                if _window is not None:
                    _window.evaluate_js(
                        "window.__keyTestResult && __keyTestResult(%s)"
                        % json.dumps({"id": rid, "result": res}))
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "armed": True, "id": rid}

    def trust_test_pointer(self):
        """2-pixel pointer wiggle, verified by reading the cursor position
        back. No clicks, nothing sent to any app. A FAILURE is recorded
        immediately (real evidence input is blocked); a success alone is
        NOT recorded -- 'Keyboard & mouse control' passes only when the
        keyboard half passes too (trust_record_input)."""
        try:
            res = lite_trust.test_pointer_wiggle()
        except Exception as e:
            res = {"ok": False, "error": str(e)}
        if not (res.get("ok") and res.get("moved")):
            self._record_test("input_control", False,
                              res.get("note") or res.get("error")
                              or "pointer did not move")
        return res

    def trust_record_input(self, request_id, observed):
        """The sandbox test's composite verdict, reported by the page: the
        keyboard half (down AND up observed) plus the pointer half. Only a
        run whose key post actually happened counts -- a frontmost-guard
        refusal proves nothing either way and records nothing."""
        o = observed if isinstance(observed, dict) else {}
        if not o.get("posted"):
            _wlog("input_test", cap="input_control", status="not_run",
                  detail="key post refused/skipped; nothing recorded")
            return {"ok": True, "recorded": False}
        passed = bool(o.get("down") and o.get("up")
                      and o.get("pointer_moved"))
        detail = ("key down+up observed, pointer moved" if passed else
                  "down=%s up=%s pointer=%s" % (bool(o.get("down")),
                                                bool(o.get("up")),
                                                bool(o.get("pointer_moved"))))
        self._record_test("input_control", passed, detail)
        return {"ok": True, "recorded": True, "passed": passed}

    def trust_test_hotkey(self, timeout=8, request_id=0):
        """Arm the Safe Stop test: listen (only) for Esc / Ctrl+K for a few
        seconds and report back through window.__hotkeyResult. Single-
        flight: while one listener is armed, another request is refused
        instead of spawning a second listener whose stale result would
        overwrite the first."""
        rid = int(request_id or 0)
        with self._trust_lock:
            if self._hotkey_armed:
                return {"ok": False, "armed": False, "busy": True,
                        "error": "A Safe Stop test is already running -- "
                                 "wait for it to finish.",
                        "error_code": "PP-HOTKEY-BUSY"}
            self._hotkey_armed = True

        def _run():
            try:
                res = lite_trust.await_stop_hotkey(
                    timeout=float(timeout or 8))
            except Exception as e:
                res = {"ok": False, "heard": None, "error": str(e),
                       "error_code": "EXCEPTION"}
            finally:
                with self._trust_lock:
                    self._hotkey_armed = False
            # A clean timeout (armed fine, the user simply pressed nothing)
            # proves nothing and records nothing; only a heard key (pass)
            # or a listener failure (fail) is real evidence.
            if res.get("heard"):
                self._record_test("stop_hotkeys", True, res.get("heard"))
            elif res.get("error"):
                self._record_test("stop_hotkeys", False, res.get("error"))
            res["id"] = rid
            try:
                if _window is not None:
                    _window.evaluate_js(
                        "window.__hotkeyResult && __hotkeyResult(%s)"
                        % json.dumps(res))
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "armed": True, "id": rid,
                "keys": "Esc or Ctrl+K"}

    def trust_relaunch(self):
        """Quit and reopen Prospector Lite -- the honest remedy when a
        permission was granted after this process started (macOS applies
        Screen Recording / Input Monitoring at launch). Spawns the fresh
        copy first, then runs the normal clean quit."""
        _wlog("trust_relaunch", status="start")
        try:
            if FROZEN and sys.platform == "darwin":
                # .../Prospector Lite.app/Contents/MacOS/exe -> the .app
                bundle = os.path.dirname(os.path.dirname(
                    os.path.dirname(sys.executable)))
                if bundle.endswith(".app"):
                    subprocess.Popen(["open", "-n", bundle])
                else:
                    subprocess.Popen([sys.executable])
            elif FROZEN:
                subprocess.Popen([sys.executable])
            else:
                subprocess.Popen([sys.executable]
                                 + [os.path.abspath(sys.argv[0])]
                                 + sys.argv[1:])
        except Exception as e:
            _wlog("trust_relaunch", status="error", detail=str(e))
            return {"ok": False, "error": str(e),
                    "error_code": "PP-RELAUNCH"}
        return self.quit_app()

    def trust_manifest(self):
        """The build's trust manifest: per-capability source references at
        the exact build commit (bundled at package time; generated live
        from source in dev runs)."""
        return lite_trust.load_manifest(version=VERSION,
                                        project_url=PROJECT_URL)

    def trust_view_code(self, cap_id, ref_index=0):
        """Open the exact-commit source URL for a capability reference, or
        return the local file + symbol when no public repository URL is
        configured (an honest local fallback -- never a moving branch)."""
        man = self.trust_manifest()
        for cap in man.get("capabilities", []):
            if cap.get("id") != cap_id:
                continue
            refs = cap.get("references", [])
            try:
                ref = refs[int(ref_index)]
            except (IndexError, ValueError, TypeError):
                return {"ok": False, "error": "no such reference"}
            url = ref.get("url") or ""
            if url:
                self.open_external(url)
                return {"ok": True, "opened": url}
            loc = "%s :: %s (lines %s-%s) @ commit %s" % (
                ref.get("file"), ref.get("symbol"),
                ref.get("line_start"), ref.get("line_end"),
                (man.get("generated_from") or "unknown")[:12])
            return {"ok": True, "opened": "", "local": loc,
                    "file": ref.get("file"), "symbol": ref.get("symbol"),
                    "note": "No public repository URL is configured in "
                            "this build, so the exact local reference is "
                            "shown instead."}
        return {"ok": False, "error": "unknown capability"}

    def webhook_payload_preview(self):
        """The EXACT notification payload a run event would send, built by
        the same engine code that sends it (prospector_engine.engine.
        _webhook_payload), with example stats. Secrets are not included --
        the optional x-macro-secret header is redacted by design."""
        try:
            from prospector_engine import engine as _ppe
            saved = load_saved()
            user = str(saved.get("WEBHOOK_USER") or "")
            old = _ppe.WEBHOOK_USER
            try:
                _ppe.WEBHOOK_USER = user
                payload = _ppe._webhook_payload(
                    "stats", "Example: 120 pans, 96/hr",
                    {"cycles": 120, "pans_per_hr": 96,
                     "runtime_s": 4500, "recoveries": 1})
            finally:
                _ppe.WEBHOOK_USER = old
            hdrs = {"Content-Type": "application/json",
                    "User-Agent": "ProspectorLite/1.0"}
            if str(saved.get("WEBHOOK_SECRET") or "").strip():
                hdrs["x-macro-secret"] = "(your secret -- never shown or "\
                                         "logged)"
            return {"ok": True, "payload": payload, "headers": hdrs,
                    "url_set": bool(str(saved.get("WEBHOOK_URL")
                                        or "").strip()),
                    "enabled": bool(saved.get("WEBHOOK_ENABLED")),
                    "screenshot_optin":
                        bool(saved.get("NOTIFY_SCREENSHOT"))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- onboarding state machine (lite_onboarding.py) -------------------

    def onboarding_state(self):
        return _onboarding().state

    def onboarding_mark(self, state, via=None):
        """Advance the wizard (forward-only; rerun/reset go backward).
        `via` stamps completion_via when marking FINISHED (the wizard's
        own completion passes 'wizard'; the Skip-wizard modal's
        mark-complete path goes through wizard_skip instead)."""
        s = str(state or "")
        if s == "FINISHED" and via:
            return _onboarding().mark_completed_via(str(via))
        return _onboarding().mark(s)

    def onboarding_decline(self, cap_id, declined=True):
        return _onboarding().decline_optional(str(cap_id or ""),
                                              bool(declined))

    def onboarding_rerun(self):
        """Reopen the full wizard from the Trust step (Help / Trust
        Center). Nothing is deleted."""
        return _onboarding().rerun()

    def onboarding_reset(self):
        """Reset ONLY the wizard progress -- builds, calibration, settings
        and history are untouched."""
        return _onboarding().reset()

    # ---- guided calibration (drives the SAME engine + save path as the
    # Calibrate tab; the registry lives in lite_onboarding.py) -------------

    def _advcue_migration_backup(self, cfg):
        """One-time safety net when an install that FINISHED setup under the
        old schema meets the new Advanced-cue-matching requirement: snapshot
        the config before the user recalibrates anything, and remember the
        review in the onboarding state. Values are never modified or deleted
        here -- this only preserves what already exists."""
        try:
            ob = _onboarding()
            if not ob.finished():
                return
            if ob.state.get("advcue_review"):
                return
            _, missing = lite_onboarding.cue_masks_state(cfg)
            if not missing:
                return
            bak = CONFIG_FILE + ".pre-advcue.bak"
            if not os.path.isfile(bak) and os.path.isfile(CONFIG_FILE):
                import shutil
                shutil.copyfile(CONFIG_FILE, bak)
            ob.state["advcue_review"] = {"when": int(time.time()),
                                         "missing": missing,
                                         "backup": bak}
            ob._save()
        except Exception:
            pass

    def _cap_repair_backup(self):
        """One-time safety net before a capacity save REPLACES a stored
        pair that fails the suspicion check (lite_onboarding.
        cap_pair_suspicion): snapshot the config once to
        CONFIG_FILE + '.pre-caprepair.bak' so the pre-repair values stay
        recoverable. Values are never modified or deleted here -- the
        _advcue_migration_backup pattern."""
        try:
            cfg = load_saved()
            if not lite_onboarding.cap_pair_suspicion(cfg):
                return
            bak = CONFIG_FILE + ".pre-caprepair.bak"
            if not os.path.isfile(bak) and os.path.isfile(CONFIG_FILE):
                import shutil
                shutil.copyfile(CONFIG_FILE, bak)
        except Exception:
            pass

    def calibration_registry(self):
        """Registry items + live statuses + sequential progression for the
        guided-calibration step and the Trust Center."""
        cfg = load_saved()
        health = None
        found = None
        s = _sensing()
        if s is not None:
            try:
                health = s.health()
            except Exception:
                health = None
            try:
                d = s.detect_window()
                # find_roblox_window returns {found: False, error: ...} on
                # failure -- a NON-empty dict, so bool(d) would be a
                # permanent false "found". Read the actual key.
                found = bool(d.get("found")) if isinstance(d, dict) else None
            except Exception:
                found = None
            try:
                self._diag_health_cache = {
                    "ok": bool((health or {}).get("ok", True)),
                    "reason": str((health or {}).get("reason", "")),
                    "found": found, "when": time.time()}
            except Exception:
                pass
        try:
            setup_finished = _onboarding().finished()
        except Exception:
            setup_finished = False
        self._advcue_migration_backup(cfg)
        return lite_onboarding.compose_registry(
            cfg, health=health, window_found=found,
            setup_finished=setup_finished,
            owner=bool(_is_owner() and not FROZEN))

    # ---- calibration example screenshots (assets/onboarding/calibration) --
    # Honest pipeline: an example image is shown ONLY when a real, owner-
    # approved capture exists in the shipped asset manifest. Until then the
    # wizard shows a clearly-labelled pending note -- never a fabricated
    # screenshot. The owner capture tool below runs only on the owner's dev
    # checkout (never in packaged builds).

    def _example_manifest_path(self):
        return os.path.join(_resource(os.path.join("assets", "onboarding",
                                                   "calibration")),
                            "manifest.json")

    def calibration_example(self, item_id):
        """The example image + annotations for a wizard item, as a data URL
        (webviews cannot load arbitrary file paths). Placeholder when no
        approved asset exists."""
        item_id = str(item_id or "")
        base = os.path.dirname(self._example_manifest_path())
        man = _read_json(self._example_manifest_path(), {})
        entry = (man.get("items") or {}).get(item_id)
        if not isinstance(entry, dict):
            return {"placeholder": True, "alt": ""}
        out = {"alt": entry.get("alt", ""),
               "annotations": entry.get("annotations") or []}
        rel = entry.get("file") or ""
        path = os.path.join(base, rel) if rel else ""
        if entry.get("approved") and rel and os.path.isfile(path) \
                and os.path.realpath(path).startswith(
                    os.path.realpath(base) + os.sep):
            try:
                import base64
                with open(path, "rb") as f:
                    out["img"] = ("data:image/png;base64,"
                                  + base64.b64encode(f.read()).decode())
                out["placeholder"] = False
                return out
            except OSError:
                pass
        out["placeholder"] = True
        if rel and not entry.get("approved"):
            out["pending_review"] = True
        return out

    def owner_example_capture(self, item_id):
        """OWNER + dev checkout only: capture the current screen as the raw
        example for `item_id`. Saved un-approved; the owner crops/redacts
        the PNG (any editor), then approves it. Never available in packaged
        builds, so end users can neither see nor trigger this."""
        if FROZEN or not _is_owner():
            return {"ok": False, "error": "Owner dev tool only."}
        item_id = str(item_id or "")
        mp = self._example_manifest_path()
        man = _read_json(mp, {})
        if item_id not in (man.get("items") or {}):
            return {"ok": False, "error": "Unknown item."}
        try:
            import mss
            import mss.tools
            with mss.mss() as sct:
                img = sct.grab(sct.monitors[1])
                rel = os.path.join("common", "%s.png" % item_id)
                dest = os.path.join(os.path.dirname(mp), rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                mss.tools.to_png(img.rgb, (img.width, img.height),
                                 output=dest)
            man["items"][item_id]["file"] = rel
            man["items"][item_id]["approved"] = False
            tmp = mp + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(man, f, indent=1)
            os.replace(tmp, mp)
            return {"ok": True, "path": dest,
                    "note": "Saved UN-approved. Crop/redact the PNG, then "
                            "approve it. It ships only after approval."}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def owner_example_approve(self, item_id, approved=True):
        """OWNER + dev checkout only: mark an example as reviewed/approved
        (or revoke approval)."""
        if FROZEN or not _is_owner():
            return {"ok": False, "error": "Owner dev tool only."}
        mp = self._example_manifest_path()
        man = _read_json(mp, {})
        entry = (man.get("items") or {}).get(str(item_id or ""))
        if not isinstance(entry, dict):
            return {"ok": False, "error": "Unknown item."}
        entry["approved"] = bool(approved)
        tmp = mp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(man, f, indent=1)
        os.replace(tmp, mp)
        return {"ok": True, "approved": bool(approved)}

    # ---- readiness check (Step 4) ----------------------------------------

    def readiness_check(self):
        """Run every readiness probe and return a readable checklist.
        Required failures block Start Macro (launch() enforces the same
        conditions) but never block the app itself."""
        plat = lite_trust.platform_key()
        items = []

        def add(iid, title, status, detail, fix=""):
            items.append({"id": iid, "title": title, "status": status,
                          "detail": detail, "fix": fix})

        saved = load_saved()
        caps = lite_trust.capability_statuses(saved)
        for cid, title in (("screen_detection", "Screen detection"),
                           ("input_control", "Keyboard & mouse control"),
                           ("stop_hotkeys", "Safe Stop hotkeys")):
            st = caps.get(cid, {}).get("status")
            if plat == "mac":
                if st == "granted":
                    add(cid, title, "pass", "Permission granted.")
                elif st == "not_granted":
                    add(cid, title, "fail",
                        "macOS has not granted this permission.",
                        fix="trust")
                else:
                    add(cid, title, "warn",
                        "Could not read the permission state.",
                        fix="trust")
            else:
                t = self._cap_tests.get(cid)
                if t and t["status"] == "passed":
                    add(cid, title, "pass",
                        "Verified by your own test this session.")
                elif t and t["status"] == "failed":
                    add(cid, title, "warn",
                        "Your test failed this session: %s"
                        % (t["detail"] or "see the Trust tab."),
                        fix="trust")
                else:
                    add(cid, title, "info",
                        "No OS permission needed; use the Trust tab tests "
                        "to prove it works.", fix="trust")
        # Roblox window (informational -- Roblox may simply not be open)
        s = _sensing()
        found = None
        if s is not None:
            try:
                d = s.detect_window()
                found = bool(d.get("found")) if isinstance(d, dict) else None
            except Exception:
                found = None
        if found is True:
            add("roblox", "Roblox window", "pass", "Roblox window found.")
        else:
            add("roblox", "Roblox window", "info",
                "Roblox is not open right now -- open it before starting "
                "a run.")
        # Required calibration
        reg = self.calibration_registry()
        if reg["ready"]:
            detail = ("Required items are user-calibrated."
                      if not reg["auto_calibrate"] else
                      "Auto-calibration covers the required items; manual "
                      "calibration makes them exact.")
            add("calibration", "Required calibration", "pass", detail,
                fix="calibration")
        else:
            add("calibration", "Required calibration", "fail",
                "Required items need attention: %s"
                % ", ".join(reg["blockers"]), fix="calibration")
        # Advanced cue matching -- the required primary prompt detector.
        # Its own row so the requirement is visible even inside the
        # aggregate. Derived from the SAME registry status the aggregate
        # row uses, so the two rows can never contradict (a stale window,
        # for example, fails both with the same re-capture guidance).
        cm_live = next((i["live"] for i in reg["items"]
                        if i["id"] == "cue_masks"), {})
        cm_st = cm_live.get("status")
        if cm_st == "ok":
            add("cue_masks", "Advanced cue matching", "pass",
                cm_live.get("detail", "All three prompt masks are "
                                      "captured."), fix="calibration")
        elif cm_st == "needs_review":
            add("cue_masks", "Advanced cue matching", "fail",
                cm_live.get("detail", "Masks need review."),
                fix="calibration")
        elif cm_st == "stale":
            # masks ARE captured -- only the re-capture guidance applies
            add("cue_masks", "Advanced cue matching", "fail",
                cm_live.get("detail", "Masks captured, but the window "
                                      "changed -- re-capture the three "
                                      "prompts."), fix="calibration")
        else:
            missing_note = cm_live.get(
                "detail", "Capture the three prompt masks from the "
                          "guided calibration step.")
            add("cue_masks", "Advanced cue matching", "fail",
                "Required: %s Existing single-pixel values are kept as "
                "a fallback but do not count as ready." % missing_note,
                fix="calibration")
        # Data directory write probe
        try:
            probe = os.path.join(DATA_DIR, ".write_probe")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
            add("datadir", "Data folder", "pass",
                "Writable: %s" % DATA_DIR)
        except OSError as e:
            add("datadir", "Data folder", "fail",
                "Cannot write to %s (%s)" % (DATA_DIR, e))
        # Settings save + reload probe: exercises the same atomic
        # write-then-read machinery on a DEDICATED probe file, so it can
        # never race a real settings save or leave residue in the config.
        try:
            probe_path = os.path.join(DATA_DIR, ".settings_probe.json")
            marker = int(time.time())
            tmp = probe_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"probe": marker}, f)
            os.replace(tmp, probe_path)
            back = _read_json(probe_path, {}).get("probe")
            os.remove(probe_path)
            if back == marker:
                add("settings", "Settings save & reload", "pass",
                    "Atomic write + read-back verified.")
            else:
                add("settings", "Settings save & reload", "fail",
                    "The saved value did not read back.")
        except Exception as e:
            add("settings", "Settings save & reload", "fail", str(e))
        # Build identity
        ident = lite_trust.build_identity(version=VERSION,
                                          project_url=PROJECT_URL)
        if ident["commit"] != "unknown":
            add("identity", "Build identity", "pass",
                "v%s @ %s%s" % (ident["version"], ident["commit_short"],
                                " (modified source)" if ident["dirty"]
                                else ""))
        else:
            add("identity", "Build identity", "warn",
                "No build stamp found (source run outside git?).")
        # Network defaults
        url = str(saved.get("WEBHOOK_URL") or "").strip()
        if not url and not saved.get("WEBHOOK_ENABLED"):
            add("network", "Network defaults", "pass",
                "No network features enabled. Normal use is fully "
                "offline.")
        else:
            add("network", "Network defaults", "info",
                "Discord notifications are configured (your own webhook). "
                "Everything else is offline.")
        add("platform", "Platform", "info",
            "%s / %s%s" % (ident["os"], ident["arch"],
                           "" if FROZEN else " (running from source)"))
        result = {"items": items,
                  "ok": not any(i["status"] == "fail" for i in items),
                  "when": int(time.time())}
        try:
            ob = _onboarding()
            ob.record_readiness({"ok": result["ok"],
                                 "when": result["when"],
                                 "fails": [i["id"] for i in items
                                           if i["status"] == "fail"]})
        except Exception:
            pass
        return result

    def quit_app(self):
        """User-clicked Exit from the wizard. Same teardown as closing the
        window (inputs released, engine stopped)."""
        def _later():
            time.sleep(0.2)
            try:
                _quit_everything(self)
            except Exception:
                pass
            os._exit(0)
        threading.Thread(target=_later, daemon=True).start()
        return {"ok": True}

    # ---- local data management (Trust Center) ----------------------------

    _DATA_FILES = (
        ("prospecting_config.json", "Settings + calibration (the config)"),
        ("prospecting_builds.json", "Saved builds"),
        ("prospecting_scripts.json", "Studio scripts library"),
        ("run_history.json", "Run history"),
        ("tutorial_content.json", "Tutorial/help overrides"),
        ("prospecting_secrets.json",
         "Local secrets (Coach API key) -- never exported"),
        ("onboarding_state.json", "Setup wizard progress"),
        ("tutorial_state.json", "Tutorial viewing history"),
        ("diagnostics_state.json",
         "Diagnostics history + suppressions (local only)"),
        ("onboarding.log", "Setup wizard + diagnostics action log"),
        ("onboarding.log.1", "Setup wizard log (rotated)"),
        ("coach_history.json", "Coach chat transcript"),
        ("prospecting_calib_log.csv", "Calibration session log"),
        ("studio_macro_status.json", "Studio live-status mirror"),
        ("studio_push.json", "Studio publish handshake"),
        ("instance_id", "Engine instance identity (random local id)"),
        (".migrated_from_prospectors_plus", "One-time migration marker"),
    )

    def data_manifest(self):
        """What actually lives in the data folder, with sizes -- the Trust
        Center's Local Data table. Only known Prospector Lite files are
        listed or ever touched by the delete actions."""
        out = []
        for name, why in self._DATA_FILES:
            p = os.path.join(DATA_DIR, name)
            if os.path.exists(p):
                try:
                    size = os.path.getsize(p)
                except OSError:
                    size = 0
                out.append({"name": name, "purpose": why, "bytes": size})
        logdir = os.path.join(DATA_DIR, "run_logs")
        if os.path.isdir(logdir):
            try:
                n = len(os.listdir(logdir))
                size = sum(os.path.getsize(os.path.join(logdir, f))
                           for f in os.listdir(logdir))
            except OSError:
                n, size = 0, 0
            out.append({"name": "run_logs/",
                        "purpose": "Full logs of past runs (%d files)" % n,
                        "bytes": size})
        return {"dir": DATA_DIR, "files": out}

    def open_data_folder(self):
        try:
            if sys.platform == "darwin":
                subprocess.run(["open", DATA_DIR], check=False, timeout=10)
            elif os.name == "nt":
                os.startfile(DATA_DIR)  # noqa: attribute exists on Windows
            else:
                subprocess.run(["xdg-open", DATA_DIR], check=False,
                               timeout=10)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def delete_local_data(self, kind):
        """Scoped deletion, known files only, never a recursive wipe of an
        arbitrary path. kinds: history | logs | wizard | secrets | all.
        The UI double-confirms 'all'."""
        global _ONBOARD
        kind = str(kind or "")
        removed = []

        def rm(rel):
            p = os.path.join(DATA_DIR, rel)
            try:
                if os.path.isfile(p):
                    os.remove(p)
                    removed.append(rel)
            except OSError:
                pass

        def rm_logs():
            d = os.path.join(DATA_DIR, "run_logs")
            if os.path.isdir(d):
                for f in os.listdir(d):
                    try:
                        os.remove(os.path.join(d, f))
                        removed.append("run_logs/" + f)
                    except OSError:
                        pass

        if kind == "history":
            rm("run_history.json")
            rm_logs()
        elif kind == "logs":
            rm_logs()
        elif kind == "wizard":
            # Reset IN PLACE (a fresh NOT_STARTED file) rather than deleting:
            # a deleted file would let the legacy WELCOME_SEEN bridge re-mark
            # the wizard finished, so it would never reopen.
            try:
                _onboarding().reset()
                removed.append("onboarding_state.json (reset)")
            except Exception:
                pass
        elif kind == "secrets":
            rm("prospecting_secrets.json")
        elif kind == "all":
            for name, _why in self._DATA_FILES:
                rm(name)
            rm_logs()
            for extra in ("prospecting_config.json.bak",
                          "prospecting_scripts.json.bak"):
                rm(extra)
            _ONBOARD = None
        else:
            return {"ok": False, "error": "unknown kind"}
        return {"ok": True, "removed": len(removed)}

    _JS_ERRS = 0

    def log_js_error(self, message, source="", line=0):
        """window.onerror / unhandledrejection forwarder: JavaScript-side
        failures land in the onboarding log instead of dying invisibly
        inside the webview. Rate-limited; content is the error text only
        (no page content, no secrets)."""
        if Api._JS_ERRS > 200:
            return {"ok": False}
        Api._JS_ERRS += 1
        _wlog("js_error", status="error",
              detail="%s (%s:%s)" % (str(message)[:200],
                                     str(source)[-80:], line))
        return {"ok": True}

    def diag_summary(self):
        """A short copyable text summary for support requests: identity,
        capability statuses, session test results, last readiness verdict.
        NO secrets, NO paths beyond the data dir, NO config dump."""
        try:
            ident = lite_trust.build_identity(version=VERSION,
                                              project_url=PROJECT_URL)
            caps = lite_trust.capability_statuses(load_saved())
            lines = ["%s v%s (%s, %s, %s)" % (
                APP_NAME, ident.get("version"), ident.get("commit_short"),
                ident.get("os"), "packaged" if FROZEN else "source")]
            for cid in ("screen_detection", "input_control", "stop_hotkeys",
                        "discord_notifications", "coach_ai"):
                st = caps.get(cid, {})
                t = self._cap_tests.get(cid)
                lines.append("%s: %s%s" % (
                    cid, st.get("status", "?"),
                    (" | test %s (%s)" % (t["status"],
                                          time.strftime("%H:%M:%S",
                                          time.localtime(t["when"])))
                     if t else "")))
            try:
                lr = _onboarding().state.get("last_readiness")
                if lr:
                    lines.append("last readiness: ok=%s fails=%s"
                                 % (lr.get("ok"), lr.get("fails")))
            except Exception:
                pass
            return {"ok": True, "text": "\n".join(lines)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_wizard_log(self):
        """Open the onboarding log in the OS default viewer."""
        try:
            if not os.path.isfile(_WIZLOG_FILE):
                _wlog("log_opened")
            if sys.platform == "darwin":
                subprocess.run(["open", _WIZLOG_FILE], check=False,
                               timeout=10)
            elif os.name == "nt":
                os.startfile(_WIZLOG_FILE)  # noqa
            else:
                subprocess.run(["xdg-open", _WIZLOG_FILE], check=False,
                               timeout=10)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def export_diagnostics(self):
        """Save a diagnostics summary (readiness + identity + statuses +
        the onboarding log tail). Contains NO secrets: no webhook URL, no
        API key, no config dump, no screenshots, no keystroke content."""
        try:
            ident = lite_trust.build_identity(version=VERSION,
                                              project_url=PROJECT_URL)
            payload = {
                "app": APP_NAME, "identity": ident,
                "capabilities": lite_trust.capability_statuses(
                    load_saved()),
                "session_tests": self._cap_tests,
                "readiness": self.readiness_check(),
                "calibration": {
                    k: v["live"]["status"] if isinstance(v, dict) else v
                    for k, v in
                    ((i["id"], i) for i in
                     self.calibration_registry()["items"])},
                "data_dir": DATA_DIR,
                "onboarding_log_tail": _wizlog_tail(),
            }
            try:
                import webview
            except Exception:
                webview = None
            if _window is not None and webview is not None:
                res = _window.create_file_dialog(
                    webview.SAVE_DIALOG,
                    save_filename="prospector-lite-diagnostics.json",
                    file_types=("JSON file (*.json)", "All files (*.*)"))
                if not res:
                    return {"cancelled": True}
                path = res if isinstance(res, str) else res[0]
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=1)
                return {"ok": True, "path": str(path)}
            return {"ok": False, "error": "unavailable"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- diagnostics center (chunk D2): warnings, apply/undo, FAQ --------

    def _diag_ctx(self):
        """Assemble the lite_diagnostics evaluate() ctx from state that
        already exists in the host: last stats frame, per-run event
        counts, the rolling telemetry window, calibration status with
        live health (exactly as readiness computes it), capability
        statuses, merged config values, mode/build, the cached
        window-found probe (NEVER a fresh grab here) and the last typed
        launch refusal."""
        cfg = load_saved()
        settings = dict(DEFAULTS)
        settings.update({k: v for k, v in cfg.items() if k in DEFAULTS})
        # live health exactly as readiness does (window-rect compare, no
        # screen grab); window_found only from the cached probe
        health = {"ok": True, "reason": ""}
        s = _sensing()
        if s is not None:
            try:
                h = s.health()
                if isinstance(h, dict):
                    health = {"ok": bool(h.get("ok", True)),
                              "reason": str(h.get("reason", ""))}
            except Exception:
                pass
        hc = getattr(self, "_diag_health_cache", None) or {}
        found = hc.get("found") if isinstance(hc, dict) else None
        try:
            setup_finished = _onboarding().finished()
        except Exception:
            setup_finished = False
        try:
            cal_status = lite_onboarding.calibration_status(
                cfg, health=health, window_found=found,
                setup_finished=setup_finished)
        except Exception:
            cal_status = {}
        caps = {}
        try:
            for cid, st in (lite_trust.capability_statuses(cfg)
                            or {}).items():
                caps[cid] = str((st or {}).get("status", ""))
        except Exception:
            pass
        # a failed session hotkey test is definitive user-visible evidence
        # (rule N reads 'test_failed'); a definitive not_granted wins
        t = (self._cap_tests or {}).get("stop_hotkeys")
        if (t and t.get("status") == "failed"
                and caps.get("stop_hotkeys") != "not_granted"):
            caps["stop_hotkeys"] = "test_failed"
        try:
            sd = _studio_load()
            mode = _studio_ui_mode(sd)
            build_active = bool(sd.get("active"))
        except Exception:
            mode, build_active = "classic", False
        return {
            "platform": lite_trust.platform_key(),
            "stats": dict(self._last_stats) if self._last_stats else None,
            "event_counts": dict(getattr(self, "_diag_event_counts", {})
                                 or {}),
            "recent_events": list(getattr(self, "_diag_ctx_events", [])
                                  or []),
            "cal_status": cal_status,
            "cal_health": health,
            "capabilities": caps,
            "settings": settings,
            "mode": mode,
            "build_active": build_active,
            "window_found": found,
            "launch_refusal": getattr(self, "_diag_launch_refusal", None),
            "run_active": bool(getattr(self, "_run_active", False)),
        }

    def diagnostics_state(self, force=False):
        """The warning surface: evaluate the rules over the live ctx,
        merge with the session store (recurrence/escalation), apply
        stored suppressions and session dismissals, and summarize for
        the badges. Debounced: recomputed at most every 2 s."""
        now = time.time()
        cached = getattr(self, "_diag_cache", None)
        if not force and cached and now - cached[0] < 2.0:
            return cached[1]
        if lite_diagnostics is None:
            return {"events": [], "summary": _diag_summarize([]),
                    "when": int(now)}
        ctx = self._diag_ctx()
        try:
            fresh = lite_diagnostics.evaluate(ctx)
        except Exception:
            fresh = []
        if ctx.get("mode") == "classic":
            blocker = _diag_blocker_event(ctx.get("cal_status"))
            if blocker is not None and not any(
                    e.get("code") == blocker["code"] for e in fresh):
                fresh.append(blocker)
        try:
            merged = lite_diagnostics.merge_events(
                getattr(self, "_diag_prior", []), fresh, int(now))
        except Exception:
            merged = fresh
        self._diag_prior = merged
        store = _diag_store_load()
        try:
            visible = lite_diagnostics.apply_suppressions(
                merged, store.get("suppressions"), now)
        except Exception:
            visible = list(merged)
        # session dismissals hide a code while it persists; once the code
        # clears, the dismissal is forgotten so a recurrence shows again
        dis = getattr(self, "_diag_dismissed", None)
        if dis is None:
            dis = self._diag_dismissed = {}
        live_codes = set(e.get("code") for e in merged)
        for code in list(dis):
            if code not in live_codes:
                del dis[code]
        visible = [e for e in visible
                   if not (e.get("code") in dis
                           and e.get("dismissible", True))]
        result = {"events": visible, "summary": _diag_summarize(visible),
                  "when": int(now)}
        self._diag_cache = (now, result)
        return result

    def setting_locator(self):
        """{key: {control:'cycle'|'tab', tab, section}} from the
        lite_diagnostics registry -- the JS deep-link layer resolves
        placement from this, never from text matching."""
        out = {}
        if lite_diagnostics is not None:
            for k, e in (lite_diagnostics.SETTING_REGISTRY or {}).items():
                out[k] = {"control": e.get("control", "tab"),
                          "tab": e.get("tab", ""),
                          "section": e.get("section", "")}
        return out

    def diag_apply(self, payload):
        """Apply ONE recommended setting value. Server-side re-validation
        against the lite_diagnostics registry: non-registry keys and
        auto_apply-unsafe keys are rejected (PP-DIAG-APPLY); the value is
        clamped into RANGES. Writes through the SAME single-writer path
        as save_config, snapshots {key, prev, next} for undo, and
        refreshes the UI fields. One apply = exactly one key."""
        key = str((payload or {}).get("key") or "")
        entry = (lite_diagnostics.SETTING_REGISTRY.get(key)
                 if lite_diagnostics is not None else None)
        if not entry or not entry.get("safe_auto_apply") or key not in TYPES:
            _wlog("diag_apply", cap=key, status="rejected",
                  code="PP-DIAG-APPLY",
                  detail="not a registry auto-apply key")
            return {"ok": False, "error_code": "PP-DIAG-APPLY",
                    "error": "This setting cannot be applied "
                             "automatically -- open it instead."}
        suggested = (payload or {}).get("suggested")
        if not isinstance(suggested, (int, float)) \
                or isinstance(suggested, bool):
            _wlog("diag_apply", cap=key, status="rejected",
                  code="PP-DIAG-APPLY", detail="non-numeric suggestion")
            return {"ok": False, "error_code": "PP-DIAG-APPLY",
                    "error": "The suggested value is not numeric."}
        clamped = lite_diagnostics.clamp_suggestion(key, suggested)
        cur = load_saved()
        prev = cur.get(key, DEFAULTS.get(key))
        try:
            n = self.save_config({key: clamped})
        except OSError as e:
            _wlog("diag_apply", cap=key, status="error",
                  code="PP-DIAG-APPLY", detail=str(e))
            return {"ok": False, "error_code": "PP-DIAG-APPLY",
                    "error": "Could not save: %s" % e}
        if not n:
            return {"ok": False, "error_code": "PP-DIAG-APPLY",
                    "error": "Nothing was written."}
        apply_id = "apply-%d" % int(time.time() * 1000)
        store = _diag_store_load()
        store["applied"].append({"id": apply_id, "key": key, "prev": prev,
                                 "next": clamped,
                                 "when": int(time.time())})
        _diag_store_save(store)
        _wlog("diag_apply", cap=key, status="ok",
              detail="%s -> %s" % (prev, clamped))
        self._diag_cache = None
        try:
            if _window is not None:
                _window.evaluate_js(
                    "window.refreshValues && window.refreshValues()")
        except Exception:
            pass
        return {"ok": True, "id": apply_id, "key": key,
                "prev": prev, "next": clamped}

    def diag_undo(self, apply_id):
        """Restore the pre-apply value of ONE apply record via the same
        single-writer path, then drop the record (one level per
        record)."""
        store = _diag_store_load()
        rec = next((r for r in store["applied"]
                    if isinstance(r, dict) and r.get("id") == apply_id),
                   None)
        if rec is None:
            return {"ok": False, "error": "Unknown apply id."}
        key = rec.get("key")
        if key not in TYPES:
            return {"ok": False, "error": "Unknown setting."}
        try:
            self.save_config({key: rec.get("prev")})
        except OSError as e:
            _wlog("diag_undo", cap=key, status="error",
                  code="PP-DIAG-APPLY", detail=str(e))
            return {"ok": False, "error": "Could not save: %s" % e}
        store["applied"] = [r for r in store["applied"]
                            if not (isinstance(r, dict)
                                    and r.get("id") == apply_id)]
        _diag_store_save(store)
        _wlog("diag_undo", cap=key, status="ok",
              detail="restored %s" % (rec.get("prev"),))
        self._diag_cache = None
        try:
            if _window is not None:
                _window.evaluate_js(
                    "window.refreshValues && window.refreshValues()")
        except Exception:
            pass
        return {"ok": True, "key": key, "restored": rec.get("prev")}

    def diag_dismiss(self, event_id):
        """Hide one event for this session (while its code persists).
        Recorded in the store history; CRITICAL/dismissible=False events
        refuse."""
        ev = next((e for e in getattr(self, "_diag_prior", [])
                   if e.get("id") == event_id or e.get("code") == event_id),
                  None)
        if ev is None:
            return {"ok": False, "error": "Unknown event."}
        if not ev.get("dismissible", True):
            return {"ok": False, "error": "This event cannot be dismissed."}
        self._diag_dismissed[ev.get("code")] = True
        store = _diag_store_load()
        store["history"].append({"code": ev.get("code"),
                                 "when": int(time.time()),
                                 "action": "dismissed"})
        _diag_store_save(store)
        self._diag_cache = None
        return {"ok": True}

    def diag_suppress(self, code):
        """Never show this code again (persisted). CRITICAL events are
        never suppressible (apply_suppressions enforces it too)."""
        code = str(code or "")
        ev = next((e for e in getattr(self, "_diag_prior", [])
                   if e.get("code") == code), None)
        if ev is not None and (not ev.get("suppressible", True)
                               or ev.get("severity") == "CRITICAL"):
            return {"ok": False,
                    "error": "This warning cannot be turned off."}
        store = _diag_store_load()
        store["suppressions"][code] = {"forever": True, "until": None}
        store["history"].append({"code": code, "when": int(time.time()),
                                 "action": "suppressed"})
        _diag_store_save(store)
        self._diag_cache = None
        return {"ok": True}

    def diag_unsuppress_all(self):
        store = _diag_store_load()
        store["suppressions"] = {}
        _diag_store_save(store)
        self._diag_cache = None
        return {"ok": True}

    def faq_list(self):
        return {"entries": (lite_diagnostics.FAQ_ENTRIES
                            if lite_diagnostics is not None else [])}

    def faq_entry(self, faq_id):
        for e in (lite_diagnostics.FAQ_ENTRIES
                  if lite_diagnostics is not None else []):
            if e.get("id") == faq_id:
                return e
        return None

    def save_pixels(self, pixels, colors=None, fr=None):
        """Save calibrated pixel coordinates. [Phase 04 C8] The semantic
        calibration write -- pixel persistence, CAP_BAR_WIDTH derivation,
        window rect/origin + PIXEL_RATIOS capture, the FR/autopan group
        and the AUTO_CALIBRATE / WINDOW_RELATIVE force -- runs in the
        engine's one implementation (protocol 4.15 savePixels). A
        capacity-pair rejection (ok:False + reasons; nothing written,
        previous values retained) is forwarded verbatim so every caller
        can show the exact reasons; successful saves keep the legacy
        "saved" result."""
        s = _sensing()
        if s is None:
            return _SENSING_ERR
        r = s.save_pixels(pixels, colors=colors, fr=fr)
        if isinstance(r, dict) and r.get("ok") is False:
            return r
        return "saved"

    # ---- calibration export / import ----
    def _calibration_dict(self):
        cur = load_saved()
        keys = ["CAP_FULL_PIXEL", "CAP_LEFT_PIXEL", "DEPOSIT_PIX", "PAN_PIX",
                "SHAKE_PIX", "DIG_TRIGGER_PIXEL"]
        return {"app": APP_NAME, "kind": "calibration",
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
        px = data.get("pixels") or {}
        keys = ["CAP_FULL_PIXEL", "CAP_LEFT_PIXEL", "DEPOSIT_PIX", "PAN_PIX",
                "SHAKE_PIX", "DIG_TRIGGER_PIXEL"]
        applied = {}
        for k in keys:
            v = px.get(k)
            if isinstance(v, (list, tuple)) and len(v) == 2:
                applied[k] = [int(v[0]), int(v[1])]
        if not applied:
            return {"ok": False, "error": "No pixel data in that file."}
        s = _sensing()
        if s is None:
            return {"ok": False, "error": _SENSING_ERR}
        # [Phase 04 C8] the import-path derivations (explicit ratio/rect
        # adoption, CAP_BAR_WIDTH from the merged ends or the file value,
        # colors) run in the engine's one calibration writer. Import does
        # NOT force the auto-calibrate flags -- shipped semantics.
        ratios = (data["PIXEL_RATIOS"]
                  if isinstance(data.get("PIXEL_RATIOS"), dict) else None)
        wrect = (list(data["CALIB_WINDOW_RECT"])
                 if isinstance(data.get("CALIB_WINDOW_RECT"), (list, tuple))
                 else None)
        colors = (data["PIXEL_COLORS"]
                  if isinstance(data.get("PIXEL_COLORS"), dict) else None)
        r = s.save_pixels(applied, colors=colors, ratios=ratios,
                          window_rect=wrect,
                          cap_bar_width_fallback=data.get("CAP_BAR_WIDTH"),
                          derive_from_window=False)
        # the one calibration writer validates capacity pairs -- a
        # rejected import wrote nothing, so say so (reasons verbatim)
        if isinstance(r, dict) and r.get("ok") is False:
            return {"ok": False,
                    "error": " ".join(r.get("reasons") or [])
                             or "The calibration file failed validation.",
                    "reasons": r.get("reasons") or []}
        return {"ok": True, "pixels": applied,
                "colors": load_saved().get("PIXEL_COLORS", {})}

    def run_log(self, name):
        # Return the saved full log for a past run (run_logs/<name>).
        try:
            if not name or ".." in name or os.path.basename(name) != name:
                return {"error": "bad name"}
            path = os.path.join(os.path.dirname(CONFIG_FILE), "run_logs", name)
            if not os.path.isfile(path):
                return {"error": "log not found (older runs may not have one)"}
            with open(path) as f:
                return {"text": f.read()}
        except Exception as e:
            return {"error": str(e)}

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
            b = (base or "").strip()
            if b and not _coach_base_ok(b):
                return {"ok": False,
                        "error": "The base URL must be https:// (plain "
                                 "http:// is allowed only for localhost, "
                                 "e.g. a local Ollama). Your API key must "
                                 "never travel unencrypted."}
            s["COACH_BASE_URL"] = b
        s["COACH_API_KEY"] = ""                       # never store the key in config
        if key is not None:
            _save_coach_key(key)                      # secrets file only
        _config_write(s)
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
        if base and not _coach_base_ok(base):
            # defence in depth: a hand-edited config must not put the API
            # key on an unencrypted wire (save-time validation is primary)
            return {"reply": "The Coach base URL must be https:// (plain "
                             "http:// only for localhost). Fix it in Coach "
                             "settings (⚙).",
                    "changes": [], "topic": "api", "askStats": False,
                    "chips": []}
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

        def _do():
            # Verified TLS only -- an unreachable or badly-certified endpoint
            # is an error, never a reason to weaken certificate checks.
            with urllib.request.urlopen(req, timeout=40,
                                        context=_tls_context()) as r:
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
                hint = ", the API key was rejected. Check it in Coach settings (⚙)."
            elif e.code == 404:
                hint = ", that model name isn't on your key. Try another in ⚙."
            elif e.code == 429:
                hint = ", rate/credit limit hit on your account."
            return {"reply": "%s API error %d%s %s" % (provname, e.code, hint, detail),
                    "changes": [], "topic": "api", "askStats": False, "chips": []}
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
                             "Everything else still works, you can tune settings "
                             "manually in the tabs.", "changes": [], "topic": "",
                    "askStats": False, "chips": []}
        if stats:                              # remember stats the user gives us
            try:
                cur = load_saved()
                cur["COACH_STATS"] = {k: stats[k] for k in stats}
                _config_write(cur)
            except Exception:
                pass
        ctx = self._coach_context(stats)
        ctx["prev_topic"] = prev_topic or ""
        if load_saved().get("COACH_MODE") == "api":
            try:
                res = self._coach_api(message or "", ctx, convo)
            except Exception as e:
                res = {"reply": "API mode error (%s), using the offline brain." % e,
                       "changes": [], "topic": "", "askStats": False, "chips": []}
            if res is not None:                # None means: no key set -> fall through
                return res
        try:
            return _coach.respond(message or "", ctx)
        except Exception as e:
            return {"reply": "Sorry, I hit an error working that out (%s). Try "
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
        """For the UI: report whether the Roblox window is found and where.
        [Phase 04 C8] engine-side lookup (4.15 detectWindow)."""
        s = _sensing()
        if s is None:
            return {"found": False, "error": _SENSING_ERR}
        return s.detect_window()

    def auto_calibrate(self):
        """Place every pixel automatically from the detected Roblox window using
        the saved ratio profile. No clicking required. [Phase 04 C8] the
        placement math runs engine-side (4.15 auto); persistence keeps the
        manual-save semantics exactly as before (via save_pixels)."""
        s = _sensing()
        if s is None:
            return {"ok": False, "error": _SENSING_ERR}
        rect = s.detect_window()
        if not rect.get("found"):
            return {"ok": False, "error": rect.get("error", "Roblox not found")}
        r = s.auto(apply=False)
        if not r.get("placed") or not r.get("pixels"):
            return {"ok": False, "needs_manual": True, "window": rect,
                    "error": "No calibration profile yet. Calibrate the spots "
                             "once with Roblox open (just this first time) and "
                             "Auto-calibrate will handle it from then on."}
        pixels = r["pixels"]
        # persist them exactly like a manual save (incl. bar width)
        self.save_pixels(pixels)
        return {"ok": True, "pixels": pixels, "window": rect,
                "count": len(pixels)}

    def sample_pixels(self):
        """Live readout for the Calibrate 'Test detection' tool. [Phase 04
        C8] sampling runs engine-side with the ENGINE's own is_white /
        is_yellow thresholds (4.15 sampleSaved -- the same 175/140/45
        values this handler previously inlined); this maps the verb
        result back onto the legacy JS shape."""
        s = _sensing()
        if s is None:
            return {"error": _SENSING_ERR}
        r = s.sample_saved()
        if "error" in r:
            return {"error": r["error"]}
        res = {"pixels": r.get("pixels", {})}
        if "capFull" in r:
            res["cap_full"] = r["capFull"]
        for k, v in (r.get("whites") or {}).items():
            res[k + "_white"] = v
        if not res["pixels"]:
            # Nothing saved to sample. On a fresh install with
            # auto-calibration on that is EXPECTED (pixels are placed at
            # run start from the profile) -- say so instead of dumping an
            # empty JSON blob that reads as a failure.
            auto = bool(load_saved().get("AUTO_CALIBRATE", True))
            res["empty"] = True
            res["note"] = (
                "No saved calibration points to sample yet. Auto-"
                "calibration is ON: the required points are placed "
                "automatically each time a run starts, so this is normal "
                "for a fresh install. Calibrate by hand to pin exact "
                "pixels you can sample here." if auto else
                "No saved calibration points to sample. Auto-calibration "
                "is OFF, so calibrate the required points first.")
        return res

    def cap_bar_review(self):
        """The Calibrate tab's banner check for the capacity pair: the
        stored-pair suspicion detail (the needs_review migration,
        lite_onboarding.cap_pair_suspicion) or empty. Pure config read --
        cheap enough for the tab's 8 s health poll."""
        try:
            return {"detail":
                    lite_onboarding.cap_pair_suspicion(load_saved()) or ""}
        except Exception:
            return {"detail": ""}

    def test_capacity(self):
        """Test Capacity Calibration: one fresh grab evaluated with the
        exact RUNTIME capacity math (right-tip is_yellow over the 6x6
        runtime box, fill fraction over the cap_fill band, pair
        validation) plus an annotated preview of the bar region. Routed
        to the same in-process engine Sensing every calibration verb
        uses (capacity_probe)."""
        s = _sensing()
        if s is None:
            return {"ok": False, "tip_yellow": False, "tip_hex": "",
                    "fill_frac": 0.0, "preview": "",
                    "reasons": [_SENSING_ERR]}
        try:
            return s.capacity_probe()
        except Exception as e:
            return {"ok": False, "tip_yellow": False, "tip_hex": "",
                    "fill_frac": 0.0, "preview": "",
                    "reasons": ["The capacity probe failed: %s" % e]}

    def test_find_read(self):
        """OCR the find pop-up region ONCE and show raw lines + what parsed,
        instant calibration check, no run needed. [Phase 04 C8] the grab +
        OCR run through the ENGINE's Vision path (4.15 testRead) -- this
        removed the app-side duplicate of the engine's finds OCR."""
        s = _sensing()
        if s is None:
            return {"error": _SENSING_ERR}
        return s.test_read("find")

    def test_earn_read(self):
        """Run the earnings OCR ONCE on the calibrated money/shards regions
        and return what it sees -- instant verification, no run needed.
        [Phase 04 C8] engine-side OCR (4.15 testRead), exact legacy
        fallback strings preserved in the one implementation."""
        s = _sensing()
        if s is None:
            return {"money": _SENSING_ERR, "shards": _SENSING_ERR}
        return s.test_read("earnings")

    def webhook_get(self):
        try:
            return {"url": str(load_saved().get("WEBHOOK_URL") or "")}
        except Exception:
            return {"url": ""}

    def _engine_settings_set(self, values, opaque=False):
        """C5 single-writer rule: while an ipc engine is alive it owns the
        config file, so writes go through settings.set/setOpaque. Returns
        the ack, or None when no live engine (caller writes directly --
        today's behavior, safe because no concurrent writer exists)."""
        if not (self._ipc and self.engine is not None
                and self.engine.alive()):
            return None
        cmd = "settings.setOpaque" if opaque else "settings.set"
        return self.engine.request(cmd, {"values": values})

    def webhook_set(self, url):
        try:
            val = str(url or "").strip()
            if val and not val.lower().startswith("https://"):
                return {"ok": False,
                        "error": "The webhook URL must start with https://"}
            ack = self._engine_settings_set({"WEBHOOK_URL": val})
            if ack is not None:
                return {"ok": bool(ack.get("ok"))}
            cur = load_saved()
            cur["WEBHOOK_URL"] = val
            _config_write(cur)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # Pages whose settings apply to ANY run (the engine checks them in its
    # control loop or notification path, classic cycle and Studio scripts
    # alike). Every other settings page tunes the classic cycle, so its keys
    # are CLASSIC-owned. Studio-owned state (active build, CLASSIC | STUDIO
    # mode, editor flags) lives in the script-library file, never here.
    _SHARED_PAGES = ("Notifications", "Auto-stop", "Window", "Earnings")

    def settings_ownership(self):
        """Explicit owner for every tuning key: {'classic': [...],
        'shared': [...]}. Computed from the settings schema so new keys
        always land in a group (unlisted pages default to classic)."""
        classic, shared = set(), set()
        for title, rows in SECTIONS:
            dst = shared if title in self._SHARED_PAGES else classic
            for row in rows:
                dst.add(row[0])
        shared.add("WEBHOOK_URL")          # set via its own field, same owner
        classic.update(("RELICS", "RELICS_ENABLED"))
        return {"classic": sorted(classic), "shared": sorted(shared)}

    def settings_reset(self, group, include_calibration=False):
        """Reset one ownership group to shipped defaults. 'classic' and
        'shared' restore config keys from the schema defaults; calibration
        pixels and keybinds are precious (machine-specific), so 'shared'
        touches them only when explicitly asked. 'studio' clears the active
        build, mode, remembered build and editor flags but NEVER deletes
        scripts. Config is rewritten in one pass -- a failed write leaves
        the file as it was."""
        if group == "studio":
            d = _studio_load()
            d["active"] = ""
            d["mode"] = "classic"
            d["last_active"] = ""
            d["meta"] = {}
            restore = d.get("classic_tracker")
            d["classic_tracker"] = None
            try:
                _studio_write(d)
                self._studio_push_active("")
                if restore is not None:
                    # back to CLASSIC: un-park the AutoPan-Tracking choice
                    _config_patch({"TRACKER_MODE": restore})
            except OSError as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True,
                    "reset": ["active build", "mode", "editor flags"]}
        own = self.settings_ownership()
        if group not in own:
            return {"ok": False, "error": "Unknown settings group."}
        cur = load_saved()
        hit = []
        for k in own[group]:
            if k == "RELICS":
                if cur.get("RELICS") != DEFAULT_RELICS:
                    cur["RELICS"] = list(DEFAULT_RELICS)
                    hit.append(k)
            elif k == "WEBHOOK_URL":
                if cur.get("WEBHOOK_URL"):
                    cur["WEBHOOK_URL"] = ""
                    hit.append(k)
            elif k in DEFAULTS:
                if k in cur and cur[k] != DEFAULTS[k]:
                    hit.append(k)
                cur[k] = DEFAULTS[k]
        if group == "shared":
            for k in _HK_DEFAULTS:
                if cur.get(k, _HK_DEFAULTS[k]) != _HK_DEFAULTS[k]:
                    hit.append(k)
                cur[k] = _HK_DEFAULTS[k]
            if include_calibration:
                for k, v in PIXEL_DEFAULTS.items():
                    if list(cur.get(k, v)) != list(v):
                        hit.append(k)
                    cur[k] = list(v)
        if group == "classic":
            # TRACKER_MODE just went back to its default, so the value STUDIO
            # parked (see studio_mode) must not outlive it.
            d = _studio_load()
            if d.get("classic_tracker") is not None:
                d["classic_tracker"] = None
                try:
                    _studio_write(d)
                except OSError:
                    pass
        vals = {k: cur[k] for k in hit if k in TYPES}
        ack = self._engine_settings_set(vals) if vals else None
        if ack is None or not ack.get("ok"):
            try:
                _config_write(cur)
            except OSError as e:
                return {"ok": False, "error": str(e)}
        else:
            # engine owned the scalar keys; write the non-scalar leftovers
            # (relics, pixels, hotkeys, webhook url) through the file
            rest = {k: cur[k] for k in hit if k not in TYPES}
            if rest:
                base = load_saved()
                base.update(rest)
                try:
                    _config_write(base)
                except OSError as e:
                    return {"ok": False, "error": str(e)}
        return {"ok": True, "reset": sorted(hit)}

    def save_config(self, data):
        """Write scalar settings, PRESERVING relic keys already in the file."""
        vals = {k: _coerce(t, data[k]) for k, t in TYPES.items() if k in data}
        ack = self._engine_settings_set(vals) if vals else None
        if ack is not None and ack.get("ok"):
            return len(vals)
        cur = load_saved()
        cur.update(vals)
        _config_write(cur)
        return len(vals)

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
        ack = self._engine_settings_set({"RELICS": clean,
                                         "RELICS_ENABLED": bool(enabled)})
        if ack is not None and ack.get("ok"):
            return len(clean)
        cur["RELICS"] = clean
        cur["RELICS_ENABLED"] = bool(enabled)
        _config_write(cur)
        return len(clean)

    def set_window_compact(self, on):
        """Resize the native window for compact / normal mode."""
        try:
            if _window is not None:
                if on:
                    _window.resize(740, 820)
                else:
                    _window.resize(1340, 900)
        except Exception:
            pass
        return {"ok": True}

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
                _config_write(cur)
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
        _config_write(cur)
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

    def calibration_health(self):
        """Compare the LIVE Roblox window to the size it was calibrated at.
        [Phase 04 C8] the check + exact message composition run
        engine-side (4.15 health). The result (plus a window lookup --
        cheap, never a screen grab) feeds the diagnostics ctx cache, so
        the UI's existing 8 s tick keeps diagnostics_state honest without
        any probe of its own."""
        s = _sensing()
        if s is None:
            return {"ok": True, "reason": ""}
        h = s.health()
        try:
            found = None
            d = s.detect_window()
            if isinstance(d, dict):
                found = bool(d.get("found"))
            self._diag_health_cache = {
                "ok": bool((h or {}).get("ok", True)),
                "reason": str((h or {}).get("reason", "")),
                "found": found, "when": time.time()}
        except Exception:
            pass
        return h

    def set_advanced_cues(self, on):
        cur = load_saved()
        cur["ADVANCED_CUES"] = bool(on)
        try:
            _config_write(cur)
        except OSError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def set_cue_masks_only(self, on):
        cur = load_saved()
        cur["CUE_MASKS_ONLY"] = bool(on)
        try:
            _config_write(cur)
        except OSError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    # ---- build sharing: export / import / file attachments -------------------
    def export_build(self, name):
        """Write ONE build (settings + description + any attached file) to a
        .ppbuild file via the OS save dialog, so it can be shared with a friend."""
        entry = _builds_all().get(name)
        if not isinstance(entry, dict):
            return {"ok": False, "error": "Build not found."}
        payload = {"_ppbuild": 1, "app": APP_NAME, "name": name,
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
                            "Studio script (*.ppscript)", "All files (*.*)"))
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

    # ---- PROSPECTOR STUDIO: script library Api ------------------------------
    def studio_list(self):
        """Everything the Studio library and the Run-tab selector render.
        Results are cached against the scripts-file stamp so big libraries
        do not re-validate on every tab click."""
        try:
            st = os.stat(SCRIPTS_FILE)
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        if (key is not None and _STUDIO_LIST_CACHE["key"] == key
                and _STUDIO_LIST_CACHE["scripts"] is not None):
            return {"ok": True, "scripts": _STUDIO_LIST_CACHE["scripts"],
                    "active": _STUDIO_LIST_CACHE["active"],
                    "running": self.proc is not None}
        d = _studio_load()
        out = []
        for name in sorted(d["scripts"], key=lambda n: -(d["scripts"][n].get("updated") or 0)):
            s = d["scripts"][name]
            chk = _studio_validate(_studio_normalize(s))
            caps = ([STUDIO3_CAP_LABEL.get(c, c) for c in s.get("caps")]
                    if s.get("version") == STUDIO_SCHEMA_V3
                    and isinstance(s.get("caps"), list) else [])
            out.append({"name": name,
                        "description": s.get("description", ""),
                        "blocks": _studio_count_blocks(s.get("blocks") or []),
                        "updated": s.get("updated", 0),
                        "active": name == d["active"],
                        "runnable": bool(chk["ok"] and not chk["problems"]),
                        "caps": caps,
                        "kind": _studio_kind(s),
                        "issues": len(chk["errors"]) + len(chk["problems"])})
        _STUDIO_LIST_CACHE["key"] = key
        _STUDIO_LIST_CACHE["scripts"] = out
        _STUDIO_LIST_CACHE["active"] = d["active"]
        return {"ok": True, "scripts": out, "active": d["active"],
                "running": self.proc is not None}

    def studio_get(self, name):
        d = _studio_load()
        s = d["scripts"].get(name)
        if not isinstance(s, dict):
            return {"ok": False, "error": "Script not found."}
        if s.get("version") in (STUDIO_SCHEMA_V2, STUDIO_SCHEMA_V3):
            return {"ok": False, "error":
                    "This script was made in Prospector Studio (the "
                    "desktop app). Open it there to edit it. You can "
                    "still set it active, run it, export it, and share "
                    "it from this library."}
        return {"ok": True, "script": _studio_normalize(s),
                "active": d["active"] == name}

    def studio_validate(self, script):
        return _studio_validate(script)

    def studio_templates(self):
        return {"ok": True, "templates": _studio_templates()}

    def studio_save(self, script, prev_name=None):
        """Save a script under script['name']. Schema errors reject; mere
        runnability problems save fine (drafts must never lose work) and are
        returned so the editor can show them. Renames move the entry and keep
        the active pointer (and the engine config copy) in step."""
        chk = _studio_validate(script)
        if not chk["ok"]:
            return {"ok": False, "error": " ".join(chk["errors"][:3]),
                    "errors": chk["errors"], "problems": chk["problems"]}
        script = json.loads(json.dumps(script))
        name = script["name"]
        d = _studio_load()
        prev = prev_name if (isinstance(prev_name, str)
                             and prev_name in d["scripts"]) else None
        if name in d["scripts"] and prev != name:
            return {"ok": False,
                    "error": "A script called '%s' already exists; pick a "
                             "different name." % name,
                    "errors": [], "problems": chk["problems"]}
        now = int(time.time())
        script["updated"] = now
        if not script.get("created"):
            script["created"] = now
        if prev is not None and prev != name:
            del d["scripts"][prev]
            if d["active"] == prev:
                d["active"] = name
        d["scripts"][name] = script
        try:
            _studio_write(d)
        except OSError as e:
            return {"ok": False, "error": "Could not save: %s" % e,
                    "errors": [], "problems": chk["problems"]}
        if d["active"] == name:
            self._studio_push_active(name if not chk["problems"] else "")
            if chk["problems"]:
                d["active"] = ""
                _studio_write(d)
        return {"ok": True, "name": name, "problems": chk["problems"],
                "active": d["active"]}

    def studio_delete(self, name):
        d = _studio_load()
        if name not in d["scripts"]:
            return {"ok": False, "error": "Script not found."}
        del d["scripts"][name]
        if d["active"] == name:
            d["active"] = ""
            self._studio_push_active("")
        try:
            _studio_write(d)
        except OSError as e:
            return {"ok": False, "error": "Could not save: %s" % e}
        return {"ok": True, "active": d["active"]}

    def studio_duplicate(self, name):
        d = _studio_load()
        s = d["scripts"].get(name)
        if not isinstance(s, dict):
            return {"ok": False, "error": "Script not found."}
        base = (name + " copy")[:60]
        nm, i = base, 2
        while nm in d["scripts"]:
            nm = ("%s %d" % (base, i))[:60]
            i += 1
        c = json.loads(json.dumps(s))
        c["name"] = nm
        c["created"] = c["updated"] = int(time.time())
        d["scripts"][nm] = c
        try:
            _studio_write(d)
        except OSError as e:
            return {"ok": False, "error": "Could not save: %s" % e}
        return {"ok": True, "name": nm}

    def _studio_push_active(self, name):
        """Write the active-script keys into the engine config. Empty name =
        back to the built-in modes. The engine reads these via load_config()."""
        d = _studio_load()
        s = d["scripts"].get(name) if name else None
        cur = load_saved()
        cur["SCRIPT_MODE"] = bool(s)
        cur["SCRIPT_ACTIVE"] = name if s else ""
        cur["SCRIPT_JSON"] = (json.dumps(_studio_normalize(s),
                                         separators=(",", ":")) if s else "")
        _config_write(cur)

    def studio_set_active(self, name):
        """Make a script the active mode ('' = back to built-in modes). Only
        clean, runnable scripts can be activated."""
        d = _studio_load()
        if name:
            s = d["scripts"].get(name)
            if not isinstance(s, dict):
                return {"ok": False, "error": "Script not found."}
            chk = _studio_validate(_studio_normalize(s))
            if not chk["ok"] or chk["problems"]:
                return {"ok": False,
                        "error": "Fix this first: "
                                 + " ".join((chk["errors"] + chk["problems"])[:2]),
                        "errors": chk["errors"], "problems": chk["problems"]}
        d["active"] = name or ""
        if STUDIO_LAUNCH and name:
            # The top-level mode is server-owned and must always match the
            # active entry's kind: activating a script-kind entry IS
            # choosing STUDIO SCRIPT, a build IS choosing STUDIO BUILD.
            d["mode"] = ("script"
                         if _studio_kind(d["scripts"].get(name)) == "script"
                         else "studio")
        try:
            _studio_write(d)
            self._studio_push_active(name or "")
        except OSError as e:
            return {"ok": False, "error": "Could not save: %s" % e}
        return {"ok": True, "active": d["active"]}

    def studio_mode(self, mode=None):
        """The macro window's top-level CLASSIC | STUDIO BUILD | STUDIO
        SCRIPT mode (Studio launch). The invariant is structural: CLASSIC
        always runs the built-in cycle (no active entry), STUDIO BUILD
        ("studio") always runs an active build-kind entry, and STUDIO
        SCRIPT ("script") always runs an active script-kind entry --
        launch() refuses any mismatch. Switching to CLASSIC remembers the
        entry (last_active) so switching back restores it instead of
        losing the selection. Refuses to switch while a run is live."""
        d = _studio_load()
        cur = _studio_ui_mode(d)
        # A plain GET never has side effects. A SET that matches the current
        # mode is also a no-op -- except re-choosing a Studio mode with no
        # active entry, which retries the restore (deliberate user intent).
        if mode is None or (mode == cur
                            and (mode == "classic" or d["active"])):
            return {"ok": True, "mode": cur, "active": d["active"],
                    "kind": (_studio_kind(d["scripts"].get(d["active"]))
                             if d["active"] else ""),
                    "needs_build": cur == "studio" and not d["active"],
                    "needs_script": cur == "script" and not d["active"]}
        if mode not in ("classic", "studio", "script"):
            return {"ok": False, "error": "Unknown mode."}
        if self.proc is not None:
            return {"ok": False, "error": "Stop the run first, then switch."}
        if mode == "classic":
            if d["active"]:
                d["last_active"] = d["active"]
            d["active"] = ""
            d["mode"] = "classic"
            restore = d.get("classic_tracker")
            d["classic_tracker"] = None
            try:
                _studio_write(d)
                self._studio_push_active("")
                if restore is not None:
                    # give back the AutoPan-Tracking choice STUDIO parked
                    _config_patch({"TRACKER_MODE": restore})
            except OSError as e:
                return {"ok": False, "error": "Could not save: %s" % e}
            return {"ok": True, "mode": "classic", "active": "",
                    "tracker": bool(load_saved().get("TRACKER_MODE"))}
        want_kind = "script" if mode == "script" else "build"
        d["mode"] = mode
        try:
            _studio_write(d)
        except OSError as e:
            return {"ok": False, "error": "Could not save: %s" % e}
        # Both Studio modes own Start: park the classic AutoPan-Tracking
        # choice (see _studio_park_tracker; the way back restores it).
        err = self._studio_park_tracker()
        if err:
            return {"ok": False, "error": "Could not save: %s" % err}
        # Restore only an entry of the mode's own kind; a build must never
        # become the STUDIO SCRIPT selection and vice versa.
        active = d["active"]
        if active and _studio_kind(d["scripts"].get(active)) != want_kind:
            d["last_active"] = active
            d["active"] = active = ""
            try:
                _studio_write(d)
                self._studio_push_active("")
            except OSError as e:
                return {"ok": False, "error": "Could not save: %s" % e}
        if not active:
            for name in (d.get("last_active"), STUDIO_SCRIPT):
                s = d["scripts"].get(name) if name else None
                if isinstance(s, dict) and _studio_kind(s) == want_kind:
                    r = self.studio_set_active(name)  # validates first
                    if r.get("ok"):
                        active = name
                    break
        return {"ok": True, "mode": mode, "active": active,
                "kind": want_kind if active else "",
                "needs_build": mode == "studio" and not active,
                "needs_script": mode == "script" and not active,
                "tracker": False}

    def _studio_park_tracker(self, sd=None):
        """STUDIO owns Start: the engine must not see TRACKER_MODE (AutoPan
        Tracking is a CLASSIC program choice, and in the engine the tracker
        outranks Studio scripts). The classic preference is parked in the
        script library and restored by the switch back / Reset Studio.
        Belt-and-braces at launch too, because Prospector Studio publishes
        straight into the config and an interrupted switch can leave the
        toggle behind. Returns an error string, or None on success/no-op."""
        if not load_saved().get("TRACKER_MODE"):
            return None
        try:
            sd = sd if sd is not None else _studio_load()
            sd["classic_tracker"] = True
            _studio_write(sd)
            _config_patch({"TRACKER_MODE": False})
        except OSError as e:
            return str(e)
        return None

    def studio_push_info(self):
        """The publish stamp Prospector Studio left for the active build
        (name + content revision), so this window can show the exact same
        revision the Studio editor shows. Read-only."""
        push = self._studio_push_info()
        return {"ok": True, "name": str(push.get("name") or ""),
                "rev": str(push.get("rev") or "")}

    def studio_params(self):
        """The active script's declared macro-adjustable parameters, with
        their live values. Generated from the pushed document — an empty
        list simply means the author exposed nothing."""
        d = _studio_load()
        name = d["active"]
        if not name:
            return {"ok": True, "name": "", "params": []}
        s = d["scripts"].get(name)
        return {"ok": True, "name": name,
                "kind": _studio_kind(s),
                "running": self.proc is not None,
                "params": _studio_params(_studio_normalize(s))}

    def studio_set_param(self, pname, value):
        """Change one declared parameter of the active script. The write
        lands in the stored document (variable initial or node param), is
        re-validated as a whole — an edit that would break the script rolls
        back with the reason — and is re-pushed into the engine config.
        Values always apply at the NEXT run start (the engine binds config
        per run); a live run keeps what it started with."""
        d = _studio_load()
        name = d["active"]
        if not name:
            return {"ok": False, "error": "No active script."}
        s = d["scripts"].get(name)
        decl = None
        for p in _studio_params(_studio_normalize(s)):
            if p["name"] == pname:
                decl = p
                break
        if decl is None:
            return {"ok": False, "error": "This script declares no "
                                          "parameter called %r." % (pname,)}
        # Coerce + clamp against the declaration -- garbage never lands.
        if decl["type"] == "number":
            try:
                value = float(value)
            except (TypeError, ValueError):
                return {"ok": False, "error": "That needs to be a number."}
            if value == int(value):
                value = int(value)
            if "min" in decl and value < decl["min"]:
                value = decl["min"]
            if "max" in decl and value > decl["max"]:
                value = decl["max"]
        elif decl["type"] == "bool":
            value = bool(value)
        elif decl["type"] == "choice":
            if value not in decl.get("options", []):
                return {"ok": False, "error": "Pick one of the listed "
                                              "options."}
        else:
            if not isinstance(value, str) or len(value) > STUDIO2_STR_MAX:
                return {"ok": False, "error": "That text is too long."}
        before = json.loads(json.dumps(s))
        if decl["kind"] == "variable":
            hit = False
            for v in s.get("variables") or []:
                if isinstance(v, dict) and v.get("name") == pname:
                    v["initial"] = value
                    hit = True
            if not hit:
                return {"ok": False, "error": "The parameter's variable is "
                                              "gone; publish again from "
                                              "Prospector Studio."}
        else:
            b = _studio_find_block(s.get("blocks"), decl["node"])
            if b is None:
                return {"ok": False, "error": "The parameter's step is "
                                              "gone; publish again from "
                                              "Prospector Studio."}
            if not isinstance(b.get("params"), dict):
                b["params"] = {}
            b["params"][decl["key"]] = value
        chk = _studio_validate(_studio_normalize(s))
        if not chk["ok"] or chk["problems"]:
            d["scripts"][name] = before      # visible rollback, with the why
            return {"ok": False, "rolled_back": True,
                    "error": "That value broke the script, so it was NOT "
                             "applied: "
                             + " ".join((chk["errors"] + chk["problems"])[:2])}
        try:
            _studio_write(d)
            self._studio_push_active(name)
        except OSError as e:
            return {"ok": False, "error": "Could not save: %s" % e}
        return {"ok": True, "name": pname, "value": value,
                "effective": "next-run",
                "running": self.proc is not None}

    def studio_open_in_studio(self, node=None, name=None):
        """Ask the Prospector Studio app (when it launched this macro) to
        focus the authoring workspace for a script (the active one unless a
        name is given). The ask is a small request file in the data folder;
        Studio watches for it while the macro is open. No-op outside a
        Studio launch."""
        if not STUDIO_LAUNCH:
            return {"ok": False, "error": "Only available when Prospector "
                                          "Studio launched the macro."}
        d = _studio_load()
        payload = {"ts": time.time(),
                   "script": (str(name) if isinstance(name, str) and name
                              else d["active"] or STUDIO_SCRIPT or ""),
                   "node": str(node or "")}
        try:
            with open(os.path.join(DATA_DIR, "studio_open_request.json"),
                      "w", encoding="utf-8") as f:
                json.dump(payload, f)
        except OSError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def studio_meta(self, patch=None):
        """Small Studio flags that must survive restarts (for example whether
        the editor walkthrough already offered itself once)."""
        d = _studio_load()
        if isinstance(patch, dict):
            for k, v in patch.items():
                if isinstance(k, str) and k.isidentifier() and len(k) <= 40:
                    d["meta"][k] = bool(v)
            try:
                _studio_write(d)
            except OSError:
                pass
        return {"ok": True, "meta": d["meta"]}

    def studio_export(self, name):
        """Write ONE script to a .ppscript file via the OS save dialog."""
        d = _studio_load()
        s = d["scripts"].get(name)
        if not isinstance(s, dict):
            return {"ok": False, "error": "Script not found."}
        payload = {"_ppscript": 1, "app": APP_NAME,
                   "script": json.loads(json.dumps(s))}
        try:
            import webview
        except Exception:
            webview = None
        safe = ("".join(c if (c.isalnum() or c in " -_") else "_" for c in name).strip()
                or "script")
        try:
            if _window is not None and webview is not None:
                res = _window.create_file_dialog(
                    webview.SAVE_DIALOG, save_filename=safe + ".ppscript",
                    file_types=("Prospector Studio script (*.ppscript)",
                                "JSON file (*.json)", "All files (*.*)"))
                if not res:
                    return {"cancelled": True}
                path = res[0] if isinstance(res, (list, tuple)) else res
            else:
                path = os.path.join(os.path.dirname(CONFIG_FILE), safe + ".ppscript")
            with open(path, "w") as f:
                json.dump(payload, f, indent=2)
            return {"ok": True, "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def studio_import(self, text):
        """Add a script shared as a .ppscript/.json file. The file is treated
        as UNTRUSTED input: parsed, sanitized, strictly re-validated, and
        renamed on a clash so nothing is ever overwritten or executed."""
        if not isinstance(text, str) or len(text) > 2 * 1024 * 1024:
            return {"ok": False, "error": "That file is too large to be a script."}
        try:
            data = json.loads(text)
        except Exception:
            return {"ok": False, "error": "That is not a valid script file."}
        raw = None
        if isinstance(data, dict) and isinstance(data.get("script"), dict):
            raw = data["script"]
        elif isinstance(data, dict) and data.get("format") == "ppscript":
            raw = data
        if raw is None:
            return {"ok": False, "error": "No script found in that file."}
        script, err = _studio_sanitize(raw)
        if err:
            return {"ok": False, "error": err}
        d = _studio_load()
        base = script["name"]
        nm, i = base, 2
        while nm in d["scripts"]:
            nm = ("%s (%d)" % (base, i))[:60]
            i += 1
        script["name"] = nm
        d["scripts"][nm] = script
        try:
            _studio_write(d)
        except OSError as e:
            return {"ok": False, "error": "Could not save: %s" % e}
        chk = _studio_validate(script)
        return {"ok": True, "name": nm, "problems": chk["problems"]}

    def studio_import_dialog(self):
        """Native file picker for a shared script (the HTML file input greys
        out custom extensions on macOS, same as builds)."""
        try:
            import webview
        except Exception:
            webview = None
        if _window is None or webview is None:
            return {"ok": False, "error": "unavailable"}
        try:
            res = _window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=("Prospector Studio script (*.ppscript;*.json)",
                            "All files (*.*)"))
            if not res:
                return {"cancelled": True}
            path = res[0] if isinstance(res, (list, tuple)) else res
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read(2 * 1024 * 1024 + 1)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return self.studio_import(text)

    def studio_run(self, name):
        """Set a script active and start the macro, through the exact same
        launch path as the Start button (stats, HUD, history, hotkeys all
        behave identically)."""
        if self.proc is not None:
            return {"ok": False, "error": "Already running. Stop the current "
                                          "run first."}
        if STUDIO_LAUNCH:
            # Running an entry IS choosing its mode: flip the top level to
            # the entry's kind so the launch() invariant (mode <=> active
            # kind) holds and the window's tabs reflect what is actually
            # about to run.
            d = _studio_load()
            want = ("script"
                    if _studio_kind(d["scripts"].get(name)) == "script"
                    else "studio")
            if d.get("mode") != want:
                d["mode"] = want
                try:
                    _studio_write(d)
                except OSError as e:
                    return {"ok": False, "error": "Could not save: %s" % e}
        r = self.studio_set_active(name)
        if not r.get("ok"):
            return r
        out = self.launch(None)
        err = None if out == "launched" else {
            "already running": "Already running. Stop the current run first.",
        }.get(out, "Could not start (%s)." % out)
        return {"ok": out == "launched", "state": out, "error": err}

    def studio_stop(self):
        self.stop()
        return {"ok": True}

    def studio_state(self):
        """Live state for the Studio window (polled while it is open)."""
        d = _studio_load()
        return {"ok": True, "running": self.proc is not None,
                "status": getattr(self, "_macro_status", "off"),
                "active": d["active"]}

    def open_studio_window(self):
        global _studio_win
        if STUDIO_LAUNCH:
            # Authoring belongs to Prospector Studio; the legacy embedded
            # editor never appears in a Studio launch.
            return "studio-owns-editing"
        try:
            if _studio_win is not None:
                _studio_win.evaluate_js("window.__reload && window.__reload()")
                _studio_win.show()
                return "shown"
        except Exception as e:
            return "err:%s" % e
        return "no-window"

    def close_studio_window(self):
        global _studio_win
        try:
            if _studio_win is not None:
                _studio_win.hide()
        except Exception:
            pass
        return "hidden"

    def studio_edit(self, name):
        """Open the Studio window with one script loaded (library Open)."""
        out = self.open_studio_window()
        if isinstance(name, str) and name:
            _studio_eval("window.loadScript&&loadScript(%s)" % json.dumps(name))
        return out

    def studio_new(self):
        """Open the Studio window straight into the template picker."""
        out = self.open_studio_window()
        _studio_eval("window.newScript&&newScript()")
        return out

    # ---- calibrate: wait for the user to mark a spot, capture its x/y + colour
    def cue_mask_status(self):
        s = _sensing()
        if s is None:
            return {"advanced": True, "masks_only": False, "cues": {}}
        return s.cue_status()

    def cue_mask_check(self, cue):
        """Validate a saved mask against the LIVE screen with the real
        detector math (Sensing.cue_check mirrors Detector._cue_mask_match:
        ratio re-placement, 2 px drift refusal, 85% white-over-mask
        threshold). Used by the guided detail page's Test button."""
        s = _sensing()
        if s is None:
            return {"ok": False, "error": _SENSING_ERR}
        if cue not in ("PAN", "SHAKE", "DEPOSIT"):
            return {"ok": False, "error": "Unknown cue."}
        try:
            return s.cue_check(cue)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def clear_cue_mask(self, cue):
        s = _sensing()
        if s is None:
            return {"ok": False, "error": _SENSING_ERR}
        return s.cue_clear(cue)

    def capture_cue_mask(self, cue, thresh=None):
        """ADVANCED CALIBRATION: one-shot capture of the cue word around the
        calibrated cue pixel. [Phase 04 C8] the grab, white-select, mask
        packing and persistence run engine-side (the sensing module's
        one-shot path; superseded in the UI by the interactive overlay
        flow but preserved as a shipped surface)."""
        s = _sensing()
        if s is None:
            return {"ok": False, "error": _SENSING_ERR}
        return s.capture_cue_mask(cue, thresh)

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
                    return {"error": "Timed out, no click or Enter within 30s. "
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
        """Start gate. Captures the last typed refusal for the diagnostics
        layer (rule O consumes it), then delegates to _launch_inner --
        behavior is otherwise byte-identical."""
        r = self._launch_inner(data, relics, enabled)
        try:
            rs = r if isinstance(r, str) else ""
            self._diag_launch_refusal = (
                None if rs in ("launched", "already running") else (rs or None))
            self._diag_cache = None
        except Exception:
            pass
        return r

    def _launch_inner(self, data=None, relics=None, enabled=None):
        if data is not None:
            self.save_config(data)
        if relics is not None:
            self.save_relics(relics, enabled)
        if self.proc is not None:
            return "already running"
        # Trust gate: a macro that cannot see the screen, press keys or hear
        # its Safe Stop hotkey is unsafe to start. Only a DEFINITIVE
        # not-granted state blocks (an unreadable state never does), and only
        # Start is blocked -- the rest of the app stays fully usable.
        if lite_trust.platform_key() == "mac":
            try:
                _caps = lite_trust.capability_statuses()
                _missing = [cid for cid in ("screen_detection",
                                            "input_control", "stop_hotkeys")
                            if _caps.get(cid, {}).get("status")
                            == "not_granted"]
            except Exception:
                _missing = []
            if _missing:
                return "perm:" + ",".join(_missing)
        # Calibration gate: the same condition the Readiness Check reports
        # (readiness_check's docstring has always promised launch() enforces
        # it). Auto-calibrate covers the PIXEL items, but Advanced cue
        # matching (cue_masks) is required and can only come from a real
        # capture -- a mask-less install blocks with "cal:cue_masks", and
        # missing/stale manual values block exactly as before. CLASSIC runs
        # only: Studio builds/scripts drive their own programs and may use
        # no pixel calibration at all.
        try:
            _is_classic = _studio_ui_mode(_studio_load()) == "classic"
        except Exception:
            _is_classic = True
        if _is_classic:
            try:
                _cfg = load_saved()
                _s = _sensing()
                _health = None
                if _s is not None:
                    try:
                        _health = _s.health()
                    except Exception:
                        _health = None
                _st = lite_onboarding.calibration_status(_cfg,
                                                         health=_health)
                _ready, _blockers = lite_onboarding.calibration_ready(_st)
            except Exception:
                _ready, _blockers = True, []
            if not _ready:
                return "cal:" + ",".join(_blockers)
        if STUDIO_LAUNCH:
            # Top-level CLASSIC | STUDIO BUILD | STUDIO SCRIPT invariant:
            # each Studio mode must have an active entry of its own kind,
            # CLASSIC must have none. Refuse (with a reason the UI shows)
            # rather than silently running the wrong program.
            sd = _studio_load()
            ui_mode = _studio_ui_mode(sd)
            if ui_mode == "studio" and not sd["active"]:
                return "no-studio-build"
            if ui_mode == "script" and not sd["active"]:
                return "no-studio-script"
            if ui_mode == "classic" and sd["active"]:
                return "classic-with-active-build"
            if sd["active"]:
                kind = _studio_kind(sd["scripts"].get(sd["active"]))
                if (ui_mode, kind) not in (("studio", "build"),
                                           ("script", "script")):
                    return "mode-kind-mismatch"
            if ui_mode in ("studio", "script"):
                err = self._studio_park_tracker(sd)
                if err:
                    return "error: %s" % err
        # Run provenance (Track E): the mode + pushed revision this run
        # actually starts under -- stamped into the history entry so runs
        # stay comparable after the fact. Standalone derives the same truth
        # from the scripts store (an active entry runs instead of classic).
        try:
            _sdp = sd if STUDIO_LAUNCH else _studio_load()
            self._run_mode = _studio_ui_mode(_sdp)
            _pp = self._studio_push_info()
            self._run_rev = (str(_pp.get("rev") or "")
                             if _sdp["active"]
                             and _pp.get("name") == _sdp["active"] else "")
        except Exception:
            self._run_mode, self._run_rev = "", ""
        # When frozen there is no python.exe to run the .py macro, so re-launch
        # THIS exe with --run-macro (handled in main()). In dev, run the script.
        if FROZEN:
            cmd = [sys.executable, "--run-macro"]
        else:
            cmd = [sys.executable or "python", MACRO_FILE]
        self._ipc = bool(_EngineClient is not None
                         and load_saved().get("ENGINE_IPC", False))
        if self._ipc:
            return self._launch_ipc(cmd)
        self.proc = subprocess.Popen(
            cmd, cwd=DATA_DIR, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            env=dict(os.environ, PPENGINE_HOME=DATA_DIR),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
        self._run_active = True
        self._last_stats = None
        self._script_block = None             # never show a stale script step
        self._script_hud = ""
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
        # Shared run identity + provenance (Track E): the engine's persistent
        # run_id becomes the entry id, and the entry records which mode (and
        # pushed revision) the run started under. Absent truth stays absent:
        # a legacy engine without run_id writes no id, never a fake one.
        _rid = str(st.get("run_id") or "")
        if _rid:
            entry["id"] = _rid
        _rmode = getattr(self, "_run_mode", "")
        if _rmode:
            entry["mode"] = _rmode
        _rrev = getattr(self, "_run_rev", "")
        if _rrev:
            entry["rev"] = _rrev
        if self._synthetic_stop:
            # host-synthesized after an engine crash (protocol section 9.2):
            # never presented as an engine-emitted final
            entry["synthetic"] = True
            self._synthetic_stop = False
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
        try:
            _log_lines = getattr(self, "_run_log", None) or []
            if _log_lines:
                import datetime as _dt2
                _logdir = os.path.join(os.path.dirname(CONFIG_FILE), "run_logs")
                os.makedirs(_logdir, exist_ok=True)
                _fname = "run-" + _dt2.datetime.now().strftime("%Y%m%d-%H%M%S") + ".log"
                with open(os.path.join(_logdir, _fname), "w") as _lf:
                    _lf.write("\n".join(_log_lines[-40000:]))
                entry["log_file"] = _fname
                _files = sorted(f for f in os.listdir(_logdir) if f.endswith(".log"))
                for _old in _files[:-120]:
                    try:
                        os.remove(os.path.join(_logdir, _old))
                    except OSError:
                        pass
        except Exception:
            pass
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
        if self._ipc and self.engine is not None:
            # ipc mode has no PAUSE_TOGGLE: the host knows the state from
            # run.paused/run.resumed events and sends the explicit verb.
            self.engine.fire("run.resume" if self._engine_paused
                             else "run.pause")
            return "ok"
        return self._engine_cmd("PAUSE_TOGGLE")

    def relic_reset(self):
        """Restart every relic timer at full (someone else placed one)."""
        if self._ipc and self.engine is not None:
            self.engine.fire("relic.resetAll")
            return "ok"
        return self._engine_cmd("RELIC_RESET")

    def relic_reset_one(self, idx):
        if self._ipc and self.engine is not None:
            self.engine.fire("relic.resetOne", {"index": int(idx)})
            return "ok"
        return self._engine_cmd("RELIC_RESET_ONE %d" % int(idx))

    def relic_set(self, idx, secs):
        """Set one relic's remaining time exactly (works while paused too)."""
        if self._ipc and self.engine is not None:
            self.engine.fire("relic.set", {"index": int(idx),
                                           "seconds": int(secs)})
            return "ok"
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
        if self._ipc and self.engine is not None:
            # ipc mode: in-band shutdown (protocol section 10.1). The engine
            # emits run.stopped with FRESH final stats -- history is saved
            # from that event, never from stale 2 s data. The ladder
            # (shutdown -> terminate -> kill) runs off-thread so the UI
            # returns immediately, and a force-kill triggers the host-side
            # input-release backstop (a dead engine cannot lift its keys).
            client = self.engine
            self._macro_status = "off"
            threading.Thread(
                target=lambda: client.shutdown(
                    on_force_kill=_host_release_inputs),
                daemon=True).start()
            return "stopped"
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
        _config_write(cur)
        return "saved"

    def _overlay_preconditions(self):
        """Shared guard for every path that opens the calibration overlay.
        Returns an error dict, or None when it is safe to proceed. Checks
        (in order): the overlay window exists, screen capture is permitted
        (macOS -- otherwise the user gets a full-screen BLACK trap), and
        the sensing engine is available."""
        if _overlay is None:
            _wlog("overlay_open", status="error", code="PP-CAL-OVERLAY")
            return {"error": "The calibration overlay window is not "
                             "available in this session. Restart "
                             "Prospector Lite and try again.",
                    "error_code": "PP-CAL-OVERLAY"}
        if lite_trust.platform_key() == "mac":
            try:
                pre = lite_trust._mac_preflights().get("screen_detection")
            except Exception:
                pre = None
            if pre is False:
                _wlog("overlay_open", status="refused",
                      code="PP-CAL-SCREEN")
                return {"error": "Screen Recording is not granted, so the "
                                 "overlay would only show a black screen. "
                                 "Grant it on the Trust & Permissions step "
                                 "first, then come back.",
                        "error_code": "PP-CAL-SCREEN",
                        "needs_permission": "screen_detection"}
            # granted-mid-session: the grant exists but can not apply to
            # THIS process until relaunch -- the capture would still be
            # black. A passing screen test this session proves otherwise.
            t = self._cap_tests.get("screen_detection")
            if (pre is True
                    and lite_trust.launch_preflights().get(
                        "screen_detection") is False
                    and not (t and t["status"] == "passed")):
                _wlog("overlay_open", status="refused",
                      code="PP-CAL-RESTART")
                return {"error": "Screen Recording was granted after this "
                                 "copy of Prospector Lite started, so "
                                 "capture cannot work yet. Restart "
                                 "Prospector Lite (button on the Trust & "
                                 "Permissions step), then calibrate.",
                        "error_code": "PP-CAL-RESTART",
                        "needs_permission": "screen_detection"}
        if _sensing() is None:
            return {"error": _SENSING_ERR, "error_code": "PP-CAL-ENGINE"}
        return None

    def _overlay_show(self):
        """Show the pre-created overlay, re-fitted to the CURRENT main
        display (its geometry was frozen at boot; displays change).
        Per-platform lookup so the Windows copy re-fits too instead of
        silently failing the mac-only import.

        Every show is a NEW overlay session: the sequence id below travels
        through overlay_image() so the page can drop any async result that
        belongs to a previous capture -- the reused window must never show
        a stale banner or keep a stale interaction mode."""
        self._overlay_seq = getattr(self, "_overlay_seq", 0) + 1
        try:
            if sys.platform == "darwin":
                import Quartz as _Q
                b = _Q.CGDisplayBounds(_Q.CGMainDisplayID())
                _overlay.move(int(b.origin.x), int(b.origin.y))
                _overlay.resize(int(b.size.width), int(b.size.height))
            elif os.name == "nt":
                import ctypes as _ct
                sw = int(_ct.windll.user32.GetSystemMetrics(0))
                sh = int(_ct.windll.user32.GetSystemMetrics(1))
                _overlay.move(0, 0)
                _overlay.resize(sw, sh)
        except Exception:
            pass
        _overlay.evaluate_js("window.__reload && window.__reload()")
        _overlay.show()

    def start_overlay_calibrate(self, key, label="", context=""):
        """Open a full-screen overlay (a snapshot of your screen). Click the
        target pixel, see a marker + colour + coords, Confirm to save.
        [Phase 04 C8] the frame lives in the ENGINE's capture session
        (4.15 capture); this host keeps only overlay-window state and the
        stride-downsampled preview the engine hands back.

        `context` ('guided_setup' | 'normal_calibration') affects ONLY the
        completion callback the main window receives (__calDone) so each
        surface can route its own navigation; calibration semantics and the
        saved values are identical for both."""
        global _overlay
        err = self._overlay_preconditions()
        if err:
            return err
        s = _sensing()
        try:
            cap = s.capture()
        except Exception as e:
            _wlog("overlay_open", cap=str(key), status="error",
                  code="PP-CAL-CAPTURE", detail=str(e))
            return {"error": "Screen capture failed: %s" % e,
                    "error_code": "PP-CAL-CAPTURE"}
        self._shot_w, self._shot_h = cap["fullW"], cap["fullH"]
        self._shot_b64 = cap["image"]
        self._overlay_key = key
        self._overlay_label = label or key
        self._overlay_pending = None
        self._overlay_proposed = None
        self._overlay_region = None
        self._overlay_ctx = context or "normal_calibration"
        try:
            self._overlay_show()
        except Exception as e:
            return {"error": str(e), "error_code": "PP-CAL-OVERLAY"}
        _wlog("overlay_open", cap=str(key), status="ok",
              detail=self._overlay_ctx)
        return {"ok": True}

    def start_overlay_region(self, base, label="", context=""):
        """Open the overlay in REGION mode: the user drags one rectangle and
        both corner pixels (<base>_TL_PIXEL / <base>_BR_PIXEL) get saved.
        Much easier to aim than two separate corner clicks."""
        if base not in ("MONEY", "SHARDS", "FIND"):
            return {"error": "Unknown region."}
        r = self.start_overlay_calibrate("REGION:" + base, label or base,
                                         context)
        return r

    def _cal_done_notify(self, ok, cancelled=False):
        """Tell the MAIN window a calibration overlay finished. Carries the
        calling context and the overlay key so the guided wizard can react
        to ITS captures only; a stale or foreign result is ignored by the
        listener. Fires alongside (never instead of) __calRefresh."""
        try:
            if _window is not None:
                payload = json.dumps({
                    "ctx": getattr(self, "_overlay_ctx",
                                   "normal_calibration"),
                    "key": str(getattr(self, "_overlay_key", "") or ""),
                    "ok": bool(ok), "cancelled": bool(cancelled)})
                _window.evaluate_js(
                    "window.__calDone && window.__calDone(%s)" % payload)
        except Exception:
            pass

    def overlay_region(self, fx0, fy0, fx1, fy1):
        """Store the dragged rectangle (screen fractions) and echo its pixel
        size so the overlay can show what was grabbed."""
        key = getattr(self, "_overlay_key", None)
        if not (key and str(key).startswith("REGION:")):
            return {"error": "Not in region mode."}
        try:
            w, h = self._shot_w, self._shot_h
            x0 = max(0, min(w - 1, int(round(float(fx0) * w))))
            y0 = max(0, min(h - 1, int(round(float(fy0) * h))))
            x1 = max(0, min(w - 1, int(round(float(fx1) * w))))
            y1 = max(0, min(h - 1, int(round(float(fy1) * h))))
            tl = [min(x0, x1), min(y0, y1)]
            br = [max(x0, x1), max(y0, y1)]
            if br[0] - tl[0] < 8 or br[1] - tl[1] < 6:
                return {"error": "That box is too small. Drag a bigger one."}
            self._overlay_region = {"tl": tl, "br": br}
            return {"ok": True, "w": br[0] - tl[0], "h": br[1] - tl[1]}
        except Exception as e:
            return {"error": str(e)}

    def overlay_image(self):
        """The overlay page's single source of truth for one capture
        session: image, label, interaction mode, the exact action hint the
        top banner shows, and the session sequence id. The page rebuilds
        ALL of its state from this on every reload -- banner text and
        interaction mode are never assembled page-side from leftovers."""
        d = {"src": getattr(self, "_shot_b64", ""),
             "label": getattr(self, "_overlay_label", ""),
             "seq": getattr(self, "_overlay_seq", 0)}
        key = getattr(self, "_overlay_key", None)
        if key and str(key).startswith("REGION:"):
            d["region_mode"] = True
            d["mode"] = "region"
            d["hint"] = "drag a box around it, corner to corner"
            return d
        if key and str(key).startswith("CUEMASK:"):
            d["mode"] = "cue"
            d["cue_mode"] = getattr(self, "_cm_mode", "locate")
            if d["cue_mode"] == "edit":
                d["hint"] = ("click each letter (and the mouse) to "
                             "include/exclude — green = kept")
                try:
                    st = _sensing().cue_edit_state()
                    if st:
                        d["cue_img"] = st["image"]
                        d["cue_px"] = st["px"]
                except Exception:
                    pass
            else:
                d["hint"] = "click on the cue word"
            return d
        d["mode"] = "pixel"
        p = getattr(self, "_overlay_proposed", None)
        w = getattr(self, "_shot_w", 0)
        h = getattr(self, "_shot_h", 0)
        if p and w and h:
            d["proposed"] = {"fx": p["x"] / float(w), "fy": p["y"] / float(h),
                             "hex": p["hex"], "x": p["x"], "y": p["y"]}
            d["hint"] = ("the red × is the detected spot — "
                         "Confirm or Redo")
        else:
            d["hint"] = "click the exact spot, then Confirm"
        return d

    def overlay_pick(self, fx, fy):
        # [Phase 04 C8] all pixel math runs on the engine's stored session
        # frame (4.15 pick / the cue editor's white-grid) -- never a fresh
        # host grab.
        key = getattr(self, "_overlay_key", None)
        if key and str(key).startswith("REGION:"):
            return {"error": "region mode"}   # region uses drag, not clicks
        if key and str(key).startswith("CUEMASK:"):
            if getattr(self, "_cm_mode", "locate") != "locate":
                return {"error": "already located"}
            try:
                w, h = self._shot_w, self._shot_h
                cx = max(0, min(w - 1, int(round(float(fx) * w))))
                cy = max(0, min(h - 1, int(round(float(fy) * h))))
                r = _sensing().cue_begin(self._cm_cue, self._cm_thresh,
                                         at=(cx, cy))
                if not r.get("cueEdit"):
                    return {"error": "No white cue text there -- click right on the cue word."}
                self._cm_mode = "edit"
                return {"cue_edit": True, "img": r["image"], "px": r["px"]}
            except Exception as e:
                return {"error": str(e)}
        try:
            r = _sensing().pick(fx, fy)
            rr, gg, bb = r["rgb"]
            self._overlay_pending = {"x": r["x"], "y": r["y"], "r": rr,
                                     "g": gg, "b": bb, "hex": r["hex"]}
            return self._overlay_pending
        except Exception as e:
            return {"error": str(e)}

    def _region_preview_save(self, base, reg):
        """Stash a zoomed PNG preview of the confirmed drag box in
        REGION_PREVIEWS[base]. [Phase 04 C8] the crop + zoom run on the
        engine's session frame (4.15 crop); the preview blob itself stays
        a HOST-only key (protocol: REGION_PREVIEWS is setOpaque-class,
        never read by the engine)."""
        try:
            tl, br = reg.get("tl"), reg.get("br")
            if not tl or not br:
                return
            x0, y0 = int(min(tl[0], br[0])), int(min(tl[1], br[1]))
            x1, y1 = int(max(tl[0], br[0])), int(max(tl[1], br[1]))
            r = _sensing().crop({"x": x0, "y": y0,
                                 "w": x1 - x0, "h": y1 - y0})
            cur = load_saved()
            rp = cur.get("REGION_PREVIEWS") or {}
            rp[base] = {"preview": r["image"], "w": r["w"], "h": r["h"]}
            cur["REGION_PREVIEWS"] = rp
            _config_write(cur)
        except Exception:
            pass

    def overlay_confirm(self):
        key = getattr(self, "_overlay_key", None)
        if key and str(key).startswith("REGION:"):
            base = str(key).split(":", 1)[1]
            reg = getattr(self, "_overlay_region", None)
            if reg and base in ("MONEY", "SHARDS", "FIND"):
                self.save_pixels({base + "_TL_PIXEL": reg["tl"],
                                  base + "_BR_PIXEL": reg["br"]})
                try:
                    self._region_preview_save(base, reg)
                except Exception:
                    pass
            self._close_overlay()
            try:
                if _window is not None:
                    _window.evaluate_js("window.__calRefresh&&window.__calRefresh()")
            except Exception:
                pass
            self._cal_done_notify(True)
            return {"ok": True}
        if key and str(key).startswith("CUEMASK:"):
            saved = False
            try:
                r = _sensing().cue_save()
                saved = isinstance(r, dict) and "error" not in r
            except Exception:
                saved = False
            self._close_overlay()
            try:
                if _window is not None:
                    _window.evaluate_js("window.renderCueCaps&&window.renderCueCaps()")
            except Exception:
                pass
            self._cal_done_notify(saved)
            return {"ok": True}
        p = getattr(self, "_overlay_pending", None)
        if p and key:
            x, y, hexv = p["x"], p["y"], p["hex"]
            if key in ("CAP", "CAP_RIGHT", "CAP_LEFT"):
                # capacity endpoints: guard the pick against the SESSION
                # frame first (manual clicks on the pale anti-aliased tip
                # used to save fine and then fail the runtime gold test --
                # reproduction report issue 5). A failed guard or a
                # rejected pair save returns WITHOUT closing the overlay:
                # the page shows the exact reasons, Redo stays possible
                # and the previous values remain untouched.
                gk = "CAP_LEFT" if key == "CAP_LEFT" else "CAP_RIGHT"
                try:
                    guard = _sensing().cap_endpoint_guard(gk, x, y)
                except Exception as e:
                    guard = {"ok": False,
                             "reason": "Could not validate the click "
                                       "against the capture: %s" % e}
                if not guard.get("ok"):
                    return {"ok": False, "error": "cap_endpoints",
                            "reason": guard.get("reason", ""),
                            "reasons": [guard.get("reason", "")]}
                x, y = int(guard["x"]), int(guard["y"])
                hexv = guard.get("hex") or hexv
                if key == "CAP_LEFT":
                    self._cap_repair_backup()
                    r = self.save_pixels({"CAP_LEFT_PIXEL": [x, y]})
                else:
                    px = {"CAP_FULL_PIXEL": [x, y]}
                    cl = getattr(self, "_overlay_cap_left", None)
                    prop = getattr(self, "_overlay_proposed", None)
                    # the auto-detected left tip came from the SAME frame
                    # as the proposal: save the pair together when the
                    # user confirmed the proposal unchanged (a manual
                    # re-pick saves the right end alone -- validation
                    # then runs against the stored left)
                    same = (prop is not None and p.get("x") == prop.get("x")
                            and p.get("y") == prop.get("y"))
                    if cl and (key == "CAP" or same):
                        px["CAP_LEFT_PIXEL"] = [int(cl[0]), int(cl[1])]
                    self._cap_repair_backup()
                    r = self.save_pixels(px, {"CAP_FULL_PIXEL": hexv})
                if isinstance(r, dict) and r.get("ok") is False:
                    return r
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
        self._cal_done_notify(bool(p and key))
        return {"ok": True}

    def overlay_cancel(self):
        self._close_overlay()
        self._cal_done_notify(False, cancelled=True)
        return {"ok": True}

    def start_cue_mask_capture(self, cue, thresh=None, context=""):
        """Open the calibration OVERLAY to capture a cue mask: first LOCATE (click
        the cue word on the game), then EDIT (click each letter / the mouse to
        include or exclude it), then Confirm. [Phase 04 C8] the frame goes
        into the ENGINE's capture session; locate/edit pixel math runs
        engine-side (4.15 cueMask); this host keeps overlay state.
        `context` affects only the __calDone navigation callback."""
        global _overlay
        if cue not in ("PAN", "SHAKE", "DEPOSIT"):
            return {"ok": False, "error": "Unknown cue."}
        err = self._overlay_preconditions()
        if err:
            return dict(err, ok=False)
        s = _sensing()
        try:
            cap = s.capture()
        except Exception as e:
            return {"ok": False, "error": str(e),
                    "error_code": "PP-CAL-CAPTURE"}
        self._cm_cue = cue
        self._cm_thresh = int(thresh) if thresh else 160
        self._cm_mode = "locate"
        self._shot_w, self._shot_h = cap["fullW"], cap["fullH"]
        self._shot_b64 = cap["image"]
        self._overlay_ctx = context or "normal_calibration"
        self._overlay_key = "CUEMASK:" + cue
        names = {"PAN": "Pan", "SHAKE": "Shake", "DEPOSIT": "Collect Deposit"}
        # the banner composes "Calibrate: <label> - <hint>", so the label is
        # just the target; the action ("click on the cue word") is the hint
        self._overlay_label = "\u201c%s\u201d cue" % names[cue]
        try:
            self._overlay_show()
        except Exception as e:
            return {"ok": False, "error": str(e),
                    "error_code": "PP-CAL-OVERLAY"}
        return {"ok": True}

    def cue_toggle(self, fx, fy):
        """EDIT mode: toggle the connected white region (a letter / the mouse)
        under the click in/out of the mask. [Phase 04 C8] flood-fill +
        rendering run engine-side (4.15 cueMask toggle)."""
        try:
            if getattr(self, "_cm_mode", None) != "edit":
                return {"error": "not editing"}
            r = _sensing().cue_toggle(fx, fy)
            if "error" in r:
                return {"error": r["error"]}
            return {"img": r["image"], "px": r["px"]}
        except Exception as e:
            return {"error": str(e)}

    def cue_reset(self):
        # back to LOCATE: the engine session keeps the frame; the next
        # locate click re-runs the engine's white-grid there
        self._cm_mode = "locate"
        return {"ok": True}

    def _close_overlay(self):
        global _overlay
        try:
            if _overlay is not None:
                _overlay.hide()
        except Exception:
            pass

    # _grab_full / _detect_capacity_px / _detect_cue_px moved engine-side
    # at Phase 04 C8 (prospector_engine.sensing): the wizard's grabs and
    # detection run in the engine's capture session (protocol 4.15
    # calibration.detect); the dormant empty-bar-diff branch was not
    # carried into v1.0 (no shipped call site).

    def wizard_capture_empty(self):
        """Remember a snapshot of the screen with the capacity bar EMPTY.
        [Phase 04 C8] the empty-bar diff refinement was dropped from the
        detector (dormant -- no shipped call site); the grab still runs
        through the engine session so the handler keeps its shape."""
        s = _sensing()
        if s is None:
            return {"ok": False, "error": _SENSING_ERR}
        try:
            s.capture()
            return {"ok": True, "msg": "Captured the empty bar"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def wizard_propose(self, kind, label="", context=""):
        """Auto-detect a spot, then OPEN THE OVERLAY pre-marked with a red X at
        the guess so the user can Confirm or Redo. Nothing is saved until they
        confirm. If detection fails, the overlay opens for a plain manual pick.
        [Phase 04 C8] detection + the frame run engine-side (4.15 detect:
        the fresh grab becomes the capture session).
        `context` affects only the __calDone navigation callback."""
        global _overlay
        err = self._overlay_preconditions()
        if err:
            return err
        self._overlay_ctx = context or "normal_calibration"
        s = _sensing()
        if kind in ("CAP", "CAP_RIGHT", "CAP_LEFT"):
            target, cue = "capacityBar", None
        elif kind in ("PAN_PIX", "SHAKE_PIX", "DEPOSIT_PIX"):
            target, cue = "cuePrompt", kind
        else:
            target, cue = None, None
        try:
            if target:
                det = s.detect(target, cue)
                cap = s.session_preview()
            else:
                det = {"detected": False, "message": "unknown target"}
                cap = s.capture()
        except Exception as e:
            return {"error": str(e), "error_code": "PP-CAL-CAPTURE"}
        self._shot_w, self._shot_h = cap["fullW"], cap["fullH"]
        self._shot_b64 = cap["image"]
        self._overlay_key = kind
        self._overlay_label = label or kind
        self._overlay_pending = None
        self._overlay_proposed = None
        self._overlay_cap_left = None
        if det.get("detected"):
            p = det.get("proposal") or {}
            if kind in ("CAP", "CAP_RIGHT"):
                tx, ty = p["right"]
                self._overlay_cap_left = p.get("left")
                rr, gg, bb = p["rgb"]
                hexv = p["hex"]
            elif kind == "CAP_LEFT":
                tx, ty = p["left"]
                pk = s.pick(tx / float(cap["fullW"]),
                            ty / float(cap["fullH"]))
                rr, gg, bb = pk["rgb"]
                hexv = pk["hex"]
            else:
                tx, ty = p["pixel"]
                rr, gg, bb = p["rgb"]
                hexv = p["hex"]
            self._overlay_pending = {"x": tx, "y": ty, "r": rr,
                                     "g": gg, "b": bb, "hex": hexv}
            self._overlay_proposed = dict(self._overlay_pending)
        try:
            self._overlay_show()
        except Exception as e:
            return {"error": str(e), "error_code": "PP-CAL-OVERLAY"}
        return {"ok": True, "detected": bool(self._overlay_proposed),
                "msg": det.get("message")}

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
                self._run_log = []
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
            if line.startswith("__SCRIPT__ "):
                try:
                    self._on_script_block(json.loads(line[11:]))
                except Exception:
                    pass
                continue
            if line.startswith("__SCRIPTHUD__ "):
                try:
                    self._on_script_hud(
                        json.loads(line[14:]).get("text", ""))
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
            try:
                if not hasattr(self, "_run_log"):
                    self._run_log = []
                self._run_log.append(line)
                if len(self._run_log) > 60000:
                    del self._run_log[:20000]
            except Exception:
                pass
        rc = proc.wait()
        _emit_log(f"[macro exited, code {rc}]")
        self.proc = None
        self._macro_status = "off"
        self._save_history()              # macro ended on its own (Esc / self-stop)
        _emit_state(False)

    # ---- Prospector Engine ipc mode (Phase 04 C3) --------------------------
    # With ENGINE_IPC on, the engine speaks PPE1 frames and these handlers
    # feed the exact same UI surfaces the legacy _pump feeds. Statuses come
    # only from structured events -- never from substring-matching log text
    # (protocol section 2 forbids it in ipc mode).

    def _launch_ipc(self, cmd):
        self._engine_paused = False
        client = _EngineClient(
            cmd, home=DATA_DIR, host="lite", cwd=DATA_DIR,
            on_event=self._on_engine_event, on_diag=self._on_engine_diag,
            on_exit=self._on_engine_exit)
        self.engine = client
        client.spawn()
        self.proc = client.proc
        self._run_active = True
        self._last_stats = None
        self._script_block = None             # never show a stale script step
        self._script_hud = ""
        self._events = []                     # detailed telemetry for this run
        self._finds = []                      # analytics: logged finds
        self._phase_samples = {}              # per-phase durations (ms)
        self._phase_last = None
        self._macro_status = "idle"
        threading.Thread(target=self._ipc_ready_watch, args=(client,),
                         daemon=True).start()
        return "launched"

    def _ipc_ready_watch(self, client):
        if client.wait_ready():
            eng = (client.hello or {}).get("engine", {})
            _emit_log("[engine] ready: v%s fp=%s (ipc mode)"
                      % (eng.get("version"), eng.get("sourceFingerprint")))
            return
        ref = client.refused or {}
        tail = "\n".join(client.stderr_tail[-6:])
        _emit_log("[engine] failed to start: %s -- %s\n%s"
                  % (ref.get("code"), ref.get("message"), tail))

    def _on_engine_diag(self, line):
        # non-frame diagnostics: forward to the log surface verbatim, never
        # interpreted (retires the legacy substring status machine)
        _emit_log(line)
        try:
            if not hasattr(self, "_run_log"):
                self._run_log = []
            self._run_log.append(line)
            if len(self._run_log) > 60000:
                del self._run_log[:20000]
        except Exception:
            pass

    def _on_engine_event(self, fr):
        ev, d = fr["ev"], fr.get("data", {})
        ts = float(fr.get("ts", 0.0))
        if ev == "run.started":
            # legacy __RESET__ + [RUNNING], keyed per run (protocol 3.2)
            _hud_eval("window.hudReset&&hudReset()")
            self._finds = []
            self._last_stats = None
            self._run_log = []
            self._events = []
            self._diag_event_counts = {}  # per-run diagnostics counters
            self._diag_cache = None
            self._phase_samples = {}
            self._phase_last = None
            self._run_active = True
            self._engine_paused = False
            self._macro_status = "running"
            _hud_eval("window.hudRun&&hudRun('run')")
            _emit_paused(False)
        elif ev == "run.stats":
            flat = {}
            flat.update(d.get("raw", {}))
            flat.update(d.get("derived", {}))
            flat.update(d.get("meta", {}))
            js = json.dumps(flat)             # parse first, re-serialize:
            _emit_stats(js)                   # never inject raw frame text
            _hud_eval("window.hudStats&&hudStats(%s)" % js)
            self._last_stats = flat
            if self._macro_status in ("off", "idle"):
                self._macro_status = "running"
        elif ev == "find.new":
            fv = {k: v for k, v in d.items() if k != "runId"}
            _hud_eval("window.hudFind&&hudFind(%s)" % json.dumps(fv))
            if not hasattr(self, "_finds") or self._finds is None:
                self._finds = []
            self._finds.append(fv)
            if len(self._finds) > 500:
                self._finds = self._finds[-500:]
        elif ev == "find.updated":
            fv = {k: v for k, v in d.items() if k not in ("runId", "final")}
            fid = fv.get("id")
            for i in range(len(getattr(self, "_finds", []) or [])):
                if self._finds[i].get("id") == fid:
                    self._finds[i] = fv
                    break
        elif ev == "safety.event":
            rec = {"t": round(ts, 1), "type": d.get("type", "?"),
                   "reason": d.get("reason", "")}
            for k in ("where", "contents"):
                if d.get(k):
                    rec[k] = d[k]
            _hud_eval("window.hudEvent&&hudEvent(%s)" % json.dumps(rec))
            if not hasattr(self, "_events") or self._events is None:
                self._events = []
            self._events.append(rec)
            if len(self._events) > 2000:
                self._events = self._events[-2000:]
            # diagnostics ctx: reuse the record that already flows -- a
            # rolling window plus per-run type counts (no new traffic)
            try:
                ce = getattr(self, "_diag_ctx_events", None)
                if ce is None:
                    ce = self._diag_ctx_events = []
                ce.append(rec)
                if len(ce) > 100:
                    del ce[:len(ce) - 100]
                cc = getattr(self, "_diag_event_counts", None)
                if cc is None:
                    cc = self._diag_event_counts = {}
                cc[rec["type"]] = cc.get(rec["type"], 0) + 1
            except Exception:
                pass
        elif ev == "run.phase":
            name = d.get("phase", "?")
            _hud_eval("window.hudPhase&&hudPhase(%s)" % json.dumps(name))
            # phase timings derive from engine-stamped ts (protocol 5.4),
            # never from arrival wall-clock
            _last = getattr(self, "_phase_last", None)
            if _last:
                _pn, _pt = _last
                _d = (ts - _pt) * 1000.0
                if 0 < _d < 120000:
                    _ps = getattr(self, "_phase_samples", None)
                    if _ps is None:
                        _ps = self._phase_samples = {}
                    _arr = _ps.setdefault(_pn, [])
                    _arr.append(_d)
                    if len(_arr) > 500:
                        del _arr[:len(_arr) - 500]
            self._phase_last = (name, ts)
        elif ev == "geode.timer":
            _gm = int(d.get("ms", 0) or 0)
            _gl = json.dumps(d.get("label", ""))
            _hud_eval("window.hudGeode&&hudGeode(%d,%s)" % (_gm, _gl))
            try:
                if _window is not None:
                    _window.evaluate_js(
                        "window.geodeTimer&&geodeTimer(%d,%s)" % (_gm, _gl))
            except Exception:
                pass
        elif ev == "script.block":
            payload = {k: v for k, v in d.items() if k != "runId"}
            self._on_script_block(payload)
        elif ev == "script.hud":
            _emit_log("[script] %s" % d.get("text", ""))
            self._on_script_hud(d.get("text", ""))
        elif ev == "hotkey.popout":
            try:
                self.toggle_popout()
            except Exception:
                pass
        elif ev == "run.paused":
            self._engine_paused = True
            self._macro_status = "paused"
            _hud_eval("window.hudRun&&hudRun('pause')")
            _emit_paused(True)
        elif ev == "run.resumed":
            self._engine_paused = False
            self._macro_status = "running"
            _hud_eval("window.hudRun&&hudRun('run')")
            _emit_paused(False)
        elif ev == "run.stopped":
            self._engine_paused = False
            self._macro_status = "stopped"
            _hud_eval("window.hudRun&&hudRun('idle')")
            _emit_paused(False)
            fin = d.get("final") or {}
            flat = {}
            flat.update(fin.get("raw", {}))
            flat.update(fin.get("derived", {}))
            flat.update(fin.get("meta", {}))
            if flat:
                self._last_stats = flat       # fresh final, never stale 2 s
            self._save_history()              # keys off run.stopped (sec 6)
        elif ev == "safety.safePaused":
            self._macro_status = "safe-pause"
        elif ev == "safety.recovery":
            if d.get("stage") == "start":
                self._macro_status = "recovering"
            elif self._macro_status == "recovering":
                self._macro_status = "running"
        elif ev == "safety.hardStopped":
            self._macro_status = "stopped"
        elif ev == "engine.log":
            self._on_engine_diag(d.get("text", ""))
        elif ev == "engine.hello":
            # 1.5 runner identity: the engine's durable instance GUID,
            # executable and home, mirrored into the status file at 1 Hz
            # so Prospector Studio can verify WHICH runner is answering.
            eng = d.get("engine") or {}
            proto = d.get("protocol")
            self._engine_instance = {
                "instanceId": str(eng.get("instance") or ""),
                "pid": d.get("pid"),
                "exePath": str(eng.get("exePath") or ""),
                "dataDir": str(d.get("home") or ""),
                "fingerprint": str(eng.get("sourceFingerprint") or ""),
                "protocol": proto if isinstance(proto, dict) else None,
            }
        elif ev == "engine.bye":
            if d.get("reason") == "fatal":
                _emit_log("[engine] refused: %s -- %s"
                          % (d.get("code"), d.get("message")))

    def _on_engine_exit(self, info):
        client = self.engine
        if info.clean:
            _emit_log("[macro exited, code %s]" % info.code)
        else:
            _emit_log("[engine crashed, code %s] stderr tail:\n%s"
                      % (info.code,
                         "\n".join((client.stderr_tail if client else [])[-6:])))
            if getattr(self, "_run_active", False) and self._last_stats:
                self._synthetic_stop = True   # crash record, marked synthetic
            self._crash_report(client, info)
            self._save_history(reason="engine-crashed")
        self.proc = None
        self.engine = None
        self._macro_status = "off"
        self._save_history()          # no-op if the run was already recorded
        _emit_state(False)

    def _crash_report(self, client, info):
        """Persist stderr tail + recent events for diagnostics (9.2.3)."""
        try:
            import datetime
            d = os.path.join(os.path.dirname(CONFIG_FILE),
                             "engine_crash_reports")
            os.makedirs(d, exist_ok=True)
            fname = ("crash-"
                     + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                     + ".json")
            with open(os.path.join(d, fname), "w") as f:
                json.dump({"exit_code": info.code,
                           "stderr": list(client.stderr_tail) if client else [],
                           "events": (client.recent_events[-200:]
                                      if client else [])}, f, indent=2)
            olds = sorted(x for x in os.listdir(d) if x.endswith(".json"))
            for x in olds[:-20]:
                try:
                    os.remove(os.path.join(d, x))
                except OSError:
                    pass
        except Exception:
            pass


def _host_release_inputs(vocab):
    """Protocol section 10.1 backstop: after a force-kill the dead engine
    cannot lift its held inputs, so the host itself issues OS-level
    up-events for the whole injectable vocabulary (idempotent).

    Windows builds do not ship pynput, so the release is issued through
    the same ctypes SendInput/mouse_event family the engine's own input
    path uses -- previously the except-ImportError made this backstop a
    silent no-op on every Windows build."""
    try:
        from pynput.keyboard import Controller as _KC, Key as _K
        from pynput.mouse import Controller as _MC, Button as _MB
        kb, ms = _KC(), _MC()
        for name in (vocab or {}).get("keys", []):
            try:
                if name == "Shift":
                    kb.release(_K.shift)
                elif name == "Space":
                    kb.release(_K.space)
                else:
                    kb.release(name.lower())
            except Exception:
                pass
        for _b in (vocab or {}).get("buttons", []):
            try:
                ms.release(_MB.left)
            except Exception:
                pass
        return
    except Exception:
        pass
    if os.name != "nt":
        return
    try:
        import ctypes
        u32 = ctypes.windll.user32
        KEYEVENTF_KEYUP = 0x0002
        _VK = {"W": 0x57, "A": 0x41, "S": 0x53, "D": 0x44,
               "Space": 0x20, "Shift": 0x10, "E": 0x45, "Q": 0x51}
        for name in (vocab or {}).get("keys", []):
            vk = _VK.get(name) or _VK.get(str(name).upper())
            if vk:
                try:
                    u32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
                except Exception:
                    pass
        if (vocab or {}).get("buttons"):
            MOUSEEVENTF_LEFTUP = 0x0004
            try:
                u32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            except Exception:
                pass
    except Exception:
        pass


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
_studio_win = None

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
 function fmt(s){s=Math.max(0,Math.round(s||0));var h=Math.floor(s/3600),m=Math.floor((s%3600)/60),ss=String(s%60).padStart(2,"0");return h>0?(h+":"+String(m).padStart(2,"0")+":"+ss):(m+":"+ss);}
 async function poll(){let r;try{r=await api().pill_state();}catch(e){return;}
   alive=r.alive;const sm=SM[r.status||"off"]||["","-"];
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
 $("toggle").onclick=async()=>{try{if(alive){await api().stop();}
   else{const r=await api().launch();
     // launch() refusals ('perm:...', 'cal:...', mode errors) must not
     // read as a dead button on this tiny surface: flash the reason and
     // restore the main window where the full explanation lives.
     if(typeof r==='string'&&r!=='launched'&&r!=='already running'){
       const st=$("status");if(st)st.textContent=(r.indexOf('perm:')===0?'needs permission':r.indexOf('cal:')===0?'needs calibration':'blocked');
       try{api().restore();}catch(_){}}}
   }catch(e){}poll();};
 $("expand").onclick=()=>{try{api().restore();}catch(e){}};
 $("pausebt").onclick=async()=>{try{await api().pause_toggle();}catch(e){}};
 setInterval(poll,1000);poll();
</script></body></html>'''


_LOG_BUF = []
_LOG_LOCK = threading.Lock()
_LOG_FLUSHING = False


def _log_flush_loop():
    # Batch log lines into one evaluate_js every ~250ms. One bridge call per
    # line floods the UI thread on long runs (the 8h-session lag); batching
    # keeps the app responsive no matter how chatty the engine gets.
    global _LOG_FLUSHING
    while True:
        time.sleep(0.25)
        with _LOG_LOCK:
            if not _LOG_BUF:
                _LOG_FLUSHING = False
                return
            chunk = "\n".join(_LOG_BUF)
            del _LOG_BUF[:]
        if _window is not None:
            try:
                _window.evaluate_js("window.addLog && addLog(%s)" % json.dumps(chunk))
            except Exception:
                pass


def _emit_log(text):
    if _window is None:
        return
    global _LOG_FLUSHING
    with _LOG_LOCK:
        _LOG_BUF.append(text)
        if not _LOG_FLUSHING:
            _LOG_FLUSHING = True
            threading.Thread(target=_log_flush_loop, daemon=True).start()


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
    full = HELP[key]
    short = full.split("\n", 1)[0].split(". ", 1)[0].strip().rstrip(".")
    if short and len(short) < len(full):
        short += "."
    tip = (short or full).replace('"', "&quot;")
    return f'<span class="qm" data-tip="{tip}">?</span>'


def _hud_html():
    return r"""<!doctype html><html><head><meta charset="utf-8"><style>
 html,body{margin:0;background:#171310 url("data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIzMjAiIGhlaWdodD0iMzAwIj48cmVjdCB3aWR0aD0iMzIwIiBoZWlnaHQ9IjMwMCIgZmlsbD0iIzE2MTIxMCIvPjxmaWx0ZXIgaWQ9ImciPjxmZVR1cmJ1bGVuY2UgdHlwZT0iZnJhY3RhbE5vaXNlIiBiYXNlRnJlcXVlbmN5PSIwLjg1IDAuMDE0IiBudW1PY3RhdmVzPSIzIiBzZWVkPSIxMSIgc3RpdGNoVGlsZXM9InN0aXRjaCIgcmVzdWx0PSJuIi8+PGZlQ29sb3JNYXRyaXggaW49Im4iIHR5cGU9Im1hdHJpeCIgdmFsdWVzPSIwIDAgMCAwIDAuMTQgMCAwIDAgMCAwLjExNSAwIDAgMCAwIDAuMDkgMCAwIDAgMC4xNiAwIi8+PC9maWx0ZXI+PHJlY3Qgd2lkdGg9IjMyMCIgaGVpZ2h0PSIzMDAiIGZpbHRlcj0idXJsKCNnKSIvPjxnIHN0cm9rZT0iIzBkMGEwNyIgc3Ryb2tlLXdpZHRoPSIyIiBvcGFjaXR5PSIwLjg1Ij48bGluZSB4MT0iMCIgeTE9IjYwIiB4Mj0iMzIwIiB5Mj0iNjAiLz48bGluZSB4MT0iMCIgeTE9IjEyMCIgeDI9IjMyMCIgeTI9IjEyMCIvPjxsaW5lIHgxPSIwIiB5MT0iMTgwIiB4Mj0iMzIwIiB5Mj0iMTgwIi8+PGxpbmUgeDE9IjAiIHkxPSIyNDAiIHgyPSIzMjAiIHkyPSIyNDAiLz48L2c+PGcgc3Ryb2tlPSIjMmIyMTE5IiBzdHJva2Utd2lkdGg9IjEiIG9wYWNpdHk9IjAuNTUiPjxsaW5lIHgxPSIwIiB5MT0iNjIiIHgyPSIzMjAiIHkyPSI2MiIvPjxsaW5lIHgxPSIwIiB5MT0iMTIyIiB4Mj0iMzIwIiB5Mj0iMTIyIi8+PGxpbmUgeDE9IjAiIHkxPSIxODIiIHgyPSIzMjAiIHkyPSIxODIiLz48bGluZSB4MT0iMCIgeTE9IjI0MiIgeDI9IjMyMCIgeTI9IjI0MiIvPjwvZz48L3N2Zz4=");background-size:320px 300px;color:#ece0d0;height:100%;
  font:12.5px Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  border-radius:14px;overflow:hidden;-webkit-user-select:none;user-select:none}
 .wrap{padding:12px 14px 10px;display:flex;flex-direction:column;gap:7px;height:100%;box-sizing:border-box}
 .hd{display:flex;align-items:center;gap:9px}
 .led{width:10px;height:10px;border-radius:50%;background:#5a5347;flex:none}
 .led.run{background:#7faf5d;box-shadow:0 0 9px rgba(127,175,93,.8)}
 .led.pause{background:#a8794a;box-shadow:0 0 9px rgba(168,121,74,.7)}
 .state{font-size:18px;font-weight:700}
 .sub{color:#9c9183;font-size:11px;margin-left:auto;font-variant-numeric:tabular-nums}
 .hudx{color:#6a6253;cursor:pointer;font-size:13px;line-height:1;padding:2px 4px;flex:none;-webkit-app-region:no-drag}
 .hudx:hover{color:#ece4d6}
 .stats{display:flex;gap:11px;color:#9c9183;font-size:11.5px;flex-wrap:wrap}
 .stats b{color:#caa06e;font-variant-numeric:tabular-nums;font-weight:600}
 .ttl{color:#6a5d4d;font-size:9.5px;letter-spacing:.13em;text-transform:uppercase}
 svg{width:100%;display:block}
 .evt{flex:1;font-size:11px;color:#9c8e7c;line-height:1.55;overflow:hidden}
 .evt .warn{color:#e0a05f}.evt .bad{color:#e07b5f}.evt .good{color:#9bc07e}
 .hint{color:#524b40;font-size:9.5px;text-align:center}
 .gtimer{color:#d8a34a;font-size:12px;font-weight:700;text-align:center;background:rgba(168,121,74,.14);border:1px solid rgba(168,121,74,.32);border-radius:7px;padding:4px 8px;margin:2px 0;font-variant-numeric:tabular-nums}
 body.scriptmode #hscript,body.scriptmode #hscripthud{font-size:13.5px;padding:6px 10px}
</style></head><body><div class="wrap">
 <div class="hd"><span class="led" id="led"></span><span class="state" id="state">idle</span>
   <span class="sub" id="sub"></span><span class="hudx" id="hudx" title="Hide the HUD" onclick="try{window.pywebview.api.hud_toggle()}catch(e){}">&#10005;</span></div>
 <div class="stats"><span>pans <b id="hpans">0</b></span><span><b id="hpph">0</b>/hr</span>
   <span>clean <b id="hclean">–</b></span><span>finds <b id="hfinds">0</b></span>
   <span>lag <b id="hlag">–</b></span><span>miss <b id="hmiss">0</b></span></div>
 <div class="gtimer" id="hgtimer" style="display:none"></div>
 <div class="gtimer" id="hscript" style="display:none"></div>
 <div class="gtimer" id="hscripthud" style="display:none"></div>
 <div class="ttl" id="hcycttl">cycle, live</div>
 <svg id="hudsvg"></svg>
 <div class="ttl">events</div>
 <div class="evt" id="hevt"></div>
 <div class="hint">drag to move · HUD button in the app hides me</div>
</div><script>
""" + _CYCMODEL_JS + r"""

 let VALS=null,AB=null,MODEL=null,SPANS=null,TOTAL=1;
 const MAP={dig:'dig',water:'swalk',glide:'glide',shake:'shake',settle:'land'};
 const LABEL={dig:'DIGGING',water:'HOLD S to water',glide:'HOLD W glide',
   shake:'SHAKING',settle:'SETTLING',recover:'RECOVERING'};
 let cur={stage:null,t0:0},runState='idle';
 // HUD honesty: the classic-settings cycle diagram describes only CLASSIC
 // runs. STUDIO BUILD runs get a neutral "as observed" strip driven by the
 // engine's real run.phase events (dig/water/shake, Track C); STUDIO SCRIPT
 // runs hide the diagram entirely -- the script step + hud line ARE the
 // truth there. Nothing estimated, no fake zeros.
 let MODE='classic',OBS='';
 const OBSSEG=[['dig','#a8794a','DIG'],['water','#6ba1b5','WATER'],['shake','#caa06e','SHAKE']];
 function applyMode(){var t=document.getElementById('hcycttl'),s=document.getElementById('hudsvg');
   if(MODE==='script'){document.body.classList.add('scriptmode');
     if(t)t.style.display='none';if(s)s.style.display='none';MODEL=null;SPANS=null;return;}
   document.body.classList.remove('scriptmode');
   if(t)t.style.display='';if(s)s.style.display='';
   if(MODE==='studio'){if(t)t.textContent='build phases, as observed';
     MODEL=null;SPANS=null;drawObserved();}
   else{if(t)t.textContent='cycle, live';rebuild();}}
 function drawObserved(){const svg=document.getElementById('hudsvg');if(!svg)return;
   const W=356,H=64,bw=(W-8)/3;let d='';
   OBSSEG.forEach((s,i)=>{const x=4+i*bw,on=OBS===s[0];
     d+='<rect x="'+(x+1)+'" y="8" width="'+(bw-2)+'" height="26" rx="3" fill="'+s[1]+'" fill-opacity="'+(on?'0.95':'0.25')+'"'+(on?' stroke="#ece0d0" stroke-opacity=".8"':'')+'/>';
     d+='<text x="'+(x+bw/2)+'" y="56" fill="'+(on?'#ece0d0':'#6a6253')+'" font-size="9" text-anchor="middle">'+s[2]+'</text>';});
   svg.setAttribute('viewBox','0 0 '+W+' '+H);svg.innerHTML=d;}
 function rebuild(){if(!VALS||MODE!=='classic')return;MODEL=cycModel(VALS,AB||{});TOTAL=Math.max(MODEL.cap,1);
   SPANS={};let t=0;
   MODEL.segs.forEach(s=>{const sp=SPANS[s.stage]||(SPANS[s.stage]=[t,t]);
     sp[1]=t+s.hi;if(sp[0]>t)sp[0]=t;t+=s.hi;});
   draw();}
 function draw(){const svg=document.getElementById('hudsvg');if(!svg||!MODEL)return;
   const W=356,H=64,L=4,R=4;const x=t=>L+(W-L-R)*t/TOTAL;
   const col={dig:'#a8794a',swalk:'#6ba1b5',glide:'#9bc07e',shake:'#caa06e',land:'#b58f6b'};
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
 window.hudPhase=function(p){
   if(MODE==='studio'){if(p==='dig'||p==='water'||p==='shake'){OBS=p;drawObserved();}}
   else cur={stage:MAP[p]||null,t0:performance.now()};
   const st=document.getElementById('state');
   if(MODE==='studio')st.textContent=(p||'').toUpperCase();
   else if(LABEL[p])st.textContent=LABEL[p];else st.textContent=(p||'').toUpperCase();};
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
   var rt=Math.max(0,Math.round(s.runtime_s||0)),_h=Math.floor(rt/3600),_m=Math.floor((rt%3600)/60),_s2=String(rt%60).padStart(2,'0');g('sub') && (document.getElementById('sub').textContent=
     _h>0?(_h+':'+String(_m).padStart(2,'0')+':'+_s2):(_m+':'+_s2));
   runState=runState==='pause'?'pause':'run';
   document.getElementById('led').className='led'+(runState==='pause'?' pause':' run');};
 window.hudEvent=function(e){const box=document.getElementById('hevt');if(!box)return;
   const cls=(e.type==='nudge'||e.type==='recover'||e.type==='recenter')?'warn'
     :(e.type&&e.type.indexOf('fail')>=0)||e.type==='no_progress'||e.type==='break_out'
       ||e.type==='safe_stop'||e.type==='hard_stop'?'bad':'';
   const d=document.createElement('div');d.className='e '+cls;
   d.textContent='· '+(e.type||'?')+(e.reason?(', '+e.reason):'');
   box.prepend(d);while(box.children.length>4)box.removeChild(box.lastChild);};
 window.hudFind=function(f){const box=document.getElementById('hevt');if(!box)return;
   const d=document.createElement('div');d.className='e good';
   d.textContent='◆ '+(f.mod?f.mod+' ':'')+f.name+' '+f.kg+'kg';
   box.prepend(d);while(box.children.length>4)box.removeChild(box.lastChild);};
 window.hudReset=function(){OBS='';boot();document.getElementById('hevt').innerHTML='';
   var sc=document.getElementById('hscript');if(sc){sc.style.display='none';sc.textContent='';}
   var sh=document.getElementById('hscripthud');if(sh){sh.style.display='none';sh.textContent='';}};
 window.hudScript=function(p){const el=document.getElementById('hscript');if(!el||!p)return;
   el.style.display='block';
   el.textContent='▸ '+(p.type||'?')+' · '+(p.id||'?')
     +(typeof p.pass!=='undefined'?(' · pass '+p.pass):'')
     +(typeof p.n!=='undefined'?(' · step '+p.n):'');};
 window.hudScriptHud=function(t){const el=document.getElementById('hscripthud');if(!el)return;
   if(!t){el.style.display='none';el.textContent='';return;}
   el.style.display='block';el.textContent=t;};
 async function boot(){try{const st=await window.pywebview.api.get_state();
   VALS=st.values||{};AB=st.autobuild||{};
   MODE=(st.studio_mode==='studio'||st.studio_mode==='script')?st.studio_mode:'classic';
   applyMode();}catch(e){}}
 window.addEventListener('pywebviewready',()=>{boot();tick();});
 setTimeout(()=>{boot();tick();},700);
</script></body></html>"""


_OVERLAY_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><style>
 html,body{margin:0;height:100%;overflow:hidden;cursor:crosshair;background:#000;font:600 13px -apple-system,"Segoe UI",sans-serif;color:#ece4d6;-webkit-user-select:none;user-select:none}
 #shot{position:fixed;inset:0;width:100vw;height:100vh;object-fit:fill;display:block}
 .bar{position:fixed;top:14px;left:50%;transform:translateX(-50%);background:rgba(31,29,26,.93);border:1px solid #423d35;border-radius:12px;padding:9px 16px;z-index:9;max-width:86vw;text-align:center}
 .bar b{color:#e0b873}
 #err{color:#f0a6a6}
 #marker{position:fixed;width:24px;height:24px;transform:translate(-50%,-50%);display:none;z-index:8;pointer-events:none}
 #marker::before,#marker::after{content:"";position:absolute;left:50%;top:50%;width:24px;height:3px;background:#ff5b5b;border-radius:2px;box-shadow:0 0 4px #000,0 0 1px #000;transform:translate(-50%,-50%) rotate(45deg)}
 #marker::after{transform:translate(-50%,-50%) rotate(-45deg)}
 #loupe{position:fixed;width:124px;height:124px;border-radius:50%;border:2px solid #e0b873;box-shadow:0 6px 20px rgba(0,0,0,.55);display:none;z-index:8;pointer-events:none;background-repeat:no-repeat;image-rendering:pixelated}
 #loupe::after{content:"";position:absolute;left:50%;top:50%;width:11px;height:11px;transform:translate(-50%,-50%);border:1px solid #e0b873;border-radius:50%}
 #tip{position:fixed;z-index:9;background:rgba(31,29,26,.96);border:1px solid #423d35;border-radius:12px;padding:12px;display:none;min-width:200px}
 #tip .sw{width:100%;height:30px;border-radius:7px;border:1px solid rgba(0,0,0,.25);margin-bottom:8px}
 #tip .meta{font-variant:tabular-nums;margin-bottom:10px;line-height:1.6} #tip .meta s{text-decoration:none;color:#9c9183}
 #tip .row{display:flex;gap:8px}
 button{font:inherit;font-weight:700;border:0;border-radius:9px;padding:8px 12px;cursor:pointer}
 .go{background:#7faf5d;color:#241a02;flex:1}.re{background:#2a2418;color:#e9e0cf}.cn{background:#3a201c;color:#f0c0b0}
</style></head><body>
 <img id="shot" alt="">
 <div class="bar">Calibrate: <b id="lab"></b> &nbsp;&middot;&nbsp; <span id="act"></span> &nbsp;&middot;&nbsp; Esc cancels<span id="err"></span></div>
 <div id="loupe"></div><div id="marker"></div><div id="maskbox" style="position:fixed;border:2px solid #7faf5d;background:rgba(127,175,93,.22);display:none;z-index:7;pointer-events:none;border-radius:3px"></div>
 <div id="tip"><div class="sw" id="sw"></div>
   <div class="meta"><s>colour</s> <b id="hex">&mdash;</b><br><s>at</s> <b id="xy">&mdash;</b></div>
   <div class="row"><button class="go" id="ok">Confirm</button><button class="re" id="redo">Redo</button><button class="cn" id="cancel">&#10005;</button></div></div>
 <div id="cuebar" style="display:none;position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:rgba(31,29,26,.97);border:1px solid #423d35;border-radius:12px;padding:12px 16px;z-index:10;text-align:center;max-width:80vw"><div id="cuemsg" style="margin-bottom:9px;line-height:1.5">Click each letter (and the mouse) to include or exclude it. <b style="color:#7faf5d">Green = kept.</b> <span id="cuepx"></span></div><div class="row" style="justify-content:center"><button class="go" id="cueok">Confirm</button><button class="re" id="cueredo">Start over</button><button class="cn" id="cuecancel">&#10005;</button></div></div>
<script>
 'use strict';
 // ONE session model. Every reload rebuilds ALL state from overlay_image()
 // (the single source of truth: src, label, hint, mode, seq). Nothing --
 // banner text, cue-edit mode, a pending proposal, region drag state, the
 // picked flag -- survives from a previous calibration session, and a
 // session token guards every async return so a stale response from an
 // earlier step can neither repaint nor dead-lock the page. History: the
 // old page kept three of those across sessions and one region session
 // rebuilt the banner's innerHTML, which is exactly how the "stuck on
 // 'Finds pop-up box'" and "clicks stopped working" reports happened.
 const api=()=>window.pywebview&&window.pywebview.api;
 const $=function(id){return document.getElementById(id);};
 const shot=$('shot'),loupe=$('loupe'),marker=$('marker'),tip=$('tip');
 let BOOTN=0;
 let S=null; // the current session; replaced wholesale by boot()
 function blank(){return {seq:0,mode:'pixel',cueEdit:false,picked:false,
   prop:null,natW:0,natH:0,busy:false,ra:null,rb:null,errT:null};}
 function resetDom(){marker.style.display='none';tip.style.display='none';
   loupe.style.display='none';$('maskbox').style.display='none';
   $('sw').style.display='';$('cuebar').style.display='none';
   $('err').textContent='';}
 function flashErr(msg){if(!S)return;$('err').textContent=' · '+msg;
   if(S.errT)clearTimeout(S.errT);
   const ms=Math.min(15000,Math.max(3200,60*String(msg||'').length));
   const tok=S;S.errT=setTimeout(function(){if(tok===S)$('err').textContent='';},ms);}
 async function boot(){
   const my=++BOOTN;
   let d=null;try{d=await api().overlay_image();}catch(e){d=null;}
   if(my!==BOOTN)return;            // a newer session started while loading
   S=blank();resetDom();
   if(!d){S.mode='dead';$('lab').textContent='';
     $('act').textContent='press Esc to close, then reopen the capture';
     flashErr('Could not load the capture - Esc and retry.');return;}
   S.seq=d.seq||0;S.mode=d.mode||(d.region_mode?'region':(d.cue_mode?'cue':'pixel'));
   $('lab').textContent=d.label||'';
   $('act').textContent=d.hint||defaultHint(d);
   if(d.src)shot.src=d.src;
   if(S.mode==='cue'&&d.cue_mode==='edit'&&d.cue_img){enterEdit(d.cue_img,d.cue_px);}
   else if(S.mode==='pixel'&&d.proposed){S.prop=d.proposed;}
 }
 function defaultHint(d){
   if(d.region_mode)return 'drag a box around it, corner to corner';
   if(d.cue_mode==='edit')return 'click each letter (and the mouse) to include/exclude — green = kept';
   if(d.cue_mode)return 'click on the cue word';
   if(d.proposed)return 'the red × is the detected spot — Confirm or Redo';
   return 'click the exact spot, then Confirm';
 }
 shot.onload=function(){if(!S||S.cueEdit)return;
   S.natW=shot.naturalWidth;S.natH=shot.naturalHeight;
   loupe.style.backgroundImage='url('+shot.src+')';
   if(S.prop)placeProposed();};
 function frac(e){const r=shot.getBoundingClientRect();
   return [(e.clientX-r.left)/r.width,(e.clientY-r.top)/r.height];}
 function showTipAt(cx,cy,hex,x,y){marker.style.display='block';
   marker.style.left=cx+'px';marker.style.top=cy+'px';
   $('sw').style.background=hex;$('hex').textContent=hex;$('xy').textContent=x+', '+y;
   let tx=cx+24,ty=cy+24;if(tx>innerWidth-236)tx=cx-220;if(ty>innerHeight-160)ty=cy-160;
   tip.style.left=tx+'px';tip.style.top=ty+'px';tip.style.display='block';
   S.picked=true;loupe.style.display='none';}
 function placeProposed(){if(!S||!S.prop||!S.natW)return;const r=shot.getBoundingClientRect();
   showTipAt(r.left+S.prop.fx*r.width, r.top+S.prop.fy*r.height, S.prop.hex, S.prop.x, S.prop.y);}
 function setCuePx(px){$('cuepx').textContent=px?('('+px+' px kept)'):'';}
 function enterEdit(img,px){S.cueEdit=true;shot.src=img;
   marker.style.display='none';tip.style.display='none';loupe.style.display='none';
   $('act').textContent='click each letter (and the mouse) to include/exclude — green = kept';
   $('cuebar').style.display='block';setCuePx(px);}
 // ---- pointer input (all modes route through the ONE session) ----------
 document.addEventListener('mousemove',function(e){
   if(!S||S.picked||S.cueEdit||!S.natW)return;
   if(S.mode==='region'){if(S.ra&&!S.picked){S.rb=frac(e);drawDrag();loupe.style.display='none';}return;}
   loupe.style.display='block';
   loupe.style.left=(e.clientX+20)+'px';loupe.style.top=(e.clientY+20)+'px';
   const z=9,bw=S.natW*z,bh=S.natH*z;loupe.style.backgroundSize=bw+'px '+bh+'px';
   const f=frac(e);loupe.style.backgroundPosition=(-(f[0]*bw)+62)+'px '+(-(f[1]*bh)+62)+'px';});
 document.addEventListener('mousedown',function(e){
   if(!S||S.mode!=='region'||S.picked||S.busy)return;
   if(tip.contains(e.target))return;
   e.preventDefault();S.ra=frac(e);S.rb=null;});
 document.addEventListener('mouseup',async function(e){
   if(!S||S.mode!=='region'||!S.ra||S.picked||S.busy)return;
   S.rb=frac(e);const tok=S;S.busy=true;
   const da=S.ra.slice(),db=S.rb.slice(); // snapshot: repaint THIS drag only
   let r=null;try{r=await api().overlay_region(
     Math.min(da[0],db[0]),Math.min(da[1],db[1]),
     Math.max(da[0],db[0]),Math.max(da[1],db[1]));}catch(_){r=null;}
   if(tok!==S)return;S.busy=false;
   if(!r||r.error){S.ra=S.rb=null;$('maskbox').style.display='none';
     flashErr((r&&r.error)||'Drag did not register - try again.');return;}
   S.picked=true;S.ra=da;S.rb=db;drawDrag();showRegionConfirm(e.clientX,e.clientY,r.w,r.h);});
 function drawDrag(){if(!S.ra||!S.rb)return;const rc=shot.getBoundingClientRect();
   const x0=Math.min(S.ra[0],S.rb[0])*rc.width+rc.left,y0=Math.min(S.ra[1],S.rb[1])*rc.height+rc.top;
   const w=Math.abs(S.ra[0]-S.rb[0])*rc.width,h=Math.abs(S.ra[1]-S.rb[1])*rc.height;
   const m=$('maskbox');m.style.left=x0+'px';m.style.top=y0+'px';
   m.style.width=Math.max(2,w)+'px';m.style.height=Math.max(2,h)+'px';m.style.display='block';}
 function showRegionConfirm(cx,cy,w,h){$('sw').style.display='none';
   $('hex').textContent=w+'×'+h+' px box';$('xy').textContent='looks right? Confirm';
   let tx=cx+24,ty=cy+24;if(tx>innerWidth-236)tx=cx-220;if(ty>innerHeight-160)ty=cy-160;
   tip.style.left=tx+'px';tip.style.top=ty+'px';tip.style.display='block';
   loupe.style.display='none';}
 document.addEventListener('click',async function(e){
   if(!S||S.busy||S.mode==='dead')return;
   if(tip.contains(e.target)||$('cuebar').contains(e.target))return;
   if(S.mode==='region')return;   // region uses drag, not click
   if(S.picked)return;            // a confirm card is open - use its buttons
   const tok=S;const f=frac(e);
   if(S.cueEdit){S.busy=true;let tr=null;try{tr=await api().cue_toggle(f[0],f[1]);}catch(_){tr=null;}
     if(tok!==S)return;S.busy=false;
     if(tr&&tr.img){shot.src=tr.img;setCuePx(tr.px);}
     else if(tr&&tr.error)flashErr(tr.error);
     else flashErr('No response - click again.');
     return;}
   S.busy=true;let r=null;try{r=await api().overlay_pick(f[0],f[1]);}catch(_){r=null;}
   if(tok!==S)return;S.busy=false;
   if(!r){flashErr('No response - click again, or Esc to cancel.');return;}
   if(r.error){flashErr(r.error);return;}
   if(r.cue_edit){enterEdit(r.img,r.px);}
   else{showTipAt(e.clientX,e.clientY,r.hex,r.x,r.y);}});
 // ---- buttons + keys ----------------------------------------------------
 $('redo').onclick=function(){if(!S)return;
   if(S.prop&&S.mode==='pixel')$('act').textContent='click the exact spot, then Confirm';
   S.picked=false;S.ra=S.rb=null;S.prop=null;
   marker.style.display='none';tip.style.display='none';$('maskbox').style.display='none';};
 $('cancel').onclick=function(){try{api().overlay_cancel();}catch(e){}};
 function doConfirm(){if(!S||S.busy)return;S.busy=true;const tok=S;
   try{Promise.resolve(api().overlay_confirm()).then(function(r){
     // a REJECTED save (r.ok===false) leaves the overlay open: show the
     // exact reasons in the banner, unlock the session so Redo / a new
     // pick / Esc all still work, and keep the previous values intact.
     if(tok!==S)return;
     if(r&&r.ok===false){S.busy=false;
       flashErr((r.reasons&&r.reasons.length?r.reasons.join(' '):'')
                ||r.reason||r.error||'Not saved - try again or Esc.');}
   }).catch(function(){
     if(tok===S){S.busy=false;flashErr('Confirm failed - try again or Esc.');}});}
   catch(e){S.busy=false;flashErr('Confirm failed - try again or Esc.');}}
 $('ok').onclick=doConfirm;
 $('cueok').onclick=doConfirm;
 $('cueredo').onclick=async function(){if(!S)return;
   try{await api().cue_reset();}catch(_){}
   boot();};
 $('cuecancel').onclick=function(){try{api().overlay_cancel();}catch(e){}};
 document.addEventListener('keydown',function(e){
   if(e.key==='Escape'){try{api().overlay_cancel();}catch(_){}return;}
   if(e.key==='Enter'&&S&&S.picked&&!S.busy){doConfirm();}});
 window.__reload=function(){boot();};
 window.addEventListener('pywebviewready',boot);
 boot();
</script></body></html>"""


def build_html():
    navs, panels = [], []

    def nav(tabid, icon, label, active=False, search=""):
        a = " active" if active else ""
        ds = f' data-search="{search}"' if search else ""
        navs.append({"id": tabid, "html": (
            f'<button type="button" class="tab{a}" data-tab="{tabid}"{ds}>'
            f'<span class="ti">{icon}</span><span>{label}</span>'
            f'<span class="navbadge" data-badge="{tabid}"></span></button>')})

    # Run
    nav("run", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M7 5l12 7l-12 7z"/></svg>', "Run", True)
    panels.append(
        '<section class="panel active" id="prun"><div class="phead"><h2>Run</h2>'
        '<p class="chint">Start the macro, tab into Roblox. Ctrl+K also '
        'starts/stops; Esc quits.</p></div>'
        '<div class="stlaunch" id="stlaunch" style="display:none">'
        '<span class="slnote" id="slnote"></span></div>'
        '<div class="scriptcard" id="runscriptcard">'
        '<div class="scr-top"><span class="scr-name" id="rsc_name"></span>'
        '<span class="sth-badge" id="rsc_state">idle</span>'
        '<span class="sth-badge" id="rsc_rev" style="display:none"></span></div>'
        '<div class="scr-step" id="rsc_step"><span class="lbl">current step</span> —</div>'
        '<div class="scr-hud" id="rsc_hud"></div></div>'
        '<div class="calbanner" id="calbanner"></div>'
        '<div class="runbtns"><button type="button" id="startbtn" class="big go">'
        'Start macro</button><button type="button" id="pausebtn" class="big pause">'
        'Pause</button>'
        '<button type="button" id="stopbtn" class="big stop" '
        'disabled>Stop</button><span id="rstate" class="rstate">stopped</span>'
        '<span id="runflow" class="runflow" title="How the macro runs: hover for the full order of events">how it runs</span></div>'
        '<div class="scriptbar" id="scriptbar"><span class="sblab">Mode</span>'
        '<select id="scriptsel" title="Run a built-in mode or one of your Studio scripts"><option value="">Built-in modes</option></select>'
        '<span id="scriptnote" class="scriptnote"></span></div>'
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
    _regions_done = False
    for key, label, desc, _d in PIXEL_FIELDS:
        if key.endswith("_TL_PIXEL") or key.endswith("_BR_PIXEL"):
            # The six corner pairs collapse into three drag-a-box rows: one
            # drag on the overlay fills both corners. Storage keys unchanged.
            if not _regions_done:
                for base, rlabel, rdesc in REGION_FIELDS:
                    calrows.append(
                        f'<div class="calrow" data-regionkey="{base}">'
                        f'<div class="calinfo"><div class="calname">{rlabel}</div>'
                        f'<div class="caldesc">{rdesc}</div></div>'
                        f'<div class="calval">'
                        f'<img class="rgthumb" id="rgimg_{base}" alt="" '
                        f'style="display:none">'
                        f'<span class="frstat" id="rg_{base}">not set</span></div>'
                        f'<button type="button" class="btn2 regbtn" '
                        f'data-regionkey="{base}" data-reglabel="{rlabel}">'
                        f'Draw box</button></div>')
                _regions_done = True
            continue
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
        if key == "CAP_LEFT_PIXEL":
            # Test Capacity Calibration: fresh screenshot + the exact
            # runtime math on the two capacity rows above
            calrows.append(
                '<div class="calrow" id="capTestRow">'
                '<div class="calinfo"><div class="calname">Test capacity '
                'calibration</div>'
                '<div class="caldesc">One fresh screenshot read with the '
                'exact runtime math: right-tip gold test, fill fraction '
                'over the bar band, endpoint-pair validation.</div></div>'
                '<button type="button" class="btn2" id="capTest">'
                'Test capacity calibration</button></div>'
                '<div class="detout" id="capTestOut"></div>')
    panels.append(
        '<section class="panel" id="pcal"><div class="phead"><h2>Calibrate pixels</h2>'
        '<p class="chint">Open Prospecting in Roblox with the HUD visible, then run '
        '<b>Guided calibration</b> below. <b>Test detection</b> shows live whether '
        'the macro sees everything correctly.</p></div>'
        '<div class="calbanner" id="calbanner2"></div>'
        '<div class="autocal">'
        '  <button type="button" id="wizbtn" class="btn">✨ Guided calibration (recommended)</button>'
        '  <button type="button" id="dettest" class="btn2">Test detection (live)</button>'
        '  <button type="button" id="earntest" class="btn2">Test money/shards read</button>'
        '  <button type="button" id="findtest" class="btn2">Test find pop-up read</button>'
        '  <button type="button" class="btn2" data-faq-open="">FAQ &amp; troubleshooting</button>'
        '  <div class="detout" id="detout"></div>'
        '  <div class="detout" id="earnout"></div>'
        '  <div class="detout" id="findout"></div>'
        '</div>'
        '<div class="caldiv"><span>or calibrate manually</span></div>'
        '<p class="chint" style="margin:0 0 10px">Click a <b>Calibrate</b> button, then '
        'either click the exact spot in-game, or hover it and press <b>Enter</b>. '
        'Press <b>Esc</b> to cancel. Do them all, then <b>Save calibration</b>.</p>'
        f'<div class="calrows">{"".join(calrows)}</div>'
        '<div class="caldiv"><span>Fortune River recovery (optional, advanced)</span></div>'
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
        '<div class="calrow"><div class="calinfo"><div class="calname">Auto Pan button, ON state</div>'
        '<div class="caldesc">With the game\'s Auto Pan turned ON (green), click the button. '
        'Saves its position and ON colour (used by Tracker + relics).</div></div>'
        '<div class="calval"><span class="frstat" id="frstat_apon">not set</span>'
        '<span class="calsw2" id="frsw_apon"></span></div>'
        '<button type="button" class="btn2 frbtn" data-frk="apon">Calibrate</button></div>'
        '<div class="calrow"><div class="calinfo"><div class="calname">Auto Pan button, OFF state</div>'
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
        '</div>'
        '<div class="advcal"><div class="phead" style="margin-top:6px">'
        '<h2>Advanced cue matching <span class="advbeta">required</span></h2>'
        '<p class="chint">The primary prompt detector: instead of checking a small '
        'white box, it matches the EXACT letter shape of each cue, so a player '
        'standing in white can\'t trigger it, and it tolerates minor visual '
        'variation better than a single exact pixel. All three captures are '
        'REQUIRED for readiness -- the single-pixel checks remain only as a '
        'fallback while a mask is missing or disabled. Open Prospecting with the '
        'cue on screen, then Capture. Re-capture if you change window size or '
        'resolution.</p></div>'
        '<label class="row"><span class="lbl">Use advanced cue matching '
        '(required; switching it off keeps readiness blocked)</span>'
        '<span class="switch"><input type="checkbox" id="advcue"><span class="track">'
        '<span class="knob"></span></span></span></label>'
        '<label class="row"><span class="lbl">Masks only \u2014 no pixel fallback (for testing)</span>'
        '<span class="switch"><input type="checkbox" id="cueonly"><span class="track">'
        '<span class="knob"></span></span></span></label>'
        '<label class="row"><span class="lbl">White sensitivity (lower = more permissive)</span>'
        '<input type="number" id="cuethresh" value="160" min="80" max="240" style="width:90px"></label>'
        '<button type="button" id="cuewizbtn" class="btn" style="margin-top:10px">\u2728 Guided cue capture</button>'
        '<div id="cuewiz" class="cuewiz" style="display:none">'
        '<div class="cwhead"><span id="cwstepn">Step 1 of 3</span>'
        '<button type="button" class="cwx" id="cwclose">\u2715</button></div>'
        '<div class="cwtitle" id="cwtitle"></div>'
        '<div class="cwtip" id="cwtip"></div>'
        '<div class="cwprevwrap"><img id="cwprev" class="cwprev" alt="capture preview">'
        '<div class="cwph" id="cwph">Do the step in-game, then Capture.</div></div>'
        '<div class="cwstat" id="cwstat"></div>'
        '<div class="cwbtns"><button type="button" id="cwcap" class="btn">Capture</button>'
        '<button type="button" id="cwprevb" class="btn2">\u2039 Back</button>'
        '<button type="button" id="cwnext" class="btn2">Next \u203a</button></div></div>'
        '<div class="cuecap" id="cuecap"></div></div>'
        '</section>')

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

    # Studio (custom block scripts)
    nav("studio", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="13.5" width="8" height="6.5" rx="1.5"/><rect x="13" y="13.5" width="8" height="6.5" rx="1.5"/><rect x="8" y="4" width="8" height="6.5" rx="1.5"/></svg>', "Studio")
    panels.append(
        '<section class="panel" id="pstudio"><div class="phead"><h2>Studio</h2>'
        '<p class="chint">Build your own farming mode from Prospecting blocks: '
        'digs, walks, shakes, prompts and waits. No code needed, the safety '
        'nets stay on, and a script runs and counts pans exactly like a '
        'built-in mode. Share one as a single .ppscript file.</p></div>'
        '<div id="sthdr" class="sthdr" style="display:none"></div>'
        '<div id="bpwrap" class="sthdr spwrap" style="display:none">'
        '<div class="sth-top"><span class="sth-name">Build settings from the graph</span>'
        '<span class="sth-badge" title="The engine reads its configuration when a run starts; a live run keeps what it started with">applies at the next start</span>'
        '<input id="bpsearch" class="spsearch" placeholder="search"></div>'
        '<div id="bpgrid" class="spgrid"></div></div>'
        '<div class="stbtns">'
        '<button type="button" id="stopen" class="btn">Open Studio</button>'
        '<button type="button" id="stnew" class="btn2">New script\u2026</button>'
        '<button type="button" id="stimport" class="btn2">Import script\u2026</button>'
        '</div>'
        '<div id="stgrid" class="stgrid"></div></section>')

    # Studio Script (Studio-launch only: general automation, same engine)
    nav("script", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M7 8l-4 4l4 4"/><path d="M17 8l4 4l-4 4"/><path d="M14 4l-4 16"/></svg>', "Script")
    panels.append(
        '<section class="panel" id="pscript"><div class="phead"><h2>Studio Script</h2>'
        '<p class="chint">The Studio Script pushed from Prospector Studio runs '
        'through the same engine, safety rails and hotkeys as every other '
        'mode. Author and edit it in Prospector Studio; start it from the '
        'Run tab or with the start hotkey.</p></div>'
        '<div id="schdr" class="sthdr" style="display:none"></div>'
        '<div class="scriptcard" id="scstatus">'
        '<div class="scr-top"><span class="scr-name" id="ssc_name"></span>'
        '<span class="sth-badge" id="ssc_state">idle</span></div>'
        '<div class="scr-step" id="ssc_step"><span class="lbl">current step</span> \u2014</div>'
        '<div class="scr-hud" id="ssc_hud"></div></div>'
        '<div id="spwrap" class="sthdr spwrap" style="display:none">'
        '<div class="sth-top"><span class="sth-name">Script settings</span>'
        '<span class="sth-badge" title="The engine reads its configuration when a run starts; a live run keeps what it started with">applies at the next start</span>'
        '<input id="spsearch" class="spsearch" placeholder="search"></div>'
        '<div id="spgrid" class="spgrid"></div></div>'
        '<div id="scgrid" class="stgrid"></div></section>')

    nav("keys", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6 9.5h.01M9.5 9.5h.01M13 9.5h.01M16.5 9.5h.01M6.5 13.5h11"/></svg>', "Keybinds")

    # Trust Center: permissions, data, network, build identity, source.
    # Rendered by JS from the same lite_trust registry the setup wizard uses.
    nav("trust", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3v5c0 4.4-3 8.4-7 10-4-1.6-7-5.6-7-10V6z"/><path d="M9.2 12.2l2 2 3.6-4"/></svg>', "Trust Center")
    panels.append(
        '<section class="panel" id="ptrust"><div class="phead"><h2>Trust Center</h2>'
        '<p class="chint">Every permission, every byte stored, every network '
        'path &mdash; with live status, real tests and the exact source '
        'behind each capability. Nothing here is marketing: each claim is '
        'backed by a check you can run.</p></div>'
        '<div id="tcbody"><p class="chint">Loading&hellip;</p></div></section>')
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
         "Hold W, build speed toward land, click RIGHT before the edge.",
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
            f'<div><h3>{_sn}</h3><p>{_tag}</p></div>'
            f'<span class="stagebadge" id="sb_{_sid}"></span></div>'
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
                ("swalk", "S walk back", TAB_ICON.get("Walk back into water", ""), 288),
                ("glide", "W glide", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8h11a3 3 0 1 0-3-3M3 13h15a3 3 0 1 1-3 3M3 18h7"/></svg>', 470),
                ("shake", "click! shake", TAB_ICON.get("Shake", ""), 648),
                ("land", "land & prove", TAB_ICON.get("Return to land (dig-probe)", ""), 854)])
        + '<path d="M133 73h128" class="cya"/><path d="M315 73h128" class="cya"/>'
          '<path d="M497 73h124" class="cya"/><path d="M675 73h152" class="cya"/>'
          '<path d="M854 100v34H150" class="cya cyaback"/>'
          '<text x="500" y="150" class="cyloop">momentum carries you out, the loop restarts</text>'
        '</svg></div>')
    panels.append(
        '<section class="panel" id="pcycle"><div class="phead"><h2>Cycle</h2>'
        '<p class="chint">This IS your pan loop. Click any stage in the '
        'diagram to jump to its knobs; the numbers on the diagram are live. '
        'Safety nets sit below the loop.</p></div>'
        + '<div class="calbanner cycwarn" id="cycbanner"></div>'
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
        extra = ""
        if title == "Notifications":
            extra = ('<div class="whbox">'
                     '<p class="chint" style="margin:0 0 9px">Notifications are '
                     'OFF until you add your own Discord webhook. The app never '
                     'sends anything anywhere until you do. One-time setup:</p>'
                     '<ol class="whsteps">'
                     '<li>In your Discord server: Server Settings &rarr; '
                     'Integrations &rarr; Webhooks &rarr; New Webhook &rarr; '
                     'Copy Webhook URL.</li>'
                     '<li>Paste the URL below and press Save.</li>'
                     '<li>Turn on <b>Discord notifications</b>, then Send '
                     'test.</li></ol>'
                     '<div class="whactions" style="align-items:center">'
                     '<input id="whurl" type="password" autocomplete="off" '
                     'spellcheck="false" placeholder="https://discord.com/api/webhooks/…" '
                     'style="flex:1;min-width:130px;background:var(--field);'
                     'color:var(--txt);border:1px solid var(--line2);'
                     'border-radius:8px;padding:8px 10px;font:inherit;font-size:12.5px">'
                     '<button type="button" id="whurlsave" class="btn2">Save</button>'
                     '<button type="button" id="testnotify" class="btn2">Send test</button></div>'
                     '<div class="detout" id="whurlout"></div>'
                     '<p class="chint" style="margin:8px 0 0">Sent only to the '
                     'webhook you configure: the event, run stats, the name '
                     'above and (if enabled) a screenshot. Never your IP, '
                     'location or system details. Delete the URL to switch '
                     'notifications off completely.</p></div>'
                     '<div class="detout" id="notifyout"></div>')
        panels.append(
            f'<section class="panel" id="p_{title}"><div class="phead"><h2>{title}</h2>'
            f'<p class="chint">{hint}</p></div>'
            f'<div class="rows">{"".join(rows)}</div>{extra}</section>')

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
    nav("settings", '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>', "Settings")
    panels.append(
        '<section class="panel" id="p_settings"><div class="phead"><h2>Settings</h2>'
        '<p class="chint">App appearance and behaviour. Saved on this computer; '
        'these do not change your macro tuning or your builds.</p></div>'
        '<div class="rows">'
        '<label class="row"><span class="lbl">Compact mode (mini sidebar, opens on hover)</span>'
        '<span class="switch"><input type="checkbox" id="set_compact">'
        '<span class="track"><span class="knob"></span></span></span></label>'
        '<label class="row"><span class="lbl">Wood texture background</span>'
        '<span class="switch"><input type="checkbox" id="set_wood" checked>'
        '<span class="track"><span class="knob"></span></span></span></label>'
        '<label class="row"><span class="lbl">Reduce animations</span>'
        '<span class="switch"><input type="checkbox" id="set_reduce">'
        '<span class="track"><span class="knob"></span></span></span></label>'
        '<label class="row"><span class="lbl">Skip the setup wizard automatically on launch</span>'
        '<span class="switch"><input type="checkbox" id="set_skipwiz">'
        '<span class="track"><span class="knob"></span></span></span></label>'
        '<label class="row"><span class="lbl">Open tutorial whenever Prospector Lite opens</span>'
        '<span class="switch"><input type="checkbox" id="set_tutauto" checked>'
        '<span class="track"><span class="knob"></span></span></span></label>'
        '</div>'
        '<p class="chint" style="margin-top:16px">Press Ctrl+Z to undo a setting '
        'change and Ctrl+Y to redo. Undo covers slider, box and preset changes.</p>'
        '<p class="chint" style="margin-top:6px">Something behaving oddly? '
        '<button type="button" class="btn2" data-faq-open="">FAQ &amp; '
        'troubleshooting</button></p>'
        '<div class="ownercard" id="ownercard">'
        '<h3>Settings ownership</h3>'
        '<p class="chint">Every setting has one owner, so the two macro modes '
        'never trample each other. <b>Classic</b> owns the built-in cycle '
        'tuning: modes, dig/shake/walk timing, recovery, relics. '
        '<b>Studio</b> owns which build is active, the CLASSIC | STUDIO '
        'switch and editor flags — stored with the script library, never in '
        'the classic config. <b>Shared</b> is used by every run: '
        'calibration, game window, notifications, auto-stop, earnings '
        'tracking, keybinds and appearance.</p>'
        '<div class="ownerbtns">'
        '<button type="button" class="btn2" id="rst_classic">Reset Classic…</button>'
        '<button type="button" class="btn2" id="rst_studio">Reset Studio…</button>'
        '<button type="button" class="btn2" id="rst_shared">Reset Shared…</button>'
        '</div>'
        '<label class="row"><span class="lbl">Reset Shared also clears calibration '
        '(you would have to calibrate again)</span>'
        '<span class="switch"><input type="checkbox" id="rst_cal">'
        '<span class="track"><span class="knob"></span></span></span></label>'
        '</div>'
        '</section>')
    nav_html = {n["id"]: n["html"] for n in navs}
    PINNED = ["run", "cycle", "builds", "cal", "relics", "hist", "studio", "keys", "trust", "settings"]
    GROUPS = [
        ("Modes", ["Treasure chest", "Shards", "Geodes"]),
        ("Tracking", ["Earnings", "Tracker"]),
        ("Alerts and limits", ["Notifications", "Auto-stop"]),
        ("Advanced", ["Advanced tuning"]),
        ("Setup", ["Window", "Relic behaviour"]),
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
     if(digsNeed===null)notes.push('digs-to-fill unknown (save your stats in Auto-build), showing Max digs');
   }
   seg('swalk','S walk to the Pan cue',PAN_BACK,PAN_BACK,
     {parts:[['PAN_BACK_MAX_MS',n('PAN_BACK_MAX_MS')],['EASY_WATER_BACK_MS',n('EASY_WATER_BACK_MS')]],
      note:'budget, the cue usually fires sooner',cue:true});
   if(XTRA>0)seg('swalk','deeper (extra S)',XTRA,XTRA,
     {parts:[['WATER_EXTRA_BACK_MS',n('WATER_EXTRA_BACK_MS')],['EASY_WATER_BACK_MS',n('EASY_WATER_BACK_MS')]]});
   if(SDLY>0)seg('glide','start delay',SDLY,SDLY,
     {parts:[['SHAKE_START_DELAY_MS',n('SHAKE_START_DELAY_MS')],['EASY_SHAKE_DELAY_MS',n('EASY_SHAKE_DELAY_MS')]]});
   if(b('SHAKE_MOMENTUM_W')){
     if(n('SHAKE_W_LEAD_MS')>0)seg('glide','W momentum glide',n('SHAKE_W_LEAD_MS'),n('SHAKE_W_LEAD_MS'),
       {parts:[['SHAKE_W_LEAD_MS',n('SHAKE_W_LEAD_MS')]]});
   }else notes.push('Hold W during shake is OFF, no momentum, no glide');
   const cad=Math.max(1,n('SHAKE_CLICK_MS')+n('SHAKE_CLICK_GAP_MS'));
   if(n('SHAKE_CLICKS')>0){
     const d=cad*n('SHAKE_CLICKS');
     seg('shake','shake, exactly '+n('SHAKE_CLICKS')+' clicks',d,d,
       {parts:[['SHAKE_CLICKS',n('SHAKE_CLICKS')],['SHAKE_CLICK_MS',n('SHAKE_CLICK_MS')],
               ['SHAKE_CLICK_GAP_MS',n('SHAKE_CLICK_GAP_MS')]],ticks:cad});
   }else{
     const lo=drain!==null?Math.min(drain,n('SHAKE_HOLD_MS')):n('SHAKE_HOLD_MS');
     seg('shake',drain!==null?'shake, drain (est. from your stats)':'shake, until empty (cap)',
       lo,n('SHAKE_HOLD_MS'),
       {parts:[['SHAKE_CLICK_MS',n('SHAKE_CLICK_MS')],['SHAKE_CLICK_GAP_MS',n('SHAKE_CLICK_GAP_MS')],
               ['SHAKE_HOLD_MS',n('SHAKE_HOLD_MS')],['SHAKE_BAIL_MS',n('SHAKE_BAIL_MS')]],
        ticks:cad,bail:n('SHAKE_BAIL_MS')});
     if(drain===null)notes.push('drain time unknown (save your stats in Auto-build), showing the cap');
   }
   if(POST>0)seg('land','settle onto land',POST,POST,
     {parts:[['POST_SHAKE_SETTLE_MS',n('POST_SHAKE_SETTLE_MS')],['EASY_FIRST_DIG_DELAY_MS',n('EASY_FIRST_DIG_DELAY_MS')]]});
   let est=0,cap=0;
   S.forEach(x=>{est+=x.lo;cap+=x.hi;});
   return {segs:S,est,cap,notes,pph:est>0?3600000/est:0,drain,digsNeed};
 }/*CYCMODEL-END*/'''


HTML = r"""<!doctype html><html><head><meta charset="utf-8"><!-- system fonts only: no network fetch --><style>
 :root{--bg:#171310;--bg2:#1c1714;--panel:#221c18;--head:#181310;--line:#332a23;
  --line2:#463a2f;--txt:#ece0d0;--mut:#9c8e7c;--dim:#6a5d4d;--accent:#a8794a;
  --accent-lit:#caa06e;--accent2:#8a9b6a;--teal-lit:#9bc07e;--green:#7faf5d;
  --field:#161210;--nav:#141009;--sand-dim:rgba(168,121,74,.14);
  --sand-glow:rgba(168,121,74,.24);--ease:cubic-bezier(.22,1,.36,1)}
 *{box-sizing:border-box} html,body{height:100%;margin:0}
 body{background:var(--bg) url("data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIzMjAiIGhlaWdodD0iMzAwIj48cmVjdCB3aWR0aD0iMzIwIiBoZWlnaHQ9IjMwMCIgZmlsbD0iIzE2MTIxMCIvPjxmaWx0ZXIgaWQ9ImciPjxmZVR1cmJ1bGVuY2UgdHlwZT0iZnJhY3RhbE5vaXNlIiBiYXNlRnJlcXVlbmN5PSIwLjg1IDAuMDE0IiBudW1PY3RhdmVzPSIzIiBzZWVkPSIxMSIgc3RpdGNoVGlsZXM9InN0aXRjaCIgcmVzdWx0PSJuIi8+PGZlQ29sb3JNYXRyaXggaW49Im4iIHR5cGU9Im1hdHJpeCIgdmFsdWVzPSIwIDAgMCAwIDAuMTQgMCAwIDAgMCAwLjExNSAwIDAgMCAwIDAuMDkgMCAwIDAgMC4xNiAwIi8+PC9maWx0ZXI+PHJlY3Qgd2lkdGg9IjMyMCIgaGVpZ2h0PSIzMDAiIGZpbHRlcj0idXJsKCNnKSIvPjxnIHN0cm9rZT0iIzBkMGEwNyIgc3Ryb2tlLXdpZHRoPSIyIiBvcGFjaXR5PSIwLjg1Ij48bGluZSB4MT0iMCIgeTE9IjYwIiB4Mj0iMzIwIiB5Mj0iNjAiLz48bGluZSB4MT0iMCIgeTE9IjEyMCIgeDI9IjMyMCIgeTI9IjEyMCIvPjxsaW5lIHgxPSIwIiB5MT0iMTgwIiB4Mj0iMzIwIiB5Mj0iMTgwIi8+PGxpbmUgeDE9IjAiIHkxPSIyNDAiIHgyPSIzMjAiIHkyPSIyNDAiLz48L2c+PGcgc3Ryb2tlPSIjMmIyMTE5IiBzdHJva2Utd2lkdGg9IjEiIG9wYWNpdHk9IjAuNTUiPjxsaW5lIHgxPSIwIiB5MT0iNjIiIHgyPSIzMjAiIHkyPSI2MiIvPjxsaW5lIHgxPSIwIiB5MT0iMTIyIiB4Mj0iMzIwIiB5Mj0iMTIyIi8+PGxpbmUgeDE9IjAiIHkxPSIxODIiIHgyPSIzMjAiIHkyPSIxODIiLz48bGluZSB4MT0iMCIgeTE9IjI0MiIgeDI9IjMyMCIgeTI9IjI0MiIvPjwvZz48L3N2Zz4=");background-size:320px 300px;color:var(--txt);font:13.5px/1.5 "Inter",-apple-system,
  BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;display:flex;flex-direction:column}
 .topbar{flex:0 0 auto;background:var(--head) url("data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIzMjAiIGhlaWdodD0iMzAwIj48cmVjdCB3aWR0aD0iMzIwIiBoZWlnaHQ9IjMwMCIgZmlsbD0iIzE2MTIxMCIvPjxmaWx0ZXIgaWQ9ImciPjxmZVR1cmJ1bGVuY2UgdHlwZT0iZnJhY3RhbE5vaXNlIiBiYXNlRnJlcXVlbmN5PSIwLjg1IDAuMDE0IiBudW1PY3RhdmVzPSIzIiBzZWVkPSIxMSIgc3RpdGNoVGlsZXM9InN0aXRjaCIgcmVzdWx0PSJuIi8+PGZlQ29sb3JNYXRyaXggaW49Im4iIHR5cGU9Im1hdHJpeCIgdmFsdWVzPSIwIDAgMCAwIDAuMTQgMCAwIDAgMCAwLjExNSAwIDAgMCAwIDAuMDkgMCAwIDAgMC4xNiAwIi8+PC9maWx0ZXI+PHJlY3Qgd2lkdGg9IjMyMCIgaGVpZ2h0PSIzMDAiIGZpbHRlcj0idXJsKCNnKSIvPjxnIHN0cm9rZT0iIzBkMGEwNyIgc3Ryb2tlLXdpZHRoPSIyIiBvcGFjaXR5PSIwLjg1Ij48bGluZSB4MT0iMCIgeTE9IjYwIiB4Mj0iMzIwIiB5Mj0iNjAiLz48bGluZSB4MT0iMCIgeTE9IjEyMCIgeDI9IjMyMCIgeTI9IjEyMCIvPjxsaW5lIHgxPSIwIiB5MT0iMTgwIiB4Mj0iMzIwIiB5Mj0iMTgwIi8+PGxpbmUgeDE9IjAiIHkxPSIyNDAiIHgyPSIzMjAiIHkyPSIyNDAiLz48L2c+PGcgc3Ryb2tlPSIjMmIyMTE5IiBzdHJva2Utd2lkdGg9IjEiIG9wYWNpdHk9IjAuNTUiPjxsaW5lIHgxPSIwIiB5MT0iNjIiIHgyPSIzMjAiIHkyPSI2MiIvPjxsaW5lIHgxPSIwIiB5MT0iMTIyIiB4Mj0iMzIwIiB5Mj0iMTIyIi8+PGxpbmUgeDE9IjAiIHkxPSIxODIiIHgyPSIzMjAiIHkyPSIxODIiLz48bGluZSB4MT0iMCIgeTE9IjI0MiIgeDI9IjMyMCIgeTI9IjI0MiIvPjwvZz48L3N2Zz4=");background-size:320px 300px;border-bottom:1px solid var(--line);
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
 .side{flex:0 0 208px;background:var(--nav) url("data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIzMjAiIGhlaWdodD0iMzAwIj48cmVjdCB3aWR0aD0iMzIwIiBoZWlnaHQ9IjMwMCIgZmlsbD0iIzE2MTIxMCIvPjxmaWx0ZXIgaWQ9ImciPjxmZVR1cmJ1bGVuY2UgdHlwZT0iZnJhY3RhbE5vaXNlIiBiYXNlRnJlcXVlbmN5PSIwLjg1IDAuMDE0IiBudW1PY3RhdmVzPSIzIiBzZWVkPSIxMSIgc3RpdGNoVGlsZXM9InN0aXRjaCIgcmVzdWx0PSJuIi8+PGZlQ29sb3JNYXRyaXggaW49Im4iIHR5cGU9Im1hdHJpeCIgdmFsdWVzPSIwIDAgMCAwIDAuMTQgMCAwIDAgMCAwLjExNSAwIDAgMCAwIDAuMDkgMCAwIDAgMC4xNiAwIi8+PC9maWx0ZXI+PHJlY3Qgd2lkdGg9IjMyMCIgaGVpZ2h0PSIzMDAiIGZpbHRlcj0idXJsKCNnKSIvPjxnIHN0cm9rZT0iIzBkMGEwNyIgc3Ryb2tlLXdpZHRoPSIyIiBvcGFjaXR5PSIwLjg1Ij48bGluZSB4MT0iMCIgeTE9IjYwIiB4Mj0iMzIwIiB5Mj0iNjAiLz48bGluZSB4MT0iMCIgeTE9IjEyMCIgeDI9IjMyMCIgeTI9IjEyMCIvPjxsaW5lIHgxPSIwIiB5MT0iMTgwIiB4Mj0iMzIwIiB5Mj0iMTgwIi8+PGxpbmUgeDE9IjAiIHkxPSIyNDAiIHgyPSIzMjAiIHkyPSIyNDAiLz48L2c+PGcgc3Ryb2tlPSIjMmIyMTE5IiBzdHJva2Utd2lkdGg9IjEiIG9wYWNpdHk9IjAuNTUiPjxsaW5lIHgxPSIwIiB5MT0iNjIiIHgyPSIzMjAiIHkyPSI2MiIvPjxsaW5lIHgxPSIwIiB5MT0iMTIyIiB4Mj0iMzIwIiB5Mj0iMTIyIi8+PGxpbmUgeDE9IjAiIHkxPSIxODIiIHgyPSIzMjAiIHkyPSIxODIiLz48bGluZSB4MT0iMCIgeTE9IjI0MiIgeDI9IjMyMCIgeTI9IjI0MiIvPjwvZz48L3N2Zz4=");background-size:320px 300px;border-right:1px solid var(--line);
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
 .stagebadge{margin-left:auto;align-self:center;min-width:18px;height:18px;border-radius:50%;display:none;align-items:center;justify-content:center;font-size:11px;font-weight:800;background:#e0b34a;color:#1a1005;flex:none}
 .stagebadge.show{display:inline-flex}
 .crow .ctl{display:flex;gap:10px;align-items:center}
 .crng{width:190px;accent-color:var(--accent)}
 .cnum{width:78px}
 .cygraph{max-width:980px;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin-bottom:16px;position:sticky;top:0;z-index:6;box-shadow:0 12px 30px -20px rgba(0,0,0,.9)}
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
 .acard{background:var(--panel);border:1px solid var(--line2);border-radius:11px;padding:11px 13px;box-shadow:inset 0 1px 0 rgba(255,255,255,.04),0 3px 9px -4px rgba(0,0,0,.55)}
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
 .grouphdr{display:flex;align-items:center;justify-content:space-between;width:100%;background:#1d1711;color:var(--dim);text-transform:uppercase;letter-spacing:.05em;font-size:10.5px;font-weight:700;padding:9px 11px;border-radius:8px;border:1px solid rgba(0,0,0,.22);box-shadow:inset 0 1px 0 rgba(255,255,255,.035),0 1px 2px rgba(0,0,0,.3);text-shadow:0 1px 1px rgba(0,0,0,.45)}
 .grouphdr:hover{color:var(--mut)}
 .grouphdr .chev{transition:transform .15s var(--ease);opacity:.55;font-size:15px;font-weight:400}
 .navgroup:not(.collapsed) .grouphdr .chev{transform:rotate(90deg)}
 .groupkids{overflow:hidden;max-height:440px;opacity:1;transition:max-height .27s cubic-bezier(.22,1,.36,1),opacity .18s ease}
 .grouphdr .chev{transition:transform .22s ease}
 .navgroup.collapsed .groupkids{max-height:0;opacity:0}
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
 .content{flex:1;overflow-y:auto;padding:0;overscroll-behavior:contain;background:var(--bg)}
 .cwrap{padding:16px 32px 44px;min-height:100%;box-sizing:border-box;background:var(--bg) url("data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIzMjAiIGhlaWdodD0iMzAwIj48cmVjdCB3aWR0aD0iMzIwIiBoZWlnaHQ9IjMwMCIgZmlsbD0iIzE2MTIxMCIvPjxmaWx0ZXIgaWQ9ImciPjxmZVR1cmJ1bGVuY2UgdHlwZT0iZnJhY3RhbE5vaXNlIiBiYXNlRnJlcXVlbmN5PSIwLjg1IDAuMDE0IiBudW1PY3RhdmVzPSIzIiBzZWVkPSIxMSIgc3RpdGNoVGlsZXM9InN0aXRjaCIgcmVzdWx0PSJuIi8+PGZlQ29sb3JNYXRyaXggaW49Im4iIHR5cGU9Im1hdHJpeCIgdmFsdWVzPSIwIDAgMCAwIDAuMTQgMCAwIDAgMCAwLjExNSAwIDAgMCAwIDAuMDkgMCAwIDAgMC4xNiAwIi8+PC9maWx0ZXI+PHJlY3Qgd2lkdGg9IjMyMCIgaGVpZ2h0PSIzMDAiIGZpbHRlcj0idXJsKCNnKSIvPjxnIHN0cm9rZT0iIzBkMGEwNyIgc3Ryb2tlLXdpZHRoPSIyIiBvcGFjaXR5PSIwLjg1Ij48bGluZSB4MT0iMCIgeTE9IjYwIiB4Mj0iMzIwIiB5Mj0iNjAiLz48bGluZSB4MT0iMCIgeTE9IjEyMCIgeDI9IjMyMCIgeTI9IjEyMCIvPjxsaW5lIHgxPSIwIiB5MT0iMTgwIiB4Mj0iMzIwIiB5Mj0iMTgwIi8+PGxpbmUgeDE9IjAiIHkxPSIyNDAiIHgyPSIzMjAiIHkyPSIyNDAiLz48L2c+PGcgc3Ryb2tlPSIjMmIyMTE5IiBzdHJva2Utd2lkdGg9IjEiIG9wYWNpdHk9IjAuNTUiPjxsaW5lIHgxPSIwIiB5MT0iNjIiIHgyPSIzMjAiIHkyPSI2MiIvPjxsaW5lIHgxPSIwIiB5MT0iMTIyIiB4Mj0iMzIwIiB5Mj0iMTIyIi8+PGxpbmUgeDE9IjAiIHkxPSIxODIiIHgyPSIzMjAiIHkyPSIxODIiLz48bGluZSB4MT0iMCIgeTE9IjI0MiIgeDI9IjMyMCIgeTI9IjI0MiIvPjwvZz48L3N2Zz4=");background-size:320px 300px}
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
  border-radius:8px;padding:9px 11px;text-align:right;color-scheme:dark;transition:border-color .15s,box-shadow .15s}
 input[type=number]:focus,input[type=text]:focus,.rname:focus{outline:0;border-color:var(--accent);
  box-shadow:0 0 0 3px rgba(168,121,74,.28)}
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
 .log{background:#080706;border:1px solid var(--line);border-radius:12px;padding:13px 15px;-webkit-user-select:text;user-select:text;cursor:text;
  height:calc(100vh - 320px);overflow-y:auto;white-space:pre-wrap;font:12px ui-monospace,
  Menlo,monospace;color:#c2c8ba;margin:0}
 .calrows{max-width:720px}
 .navbadge{margin-left:auto;min-width:16px;height:16px;border-radius:50%;display:none;align-items:center;justify-content:center;font-size:11px;font-weight:800;flex-shrink:0;padding:0 3px}
 .navbadge.show{display:inline-flex}
 .navbadge.red{background:#e05c5c;color:#fff}
 .navbadge.yellow{background:#e0b34a;color:#1a1005}
 .rows,.cstage,.bcard{box-shadow:inset 0 1px 0 rgba(255,255,255,.05),0 4px 12px -6px rgba(0,0,0,.6)}
 .acard{box-shadow:inset 0 1px 0 rgba(255,255,255,.04),0 2px 6px rgba(0,0,0,.3)}
 .cygraph{box-shadow:inset 0 1px 0 rgba(255,255,255,.06),0 13px 22px -8px rgba(0,0,0,.86)}
 .topbar{box-shadow:0 7px 20px -12px rgba(0,0,0,.72)}
 .side{box-shadow:inset -11px 0 16px -13px rgba(0,0,0,.66)}
 .tab:hover{background:rgba(255,255,255,.035)}
 .tab.active{box-shadow:inset 0 1px 3px rgba(0,0,0,.5),inset 0 0 0 1px rgba(255,255,255,.03)}
 .btn{box-shadow:0 2px 4px rgba(0,0,0,.42),inset 0 1px 0 rgba(255,255,255,.16)}
 .btn:active{transform:translateY(1px);box-shadow:0 1px 2px rgba(0,0,0,.4)}
 .btn2{box-shadow:0 2px 4px rgba(0,0,0,.36),inset 0 1px 0 rgba(255,255,255,.05)}
 .btn2:active{transform:translateY(1px)}
 .big{box-shadow:0 3px 7px rgba(0,0,0,.42),inset 0 1px 0 rgba(255,255,255,.14)}
 .big:active{transform:translateY(1px);box-shadow:0 1px 3px rgba(0,0,0,.4)}
 .track{box-shadow:inset 0 1px 3px rgba(0,0,0,.55),inset 0 -1px 0 rgba(255,255,255,.04)}
 .knob{box-shadow:0 1px 2px rgba(0,0,0,.55),0 0 0 1px rgba(0,0,0,.18)}
 input[type=number],input[type=text],.chex,.rname{box-shadow:inset 0 1px 2px rgba(0,0,0,.4)}
 .log{box-shadow:inset 0 2px 10px rgba(0,0,0,.6)}
 .tab{background:#221c16;border:1px solid rgba(0,0,0,.25);box-shadow:inset 0 1px 0 rgba(255,255,255,.05),0 1px 2px rgba(0,0,0,.4);text-shadow:0 1px 1px rgba(0,0,0,.55)}
 .tab .ti{opacity:.8}
 .tab:hover{background:#2c241b;border-color:rgba(0,0,0,.3)}
 .tab.active{background:rgba(168,121,74,.2);border-color:rgba(168,121,74,.38);box-shadow:inset 0 1px 3px rgba(0,0,0,.45)}
 .cstage,.bcard{position:relative}
 .cstage::after,.bcard::after{content:"";position:absolute;top:0;left:0;right:0;height:24px;pointer-events:none;background-image:radial-gradient(circle 5px at 14px 13px,#9c9283 0%,#4a4236 52%,#17120c 76%,rgba(20,16,11,0) 100%),radial-gradient(circle 5px at calc(100% - 14px) 13px,#9c9283 0%,#4a4236 52%,#17120c 76%,rgba(20,16,11,0) 100%);background-repeat:no-repeat;opacity:.9}
 input,select{color-scheme:dark}
 #bsplash{position:fixed;inset:0;z-index:9000;pointer-events:none;display:flex;align-items:center;justify-content:center;opacity:0}
 #bsplash.bspon{animation:bspfade 1.75s cubic-bezier(.22,1,.36,1) forwards}
 @keyframes bspfade{0%{opacity:0}10%{opacity:1}72%{opacity:1}100%{opacity:0}}
 #bsplash .bsp-card{display:flex;align-items:center;gap:15px;background:linear-gradient(180deg,rgba(43,35,28,.98),rgba(27,21,17,.98));border:1px solid var(--line2);border-radius:16px;padding:20px 34px;box-shadow:0 34px 80px -22px rgba(0,0,0,.85),inset 0 1px 0 rgba(255,255,255,.07)}
 #bsplash.bspon .bsp-card{animation:bspcard 1.75s cubic-bezier(.22,1,.36,1) forwards}
 @keyframes bspcard{0%{transform:scale(.92)}13%{transform:scale(1)}100%{transform:scale(1)}}
 #bsplash .bsp-mark{color:var(--accent-lit);font-size:28px;line-height:1;filter:drop-shadow(0 0 10px rgba(202,160,110,.55))}
 #bsplash .bsp-txt{font-family:Georgia,'Iowan Old Style','Times New Roman',serif;font-size:26px;color:var(--txt);letter-spacing:.01em;white-space:nowrap}
 #bsplash .bsp-txt b{color:var(--accent-lit);font-style:italic;font-weight:600}
 body.compact .side{flex:0 0 60px;overflow:hidden;transition:flex-basis .16s ease}
 body.compact .side .navsearch,body.compact .side .pretitle,body.compact .side .chip,body.compact .side .navsep,body.compact .side .navgroup,body.compact .side .tab>span:nth-child(2),body.compact .side .tab .navbadge{display:none}
 body.compact .side .tab{justify-content:center;padding-left:0;padding-right:0}
 body.compact .side .tab .ti{opacity:.92}
 body.compact .side:hover{flex-basis:214px}
 body.compact .side:hover .navsearch,body.compact .side:hover .pretitle,body.compact .side:hover .navsep,body.compact .side:hover .navgroup,body.compact .side:hover .chip{display:block}
 body.compact .side:hover .tab>span:nth-child(2){display:block}
 body.compact .side:hover .tab{justify-content:flex-start;padding-left:11px;padding-right:11px}
 body.compact .preview{display:none}
 .topgroup{display:flex;align-items:center;gap:8px}
 .hammenu{display:none;background:#2a2418;color:#e9e0cf;border:1px solid var(--line2);border-radius:8px;padding:7px 12px;font-size:16px;line-height:1;cursor:pointer;box-shadow:0 2px 4px rgba(0,0,0,.36),inset 0 1px 0 rgba(255,255,255,.05)}
 body.compact .hammenu{display:inline-flex;align-items:center}
 body.compact .topgroup{display:none;position:fixed;top:54px;right:14px;flex-direction:column;align-items:stretch;gap:6px;background:var(--panel);border:1px solid var(--line2);border-radius:12px;padding:10px;box-shadow:0 22px 55px -18px rgba(0,0,0,.82);z-index:400;width:230px}
 body.compact .topbar.hmopen .topgroup{display:flex}
 body.compact .topgroup .btn2,body.compact .topgroup .btn,body.compact .topgroup .topfield{width:100%;text-align:left}
 .hrfield{display:inline-flex;align-items:center;gap:6px;margin-left:10px}
 .hrfield .hrlbl{color:var(--dim);font-size:11px;white-space:nowrap}
 .hrfield .hrin{width:74px}
 body.nowood .cwrap,body.nowood .topbar,body.nowood .side,body.nowood .content{background-image:none}
 body.reduce-motion *,body.reduce-motion *::before,body.reduce-motion *::after{transition-duration:.001s!important;animation-duration:.001s!important}
 .hcard{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:12px;box-shadow:inset 0 1px 0 rgba(255,255,255,.05),0 4px 12px -6px rgba(0,0,0,.6)}
 .hc-hd{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
 .hc-hd b{font-size:14px;color:var(--txt)}
 .hbadge{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);background:var(--field);border:1px solid var(--line2);border-radius:999px;padding:2px 9px}
 .hbadge.tracker{color:var(--accent2);border-color:var(--accent2)}
 .hbadge.script{color:var(--accent-lit);border-color:var(--accent-lit)}
 .stbtns{display:flex;gap:9px;margin-bottom:14px;flex-wrap:wrap}
 .stgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
 .stcard{background:var(--panel);border:1px solid var(--line2);border-radius:12px;padding:13px 15px}
 .stcard.active{border-color:var(--accent)}
 .stcard h3{margin:0 0 3px;font-size:14px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
 .stchip{font-size:10px;font-weight:800;letter-spacing:.05em;text-transform:uppercase;color:var(--green);border:1px solid var(--green);border-radius:99px;padding:1px 8px}
 .stchip.issues{color:#c2924c;border-color:#c2924c}
 .stdesc{color:var(--mut);font-size:12.5px;line-height:1.5;margin:0 0 6px;min-height:18px}
 .stmeta{color:var(--dim);font-size:11.5px;margin-bottom:10px}
 .strow{display:flex;gap:6px;flex-wrap:wrap}
 .strow .btn2{padding:6px 10px;font-size:12px}
 .stempty{border:2px dashed var(--line2);border-radius:12px;padding:26px;text-align:center;color:var(--mut);grid-column:1/-1;line-height:1.7}
 .scriptbar{display:flex;gap:9px;align-items:center;margin:10px 2px 0;flex-wrap:wrap}
 .scriptbar .sblab{color:var(--dim);font-size:10px;text-transform:uppercase;letter-spacing:.06em;font-weight:700}
 .scriptbar select{background:var(--bg2);border:1px solid var(--line2);border-radius:7px;color:var(--txt);padding:6px 9px;font:inherit;font-size:12px;max-width:280px}
 .scriptbar select:focus{outline:none;border-color:var(--accent)}
 .scriptnote{color:var(--mut);font-size:11.5px}
 .stlaunch{display:flex;gap:8px;align-items:center;margin:0 2px 12px;flex-wrap:wrap}
 .slnote{color:var(--mut);font-size:11.5px}
 /* ---- Studio-launch: top-level CLASSIC | STUDIO tabs ---- */
 .modestrip{display:none;gap:3px;align-items:center;margin-left:14px;padding:3px;
  background:var(--bg2);border:1px solid var(--line2);border-radius:11px}
 body.studiolaunch .modestrip{display:flex}
 .mtab{background:transparent;border:0;border-radius:8px;color:var(--mut);
  padding:7px 15px;font:inherit;font-size:12.5px;font-weight:700;letter-spacing:.4px;
  text-transform:uppercase;cursor:pointer;transition:color .15s,background .15s}
 .mtab:hover{color:var(--txt)}
 .mtab.on{background:var(--sand-dim);color:var(--accent-lit);box-shadow:inset 0 0 0 1px var(--accent)}
 /* mode-scoped surfaces: CLASSIC hides the Studio surfaces; the two Studio
    modes hide the classic-cycle-only tabs, groups, presets and the Run-tab
    script picker. STUDIO BUILD shows the build library tab, STUDIO SCRIPT
    shows the Script tab -- never both, never the other mode's surface. */
 body.studiolaunch.mode-classic .tab[data-tab="studio"]{display:none}
 body.studiolaunch #scriptsel{display:none}
 .tab[data-tab="script"]{display:none}
 body.studiolaunch.mode-script .tab[data-tab="script"]{display:flex}
 body.studiolaunch.mode-script .tab[data-tab="studio"]{display:none}
 body.studiolaunch.mode-studio .tab[data-tab="cycle"],
 body.studiolaunch.mode-studio .tab[data-tab="builds"],
 body.studiolaunch.mode-studio .tab[data-tab="relics"],
 body.studiolaunch.mode-studio .tab[data-tab="Tracker"],
 body.studiolaunch.mode-studio .tab[data-tab="Relic behaviour"],
 body.studiolaunch.mode-studio .navgroup[data-group="Modes"],
 body.studiolaunch.mode-studio .navgroup[data-group="Advanced"],
 body.studiolaunch.mode-studio .navgroup[data-group="Other"],
 body.studiolaunch.mode-studio .side .pretitle,
 body.studiolaunch.mode-studio .side .chip,
 body.studiolaunch.mode-script .tab[data-tab="cycle"],
 body.studiolaunch.mode-script .tab[data-tab="builds"],
 body.studiolaunch.mode-script .tab[data-tab="relics"],
 body.studiolaunch.mode-script .tab[data-tab="Tracker"],
 body.studiolaunch.mode-script .tab[data-tab="Relic behaviour"],
 body.studiolaunch.mode-script .navgroup[data-group="Modes"],
 body.studiolaunch.mode-script .navgroup[data-group="Advanced"],
 body.studiolaunch.mode-script .navgroup[data-group="Other"],
 body.studiolaunch.mode-script .side .pretitle,
 body.studiolaunch.mode-script .side .chip{display:none}
 /* Studio-launch: the legacy embedded editor never appears -- its entry
    points are gone and authoring hands off to Prospector Studio. */
 body.studiolaunch #stopen,body.studiolaunch #stnew{display:none}
 /* Dynamic settings from the pushed graph (Studio modes) */
 .spwrap{margin:0 0 14px}
 .spsearch{margin-left:auto;background:var(--field);border:1px solid var(--line2);
  border-radius:8px;color:var(--txt);font:inherit;font-size:12px;padding:5px 9px;width:150px}
 .spsearch:focus{outline:none;border-color:var(--accent)}
 .spgrid{margin-top:8px}
 .spgroup{margin:10px 0 4px;color:var(--mut);font-size:10.5px;font-weight:700;
  letter-spacing:.5px;text-transform:uppercase}
 .sprow{display:flex;gap:9px;align-items:center;padding:5px 0;border-bottom:1px solid var(--line)}
 .sprow:last-child{border-bottom:none}
 .sp-pin{background:none;border:0;color:var(--dim);cursor:pointer;font-size:13px;padding:0 2px}
 .sp-pin.on{color:var(--accent-lit)}
 .sp-lab{flex:1;min-width:0}
 .sp-name{font-size:12.5px;color:var(--txt)}
 .sp-desc{font-size:10.5px;color:var(--dim)}
 .sp-state{font-size:9.5px;font-weight:700;letter-spacing:.4px;text-transform:uppercase;
  padding:2px 6px;border-radius:6px;border:1px solid var(--line2);color:var(--dim)}
 .sp-state.ovr{border-color:var(--accent);color:var(--accent-lit)}
 .sprow input[type=number],.sprow input[type=text],.sprow select{background:var(--field);
  border:1px solid var(--line2);border-radius:7px;color:var(--txt);font:inherit;
  font-size:12px;padding:4px 8px;width:110px}
 .sprow input:focus,.sprow select:focus{outline:none;border-color:var(--accent)}
 .sp-unit{color:var(--dim);font-size:11px;min-width:24px}
 /* Live step card: STUDIO SCRIPT (Run tab + Script tab) and STUDIO BUILD
    (Run tab, labeled "build step") -- the engine emits script.block for
    both kinds, so hiding it for builds would be a styling lie. */
 .scriptcard{margin:0 2px 12px;padding:12px 14px;border:1px solid var(--line2);
  border-radius:12px;background:var(--bg2);display:none}
 body.studiolaunch.mode-script .scriptcard{display:block}
 body.studiolaunch.mode-studio #runscriptcard{display:block}
 .scr-top{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 .scr-name{font-weight:700;font-size:13.5px}
 .scr-step{margin-top:7px;color:var(--txt);font-size:12.5px;font-variant-numeric:tabular-nums}
 .scr-step .lbl{color:var(--mut)}
 .scr-hud{margin-top:5px;color:var(--accent-lit);font-size:12px;min-height:15px}
 /* ---- main-window confirm modal (mode switch, resets) ---- */
 .mmodal{position:fixed;inset:0;z-index:1200;display:none;align-items:center;justify-content:center;background:rgba(0,0,0,.55)}
 .mmodal.show{display:flex;animation:fadeIn .22s var(--ease)}
 .mbox{background:var(--bg2);border:1px solid var(--line2);border-radius:14px;padding:20px 22px;max-width:420px;box-shadow:0 18px 50px rgba(0,0,0,.5)}
 .mbox h3{margin:0 0 8px;font-size:15px}
 .mbox p{margin:0 0 16px;color:var(--mut);font-size:12.5px;line-height:1.55}
 .mrow{display:flex;gap:9px;justify-content:flex-end}
 .skipopt{margin:0 0 13px}
 .skipopt p{margin:6px 0 0;color:var(--mut);font-size:12px;line-height:1.5}
 /* ---- diagnostics: warning drawer, rec chip, FAQ browser ---- */
 #diagdrawer{position:fixed;top:0;right:0;bottom:0;width:min(500px,94vw);z-index:1100;background:var(--bg2);border-left:1px solid var(--line2);box-shadow:-26px 0 70px -18px rgba(0,0,0,.75);display:none;flex-direction:column}
 #diagdrawer.show{display:flex}
 .ddhead{display:flex;align-items:center;gap:10px;padding:13px 16px;border-bottom:1px solid var(--line)}
 .ddhead h3{margin:0;font-size:15px;flex:1}
 #ddclose{background:none;border:none;color:var(--mut);font-size:16px;cursor:pointer;padding:4px 8px;border-radius:8px}
 #ddclose:hover{background:var(--panel);color:var(--txt)}
 #ddlist{border-bottom:1px solid var(--line);max-height:170px;overflow-y:auto;flex-shrink:0}
 .ddev{display:flex;align-items:center;gap:9px;width:100%;text-align:left;background:none;border:none;border-bottom:1px solid var(--line);padding:8px 16px;color:var(--txt);font:inherit;font-size:12.5px;cursor:pointer}
 .ddev:hover{background:var(--panel)}
 .ddev.sel{background:var(--panel);box-shadow:inset 3px 0 0 var(--accent)}
 .ddev .ddevt{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .ddev .ddevn{color:var(--dim);font-size:11px;flex-shrink:0}
 #ddbody{flex:1;overflow-y:auto;padding:14px 16px 22px}
 .ddsec{margin:0 0 14px}
 .ddsec h4{margin:0 0 5px;font-size:10.5px;text-transform:uppercase;letter-spacing:.09em;color:var(--dim)}
 .ddsec p{margin:0;font-size:12.5px;line-height:1.55;color:var(--txt)}
 .ddsec ul{margin:4px 0 0;padding-left:18px;font-size:12.5px;line-height:1.55;color:var(--mut)}
 .ddchip{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:3px 10px;font-size:11px;font-weight:800;letter-spacing:.05em}
 .ddchip.red{background:#e05c5c;color:#fff}
 .ddchip.yellow{background:#e0b34a;color:#1a1005}
 .ddchip.info{background:var(--panel);color:var(--mut);border:1px solid var(--line2)}
 .ddconf{display:inline-flex;align-items:center;border-radius:999px;padding:3px 10px;font-size:11px;font-weight:700;background:var(--panel);border:1px solid var(--line2);color:var(--txt)}
 .ddfirst{background:var(--panel);border:1px solid var(--line2);border-radius:11px;padding:10px 12px}
 .ddfirst b{display:block;margin-bottom:3px;font-size:13px}
 .ddrow{display:flex;flex-wrap:wrap;align-items:center;gap:8px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 11px;margin:0 0 7px}
 .ddrow .ddlab{flex:1 1 150px;font-size:12.5px}
 .ddrow .ddval{font-size:12px;color:var(--mut);white-space:nowrap}
 .ddrow .ddwhy{flex-basis:100%;font-size:11.5px;color:var(--dim);line-height:1.45}
 .ddbtns{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
 .ddcode{color:var(--dim);font-size:11px;margin-left:auto}
 #diagrec{position:fixed;z-index:1105;display:none;background:var(--bg2);border:1px solid var(--line2);border-radius:12px;padding:11px 13px;max-width:330px;box-shadow:0 18px 50px -12px rgba(0,0,0,.8);font-size:12.5px;line-height:1.5}
 #diagrec .drhead{display:flex;align-items:center;gap:8px;margin-bottom:5px;font-weight:700}
 #diagrec .drx{margin-left:auto;background:none;border:none;color:var(--mut);cursor:pointer;font-size:13px;padding:2px 6px}
 #diagrec .drbtns{display:flex;gap:8px;margin-top:8px}
 #faqmodal{position:fixed;inset:0;z-index:1210;display:none;align-items:center;justify-content:center;background:rgba(0,0,0,.55)}
 #faqmodal.show{display:flex}
 .faqbox{background:var(--bg2);border:1px solid var(--line2);border-radius:14px;padding:18px 20px;width:min(640px,92vw);max-height:84vh;display:flex;flex-direction:column;box-shadow:0 18px 50px rgba(0,0,0,.5)}
 .faqbox h3{margin:0 0 10px;font-size:15px}
 #faqsearch{width:100%;background:var(--panel);border:1px solid var(--line2);border-radius:9px;color:var(--txt);font:inherit;font-size:13px;padding:8px 11px;margin-bottom:10px}
 #faqlist,#faqentry{flex:1;overflow-y:auto;min-height:120px}
 .faqq{display:block;width:100%;text-align:left;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:9px 12px;margin:0 0 7px;color:var(--txt);font:inherit;font-size:13px;cursor:pointer}
 .faqq:hover{border-color:var(--line2);background:var(--bg2)}
 .faqq .fqs{display:block;margin-top:3px;color:var(--dim);font-size:11.5px}
 #faqentry h4{margin:12px 0 5px;font-size:10.5px;text-transform:uppercase;letter-spacing:.09em;color:var(--dim)}
 #faqentry p,#faqentry li{font-size:12.5px;line-height:1.55;color:var(--txt)}
 #faqentry ul,#faqentry ol{margin:4px 0 0;padding-left:18px}
 .faqlinks{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px}
 /* ---- settings ownership card ---- */
 .ownercard{margin-top:22px;padding:16px 18px;border:1px solid var(--line2);border-radius:12px;background:var(--bg2)}
 .ownercard h3{margin:0 0 6px;font-size:13.5px}
 .ownerbtns{display:flex;gap:9px;flex-wrap:wrap;margin:12px 0 6px}
 /* ---- Studio-panel build header (Studio launch) ---- */
 .sthdr{margin:0 0 14px;padding:14px 16px;border:1px solid var(--line2);border-radius:12px;background:var(--bg2)}
 .sth-top{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 .sth-name{font-weight:700;font-size:14px}
 .sth-badge{font-size:10.5px;font-weight:700;letter-spacing:.4px;text-transform:uppercase;padding:3px 8px;border-radius:7px;border:1px solid var(--line2);color:var(--mut)}
 .sth-badge.on{border-color:var(--accent);color:var(--accent-lit);background:var(--sand-dim)}
 .sth-meta{margin:7px 0 10px;color:var(--mut);font-size:12px;line-height:1.5}
 .sth-btns{display:flex;gap:8px;flex-wrap:wrap}
 .hc-rt{margin-left:auto;color:var(--accent-lit);font-weight:700;font-variant-numeric:tabular-nums;font-size:13px}
 .hc-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:12px}
 .hc{background:var(--field);border-radius:9px;padding:8px 10px;text-align:center;box-shadow:inset 0 1px 2px rgba(0,0,0,.3)}
 .hc .hcv{font-size:17px;font-weight:700;color:var(--txt);font-variant-numeric:tabular-nums}
 .hc .hcl{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.05em;margin-top:2px}
 .hc-foot{margin-top:12px;display:flex;justify-content:flex-end;align-items:center}
 .hnolog{color:var(--dim);font-size:11px;font-style:italic}
 #logmodal{position:fixed;inset:0;z-index:9500;background:rgba(8,6,4,.62);display:none;align-items:center;justify-content:center;padding:30px}
 #logmodal .lm-box{background:var(--panel);border:1px solid var(--line2);border-radius:14px;width:min(880px,92vw);height:min(80vh,720px);display:flex;flex-direction:column;box-shadow:0 30px 80px -20px rgba(0,0,0,.85)}
 #logmodal .lm-hd{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--line)}
 #logmodal .lm-hd b{font-size:14px}
 #logmodal .lm-name{color:var(--dim);font-size:11px;font-family:ui-monospace,Menlo,monospace}
 #logmodal .lm-x{margin-left:auto;background:none;border:none;color:var(--mut);font-size:15px;cursor:pointer;border-radius:6px;padding:2px 8px}
 #logmodal .lm-x:hover{color:var(--txt);background:var(--bg2)}
 #logmodal .lm-body{flex:1;overflow:auto;margin:0;padding:14px 16px;font:12px ui-monospace,Menlo,monospace;color:#c2c8ba;white-space:pre-wrap;-webkit-user-select:text;user-select:text}
 .whbox{margin-top:14px;max-width:600px}
 .whrow{display:flex;align-items:center;gap:10px}
 .whrow .whlbl{color:var(--txt);font-size:13px;white-space:nowrap}
 .whrow .whin{flex:1;background:var(--field);color:var(--txt);border:1px solid var(--line2);border-radius:8px;padding:9px 11px;font:12px ui-monospace,Menlo,monospace;box-shadow:inset 0 1px 2px rgba(0,0,0,.4)}
 .whrow .whin:focus{outline:0;border-color:var(--accent);box-shadow:inset 0 1px 2px rgba(0,0,0,.4),0 0 0 3px rgba(168,121,74,.28)}
 .whactions{display:flex;gap:8px;margin-top:10px}
 .whsteps{margin:0;padding-left:20px;color:var(--mut);font-size:12.5px;line-height:1.7}
 .whsteps li{margin:2px 0}
 .whsteps b{color:var(--txt)}
 .dscbtn{display:inline-flex;align-items:center;justify-content:center;gap:8px;background:#5865f2;color:#fff;border:0;border-radius:9px;padding:9px 15px;font:inherit;font-weight:700;cursor:pointer}
 .dscbtn:hover{background:#4a54cc}
 .dscbtn svg{width:17px;height:17px;flex:none}
 .gate-dsc{width:100%;margin-top:12px}
 .tour{position:fixed;inset:0;z-index:600;pointer-events:none}
 .tourspot{position:fixed;border-radius:10px;box-shadow:0 0 0 9999px rgba(6,5,3,.76);border:2px solid var(--accent,#e0b873);transition:left .25s,top .25s,width .25s,height .25s,opacity .15s;pointer-events:none}
 @media (prefers-reduced-motion:reduce){.tourspot{transition:none}}
 .tourpop{position:fixed;background:var(--panel);border:1px solid var(--line2);border-radius:14px;padding:18px 20px;width:344px;box-shadow:0 24px 70px -20px rgba(0,0,0,.85);z-index:601;pointer-events:auto;max-height:calc(100vh - 24px);overflow-y:auto}
 .tourpop.center{left:50%!important;top:50%!important;transform:translate(-50%,-50%);width:440px}
 .tourpop{transition:opacity .15s ease}
 .tourpop.tfade,.tourspot.tfade{opacity:0}
 #tourarrow{position:absolute;width:12px;height:12px;background:var(--panel);border-left:1px solid var(--line2);border-top:1px solid var(--line2);display:none;pointer-events:none}
 .tourbody .tlink{color:var(--accent-lit,#e0b873);cursor:pointer;text-decoration:underline}
 #tourmenu{position:fixed;z-index:640;background:var(--panel);border:1px solid var(--line2);border-radius:12px;padding:8px;box-shadow:0 18px 50px -12px rgba(0,0,0,.8);display:flex;flex-direction:column;min-width:250px}
 #tourmenu .tmhd{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.1em;padding:4px 10px 6px}
 #tourmenu button{font:inherit;text-align:left;background:none;border:none;border-radius:8px;padding:7px 10px;color:var(--txt);cursor:pointer;font-size:13px;display:flex;align-items:center;gap:8px}
 #tourmenu button:hover{background:var(--bg2)}
 #tourmenu .tmdone{color:var(--accent2,#7d9b63);font-size:11px;margin-left:auto}
 .explainbtn{font:inherit;font-size:12px;font-weight:600;margin-left:12px;vertical-align:4px;background:none;border:1px solid var(--line2);color:var(--mut);border-radius:999px;padding:3px 10px;cursor:pointer}
 .explainbtn:hover{color:var(--txt);border-color:var(--accent,#e0b873)}
 .preview{display:none;flex:0 0 360px;flex-direction:column;border-left:1px solid var(--line);background:var(--bg2);min-width:0}
 body.prev-on .preview{display:flex}
 body.coach-on .preview{display:none!important}
 .prev-head{display:flex;align-items:center;gap:9px;padding:12px 14px;border-bottom:1px solid var(--line)}
 .prev-mark{color:var(--accent-lit,#e0b873)}
 .prev-title{font-weight:700;font-size:14px}
 .prev-sub{color:var(--dim);font-size:11px;display:block}
 .prev-head button{margin-left:auto;background:none;border:none;color:var(--mut);cursor:pointer;font-size:13px;border-radius:6px;padding:3px 7px}
 .prev-head button:hover{color:var(--txt);background:var(--panel)}
 .prev-body{flex:1;overflow:auto;padding:14px;min-height:0}
 .ph-body{font-size:13px;color:var(--mut);line-height:1.6}
 .ph-body p{margin:0 0 9px} .ph-body p:last-child{margin-bottom:0}
 .ph-body b{color:var(--txt);font-weight:700} .ph-body i{color:var(--txt)}
 .ph-body code{background:var(--field);padding:1px 5px;border-radius:4px;font:12px ui-monospace,Menlo,monospace;color:var(--accent-lit,#e0b873)}
 .ph-body h4{margin:13px 0 5px;font-size:11.5px;font-weight:800;letter-spacing:.05em;text-transform:uppercase;color:var(--accent-lit,#e0b873)}
 .ph-body ul{margin:6px 0 10px;padding-left:18px} .ph-body li{margin:3px 0;line-height:1.55}
 .ph-body .ph-call{background:var(--field);border-left:3px solid var(--accent,#a8794a);border-radius:0 8px 8px 0;padding:8px 11px;margin:9px 0;color:var(--mut);font-size:12.5px;line-height:1.55}
 .ph-body .ph-call b{color:var(--accent-lit,#e0b873);margin-right:4px}
 .prev-empty{color:var(--dim);font-size:12.5px;line-height:1.75;padding:8px 4px}
 .prev-empty b{color:var(--mut)}
 #prevtoggle.on{background:var(--accent);color:#241a02}
 .prevsnap{position:relative;border:1px solid var(--line2);border-radius:12px;overflow:hidden;background:var(--bg);height:440px;padding:16px 0 0 16px;box-sizing:border-box}
 .prevsnap .snapin{transform:scale(.43);transform-origin:top left;width:230%;pointer-events:none;opacity:.92;filter:saturate(.95)}
 .prevsnap::after{content:"";position:absolute;left:0;right:0;bottom:0;height:52px;background:linear-gradient(rgba(20,17,12,0),var(--bg));pointer-events:none}
 .prevsnap .snaptag,.prevmock .snaptag{position:absolute;top:8px;right:8px;background:rgba(6,5,3,.74);border:1px solid var(--line2);color:var(--accent-lit,#e0b873);font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;padding:3px 8px;border-radius:999px;pointer-events:none;z-index:2}
 .prevlbl{color:var(--dim);font-size:11px;margin:8px 2px 0}
 .prevmock{position:relative;border:1px solid var(--line2);border-radius:10px;overflow:hidden;background:var(--bg);padding:12px;pointer-events:none;opacity:.92}
 .prevhelp h3{margin:0 0 2px;font-size:15px}
 .prevhelp .ph-kind{color:var(--dim);font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;font-weight:700;margin-bottom:8px}
 .prevhelp .ph-body{color:var(--mut);font-size:13px;line-height:1.7}
 .prevhelp .ph-body b{color:var(--txt)}
 .prevhelp .ph-meta{margin-top:10px;color:var(--dim);font-size:11.5px;border-top:1px solid var(--line);padding-top:8px;line-height:1.6}
 .prevhelp img{max-width:100%;border-radius:8px;border:1px solid var(--line2);margin-top:10px;cursor:zoom-in}
 .ph-vid{position:relative;padding-top:56.25%;margin-top:10px;border-radius:8px;overflow:hidden;border:1px solid var(--line2)}
 .ph-vid iframe{position:absolute;inset:0;width:100%;height:100%;border:0}
 .prevseg{display:flex;gap:8px;align-items:flex-start;padding:6px 0;border-bottom:1px solid rgba(51,47,42,.5);font-size:12.5px}
 .prevseg:last-child{border-bottom:0}
 .prevseg .sgdot{width:9px;height:9px;border-radius:3px;margin-top:3px;flex:none}
 .prevseg .sgms{margin-left:auto;color:var(--dim);font-variant:tabular-nums;flex:none;padding-left:8px}
 .prevseg .sgn{color:var(--txt);font-weight:600}
 .prevseg .sgp{color:var(--dim);font-size:11px;display:block}
 .prevseg-d{padding:10px 0;border-bottom:1px solid rgba(51,47,42,.5)}
 .prevseg-d:last-child{border-bottom:0}
 .prevseg-d .psd-h{display:flex;align-items:center;gap:8px}
 .prevseg-d .psd-n{color:var(--txt);font-weight:700;font-size:13px}
 .prevseg-d .psd-ms{margin-left:auto;color:var(--dim);font-variant:tabular-nums;font-size:11.5px;flex:none;padding-left:8px}
 .prevseg-d .sgdot{width:9px;height:9px;border-radius:3px;flex:none}
 .prevseg-d .psd-stage{color:var(--dim);font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;margin:2px 0 0 17px}
 .prevseg-d .psd-note{color:var(--accent-lit,#e0b873);font-size:11.5px;font-style:italic;margin:5px 0 0 17px}
 .prevseg-d .psd-body{color:var(--mut);font-size:12.5px;line-height:1.65;margin:6px 0 0 17px}
 .prevseg-d .psd-parts{color:var(--dim);font-size:11px;line-height:1.55;margin:6px 0 0 17px}
 .prevseg-d .psd-parts b{color:var(--mut);font-weight:600}
 .cmw{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
 .cmw .cmw-shot{flex-basis:100%;margin-top:8px;border-radius:8px;overflow:hidden;border:1px solid var(--line2);background:#0d0a08;line-height:0}
 .cmw .cmw-shot img{width:100%;display:block;image-rendering:pixelated}
 .cmw .cmw-sw{width:46px;height:46px;border-radius:9px;border:1px solid var(--line2);flex:none;box-shadow:inset 0 0 0 1px rgba(0,0,0,.25)}
 .cmw .cmw-sw.none{background:repeating-linear-gradient(45deg,#2a2418,#2a2418 6px,#221d13 6px,#221d13 12px)}
 .cmw .cmw-meta{font-size:12px;line-height:1.7;font-variant-numeric:tabular-nums}
 .cmw .cmw-meta s{text-decoration:none;color:var(--dim);margin-right:6px}
 .cmw .cmw-meta b{color:var(--txt)}
 .cmw .cmw-hint{color:var(--dim);font-size:11px;margin-top:2px;line-height:1.5}
 .runflow{margin-left:12px;color:var(--dim);font-size:12px;border:1px solid var(--line2);border-radius:999px;padding:3px 11px;cursor:help;white-space:nowrap}
 .runflow:hover{color:var(--txt);border-color:var(--accent,#e0b873)}
 @keyframes pvfade{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}
 .prev-body>*{animation:pvfade .18s var(--ease,ease)}
 .pvchart{background:linear-gradient(180deg,rgba(42,34,26,.5),rgba(23,19,15,.5));border:1px solid var(--line2);border-radius:10px;padding:11px 12px;margin:0 0 13px}
 .pvc-hd{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.06em;font-weight:800;margin-bottom:9px}
 .pvc-hd b{font-weight:800}
 .pvc-row{display:flex;align-items:center;gap:8px;margin:5px 0}
 .pvc-lbl{width:56px;text-align:right;font-size:11px;color:var(--accent-lit,#e0b873);font-variant-numeric:tabular-nums;flex:none}
 .pvc-pl{width:96px;text-align:right;font-size:11px;flex:none}
 .pvc-track{flex:1;background:#201b15;border-radius:5px;height:20px;box-shadow:inset 0 1px 2px rgba(0,0,0,.4)}
 .pvc-bar{display:flex;gap:2px;height:100%;border-radius:5px;overflow:hidden}
 .pvc-seg{background:#3a332a;position:relative}
 .pvc-seg.on{box-shadow:inset 0 0 0 2px rgba(255,246,232,.7);z-index:1;animation:pvglow 1.8s ease-in-out infinite}
 .pvc-tag{display:inline-block;font-size:9.5px;font-weight:800;color:#1a1005;padding:2px 8px;border-radius:999px;margin-left:8px;text-transform:none;vertical-align:middle}
 .pvc-tot{width:50px;font-size:11px;color:var(--dim);font-variant-numeric:tabular-nums;flex:none}
 .pvc-note{font-size:11px;color:var(--dim);line-height:1.5;margin-top:8px} .pvc-note b{color:var(--accent-lit,#e0b873)}
 .pvc-ir{flex:1;height:10px;background:#201b15;border-radius:5px;overflow:hidden;box-shadow:inset 0 1px 2px rgba(0,0,0,.4)}
 .pvc-ir b{display:block;height:100%;border-radius:5px}
 .pvc-none{flex:1;font-size:11px;color:var(--dim)}
 .pvgrow.g1{animation:pvgrow1 3s ease-in-out infinite}
 .pvgrow.g2{animation:pvgrow2 3s ease-in-out infinite}
 .pvgrow.g3{animation:pvgrow3 3s ease-in-out infinite}
 .pvsee.a.d1{animation:pvs1a 3s ease-in-out infinite} .pvsee.b.d1{animation:pvs1b 3s ease-in-out infinite}
 .pvsee.a.d2{animation:pvs2a 3s ease-in-out infinite} .pvsee.b.d2{animation:pvs2b 3s ease-in-out infinite}
 .pvsee.a.d3{animation:pvs3a 3s ease-in-out infinite} .pvsee.b.d3{animation:pvs3b 3s ease-in-out infinite}
 .pvcnt{animation:pvcount 3s linear infinite}
 .pvc-meter{position:relative;height:16px;background:#201b15;border-radius:5px;overflow:hidden;box-shadow:inset 0 1px 2px rgba(0,0,0,.4)}
 .pvc-fill{position:absolute;left:0;top:0;bottom:0;background:#c76d6d;border-radius:5px 0 0 5px;animation:pvfill 2.4s ease-in infinite}
 .pvc-line{position:absolute;top:-2px;bottom:-2px;left:60%;width:2px;background:var(--accent-lit,#caa06e)}
 .pvc-tmr{position:relative;height:14px;background:#201b15;border-radius:5px;overflow:hidden}
 .pvc-tk{position:absolute;top:0;bottom:0;width:4%;background:var(--accent,#a8794a);border-radius:2px}
 .pvc-ph{position:absolute;top:-2px;bottom:-2px;left:0;width:2px;background:var(--accent-lit,#caa06e);animation:pvsweep 2.8s linear infinite}
 .pvc-lg{display:flex;gap:14px;font-size:11px;color:var(--dim);margin-top:6px}
 .pvc-sw{display:inline-block;width:9px;height:9px;background:var(--accent,#a8794a);border-radius:2px;vertical-align:-1px}
 .pvc-gp{display:inline-block;width:16px;height:5px;background:#201b15;border:1px solid var(--line2);border-radius:2px;vertical-align:1px}
 .pvc-wheel{position:relative;width:88px;height:88px;margin:2px auto 2px}
 .pvc-disc{position:absolute;inset:0;border-radius:50%;opacity:.4}
 .pvc-rev{position:absolute;top:50%;left:50%;width:88px;height:88px;border-radius:50%;transform:translate(-50%,-50%) scale(var(--sc,.5));-webkit-mask:radial-gradient(circle,#000 60%,transparent 61%);mask:radial-gradient(circle,#000 60%,transparent 61%);animation:pvtol 3.4s ease-in-out infinite}
 .pvc-core{position:absolute;top:50%;left:50%;width:24px;height:24px;border-radius:50%;transform:translate(-50%,-50%);box-shadow:0 0 0 2px #161210}
 .pvc-ring{position:absolute;top:50%;left:50%;width:88px;height:88px;border-radius:50%;border:2px solid var(--accent-lit,#caa06e);transform:translate(-50%,-50%) scale(var(--sc,.5));animation:pvtol 3.4s ease-in-out infinite}
 .pvc-grad{position:relative;height:16px;border-radius:4px;background:linear-gradient(90deg,#000,#fff)}
 .pvc-gl{position:absolute;top:-4px;bottom:-4px;width:2px;background:var(--accent-lit,#caa06e);animation:pvglow 1.8s ease-in-out infinite}
 .pvc-mode{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
 .pvc-mnode{background:#201b15;border:1px solid var(--line2);border-radius:6px;padding:4px 9px;font-size:11px;color:var(--txt)}
 .pvc-mnode.act{background:var(--accent,#a8794a);color:#1a1005;border-color:var(--accent)}
 .pvc-marr{font-size:10px;color:var(--dim)}
 .pvc-dm{background:#2b2d31;border-radius:8px;padding:9px 10px;display:flex;gap:8px}
 .pvc-dma{width:26px;height:26px;border-radius:50%;background:#5865f2;flex:none}
 .pvc-dmt{font-size:11px;color:#f2c079} .pvc-dmb{font-size:11px;color:#dbdee1;margin-top:1px}
 .pvc-rdo{display:flex;gap:8px} .pvc-rdo span{flex:1;background:#201b15;border-radius:7px;padding:7px 9px;font-size:12px;color:var(--txt)} .pvc-rdo s{text-decoration:none;color:var(--dim);margin-right:6px;font-size:11px}
 .pvc-rar{display:flex;gap:4px;flex-wrap:wrap}
 .pvc-rp{font-size:11px;color:#1a1005;padding:2px 8px;border-radius:999px} .pvc-rp.on{box-shadow:inset 0 0 0 2px rgba(255,246,232,.8)}
 .pvc-callout{background:#201b15;border-left:3px solid var(--accent,#a8794a);border-radius:0 8px 8px 0;padding:9px 12px;font-size:12px;color:var(--mut);line-height:1.55}
 body.reduce-motion .pvchart *{animation:none!important}
 @keyframes pvglow{0%,100%{opacity:.66}50%{opacity:1}}
 @keyframes pvgrow1{0%{width:0}55%{width:32%}86%{width:32%}100%{width:0}}
 @keyframes pvgrow2{0%{width:0}55%{width:64%}86%{width:64%}100%{width:0}}
 @keyframes pvgrow3{0%{width:0}55%{width:94%}86%{width:94%}100%{width:0}}
 @keyframes pvs1a{0%,100%{width:38%}50%{width:66%}} @keyframes pvs1b{0%,100%{width:62%}50%{width:44%}}
 @keyframes pvs2a{0%,100%{width:30%}50%{width:84%}} @keyframes pvs2b{0%,100%{width:80%}50%{width:26%}}
 @keyframes pvs3a{0%,100%{width:22%}50%{width:92%}} @keyframes pvs3b{0%,100%{width:88%}50%{width:14%}}
 @keyframes pvcount{0%{width:0}18%{width:34%}30%{width:34%}48%{width:67%}60%{width:67%}78%{width:100%}92%{width:100%}100%{width:0}}
 @keyframes pvfill{0%{width:0}55%{width:60%}82%{width:60%}100%{width:0}}
 @keyframes pvsweep{0%{left:-2%}100%{left:102%}}
 @keyframes pvtol{0%,100%{transform:translate(-50%,-50%) scale(.3)}50%{transform:translate(-50%,-50%) scale(var(--sc,.7))}}
 .pvc-cells{display:flex;gap:3px;align-items:center}
 .pvc-cell{flex:1;height:16px;border-radius:4px;background:#e0b357;animation:pvcell 2.8s linear infinite;animation-fill-mode:backwards}
 .pvc-more{font-size:10px;color:var(--dim);flex:none;padding-left:3px}
 .pvc-fcell{position:absolute;top:2px;bottom:2px;background:#c76d6d;border-radius:3px;animation:pvcell 2.8s linear infinite;animation-fill-mode:backwards}
 .pvc-fillv{position:absolute;left:0;top:0;bottom:0;background:#c76d6d;border-radius:5px 0 0 5px;animation:pvfillv 2.6s ease-in infinite}
 .pvc-fills{position:absolute;left:0;top:0;bottom:0;background:#c76d6d;border-radius:5px 0 0 5px}
 .pvc-cap{flex:none;min-width:34px;text-align:center;font-size:10.5px;font-weight:800;color:var(--txt);background:#201b15;border:1px solid var(--line2);border-bottom-width:2px;border-radius:5px;padding:3px 6px}
 .pvc-holdb{animation:pvholdv 2.6s ease-in-out infinite}
 .pvc-lbl.me{color:#fff}
 .pvc-chips{display:flex;align-items:stretch;gap:5px;margin:0 0 7px;flex-wrap:wrap}
 .pvc-ch{background:#201b15;border:1px solid #33302a;border-radius:6px;padding:5px 8px;font-size:10.5px;color:var(--mut);display:flex;align-items:center;text-align:center;line-height:1.35}
 .pvc-ch.on{border-color:var(--accent,#c2924c);background:#2a2118;color:var(--accent-lit,#e0b873)}
 .pvc-ch.bad{border-color:#7a4a42;color:#d88a6a}
 .pvc-ch.dim{opacity:.5}
 .pvc-ch.pvc-strike{text-decoration:line-through;opacity:.5}
 .pvc-ch.pvc-act{flex:1.6;flex-direction:column;align-items:stretch;gap:5px;text-align:left}
 .pvc-arr{color:var(--dim);align-self:center;font-size:10px;flex:none}
 .pvc-sub{display:block;font-size:9.5px;color:var(--dim);width:100%}
 .pvc-big{margin-left:auto;text-align:right;font-size:19px;color:var(--accent-lit,#e0b873);font-weight:700;align-self:center}
 .pvc-big small{display:block;font-size:9.5px;color:var(--dim);font-weight:400}
 .pvc-hb{display:block;height:7px;background:#181410;border-radius:4px;overflow:hidden}
 .pvc-hb i{display:block;height:100%;background:#c69a6e;border-radius:4px;animation:pvholdv 2.6s ease-in-out infinite}
 .pvc-rt2{width:92px;font-size:10px;color:var(--dim);flex:none;line-height:1.3}
 .pvc-rt2 b{color:var(--mut)}
 .pvc-cad{display:flex;gap:3px;align-items:center;height:18px;margin:2px 0}
 .pvc-cadw{flex:5;background:#181410;border:1px solid #33302a;border-radius:4px;height:100%;display:flex;align-items:center;justify-content:center;font-size:9.5px;color:var(--dim)}
 .pvc-cadt{flex:1;background:var(--accent2,#7d9b63);border-radius:4px;height:100%;display:flex;align-items:center;justify-content:center;font-size:9.5px;color:#1c2413;font-weight:700}
 .ph-row{display:flex;gap:8px;margin:0 0 6px;align-items:flex-start}
 .ph-tag{flex:none;width:76px;text-align:right;font-size:10px;font-weight:700;padding-top:1px;letter-spacing:.02em}
 .ph-tag i{font-style:normal;margin-right:3px}
 .ph-tag.up{color:var(--accent-lit,#e0b873)} .ph-tag.dn{color:#5aa0bd} .ph-tag.fx{color:#d88a6a} .ph-tag.ok{color:#8cc06a}
 .ph-tx{flex:1;font-size:11.5px;color:var(--mut);line-height:1.5}
 .ph-tx b{color:var(--txt)} .ph-tx s{text-decoration:none;color:#d88a6a} .ph-tx code{background:#201b15;border-radius:4px;padding:0 4px;font-size:10.5px}
 .ph-lk{display:inline-block;background:#201b15;border:1px solid var(--line2);border-radius:5px;padding:1px 7px;font-size:10.5px;color:var(--accent-lit,#e0b873);margin:0 3px 3px 0;cursor:pointer}
 .ph-lk:hover{border-color:var(--accent,#c2924c)}
 .ph-steps{margin:0 0 6px}
 .ph-step{display:flex;gap:8px;font-size:11.5px;color:var(--mut);line-height:1.5;margin:0 0 5px}
 .ph-step i{font-style:normal;flex:none;width:16px;height:16px;border-radius:50%;background:#2a2118;border:1px solid var(--accent,#a8794a);color:var(--accent-lit,#e0b873);font-size:9.5px;display:flex;align-items:center;justify-content:center;margin-top:1px}
 .ph-pills{display:flex;gap:6px;margin:2px 0 12px;font-size:10px;flex-wrap:wrap}
 .ph-pills span{background:#201b15;border:1px solid #33302a;border-radius:999px;padding:2px 9px;color:var(--dim)}
 .ph-pills span b{color:var(--mut)}
 @keyframes pvcell{0%{opacity:.15}7%{opacity:1}78%{opacity:1}86%{opacity:.15}100%{opacity:.15}}
 @keyframes pvfillv{0%{width:0}55%{width:var(--pvw,60%)}82%{width:var(--pvw,60%)}100%{width:0}}
 @keyframes pvholdv{0%{width:0}45%{width:var(--pvw,50%)}88%{width:var(--pvw,50%)}100%{width:0}}
 .prevsub-item{border:1px solid var(--line2);border-radius:9px;padding:9px 11px;margin-top:9px;background:var(--panel)}
 .prevsub-item .psi-name{font-weight:700;font-size:13px;color:var(--txt)}
 .prevsub-item .psi-desc{color:var(--mut);font-size:12px;line-height:1.55;margin-top:3px}
 .prevmock{position:relative;border:1px solid var(--line2);border-radius:10px;background:var(--bg);padding:12px;margin:10px 0 4px;pointer-events:none;overflow:hidden}
 .mk-hud .mkh-led{position:absolute;top:11px;right:11px;width:9px;height:9px;border-radius:50%;background:var(--accent2,#7d9b63);box-shadow:0 0 8px var(--accent2,#7d9b63)}
 .mk-hud .mkh-stage{font-weight:800;color:var(--accent-lit,#e0b873);letter-spacing:.05em;font-size:13px}
 .mk-hud .mkh-row{display:flex;justify-content:space-between;font-size:12px;color:var(--mut);margin-top:5px}
 .mk-hud .mkh-row b{color:var(--txt);font-variant:tabular-nums}
 .mk-hud .mkh-find{margin-top:8px;font-size:11.5px;color:var(--teal-lit,#9bc07e);border-top:1px solid var(--line);padding-top:7px}
 .mk-an .mka-sec{color:var(--accent-lit,#e0b873);font-size:10px;text-transform:uppercase;letter-spacing:.06em;font-weight:700;margin:2px 0 6px}
 .mk-an .mka-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:10px}
 .mk-an .mka-card{background:var(--panel);border:1px solid var(--line2);border-radius:8px;padding:7px 9px}
 .mk-an .mka-card i{color:var(--dim);font-style:normal;font-size:9.5px;text-transform:uppercase;letter-spacing:.05em;display:block}
 .mk-an .mka-card b{font-size:16px;color:var(--txt)}
 .mk-an .mka-bars{display:flex;align-items:flex-end;gap:6px;height:52px;padding:6px;background:var(--panel);border:1px solid var(--line2);border-radius:8px}
 .mk-an .mka-bars i{flex:1;background:linear-gradient(var(--accent-lit,#e0b873),var(--accent,#c2924c));border-radius:3px 3px 0 0}
 .mk-pill{display:flex;align-items:center;gap:8px;border-radius:999px;padding:8px 14px}
 .mk-pill .mkp-dot{width:9px;height:9px;border-radius:50%;background:var(--accent2,#7d9b63)}
 .mk-pill b{color:var(--accent-lit,#e0b873);font-size:13px}
 .mk-pill .mkp-s{color:var(--mut);font-size:11px;margin-left:auto}
 .mk-coach .mkc-msg{border-radius:10px;padding:7px 10px;font-size:12px;margin-bottom:6px;line-height:1.5;max-width:88%}
 .mk-coach .mkc-msg.you{background:var(--accent);color:#241a02;margin-left:auto}
 .mk-coach .mkc-msg.bot{background:var(--panel);border:1px solid var(--line2);color:var(--mut)}
 .mk-coach .mkc-chip{display:inline-block;background:var(--accent2,#7d9b63);color:#20240f;font-size:11px;font-weight:700;border-radius:7px;padding:4px 10px}
 .tourmedia img{max-width:100%;max-height:170px;border-radius:8px;border:1px solid var(--line2);margin-top:10px;cursor:zoom-in;display:block}
 .touredit{margin-left:8px;background:none;border:1px solid var(--line2);color:var(--mut);border-radius:6px;font-size:11px;padding:2px 8px;cursor:pointer;font-weight:600}
 .touredit:hover{color:var(--txt);border-color:var(--accent,#e0b873)}
 #lightbox{position:fixed;inset:0;z-index:900;background:rgba(6,5,3,.9);display:flex;align-items:center;justify-content:center;cursor:zoom-out}
 #lightbox img{max-width:92vw;max-height:92vh;border-radius:10px}
 #tutedit{position:fixed;inset:0;z-index:800;background:rgba(6,5,3,.6);display:flex;align-items:center;justify-content:center}
 #tutedit .tew{background:var(--panel);border:1px solid var(--line2);border-radius:14px;padding:18px 20px;width:520px;max-width:94vw;max-height:90vh;overflow:auto}
 #tutedit h3{margin:0 0 4px}
 #tutedit .teid{color:var(--dim);font-size:11px;margin-bottom:6px}
 #tutedit label{display:block;color:var(--mut);font-size:12px;margin:10px 0 4px}
 #tutedit input[type=text],#tutedit textarea{width:100%;box-sizing:border-box;background:var(--bg2);border:1px solid var(--line2);border-radius:8px;color:var(--txt);font:inherit;padding:8px;font-size:13px}
 #tutedit textarea{min-height:130px;resize:vertical}
 #tutedit .tebtns{display:flex;gap:8px;margin-top:14px;align-items:center;flex-wrap:wrap}
 #tutedit .teimg{max-height:90px;border-radius:6px;border:1px solid var(--line2);display:block;margin-top:6px}
 #tutedit .tehint{color:var(--dim);font-size:11px;margin-top:8px;line-height:1.5}
 .howbox{background:var(--panel);border:1px solid var(--line2);border-radius:12px;padding:2px 18px 4px;margin:0 0 14px;max-width:720px}
 .howbox summary{cursor:pointer;font-weight:700;padding:10px 0;color:var(--txt);list-style:none;display:flex;align-items:center;gap:9px}
 .howbox summary::-webkit-details-marker{display:none}
 .howbox summary::before{content:'›';color:var(--accent-lit,#e0b873);font-weight:800;transition:transform .15s}
 .howbox[open] summary::before{transform:rotate(90deg)}
 .howbox .hwnote{color:var(--mut);font-size:13px;margin:2px 0 10px;line-height:1.6}
 .howbox ol{margin:2px 0 12px;padding-left:20px;color:var(--mut);line-height:1.65;font-size:13px}
 .howbox li{margin:4px 0}
 .howbox b{color:var(--txt)}
 .tourstepn{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.1em;margin-bottom:6px}
 .tourtitle{font-size:19px;font-weight:700}
 .tourbody{color:var(--mut);font-size:14px;line-height:1.65;margin-top:9px}
 .tourbody b{color:var(--txt)}
 .tourdots{display:flex;gap:5px;margin-top:15px;flex-wrap:wrap}
 .tourdots i{width:6px;height:6px;border-radius:50%;background:var(--line2)}
 .tourdots i.on{background:var(--accent,#e0b873)}
 .tourbtns{display:flex;align-items:center;gap:8px;margin-top:16px}
 .tourbtns .grow{flex:1}
 .tourbtn{font:inherit;font-weight:600;border:none;border-radius:8px;padding:8px 16px;cursor:pointer;font-size:13px}
 .tourbtn.go{background:var(--accent,#e0b873);color:#1a1005}
 .tourbtn.ghost{background:none;border:1px solid var(--line2);color:var(--mut)}
 .tourbtn.ghost:hover{color:var(--txt)}
 .tourx{position:absolute;top:8px;right:10px;background:none;border:none;color:var(--dim);font:inherit;font-size:15px;line-height:1;padding:4px 6px;cursor:pointer;border-radius:6px}
 .tourx:hover{color:var(--txt)}
 .tournoauto{display:flex;align-items:center;gap:7px;margin-top:12px;font-size:12.5px;color:var(--mut);cursor:pointer}
 .introbox{background:var(--sand-dim,rgba(212,148,58,.10));border:1px solid var(--sand-glow,rgba(212,148,58,.3));border-radius:12px;padding:14px 18px;margin:0 0 14px;max-width:720px}
 .introt{font-weight:700;margin-bottom:8px}
 .introl{margin:0;padding-left:20px;line-height:1.7;font-size:14px;color:var(--mut)}
 .introl b{color:var(--txt)}
 .introx{margin-top:10px;background:none;border:1px solid var(--line2);border-radius:8px;color:var(--mut);padding:6px 12px;font:inherit;font-size:12.5px;cursor:pointer}
 .introx:hover{color:var(--txt);border-color:var(--dim)}
 .calbanner{display:none;background:rgba(224,92,92,.12);border:1px solid rgba(224,92,92,.4);color:#f0b0b0;border-radius:10px;padding:10px 14px;margin:0 0 12px;font-size:13px;line-height:1.5}
 .calbanner.show{display:block}
 .calbanner b{color:#f5c6c6}
 .calbanner.cycwarn{background:rgba(224,179,74,.12);border-color:rgba(224,179,74,.42);color:#e8cf9a}
 .calbanner.cycwarn b{color:#f2ddb2}
 .advcal{max-width:720px;margin-top:8px;border-top:1px solid var(--line);padding-top:10px}
 .advbeta{font-size:10px;font-weight:600;color:var(--accent);border:1px solid var(--line2);border-radius:999px;padding:2px 8px;vertical-align:middle;margin-left:6px}
 .cuecap{display:flex;flex-direction:column;gap:8px;margin-top:10px}
 .cuerow{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
 .cuerow .cuename{font-weight:600;min-width:180px}
 .cuerow .cuestat{font-size:12px;color:var(--mut);flex:1;min-width:120px}
 .cuerow .cuestat.ok{color:var(--accent2,#7dbb6a)}
 .cuewiz{max-width:560px;margin-top:12px;background:var(--panel2,var(--panel));border:1px solid var(--line2);border-radius:12px;padding:16px}
 .cwhead{display:flex;align-items:center;justify-content:space-between;font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--mut)}
 .cwx{background:none;border:none;color:var(--dim);cursor:pointer;font-size:15px}
 .cwx:hover{color:var(--txt)}
 .cwtitle{font-size:17px;font-weight:700;margin-top:6px}
 .cwtip{font-size:13.5px;color:var(--mut);margin-top:6px;line-height:1.5}
 .cwprevwrap{margin-top:12px;min-height:70px;display:flex;align-items:center;justify-content:center;background:#0a0805;border:1px solid var(--line2);border-radius:8px;padding:10px}
 .cwprev{display:none;image-rendering:pixelated;max-width:100%;border-radius:4px}
 .cwph{color:var(--dim);font-size:12.5px}
 .cwstat{font-size:12.5px;color:var(--mut);margin-top:10px;line-height:1.5;min-height:16px}
 .cwstat b{color:var(--txt)}
 .cwbtns{display:flex;gap:9px;margin-top:14px;flex-wrap:wrap}
 .cuegallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px;margin-top:12px;max-width:720px}
 .cuecard{background:var(--panel2,var(--panel));border:1px solid var(--line2);border-radius:10px;overflow:hidden}
 .cuecardh{font-size:12px;font-weight:600;padding:8px 10px;border-bottom:1px solid var(--line)}
 .cuethumb{display:block;width:100%;background:#0a0805;image-rendering:pixelated;max-height:130px;object-fit:contain}
 .cuethumb.none{display:flex;align-items:center;justify-content:center;height:74px;color:var(--dim);font-size:12px}
 .cuecardf{display:flex;align-items:center;gap:6px;padding:8px 10px;font-size:11.5px;color:var(--mut);flex-wrap:wrap}
 .cuecardf .ok{color:var(--accent2,#7dbb6a);font-weight:600}
 .cuecardf .grow{flex:1}
 .frstat{font-size:12px;opacity:.75;margin-right:8px}
 .rgthumb{height:26px;max-width:110px;border-radius:5px;border:1px solid var(--line2);object-fit:cover;image-rendering:pixelated;box-shadow:0 1px 3px rgba(0,0,0,.5)}
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
 .ok{position:fixed;right:16px;bottom:14px;background:#231c17;color:#e6dccb;
  border:1px solid var(--line2);border-radius:10px;padding:8px 13px;font-size:13px;opacity:0;
  box-shadow:0 10px 26px -10px rgba(0,0,0,.7);transition:opacity .2s} .ok.show{opacity:1}
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
 .gate-card{width:100%;max-width:430px;animation:fadeIn .55s var(--ease) .1s both}
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
 #welGo{width:100%;background:var(--accent);color:#241a02;font-size:15px;padding:13px;border-radius:11px;margin-top:2px}
 #welGo:hover{filter:brightness(1.05);transform:translateY(-1px)}
 #welGo[disabled]{opacity:.6;transform:none;cursor:default}
 .gate-err{min-height:18px;margin-top:11px;color:#f0a6a6;font-size:13px}
 .gate-foot{margin-top:24px;color:var(--dim);font-size:12.5px;line-height:1.6}
 .wel-list{margin:0 0 18px;padding:0;list-style:none;text-align:left}
 .wel-list li{margin:0 0 10px;font-size:13.5px;line-height:1.5;color:var(--mut)}
 .wel-list li b{color:var(--txt)}
 .wel-again{display:flex;gap:7px;align-items:center;justify-content:center;margin-top:12px;color:var(--dim);font-size:12.5px;cursor:pointer}
 .wel-links{margin-top:14px;display:flex;gap:14px;justify-content:center}
 .wel-links a{color:var(--accent);font-size:12.5px;text-decoration:none}
 .wel-links a:hover{text-decoration:underline}
 .gc-ver{color:var(--dim);font-size:11.5px;font-weight:600;margin-left:6px}
 /* ---- setup wizard (first-run steps 2-4) + Trust Center ---- */
 #setup{position:fixed;inset:0;z-index:980;display:none;background:var(--bg);flex-direction:column}
 #setup.show{display:flex;animation:fadeIn .3s var(--ease)}
 .sup-head{display:flex;align-items:center;gap:18px;padding:14px 22px;border-bottom:1px solid var(--line);background:var(--bg2);flex-wrap:wrap}
 .sup-brand{display:inline-flex;align-items:center;gap:9px;font-weight:800;font-size:15px}
 .sup-rail{display:flex;gap:4px;list-style:none;margin:0;padding:0;flex-wrap:wrap}
 .sup-rail li{display:flex;align-items:center;gap:7px;padding:6px 11px;border-radius:9px;color:var(--dim);font-size:12.5px;font-weight:700}
 .sup-rail li .n{width:20px;height:20px;border-radius:50%;border:1.5px solid var(--line2);display:inline-flex;align-items:center;justify-content:center;font-size:11px}
 .sup-rail li.cur{color:var(--accent-lit);background:var(--sand-dim)}
 .sup-rail li.cur .n{border-color:var(--accent);color:var(--accent-lit)}
 .sup-rail li.done{color:var(--mut)}
 .sup-rail li.done .n{border-color:#7faf5d;color:#7faf5d}
 .sup-body{flex:1;overflow-y:auto;padding:24px;max-width:1060px;width:100%;margin:0 auto;box-sizing:border-box}
 .sup-foot{display:flex;gap:12px;align-items:center;padding:13px 22px;border-top:1px solid var(--line);background:var(--bg2)}
 .sup-note{color:var(--mut);font-size:12.5px}
 .sup-h1{font-size:21px;font-weight:800;margin:0 0 6px}
 .sup-sub{color:var(--mut);font-size:13.5px;line-height:1.55;margin:0 0 18px;max-width:76ch}
 .plat-tabs{display:flex;gap:4px;margin:0 0 16px;padding:3px;background:var(--bg2);border:1px solid var(--line2);border-radius:11px;width:max-content}
 .plat-tabs button{background:transparent;border:0;border-radius:8px;color:var(--mut);padding:7px 16px;font:inherit;font-weight:700;cursor:pointer}
 .plat-tabs button[aria-selected="true"]{background:var(--sand-dim);color:var(--accent-lit);box-shadow:inset 0 0 0 1px var(--accent)}
 .plat-badge{display:inline-block;margin-left:10px;padding:3px 9px;border-radius:99px;background:var(--sand-dim);color:var(--accent-lit);font-size:11px;font-weight:800;letter-spacing:.4px}
 .cap-card{border:1px solid var(--line2);border-radius:13px;background:var(--panel);padding:15px 17px;margin:0 0 13px}
 .cap-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
 .cap-title{font-weight:800;font-size:14.5px}
 .cap-badge{padding:2.5px 9px;border-radius:99px;font-size:10.5px;font-weight:800;letter-spacing:.4px;border:1px solid var(--line2);color:var(--mut)}
 .cap-badge.req{background:rgba(194,146,76,.14);border-color:rgba(194,146,76,.4);color:var(--accent-lit)}
 .cap-badge.opt{background:rgba(90,140,190,.12);border-color:rgba(90,140,190,.35);color:#9ec4e8}
 .cap-st{margin-left:auto;display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:800}
 .cap-st .dot{width:9px;height:9px;border-radius:50%;background:var(--dim)}
 .cap-st.ok{color:#8fce7d}.cap-st.ok .dot{background:#7faf5d}
 .cap-st.no{color:#f0a6a6}.cap-st.no .dot{background:#e07a6a}
 .cap-st.mid{color:#e8cf8f}.cap-st.mid .dot{background:#d9a441}
 .cap-st.off{color:var(--mut)}.cap-st.off .dot{background:var(--line2)}
 /* Sequential progression (one engine for permissions + calibration):
    UPCOMING steps fade but stay readable; the ACTIVE step is highlighted;
    every state also carries a text chip -- never colour/opacity alone. */
 .step-upcoming{opacity:.55}
 .step-upcoming .cap-actions button:not([data-act="code"]):not([data-cal="code"]){pointer-events:none;opacity:.55}
 .step-active{border-color:#d9a441;box-shadow:0 0 0 1px rgba(217,164,65,.35)}
 .step-chip{display:inline-flex;align-items:center;font-size:11px;font-weight:800;
   border-radius:8px;padding:2px 8px;margin-left:8px;letter-spacing:.02em}
 .step-chip.next{background:rgba(217,164,65,.18);color:#e8cf8f}
 .step-chip.done{background:rgba(127,175,93,.16);color:#8fce7d}
 .step-chip.up{background:var(--line2);color:var(--mut)}
 .step-chip.rev{background:rgba(224,122,106,.16);color:#f0a6a6}
 .step-chip.optional{background:var(--line2);color:var(--mut)}
 .step-num{display:inline-flex;align-items:center;justify-content:center;
   width:22px;height:22px;border-radius:50%;border:1px solid var(--line2);
   font-size:12px;font-weight:800;margin-right:9px;flex:none}
 .step-active .step-num{border-color:#d9a441;color:#e8cf8f}
 .sup-progress{font-size:12.5px;color:var(--mut);margin:2px 0 10px}
 .cal-pre{border:1px dashed var(--line2);border-radius:11px;padding:10px 13px;
   margin:0 0 13px;font-size:13px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
 .gd-back{margin:0 0 10px}
 .gd-sec{margin:12px 0}
 .gd-sec h4{font-size:12.5px;text-transform:uppercase;letter-spacing:.06em;
   color:var(--mut);margin:0 0 5px}
 .gd-sec ol,.gd-sec ul{margin:4px 0 4px 20px;padding:0}
 .gd-sec li{margin:3px 0;font-size:13.5px}
 .gd-kv{font-size:13.5px;margin:3px 0}
 .gd-out{margin-top:10px;font-size:13.5px}
 .gd-out .ok{color:#8fce7d}.gd-out .no{color:#f0a6a6}
 .gd-cues{display:flex;gap:10px;flex-wrap:wrap;margin:8px 0}
 .gd-cue{border:1px solid var(--line2);border-radius:10px;padding:9px 11px;min-width:150px}
 .gd-cue img{max-width:140px;display:block;margin:6px 0;border-radius:6px}
 .gd-cue .st{font-size:12px;font-weight:800}
 .cap-desc{color:var(--mut);font-size:13px;line-height:1.55;margin:8px 0 10px;max-width:82ch}
 .cap-facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:8px 16px;margin:0 0 11px}
 .cap-facts div{font-size:12px;color:var(--mut);line-height:1.5}
 .cap-facts b{display:block;color:var(--txt);font-size:11px;letter-spacing:.4px;text-transform:uppercase;margin-bottom:2px}
 .cap-actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
 .cap-actions .btn2{font-size:12px}
 .cap-more{margin-top:9px}
 .cap-more summary{cursor:pointer;color:var(--accent);font-size:12.5px;font-weight:700}
 .cap-more[open] summary{margin-bottom:7px}
 .cap-test{margin-top:10px;border:1px dashed var(--line2);border-radius:10px;padding:10px 12px;font-size:12.5px;color:var(--mut);display:none}
 .cap-test.show{display:block}
 .cap-test img{max-width:240px;border-radius:7px;border:1px solid var(--line2);display:block;margin:8px 0}
 .cap-test input{background:var(--field);border:1px solid var(--line2);border-radius:8px;color:var(--txt);padding:8px 10px;font:inherit;width:220px}
 .sup-group{margin:22px 0 10px;color:var(--dim);font-size:11.5px;font-weight:800;letter-spacing:.6px;text-transform:uppercase}
 .cal-keys{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--dim)}
 .cal-eg{margin:8px 0;padding:9px 11px;border:1px dashed var(--line2);border-radius:9px;font-size:12px;color:var(--dim)}
 .cal-eg img{max-width:320px;display:block;border-radius:7px;border:1px solid var(--line2);margin-top:6px}
 .rdy-item{display:flex;gap:11px;align-items:flex-start;border:1px solid var(--line2);border-radius:11px;background:var(--panel);padding:11px 14px;margin:0 0 9px}
 .rdy-item .mark{font-weight:900;font-size:12px;min-width:44px;text-align:center;padding:3px 7px;border-radius:8px}
 .rdy-item.pass .mark{color:#8fce7d;background:rgba(127,175,93,.12)}
 .rdy-item.fail .mark{color:#f0a6a6;background:rgba(224,122,106,.12)}
 .rdy-item.warn .mark{color:#e8cf8f;background:rgba(217,164,65,.12)}
 .rdy-item.info .mark{color:var(--mut);background:var(--bg2)}
 .rdy-item .t{font-weight:800;font-size:13px}
 .rdy-item .d{color:var(--mut);font-size:12.5px;line-height:1.5}
 .rdy-item .btn2{margin-left:auto;flex-shrink:0}
 #supReturn{position:fixed;right:18px;bottom:18px;z-index:985;display:none;background:var(--accent);color:#241a02;font-weight:800;border:0;border-radius:99px;padding:11px 18px;cursor:pointer;box-shadow:0 8px 22px rgba(0,0,0,.4)}
 #supReturn.show{display:block}
 .tc-sec{border:1px solid var(--line2);border-radius:13px;background:var(--panel);padding:15px 17px;margin:0 0 14px}
 .tc-sec h3{margin:0 0 8px;font-size:14px}
 .tc-kv{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;font-size:12.5px}
 .tc-kv b{color:var(--mut);font-weight:600}
 .tc-kv span{font-family:ui-monospace,Menlo,monospace;word-break:break-all}
 .tc-files{width:100%;border-collapse:collapse;font-size:12.5px}
 .tc-files td{padding:5px 8px;border-top:1px solid var(--line);color:var(--mut)}
 .tc-files td:first-child{font-family:ui-monospace,Menlo,monospace;color:var(--txt)}
 .tc-danger{border-color:rgba(224,122,106,.4)}
 pre.tc-pre{background:var(--bg2);border:1px solid var(--line2);border-radius:9px;padding:10px;font-size:11px;overflow-x:auto;max-height:260px;color:var(--mut);white-space:pre-wrap;word-break:break-all}
</style></head><body>
 <div id="splash">
   <div class="sp-logo"><svg class="pp-gem" viewBox="0 0 24 24" fill="none"><path d="M6.5 4h11l4 5.2L12 21 2.5 9.2z" fill="#fff"/><path d="M2.5 9.2h19M6.5 4l2.6 5.2L12 21M17.5 4l-2.6 5.2L12 21M9.1 9.2h5.8" stroke="#0a0908" stroke-opacity=".32" stroke-width=".8" stroke-linejoin="round"/></svg> Prospector <b>Lite</b></div>
   <div class="sp-sub">loading&hellip;</div>
   <div class="sp-bar"><i></i></div>
 </div>
 <div id="gate">
   <div class="gate-left">
     <div class="gate-paths" id="gatePaths"></div>
     <div class="gate-fade"></div>
     <div class="gl-top"><span class="gl-pk"><svg class="pp-gem" viewBox="0 0 24 24" fill="none"><path d="M6.5 4h11l4 5.2L12 21 2.5 9.2z" fill="#fff"/><path d="M2.5 9.2h19M6.5 4l2.6 5.2L12 21M17.5 4l-2.6 5.2L12 21M9.1 9.2h5.8" stroke="#0a0908" stroke-opacity=".32" stroke-width=".8" stroke-linejoin="round"/></svg></span> Prospector Lite</div>
     <div class="gl-quote">
       <p>&ldquo;Set it up once and let it dig. Prospector Lite runs the whole panning loop while you&rsquo;re away.&rdquo;</p>
       <footer>free &middot; source available for inspection</footer>
     </div>
   </div>
   <div class="gate-right">
     <div class="gate-card" role="dialog" aria-labelledby="welTitle">
       <div class="gc-logo"><span class="gl-pk"><svg class="pp-gem" viewBox="0 0 24 24" fill="none"><path d="M6.5 4h11l4 5.2L12 21 2.5 9.2z" fill="#fff"/><path d="M2.5 9.2h19M6.5 4l2.6 5.2L12 21M17.5 4l-2.6 5.2L12 21M9.1 9.2h5.8" stroke="#0a0908" stroke-opacity=".32" stroke-width=".8" stroke-linejoin="round"/></svg></span> Prospector Lite <span class="gc-ver" id="welVer"></span></div>
       <h1 id="welTitle">Welcome</h1>
       <div class="gc-sub">A free macro for Roblox <i>Prospecting</i>, with its source available for inspection. It reads your screen and presses ordinary keys and clicks &mdash; nothing more.</div>
       <ul class="wel-list">
         <li><b>External only.</b> It never injects into Roblox, never modifies the game or its files, and never reads game memory.</li>
         <li><b>Private by design.</b> Everything stays on this computer: no account, no access code, no analytics, and no network requests unless you set up optional notifications yourself.</li>
         <li><b>Permissions.</b> macOS asks for Screen Recording (to see the game), Accessibility (to press keys) and Input Monitoring (so the Safe&nbsp;Stop hotkey always works). The next step explains and tests each one before macOS shows any prompt. Windows needs no admin rights.</li>
         <li><b>Safe Stop.</b> Esc or Ctrl+K stops the macro instantly and releases every key and mouse button.</li>
       </ul>
       <button type="button" id="welGo" class="btn">Continue</button>
       <div class="wel-links" id="welActions" style="display:none">
         <a href="#" id="welContinue" style="display:none">Continue through setup</a>
         <a href="#" id="welReview">Review setup</a>
         <a href="#" id="welCal" style="display:none">Review calibration</a>
         <a href="#" id="welTut">Start tutorial</a>
         <a href="#" id="welOpenApp" style="display:none">Open the main app</a>
         <a href="#" id="welTrustC">Trust Center</a>
       </div>
       <button type="button" id="welSkip" class="btn2" style="width:100%;margin-top:8px">Skip wizard</button>
       <label class="wel-again"><input type="checkbox" id="welAgain" checked> Show this screen at every launch</label>
       <label class="wel-again"><input type="checkbox" id="welSkipAuto"> Skip the setup wizard automatically on launch</label>
       <label class="wel-again"><input type="checkbox" id="welTutAuto" checked> Open tutorial whenever Prospector Lite opens</label>
       <div class="wel-again" id="welAgainErr" style="display:none;color:#e07a6a" role="alert"></div>
       <div class="wel-links">
         <a href="#" id="welSrc" style="display:none">View source</a>
         <a href="#" id="welPriv">Privacy &amp; data</a>
         <a href="#" id="welSec">Security</a>
       </div>
       <div class="gate-foot" id="welBuild"></div>
       <div class="gate-foot" id="welMigr" style="display:none"></div>
     </div>
   </div>
 </div>

 <div id="setup" role="dialog" aria-modal="true" aria-labelledby="supTitle">
   <div class="sup-head">
     <span class="sup-brand"><svg class="pp-gem" viewBox="0 0 24 24" fill="none"><path d="M6.5 4h11l4 5.2L12 21 2.5 9.2z" fill="#fff"/><path d="M2.5 9.2h19M6.5 4l2.6 5.2L12 21M17.5 4l-2.6 5.2L12 21M9.1 9.2h5.8" stroke="#0a0908" stroke-opacity=".32" stroke-width=".8" stroke-linejoin="round"/></svg> <span id="supTitle">Prospector Lite setup</span></span>
     <ol class="sup-rail" id="supRail" aria-label="Setup steps">
       <li data-step="welcome"><span class="n">1</span> Welcome</li>
       <li data-step="trust"><span class="n">2</span> Trust &amp; Permissions</li>
       <li data-step="cal"><span class="n">3</span> Guided Calibration</li>
       <li data-step="ready"><span class="n">4</span> Readiness Check</li>
       <li data-step="app"><span class="n">5</span> Prospector Lite</li>
     </ol>
   </div>
   <div class="sup-body" id="supBody" tabindex="-1"></div>
   <div class="sup-foot">
     <button type="button" class="btn2" id="supBack">&larr; Back</button>
     <div class="grow"></div>
     <span class="sup-note" id="supNote" aria-live="polite"></span>
     <button type="button" class="btn2" id="supSkip">Skip wizard</button>
     <button type="button" class="btn" id="supNext">Continue &rarr;</button>
   </div>
 </div>
 <button type="button" id="supReturn" aria-label="Return to setup">&larr; Return to setup</button>

 <div class="topbar">
   <div class="brand"><svg class="pp-gem" viewBox="0 0 24 24" fill="none"><path d="M6.5 4h11l4 5.2L12 21 2.5 9.2z" fill="#fff"/><path d="M2.5 9.2h19M6.5 4l2.6 5.2L12 21M17.5 4l-2.6 5.2L12 21M9.1 9.2h5.8" stroke="#0a0908" stroke-opacity=".32" stroke-width=".8" stroke-linejoin="round"/></svg> Prospector <b>Lite</b></div>
   <div class="modestrip" id="modestrip" role="tablist" aria-label="Macro mode">
     <button type="button" class="mtab" id="mode_classic" role="tab" aria-selected="false" title="The proven built-in cycle — modes, routes, relics, recovery">Classic</button>
     <button type="button" class="mtab" id="mode_studio" role="tab" aria-selected="false" title="Run a prospecting build authored in Prospector Studio">Studio Build</button>
     <button type="button" class="mtab" id="mode_script" role="tab" aria-selected="false" title="Run a general-automation Studio Script through the same engine">Studio Script</button>
   </div>
   <div class="grow"></div>
   <button class="hammenu" id="hammenu" title="Menu" aria-label="Menu">&#9776;</button>
   <div class="topgroup" id="topgroup">
   <input class="topfield sm" id="buildname" placeholder="build name">
   <button class="btn2" id="savebuild">Save build</button>
   <button class="btn2" id="coachtoggle" title="Describe a problem, get the setting to change">✦ Coach</button>
   <button class="btn2" id="prevtoggle" title="Show or hide the Preview panel">⧉ Preview</button>
   <button class="btn2" id="analyticsbtn" title="Open the analytics dashboard">☷ Analytics</button>
   <button class="btn2" id="popout" title="Pop out a floating control">⤢ Pop out</button>
   <button class="btn2" id="hudbtn" title="Live overlay HUD beside the game">⬒ HUD</button>
   <button class="btn2" id="tourbtn" title="Tutorials: the full tour and every page walkthrough">❓ Tutorial</button>
   </div>
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
   <div class="content"><div class="cwrap">{{PANELS}}</div></div>
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
           <option value="offline">Offline brain, free, instant</option>
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
           <input id="ckey" type="password" autocomplete="off" placeholder="paste key, stays on this PC"></div>
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
   <aside class="preview" id="preview">
     <div class="prev-head"><span class="prev-mark">⧉</span>
       <div class="prev-titlewrap"><div class="prev-title">Preview</div>
         <span class="prev-sub" id="prevsub">hover anything to inspect it</span></div>
       <button id="prevclose" title="Hide the preview panel">✕</button>
     </div>
     <div class="prev-body" id="prevbody"></div>
   </aside>
 </div>
 <div class="ok" id="toast"></div>
 <div class="mmodal" id="mcfm" role="dialog" aria-modal="true" aria-labelledby="mcfmtitle">
  <div class="mbox"><h3 id="mcfmtitle"></h3><p id="mcfmbody"></p>
   <div class="mrow"><button type="button" class="btn" id="mcfmyes">Yes</button>
    <button type="button" class="btn2" id="mcfmno">Cancel</button></div></div></div>
 <div class="mmodal" id="skipmodal" role="dialog" aria-modal="true" aria-labelledby="skiptitle" style="z-index:1300">
  <div class="mbox" style="max-width:480px"><h3 id="skiptitle">Skip the setup wizard?</h3>
   <div class="skipopt"><button type="button" class="btn" id="skipSession">Skip this time</button>
    <p>Open the app now. Setup stays exactly as it is and the wizard can come back next launch.</p></div>
   <div class="skipopt"><button type="button" class="btn2" id="skipMark">Mark wizard complete</button>
    <p>Records the wizard as reviewed. Anything actually missing (permissions, calibration) still shows as a warning.</p></div>
   <div class="skipopt"><button type="button" class="btn2" id="skipAuto">Skip wizard automatically in future</button>
    <p>From now on, launches go straight to the app. Explicitly opening Welcome still shows the wizard. You can turn this off in Welcome, Settings, or the Trust Center.</p></div>
   <div class="mrow"><button type="button" class="btn2" id="skipCancel">Cancel</button></div></div></div>
 <div id="bsplash" aria-hidden="true"><div class="bsp-card"><span class="bsp-mark">&#9670;</span><span class="bsp-txt"></span></div></div>
 <div id="tour" class="tour" style="display:none"><div id="tourspot" class="tourspot"></div>
  <div id="tourpop" class="tourpop"><div id="tourarrow"></div>
   <button type="button" class="tourx" id="tourx" aria-label="Close tutorial">✕</button>
   <div class="tourstepn" id="tourstepn"></div>
   <div class="tourtitle" id="tourtitle"></div><div class="tourbody" id="tourbody"></div>
   <div class="tourdots" id="tourdots"></div>
   <div class="tourbtns"><button type="button" class="tourbtn ghost" id="tourskip">Skip tour</button>
    <span class="grow"></span><button type="button" class="tourbtn ghost" id="tourback">Back</button>
    <button type="button" class="tourbtn go" id="tournext">Next</button></div>
   <label class="tournoauto" id="tourNoAutoRow" style="display:none"><input type="checkbox" id="tourNoAuto"> Do not open automatically in future</label></div></div>
 <div id="lightbox" style="display:none"><img id="lightboximg" alt=""></div>
 <div id="diagdrawer" role="dialog" aria-label="Warning details">
  <div class="ddhead"><h3 id="ddtitle">Warnings</h3>
   <button type="button" id="ddclose" aria-label="Close warning details">&#10005;</button></div>
  <div id="ddlist"></div>
  <div id="ddbody"></div></div>
 <div id="diagrec" role="dialog" aria-label="Recommended value"></div>
 <div id="faqmodal" role="dialog" aria-modal="true" aria-labelledby="faqtitle">
  <div class="faqbox"><h3 id="faqtitle">FAQ and troubleshooting</h3>
   <input id="faqsearch" type="text" placeholder="Search questions and symptoms&hellip;" spellcheck="false">
   <div id="faqlist"></div>
   <div id="faqentry" style="display:none"></div>
   <div class="mrow" style="margin-top:12px">
    <button type="button" class="btn2" id="faqback" style="display:none">&#8592; All questions</button>
    <button type="button" class="btn2" id="faqclose">Close</button></div></div></div>
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
 function preset(p,lbl){fields().forEach(el=>{const k=el.dataset.key; if(!(k in p))return;
   if(el.dataset.type==='bool')el.checked=!!p[k]; else el.value=p[k];});
   if(window.syncCycle)syncCycle();if(window.__undoCommit)window.__undoCommit();if(window.splash)window.splash('<b>'+(lbl||'Preset')+'</b> loaded');}
 function toast(t){const e=$('#toast');e.textContent=t;e.classList.add('show');
   clearTimeout(window._tt);window._tt=setTimeout(()=>e.classList.remove('show'),1800);}
 window.splash=function(msg){var s=document.getElementById('bsplash');if(!s)return;
   var txt=s.querySelector('.bsp-txt');if(txt)txt.innerHTML=msg||'Loaded';
   clearTimeout(s._t);
   if(document.body.classList.contains('reduce-motion')){s.style.opacity='1';s._t=setTimeout(function(){s.style.opacity='';},1300);return;}
   s.classList.remove('bspon');void s.offsetWidth;s.classList.add('bspon');
   s._t=setTimeout(function(){s.classList.remove('bspon');},1750);};
 (function(){var h=document.getElementById('hammenu'),tb=document.querySelector('.topbar');if(h&&tb){
   h.addEventListener('click',function(e){e.stopPropagation();tb.classList.toggle('hmopen');});
   document.addEventListener('click',function(e){if(!tb.contains(e.target))tb.classList.remove('hmopen');});
   var tg=document.getElementById('topgroup');if(tg)tg.addEventListener('click',function(e){if(e.target.closest('button'))tb.classList.remove('hmopen');});}})();
 (function(){var mins=document.querySelector('[data-key="AUTOSTOP_MINUTES"]');if(!mins)return;
   var box=document.createElement('span');box.className='hrfield';
   var lb=document.createElement('span');lb.className='hrlbl';lb.textContent='or hours';
   var hi=document.createElement('input');hi.type='number';hi.min='0';hi.step='0.5';hi.className='cnum hrin';
   function toH(){var m=parseInt(mins.value||'0',10)||0;hi.value=Math.round(m/60*100)/100;}
   toH();
   hi.addEventListener('input',function(){var v=parseFloat(hi.value||'0')||0;mins.value=Math.round(v*60);mins.dispatchEvent(new Event('input',{bubbles:true}));});
   hi.addEventListener('change',function(){mins.dispatchEvent(new Event('change',{bubbles:true}));});
   mins.addEventListener('input',toH);mins.addEventListener('change',toH);
   box.appendChild(lb);box.appendChild(hi);mins.parentNode.appendChild(box);})();
 (function(){
   function pref(id,cls,def,inv,cb){var el=document.getElementById(id);if(!el)return;
     var v=null;try{v=localStorage.getItem('pp_'+id);}catch(e){}
     var on=(v===null)?def:(v==='1');el.checked=on;
     function ap(fromUser){var active=inv?!el.checked:el.checked;document.body.classList.toggle(cls,active);if(cb)cb(active,fromUser);}
     ap(false);el.addEventListener('change',function(){ap(true);try{localStorage.setItem('pp_'+id,el.checked?'1':'0');}catch(e){}});}
   pref('set_compact','compact',false,false,function(on,fromUser){
     if(on){try{if(window.__showPreviewPanel)window.__showPreviewPanel(false);}catch(e){}}
     try{if(window.pywebview&&window.pywebview.api&&window.pywebview.api.set_window_compact)window.pywebview.api.set_window_compact(on);}catch(e){}});
   pref('set_wood','nowood',true,true);
   pref('set_reduce','reduce-motion',false,false);
 })();
 // Skip-wizard-automatically lives in the CONFIG (SKIP_WIZARD_AUTOMATICALLY,
 // shared with the Welcome gate + Trust Center), not localStorage: manual
 // wiring, stored value on render, revert on save failure.
 (function(){var el=document.getElementById('set_skipwiz');if(!el)return;
   function sync(){try{var a=window.pywebview&&window.pywebview.api;if(!a||!a.welcome_state)return;
     a.welcome_state().then(function(w){el.checked=!!(w&&w.skip_wizard_automatically);}).catch(function(){});}catch(e){}}
   window.addEventListener('pywebviewready',sync);
   if(window.pywebview&&window.pywebview.api)sync();
   // the same pref is writable from Welcome and the Trust Center --
   // re-read the stored value every time the Settings tab opens
   document.querySelectorAll('.tab[data-tab="settings"]').forEach(function(b){b.addEventListener('click',function(){setTimeout(sync,60);});});
   el.addEventListener('change',async function(){var want=!!el.checked;var r=null;
     try{r=await window.pywebview.api.wizard_skip_pref(want);}catch(e){r={ok:false,error:String(e)};}
     if(!r||!r.ok){el.checked=!want;
       if(window.toast)toast('Could not save this preference ['+((r&&r.error_code)||'PP-SKIP-SAVE')+']');}});})();
 // Tutorial auto-open lives in the CONFIG too (TUTORIAL_AUTO_OPEN, shared
 // with the Welcome gate + Trust Center + tour footer): stored value on
 // render, revert on save failure. Checked = auto_open true.
 (function(){var el=document.getElementById('set_tutauto');if(!el)return;
   function sync(){try{var a=window.pywebview&&window.pywebview.api;if(!a||!a.tutorial_state)return;
     a.tutorial_state().then(function(t){el.checked=!(t&&t.auto_open===false);}).catch(function(){});}catch(e){}}
   window.addEventListener('pywebviewready',sync);
   if(window.pywebview&&window.pywebview.api)sync();
   document.querySelectorAll('.tab[data-tab="settings"]').forEach(function(b){b.addEventListener('click',function(){setTimeout(sync,60);});});
   el.addEventListener('change',async function(){var want=!!el.checked;var r=null;
     try{r=await window.pywebview.api.tutorial_set_auto_open(want);}catch(e){r={ok:false,error:String(e)};}
     if(!r||!r.ok){el.checked=!want;
       if(window.toast)toast('Could not save this preference ['+((r&&r.error_code)||'PP-TUT-AUTO')+']');}});})();
 (function(){
   function T(id){return document.getElementById(id);}
   function wsleep(ms){return new Promise(function(r){setTimeout(r,ms);});}
   function raf2(){return new Promise(function(r){requestAnimationFrame(function(){requestAnimationFrame(function(){r();});});});}
   function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
   var TOUR_LABEL={main:'Tour',calibrate:'Calibration',cycle:'Cycle tuning',recovery:'Recovery',modes:'Modes',tracking:'Tracking',builds:'Builds',relics:'Relics',alerts:'Alerts and limits',studio:'Studio'};
   var TOUR_LIST=[['main','The full tour'],['calibrate','Calibration'],['cycle','Cycle tuning'],['recovery','Recovery and safety nets'],['modes','Modes: Treasure, Shards, Geodes'],['tracking','Tracking'],['builds','Builds'],['relics','Relics'],['alerts','Notifications and auto-stop'],['studio','Studio: build your own mode']];
   var TAB_TOURS={cal:'calibrate',cycle:'cycle',builds:'builds',relics:'relics','Earnings':'tracking','Tracker':'tracking','Notifications':'alerts','Auto-stop':'alerts','Treasure chest':'modes','Shards':'modes','Geodes':'modes',studio:'studio'};
   var PINNED_TABS={run:1,cycle:1,builds:1,cal:1,relics:1,hist:1,studio:1,keys:1,trust:1};
   var TOURS={},HELPMAP={},OWNER=false,LOADED=false,builtExplain=false;
   var TOUR=[],ti=0,curName='main',running=false;
   try{document.body.appendChild(T('tour'));}catch(e){}
   // The MAIN tour's chip key is schema-scoped and DISTINCT from the
   // legacy pre-schema key ('pp_tour_done'): the new tour must never
   // write the key the legacy-migration branch reads, or a first run
   // fabricates a 'migrated' record and a future schema bump can never
   // re-offer the tutorial.
   function flagKey(n){return n==='main'?'pp_tour_main_v2':'pp_tour_'+n;}
   function seen(n){try{return localStorage.getItem(flagKey(n))==='1';}catch(e){return true;}}
   function mark(n){try{localStorage.setItem(flagKey(n),'1');}catch(e){}}
   // MAIN-tutorial lifecycle lives Python-side (tutorial_state.json, atomic,
   // schema-versioned) -- localStorage remains only for the per-page mini
   // tours and as the legacy migration source.
   function tutApi(){return window.pywebview&&window.pywebview.api;}
   function tutMark(st,legacy){try{var a=tutApi();if(a)a.tutorial_mark(st,!!legacy);}catch(e){}}
   function setupOverlayVisible(){
     var s=document.getElementById('setup'),r=document.getElementById('supReturn');
     return (s&&s.classList.contains('show'))||(r&&r.classList.contains('show'));}
   function ytEmbed(u){if(!u)return '';var m=String(u).match(/(?:youtu\.be\/|v=|embed\/)([\w-]{6,})/);var id=m?m[1]:'';if(!id&&/^[\w-]{6,}$/.test(u))id=u;return id?('https://www.youtube.com/embed/'+id):'';}
   function mediaHTML(st){var h='';if(st&&st.img)h+='<img class="tmimg" src="'+esc(st.img)+'" alt="">';var e=ytEmbed(st&&st.vid);if(e)h+='<div class="ph-vid"><iframe src="'+e+'" allowfullscreen loading="lazy"></iframe></div>';return h;}
   // media + owner-edit affordances live in the card, built once
   var mediaBox=document.createElement('div');mediaBox.className='tourmedia';
   var stepLbl=document.createElement('span');
   var editBtn=document.createElement('button');editBtn.type='button';editBtn.className='touredit';editBtn.textContent='Edit';editBtn.style.display='none';
   (function(){var b=T('tourbody');if(b&&b.parentNode){b.parentNode.insertBefore(mediaBox,b.nextSibling);}var sn=T('tourstepn');if(sn){sn.textContent='';sn.appendChild(stepLbl);sn.appendChild(editBtn);}})();
   editBtn.onclick=function(){var st=TOUR[ti];if(st)openEditor(st.id,'card');};
   async function loadTutorials(force){
     if(LOADED&&!force)return;
     try{var c=await window.pywebview.api.tutorial_content();
       if(c&&c.tours){TOURS=c.tours;HELPMAP=c.help||{};OWNER=!!c.owner;LOADED=true;}}catch(e){}
     window.__HELP=HELPMAP;window.__OWNER=OWNER;
     buildExplain();
     if(window.__refreshPreview)window.__refreshPreview();
   }
   async function place(anim){
     var st=TOUR[ti],spot=T('tourspot'),pop=T('tourpop'),arr=T('tourarrow');
     if(anim){pop.classList.add('tfade');spot.classList.add('tfade');await wsleep(150);}
     stepLbl.textContent=(TOUR_LABEL[curName]||'Tour')+' · step '+(ti+1)+' of '+TOUR.length+'  ';
     T('tourtitle').textContent=st.title;T('tourbody').innerHTML=st.body;
     mediaBox.innerHTML=mediaHTML(st);
     editBtn.style.display=OWNER?'':'none';
     T('tourdots').innerHTML=TOUR.map(function(_x,k){return '<i class="'+(k===ti?'on':'')+'"></i>';}).join('');
     T('tourback').style.visibility=ti>0?'visible':'hidden';
     T('tournext').textContent=(ti===TOUR.length-1)?'Finish':'Next';
     if(st.tab){var grp=document.querySelector('.navgroup .tab[data-tab="'+st.tab+'"]');
       if(grp){var g=grp.closest('.navgroup');if(g)g.classList.remove('collapsed');}
       var tt=document.querySelector('.tab[data-tab="'+st.tab+'"]');
       if(tt&&!tt.classList.contains('active')){tt.click();await wsleep(380);}}
     if(st.open){var oe=document.querySelector(st.open);if(oe&&!oe.open)oe.open=true;}
     var el=st.sel?document.querySelector(st.sel):null;
     if(el&&st.row)el=el.closest('label.row')||el.closest('.row')||el;
     var r=null;
     if(el){try{el.scrollIntoView({block:'center',behavior:'auto'});}catch(e){}
       await raf2();
       r=el.getBoundingClientRect();
       if(!(r.width>0&&r.height>0))r=null;}
     if(!r){spot.style.display='none';arr.style.display='none';pop.classList.add('center');
       pop.style.left='';pop.style.top='';pop.style.transform='';
       if(anim)requestAnimationFrame(function(){pop.classList.remove('tfade');spot.classList.remove('tfade');});return;}
     var pad=8;
     spot.style.display='block';
     spot.style.left=(r.left-pad)+'px';spot.style.top=(r.top-pad)+'px';
     spot.style.width=(r.width+pad*2)+'px';spot.style.height=(r.height+pad*2)+'px';
     pop.classList.remove('center');pop.style.transform='';
     void pop.getBoundingClientRect();
     await new Promise(function(done){requestAnimationFrame(function(){
       var m=12,gap=16,W=window.innerWidth,H=window.innerHeight;
       var pw=pop.offsetWidth||364,ph=pop.offsetHeight||240;
       var x=0,y=0,side='';
       if(r.right+gap+pw<=W-m){side='right';x=r.right+gap;y=r.top;}
       else if(r.left-gap-pw>=m){side='left';x=r.left-gap-pw;y=r.top;}
       else if(r.bottom+gap+ph<=H-m){side='below';x=r.left+r.width/2-pw/2;y=r.bottom+gap;}
       else if(r.top-gap-ph>=m){side='above';x=r.left+r.width/2-pw/2;y=r.top-gap-ph;}
       if(!side){arr.style.display='none';pop.classList.add('center');pop.style.left='';pop.style.top='';done();return;}
       x=Math.max(m,Math.min(x,W-pw-m));y=Math.max(m,Math.min(y,H-ph-m));
       pop.style.left=x+'px';pop.style.top=y+'px';
       var cx=r.left+r.width/2-x,cy=r.top+r.height/2-y,a=arr.style;
       a.display='block';
       if(side==='right'){a.left='-7px';a.top=Math.max(14,Math.min(cy-6,ph-26))+'px';a.transform='rotate(-45deg)';}
       else if(side==='left'){a.left=(pw-7)+'px';a.top=Math.max(14,Math.min(cy-6,ph-26))+'px';a.transform='rotate(135deg)';}
       else if(side==='below'){a.top='-7px';a.left=Math.max(14,Math.min(cx-6,pw-26))+'px';a.transform='rotate(45deg)';}
       else{a.top=(ph-7)+'px';a.left=Math.max(14,Math.min(cx-6,pw-26))+'px';a.transform='rotate(225deg)';}
       done();
     });});
     if(anim)requestAnimationFrame(function(){pop.classList.remove('tfade');spot.classList.remove('tfade');});
   }
   async function show(){T('tour').style.display='block';running=true;
     // the auto-open opt-out only belongs to the MAIN tour; it renders the
     // STORED preference (checked = do not open automatically)
     var nr=T('tourNoAutoRow');if(nr)nr.style.display=(curName==='main')?'':'none';
     if(curName==='main'){try{var ta=tutApi();if(ta&&ta.tutorial_state)ta.tutorial_state().then(function(s){
       var na=T('tourNoAuto');if(na)na.checked=!!(s&&s.auto_open===false);}).catch(function(){});}catch(e){}}
     await place(true);}
   function end(reason){T('tour').style.display='none';running=false;mark(curName);
     if(curName==='main')tutMark(reason==='finish'?'COMPLETED':'DISMISSED');
     if(curName==='main'){var t=document.querySelector('.tab[data-tab="run"]');if(t)t.click();}}
   window.startTour=async function(name){await loadTutorials();curName=(name&&TOURS[name])?name:'main';
     TOUR=TOURS[curName]||[];if(!TOUR.length){return;}ti=0;mark(curName);
     if(curName==='main')tutMark('ACTIVE');
     closeMenu();await show();};
   var nb=T('tournext');if(nb)nb.onclick=async function(){if(ti>=TOUR.length-1){end('finish');return;}ti++;await place(true);};
   var bk=T('tourback');if(bk)bk.onclick=async function(){if(ti>0){ti--;await place(true);}};
   var sk=T('tourskip');if(sk)sk.onclick=function(){end('skip');};
   var tx=T('tourx');if(tx)tx.onclick=function(){end('skip');};
   var naBox=T('tourNoAuto');
   if(naBox)naBox.addEventListener('change',async function(){
     var want=!naBox.checked; // checked = do NOT open automatically
     var r=null;try{r=await tutApi().tutorial_set_auto_open(want);}catch(e){r={ok:false,error:String(e)};}
     if(!r||!r.ok){naBox.checked=!naBox.checked;
       if(window.toast)toast('Could not save this preference ['+((r&&r.error_code)||'PP-TUT-AUTO')+']');}});
   document.addEventListener('keydown',function(e){if(!running)return;
     if(e.key==='Escape'){end('skip');return;}
     var tg=((e.target&&e.target.tagName)||'').toLowerCase();
     if(tg==='input'||tg==='textarea'||tg==='select')return;
     if(e.key==='ArrowRight'){e.preventDefault();if(nb)nb.click();}
     else if(e.key==='ArrowLeft'){e.preventDefault();if(bk)bk.click();}});
   var tb0=T('tourbody');
   if(tb0)tb0.addEventListener('click',function(e){
     var t=e.target&&e.target.closest?e.target.closest('[data-tourlink],[data-jump]'):null;
     if(!t)return;
     var tl=t.getAttribute('data-tourlink');
     if(tl){window.startTour(tl);return;}
     var k=t.getAttribute('data-jump');
     if(k){var ct=document.querySelector('.tab[data-tab="cycle"]');
       if(ct&&!ct.classList.contains('active'))ct.click();
       setTimeout(function(){try{cygJump(k);}catch(e2){}},420);}});
   var menuEl=null;
   function menuAway(e){if(menuEl&&!menuEl.contains(e.target)){var b=T('tourbtn');if(b&&b.contains(e.target))return;closeMenu();}}
   function closeMenu(){if(menuEl){menuEl.remove();menuEl=null;document.removeEventListener('mousedown',menuAway,true);}}
   function openMenu(){if(menuEl){closeMenu();return;}
     var btn=T('tourbtn');if(!btn)return;var r=btn.getBoundingClientRect();
     menuEl=document.createElement('div');menuEl.id='tourmenu';
     menuEl.innerHTML='<div class="tmhd">Tutorials</div>'+TOUR_LIST.map(function(t){
       return '<button type="button" data-tour="'+t[0]+'">'+t[1]+(seen(t[0])?' <span class="tmdone">seen</span>':'')+'</button>';}).join('')
       +'<div class="tmhd" style="margin-top:4px">App</div>'
       +'<button type="button" id="tmfaq">FAQ &amp; troubleshooting</button>'
       +'<button type="button" id="tmwelcome">Welcome, privacy &amp; version</button>'
       +'<button type="button" id="tmtrust">Trust Center</button>'
       +'<button type="button" id="tmsetup">Re-run setup wizard</button>';
     document.body.appendChild(menuEl);
     menuEl.style.top=(r.bottom+8)+'px';
     menuEl.style.right=Math.max(10,window.innerWidth-r.right)+'px';
     menuEl.querySelectorAll('button[data-tour]').forEach(function(b){
       b.onclick=function(){window.startTour(b.getAttribute('data-tour'));};});
     var fqb=menuEl.querySelector('#tmfaq');
     if(fqb)fqb.onclick=function(){closeMenu();if(window.openFaq)window.openFaq();};
     var wb=menuEl.querySelector('#tmwelcome');
     if(wb)wb.onclick=function(){if(window.openWelcome)window.openWelcome();};
     var tcb=menuEl.querySelector('#tmtrust');
     if(tcb)tcb.onclick=function(){closeMenu();var t=document.querySelector('.tab[data-tab="trust"]');if(t)t.click();};
     var sb=menuEl.querySelector('#tmsetup');
     if(sb)sb.onclick=function(){closeMenu();if(window.__setupRerun)__setupRerun();};
     document.addEventListener('mousedown',menuAway,true);}
   var tb=T('tourbtn');if(tb)tb.onclick=openMenu;
   function buildExplain(){if(builtExplain)return;builtExplain=true;
     Object.keys(TAB_TOURS).forEach(function(tabid){
       var p=document.getElementById((PINNED_TABS[tabid]?'p':'p_')+tabid);if(!p)return;
       var h=p.querySelector('.phead h2');if(!h||h.querySelector('.explainbtn'))return;
       var b=document.createElement('button');b.type='button';b.className='explainbtn';
       b.textContent='✨ Explain this page';
       b.onclick=function(){window.startTour(TAB_TOURS[tabid]);};
       h.appendChild(b);});}
   document.querySelectorAll('.tab').forEach(function(t){
     t.addEventListener('click',function(){
       var name=TAB_TOURS[t.getAttribute('data-tab')];
       if(!name||running||seen(name))return;
       // a diagnostics/FAQ deep link owns this navigation: its tab click
       // must not auto-start a first-visit tour that then yanks the user
       // to the tour's own tab
       if(Date.now()-(window.__deepNavAt||0)<1600)return;
       var g=T('gate');if(g&&g.classList.contains('show'))return;
       if(setupOverlayVisible())return; // never over (or under) the setup wizard
       setTimeout(function(){if(!running&&!seen(name)&&!setupOverlayVisible())window.startTour(name);},430);});});
   window.addEventListener('resize',function(){var t=T('tour');if(t&&t.style.display!=='none')place();});
   // Auto-open the MAIN tutorial on every fresh main-app entry (process
   // boot, and each wizard visit -> return): since schema 3 the persisted
   // lifecycle is HISTORY (last outcome), never a suppressor. Never while
   // the welcome gate, the setup wizard (even suspended), the
   // Calibrate-tab quick wizard or the skip modal is up; at most once per
   // entry (TUT_ENTRY_SHOWN, reset by SETUP.open); the TUTORIAL_AUTO_OPEN
   // preference turns the whole behavior off. A legacy localStorage
   // pp_tour_done flag still migrates as COMPLETED history, but no
   // longer suppresses the open.
   var _tutChecking=false;
   let TUT_ENTRY_SHOWN=false;
   window.__tutEntryReset=function(){TUT_ENTRY_SHOWN=false;};
   function calWizardOpen(){var w=document.getElementById('wizard');
     return !!(w&&(w.style.display==='flex'||w.style.display==='block'));}
   function skipModalOpen(){var m=document.getElementById('skipmodal');
     return !!(m&&m.classList.contains('show'));}
   window.maybeStartTour=async function(){if(_tutChecking)return;_tutChecking=true;try{
     var g=document.getElementById('gate');if(g&&g.classList.contains('show'))return;
     if(setupOverlayVisible()||running)return;
     if(calWizardOpen()||skipModalOpen())return;
     var a=tutApi();if(!a)return;
     var st=null;try{st=await a.tutorial_state();}catch(e){st=null;}
     // re-check the live gates AFTER the await: a tour may have started
     // (or the wizard reopened) while the bridge call was in flight --
     // acting on stale gates here is how a duplicate check once recorded
     // a fabricated legacy migration mid-tour.
     if(running||setupOverlayVisible())return;
     var g2=document.getElementById('gate');if(g2&&g2.classList.contains('show'))return;
     if(calWizardOpen()||skipModalOpen())return;
     if(!st)return;
     var legacyDone=false;try{legacyDone=localStorage.getItem('pp_tour_done')==='1';}catch(e){legacyDone=false;}
     if(legacyDone){tutMark('COMPLETED',true);
       try{localStorage.removeItem('pp_tour_done');}catch(e){}}
     if(st.auto_open===false||TUT_ENTRY_SHOWN)return;
     TUT_ENTRY_SHOWN=true;
     window.startTour('main');
   }catch(e){}finally{_tutChecking=false;}};
   loadTutorials();
   window.addEventListener('pywebviewready',function(){if(!LOADED)loadTutorials(true);});
   setTimeout(function(){if(!LOADED)loadTutorials(true);},700);
   setTimeout(function(){if(!LOADED)loadTutorials(true);},1800);

   // ===== Lightbox (click any tutorial/help image to enlarge) =====
   document.addEventListener('click',function(e){
     var img=e.target;
     if(img&&img.tagName==='IMG'&&(img.classList.contains('tmimg')||img.classList.contains('phimg'))){
       var lb=T('lightbox');if(lb){T('lightboximg').src=img.src;lb.style.display='flex';}}
     else if(e.target&&e.target.id==='lightbox'){e.target.style.display='none';}
     else if(e.target&&e.target.id==='lightboximg'){T('lightbox').style.display='none';}
   });

   // ===== Owner editor =====
   var edWrap=null;
   window.openEditor=function(id,kind){
     if(!OWNER)return;
     var cur=null;
     if(kind==='card'){for(var n in TOURS){(TOURS[n]||[]).forEach(function(s){if(s.id===id)cur=s;});}}
     else{cur=HELPMAP[id]||{};}
     cur=cur||{};
     if(edWrap)edWrap.remove();
     edWrap=document.createElement('div');edWrap.id='tutedit';
     edWrap.innerHTML='<div class="tew"><h3>Edit content</h3><div class="teid">'+esc(id)+'</div>'+
       (kind==='card'?'<label>Title</label><input type="text" id="ed_title" value="'+esc(cur.title||'')+'">':'')+
       '<label>Text</label><textarea id="ed_body">'+esc(cur.body||'')+'</textarea>'+
       '<label>Image</label><div id="ed_imgwrap">'+(cur.img?'<img class="teimg" src="'+esc(cur.img)+'">':'<span class="tehint">none</span>')+'</div>'+
       '<div class="tebtns"><button class="btn2" id="ed_pick">Pick image</button><button class="btn2" id="ed_imgclr">Remove image</button></div>'+
       '<label>YouTube link</label><input type="text" id="ed_vid" value="'+esc(cur.vid||'')+'" placeholder="https://youtu.be/...">'+
       '<div class="tehint">You can use simple bold with the b tag. Leave a field blank to fall back to the default.</div>'+
       '<div class="tebtns"><button class="btn" id="ed_save">Save</button><button class="btn2" id="ed_reset">Reset to default</button><button class="btn2" id="ed_cancel">Cancel</button></div></div>';
     document.body.appendChild(edWrap);
     var pickedImg=cur.img||'';
     T('ed_pick').onclick=async function(){try{var r=await window.pywebview.api.pick_tutorial_image();
       if(r&&r.ok){pickedImg=r.img;T('ed_imgwrap').innerHTML='<img class="teimg" src="'+pickedImg+'">';}
       else if(r&&r.error)toast(r.error);}catch(e){}};
     T('ed_imgclr').onclick=function(){pickedImg='';T('ed_imgwrap').innerHTML='<span class="tehint">none</span>';};
     T('ed_cancel').onclick=function(){edWrap.remove();edWrap=null;};
     T('ed_save').onclick=async function(){
       var patch={body:(T('ed_body')?T('ed_body').value:''),img:pickedImg,vid:(T('ed_vid')?T('ed_vid').value:'')};
       if(kind==='card'&&T('ed_title'))patch.title=T('ed_title').value;
       try{var r=await window.pywebview.api.save_tutorial_entry(id,patch);
         if(r&&r.ok){applyContent(r.content);toast('Saved');edWrap.remove();edWrap=null;
           if(running)place();else showHelp(lastHelpKey,lastHelpTitle);}
         else toast((r&&r.error)||'Save failed');}catch(e){toast('Save failed');}};
     T('ed_reset').onclick=async function(){try{var r=await window.pywebview.api.reset_tutorial_entry(id);
       if(r&&r.ok){applyContent(r.content);toast('Reset to default');edWrap.remove();edWrap=null;
         if(running)place();else showHelp(lastHelpKey,lastHelpTitle);}}catch(e){}};
   };
   function applyContent(c){if(c&&c.tours){TOURS=c.tours;HELPMAP=c.help||{};OWNER=!!c.owner;window.__HELP=HELPMAP;}}

   // ===== Preview panel: hover anything, always shows something =====
   var body=document.body,prev=T('preview'),pbody=T('prevbody'),psub=T('prevsub');
   var lastKind='',lastArg='';
   function setPrev(on){if(on){body.classList.add('prev-on');body.classList.remove('coach-on');var ct=T('coachtoggle');if(ct)ct.classList.remove('on');var pt=T('prevtoggle');if(pt)pt.classList.add('on');}
     else{body.classList.remove('prev-on');var pt2=T('prevtoggle');if(pt2)pt2.classList.remove('on');}}
   var ptgl=T('prevtoggle');if(ptgl)ptgl.onclick=function(){setPrev(!body.classList.contains('prev-on'));};
   var pclose=T('prevclose');if(pclose)pclose.onclick=function(){setPrev(false);};
   window.__previewOn=function(){return body.classList.contains('prev-on');};
   window.__showPreviewPanel=setPrev;
   function tabLabel(tabid){var t=document.querySelector('.tab[data-tab="'+tabid+'"] span:nth-child(2)');return t?t.textContent:tabid;}
   function panelOf(tabid){return document.getElementById((PINNED_TABS[tabid]?'p':'p_')+tabid);}
   function chintOf(tabid){var p=panelOf(tabid);var c=p&&p.querySelector('.chint');return c?c.textContent.trim():'';}
   // A cloned panel would carry data-key/id/name and pollute collect()/getElementById.
   // Strip every identifying attribute and disable inputs before it enters the DOM.
   function sanitize(node){
     node.querySelectorAll('[data-key],[id],[name],[for],[data-pkey],[data-regionkey],[data-badge]').forEach(function(x){
       ['data-key','id','name','for','data-pkey','data-regionkey','data-badge','data-tab'].forEach(function(a){x.removeAttribute(a);});});
     node.querySelectorAll('input,select,textarea,button').forEach(function(x){x.disabled=true;x.tabIndex=-1;});
     node.querySelectorAll('.explainbtn,script').forEach(function(x){x.remove();});
     return node;
   }
   function snapshot(panel,label){
     var clone=sanitize(panel.cloneNode(true));clone.style.display='block';clone.classList.remove('active');
     return '<div class="prevsnap"><div class="snapin">'+clone.outerHTML+'</div><div class="snaptag">Preview</div></div>'+
       (label?'<div class="prevlbl">'+esc(label)+'</div>':'');
   }
   function showTab(tabid){
     var panel=panelOf(tabid);if(!panel){showGeneric(null);return;}
     if(psub)psub.textContent='page preview';
     pbody.innerHTML=snapshot(panel,tabLabel(tabid)+' - click the tab to open it');
   }
   function showGroup(gname,kids){
     if(psub)psub.textContent='section';
     var h='<div class="prevhelp"><h3>'+esc(gname)+'</h3><div class="ph-kind">Section, '+kids.length+' page'+(kids.length>1?'s':'')+'</div>'+
       '<div class="ph-body">Hover a page below to preview it. These are grouped so the sidebar stays short.</div></div>';
     kids.forEach(function(tabid){
       h+='<div class="prevsub-item"><div class="psi-name">'+esc(tabLabel(tabid))+'</div>'+
          '<div class="psi-desc">'+esc(chintOf(tabid)||'Open this page for its settings.')+'</div></div>';});
     pbody.innerHTML=h;
   }
   var KIND_LABEL={setting:'Setting',stage:'Cycle stage',cal:'Calibration',cyc:'Chart',build:'Saved build',
     stat:'Live stat',relic:'Relics',preset:'Quick preset',history:'Run history',control:'Control',page:'Page'};
   function mockFor(id){
     if(id==='hudbtn')return '<div class="prevmock mk-hud"><div class="mkh-led"></div><div class="mkh-stage">DIGGING</div>'+
       '<div class="mkh-row"><span>pans</span><b>34</b></div><div class="mkh-row"><span>pans/hr</span><b>612</b></div>'+
       '<div class="mkh-row"><span>clean</span><b>91%</b></div><div class="mkh-find">◆ Iridescent Painite 4.2kg</div><div class="snaptag">Preview</div></div>';
     if(id==='analyticsbtn')return '<div class="prevmock mk-an"><div class="mka-sec">Throughput</div>'+
       '<div class="mka-grid"><div class="mka-card"><i>Pans</i><b>128</b></div><div class="mka-card"><i>Pans/hr</i><b>612</b></div>'+
       '<div class="mka-card"><i>Cycle</i><b>5.9s</b></div><div class="mka-card"><i>Digs</i><b>141</b></div></div>'+
       '<div class="mka-sec">Earnings</div><div class="mka-bars"><i style="height:40%"></i><i style="height:65%"></i><i style="height:52%"></i><i style="height:88%"></i><i style="height:70%"></i></div><div class="snaptag">Preview</div></div>';
     if(id==='popout')return '<div class="prevmock mk-pill"><span class="mkp-dot"></span><b>Digging</b><span class="mkp-s">34 pans · 612/hr</span><div class="snaptag">Preview</div></div>';
     if(id==='coachtoggle')return '<div class="prevmock mk-coach"><div class="mkc-msg you">it lands in the water</div><div class="mkc-msg bot">Raise Walk forward before shaking so momentum carries you onto land. Want me to bump it?</div><div class="mkc-chip">Apply change</div><div class="snaptag">Preview</div></div>';
     return '';
   }
   function calMock(el){
     if(!el)return '';
     var pk=el.getAttribute('data-pkey'),rk=el.getAttribute('data-regionkey');
     if(pk){var cc=(T('cc_'+pk)||{}).value||'',cx=(T('cx_'+pk)||{}).value||'',cy=(T('cy_'+pk)||{}).value||'';
       var okc=/^#?[0-9a-fA-F]{3,6}$/.test(cc);
       var sw=okc?('<span class="cmw-sw" style="background:'+esc(cc.charAt(0)==='#'?cc:('#'+cc))+'"></span>'):'<span class="cmw-sw none"></span>';
       return '<div class="prevmock cmw">'+sw+'<div class="cmw-meta">'+
         '<div><s>colour</s> <b>'+(cc?esc(cc):'not set yet')+'</b></div>'+
         '<div><s>at</s> <b>'+((cx!==''&&cy!=='')?(esc(cx)+', '+esc(cy)):'not set yet')+'</b></div></div>'+
         '<div class="snaptag">Live</div></div>';}
     if(rk){var st=((T('rg_'+rk)||{}).textContent||'not set').replace(/\s+/g,' ').trim();
       var rp=regionPreviews[rk]||{};
       var shot=rp.preview?('<div class="cmw-shot"><img src="'+rp.preview+'" alt="captured region"></div>'):'';
       var hint=rp.preview?('Captured '+(rp.w||'?')+'×'+(rp.h||'?')+' px. This is the exact strip the macro reads for '+esc((rk||'').toLowerCase())+'.'):'Drag a box on the overlay to set this. What you captured shows here once set.';
       return '<div class="prevmock cmw"><div class="cmw-meta"><div><s>region</s> <b>'+esc(st)+'</b></div>'+
         '<div class="cmw-hint">'+hint+'</div></div>'+shot+'<div class="snaptag">'+(rp.preview?'Saved':'Live')+'</div></div>';}
     return '';
   }
   var PV_IMP={
     DIG_SPEED:{k:'trade',a:'faster digs',b:'fewer misses',d:2},
     CAP_EMPTY_FRAC:{k:'trade',a:'empties sooner',b:'no residue',d:2},
     X_STRAFE_MS:{k:'trade',a:'covers ground',b:'consistent',d:2},
     FINDS_MIN_CONF:{k:'trade',a:'catches more',b:'accurate',d:1},
     FINDS_BAND_MIN:{k:'trade',a:'lenient',b:'strict',d:1},
     PERFECT:{k:'toggle',g:1,c:3,gl:'tighter dig timing',cl:'green detect fails at high dig speed',seq:['dig starts','release on green','dig lands'],off:['timed hold instead','dig still lands']},
     DIG_PIPELINE:{k:'toggle',g:3,c:3,gl:'back-to-back digs',cl:'desyncs on lag or a mistimed dig',seq:['fill learned','digs fire on rhythm','bar checked at end'],off:['check after every dig','steadier, slower']},
     DIG_FILL_SMART:{k:'toggle',g:1,c:1,gl:'aims for zero downtime',cl:'often does not help',seq:['dig fired','wait while the bar moves','re-dig if short'],off:['fixed wait','can re-dig mid-fill']},
     SMART_TIMING:{k:'toggle',g:0,c:1,gl:'auto-dials timings',cl:'little payoff',seq:['misses climb','timings get nudged','kept if better'],off:['timings stay put','you tune by hand']},
     SHAKE_MOMENTUM_W:{k:'toggle',g:2,c:1,gl:'glides onto land',cl:'can overshoot some spots',seq:['shake starts','W held while draining','slides onto land'],off:['W not held','stays in the water']},
     SHARDS_ASSUME_FULL:{k:'toggle',g:2,c:2,gl:'faster, skips full read',cl:'may leave shards',seq:['bar moves','treated as full','walk back now'],off:['waits for the full read','slower']},
     GEODE_CONFIRM_FULL:{k:'toggle',g:1,c:1,gl:'avoids an early shake',cl:'slightly slower',seq:['digs done','wait for FULL read','walk back'],off:['trusts the dig count','moves immediately']},
     RECOVER_ENABLED:{k:'safety',p:3,pl:'getting stuck',seq:['stuck detected','nudges wiggle you free','loop resumes'],off:['no nudges','waits for safe stop']},
     BREAKOUT_ENABLED:{k:'safety',p:3,pl:'stuck loops',seq:['nudges failed','click burst + reposition','loop resumes'],off:['no break-out','waits for safe stop']},
     SHARDS_GREEN_CONFIRM:{k:'safety',p:3,pl:'false clicked reads',seq:['click sent','green bar confirms it','no double digs'],off:['bar-only proof','can double-click']},
     GEODE_GREEN_CONFIRM:{k:'safety',p:3,pl:'a missed tap costing the whole animation delay',seq:['tap sent','green bar confirms it','no green = re-tap, then nudge'],off:['waits the full animation','~12s before it nudges']},
     FR_RECOVERY:{k:'safety',p:3,pl:'soft-stop dead ends in Fortune River spots',seq:['soft stop','fast-travel back','resume panning'],off:['no warp','stays parked']},
     SR_RECOVERY:{k:'safety',p:3,pl:'soft-stop dead ends in Starfall River spots',seq:['soft stop','fast-travel back','resume panning'],off:['no warp','stays parked']},
     SHAKE_RETRY_ENABLED:{k:'safety',p:2,pl:'a single failed shake',seq:['shake missed','try it again','cycle saved'],off:['miss just counts','stops sooner']},
     SAFE_STOP_RETRY:{k:'safety',p:2,pl:'quitting on a hazard',seq:['cannot proceed','pause + retry','run survives'],off:['stops immediately','run over']},
     AUTOPAN_GUARD:{k:'safety',p:2,pl:'Auto-Pan turning itself off',seq:['button reads OFF','clicked back ON','tracking continues'],off:['stays off','run stalls']},
     LAND_CUE_ASSIST:{k:'safety',p:2,pl:'landing before the cue',seq:['about to probe','wait for the cue','probe on the dirt'],off:['probes blind','extra nudges']}};
   var PV_COLK={AUTOPAN_TOL:{k:'AUTOPAN_ON_RGB',d:[95,175,90],t:'the Auto Pan ON colour'},FR_TEXT_TOL:{k:'FR_TEXT_RGB',d:[232,120,200],t:'the Fortune River row pink'},SR_TEXT_TOL:{k:'SR_TEXT_RGB',d:[120,200,232],t:'the Starfall row colour'}};
   var PV_BRIGHT={FINDS_WHITE_MIN:['darker = ignored','brighter = card text'],FINDS_DARK_MAX:['this dark = card backing','brighter = ignored']};
   var PV_MODE={
     TREASURE_MODE:{ch:['dig deposit','strafe to sands','dig','strafe back'],act:[1,3],rm:'walk, shake, land',nt:'no shaking at all',tag:'Rubble Creek'},
     GEODE_MODE:{ch:['tap dig','wait out animation','normal shake'],act:[0,1],rm:'re-dig on stall',nt:'waits instead of nudging',tag:'slow fills'},
     TRACKER_MODE:{ch:['game auto-pans','macro watches','stats count'],act:[1],rm:'all macro input',nt:'sends nothing at all',tag:'benchmark'},
     X_PATTERN:{ch:['diagonal walk-back','shake','straight forward'],act:[0],rm:'straight walk-back',nt:'each pan covers new ground',tag:'anti-drift'}};
   var PV_DM={
     WEBHOOK_ENABLED:{m:'Macro started',w:'every event you enable below'},
     WEBHOOK_USER:{m:'Macro started',w:'DMs go to this username'},
     NOTIFY_START:{m:'Macro started',w:'when a session begins'},
     NOTIFY_STOP:{m:'Macro stopped: bag full (240 pans)',w:'manual, timer, or bag full'},
     NOTIFY_STATS:{m:'40 pans, 612/hr, 91% clean, 41m',w:'every N min (your stats interval)'},
     NOTIFY_SAFE_STOP:{m:'Safe-stopped: snagged on terrain. Retrying in 60s (1/3)',w:'when it pauses on trouble'},
     NOTIFY_RECOVERIES:{m:'Recovering: got stuck, correcting',w:'every recovery (can be chatty)'},
     NOTIFY_ERRORS:{m:'Macro error: engine stopped',w:'on an unexpected error'},
     NOTIFY_SCREENSHOT:{m:'Alert with a screenshot attached',w:'adds a picture to the alerts above'}};
   var PV_RDO={
     EARN_TRACK:{r:['money $1,240,000','shards 8,450'],f:'$/hr, $/pan, Analytics, History',nt:'Gains only; spending mid-run is ignored. macOS only.'},
     FINDS_TRACK:{r:['◆ Iridescent Painite 4.2kg','◆ Void 1.1kg'],f:'ticker, loot value, rarity stats',nt:'Every card, valued from your prices and sell boost. macOS only.'}};
   var PV_NOTE={FINDS_DEBUG:'Verbose finds diagnostics. Leave off for normal runs.',SELL_BOOST_PCT:'Loot value multiplied by your sell boost feeds the $/hr you see.',WINDOW_RELATIVE:'Shifts every pixel when the Roblox window moves, instead of fixed positions.'};
   var PV_SEQ={
     DEPOSIT_MAX_MS:['W','shake ended','hold up to','land cue found'],
     LAND_SETTLE_MS:['W','land cue shows','keep for','probe dig'],
     LAND_ASSIST_MAX_MS:['W','about to probe','hold until cue, cap','probe dig'],
     LAND_PROBE_NUDGE_MS:['W','probe missed','nudge for','probe again'],
     EASY_LAND_FWD_MS:['W','normal landing','extra for','dig'],
     RECOVER_BACK_MS:['taps','stuck detected','budget of','re-check'],
     BREAKOUT_SHAKE_MS:['click','break-out starts','burst for','reposition'],
     BREAKOUT_REPOS_MS:['W','shake cleared','hold for','loop resumes'],
     SHAKE_RETRY_DEEPER_MS:['S','shake would not start','deeper tap of','click again'],
     FR_WALK_MAX_MS:['W','after warp','walk up to','water reached'],
     FR_END_A_MS:['A','land cue','hold for','loop restarts'],
     FR_STRAFE_MS:['D','back at pan','tap for','lined up'],
     SR_A_MAX_MS:['A','after warp','strafe up to','Pan cue'],
     SR_S_MAX_MS:['S','centred','walk up to','Pan cue'],
     TREASURE_MOVE_MAX_MS:['D','dig done','strafe up to','Collect cue'],
     FINDS_CARD_SEC:['card','card appears','on screen for','fades out','s'],
     AUTOPAN_SETTLE_MS:['wait','button clicked','wait for','re-read colour'],
     AUTOPAN_RELOCK_DELAY_MS:['wait','cursor parked','wait for','Shift re-lock'],
     FR_SCAN_HOVER_MS:['wait','cursor step','dwell for','next step'],
     FR_DOUBLE_GAP_MS:['wait','first click','gap of','second click'],
     FR_OPEN_MS:['wait','double-tap 4','wait for','menu open'],
     FR_CLICK_SETTLE_MS:['wait','cursor moved','pause for','click'],
     FR_ACTION_GAP_MS:['wait','step done','pause for','next step'],
     FR_WARP_MS:['wait','row clicked','loading for','walk out'],
     GEODE_DELAY_MS:['wait','dig tap','animation up to','next tap'],
     GEODE_START_MS:['wait','click sent','wait up to','dig confirmed'],
     GEODE_DIG_MS:['tap','on the spot','held for','animation runs'],
     GEODE_SHAKE_HOLD_MS:['click','shake starts','up to','pan empty'],
     TREASURE_DIG_MS:['click','on the spot','held for','dig animation'],
     DIG_PLATEAU_MS:['wait','bar stops rising','still for','dig again'],
     DIG_SMART_CAP_MS:['wait','dig fired','wait up to','move on anyway'],
     DIG_PIPELINE_GAP_MS:['wait','dig fired','gap of','next dig'],
     EASY_WATER_RETURN_DELAY_MS:['wait','pan reads full','wait for','walk to water'],
     X_RECENTER_MS:['D/A','drifted sideways','after','strafe to centre'],
     PROBE_GAP_MS:['wait','nudge done','settle for','next probe']};
   var PV_CNT={
     MAX_DIGS_TO_FILL:['pan empty','each step = one dig','bar reads full'],
     GEODE_DIGS_TO_FILL:['pan empty','each step = one tap','bar reads full'],
     TREASURE_DIGS:['on a spot','each step = one dig','strafe on'],
     SHAKE_CLICKS:['shake starts','exactly this many clicks','stops, full or not'],
     SHARDS_DIG_CLICKS:['on land','exact dig clicks','walk back'],
     SHARDS_CLICK_RETRIES:['bar never moved','one more click each','nudge forward'],
     GEODE_START_TRIES:['no green bar','one more tap each','nudge forward'],
     SHAKE_START_RETRIES:['no drain yet','deeper tap + click','normal bail'],
     LAND_DIG_TRIES:['probe missed','nudge + probe','safe stop'],
     FR_FIND_TRIES:['row not found','one full sweep each','re-open device'],
     FR_OPEN_TRIES:['menu missing','re-equip + open','give up'],
     SAFE_STOP_MAX_RETRIES:['retry failed','one more retry','hard stop']};
   var PV_TRG={
     STUCK_TICKS:['same screen read','recovery starts'],
     RECOVER_LIMIT:['one recovery each','break-out'],
     SHAKE_FAIL_LIMIT:['failed shake','safe stop'],
     SHAKE_GLITCH_LIMIT:['failed shake','quick click-to-empty'],
     BREAKOUT_LIMIT:['break-out attempt','safe stop'],
     NO_PROGRESS_SEC:['nothing completes','click-to-empty'],
     SHAKE_BAIL_MS:['pan still full','give up + retry'],
     SHARDS_CLICK_CONFIRM_MS:['bar still empty','retry the click'],
     FINDS_MIN_DWELL:['sample with card','it counts'],
     FINDS_EMPTY_MS:['quiet, no cards','stack resets'],
     FR_CROSS_CONFIRM:['water read','committed'],
     AUTOSTOP_MINUTES:['minute running','macro stops'],
     RELIC_LAND_MAX_S:['waiting for land','place anyway'],
     AUTOPAN_STALL_SEC:['bar idle','toggle Auto Pan'],
     SHAKE_START_CONFIRM_MS:['still full','deeper tap'],
     SHAKE_STALL_MS:['drain frozen','end the attempt'],
     STOP_AFTER_PANS:['pan emptied','macro stops']};
   var PV_READ={
     TRACKER_POLL_MS:{t:'reads the capacity bar',s:'fill and drain, watch-only'},
     EARN_OCR_SEC:{t:'reads money and shards',s:'the HUD totals'},
     FINDS_FAST_MS:{t:'reads the find pop-up area',s:'pixel sample, counts cards'},
     FINDS_OCR_MS:{t:'reads find card text',s:'name, weight, rarity'},
     AUTOPAN_GUARD_SEC:{t:'reads the Auto Pan button',s:'colour check'}};
   var PV_TRAIN={SHAKE_CLICK_MS:{on:'SELF',off:'SHAKE_CLICK_GAP_MS',a:'click held',b:'gap',hd:'The shake rattle'},SHAKE_CLICK_GAP_MS:{on:'SHAKE_CLICK_MS',off:'SELF',a:'click held',b:'gap',hd:'The shake rattle'},BURST_ON_MS:{on:'SELF',off:'BURST_OFF_MS',a:'tap held',b:'released',hd:'Recovery jitter taps'},BURST_OFF_MS:{on:'BURST_ON_MS',off:'SELF',a:'tap held',b:'released',hd:'Recovery jitter taps'},TREASURE_DIG_GAP_MS:{on:'TREASURE_DIG_MS',off:'SELF',a:'dig click',b:'animation wait',hd:'Treasure dig rhythm'}};
   var PV_ZERO={SHAKE_CLICKS:'keeps clicking until the bar reads empty',GEODE_DIGS_TO_FILL:'keeps digging until the bar reads full',SHARDS_DIG_CLICKS:'off: the normal dig logic runs instead',STOP_AFTER_PANS:'off: no pan limit',AUTOPAN_STALL_SEC:'off: no idle kick',SHAKE_START_CONFIRM_MS:'off: no fast start check',SHAKE_STALL_MS:'off: no drain fail-fast',X_RECENTER_MS:'never recenters',DIG_PIPELINE_GAP_MS:'automatic: derived from your dig speed'};
   var PV_ZBEST={SHAKE_CLICKS:'slow-shake gear, most builds',GEODE_DIGS_TO_FILL:'unknown dig counts',SHARDS_DIG_CLICKS:'non-shard builds',STOP_AFTER_PANS:'endless runs',DIG_PIPELINE_GAP_MS:'most pipeline users',X_RECENTER_MS:'wide open strips'};
   function _pvSum(S){var t=0;S.forEach(function(s){t+=(s.hi||0);});return t;}
   function _pvAssign(V,k,val){var o={};for(var q in V)o[q]=V[q];o[k]=val;return o;}
   function _pvCol(st){var c={dig:'#c08a4e',swalk:'#5aa0bd',glide:'#8cc06a',shake:'#e0b357',land:'#c69a6e',safety:'#c76d6d',other:'#9c9183'};return c[st]||'#9c9183';}
   function _pvMag(n){return [6,32,64,94][Math.max(0,Math.min(3,n|0))];}
   function _pvHex(rgb){try{if(rgb&&rgb.length===3){if((rgb[0]|0)+(rgb[1]|0)+(rgb[2]|0)===0)return '#6fae5a';return '#'+rgb.map(function(c){return ('0'+Math.max(0,Math.min(255,c|0)).toString(16)).slice(-2);}).join('');}if(typeof rgb==='string'&&rgb.charAt(0)==='#')return rgb;}catch(e){}return '#6fae5a';}
   function _pvNbr(rgb){var r=95,g=175,b=90;try{if(rgb&&rgb.length===3&&((rgb[0]|0)+(rgb[1]|0)+(rgb[2]|0)>0)){r=rgb[0]|0;g=rgb[1]|0;b=rgb[2]|0;}}catch(e){}function h(x,y,z){function c(n){return ('0'+Math.max(0,Math.min(255,n|0)).toString(16)).slice(-2);}return '#'+c(x)+c(y)+c(z);}return 'conic-gradient(from 0deg,'+h(r+42,g+42,b+42)+','+h(r,g,b)+','+h(r-48,g-48,b-48)+','+h(r-22,g+8,b+38)+','+h(r,g,b)+','+h(r+38,g+8,b-22)+','+h(r+42,g+42,b+42)+')';}
   function _pvPph(ms){if(!(ms>0))return '';var p=3600000/ms;return (p>=1000?((p/1000).toFixed(1)+'k'):Math.round(p))+' pans/hr';}
   function _pvChip(t,cls){return '<div class="pvc-ch'+(cls?' '+cls:'')+'">'+t+'</div>';}
   function _pvChips(items){return '<div class="pvc-chips">'+items.join('<span class="pvc-arr">▸</span>')+'</div>';}
   function _pvFrac(v,mn,mx,fb){return (isFinite(mn)&&isFinite(mx)&&mx>mn)?Math.max(0,Math.min(1,(v-mn)/(mx-mn))):fb;}
   function _pvRange(el){var rng=el&&(el.querySelector('input.crng')||el.querySelector('input[type=range]'));var inp=el&&el.querySelector('[data-key]');return {v:inp?parseFloat(inp.value):NaN,mn:rng?parseFloat(rng.min):NaN,mx:rng?parseFloat(rng.max):NaN,st:rng?parseFloat(rng.step):NaN,inp:inp};}
   function _pvZeroCard(K){var main=PV_ZERO[K]||'off';var best=PV_ZBEST[K];return '<div class="pvchart"><div class="pvc-hd">set to <b>0</b> · '+(main.indexOf('off')===0?'off':'automatic')+'</div>'+_pvChips([_pvChip(esc(main),'on')])+(best?_pvChips([_pvChip('best for'),_pvChip(esc(best))]):'')+'</div>';}
   function _pvRow(S,chg,scale,val,cur){var tot=_pvSum(S);var occ={};var bars=S.map(function(s){var k=s.stage+'|'+s.name;occ[k]=(occ[k]||0)+1;var on=!!chg[k+'#'+occ[k]];var w=(s.hi||0)/scale*100;return '<div class="pvc-seg'+(on?' on':'')+'" style="flex:'+Math.max(0.4,w).toFixed(2)+';background:'+_pvCol(s.stage)+(on?'':';opacity:.34')+'" title="'+esc(s.name||'')+'"></div>';}).join('');var wpct=Math.max(3,tot/scale*100);var me=(val===Math.round(cur));return '<div class="pvc-row"><div class="pvc-lbl'+(me?' me':'')+'" title="'+(me?'your current value':'')+'">'+val+'ms</div><div class="pvc-track"><div class="pvc-bar" style="width:'+wpct.toFixed(1)+'%">'+bars+'</div></div><div class="pvc-rt2"><b>'+Math.round(tot)+'ms</b> per pan<br>≈'+_pvPph(tot)+'</div></div>';}
   function _pvPan(K,el){try{var R=_pvRange(el);if(!R.inp)return '';var v=isFinite(R.v)?R.v:0,mn=R.mn,mx=R.mx;var st=isFinite(R.st)&&R.st>0?R.st:1;var d=NaN;try{if(typeof DEF!=='undefined'&&DEF&&isFinite(parseFloat(DEF[K])))d=parseFloat(DEF[K]);}catch(e0){}var lo=isFinite(d)?Math.min(v,d):v,hi=isFinite(d)?Math.max(v,d):v;if(hi-lo<Math.max(st,4,hi*0.25)){var span=Math.max(st*4,Math.round(Math.max(hi,40)*0.9),20);hi=lo+span;}if(isFinite(mx)&&hi>mx)hi=mx;if(isFinite(mn)&&lo<mn)lo=mn;if(hi<=lo)hi=lo+Math.max(st,10);lo=Math.round(lo);hi=Math.round(hi);var V=collect();var Slo=(cycModel(_pvAssign(V,K,lo),window._AB||{}).segs)||[];var Shi=(cycModel(_pvAssign(V,K,hi),window._AB||{}).segs)||[];var occ={},durLo={},i,k,id;for(i=0;i<Slo.length;i++){k=Slo[i].stage+'|'+Slo[i].name;occ[k]=(occ[k]||0)+1;durLo[k+'#'+occ[k]]=Slo[i].hi||0;}var occ2={},chg={},any=false,stg=null,seen={};for(i=0;i<Shi.length;i++){k=Shi[i].stage+'|'+Shi[i].name;occ2[k]=(occ2[k]||0)+1;id=k+'#'+occ2[k];seen[id]=1;var a=durLo.hasOwnProperty(id)?durLo[id]:null,b=Shi[i].hi||0;if(a===null||Math.abs(b-a)>=0.75){chg[id]=1;any=true;if(!stg)stg=Shi[i].stage;}}for(id in durLo){if(!seen[id]){chg[id]=1;any=true;if(!stg)stg=id.split('|')[0];}}if(!any)return '';var tLo=_pvSum(Slo),tHi=_pvSum(Shi);var scale=Math.max(tLo,tHi)||1;var SN={dig:'Dig',swalk:'Walk back',glide:'Glide and start',shake:'Shake and drain',land:'Land and prove',safety:'Safety nets',other:'Other'};var nm=SN[stg]||stg;var vr=Math.round(v);var dPph=Math.abs(3600000/Math.max(1,tLo)-3600000/Math.max(1,tHi));var cost=dPph>=10?(' The slower row gives up about <b>'+(dPph>=1000?((dPph/1000).toFixed(1)+'k'):Math.round(dPph))+' pans/hr</b>.'):'';var extra=(vr===lo||vr===hi)?'':' You are at '+vr+'ms.';return '<div class="pvchart"><div class="pvc-hd">Effect on one pan<span class="pvc-tag" style="background:'+_pvCol(stg)+'">'+nm+'</span></div>'+_pvRow(Slo,chg,scale,lo,v)+_pvRow(Shi,chg,scale,hi,v)+'<div class="pvc-note">Same time scale, at '+lo+' vs '+hi+'ms.'+extra+' The glowing block is what changes.'+cost+'</div></div>';}catch(e){return '';}}
   function _pvTrade(im,K,el){var R=_pvRange(el||null);var f=_pvFrac(R.v,R.mn,R.mx,0.5);var w1=Math.round(6+88*f),w2=100-w1;var lean=f<0.33?('leaning '+im.b):(f>0.66?('leaning hard into '+im.a):'balanced');var vt=isFinite(R.v)?String(R.v):'';return '<div class="pvchart"><div class="pvc-hd">at <b>'+esc(vt)+'</b> · '+esc(lean)+'</div><div class="pvc-row"><span class="pvc-pl" style="color:#8cc06a">'+esc(im.a)+'</span><span class="pvc-ir"><b style="width:'+w1+'%;background:#8cc06a"></b></span></div><div class="pvc-row"><span class="pvc-pl" style="color:#d88a6a">'+esc(im.b)+'</span><span class="pvc-ir"><b style="width:'+w2+'%;background:#c98a5a"></b></span></div><div class="pvc-note">Your value sits '+Math.round(f*100)+'% toward <b>'+esc(im.a)+'</b>. The further you push, the sharper the trade.</div></div>';}
   function _pvToggle(im,el){var inp=el&&el.querySelector('[data-key]');var on=!!(inp&&inp.checked);var g=Math.max(0,Math.min(3,im.g|0)),c=Math.max(0,Math.min(3,im.c|0));var chips=on?_pvChips([_pvChip(esc(im.seq[0])),_pvChip('<span class="pvc-cap">on</span>'+esc(im.seq[1]),'on'),_pvChip(esc(im.seq[2]))]):_pvChips([_pvChip(esc(im.seq[0])),_pvChip(esc(im.off[0]),'dim'),_pvChip(esc(im.off[1]),'bad')]);var gh=(on&&g>0)?'<span class="pvc-ir"><b style="width:'+_pvMag(g)+'%;background:#8cc06a"></b></span><span class="pvc-rt2">'+esc(im.gl)+'</span>':'<span class="pvc-ir"><b style="width:6%;background:#4a4438"></b></span><span class="pvc-rt2">'+(on?'no real gain':'none while off')+'</span>';var cw=on?_pvMag(c):74;return '<div class="pvchart"><div class="pvc-hd">currently <b'+(on?'':' style="color:#c76d6d"')+'>'+(on?'on':'off')+'</b></div>'+chips+'<div class="pvc-row"><span class="pvc-pl" style="color:#8cc06a">gain</span>'+gh+'</div><div class="pvc-row"><span class="pvc-pl" style="color:#d88a6a">cost</span><span class="pvc-ir"><b style="width:'+cw+'%;background:'+(on?'#c76d6d':'#c76d6d')+'"></b></span><span class="pvc-rt2">'+esc(on?im.cl:im.off[1])+'</span></div></div>';}
   function _pvSafety(im,el){var inp=el&&el.querySelector('[data-key]');var on=!!(inp&&inp.checked);var p=Math.max(1,Math.min(3,im.p||1));var chips=on?_pvChips([_pvChip(esc(im.seq[0])),_pvChip(esc(im.seq[1]),'on'),_pvChip(esc(im.seq[2]))]):_pvChips([_pvChip(esc(im.seq[0])),_pvChip(esc(im.off[0]),'dim'),_pvChip(esc(im.off[1]),'bad')]);var bar=on?'<div class="pvc-row"><span class="pvc-pl" style="color:#8cc06a">protects</span><span class="pvc-ir"><b style="width:'+_pvMag(p)+'%;background:#6fa85a"></b></span><span class="pvc-rt2">against '+esc(im.pl||'failures')+'</span></div>':'<div class="pvc-row"><span class="pvc-pl" style="color:#d88a6a">exposed</span><span class="pvc-ir"><b style="width:12%;background:#4a4438"></b></span><span class="pvc-rt2">'+esc(im.off[1])+'</span></div>';return '<div class="pvchart"><div class="pvc-hd">currently <b'+(on?'':' style="color:#c76d6d"')+'>'+(on?'on':'off')+'</b></div>'+chips+bar+'</div>';}
   function _pvCount(K,v){v=Math.round(v);if(!(v>0))return _pvZeroCard(K);var m=PV_CNT[K];var n=Math.min(v,20);var chips=m?_pvChips([_pvChip(esc(m[0])),_pvChip(esc(m[1]),'on'),_pvChip(esc(m[2]))]):'';return '<div class="pvchart"><div class="pvc-hd"><b>'+v+'</b>, one after another</div>'+chips+'<div class="pvc-row"><span class="pvc-pl">'+v+'×</span><span class="pvc-ir" style="height:15px"><b data-pvsteps="'+n+'" data-pvw="100" style="width:100%;background:#e0b357"></b></span></div></div>';}
   function _pvTrig(K,v,mn,mx,u){u=u||'';v=Math.round(v);if(!(v>0))return _pvZeroCard(K);var f=_pvFrac(v,mn,mx,0.55);var lp=Math.round(15+77*f);var m=PV_TRG[K];var chips=m?_pvChips([_pvChip(esc(m[0])),_pvChip('<b>'+v+u+'</b>','on'),_pvChip(esc(m[1]),'bad')]):'';var inner;if(v<=12){inner='<b class="pvc-fills" data-pvsteps="'+v+'" data-pvw="'+lp+'" style="width:'+lp+'%"></b>';}else{inner='<b class="pvc-fillv" style="width:'+lp+'%;--pvw:'+lp+'%"></b>';}return '<div class="pvchart"><div class="pvc-hd">fires at <b>'+v+u+'</b></div>'+chips+'<div class="pvc-meter">'+inner+'<i class="pvc-line" style="left:'+lp+'%"></i></div><div class="pvc-note">A higher setting pushes the line right: more patience, slower to react.</div></div>';}
   function _pvTimer(K,v,u,mn,mx){var tr=PV_TRAIN[K];if(!tr)return _pvRead(K,v,u,mn,mx);var V=collect();var kOn=tr.on==='SELF'?K:tr.on,kOff=tr.off==='SELF'?K:tr.off;var vOn=parseFloat(tr.on==='SELF'?v:V[tr.on]);var vOff=parseFloat(tr.off==='SELF'?v:V[tr.off]);if(!isFinite(vOn)||vOn<0)vOn=1;if(!isFinite(vOff)||vOff<0)vOff=1;if(vOn+vOff<=0){vOn=1;vOff=1;}var dOn=1,dOff=1;try{if(typeof DEF!=='undefined'&&DEF){dOn=Math.max(1,parseFloat(DEF[kOn])||1);dOff=Math.max(1,parseFloat(DEF[kOff])||1);}}catch(e0){}var win=Math.max(4*(dOn+dOff),vOn+vOff,vOn/0.46,vOff/0.46);var twp=Math.max(1,Math.min(46,vOn/win*100)),gwp=Math.max(0.8,Math.min(46,vOff/win*100));var x=0.8,c2=0,ticks='';while(x+twp<=99&&c2<12){ticks+='<i class="pvc-tk" style="left:'+x.toFixed(2)+'%;width:'+twp.toFixed(2)+'%"></i>';x+=twp+gwp;c2++;}if(!c2)ticks='<i class="pvc-tk" style="left:0.8%;width:'+twp.toFixed(2)+'%"></i>';var rate=Math.round(1000/(vOn+vOff));var chips=_pvChips([_pvChip('<span class="pvc-cap">'+esc(tr.a.split(' ')[0])+'</span>'+esc(tr.a)+' '+Math.round(vOn)+'ms','on'),_pvChip(esc(tr.b)+' '+Math.round(vOff)+'ms'),_pvChip('repeats')]);return '<div class="pvchart"><div class="pvc-hd">'+esc(tr.hd)+' · <b>≈'+rate+' a second</b></div>'+chips+'<div class="pvc-tmr">'+ticks+'<i class="pvc-ph"></i></div><div class="pvc-note">Widths are to scale, so only the side you change moves.</div></div>';}
   function _pvRead(K,v,u,mn,mx){
     if(K==='WEBHOOK_STATS_MIN'){if(!(v>0))return _pvZeroCard(K);var ph=Math.round(60/v*10)/10;return '<div class="pvchart"><div class="pvc-hd">every <b>'+v+'min</b></div><div class="pvc-dm"><i class="pvc-dma"></i><div><div class="pvc-dmt">Prospector Lite</div><div class="pvc-dmb">40 pans, 612/hr, 91% clean, 41m</div></div></div>'+_pvChips([_pvChip('<b>'+(ph===Math.round(ph)?Math.round(ph):ph)+'</b> DM'+(ph===1?'':'s')+' per hour','on')])+'<div class="pvc-note">A pulse for your phone; spot a degraded run without the computer.</div></div>';}
     if(K==='SAFE_STOP_RETRY_SEC'){var mr='';try{var V2=collect();if(isFinite(parseFloat(V2.SAFE_STOP_MAX_RETRIES)))mr=String(Math.round(parseFloat(V2.SAFE_STOP_MAX_RETRIES)));}catch(e2){}return '<div class="pvchart"><div class="pvc-hd">retry cadence · <b>'+Math.round(v)+'s</b></div><div class="pvc-cad"><div class="pvc-cadw">wait '+Math.round(v)+'s</div><div class="pvc-cadt">try</div><div class="pvc-cadw">wait '+Math.round(v)+'s</div><div class="pvc-cadt">try</div></div><div class="pvc-note">Up to <b>'+(mr||'3')+' retries</b> (your hard-stop limit), then it stops for real.</div></div>';}
     var m=PV_READ[K];if(!m)return '';
     var rate,rl;
     if(u==='ms'){var rs=1000/Math.max(1,v);if(rs>=1){rate='≈'+(rs>=10?Math.round(rs):Math.round(rs*10)/10);rl='reads a second';}else{rate='≈'+(Math.round(rs*60*10)/10);rl='reads a minute';}}
     else if(u==='s'){var rm=60/Math.max(1,v);rate='≈'+(rm>=10?Math.round(rm):Math.round(rm*10)/10);rl='reads a minute';}
     else{rate='≈'+(Math.round(60/Math.max(1,v)*10)/10);rl='an hour';}
     var f=_pvFrac(v,mn,mx,0.5);var p=Math.max(2,Math.round(6-4*f));var W=100/p,tw=Math.max(1.6,Math.min(W-2,W*(1/(2+9*f)))),ticks='';
     for(var i=0;i<p;i++){ticks+='<i class="pvc-tk" style="left:'+(i*W+0.8).toFixed(2)+'%;width:'+tw.toFixed(2)+'%"></i>';}
     return '<div class="pvchart"><div class="pvc-hd">every <b>'+v+u+'</b></div><div class="pvc-chips"><div class="pvc-ch on">'+esc(m.t)+'<span class="pvc-sub">'+esc(m.s)+'</span></div><div class="pvc-big">'+esc(String(rate))+'<small>'+esc(rl)+'</small></div></div><div class="pvc-tmr">'+ticks+'<i class="pvc-ph"></i></div><div class="pvc-note">Lower = reacts faster, a little more CPU. Higher = lighter, slower to notice.</div></div>';}
   function _pvSeq(K,v,mn,mx){if(PV_ZERO[K]&&!(v>0))return _pvZeroCard(K);var m=PV_SEQ[K]||['wait','before','for','next step'];var u=m[4]||'ms';var f=_pvFrac(v,mn,mx,0.5);var w=Math.round(8+88*f);var mid='<div class="pvc-ch on pvc-act"><span><span class="pvc-cap">'+esc(m[0])+'</span>'+esc(m[2])+' '+Math.round(v)+u+'</span><span class="pvc-hb"><i style="--w:'+w+'%;width:'+w+'%"></i></span></div>';return '<div class="pvchart"><div class="pvc-hd"><b>'+Math.round(v)+u+'</b> '+esc(m[2])+' · '+esc(m[3])+'</div><div class="pvc-chips">'+_pvChip(esc(m[1]))+'<span class="pvc-arr">▸</span>'+mid+'<span class="pvc-arr">▸</span>'+_pvChip(esc(m[3]))+'</div><div class="pvc-note">The bar is your value against its full range.</div></div>';}
   function _pvColour(K,el){var o=PV_COLK[K]||{};var fr=window.__pvfr||{};var rgb=(fr[o.k]&&(fr[o.k][0]|0)+(fr[o.k][1]|0)+(fr[o.k][2]|0)>0)?fr[o.k]:(o.d||[95,175,90]);var hex=_pvHex(rgb);var R=_pvRange(el);var v=isFinite(R.v)?R.v:0;var mx=isFinite(R.mx)?R.mx:60;var pct=Math.max(0,Math.min(1,v/(mx||60)));var sc=(0.32+0.66*pct).toFixed(2);var cg=_pvNbr(rgb);return '<div class="pvchart"><div class="pvc-hd">accepts <b>±'+Math.round(v)+'</b> per channel</div>'+_pvChips([_pvChip('matches '+esc(o.t||'the target colour'),'on'),_pvChip(esc(hex),'dim')])+'<div class="pvc-wheel"><i class="pvc-disc" style="background:'+cg+'"></i><i class="pvc-rev" style="background:'+cg+';--sc:'+sc+'"></i><i class="pvc-core" style="background:'+hex+'"></i><i class="pvc-ring" style="--sc:'+sc+'"></i></div><div class="pvc-note">Only shades inside the ring count. Tight = precise but lighting can break it; wide = tolerant but off-states can sneak in.</div></div>';}
   function _pvBright(K,v,mn,mx){mn=isFinite(mn)?mn:0;mx=isFinite(mx)?mx:255;var pct=Math.max(0,Math.min(100,(v-mn)/(mx-mn)*100));var m=PV_BRIGHT[K]||['darker = ignored','brighter = counts'];return '<div class="pvchart"><div class="pvc-hd">cutoff at <b>'+Math.round(v)+'</b></div><div class="pvc-grad"><i class="pvc-gl" style="left:'+pct.toFixed(1)+'%"></i></div><div class="pvc-chips">'+_pvChip(esc(m[0]))+'<div class="pvc-ch on" style="margin-left:auto">'+esc(m[1])+'</div></div></div>';}
   function _pvModeV(K,el){var m=PV_MODE[K];if(!m)return '';var chain='';for(var i=0;i<m.ch.length;i++){chain+='<span class="pvc-mnode'+(m.act.indexOf(i)>=0?' act':'')+'">'+esc(m.ch[i])+'</span>'+(i<m.ch.length-1?'<span class="pvc-marr">then</span>':'');}return '<div class="pvchart"><div class="pvc-hd">rewires the loop · <b>'+esc(m.tag)+'</b></div><div class="pvc-mode">'+chain+'</div><div class="pvc-chips"><div class="pvc-ch pvc-strike">'+esc(m.rm)+'</div><div class="pvc-ch on">'+esc(m.nt)+'</div></div></div>';}
   function _pvDM(K){var m=PV_DM[K]||{m:'Macro started',w:''};return '<div class="pvchart"><div class="pvc-hd">your webhook</div><div class="pvc-dm"><i class="pvc-dma"></i><div><div class="pvc-dmt">Prospector Lite</div><div class="pvc-dmb">'+esc(m.m)+'</div></div></div>'+(m.w?_pvChips([_pvChip('fires'),_pvChip(esc(m.w),'on')]):'')+'</div>';}
   function _pvRdo(K){var m=PV_RDO[K];if(!m)return '';return '<div class="pvchart"><div class="pvc-hd">what it reads</div>'+_pvChips([_pvChip(esc(m.r[0]),'on'),_pvChip(esc(m.r[1]),'on')])+_pvChips([_pvChip('feeds'),_pvChip(esc(m.f))])+'<div class="pvc-note">'+esc(m.nt)+'</div></div>';}
   function _pvRarity(el){var order=['common','uncommon','rare','epic','legendary','mythic','exotic'];var cols={common:'#7c8a6e',uncommon:'#6ea86a',rare:'#5aa0bd',epic:'#a97fc4',legendary:'#e0b357',mythic:'#d86a6a',exotic:'#e08adf'};var inp=el&&el.querySelector('[data-key]');var v=inp?String(inp.value||'').toLowerCase():'';var idx=order.indexOf(v);if(idx<0)idx=6;var pills=order.map(function(r,i){return '<span class="pvc-rp'+(i>=idx?' on':'')+'" style="background:'+cols[r]+(i<idx?';opacity:.32':'')+'">'+r+'</span>';}).join('');return '<div class="pvchart"><div class="pvc-hd">valued from <b>'+esc(order[idx])+'</b> up</div><div class="pvc-rar">'+pills+'</div><div class="pvc-note">Below the cutoff auto-sells into the money counter; valuing it here would double-count.</div></div>';}
   function _pvNote(t){return '<div class="pvchart"><div class="pvc-callout">'+esc(t)+'</div></div>';}
   function pvAnimate(root){try{
     if(document.body.classList.contains('reduce-motion'))return;
     if(!root||!root.querySelectorAll)return;
     root.querySelectorAll('[data-pvsteps]').forEach(function(el){
       if(!el.animate)return;
       var n=Math.max(1,parseInt(el.getAttribute('data-pvsteps'),10)||1);
       var w=parseFloat(el.getAttribute('data-pvw'));if(!isFinite(w)||w<=0)w=100;
       var slide=0.3,hold=0.22,tail=0.9,D=n*(slide+hold)+tail;
       var kf=[{width:'0%',offset:0}];
       for(var i=1;i<=n;i++){
         var t1=Math.min(1,((i-1)*(slide+hold)+slide)/D);
         var t2=Math.min(1,(i*(slide+hold))/D);
         var ww=(w*i/n).toFixed(2)+'%';
         kf.push({width:ww,offset:t1});kf.push({width:ww,offset:t2});}
       kf.push({width:w.toFixed(2)+'%',offset:1});
       try{el.animate(kf,{duration:Math.round(D*1000),iterations:Infinity,easing:'linear'});}catch(e2){}
     });
   }catch(e){}}
   var _pvLblMap=null,_pvCtlMap=null;
   function _pvVars(t){var out=[t];var np=t.replace(/\s*\([^)]*\)\s*/g,' ').replace(/\s+/g,' ').trim();if(np)out.push(np);var c0=t.split(',')[0].trim();if(c0)out.push(c0);var s0=t.split('/')[0].trim();if(s0)out.push(s0);var c0np=c0.replace(/\s*\([^)]*\)\s*/g,' ').replace(/\s+/g,' ').trim();if(c0np)out.push(c0np);var s0np=s0.replace(/\s*\([^)]*\)\s*/g,' ').replace(/\s+/g,' ').trim();if(s0np)out.push(s0np);return out;}
   function _pvLabels(){if(_pvLblMap)return _pvLblMap;_pvLblMap={};try{document.querySelectorAll('[data-key]').forEach(function(inp){var row=inp.closest('label.row,label.crow,.row,.crow');if(!row)return;var l=row.querySelector('.lbl');if(!l)return;var t=l.textContent.replace('?','').replace(/\s+/g,' ').trim().toLowerCase();if(!t)return;var k=inp.getAttribute('data-key');_pvVars(t).forEach(function(vv){if(vv&&!_pvLblMap[vv])_pvLblMap[vv]=k;});});}catch(e){}return _pvLblMap;}
   function _pvCtls(){if(_pvCtlMap)return _pvCtlMap;_pvCtlMap={};try{
     document.querySelectorAll('.calrow').forEach(function(cr){var nm=cr.querySelector('.calname');if(!nm)return;var t=nm.textContent.replace(/\s+/g,' ').trim().toLowerCase();if(!t)return;var sel='';if(cr.getAttribute('data-pkey'))sel='.calrow[data-pkey=\''+cr.getAttribute('data-pkey')+'\']';else if(cr.getAttribute('data-regionkey'))sel='.calrow[data-regionkey=\''+cr.getAttribute('data-regionkey')+'\']';else{var fb=cr.querySelector('.frbtn');if(fb)sel='FRK:'+fb.getAttribute('data-frk');}
       if(sel)_pvVars(t).forEach(function(vv){if(vv&&!_pvCtlMap[vv])_pvCtlMap[vv]=sel;});});
     document.querySelectorAll('button[id]').forEach(function(b){var t=b.textContent.replace(/\s+/g,' ').trim().toLowerCase();if(!t||t.length>44)return;t=t.replace(/^[^a-z0-9]+/,'').replace(/[.\u2026]+$/,'').trim();if(!t)return;var sel='#'+b.id;_pvVars(t).forEach(function(vv){if(vv&&!_pvCtlMap[vv])_pvCtlMap[vv]=sel;});});
     document.querySelectorAll('label.row input[id],label.row select[id]').forEach(function(inp){var row=inp.closest('label.row');var l=row&&row.querySelector('.lbl');if(!l)return;var t=l.textContent.replace('?','').replace(/\s+/g,' ').trim().toLowerCase();if(!t)return;var sel='#'+inp.id;_pvVars(t).forEach(function(vv){if(vv&&!_pvCtlMap[vv])_pvCtlMap[vv]=sel;});});
   }catch(e){}return _pvCtlMap;}
   function _pvFlash(el){try{var row=el.closest('label.row,.row,.calrow')||el;row.scrollIntoView({block:'center',behavior:'auto'});row.classList.add('hlrow');setTimeout(function(){row.classList.remove('hlrow');},1900);}catch(e){}}
   function _pvGoto(el){var panel=el.closest('.panel');if(!panel)return false;var pid=panel.id;var tabid=(pid.indexOf('p_')===0)?pid.slice(2):pid.slice(1);var tb=document.querySelector('.tab[data-tab="'+tabid+'"]');if(tb){var g=tb.closest('.navgroup');if(g)g.classList.remove('collapsed');if(!tb.classList.contains('active'))tb.click();}
     setTimeout(function(){_pvFlash(el);},380);return true;}
   function pvJump(label){try{var q=String(label).toLowerCase().replace(/\s+/g,' ').trim();q=q.replace(/\.\.\.$/,'').trim();
     var key=_pvLabels()[q];
     if(key){var inp=document.querySelector('#pcycle [data-key="'+key+'"]');
       if(inp){var ct=document.querySelector('.tab[data-tab="cycle"]');if(ct&&!ct.classList.contains('active'))ct.click();setTimeout(function(){try{cygJump(key);}catch(e2){}},380);return true;}
       var any=document.querySelector('[data-key="'+key+'"]');if(any)return _pvGoto(any);}
     var sel=_pvCtls()[q];
     if(sel){var el2=null;if(sel.indexOf('FRK:')===0){var fb2=document.querySelector('.frbtn[data-frk="'+sel.slice(4)+'"]');el2=fb2?fb2.closest('.calrow'):null;}else el2=document.querySelector(sel);
       if(el2)return _pvGoto(el2);}
     return false;}catch(e){return false;}}
   function previewVisual(kind,key,el){
     try{
       if(kind!=='setting')return '';
       var K=String(key||'').replace(/^help:/,'');
       var inp=el&&el.querySelector('[data-key]');if(!inp)return '';
       var out='';
       var rng=el.querySelector('input.crng')||el.querySelector('input[type=range]');
       var v=parseFloat(inp.value),mn=parseFloat((rng||inp).min),mx=parseFloat((rng||inp).max);
       if(PV_IMP[K]){var im=PV_IMP[K];out=im.k==='trade'?_pvTrade(im,K,el):im.k==='toggle'?_pvToggle(im,el):_pvSafety(im,el);}
       else if(PV_COLK[K])out=_pvColour(K,el);
       else if(PV_MODE[K])out=_pvModeV(K,el);
       else if(PV_DM[K])out=_pvDM(K);
       else if(PV_RDO[K])out=_pvRdo(K);
       else if(K==='FINDS_BANK_RARITY')out=_pvRarity(el);
       else if(PV_NOTE[K])out=_pvNote(PV_NOTE[K]);
       else{
         var typ=inp.getAttribute('data-type')||inp.type;
         if(inp.type==='checkbox'||inp.type==='text'||typ==='bool'||typ==='str')out='';
         else if(PV_BRIGHT[K])out=_pvBright(K,v,mn,mx);
         else if(!isFinite(v))out='';
         else if(PV_TRAIN[K])out=_pvTimer(K,v,'ms',mn,mx);
         else if(K==='WEBHOOK_STATS_MIN'||K==='SAFE_STOP_RETRY_SEC')out=_pvRead(K,v,/MIN$/.test(K)?'min':'s',mn,mx);
         else if(/CLICKS$|_DIGS$|DIGS_TO_FILL|MAX_DIGS|TRIES$|RETRIES$/.test(K))out=_pvCount(K,v);
         else{
           out=_pvPan(K,el);
           if(!out){
             if(PV_SEQ[K])out=_pvSeq(K,v,mn,mx);
             else if(/TICKS|LIMIT|STALL|BAIL|CONFIRM|NO_PROGRESS|STOP_AFTER|DWELL|MAX_RETRIES|GLITCH|EMPTY_MS|AUTOSTOP_MINUTES|LAND_MAX_S/.test(K))out=_pvTrig(K,v,mn,mx,/MINUTES/.test(K)?'min':(/_SEC$|_S$/.test(K)?'s':(/_MS/.test(K)?'ms':'')));
             else if(/_SEC$|_S$|POLL|OCR|FAST|MIN$|MINUTES/.test(K))out=_pvRead(K,v,/MIN$|MINUTES/.test(K)?'min':(/_SEC$|_S$/.test(K)?'s':'ms'),mn,mx);
             else if(/_MS$/.test(K))out=_pvSeq(K,v,mn,mx);
           }
         }
       }
       var pills='';
       try{if(typeof DEF!=='undefined'&&DEF&&DEF.hasOwnProperty(K)){var d0=DEF[K];var cur=(inp.type==='checkbox')?(inp.checked?'on':'off'):String(inp.value);var df=(typeof d0==='boolean')?(d0?'on':'off'):String(d0);pills='<div class="ph-pills"><span>default <b>'+esc(df)+'</b></span><span>you <b>'+esc(cur)+'</b></span></div>';}}catch(e8){}
       return out+pills;
     }catch(e){}
     return '';
   }
   function md(t){t=String(t==null?'':t);var lines=t.split(/\r?\n/),out=[],list=null,para=[];
     var TAGS={raise:['↑','raise it if','up'],lower:['↓','lower it if','dn'],fixes:['⚑','fixes','fx'],pairs:['⇄','pairs with','ok'],healthy:['✓','healthy','ok'],climbing:['⚑','climbing?','fx'],fixpath:['→','fix path','up'],wrongif:['⚑','wrong if','fx'],owns:['→','its knobs','up'],when:['→','use it when','up']};
     function il(s){return s.replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>').replace(/\*([^*\n]+)\*/g,'<i>$1</i>').replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\{\{([^}]+)\}\}/g,'<s>$1</s>');}
     function chips(s){return s.split('|').map(function(c){c=c.trim();if(!c)return '';return '<span class="ph-lk" data-pvjump="'+esc(c)+'">'+il(c)+'</span>';}).join('');}
     function fp(){if(para.length){out.push('<p>'+il(para.join(' '))+'</p>');para=[];}}
     function fl(){if(list){out.push('<ul>'+list.join('')+'</ul>');list=null;}}
     for(var i=0;i<lines.length;i++){var ln=lines[i].trim();
       if(!ln){fp();fl();continue;}
       var mh=ln.match(/^##\s+(.*)/);if(mh){fp();fl();out.push('<h4>'+il(mh[1])+'</h4>');continue;}
       var mt=ln.match(/^(raise|lower|fixes|pairs|healthy|climbing|fixpath|wrongif|owns|when):\s*(.*)/i);
       if(mt){fp();fl();var tg=TAGS[mt[1].toLowerCase()];var body=(mt[1].toLowerCase()==='pairs'||mt[1].toLowerCase()==='fixpath'||mt[1].toLowerCase()==='owns')?chips(mt[2]):il(mt[2]);
         out.push('<div class="ph-row"><span class="ph-tag '+tg[2]+'"><i>'+tg[0]+'</i>'+tg[1]+'</span><span class="ph-tx">'+body+'</span></div>');continue;}
       var ms=ln.match(/^steps:\s*(.*)/i);
       if(ms){fp();fl();var st=ms[1].split('|');var sh='';for(var j=0;j<st.length;j++){if(st[j].trim())sh+='<div class="ph-step"><i>'+(j+1)+'</i><span>'+il(st[j].trim())+'</span></div>';}out.push('<div class="ph-steps">'+sh+'</div>');continue;}
       var mb=ln.match(/^[-•]\s+(.*)/);if(mb){fp();if(!list)list=[];list.push('<li>'+il(mb[1])+'</li>');continue;}
       var mc=ln.match(/^(Example|Tip|Note|Why)\s*:\s*(.*)/);if(mc){fp();fl();out.push('<div class="ph-call"><b>'+mc[1]+'</b> '+il(mc[2])+'</div>');continue;}
       fl();para.push(ln);}
     fp();fl();return out.join('')||'<p></p>';}
   function showHelp(kind,key,title,el){
     var ent=HELPMAP[key]||{};
     lastKind='help';lastArg=key;
     if(psub)psub.textContent='explanation';
     var mock=mockFor(key)||((kind==='cal'&&el)?calMock(el):'');
     var h='<div class="prevhelp"><h3>'+esc(title||key)+'</h3><div class="ph-kind">'+(KIND_LABEL[kind]||'Control')+'</div>';
     if(mock)h+=mock;
     var _pv=previewVisual(kind,key,el)||'';lastHelpKind=kind;lastHelpEl=el||null;_pvLastHTML=_pv;h+='<div id="pvbox">'+_pv+'</div>';
     h+='<div class="ph-body">'+md(ent.body||'Loading the full explanation...')+'</div>';
     if(ent.img)h+='<img class="phimg" src="'+esc(ent.img)+'" alt="">';
     var ev=ytEmbed(ent.vid);if(ev)h+='<div class="ph-vid"><iframe src="'+ev+'" allowfullscreen loading="lazy"></iframe></div>';
     if(key==='cyc:graph')h+=graphBreakdown();
     if(OWNER)h+='<div class="tebtns"><button class="touredit" id="prevedit">Edit</button></div>';
     h+='</div>';
     pbody.innerHTML=h;
     pvAnimate(pbody);
     if(OWNER){var pe=T('prevedit');if(pe)pe.onclick=function(){openEditor(key,'help');};}
   }
   function showGeneric(el){
     // Absolute fallback so the panel is NEVER empty.
     if(psub)psub.textContent='';
     var title='',bodyTxt='';
     if(el){
       var b=el.closest('button,.chip,a,label,select,input,.tab,.grouphdr,.acard,.stat,.rrow,.calrow,.bcard');
       if(b){title=(b.getAttribute('title')||b.textContent||'').replace(/\s+/g,' ').trim().slice(0,60);
         bodyTxt=b.getAttribute('title')||'';}
     }
     if(!title){var act=document.querySelector('.tab.active');title=act?(tabLabel(act.getAttribute('data-tab'))+' page'):'Prospector Lite';
       var at=act&&act.getAttribute('data-tab');bodyTxt=at?(chintOf(at)||''):'';}
     var h='<div class="prevhelp"><h3>'+esc(title||'Preview')+'</h3><div class="ph-kind">Control</div>'+
       '<div class="ph-body">'+(esc(bodyTxt)||'Hover a setting, button, tab or stat for a full explanation. Everything in the app has one.')+'</div></div>';
     pbody.innerHTML=h;
   }
   function graphBreakdown(){
     try{var M=cycModel(collect(),window._AB||{});var S=M.segs||[];if(!S.length)return '';
       var col={dig:'#a8794a',swalk:'#6ba1b5',glide:'#9bc07e',shake:'#caa06e',land:'#b58f6b',safety:'#b06b6b',other:'#9c9183'};
       var SN={dig:'Dig',swalk:'Walk back',glide:'Glide and start',shake:'Shake and drain',land:'Land and prove',safety:'Safety nets',other:'Other tuning'};
       var h='<div class="ph-meta">Every block below is one phase of a single pan, in the order it runs. Hover the matching bar on the timeline to highlight it, or click a bar to jump straight to its setting.</div>';
       var seen={};
       S.forEach(function(s){
         var lo=Math.round(s.lo||0),hi=Math.round(s.hi||0);
         var ms=(lo===hi)?(lo+'ms'):(lo+' to '+hi+'ms');
         var pk=s.jump||(s.parts&&s.parts[0]&&s.parts[0][0])||'';
         var deep=(pk&&!seen[pk])?((HELPMAP['help:'+pk]||{}).body||''):'';
         if(pk)seen[pk]=1;
         h+='<div class="prevseg-d">';
         h+='<div class="psd-h"><span class="sgdot" style="background:'+(col[s.stage]||'#9c9183')+'"></span>'+
            '<span class="psd-n">'+esc(s.name)+'</span><span class="psd-ms">'+ms+'</span></div>';
         h+='<div class="psd-stage">'+esc(SN[s.stage]||s.stage)+'</div>';
         if(s.note)h+='<div class="psd-note">'+esc(s.note)+'</div>';
         if(deep)h+='<div class="psd-body">'+md(deep)+'</div>';
         if(s.parts&&s.parts.length)h+='<div class="psd-parts">Behind it: '+s.parts.map(function(p){return '<b>'+esc(p[0])+'</b> = '+esc(p[1]);}).join(', ')+'</div>';
         h+='</div>';
       });
       return h;}catch(e){return '';}
   }
   function labelText(row){var l=row&&row.querySelector('.lbl,.calname,.sl,.al');return l?l.textContent.replace('?','').replace(/\s+/g,' ').trim():'';}
   // Zone-based resolution: the whole row/card/button is the hover zone.
   function resolve(el){
     if(!el||!el.closest)return null;
     var gh=el.closest('.grouphdr');
     if(gh){var g=gh.closest('.navgroup');var kids=g?[].map.call(g.querySelectorAll('.groupkids .tab'),function(t){return t.getAttribute('data-tab');}):[];
       return {kind:'group',name:(gh.textContent||'').replace(/[›\s]+$/,'').trim(),kids:kids};}
     var tab=el.closest('.side .tab');
     if(tab)return {kind:'tab',tabid:tab.getAttribute('data-tab')};
     // settings row (Cycle stages + section pages)
     var row=el.closest('label.crow,label.row,.crow,.row');
     if(row){var dk=row.querySelector('[data-key]');
       if(dk)return {kind:'setting',key:'help:'+dk.getAttribute('data-key'),title:labelText(row),el:row};}
     var cal=el.closest('.calrow');
     if(cal){var pk=cal.getAttribute('data-pkey'),rk=cal.getAttribute('data-regionkey'),frb=cal.querySelector('.frbtn');
       if(pk&&HELPMAP['cal:'+pk])return {kind:'cal',key:'cal:'+pk,title:labelText(cal),el:cal};
       if(rk&&HELPMAP['cal:'+rk])return {kind:'cal',key:'cal:'+rk,title:labelText(cal),el:cal};
       if(frb&&frb.getAttribute('data-frk'))return {kind:'cal',key:'fr:'+frb.getAttribute('data-frk'),title:labelText(cal),el:cal};}
     var cs=el.closest('.cstage');if(cs&&cs.id.indexOf('cs_')===0&&HELPMAP['stage:'+cs.id.slice(3)])return {kind:'stage',key:'stage:'+cs.id.slice(3),title:(cs.querySelector('h3')||{}).textContent||''};
     var cn=el.closest('.cnode');if(cn&&cn.getAttribute('data-stage')&&HELPMAP['stage:'+cn.getAttribute('data-stage')])return {kind:'stage',key:'stage:'+cn.getAttribute('data-stage'),title:'Stage'};
     if(el.closest('.cygraph'))return {kind:'cyc',key:'cyc:graph',title:'Cycle timeline'};
     if(el.closest('.cycwrap'))return {kind:'cyc',key:'cyc:diagram',title:'The cycle diagram'};
     if(el.closest('.rrow'))return {kind:'relic',key:'relicrow',title:'Relic row'};
     if(el.closest('#histbox'))return {kind:'history',key:'histlist',title:'Run history'};
     if(el.closest('#pkeys,#p_Keybinds'))return {kind:'control',key:'keybinds',title:'Keybinds'};
     if(el.closest('#runflow'))return {kind:'control',key:'runflow',title:'How the macro runs'};
     var bc=el.closest('.bcard');
     if(bc){var nm=(bc.querySelector('h3')||{}).textContent||'Build',ds=(bc.querySelector('.bdesc')||{}).textContent||'';
       return {kind:'build',dyn:{title:nm,body:'<b>'+esc(nm)+'</b>'+(ds?'. '+esc(ds):'')+'.<br><br>'+(HELPMAP.buildcard?HELPMAP.buildcard.body:'A build is a full snapshot of every setting plus relics. Load applies it all at once.')}};}
     var stat=el.closest('.stat');
     if(stat){var sv=stat.querySelector('.sv');var sid=sv?sv.id:'';if(sid)return {kind:'stat',key:sid,title:labelText(stat)};}
     var idel=el.closest('[id]');
     if(idel&&HELPMAP[idel.id]){var k=(idel.id==='hudbtn'||idel.id==='analyticsbtn'||idel.id==='popout'||idel.id==='coachtoggle')?'control':'control';return {kind:'control',key:idel.id,title:(idel.getAttribute('title')||idel.textContent||idel.id).replace(/\s+/g,' ').trim()};}
     return {kind:'generic',el:el};
   }
   var hoverT=null,lastEl=null,curSig='',lastHelpEl=null,lastHelpKind='',_pvLastHTML='';
   function sigOf(res){if(!res)return '';if(res.dyn)return (res.key||res.kind)+'|'+(res.dyn.title||'');return (res.kind||'')+'|'+(res.tabid||res.key||res.name||res.title||'');}
   function render(res){
     if(!res){showGeneric(null);return;}
     if(res.kind==='tab'){showTab(res.tabid);return;}
     if(res.kind==='group'){showGroup(res.name,res.kids);return;}
     if(res.kind==='generic'){showGeneric(res.el);return;}
     if(res.dyn){lastKind='dyn';if(psub)psub.textContent='explanation';
       pbody.innerHTML='<div class="prevhelp"><h3>'+esc(res.dyn.title)+'</h3><div class="ph-kind">'+(KIND_LABEL[res.kind]||'Control')+'</div><div class="ph-body">'+res.dyn.body+'</div></div>';return;}
     showHelp(res.kind,res.key,res.title,res.el);
   }
   document.addEventListener('mouseover',function(e){
     if(!body.classList.contains('prev-on'))return;
     if(running)return;
     var el=e.target;if(!el||!el.closest)return;
     if(prev&&prev.contains(el))return;
     if(el.closest('#tutedit,#lightbox,#tourmenu,.coach'))return;
     lastEl=el;clearTimeout(hoverT);hoverT=setTimeout(function(){var res=resolve(el);if(!res||res.kind==='generic')return;var sig=sigOf(res);if(sig===curSig)return;curSig=sig;render(res);},90);
   });
   window.__refreshPreview=function(){try{if(body.classList.contains('prev-on')){var res=resolve(lastEl);if(res&&res.kind!=='generic'){curSig=sigOf(res);render(res);}}}catch(e){}};
   if(pbody)pbody.addEventListener('click',function(e){
     var lk=e.target&&e.target.closest?e.target.closest('.ph-lk'):null;
     if(!lk)return;
     pvJump(lk.getAttribute('data-pvjump')||lk.textContent);});
   var _pvLiveT=null;
   function _pvLive(e){var t=e.target;if(!t||!t.matches)return;
     if(!(t.matches('[data-key]')||t.matches('input.crng')))return;
     if(!body.classList.contains('prev-on'))return;
     if(lastKind!=='help'||!lastHelpEl||String(lastArg||'').indexOf('help:')!==0)return;
     clearTimeout(_pvLiveT);_pvLiveT=setTimeout(function(){
       var box=T('pvbox');if(!box)return;
       var nh='';try{nh=previewVisual(lastHelpKind||'setting',lastArg,lastHelpEl)||'';}catch(e2){}
       if(nh!==_pvLastHTML){_pvLastHTML=nh;box.innerHTML=nh;pvAnimate(box);}},140);}
   document.addEventListener('input',_pvLive,true);
   document.addEventListener('change',_pvLive,true);
   showGeneric(null);
   setPrev(true);
 })();
 window.addLog=t=>{const l=$('#log');if(!l)return;let s=l.textContent+t+"\n";
   if(s.length>80000)s=s.slice(-60000).replace(/^[^\n]*\n/,'');
   l.textContent=s;l.scrollTop=l.scrollHeight;};
 window.refreshValues=async function(){try{const s=await window.pywebview.api.get_state();setVals(s.values);}catch(e){}};
 window.setRunning=r=>{$('#startbtn').disabled=r;$('#stopbtn').disabled=!r;
   $('#rstate').textContent=r?'running':'stopped';
   if(window.scrState)scrState(r?'running':'stopped');};
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
   const r=$('#rstate');if(r&&p)r.textContent='paused';
   if(window.scrState&&p)scrState('paused');};
 (function(){try{var ib=document.getElementById('introbox');if(!ib)return;if(localStorage.getItem('pp_intro_hide')==='1')ib.style.display='none';var x=document.getElementById('introx');if(x)x.onclick=()=>{try{localStorage.setItem('pp_intro_hide','1');}catch(e){}ib.style.display='none';};}catch(e){}})();
 function setStageBadge(stage,on){const el=document.getElementById('sb_'+stage);if(!el)return;
   el.className='stagebadge'+(on?' show':'');el.textContent=on?'!':'';
   el.title=on?'Tuning may be off in this stage; see the Cycle warning above.':'';}
 let HEALTH_CAL={ok:true,reason:''};
 // Banners keep rendering from their probes (and are clickable: they open the
 // warning drawer). The nav badges are owned EXCLUSIVELY by renderDiagBadges
 // -- the probes feed the diagnostics ctx Python-side instead of writing
 // badges here (the old dual-writer race is gone).
 function applyCalBadge(){const bs=document.querySelectorAll('.calbanner');
   const msgs=[];
   if(!HEALTH_CAL.ok)msgs.push('<b>Re-calibration needed.</b> '+HEALTH_CAL.reason);
   if(window.CAP_REVIEW)msgs.push('<b>Capacity calibration needs review.</b> '+(window.__esc?window.__esc(window.CAP_REVIEW):window.CAP_REVIEW));
   if(msgs.length){bs.forEach(b=>{b.innerHTML=msgs.join('<br>');b.classList.add('show');});}
   else{bs.forEach(b=>b.classList.remove('show'));}}
 async function checkCalHealth(){try{HEALTH_CAL=await window.pywebview.api.calibration_health()||{ok:true};}catch(_){HEALTH_CAL={ok:true};}
   try{window.CAP_REVIEW=((await window.pywebview.api.cap_bar_review())||{}).detail||'';}catch(_){window.CAP_REVIEW='';}
   applyCalBadge();
   if(window.refreshDiagnostics)refreshDiagnostics();}
 function applyHealth(s){
   const pans=Math.max(1,(s&&s.cycles)||0);
   const cb=document.getElementById('cycbanner');
   const STG=['dig','swalk','glide','shake','land','safety'];
   if(!s||pans<5){if(cb)cb.classList.remove('show');STG.forEach(st=>setStageBadge(st,false));return;}
   const rate=(v)=>(v||0)/pans;var issues=[];var bad={};
   if(rate(s.nudges)>=0.6){issues.push('lots of nudges ('+(s.nudges||0)+'): struggling to reach land, so raise Land settle in the Land stage');bad.land=1;}
   if(rate(s.shake_misses)>=0.4){issues.push('frequent missed shakes ('+(s.shake_misses||0)+'): raise the shake start delay in the Glide stage');bad.glide=1;}
   if(rate(s.recoveries)>=0.5){issues.push('frequent recoveries ('+(s.recoveries||0)+', getting stuck): re-check calibration and ease the Safety nets limits');bad.safety=1;}
   STG.forEach(st=>setStageBadge(st,!!bad[st]));
   if(cb){if(issues.length){cb.innerHTML='<b>Tuning may be off.</b> Over your last run: '+issues.join('; ')+'. The flagged stages below are marked yellow.';cb.classList.add('show');}else{cb.classList.remove('show');}}}
 // ===== diagnostics layer: badges + warning drawer + deep links + FAQ =====
 (function(){
   const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
   const api=()=>window.pywebview&&window.pywebview.api;
   let DIAG={events:[],summary:null};
   let DD={open:false,sel:null,applied:{}};   // applied: key -> {id,prev}
   let LOC=null,FAQ=null,_busy=false;
   const SEVR={INFO:0,NOTICE:1,WARNING:2,ERROR:3,CRITICAL:4};
   const CONFR={possible:0,medium:1,high:2};
   const RED=e=>e.severity==='ERROR'||e.severity==='CRITICAL';
   const CONF_LABEL={high:'High confidence',medium:'Medium confidence',possible:'Possible cause'};
   window.refreshDiagnostics=async function(force){
     if(_busy)return;_busy=true;
     try{const a=api();if(!a||!a.diagnostics_state)return;
       let r=null;try{r=await a.diagnostics_state(!!force);}catch(e){r=null;}
       if(!r||!Array.isArray(r.events))return;
       DIAG=r;renderDiagBadges(r.summary||{});
       if(DD.open)drawerRender();
     }finally{_busy=false;}};
   function renderDiagBadges(summary){
     document.querySelectorAll('.navbadge').forEach(el=>{el.className='navbadge';el.textContent='';el.title='';el.dataset.top='';});
     const tabs=(summary&&summary.tabs)||{};
     Object.keys(tabs).forEach(tab=>{
       const el=document.querySelector('.navbadge[data-badge="'+tab+'"]');if(!el)return;
       const b=tabs[tab]||{};
       if(b.red>0){el.className='navbadge show red';el.textContent=b.red>1?String(b.red):'!';el.title=b.top_red_title||'';el.dataset.top=b.top_red_id||'';}
       else if(b.yellow>0){el.className='navbadge show yellow';el.textContent=b.yellow>1?String(b.yellow):'!';el.title=b.top_yellow_title||'';el.dataset.top=b.top_yellow_id||'';}});}
   window.renderDiagBadges=renderDiagBadges;
   // badge clicks open the drawer at that tab's top event; the tab must NOT switch
   document.querySelectorAll('.navbadge').forEach(el=>{
     el.style.cursor='pointer';
     el.addEventListener('click',ev=>{
       if(!el.classList.contains('show'))return;
       ev.stopPropagation();ev.preventDefault();
       openDiagDrawer(el.dataset.top||null);});});
   // banners become clickable: open the drawer at their tab's top event
   function bannerTop(tab){const t=((DIAG.summary||{}).tabs||{})[tab]||{};return t.top_red_id||t.top_yellow_id||null;}
   ['calbanner','calbanner2'].forEach(id=>{const b=document.getElementById(id);if(!b)return;
     b.style.cursor='pointer';b.setAttribute('role','button');
     b.addEventListener('click',()=>openDiagDrawer(bannerTop('cal')));});
   (function(){const b=document.getElementById('cycbanner');if(!b)return;
     b.style.cursor='pointer';b.setAttribute('role','button');
     b.addEventListener('click',e=>{e.stopPropagation();openDiagDrawer(bannerTop('cycle'));});})();
   // ---- drawer ----
   window.openDiagDrawer=async function(eventId){
     await window.refreshDiagnostics(true);
     const d=document.getElementById('diagdrawer');if(!d)return;
     DD.open=true;d.classList.add('show');
     const evs=DIAG.events||[];
     DD.sel=(eventId&&evs.some(e=>e.id===eventId))?eventId:(evs[0]?evs[0].id:null);
     drawerRender();};
   function closeDrawer(){DD.open=false;const d=document.getElementById('diagdrawer');if(d)d.classList.remove('show');recHide();}
   const dc=document.getElementById('ddclose');if(dc)dc.onclick=closeDrawer;
   document.addEventListener('keydown',e=>{
     if(e.key!=='Escape')return;
     const fm=document.getElementById('faqmodal');
     if(fm&&fm.classList.contains('show')){fm.classList.remove('show');return;}
     const dr=document.getElementById('diagrec');
     if(dr&&dr.style.display==='block'){recHide();return;}
     if(DD.open)closeDrawer();});
   function sortForList(evs){return evs.slice().sort((a,b)=>
     (SEVR[b.severity]||0)-(SEVR[a.severity]||0)
     ||(b.recurrence_count||0)-(a.recurrence_count||0)
     ||(CONFR[b.confidence]||0)-(CONFR[a.confidence]||0));}
   function drawerRender(){
     const list=document.getElementById('ddlist'),body=document.getElementById('ddbody');
     if(!list||!body)return;
     const evs=sortForList(DIAG.events||[]);
     if(!evs.length){list.innerHTML='';body.innerHTML='<div class="ddsec"><p>No active warnings. Everything the diagnostics layer watches looks fine right now.</p></div>';return;}
     if(!DD.sel||!evs.some(e=>e.id===DD.sel))DD.sel=evs[0].id;
     list.innerHTML=evs.length>1?evs.map(e=>
       '<button type="button" class="ddev'+(e.id===DD.sel?' sel':'')+'" data-ev="'+esc(e.id)+'">'
       +'<span class="ddchip '+(RED(e)?'red':(e.severity==='WARNING'||e.severity==='NOTICE')?'yellow':'info')+'">'+esc(e.severity)+'</span>'
       +'<span class="ddevt">'+esc(e.title)+'</span>'
       +((e.recurrence_count||1)>1?'<span class="ddevn">&times;'+e.recurrence_count+'</span>':'')
       +'</button>').join(''):'';
     list.querySelectorAll('button[data-ev]').forEach(b=>b.onclick=()=>{DD.sel=b.dataset.ev;drawerRender();});
     const ev=evs.find(e=>e.id===DD.sel);if(!ev){body.innerHTML='';return;}
     body.innerHTML=eventHtml(ev);wireEvent(body,ev);}
   function recsSorted(ev){return (ev.recommendations||[]).slice().sort((a,b)=>(a.priority||99)-(b.priority||99));}
   function eventHtml(ev){
     const recs=recsSorted(ev),first=recs[0]||null;
     const sevCls=RED(ev)?'red':(ev.severity==='WARNING'||ev.severity==='NOTICE')?'yellow':'info';
     let h='<div class="ddsec" style="display:flex;align-items:center;gap:8px">'
       +'<span class="ddchip '+sevCls+'">'+esc(ev.severity)+'</span>'
       +((ev.recurrence_count||1)>1?'<span class="ddconf">seen &times;'+ev.recurrence_count+'</span>':'')
       +'<span class="ddcode">'+esc(ev.code||'')+'</span></div>';
     h+='<div class="ddsec"><h4>What happened</h4><p><b>'+esc(ev.title)+'</b></p><p>'+esc(ev.summary)+'</p></div>';
     h+='<div class="ddsec"><h4>What Prospector Lite observed</h4><p>'+esc(ev.observed)+'</p>'
       +((ev.evidence||[]).length?'<ul>'+(ev.evidence||[]).map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>':'')+'</div>';
     if(first)h+='<div class="ddsec"><h4>Most likely cause</h4>'
       +'<p><span class="ddconf">'+esc(CONF_LABEL[ev.confidence]||'Possible cause')+'</span></p>'
       +'<p style="margin-top:6px">'+esc(first.explanation)+'</p></div>';
     if(first)h+='<div class="ddsec"><h4>Recommended first action</h4>'
       +'<div class="ddfirst"><b>'+esc(first.title)+'</b><span style="font-size:12px;color:var(--mut)">'+esc(first.expected_effect||'')+'</span></div></div>';
     const stRows=[];const seen={};
     recs.forEach(r=>(r.setting_targets||[]).forEach(t=>{if(seen[t.key])return;seen[t.key]=1;stRows.push({t:t,r:r});}));
     if(stRows.length){h+='<div class="ddsec"><h4>Exact settings to review</h4>'
       +stRows.map((x,i)=>{const t=x.t,r=x.r;
         const ap=DD.applied[t.key];
         const val=(t.current==null?'?':t.current)+(t.suggested!=null?(' &#8594; '+t.suggested):'')+(t.units?(' '+esc(t.units)):'');
         return '<div class="ddrow" data-si="'+i+'">'
           +'<span class="ddlab"><b>'+esc(t.label||t.key)+'</b></span>'
           +'<span class="ddval">'+val+'</span>'
           +(t.reason?'<span class="ddwhy">'+esc(t.reason)+'</span>':'')
           +'<span style="flex-basis:100%;display:flex;gap:7px;flex-wrap:wrap">'
           +'<button type="button" class="btn2" data-open-setting="'+esc(t.key)+'" data-si="'+i+'">Open setting</button>'
           +(r.auto_apply&&t.suggested!=null&&!ap?'<button type="button" class="btn2" data-apply="'+esc(t.key)+'" data-si="'+i+'">Apply suggested value</button>':'')
           +(ap?'<button type="button" class="btn2" data-undo="'+esc(t.key)+'">Undo</button>':'')
           +'</span></div>';}).join('')+'</div>';}
     const calT=[];recs.forEach(r=>(r.calibration_targets||[]).forEach(c=>{if(calT.indexOf(c)<0)calT.push(c);}));
     if(calT.length)h+='<div class="ddsec"><h4>Exact calibrations to review</h4>'
       +calT.map(c=>'<div class="ddrow"><span class="ddlab">'+esc(c)+'</span>'
       +'<button type="button" class="btn2" data-open-cal="'+esc(c)+'">Open calibration</button></div>').join('')+'</div>';
     const permT=[];recs.forEach(r=>(r.permission_targets||[]).forEach(p=>{if(permT.indexOf(p)<0)permT.push(p);}));
     if(permT.length)h+='<div class="ddsec"><h4>Permissions</h4>'
       +permT.map(p=>'<div class="ddrow"><span class="ddlab">'+esc(p)+'</span>'
       +'<button type="button" class="btn2" data-open-perm="'+esc(p)+'">Open permission</button></div>').join('')+'</div>';
     if((ev.other_causes||[]).length)h+='<div class="ddsec"><h4>Other possible causes</h4><ul>'
       +(ev.other_causes||[]).map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul></div>';
     if(first&&first.expected_effect)h+='<div class="ddsec"><h4>Expected effect</h4><p>'+esc(first.expected_effect)+'</p></div>';
     if(first&&first.tradeoff)h+='<div class="ddsec"><h4>Tradeoff</h4><p>'+esc(first.tradeoff)+'</p></div>';
     if(first&&first.verify)h+='<div class="ddsec"><h4>How to verify</h4><p>'+esc(first.verify)+'</p></div>';
     if(ev.faq_id)h+='<div class="ddsec"><h4>Related FAQ</h4>'
       +'<button type="button" class="btn2" id="ddfaq" data-faqid="'+esc(ev.faq_id)+'">Open the FAQ entry</button></div>';
     h+='<div class="ddbtns"><button type="button" class="btn2" id="ddcopy">Copy diagnostic details</button>'
       +((ev.dismissible!==false)?'<button type="button" class="btn2" id="dddismiss">Dismiss</button>':'')
       +((ev.suppressible!==false&&ev.severity!=='CRITICAL')?'<button type="button" class="btn2" id="ddsuppress">Don’t show again for this code</button>':'')
       +'</div>';
     return h;}
   function wireEvent(body,ev){
     const recs=recsSorted(ev);
     body.querySelectorAll('button[data-open-setting]').forEach(b=>b.onclick=()=>{
       const key=b.dataset.openSetting;
       let target=null,rec=null;
       recs.forEach(r=>(r.setting_targets||[]).forEach(t=>{if(t.key===key&&!target){target=t;rec=r;}}));
       navigateToSetting(key,target?{target:target,allowApply:!!(rec&&rec.auto_apply),verify:rec?rec.verify:''}:null);});
     body.querySelectorAll('button[data-apply]').forEach(b=>b.onclick=async()=>{
       const key=b.dataset.apply;let target=null;
       recs.forEach(r=>(r.setting_targets||[]).forEach(t=>{if(t.key===key&&!target)target=t;}));
       if(!target||target.suggested==null)return;
       b.disabled=true;let r=null;
       try{r=await api().diag_apply({key:key,suggested:target.suggested});}catch(e){r=null;}
       if(r&&r.ok){DD.applied[key]={id:r.id,prev:r.prev};toast('Applied: '+key+' = '+r.next);}
       else{toast('Could not apply'+((r&&r.error_code)?' ['+r.error_code+']':''));b.disabled=false;}
       drawerRender();});
     body.querySelectorAll('button[data-undo]').forEach(b=>b.onclick=async()=>{
       const key=b.dataset.undo,a=DD.applied[key];if(!a)return;
       let r=null;try{r=await api().diag_undo(a.id);}catch(e){r=null;}
       if(r&&r.ok){delete DD.applied[key];toast('Restored '+key);}
       else toast('Could not undo');
       drawerRender();});
     body.querySelectorAll('button[data-open-cal]').forEach(b=>b.onclick=()=>navigateToCalibration(b.dataset.openCal));
     body.querySelectorAll('button[data-open-perm]').forEach(b=>b.onclick=()=>navigateToPermission(b.dataset.openPerm));
     const fq=body.querySelector('#ddfaq');if(fq)fq.onclick=()=>openFaq(fq.dataset.faqid);
     const cp=body.querySelector('#ddcopy');if(cp)cp.onclick=async()=>{
       try{await navigator.clipboard.writeText(JSON.stringify(ev,null,1));toast('Diagnostic details copied');}
       catch(e){toast('Clipboard unavailable');}};
     const dm=body.querySelector('#dddismiss');if(dm)dm.onclick=async()=>{
       try{await api().diag_dismiss(ev.id);}catch(e){}
       DIAG.events=(DIAG.events||[]).filter(x=>x.id!==ev.id);DD.sel=null;
       if(!(DIAG.events||[]).length)closeDrawer();else drawerRender();
       window.refreshDiagnostics(true);};
     const sp=body.querySelector('#ddsuppress');if(sp)sp.onclick=async()=>{
       try{await api().diag_suppress(ev.code);}catch(e){}
       DIAG.events=(DIAG.events||[]).filter(x=>x.code!==ev.code);DD.sel=null;
       if(!(DIAG.events||[]).length)closeDrawer();else drawerRender();
       window.refreshDiagnostics(true);};}
   // ---- floating recommendation chip near a deep-linked setting row ----
   function recHide(){const el=document.getElementById('diagrec');if(el)el.style.display='none';}
   function recShow(key,info){const el=document.getElementById('diagrec');if(!el||!info||!info.target)return;
     const inp=document.querySelector('[data-key="'+key+'"]');if(!inp)return;
     const t=info.target;
     el.innerHTML='<div class="drhead">'+esc(t.label||key)
       +'<button type="button" class="drx" aria-label="Close">&#10005;</button></div>'
       +'<div>'+(t.current==null?'?':t.current)+' &#8594; <b>'+(t.suggested==null?'?':t.suggested)+'</b>'+(t.units?(' '+esc(t.units)):'')+'</div>'
       +(t.reason?'<div style="color:var(--mut);margin-top:3px">Why: '+esc(t.reason)+'</div>':'')
       +'<div class="drbtns">'
       +(info.allowApply&&t.suggested!=null?'<button type="button" class="btn2" data-recapply="1">Apply</button>':'')
       +(info.verify?'<button type="button" class="btn2" data-rechow="1" title="'+esc(info.verify)+'">How to test</button>':'')
       +'</div>';
     const r=(inp.closest('.row,.crow,label.row')||inp).getBoundingClientRect();
     el.style.display='block';
     el.style.top=Math.min(window.innerHeight-140,r.bottom+8)+'px';
     el.style.left=Math.max(10,Math.min(r.left,window.innerWidth-350))+'px';
     el.querySelector('.drx').onclick=recHide;
     const ab=el.querySelector('[data-recapply]');
     if(ab)ab.onclick=async()=>{ab.disabled=true;let res=null;
       try{res=await api().diag_apply({key:key,suggested:t.suggested});}catch(e){res=null;}
       if(res&&res.ok){DD.applied[key]={id:res.id,prev:res.prev};toast('Applied: '+key+' = '+res.next);recHide();}
       else{toast('Could not apply'+((res&&res.error_code)?' ['+res.error_code+']':''));ab.disabled=false;}};
     setTimeout(()=>{const away=e2=>{if(!el.contains(e2.target)){recHide();
         document.removeEventListener('mousedown',away,true);
         document.removeEventListener('click',away,true);}};
       document.addEventListener('mousedown',away,true);
       document.addEventListener('click',away,true);},50);}
   // ---- exact deep links (stable ids, no text matching) ----
   async function locator(){if(LOC)return LOC;
     try{LOC=await api().setting_locator()||{};}catch(e){LOC={};}
     if(!LOC||typeof LOC!=='object')LOC={};
     return LOC;}
   window.navigateToSetting=async function(key,info){
     window.__deepNavAt=Date.now(); // a deep link owns this navigation -- the first-visit tab tour must not yank it away
     const loc=(await locator())[key]||null;
     if(loc&&loc.control==='cycle'){
       const ct=document.querySelector('.tab[data-tab="cycle"]');
       if(ct&&!ct.classList.contains('active'))ct.click();
       setTimeout(()=>{try{cygJump(key);}catch(e){}
         if(info)recShow(key,info);},420);
       return;}
     // section-tab keys: _pvGoto's approach -- derive the tab from the
     // OWNING panel id (handles the pinned 'p'+id vs 'p_'+title split and
     // hidden tabs), uncollapse its navgroup, click it, then flash the row
     const el=document.querySelector('[data-key="'+key+'"]');
     if(el){
       const panel=el.closest('.panel');
       if(panel){const pid=panel.id;
         const tabid=(pid.indexOf('p_')===0)?pid.slice(2):pid.slice(1);
         const tb=document.querySelector('.tab[data-tab="'+tabid+'"]');
         if(tb){const g=tb.closest('.navgroup');if(g)g.classList.remove('collapsed');
           if(!tb.classList.contains('active'))tb.click();}}
       setTimeout(()=>{flashEl(el.closest('label.row,.row,.crow')||el);
         if(info)recShow(key,info);},420);
       return;}
     if(loc&&loc.tab){const tb=document.querySelector('.tab[data-tab="'+loc.tab+'"]');if(tb)tb.click();}};
   const CAL_ANCHORS={
     cap_bar:'.calrow[data-pkey="CAP_FULL_PIXEL"]',
     pan_prompt:'.calrow[data-pkey="PAN_PIX"]',
     deposit_prompt:'.calrow[data-pkey="DEPOSIT_PIX"]',
     shake_prompt:'.calrow[data-pkey="SHAKE_PIX"]',
     dig_green:'.calrow[data-pkey="DIG_TRIGGER_PIXEL"]',
     money_region:'.calrow[data-regionkey="MONEY"]',
     shards_region:'.calrow[data-regionkey="SHARDS"]',
     find_region:'.calrow[data-regionkey="FIND"]',
     cue_masks:'.advcal',
     roblox_window:'#winstat',
     autopan_button:'.frbtn[data-frk="apon"]',
     fortune_river:'.frbtn[data-frk="text"]'};
   function flashEl(el){if(!el)return;
     try{el.scrollIntoView({block:'center'});}catch(e){}
     el.classList.add('hlrow');setTimeout(()=>el.classList.remove('hlrow'),1900);}
   window.navigateToCalibration=function(itemId){
     window.__deepNavAt=Date.now();
     const s=document.getElementById('setup');
     if(s&&s.classList.contains('show')&&window.__wizCalDetail){__wizCalDetail(itemId);return;}
     const tb=document.querySelector('.tab[data-tab="cal"]');
     if(tb&&!tb.classList.contains('active'))tb.click();
     setTimeout(()=>{
       const sel=CAL_ANCHORS[itemId]||'';let el=sel?document.querySelector(sel):null;
       if(el&&el.classList&&el.classList.contains('frbtn'))el=el.closest('.calrow')||el;
       if(itemId==='cap_bar'){
         flashEl(document.querySelector('.calrow[data-pkey="CAP_LEFT_PIXEL"]'));
         flashEl(document.getElementById('capTestRow'));
         const ctb=document.getElementById('capTest');
         if(ctb){ctb.classList.add('hlrow');setTimeout(()=>ctb.classList.remove('hlrow'),1900);}}
       flashEl(el);},420);};
   window.navigateToPermission=function(capId){
     window.__deepNavAt=Date.now();
     const tb=document.querySelector('.tab[data-tab="trust"]');
     if(tb&&!tb.classList.contains('active'))tb.click();
     let tries=0;
     const t=setInterval(()=>{tries++;
       const card=document.querySelector('#ptrust .cap-card[data-capid="'+capId+'"]');
       if(card){clearInterval(t);flashEl(card);
         const test=card.querySelector('button[data-act="test"]');
         if(test){test.classList.add('hlrow');setTimeout(()=>test.classList.remove('hlrow'),1900);}}
       else if(tries>25)clearInterval(t);},200);};
   // ---- FAQ browser ----
   async function faqEntries(){if(FAQ)return FAQ;
     let r=null;try{r=await api().faq_list();}catch(e){r=null;}
     FAQ=(r&&Array.isArray(r.entries))?r.entries:[];return FAQ;}
   window.openFaq=async function(entryId){
     const m=document.getElementById('faqmodal');if(!m)return;
     await faqEntries();m.classList.add('show');
     const s=document.getElementById('faqsearch');if(s)s.value='';
     if(entryId&&FAQ.some(e=>e.id===entryId))faqEntry(entryId);else faqList('');};
   function faqClose(){const m=document.getElementById('faqmodal');if(m)m.classList.remove('show');}
   function faqList(q){
     const list=document.getElementById('faqlist'),entry=document.getElementById('faqentry');
     const back=document.getElementById('faqback'),srch=document.getElementById('faqsearch');
     if(!list)return;
     list.style.display='';if(entry)entry.style.display='none';
     if(back)back.style.display='none';if(srch)srch.style.display='';
     q=String(q||'').toLowerCase().trim();
     const hits=(FAQ||[]).filter(e=>!q
       ||String(e.question||'').toLowerCase().indexOf(q)>=0
       ||(e.symptoms||[]).some(s=>String(s).toLowerCase().indexOf(q)>=0));
     list.innerHTML=hits.length?hits.map(e=>
       '<button type="button" class="faqq" data-faq="'+esc(e.id)+'">'+esc(e.question)
       +((e.symptoms||[]).length?'<span class="fqs">'+esc((e.symptoms||[])[0])+'</span>':'')
       +'</button>').join('')
       :'<p class="chint">No FAQ entry matches that search.</p>';
     list.querySelectorAll('button[data-faq]').forEach(b=>b.onclick=()=>faqEntry(b.dataset.faq));}
   function faqEntry(id){
     const list=document.getElementById('faqlist'),entry=document.getElementById('faqentry');
     const back=document.getElementById('faqback'),srch=document.getElementById('faqsearch');
     const e=(FAQ||[]).find(x=>x.id===id);if(!e||!entry)return;
     if(list)list.style.display='none';entry.style.display='';
     if(back)back.style.display='';if(srch)srch.style.display='none';
     let h='<p><b>'+esc(e.question)+'</b></p>';
     if((e.symptoms||[]).length)h+='<h4>Symptoms</h4><ul>'+(e.symptoms||[]).map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>';
     if((e.likely_causes||[]).length)h+='<h4>Likely causes</h4><ul>'+(e.likely_causes||[]).map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>';
     if(e.first_action)h+='<h4>First thing to try</h4><p>'+esc(e.first_action)+'</p>';
     if((e.steps||[]).length)h+='<h4>Steps</h4><ol>'+(e.steps||[]).map(x=>'<li>'+esc(x)+'</li>').join('')+'</ol>';
     if(e.verify)h+='<h4>How to verify</h4><p>'+esc(e.verify)+'</p>';
     if((e.platforms||[]).length)h+='<h4>Applies to</h4><p>'+esc((e.platforms||[]).join(', '))+'</p>';
     const links=[];
     (e.related_settings||[]).forEach(k=>links.push('<button type="button" class="btn2" data-fs="'+esc(k)+'">Open setting: '+esc(k)+'</button>'));
     (e.related_calibrations||[]).forEach(c=>links.push('<button type="button" class="btn2" data-fc="'+esc(c)+'">Open calibration: '+esc(c)+'</button>'));
     (e.related_permissions||[]).forEach(p=>links.push('<button type="button" class="btn2" data-fp="'+esc(p)+'">Open permission: '+esc(p)+'</button>'));
     if(links.length)h+='<h4>Open the exact surface</h4><div class="faqlinks">'+links.join('')+'</div>';
     entry.innerHTML=h;
     entry.querySelectorAll('button[data-fs]').forEach(b=>b.onclick=()=>{faqClose();closeDrawer();navigateToSetting(b.dataset.fs,null);});
     entry.querySelectorAll('button[data-fc]').forEach(b=>b.onclick=()=>{faqClose();closeDrawer();navigateToCalibration(b.dataset.fc);});
     entry.querySelectorAll('button[data-fp]').forEach(b=>b.onclick=()=>{faqClose();closeDrawer();navigateToPermission(b.dataset.fp);});}
   (function(){const s=document.getElementById('faqsearch');if(s)s.addEventListener('input',()=>faqList(s.value));
     const c=document.getElementById('faqclose');if(c)c.onclick=faqClose;
     const b=document.getElementById('faqback');if(b)b.onclick=()=>faqList((document.getElementById('faqsearch')||{}).value||'');
     const m=document.getElementById('faqmodal');if(m)m.addEventListener('click',e=>{if(e.target===m)faqClose();});})();
   // every [data-faq-open] anywhere (Settings page, Calibrate tab, Trust
   // Center, wizard readiness page) opens the browser
   document.addEventListener('click',e=>{
     const b=e.target&&e.target.closest?e.target.closest('[data-faq-open]'):null;
     if(!b)return;e.preventDefault();
     openFaq(b.getAttribute('data-faq-open')||null);});
 })();
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
 window.setStats=s=>{if(!s)return;try{applyHealth(s);}catch(e){}
   try{if(window.refreshDiagnostics)refreshDiagnostics();}catch(e){}
   var _rs=Math.max(0,Math.round(s.runtime_s||0)),_rh=Math.floor(_rs/3600),_rm=Math.floor((_rs%3600)/60),_rss=String(_rs%60).padStart(2,'0');
   $('#st_run').textContent=_rh>0?(_rh+':'+String(_rm).padStart(2,'0')+':'+_rss):(_rm+':'+_rss); $('#st_cyc').textContent=s.cycles||0;
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
   if(!list||!list.length){box.innerHTML='<div class="hempty">No runs yet. Finish a session and it shows up here.</div>';return;}
   box.innerHTML=list.slice().reverse().map(r=>{
     const _rs=(r.runtime_s||0),_h=Math.floor(_rs/3600),_mm=Math.floor((_rs%3600)/60),_ss=Math.round(_rs%60);
     const rt=(_h>0?(_h+'h '+_mm+'m'):(_mm>0?(_mm+'m '+_ss+'s'):(_ss+'s')));
     const cells=[['pans',r.cycles||0],['pans/hr',r.pans_per_hr||0],['digs',(r.digs!=null?r.digs:'-')],['clean',((r.clean_pct!=null&&r.cycles)?(r.clean_pct+'%'):'-')],['recoveries',r.recoveries||0],['nudges',r.nudges||0],['relics',r.relics_used||0],['finds',r.finds_count||0]];
     const grid=cells.map(c=>'<div class="hc"><div class="hcv">'+c[1]+'</div><div class="hcl">'+c[0]+'</div></div>').join('');
     const logbtn=r.log_file?('<button type="button" class="btn2 hlogbtn" data-log="'+_esc(r.log_file)+'">View full log</button>'):'<span class="hnolog">no saved log</span>';
     return '<div class="hcard"><div class="hc-hd"><b>'+_esc(r.ended||'run')+'</b>'+(r.tracker?'<span class="hbadge tracker">Tracker</span>':'')+(r.script?'<span class="hbadge script">'+_esc(r.script)+'</span>':'')+'<span class="hbadge">'+_esc(r.reason||r.stop_reason||'manual')+'</span><span class="hc-rt">'+rt+'</span></div>'+'<div class="hc-grid">'+grid+'</div>'+_earn(r)+_phases(r)+_whyLine(r)+_timeline(r)+'<div class="hc-foot">'+logbtn+'</div></div>';}).join('');
   box.querySelectorAll('.hlogbtn').forEach(b=>b.onclick=async()=>{let r;try{r=await window.pywebview.api.run_log(b.dataset.log);}catch(e){r={error:'failed to load'};}showLogModal((r&&r.text)?r.text:('('+((r&&r.error)||'no log')+')'),b.dataset.log);});}
 function showLogModal(text,name){var m=document.getElementById('logmodal');
   if(!m){m=document.createElement('div');m.id='logmodal';
     m.innerHTML='<div class="lm-box"><div class="lm-hd"><b>Run log</b><span class="lm-name"></span><button type="button" class="lm-x">✕</button></div><pre class="lm-body"></pre></div>';
     document.body.appendChild(m);
     m.addEventListener('click',function(e){if(e.target===m)m.style.display='none';});
     m.querySelector('.lm-x').onclick=function(){m.style.display='none';};
     document.addEventListener('keydown',function(e){if(e.key==='Escape'&&m.style.display==='flex')m.style.display='none';});}
   m.querySelector('.lm-name').textContent=name||'';m.querySelector('.lm-body').textContent=text;m.style.display='flex';}
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
   const id=b.dataset.tab; const pid=(id==='run'||id==='cycle'||id==='builds'||id==='cal'||id==='relics'||id==='hist'||id==='studio'||id==='keys'||id==='trust')?('p'+id):('p_'+id);
   document.getElementById(pid).classList.add('active');
   const _g=b.closest('.navgroup');if(_g)_g.classList.remove('collapsed');
   if(id==='hist')loadHistory();
   if(id==='builds')loadBuildsPage();
   if(id==='trust'&&window.__tcRender)__tcRender();
   if(window.refreshDiagnostics)refreshDiagnostics();});
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
 // The overlay API reports failure IN-BAND ({error, error_code}) -- it never
 // rejects -- so every picker entry point must await and surface the result;
 // a fire-and-forget call turns a Screen-Recording refusal into a silent
 // no-op ("the button does nothing").
 async function overlayCall(p){let r=null;
   try{r=await p;}catch(e){r={error:String(e)};}
   if(r&&r.error){toast(r.error+(r.error_code?(' ['+r.error_code+']'):''));
     if(r.needs_permission){const tb=document.querySelector('.tab[data-tab="trust"]');if(tb)tb.click();}}
   return r;}
 window.__overlayCall=overlayCall;
 $$('.calbtn').forEach(btn=>btn.onclick=()=>{
   const key=btn.dataset.pkey;
   const lab=((btn.closest('.calrow')||document).querySelector('.calname')||{}).textContent||key;
   overlayCall(window.pywebview.api.start_overlay_calibrate(key,lab));});
 $$('.regbtn').forEach(btn=>btn.onclick=()=>{
   const base=btn.dataset.regionkey,lab=btn.dataset.reglabel||base;
   overlayCall(window.pywebview.api.start_overlay_region(base,lab));});
 let regionPreviews={};
 function setRegionPreviews(rp){regionPreviews=rp||{};}
 function markRegions(px){['MONEY','SHARDS','FIND'].forEach(b=>{
   const el=document.getElementById('rg_'+b);if(!el)return;
   const tl=px[b+'_TL_PIXEL']||[0,0],br=px[b+'_BR_PIXEL']||[0,0];
   const set=(tl[0]||tl[1]||br[0]||br[1]);
   el.textContent=set?('set · '+Math.abs(br[0]-tl[0])+'×'+Math.abs(br[1]-tl[1])+' px'):'not set';
   el.className='frstat'+(set?' ok':'');
   const im=document.getElementById('rgimg_'+b);const rp=regionPreviews[b];
   if(im){if(set&&rp&&rp.preview){im.src=rp.preview;im.style.display='';}
     else{im.style.display='none';im.removeAttribute('src');}}});
   if(window.__refreshPreview){try{window.__refreshPreview();}catch(e){}}}
 window.__calRefresh=async()=>{try{const s=await window.pywebview.api.get_state();
   setPixels(s.pixels||{});setColors(s.colors||{});setFR(s.fr||{});window.__pvfr=s.fr||{};setRegionPreviews(s.region_previews||{});markRegions(s.pixels||{});toast('Calibrated ✓');}catch(e){}};
 (function(){const A=()=>window.pywebview.api;
   const WIZ=[
    {t:'Guided calibration',b:'This sets up every detection spot for you. Keep Roblox open with the HUD visible, then click Detect to find the game window.',d:async()=>{const w=await A().detect_roblox();return (w&&w.found)?{ok:true,msg:'Found Roblox '+w.w+'×'+w.h}:{ok:false,error:'Roblox not found, open the game, then Detect.'};},m:null},
    {t:'Capacity bar, RIGHT end',b:'Dig until your capacity bar is <b>completely full</b> (all yellow). Detect, the red ✕ should sit on the <b>right tip</b>. Confirm (or Redo).',d:()=>A().wizard_propose('CAP_RIGHT','Capacity right end'),m:null},
    {t:'Capacity bar, LEFT end',b:'Keep the bar full. Detect, the red ✕ should sit on the <b>left tip</b> (where the bar starts). Confirm (or Redo). This sets the bar width the macro watches to register digs.',d:()=>A().wizard_propose('CAP_LEFT','Capacity left end'),m:null},
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
       if('detected' in r){msg=r.detected?'Found a spot, check the red ✕ in the overlay, then Confirm (or Redo to pick it yourself).':'Could not auto-find it, click the spot in the overlay, then Confirm.';}
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
   window.__overlayCall(window.pywebview.api.start_overlay_calibrate(key,lab));});
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
 $('#savepixels').onclick=async()=>{
   const r=await window.pywebview.api.save_pixels(collectPixels(),collectColors(),collectFR());
   if(r&&r.ok===false){ // capacity pair rejected: nothing was written
     toast('Not saved - the capacity endpoints failed validation');
     const o=document.getElementById('capTestOut');
     if(o)o.innerHTML='<div class="detrow det-no"><b>Calibration NOT saved.</b> Previous values kept.</div>'
       +((r.reasons)||[]).map(x=>'<div class="detrow det-no">'+window.__esc(x)+'</div>').join('');
     return;}
   toast('Calibration saved');};
 window.__esc=s=>String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
 // Shared PASS/FAIL card for the Test Capacity Calibration action (used
 // by the Calibrate tab and the wizard's cap_bar detail page).
 window.__capTestCard=function(r){r=r||{};const E2=window.__esc;
   let h='<div class="detrow">'+(r.ok?'<span class="det-ok"><b>PASS ✓</b></span>':'<span class="det-no"><b>FAIL ✗</b></span>')+'</div>';
   if(r.right&&r.left)h+='<div class="detrow">Right tip ('+E2(r.right[0])+', '+E2(r.right[1])+') · left tip ('+E2(r.left[0])+', '+E2(r.left[1])+') · tips '+E2(r.width)+' px apart · stored width '+E2(r.stored_width!=null?r.stored_width:r.width)+' px</div>';
   if(r.tip_hex)h+='<div class="detrow"><span class="detsw" style="background:'+E2(r.tip_hex)+'"></span>Right-tip reads <b>'+E2(r.tip_hex)+'</b> → '+(r.tip_yellow?'<span class="det-ok">gold ✓</span>':'<span class="det-no">not gold</span>')+'</div>';
   if(typeof r.fill_frac==='number')h+='<div class="detrow">Fill: <b>'+Math.round(r.fill_frac*100)+'%</b> of the runtime band reads yellow</div>';
   h+=((r.reasons)||[]).map(x=>'<div class="detrow det-no">'+E2(x)+'</div>').join('');
   if(r.preview)h+='<div class="detrow"><img src="'+r.preview+'" alt="annotated capacity bar crop" style="max-width:100%;image-rendering:pixelated;border-radius:6px"></div>';
   if(!r.ok)h+='<div class="detrow"><button type="button" class="btn2" id="capRecal">Recalibrate right end</button></div>';
   return h;};
 (function(){const b=document.getElementById('capTest');if(!b)return;
   b.onclick=async()=>{const o=document.getElementById('capTestOut');if(!o)return;
     o.innerHTML='<div class="detrow">testing against a fresh screenshot…</div>';
     let r=null;try{r=await window.pywebview.api.test_capacity();}catch(e){r={ok:false,reasons:[String(e)]};}
     o.innerHTML=window.__capTestCard(r);
     const rb=o.querySelector('#capRecal');
     if(rb)rb.onclick=()=>{const cb=document.querySelector('.calbtn[data-pkey="CAP_FULL_PIXEL"]');if(cb)cb.click();};};})();
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
     box.innerHTML='<div class="detrow"><b>OCR lines:</b> '+(lines.length?lines.map(x=>'\u201c'+x+'\u201d').join(' · '):'(nothing, widen/aim the region while a find is showing)')+'</div>';};})();
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
 async function renderCueCaps(){const box=$('#cuecap');if(!box)return;
   let st={cues:{}};try{st=await window.pywebview.api.cue_mask_status();}catch(_){}
   const adv=$('#advcue');if(adv)adv.checked=!!st.advanced;
   const co=$('#cueonly');if(co)co.checked=!!st.masks_only;
   const NAMES={PAN:'Pan (in water)',DEPOSIT:'Collect Deposit (on land)',SHAKE:'Shake'};
   box.innerHTML='<div class="cuegallery">'+Object.keys(NAMES).map(cue=>{const c=(st.cues&&st.cues[cue])||{};
     return '<div class="cuecard" data-cue="'+cue+'"><div class="cuecardh">'+NAMES[cue]+'</div>'
       +(c.has?('<img class="cuethumb" src="'+c.preview+'" alt="">'):'<div class="cuethumb none">not captured</div>')
       +'<div class="cuecardf">'+(c.has?('<span class="ok">'+c.px+' px</span>'):'<span>\u2014</span>')
       +'<span class="grow"></span>'
       +'<button type="button" class="btn2 cuecapb">'+(c.has?'Re-do':'Capture')+'</button>'
       +(c.has?'<button type="button" class="btn2 cueclr">Clear</button>':'')+'</div></div>';}).join('')+'</div>';
   box.querySelectorAll('.cuecard').forEach(row=>{const cue=row.dataset.cue;
     row.querySelector('.cuecapb').onclick=async()=>{const th=parseInt(($('#cuethresh')||{}).value||'160',10);
       let r;try{r=await window.pywebview.api.start_cue_mask_capture(cue,th);}catch(_){r={ok:false,error:'could not open'};}
       if(r&&!r.ok)toast(r.error||'Could not open capture');else toast('Click the cue word, then Confirm.');};
     const clr=row.querySelector('.cueclr');
     if(clr)clr.onclick=async()=>{await window.pywebview.api.clear_cue_mask(cue);toast('Cleared '+cue);renderCueCaps();};});}
 window.renderCueCaps=renderCueCaps;
 (function(){const a=$('#advcue');if(a)a.onchange=async()=>{await window.pywebview.api.set_advanced_cues(a.checked);};
   const co=$('#cueonly');if(co)co.onchange=async()=>{await window.pywebview.api.set_cue_masks_only(co.checked);};})();
 (function(){const STEPS=[
   {cue:'PAN',title:'Pan cue (in the water)',tip:'Stand in the WATER so the white \u201cPan\u201d prompt shows, then Capture \u2014 a view opens over the game; click the word and Confirm.'},
   {cue:'DEPOSIT',title:'Collect Deposit cue (on land)',tip:'Step onto LAND so \u201cCollect Deposit\u201d shows, then Capture \u2014 click the word in the view and Confirm.'},
   {cue:'SHAKE',title:'Shake cue',tip:'Begin a SHAKE so the \u201cShake\u201d prompt shows, then Capture \u2014 click the word in the view and Confirm.'}];
   let i=0;const g=id=>document.getElementById(id);
   function show(){const s=STEPS[i];const w=g('cuewiz');if(!w)return;w.style.display='block';
     g('cwstepn').textContent='Step '+(i+1)+' of '+STEPS.length;
     g('cwtitle').textContent=s.title;g('cwtip').textContent=s.tip;
     const im=g('cwprev');im.style.display='none';im.src='';g('cwph').style.display='';
     g('cwph').textContent='Do the step in-game, then Capture (a full-screen view opens over the game).';g('cwstat').textContent='';
     g('cwprevb').style.display=i>0?'':'none';g('cwnext').textContent=(i===STEPS.length-1)?'Done':'Next \u203a';
     const c=g('cwcap');c.disabled=false;c.textContent='Capture';}
   const bt=g('cuewizbtn');if(bt)bt.onclick=()=>{i=0;show();};
   const cl=g('cwclose');if(cl)cl.onclick=()=>{g('cuewiz').style.display='none';renderCueCaps();};
   const cap=g('cwcap');if(cap)cap.onclick=async()=>{const th=parseInt((g('cuethresh')||{}).value||'160',10);
     let r;try{r=await window.pywebview.api.start_cue_mask_capture(STEPS[i].cue,th);}catch(_){r={ok:false,error:'could not open'};}
     if(r&&!r.ok){g('cwph').style.display='';g('cwph').textContent=r.error||'Could not open the capture view.';g('cwstat').textContent='';}
     else{g('cwph').style.display='';g('cwph').textContent='A full-screen view opened over the game. Click the '+STEPS[i].cue+' cue word, check the green box, then Confirm. Back here, hit Next.';g('cwstat').textContent='';}};
   const nx=g('cwnext');if(nx)nx.onclick=()=>{if(i>=STEPS.length-1){g('cuewiz').style.display='none';renderCueCaps();return;}i++;show();};
   const pv=g('cwprevb');if(pv)pv.onclick=()=>{if(i>0){i--;show();}};})();
 $('#importcal').onclick=()=>$('#importfile').click();
 $('#importfile').onchange=async ev=>{const f=ev.target.files[0];if(!f)return;
   const text=await f.text();let r;try{r=await window.pywebview.api.import_calibration(text);}catch(_){r={ok:false,error:'could not read file'};}
   if(r&&r.ok){setPixels(r.pixels||{});setColors(r.colors||{});toast('Imported calibration \u2713');}else{toast('Import failed: '+((r&&r.error)||'invalid'));}
   ev.target.value='';};
 // presets
 $('#pv1').onclick=()=>preset(V1,'v1 fast 1-dig'); $('#pv2').onclick=()=>preset(V2,'v2 multi-dig'); $('#pv3').onclick=()=>preset(GEODE,'v3 geode'); $('#pdef').onclick=()=>preset(DEF,'Defaults');
 (function(){
   var uStack=[],rStack=[],base=null,applying=false;
   function readAll(){try{return JSON.stringify(collect());}catch(e){return null;}}
   function commit(){if(applying)return;var now=readAll();if(now==null)return;if(base===null){base=now;return;}if(now===base)return;uStack.push(base);if(uStack.length>300)uStack.shift();rStack.length=0;base=now;}
   function resetHist(){base=readAll();uStack.length=0;rStack.length=0;}
   function applyState(json){applying=true;try{setVals(JSON.parse(json));}catch(e){}applying=false;base=json;}
   function doUndo(){if(!uStack.length){toast('Nothing to undo');return;}rStack.push(base);applyState(uStack.pop());toast('Undo');}
   function doRedo(){if(!rStack.length){toast('Nothing to redo');return;}uStack.push(base);applyState(rStack.pop());toast('Redo');}
   window.__undoCommit=commit;window.__undoReset=resetHist;
   document.addEventListener('change',function(e){var t=e.target;if(t&&((t.dataset&&t.dataset.key)||(t.classList&&t.classList.contains('crng'))))commit();});
   document.addEventListener('keydown',function(e){
     if(!(e.metaKey||e.ctrlKey))return;
     var k=(e.key||'').toLowerCase();if(k!=='z'&&k!=='y')return;
     var ae=document.activeElement;
     if(ae&&/^(input|textarea)$/i.test(ae.tagName)&&!(ae.dataset&&ae.dataset.key))return;
     if(k==='z'&&!e.shiftKey){e.preventDefault();doUndo();}
     else if(k==='y'||(k==='z'&&e.shiftKey)){e.preventDefault();doRedo();}
   });
   setTimeout(resetHist,1400);
 })();
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
 $('#savekeys').onclick=async()=>{try{await window.pywebview.api.save_hotkeys(hotkeys);toast('Keybinds saved, Stop & Start the macro to apply');}catch(e){toast('Save failed');}};
 $('#saverelics').onclick=async()=>{const n=await window.pywebview.api.save_relics(collectRelics(),$('#relicsMaster').checked);toast('Saved '+n+' relic(s)');};
 $('#startbtn').onclick=async()=>{
   const r=await window.pywebview.api.launch(collect(),collectRelics(),$('#relicsMaster').checked);
   if(r==='no-studio-build'){toast('STUDIO BUILD has no build selected — pick one on the Studio tab.');
     const tb=document.querySelector('.tab[data-tab="studio"]');if(tb)tb.click();return;}
   if(r==='no-studio-script'){toast('STUDIO SCRIPT has no script selected — pick one on the Script tab.');
     const tb=document.querySelector('.tab[data-tab="script"]');if(tb)tb.click();return;}
   if(r==='classic-with-active-build'){toast('A Studio entry is still active — switch to its Studio mode, or Reset Studio in Settings.');return;}
   if(r==='mode-kind-mismatch'){toast('The active entry does not match the selected mode — re-pick it on its own tab.');
     if(window.modeRefresh)modeRefresh();return;}
   if(typeof r==='string'&&r.indexOf('perm:')===0){
     toast('Start is disabled until macOS grants: '+r.slice(5).replace(/_/g,' ')+' — opening the Trust Center.');
     const tb=document.querySelector('.tab[data-tab="trust"]');if(tb)tb.click();return;}
   if(typeof r==='string'&&r.indexOf('cal:')===0){
     toast('Start is disabled: required calibration needs attention ('+r.slice(4).replace(/_/g,' ')+') — opening the Calibrate tab.');
     const cb=document.querySelector('.tab[data-tab="cal"]');if(cb)cb.click();return;}
   if(r!=='launched'&&r!=='already running'){toast(r||'Could not start.');return;}
   setRunning(true);toast('Launched, Ctrl+K to start');};
 $('#stopbtn').onclick=async()=>{await window.pywebview.api.stop();setRunning(false);};
 // ---- Studio: the script library on the tab + the Run-tab mode selector ----
 // Under a Studio launch the ONE library is presented as two kind-scoped
 // views: builds on the Studio tab (STUDIO BUILD), scripts on the Script
 // tab (STUDIO SCRIPT). Standalone Lite keeps the single mixed grid.
 (function(){
   var grid=$('#stgrid');
   function stDate(ts){if(!ts)return 'never';return new Date(ts*1000).toLocaleDateString();}
   function renderGrid(el,list,slaunch,emptyHtml){
     if(!el)return;
     if(!list.length){el.innerHTML='<div class="stempty">'+emptyHtml+'</div>';return;}
     el.innerHTML='';
     list.forEach(function(s){
       var c=document.createElement('div');c.className='stcard'+(s.active?' active':'');
       c.innerHTML='<h3></h3><div class="stdesc"></div><div class="stmeta"></div>'+
         '<div class="strow">'+
         (slaunch
           ?'<button type="button" class="btn2 stedit" title="Open it in Prospector Studio, where authoring lives">Open in Prospector Studio</button>'
           :'<button type="button" class="btn2 stedit" title="Open this script in the Studio editor">Open</button>')+
         '<button type="button" class="btn2 stact"></button>'+
         '<button type="button" class="btn2 strun2" title="Set active and start the macro">Run</button>'+
         '<button type="button" class="btn2 stdup">Duplicate</button>'+
         '<button type="button" class="btn2 stexp" title="Save as a .ppscript file to share">Export</button>'+
         '<button type="button" class="btn2 stdel" title="Delete this script">✕</button></div>';
       c.querySelector('h3').innerHTML=_esc(s.name)+(s.active?' <span class="stchip">active</span>':'')+
         (s.issues?' <span class="stchip issues">'+s.issues+' to fix</span>':'');
       c.querySelector('.stdesc').textContent=s.description||'No description yet.';
       c.querySelector('.stmeta').textContent=s.blocks+' block'+(s.blocks===1?'':'s')+' · edited '+stDate(s.updated)
         +(s.caps&&s.caps.length?(' · uses: '+s.caps.join(', ')):'');
       c.querySelector('.stedit').onclick=function(){
         if(slaunch)window.pywebview.api.studio_open_in_studio(null,s.name);
         else window.pywebview.api.studio_edit(s.name);};
       var act=c.querySelector('.stact');act.textContent=s.active?'Deactivate':'Set active';
       act.title=s.active?'Hand control back to the built-in modes':'Make this the mode the Start button runs';
       act.onclick=async function(){var r2;
         try{r2=await window.pywebview.api.studio_set_active(s.active?'':s.name);}catch(e){r2=null;}
         if(r2&&r2.ok)toast(s.active?'Back to the built-in modes':'"'+s.name+'" is now the active mode');
         else toast((r2&&r2.error)||'Could not set it active');
         stRefresh();if(window.modeRefresh)window.modeRefresh();};
       c.querySelector('.strun2').onclick=async function(){var r2;
         try{r2=await window.pywebview.api.studio_run(s.name);}catch(e){r2=null;}
         if(r2&&r2.ok){setRunning(true);toast('Running "'+s.name+'". Click into Roblox; Esc stops.');}
         else toast((r2&&r2.error)||'Could not start');
         stRefresh();if(window.modeRefresh)window.modeRefresh();};
       c.querySelector('.stdup').onclick=async function(){
         try{await window.pywebview.api.studio_duplicate(s.name);}catch(e){}
         stRefresh();};
       c.querySelector('.stexp').onclick=async function(){var r2;
         try{r2=await window.pywebview.api.studio_export(s.name);}catch(e){r2=null;}
         if(r2&&r2.ok)toast('Exported. Send the .ppscript file to a friend.');
         else if(r2&&!r2.cancelled)toast((r2&&r2.error)||'Export failed');};
       var del=c.querySelector('.stdel');
       del.onclick=async function(){if(!del.dataset.arm){del.dataset.arm='1';del.textContent='sure?';
           setTimeout(function(){del.dataset.arm='';del.textContent='✕';},2500);return;}
         try{await window.pywebview.api.studio_delete(s.name);}catch(e){}
         toast('Deleted "'+s.name+'"');stRefresh();};
       el.appendChild(c);});
   }
   window.stRefresh=async function(){
     var r;try{r=await window.pywebview.api.studio_list();}catch(e){r=null;}
     if(r&&!r.ok)r=null;
     var all=r?r.scripts:[];
     var slaunch=document.body.classList.contains('studiolaunch');
     var sel=$('#scriptsel');
     if(sel){var cur=r?r.active:'';
       sel.innerHTML='<option value="">Built-in modes</option>'+(all.map(function(s){
         return '<option value="'+_esc(s.name)+'"'+(s.active?' selected':'')+'>Script: '+_esc(s.name)+'</option>';}).join(''));
       var note=$('#scriptnote');
       if(note)note.textContent=cur?('"'+cur+'" runs instead of the built-in mode toggles.'):'';}
     renderGrid(grid,slaunch?all.filter(function(s){return s.kind!=='script';}):all,slaunch,
       slaunch?'<b>No Studio Builds yet.</b><br>Author one in Prospector Studio and press Run — it lands here automatically.'
              :'<b>No scripts yet.</b><br>Open Studio to build your first one from a template, or import a friend\'s .ppscript file.');
     if(slaunch)renderGrid($('#scgrid'),all.filter(function(s){return s.kind==='script';}),slaunch,
       '<b>No Studio Scripts yet.</b><br>Create a Script project in Prospector Studio and press Run — it appears here automatically.');
     if(window.schdrRefresh)window.schdrRefresh();
     if(window.paramsRefresh)window.paramsRefresh();
   };
   var so=$('#stopen');if(so)so.onclick=function(){window.pywebview.api.open_studio_window();};
   var sn=$('#stnew');if(sn)sn.onclick=function(){window.pywebview.api.studio_new();};
   var si=$('#stimport');if(si)si.onclick=async function(){var r;
     try{r=await window.pywebview.api.studio_import_dialog();}catch(e){r=null;}
     if(r&&r.ok)toast('Imported "'+r.name+'"'+(r.problems&&r.problems.length?
       '. Open it in Studio to fix '+r.problems.length+' thing(s) before it can run.':''));
     else if(r&&!r.cancelled)toast((r&&r.error)||'Import failed');
     stRefresh();};
   var ss=$('#scriptsel');if(ss)ss.onchange=async function(){var v=ss.value,r;
     try{r=await window.pywebview.api.studio_set_active(v);}catch(e){r=null;}
     if(r&&r.ok)toast(v?('"'+v+'" is now the active mode'):'Back to the built-in modes');
     else toast((r&&r.error)||'Could not set it active');
     stRefresh();};
   document.querySelectorAll('.tab[data-tab="studio"],.tab[data-tab="script"],.tab[data-tab="run"]').forEach(function(t){
     t.addEventListener('click',function(){setTimeout(window.stRefresh,60);});});
   stRefresh();
 })();
 // ---- Studio-launch: top-level CLASSIC | STUDIO BUILD | STUDIO SCRIPT ----
 // Visible only when Prospector Studio launched this app (PP_STUDIO_LAUNCH).
 // The invariant is structural and server-owned: CLASSIC <=> no active
 // entry (the built-in cycle), STUDIO BUILD ("studio") <=> an active
 // build-kind entry, STUDIO SCRIPT ("script") <=> an active script-kind
 // entry. studio_mode() on the Python side validates, remembers
 // last_active, and refuses mid-run; launch() refuses any mismatch. Each
 // mode shows only its own surfaces (body.mode-* CSS) plus the shared ones.
 (function(){
   var strip=$('#modestrip');if(!strip)return;
   function mconfirm(title,body,cb){var m=$('#mcfm');if(!m){cb(window.confirm?window.confirm(title):true);return;}
     $('#mcfmtitle').textContent=title;$('#mcfmbody').textContent=body;
     m.classList.add('show');
     function done(v){m.classList.remove('show');document.removeEventListener('keydown',esc);cb(v);}
     function esc(e){if(e.key==='Escape')done(false);}
     document.addEventListener('keydown',esc);
     $('#mcfmyes').onclick=function(){done(true);};
     $('#mcfmno').onclick=function(){done(false);};
     $('#mcfmyes').focus();}
   window.mconfirm=mconfirm;
   var MODES=['classic','studio','script'];
   var HOMETAB={classic:'run',studio:'studio',script:'script'};
   var MODE='classic',NEEDS=false;
   function paint(r){document.body.classList.add('studiolaunch');
     NEEDS=!!(r&&(r.needs_build||r.needs_script));
     MODES.forEach(function(m){
       document.body.classList.toggle('mode-'+m,MODE===m);
       var b=$('#mode_'+m);if(!b)return;
       b.classList.toggle('on',MODE===m);b.setAttribute('aria-selected',MODE===m?'true':'false');});
     var wrap=$('#stlaunch'),note=$('#slnote');
     if(wrap)wrap.style.display='flex';
     if(note)note.textContent=MODE==='studio'
       ?((r&&r.active)?('Studio build "'+r.active+'" runs when you press Start.')
                      :'STUDIO BUILD is selected but no build is active \u2014 pick one on the Studio tab.')
       :MODE==='script'
       ?((r&&r.active)?('Studio Script "'+r.active+'" runs when you press Start.')
                      :'STUDIO SCRIPT is selected but no script is active \u2014 pick one on the Script tab.')
       :'The classic cycle runs when you press Start.';
     var cur=document.querySelector('.tab.active');
     if(cur&&getComputedStyle(cur).display==='none'){
       var tb=document.querySelector('.tab[data-tab="'+HOMETAB[MODE]+'"]');
       if(tb)tb.click();}
     // the live step card serves both Studio modes: label it honestly
     var sl2=document.querySelector('#rsc_step .lbl');
     if(sl2)sl2.textContent=(MODE==='studio')?'build step':'current step';
     if(window.sthdrRefresh)window.sthdrRefresh();
     if(window.schdrRefresh)window.schdrRefresh();}
   async function refresh(){var st;try{st=await window.pywebview.api.get_state();}catch(e){return;}
     if(!st||!st.studio_launch)return;
     strip.style.display='flex';
     var r;try{r=await window.pywebview.api.studio_mode();}catch(e){r=null;}
     if(r&&r.ok)MODE=r.mode;else MODE=st.studio_mode||'classic';
     paint(r);}
   async function switchTo(m){
     if(m===MODE&&!NEEDS)return;
     var st;try{st=await window.pywebview.api.get_state();}catch(e){st=null;}
     if(st&&st.running){
       mconfirm('Stop the run and switch?',
         'Switching modes stops the current run first. Input is released safely \u2014 nothing stays held.',
         async function(okv){if(!okv)return;
           try{await window.pywebview.api.stop();}catch(e){}
           if(window.setRunning)setRunning(false);
           setTimeout(function(){switchTo(m);},600);});
       return;}
     var r;try{r=await window.pywebview.api.studio_mode(m);}catch(e){r=null;}
     if(!r||!r.ok){toast((r&&r.error)||'Could not switch');return;}
     MODE=r.mode;paint(r);
     // The switch parks/restores AutoPan Tracking server-side; mirror the
     // result into the (possibly hidden) input so a later Start's collect()
     // cannot write a stale value back over it.
     if(typeof r.tracker!=='undefined'){
       var tin=document.querySelector('[data-key="TRACKER_MODE"]');
       if(tin)tin.checked=!!r.tracker;}
     if(m==='studio'&&r.needs_build){
       var tb=document.querySelector('.tab[data-tab="studio"]');if(tb)tb.click();
       toast('STUDIO BUILD runs the active build \u2014 pick one here.');}
     else if(m==='script'&&r.needs_script){
       var tb2=document.querySelector('.tab[data-tab="script"]');if(tb2)tb2.click();
       toast('STUDIO SCRIPT runs the active script \u2014 pick one here.');}
     else toast(m==='studio'?('STUDIO BUILD \u2014 "'+(r.active||'')+'" is the active build.')
               :m==='script'?('STUDIO SCRIPT \u2014 "'+(r.active||'')+'" is the active script.')
               :'CLASSIC \u2014 the built-in cycle is in charge.');
     if(window.stRefresh)window.stRefresh();}
   $('#mode_classic').onclick=function(){switchTo('classic');};
   $('#mode_studio').onclick=function(){switchTo('studio');};
   var msb=$('#mode_script');if(msb)msb.onclick=function(){switchTo('script');};
   var ss=$('#scriptsel');if(ss)ss.addEventListener('change',function(){setTimeout(refresh,150);});
   window.modeRefresh=refresh;window.slRefresh=refresh;
   window.addEventListener('pywebviewready',function(){setTimeout(refresh,120);});
   setTimeout(refresh,900);refresh();
 })();
 // ---- STUDIO SCRIPT: the modern script surface (Studio launch only) ------
 // Header (name / revision / validation / caps) + live run card fed by the
 // engine's script.block + script.hud events. The legacy embedded editor
 // plays no part in this mode.
 (function(){
   var STATE='stopped';
   function setTxt(id,t){var el=$(id);if(el)el.textContent=t;}
   window.scrState=function(s){STATE=s;setTxt('#rsc_state',s);setTxt('#ssc_state',s);};
   window.setScriptStep=function(p){if(!p)return;
     var t=(p.type||'?')+' \u00b7 '+(p.id||'?')
       +(typeof p.pass!=='undefined'?(' \u00b7 pass '+p.pass):'')
       +(typeof p.n!=='undefined'?(' \u00b7 step '+p.n):'');
     var lbl=document.body.classList.contains('mode-studio')?'build step':'current step';
     ['#rsc_step','#ssc_step'].forEach(function(id){var el=$(id);if(el)
       el.innerHTML='<span class="lbl">'+lbl+'</span> '+_esc(t);});};
   window.setScriptHud=function(t){setTxt('#rsc_hud',t||'');setTxt('#ssc_hud',t||'');};
   window.schdrRefresh=async function(){
     var h=$('#schdr');if(!h)return;
     var st;try{st=await window.pywebview.api.get_state();}catch(e){return;}
     if(!st||!st.studio_launch)return;
     var r;try{r=await window.pywebview.api.studio_list();}catch(e){r=null;}
     if(r&&r.ok===false)r=null;
     var rows=(r&&r.scripts||[]).filter(function(s){return s.kind==='script';});
     var row=rows.filter(function(s){return s.active;})[0]
        ||rows.filter(function(s){return s.name===(st.studio_script||'');})[0];
     // the Run-tab card belongs to whichever Studio mode is active:
     // mode-studio's build header owns it there (sthdrRefresh)
     var msc=document.body.classList.contains('mode-script');
     if(!row){h.style.display='none';setTxt('#ssc_name','');if(msc)setTxt('#rsc_name','');return;}
     var rev='';try{var pi=await window.pywebview.api.studio_push_info();
       if(pi&&pi.ok&&pi.name===row.name)rev=pi.rev||'';}catch(e){}
     h.style.display='block';
     h.innerHTML='<div class="sth-top"><span class="sth-name"></span>'
       +(row.active?'<span class="sth-badge on">active</span>':'<span class="sth-badge">not active</span>')
       +'<span class="sth-badge">Studio Script</span>'
       +(rev?'<span class="sth-badge" title="Content revision \u2014 matches the revision shown in Prospector Studio">rev '+_esc(rev)+'</span>':'')
       +(row.issues?'<span class="sth-badge">'+row.issues+' to fix</span>'
                   :'<span class="sth-badge on">valid \u2014 ready to run</span>')
       +'</div><div class="sth-meta"></div>'
       +'<div class="sth-btns"><button type="button" class="btn2" id="sch_open">Open in Prospector Studio</button>'
       +'<button type="button" class="btn2" id="sch_reload">Reload from Studio</button></div>';
     h.querySelector('.sth-name').textContent=row.name;
     var meta=[];meta.push(row.blocks+' step'+(row.blocks===1?'':'s'));
     if(row.caps&&row.caps.length)meta.push('uses: '+row.caps.join(', '));
     if(row.description)meta.push(row.description);
     h.querySelector('.sth-meta').textContent=meta.join(' \u00b7 ');
     setTxt('#ssc_name',row.name);if(msc)setTxt('#rsc_name',row.name);
     var rv=$('#rsc_rev');if(rv&&msc){rv.style.display=rev?'':'none';if(rev)rv.textContent='rev '+rev;}
     $('#sch_open').onclick=function(){window.pywebview.api.studio_open_in_studio(null,row.name);};
     $('#sch_reload').onclick=async function(){
       var s2;try{s2=await window.pywebview.api.get_state();}catch(e){s2=null;}
       if(s2&&s2.running){toast('Stop the run first \u2014 reloading replaces the script.');return;}
       if(window.stRefresh)window.stRefresh();
       window.schdrRefresh();toast('Script library reloaded from disk.');};
   };
   window.addEventListener('pywebviewready',function(){setTimeout(window.schdrRefresh,180);});
   setTimeout(window.schdrRefresh,980);
 })();
 // ---- dynamic settings from the pushed graph (Studio modes) --------------
 // Rendered from the active document's DECLARED parameters (settings.params,
 // written by Prospector Studio at publish). Values edit the stored script
 // and re-push it; the engine binds config at run start, so every change is
 // honestly labeled "applies at the next start".
 (function(){
   var PINKEY='pps_param_pins';
   function pins(){try{return JSON.parse(localStorage.getItem(PINKEY))||[];}catch(e){return [];}}
   function setPins(l){try{localStorage.setItem(PINKEY,JSON.stringify(l));}catch(e){}}
   function fmtv(p,v){return p.type==='number'?String(v):p.type==='bool'?(v?'on':'off'):String(v);}
   function row(p){
     var d=document.createElement('div');d.className='sprow';
     var pinned=pins().indexOf(p.name)>=0;
     var ovr=JSON.stringify(p.current)!==JSON.stringify(p.default);
     var pin=document.createElement('button');pin.type='button';
     pin.className='sp-pin'+(pinned?' on':'');pin.textContent='★';
     pin.title=pinned?'Unpin':'Pin to the top';
     pin.onclick=function(){var l=pins();
       if(pinned)l=l.filter(function(n){return n!==p.name;});else l.push(p.name);
       setPins(l);paramsRefresh();};
     d.appendChild(pin);
     var lab=document.createElement('div');lab.className='sp-lab';
     lab.innerHTML='<div class="sp-name"></div>'+(p.desc?'<div class="sp-desc"></div>':'');
     lab.querySelector('.sp-name').textContent=p.label;
     if(p.desc)lab.querySelector('.sp-desc').textContent=p.desc;
     d.appendChild(lab);
     var st=document.createElement('span');st.className='sp-state'+(ovr?' ovr':'');
     st.textContent=ovr?'overridden':'default';
     st.title=ovr?('Published default: '+fmtv(p,p.default)+'. Click to reset.'):'At the published default';
     if(ovr){st.style.cursor='pointer';st.onclick=function(){apply(p,p.default);};}
     d.appendChild(st);
     var inp;
     if(p.type==='bool'){inp=document.createElement('input');inp.type='checkbox';inp.checked=!!p.current;
       inp.onchange=function(){apply(p,inp.checked);};}
     else if(p.type==='choice'){inp=document.createElement('select');
       (p.options||[]).forEach(function(o){var op=document.createElement('option');
         op.value=o;op.textContent=o;if(o===p.current)op.selected=true;inp.appendChild(op);});
       inp.onchange=function(){apply(p,inp.value);};}
     else{inp=document.createElement('input');
       inp.type=p.type==='number'?'number':'text';
       if(typeof p.min!=='undefined')inp.min=p.min;
       if(typeof p.max!=='undefined')inp.max=p.max;
       if(typeof p.step!=='undefined')inp.step=p.step;
       inp.value=p.current==null?'':p.current;
       inp.onchange=function(){apply(p,p.type==='number'?Number(inp.value):inp.value);};}
     d.appendChild(inp);
     var un=document.createElement('span');un.className='sp-unit';un.textContent=p.unit||'';
     d.appendChild(un);
     return d;
   }
   async function apply(p,val){
     var r;try{r=await window.pywebview.api.studio_set_param(p.name,val);}catch(e){r=null;}
     if(r&&r.ok)toast('"'+p.label+'" = '+fmtv(p,r.value)
       +(r.running?' — applies when the NEXT run starts':' — applies at the next start'));
     else toast((r&&r.error)||'Could not change it','err');
     paramsRefresh();
   }
   window.paramsRefresh=async function(){
     var r;try{r=await window.pywebview.api.studio_params();}catch(e){r=null;}
     if(!r||!r.ok)return;
     var scriptKind=r.kind==='script';
     var wrap=$(scriptKind?'#spwrap':'#bpwrap'),grid=$(scriptKind?'#spgrid':'#bpgrid');
     var other=$(scriptKind?'#bpwrap':'#spwrap');
     if(other)other.style.display='none';
     if(!wrap||!grid)return;
     if(!r.params||!r.params.length){wrap.style.display='none';return;}
     wrap.style.display='block';
     var box=$(scriptKind?'#spsearch':'#bpsearch');
     var q=(box&&box.value||'').toLowerCase();
     var list=r.params.filter(function(p){
       return !q||((p.label+' '+p.name+' '+p.group).toLowerCase().indexOf(q)>=0);});
     var pn=pins(),groups={};
     list.forEach(function(p){var g=pn.indexOf(p.name)>=0?'★ Pinned':p.group;
       (groups[g]=groups[g]||[]).push(p);});
     grid.innerHTML='';
     Object.keys(groups).sort(function(a,b){
       return a==='★ Pinned'?-1:b==='★ Pinned'?1:(a<b?-1:1);}).forEach(function(g){
       var h=document.createElement('div');h.className='spgroup';h.textContent=g;grid.appendChild(h);
       groups[g].forEach(function(p){grid.appendChild(row(p));});});
   };
   ['#spsearch','#bpsearch'].forEach(function(id){var el=$(id);
     if(el)el.addEventListener('input',function(){window.paramsRefresh();});});
   document.querySelectorAll('.tab[data-tab="studio"],.tab[data-tab="script"]').forEach(function(t){
     t.addEventListener('click',function(){setTimeout(window.paramsRefresh,80);});});
   window.addEventListener('pywebviewready',function(){setTimeout(window.paramsRefresh,220);});
   setTimeout(window.paramsRefresh,1050);
 })();
 // ---- Studio-launch: build header on the Studio tab + settings resets ----
 (function(){
   window.sthdrRefresh=async function(){var h=$('#sthdr');if(!h)return;
     var st;try{st=await window.pywebview.api.get_state();}catch(e){return;}
     if(!st||!st.studio_launch){h.style.display='none';return;}
     var r;try{r=await window.pywebview.api.studio_list();}catch(e){r=null;}
     if(r&&r.ok===false)r=null;
     var active=(r&&r.active)||'';var pushed=st.studio_script||'';
     var name=active||pushed;
     if(!name){h.style.display='none';return;}
     // The list row carries everything the header needs (block count +
     // validation issue count) and works for v2 Studio-made scripts, which
     // studio_get deliberately refuses to hand to the inline editor.
     var row=(r&&r.scripts||[]).filter(function(s){return s.name===name;})[0];
     var steps=row?row.blocks:null;
     var verdict=row?(row.issues?(row.issues+' to fix \u2014 open it in Prospector Studio')
                                :'valid \u2014 ready to run'):'';
     var rev='';try{var pi=await window.pywebview.api.studio_push_info();
       if(pi&&pi.ok&&pi.name===name)rev=pi.rev||'';}catch(e){}
     h.style.display='block';
     h.innerHTML='<div class="sth-top"><span class="sth-name"></span>'
       +(active?'<span class="sth-badge on">active</span>':'<span class="sth-badge">not active</span>')
       +(pushed&&pushed===name?'<span class="sth-badge">pushed from Prospector Studio</span>':'')
       +(rev?'<span class="sth-badge" title="Build revision \u2014 matches the revision shown in Prospector Studio">rev '+_esc(rev)+'</span>':'')
       +'</div><div class="sth-meta"></div>'
       +'<div class="sth-btns"><button type="button" class="btn2" id="sth_reload">Reload from Studio</button>'
       +'<button type="button" class="btn2" id="sth_open">Open in Prospector Studio</button></div>';
     h.querySelector('.sth-name').textContent=name;
     var meta=[];if(steps!==null)meta.push(steps+' step'+(steps===1?'':'s'));
     if(verdict)meta.push(verdict);
     h.querySelector('.sth-meta').textContent=meta.join(' \u00b7 ');
     // STUDIO BUILD: the Run-tab live step card shows this build's name and
     // revision (the engine emits script.block for build runs too)
     if(document.body.classList.contains('mode-studio')){
       var rn=$('#rsc_name');if(rn)rn.textContent=name;
       var rv2=$('#rsc_rev');if(rv2){rv2.style.display=rev?'':'none';
         if(rev)rv2.textContent='rev '+rev;}}
     $('#sth_reload').onclick=async function(){
       var s2;try{s2=await window.pywebview.api.get_state();}catch(e){s2=null;}
       if(s2&&s2.running){toast('Stop the run first \u2014 reloading replaces the build.');return;}
       if(window.stRefresh)window.stRefresh();
       window.sthdrRefresh();toast('Build library reloaded from disk.');};
     $('#sth_open').onclick=async function(){
       var r2;try{r2=await window.pywebview.api.studio_open_in_studio('');}catch(e){r2=null;}
       toast(r2&&r2.ok?'Asked Prospector Studio to open this build.'
                      :((r2&&r2.error)||'Could not reach Prospector Studio.'));};};
   document.querySelectorAll('.tab[data-tab="studio"]').forEach(function(t){
     t.addEventListener('click',function(){setTimeout(window.sthdrRefresh,60);});});
   window.addEventListener('pywebviewready',function(){setTimeout(window.sthdrRefresh,150);});
   setTimeout(window.sthdrRefresh,950);
   // Settings ownership resets. Each group belongs to exactly one owner;
   // the server does the work and reports what changed. The page reloads
   // afterwards so every input shows the restored values.
   function doReset(group,label,body){
     if(!window.mconfirm)return;
     window.mconfirm('Reset '+label+' settings?',body,async function(okv){
       if(!okv)return;
       var inc=group==='shared'&&!!($('#rst_cal')&&$('#rst_cal').checked);
       var r;try{r=await window.pywebview.api.settings_reset(group,inc);}catch(e){r=null;}
       if(!r||!r.ok){toast((r&&r.error)||'Reset failed.');return;}
       if(group==='shared'){try{['pp_set_compact','pp_set_wood','pp_set_reduce'].forEach(function(k){localStorage.removeItem(k);});}catch(e){}}
       toast(label+' settings reset ('+((r.reset&&r.reset.length)||0)+' changed). Reloading\u2026');
       setTimeout(function(){location.reload();},700);});}
   var bc=$('#rst_classic');if(bc)bc.onclick=function(){doReset('classic','Classic',
     'The built-in cycle tuning goes back to shipped defaults: modes, dig/shake/walk timing, recovery and relics. Your saved builds, Studio scripts, calibration and history are untouched.');};
   var bs=$('#rst_studio');if(bs)bs.onclick=function(){doReset('studio','Studio',
     'Clears which build is active, the CLASSIC | STUDIO mode and editor flags. Your scripts themselves are NOT deleted.');};
   var bh=$('#rst_shared');if(bh)bh.onclick=function(){doReset('shared','Shared',
     'Notifications, auto-stop, window tracking, earnings tracking, keybinds and appearance go back to defaults. Calibration is cleared only if you ticked the box below.');};
 })();
 (function(){const b=$('#hudbtn');if(b)b.onclick=async()=>{
   try{const r=await window.pywebview.api.hud_toggle();
     toast(r==='shown'?'HUD on, drag it beside Roblox':'HUD hidden');}catch(e){}};})();
 (function(){const b=$('#pausebtn');if(b)b.onclick=async()=>{
   try{await window.pywebview.api.pause_toggle();}catch(e){}};})();
 // builds, a build saves ALL settings + relics; loading applies them all.
 // The dedicated Builds PAGE owns listing/search/sort/descriptions.
 async function loadBuild(name){if(!name)return;
   const e=await window.pywebview.api.load_build(name);if(!e)return;
   setVals(e); if(window.__undoCommit)window.__undoCommit(); setRelics(e.RELICS||[], e.RELICS_ENABLED); if(window.splash)window.splash('<b>'+_esc(name)+'</b> loaded');}
 let BLD={list:[],q:'',sort:'new'};
 function bldDate(ts){if(!ts)return '-';return new Date(ts*1000).toLocaleDateString();}
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
     d.textContent=BLD.list.length?'No builds match that search.':'No builds yet, save your current settings above.';
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
   })();
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
   const col={dig:'#a8794a',swalk:'#6ba1b5',glide:'#9bc07e',shake:'#caa06e',land:'#b58f6b'};
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
   if(tot)tot.textContent=', est '+(M.est/1000).toFixed(2)+'s · budget '+(M.cap/1000).toFixed(2)
     +'s · ≈'+Math.round(M.pph)+' pans/hr at est';
   const nt=document.getElementById('cygnotes');
   if(nt)nt.textContent=M.notes.join('  ·  ');
   const tip=document.getElementById('cygtip');
   svg.querySelectorAll('.cygseg').forEach(r=>{
     const s=M.segs[+r.dataset.i];
     r.addEventListener('mousemove',e=>{if(!tip)return;
       tip.style.display='block';
       tip.innerHTML='<b>'+s.name+'</b>, '+(s.lo===s.hi?Math.round(s.lo)+'ms'
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
 async function init(){const s=await window.pywebview.api.get_state();
   DEF=s.defaults;V1=s.v1;V2=s.v2;GEODE=s.geode||{};window._AB=s.autobuild||{};setVals(s.values);if(window.__undoReset)window.__undoReset();setRunning(s.running);
   setRelics(s.relics||[],s.relics_enabled);loadBuildsPage();setPixels(s.pixels||{});setColors(s.colors||{});setFR(s.fr||{});window.__pvfr=s.fr||{};setRegionPreviews(s.region_previews||{});markRegions(s.pixels||{});setHotkeys(s.hotkeys||{});renderCueCaps();checkCalHealth();if(!window._calhi){window._calhi=setInterval(checkCalHealth,8000);try{window.addEventListener('focus',checkCalHealth);}catch(e){}}
   loadHistory();
}
 (function(){const b=document.getElementById('testnotify');if(!b)return;
   b.onclick=async()=>{const out=document.getElementById('notifyout');
     const en=document.querySelector('[data-key="WEBHOOK_ENABLED"]');
     if(en&&!en.checked&&out){out.innerHTML='<div class="detrow">Turn on <b>Discord notifications</b> first.</div>';return;}
     if(out)out.innerHTML='<div class="detrow">sending…</div>';
     let r;try{r=await window.pywebview.api.test_webhook();}catch(e){if(out)out.innerHTML='<div class="detrow det-no">failed: '+e+'</div>';return;}
     if(out)out.innerHTML=r&&r.ok?('<div class="detrow det-ok">Sent to your webhook'+(r.user?(' as '+r.user):'')+'. If nothing arrives, re-copy the webhook URL from Discord and save it again.</div>'):('<div class="detrow det-no">'+((r&&r.error)||'failed')+'</div>');};})();
 (function(){const i=document.getElementById('whurl'),b=document.getElementById('whurlsave');if(!i||!b)return;
   const out=document.getElementById('whurlout');
   (async()=>{try{const r=await window.pywebview.api.webhook_get();if(r&&r.url)i.value=r.url;}catch(e){}})();
   b.onclick=async()=>{const v=(i.value||'').trim();
     if(v&&!/^https:\/\//i.test(v)){if(out)out.innerHTML='<div class="detrow det-no">The webhook URL must start with https://</div>';return;}
     let r={};try{r=await window.pywebview.api.webhook_set(v);}catch(e){r={ok:false,error:String(e)};}
     if(out)out.innerHTML=(r&&r.ok)?('<div class="detrow det-ok">'+(v?'Webhook saved.':'Webhook removed — notifications are fully off.')+'</div>'):('<div class="detrow det-no">'+((r&&r.error)||'failed')+'</div>');};})();
 window.addEventListener('pywebviewready',boot);
 if(window.pywebview&&window.pywebview.api)boot();

 // ---- splash + welcome ----
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
 let _welInfo=null;
 function welcomeShow(){genPaths();const g=document.getElementById('gate');if(g)g.classList.add('show');
   setTimeout(()=>{const c=document.getElementById('welGo');if(c)c.focus();},140);}
 function welcomeHide(){const g=document.getElementById('gate');if(g)g.classList.remove('show');}
 // The checkbox ALWAYS renders the stored preference -- never a template
 // default, never a forced value. Toggling persists immediately; a failed
 // save is shown and the box reverts (it never silently lies).
 function welSetChecked(v){const a=document.getElementById('welAgain');if(a)a.checked=!!v;}
 function welErr(msg){const e=document.getElementById('welAgainErr');
   if(e){e.textContent=msg||'';e.style.display=msg?'':'none';}}
 (function(){const a=document.getElementById('welAgain');if(!a)return;
   a.addEventListener('change',async()=>{const want=!!a.checked;welErr('');
     let r=null;try{r=await _api().welcome_set_always_show(want);}catch(e){r={ok:false,error:String(e)};}
     if(!r||!r.ok){a.checked=!want;
       welErr('Could not save this preference ('+((r&&r.error)||'bridge unavailable')+'). ['+((r&&r.error_code)||'PP-WEL-SAVE')+']');}});})();
 // The auto-skip checkbox follows the same render-stored-value +
 // revert-on-save-failure contract as welAgain.
 function welSkipSet(v){const a=document.getElementById('welSkipAuto');if(a)a.checked=!!v;}
 (function(){const a=document.getElementById('welSkipAuto');if(!a)return;
   a.addEventListener('change',async()=>{const want=!!a.checked;welErr('');
     let r=null;try{r=await _api().wizard_skip_pref(want);}catch(e){r={ok:false,error:String(e)};}
     if(!r||!r.ok){a.checked=!want;
       welErr('Could not save this preference ('+((r&&r.error)||'bridge unavailable')+'). ['+((r&&r.error_code)||'PP-SKIP-SAVE')+']');}});})();
 // The tutorial auto-open checkbox too (checked = auto_open true). Its
 // stored value comes from tutorial_state(), fetched on every gate render.
 async function welTutSync(){try{const t=await _api().tutorial_state();
   const a=document.getElementById('welTutAuto');if(a)a.checked=!(t&&t.auto_open===false);}catch(e){}}
 (function(){const a=document.getElementById('welTutAuto');if(!a)return;
   a.addEventListener('change',async()=>{const want=!!a.checked;welErr('');
     let r=null;try{r=await _api().tutorial_set_auto_open(want);}catch(e){r={ok:false,error:String(e)};}
     if(!r||!r.ok){a.checked=!want;
       welErr('Could not save this preference ('+((r&&r.error)||'bridge unavailable')+'). ['+((r&&r.error_code)||'PP-TUT-AUTO')+']');}});})();
 // WEL_EXPLICIT: true only when the user deliberately opened Welcome
 // (Tutorial menu). boot() never sets it. Explicit Welcome always routes
 // into the wizard for review; the boot path keeps its original behavior.
 // WEL_RESUME mirrors welcome_state().resume so Continue can reopen the
 // wizard at the right page (SETUP.resume falls back to 'trust', which is
 // also the review entry when setup is FINISHED).
 let WEL_EXPLICIT=false, WEL_RESUME='';
 // SESSION_SKIP: JS-only 'skip this time' flag -- nothing persists, next
 // launch routes normally.
 let SESSION_SKIP=false;
 function skipModalShow(){const m=document.getElementById('skipmodal');if(m)m.classList.add('show');}
 function skipModalHide(){const m=document.getElementById('skipmodal');if(m)m.classList.remove('show');}
 function _skipFinish(){skipModalHide();welcomeHide();
   const s=document.getElementById('setup');if(s)s.classList.remove('show');
   const r=document.getElementById('supReturn');if(r)r.classList.remove('show');
   _startApp();setTimeout(()=>{if(window.maybeStartTour)window.maybeStartTour();},900);}
 (function(){const w=(id,f)=>{const el=document.getElementById(id);if(el)el.addEventListener('click',f);};
   w('welSkip',()=>skipModalShow());
   w('supSkip',()=>skipModalShow());
   w('skipCancel',()=>skipModalHide());
   w('skipSession',async()=>{SESSION_SKIP=true;
     try{await _api().wizard_skip('session');}catch(e){} // logging only -- no persistence
     _skipFinish();});
   w('skipMark',async()=>{try{await _api().wizard_skip('mark_complete');}catch(e){}_skipFinish();});
   w('skipAuto',async()=>{try{await _api().wizard_skip('auto');}catch(e){}_skipFinish();});})();
 // Post-setup welcome actions: once setup is FINISHED the welcome screen
 // offers Review setup / Start tutorial / Trust Center. Showing the welcome
 // at every launch never re-runs permissions or calibration -- Continue just
 // opens the app. An EXPLICIT Welcome (menu) always reveals the full action
 // list and relabels Continue: it goes INTO the wizard, never straight out.
 function welActions(setupNeeded){
   const ex=WEL_EXPLICIT;
   const w=document.getElementById('welActions');if(w)w.style.display=(ex||!setupNeeded)?'':'none';
   const sh=(id,on)=>{const el=document.getElementById(id);if(el)el.style.display=on?'':'none';};
   sh('welContinue',ex);sh('welCal',ex);sh('welOpenApp',ex);
   const wr=document.getElementById('welReview');
   if(wr)wr.textContent=ex?'Review permissions':'Review setup';
   const g=document.getElementById('welGo');
   if(g)g.textContent=ex?'Continue through setup →':(setupNeeded?'Continue':'Open Prospector Lite');}
 window.openWelcome=async function(){
   WEL_EXPLICIT=true;
   let sn=_setupNeeded;
   try{const w=await _api().welcome_state();welSetChecked(w&&w.show_every_launch);
     welSkipSet(w&&w.skip_wizard_automatically);WEL_RESUME=(w&&w.resume)||'';
     sn=!!(w&&w.setup_needed);}catch(e){}
   welTutSync();
   welActions(sn);
   welcomeShow();};
 function welcomeFill(info){try{
   const v=document.getElementById('welVer');if(v)v.textContent='v'+(info.version||'');
   const s=document.getElementById('welSrc');if(s&&info.project_url)s.style.display='';
   const b=document.getElementById('welBuild');if(b){b.textContent=(info.name||'Prospector Lite')+' v'+(info.version||'')
     +(info.commit?(' · build '+info.commit):'')+(info.engine_fp?(' · engine '+String(info.engine_fp).slice(0,8)):'')
     +' · '+(info.platform||'');}
   const m=document.getElementById('welMigr');if(m&&info.migrated){m.textContent=info.migrated;m.style.display='';}
 }catch(e){}}
 function _startApp(){if(document.body.dataset.welinit==='1')return;document.body.dataset.welinit='1';
   init();if(window.maybeStartTour)setTimeout(window.maybeStartTour,900);}
 let _setupNeeded=false, _booted=false;
 function bootFail(err){splashHide();
   const g=document.getElementById('gate');if(g)g.classList.add('show');genPaths();
   const t=document.getElementById('welTitle');if(t)t.textContent='Startup problem';
   welErr('The app bridge did not answer ('+err+'). [PP-BOOT-BRIDGE] Close and reopen Prospector Lite; if it persists, use Export diagnostics from the Trust Center.');}
 async function boot(){if(_booted)return;_booted=true;
   let w=null,err='';try{w=await _api().welcome_state();}catch(e){err=String(e);}
   await new Promise(r=>setTimeout(r,650));
   if(!w){bootFail(err||'no response');return;}
   splashHide();
   _welInfo=(w&&w.info)||{};welcomeFill(_welInfo);_setupNeeded=!!(w&&w.setup_needed);
   welSetChecked(w&&w.show_every_launch);welSkipSet(w&&w.skip_wizard_automatically);welActions(_setupNeeded);
   const resume=(w&&w.resume)||'';WEL_RESUME=resume;
   // route comes from lite_onboarding.compute_startup_route -- the single
   // routing authority (welcome vs wizard_resume vs main).
   const route=(w&&w.route)||'';
   if(route==='welcome'){welTutSync();welcomeShow();}
   else if(route==='wizard_resume'&&window.SETUP){SETUP.resume(resume);}
   else{_startApp();}}
 (function(){const b=document.getElementById('welGo');if(!b)return;
   b.addEventListener('click',async()=>{
     let r=null;try{r=await _api().welcome_done();}catch(e){r={ok:false,error:String(e)};}
     if(r&&r.ok===false){welErr('Could not save setup progress ('+(r.error||'')+'). ['+(r.error_code||'PP-WEL-DONE')+']');}
     welcomeHide();
     // Explicit Welcome ALWAYS continues into the wizard for review,
     // whatever the completion state; the boot path keeps its behavior.
     if(WEL_EXPLICIT&&window.SETUP){SETUP.resume(WEL_RESUME);}
     else if(_setupNeeded&&window.SETUP){SETUP.open('trust');}else{_startApp();}});
   const wc=document.getElementById('welContinue');if(wc)wc.onclick=e=>{e.preventDefault();
     try{_api().welcome_done();}catch(_){}
     welcomeHide();if(window.SETUP)SETUP.resume(WEL_RESUME);};
   const wca=document.getElementById('welCal');if(wca)wca.onclick=e=>{e.preventDefault();
     welcomeHide();if(window.SETUP)SETUP.open('cal');};
   const wo=document.getElementById('welOpenApp');if(wo)wo.onclick=e=>{e.preventDefault();
     welcomeHide();_startApp();};
   const wr=document.getElementById('welReview');if(wr)wr.onclick=e=>{e.preventDefault();
     welcomeHide();if(window.SETUP)SETUP.open('trust');};
   const wt=document.getElementById('welTut');if(wt)wt.onclick=e=>{e.preventDefault();
     welcomeHide();_startApp();setTimeout(()=>{if(window.startTour)startTour('main');},450);};
   const wtc=document.getElementById('welTrustC');if(wtc)wtc.onclick=e=>{e.preventDefault();
     welcomeHide();_startApp();setTimeout(()=>{const t=document.querySelector('.tab[data-tab="trust"]');if(t)t.click();},200);};
   const sc=document.getElementById('welSrc');if(sc)sc.onclick=e=>{e.preventDefault();try{_api().open_external((_welInfo&&_welInfo.project_url)||'');}catch(_){}};
   const pv=document.getElementById('welPriv');if(pv)pv.onclick=e=>{e.preventDefault();try{_api().open_doc('PRIVACY.md');}catch(_){}};
   const se=document.getElementById('welSec');if(se)se.onclick=e=>{e.preventDefault();try{_api().open_doc('SECURITY.md');}catch(_){}};
   document.addEventListener('keydown',e=>{if(e.key==='Escape'){const g=document.getElementById('gate');
     if(g&&g.classList.contains('show')&&document.body.dataset.welinit==='1')welcomeHide();}});})();
 // JS-side failures are logged Python-side (no secrets, error text only)
 window.addEventListener('error',e=>{try{_api()&&_api().log_js_error(String(e.message||e),String(e.filename||''),e.lineno||0);}catch(_){}});
 window.addEventListener('unhandledrejection',e=>{try{_api()&&_api().log_js_error('unhandledrejection: '+String((e.reason&&e.reason.message)||e.reason),'',0);}catch(_){}});

 // ---- setup wizard (steps 2-4) + Trust Center ------------------------------
 // Registry-driven: everything rendered here comes from Api.trust_state /
 // calibration_registry / readiness_check, which in turn read lite_trust.py
 // and lite_onboarding.py. No status is invented in the UI layer.
 (function(){
   const E=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
   const $id=id=>document.getElementById(id);
   let ST=null, PAGE='trust', PLAT='mac', DET='mac', CAL=null;
   const PAGES=['trust','cal','ready'];
   function railSet(){const done={welcome:1};PAGES.forEach((p,i)=>{if(PAGES.indexOf(PAGE)>i)done[p]=1;});
     document.querySelectorAll('#supRail li').forEach(li=>{const s=li.dataset.step;
       li.classList.toggle('cur',s===PAGE);li.classList.toggle('done',!!done[s]&&s!==PAGE);
       if(s===PAGE)li.setAttribute('aria-current','step');else li.removeAttribute('aria-current');});}
   function stDot(st){ // status -> colour class + label (text carries meaning, never colour alone)
     const m={granted:['ok','Granted'],untested:['mid','Not tested yet'],configured:['mid','Configured'],
       not_granted:['no','Not granted'],disabled:['off','Off (default)'],not_requested:['off','Never requested'],
       info:['off','No permission'],unknown:['mid','Unknown']};
     return m[st]||['mid',st||'?'];}
   const OS_CAPS=['screen_detection','input_control','stop_hotkeys'];
   function capPill(c){ // the authoritative pill for the three OS capabilities:
     // OS preflight + requested-once + restart inference + this session's real
     // test compose the label, so "Not granted" is never shown for
     // never-asked, restart-pending, or merely-untested states.
     const live=c.live||{};
     if(OS_CAPS.indexOf(c.id)<0)return stDot(live.status);
     const t=live.test;
     if(live.status==='granted'&&live.requires_restart)return ['mid','Granted — restart to apply'];
     if(live.status==='not_granted'&&!live.requested)return ['mid','Not requested yet'];
     if(live.status==='untested'&&t&&t.status==='passed')return ['ok','Works (tested this session)'];
     if(live.status==='untested'&&t&&t.status==='failed')return ['no','Test failed'];
     return stDot(live.status);}
   function capDetail(c){const live=c.live||{};let d=live.detail||'';
     if(OS_CAPS.indexOf(c.id)>=0){
       if(live.status==='not_granted'&&!live.requested)
         d='macOS has not been asked yet — nothing is wrong. Use Request access… to register the app, then flip its switch.';
       if(live.requires_restart)
         d+=' The change applies after Prospector Lite restarts.';
       if(live.test)d+=' Last test this session: '+live.test.status+'.';}
     return d;}
   function cardStamp(c,prog){const l=c.live||{};
     return [l.status,l.requires_restart?1:0,l.requested?1:0,
             (l.test&&l.test.status)||'',
             prog?(prog.state+':'+prog.seq+':'+(prog.reason||'')):''].join('|');}
   // ---- sequential progression over the REQUIRED capabilities ----------
   // Derived from real state only: a mac capability is complete when the
   // OS reports granted with no restart pending; on Windows (no OS
   // preflights) only a passed session test completes it. Opening System
   // Settings never completes anything.
   function capComplete(c){const l=c.live||{};
     if(OS_CAPS.indexOf(c.id)<0)return false;
     if(DET==='mac')return l.status==='granted'&&!l.requires_restart;
     return !!(l.test&&l.test.status==='passed');}
   function trustProg(reqCaps){const steps=reqCaps.map(c=>({id:c.id,required:true,
       complete:capComplete(c),title:c.title}));
     // same state/ordering rules as the Python engine
     // (lite_onboarding.progression); today's registry has exactly the
     // three OS capabilities as REQUIRED -- if a future required cap is
     // added outside OS_CAPS, capComplete must gain a completion rule for
     // it or this list would deadlock on a never-completable ACTIVE step.
     const out={};let seq=0,done=0,active=null,gate='';
     steps.forEach(s=>{seq++;
       if(s.complete){done++;out[s.id]={state:'COMPLETE',seq,reason:'Complete - open it any time to review or redo it.'};return;}
       if(active===null){active=s.id;gate=s.title;
         out[s.id]={state:'ACTIVE',seq,reason:'Do this next.'};return;}
       out[s.id]={state:'UPCOMING',seq,reason:'Complete '+gate+' first.'};});
     out['']={total:steps.length,done:done,active:active};
     return out;}
   function stepChip(p){if(!p)return '';
     if(p.state==='COMPLETE')return '<span class="step-chip done">Complete</span>';
     if(p.state==='ACTIVE')return '<span class="step-chip next">Do this next</span>';
     if(p.state==='UPCOMING')return '<span class="step-chip up">Upcoming</span>';
     if(p.state==='NEEDS_REVIEW')return '<span class="step-chip rev">Needs review</span>';
     if(p.state==='BLOCKED')return '<span class="step-chip rev">Blocked</span>';
     if(p.state==='OPTIONAL')return '<span class="step-chip optional">Optional</span>';
     return '';}
   function stepClass(p){if(!p)return '';
     if(p.state==='ACTIVE'||p.state==='NEEDS_REVIEW')return ' step-active';
     if(p.state==='UPCOMING')return ' step-upcoming';
     return '';}
   function reqBadge(l){if(l==='REQUIRED_FOR_CORE')return '<span class="cap-badge req">Required</span>';
     if(l==='REQUIRED_FOR_SPECIFIC_FEATURE')return '<span class="cap-badge req">Required for a feature</span>';
     if(l==='OPTIONAL')return '<span class="cap-badge opt">Optional</span>';
     if(l==='NOT_REQUIRED')return '<span class="cap-badge">Never requested</span>';
     return '<span class="cap-badge">Info</span>';}
   function capCard(c,compact,prog){
     const live=c.live||{},[cls,lab]=capPill(c);
     const osl=(c.operating_system_label||{})[PLAT]||'';
     const real=(PLAT===DET); // action buttons only on the platform we are actually on
     let acts='';
     if(OS_CAPS.indexOf(c.id)>=0){
       if(real&&DET==='mac'&&live.status!=='granted')
         acts+='<button type="button" class="btn2" data-act="request" data-cap="'+c.id+'">Request access&hellip;</button>';
       if(real&&DET==='mac')
         acts+='<button type="button" class="btn2" data-act="settings" data-cap="'+c.id+'">Open System Settings</button>';
       if(real&&DET==='mac'&&(live.status!=='granted'||live.requires_restart))
         acts+='<button type="button" class="btn2" data-act="recheck" data-cap="'+c.id+'">I&rsquo;ve enabled it &mdash; check again</button>';
       if(real&&DET==='mac'&&live.requires_restart)
         acts+='<button type="button" class="btn2" data-act="relaunch" data-cap="'+c.id+'">Restart Prospector Lite</button>';
       if(real)acts+='<button type="button" class="btn2" data-act="test" data-cap="'+c.id+'">Test '+E(c.title.split(' ')[0].toLowerCase()==='safe'?'Safe Stop':c.title)+'</button>';
     }
     if(c.id==='discord_notifications'&&real){
       acts+='<button type="button" class="btn2" data-act="preview" data-cap="'+c.id+'">Preview exact payload</button>';
       acts+='<button type="button" class="btn2" data-act="notifpage" data-cap="'+c.id+'">Configure (Notifications page)</button>';}
     acts+='<button type="button" class="btn2" data-act="code" data-cap="'+c.id+'">View code</button>';
     let facts='<div class="cap-facts">'
       +'<div><b>Data it can access</b>'+E(c.data_accessed)+'</div>'
       +'<div><b>Kept on disk</b>'+E(c.data_retained)+'</div>'
       +'<div><b>Leaves this computer</b>'+E(c.network_behaviour)+'</div>'
       +(osl?('<div><b>'+(PLAT==='mac'?'macOS label':'Windows')+'</b>'+E(osl)+'</div>'):'')+'</div>';
     let more='<details class="cap-more"><summary>Why this is needed, what happens if you decline, how to revoke</summary>'
       +'<div class="cap-desc">'+E(c.detailed_explanation)+'</div>'
       +'<div class="cap-desc"><b>If you decline:</b> '+E(c.declined_behaviour)+'</div>'
       +'<div class="cap-desc"><b>To revoke:</b> '+E((c.revoke_instructions||{})[PLAT]||'')+'</div>'
       +(c.privacy_notes?('<div class="cap-desc"><b>Privacy note:</b> '+E(c.privacy_notes)+'</div>'):'')
       +'</details>';
     const num=(prog&&prog.seq)?('<span class="step-num" aria-hidden="true">'+prog.seq+'</span>'):'';
     const reason=(prog&&(prog.state==='UPCOMING'||prog.state==='NEEDS_REVIEW'||prog.state==='BLOCKED'))
       ?('<div class="cap-desc"><b>'+(prog.state==='UPCOMING'?'Upcoming:':'Review:')+'</b> '+E(prog.reason)+'</div>'):'';
     // UPCOMING must be REALLY disabled (attribute-level, like the
     // calibration checklist) -- CSS pointer-events alone lets keyboard
     // users activate a disabled-looking button. View code stays usable.
     if(prog&&prog.state==='UPCOMING')
       acts=acts.replace(/<button type="button" class="btn2" data-act="(?!code)/g,
         '<button type="button" class="btn2" disabled aria-disabled="true" title="'+E(prog.reason)+'" data-act="');
     const aria=prog?(' aria-label="Step '+prog.seq+': '+E(c.title)+' - '
       +E(prog.state==='COMPLETE'?'complete':(prog.state==='ACTIVE'?'do this next':prog.state.toLowerCase().replace('_',' ')))+'"'):'';
     return '<div class="cap-card'+stepClass(prog)+'" data-capid="'+c.id+'" data-compact="'+(compact?1:0)+'" data-stamp="'+E(cardStamp(c,prog))+'"'+aria+'><div class="cap-head">'
       +num+'<span class="cap-title">'+E(c.title)+'</span>'+reqBadge(c.required_level)+stepChip(prog)
       +'<span class="cap-st '+cls+'"><span class="dot"></span>'+E(lab)+'</span></div>'
       +'<div class="cap-desc">'+E(c.short_description)+' '+E(capDetail(c))+'</div>'+reason
       +(compact?'':facts)
       +'<div class="cap-actions">'+acts+'</div>'
       +'<div class="cap-test" id="captest_'+c.id+'" aria-live="polite"></div>'
       +(compact?'':more)+'</div>';}
   // In-place card refresh: patches only cards whose composed state actually
   // changed, preserves the test-output area, and never touches a card whose
   // test is currently armed -- so a background refresh can not wipe an
   // in-flight test (the old full re-render did exactly that).
   const _busy={};
   function updateCards(root){if(!ST||!root)return;let rewire=null;
     // recompute progression when this surface is the wizard trust step
     // (the Trust Center has no progression: prog stays undefined there).
     // Must mirror renderTrust EXACTLY -- including the OPTIONAL stamp for
     // optional caps -- or the stamp mismatch strips their chips on the
     // first background refresh.
     let prog=null;
     if(root.id==='supBody'&&PAGE==='trust'){
       const caps=ST.capabilities.filter(c=>(c.platforms||[]).indexOf(PLAT)>=0);
       prog=trustProg(caps.filter(c=>c.required_level.indexOf('REQUIRED')===0));
       caps.filter(c=>c.required_level==='OPTIONAL').forEach(c=>{
         prog[c.id]={state:'OPTIONAL',seq:0,reason:''};});}
     ST.capabilities.forEach(c=>{
       const card=root.querySelector('.cap-card[data-capid="'+c.id+'"]');
       if(!card||_busy[c.id])return;
       const p=prog?prog[c.id]:undefined;
       const stamp=cardStamp(c,p);
       if(card.dataset.stamp===stamp)return;
       const keepEl=card.querySelector('.cap-test');
       const wrap=document.createElement('div');
       wrap.innerHTML=capCard(c,card.dataset.compact==='1',p);
       const fresh=wrap.firstChild;
       const ct=fresh.querySelector('.cap-test');
       // MOVE the live test-output node (listeners and all) into the fresh
       // card -- an innerHTML copy would leave a visually intact sandbox
       // whose Start-test button is dead.
       if(ct&&keepEl)ct.replaceWith(keepEl);
       card.replaceWith(fresh);rewire=root;});
     if(rewire){wireCards(rewire);
       const sum=root.querySelector('.sup-progress');
       if(sum&&prog&&prog[''])sum.textContent=trustProgText(prog['']);}}
   function trustProgText(s){return 'Required permissions: '+s.done+' of '+s.total
     +' complete'+(s.active?'':' - all done');}
   let _reqId=0;
   function wireCards(box){
     box.querySelectorAll('button[data-act]').forEach(b=>{b.onclick=async()=>{
       const cap=b.dataset.cap, act=b.dataset.act;
       // Resolve the output area through the SURFACE root at every write:
       // (a) the wizard and the Trust Center both render these cards with
       // the same element ids, so a global $id() can hit the other
       // (hidden) surface; (b) a card can be replaced by updateCards while
       // an action awaits, and a captured node would then be detached.
       const outEl=()=>box.querySelector('.cap-card[data-capid="'+cap+'"] .cap-test');
       const show=h=>{const o=outEl();if(o){o.classList.add('show');o.innerHTML=h;}};
       if(act==='request'){show('Asking macOS&hellip; if a prompt appears it is the system asking, with this app named.');
         let r={};try{r=await _api().trust_request(cap);}catch(e){r={ok:false,error:String(e)};}
         if(r.granted){show('Granted. Use Test to prove it works.');}
         else if(r.ok===false){show('&#10007; '+E(r.error||'The request could not run.')+(r.error_code?(' ['+E(r.error_code)+']'):''));}
         else{show(E(r.note||'macOS listed the app in System Settings; flip the switch there, then use “I’ve enabled it — check again”. A restart of the app may be needed.'));}
         refresh(false);armPoll();return;}
       if(act==='settings'){let r={};try{r=await _api().trust_open_settings(cap);}catch(e){r={ok:false,error:String(e)};}
         if(r&&r.ok===false){show('&#10007; Could not open System Settings ('+E(r.error||'')+'). Open it manually: System Settings &rarr; Privacy &amp; Security &rarr; '+E(((ST&&ST.capabilities.find(x=>x.id===cap)||{}).permission_category||{}).mac||'')+'.');}
         else{show('System Settings should now be open at the right pane. Flip the switch for <b>Prospector Lite</b>, come back, and this card re-checks automatically (or click “I’ve enabled it — check again”). If the switch already looks ON but this still says Not granted, the row may belong to an older copy: remove it with the &minus; button, then Request access here to re-register.');}
         refresh(false);armPoll();return;}
       if(act==='recheck'){show('Checking with macOS&hellip;');
         armPoll();
         await refresh(false);
         const c2=((ST&&ST.capabilities)||[]).find(x=>x.id===cap);const lv=(c2&&c2.live)||{};
         if(lv.status==='granted'&&!lv.requires_restart){show('&#10003; macOS reports access granted to this app. Use Test to prove it works.');}
         else if(lv.status==='granted'&&lv.requires_restart){show('&#9888; Granted, but this running copy started before the change &mdash; restart Prospector Lite to apply it (button above).');}
         else if(lv.status==='unknown'){show('&#9888; The system check API was unavailable, so the state cannot be read. Run the Test instead &mdash; it exercises the real capability. [PP-TRUST-UNKNOWN]');}
         else{show('&#10007; macOS still reports no access for this exact copy. If the switch in System Settings already looks ON, that row may belong to an older copy of the app: remove it there (&minus; button), click Request access&hellip; here to re-register, flip the new row ON, then restart the app. [PP-TRUST-STALE]');}
         return;}
       if(act==='relaunch'){show('Restarting Prospector Lite&hellip; it will reopen by itself.');
         try{await _api().trust_relaunch();}catch(e){}return;}
       if(act==='test'){
         if(cap==='screen_detection'){show('Capturing a small centre patch&hellip;');
           let r={};try{r=await _api().trust_test_screen();}catch(e){r={error:String(e)};}
           if(r.ok){show((r.nonblank?'&#10003; Capture works &mdash; ':'&#9888; ')+E(r.note||'')+' ('+r.width+'&times;'+r.height+' px, shown once, not saved)'+(r.preview?('<img src="'+r.preview+'" alt="one-shot capture preview">'):''));}
           else{show('&#10007; '+E(r.error||'failed')+' &mdash; '+E(r.note||''));}
           refresh(false);return;}
         if(cap==='input_control'){
           show('Click into the box below, then press Start test and keep this window focused. The app types one harmless letter into its own box and wiggles the pointer 2&nbsp;px &mdash; it checks it is the focused app first, so if you switch away nothing is typed anywhere.'
             +'<div style="margin-top:8px;display:flex;gap:8px;align-items:center"><input data-sand="key" placeholder="test lands here" aria-label="Input test target"> <button type="button" class="btn2" data-sand="go">Start test</button></div><div data-sand="out" style="margin-top:7px"></div>');
           const sand=n=>{const o=outEl();return o?o.querySelector('[data-sand="'+n+'"]'):null;};
           const go=sand('go');if(go)go.onclick=async()=>{
             if(_busy[cap])return;_busy[cap]=1;go.disabled=true;
             const f=sand('key');
             let down=false,up=false,postRes=null;
             const rid=++_reqId;
             window.__keyTestResult=m=>{if(m&&m.id===rid)postRes=m.result||null;};
             if(f){f.value='';f.focus();}
             // match the PHYSICAL key (e.code) -- the injected event is a
             // keycode/scancode, so on non-QWERTY layouts e.key is not 't'
             const isT=e=>((e.code||'')==='KeyT')||((e.key||'').toLowerCase()==='t');
             const h1=e=>{if(isT(e))down=true;};
             const h2=e=>{if(isT(e))up=true;};
             window.addEventListener('keydown',h1,true);window.addEventListener('keyup',h2,true);
             let pr={};try{pr=await _api().trust_test_pointer();}catch(e){pr={ok:false,error:String(e)};}
             try{await _api().trust_test_key(rid);}catch(e){}
             setTimeout(()=>{window.removeEventListener('keydown',h1,true);window.removeEventListener('keyup',h2,true);
               window.__keyTestResult=null;
               _busy[cap]=0;const g2=sand('go');if(g2)g2.disabled=false;
               // record the REAL composite outcome: the keyboard half only
               // counts when the post actually happened, and a pass needs
               // both halves -- the pill must never claim 'Works' from the
               // pointer wiggle alone
               // posted requires an actually-received worker callback: a
               // lost delivery must record NOTHING, not a keyboard fail
               try{_api().trust_record_input(rid,{down:down,up:up,
                 posted:!!(postRes&&postRes.posted!==false),
                 pointer_moved:!!(pr&&pr.ok&&pr.moved)});}catch(e){}
               let kb;
               if(postRes&&postRes.posted===false){kb='&#9888; '+E(postRes.error||'The test key was not posted.')+(postRes.error_code?(' ['+E(postRes.error_code)+']'):'');}
               else if(down&&up){kb='&#10003; keystroke arrived AND released cleanly';}
               else if(down){kb='&#9888; key-down seen but no key-up';}
               else if(DET==='mac'){kb='&#10007; no keystroke arrived &mdash; grant Accessibility to Prospector Lite, keep this window focused, then retry. If you granted it while the app was running, restart the app first.';}
               else{kb='&#10007; no keystroke arrived &mdash; keep this window focused and retry. If Roblox runs as administrator, run both apps at the same normal level.';}
               const mo=(pr&&pr.ok)?(pr.moved?'&#10003; pointer moved and was restored':('&#10007; pointer did not move'+(DET==='mac'?' (macOS is blocking synthetic input — grant Accessibility)':'')))
                 :('&#9888; pointer test unavailable: '+E((pr&&pr.error)||''));
               const o2=sand('out');if(o2)o2.innerHTML=kb+'<br>'+mo;refresh(false);},2600);};
           return;}
         if(cap==='stop_hotkeys'){
           if(_busy[cap])return;_busy[cap]=1;b.disabled=true;
           const rid=++_reqId;let secs=8;
           show('Armed &mdash; <b data-hkleft>8</b>s left. Press <b>Esc</b> or <b>Ctrl+K</b> now (click into Roblox or anywhere first if you like: it must work globally).');
           const tick=setInterval(()=>{secs--;const o=outEl();const el=o&&o.querySelector('[data-hkleft]');if(el)el.textContent=Math.max(0,secs);if(secs<=0)clearInterval(tick);},1000);
           const done=h=>{clearInterval(tick);_busy[cap]=0;b.disabled=false;
             window.__hotkeyResult=null;show(h);refresh(false);};
           window.__hotkeyResult=r=>{if(r&&r.id!==undefined&&r.id!==rid)return; // stale arm
             if(r&&r.heard)done('&#10003; Heard <b>'+E(r.heard)+'</b> &mdash; Safe Stop works.');
             else if(r&&r.error)done('&#10007; '+E(r.error)+(r.error_code?(' ['+E(r.error_code)+']'):''));
             else done('&#9888; '+E((r&&r.note)||'Nothing heard within the window.')+' Run it again and press Esc or Ctrl+K while it is armed.');};
           let a=null;try{a=await _api().trust_test_hotkey(8,rid);}catch(e){}
           if(!a||!a.armed){done('&#10007; '+E((a&&a.error)||'Could not arm the Safe Stop test.')+((a&&a.error_code)?(' ['+E(a.error_code)+']'):''));}
           return;}
         return;}
       if(act==='preview'){show('Building the exact payload from the same engine code that sends it&hellip;');
         let r={};try{r=await _api().webhook_payload_preview();}catch(e){r={error:String(e)};}
         if(r.ok){show('This is everything a notification sends (example stats). Screenshot attach is a separate opt-in, currently '+(r.screenshot_optin?'ON — when it fires, the body additionally carries a screenshot field (base64 PNG) and screenshot_format':'OFF — no screenshot fields are ever added while it stays off')+'.<pre class="tc-pre">'+E(JSON.stringify({headers:r.headers,body:r.payload},null,1))+'</pre>');}
         else{show('&#10007; '+E(r.error||'failed'));}return;}
       if(act==='notifpage'){SETUP.suspend();const t=document.querySelector('.tab[data-tab="Notifications"],.tab[data-tab="notifications"]');
         const nt=t||document.querySelector('.tab[data-tab="settings"]');if(nt)nt.click();
         const real=document.querySelector('#p_Notifications')?document.querySelector('.tab[data-tab="Notifications"]'):null;if(real)real.click();return;}
       if(act==='code'){let r={};try{r=await _api().trust_view_code(cap,0);}catch(e){r={error:String(e)};}
         if(r.opened){show('Opened the exact-commit source in your browser.');}
         else if(r.local){show('No public repository URL is configured in this build, so here is the exact local reference (never a moving branch):<pre class="tc-pre">'+E(r.local)+'</pre>');}
         else{show('&#10007; '+E(r.error||'failed'));}return;}
     };});}
   function platTabs(){return '<div class="plat-tabs" role="tablist" aria-label="Platform">'
     +'<button role="tab" id="plat_mac" aria-selected="'+(PLAT==='mac')+'">macOS'+(DET==='mac'?' (this computer)':'')+'</button>'
     +'<button role="tab" id="plat_win" aria-selected="'+(PLAT==='win')+'">Windows'+(DET==='win'?' (this computer)':'')+'</button></div>';}
   function wirePlat(box,rerender){['mac','win'].forEach(p=>{const b=$id('plat_'+p);if(b)b.onclick=()=>{
       if(Object.keys(_busy).some(k=>_busy[k])){toast('A test is running — wait for it to finish first.');return;}
       PLAT=p;rerender();};
     const t=box.querySelector('.plat-tabs');if(t)t.onkeydown=e=>{if(e.key==='ArrowRight'||e.key==='ArrowLeft'){
       if(Object.keys(_busy).some(k=>_busy[k])){toast('A test is running — wait for it to finish first.');return;}
       PLAT=(PLAT==='mac'?'win':'mac');rerender();const nb=$id('plat_'+PLAT);if(nb)nb.focus();}};});}
   function renderTrust(){GEN++;PAGE='trust';railSet();const b=$id('supBody');
     if(!ST){b.innerHTML='<p class="sup-sub">Loading&hellip;</p><div class="cap-actions"><button type="button" class="btn2" id="trustRetry">Retry</button></div>';
       const rt=$id('trustRetry');if(rt)rt.onclick=async()=>{await refresh(false);renderTrust();};return;}
     const caps=ST.capabilities.filter(c=>(c.platforms||[]).indexOf(PLAT)>=0);
     const req=caps.filter(c=>c.required_level.indexOf('REQUIRED')===0);
     const opt=caps.filter(c=>c.required_level==='OPTIONAL');
     // Sequential progression over the required permissions: exactly one
     // ACTIVE step, later ones faded + labelled Upcoming. The full
     // "never requested" capability list stays in the Trust Center; this
     // first-run page keeps only the capabilities being set up.
     const prog=trustProg(req);
     const sum=prog['']||{};
     b.innerHTML='<h2 class="sup-h1">Trust &amp; Permissions<span class="plat-badge">'+(DET==='mac'?'macOS detected':(DET==='win'?'Windows detected':DET))+'</span></h2>'
       +'<p class="sup-sub">Work through the required permissions in order - the highlighted card is the one to do next. Nothing is requested until you click a button below. '+(ST.dev_note?E(ST.dev_note):'')+'</p>'
       +platTabs()
       +(PLAT!==DET?'<p class="sup-sub">You are browsing the '+(PLAT==='mac'?'macOS':'Windows')+' column for reference; live statuses always describe THIS computer ('+(DET==='mac'?'macOS':'Windows')+').</p>':'')
       +'<div class="cap-actions" style="margin:2px 0 8px"><button type="button" class="btn2" id="trustRefresh">&#8635; Refresh status</button><span class="sup-note" id="trustChecked" aria-live="polite"></span></div>'
       +'<div class="sup-progress" aria-live="polite">'+E(trustProgText(sum))+'</div>'
       +'<div class="sup-group">Required for the macro</div>'+req.map(c=>capCard(c,false,prog[c.id])).join('')
       +'<div class="sup-group">Optional &mdash; off by default, never blocks setup</div>'+opt.map(c=>capCard(c,false,{state:'OPTIONAL',seq:0,reason:''})).join('')
       +'<p class="sup-sub">Prospector Lite uses only the access shown here. Detailed privacy and network information - including everything this app never requests (microphone, camera, location, admin rights, full-disk access) - is in the <b>Trust Center</b> tab after setup.</p>';
     wirePlat(b,renderTrust);wireCards(b);
     const act=sum.active?b.querySelector('.cap-card[data-capid="'+sum.active+'"]'):null;
     if(act&&act.scrollIntoView)try{act.scrollIntoView({block:'nearest'});}catch(e){}
     const rf=$id('trustRefresh');if(rf)rf.onclick=async()=>{rf.disabled=true;await refresh(false);rf.disabled=false;stampChecked();};
     stampChecked();
     $id('supBack').textContent='← Welcome';$id('supNext').textContent='Continue to Calibration →';
     $id('supNote').textContent=(DET==='mac')?'You can continue any time - Start Macro stays disabled until the required permissions work.':'Windows has no permission prompts here - run the tests to prove everything works.';
     b.focus();
     refresh(false); // repaint from a fresh snapshot (entry may have used a stale one)
   }
   function stampChecked(){const el=$id('trustChecked');
     if(el&&ST&&ST.checked_at)el.textContent='Status checked '+new Date(ST.checked_at*1000).toLocaleTimeString();}
   function calStatus(st){const m={ok:['ok','Calibrated'],auto:['mid','Auto'],stale:['no','Stale'],unset:['off','Not set'],needs_review:['no','Needs review'],off:['off','Off']};return m[st]||['mid',st||'?'];}
   // ---- guided calibration: checklist + in-wizard detail pages ------------
   // The guided flow NEVER leaves the wizard: every capture drives the same
   // shared calibration service (wizard_propose / start_overlay_calibrate /
   // start_overlay_region / start_cue_mask_capture) with
   // context='guided_setup'; the only difference from the Calibrate tab is
   // where the completion callback navigates. The full-screen picker is an
   // always-on-top overlay window -- the main window stays on the wizard.
   let CALV={view:'list',item:null,awaiting:null,gen:0};
   function calNav(){CALV.gen++;CALV.awaiting=null;}
   // Per-item capture plans against the shared service. Every stage
   // carries its own PREP text: multi-stage plans NEVER auto-chain -- after
   // each confirmed capture the user lands back on the wizard's stage card,
   // switches to Roblox to set up the next target, and starts the next
   // capture explicitly. (The rc.4 flow chained them back-to-back, which
   // made cue/Auto-Pan calibration impossible to set up between captures.)
   const GD_STEPS={
     cap_bar:{kind:'propose',seq:[
       ['CAP_RIGHT','Capacity bar - RIGHT tip','In Roblox, dig until the pan capacity bar is COMPLETELY full (all yellow). Leave it full and come back here.'],
       ['CAP_LEFT','Capacity bar - LEFT tip','Keep the bar full - nothing else to change in the game. The picker will propose the LEFT tip next.']]},
     pan_prompt:{kind:'propose',seq:[
       ['PAN_PIX','Pan prompt','Stand in the WATER so the white "Pan" prompt shows at the bottom of the screen, then come back here.']]},
     deposit_prompt:{kind:'propose',seq:[
       ['DEPOSIT_PIX','Collect Deposit prompt','Step onto LAND at a deposit so the white "Collect Deposit" prompt shows, then come back here.']]},
     shake_prompt:{kind:'propose',seq:[
       ['SHAKE_PIX','Shake prompt','Fill the pan, walk to the water and BEGIN a shake so the white "Shake" prompt shows, then come back here quickly - the capture snapshots the game the moment you press Start.']]},
     cue_masks:{kind:'cues',cues:[
       ['PAN','"Pan" cue mask','In Roblox, stand in the WATER so the "Pan" prompt is visible. Come back here and press Start - the capture snapshots the game at that moment, then you click the word and adjust the green letters.'],
       ['DEPOSIT','"Collect Deposit" cue mask','Now step onto LAND at a deposit so "Collect Deposit" shows. Come back here and press Start when it is on screen.'],
       ['SHAKE','"Shake" cue mask','Now BEGIN a shake so the "Shake" prompt shows, then quickly come back and press Start while it is still on screen.']]},
     dig_green:{kind:'pixel',seq:[
       ['DIG_TRIGGER_PIXEL','Green dig-bar zone','Start a dig on land so the dig bar with its green zone is on screen, then come back here.']]},
     money_region:{kind:'region',base:'MONEY',prep:'Make sure the money counter is visible in its usual corner (close any menu covering it).'},
     shards_region:{kind:'region',base:'SHARDS',prep:'Make sure the shards counter is visible (close any menu covering it).'},
     find_region:{kind:'region',base:'FIND',prep:'Note where find pop-ups appear - having one on screen helps you aim the box but is not required.'},
     // fortune_river has NO wizard plan: the registry excludes it from the
     // wizard (wizard:false) and it is calibrated from the Calibrate tab's
     // Fortune River section only.
     autopan_button:{kind:'pixel',anykey:true,seq:[
       ['AUTOPAN_ON','Auto Pan button (ON state)','In Roblox, switch Auto Pan ON so its button shows the ON colour, then come back here.'],
       ['AUTOPAN_OFF','Auto Pan button (OFF state)','Now switch Auto Pan OFF in the game so the button shows the OFF colour, then come back here.']]}};
   function calItem(id){return ((CAL&&CAL.items)||[]).find(x=>x.id===id)||null;}
   async function calReload(){try{CAL=await _api().calibration_registry();}catch(e){}}
   // completion callback from the overlay (Python fires __calDone after
   // Confirm or Cancel, carrying the calling context + overlay key). A
   // guided listener reacts ONLY to guided_setup results while a guided
   // detail page is actually awaiting one -- a stale or foreign result can
   // never navigate the wizard.
   const _prevCalDone=window.__calDone;
   window.__calDone=function(r){if(_prevCalDone)try{_prevCalDone(r);}catch(e){}
     try{onGdDone(r||{});}catch(e){}};
   async function onGdDone(r){
     if(r.ctx!=='guided_setup')return;
     if(CALV.view!=='detail'||!CALV.awaiting)return;
     const aw=CALV.awaiting,gen=CALV.gen;
     // the bridge carries the overlay key precisely so a stale completion
     // from a DIFFERENT capture can never be credited to this stage
     const expect=aw.plan[aw.next-1];
     const ek=expect?(expect.kind==='cue'?('CUEMASK:'+expect.key):(expect.kind==='region'?('REGION:'+expect.key):expect.key)):'';
     if(r.key&&ek&&r.key!==ek)return;
     CALV.awaiting=null;
     if(r.cancelled){
       gdStageCard(aw.item,aw.plan,aw.next-1,
         '<span class="no">Cancelled - nothing was saved.</span> Set up the game again if needed, then press Start capture to retry this one.');
       gdButtons(false);return;}
     if(!r.ok){
       gdStageCard(aw.item,aw.plan,aw.next-1,
         '<span class="no">That capture did not save.</span> Press Start capture to retry it.');
       gdButtons(false);return;}
     if(aw.next<aw.plan.length){
       // more captures in this plan: RETURN TO THE WIZARD with the next
       // stage's prep card -- the user goes back into Roblox, prepares the
       // next target, and starts the next capture explicitly. Never chain.
       if(gen!==CALV.gen)return;
       gdStageCard(aw.item,aw.plan,aw.next,
         '<span class="ok">&#10003; Capture '+(aw.next)+' of '+aw.plan.length+' saved.</span>');
       gdButtons(false);return;}
     gdOut('Validating&hellip;');
     await calReload();
     if(gen!==CALV.gen)return; // user navigated away while validating
     const it=calItem(aw.item.id);
     const st=(it&&it.live&&it.live.status)||'';
     const good=(st==='ok')||(!it.required&&st!=='unset');
     if(good){gdSuccess(it);}
     else{
       gdOut('<span class="no">Saved, but validation reports: '+E((it&&it.live&&it.live.detail)||st||'unknown')+'</span> Press Start calibration to redo this step; the checklist keeps it active until it passes.');
       gdButtons(false);}
   }
   // The next actionable step after `id` in registry order (for the
   // success panel's Next button): the first later non-complete step, else
   // the first non-complete anywhere, else null (everything done).
   function gdNextTarget(id){
     const items=(CAL&&CAL.items||[]).filter(i=>i.id!=='roblox_window');
     const idx=items.findIndex(i=>i.id===id);
     const after=items.slice(idx+1).find(i=>(i.prog||{}).state!=='COMPLETE'&&(i.prog||{}).state!=='OPTIONAL');
     if(after)return after;
     const opt=items.slice(idx+1).find(i=>(i.prog||{}).state==='OPTIONAL'&&(i.live||{}).status==='unset');
     if(opt)return opt;
     return items.find(i=>i.id!==id&&(i.prog||{}).state!=='COMPLETE'&&(i.prog||{}).state!=='OPTIONAL')||null;}
   function gdSuccess(it){
     const nxt=gdNextTarget(it.id);
     const saved=it.saved?(' <span class="cal-keys">'+E(it.saved)+'</span>'):'';
     gdOut('<span class="ok">&#10003; Saved and validated.</span>'+saved
       +'<div class="cap-actions" style="margin-top:9px">'
       +(nxt?('<button type="button" class="btn" id="gdnext">Next: '+E(nxt.title)+' &rarr;</button>'):'')
       +'<button type="button" class="btn2" id="gdlist">Back to the checklist</button>'
       +(nxt?'':'<span class="cal-keys">All steps are covered - continue to Readiness when ready.</span>')
       +'</div>');
     gdButtons(false);
     const nb=$id('gdnext');if(nb)nb.onclick=()=>{calNav();renderCalDetail(nxt.id);};
     const lb=$id('gdlist');if(lb)lb.onclick=()=>{calNav();renderCal();};}
   // Stage card: what to do IN THE GAME before this capture + an explicit
   // Start button. Rendered between every capture of a multi-stage plan.
   function gdStageCard(item,plan,idx,note){
     CALV.stage=idx;
     const st=plan[idx];
     gdOut((note?note+'<br>':'')
       +'<b>Capture '+(idx+1)+' of '+plan.length+': '+E(st.label)+'</b>'
       +'<div class="gd-kv" style="margin-top:5px">'+E(st.prep||'Set up the game as described above, then start the capture.')+'</div>'
       +'<div class="cap-actions" style="margin-top:8px">'
       +'<button type="button" class="btn" id="gdcap">Start capture '+(idx+1)+'</button>'
       +(idx>0?'<button type="button" class="btn2" id="gdredoprev">Redo previous capture</button>':'')
       +'<button type="button" class="btn2" id="gdstop">Stop this sequence</button>'
       +'</div>');
     const cb=$id('gdcap');if(cb)cb.onclick=()=>gdCapture(item,plan,idx);
     const rp=$id('gdredoprev');if(rp)rp.onclick=()=>gdStageCard(item,plan,idx-1,'Redoing the previous capture.');
     const sb=$id('gdstop');if(sb)sb.onclick=()=>{CALV.awaiting=null;renderCalDetail(item.id);};}
   function gdOut(h){const o=$id('gdout');if(o)o.innerHTML=h;}
   function gdButtons(running){const s=$id('gdstart');if(s)s.disabled=!!running;
     const t=$id('gdtest');if(t)t.disabled=!!running;}
   async function gdPreflight(item){
     // Readiness confirm INSIDE the page: Roblox must be visible for any
     // screen capture; a missing permission is routed by calErr (below) to
     // the exact trust step -- also inside the wizard.
     gdOut('Checking Roblox is visible&hellip;');
     let w=null;try{w=await _api().detect_roblox();}catch(e){w=null;}
     if(!(w&&w.found)){
       gdOut('<span class="no">Roblox was not found on screen.</span> Open Roblox in Prospecting on your primary display, set up the scene as described above, then press Start again.');
       return false;}
     return true;}
   // Start ONE capture (one overlay session) for stage `idx`. Advancing to
   // the next stage happens only through onGdDone -> gdStageCard -> the
   // user's explicit Start; nothing here chains captures.
   async function gdCapture(item,plan,idx){
     const step=plan[idx];CALV.awaiting={item:item,plan:plan,next:idx+1};
     gdButtons(true);
     gdOut('Capture '+(idx+1)+' of '+plan.length+': <b>'+E(step.label)+'</b> - the full-screen picker is open on top. Click the target, then Confirm (or Cancel to come back here).');
     let r=null;
     try{
       if(step.kind==='propose')r=await _api().wizard_propose(step.key,step.label,'guided_setup');
       else if(step.kind==='pixel')r=await _api().start_overlay_calibrate(step.key,step.label,'guided_setup');
       else if(step.kind==='region')r=await _api().start_overlay_region(step.key,step.label,'guided_setup');
       else if(step.kind==='cue')r=await _api().start_cue_mask_capture(step.key,null,'guided_setup');
     }catch(e){r={error:String(e)};}
     if(r&&(r.error||(r.ok===false))){
       // an OPEN failure (capture/permission) must not lose the sequence
       // position: re-render THIS stage's card with the error as its note
       // so Start capture retries in place. Permission routing stays.
       CALV.awaiting=null;gdButtons(false);
       let note='&#10007; '+E((r&&r.error)||'failed')+((r&&r.error_code)?(' ['+E(r.error_code)+']'):'');
       if(r&&r.needs_permission)note+=' <button type="button" class="btn2" data-goperm="1">Open the permission step</button>';
       gdStageCard(item,plan,idx,'<span class="no">'+note+'</span>');
       const o=$id('gdout');if(o){const gp=o.querySelector('button[data-goperm]');
         if(gp)gp.onclick=()=>{calNav();renderTrust();};}}
   }
   function gdPlan(item){
     const p=GD_STEPS[item.id];if(!p)return [];
     if(p.kind==='propose'||p.kind==='pixel')
       return p.seq.map(s=>({kind:p.kind,key:s[0],label:s[1],prep:s[2]||''}));
     if(p.kind==='region')return [{kind:'region',key:p.base,label:item.title,prep:p.prep||''}];
     if(p.kind==='cues')return p.cues.map(c=>({kind:'cue',key:c[0],label:c[1],prep:c[2]||''}));
     return [];}
   function gdCalErr(r){let h='&#10007; '+E((r&&r.error)||'failed')+((r&&r.error_code)?(' ['+E(r.error_code)+']'):'');
     if(r&&r.needs_permission)h+=' <button type="button" class="btn2" data-goperm="1">Open the permission step</button>';
     gdOut(h);
     const o=$id('gdout');if(o){const gp=o.querySelector('button[data-goperm]');
       if(gp)gp.onclick=()=>{calNav();renderTrust();};}}
   async function gdStart(item){
     calNav();CALV.view='detail'; // fresh generation for this attempt
     if(!await gdPreflight(item))return;
     const plan=gdPlan(item);
     if(!plan.length){gdOut('This item has no guided capture plan.');return;}
     // Multi-stage plans open with the FIRST stage's prep card so the user
     // can set the game up before anything captures; single captures with
     // a prep note show it once too, then start on the user's click.
     if(plan.length>1||plan[0].prep){gdStageCard(item,plan,0,'');}
     else{gdCapture(item,plan,0);}}
   async function gdTest(item){
     gdButtons(true);gdOut('Testing against the live screen&hellip;');
     try{
       if(item.id==='cue_masks'){
         const parts=[];
         for(const c of ['PAN','DEPOSIT','SHAKE']){
           let r=null;try{r=await _api().cue_mask_check(c);}catch(e){r={ok:false,error:String(e)};}
           if(r&&r.ok)parts.push('<b>'+c+'</b>: '+(r.match?'<span class="ok">match ('+Math.round(r.fraction*100)+'% of letter pixels white)</span>':'<span class="no">no match right now ('+Math.round(r.fraction*100)+'%; needs 85%)</span>')+(r.background_white>0.5?' <span class="no">warning: the background is mostly white too - re-check the capture</span>':''));
           else parts.push('<b>'+c+'</b>: <span class="no">'+E((r&&r.error)||'no result')+'</span>');}
         gdOut(parts.join('<br>')+'<br>A prompt only matches while it is on screen - test each one in its real situation.');
       }else if(item.id==='money_region'||item.id==='shards_region'){
         let r=null;try{r=await _api().test_earn_read();}catch(e){r={};}
         gdOut('money: '+E((r&&r.money)||'?')+' &middot; shards: '+E((r&&r.shards)||'?'));
       }else if(item.id==='find_region'){
         let r=null;try{r=await _api().test_find_read();}catch(e){r={};}
         const lines=(r&&r.lines)||[];
         gdOut(r&&r.error?('<span class="no">'+E(r.error)+'</span>'):('OCR lines: '+(lines.length?lines.map(x=>'&ldquo;'+E(x)+'&rdquo;').join(' &middot; '):'(nothing right now - test while a find pop-up is showing)')));
       }else{
         let r=null;try{r=await _api().sample_pixels();}catch(e){r={error:String(e)};}
         if(r&&r.error)gdCalErr(r);
         else if(r&&r.empty)gdOut('&#9432; '+E(r.note||'Nothing saved to sample yet.'));
         else gdOut('<pre class="tc-pre">'+E(JSON.stringify(r,null,1))+'</pre>');}
     }finally{gdButtons(false);}}
   async function renderCalDetail(id){const g=++GEN;PAGE='cal';railSet();calNav();
     CALV.view='detail';CALV.item=id;
     const b=$id('supBody');
     if(!CAL){await calReload();if(g!==GEN)return;}
     const it=calItem(id);
     if(!it){renderCal();return;}
     const ins=it.instruction||{};
     const live=it.live||{},[cls,lab]=calStatus(live.status);
     const p=it.prog||{};
     const list=(t,a)=>((a&&a.length)?('<div class="gd-sec"><h4>'+t+'</h4><ul>'+a.map(x=>'<li>'+E(x)+'</li>').join('')+'</ul></div>'):'');
     const kv=(t,v)=>(v?('<div class="gd-kv"><b>'+t+':</b> '+E(v)+'</div>'):'');
     let cuesHtml='';
     if(it.id==='cue_masks'){
       let cs=null;try{cs=await _api().cue_mask_status();}catch(e){cs=null;}
       if(g!==GEN)return;
       const NAMES={PAN:'Pan (in water)',DEPOSIT:'Collect Deposit (on land)',SHAKE:'Shake'};
       cuesHtml='<div class="gd-sec"><h4>The three captures</h4><div class="gd-cues">'
         +Object.keys(NAMES).map(cu=>{const c=(cs&&cs.cues&&cs.cues[cu])||{};
           return '<div class="gd-cue"><b>'+NAMES[cu]+'</b>'
             +(c.has?('<img src="'+c.preview+'" alt="captured letter mask for '+cu+'">'):'')
             +'<div class="st">'+(c.has?('<span class="ok">captured &middot; '+c.px+' px</span>'):'<span class="no">not captured</span>')+'</div></div>';}).join('')
         +'</div></div>';}
     b.innerHTML='<div class="cap-actions gd-back"><button type="button" class="btn2" id="gdback">&larr; Back to calibration steps</button></div>'
       +'<h2 class="sup-h1">'+(p.seq?('Step '+p.seq+' &middot; '):'')+E(it.title)
       +(it.required?'<span class="cap-badge req">Required</span>':'<span class="cap-badge opt">Optional</span>')
       +'<span class="cap-st '+cls+'"><span class="dot"></span>'+E(lab)+'</span></h2>'
       +'<p class="sup-sub">'+E(ins.purpose||it.purpose)+'</p>'
       +list('Used by',ins.affected_modes||it.modes)
       +list('Before you start',ins.prerequisites)
       +list('Set up Roblox',ins.roblox_setup_steps)
       +kv('Where to stand',ins.player_position)
       +kv('Camera',ins.camera_setup)
       +list('Must be visible',ins.required_visible_elements)
       +list('Close or hide',ins.close_or_hide)
       +'<div class="gd-sec"><h4>What to select</h4><div class="gd-kv">'+E(ins.selection_target||it.instructions)+'</div>'+kv('How',ins.exact_action)+'</div>'
       +kv('A correct result looks like',ins.correct_result)
       +list('Common mistakes',ins.common_mistakes)
       +cuesHtml
       +'<div class="cal-eg" id="gdeg">Loading example&hellip;</div>'
       +'<div class="gd-sec"><h4>Privacy</h4>'+kv('Captured',ins.captured_data)+kv('Retention',ins.retention)+kv('Validated by',ins.validation)+'</div>'
       +kv('If you skip it',ins.unavailable_without||it.skip_consequence)
       +(live.status==='ok'?('<div class="gd-sec"><h4>Saved calibration</h4><div class="gd-kv"><span class="ok">&#10003; Complete.</span> '+E(it.saved||live.detail||'Values saved.')+' Press Next to continue, or Recalibrate to redo it.</div></div>'):'')
       +'<div class="cap-actions">'
       +'<button type="button" class="btn" id="gdstart">'+(live.status==='ok'?'Recalibrate':'Start calibration')+'</button>'
       +'<button type="button" class="btn2" id="gdtest">Test existing calibration</button>'
       +(it.id==='cap_bar'?'<button type="button" class="btn2" id="capTest">Test capacity calibration</button>':'')
       +(it.id==='cue_masks'?'<button type="button" class="btn2" id="gdclear">Clear captured masks</button>':'')
       +'<button type="button" class="btn2" id="gdcode">View code</button>'
       +(live.status==='ok'?(function(){const nx=gdNextTarget(it.id);return nx?('<button type="button" class="btn" id="gdnextc">Next: '+E(nx.title)+' &rarr;</button>'):'';})():'')
       +'</div>'
       +'<div class="gd-out" id="gdout" aria-live="polite">'+(live.status==='ok'?'':E(live.detail||''))+'</div>'
       +kv('Retry',ins.retry_help);
     const bk=$id('gdback');if(bk)bk.onclick=()=>{calNav();renderCal();};
     const nc=$id('gdnextc');if(nc)nc.onclick=()=>{const nx=gdNextTarget(it.id);calNav();if(nx)renderCalDetail(nx.id);else renderCal();};
     const st=$id('gdstart');if(st)st.onclick=()=>gdStart(it);
     const ts=$id('gdtest');if(ts)ts.onclick=()=>gdTest(it);
     // Test Capacity Calibration (cap_bar only): runtime-math probe with
     // the shared PASS/FAIL card; the Recalibrate button re-enters the
     // guided right-end flow.
     const ctb=b.querySelector('#capTest');if(ctb)ctb.onclick=async()=>{
       gdOut('Testing against a fresh screenshot&hellip;');
       let r=null;try{r=await _api().test_capacity();}catch(e){r={ok:false,reasons:[String(e)]};}
       const o=$id('gdout');if(!o)return;
       o.innerHTML=window.__capTestCard?window.__capTestCard(r):(r&&r.ok?'PASS':'FAIL');
       const rb=o.querySelector('#capRecal');if(rb)rb.onclick=()=>gdStart(it);};
     const cl=$id('gdclear');if(cl)cl.onclick=async()=>{
       for(const c of ['PAN','DEPOSIT','SHAKE']){try{await _api().clear_cue_mask(c);}catch(e){}}
       toast('Cleared captured masks');renderCalDetail('cue_masks');};
     const cd=$id('gdcode');if(cd)cd.onclick=()=>{const ref=(it.refs||[])[0];
       gdOut(ref?('Implemented in <b>'+E(ref.module.replace(/\./g,'/'))+'.py</b> &mdash; '+E(ref.symbol)+' ('+E(ref.why)+'). Exact line-anchored links live in the Trust Center.'):'No reference.');};
     // honest example imagery: a real approved capture or a clearly-labelled note
     (async()=>{let ex=null;try{ex=await _api().calibration_example(it.id);}catch(e){}
       const eg=$id('gdeg');if(!eg)return;
       if(ex&&ex.img){eg.innerHTML='<div style="position:relative;display:inline-block"><img src="'+ex.img+'" alt="'+E(ex.alt)+'">'
         +'<svg style="position:absolute;inset:0;width:100%;height:100%" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">'
         +(ex.annotations||[]).map(a=>{if(a.type==='rect')return '<rect x="'+(a.x*100)+'" y="'+(a.y*100)+'" width="'+((a.w||0)*100)+'" height="'+((a.h||0)*100)+'" fill="none" stroke="#ff5b5b" stroke-width="0.6" vector-effect="non-scaling-stroke"/>';
           if(a.type==='point')return '<circle cx="'+(a.x*100)+'" cy="'+(a.y*100)+'" r="1.4" fill="none" stroke="#ff5b5b" stroke-width="0.6"/>';
           return '';}).join('')+'</svg></div>'
         +(ex.annotations||[]).filter(a=>a.label).map(a=>'<div style="margin-top:4px">&#9656; '+E(a.label)+'</div>').join('');}
       else if(ex){eg.innerHTML='Example screenshot: '+(ex.pending_review?'captured, awaiting owner review.':'not yet available in this build.')+' The instructions above are complete without it.';}
       if(CAL&&CAL.owner&&eg){eg.innerHTML+='<div class="cap-actions" style="margin-top:7px"><button type="button" class="btn2" id="gdowncap">Owner: capture example</button>'
         +((ex&&ex.pending_review)?'<button type="button" class="btn2" id="gdownok">Owner: approve</button>':'')
         +((ex&&ex.img)?'<button type="button" class="btn2" id="gdownrev">Owner: revoke approval</button>':'')+'</div>';
         const oc=$id('gdowncap');if(oc)oc.onclick=async()=>{try{const r=await _api().owner_example_capture(it.id);toast(r&&r.ok?('Captured: '+r.note):((r&&r.error)||'failed'));renderCalDetail(it.id);}catch(e){}};
         const ok2=$id('gdownok');if(ok2)ok2.onclick=async()=>{try{await _api().owner_example_approve(it.id,true);toast('Approved');renderCalDetail(it.id);}catch(e){}};
         const rv=$id('gdownrev');if(rv)rv.onclick=async()=>{try{await _api().owner_example_approve(it.id,false);toast('Approval revoked');renderCalDetail(it.id);}catch(e){}};}})();
     $id('supBack').textContent='← Calibration steps';$id('supNext').textContent='Continue to Readiness →';
     $id('supNote').textContent='';b.focus();}
   async function renderCal(){const g=++GEN;PAGE='cal';railSet();calNav();
     CALV.view='list';CALV.item=null;
     const b=$id('supBody');
     b.innerHTML='<p class="sup-sub">Loading calibration&hellip;</p>';
     await calReload();
     if(g!==GEN)return; // the user navigated away while this loaded
     if(!CAL){b.innerHTML='<p class="sup-sub">Calibration engine unavailable.</p><div class="cap-actions"><button type="button" class="btn2" id="calRetry">Retry</button></div>';
       const cr=$id('calRetry');if(cr)cr.onclick=renderCal;return;}
     const win=calItem('roblox_window');
     const req=CAL.items.filter(i=>i.required&&i.id!=='roblox_window');
     const opt=CAL.items.filter(i=>!i.required);
     const sum=CAL.progress||{};
     const card=i=>{const live=i.live||{},[cls,lab]=calStatus(live.status);
       const p=i.prog||{};
       const open=(p.state==='UPCOMING')?'':('<button type="button" class="btn2" data-gd="'+i.id+'">'+(p.state==='COMPLETE'?'Review / redo':'Open this step')+'</button>');
       const disabled=(p.state==='UPCOMING')?('<button type="button" class="btn2" disabled aria-disabled="true" title="'+E(p.reason)+'">Open this step</button>'):'';
       const reason=(p.state==='UPCOMING'||p.state==='NEEDS_REVIEW'||p.state==='BLOCKED')?('<div class="cap-desc"><b>'+(p.state==='UPCOMING'?'Upcoming:':'Review:')+'</b> '+E(p.reason)+'</div>'):'';
       const savedLine=((p.state==='COMPLETE'||(p.state==='OPTIONAL'&&live.status==='ok'))&&i.saved)?('<div class="cap-desc"><b>Saved:</b> '+E(i.saved)+' &middot; open the step to review or recalibrate, or just carry on.</div>'):'';
       return '<div class="cap-card'+stepClass(p)+'" data-calid="'+i.id+'" aria-label="Step '+(p.seq||0)+': '+E(i.title)+' - '+E((p.state||'').toLowerCase().replace('_',' '))+'"><div class="cap-head">'
         +(p.seq?('<span class="step-num" aria-hidden="true">'+p.seq+'</span>'):'')
         +'<span class="cap-title">'+E(i.title)+'</span>'
         +(i.required?'<span class="cap-badge req">Required</span>':'<span class="cap-badge opt">Optional</span>')+stepChip(p)
         +'<span class="cap-st '+cls+'"><span class="dot"></span>'+E(lab)+'</span></div>'
         +'<div class="cap-desc">'+E(i.purpose)+' '+E(live.detail||'')+'</div>'+savedLine+reason
         +'<div class="cap-actions">'+open+disabled+'<button type="button" class="btn2" data-cal="code" data-item="'+i.id+'">View code</button></div>'
         +'<div class="cap-test" id="caltest_'+i.id+'" aria-live="polite"></div></div>';};
     const wlive=(win&&win.live)||{};
     b.innerHTML='<h2 class="sup-h1">Guided Calibration</h2>'
       +'<p class="sup-sub">Work through the numbered steps in order - the highlighted card is the one to do next. Every step runs the same calibration engine and the same save file as the Calibrate tab (there is exactly one set of values); the guided pages just walk you through it without leaving setup.</p>'
       +'<div class="sup-progress" aria-live="polite">Required calibration: '+(sum.done||0)+' of '+(sum.total||0)+' complete'+(sum.active?'':' - all done')+'</div>'
       +'<div class="cal-pre"><span><b>Before you calibrate:</b> '+E((win&&win.purpose)||'Roblox must be visible.')+' <span id="calwin">'+E(wlive.detail||'')+'</span></span>'
       +'<button type="button" class="btn2" data-cal="detect" data-item="roblox_window">Detect Roblox window</button>'
       +'<span class="cap-test" id="caltest_roblox_window" aria-live="polite"></span></div>'
       +'<div class="sup-group">Required - in order</div>'+req.map(card).join('')
       +'<div class="sup-group">Optional - only for the features you turn on</div>'+opt.map(card).join('');
     b.querySelectorAll('button[data-gd]').forEach(btn=>{btn.onclick=()=>renderCalDetail(btn.dataset.gd);});
     b.querySelectorAll('button[data-cal]').forEach(btn=>{btn.onclick=async()=>{
       const item=btn.dataset.item, act=btn.dataset.cal, out=$id('caltest_'+item);
       const show=h=>{if(out){out.classList.add('show');out.innerHTML=h;}};
       const calErr=r=>{let h='&#10007; '+E((r&&r.error)||'failed')+((r&&r.error_code)?(' ['+E(r.error_code)+']'):'');
         if(r&&r.needs_permission)h+=' <button type="button" class="btn2" data-goperm="1">Open Trust &amp; Permissions</button>';
         show(h);
         if(out){const gp=out.querySelector('button[data-goperm]');if(gp)gp.onclick=()=>renderTrust();}};
       if(act==='detect'){show('Looking for the Roblox window&hellip;');
         let r={};try{r=await _api().detect_roblox();}catch(e){r={found:false,error:String(e)};}
         show(r&&r.found?('&#10003; Found: '+E(r.w+'×'+r.h)+' at ('+E(r.x+', '+r.y)+')')
           :('&#10007; Not found &mdash; '+E((r&&r.error)||'open Roblox on your primary display, then retry.')));
         renderCalSoon();return;}
       if(act==='code'){const it=calItem(item)||{};const ref=(it.refs||[])[0];
         show(ref?('Implemented in <b>'+E(ref.module.replace(/\./g,'/'))+'.py</b> &mdash; '+E(ref.symbol)+' ('+E(ref.why)+'). Exact line-anchored links live in the Trust Center.'):'No reference.');return;}
     };});
     const act=sum.active?b.querySelector('.cap-card[data-calid="'+sum.active+'"]'):null;
     if(act&&act.scrollIntoView)try{act.scrollIntoView({block:'nearest'});}catch(e){}
     $id('supBack').textContent='← Trust & Permissions';$id('supNext').textContent='Continue to Readiness →';
     $id('supNote').textContent=CAL.ready?'Required calibration is covered - you can continue.':'Do this next: '+((calItem(sum.active)||{}).title||CAL.blockers.join(', '));
     b.focus();}
   // NEVER while a guided DETAIL page owns the surface: the bridge fires
   // __calRefresh BEFORE __calDone on every confirm, and a deferred
   // checklist re-render here would yank the detail page mid-plan (multi
   // stage captures) or wipe a failure state the spec requires to stay.
   // The detail flow refreshes itself through onGdDone/renderCalDetail.
   let _calTimer=null;function renderCalSoon(){if(PAGE!=='cal'||CALV.view==='detail')return;clearTimeout(_calTimer);_calTimer=setTimeout(()=>{if(PAGE==='cal'&&CALV.view!=='detail')renderCal();},600);}
   const _prevCalRefresh=window.__calRefresh;
   window.__calRefresh=function(){if(_prevCalRefresh)try{_prevCalRefresh();}catch(e){}renderCalSoon();};
   async function renderReady(){const g=++GEN;PAGE='ready';railSet();const b=$id('supBody');
     b.innerHTML='<p class="sup-sub">Running the readiness checks&hellip;</p>';
     let rc=null;try{rc=await _api().readiness_check();}catch(e){}
     let dgv=[];try{const dg=await _api().diagnostics_state();
       if(dg&&Array.isArray(dg.events))dgv=dg.events;}catch(e){}
     if(g!==GEN)return; // the user navigated away while the checks ran
     if(!rc){b.innerHTML='<p class="sup-sub">Readiness check unavailable.</p><div class="cap-actions"><button type="button" class="btn2" id="rdyRetry">Retry</button></div>';
       const rr=$id('rdyRetry');if(rr)rr.onclick=renderReady;return;}
     // failed rows get a Details button when a diagnostic event matches
     // (permission / calibration codes) -- it opens the warning drawer
     const dmatch=i=>{const cal=c=>/^PP-D-(CAP-|CAL-|CUE-)/.test(c);
       for(const e of dgv){const c=e.code||'';
         if(i.id==='screen_detection'&&c==='PP-D-PERM-SCREEN_DETECTION')return e;
         if(i.id==='input_control'&&c==='PP-D-PERM-INPUT_CONTROL')return e;
         if(i.id==='stop_hotkeys'&&c==='PP-D-SAFESTOP')return e;
         if((i.id==='calibration'||i.id==='cue_masks')&&cal(c))return e;}
       return null;};
     const row=i=>{const m={pass:'PASS',fail:'FAIL',warn:'WARN',info:'INFO'};
       const fix=i.fix?('<button type="button" class="btn2" data-fix="'+E(i.fix)+'" data-fixitem="'+E(i.id)+'">Fix now</button>'):'';
       const dev=(i.status==='fail')?dmatch(i):null;
       const det=dev?('<button type="button" class="btn2" data-diag="'+E(dev.id)+'">Details</button>'):'';
       return '<div class="rdy-item '+E(i.status)+'"><span class="mark">'+(m[i.status]||'?')+'</span>'
         +'<div><div class="t">'+E(i.title)+'</div><div class="d">'+E(i.detail)+'</div></div>'+fix+det+'</div>';};
     b.innerHTML='<h2 class="sup-h1">Readiness Check</h2>'
       +'<p class="sup-sub">'+(rc.ok?'Everything required passed. You are ready to prospect.':'Some required items need attention. You can still enter the app &mdash; only Start Macro stays disabled until they pass, and the Run tab will say exactly why.')+'</p>'
       +rc.items.map(row).join('')
       +'<div class="cap-actions" style="margin-top:14px">'
       +'<button type="button" class="btn2" id="rdyRetest">Retest</button>'
       +'<button type="button" class="btn2" id="rdyDiag">Export diagnostics</button>'
       +'<button type="button" class="btn2" id="rdyCopy">Copy diagnostic summary</button>'
       +'<button type="button" class="btn2" id="rdyLog">Open wizard log</button>'
       +'<button type="button" class="btn2" id="rdyFolder">Open data folder</button>'
       +'<button type="button" class="btn2" data-faq-open="">FAQ &amp; troubleshooting</button>'
       +'<button type="button" class="btn2" id="rdyQuit">Exit app</button></div>';
     // Fix Now opens the EXACT wizard step: a calibration failure whose id
     // is a registry item deep-links to its guided detail page; everything
     // else lands on the right wizard page. Never the normal Calibrate tab.
     b.querySelectorAll('button[data-fix]').forEach(f=>{f.onclick=()=>{
       if(f.dataset.fix==='trust')renderTrust();
       else if(f.dataset.fix==='calibration'){
         const iid=f.dataset.fixitem;
         if(iid&&iid!=='calibration')renderCalDetail(iid); // unknown ids fall back to the checklist inside renderCalDetail
         else renderCal();}};});
     b.querySelectorAll('button[data-diag]').forEach(f=>{f.onclick=()=>{
       if(window.openDiagDrawer)openDiagDrawer(f.dataset.diag);};});
     const rt=$id('rdyRetest');if(rt)rt.onclick=renderReady;
     const dg=$id('rdyDiag');if(dg)dg.onclick=async()=>{try{const r=await _api().export_diagnostics();if(r&&r.ok)toast('Saved '+r.path);else if(r&&r.error)toast('Export failed: '+r.error);}catch(e){}};
     const cp=$id('rdyCopy');if(cp)cp.onclick=async()=>{try{const r=await _api().diag_summary();
       if(r&&r.ok){try{await navigator.clipboard.writeText(r.text);toast('Summary copied');}
         catch(e){toast('Clipboard unavailable — use Export diagnostics');}}
       else toast('Summary failed: '+((r&&r.error)||''));}catch(e){}};
     const lg=$id('rdyLog');if(lg)lg.onclick=()=>{try{_api().open_wizard_log();}catch(e){}};
     const fo=$id('rdyFolder');if(fo)fo.onclick=()=>{try{_api().open_data_folder();}catch(e){}};
     const qb=$id('rdyQuit');if(qb)qb.onclick=()=>{mconfirm('Quit Prospector Lite? Setup progress is saved and resumes next launch.',()=>{try{_api().quit_app();}catch(e){}});};
     $id('supBack').textContent='← Calibration';$id('supNext').textContent=rc.ok?'Finish setup →':'Enter the app anyway →';
     $id('supNote').textContent='';b.focus();}
   // refresh(): fetch a fresh trust snapshot and PATCH the visible cards in
   // place (wizard trust step + Trust Center). Never a destructive full
   // re-render -- page entries call renderTrust() themselves. A sequence
   // counter drops out-of-order results so a slow older fetch can not
   // overwrite a newer one.
   let _seq=0, GEN=0;
   async function refresh(render){const my=++_seq;let st=null;
     try{st=await _api().trust_state();}catch(e){st=null;}
     if(!st||my!==_seq)return false;
     if(st.seq&&ST&&ST.seq&&st.seq<ST.seq)return false; // server-side ordering too
     ST=st;DET=ST.platform||'mac';
     const s=$id('setup');
     if(s&&s.classList.contains('show')&&PAGE==='trust')updateCards($id('supBody'));
     const tc=$id('tccaps');if(tc)updateCards(tc);
     stampChecked();
     return true;}
   // Bounded post-action poll: armed by Request access / Open System
   // Settings / check-again, runs every 2.5 s for at most 90 s and ONLY
   // while a trust surface is actually visible. Complements (never
   // replaces) the manual Refresh button and the focus watcher below.
   let _pollT=null,_pollUntil=0;
   function trustVisible(){const s=$id('setup');
     return (s&&s.classList.contains('show')&&PAGE==='trust')||!!document.querySelector('#ptrust.active');}
   function armPoll(){_pollUntil=Date.now()+90000;
     if(_pollT)return;
     _pollT=setInterval(()=>{
       if(Date.now()>_pollUntil||!trustVisible()){clearInterval(_pollT);_pollT=null;return;}
       refresh(false);},2500);}
   // Focus watcher: pywebview exposes no app-activation event Python-side
   // and the JS window 'focus' event is not guaranteed across returns from
   // System Settings, so a cheap document.hasFocus() transition check is
   // the reliable trigger. It only acts when a trust surface is visible.
   let _hadFocus=document.hasFocus();
   setInterval(()=>{const f=document.hasFocus();
     if(f&&!_hadFocus){
       if(trustVisible())refresh(false);
       const s=$id('setup');
       if(s&&s.classList.contains('show')&&PAGE==='ready')renderReady();
     }
     _hadFocus=f;},800);
   window.SETUP={
     open:async function(page){await refresh(false);PLAT=DET==='win'?'win':'mac';
       const s=$id('setup');if(s)s.classList.add('show');$id('supReturn').classList.remove('show');
       // a wizard visit makes the next return to the app a FRESH entry --
       // the main tutorial may auto-open again after this closes
       if(window.__tutEntryReset)window.__tutEntryReset();
       try{_api().onboarding_mark('TRUST_STARTED');}catch(e){}
       if(page==='cal')renderCal();else if(page==='ready')renderReady();else renderTrust();},
     resume:function(state){const map={NOT_STARTED:'trust',WELCOME_COMPLETE:'trust',TRUST_STARTED:'trust',TRUST_COMPLETE:'cal',CALIBRATION_STARTED:'cal',CALIBRATION_COMPLETE:'ready',READINESS_COMPLETE:'ready'};
       this.open(map[state]||'trust');},
     suspend:function(){const s=$id('setup');if(s)s.classList.remove('show');$id('supReturn').classList.add('show');
       if(document.body.dataset.welinit!=='1')_startApp();},
     calRefresh:renderCalSoon};
   $id('supReturn').onclick=()=>{$id('supReturn').classList.remove('show');const s=$id('setup');if(s)s.classList.add('show');
     if(PAGE==='cal')renderCal();else if(PAGE==='ready')renderReady();else renderTrust();};
   $id('supBack').onclick=()=>{if(PAGE==='trust'){const s=$id('setup');if(s)s.classList.remove('show');welcomeShow();}
     else if(PAGE==='cal')renderTrust();else renderCal();};
   $id('supNext').onclick=async()=>{
     if(PAGE==='trust'){try{await _api().onboarding_mark('TRUST_COMPLETE');}catch(e){}renderCal();}
     else if(PAGE==='cal'){try{await _api().onboarding_mark('CALIBRATION_STARTED');await _api().onboarding_mark('CALIBRATION_COMPLETE');}catch(e){}renderReady();}
     else{try{await _api().onboarding_mark('READINESS_COMPLETE');await _api().onboarding_mark('FINISHED','wizard');}catch(e){}
       const s=$id('setup');if(s)s.classList.remove('show');$id('supReturn').classList.remove('show');_startApp();
       // _startApp is a no-op when the app already booted behind the wizard,
       // so trigger the main-tutorial entry check explicitly: the wizard is
       // closing, which is a fresh main-app entry (maybeStartTour re-checks
       // the live gates + the auto-open pref; a second call is harmless --
       // TUT_ENTRY_SHOWN makes it once per entry).
       setTimeout(()=>{if(window.maybeStartTour)maybeStartTour();},1000);}};
   window.__setupRerun=async function(){try{await _api().onboarding_rerun();}catch(e){}SETUP.open('trust');};
   // the warning drawer's navigateToCalibration routes here while the
   // wizard is open (the readiness Fix-now precedent): the guided detail
   // page for a registry item, checklist fallback for unknown ids
   window.__wizCalDetail=function(id){try{
     if(id&&id!=='calibration')renderCalDetail(id);else renderCal();
   }catch(e){}};
   // Window-focus fast path: NON-destructive card patch (a full re-render
   // here used to wipe an in-flight test the instructions told the user to
   // run while switching apps). The hasFocus() watcher above is the
   // fallback when this event does not fire.
   window.addEventListener('focus',()=>{if(trustVisible())refresh(false);});
   // ---- Trust Center tab ----
   window.__tcRender=async function(){const box=$id('tcbody');if(!box)return;
     box.innerHTML='<p class="chint">Loading&hellip;</p>';
     let st=null,man=null,dm=null,ws=null,tut=null;try{st=await _api().trust_state();}catch(e){}
     try{man=await _api().trust_manifest();}catch(e){}
     try{dm=await _api().data_manifest();}catch(e){}
     try{ws=await _api().welcome_state();}catch(e){}
     try{tut=await _api().tutorial_state();}catch(e){}
     if(!st){box.innerHTML='<p class="chint">Trust state unavailable.</p>';return;}
     ST=st;DET=st.platform||'mac';PLAT=DET==='win'?'win':'mac';
     const id=st.identity||{};
     const kv=o=>'<div class="tc-kv">'+Object.keys(o).map(k=>'<b>'+E(k)+'</b><span>'+E(o[k])+'</span>').join('')+'</div>';
     let h='';
     h+='<div class="tc-sec"><h3>Permissions &amp; capability tests</h3><div class="cap-desc">Live status from the operating system; every Test runs the real capability.</div><div id="tccaps">'
       +st.capabilities.filter(c=>(c.platforms||[]).indexOf(PLAT)>=0).map(c=>capCard(c,false)).join('')+'</div></div>';
     h+='<div class="tc-sec"><h3>Build identity</h3>'+kv({
       'Version':id.version||'','Commit':(id.commit||'')+(id.dirty?' (built from modified source)':''),
       'Built':id.date||'(source run)','Platform':(id.os||'')+' / '+(id.arch||''),
       'Package':id.package||'','Signed':id.signed?'yes':'no (unsigned build)',
       'Notarized':id.notarized?'yes':'no','Licence':id.licence_status||''})+'</div>';
     h+='<div class="tc-sec"><h3>Source code</h3><div class="cap-desc">'
       +(id.project_url?('Repository: <a href="#" id="tcrepo">'+E(id.project_url)+'</a> &mdash; every View Code button opens the file at commit '+E(id.commit_short||'')+', never a moving branch.')
       :('No public repository URL is configured in this build, so View Code buttons show the exact local file + symbol + commit instead. The trust manifest below is generated from the exact source of this build.'))
       +'</div>'+(man&&man.capabilities?('<details class="cap-more"><summary>Trust manifest ('+man.capabilities.length+' capabilities, commit '+E((man.generated_from||'').slice(0,12))+')</summary><pre class="tc-pre">'+E(JSON.stringify(man,null,1))+'</pre></details>'):'')+'</div>';
     h+='<div class="tc-sec"><h3>Network behaviour</h3><div class="cap-desc">Normal startup and macro use make ZERO network requests. The only outbound paths are the two optional, off-by-default features above (your own Discord webhook, your own Coach AI key) and links you click. TLS certificate verification can never be disabled. There is no update check, no analytics, no telemetry.</div></div>';
     h+='<div class="tc-sec"><h3>Roblox safety boundary</h3><div class="cap-desc">Prospector Lite never injects into Roblox, never reads or writes another process’s memory, never modifies game files and never intercepts network traffic. It sees pixels and presses ordinary keys — the same boundary a human at the keyboard has. Verify: the source scans in public_release_tests.py fail the build if any process-memory API appears.</div></div>';
     if(dm){h+='<div class="tc-sec"><h3>Local data</h3><div class="cap-desc">Everything lives in <span class="cal-keys">'+E(dm.dir)+'</span> — nothing is written into the app bundle.</div>'
       +'<table class="tc-files">'+dm.files.map(f=>'<tr><td>'+E(f.name)+'</td><td>'+E(f.purpose)+'</td><td>'+(f.bytes>1048576?((f.bytes/1048576).toFixed(1)+' MB'):((f.bytes/1024).toFixed(1)+' KB'))+'</td></tr>').join('')+'</table>'
       +'<div class="cap-actions" style="margin-top:10px">'
       +'<button type="button" class="btn2" id="tcOpen">Open data folder</button>'
       +'<button type="button" class="btn2" id="tcExpCal">Export calibration</button>'
       +'<button type="button" class="btn2" id="tcDiag">Export diagnostics</button>'
       +'<button type="button" class="btn2" id="tcDelHist">Delete history</button>'
       +'<button type="button" class="btn2" id="tcDelLogs">Delete logs</button>'
       +'<button type="button" class="btn2 tc-danger" id="tcDelAll">Delete ALL local data&hellip;</button>'
       +'</div></div>';}
     h+='<div class="tc-sec"><h3>Setup wizard</h3><div class="cap-desc">Re-run the full first-run wizard (trust, calibration, readiness) any time. Re-running deletes nothing.</div>'
       +'<div class="cap-actions"><button type="button" class="btn2" id="tcRerun">Re-run setup wizard</button>'
       +'<button type="button" class="btn2" id="tcResetOb">Reset wizard progress only</button></div>'
       +'<label class="row" style="margin-top:10px"><span class="lbl">Skip the setup wizard automatically on launch</span>'
       +'<span class="switch"><input type="checkbox" id="tcSkipAuto"'+((ws&&ws.skip_wizard_automatically)?' checked':'')+'>'
       +'<span class="track"><span class="knob"></span></span></span></label>'
       +'<label class="row" style="margin-top:6px"><span class="lbl">Open tutorial whenever Prospector Lite opens</span>'
       +'<span class="switch"><input type="checkbox" id="tcTutAuto"'+((tut&&tut.auto_open===false)?'':' checked')+'>'
       +'<span class="track"><span class="knob"></span></span></span></label></div>';
     h+='<div class="tc-sec"><h3>Help &amp; troubleshooting</h3><div class="cap-desc">Common problems (permissions, calibration, detection, tuning) with exact fixes and deep links to the right surface. Warnings hidden with &ldquo;Don&rsquo;t show again&rdquo; can be restored here.</div>'
       +'<div class="cap-actions"><button type="button" class="btn2" data-faq-open="">FAQ &amp; troubleshooting</button>'
       +'<button type="button" class="btn2" id="tcUnsup">Show suppressed warnings again</button></div></div>';
     h+='<div class="tc-sec"><h3>Security reporting</h3><div class="cap-desc">Found a vulnerability? SECURITY.md explains how to report it privately.</div>'
       +'<div class="cap-actions"><button type="button" class="btn2" id="tcSec">Open SECURITY.md</button>'
       +'<button type="button" class="btn2" id="tcPriv">Open PRIVACY.md</button>'
       +'<button type="button" class="btn2" id="tcPerm">Open PERMISSIONS.md</button></div></div>';
     box.innerHTML=h;wireCards(box);
     const w=(i,f)=>{const el=$id(i);if(el)el.onclick=f;};
     w('tcOpen',()=>{try{_api().open_data_folder();}catch(e){}});
     w('tcExpCal',async()=>{try{const r=await _api().export_calibration();if(r&&r.ok)toast('Saved '+r.path);}catch(e){}});
     w('tcDiag',async()=>{try{const r=await _api().export_diagnostics();if(r&&r.ok)toast('Saved '+r.path);}catch(e){}});
     w('tcDelHist',()=>{mconfirm('Delete run history and all run logs?',async()=>{try{await _api().delete_local_data('history');toast('History deleted');__tcRender();}catch(e){}});});
     w('tcDelLogs',()=>{mconfirm('Delete all run logs?',async()=>{try{await _api().delete_local_data('logs');toast('Logs deleted');__tcRender();}catch(e){}});});
     w('tcDelAll',()=>{mconfirm('Delete ALL Prospector Lite data on this computer - settings, calibration, builds, scripts, history, secrets? This cannot be undone.',()=>{mconfirm('Really delete everything? The app returns to a fresh first run.',async()=>{try{await _api().delete_local_data('all');toast('All local data deleted');__tcRender();}catch(e){}});});});
     w('tcUnsup',async()=>{try{await _api().diag_unsuppress_all();toast('Suppressed warnings restored');}catch(e){}
       if(window.refreshDiagnostics)refreshDiagnostics(true);});
     w('tcRerun',()=>{if(window.__setupRerun)__setupRerun();});
     w('tcResetOb',()=>{mconfirm('Reset the setup wizard to a fresh first run? Builds, calibration and settings are NOT touched.',async()=>{try{await _api().onboarding_reset();toast('Wizard reset - it will open on next launch');}catch(e){}});});
     const tsk=$id('tcSkipAuto');if(tsk)tsk.onchange=async()=>{const want=!!tsk.checked;
       let r=null;try{r=await _api().wizard_skip_pref(want);}catch(e){r={ok:false,error:String(e)};}
       if(!r||!r.ok){tsk.checked=!want;toast('Could not save this preference ['+((r&&r.error_code)||'PP-SKIP-SAVE')+']');}};
     const tta=$id('tcTutAuto');if(tta)tta.onchange=async()=>{const want=!!tta.checked;
       let r=null;try{r=await _api().tutorial_set_auto_open(want);}catch(e){r={ok:false,error:String(e)};}
       if(!r||!r.ok){tta.checked=!want;toast('Could not save this preference ['+((r&&r.error_code)||'PP-TUT-AUTO')+']');}};
     w('tcSec',()=>{try{_api().open_doc('SECURITY.md');}catch(e){}});
     w('tcPriv',()=>{try{_api().open_doc('PRIVACY.md');}catch(e){}});
     w('tcPerm',()=>{try{_api().open_doc('PERMISSIONS.md');}catch(e){}});
     const rp=$id('tcrepo');if(rp)rp.onclick=e=>{e.preventDefault();try{_api().open_external(id.project_url);}catch(_){}};};
 })();

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
       +'<div class="cdiff-state ok">✓ Applied, press Save settings to keep</div><div class="cdiff-state no">Not applied</div>'
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
     let res;try{res=await capi().assistant_chat(text, prevTopic, stats||null, buildConvo());}catch(e){res={reply:'I could not reach the engine, try reopening the app.',changes:[],chips:[]};}
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
   function openCoach(){body.classList.remove('prev-on');var pt=document.getElementById('prevtoggle');if(pt)pt.classList.remove('on');body.classList.add('coach-on');tgl.classList.add('on');greet();setTimeout(()=>inp.focus(),60);}
   function closeCoach(){body.classList.remove('coach-on');body.classList.remove('coach-expand');tgl.classList.remove('on');if(window.__showPreviewPanel)window.__showPreviewPanel(true);}
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
     anthropic:{base:'',models:[['claude-haiku-4-5-20251001','Haiku 4.5, cheapest'],['claude-sonnet-5','Sonnet 5, smartest']]},
     openai:{base:'',models:[['gpt-5.4-mini','GPT-5.4-mini, cheap'],['gpt-5.4','GPT-5.4'],['gpt-4o-mini','GPT-4o-mini, safe fallback']]},
     gemini:{base:'https://generativelanguage.googleapis.com/v1beta/openai',models:[['gemini-2.5-flash-lite','2.5 Flash-Lite, cheapest'],['gemini-2.5-flash','2.5 Flash']]},
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
     if(keyIn)keyIn.placeholder=c.has_key?'•••••• key saved, type to replace':'paste key, stays on this PC';
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
     if(keyIn)keyIn.value='';loadCfg();if(window.toast)toast('API key cleared, back to offline');};
   // Coach no longer auto-opens; the Preview panel owns that space by default.
   function boot(){loadCfg();}
   if(window.pywebview&&window.pywebview.api)boot();
   else window.addEventListener('pywebviewready',boot);
 })();
</script></body></html>"""


ANALYTICS_HTML = r'''<!doctype html><html><head><meta charset="utf-8"><style>
 :root{--bg:#171310;--bg2:#1c1714;--panel:#221c18;--line:#332a23;--line2:#463a2f;
   --txt:#ece0d0;--mut:#9c8e7c;--dim:#6a5d4d;--accent:#a8794a;--accent-lit:#caa06e;--accent2:#8a9b6a;}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--txt);font:13px -apple-system,"Segoe UI",sans-serif}
 .ahead{display:flex;align-items:center;gap:10px;padding:13px 18px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:2}
 .ahead .t{font-size:15px;font-weight:700}.ahead .t b{color:var(--accent-lit)}
 .ahead .rs{margin-left:auto;color:var(--mut);font-size:12px}
 .awrap{padding:6px 18px 30px;background:var(--bg2) url("data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIzMjAiIGhlaWdodD0iMzAwIj48cmVjdCB3aWR0aD0iMzIwIiBoZWlnaHQ9IjMwMCIgZmlsbD0iIzE2MTIxMCIvPjxmaWx0ZXIgaWQ9ImciPjxmZVR1cmJ1bGVuY2UgdHlwZT0iZnJhY3RhbE5vaXNlIiBiYXNlRnJlcXVlbmN5PSIwLjg1IDAuMDE0IiBudW1PY3RhdmVzPSIzIiBzZWVkPSIxMSIgc3RpdGNoVGlsZXM9InN0aXRjaCIgcmVzdWx0PSJuIi8+PGZlQ29sb3JNYXRyaXggaW49Im4iIHR5cGU9Im1hdHJpeCIgdmFsdWVzPSIwIDAgMCAwIDAuMTQgMCAwIDAgMCAwLjExNSAwIDAgMCAwIDAuMDkgMCAwIDAgMC4xNiAwIi8+PC9maWx0ZXI+PHJlY3Qgd2lkdGg9IjMyMCIgaGVpZ2h0PSIzMDAiIGZpbHRlcj0idXJsKCNnKSIvPjxnIHN0cm9rZT0iIzBkMGEwNyIgc3Ryb2tlLXdpZHRoPSIyIiBvcGFjaXR5PSIwLjg1Ij48bGluZSB4MT0iMCIgeTE9IjYwIiB4Mj0iMzIwIiB5Mj0iNjAiLz48bGluZSB4MT0iMCIgeTE9IjEyMCIgeDI9IjMyMCIgeTI9IjEyMCIvPjxsaW5lIHgxPSIwIiB5MT0iMTgwIiB4Mj0iMzIwIiB5Mj0iMTgwIi8+PGxpbmUgeDE9IjAiIHkxPSIyNDAiIHgyPSIzMjAiIHkyPSIyNDAiLz48L2c+PGcgc3Ryb2tlPSIjMmIyMTE5IiBzdHJva2Utd2lkdGg9IjEiIG9wYWNpdHk9IjAuNTUiPjxsaW5lIHgxPSIwIiB5MT0iNjIiIHgyPSIzMjAiIHkyPSI2MiIvPjxsaW5lIHgxPSIwIiB5MT0iMTIyIiB4Mj0iMzIwIiB5Mj0iMTIyIi8+PGxpbmUgeDE9IjAiIHkxPSIxODIiIHgyPSIzMjAiIHkyPSIxODIiLz48bGluZSB4MT0iMCIgeTE9IjI0MiIgeDI9IjMyMCIgeTI9IjI0MiIvPjwvZz48L3N2Zz4=");background-size:320px 300px}

 .asec{color:var(--accent-lit);font-weight:700;font-size:11px;letter-spacing:.05em;text-transform:uppercase;margin:16px 2px 8px}
 .agrid{display:grid;grid-template-columns:repeat(4,1fr);gap:9px}
 .acard{background:var(--panel);border:1px solid var(--line2);border-radius:11px;padding:11px 13px;box-shadow:inset 0 1px 0 rgba(255,255,255,.04),0 3px 9px -4px rgba(0,0,0,.55)}
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
 .acard[data-tip],.asec[data-tip],.arow[data-tip]{cursor:help}
 .acard[data-tip]:hover,.asec[data-tip]:hover{border-color:var(--accent,#c2924c)}
 #atip{position:fixed;display:none;max-width:320px;background:var(--panel);color:var(--mut);border:1px solid var(--line2);border-radius:11px;padding:11px 13px;font-size:12.5px;line-height:1.65;box-shadow:0 18px 50px -14px rgba(0,0,0,.85);z-index:50;pointer-events:none}
 #atip b{color:var(--txt)}
 </style></head><body>
 <div class="ahead"><div class="t">Prospectors <b>Analytics</b></div><span class="rs" id="ars">idle</span></div>
 <div class="awrap"><div id="aroot"></div></div>
 <div id="atip"></div>
 <script>
 var ATIP={
  'Throughput':'<b>Throughput</b> is raw speed: how many pans you are completing and how fast. If these numbers are lower than you want, the fix is on the Cycle page, trim the widest blocks on the timeline.',
  'Pans':'<b>Pans</b> is the total completed loops this session (dig, walk, shake, land). It is the number every other stat is measured against. The clean % beside it is the share that ran with no nudges or recoveries.',
  'Pans / hr':'<b>Pans per hour</b> at the current pace, with the session runtime beside it. It swings early and settles as runtime grows, so give it a few minutes before trusting it. This is the headline speed number to tune against.',
  'Cycle time':'<b>Cycle time</b> is how long one pan takes on average, with p50 (median), p95 (the slow tail) and your last pan beside it. A big gap between p50 and p95 means some pans are stalling, usually recovery or hunting for land.',
  'Digs (registered)':'<b>Digs</b> counts every dig that registered, including probe digs, with digs per pan and total clicks beside it. Far more digs than pans means it is probing or re-digging too much: fix momentum (Glide and start) or turn on Smart fill wait.',
  'Earnings':'<b>Earnings</b> is measured money and shards, read off the HUD. It needs earnings tracking on and the Money and Shards regions calibrated. It lets you compare builds on real income instead of just pans per hour. macOS only.',
  'Money':'<b>Money</b> earned this session, with per-hour and per-pan beside it. It is credited as the gain between HUD reads, so spending mid-run is ignored. Needs Track money and shards on and the Money region drawn.',
  'Shards':'<b>Shards</b> earned this session, with per-hour and per-pan beside it. Same idea as money but for the shard total. Needs earnings tracking on and the Shards region drawn. The number to watch on a shard farm.',
  'Loot value (kept)':'<b>Loot value</b> is an estimate of what the finds you KEEP are worth, using item prices times your sell boost. It is separate from Money (which is coins already sold). Together they make the TOTAL per hour figure.',
  'TOTAL / hr':'<b>Total per hour</b> adds the money you auto-sold to the estimated value of the loot you kept, giving the fullest read on what a build actually earns per hour. This is the best single number for comparing builds.',
  'Finds':'<b>Finds</b> summarises what you dug up: item count, total kilograms, and your heaviest single find. The rarity and modifier chips below break it down. Needs finds tracking on and the Find region drawn. macOS only.',
  'Reliability':'<b>Reliability</b> is how smoothly the run went. High recoveries, nudges or shake misses point at specific Cycle stages to tune. A clean run sits high on clean cycles and near zero on the rest.',
  'Clean cycles':'<b>Clean cycles</b> is the share of pans that ran start to finish with no nudges, retries or recoveries, with the raw count beside it. A tuned build sits high. A falling clean % tells you a problem is creeping in.',
  'Recoveries':'<b>Recoveries</b> counts how often the recovery ladder stepped in, with nudges and shake misses beside it. Climbing nudges means landing short (fix momentum), climbing shake misses means the pan is not emptying (fix the Shake stage).',
  'Shake retries':'<b>Shake retries</b> counts shakes that had to be re-attempted, with outright fails beside it. A few is normal on any build. A lot means shakes are not starting or not emptying: look at the Shake and Glide stages and the capacity calibration.',
  'Stops / pauses':'<b>Stops and pauses</b> shows safe stops versus hard stops, with pauses and relics placed beside it. Safe stops that recover are fine; repeated hard stops usually mean calibration or the spot. Check the History timeline for the reason.',
  'rarity':'<b>Rarity breakdown</b> of your finds this session, most common first. It shows how your luck is shaping the drop mix. Needs finds tracking on.',
  'modifier':'<b>Modifier breakdown</b> of your finds, most common first. Modified ores (Perfect, Mutated, Iridescent and so on) are worth much more, so this is a quick read on modifier luck. Needs finds tracking on.',
  'Find log':'<b>Find log</b> is the running list of every item detected this session with its time, name, weight, rarity and estimated value. A question mark means a low-confidence read; an x-number means a duplicate proc. Needs finds tracking on.'
 };
 (function(){var tip=document.getElementById('atip');if(!tip)return;
   document.addEventListener('mouseover',function(e){var el=e.target.closest&&e.target.closest('[data-tip]');if(!el)return;
     tip.innerHTML=ATIP[el.getAttribute('data-tip')]||el.getAttribute('data-tip');tip.style.display='block';
     var r=el.getBoundingClientRect();var x=r.left,y=r.bottom+8;
     if(x+330>window.innerWidth)x=window.innerWidth-334;if(y+tip.offsetHeight>window.innerHeight-8)y=r.top-tip.offsetHeight-8;
     tip.style.left=Math.max(8,x)+'px';tip.style.top=Math.max(8,y)+'px';});
   document.addEventListener('mouseout',function(e){if(e.target.closest&&e.target.closest('[data-tip]'))tip.style.display='none';});})();
 window.fmtBig=window.fmtBig||function(n){n=Number(n)||0;const a=Math.abs(n);
   if(a>=1e15)return (n/1e15).toFixed(2)+'Q';if(a>=1e12)return (n/1e12).toFixed(2)+'T';
   if(a>=1e9)return (n/1e9).toFixed(2)+'B';if(a>=1e6)return (n/1e6).toFixed(2)+'M';
   if(a>=1e3)return (n/1e3).toFixed(1)+'K';return String(Math.round(n));};

window._analyticsFinds=window._analyticsFinds||[];
window.renderAnalytics=function(root,s,finds){
  if(!root)return;
  const B=window.fmtBig||(x=>String(x));
  const fmtS=v=>{v=Math.max(0,Math.round(v||0));var h=Math.floor(v/3600),m=Math.floor((v%3600)/60),ss=String(v%60).padStart(2,'0');return h>0?(h+':'+String(m).padStart(2,'0')+':'+ss):(m+':'+ss);};
  s=s||{};finds=finds||[];
  const card=(l,v,sub)=>'<div class="acard" data-tip="'+l+'"><div class="al">'+l+'</div><div class="av">'+v+'</div>'+(sub?'<div class="as">'+sub+'</div>':'')+'</div>';
  const sec=(l,extra)=>'<div class="asec" data-tip="'+l+'">'+(extra||l)+'</div>';
  let h='';
  h+=sec('Throughput')+'<div class="agrid">';
  h+=card('Pans',s.cycles||0,(s.clean_pct!=null?s.clean_pct+'% clean':''));
  h+=card('Pans / hr',s.pans_per_hr||0,'runtime '+fmtS(s.runtime_s));
  h+=card('Cycle time',(s.cyc_mean_s||0)+'s','p50 '+(s.cyc_p50_s||0)+' · p95 '+(s.cyc_p95_s||0)+' · last '+(s.cyc_last_s||0));
  h+=card('Digs (registered)',s.digs||0,(s.digs_per_pan||0)+'/pan · '+(s.dig_clicks||0)+' clicks');
  h+='</div>';
  h+=sec('Earnings')+'<div class="agrid">';
  h+=card('Money','$'+B(s.money_earned||0),'$'+B(s.money_per_hr||0)+'/hr · $'+B(s.money_per_pan||0)+'/pan');
  h+=card('Shards',B(s.shards_earned||0),B(s.shards_per_hr||0)+'/hr · '+(s.shards_per_pan||0)+'/pan');
  h+=card('Loot value (kept)','$'+B(s.loot_value||0),'$'+B(s.loot_per_hr||0)+'/hr est');
  h+=card('TOTAL / hr','$'+B(s.total_per_hr||0),'money + kept loot');
  h+='</div>';
  h+=sec('Finds','Finds: '+(s.finds_count||0)+' items · '+B(s.find_kg||0)+' kg · best '+B(s.best_kg||0)+' kg'+(s.finds_stack?' · stack '+s.finds_stack:'')+(s.finds_lowconf?' · '+s.finds_lowconf+' low-conf':''));
  const rar=s.by_rarity||{},mod=s.by_mod||{};
  const chips=o=>Object.keys(o).length?Object.entries(o).sort((a,b)=>b[1]-a[1]).map(e=>'<span class="achip">'+e[0]+' <b>'+e[1]+'</b></span>').join(''):'<span class="adim">none yet</span>';
  h+='<div class="arow" data-tip="rarity"><span class="albl">rarity</span>'+chips(rar)+'</div>';
  h+='<div class="arow" data-tip="modifier"><span class="albl">modifier</span>'+chips(mod)+'</div>';
  h+=sec('Reliability')+'<div class="agrid">';
  h+=card('Clean cycles',(s.clean_pct!=null?s.clean_pct+'%':'-'),(s.clean_cycles||0)+' of '+(s.cycles||0));
  h+=card('Recoveries',s.recoveries||0,(s.nudges||0)+' nudges · '+(s.shake_misses||0)+' shake misses');
  h+=card('Shake retries',(s.shake_retries!=null?s.shake_retries:'—'),(s.shake_fails!=null?(s.shake_fails+' fails'):'—'));
  h+=card('Stops / pauses',(s.safe_stops||0)+' / '+(s.hard_stops||0),(s.pauses||0)+' pauses · '+(s.relics_used||0)+' relics');
  h+='</div>';
  h+=sec('Find log','Find log ('+finds.length+')');
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
 :root{--bg2:#1c1714;--panel:#221c18;--line:#332a23;--line2:#463a2f;--txt:#ece0d0;--mut:#9c8e7c;--dim:#6a5d4d;--accent:#a8794a;--accent-lit:#caa06e;--accent2:#8a9b6a;--teal-lit:#9bc07e}
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
       <option value="offline">Offline brain, free, instant</option><option value="anthropic">Claude (Anthropic)</option><option value="openai">OpenAI (GPT)</option><option value="gemini">Google Gemini</option><option value="deepseek">DeepSeek</option><option value="custom">Custom / local</option></select></div>
     <div id="cloud"><div class="fld"><span class="lab">Model</span><select id="msel"></select></div>
       <div class="fld" id="mrow" style="display:none"><span class="lab">Model id</span><input id="mid" type="text" placeholder="exact model id"></div>
       <div class="fld" id="brow" style="display:none"><span class="lab">Base URL</span><input id="base" type="text" placeholder="https://…/v1"></div>
       <div class="fld"><span class="lab">API key</span><input id="key" type="password" placeholder="paste key, stays on this PC"></div></div>
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
   w.innerHTML='<div class="dh">Suggested change'+(ch.length>1?'s':'')+'</div>'+rows+'<div class="ds ok">✓ Applied, press Save settings in the app to keep</div><div class="ds no">Not applied</div>'+(live?('<div class="da"><button class="ap">Implement '+ch.length+' change'+(ch.length>1?'s':'')+'</button><button class="dm">Don’t apply</button></div>'):'');
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
 const PROV={anthropic:{base:'',models:[['claude-haiku-4-5-20251001','Haiku 4.5, cheapest'],['claude-sonnet-5','Sonnet 5, smartest']]},
   openai:{base:'',models:[['gpt-5.4-mini','GPT-5.4-mini, cheap'],['gpt-5.4','GPT-5.4'],['gpt-4o-mini','GPT-4o-mini, safe fallback']]},
   gemini:{base:'https://generativelanguage.googleapis.com/v1beta/openai',models:[['gemini-2.5-flash-lite','2.5 Flash-Lite, cheapest'],['gemini-2.5-flash','2.5 Flash']]},
   deepseek:{base:'https://api.deepseek.com/v1',models:[['deepseek-chat','deepseek-chat']]},custom:{base:'',models:[]}};
 function setSub(m){sub.textContent=(m==='api')?'AI API mode':'offline tuning assistant';}
 function fillModels(p,sel){msel.innerHTML='';((PROV[p]||{}).models||[]).forEach(m=>{const o=document.createElement('option');o.value=m[0];o.textContent=m[1];msel.appendChild(o);});const o=document.createElement('option');o.value='__other__';o.textContent='Other model id…';msel.appendChild(o);const has=((PROV[p]||{}).models||[]).some(m=>m[0]===sel);if(sel&&has)msel.value=sel;else if(sel&&p!=='custom'){msel.value='__other__';mid.value=sel;}}
 function applyUI(){const p=provSel.value;cloud.style.display=(p==='offline')?'none':'block';brow.style.display=(p==='custom')?'flex':'none';mrow.style.display=(p!=='offline'&&(msel.value==='__other__'||p==='custom'))?'flex':'none';}
 provSel.onchange=()=>{fillModels(provSel.value,'');applyUI();};msel.onchange=applyUI;
 async function loadCfg(){let c={};try{c=await api().coach_settings();}catch(e){c={mode:'offline',model:'',base:''};}
   let p='offline';if(c.mode==='api'){const b=(c.base||'');if(/deepseek/.test(b))p='deepseek';else if(/googleapis/.test(b))p='gemini';else if(b)p='custom';else if(((c.model||'').toLowerCase()).indexOf('claude')===0)p='anthropic';else p='openai';}
   provSel.value=p;fillModels(p,c.model||'');base.value=c.base||'';if(p==='custom')mid.value=c.model||'';keyIn.placeholder=c.has_key?'•••••• key saved, type to replace':'paste key, stays on this PC';applyUI();setSub(c.mode);}
 $('cfgb').onclick=()=>{const on=document.body.classList.toggle('cfgon');if(on)loadCfg();};
 $('save').onclick=async()=>{const p=provSel.value,mode=(p==='offline')?'offline':'api';let model='',b='';if(mode==='api'){model=(msel.value==='__other__'||p==='custom')?mid.value.trim():msel.value;b=(p==='custom')?base.value.trim():((PROV[p]||{}).base||'');}const key=(keyIn.value.trim())?keyIn.value.trim():null;try{await api().save_coach_settings(mode,key,model||null,b);}catch(e){}keyIn.value='';setSub(mode);document.body.classList.remove('cfgon');toast(mode==='api'?('Coach: '+p):'Coach: offline');};
 $('clr').onclick=async()=>{try{await api().save_coach_settings('offline','__CLEAR__');}catch(e){}keyIn.value='';loadCfg();toast('API key cleared');};
 function boot(){loadHist();loadCfg();setTimeout(()=>inp.focus(),80);}
 window.__reload=function(){boot();};
 window.addEventListener('pywebviewready',boot);
 if(window.pywebview&&window.pywebview.api)boot();
</script></body></html>'''


def _studio_eval(js):
    """Run JS inside the Studio window (safe no-op when it is not open)."""
    try:
        if _studio_win is not None:
            _studio_win.evaluate_js(js)
    except Exception:
        pass


def _studio_html():
    """The Prospector Studio editor window. The palette, ranges, defaults and
    summaries are generated from STUDIO_BLOCKS (prospecting_ui.py) at render
    time, so the editor can never drift from the schema the validator and the
    engine interpreter enforce."""
    palette = []
    for gid, glabel in STUDIO_GROUPS:
        cards = []
        for t, d in STUDIO_BLOCKS.items():
            if d.get("group") != gid:
                continue
            cards.append(
                '<div class="pcard" tabindex="0" role="button" draggable="true"'
                ' data-type="%s" aria-label="Add block: %s">'
                '<span class="pico">%s</span><span>%s</span></div>'
                % (t, d["name"], d["icon"], d["name"]))
        palette.append('<div class="pgroup" data-group="%s">'
                       '<div class="pgh">%s</div>%s</div>'
                       % (gid, glabel, "".join(cards)))
    blocks_json = json.dumps(
        {t: {"name": d["name"], "group": d["group"], "icon": d["icon"],
             "summary": d["summary"], "kids": t in STUDIO_CONTAINERS,
             "params": d["params"]}
         for t, d in STUDIO_BLOCKS.items()}).replace("</", "<\\/")
    html = r'''<!doctype html><html><head><meta charset="utf-8"><style>
 :root{--bg:#171310;--bg2:#1c1714;--panel:#221c18;--head:#181310;--line:#332a23;
  --line2:#463a2f;--txt:#ece0d0;--mut:#9c8e7c;--dim:#6a5d4d;--accent:#a8794a;
  --accent-lit:#caa06e;--accent2:#8a9b6a;--teal-lit:#9bc07e;--green:#7faf5d;
  --field:#161210;--red:#d88a6a;--ease:cubic-bezier(.22,1,.36,1)}
 *{box-sizing:border-box} html,body{height:100%;margin:0}
 body{background:var(--bg);color:var(--txt);display:flex;flex-direction:column;
  font:13.5px/1.5 "Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
  -webkit-user-select:none;user-select:none;overflow:hidden}
 button{font:inherit;font-weight:600;border:0;border-radius:9px;padding:8px 13px;cursor:pointer;
  transition:transform .12s var(--ease),filter .15s,background .15s;color:var(--txt)}
 button:focus-visible,.pcard:focus-visible,.blk:focus-visible{outline:2px solid var(--accent-lit);outline-offset:2px}
 .btn{background:var(--accent);color:#241a02} .btn:hover{filter:brightness(1.06)}
 .btn2{background:#2a2418;color:#e9e0cf} .btn2:hover{background:#352d1c}
 button:disabled{opacity:.42;cursor:default;transform:none !important;filter:none !important}
 input,select,textarea{font:inherit;background:var(--field);color:var(--txt);
  border:1px solid var(--line2);border-radius:8px;padding:7px 10px}
 input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent)}
 .topbar{flex:0 0 auto;display:flex;align-items:center;gap:9px;padding:11px 16px;
  background:var(--head);border-bottom:1px solid var(--line)}
 .brand{font-size:15px;font-weight:600;white-space:nowrap} .brand b{color:var(--accent-lit)}
 .grow{flex:1}
 .valdot{width:10px;height:10px;border-radius:50%;background:var(--dim);flex:none}
 .valdot.ok{background:var(--green)} .valdot.warn{background:#c2924c} .valdot.err{background:var(--red)}
 .valtxt{color:var(--mut);font-size:12px;white-space:nowrap}
 .runpill{display:none;align-items:center;gap:7px;background:rgba(127,175,93,.12);
  border:1px solid var(--green);border-radius:99px;padding:4px 12px;font-size:12px;color:var(--green);font-weight:700}
 body.running .runpill{display:inline-flex}
 .metabar{flex:0 0 auto;display:flex;gap:9px;align-items:center;padding:9px 16px;
  background:var(--bg2);border-bottom:1px solid var(--line)}
 .metabar label{color:var(--dim);font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;font-weight:700}
 #scname{width:230px;font-weight:700} #scdesc{flex:1;min-width:120px}
 .dirty{color:var(--accent-lit);font-size:11.5px;font-weight:700;visibility:hidden;white-space:nowrap}
 body.isdirty .dirty{visibility:visible}
 .main{flex:1;display:grid;grid-template-columns:222px minmax(0,1fr) 302px;min-height:0}
 @media (max-width:980px){.main{grid-template-columns:170px minmax(0,1fr) 236px}}
 @media (max-width:760px){.main{grid-template-columns:150px minmax(0,1fr) 200px}}
 .pal{border-right:1px solid var(--line);overflow-y:auto;padding:12px 10px;background:var(--bg2)}
 .pgh{color:var(--dim);font-size:10.5px;text-transform:uppercase;letter-spacing:.07em;
  font-weight:800;margin:12px 4px 6px} .pgroup:first-child .pgh{margin-top:0}
 .pcard{display:flex;align-items:center;gap:9px;background:var(--panel);border:1px solid var(--line2);
  border-radius:9px;padding:7px 10px;margin-bottom:5px;cursor:grab;font-weight:600;font-size:12.5px;color:var(--txt)}
 .pcard:hover{border-color:var(--accent);background:#272019}
 .pcard .pico{flex:none;width:16px;height:16px;opacity:.75;display:flex}
 .pcard .pico svg{width:16px;height:16px}
 .pgroup[data-group="action"] .pcard{border-left:3px solid var(--accent)}
 .pgroup[data-group="sense"] .pcard{border-left:3px solid var(--teal-lit)}
 .pgroup[data-group="flow"] .pcard{border-left:3px solid #6ba1b5}
 .palhint{color:var(--dim);font-size:11px;line-height:1.5;margin:12px 4px 0}
 .canvas{overflow-y:auto;padding:16px 18px 90px;position:relative}
 .cvempty{border:2px dashed var(--line2);border-radius:14px;padding:38px 22px;text-align:center;color:var(--mut)}
 .cvempty b{color:var(--txt)} .cvempty .btn2{margin-top:12px}
 .looplab{display:flex;align-items:center;gap:8px;color:var(--dim);font-size:11px;
  text-transform:uppercase;letter-spacing:.06em;font-weight:800;margin:0 0 8px}
 .looplab svg{width:14px;height:14px}
 .loopwrap{border-left:3px solid var(--line2);padding-left:12px;margin-left:5px}
 .blk{display:flex;align-items:flex-start;gap:9px;background:var(--panel);border:1px solid var(--line2);
  border-radius:10px;padding:8px 10px;margin:0 0 6px;cursor:default;position:relative}
 .blk[draggable="true"]{cursor:grab}
 .blk:hover{border-color:#5a4c3d}
 .blk.sel{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
 .blk.live{border-color:var(--green);box-shadow:0 0 0 1px var(--green)}
 .blk.issue{border-color:var(--red)}
 .blk .bico{flex:none;width:16px;height:16px;margin-top:2px;opacity:.8}
 .blk .bico svg{width:16px;height:16px}
 .blk.g-action .bico{color:var(--accent-lit)} .blk.g-sense .bico{color:var(--teal-lit)} .blk.g-flow .bico{color:#6ba1b5}
 .bmain{flex:1;min-width:0}
 .bname{font-weight:700;font-size:12.5px}
 .bsum{color:var(--mut);font-size:12px;line-height:1.45;overflow-wrap:break-word}
 .bissue{color:var(--red);font-size:11.5px;margin-top:3px;font-weight:600}
 .bbtns{flex:none;display:flex;gap:4px;opacity:0;transition:opacity .12s}
 .blk:hover .bbtns,.blk.sel .bbtns,.blk:focus-within .bbtns{opacity:1}
 .bb{background:transparent;color:var(--mut);padding:2px 6px;border-radius:6px;font-size:12px}
 .bb:hover{background:#352d1c;color:var(--txt)}
 .kids{margin:0 0 6px 14px;padding-left:10px;border-left:3px solid var(--line2)}
 .blk.g-action + .kids,.kids.g-action{border-left-color:var(--accent)}
 .kids.g-sense{border-left-color:var(--teal-lit)} .kids.g-flow{border-left-color:#6ba1b5}
 .addkid{display:block;background:transparent;border:1px dashed var(--line2);color:var(--dim);
  border-radius:8px;padding:4px 10px;font-size:11.5px;margin:2px 0 6px;width:auto}
 .addkid:hover{color:var(--txt);border-color:var(--accent)}
 .dropline{height:3px;border-radius:2px;background:var(--accent-lit);margin:2px 0 5px;display:none}
 .dropline.show{display:block}
 .blk.dropin{outline:2px dashed var(--accent-lit);outline-offset:2px}
 .insp{border-left:1px solid var(--line);overflow-y:auto;background:var(--bg2);display:flex;flex-direction:column}
 .inspbody{padding:14px;flex:0 0 auto}
 .ihead{display:flex;align-items:center;gap:9px;margin-bottom:3px}
 .ihead .bico{width:17px;height:17px;display:flex} .ihead .bico svg{width:17px;height:17px}
 .ihead h3{margin:0;font-size:14.5px}
 .ikind{color:var(--dim);font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;font-weight:700;margin-bottom:9px}
 .isum{background:var(--field);border-left:3px solid var(--accent);border-radius:0 8px 8px 0;
  padding:7px 10px;margin:0 0 13px;color:var(--mut);font-size:12.5px;line-height:1.5}
 .isum b{color:var(--txt)}
 .prow{margin-bottom:12px}
 .prow .plab{display:block;color:var(--mut);font-size:12px;font-weight:600;margin-bottom:4px}
 .prange{display:flex;gap:8px;align-items:center}
 .prange input[type="range"]{flex:1;padding:0;accent-color:var(--accent);background:transparent;border:0}
 .prange input[type="number"]{width:84px}
 .prow select,.prow input[type="text"]{width:100%}
 .switch{position:relative;display:inline-block;width:38px;height:22px;flex:none}
 .switch input{opacity:0;width:0;height:0;position:absolute}
 .switch .track{position:absolute;inset:0;background:#3a3128;border-radius:99px;transition:background .15s;cursor:pointer}
 .switch .knob{position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:50%;background:#8d8171;transition:left .15s,background .15s}
 .switch input:checked + .track{background:var(--accent)} .switch input:checked + .track .knob{left:19px;background:#241a02}
 .boolrow{display:flex;align-items:center;gap:10px}
 .inone{color:var(--dim);padding:20px 14px;font-size:12.5px;line-height:1.6}
 .helpbox{flex:1;border-top:1px solid var(--line);padding:13px 14px;min-height:120px}
 .helpbox h3{margin:0 0 2px;font-size:13.5px}
 .ph-kind{color:var(--dim);font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;font-weight:700;margin-bottom:8px}
 .ph-body{font-size:12.5px;color:var(--mut);line-height:1.6}
 .ph-body p{margin:0 0 9px} .ph-body b{color:var(--txt);font-weight:700} .ph-body i{color:var(--txt)}
 .ph-body s{text-decoration:none;color:var(--red);font-weight:600}
 .ph-body code{background:var(--field);padding:1px 5px;border-radius:4px;font:12px ui-monospace,Menlo,monospace;color:var(--accent-lit)}
 .ph-body ul{margin:6px 0 10px;padding-left:18px} .ph-body li{margin:3px 0}
 .ph-body .ph-call{background:var(--field);border-left:3px solid var(--accent);border-radius:0 8px 8px 0;padding:8px 11px;margin:9px 0;font-size:12px;line-height:1.55}
 .ph-body .ph-call b{color:var(--accent-lit);margin-right:4px}
 .ph-row{display:flex;gap:8px;margin:0 0 6px;align-items:flex-start}
 .ph-tag{flex:none;width:76px;text-align:right;font-size:10px;font-weight:700;padding-top:1px}
 .ph-tag i{font-style:normal;margin-right:3px}
 .ph-tag.up{color:var(--accent-lit)} .ph-tag.dn{color:#5aa0bd} .ph-tag.fx{color:var(--red)} .ph-tag.ok{color:#8cc06a}
 .ph-tx{flex:1;font-size:12px;color:var(--mut);line-height:1.5}
 .ph-steps{margin:7px 0 9px}
 .ph-step{display:flex;gap:8px;margin:0 0 5px;font-size:12px;color:var(--mut);line-height:1.5}
 .ph-step i{font-style:normal;flex:none;width:17px;height:17px;border-radius:50%;background:var(--field);
  color:var(--accent-lit);font-weight:800;font-size:10px;display:flex;align-items:center;justify-content:center;margin-top:1px}
 .ph-lk{display:inline-block;background:#201b15;border:1px solid var(--line2);border-radius:5px;padding:1px 7px;font-size:10.5px;color:var(--accent-lit);margin:0 3px 3px 0}
 .probbar{flex:0 0 auto;border-top:1px solid var(--line);background:var(--bg2);
  max-height:130px;overflow-y:auto;padding:8px 16px;display:none}
 .probbar.show{display:block}
 .prob{display:flex;gap:8px;align-items:flex-start;font-size:12.5px;margin:3px 0;color:var(--mut);cursor:pointer}
 .prob i{font-style:normal;flex:none;margin-top:1px}
 .prob.err{color:var(--red)} .prob.warn{color:#c2924c}
 .prob:hover{color:var(--txt)}
 .palfilter{width:100%;margin:0 0 10px;font-size:12px}
 .qins{position:fixed;left:50%;top:14%;transform:translateX(-50%);z-index:55;width:min(420px,90vw);
  background:var(--panel);border:1px solid var(--line2);border-radius:13px;padding:10px;display:none;
  box-shadow:0 18px 50px rgba(0,0,0,.55)}
 .qins.show{display:block}
 .qins input{width:100%;margin-bottom:8px}
 .qi{display:flex;align-items:center;gap:9px;padding:7px 9px;border-radius:8px;cursor:pointer;font-weight:600;font-size:12.5px}
 .qi .pico{width:15px;height:15px;display:flex;opacity:.75} .qi .pico svg{width:15px;height:15px}
 .qi .qgrp{margin-left:auto;color:var(--dim);font-size:10px;text-transform:uppercase;letter-spacing:.05em;font-weight:800}
 .qi.on,.qi:hover{background:rgba(168,121,74,.14);color:var(--accent-lit)}
 .ctx{position:fixed;z-index:56;background:var(--panel);border:1px solid var(--line2);border-radius:11px;
  padding:5px;min-width:200px;box-shadow:0 14px 40px rgba(0,0,0,.5)}
 .ctx button{display:flex;width:100%;text-align:left;background:transparent;color:var(--txt);
  padding:7px 10px;border-radius:7px;font-size:12.5px;font-weight:600;gap:8px;align-items:center}
 .ctx button:hover,.ctx button:focus-visible{background:rgba(168,121,74,.14);color:var(--accent-lit)}
 .ctx button:disabled{opacity:.4}
 .ctx button .kbd{margin-left:auto;color:var(--dim);font-size:10.5px;font-weight:700}
 .ctx .sep{height:1px;background:var(--line);margin:4px 6px}
 .ctx button.danger{color:var(--red)}
 .blk{content-visibility:auto;contain-intrinsic-size:auto 44px}
 .blk.new{animation:blkin .2s var(--ease)}
 @keyframes blkin{from{opacity:0;transform:translateY(-5px)}to{opacity:1;transform:none}}
 #toast.err{border-color:var(--red);color:#f0c0b0}
 @media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none !important;transition:none !important}}
 #toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%) translateY(8px);background:#2c2620;
  border:1px solid var(--line2);border-radius:10px;padding:10px 18px;opacity:0;pointer-events:none;
  transition:opacity .2s,transform .2s;z-index:60;font-weight:600;max-width:80vw}
 #toast.show{opacity:1;transform:translateX(-50%)}
 .modal{position:fixed;inset:0;background:rgba(10,8,6,.72);display:none;align-items:center;justify-content:center;z-index:50}
 .modal.show{display:flex}
 .mcard{background:var(--panel);border:1px solid var(--line2);border-radius:14px;padding:20px;width:min(560px,92vw);max-height:82vh;overflow-y:auto}
 .mcard h3{margin:0 0 4px} .mcard .mhint{color:var(--mut);font-size:12.5px;margin:0 0 14px}
 .tplcard{display:flex;gap:12px;align-items:flex-start;border:1px solid var(--line2);border-radius:10px;
  padding:12px 14px;margin-bottom:8px;cursor:pointer}
 .tplcard:hover{border-color:var(--accent);background:#272019}
 .tplcard h4{margin:0 0 3px;font-size:13.5px} .tplcard p{margin:0;color:var(--mut);font-size:12px;line-height:1.5}
 .mbtns{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}
 .offer{display:none;align-items:center;gap:10px;background:rgba(168,121,74,.12);border:1px solid var(--accent);
  border-radius:10px;padding:9px 13px;margin:0 0 13px;font-size:12.5px}
 .offer.show{display:flex}
 .tour{position:fixed;inset:0;z-index:70;display:none}
 .tourdim{position:absolute;inset:0;background:rgba(8,6,4,.55)}
 .tourspot{position:absolute;border:2px solid var(--accent-lit);border-radius:12px;
  box-shadow:0 0 0 9999px rgba(8,6,4,.55);pointer-events:none;transition:all .25s var(--ease)}
 .tourpop{position:absolute;width:min(340px,86vw);background:var(--panel);border:1px solid var(--line2);
  border-radius:13px;padding:15px 16px;box-shadow:0 14px 40px rgba(0,0,0,.5)}
 .tourpop.center{left:50%;top:50%;transform:translate(-50%,-50%)}
 .tourpop h3{margin:0 0 6px;font-size:14.5px}
 .tourpop .tb{color:var(--mut);font-size:12.5px;line-height:1.6;-webkit-user-select:text;user-select:text}
 .tourpop .tb b{color:var(--txt)}
 .tstep{color:var(--dim);font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}
 .tbtns{display:flex;gap:8px;margin-top:12px;align-items:center}
 .tbtns .grow{flex:1}
 ::-webkit-scrollbar{width:9px;height:9px} ::-webkit-scrollbar-thumb{background:#3a3128;border-radius:6px}
 ::-webkit-scrollbar-track{background:transparent}
</style></head><body>
 <div class="topbar">
   <div class="brand"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" style="vertical-align:-2px"><path d="M6.5 4h11l4 5.2L12 21 2.5 9.2z" fill="#caa06e"/></svg> Prospector <b>Studio</b></div>
   <select id="libsel" title="Open a saved script" aria-label="Open a saved script" style="max-width:190px"></select>
   <button class="btn2" id="stnewbtn" title="Start a new script from a template">New script</button>
   <span class="grow"></span>
   <span class="runpill" id="runpill">&#9679; running</span>
   <span class="valdot" id="valdot"></span><span class="valtxt" id="valtxt" aria-live="polite">no script</span><span class="valtxt" id="laptxt" title="Worst case for one full lap, if every wait runs to its limit"></span>
   <button class="btn2" id="undobtn" title="Undo (Ctrl+Z)" aria-label="Undo">&#8630;</button>
   <button class="btn2" id="redobtn" title="Redo (Ctrl+Shift+Z)" aria-label="Redo">&#8631;</button>
   <button class="btn2" id="valbtn" title="Check the whole script for problems">Validate</button>
   <button class="btn" id="stsave" title="Save this script (Ctrl+S)">Save</button>
   <button class="btn2" id="strun" title="Save, set active and start the macro">&#9654; Run</button>
   <button class="btn2" id="ststop" title="Stop the macro" disabled>&#9632; Stop</button>
   <button class="btn2" id="sthelp" title="Replay the Studio walkthrough">&#10067;</button>
 </div>
 <div class="metabar">
   <label for="scname">Name</label><input id="scname" maxlength="60" spellcheck="false" aria-label="Script name">
   <label for="scdesc">Notes</label><input id="scdesc" maxlength="300" spellcheck="false" placeholder="what this script farms, for friends you share it with" aria-label="Script description">
   <span class="dirty" id="dirtylab">unsaved changes</span>
 </div>
 <div class="main">
   <aside class="pal" id="pal" aria-label="Block palette"><input id="palfilter" class="palfilter" type="text" placeholder="Filter blocks&hellip;" aria-label="Filter palette blocks" spellcheck="false">{{PALETTE}}
     <div class="palhint">Click a block to add it after the selected one, or drag it exactly where it should go. Drop onto an If, Repeat or Group to put it inside. Press <b>/</b> to quick-add by name; right-click any block for more.</div>
   </aside>
   <section class="canvas" id="canvasWrap">
     <div class="offer" id="touroffer"><span>New to Studio? A two minute walkthrough shows you how to build the Treasure script from blocks.</span><button class="btn" id="offeryes">Show me</button><button class="btn2" id="offerno">No thanks</button></div>
     <div class="looplab" id="looplab"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M17 2l3 3-3 3"/><path d="M20 5H7a4 4 0 0 0-4 4v1M7 22l-3-3 3-3"/><path d="M4 19h13a4 4 0 0 0 4-4v-1"/></svg> repeats forever while the macro runs</div>
     <div class="loopwrap" id="loopwrap"><div id="canvas"></div><button class="addkid" id="addend" title="Add a block at the end (or press /)">+ add a block</button></div>
   </section>
   <aside class="insp" id="insp" aria-label="Block inspector">
     <div class="inspbody" id="inspbody"><div class="inone">Select a block on the canvas to edit what it does.</div></div>
     <div class="helpbox" id="helpbox"></div>
   </aside>
 </div>
 <div class="probbar" id="probbar"></div>
 <div class="modal" id="tplmodal" role="dialog" aria-label="New script">
   <div class="mcard"><h3>New script</h3><p class="mhint">Pick a starting point. Templates are the built-in cycles rebuilt from blocks, ready to tweak.</p>
     <div id="tpllist"></div>
     <div class="mbtns"><button class="btn2" id="tplcancel">Cancel</button></div>
   </div>
 </div>
 <div class="modal" id="cfmmodal" role="dialog" aria-label="Confirm">
   <div class="mcard"><h3 id="cfmtitle">Are you sure?</h3><p class="mhint" id="cfmbody"></p>
     <div class="mbtns"><button class="btn2" id="cfmno">Cancel</button><button class="btn" id="cfmyes">Yes</button></div>
   </div>
 </div>
 <div class="tour" id="sttour"><div class="tourdim"></div><div class="tourspot" id="sttourspot"></div>
   <div class="tourpop" id="sttourpop"><div class="tstep" id="sttourstep"></div><h3 id="sttourtitle"></h3>
     <div class="tb" id="sttourbody"></div>
     <div class="tbtns"><button class="btn2" id="sttourskip">Skip</button><span class="grow"></span>
       <button class="btn2" id="sttourback">Back</button><button class="btn" id="sttournext">Next</button></div></div>
 </div>
 <div class="qins" id="qins" role="dialog" aria-label="Quick add a block">
   <input id="qinsin" placeholder="Type a block name&hellip;" aria-label="Search blocks" spellcheck="false">
   <div id="qinslist" role="listbox" aria-label="Matching blocks"></div>
 </div>
 <div id="ctx" class="ctx" role="menu" style="display:none"></div>
 <div id="toast" role="status"></div>
<script>
 var __BLOCKS={{BLOCKS}};
 var api=function(){return window.pywebview&&window.pywebview.api;};
 var T=function(id){return document.getElementById(id);};
 function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
 var S={script:null,prevName:null,sel:null,undo:[],redo:[],dirty:false,insertInto:null,
        problems:[],helpmap:{},templates:[],running:false,meta:{},lib:[]};
 var GK={action:'g-action',sense:'g-sense',flow:'g-flow'};
 function toast(m,kind){var t=T('toast');t.textContent=m;
   t.className=(kind==='err')?'err':'';t.classList.add('show');
   clearTimeout(t._t);t._t=setTimeout(function(){t.classList.remove('show');},2600);}
 // ---- tree helpers ---------------------------------------------------------
 function walk(blocks,fn,parent,depth){blocks=blocks||[];for(var i=0;i<blocks.length;i++){
   if(fn(blocks[i],blocks,i,parent,depth||0)===false)return false;
   if(blocks[i].children&&walk(blocks[i].children,fn,blocks[i],(depth||0)+1)===false)return false;}return true;}
 function find(id){var hit=null;if(!S.script)return null;
   walk(S.script.blocks,function(b){if(b.id===id){hit=b;return false;}});return hit;}
 function findList(id){var hit=null;if(!S.script)return null;
   walk(S.script.blocks,function(b,lst,i){if(b.id===id){hit={list:lst,i:i};return false;}});return hit;}
 function nextId(){var mx=0;walk(S.script.blocks,function(b){var m=/^b(\d+)$/.exec(b.id||'');
   if(m)mx=Math.max(mx,parseInt(m[1],10));});return 'b'+(mx+1);}
 function countBlocks(bl){var n=0;walk(bl,function(){n++;});return n;}
 function depthOf(bl){var mx=1;walk(bl,function(b,l,i,p,d){mx=Math.max(mx,d+1);});return mx;}
 function newBlock(type){var d=__BLOCKS[type];var b={id:nextId(),type:type,params:{}};
   (d.params||[]).forEach(function(p){b.params[p.key]=p['default'];});
   if(d.kids)b.children=[];return b;}
 // ---- summaries -------------------------------------------------------------
 function choiceLabel(d,key,val){var p=(d.params||[]).filter(function(x){return x.key===key;})[0];
   if(!p||!p.choices)return String(val);
   for(var i=0;i<p.choices.length;i++)if(p.choices[i][0]===val)return p.choices[i][1];
   return String(val);}
 function summarize(b){var d=__BLOCKS[b.type];if(!d)return b.type;var P=b.params||{};
   if(b.type==='comment')return P.text?esc(P.text):'<i>(empty note; write one in the inspector)</i>';
   if(b.type==='group')return esc(P.label||'Group')+': runs the steps inside, in order.';
   var s=d.summary||d.name;
   if(b.type==='shake')s=s.replace('{clicks_t}',P.clicks>0?('exactly '+P.clicks+' clicks'):'until the pan reads empty');
   if(b.type==='click')s=s.replace('{at_t}',P.at==='center'?', at the screen centre':(P.at==='autopan'?', at the Auto Pan button':''));
   if(b.type==='wait_cue'){s=s.replace('{hold_t}',(P.hold&&P.hold!=='none')?(', holding '+P.hold):'')
     .replace('{fresh_t}',P.fresh?', after leaving the current one':'');}
   ['cue','state','check','key','hold','at'].forEach(function(k){
     if(P[k]!==undefined)s=s.split('{'+k+'_t}').join(esc(choiceLabel(d,k,P[k])));});
   s=s.replace(/\{(\w+)\}/g,function(m,k){return P[k]!==undefined?esc(String(P[k])):m;});
   return s;}
 // ---- validation (client; the Python validator is authoritative on save) ----
 function clientCheck(){var errs=[],probs=[];if(!S.script)return{errs:errs,probs:probs};
   var bl=S.script.blocks||[];
   if(!bl.length)probs.push({id:null,msg:'The script is empty; add blocks from the palette, or start from a template.'});
   if(countBlocks(bl)>500)errs.push({id:null,msg:'Too many blocks (limit 500).'});
   if(depthOf(bl)>16)errs.push({id:null,msg:'Blocks are nested deeper than 16 levels.'});
   var anyAct=false;
   walk(bl,function(b,lst,i,parent){var d=__BLOCKS[b.type];
     if(!d){errs.push({id:b.id,msg:'Unknown block type.'});return;}
     if(['dig','shake','hold_key','tap_key','click','relic'].indexOf(b.type)>=0)anyAct=true;
     (d.params||[]).forEach(function(p){var v=(b.params||{})[p.key];
       if(p.type==='int'){if(typeof v!=='number'||!isFinite(v)||v!==Math.round(v)||v<p.range[0]||v>p.range[1])
         errs.push({id:b.id,msg:d.name+': "'+p.label+'" must be between '+p.range[0]+' and '+p.range[1]+'.'});}
       else if(p.type==='choice'){if(!p.choices.some(function(c){return c[0]===v;}))
         errs.push({id:b.id,msg:d.name+': pick a valid "'+p.label+'".'});}});
     if(d.kids&&(!b.children||!b.children.length))
       probs.push({id:b.id,msg:d.name+' is empty; give it at least one step inside, or remove it.'});
     for(var j=0;j<i;j++)if(lst[j].type==='stop'){
       probs.push({id:b.id,msg:d.name+' can never run: it sits after a Safe stop.'});break;}});
   if(bl.length&&!anyAct)probs.push({id:null,msg:'This script never sends any input, so it would do nothing.'});
   return{errs:errs,probs:probs};}
 // ---- render -----------------------------------------------------------------
 function blkHTML(b){var d=__BLOCKS[b.type]||{name:b.type,group:'flow',icon:''};
   var iss=S._issueById[b.id];
   var h='<div class="blk '+GK[d.group]+(S.sel===b.id?' sel':'')+(iss?' issue':'')+(S._animId===b.id?' new':'')+'" data-id="'+esc(b.id)+
     '" tabindex="0" draggable="true" role="button" aria-label="'+esc(d.name)+'">'+
     '<span class="bico">'+d.icon+'</span><div class="bmain"><div class="bname">'+esc(d.name)+'</div>'+
     '<div class="bsum">'+summarize(b)+'</div>'+(iss?('<div class="bissue">'+esc(iss)+'</div>'):'')+
     '</div><div class="bbtns">'+
     '<button class="bb" data-act="dup" title="Duplicate this block" aria-label="Duplicate">&#10697;</button>'+
     '<button class="bb" data-act="del" title="Delete this block" aria-label="Delete">&#10005;</button></div></div>';
   if(d.kids){h+='<div class="kids '+GK[d.group]+'" data-kidsof="'+esc(b.id)+'">'+
     (b.children||[]).map(blkHTML).join('')+
     '<button class="addkid" data-into="'+esc(b.id)+'">+ step inside</button></div>';}
   return h;}
 function render(){var cv=T('canvas');
   if(!S.script){cv.innerHTML='';T('loopwrap').style.display='none';T('looplab').style.display='none';
     T('inspbody').innerHTML='<div class="inone">Open a script from the top left, or press New script.</div>';
     setValidity();return;}
   var chk=clientCheck();S.problems=chk;S._issueById={};
   chk.errs.concat(chk.probs).forEach(function(p){if(p.id&&!S._issueById[p.id])S._issueById[p.id]=p.msg;});
   T('looplab').style.display='';T('loopwrap').style.display='';
   var bl=S.script.blocks||[];
   cv.innerHTML=bl.length?bl.map(blkHTML).join(''):
     '<div class="cvempty"><b>An empty canvas.</b><br>Add your first block from the palette on the left,'+
     '<br>or start again from a template.<br><button class="btn2" id="cvtpl">Choose a template</button></div>';
   S._animId=null;
   var ct=T('cvtpl');if(ct)ct.onclick=function(){openTplModal(false);};
   T('scname').value=S.script.name||'';T('scdesc').value=S.script.description||'';
   document.body.classList.toggle('isdirty',S.dirty);
   renderInspector();renderProblems();setValidity();
   T('undobtn').disabled=!S.undo.length;T('redobtn').disabled=!S.redo.length;}
 function lapMs(bl){var t=0;for(var i=0;i<(bl||[]).length;i++){var b=bl[i];var P=b.params||{};
   switch(b.type){
     case 'dig':t+=(P.hold_ms||0)+400;break;
     case 'shake':t+=(P.clicks>0)?(P.clicks*((P.click_ms||0)+(P.gap_ms||0))):(P.max_ms||0);break;
     case 'hold_key':t+=P.ms||0;break;
     case 'tap_key':t+=P.hold_ms||0;break;
     case 'click':t+=(P.hold_ms||0)+((P.at&&P.at!=='none')?60:0);break;
     case 'wait':t+=P.ms||0;break;
     case 'relic':t+=2600;break;
     case 'wait_cue':case 'wait_cap':t+=P.timeout_ms||0;break;
     case 'repeat':t+=(P.times||1)*lapMs(b.children);break;
     case 'if_cue':case 'if_cap':case 'if_not':case 'group':t+=lapMs(b.children);break;
     case 'stop':return t;
   }}return t;}
 function setValidity(){var d=T('valdot'),t=T('valtxt'),lp=T('laptxt');
   if(!S.script){d.className='valdot';t.textContent='no script';if(lp)lp.textContent='';return;}
   var e=S.problems.errs.length,p=S.problems.probs.length;
   if(e){d.className='valdot err';t.textContent=e+' error'+(e>1?'s':'');}
   else if(p){d.className='valdot warn';t.textContent=p+' to fix before it can run';}
   else{d.className='valdot ok';t.textContent='ready to run';}
   if(lp){var ms=lapMs(S.script.blocks);
     lp.textContent=(ms>0&&(S.script.blocks||[]).length)?('lap \u2264 '+(ms<1000?(Math.round(ms)+' ms'):((ms/1000).toFixed(1)+' s'))):'';}}
 function renderProblems(extra){var pb=T('probbar');var items=[];
   S.problems.errs.forEach(function(p){items.push({lv:'err',p:p});});
   S.problems.probs.forEach(function(p){items.push({lv:'warn',p:p});});
   (extra||[]).forEach(function(m){items.push({lv:'err',p:{id:null,msg:m}});});
   if(!items.length){pb.className='probbar';pb.innerHTML='';return;}
   pb.className='probbar show';
   pb.innerHTML=items.map(function(x){return '<div class="prob '+x.lv+'" data-target="'+esc(x.p.id||'')+'">'+
     '<i>'+(x.lv==='err'?'&#9888;':'&#9873;')+'</i><span>'+esc(x.p.msg)+'</span></div>';}).join('');}
 function paramRow(b,p){var v=(b.params||{})[p.key];var id='pf_'+p.key;
   if(p.type==='int'){var r=p.range;
     return '<div class="prow"><label class="plab" for="'+id+'">'+esc(p.label)+'</label>'+
       '<div class="prange"><input type="range" id="'+id+'r" min="'+r[0]+'" max="'+r[1]+'" step="'+r[2]+
       '" value="'+v+'" data-pkey="'+p.key+'" aria-label="'+esc(p.label)+' slider">'+
       '<input type="number" id="'+id+'" min="'+r[0]+'" max="'+r[1]+'" step="'+r[2]+'" value="'+v+
       '" data-pkey="'+p.key+'"></div></div>';}
   if(p.type==='bool'){
     return '<div class="prow"><div class="boolrow"><span class="switch"><input type="checkbox" id="'+id+
       '" data-pkey="'+p.key+'"'+(v?' checked':'')+' aria-label="'+esc(p.label)+'"><span class="track"><span class="knob"></span></span></span>'+
       '<label class="plab" for="'+id+'" style="margin:0">'+esc(p.label)+'</label></div></div>';}
   if(p.type==='choice'){
     return '<div class="prow"><label class="plab" for="'+id+'">'+esc(p.label)+'</label>'+
       '<select id="'+id+'" data-pkey="'+p.key+'">'+p.choices.map(function(c){
         return '<option value="'+esc(c[0])+'"'+(c[0]===v?' selected':'')+'>'+esc(c[1])+'</option>';}).join('')+
       '</select></div>';}
   return '<div class="prow"><label class="plab" for="'+id+'">'+esc(p.label)+'</label>'+
     '<input type="text" id="'+id+'" maxlength="'+(p.max||200)+'" value="'+esc(v)+'" data-pkey="'+p.key+'" spellcheck="false"></div>';}
 function renderInspector(){var box=T('inspbody');var b=S.sel?find(S.sel):null;
   if(!b){box.innerHTML='<div class="inone">Select a block on the canvas to edit what it does.<br><br>'+
     'Keyboard: arrows move the selection, Alt+arrows move or nest a block, / quick-adds by name, Ctrl+C, Ctrl+V and Ctrl+D copy, paste and duplicate, Delete removes.</div>';
     showHelpFor(null);return;}
   var d=__BLOCKS[b.type];
   var h='<div class="ihead"><span class="bico '+GK[d.group]+'">'+d.icon+'</span><h3>'+esc(d.name)+'</h3></div>'+
     '<div class="ikind">'+(d.kids?'container block':'block')+'</div>'+
     '<div class="isum" id="isum">'+summarize(b)+'</div>'+
     (d.params||[]).map(function(p){return paramRow(b,p);}).join('');
   box.innerHTML=h;
   box.querySelectorAll('[data-pkey]').forEach(function(el){
     var ev=(el.type==='range')?'input':'change';
     el.addEventListener(ev,function(){onParam(b.id,el);});
     if(el.type==='number')el.addEventListener('input',function(){onParam(b.id,el);});
     if(el.type==='text')el.addEventListener('input',function(){onParam(b.id,el);});});
   showHelpFor(b.type);}
 function onParam(bid,el){var b=find(bid);if(!b)return;var d=__BLOCKS[b.type];var k=el.getAttribute('data-pkey');
   var p=(d.params||[]).filter(function(x){return x.key===k;})[0];if(!p)return;
   var v;
   if(p.type==='int'){v=parseInt(el.value,10);if(isNaN(v))return;v=Math.max(p.range[0],Math.min(p.range[1],v));
     var num=document.getElementById('pf_'+k),rng=document.getElementById('pf_'+k+'r');
     if(num&&el!==num)num.value=v;if(rng&&el!==rng)rng.value=v;}
   else if(p.type==='bool')v=!!el.checked;
   else if(p.type==='choice')v=el.value;
   else v=String(el.value).slice(0,p.max||200);
   apply(function(s){var bb=find(bid);if(bb)bb.params[k]=v;},'p:'+bid+':'+k,true);
   var bb=find(bid);var card=document.querySelector('.blk[data-id="'+bid+'"] .bsum');
   if(card&&bb)card.innerHTML=summarize(bb);
   var is=T('isum');if(is&&bb)is.innerHTML=summarize(bb);}
 // ---- mutation + undo --------------------------------------------------------
 function snapshot(){return JSON.stringify({b:S.script.blocks,n:S.script.name,d:S.script.description});}
 function apply(fn,coalesce,light){if(!S.script)return;
   var now=Date.now();
   if(!(coalesce&&S._lastKey===coalesce&&now-(S._lastT||0)<900)){
     S.undo.push(snapshot());if(S.undo.length>100)S.undo.shift();S.redo=[];}
   S._lastKey=coalesce||null;S._lastT=now;
   fn(S.script);S.dirty=true;
   if(light){document.body.classList.add('isdirty');
     T('undobtn').disabled=!S.undo.length;T('redobtn').disabled=!S.redo.length;
     var chk=clientCheck();S.problems=chk;renderProblems();setValidity();return;}
   render();}
 function restore(json){var o=JSON.parse(json);S.script.blocks=o.b;S.script.name=o.n;S.script.description=o.d;
   if(S.sel&&!find(S.sel))S.sel=null;S.dirty=true;render();}
 function undo(){if(!S.undo.length)return;S.redo.push(snapshot());restore(S.undo.pop());}
 function redo(){if(!S.redo.length)return;S.undo.push(snapshot());restore(S.redo.pop());}
 // ---- add / move / delete ----------------------------------------------------
 function addBlock(type,at){if(!S.script){toast('Open or create a script first.');return;}
   if(countBlocks(S.script.blocks)>=500){toast('Block limit reached (500).');return;}
   var nb=newBlock(type);
   var oldSel=S.sel,oldInto=S.insertInto;
   S._animId=nb.id;
   apply(function(s){
     if(at&&at.into){var host=find(at.into);if(host&&host.children){host.children.push(nb);S.sel=nb.id;return;}}
     if(at&&at.before!==undefined&&at.list){at.list.splice(at.before,0,nb);S.sel=nb.id;return;}
     if(oldSel){var loc=findList(oldSel);var selB=find(oldSel);
       if(selB&&__BLOCKS[selB.type].kids&&oldInto===oldSel){selB.children.push(nb);S.sel=nb.id;return;}
       if(loc){loc.list.splice(loc.i+1,0,nb);S.sel=nb.id;return;}}
     s.blocks.push(nb);S.sel=nb.id;});
   S.insertInto=null;focusSel();}
 function delBlock(id){var loc0=findList(id);
   var nxt=loc0?(loc0.list[loc0.i+1]||loc0.list[loc0.i-1]||null):null;
   apply(function(s){var loc=findList(id);if(loc)loc.list.splice(loc.i,1);
     if(S.sel===id)S.sel=nxt?nxt.id:null;});
   if(S.sel)focusSel();}
 function freshId(){S._idN=(S._idN||0)+1;return 'b'+Date.now().toString(36)+'x'+S._idN;}
 function reId(x){x.id=freshId();(x.children||[]).forEach(reId);return x;}
 function dupBlock(id){var b=find(id);if(!b)return;
   var cp=reId(JSON.parse(JSON.stringify(b)));
   S._animId=cp.id;
   apply(function(s){var loc=findList(id);if(loc){loc.list.splice(loc.i+1,0,cp);S.sel=cp.id;}});
   focusSel();}
 function moveBlock(id,dir){var loc=findList(id);if(!loc)return;
   if(dir==='up'&&loc.i>0)apply(function(){loc.list.splice(loc.i-1,0,loc.list.splice(loc.i,1)[0]);});
   else if(dir==='down'&&loc.i<loc.list.length-1)apply(function(){loc.list.splice(loc.i+1,0,loc.list.splice(loc.i,1)[0]);});
   else if(dir==='out'){var parent=null;walk(S.script.blocks,function(b){
       if(b.children&&b.children.indexOf(loc.list[loc.i])>=0){parent=b;return false;}});
     if(parent){var ploc=findList(parent.id);
       apply(function(){var b=loc.list.splice(loc.i,1)[0];ploc.list.splice(ploc.i+1,0,b);});}
     else return;}
   else if(dir==='in'&&loc.i>0){var prev=loc.list[loc.i-1];
     if(prev&&__BLOCKS[prev.type]&&__BLOCKS[prev.type].kids&&depthOf(S.script.blocks)<16)
       apply(function(){var b=loc.list.splice(loc.i,1)[0];prev.children.push(b);});
     else return;}
   else return;
   focusSel();}
 function moveTo(id,target){ // drag: target={before:{id}, into:id}
   var loc=findList(id);if(!loc)return;var b=loc.list[loc.i];
   var bad=false;walk([b],function(x){if(target.into&&x.id===target.into)bad=true;
     if(target.beforeId&&x.id===target.beforeId)bad=true;});
   if(bad)return;
   S.sel=id;
   apply(function(s){loc.list.splice(loc.i,1);
     if(target.into){var host=find(target.into);if(host&&host.children)host.children.push(b);else loc.list.splice(loc.i,0,b);return;}
     if(target.beforeId){var t=findList(target.beforeId);if(t)t.list.splice(t.i,0,b);else loc.list.splice(loc.i,0,b);return;}
     if(target.end)s.blocks.push(b);else loc.list.splice(loc.i,0,b);});}
 function focusSel(){if(!S.sel)return;var el=document.querySelector('.blk[data-id="'+S.sel+'"]');
   if(el){el.focus();el.scrollIntoView({block:'nearest'});}}
 function select(id){S.sel=id;S.insertInto=null;
   document.querySelectorAll('.blk.sel').forEach(function(x){x.classList.remove('sel');});
   if(id){var el=document.querySelector('.blk[data-id="'+id+'"]');
     if(el)el.classList.add('sel');}
   renderInspector();}
 // ---- selection + canvas events ----------------------------------------------
 T('canvasWrap').addEventListener('click',function(e){
   var ak=e.target.closest?e.target.closest('.addkid'):null;
   if(ak){S.sel=ak.getAttribute('data-into');S.insertInto=S.sel;render();
     toast('Now pick a block from the palette; it goes inside.');return;}
   var bb=e.target.closest?e.target.closest('.bb'):null;
   if(bb){var blk0=bb.closest('.blk');var id0=blk0.getAttribute('data-id');
     if(bb.getAttribute('data-act')==='del')confirmBox('Delete this block?',
       'It goes away along with anything inside it. Undo brings it back.',function(){delBlock(id0);});
     else dupBlock(id0);return;}
   var blk=e.target.closest?e.target.closest('.blk'):null;
   if(blk)select(blk.getAttribute('data-id'));
   else select(null);});
 T('canvasWrap').addEventListener('keydown',function(e){
   var blk=e.target.closest?e.target.closest('.blk'):null;if(!blk)return;
   var id=blk.getAttribute('data-id');
   if(e.key==='Delete'||e.key==='Backspace'){e.preventDefault();delBlock(id);}
   else if(e.key==='ArrowUp'&&e.altKey){e.preventDefault();moveBlock(id,'up');}
   else if(e.key==='ArrowDown'&&e.altKey){e.preventDefault();moveBlock(id,'down');}
   else if(e.key==='ArrowLeft'&&e.altKey){e.preventDefault();moveBlock(id,'out');}
   else if(e.key==='ArrowRight'&&e.altKey){e.preventDefault();moveBlock(id,'in');}
   else if(e.key==='ArrowUp'||e.key==='ArrowDown'){e.preventDefault();
     var all=[].slice.call(document.querySelectorAll('.blk'));
     var ix=all.indexOf(blk)+(e.key==='ArrowDown'?1:-1);
     if(ix>=0&&ix<all.length){select(all[ix].getAttribute('data-id'));focusSel();}}
   else if((e.key==='c'||e.key==='C')&&(e.ctrlKey||e.metaKey)){e.preventDefault();copyBlock(id);}
   else if((e.key==='v'||e.key==='V')&&(e.ctrlKey||e.metaKey)){e.preventDefault();pasteBlock(id);}
   else if((e.key==='d'||e.key==='D')&&(e.ctrlKey||e.metaKey)){e.preventDefault();dupBlock(id);}});
 // ---- drag and drop ------------------------------------------------------------
 var drag={type:null,id:null};
 document.addEventListener('dragstart',function(e){
   var pc=e.target.closest?e.target.closest('.pcard'):null;
   if(pc){drag={type:pc.getAttribute('data-type'),id:null};e.dataTransfer.effectAllowed='copy';return;}
   var blk=e.target.closest?e.target.closest('.blk'):null;
   if(blk){drag={type:null,id:blk.getAttribute('data-id')};e.dataTransfer.effectAllowed='move';}});
 function clearDropUI(){document.querySelectorAll('.blk.dropin').forEach(function(x){x.classList.remove('dropin');});
   var dl=T('droplineEl');if(dl)dl.remove();}
 T('canvasWrap').addEventListener('dragover',function(e){
   if(!drag.type&&!drag.id)return;e.preventDefault();clearDropUI();
   var wr0=T('canvasWrap'),wrr=wr0.getBoundingClientRect();
   if(e.clientY<wrr.top+44)wr0.scrollTop-=16;
   else if(e.clientY>wrr.bottom-44)wr0.scrollTop+=16;
   var blk=e.target.closest?e.target.closest('.blk'):null;
   if(blk){var r=blk.getBoundingClientRect();var b=find(blk.getAttribute('data-id'));
     var isCont=b&&__BLOCKS[b.type]&&__BLOCKS[b.type].kids;
     var relY=(e.clientY-r.top)/r.height;
     if(isCont&&relY>0.33&&relY<0.67){blk.classList.add('dropin');return;}
     var dl=document.createElement('div');dl.id='droplineEl';dl.className='dropline show';
     if(relY<=0.5)blk.parentNode.insertBefore(dl,blk);
     else blk.parentNode.insertBefore(dl,blk.nextSibling&&blk.nextSibling.classList&&blk.nextSibling.classList.contains('kids')?blk.nextSibling.nextSibling:blk.nextSibling);
     dl.setAttribute('data-before',relY<=0.5?blk.getAttribute('data-id'):(afterIdOf(blk)||''));}});
 function afterIdOf(blk){var n=blk.nextSibling;
   while(n&&(!n.classList||n.classList.contains('kids')||n.id==='droplineEl'))n=n.nextSibling;
   return n&&n.classList&&n.classList.contains('blk')?n.getAttribute('data-id'):null;}
 T('canvasWrap').addEventListener('drop',function(e){
   if(!drag.type&&!drag.id)return;e.preventDefault();
   var into=document.querySelector('.blk.dropin');
   var dl=T('droplineEl');
   var target=null;
   if(into)target={into:into.getAttribute('data-id')};
   else if(dl)target=dl.getAttribute('data-before')?{beforeId:dl.getAttribute('data-before')}:{end:true};
   else target={end:true};
   clearDropUI();
   if(drag.type){var nb=newBlock(drag.type);
     if(countBlocks((S.script||{}).blocks||[])>=500){toast('Block limit reached (500).');return;}
     if(!S.script){toast('Open or create a script first.');return;}
     S._animId=nb.id;
     apply(function(s){
       if(target.into){var h=find(target.into);if(h&&h.children){h.children.push(nb);S.sel=nb.id;return;}}
       if(target.beforeId){var t=findList(target.beforeId);if(t){t.list.splice(t.i,0,nb);S.sel=nb.id;return;}}
       s.blocks.push(nb);S.sel=nb.id;});}
   else if(drag.id)moveTo(drag.id,target);
   drag={type:null,id:null};});
 document.addEventListener('dragend',clearDropUI);
 // ---- palette ---------------------------------------------------------------
 document.querySelectorAll('.pcard').forEach(function(pc){
   pc.addEventListener('click',function(){addBlock(pc.getAttribute('data-type'));});
   pc.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();addBlock(pc.getAttribute('data-type'));}});
   pc.addEventListener('mouseenter',function(){showHelpFor(pc.getAttribute('data-type'));});});
 // ---- clipboard: copy/paste whole block trees (ids regenerated, params
 //      re-clamped client-side; the Python validator still gates save/run) ----
 var CLIP=null;
 function sanitizeTree(src){if(!src||typeof src!=='object')return null;
   var d=__BLOCKS[src.type];if(!d)return null;
   var nb={id:freshId(),type:src.type,params:{}};
   (d.params||[]).forEach(function(p){var v=(src.params||{})[p.key];
     if(p.type==='int'){v=parseInt(v,10);if(isNaN(v))v=p['default'];
       v=Math.max(p.range[0],Math.min(p.range[1],v));}
     else if(p.type==='bool')v=!!v;
     else if(p.type==='choice'){if(!p.choices.some(function(c){return c[0]===v;}))v=p['default'];}
     else v=String(v==null?'':v).slice(0,p.max||200);
     nb.params[p.key]=v;});
   if(d.kids){nb.children=[];(src.children||[]).forEach(function(k){
     var c=sanitizeTree(k);if(c)nb.children.push(c);});}
   return nb;}
 function copyBlock(id){var b=find(id);if(!b)return;
   CLIP=JSON.parse(JSON.stringify(b));
   var d=__BLOCKS[b.type]||{name:'block'};
   toast('Copied '+d.name+'.');}
 function pasteBlock(afterId){
   if(!CLIP){toast('Nothing copied yet.');return;}
   if(!S.script){toast('Open or create a script first.');return;}
   var nb=sanitizeTree(CLIP);
   if(!nb){toast('That copied block cannot be pasted here.','err');return;}
   if(countBlocks(S.script.blocks)+countBlocks([nb])>500){toast('Block limit reached (500).','err');return;}
   S._animId=nb.id;
   apply(function(s){var tid=afterId||S.sel;
     if(tid){var loc=findList(tid);if(loc){loc.list.splice(loc.i+1,0,nb);S.sel=nb.id;return;}}
     s.blocks.push(nb);S.sel=nb.id;});
   focusSel();}
 // ---- context menu ----
 var ctxEl=T('ctx');
 function ctxAway(e){if(!ctxEl.contains(e.target))closeCtx();}
 function closeCtx(){ctxEl.style.display='none';ctxEl.innerHTML='';
   document.removeEventListener('mousedown',ctxAway,true);}
 function openCtx(x,y,id){
   var items=id?[
     ['Duplicate','Ctrl+D',function(){dupBlock(id);}],
     ['Copy','Ctrl+C',function(){copyBlock(id);}],
     ['Paste after','Ctrl+V',function(){pasteBlock(id);},!CLIP],
     null,
     ['Move up','Alt+\u2191',function(){moveBlock(id,'up');}],
     ['Move down','Alt+\u2193',function(){moveBlock(id,'down');}],
     ['Nest into the block above','Alt+\u2192',function(){moveBlock(id,'in');}],
     ['Un-nest','Alt+\u2190',function(){moveBlock(id,'out');}],
     null,
     ['Delete','Del',function(){delBlock(id);},false,true]]
   :[
     ['Add a block\u2026','/',function(){openQins(true);}],
     ['Paste','Ctrl+V',function(){pasteBlock(null);},!CLIP]];
   ctxEl.innerHTML='';
   items.forEach(function(it){
     if(!it){var sp=document.createElement('div');sp.className='sep';ctxEl.appendChild(sp);return;}
     var b=document.createElement('button');b.type='button';b.setAttribute('role','menuitem');
     b.innerHTML=esc(it[0])+'<span class="kbd">'+esc(it[1])+'</span>';
     if(it[4])b.className='danger';
     if(it[3])b.disabled=true;
     b.onclick=function(){closeCtx();it[2]();};
     ctxEl.appendChild(b);});
   ctxEl.style.display='block';
   var W=window.innerWidth,H=window.innerHeight,r=ctxEl.getBoundingClientRect();
   ctxEl.style.left=Math.max(4,Math.min(x,W-r.width-8))+'px';
   ctxEl.style.top=Math.max(4,Math.min(y,H-r.height-8))+'px';
   document.addEventListener('mousedown',ctxAway,true);
   var fb=ctxEl.querySelector('button:not(:disabled)');if(fb)fb.focus();}
 T('canvasWrap').addEventListener('contextmenu',function(e){
   e.preventDefault();
   var blk=e.target.closest?e.target.closest('.blk'):null;
   if(blk){select(blk.getAttribute('data-id'));openCtx(e.clientX,e.clientY,S.sel);}
   else if(S.script)openCtx(e.clientX,e.clientY,null);});
 document.addEventListener('keydown',function(e){
   if(e.key==='Escape'&&ctxEl.style.display==='block')closeCtx();});
 // ---- quick insert: press / and type the block's name ----
 var qinsAtEnd=false,qinsSel=0;
 function openQins(atEnd){qinsAtEnd=!!atEnd;qinsSel=0;
   T('qins').classList.add('show');
   var inp=T('qinsin');inp.value='';qinsRender('');inp.focus();}
 function closeQins(){T('qins').classList.remove('show');}
 function qinsMatches(q){q=q.toLowerCase();var out=[];
   for(var t in __BLOCKS){var d=__BLOCKS[t];
     if(!q||d.name.toLowerCase().indexOf(q)>=0||t.indexOf(q)>=0)out.push(t);}
   return out;}
 function qinsRender(q){var list=qinsMatches(q);var L=T('qinslist');
   qinsSel=Math.max(0,Math.min(qinsSel,list.length-1));
   L.innerHTML=list.map(function(t,i){var d=__BLOCKS[t];
     return '<div class="qi'+(i===qinsSel?' on':'')+'" data-type="'+t+'" role="option"'+
       (i===qinsSel?' aria-selected="true"':'')+'><span class="pico">'+d.icon+'</span>'+
       esc(d.name)+'<span class="qgrp">'+d.group+'</span></div>';}).join('')
     ||'<div class="qi" style="cursor:default;color:var(--dim)">No block matches that.</div>';
   L.querySelectorAll('.qi[data-type]').forEach(function(el){
     el.onclick=function(){qinsPick(el.getAttribute('data-type'));};});}
 function qinsPick(t){closeQins();
   if(qinsAtEnd){S.sel=null;S.insertInto=null;}
   addBlock(t);}
 (function(){var inp=T('qinsin');
   inp.addEventListener('input',function(){qinsSel=0;qinsRender(inp.value.trim());});
   inp.addEventListener('keydown',function(e){
     var items=T('qinslist').querySelectorAll('.qi[data-type]');
     if(e.key==='ArrowDown'){e.preventDefault();qinsSel=Math.min(items.length-1,qinsSel+1);qinsRender(inp.value.trim());}
     else if(e.key==='ArrowUp'){e.preventDefault();qinsSel=Math.max(0,qinsSel-1);qinsRender(inp.value.trim());}
     else if(e.key==='Enter'){e.preventDefault();var el=items[qinsSel];
       if(el)qinsPick(el.getAttribute('data-type'));}
     else if(e.key==='Escape'){closeQins();}});})();
 document.addEventListener('keydown',function(e){
   if(e.key!=='/'||e.ctrlKey||e.metaKey||e.altKey)return;
   var tg=((e.target&&e.target.tagName)||'').toLowerCase();
   if(tg==='input'||tg==='textarea'||tg==='select')return;
   if(T('tplmodal').classList.contains('show')||T('cfmmodal').classList.contains('show'))return;
   if(!S.script)return;
   e.preventDefault();openQins(false);});
 document.addEventListener('mousedown',function(e){var q=T('qins');
   if(q.classList.contains('show')&&!q.contains(e.target))closeQins();});
 (function(){var ae=T('addend');if(ae)ae.onclick=function(){openQins(true);};})();
 // ---- palette filter ----
 (function(){var pf=T('palfilter');if(!pf)return;
   pf.addEventListener('input',function(){var q=pf.value.trim().toLowerCase();
     document.querySelectorAll('.pcard').forEach(function(c){
       var d=__BLOCKS[c.getAttribute('data-type')]||{name:''};
       c.style.display=(!q||d.name.toLowerCase().indexOf(q)>=0)?'':'none';});
     document.querySelectorAll('.pgroup').forEach(function(g){
       var any=[].some.call(g.querySelectorAll('.pcard'),function(c){return c.style.display!=='none';});
       g.style.display=any?'':'none';});});})();
 // ---- hovering a canvas block explains it, same as the palette ----
 T('canvasWrap').addEventListener('mouseover',function(e){
   var blk=e.target.closest?e.target.closest('.blk'):null;
   if(blk){var b=find(blk.getAttribute('data-id'));if(b)showHelpFor(b.type);}});
 // ---- help ------------------------------------------------------------------
 function md(t){t=String(t==null?'':t);var lines=t.split(/\r?\n/),out=[],list=null,para=[];
   var TAGS={raise:['↑','raise it if','up'],lower:['↓','lower it if','dn'],fixes:['⚑','fixes','fx'],pairs:['⇄','pairs with','ok'],healthy:['✓','healthy','ok'],climbing:['⚑','climbing?','fx'],fixpath:['→','fix path','up'],wrongif:['⚑','wrong if','fx'],owns:['→','its knobs','up'],when:['→','use it when','up']};
   function il(s){return s.replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>').replace(/\*([^*\n]+)\*/g,'<i>$1</i>').replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\{\{([^}]+)\}\}/g,'<s>$1</s>');}
   function chips(s){return s.split('|').map(function(c){c=c.trim();if(!c)return '';return '<span class="ph-lk">'+il(c)+'</span>';}).join('');}
   function fp(){if(para.length){out.push('<p>'+il(para.join(' '))+'</p>');para=[];}}
   function fl(){if(list){out.push('<ul>'+list.join('')+'</ul>');list=null;}}
   for(var i=0;i<lines.length;i++){var ln=lines[i].trim();
     if(!ln){fp();fl();continue;}
     var mh=ln.match(/^##\s+(.*)/);if(mh){fp();fl();out.push('<h4>'+il(mh[1])+'</h4>');continue;}
     var mt=ln.match(/^(raise|lower|fixes|pairs|healthy|climbing|fixpath|wrongif|owns|when):\s*(.*)/i);
     if(mt){fp();fl();var tg=TAGS[mt[1].toLowerCase()];var body=(mt[1].toLowerCase()==='pairs'||mt[1].toLowerCase()==='fixpath'||mt[1].toLowerCase()==='owns')?chips(mt[2]):il(mt[2]);
       out.push('<div class="ph-row"><span class="ph-tag '+tg[2]+'"><i>'+tg[0]+'</i>'+tg[1]+'</span><span class="ph-tx">'+body+'</span></div>');continue;}
     var ms=ln.match(/^steps:\s*(.*)/i);
     if(ms){fp();fl();var st=ms[1].split('|');var sh='';for(var j=0;j<st.length;j++){if(st[j].trim())sh+='<div class="ph-step"><i>'+(j+1)+'</i><span>'+il(st[j].trim())+'</span></div>';}out.push('<div class="ph-steps">'+sh+'</div>');continue;}
     var mb=ln.match(/^[-•]\s+(.*)/);if(mb){fp();if(!list)list=[];list.push('<li>'+il(mb[1])+'</li>');continue;}
     var mc=ln.match(/^(Example|Tip|Note|Why)\s*:\s*(.*)/);if(mc){fp();fl();out.push('<div class="ph-call"><b>'+mc[1]+'</b> '+il(mc[2])+'</div>');continue;}
     fl();para.push(ln);}
   fp();fl();return out.join('')||'<p></p>';}
 function showHelpFor(type){var hb=T('helpbox');
   if(!type){hb.innerHTML='<h3>Prospector Studio</h3><div class="ph-kind">help</div><div class="ph-body">'+
     '<p>Build your own farming mode from blocks. The macro runs your blocks top to bottom, then repeats forever, '+
     'counting pans exactly like a built-in mode. Hover any palette block to read what it does.</p>'+
     '<p>Esc and Ctrl+K always stop a running script, and the safety nets stay on.</p></div>';return;}
   var d=__BLOCKS[type];var ent=S.helpmap['studio:'+type]||{};
   hb.innerHTML='<h3>'+esc(d?d.name:type)+'</h3><div class="ph-kind">'+(d&&d.kids?'container block':'block')+'</div>'+
     '<div class="ph-body">'+md(ent.body||'')+'</div>';}
 // ---- problems bar click -> select the block ---------------------------------
 T('probbar').addEventListener('click',function(e){var pr=e.target.closest?e.target.closest('.prob'):null;
   if(!pr)return;var id=pr.getAttribute('data-target');if(id){S.sel=id;render();focusSel();}});
 // ---- name/desc --------------------------------------------------------------
 T('scname').addEventListener('input',function(){if(!S.script)return;
   apply(function(s){s.name=T('scname').value;},'meta:name',true);});
 T('scdesc').addEventListener('input',function(){if(!S.script)return;
   apply(function(s){s.description=T('scdesc').value;},'meta:desc',true);});
 // ---- confirm modal ------------------------------------------------------------
 var cfmCb=null;
 function confirmBox(title,body,cb){T('cfmtitle').textContent=title;T('cfmbody').textContent=body;
   cfmCb=cb;T('cfmmodal').classList.add('show');T('cfmyes').focus();}
 T('cfmyes').onclick=function(){T('cfmmodal').classList.remove('show');var cb=cfmCb;cfmCb=null;if(cb)cb();};
 T('cfmno').onclick=function(){T('cfmmodal').classList.remove('show');cfmCb=null;};
 // ---- library / templates ------------------------------------------------------
 function guardDirty(cb){if(S.dirty&&S.script)confirmBox('Throw away unsaved changes?',
   '"'+(S.script.name||'This script')+'" has edits that are not saved yet.',cb);else cb();}
 async function refreshLib(keep){var r;try{r=await api().studio_list();}catch(e){return;}
   if(!r||!r.ok)return;S.lib=r.scripts;var sel=T('libsel');
   sel.innerHTML='<option value="">Open a script&hellip;</option>'+r.scripts.map(function(s){
     return '<option value="'+esc(s.name)+'"'+(S.script&&s.name===S.prevName?' selected':'')+'>'+
       esc(s.name)+(s.active?' (active)':'')+'</option>';}).join('');
   if(keep&&S.prevName)sel.value=S.prevName;}
 window.loadScript=function(name){guardDirty(async function(){
   var r;try{r=await api().studio_get(name);}catch(e){return;}
   if(!r||!r.ok){toast((r&&r.error)||'Could not open it.');return;}
   S.script=r.script;if(!Array.isArray(S.script.blocks))S.script.blocks=[];
   S.prevName=r.script.name;S.sel=null;S.undo=[];S.redo=[];S.dirty=false;
   render();refreshLib(true);});};
 T('libsel').addEventListener('change',function(){var v=T('libsel').value;if(v)loadScript(v);});
 function uniqueName(base){var names={};S.lib.forEach(function(s){names[s.name]=1;});
   if(!names[base])return base;var i=2;while(names[base+' '+i])i++;return base+' '+i;}
 async function openTplModal(cancelable){
   if(!S.templates.length){try{var r=await api().studio_templates();S.templates=(r&&r.templates)||[];}catch(e){}}
   var tl=T('tpllist');
   tl.innerHTML=S.templates.map(function(t,i){
     return '<div class="tplcard" tabindex="0" role="button" data-i="'+i+'"><div>'+
       '<h4>'+esc(t.name)+'</h4><p>'+esc(t.description)+'</p></div></div>';}).join('');
   tl.querySelectorAll('.tplcard').forEach(function(c){
     function go(){useTemplate(parseInt(c.getAttribute('data-i'),10));}
     c.onclick=go;c.onkeydown=function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();go();}};});
   T('tplmodal').classList.add('show');}
 function useTemplate(i){var t=S.templates[i];if(!t)return;
   T('tplmodal').classList.remove('show');
   guardDirty(function(){
     var sc=JSON.parse(JSON.stringify(t));
     sc.name=uniqueName(sc.name==='Blank'?'My script':sc.name);
     S.script=sc;S.prevName=null;S.sel=null;S.undo=[];S.redo=[];S.dirty=true;render();
     toast('New script. Press Save when you are happy with it.');});}
 window.newScript=function(){openTplModal(true);};
 T('stnewbtn').onclick=function(){openTplModal(true);};
 T('tplcancel').onclick=function(){T('tplmodal').classList.remove('show');};
 // ---- save / validate / run ------------------------------------------------------
 async function doSave(){if(!S.script){toast('Nothing to save yet.');return false;}
   S.script.name=(T('scname').value||'').trim();S.script.description=T('scdesc').value;
   if(!S.script.name){toast('Give the script a name first.');T('scname').focus();return false;}
   var r;try{r=await api().studio_save(S.script,S.prevName);}catch(e){toast('Save failed.');return false;}
   if(!r||!r.ok){renderProblems([(r&&r.error)||'Save failed.']);toast((r&&r.error)||'Save failed.','err');return false;}
   S.prevName=r.name;S.dirty=false;document.body.classList.remove('isdirty');
   refreshLib(true);render();
   toast(r.problems&&r.problems.length?'Saved. Fix the flagged steps before running it.':'Saved.');
   return !(r.problems&&r.problems.length);}
 T('stsave').onclick=doSave;
 T('valbtn').onclick=async function(){if(!S.script)return;
   S.script.name=(T('scname').value||'').trim()||S.script.name;
   var r;try{r=await api().studio_validate(S.script);}catch(e){return;}
   if(!r)return;
   var extra=(r.errors||[]);render();renderProblems(extra);
   toast(r.ok?((r.problems&&r.problems.length)?'Almost: '+r.problems.length+' thing(s) to fix.':'All good. Ready to run.')
            :'Problems found; see the list below.');};
 T('strun').onclick=async function(){if(!S.script)return;
   var clean=await doSave();if(!clean){return;}
   var r;try{r=await api().studio_run(S.prevName);}catch(e){toast('Could not start.');return;}
   if(!r||!r.ok){toast((r&&r.error)||'Could not start.','err');return;}
   toast('Running. Click into Roblox so the game has focus. Esc stops.');pollState();};
 T('ststop').onclick=async function(){try{await api().studio_stop();}catch(e){}pollState();};
 T('undobtn').onclick=undo;T('redobtn').onclick=redo;
 document.addEventListener('keydown',function(e){
   if((e.ctrlKey||e.metaKey)&&!e.shiftKey&&(e.key==='z'||e.key==='Z')){e.preventDefault();undo();}
   else if((e.ctrlKey||e.metaKey)&&(e.key==='y'||e.key==='Y'||((e.key==='z'||e.key==='Z')&&e.shiftKey))){e.preventDefault();redo();}
   else if((e.ctrlKey||e.metaKey)&&(e.key==='s'||e.key==='S')){e.preventDefault();doSave();}});
 // ---- live state ------------------------------------------------------------------
 var pollT=null;
 async function pollState(){clearTimeout(pollT);
   var r;try{r=await api().studio_state();}catch(e){r=null;}
   if(r&&r.ok){S.running=!!r.running;document.body.classList.toggle('running',S.running);
     T('strun').disabled=S.running;T('ststop').disabled=!S.running;
     var rp=T('runpill');rp.innerHTML='&#9679; running'+(r.active?(': '+esc(r.active)):'');}
   pollT=setTimeout(pollState,1500);}
 window.scriptStep=function(info){if(!info)return;
   document.querySelectorAll('.blk.live').forEach(function(x){x.classList.remove('live');});
   var el=document.querySelector('.blk[data-id="'+esc(info.id||'')+'"]');
   if(el)el.classList.add('live');};
 // ---- walkthrough (steps come from the shared tutorial registry) --------------------
 var TOUR=[],ti=0;
 function tourPlace(){var st=TOUR[ti];if(!st)return;var tp=T('sttourpop'),sp=T('sttourspot');
   T('sttourstep').textContent='Walkthrough · step '+(ti+1)+' of '+TOUR.length;
   T('sttourtitle').textContent=st.title||'';T('sttourbody').innerHTML=st.body||'';
   T('sttourback').style.visibility=ti>0?'visible':'hidden';
   T('sttournext').textContent=ti===TOUR.length-1?'Finish':'Next';
   var el=st.sel?document.querySelector(st.sel):null;var r=el?el.getBoundingClientRect():null;
   if(!r||!(r.width>0)){sp.style.display='none';tp.classList.add('center');tp.style.left='';tp.style.top='';return;}
   sp.style.display='block';tp.classList.remove('center');
   sp.style.left=(r.left-7)+'px';sp.style.top=(r.top-7)+'px';
   sp.style.width=(r.width+14)+'px';sp.style.height=(r.height+14)+'px';
   var W=window.innerWidth,H=window.innerHeight,pw=Math.min(340,W*0.86),ph=tp.offsetHeight||220;
   var x=r.right+18,y=r.top;
   if(x+pw>W-10)x=Math.max(10,r.left-pw-18);
   if(x+pw>W-10||x<10){x=Math.max(10,Math.min(W-pw-10,r.left));y=r.bottom+16;}
   if(y+ph>H-10)y=Math.max(10,H-ph-10);
   tp.style.left=x+'px';tp.style.top=y+'px';}
 function tourShow(){if(!TOUR.length)return;T('sttour').style.display='block';tourPlace();}
 function tourEnd(){T('sttour').style.display='none';try{api().studio_meta({editor_tour_seen:true});}catch(e){}}
 T('sttournext').onclick=function(){if(ti>=TOUR.length-1){tourEnd();return;}ti++;tourPlace();};
 T('sttourback').onclick=function(){if(ti>0){ti--;tourPlace();}};
 T('sttourskip').onclick=tourEnd;
 document.addEventListener('keydown',function(e){if(T('sttour').style.display==='block'){
   if(e.key==='Escape')tourEnd();
   else if(e.key==='ArrowRight')T('sttournext').click();
   else if(e.key==='ArrowLeft')T('sttourback').click();}});
 window.addEventListener('resize',function(){if(T('sttour').style.display==='block')tourPlace();});
 async function startEditorTour(){
   if(!TOUR.length){try{var c=await api().tutorial_content();
     TOUR=(c&&c.tours&&c.tours.studio_editor)||[];S.helpmap=(c&&c.help)||S.helpmap;}catch(e){}}
   if(!TOUR.length)return;ti=0;tourShow();}
 T('sthelp').onclick=startEditorTour;
 T('offeryes').onclick=function(){T('touroffer').classList.remove('show');startEditorTour();};
 T('offerno').onclick=function(){T('touroffer').classList.remove('show');
   try{api().studio_meta({editor_tour_seen:true});}catch(e){}};
 // ---- boot -----------------------------------------------------------------------
 async function boot(){
   try{var c=await api().tutorial_content();S.helpmap=(c&&c.help)||{};
     TOUR=(c&&c.tours&&c.tours.studio_editor)||[];}catch(e){}
   try{var m=await api().studio_meta();S.meta=(m&&m.meta)||{};}catch(e){}
   await refreshLib();
   if(!S.script){
     if(S.lib.length){loadScript(S.lib[0].name);}
     else{render();openTplModal(true);}}
   else render();
   if(!S.meta.editor_tour_seen)T('touroffer').classList.add('show');
   showHelpFor(null);pollState();}
 window.__reload=function(){refreshLib(true);pollState();};
 window.addEventListener('pywebviewready',boot);
 setTimeout(function(){if(!S.helpmap||!Object.keys(S.helpmap).length)boot();},900);
</script></body></html>'''
    return (html.replace("{{PALETTE}}", "".join(palette))
                .replace("{{BLOCKS}}", blocks_json))


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
    global _window, _pill, _overlay, _coach_win, _analytics_win, _hud, _studio_win
    # Capability handshake (Studio run preflight): print the engine's OWN
    # generated capability manifest and exit. Works identically in dev
    # (python3 prospecting_app.py --capabilities) and frozen (the sidecar
    # exe with --capabilities) — an older build that lacks this flag simply
    # opens no manifest, which the preflight treats as UNKNOWN/stale.
    if "--capabilities" in sys.argv:
        from prospector_engine.engine import script_capabilities
        print(json.dumps(script_capabilities(), indent=1, sort_keys=True))
        return
    # Frozen macro mode: the bundled exe re-invokes itself with --run-macro to
    # run the actual macro in-process (there is no separate python.exe).
    # [Phase 04 C6] --run-engine is the same re-exec under the engine's own
    # name; the legacy --run-macro alias is kept.
    if FROZEN and ("--run-macro" in sys.argv or "--run-engine" in sys.argv):
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
    try:
        _scrub_config_file()   # drop legacy access/tracking keys from disk
    except Exception:
        pass
    print("[boot] building main window…", flush=True)
    _window = webview.create_window(
        "Prospector Macro" if STUDIO_LAUNCH else APP_NAME,
        html=_themed(build_html()),
                                    js_api=api, width=1340, height=900,
                                    min_size=(600, 560))
    try:
        _window.events.closed += lambda: _quit_everything(api)
    except Exception:
        pass
    global _pill, _overlay
    try:
        import Quartz as _Q
        _b = _Q.CGDisplayBounds(_Q.CGMainDisplayID())
        _sw, _sh = int(_b.size.width), int(_b.size.height)
    except Exception:
        _sw, _sh = 1440, 900
    print("[boot] main ok -> pill", flush=True)
    try:
        _pill = webview.create_window(
            APP_NAME, html=_themed(PILL_HTML), js_api=api,
            width=272, height=178, frameless=True, on_top=True,
            easy_drag=True, resizable=False, hidden=True)
    except Exception as _e:
        print("[pill] precreate failed: %s" % _e)
    print("[boot] pill ok -> hud (PP_NO_HUD=1 skips it)", flush=True)
    try:
        if os.environ.get("PP_NO_HUD"):
            raise RuntimeError("skipped by PP_NO_HUD")
        _hud = webview.create_window(
            "HUD, Prospector Lite", html=_themed(_hud_html()), js_api=api,
            width=384, height=470, frameless=True, on_top=True,
            easy_drag=True, resizable=False, hidden=True)
    except Exception as _e:
        print("[hud] precreate failed: %s" % _e)
    print("[boot] hud ok -> calibrate overlay", flush=True)
    try:
        _overlay = webview.create_window(
            "Calibrate", html=_themed(_OVERLAY_HTML), js_api=api,
            x=0, y=0, width=_sw, height=_sh,
            frameless=True, on_top=True, easy_drag=False, hidden=True)
    except Exception as _e:
        print("[overlay] precreate failed: %s" % _e)
    print("[boot] overlay ok -> coach", flush=True)
    try:
        _coach_win = webview.create_window(
            "Coach, Prospector Lite", html=_themed(COACH_HTML), js_api=api,
            width=900, height=820, min_size=(560, 560), hidden=True)
        _hide_on_close(_coach_win)
    except Exception as _e:
        print("[coach] precreate failed: %s" % _e)
    print("[boot] coach ok -> analytics", flush=True)
    try:
        _analytics_win = webview.create_window(
            "Analytics, Prospector Lite", html=_themed(ANALYTICS_HTML), js_api=api,
            width=980, height=860, min_size=(620, 560), hidden=True)
        _hide_on_close(_analytics_win)
    except Exception as _e:
        print("[analytics] precreate failed: %s" % _e)
    print("[boot] analytics ok -> studio", flush=True)
    if not STUDIO_LAUNCH:
        # The embedded block editor is a standalone-Lite surface. Under a
        # Prospector Studio launch, authoring lives in Prospector Studio
        # and the legacy editor window must never exist, let alone appear.
        try:
            _studio_win = webview.create_window(
                "Studio, Prospector Lite", html=_themed(_studio_html()),
                js_api=api, width=1200, height=800, min_size=(720, 540),
                hidden=True)
            _hide_on_close(_studio_win)
        except Exception as _e:
            print("[studio] precreate failed: %s" % _e)
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
    # PP_DEBUG=1 opens the webview inspector so bridge/JS failures are
    # visible instead of dying silently inside the packaged WKWebView.
    webview.start(debug=bool(os.environ.get("PP_DEBUG")))
    _quit_everything(api)        # normal close path -> kill engine + hard exit


if __name__ == "__main__":
    main()
