#!/usr/bin/env python3
"""
timeline.py — the cut / mute engine over an FCP/xmeml <sequence>.

Two operations, split by behaviour (see CONTEXT.md → Editing operations):

  ripple_cut(seq, spans)          Remove spans and shift everything after them
                                  left; the timeline SHORTENS (recomputes
                                  <duration> + MZ.WorkOutPoint).
  mute_spans(seq, tracks, spans)  Split spans out and disable them in place on
                                  the given tracks; timeline length UNCHANGED.
  disable_span(seq, track, span)  Sugar for muting one span on one track. This
                                  is how a camera switch is applied — only MAIN
                                  video is ever disabled (ADR-0001).

Clipitem ids are allocated globally from `seq` and hidden from callers. xmeml
<linkclipref> requires ids unique across the whole sequence, so the mute
operations take `seq` even though they only write to a subset of its tracks
(an ElementTree <track> can't reach its <sequence> — no parent pointers).

Spans are (start_frame, end_frame) and may be passed in any order: ripple_cut
applies them right-to-left so earlier coordinates stay valid; mute is
order-independent.

Extracted from remove_silence.py (Stage 3), which had become the de-facto host
that switch_angles imported its private cut helpers from.
"""

import copy
import xml.etree.ElementTree as ET

TICKS_PER_FRAME = 10_160_640_000   # Premiere internal ticks at 25 fps NDF


# ---------------------------------------------------------------------------
# Track selection
# ---------------------------------------------------------------------------

def all_tracks(seq: ET.Element) -> list:
    """Every video + audio <track> in the sequence, in document order."""
    tracks = []
    for kind in ("video", "audio"):
        media = seq.find(f"media/{kind}")
        if media is not None:
            tracks.extend(media.findall("track"))
    return tracks


# ---------------------------------------------------------------------------
# Clipitem helpers (internal)
# ---------------------------------------------------------------------------

def _set_clip_endpoints(item: ET.Element, start: int, end: int,
                        in_: int, out_: int):
    dur = out_ - in_
    item.find("start").text        = str(start)
    item.find("end").text          = str(end)
    item.find("in").text           = str(in_)
    item.find("out").text          = str(out_)
    item.find("duration").text     = str(dur)
    item.find("pproTicksIn").text  = str(in_  * TICKS_PER_FRAME)
    item.find("pproTicksOut").text = str(out_ * TICKS_PER_FRAME)


def _next_id(seq: ET.Element) -> int:
    """Find the highest existing clipitem-N id and return N+1."""
    max_id = 0
    for item in seq.iter("clipitem"):
        attr = item.get("id", "")
        if attr.startswith("clipitem-"):
            try:
                max_id = max(max_id, int(attr.split("-")[1]))
            except (ValueError, IndexError):
                pass
    return max_id + 1


def _clone(item: ET.Element, new_id: int) -> ET.Element:
    cloned = copy.deepcopy(item)
    cloned.set("id", f"clipitem-{new_id}")
    return cloned


def _apply_one_cut(tracks: list, cut_start: int, cut_end: int,
                   mode: str, id_counter: int) -> int:
    """
    Apply one span [cut_start, cut_end] to every clipitem in `tracks`.
    mode="cut" ripples (shifts later items left); mode="mute" disables in place.
    Returns the updated id_counter.
    """
    cut_len = cut_end - cut_start

    for track in tracks:
        to_add    = []
        to_remove = []

        for item in list(track.findall("clipitem")):
            s  = int(item.find("start").text)
            e  = int(item.find("end").text)
            ci = int(item.find("in").text)
            co = int(item.find("out").text)

            # --- entirely before cut → unchanged ---
            if e <= cut_start:
                pass

            # --- entirely after cut → shift (cut mode only) ---
            elif s >= cut_end:
                if mode == "cut":
                    item.find("start").text = str(s - cut_len)
                    item.find("end").text   = str(e - cut_len)

            # --- entirely inside cut → remove / disable ---
            elif s >= cut_start and e <= cut_end:
                if mode == "cut":
                    to_remove.append(item)
                else:
                    item.find("enabled").text = "FALSE"

            # --- cut completely inside clip → split ---
            elif s < cut_start and e > cut_end:
                left_out  = ci + (cut_start - s)
                right_in  = ci + (cut_end   - s)

                if mode == "cut":
                    # Left stays, right shifts left by cut_len
                    _set_clip_endpoints(item, s, cut_start, ci, left_out)
                    right = _clone(item, id_counter); id_counter += 1
                    _set_clip_endpoints(right, cut_start, e - cut_len, right_in, co)
                    to_add.append(right)
                else:  # mute
                    _set_clip_endpoints(item, s, cut_start, ci, left_out)
                    mid = _clone(item, id_counter); id_counter += 1
                    _set_clip_endpoints(mid, cut_start, cut_end, left_out, right_in)
                    mid.find("enabled").text = "FALSE"
                    to_add.append(mid)
                    right = _clone(item, id_counter); id_counter += 1
                    _set_clip_endpoints(right, cut_end, e, right_in, co)
                    to_add.append(right)

            # --- cut overlaps clip end (s < cut_start < e <= cut_end) ---
            elif s < cut_start and e <= cut_end:
                new_out = ci + (cut_start - s)
                if mode == "cut":
                    _set_clip_endpoints(item, s, cut_start, ci, new_out)
                else:
                    _set_clip_endpoints(item, s, cut_start, ci, new_out)
                    disabled = _clone(item, id_counter); id_counter += 1
                    _set_clip_endpoints(disabled, cut_start, e, new_out, co)
                    disabled.find("enabled").text = "FALSE"
                    to_add.append(disabled)

            # --- cut overlaps clip start (cut_start <= s < cut_end < e) ---
            else:  # s >= cut_start (but s < cut_end) and e > cut_end
                new_in = ci + (cut_end - s)
                if mode == "cut":
                    _set_clip_endpoints(item, cut_start, e - cut_len, new_in, co)
                else:
                    disabled = _clone(item, id_counter); id_counter += 1
                    _set_clip_endpoints(disabled, s, cut_end, ci, new_in)
                    disabled.find("enabled").text = "FALSE"
                    to_add.append(disabled)
                    _set_clip_endpoints(item, cut_end, e, new_in, co)

        for item in to_remove:
            track.remove(item)
        for item in to_add:
            track.append(item)

        # Re-sort clipitems by start time
        items_sorted = sorted(track.findall("clipitem"),
                               key=lambda x: int(x.find("start").text))
        for item in track.findall("clipitem"):
            track.remove(item)
        for item in items_sorted:
            track.append(item)

    return id_counter


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------

def _clean_spans(spans) -> list:
    """Coerce to int (start, end) pairs and drop empty/inverted spans."""
    return [(int(s), int(e)) for s, e in spans if int(e) > int(s)]


def ripple_cut(seq: ET.Element, spans) -> None:
    """Ripple-delete `spans` across all tracks; the timeline shortens.

    Applied right-to-left so each span's coordinates stay valid as earlier ones
    collapse. Recomputes <duration> (and MZ.WorkOutPoint, if present) from the
    video tracks afterwards.
    """
    spans = _clean_spans(spans)
    if not spans:
        return

    tracks = all_tracks(seq)
    id_counter = _next_id(seq)
    for (cut_start, cut_end) in sorted(spans, reverse=True):
        id_counter = _apply_one_cut(tracks, cut_start, cut_end, "cut", id_counter)

    vid = seq.find("media/video")
    max_end = 0
    for track in vid.findall("track"):
        for item in track.findall("clipitem"):
            max_end = max(max_end, int(item.find("end").text))
    dur_el = seq.find("duration")
    if dur_el is not None:
        dur_el.text = str(max_end)
    if seq.get("MZ.WorkOutPoint"):
        seq.set("MZ.WorkOutPoint", str(max_end * TICKS_PER_FRAME))


def mute_spans(seq: ET.Element, tracks, spans) -> None:
    """Disable `spans` in place on `tracks` (split + <enabled>FALSE</enabled>).

    Length-preserving: no ripple, <duration> untouched. `seq` is needed only to
    allocate globally-unique clipitem ids for the split fragments.
    """
    spans = _clean_spans(spans)
    if not spans:
        return

    tracks = list(tracks)
    id_counter = _next_id(seq)
    for (cut_start, cut_end) in spans:
        id_counter = _apply_one_cut(tracks, cut_start, cut_end, "mute", id_counter)


def disable_span(seq: ET.Element, track: ET.Element, span) -> None:
    """Mute one span on one track — how a camera switch reveals DIV by disabling
    only MAIN's video clipitem (ADR-0001)."""
    mute_spans(seq, [track], [span])
