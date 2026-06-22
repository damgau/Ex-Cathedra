#!/usr/bin/env python3
"""
Stage 2 — sync_audios.py
Detects which camera/channel carries clean audio, cross-correlates
waveforms to find the sync offset, and shifts the later camera on
the timeline.
Output: OUTPUT/02_sync.xml, OUTPUT/audio_config.json
"""

import sys
import json
import argparse
import subprocess
import struct
from pathlib import Path
from copy import deepcopy
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

import numpy as np
from scipy.signal import correlate

import xml.etree.ElementTree as ET
ET.register_namespace("", "")

sys.path.insert(0, str(Path(__file__).parent))
from progress import ProgressReporter   # noqa: E402
from ffbin import ffmpeg                 # noqa: E402

BASE_DIR    = Path(__file__).parent.parent
OUTPUT_DIR  = BASE_DIR / "OUTPUT"

SAMPLE_RATE    = 8000       # Hz — downsample for speed
SAMPLE_SECS    = 300        # seconds of audio used for correlation
ANALYSIS_SECS  = 60         # seconds used for channel quality analysis
TICKS_PER_FRAME = 10_160_640_000
P2_NS = "urn:schemas-Professional-Plug-in:P2:ClipMetadata:v3.1"


# ---------------------------------------------------------------------------
# Audio extraction via ffmpeg
# ---------------------------------------------------------------------------

def _walk_p2_audio_chain(video_mxf: Path, channel: int) -> list[Path]:
    """
    Walk the P2 clip chain starting from the Top clip and return ordered
    audio MXF paths for the given channel (1-based).
    Each audio channel is a separate file: AUDIO/{stem}{channel-1:02d}.MXF
    """
    clip_dir  = video_mxf.parent.parent / "CLIP"
    audio_dir = video_mxf.parent.parent / "AUDIO"
    ch_suffix = f"{channel - 1:02d}"

    # Resolve the Top-of-chain name from this clip's CLIP metadata
    clip_xml = clip_dir / f"{video_mxf.stem}.XML"
    if clip_xml.exists():
        root    = ET.parse(clip_xml).getroot()
        top_el  = root.find(f".//{{{P2_NS}}}Top/{{{P2_NS}}}ClipName")
        start   = top_el.text if top_el is not None else video_mxf.stem
    else:
        start = video_mxf.stem

    # Walk forward through the chain collecting audio files
    audio_files, name = [], start
    while name:
        for ext in (".MXF", ".mxf"):
            candidate = audio_dir / f"{name}{ch_suffix}{ext}"
            if candidate.exists():
                audio_files.append(candidate)
                break
        cx = clip_dir / f"{name}.XML"
        if not cx.exists():
            break
        cr      = ET.parse(cx).getroot()
        next_el = cr.find(f".//{{{P2_NS}}}Next/{{{P2_NS}}}ClipName")
        name    = next_el.text if next_el is not None else None

    return audio_files


def _mxf_files_from_xml(xml_path: Path, cam_label: str) -> list[Path]:
    """Return ordered list of MXF paths for the given camera label ('MAIN CAM' or 'DIV CAM')."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    seq  = root.find("sequence")
    media = seq.find("media")
    vid   = media.find("video")
    tracks = vid.findall("track")

    # V1 = DIV CAM (targeted=1), V2 = MAIN CAM (targeted=0)
    # Identify by the INPUT path in pathurl
    paths = []
    for track in tracks:
        items = track.findall("clipitem")
        if not items:
            continue
        first_url = items[0].find("file/pathurl")
        if first_url is None:
            continue
        url = first_url.text
        if cam_label.replace(" ", "%20") in url:
            for item in items:
                f = item.find("file/pathurl")
                if f is not None and f.text:
                    raw = unquote(f.text.replace("file://localhost", "")).replace("\\", "/")
                    # Strip leading "/" before a Windows drive letter (/G:/… → G:/…)
                    if len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
                        raw = raw[1:]
                    paths.append(Path(raw))
            break
    return paths


def extract_audio_channel(mxf_files: list[Path], channel: int,
                           max_secs: int = SAMPLE_SECS,
                           reporter=None, phase: str = "") -> np.ndarray:
    """
    Extract `max_secs` of mono audio from `channel` (1-based) across the P2
    clip chain rooted at each video MXF, downsampled to SAMPLE_RATE Hz.
    Audio lives in AUDIO/{stem}{channel-1:02d}.MXF — never the video MXF.

    `reporter`/`phase` (optional): emit a per-file liveness note for watchers.
    """
    target_samples = max_secs * SAMPLE_RATE
    pcm_chunks = []
    collected  = 0

    for fi, video_mxf in enumerate(mxf_files, 1):
        audio_chain = _walk_p2_audio_chain(video_mxf, channel)
        for audio_mxf in audio_chain:
            if collected >= target_samples:
                break
            remaining_secs = (target_samples - collected) / SAMPLE_RATE
            cmd = [
                ffmpeg(), "-nostdin", "-loglevel", "error",
                "-i", str(audio_mxf),
                "-map", "0:a:0",
                "-ar", str(SAMPLE_RATE),
                "-ac", "1",
                "-t", str(remaining_secs),
                "-f", "f32le", "pipe:1",
            ]
            result = subprocess.run(cmd, capture_output=True)
            if result.returncode != 0:
                print(f"  [WARN] ffmpeg error on {audio_mxf.name}: {result.stderr[:120].decode()}")
                continue
            chunk = np.frombuffer(result.stdout, dtype=np.float32)
            pcm_chunks.append(chunk)
            collected += len(chunk)
        if reporter is not None and phase:
            reporter.note(f"{phase} file {fi}/{len(mxf_files)}")

    return np.concatenate(pcm_chunks) if pcm_chunks else np.array([], dtype=np.float32)


# ---------------------------------------------------------------------------
# Channel quality analysis
# ---------------------------------------------------------------------------

def channel_quality(audio: np.ndarray) -> dict:
    """Return quality metrics for a channel's audio."""
    if audio.size == 0:
        return {"rms_db": -120.0, "activity": 0.0, "dynamic_range_db": 0.0}

    rms = float(np.sqrt(np.mean(audio ** 2)))
    rms_db = 20 * np.log10(rms + 1e-9)

    # Fraction of 10ms windows above -40 dBFS (activity detector)
    win = max(1, SAMPLE_RATE // 100)
    frames = audio[:len(audio) - len(audio) % win].reshape(-1, win)
    frame_rms = np.sqrt(np.mean(frames ** 2, axis=1))
    activity = float(np.mean(frame_rms > 10 ** (-40 / 20)))

    # Dynamic range: 95th percentile minus 5th percentile of frame-level RMS
    p5, p95 = np.percentile(frame_rms, [5, 95])
    dyn = float(20 * np.log10((p95 + 1e-9) / (p5 + 1e-9)))

    return {"rms_db": round(rms_db, 1), "activity": round(activity, 3),
            "dynamic_range_db": round(dyn, 1)}


def detect_best_channel(mxf_files: list[Path], cam_name: str) -> tuple[int, list[dict]]:
    """
    Analyse all 4 channels and return (best_channel_1based, metrics_list).
    Scoring: activity × dynamic_range — penalises silent or constant-level channels.
    """
    print(f"  Analysing {cam_name} audio channels...")
    metrics = []
    for ch in range(1, 5):
        audio = extract_audio_channel(mxf_files, ch, max_secs=ANALYSIS_SECS)
        m = channel_quality(audio)
        m["channel"] = ch
        metrics.append(m)
        print(f"    CH{ch}: RMS={m['rms_db']:+.1f} dBFS  "
              f"activity={m['activity']:.0%}  "
              f"dyn_range={m['dynamic_range_db']:.1f} dB")

    def score(m):
        return m["activity"] * m["dynamic_range_db"]

    best = max(metrics, key=score)
    return best["channel"], metrics


# ---------------------------------------------------------------------------
# Cross-correlation sync
# ---------------------------------------------------------------------------

def find_sync_offset_frames(audio_a: np.ndarray, audio_b: np.ndarray) -> int:
    """
    Return how many frames `a` started BEFORE `b` (i.e. a's lead over b).

    scipy.correlate(a, b) peaks at the lag where a's content best lines up
    with b's. When `a` started recording earlier, the shared event sits at a
    LARGER offset inside a, so a's content is delayed relative to b and the
    peak lands at a positive lag.

      Positive result → a started first  → a leads b by that many frames.
      Negative result → b started first  → b leads a by abs(result) frames.

    Caller convention (see main): pass (main_audio, div_audio) so a positive
    return means MAIN started before DIV.
    """
    if audio_a.size == 0 or audio_b.size == 0:
        sys.exit("[ERROR] Audio extraction returned no data — check MXF paths and ffmpeg output above.")

    def norm(x):
        x = x - np.mean(x)
        std = np.std(x)
        return x / std if std > 0 else x

    a = norm(audio_a.astype(np.float64))
    b = norm(audio_b.astype(np.float64))

    corr = correlate(a, b, mode="full", method="fft")
    # peak at index i → k = i - (len(b)-1)
    lag_samples = int(np.argmax(corr)) - (len(b) - 1)
    return round(lag_samples * 25 / SAMPLE_RATE)


# ---------------------------------------------------------------------------
# XML mutation
# ---------------------------------------------------------------------------

def _clipitem_start_end(item: ET.Element):
    return int(item.find("start").text), int(item.find("end").text)




def shift_camera_clips(seq: ET.Element, cam_label: str, offset_frames: int):
    """Shift all video and audio clipitems for `cam_label` by `offset_frames`."""
    vid = seq.find("media/video")
    aud = seq.find("media/audio")

    def shift_track_items(track):
        for item in track.findall("clipitem"):
            start = int(item.find("start").text)
            end   = int(item.find("end").text)
            item.find("start").text = str(start + offset_frames)
            item.find("end").text   = str(end   + offset_frames)

    for track in vid.findall("track"):
        items = track.findall("clipitem")
        if not items:
            continue
        f = items[0].find("file/pathurl")
        if f is not None and cam_label.replace(" ", "%20") in (f.text or ""):
            shift_track_items(track)
            break

    # For audio tracks, identify the camera by matching clip names to video
    cam_names = set()
    for track in vid.findall("track"):
        for item in track.findall("clipitem"):
            f = item.find("file/pathurl")
            if f is not None and cam_label.replace(" ", "%20") in (f.text or ""):
                cam_names.add(item.find("name").text)

    for track in aud.findall("track"):
        items = track.findall("clipitem")
        if items and items[0].find("name").text in cam_names:
            shift_track_items(track)

    # Update sequence duration
    dur_el = seq.find("duration")
    if dur_el is not None:
        max_end = 0
        for track in vid.findall("track"):
            for item in track.findall("clipitem"):
                end = int(item.find("end").text)
                max_end = max(max_end, end)
        dur_el.text = str(max_end)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Stage 2: Detect clean audio channel and sync cameras."
    )
    parser.add_argument("--input",  default=str(OUTPUT_DIR / "01_create.xml"))
    parser.add_argument("--output", default=str(OUTPUT_DIR / "02_sync.xml"))
    parser.add_argument("--audio-config", default=str(OUTPUT_DIR / "audio_config.json"))
    parser.add_argument("--force-camera",  choices=["main", "div"],
                        help="Force which camera has the clean audio")
    parser.add_argument("--force-channel", type=int, choices=[1, 2, 3, 4],
                        help="Force which channel on that camera to use")
    args = parser.parse_args()

    input_path  = Path(args.input)
    output_path = Path(args.output)
    config_path = Path(args.audio_config)

    if not input_path.exists():
        sys.exit(f"[ERROR] Input not found: {input_path}")

    OUTPUT_DIR.mkdir(exist_ok=True)
    rep = ProgressReporter(output_path, label="sync_audios")

    print(f"[1/5] Parsing {input_path.name}...")
    main_mxfs = _mxf_files_from_xml(input_path, "MAIN CAM")
    div_mxfs  = _mxf_files_from_xml(input_path, "DIV CAM")
    print(f"      MAIN CAM: {len(main_mxfs)} MXF files")
    print(f"      DIV CAM:  {len(div_mxfs)} MXF files")

    # ---- Channel detection ----
    print("\n[2/5] Detecting best audio channel per camera...")

    main_ch, div_ch = 1, 1   # defaults used for sync if detection is skipped

    if bool(args.force_camera) != bool(args.force_channel):
        sys.exit("[ERROR] --force-camera and --force-channel must be used together.")

    if args.force_camera and args.force_channel:
        clean_cam = "MAIN CAM" if args.force_camera == "main" else "DIV CAM"
        clean_ch  = args.force_channel
        if args.force_camera == "main":
            main_ch = clean_ch
        else:
            div_ch  = clean_ch
        print(f"      Forced: {clean_cam} CH{clean_ch}")
    else:
        main_ch, main_metrics = detect_best_channel(main_mxfs, "MAIN CAM")
        div_ch,  div_metrics  = detect_best_channel(div_mxfs,  "DIV CAM")

        main_score = main_metrics[main_ch - 1]["activity"] * main_metrics[main_ch - 1]["dynamic_range_db"]
        div_score  = div_metrics[div_ch - 1]["activity"]   * div_metrics[div_ch - 1]["dynamic_range_db"]

        if main_score >= div_score:
            clean_cam, clean_ch = "MAIN CAM", main_ch
        else:
            clean_cam, clean_ch = "DIV CAM", div_ch

        print(f"\n  Detected clean audio: {clean_cam} — Channel {clean_ch}")
        print(f"  (MAIN CAM best: CH{main_ch}, score={main_score:.1f}  |  "
              f"DIV CAM best: CH{div_ch}, score={div_score:.1f})")

        # Autonomous: accept the detected channel automatically.
        # Override with --force-camera / --force-channel.
        print("  Auto-accepting detected channel "
              "(override with --force-camera / --force-channel).")

    print(f"\n  Using: {clean_cam} CH{clean_ch}")

    # Save audio config for downstream stages
    audio_config = {"camera": clean_cam, "channel": clean_ch}
    config_path.write_text(json.dumps(audio_config, indent=2), encoding="utf-8")
    print(f"  Saved: {config_path.name}")

    # ---- Sync ----
    print("\n[3/5] Extracting audio for sync analysis "
          f"(up to {SAMPLE_SECS // 60} min, 8 kHz)...")
    print(f"      Using MAIN CAM CH{main_ch} and DIV CAM CH{div_ch}")

    main_audio = extract_audio_channel(main_mxfs, channel=main_ch,
                                       max_secs=SAMPLE_SECS,
                                       reporter=rep, phase="extract MAIN")
    div_audio  = extract_audio_channel(div_mxfs,  channel=div_ch,
                                       max_secs=SAMPLE_SECS,
                                       reporter=rep, phase="extract DIV")
    print(f"      MAIN CAM: {len(main_audio)/SAMPLE_RATE:.1f}s extracted")
    print(f"      DIV  CAM: {len(div_audio)/SAMPLE_RATE:.1f}s extracted")

    print("\n[4/5] Computing cross-correlation...")
    rep.note("cross-correlation")
    # main_lead_frames = how many frames MAIN started BEFORE DIV.
    #   > 0: MAIN started first → DIV is the later camera → shift DIV forward
    #   < 0: DIV started first  → MAIN is the later camera → shift MAIN forward
    # We always shift the later-starting camera forward to align the two.
    main_lead_frames = find_sync_offset_frames(main_audio, div_audio)
    lead_secs        = main_lead_frames / 25
    print(f"      MAIN leads DIV by {main_lead_frames} frames ({lead_secs:+.2f}s)")

    if main_lead_frames >= 0:
        cam_to_shift = "DIV CAM"
        offset       = main_lead_frames
    else:
        cam_to_shift = "MAIN CAM"
        offset       = -main_lead_frames

    print(f"      {cam_to_shift} will be shifted by +{offset} frames")

    # Autonomous: apply the detected offset automatically (no confirmation).
    print("\n[5/5] Applying sync offset to XML...")
    tree = ET.parse(input_path)
    seq  = tree.getroot().find("sequence")
    shift_camera_clips(seq, cam_to_shift, offset)

    tree.write(str(output_path), xml_declaration=True, encoding="UTF-8")
    rep.done(f"{cam_to_shift} shifted +{offset} frames")
    print(f"[OK] Written: {output_path}")
    print(f"\nNext step: open OUTPUT/02_sync.xml in Premiere, scrub the")
    print(f"timeline and confirm audio aligns across both cameras.")


if __name__ == "__main__":
    main()
