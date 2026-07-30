# How recommendations are computed

Every suggestion in the warning drawer comes from a fixed, inspectable
rule — there is no model, no heuristics that change between runs, and no
network. This document explains the machinery in plain terms and lists
every rule family. The exact thresholds, codes, and line references are
in `docs/final-prepublish/RECOMMENDATION_RULES.md`; the engine itself is
`lite_diagnostics.py` (pure Python, no screen access, crash-proof, and
covered by `diagnostics_tests.py`, 185 checks).

## The pipeline

1. **Evidence** — the app assembles what it already knows: the latest
   run statistics, this run's engine safety events, live calibration
   statuses and window health, permission states, your settings, and
   the last launch refusal. Nothing new is captured.
2. **Rules** — 16 rule families evaluate that evidence against exact
   numeric thresholds. Each firing rule produces one diagnostic with
   observed facts, evidence lines (threshold included), a confidence
   label, and one or more recommendations.
3. **Bounds** — every suggested setting value comes from a registry
   derived from the app's own settings schema and slider ranges. A
   suggestion is always clamped to the setting's real bounds, and a
   setting without known bounds can never be one-click applied — only
   opened.
4. **Honest confidence** — one shared policy: barely over a threshold
   is a *Possible cause*; comfortably over is *Medium*; double the
   threshold, or categorical evidence (a permission simply is not
   granted), is *High*. Low-confidence titles say "may".
5. **Recurrence** — repeats merge into one issue with a seen-count;
   some families escalate severity when a problem keeps coming back;
   resolved issues drop off on their own.

Applying a suggestion changes exactly one setting per click, through the
same save path the Settings page uses, and records a snapshot so **Undo**
always restores the exact previous value.

## The rule families

The drawer shows the exact tradeoff text per recommendation; this table
summarizes what each family watches, what it may suggest, and what the
suggestion costs.

| Family | Fires on (trigger evidence) | Settings/surfaces targeted | Tradeoff of the suggestion |
|---|---|---|---|
| A — Corrective nudges | ≥ 0.6 nudges/cycle over ≥ 5 cycles | `WATER_EXTRA_BACK_MS` down, `SHAKE_START_DELAY_MS` up, `LAND_SETTLE_MS` up | Slightly slower cycles in exchange for fewer corrections |
| B — Shake starts too early | ≥ 3 shake-start retries or ≥ 2 shake glitches this run | `SHAKE_START_DELAY_MS`, `SHAKE_START_CONFIRM_MS`, `EASY_SHAKE_DELAY_MS` — all up | Later shake starts: steadier, but a touch slower |
| C — Shake starts too late | ≥ 0.4 shake misses/cycle over ≥ 5 cycles | `SHAKE_START_DELAY_MS` down (only if you added one), `SHAKE_BAIL_MS` up; recapture cue masks | Reducing delay risks re-introducing early starts; a longer bail window waits longer on genuine misses |
| D/E/F — Missed Pan / Collect Deposit / Shake prompt | A no-progress stall while that prompt's calibration (or the cue masks, or window health) reads stale/needs-review | Recapture the cue masks and the prompt point (calibration, not settings) | A few minutes of recalibration |
| G — Auto Pan stuck | ≥ 2 tracker kicks or ≥ 3 guard events this run | `AUTOPAN_SETTLE_MS` up, `AUTOPAN_TOL` up, `AUTOPAN_STALL_SEC` on (5 s); calibrate the Auto Pan button if unset | Slower/looser tracking reaction — more tolerant, less snappy |
| H — Capacity bar suspect / hard stops | Stored endpoint pair reads needs-review, or ≥ 1 hard stop | Run Test capacity calibration; redo the capacity right end | None — this one is simply broken until repaired |
| I — Recovery loop | ≥ 0.5 recoveries/cycle over ≥ 5 cycles, or ≥ 3 recovery-ladder rungs | `RECOVER_LIMIT` down, `NO_PROGRESS_SEC` up, `RECOVER_BACK_MS` up | Stops retrying sooner (less self-healing) / reacts to stalls later / bigger corrective movement |
| J — Run stalled (cause chain) | A no-progress event during an active run; cause priority: missing required permission → Roblox window lost → stale calibration | Grant the permission / re-find the window / recalibrate the stale items | None — these are prerequisites |
| K — Finds tracking misses | Finds tracking on, and the finds region is uncalibrated/stale or ≥ 3 ghost/fork events | Recalibrate the finds box; `FINDS_MIN_CONF` down, `FINDS_CARD_SEC` up, `FINDS_EMPTY_MS` up | Lower confidence admits more ghosts; longer windows react more slowly |
| L — Earnings analytics stale | Earnings tracking on, and the money/shards region is uncalibrated/stale | Recalibrate the regions; reset `EARN_OCR_SEC` to 10 s if you raised it past 60 | More frequent reads cost a little more CPU |
| M — Required permission missing | Screen detection (Screen Recording) or input control (Accessibility) definitively not granted | Grant it (deep link to the Trust Center card) | None — the macro cannot work without it. CRITICAL; never suppressible |
| N — Safe Stop cannot listen | The stop-hotkey capability is missing or its test failed | Grant Input Monitoring / re-test | None — running without a panic key is not safe |
| O — Build/script selection conflict | Start refused because the mode and the active Studio build/script disagree | Open the Studio/script tab and fix the selection | None |
| Calibration stale (window changed) | Window health check fails (moved/resized/rescaled Roblox window) | Recalibrate the affected items | A few minutes of recalibration |

Additionally, the app itself raises **"Required calibration is
incomplete"** (severity ERROR) whenever required setup is missing — for
example right after "Mark wizard complete" — so a skipped wizard can
never hide a genuinely unready state.

## What auto-apply will never do

- Change a setting with no known bounds (Open-only).
- Change more than one setting per click.
- Exceed the setting's real slider range (suggestions are clamped).
- Act on its own — every change is a button you click, and every change
  has an Undo.
