#!/usr/bin/env python3
"""
Stage 6 — delete_enable_clip.py
Commits an editor-reviewed timeline into the final cut.

The editor imports OUTPUT/05_fillers.xml into Premiere, re-enables any false
positives, disables anything else they want gone (bad takes, manual trims), and
exports the reviewed timeline as OUTPUT/06_reviewed.xml. This tool ripple-deletes
every timeline span that is dead on EVERY track and writes OUTPUT/07_final.xml.

Multicam-safe by design: a span where only one video track is disabled (e.g. MAIN
CAM off so DIV CAM shows) is still covered by an enabled clip, so it is preserved.
Only spans with no enabled clip on any track are removed.

Output: OUTPUT/07_final.xml
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

import xml.etree.ElementTree as ET

# Windows consoles default to cp1252 and mangle accented text. Force UTF-8.
if getattr(sys.stdout, "encoding", "").lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# Reuse the ripple-cut engine from the shared timeline module.
sys.path.insert(0, str(Path(__file__).parent))
from timeline import ripple_cut   # noqa: E402

BASE_DIR   = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "OUTPUT"
FPS        = 25


def _is_enabled(item: ET.Element) -> bool:
    """A clipitem is enabled unless it carries <enabled>FALSE</enabled>."""
    el = item.find("enabled")
    return el is None or (el.text or "").strip().upper() != "FALSE"


def _all_tracks(seq: ET.Element) -> list:
    tracks = []
    for kind in ("video", "audio"):
        media = seq.find(f"media/{kind}")
        if media is not None:
            tracks.extend(media.findall("track"))
    return tracks


def _merge(intervals: list) -> list:
    """Merge overlapping / touching [start, end) intervals."""
    if not intervals:
        return []
    intervals = sorted(intervals)
    merged = [list(intervals[0])]
    for s, e in intervals[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def removable_spans(seq: ET.Element) -> tuple:
    """
    Compute the timeline spans to ripple-delete: the complement of the union of
    all ENABLED clip coverage, within [0, timeline_end).

    Returns (spans, n_disabled, n_disabled_removed) for reporting.
    """
    tracks = _all_tracks(seq)

    enabled_intervals = []
    disabled_intervals = []
    timeline_end = 0
    for track in tracks:
        for item in track.findall("clipitem"):
            s = int(item.find("start").text)
            e = int(item.find("end").text)
            if e <= s:
                continue
            timeline_end = max(timeline_end, e)
            if _is_enabled(item):
                enabled_intervals.append((s, e))
            else:
                disabled_intervals.append((s, e))

    covered = _merge(enabled_intervals)

    # Complement of covered within [0, timeline_end).
    spans = []
    cursor = 0
    for s, e in covered:
        if s > cursor:
            spans.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < timeline_end:
        spans.append((cursor, timeline_end))

    # A disabled clip is "removed" if it sits entirely inside a removable span;
    # otherwise it is retained (covered by an enabled clip elsewhere = camera switch).
    n_removed = 0
    for ds, de in disabled_intervals:
        if any(s <= ds and de <= e for s, e in spans):
            n_removed += 1

    return spans, len(disabled_intervals), n_removed


def main():
    parser = argparse.ArgumentParser(
        description="Stage 6: Ripple-delete fully-disabled (dead-air) spans from a "
                    "reviewed timeline, preserving multicam camera switches."
    )
    parser.add_argument("--input",  default=str(OUTPUT_DIR / "06_reviewed.xml"))
    parser.add_argument("--output", default=str(OUTPUT_DIR / "07_final.xml"))
    args = parser.parse_args()

    input_path  = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        sys.exit(f"[ERROR] Input not found: {input_path}\n"
                 f"        Export your reviewed Premiere timeline to {input_path} first.")

    print(f"[1/3] Loading reviewed timeline: {input_path.name}")
    tree = ET.parse(input_path)
    seq  = tree.getroot().find("sequence")
    if seq is None:
        sys.exit("[ERROR] No <sequence> found — is this an FCP7/xmeml export?")

    old_dur = int(seq.find("duration").text)

    print("[2/3] Finding dead-air spans (disabled on every track)...")
    spans, n_disabled, n_removed = removable_spans(seq)
    removed_frames = sum(e - s for s, e in spans)
    print(f"      {n_disabled} disabled clipitem(s) found across all tracks")
    print(f"      {len(spans)} dead-air span(s) to remove "
          f"({removed_frames/FPS:.1f}s)")
    print(f"      {n_removed} disabled clip(s) inside dead-air → deleted; "
          f"{n_disabled - n_removed} retained (camera switches / covered elsewhere)")

    if not spans:
        print("  Nothing to remove — copying input to output unchanged.")
        import shutil
        OUTPUT_DIR.mkdir(exist_ok=True)
        shutil.copy(input_path, output_path)
        print(f"[OK] Written: {output_path}")
        return

    print("[3/3] Ripple-deleting dead-air spans across all tracks...")
    ripple_cut(seq, spans)

    OUTPUT_DIR.mkdir(exist_ok=True)
    tree.write(str(output_path), xml_declaration=True, encoding="UTF-8")
    new_dur = int(seq.find("duration").text)
    print(f"[OK] Written: {output_path}")
    print(f"     Duration: {old_dur/FPS:.1f}s ({old_dur/FPS/60:.1f} min) "
          f"→ {new_dur/FPS:.1f}s ({new_dur/FPS/60:.1f} min)")


if __name__ == "__main__":
    main()
