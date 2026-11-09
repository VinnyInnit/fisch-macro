"""Release-build check on the real OS (GitHub's Mac / Windows machines): can the macro read
text here? Draws a fake Roblox window with a Shake button and a Fisch hotbar using real
fonts, then runs the macro's own Shake finder and rod reader on it. Needs Pillow."""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "macro"))
import numpy as np
from PIL import Image, ImageDraw, ImageFont

import fisch_auto as fa

FONTS = ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/System/Library/Fonts/Helvetica.ttc",
         "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]


def font(size):
    for f in FONTS:
        if os.path.exists(f):
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


def scene():
    rng = np.random.default_rng(1)
    img = Image.fromarray(rng.integers(60, 120, (1080, 1920, 3)).astype(np.uint8))
    return img, ImageDraw.Draw(img)


ok = True
print("platform", sys.platform, "| OCR available:", fa.ocr_available(), fa._WIN_OCR.get("error", ""))
if sys.platform == "win32":
    fa._win_bitmap(np.zeros((8, 8, 3), np.uint8))
    print("bitmap via:", fa._WIN_OCR.get("way"))
if not fa.ocr_available():
    print("FAIL: no text recognition")
    sys.exit(1)

# 1) a Shake button
img, d = scene()
d.rounded_rectangle((820, 470, 1020, 540), 14, fill=(30, 30, 38), outline=(255, 255, 255), width=3)
d.text((920, 505), "Shake", font=font(34), fill=(255, 255, 255), anchor="mm")
t = time.time()
pos = fa.find_shake(np.asarray(img))
dt = time.time() - t
print(f"shake button -> {pos} ({dt * 1000:.0f} ms)")
ok &= pos is not None and abs(pos[0] - 920) < 40 and abs(pos[1] - 505) < 25

# 2) only look-alike text: must NOT be taken as a button
img, d = scene()
d.text((700, 400), "Wind Shake", font=font(30), fill=(255, 255, 255))
d.text((700, 600), "Shake it off", font=font(30), fill=(255, 255, 255))
arr = np.asarray(img)
crop = np.ascontiguousarray(arr[int(0.07 * 1080):int(0.86 * 1080)][::2, ::2])
print("  raw text read:", [(t, [round(v) for v in b]) for t, _, b in fa.ocr_fast(crop)])
pos = fa.find_shake(arr)
print(f"look-alikes -> {pos}")
ok &= pos is None

# 3) hotbar: slot 1 holds the rod (enchants above it), slot 2 is "Equipment Bag"
img, d = scene()
f = font(13)
d.rectangle((650, 1010, 716, 1075), fill=(40, 40, 46))
d.rectangle((723, 1010, 789, 1075), fill=(40, 40, 46))
d.text((683, 1028), "Clever Wise", font=f, fill=(255, 230, 120), anchor="mm")
d.text((683, 1046), "Pinion's Aria", font=f, fill=(255, 255, 255), anchor="mm")
d.text((756, 1033), "Equipment", font=f, fill=(255, 255, 255), anchor="mm")
d.text((756, 1050), "Bag", font=f, fill=(255, 255, 255), anchor="mm")
d.text((960, 960), "Sparkling Celestial Crowned Anglerfish", font=font(16), fill=(255, 255, 255), anchor="mm")
d.text((960, 935), "Current Bait: Mist Worms [x444]", font=font(16), fill=(255, 220, 140), anchor="mm")
d.text((960, 120), "[Strange Whirlpool]", font=font(40), fill=(255, 255, 255), anchor="mm")
t = time.time()
ident = fa.identify(np.asarray(img))
dt = time.time() - t
print(f"identify -> rod {ident.get('rod')!r} enchants {ident.get('enchant')!r} {ident.get('enchant2')!r} "
      f"area {ident.get('area')!r} ({dt * 1000:.0f} ms) slot1={ident['raw'].get('slot1')}")
ok &= ident.get("rod") == "Pinion's Aria"
print(f"bait -> {ident.get('bait')!r}")
ok &= ident.get("bait") == "Mist Worms"

print("OCR CHECK PASSED" if ok else "OCR CHECK FAILED")
sys.exit(0 if ok else 1)
