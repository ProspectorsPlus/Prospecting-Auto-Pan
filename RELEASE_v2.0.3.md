# GitHub Release — copy/paste

Draft a new release on GitHub → tag `v2.0.3`. Title + description below.
The installer (`ProspectorsPlusSetup.exe`) is attached automatically by the build.

---

**Tag:** `v2.0.3`
**Title:** `Prospectors Plus v2.0.3 — auto-update + run history`

---

Prospectors Plus v2.0.3 — auto-update + run history

### ✨ New

- **One-click auto-update.** When a new version is out, the app shows an
  **Update now** button that downloads and installs it in place and reopens — no
  manual reinstall. Critical updates show a red banner. (Works from this version on.)
- **Run history that actually saves.** Every finished session is recorded with its
  full stats (pans, pans/hr, runtime, recoveries, nudges, relics, safe/hard stops,
  and why it stopped). Review past runs in the **History** tab, even after closing.
- **More live stats** while running: nudges, relics used, safe-stops, hard-stops,
  and the latest stop reason.

### 🐛 Fixed

- **Run history wasn't saving** — it's now written by the app on stop / exit, so it
  records reliably however you stop.
- **Long runs stay consistent** — fixed the slow drift where shakes/dig-probes
  degraded over time.
- **Safe-stop retries** instead of hard-stopping, so AFK sessions recover on their
  own (hard-stop only after repeated failures; adjustable in Recovery / safety).
- **Per-relic on/off switches** now work individually.

### 🖥️ Install / update

On v2.0.1+? Click **Update now** in the banner. Otherwise download
**ProspectorsPlusSetup.exe** below (More info → Run anyway if Windows prompts).
Settings, builds, and calibration carry over.
