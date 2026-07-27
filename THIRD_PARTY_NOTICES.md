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
| pynput | 1.8.2 | LGPL-3.0 | https://github.com/moses-palmer/pynput | Global Safe Stop/toggle hotkey listener (macOS) and the opt-in Studio input recorder |
| certifi | installed at build time — not pinned yet | MPL-2.0 | https://github.com/certifi/python-certifi | Fallback CA trust store so TLS certificate verification is always possible (verification is never disabled) |
| pyobjc (core + framework bindings, macOS only) | 12.2.1 | MIT | https://github.com/ronaldoussoren/pyobjc | Quartz/Cocoa APIs: input synthesis, window/display queries |
| pythonnet (Windows only) | installed at build time — not pinned yet | MIT | https://github.com/pythonnet/pythonnet | .NET bridge for pywebview's WebView2 backend |
| clr-loader (Windows only) | installed at build time — not pinned yet | MIT | https://github.com/pythonnet/clr-loader | CLR loading for pythonnet |

Notes:

- pyobjc is distributed as `pyobjc-core` plus per-framework wheels (`pyobjc-framework-Quartz`, `pyobjc-framework-Cocoa`, and others); all are MIT-licensed and versioned together (12.2.1 verified locally).
- pynput is LGPL-3.0. It is used as an ordinary, unmodified Python dependency (dynamically imported at runtime), which the LGPL permits regardless of the license this project eventually chooses; see [LICENSE_CHOICE_REQUIRED.md](LICENSE_CHOICE_REQUIRED.md).
- certifi is MPL-2.0 (file-level copyleft on certifi's own files only; it does not affect this project's license choice). Its version should be pinned as part of release hardening, like the Windows-only packages.
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
for p in ("pywebview", "mss", "numpy", "pillow", "pynput", "certifi",
          "pyobjc-core", "pythonnet", "clr-loader"):
    try:
        d = m.metadata(p)
        print(p, m.version(p), d.get("License-Expression") or d.get("License", "")[:40])
    except m.PackageNotFoundError:
        print(p, "(not installed on this platform)")
PY
```

If a dependency is added, removed, or re-licensed, this file must be updated in the same change.
