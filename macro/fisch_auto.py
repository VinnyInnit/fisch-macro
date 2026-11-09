"""
Zero-setup detection:
  - find the Roblox window on screen
  - read the hotbar and area title with the OS text recognition (macOS Vision) and match
    them against Fisch Wiki data: rod (+ its Control/Resilience/special minigame), enchant, area
  - find the reel track on screen by itself (no clicks): it's a long horizontal bar centred
    in the lower part of the Roblox window, about 40% of the window wide
  - "auto" reel detection that learns the track/bar/fish look from the screen, so rod skins
    that restyle the minigame work too
"""

import difflib
import json
import os
import re
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "fisch_data.json"), encoding="utf-8") as _f:
    DATA = json.load(_f)

ROD_NAMES = list(DATA["rods"].keys())
AREA_FLAGS = {  # areas whose conditions change the minigame (from the wiki)
    "Bellona's Frenzy of War": "shaking_minigame",
    "Olympian Fissure": "shaking_minigame",
    "Wrath of Olympus": "shaking_minigame",
}


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


_ROD_NORM = {_norm(n): n for n in ROD_NAMES}
_ENCH_NORM = {_norm(n): n for n in DATA["enchants"]}
_AREA_NORM = {_norm(n): n for n in DATA["areas"]}


def match(text, table, cutoff):
    """Best fuzzy match of OCR text against a {normalised: real name} table."""
    t = _norm(text)
    if not t:
        return None
    if t in table:
        return table[t]
    m = difflib.get_close_matches(t, list(table), n=1, cutoff=cutoff)
    if m:
        return table[m[0]]
    # OCR line might hold extra words ("Clever Pinion's Aria"): try every sub-span
    words = t.split()
    best, best_r = None, cutoff
    for i in range(len(words)):
        for j in range(i + 1, min(len(words), i + 5) + 1):
            sub = " ".join(words[i:j])
            for k in difflib.get_close_matches(sub, list(table), n=1, cutoff=best_r):
                r = difflib.SequenceMatcher(None, sub, k).ratio()
                if r > best_r:
                    best, best_r = table[k], r
    return best


def rod_info(name):
    if not name or name not in DATA["rods"]:
        return None
    control, resil, flags = DATA["rods"][name]
    try:
        c = float(control)
    except ValueError:
        c = 0.0
    return {"name": name, "control": c, "resilience": resil, "flags": flags.split() if flags else [],
            "bar_width": max(0.02, min(1.0, 0.30 + c))}


def mode_for(rod, area=None):
    """Which reel logic to use, plus warnings, from the detected rod/area."""
    notes = []
    mode = "auto"
    if rod:
        f = rod["flags"]
        if "dark_fish" in f:
            mode = "remembrance"
        if "two_fish" in f:
            notes.append(f"{rod['name']} has two fish in its minigame; only one is followed.")
        if "notes" in f:
            if rod["name"] == "Pinion's Aria":
                mode = "pinion"          # catches the falling notes (that's its perfect catch)
            else:
                notes.append(f"{rod['name']} has falling notes; this mode keeps the fish in the bar but doesn't "
                             "chase notes yet (only Pinion's Aria's notes are handled).")
        if "click_progress" in f:
            notes.append(f"{rod['name']} gains progress per click; played like a normal rod.")
    if area and AREA_FLAGS.get(area) == "shaking_minigame":
        notes.append(f"In {area} the minigame shakes; scanning a taller area to compensate.")
    return mode, notes


# --------------------------------------------------------------- window

def find_roblox_window():
    """{'left','top','width','height'} of the Roblox game window in screen points, or None."""
    if sys.platform == "darwin":
        try:
            import Quartz
            infos = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID)
            best = None
            for w in infos or []:
                owner = str(w.get("kCGWindowOwnerName", "") or "")
                if "roblox" not in owner.lower() or "studio" in owner.lower():
                    continue
                if int(w.get("kCGWindowLayer", 0)) != 0:
                    continue
                b = w.get("kCGWindowBounds", {})
                rect = {"left": int(b["X"]), "top": int(b["Y"]), "width": int(b["Width"]), "height": int(b["Height"])}
                if rect["width"] < 300 or rect["height"] < 200:
                    continue
                if best is None or rect["width"] * rect["height"] > best["width"] * best["height"]:
                    best = rect
            if best and best["height"] > 400:
                # windowed mode has a ~28pt title bar on top; skip it
                tb = 28 if best["top"] > 0 else 0
                best = dict(best, top=best["top"] + tb, height=best["height"] - tb)
            return best
        except Exception:
            return None
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            found = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            def cb(hwnd, _):
                if not user32.IsWindowVisible(hwnd):
                    return True
                n = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value.strip() == "Roblox":
                    found.append(hwnd)
                return True
            user32.EnumWindows(cb, 0)
            for hwnd in found:
                r = wintypes.RECT()
                user32.GetClientRect(hwnd, ctypes.byref(r))
                pt = wintypes.POINT(0, 0)
                user32.ClientToScreen(hwnd, ctypes.byref(pt))
                if r.right > 300 and r.bottom > 200:
                    return {"left": pt.x, "top": pt.y, "width": r.right, "height": r.bottom}
        except Exception:
            return None
    return None


# ------------------------------------------------------------------ OCR

_WIN_OCR = {}


def _win_engine():
    """Windows' built-in text recognition (Windows.Media.Ocr), or None."""
    if "engine" in _WIN_OCR:
        return _WIN_OCR["engine"]
    eng = None
    try:
        from winrt.windows.media.ocr import OcrEngine
        eng = OcrEngine.try_create_from_user_profile_languages()
        if eng is None:
            from winrt.windows.globalization import Language
            eng = OcrEngine.try_create_from_language(Language("en-US"))
    except Exception as e:
        _WIN_OCR["error"] = repr(e)
        eng = None
    _WIN_OCR["engine"] = eng
    return eng


def _win_bitmap(img_rgb):
    """numpy RGB -> SoftwareBitmap (BGRA8). pywinrt versions differ in how a byte buffer is
    passed, so a few ways are tried; the one that works is remembered."""
    from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode
    h, w = img_rgb.shape[:2]
    bgra = np.empty((h, w, 4), np.uint8)
    bgra[:, :, 0] = img_rgb[:, :, 2]
    bgra[:, :, 1] = img_rgb[:, :, 1]
    bgra[:, :, 2] = img_rgb[:, :, 0]
    bgra[:, :, 3] = 255
    raw = bgra.tobytes()

    def via_buffer():
        from winrt.windows.storage.streams import Buffer
        buf = Buffer(len(raw))
        mv = memoryview(buf)
        mv[:len(raw)] = raw
        buf.length = len(raw)
        return buf

    def via_bytes():
        return raw

    def via_writer():
        from winrt.windows.storage.streams import DataWriter
        dw = DataWriter()
        dw.write_bytes(raw)
        return dw.detach_buffer()

    ways = [via_bytes, via_buffer, via_writer]
    if "way" in _WIN_OCR:
        ways = [_WIN_OCR["way"]] + [f for f in ways if f is not _WIN_OCR["way"]]
    last = None
    for way in ways:
        try:
            bmp = SoftwareBitmap.create_copy_from_buffer(way(), BitmapPixelFormat.BGRA8, w, h,
                                                        BitmapAlphaMode.PREMULTIPLIED)
            _WIN_OCR["way"] = way
            return bmp
        except Exception as e:
            last = e
    raise RuntimeError(f"couldn't hand the image to Windows OCR: {last!r}")


def _win_ocr(img_rgb):
    eng = _win_engine()
    if eng is None:
        return []
    import asyncio
    h, w = img_rgb.shape[:2]
    try:
        from winrt.windows.media.ocr import OcrEngine
        lim = int(OcrEngine.max_image_dimension or 0)
    except Exception:
        lim = 0
    step = 1
    if lim and max(h, w) > lim:
        step = int(np.ceil(max(h, w) / lim))
        img_rgb = np.ascontiguousarray(img_rgb[::step, ::step])
    bmp = _win_bitmap(img_rgb)

    async def go():
        return await eng.recognize_async(bmp)
    res = asyncio.run(go())
    out = []
    for line in res.lines:
        xs0, ys0, xs1, ys1 = [], [], [], []
        for wd in line.words:
            r = wd.bounding_rect
            xs0.append(r.x); ys0.append(r.y); xs1.append(r.x + r.width); ys1.append(r.y + r.height)
        if not xs0:
            continue
        out.append((str(line.text), 0.9, (min(xs0) * step, min(ys0) * step, max(xs1) * step, max(ys1) * step)))
    return out


def ocr_available():
    if sys.platform == "win32":
        return _win_engine() is not None
    if sys.platform != "darwin":
        return False
    try:
        import Vision  # noqa: F401
        return True
    except Exception:
        return False


def ocr(img_rgb, upscale=1):
    """Text lines in an RGB image: [(text, confidence, (x0, y0, x1, y1))] in image pixels."""
    if not ocr_available():
        return []
    if sys.platform == "win32":
        if upscale > 1:
            img_rgb = np.repeat(np.repeat(img_rgb, upscale, axis=0), upscale, axis=1)
        return [(t, c, tuple(v / upscale for v in b)) for t, c, b in _win_ocr(img_rgb)]
    import Vision
    from Foundation import NSData
    import mss.tools
    if upscale > 1:
        img_rgb = np.repeat(np.repeat(img_rgb, upscale, axis=0), upscale, axis=1)
    h, w = img_rgb.shape[:2]
    png = mss.tools.to_png(np.ascontiguousarray(img_rgb).tobytes(), (w, h))
    data = NSData.dataWithBytes_length_(png, len(png))
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(0)            # accurate
    req.setUsesLanguageCorrection_(False)
    handler.performRequests_error_([req], None)
    out = []
    for o in req.results() or []:
        cands = o.topCandidates_(1)
        if not cands:
            continue
        bb = o.boundingBox()
        x0 = bb.origin.x * w
        x1 = x0 + bb.size.width * w
        y1 = (1 - bb.origin.y) * h
        y0 = y1 - bb.size.height * h
        out.append((str(cands[0].string()), float(cands[0].confidence()),
                    (x0 / upscale, y0 / upscale, x1 / upscale, y1 / upscale)))
    return out


def ocr_fast(img_rgb):
    """Quick text read for short, large words (the Shake button): Vision's fast mode on the
    image handed over directly (no PNG). Same output format as ocr()."""
    if not ocr_available():
        return []
    if sys.platform == "win32":
        return _win_ocr(img_rgb)
    try:
        import Vision
        import Quartz
        from Foundation import NSData
        h, w = img_rgb.shape[:2]
        rgba = np.empty((h, w, 4), np.uint8)
        rgba[:, :, :3] = img_rgb
        rgba[:, :, 3] = 255
        raw = rgba.tobytes()
        data = NSData.dataWithBytes_length_(raw, len(raw))
        provider = Quartz.CGDataProviderCreateWithCFData(data)
        cs = Quartz.CGColorSpaceCreateDeviceRGB()
        cg = Quartz.CGImageCreate(w, h, 8, 32, 4 * w, cs, Quartz.kCGImageAlphaNoneSkipLast, provider,
                                  None, False, Quartz.kCGRenderingIntentDefault)
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(1)            # fast
        req.setUsesLanguageCorrection_(False)
        handler.performRequests_error_([req], None)
        out = []
        for o in req.results() or []:
            cands = o.topCandidates_(1)
            if not cands:
                continue
            bb = o.boundingBox()
            x0 = bb.origin.x * w
            x1 = x0 + bb.size.width * w
            y1 = (1 - bb.origin.y) * h
            y0 = y1 - bb.size.height * h
            out.append((str(cands[0].string()), float(cands[0].confidence()), (x0, y0, x1, y1)))
        return out
    except Exception:
        return ocr(img_rgb)


def find_shake(win_img, reader=None):
    """Where the Shake button is in a screenshot of the Roblox window: (x, y) in image
    pixels, or None when there is no Shake button. Only a box whose whole text reads
    "shake" counts, so item descriptions, chat and menus never match. The very top
    (Roblox/Fisch menu bar) and the bottom (hotbar) are never looked at."""
    reader = reader or ocr_fast
    H, W = win_img.shape[:2]
    top, bot = int(0.07 * H), int(0.86 * H)
    crop = win_img[top:bot]
    step = 2 if W >= 1600 else 1                 # the word is big: half resolution is plenty
    small = np.ascontiguousarray(crop[::step, ::step])
    best = None
    for text, conf, (x0, y0, x1, y1) in reader(small):
        t = "".join(ch for ch in text.lower() if ch.isalpha())
        if not (4 <= len(t) <= 6):
            continue
        if t != "shake" and difflib.SequenceMatcher(None, t, "shake").ratio() < 0.8:
            continue
        if conf < 0.3:
            continue
        if (y1 - y0) * step < 0.012 * H:         # far too small to be the button
            continue
        cx, cy = (x0 + x1) / 2 * step, top + (y0 + y1) / 2 * step
        if best is None or conf > best[2]:
            best = (cx, cy, conf)
    return None if best is None else (best[0], best[1])


def slot1_lines(lines, W, H):
    """OCR lines [(text, conf, box)] of the bottom strip -> the lines inside hotbar slot 1,
    top to bottom. Slot 1 is found next to slot 2 ("Equipment Bag"); without that anchor,
    it's the leftmost slot of the centred slot row."""
    def cx(l): return (l[2][0] + l[2][2]) / 2
    def cy(l): return (l[2][1] + l[2][3]) / 2
    eq = None
    for l in lines:
        t = _norm(l[0])
        if t in ("equipment", "equipment bag") or difflib.SequenceMatcher(None, t, "equipment").ratio() >= 0.8:
            if eq is None or l[2][1] > eq[2][1]:
                eq = l
    if eq is not None:
        ew = eq[2][2] - eq[2][0]
        lh = max(4.0, eq[2][3] - eq[2][1])
        pitch = ew * 1.2                              # slots are a little wider than "Equipment"
        c1 = eq[2][0] - 0.1 * ew - pitch / 2          # centre of slot 1, just left of slot 2
        out = [l for l in lines if abs(cx(l) - c1) <= 0.62 * pitch
               and eq[2][1] - 3.5 * lh <= cy(l) <= eq[2][3] + 3.5 * lh and l is not eq]
    else:
        row = [l for l in lines if cy(l) > 0.55 * H and 0.25 * W < cx(l) < 0.75 * W]
        if not row:
            return []
        first = min(row, key=lambda l: l[2][0])
        width = max(first[2][2] - first[2][0], 0.03 * W)
        out = [l for l in row if cx(l) <= first[2][0] + width * 1.05]
    return sorted(out, key=cy)


def read_rod(slot):
    """Rod name from the lines of slot 1. Names can be split over two lines ("Olympian" /
    "Godbreaker", "Plaguereave" / "r"), so runs of neighbouring lines are tried too.
    Returns (name, set of line indexes used)."""
    best, best_score, used = None, 0.0, set()
    n = len(slot)
    for i in range(n):
        for j in range(i + 1, min(n, i + 3) + 1):
            parts = [slot[k][0] for k in range(i, j)]
            for joined in {" ".join(parts), "".join(parts)}:
                t = _norm(joined)
                if not t:
                    continue
                for k in difflib.get_close_matches(t, list(_ROD_NORM), n=1, cutoff=0.78):
                    r = difflib.SequenceMatcher(None, t, k).ratio() + 0.01 * (j - i)
                    if r > best_score:
                        best, best_score, used = _ROD_NORM[k], r, set(range(i, j))
    return best, used


def read_enchants(texts):
    """Enchant names in the leftover slot lines (a rod can show two, sometimes on one line,
    e.g. "Clever Wise"; one enchant can also be split, e.g. "Glimmering" / "Crown")."""
    words = [w for t in texts for w in _norm(t).split()]
    found = []
    k = 0
    while k < len(words):
        hit = None
        for span in (3, 2, 1):                                # longest name first
            if k + span <= len(words):
                t = " ".join(words[k:k + span])
                m = difflib.get_close_matches(t, list(_ENCH_NORM), n=1, cutoff=0.8 if span > 1 else 0.86)
                if m:
                    hit = (_ENCH_NORM[m[0]], span)
                    break
        if hit:
            if hit[0] not in found:
                found.append(hit[0])
            k += hit[1]
        else:
            k += 1
    return found


def identify(win_img):
    """Read rod / enchant / area off a screenshot of the Roblox window (RGB array)."""
    H, W = win_img.shape[:2]
    result = {"rod": None, "enchant": None, "area": None, "raw": {}}
    if not ocr_available():
        result["error"] = "text recognition not available on this computer"
        return result
    # hotbar: bottom strip of the window. Fisch keeps your rod in slot 1, so only text inside
    # that slot counts (descriptions, bait text, buff icons and fish names like "Sparkling
    # Celestial Anglerfish" used to be mistaken for the rod).
    y0 = int(H * 0.84)
    lines = ocr(win_img[y0:], upscale=2)
    result["raw"]["hotbar"] = [t for t, _, _ in lines]
    slot = slot1_lines(lines, W, H - y0)
    result["raw"]["slot1"] = [t for t, _, _ in slot]
    rod, used = read_rod(slot)
    result["rod"] = rod
    if rod:
        rest = [t for k, (t, _, _) in enumerate(slot) if k not in used]
        ench = read_enchants(rest)
        if ench:
            result["enchant"] = ench[0]
            if len(ench) > 1:
                result["enchant2"] = ench[1]
        elif rest:
            result["raw"]["above_rod"] = rest
    # area: big title near the top of the screen, usually in [brackets]
    top = ocr(win_img[: int(H * 0.22)], upscale=1)
    result["raw"]["top"] = [t for t, _, _ in top]
    for text, conf, box in sorted(top, key=lambda l: -(l[2][3] - l[2][1])):  # biggest text first
        inner = re.findall(r"\[([^\]]+)\]", text)
        for cand in (inner or [text]):
            a = match(cand, _AREA_NORM, 0.82)
            if a:
                result["area"] = a
                break
        if result["area"]:
            break
    return result


# ---------------------------------------------------------- reel locate

def _shift(a, k):
    out = np.empty_like(a)
    if k > 0:
        out[k:] = a[:-k]
        out[:k] = a[:1]
    elif k < 0:
        out[:k] = a[-k:]
        out[k:] = a[-1:]
    else:
        out[:] = a
    return out


def row_gradient(row):
    """Horizontal colour change per column for one (W, 3) row."""
    g = np.abs(_shift(row, -2) - _shift(row, 2)).sum(axis=1)
    return g


def locate_reel(img, y_range=(0.70, 0.95), width_range=(0.30, 0.52), center_tol=0.0012, min_score=None,
                ref_h=None):
    """Find the reel track in a screenshot of the Roblox window (RGB array).
    Returns dict(x0, x1, y, half) in image pixels, plus a confidence score, or None.

    The track is a long horizontal bar, horizontally centred, in the lower part of the
    window. Its two ends are strong vertical edges at the same distance from the centre,
    and between them the row is mostly smooth (track + bar, broken only by the fish
    line and a few icons).

    Checked against real footage: in every recording the track is centred to within 3 px,
    ~39-41% of the window wide, ~3% of the window tall, at 80-89% of the window height.
    Real tracks score 3000-7000 at 1080p; look-alikes (empty inventory rows, menus) score
    <900 or are too tall, and are rejected; the caller also confirms a bar is inside."""
    H, W = img.shape[:2]
    ref_h = ref_h or H                 # full window height (img may be just its lower part)
    f = img.astype(np.int16)
    ys = range(int(H * y_range[0]), int(H * y_range[1]), 2)
    dmin, dmax = int(W * width_range[0] / 2), int(W * width_range[1] / 2)
    ds = np.arange(dmin, dmax)
    best_rows = []
    cxs = sorted({W // 2 + int(round(o * W)) for o in np.linspace(-center_tol, center_tol, 5)})
    if min_score is None:
        min_score = 1500 * ref_h / 1080
    for y in ys:
        row = f[max(0, y - 1):y + 2].mean(axis=0)
        g = row_gradient(row)
        gmax = np.maximum.reduce([_shift(g, k) for k in range(-3, 4)])
        smooth = (g < 30).astype(np.int32)
        cs = np.concatenate(([0], np.cumsum(smooth)))
        best = (0.0, 0, 0)
        for cx in cxs:
            li, ri = cx - ds, cx + ds
            ok = (li >= 3) & (ri < W - 3)
            if not ok.any():
                continue
            e = np.minimum(gmax[np.clip(li, 0, W - 1)], gmax[np.clip(ri, 0, W - 1)]).astype(float)
            a, b = np.clip(li + 5, 0, W), np.clip(ri - 5, 0, W)
            frac = (cs[b] - cs[a]) / np.maximum(1, b - a)
            score = np.where(ok & (frac >= 0.72) & (e >= 40), e * frac, 0)
            k = int(np.argmax(score))
            if score[k] > best[0]:
                best = (float(score[k]), int(ds[k]), cx)
        best_rows.append((y, *best))
    # group consecutive rows that agree on the same ends
    bands, cur = [], []
    for y, s, d, cx in best_rows:
        if s > 0 and cur and abs(d - cur[-1][2]) <= 3 and abs(cx - cur[-1][3]) <= 3 and y - cur[-1][0] <= 2:
            cur.append((y, s, d, cx))
        else:
            if cur:
                bands.append(cur)
            cur = [(y, s, d, cx)] if s > 0 else []
    if cur:
        bands.append(cur)
    good = []
    for b in bands:
        height = b[-1][0] - b[0][0] + 2
        if not (0.012 * ref_h <= height <= 0.04 * ref_h):
            continue
        d = int(np.median([r[2] for r in b]))
        cx = int(np.median([r[3] for r in b]))
        good.append({"x0": cx - d, "x1": cx + d, "y": (b[0][0] + b[-1][0]) // 2, "half": height / 2,
                     "score": sum(r[1] for r in b)})
    good = [g_ for g_ in good if g_["score"] >= min_score]
    if not good:
        return None
    return max(good, key=lambda g_: g_["score"])


def reel_present(img, center, half, mx):
    """Is the reel track really in this capture? `img` is RGB and spans the track plus `mx`
    pixels on each side and a few rows above/below; `center`/`half` are the track's centre
    row and half-height in image pixels.

    The track is a solid band: along almost its whole length its top and bottom edges
    contrast with what's just outside it, and both ends are sharp vertical edges. Scenery
    (sand, water, wood), the hotbar and text rows don't do that, so they no longer pass
    as a reel. Returns (ok, score) where score is the weaker edge fraction (0..1)."""
    f = img.astype(np.int16)
    H, W = f.shape[:2]
    c, hh = float(center), max(2.0, float(half))
    x0, x1 = int(mx) + 4, W - int(mx) - 4
    if x1 - x0 < 20:
        return False, 0.0

    def row(y):
        y = int(round(y))
        if y < 0 or y >= H:
            return None
        return f[y, x0:x1]

    def edge_frac(y_out, y_in):
        a, b = row(y_out), row(y_in)
        if a is None or b is None:
            return 0.0
        d = np.abs(a - b).max(axis=1)
        return float((d > 24).mean())

    # the track may sit a few pixels off our estimate: take the best nearby centre
    top = bot = 0.0
    for dc in (0, -1, 1, -2, 2, -3, 3):
        cc = c + dc
        t_ = max(edge_frac(cc - hh - k, cc - 0.45 * hh) for k in (2, 3, 4, 5))
        b_ = max(edge_frac(cc + hh + k, cc + 0.45 * hh) for k in (2, 3, 4, 5))
        if min(t_, b_) > min(top, bot):
            top, bot = t_, b_
        if min(top, bot) >= 0.9:
            break
    # the ends: compare just outside each end with just inside it, over the track rows
    rows = slice(max(0, int(c - 0.5 * hh)), min(H, int(c + 0.5 * hh) + 1))
    ends = []
    m = int(mx)
    for side in (0, 1):
        best = 0.0
        for k in (2, 3, 4, 6):              # allow the end to be a few pixels off our estimate
            out_x, in_x = (m - k, m + k + 2) if side == 0 else (W - 1 - m + k, W - 1 - m - k - 2)
            if 0 <= out_x < W and 0 <= in_x < W:
                d = np.abs(f[rows, out_x] - f[rows, in_x]).max(axis=1)
                best = max(best, float((d > 24).mean()))
        ends.append(best)
    end_ok = sum(e >= 0.6 for e in ends)
    # inside, the track is smooth (track colour + bar), broken only by the fish line and icons
    core = np.median(f[max(0, int(c - 0.4 * hh)):min(H, int(c + 0.4 * hh) + 1), x0:x1], axis=0)
    smooth = float((row_gradient(core) < 30).mean())
    score = min(top, bot, smooth)
    ok = top >= 0.6 and bot >= 0.6 and (top + bot) >= 1.45 and end_ok == 2 and smooth >= 0.8
    return ok, score


def confirm_reel(img, loc, expected_w=None):
    """A located track only counts if a bar is actually sitting in it."""
    x0, x1 = loc["x0"], loc["x1"]
    c, half = loc["y"], loc["half"]
    y0 = max(0, int(c - half - 8))
    y1 = min(img.shape[0], int(c + half + 9))
    bar, fish, tf = detect_auto(img[y0:y1, x0:x1], c - y0, half, expected_w)
    if bar is None:
        return False, None
    w = bar[1] - bar[0]
    if not (0.02 <= w <= 0.8):
        return False, None
    if expected_w and not (0.5 * expected_w <= w <= 1.8 * expected_w):
        return False, None
    if tf < 0.2:
        return False, None
    # colours, for modes that use colour matching (Remembrance)
    core = img[int(c - 0.4 * half):int(c + 0.4 * half) + 1, x0:x1]
    C = np.median(core, axis=0)
    W = C.shape[0]
    inbar = np.zeros(W, bool)
    inbar[int(bar[0] * W):int(bar[1] * W)] = True
    track_col = [int(v) for v in np.median(C[~inbar], axis=0)] if (~inbar).any() else None
    bar_col = [int(v) for v in np.median(C[inbar], axis=0)]
    return True, {"bar": bar, "fish": fish, "track_frac": tf, "track_color": track_col, "bar_color": bar_col}


# ---------------------------------------------------- auto bar/fish detection

def detect_auto(img, center, half, expected_w=None, memory=None, x_range=None):
    """Skin-proof detection on a capture that contains the whole track height plus a few
    pixels above and below it. `center`/`half` are the track's centre row and half-height
    in image pixels. Returns (bar(l, r) | None, fish | None, track_frac) as fractions of width."""
    memory = memory if memory is not None else {}
    f = img.astype(np.int16)
    Himg, W = f.shape[:2]
    c, hh = int(round(center)), max(1.0, half)
    core = f[max(0, int(c - 0.45 * hh)):min(Himg, int(c + 0.45 * hh) + 1)]
    C = np.median(core, axis=0)                                  # (W, 3) colour per column
    g = row_gradient(C)
    T = max(36.0, 4.0 * float(np.median(g)))

    # track colour model: the two ends of the track (unless the bar is parked on one of them)
    # sample a little inside each end (the very ends have rounded caps / outlines)
    a, b = max(2, int(0.015 * W)), max(5, int(0.045 * W))
    left, right = np.median(C[a:b], axis=0), np.median(C[W - b:W - a], axis=0)
    xs = np.arange(W) / max(1, W - 1)

    def dist(a, b):        # colour difference that also notices pastel-vs-grey
        sa = a.max(axis=-1) - a.min(axis=-1)
        sb = b.max(axis=-1) - b.min(axis=-1)
        return np.maximum(np.abs(a - b).max(axis=-1), 1.5 * np.abs(sa - sb))

    end_gap = float(dist(left, right))
    one_end_covered = end_gap >= 35
    if not one_end_covered:
        track_col = left[None, :] * (1 - xs[:, None]) + right[None, :] * xs[:, None]
    else:
        # one end is covered by the bar: which end is the track?
        nl = int((dist(C, left[None, :]) < 25).sum())
        nr = int((dist(C, right[None, :]) < 25).sum())
        known = memory.get("track_ref")
        forced = memory.get("force_ref")
        bw = expected_w or memory.get("bar_w")
        if forced in ("left", "right"):
            # the macro saw the bar ignore our input with the other choice: the sides were swapped
            ref = left if forced == "left" else right
        elif known is not None:
            # the empty track's colour from earlier frames decides
            ref = left if dist(left, known) <= dist(right, known) else right
        elif bw:
            # the bar is the part whose width matches this rod's bar (wide bars cover more
            # than half the track, so "the bigger part is the track" would be backwards)
            frac_l = 1 - nl / W            # what's NOT the left colour, if left were the track
            frac_r = 1 - nr / W
            ref = left if abs(frac_l - bw) <= abs(frac_r - bw) else right
        else:
            ref = left if nl >= nr else right
        track_col = np.repeat(ref[None, :], W, axis=0)
    if "track_col" in memory and memory["track_col"].shape == track_col.shape:
        prev = memory["track_col"]
        if not one_end_covered and float(dist(np.median(prev, axis=0), np.median(track_col, axis=0))) > 30:
            pass        # both ends show the track right now and the memory disagrees: it was wrong
        else:
            # once known, the empty-track look is stable: blend slowly
            track_col = 0.8 * prev + 0.2 * track_col
    diff = dist(C, track_col)
    thr = max(18.0, min(35.0, 3.0 * float(np.median(diff)) + 10))
    if one_end_covered and "track_col" not in memory:
        thr = max(thr, 30.0)          # constant track model: tolerate the track's own gradient
    non_track = diff > thr
    memory["track_col"] = np.where(non_track[:, None], memory.get("track_col", track_col), track_col)
    if not one_end_covered and (~non_track).sum() >= 0.1 * W:
        # both ends show empty track, so this is reliable: remember what empty track looks like, to tell the ends apart when the bar covers one
        tc = np.median(C[~non_track], axis=0)
        old = memory.get("track_ref")
        memory["track_ref"] = tc if old is None else 0.9 * old + 0.1 * tc

    # fish first: a thin line that differs from what's on BOTH sides of it (a bar's edge only
    # differs on one side, so it can't pass as the fish - even when the fish is right at the
    # edge, which is exactly when it matters). Strongest where the line sticks out above and
    # below the track.
    top, bot = int(c - hh), int(c + hh)
    kk = max(4, int(0.012 * W))

    def line(X):
        a = np.abs(X - _shift(X, kk)).max(axis=1)
        b = np.abs(X - _shift(X, -kk)).max(axis=1)
        return np.minimum(a, b).astype(float)
    score = line(C)
    for r0, r1 in ((top - 7, top - 2), (bot + 2, bot + 7)):
        r0, r1 = max(0, r0), min(Himg, r1)
        if r1 > r0:
            score += 0.7 * line(np.median(f[r0:r1], axis=0))
    score[:kk + 3] = 0
    score[-(kk + 3):] = 0
    fish = None
    fish_span = None
    floor = 3 * float(np.median(score)) + 20
    peaks = []
    s_ = score.copy()
    for _ in range(6):                                            # strongest few peaks
        x = int(np.argmax(s_))
        v = float(s_[x])
        if v <= max(30.0, floor):
            break
        lo, hi = x, x
        while lo > 0 and score[lo - 1] >= 0.5 * v:
            lo -= 1
        while hi < W - 1 and score[hi + 1] >= 0.5 * v:
            hi += 1
        if (hi - lo + 1) <= max(4, 0.016 * W):                    # the fish is a thin line
            peaks.append(((lo + hi) / 2 / W, v, lo, hi))
        s_[max(0, lo - 2):hi + 3] = 0
    if peaks:
        last = memory.get("fish")
        pick = peaks[0]
        if last is not None:
            near = [p for p in peaks if abs(p[0] - last) < 0.12 and p[1] >= 0.5 * peaks[0][1]]
            pick = near[0] if near else peaks[0]
        fish, fish_span = pick[0], (pick[2], pick[3])
        memory["fish"] = fish

    # bar: the widest stretch of non-track columns. The fish line's columns count as bar only
    # when there's bar on both sides of it (fish inside the bar); a fish just outside the bar
    # must not stretch the bar out to reach it.
    nt = non_track.copy()
    if fish_span is not None:
        lo, hi = max(0, fish_span[0] - 2), min(W, fish_span[1] + 3)
        left_bar = nt[max(0, lo - 4):lo].any() if lo > 0 else False
        right_bar = nt[hi:hi + 4].any() if hi < W else False
        nt[lo:hi] = left_bar and right_bar
    bar = None
    spans = []
    # drop slivers (anti-aliased leftovers of the fish line), then bridge icons inside the bar
    pieces = [(a, b) for a, b in _runs(nt) if b - a >= max(3, int(0.006 * W))]
    for s, e in _merge(pieces, max(2, int(0.03 * W))):
        if (e - s) >= 0.02 * W:
            spans.append((s, e))
    if spans:
        if expected_w:
            s, e = min(spans, key=lambda se: abs((se[1] - se[0]) / W - expected_w) - 0.5 * (se[1] - se[0]) / W)
        else:
            s, e = max(spans, key=lambda se: se[1] - se[0])
        bar = (s / W, e / W)
        if not one_end_covered and 0.03 * W <= e - s <= 0.97 * W and 0.03 * W < s and e < 0.97 * W:
            # both ends show track, so this width is trustworthy: remember it
            w_ = (e - s) / W
            memory["bar_w"] = w_ if memory.get("bar_w") is None else 0.8 * memory["bar_w"] + 0.2 * w_
            memory.pop("force_ref", None)
    track_frac = float((~non_track).mean())
    return bar, fish, track_frac


def _runs(mask):
    if not mask.any():
        return []
    d = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.flatnonzero(d == 1).tolist(), np.flatnonzero(d == -1).tolist()))


def _merge(rs, gap):
    out = []
    for s, e in rs:
        if out and s - out[-1][1] <= gap:
            out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out
