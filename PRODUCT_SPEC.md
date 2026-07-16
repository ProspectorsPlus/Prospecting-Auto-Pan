# Prospector Studio — Product Spec

> Scope: the Studio scripting subsystem only. For everything pre-existing see
> [PROJECT_MEMORY.md](PROJECT_MEMORY.md). Status of each item is tracked in TASKS.md / PROGRESS.md.

## What it is
Prospector Studio is a visual, no-code scripting system inside Prospectors Plus. Users compose
custom farming behaviours ("custom modes") from Prospecting-specific blocks in a dedicated Studio
window, run them through the real engine with the real calibrated detection, and share them as
single `.ppscript` files. The relationship to the macro mirrors "Roblox vs Roblox Studio".

## Who it is for
The invited, mostly non-power users of Prospectors Plus, plus the owner. No coding knowledge is
assumed: visual blocks, plain-English parameter labels, tagged hover-help on everything, inline
validation that names the fix, and a guided tour.

## Problem it solves
Every new farming meta currently requires the developer to design, build, mirror, and release a new
hard-coded mode (Standard, Treasure, Shards, Geodes). Studio turns that into user-composable
scripts that are safe, shareable, and integrated with calibration, detection, stats, and safety.

## MVP definition (acceptance)
A user with zero coding knowledge can:
1. Open the Studio tab -> Studio window, pick the "Treasure (Rubble Creek)" template or rebuild it
   from blocks (dig on the deposit, strafe to the sands until the Collect cue, dig, strafe back,
   repeat).
2. Save it, set it as the active custom mode (from Studio or the Run tab selector).
3. Run it against the real game using the existing calibrated Detector; stop it with Esc or Ctrl+K;
   pause/resume works.
4. See pans/digs counted live on the Run tab; the run is labelled with the script name in History.
5. Export the script as one `.ppscript` file; a friend imports it via the native open dialog
   (auto-renamed on clash, never overwriting).
Both platform copies stay byte-identical for shared code and the full verification protocol passes.

## Feature requirements
### Entry point
- New pinned sidebar tab **Studio** (id `studio`, panel `#pstudio`) placed after `hist`, using the
  existing `nav()` pattern with a matching SVG outline icon.
- The panel shows: a short explainer, the script library (name, description, block count, last
  edited, active marker), and buttons: Open Studio, New script, Import script; per-script
  Run/Stop, Set active, Duplicate, Export, Delete (confirmed).
- **Open Studio** shows a dedicated resizable window (~1200x800) rendering `STUDIO_HTML`, created
  hidden at startup and shown on demand exactly like the analytics window; same palette,
  typography, chips, buttons, callouts as the main window.

### Script model
- A script is data, never code: `{format:"ppscript", version:1, name, description, author,
  created, updated, blocks:[...], settings:{...}}`; blocks are an ordered tree
  `{id, type, params:{...}, children:[...]}` with stable string ids.
- Storage: `prospecting_scripts.json` beside the config (respects `_data_dir()` when frozen),
  read/written only through Api methods, two-phase writes.
- Share: `.ppscript` = `{_ppscript:1, app:"Prospectors Plus", script:{...}}` via native dialogs,
  auto-rename on clash, mirroring the `.ppbuild` flow.
- Strict Python-side schema validation on load, import, save, and before run: unknown types,
  missing/extra params, out-of-range values, nesting depth > 16, > 500 blocks, non-finite numbers
  are rejected with a human error naming the block.

### Block palette (MVP, 17 types)
Actions: Dig click; Shake clicks (count or until-empty, optional hold-W momentum); Hold key
(W/A/S/D); Tap key (whitelist: WASD, 1-9, Shift, Space); Click mouse (optional at calibrated
point: screen centre / Auto Pan button); Wait; Use relic slot; Notify (Discord via `post_webhook`).
Sensing: Wait for cue (Pan / Collect Deposit / Shake; optional key held while waiting; optional
"leave first" fresh matching; timeout with continue-or-safe-stop); Wait for capacity (full/empty,
timeout behaviour); If cue / If capacity (containers); If not (container).
Flow: Repeat forever (implicit top-level loop, not a palette card); Repeat N times (container);
Group (container); Safe stop (message); Comment (no-op).
Every block: tagged hover-help (lead + raise:/lower:/fixes:/pairs:/Note:), params bounded by
`RANGES` where they correspond to an existing key, defaults from existing engine constants.

### Editor (Studio window)
- Left palette (Actions / Sensing / Flow groups), centre canvas (vertical indented nestable list of
  block cards, group-coloured left rails), right inspector (params with slider+number pairs,
  dropdowns, key/cue pickers, live one-line "what this block will do", help).
- Add via drag or inline "+"; reorder by drag; nest by dropping onto containers; select to edit;
  per-block delete/duplicate. Full keyboard path for every drag action (add, move, indent/outdent,
  delete). Visible focus states, aria labels.
- Top bar: editable name/description, Validate, Save, Run, Stop, live validity dot.
- Live client validation + authoritative Python validation on save/run: empty script, empty
  containers, unbounded waits without safe-stop fallback, unreachable blocks after top-level Safe
  stop, non-whitelisted key, out-of-range params. Inline tagged red messages naming the fix.
- Templates: "Standard loop", "Treasure (Rubble Creek)", "Blank". New scripts start from one.
- Undo/redo; unsaved-changes protection on close.

### Interpreter (engine)
- A clearly separated section in `prospecting_old.py` (mirrored) walks the block tree tick-by-tick
  inside the existing supervisor loop, driving ONLY existing input/Detector/stats/recovery
  primitives. Built-in modes remain untouched and default.
- Dispatch: Tracker > custom script > Treasure > supervisor.
- Safety rails independent of scripts: Esc/Ctrl+K always win; pause works; per-tick no-progress
  watchdog (`NO_PROGRESS_SEC`); loop/iteration guards; every Wait has an effective timeout; runtime
  key whitelist; safe stop cannot be bypassed; a bad script can never crash the engine process.
- Emits: reuse `__STATS__` (pans counted one per top-level loop pass, digs per dig block);
  lightweight `__SCRIPT__` emit for the current block. `as_dict` carries the script name; History
  labels the run like TRACKER runs.
- Config plumbing: `SCRIPT_MODE` / `SCRIPT_ACTIVE` / `SCRIPT_JSON` travel through
  `prospecting_config.json` like every other setting.

### Selecting, running, sharing
- Active mode set from the Studio library or a compact Run-tab selector; selecting a custom script
  visibly supersedes the built-in mode toggles with a one-line note. Run/Stop/Pause identical to
  built-in runs.
- Export/import `.ppscript` via native dialogs; scripts can be attached to a `.ppbuild` build
  (extends the existing attachment path).
- Owner-editable block help flows through the existing `tutorial_content.json` override system.

### Onboarding and help
- A `studio` tour in `TOUR_DEFAULTS` auto-offers once on first open of the Studio tab (own
  localStorage flag) and teaches the library; an editor walkthrough inside the Studio window
  (steps also served from `TOUR_DEFAULTS`, flag persisted app-side) builds the Treasure template
  step by step. "Explain this page" affordance; hover-help on every Studio control.

## Explicitly deferred
Free-text scripting language/parser; marketplace/cloud sharing; arbitrary keys outside the
whitelist; any capability to send input to non-game windows.

## Success criteria
See "Definition of done" in the implementation prompt; tracked in TASKS.md, verified in
TEST_PLAN.md, adversarially reviewed in EVALUATION.md.
