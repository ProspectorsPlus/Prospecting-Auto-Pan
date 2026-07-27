# Prospector Lite — Dependency inventory

Machine-readable companions are produced with every build rather than
tracked in the repository: CI generates a CycloneDX SBOM of the exact build
environment (`cyclonedx_py environment` → `sbom-macos.cdx.json` /
`sbom-windows.cdx.json`, attached to the build artifacts alongside
`SHA256SUMS`; see `.github/workflows/build-macos.yml` and
`build-windows.yml`), and the exact macOS closure is reproducible from the
dedicated build venv that `build_dmg.command` creates
(`build/venv/bin/pip freeze` after a build). License notices:
[THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md).

## Runtime dependencies (shipped inside the packages)

| Package | Version (rc.2 build) | License | Why | Platform |
|---|---|---|---|---|
| pywebview | 6.2.1 | BSD-3-Clause | the GUI shell (native webview windows) | both |
| mss | 10.2.0 | MIT | screen capture (the only way the app "sees") | both |
| numpy | 2.5.1 | BSD-3-Clause | pixel-array math for detection | both |
| pillow | 12.3.0 | MIT-CMU | icon generation, image handling | both |
| pynput | 1.8.2 | LGPL-3.0 | global hotkey listener (Esc / Ctrl+K) | both |
| certifi | installed at build time | MPL-2.0 | CA trust-store fallback so TLS certificate verification always has roots, even when a bundled Python ships an empty default store (verification is mandatory — there is no unverified mode) | both |
| pyobjc (core + frameworks) | 12.2.1 | MIT | Quartz events, window queries, WebKit bridge | macOS |
| pythonnet + clr-loader | installed at build time (unpinned — see gap below) | MIT | WebView2 bridge for pywebview | Windows |
| CPython runtime | 3.13.1 (macOS build) / 3.12 (CI) | PSF-2.0 | bundled by PyInstaller | both |

Direct vs transitive: everything above is direct except `clr-loader`
(dependency of pythonnet), `six` (pynput), and PyInstaller's helpers
(`altgraph`, `macholib`) plus `bottle`/`proxy_tools`/`typing_extensions`
(pywebview). The full closure is in the per-build SBOM and the build-venv
freeze described at the top.

## Build-time only (never shipped as importable code)

| Tool | Version | Purpose |
|---|---|---|
| PyInstaller (+ hooks-contrib) | 6.21.0 / 2026.6 | freezing the self-contained app |
| Inno Setup 6 | choco latest (CI) | Windows installer |
| cyclonedx-bom | latest | SBOM generation |

## Audit status (this session)

- `pip-audit` over the frozen macOS closure: **no known vulnerabilities**.
- `bandit` over first-party sources: 0 high (see `SECURITY_AUDIT.md`).
- No dependency is fetched at application runtime; everything ships in the
  package. The app performs no network requests by default
  (`NETWORK_BEHAVIOR.md`).

## Known gaps

- CI installs build deps unpinned (`pip install pyinstaller pywebview …`).
  The exact closure of any given build is captured in that build's SBOM and
  is reproducible from the build venv; converting both platforms to
  hash-pinned requirements (pip-compile) is recommended before 1.0.0 final.
- `pynput` is LGPL-3.0: it is bundled unmodified inside the PyInstaller
  package; the notices file states where to obtain its source. Keep this
  in mind when choosing the project license.
