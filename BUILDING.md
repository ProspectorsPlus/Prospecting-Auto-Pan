# Building Prospector Lite

Three ways to run it: from source, as a packaged macOS app, or as a packaged Windows app.

## Prerequisites

- **Python 3.11 or newer** (3.13 is what development is tested on; the Windows CI build uses 3.11).
- Runtime dependencies (see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for versions/licenses):
  - All platforms: `pywebview`, `mss`, `numpy`, `pillow`
  - macOS: `pyobjc` (Quartz/Cocoa bindings)
  - Windows: `pythonnet` + `clr-loader` (WebView2 backend for pywebview)
- Build-time tools (packaging only): PyInstaller; Inno Setup 6 (Windows installer).

The app itself needs no network; `pip install` is the only step that does. For a fully offline build, pre-download wheels with `pip download` on a connected machine and install with `pip install --no-index --find-links <dir>`.

## Run from source

```sh
# macOS
pip3 install pywebview pyobjc mss numpy pillow
python3 prospecting_app.py

# Windows (use the copies under windows\)
pip install pywebview pythonnet mss numpy pillow
python windows\prospecting_app.py
```

Running from source keeps all data files next to the scripts (packaged builds use the per-user data directory instead — see [README.md](README.md)).

First launch on macOS: grant Screen Recording and Accessibility to your terminal (or the app), then fully restart it.

## Windows package

Everything lives under `windows/`:

- `windows/prospecting.spec` — PyInstaller **one-folder** build. It resolves the shared `prospector_engine/` package from the repo root (`pathex=[".."]`).
- `windows/installer.iss` — Inno Setup 6 script that wraps the PyInstaller output into an installer (per-user install offered; no admin required to run the app).
- A portable ZIP is produced from the same one-folder output.

Local build, on a Windows machine:

```bat
cd windows
pip install pyinstaller pywebview pythonnet mss numpy pillow
python -m PyInstaller --noconfirm prospecting.spec
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
```

CI build: `.github/workflows/build-windows.yml` runs the same steps on a real GitHub Actions Windows runner (Python 3.11, Chocolatey-installed Inno Setup) on version tags, so no local Windows PC is required. Note: parts of the packaging pipeline are being reworked for the public release (naming, artifact layout, checksums/SBOM steps) — see [RELEASING.md](RELEASING.md).

## macOS package

`build_dmg.command` (run it on a Mac; it uses only built-in tools like `hdiutil`) produces the distributable DMG. The script is being rewritten for the public release; its intent:

- Build a self-contained `Prospector Lite.app` with PyInstaller (bundling the app files and the `prospector_engine/` package).
- Ship a clean default config — never the developer's personal config, webhook, or secrets.
- Wrap the `.app` in a drag-to-install DMG under `dist/`.

The macOS build is currently **unsigned** (no Apple Developer certificate). First launch of a downloaded copy requires right-click → Open. Document/verify checksums as described in [README.md](README.md).

## Verifying a build

Before treating any build as good, run the test suites from the repo root:

```sh
python3 tour_check.py
python3 finds_sim.py
python3 studio_tests.py
python3 prospecting_selftest.py
python3 public_release_tests.py   # release gate (being added in parallel)
```

plus the `engine_*` suites listed in [CONTRIBUTING.md](CONTRIBUTING.md). Release packaging steps, checksums, and the SBOM are covered in [RELEASING.md](RELEASING.md).
