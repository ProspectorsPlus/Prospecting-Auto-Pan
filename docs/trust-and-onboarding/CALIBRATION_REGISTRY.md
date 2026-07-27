# Calibration Registry — Reviewer Reference

Internal reviewer documentation for `lite_onboarding.CALIBRATION_ITEMS`
(`lite_onboarding.py:157-363`), the registry behind wizard Step 3 (Guided Calibration) and the
Trust Center calibration view. The registry is *derived from what the runtime actually reads*
(`prospector_engine/sensing.py` `PIXEL_KEYS` :35-41 / `CORE_PIXEL_KEYS` :43-45 + engine
defaults) — it does not invent settings. Engine read sites below come from the verified
inventory in `docs/trust-and-onboarding/CURRENT_SYSTEM.md` §4.

## The single-store guarantee

There is exactly one calibration store. The guided wizard drives the **same sensing engine
and the same save path** as the Calibrate tab:

- The wizard's "Calibrate (guided)" button clicks through to the Calibrate tab's own wizard
  (`#wizbtn`) rather than reimplementing it (`prospecting_app.py:10052-10053`); pixel and
  region buttons call the same `start_overlay_calibrate` / `start_overlay_region` overlay
  flows (`prospecting_app.py:10054-10057`) as the tab.
- Every semantic calibration write funnels through `Sensing.save_pixels`
  (`prospector_engine/sensing.py:821`), which derives `CAP_BAR_WIDTH`, re-derives
  `PIXEL_RATIOS`, captures the window rect and forces `AUTO_CALIBRATE=False` +
  `WINDOW_RELATIVE=False` on interactive manual saves (`sensing.py:913-919`; host wrapper
  `Api.save_pixels`, `prospecting_app.py:3268-3278`).
- Statuses shown in the wizard are computed from the live config by
  `lite_onboarding.calibration_status` (`lite_onboarding.py:379-443`) via
  `Api.calibration_registry` (`prospecting_app.py:2854-2885`) — the UI invents nothing.

## Atomicity and rollback

All engine-side saves go through `prospector_engine/settings.py:104` `atomic_write`:
tmp file + fsync + `os.replace`, with a rolling `.bak` of the previous good config and a
`.corrupt.bak` preserved if the on-disk JSON was ever unparsable (CURRENT_SYSTEM.md §3). A
crash mid-save can therefore lose at most the transaction in flight, never the store; the
previous calibration survives in `prospecting_config.json.bak`. (Known, tracked exception:
a handful of app-side non-calibration writes still use bare `open()+json.dump` —
CURRENT_SYSTEM.md §8.7.)

## Status semantics

`calibration_status(cfg, health, window_found)` returns one of six statuses per item
(`lite_onboarding.py:379-443`); the wizard renders them with text labels, never colour alone
(`prospecting_app.py:10018`):

| Status | Meaning | How it is computed |
|---|---|---|
| `auto` | Placed from the built-in ratio profile each run start; runnable out of the box | Required item and `AUTO_CALIBRATE` truthy (default True — `prospector_engine/engine.py:728`; placement `apply_auto_calibrate`, `engine.py:855-882`) (`lite_onboarding.py:431-435`) |
| `ok` | User-calibrated and still valid | Required item, auto off, health not failing (`:441-442`); optional item with a value set (`_pix_set`, `:368-376,414-419`); `roblox_window` when the window is found (`:400-401`) |
| `stale` | User-calibrated but the Roblox window moved/resized since | Required item, auto off, `Sensing.health()` reports not-ok (live rect vs `CALIB_WINDOW_RECT`, ±4 px — `sensing.py:754`; wired in `Api.calibration_registry`, `prospecting_app.py:2860-2865`) (`:436-440`) |
| `default` | Shipped default coordinates, never confirmed by the user | **Defined in the vocabulary but not currently emitted**: `_pix_set` can distinguish a shipped default (returns `None` when a `default` argument is supplied, `:374-375`) but `calibration_status` never passes one, so today required items resolve to `auto`/`ok`/`stale` only. Documented honestly as a reserved status. |
| `unset` | No value yet | Optional item whose activating feature is ON but keys are empty (`:420-424`); `roblox_window` when Roblox is not open / not checked (`:402-407`) |
| `off` | Feature that needs this item is disabled | Optional item, activating flag(s) false (`:425-428`); condition strings are parsed as upper-case flag names split on "or" (`:410-413`) |

`calibration_ready(statuses)` (`lite_onboarding.py:446-456`): every required item except
`roblox_window` must be `auto` or `ok`; anything else is a blocker surfaced by the Readiness
Check (`Api.readiness_check`, `prospecting_app.py:3029-3041`). Note `launch()` itself performs
no calibration validation (CURRENT_SYSTEM.md §4) — `AUTO_CALIBRATE=True` plus the baked ratio
profile (`PIXEL_RATIOS_DEFAULT`, `sensing.py:50-58`) make the app runnable out of the box, so
readiness reports honestly instead of blocking.

---

## Required items (drive every classic cycle)

Classification evidence: with these wrong, the run itself breaks — the engine mis-times digs,
never sees the pan fill, or walks past the water. They never block the app (auto-calibration
covers them), but stale/missing values here are run-breaking, hence `required: True`.

### roblox_window

- **Keys:** none (detection only). Everything else is calibrated relative to the window.
- **Action:** `detect` → `Api.detect_roblox` via the wizard card (`prospecting_app.py:10048-10051`).
- **Engine read sites:** `prospector_engine/platform_mac.py :: find_roblox_window` L106
  (owner name + bounds only, never window titles — CURRENT_SYSTEM.md §5),
  `prospector_engine/platform_win.py :: find_roblox_window` L78.
- **Status:** `ok` when found, `unset` otherwise — Roblox simply not being open is stated as
  fine-to-continue, not a failure (`lite_onboarding.py:399-407`).

### cap_bar — Pan capacity bar

- **Keys:** `CAP_FULL_PIXEL`, `CAP_LEFT_PIXEL`, `CAP_BAR_WIDTH` (derived, verified > 20 px).
- **Engine read sites:** `prospector_engine/engine.py:190,196` (bar fill/empty/dig-registered
  detection — the heartbeat of every cycle); saver `sensing.py:821` (`Sensing.save_pixels`
  derives the width).
- **Breaks if wrong:** the macro cannot tell full from empty pans; every classic mode stalls
  or loops.

### pan_prompt / deposit_prompt / shake_prompt — the three white cue pixels

- **Keys:** `PAN_PIX` / `DEPOSIT_PIX` / `SHAKE_PIX`.
- **Engine read sites:** `prospector_engine/engine.py:214-216` (cue checks anchoring
  water-side, land-side, and shake-start confirmation respectively).
- **Auto-proposal:** `Sensing._detect_cue_px` (`sensing.py:304`) proposes each pixel during
  the guided flow; the user confirms.
- **Breaks if wrong:** walk-back overshoots the water (pan), digs start blind (deposit),
  missed shakes waste pans instead of being retried (shake).

## Optional / conditional items

Each names the setting that activates it; the wizard shows them only as optional with the
skip consequence spelled out (`skip_consequence` fields, rendered at
`prospecting_app.py:10038`). Evidence of graceful degradation: trackers silently skip or
disable when regions are unset (`engine.py:1664-1676, 2018-2020`), recovery/tracker modes log
and degrade (CURRENT_SYSTEM.md §4).

| id | Keys | Condition (activating flag) | Engine read sites | If skipped |
|---|---|---|---|---|
| `dig_green` | `DIG_TRIGGER_PIXEL` | `PERFECT`, `SHARDS_GREEN_CONFIRM` or `GEODE_GREEN_CONFIRM` | `engine.py:170` (dig-bar green zone; `Detector` dig_region/is_white reader) | Perfect mode and green-confirm nudges don't fire; plain digs still work |
| `money_region` | `MONEY_TL_PIXEL`, `MONEY_BR_PIXEL` | `EARN_TRACK` | `engine.py:449-454`; min-size validation `engine.py:1673` / `sensing.py:404-439`; test read `Sensing.test_read` (`sensing.py:395`) | Earnings stats stay empty |
| `shards_region` | `SHARDS_TL_PIXEL`, `SHARDS_BR_PIXEL` | `EARN_TRACK` | same as money (min 12×8) | Shard stats stay empty |
| `find_region` | `FIND_TL_PIXEL`, `FIND_BR_PIXEL` | `FINDS_TRACK` | `engine.py:449-454,2018` (min 20×10) | The finds log stays empty |
| `fortune_river` | `FR_OPEN_PIXEL`, `FR_HOME_PIXEL`, `FR_SCAN_X`, `FR_BOX_TOP`, `FR_BOX_BOTTOM` | `FR_RECOVERY` | `engine.py:740-760` (recovery click group); saved via the FR group path in `Sensing.save_pixels` | Fortune River auto-recovery stays off |
| `autopan_button` | `AUTOPAN_BTN_PIXEL` | `TRACKER_MODE` | `engine.py:388-391` (Auto Pan toggle verification) | The tracker degrades gracefully and logs it |
| `cue_masks` | `CUE_MASKS` | `ADVANCED_CUES` | placed with drift detection `engine.py:1239-1260`; capture/save `Sensing.cue_save` (`sensing.py:626`) | The simpler single-pixel checks are used |

`_pix_set` treats `[0, 0]` as unset (`lite_onboarding.py:368-376`), matching the engine's
own "[0,0] = unset" convention for regions (CURRENT_SYSTEM.md §4). The `cue_masks` item is
the one non-pixel key, checked by truthiness of the mask dict (`:416-417`).

## Example screenshots (honest-placeholder pipeline)

Wizard cards can show an annotated example image — but **only when a real, owner-approved
capture exists** in `assets/onboarding/calibration/manifest.json`
(`Api.calibration_example`, `prospecting_app.py:2899-2928`). Until then the card shows a
clearly-labelled pending note whose alt text describes what the image will show
(`prospecting_app.py:10075`) — never a fabricated screenshot. The capture tool
(`owner_example_capture` / `owner_example_approve`, `prospecting_app.py:2930-2979`) runs only
on the owner's dev checkout (`FROZEN or not _is_owner()` guard) and saves un-approved; the
owner crops/redacts externally, then approves. **Final public packaging remains blocked on
the owner supplying approved screenshots** — an owner action, tracked in the release
checklist.

## Schema hook

`CALIBRATION_SCHEMA = 1` (`lite_onboarding.py:29`) is stored in the wizard state file
(`calibration_schema` field). A future calibration-schema bump can compare stored vs current
and re-open only the calibration step; see FIRST_RUN_STATE.md for the mechanism and its
current (not-yet-exercised) status.

## Test coverage

The onboarding/trust suite (`onboarding_trust_tests.py`, being written in parallel; run in CI
at `.github/workflows/ci.yml:56-57`) pins registry sanity (every item maps to real config
keys, conditions parse), status computation and readiness blocking. Engine-side behaviour
(auto-calibration placement, save atomicity, degradation when regions are unset) is covered
by the engine suites listed in TEST_MATRIX.md. Windows execution of all of this is prepared,
not executed, in this pass.
