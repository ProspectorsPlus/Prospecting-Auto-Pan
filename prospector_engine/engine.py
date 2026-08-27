#!/usr/bin/env python3
"""
Treasure macro -- minimal dig loop.

THE IDEA
    Roblox window is pinned to a fixed 1280x720 size (see platform_mac.
    pin_window / platform_win.pin_window). Every baked *_PIXEL constant is
    ROBLOX-WINDOW-RELATIVE, not screen-absolute -- an (x, y) offset from the
    window's own live top-left corner, resolved fresh every time it's used
    (see _screen_point()/State.window_origin). That's what makes one baked
    coordinate set correct across machines: menu bar height, notch, whatever
    else shifts where the pinned window actually lands on screen doesn't
    matter, because the offset is measured live instead of assumed.

    While running: if BOTH dig-spot check pixels match their calibrated
    reference color (within DIG_SPOT_TOL_PCT), tap the dig button (a fixed
    1ms hold -- an instant click, not a timed hold). If the capacity bar
    reads full, stop tapping (pan-swap isn't implemented yet -- that's a
    later feature).

    Why two points instead of one "ready" cue pixel: a single UI-cue pixel's
    brightness turned out to drift with lighting/animation state in ways
    that didn't reliably track actual dig eligibility (it read both way
    under and way over its own threshold on the same coordinate, depending
    on unrelated screen state). Two terrain-color check points, both
    required, calibrated directly off a CONFIRMED valid dig spot, is more
    robust than one brightness threshold.

    The run loop is organized as phases:
        Phase 1 -- digging        (dig_tap / on_dig_spot, automatic, every tick)
        Phase 2 -- reset character (request_reset_character, manual/F4 only)
        Phase 3 -- pan swap        (pan_swap, automatic when capacity reads
                                    full, also standalone-testable via F5)

Hotkeys:
    F1  -> find/pin the Roblox window and start the loop
    F2  -> stop the loop
    F3  -> pop up the pixel + color under the cursor right now
    F4  -> reset-character test sequence (see request_reset_character)
    F5  -> pan-swap test sequence (see request_pan_swap_test) -- runs the
           swap once on its own, without needing capacity to actually be full

    NOTE: F5, like F1-F4, needs to be registered in platform_mac.py /
    platform_win.py's make_listener() -- that wiring lives outside this file
    and isn't included here, so add HOTKEY_PAN_SWAP_TEST -> request_pan_swap_test
    there the same way the existing hotkeys are wired.

There is deliberately no quit hotkey: the F4 sequence taps Escape as its
first step, and Escape going through the same OS-level input pipe the
hotkey listener watches would stop the listener mid-sequence. Quit via
the GUI's close button, or Ctrl+C in the CLI.

SETUP
    macOS:   pip3 install pyobjc-framework-Quartz pyobjc-framework-ApplicationServices mss numpy pynput --break-system-packages
             Grant Terminal (or your launcher) both Accessibility and Screen
             Recording in System Settings > Privacy & Security.
    Windows: pip install mss numpy

RUN
    python3 treasure.py                launches the GUI
    python3 treasure.py --calibrate    hand-derive DIG_SPOT_A/B_PIXEL+COLOR /
                                        CAP_FULL_PIXEL for a new layout (or
                                        just use F3 in-game, see below)

One engine source serves both platforms: screen capture (mss) and colour
detection are shared here; window lookup/pin, mouse input, and the hotkey
listener live in prospector_engine/platform_mac.py or platform_win.py.
"""
import sys
import threading
import pyautogui
import Quartz
import time

if sys.platform == "win32":
    from prospector_engine import platform_win as _plat
else:
    from prospector_engine import platform_mac as _plat

np = _plat.np
mss = _plat.mss
_MSS = _plat._MSS
globals().update(_plat.ENGINE_GLOBALS)
globals().update(_plat.PLATFORM_SEAMS)
_plat.bind(sys.modules[__name__])

get_scale = _plat.get_scale
find_window_origin = _plat.find_window_origin
find_roblox_rect = _plat.find_roblox_rect
mouse_down = _plat.mouse_down
mouse_up = _plat.mouse_up
move_relative = _plat.move_relative
key_down = _plat.key_down
key_up = _plat.key_up
v3_keycode = _plat.v3_keycode
make_listener = _plat.make_listener
pin_window = _plat.pin_window
show_popup = _plat.show_popup
set_clipboard = _plat.set_clipboard

# The window title/owner substring platform_*.find_window_origin() matches.
ROBLOX_TITLE = "Roblox"

# ============================================================================
# CONFIG
# ============================================================================

# --- Fixed window geometry: pin_window() places Roblox here at launch -------
WINDOW_W = 1280
WINDOW_H = 720

# --- ROBLOX-RELATIVE COORDINATES ---------------------------------------------
# Every *_PIXEL constant below is an (x, y) offset in PHYSICAL pixels from the
# Roblox window's own top-left corner -- NOT a screen-absolute coordinate.
# (0, 0) is the window's top-left, wherever that happens to sit on screen.
#
# Why: the window's on-screen position after pin_window() depends on things
# that vary per Mac -- menu bar height, notch, whatever -- so a screen-
# absolute pixel baked on one machine silently drifts on another. Window-
# relative coordinates sidestep that entirely: --calibrate/F3 report the
# cursor's position relative to the CURRENT live Roblox window (see
# find_window_origin()), and every read/click at runtime adds that same live
# origin back on (see _screen_point() below) -- so the offset is measured
# fresh every time, never assumed or hardcoded.
#
# Re-derive with F3 or --calibrate per layout; both refuse to report a pixel
# unless Roblox is actually found on screen.

# --- Dig: fixed instant tap, gated by two terrain-color check points --------
# Both points sampled (via F3) while standing on a CONFIRMED valid dig spot.
# Both must independently match their reference color within DIG_SPOT_TOL_PCT
# for on_dig_spot() to fire -- re-derive per layout with F3 or --calibrate.
DIG_SPOT_A_PIXEL  = (559, 614)
DIG_SPOT_A_COLOR  = (51, 51, 51)
DIG_SPOT_B_PIXEL  = (556, 607)
DIG_SPOT_B_COLOR  = (201, 201, 201)
DIG_SPOT_TOL_PCT  = 10.0            # per-channel tolerance, as % of the 0-255 range
WHITE_MIN         = 175             # r,g,b must all be >= this to count "white" (diagnostic only)
DIG_HOLD_MS       = 1               # fixed instant tap -- not meant to change

# --- Capacity: stop digging when the pan reads full --------------------------
CAP_FULL_PIXEL    = (799, 542)      # (x, y) -- gray empty / yellow full
YEL_MIN           = 140             # r and g must both be >= this
YEL_BLUE_GAP      = 45              # ...and blue must be <= min(r,g) - this

SAMPLE_BOX        = 6               # NxN px box averaged around a watched pixel

# --- Pan swap: swap a full pan for an empty one, then confirm it's re-equipped --
PAN_SWAP_BUTTON_PIXEL      = (442, 195)   # opens the pans menu
PAN_SWAP_FIRST_PAN_PIXEL   = (508, 255)   # top pan slot in the list
PAN_SWAP_BOTTOM_PAN_PIXEL  = (514, 531)   # bottom (empty) pan slot, after scrolling
PAN_SWAP_EQUIP_PIXEL       = (769, 515)   # equip button -- same spot for either slot
PAN_SWAP_CHECK_PIXEL       = (500, 547)   # sampled after re-equip to confirm pan is out
PAN_SWAP_CHECK_COLOR       = (140, 140, 140)
PAN_SWAP_CHECK_TOL_PCT     = 5.0
PAN_SWAP_SCROLL_COUNT      = 20           # number of scroll-down ticks to reveal the list
PAN_SWAP_SCROLL_DURATION_S = 0.25          # ...spread over this many seconds
PAN_SWAP_CLICK_PRE_MS      = 100          # pre/post delay for each click_buffered() call
PAN_SWAP_CLICK_POST_MS     = 100
PAN_SWAP_MAX_ATTEMPTS      = 5            # safety cap -- don't retry forever if the
                                           # equip check never reads correctly

# --- Pan swap: initial gate before the swap sequence proper starts -----------
# Repeatedly press 1 and wait, until PAN_SWAP_START_CHECK_PIXEL reads
# something OTHER than PAN_SWAP_START_CHECK_COLOR -- see pan_swap().
PAN_SWAP_START_CHECK_PIXEL   = (540, 523)
PAN_SWAP_START_CHECK_COLOR   = (194, 55, 27)
PAN_SWAP_START_CHECK_TOL_PCT = 10.0
PAN_SWAP_START_POLL_S        = 0.25

# --- Window-origin resync: how often the run loop re-measures the live
# Roblox window position while running, so a mid-run drag/drift self-heals
# instead of only correcting on the next F1. ---------------------------------
WINDOW_RESYNC_S = 1.0

# --- Hotkeys ------------------------------------------------------------------
# fn+F1/F2 register as plain F1/F2 key codes at the OS level (fn is a hardware
# modifier that swaps media keys for the standard F-key, not a tracked
# modifier key) -- so these binds don't need ctrl/alt/shift.
HOTKEY_START      = {"ctrl": False, "alt": False, "shift": False, "code": "F1"}
HOTKEY_STOP       = {"ctrl": False, "alt": False, "shift": False, "code": "F2"}
HOTKEY_PIXEL_INFO = {"ctrl": False, "alt": False, "shift": False, "code": "F3"}
HOTKEY_RESET_CHARACTER = {"ctrl": False, "alt": False, "shift": False, "code": "F4"}
HOTKEY_PAN_SWAP_TEST = {"ctrl": False, "alt": False, "shift": False, "code": "F5"}

LOOP_POLL_S = 0.01   # how often the run loop re-checks the screen when idle

# --- Reset-character test sequence (F4) --------------------------------------
RESET_POST_ENTER_MS = 8000    # wait after the click sequence, before the right-click drag
RESET_DRAG_MS       = 1000    # total duration of the straight-down drag
RESET_DRAG_STEP_MS  = 16      # ~60Hz step rate while dragging
RESET_DRAG_STEP_PX  = 12      # px per step -- "decent speed" (~750px/s)


# ============================================================================
# Colour tests
# ============================================================================
def is_white(r, g, b):
    return r >= WHITE_MIN and g >= WHITE_MIN and b >= WHITE_MIN


def is_yellow(r, g, b):
    return r >= YEL_MIN and g >= YEL_MIN and b <= min(r, g) - YEL_BLUE_GAP


def color_close(rgb, target, tol_pct=DIG_SPOT_TOL_PCT):
    """True if every channel of rgb is within tol_pct% of the 0-255 range
    of the matching channel in target."""
    tol = tol_pct / 100.0 * 255
    return all(abs(a - t) <= tol for a, t in zip(rgb, target))


def _resolve_origin(retries=5, delay=0.05):
    """Find the Roblox window's current top-left in PHYSICAL pixels --
    retried briefly since the window can take a moment to settle right after
    pin_window() moves/resizes it. Returns None if Roblox isn't found."""
    for _ in range(retries):
        o = find_window_origin()
        if o:
            return o
        time.sleep(delay)
    return None


def _screen_point(x_rel, y_rel):
    """Convert a ROBLOX-WINDOW-RELATIVE physical-pixel coordinate (the space
    every *_PIXEL constant and --calibrate/F3 report live in) into a
    screen-absolute physical-pixel coordinate, using the live window origin
    -- never a saved/assumed one, so this can't double-count or go stale."""
    ox, oy = State.window_origin or (0, 0)
    return x_rel + ox, y_rel + oy


# ============================================================================
# Capture + detection
# ============================================================================
class Detector:
    """Samples the ROBLOX-WINDOW-RELATIVE *_PIXEL constants, resolved
    against `origin` (a live screen-absolute physical-pixel window origin --
    see _resolve_origin()/_screen_point()). Call set_origin() to re-anchor
    after a window-origin resync; regions are only rebuilt when it actually
    changes, so this stays cheap to call every tick."""

    def __init__(self, sct, origin=(0, 0)):
        self.sct = sct
        self.origin = origin
        self._build_regions()

    def _build_regions(self):
        self.dig_a_region = self._box(DIG_SPOT_A_PIXEL)
        self.dig_b_region = self._box(DIG_SPOT_B_PIXEL)
        self.cap_region = self._box(CAP_FULL_PIXEL)

    def set_origin(self, origin):
        if origin != self.origin:
            self.origin = origin
            self._build_regions()

    def _box(self, pixel):
        x, y = pixel[0] + self.origin[0], pixel[1] + self.origin[1]
        h = SAMPLE_BOX
        return {"left": x - h // 2, "top": y - h // 2, "width": h, "height": h}

    def _rgb(self, region):
        img = np.asarray(self.sct.grab(region))[:, :, :3]   # BGRA -> BGR
        b, g, r = img.reshape(-1, 3).mean(0)
        return r, g, b

    def on_dig_spot(self):
        """True when BOTH terrain check points currently match their
        calibrated reference color (within DIG_SPOT_TOL_PCT) -- a diggable
        state is on screen right now."""
        return (color_close(self._rgb(self.dig_a_region), DIG_SPOT_A_COLOR)
                and color_close(self._rgb(self.dig_b_region), DIG_SPOT_B_COLOR))

    def capacity_full(self):
        return is_yellow(*self._rgb(self.cap_region))


def _sample_rgb(pixel, box=SAMPLE_BOX):
    """Grab a fresh NxN screen sample centered on the ROBLOX-WINDOW-RELATIVE
    `pixel` (resolved against the live window origin, same as click_buffered)
    and return its averaged (r, g, b). Opens its own short-lived mss context
    -- fine for occasional calls (hotkey tests, pan-swap gating) but the hot
    dig loop uses Detector's cached regions instead, not this."""
    x, y = _screen_point(*pixel)
    with _MSS() as sct:
        h = box
        region = {"left": x - h // 2, "top": y - h // 2, "width": h, "height": h}
        img = np.asarray(sct.grab(region))[:, :, :3]
        b, g, r = img.reshape(-1, 3).mean(0)
    return r, g, b


# ============================================================================
# Quartz-level right-click-drag (macOS)
#
# Games read raw HID motion *deltas*, not the cursor's absolute position, for
# mouselook-style camera dragging -- posting a plain move/absolute-position
# event does nothing in Roblox even while the button is genuinely held down.
# kCGMouseEventDeltaX/Y is the field that actually drives it.
# ============================================================================
def _get_mouse_location():
    return Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))


def _right_click_down():
    loc = _get_mouse_location()
    event = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventRightMouseDown, loc, Quartz.kCGMouseButtonRight)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _right_click_up():
    loc = _get_mouse_location()
    event = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventRightMouseUp, loc, Quartz.kCGMouseButtonRight)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _right_drag_step(dx, dy):
    loc = _get_mouse_location()
    event = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventRightMouseDragged, loc, Quartz.kCGMouseButtonRight)
    Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventDeltaX, dx)
    Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventDeltaY, dy)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _right_drag_relative(dx, dy, steps, step_delay):
    """Split a total (dx, dy) motion into `steps` posted delta events,
    `step_delay` apart -- mirrors RESET_DRAG_MS/RESET_DRAG_STEP_MS/PX."""
    step_dx = dx / steps
    step_dy = dy / steps
    for _ in range(steps):
        _raise_if_aborted()
        _right_drag_step(step_dx, step_dy)
        time.sleep(step_delay)

def _left_click_down():
    loc = _get_mouse_location()
    event = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventLeftMouseDown, loc, Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _left_click_up():
    loc = _get_mouse_location()
    event = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventLeftMouseUp, loc, Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _get_scale():
    """Same computation --calibrate/F3 use (get_scale() against a fresh MSS
    monitor read) -- the single source of truth for physical-pixel <-> point
    conversion, so clicks land on the exact spot those tools showed."""
    with _MSS() as sct:
        return get_scale(sct) or 1.0


def _move_absolute(x, y):
    """(x, y) are PHYSICAL pixels -- the same space --calibrate/F3/Detector
    read from. Quartz's CGEvent location field is in POINTS, so convert
    before posting (mirrors platform_mac.move_cursor's x/scale, y/scale)."""
    s = _get_scale()
    px, py = x / s, y / s
    event = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventMouseMoved, (px, py), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _scroll_down_step(lines=1):
    """Post one scroll-wheel event, `lines` notches downward (negative
    wheel1 == down, matching natural/non-natural scroll direction as Quartz
    sees it at the HID level)."""
    event = Quartz.CGEventCreateScrollWheelEvent(
        None, Quartz.kCGScrollEventUnitLine, 1, -lines)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _scroll_down(count, duration_s):
    """Post `count` scroll-down events spread evenly across duration_s
    seconds -- e.g. _scroll_down(20, 1.0) for ~20 scroll-downs in 1 second."""
    if count <= 0:
        return
    delay = duration_s / count
    for _ in range(count):
        _raise_if_aborted()
        _scroll_down_step()
        time.sleep(delay)

# ======
# Helpers for mouse actions
# ======

def move_absolute(x, y):
    """(x, y) are PHYSICAL pixels, same conversion as _move_absolute()."""
    s = _get_scale()
    pyautogui.moveTo(x / s, y / s)

def click_buffered(x, y, pre=50, post=50):
    """
    (x, y) are ROBLOX-WINDOW-RELATIVE physical pixels -- the same space
    --calibrate/F3 report and DIG_SPOT_*/PAN_SWAP_* are baked in; resolved
    against the live window origin here, same as _sample_rgb()/Detector.
    pre/post are in milliseconds, matching the AHK version.
    """
    x, y = _screen_point(x, y)
    _raise_if_aborted()
    _interruptible_sleep(pre / 1000)
    _move_absolute(x - 1, y - 1)
    _interruptible_sleep(0.1)  # 100ms
    _move_absolute(x, y)
    _left_click_down()
    _left_click_up()
    _interruptible_sleep(post / 1000)


# ============================================================================
# Run state + hotkeys
# ============================================================================
class State:
    running = False
    alive = True
    resetting = False
    pan_swapping = False
    window_origin = None      # live (x, y) screen-absolute physical-pixel
                               # top-left of the Roblox window, set by
                               # request_start() and kept fresh by run()'s
                               # periodic resync -- every ROBLOX-relative
                               # *_PIXEL constant is resolved against this
    abort_requested = False   # set by request_stop() / request_quit(); cleared
                               # by request_start(); checked throughout any
                               # in-flight sequence (pan_swap, reset-character)
                               # so F2 actually cancels them instead of just
                               # gating the next tick() call


_MOUSE_DOWN = False
_HELD_KEYS = set()


class MacroStopped(Exception):
    """Raised internally to unwind an in-flight sequence (pan_swap,
    reset-character) the moment State.abort_requested or State.alive goes
    false. Never meant to escape the function that catches it."""


def _raise_if_aborted():
    if not State.alive or State.abort_requested:
        raise MacroStopped()


def _interruptible_sleep(seconds):
    """Like time.sleep(seconds), but checked in small slices so a stop
    request lands within one slice instead of after the full duration --
    this is what makes F2 actually cut off a multi-second wait mid-sequence
    (e.g. the 8s post-Enter wait in reset-character) instead of only taking
    effect on the *next* tick."""
    end = time.time() + seconds
    slice_s = 0.05
    while True:
        _raise_if_aborted()
        remaining = end - time.time()
        if remaining <= 0:
            return
        time.sleep(min(slice_s, remaining))


def request_start(origin="hotkey"):
    """Find + pin the Roblox window, resolve its live origin (every
    ROBLOX-relative *_PIXEL constant is resolved against this), then start
    the dig loop. Bound to F1."""
    ok, msg = pin_window(WINDOW_W, WINDOW_H)
    print(f"[{origin}] {msg}")
    if not ok:
        return
    o = _resolve_origin()
    if not o:
        print(f"[{origin}] pinned, but couldn't re-find the Roblox window "
              f"afterward -- not starting")
        return
    State.window_origin = o
    State.abort_requested = False
    State.running = True
    print(f"[{origin}] started (window origin {o})")


def request_pixel_info(origin="hotkey"):
    """Sample the pixel under the cursor right now using the EXACT same box
    size + averaging the live Detector uses (same SAMPLE_BOX) -- this
    reports what the running macro would actually see at this spot, not
    just a raw color swatch. Reported PIXEL is ROBLOX-WINDOW-RELATIVE (the
    live window origin subtracted off), so it pastes straight into
    DIG_SPOT_A/B_PIXEL+COLOR / CAP_FULL_PIXEL / PAN_SWAP_* with no manual
    offsetting -- refuses (no popup, no clipboard write) if Roblox isn't
    currently found on screen, since there's no origin to measure against.
    The WHITE/YELLOW tags are diagnostic only (YELLOW matches what
    capacity_full() checks; WHITE isn't used for dig gating anymore -- see
    on_dig_spot()/color_close()). Bound to F3."""
    win_origin = find_window_origin()
    if not win_origin:
        msg = "Roblox window not found on screen -- can't compute a relative pixel"
        print(f"[{origin}] {msg}")
        return
    with _MSS() as sct:
        scale = get_scale(sct)
        p = _cursor_point()
        px, py = int(p.x * scale), int(p.y * scale)
        h = SAMPLE_BOX
        box = {"left": px - h // 2, "top": py - h // 2, "width": h, "height": h}
        img = np.asarray(sct.grab(box))[:, :, :3]
        b, g, r = img.reshape(-1, 3).mean(0)
    r, g, b = int(r), int(g), int(b)
    rel_x, rel_y = px - win_origin[0], py - win_origin[1]
    tags = []
    if is_white(r, g, b):
        tags.append(f"WHITE (>= WHITE_MIN={WHITE_MIN}, diagnostic only)")
    if is_yellow(r, g, b):
        tags.append("YELLOW (capacity-full threshold)")
    tag_str = f"  [{', '.join(tags)}]" if tags else "  [neither -- below both thresholds]"
    msg = f"PIXEL=({rel_x},{rel_y})  RGB=({r},{g},{b}){tag_str}  (window-relative)"
    print(f"[{origin}] {msg}")
    clip = f"({rel_x}, {rel_y})  # RGB=({r},{g},{b}){tag_str}"
    try:
        set_clipboard(clip)
        show_popup("Pixel Info (copied)", msg)
    except Exception as e:
        print(f"[{origin}] clipboard copy failed: {e}")
        show_popup("Pixel Info", msg)


def _tap_key(name):
    """Press-and-release one V3-named key (platform-correct code looked up
    via v3_keycode). Assumes v3_keycode maps digit names like "1"/"2" the
    same way it already maps "r"/"enter"/"escape" -- check platform_mac.py /
    platform_win.py if that turns out not to hold."""
    code = v3_keycode(name)
    key_down(code)
    key_up(code)


def _reset_character_sequence(origin):
    right_click_held = False
    dequip_pan()
    
    try:
        _tap_key("escape")
        _interruptible_sleep(500 / 1000.0)
        click_buffered(641, 630, 200, 200)
        click_buffered(530, 380, 500, 0)
        _interruptible_sleep(RESET_POST_ENTER_MS / 1000.0)

        # 0) Recenter mouse before grabbing the camera
        move_absolute(*_screen_point(1280 // 2, 720 // 2))
        _interruptible_sleep(0.4)

        # 1) Hold down right click (Quartz -- registers as a real HID hold)
        _right_click_down()
        right_click_held = True
        _interruptible_sleep(0.2)  # let the button-down register before drag events start

        # 2) Drag straight down via relative HID deltas, same timing as before
        steps = max(1, RESET_DRAG_MS // RESET_DRAG_STEP_MS)
        _right_drag_relative(
            0, RESET_DRAG_STEP_PX * steps,
            steps=steps, step_delay=RESET_DRAG_STEP_MS / 1000.0)

        # 3) Stop holding down right click
        _right_click_up()
        right_click_held = False
        _interruptible_sleep(0.5)

        # 4) Move mouse to center of the 1280x720 window
        move_absolute(*_screen_point(1280 // 2, 720 // 2))

        print(f"[{origin}] reset character: done")
    except MacroStopped:
        print(f"[{origin}] reset character: aborted (stop requested)")
    finally:
        if right_click_held:
            _right_click_up()   # never leave the right mouse button stuck down
        State.resetting = False


def request_reset_character(origin="hotkey"):
    """Test sequence: Esc (wait 500ms) -> click_buffered(641, 630) ->
    click_buffered(530, 380) -> (wait 8s) -> recenter -> right-click-drag
    straight down (Quartz HID deltas) -> release -> recenter. Runs on a
    background thread so the hotkey listener (Esc/F1-3) stays responsive
    while it plays out. Bound to F4."""
    if State.resetting:
        print(f"[{origin}] reset character: already running, ignored")
        return
    State.resetting = True
    threading.Thread(target=_reset_character_sequence, args=(origin,),
                      daemon=True).start()


def request_stop(origin="hotkey"):
    """Stop the dig loop AND cancel any in-flight sequence -- pan_swap or
    reset-character -- wherever it currently is. Bound to F2.

    Setting State.running = False alone only stops the *next* tick() from
    starting; it does nothing to a pan_swap()/reset-character sequence
    already mid-flight, since those are plain blocking code once started.
    abort_requested is the flag those sequences actually check (via
    _raise_if_aborted()/_interruptible_sleep()) at every wait point, so this
    is what makes F2 cut them off within ~50ms instead of letting them run
    to completion (including all their retries)."""
    State.running = False
    State.abort_requested = True
    mouse_up()
    print(f"[{origin}] stopped")


def request_quit(origin="hotkey"):
    State.alive = False
    State.running = False
    State.abort_requested = True
    if _MOUSE_DOWN:
        mouse_up()
    print(f"[{origin}] quit")
    return False


# ============================================================================
# Phase 3: pan swap
# ============================================================================
def dequip_pan():
    """Press 1 repeatedly, sampling PAN_SWAP_START_CHECK_PIXEL after each
    press, until it no longer matches PAN_SWAP_START_CHECK_COLOR (within
    PAN_SWAP_START_CHECK_TOL_PCT) -- i.e. until the pan-swap prompt clears."""
    while True:
        _raise_if_aborted()
        _tap_key("1")
        _interruptible_sleep(PAN_SWAP_START_POLL_S)
        start_rgb = _sample_rgb(PAN_SWAP_START_CHECK_PIXEL)
        sr, sg, sb = (int(round(c)) for c in start_rgb)
        print(f"start-gate pixel {PAN_SWAP_START_CHECK_PIXEL} "
              f"(screen {_screen_point(*PAN_SWAP_START_CHECK_PIXEL)}) "
              f"RGB=({sr},{sg},{sb})  target={PAN_SWAP_START_CHECK_COLOR} "
              f"(tol {PAN_SWAP_START_CHECK_TOL_PCT}%)  window_origin={State.window_origin}")
        if not color_close(start_rgb, PAN_SWAP_START_CHECK_COLOR, PAN_SWAP_START_CHECK_TOL_PCT):
            break


def pan_swap(on_status=None, _attempt=1):
    """Swap a full pan for an empty one, then confirm the pan actually
    equipped back out. Re-resolves State.window_origin itself on entry (same
    as request_start()) so every *_PIXEL sample/click below is measured
    against the Roblox window's actual live position -- not a stale or
    never-set origin, which would default to (0, 0) and silently sample
    SCREEN-absolute coordinates instead of ROBLOX-WINDOW-relative ones. This
    matters most for request_pan_swap_test() (F5), which can be fired
    without a prior F1 run ever having set an origin. Sequence:

        press 1 -> wait 250ms -> check PAN_SWAP_START_CHECK_PIXEL, repeat
           until it's NOT PAN_SWAP_START_CHECK_COLOR (within
           PAN_SWAP_START_CHECK_TOL_PCT)
        press 2 -> wait
        -> open pans menu
        -> select first (full) pan slot -> scroll the list into view
           -> equip
        -> select bottom (empty) pan slot -> equip
        -> wait -> press 2 -> press 1 -> wait
        -> sample PAN_SWAP_CHECK_PIXEL; if it doesn't match
           PAN_SWAP_CHECK_COLOR (within PAN_SWAP_CHECK_TOL_PCT), the whole
           sequence is retried from the top, up to PAN_SWAP_MAX_ATTEMPTS
           times.

    Blocking -- call it from a background thread (see
    request_pan_swap_test) if you need the hotkey listener to stay
    responsive while it runs, same reasoning as _reset_character_sequence.

    Cancellable: every wait point checks State.abort_requested (via
    _raise_if_aborted()/_interruptible_sleep()/click_buffered()) and raises
    MacroStopped the moment F2 (request_stop) or quit fires, so a stop
    request lands within ~50ms instead of waiting for this call -- and all
    its retries -- to finish on their own. Callers (tick(), the F5 test
    thread) are responsible for catching MacroStopped.
    """

    def status(msg):
        if on_status:
            on_status(f"[pan swap] attempt {_attempt}: {msg}")

    _raise_if_aborted()
    o = _resolve_origin()
    if o:
        State.window_origin = o
    else:
        status("couldn't re-find the Roblox window -- using last known origin")

    status("waiting for pan swap prompt to clear")
    dequip_pan()

    status("press 2")
    _tap_key("2")
    _interruptible_sleep(0.5)

    status("open pans menu")
    click_buffered(*PAN_SWAP_BUTTON_PIXEL, PAN_SWAP_CLICK_PRE_MS, PAN_SWAP_CLICK_POST_MS)

    status("select first pan, scroll, equip")
    click_buffered(*PAN_SWAP_FIRST_PAN_PIXEL, PAN_SWAP_CLICK_PRE_MS, PAN_SWAP_CLICK_POST_MS)
    _scroll_down(PAN_SWAP_SCROLL_COUNT, PAN_SWAP_SCROLL_DURATION_S)
    click_buffered(*PAN_SWAP_EQUIP_PIXEL, PAN_SWAP_CLICK_PRE_MS, PAN_SWAP_CLICK_POST_MS)

    status("select bottom pan, equip")
    click_buffered(*PAN_SWAP_BOTTOM_PAN_PIXEL, PAN_SWAP_CLICK_PRE_MS, PAN_SWAP_CLICK_POST_MS)
    click_buffered(*PAN_SWAP_EQUIP_PIXEL, PAN_SWAP_CLICK_PRE_MS, PAN_SWAP_CLICK_POST_MS)

    _interruptible_sleep(0.5)
    status("press 2")
    _tap_key("2")
    _interruptible_sleep(0.75)
    status("press 1")
    _tap_key("1")
    _interruptible_sleep(1.0)

    checked_rgb = _sample_rgb(PAN_SWAP_CHECK_PIXEL)
    ok = color_close(checked_rgb, PAN_SWAP_CHECK_COLOR, PAN_SWAP_CHECK_TOL_PCT)
    if ok:
        status("pan confirmed out -- done")
        return True

    status("pan not confirmed out")
    if _attempt >= PAN_SWAP_MAX_ATTEMPTS:
        status(f"giving up after {PAN_SWAP_MAX_ATTEMPTS} attempts")
        return False
    return pan_swap(on_status=on_status, _attempt=_attempt + 1)


def request_pan_swap_test(origin="hotkey"):
    """Standalone test entry point -- runs pan_swap() once (with its own
    internal retries) without needing capacity to actually read full, so it
    can be exercised on demand. Bound to F5. Runs on a background thread so
    the hotkey listener stays responsive while it plays out, same reasoning
    as request_reset_character.

    NOTE: wiring F5 -> this function into the actual OS-level hotkey
    listener happens in platform_mac.py/platform_win.py's make_listener(),
    alongside F1-F4 -- not in this file."""
    if State.pan_swapping:
        print(f"[{origin}] pan swap: already running, ignored")
        return
    State.pan_swapping = True

    def _run():
        try:
            pan_swap(on_status=print)
        except MacroStopped:
            print(f"[{origin}] pan swap: aborted (stop requested)")
        finally:
            State.pan_swapping = False

    threading.Thread(target=_run, daemon=True).start()


# ============================================================================
# The loop
# ============================================================================
def dig_tap():
    mouse_down()
    time.sleep(DIG_HOLD_MS / 1000.0)
    mouse_up()


def tick(detector, on_status=None):
    """One poll, phase-gated:

        Phase 1 (digging):   tap the dig button while both dig-spot check
                              points match and the pan isn't full.
        Phase 3 (pan swap):  once the pan reads full, run the full pan_swap()
                              sequence right here, then return -- digging
                              resumes on the next tick once the new (empty)
                              pan is confirmed equipped.

    (Phase 2, reset-character, is manual/hotkey-only -- see
    request_reset_character -- and isn't part of this automatic gating.)
    """
    if detector.capacity_full():
        if on_status:
            on_status("capacity full -- running pan swap")
        try:
            pan_swap(on_status=on_status)
            if on_status:
                on_status("pan swap done -- resuming digging")
        except MacroStopped:
            if on_status:
                on_status("pan swap aborted -- stop requested")
        return
    if detector.on_dig_spot():
        dig_tap()
        if on_status:
            on_status("digging")
    elif on_status:
        on_status("idle")


def run(on_status=None):
    """Blocking loop -- call from a background thread. Runs until
    State.alive goes False; only taps/checks while State.running is True.
    State.window_origin is first set in request_start(), right after
    pin_window() -- this loop starts well before the window is ever pinned,
    so Detector starts anchored at (0, 0) and picks up the real origin on
    the first resync tick after State.running goes True. While running, the
    origin is re-measured every WINDOW_RESYNC_S so a mid-run drag/drift
    self-heals instead of only correcting on the next F1."""
    with _MSS() as sct:
        detector = Detector(sct, State.window_origin or (0, 0))
        last_resync = 0.0
        while State.alive:
            if State.running:
                now = time.time()
                if now - last_resync >= WINDOW_RESYNC_S:
                    o = find_window_origin()
                    if o:
                        State.window_origin = o
                    last_resync = now
                detector.set_origin(State.window_origin or (0, 0))
                tick(detector, on_status)
            elif on_status:
                on_status("stopped")
            time.sleep(LOOP_POLL_S)


# ============================================================================
# Dev tool: hand-derive DIG_SPOT_A/B_PIXEL+COLOR / CAP_FULL_PIXEL
# ============================================================================
def calibrate():
    print("CALIBRATE -- hover a target on the Roblox window, read PIXEL/RGB, "
          "Ctrl+C to quit.")
    print("  PIXEL is reported ROBLOX-WINDOW-RELATIVE (offset from the live "
          "window's top-left) --")
    print("  paste it straight into the *_PIXEL constants, no manual "
          "offsetting needed.")
    print("  two solid points on a CONFIRMED valid dig spot -> "
          "DIG_SPOT_A_PIXEL/DIG_SPOT_A_COLOR, DIG_SPOT_B_PIXEL/DIG_SPOT_B_COLOR")
    print("  RIGHT END of capacity bar -> CAP_FULL_PIXEL (gray empty / yellow full)\n")
    with _MSS() as sct:
        scale = get_scale(sct)
        full = sct.monitors[1]
        try:
            while True:
                origin = find_window_origin()
                if not origin:
                    print("\rRoblox window not found on screen -- bring it "
                          "forward...                                   ",
                          end="", flush=True)
                    time.sleep(0.1)
                    continue
                p = _cursor_point()
                px = int(p.x * scale)
                py = int(p.y * scale)
                box = {"left": px - 3, "top": py - 3, "width": 6, "height": 6}
                box["left"] = max(full["left"],
                                  min(box["left"], full["left"] + full["width"] - 6))
                box["top"] = max(full["top"],
                                 min(box["top"], full["top"] + full["height"] - 6))
                img = np.asarray(sct.grab(box))[:, :, :3]
                b, g, r = img.reshape(-1, 3).mean(0)
                rel_x, rel_y = px - origin[0], py - origin[1]
                tags = ""
                if is_white(r, g, b):  tags += " WHITE"
                if is_yellow(r, g, b): tags += " YELLOW"
                print(f"\rPIXEL=({rel_x:>5},{rel_y:>5})  RGB=({int(r):>3},{int(g):>3},"
                      f"{int(b):>3})  scale {scale:.2f} [{tags.strip()}]      ",
                      end="", flush=True)
                time.sleep(0.05)
        except KeyboardInterrupt:
            print("\nDone.")


def _cli_main():
    listener = make_listener()
    listener.start()
    print(f"F1 to find/pin window + start, F2 to stop, F3 pixel info, "
          f"F4 reset character, F5 pan swap test. Ctrl+C to quit.")
    try:
        run(on_status=print)
    except KeyboardInterrupt:
        request_quit("ctrl-c")


if __name__ == "__main__":
    _cli_main()