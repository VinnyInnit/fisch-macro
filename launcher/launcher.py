"""
Fisch Macro app launcher (this is what the .app / .exe runs).

The app itself rarely changes; the macro code ("payload") updates itself:
  1. If Fisch Macro is already running, just open its window and quit.
  2. Pick the newest payload: the one built into the app, or one downloaded earlier.
  3. Ask GitHub for the latest release's latest.json. If it's newer, download the payload,
     check its SHA-256 and its Ed25519 signature against the key built into this app, and
     unpack it. Anything that doesn't check out is thrown away.
  4. Run it. If a downloaded payload fails to start, mark it bad and run the built-in one.
Settings live in the user's data folder, so updates never reset them.
"""

import hashlib
import io
import ssl
import json
import os
import shutil
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
import zipfile

import ed25519_verify
from app_info import APP_NAME, PUBLIC_KEY_HEX, REPO, SHELL_VERSION

UA = {"User-Agent": f"{APP_NAME}/{SHELL_VERSION}"}
try:                                    # frozen Python on Mac doesn't see the system certificates
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL_CTX = ssl.create_default_context()


def data_dir():
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    else:
        base = os.path.expanduser("~/.local/share")
    d = os.path.join(base, "FischMacro")
    os.makedirs(d, exist_ok=True)
    return d


DATA = data_dir()
PAYLOADS = os.path.join(DATA, "payload")
STATE = os.path.join(DATA, "launcher.json")
LOG = os.path.join(DATA, "launcher.log")


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def vtuple(v):
    try:
        return tuple(int(x) for x in str(v).strip().lstrip("v").split("."))
    except ValueError:
        return (0,)


def bundled_dir():
    base = getattr(sys, "_MEIPASS", None) or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    for cand in (os.path.join(base, "payload"), os.path.join(base, "macro")):
        if os.path.exists(os.path.join(cand, "app.py")):
            return cand
    raise SystemExit("The macro files are missing from this app.")


def read_version(d):
    try:
        with open(os.path.join(d, "VERSION")) as f:
            return f.read().strip()
    except OSError:
        return "0"


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, STATE)


def already_running():
    """If another copy is serving the window, open it and return True."""
    for port in range(8765, 8775):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=0.4) as r:
                if r.status == 200 and b'"phase"' in r.read(400):
                    webbrowser.open(f"http://127.0.0.1:{port}/")
                    return True
        except Exception:
            continue
    return False


# ------------------------------------------------------------------ update

def fetch(url, timeout=6, limit=50 * 1024 * 1024):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("download too large")
    return data


def safe_extract(zf, dest):
    root = os.path.realpath(dest)
    for m in zf.infolist():
        target = os.path.realpath(os.path.join(dest, m.filename))
        if not (target == root or target.startswith(root + os.sep)):
            raise ValueError(f"unsafe path in update: {m.filename}")
    zf.extractall(dest)


def check_update(current_version, st):
    """Download and install a newer payload if there is one. Returns a note for the window."""
    if not REPO:
        return ""
    try:
        meta = json.loads(fetch(f"https://github.com/{REPO}/releases/latest/download/latest.json", timeout=5))
    except Exception as e:
        log(f"update check skipped: {e!r}")
        return ""
    ver = str(meta.get("version", "0"))
    if vtuple(ver) <= vtuple(current_version) or ver in st.get("bad", []):
        return ""
    if int(meta.get("min_shell", 1)) > SHELL_VERSION:
        return (f"Version {ver} is out, but it needs the new app download: get it from "
                f"https://github.com/{REPO}/releases/latest")
    try:
        blob = fetch(f"https://github.com/{REPO}/releases/download/v{ver}/{meta['payload']}")
        digest = hashlib.sha256(blob).hexdigest()
        if digest != meta.get("sha256"):
            raise ValueError("checksum mismatch")
        sig = bytes.fromhex(meta.get("signature", ""))
        if not ed25519_verify.verify(bytes.fromhex(PUBLIC_KEY_HEX), digest.encode(), sig):
            raise ValueError("signature doesn't match this app's key")
        dest = os.path.join(PAYLOADS, ver)
        tmp = dest + ".partial"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            safe_extract(zf, tmp)
        if not os.path.exists(os.path.join(tmp, "app.py")) or read_version(tmp) != ver:
            raise ValueError("update package is incomplete")
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(tmp, dest)
        st["installed"] = ver
        save_state(st)
        log(f"updated to {ver}")
        notes = str(meta.get("notes", "")).strip()
        return f"Updated to v{ver}." + (f" {notes}" if notes else "")
    except Exception as e:
        log(f"update to {ver} failed: {e!r}")
        return ""


def choose_payload(st):
    """(dir, version, downloaded?) of the newest usable payload."""
    b = bundled_dir()
    best = (b, read_version(b), False)
    inst = st.get("installed")
    if inst and inst not in st.get("bad", []):
        d = os.path.join(PAYLOADS, inst)
        if os.path.exists(os.path.join(d, "app.py")) and vtuple(inst) > vtuple(best[1]):
            best = (d, inst, True)
    return best


def prune(keep):
    """Remove old downloaded payloads (keep the one in use)."""
    try:
        for name in os.listdir(PAYLOADS):
            if name != keep:
                shutil.rmtree(os.path.join(PAYLOADS, name), ignore_errors=True)
    except OSError:
        pass


def run_payload(d):
    sys.path.insert(0, d)
    os.environ["FISCH_DATA_DIR"] = DATA
    os.environ["FISCH_HOST_NAME"] = APP_NAME
    import app                  # noqa: E402  (the macro's own entry point)
    app.main()


def _bundle_hint():          # never called: makes the app builder pack every module the macro uses
    import bundle_imports    # noqa: F401


def selftest():
    """Used by the release build: can the built app load the macro and everything it needs?"""
    out = os.environ.get("FISCH_SELFTEST_OUT") or os.path.join(DATA, "selftest.txt")
    try:
        d = bundled_dir()
        sys.path.insert(0, d)
        os.environ["FISCH_DATA_DIR"] = DATA
        import importlib
        mods = ["app", "fisch_macro", "fisch_reel", "fisch_auto", "fisch_overlay", "fisch_platform",
                "fisch_sck", "numpy", "mss", "mss.tools", "pynput.mouse", "pynput.keyboard", "certifi"]
        if sys.platform == "darwin":
            mods += ["objc", "AppKit", "Quartz", "Vision", "ScreenCaptureKit", "CoreMedia", "dispatch"]
        if sys.platform == "win32":
            mods += ["tkinter"]
        for m in mods:
            importlib.import_module(m)
        import fisch_sck
        info = f"OK payload {read_version(d)} | sck {fisch_sck.available()} | ssl {SSL_CTX is not None}"
        code = 0
    except Exception:
        info, code = "FAIL\n" + traceback.format_exc(), 1
    with open(out, "w") as f:
        f.write(info + "\n")
    print(info, flush=True)
    sys.exit(code)


def main():
    if "--selftest" in sys.argv:
        selftest()
    if already_running():
        return
    os.makedirs(PAYLOADS, exist_ok=True)
    st = load_state()
    d, ver, downloaded = choose_payload(st)
    note = ""
    if "--no-update" not in sys.argv:
        note = check_update(ver, st)
        if note.startswith("Updated"):
            d, ver, downloaded = choose_payload(st)
    os.environ["FISCH_UPDATE_NOTE"] = note
    log(f"starting payload {ver} from {'download' if downloaded else 'app'}")
    if downloaded:
        prune(ver)
    t_start = time.time()
    try:
        run_payload(d)
    except SystemExit:
        raise
    except Exception:
        log("payload crashed:\n" + traceback.format_exc())
        if not downloaded or time.time() - t_start > 30:
            raise                      # only a failure right at start-up means "bad update"
        # a bad update must never brick the app: remember it and fall back to the built-in one
        st.setdefault("bad", []).append(ver)
        st.pop("installed", None)
        save_state(st)
        for m in [m for m in sys.modules if m in ("app",) or m.startswith("fisch_")]:
            del sys.modules[m]
        sys.path.remove(d)
        os.environ["FISCH_UPDATE_NOTE"] = f"Update v{ver} didn't start, so the built-in version is running."
        run_payload(bundled_dir())


if __name__ == "__main__":
    main()
