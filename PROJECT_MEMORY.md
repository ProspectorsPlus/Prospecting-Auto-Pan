# Prospectors Plus — Project Memory (comprehensive)

> Permanent memory for the Claude Project. This is the single most complete description of the
> Prospectors Plus macro: what it is, the game it automates, its architecture, every important file,
> how every subsystem works, the design decisions and reasoning behind them ("our thoughts"), the
> release pipeline, and the conventions any future contributor must follow. It is intentionally long.
> When in doubt, this file is the map; the code is the territory.

**Current version at time of writing:** 4.2.0 (macOS + Windows). Owner/user: Ibraheem.
**Repo root:** `/Users/ibraheemarif/Roblox Macro/Claude`.

---

## TABLE OF CONTENTS
1. What Prospectors Plus is
2. The game it automates (Prospecting) — mechanics the macro reads and drives
3. High-level architecture (two-process design, platforms)
4. File map — every file and what it's for
5. The engine (`prospecting_old.py`) — the supervisor state machine in depth
6. Detection & calibration (pixels, ratios, window tracking, cue masks)
7. The app / UI (`prospecting_app.py`) — pywebview, the Api, the pages
8. The settings schema (`prospecting_ui.py`) — SECTIONS, the Cycle stages
9. The Coach (`prospecting_assistant.py`)
10. Modes — Standard, Treasure, Shards, Geodes (with history/reasoning)
11. Recovery & safety nets — the escalation ladder
12. Tracking — earnings, finds, and Tracker/auto-pan
13. Notifications, auto-stop, relics
14. The access-code system (machine-locked invites + revocation)
15. Builds — save/load/share/attachments
16. The interactive tutorial / tour
17. The release pipeline (Windows Actions, macOS DMG, update banner, website)
18. mac ↔ Windows lockstep discipline + verification protocol
19. Version history & feature timeline
20. Design philosophy, decisions, and "our thoughts"
21. Known issues, gotchas, and things not to break
22. Conventions for future work
23. Prospector Studio (custom block scripting)

---

## 1. What Prospectors Plus is

Prospectors Plus is an **invite-only auto-pan macro** for the Roblox game **Prospecting**. It
automates the game's core panning loop — dig to fill your pan, walk into the water, shake the pan
empty, walk back onto land, repeat — while continuously **reading the screen** to self-correct. It is
a desktop app (macOS and Windows) with a rich UI: live stats, a visual "Cycle" tuning page, saveable
builds, calibration wizards, an offline/AI tuning Coach, analytics dashboards, a live HUD overlay,
Discord notifications, and an interactive tutorial.

Key properties:
- **Screen-reading, not memory-reading.** It never touches Roblox's memory or network. It captures
  the screen, checks specific pixels/regions (the capacity bar, the HUD prompt words, the dig
  skill-check bar), and sends OS-level keyboard/mouse input like a human would. This is why
  **calibration** (teaching it where those things are on *your* screen) is mandatory and first.
- **Robust by design.** A fused cue+capacity state machine cross-checks two independent signals
  before trusting anything, verifies each action's result on the next tick, and self-heals from
  missed clicks, over/undershoots, stuck cues, and drift through an escalating recovery ladder that
  ends in a graceful "safe stop," not a crash.
- **Invite-only.** Access is gated by machine-locked codes that bind to the first PC that redeems
  them and can be revoked remotely by the owner.

Audience: the owner (a power user) plus a small invited group who are **not** power users — hence the
heavy investment in plain-English labels, the Coach, health badges, and the tutorial.

---

## 2. The game it automates: Prospecting

Prospecting is a Roblox game about panning for valuable materials. The macro interacts with these
game elements (all read off the HUD):

- **The pan & capacity bar ("Pan Fill" bar).** You dig on land to load your pan with dirt; a bar
  fills toward FULL (it turns **yellow** at the right end when full). In the water you **shake** the
  pan, which drains the bar to **empty** and yields finds. The bar is the macro's *ground truth* for
  "did the pan fill / empty."
- **HUD prompt words.** A white prompt appears at the bottom depending on context: **`Pan`** when
  you're standing in water, **`Collect Deposit`** when you're on land (able to deposit/dig), and
  **`Shake`** while shaking. These are the macro's ground truth for "where am I."
- **The dig skill-check ("green dig-bar").** When you dig, a small bar can flash **green** at the
  perfect-release moment, a few frames *before* the capacity bar visibly moves. The macro can use
  this for "Perfect dig" and for confirming a dig registered earlier than the capacity bar would show.
- **Movement.** Standard Roblox WASD; the macro holds **S** to back into water, **W** to glide toward
  land, and uses the game's momentum so the shake carries you from water onto land.
- **Dig speed / equipment.** Different shovels/gear have different dig speeds and animation lengths.
  Very slow gear (e.g. "10% dig speed" **geode** shovels) has long fill *and* shake animations, which
  drove the dedicated **Geodes** mode.
- **Finds & rarity.** Shaking yields item pop-ups of varying rarity (Common → … → Exotic → Cosmic and
  friends), shown as a stack of cards. The finds tracker OCRs these.
- **Currencies.** Money and shards, shown as HUD totals; the earnings tracker OCRs them.
- **Relics / timed items.** Buffs (solar mags, idols, etc.) with cooldowns you activate from the
  hotbar; the macro can auto-fire them on a schedule.
- **Built-in Auto Pan.** The game itself has an auto-pan toggle; the macro's **Tracker** mode
  benchmarks *that* (watch-only) and can run relics while the game auto-pans.
- **Maps / fast travel.** Some areas (Fortune River, Starfall River) have warp devices; the optional
  FR/SR recovery can fast-travel back to a panning spot after a soft stop.

Understanding this game layer is essential: every macro setting maps to one of these mechanics.

---

## 3. High-level architecture

**Two processes:**

1. **The app / UI** — `prospecting_app.py`, a `pywebview` desktop window rendering an HTML/CSS/JS UI.
   It owns settings, calibration, builds, analytics, the Coach, the HUD, the tutorial, updates, and
   the access gate. It **launches the engine as a subprocess** and talks to it (start/stop, config,
   live stats stream via stdout lines like `__STATS__`, `__GEODE__`, etc.).
2. **The engine** — `prospecting_old.py`, the headless automation core: the supervisor state machine,
   screen capture, detection, input, modes, recovery, and the finds/earnings OCR threads. It runs the
   actual panning loop and prints stats/events the app reads.

**Why two processes?** So the heavy, timing-sensitive automation loop and OCR threads don't stall the
UI, and so Ctrl+C / stop only kills the macro loop, not the whole app. (There was real work to make
"close the app fully quits everything" behave — see §21.)

**Platforms.** macOS and Windows share ~95% of the code. The differences are concentrated in the
**input/capture layer** and a few platform bits:
- **macOS:** screen capture and input via Quartz / CoreGraphics (pyobjc `Quartz` `CGEvent*`,
  `CGDisplayBounds`, `CGWindowListCreateImage`), OCR via Apple **Vision** (finds/earnings tracking is
  macOS-only). Retina scaling handled (detection in physical px, events in points).
- **Windows:** input via `ctypes` `SendInput`, metrics via `GetSystemMetrics`; there's a "Frozen
  macro mode" and a few naming differences ("click" vs "mark"). Finds/earnings OCR dashboards are OFF
  on Windows this era (they use Apple Vision).

The Windows copies live under `windows/` and must stay **byte-identical** to the macOS copies except
for those platform layers. This "lockstep" is a hard rule (§18).

---

## 4. File map

**Active core (the 4 that matter, mirrored in `windows/`):**
- `prospecting_app.py` (~4,970 lines) — the pywebview app + entire UI (`build_html()` etc.), the
  `Api` class exposed to JS, calibration overlay, cue-mask editor, analytics/HUD/coach windows,
  tutorial, access gate, updater. **All UI HTML/CSS/JS is inside this file as raw triple-quoted
  strings.**
- `prospecting_old.py` (~5,138 lines) — **the active engine** (despite the "old" name). The supervisor
  loop, detection, input, modes, recovery, tracking, OCR.
- `prospecting_ui.py` (~1,039 lines) — the **settings schema**: `SECTIONS` (the list of
  `(title, [(key,label,type,default), …])`), `SECTION_HINT`, `TAB_ICON`, `PIXEL_FIELDS`,
  `PIXEL_DEFAULTS`. Single source of truth for the 144 settings and calibration targets.
- `prospecting_assistant.py` (~1,145 lines) — the **Coach**: offline heuristics + optional AI API
  mode, and `RANGES` (known-safe min/max/step per setting, which also drive the Cycle sliders).

**Windows packaging (`windows/`):** `prospecting.spec` (PyInstaller), `installer.iss` (Inno Setup),
`build.bat`, `Install.bat`, `Prospectors Plus.bat`, `README.txt`, `icon.ico`, plus the mirrored
`prospecting_app.py` / `prospecting_old.py` / `prospecting_ui.py` / `prospecting_assistant.py` and
seed `prospecting_config.json` / `prospecting_prices.json`. The `Prospectors Plus Windows.zip` is a
14-file bundle rebuilt whenever anything under `windows/` changes.

**Data / state (JSON):**
- `prospecting_config.json` — saved settings (the 144 keys) + calibration.
- `prospecting_builds.json` — saved builds (named setups; can include base64 attachments).
- `run_history.json` — past runs + stats. `coach_history.json` — Coach chat history.
- `prospecting_prices.json` — item/loot prices for value math. `prospecting_secrets.json` — local
  secrets. `prospecting_calib_log.csv` — calibration debugging log.

**Website / release (`docs/`, GitHub Pages):**
- `index.html` — the marketing + download site (OS-aware header, macOS + Windows download buttons,
  Gatekeeper "how to open" modal). `version.json` — drives the in-app update banner. `codes.json` —
  the machine-lock/revocation list. `favicon.svg`.

**CI:** `.github/workflows/build-windows.yml` — builds the Windows `.exe` on a `v*` tag push and
injects the `PP_SYNC_URL` Discord webhook secret (unconditionally — see §21).
**macOS build:** `build_dmg.command` — run on the owner's Mac to produce `ProspectorsPlus-<ver>.dmg`.

**Tests / sims:** `finds_sim.py` (finds-detector scenarios — must print "ALL SCENARIOS PASS"),
`finds_current_code.py`, `prospecting_selftest.py`.

**Docs / notes (many `.md`):** `Prospectors_Plus_Knowledge_Base.md`, `Prospecting_Macro_Report.md`,
`CALIBRATION_GUIDE.md`, `FINDS_LESSONS.md`, `RELEASE_*.md`, prompt files for past rebuilds, etc. Plus
this file.

**Legacy / auxiliary Python (not the active path; kept for reference):** `prospecting_macro.py`,
`prospecting_core.py`, `prospecting_new.py`, `prospecting_other.py`, `prospecting_friend.py`,
`run_old.py`. The active engine is `prospecting_old.py`; don't confuse these.

---

## 5. The engine (`prospecting_old.py`) — the supervisor in depth

The engine's heart is the **ROBUST SUPERVISOR: a fused cue + capacity state machine.** It re-senses
every tick and cross-checks:
- **WHERE am I** → the HUD prompt cue (`Collect Deposit` / `Pan` / `Shake`).
- **WHAT's in the pan** → the capacity bar (full / empty / partial).

Capacity is ground truth for "did it empty" (the Shake cue can stick on screen); the cue is ground
truth for "where am I." Every action **verifies its own result on the next tick**. A deadlock (same
situation N ticks) escalates through recovery nudges, then a safe stop. It self-heals from any start
state and from missed clicks, under/overshoots, stuck cues, and drift.

**The main loop, in order (Standard mode):**
1. **Dig** — on land, pan empty: hold the dig click, watch the capacity bar rise. Multi-dig builds
   repeat up to `MAX_DIGS_TO_FILL`. Options: `PERFECT` (release exactly on the green dig-bar; needs
   `DIG_TRIGGER_PIXEL`), `DIG_FILL_SMART` (watch the bar's *motion* so it doesn't re-dig mid-fill),
   `DIG_PIPELINE` (fire follow-up digs on the dig-animation rhythm, learning the count).
2. **Walk back into water** — hold **S** until the `Pan` cue (`PAN_BACK_MAX_MS`, `WATER_EXTRA_BACK_MS`).
3. **Glide & start shake** — hold **W** toward land; click to begin the shake just before the edge so
   momentum slides you onto land (`SHAKE_MOMENTUM_W`, `SHAKE_W_LEAD_MS`, `SHAKE_START_DELAY_MS`).
4. **Shake & drain** — rapid **clicks** (not a held press) empty the pan (`SHAKE_CLICK_MS`,
   `SHAKE_CLICK_GAP_MS`); `SHAKE_CLICKS`=0 means shake until the bar reads empty; `CAP_EMPTY_FRAC`
   sets how empty counts as empty.
5. **Land & prove** — settle on land (`POST_SHAKE_SETTLE_MS`), find the land cue
   (`DEPOSIT_MAX_MS`), optional `LAND_CUE_ASSIST`, then a tiny **probe dig** to confirm diggable land
   before the next real dig (`DIG_PROBE_MS`, `LAND_DIG_TRIES`).
6. **Repeat.**

**Detection layer:** `Detector` with `on_pan()/on_shake()/on_deposit()` (cue reads), `dig_bar_green()`
(green skill-check), capacity read (full/empty via the two `CAP_*` ends), and the HUD text layer
(white-pixel fraction → prompt word, the slow-but-authoritative corrector). Cue reads go through
`_cue(cue_key, region)` which returns a **pixel-mask match** when Advanced cue matching is on and a
mask exists (and the window matches), otherwise the **white-box** check (`SAMPLE_BOX`≈6px,
`CUE_WHITE_FRAC`≈0.12). `CUE_MASKS_ONLY` disables the box fallback for testing.

**Input engine:** OS-level HID input (Quartz `CGEvent` on macOS; `ctypes SendInput` on Windows),
high-precision timing, cursor move/click/scroll helpers (also used by Fortune River recovery).

**Threads & stats:** background OCR threads for finds and earnings; a `SessionStats` object counts
pans, digs, recoveries, nudges, shake-misses, relics, etc., streamed to the app. The **RAM-leak fix**
(§21) wraps each OCR iteration in an autorelease pool (`_ARPool`) so pyobjc Vision image buffers don't
accumulate.

**Emit lines:** the engine prints tagged lines the app parses (stats, geode countdown `__GEODE__`,
etc.). The app's live stats grid and HUD are driven by these.

---

## 6. Detection & calibration

**Calibration targets (`PIXEL_FIELDS` in `prospecting_ui.py`):**
- `CAP_FULL_PIXEL` / `CAP_LEFT_PIXEL` — the right/left ends of the capacity bar (defines its width;
  the macro reads fill/empty from this).
- `DEPOSIT_PIX` / `PAN_PIX` / `SHAKE_PIX` — a white pixel on each HUD prompt word.
- `DIG_TRIGGER_PIXEL` — the green dig skill-check target (only for Perfect dig).
- `MONEY_TL/BR`, `SHARDS_TL/BR`, `FIND_TL/BR` — OCR rectangles for tracking (corner pairs; leave room
  left of numbers that grow).
- `AUTOPAN_BTN_PIXEL` + `AUTOPAN_ON_RGB`/`AUTOPAN_OFF_RGB` — the game's Auto Pan button + its state
  colours (for Tracker relics). Optional `Open Fast Travel` / `Screen centre` for FR/SR recovery.

**Guided calibration wizard (app):** detect Roblox window → capacity RIGHT end (bar full) → capacity
LEFT end → `Pan` cue (in water) → `Collect Deposit` cue (on land) → `Shake` cue (shaking) → done. Each
step uses a full-screen **overlay** that proposes a spot (red ✕), with Confirm/Redo, or click-exact /
hover+Enter, Esc to cancel.

**Window-relative / auto-calibration.** Pixels can be stored as **window fractions** (ratios) so they
survive the Roblox window moving/resizing. `apply_auto_calibrate()` places them from the live window
rect (`find_roblox_rect()`); `CALIB_WINDOW_RECT` records the calibration window. `WINDOW_RELATIVE` is
the older shift-on-move toggle. When the window size/resolution drifts, the app shows a red
"recalibrate" badge and can disable stale masks (`State.recal_reason`).

**Advanced cue matching (pixel masks).** Instead of the white box, store the **exact white-letter
shape** of each cue as a packed bit mask: `CUE_MASKS[cue] = {ratio:[rl,rt,rw,rh] window-fractions, w,
h, bits:base64 packbits, px, preview:data-url}`. At start, `place_cue_masks()` positions each mask
from its ratio × the live window; matching requires ≥`CUE_MASK_FRAC`≈0.85 of masked pixels to be
white. Captured via a guided overlay flow: **locate** the word → **flood-fill toggle** each letter (and
the mouse) to include/exclude → **save** with a highlighted preview PNG shown in a persistent gallery
in the app. `White sensitivity` tunes the white threshold. This defeats "a player in white trips the
cue."

The overlay editor state lives on the `Api` (`_cm_cue/_cm_shot/_cm_box/_cm_white/_cm_mask/…`);
`_cm_floodfill` is a 4-connectivity BFS over the white component; `_cm_render` builds the dimmed
zoomed composite with the mask in green; `_cm_save` crops to bbox, packs bits, computes the window
ratio, and generates the preview.

---

## 7. The app / UI (`prospecting_app.py`)

A `pywebview` window. **All UI is generated in Python as strings** — chiefly `build_html()` (the main
window) plus `_hud_html()`, `ANALYTICS_HTML`, `COACH_HTML`, `PILL_HTML`, and `_OVERLAY_HTML` (the
calibration overlay). **The main UI body/script is one big raw triple-quoted string** (`r"""..."""`):
inside it `'` and `"` are fine, `\n`/`{}` are literal (raw, non-f-string) — so JS with braces is safe.

**The `Api` class** is exposed to JS (`window.pywebview.api.*`): `get_state`, `save_config`,
`start/stop`, `detect_roblox`, `wizard_propose`, `overlay_pick/confirm/cancel/image`, the cue-mask
methods (`start_cue_mask_capture`, `cue_toggle`, `cue_reset`, `cue_mask_status`, `clear_cue_mask`,
`set_advanced_cues`, `set_cue_masks_only`), builds (`export_build`, `import_build`,
`import_build_dialog`, `attach_build_file`, `download_build_file`, `remove_build_file`, `builds_info`),
`calibration_health`, `check_update`/`do_update`, `verify_access`/`access_state`, analytics/HUD window
control, and the Coach endpoints. `DEFAULT_BUILDS` ships "Geode Farm" + "Geode Farm 1-Tap" to everyone
via `_builds_all()`.

**Pages (sidebar tabs):** Run, Cycle, Builds, Calibrate, Relics, History, Access — plus grouped
section pages (Modes / Tracking / Alerts & limits / Advanced / Setup). Nav is built by `nav(tabid,
icon, label)`; tabs carry `data-tab` and a `<span class="navbadge" data-badge="…">`. Clicking
`.tab[data-tab="X"]` shows panel `#pX`. `PINNED` + `GROUPS` control ordering; there is **no "OTHER"
catch-all** anymore (every section has a home after the UX reorg).

**Cycle page:** the tuning centerpiece. Engine-tuning sections are *not* flat tabs — they become
**stages** on a live diagram with a millisecond timeline. `_STAGES` maps each stage to its keys:
`dig`, `swalk` (walk back), `glide` (glide & start), `shake` (shake & drain), `land` (land & prove),
`safety` (safety nets), plus an auto "other" catch for any unclaimed key. Every numeric setting renders
as a slider (bounds from Coach `RANGES`) synced to a number box; keys keep their `data-key`, so builds/
config/Coach are untouched. Each stage block is `#cs_<stage>`; diagram nodes are
`.cnode[data-stage="…"]`.

**Health badges:** `setNavBadge(tabid, level, tip)`; red on Calibrate for hard-stops/stale
calibration; yellow on the **Cycle** tab when per-pan nudge / shake-miss / recovery rates are high
(after ≥5 pans), with a combined tooltip pointing users at the timings to fix.

**Other windows:** Analytics dashboard, HUD/pill overlay (draggable, always-on-top, stage + stats +
find ticker), Coach side panel, and the calibration overlay.

**Update banner** (`check_update`) reads `docs/version.json`; `do_update` downloads/relaunches the
installer.

---

## 8. The settings schema (`prospecting_ui.py`)

`SECTIONS` is the ordered list of `(section_title, [(KEY, label, type, default), …])`. Types: `int`,
`bool`, `str`, `float`. There are **144** settings across 17 sections. `SECTION_HINT[title]` gives a
one-line plain-English description of each section. `TAB_ICON[title]` gives an SVG icon.
`PIXEL_FIELDS` / `PIXEL_DEFAULTS` define the calibration targets. See `03_SETTINGS_REFERENCE.md`
(in `tutorial_handoff/`) for the full extracted table.

Sections (titles): Easy tuning, Tracker, Relic behaviour, Earnings, Treasure chest, Shards, Geodes,
Mode / Dig, Walk back into water, Shake, Return to land (dig-probe), Recovery / safety, Recovery
movement (jitter taps), Notifications, Auto-stop, Window, Advanced tuning. The seven engine-tuning
titles (Mode/Dig, Walk back, Shake, Return to land, Recovery/safety, Recovery movement, Easy tuning)
are `_MOVED` onto the Cycle page as stages rather than shown as flat tabs.

**Never drop or rename a KEY.** Builds and configs are keyed by these; renaming breaks saved setups.
Labels can change; keys cannot. The verification protocol counts exactly 144.

---

## 9. The Coach (`prospecting_assistant.py`)

An in-app tuning assistant with two modes: **offline** heuristics (pattern-matches a described problem
to setting changes) and an **AI API mode** (calls a model with the current config + problem). It emits
"suggested change" cards that the user applies with one click (then Save settings to persist). It also
owns **`RANGES`** — the known-safe (min, max, step) per numeric setting — which double as the Cycle
page slider bounds. Chat history persists to `coach_history.json`. The Coach is the primary "I don't
know what this setting does" escape hatch for non-power-users, alongside the tutorial and health badges.

---

## 10. Modes (with history/reasoning)

- **Standard** (no mode) — the full §5 loop. Covers most money/xp builds.
- **Treasure chest** (`TREASURE_MODE`) — chest hunting: **no shake**; dig `TREASURE_DIGS` times (often
  1), then **strafe L/R** to the next Collect prompt, dig, repeat. Slow gear uses a long
  `TREASURE_DIG_GAP_MS` (~12000ms) to wait out the animation.
- **Shards** (`SHARDS_DIG_CLICKS`) — exact-click digging for shard farms: fire a fixed click count,
  prove it registered via the capacity bar or the **green dig-bar** (`SHARDS_GREEN_CONFIRM`, frames
  earlier), and with `SHARDS_ASSUME_FULL` start walking back the moment the fill begins. Solves "digs
  2–3 times before the bar moves" on very fast builds.
- **Geodes** (`GEODE_MODE`) — built specifically for **very slow fill+shake animations** (10% dig-speed
  geode shovels). It taps the dig `GEODE_DIGS_TO_FILL` times (0 = auto until full), **waits out the
  animation** between taps (`GEODE_DELAY_MS`) so it doesn't false-nudge mid-fill, confirms the dig
  started via the green bar (`GEODE_START_MS`), then runs the **normal walk-back + momentum shake**
  (NOT Treasure's strafe), shaking until truly empty (`GEODE_SHAKE_HOLD_MS` caps it). The HUD shows a
  **live dig-delay countdown**. Two default builds ship: "Geode Farm" and "Geode Farm 1-Tap." This was
  a large, iterative build (multi-dig timing, recovery cascade suppression, shake-until-empty, empty-
  threshold tuning) — see §19.

---

## 11. Recovery & safety nets (the escalation ladder)

On by default; framed to users as "seat belts." Gentlest first:
1. **Stuck detection** — same situation for `STUCK_TICKS` reads = stuck.
2. **Recovery nudges** — small un-wedge movements (`RECOVER_ENABLED`, `RECOVER_BACK_MS`, jitter taps
   `BURST_ON_MS`/`BURST_OFF_MS`), up to `RECOVER_LIMIT`.
3. **Shake re-attempt** — retry an unregistered shake (`SHAKE_RETRY_ENABLED`); after
   `SHAKE_GLITCH_LIMIT` bad shakes, quick click-to-empty; after `SHAKE_FAIL_LIMIT`, stop.
4. **Break-out** — escape a stuck loop by repositioning (`BREAKOUT_ENABLED`, `BREAKOUT_SHAKE_MS`,
   `BREAKOUT_REPOS_MS`), up to `BREAKOUT_LIMIT`.
5. **No-progress watchdog** — nothing changes for `NO_PROGRESS_SEC` → force click-to-empty.
6. **Safe stop** — pause & retry (`SAFE_STOP_RETRY`) every `SAFE_STOP_RETRY_SEC`, up to
   `SAFE_STOP_MAX_RETRIES`, then finally stop. Keeps it from hard-quitting on a transient hazard.

Run-tab counters (nudges / shake-misses / recoveries) map to specific tuning: nudges→Land&prove,
shake-misses→Shake, recoveries→Safety/calibration. Advanced recovery: Fortune River (`FR_*`) and
Starfall River (`SR_*`) fast-travel-back on soft stop (map-specific, complex, off by default), and
X-pattern (`X_PATTERN`) diagonal walk-backs to fight drift.

---

## 12. Tracking (three distinct things)

- **Earnings** (`EARN_TRACK`) — OCR money & shard HUD totals every `EARN_OCR_SEC`s for Analytics
  (macOS Vision; macOS-only). Needs Money/Shards regions calibrated.
- **Finds** (`FINDS_TRACK`) — watches the item pop-up stack, logs identity + rarity, feeds the ticker
  and loot value. A fade-direction stack tracker counts fast bursts and duplicates accurately and
  reads coloured tier text; `FINDS_STACK_NEWEST` (bottom/top) is the main knob. macOS-only. See
  `FINDS_LESSONS.md` and `finds_sim.py`.
- **Tracker / Auto-Pan** (`TRACKER_MODE`) — **watch-only** benchmarking: sends zero input, reads the
  capacity bar, and counts the **game's own Auto Pan**, so you can compare it to the macro on the same
  ruler (runs labelled TRACKER). Optional `TRACKER_RELICS` runs relics while the game auto-pans via a
  safe "Auto Pan off → relic → Auto Pan on" dance (needs the Auto Pan button + ON/OFF colours
  calibrated); `AUTOPAN_GUARD`/`AUTOPAN_STALL_SEC` re-enable/kick it if it toggles off or wedges.

---

## 13. Notifications, auto-stop, relics

- **Notifications** — `WEBHOOK_ENABLED` DMs via the Prospectors Discord bot (enter `WEBHOOK_USER`).
  Toggle events: start, stop, periodic stats (every N min), safe-stop, recoveries (noisy; off),
  errors; `NOTIFY_SCREENSHOT` attaches an image. The webhook URL is injected at build time from a
  GitHub secret (`PP_SYNC_URL`).
- **Auto-stop** — `AUTOSTOP_ENABLED` + `AUTOSTOP_MINUTES` (time cap), `STOP_AFTER_PANS` (bag-full
  guard).
- **Relics** — auto-fire timed items on cooldown; `RELIC_ON_LAND` waits for safe land, `RELIC_LAND_MAX_S`
  is the max wait, `RELIC_RELATIVE` keeps timers counting while paused.

---

## 14. Access-code system (machine-locked invites)

Access is invite-only. On first launch the user enters a code; `verify_access(code)` binds it to this
machine. `access_state()` re-checks on each launch: unlocked / moved (copied to another PC → re-lock)
/ revoked. Revocation is driven by `docs/codes.json` (a published list of valid/active code hashes);
the app revalidates once per launch against it (fail-open when offline). Codes are machine-locked so
copying the config to another machine re-locks it. Owner tooling/notes live in `ACCESS_CODES_*`.

---

## 15. Builds

A build is a named snapshot of all settings. On the Builds page: search, sort (newest/oldest/most-
used/recent), describe, load, overwrite. **Sharing:** `Export` writes a `.ppbuild` file; `Import`
loads a friend's (auto-renamed on clash, never overwrites). **Attachments:** `Attach` embeds a
Roblox-equipment doc (Word/PDF/image/text, base64) *inside* the build; a `Download Roblox build`
button hands it back, so the recipient knows exactly what gear to make — and the macro is already
tuned for it. Attachments travel inside the single `.ppbuild`. `DEFAULT_BUILDS` (Geode Farm + Geode
Farm 1-Tap) ship to everyone via `_builds_all()`; `builds_info` merges defaults + saved.

---

## 16. The interactive tutorial / tour

A Steam-style spotlight tour built into `build_html()` (both app copies, identical). It dims the app
and cuts a lit hole around a target, with an explainer card (title + body + step dots + Back/Next/
Skip). It auto-switches tabs per step and returns to Run at the end. Auto-starts once on first launch
(gated so it never collides with the access gate; persists `localStorage 'pp_tour_done'`); replay via
the toolbar **❓ Tutorial** button. There's also a persistent "getting started" strip on the Run page.
**As of 2026-07-14 the tour is a full teaching system** (built from `tutorial_handoff/`, unreleased):
a `TOURS` registry of 9 named tours / 75 steps — `main` (20 steps, Calibration-first order, "how the
macro runs" overview), plus deep-dives `calibrate` (13), `cycle` (10, one step per `#cs_*` stage),
`recovery` (8, the escalation ladder rung by rung), `modes` (5), `tracking` (6), `builds` (5),
`relics` (4), `alerts` (4) — with troubleshooting callouts woven in from the reference §11.
`startTour(name)` runs any of them; each deep-dive **auto-offers once** the first time its tab is
opened (per-tour `localStorage 'pp_tour_<name>'` flags; main keeps `'pp_tour_done'`), every covered
page gets an injected **"✨ Explain this page"** button, and the **❓ Tutorial** button now opens a
Tutorials menu listing all 9. The four positioning bugs are fixed per the spec: instant scroll +
double-rAF measurement (never mid-smooth-scroll), `#tour` reparented to `document.body`, popover
measured after reflow and clamped on-screen with right→left→below→above flip + a pointing arrow +
centered fallback, backdrop `pointer-events:none` / card `auto`. Steps support `tab`, `sel`,
`row:1` (highlight the enclosing setting row via `[data-key=…]`), `open` (expand a `<details>`),
`center`, and body links `data-tourlink` (chain to another tour) / `data-jump` (flash a Cycle
setting via `cygJump`). ←/→/Esc keyboard nav; `prefers-reduced-motion` respected. There is also a
persistent **"How the macro runs"** `<details>` panel (`#howworks`) on the Run tab.

**v2 overhaul (2026-07-14, unreleased, both copies byte-identical, all six rendered HTML surfaces
identical):**
- **Tour content moved to Python** `TOUR_DEFAULTS` (9 tours / 74 steps) and served to the JS via the
  `tutorial_content()` Api. Merge order defaults < remote cache < local owner edits, per entry id.
  Nav fix: sub-tours now **expand the collapsed sidebar `.navgroup`** before clicking a section tab
  (was the cause of "Modes/Tracking don't open"); every sub-tour switches to its home tab even on a
  centered step. Content corrected: "lands in the water" is a **Glide-and-start / momentum** problem
  (`SHAKE_W_LEAD_MS`, not Land-and-prove); Treasure = the **Rubble Creek deposit↔sands** two-step (not
  chests); no "fish" (no fish in the game). **No em dashes and no `&`/`&amp;` anywhere a user can see**
  (swept across the whole app, not just the tour).
- **Owner-editable tutorials + media.** `_is_owner()` gates edit mode (true when the gitignored
  `prospecting_secrets.json` has `OWNER:true` or a `SYNC_URL`; users never see it). In-card and
  in-help **Edit** buttons write overrides to `tutorial_content.json` via `save_tutorial_entry` /
  `reset_tutorial_entry`; `pick_tutorial_image` embeds a screenshot as a data-url; `export_tutorials`
  writes a publishable `tutorial_content.json`; the app pulls `TUTORIAL_CONTENT_URL` into
  `tutorial_remote_cache.json` at launch (`_tutorial_refresh_remote`) so content updates ship without
  a release. Cards and deep help render an image (click to enlarge via `#lightbox`) and/or a YouTube
  embed.
- **Preview panel** (`#preview`, right side, mutually exclusive with Coach, which **no longer
  auto-opens**). Hovering a sidebar tab shows a scaled non-interactive snapshot of that page; hovering
  any setting/button/stat/stage shows deep help (`what it is / what it's for / the problem it fixes`);
  hovering the Cycle timeline adds a per-block ms breakdown from `cycModel`. Help content lives in
  `prospecting_ui.py`: `HELP` (144 settings, deepened) + new `UI_HELP` (79 controls/stats/stages/
  calibration rows, keyed e.g. `startbtn`, `stage:dig`, `cal:MONEY`, `cyc:graph`).
- **Drag-a-box region calibration.** The Money/Shards/Find corner pairs collapse into three
  `data-regionkey` rows; `start_overlay_region` + `overlay_region` + a REGION branch in the overlay
  let the user drag one rectangle that fills both `_TL_PIXEL`/`_BR_PIXEL` keys (keys unchanged).
- **Discord notifications fixed.** `post_webhook` now falls back to `SYNC_URL` when `WEBHOOK_URL` is
  empty (shipped configs carry `SYNC_URL`, so a source/user run delivers), sends a Discord **embed**
  (unknown JSON keys are dropped by Discord, so user/event/stats must be embed fields) and attaches
  the screenshot as a real **multipart** upload. New **Send test notification** button + `test_webhook`
  Api. Root cause was an empty delivery URL on non-build runs.
- **Toolbar simplified:** removed the redundant `#buildsbtn` (Builds is a sidebar tab); added the
  `#prevtoggle` Preview button.

**v2.1 (2026-07-14, same session, unreleased):** the Preview panel now has **total hover coverage**
and never goes blank. Hovering a sidebar **group header** (Modes, Tracking, Alerts) lists its
sub-tabs with descriptions; hovering a sub-tab or any pinned tab shows a **sanitized** page snapshot
(clones strip `data-key`/`id`/`name` and disable inputs, fixing a real bug where a cloned panel would
pollute `collect()`/`getElementById`); hovering any setting resolves by **row zone** (the whole
row/card/button is the target, so hovering the label text works, not just the input); hovering the
HUD / Analytics / Pop out / Coach buttons renders a **styled visual mock** of that window; the Cycle
timeline gives a per-block ms breakdown; and an absolute catch-all (`showGeneric`) guarantees the
panel always shows something. **Every one of the 144 settings now has a full-paragraph explanation**
(avg ~300 chars, what it is / what it affects / when to change it / the bug it fixes) plus 82 UI_HELP
entries for controls, stats, stages and calibration rows. The **Analytics window** got its own
floating hover explainer (`#atip`) with a paragraph on every card and section. Also fixed
pre-existing **mojibake** in the Analytics window (`Â·` mid-dots, mangled dashes) and swept em dashes
out of the **Coach** messages (`prospecting_assistant.py`) and **engine event** strings
(`prospecting_old.py`). All six rendered surfaces remain byte-identical mac vs Windows.

Verifier `tour_check.py` (repo root) runs all six protocol checks plus help-coverage and an em-dash/&
sweep, and asserts both app copies render identical HTML. Historical spec: `tutorial_handoff/`.

---

## 17. Release pipeline

**Windows:** push a `v<version>` tag → `.github/workflows/build-windows.yml` runs PyInstaller
(`prospecting.spec`) + Inno Setup (`installer.iss`) to produce `ProspectorsPlusSetup.exe`, attached to
the GitHub Release. The workflow **injects the `PP_SYNC_URL` Discord webhook** into the build
unconditionally (the `if: secrets.…` form silently never runs, so we inject always and check for empty
inside the step). MyAppVersion in `installer.iss` tracks the version.

**macOS:** run `build_dmg.command` on the owner's Mac to build `Prospectors Plus.app` and
`ProspectorsPlus-<ver>.dmg`. `Info.plist` carries the version. The app is unsigned, so first launch
needs the Gatekeeper "Open Anyway" dance (documented on the site + in release notes).

**Update banner:** `docs/version.json` (version, url, installer, critical, notes) drives the in-app
"update available/critical" banner via `check_update`. Bumping it notifies all existing users.

**Website:** `docs/index.html` on GitHub Pages — OS-aware hero (auto-detects mac/Windows), equal-
weight macOS + Windows download buttons, and a Gatekeeper "how to open on macOS" modal.

**Release checklist (owner runs the git/build parts):** bump `docs/version.json`, `installer.iss`,
`Info.plist`; write `RELEASE_v<ver>.md`; commit; tag `v<ver>`; push tag (triggers Windows build);
build the DMG locally; upload the DMG to the release; verify both assets attach. All git/tag/push and
both platform builds are **the owner's to run** — the assistant prepares files and gives commands but
does not run git against the repo (see §21 on stale locks).

---

## 18. mac ↔ Windows lockstep + verification protocol

**Lockstep rule:** every change to an engine/app/ui/assistant file is mirrored into the `windows/`
copy; shared code stays byte-identical. Only the input/capture layer and a few platform bits differ.

**Verification protocol — run after every change:**
1. `py_compile` all 8 active Python files (4 mac + 4 windows).
2. `node --check` every `<script>` extracted from `build_html()`, `_hud_html()`, `ANALYTICS_HTML`,
   `COACH_HTML`, `PILL_HTML`, `_OVERLAY_HTML` (both copies).
3. Confirm exactly **144** unique `data-key` settings render.
4. `python3 finds_sim.py` → "ALL SCENARIOS PASS".
5. Confirm mac/Windows lockstep (slice-compare the shared blocks).
6. Rebuild `Prospectors Plus Windows.zip` (14 files) if anything under `windows/` changed.

The assistant keeps reusable one-off Python checkers for these (JS extraction + node --check + key
count + target resolution + lockstep). Always green before moving on.

---

## 19. Version history & feature timeline

- **1.x–2.x** — foundational macro, calibration, basic modes, early releases (`RELEASE_v1.0.0` …
  `RELEASE_v2.2.0`).
- **4.0.0** — the big rework: the **Cycle page** (visual loop + timeline), the **Builds page**,
  **Shards mode**, the **live HUD overlay**, a reworked **finds detector**, macOS shake-smoothness
  fix, and **machine-locked access codes**.
- **4.1.0** — **Geodes mode** (slow-animation dig+shake), live dig-delay countdown, green-confirmed
  digs, shake-until-empty, the **v3 geode preset**, and "closing the app fully quits it."
- **4.2.0** (current) — **build sharing** (export/import + attachments), a **second geode build**
  (Geode Farm 1-Tap), the **major RAM-leak fix**, a **shake-miss counter**, no more false "capacity
  mis-calibrated" stop on fast builds, and a smoother shake.
- **Post-4.2.0 work in progress (not yet released):** advanced **cue-mask matching** (spoof-proof
  detection with the per-letter mask editor + persistent gallery + masks-only test toggle),
  window/resolution **recalibrate badges**, the **health-badge** system, a big **UX reorg** (nav
  regrouped, "Smart/experimental"→"Advanced tuning", getting-started strip, timer shows hours), and
  the **interactive tutorial** (now being expanded — see `tutorial_handoff/`).

---

## 20. Design philosophy & "our thoughts"

- **Ground-truth cross-checking over single signals.** The whole engine philosophy is "never trust
  one sensor." Cue says where, capacity says what; each action re-verifies. This is why it survives
  stuck cues and missed clicks that would derail a naive click-timer macro.
- **Graceful degradation, never flail.** Recovery escalates gently and ends in a *safe stop* (pause &
  retry), not a crash or a hard stop, so overnight runs survive transient hazards.
- **Tune by seeing, not by guessing.** The Cycle page turns abstract millisecond knobs into a visual
  timeline you can watch; the Coach translates plain-English problems into exact changes; health
  badges point at the setting to fix. All three exist because most users aren't the owner.
- **Builds are the unit of sharing.** A build carries the tuning *and* (optionally) the equipment doc,
  so "here's my geode setup" is one file. Defaults ship so new users have a working start.
- **Keys are forever, labels are flexible.** Config/builds are keyed by stable KEYs; we rename labels
  freely for clarity but never keys, so old builds never break.
- **Lockstep discipline.** Two platforms, one behavior. Every edit is mirrored and verified; the
  Windows zip is a build artifact, not a source of truth.
- **Owner runs git/builds.** The assistant prepares and verifies but hands the owner the exact
  commands for anything touching git history, tags, or platform builds — after real pain with stale
  `.git/*.lock` files from a sandboxed git and a mis-tagged release (§21).
- **Spoof-proofing.** Advanced cue masks exist because the cheapest possible detection (a white box)
  has a real failure mode (a white-clad player). We keep the cheap path as default and offer the
  bulletproof path opt-in.
- **Compensating for no video.** The owner can't record a tutorial, so the in-app tutorial must
  *teach*, not just point — this is why we're investing so much in it.

---

## 21. Known issues, gotchas, and things not to break

- **GitHub Actions `if: secrets.X != ''` silently never runs.** We inject `PP_SYNC_URL`
  unconditionally and check for empty inside the step. Don't "optimize" this back.
- **Stale git locks.** A sandboxed git left `.git/index.lock` / `.git/HEAD.lock`; the fix was
  `rm -f` and thereafter the **assistant never runs git against the repo** — the owner does. A
  `v4.1.0` tag once landed on the wrong (old) commit because of a failed commit; deleted and re-tagged.
- **The UI is a raw triple-quoted string.** Don't break it: `'`/`"`/`\n`/`{}` are all literal;
  inserting multi-line JS into a single-line HTML string caused an "unterminated string literal" once.
  Use raw match strings and keep anchors unique (a `renderCueCaps();` anchor matched 3 places — use a
  more specific anchor).
- **Tour positioning bugs** (measuring during smooth scroll; fixed-position containing block) — fixes
  documented in `tutorial_handoff/02_TOUR_SYSTEM.md`.
- **Health badges must target real nav tabs.** Early yellow badges targeted `_MOVED` sections (which
  live on the Cycle page, not as tabs) so they never showed; they now target the `cycle` tab.
- **macOS Vision memory can't be checked from the sandbox** (no pyobjc there). The `_ARPool` leak fix
  was validated by reasoning + the owner watching RAM.
- **Never drop a setting key / the count must stay 144.** Old builds must never zero settings added
  after they were saved (the loader keeps current values for keys absent from a loaded build).
- **App must fully quit.** Closing the window now runs `_quit_everything` (kills the engine subprocess,
  restores default signal handlers) — Ctrl+C only stops the macro loop, closing the window ends
  everything. Don't regress this.

---

## 22. Conventions for future work

- **Mirror every shared edit into `windows/`**, then run the full verification protocol (§18). Green
  before moving on.
- **Don't invent settings/cues/behaviors.** Cross-check against `prospecting_ui.py` (`SECTIONS`),
  `PIXEL_FIELDS`, and the engine constants. Use `tutorial_handoff/03_SETTINGS_REFERENCE.md` for the
  label↔key map.
- **Prefer labels over keys in UI text**, keep keys stable in code.
- **Ask the owner before**: bumping the version, releasing, deleting/renaming keys, removing a
  setting/section, or running anything that touches git.
- **When adding UI**, follow the existing patterns: `nav()` for tabs, `#p<tabid>` panels, `data-key`
  on setting inputs, `RANGES` for slider bounds, health badges via `setNavBadge`, and keep the raw
  triple-quoted string valid.
- **When changing detection/modes**, remember the cross-check philosophy and the recovery ladder; add
  a `finds_sim`-style check where feasible; test on both fast and slow (geode) builds.
- **This file and `tutorial_handoff/` are living docs** — update them when architecture, files, or
  decisions change so the Project's memory stays true.

---

## 23. Prospector Studio (custom block scripting)

**Built 2026-07-16 (unreleased).** Studio is the app's own "Roblox Studio": users visually
compose custom farming modes from Prospecting-specific blocks and run them through the real
engine. Full docs: `PRODUCT_SPEC.md`, `ARCHITECTURE.md`, `DECISIONS.md` (30 recorded
decisions), `TEST_PLAN.md`, `EVALUATION.md`.

**Where everything lives (all mirrored, no new runtime files):**
- `prospecting_ui.py` -> `STUDIO_BLOCKS` (17 block types: params/ranges/icons/tagged help),
  `STUDIO_GROUPS`, `STUDIO_KEY_WHITELIST` (W A S D, 1-9, Shift, Space), `STUDIO_CONTAINERS`,
  `STUDIO_MAX_BLOCKS`/`_DEPTH`. THE single source of truth: the app renders the palette from
  it, the validator walks it, and `studio_tests.py` asserts the engine's handler table matches
  its type set exactly (drift guard).
- `prospecting_app.py` -> the script model (`_studio_validate`/`_studio_sanitize`/templates/
  two-phase persistence in `prospecting_scripts.json`), ~20 `studio_*` Api methods, the
  `STUDIO_HTML` editor window (`_studio_html()`), the Studio sidebar tab + library, the
  Run-tab Mode selector, History `script` badges, tours `studio` + `studio_editor`.
- `prospecting_old.py` -> "CUSTOM SCRIPTS (Prospector Studio)" section: `ScriptRunner` (an
  explicit-stack walker, ONE block step per supervisor tick, sleeps sliced at 25 ms so
  Esc/Ctrl+K/Pause abort instantly), `script_tick`, `_SCRIPT_HANDLERS`. Dispatch order:
  Tracker > script > Treasure > supervisor. Config plumbing: `SCRIPT_MODE`, `SCRIPT_ACTIVE`,
  `SCRIPT_JSON` ride `prospecting_config.json` (written only by `studio_set_active`;
  `save_config` cannot touch them).

**The contract:** a script is data, never code: `{format:"ppscript", version:1, name, ...,
blocks:[{id,type,params,children}]}`. The whole top level is an implicit repeat-forever loop;
ONE completed pass = ONE pan (cycles/clean/cycle_ms counted like built-ins). Share format
`.ppscript` = `{_ppscript:1, app, script}`, native dialogs, auto-rename, sanitize + strict
re-validation on import (unknown types / >500 blocks / >16 depth are refused; params clamped;
ids regenerated). Builds can carry a `.ppscript` as their attachment.

**Safety rails (independent of the editor, re-enforced at runtime):** key whitelist at
runtime (safe stop on violation, zero events sent), every wait clamped to [100 ms, 120 s],
do-nothing scripts stop themselves (50 empty passes), 180 s no-step stall watchdog, lap cap,
`finally: release_all()` on every abort, interpreter exceptions -> `safe_stop` (engine
process never dies). Script recovery deliberately uses the simple rungs only (timeouts,
watchdogs, safe stop with retry); nudges/break-outs assume standard geometry (decision #25).

**Blocks (17):** dig, shake (until-empty + optional hold-W momentum), hold_key, tap_key,
click (cursor / screen centre / Auto Pan button), wait, relic, notify, wait_cue (the
walk-until-prompt primitive: optional held key + optional "leave the current prompt first"
fresh matching), wait_cap, if_cue, if_cap, if_not, repeat, group, stop, comment. Templates:
Standard loop, Treasure (Rubble Creek) (the MVP acceptance script), Blank.

**Verification additions:** `studio_tests.py` (dev-only, not shipped) must print
`STUDIO TESTS: ALL PASS`; `tour_check.py` now node-checks the STUDIO surface, resolves the
`studio` tour against the main html and `studio_editor` against STUDIO_HTML, and
byte-compares six studio lockstep regions (app x4 + engine + ui schema).

**Gotchas:**
- Keep the interpreter's `_SCRIPT_HANDLERS` and `STUDIO_BLOCKS` in lockstep; the drift test
  fails otherwise. Never rename a block type or param key (saved .ppscript files break).
- The tab-switch handler in the main JS has a HARD-CODED pinned-id list; any new pinned tab
  must be added there too (this bit Studio once).
- The editor tour's seen-flag lives in `prospecting_scripts.json` meta, NOT localStorage
  (html-string secondary windows do not share reliable localStorage).
- One live-game Treasure pass is still owner-verification (built + proven against the
  deterministic detector/input stubs overnight; see EVALUATION.md).
