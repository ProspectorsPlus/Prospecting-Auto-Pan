# The Trust Center

The Trust Center is the permanent, always-available version of the setup wizard's trust screen: one tab where you can see what Prospector Lite can do, prove it, and manage everything it stores. Open it from the shield icon in the sidebar, or from the Tutorial menu → **Trust Center**. This document walks through every section. (The section headings below match the in-app labels exactly, including their spelling.)

Everything shown is live — statuses come from real operating-system checks (`prospecting_app.py`, `Api.trust_state`; `lite_trust.py`, `capability_statuses`), and reading them can never trigger a permission prompt. Prompts fire only from buttons you click.

## Permissions & capability tests

One card per capability, filtered to your platform, showing:

- **Live status.** On macOS: *granted* / *not granted*, read with the real preflight APIs (`CGPreflightScreenCaptureAccess`, `AXIsProcessTrusted`, `CGPreflightListenEventAccess`). On Windows there is no permission model for these, and the card says so instead of inventing a "granted" state. Optional network features show *configured* / *disabled*; never-requested items show *not requested*.
- **Request** (macOS only): triggers the actual OS permission prompt for that capability — the only way a prompt ever appears.
- **Open Settings** (macOS only): deep-links to the exact System Settings pane, with the manual path as fallback.
- **Test**: runs the real capability, bounded and safe — a small one-shot screen grab shown once and discarded; one sandbox keystroke whose key-down *and* key-up are verified plus a 2-pixel pointer wiggle read back from the cursor; an 8-second Safe Stop listen for Esc / Ctrl+K only.
- **View code**: where this capability is implemented (module + symbol), with exact line-anchored links in the Source code section below.
- What happens if you **decline**, and how to **revoke** later.

The full permission guide, including the "Screen & System Audio Recording" label explanation and the never-requested list, is `PERMISSIONS.md` — openable from the Security reporting section at the bottom of the tab.

## Build identity

Exactly which build you are running (`lite_trust.py`, `build_identity`):

| Row | Meaning |
|---|---|
| Version | The app version (e.g. `1.0.0-rc.5`). |
| Commit | The exact git commit this build was made from. "(built from modified source)" marks a development build whose tree did not match that commit. |
| Built | Build timestamp from the stamped `build_info.json` (blank for source runs). |
| Platform | OS and architecture the app is running on now. |
| Package | `app-bundle` for packaged builds, `source` when running from a checkout. |
| Signed | Whether the build was signed with a real certificate. `no (unsigned build)` means an ad-hoc/unsigned build — see `VERIFY_DOWNLOAD.md` for what that implies. |
| Notarized | Whether Apple notarization was performed (macOS). |
| Licence | The licensing status: source-available; no open-source license chosen yet (`LICENSE_CHOICE_REQUIRED.md`). |

Use this together with `VERIFY_DOWNLOAD.md` to tie your download to a published release.

## Source code

Two things live here:

- **Repository link** — when the build was made with a public repository URL configured, every **View Code** button opens the file *at the build's exact commit*, never a moving branch. When no URL is configured (the current state, pending publication), the section says so honestly and View Code shows the exact local file + symbol + line range + commit instead.
- **The trust manifest** — an expandable JSON listing, for every capability, the file, symbol, and exact line range implementing it. It is generated at build time from the exact source of the build (`lite_trust.py`, `generate_manifest`, invoked as `python3 lite_trust.py --emit`); a reference that stops resolving fails the build, so the manifest cannot silently rot.

## Network behaviour

A plain statement of the app's entire network surface: normal startup and macro use make **zero** requests; the only outbound paths are the two optional, off-by-default features (your own Discord webhook, your own Coach AI key — both covered on the capability cards above, with an exact payload preview for the webhook) and links you click. TLS certificate verification can never be disabled. There is no update check, no analytics, no telemetry. The full inventory with evidence is `docs/public-release/NETWORK_BEHAVIOR.md`, and the release gate runs the app and engine in network-denied child processes to prove the default is really zero (`public_release_tests.py`).

## Roblox safety boundary

The boundary the app keeps with the game: it never injects into Roblox, never reads or writes another process's memory, never modifies game files, and never intercepts network traffic. It sees pixels and presses ordinary keys — the same boundary a human at the keyboard has. The source scans in `public_release_tests.py` fail the build if any process-memory API appears; the full write-up is `docs/public-release/ROBLOX_SAFETY_BOUNDARY.md`. Note this is a statement about *how the tool works*, not a safety guarantee — no macro is risk-free.

## Local data

A live table of everything in the data folder — file name, purpose, and current size (`prospecting_app.py`, `Api.data_manifest`). Only known Prospector Lite files are listed, and only those files can ever be touched by the delete actions. Nothing is written into the app bundle. The folder is `~/Library/Application Support/Prospector Lite` (packaged macOS), `%APPDATA%\Prospector Lite` (packaged Windows), or the script folder when running from source.

Management actions:

- **Open data folder** — opens it in Finder / Explorer.
- **Export calibration** — saves a portable calibration JSON via the OS save dialog.
- **Export diagnostics** — saves a diagnostics summary (readiness results, build identity, capability statuses, calibration statuses). It contains **no secrets**: no webhook URL, no API key, no config dump (`prospecting_app.py`, `Api.export_diagnostics`).
- **Delete history** / **Delete logs** — remove run history and/or the `run_logs/` files.
- **Delete ALL local data…** — double-confirmed, and scoped strictly to the known files listed in the table (`prospecting_app.py`, `Api.delete_local_data`); it is never a recursive wipe of an arbitrary path. Afterwards the app returns to a fresh first run.

## Setup wizard

- **Re-run setup wizard** — reopens the full first-run flow (Trust & Permissions → Guided Calibration → Readiness Check) any time. Re-running deletes nothing; it simply walks you through again. Also available from the Tutorial menu.
- **Reset wizard progress only** — resets the wizard to a brand-new first run. This touches *only* the wizard's own progress (`onboarding_state.json`); builds, calibration, settings, and history are untouched (`lite_onboarding.py`, `Onboarding.reset`).

## Security reporting

Buttons that open the bundled documents: **SECURITY.md** (how to report a vulnerability privately), **PRIVACY.md** (the complete data story), and **PERMISSIONS.md** (the permission guide). If you find behavior that contradicts anything the Trust Center shows, that is precisely what the security-reporting channel is for.
