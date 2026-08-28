# Treasure Navigator

An arrow-guided Roblox *Treasure* macro: pin the client area, watch the
equipped treasure map's arrow, walk and turn toward it under closed-loop
control, confirm arrival, run the bounded dig / pan-swap services, and stop
safely.

`TREASURE_NAVIGATION_PLAN.md` is the authoritative architecture and evidence
specification. `CLAUDE.md` is the short operating contract. `STATUS.md` is the
honest per-gate status. `DECISIONS.md` records local implementation decisions.

`complexion.md` is **historical and stale** — it describes a different
repository state and is not architecture truth.

---

## What actually works today

| Capability | Status |
|---|---|
| Explicit coordinate spaces (logical / backing / canonical) | implemented and tested |
| Window-specific capture at 57–110 unique fps | implemented; **ScreenCaptureKit** on macOS |
| Connect to the Roblox client without resizing it | implemented and tested |
| Optional bounded *Fit & Verify Viewport* state machine | implemented; one measured `canonical_verified` on this Mac (2x, one display); **E-VIEW matrix pending** |
| One coherent stamped frame per decision, keyed by run/generation/revision | implemented |
| Per-frame diagnostic packet, Minimal and Full Diagnostics overlays | implemented |
| Single input authority, leases, watchdog, out-of-process deadman | implemented; 10 000 stop races clean |
| Bounded dig / dequip / pan-swap / reset services | implemented; behavior characterized against the previous build |
| Standalone dig loop (**F6**) | implemented; **pixels pending reverification** |
| Shadow observation, telemetry, evidence recorder, dashboard | implemented |
| Arrow detection by shape and local contrast, one temporal transaction per frame | implemented; **measured on a real-frame corpus** (eval: recall 80 %, 0 false locks, 0 identity switches); **E-PROF pending** |
| Signed direction from barb asymmetry, notch line, tip and axis, with reversal hysteresis | implemented; eval sign accuracy 91 %, median 10 deg; **E-DIR-E2E pending** |
| Real-frame corpus, split by contiguous sequence, with a bbox-aware evaluator and a regression gate | implemented (`tests/corpus/real`, `prospector_engine/corpus.py`) |
| Bounded per-frame trace (capture, scheduling, ROI/full detector, direction, preview, governor) | implemented; exported as JSONL on Stop and by `--shadow-bench` |
| Offline stratified evaluator with per-stratum confidence bounds | implemented (rendered stress only) |
| Cadence governor (WARMUP/STABLE/PROBE/COOLDOWN/DEGRADED), judged on a recent window, recovering from DEGRADED | implemented and tested; native: 57 of 57 unique fps with perception at Balanced 60 |
| Guided commissioning window with keyed, scoped blockers | implemented; every evidence step stays PENDING until its physical procedure |
| Shift-Lock steering controller and yaw calibration contract | implemented; **refuses to steer**, no calibration exists |
| Conservative progress guard (may say "stop", never "go around") | implemented; **E-MOTION pending** |
| Live navigation (steering) | **refuses to run** — it names the pending experiments instead |
| Obstacle recovery / 2.5D terrain grid | **not built** — only the input contract for it exists |
| Automatic arrival | **disabled** — needs E-ARRIVE |
| Automatic profile classification | **disabled** — selection is explicit |
| Automatic next map | **disabled** — needs E-NEXT_MAP |

That table is the point of the project, not an apology for it. Each feature is
enabled only for the exact OS, arrow profile, and condition whose evidence gate
passed, and no gate has been run yet. See `STATUS.md`.

## What Shadow shows you

Start Shadow and the SHADOW VIEW draws, over the live frame it actually
decided from:

* the **assumed player-forward arm** — dashed and labelled, because E-ANCHOR
  and E-FORWARD have not been run and this is a hypothesis you are being shown,
  not a measurement;
* the **desired map-arrow arm** and the **signed turn** between them, with the
  angle in degrees;
* a thin arm per **direction cue**, so when the fused cue abstains because its
  components disagree you can see by how much;
* the detected arrow's **contour, box, centroid and tip**, plus dashed boxes for
  candidates that were **rejected**, and why;
* a caption with confidence, profile status, abstention reason, and per-stage
  timings.

Everything in that picture comes from one `DiagnosticObservation`, which holds
the frame itself — so the overlay can never be drawn from one frame over the
image of another.

## Why the arrow is found by shape rather than colour

On the green map the arrow's green chromaticity is **0.518** and the grass
behind it is **0.520**. No colour rule can separate them, and the previous
detector — which ranked candidates by area and scored confidence by how close
that area was to the middle of an allowed range — promoted the grass.

Colour now only *proposes*. What decides is a weighted set of independent
terms — local contrast (the one soft veto: every measured view of the arrow is
brighter than what is behind it), the two-notch signature, the barbs beyond
the notch line, a prominent tip, the outline bands, a locally-measured
boundary — and an explicit temporal machine: an identity is earned over
consistent frames, held through brief loss, challenged but never replaced in
one frame by a distractor, and abstained on ambiguity. Direction is a
weighted vote of independent polarity cues led by the barb asymmetry, which
survives perspective and a hidden shaft end. See `DECISIONS.md` D-024, D-030.

The real-frame corpus in `tests/corpus/real` is what the detector is judged
on. Run `treasure.py --detector-report --corpus tests/corpus/real --json out.json`
for the per-sequence and per-stratum numbers with their counts, and
`treasure.py --detector-report` for the rendered stress strata.

## Coordinate spaces

Four, and they are never interchangeable:

| Space | Units | Who speaks it |
|---|---|---|
| `DISPLAY_LOGICAL` | macOS points; Windows device pixels under Per-Monitor V2 | window APIs, Accessibility, `mss` |
| `CLIENT_LOGICAL` | same, relative to the client's top-left | pin requests, capture crops |
| `CLIENT_BACKING` | native device pixels | what a Retina capture contains |
| `CANONICAL` | fixed 1280×720 | every detector, calibrated pixel, and overlay coordinate |

Handing a device-pixel rectangle to an API that wants logical units is what
made the capture return the desktop instead of the game. `Affine2D` carries its
source and target space and refuses to compose mismatched ones, so that class
of bug no longer type-checks.

## The dig / pan-swap pixels are pending reverification

The calibrated coordinates were derived against the old macOS *window-frame*
basis with the frame pinned to 1280×720. The canonical viewport pins the
**client** to 1280×720, which moves the origin *and* changes the size of the
game viewport. The values are carried over unchanged and marked `PENDING`
everywhere they surface. Re-derive them before any unattended run:

```sh
.venv/bin/python treasure.py --calibrate
```

It reports in the canonical client basis, so the numbers paste straight into
`TreasurePixels`.

---

## Setup (macOS)

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-macos.lock
.venv/bin/python -m pip install --no-deps -e .
```

Grant **Accessibility** and **Screen Recording** to whichever process launches
Python. Without them, window pinning and capture fail with a clear message
rather than degrading silently.

Windows: `requirements-windows.lock` is deliberately unpopulated — generate it
on Windows (see that file's header). No code in `platform_win.py` has ever run
on Windows.

## Running

```sh
.venv/bin/python treasure.py              # dashboard
.venv/bin/python treasure.py --self-test  # imports and contracts, emits no input
.venv/bin/python treasure.py --smoke-test # packaging smoke test, emits no input
.venv/bin/python treasure.py --calibrate  # client-relative pixel read-out
.venv/bin/python treasure.py --capture-probe   # measure the pipeline, read-only
.venv/bin/python treasure.py --replay <session-dir>  # replay a recording
.venv/bin/python treasure.py --detector-report --corpus tests/corpus/real  # real-frame report
.venv/bin/python treasure.py --shadow-bench 15 --json bench.json  # native capture + perception
.venv/bin/python treasure.py --soak 10        # bounded synthetic soak
```

Every offline mode is mutually exclusive and bounded: no dashboard, no input
authority, no deadman helper, a report, an exit status.
`tests/test_cli_lifecycle.py` runs them as subprocesses and checks exactly
that. `--shadow-bench` needs a Roblox window on screen; it moves nothing and
sends nothing.

`--replay` runs a recorded session back through the real perception pipeline
and navigator. There is no input authority and no platform port anywhere in
that path, so it cannot emit anything; it uses recorded timestamps, so a slow
replay does not make every frame look stale.

### Live mode requires a physical human action

There is no way to start Live from software. The sequence is:

1. Physically click **Arm Live…** in the dashboard. This creates a one-use,
   30-second, process-run-bound token that is never persisted.
2. Physically focus Roblox.
3. Press **F1**.

Clicking Tk removes focus from Roblox, so there is no actionable *Start Live*,
*Reset*, or *Pan Test* button — those are guidance labels, and the hotkeys
carry the intent while Roblox is positively focused. A failed readiness check
spends the token: you re-arm.

Hotkeys (all require Roblox to be positively focused, except **F2**, which
always works): **F1** start Live (armed) · **F2** stop · **F3** pixel probe ·
**F4** reset character · **F5** pan-swap test · **F6** dig loop.

**F6** is the pre-navigator dig loop, rebuilt on the bounded services: tap
while both terrain check points match, pan-swap when the capacity bar reads
full, stop on anything else — now with an attempt cap, a deadline, and instant
cancellation. Re-derive the pixels with `--calibrate` first, or it will
correctly report `CUE_LOST` and do nothing.

## Development

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check . && .venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy prospector_engine treasure.py treasure_gui.py deadman.py
```

No test emits real OS input. Tests that would need to are marked `native` and
are not part of the default run.

## Safety model in one paragraph

One process-wide input authority owns the only held-key ledger and is the only
caller of a platform port. Every held input has a monotonic lease, an
independent watchdog, and a registration with an out-of-process helper that can
release inputs but has no code path to press one. Stop, focus loss, an invalid
viewport, stale evidence, an uncaught error, a mode transfer, and shutdown all
release everything, unconditionally and without consulting focus. If any part of
a release fails, uncertainty is latched and every input-emitting mode is refused
until an explicit release-only recovery succeeds.

## Distribution

**Not distributable yet.** `G-LICENSE` (plan §15) is unresolved: the owner must
confirm first-party reuse rights and choose Treasure's terms before anything is
published. `pyproject.toml` says `UNLICENSED` and builds are private/internal
only. Nothing here may be described as open source.
