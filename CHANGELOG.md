# Changelog

All notable changes to Prospector Lite are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
