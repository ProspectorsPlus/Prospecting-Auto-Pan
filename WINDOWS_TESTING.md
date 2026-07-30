# Windows testing

How to verify a Windows build of Prospector Lite, what is automated, what is
manual, and — honestly — what has actually been executed so far.

## Current status (honest)

**Prepared and static-validated, NEVER yet executed on real Windows.**

- Everything Windows-side has been verified only statically, on macOS:
  `py_compile` of the `windows/` copies, `tour_check.py` lockstep + full-file
  twin parity, YAML parsing of the workflows, and a review pass over
  `packaging/windows_acceptance.ps1` (PowerShell is not installed on the
  development Mac, so even a `pwsh` syntax check has not run).
- The frozen exe, the installer, `windows_acceptance.ps1`, and every step of
  `.github/workflows/build-windows.yml` have never run anywhere.
- **A green `build-windows.yml` run remains a release blocker** (see
  `PUBLIC_RELEASE_READINESS.md`). Do not publish a Windows artifact before
  one exists.

## Run the CI build

The whole pipeline (PyInstaller build, content scan, smoke test, acceptance
probes, installer, SBOM, checksums) runs on a GitHub Actions Windows runner:

```
gh workflow run build-windows.yml
```

Watch it with `gh run watch`, or list runs with
`gh run list --workflow=build-windows.yml`. Artifacts (installer, portable
ZIP, checksums, SBOM) land on the run page; `release.yml` attaches them to a
draft release on `v*` tags. Nothing publishes without a manual click.

## Run the acceptance script (real Windows machine)

From a source checkout on Windows, after building (`windows\build.bat`) or
installing:

```
pwsh -File packaging\windows_acceptance.ps1
```

- Arg 1 is the app folder; the default is the newest folder under
  `windows\dist\` (normally `windows\dist\Prospector Lite`). Point it at the
  installed folder to test an installer output instead.
- Run it from a **normal** PowerShell. The script refuses elevated shells
  (exit code 2) so results reflect a real per-user install.
- It ends with `WINDOWS ACCEPTANCE: ALL PASS` or a failure list; exit code 0
  means pass.

Automated probes: exe presence + version identity against `build_info.json`
and the source `VERSION`; an isolated `--capabilities` run (temp
`PP_DATA_DIR`, install folder must stay untouched); a bounded first-boot
probe (bridge liveness via `onboarding_state.json`, zero owned TCP
connections, clean kill); the `%APPDATA%\Prospector Lite` data-dir
convention in a sandboxed `APPDATA`; bundle-content checks (trust manifest,
permission docs present; personal files and banned brand/endpoint strings
absent); and a static DPI-awareness check. It fakes nothing: DPI scaling
behavior is a printed manual checklist, not a scripted result.

## Manual DPI checklist

`prospector_engine/platform_win.py` sets `SetProcessDpiAwareness(2)` at
import, so capture and cursor coordinates are both physical pixels. That
property is checked statically; the actual scaling behavior needs a human at
each scale factor:

1. Settings > System > Display > Scale: set **100%**. Restart the app.
2. Run Guided calibration, then "Test detection (live)" on the Calibrate
   tab: overlay aligned with the game HUD, detection reads the right pixels,
   clicks land where the overlay says.
3. Repeat at **125%** and **150%** (restart the app after each change).

## Safe Stop key test

With a run active and Roblox focused:

- Press **Esc** — the app must stop instantly and release every held key and
  mouse button (Safe Stop).
- Press **Ctrl+K** — start/stop toggle must work while the game, not the
  app, has keyboard focus (the hotkey listener is a `GetAsyncKeyState`
  poller, so no extra Windows permissions are involved).

## Beyond this file

Interactive journeys (setup wizard end-to-end, guided calibration against
the live game, an actual mining session) are human steps; see the acceptance
matrix under `docs/`. `windows/README.txt` covers user-facing hotkeys and
troubleshooting.
