# Prospector Lite

**Version 1.0.0-rc.1** (release candidate) — an open-source desktop macro for the Roblox game *Prospecting*, for macOS and Windows.

Prospector Lite automates the pan/dig/shake loop by **reading pixels from your screen and sending ordinary keyboard/mouse input through the operating system**. It is an *external* tool: it never touches the Roblox process, its memory, its files, or its network traffic.

> **Not affiliated.** Prospector Lite is not affiliated with, endorsed by, or supported by Roblox Corporation or the developers of *Prospecting*. Using automation tools may violate the Roblox Terms of Use and can put your account at risk. Use it at your own discretion and risk.

## What it does

- **Auto-pan loop** — detects the dig bar, capacity bar, and game prompts from screen pixels and drives the full pan/dig/shake cycle.
- **Guided calibration** — an in-app Calibrate tab tunes the pixel positions for your screen and stores them locally.
- **Builds** — save, switch, and share named setting presets (`.ppbuild` files).
- **Analytics & HUD** — live pans/hour, run history, and an optional overlay HUD. All statistics stay on your machine.
- **Discord notifications (opt-in)** — paste *your own* Discord webhook URL to get start/stop/stats/safety alerts, optionally with a screenshot. Off by default; nothing is built in.
- **The Coach** — an in-app helper. The default "offline brain" is a local rule engine with no network access. An optional AI mode uses *your own* API key (Anthropic, OpenAI, Gemini, DeepSeek, or a custom/local endpoint).
- **Prospector Studio** — a built-in visual block editor for custom farming scripts (`.ppscript` files). Scripts are validated JSON walked as data — they are never executed as program code.
- **Interactive tutorial** and a welcome screen that explains privacy and shows the exact version/build you are running.

## How it works (the safety boundary)

- **Screen reading only**: `mss` screen capture (plus Quartz display/window queries on macOS, Win32 window queries on Windows) — see `prospector_engine/platform_mac.py` and `prospector_engine/platform_win.py`.
- **Ordinary OS input**: Quartz `CGEventPost` on macOS, `SendInput` scancodes on Windows.
- **No injection, no memory reading**: no DLL/dylib injection, no `ReadProcessMemory`/`WriteProcessMemory`/`CreateRemoteThread`, no `ptrace`/`task_for_pid`, no packet interception, no drivers or kernel extensions, no Roblox file modification. You can verify this yourself — see [docs/public-release/ROBLOX_SAFETY_BOUNDARY.md](docs/public-release/ROBLOX_SAFETY_BOUNDARY.md).
- **No claim of undetectability.** External input can still look automated. No macro is risk-free.

## Privacy in one paragraph

Prospector Lite makes **zero network requests by default**: no update checks, no analytics, no telemetry, no IP or location collection, no remote content. There is no login and no access code. The only two code paths that can reach the network are opt-in and point at servers *you* choose: your own Discord webhook, and the Coach's optional AI mode with your own API key. Full details: [PRIVACY.md](PRIVACY.md) and [docs/public-release/NETWORK_BEHAVIOR.md](docs/public-release/NETWORK_BEHAVIOR.md).

## Supported systems

| Platform | Notes |
|---|---|
| macOS | Packaged `.app`/DMG, or run from source. Needs Screen Recording + Accessibility permissions (below). The current build is **unsigned** — see Install. |
| Windows | Installer or portable ZIP, or run from source. No administrator rights, drivers, or services required to run. |

Running from source requires **Python 3.11+** (3.13 tested).

## Permissions (and why)

- **macOS — Screen Recording**: required to read game pixels (detection is entirely screen-based). Grant in System Settings → Privacy & Security → Screen Recording.
- **macOS — Accessibility**: required to send keyboard/mouse events. Grant in System Settings → Privacy & Security → Accessibility.
- Without these permissions the app cannot see or control anything; it fails safely rather than misbehaving.
- **Windows**: no special permissions. The app runs as a normal user process; the installer offers a per-user install, and the portable ZIP needs no installation at all.

## Install from a release

> `<repository URL — to be filled in when published>` — releases are not published yet; this section describes the process once they are.

1. Download the artifact for your platform from the release page (macOS DMG; Windows installer `.exe` or portable `.zip`).
2. **Verify the checksum** against the `SHA256SUMS.txt` published with the release:

   ```sh
   # macOS
   shasum -a 256 -c SHA256SUMS.txt
   # Windows (PowerShell)
   Get-FileHash .\<file> -Algorithm SHA256
   ```

3. **macOS**: the app is currently **unsigned** (no Apple Developer certificate yet), so the first launch requires right-click → Open → Open, or approval under System Settings → Privacy & Security. This is stated plainly so you know what to expect; if you prefer, build from source instead.
4. **Windows**: run the installer, or unzip the portable build anywhere and run the app.

To build it yourself, see [BUILDING.md](BUILDING.md).

## Configuration and data

All data lives in local JSON files — there is no server side.

| Where | When |
|---|---|
| Next to the scripts | Running from source |
| `~/Library/Application Support/Prospector Lite` | Packaged macOS app |
| `%APPDATA%\Prospector Lite` | Packaged Windows app |

Key files: `prospecting_config.json` (settings + calibration), `prospecting_builds.json`, `prospecting_scripts.json` (Studio scripts), `run_history.json`, `run_logs/` (per-run logs), `tutorial_content.json` (local help edits), `instance_id` (random local UUID for the Studio-companion handshake — never transmitted), `prospecting_calib_log.csv` (calibration log), and gitignored `prospecting_secrets.json` (your Coach API key, if you set one). Deleting the folder (or individual files) deletes your data; nothing exists anywhere else.

If a legacy "Prospectors Plus" data folder exists, a one-time copy-only migration imports your settings and strips the old private keys; the old folder is never modified. See [PRIVACY.md](PRIVACY.md).

## Safe Stop

- **Esc** quits the macro instantly. **Ctrl+K** toggles start/stop. Every stop path first releases **all** held keys and mouse buttons (`release_all` in `prospector_engine/engine.py`), so input can never stay stuck down.
- Watchdogs run independently of the macro logic: a no-progress timeout, a bounded recovery loop (hard stop after repeated failures), and strict step/time budgets for Studio scripts.

## Troubleshooting basics

- **Nothing is detected / clicks land wrong (macOS)**: re-check Screen Recording and Accessibility permissions, then fully quit and reopen the app (permissions apply on restart).
- **Detection stopped working after a game update**: re-run the Calibrate tab; pixel positions move when the game UI changes.
- **Webhook test fails**: the URL must start with `https://`; use the "send test" button on the Notifications page — it reports the exact HTTP error.
- **macOS says the app is from an unidentified developer**: expected for now (unsigned build); right-click → Open.
- **Old macOS Python certificate errors**: webhook delivery retries once without SSL verification as a fallback — see the known-limitation note in [SECURITY.md](SECURITY.md).

More help: [SUPPORT.md](SUPPORT.md).

## Limitations

- Detection is pixel-based: unusual resolutions, color filters, or game UI updates can break it until you recalibrate.
- The macOS build is unsigned (see above).
- The webhook sender falls back to an SSL-unverified retry on certificate failures (documented limitation for old bundled-Python certificate setups).
- No macro is "safe" or "undetectable"; nobody can promise your account is risk-free.

## Security

To report a vulnerability, see [SECURITY.md](SECURITY.md). For the full threat model and network inventory, see [docs/public-release/](docs/public-release/).

## License

**This project does not have a license yet.** Until one is chosen, the code is not licensed for redistribution. See [LICENSE_CHOICE_REQUIRED.md](LICENSE_CHOICE_REQUIRED.md) for status and options.
