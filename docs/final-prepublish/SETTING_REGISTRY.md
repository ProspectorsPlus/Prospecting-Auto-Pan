# The setting registry (1.0.0-rc.6)

How `lite_diagnostics.py` knows what every setting is, where it lives
on screen, and what values are legal — derived from the app's existing
schema, never hand-duplicated. Line references are to the rc.6 tree.

## Derivation — `build_setting_registry()` (lite_diagnostics.py:267-299)

Three existing sources, imported tolerantly (:37-54 — a broken import
degrades to empty metadata rather than crashing the app):

- `prospecting_ui.SECTIONS` — the shipped settings schema: keys,
  labels, types, defaults, section membership. 17 sections, **146
  keys** → the registry has exactly 146 entries
  (`len(reg) == len(ui.DEFAULTS)` is a test invariant).
- `prospecting_assistant.RANGES` — the Coach's slider bounds
  `(lo, hi, step)`: 94 entries, plus two `setdefault` additions
  mirroring app sliders that RANGES lacked (`SAFE_STOP_RETRY_SEC`
  (10, 600, 10), `SAFE_STOP_MAX_RETRIES` (0, 10, 1), :53-54) → **96 of
  146 entries are bounded**; the other 50 carry `lo=hi=step=None`.
- `prospecting_ui.HELP` — per-key help text.

Plus one curated layer, `_META` (:131-264): `effects`, `tradeoffs`,
`related_diagnostics`, and `safe_auto_apply` for exactly the **16 keys
the rules target** (WATER_EXTRA_BACK_MS, SHAKE_START_DELAY_MS,
LAND_SETTLE_MS, SHAKE_START_CONFIRM_MS, EASY_SHAKE_DELAY_MS,
SHAKE_BAIL_MS, AUTOPAN_SETTLE_MS, AUTOPAN_TOL, AUTOPAN_STALL_SEC,
RECOVER_LIMIT, NO_PROGRESS_SEC, RECOVER_BACK_MS, FINDS_MIN_CONF,
FINDS_CARD_SEC, FINDS_EMPTY_MS, EARN_OCR_SEC).

Entry shape (:281-297): `key, label, section, type, default, control,
tab, lo, hi, step, units, help, effects, tradeoffs,
related_diagnostics, safe_auto_apply`. Units are parsed from the
shipped label (`_units_from_label`, :110-121: ms / s / % / fraction).

## The control-location model

The UI reality the deep links must match: seven engine-tuning sections
were absorbed by the Cycle page's stage editor, the rest are section
tabs. `_MOVED_SECTIONS` (:102-104) names the seven ("Easy tuning",
"Mode / Dig", "Walk back into water", "Shake", "Return to land
(dig-probe)", "Recovery / safety", "Recovery movement (jitter taps)");
`control` = `"cycle"` for their keys (54 of 146) else `"tab"` (92),
and `tab` is `"cycle"` or the section title, which is the data-tab id
(:287-288). The host exposes this as `Api.setting_locator`
(prospecting_app.py:4398-4408) so the JS deep-link layer
(`navigateToSetting` — `cygJump` for cycle rows, panel-id derivation
for tabs) never text-matches.

## Bounds and auto-apply policy

- `clamp_suggestion(key, value)` (:305-318): clamps into `[lo, hi]`
  when RANGES knows the key; int keys round to int, floats to 4 dp;
  unbounded keys pass through untouched — and are therefore never
  auto-appliable.
- Registry: `safe_auto_apply = curated flag AND key in RANGES`
  (:296-297) — curation alone is not enough, bounds are mandatory.
- `make_recommendation` (:359-366) force-disables `auto_apply` when any
  target lacks bounds or a suggested value, or when there are no
  setting targets at all (an Open-only recommendation is never
  one-click).
- The host re-validates independently in `diag_apply`
  (prospecting_app.py:4410-4433): non-registry keys, non-safe keys,
  unknown types, and non-numeric suggestions are refused with
  `PP-DIAG-APPLY`; the accepted value is clamped again and written
  through the single config-writer path, one key per apply, with an
  undo snapshot.

## Invariants tested (diagnostics_tests.py:80-153, 648-693)

- Every rule-referenced key exists in `ui.DEFAULTS` and the registry;
  every `_META` key is a real SECTIONS key; registry covers every
  SECTIONS key (146 = 146).
- Placement: `WATER_EXTRA_BACK_MS` → cycle; `FINDS_MIN_CONF` →
  "Earnings" tab; `AUTOPAN_SETTLE_MS` → "Tracker" tab; and globally
  `control == "cycle"` exactly for keys of the seven moved sections.
- Bounds flow from RANGES (`SHAKE_BAIL_MS` = 200/2500/150); a
  RANGES-less key (`TRACKER_POLL_MS`) carries `lo is None` and
  `safe_auto_apply is False`.
- Clamping at both ends, int and float; pass-through for unbounded
  keys; `setting_target` carries lo/hi/step and a correct delta.
- Global (over every event any test produced): every suggested value
  lies inside its RANGES bounds; no unbounded target is auto-apply;
  every setting target is a real SECTIONS key.
