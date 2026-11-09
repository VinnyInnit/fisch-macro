"""
60 fps screen reading on macOS with ScreenCaptureKit.

On recent macOS the old one-shot capture calls (what mss uses) take ~80-100 ms each, so the
macro could only look at the reel ~10 times a second. ScreenCaptureKit instead streams the
reel area to us as the game draws it (up to the game's frame rate, usually 60/s) with a
few ms of delay.

Everything here is optional: if the pyobjc ScreenCaptureKit bindings are missing, the
stream doesn't start, or its picture doesn't match a normal screenshot, `CaptureHub`
silently keeps using the old capture.
"""

import sys
import threading
import time

import numpy as np

IS_MAC = sys.platform == "darwin"

_SCK = _CM = _Q = None
_dispatch_queue_create = None
_import_error = None
if IS_MAC:
    try:
        import objc
        from Foundation import NSObject
        import ScreenCaptureKit as _SCK
        import CoreMedia as _CM
        import Quartz as _Q
        try:
            from dispatch import dispatch_queue_create as _dispatch_queue_create
        except Exception:
            try:
                from libdispatch import dispatch_queue_create as _dispatch_queue_create
            except Exception:
                _dispatch_queue_create = None
    except Exception as e:          # bindings not installed
        _SCK = None
        _import_error = repr(e)
else:
    _import_error = "not macOS"


_OWNER = [None]          # fallback if PyObjC won't keep a Python attribute on the helper objects


def available():
    return _SCK is not None, (_import_error or "")


if _SCK is not None:
    _OutProto = objc.protocolNamed("SCStreamOutput")
    _DelProto = objc.protocolNamed("SCStreamDelegate")

    class _Output(NSObject, protocols=[_OutProto]):
        def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, otype):
            owner = getattr(self, "owner", None) or _OWNER[0]
            if owner is not None:
                owner._on_frame(sbuf, otype)

    class _Delegate(NSObject, protocols=[_DelProto]):
        def stream_didStopWithError_(self, stream, error):
            owner = getattr(self, "owner", None) or _OWNER[0]
            if owner is not None:
                owner._dead = f"stream stopped: {error}"


class SckStream:
    """Streams one rectangle of the screen (global points) as BGRA arrays."""

    def __init__(self, rect, scale=1.0, fps=120):
        self.rect = dict(rect)
        self.scale = float(scale)
        self.fps = fps
        self.seq = 0
        self.frame = None           # latest BGRA array
        self.frame_t = 0.0
        self.cond = threading.Condition()
        self._dead = None
        self.error = None
        self._keep = []

    # -- callbacks (run on a dispatch queue thread)
    def _on_frame(self, sbuf, otype):
        try:
            if otype != _SCK.SCStreamOutputTypeScreen:
                return
            try:
                att = _CM.CMSampleBufferGetSampleAttachmentsArray(sbuf, False)
                if att is not None and len(att):
                    st = att[0].get(_SCK.SCStreamFrameInfoStatus)
                    if st is not None and int(st) != int(_SCK.SCFrameStatusComplete):
                        return          # idle/blank frame: no new pixels
            except Exception:
                pass
            pb = _CM.CMSampleBufferGetImageBuffer(sbuf)
            if pb is None:
                return
            _Q.CVPixelBufferLockBaseAddress(pb, 1)
            try:
                h = int(_Q.CVPixelBufferGetHeight(pb))
                w = int(_Q.CVPixelBufferGetWidth(pb))
                bpr = int(_Q.CVPixelBufferGetBytesPerRow(pb))
                base = _Q.CVPixelBufferGetBaseAddress(pb)
                buf = base.as_buffer(bpr * h)
                arr = np.frombuffer(buf, np.uint8, count=bpr * h).reshape(h, bpr)[:, :w * 4]
                arr = arr.reshape(h, w, 4).copy()
            finally:
                _Q.CVPixelBufferUnlockBaseAddress(pb, 1)
            with self.cond:
                self.frame = arr
                self.frame_t = time.perf_counter()
                self.seq += 1
                self.cond.notify_all()
        except Exception as e:      # never let an exception escape into the dispatch queue
            self.error = repr(e)

    # -- control
    def start(self, timeout=2.0):
        if _SCK is None:
            self.error = _import_error
            return False
        try:
            box, ev = {}, threading.Event()

            def got(content, err):
                box["c"], box["e"] = content, err
                ev.set()
            _SCK.SCShareableContent.getShareableContentWithCompletionHandler_(got)
            if not ev.wait(timeout) or box.get("c") is None:
                self.error = f"no shareable content ({box.get('e')})"
                return False
            r = self.rect
            cx, cy = r["left"] + r["width"] / 2, r["top"] + r["height"] / 2
            disp, frame = None, None
            for d in box["c"].displays():
                f = d.frame()
                if f.origin.x <= cx < f.origin.x + f.size.width and f.origin.y <= cy < f.origin.y + f.size.height:
                    disp, frame = d, f
                    break
            if disp is None:
                self.error = "reel is not on a display"
                return False
            flt = _SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(disp, [])
            cfg = _SCK.SCStreamConfiguration.alloc().init()
            cfg.setSourceRect_(_Q.CGRectMake(r["left"] - frame.origin.x, r["top"] - frame.origin.y,
                                             r["width"], r["height"]))
            cfg.setWidth_(int(round(r["width"] * self.scale)))
            cfg.setHeight_(int(round(r["height"] * self.scale)))
            cfg.setMinimumFrameInterval_(_CM.CMTimeMake(1, int(self.fps)))
            cfg.setPixelFormat_(1111970369)      # 'BGRA'
            cfg.setShowsCursor_(False)
            try:
                cfg.setQueueDepth_(4)
            except Exception:
                pass
            out, dele = _Output.alloc().init(), _Delegate.alloc().init()
            _OWNER[0] = self
            try:
                out.owner = dele.owner = self
            except Exception:
                pass
            stream = _SCK.SCStream.alloc().initWithFilter_configuration_delegate_(flt, cfg, dele)
            q = _dispatch_queue_create(b"fisch.capture", None) if _dispatch_queue_create else None
            res = stream.addStreamOutput_type_sampleHandlerQueue_error_(out, _SCK.SCStreamOutputTypeScreen, q, None)
            if isinstance(res, tuple):
                if not res[0]:
                    self.error = f"addStreamOutput failed: {res[1]}"
                    return False
            started, sev = {}, threading.Event()

            def on_start(err):
                started["e"] = err
                sev.set()
            stream.startCaptureWithCompletionHandler_(on_start)
            self._keep = [flt, cfg, out, dele, stream, q]
            self.stream = stream
            if not sev.wait(timeout) or started.get("e") is not None:
                self.error = f"start failed: {started.get('e')}"
                self.stop()
                return False
            # first frame
            with self.cond:
                self.cond.wait_for(lambda: self.frame is not None, timeout)
            if self.frame is None:
                self.error = "no frames arrived"
                self.stop()
                return False
            return True
        except Exception as e:
            self.error = repr(e)
            return False

    def stop(self):
        st = getattr(self, "stream", None)
        if st is not None:
            try:
                st.stopCaptureWithCompletionHandler_(lambda err: None)
            except Exception:
                pass
        self.stream = None
        self._dead = self._dead or "stopped"

    @property
    def alive(self):
        return self._dead is None and getattr(self, "stream", None) is not None


class _Raw:
    def __init__(self, bgra):
        self._a = bgra

    def __array__(self, dtype=None, copy=None):
        return self._a


class CaptureHub:
    """Drop-in for an mss instance (`.grab(region)`, `.monitors`). Regions inside the
    streamed reel area come from the ScreenCaptureKit stream; everything else (finding the
    reel, reading the rod name) uses the normal capture."""

    def __init__(self, base, log=None):
        self.base = base
        self.monitors = base.monitors
        self.log = log or (lambda m: None)
        self.stream = None
        self.region = None
        self.method = "mss"
        self.why = ""
        self._failed_regions = set()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def close(self):
        if self.stream:
            self.stream.stop()
        self.stream = None
        try:
            self.base.__exit__(None, None, None)
        except Exception:
            pass

    def _key(self, r):
        return (int(r["left"]), int(r["top"]), int(r["width"]), int(r["height"]))

    def want(self, region):
        """Stream this region from now on (call when the reel's position is known)."""
        if not IS_MAC or _SCK is None:
            self.why = _import_error or ""
            return False
        key = self._key(region)
        if self.stream is not None and self.stream.alive and self._key(self.region) == key:
            return True
        if key in self._failed_regions:
            return False
        if self.stream is not None:
            self.stream.stop()
            self.stream = None
        # scale (image pixels per point) as the normal capture sees it
        try:
            ref = np.asarray(self.base.grab(region))
            scale = ref.shape[1] / max(1, region["width"])
        except Exception:
            ref, scale = None, 1.0
        s = SckStream(region, scale)
        if not s.start():
            self.why = s.error or "unknown"
            self._failed_regions.add(key)
            self.method = "mss"
            self.log(f"Fast screen reading unavailable ({self.why}); using the slower method")
            return False
        # the stream must show the same picture as a normal screenshot of that spot
        if ref is not None:
            a = s.frame[:, :, :3].astype(np.int16)
            b = ref[:, :, :3].astype(np.int16)
            if a.shape != b.shape:
                s.stop()
                self.why = f"size mismatch {a.shape} vs {b.shape}"
                self._failed_regions.add(key)
                self.log(f"Fast screen reading disabled ({self.why})")
                return False
            diff = float(np.abs(a - b).mean())
            if diff > 40:
                s.stop()
                self.why = f"picture mismatch ({diff:.0f})"
                self._failed_regions.add(key)
                self.log(f"Fast screen reading disabled ({self.why})")
                return False
        self.stream, self.region, self.method = s, dict(region), "screencapturekit"
        return True

    def _inside(self, r):
        g = self.region
        return (g is not None and r["left"] >= g["left"] and r["top"] >= g["top"]
                and r["left"] + r["width"] <= g["left"] + g["width"]
                and r["top"] + r["height"] <= g["top"] + g["height"])

    def grab(self, r):
        s = self.stream
        if s is not None and s.alive and s.frame is not None and self._inside(r):
            g, sc = self.region, s.scale
            x0 = int(round((r["left"] - g["left"]) * sc))
            y0 = int(round((r["top"] - g["top"]) * sc))
            x1 = x0 + int(round(r["width"] * sc))
            y1 = y0 + int(round(r["height"] * sc))
            return _Raw(s.frame[y0:y1, x0:x1])
        if s is not None and not s.alive:
            self.log(f"Fast screen reading stopped ({s._dead}); using the slower method")
            self.stream, self.method = None, "mss"
        return self.base.grab(r)

    def wait_new(self, last_seq, timeout=0.03):
        """Block until the stream has a frame newer than `last_seq` (or timeout). Returns
        the current sequence number (0 when not streaming)."""
        s = self.stream
        if s is None or not s.alive:
            return 0
        with s.cond:
            s.cond.wait_for(lambda: s.seq != last_seq, timeout)
            return s.seq

    def frame_time(self):
        s = self.stream
        return s.frame_t if (s is not None and s.alive) else None
