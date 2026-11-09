"""Draws the Fischeroo app icon (assets/icon.png 1024px, icon.icns for Mac, icon.ico for
Windows). Needs Pillow. Drawn 4x larger and scaled down for smooth edges."""
import math
import os

from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets")
S = 4096                      # work size (scaled to 1024)


def lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(len(a)))


def squircle_mask(size, inset, radius):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle((inset, inset, size - inset, size - inset), radius, fill=255)
    return m


def draw():
    inset = int(S * 0.09)            # macOS icons leave a margin around the rounded square
    side = S - 2 * inset
    # background: deep ocean to teal, top to bottom
    bg = Image.new("RGB", (S, S))
    px = ImageDraw.Draw(bg)
    top, bot = (16, 46, 92), (14, 150, 170)
    for y in range(S):
        t = min(1, max(0, (y - inset) / side))
        px.line([(0, y), (S, y)], fill=lerp(top, bot, t ** 1.2))
    art = bg.convert("RGBA")
    d = ImageDraw.Draw(art)

    # sun glow
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((S * 0.62, S * 0.16, S * 0.82, S * 0.36), fill=(255, 236, 180, 90))
    art.alpha_composite(glow.filter(ImageFilter.GaussianBlur(S * 0.035)))
    ImageDraw.Draw(art).ellipse((S * 0.675, S * 0.215, S * 0.765, S * 0.305), fill=(255, 244, 205, 255))   # moon

    # waves (three layers)
    for k, (yb, col, amp) in enumerate([(0.70, (40, 190, 210, 255), 0.022),
                                        (0.77, (24, 160, 190, 255), 0.026),
                                        (0.84, (16, 120, 160, 255), 0.03)]):
        pts = []
        for i in range(0, 241):
            x = inset + side * i / 240
            y = S * yb + S * amp * math.sin(i / 240 * math.pi * 3 + k * 1.3)
            pts.append((x, y))
        pts += [(S - inset, S), (inset, S)]
        d.polygon(pts, fill=col)
        # foam line
        d.line(pts[:241], fill=(220, 250, 255, 200), width=int(S * 0.008))

    # fishing line from the top, with a hook
    hx, hy = S * 0.36, S * 0.47
    d.line([(S * 0.36, inset - 10), (hx, hy)], fill=(245, 245, 250, 255), width=int(S * 0.010))
    r = S * 0.045
    d.arc((hx - r * 2, hy - r * 0.2, hx, hy + r * 1.8), 0, 200, fill=(230, 232, 240, 255), width=int(S * 0.016))
    d.polygon([(hx - r * 2.05, hy + r * 0.75), (hx - r * 2.35, hy + r * 0.25), (hx - r * 1.7, hy + r * 0.45)],
              fill=(230, 232, 240, 255))

    # the fish: leaping, orange-gold body with a tail, fin, eye and a little smile
    fish = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    f = ImageDraw.Draw(fish)
    cx, cy, L, H = S * 0.53, S * 0.555, S * 0.36, S * 0.22
    body = (cx - L / 2, cy - H / 2, cx + L / 2, cy + H / 2)
    # tail
    tx = cx + L / 2 - S * 0.02
    f.polygon([(tx, cy), (tx + S * 0.12, cy - S * 0.10), (tx + S * 0.09, cy), (tx + S * 0.12, cy + S * 0.10)],
              fill=(240, 120, 40, 255))
    f.ellipse(body, fill=(255, 150, 50, 255))
    # belly highlight
    f.ellipse((cx - L * 0.42, cy, cx + L * 0.25, cy + H * 0.45), fill=(255, 200, 110, 255))
    # top fin
    f.polygon([(cx - L * 0.05, cy - H * 0.45), (cx + L * 0.18, cy - H * 0.85), (cx + L * 0.25, cy - H * 0.40)],
              fill=(240, 120, 40, 255))
    # gill
    f.arc((cx - L * 0.22, cy - H * 0.32, cx - L * 0.02, cy + H * 0.32), 300, 60,
          fill=(225, 105, 35, 255), width=int(S * 0.011))
    # eye
    ex, ey, er = cx - L * 0.28, cy - H * 0.12, S * 0.032
    f.ellipse((ex - er, ey - er, ex + er, ey + er), fill=(255, 255, 255, 255))
    f.ellipse((ex - er * 0.55, ey - er * 0.45, ex + er * 0.45, ey + er * 0.55), fill=(20, 30, 50, 255))
    f.ellipse((ex - er * 0.15, ey - er * 0.35, ex + er * 0.15, ey - er * 0.05), fill=(255, 255, 255, 255))
    # smile
    f.arc((cx - L * 0.47, cy - H * 0.02, cx - L * 0.30, cy + H * 0.22), 20, 120, fill=(150, 60, 20, 255),
          width=int(S * 0.009))
    fish = fish.rotate(-14, resample=Image.BICUBIC, center=(cx, cy))
    # soft shadow under the fish
    sh = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sh.paste((0, 20, 40, 110), (0, 0, S, S), fish.split()[3])
    art.alpha_composite(sh.filter(ImageFilter.GaussianBlur(S * 0.02)).transform(
        (S, S), Image.AFFINE, (1, 0, -S * 0.01, 0, 1, -S * 0.02)))
    art.alpha_composite(fish)

    # water splash drops
    for (x, y, rr) in [(0.80, 0.66, 0.018), (0.86, 0.60, 0.012), (0.30, 0.68, 0.014), (0.25, 0.63, 0.009)]:
        d2 = ImageDraw.Draw(art)
        d2.ellipse((S * x - S * rr, S * y - S * rr, S * x + S * rr, S * y + S * rr), fill=(225, 250, 255, 255))

    # cut to the rounded square, add a soft drop shadow (macOS style)
    mask = squircle_mask(S, inset, int(side * 0.225))
    icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 90), (0, 0, S, S), mask)
    icon.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(S * 0.012)).transform(
        (S, S), Image.AFFINE, (1, 0, 0, 0, 1, -S * 0.01)))
    clipped = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    clipped.paste(art, (0, 0), mask)
    icon.alpha_composite(clipped)
    # thin light rim
    rim = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(rim).rounded_rectangle((inset, inset, S - inset, S - inset), int(side * 0.225),
                                          outline=(255, 255, 255, 40), width=int(S * 0.004))
    icon.alpha_composite(rim)
    return icon.resize((1024, 1024), Image.LANCZOS)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    img = draw()
    img.save(os.path.join(OUT, "icon.png"))
    img.save(os.path.join(OUT, "icon.icns"))
    img.save(os.path.join(OUT, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", os.listdir(OUT))
