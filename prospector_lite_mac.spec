# PyInstaller spec for Prospector Lite (macOS, one-folder .app bundle).
# Build:  pyinstaller prospector_lite_mac.spec      (run on macOS)
# Output: dist/Prospector Lite.app  (self-contained; no system Python needed)
#
# Normally invoked through build_dmg.command, which also stamps
# build_info.json, generates the .icns and wraps the app into a DMG.

import os
import re
from PyInstaller.utils.hooks import collect_all

VERSION = re.search(r'VERSION\s*=\s*"([^"]+)"',
                    open("prospecting_app.py", encoding="utf-8").read()).group(1)

datas, binaries, hiddenimports = [], [], []
for pkg in ("webview", "mss", "numpy", "pynput", "certifi"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# files the app reads/serves at runtime (resolved via sys._MEIPASS)
datas += [
    ("prospecting_old.py", "."),
    ("prospecting_ui.py", "."),
    ("prospecting_assistant.py", "."),
    # the sanitized public default config (same one the Windows build ships)
    ("windows/prospecting_config.json", "."),
    ("prospecting_prices.json", "."),
    ("icon.png", "."),
]
# public documentation shipped inside the bundle (welcome-screen + Trust
# Center links), the build identity stamp and the trust manifest written by
# build_dmg.command just before this runs
for extra in ("PRIVACY.md", "SECURITY.md", "README.md",
              "THIRD_PARTY_NOTICES.md", "PERMISSIONS.md", "TRUST_CENTER.md",
              "VERIFY_DOWNLOAD.md", "CALIBRATION_GUIDE.md",
              "LICENSE_CHOICE_REQUIRED.md",
              "build/build_info.json", "build/trust_manifest.json"):
    if os.path.exists(extra):
        datas += [(extra, ".")]
# guided-calibration example assets (manifest + any approved images)
if os.path.isdir("assets/onboarding/calibration"):
    datas += [("assets/onboarding/calibration",
               "assets/onboarding/calibration")]

hiddenimports += [
    "prospecting_ui", "prospecting_assistant", "mss.darwin",
    "lite_trust", "lite_onboarding",
    # pyobjc frameworks pywebview + the engine touch at runtime
    "objc", "AppKit", "Foundation", "WebKit", "Quartz", "CoreFoundation",
    # the shared engine package (prospecting_old.py shim imports it via runpy)
    "prospector_engine", "prospector_engine.client",
    "prospector_engine.protocol", "prospector_engine.sensing",
    "prospector_engine.engine", "prospector_engine.ipc",
    "prospector_engine.settings", "prospector_engine.platform_mac",
    "prospector_engine.recorder", "prospector_engine.cycleplan",
    "prospector_engine.vision", "prospector_engine.recovery",
    "prospector_engine.flows",
]

a = Analysis(
    ["prospecting_app.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Prospector Lite",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon="build/icon.icns" if os.path.exists("build/icon.icns") else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="Prospector Lite",
)
app = BUNDLE(
    coll,
    name="Prospector Lite.app",
    icon="build/icon.icns" if os.path.exists("build/icon.icns") else None,
    bundle_identifier="org.prospectorlite.app",
    info_plist={
        "CFBundleName": "Prospector Lite",
        "CFBundleDisplayName": "Prospector Lite",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        "LSApplicationCategoryType": "public.app-category.utilities",
        # Permission strings shown by macOS next to its own prompts. The app
        # requests ONLY Screen Recording (to see the game) and Accessibility /
        # Input Monitoring (to send and observe ordinary input). No camera,
        # microphone, location, contacts or network entitlements.
        "NSAppleEventsUsageDescription":
            "Prospector Lite never scripts other apps; this text exists only "
            "so macOS shows a clear name if an automation prompt ever "
            "appears.",
    },
)
