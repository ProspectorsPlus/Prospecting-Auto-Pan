# Prospectors Plus 4.0.0

The biggest update yet — a new tuning experience, a smarter finds detector, a
live overlay, and a machine-locked invite system.

## What's new

**Cycle page.** Your whole pan loop is now drawn as a live diagram — Dig → walk
back (S) → glide (W) → shake → land — with a millisecond **timeline graph**
underneath. Every timing is a slider; drag one and the graph redraws instantly.
Click any bar in the graph to jump straight to the exact setting behind it. The
old scattered engine-tuning tabs are gone.

**Builds page.** All your saved builds in one place: search by name, sort by
newest / oldest / most-used / recently-used, add descriptions, and load or
overwrite with one click. No more long dropdown in the toolbar.

**Shards mode** (Modes → Shards). Exact-click digging for shard farms: click the
dig a fixed number of times, prove it registered by the capacity bar (or the
**green dig-bar**, frames earlier), and — with *assume full* — start walking back
to water *during* the fill animation. Fixes the "digs 2–3 times before the bar
moves" problem on very fast builds.

**Live HUD overlay** (⬒ HUD in the toolbar). A draggable card showing the current
stage in real time (Digging / Hold S / Glide / Shaking / Settling), live stats,
and a find ticker.

**Reworked finds detector.** A fade-direction stack tracker that counts fast
skull bursts and identical duplicates far more accurately, reads coloured tier
text (Cosmic and friends), and never rewrites one find into another.

**Smoother cycles on macOS.** The shake no longer stutters under the analytics
load — the finds OCR pipeline was rebuilt to stop starving the input timing.

**Machine-locked codes.** Access codes now bind to the first computer that
redeems them; copying the config to another machine re-locks it, and codes can
be deactivated remotely by the owner.

## Platform notes

- **Windows**: full automation (dig / shake / movement / recovery / relics /
  Shards / Cycle UI / Builds / HUD). The finds & earnings **OCR dashboards are
  macOS-only** this release (they use Apple Vision); on Windows they simply stay
  off.
- **macOS**: everything, including finds/earnings tracking.

## Install

- **Windows**: download `ProspectorsPlusSetup.exe` from the release and run it.
- **macOS**: open `ProspectorsPlus-4.0.0.dmg`, drag the app to Applications.
  First launch: right-click → Open (unsigned indie app). Needs Python 3.

Existing users see the update banner automatically.
