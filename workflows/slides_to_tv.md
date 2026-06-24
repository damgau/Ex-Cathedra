# Workflow: Slides → TV-friendly 1920×1080 stills for Premiere

Convert a speaker's support deck — a **PDF**, a **PPTX**, or a **folder of exported JPEGs** — into a
consistent set of broadcast-ready **1920×1080 square-pixel PNGs**, reframed to 16:9 by **padding,
never stretching**. Also writes a `manifest.json` recording each still's source mapping, bar colour,
and extracted text — the seed for a future transcript→slide placement stage on V3.

> This builds the **converter only**. Putting the stills onto V3 at the right timecode is a separate,
> future stage (`tools/place_slides.py`, see *Future stage* below) — not part of this workflow.

---

## Objective

Given one deck in any of the three input shapes, produce `OUTPUT/slides/<deck-slug>/` containing
`001.png … NNN.png` (every one exactly 1920×1080, RGB, square pixels, slide centred and undistorted)
plus a `manifest.json`. Success = open any PNG and the slide fills 16:9 with no horizontal stretch,
symmetric bars that match the deck (or a clean white/black), readable text; and the manifest's
`slides[]` maps each PNG back to its true source page with the page's text.

## Required Inputs

- `--input` — **one** of:
  - a **PDF** file (`*.pdf`),
  - a **PPTX** file (`*.pptx`) — requires LibreOffice (see Tools),
  - a **directory of images** (exported JPEG/PNG slides).
- **LibreOffice** (`soffice`) — *only* for PPTX input. Auto-resolved (see below); install from
  libreoffice.org, or set `SOFFICE_BIN` in `.env` to the full binary path.
- **pypdfium2** + **Pillow** + **numpy** in the active Python (system Python 3.14 has them; the
  project `.venv` is broken — use system Python). `pip install pypdfium2 pillow` if missing.

## Tools Used

- `tools/slides_to_tv.py` — the whole converter. Ingests the deck, reframes each slide to 1920×1080
  (contain + symmetric bars), writes the PNGs + `manifest.json`. Lazily resolves `soffice` with an
  `ffbin.py`-style cached resolver (`SOFFICE_BIN` → PATH → known install glob → guided error).

## Steps

1. Confirm deps: `python -c "import pypdfium2, PIL, numpy"`. For PPTX, confirm LibreOffice is
   installed (or `SOFFICE_BIN` is set).
2. Run the converter on the deck:

   ```
   python tools/slides_to_tv.py --input "<file.pdf | file.pptx | folder-of-images>" \
       [--output OUTPUT/slides/<deck-name>] [--fill auto|white|black|#RRGGBB]
   ```

   - `--input` auto-detects shape by suffix / `is_dir`.
   - `--output` defaults to `OUTPUT/slides/<deck-slug>/` (accent/punctuation-stripped stem).
   - `--fill` defaults to `auto` (uniform-edge median → luminance-adaptive white/black fallback);
     pass `white`/`black`/`#RRGGBB` to force the bar colour.
3. Review the console: ingested slide count, any per-slide skips, any low-res softness warning.
4. Spot-check a few PNGs (each 1920×1080, centred, undistorted, bars correct, text readable) and the
   `manifest.json` (`slides[]` n / png / source_page / bar_color / text; `skipped` for any drops).
5. **Import into Premiere** onto **V3** (the slide track). See *Premiere import* below for the exact
   setting that displays the square-pixel still as correct 16:9.
6. Re-running the same deck is safe — the output dir is scope-cleaned first (idempotent).

### `--fill` modes

- `auto` (default): sample a thin (~2.5%) strip along each bar-abutting slide edge. If **both** edges
  are uniform (low per-channel std) **and** close to each other → bars = their combined **median**
  (seamless on uniform colour decks). Otherwise → **luminance-adaptive**: white bars for a
  predominantly-light slide, black for a predominantly-dark one (black is also the broadcast-neutral,
  so it never reads as a mistake). One shared colour, symmetric both sides.
- `white` / `black` / `#RRGGBB`: skip detection, force that colour.

### Documented internal constants (in `tools/slides_to_tv.py`)

Promoted to a flag only if a real run needs to turn it every time (none have so far):

- `TARGET_W,TARGET_H = 1920,1080`, `FPS = 25`
- `EDGE_STRIP_FRAC = 0.025` — edge strip width sampled for bar colour
- `UNIFORM_STD_MAX = 10.0` — per-channel std below which an edge counts as "uniform"
- `EDGE_MATCH_MAX = 12` — max per-channel delta for the two edges to share a median
- `LUMA_SPLIT = 128.0` — mean luminance split for white-vs-black fallback
- `SOFT_FLOOR_PX = 1280` — warn when a **raster** source is upscaled from below this binding dim
- `SOFFICE_TIMEOUT = 180` — PPTX→PDF convert timeout (first convert is slow)

## Expected Outputs

`OUTPUT/slides/<deck-slug>/`:

- `001.png … NNN.png` — 1920×1080 RGB square-pixel, contiguously numbered (renumbered if a source
  page was skipped).
- `manifest.json`:
  ```json
  {
    "deck": "...", "source": "...", "source_kind": "pdf|pptx|images",
    "fill_mode": "auto", "fps": 25, "size": [1920, 1080],
    "skipped": [{"source_page": N, "source_file": null, "reason": "..."}],
    "slides": [{"n": 1, "png": "001.png", "source_page": 1,
                "source_file": "<basename or null>", "bar_color": "#rrggbb|null",
                "text": "<page text, empty for image folders>"}]
  }
  ```
  `source_page` is the **true** source index even when output is renumbered; `bar_color` is `null`
  for native-16:9 slides (no bars); `text` is the page's text layer (free for PDF/PPTX), **empty for
  image folders** (no text layer — OCR would be a future concern). `source_file` carries the original
  filename for image-folder slides (the only stable source identity there).

## Premiere import (square-pixel still in the 1440×1080-anamorphic sequence)

The stills are **square-pixel 1920×1080** (decision 1) — correct everywhere, but the edit sequence is
1440×1080 **anamorphic** (PAR 1.333, see `tools/create_xml.py`). On import, Premiere must treat each
still as **square pixels**, not conform it to the sequence PAR:

- Premiere → **Project panel → right-click the still(s) → Modify → Interpret Footage → Pixel Aspect
  Ratio → "Square Pixels (1.0)"** (it usually auto-detects 1.0 from the 1920×1080 PNG; verify).
- Drop on **V3**; the 16:9 frame fills the 16:9 raster with **no horizontal stretch**. Set Motion →
  Scale to "Scale to Frame Size" / fit if the project default doesn't already.
- ☐ *To confirm on the next real import and lock here:* the exact menu path + scale setting that
  yields no stretch and no unexpected re-scale. (Verification step 6.)

## Edge Cases & Known Issues

- **macOS `._` junk + U+200E filenames in exported-JPEG folders.** The Tallier sample folder is **46
  real JPEGs + 46 AppleDouble `._` stubs**, and the real names carry embedded **U+200E** bidi marks
  (so `grep '^\._'` / name-prefix filtering is unreliable). We filter by **open-attempt** (cheap
  dotfile skip first, then Pillow-open and drop failures) and **natural-sort** after stripping bidi
  marks. Result: exactly the 46 real slides, in order. If filenames carry **no** numeric sequence the
  tool **warns** (order is then just filesystem order and may be wrong).
- **LibreOffice on Windows — three gotchas that cost real debugging time:**
  1. Must use **`soffice.com`**, not `soffice.exe`. The `.exe` is a launcher that detaches and
     returns 0 *before* converting (no PDF ever appears). `soffice.com` is the console wrapper that
     blocks and returns the real result. The resolver picks `.com` automatically on Windows.
  2. Pass **absolute** paths for `--outdir` and the input — soffice resolves a relative `--outdir`
     against its own cwd, not ours.
  3. A throwaway `-env:UserInstallation` profile was **tried and dropped**: on a cold run soffice
     only *initialises* the fresh profile and silently skips the conversion (exit 0, no PDF). The
     **default profile** is used instead. The trade-off: if a LibreOffice window is already open it
     can lock the profile — the convert then fails with "no PDF", and the guided error says to close
     LibreOffice and retry.
  4. soffice prints a harmless `Entity: line 1: parser error : Document is empty` warning and can
     return a quirky exit code even on a clean convert — so success is judged by **the PDF
     existing**, not the exit code. First convert is slow (~ up to the 180 s timeout).
- **Failure granularity.** soffice failure/timeout → **hard-abort** the whole deck (no partial deck
  is meaningful). A single unrenderable PDF page → **skip-and-warn**, keep going, renumber output
  contiguously, and record the true `source_page` + reason in `manifest.skipped`. (In an image folder
  an unopenable file is treated as *junk*, not a skipped slide.)
- **16:9 decks pass through** with no bars (`bar_color: null`), just a resize. Busy/photographic
  edges → luminance-adaptive white/black fallback, symmetric. Verified: dark forest photo → black
  bars; light newspaper scan → near-seamless white bars; white-background PDF → seamless white bars.
- **Low-res sources** are upscaled with Lanczos and **flagged** with a softness warning when a raster
  source's binding dimension is below `SOFT_FLOOR_PX` (e.g. the 720×540 Tallier JPEGs). Never refused,
  never shrunk-and-barred. *Parked alternative (B):* cap-the-upscale and centre at native res with
  larger bars, if softness ever disappoints. Vector PDF/PPTX render sharp at the contained size, so
  they never trigger the warning.
- **Re-run is idempotent.** The output dir is scope-cleaned (only this tool's own `*.png` +
  `manifest.json`) before writing — no ghost slides from a prior run. If it contains **foreign**
  files the tool **refuses** (exits 1) rather than deleting.
- **Square-pixel still vs anamorphic sequence** — see *Premiere import* above; this is a deliberate
  choice (stills stay correct in preview/reuse/archive), not a bug.
- **Python env / UTF-8.** Use system Python 3.14 (project `.venv` is broken, macOS origin). stdout is
  reconfigured to UTF-8 so accented deck names ("pèlerinage") don't crash on Windows cp1252.

## Future stage (documented only — not built)

`tools/place_slides.py` (~Stage 7): consume a timecoded transcript + this `manifest.json`, match each
slide's `text` to where it's discussed, and emit slide clipitems on **V3** of `OUTPUT/04_angles.xml`
using the FCP7 `<clipitem>`/`<file>` helpers patterned on `tools/create_xml.py`. Image-folder decks
have no `text` (empty) and would need OCR first.

## Changelog

- 2026-06-24 — Initial version. Built `tools/slides_to_tv.py` (PDF via pypdfium2, PPTX via LibreOffice
  `soffice.com`, image folder via open-attempt junk filter + natural sort). Verified on all three real
  inputs: PDF (30pp, white seamless bars, text), PPTX (15pp 16:9, accents OK), image folder (46/46,
  luminance-adaptive black/white bars, low-res softness warning). Idempotent re-run + foreign-file
  refusal confirmed. **Adapted from plan:** dropped the throwaway LibreOffice profile (broke cold
  conversion on Windows) in favour of the default profile + guided lock error; pinned `soffice.com`
  and absolute paths. Premiere V3 import setting still to be locked on first real import.
