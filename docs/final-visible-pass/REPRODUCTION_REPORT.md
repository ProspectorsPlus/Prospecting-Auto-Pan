# Final visible-onboarding pass — reproduction report

Baseline: commit `40630d7cfd72ff50de571a749aedeaba15d084a3` (branch
`fable/prospector-engine`, version 1.0.0-rc.3, clean tree). The shipped rc.3
DMG (`dist/ProspectorLite-1.0.0-rc.3-macos-arm64.dmg`, Jul 28 17:09) carries a
`build_info.json` with exactly this commit and `dirty: false`, so every
source-level finding below is byte-identical in the packaged app.

Reproduction method: the real embedded UI (the exact HTML+JS produced by
`build_html()` with an isolated `PP_DATA_DIR`) was driven in jsdom with a faked
pywebview bridge whose response shapes were copied from the real Api methods.
The probe clicks the same buttons a user clicks; every claim below is the
probe's recorded DOM state, cross-checked against the source line that causes
it. The probe evolved into the permanent regression suite
(`wizard_ui_tests.py` / `wizard_ui_tests.js`) added by this pass.

## Issue 1 — guided calibration escapes the wizard into the Calibrate tab

- Steps: first run → Welcome → Continue → Trust step → Continue to
  Calibration → click **Calibrate (guided)** on the Capacity card.
- Actual (probe): `[after-capacity-click] activeTab=cal setupShown=false
  supReturnShown=true calTabModalOpen=flex` — the setup wizard overlay is
  hidden, the main app's **normal Calibration tab is selected**, and the
  Calibrate tab's legacy 7-step modal opens on top. The user has left the
  wizard; only a small floating "← Return to setup" pill remains.
- Expected: a Capacity detail page opens **inside** the wizard; the main tab
  never changes; success returns to the wizard checklist.
- Root cause: `prospecting_app.py:10871-10872` — the `data-cal="wizard"`
  handler is literally
  `SETUP.suspend(); document.querySelector('.tab[data-tab="cal"]').click();
  $id('wizbtn').click()`. The `data-cal="tab"` handler at 10879 does the same
  tab switch for cue-mask/Fortune-River/AutoPan items.
- Where the user lands afterwards: success, cancellation and failure of the
  legacy modal all leave the user parked on the normal Calibration tab with
  the modal's own state; wizard context is lost until they find the pill.
- Affected files: `prospecting_app.py` (renderCal + handlers, 10827-10908),
  `windows/prospecting_app.py` (byte mirror).
- Source run and packaged behavior are identical (same embedded JS; DMG
  commit == HEAD).

## Issue 2 — no sequential progression on the calibration checklist

- Actual (probe, calibration step): 6 cards rendered; `cards with reduced
  opacity: 0`; `has Upcoming label: false`; `has "Do this next" label:
  false`. Status pills show only live value states (`Auto`, `Not set`,
  `Off`) — every card is equally prominent and every action is enabled at
  once.
- Expected: Capacity is the single ACTIVE step; later required steps are
  faded and labelled Upcoming; completing a step activates the next.
- Root cause: no progression engine exists. `renderCal`
  (`prospecting_app.py:10827`) maps registry order straight to cards;
  `lite_onboarding.calibration_status` returns only value states
  (ok/auto/stale/unset/off), never step states. Grep confirms the strings
  `Upcoming` / `Do this next` appear nowhere in the codebase.

## Issue 3 — no sequential progression on the permissions page

- Actual: `renderTrust` (`prospecting_app.py:10801-10823`) renders all
  capability cards simultaneously in three static groups; `capCard`
  (10625-10663) has no state-dependent visibility and the CSS has no
  opacity rules for `.cap-card`. Nothing is faded, nothing is labelled
  Upcoming, nothing activates in sequence.
- Root cause: same as issue 2 — the progression concept does not exist.

## Issue 4 — the global "never requested" list dominates the permission page

- Actual: the wizard permissions step ends with
  `<div class="sup-group">Never requested (so you can see we know)</div>`
  (`prospecting_app.py:10815`) rendering six extra cards (microphone,
  camera, location, admin privileges, full disk access, sound alerts) on the
  first-run page.
- Expected: the wizard page shows only the capabilities being set up, with a
  concise pointer to the Trust Center; the detailed never-requested list
  stays in the Trust Center / PERMISSIONS.md.

## Issue 5 — Advanced Cue Matching is optional everywhere

- Actual: registry entry `cue_masks` has `required: False`, condition
  `ADVANCED_CUES` (`lite_onboarding.py:374-390`); the Calibrate tab labels
  it "Advanced cue matching <span class=advbeta>optional</span>"
  (`prospecting_app.py:7083`); `ADVANCED_CUES` defaults to off
  (`prospector_engine/engine.py:227`); `calibration_ready` ignores optional
  items, so masks never block readiness or `launch()`; a single-pixel-only
  setup reads as fully Ready.
- Expected: Advanced Cue Matching is a required, default-on, first-class
  calibration; single-pixel-only data does not satisfy readiness; existing
  single-pixel users are marked Needs review (values preserved).
- Root cause: the requirement simply was never made; additionally
  `_required_values_present` (`lite_onboarding.py:405-420`) only understands
  `*_PIX`/`*_PIXEL`/`CAP_BAR_WIDTH` keys and the required-item branch of
  `calibration_status` reports `auto` for anything when `AUTO_CALIBRATE` is
  on — both wrong for masks, which auto-calibration cannot place.

## Issue 6 — tutorial starts mid-setup, not after setup, and only once ever

- Steps (probe): click Capacity in the wizard (wizard suspends per issue 1)
  → wait 1 s.
- Actual: `tour visible after wizard suspension (mid-setup): true` and
  `localStorage pp_tour_done: 1`. `SETUP.suspend()` calls `_startApp()`
  (`prospecting_app.py:10992`), which schedules `maybeStartTour` after
  900 ms (10545); `maybeStartTour` (8915) checks only the welcome gate —
  not the setup overlay — so the full 20-step tour opens on top of the
  Calibrate tab while the user is mid-calibration, and `startTour` marks it
  seen **at start** (8855). On any later launch the tutorial therefore
  never auto-starts again; the user's report ("the tutorial does not begin
  after setup") is this sequence observed from the outside.
- Additional fragility: the seen flag lives in WebKit `localStorage` (not
  the app data dir), `seen()` fails closed (returns true when localStorage
  throws, 8781), there is no schema version, and completed vs dismissed is
  not distinguished.
- Expected: the main tutorial starts automatically exactly once per
  tutorial-schema version, after setup genuinely finishes, with state
  persisted atomically in the app's data directory and restartable from the
  Tutorials menu.

## Issue 7 — old cowboy icon everywhere at the OS level

- Actual: root `icon.png` (2048×2048, sha256 26e3b946…) is the old cartoon
  cowboy; `build/icon.icns` (compiled from it, reused on every rebuild via
  the `[ ! -f build/icon.icns ]` guard in `build_dmg.command:72`),
  `windows/icon.png` (byte-identical copy), `windows/icon.ico` (Jun 25,
  same guard in `build.bat`), and the rc.3 DMG/app bundle all carry the
  cowboy. Meanwhile the in-app UI already brands with a diamond (`pp-gem`
  inline SVG ×5 + `docs/favicon.svg`).
- Expected: an original diamond mark as the app icon across macOS bundle,
  Dock, DMG, Windows EXE/installer/shortcuts, with no stale cowboy assets
  in the shipped packages.
- Root-cause notes for the fix: replacing `icon.png` alone changes nothing
  — the stale-icon guards must be cleared (`build/icon.icns`,
  `build/icon.iconset`, `windows/icon.ico`) and `windows/icon.png` copied
  explicitly; `windows/installer.iss` hand-stamps its version.

## Issue 8 — calibration instructions are one-liners

- Actual: each registry item carries a single `instructions` sentence
  (e.g. cap_bar: "Click the RIGHT tip of the pan-fill bar, then the LEFT
  tip…"). There is no structured instruction registry: nothing tells the
  user what to open in Roblox, where to stand, what must be visible, what a
  correct result looks like, common mistakes, retry paths, or what stays
  unavailable when skipped.

## Packaged verification of this baseline

`dist/ProspectorLite-1.0.0-rc.3-macos-arm64.dmg` was verified to be built
from the audited commit (`build_info.json`: commit 40630d7, dirty=false,
version 1.0.0-rc.3). The DOM probe drives the identical embedded UI those
bundles run. The packaged first-boot probes of the prior pass
(`packaging/packaged_acceptance.command`) cover bridge liveness and welcome
lifecycle; they do not exercise the guided-calibration click path, which is
why this defect survived rc.3's acceptance.
