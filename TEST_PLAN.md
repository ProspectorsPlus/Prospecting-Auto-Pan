# Prospector Studio — Test plan and results

Results are appended per run; the latest full run is authoritative.

## A. Static / protocol (every integration)
1. `python3 -m py_compile` all 8 active files.
2. `python3 tour_check.py` -> RESULT: ALL CHECKS PASS (extended: STUDIO_HTML scripts node
   --check both copies; studio tour selectors resolve — `studio` against the main html,
   `studio_editor` against STUDIO_HTML; studio lockstep regions byte-identical).
3. `python3 finds_sim.py` -> ALL SCENARIOS PASS.
4. Zip content check when windows/ changed: unzip -l matches the 14 tracked files.

## B. Unit (studio_tests.py)
- Validator accepts: every template; a maximal script using all 17 types.
- Validator rejects (with the offending block named): unknown type, missing param, extra param,
  out-of-range, non-finite, wrong param type, depth 17, 501 blocks, non-whitelisted key,
  duplicate ids, bad name, children on a leaf, non-dict block, blocks non-list.
- Sanitizer (import): unknown fields dropped; ids regenerated; clamps applied; result validates.
- Schema<->interpreter drift: STUDIO_BLOCKS type set == ScriptRunner handler set; whitelist sets
  match engine `_SCRIPT_KEYS` tokens.
- Templates: standard + treasure validate; treasure structure matches the MVP path.

## C. Interpreter (stubbed input/detector; mac engine imported with input functions patched)
- Sequence order: actions fire in document order; group flattening correct.
- repeat N: body runs exactly N times; nesting works.
- if_cue/if_cap/if_not: children run only when the read matches; else skipped.
- wait_cue timeout + on_timeout=stop -> safe_stop called; =continue -> continues.
- Timeout clamp: timeout_ms 0 or 10^9 clamps into [100, 120000].
- Whitelist: a hand-tampered key token -> safe stop, no key event sent.
- Watchdog: all-comment script -> stops itself (empty-pass guard), engine alive.
- Esc mid-wait: State.running=False aborts within a slice; release_all ran.
- Pans: one cycles++ per top-level pass; digs counted per dig block.
- Treasure template on a scripted detector feed: emitted action order is
  dig, (wait), strafe D until fresh deposit, dig, (wait), strafe A until fresh deposit, wrap;
  cycles increments per wrap.
- Tamper battery via the APP validator: huge nesting, bad types, illegal keys, giant JSON,
  non-finite numbers -> rejected with human messages; engine runner given a bad SCRIPT_JSON ->
  safe stop, no crash.

## D. Editor logic (node harness where feasible)
- validation flags: empty script, empty container, unreachable after stop.
- undo/redo round-trips; add/reorder/nest produce the expected tree.

## E. Manual in the running app (macOS, live)
- Launch `python3 prospecting_app.py`; full quit + relaunch after UI edits.
- Studio tab renders; library empty state; New script -> template picker; Open Studio window.
- Build Treasure from blocks by hand (palette add, params, nest), validate, save.
- Set active (Studio + Run tab selector); supersede note shows; run; stop with Esc and Ctrl+K;
  pause/resume; stats count; History labelled; export; delete; re-import; identical + runnable.
- Restart persistence: scripts + active survive full quit.
- Error/empty/validation states; small window; keyboard-only pass; JS consoles clean.
- Live game verification: performed ONLY if Roblox + calibration are available; otherwise the
  deterministic stub run stands in and the live pass is recorded as an owner follow-up.

## Results log
(appended chronologically)
