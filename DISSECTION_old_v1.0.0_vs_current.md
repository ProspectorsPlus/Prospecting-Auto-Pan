# Dissection — your friend's old version (v1.0.0) vs the current engine

**Your friend's build** = the `ProspectorsPlusSetup.exe` dated **Jun 25 18:25**, which
is release **v1.0.0** (tagged Jun 25 18:19). The `Prospectors Plus.bat` just
launches `prospecting_app.py` — it isn't the macro, it's the Windows launcher.
The recovered v1.0.0 source is in `Prospectors_Old_v1.0.0/` (run it on Mac with
`python3 run_old.py`).

## Headline
The **core is the same. The difference is everything bolted on around it.**

| | v1.0.0 (friend's) | Current |
|---|---|---|
| `prospecting_old.py` (engine) | **1,705 lines** | **3,350 lines** |
| `prospecting_app.py` (app) | 735 | 3,009 |
| Engine functions | 53 | ~78 |
| Access gate | none | yes |

## What is IDENTICAL (so it's NOT the reason for the difference)
- **The momentum shake (`do_shake`)** — same docstring, same rapid-click-stream
  logic, same "capacity is truth / bail only if still full past `SHAKE_BAIL_MS`".
- **The core supervisor loop** — sense → `act` → `recover` → `break_out` →
  `safe_stop`. It already existed in v1.0.0.
- **The default timings** — `DIG_CLICK_MS=75`, `SHAKE_CLICK_MS=18`,
  `SHAKE_CLICK_GAP_MS=14`, `SHAKE_HOLD_MS=1500`, `SHAKE_BAIL_MS=500`,
  `STUCK_TICKS=3`, `RECOVER_LIMIT=3`, `SHAKE_FAIL_LIMIT=5`,
  `POST_SHAKE_SETTLE_MS=150`, `PRE_DIG_SETTLE_MS=60` — **all the same.**

So the shake technique and the numbers your friend is running are the same ones
in the current build. The clean cycle was always good.

## What the CURRENT build ADDED (the actual differences)
~1,600 extra engine lines of subsystems that run alongside the core loop and can
**interfere with a cycle that was already fine**:

1. **Aggressive recovery fast-paths — the big one.** v1.0.0 has **none** of these:
   - `SHAKE_GLITCH_LIMIT` — after N "failed" shakes, *instantly* break-out.
   - `NO_PROGRESS_SEC` — if nothing "completes" for N seconds, *instantly* break-out.
   These fire **mid-cycle** and can yank the character out of a good rhythm, and
   they are exactly what caused the "it auto-does Fortune River" / instant
   break-out loop (the `0`-means-always-true bug). **v1.0.0 simply runs the loop
   and only safe-stops on a genuine deadlock** — far fewer false interruptions.
2. **Adaptive timing** (`SMART_TIMING`, `_adapt_shake`, `_adapt_land`,
   `ADAPT_*`). The current engine can *change your tuned timings on the fly*.
   v1.0.0 never moves your numbers — it runs the values you set, every cycle.
3. **Fortune River / Starfall River recovery** (`fortune_river_recover`,
   `starfall_river_recover`, cursor sweeps, scrolling). Powerful, but it's a lot
   of fragile screen-driving that can misfire. v1.0.0 has none of it.
4. **Shake start-confirm + deeper-tap retries** (`SHAKE_START_CONFIRM_MS`,
   `SHAKE_START_RETRIES`, `SHAKE_STALL_MS`) and **fixed shake-count**
   (`SHAKE_CLICKS`). These change *how the shake decides it started/finished*
   vs v1.0.0's plain "click until empty".
5. **Whole new modes** — Tracker/Auto-Pan, smart/pipelined digs, treasure mode,
   earnings OCR, X-pattern, relic-on-land. More surface area = more that can go
   sideways, even when off by default (extra branches, extra sensing).

## Why the old one "feels better / more consistent"
Because the clean loop was never the problem — the **added recovery/adaptive
layers occasionally fight it.** A cycle that v1.0.0 would have completed can, in
the current build, trip a fast-path break-out, an adaptive timing nudge, or a
recovery routine, and turn a good pan into a stall. Fewer moving parts → fewer
false interventions → higher clean-cycle %.

(This is also the entire thesis behind the **Fable5** rebuild: same core idea,
but consistency-first, with the shake *proving* it started and no blind
interference — a principled answer to "the middle versions got too busy.")

## You can get most of the old feel in the CURRENT build
Without downgrading, set these in the current app to strip the interfering
layers back to a v1.0.0-style clean loop:
- `SHAKE_GLITCH_LIMIT = 0`  (disable instant shake-glitch break-out)
- `NO_PROGRESS_SEC = 0`  (disable no-progress break-out)
- `SMART_TIMING = off`  (stop auto-changing your timings)
- `FR_RECOVERY = off`, `SR_RECOVERY = off`  (no fast-travel recovery)
- `X_PATTERN = off`, `TRACKER_MODE = off`, `DIG_FILL_SMART/DIG_PIPELINE = off`
- Keep `RECOVER_ENABLED`/`BREAKOUT_ENABLED` on (that's the *original* gentle
  ladder v1.0.0 also had) but with the fast-paths above disabled.
> Note: `0` now correctly means **disabled** for the threshold knobs (that was a
> bug in 2.3.0). So setting them to 0 is safe in the current build.

## Run the old version on your Mac (for A/B testing)
```
python3 run_old.py          # or double-click run_old.command
```
It launches `Prospectors_Old_v1.0.0/prospecting_app.py` in its own folder, with
your current calibrated pixels already copied in, and **does not touch your
latest version**.

## Side note — the AutoHotkey file
`SandshakingMacro_CLEAN.ahk` (Jun 22) is a *separate* AutoHotkey "sandshaking"
macro — likely the original inspiration, not the Python app. Its own timings
(`BaseDuration=600`, `WalkbackDuration=300`, `ShakeTime=3000`,
`ShakeTimeout=250`) are a different lineage. Worth keeping as reference, but the
"very old version" your friend runs is the **Python v1.0.0** above.
