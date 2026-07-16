# Prospector Studio — Progress

Updated: 2026-07-16 (session start)

## Current status
Inspection complete; source-of-truth docs written. Baseline verified green before any change:
- `python3 tour_check.py` -> RESULT: ALL CHECKS PASS (144 keys, 9 tours/74 steps, lockstep ok)
- `python3 finds_sim.py` -> ALL SCENARIOS PASS

## What already exists (preserved, not rebuilt)
Everything in PROJECT_MEMORY.md: the pywebview app + all HTML surfaces in prospecting_app.py,
the active engine prospecting_old.py (supervisor, Detector, input, modes, recovery ladder,
SessionStats, emits, webhooks), the 144-key schema + HELP/UI_HELP in prospecting_ui.py, RANGES in
prospecting_assistant.py, tours/preview/builds/access/presets/keybinds, the windows/ mirror, the
verification harness (tour_check.py, finds_sim.py), the 14-file Windows zip.

## What is missing (this project)
All of Prospector Studio: schema, persistence, Api, interpreter, Studio tab + window + editor,
run selector, sharing, tours/help, tests, verification extensions.

## Key facts discovered during inspection (they drive the implementation)
- App copies differ ONLY in `_roblox_rect()` and the screen-size block in `main()` (83 lines);
  ui/assistant copies are fully identical; the engine copies diverge in the input/capture/OCR/
  hotkey/main() regions but the areas I touch (config globals area, SessionStats.as_dict, the
  mode dispatch block, the region before `def treasure_tick`) are shared and byte-identical.
- Mode dispatch lives in engine main(): `if TRACKER_MODE ... elif TREASURE_MODE ... else sup.tick`.
- `save_config()` merges only TYPES keys into the existing config -> SCRIPT_* keys survive.
- `post_webhook(event,...)`: unknown events pass the per-event flag check; still needs
  WEBHOOK_ENABLED + URL.
- Tour content is Python TOUR_DEFAULTS served via tutorial_content(); per-tour localStorage flags
  `pp_tour_<name>`; TOUR_LIST/TAB_TOURS wire the menu + auto-offer; tour_check asserts selector
  resolution + em-dash sweep + lockstep slices between anchors.
- mac keys: KEY_W/S/A/D/SHIFT + SLOT_KEYCODES digits; windows scancodes likewise; no KEY_SPACE yet.

## Done so far
- [x] Baseline protocol run (green).
- [x] PRODUCT_SPEC.md, ARCHITECTURE.md, IMPLEMENTATION_PLAN.md, TASKS.md, DECISIONS.md,
      TEST_PLAN.md, PROGRESS.md, EVALUATION.md scaffold.

## Problems / blockers
None yet.

## Next action
Implement step 1 (schema + validation + persistence + Api + templates) in the macOS copies via a
scripted patch, mirror to windows/, extend nothing in tour_check yet, run the protocol, then unit
tests.
