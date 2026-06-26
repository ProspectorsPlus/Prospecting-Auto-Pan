# GitHub Release — copy/paste

Draft a new release on GitHub → tag `v2.0.4`. Title + description below.
The installer (`ProspectorsPlusSetup.exe`) is attached automatically by the build.

---

**Tag:** `v2.0.4`
**Title:** `Prospectors Plus v2.0.4 — water walk-back fix`

---

Prospectors Plus v2.0.4 — water walk-back fix

### 🐛 Fixed

- **No more getting stuck after a dig overshoots toward land.** Previously a shake
  that didn't start would still hold W and shove you further onto land, so the
  pan stayed full and the macro bounced S↔W without ever reaching the water.
  Momentum now only kicks in once the shake is actually draining, and the
  walk-back to the water reaches farther each time it misses — so it gets back in
  on its own.

### 🖥️ Update

Click **Update now** in the in-app banner, or download **ProspectorsPlusSetup.exe**
below. Settings, builds, and calibration carry over.
