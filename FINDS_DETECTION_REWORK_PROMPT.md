# PROMPT — Rework the "Finds Detection" analytics engine for Prospectors Plus (Roblox *Prospecting* auto‑pan macro)

You are an expert computer‑vision + automation engineer taking over one focused
subsystem of a mature, working desktop macro. **Read this whole document before
touching anything.** It is long on purpose: it gives you (1) the full product
context so you don't break the rest of the app, (2) the exact current
implementation of the subsystem you're reworking, (3) the precise failure and
the desired algorithm, and (4) the money/valuation model your output must keep
correct. A companion ZIP holds every reference file named here.

Your single job: **make the "finds" detector count every ore the game awards —
especially when several are awarded in one pan and they arrive very fast —
and identify each one's modifier, weight, and rarity, then value it correctly.**
Everything else in the macro already works; do not regress it.

---

## 0. TL;DR of the ask (details below)

The game shows found items as a **vertical stack of fading notification cards**
in the bottom‑right HUD. New cards animate **in** (fade from transparent →
solid) and push older cards along; old cards animate **out** (fade solid →
transparent) and leave. When the user is at a high‑luck spot, **multiple
Dinosaur Skulls drop per pan and the cards arrive faster than the current
polling catches**, so finds are missed. The current detector counts by
sequence position and misses fast bursts and multi‑drops.

**Build a much more advanced tracker** that:

1. **Samples the region constantly** (as fast as the machine allows — the OCR
   is the bottleneck; decouple a fast *presence/opacity* sampler from the
   slower *identity* OCR).
2. On each snapshot, **detects how many cards are present** and each card's
   **opacity / fade state**.
3. Uses **fade DIRECTION across consecutive snapshots** to classify each card:
   getting **more opaque (darker/solid) = animating IN = NEW**; getting **less
   opaque (lighter/transparent) = animating OUT = OLD/leaving**.
4. **Counts the new ones** from that classification (e.g. "5 cards visible, 2
   fading out ⇒ ~3 are new arrivals this window"), and **cross‑checks** that
   the number it *added* equals the number of *new* cards it *saw* — reconcile
   the opacity‑based count with the positional/sequence count and never drop a
   card silently.
5. Identifies each new card (name → snap to the 106‑ore table; modifier →
   snap to the tier list; weight kg; rarity from the ore) and emits it, then
   values it via the money model in §7.

Accuracy target: over a multi‑minute high‑luck session, the number of skulls
the analytics logs should match the number the player actually banked
(cross‑check against the in‑game backpack count) within a few percent, with no
gross under‑count during fast bursts and no double counting from animation
frames or duplicate identical drops.

---

## 1. The product you are working inside

**Prospectors Plus** is an invite‑only desktop app (its own window) that
automates the Roblox game **Prospecting**. It is NOT an exploit/injector — it is
screen‑reading + synthetic OS input. It runs on macOS (primary) and Windows,
kept byte‑for‑byte in lockstep except platform input/launcher code.

Two processes at runtime:

- **The app** (`prospecting_app.py`, pywebview): the window, the HTML/JS UI, the
  settings, the calibration overlay, the Coach (a tuning assistant), the
  **Analytics window** (a second OS window, like a pop‑out), run history, and
  the JS↔Python bridge. It launches the engine as a subprocess and pumps its
  stdout.
- **The engine** (`prospecting_old.py` — legacy name, current engine): the
  actual macro. Input layer (macOS Quartz `CGEvent` / Windows `SendInput`),
  screen detector (`mss` + `numpy`), the tick‑based supervisor state machine
  that drives dig → walk to water → shake → walk to land → repeat, recovery
  logic, relic timers, **and the finds tracker you are reworking**
  (`FindsWatcher`).

Supporting files: `prospecting_ui.py` (settings schema `SECTIONS`, `HELP`,
`PIXEL_FIELDS`), `prospecting_assistant.py` (the Coach brain + tunable
`RANGES`), `prospecting_config.json` (active settings + calibrated pixels),
`prospecting_prices.json` (ore value table — see §7), `run_history.json`
(per‑run stats), and a `windows/` folder holding the Windows copies.

**Communication protocol (engine → app):** the engine prints one line per
event on stdout; the app's `_pump` parses them:

- `__STATS__ {json}` — live run stats (pans, digs, pans/hr, earnings, etc.).
- `__EVENT__ {json}` — structured telemetry (recover, nudge, shake_fail, …).
- `__PHASE__ <name>` — phase timing markers.
- `__FIND__ {json}` — a NEW find (has an `id`). App appends to its finds list.
- `__FIND_UPD__ {json}` — an UPDATE to a previously emitted find (same `id`);
  app replaces the record in place (so a find can be re‑stated with a better
  weight/modifier once it's read more clearly, without double counting).
- `__RESET__` — a fresh run started; app clears the finds list + stats.
- `[RUNNING]/[PAUSED]/[STOPPED]/[RELIC TIMERS RESET]/…` — plain status lines.
- Control **in** (app → engine, via stdin): `PAUSE`, `RESUME`, `PAUSE_TOGGLE`,
  `RELIC_RESET`, `RELIC_SET <i> <secs>`, `RELIC_RESET_ONE <i>`.

**Hard invariants for the whole project — do not violate:**

1. The engine's core loop (dig/shake/movement) is battle‑tested. **Do not
   touch it.** You are only changing the finds subsystem (the `FindsWatcher`
   class and its helper functions) and, minimally, the app pump + Analytics
   window rendering if you add fields.
2. **Keep macOS and Windows copies in lockstep.** The finds code is identical
   on both platforms (it uses `mss` + macOS Vision OCR; the Windows OCR path
   may differ — see §6 note). Every change to `prospecting_old.py`'s finds
   section must be mirrored into `windows/prospecting_old.py`.
3. **New/changed settings** go into `prospecting_ui.py` `SECTIONS` + `HELP`
   (both copies), get a module‑global default in `prospecting_old.py` (both
   copies) so `load_config()` applies them, and (if the Coach should tune
   them) into `prospecting_assistant.py` `RANGES` (both copies).
4. **Verify before shipping:** `py_compile` all changed Python; render the
   app HTML in `prospecting_app.py` (`build_html()`, `ANALYTICS_HTML`,
   `COACH_HTML`, `PILL_HTML`) and `node --check` the extracted `<script>`
   blocks — the JS lives inside Python strings, so `py_compile` alone does NOT
   catch JS errors. Then rebuild `Prospectors Plus Windows.zip`.
5. **Prove it with a simulator, but trust only live runs.** The finds logic is
   pure enough to unit‑test by feeding synthetic OCR frames to `FindsWatcher`
   (there's a working pattern for this — see §6). Simulation catches
   regressions; only the user's live game confirms real behavior.
6. Never expose internal sandbox paths to the user; you cannot `git push`
   (hand the user the commands).

---

## 2. The game *Prospecting* — only what matters here

Core loop the macro automates ("auto‑pan"): dig on land to fill a **pan
capacity bar**; when full, walk backward into water; **shake** (rapid clicks)
to empty the pan, which is what *awards ores*; momentum carries you back to
land; dig again. Each shake can award **one or more ores**. Ore quality is
gated by luck; higher luck = more and better ores per pan.

**Ores** have: a **name** (one of 106 known ores — see `prospecting_base_values`
and `prospecting_prices.json`), an optional **modifier** (a value tier), a
**rarity** (Common → Uncommon → Rare → Epic → Legendary → Mythic → Exotic), and
a **weight in kg** (the "size"). The high‑value endgame target is the
**Dinosaur Skull** (Exotic). With the **Mythical shovel enchant** there is a
**~10% chance to duplicate** a Mythic/Exotic ore — the duplicate is a **separate
item with the SAME name, modifier, and weight**. So two genuinely distinct
finds can be byte‑identical; they must both count.

**Value‑tier modifiers** (these are exactly the words that appear on the
cards), with their price multipliers (authoritative, from the community model —
see §7): `Shiny 1.2, Pure 1.35, Glowing 1.6, Scorching 2.0, Irradiated 2.5,
Iridescent 3.5, Cosmic 6.0, Mutated 9.0, Lunar 14.0, Perfect 24.0`.

### 2.1 The finds HUD (THE thing you are reading) — study this carefully

When you collect ore, the game shows a **notification card** in the
**bottom‑right** of the screen. Observed behavior (confirmed by the user):

- Cards **stack vertically**. A newly found item's card **appears at one end**
  (the user believes the NEW one comes in **underneath / at the bottom**, and
  older cards get **pushed upward**), so the stack reads oldest→newest from top
  to bottom (a scrolling feed).
- Each card **fades**: it animates **in** (transparent → solid) when it arrives
  and animates **out** (solid → transparent) as it ages, then disappears.
- Several cards are visible **at once**. When drops come fast (multiple skulls
  per pan at high luck), you can see e.g. 5 cards simultaneously, some freshly
  fading in, some old and fading out.
- Each card shows: the **item name** (with the modifier as a leading word, e.g.
  "Iridescent Dinosaur Skull"), the **rarity word** (e.g. "Exotic"), and the
  **weight** ("857 kg"). These sit on nearby lines within the card. A green
  `+$…` money‑gain popup can flash near/over the area — ignore lines with `$`
  or a leading `+`.
- The card art/background is semi‑transparent and sits over the 3‑D world, so
  the **background color changes with terrain**. **Detection must be
  location‑based (a fixed calibrated screen box) and text/opacity‑based —
  never keyed on a background pixel color.**

The user's calibrated finds region (example) is a **tall** box:
`FIND_TL_PIXEL = [1529, 313]`, `FIND_BR_PIXEL = [1781, 982]` (≈252×669 px),
deliberately sized to hold several stacked cards at once.

---

## 3. The Analytics system (what the finds feed into)

There is an **Analytics window** (second OS window, opened from the app header;
it hides instead of closing so it can reopen). It polls the app for a snapshot
every ~1.5 s and renders:

- **Throughput:** pans, pans/hr, cycle time (mean/p50/p95/last), registered
  digs, digs/pan, clicks/pan.
- **Earnings:** money (OCR'd from the on‑screen money counter), shards,
  **loot value (kept)**, **TOTAL/hr** (= money/hr + kept‑loot/hr).
- **Finds:** count, total kg, best kg, **rarity distribution**, **modifier
  distribution**, and a **find log** (per‑item rows: time, name+modifier, kg,
  rarity, ~value).
- **Reliability:** clean‑cycle %, recoveries, nudges, shake retries/fails,
  stops/pauses/relics.

Two earning streams, kept distinct to avoid double counting:

- **Auto‑sold ores** → already added to the on‑screen **money counter**, which
  a separate OCR reads directly (money/hr). Do NOT value these again.
- **Kept/banked ores** (at/above `FINDS_BANK_RARITY`, default "Exotic" — e.g.
  Dinosaur Skulls the player banks) → NOT in the money counter yet, so the
  finds tracker **estimates** their value (loot value) and that feeds TOTAL/hr.

So the finds tracker's value only counts items **at/above the bank‑rarity
floor**; below that it still *logs* the find but contributes **$0** to loot
value (they auto‑sold and are already in money). This split must be preserved.

The engine's `SessionStats` accumulates finds fields: `finds_count`,
`find_kg`, `best_kg`, `by_rarity{}`, `by_mod{}`, `loot_value`. When a find is
re‑stated via `__FIND_UPD__`, the engine adjusts these totals by the **delta**
so they stay exact (never double add). Keep that behavior.

---

## 4. The current finds implementation (what you're replacing)

The current detector (`FindsWatcher` in `prospecting_old.py`, mirrored in
`windows/`) already moved from a naive single‑popup model to a **stack
sequence tracker**. Its full source is in the ZIP (`finds_current_code.py`
extract) and in the live files. Summary of the pipeline:

1. **`_ocr_lines(sct, reg)`** — grab the calibrated box with `mss`, upscale 3×,
   run macOS **Vision** text recognition (`VNRecognizeTextRequest`, accurate
   level, no language correction). Returns per line: `{t: text, cy: top‑down
   normalized y‑center (0..1), conf: recognition confidence (0..1), h: line
   height}`. NOTE: `conf` is available and is a natural **opacity/fade proxy**
   (a solid fresh card reads with high confidence; a faded one lower) — the new
   algorithm should exploit this and/or a direct pixel‑opacity measure.

2. **`_cards_from_lines(lines, ore_names)`** — cluster lines into **cards**
   anchored on each `NNN kg` line (exactly one per card). For each kg anchor it
   attaches the nearest name line (fuzzy‑snapped to the 106‑ore list via
   `_snap_name`, which also splits off a leading modifier) and the nearest
   rarity word, within a window ≈2.6× the median line height. Returns cards
   ordered top→bottom: `{name, mod, kg, rarity, cy, conf}`.

3. **`FindsWatcher._scan`** — the tracker. Each frame it builds `cur` cards,
   normalizes so the newest is at the list END, and **sequence‑aligns** `cur`
   to the previously tracked cards `prev`: it finds the shift `s` (how many old
   cards dropped off the front) that best matches `prev[s:]` to the front of
   `cur` using `_card_same` (fuzzy: same name, kg within 12%, modifier agrees
   or blank). Cards that dropped off the front are **finalized**; overlapping
   cards **accumulate another read**; cards at the tail beyond the overlap are
   **new finds** (must exceed `FINDS_MIN_CONF`). Emits via `_emit`, which counts
   once per card `id`, resolves best `name/mod/kg` (majority name, majority
   non‑blank modifier, **mode** weight) from the accumulated reads, values it,
   and prints `__FIND__` (first time) or `__FIND_UPD__` (refinement, delta‑
   adjusted). An empty region for `FINDS_EMPTY_CLEAR` frames finalizes the
   whole stack.

4. **`_value(name, mod, rarity, kg)`** — the money model, §7. Gated by the
   bank‑rarity floor.

Current settings (in `prospecting_config.json` / `SECTIONS`): `FINDS_TRACK`,
`FINDS_OCR_MS` (250 ms poll), `FINDS_STACK_NEWEST` ("bottom"|"top"),
`FINDS_MIN_CONF` (0.30), `FINDS_EMPTY_CLEAR` (3), `FINDS_BANK_RARITY`
("Exotic"), `SELL_BOOST_PCT`, and the two calibrated region corners
`FIND_TL_PIXEL`/`FIND_BR_PIXEL`.

**This works for slow, well‑spaced finds** but is proven to **miss finds during
fast multi‑drop bursts** (the exact user complaint). Why it fails:

- **OCR is slow (~150–350 ms per full‑stack read).** At high luck, several
  skulls can appear within one OCR cycle, so a whole card can arrive AND begin
  leaving between two reads — the sequence aligner never sees it as a distinct
  tail card, or collapses it into a neighbor.
- **Sequence alignment is brittle under fast change.** When many cards shift
  and multiple new ones appear at once, the best‑`s` search can misalign,
  under‑counting the new tail.
- **No use of fade direction.** The tracker treats a card as "present or not";
  it never uses the opacity *trend* to positively classify a card as
  arriving‑new vs leaving‑old, which is the most reliable signal the user has
  identified.

---

## 5. THE REWORK — the algorithm the user wants (design to this)

Reconceive the detector as a proper **multi‑object tracker with
opacity‑direction classification and a count reconciliation check.** Concretely:

### 5.1 Decouple sampling from identity OCR (speed)
Run **two cadences**:

- A **fast presence/opacity sampler** (target ≤ ~30–50 ms/frame): cheap —
  crop the calibrated box, and for each **card slot** measure an **opacity /
  text‑intensity** signal without full OCR. Options: per‑row white/bright‑pixel
  fraction, or edge/contrast energy in each horizontal band. This gives, every
  fast tick, a vector of "how solid is the content at each vertical band" — i.e.
  which slots hold a card and how opaque each is — **many times faster than
  OCR**. This is what lets you "check constantly."
- A **slower identity OCR** (Vision) that runs as often as it can (or on demand
  when the fast sampler says a new card is arriving) to attach name/modifier/
  kg/rarity to tracked slots. Identity can lag; **counting must not.**

### 5.2 Track cards as objects with an opacity trajectory
Maintain a set of tracked cards, each with: an `id`, a **vertical position**
(band/slot), an **opacity history** (last N fast samples), a **direction**
(rising = fading IN, falling = fading OUT, computed from the opacity slope), an
accumulated set of OCR **identity reads**, and an `emitted` flag.

Each fast tick:

- Detect the current occupied bands and their opacity.
- **Associate** bands to existing tracked cards by position proximity + opacity
  continuity (a card's opacity changes smoothly between fast ticks; it doesn't
  jump). Account for the whole stack **shifting** by one slot when a new card
  is inserted (all bands move together — detect the shift and re‑associate).
- Update each tracked card's opacity history and recompute its **direction**.

### 5.3 Classify new vs old by fade DIRECTION (the user's key idea)
- A band whose opacity is **rising from ~0 toward solid** = a card **animating
  IN** = a **NEW arrival**. The moment a rising card crosses a confidence
  threshold, mint a new tracked card (`id`) and schedule OCR to identify it.
- A band whose opacity is **falling toward 0** = a card **animating OUT** =
  **old, already counted**, about to leave. Do not count it again; finalize its
  identity (best OCR) when it drops below a floor and remove it.
- Steady/high opacity with no trend = a card sitting mid‑life (already counted).

### 5.4 Count reconciliation (the cross‑check the user asked for)
Keep two independent counters and reconcile them every window:

- **Positional/flow count:** from the stack shift — how many new slots entered
  at the newest end since the last check (newest‑end insertions).
- **Opacity‑direction count:** how many bands were classified as freshly
  fading‑IN.

If they disagree (e.g. positional says 3 new but only 2 were seen fading in
because one arrived and started leaving between reads), **reconcile toward not
losing a find**: infer the missed one, emit it with best‑effort identity
(mark it low‑confidence if OCR never caught it), and log a diagnostic. "If it
saw 3, it must have added 3." Never silently drop. Equally, never invent a find
that neither signal supports (guard against ghosts with the opacity floor and a
minimum dwell).

### 5.5 Duplicates
Because identical dupes are two separate physical cards, they are **two bands /
two arrival events** — they count separately by construction (position + arrival
timing), NOT by content. Do not merge two arrivals just because their OCR text
is identical.

### 5.6 Identity refinement
While a card lives, keep OCR‑ing it; resolve name (majority, snapped), modifier
(majority non‑blank, snapped to the tier list), weight (mode of reads — a single
blurry frame must not shift it). Emit `__FIND__` as soon as a new card is
confidently identified (responsive), and `__FIND_UPD__` (same `id`) if the
resolved values improve before it leaves. The engine adjusts stats by delta on
updates so totals stay exact.

### 5.7 Robustness details to honor
- **Fast, constant sampling** — the user gets skulls "very fast"; the fast
  sampler must run continuously and cheaply. Make its rate a setting.
- **Terrain independence** — opacity/text signals only; no background‑color
  keying.
- **Stack direction** — support newest‑at‑bottom (default) and newest‑at‑top,
  as a setting; confirm the real direction against live footage/screens.
- **Region** — keep the calibrated tall box; the taller it is, the more of the
  fading stack you can see and classify.
- Expose tunables: fast sample rate, OCR rate, opacity thresholds
  (arrive/leave), min dwell frames to count, per‑card confidence floor,
  stack direction, and keep the value/bank‑floor settings.

---

## 6. Implementation & testing notes

- **OCR backend:** macOS uses Apple **Vision** (`Vision`, `Foundation` via
  pyobjc). The confidence value is available per recognized line and is a good
  opacity proxy in addition to a direct pixel measure. On Windows the current
  code path is macOS‑specific; if you add Windows OCR, guard it and keep the
  mac/win files structurally in lockstep (the finds logic identical; only the
  OCR call platform‑specific). The user runs macOS; get mac right first.
- **Pixel opacity measure:** you already grab the region as a numpy array in
  `_ocr_lines`; a cheap per‑band brightness/whiteness fraction over the same
  crop gives the fast opacity signal without a second capture. Reuse one grab
  for both fast‑signal and (when due) OCR.
- **Simulator/unit test (do this):** `FindsWatcher` can be driven headless by
  monk‑patching its capture/OCR to return **scripted frames**. There is a
  working harness pattern: exec the finds slice of `prospecting_old.py` in an
  isolated namespace with stubbed `State`/`log`/`print`, set
  `fw._ocr_lines`/the fast sampler to pop from a scripted frame queue, feed a
  sequence of stacks (cards arriving, fading in over a few frames, shifting,
  fading out), and assert the counted finds match the injected arrivals —
  including a fast burst of 3–5 skulls within one OCR interval and identical
  dupes. Include a scenario that reproduces the current miss (multiple per pan)
  and prove the rework fixes it. Keep/extend this as a regression test.
- **Do not change the emit protocol** (`__FIND__`/`__FIND_UPD__` with `id`) or
  the app's pump/Analytics rendering contract unless you also update the app
  (both copies) and re‑verify the JS.

---

## 7. The money / valuation model (must stay correct)

Per‑item KEPT value (loot value), used by `_value`:

```
value = per_kg[name] * kg * modifier_mult[mod] * rarity_mult[rarity]
        * (1 + sell_boost_pct / 100)
```

gated so it returns 0 unless `rarity >= FINDS_BANK_RARITY` (default Exotic).

- `per_kg[name]` — from `prospecting_prices.json`, the **literal wiki base $/kg**
  (per‑item sale value). IMPORTANT: use the **base** value, NOT the model's
  aggregate‑scaled value. (A recent bug used the model‑scaled value, which
  double‑valued Dinosaur Skull: base 50,000,000/kg vs model 100,000,000/kg. The
  base is correct for "what one item sells for.")
- `modifier_mult[mod]` — the tier multipliers in §2 (Shiny 1.2 … Perfect 24.0).
- `rarity_mult` — currently all 1.0 (rarity value is already in per‑kg).
- `sell_boost_pct` — the player's panel Sell Boost as a percent; the law is
  `× (1 + SB/100)` (panel 3,364% ⇒ ×34.64; a skull‑selling build reaches
  7,110% ⇒ ×72.1). This is a user setting (`SELL_BOOST_PCT`).
- `per_item_sell_boost{}` — optional per‑item override (empty by default; only
  if a specific item sells at a different boost, e.g. a max‑sell skull build).

Sanity check from a real screen: a **Scorching (×2.0) Dinosaur Skull, 57 kg,
at 7,110% sell** = 50,000,000 × 57 × 2.0 × 72.1 ≈ **$411 B**. A **Pure (×1.35)
Dinosaur Skull, 66 kg** ≈ **$321 B**. If your numbers come out exactly double
these, you're using the model‑scaled per‑kg instead of the base — fix the data,
not the formula.

The deeper economic model (drop rates, cycle time, $/hr predictor, event
multipliers) is in `PROSPECTING_MODEL_HANDOFF.md` (community model by zexy_24 +
PPatel772). You don't need it to fix detection, but it's the authority for any
$/hr math and for how skull banking economics work. `prospecting_base_values`
(csv+json) is the source of the 106 ore names, rarities, and base $/kg.

---

## 8. Deliverables & acceptance criteria

**Deliver:**
1. Reworked finds detector in `prospecting_old.py` (and mirrored to
   `windows/prospecting_old.py`) implementing §5: fast opacity sampler +
   direction classification + count reconciliation + identity OCR + delta‑exact
   emit/update.
2. New/renamed settings wired through `prospecting_ui.py` `SECTIONS`+`HELP` and
   `prospecting_assistant.py` `RANGES` (both copies), defaults in the engine so
   `load_config()` applies them, sensible values in `prospecting_config.json`.
3. Any Analytics/app changes needed to show new diagnostics (e.g. a live "cards
   in stack / new this window" debug line, and a per‑find confidence), kept in
   lockstep and JS‑verified.
4. A headless regression test (extend the existing harness pattern) that proves:
   a fast burst of 5 skulls in one OCR interval all count; identical dupes count
   separately; animation frames never double count; a slow single find still
   counts once; nothing is invented from noise.
5. Rebuilt `Prospectors Plus Windows.zip` and the git/release steps for the
   user to run (you can't push).

**Acceptance (the user validates live):**
- During a high‑luck session where multiple skulls drop per pan, the analytics
  find count tracks the real banked‑skull count (cross‑check the in‑game
  backpack) within a few percent — no gross misses during bursts.
- Each logged skull has the correct modifier, weight, and rarity, and a value
  matching §7 (not doubled).
- Ctrl+C still exits cleanly; the rest of the macro (dig/shake/movement, relic
  timers, pause/resume, money/shards OCR, Analytics window reopen + reset)
  is unchanged.

---

## 9. How to work

Read `Prospectors_Plus_Knowledge_Base.md` first (full project brain dump:
mechanics, glitches, architecture, war stories, hard rules), then the current
finds code (in the live files and the ZIP extract). Work the problem end to
end: understand the stack behavior, design against §5, build the fast opacity
sampler, the fade‑direction classifier, the count reconciliation, the identity
binding, and the regression tests. Change **only** the finds subsystem, keep
both platform copies in lockstep, and verify before every hand‑off
(`py_compile`; render `build_html()`, `ANALYTICS_HTML`, `COACH_HTML`,
`PILL_HTML` and `node --check` the extracted `<script>` blocks; run the
headless test; rebuild the Windows zip).

Only the user's live game confirms real behavior. Each time you hand back,
tell them the two or three specific things to watch (e.g. "log 5 skulls in a
pan, then check the backpack count matches"), and what the diagnostics should
show.

## 10. Operating guidance (you are Claude Fable 5 — read this)

This task is large, long‑running, and partly ambiguous (you can't see the game;
the user can). Operate accordingly:

- **When you have enough information to act, act.** Don't re‑derive facts this
  document already establishes, re‑litigate decisions the user has made, or
  narrate options you won't pursue. If you're weighing an approach, pick one and
  say why in a sentence — don't survey. This does not apply to your private
  reasoning.
- **Build the simplest thing that meets §5 and §8; don't over‑engineer.** No
  features, abstractions, or refactors beyond what the finds rework needs. Don't
  touch the dig/shake/movement core or add speculative config. A helper is only
  worth it if it's used more than once. Match the surrounding code's style.
- **Ground every progress claim in a tool result.** Before you tell the user
  something works, point to the evidence (a passing test, an OCR read, a
  compile). If tests fail, say so with the output; if a case is unverified, say
  so plainly. Never report a fabricated or assumed status.
- **Stay in scope; report, don't act, when the user is thinking.** If the user
  asks a question or describes a problem rather than requesting a change, answer
  it and stop — don't pre‑emptively rewrite things. Before any state‑changing
  action (deleting files, rewriting large regions), confirm the evidence
  supports that specific action.
- **Verify with fresh‑context subagents.** For the acceptance checks in §8,
  spin up a separate verifier subagent against the spec rather than grading your
  own work; it catches more. Delegate independent subtasks (e.g. building the
  headless test harness while you write the classifier) and keep working while
  they run. Establish a self‑check cadence: every meaningful milestone, verify
  the finds count against the injected‑arrival ground truth in the harness.
- **Keep a lessons file.** As you learn how this game's stack actually animates
  (fade timing, insertion end, OCR quirks, what confidence values mean),
  record one lesson per note with a one‑line summary, including *why* it
  mattered. Reference it so you don't relearn the same thing. Update or delete
  notes that prove wrong; don't duplicate what the code or this doc already
  says.
- **Pause only when genuinely blocked.** Ask the user only for a destructive or
  irreversible action, a real scope change, or input only they can give (e.g.
  "record a 20‑second clip of a fast skull burst so we can confirm the stack
  direction and fade timing"). Otherwise proceed to completion; don't end a
  turn on a plan or a promise ("I'll now…") — do the work.
- **Effort:** this is a capability‑sensitive vision + concurrency problem —
  run it at high effort, and give the detection design and the reconciliation
  logic the rigor they need.

Note: you do **not** need to reproduce or narrate your internal reasoning in
your responses — a plain outcome‑first summary is what the user wants. If your
harness surfaces a remaining‑context countdown, ignore it; there is ample room
to finish.
