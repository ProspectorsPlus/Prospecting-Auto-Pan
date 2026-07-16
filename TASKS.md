# Prospector Studio — Task checklist

## 1. Schema, model, persistence
- [ ] `STUDIO_BLOCKS` (17 types) + `STUDIO_GROUPS` + `STUDIO_KEY_WHITELIST` in prospecting_ui.py
- [ ] Tagged help text for every block type (no em dashes, no bare &)
- [ ] Param ranges reuse RANGES bounds where a matching key exists
- [ ] `_studio_load/_studio_write` (two-phase) for prospecting_scripts.json
- [ ] `_studio_validate(script)` strict: unknown type, missing/extra params, out-of-range,
      non-finite, depth>16, blocks>500, key whitelist, name rules, human errors naming the block
- [ ] Api: studio_list, studio_get, studio_save, studio_delete, studio_duplicate,
      studio_set_active, studio_rename, studio_validate, studio_templates, studio_meta
- [ ] Api: studio_export (save dialog), studio_import (text), studio_import_dialog (open dialog)
- [ ] Templates: Blank, Standard loop, Treasure (Rubble Creek); all validate clean
- [ ] `studio:<type>` help served through _tutorial_merged()
- [ ] Mirror + protocol green

## 2. Engine interpreter
- [ ] SCRIPT_MODE / SCRIPT_ACTIVE / SCRIPT_JSON globals (load_config plumbing)
- [ ] KEY_SPACE in both platform key tables
- [ ] ScriptRunner: explicit stack walker, one block step per tick, sliced sleeps
- [ ] Handlers for all 17 types driving existing primitives only
- [ ] Pans counted per top-level pass; digs/dig_clicks; clean_cycles; cycle_ms
- [ ] Safety: runtime whitelist, wait clamp 100ms..120s, no-progress watchdog,
      empty-pass guard, per-pass step budget, release_all on abort, exceptions -> safe_stop
- [ ] Esc/Ctrl+K/pause behave exactly like built-in modes
- [ ] Dispatch: TRACKER > SCRIPT > TREASURE > supervisor
- [ ] as_dict "script" label; __SCRIPT__ emit (throttled)
- [ ] Relic timers + notifications + auto-stop still work during a script run
- [ ] Mirror + protocol green

## 3. Studio tab + window
- [ ] nav("studio") after hist + #pstudio panel (explainer, library, buttons)
- [ ] Library: name, desc, block count, last edited, active marker; Run/Stop, Set active,
      Duplicate, Export, Delete (confirm), New, Import, Open Studio
- [ ] STUDIO_HTML window created hidden in main(); open/close Api; app-identity styling
- [ ] _studio_eval + __SCRIPT__ forwarding
- [ ] Mirror + protocol green

## 4. Editor
- [ ] Palette generated from STUDIO_BLOCKS (never hand-copied)
- [ ] Canvas: indented nestable list, group-coloured rails, selection
- [ ] Add via drag AND inline "+"/keyboard; reorder by drag AND Alt+arrows; nest by drop AND
      indent/outdent keys; delete/duplicate per block
- [ ] Inspector: sliders synced to number boxes (range bounds), dropdowns, key/cue pickers,
      live "what this block will do" line, per-block help
- [ ] Undo/redo (cap 100); unsaved-changes protection
- [ ] Live validation inline + problems list (tagged red style); authoritative server check on
      save/run
- [ ] Templates flow (new script from template)
- [ ] Run/Stop from Studio; live validity indicator; running/disabled states
- [ ] Keyboard operability + focus states + aria labels
- [ ] Small-window layout holds up
- [ ] Mirror + protocol green

## 5. Run tab + sharing
- [ ] Run-tab compact selector (built-in modes vs custom scripts) + supersede note
- [ ] History badge with script name
- [ ] .ppscript export/import round-trip identical; auto-rename on clash
- [ ] Attach a script to a .ppbuild (extend existing attachment path)
- [ ] Mirror + protocol green

## 6. Onboarding + help
- [ ] TOUR_DEFAULTS['studio'] (main window) + auto-offer once + Tutorials menu entry +
      Explain this page
- [ ] TOUR_DEFAULTS['studio_editor'] rendered inside the Studio window; auto-offers once
      (app-side flag); builds the Treasure template step by step
- [ ] UI_HELP entries for every Studio control (tab, buttons, selector, library rows)
- [ ] No em dashes / bare & anywhere user-visible
- [ ] Mirror + protocol green

## 7. Tests + verification + packaging
- [ ] studio_tests.py: validator unit tests (good/bad scripts, tamper battery)
- [ ] studio_tests.py: schema<->interpreter drift assertion
- [ ] studio_tests.py: interpreter walk tests (order, repeat, conditionals, timeouts,
      whitelist, watchdog) with stubbed input/detector
- [ ] studio_tests.py: Treasure template integration (simulated detector; dig/strafe/dig/strafe
      order; pans increment)
- [ ] tour_check.py: STUDIO_HTML node --check, studio selectors, studio lockstep regions
- [ ] Full protocol green (both copies)
- [ ] Windows zip rebuilt (14 files) and contents match windows/
- [ ] Manual pass in the running app (launch, build, save, activate, export, delete, re-import,
      restart persistence)
- [ ] README.md + PROJECT_MEMORY.md updated

## 8. Evaluation
- [ ] EVALUATION.md adversarial pass; all meaningful findings fixed; re-run protocol
- [ ] Final report
