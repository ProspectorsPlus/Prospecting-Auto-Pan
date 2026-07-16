# Prospector Studio — Decisions log

Format: decision, why, date (2026-07-16 unless noted).

1. **Schema source of truth in `prospecting_ui.py` (`STUDIO_BLOCKS`).** It is the schema file,
   already imported by the app and byte-identical across copies. The app generates the JS palette
   from it at render time; tests assert the interpreter's handler table matches its type set. The
   engine does NOT import it: the interpreter carries an independent hard safety table
   (whitelist, clamps) by design, so a schema bug can never widen what a script may do.
2. **Scripts persist in `prospecting_scripts.json`** `{active, scripts:{name:script}, meta}`,
   dict-keyed by name exactly like builds; share format `.ppscript` wraps one script
   (`{_ppscript:1, app, script}`), mirroring `.ppbuild`.
3. **Engine plumbing via config keys** `SCRIPT_MODE`/`SCRIPT_ACTIVE`/`SCRIPT_JSON` in
   `prospecting_config.json`: rides the existing `load_config()` path; `save_config()` only
   touches TYPES keys so they survive UI saves. Written only by `studio_set_active`.
4. **Interpreter placement:** a clearly separated "CUSTOM SCRIPTS (Prospector Studio)" section in
   `prospecting_old.py` (+ mirror), NOT a new module: the protocol compiles exactly 8 files and
   the zip ships 14. Modularity via the `ScriptRunner` class.
5. **Dispatch order Tracker > Script > Treasure > supervisor.** Tracker is watch-only and safest;
   an active script supersedes built-in mode toggles (the UI also shows a note).
6. **One pan = one completed top-level pass.** Blocks have no "count a pan" concept a non-coder
   must remember; the implicit repeat-forever loop wrapping is the natural cycle boundary and
   matches how treasure_tick counts (one spot pass = one pan).
7. **`wait_cue` gets `hold` (none/W/A/S/D) and `fresh` (leave-then-find) params.** Walking until
   a cue is THE core Prospecting move (walk_back, treasure strafe); without holding a key while
   waiting the Treasure/Standard rebuilds would be impossible with fixed-duration holds alone.
   `fresh` reproduces treasure_tick's two-phase strafe (cue must drop before it counts again).
8. **`shake` gets `momentum_w` (hold W while shaking).** Reproduces SHAKE_MOMENTUM_W glide; the
   standard cycle cannot be rebuilt faithfully without it.
9. **Key whitelist:** W/A/S/D, digits 1-9, Shift, Space. `KEY_SPACE` added to each platform's
   (already divergent) key table so the shared interpreter stays byte-identical.
10. **Blocking-but-sliced execution.** One block step per supervisor tick; sleeps run in <=25 ms
    slices checking `State.running`, so Esc/Ctrl+K/pause abort instantly (same feel as built-in
    modes; treasure_tick already blocks per tick). `finally: release_all()` on every abort.
11. **Wait timeouts clamped to [100 ms, 120 s] at runtime** regardless of what the editor saved;
    "no timeout" cannot exist. Editor validation additionally warns when on-timeout is
    "keep going" everywhere (infinite-hang guard is the clamp itself).
12. **Notify block uses `post_webhook("script", ...)`,** honouring WEBHOOK_ENABLED and the user's
    URL exactly like every other notification; no new delivery path.
13. **Studio window reuses the main `Api`** (same js_api instance) like analytics/coach; created
    hidden at startup, shown on demand; hidden (not destroyed) on close via `_hide_on_close`.
14. **Editor is an indented nestable block LIST,** not free node wiring: clearer for non-coders,
    keyboard-accessible, and always a valid tree.
15. **Editor-tour seen flag persists app-side** (`prospecting_scripts.json` meta) because
    localStorage in secondary html-string pywebview windows is not reliably persistent/shared.
16. **`studio_tests.py` stays dev-only** (repo root, not in the zip, not compiled by the protocol
    list) so the shipped file set is unchanged.
17. **Tampered imports are sanitized then re-validated;** unknown fields dropped, ids
    regenerated when missing/duplicated, params clamped ONLY on import-sanitize (save/run
    validation rejects instead, so the editor never silently changes what the user typed).
18. **Script name rules:** 1..60 chars, printable, no leading/trailing space; auto-rename on
    import/duplicate clash with " (2)" suffixes like builds.
19. **`__SCRIPT__` emit is throttled** (only on block change) and print-only, mirroring
    `__GEODE__`; the app forwards it to the Studio window; HUD untouched for MVP.
20. **Deleting the active script deactivates script mode** (config flipped back to built-ins) so
    the engine can never be pointed at a ghost script; the Run tab selector reflects it.
21. **Validation on save is strict but non-blocking for SAVING drafts** with problems EXCEPT
    structural/schema errors (unknown type, bad shape): those cannot be represented by the
    editor anyway and always reject. Problems that only affect runnability (empty script, empty
    container) save fine but block RUN and set-active, with the problems list shown. Users must
    never lose work because a draft is incomplete.
22. **Running from Studio saves first** (script + set active), then launches through the exact
    same `launch()` path as the Start button, so hotkeys, stats, history, HUD behave identically.
23. **Block param types are int/bool/choice only** (choices as dropdowns); no free-text params
    except comment/notify text and the script name/description; text fields are length-capped
    and rendered escaped everywhere (no HTML injection from imported scripts).
24. **The interpreter treats `State.want_reset` like the supervisor:** consumed at tick start,
    resets the walker to the top with counters cleared (safe-pause retry semantics preserved).
25. **Recovery ladder in script mode is intentionally the simple rungs** (wait-timeout behaviour,
    no-progress watchdog, safe stop with retry): nudges/break-outs assume the standard
    water-land geometry, which a custom script may not have. Documented in help; safe stop with
    retry remains, so overnight resilience is kept. Recorded as a known, deliberate limitation.
26. **The Run-tab selector writes through `studio_set_active`** (single code path for
    activation); the Modes tour text still describes built-ins, with the note added where modes
    are superseded.
27. **prospecting_scripts.json is NOT git-tracked** (user data, like builds); the tracked default
    stays absent so shipped zips carry no personal scripts. Templates ship in code.
28. **Studio icon** is a wrench-in-hexagon outline SVG matching the 1.7 stroke icon set.
29. **`click` block `at` choices:** none (current cursor), centre (calibrated screen centre via
    the live window rect like FR recovery), autopan (AUTOPAN_BTN_PIXEL). Centre uses
    `find_roblox_rect()` at run time; falls back to current cursor with a log line when no
    window is found (never clicks outside the game window rect).
30. **Windows-frozen script path:** `SCRIPT_JSON` travels inside the config file itself, so the
    frozen engine needs no new file access; `prospecting_scripts.json` lives in `_data_dir()`
    for the app only.

## V1 review board round (2026-07-16, same session)

31. **Normalize-then-validate at every load boundary.** Missing params/fields in stored or
    imported scripts are filled with schema defaults on read (`_studio_normalize`); the
    editor always writes complete data, so SAVING stays strict. Adding a block param never
    needs a migration; `STUDIO_SCHEMA_VERSION` only gates files from genuinely newer apps
    (refused at import with a clear message). Stored data is never mutated in place.
32. **Rolling `.bak` on every scripts-file write + fallback on read.** A crash mid-write, a
    truncated file, or a bad hand-edit can lose at most the very last change, never the
    library. Config writes on the Studio-owned path became two-phase for the same reason.
33. **`studio_list` cached against the scripts-file stamp** (mtime+size), invalidated by
    every write. Big libraries stop re-validating on each tab click.
34. **Selection is a light path.** Clicking or arrow-navigating swaps the `.sel` class and
    re-renders only the inspector; the canvas DOM is rebuilt only on structural changes.
    Measured at the 500-block cap: selection went from ~310 ms to ~45 ms.
35. **`content-visibility:auto` on block cards.** Offscreen blocks skip layout, cutting
    structural edits at the cap from ~330 ms to ~80-100 ms; progressive enhancement (older
    WebKit simply ignores it). All mutation paths render exactly once (double renders
    removed by moving selection updates inside `apply`).
36. **Workflow surface: quick-insert (/) with fuzzy filter, block context menu, copy/paste
    of whole subtrees, palette filter.** Pasted trees are re-clamped and re-idd client-side
    (`sanitizeTree`) and still gated by the Python validator on save/run. Multi-select,
    favorites, recents and parameter presets were REJECTED for v1: with 17 block types and
    scripts under a few dozen blocks they add surface without saving real effort.
37. **Worst-case lap estimate in the top bar** (`lap <= Ns`), computed from the same params
    the interpreter clamps; declared "worst case" because cue waits can finish early. A full
    dry-run simulator was rejected: the estimate plus the live block highlight covers the
    real need at a fraction of the complexity.
38. **The 500-block / 16-depth caps stay.** At the cap the interpreter costs 0.8 us per
    tick (measured, stubbed I/O) and the editor stays responsive; raising the cap without a
    virtualized canvas would sacrifice the guarantees for a use case that does not exist.
39. **Deliberately NOT extracted:** the duplicated md() help renderer (main window vs
    Studio window). Sharing it means surgery inside the tour-js lockstep region for a
    cosmetic drift risk; revisit only if the tagged-help grammar changes.
