#!/usr/bin/env python3
"""lite_trust.py -- Prospector Lite's capability registry and trust facts.

This module is the single source of truth for every OS-sensitive capability
the application has. It drives:

  * the first-run "Trust & Permissions" screen (Step 2 of setup),
  * the permanent Trust Center,
  * the Readiness Check,
  * PERMISSIONS.md and the trust documentation,
  * the build-time trust manifest (exact source references per capability),
  * the regression tests in onboarding_trust_tests.py.

Design rules (enforced by tests):
  - Every capability listed here is REAL: it maps to code that exists in this
    repository, referenced by module + symbol, resolved to file + line range
    at build time.
  - Status is never faked. On macOS the three TCC permissions are read with
    the real preflight APIs (CGPreflightScreenCaptureAccess,
    AXIsProcessTrusted, CGPreflightListenEventAccess). On Windows there is no
    equivalent permission model and the UI says so instead of inventing a
    "granted" state.
  - Detection never triggers an OS prompt. Prompts fire only from the
    explicit request_permission()/test_* calls wired to clearly-labelled
    buttons.
  - No capability here requests microphone, camera, location, Full Disk
    Access or administrator rights; the registry instead carries explicit
    NOT_REQUIRED entries so users can see that the application knows about
    those categories and does not want them.

The module is import-safe on every platform: all OS framework imports are
local to the functions that need them.
"""

import base64
import json
import os
import platform as _platform
import subprocess
import sys
import time

FROZEN = getattr(sys, "frozen", False)
_HERE = (os.path.dirname(sys.executable) if FROZEN
         else os.path.dirname(os.path.abspath(__file__)))

# Required levels (Phase-2 classification)
REQUIRED_FOR_CORE = "REQUIRED_FOR_CORE"
REQUIRED_FOR_SPECIFIC_FEATURE = "REQUIRED_FOR_SPECIFIC_FEATURE"
OPTIONAL = "OPTIONAL"
INFORMATIONAL_ONLY = "INFORMATIONAL_ONLY"
NOT_REQUIRED = "NOT_REQUIRED"


def platform_key():
    """'mac', 'win' or 'other' -- which platform column applies here."""
    if sys.platform == "darwin":
        return "mac"
    if os.name == "nt":
        return "win"
    return "other"


# ---------------------------------------------------------------------------
# Capability registry
# ---------------------------------------------------------------------------
# source_references: (module, symbol-or-None, one-line why). Symbols are
# resolved to file + exact line ranges by generate_manifest(); a reference
# that stops resolving fails the build (onboarding_trust_tests.py).

CAPABILITIES = [
    {
        "id": "screen_detection",
        "title": "Screen Detection",
        "short_description":
            "Reads pixels of your screen to see the game's bars and prompts.",
        "detailed_explanation":
            "Prospector Lite works by looking at the screen the way you do: "
            "it samples a handful of calibrated pixels (the pan-fill bar, the "
            "dig bar, the Pan/Deposit/Shake prompts) and, when you enable "
            "trackers, reads the small money/shards/finds regions you drew. "
            "Frames are processed in memory and immediately discarded. "
            "Nothing is recorded, nothing is saved to disk by default, and "
            "no audio is ever captured.",
        "platforms": ["mac", "win"],
        "required_level": REQUIRED_FOR_CORE,
        "features_enabled": [
            "All macro modes (the cycle is driven by what it sees)",
            "Calibration and the live detection test",
            "Earnings / finds trackers (optional regions)"],
        "data_accessed":
            "Visible screen pixels of the display Roblox is on, sampled at "
            "specific calibrated points and small user-drawn regions.",
        "data_retained":
            "None by default. Calibration stores coordinates and reference "
            "colours (numbers), not images. The only screenshots ever "
            "written or sent are ones you explicitly enable: the optional "
            "Discord notification screenshot (off by default).",
        "network_behaviour": "None. Detection is entirely local.",
        "privacy_notes":
            "macOS labels this permission category 'Screen Recording' (newer "
            "versions: 'Screen & System Audio Recording'). The OS label "
            "covers more than this app uses: Prospector Lite reads screen "
            "pixels only and has no audio-capture code at all -- see the "
            "source references.",
        "permission_category": {"mac": "Screen Recording", "win": None},
        "operating_system_label": {
            "mac": "System Settings > Privacy & Security > Screen Recording "
                   "(shown as 'Screen & System Audio Recording' on recent "
                   "macOS)",
            "win": "Windows has no screen-capture permission prompt for "
                   "desktop apps; capture either works or is blocked by "
                   "special content/remote-desktop setups."},
        "request_strategy": {
            "mac": "explicit_request",   # CGRequestScreenCaptureAccess on click
            "win": "none"},
        "detection_strategy": {
            "mac": "CGPreflightScreenCaptureAccess (no prompt)",
            "win": "capability test on demand"},
        "test_strategy":
            "Capture a small centre patch of the screen, report its size and "
            "whether it is non-blank, show it once in-app, then discard it.",
        "revoke_instructions": {
            "mac": "System Settings > Privacy & Security > Screen Recording: "
                   "switch off Prospector Lite. macOS may relaunch the app.",
            "win": "Not applicable -- there is no permission to revoke. "
                   "Uninstalling or quitting the app is the control."},
        "declined_behaviour":
            "The app opens and everything except detection works. Starting a "
            "macro is blocked with a clear message, because a blind macro "
            "would misclick. Calibration tests report the denied capture "
            "honestly (black frames).",
        "source_references": [
            ("prospector_engine.sensing", "Sensing._grab_full",
             "the calibration/full-frame capture"),
            ("prospector_engine.sensing", "Sensing.sample_saved",
             "the 6x6 px live sampling around each calibrated point"),
            ("prospector_engine.engine", "Detector",
             "the run-time pixel detector fed by mss grabs"),
            ("prospector_engine.engine", "_grab_screenshot_b64",
             "the only screenshot encoder on the notification path; used "
             "solely for the opt-in Discord screenshot"),
            ("lite_trust", "test_screen_capture",
             "the in-app capability test"),
        ],
        "local_document": "PERMISSIONS.md",
    },
    {
        "id": "input_control",
        "title": "Keyboard & Mouse Control",
        "short_description":
            "Presses ordinary keys and mouse buttons, exactly like a person.",
        "detailed_explanation":
            "The macro plays by pressing the same keys and clicks you would "
            "(W/A/S/D, Space, the left mouse button...). On macOS this uses "
            "the system CGEvent API; on Windows, SendInput. It types nothing "
            "into other applications and reads nothing -- this capability is "
            "output only. Every key and button has a registered release, and "
            "every stop path (Safe Stop, quit, crash recovery) funnels "
            "through a release-everything routine.",
        "platforms": ["mac", "win"],
        "required_level": REQUIRED_FOR_CORE,
        "features_enabled": [
            "All macro modes (movement, digging, panning, shaking)",
            "The in-app control test",
            "Input release on stop (safety)"],
        "data_accessed":
            "None. This is synthetic output; it does not observe your input.",
        "data_retained": "None.",
        "network_behaviour": "None.",
        "privacy_notes":
            "macOS files this under 'Accessibility' because posting events "
            "is an assistive-technology API. Granting it lets the app press "
            "keys and move the mouse; it does not let the app read your "
            "screen or your keystrokes (those are separate permissions).",
        "permission_category": {"mac": "Accessibility", "win": None},
        "operating_system_label": {
            "mac": "System Settings > Privacy & Security > Accessibility",
            "win": "No permission prompt. If Roblox runs as administrator "
                   "and Prospector Lite does not, Windows silently drops the "
                   "input -- run both at the same normal level (neither "
                   "needs admin)."},
        "request_strategy": {
            "mac": "explicit_request",   # AXIsProcessTrustedWithOptions prompt
            "win": "none"},
        "detection_strategy": {
            "mac": "AXIsProcessTrusted (no prompt)",
            "win": "capability test on demand"},
        "test_strategy":
            "A sandbox test inside the app: you click Start Test, the app "
            "sends one harmless keystroke and a 2-pixel pointer wiggle to "
            "itself, and the test panel confirms both the key-down AND the "
            "key-up arrived (proving clean release). The app verifies it "
            "is the focused application before posting, so if you switch "
            "away mid-test nothing is typed anywhere.",
        "revoke_instructions": {
            "mac": "System Settings > Privacy & Security > Accessibility: "
                   "switch off Prospector Lite.",
            "win": "Not applicable -- no permission to revoke."},
        "declined_behaviour":
            "The app opens and settings/calibration viewing work, but "
            "starting a macro is blocked with a clear message (it could not "
            "press anything).",
        "source_references": [
            ("prospector_engine.platform_mac", "key_down",
             "macOS key press (CGEventPost)"),
            ("prospector_engine.platform_mac", "key_up",
             "macOS key release"),
            ("prospector_engine.platform_win", "key_down",
             "Windows key press (SendInput scancode)"),
            ("prospector_engine.engine", "release_all",
             "the release-everything safety floor on every stop path"),
            ("prospecting_app", "_host_release_inputs",
             "host-side release backstop if the engine dies mid-press"),
            ("lite_trust", "test_input_control",
             "the in-app sandbox test"),
        ],
        "local_document": "PERMISSIONS.md",
    },
    {
        "id": "stop_hotkeys",
        "title": "Safe Stop & Global Hotkeys",
        "short_description":
            "Listens for your configured control keys (Esc, Ctrl+K...) so "
            "you can always stop the macro instantly.",
        "detailed_explanation":
            "While the macro runs, a listener watches for the few control "
            "chords you configured -- by default Esc (quit), Ctrl+K "
            "(start/stop), Ctrl+J (soft stop), Ctrl+L (pause) -- even when "
            "Roblox has focus. That is what makes Safe Stop reliable: you "
            "never have to find the app window to stop it. The listener "
            "matches configured chords; it does not log keystrokes, build "
            "any buffer of what you type, or transmit anything.",
        "platforms": ["mac", "win"],
        "required_level": REQUIRED_FOR_CORE,
        "features_enabled": [
            "Safe Stop from anywhere (safety)",
            "Start/stop, pause, soft-stop, relic hotkeys",
            "Optional Studio input recorder (records only while you record)"],
        "data_accessed":
            "Global key events, matched against your configured control "
            "chords. On macOS this uses a listen-only event tap (pynput); "
            "on Windows a 30 ms GetAsyncKeyState poll of the specific keys.",
        "data_retained":
            "None. No keystroke log exists. The optional Studio recorder "
            "stores events only during an explicit recording you start and "
            "stop.",
        "network_behaviour": "None.",
        "privacy_notes":
            "macOS calls this category 'Input Monitoring'. It is the price "
            "of a global panic key: without it, Esc could not stop the "
            "macro while Roblox has focus. The listener code is small and "
            "linked below so you can read exactly what it matches.",
        "permission_category": {"mac": "Input Monitoring", "win": None},
        "operating_system_label": {
            "mac": "System Settings > Privacy & Security > Input Monitoring",
            "win": "No permission prompt (polls specific keys only)."},
        "request_strategy": {
            "mac": "explicit_request",   # CGRequestListenEventAccess on click
            "win": "none"},
        "detection_strategy": {
            "mac": "CGPreflightListenEventAccess (no prompt)",
            "win": "capability test on demand"},
        "test_strategy":
            "Press Esc or Ctrl+K (the default Safe Stop keys) while the "
            "test is armed; the app reports whether it heard it. Nothing "
            "else is captured.",
        "revoke_instructions": {
            "mac": "System Settings > Privacy & Security > Input Monitoring: "
                   "switch off Prospector Lite.",
            "win": "Not applicable -- no permission to revoke."},
        "declined_behaviour":
            "Starting a macro is blocked, because without the listener the "
            "Safe Stop hotkey cannot work and running without a panic key "
            "is not safe. The in-app Stop button would still work, but it "
            "requires reaching the window; Prospector Lite treats the "
            "hotkey as required for your safety.",
        "source_references": [
            ("prospector_engine.platform_mac", "make_listener",
             "the macOS hotkey listener: matches configured chords only"),
            ("prospector_engine.platform_win", "make_listener",
             "the Windows key poller (GetAsyncKeyState, no hook)"),
            ("prospector_engine.recorder", "Recorder",
             "the opt-in Studio input recorder (explicit start/stop)"),
            ("lite_trust", "await_stop_hotkey",
             "the in-app Safe Stop test"),
        ],
        "local_document": "PERMISSIONS.md",
    },
    {
        "id": "discord_notifications",
        "title": "Discord Notifications (optional)",
        "short_description":
            "Optional: posts run events to a Discord webhook URL that YOU "
            "provide. Off by default.",
        "detailed_explanation":
            "If you paste your own Discord webhook URL, the macro can post "
            "you a message on start, stop, Safe Stop, bag-full and periodic "
            "stats. This is the app's only network feature that runs during "
            "a macro, it is disabled by default, the destination is 100% "
            "yours (there is no developer endpoint), and you can preview the "
            "exact payload before enabling it. TLS certificate verification "
            "is mandatory -- there is no insecure fallback. This is a "
            "network feature: it does NOT use the microphone, does not "
            "record audio, and has nothing to do with any recording "
            "permission.",
        "platforms": ["mac", "win"],
        "required_level": OPTIONAL,
        "features_enabled": [
            "Run event pings (start/stop/safe-stop/bag-full/stats)",
            "Optional screenshot attachment (a second, separate opt-in)"],
        "data_accessed":
            "Sends only: event name, your optional display name, run stats "
            "(pans, per-hour, runtime, recoveries) and -- only if you also "
            "enable it -- a downscaled screenshot.",
        "data_retained":
            "Your webhook URL is stored in the local config file. Nothing "
            "else. The URL is never logged in full.",
        "network_behaviour":
            "HTTPS POST to the exact URL you configured, 8-second timeout, "
            "no retry storm (one attempt per event), certificate "
            "verification always on.",
        "privacy_notes":
            "No IP lookup, no location, no machine identifier, no analytics "
            "ride along -- the payload preview in the app is generated by "
            "the same code that sends.",
        "permission_category": {"mac": None, "win": None},
        "operating_system_label": {
            "mac": "No macOS permission involved (outbound network only).",
            "win": "No permission. Windows Firewall does not prompt for "
                   "outbound connections."},
        "request_strategy": {"mac": "user_configuration",
                             "win": "user_configuration"},
        "detection_strategy": {
            "mac": "configured / enabled flags in local settings",
            "win": "configured / enabled flags in local settings"},
        "test_strategy":
            "'Send test notification' posts one test payload to your URL "
            "and shows the result.",
        "revoke_instructions": {
            "mac": "Clear the webhook URL (Notifications page or Trust "
                   "Center) or turn notifications off. Deleting the URL "
                   "removes it from disk.",
            "win": "Same: clear the URL or disable notifications."},
        "declined_behaviour":
            "Nothing changes -- this is off by default. The macro never "
            "needs the network.",
        "source_references": [
            ("prospector_engine.engine", "post_webhook",
             "the only engine egress: fires on run events when enabled"),
            ("prospector_engine.engine", "_webhook_payload",
             "exactly what is sent (also feeds the in-app preview)"),
            ("prospector_engine.engine", "_webhook_send",
             "the sender: HTTPS, verified TLS only, 8s timeout"),
            ("prospector_engine.engine", "_webhook_tls_context",
             "certificate verification is mandatory; no bypass exists"),
            ("prospecting_app", "Api.test_webhook",
             "the Send Test button"),
            ("prospecting_app", "Api.webhook_payload_preview",
             "the payload preview shown before you enable"),
        ],
        "local_document": "PERMISSIONS.md",
    },
    {
        "id": "coach_ai",
        "title": "Coach cloud AI (optional)",
        "short_description":
            "Optional: the Coach chat can use YOUR OWN AI-provider key. "
            "Offline by default.",
        "detailed_explanation":
            "The built-in Coach answers setup questions offline, for free. "
            "If you choose, you can point it at a cloud model (Anthropic, "
            "OpenAI, or a custom endpoint) using your own API key. Only "
            "then does a chat message leave this computer -- and it goes to "
            "the provider you chose, never to the developer. Your key is "
            "stored locally and is never shown back to the interface or "
            "included in exports.",
        "platforms": ["mac", "win"],
        "required_level": OPTIONAL,
        "features_enabled": ["Cloud-model Coach replies (opt-in)"],
        "data_accessed":
            "When you send a message in API mode: your message, current "
            "macro settings values and summary stats (so the Coach can "
            "advise), and your API key for authentication.",
        "data_retained":
            "Chat history and your key stay local (the key in a separate "
            "restricted file). Delete them any time from the Coach panel "
            "or Trust Center.",
        "network_behaviour":
            "HTTPS to the provider you selected, only when you press Send "
            "in API mode. Verified TLS only.",
        "privacy_notes":
            "Offline mode makes zero requests. The provider sees what any "
            "API customer sends it; read your provider's data policy.",
        "permission_category": {"mac": None, "win": None},
        "operating_system_label": {
            "mac": "No macOS permission involved (outbound network only).",
            "win": "No permission involved."},
        "request_strategy": {"mac": "user_configuration",
                             "win": "user_configuration"},
        "detection_strategy": {
            "mac": "coach mode + key-present flags in local settings",
            "win": "coach mode + key-present flags in local settings"},
        "test_strategy":
            "Send a Coach message in API mode; errors are shown verbatim.",
        "revoke_instructions": {
            "mac": "Coach settings > switch to Offline, and Clear key.",
            "win": "Same."},
        "declined_behaviour":
            "Coach stays in offline mode. Everything works.",
        "source_references": [
            ("prospecting_app", "Api._coach_api",
             "the only Coach egress: user-triggered, user's own key"),
            ("prospecting_app", "_save_coach_key",
             "key storage: local secrets file only, never the config"),
            ("prospecting_app", "Api.coach_settings",
             "the UI never receives the key back (has_key flag only)"),
        ],
        "local_document": "PERMISSIONS.md",
    },
    {
        "id": "sound_alerts",
        "title": "Sound alerts",
        "short_description":
            "Plays a short system sound on certain events. No permission "
            "needed.",
        "detailed_explanation":
            "Alerts use the system beep (macOS: afplay of a built-in system "
            "sound; Windows: MessageBeep). This is audio OUTPUT only -- it "
            "is unrelated to any recording permission and needs none.",
        "platforms": ["mac", "win"],
        "required_level": INFORMATIONAL_ONLY,
        "features_enabled": ["Audible alert on stop/error events"],
        "data_accessed": "None.",
        "data_retained": "None.",
        "network_behaviour": "None.",
        "privacy_notes": "Output only; no microphone code exists in the app.",
        "permission_category": {"mac": None, "win": None},
        "operating_system_label": {"mac": "No permission involved.",
                                   "win": "No permission involved."},
        "request_strategy": {"mac": "none", "win": "none"},
        "detection_strategy": {"mac": "always available",
                               "win": "always available"},
        "test_strategy": "Alerts sound during normal use; no test needed.",
        "revoke_instructions": {"mac": "Turn alerts off in settings.",
                                "win": "Turn alerts off in settings."},
        "declined_behaviour": "Silence. Nothing else changes.",
        "source_references": [
            ("prospector_engine.engine", "_beep",
             "the complete sound implementation"),
        ],
        "local_document": "PERMISSIONS.md",
    },
    # ---- explicit NOT_REQUIRED entries -----------------------------------
    {
        "id": "microphone",
        "title": "Microphone",
        "short_description": "Not requested. The app has no audio-capture "
                             "code.",
        "detailed_explanation":
            "Prospector Lite never asks for the microphone. Discord "
            "notifications are a network feature and need no audio access. "
            "If macOS ever shows a microphone prompt naming this app, "
            "treat it as a red flag for a tampered download and verify "
            "your copy's checksum.",
        "platforms": ["mac", "win"],
        "required_level": NOT_REQUIRED,
        "features_enabled": [],
        "data_accessed": "None -- no code path can access audio input.",
        "data_retained": "None.",
        "network_behaviour": "None.",
        "privacy_notes":
            "The macOS Screen Recording category is labelled 'Screen & "
            "System Audio Recording' on recent versions; that label belongs "
            "to the OS category, not to what this app captures (pixels "
            "only).",
        "permission_category": {"mac": None, "win": None},
        "operating_system_label": {"mac": "Never requested.",
                                   "win": "Never requested."},
        "request_strategy": {"mac": "never", "win": "never"},
        "detection_strategy": {"mac": "static fact", "win": "static fact"},
        "test_strategy": "Verified by tests: no audio-capture API appears "
                         "anywhere in the source.",
        "revoke_instructions": {"mac": "Nothing to revoke.",
                                "win": "Nothing to revoke."},
        "declined_behaviour": "Not applicable.",
        "source_references": [],
        "local_document": "PERMISSIONS.md",
    },
    {
        "id": "camera",
        "title": "Camera",
        "short_description": "Not requested, ever.",
        "detailed_explanation": "No code path touches the camera.",
        "platforms": ["mac", "win"],
        "required_level": NOT_REQUIRED,
        "features_enabled": [], "data_accessed": "None.",
        "data_retained": "None.", "network_behaviour": "None.",
        "privacy_notes": "",
        "permission_category": {"mac": None, "win": None},
        "operating_system_label": {"mac": "Never requested.",
                                   "win": "Never requested."},
        "request_strategy": {"mac": "never", "win": "never"},
        "detection_strategy": {"mac": "static fact", "win": "static fact"},
        "test_strategy": "Source-scan tests.",
        "revoke_instructions": {"mac": "Nothing to revoke.",
                                "win": "Nothing to revoke."},
        "declined_behaviour": "Not applicable.",
        "source_references": [], "local_document": "PERMISSIONS.md",
    },
    {
        "id": "location",
        "title": "Location",
        "short_description": "Not requested, ever.",
        "detailed_explanation":
            "No location API, no IP-based geolocation, no lookup services. "
            "(Older private builds phoned a location service; that code was "
            "removed before the public release and tests keep it out.)",
        "platforms": ["mac", "win"],
        "required_level": NOT_REQUIRED,
        "features_enabled": [], "data_accessed": "None.",
        "data_retained": "None.", "network_behaviour": "None.",
        "privacy_notes": "",
        "permission_category": {"mac": None, "win": None},
        "operating_system_label": {"mac": "Never requested.",
                                   "win": "Never requested."},
        "request_strategy": {"mac": "never", "win": "never"},
        "detection_strategy": {"mac": "static fact", "win": "static fact"},
        "test_strategy": "Source-scan tests (public_release_tests.py bans "
                         "geolocation endpoints).",
        "revoke_instructions": {"mac": "Nothing to revoke.",
                                "win": "Nothing to revoke."},
        "declined_behaviour": "Not applicable.",
        "source_references": [], "local_document": "PERMISSIONS.md",
    },
    {
        "id": "admin_privileges",
        "title": "Administrator / root",
        "short_description": "Not required and not recommended.",
        "detailed_explanation":
            "Prospector Lite installs per-user and runs as a normal user on "
            "both platforms. Do not run it elevated. On Windows, if Roblox "
            "itself runs as administrator, run BOTH programs normally "
            "instead -- elevating the macro is never the fix.",
        "platforms": ["mac", "win"],
        "required_level": NOT_REQUIRED,
        "features_enabled": [], "data_accessed": "None.",
        "data_retained": "None.", "network_behaviour": "None.",
        "privacy_notes": "",
        "permission_category": {"mac": None, "win": None},
        "operating_system_label": {
            "mac": "Never requested (no privileged helper, no sudo).",
            "win": "Installer uses per-user mode (PrivilegesRequired="
                   "lowest); no UAC prompt in normal use."},
        "request_strategy": {"mac": "never", "win": "never"},
        "detection_strategy": {"mac": "static fact",
                               "win": "elevation check (informational)"},
        "test_strategy": "The Windows tab shows whether the app is "
                         "currently elevated (it should not be).",
        "revoke_instructions": {"mac": "Nothing to revoke.",
                                "win": "Nothing to revoke."},
        "declined_behaviour": "Not applicable.",
        "source_references": [], "local_document": "PERMISSIONS.md",
    },
    {
        "id": "full_disk_access",
        "title": "Full Disk Access",
        "short_description": "Not requested, ever.",
        "detailed_explanation":
            "The app reads and writes only its own data folder plus files "
            "you explicitly pick in open/save dialogs.",
        "platforms": ["mac"],
        "required_level": NOT_REQUIRED,
        "features_enabled": [], "data_accessed": "None.",
        "data_retained": "None.", "network_behaviour": "None.",
        "privacy_notes": "",
        "permission_category": {"mac": None, "win": None},
        "operating_system_label": {"mac": "Never requested.", "win": None},
        "request_strategy": {"mac": "never", "win": "never"},
        "detection_strategy": {"mac": "static fact", "win": "static fact"},
        "test_strategy": "Source-scan tests.",
        "revoke_instructions": {"mac": "Nothing to revoke.", "win": ""},
        "declined_behaviour": "Not applicable.",
        "source_references": [], "local_document": "PERMISSIONS.md",
    },
]

CAP_BY_ID = {c["id"]: c for c in CAPABILITIES}

# macOS System Settings deep links (verified panes; the UI always offers the
# manual path too, so a link that stops working on a future macOS degrades
# gracefully).
_MAC_SETTINGS_LINKS = {
    "screen_detection":
        "x-apple.systempreferences:com.apple.preference.security"
        "?Privacy_ScreenCapture",
    "input_control":
        "x-apple.systempreferences:com.apple.preference.security"
        "?Privacy_Accessibility",
    "stop_hotkeys":
        "x-apple.systempreferences:com.apple.preference.security"
        "?Privacy_ListenEvent",
}


# ---------------------------------------------------------------------------
# Live status detection (never prompts)
# ---------------------------------------------------------------------------

def _ax_module():
    """The pyobjc module carrying the AXIsProcessTrusted* symbols.
    ApplicationServices re-exports them from HIServices; packaged builds
    have both, but importing either works, so try both -- a bundling
    change can not silently break the Accessibility preflight."""
    try:
        import ApplicationServices as m
        return m
    except Exception:
        import HIServices as m
        return m


def _mac_preflights():
    """The three real macOS TCC states for THIS process. Read-only: none of
    these calls can trigger a system prompt."""
    out = {}
    try:
        import Quartz
        out["screen_detection"] = bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:
        out["screen_detection"] = None
    try:
        out["input_control"] = bool(_ax_module().AXIsProcessTrusted())
    except Exception:
        out["input_control"] = None
    try:
        import Quartz
        out["stop_hotkeys"] = bool(Quartz.CGPreflightListenEventAccess())
    except Exception:
        out["stop_hotkeys"] = None
    return out


_LAUNCH_PREFLIGHTS = None


def launch_preflights():
    """The preflight states as they were the FIRST time this process asked
    (cached). macOS applies Screen Recording / Input Monitoring grants to a
    process at launch: comparing the live preflight against this snapshot is
    how the app honestly detects 'granted now, but a restart is needed for
    it to take effect'."""
    global _LAUNCH_PREFLIGHTS
    if _LAUNCH_PREFLIGHTS is None:
        _LAUNCH_PREFLIGHTS = (_mac_preflights()
                              if platform_key() == "mac" else {})
    return _LAUNCH_PREFLIGHTS


def _win_elevated():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return None


def capability_statuses(settings=None):
    """{cap_id: {"status": ..., "detail": ...}} using real checks only.

    Statuses: granted / not_granted (mac TCC), untested (win),
    configured / disabled (network opt-ins), not_requested (NOT_REQUIRED),
    info (informational), unknown (check unavailable).
    `settings` is the loaded config dict for the opt-in rows (optional).
    """
    plat = platform_key()
    s = settings or {}
    out = {}
    pre = _mac_preflights() if plat == "mac" else {}
    for cap in CAPABILITIES:
        cid = cap["id"]
        if cap["required_level"] == NOT_REQUIRED:
            if cid == "admin_privileges" and plat == "win":
                elev = _win_elevated()
                if elev is True:
                    _d = ("Currently running elevated -- not "
                          "recommended; run it normally.")
                elif elev is False:
                    _d = "Running as a normal user (correct)."
                else:
                    _d = ("Could not read the elevation state (the check "
                          "API was unavailable).")
                out[cid] = {"status": "not_requested", "detail": _d}
            else:
                out[cid] = {"status": "not_requested",
                            "detail": "This app never asks for it."}
            continue
        if cap["required_level"] == INFORMATIONAL_ONLY:
            out[cid] = {"status": "info", "detail": "No permission involved."}
            continue
        if cid == "discord_notifications":
            url = str(s.get("WEBHOOK_URL") or "").strip()
            enabled = bool(s.get("WEBHOOK_ENABLED"))
            if enabled and url:
                st, d = "configured", "Enabled, posting to your webhook URL."
            elif url:
                st, d = "configured", ("A URL is saved but notifications are "
                                       "switched off.")
            else:
                st, d = "disabled", "Off (the default). No URL is set."
            out[cid] = {"status": st, "detail": d}
            continue
        if cid == "coach_ai":
            mode = str(s.get("COACH_MODE") or "offline")
            if mode == "api":
                out[cid] = {"status": "configured",
                            "detail": "API mode with your own key."}
            else:
                out[cid] = {"status": "disabled",
                            "detail": "Offline mode (the default). No "
                                      "requests are made."}
            continue
        # OS-permission capabilities
        if plat == "mac":
            g = pre.get(cid)
            if g is True:
                out[cid] = {"status": "granted",
                            "detail": "macOS reports access is granted to "
                                      "this app."}
            elif g is False:
                out[cid] = {"status": "not_granted",
                            "detail": "macOS has not granted this yet "
                                      "(never asked, or declined)."}
            else:
                out[cid] = {"status": "unknown",
                            "detail": "The system check API was "
                                      "unavailable."}
        elif plat == "win":
            # Windows has no permission model to read, so nothing is
            # asserted: the row stays "untested" until the user's own Test
            # proves the capability works.
            out[cid] = {"status": "untested",
                        "detail": "Windows shows no permission prompt for "
                                  "this; run Test to prove it works."}
        else:
            out[cid] = {"status": "unknown",
                        "detail": "Unsupported platform."}
    return out


def request_permission(cap_id):
    """Trigger the real OS permission request for `cap_id` (macOS only).
    MUST be called only from an explicit user action -- this is the call
    that makes the system prompt appear / registers the app in the pane."""
    if platform_key() != "mac":
        return {"ok": False, "error": "No OS request exists on this "
                                      "platform."}
    try:
        if cap_id == "screen_detection":
            import Quartz
            granted = bool(Quartz.CGRequestScreenCaptureAccess())
            return {"ok": True, "granted": granted,
                    "note": ("" if granted else
                             "macOS listed the app under Screen Recording; "
                             "flip the switch there, then use Test. A "
                             "restart of the app may be needed.")}
        if cap_id == "input_control":
            ax = _ax_module()
            granted = bool(ax.AXIsProcessTrustedWithOptions(
                {ax.kAXTrustedCheckOptionPrompt: True}))
            return {"ok": True, "granted": granted}
        if cap_id == "stop_hotkeys":
            import Quartz
            granted = bool(Quartz.CGRequestListenEventAccess())
            return {"ok": True, "granted": granted,
                    "note": ("" if granted else
                             "macOS listed the app under Input Monitoring; "
                             "flip the switch there, then use Test.")}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": False, "error": "No OS request for this capability."}


def open_settings(cap_id):
    """Open the exact macOS System Settings pane (with a safe fallback)."""
    if platform_key() != "mac":
        return {"ok": False, "error": "Only macOS has a settings pane for "
                                      "this."}
    url = _MAC_SETTINGS_LINKS.get(cap_id)
    try:
        if url:
            subprocess.run(["open", url], check=False, timeout=10)
        else:
            subprocess.run(["open", "-b", "com.apple.systempreferences"],
                           check=False, timeout=10)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Capability tests (each one runs the REAL capability, bounded and safe)
# ---------------------------------------------------------------------------

def test_screen_capture(with_preview=True):
    """Grab a small centre patch of the primary display, report size and
    non-blankness, optionally return a one-shot preview (shown in-app,
    never written to disk), then discard the frame.

    On macOS, calling this before Screen Recording is granted either fails
    or yields a black frame -- both are reported honestly (and the call
    itself makes macOS register the app in the Screen Recording pane)."""
    try:
        import mss
        import mss.tools
        with mss.mss() as sct:
            mon = sct.monitors[1]
            w, h = int(mon["width"]), int(mon["height"])
            bw, bh = min(240, w), min(150, h)
            box = {"left": int(mon["left"] + (w - bw) // 2),
                   "top": int(mon["top"] + (h - bh) // 2),
                   "width": bw, "height": bh}
            img = sct.grab(box)
            raw = bytes(img.rgb)
            sample = raw[::97] or b"\x00"
            spread = max(sample) - min(sample)
            nonblank = spread > 8
            out = {"ok": True, "width": img.width, "height": img.height,
                   "nonblank": bool(nonblank),
                   "note": ("Capture works." if nonblank else
                            "The frame came back blank/black. On macOS "
                            "that usually means Screen Recording is not "
                            "granted yet, or the app needs a restart "
                            "after granting.")}
            if with_preview and nonblank:
                png = mss.tools.to_png(img.rgb, (img.width, img.height))
                out["preview"] = ("data:image/png;base64,"
                                  + base64.b64encode(png).decode("ascii"))
            return out
    except Exception as e:
        return {"ok": False, "error": str(e),
                "note": "Capture failed. On macOS, grant Screen Recording "
                        "and restart the app; on Windows, check "
                        "remote-desktop/protected-content setups."}


_TEST_KEY_MAC = 17          # 't' on ANSI layouts; harmless in our own field
_TEST_KEY_WIN_SCAN = 0x14   # 't' scancode


def _app_is_frontmost():
    """True/False when it can be determined whether THIS app currently has
    focus; None when it cannot. Used to enforce the input test's promise:
    the test keystroke is posted only while our own window is focused, so
    it can never land in Roblox or any other application."""
    try:
        if platform_key() == "mac":
            from AppKit import NSApplication
            return bool(NSApplication.sharedApplication().isActive())
        if platform_key() == "win":
            import ctypes
            u32 = ctypes.windll.user32
            hwnd = u32.GetForegroundWindow()
            if not hwnd:
                return None
            # Resolve to the top-level root first: with WebView2, keyboard
            # focus can sit in a child HWND owned by the out-of-process
            # msedgewebview2.exe even though the top-level window is ours.
            GA_ROOT = 2
            root = u32.GetAncestor(hwnd, GA_ROOT) or hwnd
            pid = ctypes.c_ulong()
            u32.GetWindowThreadProcessId(root, ctypes.byref(pid))
            return pid.value == os.getpid()
    except Exception:
        return None
    return None


def _win_send_scancode(scan, up):
    """One key event via SendInput with a KEYBDINPUT scancode entry -- the
    same API family the engine's platform_win input path uses (keybd_event
    does not document the SCANCODE flag and can drop the event).

    The union MUST include MOUSEINPUT: it is the largest INPUT member, so
    without it sizeof(INPUT) comes out 32 instead of 40 on Win64 and
    SendInput rejects every call (cbSize mismatch)."""
    import ctypes
    import ctypes.wintypes as wt
    KEYEVENTF_SCANCODE, KEYEVENTF_KEYUP = 0x0008, 0x0002
    ULONG_PTR = ctypes.c_size_t

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = (("dx", wt.LONG), ("dy", wt.LONG),
                    ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                    ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR))

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = (("wVk", wt.WORD), ("wScan", wt.WORD),
                    ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                    ("dwExtraInfo", ULONG_PTR))

    class _INPUTunion(ctypes.Union):
        _fields_ = (("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT))

    class _INPUT(ctypes.Structure):
        _fields_ = (("type", wt.DWORD), ("union", _INPUTunion))

    INPUT_KEYBOARD = 1
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    inp = _INPUT(INPUT_KEYBOARD, _INPUTunion(
        ki=_KEYBDINPUT(0, scan, flags, 0, 0)))
    sent = ctypes.windll.user32.SendInput(1, ctypes.byref(inp),
                                          ctypes.sizeof(_INPUT))
    return int(sent) == 1


def post_test_key(delay=0.35):
    """Post ONE harmless key press+release to the system after `delay`
    seconds (giving the in-app sandbox field time to take focus). The
    wizard's sandbox page observes both the key-down and key-up, proving
    (a) synthetic input works and (b) releases are clean. The post is
    REFUSED unless this app is the frontmost application at post time, so
    switching to another app mid-test types nothing anywhere. Never call
    this outside the explicit in-app test.

    The result dict is the honest record of what happened -- the caller
    (Api.trust_test_key) delivers it back to the UI so a refusal or an
    exception is reported as itself, never mis-blamed on a permission."""
    time.sleep(max(0.0, float(delay)))
    if _app_is_frontmost() is not True:
        return {"ok": False, "posted": False, "skipped": True,
                "error_code": "NOT_FRONTMOST",
                "error": "Prospector Lite was not the focused app -- "
                         "nothing was typed anywhere. Keep this window "
                         "focused during the test and try again."}
    plat = platform_key()
    try:
        if plat == "mac":
            import Quartz
            for down in (True, False):
                ev = Quartz.CGEventCreateKeyboardEvent(
                    None, _TEST_KEY_MAC, down)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                time.sleep(0.04)
            return {"ok": True, "posted": True}
        if plat == "win":
            for up in (False, True):
                if not _win_send_scancode(_TEST_KEY_WIN_SCAN, up):
                    return {"ok": False, "posted": False,
                            "error_code": "SENDINPUT_BLOCKED",
                            "error": "Windows blocked the synthetic key "
                                     "(SendInput sent 0 events)."}
                time.sleep(0.04)
            return {"ok": True, "posted": True}
        return {"ok": False, "posted": False, "error_code": "PLATFORM",
                "error": "Unsupported platform."}
    except Exception as e:
        return {"ok": False, "posted": False, "error_code": "EXCEPTION",
                "error": str(e)}


def test_pointer_wiggle():
    """Move the pointer 2 px and back, verifying the move actually happened
    by reading the cursor position -- real proof of mouse control with no
    click and no effect on any application."""
    plat = platform_key()
    try:
        if plat == "mac":
            import Quartz
            loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
            moved = False
            for dx in (2, -2):
                ev = Quartz.CGEventCreateMouseEvent(
                    None, Quartz.kCGEventMouseMoved,
                    (loc.x + dx, loc.y), Quartz.kCGMouseButtonLeft)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                time.sleep(0.05)
                now = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
                if dx == 2 and abs(now.x - loc.x) >= 1:
                    moved = True
            return {"ok": True, "moved": moved,
                    "note": ("" if moved else
                             "The pointer did not move -- macOS is "
                             "blocking synthetic input (grant "
                             "Accessibility).")}
        if plat == "win":
            import ctypes

            class _PT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
            pt = _PT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            ctypes.windll.user32.SetCursorPos(pt.x + 2, pt.y)
            time.sleep(0.05)
            now = _PT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(now))
            moved = abs(now.x - pt.x) >= 1
            ctypes.windll.user32.SetCursorPos(pt.x, pt.y)
            return {"ok": True, "moved": bool(moved)}
        return {"ok": False, "error": "Unsupported platform."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def test_input_control():
    """The composite in-app control check used by Readiness: pointer wiggle
    (verifiable Python-side). The keyboard half runs through the sandbox
    field in the UI via post_test_key()."""
    return test_pointer_wiggle()


def await_stop_hotkey(timeout=8.0):
    """Arm a one-shot listener for the Safe Stop keys (Esc, or Ctrl+K) and
    wait up to `timeout` seconds for the user to press one. Blocking --
    the app calls this on a worker thread. Observes ONLY those chords;
    nothing is logged."""
    plat = platform_key()
    deadline = time.time() + max(1.0, float(timeout))
    try:
        if plat == "mac":
            from pynput import keyboard
            hit = {"key": None}
            ctrl = {"down": False}

            def on_press(k):
                if k in (keyboard.Key.ctrl, keyboard.Key.ctrl_l,
                         keyboard.Key.ctrl_r):
                    ctrl["down"] = True
                    return
                if k == keyboard.Key.esc:
                    hit["key"] = "Esc"
                    return False
                ch = getattr(k, "char", None)
                # Ctrl+letter arrives as a control character on macOS
                vk = getattr(k, "vk", None)
                if ctrl["down"] and (ch in ("k", "\x0b") or vk == 40):
                    hit["key"] = "Ctrl+K"
                    return False

            def on_release(k):
                if k in (keyboard.Key.ctrl, keyboard.Key.ctrl_l,
                         keyboard.Key.ctrl_r):
                    ctrl["down"] = False

            lis = keyboard.Listener(on_press=on_press, on_release=on_release)
            try:
                lis.start()
            except Exception as e:
                return {"ok": False, "heard": None,
                        "error_code": "LISTENER_START",
                        "error": "The key listener could not start (%s). "
                                 "On macOS this usually means Input "
                                 "Monitoring is not granted to this app."
                                 % e}
            # a listener that dies immediately (macOS refusing the event
            # tap) must be reported as a start failure, not as a silent
            # 8-second timeout
            time.sleep(0.25)
            if not lis.is_alive() and not hit["key"]:
                return {"ok": False, "heard": None,
                        "error_code": "LISTENER_DIED",
                        "error": "macOS refused the key listener -- grant "
                                 "Input Monitoring to Prospector Lite, "
                                 "then restart the app and retest."}
            while time.time() < deadline and lis.is_alive():
                time.sleep(0.05)
            try:
                lis.stop()
            except Exception:
                pass
            if hit["key"]:
                return {"ok": True, "heard": hit["key"]}
            return {"ok": True, "heard": None,
                    "note": "Nothing heard within the window. If you did "
                            "press it, macOS is blocking the listener -- "
                            "grant Input Monitoring to Prospector Lite "
                            "and restart the app."}
        if plat == "win":
            import ctypes
            VK_ESC, VK_CTRL, VK_K = 0x1B, 0x11, 0x4B
            u32 = ctypes.windll.user32
            while time.time() < deadline:
                if u32.GetAsyncKeyState(VK_ESC) & 0x8000:
                    return {"ok": True, "heard": "Esc"}
                if (u32.GetAsyncKeyState(VK_CTRL) & 0x8000
                        and u32.GetAsyncKeyState(VK_K) & 0x8000):
                    return {"ok": True, "heard": "Ctrl+K"}
                time.sleep(0.03)
            return {"ok": True, "heard": None,
                    "note": "Nothing heard within the window."}
        return {"ok": False, "error": "Unsupported platform."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Build identity
# ---------------------------------------------------------------------------

def _read_build_info():
    base = getattr(sys, "_MEIPASS", _HERE)
    for p in (os.path.join(base, "build_info.json"),
              os.path.join(_HERE, "build", "build_info.json")):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
        except (OSError, ValueError):
            continue
    return {}


def _git(*args):
    try:
        r = subprocess.run(["git"] + list(args), cwd=_HERE,
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def build_identity(version="", project_url=""):
    """Everything that identifies THIS build. Frozen builds read the
    stamped build_info.json; source runs ask git FIRST so a stale stamp
    left behind by an earlier packaging run can never misreport the
    checkout's real commit or hide local modifications. `version` and
    `project_url` are passed in by the app (single source of truth
    there)."""
    bi = _read_build_info()
    if FROZEN:
        commit = bi.get("commit", "")
        dirty = bool(bi.get("dirty"))
    else:
        live = _git("rev-parse", "HEAD")
        commit = live or bi.get("commit", "")
        dirty = (bool(_git("status", "--porcelain",
                           "--untracked-files=no")) if live
                 else bool(bi.get("dirty")))
    ident = {
        # frozen builds trust the stamp; source runs trust the app's
        # VERSION constant so a stale build_info.json from an earlier
        # packaging run can never misreport the checkout's version
        # (same rule as the commit fields above)
        "version": ((bi.get("version") or version) if FROZEN
                    else (version or bi.get("version") or "")),
        "commit": commit or "unknown",
        "commit_short": (commit or "unknown")[:12],
        "dirty": bool(dirty),
        "date": bi.get("date", "") if FROZEN else "",
        "platform": platform_key(),
        "os": "%s %s" % (_platform.system(), _platform.release()),
        "arch": _platform.machine(),
        "frozen": FROZEN,
        "package": (bi.get("package", "app-bundle") if FROZEN
                    else "source"),
        "project_url": (bi.get("project_url") or project_url or ""),
        "signed": bi.get("signed", False),
        "notarized": bi.get("notarized", False),
        "licence_status": "source-available; no open-source licence chosen "
                          "yet (see LICENSE_CHOICE_REQUIRED.md)",
    }
    if ident["dirty"] and not FROZEN:
        ident["development_build"] = True
    return ident


def source_url_for(ident, path, line_start=None, line_end=None):
    """Exact-commit URL for a source file, or '' when no public repository
    URL is configured (the UI then shows the local path + commit instead --
    never a silent fallback to a moving branch)."""
    base = (ident.get("project_url") or "").rstrip("/")
    commit = ident.get("commit") or ""
    if not base or not commit or commit == "unknown":
        return ""
    url = "%s/blob/%s/%s" % (base, commit, path.replace(os.sep, "/"))
    if line_start:
        url += "#L%d" % int(line_start)
        if line_end and int(line_end) > int(line_start):
            url += "-L%d" % int(line_end)
    return url


# ---------------------------------------------------------------------------
# Trust manifest (build-time generation; frozen builds read the bundled copy)
# ---------------------------------------------------------------------------

def _resolve_ref(module_name, symbol):
    """(relative_file, line_start, line_end) for a module or dotted symbol.
    Resolved STATICALLY (ast over the source on disk): no imports, no side
    effects, and platform-specific modules (platform_win on a Mac) resolve
    the same everywhere. Requires a source checkout (build time / dev)."""
    import ast
    rel = module_name.replace(".", os.sep) + ".py"
    path = os.path.join(_HERE, rel)
    if not os.path.isfile(path):
        raise FileNotFoundError("no source for module %r (%s)"
                                % (module_name, rel))
    if not symbol:
        return rel, None, None
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    node = None
    body = tree.body
    for part in symbol.split("."):
        node = None
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)) and n.name == part:
                node = n
                break
        if node is None:
            break
        body = getattr(node, "body", [])
    if node is None:
        # nested definition (a def inside a def): accept a UNIQUE match by
        # final name; ambiguity fails the build rather than guessing.
        want = symbol.split(".")[-1]
        hits = [n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)) and n.name == want]
        if len(hits) == 1:
            node = hits[0]
        elif len(hits) > 1:
            raise AttributeError("symbol %r ambiguous in %s (%d matches)"
                                 % (symbol, rel, len(hits)))
        else:
            raise AttributeError("symbol %r not found in %s"
                                 % (symbol, rel))
    start = min([node.lineno]
                + [d.lineno for d in getattr(node, "decorator_list", [])])
    return rel, start, int(getattr(node, "end_lineno", node.lineno))


def generate_manifest(version="", project_url=""):
    """Resolve every capability's source references against the live source
    tree. Raises if any reference is dead (the build must fail rather than
    ship a wrong code link)."""
    ident = build_identity(version=version, project_url=project_url)
    caps = []
    for cap in CAPABILITIES:
        refs = []
        for module_name, symbol, why in cap["source_references"]:
            rel, a, b = _resolve_ref(module_name, symbol)
            refs.append({
                "file": rel, "symbol": symbol or "(module)",
                "line_start": a, "line_end": b, "why": why,
                "url": source_url_for(ident, rel, a, b),
            })
        caps.append({
            "id": cap["id"], "title": cap["title"],
            "required_level": cap["required_level"],
            "summary": cap["short_description"],
            "local_document": cap["local_document"],
            "references": refs,
        })
    return {
        "schema": 1,
        "generated_from": ident["commit"],
        "version": ident["version"],
        "project_url": ident["project_url"],
        "note": ("Line numbers are generated from the exact source this "
                 "build was made from. When project_url is set, every url "
                 "pins that commit -- never a branch."),
        "capabilities": caps,
    }


def load_manifest(version="", project_url=""):
    """The manifest for THIS build: bundled copy when frozen, generated
    live from source otherwise."""
    base = getattr(sys, "_MEIPASS", None)
    candidates = []
    if base:
        candidates.append(os.path.join(base, "trust_manifest.json"))
    candidates.append(os.path.join(_HERE, "build", "trust_manifest.json"))
    if FROZEN:
        for p in candidates:
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                continue
        return {"schema": 1, "capabilities": [], "generated_from": "unknown",
                "error": "trust manifest missing from this package"}
    try:
        return generate_manifest(version=version, project_url=project_url)
    except Exception as e:
        for p in candidates:
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                continue
        return {"schema": 1, "capabilities": [], "generated_from": "unknown",
                "error": "manifest generation failed: %s" % e}


def _main(argv):
    if "--emit" in argv:
        out = os.path.join(_HERE, "build", "trust_manifest.json")
        i = argv.index("--emit")
        if len(argv) > i + 1 and not argv[i + 1].startswith("-"):
            out = argv[i + 1]
        version = ""
        url = os.environ.get("PP_PROJECT_URL", "")
        try:
            import re
            with open(os.path.join(_HERE, "prospecting_app.py"),
                      encoding="utf-8") as f:
                m = re.search(r'VERSION\s*=\s*"([^"]+)"', f.read())
            version = m.group(1) if m else ""
            if not url:
                m2 = re.search(r'PROJECT_URL\s*=\s*"([^"]*)"',
                               open(os.path.join(_HERE, "prospecting_app.py"),
                                    encoding="utf-8").read())
                url = m2.group(1) if m2 else ""
        except OSError:
            pass
        man = generate_manifest(version=version, project_url=url)
        if os.path.dirname(out):
            os.makedirs(os.path.dirname(out), exist_ok=True)
        tmp = out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(man, f, indent=1)
        os.replace(tmp, out)
        print("trust manifest: %d capabilities, commit %s -> %s"
              % (len(man["capabilities"]), man["generated_from"][:12], out))
        return 0
    print(json.dumps(capability_statuses(), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
