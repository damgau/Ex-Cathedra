#!/usr/bin/env python3
"""
Regression test for switch_angles — pins the camera-switch invariants without
needing any video.

Builds a synthetic xmeml sequence (V1=DIV, V2=MAIN both full-length + enabled,
plus an audio track) and a synthetic MAIN-framing signal with one real no-face
gap and one sub-trigger blip, then checks:

  - exactly one DIV window is produced (the blip is ignored),
  - pre-roll + snap pull its start earlier AND onto a seeded edit point,
  - applying it disables ONLY MAIN video over that window,
  - the DIV track and the audio track are left untouched,
  - the sequence <duration> is unchanged.

Run:  python tools/test_switch_angles.py     (exits non-zero on any mismatch)
"""

import sys
from pathlib import Path
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).parent))
from switch_angles import (
    FPS, detect_div_runs, shape_windows, _find_video_track,
    _edit_points, _timeline_end, _apply_one_cut, _next_id,
)

TICKS = 10_160_640_000
TL_END = 7500          # 300 s @ 25 fps
EDIT_PT = 3000         # interior MAIN cut (a "pause")


# ---------------------------------------------------------------------------
# Synthetic xmeml builder
# ---------------------------------------------------------------------------

def _clipitem(cid, start, end, cam):
    it = ET.Element("clipitem", {"id": f"clipitem-{cid}"})
    ET.SubElement(it, "name").text = cam
    ET.SubElement(it, "enabled").text = "TRUE"
    ET.SubElement(it, "duration").text = str(end - start)
    ET.SubElement(it, "start").text = str(start)
    ET.SubElement(it, "end").text = str(end)
    ET.SubElement(it, "in").text = str(start)
    ET.SubElement(it, "out").text = str(end)
    ET.SubElement(it, "pproTicksIn").text = str(start * TICKS)
    ET.SubElement(it, "pproTicksOut").text = str(end * TICKS)
    f = ET.SubElement(it, "file", {"id": "file-1"})
    ET.SubElement(f, "pathurl").text = (
        f"file://localhost/G:/proj/INPUT/{cam.replace(' ', '%20')}/VIDEO/X.MXF"
    )
    return it


def build_sequence():
    xmeml = ET.Element("xmeml", {"version": "4"})
    seq = ET.SubElement(xmeml, "sequence", {"id": "sequence-1"})
    ET.SubElement(seq, "duration").text = str(TL_END)
    media = ET.SubElement(seq, "media")
    video = ET.SubElement(media, "video")

    # V1 = DIV (bottom), single full-length clip
    div_tr = ET.SubElement(video, "track")
    div_tr.append(_clipitem(10, 0, TL_END, "DIV CAM"))

    # V2 = MAIN (top), split at EDIT_PT to seed an interior edit point/pause
    main_tr = ET.SubElement(video, "track")
    main_tr.append(_clipitem(20, 0, EDIT_PT, "MAIN CAM"))
    main_tr.append(_clipitem(21, EDIT_PT, TL_END, "MAIN CAM"))

    # one audio track, full length
    audio = ET.SubElement(media, "audio")
    aud_tr = ET.SubElement(audio, "track")
    aud_tr.append(_clipitem(30, 0, TL_END, "MAIN CAM"))

    return seq


def _enabled(it):
    el = it.find("enabled")
    return el is None or (el.text or "").upper() != "FALSE"


def _coverage(track, want_enabled):
    return sorted((int(i.find("start").text), int(i.find("end").text))
                  for i in track.findall("clipitem")
                  if _enabled(i) == want_enabled)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def main():
    seq = build_sequence()

    # Framing samples @ 4 fps (every 6 frames). Face present except:
    #   - real gap [3012, 4500)  -> should switch to DIV
    #   - blip     [1000, 1018)  -> below trigger, must be ignored
    samples = []
    for f in range(0, TL_END, 6):
        present = not ((3010 <= f < 4500) or (1000 <= f < 1018))
        samples.append({"timeline_frame": f, "face_present": present})

    runs = detect_div_runs(samples, TL_END,
                           trigger_frames=round(1.0 * FPS),
                           return_frames=round(1.0 * FPS))
    windows = shape_windows(
        runs, _edit_points(_find_video_track(seq, "MAIN CAM")), TL_END,
        pre_frames=round(1.0 * FPS), post_frames=round(0.75 * FPS),
        snap_tol=round(0.75 * FPS), min_frames=round(1.0 * FPS),
    )

    failures = []

    def check(name, ok):
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)

    check("one DIV window (blip ignored)", len(windows) == 1)
    if windows:
        s, e = windows[0]
        natural_start = runs[0][0] if runs else -1
        check(f"start snapped to edit point {EDIT_PT} (got {s})", s == EDIT_PT)
        check(f"pre-roll+snap moved start earlier than natural {natural_start} (got {s})",
              s < natural_start)
        check("no window covers the sub-trigger blip @1000",
              not (s <= 1000 < e))

    # Apply to the MAIN track and check XML invariants
    main_tr = _find_video_track(seq, "MAIN CAM")
    div_tr  = _find_video_track(seq, "DIV CAM")
    div_before = _coverage(div_tr, True)
    aud_tr = seq.find("media/audio/track")
    aud_before = _coverage(aud_tr, True)

    id_counter = _next_id(seq)
    for (s, e) in windows:
        id_counter = _apply_one_cut([main_tr], s, e, mode="mute", id_counter=id_counter)

    disabled = _coverage(main_tr, False)
    check(f"MAIN disabled coverage == [{windows[0]}] (got {disabled})",
          disabled == [windows[0]])
    check("DIV track untouched (still fully enabled)",
          _coverage(div_tr, True) == div_before and _coverage(div_tr, False) == [])
    check("audio track untouched",
          _coverage(aud_tr, True) == aud_before and _coverage(aud_tr, False) == [])
    check(f"timeline duration unchanged ({TL_END})", _timeline_end(seq) == TL_END)
    check("every frame still covered by an enabled clip (multicam-safe)",
          _coverage(div_tr, True) == [(0, TL_END)])

    if failures:
        print(f"\n{len(failures)} test(s) FAILED.")
        sys.exit(1)
    print("\nAll switch_angles invariants hold.")


if __name__ == "__main__":
    main()
