# Prospectors Plus — Fable engine (v4.0.0)

A ground-up rebuild of the auto-pan macro for the Roblox game **Prospecting**,
built for consistency first. macOS-first with a portable core (the input layer
already carries a Windows `SendInput` backend for the port).

## Run it

```
cd Fable5
python3 app.py        # or double-click run.command
```

Same dependencies as the old app (already on this machine): `pywebview`,
`mss`, `numpy`, `pynput`, `pyobjc`. First launch auto-imports your old
calibration, timings, relics, hotkeys, and stats from
`../prospecting_config.json` (never webhooks/keys/secrets).

Hotkeys (rebindable in Settings): **Ctrl+K** start/stop, **Ctrl+J** soft-stop
test, **Esc** quit engine.

## Why this one is more consistent

The old engine's telemetry (last 40 runs) showed exactly where cycles died:
397 blind landing nudges, 124 shakes that never started, 115 shakes that
didn't empty. Each has a structural fix here, and all of them are proven by
simulation before ever touching the game:

1. **Continuous perception.** A background thread captures ONE combined
   region at 60 fps and publishes a filtered world-state (capacity fill 0–1,
   fill **rate**, debounced cues). The old engine grabbed the screen
   per-check and was blind during every sleep/hold. Actions now wait on
   frames, not on hope. Cue flicker is erased by asymmetric hysteresis.
2. **Start-confirmed shake.** The shake must PROVE it started (Shake cue or
   measurable fill drop) within ~320 ms. If not: an S-tap slightly deeper
   and keep clicking — a ~150 ms micro-retry instead of a lost 5–30 s cycle.
   Root cause handled too: momentum W now engages only AFTER the start
   confirms, so a failed first click is never dragged back onto land
   (`SHAKE_W_AFTER_START`, on by default; off = old behavior). Walk-back
   also defaults 60 ms deeper past the Pan cue — edge clicks are the #1
   silent failure. The rapid-click stream itself is untouched (macOS holds
   do nothing — clicks are law).
3. **Drain-aware emptying.** The engine watches the bar drain and stops on
   *confirmed empty* (2 frames), with a stall detector and a shake budget
   predicted from your stats — slow builds don't get cut off, fast builds
   don't over-click into the next dig.
4. **Cue-confirmed landing.** After the pan empties, W is held until the
   Collect Deposit cue debounces on — landing is no longer momentum
   guesswork, so the dig-probe (kept, as verification) almost never nudges.
5. **Watchdogs with reasons.** Same proven ladder (stuck → recover →
   break-out → safe-stop with auto-retry) plus fast paths for shake-glitch
   and no-progress, a sensor-stall guard, and a mis-calibration guard. Every
   threshold treats **0 as disabled** (the v2 0-bug is regression-tested).
   Every action verifies its own outcome; every escalation carries a reason.
6. **Tested like software.** `sim/` contains a virtual-clock game model with
   the real glitches (edge misses, random start failures, sticky Shake cue,
   flicker, dig lag) and a scenario suite:

   ```
   python3 -m sim.test_engine     # 31 checks
   python3 -m sim.run_sim         # monte-carlo consistency report
   ```

   Baseline: ~2450 pans/hr at 100% clean cycles; with a pathological
   edge-heavy shoreline it still holds ~2270/hr with zero hard stops.

## Layout

```
app.py                UI (pywebview) — dashboard, settings, calibrate, history
run_engine.py         engine process entry
engine/
  config.py           schema + validation + legacy import + game formulas
  clock.py            ms-accurate hybrid sleep (+ virtual clock interface)
  inputs.py           Mac (Quartz) + Windows (SendInput) input backends
  vision.py           frame -> signals (pure, unit-tested)
  percept.py          60 fps sensing thread, hysteresis, event waits
  actions.py          dig / go_water / shake v2 / landing v2 (verified verbs)
  brain.py            capacity-primary supervisor + watchdog ladder
  telemetry.py        '@@ {json}' protocol, phase timings, run history
  runtime.py          engine main: hotkeys, stdin commands, safe-stop retry
sim/                  game simulator + harness + scenario tests
fable_config.json     settings (created on first run)
fable_history.json    per-run stats + event reasons
```

## Dashboard

Live capacity bar + cue lights straight from the perception thread, current
phase, pans/hr, **clean-cycle %** (cycles with zero retries/recoveries — the
consistency number), saves counter, and a color-coded event feed. History
keeps per-run reason breakdowns.

## Not ported yet (deliberately — core first)

Fortune River recovery, treasure mode, Discord webhooks, the Coach, access
gate, auto-update. The old app still works for those; this engine's
protocol was designed so they bolt on cleanly.
