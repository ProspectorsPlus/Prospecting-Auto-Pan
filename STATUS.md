# Status

Per-phase and per-gate status. Three columns, because they fail independently
(plan §15): what can be finished on this machine, what needs macOS hardware,
and what needs Windows hardware.

Last updated: 2026-08-28. Development machine: macOS 25.4, arm64, CPython
3.13.15, Tk 9.0. **No Roblox session was operated and Live was never armed
during implementation.**

---

## What changed on 2026-08-28 (second pass)

The first pass rebuilt capture. This one rebuilt what capture feeds: the
detector, the lifecycle, the dashboard, and the control path.

| Before | After |
|---|---|
| Candidates ranked by **area**; confidence was an area-fit score. In daylight a patch of grass beat the arrow | Nine independent scored terms; the arrowhead's two-notch topology is *necessary*, not merely weighted |
| Direction from an unsigned PCA axis on a shape whose elongation is 1.3 — silent 180° flips | Signed from notch/tip topology, polarity decided by taper, PCA demoted to a fallback above an anisotropy floor |
| Preview showed frame 53545 beside a decision panel showing 53542 | One `RuntimeKey` per packet; a consumer draws only what supersedes what it has |
| Profile dropdown read `generic_saturated_v0` while the pipeline ran `yellow_map_v0` | `ProfileAuthority` owns the one profile; labels are derived from stable ids, swaps land at a frame boundary |
| One button that found *and* resized the window | `Connect Roblox…` (touches nothing) and `Fit & Lock Viewport` (bounded state machine, three stable read-backs) |
| Governor judged tiers on captured frames; stuck at 15 Hz in one session, 73 fps at 209 % CPU in another | `WARMUP/STABLE/PROBE/COOLDOWN/DEGRADED` judged on **processed** throughput; a failed probe is remembered |
| `drop 7055`, cumulative and alarming | Six separated rates, per-session counters with lifetime labelled, "superseded" with its meaning explained |
| `ru_maxrss` displayed as current memory | Current RSS and peak RSS measured separately |
| Steering was a bare PD controller with a pixel gain | `ShiftLockController` with a verified control-mode proof, a measured yaw calibration, and W as an evidence-bound lease |
| No motion guard | `ProgressGuard`: releases before confirming, abstains on ambiguity, and cannot express a maneuver |

---

## Legend

`done` · `partial` · `pending` (gate exists, not run) · `blocked` · `n/a`

---

## Phases

| Phase | Local implementation / replay | macOS commissioning | Windows commissioning |
|---|---|---|---|
| 0A Characterization | **done** — legacy transcript recorded, 14 tests green | n/a | n/a |
| 0B Platform ports and viewport | **done** — explicit coordinate spaces, connect/fit split, bounded fit state machine, geometry revisions | **partial** — geometry verified read-only; the fit half is **pending** (E-VIEW) | **pending** (E-VIEW) |
| 0C Input authority and deadman | **done** — ledger, capabilities, watchdog, real helper subprocess, 10 000 stop races clean | **pending** (real up-events, force-kill) | **pending** |
| 0D Coordinator migration | **done** — priority loop, one worker, atomic keyed packets, terminal packets on stop | n/a | n/a |
| 0E Bounded legacy services | **done** — dig/reset/dequip/pan-swap typed and bounded | **pending** (owner-observed run) | **pending** |
| 1 Shadow foundation and GUI | **done** — window-specific capture, governor FSM, redesigned dashboard, overlay modes, diagnostics drawer, bounded recorder | **partial** — Shadow verified against the live client at 57 unique fps in the previous pass; owner-observed session with the new detector **pending** | **pending** |
| 2 Offline perception | **partial** — production detector, tracker, direction estimator and a stratified evaluator all implemented and measured on rendered stress frames; **no labelled corpus of real frames exists** | **blocked** on recordings | **blocked** on recordings |
| 3 Shadow navigation and controller | **partial** — Shift-Lock FSM, yaw calibration contract, control-mode proof and progress guard implemented and gated off; closed-loop verified against a simulated camera | **pending** (E-YAW, E-SHIFTLOCK, E-STEER-CAL need physical arming) | **pending** |
| 4 One-map Live lifecycle | **not started, on purpose** — `NAVIGATE → DIG` stays unwired while E-ARRIVE is pending | **pending** | **pending** |
| 5 Multi-map lifecycle | **blocked** on E-NEXT_MAP | **pending** | **pending** |
| 6 Packaging and release | **partial** — spec, build scripts, verifier, smoke test | **pending** (clean native build) | **pending** |

---

## Experiment gates

Every gate below is **pending**. None has been run, and none may be reported
as passed on the strength of code review, rendered fixtures, or the supplied
screenshots.

| Gate | Status | What it needs that this machine cannot provide |
|---|---|---|
| E-VIEW | pending | Fitting and reading back a real Roblox client on each OS at several DPI/Retina settings. Read-only geometry inspection has been done on macOS; the **fit** half needs the owner, because resizing the game window is outside what an unattended agent may do. |
| E-ANCHOR | pending | Reviewer-labelled avatar control pivot across sessions. |
| E-FORWARD | pending | **Physically armed** bounded `W` pulses with blinded reviewer labelling. |
| E-DIR-IDEAL | pending | Manual masks plus repeatable aligned-zero outcome trials. |
| E-PROF | pending | Multi-map, multi-session labelled corpus of **real** frames with train/validation/held-out splits. Rendered frames are training stress and may never appear in a held-out split (plan §7.2). |
| E-DIR-E2E | pending | E-PROF output plus fresh held-out data. |
| E-ARRIVE | pending | Full approach/fade sequences and long negatives. |
| E-MOTION | pending | Labelled stationary / blocked / turning / lagging clips at 30–120 FPS, and an open-ground locomotion baseline from physically armed trials. |
| E-SHIFTLOCK | pending | A repeatable way to *verify* Shift Lock on each OS: either a stable on-screen cue the detector confirms, or a separately armed stationary micro-yaw check. |
| E-YAW | pending | **Physically armed** yaw pulses on each OS, with the observed rotation confirmed by perception rather than a configured multiplier. |
| E-STEER-CAL | pending | **Physically armed** manual-target trials to freeze the alignment threshold. |
| E-STEER-E2E | pending | Guarded open-ground routes with the frozen controller. |
| E-RECOVERY | pending | Capped private-server trials with labelled obstacles. *(No recovery ladder is in the control path in this pass.)* |
| E-DIG / E-LIFECYCLE | pending | Labelled dig, pan-full, completion, and interruption episodes. |
| E-NEXT_MAP | pending | Inventory/equip state evidence. Next-map automation stays off. |
| E-SKIP_MAP | pending | Separate gate; `ABANDONED` safe-stops until it passes. |
| E-PERF | pending | Measured p50/p95 capture, perception, control, and **Stop latency in wall-clock milliseconds on real hardware**, across both OSes. |

Because every gate is pending:

- **Live navigation refuses to steer**, twice over: `make_live_worker` names the
  pending gates, and `ShiftLockController` independently refuses because no yaw
  calibration exists. Passing every perception gate is still not permission to
  move a mouse.
- **Automatic profile classification is off.** Selection is explicit.
- **Recovery is off**, and no recovery ladder is reachable from the control path.
- **Next-map automation is off.**

---

## Observed local facts (not gate passes)

Measured on the development Mac and reproducible with the command shown. None
of these passes a gate.

### Detector, on rendered stress frames

`.venv/bin/python treasure.py --detector-report`

Rendered frames fitted to the owner's seven measured crops. **Training stress
only** — plan §7.2 forbids synthetic data in a held-out split, so nothing here
can pass E-PROF or E-DIR-E2E. Six episodes per stratum, 15 headings each, plus
six arrow-absent frames per episode.

| Stratum | Coverage | Median | p90 | p95 | Bias | Bad >10° | Flips |
|---|---|---|---|---|---|---|---|
| day-grass | 100 % | 0.45° | 1.40° | 1.55° | −0.00° | 0 | **0** |
| day-dirt | 100 % | 0.56° | 1.46° | 1.58° | +0.08° | 0 | **0** |
| water | 100 % | 0.48° | 1.42° | 1.92° | +0.01° | 0 | **0** |
| pale-terrain | 100 % | 0.48° | 1.68° | 1.95° | +0.09° | 0 | **0** |
| night-grass | 100 % | 0.63° | 1.13° | 1.44° | +0.06° | 0 | **0** |
| small-arrow (45 px) | 100 % | 0.88° | 2.23° | 2.49° | +0.19° | 0 | **0** |
| large-arrow (210 px) | 100 % | 0.23° | 1.52° | 1.78° | +0.02° | 0 | **0** |
| foreshortened (0.5×) | 100 % | 0.16° | 1.20° | 1.22° | +0.02° | 0 | **0** |
| blurred | 100 % | 0.53° | 1.11° | 1.48° | −0.04° | 0 | **0** |
| translucent (α 0.55) | 93.3 % | 0.32° | 1.52° | 1.78° | −0.04° | 0 | **0** |
| dim (0.5×) | 100 % | 0.49° | 1.35° | 1.54° | +0.12° | 0 | **0** |
| **same-colour clutter** | **48.9 %** | 0.54° | 77.5° | 117.6° | +5.13° | 25 % | **4** |
| **same-colour occlusion** | **51.1 %** | 1.13° | 121.0° | 132.8° | +5.41° | 34.8 % | **10** |

Read this honestly:

- Eleven of thirteen strata meet the accuracy targets outright and produce
  **zero polarity flips**. They still show as "MISSES" in the report, and
  correctly so: six episodes cannot support a <0.5 % flip bound, and the
  evaluator refuses to pretend otherwise. The failure there is sample size.
- The two same-colour adversarial strata are a **real weakness**, reported
  rather than averaged away. Solid blobs of the arrow's exact colour, some
  overlapping the arrow itself, defeat it about half the time. Real terrain may
  or may not be that adversarial — that is what E-PROF is for.

### Detector, on the owner's real crops

Six of seven detected. The strongly foreshortened one abstains on polarity,
which is the honest answer for a nearly edge-on arrow. Directions agree with
eyeballed ground truth to within 6–17°, which is inside the labelling error of
eyeballing.

### Perception cost

| Path | p50 |
|---|---|
| Full-frame reacquisition pass | 11–15.5 ms |
| Tracked region-of-interest pass | 2.2–3.3 ms |

The pipeline runs a full pass every ~20 frames and the ROI path otherwise. The
first implementation of the contrast ring took **686 ms** per frame by dilating
a full-frame mask with a 257-pixel element; bounding the kernel and windowing
it to the candidate's bounding box is what made it usable.

### Stop safety

`.venv/bin/python -m pytest -q tests/test_stop_safety.py`

- **10 000 deterministic Stop-versus-acquisition races: zero leaked leases,
  zero input edges after admission closed.**
- Every watchdog condition — focus lost, focus unknown, viewport invalid,
  capture stale, window replaced — releases held input.
- Shadow mode emits exactly zero input edges, asserted against the port
  transcript.
- A release is never focus-gated; an unconfirmed release latches and blocks
  every new press until an explicit release-only handshake succeeds.

Stop *latency in wall-clock milliseconds* is **not** measured here. These tests
run on a virtual clock and bound the release **work** (edges emitted), not the
time it takes on real hardware. The <100 ms p95 requirement belongs to E-PERF
and is pending.

### Cadence governor

`.venv/bin/python -m pytest -q tests/test_governor.py`

All five required traces pass: 120 captured / 57 processed converges to 60; a
genuine 120 Hz workload stays at 120; a 60-capped source probes once and stops
oscillating; a 250 ms transient causes no permanent downshift; sustained
over-age evidence immediately blocks Live.

### Soak

`.venv/bin/python treasure.py --soak 10`

Ten bounded minutes of the real capture, perception and telemetry path against
a synthetic source. Results are recorded in the pass report; the check is that
threads, file descriptors, buffer pool occupancy and the event log stay bounded
and that the RSS slope after warmup is under the provisional 1 MB/min target.

---

## Native checks the owner must run

The agent could not do any of these. Each needs a real Roblox session and, for
the armed ones, a human watching.

### macOS

1. Grant Accessibility **and** Screen Recording to the launching process.
2. `.venv/bin/python treasure.py`, with Roblox open and windowed (not native
   fullscreen). Press **Connect Roblox…**. The ROBLOX card should read
   `Connected` and the CAPTURE card should show a real frame rate.
3. Press **Fit & Lock Viewport**. Expect either `canonical_verified` with a
   client of `1280x720 pt` and a backing size of `2560x1440 px` on a 2× display,
   or `achieved_clamped` with the real achieved numbers. **A clamp is a valid
   answer** — record it. Roblox enforces a minimum window size.
4. `.venv/bin/python treasure.py --calibrate` → re-derive every `TreasurePixels`
   value. It refuses to suggest baking a value while the viewport is
   non-canonical, and says so inline.
5. Start Shadow with the green map equipped. Confirm the detector locks the
   arrow, that Full Diagnostics shows two notches on it, and that the direction
   arm points where the arrow points. **This is the E-PROF evidence-gathering
   step**: run Start Diagnostic Recording alongside it.
6. Confirm capture continues while the dashboard itself is frontmost.
7. Only then consider F4/F5/F6 with Roblox focused, watching for a stuck input.

### macOS, physically armed (do not run unattended)

8. **E-SHIFTLOCK**: with the character stationary and Shift Lock on, arm and
   issue one bounded micro-yaw pulse. Confirm the camera rotates and the
   character does not translate. Repeat with Shift Lock off and confirm the
   difference is observable. That difference is the verification method.
9. **E-YAW**: with `W` released, issue bounded positive and negative yaw pulses
   at several magnitudes, ten repeats each. Record sign, degrees per mouse
   unit, response delay, minimum effective movement, linear range, saturation,
   repeatability and reversal backlash. **Confirm each from perception**, not
   from the number that was sent.
10. **E-STEER-CAL**: manual-target trials to find the largest alignment
    threshold that still gives acceptable route outcomes. Freeze it before any
    held-out perception evaluation.

### Windows — everything, from scratch

No code in `platform_win.py` has ever executed. In addition to the macOS list:

1. Generate `requirements-windows.lock` on Windows (see that file's header).
2. Confirm Per-Monitor V2 is actually in effect — the fit message reports the
   mechanism that succeeded.
3. Repeat the geometry and capture checks at 100 / 125 / 150 / 175 / 200 %
   scaling, and on a secondary monitor with a negative origin.
4. Verify `WindowsPrintWindowSource` returns client content and not a black
   frame; some GPU-composited windows need Windows Graphics Capture instead,
   which is the documented next step (DECISIONS.md D-018).
5. Re-run `mypy` on Windows: `platform_win.py` has two Win32-only names exempted
   from checking on macOS.

---

## Release blocker

**G-LICENSE** is unresolved. Nothing may be pushed, tagged, published, or
described as open source until the owner confirms first-party reuse rights and
records Treasure's distribution terms.
