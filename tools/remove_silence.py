#!/usr/bin/env python3
"""
Stage 3 — remove_silence.py
Detects long pauses in the clean audio and cuts or mutes them across all tracks.
Output: OUTPUT/03_silence.xml
"""

import sys
import json
import copy
import argparse
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

import numpy as np
import xml.etree.ElementTree as ET

BASE_DIR     = Path(__file__).parent.parent
OUTPUT_DIR   = BASE_DIR / "OUTPUT"

SAMPLE_RATE      = 8000      # Hz
TICKS_PER_FRAME  = 10_160_640_000
FPS              = 25


# ---------------------------------------------------------------------------
# Audio extraction
# ---------------------------------------------------------------------------

def _get_cam_clips(seq: ET.Element, cam_label: str) -> list:
    """
    Return [(timeline_start, mxf_path, source_in, timeline_duration), ...]
    for the given camera's video track, in order.
    """
    vid = seq.find("media/video")
    for track in vid.findall("track"):
        items = track.findall("clipitem")
        if not items:
            continue
        f = items[0].find("file/pathurl")
        if f is None or f.text is None:
            continue
        if cam_label.replace(" ", "%20") not in f.text:
            continue
        result = []
        for item in items:
            furl = item.find("file/pathurl")
            if furl is not None and furl.text:
                p   = Path(furl.text.replace("file://localhost", "")
                                    .replace("%20", " ").replace("%28", "(")
                                    .replace("%29", ")"))
                t_s = int(item.find("start").text)
                s_in = int(item.find("in").text)
                dur  = int(item.find("end").text) - t_s
                result.append((t_s, p, s_in, dur))
        return result
    return []


def extract_timeline_audio(cam_clips: list, channel: int) -> tuple:
    """
    Extract and concatenate audio for the clean camera's clips.
    Returns (audio_array, timeline_start_frame).
    """
    chunks = []
    for (t_start, mxf, src_in, dur) in cam_clips:
        start_secs = src_in / FPS
        dur_secs   = dur   / FPS
        cmd = [
            "ffmpeg", "-loglevel", "error",
            "-ss", str(start_secs), "-i", str(mxf),
            "-t", str(dur_secs),
            "-map", f"0:a:{channel - 1}",
            "-ar", str(SAMPLE_RATE), "-ac", "1",
            "-f", "f32le", "pipe:1",
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            print(f"  [WARN] ffmpeg error on {mxf.name}: {result.stderr[:100].decode()}")
            continue
        chunks.append(np.frombuffer(result.stdout, dtype=np.float32))
    audio = np.concatenate(chunks) if chunks else np.array([], dtype=np.float32)
    return audio, cam_clips[0][0] if cam_clips else 0


# ---------------------------------------------------------------------------
# Silence detection
# ---------------------------------------------------------------------------

def detect_silence_frames(audio: np.ndarray, timeline_offset: int,
                           threshold_db: float, min_dur_s: float,
                           padding_s: float) -> list:
    """
    Return list of (cut_start_frame, cut_end_frame) in timeline coordinates.
    """
    if audio.size == 0:
        return []

    threshold = 10 ** (threshold_db / 20)
    win = SAMPLE_RATE // 100         # 10 ms windows
    pad_wins = int(padding_s * 100)  # padding in 10ms units
    min_wins = int(min_dur_s * 100)  # minimum silence in 10ms units

    n = len(audio) // win
    if n == 0:
        return []
    frames_arr = audio[:n * win].reshape(n, win)
    rms = np.sqrt(np.mean(frames_arr ** 2, axis=1))
    is_silent = rms < threshold

    regions = []
    in_sil, sil_start = False, 0
    for i in range(len(is_silent)):
        if is_silent[i] and not in_sil:
            in_sil, sil_start = True, i
        elif not is_silent[i] and in_sil:
            length = i - sil_start
            if length >= min_wins:
                cut_s = sil_start + pad_wins
                cut_e = i - pad_wins
                if cut_e > cut_s:
                    regions.append((cut_s, cut_e))
            in_sil = False
    if in_sil:
        length = len(is_silent) - sil_start
        if length >= min_wins:
            cut_s = sil_start + pad_wins
            cut_e = len(is_silent) - pad_wins
            if cut_e > cut_s:
                regions.append((cut_s, cut_e))

    # Convert 10ms-window indices to timeline frames
    result = []
    for (ws, we) in regions:
        t_start = timeline_offset + round(ws * 0.01 * FPS)   # 10ms * 25fps
        t_end   = timeline_offset + round(we * 0.01 * FPS)
        result.append((t_start, t_end))
    return result


# ---------------------------------------------------------------------------
# XML clip manipulation helpers
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


# ---------------------------------------------------------------------------
# Cut / mute application
# ---------------------------------------------------------------------------

def _apply_one_cut(all_tracks: list, cut_start: int, cut_end: int,
                   mode: str, id_counter: int) -> int:
    """
    Apply one silence cut [cut_start, cut_end] to every clipitem in all_tracks.
    Returns updated id_counter.
    """
    cut_len = cut_end - cut_start

    for track in all_tracks:
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


def apply_cuts(seq: ET.Element, cuts: list, mode: str):
    """Apply all silence cuts to the sequence (processed last-to-first in cut mode)."""
    vid = seq.find("media/video")
    aud = seq.find("media/audio")
    all_tracks = list(vid.findall("track")) + list(aud.findall("track"))
    id_counter = _next_id(seq)

    ordered = sorted(cuts, reverse=(mode == "cut"))  # reverse for cut mode
    for (cut_start, cut_end) in ordered:
        id_counter = _apply_one_cut(all_tracks, cut_start, cut_end,
                                     mode, id_counter)

    # Update sequence duration
    if mode == "cut":
        max_end = 0
        for track in vid.findall("track"):
            for item in track.findall("clipitem"):
                end = int(item.find("end").text)
                max_end = max(max_end, end)
        seq.find("duration").text = str(max_end)
        work_ticks = max_end * TICKS_PER_FRAME
        if seq.get("MZ.WorkOutPoint"):
            seq.set("MZ.WorkOutPoint", str(work_ticks))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Stage 3: Remove silence from the synced sequence."
    )
    parser.add_argument("--input",        default=str(OUTPUT_DIR / "02_sync.xml"))
    parser.add_argument("--audio-config", default=str(OUTPUT_DIR / "audio_config.json"))
    parser.add_argument("--output",       default=str(OUTPUT_DIR / "03_silence.xml"))
    parser.add_argument("--mode",         choices=["cut", "mute"], default="cut",
                        help="cut (default): hard cuts; mute: disable segments")
    parser.add_argument("--silence-threshold",    type=float, default=-40.0,
                        metavar="dBFS", help="RMS threshold for silence (default: -40)")
    parser.add_argument("--silence-min-duration", type=float, default=1.5,
                        metavar="s",    help="Minimum silence to cut (default: 1.5s)")
    parser.add_argument("--silence-padding",      type=float, default=0.3,
                        metavar="s",    help="Handle to keep on each side (default: 0.3s)")
    args = parser.parse_args()

    input_path  = Path(args.input)
    config_path = Path(args.audio_config)
    output_path = Path(args.output)

    if not input_path.exists():
        sys.exit(f"[ERROR] Input not found: {input_path}")
    if not config_path.exists():
        sys.exit(f"[ERROR] audio_config.json not found — run sync_audios.py first")

    cfg     = json.loads(config_path.read_text())
    cam     = cfg["camera"]   # "MAIN CAM" or "DIV CAM"
    channel = cfg["channel"]  # 1-4
    print(f"[1/4] Clean audio: {cam} CH{channel}")

    tree = ET.parse(input_path)
    seq  = tree.getroot().find("sequence")

    print("[2/4] Extracting clean audio for silence analysis...")
    clips = _get_cam_clips(seq, cam)
    if not clips:
        sys.exit(f"[ERROR] No clips found for {cam} in {input_path}")
    print(f"      {len(clips)} clips, starting at frame {clips[0][0]}")
    audio, t_offset = extract_timeline_audio(clips, channel)
    print(f"      Extracted {len(audio)/SAMPLE_RATE:.1f}s of audio "
          f"(timeline offset: {t_offset} frames)")

    print(f"\n[3/4] Detecting silence "
          f"(threshold={args.silence_threshold}dBFS, "
          f"min={args.silence_min_duration}s, "
          f"padding={args.silence_padding}s)...")
    cuts = detect_silence_frames(
        audio, t_offset,
        threshold_db=args.silence_threshold,
        min_dur_s=args.silence_min_duration,
        padding_s=args.silence_padding,
    )
    total_cut_frames = sum(e - s for s, e in cuts)
    print(f"      Found {len(cuts)} silence regions "
          f"({total_cut_frames / FPS:.1f}s total cut)")
    for i, (s, e) in enumerate(cuts[:10]):
        print(f"        [{i+1}] {s/FPS:.2f}s – {e/FPS:.2f}s  "
              f"({(e-s)/FPS:.2f}s)")
    if len(cuts) > 10:
        print(f"        ... and {len(cuts) - 10} more")

    if not cuts:
        print("  No silence detected — copying input to output unchanged.")
        import shutil
        shutil.copy(input_path, output_path)
        print(f"[OK] Written: {output_path}")
        return

    print(f"\n[4/4] Applying {len(cuts)} cuts (mode={args.mode})...")
    apply_cuts(seq, cuts, mode=args.mode)

    OUTPUT_DIR.mkdir(exist_ok=True)
    tree.write(str(output_path), xml_declaration=True, encoding="UTF-8")
    print(f"[OK] Written: {output_path}")
    new_dur = int(seq.find("duration").text)
    print(f"     New sequence duration: {new_dur} frames "
          f"({new_dur / FPS / 60:.1f} min)")


if __name__ == "__main__":
    main()
