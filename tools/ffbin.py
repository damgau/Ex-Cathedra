"""
Tool: ffbin.py
Purpose: Resolve the ffmpeg / ffprobe binaries once, robustly, so the pipeline
         stages that shell out to them work without manual PATH surgery.
Inputs:  Optional FFMPEG_BIN / FFPROBE_BIN in .env (full path overrides).
Outputs: ffmpeg() / ffprobe() -> absolute path to the binary (cached).

Resolution order (per binary):
  1. .env override (FFMPEG_BIN / FFPROBE_BIN)
  2. already on PATH (shutil.which)
  3. winget default install location (version-agnostic glob)
  4. give up with a guided error

Resolution is lazy + cached so --help and unit tests don't fail on a machine
without ffmpeg; the error only fires the first time a binary is actually needed.
"""

import os
import shutil
import sys
from functools import lru_cache
from glob import glob
from pathlib import Path


def _resolve(name: str) -> str:
    # 1. explicit override from .env
    env = os.environ.get(f"{name.upper()}_BIN")
    if env and Path(env).exists():
        return env

    # 2. already on PATH
    hit = shutil.which(name)
    if hit:
        return hit

    # 3. winget default install (version-agnostic — the version is globbed)
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        pattern = str(
            Path(local) / "Microsoft" / "WinGet" / "Packages" / "Gyan.FFmpeg*"
            / "ffmpeg-*-full_build" / "bin" / f"{name}.exe"
        )
        hits = sorted(glob(pattern))
        if hits:
            return hits[-1]            # newest installed version

    # 4. guided error
    sys.exit(
        f"[ERROR] {name} not found. Install it (winget install Gyan.FFmpeg) "
        f"or set {name.upper()}_BIN in .env to its full path."
    )


@lru_cache(maxsize=None)
def ffmpeg() -> str:
    return _resolve("ffmpeg")


@lru_cache(maxsize=None)
def ffprobe() -> str:
    return _resolve("ffprobe")


if __name__ == "__main__":
    # Quick self-check: print what we resolved.
    print("ffmpeg :", ffmpeg())
    print("ffprobe:", ffprobe())
