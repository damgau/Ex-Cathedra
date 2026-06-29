# Workflow: place_slides (slides onto V3, timed to the talk)

Place the speaker's deck slides full-screen on a new V3 track of `04_angles.xml`, timed to when each
slide is on the projector — by reading the locked DIV camera, not the transcript.

---

## Objective

Produce `OUTPUT/05_slides.xml` (the `04_angles` cut with a V3 slide track added) + a review report, so the
editor imports it into Premiere and gets each deck slide appearing full-screen as the speaker begins it,
then cutting back to the speaker. A **reviewed draft**: the editor scrubs, swaps any mislabeled slide, and
hand-adds any slide that wasn't confidently placed.

Why the projector and not the transcript: this deck arrived as **image-only JPEGs with no text layer**, so
transcript↔slide-text matching is impossible. The DIV safety camera is a **static wide shot** whose screen
is readable across the whole talk, so we read the screen directly.

## Required Inputs

- `OUTPUT/04_angles.xml` — the final cut; V3 is appended (DIV=V1, MAIN=V2 untouched).
- `OUTPUT/slides/<deck>/` — the deck converted by `slides_to_tv.py`: `NNN.png` (square-pixel 1920×1080) +
  `manifest.json`. These are **both** the ORB match references **and** the V3 assets.
- `OUTPUT/04_transcript.json` — word-level transcript with timeline-frame timestamps (re-run Whisper first).
- **Screen quad** — the 4 corners of the projected screen in 1920×1080 space (`tl,tr,br,bl`). Default in the
  tool is the Tallier value; re-derive per talk if the camera framing differs (see Steps).

## Tools Used

- `tools/create_transcript.py` — Whisper pre-step → `OUTPUT/04_transcript.json` (Python 3.11 whisper venv).
- `tools/place_slides.py` — the stage: sample DIV → frame-diff change-detection → ORB identity → cutaways → V3.

## Steps

1. **Regenerate the transcript** (required: drives cutaway duration + start-snap):
   ```
   .venv_whisper\Scripts\python.exe tools\create_transcript.py
   ```
   → `OUTPUT/04_transcript.json` (~12k words on this talk; fast on GPU). ffmpeg is resolved via `ffbin.py`.
2. **Confirm the deck is converted**: `OUTPUT/slides/<deck>/` has the PNGs + `manifest.json`
   (run `slides_to_tv.py` if not).
3. **Set the screen quad** (only if framing changed): dump one DIV frame, read the four *projected-area*
   corners (tl, tr, br, bl) in 1920×1080 space, pass them as `--screen-quad x1,y1,x2,y2,x3,y3,x4,y4`. The DIV
   camera is locked, so one quad serves the whole talk. **Do not** rely on auto-detection — the bright-rect
   heuristic grabs only the dense-text sub-region.
4. **Run the stage**:
   ```
   python tools\place_slides.py
   ```
   → `OUTPUT/05_slides.xml` + `OUTPUT/05_slides_report.json`. (~15 min: it decodes the whole DIV track once.
   Use `--start/--end` seconds to test a sub-range.)
5. **Review the report**: slide ids should be mostly increasing; durations 8–25 s; the `low_confidence` and
   `unplaced_deck_slides` lists should be short.
6. **Import into Premiere**: open `05_slides.xml`. The V3 stills are pre-rendered at the sequence's own
   **1440×1080-anamorphic** geometry and tagged **PAR 1.333 via the PNG pHYs chunk**, so they fill the frame at
   correct 16:9 / 100% width with no Scale-to-Frame. Scrub: each placed slide appears full-screen as the
   speaker begins it, holds, cuts back; audio is continuous underneath. Swap any low-confidence/mislabeled
   slide; hand-add unplaced ones at their marked times.
   - *If a still still reads PAR 1.0 / looks squeezed* (some Premiere versions ignore PNG pHYs), select the V3
     stills → **Interpret Footage → Pixel Aspect Ratio → HD Anamorphic 1080 (1.333)**. One action for all.

## Expected Outputs

- `OUTPUT/05_slides.xml` — `04_angles` + a V3 track of still clipitems (V1/V2 + all audio untouched).
- `OUTPUT/05_slides_assets/slide_NNN.png` — the derived V3 stills: each deck slide reframed to 16:9 with a
  **blurred-upscaled background** (no bars) and squeezed to 1440×1080 anamorphic. Scope-cleaned each run.
- `OUTPUT/05_slides_report.json` — per cutaway: `slide`, `png`, ORB `inliers`, `confident`, `start_frame`,
  `duration_s`, `words`, `cue`, `ordinal_outlier`, `top3`; plus `unplaced_deck_slides`, `low_confidence`, and
  `ordinal_outliers` lists for the editor.

## Edge Cases & Known Issues

- **Identity is best-effort.** The deck slides share one white template (orange arrow + title + bullets), so
  only **ORB feature matching** separates them — NCC/pHash/edge correlation all fail (the white template
  dominates). ORB is correct even at low inlier counts, but labels below ~60 inliers are flagged
  `low_confidence` for the editor to verify. Slides advance roughly in order, so a placed id that jumps
  *backwards* is flagged `ordinal_outlier` (a likely mislabel to swap) **even when ORB called it confident**.
- **Timing uses frame-differencing, not correlation.** White-stays-white contributes nothing, so only the
  changed text registers (same-slide change ≈ 0.000, different ≈ 0.03–0.06). Persistence-debounced (~2 s) to
  reject the speaker crossing the screen, fades, and glare.
- **Animation builds** are collapsed by same-slide merge; a partial build that ORB maps to a *different*
  slide can create an extra cutaway — just delete it in Premiere.
- **Fewer than 46 placed is expected** — short flip-through slides (<5 s dwell) are intentionally skipped, and
  low-quality matches may be missed. Add missing slides by hand at the marked times.
- **V3 still geometry & background.** Deck PNGs are 4:3 content padded to 16:9 with solid bars (black on the 6
  photo slides, white on the 40 text slides). For V3 the tool strips the bars and rebuilds each slide as the
  sharp content centred over a **blurred upscaled copy of itself**, then squeezes it to **1440×1080 anamorphic**
  and writes a **pHYs chunk so the PNG itself carries PAR 1.333** (Premiere reads a still's PAR from the file,
  not the XML). That displays at correct 16:9 / 100% width with no Scale-to-Frame — closing the square-pixel
  stretch open item from `slides_to_tv.md`. Background blur strength = `BG_BLUR_DOWN` (10; 16 was too strong,
  6 too light). Manual PAR fallback in Step 6 if a Premiere version ignores pHYs.
- **create_transcript.py**: run with `.venv_whisper\Scripts\python.exe` (Windows; the docstring's `bin/python`
  is macOS). It now lives in `tools/` so `BASE_DIR` resolves to the project root, and ffmpeg comes from
  `ffbin.py` (need not be on PATH).
- No new Python dependencies — `place_slides.py` uses numpy + cv2 (already in system Python).

## Changelog

- 2026-06-29 — Initial version. Design grilled + Phase-0 spike validated (matcher = ORB, change = frame-diff,
  fixed manual quad). Decoupled timing (robust) from identity (best-effort, editor-correctable).
- 2026-06-29 — V3 stills now rendered at 1440×1080 anamorphic (was square-pixel + Scale-to-Frame → it
  stretched to 133%) with a blurred-upscaled background replacing the black/white pillarbox bars.
- 2026-06-29 — Embedded PAR 1.333 in the still PNGs (pHYs) so Premiere reads them anamorphic (was 1.0 /
  squeezed); softened the background blur (downscale 16 → 10).
