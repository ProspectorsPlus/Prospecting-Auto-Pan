# Final pre-publish pass — implementation report (1.0.0-rc.6)

Baseline `8bb67db` (rc.5) → the six implementation commits of this pass
plus the documentation/version chunk. Every phase started from a
reproduced defect or a stated product decision in
REPRODUCTION_REPORT.md (same directory). Companion references:
DIAGNOSTIC_ARCHITECTURE.md, RECOMMENDATION_RULES.md,
SETTING_REGISTRY.md, PAN_RIGHT_ENDPOINT_REPORT.md, WINDOWS_PARITY.md.

## Phase 1 — routing, explicit Welcome, Skip Wizard, Fortune River (`c6ef1be`)

- **What**: one startup-routing authority
  (`lite_onboarding.compute_startup_route`, lite_onboarding.py:191-217
  — priority: studio → explicit Welcome → session skip → FINISHED
  (auto-skip / show-welcome / straight in) → auto-skip pref → fresh →
  show-welcome → resume; routes `main`/`welcome`/`wizard_resume` with
  reasons); `welcome_state` exposes `route` +
  `skip_wizard_automatically` and `boot()` acts on it verbatim
  (prospecting_app.py:12366-12380). Explicit Welcome always continues
  into the wizard (issue 1) with the full six-action list. Skip Wizard
  modal (`#skipmodal`) with four honest options (issue 2): session-only
  JS flag (logged, nothing written), `mark_completed_via
  ("marked_complete")` bookkeeping stamp (readiness stays live),
  `SKIP_WIZARD_AUTOMATICALLY` pref (reversible in Welcome / Settings /
  Trust Center), cancel. `fortune_river` is `wizard: False`
  (lite_onboarding.py:439) — out of the wizard entirely (issue 6);
  the Calibrate tab keeps its "(optional, advanced)" section.
- **Verified**: 96-combination routing table + jsdom scenarios E/F in
  `wizard_ui_tests`; `onboarding_trust_tests` welcome_pref child
  extended; tour_check.

## Phase 2 — tutorial lifecycle (`86310cd`)

- **What** (issue 3): `TUTORIAL_AUTO_OPEN` pref (default True,
  engine-routed persistence, error code PP-TUT-AUTO), toggleable in
  four places (tour footer, Settings, Welcome, Trust Center);
  `tutorial_state.json` schema 3 — lifecycle becomes history (last
  outcome, seen_count, last_seen_version; v2 migrates in place);
  `maybeStartTour` opens once per main-app entry (`TUT_ENTRY_SHOWN`,
  reset only by `SETUP.open`), guards against gate/wizard/modals and
  re-checks after its await; real close X (`#tourx`); a skipped wizard
  still gets the tutorial.
- **Verified**: jsdom scenarios B/C/E/F updated + new G (auto_open off
  + manual start + opt-out), Python v2→v3 migration + ACTIVE stamping
  tests, tour_check.

## Phase 3 — pan capacity right end (`46c74da`, the release blocker)

- **What** (issue 5): the silent bad-save path is dead. Full narrative
  with line refs in PAN_RIGHT_ENDPOINT_REPORT.md: `cap_endpoint_guard`
  walks manual picks ≤10 px inward to the one `_solid_gold` predicate
  shared with the auto-detector; pure `validate_cap_pair` (nine named
  reasons); `save_pixels` rejects atomically (writes NOTHING, exact
  reasons) and always rewrites `CAP_BAR_WIDTH` on a validated pair;
  overlay confirm guards first, renders reasons verbatim with Redo
  alive, saves the same-frame left tip on a confirmed-unchanged
  proposal, and takes a one-time `.pre-caprepair.bak`; **Test capacity
  calibration** evaluates a fresh grab with the exact runtime math;
  suspicious stored pairs read `needs_review` (values never modified).
- **Verified**: new `capacity_tests.py` (51 checks, synthetic frames
  incl. the anti-aliased edge, in CI), wizard_ui overlay-rejection
  scenarios, parity golden deliberately regenerated for the additive
  `ok`/`width` fields, full engine battery green.

## Phase 4 — diagnostics engine (`180ec6d`)

- **What** (issue 4, engine half): `lite_diagnostics.py` (2297 lines,
  pure/crash-proof) — event/recommendation models, the 146-entry
  setting registry derived from SECTIONS/RANGES/HELP with curated
  metadata and a bounds-mandatory auto-apply policy, 16 rule families
  over the real closed vocabularies with exact thresholds and
  escalation ladders, merge/recurrence, a suppression store (CRITICAL
  never suppressible), and the 20-entry FAQ knowledge base
  cross-validated against the real registries. Specs pin
  `lite_diagnostics` as a hidden import.
- **Verified**: `diagnostics_tests.py` (185 executed checks, in CI both
  platforms); details in RECOMMENDATION_RULES.md / SETTING_REGISTRY.md.

## Phase 5 — diagnostics host + UI (`e01cd6c`)

- **What** (issue 4, product half): `Api.diagnostics_state` assembles
  real evidence with a 2 s debounce and host-synthesized
  `PP-D-CAL-REQUIRED`; one badge writer (`renderDiagBadges`) with
  red/yellow counts and drawer-opening clicks (no tab switch);
  `#diagdrawer` renders every detail section (observed + evidence,
  cause + confidence wording, first action, exact settings
  current→suggested with Open/Apply/Undo, calibrations, permissions,
  causes/effect/tradeoff/verify, FAQ, copy, dismiss,
  don't-show-again); registry-bounded `diag_apply`/`diag_undo` with
  snapshots in `diagnostics_state.json`; exact deep links
  (`navigateToSetting`/`Calibration`/`Permission`); the FAQ browser
  with six entry points; a new main-tour card for the system.
- **Verified**: jsdom scenario H (43 checks) driven by the REAL
  `evaluate()` output; all suites green; mirror regenerated.

## Phase 6 — Windows parity (`a3b1db2`)

- **What** (issue 7): the silently diverged
  `windows/prospecting_ui.py` (missing rc.3's atomic config write)
  healed; `sync_windows_app.py` now syncs the four verbatim twins;
  `tour_check.py` full-file byte-parity guard (proven to fail on a
  corrupted byte); new `packaging/windows_acceptance.ps1` with honest
  manual checklists, wired into `build-windows.yml`; `WINDOWS_TESTING.md`;
  BUILDING.md Python-version claims corrected. Details in
  WINDOWS_PARITY.md.
- **Verified**: statically only (see honest gaps).

## Phase 7 — documentation + version (this chunk)

- Version 1.0.0-rc.5 → **1.0.0-rc.6** on all three code surfaces
  (prospecting_app.py:49, the regenerated mirror, installer.iss:6) and
  the current-version doc surfaces (README, RELEASING, SUPPORT,
  TRUST_CENTER example, VERIFY_DOWNLOAD example). CHANGELOG rc.6
  section. Updated README / SUPPORT / CALIBRATION_GUIDE / PERMISSIONS /
  TRUST_CENTER / SECURITY / PRIVACY / BUILDING / windows/README.txt.
  New user docs: WELCOME_AND_SETUP.md, TUTORIAL.md, DIAGNOSTICS.md,
  RECOMMENDATIONS.md, FAQ.md. New reports in this directory. Verified:
  tour_check, wizard_ui_tests, diagnostics_tests, capacity_tests,
  public_release_tests all green at the end of the chunk.

## Honest gaps

- **No live Roblox.** No live-game session was part of this pass; the
  capacity fix and the diagnostics are proven against synthetic frames,
  recorded reproductions, and DOM/jsdom suites — not a live game.
- **No real Windows runtime, unsigned.** The Windows runtime has never
  executed anywhere (a green `build-windows.yml` run remains a release
  blocker); `windows_acceptance.ps1` has never run (no PowerShell on
  the dev Mac — not even a syntax check). macOS builds remain unsigned
  and un-notarized; no signing or notarization claim is made.
- **Data-manifest omission (found and fixed in this pass).**
  `diagnostics_state.json` and `tutorial_state.json` were missing from
  `Api._DATA_FILES`; both are now listed in the Trust Center Local
  Data table and removed by "Delete ALL local data".
- **Import path over-reported (found and fixed in this pass).**
  `Api.import_calibration` now propagates the `save_pixels` rejection:
  a rejected import writes nothing and reports the exact reasons
  (PAN_RIGHT_ENDPOINT_REPORT.md).
- `engine_lite_drive.py` keeps its one pre-existing
  environment-dependent failure on this machine (headless overlay
  window; green in CI).
- Publication remains blocked on the standing owner actions: license,
  public repo URL, historical webhook/secret revocation +
  fresh-history decision, signing/notarization credentials, first
  green Windows CI run.
