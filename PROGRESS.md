# Prospector Studio — Progress

Updated: 2026-07-16 (end of the overnight implementation session)

## Current status: COMPLETE (one owner-only follow-up)
Every mandatory MVP item is implemented, integrated, mirrored and verified. The only open
item is the live-game acceptance pass (Roblox + calibration were not available; the same
path is proven against the deterministic detector + input stubs). See EVALUATION.md.

## What shipped (all mirrored byte-identically into windows/)
- `prospecting_ui.py`: `STUDIO_BLOCKS` (17 block types with params, ranges, icons, tagged
  help), `STUDIO_GROUPS`, `STUDIO_KEY_WHITELIST`, `STUDIO_CONTAINERS`, limits; 5 new UI_HELP
  entries for the Studio tab controls.
- `prospecting_app.py`: script model helpers (`_studio_load/_studio_write/_studio_validate/
  _studio_sanitize/_studio_templates`), 20 `studio_*` Api methods, `prospecting_scripts.json`
  persistence (two-phase writes), `.ppscript` export/import via native dialogs, the
  `STUDIO_HTML` editor window (`_studio_html()` generated from the schema), window creation
  in `main()`, `__SCRIPT__` forwarding, the Studio sidebar tab + library panel, the Run-tab
  Mode selector, History script badges, `TOUR_DEFAULTS['studio']` + `['studio_editor']`,
  build attachments accept `.ppscript`.
- `prospecting_old.py`: `SCRIPT_MODE/SCRIPT_ACTIVE/SCRIPT_JSON` config plumbing, `KEY_SPACE`
  per platform, the CUSTOM SCRIPTS interpreter section (`ScriptRunner`, `script_tick`,
  `_SCRIPT_HANDLERS`, hard safety rails), dispatch Tracker > script > Treasure > supervisor,
  `as_dict` script label.
- `tour_check.py`: STUDIO surface node --check, studio tour selector resolution, six new
  lockstep regions (app x4, engine, ui).
- `studio_tests.py` (dev-only): 60+ checks; validator battery, drift guards, interpreter
  walk tests with stubbed input/detector, Treasure-template integration, abort timing.
- Docs: PRODUCT_SPEC, ARCHITECTURE, IMPLEMENTATION_PLAN, TASKS, DECISIONS, TEST_PLAN,
  EVALUATION, README, PROJECT_MEMORY §23.
- `Prospectors Plus Windows.zip` rebuilt (14 files, byte-matched).

## Verification state (latest run, all green)
- `python3 -m py_compile` x8 ✓
- `python3 tour_check.py` -> RESULT: ALL CHECKS PASS (11 tours / 88 steps; 144 keys; studio
  lockstep regions identical) ✓
- `python3 finds_sim.py` -> ALL SCENARIOS PASS ✓
- `python3 studio_tests.py` -> STUDIO TESTS: ALL PASS ✓
- Headless Api battery (save/list/activate/rename/export/import/tamper/delete) ✓
- Browser-pane interactive pass over the real rendered UI (editor + main window) ✓
- Real app boot: all windows incl. Studio created, clean quit ✓
- App copies diff vs windows/: still exactly the two pre-existing platform hunks ✓

## Key decisions
See DECISIONS.md (30 entries). Highlights: schema single-source in prospecting_ui.py with a
drift-guard test against the engine handler table; one pan = one top-level pass; wait_cue
grew `hold` + `fresh` params (the walk-until-cue and treasure strafe primitives); runtime
whitelist + wait clamps independent of the editor.

## Problems / blockers
None. Owner follow-up: one live Treasure run at Rubble Creek (see EVALUATION.md).

## Next action (if a future session continues)
Nothing mandatory. Enhancement candidates, in value order: live current-block highlight in
the HUD, more sensing blocks (green dig-bar, money/shard reads), variables/counters,
record-actions-into-blocks helper, owner-only palette editing.
