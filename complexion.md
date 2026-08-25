# complexion.md — pruning this repo down to a small branch

Goal: figure out exactly which of the 294 tracked files a smaller fork actually needs, so
you can `git rm` / not-carry-forward the rest. Two separate levers, don't conflate them:

- **Directory-level pruning** — delete whole files/folders that the bot doesn't import at
  all (GUI, trust UI, tests, docs, packaging, Windows mirror). Mechanical, safe, verified
  against actual imports below. Gets you from 294 files to 6–7 (§1), cross-platform.
- **In-file pruning** — `prospector_engine/engine.py` is one 9,735-line file that mixes the
  loop you want with Studio's scripting VM, OCR earnings, geode/relic modes. Deleting other
  *files* does nothing to this file's size — shrinking it means cutting sections out of it
  by hand. Covered in §4.

Everything below was checked against actual `import`/`grep` results in this repo, not
assumed from file names.

## 1. The keep-list for a headless bot branch

Feature target ("Treasure Mode"): see the game, fire inputs, autopan + sandshake, the
built-in recovery/fallback ladder, Discord status pings. Cross-platform (mac + Windows).
No geode mode, no pan counters, no earnings/OCR tracking — nothing gets tracked yet; that's
a later, separate layer. One fixed window geometry, identical on every machine — see §3.

```
prospector_engine/__init__.py
prospector_engine/engine.py        # the loop, recovery calls, webhook — see §4 to shrink it
prospector_engine/recovery.py      # REQUIRED — engine.py calls _bind_recovery_and_flows()
prospector_engine/flows.py         #   unconditionally every run start (engine.py:9467).
                                    #   Default-path behavior is a no-op passthrough, but the
                                    #   import is not optional unless you patch engine.py.
prospector_engine/platform_mac.py  # BOTH — cross-platform means both stay, unlike a
prospector_engine/platform_win.py  #   single-OS fork. Each needs a new pin_window() (§3).
prospecting_old.py                 # CLI entrypoint shim (33 lines)
```

**7 files, both platforms.** That's the whole runtime dependency graph for `python3
prospecting_old.py monitor` — verified by tracing every `import prospector_engine.X` in
`engine.py`:

| Module | Import site | Conditional on |
|---|---|---|
| `recovery.py`, `flows.py` | `engine.py:3368-3369`, called from `9467` | **Nothing — always runs.** `build_default_program()` returns the stock ladder; `RecoveryRuntime.poll()` is a no-op when no `RECOVERY_JSON` override is set, but the module must be importable. |
| `client.py`/`ipc.py`/`protocol.py` | `engine.py:9386` | Only when `--ipc` or `--sim` is passed (`engine.py:9646`). Legacy CLI mode (`monitor`) never touches these. **Drop.** |
| `vision.py` | `engine.py:7078-7116` | Only inside the Studio v3 "find image" block handler. **Drop.** |
| `recorder.py` | not imported by `engine.py` at all | Only used by the GUI's Studio macro-recorder. **Drop.** |
| `cycleplan.py` | not imported by `engine.py` at all | Standalone introspection/doc-generation tool. **Drop.** |
| `sensing.py` | not imported by `engine.py`'s runtime path — used by the app/calibration UI | Its whole job is ratio-based calibration for an *unknown* window size. Treasure Mode has no unknown window size — **drop unconditionally**, not "if hardcoding" (see §3). |
| `settings.py` | optional | atomic JSON writer; only needed if something (e.g. the webhook URL) should persist between runs. No config file is required to boot either way — `load_config()` (`engine.py:98`) silently no-ops when `prospecting_config.json` is missing, every setting already has a module-level default. Drop it and the keep-list is **6 files** if you're fine with in-code constants. |

Note `windows/` (root dir) and `prospector_engine/platform_win.py` are two different
things — the first is the packaging mirror (dropped, §2), the second is the actual Windows
input/window-lookup module (kept, cross-platform requirement).

## 2. Everything else — the delete-list

| Delete | Files | Why it's safe |
|---|---:|---|
| `prospecting_app.py`, `prospecting_ui.py` | 2 | pywebview GUI host + fallback browser UI. Nothing in `prospector_engine/` imports either — the GUI *spawns* the engine as a subprocess, the engine never calls back into the GUI's code. |
| `lite_trust.py`, `lite_onboarding.py`, `lite_diagnostics.py`, `prospecting_assistant.py` | 4 | permissions wizard, first-run flow, in-app health checks, offline tuning chatbot — all GUI-only, none imported by the engine. |
| `prospecting_core.py`, `prospecting_selftest.py` | 2 | a standalone numpy perception experiment, not imported by the real engine or app anywhere. Dead weight regardless of tier. |
| All `*_tests.py`, `*_sim.py`, `tour_check.py`, `studio_conformance.py`, `engine_lite_drive.py`, `engine_pacing.py`, `engine_characterization.py` | 21 | correctness harness for the *shipped product* (Studio conformance, tour lockstep, IPC contract, dev pacing world). None are imported by `engine.py`'s runtime path — they import *it* to test it. |
| `windows/` | 14 | byte-identical mirror of the mac app + Windows-only build assets, for the dual-OS installer. If you're forking to one OS, you don't need a mirror to keep in lockstep. |
| `packaging/`, `.github/workflows/`, `build_dmg.command`, `prospector_lite_mac.spec`, `package.json`/`package-lock.json` | ~35 | installer/CI/release plumbing for shipping to strangers. `package.json` is just `jsdom`, a dev-dependency of `wizard_ui_tests.js`. |
| `docs/` | ~130 | the public wiki + release/security audit reports. Pure documentation site. |
| `assets/` | 22 | app icons + calibration reference screenshots, for the GUI's onboarding flow. |
| `engine_goldens/`, `engine_scenarios/` | 57 | fixture data consumed only by the deleted test suites. |
| Root `*.md` (README, PRIVACY, SECURITY, THREAT_MODEL, TRUST_CENTER, CHANGELOG, DECISIONS, ...) | ~31 | compliance/policy paperwork for a public release. Write your own one-paragraph README instead. |
| `windows/prospecting_config.json`, `prospecting_prices.json`, `icon.png`, `LICENSE_CHOICE_REQUIRED.md` | 4 | shipped defaults / branding — irrelevant to a personal script, and neither is required to boot (§1). |

**~280 of 294 files.** That lines up with the 6-7 file keep-list above — this repo's file
count is almost entirely product/release surface area, not engine surface area.

## 3. Treasure Mode: one fixed window geometry for everyone

The ask: mac or Windows, 720p/1080p/1440p monitor, doesn't matter — every user's Roblox
window ends up the same size in the same corner, so one hardcoded pixel-coordinate set is
correct for everyone and nobody ever calibrates. That's a stronger claim than "fixed
1280×720" alone — it requires the app to actively **place** the window, not just assume
it's already there. Two things follow from that:

**a) This needs new code that doesn't exist today.** The wiki's "auto-calibrate" is a
narrower, different thing worth not confusing with this: it auto-places *pixel coordinates*
(`apply_auto_calibrate()`, ratio × window rect) onto whatever window geometry already
exists — it never touches the window itself. The wiki says so directly: "Keep the window
where it is once you start calibrating," and a resize always goes Stale and demands
recalibration rather than being corrected for (`docs/wiki/calibration.html`). I grepped the
whole engine for it — there is currently no window move/resize call anywhere in
`prospector_engine/` (only `find_roblox_window()`/`find_roblox_rect()`, which *read*
position, never set it). To build: a `pin_window(w=1280, h=720)` added to each platform
file.
- **macOS** (`platform_mac.py`): Accessibility API — `AXUIElementSetAttributeValue` on the
  window's `kAXPositionAttribute`/`kAXSizeAttribute` (the same mechanism Rectangle/Moom use
  to move third-party windows; Roblox's window is a normal `NSWindow`, nothing special about
  it). This is not hypothetical for this codebase specifically: `lite_trust.py` already
  imports `ApplicationServices`/`HIServices` — the pyobjc framework that carries this exact
  API — for its `AXIsProcessTrusted()` preflight, and the capability registry lists
  `input_control` as `REQUIRED_FOR_CORE`, category **Accessibility**
  (`lite_trust.py:149-206`), needed today just to post synthetic key/mouse events. Setting
  window position/size needs that identical grant — no new permission category, no new
  prompt, it rides a grant the app can't run without regardless. Steps: pull Roblox's PID
  off the existing `CGWindowListCopyWindowInfo` scan (`kCGWindowOwnerPID`, already in the
  dict `find_roblox_rect()` reads but currently discards) → `AXUIElementCreateApplication(pid)`
  → its window element via `kAXWindowsAttribute`/`kAXMainWindowAttribute` →
  `AXUIElementSetAttributeValue` with an `AXValueCreate`-wrapped `CGPoint`/`CGSize`. One
  unit gotcha: AX position/size are in **points**, not physical pixels — same convention
  `CGWindowBounds` uses before `get_scale()` scales it up — so divide the target physical
  size by `get_scale()` first. One real caveat: native macOS fullscreen (green-button, not
  in-game borderless) doesn't respond to AX resize; `pin_window()` should detect and bail
  with a clear message rather than silently no-op.
- **Windows** (`platform_win.py`): `user32.SetWindowPos`. Needs `AdjustWindowRectEx` (or an
  equivalent client-rect probe) to convert "I want a 1280×720 *client area*" into the right
  outer window rect, since title-bar height isn't 0 and isn't the same as mac's.
- **The DPI trap — this is the actual risk to "same pixels for everyone":** a naive resize
  to "1280×720" on a Windows box running at 150% scaling, or a Retina mac display, will not
  produce the same physical pixel grid as an unscaled display. Both platform files already
  have `get_scale(sct)` (`platform_mac.py:74`, `platform_win.py:73`) for exactly this — the
  pin logic must call it and work in physical pixels throughout, and on Windows the process
  needs per-monitor DPI awareness declared at startup (`SetProcessDpiAwarenessContext`)
  or Windows will silently lie about the window's own rect.

**b) Drop the ratio-based calibration, keep the origin-offset shim.** `sensing.py` and
`apply_auto_calibrate()` (`engine.py:859-886`, ratio × live window size) solve "the window
could be anywhere, any size" — no longer the problem, delete outright. But
`apply_window_offset()` (`engine.py:889-910`) is worth keeping: it shifts a fixed
coordinate set by however far `find_window_origin()` says the window drifted from where it
was pinned. That's cheap (~20 lines, already written) insurance against `pin_window()`
landing a few px off, or the user dragging the window mid-run — belt-and-suspenders on top
of actively placing the window, not a replacement for it.

Net: ship one baked-in coordinate set (`CAP_FULL_PIXEL`, `PAN_PIX`, `DEPOSIT_PIX`,
`SHAKE_PIX`, `DIG_TRIGGER_PIXEL`, `TERRAIN_PIXEL` — the same ~6 constants `engine.py`
already reads as globals), call `pin_window()` once at launch, `apply_window_offset()`
every run start, and never show a calibration screen at all.

## 4. Cut everything not "see the game, fire inputs, fall back, notify"

You listed geodes and pan counts as explicit examples of "gone," not just "unused" — so
this goes further than §1's boot-time trim: physically remove tracking from `engine.py`,
don't just leave it uncalled. By section (verified line ranges):

| Cut | Lines | ~Size |
|---|---|---:|
| Prospector Studio interpreter (PPScript v2/v3/v4 IR, parallel-effect table) | 4701–~9200 | ~4,500 |
| `EarnTracker`/`FindsWatcher` + OCR (money/rarity/pan-count tracking) | 1655–2988 | ~1,330 |
| Geode mode, `_shards_dig`, `_geode_*` | 4117–4391 | ~275 |
| `RelicScheduler` (timed item-use) | 4597–4701 | ~100 |
| `SessionStats` (pans/hour/runtime counters) | inside 1521–1629 | ~100 |
| **Kept:** config/globals, `Detector` (raw perception), minimal `State` (running/stopped +
  what the recovery ladder needs), webhook, cycle primitives (`dig_once`…`fill_to_full`),
  `act`/`recover`/`break_out` supervisor, `fortune_river_recover`/`starfall_river_recover`/
  `safe_stop`, autopan toggle, CLI main | ~150–800, 1274–1521, 3006–4117 (minus EarnTracker),
  4473–4597, 9370–9735 | **~1,700** |

The Discord side shrinks with it: `post_webhook`'s `Stats` field
(pans/per-hour/runtime/recoveries, `_webhook_payload`) has nothing to report once
`SessionStats` is gone — send bare status strings ("started" / "recovering" / "stopped:
reason") instead. That's a smaller payload builder, not a missing feature.

**Design note for "tracking gets added later, on its own":** keep that boundary real, not
aspirational — have the loop emit its own state transitions somewhere (a log line, an
in-memory event, whatever), and let a future tracking layer *observe* that stream rather
than reaching back into the loop's internals to compute counts. `emit_event()`
(`engine.py:1122`) already exists as exactly this seam — a future stats module can hook it
without the core loop knowing it exists.

This is surgery, not deletion — these sections interleave through shared globals (`State`,
`_rv()`, the config dict), so pulling them out means also removing every call site that
dispatches into them (`act()`'s mode branch for Studio/geode, the OCR poll in the main loop,
relic ticks). Budget it as a rewrite pass.

## 5. Bottom line

| | Files kept | What changed |
|---|---:|---|
| This repo, as-is | 294 | full product: GUI, trust UI, Studio IDE, OCR, two installers, public wiki, test suite |
| §1+§2 pruned (directory-level only) | 6–7, both platforms | same `engine.py` (9,735 lines) running headless via CLI; loses GUI/Studio-editor/trust-wizard/installers, keeps every runtime feature but window size/position is whatever the user has (no pinning yet) |
| + §3 (Treasure Mode window pin) | 6–7 + new `pin_window()` in each platform file | identical window geometry and identical pixel meaning for every user, mac or Windows, any monitor — the actual cross-user consistency goal |
| + §4 (also trim `engine.py`) | 6–7 | `engine.py` cut to ~1,700 lines: only see-game / fire-inputs / autopan / shake / fallback / bare-status-webhook remain; geode/relic/Studio/OCR/pan-counts physically removed, tracking deferred behind an `emit_event()` seam |

§2 is a `git rm` pass you could script in an afternoon and verify with `python3 -c "import
prospector_engine.engine"`. §3 is new code (small, but the DPI handling is the part worth
not rushing). §4 is the part that actually makes the *engine* small, and it's a rewrite,
not a prune.
