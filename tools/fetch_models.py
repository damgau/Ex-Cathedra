#!/usr/bin/env python3
"""
fetch_models.py — pull the (gitignored) CV model weights the pipeline needs.

The person detector for Stage 4 (MAIN speaker-presence) is MobileNet-SSD run via
cv2.dnn. The weights are ~23 MB and kept OUT of git (see .gitignore); this script
fetches them to tools/models/ on demand. detect_framing.py calls ensure_model()
so a fresh checkout downloads automatically on first run.

Run standalone:  python tools/fetch_models.py
"""

import os
import sys
import shutil
import urllib.request
from pathlib import Path

MODELS_DIR = Path(__file__).parent / "models"

# Each entry: local filename -> list of mirror URLs (tried in order).
MOBILENET_SSD = {
    "MobileNetSSD_deploy.prototxt": [
        "https://raw.githubusercontent.com/djmv/MobilNet_SSD_opencv/master/MobileNetSSD_deploy.prototxt",
        "https://raw.githubusercontent.com/chuanqi305/MobileNet-SSD/master/deploy.prototxt",
    ],
    "MobileNetSSD_deploy.caffemodel": [
        "https://github.com/djmv/MobilNet_SSD_opencv/raw/master/MobileNetSSD_deploy.caffemodel",
        "https://raw.githubusercontent.com/djmv/MobilNet_SSD_opencv/master/MobileNetSSD_deploy.caffemodel",
    ],
}


def _download(urls: list, dest: Path) -> None:
    last = None
    # Download to a sibling .part and only atomically rename it into place once
    # the body has been fully read. A mid-stream drop raises inside the `with`
    # (caught below) and never leaves a truncated file at `dest` that the size
    # check in ensure_model would mistake for a good cached model.
    tmp = dest.with_name(dest.name + ".part")
    for url in urls:
        try:
            print(f"  fetching {dest.name} <- {url}")
            with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
            if tmp.stat().st_size > 0:
                os.replace(tmp, dest)
                return
        except Exception as e:        # noqa: BLE001 - try the next mirror
            last = e
            print(f"    failed: {e}")
    try:
        tmp.unlink()
    except OSError:
        pass
    raise RuntimeError(f"Could not download {dest.name}: {last}")


def ensure_model(spec: dict = MOBILENET_SSD, dest_dir: Path = MODELS_DIR) -> dict:
    """Ensure every file in `spec` exists under dest_dir; download what's missing.
    Returns {filename: Path}."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, urls in spec.items():
        path = dest_dir / name
        if not path.exists() or path.stat().st_size == 0:
            _download(urls, path)
        out[name] = path
    return out


def mobilenet_ssd_paths() -> tuple:
    """(prototxt_path, caffemodel_path), downloading if needed."""
    m = ensure_model()
    return (str(m["MobileNetSSD_deploy.prototxt"]),
            str(m["MobileNetSSD_deploy.caffemodel"]))


if __name__ == "__main__":
    files = ensure_model()
    for n, p in files.items():
        print(f"[OK] {n}: {p.stat().st_size/1e6:.1f} MB at {p}")
    sys.exit(0)
