"""
The app's own window (instead of a browser tab), using the system's built-in web view:
Safari's engine (WebKit) on Mac, Edge's (WebView2) on Windows, via pywebview.

The window owns the main thread's event loop, so the overlay over Roblox is attached to it:
on Mac it's built on the same Cocoa loop, on Windows it runs its own Tk loop in a thread.
Closing the window quits the app. If the window can't be shown, run() returns False and
the caller falls back to the browser tab.
"""

import sys
import threading
import time

import fisch_overlay


def available():
    try:
        import webview  # noqa: F401
        return True
    except Exception:
        return False


def run(url, app, overlay=True, log=print):
    """Blocks until the window is closed (True) or returns False if it couldn't open."""
    try:
        import webview
    except Exception as e:
        log(f"App window unavailable ({e!r}); using your browser instead")
        return False
    opened = threading.Event()
    try:
        win = webview.create_window("Fisch Macro", url, width=1000, height=880, min_size=(640, 520),
                                    background_color="#0e1116", text_select=True)
    except Exception as e:
        log(f"App window unavailable ({e!r}); using your browser instead")
        return False
    app.window = win

    def on_shown():
        opened.set()

    try:
        win.events.shown += on_shown
    except Exception:
        opened.set()

    def started():
        # runs on a helper thread once the window's event loop is going
        if overlay:
            try:
                if sys.platform == "darwin":
                    from PyObjCTools import AppHelper
                    AppHelper.callAfter(fisch_overlay.build_mac, app.overlay_state, app.stop_event, 30, False)
                elif sys.platform == "win32":
                    threading.Thread(target=fisch_overlay.run_windows, args=(app.overlay_state, app.stop_event),
                                     daemon=True).start()
            except Exception as e:
                log(f"Overlay unavailable: {e!r}")
        while not app.stop_event.is_set():      # Quit button pressed in the page -> close the window
            time.sleep(0.2)
        try:
            win.destroy()
        except Exception:
            pass

    try:
        webview.start(started)
    except Exception as e:
        if not opened.is_set():
            log(f"App window couldn't start ({e!r}); using your browser instead")
            app.window = None
            return False
        raise
    app.window = None
    return True


def focus(app):
    """Bring the window to the front (someone opened the app again)."""
    w = getattr(app, "window", None)
    if w is None:
        return False
    for m in ("restore", "show"):
        try:
            getattr(w, m)()
        except Exception:
            pass
    return True
