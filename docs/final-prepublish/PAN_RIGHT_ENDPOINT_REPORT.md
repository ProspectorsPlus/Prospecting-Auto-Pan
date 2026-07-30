# Pan capacity RIGHT-end calibration — root cause and fix (1.0.0-rc.6)

Companion to REPRODUCTION_REPORT.md issue 5 (the release blocker: a
right-end calibration that *looks* successful and then produces hard
stops). This report records the confirmed root causes, the shipped fix,
the migration story for existing installs, and the platform notes. All
line references are to the rc.6 tree.

## Root cause narrative (all reproduced, none guessed)

The reproduction probe (`python3 repro_capacity.py` against the real
`Sensing.save_pixels`, output in REPRODUCTION_REPORT.md) started from a
previously valid calibration (`CAP_FULL_PIXEL=[1122,894]`,
`CAP_LEFT_PIXEL=[678,895]`, `CAP_BAR_WIDTH=444`), re-ran right-end
calibration, and confirmed a bad point `[400,894]` — left of the left
tip. rc.5 saved it: `{"saved": ["CAP_FULL_PIXEL"]}`, no error channel,
stale width kept. The runtime fill band (`engine.py:1400-1411`, columns
`[right.x − WIDTH, right.x − 1]`) would then read `[−44, 399]` —
entirely off the bar. Five compounding causes:

1. **Manual clicks were unguarded at the anti-aliased edge.** The
   auto-detector deliberately walks both tips inward to *solid* gold
   because the literal edge pixel is a pale blend that fails the runtime
   `is_yellow` test; a manual overlay click got no walk-in and no colour
   check, so clicking the literal tip saved a point the pan-full check
   could never fire on — hard stops followed.
2. **No save-time validation.** `save_pixels` stored endpoints
   unconditionally: nothing checked right>left, row alignment, window
   bounds, or colour.
3. **Silent stale width.** `CAP_BAR_WIDTH` was only re-derived when
   `right.x − left.x > 20`; otherwise the old width silently persisted
   against new endpoints, and the return value had no error channel.
4. **The staged flow discarded the paired left tip.** The guided
   `CAP_RIGHT` confirm saved only `CAP_FULL_PIXEL`; the auto-detected
   same-frame left tip was dropped, and `CAP_LEFT` was a separate later
   capture on a NEW frame — bar drain or a window move between stages
   produced a mismatched pair.
5. **Runtime asymmetries amplify small errors** (the runtime never reads
   `CAP_LEFT_PIXEL`; `cap_fill` uses only the right tip's row; width
   rounding differed between save paths).

Ruled out: Retina/DPI transform mismatch — with the pinned mss 10.2.0
all coordinates share one physical-pixel space (see Platform notes).

## The fix

### 1. Endpoint guard — `Sensing.cap_endpoint_guard` (sensing.py:362-408)

Manual picks now go through the same predicate the auto-detector walks
to: `_solid_gold(r, g, b)` (sensing.py:353-360) — `r >= 140 and
g >= 140 and b <= min(r, g) - 55`, factored out so there is exactly ONE
definition. The guard walks up to **10 px inward** (left for
`CAP_RIGHT`, right for `CAP_LEFT`) with a ±1-row tolerance, returns the
adjusted point + its hex, and rejects off-bar picks with the clicked
hex quoted verbatim ("that point is not on the gold capacity bar - the
clicked pixel reads #1e1e1e and no solid gold sits within 10 px left of
it…"). `_solid_gold` is deliberately *stricter* than the runtime test
(blue gap 55 vs 45), so any point the guard accepts also passes runtime
`is_yellow` — pinned by capacity_tests.py:303-305.

### 2. Pure pair validation — `validate_cap_pair` (sensing.py:120-192)

A pure function (no screen, no store) returning `(ok, reasons, width)`.
Every reason names the actual numbers. The checks, with exact
thresholds: ordering (`rx >= lx`), same-end detection (`width > 20`),
minimum readable width (`width >= 24`), row alignment
(`|ry − ly| <= 8`), per-tip inside the capture frame, width plausibility
(`width <= 0.9 × frame_w`), per-tip inside the anchored Roblox window,
width vs window width, and the band-start check (`rx − width >= 0` — the
exact failure mode of the reproduced bug: "The runtime fill band would
start at x=−44, off the left edge of the screen…").

### 3. Atomic, honest save — `save_pixels` (sensing.py:1147-1312)

Any save touching `CAP_FULL_PIXEL` or `CAP_LEFT_PIXEL` validates the
*merged* resulting pair (incoming value else stored). On failure the
call **writes nothing** — the rejection returns before any store write,
so everything else in the same call is discarded too — and returns
`{"saved": [], "ok": False, "error": "cap_endpoints", "reasons": [...],
"right": …, "left": …, "width": …}`. On success `CAP_BAR_WIDTH` is
**always** rewritten from the validated pair (sensing.py:1219-1222); the
legacy `> 20` gate now applies only to saves that do not touch the pair.
The import path runs through the identical validation
(capacity_tests.py pins that a broken import writes nothing).

### 4. Overlay confirm — guard first, verbatim reasons, same-frame pair

`overlay_confirm` (prospecting_app.py:6858-6902) runs the guard before
anything else; a rejection returns without closing the overlay, the
reasons render verbatim in the overlay banner, and **Redo** stays alive
(the busy latch is released — locked in by wizard_ui_tests.js scenario
D). A `CAP_RIGHT` proposal confirmed *unchanged* saves the detector's
same-frame left tip with it (prospecting_app.py:6887-6898), killing the
mismatched-pair window; a manually re-picked right end saves alone and
validates against the stored left. Before replacing an already-suspect
stored pair, a one-time `prospecting_config.json.pre-caprepair.bak`
backup is taken (`Api._cap_repair_backup`, prospecting_app.py:3726-3742).

### 5. Test capacity calibration — `Sensing.capacity_probe` (sensing.py:530-644)

A new **Test capacity calibration** action on the Calibrate tab
(`#capTest`) and the wizard's cap_bar detail page evaluates a fresh grab
with the **exact runtime math**: the 6×6 tip box mirror of
`Detector._box` + `is_yellow`, and the `cap_fill` 20-px band fraction
with the engine's own `YEL_MIN`/`YEL_BLUE_GAP` thresholds. It reports
pass/fail, tip hex, fill fraction, per-line reasons (including
stored-width-vs-tips drift > 2 px), an annotated preview crop (endpoint
columns and band edges painted), and a **Recalibrate right end** action
on failure. Uncalibrated installs get an honest early-out with no
screen access.

## Migration / needs-review (existing installs)

`lite_onboarding.cap_pair_suspicion` (lite_onboarding.py:967-1006) is a
pure status check over the *stored* config — values are NEVER modified.
A stored pair reads **needs_review** when: right tip not right of the
left tip by > 20 px (inverted or same-end), tips > 8 px apart
vertically, stored width < 24 px, or stored width drifting > 2 px from
the tip distance ("the width is stale from an earlier calibration").
Every detail ends with the repair guidance "Run Test capacity
calibration or redo the Capacity step." Surfaced as: the wizard step's
NEEDS_REVIEW state with the reason, the Calibrate-tab "Needs review"
chip + banner (`Api.cap_bar_review`), and the `PP-D-CAP-SUSPECT`
diagnostic (severity ERROR, confidence high) whose recommendation
carries `repair_action: "test_capacity"`. The registry item pins
`related_diagnostics: ["PP-D-CAP-SUSPECT", "PP-D-CAP-HARDSTOP"]`
(lite_onboarding.py:254-260) — a contract asserted from both sides by
the test suites.

## Platform notes

- **mss must stay pinned at 10.2.0** (the version in
  THIRD_PARTY_NOTICES.md / docs/public-release/DEPENDENCIES.md and the
  reproducible-build freeze). With it, macOS grabs at nominal resolution
  and `platform_mac.get_scale` puts window rects, saved coordinates and
  capture frames in one physical-pixel space — which is exactly what
  `validate_cap_pair`'s frame/window bounds checks assume. An older mss
  would grab 2× while reporting points (a systemic hazard, documented in
  REPRODUCTION_REPORT.md, not this defect).
- **Windows** sets process DPI awareness at import
  (`platform_win.py:28-34`) so capture and cursor coordinates are
  physical pixels; `get_scale` is 1.0. Same single coordinate space,
  statically validated only (no real-Windows execution yet — see
  WINDOWS_PARITY.md).
- A Screen-Recording-denied grab on macOS returns an all-black frame;
  the guard would reject every pick with hex `#000000` — the calibration
  permission preflight exists precisely to catch that first.

## Verification

`capacity_tests.py` — 51 checks over synthetic frames (a scripted mss
seam; the pale anti-aliased edge columns are painted into the fixture
frames exactly as the real bar renders them): the pure validation rules,
the rc.5 reproduction constants replayed against `save_pixels` (zero
writes, byte-identical store on rejection), the guard walk-in/rejection
cases, `capacity_probe` pass/half-full/swapped/uncalibrated, and the
needs_review migration (including the never-mutates snapshot check).
Wired into CI. The engine parity golden for the calibration sequence was
regenerated for the additive `ok`/`width` fields in the save result —
deliberate, no detection behavior change.

## Known residual gaps (honest)

- Two gaps found while writing this report were fixed in the same
  pass: `Api.import_calibration` now propagates a `save_pixels`
  rejection (a rejected import writes nothing and reports the exact
  reasons instead of `{"ok": True}`), and the guard rejection now uses
  the same `"error": "cap_endpoints"` code as the pair rejection, so
  consumers switch on one string.
- No live-Roblox capture session was part of this pass; the fix is
  proven against synthetic frames and the recorded rc.5 reproduction,
  not a live game.

## Affected files

- `prospector_engine/sensing.py` — `_solid_gold` (353-360),
  `cap_endpoint_guard` (362-408), `validate_cap_pair` (120-192),
  `save_pixels` validation (1184-1231), `capacity_probe` (530-644).
- `prospecting_app.py` (+ byte mirror `windows/prospecting_app.py`) —
  overlay confirm guard + same-frame pair (6858-6902), rejection UI
  (8048-8064), `_cap_repair_backup` (3726-3742), `Api.test_capacity`
  (4976-4991), `Api.cap_bar_review` (4965-4974), Calibrate-tab /
  wizard capTest card (11461-11469, 8169-8181, 12987-13005).
- `lite_onboarding.py` — `cap_pair_suspicion` (967-1006),
  `calibration_status` needs_review path (1151-1158), cap_bar registry
  metadata (254-260).
- `capacity_tests.py` (new), `engine_goldens/parity/
  calibration-sequence.jsonl` (regenerated), `.github/workflows/ci.yml`.
