#!/usr/bin/env python3
"""
Stage 5 — switch_angles.py
Content-aware camera switching. Reads the silence-trimmed timeline plus the
MAIN speaker-presence signal from detect_framing.py and produces a multicam cut
that stays on MAIN and hard-cuts to the DIV (safety) angle only where the speaker
is OUT of MAIN's frame — cutting ~1s early and snapping each cut to the nearest
pause. The speaker merely turning to his slides is still "present" → we stay on
MAIN (that's the whole point of switching from face- to person-detection).

Mechanic: MAIN (V2) is the top video track, so to reveal DIV (V1) for a window
[s, e] we split + DISABLE MAIN's clip across it (DIV underneath shows through).
We ONLY ever disable the MAIN video track — never DIV, never audio, never all
tracks — so delete_enable_clip.py still reads each switch as "preserve".

Output: OUTPUT/04_angles.xml  (same <duration> as the input — disable-only, no ripple)
"""

import sys
import json
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

# Windows consoles default to cp1252 and mangle accented French. Force UTF-8.
if getattr(sys.stdout, "encoding", "").lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import xml.etree.ElementTree as ET

# Reuse the single-track split+disable + id allocation from remove_silence.
sys.path.insert(0, str(Path(__file__).parent))
from remove_silence import _apply_one_cut, _next_id   # noqa: E402

BASE_DIR   = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "OUTPUT"
FPS        = 25


# ---------------------------------------------------------------------------
# Track / timeline helpers
# ---------------------------------------------------------------------------

def _find_video_track(seq: ET.Element, cam_label: str):
    """Return the <track> under media/video whose clips belong to `cam_label`
    (matched on the first clipitem's file/pathurl), or None."""
    vid = seq.find("media/video")
    needle = cam_label.replace(" ", "%20")
    for track in vid.findall("track"):
        items = track.findall("clipitem")
        if not items:
            continue
        f = items[0].find("file/pathurl")
        if f is not None and f.text and needle in f.text:
            return track
    return None


def _timeline_end(seq: ET.Element) -> int:
    dur = seq.find("duration")
    end = int(dur.text) if dur is not None and dur.text else 0
    for track in seq.iter("clipitem"):
        e = track.find("end")
        if e is not None and e.text:
            end = max(end, int(e.text))
    return end


def _edit_points(track: ET.Element) -> list:
    """Sorted unique clipitem start/end frames on a track = the Stage-3 cut points."""
    pts = set()
    for item in track.findall("clipitem"):
        for tag in ("start", "end"):
            el = item.find(tag)
            if el is not None and el.text:
                pts.add(int(el.text))
    return sorted(pts)


# ---------------------------------------------------------------------------
# Presence signal → DIV windows
# ---------------------------------------------------------------------------

def detect_div_runs(samples: list, timeline_end: int,
                    trigger_frames: int, return_frames: int) -> list:
    """
    Debounced state machine over the per-sample speaker-presence signal.

    Returns the natural DIV windows [(start_frame, end_frame), ...]:
      - enter DIV only after the speaker is absent for >= trigger_frames
        (window starts where the absence began),
      - return to MAIN only after the speaker is present for >= return_frames
        (window ends where he came back).
    This filters single dropped detections / momentary occlusions.
    """
    samples = sorted(samples, key=lambda s: s["timeline_frame"])
    runs = []
    state = "MAIN"
    pending = None          # frame where the opposite condition first appeared
    div_start = None

    for s in samples:
        f = s["timeline_frame"]
        present = bool(s["speaker_present"])

        if state == "MAIN":
            if not present:
                if pending is None:
                    pending = f
                elif f - pending >= trigger_frames:
                    state, div_start, pending = "DIV", pending, None
            else:
                pending = None
        else:  # DIV
            if present:
                if pending is None:
                    pending = f
                elif f - pending >= return_frames:
                    runs.append((div_start, pending))
                    state, div_start, pending = "MAIN", None, None
            else:
                pending = None

    if state == "DIV" and div_start is not None:
        runs.append((div_start, timeline_end))
    return runs


def _snap(value: int, points: list, tol: int) -> int:
    """Snap `value` to the nearest edit point within `tol` frames, else unchanged."""
    if not points:
        return value
    nearest = min(points, key=lambda p: abs(p - value))
    return nearest if abs(nearest - value) <= tol else value


def shape_windows(runs: list, edit_points: list, timeline_end: int,
                  pre_frames: int, post_frames: int,
                  snap_tol: int, min_frames: int) -> list:
    """Apply pre/post-roll, snap-to-pause, merge, and the min-shot floor."""
    # 1) pre/post-roll (clamped)
    rolled = []
    for s, e in runs:
        s = max(0, s - pre_frames)
        e = min(timeline_end, e + post_frames)
        if e > s:
            rolled.append([s, e])

    # 2) snap each boundary to the nearest pause
    for w in rolled:
        w[0] = _snap(w[0], edit_points, snap_tol)
        w[1] = _snap(w[1], edit_points, snap_tol)
        if w[1] <= w[0]:                       # snapped onto itself — undo the end snap
            w[1] = min(timeline_end, w[0] + 1)

    # 3) merge overlapping / touching windows
    rolled.sort()
    merged = []
    for s, e in rolled:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    # 4) min-shot: merge windows separated by a sub-min MAIN gap
    floored = []
    for w in merged:
        if floored and (w[0] - floored[-1][1]) < min_frames:
            floored[-1][1] = max(floored[-1][1], w[1])
        else:
            floored.append(w)

    # 5) absorb sub-min MAIN slivers at the head/tail
    if floored:
        if floored[0][0] < min_frames:
            floored[0][0] = 0
        if (timeline_end - floored[-1][1]) < min_frames:
            floored[-1][1] = timeline_end

    # 6) drop any DIV window still shorter than the min shot
    return [(s, e) for s, e in floored if (e - s) >= min_frames]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Stage 5: hard-cut to DIV where the speaker is out of MAIN's "
                    "frame, with pre-roll + snap-to-pause. Disable-only (MAIN video)."
    )
    parser.add_argument("--input",    default=str(OUTPUT_DIR / "03_silence.xml"))
    parser.add_argument("--presence", default=str(OUTPUT_DIR / "main_presence.json"))
    parser.add_argument("--output",  default=str(OUTPUT_DIR / "04_angles.xml"))
    # Timing defaults validated on a stress-test conference with 14 real walk-offs:
    # caught 13/14 as substantial DIV windows (>=3.2s) with zero false cuts. They
    # are MAIN-biased — on a normal (stationary-speaker) talk they fire ~never.
    parser.add_argument("--trigger", type=float, default=1.0, metavar="s",
                        help="Speaker must be absent >= this to switch to DIV (default 1.0)")
    parser.add_argument("--return", dest="return_s", type=float, default=1.0, metavar="s",
                        help="Speaker must be back >= this to switch back to MAIN (default 1.0)")
    parser.add_argument("--pre-roll", type=float, default=1.0, metavar="s",
                        help="Cut to DIV this long BEFORE he leaves frame (default 1.0)")
    parser.add_argument("--post-roll", type=float, default=0.75, metavar="s",
                        help="Hold DIV this long after he returns (default 0.75)")
    parser.add_argument("--snap-tolerance", type=float, default=0.75, metavar="s",
                        help="Snap each cut to a pause within this distance (default 0.75)")
    parser.add_argument("--min-shot", type=float, default=1.0, metavar="s",
                        help="No MAIN or DIV shot shorter than this (default 1.0)")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Skip the final confirmation.")
    args = parser.parse_args()

    input_path    = Path(args.input)
    presence_path = Path(args.presence)
    output_path   = Path(args.output)

    if not input_path.exists():
        sys.exit(f"[ERROR] Input not found: {input_path}")
    if not presence_path.exists():
        sys.exit(f"[ERROR] Presence signal not found: {presence_path} — run detect_framing.py first")

    print(f"[1/4] Loading {input_path.name} + {presence_path.name}")
    tree = ET.parse(input_path)
    seq  = tree.getroot().find("sequence")
    if seq is None:
        sys.exit("[ERROR] No <sequence> found — is this an xmeml export?")

    # Rename the deliverable: the Stage-1/2 working name "sync" rides through
    # every stage. This is the final stage, so label it for what it is.
    name_el = seq.find("name")
    if name_el is not None:
        name_el.text = "switch_angles"

    main_track = _find_video_track(seq, "MAIN CAM")
    div_track  = _find_video_track(seq, "DIV CAM")
    if main_track is None:
        sys.exit("[ERROR] Could not find the MAIN CAM video track (pathurl match).")
    if div_track is None:
        sys.exit("[ERROR] Could not find the DIV CAM video track — nothing to reveal underneath.")

    timeline_end = _timeline_end(seq)
    edit_points  = _edit_points(main_track)
    presence     = json.loads(presence_path.read_text(encoding="utf-8"))
    samples      = presence.get("samples", [])
    if not samples:
        sys.exit("[ERROR] Presence signal has no samples.")
    n_present = sum(1 for s in samples if s["speaker_present"])
    print(f"      Timeline: {timeline_end} frames ({timeline_end/FPS/60:.1f} min), "
          f"{len(edit_points)} edit points")
    print(f"      Presence: {len(samples)} samples, speaker present in "
          f"{n_present/len(samples):.0%}")

    print("\n[2/4] Detecting absent runs (trigger="
          f"{args.trigger}s, return={args.return_s}s)...")
    runs = detect_div_runs(
        samples, timeline_end,
        trigger_frames=round(args.trigger * FPS),
        return_frames=round(args.return_s * FPS),
    )
    print(f"      {len(runs)} qualified absent run(s)")

    print(f"\n[3/4] Shaping DIV windows (pre-roll={args.pre_roll}s, "
          f"post-roll={args.post_roll}s, snap<= {args.snap_tolerance}s, "
          f"min-shot={args.min_shot}s)...")
    windows = shape_windows(
        runs, edit_points, timeline_end,
        pre_frames=round(args.pre_roll * FPS),
        post_frames=round(args.post_roll * FPS),
        snap_tol=round(args.snap_tolerance * FPS),
        min_frames=round(args.min_shot * FPS),
    )
    div_frames = sum(e - s for s, e in windows)
    print(f"      {len(windows)} DIV window(s) — "
          f"{div_frames/FPS:.1f}s on DIV "
          f"({(div_frames/timeline_end if timeline_end else 0):.0%} of timeline)")
    for i, (s, e) in enumerate(windows[:15]):
        print(f"        [{i+1:2d}] {s/FPS:7.2f}s – {e/FPS:7.2f}s  ({(e-s)/FPS:.2f}s on DIV)")
    if len(windows) > 15:
        print(f"        ... and {len(windows) - 15} more")

    if not windows:
        print("\n  No camera switches — writing input through unchanged (renamed sequence only).")
        OUTPUT_DIR.mkdir(exist_ok=True)
        tree.write(str(output_path), xml_declaration=True, encoding="UTF-8")
        print(f"[OK] Written: {output_path}")
        return

    if not args.yes:
        answer = input(f"\n  Apply {len(windows)} DIV switch(es)? [Y/n]: ").strip().lower()
        if answer in ("n", "no"):
            sys.exit("[ABORT] Cancelled by user.")

    print("\n[4/4] Disabling MAIN video across DIV windows...")
    id_counter = _next_id(seq)
    for (s, e) in windows:
        id_counter = _apply_one_cut([main_track], s, e, mode="mute", id_counter=id_counter)

    OUTPUT_DIR.mkdir(exist_ok=True)
    tree.write(str(output_path), xml_declaration=True, encoding="UTF-8")
    new_end = _timeline_end(seq)
    print(f"[OK] Written: {output_path}")
    print(f"     Timeline length unchanged: {new_end} frames "
          f"(was {timeline_end}). Only MAIN video disabled; DIV + audio untouched.")
    print("     Review in Premiere; switches are preserved by delete_enable_clip.py.")


if __name__ == "__main__":
    main()
