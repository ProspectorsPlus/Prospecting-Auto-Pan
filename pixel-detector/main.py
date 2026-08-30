# pixel-detector/main.py
#
# Starts the stream, waits for a real frame (never a blind sleep), then polls
# the target pixel at ~60Hz and prints whenever it matches TARGET_RGB.

import time

import pixel_detector

TARGET_XY_LOGICAL = (100.0, 100.0)
TARGET_RGB = (255, 0, 0)
TOLERANCE = 24
POLL_HZ = 60.0


def matches(rgb, target_rgb, tolerance=TOLERANCE):
    return all(abs(c - t) <= tolerance for c, t in zip(rgb, target_rgb, strict=True))


def main():
    x, y = TARGET_XY_LOGICAL
    print(f"Starting stream, target=({x}, {y})...")
    pixel_detector.start_stream(x, y)

    print("Waiting for first frame (this may pause for the Screen Recording prompt)...")
    while not pixel_detector.is_ready():
        time.sleep(0.01)
    print("Stream ready.")

    interval_s = 1.0 / POLL_HZ
    try:
        while True:
            rgb = pixel_detector.get_pixel_color()
            if matches(rgb, TARGET_RGB):
                print(f"MATCH  rgb={rgb}")
            time.sleep(interval_s)
    except KeyboardInterrupt:
        print("\nDone.")


if __name__ == "__main__":
    main()
