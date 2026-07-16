# Prospector Studio — Adversarial evaluation

## Round 1 (2026-07-16, after the first complete implementation)

Method: unit + integration suite (`studio_tests.py`), headless Api battery against the real
Python layer, full interactive browser-pane pass over the rendered STUDIO_HTML and the main
window (real code, pywebview api shimmed), real app boot, protocol runs, and a line-by-line
review against every mandatory MVP item.

### Defects found and fixed
1. **Interpreter skipped the sibling after any entered container.** The walker advanced the
   parent frame both at push time and again at pop time. Caught by the unit suite before any
   UI existed. Fixed in both engine copies; regression test added
   ("sibling after an entered container runs").
2. **The Studio tab never activated its panel.** The main window's tab-switch handler carries
   a hard-coded list of pinned tab ids; `studio` was missing, so `getElementById('p_studio')`
   returned null and the handler threw (all panels left inactive). Found driving the real
   rendered main window in the browser pane. Fixed in both copies.
3. **Undo/redo buttons stayed stale after parameter edits.** The light-edit path skipped the
   button refresh, so the first slider tweak left Undo visually disabled. Fixed.
4. **`prospecting_scripts.json` was not gitignored.** User script data now ignored like
   builds/history.
5. **A build attachment could not carry a script.** `attach_build_file` file types now include
   `*.ppscript`, so "my whole setup including my custom mode" travels as one `.ppbuild`.

### Attacks that were tried and correctly repelled (no change needed)
- Tampered `.ppscript`: unknown block type (`os.system`, `rm -rf`), 20-deep nesting, 501
  blocks, duplicate ids, bool-as-int, floats in int params, illegal keys (`Escape`, `F13`),
  unknown top-level fields, 3 MB payloads, non-JSON garbage. All rejected with human messages
  naming the problem; illegal-but-coercible values are sanitized on import and the result
  re-validated. Nothing is ever executed.
- Runtime whitelist: a hand-tampered script pressing a non-whitelisted key safe-stops with
  zero key events sent; `hold_key` additionally refuses non-movement keys.
- Infinite hangs: wait timeouts are clamped to [100 ms, 120 s] at runtime regardless of the
  saved value; an all-comment script stops itself after 50 do-nothing passes; a stall
  watchdog (180 s without a completed step) trips the normal safe stop; a lap cap bounds the
  top-level loop.
- Safe stop bypass: the `stop` block, timeouts and every interpreter error route through the
  existing `safe_stop` (retry ladder intact); Esc/Ctrl+K/Pause abort sleeps within 25 ms
  (measured); held keys are released on every abort path.
- Malformed `SCRIPT_JSON` in the config (hand-edited): hard safe stop with a clear message;
  the engine process stays alive and stoppable.
- Editor state: undo/redo round-trips through add/move/nest/param edits; deleting the
  selected block clears selection; renames keep the active pointer and the engine config in
  step; name clashes on save are refused (no silent overwrite); imported names auto-rename.
- HTML injection via script names/comments/descriptions: all user text is escaped in the
  library, the canvas, summaries and selects.
- Running with problems: the Python layer refuses `studio_set_active`/`studio_run` for any
  script with errors or problems ("Fix this first: ..."); saving drafts stays allowed so no
  work is lost. (A preview-harness shim initially masked this; verified against the real
  Api.)
- Engine dispatch order: Tracker (watch-only) wins over a script; a script supersedes
  Treasure/Geodes/Shards; deleting the active script flips the config back to built-ins.

### Verified against the mandatory MVP list
- Studio tab + window: ship in the app's style, open/close like analytics, byte-identical
  (tour_check lockstep regions `studio-model/api/html/panel/engine/schema` all pass). ✓
- All 17 palette blocks wired in editor AND interpreter (drift guard asserts the sets match). ✓
- Script model + validation + persistence via Api; scripts survive restart (fresh-process
  reload verified). ✓
- Editor: drag AND keyboard add/reorder/nest, RANGES-bounded sliders, per-block hover help,
  undo/redo, unsaved-changes guard, live + authoritative validation with tagged messages. ✓
- Templates load; Treasure (Rubble Creek) built from blocks completes the verified
  dig/strafe/dig/strafe order with pans counted, against the deterministic detector +
  input stubs (see limitation below). Standard-loop reproduces the default cycle shape. ✓
- Run/Stop/Pause, Ctrl+K, Esc paths identical to built-ins (same launch/stop/hotkey code);
  relic timers, watchdogs, safe stop, notifications all still run during a script run. ✓
- Safety rails: see attack list. ✓
- .ppscript export/import round-trip identical (blocks byte-equal), auto-rename on clash. ✓
- Studio tour auto-offers once (gated behind the access gate like every tour) and the
  editor walkthrough builds the Treasure script step by step; hover help on every Studio
  control; no em dashes or bare ampersands in any user-visible Studio text (swept). ✓
- py_compile x8 clean; tour_check RESULT: ALL CHECKS PASS (with the Studio surface, studio
  selectors and six new lockstep regions); finds_sim ALL SCENARIOS PASS; studio_tests ALL
  PASS; 14-file Windows zip rebuilt and its four Python files byte-match windows/. ✓
- Version, tags, docs/version.json, installer.iss, Info.plist untouched. ✓

### Genuine non-blocking limitations (not reclassified features)
- **Live-game pass is an owner follow-up.** Roblox + a calibrated game session were not
  available overnight, and starting a real run would send real input to whatever holds
  focus. The full acceptance path ran against the deterministic detector and input stubs
  instead (order, stats, stops, timeouts). The owner should run the Treasure script once at
  Rubble Creek and confirm pans count and Esc stops it.
- **Recovery in script mode uses the simple rungs** (timeouts, watchdogs, safe stop with
  retry), not nudges/break-outs, which assume the standard water-land geometry
  (DECISIONS.md #25).
- **Unsaved editor changes do not survive a full app quit.** Closing the Studio window only
  hides it (state kept); switching scripts warns; but quitting the whole app discards
  unsaved edits. Documented; acceptable for v1.

## Result
Round 1 closed with all findings fixed and re-verified (full protocol green). No blocking
findings remain.

## Round 2 (2026-07-16, v1 review board: 11-pass review of v0.9)

Method: architecture/UX/workflow/runtime/reliability/performance/accessibility/DX/product/
polish/adversarial passes; every claim measured in the running editor (browser pane, real
rendered code) or against the real Python/engine layers.

### Weaknesses found and fixed
1. **Forward compatibility hole (architecture).** Adding a param to any block type would
   have flagged every existing saved script as broken ("missing param" errors). Fixed with
   normalize-then-validate at load boundaries + a real version window (newer-schema files
   refused with a human message). Proven by tests: an old-style script missing params loads,
   validates clean, and runs in the engine.
2. **Data-loss window (reliability).** The scripts file had two-phase writes but no recovery
   for a corrupt/truncated file; the Studio config write was single-phase. Fixed: rolling
   .bak + read fallback (tested with corrupt and empty files), two-phase config write.
3. **O(n) revalidation on every library render (performance).** studio_list now caches
   against the file stamp; invalidated on write (tested).
4. **Selection cost at scale (performance).** Full canvas rebuild on every selection: ~310 ms
   at the 500-block cap. Selection became a class-swap + inspector-only render: ~45 ms.
5. **Structural edits at scale (performance).** Double renders in add/dup/del/move/drop paths
   removed; `content-visibility:auto` skips offscreen layout. Measured at the cap:
   ~330 ms -> ~80-100 ms per action. Undo ~40 ms. Interpreter: 0.8 us/tick at 456 blocks,
   16 levels deep, 50k ticks.
6. **Workflow friction (UX).** No fast add path, no context menu, no copy/paste, no palette
   filter, focus lost after delete, no drag auto-scroll, no cost feedback. Added: / quick-
   insert with fuzzy match and full keyboard control; right-click context menu (every action
   also has a shortcut, shown in the menu); Ctrl+C/V subtree copy/paste with client-side
   sanitize; palette filter; delete selects the next block; drag auto-scroll near canvas
   edges; worst-case lap estimate in the top bar; hovering a canvas block now explains it.
7. **Polish gaps.** New-block entry animation (only the added block animates; full
   reduced-motion opt-out), error-toned toasts, aria-live on the validity text.

### Adversarial re-checks after the changes
- Full editor regression at template scale: add, duplicate, Alt-move, copy/paste, delete-
  selects-next, 8x undo restore, keyboard nesting, validity dot, lap estimate: all pass.
- 511-block script correctly refused by the engine (cap works); a dead runner hard-stops
  once and the engine stays alive and stoppable.
- tour_check (incl. node --check on the grown Studio JS + lockstep), finds_sim,
  studio_tests (now 75+ checks): ALL PASS. App-copy diff vs windows/ still exactly the two
  pre-existing platform hunks. Real app boots with every window; clean quit. Zip rebuilt
  and byte-matched.

### Rejected in this round (with reasons)
- Multi-select and bulk edit; favorites/recent blocks; parameter presets: adds surface,
  saves nothing at 17 block types and typical script sizes.
- Dry-run simulator, breakpoints, timeline replay, dependency graphs: the lap estimate,
  live block highlight, History timeline and run logs already answer the real questions.
- Shared md() renderer between windows; handler auto-registration; plugin system: cost or
  risk exceeds benefit at this product size (see DECISIONS 38-39).
- Raising the 500-block cap: no user need; guarantees would degrade.

## Result
Round 2 closed. Remaining V2 opportunities: HUD current-block display, more sensing blocks
(green dig-bar, money/shard reads), variables/counters, reusable sub-script "macros",
record-actions-to-blocks, virtualized canvas if the cap ever rises.
