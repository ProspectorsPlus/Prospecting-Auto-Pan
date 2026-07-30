# Diagnostics: badges, the warning drawer, and the FAQ

How Prospector Lite tells you something is wrong, how much it actually
knows, and how to act on it. Everything here is computed and stored
locally — nothing is transmitted (see [PRIVACY.md](PRIVACY.md)).

## The badges: what the colors mean

Small count badges appear on the sidebar tabs (Calibrate, Cycle, and
Trust — whichever tab owns the fix):

- **Red** — at least one ERROR or CRITICAL issue: something is broken or
  about to break runs (a suspect capacity calibration, hard stops, a
  missing required permission, a broken Safe Stop listener).
- **Yellow** — at least one WARNING or NOTICE: something looks off and
  is worth reviewing (shake timing drift, recovery loops, stale
  analytics regions).

The number is the count of issues for that tab (a lone issue shows
"!"), and hovering shows the top issue's title. Red always wins over
yellow on the same tab. Informational events never badge.

**Clicking a badge opens the warning drawer** at that tab's top issue —
it does not switch tabs. The calibration and cycle banners are clickable
the same way.

## The warning drawer

One issue at a time (a list appears at the top when there is more than
one), each rendered as plain sections:

- **What happened** — the issue in one line, then a short summary.
- **What Prospector Lite observed** — the actual measurement plus a
  bullet list of numeric evidence, thresholds included (e.g. "nudges: 9
  in 12 cycles (0.75/cycle, threshold 0.6)").
- **Most likely cause** — with honest confidence wording: **High
  confidence**, **Medium confidence**, or **Possible cause**. The
  confidence follows a fixed do-not-overstate policy (right at a
  threshold is only "possible"; double it or categorical evidence — a
  permission simply is not granted — is "high"). When it is only
  possible, the title says "may".
- **Recommended first action** — one thing to try first.
- **Exact settings to review** — each named setting with its current →
  suggested value and units, the reason, and three buttons: **Open
  setting** (deep-links to the precise control — on the Cycle page it
  scrolls to and highlights the exact row), **Apply suggested value**
  (only for registry-bounded settings; the value is clamped to the
  setting's real bounds, one setting per click), and **Undo** (restores
  the exact previous value from a stored snapshot).
- **Exact calibrations / Permissions** — deep links to the exact
  calibration row (a capacity issue also reveals the Test capacity
  button) or the exact Trust Center capability card (highlighting its
  Test button).
- **Other possible causes**, **Expected effect**, **Tradeoff**, and
  **How to verify** — so you know what a fix should change, what it
  costs, and how to confirm it worked.
- **Related FAQ** — one click to the matching FAQ entry.
- **Copy diagnostic details** — copies the full event as JSON for a bug
  report (it contains measurements and setting names, no secrets).
- **Dismiss** — hides the issue for this session; it comes back if it
  recurs.
- **Don't show again for this code** — a persistent, per-issue-type
  suppression, reversible any time via Trust Center → "Show suppressed
  warnings again". **Never offered for CRITICAL issues** (a missing
  required permission cannot be silenced).

## Recurrence and escalation

Repeated issues are merged, not duplicated: the drawer shows "seen ×N",
and a persistently recurring issue can escalate (e.g. a missed-prompt
notice becomes a warning on the third occurrence and an error on the
sixth). Issues that stop being detected disappear on their own.

## The FAQ browser

**Help/Tutorial menu → "FAQ & troubleshooting"** — also reachable from
the drawer's Related-FAQ button, Settings, the Calibrate tab, the
wizard's Readiness Check, and the Trust Center. Twenty local, searchable
entries (search covers questions and symptoms); every entry ends with
buttons that open the exact setting, calibration, or permission it talks
about. The entry list is in [FAQ.md](FAQ.md).

## What this system is not

It is not telemetry (nothing leaves your machine), not an auto-tuner
(nothing changes unless you click Apply, and Undo always exists), and
not a black box — [RECOMMENDATIONS.md](RECOMMENDATIONS.md) documents
every rule and threshold, and the full engineering reference lives in
`docs/final-prepublish/RECOMMENDATION_RULES.md`. What persists on disk
is exactly: your suppressions, apply/undo snapshots, and a bounded
action history (`diagnostics_state.json`).
