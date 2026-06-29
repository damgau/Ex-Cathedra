#!/usr/bin/env python3
"""
Stage 5 — place_slides.py
Puts the speaker's slides full-screen on a new V3 track of OUTPUT/04_angles.xml,
timed to the talk, by reading the projector screen on the locked DIV camera.

WHY the projector (not the transcript): this deck arrived as image-only JPEGs
with no text layer, so transcript<->slide-text matching is impossible. The DIV
safety camera is a static wide shot whose projector screen is readable across
the whole talk, so we read the screen directly.

Approach (validated by a Phase-0 spike on real frames — see docs/plans/place_slides.md):
  * TIMING is robust, IDENTITY is fragile, so they are DECOUPLED.
  * Change-detection = frame-DIFFERENCING of the rectified screen (white-stays-
    white contributes 0, so only changed text registers). Correlation/NCC/pHash
    all fail here because the white template dominates. Threshold ~0.01;
    persistence-debounced to reject the speaker crossing / fades.
  * IDENTITY (which of the 46 deck PNGs) = ORB feature inlier count — the only
    matcher that separates the near-identical text-template slides. Best-effort:
    the still is placed at the robust change time, labelled with the best ORB
    match, and uncertain labels are flagged for the editor to swap.

Pipeline:
  Pass 1  stream DIV at --sample-fps, rectify the fixed --screen-quad, frame-diff
          -> persistence-debounced slide segments [t_start, t_end].
  Pass 2  per segment: grab one mid-segment full-res frame, ORB-match vs the 46
          deck PNGs -> best slide id + confidence; merge same-id neighbours.
  Build   per kept segment: start snapped to first spoken word; duration
          clamp(8s, 0.3s x words, 25s) bounded to the on-screen gap; skip <5s.
  Emit    append a V3 <track> of square-pixel 1920x1080 still clipitems.

Output: OUTPUT/05_slides.xml  +  OUTPUT/05_slides_report.json

Run with system Python (cv2 + numpy):
    python tools/place_slides.py
"""

import sys
import json
import math
import argparse
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv
load_dotenv()

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

import cv2
import numpy as np
from PIL import Image
import xml.etree.ElementTree as ET

from detect_framing import (_file_pathurl_map, _clip_pathurl, _localize_pathurl,
                            iter_main_frames)
from remove_silence import _resolve_p2_chain
from create_xml import pathurl, _rate, _logginginfo, _colorinfo, TICKS_PER_FRAME
from timeline import _next_id
from ffbin import ffmpeg
from progress import ProgressReporter

BASE_DIR   = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "OUTPUT"
FPS        = 25

# --- fixed screen quad (1920x1080 display space), from the Phase-0 spike. The
#     DIV camera is locked, so one quad serves the whole talk. Override --screen-quad.
DEFAULT_QUAD = "802,48,1602,56,1606,536,798,528"   # tl tr br bl
RECT_W, RECT_H = 1280, 720      # rectified screen (frontal)
DIFF_W, DIFF_H = 640, 360       # cheap diff resolution

# --- documented internal constants (promote to flags only if a run needs turning) ---
DECODE_SCALE   = 960            # Pass-1 decode width (decoupled from the 1920 quad space)
DIFF_THRESH    = 25             # per-pixel abs-diff threshold on blurred gray (0-255)
CHANGE_FRAC    = 0.012          # >this fraction of changed pixels = different slide
PERSIST_S      = 2.0            # new content must hold this long to confirm a change
ORB_MIN        = 60             # >=this many ORB inliers = a confident identity
ORB_FEATURES   = 1500
MIN_DWELL_S    = 5.0            # slides shown shorter than this were flipped past -> skip
FLOOR_S, CAP_S = 8.0, 25.0      # cutaway duration floor / cap
WORDS_RATE_S   = 0.3            # seconds of cutaway added per spoken word
STILL_FILE_DUR = 1_500_000      # notional still-file length (frames); any out is valid

# --- V3 still rendering -------------------------------------------------------
# The deck PNGs are 4:3 content padded to 16:9 with solid bars. For V3 we (a) drop
# the bars for a BLURRED-UPSCALED background (no black/white bands), and (b) emit
# the still at the sequence's own 1440x1080-anamorphic geometry so it displays at
# 100% width (square-pixel stills stretch to 133% in this sequence).
DISP_W, DISP_H = 1920, 1080     # composition built in 16:9 display space
SEQ_W,  SEQ_H  = 1440, 1080     # then squeezed to the sequence's anamorphic store
BAR_TRIM_TOL   = 8              # a border line this uniform (mean abs dev) is a bar
BAR_TRIM_MAX   = 0.45           # never trim more than this fraction off one side
BG_BLUR_DOWN   = 10             # background blur strength (bigger = blurrier; was 16, too strong)
BG_BLUR_SIG    = 1.0            # light smoothing so the downscale blur isn't blocky
ASSETS_SUBDIR  = "05_slides_assets"  # under OUTPUT/; derived V3 stills land here
# PNG pHYs density encoding PAR 1.333 (ppu_y/ppu_x = 4/3) so Premiere reads the
# 1440x1080 still as anamorphic (PAR 1.33), not square (1.0) — it takes a still's
# PAR from the file, ignoring the XML hint.
PHYS_DPI = (72, 96)


def _strip_bars(bgr):
    """Trim uniform (solid-colour) borders — the slides_to_tv pillarbox bars — to
    recover the real slide content. Works for black or white bars."""
    h, w = bgr.shape[:2]
    a = bgr.astype(np.int16)

    def uniform(line):
        return np.abs(line - line.mean(axis=0)).mean() < BAR_TRIM_TOL

    l = 0
    while l < int(w * BAR_TRIM_MAX) and uniform(a[:, l, :]):
        l += 1
    r = w
    while r > int(w * (1 - BAR_TRIM_MAX)) and uniform(a[:, r - 1, :]):
        r -= 1
    t = 0
    while t < int(h * BAR_TRIM_MAX) and uniform(a[t, :, :]):
        t += 1
    b = h
    while b > int(h * (1 - BAR_TRIM_MAX)) and uniform(a[b - 1, :, :]):
        b -= 1
    if r - l < w * 0.3 or b - t < h * 0.3:      # detection went haywire -> keep all
        return bgr
    return bgr[t:b, l:r]


def _fit(img, w, h):
    """Resize to CONTAIN (w,h) preserving aspect; returns (resized, x, y)."""
    ih, iw = img.shape[:2]
    s = min(w / iw, h / ih)
    nw, nh = max(1, round(iw * s)), max(1, round(ih * s))
    out = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
    return out, (w - nw) // 2, (h - nh) // 2


def _cover_blur(img, w, h):
    """Resize to COVER (w,h), centre-crop, then a moderate smooth blur (downscale/up)."""
    ih, iw = img.shape[:2]
    s = max(w / iw, h / ih)
    nw, nh = max(1, round(iw * s)), max(1, round(ih * s))
    big = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_CUBIC)
    x, y = (nw - w) // 2, (nh - h) // 2
    crop = big[y:y + h, x:x + w]
    small = cv2.resize(crop, (w // BG_BLUR_DOWN, h // BG_BLUR_DOWN), interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (0, 0), BG_BLUR_SIG)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)


def render_v3_still(deck_png: Path, out_png: Path):
    """deck PNG -> 1440x1080-anamorphic still: sharp content centred over a blurred
    upscaled background (no bars), squeezed to the sequence's pixel geometry, and
    tagged (pHYs) so Premiere reads it at PAR 1.33."""
    img = cv2.imread(str(deck_png))
    content = _strip_bars(img)
    canvas = _cover_blur(content, DISP_W, DISP_H)
    fg, x, y = _fit(content, DISP_W, DISP_H)
    canvas[y:y + fg.shape[0], x:x + fg.shape[1]] = fg          # sharp slide on top
    squeezed = cv2.resize(canvas, (SEQ_W, SEQ_H), interpolation=cv2.INTER_AREA)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(cv2.cvtColor(squeezed, cv2.COLOR_BGR2RGB)).save(out_png, dpi=PHYS_DPI)

CUE_PHRASES = [
    "comme vous voyez", "vous voyez ici", "vous voyez sur", "sur cette photo",
    "sur cette image", "sur ce graphique", "sur ce tableau", "ce tableau",
    "l'illustration", "sur cette diapo", "ici on voit", "on voit ici", "regardez",
]


# ---------------------------------------------------------------------------
# DIV track / sampling
# ---------------------------------------------------------------------------

def div_clips(seq: ET.Element) -> list:
    """Enabled DIV-CAM video clipitems as (timeline_start, source_in, dur, mxf)."""
    fmap = _file_pathurl_map(seq)
    vid = seq.find("media/video")
    for track in vid.findall("track"):
        items = track.findall("clipitem")
        if items and "DIV%20CAM" in (_clip_pathurl(items[0], fmap) or ""):
            out = []
            for it in items:
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
    return []


def grab_div_frame(clips: list, target_tf: int):
    """Direct-seek one full-res (1920x1080) DIV frame at a timeline frame."""
    for (t_s, s_in, dur, mxf) in clips:
        if t_s <= target_tf < t_s + dur:
            sub_mxf, local_in, _ = _resolve_p2_chain(mxf, s_in + (target_tf - t_s), 1)[0]
            if not sub_mxf.exists():
                return None
            cmd = [ffmpeg(), "-nostdin", "-loglevel", "error",
                   "-ss", str(local_in / FPS), "-i", str(sub_mxf),
                   "-frames:v", "1", "-vf", "scale=1920:1080",
                   "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
            buf = subprocess.run(cmd, capture_output=True).stdout
            if len(buf) < 1920 * 1080 * 3:
                return None
            return np.frombuffer(buf, np.uint8).reshape(1080, 1920, 3)
    return None


# ---------------------------------------------------------------------------
# Rectify / features
# ---------------------------------------------------------------------------

def parse_quad(s: str, scale: float = 1.0) -> np.ndarray:
    vals = [float(x) for x in s.split(",")]
    if len(vals) != 8:
        sys.exit("[ERROR] --screen-quad needs 8 numbers: x1,y1,x2,y2,x3,y3,x4,y4 (tl tr br bl)")
    pts = np.array(vals, dtype=np.float32).reshape(4, 2) * scale
    return pts


def rectify(bgr: np.ndarray, quad: np.ndarray, w: int, h: int) -> np.ndarray:
    dst = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float32)
    return cv2.warpPerspective(bgr, cv2.getPerspectiveTransform(quad, dst), (w, h))


def diff_gray(bgr_rect: np.ndarray) -> np.ndarray:
    g = cv2.cvtColor(cv2.resize(bgr_rect, (DIFF_W, DIFF_H)), cv2.COLOR_BGR2GRAY)
    return cv2.GaussianBlur(g, (5, 5), 0)


def changed_frac(a: np.ndarray, b: np.ndarray) -> float:
    return float((cv2.absdiff(a, b) > DIFF_THRESH).mean())


# ---------------------------------------------------------------------------
# Pass 1 — change-detection -> segments
# ---------------------------------------------------------------------------

def detect_segments(clips, quad_full, sample_fps, total_frames, start_f=None, end_f=None):
    """Stream DIV, frame-diff the rectified screen, persistence-debounce -> list of
    (start_tf, end_tf) slide segments covering the talk."""
    scale = DECODE_SCALE
    quad = quad_full * (scale / 1920.0)
    persist = max(1, round(PERSIST_S * sample_fps))

    win = (end_f if end_f is not None else total_frames) - (start_f or 0)
    rep = ProgressReporter(OUTPUT_DIR / "05_slides.xml",
                           total=max(1, int(win / FPS * sample_fps)),
                           label="place_slides")
    segments = []
    ref = None
    seg_start = None
    cand = None              # (gray, tf, count)
    last_tf = 0
    n = 0
    for tf, frame in iter_main_frames(clips, sample_fps, scale, start_f=start_f, end_f=end_f):
        g = diff_gray(rectify(frame, quad, RECT_W, RECT_H))
        last_tf = tf
        n += 1
        if n % 200 == 0:
            rep.update(n)
        if ref is None:
            ref = g
            seg_start = tf
            continue
        if changed_frac(g, ref) <= CHANGE_FRAC:
            cand = None                      # same slide; drop any candidate
            continue
        # differs from current slide -> candidate for a new slide
        if cand is None or changed_frac(g, cand[0]) > CHANGE_FRAC:
            cand = (g, tf, 1)                # (re)start candidate on fresh content
        else:
            cand = (cand[0], cand[1], cand[2] + 1)
        if cand[2] >= persist:               # new content held long enough -> confirm
            segments.append((seg_start, cand[1]))
            seg_start = cand[1]
            ref = cand[0]
            cand = None
    if seg_start is not None:
        segments.append((seg_start, last_tf))
    rep.done(message=f"{len(segments)} raw segments")
    return segments


# ---------------------------------------------------------------------------
# Pass 2 — ORB identity per segment
# ---------------------------------------------------------------------------

def load_deck(slides_dir: Path):
    manifest = json.loads((slides_dir / "manifest.json").read_text(encoding="utf-8"))
    orb = cv2.ORB_create(nfeatures=ORB_FEATURES)
    deck = []
    for s in manifest["slides"]:
        png = slides_dir / s["png"]
        g = cv2.cvtColor(cv2.imread(str(png)), cv2.COLOR_BGR2GRAY)
        kp, des = orb.detectAndCompute(cv2.resize(g, (RECT_W, RECT_H)), None)
        deck.append({"n": s["n"], "png": s["png"], "kp": kp, "des": des})
    return manifest, deck, orb


def orb_identity(rect_gray, deck, orb, bf):
    kp_q, des_q = orb.detectAndCompute(rect_gray, None)
    if des_q is None or len(kp_q) < 8:
        return None, 0, []
    scores = []
    for d in deck:
        if d["des"] is None or len(d["kp"]) < 8:
            scores.append(0)
            continue
        good = [m for m, nn in (mn for mn in bf.knnMatch(des_q, d["des"], k=2) if len(mn) == 2)
                if m.distance < 0.75 * nn.distance]
        scores.append(len(good))
    order = np.argsort(scores)[::-1]
    best = deck[order[0]]
    return best, scores[order[0]], [(deck[i]["n"], scores[i]) for i in order[:3]]


def identify_segments(clips, segments, quad_full, deck, orb):
    bf = cv2.BFMatcher(cv2.NORM_HAMMING)
    out = []
    for (s, e) in segments:
        mid = (s + e) // 2
        frame = grab_div_frame(clips, mid)
        if frame is None:
            out.append({"start": s, "end": e, "slide": None, "inliers": 0, "top3": []})
            continue
        rg = cv2.cvtColor(rectify(frame, quad_full, RECT_W, RECT_H), cv2.COLOR_BGR2GRAY)
        best, inl, top3 = orb_identity(rg, deck, orb, bf)
        out.append({"start": s, "end": e,
                    "slide": best["n"] if best else None,
                    "png": best["png"] if best else None,
                    "inliers": int(inl), "top3": top3})
    return out


def merge_same_slide(segs):
    """Collapse consecutive segments that ORB maps to the same slide (animation
    builds / re-detections of one slide)."""
    merged = []
    for sg in segs:
        if (merged and sg["slide"] is not None
                and sg["slide"] == merged[-1]["slide"]):
            merged[-1]["end"] = sg["end"]
            merged[-1]["inliers"] = max(merged[-1]["inliers"], sg["inliers"])
        else:
            merged.append(dict(sg))
    return merged


# ---------------------------------------------------------------------------
# Transcript helpers
# ---------------------------------------------------------------------------

def load_words(path: Path):
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("words", [])
    except Exception:
        return []


def snap_to_speech(words, t):
    for w in words:
        if w.get("start_frame", -1) >= t:
            return w["start_frame"]
    return t


def words_between(words, a, b):
    return [w for w in words if a <= w.get("start_frame", -1) < b]


def cue_hit(words, start):
    near = words_between(words, start - 2 * FPS, start + 5 * FPS)
    text = " ".join(w["word"].lower() for w in near)
    return any(p in text for p in CUE_PHRASES)


# ---------------------------------------------------------------------------
# Cutaway building + V3 emission
# ---------------------------------------------------------------------------

def build_cutaways(segs, words):
    cutaways, skipped = [], []
    for sg in segs:
        s, e = sg["start"], sg["end"]
        dwell = e - s
        if dwell < MIN_DWELL_S * FPS:
            skipped.append({**sg, "reason": f"dwell {dwell/FPS:.1f}s < {MIN_DWELL_S}s"})
            continue
        start = snap_to_speech(words, s) if words else s
        if start >= e:
            start = s
        n_words = len(words_between(words, s, e))
        dur = min(CAP_S * FPS, max(FLOOR_S * FPS, WORDS_RATE_S * n_words * FPS))
        dur = int(min(dur, e - start))          # never overrun the on-screen gap
        if dur < FLOOR_S * FPS and dur < dwell:
            dur = int(min(FLOOR_S * FPS, e - start))
        cutaways.append({
            "slide": sg["slide"], "png": sg.get("png"),
            "inliers": sg["inliers"], "confident": sg["inliers"] >= ORB_MIN,
            "seg_start": s, "seg_end": e, "start_frame": int(start),
            "duration_frames": int(dur), "duration_s": round(dur / FPS, 1),
            "words": n_words, "cue": cue_hit(words, start) if words else False,
            "ordinal_outlier": False,
            "top3": sg.get("top3", []),
        })
    flag_ordinal_outliers(cutaways)
    return cutaways, skipped


def flag_ordinal_outliers(cutaways):
    """The deck is presented in order, so a placed id that jumps *backwards* below
    the running max of confident ids is almost certainly a mislabel — flag it for
    the editor even when ORB called it confident (identity is best-effort)."""
    run_max = 0
    for c in cutaways:
        sid = c["slide"]
        if sid is None:
            continue
        if sid < run_max:
            c["ordinal_outlier"] = True
        if c["confident"]:
            run_max = max(run_max, sid)
    return cutaways


def _next_file_id(seq):
    mx = 0
    for f in seq.iter("file"):
        fid = (f.get("id") or "")
        if fid.startswith("file-"):
            try:
                mx = max(mx, int(fid.split("-")[1]))
            except (ValueError, IndexError):
                pass
    return mx + 1


def _still_clipitem(cid, fid, name, png_path, t_start, dur):
    url = pathurl(png_path)
    t_end = t_start + dur
    # 1440x1080 / HD-(1440x1080) PAR — identical geometry to the footage, so the
    # still fills the frame at 100% with no Scale-to-Frame and no 133% stretch.
    file_el = (
        f'<file id="file-{fid}">'
        f"<name>{name}</name><pathurl>{url}</pathurl>{_rate()}"
        f"<duration>{STILL_FILE_DUR}</duration>"
        f"<timecode>{_rate()}<string>00:00:00:00</string>"
        f"<frame>0</frame><displayformat>NDF</displayformat></timecode>"
        f"<media><video><samplecharacteristics>{_rate()}"
        f"<width>{SEQ_W}</width><height>{SEQ_H}</height>"
        f"<anamorphic>FALSE</anamorphic><pixelaspectratio>HD-(1440x1080)</pixelaspectratio>"
        f"<fielddominance>none</fielddominance>"
        f"</samplecharacteristics></video></media></file>"
    )
    return (
        f'<clipitem id="clipitem-{cid}">'
        f"<masterclipid>masterclip-{cid}</masterclipid>"
        f"<name>{name}</name>"
        f"<enabled>TRUE</enabled><duration>{STILL_FILE_DUR}</duration>"
        f"{_rate()}"
        f"<start>{t_start}</start><end>{t_end}</end>"
        f"<in>0</in><out>{dur}</out>"
        f"<pproTicksIn>0</pproTicksIn><pproTicksOut>{dur * TICKS_PER_FRAME}</pproTicksOut>"
        f"<alphatype>none</alphatype>"
        f"<pixelaspectratio>HD-(1440x1080)</pixelaspectratio><anamorphic>FALSE</anamorphic>"
        f"{file_el}{_logginginfo()}{_colorinfo()}"
        f"<labels><label2>Tangerine</label2></labels>"
        f"</clipitem>"
    )


def emit_v3(seq, cutaways, slides_dir, assets_dir):
    # scope-clean our own derived assets, then render one blurred-bg still per cutaway
    assets_dir.mkdir(parents=True, exist_ok=True)
    for old in assets_dir.glob("*.png"):
        old.unlink()
    cid = _next_id(seq)
    fid = _next_file_id(seq)
    items = []
    for c in cutaways:
        if c["png"] is None:
            continue
        asset = (assets_dir / f"slide_{c['slide']:03d}.png").resolve()
        render_v3_still(slides_dir / c["png"], asset)
        name = f"slide_{c['slide']:03d}"
        items.append(_still_clipitem(cid, fid, name, asset,
                                     c["start_frame"], c["duration_frames"]))
        cid += 1
        fid += 1
    track_xml = (
        '<track TL.SQTrackShy="0" TL.SQTrackExpandedHeight="25" '
        'TL.SQTrackExpanded="0" MZ.TrackTargeted="0">'
        + "".join(items)
        + "<enabled>TRUE</enabled><locked>FALSE</locked></track>"
    )
    seq.find("media/video").append(ET.fromstring(track_xml))
    return len(items)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Stage 5: place deck slides on V3, timed to the talk.")
    ap.add_argument("--input",      default=str(OUTPUT_DIR / "04_angles.xml"))
    ap.add_argument("--slides",     default=str(OUTPUT_DIR / "slides" / "Tallier_Communication_Marche_avril2026"))
    ap.add_argument("--transcript", default=str(OUTPUT_DIR / "04_transcript.json"))
    ap.add_argument("--output",     default=str(OUTPUT_DIR / "05_slides.xml"))
    ap.add_argument("--report",     default=str(OUTPUT_DIR / "05_slides_report.json"))
    ap.add_argument("--screen-quad", default=DEFAULT_QUAD,
                    help="8 ints x1,y1,...,x4,y4 (tl tr br bl) in 1920x1080 space")
    ap.add_argument("--sample-fps", type=float, default=2.0)
    ap.add_argument("--start", type=float, default=None, metavar="s",
                    help="Only analyse from this second (testing / sub-range rerun)")
    ap.add_argument("--end", type=float, default=None, metavar="s",
                    help="Only analyse up to this second")
    args = ap.parse_args()

    input_path  = Path(args.input)
    slides_dir  = Path(args.slides)
    output_path = Path(args.output)
    if not input_path.exists():
        sys.exit(f"[ERROR] Input not found: {input_path}")
    if not (slides_dir / "manifest.json").exists():
        sys.exit(f"[ERROR] Slides manifest not found: {slides_dir / 'manifest.json'}")

    tree = ET.parse(input_path)
    seq = tree.getroot().find("sequence")
    if seq is None:
        sys.exit("[ERROR] No <sequence> found.")
    clips = div_clips(seq)
    if not clips:
        sys.exit("[ERROR] No enabled DIV CAM video clips found.")
    total_frames = max(int(seq.find("duration").text),
                       max(t + d for (t, _, d, _) in clips))
    quad_full = parse_quad(args.screen_quad)
    start_f = int(args.start * FPS) if args.start is not None else None
    end_f   = int(args.end   * FPS) if args.end   is not None else None

    print(f"[1/5] DIV CAM: {len(clips)} clips, {total_frames/FPS/60:.1f} min; "
          f"sampling at {args.sample_fps} fps"
          + (f" (window {args.start}-{args.end}s)" if start_f or end_f else ""))
    segments = detect_segments(clips, quad_full, args.sample_fps, total_frames, start_f, end_f)
    print(f"      {len(segments)} raw slide segments")

    print(f"[2/5] Loading deck ({slides_dir.name}) + ORB features ...")
    manifest, deck, orb = load_deck(slides_dir)
    print(f"      {len(deck)} reference slides")

    print("[3/5] ORB identity per segment ...")
    idsegs = identify_segments(clips, segments, quad_full, deck, orb)
    idsegs = merge_same_slide(idsegs)
    print(f"      {len(idsegs)} segments after same-slide merge")

    print("[4/5] Building cutaways ...")
    words = load_words(Path(args.transcript))
    if not words:
        print("      [WARN] no transcript words — duration uses floor, no speech-snap")
    cutaways, skipped = build_cutaways(idsegs, words)
    placed = [c for c in cutaways if c["png"] is not None]
    confident = [c for c in placed if c["confident"]]
    print(f"      {len(placed)} cutaways ({len(confident)} confident), "
          f"{len(skipped)} skipped (short dwell)")

    print("[5/5] Rendering blurred-bg V3 stills + emitting ...")
    n_emit = emit_v3(seq, cutaways, slides_dir, OUTPUT_DIR / ASSETS_SUBDIR)
    seq.find("name").text = "place_slides"
    OUTPUT_DIR.mkdir(exist_ok=True)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)

    placed_ids = {c["slide"] for c in placed}
    report = {
        "source": str(input_path), "slides": str(slides_dir),
        "transcript_words": len(words), "sample_fps": args.sample_fps,
        "screen_quad": args.screen_quad,
        "n_cutaways": n_emit, "n_confident": len(confident),
        "cutaways": cutaways, "skipped_short_dwell": skipped,
        "unplaced_deck_slides": [s["n"] for s in manifest["slides"] if s["n"] not in placed_ids],
        "low_confidence": [c["slide"] for c in placed if not c["confident"]],
        "ordinal_outliers": [{"slide": c["slide"], "at_s": round(c["start_frame"] / FPS, 0)}
                             for c in placed if c["ordinal_outlier"]],
    }
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    print(f"[OK] Written: {output_path}")
    print(f"[OK] Written: {args.report}")
    print(f"     placed {n_emit} slides on V3; unplaced deck slides: "
          f"{len(report['unplaced_deck_slides'])}; low-confidence: {len(report['low_confidence'])}; "
          f"ordinal-outliers (likely mislabels): {len(report['ordinal_outliers'])}")


if __name__ == "__main__":
    main()
