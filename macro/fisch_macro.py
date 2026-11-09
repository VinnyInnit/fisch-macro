#!/usr/bin/env python3
"""
Fisch pixel macro: plays Fisch (Roblox) by reading screen pixels and sending
mouse/keyboard input. It never touches the game's memory.

SETUP
  pip3 install mss numpy pynput

  macOS: allow your Terminal (or IDE) in System Settings -> Privacy & Security
         under Screen Recording, Accessibility and Input Monitoring, then
         restart the Terminal.

  Shaking (shake_mode "auto", the default on Mac): the macro reads the screen for the
  Shake button and clicks it only while one is showing - nothing is pressed otherwise.
  In Fisch, turn off Higher Brightness / Higher Saturation and Wind Shake.
  Tip: set "reel_input": "space" to reel with Spacebar instead of the mouse,
  so a stray click can never hit a menu button mid-reel.

IMPORTANT FISCH SETTING
  By default, clicking anywhere except the SHAKE button cancels fishing.
  If you use shake_mode "click", turn ON "Allow Shake Button Misclicking".
  Shaking is optional (it only speeds up the bite), so shake_mode "wait" is
  the safest fallback if shaking gives you trouble.

HOW THE REEL WORKS (and why rods matter)
  Control = how wide your white bar is; Resilience = how slowly the fish moves.
  The macro measures your bar width live, so any rod works, but low-Control,
  low-Resilience rods on rare fish are hard even for a human. Your catch streak
  resets if a fish escapes.
  Progress moves 12%/s in or out of the bar; inputs are locked for the first
  1.2s of the minigame, so a catch takes at least ~8s.

ROD QUIRKS
  - Skins with a Custom UI recolour the minigame: recalibrate (track/bar/fish).
  - Rods with two fish (D.R.E.D., Remembrance) are not supported; the macro
    follows one fish only.
  - If the minigame shakes (Breeze Caster, Wrath of Olympus areas), raise
    "scan_half_height" to about 8 so the scan line stays on the track.

COMMANDS
  python3 fisch_macro.py calibrate        full calibration (do this first)
  python3 fisch_macro.py calibrate track  minimum setup if default colours work
                                          (then also: calibrate click)
  python3 fisch_macro.py calibrate bar    redo one step: click|track|bar|fish|shake
  python3 fisch_macro.py watch            live readout of what it sees, no input
  python3 fisch_macro.py snap             save a PNG of the reel area for checking
  python3 fisch_macro.py run              start the macro

HOTKEYS while running
  F6 = start / pause      F7 = quit
  Failsafe: slam the mouse into the top-left corner of the screen to pause.
"""

import argparse
import json
import os
import sys
import threading
import time

import numpy as np

import fisch_reel as fr
import fisch_platform as fp
import fisch_auto as fa

if sys.platform == "win32":  # make mouse + screenshot coordinates agree at any scaling
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
# the app keeps settings outside the code folder, so updates don't reset them
DATA_DIR = os.environ.get("FISCH_DATA_DIR") or HERE
CONFIG_PATH = os.path.join(DATA_DIR, "fisch_config.json")

DEFAULTS = {
    # Screen positions are in the same coordinates your mouse reports
    # (points on macOS, so Retina is handled automatically).
    "click_pos": None,          # [x, y] safe spot over water to click/cast from
    "track_left": None,         # x of left end of the reel track
    "track_right": None,        # x of right end of the reel track
    "track_y": None,            # y through the middle of the reel track
    "scan_half_height": 3,      # scan this many px above/below track_y

    # Defaults measured from the Fisch wiki's reel GIF (fischipedia.org/wiki/Fishing):
    # translucent near-black track, off-white bar, thin grey-blue fish line.
    # The grey arrow inside the bar (~132,132,136) is deliberately not matched.
    "track_color": [25, 25, 30], "track_tolerance": 22,
    "bar_color": [239, 238, 239], "bar_tolerance": 22,
    "fish_color": [66, 74, 90], "fish_tolerance": 16,
    "bar_min_frac": 0.02,       # bar must cover this share of track (Tryhard Rod can be 3%)
    "track_min_frac": 0.20,     # track colour must cover this share to count as "reel visible"

    "cast_hold_seconds": 0.95,  # how long to hold the click when casting
    "after_cast_wait": 0.6,

    "shake_mode": "auto",       # auto (click the Shake button only when it's on screen) | navigation | click | wait
    "nav_key": "\\",
    "shake_press_interval": 0.12,
    "shake_region": None,       # [x1, y1, x2, y2] for click mode (None = whole main screen)
    "shake_color": [255, 255, 255], "shake_tolerance": 8,
    "shake_timeout": 25,        # no reel this long after casting -> cast again

    "reel_timeout": 90,
    "reel_lost_seconds": 0.5,   # reel considered over after bar is gone this long
    "frame_interval": 0.005,

    # Reel controller tuning (positions are 0..1 across the track)
    "lookahead": 0.25,          # seconds of bar velocity to predict ahead
    "fish_lookahead": 0.10,     # seconds of fish velocity to lead the fish
    "brake": 0.2,               # extra braking when the bar is moving fast
    "deadzone": 0.012,
    "edge_margin": 0.06,

    "reel_input": "mouse",      # mouse | space  (Fisch also reels with Spacebar)
    "auto": True,               # find the reel, the cast spot and the rod by itself (no Setup)
    "rod": "auto",              # auto | normal | remembrance | pinion  (set from the detected rod in auto mode)
    "auto_geom": None,          # where the reel was last found, relative to the Roblox window
    "expected_bar_w": None,     # bar width from the detected rod's Control (wiki)
    "track_half_height": None,  # measured during Setup (points); used by Remembrance
    "overlay": True,            # draw what the macro sees on top of Roblox
    "post_catch_delay": 1.8,
    "start_delay": 3,
    "monitor": 1,               # which screen Roblox is on (1 = main)
    "config_version": 3,
    "toggle_key": "f6",
    "quit_key": "f7",
}


# --------------------------------------------------------------------- config

DEFAULTS.update({k: v for k, v in fr.PROFILE_DEFAULTS.items() if k not in DEFAULTS})


def load_config():
    cfg = dict(DEFAULTS)
    saved = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            saved = json.load(f)
        cfg.update(saved)
    if saved and saved.get("config_version", 1) < 2:
        # v2: older versions could save the hotbar as the reel's position, and waited 60s
        # for a bite before recasting
        if cfg.get("auto_geom") and not geom_plausible(cfg["auto_geom"]):
            cfg["auto_geom"] = None
            if cfg.get("auto", True):
                cfg.update(track_left=None, track_right=None, track_y=None)
        if cfg.get("shake_timeout", 25) >= 60:
            cfg["shake_timeout"] = 25
        cfg["config_version"] = 2
    if saved and saved.get("config_version", 1) < 3:
        # v3: shaking only happens when a Shake button is really on screen ("auto")
        if cfg.get("shake_mode") == "navigation":
            cfg["shake_mode"] = "auto"
        cfg["config_version"] = 3
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    print(f"Saved -> {CONFIG_PATH}")


def require_calibrated(cfg):
    missing = [k for k in ("click_pos", "track_left", "track_right", "track_y") if cfg.get(k) is None]
    if missing:
        sys.exit(f"Not calibrated yet (missing {', '.join(missing)}). Run: python3 fisch_macro.py calibrate")


# ------------------------------------------------------------------ screen io

def grab_rgb(sct, left, top, width, height):
    """Grab a region (in mouse coordinates) as an RGB uint8 array."""
    shot = sct.grab({"left": int(left), "top": int(top),
                     "width": max(1, int(width)), "height": max(1, int(height))})
    return np.ascontiguousarray(np.asarray(shot)[:, :, 2::-1])  # BGRA -> RGB


def strip_region(cfg):
    h = int(cfg["scan_half_height"])
    left, right = int(cfg["track_left"]), int(cfg["track_right"])
    return left, int(cfg["track_y"]) - h, right - left, 2 * h + 1


def grab_strip(sct, cfg):
    return grab_rgb(sct, *strip_region(cfg))


DEFAULT_GEOM = {"x0": 580 / 1920, "x1": 1344 / 1920, "y": 923 / 1080, "half": 16 / 1080}  # measured on a real reel
AUTO_MARGIN = 8      # points captured beyond each end of the track in auto mode (to check the ends)


def geom_plausible(g):
    """A found reel (fractions of the Roblox window) must look like Fisch's: centred,
    30-55% of the window wide, between 70% and 93% of the way down. (The hotbar sits at
    ~96%, which is what an older version sometimes mistook for the reel.)"""
    try:
        cx = (g["x0"] + g["x1"]) / 2
        w = g["x1"] - g["x0"]
        return abs(cx - 0.5) <= 0.01 and 0.28 <= w <= 0.56 and 0.70 <= g["y"] <= 0.93
    except Exception:
        return False


def capture_rect(sct, cfg, with_band=True):
    """The screen rectangle capture() grabs: (left, top, width, height, band, margin)."""
    h = int(cfg["scan_half_height"])
    left, right, ty = int(cfg["track_left"]), int(cfg["track_right"]), int(cfg["track_y"])
    mon = sct.monitors[min(cfg.get("monitor", 1), len(sct.monitors) - 1)]
    rod = cfg.get("rod")
    mx = 0
    if rod == "auto":
        # whole track height plus a few rows above/below and past each end: that's what
        # tells the real reel apart from scenery and the hotbar
        th = cfg.get("track_half_height") or 0.016 * mon["height"]
        h = max(h, int(th) + 8)
        mx = AUTO_MARGIN
    elif rod == "remembrance":
        # the white fish is told apart from the bar's arrow by spanning the full track height
        th = cfg.get("track_half_height") or 0.016 * mon["height"]
        h = max(h, int(th * 0.85))
    band = 0
    if with_band and rod == "pinion":
        band = int(cfg.get("pinion_note_band", 0.55) * mon["height"])
        band = min(band, ty - mon["top"] - h)
    return left - mx, ty - h - band, right - left + 2 * mx, 2 * h + 1 + band, band, mx


def capture(sct, cfg, with_band=True):
    """Grab the reel area. For Pinion's Aria this includes a band above the track
    where the notes fall. Returns a fisch_reel.Frame."""
    mon = sct.monitors[min(cfg.get("monitor", 1), len(sct.monitors) - 1)]
    left, top, width, height, band, mx = capture_rect(sct, cfg, with_band)
    img = grab_rgb(sct, left, top, width, height)
    scale = img.shape[1] / max(1, width)
    row0 = int(round(band * scale))
    frame = fr.Frame(img, row0, img.shape[0], mon["height"] * scale, scale)
    frame.mx = int(round(mx * scale))
    return frame


def analyze(sct, cfg, memory=None, with_band=True):
    return fr.detect(capture(sct, cfg, with_band), cfg, cfg.get("rod", "normal"), memory)


def sample_color(sct, x, y):
    # 3x3 points, take the median so one odd pixel doesn't wreck it
    img = grab_rgb(sct, x - 1, y - 1, 3, 3).reshape(-1, 3)
    return [int(v) for v in np.median(img, axis=0)]


# ------------------------------------------------------------------ detection

color_mask = fr.color_mask
runs = fr.runs
merge_runs = fr.merge_runs


def analyze_strip(img, cfg, profile=None):
    """Detection on a strip that is just the track rows (no note band)."""
    frame = fr.Frame(img, 0, img.shape[0], img.shape[0] * 60)
    return fr.detect(frame, cfg, profile or cfg.get("rod", "normal"))


class LegacyReelController:
    """(Old) bang-bang controller with velocity lookahead, kept for comparison.
    Holding the mouse pushes the bar right; releasing lets it drift left."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.prev_center = None
        self.prev_t = None
        self.vel = 0.0
        self.fish_vel = 0.0
        self.hold = False
        self.last_fish = None

    def step(self, det, now):
        bl, br = det["bar"]
        center = (bl + br) / 2
        fish = det["fish"] if det["fish"] is not None else self.last_fish
        if fish is None:
            fish = center

        if self.prev_center is not None and now > self.prev_t:
            dt = now - self.prev_t
            self.vel = 0.5 * self.vel + 0.5 * (center - self.prev_center) / dt
            self.fish_vel = 0.7 * self.fish_vel + 0.3 * (fish - self.last_fish) / dt
        self.prev_center, self.prev_t, self.last_fish = center, now, fish

        m = self.cfg["edge_margin"]
        if fish <= m:
            self.hold = False
        elif fish >= 1 - m:
            self.hold = True
        else:
            target = fish + self.fish_vel * self.cfg["fish_lookahead"]
            predicted = (center + self.vel * self.cfg["lookahead"]
                         + self.cfg["brake"] * self.vel * abs(self.vel))
            err = target - predicted
            dz = self.cfg["deadzone"]
            if err > dz:
                self.hold = True
            elif err < -dz:
                self.hold = False
            # inside deadzone: keep previous state
        return self.hold


# --------------------------------------------------------------------- input

def parse_key(name):
    from pynput.keyboard import Key
    if len(name) == 1:
        return name
    return getattr(Key, name.lower())


class Inputs:
    def __init__(self, reel_input="mouse"):
        from pynput.mouse import Controller as M, Button
        from pynput.keyboard import Controller as K, Key
        self.mouse, self.kb = M(), K()
        self.Button, self.Key = Button, Key
        self.reel_input = reel_input
        self.holding = False        # mouse held (casting, or reeling in mouse mode)
        self.space_held = False     # spacebar held (reeling in space mode)

    def move(self, pos):
        self.mouse.position = (int(pos[0]), int(pos[1]))

    def set_hold(self, on):
        """Hold/release the left mouse button."""
        if on and not self.holding:
            self.mouse.press(self.Button.left)
        elif not on and self.holding:
            self.mouse.release(self.Button.left)
        self.holding = on

    def set_reel(self, on):
        """Hold/release whatever input pushes the reel bar right."""
        if self.reel_input != "space":
            return self.set_hold(on)
        if on and not self.space_held:
            self.kb.press(self.Key.space)
        elif not on and self.space_held:
            self.kb.release(self.Key.space)
        self.space_held = on

    def release_all(self):
        self.set_hold(False)
        if self.space_held:
            self.kb.release(self.Key.space)
            self.space_held = False

    def click(self, pos=None):
        if pos:
            self.move(pos)
            time.sleep(0.02)
        self.mouse.click(self.Button.left)

    def tap(self, key):
        k = parse_key(key) if isinstance(key, str) else key
        self.kb.press(k)
        time.sleep(0.03)
        self.kb.release(k)

    def enter(self):
        self.tap(self.Key.enter)


# --------------------------------------------------------------------- macro

class Macro:
    """The fishing loop. Control it with start()/stop(); the CLI and the app
    both drive it this way. Status is exposed as .phase, .catches, .last_det
    and log lines go to the `log` callback."""

    def __init__(self, cfg, log=None):
        self.cfg = cfg
        self.log = log or (lambda msg: print(msg, flush=True))
        self.running = threading.Event()
        self.quit = threading.Event()
        self.io = Inputs(cfg.get("reel_input", "mouse"))
        self.catches = 0
        self.perfect = 0
        self.ctl = None
        self.capture_info = {}
        self.reel_fps = None
        self.window = None
        self.window_found = False
        self.ident = None
        self.ident_t = 0.0
        self._geom_logged = False
        self._stream_logged = False
        self._slow_reels = 0
        self.last_shakes = 0
        self._seen = []
        self._ident_lock = threading.Lock()
        self.phase = "idle"
        self.last_det = None
        self._timer = None
        self._thread = None

    # -- control
    def ensure_thread(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self.worker, daemon=True)
            self._thread.start()

    def start(self, delay=None):
        self.ensure_thread()
        if self.running.is_set() or self._timer is not None:
            return
        delay = self.cfg["start_delay"] if delay is None else delay
        self.phase = "starting"
        self.log(f"Starting in {delay}s - click into Roblox now")

        def go():
            self._timer = None
            if self.phase == "starting":
                self.running.set()
        self._timer = threading.Timer(delay, go)
        self._timer.daemon = True
        self._timer.start()

    def stop(self, reason="Paused"):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        was_active = self.running.is_set() or self.phase == "starting"
        self.running.clear()
        self.phase = "idle"
        if was_active:
            self.log(reason)

    def toggle(self):
        if self.running.is_set() or self.phase == "starting":
            self.stop()
        else:
            self.start()

    def shutdown(self):
        self.stop()
        self.quit.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        self.io.release_all()

    def apply_config(self):
        """Call after changing cfg so input-related settings take effect."""
        self.io.reel_input = self.cfg.get("reel_input", "mouse")

    # -- helpers
    def alive(self):
        if self.quit.is_set() or not self.running.is_set():
            return False
        x, y = self.io.mouse.position
        if x <= 2 and y <= 2:
            self.stop("Failsafe: mouse in top-left corner, paused")
            return False
        return True

    def wait(self, secs):
        end = time.time() + secs
        while time.time() < end:
            if not self.alive():
                return False
            time.sleep(0.02)
        return True

    # -- automatic setup
    def window_rect(self, sct):
        w = fa.find_roblox_window()
        self.window_found = w is not None
        if not w:
            mon = sct.monitors[min(self.cfg.get("monitor", 1), len(sct.monitors) - 1)]
            w = {"left": mon["left"], "top": mon["top"], "width": mon["width"], "height": mon["height"]}
        self.window = w
        return w

    def apply_geom(self, g, w):
        self.cfg.update(track_left=int(round(w["left"] + g["x0"] * w["width"])),
                        track_right=int(round(w["left"] + g["x1"] * w["width"])),
                        track_y=int(round(w["top"] + g["y"] * w["height"])),
                        track_half_height=round(g["half"] * w["height"], 1))

    def identify_async(self, w=None):
        """identify_now() on a helper thread with its own screen reader (one at a time)."""
        if self._ident_lock.locked():
            return False

        def run():
            try:
                import mss
                with mss.mss() as s2:
                    self.identify_now(s2, w)
            except Exception as e:
                self.log(f"Rod detection failed: {e!r}")
        threading.Thread(target=run, daemon=True).start()
        return True

    def identify_now(self, sct, w=None):
        """Read rod / enchant / area off the screen and pick the reel mode from wiki data.
        Only one read runs at a time; extra requests while one is running are dropped."""
        if not self._ident_lock.acquire(blocking=False):
            return self.ident
        try:
            return self._identify(sct, w)
        finally:
            self._ident_lock.release()

    def _identify(self, sct, w=None):
        w = w or self.window_rect(sct)
        img = grab_rgb(sct, w["left"], w["top"], w["width"], w["height"])
        prev = (self.ident or {}).get("rod")
        ident = fa.identify(img)
        if ident.get("rod") is None and prev and not ident.get("error"):
            # the hotbar was hidden (reel up, menu open...): keep what we knew
            ident = dict(self.ident, raw=ident.get("raw"))
            self.ident_t = time.time()
            return ident
        rod = fa.rod_info(ident.get("rod"))
        mode, notes = fa.mode_for(rod, ident.get("area"))
        ident.update(rod_info=rod, mode=mode, notes=notes)
        old = self.ident or {}
        changed = any(old.get(k) != ident.get(k) for k in ("rod", "enchant", "enchant2", "area", "error"))
        self.ident, self.ident_t = ident, time.time()
        if self.cfg.get("auto", True):
            self.cfg["rod"] = mode
            self.cfg["expected_bar_w"] = rod["bar_width"] if rod else None
            shaky = any("shakes" in n for n in notes) or (rod and "shaking_minigame" in rod["flags"])
            self.cfg["scan_half_height"] = 8 if shaky else DEFAULTS["scan_half_height"]
        if not changed:
            return ident
        if ident.get("error"):
            self.log(f"Rod detection off: {ident['error']}")
        else:
            bits = [ident.get("rod") or "rod not recognised"]
            if ident.get("enchant"):
                bits.append(f"enchant {ident['enchant']}" + (f" + {ident['enchant2']}" if ident.get("enchant2") else ""))
            if ident.get("area"):
                bits.append(f"in {ident['area']}")
            if rod:
                bits.append(f"Control {rod['control']:+g} (bar {rod['bar_width'] * 100:.0f}%), Resilience {rod['resilience']}%")
            self.log("Detected: " + ", ".join(bits))
            for n in notes:
                self.log(n)
        return ident

    def auto_prepare(self, sct):
        if not self.cfg.get("auto", True):
            return
        w = self.window_rect(sct)
        # cast from the middle of the game view (clicking anywhere on the water casts)
        self.cfg["click_pos"] = [w["left"] + w["width"] // 2, w["top"] + int(w["height"] * 0.45)]
        g = self.cfg.get("auto_geom")
        if g and not geom_plausible(g):
            g = self.cfg["auto_geom"] = None      # e.g. an old find that was really the hotbar
        if not g and self.cfg.get("rod") == "pinion":
            # Pinion's Aria's see-through track is hard to find by its edges; it sits where every
            # Fisch reel sits, so start from there (refined once a normal reel has been found)
            g = dict(DEFAULT_GEOM)
        if g:
            self.apply_geom(g, w)
            self.want_stream(sct)
        else:
            self.cfg["track_left"] = None
        # re-read the rod now and then (you may have switched rods); in the background so
        # the cast isn't delayed
        if fa.ocr_available() and (self.ident is None or time.time() - self.ident_t > 45):
            self.identify_async(w)

    def want_stream(self, sct):
        """Ask the capture to stream the reel area at full speed (macOS)."""
        if hasattr(sct, "want") and self.cfg.get("track_left") is not None:
            l, t, wd, h, _, _ = capture_rect(sct, self.cfg)
            # a little extra room so small position changes don't need a new stream
            ok = sct.want({"left": l - 4, "top": t - 4, "width": wd + 8, "height": h + 8})
            info = dict(self.capture_info)
            info["method"] = sct.method
            info["why"] = getattr(sct, "why", "")
            self.capture_info = info
            if ok and not self._stream_logged:
                self._stream_logged = True
                self.log("Screen reading: fast stream on (up to 60+ reads a second)")
            return ok
        return False

    def try_locate(self, sct):
        """Look for the reel in the lower part of the Roblox window."""
        w = self.window or self.window_rect(sct)
        top = w["top"] + int(w["height"] * 0.68)
        hgt = int(w["height"] * 0.27)
        img = grab_rgb(sct, w["left"], top, w["width"], hgt)
        sc = img.shape[1] / max(1, w["width"])
        if sc > 1.5:                       # Retina: half resolution is plenty to find it
            img, sc2 = img[::2, ::2], 2
        else:
            sc2 = 1
        loc = fa.locate_reel(img, y_range=(0.0, 1.0), ref_h=w["height"] * sc / sc2)
        if not loc:
            return False
        ok, info = fa.confirm_reel(img, loc, self.cfg.get("expected_bar_w"))
        if not ok:
            return False
        s = sc / sc2
        g = {"x0": loc["x0"] / s / w["width"], "x1": loc["x1"] / s / w["width"],
             "y": (top - w["top"] + loc["y"] / s) / w["height"], "half": loc["half"] / s / w["height"]}
        if not geom_plausible(g):
            return False
        # the track's edges must really be there (not a look-alike row)
        mx = int(AUTO_MARGIN * s)
        y0 = max(0, int(loc["y"] - loc["half"] - 10))
        y1 = min(img.shape[0], int(loc["y"] + loc["half"] + 11))
        x0, x1 = max(0, loc["x0"] - mx), min(img.shape[1], loc["x1"] + mx)
        present, _ = fa.reel_present(img[y0:y1, x0:x1], loc["y"] - y0, loc["half"], loc["x0"] - x0)
        if not present:
            return False
        old = self.cfg.get("auto_geom")
        self.cfg["auto_geom"] = g
        self.apply_geom(g, w)
        if info.get("track_color"):
            self.cfg["track_color"] = info["track_color"]
        if info.get("bar_color"):
            self.cfg["bar_color"] = info["bar_color"]
        if old != g:
            save_config(self.cfg)
        self.want_stream(sct)
        if not self._geom_logged:
            self._geom_logged = True
            self.log(f"Found the reel on screen (x {self.cfg['track_left']}-{self.cfg['track_right']}, "
                     f"y {self.cfg['track_y']}){'' if self.window_found else ' - Roblox window not found, searched the whole screen'}")
        return True

    # -- phases
    def cast(self):
        self.phase = "casting"
        self.io.move(self.cfg["click_pos"])
        time.sleep(0.05)
        self.io.set_hold(True)
        ok = self.wait(self.cfg["cast_hold_seconds"])
        self.io.set_hold(False)
        return ok and self.wait(self.cfg["after_cast_wait"])

    def find_shake(self, sct):
        r = self.cfg["shake_region"]
        if r is None:
            mon = sct.monitors[self.cfg.get("monitor", 1)]
            r = [mon["left"], mon["top"], mon["left"] + mon["width"], mon["top"] + mon["height"]]
        img = grab_rgb(sct, r[0], r[1], r[2] - r[0], r[3] - r[1])
        mask = color_mask(img, self.cfg["shake_color"], self.cfg["shake_tolerance"])
        ys, xs = np.nonzero(mask)
        if xs.size < 20:
            return None
        scale = img.shape[1] / (r[2] - r[0])  # Retina: image px per mouse point
        # cluster around the first hit so two buttons don't average into empty space
        x0, y0 = xs[0], ys[0]
        near = (np.abs(xs - x0) < 60 * scale) & (np.abs(ys - y0) < 60 * scale)
        return (r[0] + xs[near].mean() / scale, r[1] + ys[near].mean() / scale)

    def reel_up(self, sct, frames=3, window=0.6):
        """True once the reel has been seen on `frames` captures in a row and is clearly
        live (something moved, or it stayed up for `window` s). A single matching frame is
        not enough - that's how a still picture used to pass as a reel."""
        seen = self._seen
        det = analyze(sct, self.cfg, with_band=self.cfg.get("rod") == "pinion")
        self.last_det = det
        now = time.time()
        if not det["visible"]:
            seen.clear()
            return False
        seen.append((now, det["bar"], det["fish"]))
        del seen[:-8]
        if len(seen) < frames:
            return False
        cs = [(b[0] + b[1]) / 2 for _, b, _ in seen]
        fs = [f for _, _, f in seen if f is not None]
        moved = (max(cs) - min(cs) > 0.004) or (len(fs) >= 2 and max(fs) - min(fs) > 0.004)
        return moved or (now - seen[0][0] >= window)

    def find_shake_text(self, sct):
        """Screen position (points) of the Shake button if one is showing, else None."""
        w = self.window or self.window_rect(sct)
        img = grab_rgb(sct, w["left"], w["top"], w["width"], w["height"])
        pos = fa.find_shake(img)
        if pos is None:
            return None
        sc = img.shape[1] / max(1, w["width"])
        return (w["left"] + pos[0] / sc, w["top"] + pos[1] / sc)

    def shake(self, sct):
        """Wait for the bite. Shakes ONLY when a Shake button is actually on screen: with no
        button, nothing is pressed at all (an Enter or click with nothing to hit would open the
        inventory or a menu instead)."""
        self.phase = "shaking"
        mode = self.cfg["shake_mode"]
        see = fa.ocr_available()
        if mode == "auto" and not see:
            mode = "wait"                    # can't see the button (Windows): never press blindly
        gated = see and mode in ("auto", "navigation")
        nav_on = False
        if mode == "navigation" and not gated:
            self.io.tap(self.cfg["nav_key"])  # old behaviour (no way to see the button)
            nav_on = True
        t0 = last = time.time()
        found = False
        auto = self.cfg.get("auto", True)
        next_locate = t0 + 0.25
        next_scan = t0 + 0.15
        last_button = 0.0
        shakes = 0
        self._seen = []
        try:
            while self.alive():
                now = time.time()
                if self.cfg.get("track_left") is not None:
                    if self.reel_up(sct):
                        found = True
                        break
                if auto and now >= next_locate and not self._seen:
                    # not on screen where we expect it (or not known yet): search for it
                    known = self.cfg.get("track_left") is not None
                    next_locate = now + (1.0 if known else 0.25)
                    if self.try_locate(sct):
                        continue
                if now - t0 > self.cfg["shake_timeout"]:
                    extra = " (no Shake button showed up)" if gated and shakes == 0 else ""
                    self.log(f"No reel after {self.cfg['shake_timeout']:.0f}s{extra} - casting again")
                    break
                if gated and now >= next_scan:
                    pos = self.find_shake_text(sct)
                    next_scan = time.time() + 0.08
                    if pos is not None and self.alive():
                        last_button = time.time()
                        shakes += 1
                        if mode == "auto":
                            self.io.click(pos)            # right on the button
                            next_scan = time.time() + 0.12
                        else:
                            if not nav_on:
                                self.io.tap(self.cfg["nav_key"])
                                nav_on = True
                                time.sleep(0.05)
                            self.io.enter()
                    elif nav_on and time.time() - last_button > 0.6:
                        self.io.tap(self.cfg["nav_key"])  # button gone: leave UI navigation
                        nav_on = False
                elif not gated and now - last >= self.cfg["shake_press_interval"]:
                    last = now
                    if mode == "navigation":
                        self.io.enter()
                    elif mode == "click":
                        pos = self.find_shake(sct)
                        if pos:
                            self.io.click(pos)
                time.sleep(0.01)
        finally:
            if nav_on:
                self.io.tap(self.cfg["nav_key"])  # leave UI navigation
            if mode in ("click", "auto") and shakes:
                self.io.move(self.cfg["click_pos"])
        if gated:
            self.last_shakes = shakes
        return found

    def reel(self, sct):
        self.phase = "reeling"
        rod = self.cfg.get("rod", "normal")
        t0 = time.time()
        ctl = fr.ReelController(self.cfg, rod, t0=t0)
        self.ctl = ctl
        memory = {}
        last_seen = t0
        frames = 0
        seq = 0
        last_move = t0
        ref = None                      # (bar centre, fish) when things last moved
        held = released = 0.0           # input time since then
        last_t = t0
        streaming = hasattr(sct, "wait_new")
        try:
            while self.alive():
                if streaming:
                    seq = sct.wait_new(seq, 0.03)     # next frame from the stream (60/s)
                now = time.time()
                det = analyze(sct, self.cfg, memory)
                self.last_det = det
                if det["visible"]:
                    last_seen = now
                    frames += 1
                    hold = ctl.step(det, now)
                    self.io.set_reel(hold)
                    self._check_sides(det, hold, now, now - last_t, t0, memory)
                    c = (det["bar"][0] + det["bar"][1]) / 2
                    f = det["fish"]
                    if ref is None or abs(c - ref[0]) > 0.004 or (
                            f is not None and (ref[1] is None or abs(f - ref[1]) > 0.004)):
                        ref, last_move = (c, f), now
                        held = released = 0.0
                    else:
                        dt_ = now - last_t
                        if hold:
                            held += dt_
                        else:
                            released += dt_
                        # in a live reel the bar always answers our input: if we've both held
                        # and released for a while and nothing moved, it's a still picture
                        if (now - t0 > 2.0 and now - last_move > 1.5 and held > 0.35 and released > 0.35):
                            self.log("The reel picture isn't reacting - treating the reel as over")
                            break
                    last_t = now
                elif now - last_seen > self.cfg["reel_lost_seconds"]:
                    break
                if now - t0 > self.cfg["reel_timeout"]:
                    self.log("Reel took too long, giving up on this one")
                    break
                if not streaming or seq == 0:
                    time.sleep(self.cfg["frame_interval"])
        finally:
            self.io.release_all()
        if self.running.is_set() and frames > 5:
            self.catches += 1
            dur = time.time() - t0
            if rod == "pinion":
                perfect = ctl.notes_missed == 0
                detail = f"notes {ctl.notes_caught} caught, {ctl.notes_missed} missed"
            elif rod == "remembrance":
                perfect = ctl.frames_out == 0 and ctl.frames_avoid_in == 0
                detail = (f"white fish out {ctl.frames_out} frames, dark fish in {ctl.frames_avoid_in} frames"
                          if not perfect else "white fish always in, dark fish always out")
            else:
                perfect = ctl.frames_out == 0
                detail = "fish never left the bar" if perfect else f"fish left the bar in {ctl.frames_out} frames"
            self.perfect += int(perfect)
            fps = frames / max(dur, 0.01)
            self.reel_fps = round(fps)
            self.log(f"Reel {self.catches}: {'PERFECT' if perfect else 'not perfect'} - {detail} "
                     f"({dur:.1f}s, {fps:.0f} fps)")
            self._slow_reels = self._slow_reels + 1 if fps < 25 else 0
            if self._slow_reels == 2:      # say it once, not after every reel
                why = self.capture_info.get("why") or ""
                self.log("Warning: the screen is only being read about %d times a second (the bar needs 30+ for "
                         "reliable perfects)%s. Closing other heavy apps or lowering Roblox's graphics helps."
                         % (fps, f" - fast stream off: {why}" if why else ""))

    def _check_sides(self, det, hold, now, dt, t0, memory):
        """Auto mode, bar against a wall: if we push it away from the wall and it doesn't
        budge, we've mixed up which part is the bar and which is the empty track - tell the
        detector to swap."""
        if self.cfg.get("rod") != "auto" or now - t0 < self.cfg.get("lock_seconds", 1.2) + 0.3:
            return
        st = memory.setdefault("_sides", {"r": 0.0, "l": 0.0, "bar": None})
        bl, br = det["bar"]
        same = st["bar"] is not None and abs(bl - st["bar"][0]) < 0.004 and abs(br - st["bar"][1]) < 0.004
        st["bar"] = (bl, br)
        if br >= 0.985 and not hold and same:
            st["r"] += dt          # releasing should pull it off the right wall
        else:
            st["r"] = 0.0
        if bl <= 0.015 and hold and same:
            st["l"] += dt          # holding should push it off the left wall
        else:
            st["l"] = 0.0
        a = memory.setdefault("auto", {})
        if st["r"] > 0.25:
            a["force_ref"] = "right"
            a.pop("track_ref", None)
            a.pop("track_col", None)
            st["r"] = 0.0
        elif st["l"] > 0.25:
            a["force_ref"] = "left"
            a.pop("track_ref", None)
            a.pop("track_col", None)
            st["l"] = 0.0

    def open_capture(self):
        """mss, or the faster direct display capture on Mac if it works here."""
        import mss
        base = mss.mss()
        region = None
        if self.cfg.get("track_left") is not None:
            l, t, w, h = strip_region(self.cfg)
            region = {"left": l, "top": t, "width": w, "height": h}
        sct, info = fp.best_sct(base, region)
        self.capture_info = info
        if fp.IS_MAC:
            import fisch_sck
            ok, why = fisch_sck.available()
            if ok:
                return fisch_sck.CaptureHub(sct, log=self.log)
            self.capture_info = dict(info, why=f"ScreenCaptureKit not installed ({why})")
        return sct

    def worker(self):
        with self.open_capture() as sct:
            while not self.quit.is_set():
                if not self.running.is_set():
                    self.io.release_all()
                    time.sleep(0.05)
                    continue
                try:
                    self.auto_prepare(sct)
                    # a reel already on screen (started mid-fight): play it instead of casting
                    self._seen = []
                    if self.cfg.get("track_left") is not None and any(
                            self.reel_up(sct) for _ in range(4)):
                        self.reel(sct)
                        self.phase = "waiting"
                        self.wait(self.cfg["post_catch_delay"])
                        continue
                    if not self.cast():
                        continue
                    if not self.shake(sct):
                        continue
                    self.reel(sct)
                    self.phase = "waiting"
                    self.wait(self.cfg["post_catch_delay"])
                except Exception as e:  # never leave the mouse held down
                    self.io.release_all()
                    self.stop(f"Error: {e!r} - paused")
                    time.sleep(0.5)

    # -- hotkeys
    def start_hotkeys(self, on_quit=None):
        """Global F6/F7 hotkeys. Returns the listener, or None if unavailable."""
        try:
            from pynput import keyboard
        except Exception:
            return None
        toggle, quitk = parse_key(self.cfg["toggle_key"]), parse_key(self.cfg["quit_key"])

        def matches(key, target):
            if isinstance(target, str):
                return getattr(key, "char", None) == target
            return key == target

        def on_press(key):
            if matches(key, toggle):
                self.toggle()
            elif matches(key, quitk):
                self.stop("Stopped (quit key)")
                if on_quit:
                    on_quit()
                    return False

        try:
            listener = keyboard.Listener(on_press=on_press)
            listener.daemon = True
            listener.start()
            return listener
        except Exception as e:
            self.log(f"Hotkeys unavailable ({e}); use the buttons instead")
            return None

    def run(self):
        """CLI mode: hotkeys only."""
        self.ensure_thread()
        done = threading.Event()
        listener = self.start_hotkeys(on_quit=done.set)
        print(f"Ready. {self.cfg['toggle_key'].upper()} = start/pause, "
              f"{self.cfg['quit_key'].upper()} = quit. Equip your rod first.")
        try:
            done.wait()
        except KeyboardInterrupt:
            pass
        if listener:
            listener.stop()
        self.shutdown()
        print("bye")


# --------------------------------------------------------------- calibration

def hover(prompt, secs):
    from pynput.mouse import Controller
    m = Controller()
    print(f"\n{prompt}")
    for i in range(secs, 0, -1):
        print(f"  capturing in {i}...", end="\r", flush=True)
        time.sleep(1)
    x, y = m.position
    print(f"  got ({int(x)}, {int(y)})          ")
    return int(x), int(y)


def calibrate(cfg, step, delay):
    import mss
    steps = ["click", "track", "bar", "fish"] if step == "all" else [step]
    with mss.mss() as sct:
        if "click" in steps:
            cfg["click_pos"] = list(hover(
                "CLICK SPOT: hover over open water (somewhere safe to click, away from buttons).", delay))

        if "track" in steps:
            print("\nGet the reel minigame on screen (cast, shake, wait for a bite). "
                  "Don't click during the next steps so your bar rests on the left.")
            lx, ly = hover("TRACK LEFT: hover the very left end of the dark reel track, at mid-height.", delay)
            rx, _ = hover("TRACK RIGHT: hover the very right end of the track (same height).", delay)
            if rx <= lx:
                sys.exit("Right edge must be to the right of the left edge. Try again.")
            cfg.update(track_left=lx, track_right=rx, track_y=ly)
            # sample empty track a little in from the right end (bar rests on the left)
            cfg["track_color"] = sample_color(sct, rx - max(4, (rx - lx) // 20), ly)
            print(f"  track colour {cfg['track_color']}")

        if "bar" in steps:
            require_calibrated(cfg)
            x, y = hover("YOUR BAR: hover the middle of your white bar (minigame up, not clicking).", delay)
            cfg["bar_color"] = sample_color(sct, x, y)
            print(f"  bar colour {cfg['bar_color']}")

        if "fish" in steps:
            require_calibrated(cfg)
            print("\nFISH: with the minigame up, the script watches the track for ~1.5s and "
                  "picks the colour that is neither track nor bar.")
            input("  Press Enter, then click back into Roblox within 2s...")
            time.sleep(2)
            found = []
            end = time.time() + 1.5
            while time.time() < end:
                img = grab_strip(sct, cfg)
                other = ~(color_mask(img, cfg["track_color"], cfg["track_tolerance"]) |
                          color_mask(img, cfg["bar_color"], cfg["bar_tolerance"]))
                # ignore anti-aliased edges next to the bar
                bar = color_mask(img, cfg["bar_color"], cfg["bar_tolerance"]).any(axis=0)
                near_bar = np.convolve(bar, np.ones(5), "same") > 0
                other &= ~near_bar[None, :]
                if other.any():
                    found.append(img[other])
                time.sleep(0.03)
            if not found:
                print("  Couldn't find a fish colour. Is the minigame up? Keeping old value.")
            else:
                px = np.concatenate(found) // 8 * 8  # quantize, then take the most common colour
                cols, counts = np.unique(px, axis=0, return_counts=True)
                c = cols[counts.argmax()] + 4
                cfg["fish_color"] = [int(v) for v in c]
                print(f"  fish colour {cfg['fish_color']}")

        if "shake" in steps:
            print("\nSHAKE (only for shake_mode=click): a SHAKE button must be on screen.")
            x, y = hover("Hover a solid-coloured part of the SHAKE button (e.g. its outline).", delay)
            cfg["shake_color"] = sample_color(sct, x, y)
            cfg["shake_mode"] = "click"
            print(f"  shake colour {cfg['shake_color']} (shake_mode set to click)")

    save_config(cfg)
    print("\nNext: run `python3 fisch_macro.py watch` during a reel to check detection.")


# -------------------------------------------------------------- debug tools

def render(det, width=60):
    line = ["-"] * width
    if det["bar"]:
        for i in range(int(det["bar"][0] * width), min(width, int(det["bar"][1] * width) + 1)):
            line[i] = "="
    if det["fish"] is not None:
        line[min(width - 1, int(det["fish"] * width))] = "F"
    return "".join(line)


def watch(cfg):
    import mss
    require_calibrated(cfg)
    print("Watching (Ctrl+C to stop). '=' your bar, 'F' fish.")
    with mss.mss() as sct:
        try:
            while True:
                det = analyze(sct, cfg)
                tag = "REEL" if det["visible"] else "....."
                print(f"{tag:5} [{render(det)}] track={det['track_frac']:.2f}   ", end="\r", flush=True)
                time.sleep(0.05)
        except KeyboardInterrupt:
            print()


def snap(cfg):
    import mss
    import mss.tools
    require_calibrated(cfg)
    left, top, w, h = strip_region(cfg)
    with mss.mss() as sct:
        shot = sct.grab({"left": left - 10, "top": top - 30, "width": w + 20, "height": h + 60})
        out = os.path.join(HERE, "fisch_snap.png")
        mss.tools.to_png(shot.rgb, shot.size, output=out)
    print(f"Saved {out} - the scan line runs through the middle of this image.")


# ---------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Pixel-based Fisch macro")
    ap.add_argument("command", choices=["calibrate", "watch", "snap", "run"])
    ap.add_argument("step", nargs="?", default="all",
                    choices=["all", "click", "track", "bar", "fish", "shake"])
    ap.add_argument("--delay", type=int, default=4, help="seconds to hover during calibration")
    a = ap.parse_args()
    cfg = load_config()

    if a.command == "calibrate":
        calibrate(cfg, a.step, a.delay)
    elif a.command == "watch":
        watch(cfg)
    elif a.command == "snap":
        snap(cfg)
    else:
        require_calibrated(cfg)
        Macro(cfg).run()


if __name__ == "__main__":
    main()
