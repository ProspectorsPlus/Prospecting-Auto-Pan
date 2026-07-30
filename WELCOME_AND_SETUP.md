# Welcome, startup routing, and the Skip Wizard

How Prospector Lite decides what you see at launch, what "skipping the
wizard" really stores, and how to undo every choice. One function makes
the routing decision (`lite_onboarding.py`,
`compute_startup_route` — every combination of inputs is
regression-tested), and the app acts on its answer verbatim.

## What opens at launch

The rules, in priority order — the first one that applies wins:

1. **Launched by Prospector Studio** → straight to the main app (the
   Studio host owns onboarding).
2. **You explicitly opened Welcome** (Tutorial menu → "Welcome, privacy
   & version") → the Welcome screen, and continuing from it always goes
   into the setup wizard — never straight back to the app.
3. **You chose "Skip this time" earlier in this session** → the main
   app (nothing was written; next launch decides fresh).
4. **Setup is finished** → the main app — unless "Show this screen at
   every launch" is on, in which case the Welcome screen shows first
   (with "Open Prospector Lite" as the primary action). The "skip
   wizard automatically" preference also routes straight to the app.
5. **"Skip the setup wizard automatically on launch" is on** (even with
   setup unfinished) → the main app. Readiness warnings stay honest —
   anything actually missing still shows as a warning badge.
6. **Fresh install** (wizard never started) → the Welcome screen.
7. **"Show this screen at every launch" is on** → the Welcome screen.
8. **Otherwise** (setup started but not finished) → the wizard resumes
   at your saved step.

## The Welcome screen

Shows what the app does, its privacy posture, and the exact
version/build identity. Three checkboxes persist immediately: **Show
this screen at every launch**, **Skip the setup wizard automatically on
launch**, and **Open tutorial whenever Prospector Lite opens**.

When you open Welcome explicitly from the menu, the primary button reads
**"Continue through setup →"** and the full action list appears:

- **Continue through setup** — resume the wizard at your saved step.
- **Review permissions** — open the wizard's Trust & Permissions step.
- **Review calibration** — open the wizard's Guided Calibration step.
- **Start tutorial** — enter the app and start the main tour.
- **Open the main app** — enter the app directly.
- **Trust Center** — enter the app on the Trust Center tab.

## Skip Wizard: the four options and what each one stores

The **Skip wizard** button (on the Welcome screen and in the wizard
footer) opens a confirmation with exactly four options. Honest
persistence, option by option:

| Option | What is stored |
|---|---|
| **Skip this time** | Nothing. A session-only flag in the running window; setup stays exactly as it is and the wizard can come back next launch. |
| **Mark wizard complete** | The wizard state becomes FINISHED with a `marked_complete` stamp in `onboarding_state.json` — bookkeeping only. Readiness is still computed live: anything actually missing (permissions, calibration) keeps showing as a warning, and the Start checks are unchanged. |
| **Skip wizard automatically in future** | The `SKIP_WIZARD_AUTOMATICALLY` preference in your config. Launches go straight to the app; explicitly opening Welcome still shows the wizard. It does **not** mark setup complete. |
| **Cancel** | Nothing; back to where you were. |

No skip option ever weakens the Start gates — a macro that is missing a
required permission or calibration is still blocked with the same
message it always was, and the diagnostic badges keep reporting the gap.

## Undoing each choice

- **Skip this time** — nothing to undo; it ends with the session.
- **Mark wizard complete** — Trust Center → **Re-run setup wizard**
  (walks you through again; deletes nothing) or **Reset wizard progress
  only** (back to a brand-new first run; touches only
  `onboarding_state.json`). "Re-run setup wizard" is also in the
  Tutorial menu.
- **Skip automatically** — untick "Skip the setup wizard automatically
  on launch" in any of its three homes: the Welcome screen, Settings,
  or the Trust Center's Setup wizard section.

## Fortune River is not part of setup

The setup wizard covers only what the core macro needs. Fortune River
calibration is an optional, advanced feature that lives in the Calibrate
tab's **"Fortune River recovery (optional, advanced)"** section — it
never appears in the wizard and never blocks readiness. See
[CALIBRATION_GUIDE.md](CALIBRATION_GUIDE.md).

## Related docs

[TUTORIAL.md](TUTORIAL.md) — the tutorial's auto-open behavior.
[TRUST_CENTER.md](TRUST_CENTER.md) — the permanent home of every
setup/preference control. [DIAGNOSTICS.md](DIAGNOSTICS.md) — the warning
system that keeps a skipped wizard honest.
