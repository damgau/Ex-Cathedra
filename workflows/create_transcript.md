# Workflow: Stage 4 — Create Word-Level Transcript

Extract the clean camera's audio from the silence-trimmed sequence and run local Whisper large-v3 to produce a French word-level transcript, with each word timestamped in **timeline frames**.

---

## Objective

Produce `OUTPUT/04_transcript.json` — every transcribed word with `start_frame`/`end_frame` (timeline frames), `start_s`/`end_s` (seconds into the reconstructed audio), and a confidence `probability`. Success = the frame ranges line up with the spoken words when scrubbing `OUTPUT/03_silence.xml` in Premiere.

## Required Inputs

- `OUTPUT/03_silence.xml` — Stage 3 output; the silence-trimmed FCP XML. (Override with `--input`.)
- `OUTPUT/audio_config.json` — `{"camera": "...", "channel": N}` from Stage 2 (channel is 1-indexed).
- `INPUT/{clean cam}/AUDIO/` + `INPUT/{clean cam}/CLIP/` — P2 audio MXFs and chain metadata. Audio is read from `AUDIO/`, **never** the video MXF (see Edge Cases).
- `.venv_whisper` — a **Python 3.11** venv with `faster-whisper` + `python-dotenv`. The main project Python (3.14) can't run faster-whisper.

## Tools Used

- `tools/create_transcript.py` — audio reconstruction from the XML, ffmpeg extraction, Whisper transcription, audio-seconds → timeline-frame mapping.

## Steps

1. Confirm `.venv_whisper/bin/python` exists. If the symlink is missing (it has been deleted in git before), recreate it: `python3.11 -m venv .venv_whisper && .venv_whisper/bin/pip install faster-whisper python-dotenv`. The site-packages may already be intact — only the bin symlinks break.
2. Confirm `OUTPUT/03_silence.xml` and `OUTPUT/audio_config.json` exist and `ffmpeg` is on PATH.
3. Run with the venv interpreter: `.venv_whisper/bin/python tools/create_transcript.py`.
4. First run downloads the large-v3 model (~3 GB) from Hugging Face — expect several minutes of low-CPU, network-bound time before transcription starts.
5. Review the printed segment count / total audio duration, then the "First 10 words" sanity check at the end.

## Expected Outputs

- `OUTPUT/04_transcript.json` — `{"model", "language", "words": [{word, start_frame, end_frame, start_s, end_s, probability}, ...]}`.

## Edge Cases & Known Issues

- **Audio is NOT in the video MXF.** P2 stores each audio channel as `AUDIO/{stem}{channel-1:02d}.MXF`. Running `-map 0:a:{channel-1}` against a `VIDEO/*.MXF` fails with "Stream map matches no streams" (the video MXF's audio appears only as undecodable `data` streams). `build_audio_for_whisper` resolves the audio file and always uses `-map 0:a:0`. See `panasonic_p2_reference.md` §2–3. *(This was the original bug — the tool predated the P2 reference and pointed ffmpeg at the wrong file.)*
- **Spanned/chained clips.** A recording is split across ~10-min MXFs linked via `CLIP/*.XML`. `_resolve_p2_chain` walks from the chain `Top` through `Next` and maps the clipitem's `[in, out]` (offsets from chain Top, not from the referenced file) onto per-physical-file slices before extraction. Ported from `tools/remove_silence.py:_resolve_p2_chain`.
- **Pathurl decoding.** Use `urllib.parse.unquote` (handles `%20`, `%28`, `%3a`) and strip the leading `/` before a Windows drive letter (`/G:/…` → `G:/…`). See `panasonic_p2_reference.md` §5.
- **Muted clips are skipped.** Stage 3's mute mode sets `<enabled>FALSE</enabled>`; those clipitems are excluded from `_get_cam_clips_for_audio` so they don't appear in the transcript.
- **Timeline-frame mapping.** `segment_map` records, per extracted slice, `(audio_start_s, audio_end_s, timeline_start_frame, timeline_end_frame)`. `audio_time_to_frames` converts a Whisper timestamp back to a timeline frame and clamps past-end times to the last segment.
- **Frame rate hardcoded to 25 fps.** Correct for this PAL P2 footage; revisit for 24/29.97 material.
- **Python block-buffers stdout when redirected to a file.** The `[1/4]…[4/4]` progress prints won't appear in a redirected log until the buffer flushes or the run ends. The unbuffered `HF_TOKEN` stderr warning (harmless) is the earliest sign the model is loading — and since it fires in step `[4/4]`, seeing it confirms audio assembly (`[3/4]`) already succeeded.

## Changelog

- 2026-06-17 — Initial version. Fixed P2 audio extraction (the tool was reading audio from the video MXF instead of the `AUDIO/` MXFs) by porting the audio-file resolution + `_resolve_p2_chain` chain walk and `unquote` pathurl decoding from `tools/remove_silence.py`. Documented the venv-symlink recreation step and stdout buffering gotcha.
