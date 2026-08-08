# Public Launch — Starting State (2026-08-08)

Snapshot taken before the Prospector Lite stable public launch, from the live tree
(not from older reports).

## Repository

- Working copy: `~/Roblox Macro/Claude`
- Branch: `fable/prospector-engine`, HEAD `a94e609` (clean tracked tree)
- Remote: `https://github.com/ProspectorsPlus/Prospecting-Auto-Pan` — **already PUBLIC**
- Default branch: `main` = `1fe08d7` (= tag `v4.2.0`, the last Prospectors Plus release)
- Branch relationship: `fable/prospector-engine` is a strict fast-forward of
  `origin/main` — 105 commits ahead, 0 behind. Pushing it to `main` is a
  fast-forward; no merge or history rewrite involved.
- Tracked files on the release branch: 216 (clean Prospector Lite tree).
  `origin/main` still tracks the old Prospectors Plus tree (old app zips,
  `ACCESS_CODES_GUIDE.md`, `prospecting_calib_log.csv`, `docs/codes.json`
  access-code site, `Fable5`, calculator vendor dir…). All of those are
  **deleted at the tip** of the release branch; they remain in public history
  (they have been public since June 2026).

## Versioning

- App version source: `prospecting_app.py` `VERSION = "1.0.0-rc.6"` (+ Windows twin).
- Existing public tags: `v1`–`v4.2.0` (old Prospectors Plus product), including
  `v1.0.0` and `v1.0.1` (June 2026). **Tag `v1.0.0` is taken** — the planned
  "Prospector Lite 1.0.0" cannot reuse it without destroying an existing public
  release.
- Decision: ship the stable as **5.0.0** (tag `v5.0.0`, title "Prospector Lite
  5.0.0"), continuing the repository's monotonic release lineage. The
  `1.0.0-rc.x` line ships as 5.0.0; release notes explain the renumbering.

## GitHub / auth

- `gh` has two accounts. `IbraheemArif` has **no push access** to the repo
  (403 on push). Active account switched to **ProspectorsPlus** (scopes:
  `repo`, `workflow`, `read:org`, `gist`) — push verified via dry-run.
- Local repo config now uses `gh auth git-credential` as the credential helper
  (osxkeychain held the wrong account's credentials).
- Workflow files changed vs `main` (+318/−35), so the `workflow` scope is
  required for the push — the ProspectorsPlus token has it.
- Workflows on GitHub: `Build Windows installer` (active), `pages-build-deployment`.
- GitHub Pages: **legacy build from `main:/docs`**, live at
  `https://prospectorsplus.github.io/Prospecting-Auto-Pan/`. The live site is
  still the old Prospectors Plus page (with `codes.json` access-code gating).
  The release branch currently has **no `docs/index.html`** — a new site must be
  part of the release commit or pushing `main` breaks the URL.

## Releases

- 18 existing releases, `v1.0.0` … `v4.2.0` (latest). All are old Prospectors
  Plus builds; old binaries may embed the (now revoked) webhook — acceptable
  because the credential is dead.

## Historical secret gate — PASSED

- Full-history sweep (`git log --all -p`) for Discord webhook URLs found
  **exactly one** unique webhook (introduced in `e0f3d4f` as a baked fallback in
  `prospecting_old.py` + config; re-set in `6842a47`). Live probe returned
  **HTTP 404 (Unknown Webhook) — revoked/deleted**. Value never printed or stored.
- `WEBHOOK_SECRET` from the same commits is a payload field that only
  authenticated posts to that (dead) webhook — inert.
- Full-history pattern sweep for GitHub PATs, AWS keys, Slack hooks, OpenAI-style
  keys, and Discord bot tokens: **zero hits**.
- `prospectors-discord-bot/` was **never tracked** in git history.
- Current tracked tree: no webhook/secret material (only a UI placeholder string
  and the release-gate scanner that looks for such material).
- Consequence: the repo is already public with this history and the only
  credential in it is confirmed revoked → no history rewrite required for launch.

## License

- No license file; `LICENSE_CHOICE_REQUIRED.md` tracked. Launch uses
  "source available for inspection" wording; license choice remains an owner task.

## Known working-tree junk (untracked, must stay untracked)

`ACCESS_CODES_PRIVATE.txt`, `prospecting_secrets.json`, `coach_history.json`,
`run_history.json`, `run_logs/`, `onboarding.log`, `onboarding_state.json`,
`node_modules/`, `__pycache__/`, `dist/`, `build/`, `Prospector Studio Docs*`,
`prospectors-discord-bot/`, personal configs and old app bundles.
