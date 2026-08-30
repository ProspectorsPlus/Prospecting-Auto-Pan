# Window-relative pixel detection

How to check a pixel's color at a coordinate that's *relative to a specific
app's window*, so it keeps working no matter where that window sits on
screen or which monitor/DPI it's on. This is the system Prospector Studio's
`detect_pixel` node and calibration system use to watch the Roblox game
window regardless of where the player has it positioned.

There is no special OS API for "window-relative pixel detection" — it's
three ordinary building blocks composed together:

1. Ask the OS for the target window's on-screen rectangle.
2. Add your stored offset to that rectangle's top-left corner to get an
   absolute screen coordinate.
3. Screenshot just that one point and read its color.

Steps 1 and 3 are OS/library calls; step 2 is a couple lines of arithmetic
you write yourself.

## 1. Find the window's rectangle

You need, in **physical screen pixels**: `(x, y, width, height)` of the
target app's window, refreshed on every check (don't cache it — the window
may move between checks).

### macOS — Quartz window list

```python
import Quartz
import mss

_MSS = getattr(mss, "MSS", None) or mss.mss

def get_scale():
    """Points -> physical pixels (Retina displays report points, not pixels)."""
    with _MSS() as sct:
        main_px_width = sct.monitors[1]["width"]
    bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    return main_px_width / bounds.size.width if bounds.size.width else 1.0

def find_window_rect(owner_substring, exclude_substring=None):
    """Largest on-screen window whose owner name contains owner_substring.
    Returns (x, y, w, h) in physical px, or None."""
    opt = (Quartz.kCGWindowListOptionOnScreenOnly
           | Quartz.kCGWindowListExcludeDesktopElements)
    wins = Quartz.CGWindowListCopyWindowInfo(opt, Quartz.kCGNullWindowID)
    scale = get_scale()
    best, area = None, 0
    for w in wins or []:
        owner = str(w.get("kCGWindowOwnerName", "")).lower()
        if owner_substring.lower() not in owner:
            continue
        if exclude_substring and exclude_substring.lower() in owner:
            continue
        b = w.get("kCGWindowBounds", {})
        a = b.get("Width", 0) * b.get("Height", 0)
        if a > area:
            area, best = a, b
    if not best:
        return None
    return (int(best["X"] * scale), int(best["Y"] * scale),
            int(best["Width"] * scale), int(best["Height"] * scale))
```

- `CGWindowListCopyWindowInfo` enumerates every on-screen window with its
  owning process name and bounds — no need to know the window's title,
  just the owning app's name (`kCGWindowOwnerName`).
- Picking the *largest* matching window filters out an app's small
  helper/menu windows.
- macOS reports window bounds in **points**, not physical pixels, on
  Retina displays — multiply by `get_scale()` or your coordinates will be
  off by exactly the DPI scale factor (1.0, 2.0, etc).
- Dependency: `pyobjc-framework-Quartz` (`pip install pyobjc-framework-Quartz`).

### Windows — EnumWindows

```python
import ctypes
from ctypes import wintypes

# Call once at startup so capture and cursor coordinates agree on
# high-DPI displays (otherwise everything is off by the scale factor).
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_AWARE
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

def find_window_rect(title_substring, exclude_substring=None, class_name=None):
    """Largest visible top-level window matching by class name or title
    substring. Returns (x, y, w, h) in physical px, or None."""
    u = ctypes.windll.user32
    candidates = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lp):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        tbuf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, tbuf, n + 1)
        title = tbuf.value or ""
        cbuf = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cbuf, 256)
        cls = cbuf.value or ""
        matches = (class_name and cls == class_name) or \
                  (title_substring.lower() in title.lower())
        if exclude_substring and exclude_substring.lower() in title.lower():
            matches = False
        if not matches:
            return True
        rc = wintypes.RECT()
        if not u.GetClientRect(hwnd, ctypes.byref(rc)):
            return True
        w, h = rc.right - rc.left, rc.bottom - rc.top
        pt = wintypes.POINT(0, 0)
        u.ClientToScreen(hwnd, ctypes.byref(pt))   # client-area origin -> screen coords
        candidates.append((w * h, int(pt.x), int(pt.y), int(w), int(h)))
        return True

    u.EnumWindows(_cb, 0)
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1:]
```

- `GetClientRect` gives the window's *content area* size, excluding title
  bar/borders — usually what you want, since that's what the app actually
  renders into. `ClientToScreen` converts that client-area origin (which
  is `(0,0)`-relative to the window) into absolute screen coordinates.
  Use `GetWindowRect` instead if you want the outer frame including
  borders/titlebar.
- With per-monitor DPI awareness set, screen capture and window
  coordinates are both already in physical pixels — no extra scale
  factor needed (unlike macOS).
- No extra dependency — `ctypes` is stdlib.

### Cross-platform note

If you want one function signature across platforms, match on whatever's
stable for your target app (owner/process name, window title substring, or
window class name — Roblox's client window class is literally
`"WINDOWSCLIENT"` on Windows) and pick the *largest* match with a minimum
size floor (e.g. skip anything under 320×240) to reject loading splash
screens or tooltips.

## 2. Convert a window-relative point to a screen point

This is the part with no library — just addition. Store your target point
as an **offset from the window's top-left corner**, then add the window's
current position at check-time:

```python
def to_screen(rect, offset_x, offset_y):
    x, y, w, h = rect
    return (x + offset_x, y + offset_y)
```

Two ways to store the offset, trading off what survives:

| Storage | Survives window **move** | Survives window **resize** |
|---|---|---|
| Fixed offset in pixels: `(120, 64)` | Yes | No |
| Fraction of window size: `(0.15, 0.20)` → `x + 0.15*w, y + 0.20*h` | Yes | Yes |

Fixed offsets are simplest and fine if the window size never changes
(fullscreen/fixed-resolution apps). Use the ratio form if users can resize
the window and you want the target point to track proportionally (e.g. a
HUD element pinned to a corner).

## 3. Read the pixel's color

Use [`mss`](https://python-mss.readthedocs.io/) for fast, cross-platform
screen capture — it doesn't need a GUI toolkit and is much faster than
`PIL.ImageGrab` for repeated small grabs:

```python
import mss
import numpy as np

def rgb_at(sct, x, y, box=5):
    """Average (r, g, b) in a box×box region centered on (x, y)."""
    half = box // 2
    region = {"left": int(x) - half, "top": int(y) - half,
              "width": box, "height": box}
    frame = np.asarray(sct.grab(region))   # BGRA
    b, g, r = frame[:, :, :3].reshape(-1, 3).mean(0)
    return (r, g, b)
```

- `sct.grab()` returns pixels in **BGRA** order, not RGB — don't skip the
  channel swap.
- Grabbing a small box (e.g. 5×5) and averaging smooths out anti-aliasing,
  video/compression noise, and off-by-one rounding from the offset math,
  so the comparison below is more forgiving. A 1×1 grab works too — it's
  cheaper and fine for flat, non-anti-aliased UI colors — but a bad frame
  or one-pixel misalignment can flip the result; box-average is more
  robust. If you use one `mss.mss()` context per process and reuse it
  across calls (`sct` passed in above), repeated grabs are cheap — don't
  reopen it per check.

Then compare against a target color with tolerance, since compression and
lighting effects mean colors are rarely bit-exact:

```python
def matches(rgb, target_rgb, tolerance=24):
    return all(abs(c - t) <= tolerance for c, t in zip(rgb, target_rgb))
```

## Putting it together

```python
rect = find_window_rect("Roblox", exclude_substring="Studio")
if rect is None:
    raise RuntimeError("target window not found")

x, y = to_screen(rect, offset_x=120, offset_y=64)

with mss.mss() as sct:
    rgb = rgb_at(sct, x, y)

if matches(rgb, target_rgb=(58, 167, 255), tolerance=30):
    ...  # branch taken
```

Re-run `find_window_rect` (or at least re-fetch a cached rect periodically)
before each check, or on a "did the window move" trigger — that's the
entire trick that makes it "window-relative": nothing is ever hardcoded
to an absolute screen position, it's recomputed from the live window
rectangle every time.

## Dependencies summary

| Purpose | macOS | Windows |
|---|---|---|
| Window lookup | `pyobjc-framework-Quartz` | `ctypes` (stdlib) |
| Screen capture | `mss` | `mss` |
| Pixel math | `numpy` | `numpy` |

```
pip install mss numpy pyobjc-framework-Quartz   # macOS
pip install mss numpy                            # Windows (ctypes is stdlib)
```

## Gotchas to flag for whoever implements this

- **DPI/Retina scaling** is the #1 source of "coordinates are subtly
  wrong" bugs. macOS needs the point→pixel scale multiply; Windows needs
  `SetProcessDpiAwareness` called once at process start before any window
  or capture calls.
- **BGRA vs RGB** channel order from `mss` — easy to get backwards
  silently (colors look plausible but swapped, e.g. blue/red swapped).
- **Client area vs window frame** — decide once whether offsets are meant
  relative to the title-barred outer window or just the content area, and
  be consistent (`GetClientRect`/`kCGWindowBounds` differ in what they
  include).
- **Window-not-found** is a normal, expected state (app not running,
  minimized, or momentarily hidden during a transition) — don't crash,
  return `None`/skip the check and let the caller decide (retry, warn,
  fail the automation step).
