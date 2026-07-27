# Prospector Lite — Build reproducibility

## What was measured (macOS, this machine)

`./build_dmg.command` was run multiple times at the same source commit and
the resulting `Prospector Lite.app` trees were compared file-by-file
(SHA-256 over all 1,167 files):

- **File lists: identical** across builds.
- **1,165 / 1,167 files bit-identical**, including the app executable, all
  first-party sources, the bundled default config and docs, and every
  third-party library binary.
- **2 files differ** between otherwise identical builds:
  1. `Contents/Resources/base_library.zip` — zip *entry timestamps* are
     already normalized (all 1980-01-01), but some `.pyc` payload bytes
     inside differ run-to-run (CPython bytecode serialization detail);
     `SOURCE_DATE_EPOCH` is exported by the build script and pins what it
     can.
  2. `Contents/_CodeSignature/CodeResources` — the (ad-hoc) signature
     hashes the zip above, so it follows.

The DMG container additionally embeds filesystem timestamps, so DMG-level
checksums differ per run by design; the meaningful comparison is the
bundle content listing above.

## What makes the build deterministic

- one version source of truth (`VERSION` in `prospecting_app.py`), read by
  the spec and the DMG name;
- `build_info.json` stamps the exact git commit into the bundle;
- `SOURCE_DATE_EPOCH` set to the commit time before PyInstaller runs;
- dependencies installed into a dedicated build venv (freeze recorded in
  the release candidate as `dependencies-macos-freeze.txt`);
- no network access during the build besides `pip install` of declared
  build dependencies; no untracked source inputs (the package-content scan
  fails on personal paths/secrets).

## Toolchain used for the 1.0.0-rc.1 candidate

- macOS (Apple Silicon, arm64), Python 3.13.1 (Homebrew)
- PyInstaller 6.21.0, pywebview 6.2.1, pyobjc 12.2.1, mss 10.2.0,
  numpy 2.5.1, pillow 12.3.0, pynput 1.8.2
- Windows builds: pinned runner recipe in
  `.github/workflows/build-windows.yml` (Python 3.12, PyInstaller latest,
  Inno Setup 6 via choco). Not yet executed — requires the repo push.

## How a reviewer verifies a release binary

1. Build from the tagged source per [BUILDING.md](../../BUILDING.md).
2. Compare the bundle listing:
   `cd "dist/Prospector Lite.app" && find . -type f | sort | xargs shasum -a 256 > mine.txt`
   against the `package-manifest-macos.txt` shipped in the release
   candidate. Expect everything identical except the two files above.
3. Check the DMG's SHA-256 against `SHA256SUMS.txt` for the *exact
   artifact* you downloaded.

## Known gaps / future work

- Make `base_library.zip` byte-stable (upstream PyInstaller/CPython
  behaviour) so the whole bundle is bit-for-bit reproducible.
- Pin build-dependency versions with hashes (pip-compile) instead of the
  recorded freeze.
