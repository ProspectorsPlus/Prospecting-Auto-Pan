#!/usr/bin/env python3
"""sanitize_examples.py -- turn the owner's raw calibration screenshots into
shippable in-app example images.

Raw desktop screenshots must NEVER ship: they contain the macOS menu bar
(with the owner's name), the dock, and unrelated desktop chrome. This tool
crops each source down to just the relevant Roblox calibration area, scales
it, writes the sanitized PNG into assets/onboarding/calibration/common/ and
updates manifest.json (file + alt + approved). The RAW originals stay
outside the repository; only the sanitized crops are committed.

Usage (owner machine):
    python3 packaging/sanitize_examples.py [source_dir]
source_dir defaults to ~/Documents (where macOS saved the captures).
Missing sources are reported and skipped -- nothing is fabricated.

Crop specs are in SOURCE pixels for the 3824x2562 captures of 2026-07-30;
re-captures at other sizes need new specs (the tool refuses mismatched
dimensions rather than guessing).
"""
import json
import os
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSET_DIR = os.path.join(ROOT, "assets", "onboarding", "calibration")
OUT_DIR = os.path.join(ASSET_DIR, "common")
MANIFEST = os.path.join(ASSET_DIR, "manifest.json")
EXPECT_SIZE = (3824, 2562)
MAX_W = 1200

# macOS names screenshots with a NARROW NO-BREAK SPACE (U+202F) before
# AM/PM; resolve by timestamp so both spellings work.
S = "Screenshot 2026-07-30 at %s AM.png"


def _resolve(src_dir, fname):
    """The template uses U+202F (macOS's narrow no-break space before
    AM/PM); fall back to a plain space for hand-renamed files."""
    p = os.path.join(src_dir, fname)
    if os.path.isfile(p):
        return p
    alt = os.path.join(src_dir, fname.replace("\u202f", " "))
    return alt if os.path.isfile(alt) else p


# item -> (source file, crop box (l, t, r, b) in source px, alt text)
SPECS = {
    "cap_bar": (S % "4.05.27", (1350, 1700, 2950, 2260),
                "The pan-fill capacity bar completely full (all yellow), "
                "with the picker's confirm card on the detected RIGHT tip "
                "of the bar."),
    "pan_prompt": (S % "4.06.10", (1350, 1700, 2450, 2318),
                   "Standing in the water: the white 'Pan' prompt under "
                   "the full capacity bar, with the picker's confirm card "
                   "on the detected prompt pixel."),
    "deposit_prompt": (S % "4.06.35", (1350, 1700, 2450, 2318),
                       "Standing on land: the white 'Collect Deposit' "
                       "prompt, with the picker's confirm card on the "
                       "detected prompt pixel."),
    "shake_prompt": (S % "4.07.03", (1350, 1700, 2450, 2318),
                     "Mid-shake: the white 'Shake' prompt with the "
                     "picker's confirm card on the detected pixel (the "
                     "colourful pan-shake fills the background)."),
    "cue_masks": (S % "4.07.43", (460, 275, 3560, 2318),
                  "The cue-mask editor after clicking the 'Pan' prompt "
                  "word: kept letters glow green; the card below confirms "
                  "or restarts the selection."),
    "dig_green": (S % "4.10.44", (1900, 900, 2900, 1700),
                  "A dig in progress: the dig bar with its green target "
                  "zone, magnified by the picker's loupe."),
    "money_region": (S % "4.11.39", (3050, 1980, 3720, 2318),
                     "The money counter in the lower-right corner with "
                     "the picker's box-confirm card after dragging a "
                     "tight box around the number."),
    "shards_region": (S % "4.12.14", (2950, 1850, 3720, 2280),
                      "The shards counter in the lower-right corner with "
                      "the picker's box-confirm card after dragging a "
                      "tight box around the number."),
    "find_region": (S % "4.12.55", (2600, 380, 3720, 2318),
                    "A dragged find pop-up box on the right side of the "
                    "screen where finds appear, with the box-confirm "
                    "card."),
    "fortune_river": (S % "4.13.59", (1350, 600, 2500, 1850),
                      "The Fortune River / Fast Travel panel open with "
                      "the picker's confirm card on a captured point."),
    "autopan_button": (S % "4.15.30", (1350, 1650, 2450, 2318),
                       "The Auto Pan button showing its ON (green) state, with "
                       "the picker's confirm card on the captured ON "
                       "colour."),
}


def main():
    src_dir = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1
                                 else "~/Documents")
    os.makedirs(OUT_DIR, exist_ok=True)
    man = json.load(open(MANIFEST))
    done, missing = [], []
    for item, (fname, box, alt) in sorted(SPECS.items()):
        src = _resolve(src_dir, fname)
        if not os.path.isfile(src):
            missing.append("%s (wants %s)" % (item, fname))
            continue
        im = Image.open(src)
        if im.size != EXPECT_SIZE:
            print("!! %s: unexpected size %s (specs assume %s) -- skipped"
                  % (fname, im.size, EXPECT_SIZE))
            missing.append(item)
            continue
        crop = im.crop(box).convert("RGB")
        if crop.width > MAX_W:
            crop = crop.resize(
                (MAX_W, int(crop.height * MAX_W / crop.width)),
                Image.LANCZOS)
        rel = os.path.join("common", "%s.png" % item)
        crop.save(os.path.join(ASSET_DIR, rel), optimize=True)
        e = man["items"].setdefault(item, {})
        e["file"] = rel
        e["alt"] = alt
        e["approved"] = True
        e.setdefault("annotations", [])
        done.append(item)
    tmp = MANIFEST + ".tmp"
    with open(tmp, "w") as f:
        json.dump(man, f, indent=1)
    os.replace(tmp, MANIFEST)
    print("sanitized:", ", ".join(done) or "(none)")
    if missing:
        print("missing sources (owner still owes these):")
        for m in missing:
            print("  -", m)
    return 0


if __name__ == "__main__":
    sys.exit(main())
