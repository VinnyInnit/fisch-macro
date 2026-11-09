"""
Mac-specific helpers (no-ops elsewhere):
  - permission checks: Accessibility (needed to press the mouse/keys - without it macOS
    silently drops every input), Screen Recording, Input Monitoring (hotkeys)
  - open the right System Settings pane
  - fast screen capture straight from the display (CoreGraphics), much quicker than
    mss's window-compositing capture; falls back to mss if unavailable
"""

import ctypes
import ctypes.util
import subprocess
import sys
import time

import numpy as np

IS_MAC = sys.platform == "darwin"

SETTINGS_URLS = {
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "screen": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    "input": "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
}


def _lib(path):
    try:
        return ctypes.cdll.LoadLibrary(path)
    except OSError:
        return None


_AS = _CG = _CF = _IOK = None
if IS_MAC:
    _AS = _lib("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    _CG = _lib("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    _CF = _lib("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    _IOK = _lib("/System/Library/Frameworks/IOKit.framework/IOKit")


def permissions():
    """{'accessibility': bool|None, 'screen': bool|None, 'input': bool|None}. None = unknown."""
    out = {"accessibility": None, "screen": None, "input": None}
    if not IS_MAC:
        return out
    try:
        _AS.AXIsProcessTrusted.restype = ctypes.c_bool
        out["accessibility"] = bool(_AS.AXIsProcessTrusted())
    except Exception:
        pass
    try:
        _CG.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        out["screen"] = bool(_CG.CGPreflightScreenCaptureAccess())
    except Exception:
        pass
    try:
        _IOK.IOHIDCheckAccess.restype = ctypes.c_int
        _IOK.IOHIDCheckAccess.argtypes = [ctypes.c_int]
        r = _IOK.IOHIDCheckAccess(1)          # 1 = listen-event (Input Monitoring); 0 granted, 1 denied
        out["input"] = (r == 0) if r in (0, 1) else None
    except Exception:
        pass
    return out


def open_settings(which):
    if IS_MAC and which in SETTINGS_URLS:
        subprocess.Popen(["open", SETTINGS_URLS[which]])
        return True
    return False


def request_accessibility_prompt():
    """Ask macOS to show its own 'allow accessibility' prompt (adds Terminal to the list)."""
    if not IS_MAC:
        return
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt
        AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
    except Exception:
        pass


# ------------------------------------------------------------ fast capture

class _Pt(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _Sz(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class _Rect(ctypes.Structure):
    _fields_ = [("origin", _Pt), ("size", _Sz)]


class _Raw:
    """Looks like an mss screenshot: np.asarray() gives BGRA."""

    def __init__(self, bgra):
        self._a = bgra

    def __array__(self, dtype=None, copy=None):
        return self._a


class FastSct:
    """Wraps an mss instance; grabs regions straight from the display on macOS."""

    def __init__(self, base):
        self.base = base
        self.monitors = base.monitors
        self.ok = False
        if not (IS_MAC and _CG and _CF):
            return
        try:
            cg, cf = _CG, _CF
            cg.CGDisplayCreateImageForRect.argtypes = [ctypes.c_uint32, _Rect]
            cg.CGDisplayCreateImageForRect.restype = ctypes.c_void_p
            cg.CGGetDisplaysWithPoint.argtypes = [_Pt, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                                                  ctypes.POINTER(ctypes.c_uint32)]
            cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
            cg.CGDisplayBounds.restype = _Rect
            for f in ("CGImageGetWidth", "CGImageGetHeight", "CGImageGetBytesPerRow", "CGImageGetBitsPerPixel"):
                getattr(cg, f).argtypes = [ctypes.c_void_p]
                getattr(cg, f).restype = ctypes.c_size_t
            cg.CGImageGetDataProvider.argtypes = [ctypes.c_void_p]
            cg.CGImageGetDataProvider.restype = ctypes.c_void_p
            cg.CGDataProviderCopyData.argtypes = [ctypes.c_void_p]
            cg.CGDataProviderCopyData.restype = ctypes.c_void_p
            cg.CGImageRelease.argtypes = [ctypes.c_void_p]
            cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
            cf.CFDataGetBytePtr.restype = ctypes.c_void_p
            cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
            cf.CFDataGetLength.restype = ctypes.c_long
            cf.CFRelease.argtypes = [ctypes.c_void_p]
            self.ok = True
        except Exception:
            self.ok = False

    def _fast(self, r):
        cg, cf = _CG, _CF
        disp = ctypes.c_uint32(0)
        count = ctypes.c_uint32(0)
        cg.CGGetDisplaysWithPoint(_Pt(r["left"] + 1, r["top"] + 1), 1, ctypes.byref(disp), ctypes.byref(count))
        if count.value == 0:
            return None
        b = cg.CGDisplayBounds(disp.value)
        rect = _Rect(_Pt(r["left"] - b.origin.x, r["top"] - b.origin.y), _Sz(r["width"], r["height"]))
        img = cg.CGDisplayCreateImageForRect(disp.value, rect)
        if not img:
            return None
        try:
            if cg.CGImageGetBitsPerPixel(img) != 32:
                return None
            w, h, bpr = cg.CGImageGetWidth(img), cg.CGImageGetHeight(img), cg.CGImageGetBytesPerRow(img)
            data = cg.CGDataProviderCopyData(cg.CGImageGetDataProvider(img))
            try:
                buf = ctypes.string_at(cf.CFDataGetBytePtr(data), cf.CFDataGetLength(data))
            finally:
                cf.CFRelease(data)
        finally:
            cg.CGImageRelease(img)
        arr = np.frombuffer(buf, np.uint8)[: h * bpr].reshape(h, bpr)[:, : w * 4].reshape(h, w, 4)
        return _Raw(arr)

    def grab(self, r):
        if self.ok:
            try:
                out = self._fast(r)
                if out is not None:
                    return out
            except Exception:
                self.ok = False
        return self.base.grab(r)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        try:
            self.base.close()
        except Exception:
            pass


def benchmark(sct, region, n=8):
    """Grabs per second for this capture method on this region."""
    t = time.perf_counter()
    for _ in range(n):
        sct.grab(region)
    return n / max(1e-6, time.perf_counter() - t)


def best_sct(base, region=None):
    """Return the faster of mss and the direct display capture (and grabs/sec of each).
    The two must agree on the picture, otherwise we keep mss."""
    fast = FastSct(base)
    info = {"method": "mss", "mss_fps": None, "fast_fps": None}
    if not fast.ok or region is None:
        return (fast if fast.ok else base), info
    try:
        a = np.asarray(base.grab(region))[:, :, :3].astype(int)
        b = fast._fast(region)
        if b is None:
            return base, info
        b = np.asarray(b)[:, :, :3].astype(int)
        if a.shape != b.shape or np.abs(a - b).mean() > 25:
            return base, info
        info["mss_fps"] = round(benchmark(base, region), 1)
        info["fast_fps"] = round(benchmark(fast, region), 1)
        if info["fast_fps"] > info["mss_fps"]:
            info["method"] = "display"
            return fast, info
    except Exception:
        pass
    return base, info
