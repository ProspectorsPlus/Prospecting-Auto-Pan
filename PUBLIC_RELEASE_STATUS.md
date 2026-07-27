# Prospector Lite — public-release status

Factual working state for the public-release pass. Updated as work lands.

## Starting state
- Branch: `fable/prospector-engine`
- HEAD at start: `ad7e9eb5dc280b04e77e208e97d8bf064d5a1afc`
- Tracked tree clean at start (192 files). Remote: `origin`
  = github.com/ProspectorsPlus/Prospecting-Auto-Pan.git (private).
- Baseline test run (this session, before any change): compile checks,
  tour_check, finds_sim, studio_tests, prospecting_selftest and all ten
  engine suites — ALL PASS.

## Confirmed entry points
- App: `prospecting_app.py` (+ byte-mirrored `windows/prospecting_app.py`).
- Engine: `prospector_engine/engine.py` via `prospecting_old.py` shim, spawned
  with `home=cwd=DATA_DIR`.
- macOS package: legacy `Prospectors Plus.app` shell launcher (system Python +
  first-run pip) — to be replaced by a self-contained PyInstaller build.
- Windows package: `windows/prospecting.spec` + `installer.iss` +
  `.github/workflows/build-windows.yml`.

## Security/privacy findings (pre-change)
1. `_report_usage()` posts IP + coarse geolocation (ip-api.com) + access-code
   hash + machine-id hash to an owner Discord webhook (`SYNC_URL`) at every
   run launch and code redemption. REMOVE.
2. Access gate: `access_state()` / `verify_access()` fetch hashed codes from
   GitHub Pages; machine-locked via `_machine_id()` (hardware UUID hash).
   REMOVE (gate, fingerprint, and config fields).
3. Auto update check (`check_update()`) runs at every boot; `do_update()`
   downloads + silently runs an installer. REMOVE (manual link only).
4. `_tutorial_refresh_remote()` fetches remote tutorial content at boot.
   REMOVE.
5. Analytics window HTML loads Google Fonts from the network. REMOVE (system
   fonts).
6. CI workflow injects the owner webhook into shipped configs from repo
   secrets. REMOVE.
7. User Discord webhook notifications + Coach AI mode (user key) are
   legitimate opt-in features: keep, disclosed, off by default.
8. Secrets IN GIT HISTORY (current tracked files clean; verified this
   session): commit `6842a47` committed the owner's real Discord webhook
   URL (configs + Windows zip); commit `e0f3d4f` committed the notify-bot
   `WEBHOOK_SECRET` value and the bot endpoint as baked fallbacks. Both
   were scrubbed from files in `7ff6537` but remain in history. No API
   keys were ever committed (history `-S sk-proj/sk-ant` hits are UI
   placeholder text only). `prospecting_secrets.json` and
   `ACCESS_CODES_PRIVATE.txt` were never committed. Consequence: revoke
   the webhook + bot secret, and publish the public repo from FRESH
   history. USER ACTION.
9. `prospecting_secrets.json` (untracked, gitignored) holds the owner's coach
   API key + webhooks; never committed. Must never be bundled.

## Completed changes
- docs/public-release/ARCHITECTURE.md written.
- (in progress — see git log for the running list)

## Platform status
- macOS: dev runtime verified via test suites (this Mac).
- Windows: cannot execute locally; CI workflow will be rewritten, syntax
  validated; runtime verification on a real Windows runner = release blocker
  until the repo is pushed.

## Remaining genuine blockers (user-owned)
- Choose an open-source license (LICENSE_CHOICE_REQUIRED.md).
- Decide/confirm the public repository URL (used by welcome/About links).
- Revoke the historical Discord webhook + decide fresh-history publish.
- Apple signing/notarization credentials (unsigned RC otherwise).
- Push → run Windows CI on a real runner.

## If interrupted, resume with
```
cd <repo root>
git status --short --untracked-files=no && git log -5 --oneline
python3 tour_check.py && python3 finds_sim.py && python3 studio_tests.py
python3 public_release_tests.py   # once it exists
```
