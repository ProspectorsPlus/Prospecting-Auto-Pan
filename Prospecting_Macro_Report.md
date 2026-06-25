# Prospecting Auto-Pan Macro — How It Works (Implementation Guide)

A reference for building your own auto-pan macro for the Roblox game **Prospecting**.
It covers the perception (detection), the decision logic, the movement, the error
handling/recovery, and the differences between the two build profiles (v1 / v2).
Everything here is described as *concepts + pseudocode* so you can implement it in
any language / on any OS — though the reference build is Python on macOS.

---

## 1. The game loop we're automating

One full cycle in Prospecting:

1. Stand on **land/dirt**, hold to **dig** → fills the **Pan Fill (capacity) bar**.
   Some builds fill in one dig; others need several.
2. Walk **backward (S)** into the **water/lava** until the pan can be shaken.
3. **Shake** (click/hold mouse) → empties the pan.
4. Walk **forward (W)** back onto land.
5. Repeat.

The macro automates this indefinitely while reading the screen to know where it is
and what state the pan is in.

The core design principle that makes it reliable: **separate perception from
decision from action.** A "sense" layer reads pixels into a few booleans, a
"decide" layer picks one action from those booleans, and an "act" layer performs
it and verifies the result. Re-run that loop every tick.

---

## 2. Detection (perception layer)

There are **two independent sensor layers**, and cross-checking them is what makes
it robust. Never trust a single pixel or a single layer.

### 2a. Capacity bar (the ground truth for "what's in the pan")

The horizontal Pan Fill bar at the bottom-centre is gray when empty and fills
with **yellow** as you dig. Read a horizontal strip across the whole bar and
compute the **fraction of pixels that are yellow**:

```
region = strip across the capacity bar (width = full bar, height ~20px)
yellow = (r >= 140) AND (g >= 140) AND (b <= min(r,g) - 45)   # high R/G, low B
fill_fraction = mean(yellow)        # 0.0 .. 1.0
```

From that one measurement derive three signals:

- `pan_full`  = a pixel near the **right end** of the bar reads yellow (or
  `fill_fraction` high). "Pan is full, go shake."
- `pan_empty` = `fill_fraction < ~0.04`. "Pan drained, go dig."
- `cap_fill()` = the raw `fill_fraction`. Used to detect a **dig registering**
  (see §5d) — far more reliable than watching a single pixel.

**Why capacity is the ground truth:** the on-screen text cues glitch (below), but
the bar always reflects reality. Whenever the cue and the bar disagree, believe
the bar.

### 2b. HUD prompt cues (the "where am I" layer)

Under the bar, the game shows a prompt: **"Collect Deposit"** on land, **"Pan"**
in the water, **"Shake"** mid-shake. Don't OCR it — just sample a tiny box at the
pixel where each word's text sits and **count white-ish pixels**:

```
white = (min(r,g,b) >= 160) AND (max-min spread <= 70)   # whitish text
cue_present = (fraction of white pixels in the box) >= ~0.12
```

Important detail: **count the fraction of white pixels, don't average the box.**
Averaging dilutes thin text against the dark background and the cue never trips.

Each prompt sits at a different x. To stop the wide "Collect Deposit" text from
also lighting the "Pan"/"Shake" pixels, gate them:

```
on_land  = white(deposit_pixel)
in_water = white(pan_pixel)  AND NOT on_land
shaking  = white(shake_pixel) AND NOT on_land
```

### 2c. The critical glitch: the Shake cue STICKS

After a shake finishes, the game frequently **leaves the "Shake" prompt on screen**
instead of switching back to "Collect Deposit." So you **cannot** wait for the
"Collect Deposit" cue to know you're back on land — it may never come.

The fix (see §5d): after shaking, ignore the cue entirely and use the capacity
bar + a **dig-probe** to find land. This is the single most important lesson.

### 2d. Pixel calibration

Every watched pixel (capacity bar ends, the three cue text spots, optionally the
dig skill-bar "green" pixel) is screen-resolution specific. Provide a calibrate
mode: print `PIXEL=(x,y) RGB=(r,g,b)` under the cursor live, and a monitor mode
that prints the derived booleans so you can verify on land / in water / mid-shake
before running. On Retina/HiDPI, capture is in physical pixels while OS input is
in points — handle the scale factor.

---

## 3. The decision layer (fused state machine)

Every tick, fuse the sensors into one snapshot and pick exactly one action.

```
Situation = (where, contents)
  where    = LAND | WATER | SHAKING | LIMBO     # from cues
  contents = FULL | EMPTY | PARTIAL             # from capacity bar
```

`sense_stable()` reads all sensors (optionally votes over 2–3 reads to filter
single-frame flicker) and returns the Situation.

**Decision table — capacity-primary, gated on FULL:**

```
if pan is FULL:
    if in water (Pan cue) -> SHAKE
    else                  -> GO TO WATER (walk S)
else (empty or partial):
    -> DIG-PROBE (find land by digging, then fill)   # ignores the stuck cue
```

Why gate on **FULL** specifically: right after a shake the bar momentarily reads
*partial* while it drains. If you treat "not empty" as "go shake," a transient
partial triggers a pointless second shake. Treat anything not-FULL as "done, go
dig." Only a confirmed FULL pan means "shake."

The loop is **tick-based**: re-sense and re-decide every iteration. This is what
lets it self-heal — it doesn't blindly run a fixed script, so a missed click,
an overshoot, drift, or even being dropped into a random state just produces a
different Situation next tick and the right action follows. It works from *any*
starting state (full in water, empty on land, etc.).

---

## 4. Movement

### 4a. Smooth holds for normal legs, jitter taps for recovery

- **Normal movement** (walking to water, walking to land) = **hold the key down**
  while polling the target sensor, then release. Smooth.
- **Recovery / search movement** = **pulsed taps** (e.g. ~11 ms down / 1 ms up,
  re-check after each tap). Short taps can't sail past the target between sensor
  reads, so they're precise when you're hunting for an edge.

### 4b. Walk to water

Hold **S** until the **Pan** cue appears, then stop. Optionally hold S a little
longer (`WATER_EXTRA_BACK_MS`) to get deeper so the shake doesn't start right at
the edge (shaking at the edge sometimes misses). The cue can lag your actual
position and the character keeps gliding after key release, so this is tunable.

### 4c. The shake (with momentum)

The technique: start holding **W** (forward momentum toward land) AND shake at the
same time, so you glide back onto land *while* the pan empties.

**Shaking input — clicks, not a hold (on macOS):** a synthetic *held* mouse
button is dropped by Roblox on macOS, so a hold silently does nothing. **Rapid
clicks DO register.** So the shake = momentum-W + a stream of quick clicks until
the capacity reads empty. (On Windows a real hold may work — test it; if not, use
the click stream. The loop "click until empty" works either way.)

```
hold W (momentum)
loop until pan_empty or timeout:
    quick click
    if reached land cue -> release W (stop gliding) but keep clicking
stop
```

Stop condition is **capacity == empty**, NOT the cue (cue is stuck).

### 4d. Returning to land — the DIG-PROBE (key idea)

Because the cue is unreliable after shaking, **find land by digging**: a dig only
fills the pan on dirt. So:

```
after the pan is empty:
  for a few rounds:
    re-dig IN PLACE a few times:          # don't move yet
        before = cap_fill()
        dig
        if cap_fill() rose by >~3% (or full) -> we're on LAND:
            fill the pan to FULL (dig until full) ; done
    # none of the in-place digs raised the bar -> we're probably off-land
    nudge forward (W) a bit, then try again
  if still no land after N rounds -> SAFE STOP
```

Two subtle but essential details learned the hard way:

1. **Detect a dig by the fill level RISING, not by a single "start" pixel.** On
   multi-dig builds the pan is often already partial, so the bar's start pixel is
   already filled and never "changes" — but the *overall fill fraction* still
   rises with each dig. Watching the rise works whether the pan was empty or
   partial.
2. **A missed dig ≠ wrong position.** A dig that just didn't register (timing)
   looks identical to being off-land. If you nudge forward on the first miss you
   get a jittery "nudge between every dig" on multi-dig builds. Fix: **re-dig in
   place a few times before deciding you're off-land and nudging.**

This dig-probe is what makes it land reliably without ever trusting the glitchy
cue, and it doubles as the "fill the pan" step.

---

## 5. Error handling & recovery (the "smart" part)

Robustness comes from **layered watchdogs**, each catching a different failure,
escalating from cheap corrections to a safe stop.

### 5a. Per-action verification

Each action confirms its own result on the next tick rather than assuming success.
Dug? The bar should rise. Shook? The bar should empty. Walked to water? The Pan
cue should show. If not, the next tick sees the unchanged Situation and reacts.

### 5b. Stuck watchdog (same-situation deadlock)

Track the Situation each tick. If it stays **identical for N ticks** (`STUCK_TICKS`),
something's wedged → run a **recovery** nudge for whatever variable isn't
advancing (jitter back toward water if full-not-in-water, re-shake if won't empty,
jitter forward if can't land).

### 5c. Smart break-out (the movement-lock case)

A nasty failure: a shake is *actually animating* in-game but the macro didn't
detect it started, so it tries to **walk away mid-shake — which is impossible**
(movement is locked during a shake). Every "walk" reports it couldn't move, and
it deadlocks.

The break-out: after recovery has failed several times on the same spot, assume a
shake is locking movement and **finish it by clicking** (clicking still works when
movement is locked), which drains the pan and frees you. Then reposition forward
and let normal logic resume. Only if the break-out itself fails repeatedly do you
SAFE STOP. This also fixes the "keeps landing too close to the edge and
recovering 5×" loop, because the reposition breaks the bad position.

### 5d. Capacity-based shake bail

Don't give up on a shake just because you didn't *see* the "Shake" cue (you often
won't). Only bail if the pan is **still completely FULL** after longer than a real
shake takes (~a real shake has drained to partial by then). This avoids killing a
real-but-unseen shake — the early kill was the original cause of the movement
lock above.

### 5e. Failure counters → SAFE STOP

Independent counters catch slow-burn failures the per-situation watchdog misses
(e.g. when the macro keeps *moving* between water and land but never actually
empties — the Situation changes each tick so the deadlock watchdog never trips):

- `shake_fails` — shakes that didn't empty; over a cap → SAFE STOP.
- `land_fails`  — dig-probe couldn't find land; over a cap → SAFE STOP.
- `breakouts`   — break-outs attempted; over a cap → SAFE STOP.

All counters reset on healthy progress (a successful empty or a dig-probe hit).

### 5f. SAFE STOP

The final fail-safe: release **all** keys/mouse, stop the loop, and alert (sound /
notification). Better to stop cleanly than flail (and on a lava map, than die).

---

## 6. v1 vs v2 (build profiles)

Same brain, different dig settings:

| Aspect            | **v1** (fast build)            | **v2** (general)                         |
|-------------------|--------------------------------|------------------------------------------|
| Pan fills in      | 1 dig                          | any number of digs (detected dynamically)|
| Dig input         | one quick click (~15 ms)       | timed hold (~75 ms) per dig              |
| Fill logic        | one dig assumed                | **dig until the bar reads FULL**         |
| "Perfect" timing  | not needed                     | hold tuned to land on the green zone     |

v2's "dig until full" is just the dig-probe's fill step looping until
`pan_full`, counting nothing — it watches the bar. v1 is the special case where
that loop exits after one dig. So v2's logic is a superset; v1 ≈ v2 with
`dig_length=15ms, max_digs=1`.

Note on "perfect" digs: Prospecting's dig is a timed skill bar (a line sweeps;
releasing on the green zone = a better dig). We tried releasing exactly when a
"green" pixel lit, but the line moves too fast to catch reliably by pixel-polling,
so a **fixed-length hold tuned to land on green** for the build's dig speed worked
better. If your detection can keep up, pixel-timed release is the ideal.

---

## 7. Timing & performance pointers

- Use a **high-resolution clock** (monotonic perf counter) and a **compensated
  sleep** (sleep most of the interval, then busy-wait the last ~1–2 ms) for
  accurate short delays.
- Poll sensors as fast as needed in `wait_until`-style loops; capture of a small
  region (a few px box, or one bar strip) is cheap.
- A "hold key" = press event with no release; "release" later. "Tap" = press +
  short sleep + release.
- Disable GC during the run if your language has a stop-the-world collector, to
  avoid timing hiccups.

---

## 8. Implementation checklist (to recreate it)

1. **Screen capture** of small regions → RGB arrays. (`mss` + `numpy` in Python;
   cross-platform.)
2. **Input backend** for key down/up and mouse down/up/click. (macOS: Quartz
   CGEvent. Windows: `SendInput` via ctypes — Roblox may need DirectInput
   scancodes. Linux: xdotool/uinput.)
3. **Global start/stop + quit hotkeys** that don't type into the game. (Poll key
   *state* rather than installing a listener if your input library conflicts with
   synthetic events — this avoided a thread-safety crash on macOS.)
4. **Calibrate mode** (print pixel+RGB under cursor) and **monitor mode** (print
   the derived booleans + the action it *would* take) — build these first; you'll
   live in them while tuning.
5. **Detection functions**: `cap_fill / pan_full / pan_empty`, and
   `on_land / in_water / shaking` (white-pixel-count cues).
6. **The tick loop**: `sense → decide (table in §3) → act → repeat`, with a live
   timestamped trace log so you can see its reasoning.
7. **Movement primitives**: hold-until-cue (smooth), pulse-until-cue (jitter),
   the momentum+click shake, and the dig-probe landing.
8. **Watchdogs & counters** from §5, ending in a SAFE STOP that releases inputs.
9. **A settings file** for all the tunable timings, so you tune without editing
   code (we put a small GUI on top of a JSON config).

---

## 9. Pitfalls we hit (so you can skip them)

- **Held mouse for the shake silently does nothing on macOS** → use rapid clicks.
- **Waiting for the "Collect Deposit" cue after a shake hangs** (cue sticks) →
  find land by dig-probe instead.
- **Detecting a dig via a single "start" pixel fails on partial pans** → detect by
  the whole-bar fill *rising*.
- **Nudging forward on the first missed dig** causes jitter → re-dig in place
  first; nudge only after several misses.
- **Bailing a shake because you didn't see its cue** killed real shakes and caused
  a mid-shake movement lock → bail only when the bar stays *fully* full past a
  real shake's duration; recover a lock by clicking it out, not walking.
- **A brake tap in the wrong direction** (tapping back toward water after reaching
  land) caused a land↔water oscillation → don't brake toward the hazard; settle
  forward onto safe ground instead.
- **Averaging a text box** to detect a cue → count white pixels instead.
- **Treating "not empty" as "shake"** double-shakes on the drain transition →
  gate on FULL.

---

### TL;DR

Read the **capacity bar** (yellow fraction) as ground truth and the **HUD cues**
(white-pixel count) for position; fuse them into a (where, contents) Situation;
pick one action from a FULL-gated table every tick; do normal moves as smooth
holds and recovery as jitter taps; shake with momentum-W + rapid clicks until the
bar empties; and — because the Shake cue glitches — **find land after shaking by
digging and watching the bar rise**, not by trusting the cue. Wrap it all in
layered watchdogs that escalate nudges → break-out → SAFE STOP.
