"""Prospectors Plus — Fable engine.

A ground-up rebuild of the auto-pan engine for the Roblox game *Prospecting*.

Design pillars (learned from the v2/v3 engine's telemetry):
  * Perception is CONTINUOUS (background thread), actions are event-driven —
    no per-call screen grabs, no blind sleeps where sensing stops.
  * The capacity bar is ground truth. Cues glitch; only the Pan cue is
    trusted for "am I in the water", and every cue is debounced.
  * Every action verifies its own outcome and reports a structured result.
  * The shake confirms it actually STARTED (drain-rate / cue) and
    micro-retries in ~100 ms instead of losing a whole cycle.
  * Landing is cue-confirmed, not momentum-guessed.
  * Every "N failures / N seconds before X" threshold treats 0 as DISABLED.
  * The whole brain runs headless against a game simulator for testing.
"""

__version__ = "4.0.0"   # Prospectors Plus 4.0.0 (Fable engine)
