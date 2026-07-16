# Prospectors Plus

An invite-only auto-pan macro for the Roblox game Prospecting (macOS + Windows). It reads the
screen (never game memory) and sends OS-level input, with guided calibration, a visual Cycle
tuning page, builds, analytics, a live HUD, Discord notifications, an interactive tutorial,
and machine-locked access codes. The complete architecture map lives in
[PROJECT_MEMORY.md](PROJECT_MEMORY.md).

## Running from source (macOS)
```
python3 prospecting_app.py
```
Fully quit and reopen to reload UI changes. The Windows copies live under `windows/` and stay
byte-identical for all shared code; `Prospectors Plus Windows.zip` is the 14-file bundle.

## Prospector Studio
Studio is the built-in visual scripting system: users compose custom farming modes from
Prospecting-specific blocks (dig, shake, walk until a prompt, waits, conditions, relics) in a
dedicated editor window, run them through the real engine with the same calibration, stats
and safety nets as the built-in modes, and share them as single `.ppscript` files.

- Docs: [PRODUCT_SPEC.md](PRODUCT_SPEC.md), [ARCHITECTURE.md](ARCHITECTURE.md),
  [DECISIONS.md](DECISIONS.md), [TEST_PLAN.md](TEST_PLAN.md), [EVALUATION.md](EVALUATION.md)
- Block schema (single source of truth): `STUDIO_BLOCKS` in `prospecting_ui.py`
- Interpreter: the "CUSTOM SCRIPTS (Prospector Studio)" section of `prospecting_old.py`
- Scripts persist in `prospecting_scripts.json` (user data, not tracked)

## Verification protocol (run after every change)
```
python3 -m py_compile prospecting_app.py prospecting_old.py prospecting_ui.py \
    prospecting_assistant.py windows/prospecting_app.py windows/prospecting_old.py \
    windows/prospecting_ui.py windows/prospecting_assistant.py
python3 tour_check.py     # must print: RESULT: ALL CHECKS PASS
python3 finds_sim.py      # must print: ALL SCENARIOS PASS
python3 studio_tests.py   # must print: STUDIO TESTS: ALL PASS
```
Rebuild the zip whenever anything under `windows/` changes (command in PROJECT_MEMORY.md §18).

Releases (version bumps, tags, builds, publishing) are the owner's job; see
PROJECT_MEMORY.md §17.
