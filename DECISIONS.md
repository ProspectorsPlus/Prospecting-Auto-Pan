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
