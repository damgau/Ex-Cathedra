# Workflow: Stage 2 — Sync Cameras by Audio

Detect which camera/channel carries clean audio, cross-correlate waveforms to find the timeline offset, and shift the later-starting camera so both cameras align.

---

## Objective

Produce `OUTPUT/02_sync.xml` (an FCP XML with the later camera shifted into sync) and `OUTPUT/audio_config.json` (the chosen clean camera + channel for downstream stages). Success = scrubbing `02_sync.xml` in Premiere shows audio aligned across both cameras.

## Required Inputs

- `OUTPUT/01_create.xml` — Stage 1 output; the multi-cam FCP XML referencing the MAIN/DIV video MXFs. (Override with `--input`.)
- `INPUT/{MAIN CAM,DIV CAM}/` — the P2 card folders. Audio is read from `AUDIO/`, not the video MXF (see Edge Cases).

## Tools Used

- `tools/sync_audios.py` — channel detection, cross-correlation, XML shift.
- `tools/test_sync.py` — regression test that pins the sync **direction** convention.

## Steps

1. Confirm `OUTPUT/01_create.xml` exists and `ffmpeg`/`ffprobe` are on PATH.
2. Run `python tools/sync_audios.py`.
3. Review the printed per-channel metrics and the auto-selected clean camera/channel. The tool applies it automatically (no prompt); to override, re-run with `--force-camera` **and** `--force-channel` together.
4. Review the reported lead/shift — the later-starting camera is shifted into sync automatically.
5. Verify the result: confirm `OUTPUT/02_sync.xml` shifted the *later*-starting camera's clipitems (`start`/`end`), and scrub in Premiere.

## Expected Outputs

- `OUTPUT/02_sync.xml` — later camera shifted forward by the detected offset.
- `OUTPUT/audio_config.json` — `{"camera": "...", "channel": N}` (channel is 1-indexed).

## Edge Cases & Known Issues

- **Audio is NOT in the video MXF.** P2 stores each audio channel as `AUDIO/{stem}{channel-1:02d}.MXF`. Using `-map 0:a:N` on a `VIDEO/*.MXF` fails with "Stream map matches no streams". `extract_audio_channel` resolves the audio file and always uses `-map 0:a:0`. See `panasonic_p2_reference.md` §2–3.
- **Spanned/chained clips.** A recording is split across ~10-min MXFs linked via `CLIP/*.XML`. `_walk_p2_audio_chain` walks from the chain `Top` through `Next` so the full sample window (default 300 s) is collected even when the first clip is shorter.
- **Sync direction is sign-sensitive and was once inverted.** `find_sync_offset_frames(main, div)` returns MAIN's lead over DIV: positive = MAIN started earlier ⇒ shift **DIV** forward; negative ⇒ shift **MAIN**. Always shift the *later*-starting camera. This convention is pinned by `tools/test_sync.py` (MAIN-earlier and DIV-earlier cases) — run it after any change to the correlation logic.
- **Frame rate is hardcoded to 25 fps** in `find_sync_offset_frames` and the lead-seconds print. Correct for this PAL P2 footage; revisit if 24/29.97 material is ever used (the XML carries the true timebase in `sequence/rate/timebase`).
- **Partial `--force` flags are rejected.** `--force-camera` and `--force-channel` must be supplied together.

## Changelog

- 2026-06-17 — Initial version. Documented P2 audio extraction (audio MXF + chain walk), and the pinned sync-direction convention backed by `tools/test_sync.py`.
