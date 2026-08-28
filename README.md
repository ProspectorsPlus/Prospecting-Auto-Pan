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
| Canonical client-area pin and read-back | implemented; **E-VIEW pending** on both OSes |
| One coherent stamped frame per decision | implemented |
| Single input authority, leases, watchdog, out-of-process deadman | implemented and tested |
| Bounded dig / dequip / pan-swap / reset services | implemented; behavior characterized against the previous build |
| Shadow observation, telemetry, evidence recorder, dashboard | implemented |
| Arrow detection, direction cues, motion estimators | implemented as **candidates**; no gate has been run |
| Live navigation (steering) | **refuses to run** — it names the pending experiments instead |
| Obstacle recovery | **disabled** — needs E-MOTION *and* E-RECOVERY |
| Automatic arrival | **disabled** — needs E-ARRIVE |
| Automatic profile classification | **disabled** — selection is explicit |
| Automatic next map | **disabled** — needs E-NEXT_MAP |

That table is the point of the project, not an apology for it. Each feature is
enabled only for the exact OS, arrow profile, and condition whose evidence gate
passed, and no gate has been run yet. See `STATUS.md`.

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
```

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

Hotkeys: **F1** start Live (armed) · **F2** stop · **F3** pixel probe ·
**F4** reset character · **F5** pan-swap test.

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
