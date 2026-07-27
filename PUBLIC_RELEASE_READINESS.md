# Prospector Lite — Public-release readiness

Verdict: **READY AFTER USER ACTION** — the engineering work is complete and
verified on macOS; the remaining blockers are decisions/credentials only the
owner can provide, plus the first green Windows CI run after pushing.

## Category verdicts

| Category | Verdict | Basis |
|---|---|---|
| SECURITY | READY | Gate/tracking/fingerprint code removed; bandit 0-high; pip-audit clean; injection-API scan zero hits; https-only webhook; audits in docs/public-release/ |
| PRIVACY | READY | Zero network by default, empirically verified (network-denied suites + packaged app observed with 0 sockets); opt-in paths documented |
| BRANDING | READY | "Prospector Lite" on every surface incl. packaged bundle + welcome (visually confirmed); automated brand scan in the release gate |
| ACCESS | READY | No access code, no lock, no machine bind; welcome onboarding replaces the gate; legacy fields ignored + scrubbed |
| MACOS | READY | Self-contained PyInstaller .app + DMG built, content-audited, launched from the mounted read-only DMG offline with isolated data dir; unsigned (see user actions) |
| WINDOWS | READY AFTER USER ACTION | Spec/installer/portable-zip/CI complete and YAML-validated; **no Windows runtime execution happened in this pass** — requires push + green `build-windows.yml` run before any Windows artifact is published |
| DOCUMENTATION | READY | README/SECURITY/PRIVACY/CONTRIBUTING/CODE_OF_CONDUCT/CHANGELOG/BUILDING/RELEASING/SUPPORT/THIRD_PARTY_NOTICES + 10 audit docs; placeholders clearly marked where the repo URL is pending |
| LICENSING | READY AFTER USER ACTION | No license chosen (deliberately not chosen on the owner's behalf) — see LICENSE_CHOICE_REQUIRED.md; redistribution blocked until then |
| DEPENDENCIES | READY | Inventory + SBOM (178 components) + freeze; pip-audit clean; pinning gap documented |
| PACKAGING | READY (macOS) / pending CI (Windows) | No system Python, no first-run pip, sanitized config, docs + build identity bundled, offline smoke probe in both pipelines |
| FUNCTIONALITY | READY | All 14 pre-existing suites pass post-change (incl. golden/characterization byte-parity), plus the new release gate |
| REPOSITORY HYGIENE | READY | 143-file tracked tree; gate infra, stale binaries, dead code, personal data untracked; tracked tree clean at every commit |
| REPRODUCIBILITY | READY (documented bound) | Two independent builds: identical file lists, 1165/1167 files bit-identical; the two exceptions and the DMG-container caveat documented |
| MODERATION REVIEW MATERIALS | READY | Architecture, threat model, network inventory, Roblox safety boundary, secret audit, verification evidence — all in docs/public-release/ |

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
4. **Set `PROJECT_URL`** in `prospecting_app.py` (both copies) to the real
   public repository URL and replace the marked placeholders in the docs.
5. **Push and run Windows CI** (`build-windows.yml`); do not publish any
   Windows artifact before it is green. Optionally run a manual journey on
   a real Windows machine.
6. **(Optional) Apple Developer certificate** for signing + notarization;
   until then the DMG ships unsigned (right-click → Open), which is
   disclosed everywhere.

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
