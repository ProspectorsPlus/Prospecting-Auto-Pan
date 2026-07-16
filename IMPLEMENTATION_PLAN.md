# Prospector Studio — Implementation Plan

Order of work (each step ends green: py_compile x8, tour_check, finds_sim, unit tests, mirror):

1. **Schema + validation + persistence (Python).**
   `STUDIO_BLOCKS`/`STUDIO_GROUPS`/`STUDIO_KEY_WHITELIST` + per-block help in `prospecting_ui.py`;
   `_studio_*` helpers + `studio_*` Api methods + templates in `prospecting_app.py`;
   `studio:<type>` help keys merged in `_tutorial_merged()`. Mirror. Unit tests for the validator
   and templates.

2. **Engine interpreter.**
   `SCRIPT_MODE/SCRIPT_ACTIVE/SCRIPT_JSON` globals, `KEY_SPACE` per platform, the
   "CUSTOM SCRIPTS" section (`ScriptRunner`, `script_tick`), dispatch wiring, `as_dict` script
   label, `__SCRIPT__` emit. Mirror. Unit + integration tests with stubbed input/detector
   (Treasure order proof; whitelist rejection; timeout -> safe stop; watchdog; Esc abort).

3. **Studio tab + window shell.**
   `nav("studio", ...)` + `#pstudio` panel + library JS in `build_html()`; `STUDIO_HTML` shell
   (three panes, top bar, styling); window creation in `main()`; `open/close_studio_window`;
   `_studio_eval`; `__SCRIPT__` forwarding. Mirror.

4. **Editor.**
   Palette from `__BLOCKS`; canvas render/select/add/reorder/nest (drag + keyboard); inspector
   param controls (slider+number via ranges, dropdowns, pickers); live plain-English block
   summary; undo/redo; live validation + problems list; unsaved-changes guard; templates flow;
   Save/Validate/Run/Stop wiring. Mirror. Node-based editor logic tests where feasible.

5. **Run-tab selector + History label + sharing.**
   Run-tab active-mode selector + supersede note; History badge; `.ppscript` export/import via
   native dialogs; attach-to-.ppbuild. Mirror.

6. **Onboarding + help.**
   `TOUR_DEFAULTS['studio']` + `TOUR_LIST`/`TAB_TOURS` wiring; `TOUR_DEFAULTS['studio_editor']`
   + compact tour renderer inside STUDIO_HTML + app-side seen flag; UI_HELP entries for every
   Studio control; hover-help coverage. Mirror.

7. **Verification + packaging.**
   Extend `tour_check.py` (STUDIO_HTML scripts, studio selectors, new lockstep regions); run the
   full protocol; rebuild the 14-file Windows zip; launch the app and exercise everything
   manually; small-window + keyboard + empty/error states.

8. **Adversarial evaluation.**
   EVALUATION.md pass against every mandatory MVP item; tampered .ppscript battery; fix and
   repeat until clean.

Milestone commits after steps 2, 4, 7, and the final evaluation.
