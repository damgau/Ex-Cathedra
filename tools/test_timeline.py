#!/usr/bin/env python3
"""
Regression test for the timeline cut/mute engine (tools/timeline.py).

Pins the two operations directly — previously the engine was a private detail of
remove_silence and `cut` mode was only exercised sideways through
test_switch_angles. Builds a synthetic xmeml sequence (two video tracks + one
audio track, each one full-length clip) and checks:

  mute_spans       splits a span out, disables it in place, leaves <duration>;
  disable_span     touches ONLY the given track (the camera-switch invariant);
  ripple_cut       removes a span, shifts the remainder left, shortens <duration>;
  ripple_cut       handles multiple out-of-order spans (applied right-to-left);
  every operation  keeps clipitem ids globally unique.

Run:  python tools/test_timeline.py     (exits non-zero on any mismatch)
"""

import sys
from pathlib import Path
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).parent))
from timeline import ripple_cut, mute_spans, disable_span, all_tracks

TICKS = 10_160_640_000
TL = 1000


# ---------------------------------------------------------------------------
# Synthetic xmeml builder
# ---------------------------------------------------------------------------

def _clipitem(cid, start, end, name):
    it = ET.Element("clipitem", {"id": f"clipitem-{cid}"})
    ET.SubElement(it, "name").text = name
    ET.SubElement(it, "enabled").text = "TRUE"
    ET.SubElement(it, "duration").text = str(end - start)
    ET.SubElement(it, "start").text = str(start)
    ET.SubElement(it, "end").text = str(end)
    ET.SubElement(it, "in").text = str(start)
    ET.SubElement(it, "out").text = str(end)
    ET.SubElement(it, "pproTicksIn").text = str(start * TICKS)
    ET.SubElement(it, "pproTicksOut").text = str(end * TICKS)
    return it


def build_sequence():
    """Two video tracks (MAIN over DIV) + one audio track, each one clip 0..TL."""
    xmeml = ET.Element("xmeml", {"version": "4"})
    seq = ET.SubElement(xmeml, "sequence", {"id": "sequence-1",
                                            "MZ.WorkOutPoint": str(TL * TICKS)})
    ET.SubElement(seq, "duration").text = str(TL)
    media = ET.SubElement(seq, "media")
    video = ET.SubElement(media, "video")
    # DIV (bottom), MAIN (top)
    div_tr = ET.SubElement(video, "track"); div_tr.append(_clipitem(1, 0, TL, "DIV CAM"))
    main_tr = ET.SubElement(video, "track"); main_tr.append(_clipitem(2, 0, TL, "MAIN CAM"))
    audio = ET.SubElement(media, "audio")
    aud_tr = ET.SubElement(audio, "track"); aud_tr.append(_clipitem(3, 0, TL, "MAIN CAM"))
    return seq, main_tr, div_tr, aud_tr


def _enabled(it):
    el = it.find("enabled")
    return el is None or (el.text or "").upper() != "FALSE"


def _coverage(track, want_enabled):
    return sorted((int(i.find("start").text), int(i.find("end").text))
                  for i in track.findall("clipitem")
                  if _enabled(i) == want_enabled)


def _all_coverage(track):
    return sorted((int(i.find("start").text), int(i.find("end").text))
                  for i in track.findall("clipitem"))


def _ids(seq):
    return [i.get("id") for i in seq.iter("clipitem")]


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def main():
    failures = []

    def check(name, ok):
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures.append(name)

    # ---- mute_spans across all tracks: length-preserving disable ----
    seq, main_tr, div_tr, aud_tr = build_sequence()
    mute_spans(seq, all_tracks(seq), [(400, 600)])
    for label, tr in (("MAIN", main_tr), ("DIV", div_tr), ("audio", aud_tr)):
        check(f"mute_spans: {label} disabled == [(400, 600)]",
              _coverage(tr, False) == [(400, 600)])
        check(f"mute_spans: {label} still covers the whole timeline",
              _all_coverage(tr) == [(0, 400), (400, 600), (600, TL)])
    check("mute_spans: <duration> unchanged (no ripple)",
          seq.find("duration").text == str(TL))
    check("mute_spans: ids stay unique", len(_ids(seq)) == len(set(_ids(seq))))

    # ---- disable_span: ONLY the given track is touched (ADR-0001) ----
    seq, main_tr, div_tr, aud_tr = build_sequence()
    disable_span(seq, main_tr, (400, 600))
    check("disable_span: MAIN disabled == [(400, 600)]",
          _coverage(main_tr, False) == [(400, 600)])
    check("disable_span: DIV untouched (fully enabled, one clip)",
          _coverage(div_tr, True) == [(0, TL)] and _coverage(div_tr, False) == [])
    check("disable_span: audio untouched",
          _coverage(aud_tr, True) == [(0, TL)] and _coverage(aud_tr, False) == [])
    check("disable_span: <duration> unchanged", seq.find("duration").text == str(TL))

    # ---- ripple_cut: remove a span, shift remainder, shorten timeline ----
    seq, main_tr, div_tr, aud_tr = build_sequence()
    ripple_cut(seq, [(400, 600)])
    for label, tr in (("MAIN", main_tr), ("DIV", div_tr), ("audio", aud_tr)):
        check(f"ripple_cut: {label} coverage == [(0,400),(400,800)]",
              _all_coverage(tr) == [(0, 400), (400, 800)])
        check(f"ripple_cut: {label} has no disabled clips",
              _coverage(tr, False) == [])
    # right fragment's source in/out shifted, timeline shifted
    right = [i for i in main_tr.findall("clipitem")
             if int(i.find("start").text) == 400][0]
    check("ripple_cut: right fragment source in/out == 600/1000",
          (right.find("in").text, right.find("out").text) == ("600", "1000"))
    check("ripple_cut: <duration> recomputed to 800", seq.find("duration").text == "800")
    check("ripple_cut: MZ.WorkOutPoint recomputed", seq.get("MZ.WorkOutPoint") == str(800 * TICKS))
    check("ripple_cut: ids stay unique", len(_ids(seq)) == len(set(_ids(seq))))

    # ---- ripple_cut: multiple out-of-order spans (applied right-to-left) ----
    seq, main_tr, div_tr, aud_tr = build_sequence()
    ripple_cut(seq, [(600, 700), (200, 300)])   # 100 + 100 removed, any order
    check("ripple_cut (multi): <duration> == 800", seq.find("duration").text == "800")
    check("ripple_cut (multi): MAIN end reaches 800",
          max(int(i.find("end").text) for i in main_tr.findall("clipitem")) == 800)
    check("ripple_cut (multi): ids stay unique", len(_ids(seq)) == len(set(_ids(seq))))

    if failures:
        print(f"\n{len(failures)} test(s) FAILED.")
        sys.exit(1)
    print("\nAll timeline engine invariants hold.")


if __name__ == "__main__":
    main()
