# Changelog

All notable changes to Prospector Lite are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0-rc.4] — 2026-07

The visible-onboarding release candidate: guided calibration now lives entirely inside the setup wizard, setup progresses sequentially, Advanced Cue Matching becomes the required primary detector, the main tutorial starts itself exactly once after setup, and the app finally wears its diamond icon at the OS level.

### Changed
- **Guided calibration never leaves the wizard.** Clicking any calibration step (Capacity first) opens a detail page inside the setup wizard; the previous behavior — hiding the wizard, switching the main app to the Calibrate tab and opening its legacy modal — is gone, and a DOM regression suite fails if any guided action ever selects the normal Calibrate tab again. Success returns to the checklist automatically and activates the next step; failure and cancel stay on the guided page; a stale or foreign overlay result can never navigate. The wizard drives the SAME shared calibration engine and save path as the Calibrate tab (a `guided_setup` / `normal_calibration` context affects only the completion callback, never the values).
- **Real sequential progression** on both the permissions page and the calibration checklist: exactly one ACTIVE step ("Do this next"), later required steps faded and labelled "Upcoming" with the gating step named, completed steps labelled and reopenable, optional steps never blocking. States derive from live permission/calibration status (opening System Settings completes nothing), carry text chips + step numbers + ARIA labels — never colour or opacity alone.
- **Advanced Cue Matching is now REQUIRED** — the primary prompt detector (letter-shape masks for Pan / Collect Deposit / Shake; a prompt counts as visible only when 85% of its mask pixels read white). It is on by default, required by the calibration registry, the setup checklist, the Readiness Check and the classic-mode Start gate; single-pixel-only calibration no longer counts as ready anywhere. The single-pixel checks remain as a supported fallback while a mask is missing or disabled. Installs that finished setup before this requirement keep every existing value, get a one-time config backup (`prospecting_config.json.pre-advcue.bak`), and see the step as **Needs review** at its position instead of being silently un-finished.
- **The permission page lost its global "never requested" list.** The five never-requested capabilities and the informational row moved out of the first-run page (a concise pointer remains); the full detail stays in the Trust Center and PERMISSIONS.md, and per-capability privacy facts (access, retention, network, revocation) stay on every card.
- **Structured instructions for every calibration step.** A per-item instruction registry (purpose, affected modes, Roblox setup, where to stand, what must be visible, the exact selection, what a correct result looks like, common mistakes, captured data + retention, validation, retry, and what stays unavailable) renders on each guided detail page. Capacity's instructions state the exact right-tip/left-tip selection and the >20 px width validation. Example screenshots render only from owner-approved assets; otherwise an honest "not yet available" note appears (never a fabricated image).
- **The main tutorial starts itself — once.** After setup genuinely finishes (onboarding FINISHED), the full tutorial auto-starts exactly once per tutorial schema version. Its lifecycle (NOT_STARTED / ACTIVE / COMPLETED / DISMISSED) now persists atomically in `tutorial_state.json` inside the data directory instead of WebKit localStorage; finishing records COMPLETED, skipping records DISMISSED, neither ever auto-starts again, and the Tutorials menu can always replay it. The old localStorage flag migrates as COMPLETED so long-time users are not forced through it. The tour can no longer fire on top of (or underneath) the setup wizard, and its calibration step now teaches the maintenance tab + required cue masks instead of first-run directions; a new Trust Center step covers permissions tests, Safe Stop, local data and exports.
- **Diamond app icon everywhere.** A new original faceted-diamond mark (matching the in-app gem) generated deterministically by `packaging/make_icon.py` into an editable SVG master, all PNG sizes (16–1024), the macOS ICNS and the Windows ICO. Both build pipelines now regenerate their compiled icon on every build — the stale-icon guards that kept shipping the old artwork are removed and gate-tested, and the release gate rejects the old icon by hash.
- Readiness Check gains a dedicated **Advanced cue matching** row, and every calibration Fix Now deep-links to the exact guided detail page inside the wizard (never the normal Calibrate tab during setup).
- After setup, the Welcome screen offers **Review setup / Start tutorial / Trust Center**; the `SHOW_WELCOME_EVERY_LAUNCH` preference is untouched and showing Welcome never re-runs permissions or calibration.

### Added
- `wizard_ui_tests.py` + `wizard_ui_tests.js`: a permanent DOM-level regression suite that boots the real embedded UI (build_html) under jsdom with canned bridge payloads composed by the real registry code, and walks the full first-run journey — wizard containment, progression, cancel/failure/stale paths, needs-review migration, tutorial once-only auto-start. Wired into CI.
- `Sensing.cue_check` + `Api.cue_mask_check`: validates a saved cue mask against the live screen with the same math the run-time detector uses (ratio re-placement, 2 px drift refusal, 85% threshold), including a background-white false-positive warning.

### Fixed
- The engine parity golden for the calibration sequence was regenerated for the new `advanced: true` default (deliberate; no detection behavior changes while no masks are captured — the box fallback is byte-identical).

## [1.0.0-rc.3] — 2026-07

The wizard stabilization release candidate: every first-run blocker reported against rc.2 reproduced, root-caused and fixed.

### Fixed
- Permission cards now update live: every request/settings/test action patches the status in place, a manual **Refresh status** button exists, a bounded post-action re-check runs while the page is visible, and returning from System Settings triggers a refresh via a focus watcher that does not depend on unreliable webview events. Out-of-order refreshes are dropped.
- Honest permission states: **"Not requested yet"** (macOS was never asked) is no longer shown as "Not granted"; **"Granted — restart to apply"** appears when a grant cannot take effect in the running process, with a one-click **Restart Prospector Lite** button; per-card **"I've enabled it — check again"** explains the stale-System-Settings-row case that unsigned rebuilds cause.
- Input test: the sandbox key-post worker now reports its real result back to the page (refused-not-frontmost / blocked / posted), so a refusal is never mis-blamed on Accessibility; the pass check matches the physical key (`e.code`) so non-QWERTY layouts pass; runs are debounced; failure text is platform-correct on Windows.
- Safe Stop test: single-flight (double clicks can no longer cross-write results), visible countdown, listener-start failure reported distinctly from a timeout, stale results dropped by request id.
- "Show this screen at every launch": defaults **ON**, renders the stored preference every time (launches, Back, menu reopen), persists immediately on toggle via one positive key (`SHOW_WELCOME_EVERY_LAUNCH`, legacy `WELCOME_SEEN` migrated once and removed), and a failed save is shown instead of silently reverting.
- Calibration: picker buttons surface real errors (previously silent no-ops), Screen-Recording-denied opens an explanation instead of a full-screen black overlay, a missing overlay window is an honest error instead of a fake success, "Roblox window found" is no longer claimed when it was not (truthiness bug), the wizard Test explains an empty sample on fresh auto-calibrated installs, and saving an optional pixel no longer disables auto-calibration for the required points.
- Start Macro now enforces the calibration gate the Readiness Check reports (missing/stale required calibration blocks with a clear message), matching the long-documented contract.
- Fresh installs can no longer be silently marked as fully set up by the legacy-welcome migration (ordering bug); the onboarding state file is persisted eagerly so packaged first-boot verification is real.
- All host config writes are atomic (13 truncate-write sites removed); the engine's config backup is now a copy, closing a window where concurrent readers saw an empty config.
- Windows honesty: capability rows reflect your own passing test this session, elevation-check failure no longer reports "running as a normal user", the input test uses `SendInput` correctly, and the crash input-release backstop actually works on Windows builds (it was a silent no-op).
- Wizard diagnostics: an `onboarding.log` (no secrets, no keystrokes) records every wizard operation with error codes, JS errors are forwarded to it, diagnostics export includes it, and the Readiness page gains **Copy diagnostic summary** / **Open wizard log**. `PP_DEBUG=1` opens the webview inspector.
- Independent re-verification round (adversarial, fresh context): the in-place card patch now preserves the LIVE test area (the sandbox button stayed clickable-looking but dead), test output areas are resolved per surface at write time (the wizard and Trust Center render the same card ids; results could land on the hidden or a detached card), the Calibrate tab's own picker buttons surface the new coded refusals too, the Win64 `SendInput` struct was corrected (union must be `MOUSEINPUT`-sized), a clean Safe Stop timeout is no longer recorded as a failed test, an input-control pass now requires the observed keyboard round-trip AND the pointer wiggle, the calibration overlay refuses honestly in the granted-but-restart-pending state, the engine's stale auto-calibration ratio profile was aligned with the shipped profile (a ratio-less config placed the capacity bar ~4%/9% off), and the launch calibration gate applies to classic runs only (Studio scripts that use no pixel calibration are not blocked).

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
