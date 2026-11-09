"""
Reel minigame: per-rod detection and a planning controller aimed at perfect catches.

All horizontal positions are fractions of the track width (0 = left end, 1 = right end).
Vertical distances for notes are in fractions of screen height.

Rod profiles
  normal       dark track, white bar, one grey fish line
  remembrance  white fish must stay IN the bar, black fish must stay OUT of it
  pinion       Pinion's Aria: bar found by its cyan outline, fish by the bird icon
               above it, and falling notes that must land inside the bar

Colours were measured from footage of the real minigames (Fisch wiki GIF and
YouTube showcases); the app's Setup can re-sample them from your own screen.
"""

import math
import time

import numpy as np

PROFILES = ("auto", "normal", "remembrance", "pinion")

PROFILE_DEFAULTS = {
    # Pinion's Aria
    "pinion_note_band": 0.65,      # how far above the track to look for notes (screen heights)
    "pinion_fall_speed": 0.65,     # default note fall speed (screen heights / s) until measured
    "pinion_note_margin": 0.03,    # keep notes this far inside the bar edges
    "pinion_note_inner": 0.5,      # how far from the bar centre a note may land (0 = dead centre)
    "pinion_commit_buffer": 0.3,   # extra seconds of safety when heading over to a note
    "pinion_center_pull": 0.4,     # 0 = follow the fish freely between notes, 1 = wait mid-track
    # Remembrance
    "avoid_margin": 0.03,          # keep the dark fish this far outside the bar
    # Controller
    "input_latency": 0.06,         # seconds between our input and the bar reacting
    "plan_horizon": 1.0,           # seconds ahead to start lining up for a note
    "safe_zone": 0.6,              # aim to keep the fish inside the middle 60% of the bar
    "fish_lead": 0.08,             # seconds to lead the fish by (its velocity x this)
    "lock_seconds": 1.2,           # minigame ignores input for this long at the start
}


# ----------------------------------------------------------------- helpers

def color_mask(img, color, tol):
    diff = np.abs(img.astype(np.int16) - np.asarray(color, np.int16))
    return diff.max(axis=2) <= tol


def runs(mask):
    if not mask.any():
        return []
    d = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.flatnonzero(d == 1).tolist(), np.flatnonzero(d == -1).tolist()))


def merge_runs(rs, max_gap):
    out = []
    for s, e in rs:
        if out and s - out[-1][1] <= max_gap:
            out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def narrow_runs(mask_cols, w, max_frac=0.03, gap=2):
    """Centres of thin vertical lines (like a fish) as fractions of width."""
    out = []
    for s, e in merge_runs(runs(mask_cols), gap):
        if (e - s) <= max(3, max_frac * w):
            out.append(((s + e) / 2) / w)
    return out


# ------------------------------------------------------------- detection

class Frame:
    """One capture of the reel area. `img` is RGB; `row0`/`row1` are the image rows
    covering the track; rows above `row0` are the band used for notes and the bird."""

    def __init__(self, img, row0, row1, screen_h_px, scale=1.0):
        self.img = img
        self.row0, self.row1 = row0, row1
        self.screen_h_px = screen_h_px
        self.scale = scale            # image pixels per screen point (2 on Retina)

    @property
    def center(self):
        return (self.row0 + self.row1) // 2

    def track_top(self, cfg):
        """Image row of the top edge of the track (things above it are notes/icons)."""
        th = cfg.get("track_half_height")
        half_px = th * self.scale if th else 0.016 * self.screen_h_px
        return max(0, int(self.center - half_px))

    @property
    def strip(self):
        return self.img[self.row0:self.row1]


def detect(frame, cfg, profile, memory=None):
    """Find bar, fish, things to avoid and notes. Returns a dict (positions 0..1)."""
    memory = memory if memory is not None else {}
    strip = frame.strip
    w = strip.shape[1]
    track_cols = color_mask(strip, cfg["track_color"], cfg["track_tolerance"]).any(axis=0)
    res = {"visible": False, "bar": None, "fish": None, "avoid": [], "notes": [],
           "track_frac": float(track_cols.mean()), "profile": profile}

    if profile == "auto":
        import fisch_auto
        th = cfg.get("track_half_height")
        half_px = th * frame.scale if th else 0.016 * frame.screen_h_px
        mx = getattr(frame, "mx", 0)
        img = frame.img[:, mx:frame.img.shape[1] - mx] if mx else frame.img
        if mx:
            present, pscore = fisch_auto.reel_present(frame.img, frame.center, half_px, mx)
        else:
            present, pscore = True, 1.0
        res["present"] = pscore
        if not present:
            return res                      # no reel here: don't even look for a bar
        exp = cfg.get("expected_bar_w")
        bar, fish, tf = fisch_auto.detect_auto(img, frame.center, half_px, exp,
                                               memory.setdefault("auto", {}))
        if bar is not None:
            w = bar[1] - bar[0]
            if not (0.02 <= w <= 0.97) or (exp and not (0.45 * exp <= w <= 1.7 * exp)):
                bar = None
        res.update(bar=bar, fish=fish, track_frac=tf)
        res["visible"] = bar is not None
        return res
    if profile == "pinion":
        _detect_pinion(frame, cfg, res, memory)
    else:
        bar_cols = color_mask(strip, cfg["bar_color"], cfg["bar_tolerance"]).any(axis=0)
        bar_runs = merge_runs(runs(bar_cols), max_gap=max(2, int(0.06 * w)))
        if bar_runs:
            s, e = max(bar_runs, key=lambda r: r[1] - r[0])
            if (e - s) >= cfg["bar_min_frac"] * w:
                res["bar"] = (s / w, e / w)
        if profile == "remembrance":
            _detect_remembrance(strip, cfg, res, bar_cols)
        else:
            fish_cols = color_mask(strip, cfg["fish_color"], cfg["fish_tolerance"]).any(axis=0)
            fr = merge_runs(runs(fish_cols), 2)
            if fr:
                s, e = max(fr, key=lambda r: r[1] - r[0])
                res["fish"] = ((s + e) / 2) / w

    res["visible"] = res["bar"] is not None and (
        res["track_frac"] >= cfg["track_min_frac"] or res["fish"] is not None)
    return res


def _detect_remembrance(strip, cfg, res, bar_cols):
    """Rules checked against real Remembrance footage:
    - dark fish: a thin near-black line
    - white fish: a white capsule with a grey outline that spans the whole track height.
      On the white bar it shows as two full-height grey outline lines with white between;
      on the dark track it's a thin full-height white line. The bar's arrow glyph is grey
      too but only covers the middle of the bar, so full-height checks reject it."""
    w = strip.shape[1]
    px = strip.astype(np.int16)
    mx, mn = px.max(axis=2), px.min(axis=2)
    track = (np.abs(px - np.asarray(cfg["track_color"], np.int16)).max(axis=2) <= cfg["track_tolerance"])
    dark_f = (mx <= cfg.get("dark_fish_max", 14)).mean(axis=0)
    white_f = (mn >= 235).mean(axis=0)
    track_f = track.mean(axis=0)
    outline_f = ((mx <= 225) & (mn >= 150) & ((mx - mn) <= 16)).mean(axis=0)

    res["avoid"] = [((s_ + e_) / 2) / w for s_, e_ in merge_runs(runs(dark_f >= 0.6), 2)
                    if (e_ - s_) <= max(3, 0.03 * w)]

    cands = []
    full = merge_runs(runs(outline_f >= 0.85), 1)
    for (a0, a1), (b0, b1) in zip(full, full[1:]):          # outline pair on the bar
        gap = b0 - a1
        if max(2, 0.004 * w) <= gap <= 0.03 * w and white_f[a1:b0].mean() >= 0.6:
            cands.append(((a0 + b1) / 2) / w)
    for s_, e_ in merge_runs(runs(white_f >= 0.85), 1):      # white line on the dark track
        if (e_ - s_) <= max(3, 0.03 * w):
            left = track_f[max(0, s_ - 3)] if s_ >= 3 else 0
            right = track_f[min(w - 1, e_ + 2)]
            if min(left, right) >= 0.5:
                cands.append(((s_ + e_) / 2) / w)
    if cands:
        bl, br = res["bar"] if res["bar"] else (0.5, 0.5)
        res["fish"] = min(cands, key=lambda x: abs(x - (bl + br) / 2))


def _span_mask(w, a, b):
    m = np.zeros(w, bool)
    if a >= 0:
        m[max(0, int(a * w)):min(w, int(b * w) + 1)] = True
    return m


def _detect_pinion(frame, cfg, res, memory):
    img, strip = frame.img, frame.strip
    w = strip.shape[1]
    px = strip.astype(np.int16)
    r, g, b = px[..., 0], px[..., 1], px[..., 2]
    # bar: the cyan outline (and some of the fill) - take the outermost columns
    outline = ((g >= 205) & (b >= 230) & (r >= 135) & (r <= 210) & (g - r >= 18)).any(axis=0)
    cols = np.flatnonzero(outline)
    exp_w = memory.get("bar_w")
    if cols.size >= 2:
        s, e = cols[0], cols[-1] + 1
        width = (e - s) / w
        if 0.02 <= width <= 0.8 and (exp_w is None or width <= exp_w * 1.6):
            res["bar"] = (s / w, e / w)
            memory["bar_w"] = width if exp_w is None else 0.8 * exp_w + 0.2 * width
        elif exp_w is not None:   # extra matches outside the bar: keep the side nearest last time
            last = memory.get("bar")
            if last:
                c = (last[0] + last[1]) / 2
                res["bar"] = (max(0, c - exp_w / 2), min(1, c + exp_w / 2))
    if res["bar"]:
        memory["bar"] = res["bar"]

    # fish: the white bird icon just above the track (fallback: blue capsule in the track)
    H = frame.screen_h_px
    top = frame.track_top(cfg)
    band_top = max(0, top - int(0.085 * H))
    band_bot = max(0, top - int(0.004 * H))
    fish = None
    if band_bot > band_top:
        band = img[band_top:band_bot].astype(np.int16)
        bird = (band[..., 0] > 205) & (band[..., 1] > 205) & (band[..., 2] > 220)
        bc = bird.any(axis=0)
        groups = merge_runs(runs(bc), max(3, int(0.02 * w)))
        groups = [(s, e) for s, e in groups if bird[:, s:e].sum() >= 12]
        if groups:
            last = memory.get("fish")
            pick = (min(groups, key=lambda se: abs((se[0] + se[1]) / 2 / w - last)) if last is not None
                    else max(groups, key=lambda se: bird[:, se[0]:se[1]].sum()))
            fish = (pick[0] + pick[1]) / 2 / w
    if fish is None:
        caps = ((b >= 225) & (g >= 170) & (g <= 205) & (g - r >= 30)).any(axis=0)
        c = narrow_runs(caps, w, 0.03)
        if c:
            fish = c[0]
    res["fish"] = fish
    if fish is not None:
        memory["fish"] = fish

    # notes: lavender blobs falling in the band above the track (not the bird)
    nb_top = max(0, top - int(cfg.get("pinion_note_band", 0.55) * H))
    nb_bot = max(0, top - int(0.012 * H))       # stay clear of the bar's glowing top edge
    notes = []
    if nb_bot > nb_top:
        band = img[nb_top:nb_bot:2, ::2].astype(np.int16)   # half resolution is plenty
        rr, gg, bb = band[..., 0], band[..., 1], band[..., 2]
        m = (rr > 172) & (gg > 172) & (bb > 210) & (bb - rr >= 18)
        if fish is not None:   # blank out the bird column
            fx = int(fish * w / 2)
            m[:, max(0, fx - int(0.05 * w / 2)):fx + int(0.05 * w / 2) + 1] = False
        colsum = m.sum(axis=0)
        for s, e in merge_runs(runs(colsum > 0), max(2, int(0.015 * w / 2))):
            if colsum[s:e].sum() < 10 or (e - s) > 0.08 * w / 2:
                continue
            ys = np.flatnonzero(m[:, s:e].any(axis=1))
            bottom_px = nb_top + int(ys.max()) * 2
            dist = (top - bottom_px) / H                 # screen heights above the track
            notes.append({"x": ((s + e) / 2) * 2 / w, "dist": max(0.0, dist)})
    res["notes"] = notes


# ------------------------------------------------------------ bar physics

class BarModel:
    """Learns how the bar moves: dv/dt = A_hold*u_hold - A_rel*(1-u_hold) - k*v.
    Starts from a sensible guess and adapts from what it sees, so faster rods
    (e.g. Pinion's Aria's +100% bar speed) are handled automatically."""

    def __init__(self, a_hold=1.6, a_rel=1.6, k=2.0):
        self.a_hold, self.a_rel, self.k = a_hold, a_rel, k
        self._samples = []

    def accel(self, v, hold):
        return (self.a_hold if hold else -self.a_rel) - self.k * v

    def learn(self, v, a, hold):
        """One sample: velocity v and acceleration a (both from a centred 3-point stencil,
        so measurement noise in v and a is uncorrelated and the fit isn't biased)."""
        if not (-20 < a < 20):
            return
        self._samples.append((v, a, hold))
        if len(self._samples) > 240:
            self._samples = self._samples[-240:]
        if len(self._samples) >= 30 and len(self._samples) % 10 == 0:
            self._fit()

    def _fit(self):
        S = np.array([(v, a, 1.0 if h else 0.0) for v, a, h in self._samples])
        v, a, h = S[:, 0], S[:, 1], S[:, 2]
        if h.min() == h.max():
            return
        # a = A_hold*h - A_rel*(1-h) - k*v   -> least squares on [h, -(1-h), -v]
        X = np.stack([h, -(1 - h), -v], axis=1)
        sol, *_ = np.linalg.lstsq(X, a, rcond=None)
        ah, ar, k = sol
        if 0.2 < ah < 20 and 0.2 < ar < 20 and -0.5 < k < 15:
            # blend so one noisy fit can't throw things off
            self.a_hold = 0.7 * self.a_hold + 0.3 * ah
            self.a_rel = 0.7 * self.a_rel + 0.3 * ar
            self.k = 0.7 * self.k + 0.3 * max(0.0, k)


# --------------------------------------------------------------- planner

class ReelController:
    """Each frame: estimate bar/fish motion, then try a set of short input plans
    ("hold for t1, then release", "release for t1, then hold") in a simulation
    of the bar and pick the one that keeps the fish safest - plus, per rod,
    catching notes (Pinion's Aria) and dodging the dark fish (Remembrance)."""

    DT = 1 / 60

    def __init__(self, cfg, profile="normal", t0=None):
        self.cfg = cfg
        self.profile = profile
        self.model = BarModel()
        self.t0 = t0
        self.prev = None          # (t, center, hold)
        self.vel = 0.0
        self.fish_vel = 0.0
        self.last_fish = None
        self.avoid_prev = []
        self.hold = False
        self.notes = []           # tracked notes: dicts x, dist, speed, t
        self.width = None
        self.target = None
        self._inputs = []         # (time, hold) history, to match inputs with bar motion
        self._hist = []           # last 3 (time, bar centre, at_wall) for learning
        self.frames_avoid_in = 0  # Remembrance: frames with the dark fish inside the bar
        self.frames_out = 0       # frames with the fish outside the bar (after lock)
        self.notes_missed = 0
        self.notes_caught = 0

    def opt(self, k):
        return self.cfg.get(k, PROFILE_DEFAULTS.get(k))

    # -- tracking -------------------------------------------------------
    def _track_notes(self, seen, now):
        fall0 = self.opt("pinion_fall_speed")
        new = []
        for n in seen:
            match = None
            for old in self.notes:
                if abs(old["x"] - n["x"]) < 0.025 and old["dist"] >= n["dist"] - 0.01:
                    match = old
                    break
            if match:
                dt = now - match["t"]
                if dt > 0.01:
                    sp = (match["dist"] - n["dist"]) / dt
                    if 0.05 < sp < 5:
                        match["speed"] = 0.5 * match["speed"] + 0.5 * sp
                match.update(x=0.7 * match["x"] + 0.3 * n["x"], dist=n["dist"], t=now)
                new.append(match)
            else:
                new.append({"x": n["x"], "dist": n["dist"], "speed": fall0, "t": now})
        # notes that vanished: did they land in the bar?
        for old in self.notes:
            if old not in new and old["dist"] < 0.06 and self.prev is not None:
                c = self.prev[1]
                if self.width and abs(old["x"] - c) <= self.width / 2:
                    self.notes_caught += 1
                else:
                    self.notes_missed += 1
        self.notes = new

    # -- main step ------------------------------------------------------
    def step(self, det, now):
        if self.t0 is None:
            self.t0 = now
        bl, br = det["bar"]
        c = (bl + br) / 2
        w = br - bl
        self.width = w if self.width is None else 0.8 * self.width + 0.2 * w
        fish = det["fish"] if det["fish"] is not None else self.last_fish
        if fish is None:
            fish = c
        locked = (now - self.t0) < self.opt("lock_seconds")

        if self.prev is not None:
            dt = now - self.prev[0]
            if dt > 0:
                v_new = (c - self.prev[1]) / dt
                at_wall = bl <= 0.01 or br >= 0.99
                hist = self._hist
                hist.append((now, c, at_wall))
                if len(hist) > 3:
                    hist.pop(0)
                if not locked and len(hist) == 3 and not any(h[2] for h in hist):
                    (t0, x0, _), (t1, x1, _), (t2, x2, _) = hist
                    d0, d1 = t1 - t0, t2 - t1
                    if 0.004 < d0 < 0.08 and 0.004 < d1 < 0.08:
                        v_mid = (x2 - x0) / (d0 + d1)
                        acc = 2 * ((x2 - x1) / d1 - (x1 - x0) / d0) / (d0 + d1)
                        # only learn when the input moving the bar (sent `latency` earlier) was the
                        # same over the whole window, so timing errors can't mislabel samples
                        u = self._input_const(t0 - 0.03, t2 + 0.03)
                        if u is not None:
                            self.model.learn(v_mid, acc, u)
                self.vel = 0.4 * self.vel + 0.6 * v_new
                if self.last_fish is not None:
                    self.fish_vel = 0.6 * self.fish_vel + 0.4 * (fish - self.last_fish) / dt
        self.last_fish = fish
        if not locked and not (bl <= fish <= br):
            self.frames_out += 1
        if not locked and any(bl <= a <= br for a in det.get("avoid", [])):
            self.frames_avoid_in += 1
        if self.profile == "pinion":
            self._track_notes(det.get("notes", []), now)

        self.hold = self._pursue(c, fish, det, now)
        self.prev = (now, c, self.hold)
        self._inputs.append((now, self.hold))
        if len(self._inputs) > 120:
            self._inputs = self._inputs[-120:]
        return self.hold

    def _input_const(self, ta, tb):
        """The input in effect over [ta, tb] (shifted by latency), or None if it changed."""
        lat = self.opt("input_latency")
        ta, tb = ta - lat, tb - lat
        state = None
        for ts, h in self._inputs:
            if ts <= ta:
                state = h
            elif ts <= tb:
                if state is not None and h != state:
                    return None
                state = h if state is None else state
            else:
                break
        return state

    def _input_at(self, t):
        t -= self.opt("input_latency")
        held = False
        for ts, h in self._inputs:
            if ts <= t:
                held = h
            else:
                break
        return held

    # -- target -------------------------------------------------------------
    def _target(self, fish, det, now):
        """Where the bar's centre should be, given the rod's rules."""
        half = self.width / 2
        lat = self.opt("input_latency")
        # lead the fish a little in the direction it's moving
        tgt = fish + self.fish_vel * self.opt("fish_lead")
        lo, hi = half, 1 - half
        if self.profile == "remembrance" and det.get("avoid"):
            tgt = _remembrance_target(fish, det["avoid"], half, self.opt("avoid_margin"), tgt)
        if self.profile == "pinion":
            # between notes, don't follow the fish all the way to an edge: the next note could
            # land anywhere, and missing a note is what breaks a Pinion's Aria perfect catch
            r = max(0.0, (0.5 - half) * (1 - self.opt("pinion_center_pull")))
            tgt = min(max(tgt, 0.5 - half - r + half), 0.5 + r) if r < 0.5 - half else tgt
        if self.profile == "pinion" and self.notes:
            # the next note to land decides where we must be; keep the fish in too if possible
            nm = self.opt("pinion_note_margin")
            soon = min(self.notes, key=lambda n: n["dist"] / max(n["speed"], 0.05))
            t_land = soon["dist"] / max(soon["speed"], 0.05)
            nx = soon["x"]
            m = self.model
            vmax = max(0.05, min(m.a_hold, m.a_rel) / max(m.k, 0.3))
            gap = max(0.0, abs(nx - (self.prev[1] if self.prev else 0.5)) - (half - nm) * 0.5)
            need = gap / (0.7 * vmax) + 0.25 + lat + self.opt("pinion_commit_buffer")
            if t_land < min(self.opt("plan_horizon") + lat, need):
                # keep the note in the inner part of the bar; lean toward the fish when there's room
                room = max(0.0, (half - nm) * self.opt("pinion_note_inner"))
                tgt = nx + min(max(tgt - nx, -room), room)
        return min(max(tgt, lo), hi)

    def _pursue(self, c, fish, det, now):
        """Bang-bang chase with braking: accelerate toward the target and start braking
        exactly when the learned physics says we'd stop on it."""
        m = self.model
        lat = self.opt("input_latency")
        x, v = c, self.vel
        half = self.width / 2
        for _ in range(max(0, int(round(lat / self.DT)))):    # where the bar will be when our input lands
            v += m.accel(v, self.hold) * self.DT
            x, v = _wall(x + v * self.DT, v, half)
        tgt = self._target(fish, det, now)
        self.target = tgt
        vt = self.fish_vel if self.profile != "pinion" or not self.notes else 0.0
        e = tgt - x
        vr = v - vt
        if e > 0:
            # moving right toward target: brake if our stopping distance reaches it
            stop = _stop_dist(vr, m.a_rel, m.k) if vr > 0 else 0.0
            return stop < e
        else:
            stop = _stop_dist(-vr, m.a_hold, m.k) if vr < 0 else 0.0
            return stop >= -e

    # -- planning -------------------------------------------------------
    def _plan(self, c, fish, det, now):
        m = self.model
        lat = self.opt("input_latency")
        horizon = self.opt("plan_horizon")
        n = max(6, int(horizon / self.DT))
        w = self.width
        half = w / 2
        safe = half * self.opt("safe_zone")
        avoid = det.get("avoid", [])
        am = self.opt("avoid_margin")
        nm = self.opt("pinion_note_margin")

        # the bar keeps doing what it was doing until our input lands (latency)
        x, v = c, self.vel
        for _ in range(int(lat / self.DT)):
            v += m.accel(v, self.hold) * self.DT
            x, v = _wall(x + v * self.DT, v, half)

        notes = [(nt["x"], max(0.0, nt["dist"] / max(nt["speed"], 0.05) - lat)) for nt in self.notes]

        best, best_cost = self.hold, float("inf")
        steps = list(range(0, n + 1, 3))
        for first in (True, False):
            for switch in steps:
                cost = 0.0
                xx, vv = x, v
                for i in range(n):
                    u = first if i < switch else (not first)
                    vv += m.accel(vv, u) * self.DT
                    xx, vv = _wall(xx + vv * self.DT, vv, half)
                    t = (i + 1) * self.DT
                    decay = math.exp(-t / 0.35)
                    fx = min(1.0, max(0.0, fish + self.fish_vel * 0.35 * (1 - decay)))
                    d = abs(fx - xx)
                    wt = 1.0 / (1 + 4 * t)                       # near future matters most
                    if d > half:
                        cost += 400 * wt * (d - half + 0.01)      # fish out of the bar: lost progress
                    elif d > safe:
                        cost += 20 * wt * (d - safe)
                    cost += 0.5 * wt * d * d
                    for ax in avoid:                              # Remembrance: dark fish must stay out
                        if abs(ax - xx) < half + am:
                            cost += 300 * wt
                    for nx, tl in notes:                          # Pinion: be under each note on landing
                        if abs(t - tl) < self.DT * 1.5:
                            if abs(nx - xx) > half - nm:
                                cost += 900
                u0 = first if switch > 0 else (not first)         # the input we'd send right now
                cost += 0.02 * abs(switch - 3)                    # tie-break: prefer simple plans
                if cost < best_cost:
                    best_cost, best = cost, u0
        self.target = fish
        return best


def _stop_dist(v, a, k):
    """Distance travelled while braking from speed v>0 with deceleration a and drag k."""
    if v <= 0:
        return 0.0
    a = max(a, 1e-3)
    if k < 1e-3:
        return v * v / (2 * a)
    return v / k - (a / (k * k)) * math.log1p(k * v / a)


def _remembrance_target(fish, avoid, half, margin, pref):
    """Bar centre near `pref` that keeps the white fish in and every dark fish out,
    aiming well inside the allowed zone (not on its edge, where the bar's small
    back-and-forth would keep clipping the dark fish)."""
    segs = [(fish - half * 0.85, fish + half * 0.85)]       # centres that contain the white fish
    for a in avoid:                                          # cut out centres that would contain a dark fish
        b0, b1 = a - half - margin, a + half + margin
        nxt = []
        for s0, s1 in segs:
            if b1 <= s0 or b0 >= s1:
                nxt.append((s0, s1))
                continue
            if s0 < b0:
                nxt.append((s0, b0))
            if b1 < s1:
                nxt.append((b1, s1))
        segs = nxt
    if not segs:
        return pref
    def place(seg):
        s0, s1 = seg
        pad = min(0.03, (s1 - s0) / 2)
        return min(max(pref, s0 + pad), s1 - pad)
    return min((place(sg) for sg in segs), key=lambda x: abs(x - pref))


def _wall(x, v, half):
    """The bar stops (with a small bounce) at the ends of the track."""
    if x - half < 0:
        return half, -0.2 * v
    if x + half > 1:
        return 1 - half, -0.2 * v
    return x, v
