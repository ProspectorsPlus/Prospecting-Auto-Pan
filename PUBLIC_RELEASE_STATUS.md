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

## 1.0.0-rc.5 — calibration polish pass (2026-07-30)

The owner's full manual walkthrough of rc.4's guided calibration (with
screenshots) surfaced four defects; all reproduced, root-caused, fixed
and regression-locked (docs/final-visible-pass/CALIBRATION_POLISH_RC5.md):
the overlay's stale banner and its unclickable states were ONE bug (the
region mode destroyed the banner element, poisoning every later session's
state reset -- the overlay page now rebuilds everything from a
sequence-guarded overlay_image() single source of truth); multi-capture
calibrations no longer chain (per-stage prep cards with explicit Start,
covering advanced cues, Auto Pan, Fortune River, capacity tips); saved
calibrations walk with "Saved:" value summaries + Recalibrate/Next; and
the owner's screenshots were sanitized (menu bar/dock/name cropped) into
approved in-app examples for all 11 items via
packaging/sanitize_examples.py. Owner actions carried forward unchanged.

## 1.0.0-rc.6 — final pre-publish pass (2026-07-30)

Seven issues reproduced against the real code first
(docs/final-prepublish/REPRODUCTION_REPORT.md: probes on the real
save_pixels and the real build_html page), then fixed across six
commits (docs/final-prepublish/IMPLEMENTATION_REPORT.md): explicit
Welcome always continues into the wizard via one routing authority
(compute_startup_route, 96 combinations tested); a real Skip Wizard
with four honestly-persisted options (session flag / marked_complete
stamp / SKIP_WIZARD_AUTOMATICALLY pref / cancel — no option weakens
the Start gates); the tutorial auto-opens on every main-app entry with
an X and an honest opt-out (TUTORIAL_AUTO_OPEN, tutorial_state schema
3); the pan-capacity right-end release blocker is dead (endpoint guard
to solid gold, pure pair validation with named reasons, write-nothing
rejection, always-rewritten width, same-frame pair save, Test capacity
calibration with the exact runtime math, needs_review migration that
never modifies values); a full local diagnostic/recommendation system
(lite_diagnostics: 16 rule families, 146-entry bounded setting
registry, 20-entry FAQ; drawer with exact-setting deep links,
clamped one-key Apply/Undo, per-code suppression never for CRITICAL);
Fortune River out of onboarding (Calibrate-tab section kept, relabelled
optional/advanced); and the Windows mirror healed (prospecting_ui
atomic-write divergence) + guarded (sync covers the four twins,
tour_check full-file byte parity) + windows_acceptance.ps1 (honest:
never yet executed — no PowerShell on this Mac). New suites in CI:
capacity_tests.py (51 checks), diagnostics_tests.py (185 checks).
Version bumped to 1.0.0-rc.6 on all three surfaces; new user docs
(WELCOME_AND_SETUP, TUTORIAL, DIAGNOSTICS, RECOMMENDATIONS, FAQ) and
five engineering reports under docs/final-prepublish/. Gaps found by
the doc-grounding pass were fixed in the same pass
(diagnostics_state.json/tutorial_state.json added to the data
manifest; import_calibration propagates the save_pixels rejection;
one cap_endpoints error code).
Owner actions carried forward unchanged (license, repo URL, webhook +
bot-secret revocation + fresh-history publish, signing/notarization
credentials, first green Windows CI run).

### Verification round + final rc.6 build (2026-07-30)

An independent fresh-context verifier executed every suite, reran the
rc.5 capacity reproduction against HEAD (rejected atomically, config
byte-intact), ran four adversarial probes of its own, and byte-flip
tested the mirror guard. Findings, all fixed in c86ab3c: P1 — the
data manifest / Delete-ALL missed onboarding.log(.1),
coach_history.json and prospecting_calib_log.csv (PRIVACY.md claims
now true); P2 — a diagnostics deep link to a never-visited section
tab was yanked away by the first-visit tab tour (deep links now
suppress it; jsdom regression added); P3 — bound-pinned suggestions
offered a no-op Apply (now open-only); P3 — Settings-page preference
checkboxes went stale across surfaces (re-sync on tab open). No P0.
One more real fix from the integration battery (7ebdcc0): source-run
identity no longer trusts a stale build_info.json version stamp.

Final build (commit c86ab3c, dirty=false, ad-hoc signed, not
notarized):

- `ProspectorLite-1.0.0-rc.6-macos-arm64.dmg`
  sha256 `782d428a68da0de5e12310464854fac9110835399e7eb17a3a7bdc513ff8e030`
- `prospector-lite-1.0.0-rc.6-source.tar.gz`
  sha256 `020f5bd2eb02b338c5c4074eda88ee98576ffccd900da36cd57309fe4c888c06`

Packaged acceptance: ALL PASS on this exact DMG (first-boot probe
window widened to cover cold Gatekeeper verification of a brand-new
unsigned bundle — measured >45 s cold, 3-5 s warm). Scripted packaged
journey against the mounted read-only image with isolated data:
fresh boot routes to welcome; auto-skip on an unfinished install
routes to main with setup_needed honest; finished + pref-off routes
to main; finished + pref-on routes to welcome; bundle identity
1.0.0-rc.6/c86ab3c/dirty=false. Full engine battery green except the
one pre-existing environment-dependent engine_lite_drive failure
(headless overlay real-grab; unchanged since rc.3, green in CI).
No live Roblox session and no Windows runtime execution in this pass.

## If interrupted, resume with
```
cd <repo root>
git status --short --untracked-files=no && git log -5 --oneline
python3 tour_check.py && python3 finds_sim.py && python3 studio_tests.py
python3 public_release_tests.py   # once it exists
```
