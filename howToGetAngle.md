# How to get the arrow's angle from the player

The signed heading error of the equipped treasure map's arrow, relative to
the player's forward direction, is `DirectionObservation.error_deg`
(`prospector_engine/contracts.py:1195`). This is the same number shown in
the GUI's "Alignment error" readout (`treasure_gui.py:1168`).

It comes from `PerceptionPipeline.observe(...)`
(`prospector_engine/navigation.py`), which returns a `NavigationInputs`
whose `.direction` field is that `DirectionObservation`.

## Minimal call

```python
from prospector_engine.navigation import Navigator, NavigationCapabilities, PerceptionPipeline
from prospector_engine.vision import ArrowSegmenter, load_profiles

profile = load_profiles().get("yellow_map_v0")
navigator = Navigator(capabilities=NavigationCapabilities.observing(
    os_name=sys.platform, profile_id=profile.profile_id
))
pipeline = PerceptionPipeline(segmenter=ArrowSegmenter(profile))

inputs = pipeline.observe(frame, map_id="replay", approach_valid=False)  # frame: CapturedFrame
angle = inputs.direction.error_deg  # signed deg, arrow relative to player forward
```

## Notes

- `error_deg` is `None` unless `inputs.direction.valid` is `True` — check
  that first (mirrors the GUI's own guard at `treasure_gui.py:1169`).
- Sign convention: positive/negative per `wrap_deg` in `heading.py`. To get
  the absolute bearing instead of the relative error:
  `desired_deg = wrap_deg(forward_deg + error_deg)` (`navigation.py:2003`).
- You need a real `CapturedFrame` to call `.observe()`. Two read-only ways
  to get one without touching Roblox:
  - `treasure.py --replay DIR` — replays a recorded session; see
    `treasure.py:253` (`_run_replay`) for the exact `CapturedFrame`
    construction.
  - `treasure.py --capture-probe` — live screen capture, read-only, needs
    Screen Recording permission.
- Do not build this against `--forward-probe` or `--native-control-probe` —
  those emit real input into Roblox and are gated per `CLAUDE.md` rule 1.
