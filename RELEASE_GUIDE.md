# Prospectors Plus — Release & Update Guide (GitHub-hosted)

Everything is hosted on GitHub: the **download page** runs on GitHub Pages, the
**installer file** lives on GitHub Releases, and the installer is **built in the
cloud** by GitHub Actions — so you never need a Windows PC.

Repo: `ProspectorsPlus/Prospecting-Auto-Pan`
(If you renamed the repo, see "If your repo name is different" at the bottom.)

---

## One-time setup (do this once)

1. **Push this repo to the new account.** Your remote is already set to
   `https://github.com/ProspectorsPlus/Prospecting-Auto-Pan.git`. Push:
   ```bash
   git push -u origin main
   ```

2. **Turn on GitHub Pages.** On GitHub: repo → **Settings → Pages** →
   *Build and deployment* → Source: **Deploy from a branch** →
   Branch: **main**, Folder: **/docs** → Save.

   After a minute your download page is live at:
   **https://prospectorsplus.github.io/Prospecting-Auto-Pan/**

That's the whole setup. The app already points at that Pages URL for update
checks, and the download button already points at your latest GitHub Release.

---

## Cutting a release (each new version)

1. Bump the version in **three** places so they match:
   - `VERSION` in `prospecting_app.py` **and** `windows/prospecting_app.py`
   - `MyAppVersion` in `windows/installer.iss`
   - `version` in `docs/version.json`

2. Commit, tag, push:
   ```bash
   git commit -am "release v1.0.1"
   git tag v1.0.1
   git push && git push --tags
   ```

3. **GitHub Actions builds the installer automatically** and attaches
   `ProspectorsPlusSetup.exe` to a new Release for that tag. Watch it under the
   repo's **Actions** tab. (First run takes a few minutes.)

4. Done. Because the download button uses
   `releases/latest/download/ProspectorsPlusSetup.exe`, it **always** serves the
   newest release — no manual upload. Anyone on an older version sees the
   **"Update available"** banner next time they open the app.

> Tip: to test the build without making a public release, go to **Actions →
> Build Windows installer → Run workflow**. It builds and uploads the exe as an
> *artifact* you can download, without creating a Release.

---

## How updates work (under the hood)

- On launch the app downloads `version.json` from the Pages URL.
- If its `version` is higher than the app's built-in `VERSION`, the blue
  **"Update available → Download"** banner appears. The button opens your
  download page. It's notify-and-link (never a silent self-replace), so it can't
  break an install, and it fails silently when offline.

---

## Building locally instead (optional, needs a Windows PC)

1. Install Python 3 and [Inno Setup 6](https://jrsoftware.org/isdl.php).
2. From the `windows/` folder run `build.bat`.
3. Output: `windows/Output/ProspectorsPlusSetup.exe`.

---

## SmartScreen "unknown publisher" (optional, costs money)

New apps trigger "Windows protected your PC" — users click **More info → Run
anyway** once. To remove it entirely you need a **code-signing certificate**
(~$100–400/yr). Not required; the installer works without it.

---

## If your repo name is different

If you renamed the repo (not `Prospecting-Auto-Pan`) or used a different account,
update these to match and re-tag:
- `UPDATE_MANIFEST_URL` and `DOWNLOAD_PAGE_URL` in both `prospecting_app.py` files
- the `url` in `docs/version.json`
- the download button `href` in `docs/index.html`
- the `git remote set-url origin ...`

The pattern is:
- Pages page:  `https://<account-lowercase>.github.io/<repo>/`
- version.json: `https://<account-lowercase>.github.io/<repo>/version.json`
- installer:    `https://github.com/<Account>/<repo>/releases/latest/download/ProspectorsPlusSetup.exe`

---

## Notes

- Per-user settings, builds, and calibration live in
  `%LOCALAPPDATA%\Prospectors Plus\`, so updates never wipe them.
- The baked-in Discord webhook ships inside the installer, so friends get DMs out
  of the box — they only type their Discord username.
- The Mac app isn't packaged as an installer (it runs from source) but gets the
  same update banner.
