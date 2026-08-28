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

## The normal flow

1. Open Roblox in **windowed** mode with a treasure map equipped.
2. Press **Start Navigator**.
3. The application finds Roblox, sizes its client to 1280x720 and reads back
   what it actually got, rebinds capture, identifies which map you have
   equipped from consecutive frames, checks that the direction to the arrow
   holds still, qualifies the whole read-only pipeline, and starts observing.
4. To let it move your character: press **Arm Live** (enabled once setup is
   READY and nothing is blocking), focus Roblox, and press **F1**.
   The first two seconds under the arm are stationary - it confirms the camera
   control mode and measures how far your camera turns per unit of input - and
   then it aligns, holds `W`, and follows the arrow.
5. **Stop & Release All Input** (or **F2**) is available at every moment.

There is no pixel clicking, no numeric yaw entry, no sensitivity setting, no
corpus to record, no deadband trial and no anchor to label. The only thing that
can interrupt the flow is an OS permission dialog, and when one does the
navigator stops with the exact sentence that says what to enable.

## What actually works today

| Capability | Status |
|---|---|
| Explicit coordinate spaces (logical / backing / canonical) | implemented and tested |
| Window-specific capture at 57-110 unique fps | implemented; **ScreenCaptureKit** on macOS |
| **Automatic setup**: find Roblox, size the window, rebind capture, identify the map, check the reference, qualify | implemented and tested; nine typed stages, each with an attempt cap and a deadline |
| Transactional viewport fit (mismatch classification fenced for the duration) | implemented and tested |
| Ambiguous Roblox windows refused rather than guessed at | implemented and tested (macOS) |
| One coherent stamped frame per decision, keyed by run/generation/revision | implemented |
| Per-frame diagnostic packet, Minimal and Full Diagnostics overlays | implemented |
| Single input authority, leases, watchdog, out-of-process deadman | implemented; 10 000 stop races clean |
| Bounded dig / dequip / pan-swap / reset services | implemented; behavior characterized against the previous build |
| Standalone dig loop (**F6**) | implemented; **pixels pending reverification** |
| Observation mode, telemetry, evidence recorder, dashboard | implemented |
| Arrow detection by shape and local contrast, one temporal transaction per frame | implemented; **measured on a real-frame corpus** (eval: recall 80 %, 0 false locks, 0 identity switches); **E-PROF pending** |
| Signed direction from barb asymmetry, notch line, tip and axis, with reversal hysteresis | implemented; eval sign accuracy 91 %, median 10 deg; **E-DIR-E2E pending** |
| **Automatic map-profile selection** from consecutive frames | implemented; chosen on temporal agreement plus a score margin, verified on the real-frame corpus |
| **Automatic turn characterization** (arrow keys or relative mouse yaw) | implemented and tested against a simulated camera; **native measurement pending** |
| **Closed-loop align-then-follow**, one correction pulse in flight | implemented; simulated routes at 30/60/90/120 fps; **native route pending** |
| **Bounded reactive recovery** (release, reacquire, sticky-side strafe, probe, jump, opposite side once, abandon) | implemented and tested; **native obstacle test pending** |
| Runtime locomotion baseline sampled from this session's own walking | implemented; session-scoped, never presented as the offline E-MOTION gate |
| Real-frame corpus, split by contiguous sequence, with a bbox-aware evaluator and a regression gate | implemented (`tests/corpus/real`, `prospector_engine/corpus.py`) |
| Bounded per-frame trace (capture, scheduling, ROI/full detector, direction, preview, governor) | implemented; exported as JSONL on Stop and by `--shadow-bench` |
| Offline stratified evaluator with per-stratum confidence bounds | implemented (rendered stress only) |
| Cadence governor (WARMUP/STABLE/PROBE/COOLDOWN/DEGRADED), judged on a recent window | implemented and tested; native: 57 of 57 unique fps with perception at Balanced 60 |
| 2.5D terrain grid / detour planner | **not built** - reactive recovery is what exists |
| Automatic next map | **disabled** - needs E-NEXT_MAP |

Two kinds of status appear in that table and they are not the same thing.
*Offline evidence* (E-PROF, E-DIR-E2E, E-MOTION) is a claim about the software,
measured on held-out data, and it is still pending. *Runtime checks* are claims
about this session, measured every run, and they are what decides whether the
navigator will drive. Neither one grants input: that is still a physical click
and a physical hotkey press. See `STATUS.md`.

## What the preview shows you

The preview draws, over the live frame the navigator actually decided from:

* the **player-forward arm** — dashed, because the screen anchor is a
  hypothesis about the locked camera rather than a labelling of the avatar's
  pivot. Automatic setup checks that the heading to the arrow *holds still*
  with this anchor and records the measured jitter; E-ANCHOR and E-FORWARD, the
  offline labelling exercises, remain PENDING;
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

### Moving your character requires a physical human action

There is no way to start navigation from software. Automatic setup can reach
READY on its own; it can never arm. The sequence is:

1. Physically click **Arm Live** in the dashboard. This creates a one-use,
   30-second, process-run-bound token that is never persisted. Automatic setup
   cannot press it, and there is no code path that arms without it.
2. Physically focus Roblox.
3. Press **F1**.

The first thing the armed worker does is stand still: it confirms the camera
control mode and measures the turn actuator with bounded probes, releasing
after every one. If neither the arrow keys nor relative mouse yaw can be shown
to rotate the camera, it stops and says so rather than steering blind.

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

## How the automatic setup is bounded

Nine stages, each with an attempt cap **and** a monotonic deadline, each with a
typed failure kind and one sentence naming the next action:

| Stage | What it establishes | What its failure says |
|---|---|---|
| `FIND_ROBLOX` | one identifiable Roblox client | open it windowed / close the extra window / grant permission / leave fullscreen |
| `FIT_VIEWPORT` | the client at 1280x720, or the truth about what it clamped to | make it an ordinary window / grant Accessibility |
| `RESTART_CAPTURE` | capture rebound to the fitted client, exactly once | — |
| `STABILIZE_CAPTURE` | consecutive fresh frames matching the adopted geometry | check Screen Recording |
| `SELECT_PROFILE` | which map is equipped, by temporal agreement and margin | equip a map, or override under Advanced |
| `ESTABLISH_REFERENCE` | the heading to the arrow holds still while you do | stand still with the arrow visible |
| `SHADOW_QUALIFY` | the arrow is visible often enough, at a usable cadence | keep the arrow on screen / lower graphics settings |
| `VERIFY_CONTROL_MODE` | the locked-camera control mode, observed not assumed | turn Shift Lock on |
| `CHARACTERIZE_TURN` | sign, gain, minimum pulse, latency and reliability of the turn actuator | focus Roblox and try again |

The last two emit input and therefore run inside the armed live worker, with
the character stationary and every probe released. The first seven emit nothing
at all.

Input is released *before* the window is resized, and mismatch classification
is fenced for the duration of the fit — a resize passes through sizes that
would otherwise read as "somebody moved the window", restart capture and blank
the preview for a change that was going to succeed.

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
