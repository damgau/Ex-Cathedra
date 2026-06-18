#!/usr/bin/env python3
"""
watch_progress.py — print a heartbeat from a stage's progress sidecar.

Reads the JSON written by progress.ProgressReporter and prints a compact line:

  detect_framing  running   47%  9876/21000   14.3/s  elapsed 11:30  eta 12:42  | clip 3/4

Usage:
  python tools/watch_progress.py OUTPUT/main_framing.json   # main output -> .progress.json
  python tools/watch_progress.py OUTPUT/main_framing.progress.json
  python tools/watch_progress.py --latest                   # newest OUTPUT/*.progress.json
  python tools/watch_progress.py --latest --loop            # live, until phase done/failed
  python tools/watch_progress.py --latest --loop --interval 5

Exit code: 0 if the (final) phase is "done", 1 if "failed", 2 if no file found.
"""

import sys
import json
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from progress import progress_path_for   # noqa: E402

BASE_DIR   = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "OUTPUT"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


def _mmss(seconds) -> str:
    if seconds is None:
        return "--:--"
    seconds = int(round(seconds))
    return f"{seconds // 60:d}:{seconds % 60:02d}"


def _resolve(arg: str | None) -> Path | None:
    """Map a CLI arg to a .progress.json path. --latest (arg None) picks the
    most-recently-modified OUTPUT/*.progress.json."""
    if arg is None:
        cands = sorted(OUTPUT_DIR.glob("*.progress.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        return cands[0] if cands else None
    p = Path(arg)
    if p.name.endswith(".progress.json"):
        return p
    return progress_path_for(p)


def _format(d: dict) -> str:
    label = d.get("label") or "?"
    phase = d.get("phase") or "?"
    proc  = d.get("processed")
    total = d.get("total")
    pct   = d.get("pct")
    rate  = d.get("rate_per_s")
    pct_s   = f"{pct*100:4.0f}%" if pct is not None else "  --"
    count_s = f"{proc}/{total}" if total else f"{proc}"
    rate_s  = f"{rate:.1f}/s" if rate else "--/s"
    msg     = d.get("message")
    line = (f"{label:<16} {phase:<8} {pct_s}  {count_s:>13}  {rate_s:>8}  "
            f"elapsed {_mmss(d.get('elapsed_s'))}  eta {_mmss(d.get('eta_s'))}")
    if msg:
        line += f"  | {msg}"
    return line


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None  # missing or caught mid-write — caller retries


def main() -> int:
    ap = argparse.ArgumentParser(description="Heartbeat for a stage progress sidecar.")
    ap.add_argument("target", nargs="?", help="A .progress.json, a main output path, or omit with --latest")
    ap.add_argument("--latest", action="store_true", help="Use newest OUTPUT/*.progress.json")
    ap.add_argument("--loop", action="store_true", help="Keep printing until phase done/failed")
    ap.add_argument("--interval", type=float, default=3.0, help="Seconds between reads in --loop (default 3)")
    args = ap.parse_args()

    if not args.target and not args.latest:
        ap.error("give a path, or use --latest")

    path = _resolve(None if args.latest else args.target)
    if path is None:
        print("[watch] no progress file found in OUTPUT/")
        return 2

    if not args.loop:
        d = _read(path)
        if d is None:
            print(f"[watch] not found / unreadable: {path.name}")
            return 2
        print(_format(d))
        return 0 if d.get("phase") == "done" else (1 if d.get("phase") == "failed" else 0)

    # loop mode
    last = None
    while True:
        d = _read(path)
        if d is not None:
            line = _format(d)
            if line != last:
                print(line, flush=True)
                last = line
            if d.get("phase") in ("done", "failed"):
                return 0 if d["phase"] == "done" else 1
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
