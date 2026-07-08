# Prospectors Plus 4.0.0 (Fable engine) — Release Readiness Checklist

**Status:** committed, version-stamped `4.0.0`, engine complete and tested.
**NOT released** (per request). This document is the honest gap between "in git"
and "shippable to invite-only users."

## ✅ Done
- Fable5 engine committed, stamped **v4.0.0** (`engine/__init__.py`).
- Verified: `py_compile` clean; **31/31** sim tests pass; monte-carlo
  ~2450 pans/hr @ 100% clean cycles, **zero hard stops** across all noise
  scenarios (edge-heavy, glitchy shake, sticky cue, dig lag).
- Secrets: the personal OpenAI key is out of the tracked config and in a
  **gitignored** `prospecting_secrets.json` (old app). Fable5 never imports
  keys/webhooks/secrets. Windows zip rebuilt secret-free.
- Runtime/junk gitignored (`fable_config.json`, `fable_history.json`,
  `__pycache__`, `.DS_Store`, stray files).

## ⛔ Required before it can actually ship (invite-only product)
These exist in the **old** app and are **not ported** to Fable5 yet (its README
says so on purpose — "core first"):

1. **Access-code gate.** The product is invite-only. Port the gate screen +
   code verification (hash check against `docs/codes.json`) into Fable5
   `app.py`. Without it, anyone who gets the build can run it.
2. **Auto-update.** Port the `version.json` check + installer download/relaunch
   so shipped users get updates. Point it at a **4.0.0** `version.json`.
3. **Windows packaging.** Write a PyInstaller spec for the Fable5 layout
   (`engine/` package + `app.py` entry + `run_engine.py`), bundle the fable
   defaults, and update/port `installer.iss` (set `MyAppVersion 4.0.0`, new
   entry point). The current spec/installer target the OLD single-file app.
4. **Release version wiring.** Bump `docs/version.json` to `4.0.0` with the new
   installer URL + notes, and `installer.iss` `MyAppVersion`.
   ⚠️ Existing 3.x installs auto-update off the SAME `version.json` — make sure
   the 4.0.0 installer cleanly replaces the old app (or gate the jump), so 3.x
   users aren't broken by landing on a different app.

## ⚠️ Feature parity users will expect (port or clearly mark "coming")
The old app has these; Fable5 does not yet:
- **Fortune River / Starfall River recovery** (fast-travel on soft stop).
- **Treasure chest mode.**
- **Discord webhooks / notifications + screenshots.**
- **The Coach** (offline tuning assistant + optional API mode). If ported, reuse
  the **gitignored secrets** pattern for any API key — never bundle it.
- **Private analytics** (launch ping) if you want to keep enforcing invite-only.

## First-run migration note
Fable5 auto-imports old calibration / timings / relics / hotkeys / stats from
`../prospecting_config.json` (never secrets). A **shipped** user won't have the
old config next to the app — decide: bundle sane defaults, run the guided
calibration on first launch, or ship a migration path from the old install
location.

## Release steps (DO NOT run yet)
When the above is done:
1. Bump `docs/version.json` → 4.0.0 (+ installer URL, notes, `critical` if forced).
2. Bump `installer.iss` `MyAppVersion` → 4.0.0; build `ProspectorsPlusSetup.exe`.
3. Build the mac app bundle.
4. `git push`; `git tag v4.0.0`; `git push origin v4.0.0`.
5. GitHub → Releases → draft `v4.0.0`, attach the `.exe`, publish.
6. Confirm the download link resolves before/after `version.json` goes live.

## Secrets hygiene reminder
- The old OpenAI key (`sk-proj-…`) was present in local (unpushed) commit
  history and has been scrubbed; **rotate/regenerate it** in the OpenAI console
  to be safe, and paste the new one into the app (it saves to the gitignored
  secrets file).
- Never commit `prospecting_secrets.json` / `fable_secrets.json`. Never bundle
  them into a build.
