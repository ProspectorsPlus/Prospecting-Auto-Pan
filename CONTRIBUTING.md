# Contributing to Prospector Lite

Thanks for your interest. This document covers setting up a dev environment, the test suites you must keep green, and the project's non-negotiable rules.

> **License status**: the project has not chosen a license yet ([LICENSE_CHOICE_REQUIRED.md](LICENSE_CHOICE_REQUIRED.md)). Until one is in place, contributions cannot be formally accepted or redistributed — issues and discussion are welcome in the meantime.

## Ground rules (non-negotiable)

1. **External-only.** No code that attaches to, injects into, reads the memory of, or modifies the Roblox process or its files. Screen pixels in, OS input out. See [docs/public-release/ROBLOX_SAFETY_BOUNDARY.md](docs/public-release/ROBLOX_SAFETY_BOUNDARY.md).
2. **Zero network by default.** No new network calls, update checks, telemetry, or remote content. The only sanctioned egress paths are the two user-configured ones inventoried in [docs/public-release/NETWORK_BEHAVIOR.md](docs/public-release/NETWORK_BEHAVIOR.md) — a PR that adds a network path must update that inventory and will get extra scrutiny.
3. **No secrets in the tree.** API keys and webhook URLs belong in the gitignored `prospecting_secrets.json` / your local config, never in source, tests, or fixtures.
4. **Imported files are untrusted.** Anything a user can import (`.ppscript`, `.ppbuild`, calibration JSON) must be parsed as data, strictly validated, and never evaluated.
5. **Every stop path releases input.** Changes to the engine must preserve the invariant that all termination paths funnel through `release_all()` (`prospector_engine/engine.py`).

## Dev setup

Python 3.11+ (3.13 tested).

```sh
# macOS
pip3 install pywebview pyobjc mss numpy pillow

# Windows
pip install pywebview pythonnet mss numpy pillow

# Run the app from source
python3 prospecting_app.py
```

Fully quit and reopen the app to pick up UI changes. The engine can also run headless for development: `python3 prospecting_old.py --help`.

## Repository layout (short version)

- `prospecting_app.py` — the pywebview GUI shell (config, pages, Studio editor, Coach).
- `prospector_engine/` — the macro runtime (spawned as a subprocess via the `prospecting_old.py` shim). Platform I/O lives in `platform_mac.py` / `platform_win.py`.
- `prospecting_ui.py` — browser-fallback UI + the `STUDIO_BLOCKS` schema (single source of truth for Studio).
- `prospecting_assistant.py` — the Coach's offline rule engine.
- `windows/` — the Windows copies of the app files plus packaging (`prospecting.spec`, `installer.iss`, `build.bat`). Shared code is kept in sync with the root files; if you change a root app file, apply the same change under `windows/`.

Full map: [docs/public-release/ARCHITECTURE.md](docs/public-release/ARCHITECTURE.md).

## Tests — run these before any PR

```sh
python3 -m py_compile prospecting_app.py prospecting_old.py prospecting_ui.py prospecting_assistant.py
python3 tour_check.py            # UI tour integrity
python3 finds_sim.py             # finds-detection scenarios
python3 studio_tests.py          # Studio editor + validator
python3 studio_conformance.py    # Studio IR conformance (self-skips: see note)
python3 prospecting_selftest.py  # app self-test
# Engine suites (all must pass):
python3 engine_contract_tests.py
python3 engine_characterization.py
python3 engine_flow_tests.py
python3 engine_plan_tests.py
python3 engine_parity_tests.py
python3 engine_parallel_tests.py
python3 engine_pacing_tests.py
python3 engine_trace_tests.py
python3 public_release_tests.py     # the release gate
python3 onboarding_trust_tests.py   # the onboarding/trust suite
python3 capacity_tests.py           # capacity calibration validation (sim-safe)
python3 diagnostics_tests.py        # diagnostics/recommendation engine (pure Python)
```

Note: `studio_conformance.py` resolves its goldens from a private sibling repository and **skips cleanly (exit 0)** when they are absent — it no longer blocks outside contributors or public CI. The Studio engine paths remain covered by the tracked `engine_*` suites.

Add or extend tests when you change behavior. Engine changes must keep the golden/characterization suites passing or update them with justification in the PR description.

## Submitting changes

The repository is not published yet (`<repository URL — to be filled in when published>`). Once it is:

1. Fork and branch from `main`.
2. Keep commits focused; describe *why* in the commit message.
3. Run the full test list above and say so in the PR.
4. If your change affects privacy, network behavior, or the safety boundary, update the matching document under `docs/public-release/` in the same PR.

Security issues: do not open a public PR/issue — see [SECURITY.md](SECURITY.md).

## Code of conduct

Participation is covered by the [Code of Conduct](CODE_OF_CONDUCT.md).
