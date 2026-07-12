# Finds tracker — lessons file

One lesson per note, with why it mattered. Update or delete notes that prove
wrong against live footage; don't duplicate what the code comments already say.

1. **Zero-shift alignment steals the newest slot.** In a bottom-anchored stack
   the newest position (~cy .90) is re-occupied by every arrival, so a
   position-only matcher happily keeps the old track there and orphans the
   pushed-up cards — this single flaw caused the original burst under-count in
   sim (5 dropped → 2 counted). Fix: score down alignments that leave
   unmatched bands on the OLD side of a matched one (cards only enter at the
   new end). `_finds_align`.

2. **Same-slot rebirth.** A solo card arriving seconds after the previous one
   died lands on the exact cy of the dead card; the dying track absorbs it and
   the find is lost (and can even adopt the wrong name). A real card's fade
   never reverses, so a track whose opacity falls to a valley and then rises
   ≥0.22×ref is TWO cards → split (`_feed_band` valley + `_split`).

3. **Corpse reads re-mint dead cards.** OCR latency (~0.3 s) means a read of a
   card can arrive after its track was finalized; the OCR safety net then
   minted it as "new" (+1 phantom). Fix: `_recent_dead` (2.5 s) filters
   look-alike reads near the death position, and the net also checks candidate
   cards against live tracks' recent READ HISTORY (an occupant swap leaves the
   old card's reads behind on the neighbour track).

4. **Opacity continuity is a trap as a scoring term.** Weighting |Δop| in the
   aligner (to catch occupant swaps) regressed the soak from 100.5% to 102.4%:
   fade ramps produce real per-tick opacity deltas, so the penalty flipped
   legitimate alignments into extra arrivals. Removed; matched-count +
   old-side-orphan penalty + smallest-shift is the stable scoring.

5. **Instant evict + same-tick arrival is unsolvable by position+opacity.**
   If the stack is full and eviction is instantaneous, every band position
   stays occupied while every occupant changes — the sim shows rare +1
   over-counts (never misses) in that case (overall 100.5% across 40
   adversarial soaks). If the real game FADES the evictee (likely), the valley
   split handles it. Watch live: if over-counts appear at sustained full-stack
   rates, this is the mechanism to revisit (identity-divergence splitting).

6. **Plateau must be learned early, not at finalize.** The ghost guard
   compares a band's peak against the learned solid-card opacity; learning it
   only when tracks finalize meant the first minutes had no reference and
   solid-but-unread cards got ghost-suppressed. Learn it when a band track
   gets its FIRST confirmed read (`_add_read`).

7. **The inferred-card double-count race.** Reconciliation mints an inferred
   card when the shift outruns seen arrivals; if the missing card then fades
   in a tick later it must ATTACH to the waiting inferred track (young,
   peak≤0) instead of minting again. Same race in reverse: `_unbound` must
   only hold reads claimed by NOTHING, or the inferred card adopts a stale
   identity of an already-counted find (fixed via fallback-returns-stray).

8. **Sim uses index-from-newest positions.** Scene cy = 0.90 − i×0.16 with
   instant eviction — deliberately the most adversarial layout guess. The real
   stack direction/anchoring is UNCONFIRMED against live footage; the tracker
   supports both via FINDS_STACK_NEWEST, and logs a hint when cards keep
   appearing at the old end.

9. **Windows engine differs only in the input layer.** A whole-region diff of
   `# FINDS TRACKER` → `_grab_screenshot_b64` shows Quartz/SendInput blocks in
   between; compare the finds slices (consts, `_finds_row_signal` →
   `_EVENT_FLAG`, stats fields) individually when checking lockstep.

10. **Harness quirk.** `exec`ing the finds slice needs `__file__` absent —
    guard scripts with `m = {"__file__": ...}`; and the driver must deliver
    pending OCR results BEFORE the fast tick (matches the real thread order).

11. **Live bug: cards last ~5 s and the same skull got scanned twice.** The
    cards are translucent and the macro's camera moves constantly; a bright
    background washes out the pixel signal mid-life, the track dies, the card
    "re-appears," and position-based logic counts it again. Fix is identity +
    timing, not position: the RE-SIGHTING MERGE (`_emit`) — same name, exact
    weight, same modifier, previous copy died ABRUPTLY (never seen fading
    out), lifetimes don't overlap, within FINDS_CARD_SEC+1.5 s — updates the
    original find instead of recounting. Genuine enchant dupes coexist on
    screen, so their lifetimes overlap and the merge is blocked.

12. **Identity-divergence fork.** When a counted track's fresh reads
    consistently show a DIFFERENT card, the majority flip used to silently
    rewrite the counted row (chimera rows, lost finds). Now the row freezes,
    goes to `_recent_dead`, and the band restarts as a new card — but ONLY
    with pixel-side corroboration of a hand-off (`disrupt_t`: valley-split,
    band re-attach) within 1.5 s. Ungated, the fork cascaded in the sim's
    teleport-swaps and wrecked the sweep (94.7%).

13. **First emits wait for the second read.** The re-sighting merge needs the
    modifier settled to match safely, so a brand-new find emits on its 2nd
    OCR read (~+0.3 s latency). Final/forced emits bypass this.

14. **Alignment span must cover a whole stack of insertions.** Candidate
    shifts were capped at 2.5×pitch, so a 3-card burst (shift −0.48) wasn't
    even considered — the aligner picked a smaller wrong shift and every
    identity cross-bound one slot over. Span is now 6×pitch. S6's earlier
    "pass" had been masking this via lucky row shuffles.

15. **Know when to stop against the sim.** Remaining sweep imperfections
    (98.7%, worst ±2/31) are sim-physics artifacts: instant position jumps
    (real HUD animates pushes → fractional per-tick shifts), 0-frame occupant
    swaps, and an rng identity collision (two `Aquamarine 550.0` drops —
    indistinguishable by ANY signal, and ~0.1% likely in the real game).
    The Scene now fades evictions over 0.25 s (the game animates them); the
    deterministic scenarios pin the real behaviours exactly.

16. **Live bug #2: back-to-back skulls overwrote each other.** A young track's
    majority flips when the next card's reads land on it (missed hand-off) and
    the UPD rewrote the counted row. Two-part fix: (a) ROW FREEZE — an emitted
    find never changes name (or kg by >25%); (b) the divergence fork decides
    whether a new card took the slot. The freeze trades the sim's
    teleport-storm behaviour from under-count/chimeras (97.8%) to mild
    over-count (102.1%) — under real animated hand-offs the fork resolves it.

17. **Cosmic (tier-coloured) text is not white.** min(R,G,B)>=WHITE_MIN goes
    blind on coloured glyphs, pushing those cards onto the fragile OCR-net
    path — "struggles more with cosmic". The bright test now also accepts
    vivid colour (max channel >=220 and mean >=135). Also: modifier votes in
    _resolve must come only from reads matching the resolved name+kg — one
    stray foreign read used to stamp a wrong tier (x14 value corruption).

18. **The sim's animation-physics GUESS dominates the sweep.** Teleport
    pushes: 98.7–102%; slide-eased pushes: 116%; fade-evictions changed
    everything again. Every remaining sweep error class traces to guessed
    HUD dynamics, not to logic pinned by S1–S13. STOP tuning against the
    sweep until the real animation is captured — ask the user for a short
    screen recording of a multi-skull burst, then encode the true fade/slide
    curves into Scene and re-baseline.

19. **Inference needs a real sampling gap.** "Stack shifted 1, saw 0 arrive"
    at a healthy 25 Hz cadence is aligner noise (a physical card cannot jump
    a whole pitch in 40 ms) — speculative inferred tracks were hoovering
    stray reads into chimera finds. Gated on tick-gap > 2.5x FINDS_FAST_MS.

20. **The jitter regression was GIL pressure, not game lag.** The engine's
    sleep_until already spins the last 2 ms — but waking from time.sleep
    still needs the GIL, and the finds threads held it in long chunks:
    np.repeat to 6x-logical frames (Retina 2x captured, then x3), .tobytes(),
    and zlib PNG-encoding ~18 MB at OCR cadence — which Vision then DECODED
    again. Sandbox model (engine's own sleep primitive + replicated
    workloads): timed-sleep overrun p95 3.7→6.1 ms, max 3.9→14.1 ms with the
    old pipeline on an IDLE box; fix restores baseline p95. Fixes: CGImage
    fed straight from the buffer (no PNG either side), upscale 2x on Retina
    (not 3x), row-signal on a half-res view, OCR duty ceiling (>=26% idle),
    macOS QoS UTILITY on all analytics threads (E-cores), plus a permanent
    _LAG probe in sleep_until reporting overruns via __STATS__/trace.
    Also: live config had FINDS_BAND_MIN=0 (every row = "card band" at 25 Hz)
    — floors are clamped in __init__ now. EARN was ruled out (committed in
    the smooth era; its wait floors at 2 s regardless of EARN_OCR_SEC=1).
    NOTE: mac/windows finds code has now DIVERGED (mac-only round; port
    later: QoS no-ops on Windows, CGImage path needs a WIC/GDI equivalent or
    keep the PNG fallback there).

21. **macOS 26 threading law for the app (cost: one SIGTRAP + one deadlock).**
    Two opposite rules coexist: (a) raw AppKit window mutations
    (setIgnoresMouseEvents/setSharingType/setOpaque/…) MUST run on the main
    thread — off-main they hard-trap ("NSWMWindowCoordinator … must only be
    used from the main thread", crash report thread shows pythread_wrapper);
    (b) pywebview ops (show/hide/move/resize/evaluate_js) MUST be called from
    a WORKER thread — they block on the main thread internally, so calling
    them FROM main deadlocks (beachball, no crash report). Pattern:
    do pywebview work on the JS-API thread, dispatch only the AppKit pass via
    PyObjCTools.AppHelper.callAfter. Boot checkpoints + PP_NO_HUD kill-switch
    stay in main() — they turned a guessing game into two exact diagnoses.

22. **Overlay v2 (transparent + auto-dock + pixel-spy) REVERTED at user
    request after three macOS-26 fights (SIGTRAP, deadlock, Spaces).** v1
    stands: opaque draggable card, worker-thread pywebview only, no AppKit.
    If v2 is attempted again: the pixel-spy + capture-exclusion design
    (NSWindowSharingNone so markers are invisible to mss window-list capture,
    hollow-centre X, click-through) was sound; the killers were platform
    window management — transparent=True traps at startup, off-main AppKit
    traps, from-main pywebview deadlocks, and fullscreen Spaces need
    CanJoinAllSpaces+FullScreenAuxiliary AND an all-Spaces window scan (which
    must NOT leak into auto-calibrate — it would calibrate against hidden
    windows). Consider a native NSPanel helper or Electron-style approach
    instead of pywebview for true game overlays.
