# Diagnostic architecture (1.0.0-rc.6)

How the diagnostic/recommendation system is layered: a pure engine, a
host that assembles evidence and owns persistence, and one UI surface
that renders and deep-links. Line references are to the rc.6 tree.

```
lite_diagnostics.py (PURE: no screen, no engine import, no file I/O*)
  models · setting registry · 16 rules · ladders/merge · FAQ KB
        ▲ ctx                                   │ events
prospecting_app.py Api (HOST)                   ▼
  _diag_ctx assembly · diagnostics_state cache/debounce ·
  PP-D-CAL-REQUIRED synthesis · diagnostics_state.json store ·
  diag_apply/diag_undo/diag_dismiss/diag_suppress · setting_locator
        ▲ bridge                                │ payload
embedded JS (ONE UI)                            ▼
  renderDiagBadges (sole badge writer) · #diagdrawer · #diagrec chip ·
  #faqmodal · navigateToSetting/Calibration/Permission
```

\* except `load_suppressions`/`save_suppressions`, which take explicit
paths (module docstring, lite_diagnostics.py:25-27).

## Models (lite_diagnostics.py)

- `make_event` (:415-446): `id/code/severity/category/title/summary/
  observed/evidence/confidence/recommendations/other_causes/faq_id/
  deep_links/dismissible/suppressible/source/priority`. Severities
  `INFO..CRITICAL` (:93); confidences `possible/medium/high` (:94) via
  the shared `confidence_from_ratio` policy (:517-524). Evidence is a
  list of numeric facts with thresholds inline. Recurrence fields are
  added by `merge_events`, not here.
- `make_recommendation` (:352-381): targets (setting/calibration/
  permission), `expected_effect/tradeoff/verify`, `auto_apply` — forced
  False without bounded setting targets (:359-366).
- `setting_target` (:321-349): key/label/tab/section/control/current/
  suggested (clamped)/delta/units/lo/hi/step/reason.
- Deep links derive from targets (`_links_from_recs`, :384-412).

## Rules and post-processing

16 rule callables (`_RULES`, :1517-1534; inventory with exact
thresholds in RECOMMENDATION_RULES.md) emit 18 codes. `evaluate(ctx)`
(:1537-1556) wraps every rule in try/except and stamps priority = rule
index; `evaluate({})`, `evaluate(None)` and an all-None ctx return `[]`
(diagnostics_tests.py:648-656). `merge_events` (:1602-1634) dedupes by
`code`, carries `first_seen`, increments `recurrence_count`, applies
`LADDERS` (:1564-1581) upgrade-only, and drops resolved codes.
`apply_suppressions` (:1669-1694) never suppresses CRITICAL or
`suppressible: False` events.

## Host ctx assembly — `Api._diag_ctx` (prospecting_app.py:4275-4345)

Real sources only, no new capture:

- `stats` — the latest engine stats frame (`_last_stats`).
- `event_counts` / `recent_events` — per-run safety-event counters
  (reset at run start, :7309) and a rolling window of the last 100
  safety-event records (:7359-7368).
- `cal_status` — `lite_onboarding.calibration_status` with live window
  health; `cal_health` — `Sensing.health()` (window-rect compare, no
  grab); `window_found` — only from the cached health poll (the UI's
  existing 8 s tick, :12241 — never a fresh grab).
- `capabilities` — `lite_trust.capability_statuses` flattened, plus a
  session-scoped `test_failed` synthesis for `stop_hotkeys` when this
  session's real test failed (:4307-4321).
- `settings` — DEFAULTS merged with the saved config restricted to
  known keys; `mode`/`build_active`; `launch_refusal` — the last typed
  refusal from `launch()` (:6220-6229); `run_active`.

## Host evaluation — `Api.diagnostics_state` (:4347-4396)

2-second debounce (`force` bypasses; cache invalidated by apply/undo/
dismiss/suppress and `launch()`). Pipeline: `evaluate(ctx)` → host
synthesis of **PP-D-CAL-REQUIRED** (`_diag_blocker_event`, :573-604 —
classic mode only, from `lite_onboarding.calibration_ready`; keeps the
calibration badge honest immediately after "Mark wizard complete",
before any run telemetry exists) → `merge_events` against the prior
snapshot → `apply_suppressions` → session dismissals
(`_diag_dismissed`; a dismissal auto-clears when its code resolves, so
a recurrence reappears, :4381-4390) → `{events, summary, when}`.

`_diag_summarize` (:533-570) buckets ERROR/CRITICAL as red and
WARNING/NOTICE as yellow (INFO drops) per owning tab;
`_diag_badge_tab` (:504-531) decides ownership host-side: permission
evidence → trust, cycle-controlled settings → cycle, calibration →
cal, tab-targeted conflicts → their tab.

## Persistence — `diagnostics_state.json` (DIAG_FILE, :187)

Exactly three keys (:473-502, atomic tmp+fsync+replace, never raises):
`suppressions` `{code: {forever, until}}`, `history` (last 50
dismiss/suppress records), `applied` (last 20 apply snapshots
`{id, key, prev, next, when}` for undo). No event payloads persist.

`diag_apply` (:4410-4463): server-side re-validation against
`SETTING_REGISTRY` (`safe_auto_apply` + bounds mandatory), clamped via
`clamp_suggestion`, one key per apply, written through the single
config-writer path, snapshot recorded; refusals carry `PP-DIAG-APPLY`.
`diag_undo` (:4465-4498) restores `prev` and consumes the record.
`diag_suppress` refuses CRITICAL/non-suppressible (:4519-4535);
`diag_unsuppress_all` (:4537-4542) backs the Trust Center's "Show
suppressed warnings again".

## UI: one badge writer, one drawer

`renderDiagBadges` (:10892-10900) is the only badge writer: clears all
badges, red wins over yellow, text = count (or "!"), tooltip = the top
issue's title. Badge clicks stop propagation (no tab switch) and open
`#diagdrawer` at the tab's top event (:10902-10907); the cal/cycle
banners are clickable the same way (:10909-10915). The drawer's
per-event sections (`eventHtml`, :10953-11001) are, in order: header
strip (severity chip + seen ×N + code), What happened, What Prospector
Lite observed (+ evidence list), Most likely cause (CONF_LABEL
wording), Recommended first action, Exact settings to review
(current → suggested + Open/Apply/Undo), Exact calibrations,
Permissions, Other possible causes, Expected effect, Tradeoff, How to
verify, Related FAQ, then Copy details / Dismiss / Don't-show-again
(hidden for CRITICAL).

Deep links resolve through `Api.setting_locator` (:4398-4408) — the JS
never text-matches: `navigateToSetting` (:11074-11096, cycle rows via
`cygJump`, section tabs via panel-id derivation, `#diagrec` rec chip
near the control), `navigateToCalibration` (:11113-11126, Calibrate-tab
anchors or the wizard's `renderCalDetail` when the wizard is open;
`cap_bar` additionally reveals the Test-capacity affordance),
`navigateToPermission` (:11127-11138, polls for the async-rendered
trust card, flashes its Test button). The FAQ browser `#faqmodal`
(:10019-10026, :11141-11194) serves `lite_diagnostics.FAQ_ENTRIES` (20
entries) with search over questions + symptoms and exact-surface
buttons; entry points: Help menu, drawer, Settings, Calibrate, wizard
readiness, Trust Center.

## Privacy stance

Computed and stored locally; nothing transmitted; no new network
behavior; no silent capture (the ctx reuses data the app already
holds). What persists is the three-key store above. Both
`diagnostics_state.json` and `tutorial_state.json` are listed in
`Api._DATA_FILES`, appear in the Trust Center Local Data table, and
are removed by "Delete ALL local data" (a gap found and closed during
this pass).

## Verification

`diagnostics_tests.py` — 185 executed checks: registry derivation and
placement, clamping, every rule in both directions (tripping and
below-threshold ctx) with exact suggested values, ladders/merge,
suppression store semantics, FAQ cross-registry validation, and the
global invariants (every emitted event's faq id resolves; every
suggested value in bounds; every target a real registry id). The
drawer/deep-link/apply-undo journey is driven end-to-end by
`wizard_ui_tests.js` scenario H against the REAL `evaluate()` output.
