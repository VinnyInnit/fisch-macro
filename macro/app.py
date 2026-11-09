#!/usr/bin/env python3
"""
Fisch Macro app: a small local server that shows the macro's window in your
browser (http://127.0.0.1:8765). Nothing leaves your computer.
Start it with the "Start Fisch Macro" launcher, or: python3 app.py
"""

import json
import os
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

import fisch_macro as fm
import fisch_overlay
import fisch_window
import fisch_platform as fp
import fisch_auto as fa

HOST = "127.0.0.1"
PORTS = range(8765, 8775)
HOST_APP = os.environ.get("FISCH_HOST_NAME") or "Terminal"      # what holds the Mac permissions
IN_APP = HOST_APP != "Terminal"
try:
    VERSION = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")).read().strip()
except OSError:
    VERSION = "dev"

# settings the window may change: name -> type
EDITABLE = {
    "shake_mode": str, "nav_key": str, "reel_input": str,
    "cast_hold_seconds": float, "after_cast_wait": float, "post_catch_delay": float,
    "start_delay": float, "shake_timeout": float, "monitor": int,
    "lookahead": float, "fish_lookahead": float, "brake": float, "deadzone": float,
    "track_tolerance": int, "bar_tolerance": int, "fish_tolerance": int,
    "scan_half_height": int,
    "rod": str, "overlay": bool, "auto": bool, "weather": str, "shake_timeout_auto": bool,
    "pinion_note_band": float, "input_latency": float,
}
CHOICES = {"weather": {"auto", "Clear", "Rain", "Windy", "Foggy", "Starfall", "Tornado", "Rainbow",
                       "Aurora Borealis", "Eclipse"},
           "shake_mode": {"auto", "navigation", "click", "wait"}, "reel_input": {"mouse", "space"},
           "rod": set(fm.fr.PROFILES)}


def to_bool(v):
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


class ShotSct:
    """Looks like an mss instance but reads from a stored screenshot, so Setup can run
    the real detection code on the picture the user clicked on."""

    def __init__(self, shot):
        self.shot = shot
        mon = dict(shot["mon"])
        self.monitors = [mon, mon] + [mon] * 8

    def grab(self, r):
        s, sc = self.shot, self.shot["scale"]
        x0 = int(round((r["left"] - s["mon"]["left"]) * sc))
        y0 = int(round((r["top"] - s["mon"]["top"]) * sc))
        img = s["img"][max(0, y0):y0 + int(round(r["height"] * sc)), max(0, x0):x0 + int(round(r["width"] * sc))]
        return _Raw(img)


class _Raw:
    def __init__(self, rgb):
        self.rgb_arr = rgb

    def __array__(self, dtype=None, copy=None):
        h, w, _ = self.rgb_arr.shape
        return np.concatenate(
            [self.rgb_arr[:, :, ::-1], np.full((h, w, 1), 255, np.uint8)], axis=2)


class App:
    def __init__(self, mss_factory=None):
        self.cfg = fm.load_config()
        self.logs = deque(maxlen=200)
        self.log_id = 0
        self.lock = threading.Lock()
        self.macro = fm.Macro(self.cfg, log=self.log)
        self.mss_factory = mss_factory
        self.shot = None          # dict: img (H,W,3), png bytes, mon, scale, id
        self.preview_det = None
        self.stop_event = threading.Event()
        self.window = None            # the app window, when running as the app
        self.hotkeys = None
        self._preview_mem = {}
        self._perm = (0, fp.permissions())
        ok, why = fisch_overlay.available()
        self.overlay_ok, self.overlay_why = ok, why

    # ---------------------------------------------------------------- utils
    def log(self, msg):
        with self.lock:
            self.log_id += 1
            self.logs.append({"id": self.log_id, "t": time.strftime("%H:%M:%S"), "msg": msg})

    def new_sct(self, fast=False):
        if self.mss_factory:
            return self.mss_factory()
        import mss
        base = mss.mss()
        return fp.FastSct(base) if fast and fp.IS_MAC else base

    def calibrated(self):
        if self.cfg.get("auto", True):
            return True
        return self.has_geometry() and self.cfg.get("click_pos") is not None

    def has_geometry(self):
        return all(self.cfg.get(k) is not None for k in ("track_left", "track_right", "track_y"))

    def start_background(self):
        if IN_APP and fp.IS_MAC and not all(self.perms().values()):
            threading.Thread(target=fp.request_all_prompts, daemon=True).start()
        self.macro.ensure_thread()
        self.hotkeys = self.macro.start_hotkeys()
        threading.Thread(target=self.preview_loop, daemon=True).start()
        self.log("App ready" + ("" if self.calibrated() else " - do the Setup steps first"))

    def preview_loop(self):
        """While the macro is idle, keep reading the reel area so the live view works."""
        try:
            sct = self.new_sct(fast=True)
        except Exception as e:
            self.log(f"Can't read the screen: {e}")
            return
        while not self.stop_event.is_set():
            try:
                if self.has_geometry() and self.macro.phase in ("idle", "starting"):
                    self.preview_det = fm.analyze(sct, self.cfg, self._preview_mem)
            except Exception:
                self.preview_det = None
            time.sleep(0.12)

    # --------------------------------------------------------------- state
    def perms(self):
        t, p = self._perm
        if time.time() - t > 2:
            self._perm = (time.time(), fp.permissions())
        return self._perm[1]

    def state(self, since=0):
        m = self.macro
        det = m.last_det if m.phase not in ("idle", "starting") else self.preview_det
        with self.lock:
            logs = [l for l in self.logs if l["id"] > since]
        return {
            "phase": m.phase,
            "running": m.running.is_set(),
            "catches": m.catches,
            "perfect": m.perfect,
            "perms": self.perms(),
            "auto": bool(self.cfg.get("auto", True)),
            "ident": jsonable_ident(m.ident),
            "window_found": m.window_found,
            "ocr": fa.ocr_available(),
            "has_geometry": self.has_geometry(),
            "capture": m.capture_info,
            "reel_fps": m.reel_fps,
            "overlay_ok": self.overlay_ok,
            "overlay_why": self.overlay_why,
            "calibrated": self.calibrated(),
            "det": jsonable_det(det),
            "logs": logs,
            "hotkeys": self.hotkeys is not None,
            "cfg": {k: self.cfg.get(k) for k in list(EDITABLE) + [
                "track_left", "track_right", "track_y", "click_pos",
                "track_color", "bar_color", "fish_color", "toggle_key", "track_half_height"]},
            "shot": {"id": self.shot["id"], "w": self.shot["img"].shape[1],
                     "h": self.shot["img"].shape[0]} if self.shot else None,
            "platform": sys.platform,
            "host": HOST_APP,
            "app_path": fp.running_app()[1],
            "version": VERSION,
            "update": os.environ.get("FISCH_UPDATE_NOTE", ""),
        }

    def set_config(self, changes):
        bad = []
        for k, v in changes.items():
            if k not in EDITABLE:
                bad.append(k)
                continue
            try:
                v = EDITABLE[k](v)
            except (TypeError, ValueError):
                bad.append(k)
                continue
            if k in CHOICES and v not in CHOICES[k]:
                bad.append(k)
                continue
            if EDITABLE[k] is bool:
                v = to_bool(changes[k])
            if k == "nav_key" and len(v) != 1:
                bad.append(k)
                continue
            self.cfg[k] = v
        fm.save_config(self.cfg)
        self.macro.apply_config()
        if "weather" in changes and "weather" not in bad and self.macro.ident:
            self.macro.identify_async()          # re-work out lure speed etc. for the new weather
        return bad

    # -------------------------------------------------------- calibration
    def take_screenshot(self, delay):
        time.sleep(max(0.0, min(10.0, delay)))
        import mss.tools
        with self.new_sct() as sct:
            mons = sct.monitors
            idx = self.cfg.get("monitor", 1)
            if idx >= len(mons):
                idx = 1
            mon = mons[idx]
            raw = sct.grab(mon)
        img = np.ascontiguousarray(np.asarray(raw)[:, :, 2::-1])
        png = mss.tools.to_png(raw.rgb, raw.size)
        sid = int(time.time() * 1000)
        self.shot = {"img": img, "png": png, "mon": dict(mon),
                     "scale": img.shape[1] / mon["width"], "id": sid}
        self.log("Screenshot taken - now click the points it asks for")
        return {"id": sid, "w": img.shape[1], "h": img.shape[0]}

    def to_screen(self, px, py):
        s = self.shot
        return (int(round(s["mon"]["left"] + px / s["scale"])),
                int(round(s["mon"]["top"] + py / s["scale"])))

    def img_color(self, px, py, r=1):
        img = self.shot["img"]
        h, w = img.shape[:2]
        x0, x1 = max(0, px - r), min(w, px + r + 1)
        y0, y1 = max(0, py - r), min(h, py + r + 1)
        return [int(v) for v in np.median(img[y0:y1, x0:x1].reshape(-1, 3), axis=0)]

    def shot_strip(self):
        """The reel scan strip, cut from the stored screenshot (image pixels)."""
        s, c = self.shot, self.cfg
        sc = s["scale"]
        l = int((c["track_left"] - s["mon"]["left"]) * sc)
        r = int((c["track_right"] - s["mon"]["left"]) * sc)
        y = int((c["track_y"] - s["mon"]["top"]) * sc)
        h = max(1, int(c["scan_half_height"] * sc))
        return s["img"][max(0, y - h):y + h + 1, max(0, l):r]

    def measure_track_half(self, x, y):
        """Half the track's height in image pixels, measured on the screenshot."""
        img = self.shot["img"]
        col = img[:, x].astype(int)
        ref = np.array(self.cfg["track_color"])
        tol = self.cfg["track_tolerance"] + 6
        ok = lambda r: 0 <= r < len(col) and np.abs(col[r] - ref).max() <= tol
        if not ok(y):
            return None
        top = bot = y
        while ok(top - 1) and y - top < 200:
            top -= 1
        while ok(bot + 1) and bot - y < 200:
            bot += 1
        return (bot - top + 1) / 2

    def overlay_state(self):
        c, m = self.cfg, self.macro
        st = {"enabled": bool(c.get("overlay")) and self.has_geometry(), "origin": (0, 0)}
        if not st["enabled"]:
            return st
        det = m.last_det if m.phase not in ("idle", "starting") else self.preview_det
        if not det or not det.get("visible"):
            if m.phase in ("idle",):
                st["enabled"] = False
                return st
        half = c.get("track_half_height") or c["scan_half_height"]
        st["geom"] = {"left": c["track_left"], "right": c["track_right"], "y": c["track_y"], "half": half}
        st["det"] = det if det and det.get("visible") else None
        ctl = m.ctl if m.phase == "reeling" else None
        if ctl is not None and det and det.get("visible"):
            st["target"] = ctl.target
            st["notes"] = [{"x": n["x"], "eta": n["dist"] / max(n["speed"], 0.05)} for n in ctl.notes]
        rodname = (m.ident or {}).get("rod") or {"auto": "Auto", "normal": "Normal rod", "remembrance": "Remembrance",
                                                "pinion": "Pinion's Aria"}.get(c.get("rod"), "Auto")
        st["text"] = f"{rodname} · {m.phase} · perfect {m.perfect}/{m.catches}"
        return st

    def auto_colors(self):
        """Guess bar and fish colours from the track strip in the screenshot."""
        strip = self.shot_strip()
        if strip.size == 0:
            return {}
        px = strip.reshape(-1, 3).astype(int)
        found = {}
        bright = px[px.min(axis=1) >= 170]
        if len(bright) >= 0.03 * len(px):
            found["bar_color"] = [int(v) for v in np.median(bright, axis=0)]
        track = np.array(self.cfg["track_color"])
        bar = np.array(found.get("bar_color", self.cfg["bar_color"]))
        other = px[(np.abs(px - track).max(axis=1) > self.cfg["track_tolerance"] + 8) &
                   (np.abs(px - bar).max(axis=1) > self.cfg["bar_tolerance"] + 8) &
                   (np.abs(px - [132, 132, 136]).max(axis=1) > 12)]   # the grey arrow
        if len(other):
            q = other // 8 * 8
            cols, counts = np.unique(q, axis=0, return_counts=True)
            if counts.max() >= 3:
                found["fish_color"] = [int(v) + 4 for v in cols[counts.argmax()]]
        return found

    def calibrate(self, step, points):
        if not self.shot:
            raise ValueError("Take a screenshot first")
        pts = [(int(p[0]), int(p[1])) for p in points]
        notes = []
        if step == "track":
            if len(pts) != 2:
                raise ValueError("Click the left end, then the right end")
            (lx, ly), (rx, ry) = sorted(pts)
            if rx - lx < 40:
                raise ValueError("Those two points are too close together")
            ty = (ly + ry) // 2
            sx = rx - max(4, (rx - lx) // 20)
            img_w = self.shot["img"].shape[1]
            rod = self.cfg.get("rod", "normal")
            if (rx - lx) < 0.22 * img_w:
                raise ValueError(
                    f"That's only {100 * (rx - lx) / img_w:.0f}% of the screen wide, but the reel track is usually "
                    "about 40%. Click the very LEFT end and the very RIGHT end of the whole long track "
                    "(not the ends of your bar).")
            track_col = self.img_color(sx, ty)
            if rod == "normal" and min(track_col) > 140:
                raise ValueError(
                    f"The track near your right-hand click is light {track_col}, but the standard minigame's "
                    "track is dark. Either that click landed on your white bar (let the bar rest on the LEFT "
                    "and click the far right end of the track), or this rod has a skin that changes the "
                    "minigame. Normal mode needs the standard look: unequip the skin or use another rod.")
            sl, sy = self.to_screen(lx, ty)
            sr, _ = self.to_screen(rx, ry)
            self.cfg.update(track_left=sl, track_right=sr, track_y=sy, auto=False)
            self.cfg["track_color"] = track_col
            half = self.measure_track_half(sx, ty)
            if half:
                self.cfg["track_half_height"] = round(half / self.shot["scale"], 1)
            rod = self.cfg.get("rod", "normal")
            found = self.auto_colors() if rod == "normal" else {}
            if rod == "remembrance":
                found = {k: v for k, v in self.auto_colors().items() if k == "bar_color"}
            self.cfg.update(found)
            notes.append("Track saved")
            if rod == "normal":
                for k, label in (("bar_color", "bar"), ("fish_color", "fish")):
                    notes.append(f"{label} colour {'found' if k in found else 'kept as default'}")
        elif step == "click":
            self.cfg["click_pos"] = list(self.to_screen(*pts[0]))
            notes.append("Casting spot saved")
        elif step == "bar":
            col = self.img_color(*pts[0])
            if min(col) < 140:
                raise ValueError(f"That spot is too dark to be the white bar {col}. "
                                 "Click right on the white bar.")
            self.cfg["bar_color"] = col
            notes.append(f"Bar colour set to {self.cfg['bar_color']}")
        elif step == "fish":
            # the fish line is thin: look a few pixels around the click for it
            x, y = pts[0]
            img = self.shot["img"]
            win = img[max(0, y - 3):y + 4, max(0, x - 8):x + 9].reshape(-1, 3).astype(int)
            bar, track = np.array(self.cfg["bar_color"]), np.array(self.cfg["track_color"])
            score = np.minimum(np.abs(win - bar).max(axis=1), np.abs(win - track).max(axis=1))
            self.cfg["fish_color"] = [int(v) for v in win[score.argmax()]]
            notes.append(f"Fish colour set to {self.cfg['fish_color']}")
        else:
            raise ValueError("Unknown step")
        fm.save_config(self.cfg)
        det = None
        if step != "click" and self.cfg.get("track_left") is not None:
            det = jsonable_det(fm.analyze(ShotSct(self.shot), self.cfg, {}))
        if step == "track" and det is not None and not det["visible"]:
            notes.append("WARNING: the reel isn't recognised in this screenshot - make sure the minigame was on "
                         "screen and redo the track step")
        self.log("; ".join(notes))
        return {"notes": notes, "det": det}

    def shutdown(self):
        self.stop_event.set()
        self.macro.shutdown()
        if self.hotkeys:
            self.hotkeys.stop()


def jsonable_ident(ident):
    if not ident:
        return None
    out = {k: ident.get(k) for k in ("rod", "enchant", "enchant2", "bait", "weather", "area", "mode", "notes", "error")}
    r = ident.get("rod_info")
    if r:
        out["control"], out["resilience"] = r["control"], r["resilience"]
        out["bar_width"], out["flags"] = r["bar_width"], r["flags"]
    e = ident.get("effective")
    if e:
        out["bar_width"], out["control"], out["lure"] = e["bar_width"], e["control"], e["lure"]
        out["adjust"] = e["parts"]
    out["raw"] = ident.get("raw")
    if out.get("enchant"):
        out["enchant_resilience"] = fa.DATA["enchant_resilience"].get(out["enchant"])
    return out


def jsonable_det(det):
    if not det:
        return None
    return {"visible": bool(det["visible"]),
            "bar": [float(det["bar"][0]), float(det["bar"][1])] if det["bar"] else None,
            "fish": float(det["fish"]) if det["fish"] is not None else None,
            "avoid": [float(a) for a in det.get("avoid") or []],
            "notes": [{"x": float(n["x"]), "dist": float(n["dist"])} for n in det.get("notes") or []],
            "track_frac": float(det["track_frac"])}


# ------------------------------------------------------------------ http

def page_html():
    quit_how = "the Quit button, or Cmd+Q in its menu" if IN_APP else "Cmd+Q on Terminal"
    open_how = HOST_APP if IN_APP else 'the "Start Fisch Macro" launcher'
    return PAGE.replace("__HOST__", HOST_APP).replace("__QUIT__", quit_how).replace("__OPEN__", open_how)


def make_handler(app, server_ref):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def do_GET(self):
            path, _, q = self.path.partition("?")
            if path == "/":
                return self.send(200, page_html(), "text/html; charset=utf-8")
            if path == "/api/state":
                since = int((q.split("since=")[1].split("&")[0]) if "since=" in q else 0)
                return self.send(200, app.state(since))
            if path == "/favicon.ico":
                return self.send(200, FAVICON, "image/svg+xml")
            if path == "/api/screenshot.png":
                if not app.shot:
                    return self.send(404, {"error": "no screenshot"})
                return self.send(200, app.shot["png"], "image/png")
            self.send(404, {"error": "not found"})

        def do_POST(self):
            # only accept requests from our own page
            origin = self.headers.get("Origin")
            if origin and not origin.startswith(f"http://{HOST}:"):
                return self.send(403, {"error": "forbidden"})
            try:
                data = self.body()
                if self.path == "/api/start":
                    if not app.calibrated():
                        return self.send(400, {"error": "Finish the Setup steps first"})
                    if app.perms().get("accessibility") is False:
                        return self.send(400, {"error": "macOS is blocking the macro's mouse and keyboard, so the bar "
                                                        "can't move. Turn on " + HOST_APP + " under System Settings > Privacy & "
                                                        "Security > Accessibility, then quit " + HOST_APP + " (Cmd+Q) and open "
                                                        "the launcher again."})
                    app.macro.start()
                    return self.send(200, {"ok": True})
                if self.path == "/api/focus":
                    shown = fisch_window.focus(app)
                    if not shown:
                        webbrowser.open(f"http://{HOST}:{self.server.server_address[1]}/")
                    return self.send(200, {"ok": True})
                if self.path == "/api/stop":
                    app.macro.stop()
                    return self.send(200, {"ok": True})
                if self.path == "/api/config":
                    return self.send(200, {"bad": app.set_config(data)})
                if self.path == "/api/screenshot":
                    return self.send(200, app.take_screenshot(float(data.get("delay", 4))))
                if self.path == "/api/calibrate":
                    return self.send(200, app.calibrate(data.get("step"), data.get("points", [])))
                if self.path == "/api/identify":
                    # one scan at a time: extra clicks while one is running are ignored
                    started = app.macro.identify_async()
                    return self.send(200, {"ok": True, "busy": not started})
                if self.path == "/api/fix_permissions":
                    ok, msg = fp.reset_permissions()
                    app.log("Permissions: " + msg + (" - switch " + HOST_APP + " on in the lists, then quit and reopen"
                                                     if ok else ""))
                    if ok:
                        fp.open_settings("accessibility")
                    return self.send(200, {"ok": ok, "msg": msg})
                if self.path == "/api/open_settings":
                    which = data.get("which")
                    if which == "accessibility":
                        fp.request_accessibility_prompt()
                    return self.send(200, {"ok": fp.open_settings(which)})
                if self.path == "/api/quit":
                    self.send(200, {"ok": True})
                    app.log("Closing")
                    threading.Thread(target=lambda: (app.shutdown(), server_ref[0].shutdown()),
                                     daemon=True).start()
                    return
                self.send(404, {"error": "not found"})
            except ValueError as e:
                self.send(400, {"error": str(e)})
            except Exception as e:
                self.send(500, {"error": repr(e)})
    return H


def serve(app, open_browser=True):
    server_ref = [None]
    handler = make_handler(app, server_ref)
    server = None
    for port in PORTS:
        try:
            server = ThreadingHTTPServer((HOST, port), handler)
            break
        except OSError:
            continue
    if server is None:
        sys.exit("No free port between 8765 and 8774")
    server_ref[0] = server
    url = f"http://{HOST}:{server.server_address[1]}/"
    print(f"Fisch Macro is running at {url}")
    print("Keep this window open while you use the macro. Close it to quit.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    return server, url


def main():
    app = App()
    app.start_background()
    use_window = IN_APP and "--browser" not in sys.argv and fisch_window.available()
    server, url = serve(app, open_browser=not use_window and "--no-browser" not in sys.argv)
    use_overlay = app.overlay_ok and "--no-overlay" not in sys.argv
    try:
        if use_window:
            # the app's own window owns the main thread; the web server runs alongside it
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            if fisch_window.run(url, app, overlay=use_overlay, log=app.log):
                return                       # window closed -> quit (finally: below)
            webbrowser.open(url)             # couldn't show the window: fall back to the browser
            if use_overlay:
                try:
                    fisch_overlay.run(app.overlay_state, app.stop_event)
                except Exception as e:
                    app.log(f"Overlay unavailable: {e!r}")
            while t.is_alive() and not app.stop_event.is_set():
                time.sleep(0.5)
        elif use_overlay:
            import signal

            def _stop(*_):
                threading.Thread(target=app.shutdown, daemon=True).start()
            signal.signal(signal.SIGINT, _stop)
            # GUI toolkits want the main thread, so the web server moves to a helper thread
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            try:
                fisch_overlay.run(app.overlay_state, app.stop_event)
            except Exception as e:
                app.overlay_ok, app.overlay_why = False, f"overlay failed to start: {e!r}"
                app.log(f"Overlay unavailable: {e!r}")
                while t.is_alive() and not app.stop_event.is_set():
                    time.sleep(0.5)
        else:
            if not app.overlay_ok:
                app.log(f"Overlay off: {app.overlay_why}")
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        try:
            server.shutdown()
        except Exception:
            pass
        print("Fisch Macro closed.")


# ------------------------------------------------------------------- page

FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
           '<text y=".9em" font-size="90">🎣</text></svg>')

PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fischeroo</title>
<style>
:root{--bg:#0d1b24;--panel:#13262f;--line:#22404c;--ink:#e8f1f2;--dim:#8fa9b0;--accent:#3ec7b5;--warn:#f2b84b;--bad:#ef6b5b;--good:#5fd38d}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:22px 18px 60px}
header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:18px}
h1{font-size:22px;margin:0;letter-spacing:.2px}
h2{font-size:15px;margin:0 0 10px;color:var(--dim);font-weight:600;text-transform:uppercase;letter-spacing:.8px}
.pill{padding:4px 12px;border-radius:99px;font-weight:600;font-size:13px;background:#23414c;color:var(--dim)}
.pill.casting,.pill.shaking,.pill.waiting{background:#1d4a5a;color:#9fe3ff}
.pill.reeling{background:#174a3c;color:var(--good)}
.pill.starting{background:#4a3c17;color:var(--warn)}
.count{margin-left:auto;color:var(--dim)} .count b{color:var(--ink);font-size:18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
.card.warn{border-color:var(--warn)}
.perm{display:flex;align-items:center;gap:12px;margin:8px 0}
.perm b{min-width:150px}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
button{font:inherit;border:0;border-radius:9px;padding:10px 18px;cursor:pointer;background:#23414c;color:var(--ink);font-weight:600}
button:hover{filter:brightness(1.15)} button:disabled{opacity:.45;cursor:default;filter:none}
button.go{background:var(--accent);color:#06221e;font-size:17px;padding:12px 30px}
button.stop{background:var(--bad);color:#2a0905;font-size:17px;padding:12px 30px}
button.small{padding:6px 12px;font-size:13px}
.hint{color:var(--dim);font-size:13px}
.err{color:var(--bad);font-weight:600}
#live{width:100%;height:56px;display:block;border-radius:8px;background:#0a141a}
.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:12px 0}
.step{border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:14px}
.step.done{border-color:#2b6b54} .step.done b::before{content:"✓ ";color:var(--good)}
.step.active{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}
#shotwrap{position:relative;margin-top:10px;display:none}
#shot{width:100%;display:block;border-radius:8px;cursor:crosshair}
#loupe{position:fixed;width:150px;height:150px;border-radius:50%;border:2px solid var(--accent);pointer-events:none;display:none;box-shadow:0 4px 18px #0008;image-rendering:pixelated}
.mark{position:absolute;width:14px;height:14px;margin:-7px 0 0 -7px;border:2px solid var(--warn);border-radius:50%;pointer-events:none}
#banner{background:#0a141a;border:1px solid var(--accent);border-radius:9px;padding:9px 12px;margin-top:10px;font-weight:600;display:none}
label{display:flex;flex-direction:column;gap:4px;font-size:13px;color:var(--dim)}
select,input{font:inherit;background:#0a141a;color:var(--ink);border:1px solid var(--line);border-radius:7px;padding:7px 9px;min-width:0}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
details summary{cursor:pointer;color:var(--dim);margin:12px 0 8px}
ul.check{margin:0;padding-left:18px} ul.check li{margin:4px 0}
#log{height:150px;overflow:auto;font:12.5px/1.5 ui-monospace,Menlo,Consolas,monospace;color:var(--dim);background:#0a141a;border-radius:8px;padding:8px 10px}
#log b{color:var(--ink);font-weight:500}
@media (max-width:720px){.steps,.grid{grid-template-columns:1fr 1fr}}
</style></head><body><main>

<header>
  <h1>🎣 Fischeroo</h1>
  <span id="phase" class="pill">idle</span>
  <span class="count">Perfect: <b id="perfect">0</b> / <b id="catches">0</b> reels</span>
  <span class="hint" id="ver" style="margin-left:auto"></span>
  <button class="small" id="quitTop" style="display:none">Quit</button>
</header>
<section class="card" id="updCard" style="display:none"><p id="updText" style="margin:0"></p></section>

<section class="card warn" id="permCard" style="display:none">
  <h2 style="color:var(--warn)">Your Mac is blocking the macro</h2>
  <div id="permList"></div>
  <p class="hint" style="margin:10px 0 0">After switching __HOST__ on, quit it completely (__QUIT__) and open
  __OPEN__ again. macOS only applies these permissions to a freshly started app.</p>
  <div id="permFix" style="display:none;margin-top:10px">
    <p class="hint" style="margin:0 0 6px">Already switched on but still blocked? The switches probably belong to an
    older copy of the app: macOS ties them to the exact download. <span id="appPath"></span></p>
    <button class="small" id="fixPermBtn">Fix permissions (re-add this copy)</button>
  </div>
</section>

<section class="card">
  <div class="row">
    <button id="startBtn" class="go">Start</button>
    <button id="stopBtn" class="stop" disabled>Stop</button>
    <span class="hint" id="startHint"></span>
  </div>
  <p class="hint" style="margin:10px 0 0">After pressing Start you have a few seconds to click into Roblox (rod equipped).
  To pause from inside the game: press <b id="hk">F6</b> or move the mouse into the <b>top-left corner</b> of the screen.</p>
</section>

<section class="card">
  <h2>What the macro sees</h2>
  <canvas id="live" width="900" height="56"></canvas>
  <p class="hint" id="liveHint" style="margin:8px 0 0">Finish Setup to see the reel bar here.</p>
  <p class="hint" id="speedHint" style="margin:4px 0 0"></p>
</section>

<section class="card">
  <div class="row" style="justify-content:space-between"><h2 style="margin:0">Detected</h2>
    <div class="row"><label style="flex-direction:row;align-items:center;gap:8px"><input type="checkbox" data-k="auto" id="autoChk"> Automatic (no setup)</label>
    <button class="small" id="rescanBtn">Re-scan rod</button></div></div>
  <div class="grid" style="margin-top:12px">
    <div><div class="hint">Rod</div><b id="dRod">-</b></div>
    <div><div class="hint">Enchants</div><b id="dEnch">-</b></div>
    <div><div class="hint">Bait</div><b id="dBait">-</b></div>
    <div><div class="hint">Weather</div><b id="dWeather">-</b></div>
    <div><div class="hint">Area</div><b id="dArea">-</b></div>
    <div><div class="hint">Lure speed / Bar width</div><b id="dStats">-</b></div>
  </div>
  <p class="hint" id="dNotes" style="margin:10px 0 0"></p>
</section>

<section class="card" id="manualRod">
  <h2>Your rod</h2>
  <div class="row">
    <label style="min-width:260px">Rod type<select data-k="rod" id="rodSel">
      <option value="auto">Automatic (learns the minigame's look)</option>
      <option value="normal">Normal rod (standard minigame colours)</option>
      <option value="pinion">Pinion's Aria (falling notes)</option>
      <option value="remembrance">Remembrance (white + dark fish)</option></select></label>
    <label style="flex-direction:row;align-items:center;gap:8px;margin-top:18px"><input type="checkbox" data-k="overlay" id="ovl"> Show overlay over Roblox</label>
  </div>
  <p class="hint" id="rodHint" style="margin:10px 0 0"></p>
  <p class="hint err" id="ovlWhy" style="margin:6px 0 0"></p>
</section>

<details id="setupWrap"><summary style="margin:4px 0 12px">Manual setup (only if automatic mode can't find the reel)</summary>
<section class="card">
  <h2>Setup</h2>
  <p class="hint" style="margin:0">Get a reel minigame on screen in Fisch (cast, wait for a bite, don't click), then take a screenshot.
  You'll click on it, so the timing doesn't matter after that.</p>
  <div class="steps">
    <div class="step" id="st-track"><b>1. Reel track</b><br><span class="hint">Click its left end, then its right end</span></div>
    <div class="step" id="st-click"><b>2. Casting spot</b><br><span class="hint">Click on open water</span></div>
    <div class="step" id="st-check"><b>3. Check</b><br><span class="hint">Bar and fish found in the screenshot</span></div>
  </div>
  <div class="row">
    <button id="shotBtn">📸 Take screenshot in <span id="delayLbl">5</span>s</button>
    <button class="small" id="redoTrack" disabled>Redo track</button>
    <button class="small" id="redoClick" disabled>Redo casting spot</button>
    <button class="small" id="fixBar" disabled>Fix bar colour</button>
    <button class="small" id="fixFish" disabled>Fix fish colour</button>
  </div>
  <div id="banner"></div>
  <div id="shotwrap"><img id="shot" alt="screenshot"></div>
</section>
</details>

<section class="card">
  <h2>Settings</h2>
  <div class="grid">
    <label>Shake method<select data-k="shake_mode">
      <option value="auto">Click the Shake button only when it shows (recommended)</option>
      <option value="navigation">Navigation key + Enter (only while a Shake button shows)</option>
      <option value="wait">Don't shake (just wait)</option>
      <option value="click">Click the Shake button (by colour, old)</option></select></label>
    <label>Navigation key<input data-k="nav_key" maxlength="1"></label>
    <label>Reel with<select data-k="reel_input">
      <option value="mouse">Mouse button</option><option value="space">Spacebar</option></select></label>
    <label>Start delay (s)<input data-k="start_delay" type="number" step="1" min="0"></label>
    <label>Cast hold (s)<input data-k="cast_hold_seconds" type="number" step="0.05" min="0.1"></label>
    <label>Wait after a catch (s)<input data-k="post_catch_delay" type="number" step="0.1" min="0"></label>
    <label>Cast again if no reel after (s)<input data-k="shake_timeout" type="number" step="1" min="5"></label>
    <label>Weather<select data-k="weather">
      <option value="auto">Read from screen</option><option>Clear</option><option>Rain</option><option>Windy</option>
      <option>Foggy</option><option>Starfall</option><option>Tornado</option><option>Rainbow</option>
      <option>Aurora Borealis</option><option>Eclipse</option></select></label>
    <label style="flex-direction:row;align-items:center;gap:8px;margin-top:18px"><input type="checkbox" data-k="shake_timeout_auto"> Wait longer for slow-lure rods</label>
    <label>Screen (1 = main)<input data-k="monitor" type="number" step="1" min="1"></label>
  </div>
  <details><summary>Advanced reel tuning</summary>
    <div class="grid">
      <label>Bar lookahead<input data-k="lookahead" type="number" step="0.01"></label>
      <label>Fish lookahead<input data-k="fish_lookahead" type="number" step="0.01"></label>
      <label>Brake<input data-k="brake" type="number" step="0.05"></label>
      <label>Deadzone<input data-k="deadzone" type="number" step="0.002"></label>
      <label>Track tolerance<input data-k="track_tolerance" type="number"></label>
      <label>Bar tolerance<input data-k="bar_tolerance" type="number"></label>
      <label>Fish tolerance<input data-k="fish_tolerance" type="number"></label>
      <label>Scan height (px)<input data-k="scan_half_height" type="number"></label>
      <label>Input lag (s)<input data-k="input_latency" type="number" step="0.01"></label>
      <label>Note look-up height<input data-k="pinion_note_band" type="number" step="0.05"></label>
    </div></details>
  <div class="row" style="margin-top:8px"><button id="saveBtn">Save settings</button><span class="hint" id="saved"></span></div>
</section>

<section class="card">
  <h2>Before your first run</h2>
  <ul class="check" id="checklist">
    <li>Roblox settings: turn <b>UI Navigation Toggle</b> on. Its key must match "Navigation key" above (usually <code>\</code>).</li>
    <li>Fisch settings: turn off <b>Higher Brightness</b>, <b>Higher Saturation</b> and <b>Wind Shake</b>.</li>
    <li>Using "Click the Shake button"? Turn on <b>Allow Shake Button Misclicking</b> in Fisch, or a missed click cancels the cast.</li>
    <li class="mac">Mac: System Settings → Privacy &amp; Security → allow <b>__HOST__</b> under <b>Screen Recording</b>, <b>Accessibility</b> and <b>Input Monitoring</b>, then reopen the app. If the screenshot shows only your wallpaper, Screen Recording is still off.</li>
    <li>Rod skins with a custom minigame look need the Setup redone.</li>
  </ul>
</section>

<section class="card">
  <div class="row" style="justify-content:space-between"><h2 style="margin:0">Log</h2>
  <button class="small" id="quitBtn">Quit app</button></div>
  <div id="log" style="margin-top:10px"></div>
</section>
</main>
<canvas id="loupe" width="150" height="150"></canvas>

<script>
const $ = s => document.querySelector(s);
let S = null, lastLog = 0, mode = null, picks = [], formDirty = false;
const SHOT_DELAY = 5;
$('#delayLbl').textContent = SHOT_DELAY;

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

function banner(text, isErr) {
  const b = $('#banner'); b.style.display = text ? 'block' : 'none';
  b.textContent = text || ''; b.className = isErr ? 'err' : '';
}

function addLogs(logs) {
  const el = $('#log');
  for (const l of logs) {
    const d = document.createElement('div');
    d.innerHTML = `${l.t} <b></b>`; d.querySelector('b').textContent = l.msg;
    el.appendChild(d); lastLog = Math.max(lastLog, l.id);
  }
  if (logs.length) el.scrollTop = el.scrollHeight;
}

function drawLive(det) {
  const c = $('#live'), g = c.getContext('2d'), W = c.width, H = c.height;
  g.clearRect(0, 0, W, H);
  g.fillStyle = '#1b2a31'; g.fillRect(10, 14, W - 20, H - 28);
  if (!det) return;
  const x = f => 10 + f * (W - 20);
  if (det.bar) { g.fillStyle = '#ecefee'; g.fillRect(x(det.bar[0]), 14, x(det.bar[1]) - x(det.bar[0]), H - 28); }
  if (det.fish !== null) { g.fillStyle = '#3ec7b5'; g.fillRect(x(det.fish) - 3, 4, 6, H - 8); }
  for (const a of det.avoid || []) { g.fillStyle = '#ef6b5b'; g.fillRect(x(a) - 3, 4, 6, H - 8); }
  for (const n of det.notes || []) { g.fillStyle = '#c3a8ff'; g.beginPath(); const nx = x(n.x), ny = Math.max(6, 14 - n.dist * 40);
    g.moveTo(nx, ny - 6); g.lineTo(nx + 6, ny); g.lineTo(nx, ny + 6); g.lineTo(nx - 6, ny); g.fill(); }
}

function render() {
  const s = S;
  $('#ver').textContent = s.version ? 'v' + s.version : '';
  $('#permFix').style.display = s.app_path ? '' : 'none';
  $('#appPath').textContent = s.app_path ? 'Running copy: ' + s.app_path : '';
  $('#updCard').style.display = s.update ? '' : 'none';
  $('#updText').textContent = s.update || '';
  $('#quitTop').style.display = (s.host && s.host !== 'Terminal') ? '' : 'none';
  const p = $('#phase'); p.textContent = s.phase; p.className = 'pill ' + s.phase;
  $('#catches').textContent = s.catches; $('#perfect').textContent = s.perfect;
  const active = s.running || s.phase === 'starting';
  $('#startBtn').disabled = active || !s.calibrated;
  $('#stopBtn').disabled = !active;
  $('#startHint').textContent = s.calibrated ? '' : 'Turn on Automatic, or do the manual Setup below.';
  const I = s.ident || {};
  $('#dRod').textContent = I.rod || (I.error ? 'n/a' : (s.auto ? 'read when you press Start' : '-'));
  $('#dEnch').textContent = I.enchant ? I.enchant + (I.enchant2 ? ' + ' + I.enchant2 : '')
    : (I.raw && I.raw.above_rod && I.raw.above_rod.length ? I.raw.above_rod[0] + ' (skin?)' : '-');
  $('#dBait').textContent = I.bait || '-';
  $('#dWeather').textContent = I.weather ? I.weather + ((s.cfg.weather && s.cfg.weather !== 'auto') ? ' (set by you)' : '')
    : (I.rod ? 'not shown on screen - set it in Settings' : '-');
  $('#dArea').textContent = I.area || '-';
  $('#dStats').textContent = I.bar_width ? (I.lure !== null && I.lure !== undefined ? `${Math.round(I.lure)}%` : '?') + ` / ${Math.round(I.bar_width * 100)}%` : '-';
  if (I.adjust && I.adjust.length) $('#dStats').title = 'Includes ' + I.adjust.join('; ');
  const dn = [];
  if (I.error) dn.push(I.error + ' - the macro still finds the reel by itself.');
  if (!s.window_found && s.phase !== 'idle') dn.push('Roblox window not found - searching the whole main screen.');
  (I.notes || []).forEach(n => dn.push(n));
  if (s.auto && !s.has_geometry) dn.push('The reel position is found automatically at your first bite.');
  $('#dNotes').textContent = dn.join(' ');
  $('#manualRod').style.display = s.auto ? 'none' : '';
  $('#hk').textContent = (s.cfg.toggle_key || 'f6').toUpperCase() + (s.platform === 'darwin' ? ' (fn+' + (s.cfg.toggle_key||'f6').toUpperCase() + ' on a MacBook)' : '');
  drawLive(s.det);
  $('#liveHint').textContent = !s.calibrated ? 'Finish Setup to see the reel bar here.'
    : s.det && s.det.visible ? 'Reel minigame detected.' + (s.det.fish === null ? ' Fish not found: try "Fix fish colour".' : '')
        + ((s.det.avoid||[]).length ? ' Dark fish (red) found.' : '') + ((s.det.notes||[]).length ? ` ${s.det.notes.length} note(s) falling.` : '')
    : 'No reel minigame on screen right now.';
  $('#st-track').classList.toggle('done', s.cfg.track_left !== null);
  $('#st-click').classList.toggle('done', s.cfg.click_pos !== null);
  if (s.calibrated && s.det && s.det.visible && s.det.fish !== null) $('#st-check').classList.add('done');
  const hasShot = !!s.shot;
  for (const id of ['#redoTrack', '#redoClick', '#fixBar', '#fixFish']) $(id).disabled = !hasShot;
  $('#fixBar').disabled = $('#fixFish').disabled = !hasShot || s.cfg.track_left === null;
  if (!formDirty) for (const el of document.querySelectorAll('[data-k]')) {
    const v = s.cfg[el.dataset.k]; if (document.activeElement === el || v === undefined || v === null) continue;
    if (el.type === 'checkbox') el.checked = !!v; else el.value = v;
  }
  document.querySelectorAll('.mac').forEach(li => li.style.display = s.platform === 'darwin' ? '' : 'none');
  const P = s.perms || {}, why = {
    accessibility: ['Accessibility', 'needed to press the mouse and keys. Without it the bar never moves.'],
    screen: ['Screen Recording', 'needed to see the minigame.'],
    input: ['Input Monitoring', 'needed for the F6 pause key (optional).']};
  const missing = Object.keys(why).filter(k => P[k] === false);
  $('#permCard').style.display = missing.length ? '' : 'none';
  const key = missing.join(',');
  if ($('#permList').dataset.k !== key) {
    $('#permList').dataset.k = key;
    $('#permList').innerHTML = missing.map(k => `<div class="perm"><b>${why[k][0]}</b><span class="hint" style="flex:1">${s.host || 'Terminal'} is off: ${why[k][1]}</span><button class="small" data-open="${k}">Open settings</button></div>`).join('');
    $('#permList').querySelectorAll('[data-open]').forEach(b => b.onclick = () => api('/api/open_settings', {which: b.dataset.open}));
  }
  const cap = s.capture || {};
  const meth = cap.method === 'screencapturekit' ? 'fast stream' : (cap.method === 'display' ? 'direct' : 'standard (slow)');
  $('#speedHint').textContent = (s.reel_fps ? `Last reel: screen read ${s.reel_fps} times/second` + (s.reel_fps < 25 ? ' (too slow to steer well)' : '') + ' · ' : '')
    + (cap.method ? `Screen reading: ${meth}` + (cap.why && cap.method !== 'screencapturekit' ? ` (${cap.why})` : '') : '');
  const hints = {
    normal: 'Keeps the fish inside your bar the whole reel. Works for any rod with the standard minigame; rods whose skin changes the minigame colours just need Setup redone.',
    pinion: "Catches every falling note (that's what counts as a perfect catch for Pinion's Aria) and stays on the fish in between. Do Setup with a screenshot of a Pinion's Aria reel.",
    remembrance: 'Keeps the white fish in your bar and the dark fish out of it. Do Setup with a screenshot of a Remembrance reel.'};
  $('#rodHint').textContent = hints[s.cfg.rod] || '';
  $('#ovlWhy').textContent = s.overlay_ok ? '' : 'Overlay unavailable: ' + (s.overlay_why || '');
  $('#fixBar').style.display = $('#fixFish').style.display = s.cfg.rod === 'normal' ? '' : 'none';
}

async function poll() {
  try { S = await api('/api/state?since=' + lastLog); addLogs(S.logs); render(); }
  catch (e) { $('#phase').textContent = 'app closed'; }
  setTimeout(poll, S && (S.running) ? 150 : 300);
}

// ---------- calibration
const PROMPTS = {
  track: ['Click the LEFT end of the dark reel track', 'Now click the RIGHT end of the track'],
  click: ['Click on open water (where the macro will hold the mouse to cast)'],
  bar:   ['Click on your white bar'],
  fish:  ['Click on the fish line (use the magnifier)'],
};
function setMode(m) {
  mode = m; picks = [];
  document.querySelectorAll('.mark').forEach(e => e.remove());
  ['track', 'click'].forEach(k => $('#st-' + k).classList.toggle('active', k === m));
  banner(m ? PROMPTS[m][0] : '');
}
function nextMode() {
  if (S.cfg.track_left === null) return setMode('track');
  if (S.cfg.click_pos === null) return setMode('click');
  setMode(null);
}

$('#shotBtn').onclick = async () => {
  const b = $('#shotBtn'); b.disabled = true;
  let n = SHOT_DELAY;
  const tick = setInterval(() => { n--; if (n > 0) b.innerHTML = `📸 Switch to Roblox... ${n}`; }, 1000);
  b.innerHTML = `📸 Switch to Roblox... ${n}`;
  try {
    const r = await api('/api/screenshot', {delay: SHOT_DELAY});
    $('#shot').src = '/api/screenshot.png?id=' + r.id;
    $('#shotwrap').style.display = 'block';
    S = await api('/api/state?since=' + lastLog); addLogs(S.logs); render();
    window.focus();
    if (S.cfg.track_left === null) setMode('track');
    else if (S.cfg.click_pos === null) setMode('click');
    else banner('Screenshot ready. Use the buttons above to redo or fix any step.');
    requestAnimationFrame(() => $('#banner').scrollIntoView({behavior: 'smooth', block: 'start'}));
  } catch (e) { banner('Screenshot failed: ' + e.message, true); }
  clearInterval(tick); b.disabled = false; b.innerHTML = `📸 Take screenshot in <span id="delayLbl">${SHOT_DELAY}</span>s`;
};
$('#redoTrack').onclick = () => setMode('track');
$('#redoClick').onclick = () => setMode('click');
$('#fixBar').onclick = () => setMode('bar');
$('#fixFish').onclick = () => setMode('fish');

function imgPoint(ev) {
  const img = $('#shot'), r = img.getBoundingClientRect();
  return [Math.round((ev.clientX - r.left) * img.naturalWidth / r.width),
          Math.round((ev.clientY - r.top) * img.naturalHeight / r.height)];
}
$('#shot').addEventListener('click', async ev => {
  if (!mode) return banner('Pick a step first (e.g. "Redo track").');
  const pt = imgPoint(ev); picks.push(pt);
  const m = document.createElement('div'); m.className = 'mark';
  const img = $('#shot'); m.style.left = (pt[0] * img.clientWidth / img.naturalWidth) + 'px';
  m.style.top = (pt[1] * img.clientHeight / img.naturalHeight) + 'px'; $('#shotwrap').appendChild(m);
  const need = mode === 'track' ? 2 : 1;
  if (picks.length < need) return banner(PROMPTS[mode][picks.length]);
  const step = mode;
  try {
    const r = await api('/api/calibrate', {step, points: picks});
    S = await api('/api/state?since=' + lastLog); addLogs(S.logs); render();
    let msg = r.notes.join(' · ');
    if (r.det) msg += r.det.visible ? (r.det.fish !== null ? ' · Bar and fish found ✓' : ' · Bar found, fish not found: try "Fix fish colour"')
                                   : ' · Reel not recognised in this screenshot: was the minigame on screen?';
    setMode(null); nextMode(); if (!mode) banner(msg); else banner(msg + ' → ' + PROMPTS[mode][0]);
    if (r.det) $('#st-check').classList.toggle('done', !!(r.det.visible && r.det.fish !== null));
  } catch (e) { setMode(step); banner(e.message, true); }
});

// magnifier for picking thin things like the fish line
const loupe = $('#loupe'), lg = loupe.getContext('2d');
$('#shot').addEventListener('mousemove', ev => {
  const img = $('#shot'); if (!img.naturalWidth) return;
  const [px, py] = imgPoint(ev), Z = 6, R = 150 / Z;
  lg.imageSmoothingEnabled = false; lg.clearRect(0, 0, 150, 150);
  lg.drawImage(img, px - R / 2, py - R / 2, R, R, 0, 0, 150, 150);
  lg.strokeStyle = '#f2b84b'; lg.strokeRect(72, 72, 6, 6);
  loupe.style.display = 'block'; loupe.style.left = (ev.clientX + 24) + 'px'; loupe.style.top = (ev.clientY - 170) + 'px';
});
$('#shot').addEventListener('mouseleave', () => loupe.style.display = 'none');

// ---------- controls & settings
$('#startBtn').onclick = () => api('/api/start', {}).catch(e => alert(e.message));
$('#stopBtn').onclick = () => api('/api/stop', {});
document.querySelectorAll('[data-k]').forEach(el => el.addEventListener('input', () => { formDirty = true; $('#saved').textContent = 'Unsaved changes'; }));
$('#rodSel').addEventListener('change', async e => { await api('/api/config', {rod: e.target.value}); formDirty = false; banner(e.target.value === 'normal' ? '' : 'Rod changed: take a new screenshot during this rod\'s reel and redo the track step.'); });
$('#ovl').addEventListener('change', async e => { await api('/api/config', {overlay: e.target.checked}); formDirty = false; });
$('#autoChk').addEventListener('change', async e => { await api('/api/config', {auto: e.target.checked, rod: e.target.checked ? 'auto' : 'normal'}); formDirty = false; });
$('#rescanBtn').onclick = () => api('/api/identify', {});
$('#saveBtn').onclick = async () => {
  const changes = {};
  document.querySelectorAll('[data-k]').forEach(el => changes[el.dataset.k] = el.type === 'checkbox' ? el.checked : el.value);
  const r = await api('/api/config', changes);
  formDirty = false;
  $('#saved').textContent = r.bad.length ? 'Not saved (invalid): ' + r.bad.join(', ') : 'Saved ✓';
};
$('#quitBtn').onclick = async () => {
  const inApp = S && S.host && S.host !== 'Terminal';      // the app window may not show browser dialogs
  if (!inApp && !confirm('Quit Fischeroo?')) return;
  await api('/api/quit', {}).catch(() => {});
  document.body.innerHTML = '<main><h1>Fischeroo closed</h1><p class="hint">You can close this tab.</p></main>';
};
$('#quitTop').onclick = () => { api('/api/quit', {}).then(() => {
  document.body.innerHTML = '<main><h1>Fischeroo closed</h1><p class="hint">You can close this tab.</p></main>'; }); };
$('#fixPermBtn').onclick = () => api('/api/fix_permissions', {});
poll();
</script></body></html>
"""

if __name__ == "__main__":
    main()
