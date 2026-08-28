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
```

Everything above is safe to run unattended. No test emits real OS input
unless it is marked `native`, and `native` tests are skipped by default.

## 4. Hard rules for any agent working here

1. **Never arm Live and never operate Roblox.** Live mode requires a
   physical human click on **Arm Live** in the Tk UI followed by a physical
   hotkey press while Roblox is focused. An agent may not simulate, bypass,
   pre-authorize, or persist that arming, and may not add a code path that
   would let it.
2. **Never fabricate native evidence.** macOS and Windows gates are tracked
   separately. If hardware, a real Roblox session, or owner observation was
   not available, the gate status is `pending` with the exact steps needed.
   Mocked or opposite-OS import tests are never reported as native proof.
3. **No destructive git.** No `reset --hard`, no `clean -fd`, no force push,
   no bare `git stash` / `git stash pop` (the stash stack is shared with
   other worktrees). Do not touch unrelated user changes.
4. **Do not publish.** No push, tag, or release while **G-LICENSE** (plan
   §15) is unresolved.
5. **Do not copy Prospector Studio source, docs, or CSS.** General
   engineering ideas may be reimplemented independently; verbatim reuse is
   not permitted. Adapted first-party mechanics get provenance in
   `DECISIONS.md` and the commit message.

## 5. Architecture boundaries

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
  no logical tick reads three separate instants.
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
- Tuned numbers live in typed config with provenance
  (`prospector_engine/profiles/`), never as bare literals in logic.
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
