#!/usr/bin/env python3
"""
Stage 4 — detect_framing.py
Samples the MAIN video over the silence-trimmed timeline and runs OpenCV Haar
face detection to answer, per sampled instant: is the speaker's face visible on
MAIN? The cached signal drives switch_angles.py (Stage 5).

"Face visible" = a frontal OR profile Haar cascade fires above a size floor, so a
head-turn still counts as visible (the conservative "any face" rule). Detection
runs on downscaled frames for speed; this only affects detection, never the final
output (switch_angles only references the original MXF).

Output: OUTPUT/main_framing.json
"""

import sys
import json
import argparse
import subprocess
import tempfile
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
import xml.etree.ElementTree as ET

# Reuse the P2 spanned-chain resolver from Stage 3.
sys.path.insert(0, str(Path(__file__).parent))
from remove_silence import _resolve_p2_chain   # noqa: E402
from progress import ProgressReporter           # noqa: E402

BASE_DIR   = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "OUTPUT"
FPS        = 25


# ---------------------------------------------------------------------------
# XML / path helpers
# ---------------------------------------------------------------------------

def _localize_pathurl(furl_text: str) -> Path:
    """Decode an FCP pathurl and re-anchor it on the local INPUT/ tree so it
    resolves regardless of the machine the XML was generated on (macOS vs
    Windows). See panasonic_p2_reference.md §5 and create_transcript's notes."""
    raw = unquote(furl_text.replace("file://localhost", "")).replace("\\", "/")
    parts = raw.split("/")
    if "INPUT" in parts:
        return BASE_DIR.joinpath(*parts[parts.index("INPUT"):])
    if len(raw) > 2 and raw[0] == "/" and raw[2] == ":":   # /G:/… → G:/…
        raw = raw[1:]
    return Path(raw)


def _main_clips(seq: ET.Element) -> list:
    """Return enabled MAIN-CAM video clipitems as
    (timeline_start, source_in, duration, mxf_path), in order."""
    vid = seq.find("media/video")
    for track in vid.findall("track"):
        items = track.findall("clipitem")
        if not items:
            continue
        f0 = items[0].find("file/pathurl")
        if f0 is None or not f0.text or "MAIN%20CAM" not in f0.text:
            continue
        out = []
        for it in items:
            en = it.find("enabled")
            if en is not None and (en.text or "").upper() == "FALSE":
                continue
            furl = it.find("file/pathurl")
            if furl is None or not furl.text:
                continue
            t_s = int(it.find("start").text)
            s_in = int(it.find("in").text)
            dur = int(it.find("end").text) - t_s
            if dur <= 0:
                continue
            out.append((t_s, s_in, dur, _localize_pathurl(furl.text)))
        return out
    return []


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

class FaceDetector:
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
        # profile cascade detects one orientation → also try the mirror
        if len(self.profile.detectMultiScale(gray, **kw)):
            return True
        if len(self.profile.detectMultiScale(cv2.flip(gray, 1), **kw)):
            return True
        return False


def _extract_frames(mxf: Path, local_in_f: int, dur_f: int,
                    fps: float, scale: int, tmp: Path) -> list:
    """ffmpeg-decode a slice to downscaled JPEGs; return sorted file paths."""
    # Footage is anamorphic (1440x1080 stored, displayed 16:9). Scaling to the
    # stored 4:3 geometry leaves faces horizontally squished, which hurts Haar.
    # Force the 16:9 display geometry so faces have natural proportions.
    cmd = [
        "ffmpeg", "-loglevel", "error",
        "-ss", str(local_in_f / FPS), "-i", str(mxf),
        "-t", str(dur_f / FPS),
        "-vf", f"fps={fps},scale={scale}:{round(scale * 9 / 16)}",
        str(tmp / "f_%06d.jpg"),
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        print(f"  [WARN] ffmpeg failed on {mxf.name}: {r.stderr[:100].decode(errors='replace')}")
        return []
    return sorted(tmp.glob("f_*.jpg"))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Stage 4: detect whether the speaker's face is visible on MAIN."
    )
    parser.add_argument("--input",  default=str(OUTPUT_DIR / "03_silence.xml"))
    parser.add_argument("--output", default=str(OUTPUT_DIR / "main_framing.json"))
    parser.add_argument("--fps",   type=float, default=4.0, help="Samples/sec (default 4)")
    parser.add_argument("--scale", type=int,   default=640, help="Detection frame width px (default 640)")
    parser.add_argument("--min-face", type=float, default=0.08,
                        help="Min face height as a fraction of frame height (default 0.08)")
    parser.add_argument("--neighbors", type=int, default=5,
                        help="Haar minNeighbors (higher = stricter, default 5)")
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

    start_f = int(args.start * FPS) if args.start is not None else None
    end_f   = int(args.end   * FPS) if args.end   is not None else None
    detector = FaceDetector(args.min_face, args.neighbors)

    # Expected sample count drives the progress %/ETA. The window is the kept
    # footage in seconds (clamped to any --start/--end), sampled at args.fps.
    win_start_s = args.start if args.start is not None else 0.0
    win_end_s   = args.end   if args.end   is not None else total_frames / FPS
    expected = max(1, int(round((win_end_s - win_start_s) * args.fps)))
    if args.max_frames:
        expected = min(expected, args.max_frames)
    rep = ProgressReporter(output_path, total=expected, label="detect_framing")

    print(f"[2/3] Sampling at {args.fps} fps (scale={args.scale}px, "
          f"min-face={args.min_face}, neighbors={args.neighbors})...")
    samples = []
    stop = False
    for ci, (t_start, src_in, dur, mxf) in enumerate(clips, 1):
        if stop:
            break
        cursor = src_in    # chain-global source frame at the slice start
        for (sub_mxf, local_in, sub_dur) in _resolve_p2_chain(mxf, src_in, dur):
            if not sub_mxf.exists():
                print(f"  [WARN] missing MXF: {sub_mxf}")
                cursor += sub_dur
                continue
            with tempfile.TemporaryDirectory(prefix="excat_frames_") as td:
                frame_files = _extract_frames(sub_mxf, local_in, sub_dur,
                                              args.fps, args.scale, Path(td))
                for k, ff in enumerate(frame_files):
                    chain_global = cursor + round(k / args.fps * FPS)
                    tf = t_start + (chain_global - src_in)
                    if start_f is not None and tf < start_f:
                        continue
                    if end_f is not None and tf > end_f:
                        stop = True
                        break
                    img = cv2.imread(str(ff))
                    if img is None:
                        continue
                    samples.append({"timeline_frame": int(tf),
                                    "face_present": detector.present(img)})
                    rep.update(len(samples), message=f"clip {ci}/{len(clips)}")
                    if args.max_frames and len(samples) >= args.max_frames:
                        stop = True
                        break
            cursor += sub_dur
            if stop:
                break
        if len(samples) and len(samples) % 500 == 0:
            print(f"      ... {len(samples)} samples")

    if not samples:
        rep.fail("no frames sampled")
        sys.exit("[ERROR] No frames sampled — check ffmpeg and MXF paths above.")

    samples.sort(key=lambda s: s["timeline_frame"])

    # ---- summary ----
    n_face = sum(1 for s in samples if s["face_present"])
    longest_gap, run, runs = 0, 0, 0
    for s in samples:
        if not s["face_present"]:
            if run == 0:
                runs += 1
            run += 1
            longest_gap = max(longest_gap, run)
        else:
            run = 0
    print(f"[3/3] {len(samples)} samples — face present {n_face/len(samples):.0%}; "
          f"{runs} no-face run(s); longest ~{longest_gap/args.fps:.1f}s")

    result = {
        "source": str(input_path),
        "fps": args.fps,
        "scale": args.scale,
        "min_face": args.min_face,
        "neighbors": args.neighbors,
        "timeline_fps": FPS,
        "samples": samples,
    }
    OUTPUT_DIR.mkdir(exist_ok=True)
    output_path.write_text(json.dumps(result), encoding="utf-8")
    rep.done(message=f"{len(samples)} samples, face {n_face/len(samples):.0%}")
    print(f"[OK] Written: {output_path}")


if __name__ == "__main__":
    main()
