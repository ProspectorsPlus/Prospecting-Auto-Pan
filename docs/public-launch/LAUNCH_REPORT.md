# Prospector Lite 5.0.0 — Public Launch Report (2026-08-08)

The first public stable release of Prospector Lite. Companion documents:
`STARTING_STATE.md` (pre-launch snapshot) and
`../public-release/SECRET_AUDIT.md` (launch-day secret-gate evidence).

## Result

| Item | Value |
|---|---|
| Version / tag | **5.0.0** / `v5.0.0` |
| Release commit | `e55bf9336e86e08aec58e3e136796d7a4be09b68` |
| Release | <https://github.com/ProspectorsPlus/Prospecting-Auto-Pan/releases/tag/v5.0.0> — published 2026-08-08T20:38:33Z, marked Latest |
| Website | <https://prospectorsplus.github.io/Prospecting-Auto-Pan/> — rebuilt, deployed from `main:/docs` |
| Default branch | `main` fast-forwarded to the release commit (no history rewrite) |

## Why 5.0.0 (not 1.0.0)

Tags `v1.0.0`–`v4.2.0` already exist publicly for the old pre-rebrand
"Prospectors Plus" releases in this same repository. Re-using `v1.0.0`
would have required destroying an existing public release; a second
"1.0.0" would have sorted *below* 4.2.0 for every user comparing
versions. The audited `1.0.0-rc.x` line therefore shipped as **5.0.0**,
keeping the tag lineage monotonic.

## Artifacts (all built by `release.yml` from the tagged commit)

sha256 checksums as published in the release's `SHA256SUMS.txt`:

- `ProspectorLite-5.0.0-macos-arm64.dmg` — `878cbff01e8b…` (unsigned, not notarized)
- `ProspectorLite-5.0.0-Windows-x64-Setup.exe` — `242081f0dea0…` (unsigned)
- `ProspectorLite-5.0.0-Windows-x64-Portable.zip` — `b0c035b9cd36…` (unsigned)
- `prospector-lite-5.0.0-source.tar.gz` — `01bb47f3a888…` (git archive of `v5.0.0`)
- plus `SHA256SUMS.txt`, `RELEASE_NOTES.md`, `release-manifest.json`,
  `sbom-macos.cdx.json`, `sbom-windows.cdx.json`

Verification performed before publishing:

- CI (full test matrix) green on macOS **and** Windows at the release
  commit — including the first-ever complete Windows runs of every suite.
- Both platform builds green in the tag-triggered `release.yml` run;
  Windows acceptance probes (`windows_acceptance.ps1`) ran on a real
  Windows runner (elevated-runner caveat stated by the script).
- The released DMG was downloaded and passed the full local
  `packaged_acceptance.command` (offline boot from the read-only image,
  0 network sockets, bundle content, welcome-pref lifecycle).
- Embedded `build_info.json` in both platform bundles: version `5.0.0`,
  commit `e55bf93…`, `dirty=false`, public `project_url`.
- All three platform artifacts re-downloaded anonymously from the
  public release URLs; hashes match `SHA256SUMS.txt` and the locally
  verified copies byte-for-byte.
- `public_release_tests.py` (the release gate) ALL PASS at the release
  commit, including the release-artifact scan.

## Security / privacy gate

- The single Discord webhook present anywhere in git history probes
  **HTTP 404 (revoked)**; no other credential shapes exist in history
  (full `git log --all -p` sweep). `prospectors-discord-bot/` was never
  tracked; `prospecting_secrets.json` was never committed.
- The history was already public since June 2026, so a fresh-history
  rewrite would have protected nothing; publication proceeded on the
  existing history (documented in `SECRET_AUDIT.md`).
- The app makes zero network requests by default; the packaged
  acceptance re-confirmed 0 sockets on boot. No access codes, no
  IP/location endpoints, no hidden updater. The new website is static,
  self-contained (no external fonts, no analytics).

## CI fixes the first real runs forced (all landed pre-tag)

1. `secrets` context in step `if:` made `build-windows.yml` unparseable
   (undispatchable) → job-env presence flag.
2. The inline `python -c` identity stamp failed under the runner shell
   and the failure was swallowed → tracked `packaging/stamp_build_info.py`
   with explicit existence checks.
3. Frozen engine import created the per-user data dir even when
   `PP_DATA_DIR` pointed elsewhere → `PPENGINE_HOME` pinned to
   `DATA_DIR` (the Windows acceptance leak probe caught it; the macOS
   equivalent was latent and verified fixed in a frozen build).
4. Parity goldens embedded darwin-only OCR capabilities → the batch
   scenarios script `findsOcr`/`earningsOcr`, making transcripts
   platform-identical.
5. Windows `cp1252` consoles vs UTF-8 output → `PYTHONUTF8=1`.
6. Wall-clock-sensitive suites (`engine_pacing_tests`, the lite-drive
   real-screen-grab assertion) self-skip honestly on hosted runners.
7. Hosted runners are always elevated → `PP_ACCEPT_ELEVATED=1` (CI only)
   in the Windows acceptance script.

## Still owner-only (unchanged)

- **License choice** — deliberately deferred; all public wording is
  "source available for inspection", no redistribution grant.
- **Apple Developer signing + notarization** (macOS builds are ad-hoc).
- **Windows Authenticode** (CI signing steps activate when the
  `WINDOWS_CERT_PFX_B64`/`WINDOWS_CERT_PASSWORD` secrets exist).
- Optional: a live-game validation pass.
