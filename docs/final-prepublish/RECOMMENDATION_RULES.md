# Recommendation rules — the complete inventory (1.0.0-rc.6)

Every diagnostic rule in `lite_diagnostics.py`, with its exact trigger,
severity ladder, confidence policy, setting targets (with bounds
source), and FAQ id. The engine is pure (no screen access, no engine
import, no file I/O outside the two suppression helpers) and
crash-proof (`evaluate({})`, `evaluate(None)` and an all-None context
all return `[]` — diagnostics_tests.py:648-656). `diagnostics_tests.py`
(185 executed checks) pins every number below in both directions:
a tripping context and a below-threshold context per rule.

## Shared vocabulary

- Severities: `INFO / NOTICE / WARNING / ERROR / CRITICAL`
  (lite_diagnostics.py:93). The host maps ERROR/CRITICAL to the red
  badge bucket and WARNING/NOTICE to yellow; INFO never badges
  (prospecting_app.py:545-550).
- Confidence: `possible / medium / high` (line 94), assigned by the
  shared do-not-overstate policy `confidence_from_ratio`
  (lines 517-524): observed/threshold ≥ 2.0 → high; ≥ 1.34 → medium;
  else possible. Rules with categorical evidence (a permission is
  granted or it is not) use fixed `high`. When confidence is `possible`
  the title hedges ("may").
- Evidence: every event carries `observed` (one measured sentence) and
  `evidence` (a list of numeric facts with the threshold inline, e.g.
  `"nudges: 9 in 12 cycles (0.75/cycle, threshold 0.6)"`).
- Escalation: `LADDERS` (lines 1564-1581) maps recurrence count →
  severity; `merge_events` dedupes by event `code`, carries
  `first_seen` forward, increments `recurrence_count`, and only ever
  *upgrades* severity by ladder. Codes absent from a fresh evaluation
  are dropped (resolved).
- Suppression: `apply_suppressions` never suppresses CRITICAL events or
  events with `suppressible: False`; the store is
  `{code: {until, forever}}`, written atomically by
  `save_suppressions`.
- Auto-apply policy: `make_recommendation` forces `auto_apply=False`
  when any setting target lacks a RANGES bound or a suggested value, or
  when there are no setting targets at all. `clamp_suggestion` clamps
  every suggestion into `[lo, hi]`. Bounds come from
  `prospecting_assistant.RANGES` (plus two mirrored slider bounds for
  `SAFE_STOP_RETRY_SEC` / `SAFE_STOP_MAX_RETRIES`,
  lite_diagnostics.py:53-54).

## The rules

16 rule callables (`_RULES`, lines 1517-1534) — lettered A–O in their
docstrings plus the unlettered CAL-STALE rule — emitting 18 distinct
event codes (H and M each emit two). Evaluation order = the table
order = event priority (tie-break after severity and confidence).

| # | Code | Trigger (exact) | Severity (ladder: recurrence→sev) | Confidence | Setting targets (suggested; RANGES lo/hi/step) | Other targets | FAQ id |
|---|---|---|---|---|---|---|---|
| 0 | `PP-D-PERM-SCREEN_DETECTION` / `PP-D-PERM-INPUT_CONTROL` (M, lite_diagnostics.py:1348) | capability `== "not_granted"` (`stop_hotkeys` deliberately excluded — rule N owns it) | CRITICAL (no ladder); `dismissible=False`, `suppressible=False` | high | — | permission: that capability | `faq-mac-screen-recording` / `faq-mac-input-monitoring` |
| 1 | `PP-D-SAFESTOP` (N, :1388) | `stop_hotkeys` ∈ (`not_granted`,`test_failed`,`failed`) | ERROR (1→ERROR); `suppressible=False` | high | — | permission: `stop_hotkeys` | `faq-safe-stop-hotkeys` |
| 2 | `PP-D-CAP-SUSPECT` (H1, :936) | `cap_bar` status `== "needs_review"` | ERROR (1→ERROR) | high | — | calibration: `cap_bar`; rec carries `repair_action: "test_capacity"` | `faq-capacity-calibration` |
| 2 | `PP-D-CAP-HARDSTOP` (H2, :968) | `stats.hard_stops >= 1` | ERROR (1→ERROR) | high | — | calibration: `cap_bar`; `repair_action: "test_capacity"` | `faq-capacity-calibration` |
| 3 | `PP-D-CAL-STALE` (:1481) | `cal_health.ok` false | WARNING (1→WARNING) | high | — | calibration: every item with status exactly `stale` | `faq-roblox-window-changes` |
| 4 | `PP-D-STALL` (J, :1085) | `run_active` and ≥1 `no_progress` in `recent_events`; cause chain: missing REQUIRED permission > window lost (`window_found is False`) > stale calibration | WARNING (1→WARNING, 3→ERROR) | permission/window branches: high; calibration branch: medium if anything stale else possible | — | branch-dependent: permission target, `roblox_window`, or the stale calibration list (fallback `cue_masks`) | branch-dependent: `faq-mac-screen-recording` / `faq-mac-input-monitoring` / `faq-safe-stop-hotkeys` / `faq-window-detection` / `faq-roblox-window-changes` |
| 5 | `PP-D-NUDGE-FAR` (A, :531) | `cycles >= 5` and `nudges/cycles >= 0.6` | WARNING (1→WARNING) | high if rate ≥ 0.8 else medium | `WATER_EXTRA_BACK_MS` −80 (0/1200/80); `SHAKE_START_DELAY_MS` +50 (0/1000/50); `LAND_SETTLE_MS` +25 (0/400/25) — all auto-apply | — | `faq-movement-nudges` |
| 6 | `PP-D-SHAKE-EARLY` (B, :615) | `shake_start_retry >= 3` or `shake_glitch >= 2` | WARNING (1→WARNING) | ratio policy (max of retries/3, glitches/2) | `SHAKE_START_DELAY_MS` +50 (0/1000/50); `SHAKE_START_CONFIRM_MS` +50 (0/1500/50); `EASY_SHAKE_DELAY_MS` +60 (0/1200/60) — all auto-apply | — | `faq-shake-timing` |
| 7 | `PP-D-SHAKE-LATE` (C, :694) | `cycles >= 5` and `shake_misses/cycles >= 0.4` | WARNING (1→WARNING) | ratio policy (rate/0.4) | `SHAKE_START_DELAY_MS` −50 (only when currently > 0); `SHAKE_BAIL_MS` +150 (200/2500/150) — auto-apply | calibration: `cue_masks` (3rd rec, not auto) | `faq-shake-timing` |
| 8-10 | `PP-D-CUE-PAN-MISS` / `PP-D-CUE-DEPOSIT-MISS` / `PP-D-CUE-SHAKE-MISS` (D/E/F factory, :770-838) | `no_progress >= 1` and (prompt item or `cue_masks` status ∈ (`stale`,`needs_review`), or `cal_health.ok` false) | NOTICE (1→NOTICE, 3→WARNING, 6→ERROR) — driven by in-run stall count and again by recurrence | high when cal bad AND health bad; medium when cal bad and stalls ≥ 3; else possible | — | calibration: `cue_masks` + the prompt item | `faq-missed-prompts` |
| 11 | `PP-D-AUTOPAN-STUCK` (G, :841) | `autopan_kick >= 2` or `autopan_guard >= 3` | WARNING (1→WARNING) | ratio policy (max of kicks/2, guards/3) | `AUTOPAN_SETTLE_MS` +50 (100/2000/50); `AUTOPAN_TOL` +5 (10/120/5); `AUTOPAN_STALL_SEC` → 5 (only when currently 0; 0/60/1) — auto-apply | calibration: `autopan_button` (only when its status ∉ (`ok`,`auto`)) | `faq-autopan-stuck` |
| 12 | `PP-D-RECOVERY-LOOP` (I, :996) | (`cycles >= 5` and `recoveries/cycles >= 0.5`) or `recovery_rung >= 3` | WARNING (1→WARNING, 4→ERROR) | ratio policy (max of rate/0.5, rungs/3) | `RECOVER_LIMIT` −1 (1/8/1); `NO_PROGRESS_SEC` +2 (0/60/2); `RECOVER_BACK_MS` +40 (60/600/40) — auto-apply | — | `faq-recovery-loops` |
| 13 | `PP-D-FINDS-MISS` (K, :1190) | gate: `FINDS_TRACK` on; then `find_region` status ∉ (`ok`,`auto`) or `finds_ghost+finds_fork >= 3` | NOTICE / WARNING when churn ≥ 3 (ladder 1→NOTICE, 3→WARNING) | high when region bad AND churn ≥ 3; medium region-only; else ratio policy (churn/3) | `FINDS_MIN_CONF` −0.05 (0.0/1.0/0.05, float); `FINDS_CARD_SEC` +1 (3/15/1); `FINDS_EMPTY_MS` +50 (200/3000/50) — auto-apply | calibration: `find_region` (when bad; not auto) | `faq-finds-popup-box` |
| 14 | `PP-D-ANALYTICS-STALE` (L, :1292) | gate: `EARN_TRACK` on; then `money_region`/`shards_region` status ∉ (`ok`,`auto`) | NOTICE (1→NOTICE) | high | `EARN_OCR_SEC` → 10 (only when currently > 60; 3/120/1) — auto-apply | calibration: exactly the bad regions (not auto) | `faq-analytics-regions` |
| 15 | `PP-D-BUILD-CONFLICT@<refusal>` (O, :1421-1478) | `launch_refusal` ∈ `no-studio-build` / `no-studio-script` (NOTICE) / `classic-with-active-build` / `mode-kind-mismatch` (WARNING) | per-refusal (ladder 1→NOTICE never downgrades) | high | — | deep link overridden to the `studio`/`script` tab | `faq-importing-builds` |

Host-synthesized (not in `lite_diagnostics.py`): `PP-D-CAL-REQUIRED`
(prospecting_app.py:573-608) — ERROR, high, fired when
`lite_onboarding.calibration_ready()` reports blockers, so the
calibration badge stays honest immediately after Mark-wizard-complete;
FAQ `faq-advanced-cues`.

## Recommendation shape

Every recommendation carries: `id`, `title`, `explanation`,
`setting_targets` (each with `key/label/tab/section/control/current/
suggested/delta/units/lo/hi/step/reason` — `setting_target`,
lite_diagnostics.py:321-349), `calibration_targets`,
`permission_targets`, `expected_effect`, `tradeoff`, `verify`,
`priority`, `auto_apply`. Deep links derive from the targets
(`_links_from_recs`, :384-412).

## Cross-registry contracts (test-enforced)

- Every rule-emitted `faq_id` resolves in `FAQ_BY_ID` (20 entries;
  `validate_faq` also checks every FAQ's related settings /
  calibrations / permissions against the real registries).
- Every setting target is a real `prospecting_ui.SECTIONS` key; every
  suggested value lies inside its RANGES bounds; no unbounded target is
  ever auto-apply.
- Every calibration target is a real `lite_onboarding.CALIBRATION_ITEMS`
  id; every permission target a real `lite_trust.CAPABILITIES` id.
- `cap_bar.related_diagnostics` in the calibration registry names
  exactly `PP-D-CAP-SUSPECT` and `PP-D-CAP-HARDSTOP` — asserted from
  both sides (diagnostics_tests.py:321-323, capacity_tests.py).
