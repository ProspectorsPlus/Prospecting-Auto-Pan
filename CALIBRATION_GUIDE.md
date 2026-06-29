# Prospectors Plus — Calibration (share this with users)

Calibration tells the macro **where to look** on *your* screen. Do it once.
You only need to redo it if you change your screen resolution or Roblox UI scale.

## Easiest: Guided calibration (recommended)

1. Open **Prospecting** in Roblox so you can see the HUD (the capacity bar and the
   bottom prompts like *Pan* / *Shake* / *Collect Deposit*).
2. In Prospectors Plus open the **Calibrate** tab → click **✨ Guided calibration**.
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
- **"Collect Deposit" / "Pan" / "Shake"**: a pixel on each white prompt word.
- **Green dig pixel**: only needed for Perfect-dig mode.

## Check it worked

Click **Test detection (live)** on the Calibrate tab. With the game in view it
shows, in real time, whether the capacity reads **FULL** and whether each prompt
is **visible** — green means the macro sees it correctly.

## Fortune River recovery (only if you use it)

In the Calibrate tab's **Fortune River recovery** section, calibrate: the pink
**Fortune River** row text, the list **top/bottom edges**, the **screen-centre
home** (where your cursor rests with shift-lock off), and optionally the
**open** button. Same click-to-pick flow.

## Common issues

- **"It walks forward and never digs"** → the capacity bar pixel is off. Re-run
  Guided calibration's **Capacity bar** step with the bar full.
- **"It never goes back to water" / wrong prompt** → re-detect the **Pan** /
  **Collect Deposit** spots; make sure only one prompt shows when detecting.
- **Multiple monitors** → calibration uses your **primary** monitor. Put Roblox
  on the primary display before calibrating.
