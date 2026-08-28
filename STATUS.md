# Status

Per-phase and per-gate status. Three columns, because they fail independently
(plan §15): what can be finished on this machine, what needs macOS hardware,
and what needs Windows hardware.

Last updated: 2026-08-28 (fourth pass). Development machine: macOS 25.4, arm64,
CPython 3.13.15, Tk 9.0. **No Roblox session was operated and navigation was
never armed during implementation; no input was sent.** This pass touched the
game window not at all: Roblox was in native fullscreen on another Space
throughout, which is exactly the condition the new setup machine is built to
diagnose, and it did (see below).

---

## What changed on 2026-08-28 (fourth pass) — production navigation

The third pass measured the detector and fixed it. This one audited why
nothing downstream of the detector could ever run, and rebuilt that half.

| Confirmed root cause | Evidence it was real | Now |
|---|---|---|
| **The commissioning system could not complete.** `NavigationGates` was constructed once with every `E-*` field `PENDING` and frozen; no production code validated or persisted a gate; `CommissioningWindow` was a periodically rewritten read-only `Text` widget that performed no procedure and no state transition. "Collect Calibration Evidence", "Calibrate Live Control" and "Enable Live Control" all converged on it, and `_arm()` redirected there whenever blockers existed. | `commissioning_steps()` only rendered gate statuses; no call site anywhere set one. `_collect_evidence()` opened the window and started a recording. | Deleted. `prospector_engine/autosetup.py` runs nine typed bounded stages to READY; capability is `NavigationCapabilities`, derived from what this run observed (D-036) |
| **Live was unreachable twice over.** `make_live_worker` refused while the static gates were pending; even with them fabricated, `Navigator` built a default `ShiftLockController` whose empty `YawCalibration` also refused. Green tests hid both by injecting `ALL_PASSED` and a fabricated calibration. | `tests/test_navigation.py` `_calibrated_navigator` constructed exactly that. | `tests/test_setup_flow.py` builds the real application through `build_application` and drives it to READY with nothing injected |
| **Fit was not a transaction.** Capture, telemetry and guard polling continued during a resize, so an intermediate size was classified `CAPTURE_MISMATCH`, restarting capture and churning the epoch. The macOS AX lookup fell back to the largest, then the first, window. | `ViewportGuard.check()` compared against the adopted identity with no fence; `_ax_window_for` ended in `largest window` / `first window`. | `ViewportGuard.transaction()` fences mismatch classification, bounded by its own deadline (D-035); AX ambiguity is refused with a remedy (D-034) |
| **The GUI had no geometry contract.** Unconstrained status and blocker labels, `grid_remove` on conditional controls, and `CommissioningWindow.refresh()` scheduling its own `after` at the end of the render — so opening it repeatedly multiplied the refresh loop. | Four clicks produced four loops; a long Accessibility message widened the toplevel. | Fixed-width leaves, fixed-height wrapped message boxes, state-not-grid for conditional controls, one `Ticker` per loop, one expanding row — each asserted in `tests/test_gui.py` (D-041) |
| **Navigation was scaffolding.** Left/Right were not in the vocabulary; the player reference was a hardcoded anchor; the pipeline supplied `motion=None`; applied `W` never reached the progress ledger; recovery changed labels and emitted nothing; two steering controllers coexisted. | `PerceptionPipeline.analyze` set `motion=None`; `RecoveryLadder.escalate()` returned a level nobody executed; `SteeringController` was constructed and only ever asked for its config. | Turn keys in the vocabulary and the release floor (D-038); a measured `TurnResponse` per run (D-037); `estimate_lk_affine` wired; `NavigationApplyResult.leases_held` feeds the ledger; a recovery ladder that emits real bounded maneuvers with a sticky side; one `ArrowFollowerController` |

**Detector: deliberately unchanged.** The real-frame corpus report is
byte-identical before and after this pass, per sequence and in total.

## What changed on 2026-08-28 (third pass) — regression recovery

The second pass rebuilt the detector and the dashboard. This one measured
them on real frames and found that they had regressed, then fixed what the
measurements said.

| Observed regression | Root cause confirmed | Now |
|---|---|---|
| Arrow locks onto terrain, HUD and particles; identity changes between objects; direction sign is a coin flip | The two-notch topology was a *precondition* and real outlines are nicked by UI strokes; the boundary term was judged against the frame median and read 0 on flat sand; polarity came from taper about a notch line that a low camera angle inverts; `analyze()` selected the held track while the pipeline took direction and contour from the *first accepted* hypothesis | Stateless `propose` → `fuse` → one `commit` per unique frame; exactly one **selected** candidate feeds everything; ACQUIRE/TRACK/AMBIGUOUS/REACQUIRE/LOST with time-based bounds; polarity led by the **barb asymmetry**; structure is weighted evidence, contrast the only soft veto |
| 15 unique fps, 8 analysis fps, 264 ms p95 | An ROI miss ran a second stateful full pass on the same screenshot; per-candidate measurement windows up to 1 M px dilated with a 41 px element; a 240-sample p95 kept one old 274 ms sample "current"; two 0.5 s bad polls downshifted; DEGRADED had no path to a probe; zero processed fps fell back to capture fps | Bounded windows, bounded candidates, spaced idle searches; readiness judged on a 2 s recent window with history kept beside it; 1 s of shortfall to downshift; DEGRADED probes after cooldown; settling and SCK reconfiguration acknowledged before judging; a real zero is a zero |
| Fit & Lock frequently did not visibly resize | `_ax_window()` took `windows[0]` while `_scan_roblox()` picked a different CG window; a move to (0,0) was requested with the resize and a denied move failed the whole call; an OS clamp returned `ok=False` | AX window correlated by frame/title/largest; size only, origin preserved; clamp is `ok=True, clamped=True`, classified by the guard after three stable read-backs; typed `FitCompletion` through the coordinator; progress in the dashboard |
| 14 static, duplicated blockers | A default `ShiftLockController` was instantiated to ask why it could not steer, so E-YAW appeared three times; "not frontmost" was a permanent failure | Keyed, scoped `LiveBlocker`s recomputed on every read; one row per gate; "not frontmost" is an *expected* condition with an instruction; an 11-step guided commissioning window |
| `--detector-report` alive for hours in a Tk lifetime | Not reproduced here (the rendered report exits in 72 s) | Every offline mode is mutually exclusive and bounded, and `tests/test_cli_lifecycle.py` proves it as subprocesses: no Tk, no dashboard, no child process, bounded exit |
| Synthetic soak and rendered fixtures proved nothing about the game | Rendered frames are training stress by plan §7.2 | A **real-frame corpus** (`tests/corpus/real`, 178 frames, 14 sequences, split by contiguous sequence) with a bbox-aware evaluator and a regression gate; a native `--shadow-bench` |

---

## Legend

`done` · `partial` · `pending` (gate exists, not run) · `blocked` · `n/a`

---

## Phases

| Phase | Local implementation / replay | macOS commissioning | Windows commissioning |
|---|---|---|---|
| 0A Characterization | **done** | n/a | n/a |
| 0B Platform ports and viewport | **done** — explicit coordinate spaces, connect/fit split, bounded fit machine, AX-window correlation, honest clamps, typed completions | **partial** — one measured `canonical_verified` on this Mac (2x, one display); the DPI/display matrix is **pending** (E-VIEW) | **pending** (E-VIEW) |
| 0C Input authority and deadman | **done** — unchanged this pass | **pending** | **pending** |
| 0D Coordinator migration | **done** — plus fit completions as typed queue items | n/a | n/a |
| 0E Bounded legacy services | **done** | **pending** | **pending** |
| 1 Observation foundation and GUI | **done** — automatic setup, stable-geometry dashboard, one timer per loop, bounded per-frame trace, honest governor | **partial** — native headless observation measured at 57 of 57 unique fps (third pass); an owner-observed session with a map equipped is **pending** | **pending** |
| 2 Offline perception | **partial** — detector v2 measured on the real corpus (below); no separately held-out session exists | **blocked** on a second recording | **blocked** |
| 3 Navigation and controller | **done locally** — one follower, measured turn actuator, wired motion, bounded recovery; 94 simulated routes at 30/60/90/120 fps | **pending** — needs a windowed Roblox session; every native check below is blocked on that | **pending** |
| 4 One-map live lifecycle | **partial** — the armed prologue (control mode, turn characterization) and the follow/recover loop exist and are tested against fakes and a simulated world | **pending** | **pending** |
| 5 Multi-map lifecycle | **blocked** | **pending** | **pending** |
| 6 Packaging and release | **partial** | **pending** | **pending** |

---

## Experiment gates

None of these is passed. Two moved from "nothing measured" to "measured here,
not validated".

| Gate | Status | What changed / what it still needs |
|---|---|---|
| E-VIEW | **partial** | Fit & Verify ran against the live client: `canonical_verified` in 0.35 s, 1280×720 pt / 2560×1440 px, 3/3 stable read-backs, origin preserved, window restored afterwards. One display, one DPI. Needs the DPI/display matrix and Windows. |
| E-ANCHOR | pending | Reviewer-labelled avatar control pivot across sessions. The runtime *reference check* (heading stability with the screen anchor, measured jitter recorded) is a different and weaker claim and never presented as this gate. |
| E-FORWARD | pending | **Physically armed** bounded `W` pulses with blinded labelling. |
| E-DIR-IDEAL | pending | Manual masks plus aligned-zero outcome trials. |
| E-PROF | **pending, with regression evidence** | A real corpus exists and the eval split is a regression gate (`tests/test_corpus.py`). It is one session, one map, one machine, with the previous overlay drawn on most arrows. Needs a second, separately held-out recording of a live session with Shadow running (Start Diagnostic Recording). |
| E-DIR-E2E | **pending, with regression evidence** | Same corpus; headings are reviewer-read to about ±12°, which cannot certify a 10° p95. |
| E-ARRIVE | pending | Approach/fade sequences and long negatives. Arrival now *stops* the route after three consecutive candidates rather than being ignored; stopping needs no gate, digging on arrival would. |
| E-MOTION | pending | Labelled clips and an armed locomotion baseline. A **session-scoped** baseline is now sampled at runtime from frames where forward was genuinely applied (D-040); it is prefixed `runtime:` and its provenance says in words that it is not this gate. |
| E-SHIFTLOCK | pending | Armed stationary micro-yaw with Shift Lock on and off. The runtime check observes the centred pointer cue and never toggles Shift (D-037); the armed micro-yaw method is defined and unrun. |
| E-YAW | pending | **Physically armed** yaw pulses confirmed by perception. Superseded in the production path by the per-run `TurnCharacterizer`, which measures sign, gain, minimum pulse, latency and reliability from bounded stationary probes each session; that has been exercised against a simulated camera only and is **pending** a native run. |
| E-STEER-CAL | pending | Armed manual-target trials. |
| E-STEER-E2E | pending | Guarded routes. |
| E-RECOVERY | pending | Now **in** the control path and bounded: release, reacquire, sticky-side strafe, forward probe, jump, opposite side once, abandon. Judged on a simulated world only; needs a native obstacle test after open-ground following is safe. |
| E-DIG / E-LIFECYCLE | pending | Labelled dig episodes. |
| E-NEXT_MAP / E-SKIP_MAP | pending | Off. |
| E-PERF | **pending, with native measurements** | Headless Shadow on the live client measured below. Stop latency in wall-clock ms and the dashboard-with-Live path are not measured. |

Because no gate is validated: **Live navigation refuses to steer**, automatic
profile classification is off, recovery is off, next-map automation is off.

---

## Observed local facts (not gate passes)

### Real detector metrics — `tests/corpus/real`, eval split

`.venv/bin/python treasure.py --detector-report --corpus tests/corpus/real --json out.json`

122 eval frames: 86 arrow-present, 30 arrow-absent (22 from the recording, 8
live), 6 `unknown` (excluded and counted). Sequences marked `tune` were used
for every choice; `eval` was only read. Headings are reviewer-read (±12°).

| Build | Recall | False locks | False acquisitions (recording / live) | Identity switches | Single-frame replacements | Sign accuracy | Median error | p95 error | Perception p50 / p95 |
|---|---|---|---|---|---|---|---|---|---|
| f3f7b63 (baseline segmenter, `yellow_map_v0`) | 91.9 % (79/86) | 2 (1.9 %) | 0/22 / not run | 2 | 0 | 61.6 % | 28.2° | 172° | 3.4 / 5.3 ms |
| 4756ab7 (second pass, HEAD at start) | 52.3 % (45/86) | 8 (7.4 %) | 0/22 / not run | 0 | 0 | 51.6 % | 87.1° | 160° | 11.0 / 51.5 ms |
| **this pass** (detector v2, `yellow_map_v1`) | **80.2 % (69/86)** | **0** | **0/22 / 3/8** | **0** | **0** | **90.9 % (30/33)** | **10.4°** | 104.6° | **5.2 / 8.3 ms** |

Per stratum, this pass: pink-crystal 90.9 % (20/22), purple-night 83.3 %
(5/6), purple-pale 75 % (9/12), sand-same-colour 72.2 % (13/18), open-water
90 % (9/10), sand-occluded 72.2 % (13/18); no-arrow-ui 7/7 and no-arrow-sand
15/15 clean. The direction abstains on most occluded frames rather than
guessing; of 33 frames where it answered, 3 were reversed.

Read this honestly:

- The baseline finds *a fragment of* the arrow more often (its box overlaps
  the label) and points it wrong four times in ten. This pass finds the whole
  arrow less often and points it right nine times in ten, with no false
  locks. The occluded stratum is the weak one.
- **Tune split** (what the detector was chosen on): recall 55.6 %, 4 false
  locks — all on the player's yellow hat beside a hidden arrow — sign 100 %
  (12/12). Reported, not averaged in.
- The live event scene (rainbow lighting, yellow banners) still acquires a
  banner or particle in 3 of 8 frames. The fixed HUD bands are excluded by
  `yellow_map_v1`; the rest is held at the measured count by the gate.
- Against the production targets (recall ≥ 95 %, precision ≥ 99 %, false
  locks < 1 %, sign ≥ 99 %, p95 ≤ 10°): false locks and switches meet them;
  recall, sign and angular error do not, and the sample cannot certify any of
  the percentages. **PENDING.**

### Native Shadow — `treasure.py --shadow-bench`, live client, Balanced 60

Same machine, same scene (a busy no-arrow view, which is the worst case for
the search), headless, 15 s per configuration, read-only.

| Build | Configuration | fps consumed / unique | processed ÷ unique | capture→observation p50 / p95 / p99 / max | CPU | RSS |
|---|---|---|---|---|---|---|
| f3f7b63 | capture only | 78.2 / 78.2 (its 120 Hz tier) | 1.00 | 4.5 / 5.5 / 6.2 / 8.8 ms | 35 % | 108 MB |
| f3f7b63 | capture + perception | 77.4 / 77.6 | 0.997 | 8.9 / 11.4 / 13.6 / 95 ms | 88 % | 170 MB |
| 4756ab7 | — | not measured natively; the dashboard read 15 unique / 8 processed fps, p95 264 ms | | | | |
| this pass, first cut | capture + perception | 32.0 / 32.4 (governor at 30 Hz) | 0.99 | 16.5 / 24.9 / 38.2 / 150 ms | 73 % | 191 MB |
| **this pass, final** | capture only | 57.8 / 57.8 | 1.00 | 4.6 / 5.6 / 6.3 / 7.2 ms | 27 % | 105 MB |
| **this pass, final** | capture + perception | **57.0 / 57.2 at 60 Hz** | **0.997** | **9.7 / 17.3 / 19.8 / 25.5 ms** (outside settling; 213 ms at startup) | **86 %** | 181 MB |

Against the Balanced-60 targets, headless: ≥ 54 processed fps **met**; ratio
≥ 0.98 **met**; p95 ≤ 25 ms **met**; p99 ≤ 50 ms **met**; max ≤ 100 ms outside
settling **met**; superseded 4, pool exhausted 0 **met**. CPU: 86 % against
the baseline's 88 % at 77 fps, which is about +21 points at equal frame rate
— at the edge of the +20 criterion. With the dashboard (next table): 57.6
processed fps **met**, Minimal p95 23.9 ms **met**, Full Diagnostics p95
25.5 ms **missed by half a millisecond**, and CPU 149–176 % is the dashboard's
own cost on top; the baseline's dashboard was not measured. Memory was not
soaked natively (15 s). **E-PERF stays PENDING.**

### Native dashboard — the real Tk dashboard with Shadow running

The dashboard itself, Shadow started through the coordinator, the real Tk
preview, 15 s per overlay mode, same no-arrow scene. The first run exposed
the last regression mechanism and is kept in the table.

| Build / cadence | Overlay | unique / processed / preview fps | tier | end-to-end p50 / p95 / p99 / max (history ring) | CPU | RSS |
|---|---|---|---|---|---|---|
| this pass, before the throughput rule (Auto) | Minimal | 29.4 / 29.3 / 29.1 | 30 Hz | 18.9 / 26.8 / 30.8 / 34.2 ms | 142 % | 287 MB |
| this pass, before the throughput rule (Auto) | Full Diagnostics | 29.3 / 29.2 / 29.2 | 30 Hz | 19.6 / 26.3 / 28.4 / 34.0 ms | 140 % | 287 MB |
| **this pass, final (Auto)** | Minimal | **82.4 / 82.1 / 24.9** | 90 Hz | **10.9 / 21.9 / 27.2 / 28.4 ms** | 172 % | 283 MB |
| **this pass, final (Auto)** | Full Diagnostics | 82.1 / 75.0 / 25.2 | 90 Hz | 11.8 / 28.9 / 35.3 / 35.9 ms | 164 % | 288 MB |
| **this pass, final (Balanced 60)** | Minimal | **57.1 / 57.6 / 24.9** | 60 Hz | **10.4 / 23.9 / 25.7 / 25.8 ms** | 176 % | 292 MB |
| **this pass, final (Balanced 60)** | Full Diagnostics | 56.9 / 57.2 / 25.0 | 60 Hz | 14.5 / 25.5 / 29.2 / 32.5 ms | 149 % | 281 MB |
| f3f7b63 dashboard | — | not measured: its metrics API differs and the bench script could not read it; its headless numbers are above | | | | |

What the first run's trace said: the worker processed 52 frames a second at
60 Hz with 13 % superseded because the Tk preview at 60 fps competed for the
interpreter (full passes p50 15.9 ms against 10.3 headless); the governor
counted the superseded frames as "observation loss 10 %", downshifted to
30 Hz, and the probe back needed 54. A latest-only pipeline supersedes by
design, so the governor now judges a tier against the tier below (D-031
amendment), the preview ticks at 30 fps, and one telemetry publish samples
metrics once. Full Diagnostics still costs about seven processed frames a
second at 90 Hz through the overlay's canvas work on the Tk thread; its
overlay is capped at 20 Hz and skips rather than queues.

### Fit & Verify — live client

`before: client 1800x1054 pt at (0,67), backing 3600x2108 px`
→ `canonical_verified` in 0.35 s, `1280x720 pt / 2560x1440 px (3/3 stable)`,
origin unchanged → restored to `1800x1053 pt` afterwards (one point of
title-bar rounding). Accessibility and Screen Recording were already granted
to this terminal.

### Rendered stress strata — `treasure.py --detector-report`

Still bounded (exits in 72 s). With the new scoring the rendered
same-colour-clutter and translucent strata read far lower than the second
pass reported (1 % and 0 % coverage): the contrast veto and the profile floor
that fixed the real frames are hostile to a half-transparent rendered arrow
on pale terrain. Rendered frames are training stress; this is recorded, not
optimised for.

### Stop safety, governor traces, soak

Unchanged tests still pass (10 000 stop races clean; every watchdog condition
releases; Shadow emits zero input edges). The governor's five required traces
pass with the new hysteresis. The ten-minute synthetic soak was measured on
4756ab7's detector and has **not** been rerun with this one; the three-second
soak in the lifecycle tests passes.

---

### Fourth pass, native — what was and was not measurable

Roblox was in **native fullscreen on another Space** for the whole of this
pass. That blocks capture and window sizing outright, so every native check
below is *pending on the owner leaving fullscreen* — not deferred by choice.

What that did give is a real native result for the diagnosis itself. The real
`build_application`, the real coordinator and the real setup machine, run
against this machine:

```
stage:   failed
kind:    fullscreen
summary: Roblox is in fullscreen, where its window cannot be sized or captured
remedy:  Leave fullscreen so Roblox is an ordinary window, then press
         Start Navigator again.
detail:  Roblox is running but not on this Space - exit native fullscreen
input edges held: ()
```

`--capture-probe` and `--shadow-bench` report the same condition and exit
cleanly without touching the window.

### Dashboard cost — measured, this pass

Real Tk, real dashboard, 200 samples per loop, no capture and no worker:

| Loop | Cadence | mean | p50 | p95 |
|---|---|---|---|---|
| status | 150 ms | 0.135 ms | 0.129 | 0.160 |
| setup panel | 120 ms | 0.012 ms | 0.011 | 0.014 |
| metrics + summaries | 500 ms | 0.022 ms | 0.021 | 0.025 |
| preview (idle) | 33 ms | 0.001 ms | 0.001 | 0.001 |
| diagnostics drawer, open, data changed | 700 ms | 0.147 ms | 0.143 | 0.166 |
| diagnostics drawer, open, nothing changed | 700 ms | suppressed - 50 of 50 renders skipped |
| diagnostics drawer, folded away | 700 ms | 0.0001 ms |

**Idle GUI duty cycle: 0.11 % of one core.** The drawer was previously the
most expensive thing the window did and ran unconditionally; it now renders
only when it is visible *and* something changed.

### Pipeline latency — synthetic source, 20 s, this pass

The whole path: capture service, coordinator, observation worker, real
detector, real navigator. Synthetic frames, because Roblox was unreachable;
the numbers are about the *pipeline*, not about the game.

```
tier requested 90 Hz     governor stable
source 76.1   unique 76.1   processed 76.1   control 76.1   fps
duplicates 0   superseded 3   unobserved 3   slot depth 1
capture     p50  0.00 ms   p95  0.00   max  0.00   (the fake source is free)
perception  p50  5.53 ms   p95  7.99   max 14.33
decision    p50  0.02 ms   p95  0.03   max  0.14
end-to-end  p50  5.68 ms   p95  8.15   max 14.51
frame age   2.0 ms         cpu 77 %    rss 188 MB (peak 268)
```

Unique, processed and control rates are equal and the slot depth is one: the
control loop is taking the newest frame and building no backlog. Three
superseded frames in twenty seconds is the latest-only slot doing its job, not
loss. Perception is the whole cost; the decision is two hundredths of a
millisecond.

### Synthetic soak — 90 s, this pass

```
.venv/bin/python treasure.py --soak 1.5
```

51 unique fps and 51 processed fps sustained; RSS 122-168 MB against a 249 MB
peak; 4 607 frames; one thread before and after; clean capture shutdown;
0 of 8 pool buffers live; RSS slope -28 MB/min. **PASS** as a local soak; this
is not E-PERF.

## Native checks the owner must run

Everything below takes a few minutes and needs Roblox open, windowed, with a
map equipped so an arrow is on screen. Nothing before step 7 sends input.

### Morning, in order (about five minutes)

**Before anything: leave native fullscreen.** Roblox must be an ordinary
window on the current Space, with a treasure map equipped so an arrow is on
screen. Nothing before step 4 sends input.

1. `.venv/bin/python treasure.py`. Press **Start Navigator** — nothing else.
   Watch the stage strip: Find Roblox → Size window → Rebind capture → Check
   frames → Identify map → Check direction → Qualify. Note where it stops if
   it stops, and copy the sentence it prints.
2. If it reaches READY: check the LIVE READOUT. *Viewport* should read
   `1280x720 canonical` (or an honest clamped size), *Map profile* should be
   the map you actually have equipped, and *Alignment error* should be a
   number that changes as you turn. Press **Retry Automatic Setup** under
   Advanced twice more and confirm the window is sized the same way each time
   and the preview never blanks.
3. Under Advanced press **Record Diagnostics** and walk a normal route for two
   or three minutes — turning, grass, water, sand, standing under the arrow.
   This is the held-out session E-PROF needs. Press **Stop & Release All
   Input**; a trace lands beside the logs and the recording under recordings/.
4. **Only with a hand on F2**, and only in a private server on open ground:
   press **Arm Live**, focus Roblox, press **F1**. The first seconds are
   stationary while it confirms the camera mode and measures the turn
   actuator; the LIVE READOUT's *Turning by* row should change from
   `not measured` to `arrow keys` or `mouse yaw`. Then let it align and walk
   for no more than thirty seconds and press **F2**.

Send back: the trace file, the recording directory, one line about the size
the window ended up, the *Turning by* value, and anything the character did
that you did not expect.

**If step 4 goes wrong in any way, F2 is always live** and releases every key,
both turn keys, and the mouse buttons, without consulting focus.

### Windows — everything, from scratch

Unchanged from the previous pass: **no line of `platform_win.py` has ever
executed.** What this pass added to it - the Left/Right scancodes and the
`KEYEVENTF_EXTENDEDKEY` flag they require - is contract-tested from macOS
(`tests/test_platform_contract.py`) and is otherwise entirely unverified. The
extended-key flag in particular is the kind of thing that is either right or
sends numpad 4/6, and only a Windows machine can tell which.

---

## Release blocker

**G-LICENSE** is unresolved. Nothing may be pushed to a release, tagged,
published, or described as open source until the owner confirms first-party
reuse rights and records Treasure's distribution terms. The feature branch
`origin/treasure-production-navigation` is the only remote this pass writes.
