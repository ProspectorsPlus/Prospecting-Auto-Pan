# Decision log

Local implementation decisions taken while building against
`TREASURE_NAVIGATION_PLAN.md`, in the order they were made. Each entry says
what was decided, why, and whether it deviates from the plan.

Format: `D-nnn — date — title`.

---

## D-001 — 2026-08-27 — `raw_pointer_delta` carries the held-button context

**Plan text:** §4.2 sketches `raw_pointer_delta(self, dx: int, dy: int) -> None`.

**Decision:** the signature is
`raw_pointer_delta(dx, dy, held_button: MouseButton | None = None)`.

**Why:** §4.4 and bug B8 put the held-state ledger exclusively in
`InputAuthority`, so the port cannot know whether a button is down. But §12's
native yaw note requires the macOS primitive to *hold RMB and emit Quartz delta
drag fields* — a `kCGEventMouseMoved` with delta fields does not turn the
camera while a button is held; a `kCGEventRightMouseDragged` does. The
authority is the only component that knows both facts, so it supplies the
context at call time. Windows ignores the parameter (`MOUSEEVENTF_MOVE`
already delivers a relative delta) and accepts it only for symmetry.

**Deviation:** yes, additive parameter with a default. Reversible.

---

## D-002 — 2026-08-27 — `IntentType.PIXEL_INFO` added

**Plan text:** §3.2 lists eight intent types; F3 pixel info is not among them.

**Decision:** added a ninth, `PIXEL_INFO`, bound to F3 on both platforms.

**Why:** F3 is the existing, working calibration read-out, and §4.1 requires
the legacy pixels to be *re-derived* against the canonical client basis — which
is exactly what F3 is for. Routing it through the coordinator like everything
else is better than giving the hotkey listener a private side channel. It
reads one already-captured frame and shows a value; it is never given an input
session, does not change `RunMode`, and does not consume or disturb an arm
token.

**Deviation:** yes, additive. Reversible.

---

## D-003 — 2026-08-27 — `cursor_client_px()` added to `PlatformPort`

**Decision:** the port exposes a read-only cursor position in client-relative
physical pixels.

**Why:** both `--calibrate` and the F3 probe need it, and doing the
screen→client conversion inside the port is what keeps desktop coordinates out
of feature code (§4.3). It returns `None` rather than a coordinate when the
client rect is unverified or the cursor is outside it, so a wrong number can
never be pasted into a config file.

**Deviation:** additive, read-only, emits nothing.

---

## D-004 — 2026-08-27 — Python floor is 3.13, not a wider claimed range

**Decision:** `requires-python = ">=3.13"`.

**Why:** §17 forbids declaring a support range before verifying it. CPython
3.13.15 (macOS arm64, Tk 9.0) is the only interpreter this suite has been
executed on, and the numpy 2.5 stubs the project type-checks against already
require 3.12+. Widening to 3.11/3.12 is PENDING an actual run there.

---

## D-005 — 2026-08-27 — macOS title-bar inset is measured, not assumed

**Decision:** `MacPlatformPort` derives the client rect from the AX window
frame minus a title-bar height computed as
`2 * (close_button_y - frame_y) + close_button_height`, falling back to a
documented 28.0 pt constant only when Accessibility cannot see the traffic
lights. The port reports which of the two was used.

**Why:** §4.1 requires a *verified* client rect, and macOS only offers the
outer frame. Reading the window's own button geometry turns the inset into a
measurement instead of a guess. Observed on the development Mac on 2026-08-27
against the running Roblox client: frame origin y = 39.0 pt, close button at
y = 46.0 pt with height 14.0 pt, giving exactly 28.0 pt, and a resulting client
rect of `origin=(0, 134) px, size=(3600, 2108) px, scale=2.0`. That is one
machine and one window state; E-VIEW on macOS is still PENDING.

---

## D-006 — 2026-08-27 — Legacy pixels are carried over unchanged and marked PENDING

**Decision:** `TreasurePixels` ships the legacy dig/pan/reset coordinates
byte-for-byte with `EvidenceStatus.PENDING`, plus a
`from_legacy_window_frame(title_bar_px)` transform that is *offered but not
applied by default*.

**Why:** §4.1 says the legacy pixels must be transformed to client coordinates
and manually reverified, and that the old numbers must not be silently reused.
Two things changed at once: the origin moved (frame → client) *and* the pinned
area got bigger (a 1280×720 frame contained a 1280×664 client; the canonical
pin makes the client itself 1280×720). A mechanical y-shift fixes the first but
not the second, so applying it automatically would replace one wrong number
with a differently wrong number that looks migrated. Instead the values stay as
the ones that demonstrably worked, the status is loudly PENDING in the UI, the
self-test, and the profile card, and `--calibrate` reports in the canonical
basis so re-deriving them is a five-minute job for the owner.

---

## D-007 — 2026-08-27 — Characterization baseline is a recorded transcript

**Decision:** `tests/fixtures/legacy_sequences.json` holds the ordered input
transcript of the pre-navigator dig, dequip, pan-swap, and reset sequences,
captured from commit `5b81120` with every OS edge and every sleep intercepted.
`tests/test_characterization.py` asserts the new bounded services reproduce it.

**Why:** §15 Phase 0A wants correct current behavior preserved without
preserving B1–B13. A recorded transcript is the only form of that evidence that
survives deleting the legacy module. Screen sampling is excluded from the
comparison because the new services read one coherent frame instead of grabbing
per pixel — that difference *is* the B12 fix.

---

## D-008 — 2026-08-27 — Shadow gets a real authority generation with input disabled

**Decision:** Shadow activates an authority generation with
`emits_input=False`, so admission never opens, rather than not activating one
at all.

**Why:** it keeps one code path for generation bookkeeping and evidence
registration while making a press structurally impossible: `NoInputSession`
holds no reference to the authority, and even if it did, admission is closed.
Two independent barriers rather than one.

---

## D-009 — 2026-08-27 — Windows port is written but not executed

**Decision:** `platform_win.py` is complete, annotated, and type-checked, but
carries a module-level notice that nothing in it has run on Windows.

**Why:** §18.8 forbids claiming Windows behavior from a Mac. mypy on macOS
cannot resolve `ctypes.windll`, so `pyproject.toml` exempts exactly those two
Win32-only names for that module and says why. The opposite-OS import test
covers structure only; every Windows row in §16.3 stays `pending`.

---

## D-010 — 2026-08-27 — Capture cost measured; the canonical pin is what fits

**Decision:** record capture cost as an observed local fact and add
`treasure.py --capture-probe` so the measurement is reproducible.

**Measured on the development Mac (macOS 25.4, arm64, 2x display), read-only,
40 samples per size, mss backend:**

| Client size (physical px) | p50 | p95 | 40 ms budget |
|---|---|---|---|
| 1280 × 720 (canonical) | ~19–23 ms | ~21–33 ms | within |
| 2560 × 1440 | ~33–37 ms | ~36–47 ms | over |
| 3600 × 2108 (this machine's unpinned Roblox window) | ~70–81 ms | ~82–158 ms | over |

**Why it matters:** running Shadow against the *unpinned* window on this
machine produced decision-time frame ages of 110–160 ms, and the navigator
correctly released on `stale-frame` every tick. That is the safety machinery
working, and it is also a concrete reason the canonical pin is not cosmetic:
capture cost scales with pixel count, and only the canonical size leaves room
for perception inside the frozen budget.

**Status:** this is capture-only, on one machine, in one run. It is an
`observed_fact` for planning. **E-PERF is still PENDING** — it also covers
perception, control, preview, duplicate/stale rate, and Stop latency.

---

## D-011 — 2026-08-27 — Shadow adopts the viewport instead of moving the window

**Decision:** `START_SHADOW` calls `ViewportGuard.adopt_current()` when nothing
is pinned, rather than refusing or pinning.

**Why:** Shadow only observes, and §3.3 asks it for "a valid capture viewport",
not a canonical one. Making pure observation require moving the user's Roblox
window would be a needless side effect. Adoption is read-only, and if the
adopted rect is not the canonical size the detectors abstain with
`unsupported-viewport-size` — verified in a real Shadow run on this machine.
Nothing is silently rescaled.

---

## D-012 — 2026-08-27 — `native` tests are excluded from every default run

**Decision:** `addopts = "-m 'not native'"` in `pyproject.toml`.

**Why:** §16.1 says no test may emit OS input unless it is explicitly marked
native and physically armed. Making that the default configuration is stronger
than documenting it, because an agent or a CI runner that types plain `pytest`
still cannot emit input.

---

## D-013 — 2026-08-27 — `IntentType.RECOVER_RELEASE` added

**Plan text:** §4.4 requires "an explicit release-only recovery handshake"
before Live may be offered again after an uncertain release, but does not say
how it is triggered.

**Decision:** a new intent, `RECOVER_RELEASE`, with a dashboard button that
appears *only* while the latch is set.

**Why:** "explicit" rules out clearing the latch as a side effect of a
successful Stop. Making it an intent keeps it on the same priority queue and
the same coordinator lock as everything else, rather than giving the UI a
private path into the authority. The handshake calls
`InputAuthority.recover_release()`, which emits up-edges only — there is no
press anywhere in that path — and the latch clears only if the resulting
`ReleaseReport` says release is known safe.

**Also added:** the latch now survives the process.
`AppPaths.recovery/unsafe_release.json` is written whenever a stop or shutdown
cannot confirm a safe release, adopted at the next `RuntimeCoordinator.start()`,
and removed only by a successful handshake. An unreadable record is treated as a
record — fail closed. Plan §4.4 asks for exactly this ("a prominent
unsafe-release recovery record for the next launch"); without it, a crash while
a key was held would read as a clean start.

---

## D-014 — 2026-08-27 — Orphan recorder chunks are quarantined, not deleted

**Decision:** `EvidenceRecorder.start()` moves any chunk the previous manifest
never described into `quarantine/`.

**Why:** §11.1 says unfinished chunks are "recovered or quarantined on next
start", and that protected evidence is never deleted silently. A chunk left by
a run that ended without a bounded flush is evidence of *something* — most
likely of the crash worth investigating — so it is set aside rather than
deleted or replayed as if the manifest vouched for it.

---

## D-015 — 2026-08-27 — `SERVICE:DIG` keeps the existing dig loop reachable

**Plan text:** §6.1 reaches `DIG` only from `LIVE:NAVIGATE → ARRIVED`, and gives
standalone hotkeys only to reset (F4) and pan swap (F5).

**Decision:** added `IntentType.DIG_LOOP` on **F6**, a bounded `SERVICE` mode
that runs the same `run_dig_at_current_spot` / `run_pan_swap` services the
lifecycle will use.

**Why:** digging at an already-found spot, with automatic pan swap when the
capacity bar reads full, is the capability the pre-navigator build actually
had and the owner actually uses. Following §6.1 literally would leave that
capability unreachable until E-ARRIVE, E-PROF, E-DIR-E2E, E-YAW and
E-STEER-CAL all pass — that is, indefinitely — because the only door to `DIG`
is behind Live. Phase 0E converts these services rather than retiring them, so
leaving them uncallable would make the conversion incomplete in substance while
looking complete on paper.

**What it is not:** it is not navigation and it does not weaken anything. It
runs under the same coordinator, the same generation, the same
`ServiceInputSession`, the same watchdog and deadman, and the same focused-
hotkey requirement as F4/F5. It has a total attempt cap and a monotonic
deadline, F2 cancels it within one wait slice, and it stops on any outcome that
is not `DIG_PROGRESS` or a successful pan swap.

**Important caveat, surfaced in the UI:** the dig pixels are `PENDING`
reverification (D-006). With the canonical client pin they will simply not
match, so the service reports `CUE_LOST` and does nothing. That is the correct
failure — re-derive them with `--calibrate` first.

---

## D-016 — 2026-08-27 — `input_authority.py` stays one module at ~1200 lines

**Plan text:** §13.1 says to split a module "only if it becomes genuinely
difficult to review (for example, substantially beyond roughly 800 cohesive
lines…), and document the reason."

**Decision:** keep it as one module, and document *that* instead.

**Why:** §13.1 also assigns this module its contents explicitly — "capability
sessions, ledger, watchdog, deadman client, global release" — so splitting it
would contradict the same section. More importantly, those pieces are not
independent: the lock ordering (`_edge_barrier` → `_lock`), the epoch, the
admission gate, and the ACK-before-down sequence are one invariant that the
ledger, the sessions, the watchdog, and the release floor all participate in.
Putting a module boundary through the middle of it would reintroduce exactly
the multi-owner ambiguity that bugs B7 and B8 were about, and would make the
boundary look like an interface when it is really one atomic protocol.

The line count is also inflated by the invariant documentation that makes it
reviewable at all: roughly a third of the file is docstrings and comments that
explain the ordering rules and cite the bug IDs.

If it does grow further, the honest seam is `DeadmanClient` — the parent side
of an out-of-process protocol that shares no state with the authority — not the
sessions or the ledger.

---

## D-017 — 2026-08-28 — Logical units and device pixels are now different types

**The defect.** ``find_client_rect`` returned a rectangle named ``*_px`` holding
*device pixels*, and that rectangle was handed to ``mss``, which on macOS speaks
the display's **logical** space. Measured on the development Mac: the client was
reported as ``origin (0, 134), size 3600x2108`` in device pixels, and ``mss`` was
asked for a 3600x2108 region starting at logical (0, 134) on an 1800x1169-point
display. The capture therefore ran off the screen and returned Roblox plus the
desktop, the Dock, and whatever else was there - exactly the reported symptom.

The same confusion broke pinning. A 1280x720 **device-pixel** client request was
divided by the 2x display scale into a 640x360-point window, below Roblox's
minimum, so the OS clamped it and the size read-back never matched.

**Decision.** Introduce :mod:`prospector_engine.geometry` with four named,
non-interchangeable spaces - ``DISPLAY_LOGICAL``, ``CLIENT_LOGICAL``,
``CLIENT_BACKING``, ``CANONICAL`` - and an ``Affine2D`` that carries its source
and target space and refuses to compose mismatched ones. ``ViewportGeometry``
holds the window identity, the display, the frame rect, the client rect, the
backing scale, and every forward and inverse transform in one immutable value.

Consequences:

* Pin requests are in **logical** units on both platforms
  (``pin_client_rect(size_logical)``), so the canonical request is 1280x720
  points and no division by the display scale happens anywhere.
* The canonical processing raster stays 1280x720 regardless of display scale, so
  a calibrated pixel means the same thing on every machine.
* ``backing_scale`` (device pixels per logical unit: 2.0 on Retina) and
  ``dpi_scale`` (the user's UI scaling: 1.25 at 125%) are separate fields.
  Only the first appears in a transform; on Windows a Per-Monitor-V2 process
  already receives device pixels, so its ``backing_scale`` is 1.0.
* ``tests/test_geometry.py`` proves round-trips, mismatched-space refusal,
  negative monitor origins, window replacement at the same rectangle, display
  migration, and that a fractional Windows DPI never multiplies a coordinate.

---

## D-018 — 2026-08-28 — ScreenCaptureKit is the macOS backend; Quartz is the fallback

**Measured on the development Mac** (2x display, Roblox client cropped to a
1280x720 canonical raster, read-only, no window moved):

| Backend | Unique fps | Per-frame cost | Window-specific? |
|---|---|---|---|
| ScreenCaptureKit (async push) | **106-110** at a 120 Hz request; 58 at 60 Hz | ~5 ms capture, GPU crop and scale | yes |
| ``CGWindowListCreateImage`` (sync pull) | ~75 ceiling | ~13 ms including the copy | yes |
| ``mss`` (desktop rectangle) | ~58 ceiling | ~17 ms | **no** |

**Decision.** ScreenCaptureKit is preferred, Quartz window images are the
dependency-light fallback, and ``mss`` is a last resort that captures a desktop
rectangle and therefore picks up anything overlapping Roblox.

ScreenCaptureKit earns two new macOS-scoped dependencies
(``pyobjc-framework-ScreenCaptureKit`` and ``pyobjc-framework-CoreMedia``, both
already pinned to the same pyobjc version) because it is the only option that is
window-specific, asynchronous, keeps delivering while the dashboard is
frontmost, crops and scales on the GPU through ``sourceRect``/``destinationRect``
so no per-frame CPU resize is needed, and reports its own frame status so
uniqueness is authoritative rather than guessed.

Three pyobjc details that cost real debugging time and are now encoded with
comments so they are not rediscovered:

1. ScreenCaptureKit keeps only a **weak** reference to a stream output. Without
   retaining the delegate, frames silently stop arriving with no error.
2. Objective-C class names are process-global, so defining the delegate class
   per stream raises *"overriding existing Objective-C class"* on the second
   capture session - which is every tier change and every reacquisition. The
   class is defined once and cached; the callback lives on the instance.
3. Completion handlers are typed ``void``; returning a value from one raises
   inside the callback and terminates the process.

**Windows** gets ``WindowsPrintWindowSource`` (``PrintWindow`` with
``PW_RENDERFULLCONTENT``), which is window-specific, needs no new dependency,
and reuses its GDI objects. Windows Graphics Capture would be faster and is the
intended production backend, but it needs a Windows machine to verify and
another dependency, so it is deliberately not guessed at. **PENDING native
verification** - no code in ``platform_win.py`` has ever run on Windows.

---

## D-019 — 2026-08-28 — Event-driven latest-frame pipeline with a cadence governor

**Decision.** Replace the polled path (100 ms GUI, 50 ms capture, 50 ms worker -
about 10-20 Hz) with a push pipeline: the backend delivers, the frame is
normalized once into a pooled canonical buffer, and it is published to a
capacity-one drop-oldest slot whose ``wait_for_new`` wakes consumers
immediately. Nothing sleeps on a timer to notice a frame arrived, and no
backlog can form.

Details worth stating:

* **Uniqueness is source-authoritative.** ScreenCaptureKit's
  ``SCFrameStatusIdle`` marks a redelivered surface; those are skipped before
  the copy. Backends without such a signal fall back to a decimated digest.
  A redelivered surface must never inflate the number the governor and the UI
  read as evidence of health.
* **Memory is flat with respect to frame rate.** A ``FrameBufferPool`` of eight
  canonical buffers replaces ~290 MB/s of allocation at 110 Hz. A buffer returns
  through ``weakref.finalize`` when its frame is released, so a buffer is never
  recycled while a consumer still holds it. Measured RSS stayed at 97-108 MB
  across 60, 90, and 120 Hz.
* **A drop means a frame replaced before any consumer saw it**, not merely an
  occupied slot - otherwise the metric reads as catastrophic while the pipeline
  is perfectly healthy.
* **The governor** walks 15/30/60/90/120, downshifts immediately on sustained
  shortfall or over-age frames, and upshifts only after four healthy seconds
  *and* while already saturating the current tier, so it cannot chase a rate the
  source cannot produce. Below 30 unique fps it reports a degraded state rather
  than a healthy-looking number.

Measured end to end through the real service, consuming every frame:
58.0 / 84.8 / 106.2 unique fps at 60 / 90 / 120 Hz requests, zero duplicates,
zero drops, capture p95 6.4-7.8 ms, frame age 5-7 ms, CPU 39-49% of one core.
Reproducible with ``treasure.py --capture-probe``. **E-PERF remains PENDING**:
this is capture-and-consume only, on one machine.

---

## D-020 — 2026-08-28 — Bounded reacquisition instead of a stall that never ends

**Decision.** One supervisor thread runs the cadence governor *and* watches for
a source that needs rebuilding: a `CAPTURE_MISMATCH` from the guard, an
`INVALID` viewport, a backend reporting ill health, or no frames inside the
stall budget. It rebuilds the source through `restart_source`, behind an
exponential backoff capped at four seconds.

**Why the cap and the backoff matter.** Roblox closing, minimizing, or moving to
another Space is not a transient; without a cap the supervisor would retry every
poll forever. With it, a window that is gone for good costs one retry every few
seconds, the retry structure never grows, and the reacquisition count is a
visible metric rather than a hidden loop. A stop in flight short-circuits the
check, so a retry can never undo a shutdown.

---

## D-021 — 2026-08-28 — The reference arm is drawn, and labelled as a hypothesis

**The tension.** The mission asks Shadow to draw the player-forward arm, the
desired direction, and the angle between them. But E-ANCHOR and E-FORWARD have
not been run, so there is no validated anchor and no validated forward.

**Decision.** `ReferenceFrame` supplies both as **provisional configuration**
carrying `EvidenceStatus.PENDING`: the anchor at canonical (640, 430) and
forward as screen-up, which is precisely the hypothesis plan §7.4 sets out to
test after a deterministic camera reset. The arm is drawn dashed, labelled
"forward (assumed)", and every observation records
`forward_source = "assumed: screen-up after camera reset (E-FORWARD PENDING)"`.

**Why this is not a claim.** Drawing it is the only way a human can judge it,
which is what Shadow is *for*. Nothing downstream is affected: `steering_enabled`
still requires E-ANCHOR and E-FORWARD to be `VALIDATED`, the live worker still
refuses and names them, and the pending status is repeated in the caption, the
decision panel, and the legend. A picture a human can evaluate is the input to
the experiment, not a substitute for it.

Every candidate direction cue is also evaluated and drawn, not just the selected
one, so a fusion abstention shows *how much* the cues disagreed rather than
leaving a blank screen - which is the diagnostic E-DIR-IDEAL actually needs.

---

## D-022 — 2026-08-28 — The overlay renderer is its own module

**Decision.** `DiagnosticCanvas` moved from `treasure_gui.py` into
`treasure_overlay.py`, taking the palette with it. The dashboard imports both.

**Why.** `treasure_gui.py` had reached 1347 lines holding three separable
things: application wiring, a canvas renderer, and a dashboard. Plan §13.1 sets
roughly 800 cohesive lines as the point where a module stops being reviewable
and asks for the reason to be documented. The renderer is the cleanest seam in
the file - it takes one `DiagnosticObservation` and a Tk canvas and touches
nothing else - and splitting it leaves 834 and 559 lines. It stays at the
repository root rather than inside `prospector_engine` because the engine
package must remain importable without Tk.

**Also consolidated:** the letterbox-into-canonical helper existed in three
copies (the mss fallback, the macOS Quartz source, and the Windows PrintWindow
source). There is now one `normalize_into_canonical` in `capture.py` that all
three import - three copies of a coordinate transform is exactly the kind of
thing that drifts apart silently.

`platform_mac.py` (1212) and `capture.py` (1154) stay as they are, for the same
reason recorded in D-016: each is one ownership boundary the plan assigns
explicitly, and a module split through the middle of a coordinate contract or a
capture lifecycle would look like an interface while being an atomic protocol.

---

## D-023 — 2026-08-28 — Connecting and resizing are separate operations

**The tension.** One button did both: *Pin Window* found the Roblox client and
resized it. When the resize was clamped — which it usually was, because Roblox
enforces a minimum window size — the result was a confusing half-state, and
capture appeared to depend on a resize succeeding.

**Decision.** Two operations with two names.

`ViewportGuard.connect()` binds to the client exactly as it is. It moves
nothing, resizes nothing, and sends nothing. It is the recommended path, and it
is what Start Shadow does implicitly.

`ViewportGuard.fit_and_lock()` is the optional one, and it is a bounded state
machine rather than a call:

```text
requested -> settling -> canonical_verified | achieved_clamped | failed
```

Three consecutive read-backs must agree before an achieved size is believed, a
monotonic deadline bounds the whole attempt, and `fit_max_attempts` caps the
requests. A refusal (permission denied, fullscreen, no window) is `failed`; a
size the OS or the game clamped is `achieved_clamped`, and it is **adopted**,
because a clamp is an answer.

**Why the readback count.** A window answers a resize late. Reading once
returns the old size, and reading twice can catch it mid-animation. Three
agreeing reads is the smallest number that distinguishes "settled" from
"passing through".

**Why a revision counter.** `geometry_revision` advances on window
replacement, display migration, backing-scale change, resize and loss.
Everything derived from a frame — observations, tracker state, ROI state,
actionable commands — is keyed by it, so a resize cannot leave a stale
coordinate alive. During Live a geometry change releases `W` first and blocks
new input until the new basis is verified.

---

## D-024 — 2026-08-28 — Colour proposes, geometry disposes

**The observed failure.** In daylight a large patch of grass matching the
arrow's colour was promoted over the real arrow, which was rejected. Candidates
were ranked by **area**, and confidence was mostly an area-fit score, so a big
blob of the right colour scored highly on being the right size.

**What the measurements said.** Seven owner-supplied crops of the live arrow —
flat-on over dirt, grass, water and pale terrain, plus a strongly foreshortened
one, a right-pointing one, and one filling a quarter of the frame while
partly transparent — were segmented and measured:

| Property | Measured range |
|---|---|
| green chromaticity, arrow vs. its own background on grass | **0.518 vs 0.520** |
| interior luminance / ring luminance | 1.21 (pale terrain) – 2.65 (dirt) |
| solidity | 0.851 – 0.961 |
| extent (area / bbox) | 0.467 – 0.686 |
| circularity | 0.510 – 0.633 |
| `approxPolyDP` vertices at 2 % of perimeter | 5 – 8 |
| fitted-ellipse elongation | 1.27 – 2.93 |
| two deepest convexity defects / bbox diagonal | 0.043 – 0.155, **matched within 1.1–1.7×** |
| third-deepest defect | 0.003 – 0.018 — an order of magnitude smaller |

Three decisions follow directly from those numbers.

**1. No colour rule can work, so colour only proposes.** On grass the arrow and
the background share a chromaticity to three decimal places. A mask tight
enough to exclude the grass excludes the arrow. So the proposal stage is
deliberately loose and is *allowed* to include terrain; the score decides. Two
proposal sources are used, because neither covers the other's blind spot:
chroma-and-locally-brighter isolates the arrow when the terrain shares its
colour, and chroma alone survives the case where the arrow fills the view and
becomes its own local background.

**2. The two-notch signature is the discriminator, and it is necessary.** Every
crop has exactly two deep convexity defects of comparable depth with a large
gap to the third — the junction where the arrowhead meets the shaft. It is
invariant to rotation and scale and terrain does not produce it. It is not
merely weighted heavily: a candidate with no notches is scaled below the
acceptance threshold whatever else it scores, because an arrow-coloured ellipse
satisfied contrast, solidity, extent and scale simultaneously and won.

**3. PCA is not a direction cue for this shape.** Flat-on elongation is
1.27–1.53. An axis that weakly conditioned flips sign readily, which is the
180-degree flip seen in the field. PCA is kept as an *unsigned* axis, is
refused below an anisotropy floor, and is signed by the topology cues rather
than the other way round.

**Direction comes from topology instead.** The notch midpoint is the base of
the head; the hull vertex farthest from it is the tip; the vector between them
is signed by construction. Polarity is decided by **taper** — the head narrows
to a point while the shaft holds its width — because reach is nearly symmetric
(0.75 against 0.60 in model units) and under perspective the tail can reach
*further* than the head. Measured taper is 0.46–0.69 for heads against
0.25–0.38 for shafts.

**Two bugs this design flushed out during development.** A trapezoidal
membership band with an exclusive upper bound scored a *perfectly* matched
notch pair as zero, which is the ideal case. And selecting the two deepest
defects picks the wrong pair under foreshortening, where one notch shrinks
below an unrelated nick; the pair is now chosen by which segment passes closest
to the centroid, because the real pair is the shape's own waist.

**Clipping costs confidence rather than rejecting.** When the player stands
under the arrow it fills a quarter of the view and touches every edge. The
previous detector rejected that outright.

**What is not claimed.** The bands above are fitted to seven views from one
session on one machine. They are a *prior*. E-PROF and E-DIR-E2E need real
multi-session labelled data with a held-out split evaluated once, and both are
PENDING.

---

## D-025 — 2026-08-28 — Shift Lock is verified, and W is a lease

**Decision.** `prospector_engine/steering.py` owns the control law, the yaw
calibration and the control-mode proof. Three rules are structural rather than
conventional.

**Shift Lock is a state, not a key.** Nothing in the codebase presses Shift. A
`ShiftLockProof` is bound to the run id, the arm token, the generation, the
window identity and the calibration fingerprint, and it expires after 20
seconds, because the player can toggle Shift Lock at any moment. Any of those
changing invalidates it. Without a valid proof, Live is unavailable — not
guessed at.

**`W` is a lease, not a state.** Renewal requires a *strictly newer* accepted
frame, so one frame authorizes exactly one decision. A frozen pipeline cannot
keep the character walking: the lease expires on its own. `CommandKind.ALIGN`
is structurally unable to command forward motion — `NavigationCommand.__post_init__`
raises rather than trusting the caller to remember.

**Alignment is stationary.** `W` is never acquired outside the validated
alignment threshold, and only after several consecutive frames inside it, so a
wrong heading costs a rotation rather than a journey.

**Two control-law bugs found by the closed-loop tests.**

The jerk limiter was dimensionally wrong — it bounded the *rate* change by a
jerk-times-dt² quantity — so the yaw rate could not decelerate and the loop
sailed past zero: 11.4 degrees of overshoot on a 5-degree correction. The rate
ceiling is now the largest rate the acceleration bound can still stop within
the remaining error, `sqrt(2 · a_max · |error|)`.

Below the actuator's minimum effective movement the loop dithered forever — 11
zero crossings on a 5-degree correction — because asking for a rotation smaller
than the mouse can produce yields the mouse's minimum instead. The deadband is
now floored on the *measured* actuator resolution rather than configured
independently of it, which is what plan §9.1 requires and what makes the
requirement concrete.

**A command is applied only if every edge succeeded.** If a forward lease
cannot be taken, the yaw that would have accompanied it is not emitted, what
did land is released, and the result says `REJECTED`. Turning while believing
the character is walking is a worse state than doing nothing.

---

## D-026 — 2026-08-28 — Two views of one runtime, derived from one packet

**The observed failure.** The preview showed frame 53545 while the decision
panel showed 53542, and the profile selector read `generic_saturated_v0` while
the pipeline ran `yellow_map_v0`. Both are the same bug: two views of one
runtime rendered from different sources and allowed to disagree.

**Decision.** Every dashboard packet carries a `RuntimeKey` of run id,
coordinator generation, mode session, source epoch, geometry revision, profile
revision, frame sequence and content id. A consumer draws a packet only if it
`supersedes` what it already has.

**Ordering is by monotonic world ordinal, not by difference.** Every component
of the key is a non-decreasing counter within one run, so a newer world has
every component greater than or equal to an older one's and at least one
strictly greater. That is what lets a straggling frame from a cancelled worker
be recognised as *old* rather than merely different — the first version of this
treated any key difference as newer, which would have let a straggler overwrite
the session that replaced it.

**Stop publishes a terminal packet immediately.** The picture may persist; it
is labelled frozen with its age, and its command is `None`. A frozen image may
never look actionable.

**`ProfileAuthority` owns the one active profile.** Stable ids in, display
labels derived out — no caller recovers an id by splitting a string. A request
is staged and applied at a frame boundary, bumping `profile_revision` once and
rebuilding tracker, ROI and arrival state. Changing profile spends the arm
token; changing it during Live releases input and safe-stops.

---

## D-027 — 2026-08-28 — The progress guard may say "stop", never "go around"

**Decision.** `ProgressGuard` produces conservative evidence about whether
commanded forward motion is producing observed progress, and nothing else.

* Elapsed time never declares an obstacle. Holding `W` for two seconds is not
  evidence of a wall; measured low displacement with high motion confidence and
  low yaw contamination is.
* Ambiguous motion — low texture, poor spatial coverage, yaw contamination, a
  post-yaw hold-off — returns `UNKNOWN`, which is never a reason to act.
* A suspicion **releases forward before confirming**, so the confirming
  evidence is not measuring the motion it is judging.
* It reasons about forward the input authority *accepted*, not what the
  navigator asked for. A run of rejected commands must not look like a wall.

`ProgressVerdict` has a `release_forward` field and no field in which a
maneuver could be expressed; a test asserts that. There is no recovery ladder
in the control path, no detour, and no A/D/S/jump anywhere in this pass.

**The boundary with the later terrain work.** `TraversabilityObservation`
records, per frame, what was commanded, what motion was observed, and how much
either can be trusted. It is written now and read by nothing, so the 2.5D
traversability grid — a later phase — has real data to build on rather than a
retrofit. Building the grid on top of the observations this pass replaced would
have meant building it on the wrong ones.

---

## D-028 — 2026-08-28 — Cadence is judged on processed frames

**The observed failure.** One session reported `tier 15 Hz` with `unique 19/s`
and stayed there; another reported 73 processed fps at 209 % CPU. The governor
judged a tier on *captured* frames, so a pipeline delivering 120 and processing
57 kept a 120 Hz label, and a single transient could strand it at the bottom of
the ladder.

**Decision.** An explicit state machine — `WARMUP → STABLE → PROBE → COOLDOWN`,
with `DEGRADED` for below the 30 Hz Live floor — judged on **processed**
throughput, with frame age, p95 age, observation loss, stale frames and pool
exhaustion each able to downshift on their own.

A probe is judged against the bar for *keeping* a tier (90 %), not the lower
bar for surviving in one (70 %); otherwise a climb "succeeds" at 73 % of the
tier it just claimed, which is how a 90 Hz source ends up labelled 120 Hz. A
failed probe is remembered for a bounded period, so a 60-capped source probes
once instead of oscillating, and the ceiling expires so a machine that recovers
gets its cadence back.

**Metrics separate six rates** — requested, source, unique, processed, control,
preview — because conflating them is what made two contradictory numbers both
true. Counters are per-session with lifetime carried separately; "dropped
latest frames" is reported as **superseded**, because the design intends it and
calling it a drop alongside genuine failures made a healthy pipeline read as
catastrophic. Current RSS is measured separately from peak: `ru_maxrss` is a
peak and was being displayed as "memory now".

---

## D-029 — 2026-08-28 — The detector is judged on a real-frame corpus, split by sequence

**Plan text:** §7.2 forbids rendered frames in a held-out split; §7.4 (E-PROF)
requires labelled real sessions.

**Decision:** `tests/corpus/real` holds 170 frames extracted from the owner's
70-second screen recording of the previous dashboard build plus eight frames
captured read-only from the live client, labelled by a reviewer, split by
**contiguous sequence** into `tune` (what the detector was chosen on) and
`eval` (only ever read). `prospector_engine/corpus.py` is the contract: labels
in canonical 1280x720 pixels, positive absence labels, `unknown` frames
excluded from every rate and counted, overlay contact marked, and an
evaluator that scores the **bounding box** as well as the heading so a
confident lock on the wrong object is a measured number
(`false_locks`), not something a heading-only evaluator averages away.
`tests/test_corpus.py` holds the eval-split results as a regression floor.

**Why:** the rendered stress report said eleven of thirteen strata were
near-perfect while the field build read 52% recall, eight false locks and a
coin-flip direction sign on the game. The two disagreed because the rendered
arrow has a clean outline and real outlines are nicked by UI strokes.

**What the corpus is not:** production evidence. One session, one map, one
machine, one lighting pass per stratum, no separately held-out session, the
previous build's overlay drawn on most arrows (a favourable bias on
same-colour sand and an unfavourable one on outline quality), preview
downscaled then upscaled, WebP compressed. Every rate in `STATUS.md` carries
its count. E-PROF and E-DIR-E2E stay PENDING.

**Deviation:** none.

---

## D-030 — 2026-08-28 — One temporal transaction per frame; structure is evidence, not a veto

**Plan text:** §8 (tracking) and §7.3 (abstention).

**Decision:** `ArrowDetector` is three stages with a hard boundary:
stateless `propose()` in full-frame coordinates, `fuse()`, and `commit()`,
which advances temporal state exactly once per unique frame and returns the
previous outcome marked `duplicate` for a repeated one. A region miss no
longer runs a synchronous full pass on the same screenshot; it schedules the
global search for the next frame. The tracker is an explicit
ACQUIRE / TRACK / AMBIGUOUS / REACQUIRE / LOST machine with **time-based**
bounds and frame floors, so the contract is the same at 60 fps live and at a
corpus replayed at 2.5 fps. Exactly one candidate is `selected` per
observation and direction, contour, tip and tail derive from it. Association
runs over every fused hypothesis before presentation truncation. Polarity is
a weighted vote led by the **barb asymmetry** beyond the notch line - the one
property that survives perspective and a hidden shaft end - with reversals
against a held identity refused below a margin.

The two-notch signature, the barbs and the prominent tip are **weighted
evidence**. Each was tried as a precondition and each cut eval recall by a
third or more without removing a single false lock, because real outlines
are nicked and the notch pair is misread on exactly the frames where recall
matters. Local contrast remains the one soft veto: every measured view of the
arrow is brighter than what is behind it, and a flat sand patch under a UI
label scored 0.76 without it.

**Measured (eval split, 4756ab7 -> this pass):** recall 52% -> 80%, false
locks 8 -> 0, identity switches 0 -> 0, direction sign 52% -> 91%, median
error 87 deg -> 10 deg, perception p50/p95 11/51 ms -> 5/8 ms. The occluded
stratum and the live event scene remain the weak strata and are recorded at
their measured values.

**Deviation:** the plan's "topology is necessary" wording from D-024 is
withdrawn on this evidence.

---

## D-031 — 2026-08-28 — Readiness is judged on a recent window; history is kept beside it

**Plan text:** §7.4 E-PERF; D-028.

**Decision:** every latency tracker keeps a 240-sample history for
diagnostics and answers readiness questions - the governor's verdict and
Live eligibility - from the last two seconds of the current epoch. Epochs
reset together across every rate and latency window on cadence, source,
geometry and profile changes. Polls during settling, and while
ScreenCaptureKit has not acknowledged a reconfiguration, are tagged and
skipped. A processed rate of zero is a real zero once a consumer is attached.
DEGRADED probes upward after the cooldown. A downshift needs a full second of
shortfall. A bounded per-frame trace (`prospector_engine/trace.py`) records
where every millisecond went and is exported as JSONL on Stop.

**Why:** one 274 ms sample from a resize sat in the ring and blocked Live for
as long as the ring took to roll over; a two-poll downshift cascaded Auto to
15 Hz on a startup transient; DEGRADED had no path to a probe; a stalled
worker read as a healthy 60 Hz pipeline because zero fell back to capture.

**Amendment, same day — a tier is judged against the tier below.** With the
real dashboard running, the worker processed 52 frames a second at 60 Hz
with 13 % superseded; the governor called that "observation loss 10 %",
downshifted to 30 Hz, and the probe back needed 54. A latest-only slot
supersedes by design, so a shortfall and its loss are problems only when the
tier no longer processes more frames than the tier below could deliver
(`useful_fps > below.fps`), and a probe holds if it processes 10 % more than
the tier it left. D-028's "57 processed at 120 is a 60 Hz pipeline" still
holds: 57 is not more than 90, so it steps down twice and stops at 60. Live
eligibility keeps the latency budget and asks for a processed ratio of 0.80
and superseding under 25 % (provisional, E-PERF PENDING) instead of 0.90 and
2 %, because a pipeline processing four frames in five at 60 Hz is fresher
than one keeping every frame at 30 Hz. The preview ticks at 30 fps instead
of 60: at 60 its paste competed with the worker for the interpreter.

**Deviation:** none.

---

## D-032 — 2026-08-28 — A clamp is an answer; fit completions are typed

**Plan text:** §4.1, D-023.

**Decision:** on macOS the AX window is correlated with the CG window that
was selected for capture (frame, then title, then largest); only the size is
set, so the window keeps its origin and a denied move cannot fail a resize;
`PinResult.ok=False` is reserved for permission, no-window, fullscreen,
unsettable and API refusals, and a clamp is `ok=True, clamped=True` with the
achieved geometry for the guard to classify after three stable read-backs.
The fit thread submits a typed `FitCompletion`; the coordinator loop applies
it, ignores stale generations, and invalidates geometry only when the
revision moved.

**Measured:** `canonical_verified` in 0.35 s on the live client (1280x720 pt
/ 2560x1440 px, origin preserved) on one display at 2x. E-VIEW is now
**partial**: the fit half has one measured pass here; other DPIs, displays,
and Windows remain PENDING.

**Deviation:** none.

---

## D-033 — 2026-08-28 — Blockers are keyed and scoped; commissioning is a guided list

**Plan text:** §15 gates; mission section 10 (plain language).

**Decision:** `LiveBlocker(code, scope, status, summary, detail, remedy,
evidence)` with scopes *shadow readiness*, *current condition*, *native
commissioning evidence*, *live control eligibility*, recomputed on every read
and deduplicated by code. E-YAW is one gate row: no default controller is
instantiated to ask why it cannot steer. "Roblox is not frontmost" is an
*expected* condition with an instruction. Eleven commissioning steps are
rendered from live state and gate statuses; none passes from a fake. Controls
are named for what they do.

**Deviation:** none.

---

## D-034 — 2026-08-28 — Ambiguity is refused, not resolved by picking the biggest

**Plan text:** §4.1; mission section B.

**Decision:** `_ax_window_for` accepts an Accessibility window only when it
*identifies* one — its frame matches the CG window being captured, its title
uniquely matches, or the process has exactly one window with a readable frame.
Two windows sharing a frame, two sharing a title, or several with neither is
`(None, "ambiguous: …")`, and `pin_client_rect` turns that into a sentence
naming the remedy: close the extra Roblox window.

**Why:** D-032 correlated by frame, then title, then *largest*, then *first*.
The last two are guesses, and the failure they produce is silent and confusing:
a login prompt or a crash-report window is resized to 1280x720 while the game
stays as it was, and the fit reports success. A process with several windows is
a situation with a specific fix, not a size comparison.

**Deviation:** none.

---

## D-035 — 2026-08-28 — Fitting is a transaction; mismatch classification is fenced

**Plan text:** §4.1; mission section B.

**Decision:** `ViewportGuard.transaction(reason)` suspends `check()` and
`confirm_capture()` for the duration of a deliberate geometry change.
`fit_and_lock` runs inside one; the automatic-setup fit stage releases input
first, fits, restarts capture once, and waits for a fresh frame that matches
the adopted geometry before anything resumes. The fence is re-entrant, unwinds
on an exception, and is bounded by `fit_transaction_deadline_s` so a
transaction that dies cannot leave the guard permanently blind.

**Why:** `check()` and `confirm_capture()` are honest reporters — they see a
window that is not the size we adopted and say so. During a resize that is
exactly what was asked for, so classifying it as `CAPTURE_MISMATCH` restarted
capture, churned the source epoch, and blanked the preview for a change that
was going to succeed. Fitting looked like it did nothing; what it did was fight
the guard.

**Measured:** `tests/test_viewport_fit.py` runs twenty fits with a concurrent
`check()` reader and requires zero false mismatches, and asserts every
read-back inside a fit is fenced.

**Deviation:** none.

---

## D-036 — 2026-08-28 — Automatic setup replaces commissioning; three kinds of evidence

**Plan text:** §15 gates; mission section A.

**Decision:** `NavigationGates`, `COMMISSIONING_STEPS`, `commissioning_steps`,
`commissioning_blockers` and `CommissioningWindow` are deleted.
`prospector_engine/autosetup.py` runs nine typed, bounded stages from IDLE to
READY, and capability is `NavigationCapabilities`, derived from what this run
observed. Three kinds of evidence are kept apart:

* **offline build evidence** — the detector corpus, per-profile E-PROF — is a
  claim about the software, lives in `--detector-report` and STATUS.md, and
  gates nothing at runtime;
* **runtime checks** — this window, this geometry, these frames, this arrow,
  this actuator — are measured every run and are what READY means;
* **live safety** is unchanged: a physical click to arm and a physical hotkey
  press with Roblox focused.

**Why:** the gate structure could not complete. `NavigationGates` was
constructed once with every `E-*` field `PENDING` and frozen; no production
code validated or persisted a gate; `CommissioningWindow` was a periodically
rewritten read-only `Text` widget that ran no procedure. Three buttons
converged on it and `_arm()` redirected there whenever blockers existed, so
Live was unreachable twice over — the worker refused on pending gates, and the
default `ShiftLockController` refused on an empty `YawCalibration`. The unit
tests were green because they injected `ALL_PASSED` gates and a fabricated
calibration, which is exactly the shape of test that cannot see this.

`tests/test_setup_flow.py` builds the application through the real
`build_application` and drives it to READY with nothing injected.

**Deviation:** plan §15's gate table is retained for *offline* evidence and is
no longer the runtime authority. Recorded here as a deliberate departure.

---

## D-037 — 2026-08-28 — The control mode is observed, never toggled

**Plan text:** §9.1 E-SHIFTLOCK; mission section C.

**Decision:** the `VERIFY_CONTROL_MODE` stage looks at where the system pointer
is. In the locked camera mode Roblox pins the pointer to the centre of the
client; in the free mode it does not. A pointer held within 6 % of centre is
the cue; anything else — including a pointer that cannot be read — is "cannot
confirm", and setup stops with a sentence telling the user to switch Shift Lock
on.

**Why:** pressing Shift to find out would turn Shift Lock *off* for a player
who already had it on, which is both a worse outcome than stopping and an
input emitted before the actuator has been characterized. There is deliberately
no `ControlModeMethod.ASSERTED`.

**Limitation, stated:** this is a cue, not a proof that the camera is locked.
The independent check is the characterizer's left/right consistency
requirement: a free camera that the pointer happens to sit in the middle of
does not produce consistent signed rotation from bounded stationary probes. The
armed micro-yaw method (`ControlModeMethod.MICRO_YAW`) is defined and is
**pending** a native run.

**Deviation:** none.

---

## D-038 — 2026-08-28 — Turning is its own actuator; Left and Right join the vocabulary

**Plan text:** §4.4 release floor; mission section C.

**Decision:** `InputKey.LEFT` and `InputKey.RIGHT` are vocabulary members, so
the release floor, `release_all`, the deadman's target list and every safety
test cover them without a new code path. `NavigationCommand.turn_axis` is a
separate field from `lateral_axis` (strafe) and `yaw_delta_px` (relative
mouse); a command carrying both a turn key and a mouse delta raises, because
two actuators asking for the same rotation would double it and the response
model is fitted per backend. The opposite key is released before this one is
pressed because `_translate_navigation` releases everything not commanded this
tick *before* it acquires. On Windows both scancodes carry
`KEYEVENTF_EXTENDEDKEY`; without it the bare scancode delivers numpad 4/6.

**Deviation:** none.

---

## D-039 — 2026-08-28 — Profile identity is decided by margin, not by confidence

**Plan text:** §7.4 E-PROF; mission section G.

**Decision:** the runtime profile classifier scores each candidate as
`confidence x (0.2 + 0.8 x selection_margin)` and requires both a mean-score
margin over the runner-up and temporal agreement across consecutive frames.
`selectable_automatically` is a *runtime* property (`runtime_selectable` in the
bundled JSON), not the offline E-PROF gate; `generic_saturated_v0` and
`yellow_map_v0` opt out, the first because it is deliberately broad and would
win by matching anything, the second because it is superseded and would split
the margin with `yellow_map_v1`.

**Measured, on the real-frame corpus:** on sand frames the green-grass profile
reaches almost the same confidence as the correct yellow one (0.60 vs 0.66)
and won half the frames under a confidence-only score. Its *selection margin*
is a third of it (0.20 vs 0.56). With the margin weighting, `yellow_map_v1`
wins by 0.22 over 75 % of sixteen frames. Confidence says "something
arrow-shaped is here"; margin says "and nothing else looks like it", which is
the question profile identity actually asks.

**Deviation:** the plan gated automatic classification on E-PROF. That gate is
about whether a profile's *detector* is validated on held-out data; which map
is on screen right now is a runtime question with runtime evidence. Recorded
as a deliberate departure.

---

## D-040 — 2026-08-28 — The locomotion baseline is sampled from this run

**Plan text:** §7.4 E-MOTION; mission section E.

**Decision:** `LocomotionBaseline.measured_in_run` mints a baseline from this
session's own unobstructed walking — frames where forward was *applied* (the
authority's answer, never the request), motion confidence and coverage are
high, and yaw contamination is negligible. Twelve samples minimum; the stall
threshold is 35 % of the observed median, because the question downstream is
"has the character stopped", not "is it at full speed", and a slope or deep
water legitimately halves the speed. `condition_id` is prefixed `runtime:` and
the provenance note says, in words, that this is **not** the offline E-MOTION
gate.

**Why:** with `UNCALIBRATED_BASELINE` the progress guard abstained on every
frame, so contact could never be detected and recovery could never run — the
`motion=None` in the pipeline was only half the reason the whole path was dead.
E-MOTION is independently labelled open-ground trials that would let a *frozen*
threshold ship with the software; that is still PENDING and this does not
substitute for it. What this is: a measurement taken here, on this machine,
under the physical arm, and discarded when the session ends.

**Deviation:** CLAUDE.md rule 7 forbids populating a calibration that was not
measured. This one is measured, on real hardware, under the same physical
arming E-YAW requires. The distinction it must never blur is recorded in the
provenance note and asserted in `tests/test_navigation.py`.

---

## D-041 — 2026-08-28 — The window does not resize itself

**Plan text:** §11.2; mission section F.

**Decision:** four structural rules, each asserted in `tests/test_gui.py`
rather than left to review:

1. every dynamic string lives in a widget whose *requested* size is fixed — a
   width in characters (`fixed_label`) or a fixed-height box that wraps and
   clips (`MessageBox`);
2. conditional controls keep their grid cell and change `state`; nothing is
   `grid_remove`d, because a layout that changes when a fault appears jumps at
   the worst possible moment;
3. each polling loop owns exactly one cancellable `after` handle
   (`treasure_panels.Ticker`), with `start()` idempotent and `render_once()`
   never scheduling;
4. exactly one grid row expands.

**Why:** a ttk.Label sizes itself to its content, a grid to its children, and a
toplevel to its grid — so a status string growing from "Ready" to a sentence
about Accessibility permissions pushed the window wider, every time it changed.
And `CommissioningWindow.refresh()` scheduled its next tick at the *end* of the
render, so calling it directly to refresh on demand started a second loop that
ran forever: four clicks, four loops, four times the CPU.

The diagnostics drawer renders only when it is visible *and* something changed,
compared through a cheap key (observation identity, two rates, the packet
count, the geometry revision, the event-log sequence).

**Deviation:** none.

---

## D-042 — 2026-08-28 — The composition root is not a user interface

**Plan text:** mission sections A, H; §17.

**Decision:** `Application`, `build_application`, `EngineSetupPort` and
`shift_lock_probe` move out of `treasure_gui.py` into a new
`prospector_engine/application.py`. The Tk module imports the two names it
actually needs and owns nothing but the window. Nothing is duplicated: there
is one composition root, and both front ends call it.

**Why:** the wiring that decides *what automatic setup is* — which port, which
guard, which capture service, which profile library — lived in a module that
imports `tkinter` at the top. That had one concrete consequence, and it was
not a matter of taste: **the most important native check in the project could
not be run without opening a window.** "Does Start Navigator reach READY on
this machine?" is the question the whole fourth pass was blocked on, and the
only way to ask it was a throwaway script that reached into the GUI module —
so the answer was never committed, never repeatable, and never in CI's reach.

Moving the root made `treasure.py --setup-probe` possible: the real
`build_application`, the real coordinator, the real bounded stages, no Tk, no
input. The extraction also put ~450 lines under `mypy` for the first time
(`treasure_gui.py` is not in the checked set) and immediately found a real
`Any` leak — `make_setup` was annotated `-> Any`, so every `SetupProgress` the
setup runner returned was unchecked.

**Deviation:** none.

---

## D-043 — 2026-08-28 — A window read back once has not been read back

**Plan text:** §6.2; mission section B.

**Decision:** `--setup-probe` settles its restoring read-back — polling until
two consecutive identical client rectangles, bounded by both an attempt cap
and a monotonic deadline — instead of reading the geometry once after
`pin_client_rect` returns.

**Why:** measured, on the first native run of the probe. The client started at
1800×1053 pt, setup fitted it to 1280×720, and the restore asked for
1800×1053 and *succeeded* — but the single read-back immediately afterwards
reported `1063x610 pt at (18,499)`, a size that was never requested and did
not survive the next second. A second later the window was exactly
1800×1053 at (0,67), where it began.

macOS animates a resize, so the first read lands mid-flight. This is the same
mechanism the fit machine already handles with three stable read-backs
(D-032), which is why the fit stage was never wrong about its own result while
a fifteen-line restore was. The bug was in the probe's reporting rather than
in the restore, which makes it the more dangerous kind: it would have written
a false geometry into the evidence table and it looked like a platform defect.

**Deviation:** none.

---

## D-044 — 2026-08-28 — The packet is built after the command is acted on

**Plan text:** §6, §10; recovery-pass sections 5 and 6.

**Decision:** `_run_observer_loop` runs capture → perceive → decide → *propose
or apply* → publish. `DiagnosticObservation` carries a `CommandVisualization`
whose glyphs come from `NavigationApplyResult.leases_held` on APPLIED and from
the requested command on WOULD, and from nothing at all on REJECTED, RELEASED
or a frozen packet.

**Why:** `context.on_observation()` ran *before* `apply()`, so the packet was
already published by the time the authority answered. The UI could not
distinguish WOULD_APPLY from APPLIED because the information did not exist yet
— every requested command was drawn as though the character were moving. A
command asking for W and a right turn, applied with only `w` actually held, now
draws W and no turn.

`CommandStage` states the rest plainly: `OS_EDGE_POSTED` and
`AUTHORITY_APPLIED` are not success. `CGEventPost` returning without raising is
evidence that the call returned. Only `GAME_MOTION_CONFIRMED` — perception
seeing the world move — is success, and an applied command the motion estimator
contradicts is labelled **NO MOTION** rather than ACTIVE.

**Deviation:** none.

---

## D-045 — 2026-08-28 — Minimal means minimal, and a frozen packet is dead

**Plan text:** §11.2; recovery-pass section 6.

**Decision:** every detector internal — cue arms, cue labels, outlier text, the
contour, bbox, centroid, shaft, tip and notches — is Full Diagnostics only.
`set_mode` hides what is already on screen instead of waiting for a redraw. A
`TRANSITION` or `TERMINAL` packet keeps its picture in one flat grey, loses
every internal, and clears the action layer.

**Why:** `_draw_cue_arms` was called unconditionally and the bbox, centroid,
shaft and tip were never gated at all, which is the blue rays and "(outlier)"
text over a route in the reported screenshot. Waiting for a redraw is wrong
precisely when it matters: a stopped run has no next frame.

That last point needed a second fix the test found. Full Diagnostics throttles
overlay redraws to 20 Hz, so a Stop landing inside that window kept **ACTIVE**
on screen until the next frame — which never came. A frozen packet is never
throttled.

**Deviation:** none.

---

## D-046 — 2026-08-28 — Ctrl+Option chords, from one registry

**Plan text:** §11.2; recovery-pass section 4.

**Decision:** `prospector_engine/bindings.py` holds the only binding table and
the only chord state machine. Primary bindings are Ctrl+Option (macOS) /
Ctrl+Alt (Windows) + N/O/X/R/P/D/I. F1–F6 keep working as legacy aliases
derived from the same registry and are advertised nowhere.

**Why:** the bindings were declared twice with a virtual-key table beside each,
and spelled out a third time in the GUI and README. F1 is Help in most
applications, the row is brightness and volume by default on a Mac, and one
unmodified keypress starting a character walking is a slip away.

**Shift is in no chord** because Roblox binds Shift Lock to it: a start chord
that toggled the camera mode the navigator depends on would be fighting itself.

`ChordRecognizer` is shared rather than reimplemented per platform, because the
rules that are easy to get wrong are identical on both: rising edges only (an
autorepeating chord must not submit eighty intents a second), both modifier
sides, and state cleared on focus changes so a key-up delivered to another
application cannot leave Ctrl held forever. macOS adapts pynput events; Windows
turns polled `GetAsyncKeyState` levels into the same edges.

**Deviation:** none.

---

## D-047 — 2026-08-28 — Consumption is scoped; measurement is separate

**Plan text:** §6.3, §7.4; recovery-pass section 3.

**Decision:** `LatestFrameSlot` counts registered consumers instead of latching
a flag, `CaptureService.consuming(reason, measured=...)` scopes it, and only a
*measured* phase has its processed rate judged by the governor. Entering a
scope resets the rate window. A tier below the Live floor may not rest there:
after `ineligible_retry_s` of health the saturation gate is waived.

**Why:** `wait_for_new` set `_has_consumer` and never cleared it. Automatic
setup and the live prologue each read a handful of frames, which marked the
slot consumed for the rest of the session; neither ticks the processed-rate
counter. After setup finished the governor saw a consumer that no longer
existed reporting zero processed fps, called it a stalled worker, and walked
the cadence 60 → 30 → 15 Hz. `SteeringLimits.min_processed_fps` is 30, so Live
refused to steer — for a reason the pipeline had invented about itself.

Instrumenting setup instead was tried first and is worse: setup polls on a
deliberately slow bounded schedule, so counting its cadence as throughput
judges the pipeline on how slowly a probe chose to run, and `SHADOW_QUALIFY`
then failed its own fps floor.

**Deviation:** none.

---

## D-048 — 2026-08-28 — State is a verdict, not a coordinate basis

**Plan text:** §5, §6.2.

**Decision:** `ViewportGeometry.coordinate_basis()` is the identity without
`state`. `same_source`, `ViewportGuard.check()` and `_adopt_locked` all compare
the basis. `check()` re-adopts a healthy window when nothing is adopted, never
overwrites its own verdict with a fresh read that agrees with it, and
`restart_source` re-adopts only when the guard holds nothing usable. Each
capture source's callback is bound to that source; frames from a superseded one
are released.

**Why:** measured against the live client, where automatic setup failed
intermittently with `capture_stale` on a correctly fitted window.
`window_geometry()` can only ever report `ADOPTED_NONCANONICAL` — the port
reports the window as it finds it, and `CANONICAL_VERIFIED` is the *guard's*
verdict about it. Comparing full identities therefore compared "verified"
against "adopted" on every poll after a successful fit, called the difference a
`CAPTURE_MISMATCH`, and undid the fit it had just achieved; the unchanged
branch separately downgraded the verdict by overwriting it. Together they
produced a revision storm — about forty state flips a second, the supervisor
rebuilding the source on each — while in-flight frames from stopped sources
carried the pre-fit rect and poisoned the guard again.

Measured: `capture_stale` on 4 of 6 native runs before, 0 of 6 after.

**Deviation:** none.

---

## D-049 — 2026-08-28 — A recovery record may not write its own successor

**Plan text:** §4.4.

**Decision:** `ReleaseReport.evidence_clean` — no failures, a positive deadman
acknowledgement, an empty ledger — is what `shutdown()` persists on.
`release_known_safe` is unchanged and remains the only thing that gates Live.

**Why:** observed on this machine. The persisted record's own evidence read
`deadman_acknowledged: True, ledger_empty: True, failures: []`, and it still
blocked Live run after run. `release_known_safe` means *this release was clean
**and** no earlier uncertainty is latched*, so a run that inherited a record
re-wrote one at shutdown from a release that had gone perfectly, and the next
run inherited that. One uncertain shutdown poisoned the machine permanently.

An inherited latch must still refuse to navigate — that part was right. It must
not manufacture its own successor.

The preflight's remedy was also wrong in a way that mattered: Stop & Release
cannot clear a recovery record, because clearing it requires a positively
acknowledged release, which is the Recover Release handshake. It named the one
control guaranteed to leave the user where they started.

**Deviation:** none.

---

## D-050 — 2026-08-28 — The macOS hotkey listener died on the first keypress

**Plan text:** §3.1, §11.2.

**Decision:** `MacHotkeySource` is a direct Quartz event tap. `pynput` moves
from a runtime dependency to a dev one, kept only so the regression test that
records this can run.

**Why:** measured here, not inferred. In `pynput` 1.8.2 the callback's return
value is control flow — `AbstractListener.__init__` wraps every callback so a
`False` return raises `StopException` and stops the listener permanently:

```
$ .venv/bin/python -c "..."   # tests/test_bindings.py holds the executable form
plain 'a': LISTENER STOPPED -> StopException
ctrl_l:    LISTENER STOPPED -> StopException
```

The old adapter returned `False` for every key it did not recognize — which is
every ordinary key, *and Ctrl on its own*. So the listener died on the first
keypress of the session and no chord after it was ever heard. That is the whole
of "Ctrl+N does nothing". Every existing test called `on_press` directly and
discarded the return value, so the one contract that mattered was the one
nothing checked.

The reported cause was a callback-arity `TypeError` — the Darwin backend calls
`on_press(key, injected)` while the adapter took only `key`. **That half is not
true of 1.8.2**: `Listener._wrap(f, 2)` inspects the signature and adapts a
one-argument callback. Verified by calling `listener.on_press(key, False)`
against a one-argument bound method; it returns normally. The arity is a real
hazard in principle and a non-cause here, and the fix is the same either way.

Two things a direct tap gives that pynput 1.8.2 cannot:

* **Recovery.** macOS disables a slow tap and posts
  `kCGEventTapDisabledByTimeout`. pynput's run loop never handles it, so the
  tap stays dead while its thread stays alive and `running` stays true. Here it
  is re-enabled, counted, and turned into a hard failure if it will not stay up.
* **A readiness answer that means something.** `_create_event_tap` returning
  `None` — Input Monitoring not granted — makes pynput call `_mark_ready()` and
  return, so `wait()` succeeds and `is_alive()` is briefly true for a listener
  that will never hear anything. READY here requires the tap to exist,
  `CGEventTapIsEnabled` to be true, and the run loop that delivers key events
  to have completed an iteration.

The tap is `kCGEventTapOptionListenOnly`: it cannot swallow, alter or inject a
keystroke. Injected events are dropped by process id, so our own synthetic
`D` cannot return as `Ctrl+D`.

**Deviation:** the plan's dependency table lists `pynput` for macOS hotkeys.
Superseded here; the table is not rewritten (CLAUDE.md §8).

---

## D-051 — 2026-08-28 — Ctrl-only chords, and the F-keys are gone

**Plan text:** §11.2, and the D-046 chords this replaces.

**Decision:** the bindings are `Ctrl+N/O/X/R/P/D/I`, identical on macOS and
Windows. No Option, Alt, Shift, Fn or function key appears in any chord, alias,
label, tooltip, README line or test.

**Why:** `Ctrl+Option` is three keys on a laptop and collides with macOS
text-navigation chords; the two platforms spelling the same chord differently
made every label conditional for no benefit. The F-key aliases were removed
rather than hidden — an alias that still fires has not been removed, and a
single unmodified keypress that starts a character walking is one slip away.

Three consequences that are the actual work:

* **Exactness.** `Ctrl+Option+N` must not match `Ctrl+N`, so the recognizer
  tracks Alt, Shift and Meta precisely in order to *disqualify* a chord. The
  table is keyed by the whole modifier set, never by subset.
* **The modifier set is a level reading, not an accumulation.** Every key event
  carries what the OS says is physically down — `CGEventGetFlags` on macOS,
  `GetAsyncKeyState` on Windows. The old recognizer accumulated key-down and
  key-up edges, so the first key-up delivered to another application left it
  believing Ctrl was held forever. A level cannot be stranded.
* **Quarantine, which a level reading alone does not give.** A Ctrl the user is
  holding in another application is genuinely down, so a level reading would
  happily complete a chord the user never pressed here. On a focus transition
  every held key is quarantined and counts again only after the OS reports it
  released. On Windows this also meant *not* zeroing the poller's own level
  cache, which used to manufacture a rising edge on the very next poll.

**Deviation:** the plan's §11.2 hotkey table lists F1–F5. Superseded twice now
(D-046, then here); the plan is not rewritten.

---

## D-052 — 2026-08-28 — Prove the game takes a key before measuring a camera

**Plan text:** §3.4, §7.4, and the D-045 prologue this extends.

**Decision:** a new bounded stage, `SetupStage.VERIFY_INPUT`, runs first in the
live prologue. With the character stationary it measures the idle motion noise
floor, posts one ~160 ms forward pulse, watches only frames captured *after*
the down edge, releases, and reports one of six named outcomes.

**Why:** read out of `logs/stop-epoch7-1886181997.jsonl` on this machine. Its
last normal frame is at `1886148.84`; the file was exported at `1886181.997` —
**33.15 s** later, which is the `characterize_turn` deadline plus the poll
slack. Live entered, spent its whole budget failing to measure a camera, and
reported a timeout. No `turn-response.json` has ever been written.

Characterizing a turn *assumes* the game is acting on our input. When it is
not — the commonest failure by far — that stage cannot discover it, because
"the camera did not move" is what both faults look like. It spends thirty
seconds proving nothing and then names the wrong problem.

The six outcomes are kept separate because they have six different remedies:
`NO_POST` (Accessibility), `NO_LOOPBACK` (the OS never registered the edge),
`NO_LEASE` (the authority refused), `INSUFFICIENT_EVIDENCE` (too few usable
frames), `NO_MOTION` (Roblox is not acting on the key), `MOVED`.

`NO_LOOPBACK` is new and needs a new read-only port method, `key_state`:
`CGEventSourceKeyState` on macOS, `GetAsyncKeyState` on Windows. It answers the
one question a returning `CGEventPost` cannot — *did the edge reach the window
server at all?* Verified read-only here: it reports `False` for every key at
rest and flips to `True` while a key is physically held.

**Deviation:** the plan's prologue has two armed stages. There are now three.

---

## D-053 — 2026-08-28 — Motion was read off the frame that produced the command

**Plan text:** §7.4, §11.

**Decision:** `_motion_confirmed(motion)` — `abs(forward_speed_norm) > 0.0` on
`result.inputs.motion` — is deleted. `ForwardMotionWitness` replaces it.

**Why:** two faults in one line, and together they made the overlay's motion
claim meaningless in both directions.

* **It was not causal.** `result` is the perception of the frame the decision
  was made *from*. It describes the world before the edge went down. A command
  can never be confirmed by the frame that produced it.
* **`abs(speed) > 0` is not movement.** Optical flow is never exactly zero, so
  a stationary character reported motion on essentially every frame.

The witness records the hold's start on its *rising* edge only — restamping it
on every renewal would make every frame arrive "before the edge" and it would
abstain forever — discards frames captured at or before that instant, learns
the idle noise floor from frames where forward is not held, and requires the
median of a short held window to exceed a multiple of that floor with the
signs agreeing. It is seeded from the acceptance probe's measurement so the
first held frames are judged against a number taken on this machine seconds
earlier rather than against nothing.

`None` remains a real answer and is not `False`: holding W against a wall and
having no motion estimate are different facts.

**Deviation:** none.

---

## D-054 — 2026-08-28 — One file, the whole story

**Plan text:** §11.1, §13.

**Decision:** `prospector_engine/lifecycle.py` names sixteen stages between a
physical keypress and a character that moved. The authority, the coordinator,
the listener and the live worker all write to one journal, and `_export_trace`
appends it — plus the raw `EventLog` stream — to the stop JSONL.

**Why:** measured. Every stop trace in `logs/` contains exactly three row
kinds: `frame`, `preview`, `governor`. `stop-epoch7-1886181997.jsonl` has 6574
rows and not one of them mentions the arm, the worker, the prologue, an OS
edge, a lease or a motion verdict. A session that armed, entered Live, failed
input acceptance and stopped is byte-for-byte the same shape as a session that
never armed. `EventLog` held the answer and was memory-only, so it died with
the process.

`CommandStage.OS_EDGE_POSTED` had existed as an enum member since D-045 and was
emitted by nothing; it is now written by the only object that can honestly say
it, at the moment `_emit_down` returns.

The near-miss filter matters: every keystroke anywhere on the machine reaches
the listener, so only recognized chords and named-key near-misses are recorded.
Journalling all of them would push the events that matter out of a bounded ring
within seconds of somebody typing.

**Deviation:** none.

---

## D-055 — 2026-08-28 — The window may not call the prologue "navigating"

**Plan text:** §11.2.

**Decision:** `_LIVE_STAGE_WORDS` gives each armed stage its own sentence, and
a test asserts every `SetupStage.emits_input` stage has one. The prologue's
`SetupProgress` is published to the coordinator instead of into
`lambda _p: None`.

**Why:** the guidance line read `Navigating. Press Stop at any time.` for
every `RunMode.LIVE` state, and the badge guide read `Navigating - Stop is
always available.` Both were shown throughout the input-free prologue, so
thirty seconds of failing to characterize a camera was indistinguishable from
thirty seconds of walking — while the character stood still.

The progress sink was the other half: the control phase published its stages
into a lambda that discarded them, so even a correct renderer had nothing to
render.

**Deviation:** none.

---

## D-056 — 2026-08-28 — Qualification must predict what the next stage needs

**Plan text:** §4, §7.4.

**Decision:** `SHADOW_QUALIFY` measures seven things instead of one average:
hit rate, longest unbroken run, worst miss streak, heading jitter, mean
confidence, identity switches, and worst frame age. The hit-rate floor moves
from 0.6 to 0.85, and each shortfall names both numbers.

**Why:** the old gate was "60% of frames carried an arrow", and it does not
predict the thing it gates. A turn probe needs some still frames, a pulse, and
some frames after it, *consecutively*. Sixty percent spread evenly is a
detector that works; sixty percent arriving as one long run and one long gap is
a detector that will lose the arrow in the middle of every probe — and both
pass an average. `tests/test_autosetup.py` holds two windows with identical hit
rates where one qualifies and the other cannot fit a probe.

Identity switches are in the list because a probe measured across one measured
two different arrows, and the rotation attributed to the probe is whatever the
difference between them happened to be.

`SetupConfig.__post_init__` refuses a configuration whose `qualify_min_run`
exceeds `qualify_frames`, or whose `qualify_max_miss_streak` no window could
reach. An unsatisfiable configuration should be an error where it is written,
not a stage that mysteriously never passes.

**Deviation:** none.

---

## D-057 — 2026-08-28 — Name the measurement, not the clock

**Plan text:** §4.

**Decision:** the characterization failure paths report which measurement ran
out. The unreadable-arrow case is reported as itself from *both* the deadline
and the actuator-unproven branch.

**Why:** "measuring the camera turn took longer than allowed" is true of every
timeout and tells nobody which thing to fix. The commonest way to reach that
deadline is an arrow the detector could not read, and the old message sent a
person to check their camera settings — which were fine.

The loop now counts fresh frames and frames with no readable heading, and the
failure distinguishes: no fresh frames at all (capture), a majority unreadable
(the arrow), no probe ever started (the character was moving), and probes sent
with no measurable rotation (genuinely the camera).

**Deviation:** none.

---

## D-058 — 2026-08-28 — The gate made the detector blind on purpose

**Plan text:** §7.2, §8.

**Decision:** `ArrowDetector._resume_outside_gate`. When the positional gate
finds nothing, a candidate may resume the held identity from anywhere in the
frame if the three *non-positional* identity cues agree — orientation,
appearance signature, and scale within a deliberately wide band — and it is the
unambiguous global best. Bounded by `resume_max_age_s`.

**Why:** measured, and the mechanism is arithmetic. The gates only widen with
elapsed time (`gate_base_px + gate_rate_px_s * dt`, `scale_gate +
scale_rate_s * dt`), and elapsed time only accumulates while the track is
*missing*. So a perfectly visible arrow that moved was refused until the gate
had crawled out far enough to reach it. Reproduced at 60 fps on rendered
frames, with the arrow unambiguous in every single frame:

| step | before | after |
|---|---|---|
| jump 100 px | 67 ms | 0 ms |
| jump 180 px | 233 ms | 0 ms |
| jump 250 px | 367 ms | 0 ms |
| jump 400 px | 567 ms, new identity | 0 ms, same identity |
| scale 70→110 | 200 ms | 0 ms |
| scale 70→130 | 367 ms | 0 ms |
| scale 70→160 | 567 ms, new identity | 0 ms, same identity |
| scale 70→200 | 567 ms, new identity | 0 ms, same identity |
| sweep 25°/frame | 200 ms, **179.9° heading error** | 0 ms, 0.3° |

The sweep row is the one that mattered most. Recovering after a blackout meant
reacquiring, and the polarity guard kept the direction the arrow had *before*
it swung, so the detector came back pointing almost exactly backwards. A
steering controller handed that walks the character away from the treasure.

The rule is the inverse of the old one. Position is the identity cue a fast
camera turn destroys and the one foliage cannot fake, so it is neither
sufficient nor necessary alone; the other three are unaffected by motion. Two
close candidates resume nothing, because two similar candidates is exactly the
same-coloured-foliage case this must never fire on.

**`resume_max_age_s = 0.06` is what makes it sound rather than convenient.** A
resume is the claim *"the arrow moved between two consecutive frames"*, and
that claim is only supported while the frames are close together in time. The
first version had no such bound and took the tune split's `sand-a` from zero
false locks to one — a same-coloured sand blob resumed the identity across a
200 ms corpus gap, which is a gap the arrow could have crossed the screen in.
With the bound, resuming is inert below about 17 fps.

**Measured on the real corpus, tune and eval, before and after: every sequence
is identical.** Recall, false locks and identity switches are unchanged on all
fourteen sequences. That is the expected result and not a disappointing one:
the corpus is sampled at about 5 fps, so it cannot reach the regime this fixes.
The tuning decision was taken on `tune` alone (CLAUDE.md §9); `eval` was read to
report.

**A weakness this does *not* fix, recorded rather than hidden:** in two of the
nine rendered clutter layouts, deleting the arrow still leaves the detector
reporting a blob — with resuming switched off as well as on. A same-coloured
blob that lands near the arrow's last position is accepted by the *ordinary*
positional gate. That is what `sand-near-a`'s 26.7% false-lock rate is, it is
untouched here, and it is the strongest argument for the learned-keypoint
phase-two option.

**Not done, and not pretended:** no Lucas-Kanade bridge, no per-frame ROI
tracker, no covariance state, no `FLOW_BRIDGED` / `PREDICTED_ONLY` provenance.
The measured blackout was entirely an association-rule problem and is entirely
gone without a second tracker; adding one now would be adding a mechanism to a
symptom that no longer exists. The acceptance gates that need dense green
recordings remain PENDING for want of the recordings, not for want of the code.

**Deviation:** the mission asked for a hybrid classical tracker with optical
flow bridging. What was built is the association half of it. Recorded here
rather than reported as complete.

---

## D-059 — 2026-08-28 — A window cannot be trusted to be the build you just made

**Plan text:** §13.

**Decision:** `resolve_build_identity()` reads the version, branch, short
commit, dirty flag, process id and launch time once at startup. The short form
is in the window title; the full form is in the diagnostics drawer and is the
first line `--self-test` prints.

**Why:** a dashboard left open from an earlier run is pixel-identical to a
freshly launched one. That is how a fix gets tested against the build that did
not have it and reported as not working, and it costs a whole debugging session
every time. All four facts are shown because any one alone can mislead: a
commit matches a window that has been open since before it was written, and a
process id matches a build nobody rebuilt.

The dirty marker is not decoration. A commit id alone is a claim about code
that a modified working tree does not support.

Git is consulted only from a source checkout — a packaged build has no `.git`
beside it and reports its version rather than shelling out to a tool it does
not ship. The call is bounded and cannot raise.

The listener's health is shown beside it, for the same reason: "Hotkeys:
running" was true of a listener that had died on the first keypress of the
session, so the edge and chord counts are there too. A count that never moves
is a listener hearing nothing, whatever its state says.

**Deviation:** none.

---

## D-060 — 2026-08-28 — Every probe was a tap, because a lease may not outlive its evidence

**Plan text:** §4.3, §4.5, §9.1.

**Reported by the owner, from watching it:** *"I think it taps the keys too
fast, like W and > and all those should be held, not rapidly tapped, because it
taps it so fast and for so short it either doesn't register or moves like 10
atoms forwards."*

**Decision:** a probe holds a key by **renewing it against each newer frame**,
exactly as ordinary navigation does. `key_probe_ms` becomes
`(60, 100, 160, 240, 320)` and the acceptance pulse becomes 320 ms.

**Why:** the observation is correct, and the mechanism is a rule that was doing
its job in a place nobody had checked. `apply_navigation_command` refuses a
command whose lease outlives its evidence:

```
if command.valid_until_s > token.captured_at_s + max_evidence_age_s:
    return REJECTED_EVIDENCE, "command lease exceeds evidence age"
```

`max_evidence_age_ms` is 100, and the frame a decision is taken on is already
20-45 ms old — the measured `capture_to_observation_ms` in
`stop-epoch7-1886181997.jsonl` is **44.86 ms**. So the longest hold a *single*
command can express is 55-80 ms. Every camera probe was one command. The
ladder's top rungs were therefore unreachable: 85 ms and 100 ms sent the same
short press as 60 ms, and `TurnLimits.key_probe_ms` even carried a comment
explaining that 100 was the ceiling *because* of this. A camera that needs a
real press to move could not answer any rung, the backend was written off as
unproven, and the stage timed out — which is the 33-second failure in D-052,
arriving by a second route.

Worse, and found by chasing the same observation: **the acceptance pulse I had
just written asked for 160 ms from one command, so it was rejected outright.**
It would have reported `NO_LEASE` on every real run without ever pressing W.
The prologue test passed because the fake session did not enforce the evidence
rule — a fake laxer than the thing it stands in for, which is worse than no
fake at all. It enforces it now.

Steady-state navigation was never affected: `_translate_navigation` renews an
existing lease rather than releasing and re-acquiring, and the steering
follower explicitly keeps a turn key down across frames rather than re-pressing
it. The defect was entirely in the *probes*, which had no renewal because each
was a single command.

**No safety bound was loosened.** A renewal still requires a strictly newer
accepted frame, still revalidates focus, viewport, cancellation and capture
freshness, and is still capped by `max_rolling_lease_horizon_ms`; the deadman
lifts the key within one horizon of the last renewal. The hold is bounded twice
over — by the probe's own deadline and by that horizon — and released in a
`finally` on every path.

**A tightness this exposes and does not fix.** The per-command window is
`max_evidence_age_ms` minus the frame's age. With 45 ms of pipeline latency
that is ~55 ms, so a renewal chain only stays continuous while frames arrive
faster than that — about 18 fps, against a `qualify_min_fps` of 20. There is no
margin. Lengthening the lease would be weakening a safety bound and is
explicitly out of scope, so instead the *consequence* is counted:
`InputAuthority` records a `HOLD_LAPSED` event whenever a key is re-pressed
within 500 ms of coming up, and the drawer shows the tally. A hold that is
rattling now says so.

**Deviation:** the mission specified a 120-200 ms acceptance pulse. It is 320
ms, on the owner's report above and because the renewal chain is what makes a
press that long expressible at all.

---

## D-061 — 2026-08-28 — A journal entry may not take down the run it describes

**Decision:** `LifecycleJournal.note(stage, detail, /, **fields)` takes its
first two arguments positionally only.

**Why:** `note(stage, "focus:False", detail="Roblox is not frontmost")` raised
`TypeError: got multiple values for argument 'detail'`. It raised on the
*coordinator thread*, inside the handler for the very event it was recording,
so the coordinator's exception boundary safe-stopped the session — and the
session it stopped was the one trying to explain why a chord had been refused.
Journalling is diagnostics; diagnostics must not be able to end a run. It
happened twice in one afternoon, which is the definition of a shape rather than
a slip. Positional-only makes the collision land in `**fields` instead, and a
test asserts the field is kept rather than dropped.

**No deviation.** The plan does not specify the signature.

---

## D-062 — 2026-08-28 — One physical gesture, and it is the one the window names

**Plan text:** §3.4 specifies a one-use `LiveArmToken` created by a physical
**Arm Live** Tk callback and spent by a later hotkey, with a 30-second TTL.
§11.2 lists **Arm Live…** among the dashboard's buttons.

**Decision:** deviate. The separate arming click is removed — from the UI, from
`IntentType`, and from the preflight. `START_LIVE` from a genuine physical
chord now mints *and* consumes a `LiveArmToken` inside one coordinator
transaction.

**Why.** Four traces from a real session say the same thing four times:

```
chord_recognized      Ctrl+N
intent_queued         START_LIVE from hotkey
live.refused          no arm token
```

and nothing after it. No authorization, no LIVE transition, no `W` request, no
keyboard edge, no camera delta. The visible "nothing moves" never reached the
input backend at all, which rules out Quartz, focus, Roblox, camera calibration
and hold timing in one reading.

The cause was a gesture the window never mentioned. The message line read
*"Ready. Focus Roblox and press Ctrl+N to let the navigator move your
character"* while the coordinator required a click on a button the sentence did
not name. Both halves were individually defensible; together they were a
protocol only the source code stated.

Two gestures could have been kept and documented instead. That was rejected
because the second one buys nothing the first does not already have. Its stated
purpose was to make arming *deliberate and physical*, and the chord is both: it
is a modified key combination, pressed while Roblox is frontmost, that no
automatic setup path can produce. A second gesture whose only distinct property
is that the user might not know about it is not a safety feature.

**What replaced the token's guarantees, one for one:**

| The click gave | The chord gives |
|---|---|
| a gesture software cannot make | `PhysicalChordProof`, minted only by `ChordAuthority`, which is constructed by the coordinator and handed to exactly one hotkey listener. `source="hotkey"` is a label any caller can write; the nonce is not. |
| a one-use token | created and consumed between two statements, never stored. There is no window in which one exists to be replayed, which is *stronger* than a 30-second TTL. |
| run and generation binding | unchanged: the token still carries both, and the transition still bumps the generation. |
| readiness checked before conversion | unchanged: one `readiness()` snapshot, read once, before anything is minted. |
| a refusal that spent the token | a refusal that records `LIVE_REFUSED` with the reason, sets the coordinator's `live_refusal_detail`, and publishes a transition. The chord can simply be pressed again — which is what a user does anyway. |

**And the failure it was really made of, closed separately.** The window now
derives one `RunState` — SETTING UP, READY, STARTING LIVE, NAVIGATING, STOPPED,
BLOCKED — and both the header badge and the message line render it. READY is
read from `coordinator.blockers()` and `coordinator.live_authorization`, so
"the window says Ready and the coordinator refuses the chord" is a
contradiction rather than a combination nobody thought to look for.

**Also removed:** the preflight's `ARM_TOKEN` precondition, which asked whether
a button had been clicked. The id is kept and repointed at the question that
now matters — whether a chord will be heard at all — because a dead listener
makes every chord vanish with no symptom anywhere.

---

## D-063 — 2026-08-28 — A lease horizon is a safety bound, not a leftover

**Plan text:** §3.3 gives `max_rolling_lease_horizon_ms = 250` as the ceiling
on a rolling lease, and §4.5 requires renewal to be non-additive.

**Decision:** `_translate_navigation` grants exactly
`max_rolling_lease_horizon_ms`. It no longer derives the horizon from what
remains of the frame's evidence budget. A second bound,
`max_capture_stall_ms`, is added for the watchdog and for `_validate_for_press`.

**Why:** the horizon was
`min(250, (command.valid_until_s - now) * 1000)`, and `valid_until_s` is capped
at `captured_at_s + max_evidence_age_ms`. A command built from a 70 ms-old
frame therefore asked for a **30 ms** lease. The next frame arrived 33 ms
later, the lease had already expired, the watchdog lifted the key, and the
command built from *that* frame pressed it again. A hold meant to be continuous
came out as a rattle — and every individual step of it was correct, which is
why it survived so long. The plan's own 250 ms ceiling was never once reached.

D-060 found the same conflation in the probes and fixed it by chaining
renewals. This is the other half: renewing more often does not help if each
renewal is granted a window shorter than the gap to the next frame.

The two questions are genuinely different and now have different numbers:

* **evidence** — "may *this frame* authorize a new decision?" Still 100 ms,
  still enforced in `apply_navigation_command`, unchanged.
* **hold** — "must a key already down come up now?" 250 ms, equal to the
  plan's own horizon, applied to the lease and to the watchdog's capture
  staleness check.

`_validate_for_press` guards the *existence* of an edge — a new down, or a
renewal of one already down — so it uses the hold budget. Using the evidence
budget there meant one late frame both expired the lease and refused the
renewal that would have saved it.

**No safety bound was loosened.** A renewal still requires a strictly newer
accepted frame, still revalidates focus, viewport, cancellation, deadman health
and capture freshness, and is still capped. Nothing may hold a key without a
newer frame; what changed is only how long a hold survives *between* frames.
`max_capture_stall_ms` is a chosen bound with provenance, equal to a number the
plan already contains, not a measurement.

**Deviation:** none from the plan's values; the plan's ceiling is now the value
actually used.

---

## D-064 — 2026-08-28 — The one command that presses a key

**Decision:** `treasure.py --forward-probe [MS]` runs a bounded native movement
check. `IntentType.FORWARD_PROBE` runs as a SERVICE and is granted a navigation
session by name; `build_application` registers no worker for it, and only
`Application.enable_forward_probe` — called by that CLI mode and by nothing
else — wires one.

**Why:** every report of "it does not move" so far has been unfalsifiable. One
sentence covered a chord that never reached the coordinator, a coordinator that
never authorized Live, an edge the OS never registered, and a character walking
into a wall. Those are four different repairs, and no evidence in the
repository could tell them apart without a person watching the screen. The
probe walks the whole `NATIVE_MOTION_PATH` and prints the first stage that did
not happen, with the measured down-to-up duration beside the motion verdict.

**Why a SERVICE and not Live:** Live requires the physical chord, correctly,
and a CLI cannot press one. SERVICE already requires positive focus, fresh
capture, a running watchdog, a healthy deadman and an empty ledger — the same
readiness Live needs — and carries its own deadline. The only thing added is
the navigation session, granted for this one intent by name.

**Why the worker is not in the composition root:** so the gate is structural
rather than procedural. In the dashboard, and in every other process, the
intent resolves to "no worker" and cannot emit an edge, whatever submits it.
`tests/test_cli_lifecycle.py` asserts both halves: that `build_application`
registers nothing for it, and that only the CLI mode enables it.

**Deviation:** the plan has no CLI mode that emits input. This one does, and it
says so in its own first line of output.

---

## D-065 — 2026-08-28 — A faster pipeline was refused what a slower one was allowed

**Plan text:** §3.3 requires Live to be refused on an ineligible cadence.

**Decision:** `CadenceGovernor._live_age_ceiling_ms()` returns
`live_max_age_ms` outright. It was
`min(2000 / tier.fps, live_max_age_ms)`.

**Why.** Owner screenshot, dashboard reading **BLOCKED**, INPUT SAFETY
**Blocked - 1**, *"Capture cadence is not Live-eligible"* — with the same panel
reporting **56 unique fps**, **53 processed**, **p95 39 ms**. A healthy
pipeline, refused.

The arithmetic:

| tier | two intervals | effective ceiling | verdict on a 39 ms p95 |
|---|---|---|---|
| 30 Hz | 66.7 ms | 66.7 ms | **eligible** |
| 60 Hz | 33.3 ms | 33.3 ms | refused |
| 90 Hz | 22.2 ms | 22.2 ms | refused |
| 120 Hz | 16.7 ms | 16.7 ms | refused |

The gate got *tighter the faster the pipeline ran*. The owner's machine was
refused at 60 Hz for a frame age that the identical machine, throttled to 30
Hz, would have passed with 27 ms to spare. `live_max_age_ms` was also
unreachable above 26 Hz — `min()` never chose it — so a configured bound had
never once applied.

Two questions were conflated. *"Is this pipeline keeping up with its own
cadence?"* is a cadence question, and it is already answered twice:
`live_min_processed_ratio` in the same predicate, and the
p95-against-`max_frame_age_ms` check in `update` that downshifts the tier
outright. *"Is this evidence fresh enough to steer on?"* is a freshness
question, and its answer cannot depend on how many frames per second the
camera was asked for. A 39 ms-old frame is exactly as stale either way.

**No bound was loosened where it matters.** 75 ms remains below the input
authority's `max_evidence_age_ms` of 100, so Live is still the stricter of the
two gates — which is the relationship that was intended and, above 26 Hz, was
the one thing the old form did guarantee.

---

## D-066 — 2026-08-28 — The same disagreement, running the other way

**Decision:** `_on_start_live` refuses when `metrics.live_eligible` is false,
with the governor's own reason.

**Why:** the dashboard showed **BLOCKED — CADENCE** while `_on_start_live`
checked only `Readiness`, which has no cadence term. So the window said one
thing and the coordinator would have done another — D-062 exactly, with the
polarity reversed: this time the window was the stricter of the two, and a
chord pressed against its advice would have started Live on a pipeline the
window had just called not ready.

It is checked in `_on_start_live` rather than added to `Readiness` because
`Readiness` also gates the bounded services, and a dig loop does not need
steering cadence.

**Found while fixing it, and worth more than the fix:** every coordinator test
rig was starting Live past this gate, because a fake capture source never
produces the measurement window the governor needs. The gate was real in
production and absent under test — "a fake laxer than the thing it stands in
for", the same shape D-060 recorded. `settle_cadence_for_live` now drives the
**production** `CadenceGovernor.update` with healthy polls until it reports
eligible, so the rigs satisfy the gate by measurement rather than skipping it.
A test that wants the gate to fail simply does not call it.

---

## D-067 — 2026-08-29 — The movement path could not press a key, and was replaced

**Plan text:** §4.3 makes `apply_navigation_command` the sole navigation input
path, validating an authority-issued `EvidenceToken` before translating axes
into bounded leases.

**Decision:** deviate. That method and `_translate_navigation` are deleted. The
navigation path is now `prospector_engine/movement.py` — a level-triggered
actuator owned by the input authority, modelled on Prospector Lite's proven
`input_lease` contract.

**The measurement that came first.** The owner's report was that Lite works on
this machine, a colleague's branch works on this machine, and this did not move
at all. Posting an inert keycode through Treasure's *own* `platform_mac._post`,
on that machine, with Roblox frontmost:

```
before   : F13 down = False
key_down : F13 down = True
key_up   : F13 down = False
Accessibility (event posting) trusted: True
```

The poster works. The keycodes are byte-identical to Lite's. And
`OS_EDGE_POSTED` — recorded the instant `CGEventPost` returns — never once
appeared in any runtime trace. Nothing was ever calling it.

**Two permanent latches, both reproduced against the real classes.**

1. `InputAuthority.release_all` set `_admission_open = False`. That flag is set
   True in exactly one place, `activate_generation`, which only the coordinator
   calls and only on a mode transition — there is no re-open inside a running
   session. And `release_navigation` was hard-wired to `release_all`, so the
   *first* ordinary "stop walking" muted the session for the rest of the run.
   Reproduced: acquire W ok → `release_navigation` → acquire W refused
   `admission-closed`, with focus ok, viewport ok, capture 10 ms old, helper ok
   and ledger empty.

2. `DeadmanHelper.release_all` incremented its generation, and `_register`
   refuses any generation below its own. After the first release the helper sat
   one ahead of the parent forever, so every registration returned
   `stale-generation` — and the parent will not post a down edge without a
   positive ACK. A second, fully independent, permanent mute.

**And both were tripped on the success path of every healthy start.**
`make_live_worker` called `session.release_navigation("prologue-complete")`
three lines before the navigation loop read its first frame. Two more gates
fired on frame one for good measure: the processed-rate counter reads 0.0 until
it holds two stamps and the loop read it before ticking it, and `cursor_safe`
treated an unreadable pointer as unsafe while the pointer was sitting on the
dashboard where the user had just clicked Start Navigator.

**What the rebuild keeps.** Every property that is about a person or a running
game, expressed as a condition that *releases* rather than one that refuses a
press, checked by an independent watchdog thread: Stop releases everything from
any state; another window in front releases everything; a hold has a ceiling; a
worker that stops calling loses the keys; an unhealthy helper refuses new
presses; process death releases out of process; a release is never focus-gated
and always sweeps the whole vocabulary. `release_all` still disarms, so a Stop
racing a press still stops it — that property was briefly lost during this work
and `tests/test_stop_safety.py` caught it.

**What it drops.** Evidence tokens on the press path, per-press generation and
viewport-identity matching, the strictly-newer-frame rule, and any coupling
between the *existence* of an edge and the age of a frame. A key already down
does not become dangerous because the next screenshot was slow; it becomes
dangerous when nobody is watching it, which is what the heartbeat and the hold
ceiling are for.

**The asymmetry that runs through all of it.** `if focus is not True: refuse`
turned every ambiguous `CGWindowList` scan into a refused keypress, because that
probe returns `None` on any error. Lite refuses only on a positive "another app
is in front" and has driven a character for months that way. The same mistake
was in `_cursor_safe`. Refusing to press on "I do not know" makes a macro that
cannot move; releasing on "I do not know" would make one that cannot be
trusted — so releases are never gated on either reading. Both directions are
now pinned by tests.

---

## D-068 — 2026-08-29 — A log the owner can read

**Decision:** `prospector_engine/plainlog.py`, rendered in a panel of its own
between the preview and the Advanced disclosure.

**Why:** there were sixteen lifecycle stages, a governor trace and two event
rings, and none of them answered *"why is nothing moving"* in a sentence. It is
not in the diagnostics drawer on purpose: the drawer is the engineering half,
and it only renders while expanded, so a log living there is a log nobody reads
at the moment they need it.

Three rules keep it legible while a character is walking: one line per change
rather than per frame; per-frame topics rate-capped, and collapsing only a
sentence that is a *variation* of the one already there, judged on its first
word — "Facing 40 degrees" to "Facing 45 degrees" costs no line, "Holding W" to
"Released W" keeps both; and every failure ending in one physical action, so
that a line with no action is information rather than red.

Not cleared on Stop — reading back why the last run ended is most of what it is
for. Only Start Navigator clears it, and the whole story is exported with the
stop trace beside the numbers.

---

## D-069 — 2026-08-29 — The prologue stopped disarming the thing it proves works

**Decision:** `_LiveControlPort.release_forward` and `.release_turn` call
`NavigationInputSession.stop_moving`, never `release_navigation`. The full
release floor is reserved for explicit Stop, worker terminal exit, a terminal
safety fault, process shutdown, and an unrecoverable release failure.

**Why:** D-067 removed the twelve-condition admission test and rebuilt movement
on a level-triggered actuator, and it named this exact class of bug — an
ordinary success running the terminal release floor — as the thing it was
fixing. It then left four instances of it in the one code path Live runs first.
`stop-epoch4-1914449166.jsonl` records all four in eight milliseconds, on the
success path:

    0.834  ledger_empty     navigation:acceptance-probe-complete
    0.835  ledger_empty     navigation:acceptance-complete
    0.835  ledger_empty     navigation:prologue-complete
    0.836  ledger_empty     worker:...

Each reaches `InputAuthority.release_all`, which sets `_admission_open = False`
and calls `MovementActuator.disarm`. Admission reopens in exactly one place,
`activate_generation`, and only on a mode transition. So by the time the
prologue returned *"the camera turns, go and navigate"*, the actuator that
would do the navigating had been disarmed by the probe that proved it worked.

**Why the tests did not see it:** `tests/test_live_prologue.py` fakes
`release_navigation` as "lift the keys" — the actuator's `release_all`, which
does not clear `_armed`. A fake laxer than the thing it stands in for reports a
healthy run for a path that has muted the session for good. The regressions in
`tests/test_navigation_lifecycle.py` use the real `InputAuthority` and the real
`NavigationInputSession` and assert on a `W` down edge reaching the port.

## D-070 — 2026-08-29 — Cadence is telemetry, not permission

**Decision:** `metrics.live_eligible` is gone from `_on_start_live`. The
`CADENCE` blocker keeps its row and takes the new `advisory` status, and
`LiveBlocker.blocking` — not a status string compared in two modules — is what
callers filter on. `SteeringLimits.min_processed_fps` no longer releases; it
produces a `ControlDecision.advisories` entry. Live's entry gate is the age of
the latest frame, which `readiness()` already measured.

**Why:** a governor tier is an adaptive scheduling decision about how hard to
drive the capture source. It describes the recent past, it is reset by every
source, geometry and profile change, and it was never a statement about whether
the picture in front of the operator is usable *now*. Used as an authorization
it produced, in the owner's traces:

    live_refused  cadence:stable at 60 Hz
    live_refused  cadence:cooldown at 30 Hz     (x7, frames arriving 24 ms apart)
    live_refused  cadence:capture started

A status whose own name is "stable" refusing a keypress is not a threshold that
needs tuning; it is the wrong quantity. A 20 fps pipeline delivering 50 ms-old
frames is slower than we would like and is not dangerous. A 60 fps pipeline
whose last frame is 400 ms old is dangerous, and frame age already catches it.

Cadence still adapts and is still shown — as a `WARN` line naming the tier and
saying it is not blocking.

## D-071 — 2026-08-29 — `CGEventSourceKeyState` is diagnostic, and it was racing

**Decision:** the loopback read never decides an outcome. `NO_LOOPBACK` is
retired as a verdict (the enum member is kept so an archived report parses) and
`OS_EDGE_LOOPBACK_OBSERVED` is off `FORWARD_PULSE_PATH`. The read is taken once,
`AcceptanceConfig.loopback_delay_ms` (80 ms) after the down edge rather than in
the same breath as the post, and it may narrow the *advice* attached to a
failure, never the failure.

**Why, measured here:** in `stop-epoch4-1914449166.jsonl` the sequence is

    0.512  os_edge_posted            w   posted=True
    0.512  os_edge_loopback_missing  loopback=False
    0.522  post_edge_frame_observed  frame 8979, 4 ms after the edge
    0.834  w_hold_confirmed          forward was down for 322.7 ms

`W` was demonstrably held for a third of a second and six frames were captured
after the edge. The run was failed on a read taken microseconds after the post,
before any of those six frames were examined. Re-measured tonight through
`--native-control-probe` with an 80 ms delay, on this machine, against the real
client: `loopback=True` on both event taps, repeatedly. The immediate read was a
timing artefact, and the verdict it produced was about scheduling rather than
about Roblox.

`CGEventPostToPid` reports `loopback=False` by construction — it bypasses the
window server's key state — so retaining the gate would also have disqualified a
backend before it was measured.

## D-072 — 2026-08-29 — One safety lifecycle; unknown focus is not focus loss

**Decision:** `InputAuthority.poll_safety` no longer raises a releasing fault
for `focus is None`; it counts the reading (`unknown_focus_polls`). A positive
`False` still releases immediately and unconditionally. `SafetyFaultKind`
keeps `FOCUS_UNKNOWN` for anything that positively knows better.
`build_application` now submits every safety fault to
`RuntimeCoordinator.submit_fault`.

**Why:** two objects owned the same word and disagreed about it. macOS's
frontmost probe is a `CGWindowList` scan that answers `None` on any error or
ambiguity. `MovementActuator._blocking_condition` treats that as "carry on",
deliberately and with the whole of D-067 behind it. `poll_safety` treated it as
terminal, ran the full release floor, and disarmed that same actuator. Whichever
ran first won, which is a race and not a safety property.

The second half is worse. The fault callback only wrote an event-log line, so
the release happened and the object that owns `RunMode` was never told: the
header went on saying LIVE over a runtime whose actuator was disarmed and whose
next command could only answer *"the navigator is stopped"*. A zombie Live is
worse than a stop, because nothing on screen tells the person to press anything.

## D-073 — 2026-08-29 — A session generation is checked; a frame's evidence is not

**Decision:** `NavigationInputSession.move` and `.stop_moving` refuse when the
session's generation is not the authority's current one, and
`InputAuthority.release_navigation` refuses a superseded generation instead of
`del generation`.

**Why:** the argument was accepted and thrown away, so a straggling worker's
`finally: release_navigation()` ran the whole release floor against whatever
mode was running by then — Shadow blocks inside a native screen grab, the
coordinator cancels it, joins for its bounded deadline, gives up, starts Live;
the straggler wakes, unwinds, and disarms the Live actuator that was just armed.
Nothing in the trace said which worker did it, because the call had already
forgotten which worker it came from.

**Why this is not the machinery D-067 removed.** That validated an evidence
token by object identity and then ten properties of the *frame* — sequence,
capture instant, capture duration, two age budgets, viewport identity — per key,
per tick, and a healthy 55 fps pipeline could not pass it. This is one integer
comparison about the *session*, and its answer changes only on a mode
transition. It cannot refuse a press for being late.

## D-074 — 2026-08-29 — A native diagnostic that looks at the game

**Decision:** `prospector_engine/nativeprobe.py` and
`treasure.py --native-control-probe`. Passing the flag is the authorization. It
builds no application, no coordinator, no input authority and no deadman, needs
no profile, no map and no automatic setup, verifies Roblox is the positively
identified frontmost window, posts one edge per trial, measures the frames, and
releases in a `finally` inside a whole-vocabulary sweep.

**Why:** every earlier claim rested on evidence that cannot support it.
`CGEventPost` returning proves the call did not raise. An inert keycode (F13)
read back through `CGEventSourceKeyState` proves this process can reach
WindowServer. A unit test with a fake port proves the code is wired the way the
test wired it. None of the three is a claim about Roblox, and a keyboard trial
alone cannot separate *"our events never arrive"* from *"the game has nothing to
move"* — so the probe also carries a scroll trial, which a Roblox window will
visibly answer in any state.

**Measured on this machine, 2026-08-29, against the real client** (home page,
no character in a world), `--hold-ms 600`, two trials each, vertical scene
translation by phase correlation:

| backend                          | scroll response | W loopback at +80 ms |
|----------------------------------|-----------------|----------------------|
| `CGEventPost(kCGHIDEventTap)`    | −140.4 px       | `True`               |
| `CGEventPost(kCGSessionEventTap)`| −172.9 px       | `True`               |
| `CGEventPostToPid(<roblox pid>)` | −0.6 px         | `False`              |

Alternating hid against pid, four rounds, both directions: hid moved the page in
7 of 8 trials (the miss was at a scroll limit), pid in 0 of 8, median |dy| 229.6
against 0.7 px. **`CGEventPostToPid` does not reach Roblox at all**; both event
taps do. Production keeps `hid`, and the ladder is kept so the choice stays a
measurement.

**Not proven by this:** that `W` moves a character. There was no character —
the client sat on its home page all night. Scroll is routed by hit-testing and
keys by focus, and only a character walking settles the keyboard question.

**Two still windows, not one.** A key trial measures the scene before the edge
*and again after the key comes up*, and the threshold is a multiple of the
larger. The first version used only the "before" window and reported a false
`MOVED` for `W` on the home page — `mad 5.96` against an idle of exactly 0.00,
which is a row of thumbnails lazily loading part-way through the hold beating
an absolute floor of 0.45. Whatever is changing on its own is still changing a
moment later; a character that was walking has stopped. With the control window
the same trial reports `during 0.00, after 0.00` and no motion, which is the
truth about a home page.

## D-075 — 2026-08-29 — `cv2.phaseCorrelate` mutates its arguments

**Decision:** `measure_scene` passes copies.

**Why:** OpenCV multiplies both inputs by the Hanning window **in place**. Every
frame in a run is the `current` of one pair and the `previous` of the next, so
without copies the second comparison of every pair is a windowed image against a
raw one. A run of six *byte-identical* frames reported a mean absolute
difference of 40.9 out of 255. It was caught by a synthetic test asserting that
a still scene reads as still, and it had already contaminated the first night's
probe output: the Roblox home page appeared to have an idle noise floor of
28-37, and measures 0.00 once the copies are in.

## D-076 — 2026-08-29 — The actuator's answer never reached the navigator

**Decision:** `Navigator.note_held(held, *, now_s, yaw_posted_px, held_ms)`
replaces `note_applied`, and the live worker calls it on **every** path —
applied, blocked, released — with what the `MovementActuator` reports it is
physically holding.

**Why:** the line did not exist. `make_live_worker` called
`session.apply_command(...)` and told the navigator nothing, so for the whole
of every Live session ever run on this machine:

* `ForwardCommandLedger` stayed empty, so `held_continuously_for` was `0.0` on
  every frame;
* `RuntimeBaselineEstimator.observe` refused every sample, because its gate is
  `held_ms >= 250`, so no session ever measured a walking speed;
* `ProgressGuard` saw `holding() == False` forever and returned `UNKNOWN`;
* and obstacle recovery, all of which is downstream of that, could not
  activate — while looking, from the outside, like working code.

Every test passed because every test called `note_applied` by hand.
`tests/test_live_feedback.py` exists so that cannot recur: it drives the real
`make_live_worker`, the real `InputAuthority`, the real `MovementActuator` and
a real `CaptureService`, primes nothing, and asserts the ledger fills, the
baseline matures from ordinary walking, and a simulated stall reaches recovery
on its own.

**A second bug the same test found.** The rig kept every published
`DiagnosticObservation`. Each holds its own `CapturedFrame`, and a frame holds
a buffer out of the capture pool, so the pipeline stopped dead after eight
frames with `pool exhausted`. Production consumers keep the latest packet only,
for exactly this reason.

## D-077 — 2026-08-29 — Correcting a heading is not a reason to stand still

**Decision:** `ArrowFollowerController` is a continuous-pursuit controller.
Nine `ControlState` values, five of which walk. Forward and steering are
independent outputs; a heading error selects how hard to correct *while
walking*, and only an error past `strong_band_deg`, sustained for
`pivot_confirm_s`, stops the character at all.

**Why:** the previous policy required three consecutive frames inside an
eight-degree cone before `W` could go down, dropped `W` the moment the error
left that cone, and then turned on the spot and waited out the actuator's own
latency. That latency was measured on this machine at **322–364 ms**, so an
ordinary curve cost a stop, a turn, a wait and a fresh start, several times a
second. The controller was doing exactly what it was written to do; the mistake
was treating *currently correcting a heading* as *must stand still*.

**What the rewrite is measured against.** `tests/test_routes.py` drives the
real navigator through a world that delays every commanded rotation by the
measured 340 ms, at 30/60/90/120 Hz:

| property | result |
|---|---|
| 30 s straight route | 1 W-down edge, 0 W-up edges, every cadence |
| ordinary route duty cycle | ≥ 90 % |
| occlusion 100 ms / 500 ms / 1 s / 2 s | 0 forward releases, every cadence |
| alignment after the opening turn | p50 < 10°, p95 < 25° |
| open-ground false-stuck rate | < 1 % of ticks |

**Two things that fell out of measuring it.**

*Lead compensation may shrink a correction and may never reverse one.* The
angular rate cannot tell the target's own motion apart from the rotation this
controller just commanded, so right after a large correction it reads as though
the error is about to shoot past zero. Allowed to change the sign it turned
24° right and then immediately asked for 4° left, having moved nothing in
between.

*The derivative gain is gone.* The lead term **is** derivative action, and
running both was double compensation: a settled 24° error with a large negative
rate produced `0.6 × 2 − 0.12 × 90`, a command pointing the wrong way. The sign
of a correction now comes from the error and from nothing else.

`TurnLimits.max_correction_deg` rises 14 → 30. The old ceiling was set for a
controller that stood still to turn, where a small step costs only time; one
that walks through its own correction has to out-turn the curve it is walking.

## D-078 — 2026-08-29 — Occlusion is not obstruction

**Decision:** losing the arrow while moving enters `COAST` (keep walking on the
remembered heading, bleed the correction to neutral over `coast_decay_s`), then
`SEARCH` (keep walking, shallow alternating legs biased toward the last known
side, one correction per leg), then abandons inside `search_budget_s`. Physical
contact is a separate question answered from motion, never from arrow
visibility.

**Why:** traces in this repository hold healthy arrow losses of **0.7 to 2.65
seconds** behind foliage, and the previous grace only applied when the state
enum was exactly `FOLLOW` — every other path released. A rejected *heading* is
the commoner case than a missing arrow and had no grace at all.

**A bug found while measuring it.** The first search planned a fresh turn on
every frame of its 400 ms window, spending the whole episode's 200° rotation
budget in a quarter of a second and abandoning before it had looked anywhere.
One correction per leg, honoured through the existing one-in-flight machinery.

**Memory horizons are now synchronized.** `HeadingConfig.max_age_s` is derived
from `SteeringLimits`, `ArrowTracker` holds identity on the monotonic clock
rather than for five frames (83 ms at 60 Hz, against a two-second coast), and
`DetectorConfig.lost_after_s` at 3.0 s already exceeded the coast grace.

## D-079 — 2026-08-29 — Confirm contact while the character is still being told to walk

**Decision:** `ProgressGuard` gathers its confirming samples with forward still
held, and `ProgressVerdict.recover` replaces `release_forward`. Recovery
permission moved from `NavigationCapabilities` to the guard: the capability says
whether maneuvering is allowed, the guard says whether there is evidence now.

**Why:** the guard released `W` on a *suspicion* and confirmed from four
stationary frames. Both halves were wrong — every ambiguous stretch of
low-texture ground cost a stop, and the confirming frames were taken from a
character that had already been told to stand still, which does not answer
"can this character move".

**The relative fallback.** The runtime baseline only arrives after twelve clean
frames of unobstructed walking, so a route that met a bush in its first seconds
had obstacle detection switched off entirely and simply stopped. There is now a
strictly weaker, strictly local test: has this character's own speed collapsed
against what it was doing a moment ago. It needs ≥ 10 samples spanning ≥ 1.2 s
before it will answer, uses a harsher fraction (0.25 against 0.35) because its
reference is unproven, and marks every verdict `provisional`.

## D-080 — 2026-08-29 — Composite recovery maneuvers, and six caps on them

**Decision:** the ladder is R0 running hop (W+SPACE, then W), R1 sticky forward
arc (W+side+camera), R2 opposite running hop, R3 back out and redirect, R4 wider
detour arc. Side selection reads `TraversabilityMemory` first, the heading
second, observed lateral slip third, the failed side last.

**Why:** the old ladder opened with two rungs that released the controls and
waited — 700 ms of standing still — and then offered `A` alone, `W` alone and
`SPACE` alone. Nobody gets past a bush that way: a running jump needs `W` and
`SPACE` together, and a hedge needs a forward *arc*.

Six caps rather than one, because there are six ways an episode goes wrong:
total time, total input, jumps, reverse duration, side flips, and a jump
cooldown so `SPACE` is a jump rather than a held space bar. Success is
`restore_frames` consecutive frames of restored motion, not one — a single
frame of movement during a maneuver is what a maneuver *produces*, the
character sliding along the obstacle, and calling that success is how a ladder
resolves straight back into the same wall.

**Transitions no longer use the release floor.** Moving between pursuit and
recovery preserves the heading filter, the target identity and the sector
memory; only a fault, a terminal state or a changed world resets anything.

**A bug the route simulations found.** The frame absorbed during recovery was
also being *consumed*, so the tick that resolved an episode produced a hold
with nothing to hold and the character stopped for one frame every time it got
past something. Absorbing is not deciding; it no longer consumes.

## D-081 — 2026-08-29 — A trace for every run, not only for the one somebody stopped

**Decision:** `_export_trace` fires on a safe stop, a worker completion and a
fault as well as on Stop, and a run writes exactly one; Stop still forces one
regardless. Navigation state reaches the readable log as a typed
`PursuitTelemetry`, written only when the state, the held keys, the recovery
rung or an escalation changed.

**Why:** traces were written from exactly one place, the Stop intent handler.
The single most informative session this project has had — the first in which
the character actually moved — ended some other way, and there is no file for
it. And the navigator's state reached the dashboard as free-form status strings
in the engineering ring, where nothing could filter, rank or render them.

`held_keys` comes from the actuator's ledger and `wanted_keys` from the
controller, so when the two differ something refused a press. No earlier
version of this could show that.

## D-082 — 2026-08-29 — Measure the occlusion the grace is chosen against

**Decision:** `--shadow-bench` reports arrow readability and the distribution of
*closed* unreadable gaps against the live client, with the counts that fall
inside `coast_grace_s` and past `search_budget_s`. It needs no calibration,
holds no authority and sends nothing.

**Why:** `coast_grace_s = 2.0` and `search_budget_s = 9.0` are provisional
configuration chosen from arrow-loss intervals in older traces. This measures
the same quantity from live frames so the choice can be checked rather than
believed — and it is the one piece of native evidence that does *not* require
the owner to arm Live.

**Measured 2026-08-29, 460 and 1447 frames, capture healthy at 57.5–57.9 fps:**
the arrow was never readable, for the whole of both runs. No treasure map was
equipped; the client sat idle. The instrument works and the conditions did not
exist, which is a different statement from a passing measurement and is
recorded as such. A gap still open when the bench ends is reported separately
and never counted as a closed one, because its length is unknown.
