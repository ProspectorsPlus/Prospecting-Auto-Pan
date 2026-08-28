# Status

Per-phase and per-gate status. Three columns, because they fail independently
(plan §15): what can be finished on this machine, what needs macOS hardware,
and what needs Windows hardware.

Last updated: 2026-08-27. Development machine: macOS 25.4, arm64, CPython
3.13.15, Tk 9.0. **No Roblox session was operated and Live was never armed
during implementation.**

## Legend

`done` · `partial` · `pending` (gate exists, not run) · `blocked` · `n/a`

---

## Phases

| Phase | Local implementation / replay | macOS commissioning | Windows commissioning |
|---|---|---|---|
| 0A Characterization | **done** — legacy transcript recorded, 14 tests green | n/a | n/a |
| 0B Platform ports and viewport | **done** — instance ports, canonical client rect, B1/B9/B10/B11 fixed | **pending** (E-VIEW) | **pending** (E-VIEW) |
| 0C Input authority and deadman | **done** — ledger, capabilities, watchdog, real helper subprocess tested, B5/B8 fixed | **pending** (real up-events, force-kill) | **pending** |
| 0D Coordinator migration | **done** — priority loop, one worker, B3/B4/B6/B7/B13 fixed | n/a | n/a |
| 0E Bounded legacy services | **done** — dig/reset/dequip/pan-swap typed and bounded, B2/B12 fixed | **pending** (owner-observed run) | **pending** |
| 1 Shadow foundation and GUI | **done** — capture, telemetry, recorder with quarantine, dashboard, diagnostics, cross-launch unsafe-release recovery | **pending** (manual recording session) | **pending** |
| 2 Offline perception candidates | **partial** — candidates implemented and unit-tested on synthetic frames; **no labelled corpus exists** | **blocked** on recordings | **blocked** on recordings |
| 3 Shadow navigation and controller | **partial** — FSM, controller, recovery ladder implemented and gated off; deterministic replay works (`--replay`) | **pending** (E-YAW, E-STEER-CAL need physical arming) | **pending** |
| 4 One-map Live lifecycle | **not started, on purpose** — the plan enables a transition only when its evidence is validated, so `NAVIGATE → DIG` is not wired up while E-ARRIVE is pending | **pending** | **pending** |
| 5 Multi-map lifecycle | **blocked** on E-NEXT_MAP | **pending** | **pending** |
| 6 Packaging and release | **partial** — spec, build scripts, verifier, smoke test written | **pending** (clean native build) | **pending** |

---

## Experiment gates

Every gate below is **pending**. None has been run, and none may be reported
as passed on the strength of code review, synthetic fixtures, or the supplied
screenshots.

| Gate | Status | What it needs that this machine cannot provide |
|---|---|---|
| E-VIEW | pending | A real Roblox client to pin and read back, on each OS, at several DPI/Retina settings. |
| E-ANCHOR | pending | Reviewer-labelled avatar control pivot across sessions. |
| E-FORWARD | pending | **Physically armed** bounded `W` pulses with blinded reviewer labelling. |
| E-DIR-IDEAL | pending | Manual masks plus repeatable aligned-zero outcome trials. |
| E-PROF | pending | Multi-map, multi-session labelled corpus with train/validation/held-out splits. |
| E-DIR-E2E | pending | E-PROF output plus fresh held-out data. |
| E-ARRIVE | pending | Full approach/fade sequences and long negatives. One screenshot is not a corpus. |
| E-MOTION | pending | Labelled stationary / blocked / turning / lagging clips at 30–120 FPS. |
| E-YAW | pending | **Physically armed** yaw pulses on each OS. |
| E-STEER-CAL | pending | **Physically armed** manual-target trials to freeze the deadband interval. |
| E-STEER-E2E | pending | Guarded open-ground routes with the frozen controller. |
| E-RECOVERY | pending | Capped private-server trials with labelled obstacles. |
| E-DIG / E-LIFECYCLE | pending | Labelled dig, pan-full, completion, and interruption episodes. |
| E-NEXT_MAP | pending | Inventory/equip state evidence. Next-map automation stays off. |
| E-SKIP_MAP | pending | Separate gate; `ABANDONED` safe-stops until it passes. |
| E-PERF | pending | Measured p50/p95 capture, perception, control, and Stop latency. |

Because every gate is pending:

- **Live navigation refuses to steer.** `make_live_worker` returns a failure
  naming the pending gates instead of emitting a movement command.
- **Automatic profile classification is off.** Selection is explicit.
- **Recovery is off.** Contact evidence, if it ever fired, would `ABANDON`.
- **Next-map automation is off.** `TREASURE_COMPLETE` ends the session.

---

## Observed local facts (not gate passes)

These were measured on the development Mac and are reproducible. They inform
planning; none of them passes a gate.

| Measurement | Value | How to reproduce |
|---|---|---|
| macOS title-bar inset for the Roblox window | 28.0 pt, **measured** from the window's own close-button geometry | dashboard **Pin Window** reports `measured` vs `provisional-fallback` |
| Client rect derived from that inset | `origin=(0, 134) px, size=(3600, 2108) px, scale=2.0` (unpinned window) | `treasure.py --calibrate` |
| Capture cost, 1280×720 physical px | p50 ~19–23 ms, p95 ~21–33 ms | `treasure.py --capture-probe` |
| Capture cost, 2560×1440 | p50 ~33–37 ms, p95 ~36–47 ms | same |
| Capture cost, 3600×2108 (unpinned) | p50 ~70–81 ms, p95 ~82–158 ms | same |
| Shadow against an unpinned window | releases every tick on `stale-frame`, arrow abstains `unsupported-viewport-size` | Start Shadow without pinning |

The last two rows together are why the canonical pin matters: capture cost
scales with pixel count, and only the canonical size leaves room for perception
inside the 40 ms budget. **E-PERF is still pending** — it covers perception,
control, preview, and Stop latency as well.

## Native checks the owner must run

macOS:

1. Grant Accessibility **and** Screen Recording to the launching process.
2. `.venv/bin/python treasure.py` → **Pin Window** with Roblox open and
   windowed. Confirm the reported client size is `1280x720` within one pixel
   and note whether the title-bar inset was `measured` or `provisional-fallback`.
3. `.venv/bin/python treasure.py --calibrate` → re-derive every
   `TreasurePixels` value in the canonical client basis, then update
   `prospector_engine/engine.py` and flip its status off `PENDING`.
4. Start Shadow and confirm the preview, readiness cards, and event log update
   with Roblox frontmost and with Tk frontmost.
5. Only then consider F4/F5 with Roblox focused, watching for a stuck input.

Windows: everything above, plus 100/125/150/200% DPI, plus confirming
Per-Monitor V2 is actually in effect (the dashboard shows the mechanism).

## Release blocker

**G-LICENSE** is unresolved. Nothing may be pushed, tagged, published, or
described as open source until the owner confirms first-party reuse rights and
records Treasure's distribution terms.
