# Verifying your download

How to check that the Prospector Lite you downloaded is the Prospector Lite that was released — before you run it. Five minutes, no special tools.

> The only official download source is the project's GitHub Releases page: <https://github.com/ProspectorsPlus/Prospecting-Auto-Pan/releases>. Anything obtained anywhere else should be checksum-verified against that page — or ignored in favor of building from source yourself (`BUILDING.md`).

## 1. Verify the SHA-256 checksum

Every release publishes a `SHA256SUMS.txt` alongside the artifacts (assembled in CI from the per-platform checksum files — see `.github/workflows/release.yml`). The artifacts are:

- macOS: `ProspectorLite-<version>-macos-<arch>.dmg`
- Windows: `ProspectorLite-<version>-Windows-x64-Setup.exe` (installer) and `ProspectorLite-<version>-Windows-x64-Portable.zip` (portable)

**macOS** (Terminal, in your Downloads folder):

```sh
shasum -a 256 ProspectorLite-*.dmg
# or, with SHA256SUMS.txt downloaded next to it:
shasum -a 256 -c SHA256SUMS.txt --ignore-missing
```

**Windows** (PowerShell):

```powershell
Get-FileHash .\ProspectorLite-*-Windows-x64-Setup.exe -Algorithm SHA256
Get-FileHash .\ProspectorLite-*-Windows-x64-Portable.zip -Algorithm SHA256
```

Compare the output against the line for your file in `SHA256SUMS.txt` **taken from the official release page**, not from wherever you got the binary. Every character must match. A mismatch means the file is not the released build — do not run it, and report where you got it.

## 2. Check the build identity inside the app

After installing, open the **Trust Center** (shield tab) → **Build identity**. It shows, from the `build_info.json` stamped at build time (`lite_trust.py`, `build_identity`):

- **Version** — must match the release you downloaded (e.g. `5.0.0`).
- **Commit** — the exact git commit the build was made from. A "(built from modified source)" marker means the source tree was dirty at build time; released builds should never show it.
- **Signed** / **Notarized** — whether a real code-signing certificate and Apple notarization were used. Current builds honestly show `no` (see below).

The same version and commit appear on the welcome screen and the About section, so you can cross-check without digging.

## 3. What "unsigned" means today

Code signing proves *who* built a binary; a checksum proves *which* binary you have. Until the project has signing certificates, the checksum is your verification — which is why step 1 comes first.

- **macOS:** current builds carry only an ad-hoc signature (no Apple Developer certificate yet), so Gatekeeper will warn on first launch. After — and only after — the checksum matches: right-click the app → **Open** → **Open**. Do **not** disable Gatekeeper system-wide — advice you may find online to do so is exactly what the right-click path makes unnecessary.
- **Windows:** the installer and portable build are unsigned until an Authenticode certificate is set up (the CI signing steps exist but are inactive without one — `.github/workflows/build-windows.yml`), so SmartScreen will likely warn. SmartScreen is a reputation warning, not a malware verdict. After — and only after — the checksum matches: **More info → Run anyway**. Do **not** disable SmartScreen or your antivirus, and distrust any instructions that say to.

When real signing lands, the Build identity rows flip to `yes` and the release notes will say so; nothing about the checksum process changes.

## 4. Tie the binary to the exact source

Prospector Lite's source is available for inspection (no open-source license has been chosen yet — `LICENSE_CHOICE_REQUIRED.md`). Two mechanisms tie your running binary to the exact code:

- **The trust manifest** (Trust Center → Source code): for every OS-sensitive capability, the file, symbol, and exact line range implementing it — generated at build time from the exact source of the build (`python3 lite_trust.py --emit`). A dead reference fails the build, so the manifest always describes the code that actually shipped.
- **View Code**: release builds carry the public repository URL (<https://github.com/ProspectorsPlus/Prospecting-Auto-Pan>), so every View Code button opens the file **pinned to the build's commit** (`.../blob/<commit>/<file>#L<start>-L<end>` — `lite_trust.py`, `source_url_for`) — never a branch that could have moved since. If a build has no URL configured, the app says so and shows the exact local file + symbol + commit instead; it never silently falls back to a moving target.

To go further, `docs/public-release/REPRODUCIBLE_BUILDS.md` describes rebuilding from the same commit and comparing the app tree (the DMG container itself is not byte-reproducible, but the `.app` contents are close to bit-identical).

## Red flags — stop and verify

- The checksum does not match `SHA256SUMS.txt` from the release page.
- The version or commit in Build identity does not match the release.
- The app asks for anything on the never-requested list — microphone, camera, location, administrator rights, Full Disk Access (`PERMISSIONS.md`).
- Any download source, "helper", or guide tells you to disable Gatekeeper, SmartScreen, or your antivirus.
