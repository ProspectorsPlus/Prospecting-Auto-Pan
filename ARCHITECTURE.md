# Prospector Studio — Architecture

> Scope: Studio only. Pre-existing architecture: [PROJECT_MEMORY.md](PROJECT_MEMORY.md).

## Where each piece lives (no new runtime modules; flat repo + windows/ mirror preserved)

| Piece | File | Notes |
|---|---|---|
| Block schema (single source of truth) | `prospecting_ui.py` -> `STUDIO_BLOCKS`, `STUDIO_GROUPS`, `STUDIO_KEY_WHITELIST` | Types, params (type, label, default, range or choices), palette grouping, per-block tagged help. Imported by the app; asserted against the interpreter by tests. |
| Script validation + persistence + Api | `prospecting_app.py` (STUDIO section in `Api`) | `studio_list/get/save/delete/duplicate/set_active/rename`, `studio_import/import_dialog/export`, `studio_validate`, `studio_templates`, `studio_run/stop`, `studio_meta`. Two-phase writes to `prospecting_scripts.json`. |
| Studio window UI | `prospecting_app.py` -> `STUDIO_HTML` + `_studio_eval()` + window creation in `main()` | Raw triple-quoted HTML/CSS/JS surface like `ANALYTICS_HTML`; created hidden at startup, shown via `open_studio_window()`. Vanilla JS; state in plain objects; persistence via Api. |
| Studio tab (main window) | `prospecting_app.py` -> `build_html()` (`nav("studio", ...)`, panel `#pstudio`) + main-window JS | Library list + Open Studio + import/export/run/set-active; Run-tab script selector. |
| Interpreter | `prospecting_old.py` -> "CUSTOM SCRIPTS (Prospector Studio)" section | `ScriptRunner` class + `script_tick(det)`; hard-coded safety tables (independent of the schema by design, defense in depth). |
| Engine plumbing | `prospecting_old.py` | Globals `SCRIPT_MODE`, `SCRIPT_ACTIVE`, `SCRIPT_JSON` (filled by `load_config()` from the shared config); dispatch in `main()` loop: Tracker > script > Treasure > supervisor; `KEY_SPACE` added to each platform key table. |
| Block help (owner-editable) | `prospecting_ui.py` block defs -> merged into `_tutorial_merged()` help map as `studio:<type>` keys | Same override flow as all other help (`tutorial_content.json`, remote cache). |
| Tours | `prospecting_app.py` -> `TOUR_DEFAULTS['studio']` (main window, Studio tab) and `TOUR_DEFAULTS['studio_editor']` (rendered by the Studio window's own compact tour renderer) | Editor-tour "seen" flag persisted app-side in `prospecting_scripts.json` meta (localStorage is unreliable across html-string windows). |
| Tests | `studio_tests.py` (repo root, dev-only, not shipped in the zip) | Schema/validator/template/interpreter unit + integration tests with stubbed input/detector. |
| Verification | `tour_check.py` extended | STUDIO_HTML scripts node --check (both copies), studio lockstep regions (app + engine + ui), studio tour selector resolution (main html for `studio`, STUDIO_HTML for `studio_editor`). |

## Data contracts

### Script object (persisted, exported, and sent to the engine)
```json
{"format": "ppscript", "version": 1,
 "name": "Treasure (Rubble Creek)", "description": "...", "author": "",
 "created": 1752600000, "updated": 1752600000,
 "blocks": [{"id": "b1", "type": "dig", "params": {"hold_ms": 8}, "children": []}],
 "settings": {}}
```
- `children` present only on container types (`if_cue`, `if_cap`, `if_not`, `repeat`, `group`).
- ids are stable strings unique within the script (editor generates `b<n>`).
- The whole `blocks` list is the body of the implicit top-level "repeat forever" loop.

### prospecting_scripts.json
```json
{"active": "", "scripts": {"<name>": {script}}, "meta": {"editor_tour_seen": false}}
```
Lives next to `prospecting_config.json` (same `_data_dir()` rules when frozen). All writes are
two-phase (tmp file + `os.replace`).

### .ppscript file
```json
{"_ppscript": 1, "app": "Prospector Lite", "script": {script}}
```
Import: parse -> strict validation -> sanitize (drop unknown fields, clamp params, regenerate ids
if broken) -> auto-rename on clash -> save. Never executed; only walked as data.

### Config plumbing (app -> engine)
`studio_set_active(name)` writes into `prospecting_config.json`:
- `SCRIPT_MODE": true`, `"SCRIPT_ACTIVE": "<name>"`, `"SCRIPT_JSON": "<compact json>"`.
Setting the active mode back to built-ins writes `SCRIPT_MODE: false` (name/json cleared). The
engine's `load_config()` picks these up like any other setting because the globals exist in
`prospecting_old.py`. `save_config()` only touches `TYPES` keys, so these survive UI saves.

### Emits (engine -> app)
- `__STATS__` unchanged; `as_dict()` gains `"script": SCRIPT_ACTIVE` when a script runs (like
  `"tracker"`), used for the History badge and Run-tab note.
- `__SCRIPT__ {"id": "<block id>", "n": <loop count>, "label": "<short>"}` at block transitions
  (throttled); the app forwards it to the Studio window for the live block highlight.

## Interpreter design (engine)
- `ScriptRunner` is built once per fresh run (in the `was_running` init path) from `SCRIPT_JSON`;
  a parse/shape failure -> immediate `safe_stop("Custom script could not be loaded ...", hard=True)`
  message and no input ever sent.
- `script_tick(det)` executes at most ONE block step per supervisor-loop tick (containers push
  frames onto an explicit stack; no recursion at runtime). Long sleeps run in <=25 ms slices that
  abort the instant `State.running` flips (Esc / Ctrl+K / pause). `release_all()` on every abort
  path (`finally:`), so keys/mouse can never stay held.
- One completed pass over the top level = one pan: `stats.cycles += 1`, `cycle_ms` sampled,
  `clean_cycles` when the pass had no dirty events, `State.last_progress` refreshed.
- Watchdogs (independent of the script): `NO_PROGRESS_SEC` with no completed block -> safe stop;
  N consecutive top-level passes with zero input/wait activity -> safe stop ("script does
  nothing"); wait timeouts clamped to [100 ms, 120 s]; per-pass and total block-step budgets.
- Runtime key whitelist (`_SCRIPT_KEYS`) maps token -> platform keycode; unknown tokens are
  REJECTED at runtime (safe stop), not just in the editor.
- The runner calls only existing primitives: `mouse_tap`, `key_down/key_up/tap_key`, `click_at`,
  `wait_until`, `det.on_pan/on_deposit/on_shake/capacity_full/pan_empty`, `RelicScheduler._fire`
  -style relic use (via the same slot/click code path), `post_webhook`, `safe_stop`, `emit_event`.

## Studio window UI design
- Three panes (palette / canvas / inspector) with CSS grid; collapses gracefully at small sizes
  (palette and inspector shrink, canvas keeps priority; no overlap).
- Editor state: `S = {script, sel, undo:[], redo:[], dirty}`; every mutation goes through
  `apply(fn)` which snapshots for undo (cap 100), re-renders, re-validates.
- Rendering: canvas rebuilt from the tree each change (small trees; simple and correct).
  Drag-and-drop via HTML5 DnD with insertion markers; keyboard: arrows move selection,
  Alt+arrows move the block, Enter adds from palette, Delete removes.
- Palette metadata (`window.__BLOCKS`) is generated at render time from `STUDIO_BLOCKS` in Python
  and injected as JSON, so the editor can never drift from the schema.
- Help panel: bottom of inspector; same tagged `md()` renderer (deliberately copied into
  STUDIO_HTML; separate JS context) fed from `tutorial_content()` (`studio:<type>` keys).

## Mirroring / lockstep
Every shared edit is applied by scripted two-phase patches (assert `count(old)==1`, build the whole
new text, then write) to BOTH copies with identical bytes; anchors are chosen from regions already
byte-identical between copies. `tour_check.py` gains studio lockstep regions so drift fails the
protocol. The Windows zip is rebuilt whenever `windows/` changes.

## Error handling
- Api methods return `{ok: false, error: "<human message naming the block/fix>"}`; never throw
  into JS.
- Engine: interpreter exceptions are caught per tick -> `safe_stop("Custom script error: ...")`;
  the engine process stays alive and stoppable.
