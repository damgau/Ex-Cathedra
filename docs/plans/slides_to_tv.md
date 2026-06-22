# Plan — Slides → TV-friendly stills for Premiere

**Status:** planned, not started · **Scope:** converter only (V3 placement deferred)

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

Decisions locked with the user:

- **Home:** WAT-native — a `tools/` script + a `workflows/` SOP, like every other stage.
- **Pillarbox fill:** **smart edge-color match.** Sample the slide strip that touches each bar; if
  it's a near-uniform color, fill the bar with it so the seam disappears (handles colored-background
  decks). If the edge is busy/photographic, **fall back to solid white**.
- **Scope:** converter now; V3 placement later.

## Environment findings (verified 2026-06-22)

- System **Python 3.14.5** has **Pillow 12.1**, **cv2 4.13**, **numpy 2.4** installed. The project
  `.venv` is broken (macOS origin) — use system Python.
- **Missing:** a PDF rasterizer. Plan adds **`pypdfium2`** (broad wheel coverage incl. 3.14, no
  system deps, renders straight to PIL). `python-pptx` is **not** needed.
- **LibreOffice is installed** at `C:\Program Files\LibreOffice\program\soffice.exe` — used headless
  for **PPTX → PDF**, then pypdfium2 rasterizes the PDF. No PowerPoint, no ImageMagick (the `convert`
  on PATH is Windows' NTFS tool — must not be called).
- The Premiere XML dialect (1920×1080 / 1440×1080 anamorphic / 25fps) is defined in
  `tools/create_xml.py` — relevant only to the future V3 stage.

## Deliverables

### 1. `tools/slides_to_tv.py` (new)

Follow the repo idioms in `tools/detect_framing.py` and `tools/_template.py`: module docstring header
(Purpose / Inputs / Output), `sys.path.insert(... .parent.parent)` + `load_dotenv()`, **UTF-8 stdout
reconfigure** (deck names contain accents like "pèlerinage"), `BASE_DIR`/`OUTPUT_DIR` constants,
`argparse` `main()`, and the `try/except → ERROR/exit(1)` wrapper.

**CLI**

```
python tools/slides_to_tv.py --input "<file.pdf | file.pptx | folder-of-images>" \
    [--output OUTPUT/slides/<deck-name>] [--fill auto|white|black|#RRGGBB] [--dpi-min 1920]
```

- `--input` accepts a **PDF**, a **PPTX**, or a **directory of images** (auto-detected by suffix / `is_dir`).
- `--fill` default `auto` (edge-color match → white fallback). Explicit color/white/black overrides.
- Output defaults to `OUTPUT/slides/<deck-name>/` (slug of the input stem).

**Pipeline**

1. **Ingest → list of PIL.Image (slide order):**
   - *PDF:* `pdfium.PdfDocument(path)`; render each page at a scale chosen so the **long edge ≥ 1920px**
     (crisp on TV), cap upscale to avoid absurd sizes; `bitmap.to_pil()`.
   - *PPTX:* `soffice --headless --convert-to pdf --outdir <.tmp> <pptx>` into `.tmp/`, using a
     throwaway profile dir via `-env:UserInstallation=file:///<.tmp/lo_profile>` to dodge a running-LO
     clash; then rasterize the produced PDF as above.
   - *Image folder:* glob images, **filter out macOS `._*` AppleDouble junk**, sort naturally, open
     with Pillow. Handle the leading **U+200E** char in real filenames (don't choke on unicode).
2. **Reframe each slide to 1920×1080 (contain, no stretch):**
   - Scale the slide to fit inside 1920×1080 preserving AR.
   - For the leftover margin (left/right for 4:3, top/bottom for tall decks), compute the bar color:
     - **`auto`:** take a thin strip (~2–3% of the slide dimension) along the slide edge that abuts the
       bar; convert to numpy; if **per-channel std-dev < ~10** (uniform), bar = the strip's **median
       color** (left and right may differ → seamless on colored backgrounds); else **white**.
     - explicit `white`/`black`/`#hex` skip detection.
   - Paste the centered slide onto the filled 1920×1080 canvas. 16:9 decks → no bars, just resize.
3. **Write** `001.png … NNN.png` (zero-padded, RGB, square-pixel) into the output dir.
4. **Write `manifest.json`** in the output dir: `{deck, source, fill_mode, fps:25, slides:[{n, png,
   source_page, bar_color, text}]}`. `text` is the page's extracted text (free from pypdfium2 for
   PDF/PPTX-derived pages) — **stored now to feed the future transcript→slide matcher**, costs nothing.
   Empty for image-folder input.

Reuse Pillow + numpy already in the env; no new heavy code.

### 2. `workflows/slides_to_tv.md` (new)

Built from `workflows/_TEMPLATE.md`. Objective, the three input shapes, the `soffice` PPTX→PDF
dependency, the tool call, expected output, and an **Edge Cases** section capturing the gotchas found:

- macOS `._` junk + U+200E filenames in exported-JPEG folders (the Tallier example has 46 real + 46 junk).
- LibreOffice headless needs a temp profile and no other LO instance open; first convert can be slow.
- 16:9 decks pass through with no bars; photographic-bleed edges → white fallback.
- The still is **square-pixel 1920×1080**; when placed in the 1440×1080-anamorphic sequence the editor
  imports it as square pixels (displays correct 16:9) — note for V3.
- DPI floor so small source images aren't upscaled into mush.

### 3. `requirements.txt` (edit)

Add `pillow` and `pypdfium2` to the main-venv block (note: PPTX path also requires the
already-installed LibreOffice, documented in the workflow).

## Future stage (documented only, not built)

`tools/place_slides.py` (~Stage 7): consume a timecoded transcript + `manifest.json`, match each
slide's `text` to where it's discussed, and emit slide clipitems on **V3** of `OUTPUT/06_reviewed.xml`
using the FCP7 `<clipitem>` / `<file>` helpers patterned on `tools/create_xml.py`.

## Verification (after build)

1. `pip install pypdfium2` into system Python (Pillow already present); confirm `import pypdfium2`.
2. **PDF:** `python tools/slides_to_tv.py --input "INPUT/Powerpoint/Vieillissement socio-démographique et Logement UTL Dinant 18-04-2026.pdf"`.
   Open a few PNGs: each 1920×1080, slide centered & undistorted, bars matching the deck's background
   (or white), text readable.
3. **PPTX:** `python tools/slides_to_tv.py --input "INPUT/Powerpoint/Le sens du pèlerinage chrétien.pptx"`.
   Confirm LibreOffice produced the PDF and slides rasterized; spot-check framing.
4. **Image folder:** `python tools/slides_to_tv.py --input "INPUT/Powerpoint/Tallier_Communication_Marche_avril2026"`.
   Confirm exactly the **46 real** JPEGs convert (no `._` junk, no unicode crash).
5. Inspect `manifest.json`: per-slide png + bar_color + extracted text present.
6. Import one output PNG onto **V3** of a test Premiere sequence; confirm it fills 16:9 with no
   horizontal stretch and no unexpected scaling.
