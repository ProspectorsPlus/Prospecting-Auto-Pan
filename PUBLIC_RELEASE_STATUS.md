# Prospector Lite — public-release status

Factual working state for the public-release pass. Updated as work lands.

## Starting state
- Branch: `fable/prospector-engine`
- HEAD at start: `ad7e9eb5dc280b04e77e208e97d8bf064d5a1afc`
- Tracked tree clean at start (192 files). Remote: `origin`
  (git remote configured locally; public URL pending owner decision).
- Baseline test run (this session, before any change): compile checks,
  tour_check, finds_sim, studio_tests, prospecting_selftest and all ten
  engine suites — ALL PASS.

## Confirmed entry points
- App: `prospecting_app.py` (+ byte-mirrored `windows/prospecting_app.py`).
- Engine: `prospector_engine/engine.py` via `prospecting_old.py` shim, spawned
  with `home=cwd=DATA_DIR`.
- macOS package: legacy `Prospectors Plus.app` shell launcher (system Python +
  first-run pip) — to be replaced by a self-contained PyInstaller build.
- Windows package: `windows/prospecting.spec` + `installer.iss` +
  `.github/workflows/build-windows.yml`.

## Security/privacy findings (pre-change)
1. `_report_usage()` posts IP + coarse geolocation (ip-api.com) + access-code
   hash + machine-id hash to an owner Discord webhook (`SYNC_URL`) at every
   run launch and code redemption. REMOVE.
2. Access gate: `access_state()` / `verify_access()` fetch hashed codes from
   GitHub Pages; machine-locked via `_machine_id()` (hardware UUID hash).
   REMOVE (gate, fingerprint, and config fields).
3. Auto update check (`check_update()`) runs at every boot; `do_update()`
   downloads + silently runs an installer. REMOVE (manual link only).
4. `_tutorial_refresh_remote()` fetches remote tutorial content at boot.
   REMOVE.
5. Analytics window HTML loads Google Fonts from the network. REMOVE (system
   fonts).
6. CI workflow injects the owner webhook into shipped configs from repo
   secrets. REMOVE.
7. User Discord webhook notifications + Coach AI mode (user key) are
   legitimate opt-in features: keep, disclosed, off by default.
8. Secrets IN GIT HISTORY (current tracked files clean; verified this
   session): commit `6842a47` committed the owner's real Discord webhook
   URL (configs + Windows zip); commit `e0f3d4f` committed the notify-bot
   `WEBHOOK_SECRET` value and the bot endpoint as baked fallbacks. Both
   were scrubbed from files in `7ff6537` but remain in history. No API
   keys were ever committed (history `-S sk-proj/sk-ant` hits are UI
   placeholder text only). `prospecting_secrets.json` and
   `ACCESS_CODES_PRIVATE.txt` were never committed. Consequence: revoke
   the webhook + bot secret, and publish the public repo from FRESH
   history. USER ACTION.
9. `prospecting_secrets.json` (untracked, gitignored) holds the owner's coach
   API key + webhooks; never committed. Must never be bundled.

## Completed changes (commits 39d66bc, b531240, 1026eac, 9644cc0 + final)
- Gate/tracking/fingerprint/auto-update removal + welcome onboarding.
- Full rebrand; repo hygiene (tracked tree 143 files); docs suite + audits.
- Release gate suite (public_release_tests.py) — ALL PASS incl. artifacts.
- macOS self-contained .app + DMG built, audited, DMG-mount + offline
  journey verified, welcome screen visually confirmed via screenshot.
- Windows packaging + CI rewritten (no secret injection); YAML validated;
  runtime execution pending push (documented blocker).
- release/public-candidate/ assembled: DMG, source archive, SBOM,
  checksums, manifests, notes.
- PUBLIC_RELEASE_READINESS.md: READY AFTER USER ACTION.
- Independent fresh-context verifier ran over the candidate: verdict
  "credible public-release candidate"; its P1 (frozen builds wrote the
  Coach key inside the install dir, contradicting PRIVACY.md) and P2
  (coach_history.json missing from the privacy inventory) are FIXED
  (SECRETS_FILE now derives from DATA_DIR when frozen, with a read-only
  legacy fallback; PRIVACY.md table updated; NETWORK_BEHAVIOR.md documents
  the source-only 127.0.0.1 UI fallback). Final artifacts rebuilt at the
  closing commit.

## Platform status
- macOS: dev runtime verified via test suites (this Mac).
- Windows: cannot execute locally; CI workflow will be rewritten, syntax
  validated; runtime verification on a real Windows runner = release blocker
  until the repo is pushed.

## Remaining genuine blockers (user-owned)
- Choose an open-source license (LICENSE_CHOICE_REQUIRED.md).
- Decide/confirm the public repository URL (used by welcome/About links).
- Revoke the historical Discord webhook + decide fresh-history publish.
- Apple signing/notarization credentials (unsigned RC otherwise).
- Push → run Windows CI on a real runner.

## 1.0.0-rc.3 — wizard stabilization pass (2026-07-28)

Release-blocker fix pass over the rc.2 wizard, driven by the owner's
manual test report (stale "Not granted" statuses, non-working input and
Safe Stop tests, calibration failures, the "show every time" checkbox
defaulting off and losing its state). Every blocker was reproduced or
deterministically root-caused (docs/stabilization/REPRODUCTION_REPORT.md,
docs/stabilization/CALIBRATION_CRASH_REPORT.md), fixed, regression-tested
(extended onboarding_trust_tests.py) and re-verified against a freshly
built DMG (docs/trust-and-onboarding/ACCEPTANCE_MATRIX.md — including a
correction to rc.2's packaged first-boot claim, whose probe criterion
could never pass on a true first boot). Details:
docs/stabilization/STABILIZATION_REPORT.md and the CHANGELOG 1.0.0-rc.3
entry. Owner actions carried forward unchanged (license, repo URL, webhook
revocation, signing, Windows CI on a real runner) — signing gained urgency:
ad-hoc rebuilds are the confirmed root cause of the "enabled in System
Settings but Not granted in the app" trap, which rc.3 now explains in-app
but only a Developer ID identity can remove.

## 1.0.0-rc.2 — trust-and-onboarding pass (2026-07)

Version bumped to 1.0.0-rc.2 (all three copies in agreement). Shipped:

- 5-step first-run wizard (welcome → trust & permissions → guided
  calibration on the real sensing engine and single calibration store →
  readiness check → app), with atomic resumable state
  (`onboarding_state.json`), re-runnable from the Tutorial menu and Trust
  Center; old-welcome users migrate straight to FINISHED.
- Capability registry (`lite_trust.py`) with real macOS preflights for the
  honest three-permission story (Screen Recording, Accessibility, Input
  Monitoring), user-clicked request buttons, settings deep links, and real
  in-app tests (live capture probe, sandboxed keystroke with release
  proven, one-shot Safe Stop listener). `launch()` blocks Start (only
  Start) on a definitively missing required permission.
- Network hardening: the SSL-unverified fallback is removed at every site;
  TLS verification is mandatory with a certifi verified fallback;
  `NOTIFY_SCREENSHOT` now defaults off; exact in-app webhook payload
  preview built by the engine's own `_webhook_payload`.
- Build identity (`build_info.json`: commit/date/version/dirty/package/
  project_url/signed/notarized) + build-time `trust_manifest.json` (ast
  source refs; dead refs fail the build); exact-commit View-code links
  when a public URL is configured, honest local fallback otherwise.
- Trust Center Local Data manager (live file list, open/export/delete,
  secret-free diagnostics export); `studio_conformance.py` skips cleanly
  without the private goldens so public clones/CI pass.
- Docs/audits refreshed to rc.2 across README-level and
  docs/public-release/.

Completed since: `onboarding_trust_tests.py` landed (ALL PASS, in CI);
three independent fresh-context verifiers ran over the candidate. Their
P1 — the release gate itself went red because its scanners matched the
new rc.2 evidence docs/tests/acceptance files once tracked — is FIXED
(scanner allowances + an exec-free test harness) along with every P2
(sandbox input test now refuses to post unless the app is frontmost;
dev-run build identity prefers live git over stale packaging stamps;
Coach base URLs must be https unless localhost) and the actionable P3s
(Windows rows start "untested" instead of asserted green; manual
calibration without real values reads "unset" and blocks; Coach key
migrates to the 0600 secrets file; probe-file readiness check;
release-manifest.json now covered by SHA256SUMS.txt). Gate, onboarding
suite, tour_check and the engine battery re-run ALL PASS at the fix
commit; the final DMG + artifacts were rebuilt from that clean tree
(commit eb9bbba) and the packaged probes re-passed (0 sockets, bridge
live, identity dirty=false).

Still open: Windows runtime still never executed (CI prepared,
unverified); owner actions unchanged — license, secret revocation +
fresh history, public URL, approved example screenshots, signing
credentials.

## 1.0.0-rc.4 — final visible-onboarding pass (2026-07-30)

The owner's manual test of rc.3 reported the visible product gaps this
pass closes: guided calibration still escaped into the normal Calibrate
tab, no sequential progression existed on the permission or calibration
pages, Advanced Cue Matching was optional, the tutorial never began after
setup, and the OS-level icon was still the old artwork. Each gap was
reproduced against the real embedded UI in a scripted DOM probe
(docs/final-visible-pass/REPRODUCTION_REPORT.md), fixed, and locked in by
a permanent jsdom regression suite (`wizard_ui_tests.py` — wired into CI)
plus extended `onboarding_trust_tests.py` / release-gate scans. Highlights:
in-wizard guided detail pages over the one shared calibration service; one
progression engine (`lite_onboarding.progression`) for permissions and
calibration; Advanced Cue Matching required in the registry, readiness and
the classic Start gate (existing installs migrate to a preserved
NEEDS_REVIEW, never silently un-finished); a Python-side tutorial
lifecycle with once-only auto-start after FINISHED; a structured
instruction registry on every guided step; and a deterministic diamond
icon pipeline (`packaging/make_icon.py`) with the stale-icon build guards
removed and gate-tested. Owner actions carried forward unchanged (license,
repo URL, webhook revocation, signing/notarization, Windows CI on a real
runner, calibration example screenshots).

## If interrupted, resume with
```
cd <repo root>
git status --short --untracked-files=no && git log -5 --oneline
python3 tour_check.py && python3 finds_sim.py && python3 studio_tests.py
python3 public_release_tests.py   # once it exists
```
