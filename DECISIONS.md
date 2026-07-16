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
