# The tutorial

What the interactive tutorial does, when it opens by itself, and how to
turn that off.

## What it is

A guided tour of the app: every page, the calibration surfaces, Safe
Stop, the Trust Center, and a card explaining the warning badges — what
yellow vs red means, the Apply/Undo controls, and the FAQ. You can
start it manually any time from the Tutorial menu; the per-tab
mini-tours live there too.

## Auto-open: on every entry, by default

The tutorial opens automatically **each time you enter the main app** —
after a fresh launch, after finishing or skipping the setup wizard, and
again after you revisit the wizard and come back. Having seen, finished,
or dismissed it before does not suppress it (earlier pre-release builds
showed it only once; that behavior is gone).

It opens at most **once per entry**, and it never fires on top of the
Welcome screen, the setup wizard, or an open modal.

## Closing it

- The **X** in the tour header closes it immediately (so do "Skip tour"
  and Esc). Closing never reopens it within the same entry.
- Finishing the tour records it as completed; skipping records it as
  dismissed. Both are history only — neither affects future auto-opens.

## Turning auto-open off

The `TUTORIAL_AUTO_OPEN` preference (default **on**) is toggleable in
four places, all writing the same setting:

1. The tour footer checkbox — **"Do not open automatically in future"**.
2. Settings — "Open tutorial whenever Prospector Lite opens".
3. The Welcome screen — same label.
4. Trust Center → Setup wizard — same label.

Turning it off only stops the automatic opening; the Tutorial menu
always works.

## What is stored

`tutorial_state.json` in the data folder (schema 3): the last outcome,
how many times the tour was seen, and the last app version it was seen
in. Nothing else — no timings, no content, nothing transmitted. Older
(schema 2) files migrate in place; the file is written atomically.
