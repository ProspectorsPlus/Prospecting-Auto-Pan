# Calibration crash / failure root-cause report

Baseline: `f1e943cb39edb75bc041129e098133ab6b9e3c35` (1.0.0-rc.2). Line numbers below refer to
that commit unless marked (fixed). Every wizard-reachable calibration path was traced from the
click handler through the pywebview bridge into `prospector_engine/sensing.py` and back.

## Confirmed defects (all fixed in the rc.3 stabilization pass)

| # | Defect | Evidence (baseline) | Fix |
|---|--------|---------------------|-----|
| 1 | `pixel`/`region` picker buttons discard the API's in-band `{error}` result — every failure was a silent no-op ("fails to launch") | JS handlers `try{await start_overlay_calibrate(...)}catch(e){}` ignore the resolved dict (10105-10108); the API reports failure in-band (5212-5218), never by rejecting | Handlers render `r.error` + `[error_code]`, with an "Open Trust & Permissions" route when `needs_permission` is set |
| 2 | `start_overlay_calibrate` returned `{ok: True}` without opening anything when the pre-created `_overlay` window is None | `try: if _overlay is not None: ...` falls through to `return {"ok": True}` (5226-5232) | Shared `_overlay_preconditions()` guard returns `PP-CAL-OVERLAY` error first, for all four overlay entry points |
| 3 | Screen-capture-denied opened a full-screen frameless on-top BLACK overlay with no explanation | No preflight anywhere on the capture path; mss returns an all-black frame when unpermitted | Preflight gate (`PP-CAL-SCREEN`) refuses to open the overlay and routes to the Trust step |
| 4 | `bool(s.detect_window())` is always truthy — "Roblox window found" claimed with Roblox closed | `find_roblox_window` returns the NON-empty dict `{found: False, error: ...}` (platform_mac.py:136-138); `bool(...)` used at 2899-2900 and 3052-3053 | Both sites read `.get("found")` |
| 5 | Confirming ANY single wizard item (even optional dig-green / a tracker box) force-flipped `AUTO_CALIBRATE=False`, turning all required items into blockers instantly | `save_pixels` forces the flags on every interactive save (sensing.py:909-915) | The flip now requires a core required key (`AUTHORITATIVE_PIXEL_KEYS`); optional-only saves also seed missing required ratios so a partial `PIXEL_RATIOS` can never starve auto placement. The pinned engine contract (core save → flags forced; import path → untouched) still passes |
| 6 | `_region_preview_save` truncate-wrote the whole config non-atomically; 12 further truncate-write sites existed; engine `atomic_write` renamed the live config away to `.bak`, leaving a no-live-file window for concurrent readers | 5335-5340; grep `with open(CONFIG_FILE, "w")` = 13 sites; settings.py:109-113 | All host writes route through one atomic tmp+fsync+replace helper; `.bak` is now a copy. A static test bans the truncate-write pattern from both app copies |
| 7 | Wizard "Detect" success rendered `r.rect` — a key the API never returns → `Found at ""` | 10101 vs platform_mac return shape `{found,x,y,w,h,title}` | Message uses `w×h at (x, y)` |
| 8 | Wizard "Test" printed raw `{}` JSON on a fresh auto-calibrated install — looks broken exactly when the status honestly says "runnable out of the box" | `sample_saved` samples only SAVED keys (sensing.py:219-257) | Empty results carry an explanation (auto-calibration places points at run start); the UI renders it as a note, not a failure |
| 9 | Overlay window geometry frozen at boot (main-display size at launch) — display changes/multi-monitor could mis-fit the overlay | main() creates it once at boot size (11628-11631) | `_overlay_show()` re-fits the overlay to the current main display on every open (best-effort) |
| 10 | `launch()` never enforced the calibration gate its own readiness docstring promised | readiness fails on calibration blockers (3062-3074) but launch() checks only the TCC gate (4912-4922) | launch() computes the same `calibration_ready` condition and refuses with `cal:<blockers>`; the Run tab routes to the Calibrate tab |

## Hypotheses checked and RULED OUT (with evidence)

- **`SETUP.suspend()` + immediate `.tab[data-tab="cal"]`/`#wizbtn` click racing app init** —
  not a defect: tab handlers (8989) and the `wizbtn` handler (9069) bind at script parse time;
  `init()` only populates values. The guided-modal flow works before init completes.
- **`_overlay.show()`/`evaluate_js` from bridge worker threads crashing Cocoa** — not a
  defect: pywebview 6.2.1 dispatches both through `AppHelper.callAfter`
  (site-packages/webview/platforms/cocoa.py:748-756, 915), and `shown` is set even for
  hidden windows. Main-thread-safe.
- **Missing bundled modules for calibration** — not a defect: `pyi-archive_viewer` over the
  rc.2 PYZ shows `ApplicationServices`, `HIServices`, mss, numpy, pynput and all
  `prospector_engine.*` present.
- **Frozen asset resolution for calibration examples** — works: the manifest is bundled
  (spec lines 45-48) and `calibration_example` resolves it via `_resource`/`_MEIPASS`; a
  missing/unapproved example renders the labelled placeholder, never a crash. (All twelve
  examples still ship as placeholders — owner screenshots remain owed; that is a content gap,
  not a crash.)

## Remaining known limitations

- With screen capture granted but Roblox absent, the overlay opens on a snapshot of the
  desktop — by design (the wizard instructs opening Roblox first; "Detect Roblox window" now
  reports absence honestly).
- The guided modal (`#wizbtn`) still starts at its intro step regardless of which required
  item's "Calibrate (guided)" button was clicked; the modal itself walks all four required
  points. Cosmetic, documented for a future pass.
