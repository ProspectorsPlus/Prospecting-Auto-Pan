# Status

Per-phase and per-gate status. Three columns, because they fail independently
(plan §15): what can be finished on this machine, what needs macOS hardware,
and what needs Windows hardware.

Last updated: 2026-08-28. Development machine: macOS 25.4, arm64, CPython
3.13.15, Tk 9.0. **No Roblox session was operated and Live was never armed
during implementation.**

## What changed on 2026-08-28

The capture foundation was rebuilt. In short: coordinate spaces are now types
rather than conventions, capture is window-specific and event-driven, and Shadow
shows the reasoning behind every frame.

| Before | After |
|---|---|
| A device-pixel rect handed to a logical-unit API; the capture contained the desktop, the Dock, and blank padding | Four named coordinate spaces, transforms that refuse to compose across them, and a capture containing only the Roblox client |
| Pin requested 1280×720 **device pixels**, became a 640×360-point window, and was clamped | Pin requests 1280×720 **logical** units; a clamp is reported once as a truthful non-canonical state |
| "Viewport ok" alongside "unsupported viewport size" | One `ViewportState` every consumer reads |
| ~10–20 Hz polled loops | Push pipeline: **57 unique fps** sustained with perception, **111 fps** capture-only |
| Preview showed a raw frame; telemetry published `None` for every observation | One `DiagnosticObservation` per frame, drawn as arms, arc, contour, and candidates |
| Perception 12.3 ms | 5.2 ms (deduplicated segmentation pass, bounded ROI tracking) |

## Legend

`done` · `partial` · `pending` (gate exists, not run) · `blocked` · `n/a`

---

## Phases

| Phase | Local implementation / replay | macOS commissioning | Windows commissioning |
|---|---|---|---|
| 0A Characterization | **done** — legacy transcript recorded, 14 tests green | n/a | n/a |
| 0B Platform ports and viewport | **done** — explicit coordinate spaces, logical-unit pinning, truthful `ViewportState`; B1/B9/B10/B11 and D-017 fixed | **partial** — geometry verified read-only against the live client; pin/read-back **pending** (E-VIEW) | **pending** (E-VIEW) |
| 0C Input authority and deadman | **done** — ledger, capabilities, watchdog, real helper subprocess tested, B5/B8 fixed | **pending** (real up-events, force-kill) | **pending** |
| 0D Coordinator migration | **done** — priority loop, one worker, B3/B4/B6/B7/B13 fixed | n/a | n/a |
| 0E Bounded legacy services | **done** — dig/reset/dequip/pan-swap typed and bounded, B2/B12 fixed | **pending** (owner-observed run) | **pending** |
| 1 Shadow foundation and GUI | **done** — window-specific capture, event-driven pipeline, cadence governor, per-frame diagnostics, Shadow overlay, recorder with quarantine, cross-launch unsafe-release recovery | **partial** — Shadow verified against the live client at 57 unique fps; owner-observed session **pending** | **pending** |
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
| E-VIEW | pending | Pinning and reading back a real Roblox client on each OS at several DPI/Retina settings. Read-only geometry inspection has been done on macOS (title-bar inset measured, client rect derived, transforms round-trip); the **pin** half needs the owner, because resizing the game window is outside what an unattended agent may do. |
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
| Full pipeline, capture + consume (ScreenCaptureKit) | 58.0 / 85.2 / **111.0** unique fps at 60 / 90 / 120 Hz requests; 0 duplicates, 0 drops | `treasure.py --capture-probe` |
| Capture latency | p50 4.6–6.6 ms, p95 6.4–8.1 ms | same |
| Memory across tiers | RSS 97–104 MB, flat from 60 to 120 Hz | same |
| Quartz window fallback | 28.7 unique fps; capture p50 25.3 ms plus 11.1 ms CPU normalize | forced-source probe |
| `mss` desktop fallback | ~58 fps ceiling, and **not** window-specific | backend comparison |
| Live Shadow, dashboard running | 57 unique fps captured **and** processed, 43 fps preview, end-to-end p50 9.3 ms / p95 11.6 ms | Start Shadow and read the PIPELINE panel |
| Perception cost | 5.2 ms p50 (was 12.3 ms before deduplicating the segmentation pass and adding ROI tracking) | PIPELINE panel |
| Capture content | contains only the Roblox client — no desktop, Dock, or padding | screenshot of the Shadow view |

**E-PERF is still pending.** These are capture, perception, and preview costs on
one machine with one window size; the gate additionally covers control latency,
Stop latency, duplicate/stale rates across conditions, and both OSes.

## Native checks the owner must run

### macOS — E-VIEW, the pin half

The agent could not do this: resizing the running game window is outside what
it may do unattended, and the whole point of the check is that a **real** pin
reads back correctly.

1. Grant Accessibility **and** Screen Recording to the launching process.
2. `.venv/bin/python treasure.py`, with Roblox open and windowed (not native
   fullscreen). The VIEWPORT card should read `adopted noncanonical`.
3. Press **Pin Window**. Expect the message to report a client of
   `1280x720 pt`, a backing size of `2560x1440 px` on a 2× display, and whether
   the title-bar inset was `measured` or `provisional fallback`. The VIEWPORT
   card should change to `canonical verified`.
   * **If it reports a clamp instead**, that is the real answer: record the
     achieved size. Roblox enforces a minimum window size, and 1280×720 points
     is close to it. The application stays usable in `adopted noncanonical`,
     and the canonical raster is produced by letterboxing.
4. `.venv/bin/python treasure.py --calibrate` → re-derive every
   `TreasurePixels` value. It refuses to suggest baking a value while the
   viewport is non-canonical, and says so inline.
5. Start Shadow. Confirm the PIPELINE panel shows `screencapturekit`, at least
   30 unique fps, and that the SHADOW VIEW contains only the game.
6. Confirm capture continues while the dashboard itself is frontmost - that is
   the property the ScreenCaptureKit backend exists for.
7. Only then consider F4/F5/F6 with Roblox focused, watching for a stuck input.

### Windows — everything, from scratch

No code in `platform_win.py` has ever executed. In addition to the macOS list:

1. Generate `requirements-windows.lock` on Windows (see that file's header).
2. Confirm Per-Monitor V2 is actually in effect - the pin message reports the
   mechanism that succeeded.
3. Repeat the geometry and capture checks at 100 / 125 / 150 / 175 / 200%
   scaling, and on a secondary monitor with a negative origin.
4. Verify `WindowsPrintWindowSource` returns client content and not a black
   frame; some GPU-composited windows need
   Windows Graphics Capture instead, which is the documented next step
   (DECISIONS.md D-018).

## Release blocker

**G-LICENSE** is unresolved. Nothing may be pushed, tagged, published, or
described as open source until the owner confirms first-party reuse rights and
records Treasure's distribution terms.
