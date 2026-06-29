# Video Editing Workflow Automation

> **Historical kickoff brief (2026-06, frozen).** The stage list below is the original plan —
> including the retired Haar/face camera design and the now-current numbering. For the actual
> pipeline see `PLAN.md` and `README.md`; this file is kept only as a record of initial intent.

## Context
I want to build a program that automates my video editing workflow in
Premiere Pro. We work step by step, validating each stage before moving
on to the next.

## Input
The program takes Final Cut Pro XML files exported from Premiere Pro
(File > Export > Final Cut Pro XML...), structured like the attached
`sync_example.xml`. The output must be a new XML that can be re-imported
into a fresh Premiere Pro project.

## Pipeline stages
1. create_xml — Parse the imported Premiere Pro XML and generate a new
   project XML as the working base.
2. sync_audios — Synchronize multiple camera angles (e.g., camera 1 & 2
   rushes) using ONLY audio waveform analysis (no timecode, no metadata).
3. remove_silence — Detect and cut silent sections from the timeline.
4. detect_framing — CV pass (OpenCV Haar) over the MAIN video: is the
   speaker's face visible? Caches OUTPUT/main_framing.json.
5. switch_angles — Stay on MAIN; hard-cut to the DIV wide angle where no
   face is detected, cutting ~1s early and snapping to pauses. Only the
   MAIN video track is disabled. Output OUTPUT/04_angles.xml.

Parked (moved to maybe_later/): create_transcript + remove_fillers —
Whisper strips fillers during transcription, so the transcript-driven
remover had nothing to act on. A future audio-based remover may replace
them. See maybe_later/NOTES.md.

## Reference files
- sync_example.xml — reference of the expected output format
- IA.prproj — Premiere Pro project files
- sequence_setting.jpg — screenshot of the Premiere timeline settings
- "Adobe Premiere Pro Auto-Save" folder — ignore

## Working rules
- Maintain this MEMO.md: progress, decisions, next steps.
- Complete and validate one stage before starting the next.
- Anticipate future problems (frame rates, sample rates, multi-track
  audio, long files, drop-frame timecode, etc.).
- Before implementing any solution, explain it first and wait for
  my approval.