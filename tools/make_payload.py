"""Build the update package for a release:
  dist/payload-<version>.zip   the macro code (everything in macro/)
  dist/latest.json             version, file name, SHA-256 and the Ed25519 signature
Also unpacks the same files into build/payload, which the app builds bundle as their
built-in copy. Needs UPDATE_SIGNING_KEY (hex private key) in the environment."""

import argparse
import hashlib
import json
import os
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "macro")
SKIP = {"fisch_config.json", "__pycache__", ".DS_Store"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--min-shell", type=int, default=1)
    ap.add_argument("--notes", default=os.environ.get("RELEASE_NOTES", ""))
    a = ap.parse_args()
    key_hex = os.environ.get("UPDATE_SIGNING_KEY", "").strip()
    if not key_hex:
        sys.exit("UPDATE_SIGNING_KEY is not set (add it under the repo's Settings > Secrets > Actions)")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(key_hex))

    version = open(os.path.join(SRC, "VERSION")).read().strip()
    os.makedirs(a.out, exist_ok=True)
    name = f"payload-{version}.zip"
    path = os.path.join(a.out, name)
    files = sorted(f for f in os.listdir(SRC) if f not in SKIP and os.path.isfile(os.path.join(SRC, f)))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            zi = zipfile.ZipInfo(f, date_time=(2020, 1, 1, 0, 0, 0))     # reproducible zip
            zi.compress_type = zipfile.ZIP_DEFLATED
            with open(os.path.join(SRC, f), "rb") as fh:
                z.writestr(zi, fh.read())
    blob = open(path, "rb").read()
    digest = hashlib.sha256(blob).hexdigest()
    sig = key.sign(digest.encode()).hex()
    meta = {"version": version, "payload": name, "sha256": digest, "signature": sig,
            "min_shell": a.min_shell, "notes": a.notes}
    with open(os.path.join(a.out, "latest.json"), "w") as f:
        json.dump(meta, f, indent=2)
    stage = os.path.join(ROOT, "build", "payload")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    with zipfile.ZipFile(path) as z:
        z.extractall(stage)
    print(f"payload {version}: {len(files)} files, sha256 {digest[:16]}..., signed")


if __name__ == "__main__":
    main()
