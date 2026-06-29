# Plan — Slides → TV-friendly stills for Premiere

**Status:** design finalized via grilling 2026-06-24 · build not started · **Scope:** converter only (V3 placement deferred)

## Context

During the conferences, speakers use a support deck. We receive it as a **PDF**, a **PPTX**, or
sometimes a **folder of exported JPEGs**. These need to become broadcast-ready stills we can drop
into the Premiere edit (eventually on **V3**, the slide track). Two problems:

1. **Aspect ratio.** Decks are often **4:3**; our sequence is **16:9** (1920×1080). We must reframe
   to 16:9 **without deforming** the slide — pad, never stretch.
2. **Format.** PDF / PPTX / JPEG-folder must all collapse to one consistent set of 1920×1080 PNGs
   that import cleanly into Premiere.

This builds the **converter only**. Transcript-driven placement of the stills onto V3 is specified
as a **future stage**, not built now.

## Decisions locked (grilling — 2026-06-24)

1. **Output pixel format — square-pixel 1920×1080 PNG**, *not* 1440×1080 anamorphic. Stills stay
   correct everywhere (preview, reuse, archive); the 1440-anamorphic PAR in `create_xml.py` is a
   video-track/source-MXF concern that stills don't share. The workflow documents the Premiere
   import setting that displays it as correct 16:9, and verification step #6 confirms it empirically.
2. **Pillarbox fill fallback — luminance-adaptive, not hardcoded white.** `auto` = uniform-edge
   median match → else pick **white for predominantly-light slides, black for predominantly-dark
   slides** (mean edge/slide luminance). Black is also the broadcast-standard neutral, so it never
   reads as a mistake. Explicit `--fill white|black|#hex` overrides detection.
3. **Single shared bar color, symmetric on both sides.** Use a per-edge median only when *both*
   edges are uniform **and** close to each other (seamless on uniform colored decks); otherwise both
   bars get the same luminance-adaptive fallback. This kills the "blue bar | slide | black bar"
   mismatch and drops the independent per-side complexity (it solved a near-nonexistent case).
4. **Low-res sources — fill-and-warn.** Scale to the contained target with Lanczos (upscaling if
   needed) and **warn** when the binding dimension is below ~1280px (soft on TV); never refuse,
   never shrink-and-bar. The render target is the **contained** size (≈1× oversample for vector
   sources), not "long edge ≥ 1920". *Parked fallback (B):* if softness disappoints, switch to
   cap-the-upscale and center the smaller image at native res with larger bars.
5. **LibreOffice (`soffice`) discovery — `ffbin.py`-style resolver, never hardcoded.** Cached order:
   `SOFFICE_BIN` (.env) → `shutil.which` on PATH → known-install glob (`C:\Program Files\LibreOffice\
   program\soffice.exe` **and** macOS `/Applications/LibreOffice.app/Contents/MacOS/soffice`) →
   guided error. Lazy so PDF / image-folder input never pays for it. Add `SOFFICE_BIN=` to
   `.env.example`. (Hardcoding is exactly the macOS↔Windows trap that already bit this project.)
6. **Image folders — open-attempt junk filter + natural-sort ordering.** Filter junk by *trying to
   open each file as an image with Pillow and skipping failures* (cheap `._`/dotfile skip first for
   speed); AppleDouble stubs, `.DS_Store`, `Thumbs.db` all drop out naming-independently. Order by
   **natural sort** (handles padded *and* unpadded numerics), strip U+200E/U+200F bidi marks first,
   and **warn loudly if filenames carry no parseable numeric sequence** (then "slide order" is just
   filesystem order and may be wrong).
7. **Failure granularity — hard-abort whole-deck, skip-and-warn per slide.** soffice failure/timeout
   (~180s timeout; first convert is slow) → hard abort with guided error (no partial deck possible).
   A single failed slide (corrupt JPEG, unrenderable PDF page) → **skip-and-warn and keep going**;
   renumber output PNGs **contiguously**, but record the true `source_page` and a `skipped` list in
   the manifest so the slide→source mapping survives for the future matcher.
8. **Re-run — scoped clean-before-write.** Delete only this tool's own files (`*.png` + `manifest.json`)
   in the resolved output dir, recreate if absent, then write fresh — every run is idempotent (no
   ghost slides). No recursive blast; if the resolved `--output` contains foreign files, **warn and
   refuse** rather than delete.
9. **CLI surface — lean.** `--input`, `--output`, `--fill {auto|white|black|#RRGGBB}` only. Every
   threshold (edge-strip width ~2–3%, uniformity std-dev ~10, white/black luminance split,
   soft-quality floor ~1280px, soffice timeout) is a **documented internal constant**, promoted to a
   flag only if verification shows it needs turning every run.

Earlier locked basics (unchanged): **WAT-native** — a `tools/` script + a `workflows/` SOP, like
every other stage. Converter now; V3 placement later.

## Environment findings (verified 2026-06-22)

- System **Python 3.14.5** has **Pillow 12.1**, **cv2 4.13**, **numpy 2.4** installed. The project
  `.venv` is broken (macOS origin) — use system Python.
- **Missing:** a PDF rasterizer. Plan adds **`pypdfium2`** (broad wheel coverage incl. 3.14, no
  system deps, renders straight to PIL). `python-pptx` is **not** needed.
- **LibreOffice is installed** at `C:\Program Files\LibreOffice\program\soffice.exe` — used headless
  for **PPTX → PDF**, then pypdfium2 rasterizes the PDF. Located via the `ffbin.py`-style resolver
  (decision 5), never hardcoded. No PowerPoint, no ImageMagick (the `convert` on PATH is Windows'
  NTFS tool — must not be called).
- The Premiere XML dialect (1920×1080 / 1440×1080 anamorphic / 25fps) is defined in
  `tools/create_xml.py` — relevant only to the future V3 stage.
- **Input data reality** (`INPUT/Powerpoint/`): one PPTX (`Le sens du pèlerinage chrétien.pptx`), one
  PDF (`Vieillissement…UTL Dinant…pdf`), one image folder (`Tallier_Communication_Marche_avril2026`
  = **46 real JPEGs + 46 AppleDouble `._` junk**, real names carry embedded **U+200E** bidi marks;
  `grep '^\._'` returns 0 because of them — proves name-prefix filtering is fragile, hence decision 6).

## Deliverables

### 1. `tools/slides_to_tv.py` (new)

Follow the repo idioms in `tools/detect_framing.py` and `tools/_template.py`: module docstring header
(Purpose / Inputs / Output), `sys.path.insert(... .parent.parent)` + `load_dotenv()`, **UTF-8 stdout
reconfigure** (deck names contain accents like "pèlerinage"), `BASE_DIR`/`OUTPUT_DIR` constants,
`argparse` `main()`, and the `try/except → ERROR/exit(1)` wrapper. `soffice` resolved via a cached
`ffbin.py`-style helper (decision 5).

**CLI**

```
python tools/slides_to_tv.py --input "<file.pdf | file.pptx | folder-of-images>" \
    [--output OUTPUT/slides/<deck-name>] [--fill auto|white|black|#RRGGBB]
```

- `--input` accepts a **PDF**, a **PPTX**, or a **directory of images** (auto-detected by suffix / `is_dir`).
- `--fill` default `auto` (uniform-edge median → luminance-adaptive white/black fallback). Explicit
  color/white/black overrides detection.
- Output defaults to `OUTPUT/slides/<deck-name>/` (slug of the input stem).
- All other tunables are documented internal constants (decision 9), not flags.

**Pipeline**

1. **Ingest → list of PIL.Image (slide order):**
   - *PDF:* `pdfium.PdfDocument(path)`; render each page so the **contained** size in 1920×1080 is met
     (≈1× oversample; vector renders sharp at any size), `bitmap.to_pil()`.
   - *PPTX:* `soffice --headless --convert-to pdf --outdir <.tmp> <pptx>` into `.tmp/`, using a
     throwaway profile dir via `-env:UserInstallation=file:///<.tmp/lo_profile>` to dodge a running-LO
     clash (~180s timeout; hard-abort on failure — decision 7); then rasterize the produced PDF as above.
   - *Image folder:* glob, **filter junk by open-attempt** (cheap `._`/dotfile skip first), strip
     U+200E/U+200F, **natural-sort**, warn if no numeric sequence (decision 6).
2. **Reframe each slide to 1920×1080 (contain, no stretch):**
   - Scale the slide to fit inside 1920×1080 preserving AR (Lanczos; upscale low-res sources and warn
     below ~1280px binding dimension — decision 4).
   - For the leftover margin, compute **one shared, symmetric bar color** (decision 3):
     - **`auto`:** sample a thin strip (~2–3% of the slide dimension) along each abutting edge → numpy;
       if **both** strips are uniform (per-channel std-dev < ~10) **and** close to each other, bar =
       their combined **median color**; else bar = **luminance-adaptive** white (light slide) / black
       (dark slide) — decision 2. Same color both sides.
     - explicit `white`/`black`/`#hex` skip detection.
   - Paste the centered slide onto the filled 1920×1080 canvas. 16:9 decks → no bars, just resize.
3. **Write** `001.png … NNN.png` (zero-padded, RGB, square-pixel) into the output dir, **renumbered
   contiguously** if any slide was skipped (decision 7). Output dir is **scope-cleaned before write**
   (own `*.png`/`manifest.json` only; refuse on foreign content — decision 8).
4. **Write `manifest.json`** in the output dir: `{deck, source, fill_mode, fps:25, skipped:[…],
   slides:[{n, png, source_page, bar_color, text}]}`. `source_page` is the *true* source index even
   when output is renumbered; `skipped` lists dropped source pages with the reason. `text` is the
   page's extracted text (free from pypdfium2 for PDF/PPTX-derived pages) — **stored now to feed the
   future transcript→slide matcher**, costs nothing. **Empty for image-folder input** (no text layer;
   OCR would be a future concern — decks that arrive as JPEGs can't be auto-placed on V3 by text).

Reuse Pillow + numpy already in the env; no new heavy code.

### 2. `workflows/slides_to_tv.md` (new)

Built from `workflows/_TEMPLATE.md`. Objective, the three input shapes, the `soffice` PPTX→PDF
dependency (and `SOFFICE_BIN` override), the tool call, expected output, the documented internal
constants, and an **Edge Cases** section capturing the gotchas found:

- macOS `._` junk + U+200E filenames in exported-JPEG folders (Tallier = 46 real + 46 junk); we
  filter by open-attempt, not name prefix.
- LibreOffice headless needs a temp profile and no other LO instance open; first convert can be slow
  (~180s timeout); hard-abort on failure.
- 16:9 decks pass through with no bars; busy/photographic edges → luminance-adaptive white/black
  fallback (symmetric).
- Low-res sources are upscaled and flagged with a softness warning (parked alternative: cap-and-bar).
- The still is **square-pixel 1920×1080**; when placed in the 1440×1080-anamorphic sequence the editor
  imports it as square pixels (displays correct 16:9) — document the exact import setting; note for V3.
- Re-running a deck is idempotent (scope-cleaned output dir).

### 3. `requirements.txt` + `.env.example` (edit)

Add `pillow` and `pypdfium2` to the main-venv block (note: PPTX path also requires the
already-installed LibreOffice, documented in the workflow). Add `SOFFICE_BIN=` to `.env.example`
alongside the ffmpeg vars (optional full-path override for the `soffice` resolver).

## Future stage (documented only, not built)

`tools/place_slides.py` (~Stage 7): consume a timecoded transcript + `manifest.json`, match each
slide's `text` to where it's discussed, and emit slide clipitems on **V3** of `OUTPUT/04_angles.xml`
using the FCP7 `<clipitem>` / `<file>` helpers patterned on `tools/create_xml.py`. (Image-folder decks
have no `text` and would need OCR first.)

## Verification (after build)

1. `pip install pypdfium2` into system Python (Pillow already present); confirm `import pypdfium2`.
2. **PDF:** `python tools/slides_to_tv.py --input "INPUT/Powerpoint/Vieillissement socio-démographique et Logement UTL Dinant 18-04-2026.pdf"`.
   Open a few PNGs: each 1920×1080, slide centered & undistorted, symmetric bars matching the deck's
   background (or luminance-adaptive white/black), text readable.
3. **PPTX:** `python tools/slides_to_tv.py --input "INPUT/Powerpoint/Le sens du pèlerinage chrétien.pptx"`.
   Confirm `soffice` resolved (not hardcoded), LibreOffice produced the PDF, slides rasterized;
   spot-check framing.
4. **Image folder:** `python tools/slides_to_tv.py --input "INPUT/Powerpoint/Tallier_Communication_Marche_avril2026"`.
   Confirm exactly the **46 real** JPEGs convert (no `._` junk, no unicode crash), order correct.
5. Inspect `manifest.json`: per-slide png + bar_color + extracted text present; `skipped` empty;
   `source_page` correct.
6. Import one output PNG onto **V3** of a test Premiere sequence; confirm it fills 16:9 with no
   horizontal stretch and no unexpected scaling — **record the exact import setting that achieves this
   in the workflow** (decision 1).
7. **Re-run** the same deck and confirm the output dir is clean (no ghost PNGs from the prior run).
