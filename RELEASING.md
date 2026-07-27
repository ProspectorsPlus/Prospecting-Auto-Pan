# Releasing Prospector Lite

This is the maintainer's checklist for cutting a public release. It is deliberately strict: the project's history contains a leaked secret (see [docs/public-release/SECRET_AUDIT.md](docs/public-release/SECRET_AUDIT.md)), so publication has hard preconditions.

## Hard preconditions for the FIRST public release

These block publication; do not tag a public release until all are done.

1. **License chosen and committed.** See [LICENSE_CHOICE_REQUIRED.md](LICENSE_CHOICE_REQUIRED.md). No license file → nobody may legally redistribute the code.
2. **Fresh git history.** Commit `6842a47` placed a real Discord webhook URL into tracked config files. The public repository must start from a fresh initial commit (or a history rewritten with the owner's explicit approval) — never push the existing history as-is.
3. **Historical secrets revoked.** The webhook URL committed in `6842a47` and the notify-bot secret committed in `e0f3d4f` must be deleted/rotated on their services, regardless of the history decision.
4. **No secret injection in the pipeline (done — keep it that way).** The build pipeline was rewritten so no CI secret or local secrets file can reach a bundled config; the workflows scan the built packages for endpoint/brand strings. Any future change that reintroduces an injection step blocks release.
5. **Tracked tree is clean.** `windows/prospecting_config.json` (the only tracked config) must contain empty `WEBHOOK_URL`/`WEBHOOK_SECRET` and no legacy keys. `prospecting_secrets.json` must remain gitignored and unbundled.
6. **`PROJECT_URL` filled in.** Set the constant in `prospecting_app.py` to the real repository URL so the welcome/About links work, and replace the `<repository URL — to be filled in when published>` placeholders in the docs.

## Release process (every release)

### 1. Prepare

- [ ] Bump `VERSION` in `prospecting_app.py` (currently `1.0.0-rc.2`; confirm the `windows/` copy matches).
- [ ] Update [CHANGELOG.md](CHANGELOG.md) — move changes under the new version heading with the date.
- [ ] Run the full test matrix and require all-pass:

  ```sh
  python3 tour_check.py && python3 finds_sim.py && python3 studio_tests.py \
    && python3 studio_conformance.py && python3 prospecting_selftest.py
  # plus every engine_* suite (see CONTRIBUTING.md)
  python3 public_release_tests.py       # the release gate
  python3 onboarding_trust_tests.py     # the onboarding/trust suite
  ```

  `studio_conformance.py` self-skips with exit 0 when the private Studio goldens
  (a private sibling repository) are absent — expected on public clones and CI.
  A full conformance run happens only on a checkout that has the goldens.

- [ ] Re-run the secret scan on the tree (commands in [SECURITY.md](SECURITY.md)) and confirm no personal config/webhook/key is tracked.

### 2. Tag → CI → artifacts

- [ ] Tag the release: `git tag v<version>` and push the tag. The tag triggers `.github/workflows/release.yml`, which runs the full CI test matrix, builds the Windows installer + portable ZIP on a real Windows runner and the macOS DMG on a macOS runner (both with package-content scans, offline smoke tests, SBOMs and checksums), and attaches everything to a **draft** GitHub release — nothing goes public until a maintainer clicks Publish.
- [ ] Optionally build the macOS DMG locally too (`./build_dmg.command`) and compare package content against the CI artifact.
- [ ] Collect all artifacts into `release/public-candidate/` for the local record.

### 3. Checksums and SBOM

- [ ] Generate `SHA256SUMS.txt` over every artifact:

  ```sh
  cd release/public-candidate
  shasum -a 256 * > SHA256SUMS.txt
  ```

- [ ] Generate a CycloneDX SBOM for the release (e.g. with `cyclonedx-py` against the pinned dependency set) and place it next to the artifacts.
- [ ] Verify the checksums file on a second machine before publishing.

### 4. Publish

- [ ] Draft the GitHub release from the tag; paste the CHANGELOG section as release notes.
- [ ] Attach: macOS DMG, Windows installer, Windows portable ZIP, `SHA256SUMS.txt`, the SBOM.
- [ ] State plainly in the notes that the macOS build is unsigned and how to open it (right-click → Open).
- [ ] After publishing, download each artifact fresh and re-verify its checksum.

### 5. After

- [ ] Confirm the welcome screen of the published build shows the correct version/build identity.
- [ ] Confirm GitHub private vulnerability reporting (Security Advisories) is enabled on the repository, since [SECURITY.md](SECURITY.md) points reporters there.
- [ ] Open a milestone for the next version.

## Signing status

- **macOS**: builds are **ad-hoc signed** by default. Two build env vars change that:

  - `CODESIGN_ID` — a "Developer ID Application" identity; when set, `build_dmg.command` signs with the hardened runtime and the minimal `packaging/entitlements.plist`, then verifies with `codesign` and `spctl --assess`.
  - `PP_PROJECT_URL` — stamps the public repository URL into `build/build_info.json` so in-app "View code" links resolve to the exact commit.

  Notarization + stapling are separate **manual, owner-only** steps — they require the owner's Apple credentials (`APPLE_ID`, `APPLE_TEAM_ID`, and `APPLE_APP_PASSWORD`, an app-specific password). These credentials belong to the owner alone; never commit them or hand them to CI:

  ```sh
  # Owner-only: requires APPLE_ID / APPLE_TEAM_ID / APPLE_APP_PASSWORD in the environment
  xcrun notarytool submit "dist/Prospector Lite.dmg" \
    --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" \
    --password "$APPLE_APP_PASSWORD" --wait
  xcrun stapler staple "dist/Prospector Lite.app"
  spctl --assess --type execute "dist/Prospector Lite.app"
  ```

  Unsigned/unnotarized builds are labeled honestly in-app via the build identity (`signed: false`, `notarized: false`); until signing lands, checksum verification is the integrity mechanism and README/SECURITY say so.

- **Windows**: Authenticode signing runs in CI only when the `WINDOWS_CERT_PFX_B64` and `WINDOWS_CERT_PASSWORD` repository secrets are configured (`signtool sign` with a timestamp server, then `signtool verify` — see `.github/workflows/build-windows.yml`). No certificate exists in the repository. Without those secrets, unsigned builds are produced and SmartScreen may warn; checksum verification is the integrity mechanism.
