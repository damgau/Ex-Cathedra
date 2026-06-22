#!/usr/bin/env python3
"""
Stage 4 — detect_framing.py
Samples the MAIN video over the silence-trimmed timeline and answers, per sampled
instant: is the SPEAKER present in MAIN's frame?

This is PERSON presence, not face. The dominant "no face" case here is the speaker
turning to his slides — a perfectly normal presenter-at-the-board shot we want to
KEEP on MAIN. So we detect the *body* (MobileNet-SSD via cv2.dnn) and only flag
him ABSENT when he is physically out of MAIN's frame (walked off / leaned out /
fully occluded). That absence is rare, which is what "keep MAIN as much as
possible" wants. The cached signal drives switch_angles.py (Stage 5), which cuts
to the DIV safety angle across the absent windows.

Frames are decoded straight from ffmpeg as raw video into numpy — no JPEG encode +
temp-dir round-trip (≈2.6x faster). Detection runs on downscaled frames for speed;
this only affects detection, never the final output (switch_angles references the
original MXF). Handles both FCP-XML file-reference styles: pathurl repeated per
clipitem (the project's create_xml output) AND Premiere's <file id> dedup.

Output: OUTPUT/main_presence.json
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

# Force UTF-8 stdout so the em-dash/accents in summaries survive Windows cp1252.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

import cv2
import numpy as np
import xml.etree.ElementTree as ET

# Reuse the P2 spanned-chain resolver from Stage 3 + the on-demand model fetch.
sys.path.insert(0, str(Path(__file__).parent))
from remove_silence import _resolve_p2_chain   # noqa: E402
from ffbin import ffmpeg                        # noqa: E402
from progress import ProgressReporter           # noqa: E402
from fetch_models import mobilenet_ssd_paths    # noqa: E402

BASE_DIR   = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "OUTPUT"
FPS        = 25


# ---------------------------------------------------------------------------
# XML / path helpers
# ---------------------------------------------------------------------------

def _localize_pathurl(furl_text: str) -> Path:
    """Decode an FCP pathurl and re-anchor it on the local INPUT/ tree so it
    resolves regardless of the machine the XML was generated on (macOS vs
    Windows). See panasonic_p2_reference.md §5."""
    raw = unquote(furl_text.replace("file://localhost", "")).replace("\\", "/")
    parts = raw.split("/")
    if "INPUT" in parts:
        return BASE_DIR.joinpath(*parts[parts.index("INPUT"):])
    if len(raw) > 2 and raw[0] == "/" and raw[2] == ":":   # /G:/… → G:/…
        raw = raw[1:]
    return Path(raw)


def _file_pathurl_map(seq: ET.Element) -> dict:
    """{file_id: pathurl} for every <file id=..> that carries a <pathurl>.
    Premiere only writes the pathurl on the first clipitem per file; later
    clipitems reference it by id (<file id=".."/>)."""
    m = {}
    for f in seq.iter("file"):
        fid = f.get("id")
        pu = f.find("pathurl")
        if fid and pu is not None and pu.text:
            m[fid] = pu.text
    return m


def _clip_pathurl(it: ET.Element, fmap: dict):
    """Resolve a clipitem's pathurl directly or via its <file id> reference."""
    f = it.find("file")
    if f is None:
        return None
    pu = f.find("pathurl")
    if pu is not None and pu.text:
        return pu.text
    return fmap.get(f.get("id"))


def _main_clips(seq: ET.Element) -> list:
    """Return enabled MAIN-CAM video clipitems as
    (timeline_start, source_in, duration, mxf_path), in order."""
    fmap = _file_pathurl_map(seq)
    vid = seq.find("media/video")
    main_track = None
    for track in vid.findall("track"):
        items = track.findall("clipitem")
        if items and "MAIN%20CAM" in (_clip_pathurl(items[0], fmap) or ""):
            main_track = track
            break
    if main_track is None:
        return []
    out = []
    for it in main_track.findall("clipitem"):
        en = it.find("enabled")
        if en is not None and (en.text or "").upper() == "FALSE":
            continue
        pu = _clip_pathurl(it, fmap)
        if not pu:
            continue
        t_s = int(it.find("start").text)
        s_in = int(it.find("in").text)
        dur = int(it.find("end").text) - t_s
        if dur > 0:
            out.append((t_s, s_in, dur, _localize_pathurl(pu)))
    return out


# ---------------------------------------------------------------------------
# Frame decode — raw video piped straight from ffmpeg into numpy (no disk)
# ---------------------------------------------------------------------------

def iter_main_frames(clips: list, fps: float, scale: int,
                     start_f=None, end_f=None, max_frames=None):
    """Stream (timeline_frame, bgr_ndarray) across all MAIN clip-segments.

    Footage is anamorphic (1440x1080 stored, displayed 16:9). Scaling to the
    stored 4:3 geometry squishes the subject horizontally; force the 16:9 display
    geometry. Yields in timeline order; honours --start/--end/--max-frames and
    terminates the ffmpeg child on early stop so nothing is left decoding."""
    h = round(scale * 9 / 16)
    fsize = scale * h * 3
    yielded = 0
    for (t_start, src_in, dur, mxf) in clips:
        cursor = src_in    # chain-global source frame at the slice start
        for (sub_mxf, local_in, sub_dur) in _resolve_p2_chain(mxf, src_in, dur):
            if not sub_mxf.exists():
                print(f"  [WARN] missing MXF: {sub_mxf}")
                cursor += sub_dur
                continue
            cmd = [ffmpeg(), "-loglevel", "error",
                   "-ss", str(local_in / FPS), "-i", str(sub_mxf),
                   "-t", str(sub_dur / FPS),
                   "-vf", f"fps={fps},scale={scale}:{h}",
                   "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            k = 0
            stop = False
            while True:
                buf = proc.stdout.read(fsize)
                if len(buf) < fsize:
                    break
                chain_global = cursor + round(k / fps * FPS)
                tf = t_start + (chain_global - src_in)
                k += 1
                if start_f is not None and tf < start_f:
                    continue
                if end_f is not None and tf > end_f:
                    stop = True
                    break
                frame = np.frombuffer(buf, np.uint8).reshape(h, scale, 3)
                yield int(tf), frame
                yielded += 1
                if max_frames and yielded >= max_frames:
                    stop = True
                    break
            proc.stdout.close()
            if stop:
                proc.terminate(); proc.wait()
                return
            proc.wait()
            cursor += sub_dur


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

class PersonDetector:
    """MobileNet-SSD (Caffe) person presence via cv2.dnn. Robust to the speaker
    standing still, gesturing, turning to slides, or showing a profile — all of
    which still read as PRESENT. Weights are fetched on demand (gitignored)."""
    PERSON_CLASS = 15  # PASCAL VOC class index for "person"

    def __init__(self, proto: str, model: str, conf: float = 0.5):
        self.net = cv2.dnn.readNetFromCaffe(proto, model)
        self.conf = conf

    def present(self, bgr) -> bool:
        blob = cv2.dnn.blobFromImage(bgr, 0.007843, (300, 300), 127.5)
        self.net.setInput(blob)
        det = self.net.forward()
        for i in range(det.shape[2]):
            if det[0, 0, i, 2] > self.conf and int(det[0, 0, i, 1]) == self.PERSON_CLASS:
                return True
        return False


class FaceDetector:
    """Haar frontal+profile face detector. NO LONGER the Stage-4 signal (face
    absence over-fires — the speaker faces his slides constantly). Retained as the
    over-fire baseline for tools/benchmark_presence.py."""
    def __init__(self, min_face: float, neighbors: int):
        hp = cv2.data.haarcascades
        self.frontal = cv2.CascadeClassifier(hp + "haarcascade_frontalface_default.xml")
        self.profile = cv2.CascadeClassifier(hp + "haarcascade_profileface.xml")
        if self.frontal.empty() or self.profile.empty():
            sys.exit("[ERROR] Could not load Haar cascades from cv2.data.haarcascades")
        self.min_face = min_face
        self.neighbors = neighbors

    def present(self, bgr) -> bool:
        gray = cv2.equalizeHist(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY))
        h = gray.shape[0]
        m = max(20, int(self.min_face * h))
        kw = dict(scaleFactor=1.1, minNeighbors=self.neighbors, minSize=(m, m))
        if len(self.frontal.detectMultiScale(gray, **kw)):
            return True
        if len(self.profile.detectMultiScale(gray, **kw)):
            return True
        if len(self.profile.detectMultiScale(cv2.flip(gray, 1), **kw)):
            return True
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Stage 4: detect whether the SPEAKER is present in MAIN's frame "
                    "(person detection; absence is rare and means he left frame)."
    )
    parser.add_argument("--input",  default=str(OUTPUT_DIR / "03_silence.xml"))
    parser.add_argument("--output", default=str(OUTPUT_DIR / "main_presence.json"))
    parser.add_argument("--fps",   type=float, default=4.0, help="Samples/sec (default 4)")
    parser.add_argument("--scale", type=int,   default=480,
                        help="Decode frame width px (default 480; DNN re-blobs to 300x300)")
    parser.add_argument("--conf",  type=float, default=0.5,
                        help="Min person-detection confidence (default 0.5)")
    parser.add_argument("--start", type=float, default=None, metavar="s",
                        help="Only analyse timeline from this second")
    parser.add_argument("--end",   type=float, default=None, metavar="s",
                        help="Only analyse timeline up to this second")
    parser.add_argument("--max-frames", type=int, default=None,
                        help="Stop after this many samples (quick iteration)")
    args = parser.parse_args()

    input_path  = Path(args.input)
    output_path = Path(args.output)
    if not input_path.exists():
        sys.exit(f"[ERROR] Input not found: {input_path}")

    seq = ET.parse(input_path).getroot().find("sequence")
    if seq is None:
        sys.exit("[ERROR] No <sequence> found.")
    clips = _main_clips(seq)
    if not clips:
        sys.exit("[ERROR] No enabled MAIN CAM video clips found.")
    total_frames = sum(d for _, _, d, _ in clips)
    print(f"[1/3] MAIN CAM: {len(clips)} clip segments, "
          f"{total_frames/FPS/60:.1f} min of kept footage")

    proto, model = mobilenet_ssd_paths()       # fetched on demand if missing
    detector = PersonDetector(proto, model, conf=args.conf)

    start_f = int(args.start * FPS) if args.start is not None else None
    end_f   = int(args.end   * FPS) if args.end   is not None else None

    # Expected sample count drives the progress %/ETA.
    win_start_s = args.start if args.start is not None else 0.0
    win_end_s   = args.end   if args.end   is not None else total_frames / FPS
    expected = max(1, int(round((win_end_s - win_start_s) * args.fps)))
    if args.max_frames:
        expected = min(expected, args.max_frames)
    rep = ProgressReporter(output_path, total=expected, label="detect_framing")

    print(f"[2/3] Sampling at {args.fps} fps (scale={args.scale}px, conf={args.conf}, "
          f"detector=mobilenet-ssd)...")
    samples = []
    for tf, frame in iter_main_frames(clips, args.fps, args.scale,
                                      start_f, end_f, args.max_frames):
        samples.append({"timeline_frame": int(tf),
                        "speaker_present": detector.present(frame)})
        rep.update(len(samples))
        if len(samples) and len(samples) % 500 == 0:
            print(f"      ... {len(samples)} samples")

    if not samples:
        rep.fail("no frames sampled")
        sys.exit("[ERROR] No frames sampled — check ffmpeg and MXF paths above.")

    samples.sort(key=lambda s: s["timeline_frame"])

    # ---- summary ----
    n_present = sum(1 for s in samples if s["speaker_present"])
    longest_gap, run, runs = 0, 0, 0
    for s in samples:
        if not s["speaker_present"]:
            if run == 0:
                runs += 1
            run += 1
            longest_gap = max(longest_gap, run)
        else:
            run = 0
    print(f"[3/3] {len(samples)} samples — speaker present {n_present/len(samples):.0%}; "
          f"{runs} absent run(s); longest ~{longest_gap/args.fps:.1f}s")

    result = {
        "source": str(input_path),
        "detector": "mobilenet-ssd",
        "fps": args.fps,
        "scale": args.scale,
        "conf": args.conf,
        "timeline_fps": FPS,
        "samples": samples,
    }
    OUTPUT_DIR.mkdir(exist_ok=True)
    output_path.write_text(json.dumps(result), encoding="utf-8")
    rep.done(message=f"{len(samples)} samples, present {n_present/len(samples):.0%}")
    print(f"[OK] Written: {output_path}")


if __name__ == "__main__":
    main()
