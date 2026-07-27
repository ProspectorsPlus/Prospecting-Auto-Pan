# First-Run State Machine — Reviewer Reference

Internal reviewer documentation for `lite_onboarding.Onboarding`
(`lite_onboarding.py:52-145`), the state machine behind the five-step first run:

    1 Welcome  →  2 Trust & Permissions  →  3 Guided Calibration
               →  4 Readiness Check      →  5 the app

State lives in `onboarding_state.json` inside the user's data directory (packaged macOS
`~/Library/Application Support/Prospector Lite`, Windows `%APPDATA%\Prospector Lite`, dev =
the script folder — `prospecting_app.py:125-152`) — deliberately **not** in the main config,
so wizard progress and settings/calibration can never corrupt each other.

## States

`STATES` (`lite_onboarding.py:32-34`), strictly ordered:

    NOT_STARTED → WELCOME_COMPLETE → TRUST_STARTED → TRUST_COMPLETE
    → CALIBRATION_STARTED → CALIBRATION_COMPLETE → READINESS_COMPLETE → FINISHED

## Forward-only `mark()`

`mark(state)` (`lite_onboarding.py:102-114`) advances only when the target index is strictly
greater than the current one; unknown states and backward requests are silently ignored. This
means **a stray or repeated call can never un-finish setup** — the only sanctioned backward
transitions are:

- `rerun()` (`:130-135`) — sets `WELCOME_COMPLETE` (the wizard reopens at the Trust step)
  **without discarding completion history** (`completed_at`, `declined_optional`,
  `last_readiness` survive). Entry points: Tutorial menu "Re-run setup wizard"
  (`prospecting_app.py:8238,8249`), Trust Center (`:10187`), both via `__setupRerun`
  (`:10132`) → `Api.onboarding_rerun` (`:2841-2844`).
- `reset()` (`:137-142`) — a brand-new default state. Touches **only** the wizard state;
  builds, calibration, settings and history are untouched (`Api.onboarding_reset`,
  `prospecting_app.py:2846-2849`; Trust Center "Reset wizard progress only", `:10188`).
  The scoped data-deletion path (`delete_local_data("wizard")`,
  `prospecting_app.py:3214-3216`) removes only `onboarding_state.json` and drops the cached
  instance.

`FINISHED` additionally stamps `completed_at` (epoch seconds, `:111-112`).

## Persistence: atomic, crash-safe

`_save()` (`lite_onboarding.py:91-99`): write to `onboarding_state.json.tmp`, then
`os.replace` — a torn write can lose at most the last transition, never the file. `_load()`
(`:63-75`) is defensive: unreadable JSON, a non-dict, or an unknown `state` value all fall
back to a fresh default rather than crashing the boot path, and missing fields are
`setdefault`-ed in, so files written by older builds keep loading. Because every step marks
as it completes (see the transition map below), a crash at any point resumes at the same
step on next launch — the Quit button in the wizard says exactly that
(`prospecting_app.py:10109`).

## Schema / versioning fields

`_default_state` (`lite_onboarding.py:39-49`):

| Field | Purpose |
|---|---|
| `schema` | `SCHEMA_VERSION = 1` (`:28`) — the state-file format version. |
| `state` | Current state name. |
| `platform` | `lite_trust.platform_key()` at creation. |
| `product_version` | Stamped on every save (`:92`) — which app version last wrote the file. |
| `calibration_schema` | `CALIBRATION_SCHEMA = 1` (`:29`) — the calibration-registry generation this user completed Step 3 against. |
| `declined_optional` | Capability ids the user explicitly declined (`decline_optional`, `:121-128`; `Api.onboarding_decline`, `prospecting_app.py:2837-2839`). |
| `completed_at` | Epoch of reaching FINISHED. |
| `last_readiness` | The most recent Readiness Check summary (`record_readiness`, `:116-119`; written by `Api.readiness_check`, `prospecting_app.py:3103-3110`: `{ok, when, fails}`). |
| `migrated_from` | Present only on legacy migrations (below). |

## Legacy migration: why WELCOME_SEEN → FINISHED

`migrate_legacy(welcome_seen)` (`lite_onboarding.py:77-89`), called once at first
construction (`_onboarding()`, `prospecting_app.py:312-322`): if the state file does not
exist, the machine is at `NOT_STARTED`, and the old single-welcome-screen flag
`WELCOME_SEEN` is set, the user is marked **FINISHED** with
`migrated_from: "WELCOME_SEEN"`.

Rationale: a user who completed the pre-wizard welcome has a working, calibrated install;
forcing them back through setup would punish existing users for our redesign. The full
wizard stays one click away (Tutorial menu / Trust Center), and the launch gate protects
them anyway — permissions are enforced at Start Macro regardless of wizard state
(`prospecting_app.py:4861-4871`). The `os.path.exists` guard makes migration one-shot: it
can never overwrite real wizard progress.

## Boot and resume mapping (state → wizard page)

`Api.welcome_state()` (`prospecting_app.py:2638-2653`) returns
`{show, setup_needed, resume, info}` where `setup_needed = not finished()` and `resume` is
the raw state name. JS boot (`prospecting_app.py:9873-9878`):

1. Welcome gate still due → show it; its Continue button calls `welcome_done`
   (marks `WELCOME_COMPLETE`, `prospecting_app.py:2666-2670`) and then opens the wizard at
   the Trust page when setup is unfinished (`:9879-9883`).
2. Welcome done but wizard unfinished → `SETUP.resume(state)` (`:9877`).
3. Otherwise → straight into the app.

`SETUP.resume` maps state → page (`prospecting_app.py:10118-10119`) — the wizard has three
interactive pages for the five logical steps (Welcome is the gate; "the app" is exit):

| Stored state | Resumes at |
|---|---|
| `NOT_STARTED`, `WELCOME_COMPLETE`, `TRUST_STARTED` | Trust & Permissions |
| `TRUST_COMPLETE`, `CALIBRATION_STARTED` | Guided Calibration |
| `CALIBRATION_COMPLETE`, `READINESS_COMPLETE` | Readiness Check |
| anything else / unknown | Trust & Permissions (safe default) |

Transitions are marked at honest moments: opening the wizard or clicking any Request button
marks `TRUST_STARTED` (`prospecting_app.py:10116, 2715-2718`); the Continue buttons mark
`TRUST_COMPLETE`, `CALIBRATION_STARTED`+`CALIBRATION_COMPLETE`, and
`READINESS_COMPLETE`+`FINISHED` respectively (`:10127-10131`). "Continue anyway" is always
allowed — the wizard never traps the user; only Start Macro is gated, and the Readiness page
says so (`:10098`). `SETUP.suspend()` lets wizard cards hand off to the real Calibrate tab
or Notifications page and float a "return to setup" pill (`:10120-10124`), so leaving
mid-step neither loses progress nor fakes completion. Studio-hosted launches skip the wizard
entirely (`STUDIO_LAUNCH` forces `setup_needed=False`, `prospecting_app.py:2647-2649`).

## Reopening a step in the future (design intent)

Two version fields exist precisely so a future release can reopen **only the relevant
step**, using the same sanctioned-backward mechanism as `rerun()`:

- **Calibration-schema bump:** `CALIBRATION_SCHEMA` increments when the registry changes
  incompatibly (e.g. a new required item). On load, comparing the stored
  `calibration_schema` against the constant and setting state back to `TRUST_COMPLETE`
  reopens exactly the Guided Calibration page (per the resume map above) while leaving
  trust progress intact.
- **New mandatory capability:** needs no state surgery at all — `trust_state` and the
  Readiness Check read live registry + OS status every time, and `launch()` gates on the
  live status (`prospecting_app.py:4861-4871`). Setting state back to `WELCOME_COMPLETE`
  (exactly what `rerun()` does) would additionally re-present the Trust page on next boot.

Honesty note: **no bump has occurred yet** — today nothing consumes `calibration_schema`
beyond storing and defaulting it (`lite_onboarding.py:70`). The fields are the designed
hook, not a live mechanism; the onboarding/trust suite pins that they are written and
preserved so the hook stays usable.

## Test coverage

The state machine's contract — forward-only `mark`, rerun/reset semantics, atomic persistence,
tolerant `_load`, one-shot WELCOME_SEEN migration, `declined_optional` round-trip — is pinned
by the onboarding/trust suite (`onboarding_trust_tests.py`, being written in parallel with
this document; run in CI at `.github/workflows/ci.yml:56-57`). The welcome flow it wraps is
covered by `public_release_tests.py:335-366` (`child_app_offline`). Windows execution:
prepared, not executed in this pass (TEST_MATRIX.md).
