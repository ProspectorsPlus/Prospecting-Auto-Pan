# Final visible-onboarding pass — acceptance matrix (1.0.0-rc.4)

Build under test (final, after the independent-verification fix round):
`dist/ProspectorLite-1.0.0-rc.4-macos-arm64.dmg`
(sha256 `a641abc4f64611d7746a7012f24008075d3258d2680bedbd9773298ccc7bb9a9`),
built by `./build_dmg.command` from commit
`e0501341114c2ffddfc438f53b2913217aece8a2` with `dirty: false`
(`build/build_info.json` and the bundled copy agree). The same DMG (byte
copy) is in `release/public-candidate/` with manifest + checksums.

The PACKAGED-VISUAL journey below was walked against the FIRST rc.4 build
(commit 906a58b, sha256 d84c1d2d…); the fix round (906a58b → e050134)
changed the deferred-refresh guard, tutorial flag plumbing, permission
button disabling, readiness cue-row derivation and copy/doc wording — each
covered by the SCRIPTED suites re-run green against e050134, and the
packaged probes (bridge, offline, identity, welcome lifecycle) were re-run
against the final DMG (`ACCEPTANCE PROBES: ALL PASS`,
`v1.0.0-rc.4 @ e0501341114c dirty=False`). The independent verification
round and its outcomes are recorded in IMPLEMENTATION_REPORT.md.

Legend: **SCRIPTED** = asserted by a committed, re-runnable suite;
**PACKAGED-PROBE** = asserted against the mounted DMG by
`packaging/packaged_acceptance.command` or the recorded probe commands;
**PACKAGED-VISUAL** = observed in this session on screen in the packaged
app launched from the read-only mount with an isolated `PP_DATA_DIR`
(screenshots kept out of the repository — they show the owner's desktop);
**HONEST-GAP** = not verified in this pass, stated as such.

| # | Claim | Evidence |
|---|-------|----------|
| 1 | Capacity opens inside the wizard; no guided action selects the Calibrate tab | SCRIPTED `wizard_ui_tests.py` (insideWizard asserted at every stage) + PACKAGED-VISUAL: Guided Calibration page rendered inside the setup overlay, wizard rail on top, no tab switch |
| 2 | Guided + normal calibration share one service and one store | SCRIPTED: same bridge methods asserted (`wizard_propose`/overlay calls with `guided_setup` context); code: the context affects only `__calDone` navigation (`prospecting_app.py::_cal_done_notify`) |
| 3 | Success returns to the checklist and activates the next step | SCRIPTED: cap_bar COMPLETE → pan_prompt "Do this next" after simulated confirms |
| 4 | Failure and cancel stay on the guided detail page | SCRIPTED (both paths + stale/foreign-result guards) |
| 5 | Sequential progression on calibration: Capacity first ACTIVE, later faded "Upcoming" | SCRIPTED + PACKAGED-VISUAL: step 1 "Pan capacity bar" highlighted "Do this next"; steps 2-3 faded with "Upcoming: Complete Pan capacity bar (right + left ends) first." |
| 6 | Sequential progression on permissions; completion activates the next; settings-open ≠ complete | SCRIPTED (grant flip test) + PACKAGED-VISUAL: numbered Required cards with chips ("Complete" on this all-granted machine — live TCC state); completion logic reads preflights + session tests only |
| 7 | Global never-requested list removed from the permission page; Trust Center keeps detail | SCRIPTED (absence + pointer asserted) + PACKAGED-VISUAL (page shows only required/optional cards + concise pointer) |
| 8 | Advanced Cue Matching required in registry / readiness / launch gate; single-pixel-only ≠ ready | SCRIPTED (`wizard_ui_tests.py` python layer, `onboarding_trust_tests.py` t_cal_registry, launch drive suites) + PACKAGED-VISUAL: Readiness shows FAIL "Advanced cue matching — Required: capture the PAN, DEPOSIT, SHAKE prompt masks…" with Fix now |
| 9 | Single-pixel installs that finished setup migrate to NEEDS_REVIEW, values preserved, backup written | SCRIPTED (status + progression + preservation asserted; backup path `prospecting_config.json.pre-advcue.bak` written once by `_advcue_migration_backup`) |
| 10 | Mask validation uses the real detector math | `Sensing.cue_check` mirrors `Detector._cue_mask_match` (ratio re-placement, 2 px drift refusal, 85% threshold, background-white warning); exposed as `cue_mask_check`, used by the detail page Test button |
| 11 | Every required item has complete structured instructions; Capacity exact; no vague text | SCRIPTED (all schema fields asserted per item; "RIGHT tip"/">20 px" asserted; vague-phrase blacklist) |
| 12 | Missing example images never crash; honest placeholder | SCRIPTED (placeholder path exercised in every detail render) + code: approved-manifest-only images |
| 13 | Tutorial auto-starts once after real setup completion; never during setup; completion/dismissal persists; manual replay | SCRIPTED (3 scenarios incl. legacy-flag migration) + PACKAGED-VISUAL: tour auto-opened after FINISHED (21 steps), `tutorial_state.json` = ACTIVE; relaunch showed **no** restart |
| 14 | Tutorial targets current controls; no stale branding/access-code content | SCRIPTED `tour_check.py` (every sel/tab resolves, both copies byte-lockstep) + content audit (docs/final-visible-pass/REPRODUCTION_REPORT.md issue 6 notes) |
| 15 | Welcome preference persists; welcome never re-runs setup; post-setup actions offered | PACKAGED-PROBE (probe 5 first/second-boot pref lifecycle) + SCRIPTED (welActions display) |
| 16 | Diamond icon in the macOS package; no old icon anywhere in the pipeline | PACKAGED-PROBE: bundle `Resources/icon.icns` md5 == freshly compiled diamond icns (`bc486448…`), `CFBundleIconFile=icon.icns`, bundled `icon.png` sha ≠ legacy `26e3b946…`; gate `scan_icons` (sizes, SVG master, no stale guards, spec/installer references, legacy-hash ban); the icns decompiles to the diamond artwork |
| 17 | Diamond icon in Windows packaging | `windows/icon.png` + `windows/icon.ico` regenerated from the same geometry; `prospecting.spec` + `installer.iss` reference them; `build.bat`/CI regenerate unconditionally. HONEST-GAP: no Windows runtime/packaging execution in this pass |
| 18 | rc.4 version agreement everywhere | Gate `scan_version` (app, windows mirror, installer.iss) + probe 4 (`v1.0.0-rc.4 @ 906a58b25865 dirty=False`) + release-manifest.json |
| 19 | Checksums, SBOM, package manifest, release manifest agree | `release/public-candidate/`: manifest generated before SHA256SUMS; `shasum -c` verified; SBOM from the build venv (cyclonedx 7.3.1); package-manifest regenerated from the mounted rc.4 DMG |
| 20 | Offline: zero sockets during packaged boot; no bundle writes | PACKAGED-PROBE (lsof = 0 sockets; app runs from a read-only mount with writes only in `PP_DATA_DIR`) |
| 21 | Prior suites all pass | This session: public_release_tests, onboarding_trust_tests, wizard_ui_tests, tour_check, finds_sim, studio_tests, studio_conformance, prospecting_selftest, engine contract/parity/characterization/plan/trace/flow/pacing/parallel — ALL PASS. engine_lite_drive: 1 pre-existing environment-dependent failure (headless overlay-window creation) reproduced identically at the rc.3 baseline commit on this machine — not a regression of this pass; green in CI's environment |
| 22 | Parity golden regeneration was deliberate | One line changed (`cueStatus.advanced` false→true, the intended default flip); regenerated via `--update`; CHANGELOG records it |

## Packaged visible journey (this session)

Every step below ran against the mounted read-only rc.4 DMG with a fresh
isolated `PP_DATA_DIR`; "seen" means observed on screen.

1. Launch → Welcome seen: diamond mark, "v1.0.0-rc.4 · build 906a58b25865",
   checkbox ON, Continue focused.
2. Trust step seen (state seeded TRUST_STARTED): numbered Required cards
   with chips, order/progress summary, no never-requested list, per-card
   privacy facts intact.
3. Guided Calibration seen (TRUST_COMPLETE): prerequisite banner with live
   "Roblox window found", step 1 Capacity ACTIVE/"Do this next"
   highlighted, steps 2+ faded "Upcoming" naming the gating step, footer
   "Do this next: Pan capacity bar (right + left ends)".
4. Readiness seen (CALIBRATION_COMPLETE): real permission PASS rows,
   Required-calibration FAIL naming `cue_masks`, dedicated Advanced cue
   matching FAIL with Fix now, build identity row.
5. FINISHED → the app entered and the main tutorial auto-started
   (21 steps incl. the new Trust Center card); `tutorial_state.json`
   recorded ACTIVE.
6. Quit + relaunch → no tutorial restart; Run page seen directly.
7. Bundle icon verified byte-identical to the diamond icns; DMG detached
   cleanly; the isolated home was the only place any file was written.

Human-journey items intentionally not claimed from this session: physically
clicking through a full real calibration against live Roblox prompts
(requires the game's prompts on screen at the right moments), the OS
permission prompts on a virgin machine (this machine already grants TCC to
the app identity), and the Windows runtime. These carry forward on the
owner checklist unchanged.
