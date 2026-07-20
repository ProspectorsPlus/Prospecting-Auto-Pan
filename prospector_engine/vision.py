"""Engine-side image vision (PPE1 1.3).

Reference images ("template assets") let PPScript v3 programs find things
on screen. Prospector Studio writes them at publish time as plain PNG
files under <engine home>/studio_assets/<asset_id>.png; the engine only
ever READS them. This module is the whole imaging stack for that feature:

  * a pure-Python PNG decoder (stdlib zlib + struct + numpy only -- the
    engine ships without PIL/cv2 and must stay that way). It supports
    exactly what mss.tools.to_png and Studio canvases emit: 8-bit
    greyscale / RGB / RGBA, non-interlaced, any scanline filter (0-4).
    Anything else raises a clear ValueError naming the limitation.
  * a numpy template matcher: exhaustive normalized mean-absolute-
    difference over a search image. Every candidate position is scanned;
    large templates are pre-scored on a subsampled pixel grid and the
    leading positions re-scored exactly (a speed optimization that never
    skips positions).

Screen access does NOT live here: run-time grabs go through the run
Detector's mss handle (ScriptRunner._image_probe) and authoring-time
grabs through the Sensing capture session, so the sim world and the
tests script this module the same way they script everything else.
"""
import os
import struct
import zlib

import numpy as np

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Asset naming contract (mirrored by Prospector Studio's publisher).
ASSET_DIR_NAME = "studio_assets"
ASSET_ID_MAX = 64
_ASSET_ID_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")

# Refuse absurd decodes before allocating (a template is a UI patch, and
# the authoring verb caps its payload at 1.5 MB of base64 anyway).
_MAX_DIM = 10000


def asset_id_ok(asset_id):
    """True when the id is a safe asset name: [A-Za-z0-9_-]{1,64}."""
    return (isinstance(asset_id, str)
            and 1 <= len(asset_id) <= ASSET_ID_MAX
            and all(ch in _ASSET_ID_CHARS for ch in asset_id))


def asset_dir(home):
    return os.path.join(home, ASSET_DIR_NAME)


def asset_path(home, asset_id):
    return os.path.join(asset_dir(home), asset_id + ".png")


def load_png_rgb(path):
    """Decode a PNG file -> HxWx3 uint8 RGB (see decode_png_rgb)."""
    with open(path, "rb") as f:
        return decode_png_rgb(f.read())


def decode_png_rgb(data):
    """Decode PNG bytes -> HxWx3 uint8 RGB ndarray.

    Supports 8-bit greyscale (color type 0), RGB (2) and RGBA (6),
    non-interlaced, scanline filters 0-4. Greyscale is replicated to
    three channels; RGBA drops its alpha channel (matching treats the
    template as fully opaque). Raises ValueError with a clear message
    for anything outside that envelope.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) < 8 \
            or bytes(data[:8]) != PNG_MAGIC:
        raise ValueError("not a PNG file")
    data = bytes(data)
    pos = 8
    w = h = None
    bit_depth = color_type = interlace = 0
    idat = []
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        if len(chunk) != length:
            raise ValueError("PNG is truncated (incomplete %r chunk)"
                             % ctype.decode("latin-1"))
        pos += 12 + length          # length + type + data + crc
        if ctype == b"IHDR":
            if length != 13:
                raise ValueError("PNG has a malformed IHDR")
            (w, h, bit_depth, color_type,
             _comp, _filt, interlace) = struct.unpack(">IIBBBBB", chunk)
        elif ctype == b"IDAT":
            idat.append(chunk)
        elif ctype == b"IEND":
            break
    if w is None:
        raise ValueError("PNG has no IHDR")
    if not (1 <= w <= _MAX_DIM and 1 <= h <= _MAX_DIM):
        raise ValueError("unsupported PNG: bad dimensions %dx%d" % (w, h))
    if bit_depth != 8:
        raise ValueError("unsupported PNG: bit depth %d (8-bit only)"
                         % bit_depth)
    if color_type not in (0, 2, 6):
        raise ValueError("unsupported PNG: color type %d (greyscale, RGB "
                         "or RGBA only)" % color_type)
    if interlace != 0:
        raise ValueError("unsupported PNG: interlaced (Adam7)")
    if not idat:
        raise ValueError("PNG has no image data")
    channels = {0: 1, 2: 3, 6: 4}[color_type]
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error as e:
        raise ValueError("PNG image data is corrupt: %s" % e)
    stride = w * channels
    if len(raw) < h * (stride + 1):
        raise ValueError("PNG image data is truncated")

    out = np.empty((h, stride), dtype=np.uint8)
    prev = bytearray(stride)
    p = 0
    for row in range(h):
        ft = raw[p]
        p += 1
        cur = bytearray(raw[p:p + stride])
        p += stride
        if ft == 0:                                     # None
            pass
        elif ft == 1:                                   # Sub
            for i in range(channels, stride):
                cur[i] = (cur[i] + cur[i - channels]) & 0xFF
        elif ft == 2:                                   # Up (vectorized:
            # the only non-trivial filter mss-sized frames ever use)
            c = (np.frombuffer(bytes(cur), np.uint8)
                 + np.frombuffer(bytes(prev), np.uint8))
            cur = bytearray(c.astype(np.uint8).tobytes())
        elif ft == 3:                                   # Average
            for i in range(stride):
                a = cur[i - channels] if i >= channels else 0
                cur[i] = (cur[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:                                   # Paeth
            for i in range(stride):
                a = cur[i - channels] if i >= channels else 0
                b = prev[i]
                c2 = prev[i - channels] if i >= channels else 0
                pa = abs(b - c2)
                pb = abs(a - c2)
                pc = abs(a + b - 2 * c2)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc
                                                       else c2)
                cur[i] = (cur[i] + pr) & 0xFF
        else:
            raise ValueError("unsupported PNG filter %d" % ft)
        out[row] = np.frombuffer(bytes(cur), np.uint8)
        prev = cur
    px = out.reshape(h, w, channels)
    if channels == 1:
        px = np.repeat(px, 3, axis=2)
    elif channels == 4:
        px = px[:, :, :3]
    return np.ascontiguousarray(px)


def match_template(image, template, threshold=0.9, stride=None):
    """Exhaustive normalized mean-absolute-difference template match.

    image:     HxWx3 uint8 (RGB) -- the search area
    template:  hxwx3 uint8 (RGB) -- the reference patch
    threshold: minimum score, 0..1
    stride:    optional template-pixel sampling stride for the coarse
               pass (auto-chosen when None; 1 = fully exact everywhere)

    -> {"found": bool, "x": int, "y": int, "score": float}
       x, y = CENTRE of the best position in image coordinates
              (best_left + w // 2, best_top + h // 2)
       score = 1 - meanAbsDiff / 255 over every pixel and channel of the
               best position (1.0 = pixel-identical), rounded to 4 dp.

    Every candidate POSITION is always scanned. Large templates are
    pre-scored on a subsampled pixel grid, then the 32 leading positions
    are re-scored over the full template -- an optimization of the
    per-position cost, never of the search space.
    """
    img = np.asarray(image, dtype=np.int16)
    tpl = np.asarray(template, dtype=np.int16)
    if img.ndim != 3 or tpl.ndim != 3 \
            or img.shape[2] != 3 or tpl.shape[2] != 3:
        raise ValueError("match_template expects HxWx3 arrays")
    H, W = img.shape[0], img.shape[1]
    h, w = tpl.shape[0], tpl.shape[1]
    if h < 1 or w < 1 or h > H or w > W:
        return {"found": False, "x": 0, "y": 0, "score": 0.0}
    out_h, out_w = H - h + 1, W - w + 1
    if stride is None:
        s = 1
        if h * w > 100:                       # aim for ~100 sampled pixels
            s = int(round((h * w / 100.0) ** 0.5))
        s = max(1, min(s, min(h, w)))
    else:
        s = max(1, min(int(stride), min(h, w)))
    ys = range(0, h, s)
    xs = range(0, w, s)
    acc = np.zeros((out_h, out_w), dtype=np.int64)
    for ty in ys:
        for tx in xs:
            acc += np.abs(img[ty:ty + out_h, tx:tx + out_w]
                          - tpl[ty, tx]).sum(axis=2, dtype=np.int64)
    if s == 1:
        idx = int(acc.argmin())
        by, bx = divmod(idx, out_w)
        mad = acc[by, bx] / float(h * w * 3)
    else:
        k = min(32, acc.size)
        cand = np.argpartition(acc.ravel(), k - 1)[:k]
        best = None
        for idx in cand:
            cy, cx = divmod(int(idx), out_w)
            d = float(np.abs(img[cy:cy + h, cx:cx + w] - tpl).mean())
            if best is None or d < best[0]:
                best = (d, cx, cy)
        mad, bx, by = best
    score = round(1.0 - mad / 255.0, 4)
    found = score >= float(threshold) - 1e-9
    return {"found": bool(found), "x": int(bx + w // 2),
            "y": int(by + h // 2), "score": float(score)}
