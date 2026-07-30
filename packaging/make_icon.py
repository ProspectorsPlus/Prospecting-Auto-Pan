#!/usr/bin/env python3
"""make_icon.py -- generate every Prospector Lite icon asset from one
deterministic geometric definition.

The mark is the same faceted diamond the app already brands with in its UI
(the inline "pp-gem" SVG and docs/favicon.svg -- path "M6.5 4h11l4 5.2L12 21
2.5 9.2z"), rendered as a proper app icon: a dark rounded-square tile with a
gold-faceted diamond. Original geometric artwork -- no game logo, no text,
no emoji, no third-party imagery.

Outputs (all overwritten in place):
  assets/icon/prospector-lite-icon.svg   editable master (hand-written SVG)
  assets/icon/png/icon-<size>.png        1024 512 256 128 64 48 32 24 16
  icon.png                               1024 master used by both pipelines
  windows/icon.png                       byte-copy of icon.png
  windows/icon.ico                       multi-size (16..256)
  build/icon.iconset + build/icon.icns   macOS (only when iconutil exists)

Run from the repo root:  python3 packaging/make_icon.py
Deterministic: same input geometry -> byte-identical PNGs (PIL, no AA seed).
"""
import os
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- geometry (viewBox 0..24, matching the in-app pp-gem mark) -------------
# Crown corners
TL = (6.5, 4.0)     # table left
TR = (17.5, 4.0)    # table right
GL = (2.5, 9.2)     # girdle left
GR = (21.5, 9.2)    # girdle right
BP = (12.0, 21.0)   # bottom point (culet)
# crown/table split points on the girdle line
ML = (8.6, 9.2)
MR = (15.4, 9.2)

FACETS = [
    # (polygon, fill) -- light comes from the top-left
    ([TL, TR, MR, ML], "#f6e7c8"),          # table
    ([TL, ML, GL], "#e8cf8f"),              # left crown
    ([TR, GR, MR], "#c9a24f"),              # right crown
    ([GL, ML, BP], "#d9a441"),              # left pavilion
    ([ML, MR, BP], "#f0d9a0"),              # centre pavilion
    ([MR, GR, BP], "#b8933f"),              # right pavilion
]
BG = "#161410"
BG_EDGE = "#241d15"

SIZES = (1024, 512, 256, 128, 64, 48, 32, 24, 16)
SS = 4  # supersampling factor for clean edges


def _rounded_rect_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def render(size):
    """One tile at `size`, drawn supersampled then downscaled."""
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # tile: dark rounded square with a faint edge ring
    radius = int(big * 0.2237)  # macOS-style corner radius
    d.rounded_rectangle([0, 0, big - 1, big - 1], radius=radius, fill=BG)
    ring = max(1, big // 170)
    d.rounded_rectangle([ring, ring, big - 1 - ring, big - 1 - ring],
                        radius=max(1, radius - ring), outline=BG_EDGE,
                        width=ring)
    # diamond: centred, ~64% of the tile, nudged up slightly for optical
    # balance (the pavilion point makes it bottom-heavy)
    scale = big * 0.64 / 24.0
    ox = (big - 24.0 * scale) / 2.0
    oy = (big - 25.0 * scale) / 2.0 - big * 0.035
    for poly, fill in FACETS:
        d.polygon([(ox + x * scale, oy + y * scale) for (x, y) in poly],
                  fill=fill)
    img = img.resize((size, size), Image.LANCZOS)
    # keep corners fully transparent after the resize
    img.putalpha(Image.composite(
        img.getchannel("A"), Image.new("L", (size, size), 0),
        _rounded_rect_mask(size, int(size * 0.2237))))
    return img


SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
  <!-- Prospector Lite app icon: original geometric faceted diamond on a
       dark tile. Master source for packaging/make_icon.py (the PNG/ICNS/ICO
       pipeline renders the same geometry with PIL). -->
  <defs>
    <clipPath id="tile"><rect width="1024" height="1024" rx="229"/></clipPath>
  </defs>
  <g clip-path="url(#tile)">
    <rect width="1024" height="1024" fill="%(bg)s"/>
    <rect x="6" y="6" width="1012" height="1012" rx="223" fill="none"
          stroke="%(edge)s" stroke-width="6"/>
    <g transform="translate(184.32,148.68) scale(27.3)">
%(polys)s
    </g>
  </g>
</svg>
"""


def svg_source():
    polys = []
    for poly, fill in FACETS:
        pts = " ".join("%g,%g" % p for p in poly)
        polys.append('      <polygon points="%s" fill="%s"/>' % (pts, fill))
    return SVG % {"bg": BG, "edge": BG_EDGE, "polys": "\n".join(polys)}


def main():
    os.chdir(ROOT)
    outdir = os.path.join("assets", "icon", "png")
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join("assets", "icon",
                           "prospector-lite-icon.svg"), "w") as f:
        f.write(svg_source())
    imgs = {}
    for s in SIZES:
        imgs[s] = render(s)
        imgs[s].save(os.path.join(outdir, "icon-%d.png" % s))
    imgs[1024].save("icon.png")
    print("icon.png + %d PNG sizes written" % len(SIZES))
    # Windows: tracked copies used by prospecting.spec / installer.iss
    shutil.copyfile("icon.png", os.path.join("windows", "icon.png"))
    imgs[256].save(os.path.join("windows", "icon.ico"), format="ICO",
                   sizes=[(s, s) for s in (256, 128, 64, 48, 32, 24, 16)])
    print("windows/icon.png + windows/icon.ico written")
    # macOS: iconset + icns (build-time artefacts; regenerated every build)
    if shutil.which("iconutil"):
        iconset = os.path.join("build", "icon.iconset")
        shutil.rmtree(iconset, ignore_errors=True)
        os.makedirs(iconset, exist_ok=True)
        for s in (16, 32, 64, 128, 256, 512):
            imgs.get(s, render(s)).save(
                os.path.join(iconset, "icon_%dx%d.png" % (s, s)))
            d = s * 2
            imgs.get(d, render(d)).save(
                os.path.join(iconset, "icon_%dx%d@2x.png" % (s, s)))
        icns = os.path.join("build", "icon.icns")
        if os.path.exists(icns):
            os.remove(icns)
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns],
                       check=True)
        print("build/icon.icns written")
    else:
        print("iconutil not found -- skipped icns (windows/CI)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
