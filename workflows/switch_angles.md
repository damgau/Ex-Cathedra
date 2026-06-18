# Workflow: Stages 4–5 — Content-Aware Camera-Angle Switching

Stay on the MAIN shot, and cut to the DIV (wide safety) angle wherever the speaker's face is not
visible on MAIN — cutting ~1 s early and snapping each cut to the nearest pause. Two tools: a CV pass
(`detect_framing`) that asks "is the face visible on MAIN?", and an XML pass (`switch_angles`) that turns
that signal into a multicam timeline.

---

## Objective

Produce `OUTPUT/04_angles.xml` — `OUTPUT/03_silence.xml` with the **MAIN video track split and disabled**
across every span where no face is detected, so the DIV angle shows through. **Timeline length is
unchanged** (success = same `<duration>` as `03_silence.xml`). Only the MAIN *video* track is ever
disabled — never DIV, never audio — so the switches survive Stage 6.

## Required Inputs

- `OUTPUT/03_silence.xml` — Stage 3 output, the silence-trimmed cut (run in **cut mode**, the default, so
  both video tracks fully cover the timeline and the clip boundaries are the real edit points/pauses).
- `INPUT/MAIN CAM/VIDEO/` + `CLIP/` — P2 video MXFs + chain metadata for the CV pass.
- `OUTPUT/main_framing.json` — produced by `detect_framing.py` (Stage 4); consumed by `switch_angles.py`
  (Stage 5).

## Tools Used

- `tools/detect_framing.py` — samples MAIN frames via ffmpeg, runs OpenCV Haar face detection, writes the
  per-sample signal. Reuses `remove_silence._resolve_p2_chain`.
- `tools/switch_angles.py` — debounces the signal into DIV windows (pre-roll + snap + 1 s floor) and
  applies the split-and-disable. Reuses `remove_silence._apply_one_cut` and `_next_id`.
- `tools/test_switch_angles.py` — invariant regression test (no video needed).

## Steps

1. Confirm `OUTPUT/03_silence.xml` exists, `ffmpeg` is on PATH, and `python -c "import cv2"` works.
2. **Stage 4:** `python tools/detect_framing.py`. First do a slice for a sanity check, e.g.
   `--end 120` (first 2 min) or `--max-frames 200`, then the full run. Review the printed
   "face present X%" and "longest no-face run".
3. **Stage 5:** `python tools/switch_angles.py`. Review the printed DIV windows (timestamps + each
   duration + total DIV %), then confirm. Writes `OUTPUT/04_angles.xml`.
4. Import `OUTPUT/04_angles.xml` into a fresh Premiere project. Scrub and confirm it holds on MAIN, cuts
   to DIV ~1 s before the speaker leaves frame (on a pause, not mid-syllable), returns cleanly, no shot
   < 1 s, audio continuous.
5. Review/correct in Premiere (re-enable a wrong DIV cut, disable bad takes), export `06_reviewed.xml`,
   and proceed to `workflows/delete_enable_clip.md`.

### Tuning (Stage 4 detection)

The conservative rule cuts to DIV whenever **no** face is found, so detection sensitivity sets how much
DIV you get. Knobs: `--fps` (sampling rate), `--min-face` (lower → detect smaller/farther faces),
`--neighbors` (lower → more lenient, more detections but more false positives), `--scale`.

### Tuning (Stage 5 switching)

`--trigger` / `--return` (debounce: how long absence/return must persist), `--pre-roll` / `--post-roll`
(lead in/out), `--snap-tolerance` (how far a cut may move to land on a pause), `--min-shot` (shortest
allowed MAIN or DIV shot). `-y` skips the confirmation.

## Expected Outputs

- `OUTPUT/main_framing.json` — `{fps, scale, min_face, neighbors, timeline_fps, samples:[{timeline_frame,
  face_present}, …]}`.
- `OUTPUT/04_angles.xml` — same `<duration>` as `03_silence.xml`; MAIN video split with
  `<enabled>FALSE</enabled>` across the DIV windows. No ripple, no deletion.

## Edge Cases & Known Issues

- **Anamorphic footage.** 1440×1080 stored, displayed 16:9. Detection frames are forced to 16:9
  (`scale=W:W*9/16`) so faces aren't horizontally squished — squished faces measurably lower Haar's hit
  rate (≈36% → ≈43% on the test intro).
- **MAIN is a fairly wide shot here, and the speaker often faces his slides.** So a real chunk of
  "no face" is genuine (not a detector miss), and the conservative rule will cut to DIV often. Tune
  `--min-face`/`--neighbors`, or accept it and fix in Premiere — this is why Stage 5 is review-then-commit.
- **Haar is frontal-only by default → profile cascade added.** `detect_framing` runs frontal **and**
  profile cascades (profile on the frame and its mirror) so a head-turn still counts as "face visible".
- **Disable-only — only MAIN video.** Stage 5 never ripples/deletes and never touches DIV or audio. This
  is the invariant that keeps removal (Stage 6) and angle-switching separable: a span with one video
  track disabled = camera switch (kept); a span dead on every track = removed. Don't break it.
- **Assumes cut-mode `03_silence.xml`.** If Stage 3 was run in `--mode mute`, MAIN already carries
  disabled silence clips; switching still works but the snap edit-points and coverage get muddier.
- **Partial framing JSON → trailing DIV artifact.** If `detect_framing` only covered part of the
  timeline (e.g. `--end`/`--max-frames`) and the last sample was a no-face state, the state machine holds
  DIV to the end. Run the full timeline for the real edit.
- **Runtime.** Stage 4 spawns one ffmpeg decode per MAIN clip segment (≈321 on the test file) — minutes,
  not seconds. The signal is cached to JSON so Stage 5 thresholds can be re-tuned without re-running CV.
- **Frame rate hardcoded to 25 fps**; UTF-8 stdout forced for accented console output.

## Changelog

- 2026-06-18 — Initial version. New two-tool camera stage (`detect_framing` + `switch_angles`) added when
  `create_transcript`/`remove_fillers` were parked to `maybe_later/`. Rule: DIV only when no face on MAIN
  (OpenCV Haar frontal+profile); hard cut with ~1 s pre-roll snapped to the nearest pause; 1 s min shot.
