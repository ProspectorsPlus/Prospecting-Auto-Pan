# How to write release notes (Prospectors Plus)

Hand this file to Claude (or fill it in yourself) whenever you cut a new version.
It explains the format and gives a copy-paste template. The text you write here
goes in the **GitHub Release description** for the tag.

---

## Auto-update & critical updates

From v2.0.1 onward the app self-updates: on launch it reads `docs/version.json`,
and if the site's `version` is higher than the user's, it shows an **Update now**
banner that downloads the latest installer and runs it silently (upgrades in
place and relaunches). Users don't reinstall manually.

To push an update, you just bump the version + tag a release (below). Two flags
in `docs/version.json` control the banner:

- `"critical": true` — shows a red "Critical update required" banner with no
  "Later" button (still one-click). Use for important fixes. Set back to `false`
  for normal updates.
- `"installer"` — the URL the updater downloads. Leave it as the
  `releases/latest/download/ProspectorsPlusSetup.exe` link; it always points at
  the newest release.

Note: a user only gets one-click auto-update once they're on a build that has the
updater (v2.0.1+). Anyone still on v2.0.0 gets sent to the download page instead.

---

## Versioning (semver: MAJOR.MINOR.PATCH)

- **PATCH** (1.0.0 → 1.0.**1**): bug fixes only, nothing new.
- **MINOR** (1.0.0 → 1.**1**.0): new features, backwards-compatible.
- **MAJOR** (1.0.0 → **2**.0.0): big or breaking changes.

The tag is the version with a `v` in front: `v1.0.1`.

## Bump the version in these 4 places (they must match)

1. `VERSION` in `prospecting_app.py` (Mac)
2. `VERSION` in `windows/prospecting_app.py`
3. `MyAppVersion` in `windows/installer.iss`
4. `version` in `docs/version.json`  ← this is what triggers the in-app "Update available" banner

Then: `git commit -am "release vX.Y.Z"` → `git tag vX.Y.Z` → `git push && git push --tags`.
GitHub Actions builds the installer and creates the Release automatically.

---

## Format rules

- **Title:** `Prospectors Plus vX.Y.Z — short summary`
- Start with a 1–2 sentence **summary** of what this release is.
- Group changes under these headings (skip any that don't apply), newest-impact first:
  - **✨ Added** — new features
  - **⚡ Improved** — changes to existing behaviour
  - **🐛 Fixed** — bug fixes
  - **🧰 Under the hood** — internal/dev changes users don't see
- Each line is one short, plain-English bullet written for players, not coders.
  - Good: "Discord DMs now include pans-per-hour."
  - Avoid: "Refactored post_webhook() to thread the urllib call."
- Put a one-line **upgrade note** at the bottom if users need to do anything.

---

## Copy-paste template

```
Prospectors Plus vX.Y.Z — <short summary>

<One or two sentences describing this release.>

### ✨ Added
- <new feature>

### ⚡ Improved
- <improvement>

### 🐛 Fixed
- <fix>

---
Download the installer below and run it — your settings and saved builds carry over.
If you already have the app, it'll show an "Update available" banner on next launch.
```

---

## Tips for handing this to Claude

Tell Claude: *"Write release notes for vX.Y.Z. Here's what changed: …"* and point
it at this file for the format. Or just paste the `git log` since the last tag
(`git log v<last>..HEAD --oneline`) and ask Claude to turn it into player-friendly
notes using the headings above.
