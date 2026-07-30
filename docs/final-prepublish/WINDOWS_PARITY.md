# Windows parity (1.0.0-rc.6)

The mirror model, the divergence this pass found and healed, the new
guards that make that class of drift impossible to miss, the acceptance
script, and the honest runtime status.

## The mirror model

Root is the single source of truth; `windows/` carries five generated
copies (`packaging/sync_windows_app.py`, docstring lines 2-19):

1. `windows/prospecting_app.py` — byte-identical to root EXCEPT two
   platform-variant blocks (the primary-screen size lookup via Quartz
   vs `GetSystemMetrics`, and its 1440×900 vs 1920×1080 fallback). The
   sync script substitutes them by anchor match and **aborts loudly if
   an anchor stops matching exactly once** (sync_windows_app.py:48-54),
   so a refactor cannot silently produce a broken Windows copy.
2. Four **verbatim twins** — `prospecting_ui.py`,
   `prospecting_assistant.py`, `prospecting_old.py`,
   `prospecting_prices.json` (`VERBATIM_TWINS`,
   sync_windows_app.py:27-32) — plain byte copies, no transforms.

One command regenerates the whole set: `python3
packaging/sync_windows_app.py`, then `python3 tour_check.py` verifies
the lockstep.

## The healed divergence (REPRODUCTION_REPORT.md issue 7)

`windows/prospecting_ui.py` had silently missed the rc.3 atomic
config-write fix (root `prospecting_ui.py` writes tmp + `os.replace`;
the Windows copy still truncate-wrote `CONFIG_FILE` directly). Until
this pass, the twins were synced *by hand*, `sync_windows_app.py` only
regenerated `prospecting_app.py`, and `tour_check.py` lockstep-checked
only the studio-schema region of the ui pair — nothing could catch this
class of drift. A frozen Windows package would have shipped the
non-atomic write. Healed root→windows in commit `a3b1db2` (a 4-line
diff to `windows/prospecting_ui.py`); the other three twins were
verified byte-identical.

## The new guards

- `sync_windows_app.py` now copies the four twins too (one command,
  whole mirror set).
- `tour_check.py` gained a **full-file byte-parity check** for the
  twins — proven to fail on a deliberately corrupted byte, then
  restored (commit `a3b1db2`).
- The pre-existing guards stand: `windows/prospecting_app.py` shared
  blocks byte-lockstep in tour_check, `py_compile` of both copies, and
  the release gate's version agreement (`scan_version`).

Also fixed while here: `windows/Install.bat` (local-only, gitignored)
was repointed at the real "Prospector Lite.bat" launcher with current
branding — it still launched the pre-rename `.bat`.

## The acceptance script — `packaging/windows_acceptance.ps1` (new, 385 lines)

Run from a **normal** PowerShell on a real Windows machine
(`pwsh -File packaging\windows_acceptance.ps1 [app-folder]`); refuses
elevated shells (exit 2). Automated probes: exe presence + version
identity against `build_info.json` and the source `VERSION`; an
isolated `--capabilities` smoke run (temp `PP_DATA_DIR`, install folder
must stay untouched); a bounded first-boot probe (bridge liveness via
`onboarding_state.json` appearing, zero owned TCP connections, clean
kill); the `%APPDATA%\Prospector Lite` data-dir convention in a
sandboxed `APPDATA`; bundle content + brand/tracking-string scan; and a
static DPI-awareness check. DPI *behavior* is deliberately a printed
MANUAL 100/125/150% checklist — the script fabricates no runtime
evidence it did not gather. Wired into
`.github/workflows/build-windows.yml` after the smoke test; usage and
the manual checklists are in `WINDOWS_TESTING.md`.

## Honest runtime status

**Prepared and static-validated; NEVER yet executed on real Windows.**
Everything Windows-side is verified only statically, on macOS:
`py_compile` of the `windows/` copies, tour_check lockstep + full-file
twin parity, YAML parsing of the workflows, and a review pass over the
PowerShell (PowerShell is not installed on the development Mac, so even
a `pwsh` syntax check has not run). The frozen exe, the installer,
`windows_acceptance.ps1`, and every step of `build-windows.yml` have
never run anywhere. **A green `build-windows.yml` run on a real runner
remains a release blocker**; do not publish a Windows artifact before
one exists. No claim of Windows execution, signing, or notarization is
made anywhere in this pass.
