# Workflow: Stages 4–5 — Presence-Aware Camera-Angle Switching

Stay on the MAIN shot, and cut to the DIV (clean safety) angle only where the **speaker is
physically out of MAIN's frame** (walked off / leaned out / fully occluded) — cutting ~1 s early
and snapping each cut to the nearest pause. Two tools: a CV pass (`detect_framing`) that asks "is
the speaker present in MAIN?", and an XML pass (`switch_angles`) that turns that signal into a
multicam timeline.

> **Why presence, not face?** This stage originally cut to DIV whenever **no face** was found on
> MAIN. That over-fired badly: the speaker constantly turns to his slides — face gone, but he's a
> perfectly good "presenter at the board" MAIN shot you want to **keep**. On one conference the
> face rule put ~40 % of the timeline on DIV (367 cuts). The rule is now **person presence**: he's
> only "absent" when his *body* leaves MAIN's frame. Keep MAIN as much as possible; DIV is the
> backup. On a normal (stationary-speaker) talk this fires ~never.

---

## Objective

Produce `OUTPUT/04_angles.xml` — `OUTPUT/03_silence.xml` with the **MAIN video track split and
disabled** across every span where the speaker is absent from MAIN, so the DIV angle shows
through. **Timeline length is unchanged** (success = same `<duration>` as `03_silence.xml`). Only
the MAIN *video* track is ever disabled — never DIV, never audio — so the switches survive Stage 6.

## Required Inputs

- `OUTPUT/03_silence.xml` — Stage 3 output, run in **cut mode** (the default) so both video tracks
  fully cover the timeline and the clip boundaries are the real edit points/pauses.
- `INPUT/MAIN CAM/VIDEO/` + `CLIP/` — P2 video MXFs + chain metadata for the CV pass.
- `OUTPUT/main_presence.json` — produced by `detect_framing.py` (Stage 4); consumed by
  `switch_angles.py` (Stage 5).
- The person-detection model (MobileNet-SSD), fetched on demand to `tools/models/` (gitignored)
  by `tools/fetch_models.py` — `detect_framing` calls this automatically on first run.

## Tools Used

- `tools/detect_framing.py` — decodes MAIN frames straight from ffmpeg as **raw video piped into
  numpy** (no JPEG/temp-dir round-trip, ≈2.6× faster), runs **MobileNet-SSD person detection via
  `cv2.dnn`**, and writes the per-sample `speaker_present` signal. Reuses
  `remove_silence._resolve_p2_chain`; handles both FCP-XML file-reference styles (pathurl-per-clip
  and Premiere's `<file id>` dedup).
- `tools/switch_angles.py` — debounces the absence signal into DIV windows (pre-roll + snap + min
  shot) and applies the split-and-disable. Reuses `timeline.disable_span` (the camera-switch seam).
- `tools/fetch_models.py` — on-demand download of the (gitignored) MobileNet-SSD weights.
- `tools/test_switch_angles.py` — invariant regression test (no video needed).

## Steps

1. Confirm `OUTPUT/03_silence.xml` exists, `ffmpeg` is on PATH, and `python -c "import cv2"` works.
2. **Stage 4:** `python tools/detect_framing.py`. First a slice for a sanity check
   (`--end 120` or `--max-frames 200`), then the full run. Review the printed
   "speaker present X %" and "longest absent run". (Model auto-downloads on first run.)
3. **Stage 5:** `python tools/switch_angles.py`. Review the printed DIV windows (timestamps +
   each duration + total DIV %), then confirm. Writes `OUTPUT/04_angles.xml`.
4. Import `OUTPUT/04_angles.xml` into a fresh Premiere project. Scrub and confirm it holds on
   MAIN, cuts to DIV ~1 s before the speaker leaves frame (on a pause, not mid-syllable), returns
   cleanly, no shot < 1 s, audio continuous.
5. Review/correct in Premiere (re-enable a wrong DIV cut, add one the detector missed). That
   reviewed timeline is the final deliverable.

### Tuning (Stage 4 detection)

Knobs: `--conf` (min person-detection confidence, default 0.5; lower → more "present"),
`--fps` (sampling rate, default 4), `--scale` (decode width, default 480; the DNN re-blobs to
300×300 internally so scale mainly trades decode speed, not accuracy).

### Tuning (Stage 5 switching)

`--trigger` / `--return` (debounce: how long absence/return must persist), `--pre-roll` /
`--post-roll` (lead in/out), `--snap-tolerance` (how far a cut may move to land on a pause),
`--min-shot` (shortest allowed MAIN or DIV shot). `-y` skips the confirmation. The defaults
(`trigger=return=1.0`, `pre-roll=1.0`, `post-roll=0.75`, `snap=0.75`, `min-shot=1.0`) were
validated on a stress-test conference (see Edge Cases) and are MAIN-biased.

## Expected Outputs

- `OUTPUT/main_presence.json` — `{detector:"mobilenet-ssd", fps, scale, conf, timeline_fps,
  samples:[{timeline_frame, speaker_present}, …]}`.
- `OUTPUT/04_angles.xml` — same `<duration>` as `03_silence.xml`; MAIN video split with
  `<enabled>FALSE</enabled>` across the absence windows. No ripple, no deletion.

## Edge Cases & Known Issues

- **Presence ≠ good framing (review handles the gap).** The signal is binary "is a person in
  MAIN's frame". On a stress-test conference (speaker wandering, 14 marked off-camera windows) it
  caught **13/14 with zero false cuts**. The one miss: the speaker briefly left frame (~0.7 s, too
  short to trigger) then returned **edge-framed** — crammed to the far edge but genuinely visible,
  which the detector correctly calls "present". Treating "visible-but-edge-framed" as a switch was
  rejected (it would over-fire on normal talks); fix such rare cases in Premiere review.
- **DIV is cut to blind.** Stage 4 only analyses MAIN. This is safe because DIV is the wide that
  covers the whole stage — when the speaker is out of MAIN he is still within DIV. The only time
  he is in neither is when he leaves the stage entirely (rare → handled by the silence/removal
  stage, not here).
- **Disable-only — only MAIN video.** Stage 5 never ripples/deletes and never touches DIV or
  audio. This invariant keeps removal (Stage 6) and angle-switching separable: a span with one
  video track disabled = camera switch (kept); a span dead on every track = removed. Don't break
  it. Pinned by `tools/test_switch_angles.py`.
- **Person model is gitignored + auto-fetched.** `fetch_models.py` downloads MobileNet-SSD to
  `tools/models/` on first run (mirrors tried in order). A fresh checkout needs network once.
- **Anamorphic footage.** 1440×1080 stored, displayed 16:9. Detection frames are forced to 16:9
  (`scale=W:W*9/16`) so the subject isn't horizontally squished.
- **Premiere file-id dedup.** A Premiere-exported XML writes the `<pathurl>` only on the first
  clipitem per `<file id>`; later clips reference it by id. `detect_framing._main_clips` resolves
  the id → pathurl map, so it enumerates every MAIN segment (not just the first).
- **Assumes cut-mode `03_silence.xml`.** If Stage 3 was run in `--mode mute`, MAIN already carries
  disabled silence clips; switching still works but the snap edit-points and coverage get muddier.
- **Partial presence JSON → trailing DIV artifact.** If `detect_framing` only covered part of the
  timeline (`--end`/`--max-frames`) and the last sample was absent, the state machine holds DIV to
  the end. Run the full timeline for the real edit.
- **Runtime.** Stage 4 pipes one ffmpeg decode per MAIN clip segment; raw-pipe + DNN is far faster
  than the old Haar+JPEG path. The signal is cached to JSON so Stage 5 thresholds can be re-tuned
  without re-running CV.
- **Frame rate hardcoded to 25 fps**; UTF-8 stdout forced for accented console output.

## Changelog

- 2026-06-19 — **Re-based the trigger on speaker *presence* (MobileNet-SSD person detection),
  replacing the over-firing "no face on MAIN" rule.** Switched frame decode to raw-pipe→numpy
  (≈2.6×), added Premiere `<file id>` handling, on-demand model fetch (`fetch_models.py`), and
  renamed the signal `face_present`→`speaker_present` / `main_framing.json`→`main_presence.json`.
  Validated on a stress-test conference (13/14 real walk-offs caught, 0 false cuts); timing
  defaults locked and MAIN-biased.
- 2026-06-18 — Initial version. Two-tool camera stage (`detect_framing` + `switch_angles`); rule
  was DIV only when no face on MAIN (OpenCV Haar frontal+profile); hard cut with ~1 s pre-roll
  snapped to the nearest pause; 1 s min shot.
