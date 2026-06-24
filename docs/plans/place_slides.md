# Plan — Stage 7: `place_slides.py` (slides onto V3, timed to the talk)

**Status:** design finalized via grilling 2026-06-24 · build not started · **Scope:** the
documented-but-unbuilt *Future stage* from `workflows/slides_to_tv.md` (`tools/place_slides.py`).

## Context

The conference being edited is the **Pierre-Alain Tallier forest talk** (confirmed by reading the
DIV-CAM projector in real frames). Its support deck arrived as the **`Tallier_Communication_Marche_avril2026`
image folder = 46 JPEGs with no text layer**, already converted by `slides_to_tv.py` into
`OUTPUT/slides/Tallier_Communication_Marche_avril2026/` (46 PNGs + `manifest.json`, `text` empty).

The editor wants each slide to appear **full-screen on V3** *when the speaker is discussing it*, then cut
back to the speaker — a bounded cutaway, not an always-on overlay. Because the deck has no text, we cannot
match slide text to the transcript. Instead we use the **projector itself**: the DIV-CAM screen is clean
and readable across the talk (verified on sampled frames), so we identify which slide is projected and
when it changes.

## Decisions locked (grilling 2026-06-24)

1. **Display = full-screen cutaway.** An opaque 1920×1080 still on V3 fully covers the cameras while up,
   then V3 gaps return to the speaker. Speaker **audio is untouched** (V3 is a video-only still; V1/V2 +
   all audio tracks are left exactly as in `04_angles.xml`).
2. **Primary signal = projector slide-identification on DIV-CAM** (a refinement of raw "change-detection"):
   sample the DIV-CAM screen region densely, and for each sample **match the screen crop against the 46
   deck PNGs** to get *which* slide + a confidence score. A slide cutaway is created where the confidently
   matched slide *changes* to a new slide. The transcript's **cue phrases** ("comme vous voyez sur…",
   "sur cette photo…", "l'illustration montre…") are a secondary cross-check, not the driver.
3. **Only confident placements** (editor's choice). Low match score ⇒ skip; fewer than 46 cutaways is
   fine — the editor adds any missing slide by hand in Premiere. No interpolation, no forced 46. Identity
   comes from the image match, so a missed slide does **not** shift the rest, and animation builds map to
   the same slide PNG (no spurious change).
4. **Duration = floor 8s, grow by words spoken, capped.** `dur = clamp(8s, base + ~0.3s × words_spoken_
   while_slide_projected, ~25s)`. Start each cutaway at the slide change, **snapped forward to the first
   transcribed word at/after it** so it begins on speech. The 8s floor, per-word rate, and 25s cap are
   documented internal constants (promote to flags only if a real run needs turning them — same policy as
   `slides_to_tv.py` decision 9).
5. **Output is a reviewed draft.** `OUTPUT/05_slides.xml` (V3 added to `04_angles.xml`) + a
   `OUTPUT/05_slides_report.json`. Editor imports into Premiere, scrubs, deletes/nudges/adds.
6. **Transcript is required and re-generated.** `OUTPUT/04_transcript.json` was cleared; re-run the parked
   `create_transcript.py` (local Whisper, free, ~minutes) for word-level timeline-frame timestamps.

## Inputs

- `OUTPUT/04_angles.xml` — the final cut; V3 is appended. (Same timeline length as `03_silence.xml` —
  switch_angles is disable-only, so transcript frames stay valid here.)
- `OUTPUT/slides/Tallier_Communication_Marche_avril2026/` — the 46 reference PNGs + `manifest.json`
  (both the visual-match references **and** the assets dropped on V3). `--slides` overrides.
- `OUTPUT/04_transcript.json` — word-level transcript (re-run Whisper first).
- DIV-CAM footage — sampled via the XML's DIV clips (no separate path needed).

## Deliverables

### 1. `tools/create_transcript.py` — resurrect from `maybe_later/tools/` (re-run only)

Run as-is with `.venv_whisper/bin/python tools/create_transcript.py` to regenerate
`OUTPUT/04_transcript.json`. (It was parked only because *filler-removal* was a dead end; transcription
itself works — it already produced 12,317 words on this talk. See `maybe_later/NOTES.md` revival caveats:
needs the Python 3.11 `.venv_whisper`.) No code change required to produce the transcript.

### 2. `tools/place_slides.py` (new) — WAT-native, follows `tools/_template.py` / `detect_framing.py` idioms

**Pipeline:**
1. **Sample DIV-CAM over the timeline** (~0.5–1 fps). Reuse `detect_framing.py`'s machinery: find the DIV
   video track by `%20`-encoded label, map timeline `[start,end]`→source `[in,out]` per clipitem, resolve
   the P2 chain (`_resolve_p2_chain`), decode frames **raw via ffmpeg→numpy** (no temp JPEGs). Produces
   `(timeline_frame, frame)` samples. ffmpeg via `tools/ffbin.py`.
2. **Isolate the screen region.** Auto-detect the bright projector rectangle once (the region drifts a
   little between frames); `--roi x,y,w,h` to override / lock it. Crop each sample to that region.
3. **Identify the slide.** Downscale + grayscale + contrast-normalize the crop and each of the 46 deck
   PNGs; score by normalized cross-correlation / template match (numpy, or `cv2` which is installed).
   Per sample → `(best_slide_id, score)`.
4. **Confident slide segments.** A run of consecutive samples on the same `best_slide_id` with
   `score ≥ MATCH_MIN` = that slide is projected over `[t_start, t_end]`. A new confident id = a slide
   appearance. Drop low-score / ambiguous runs (the "only confident" rule).
5. **Build cutaways.** Per confident appearance: `start` = first confident frame, snapped forward to the
   first transcript word at/after it (optionally to a MAIN edit point); `duration` = clamp(8s,
   base + 0.3s×words-in-window, 25s). Optional cue-phrase presence near the start raises confidence.
6. **Emit V3.** Append a third `<track>` to `media/video` with one **still** clipitem per cutaway. Build
   the still `<file>`/`<clipitem>` patterned on `create_xml.py` (`_file_element`, `_video_clipitem`,
   `pathurl`, `_rate`, `TICKS_PER_FRAME`) but for a **square-pixel 1920×1080 PNG**: `<pathurl>`→PNG,
   `<media><video><samplecharacteristics>` 1920×1080 / square PAR, **no audio**, add Motion "Scale to
   Frame Size" (so it sits correctly over the 1440×1080-anamorphic sequence — carry the import note from
   `slides_to_tv.md`). Allocate clipitem ids with `timeline._next_id(seq)`. Leave V1/V2 + audio untouched.
7. **Write** `OUTPUT/05_slides.xml` + `OUTPUT/05_slides_report.json` (per cutaway: slide n, png, match
   score, start_frame, duration_s, words, snapped-to, cue-phrase hit; plus the list of deck slides that
   got **no** confident placement, for the editor to add by hand).

**CLI (lean):** `--input` (04_angles.xml), `--slides` (slides dir), `--transcript`, `--output`, `--roi`,
`--sample-fps`. Thresholds (`MATCH_MIN`, 8s floor, 0.3s/word, 25s cap, build-merge window) are documented
internal constants.

### 3. `workflows/place_slides.md` (new) + `requirements.txt` note

SOP from `workflows/_TEMPLATE.md`: the three inputs, the Whisper pre-step, the tool call, the V3 Premiere
import setting, and an Edge Cases section (screen drift / ROI, occasional blown-out slide → skipped,
animation builds folded by image-match, "fewer than 46 is expected — add missing by hand"). Note any new
matcher dependency (prefer numpy/`cv2`-only — both already in the env — to avoid additions).

## Reuse (do not re-implement)

- `tools/detect_framing.py` — DIV frame sampling, timeline→source mapping, P2 chain resolve, ffmpeg→numpy.
- `tools/create_xml.py` — still `<file>`/`<clipitem>` builders, `pathurl`, `_rate`, ticks.
- `tools/timeline.py` — `_next_id` for globally-unique clipitem ids (note: it has no add-track helper;
  appending the V3 `<track>` is new but small).
- `tools/ffbin.py` — ffmpeg resolver. `OUTPUT/slides/.../manifest.json` — slide list + order.

## Key risks (validate during build)

- **Match robustness on bright/blown slides** (the linchpin). Mitigation: contrast-normalize before
  scoring; "only confident" means a poor match is simply skipped (editor adds it). Validate on a handful
  of real DIV-CAM frames vs deck PNGs before wiring the whole pipeline.
- **Screen-region drift** between frames → auto-detect per pass or lock with `--roi`.
- **Premiere still display** (square-pixel still in a 1440-anamorphic sequence) — confirm the exact
  scale/interpret setting on first import; this open item is inherited from `slides_to_tv.md`.

## Verification (end-to-end)

1. `.venv_whisper/bin/python tools/create_transcript.py` → `OUTPUT/04_transcript.json` (sanity: first
   words print, ~12k words).
2. `slides_to_tv.py` on the Tallier folder — already done; confirm `OUTPUT/slides/Tallier_.../` has 46 PNGs.
3. `python tools/place_slides.py` → `OUTPUT/05_slides.xml` + report. Check the report: matched slide ids
   are mostly increasing, timestamps plausible, durations ≥ 8s, the "unplaced" list is short.
4. Import `05_slides.xml` into Premiere: scrub — each placed slide appears full-screen as the speaker
   begins it, holds ≥ 8s, cuts back to the speaker; audio continuous under the slide. Add any unplaced
   slide by hand. **Record the exact V3 still import/scale setting** in the workflow.
