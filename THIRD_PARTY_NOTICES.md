# Third-party notices

Prospector Lite depends on the open-source packages below. Short notices with pointers are given here; each project's full license text ships with the package itself (in the installed distribution's metadata / license files).

Versions were read from the development machine's installed package metadata on 2026-07-27 with:

```sh
python3 -c "import importlib.metadata as m; print(m.version('<package>'))"
```

## Runtime dependencies

| Package | Version verified | License | Project home | Used for |
|---|---|---|---|---|
| pywebview | 6.2.1 | BSD-3-Clause | https://pywebview.flowrl.com/ | The desktop GUI shell (native webview window) |
| mss | 10.2.0 | MIT | https://github.com/BoboTiG/python-mss | Cross-platform screen capture |
| numpy | 2.5.0 | BSD-3-Clause | https://numpy.org/ | Pixel-array math for detection |
| Pillow | 12.3.0 | MIT-CMU | https://python-pillow.github.io/ | Image handling (icons, screenshots) |
| pyobjc (core + framework bindings, macOS only) | 12.2.1 | MIT | https://github.com/ronaldoussoren/pyobjc | Quartz/Cocoa APIs: input synthesis, window/display queries |
| pythonnet (Windows only) | installed at build time — not pinned yet | MIT | https://github.com/pythonnet/pythonnet | .NET bridge for pywebview's WebView2 backend |
| clr-loader (Windows only) | installed at build time — not pinned yet | MIT | https://github.com/pythonnet/clr-loader | CLR loading for pythonnet |

Notes:

- pyobjc is distributed as `pyobjc-core` plus per-framework wheels (`pyobjc-framework-Quartz`, `pyobjc-framework-Cocoa`, and others); all are MIT-licensed and versioned together (12.2.1 verified locally).
- The Windows-only packages are installed by the CI build (`.github/workflows/build-windows.yml`) and were not verifiable on the macOS development machine; their versions should be pinned as part of release hardening.
- Python itself (3.11+; 3.13 tested) is distributed under the PSF License, https://www.python.org/.

## Build-time tools (not shipped inside the app)

| Tool | License | Project home | Used for |
|---|---|---|---|
| PyInstaller | GPL-2.0-or-later with a bootloader exception permitting distribution of bundled apps under any license | https://pyinstaller.org/ | Freezing the app for macOS and Windows packages |
| Inno Setup 6 | Inno Setup License (free to use and distribute installers) | https://jrsoftware.org/isinfo.php | The Windows installer |

## Verifying this list

```sh
python3 - <<'PY'
import importlib.metadata as m
for p in ("pywebview", "mss", "numpy", "pillow", "pyobjc-core",
          "pythonnet", "clr-loader"):
    try:
        d = m.metadata(p)
        print(p, m.version(p), d.get("License-Expression") or d.get("License", "")[:40])
    except m.PackageNotFoundError:
        print(p, "(not installed on this platform)")
PY
```

If a dependency is added, removed, or re-licensed, this file must be updated in the same change.
