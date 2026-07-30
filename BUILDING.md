# Building Prospector Lite

Three ways to run it: from source, as a packaged macOS app, or as a packaged Windows app.

## Prerequisites

- **Python 3.11 or newer** (3.13 is what development is tested on; the Windows CI build uses 3.12).
- Runtime dependencies (see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for versions/licenses):
  - All platforms: `pywebview`, `mss`, `numpy`, `pillow`, `pynput` (Safe Stop/toggle hotkey listener + opt-in Studio recorder), `certifi` (TLS trust-store fallback so certificate verification never has to be disabled)
  - macOS: `pyobjc` (Quartz/Cocoa bindings)
  - Windows: `pythonnet` + `clr-loader` (WebView2 backend for pywebview)
- Build-time tools (packaging only): PyInstaller; Inno Setup 6 (Windows installer).

The app itself needs no network; `pip install` is the only step that does. For a fully offline build, pre-download wheels with `pip download` on a connected machine and install with `pip install --no-index --find-links <dir>`.

## Run from source

```sh
# macOS
pip3 install pywebview pyobjc mss numpy pillow pynput certifi
python3 prospecting_app.py

# Windows (use the copies under windows\)
pip install pywebview pythonnet mss numpy pillow pynput certifi
python windows\prospecting_app.py
```

Running from source keeps all data files next to the scripts (packaged builds use the per-user data directory instead — see [README.md](README.md)).

First launch on macOS: grant Screen Recording, Accessibility, and Input Monitoring to your terminal (or the app), then fully restart it. The in-app setup wizard walks through all three with live status.

## Windows package

Everything lives under `windows/`:

- `windows/prospecting.spec` — PyInstaller **one-folder** build. It resolves the shared `prospector_engine/` package from the repo root (`pathex=[".."]`).
- `windows/installer.iss` — Inno Setup 6 script that wraps the PyInstaller output into an installer (per-user install offered; no admin required to run the app).
- A portable ZIP is produced from the same one-folder output.

Local build, on a Windows machine:

```bat
cd windows
pip install pyinstaller pywebview pythonnet mss numpy pillow pynput certifi
python -m PyInstaller --noconfirm prospecting.spec
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
```

CI build: `.github/workflows/build-windows.yml` runs the same steps on a real GitHub Actions Windows runner (Python 3.12, Chocolatey-installed Inno Setup) on version tags, so no local Windows PC is required. The pipeline scans the packaged output for forbidden endpoint/brand strings and produces checksums and a CycloneDX SBOM — see [RELEASING.md](RELEASING.md). Honest caveat: the Windows runtime has not been executed by hand in this release pass; the CI workflows are prepared but unverified on real hardware.

## macOS package

`build_dmg.command` (run it on a Mac; it uses only built-in tools like `hdiutil` plus PyInstaller) produces the distributable DMG. What it does:

- Stamps **build identity**: writes `build/build_info.json` with `{commit, date, version, dirty, package, project_url, signed, notarized}`. A dirty working tree marks the build as a "development build" in-app. `PP_PROJECT_URL` (env) optionally stamps the public repository URL so in-app "View code" links can point at the exact commit.
- Emits the **trust manifest**: `python3 lite_trust.py --emit` writes `build/trust_manifest.json` — per-capability source references (file, symbol, exact line ranges) resolved from the AST of the exact source being packaged. A reference that no longer resolves fails the build.
- Builds a self-contained `Prospector Lite.app` with PyInstaller (bundling the app files, the local `lite_trust` / `lite_onboarding` / `lite_diagnostics` modules — the last is the rc.6 diagnostics/recommendation engine, pinned as a hidden import in both specs — and the `prospector_engine/` package).
- Ships a clean default config — never the developer's personal config, webhook, or secrets.
- Signs: **ad-hoc** by default; set `CODESIGN_ID` to a Developer ID to sign with the hardened runtime and the minimal `packaging/entitlements.plist`, followed by `codesign`/`spctl` verification. Notarization/stapling are manual owner steps ([RELEASING.md](RELEASING.md)).
- Wraps the `.app` in a drag-to-install DMG under `dist/` and emits its SHA-256.

The macOS build is currently **unsigned** (no Apple Developer certificate); the in-app build identity shows `signed: false` so nothing pretends otherwise. First launch of a downloaded copy requires right-click → Open. Document/verify checksums as described in [README.md](README.md).

## Verifying a build

Before treating any build as good, run the test suites from the repo root:

```sh
python3 tour_check.py
python3 finds_sim.py
python3 studio_tests.py
python3 prospecting_selftest.py
python3 public_release_tests.py      # the release gate
python3 onboarding_trust_tests.py    # the onboarding/trust suite
```

plus the `engine_*` suites listed in [CONTRIBUTING.md](CONTRIBUTING.md). Release packaging steps, checksums, and the SBOM are covered in [RELEASING.md](RELEASING.md).
