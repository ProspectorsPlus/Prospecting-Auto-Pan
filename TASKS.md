# Prospector Studio — Task checklist

## 1. Schema, model, persistence
- [x] `STUDIO_BLOCKS` (17 types) + `STUDIO_GROUPS` + `STUDIO_KEY_WHITELIST` in prospecting_ui.py
- [x] Tagged help text for every block type (no em dashes, no bare &)
- [x] Param ranges reuse RANGES bounds where a matching key exists
- [x] `_studio_load/_studio_write` (two-phase) for prospecting_scripts.json
- [x] `_studio_validate(script)` strict: unknown type, missing/extra params, out-of-range,
      non-finite, depth>16, blocks>500, key whitelist, name rules, human errors naming the block
- [x] Api: studio_list, studio_get, studio_save, studio_delete, studio_duplicate,
      studio_set_active, studio_rename, studio_validate, studio_templates, studio_meta
- [x] Api: studio_export (save dialog), studio_import (text), studio_import_dialog (open dialog)
- [x] Templates: Blank, Standard loop, Treasure (Rubble Creek); all validate clean
- [x] `studio:<type>` help served through _tutorial_merged()
- [x] Mirror + protocol green

## 2. Engine interpreter
- [x] SCRIPT_MODE / SCRIPT_ACTIVE / SCRIPT_JSON globals (load_config plumbing)
- [x] KEY_SPACE in both platform key tables
- [x] ScriptRunner: explicit stack walker, one block step per tick, sliced sleeps
- [x] Handlers for all 17 types driving existing primitives only
- [x] Pans counted per top-level pass; digs/dig_clicks; clean_cycles; cycle_ms
- [x] Safety: runtime whitelist, wait clamp 100ms..120s, no-progress watchdog,
      empty-pass guard, per-pass step budget, release_all on abort, exceptions -> safe_stop
- [x] Esc/Ctrl+K/pause behave exactly like built-in modes
- [x] Dispatch: TRACKER > SCRIPT > TREASURE > supervisor
- [x] as_dict "script" label; __SCRIPT__ emit (throttled)
- [x] Relic timers + notifications + auto-stop still work during a script run
- [x] Mirror + protocol green

## 3. Studio tab + window
- [x] nav("studio") after hist + #pstudio panel (explainer, library, buttons)
- [x] Library: name, desc, block count, last edited, active marker; Run/Stop, Set active,
      Duplicate, Export, Delete (confirm), New, Import, Open Studio
- [x] STUDIO_HTML window created hidden in main(); open/close Api; app-identity styling
- [x] _studio_eval + __SCRIPT__ forwarding
- [x] Mirror + protocol green

## 4. Editor
- [x] Palette generated from STUDIO_BLOCKS (never hand-copied)
- [x] Canvas: indented nestable list, group-coloured rails, selection
- [x] Add via drag AND inline "+"/keyboard; reorder by drag AND Alt+arrows; nest by drop AND
      indent/outdent keys; delete/duplicate per block
- [x] Inspector: sliders synced to number boxes (range bounds), dropdowns, key/cue pickers,
      live "what this block will do" line, per-block help
- [x] Undo/redo (cap 100); unsaved-changes protection
- [x] Live validation inline + problems list (tagged red style); authoritative server check on
      save/run
- [x] Templates flow (new script from template)
- [x] Run/Stop from Studio; live validity indicator; running/disabled states
- [x] Keyboard operability + focus states + aria labels
- [x] Small-window layout holds up
- [x] Mirror + protocol green

## 5. Run tab + sharing
- [x] Run-tab compact selector (built-in modes vs custom scripts) + supersede note
- [x] History badge with script name
- [x] .ppscript export/import round-trip identical; auto-rename on clash
- [x] Attach a script to a .ppbuild (extend existing attachment path)
- [x] Mirror + protocol green

## 6. Onboarding + help
- [x] TOUR_DEFAULTS['studio'] (main window) + auto-offer once + Tutorials menu entry +
      Explain this page
- [x] TOUR_DEFAULTS['studio_editor'] rendered inside the Studio window; auto-offers once
      (app-side flag); builds the Treasure template step by step
- [x] UI_HELP entries for every Studio control (tab, buttons, selector, library rows)
- [x] No em dashes / bare & anywhere user-visible
- [x] Mirror + protocol green

## 7. Tests + verification + packaging
- [x] studio_tests.py: validator unit tests (good/bad scripts, tamper battery)
- [x] studio_tests.py: schema<->interpreter drift assertion
- [x] studio_tests.py: interpreter walk tests (order, repeat, conditionals, timeouts,
      whitelist, watchdog) with stubbed input/detector
- [x] studio_tests.py: Treasure template integration (simulated detector; dig/strafe/dig/strafe
      order; pans increment)
- [x] tour_check.py: STUDIO_HTML node --check, studio selectors, studio lockstep regions
- [x] Full protocol green (both copies)
- [x] Windows zip rebuilt (14 files) and contents match windows/
- [x] Manual pass in the running app (launch, build, save, activate, export, delete, re-import,
      restart persistence)
- [x] README.md + PROJECT_MEMORY.md updated

## 8. Evaluation
- [x] EVALUATION.md adversarial pass; all meaningful findings fixed; re-run protocol
- [x] Final report
