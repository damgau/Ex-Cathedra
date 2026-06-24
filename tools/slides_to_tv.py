#!/usr/bin/env python3
"""
Tool: slides_to_tv.py
Purpose: Turn a speaker's support deck — a PDF, a PPTX, or a folder of exported
         JPEGs — into a consistent set of broadcast-ready **1920x1080 square-pixel
         PNGs**, reframed to 16:9 by padding (never stretching). Also writes a
         manifest.json recording the slide->source mapping, the bar colour, and
         each page's extracted text (to feed the future transcript->slide matcher).
Inputs:  --input   a PDF file, a PPTX file, or a directory of images (auto-detected)
         --output  output dir (default OUTPUT/slides/<deck-slug>/)
         --fill    auto | white | black | #RRGGBB  (default auto)
Output:  001.png ... NNN.png (1920x1080 RGB) + manifest.json in the output dir.

The 1440x1080-anamorphic PAR used by tools/create_xml.py is a video-track/source-MXF
concern; stills are square-pixel so they stay correct in preview/reuse/archive. The
workflow documents the Premiere import setting that displays them as 16:9.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata
from functools import lru_cache
from glob import glob
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv

load_dotenv()

# Force UTF-8 stdout so accented deck names ("pelerinage") survive Windows cp1252.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from PIL import Image
import numpy as np

BASE_DIR = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "OUTPUT"
TMP_DIR = BASE_DIR / ".tmp"

# --- Documented internal constants (decision 9: promote to a flag only if a run
#     actually needs to turn it). -------------------------------------------------
TARGET_W, TARGET_H = 1920, 1080      # square-pixel 16:9 canvas (decision 1)
FPS = 25                              # sequence frame rate, recorded in manifest
EDGE_STRIP_FRAC = 0.025              # thin edge strip sampled for bar colour (~2.5%)
UNIFORM_STD_MAX = 10.0              # per-channel std below this == "uniform edge"
EDGE_MATCH_MAX = 12                  # max per-channel |delta| for the two edges to share a median
LUMA_SPLIT = 128.0                   # mean luminance >= this == light slide -> white bars
SOFT_FLOOR_PX = 1280                 # warn when a raster source's binding dim is below this
SOFFICE_TIMEOUT = 180                # PPTX->PDF convert timeout (first convert is slow)

IMG_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
BIDI_MARKS = {"‎", "‏"}    # LTR / RTL marks polluting exported filenames


# ---------------------------------------------------------------------------
# soffice resolver — ffbin.py-style (decision 5): lazy + cached, never hardcoded.
# ---------------------------------------------------------------------------

@lru_cache(maxsize=None)
def soffice() -> str:
    """Resolve the LibreOffice `soffice` binary. Order: SOFFICE_BIN (.env) ->
    PATH -> known install glob (Windows + macOS) -> guided error. Lazy so PDF /
    image-folder input never pays for it.

    On Windows we deliberately prefer **soffice.com** over soffice.exe: the .exe
    is a launcher that detaches and returns 0 *before* the conversion runs (so the
    output PDF never appears in time / at all), while the .com console wrapper
    blocks until conversion completes and returns the real exit code."""
    import shutil

    env = os.environ.get("SOFFICE_BIN")
    if env and Path(env).exists():
        return env

    names = ["soffice.com", "soffice"] if os.name == "nt" else ["soffice"]
    for nm in names:
        hit = shutil.which(nm)
        if hit:
            if os.name == "nt" and hit.lower().endswith(".exe"):
                com = Path(hit).with_suffix(".com")   # swap .exe -> blocking .com
                if com.exists():
                    return str(com)
            return hit

    candidates = [
        r"C:\Program Files\LibreOffice\program\soffice.com",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.com",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    ]
    for pat in candidates:
        hits = sorted(glob(pat))
        if hits:
            return hits[-1]

    sys.exit(
        "[ERROR] LibreOffice (soffice) not found — required to convert PPTX to PDF.\n"
        "        Install LibreOffice, or set SOFFICE_BIN in .env to soffice's full path.\n"
        "        (PDF and image-folder inputs do not need it.)"
    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def slugify(stem: str) -> str:
    """Filesystem-safe slug of a deck name (drops accents, bidi marks, punctuation)."""
    s = "".join(c for c in stem if c not in BIDI_MARKS)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\-]+", "_", s).strip("_")
    return s or "deck"


def _natural_key(name: str):
    """Natural sort: split into text/number runs so 2 < 10, padded or not."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _strip_bidi(name: str) -> str:
    return "".join(c for c in name if c not in BIDI_MARKS)


def parse_fill(val: str):
    """Return ('auto', None) or ('fixed', (r, g, b))."""
    v = (val or "auto").strip().lower()
    if v == "auto":
        return "auto", None
    if v == "white":
        return "fixed", (255, 255, 255)
    if v == "black":
        return "fixed", (0, 0, 0)
    m = re.fullmatch(r"#?([0-9a-f]{6})", v)
    if m:
        h = m.group(1)
        return "fixed", (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    sys.exit(f"[ERROR] --fill must be auto|white|black|#RRGGBB (got: {val!r})")


def _hex(rgb) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


# ---------------------------------------------------------------------------
# Ingest — each source yields slide dicts {source_page, source_file, img, text,
# is_raster} plus a list of skipped {source_page, source_file, reason}.
# ---------------------------------------------------------------------------

def ingest_pdf(path: Path):
    """Rasterize each page to its *contained* 1920x1080 size (vector -> sharp at
    any size) and pull the text layer for free. Per-page render failure ->
    skip-and-warn (decision 7)."""
    import pypdfium2 as pdfium  # lazy: image-folder input never imports it

    pdf = pdfium.PdfDocument(str(path))
    slides, skipped = [], []
    for i in range(len(pdf)):
        n = i + 1
        try:
            page = pdf[i]
            w_pt, h_pt = page.get_size()
            scale = min(TARGET_W / w_pt, TARGET_H / h_pt)  # render at contained size
            img = page.render(scale=scale).to_pil().convert("RGB")
            try:
                tp = page.get_textpage()
                text = tp.get_text_range().strip()
                tp.close()
            except Exception:
                text = ""
            page.close()
        except Exception as exc:
            print(f"  [WARN] page {n}: render failed ({exc}) — skipping")
            skipped.append({"source_page": n, "source_file": None, "reason": str(exc)})
            continue
        slides.append({"source_page": n, "source_file": None, "img": img,
                       "text": text, "is_raster": False})
    pdf.close()
    return slides, skipped


def ingest_pptx(path: Path):
    """PPTX -> PDF via headless LibreOffice, then rasterize the PDF. soffice
    failure/timeout -> hard abort (decision 7).

    Windows specifics learned the hard way: (1) the binary must be **soffice.com**,
    not soffice.exe (resolved by soffice()); (2) paths are passed **absolute** —
    soffice resolves a relative --outdir against its own cwd, not ours; (3) a
    throwaway `-env:UserInstallation` profile was dropped — on a cold run soffice
    only *initialises* the fresh profile and silently skips the conversion, so the
    default profile is used instead (a concurrent open LibreOffice would lock it ->
    covered by the guided error below)."""
    TMP_DIR.mkdir(exist_ok=True)
    out_dir = TMP_DIR.resolve()              # absolute: soffice resolves --outdir against its own cwd
    cmd = [
        soffice(), "--headless", "--norestore",
        "--convert-to", "pdf", "--outdir", str(out_dir), str(path.resolve()),
    ]
    print(f"[ingest] LibreOffice: PPTX -> PDF (timeout {SOFFICE_TIMEOUT}s, first convert is slow)...")
    try:
        proc = subprocess.run(cmd, timeout=SOFFICE_TIMEOUT,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired:
        sys.exit(f"[ERROR] LibreOffice PPTX->PDF timed out after {SOFFICE_TIMEOUT}s. "
                 "Close any open LibreOffice window and retry.")

    # Success is judged by the PDF existing, not the exit code: Windows soffice
    # prints a harmless "Document is empty" parser warning and can return a quirky
    # status even on a clean convert. No PDF == real failure -> hard abort.
    pdf_path = out_dir / (path.stem + ".pdf")
    if not pdf_path.exists():
        err = (proc.stderr.decode("utf-8", "replace").strip()
               or proc.stdout.decode("utf-8", "replace").strip())
        sys.exit(f"[ERROR] LibreOffice PPTX->PDF produced no PDF (exit {proc.returncode}). "
                 "If LibreOffice is open, close it and retry.\n        " + err)
    return ingest_pdf(pdf_path)


def ingest_images(d: Path):
    """Glob a folder, filter junk by *open-attempt* (cheap dotfile skip first),
    strip bidi marks, natural-sort, warn if no numeric sequence (decision 6).
    Open-failures are treated as junk (AppleDouble/corrupt), not skipped slides —
    an image folder has no separable 'page' that should have rendered."""
    entries = []
    for name in os.listdir(d):
        if name.startswith("."):            # ._AppleDouble, .DS_Store, dotfiles
            continue
        p = d / name
        if p.is_file():
            entries.append((_strip_bidi(name), name, p))
    entries.sort(key=lambda e: _natural_key(e[0]))

    if entries and not any(re.search(r"\d", clean) for clean, _, _ in entries):
        print("[WARN] no parseable numeric sequence in filenames — slide order is "
              "filesystem order and may be wrong.")

    slides = []
    n = 0
    for clean, orig, p in entries:
        if p.suffix.lower() not in IMG_SUFFIXES:
            # Try anyway only if it has no suffix; otherwise it's clearly not ours.
            if p.suffix:
                continue
        try:
            img = Image.open(p)
            img.load()
            img = img.convert("RGB")
        except Exception:
            continue                         # junk (open-attempt filter)
        n += 1
        slides.append({"source_page": n, "source_file": orig, "img": img,
                       "text": "", "is_raster": True})   # no text layer in images
    return slides, []


# ---------------------------------------------------------------------------
# Reframe — contain (no stretch) onto a 1920x1080 canvas with one shared,
# symmetric bar colour.
# ---------------------------------------------------------------------------

def _bar_color_auto(arr: np.ndarray, pillarbox: bool):
    """Pick one shared symmetric bar colour from the slide's two abutting edges
    (decision 2 & 3). If both edges are uniform AND close -> their combined
    median (seamless on uniform decks); else luminance-adaptive white/black."""
    h, w = arr.shape[:2]
    if pillarbox:                            # bars left|right -> sample vertical edge strips
        sw = max(1, round(w * EDGE_STRIP_FRAC))
        strips = [arr[:, :sw], arr[:, w - sw:]]
    else:                                    # bars top/bottom -> sample horizontal edge strips
        sh = max(1, round(h * EDGE_STRIP_FRAC))
        strips = [arr[:sh, :], arr[h - sh:, :]]

    flats = [s.reshape(-1, 3).astype(np.float64) for s in strips]
    uniform = [bool(np.all(f.std(axis=0) < UNIFORM_STD_MAX)) for f in flats]
    medians = [np.median(f, axis=0) for f in flats]

    if all(uniform) and np.all(np.abs(medians[0] - medians[1]) < EDGE_MATCH_MAX):
        combined = np.concatenate(flats, axis=0)
        return tuple(int(round(v)) for v in np.median(combined, axis=0))

    # luminance-adaptive fallback over the whole slide (Rec.709 luma)
    luma = float(np.mean(arr[..., :3].astype(np.float64) @ [0.2126, 0.7152, 0.0722]))
    return (255, 255, 255) if luma >= LUMA_SPLIT else (0, 0, 0)


def reframe(img: Image.Image, is_raster: bool, fill_mode, fill_color):
    """Return (canvas 1920x1080, bar_color_or_None, soft_warn_bool)."""
    w, h = img.size
    scale = min(TARGET_W / w, TARGET_H / h)
    cw = max(1, min(TARGET_W, round(w * scale)))
    ch = max(1, min(TARGET_H, round(h * scale)))

    # soft-quality warning: only for raster sources we had to upscale (decision 4).
    binding_src = w if (TARGET_W / w) <= (TARGET_H / h) else h
    soft = bool(is_raster and scale > 1.0 and binding_src < SOFT_FLOOR_PX)

    resized = img if (cw, ch) == (w, h) else img.resize((cw, ch), Image.LANCZOS)

    if (cw, ch) == (TARGET_W, TARGET_H):     # exact 16:9 -> no bars
        return resized, None, soft

    if fill_mode == "fixed":
        bar = fill_color
    else:
        bar = _bar_color_auto(np.asarray(resized), pillarbox=(cw < TARGET_W))

    canvas = Image.new("RGB", (TARGET_W, TARGET_H), bar)
    canvas.paste(resized, ((TARGET_W - cw) // 2, (TARGET_H - ch) // 2))
    return canvas, bar, soft


# ---------------------------------------------------------------------------
# Output dir — scoped clean-before-write (decision 8).
# ---------------------------------------------------------------------------

def _is_own(p: Path) -> bool:
    return p.is_file() and (p.suffix.lower() == ".png" or p.name == "manifest.json")


def prepare_output(out_dir: Path):
    if out_dir.exists():
        foreign = [p for p in out_dir.iterdir() if not _is_own(p)]
        if foreign:
            names = ", ".join(p.name for p in foreign[:5])
            sys.exit(f"[ERROR] Output dir {out_dir} contains foreign files ({names}...). "
                     "Refusing to delete. Point --output at a fresh/own dir.")
        for p in out_dir.iterdir():
            if _is_own(p):
                p.unlink()
    else:
        out_dir.mkdir(parents=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def detect_source(path: Path) -> str:
    if path.is_dir():
        return "images"
    suf = path.suffix.lower()
    if suf == ".pdf":
        return "pdf"
    if suf == ".pptx":
        return "pptx"
    sys.exit(f"[ERROR] Unsupported input: {path} (expected a .pdf, .pptx, or image folder).")


def main():
    parser = argparse.ArgumentParser(
        description="Convert a slide deck (PDF / PPTX / image folder) into 1920x1080 "
                    "square-pixel PNG stills for Premiere, padding to 16:9 (no stretch)."
    )
    parser.add_argument("--input", required=True, help="A .pdf, a .pptx, or a folder of images")
    parser.add_argument("--output", default=None, help="Output dir (default OUTPUT/slides/<deck-slug>/)")
    parser.add_argument("--fill", default="auto", help="auto | white | black | #RRGGBB (default auto)")
    args = parser.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        sys.exit(f"[ERROR] Input not found: {in_path}")

    fill_mode, fill_color = parse_fill(args.fill)
    kind = detect_source(in_path)
    deck = in_path.name if kind == "images" else in_path.stem
    out_dir = Path(args.output) if args.output else (OUTPUT_DIR / "slides" / slugify(deck))

    print(f"[1/4] Input: {in_path}  ({kind}); fill={args.fill}")
    if kind == "pdf":
        slides, skipped = ingest_pdf(in_path)
    elif kind == "pptx":
        slides, skipped = ingest_pptx(in_path)
    else:
        slides, skipped = ingest_images(in_path)

    if not slides:
        sys.exit("[ERROR] No slides ingested — nothing to write.")
    print(f"[2/4] Ingested {len(slides)} slide(s)"
          + (f"; {len(skipped)} skipped" if skipped else ""))

    prepare_output(out_dir)

    print(f"[3/4] Reframing to {TARGET_W}x{TARGET_H} -> {out_dir}")
    pad = max(3, len(str(len(slides))))
    soft_warns = 0
    records = []
    for i, sl in enumerate(slides, start=1):
        try:
            canvas, bar, soft = reframe(sl["img"], sl["is_raster"], fill_mode, fill_color)
        except Exception as exc:
            print(f"  [WARN] slide from source #{sl['source_page']}: reframe failed "
                  f"({exc}) — skipping")
            skipped.append({"source_page": sl["source_page"],
                            "source_file": sl["source_file"], "reason": str(exc)})
            continue
        if soft:
            soft_warns += 1
        png = f"{len(records) + 1:0{pad}d}.png"   # contiguous renumber over survivors
        canvas.save(out_dir / png)
        records.append({
            "n": len(records) + 1,
            "png": png,
            "source_page": sl["source_page"],
            "source_file": sl["source_file"],
            "bar_color": _hex(bar) if bar else None,
            "text": sl["text"],
        })

    if soft_warns:
        print(f"  [WARN] {soft_warns} slide(s) upscaled from a low-res source "
              f"(binding dim < {SOFT_FLOOR_PX}px) — may look soft on TV.")

    manifest = {
        "deck": deck,
        "source": str(in_path),
        "source_kind": kind,
        "fill_mode": args.fill,
        "fps": FPS,
        "size": [TARGET_W, TARGET_H],
        "skipped": skipped,
        "slides": records,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"[4/4] Wrote {len(records)} PNG(s) + manifest.json to {out_dir}")
    if skipped:
        print(f"      Skipped {len(skipped)} source page(s): "
              + ", ".join(str(s['source_page']) for s in skipped))
    print(f"[OK] {out_dir}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
