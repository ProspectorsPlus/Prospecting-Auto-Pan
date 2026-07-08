# Prospectors Plus — Complete Knowledge Base & Handoff

> **Purpose of this document.** This is a full context dump for anyone (human or AI) picking up the **Prospectors Plus** project cold. It covers the Roblox game *Prospecting*, its mechanics and glitches, exactly how the macro works, every feature that exists and why, every hard problem we hit and how we solved it, and the non-negotiable rules learned the hard way. If you are a new model starting fresh: **read the "Hard rules / invariants" section first, then the "War stories" section** — those two prevent you from re-breaking things that took many iterations to get right.

**Project:** `Prospectors Plus` — an invite-only auto-pan macro for the Roblox game **Prospecting**.
**Current staged version:** `3.0.0` (not yet pushed/published at time of writing).
**Platforms:** macOS **and** Windows, kept byte-for-byte in lockstep except platform-specific input/launcher code.
**Distribution:** GitHub — account `ProspectorsPlus`, repo `Prospecting-Auto-Pan`. GitHub Pages hosts the site + `version.json`; GitHub Releases host the Windows installer.
**Constraints that never change:**
- Cannot `git push` from the build sandbox — the **user pushes locally**.
- New engine features are **opt-in / default-off** wherever possible.
- Mac and Windows source files must stay **in lockstep**.
- Never expose internal `/sessions/...` sandbox paths to the user.

---

## 1. The Game: *Prospecting* (Roblox)

Prospecting is a mining/panning game. The core gameplay loop the macro automates ("auto-pan") is:

1. **Dig** on land to fill your **pan** with material. Each dig adds to a **capacity bar**.
2. When the pan is **full**, walk **backward into the water**.
3. **Shake** the pan in the water to empty it (this is what yields resources/shards/money).
4. Momentum/walking carries you back onto **land**; **dig** again. Repeat forever.

### 1.1 On-screen elements the macro reads
- **Capacity bar (a.k.a. "Pan Fill" bar):** a horizontal bar near the bottom-center of the screen. It is **gray/dark when empty** and fills **yellow/gold** as you dig. When it's **entirely yellow, the pan is FULL**. This bar is the single most important signal — see "Capacity is ground truth" below.
- **Prompt text (white, bottom-center):** the game shows a context prompt:
  - **"Pan"** — you are standing in the **water** (can shake).
  - **"Collect Deposit"** — you are on **land/dirt** (can dig).
  - **"Shake"** — shown while a shake is animating.
  - Next to the prompt text is a small **mouse/click icon** to its left (used by the guided calibrator to locate the prompt).
- **Dig skill bar:** when you hold to dig, a bar sweeps; releasing when the marker is on the **green zone** = a **"perfect dig"** (higher yield). The bar is usually too fast to hit reliably by pixel, so the macro mostly uses **timed holds** instead of perfect-dig.

### 1.2 Player stats (entered by the user, used by the Coach's formulas)
- **Luck** — affects drop quality (not used in timing math).
- **Capacity** — total pan capacity.
- **Dig Strength** — how much capacity each dig fills. **1.5 × Dig Strength = capacity filled per dig.**
- **Dig Speed (%)** — how fast a dig completes. Drives dig-hold length: **hold ms = 55000 / DigSpeed%**.
- **Shake Strength** — multiplies shake output.
- **Shake Speed** — governs shake "rolls per second" (see formula).
- **Walk Speed** — movement speed (affects travel legs; see the "0.62 s overhead" note).

### 1.3 Locations / items
- **Fortune River** — a specific map location the macro can fast-travel back to when it gets lost (see "Fortune River recovery"). In the Fast-Travel list, the **"Fortune River" row text is pink**.
- **Warp / Fast-Travel device** — opened via **hotkey slot 4**. The macro double-clicks slot 4 to open the Fast-Travel menu.
- **Relics / Spirit Orb / Vineheart** — usable buff items placed on hotkey slots; the macro can press a slot every N minutes (relic timer). Several saved builds are named after the relic they use (e.g. "Shards Spirit Orb", "Shards Vineheart").
- **Treasure chests** — an alternate collectible; "Treasure mode" digs then strafes side-to-side to collect, no shaking.

### 1.4 Game GLITCHES (critical — the macro is largely built around these)
These are real, observed game bugs that make naive automation fail:

1. **Shake glitch (the big one):** sometimes the game **fails to register a shake**. The pan doesn't empty, but the macro can *think* everything is fine and just walk back and forth doing nothing. Left unhandled this wastes ~30 s per occurrence. Detected via "no capacity progress" and "shake fails" (see Recovery system).
2. **Sticky "Shake" cue:** the **"Shake" prompt can persist** on screen after the shake is actually done. Therefore you **cannot trust the Shake cue** to know when shaking finished — you must read the **capacity bar** (empty = done).
3. **Cue flicker (land ↔ water):** the **"Collect Deposit"** and **"Shake"** cues glitch/flicker. The **"Pan"** cue (in-water) is the most reliable "where am I" signal. This is why the decision logic is **capacity-primary** and only trusts the Pan cue for "am I in the water".
4. **False first "Collect Deposit" after a shake / crossing water:** when returning across the water to the next shore, the game can briefly show "Collect Deposit" before you've actually crossed. This caused Fortune-River recovery to restart on the wrong shore; fixed by requiring a **sustained Pan (water) crossing** before accepting the next "Collect Deposit".

### 1.5 Platform input quirk (macOS)
- On macOS, a **synthetic *held* mouse press is NOT sustained** inside Roblox — a hold silently does nothing. **Rapid synthetic clicks DO register.** This is why the shake is implemented as a **rapid click stream**, not a hold. (Windows can hold, but the macro uses the same click-stream approach for parity.) This single fact caused several shake regressions when someone "optimized" the shake into a hold — see War Stories.

---

## 2. The Macro: Prospectors Plus — high-level

A native desktop app (its own window) that drives the game by reading the screen and sending input. It is **not** a Roblox exploit/injector — it's screen-reading + synthetic input at the OS level. Key properties:

- **Two processes at runtime:** the **app/UI** (pywebview window) launches the **engine** (`prospecting_old.py`) as a subprocess and pumps its stdout for live logs, stats, and telemetry.
- **Screen sensing:** `mss` for screen capture, `numpy` for pixel sampling.
- **Input:**
  - macOS → **Quartz CGEvent** at HID level, using macOS **virtual keycodes** (e.g. `KEY_A=0`, `KEY_D=2`, plus W/S).
  - Windows → **ctypes SendInput** using **scancodes**.
- **Access control:** invite-only via **access codes** (a gate screen in the app; codes list in `ACCESS_CODES_PRIVATE.txt`, published hashes in `docs/codes.json`).
- **Auto-update:** the app checks `docs/version.json`; if a newer (or `critical`) version exists it prompts/for installs.

---

## 3. Architecture & file map

The four **core source files** (each has a macOS copy in the repo root and a Windows copy in `windows/`, kept in lockstep):

| File | Role |
|---|---|
| `prospecting_old.py` | **The engine.** The actual macro: input layer, screen detector, supervisor state machine, all movement/shake/dig/recovery logic. Launched as a subprocess by the app. (Name is legacy; it is the current engine.) |
| `prospecting_app.py` | **The desktop app** (pywebview). Builds the HTML UI, the JS↔Python `Api` bridge, live trace, calibration overlay, relic editor, saved builds, updates, access gate, the **Coach** panel + second window, and all telemetry capture. |
| `prospecting_ui.py` | **Settings schema.** `SECTIONS` (every tunable setting: key, label, type, default), `HELP` (per-setting tooltip text), `SECTION_HINT`, `TAB_ICON`, `PIXEL_FIELDS` (calibratable pixels). Also has a browser-fallback UI. Platform-specific launcher code at the bottom differs mac vs win. |
| `prospecting_assistant.py` | **The Coach brain** (offline expert system) + the API-mode helpers (`system_prompt`, `parse_json`, `finalize_api`). Pure stdlib, no deps. |

**Config / data files (JSON, in the app directory):**
- `prospecting_config.json` — the active settings (all schema keys) + calibrated pixels + relic list + webhook + Coach settings (`COACH_MODE`, `COACH_MODEL`, `COACH_BASE_URL`, `COACH_API_KEY`, `COACH_STATS`).
- `prospecting_builds.json` — named saved builds (full setting profiles + relics).
- `run_history.json` — per-run stats **and now detailed telemetry** (see Analytics).
- `coach_history.json` — persisted Coach chat transcript.

**Distribution / release files:**
- `docs/version.json` — drives in-app auto-update (`version`, `critical`, `installer`, `url`, `notes`).
- `docs/` (GitHub Pages site), `docs/codes.json` (access-code hashes).
- `windows/installer.iss` — Inno Setup script (`#define MyAppVersion`).
- `windows/prospecting.spec` — PyInstaller spec. **Must list each bundled module** in `datas` + `hiddenimports` (it bundles `prospecting_old.py`, `prospecting_ui.py`, `prospecting_assistant.py`, config, icon). If you add a new imported module, add it here or the frozen build won't find it.
- `Prospectors Plus Windows.zip` — the packaged Windows build (rebuilt after every change).

**Other/legacy python files in the repo** (`prospecting_macro.py`, `prospecting_core.py`, `prospecting_new.py`, `prospecting_friend.py`, `prospecting_other.py`, `prospecting_selftest.py`) are older experiments / helpers. The live product is the four core files above.

**Guides (markdown):** `CALIBRATION_GUIDE.md`, `RELEASE_GUIDE.md`, `ANALYTICS_SETUP.md`, `BOT_SCREENSHOT.md`, `ACCESS_CODES_GUIDE.md`, plus `RELEASE_*.md` notes.

---

## 4. The core loop — Supervisor state machine (engine)

The engine runs a **tick-based supervisor**. Each tick it re-senses the world and decides one action. The design principle: **capacity is the ground truth; cues glitch.**

**Two things are sensed each tick:**
- **Where** (from the cue): `Pan` = in water, `Collect Deposit` = on land, `Shake` = shaking.
- **Contents** (from the capacity bar): `full` / `empty` / `partial`.

**Decision (`act`)** — capacity-primary:
- **FULL and in water (Pan cue)** → `do_shake` (empty the pan).
- **FULL and not in water** → `go_water` (walk back into the water).
- **NOT full** → `return_and_dig` (dig-probe: find land by test-digging, then fill to full).

**Watchdog / escalation ladder** (per tick, in `Supervisor.tick`):
1. Normal `act`.
2. If the same (where, contents) signature repeats `STUCK_TICKS` times → `recover` (jittered corrective taps). Up to `RECOVER_LIMIT` times.
3. Still stuck → `break_out` (finish a stuck shake by clicking, then reposition forward). Up to `BREAKOUT_LIMIT`.
4. **Fast paths** checked *before* the normal ladder:
   - **Shake-glitch:** if `SHAKE_GLITCH_LIMIT > 0` and `shake_fails >= SHAKE_GLITCH_LIMIT` → immediate click-to-empty break-out.
   - **No-progress:** if `NO_PROGRESS_SEC > 0` and nothing has completed (no pan emptied, no dig registered) for that long → immediate break-out.
5. If break-outs keep failing → `safe_stop(reason)`.

**`safe_stop(reason)`** — by default **pauses and retries** (so an AFK run heals itself) up to `SAFE_STOP_MAX_RETRIES`, then **hard-stops**. If **Fortune River recovery** is enabled and this is a soft stop, it fast-travels back instead of just pausing.

**Key detail:** every escalation/stop now **emits a structured telemetry event** with a human reason (see Analytics).

### 4.1 The momentum shake (`do_shake`) — THE CROWN JEWEL, DO NOT REWRITE
This is the single most fragile, most-broken-and-restored piece of code. The working technique:

- Optionally hold **W** (`SHAKE_MOMENTUM_W`, default ON) so momentum glides you toward land as the pan drains.
- **Rattle rapid mouse clicks** (`mouse_tap(SHAKE_CLICK_MS)` with `SHAKE_CLICK_GAP_MS` between) — because a *held* press does nothing on macOS.
- Keep clicking until the **capacity bar reads empty** (capacity is truth; the Shake cue sticks). A slower shake just needs more clicks.
- `SHAKE_CLICKS > 0` forces an **exact click count** (use when an extra auto-click bleeds into the next dig and ruins perfect-digs). `0` = auto until empty.
- **Bails** only if the pan stays **completely full** past `SHAKE_BAIL_MS` (means the shake never started → count a `shake_fail`).
- On empty: increments cycle count, sets `last_progress`, sleeps `POST_SHAKE_SETTLE_MS` to settle onto land.

**Every attempt to "improve" this by changing it to a hold, an exact 2-click count, shake-in-place, or break-before-click has broken shaking.** The current version is the restored original momentum shake (from git commit `6f237b0`). **Do not touch `do_shake`.** If you must change shake *behavior*, do it via the surrounding knobs, never by rewriting the click loop.

### 4.2 `go_water` (walk back into water) + X pattern
- Holds **S** until the **Pan cue** (in water), then optionally holds a bit longer (`WATER_EXTRA_BACK_MS`) to go deeper (so the shake doesn't start right at the edge). Uses a growing budget (`PAN_BACK_MAX_MS × (1+water_fails)`) so consecutive misses walk farther.
- **X pattern** (opt-in): alternate 45° diagonal walk-backs (S + A, then S + D…) so each pass covers new ground. Recently reworked (see X-pattern section).

### 4.3 `return_and_dig` (dig-probe) — how it finds land
- After a shake you don't fully trust the land cue, so it **test-digs**: dig once, watch the capacity bar; if the fill **rises**, you're on land (a "dig-probe HIT"). If not, **nudge W forward** and try again, up to `LAND_DIG_TRIES` rounds. If it never finds land → `safe_stop`. Each forward nudge is a logged "nudge" telemetry event.

---

## 5. Detection & calibration

The macro reads specific **screen pixels** (absolute screen coordinates by default). These must be calibrated per user because screen resolution and Roblox UI scale differ.

**Calibratable pixels (`PIXEL_FIELDS`):**
- `CAP_FULL_PIXEL` — right tip of the capacity bar (gray empty → yellow full).
- `CAP_LEFT_PIXEL` — left tip of the bar (used with the right to measure bar width).
- `DEPOSIT_PIX` — a pixel on the white "Collect Deposit" text.
- `PAN_PIX` — a pixel on the white "Pan" text.
- `SHAKE_PIX` — a pixel on the white "Shake" text.
- `DIG_TRIGGER_PIXEL` — the green target on the dig skill bar (only needed for Perfect dig).

**Capacity detection specifics:**
- `is_yellow` test ≈ `r,g ≥ 140 and b ≤ min(r,g) − 45`.
- Pale **anti-aliased edge pixels** at the bar tips can *fail* `is_yellow` even when full → the detector **insets the detected ends inward** to solid gold. (This was a real bug: digs "not registering" after calibration because the sampled pixel sat on a pale edge.)
- The bar can be read in **8 segments** to know how full it is (used for anti-overflow: stop shaking as the bar clears).

**Three ways to calibrate (in the Calibrate tab):**
1. **Guided calibration (recommended):** a wizard that auto-detects the capacity bar (with bar full) and each prompt (stand where the prompt shows, click Detect). Confines detection to the lower-center band + saturated gold so it doesn't pick sand/pan-dish. If auto-detect fails, "Pick manually".
2. **Manual overlay:** a **transparent full-screen overlay** freezes a screenshot; the user hovers a magnifier loupe and clicks the exact pixel, sees colour + coords, Confirms.
3. **Test detection (live):** shows in real time whether capacity reads FULL and each cue is visible (green = good).

**Fortune River calibration (separate):** the pink FR row colour (`FR_TEXT_RGB`), the list box top/bottom edges (`FR_BOX_TOP`/`FR_BOX_BOTTOM`), the scan X column (`FR_SCAN_X`), the **screen-centre "home" pixel** (`FR_HOME_PIXEL`, where the cursor rests with shift-lock off), and optionally the open button (`FR_OPEN_PIXEL`).

**Calibration invariants (learned the hard way):**
- On calibrate, the app sets **`AUTO_CALIBRATE=false` and `WINDOW_RELATIVE=false`**. Otherwise the app re-derived pixels from baked dev-screen ratios and/or shifted them by window position, breaking the user's fresh calibration (digs stopped registering).
- Pixels are **absolute screen coordinates** by default. `WINDOW_RELATIVE` (off by default) shifts them when the Roblox window moves — only for advanced users who re-calibrate as a reference.

---

## 6. The calculator formulas (used by the Coach)

Ported from the game's community calculator. Used only for **expected/estimated** timings (never blindly forced onto live knobs):

- **Rolls per second from shake speed:**
  `r = 4.03266e-9·ss³ − 1.68935e-5·ss² + 0.0255557·ss + 0.206594`  (ss = Shake Speed stat)
- **Digs to fill the pan:** `n = ceil(Capacity / (1.5 × DigStrength))`
- **Dig hold length:** `hold_ms = 55000 / DigSpeed%`
- **Per-dig time:** `≈ 190 / DigSpeed%` seconds
- **Full cycle time:**
  `cycle_sec = Capacity / (r × ShakeStrength) + 1.5 + 190·n / DigSpeed% + 0.62`
- **Pans per hour:** `3600 / cycle_sec`

**The `0.62 s` constant** is a **measured, calibrated overhead** (walk/turn/settle), found to be **constant, NOT walk-speed dependent**, from two data points (walk-speed 24 → ~25 pans/min; walk-speed 34 → ~19 pans/min gave a consistent fixed overhead). Keep it as a constant.

---

## 7. Full settings reference

All tunable settings (grouped as they appear in the UI). `[type, default]`.

### Easy tuning (plain-language offsets; 0 = no change; the backend maps each onto the real knobs)
- `EASY_WATER_BACK_MS` [int, 0] — go further back into the water.
- `EASY_LAND_FWD_MS` [int, 0] — go further onto land.
- `EASY_SHAKE_DELAY_MS` [int, 0] — wait longer before the shake starts.
- `EASY_FIRST_DIG_DELAY_MS` [int, 0] — wait longer before the first dig.
- `EASY_WATER_RETURN_DELAY_MS` [int, 0] — pause before heading back to water when full.

### Treasure chest (no-shake collection mode)
- `TREASURE_MODE` [bool, False] — dig, then strafe D until Collect cue, dig, strafe A, repeat.
- `TREASURE_DIGS` [int, 1] — digs per chest before strafing.
- `TREASURE_DIG_MS` [int, 8] — quick dig click length.
- `TREASURE_DIG_GAP_MS` [int, 12000] — delay between dig clicks (chest dig animation is slow, ~12 s).
- `TREASURE_MOVE_MAX_MS` [int, 2500] — safety cap on strafing.

### Mode / Dig
- `PERFECT` [bool, False] — release on green (usually leave OFF; bar too fast — use timed hold).
- `DIG_CLICK_MS` [int, 75] — dig hold length (auto-fills from Dig speed, or set manually).
- `DIG_SPEED` [int, 100] — your dig-speed stat %; auto-fills hold (`55000/speed`).
- `MAX_DIGS_TO_FILL` [int, 8] — safety cap on digs to fill (watches the bar; set above your build's need).
- `DIG_FILL_MS` [int, 250] — wait for FULL after each dig.
- `PRE_DIG_SETTLE_MS` [int, 60] — settle after landing before the first dig (so it doesn't fire mid-glide).

### Walk back into water
- `PAN_BACK_MAX_MS` [int, 200] — safety cap on the S walk-back.
- `WATER_EXTRA_BACK_MS` [int, 0] — keep holding S after the Pan cue (go deeper).

### Shake
- `SHAKE_MOMENTUM_W` [bool, True] — hold W during shake for momentum onto land.
- `SHAKE_CLICKS` [int, 0] — exact click count (0 = auto until empty). Use fixed count if an extra click bleeds into the dig.
- `SHAKE_CLICK_MS` [int, 18] — each shake click length.
- `SHAKE_CLICK_GAP_MS` [int, 14] — gap between shake clicks (lower = faster rattle).
- `SHAKE_HOLD_MS` [int, 1500] — overall shake time cap (stops early when empty).
- `SHAKE_BAIL_MS` [int, 500] — if still completely full past this, the shake didn't start → retry.
- `SHAKE_START_DELAY_MS` [int, 0] — pause between reaching water and starting the shake.
- `POST_SHAKE_SETTLE_MS` [int, 150] — settle after the pan empties (momentum onto land) before the dig-probe.

### Return to land (dig-probe)
- `DEPOSIT_MAX_MS` [int, 1200] — cap on the forward W walk looking for land.
- `LAND_SETTLE_MS` [int, 45] — hold W a touch after the land cue (prevents land↔water flicker).
- `DIG_PROBE_MS` [int, 320] — time to detect a probe-dig hit before calling it a miss.
- `PROBE_GAP_MS` [int, 80] — settle after a forward nudge before the next probe.
- `LAND_PROBE_NUDGE_MS` [int, 90] — forward W nudge between probe digs.
- `LAND_DIG_TRIES` [int, 5] — nudge rounds before giving up (then SAFE STOP).

### Recovery / safety
- `RECOVER_ENABLED` [bool, True] — master switch for stuck-recovery (jitter taps).
- `SHAKE_RETRY_ENABLED` [bool, True] — re-attempt a shake when stuck full in water.
- `BREAKOUT_ENABLED` [bool, True] — last-resort break-out to escape stuck loops.
- `STUCK_TICKS` [int, 3] — identical reads before recovery triggers.
- `RECOVER_LIMIT` [int, 3] — recoveries before break-out.
- `RECOVER_BACK_MS` [int, 160] — recovery nudge budget (pulsed taps).
- `SHAKE_FAIL_LIMIT` [int, 5] — failed shakes before SAFE STOP.
- `SHAKE_GLITCH_LIMIT` [int, 2] — failed shakes before a quick click-to-empty. **0 = disabled** (see 0-threshold bug).
- `NO_PROGRESS_SEC` [int, 5] — seconds of nothing-completing before a click-to-empty. **0 = disabled.**
- `BREAKOUT_LIMIT` [int, 2] — break-out attempts before SAFE STOP.
- `BREAKOUT_SHAKE_MS` [int, 700] — click-to-finish a movement-locking shake.
- `BREAKOUT_REPOS_MS` [int, 160] — forward reposition nudge during a break-out.
- `SAFE_STOP_RETRY` [bool, True] — safe-stop pauses & retries (AFK self-heal) instead of hard-stopping.
- `SAFE_STOP_RETRY_SEC` [int, 60] — wait before each retry.
- `SAFE_STOP_MAX_RETRIES` [int, 3] — hard-stop after this many failed retries.

### Recovery movement (jitter taps)
- `BURST_ON_MS` [int, 11] — tap hold per pulse.
- `BURST_OFF_MS` [int, 1] — tap release per pulse.

### Notifications (Discord)
- `WEBHOOK_ENABLED` [bool, False] — DM via the Prospectors bot.
- `WEBHOOK_USER` [str, ""] — your exact Discord username (the Coach must NEVER overwrite this).
- `WEBHOOK_STATS_MIN` [int, 60] — stats DM interval (0 = off).
- `NOTIFY_START/STOP/STATS/SAFE_STOP/RECOVERIES/ERRORS` [bools] — which events to send.
- `NOTIFY_SCREENSHOT` [bool, True] — attach a screenshot to safe-stop/recovery/hard-stop/stats DMs.

### Auto-stop
- `AUTOSTOP_ENABLED` [bool, False], `AUTOSTOP_MINUTES` [int, 60] — stop after N minutes.
- `STOP_AFTER_PANS` [int, 0] — stop after N pans (0 = off; a bag-full guard).

### Window
- `WINDOW_RELATIVE` [bool, False] — shift pixels when the Roblox window moves (off = absolute coords).

### Smart / experimental
- `SMART_TIMING` [bool, False] — trial-and-error auto-tuning (experimental).
- `ADAPT_MISS_PCT` [int, 20] — miss-rate threshold before smart timing acts.
- `X_PATTERN` [bool, False] — diagonal walk-backs (cover new ground).
- `X_STRAFE_MS` [int, 220] — how long the diagonal is held each pass before finishing straight (lower = straighter/less drift; 0 = old full diagonal).
- `X_RECENTER_MS` [int, 400] — sideways drift budget before an auto-recenter strafe (0 = never).
- `FR_RECOVERY` [bool, False] — Fortune River fast-travel recovery on soft stop.
- `FR_TEXT_TOL` [int, 55] — colour match tolerance for the pink FR row.
- `FR_SCAN_HOVER_MS` [int, 12] — dwell per step while sweeping the list.
- `FR_MOVE_STEP` [int, 8] — max px per cursor-move step (smaller = smoother, less mouse-accel error).
- `FR_FIND_TRIES` [int, 8] — full top-to-bottom sweeps before re-opening the device.
- `FR_OPEN_TRIES` [int, 3] — re-equip slot 4 + re-open attempts.
- `FR_SCROLL_STEPS` [int, 3] — wheel notches per scroll.
- `FR_DOUBLE_GAP_MS` [int, 120] — gap between the two clicks of the open double-click.
- `FR_OPEN_MS` [int, 600] — wait for the Fast-Travel menu to appear.
- `FR_CLICK_SETTLE_MS` [int, 300] — pause around each click.
- `FR_ACTION_GAP_MS` [int, 500] — pause between each recovery step.
- `FR_WARP_MS` [int, 2500] — wait after teleport for the world to load.
- `FR_STRAFE_MS` [int, 10] — tiny D strafe to line up after returning.
- `FR_WALK_MAX_MS` [int, 6000] — max W walk to reach water.
- `FR_END_A_MS` [int, 300] — hold A on land before restarting the loop.
- `FR_CROSS_CONFIRM` [int, 3] — cue reads to confirm a real water crossing/arrival (stops restarting on the spawn shore).

---

## 8. Major features (what / why / how)

### 8.1 Treasure mode
No-shake chest collection: dig `TREASURE_DIGS` times, hold D until the Collect cue, dig, hold A until the cue, alternating. `TREASURE_DIG_GAP_MS` (~12 s) prevents dig clicks from stacking on the slow chest animation.

### 8.2 Fortune River recovery (`FR_RECOVERY`)
On a soft stop, instead of just pausing, the macro fast-travels back to Fortune River and resumes. Sequence:
1. Switch to **hotkey 4** and **double-click** to open the Fast-Travel menu.
2. Press **Shift** to **exit shift-lock** (so the mouse can move freely); it re-enters shift-lock before digging again.
3. Sweep the cursor down a **calibrated list box** using **relative-from-home** movement (calibrate a "home"/screen-centre pixel so the cursor never flies off-screen), looking for the **pink "Fortune River" text**; **scroll** if not found; go to the X, then top, then bottom, then scroll.
4. Click Fortune River, wait `FR_WARP_MS` for load, switch to the pan, walk into the water.
5. Require a **sustained Pan (water) crossing** (`FR_CROSS_CONFIRM` reads) before accepting the next-shore "Collect Deposit"; hold **A** for `FR_END_A_MS` on land, then restart the loop.
- **Settle delays everywhere** (`FR_CLICK_SETTLE_MS`, `FR_ACTION_GAP_MS`) because doing it too fast means nothing loads.
- **Calibrations auto-save** on each FR capture.

### 8.3 Recovery / safety system
The escalation ladder (recover → break-out → safe-stop → retry/hard-stop) plus the two **fast paths** (shake-glitch, no-progress) that catch the shake glitch in ~5 s instead of ~30 s. See §4.

### 8.4 X pattern (reworked)
Diagonal walk-backs to cover new ground. **Problem:** the old version held the diagonal for the *whole* back-walk, so it (a) drifted sideways over time, (b) reached the wrong depth, and (c) the straight forward-leg during the shake landed in the water. **Fix (v3.0.0):**
- **Bounded diagonal** (`X_STRAFE_MS`, default 220): hold the diagonal only briefly, then finish the walk-back **straight** → consistent depth + landing.
- **Auto-recenter** (`X_RECENTER_MS`, default 400): track sideways drift (`x_balance`); when it exceeds the budget, do a **pure A/D strafe** back toward centre (no S, so depth is unchanged) before the next pass → never wanders off the strip.
- Emits a `recenter` telemetry event. **`do_shake` untouched.**

### 8.5 Relics
A relic timer editor: place items (name, slot, minutes, clicks); the macro presses the slot every N minutes. Builds like "Shards Spirit Orb" use slot-6 every ~14 min.

### 8.6 Notifications + screenshots
Discord DMs via the Prospectors bot on start/stop/safe-stop/stats, optionally with a **screenshot** attached (base64 PNG posted to the bot, which forwards it) so the user can see what went wrong remotely.

### 8.7 Pop-out pill (live dashboard)
A small always-on-top floating window showing live status/runtime/pans/pans-hr/recoveries and the **current phase** (Walking to water, Shaking, Digging, Walking to land, Recovering…). Phases are emitted by the engine as `__PHASE__ <name>` lines.

### 8.8 Custom keybinds
User-rebindable: Start/Stop (default Ctrl+K), Soft-stop (Ctrl+J), Quit (Esc), Pop-out toggle (Ctrl+P).

### 8.9 Saved builds
Named full-setting profiles in `prospecting_builds.json` (includes relics). Loading a build **merges** it into the active config (keeping calibrated pixels / webhook / window settings the build doesn't carry). Current builds: Money (×4), Shards (many variants), Shards X, Auto Shards, Shards Spirit Orb, Shards Vineheart (×2).

### 8.10 Auto-build (conservative)
Enter your in-game stats → it shows **expected** timings from the formulas (cycle, pans/hr, digs-to-fill, ideal dig-hold) and, on Apply, sets **only the safe stat-driven dig knobs** (`MAX_DIGS_TO_FILL`, `DIG_SPEED`, `DIG_CLICK_MS`, `PERFECT`) — it deliberately **does not touch shake/movement/recovery**, because earlier aggressive auto-build versions repeatedly broke shaking. (Hyperspeed and full auto-tuning were tried and reverted.)

### 8.11 Private analytics
Each run launch posts a card to a **private Discord webhook the owner controls** (NOT the shared bot, so other server admins can't see it): Discord username (if entered), app version, IP + coarse geo (via `ip-api.com`), ISP, and an access-code hash. Purpose: enforce invite-only, spot abuse. Implementation notes: needs a **custom User-Agent** header (Discord/Cloudflare 403s `Python-urllib`), and a **macOS SSL unverified-context fallback** (mac Python often can't verify certs). Config key: `SYNC_URL`. The key/webhook lives in the bundled config; it is never written into builds. (History: tried a self-hosted endpoint and a bot-side server; both rejected — the user didn't want to host anything — so it's a webhook.)

### 8.12 The Coach (tuning assistant) — see §9 (its own section, it's large).

### 8.13 Advanced run telemetry / analytics (v3.0.0)
The engine emits structured **`__EVENT__ <json>`** lines for every notable event with a **reason**: `safe_stop`, `hard_stop`, `recover`, `break_out`, `nudge`, `shake_fail`, `shake_glitch`, `no_progress`, `fr_recover`, `recenter`. The app aggregates them per run into `run_history.json` as `event_counts`, `reason_counts`, and a capped `events` timeline. The **History tab** shows a per-run reason breakdown + an expandable detailed timeline. The **Coach** can answer "why did it safe-stop / why so many nudges" (offline) and gets a telemetry block in its API system prompt. **All additive — no macro control-flow changes.**

---

## 9. The Coach (in depth)

A tuning assistant docked on the **right** of the app (auto-opens on launch), with a real **second pop-out window**. It has **two engines**, and the offline one is the default.

### 9.1 Offline brain (default, `prospecting_assistant.py`)
A **local expert system** — no internet, no key, works on any user's machine. `respond(message, ctx)` returns `{reply, changes, topic, askStats, chips}`.
- **Symptom catalog:** many entries, each with weighted keyword lists + anti-keywords, and a `fix()` function that **nudges specific knobs relative to their current value, clamped to safe ranges** (`RANGES` table). Covers shaking (late/slow/miss/overflow/over-shake), water/land (lands in water, shakes on land, overshoot/short water/land), digging (early/late/hold-long/short/not-filling/overflow), dig-probe, recovery (too aggressive/too slow), Fortune River (triggers too easily/can't find/wrong row/won't open/too fast/wrong shore/jumpy), X-pattern drift, and capacity/calibration.
- **Escalation:** "still happening" re-applies the last topic harder (2× step).
- **Efficiency analyzer:** trims dead time; with stats, computes ideal cycle and matches dig knobs.
- **"Why did X happen":** reads the run telemetry (`reason_counts`) and explains causes.
- **Explain a setting**, **stats intake form**, **explicit "set X to N"**.
- **Safety:** it only ever proposes real schema keys; `assistant_apply` re-validates every key against the schema + ranges before writing. It never touches code, and never overwrites `WEBHOOK_USER`.
- The user **confirms** every change via a diff card ("Implement N changes" / "Don't apply"). Nothing applies silently.

### 9.2 API mode (optional, bring-your-own-key)
Switch in the ⚙ menu. The app calls the user's chosen LLM with `system_prompt(ctx)` (full settings schema + current values + formulas + builds + run telemetry) and instructions to return **one JSON object** `{reply, changes:[{key,to,reason}], chips}`. `parse_json` extracts the **first balanced JSON object** (robust against extra text/multiple objects — this fixed a raw-JSON-leak bug), then `finalize_api` validates every change against the schema. So the diff-card / apply / safety path is **identical to offline**.
- **Providers:** Anthropic native (model starts with `claude`) OR any **OpenAI-compatible** endpoint (OpenAI, Google Gemini, DeepSeek, local Ollama) via an optional **base URL**. The ⚙ has an **Engine dropdown → Model dropdown** with known-good IDs per provider (so users don't fat-finger a model id) + a "Custom / local" option for a base URL.
- **Default model:** `claude-haiku-4-5-20251001` (cheapest capable; ~0.5–0.7¢/message for this task). OpenAI cheap pick: `gpt-5.4-mini` ($0.75/$4.50 per Mtok) with `gpt-4o-mini` as a universal fallback.
- OpenAI path uses `max_completion_tokens: 2048` (reasoning models can eat a small budget before emitting JSON) and `response_format: json_object`.
- **Conversation memory + condensing:** the last ~14 turns are sent as context; current settings are always in the system prompt so state is never lost even when older turns drop.
- The API key is stored only in local config (`COACH_API_KEY`), never returned to the UI (only a "key saved" flag), never written into builds or analytics.

### 9.3 UI
- Docked right panel + a **real second OS window** (`COACH_HTML`, pre-created hidden at startup and shown/hidden — runtime `create_window` is flaky). Both share the same `Api` and the same **persisted chat history** (`coach_history.json`), so the conversation is continuous across panel/window.
- Header icons: ⊕ New chat, ⚙ Settings, ⤢ Open in window, ✕ Close. Diff cards with Implement/Don't-apply and applied/skipped states. Typing indicator. Formatted (markdown-ish) replies. Applying changes in the window live-refreshes the main window's fields.

**Important constraint (established with the user):** you **cannot** use a Claude.ai/Cowork subscription to power a distributed app — Anthropic's terms forbid third-party apps routing through Free/Pro/Max via OAuth; the sanctioned "bring your own Claude" path is an **API key**. "Connections/Connectors" are the opposite direction (they let Claude reach into other tools). So there is no way to wire "Claude-in-this-chat" into the macro; friends use offline (free) or their own API key.

---

## 10. War stories — every hard problem and how we solved it

These are the expensive lessons. A new model should read these to avoid re-breaking things.

1. **The shake keeps breaking.** Multiple "optimizations" each broke shaking: forcing an exact 2-click count (under-shook); a **held press** (does nothing on macOS); shake-in-place (left the player deep in water); break-before-click on land (broke when the land cue flickered). **Fix:** restored the **original momentum shake verbatim from git** (`6f237b0`). **Rule: don't rewrite `do_shake`.**

2. **Digs not registering after calibration (major).** Two causes: (a) `apply_auto_calibrate()` re-derived pixels from **baked dev-screen ratios** and `WINDOW_RELATIVE` **shifted** them → fixed by setting **`AUTO_CALIBRATE=false` and `WINDOW_RELATIVE=false` on calibrate**; (b) the capacity pixel sat on a **pale anti-aliased edge** failing `is_yellow` → **inset the detected ends inward to solid gold**.

3. **The critical 0-threshold bug ("broke all builds" / "auto does the Fortune River recovery").** When `SHAKE_GLITCH_LIMIT` or `NO_PROGRESS_SEC` were **0**, the checks (`shake_fails >= 0`, `time_since_progress > 0`) were **true on the very first tick**, so the macro instantly broke-out twice and jumped to Fortune-River recovery before a single shake. Trace showed `shake not registering x0 → break-out #1 → #2 → FR-recover`. **Fix:** treat **0 as disabled** in both engines (`if LIMIT > 0 and …`) and set sane config values. **Rule: any "N failures/seconds before X" threshold must treat 0 as OFF.** (Ironically a "full git revert" to 2.3.0 re-introduced the bad 0 values — the bad config was baked into that release.)

4. **Fortune River recovery pain (many rounds):** "doesn't open slot 4" (needed press-4-then-double-click); cursor **flew off-screen** (switched to **relative-from-home** movement with a calibrated home pixel); **calibrations weren't saving** (auto-save each capture); "moves too fast, nothing loads" (added `FR_CLICK_SETTLE_MS`/`FR_ACTION_GAP_MS`); "lands in water / restarts on wrong shore" (require a **sustained Pan crossing** before accepting the next Collect cue).

5. **Keybinds tab was empty.** The pinned tab id `keys` mapped to panel `pkeys`, but the JS id logic produced `p_keys`. **Fix:** add the id to the pinned list. (Same class of bug later fixed for the `auto` tab.)

6. **Calibrate overlay black screen / pop-out not working.** Runtime `webview.create_window` was flaky. **Fix:** **pre-create the pill + overlay (+ later the Coach window) hidden at startup** and show/hide them. Overlay re-fetches the screenshot on show and waits for `pywebviewready`.

7. **Auto-detect calibration picked sand / the pan dish.** **Fix:** confine capacity detection to the lower-centre band + saturated gold; confine cue detection to the prompt's bottom-most centred white line / leftmost blob (the mouse icon).

8. **Auto-build broke everything (twice).** Aggressive auto-build/hyperspeed changed shake/movement/segments and destabilized runs. **Fix:** reverted to the known-good engine and rebuilt a **conservative** auto-build that only sets safe dig knobs and analyzes *expected* timings (not set in stone).

9. **Analytics didn't send.** Three fixes: needed an app restart; macOS Python **HTTPS cert verification failure** → **unverified SSL fallback**; Discord **403** on the default agent → **custom User-Agent** header. Also: rejected hosting a server/website (privacy + no-hosting) → settled on a **private Discord webhook** with client-side IP/geo.

10. **Coach raw-JSON leak.** In API mode the model's reply (or multiple concatenated JSON objects) leaked into the chat as raw JSON. **Cause:** the old parser used first-`{` to last-`}`. **Fix:** a brace-matching parser that extracts the **first complete balanced object**.

11. **Coach UI clunky / "invalid model id".** Redesigned the settings to **Engine→Model dropdowns** (no typing model ids) with per-provider known-good lists, and cleaned the panel (icon header, no cutoff, typing indicator, formatted replies, clearer buttons, expand, persistent history).

12. **X pattern drift / ends in water.** Bounded diagonal + auto-recenter (see §8.4).

---

## 11. HARD RULES / INVARIANTS — do not violate

1. **Never rewrite `do_shake`.** The momentum click-stream shake is sacred. Tune via knobs only.
2. **Capacity bar is ground truth.** Cues glitch (sticky Shake, flickering Deposit). Only trust the **Pan** cue for "in water".
3. **macOS: held presses don't register in Roblox** — shake must be rapid clicks.
4. **Any "N-before-action" threshold must treat 0 as disabled** (guard with `> 0`).
5. **Calibration must not be overridden** by `AUTO_CALIBRATE` or `WINDOW_RELATIVE` — set both false on calibrate. Pixels are absolute screen coords.
6. **Keep mac + Windows source in lockstep.** Every engine/app/ui/assistant edit goes to both copies. The assistant file is identical on both; the ui/app differ only in platform input/launcher code.
7. **New engine features are opt-in / default-off** and **additive** — prefer emitting data / adding a guarded branch over changing existing control flow. Movement changes are high-risk.
8. **The Coach only ever writes validated schema keys** (`assistant_apply` re-checks against `TYPES` + `RANGES`); it never touches code and never overwrites `WEBHOOK_USER` or the API key.
9. **Pre-create extra windows hidden at startup**; don't create windows at runtime (flaky).
10. **Cannot push from the sandbox** — hand the user the `git push` / tag / release steps.
11. **Verify before shipping:** `py_compile` all changed Python; render `build_html()`; extract the `<script>` and `node --check` it (both the main window JS and `COACH_HTML` JS); rebuild the Windows zip. The JS lives inside Python strings, so `py_compile` alone does NOT catch JS syntax errors.
12. When adding a new imported module, add it to `windows/prospecting.spec` (`datas` + `hiddenimports`).
13. **Don't state analytics specifics in git commit messages** — just say "analytics".

---

## 12. Version history (high level)
- **2.0.x / 2.1.x / 2.2.x** — early releases (core loop, calibration, builds, FR recovery groundwork, UI).
- **2.3.0** (published, critical) — faster shake-glitch recovery (`NO_PROGRESS_SEC`, `SHAKE_GLITCH_LIMIT`), calibration + dig-detection reliability, Keybinds fix, analytics, stability. **Note: shipped with the bad 0-threshold config.**
- **2.3.1** (staged, later superseded) — the 0-threshold fix (0 = disabled).
- **3.0.0** (staged now, not pushed) — the **Coach** (offline + optional multi-provider API + second window + persistent history), **advanced run telemetry** (event reasons, History breakdown, "why" answers), the **X-pattern rework**, wider default window, and the 0-threshold fix folded in.

**Release process:** bump `VERSION` (both app copies), `windows/installer.iss` `MyAppVersion`, and `docs/version.json` (`version`, `critical`, `notes`); rebuild the Windows zip; user runs `git push`, `git tag vX.Y.Z`, `git push origin vX.Y.Z`, builds `ProspectorsPlusSetup.exe` on Windows from `installer.iss`, and publishes a GitHub Release with the `.exe` attached to the tag. `version.json` goes live on push (Pages), so publish the release with the exe around the same time or the download 404s briefly. `critical:true` prompts existing installs to update.

---

## 13. Quick-start for a new model continuing this project
1. Read §11 (Hard rules) and §10 (War stories) first.
2. The live code is the four files: `prospecting_old.py` (engine), `prospecting_app.py` (app+Coach), `prospecting_ui.py` (schema), `prospecting_assistant.py` (Coach brain) — each with a `windows/` copy.
3. To add a **setting**: add to `SECTIONS` + `HELP` in `prospecting_ui.py` (both copies), define a module-global default with the same name in `prospecting_old.py` (both copies) so `load_config` applies it, and (if the Coach should tune it) add it to `RANGES` in `prospecting_assistant.py`.
4. To change **behavior**: prefer a new guarded branch / knob over editing existing movement or the shake.
5. **Always** mirror to Windows and run the verification in §11.11 before rebuilding the zip.
6. You cannot push — give the user the git/release commands.

*End of knowledge base.*
