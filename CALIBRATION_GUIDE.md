# Prospector Lite — Calibration (share this with users)

Calibration tells the macro **where to look** on *your* screen. Do it once.
You only need to redo it if you change your screen resolution or Roblox UI scale.

## First run: the setup wizard does this for you

On first launch the setup wizard's **Guided Calibration** step walks you
through the same items below. It drives the **same detection engine and the
same save path** as the Calibrate tab — there is only one calibration store,
so anything you calibrate in the wizard shows up on the Calibrate tab and
vice versa. You can re-run it any time: Tutorial menu → "Re-run setup
wizard".

The wizard (and the Trust Center) shows an honest status for each item:

- **auto** — no manual calibration yet; the built-in auto-calibration places
  this item from the standard screen ratios at every run start. Works out of
  the box for most setups.
- **ok** — you calibrated it and it matches your current screen.
- **stale** — it was calibrated for a different window size/position than the
  current one; re-calibrate before trusting it.

(Other statuses you may see: **default**, **unset** for optional items you
haven't touched, **off** for items whose feature is disabled, and
**needs review** — the saved values look wrong when checked against each
other; the message says exactly why and how to repair it. Your values are
never changed automatically.)

Some wizard steps will show an example screenshot of what to look for
in-game. Until those images ship, the wizard shows a clearly-labelled
"example pending" note with a text description instead — it never shows a
mock-up pretending to be the real game.

## Easiest: Guided calibration (recommended)

1. Open **Prospecting** in Roblox so you can see the HUD (the capacity bar and the
   bottom prompts like *Pan* / *Shake* / *Collect Deposit*).
2. In Prospector Lite open the **Calibrate** tab → click **✨ Guided calibration**.
3. Follow the steps. For each one, do what it says in-game, then click **Detect**:
   - **Capacity bar** — dig until the bar is **completely full (all yellow)**, then Detect. It scans the screen and finds the bar's ends.
   - **"Pan" prompt** — stand in the **water** so the white *Pan* prompt shows, then Detect.
   - **"Collect Deposit" prompt** — step onto **land** so *Collect Deposit* shows, then Detect.
   - **"Shake" prompt** — start a **shake** so *Shake* shows, then Detect.
4. Each step saves automatically. When it says **All set ✓**, you're done.

If a step can't detect (e.g. another white thing is on screen), click **Pick
manually** — your screen freezes into a pickable overlay; move the magnifier over
the exact spot, click it, check the colour/coords, and press **Confirm**.

> Tip: make sure **only** the prompt it's asking for is visible at the bottom when
> you press Detect. If two prompts overlap, detection can grab the wrong one.

## Fully manual (fallback)

On the **Calibrate** tab, each spot has its own **Calibrate** button. Click it,
then on the frozen overlay click the exact pixel and **Confirm**. Spots:

- **Capacity bar — RIGHT end**: the right tip of the bar (yellow when full).
- **Capacity bar — LEFT end**: the left tip (used to measure the bar width).

The capacity endpoints are **validated at save time**. Your click is
walked up to 10 px inward onto solid gold (the bar's literal edge pixel is
a pale blend the runtime test would never match), a click that is not on
the gold bar at all is rejected with the exact colour it read, and the
resulting left/right pair must pass ordering, row-alignment, bounds, and
width checks before anything is written. A rejected save **keeps your
previous values** and shows every reason in the overlay — press Redo and
pick again. This kills the old failure mode where a bad right-end pick
saved "successfully" and then hard-stopped every run.
- **"Collect Deposit" / "Pan" / "Shake"**: a pixel on each white prompt word.
- **Green dig pixel**: only needed for Perfect-dig mode.

## Check it worked

Click **Test detection (live)** on the Calibrate tab. With the game in view it
shows, in real time, whether the capacity reads **FULL** and whether each prompt
is **visible** — green means the macro sees it correctly.

For the capacity bar specifically, use **Test capacity calibration** (on the
Calibrate tab next to the endpoint rows, and on the wizard's Capacity step).
With the bar **full** and in view, it takes one fresh screenshot and reads it
with the exact math the running macro uses: the right-tip gold test and the
fill fraction over the runtime band, plus the endpoint-pair validation. You
get PASS/FAIL, the measured tip colour, the fill percentage, an annotated
picture of what was read, per-line reasons on failure, and a one-click
**Recalibrate right end** action.

### "Needs review" on the Capacity bar

If a previously saved endpoint pair looks wrong when checked against itself
(swapped or same-end tips, tips on different rows, an implausibly small
width, or a stored width that no longer matches the tips), the app marks the
step **Needs review** and says exactly why. Your saved values are **never
modified automatically** — run Test capacity calibration or redo the
Capacity step to repair it; a one-time backup
(`prospecting_config.json.pre-caprepair.bak`) is kept the first time a
suspect pair is replaced.

## Fortune River recovery (optional, advanced — not part of the setup wizard)

Fortune River calibration lives only in the Calibrate tab's **"Fortune River
recovery (optional, advanced)"** section; the setup wizard does not include
it, and it never blocks readiness. If you use the feature, calibrate there:
the pink **Fortune River** row text, the list **top/bottom edges**, the
**screen-centre home** (where your cursor rests with shift-lock off), and
optionally the **open** button. Same click-to-pick flow. Anything you
calibrated before keeps its values.

## Common issues

- **"It walks forward and never digs" / hard stops** → the capacity bar
  calibration is off. Run **Test capacity calibration** with the bar full —
  it names the exact problem — then redo the Capacity step if it fails.
- **"It never goes back to water" / wrong prompt** → re-detect the **Pan** /
  **Collect Deposit** spots; make sure only one prompt shows when detecting.
- **Multiple monitors** → calibration uses your **primary** monitor. Put Roblox
  on the primary display before calibrating.
