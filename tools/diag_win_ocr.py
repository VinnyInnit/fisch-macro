import asyncio, numpy as np
from PIL import Image, ImageDraw, ImageFont
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode, BitmapDecoder
from winrt.windows.storage.streams import DataWriter, Buffer, InMemoryRandomAccessStream
eng = OcrEngine.try_create_from_user_profile_languages()
F = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 34)
def img(text, x):
    im = Image.new("RGB", (900, 300), (70, 80, 90)); d = ImageDraw.Draw(im); d.text((x, 120), text, font=F, fill=(255, 255, 255))
    return np.asarray(im)
def bgra(a):
    h, w = a.shape[:2]; b = np.empty((h, w, 4), np.uint8); b[..., 0] = a[..., 2]; b[..., 1] = a[..., 1]; b[..., 2] = a[..., 0]; b[..., 3] = 255
    return b.tobytes(), w, h
def m_bytes(a):
    raw, w, h = bgra(a); return SoftwareBitmap.create_copy_from_buffer(raw, BitmapPixelFormat.BGRA8, w, h, BitmapAlphaMode.PREMULTIPLIED)
def m_writer(a):
    raw, w, h = bgra(a); dw = DataWriter(); dw.write_bytes(raw); return SoftwareBitmap.create_copy_from_buffer(dw.detach_buffer(), BitmapPixelFormat.BGRA8, w, h, BitmapAlphaMode.PREMULTIPLIED)
def m_copyinto(a):
    raw, w, h = bgra(a); bmp = SoftwareBitmap(BitmapPixelFormat.BGRA8, w, h, BitmapAlphaMode.PREMULTIPLIED)
    dw = DataWriter(); dw.write_bytes(raw); bmp.copy_from_buffer(dw.detach_buffer()); return bmp
def m_png(a):
    import io
    bio = io.BytesIO(); Image.fromarray(a).save(bio, "PNG"); data = bio.getvalue()
    async def go():
        st = InMemoryRandomAccessStream(); dw = DataWriter(st); dw.write_bytes(data); await dw.store_async(); await dw.flush_async(); dw.detach_stream(); st.seek(0)
        dec = await BitmapDecoder.create_async(st)
        return await dec.get_software_bitmap_async()
    return asyncio.run(go())
async def rec(b): return await eng.recognize_async(b)
for name, f in [("bytes", m_bytes), ("writer", m_writer), ("copy_into", m_copyinto), ("png", m_png)]:
    out = []
    for text, x in [("Hello first", 50), ("Second words", 400), ("Third thing", 200)]:
        try:
            r = asyncio.run(rec(f(img(text, x))))
            out.append(r.text)
        except Exception as e:
            out.append("ERR " + repr(e)[:80])
    print(f"{name:10} -> {out}", flush=True)
# same method, but the engine called without asyncio.run each time (one loop)
async def all_in_one():
    res = []
    for text, x in [("Hello first", 50), ("Second words", 400)]:
        r = await eng.recognize_async(m_writer(img(text, x))); res.append(r.text)
    return res
print("one loop   ->", asyncio.run(all_in_one()))
