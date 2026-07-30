# Calibration polish pass — 1.0.0-rc.5

Driven by the owner's full manual walkthrough of rc.4's guided calibration
(21 screenshots of real capture sessions, including two defects caught in
the act). Final build after the independent-verification fix round:
`dist/ProspectorLite-1.0.0-rc.5-macos-arm64.dmg` (sha256
`200ec151b84054db7d0c9c6a6f1c1e5eb64569028e9ca16b49c842d3559dce89` --
`SHA256SUMS.txt` is the machine-checkable source of truth), commit
`6c2feeb4`, dirty=false; byte copy + manifest + checksums (now covering
every artifact) + SBOM in `release/public-candidate/` (rc.4 record
archived under `release/archive/1.0.0-rc.4/`).

## Root causes

1. **Stale top banner AND the unclickable overlay — one bug.** The
   overlay window is reused across capture sessions. Its region-mode
   script replaced the whole banner's `innerHTML`, destroying the
   `<b id="lab">` element inside it. Every later session's boot then
   threw at `getElementById('lab').textContent = …` inside a swallowed
   try/catch — *after* updating the screenshot but *before* resetting the
   previous session's `prop` (pending proposal) and `cueMode` flags. Net
   effect after any region calibration: the banner froze on the old text
   ("Finds pop-up box · drag a box around it…" — the owner's screenshots
   show it over the Auto Pan and Fast Travel captures), and a leftover
   cue-editor mode or auto-shown stale proposal silently ate every click.
2. **Chained multi-captures.** rc.4's guided flow auto-advanced to the
   next capture of a plan the moment the previous one was confirmed —
   impossible to use when the game state must change between captures
   (the three cue prompts, Auto Pan ON→OFF, Fortune River's five UI
   states).
3. **"Skipped" saved calibrations.** Completed steps were visible but
   inert-looking (a bare Complete chip); nothing showed *what* was saved
   or invited review, so the walk felt like it skipped them.

## Fixes

- The overlay page was rewritten around one session model:
  `overlay_image()` is the single source of truth (image, label, action
  hint, interaction mode, session id from `_overlay_show`); every reload
  rebuilds ALL page state; the banner is only written via `textContent`;
  a session token guards every async return; bridge errors surface in
  the banner; Enter confirms; buttons are single-fire.
- Guided plans carry per-stage prep text and NEVER auto-chain: each
  confirm returns to the wizard's stage card ("Capture 2 of 3 — …go to
  LAND…") with an explicit Start-capture button; cancel/failure keep the
  stage card with a retry.
- Completed steps render a "Saved:" value summary
  (`lite_onboarding.saved_summary`) on the checklist and the detail page,
  with Recalibrate / Test / Next actions; the success panel offers
  "Next: <step>" and "Back to the checklist" instead of auto-navigating.
- The owner's screenshots were sanitized (`packaging/sanitize_examples.py`
  crops the macOS menu bar, dock, personal name and desktop chrome; only
  the relevant Roblox area ships, downscaled) into approved example
  images for all 11 calibratable items; `roblox_window` intentionally
  keeps its honest placeholder. Raw originals are NOT in the repository.

## Independent verification round

Three adversarial fresh-context verifiers audited the pass (58 overlay
probes, 48,000-call saved_summary fuzz, per-pixel image inspection,
DMG/tarball/history sweeps). Verdict: no P0; every claim held except one
P1/P2 cluster -- five example images still carried desktop-chrome slivers
at their bottom edge (the autopan one showed identifiable Dock icon
tops), the report quoted a stale DMG hash, a half-saved legacy region
could fabricate a "Complete" box summary, and an overlay OPEN failure
dropped the stage card. All were fixed in the follow-up commit
(crops re-cut and pixel-scan-verified clean, honest region-pair
completeness, in-place stage retry, plus the verifiers' overlay P3s:
failed-reload banner clearing, confirm-rejection busy latch, mid-save
drag snapshotting, dead-branch removal), the suites re-ran green, and
the DMG was rebuilt from the fixed commit with full checksum coverage.

## Verification

- `wizard_ui_tests.py` grew staged-flow assertions (prep card before any
  capture, no auto-chaining, explicit Start per stage, success-panel
  navigation, saved summaries, Recalibrate/Next on completed steps) and a
  new overlay DOM scenario with twelve regressions: banner follows the
  session after region mode, clicks work after region/cue sessions,
  errors are shown and the page stays clickable, the cue editor never
  leaks, and a delayed stale `overlay_image` cannot repaint a newer
  session. All suites pass at HEAD; the release gate passes; packaged
  acceptance probes pass on the rc.5 DMG.
- Packaged visual check (isolated `PP_DATA_DIR`, read-only mount, seeded
  partially-calibrated user): the Guided Calibration page showed steps
  2-4 Complete with "Saved: Saved point (x, y) · open the step to review
  or recalibrate, or just carry on." + Review/redo buttons, and Advanced
  cue matching highlighted "Do this next" with the honest
  "Not captured yet: DEPOSIT, SHAKE" partial-mask state.
- Honest limits: full end-to-end capture interaction (real overlay clicks
  against live Roblox prompts) and Windows runtime remain human/CI items,
  as before. `engine_lite_drive.py` keeps its one pre-existing
  environment-dependent failure on this machine (headless overlay window;
  green in CI).
