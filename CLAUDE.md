# CLAUDE.md — Treasure Navigator

Repository-specific operating rules. Written by hand from
`TREASURE_NAVIGATION_PLAN.md` §17; every command below was executed in this
worktree before being recorded here.

`TREASURE_NAVIGATION_PLAN.md` is the authoritative architecture and evidence
specification. This file is the short operating contract — it deliberately
contains no measurements, no thresholds, and no architecture essay. When the
two disagree, the plan wins.

---

## 1. What this repository is

A Roblox *Treasure* macro being rebuilt into an observable, cross-platform
navigator: pin the Roblox client area, watch the equipped treasure map's
arrow, walk/turn toward it under closed-loop control, confirm arrival, run
the bounded dig / pan-swap services, and stop safely.

`complexion.md` is historical and describes a different repository state. Do
not treat it as architecture truth.

## 2. Environment (verified)

- Interpreter: `.venv/bin/python` — CPython 3.13.15, macOS arm64, Tk 9.0.
- The ambient system `python3` is 3.8.10 and is **unsupported**. Never invoke
  a bare `python3` for this project; always spell out `.venv/bin/python`.
- `.venv-python38-backup/` is a **user-owned untracked directory**. Never
  read it recursively, move it, stage it, delete it, or package it. It is
  listed in `.gitignore` purely so it cannot be staged by accident.

Setup from scratch (macOS):

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Reproducible installs use the platform lock, then the project without deps:

```sh
.venv/bin/python -m pip install --require-hashes -r requirements-macos.lock
.venv/bin/python -m pip install --no-deps -e .
```

macOS additionally needs **Accessibility** and **Screen Recording** granted
to whichever process launches Python (Terminal, iTerm, or the packaged app).
Without them window pinning and capture fail with a clear message; they do
not silently degrade.

## 3. Safe local commands

```sh
.venv/bin/python -m pytest -q                 # full local suite
.venv/bin/python -m pytest -q -m "not native" # explicit: never emits OS input
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy prospector_engine deadman.py
.venv/bin/python treasure.py --self-test      # imports + contracts, no input
.venv/bin/python treasure.py --smoke-test     # packaging smoke test, no input
.venv/bin/python treasure.py --capture-probe  # measure the pipeline, read-only
.venv/bin/python treasure.py --replay DIR     # replay a recording, no input
.venv/bin/python treasure.py --detector-report  # rendered stress strata, no input
.venv/bin/python treasure.py --detector-report --corpus tests/corpus/real --json out.json
                                              # real-frame corpus report, no input
.venv/bin/python treasure.py --soak 10        # bounded pipeline soak, no input
.venv/bin/python treasure.py --shadow-bench 15 --json bench.json
                                              # native capture + headless perception
.venv/bin/python treasure.py --setup-probe    # real automatic setup, no input sent
.venv/bin/python treasure.py --hotkey-test 30 # watch the listener hear keys, no input
.venv/bin/python treasure.py --tracking-report  # rendered recovery latency, no input
```

Two modes are deliberately **not** in that list, because neither is safe to run
unattended:

```sh
.venv/bin/python treasure.py --forward-probe 600    # PRESSES W AGAINST ROBLOX
.venv/bin/python treasure.py --native-control-probe  # PRESSES KEYS AT ROBLOX
```

`--detector-report` (rendered) and `--soak` use the fixtures in `tests/`, and
`--detector-report --corpus` reads the committed real-frame corpus, so all
three need a source checkout. None touches a window or emits input.
`--shadow-bench` captures the real Roblox window through the production
source and runs perception on it; it moves nothing and sends nothing, and
needs Screen Recording. It also reports how often the arrow was readable and
the distribution of unreadable gaps, which is the evidence
`SteeringLimits.coast_grace_s` and `search_budget_s` are chosen against — run
it with a treasure map equipped, or it will honestly report that nothing was
measured. `--setup-probe` runs the production setup machine
through the real `build_application`, so it *does* resize the Roblox client —
that is the stage under test — and restores the original client size on the
way out. It stops at the observation half and prints the held-lease ledger;
the stages that move the camera run inside Live and are unreachable from a
command line. Every mode is mutually exclusive and bounded
(`tests/test_cli_lifecycle.py`).

Everything in the first block is safe to run unattended; `--forward-probe` is
not, and an agent must never run it. `native` tests are excluded by the
default `addopts` in `pyproject.toml`, so a plain `pytest` cannot emit OS
input; opt in with `-m native` only while physically at the machine.

`--capture-probe` and `--calibrate` read pixels. They move no window and send
no input, but they do need Screen Recording permission.

On macOS, `--calibrate` reads through `pixel-detector/` — a standalone
ScreenCaptureKit + pybind11 extension (`detector.mm`) — rather than the
production capture pipeline; every other platform still uses the pipeline
reader (`_run_calibrate_capture_pipeline` in `treasure.py`), since
`pixel-detector` is macOS-only. It is built once, out of band:

```sh
cd pixel-detector
../.venv/bin/pip install pybind11   # xcode-select --install first, once
../.venv/bin/python setup.py build_ext --inplace
```

It averages the same `DEFAULT_PIXELS.sample_box_px` square the production
frame pipeline does (falling back to a single pixel, with a NON-CANONICAL
note, when the client isn't pinned to the canonical size), and
`MacScreenCaptureKitSource` was fixed to request the same `kCGColorSpaceSRGB`
color space `pixel-detector` uses — so a value read with `--calibrate` is
what the running navigator reads at that point, not just close to it
(D-093). Every color-gated field in `TreasurePixels`/`ChestPixel` predates
that fix and is still `PENDING`; D-093 in `DECISIONS.md` has the exact list
still owed a re-measurement with the fixed tool.

`--hotkey-test` starts the real global listener and prints every key edge it
normalizes, so a person can see whether a chord is heard. It submits to a list
rather than to the coordinator, and it is built without the physical-chord
capability, so nothing it recognizes can start a mode or reach an input
session; it needs Input Monitoring.

`--forward-probe` and `--native-control-probe` are the **two modes that emit
input**, and an agent must never run either unless the owner has asked for it
in the current session (rule 1 below). `--forward-probe` presses `W` once
through the whole production stack — coordinator, authority, readiness — and
reports whether the world moved. `--native-control-probe` presses at the client
directly, with no coordinator, no authority, no profile and no setup, so a
negative result cannot be explained away by anything upstream:

```sh
.venv/bin/python treasure.py --native-control-probe \
    --key w,a,s,d --hold-ms 600 --backend hid,session,pid --trials 2 \
    [--scroll] [--no-camera] [--json PATH]
```

Passing the flag is the authorization. It refuses unless Roblox is the
positively identified frontmost window, posts exactly one edge per trial,
releases it in a `finally`, and sweeps the whole vocabulary on the way out.
`--scroll` adds a transport trial that a Roblox window answers in **any** state,
which is what separates *"our events never arrive"* from *"the game has nothing
to move"* — a client on its home page gives an honest "no motion" for `W` that
looks exactly like a broken backend and is not one (D-074).

## 4. Hard rules for any agent working here

1. **Never start Live and never operate Roblox.** Live requires one physical
   human press of **Ctrl+N** while Roblox is focused. Since D-062 that is the
   whole gesture: the separate **Arm Live** click is gone, and the chord both
   authorizes and starts in a single coordinator transaction. The gate did not
   get weaker — the intent must carry a `PhysicalChordProof` minted by
   `ChordAuthority`, which is handed to exactly one hotkey listener — but it
   did get shorter, so read this rule as covering *the whole* of arming.
   An agent may not simulate, bypass, pre-authorize, or persist that press,
   may not hand `ChordAuthority` to anything but the listener, and may not add
   a code path that would let it. An agent must also never run
   `--forward-probe` or `--native-control-probe` — the two commands that press
   a key — unless the owner has asked for exactly that in the current session,
   and then only against a client the owner has said is ready.
2. **Never press a game hotkey to test it.** The Ctrl chords (Ctrl+N, O, X,
   1, 2, 3, 4, 5, I — identical on both platforms, no F-key aliases) drive
   real input into Roblox, with one exception: Ctrl+5 finds and resizes the
   Roblox client (geometry only — the same unarmed action automatic setup
   already runs every session) and arms a read-only heading-error poller; it
   never reaches an input session and never presses a key. Their handlers
   are covered by tests that use fakes and synthetic CGEvents; an agent must
   never fire them on the real machine.
3. **Never fabricate native evidence.** macOS and Windows gates are tracked
   separately. If hardware, a real Roblox session, or owner observation was
   not available, the gate status is `pending` with the exact steps needed.
   Mocked or opposite-OS import tests are never reported as native proof.
4. **No destructive git.** No `reset --hard`, no `clean -fd`, no force push,
   no bare `git stash` / `git stash pop` (the stash stack is shared with
   other worktrees). Do not touch unrelated user changes.
5. **Do not publish.** No push, tag, or release while **G-LICENSE** (plan
   §15) is unresolved.
6. **Do not copy Prospector Studio source, docs, or CSS.** General
   engineering ideas may be reimplemented independently; verbatim reuse is
   not permitted. Adapted first-party mechanics get provenance in
   `DECISIONS.md` and the commit message.
7. **Never populate a calibration that was not measured.** `YawCalibration`,
   `ShiftLockProof`, and `LocomotionBaseline` reach `VALIDATED` only through
   their physically armed procedures on real hardware. Filling them in to make
   a code path reachable converts a safety gate into a comment. Tests may
   construct one; they say so in a docstring and live in `tests/`.
8. **A default `pytest` run may never emit an OS edge.** `native` tests are
   excluded by `addopts`, and that is not the whole rule: a test that shells
   out to an input-emitting CLI mode emits input whenever the machine happens
   to satisfy that mode's preconditions. One did, briefly, during the tenth
   pass — it ran `--native-control-probe` in a subprocess and asserted on the
   refusal, and on a machine with Roblox frontmost it pressed keys and dragged
   the right mouse button across a live client instead. Assert such a refusal
   in-process, against a port that says "not frontmost".
9. **Rendered frames are training stress, never held-out validation.** Plan
   §7.2 is explicit. `tests/arrow_fixtures.py` exists to stress the detector
   deterministically, and no gate may be passed on its output.
10. **Tune on `tune`, read `eval` once.** In `tests/corpus/real/labels.json`
   the sequences marked `tune` are the only ones a detector change may be
   chosen on; `eval` sequences are read to report, never to pick. A new
   real recording extends the corpus as new sequences with provenance; the
   private recording itself is never committed.
11. **The bridge is inert at corpus cadence, on purpose.**
   `TemporalConfig.max_step_s` bounds the gap between two frames a patch match
   may claim continuity across. The real corpus is sampled at about 5 fps, so
   the bridge cannot fire there and no corpus number measures it — in either
   direction. An unbounded version bought 2.4 points of eval recall and cost a
   false lock on the same-coloured sand sequence (D-094). Do not widen this
   bound to make a corpus number move.
12. **A simulation is never a navigation gate.** `tests/test_routes.py` drives
   the real navigator through a world that models the measured turn latency and
   three shapes of obstacle, and it is a claim about the *decision path*. Duty
   cycle, alignment, occlusion behaviour and recovery success **on the real
   game** stay `pending` until an armed route produces a trace. The simulations
   are how a change is chosen; they are never how it is passed.

## 5. Architecture boundaries

- **One composition root, and it is not the GUI.**
  `prospector_engine/application.py` wires every object the process owns and
  imports no user interface. `treasure_gui.py` owns the Tk window and calls
  `build_application()`; so does `treasure.py --setup-probe`. Never put wiring
  back in the GUI module — it is what made automatic setup unverifiable
  without opening a window (D-042).
- **One input authority.** `prospector_engine/input_authority.py` owns the
  only held-key/button ledger and the only calls into a `PlatformPort`.
  Feature code receives a narrow capability object — `NoInputSession`,
  `NavigationInputSession`, or `ServiceInputSession` — and never a raw port,
  the ledger, or a platform module.
- **One mode owner.** `prospector_engine/coordinator.py` owns `RunMode`, the
  authority generation, and the single mode worker. The GUI and hotkey
  threads submit `RuntimeIntent` objects and nothing else.
- **One coherent frame per decision.** `prospector_engine/capture.py`
  publishes a stamped `CapturedFrame`; no feature captures independently and
  no logical tick reads three separate instants. Consumers wake on
  `wait_for_new`, not on a timer — the pipeline is push-shaped.
- **Coordinate spaces are types, not conventions.**
  `prospector_engine/geometry.py` defines `DISPLAY_LOGICAL`, `CLIENT_LOGICAL`,
  `CLIENT_BACKING`, and `CANONICAL`. Never pass a device-pixel rectangle to a
  window API, and never write a transform by hand — compose an `Affine2D`,
  which refuses mismatched spaces.
- **One viewport authority.** `ViewportGuard` owns the single
  `ViewportState`. Detector readiness, coordinator readiness, the GUI, and Live
  gating all read it, so "viewport ok" cannot coexist with "unsupported
  viewport size".
- **One observation per frame, keyed.** `DiagnosticObservation` holds its own
  frame and a `RuntimeKey` of run, coordinator generation, mode session, source
  epoch, geometry revision, profile revision, frame sequence and content id.
  Every consumer calls `key.supersedes(...)` before drawing or acting; ordering
  is by monotonic world ordinal, so a straggler from a cancelled worker is
  recognised as old rather than merely different.
- **Connecting is not resizing.** `ViewportGuard.connect()` binds to the client
  and touches nothing. `fit_and_lock()` is the separate, optional, bounded
  state machine. Capture must never depend on a resize succeeding.
- **One profile authority.** `ProfileAuthority` owns the active arrow profile.
  Selection is by stable id; display labels are derived from ids and never
  parsed back. A swap is staged and applied at a frame boundary.
- **Colour proposes, geometry disposes.** `prospector_engine/arrow.py` may use
  colour to generate candidates and never to accept one. On the green map the
  arrow and the grass share a chromaticity to three decimals (D-024).
- **Forward and steering are independent outputs.** `ArrowFollowerController`
  is a continuous-pursuit controller: a heading error selects how hard to
  correct *while walking*, and only a severe error sustained past
  `pivot_confirm_s` stops the character. Five of nine `ControlState` values
  walk, and a turn pulse completing never releases `W` (D-077).
- **Occlusion is not obstruction.** A lost arrow while moving is COAST, then a
  bounded moving SEARCH, then a safe stop. Whether the character is *stuck* is
  a separate question, answered from measured motion and never from arrow
  visibility (D-078).
- **Two halves of perception, and only one of them owns identity.**
  `prospector_engine/arrow.py` is the global structural authority: it alone
  acquires, associates, switches, reacquires and loses. `temporal.py` is a
  bounded local correlator that carries an *already established* identity
  across frames the detector could not segment; it never acquires and never
  switches, its template is only ever cut on a global commit, and a global
  commit always wins outright rather than being averaged with it (D-094).
  Which half produced a frame's evidence is a type — `EvidenceProvenance` —
  and it reaches the observation, the timing and the trace. Never add a third
  tracker; never let a bridged frame be read as an acquisition.
- **A bounded thing running out ends an episode, not the mission.**
  `MissionSupervisor` turns `ABANDONED` into a bounded pause with nothing held
  and a reacquisition inside the *same* mode session, generation and physical
  authorization (D-096). Repairs are capped by count, total time, per-repair
  deadline and a thrash detector. `ARRIVED` is never repaired, `CANCELLED` is
  a person pressing Stop, and no code path here may mint, simulate or persist
  a chord.
- **One readiness predicate for Live, read by everyone.** `Readiness.live_ok`
  (`input_ok and setup_ready`) is what `_on_start_live` and the SETUP blocker
  both consult. Two authorities for that question is how Ctrl+N came to enter
  Live three and a half seconds before setup reported it had failed (D-098).
  `input_ok` keeps its narrower meaning for bounded SERVICE modes.
- **Setup repairs itself, and the hard set stays hard.** A failure carries a
  `SetupDisposition`: HARD is never retried, RECOVERABLE resumes from the
  earliest invalidated prerequisite, ENVIRONMENTAL is waited for with nothing
  held (D-097). The mapping is a table so it can be read at a glance, an
  unknown kind defaults to HARD, and a test asserts the hard set has not
  grown.
- **The keyboard the navigator reasons about is the actuator's, not its own.**
  `Navigator.note_held` is fed from `MovementOutcome.held` on every path.
  Everything downstream — the applied-forward ledger, the locomotion baseline,
  the stuck detector, recovery — is dead without that call, and for a whole
  pass it was missing (D-076).
- **One heading, one filter.** `prospector_engine/heading.py` owns the only
  remembered direction; `prospector_engine/traversability.py` owns the only
  memory of which way already failed. Neither is duplicated anywhere.
- **`release` means let go *and forget*.** It is for safety, terminal states
  and world changes. Ordinary transitions between FOLLOW, CORRECT, COAST,
  SEARCH and RECOVERY set a new level and keep every piece of memory (D-080).
- **`W` is an evidence-bound lease.** `CommandKind.ALIGN` cannot command any
  movement axis — the contract raises. A navigation command is APPLIED only if
  every edge landed.
- **Release is unconditional.** `release_all()` is idempotent, never
  focus-gated, and always attempts the full input vocabulary even after an
  individual edge fails.
- **Platform modules are instance-based.** They must not import each other,
  bind back into the engine module, or hold authoritative input state. A
  module that imports Quartz or ctypes at import time may only be imported
  on its own OS; `ports.py` stays import-clean on both.
- **`deadman.py` is release-only.** It can release inputs; it has no code
  path that presses one. `treasure.py --deadman` dispatches it before Tk,
  OpenCV, capture, or engine imports.

## 6. Code conventions

- Frozen dataclasses and enums for anything crossing a thread boundary; no
  stringly-typed lifecycle. NumPy arrays in a frozen dataclass are marked
  non-writeable.
- Units live in the name: `_px`, `_ms`, `_s`, `_deg`, `_norm`. Internal
  time is `time.monotonic()` seconds; milliseconds appear only at
  boundaries.
- Errors become typed outcomes at subsystem boundaries. Unexpected
  exceptions safe-stop at the coordinator boundary; they never escape into
  a thread with a held input.
- Every retry loop has **both** an attempt cap and a monotonic deadline.
- Tuned numbers live in a frozen config dataclass that carries a
  `Provenance` field (`AuthorityConfig`, `SteeringConfig`, `TreasurePixels`,
  …), never as bare literals in logic. Versioned package data lives in
  `prospector_engine/profiles/`. A test enforces the provenance rule.
- Docstrings state invariants and failure behavior. Comments explain why a
  constraint exists and cite the bug or experiment ID (`B7`, `E-MOTION`).

## 7. Evidence vocabulary

Used consistently in code, logs, docs, and commit messages:

| Term | Meaning |
|---|---|
| **observed fact** | Measured here, with the command that produced it. |
| **provisional configuration** | A chosen starting value with provenance; not a measurement. |
| **validated** | Passed its frozen gate on held-out data for that exact OS/profile/condition. |
| **guarded beta** | Works only under physical arming and owner observation. |
| **pending** | The gate exists and has not been run. Never presented as a pass. |
| **unsupported** | Ruled out for this OS/profile/condition until new evidence. |

A feature is enabled in production only for the exact OS, profile, and
condition whose gate passed. Aggregate passes never cover a failing stratum.

## 8. Where things are recorded

- `TREASURE_NAVIGATION_PLAN.md` — authoritative plan (do not rewrite it to
  match the code; change the code or record a deviation).
- `DECISIONS.md` — short dated log of local implementation decisions and
  their rationale.
- `STATUS.md` — per-phase local / macOS / Windows gate status.

Note: `ruff format` rewrites Python blocks inside Markdown, so `*.md` is
excluded in `pyproject.toml`. The plan must never be reformatted.
