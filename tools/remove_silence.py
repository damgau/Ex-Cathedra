#!/usr/bin/env python3
"""
Stage 3 — remove_silence.py
Detects long pauses in the clean audio and cuts or mutes them across all tracks.
Output: OUTPUT/03_silence.xml
"""

import sys
import json
import argparse
import subprocess
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

import numpy as np
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).parent))
from progress import ProgressReporter           # noqa: E402
from timeline import ripple_cut, mute_spans, all_tracks  # noqa: E402
from ffbin import ffmpeg                                  # noqa: E402

BASE_DIR     = Path(__file__).parent.parent
OUTPUT_DIR   = BASE_DIR / "OUTPUT"

SAMPLE_RATE      = 8000      # Hz
FPS              = 25
P2_NS            = 'urn:schemas-Professional-Plug-in:P2:ClipMetadata:v3.1'


# ---------------------------------------------------------------------------
# P2 chain resolution
# ---------------------------------------------------------------------------

def _resolve_p2_chain(mxf: Path, source_in: int, duration: int) -> list:
    """
    Panasonic P2 spanned clips: a long recording is split across multiple MXF
    files linked via CLIP/*.XML Next/Top elements.  When an FCP XML references
    the last clip in the chain but with in/out spanning the whole recording,
    we walk the chain from Top and return per-physical-file slices:
      [(video_mxf, local_src_in, local_dur), ...]
    Falls back to [(mxf, source_in, duration)] when no chain is found.
    """
    clip_dir = mxf.parent.parent / "CLIP"
    candidates = list(clip_dir.glob(f"{mxf.stem}.XML")) + list(clip_dir.glob(f"{mxf.stem}.xml"))
    if not candidates:
        return [(mxf, source_in, duration)]

    try:
        root = ET.parse(candidates[0]).getroot()
    except Exception:
        return [(mxf, source_in, duration)]

    top_el = root.find(f'.//{{{P2_NS}}}Top/{{{P2_NS}}}ClipName')
    if top_el is None:
        return [(mxf, source_in, duration)]  # single clip, no chain

    # Walk chain from Top, collecting {name, offset, duration}
    chain = []
    name = top_el.text
    cumulative = 0
    while name:
        cxml_candidates = list(clip_dir.glob(f"{name}.XML")) + list(clip_dir.glob(f"{name}.xml"))
        if not cxml_candidates:
            break
        try:
            croot = ET.parse(cxml_candidates[0]).getroot()
        except Exception:
            break
        dur_el    = croot.find(f'.//{{{P2_NS}}}Duration')
        next_el   = croot.find(f'.//{{{P2_NS}}}Next/{{{P2_NS}}}ClipName')
        offset_el = croot.find(f'.//{{{P2_NS}}}OffsetInShot')
        clip_dur  = int(dur_el.text) if dur_el is not None else 0
        offset    = int(offset_el.text) if offset_el is not None else cumulative
        chain.append({'name': name, 'duration': clip_dur, 'offset': offset})
        cumulative = offset + clip_dur
        name = next_el.text if next_el is not None else None

    if not chain:
        return [(mxf, source_in, duration)]

    # Map global range [source_in, source_in+duration] onto physical clip files
    g_start = source_in
    g_end   = source_in + duration
    result  = []
    for clip in chain:
        O       = clip['offset']
        D       = clip['duration']
        c_start = max(g_start, O)
        c_end   = min(g_end, O + D)
        if c_end <= c_start:
            continue
        clip_mxf = mxf.parent / f"{clip['name']}{mxf.suffix}"
        result.append((clip_mxf, c_start - O, c_end - c_start))

    return result if result else [(mxf, source_in, duration)]


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
                raw = unquote(furl.text.replace("file://localhost", ""))
                # Strip leading "/" before Windows drive letter (e.g. /G:/... → G:/...)
                if len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
                    raw = raw[1:]
                p   = Path(raw)
                t_s = int(item.find("start").text)
                s_in = int(item.find("in").text)
                dur  = int(item.find("end").text) - t_s
                result.append((t_s, p, s_in, dur))
        return result
    return []


def extract_timeline_audio(cam_clips: list, channel: int, reporter=None) -> tuple:
    """
    Extract and concatenate audio for the clean camera's clips.
    Returns (audio_array, timeline_start_frame).

    P2 cameras store each channel in a separate MXF under AUDIO/:
      VIDEO/0158Q5.MXF  →  AUDIO/0158Q500.MXF (CH1), 0158Q501.MXF (CH2), …
    When a single clipitem spans a P2 chain, _resolve_p2_chain expands it
    into per-physical-file slices before extraction.

    `reporter` (optional ProgressReporter) is updated once per clip.
    """
    chunks = []
    channel_idx = channel - 1

    for ci, (t_start, mxf, src_in, dur) in enumerate(cam_clips, 1):
        sub_clips = _resolve_p2_chain(mxf, src_in, dur)
        for (sub_mxf, sub_in, sub_dur) in sub_clips:
            audio_mxf = sub_mxf.parent.parent / "AUDIO" / f"{sub_mxf.stem}{channel_idx:02d}.MXF"
            if not audio_mxf.exists():
                audio_mxf = sub_mxf.parent.parent / "AUDIO" / f"{sub_mxf.stem}{channel_idx:02d}.mxf"
            start_secs = sub_in  / FPS
            dur_secs   = sub_dur / FPS
            cmd = [
                ffmpeg(), "-loglevel", "error",
                "-ss", str(start_secs), "-i", str(audio_mxf),
                "-t", str(dur_secs),
                "-map", "0:a:0",
                "-ar", str(SAMPLE_RATE), "-ac", "1",
                "-f", "f32le", "pipe:1",
            ]
            result = subprocess.run(cmd, capture_output=True)
            if result.returncode != 0:
                print(f"  [WARN] ffmpeg error on {audio_mxf.name}: {result.stderr[:100].decode()}")
                continue
            chunks.append(np.frombuffer(result.stdout, dtype=np.float32))
        if reporter is not None:
            reporter.update(ci, message=f"extracting clip {ci}/{len(cam_clips)}")

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
    rep = ProgressReporter(output_path, total=len(clips), label="remove_silence")
    audio, t_offset = extract_timeline_audio(clips, channel, reporter=rep)
    print(f"      Extracted {len(audio)/SAMPLE_RATE:.1f}s of audio "
          f"(timeline offset: {t_offset} frames)")

    print(f"\n[3/4] Detecting silence "
          f"(threshold={args.silence_threshold}dBFS, "
          f"min={args.silence_min_duration}s, "
          f"padding={args.silence_padding}s)...")
    rep.note("detecting silence")
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
        rep.done("no silence detected")
        print(f"[OK] Written: {output_path}")
        return

    print(f"\n[4/4] Applying {len(cuts)} cuts (mode={args.mode})...")
    rep.note(f"applying {len(cuts)} cuts")
    if args.mode == "cut":
        ripple_cut(seq, cuts)
    else:
        mute_spans(seq, all_tracks(seq), cuts)

    OUTPUT_DIR.mkdir(exist_ok=True)
    tree.write(str(output_path), xml_declaration=True, encoding="UTF-8")
    rep.done(f"{len(cuts)} cuts, {total_cut_frames/FPS:.1f}s removed")
    print(f"[OK] Written: {output_path}")
    new_dur = int(seq.find("duration").text)
    print(f"     New sequence duration: {new_dur} frames "
          f"({new_dur / FPS / 60:.1f} min)")


if __name__ == "__main__":
    main()
