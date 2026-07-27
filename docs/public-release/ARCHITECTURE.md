# Prospector Lite — Architecture (public-release map)

Prospector Lite is a desktop macro for the Roblox game *Prospecting*. It is an
**external** tool: it reads pixels from the screen and sends ordinary OS-level
keyboard/mouse input. It never injects into Roblox, never attaches to or reads
the Roblox process, and never touches game files or network traffic.

## Process model

```
┌────────────────────────────┐   spawns (subprocess, stdio)   ┌──────────────────────────┐
│ prospecting_app.py         │ ─────────────────────────────► │ prospector_engine/       │
│ (pywebview GUI shell)      │   PPE1 IPC frames or legacy    │ engine.py (macro runtime)│
│ main window, HUD, pill,    │   line stream                  │ screen read → state      │
│ analytics, coach, studio   │ ◄───────────────────────────── │ machine → input events   │
└────────────────────────────┘   stats/events                 └──────────────────────────┘
```

- **`prospecting_app.py`** — the desktop app (pywebview + embedded HTML/JS
  surfaces). Owns config, builds, history, calibration UI, welcome screen,
  Studio block editor, HUD/analytics windows.
- **`prospector_engine/`** — the shared macro engine package. Entry via the
  `prospecting_old.py` launcher shim (dev) or self re-exec `--run-engine`
  (frozen). Home = the data directory (config lives there).
  - `engine.py` — supervisor loop, detection, cycle state machine, recovery,
    safe-stop, Studio script interpreter.
  - `platform_mac.py` / `platform_win.py` — screen capture (mss/Quartz) and
    input synthesis (Quartz events / SendInput). No process-memory APIs.
  - `ipc.py`, `client.py`, `protocol.py` — local parent↔child stdio protocol
    (no sockets).
  - `sensing.py`, `vision.py`, `recovery.py`, `flows.py`, `cycleplan.py`,
    `recorder.py`, `settings.py` — calibration, detection, recovery, config
    schema.
- **`prospecting_ui.py`** — browser-fallback settings UI + `STUDIO_BLOCKS`
  block schema (single source of truth for the Studio editor).
- **`prospecting_assistant.py`** — the Coach's offline rule engine and
  calculator formulas (no network of its own).

## Platform trees

- Repo root = macOS tree. **`windows/`** mirrors the four app files
  byte-identically for all shared code (`prospecting_app.py`,
  `prospecting_old.py`, `prospecting_ui.py`, `prospecting_assistant.py`)
  plus Windows packaging (`prospecting.spec` PyInstaller one-folder,
  `installer.iss` Inno Setup, `build.bat`). `tour_check.py` enforces
  lockstep between the copies.
- macOS packaging: PyInstaller `.app` + DMG (see `BUILDING.md`);
  `build_dmg.command` builds it locally.
- CI: `.github/workflows/` builds both platforms.

## Data locations

Dev (running from source): all data files live next to the scripts.
Frozen (packaged):
- macOS: `~/Library/Application Support/Prospector Lite`
- Windows: `%APPDATA%\Prospector Lite`
  (one-time migration imports user data from the legacy
  `%LOCALAPPDATA%\Prospectors Plus` directory; nothing is deleted).

Files: `prospecting_config.json` (settings + calibration),
`prospecting_builds.json`, `prospecting_scripts.json` (Studio),
`run_history.json`, `tutorial_content.json` (local help edits),
`instance_id` (random UUID for the local Studio-companion handshake; never
transmitted), `studio_macro_status.json` / `studio_push.json` (only under a
Prospector Studio embedded launch).

## Network behaviour (after the public-release pass)

Normal use makes **zero network requests**. The only egress paths are
user-configured and disabled by default:
1. Discord webhook notifications (`WEBHOOK_ENABLED` + user's `WEBHOOK_URL`).
2. The Coach's optional AI mode (user-supplied API key to the provider the
   user picks). Offline rule mode is the default.
Full inventory: `docs/public-release/NETWORK_BEHAVIOR.md`.

## Test infrastructure (all local, no network)

`tour_check.py` (UI/lockstep protocol), `finds_sim.py`, `studio_tests.py`,
`prospecting_selftest.py`, and the engine suites
(`engine_contract_tests.py`, `engine_parity_tests.py`,
`engine_characterization.py`, `engine_plan_tests.py`, `engine_trace_tests.py`,
`engine_flow_tests.py`, `engine_pacing_tests.py`, `engine_parallel_tests.py`,
`engine_lite_drive.py`, `studio_conformance.py`) against
`engine_scenarios/` + `engine_goldens/`. `public_release_tests.py` adds the
branding / privacy / access-code / network-egress release gates.

## Prospector Studio relationship

Prospector Studio is a separate authoring product that can embed this app
(`PP_DATA_DIR`, `PP_THEME`, `PP_STUDIO_LAUNCH`, `PP_STUDIO_SCRIPT` env
contract). Standalone Prospector Lite sets none of these and has no Studio
dependency; the `PP_*` names are internal compatibility identifiers.
