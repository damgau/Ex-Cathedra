#!/usr/bin/env python3
"""
Phase-1 benchmark — benchmark_presence.py
Stress-tests candidate "is the speaker present in MAIN's frame?" detectors on a
short slice, on TWO axes:

  * accuracy  — speaker-present %, count/duration of "absent" runs, timestamps,
    plus a handful of dumped frames at the absent moments to eyeball.
  * speed     — per-detector detect throughput (fps), and a decode-strategy
    comparison: the current JPEG-to-temp-dir extraction vs. piping raw frames
    straight from ffmpeg into numpy (no JPEG encode + disk round-trip).

This does NOT touch the pipeline. It only reads an XML slice + the MXFs and
writes disposable artifacts under .tmp/benchmark_presence/. Use it to pick the
detector + detection scale + decode strategy before reworking detect_framing.

Handles BOTH FCP-XML file-reference styles: pathurl repeated per clipitem (the
project's create_xml output) AND Premiere's id-dedup (pathurl only on the first
clipitem per <file id>, later ones just <file id=".."/>).

Run (smoke):  python tools/benchmark_presence.py --max-frames 200 --scales 480
Run (full):   python tools/benchmark_presence.py
"""

import sys
import time
import argparse
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

import cv2
import numpy as np
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).parent))
from remove_silence import _resolve_p2_chain          # noqa: E402
from detect_framing import _localize_pathurl, FaceDetector, FPS  # noqa: E402
from ffbin import ffmpeg                                          # noqa: E402

BASE_DIR   = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "OUTPUT"
TMP_DIR    = BASE_DIR / ".tmp" / "benchmark_presence"


# ---------------------------------------------------------------------------
# Clip enumeration (file-id aware)
# ---------------------------------------------------------------------------

def _file_pathurl_map(seq: ET.Element) -> dict:
    """{file_id: pathurl} for every <file id=..> that carries a <pathurl>."""
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


def main_clips(seq: ET.Element) -> list:
    """Enabled MAIN-CAM video clipitems as (timeline_start, source_in, dur, mxf)."""
    fmap = _file_pathurl_map(seq)
    vid = seq.find("media/video")
    main_track = None
    for track in vid.findall("track"):
        items = track.findall("clipitem")
        if items and (_clip_pathurl(items[0], fmap) or "") .find("MAIN%20CAM") >= 0:
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
# Frame decode: PIPE (rawvideo → numpy) and JPEG-to-temp-dir (the current way)
# ---------------------------------------------------------------------------

def _vf(fps: float, scale: int) -> str:
    # Anamorphic fix: footage is 1440x1080 stored, displayed 16:9 — force 16:9.
    return f"fps={fps},scale={scale}:{round(scale * 9 / 16)}"


def iter_main_frames(clips: list, fps: float, scale: int, max_frames=None):
    """Stream (timeline_frame, bgr_ndarray) across all MAIN clip-segments via a
    raw-video pipe (no disk). Mirrors detect_framing's timeline-frame math."""
    h = round(scale * 9 / 16)
    fsize = scale * h * 3
    yielded = 0
    for (t_start, src_in, dur, mxf) in clips:
        cursor = src_in
        for (sub_mxf, local_in, sub_dur) in _resolve_p2_chain(mxf, src_in, dur):
            if not sub_mxf.exists():
                cursor += sub_dur
                continue
            cmd = [ffmpeg(), "-nostdin", "-loglevel", "error",
                   "-ss", str(local_in / FPS), "-i", str(sub_mxf),
                   "-t", str(sub_dur / FPS), "-vf", _vf(fps, scale),
                   "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            k = 0
            while True:
                buf = proc.stdout.read(fsize)
                if len(buf) < fsize:
                    break
                frame = np.frombuffer(buf, np.uint8).reshape(h, scale, 3)
                chain_global = cursor + round(k / fps * FPS)
                tf = t_start + (chain_global - src_in)
                yield int(tf), frame
                k += 1
                yielded += 1
                if max_frames and yielded >= max_frames:
                    proc.stdout.close(); proc.terminate(); proc.wait()
                    return
            proc.stdout.close(); proc.wait()
            cursor += sub_dur


def decode_only_pipe(clips, fps, scale, max_frames=None) -> int:
    n = 0
    for _ in iter_main_frames(clips, fps, scale, max_frames):
        n += 1
    return n


def decode_only_jpeg(clips, fps, scale, max_frames=None) -> int:
    """The current strategy: ffmpeg writes JPEGs to a temp dir, then cv2.imread."""
    import tempfile
    n = 0
    for (_, src_in, dur, mxf) in clips:
        for (sub_mxf, local_in, sub_dur) in _resolve_p2_chain(mxf, src_in, dur):
            if not sub_mxf.exists():
                continue
            with tempfile.TemporaryDirectory(prefix="bench_jpg_") as td:
                cmd = [ffmpeg(), "-nostdin", "-loglevel", "error",
                       "-ss", str(local_in / FPS), "-i", str(sub_mxf),
                       "-t", str(sub_dur / FPS), "-vf", _vf(fps, scale),
                       str(Path(td) / "f_%06d.jpg")]
                if subprocess.run(cmd, capture_output=True).returncode != 0:
                    continue
                for ff in sorted(Path(td).glob("f_*.jpg")):
                    if cv2.imread(str(ff)) is not None:
                        n += 1
                    if max_frames and n >= max_frames:
                        return n
    return n


# ---------------------------------------------------------------------------
# Detectors  (.name, .present(bgr) -> bool)
# ---------------------------------------------------------------------------

class HOGDetector:
    name = "hog"
    def __init__(self):
        self.hog = cv2.HOGDescriptor()
        self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
    def present(self, bgr) -> bool:
        rects, _ = self.hog.detectMultiScale(bgr, winStride=(8, 8),
                                             padding=(8, 8), scale=1.05)
        return len(rects) > 0


class MotionDetector:
    """MOG2 foreground ratio. NOTE: a motionless speaker fades into the
    background and reads ABSENT — the known failure mode we want to see."""
    name = "motion"
    def __init__(self, thr=0.004):
        self.bg = cv2.createBackgroundSubtractorMOG2(history=200, varThreshold=32,
                                                     detectShadows=False)
        self.thr = thr
    def present(self, bgr) -> bool:
        fg = self.bg.apply(bgr)
        return (cv2.countNonZero(fg) / fg.size) > self.thr


class DNNPersonDetector:
    """cv2.dnn person detector. MobileNet-SSD (caffe) or an ONNX model.
    Skipped unless --dnn-model is given (no asset checked in yet)."""
    name = "dnn"
    def __init__(self, model: str, config: str = None, conf=0.5):
        if config:
            self.net = cv2.dnn.readNetFromCaffe(config, model)
            self.kind = "ssd"
        else:
            self.net = cv2.dnn.readNet(model)
            self.kind = "onnx"
        self.conf = conf
    def present(self, bgr) -> bool:
        if self.kind == "ssd":
            blob = cv2.dnn.blobFromImage(bgr, 0.007843, (300, 300), 127.5)
            self.net.setInput(blob)
            det = self.net.forward()
            for i in range(det.shape[2]):
                if det[0, 0, i, 2] > self.conf and int(det[0, 0, i, 1]) == 15:  # 15=person (VOC)
                    return True
            return False
        # generic onnx: caller would adapt; treat any high-conf box as person
        blob = cv2.dnn.blobFromImage(bgr, 1/255.0, (640, 640), swapRB=True)
        self.net.setInput(blob)
        out = self.net.forward()
        return bool((out[..., 4] > self.conf).any())


class FaceBaseline:
    """The CURRENT signal — proves how badly face-presence over-fires."""
    name = "face"
    def __init__(self):
        self.fd = FaceDetector(min_face=0.08, neighbors=5)
    def present(self, bgr) -> bool:
        return self.fd.present(bgr)


def build_detectors(names, dnn_model, dnn_config):
    out = []
    for n in names:
        if n == "face":
            out.append(FaceBaseline())
        elif n == "hog":
            out.append(HOGDetector())
        elif n == "motion":
            out.append(MotionDetector())
        elif n == "dnn":
            if not dnn_model:
                print("  [skip] dnn — no --dnn-model given")
                continue
            out.append(DNNPersonDetector(dnn_model, dnn_config))
    return out


# ---------------------------------------------------------------------------
# Accuracy summary over a present/absent signal
# ---------------------------------------------------------------------------

def summarize(signal, fps, trigger_s) -> dict:
    """signal = [(timeline_frame, present_bool), ...] in time order."""
    n = len(signal)
    n_present = sum(1 for _, p in signal if p)
    trig = max(1, round(trigger_s * fps))
    runs, longest, cur = 0, 0, 0
    qualified = []
    run_start = None
    for tf, p in signal:
        if not p:
            if cur == 0:
                run_start = tf
            cur += 1
            longest = max(longest, cur)
        else:
            if cur >= trig:
                runs += 1
                qualified.append((run_start, tf))
            cur = 0
    if cur >= trig:
        runs += 1
        qualified.append((run_start, signal[-1][0]))
    return {
        "samples": n,
        "present_pct": (n_present / n) if n else 0.0,
        "absent_runs_ge_trigger": runs,
        "longest_absent_s": longest / fps,
        "qualified": qualified,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Benchmark speaker-presence detectors (speed + accuracy).")
    ap.add_argument("--input", default=str(OUTPUT_DIR / "03_silence_10min.xml"))
    ap.add_argument("--fps", type=float, default=4.0, help="Sampling rate (held fixed; default 4)")
    ap.add_argument("--scales", default="640,480,320", help="Detection-frame widths to sweep")
    ap.add_argument("--detectors", default="face,hog,motion", help="Comma list: face,hog,motion,dnn")
    ap.add_argument("--dnn-model", default=None, help="Path to a person model (.caffemodel/.onnx)")
    ap.add_argument("--dnn-config", default=None, help="Caffe .prototxt (for MobileNet-SSD)")
    ap.add_argument("--trigger", type=float, default=1.0, help="Absent-run floor for counting (s)")
    ap.add_argument("--dump", type=int, default=6, help="Absent frames to dump per detector/scale")
    ap.add_argument("--decode-scale", type=int, default=480, help="Scale for the decode-strategy A/B test")
    ap.add_argument("--decode-frames", type=int, default=200,
                    help="Frames for the decode A/B test (kept small; jpeg path is slow)")
    ap.add_argument("--max-frames", type=int, default=None, help="Cap detector-sweep samples (smoke test)")
    ap.add_argument("--outdir", default=str(TMP_DIR))
    args = ap.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        sys.exit(f"[ERROR] Input not found: {in_path}")
    seq = ET.parse(in_path).getroot().find("sequence")
    if seq is None:
        sys.exit("[ERROR] No <sequence> found.")
    clips = main_clips(seq)
    if not clips:
        sys.exit("[ERROR] No enabled MAIN CAM clips (file-id resolution failed?).")
    total = sum(d for _, _, d, _ in clips)
    print(f"[input] {in_path.name}: {len(clips)} MAIN segment(s), "
          f"{total/FPS/60:.1f} min kept footage, sampling @ {args.fps} fps")

    scales = [int(s) for s in args.scales.split(",") if s.strip()]
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    report = [f"# benchmark_presence — {in_path.name}",
              f"\nMAIN footage: {total/FPS/60:.1f} min, fps={args.fps}, "
              f"detectors={args.detectors}, scales={scales}\n"]

    # ---- decode-strategy A/B (once, at --decode-scale, capped — jpeg path is slow) ----
    dcap = args.max_frames or args.decode_frames
    print(f"\n[decode] strategy A/B at scale={args.decode_scale} ({dcap} frames) ...")
    t0 = time.perf_counter(); n_jpg = decode_only_jpeg(clips, args.fps, args.decode_scale, dcap); t_jpg = time.perf_counter() - t0
    t0 = time.perf_counter(); n_pipe = decode_only_pipe(clips, args.fps, args.decode_scale, dcap); t_pipe = time.perf_counter() - t0
    print(f"  JPEG-to-temp-dir : {n_jpg:5d} frames  {t_jpg:7.1f}s  ({n_jpg/t_jpg:5.1f} f/s)")
    print(f"  raw pipe→numpy   : {n_pipe:5d} frames  {t_pipe:7.1f}s  ({n_pipe/t_pipe:5.1f} f/s)")
    speedup = (t_jpg / t_pipe) if t_pipe else 0
    print(f"  → pipe is {speedup:.2f}x the jpeg path")
    report.append(f"## Decode strategy (scale {args.decode_scale})\n"
                  f"| strategy | frames | wall s | f/s |\n|---|---|---|---|\n"
                  f"| jpeg-temp-dir | {n_jpg} | {t_jpg:.1f} | {n_jpg/t_jpg:.1f} |\n"
                  f"| raw-pipe | {n_pipe} | {t_pipe:.1f} | {n_pipe/t_pipe:.1f} |\n"
                  f"\n**pipe is {speedup:.2f}× the jpeg path**\n")

    # ---- detector sweep per scale ----
    rows = []
    for scale in scales:
        print(f"\n[scale {scale}] streaming frames once, running all detectors ...")
        dets = build_detectors(args.detectors.split(","), args.dnn_model, args.dnn_config)
        signals = {d.name: [] for d in dets}
        det_time = {d.name: 0.0 for d in dets}
        dumped = {d.name: 0 for d in dets}
        ddir = {d.name: (outdir / f"scale_{scale}" / d.name) for d in dets}
        for d in dets:
            ddir[d.name].mkdir(parents=True, exist_ok=True)
        nframes = 0
        for tf, frame in iter_main_frames(clips, args.fps, scale, args.max_frames):
            nframes += 1
            for d in dets:
                t0 = time.perf_counter()
                present = d.present(frame)
                det_time[d.name] += time.perf_counter() - t0
                signals[d.name].append((tf, present))
                if not present and dumped[d.name] < args.dump:
                    img = frame.copy()
                    cv2.putText(img, f"{d.name} ABSENT t={tf/FPS:.1f}s", (8, 22),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
                    cv2.imwrite(str(ddir[d.name] / f"absent_{tf:06d}.jpg"), img)
                    dumped[d.name] += 1
            if nframes % 500 == 0:
                print(f"    ... {nframes} frames")
        print(f"    {nframes} frames sampled at scale {scale}")
        report.append(f"\n## Detectors @ scale {scale}  ({nframes} frames)\n"
                      f"| detector | present % | absent runs ≥{args.trigger}s | "
                      f"longest absent | detect f/s | detect s |\n"
                      f"|---|---|---|---|---|---|\n")
        for d in dets:
            s = summarize(signals[d.name], args.fps, args.trigger)
            fps_thru = nframes / det_time[d.name] if det_time[d.name] else 0
            line = (f"| {d.name:6s} | {s['present_pct']:6.0%} | {s['absent_runs_ge_trigger']:3d} | "
                    f"{s['longest_absent_s']:5.1f}s | {fps_thru:6.1f} | {det_time[d.name]:5.1f} |")
            print("    " + line.replace("|", " "))
            report.append(line + "\n")
            rows.append((scale, d.name, s, fps_thru))

    report_path = outdir / "report.md"
    report_path.write_text("".join(report), encoding="utf-8")
    print(f"\n[OK] Report: {report_path}")
    print(f"     Annotated absent frames: {outdir}/scale_*/<detector>/")
    print("\nReview the present% (higher = stays on MAIN through slide-facing), the absent runs"
          "\n(should be few genuine exits), the throughput, and the dumped frames — then pick the"
          "\ndetector + scale + decode strategy.")


if __name__ == "__main__":
    main()
