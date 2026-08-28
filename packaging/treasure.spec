# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Treasure Navigator.

Two things this file exists to get right:

1. **Package data.** Arrow profiles, the evaluation spec, and the arrival
   assets are read with ``importlib.resources`` from inside
   ``prospector_engine``, so they must be collected as package data rather
   than assumed to sit next to the executable (plan 11.4).
2. **Deadman dispatch.** The frozen build launches the release-only helper as
   ``sys.executable --deadman``, so ``deadman.py`` must be a real module in
   the bundle, not merely a script beside it (plan 4.5).

Build through ``packaging/build_macos.sh`` or ``packaging/build_windows.ps1``;
those create an isolated environment, install the matching hashed lock, and run
the verification steps. Invoking PyInstaller by hand skips all of that.

NATIVE STATUS: this spec has not been executed. No bundle has been produced on
either OS during implementation. See STATUS.md, phase 6.
"""

import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent

datas = [
    (str(ROOT / "prospector_engine" / "profiles"), "prospector_engine/profiles"),
    (str(ROOT / "prospector_engine" / "assets"), "prospector_engine/assets"),
]

hiddenimports = [
    "deadman",
    "prospector_engine.platform_win" if sys.platform == "win32" else "prospector_engine.platform_mac",
]

# Trim what the app genuinely does not use. Everything removed here is checked
# by packaging/verify_bundle.py, which fails loudly rather than shipping a
# bundle that imports at build time and dies at run time.
excludes = [
    "matplotlib",
    "scipy",
    "pandas",
    "pytest",
    "hypothesis",
    "mypy",
    "IPython",
    "setuptools",
]

a = Analysis(
    [str(ROOT / "treasure.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TreasureNavigator",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    # Windows: Per-Monitor V2 is declared in the manifest as well as at
    # runtime, because the runtime call happens after Tk has already sized
    # itself (plan 14.3).
    manifest=str(ROOT / "packaging" / "treasure.manifest") if sys.platform == "win32" else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="TreasureNavigator",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Treasure Navigator.app",
        bundle_identifier="plus.prospectors.treasure",
        info_plist={
            "CFBundleShortVersionString": "0.5.0",
            "NSHighResolutionCapable": True,
            # macOS shows these strings in the permission prompts. Both are
            # required: Accessibility to pin and drive the window, Screen
            # Recording to capture the client area at all.
            "NSAppleEventsUsageDescription": (
                "Treasure Navigator uses Accessibility to position the Roblox window "
                "and to send input only while you have physically armed Live mode."
            ),
        },
    )
