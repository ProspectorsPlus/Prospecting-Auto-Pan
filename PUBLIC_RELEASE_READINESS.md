# Prospector Lite — Public-release readiness

Updated for 1.0.0-rc.6 (final pre-publish pass: startup-routing
authority + Skip Wizard, tutorial auto-open with honest opt-out,
pan-capacity right-end fix + Test capacity calibration, local
diagnostic/recommendation system with FAQ, Fortune River out of
onboarding, Windows mirror healed + guarded + acceptance script — see
docs/final-prepublish/IMPLEMENTATION_REPORT.md; rc.5 calibration polish
in docs/final-visible-pass/CALIBRATION_POLISH_RC5.md, rc.3
stabilization history in docs/stabilization/STABILIZATION_REPORT.md).

Verdict: **READY AFTER USER ACTION** — the engineering work is complete and
verified on macOS; the remaining blockers are decisions/credentials only
the owner can provide, plus the first green Windows CI run after pushing.

> **Superseded by launch (2026-08-08).** Prospector Lite **5.0.0** was
> published: branch pushed, tag `v5.0.0`, public GitHub Release with
> macOS + Windows artifacts from CI, website deployed from `docs/`.
> The user-action list resolved as recorded in `RELEASING.md` (launch
> record) and `docs/public-launch/`: secrets verified revoked, fresh
> history moot (history public since June 2026), `PROJECT_URL` set,
> Windows CI green. Still open, owner-only: license choice, Apple
> signing/notarization, Windows Authenticode.

## Category verdicts

| Category | Verdict | Basis |
|---|---|---|
| SECURITY | READY | Gate/tracking/fingerprint code removed; bandit 0-high; pip-audit clean; injection-API scan zero hits; https-only webhook; **TLS bypass removed in rc.2** — certificate verification mandatory at all three egress sites, certifi verified fallback, no unverified retry; audits in docs/public-release/ |
| PRIVACY | READY | Zero network by default, empirically verified (network-denied suites + packaged app observed with 0 sockets); opt-in paths documented; **`NOTIFY_SCREENSHOT` now defaults off** — webhook screenshots are a separate opt-in |
| TRUST & ONBOARDING | READY | rc.2: 5-step first-run wizard with resumable atomic state; real macOS preflights and real in-app capability tests; permanent Trust Center with local-data management; `launch()` permission gate; build identity + build-time trust manifest. rc.6 adds: one startup-routing authority, honest Skip Wizard (no option weakens the Start gates), tutorial auto-open with opt-out, and a fully local diagnostic/recommendation system (16 rule families, bounded Apply/Undo, 20-entry FAQ) — nothing transmitted |
| BRANDING | READY | "Prospector Lite" on every surface incl. packaged bundle + welcome (visually confirmed); automated brand scan in the release gate |
| ACCESS | READY | No access code, no lock, no machine bind; the first-run wizard replaces the gate; legacy fields ignored + scrubbed; old-welcome users migrate straight to FINISHED |
| MACOS | READY | Self-contained PyInstaller .app + DMG pipeline, content-audited, launched from the mounted read-only DMG offline with isolated data dir; ad-hoc signed unless `CODESIGN_ID` set (see user actions); signed/notarized state honestly shown in-app |
| WINDOWS | READY AFTER USER ACTION | Spec/installer/portable-zip/CI complete and YAML-validated, optional Authenticode steps gated on cert secrets; rc.6 healed a real mirror divergence (windows/prospecting_ui.py missed the atomic config write), added a full-file twin byte-parity guard + one-command mirror sync, and `packaging/windows_acceptance.ps1` (in build-windows.yml); **Windows runtime still never executed** — requires push + green `build-windows.yml` run before any Windows artifact is published |
| DOCUMENTATION | READY | README/SECURITY/PRIVACY/CONTRIBUTING/CODE_OF_CONDUCT/CHANGELOG/BUILDING/RELEASING/SUPPORT/THIRD_PARTY_NOTICES + audit docs, updated through rc.6 (diagnostics privacy story, Skip Wizard/tutorial preferences, capacity validation + Test action); new user docs WELCOME_AND_SETUP/TUTORIAL/DIAGNOSTICS/RECOMMENDATIONS/FAQ + five engineering reports in docs/final-prepublish/; placeholders clearly marked where the repo URL is pending |
| LICENSING | READY AFTER USER ACTION | No license chosen (deliberately not chosen on the owner's behalf) — see LICENSE_CHOICE_REQUIRED.md; redistribution blocked until then |
| DEPENDENCIES | READY | Inventory (now incl. certifi, MPL-2.0) + per-build SBOM + reproducible build-venv freeze; pip-audit clean; pinning gap documented |
| PACKAGING | READY (macOS) / pending CI (Windows) | No system Python, no first-run pip, sanitized config, docs + build identity + trust manifest bundled, offline smoke probe in both pipelines; the rc.4 screenshots debt was CLOSED in rc.5 (owner captures sanitized into approved examples for all 11 items); specs pin the new `lite_diagnostics` hidden import |
| FUNCTIONALITY | READY | All pre-existing suites pass post-change (incl. golden/characterization byte-parity), plus the release gate, the onboarding/trust suite, the wizard DOM suite, and the rc.6 additions `capacity_tests.py` (51 checks) and `diagnostics_tests.py` (185 checks), all in CI |
| REPOSITORY HYGIENE | READY | Gate infra, stale binaries, dead code, personal data untracked; `studio_conformance.py` now skips cleanly when the private goldens are absent, so public clones/CI pass |
| REPRODUCIBILITY | READY (documented bound) | Two independent builds: identical file lists, 1165/1167 files bit-identical; the two exceptions and the DMG-container caveat documented; rc.2 adds two commit-derived stamped inputs (richer build_info.json, trust_manifest.json) |
| MODERATION REVIEW MATERIALS | READY | Architecture, threat model (incl. new trust-surface rows), network inventory, Roblox safety boundary (three permissions), secret audit, verification evidence — all in docs/public-release/ |

## Why not plain READY — exact user actions required

1. **Choose a license** and commit it as `LICENSE` (LICENSE_CHOICE_REQUIRED.md
   compares MIT / Apache-2.0 / GPL-3.0; note pynput is LGPL-3.0).
2. **Revoke the historically committed secrets**: delete/regenerate the
   Discord webhook from commit `6842a47` and rotate the notify-bot secret
   from commit `e0f3d4f` (values live only in history and in your local
   `prospecting_secrets.json`).
3. **Publish from fresh history** (or owner-approved history rewrite):
   pushing the existing history would expose item 2. RELEASING.md documents
   the procedure.
4. **Set the public repository URL** — `PROJECT_URL` in `prospecting_app.py`
   (both copies) or `PP_PROJECT_URL` at build time — and replace the marked
   placeholders in the docs. Until set, "View code" uses the honest local
   fallback instead of exact-commit links.
5. ~~Supply approved example calibration screenshots~~ — **CLOSED in
   rc.5**: the owner's captures were sanitized
   (`packaging/sanitize_examples.py`) into approved example images for
   all 11 calibratable items; only `roblox_window` keeps its honest
   placeholder.
6. **Push and run Windows CI** (`build-windows.yml`); do not publish any
   Windows artifact before it is green. Optionally run a manual journey on
   a real Windows machine — `packaging/windows_acceptance.ps1` +
   `WINDOWS_TESTING.md` now script/describe that verification.
7. **(Optional) Apple Developer certificate** for signing + notarization
   (`CODESIGN_ID` enables hardened-runtime signing; notarization/stapling
   are manual steps needing Apple credentials); until then the DMG ships
   ad-hoc signed (right-click → Open), which is disclosed everywhere and
   labelled in-app. **(Optional) Windows Authenticode certificate** via the
   `WINDOWS_CERT_PFX_B64`/`WINDOWS_CERT_PASSWORD` CI secrets.

## Hard-blocker checklist from the release specification

- active IP/location tracking: **absent** ✓
- hidden telemetry: **absent** ✓
- bundled secret: **absent** (package audited) ✓
- access-code dependency: **absent** ✓
- open-source license: **missing — user action** ✗ (blocks publication, not readiness of the work)
- unreviewed executable download: none published ✓
- broken Safe Stop: all stop paths release inputs (suite-verified) ✓
- package needs system Python: **no** (verified from mounted DMG with restricted PATH) ✓
- Windows package claimed but untested: **not claimed** — explicitly pending CI ✓
- prohibited branding: **absent** (scanned, incl. inside the package) ✓
- source/package mismatch: build_info commit stamp + package manifest ✓
- unresolved P0/P1 security finding: **none** ✓
