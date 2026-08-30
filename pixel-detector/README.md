# pixel-detector

A small native macOS extension: streams the main display through
ScreenCaptureKit and reads the color of one target pixel, exposed to Python
via pybind11. Built for `treasure.py --calibrate` (see `_run_calibrate_mac`
in `../treasure.py`); it is macOS-only and touches nothing in the navigation
loop (see `../DECISIONS.md` D-093 for why).

## Prerequisites (run once by hand)

```sh
xcode-select --install
../.venv/bin/pip install pybind11
```

## Build

```sh
../.venv/bin/python setup.py build_ext --inplace
```

Produces `pixel_detector.cpython-*.so` in this directory. Rebuild after
editing `detector.mm`; the `.so` and `build/` are gitignored.

## API

```python
import pixel_detector

pixel_detector.start_stream(x, y, box_px=1.0)  # logical screen coords; blocks
                                                # until the stream is confirmed
                                                # started (this is where a
                                                # first-run Screen Recording
                                                # prompt is waited out, not
                                                # slept past)
while not pixel_detector.is_ready():  # True once a real frame has landed
    ...
pixel_detector.update_target(x, y, box_px=1.0)  # move/resize without restarting
r, g, b = pixel_detector.get_pixel_color()      # mean over the box_px square
```

`x`/`y` are logical (point) screen coordinates, scaled internally to physical
pixels by `NSScreen.backingScaleFactor`. `box_px` is the side of the
mean-sample square, in the *same* logical-point units as `x`/`y` — `1.0`
reads a single pixel; `treasure.py --calibrate` passes
`DEFAULT_PIXELS.sample_box_px` so it previews exactly what
`CapturedFrame.sample_mean_rgb` would read at that point in production, not
just a nearby pixel. Needs Screen Recording permission granted to whatever
process launched Python.

## Verifying it's reading correctly

Cross-check against a real screenshot: `screencapture -x check.png`, then
compare `PIL.Image.open("check.png").getpixel((x*scale, y*scale))` against
`get_pixel_color()` for a few static, non-animated points. This is how the
extension was validated when it was built — matched within a few units per
channel on desktop wallpaper pixels; a point that changes between the two
reads (an animated icon, scrolling terminal text, a blinking cursor) is not a
bug, it's a pixel that moved.
