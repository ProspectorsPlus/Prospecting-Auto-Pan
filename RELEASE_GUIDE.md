# Prospectors Plus — Release & Update Guide

This explains how to ship the standalone Windows installer and how the in-app
"Update available" banner works. You do **not** need a Windows PC — the installer
is built in the cloud by GitHub Actions.

---

## One-time setup

1. **Put these files on your website**, both reachable at the same folder, e.g.
   `https://your-site.com/prospectors/`:
   - `website/index.html`  → the download page
   - `website/version.json` → the manifest the app checks for updates

2. **Point the app at your site.** Edit the three lines at the top of **both**:
   - `prospecting_app.py` (Mac)
   - `windows/prospecting_app.py` (Windows)

   ```python
   VERSION             = "1.0.0"
   UPDATE_MANIFEST_URL = "https://your-site.com/prospectors/version.json"
   DOWNLOAD_PAGE_URL   = "https://your-site.com/prospectors/"
   ```
   Also set the `url` in `website/version.json` to your download page, and the
   `href` of the download button in `website/index.html` to the installer file.

3. **Push this repo to GitHub** (the `.github/workflows/build-windows.yml` file
   makes cloud builds work).

---

## Cutting a release (each new version)

1. Bump the version number in **three** places so they match:
   - `VERSION` in `windows/prospecting_app.py` (and the Mac one)
   - `MyAppVersion` in `windows/installer.iss`
   - `version` in `website/version.json`

2. Commit, then tag and push:
   ```bash
   git commit -am "release v1.0.1"
   git tag v1.0.1
   git push && git push --tags
   ```

3. GitHub Actions builds **Prospectors Plus Setup.exe** automatically and
   attaches it to a Release. Download it from the repo's **Releases** page
   (or the **Actions** run if you used "Run workflow" manually).

4. **Upload `Prospectors Plus Setup.exe` to your website** (the same folder as
   the download page), and update `version.json` on the site to the new version.

That's it. Anyone running an older version sees an **"Update available"** banner
the next time they open the app, with a button that opens your download page.

---

## How updates work (under the hood)

- On launch the app fetches `version.json` from `UPDATE_MANIFEST_URL`.
- If `version` there is higher than the app's built-in `VERSION`, it shows the
  banner. The **Download update** button opens `url` (your download page).
- It's a *notify + link* flow (not silent self-replace), so it can never brick
  an install. If the user is offline the check fails silently.

---

## Building locally instead (optional, needs a Windows PC)

1. Install Python 3 and [Inno Setup 6](https://jrsoftware.org/isdl.php).
2. From the `windows/` folder run `build.bat`.
3. Output: `windows/Output/Prospectors Plus Setup.exe`.

---

## Removing the SmartScreen warning (optional, costs money)

New apps from an unknown publisher trigger "Windows protected your PC"
(users click **More info → Run anyway** once). To remove it entirely you need a
**code-signing certificate** (~$100–400/yr from a CA like DigiCert/Sectigo, or
cheaper via resellers). Once you have a `.pfx`, sign the exe in the workflow with
`signtool`. Not required — the installer works without it.

---

## Notes

- Per-user settings, saved builds, and calibration live in
  `%LOCALAPPDATA%\Prospectors Plus\` so updates never wipe them.
- The baked-in Discord webhook URL/secret ship inside the installer, so friends
  get DMs out of the box — they only type their Discord username.
- The Mac version isn't packaged as an installer here (it runs from source), but
  it gets the same update banner.
