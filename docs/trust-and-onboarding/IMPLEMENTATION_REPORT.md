# Implementation report — trust & onboarding release (1.0.0-rc.2)

What was built in this pass, where it lives, and what remains for the
owner. Companion documents: CURRENT_SYSTEM.md (the before-state),
ACCEPTANCE_MATRIX.md (what was executed), TEST_MATRIX.md (coverage map).

## Delivered

1. **Capability registry** — `lite_trust.py`. Eleven capabilities
   (3 required, 2 optional network, 1 informational, 5 explicitly
   never-requested) with full Phase-2 schema, REAL macOS detection
   (`CGPreflightScreenCaptureAccess`, `AXIsProcessTrusted`,
   `CGPreflightListenEventAccess` — none can prompt), explicit
   user-triggered requests, capability tests, System Settings deep links
   with manual fallbacks.
2. **First-run state machine + calibration registry** —
   `lite_onboarding.py`. Atomic `onboarding_state.json`, forward-only
   transitions, rerun/reset, corrupt-file recovery, one-time
   `WELCOME_SEEN → FINISHED` bridge for existing installs; twelve
   calibration items derived from the keys the engine actually reads,
   with honest status semantics.
3. **Wizard + Trust Center UI** — inside `prospecting_app.py` (the
   `#setup` overlay, the `trust` pinned tab, `SETUP`/`__tcRender` JS),
   step rail with ARIA, platform tabs (keyboard-switchable), capability
   cards, in-app sandbox tests, calibration checklist that drives the
   existing overlay/wizard flows (single calibration store), Readiness
   Check with Fix Now / Retest / Export diagnostics / Exit, hamburger
   menu entries, and window-focus status refresh (no polling loops).
4. **Trust gate on Start** — `launch()` returns `perm:<ids>` when a
   required macOS permission is definitively not granted; the UI opens
   the Trust Center. Nothing else is blocked.
5. **Mandatory TLS** — all three `ssl._create_unverified_context`
   fallbacks removed (engine webhook, webhook test, Coach API); verified
   contexts with a bundled-certifi trust-store fallback; SSL failures
   fail safely with clear messages; `NOTIFY_SCREENSHOT` defaults off;
   payload preview built by the engine's own `_webhook_payload`.
6. **Build identity + trust manifest** — richer `build_info.json`
   (commit/date/version/dirty/package/project_url/signed/notarized) on
   all three build paths; `lite_trust.py --emit` generates
   `trust_manifest.json` from the exact source with an ast resolver that
   fails the build on dead references; View Code opens exact-commit URLs
   when a repo URL is configured, otherwise an honest local fallback.
7. **Example-asset pipeline** — `assets/onboarding/calibration/`
   manifest with honest placeholders; owner-only (dev checkout) capture
   and approve tools; nothing fabricated ships.
8. **Packaging** — macOS spec/DMG script updated (certifi, docs, assets,
   manifest, hardened-runtime signing readiness with tracked minimal
   entitlements); Windows spec/installer/build.bat/CI refreshed (rc.2,
   per-user, publisher wording, optional Authenticode via CI secrets);
   `packaging/sync_windows_app.py` regenerates the Windows app copy;
   `packaging/packaged_acceptance.command` automates the DMG probes;
   `packaging/make_release_manifest.py` emits `release-manifest.json`.
9. **Tests** — `onboarding_trust_tests.py` (new, in CI) +
   `public_release_tests.py` gate extensions (TLS-bypass ban,
   licence-wording scan). Full suite results in ACCEPTANCE_MATRIX.md.
10. **Docs** — PERMISSIONS / TRUST_CENTER / VERIFY_DOWNLOAD /
    INSTALL_MACOS / INSTALL_WINDOWS (new), every existing public and
    audit doc refreshed for rc.2, and this internal suite.

## Bugs found and fixed during the pass

- Wizard reset deleted the state file, letting the legacy bridge
  immediately re-mark setup FINISHED (reset now rewrites NOT_STARTED in
  place; caught by `child_api_flow`).
- `_tutorial_remote()` dangling reference crashed the owner-only
  tutorial export (removed; local-only export now).
- `windows/prospecting_app.py` was behind root by the Studio
  protocol-1.5 identity feature (regenerated; sync is now scripted).
- `studio_conformance.py` hard-failed on public clones (now skips).
- Acceptance script: `lsof -p -i` OR-semantics and the pipefail-on-empty
  lsof result produced false readings (fixed with `-a` and a guard).

## Honest status

- Windows: prepared, never executed on real Windows this pass.
- Signing/notarization: not performed (no credentials); builds are
  ad-hoc signed and say so in Build Identity.
- Example screenshots: placeholders; final public packaging blocked on
  owner-approved captures.
- Licence, public repo URL, historical-secret revocation, fresh-history
  publication: owner actions, unchanged from rc.1 (see
  PUBLIC_RELEASE_READINESS.md).
