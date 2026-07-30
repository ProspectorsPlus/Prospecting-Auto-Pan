# Final visible-onboarding pass — implementation report (1.0.0-rc.4)

Baseline `40630d7` (rc.3) → implementation commits this pass. Companion
documents: REPRODUCTION_REPORT.md (the defects, reproduced against the
real embedded UI), ACCEPTANCE_MATRIX.md (claim-by-claim evidence for the
rc.4 DMG).

## Architecture delivered

```
             Shared Calibration Service (prospector_engine/sensing.py
             + the Api overlay methods -- ONE store, ONE save path)
                   ├── Guided Setup Calibration UI  (in-wizard checklist
                   │    + per-item detail pages; context "guided_setup")
                   └── Normal Calibration Tab UI    (maintenance surface;
                        context "normal_calibration")
```

The context is carried by the overlay entry points
(`wizard_propose` / `start_overlay_calibrate` / `start_overlay_region` /
`start_cue_mask_capture`) and echoed back by `overlay_confirm` /
`overlay_cancel` through one new window-global callback
(`__calDone {ctx, key, ok, cancelled}`). It affects navigation only —
both surfaces run identical calibration semantics on identical values.

## Change inventory

- `lite_onboarding.py`
  - `cue_masks` became a REQUIRED registry item (dependencies:
    pan/deposit/shake prompts; CALIBRATION_SCHEMA bumped to 2).
  - `cue_masks_state()` (per-cue completeness), `calibration_status`
    gained `setup_finished` and the `needs_review` status; the item can
    never report `auto` and honours the `ADVANCED_CUES` switch.
  - `progression()` — the one sequential-progression engine
    (COMPLETE/ACTIVE/UPCOMING/OPTIONAL/BLOCKED/NEEDS_REVIEW, reasons,
    summary) used by calibration and (in JS mirror form) permissions.
  - `CALIBRATION_INSTRUCTIONS` — the structured instruction registry.
  - `compose_registry()` — the full guided-calibration payload, shared by
    `Api.calibration_registry` and the DOM test suite.
- `prospecting_app.py` (mirrored to `windows/prospecting_app.py` via
  `packaging/sync_windows_app.py`)
  - Python: registry delegation + one-time advanced-cue migration backup;
    `_cal_done_notify`; `cue_mask_check`; readiness row "Advanced cue
    matching"; tutorial lifecycle (`tutorial_state.json`, schema 2,
    `tutorial_state`/`tutorial_mark`); rc.4 VERSION.
  - Wizard JS: `renderCal` rebuilt as checklist + `renderCalDetail`
    per-item pages with the required nav states; the Calibrate-tab escape
    handlers (`SETUP.suspend + .tab[data-tab="cal"].click + #wizbtn.click`)
    are GONE; `renderTrust` renders progression and drops the
    never-requested group for a concise Trust-Center pointer; readiness
    Fix now deep-links to guided detail pages; `supNext` finish triggers
    the once-only tutorial check.
  - Tour JS: main-tutorial lifecycle moved server-side; `maybeStartTour`
    gates on gate/setup-overlay/onboarding-FINISHED/NOT_STARTED and
    migrates the legacy localStorage flag; per-tab mini-tours refuse to
    fire over the setup overlay; `main.cal` step rewritten for the new
    architecture; new `main.trust` step.
  - Welcome: post-setup actions (Review setup / Start tutorial / Trust
    Center); Calibrate tab's advanced-cue block relabelled "required".
- `prospector_engine/engine.py`: `ADVANCED_CUES` default ON (no behavior
  change without masks — box fallback identical; parity golden
  regenerated deliberately).
- `prospector_engine/sensing.py`: `cue_check()` (run-time-math mask
  validation); default-ON reads for `ADVANCED_CUES`.
- Icon: `packaging/make_icon.py` (deterministic diamond → SVG master,
  9 PNG sizes, root icon.png, windows png+ico, iconset+icns);
  `build_dmg.command` and `windows/build.bat` regenerate compiled icons
  unconditionally (stale guards removed; dmg build now FAILS if the icns
  is missing).
- Tests: `wizard_ui_tests.py` + `wizard_ui_tests.js` (new, in CI);
  `onboarding_trust_tests.py` t_cal_registry updated for the requirement;
  `public_release_tests.py` gained `scan_icons`; `studio_tests.py` and
  `engine_lite_drive.py` seed masks where they drive `launch()`;
  `package.json`/`package-lock.json` now tracked (jsdom pin).
- Docs/release: CHANGELOG 1.0.0-rc.4; README/SUPPORT/TRUST_CENTER/
  VERIFY_DOWNLOAD/RELEASING at rc.4; PUBLIC_RELEASE_STATUS/READINESS
  entries; `release/public-candidate/` rebuilt for rc.4 (rc.3 archived to
  `release/archive/1.0.0-rc.3/`).

## Deliberate deviations from the specification's suggested layout

- The guided detail page's **Reset** control is delivered only for
  Advanced cue matching ("Clear captured masks", the one place a clean
  clear semantics exists via `clear_cue_mask`). Pixel/region items have no
  destructive reset: re-running Start replaces the saved value atomically,
  and zeroing a pixel via the shared save path would flip
  `AUTO_CALIBRATE` off as a side effect — a reset that can leave a user
  worse off than a redo. Recalibrate-to-replace is the supported path.
- The DMG **volume** keeps the standard disk icon; the diamond is the app
  bundle's icon (Dock, Finder, window). Release wording says so exactly.

## Independent verification round (post-implementation)

A six-lens adversarial fresh-context verification ran against the first
rc.4 build (906a58b). It confirmed containment, the cue requirement, the
migration, icon/packaging integrity and documentation honesty — and found
one P0 and two P1s, all fixed and re-verified in the follow-up build:

- **P0**: the bridge fires `__calRefresh` before `__calDone`, and the
  deferred checklist re-render could replace a guided detail page 600 ms
  after a capture (aborting multi-stage plans and wiping failure states).
  Fixed by making the deferred refresh yield while a detail page owns the
  surface; the DOM suite now replicates the real event order + latency and
  asserts detail-page persistence through the refresh window.
- **P1**: the new main tour wrote the legacy `pp_tour_done` flag that the
  migration branch reads, so a first-run race could record a fabricated
  "legacy migration" and a future schema bump could never re-offer the
  tutorial. Fixed: schema-scoped flag key, legacy key consumed on
  migration, post-await gate re-checks, single-flight, and
  `tutorial_mark` now refuses NOT_STARTED and out-of-order legacy writes.
- **P1**: UPCOMING permission cards were only CSS-disabled
  (`pointer-events:none`), so keyboard users could activate them. Fixed
  with attribute-level `disabled aria-disabled` (matching the calibration
  checklist), plus the optional-card chip-stripping refresh mismatch.
- P2/P3 sweep: readiness cue row now derives from the same status as the
  aggregate (no contradictions on stale windows), `cue_masks_state`
  requires placeable masks (bits + 4-part ratio + positive w/h), stale
  guided results are also key-matched, the legacy Windows ICO hash is
  gate-banned alongside the PNG, `studio_conformance.py`'s stale
  "not shipped" docstring was corrected, failure copy names the real
  button, and the launch-gate comment reflects the mask requirement.

## Honest limits

- `engine_lite_drive.py` has one environment-dependent failure on this
  machine (headless calibration-overlay window creation) that reproduces
  identically at the rc.3 baseline commit — pre-existing, green in CI.
- Windows: source mirror, spec, installer, icons and workflows are
  updated and statically validated (YAML parse, lockstep checks, version
  agreement); **no Windows runtime executed**. Run
  `.github/workflows/build-windows.yml` (workflow_dispatch) or
  `windows\build.bat` on a real Windows machine before claiming Windows
  readiness.
- Calibration example screenshots remain owner-owed; every guided page
  shows the honest "not yet available" note and is complete without them.
- Publication remains blocked on the standing owner actions: license,
  public repo URL, historical webhook/secret revocation + fresh-history
  decision, signing/notarization credentials, first green Windows CI run.
