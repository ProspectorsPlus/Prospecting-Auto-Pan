# `--calibrate` — previous implementation (pre-GetPixelColor)

Saved before swapping the pixel-read path to the `GetPixelColor` PyPI
package. This is the exact body of `_run_calibrate()` in `treasure.py` as it
stood before the change: it read pixels through the production capture
pipeline (`CaptureService` / `ViewportGuard` / `EvidenceRegistry`) via
`envelope.frame.sample_mean_rgb(...)`, rather than reading the real screen
pixel directly.

```python
def _run_calibrate() -> int:
    """Read the client-relative pixel under the cursor.

    Reports in the **canonical client basis** so the value pastes straight into
    a ``TreasurePixels`` field. It refuses when the Roblox client rect cannot be
    verified, because there is nothing to measure against.
    """
    import time

    from prospector_engine.capture import CaptureService, EvidenceRegistry, ViewportGuard
    from prospector_engine.engine import DEFAULT_PIXELS
    from prospector_engine.ports import create_platform_port

    port = create_platform_port()
    guard = ViewportGuard(port)
    guard.adopt_current()
    capture = CaptureService(
        guard, EvidenceRegistry("calibrate"), source_factory=port.create_capture_source
    )
    capture.start()
    print("CALIBRATE - hover a target inside the Roblox client, Ctrl+C to quit.")
    print("  PIXEL is reported in CANONICAL CLIENT coordinates (physical px from the")
    print("  client area's top-left), which is the basis TreasurePixels uses.")
    try:
        while True:
            envelope = capture.latest()
            cursor = port.cursor_client_px()
            if envelope is None or cursor is None:
                print(
                    "\rRoblox client not found or cursor outside it...            ",
                    end="",
                    flush=True,
                )
                time.sleep(0.1)
                continue
            r, g, b = envelope.frame.sample_mean_rgb(cursor, DEFAULT_PIXELS.sample_box_px)
            geometry = envelope.frame.geometry
            note = "" if geometry.is_canonical else "  [NON-CANONICAL: do not bake this]"
            print(
                f"\rPIXEL=({cursor[0]:>5},{cursor[1]:>5})  RGB=({int(r):>3},{int(g):>3},"
                f"{int(b):>3})  {geometry.state.value}{note}   ",
                end="",
                flush=True,
            )
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\nDone.")
    finally:
        capture.stop()
    return 0
```

## Restoring it

Paste the body above back over `_run_calibrate()` in `treasure.py` to revert
to sampling through the capture pipeline instead of `getpixelcolor`.

## 2026-08-29: this was also the pre-ScreenCaptureKit body

The `getpixelcolor`-based swap described above never actually landed in
`treasure.py` — as of 2026-08-29, `_run_calibrate()` still had exactly the
body shown above (the `CaptureService`/`ViewportGuard` path). That is the
version replaced when `--calibrate` was switched to the new
ScreenCaptureKit + pybind11 extension in `pixel-detector/` (see
`newPixelDetectionSystemAttempt.md` and `pixel-detector/README.md`). "Old"
and "current" were therefore the same code at the time of that change; this
file is the single saved copy of both.
