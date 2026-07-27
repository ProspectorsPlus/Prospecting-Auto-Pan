# Changelog

All notable changes to Prospector Lite are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0-rc.2] — 2026-07

The trust-and-onboarding release candidate: a first-run experience that shows, tests, and explains every capability the app uses, plus network hardening.

### Added
- **Five-step first-run setup wizard**: Welcome → Trust & Permissions → Guided Calibration → Readiness Check → the app. Progress persists atomically in `onboarding_state.json` and resumes after a close; re-runnable any time from the Tutorial menu ("Re-run setup wizard") or the Trust Center; resetting deletes only wizard progress. Users who completed the old single welcome screen are migrated as finished — nobody is forced back through setup.
- **Trust & Permissions screen** (wizard step 2, macOS + Windows tabs): capability cards with **real** macOS permission detection (`CGPreflightScreenCaptureAccess` / `AXIsProcessTrusted` / `CGPreflightListenEventAccess` — detection never triggers an OS prompt), Request buttons that fire the real OS request APIs, per-capability in-app tests (one-shot screen grab preview, sandboxed key-press with proven key-up, 2-px pointer wiggle read back, 8-second Safe Stop hotkey test), "View code" source references, and honest decline/revoke information. Explicit "not required" cards for microphone, camera, location, admin, and Full Disk Access.
- **Permanent Trust Center tab**: live permission status and tests, build identity, network-behavior summary, and a Local Data section listing every data file with sizes plus Open folder / Export calibration / Export diagnostics (secret-free) / Delete history / Delete logs / Delete ALL (double-confirmed, scoped to known files only).
- **Guided calibration in the wizard** (step 3) driving the *same* sensing engine and save path as the Calibrate tab — one calibration store, no parallel data. Backed by a calibration registry (`lite_onboarding.py` `CALIBRATION_ITEMS`) with honest per-item statuses (`auto`/`ok`/`stale`/`default`/`unset`/`off`).
- **Readiness check** (step 4) summarizing permissions + calibration; on macOS a definitively missing required permission gates only the Start button — never the app, settings, or docs.
- **Build identity + trust manifest**: `build_info.json` now carries commit, date, version, dirty flag, package type, project URL, and signing/notarization state (dirty tree ⇒ "development build" marker in-app). `lite_trust.py --emit` generates `trust_manifest.json` at build time — per-capability file/symbol/line references resolved from the exact source (stale references fail the build). "View code" opens exact-commit URLs when a public repository URL is configured, otherwise an honest local file+symbol+commit fallback — never a moving branch.
- **Webhook payload preview**: the Notifications page shows the exact payload built by the same engine function that sends (`_webhook_payload`); the optional secret header is redacted in previews.
- **Example-screenshot pipeline** for calibration steps (`assets/onboarding/calibration/manifest.json`): an image ships only when owner-approved and present; until then the wizard shows a clearly-labeled pending note.
- Public docs for the new surface, including a permissions reference, download-verification and per-platform install guides.

### Changed
- **All unverified-TLS fallbacks removed.** Webhook and Coach traffic is verified-TLS-only, with the bundled `certifi` trust store covering interpreters that ship without CA certificates (`git grep _create_unverified_context` returns nothing). *Compatibility note*: webhooks pointed at hosts with broken certificates previously succeeded via the insecure retry — they now fail safely instead.
- **`NOTIFY_SCREENSHOT` now defaults to off.** Enabling the webhook no longer implicitly opts into screenshot attachments; attaching screenshots is a separate opt-in.
- Windows installer wording corrected: per-user install (`PrivilegesRequired=lowest`), no administrator rights required or recommended.
- `studio_conformance.py` now skips cleanly (exit 0) when the private Studio goldens are absent, so public clones and CI pass.
- Documentation now consistently describes the project as source-available (no open-source license has been chosen yet) and documents all **three** macOS permissions, including Input Monitoring for the Safe Stop hotkeys.

## [1.0.0-rc.1] — 2026-07

The first public release candidate, and the first version under the **Prospector Lite** name. Everything before it (versions labeled 1.x through 4.x under the old "Prospectors Plus" name) was a private, invite-only pre-release and is not supported.

### Added
- Welcome onboarding screen on first launch: explains what the app does, its privacy posture, and the exact version/build identity. Reopenable any time via Tutorial menu → "Welcome, privacy & version"; includes a "Show this screen at every launch" option.
- One-time, copy-only migration of user data from a legacy "Prospectors Plus" install (config, builds, scripts, run history, tutorial edits). The old folder is never modified; the legacy access-gate/analytics keys (`ACCESS_OK`, `ACCESS_HASH`, `ACCESS_MACHINE`, `MACHINE_SALT`, `SYNC_URL`) are stripped and never carried over.
- Public documentation suite: privacy policy, security policy, threat model, network-behavior inventory, Roblox safety boundary, secret audit, building/releasing guides.

### Changed
- **Rebrand**: "Prospectors Plus" → **Prospector Lite** throughout the app. Packaged data directories move to `Prospector Lite` (macOS `~/Library/Application Support/Prospector Lite`, Windows `%APPDATA%\Prospector Lite`).
- **Discord notifications are now entirely user-owned**: nothing is preconfigured or bundled. Notifications only ever go to a webhook URL you paste yourself, and are off by default.

### Removed
- **Access codes / invite gate** — the app has no login, no codes, and no machine locking. It opens straight to the welcome screen.
- **Owner analytics and IP/location tracking** — the `SYNC_URL` owner-analytics endpoint and all machine-identity/geolocation collection are gone from the codebase.
- **All phone-home behavior** — no update checks, no telemetry, no remote content. Default network activity is zero requests (see `docs/public-release/NETWORK_BEHAVIOR.md`).

### Security
- The Coach API key now lives only in the gitignored `prospecting_secrets.json` (never in the config file, never bundled).
- A repository-history secret audit was performed; see `docs/public-release/SECRET_AUDIT.md` for findings and the fresh-history publication requirement.

## Earlier versions (private)

Versions 1.0.0 through 4.2.0 were distributed privately as "Prospectors Plus" between early and mid 2026. They included features that no longer exist (access codes, machine locking, owner analytics) and are intentionally not documented here.
