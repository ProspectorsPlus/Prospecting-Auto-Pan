# GitHub Release — copy/paste

Draft a new release on GitHub → tag `v2.0.1`. Title + description below.
The installer (`ProspectorsPlusSetup.exe`) is attached automatically by the build.

---

**Tag:** `v2.0.1`
**Title:** `Prospectors Plus v2.0.1 — stability & quality-of-life`

---

Prospectors Plus v2.0.1 — stability & quality-of-life

A focused update fixing long-session reliability and adding things you asked for.
Update in-app or grab the installer below.

### 🐛 Fixed

- **Long runs stay consistent.** Fixed the slow degradation where, the longer it
  ran, shakes and dig-probes would drift or stop working. (Memory is no longer
  allowed to pile up, and any stray held key/click is cleared every cycle.)
- **Relic on/off switches work.** You can now enable or disable each relic row
  individually, not just its timer.

### ✨ New

- **One-click auto-update.** When a new version is out, the app shows an update
  banner with an **Update now** button that downloads and installs it in place and
  reopens — no manual reinstall. (Takes effect from this version onward.)
- **Safe-stop now retries instead of stopping.** If it hits a hazard or gets
  stuck while you're AFK, it pauses, waits, and tries again — only hard-stopping
  after several failed retries in a row. Wait time and retry count are adjustable
  in Recovery / safety.
- **Run history.** Every finished session is saved with its stats, so you can
  review past runs anytime in the new **History** tab — even after closing the app.
- **More stats.** Live counters now include nudges, relics used, safe-stops,
  hard-stops, and the reason the macro last stopped (manual / safe / auto / bag).

### 🖥️ Install / update

Already on v2.0.0? You'll see an "Update available" banner — click it. Otherwise
download **ProspectorsPlusSetup.exe** below (More info → Run anyway if Windows
prompts). Your settings, builds, and calibration carry over.
