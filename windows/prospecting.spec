# PyInstaller spec for Prospectors Plus (Windows, one-folder build).
# Build:  pyinstaller prospecting.spec     (run on Windows)
# Output: dist\Prospectors Plus\Prospectors Plus.exe
#
# Notes:
# - pywebview on Windows renders via the .NET WebView2 runtime through pythonnet,
#   so we collect 'webview', 'clr_loader' and 'pythonnet' in full.
# - prospecting_old.py is launched at runtime via runpy (not a static import),
#   so it's added as a data file AND its libraries (mss, numpy) are pulled in as
#   hidden imports so they end up in the bundle.

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for pkg in ("webview", "clr_loader", "pythonnet", "mss", "numpy"):
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
    ("prospecting_config.json", "."),
    ("icon.png", "."),
]
hiddenimports += ["clr", "prospecting_ui", "mss.windows"]

block_cipher = None

a = Analysis(
    ["prospecting_app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Prospectors Plus",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,            # GUI app, no console window
    icon="icon.ico",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Prospectors Plus",
)
