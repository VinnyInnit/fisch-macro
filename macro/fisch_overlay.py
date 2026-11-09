"""
Live overlay drawn on top of Roblox: shows what the macro sees and what it's aiming for.

- Click-through and always on top (works over full-screen Roblox).
- Hidden from screenshots where the OS allows it, and it only draws BELOW the reel
  track, so it can never confuse the macro's own screen reading.

macOS: uses PyObjC (installed by the launcher). Windows: uses tkinter.
`get_state()` is called ~30 times a second and returns a dict (see App.overlay_state).
"""

import sys
import threading

COLORS = {
    "bar": (0.93, 0.95, 0.96, 0.95),
    "fish": (0.24, 0.78, 0.71, 1.0),
    "target": (0.95, 0.72, 0.29, 1.0),
    "avoid": (0.94, 0.42, 0.36, 1.0),
    "note": (0.75, 0.66, 1.0, 1.0),
    "text": (1.0, 1.0, 1.0, 1.0),
    "panel": (0.05, 0.11, 0.14, 0.78),
    "track": (1.0, 1.0, 1.0, 0.25),
}


def layout(state):
    """Turn macro state into simple shapes in screen points (top-left origin).
    Everything sits in a lane just below the reel track."""
    shapes = []
    g = state.get("geom")
    if not state.get("enabled") or not g:
        return shapes
    left, right, ty, half = g["left"], g["right"], g["y"], g["half"]
    W = right - left
    lane = ty + half + 10            # top of the lane we draw in
    lh = 12                          # lane height
    X = lambda f: left + f * W
    shapes.append(("rect", left, lane, W, 2, "track"))
    det = state.get("det") or {}
    if det.get("bar"):
        b0, b1 = det["bar"]
        shapes.append(("rect", X(b0), lane + 3, X(b1) - X(b0), 5, "bar"))
    for a in det.get("avoid") or []:
        shapes.append(("tri", X(a), lane, lh, "avoid"))
    for n in state.get("notes") or []:
        shapes.append(("diamond", X(n["x"]), lane + 6, 6, "note"))
        shapes.append(("text", X(n["x"]) - 10, lane + lh + 2, f"{n['eta']:.1f}s", "note"))
    if det.get("fish") is not None:
        shapes.append(("tri", X(det["fish"]), lane, lh, "fish"))
    if state.get("target") is not None:
        shapes.append(("rect", X(state["target"]) - 1, lane - 2, 2, lh + 4, "target"))
    shapes.append(("panel", left, lane + lh + 18, 330, 22, "panel"))
    shapes.append(("text", left + 8, lane + lh + 22, state.get("text", ""), "text"))
    return shapes


# ------------------------------------------------------------------ macOS

def run_mac(get_state, stop_event, fps=30):
    import objc
    from AppKit import (NSApplication, NSWindow, NSView, NSColor, NSBezierPath, NSScreen,
                        NSFont, NSString, NSTimer, NSMakeRect, NSFontAttributeName,
                        NSForegroundColorAttributeName, NSBackingStoreBuffered)
    from Foundation import NSObject

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)                      # "accessory": no Dock icon, never steals focus

    def col(name):
        r, g, b, a = COLORS[name]
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a)

    class OverlayView(NSView):
        def isFlipped(self):                         # top-left origin, like the screen coords
            return True

        def drawRect_(self, rect):
            try:
                st = get_state()
            except Exception:
                return
            origin = st.get("origin", (0, 0))
            font = NSFont.boldSystemFontOfSize_(12)
            for sh in layout(st):
                kind = sh[0]
                if kind in ("rect", "panel"):
                    _, x, y, w, h, c = sh
                    col(c).set()
                    r = NSMakeRect(x - origin[0], y - origin[1], w, h)
                    (NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 6, 6)
                     if kind == "panel" else NSBezierPath.bezierPathWithRect_(r)).fill()
                elif kind == "tri":
                    _, x, y, h, c = sh
                    col(c).set()
                    p = NSBezierPath.bezierPath()
                    x -= origin[0]; y -= origin[1]
                    p.moveToPoint_((x, y)); p.lineToPoint_((x - h * 0.6, y + h)); p.lineToPoint_((x + h * 0.6, y + h))
                    p.closePath(); p.fill()
                elif kind == "diamond":
                    _, x, y, r_, c = sh
                    col(c).set()
                    p = NSBezierPath.bezierPath()
                    x -= origin[0]; y -= origin[1]
                    p.moveToPoint_((x, y - r_)); p.lineToPoint_((x + r_, y)); p.lineToPoint_((x, y + r_)); p.lineToPoint_((x - r_, y))
                    p.closePath(); p.fill()
                elif kind == "text":
                    _, x, y, text, c = sh
                    NSString.stringWithString_(text).drawAtPoint_withAttributes_(
                        (x - origin[0], y - origin[1]),
                        {NSFontAttributeName: font, NSForegroundColorAttributeName: col(c)})

    class Ticker(NSObject):
        def tick_(self, timer):
            if stop_event.is_set():
                app.terminate_(None)
                return
            st = get_state()
            vis = bool(st.get("enabled") and st.get("geom"))
            if vis != self.win.isVisible():
                self.win.orderFrontRegardless() if vis else self.win.orderOut_(None)
            if vis:
                self.view.setNeedsDisplay_(True)

    screen = NSScreen.screens()[0]                   # the main screen (where Roblox usually is)
    frame = screen.frame()
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(frame, 0, NSBackingStoreBuffered, False)
    win.setOpaque_(False)
    win.setBackgroundColor_(NSColor.clearColor())
    win.setIgnoresMouseEvents_(True)                 # clicks go straight through to Roblox
    win.setHasShadow_(False)
    win.setLevel_(1000)                              # above full-screen apps
    win.setCollectionBehavior_((1 << 0) | (1 << 4) | (1 << 8))   # all Spaces, stationary, full-screen aux
    try:
        win.setSharingType_(0)                       # keep it out of screenshots
    except Exception:
        pass
    view = OverlayView.alloc().initWithFrame_(((0, 0), (frame.size.width, frame.size.height)))
    win.setContentView_(view)
    ticker = Ticker.alloc().init()
    ticker.win, ticker.view = win, view
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(1.0 / fps, ticker, "tick:", None, True)
    app.run()


# ---------------------------------------------------------------- Windows

def run_windows(get_state, stop_event, fps=30):
    import ctypes
    import tkinter as tk

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    key = "#010203"                                  # this colour becomes see-through
    root.configure(bg=key)
    root.attributes("-transparentcolor", key)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{sw}x{sh}+0+0")
    canvas = tk.Canvas(root, width=sw, height=sh, bg=key, highlightthickness=0)
    canvas.pack()
    root.update_idletasks()

    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = -20, 0x80000, 0x20, 0x80, 0x08000000
    style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                        style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
    try:
        ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x11)   # exclude from screen capture
    except Exception:
        pass

    def hexcol(name):
        r, g, b, _ = COLORS[name]
        return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))

    shown = [True]

    def tick():
        if stop_event.is_set():
            root.destroy()
            return
        try:
            st = get_state()
        except Exception:
            st = {}
        vis = bool(st.get("enabled") and st.get("geom"))
        if vis != shown[0]:
            (root.deiconify() if vis else root.withdraw())
            shown[0] = vis
        canvas.delete("all")
        if vis:
            for shp in layout(st):
                kind = shp[0]
                if kind in ("rect", "panel"):
                    _, x, y, w, h, c = shp
                    canvas.create_rectangle(x, y, x + w, y + h, fill=hexcol(c), outline="")
                elif kind == "tri":
                    _, x, y, h, c = shp
                    canvas.create_polygon(x, y, x - h * 0.6, y + h, x + h * 0.6, y + h, fill=hexcol(c), outline="")
                elif kind == "diamond":
                    _, x, y, r, c = shp
                    canvas.create_polygon(x, y - r, x + r, y, x, y + r, x - r, y, fill=hexcol(c), outline="")
                elif kind == "text":
                    _, x, y, text, c = shp
                    canvas.create_text(x, y, text=text, anchor="nw", fill=hexcol(c), font=("Segoe UI", 10, "bold"))
        root.after(int(1000 / fps), tick)

    tick()
    root.mainloop()


def available():
    """(ok, reason) - whether an overlay can run on this computer."""
    if sys.platform == "darwin":
        try:
            import AppKit  # noqa: F401
            return True, ""
        except Exception:
            return False, "PyObjC is not installed (the launcher installs it)"
    if sys.platform == "win32":
        try:
            import tkinter  # noqa: F401
            return True, ""
        except Exception:
            return False, "tkinter is missing from this Python"
    return False, "overlay is only supported on Mac and Windows"


def run(get_state, stop_event):
    """Blocks; must be called on the main thread."""
    if sys.platform == "darwin":
        return run_mac(get_state, stop_event)
    if sys.platform == "win32":
        return run_windows(get_state, stop_event)
    raise RuntimeError("overlay not supported on this OS")
