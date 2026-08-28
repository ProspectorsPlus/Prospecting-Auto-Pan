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
