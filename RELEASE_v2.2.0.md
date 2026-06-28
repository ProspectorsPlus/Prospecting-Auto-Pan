# GitHub Release — copy/paste

Draft a new release on GitHub → tag `v2.2.0`. Title + description below.
The installer (`ProspectorsPlusSetup.exe`) is attached automatically by the build.

---

**Tag:** `v2.2.0`
**Title:** `Prospectors Plus v2.2.0 — easy tuning + smoother movement`

---

Prospectors Plus v2.2.0 — easy tuning + smoother movement

### ✨ New

- **Easy tuning tab.** Plain-language tweaks — "go further back into the water",
  "go further onto land", "wait longer before the shake / first dig", "wait before
  heading back to water". The macro works out which underlying timings to adjust,
  so you tune by intent. (It applies on the next Start; the Run log shows an
  `[easy] …` line confirming what was set.)
- **Smarter X pattern.** Now one smooth diagonal walk-back with built-in drift
  correction — it picks the side that pulls back toward centre, so it never wanders
  left/right over long sessions.

### 🐛 Fixed

- **Dig overshoot no longer traps the macro on land** — the walk-back reaches
  farther on each miss until it gets back to the water.
- **Discord notifications** could stop if a config got blanked — the URL/secret are
  now baked-in fallbacks so DMs keep working.

### Also included since 2.1.0

- Smart auto-timing (experimental), run history, expanded stats, safe-stop retry,
  per-relic toggles, and the rustic look.

### 🖥️ Update

Click **Update now** in the in-app banner, or download **ProspectorsPlusSetup.exe**
below. Settings, builds, and calibration carry over.
