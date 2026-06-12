# Video Editing Workflow Automation

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
4. create_transcript — Generate a transcript of the spoken audio with
   word-level timestamps. Spoken language: French.
5. remove_fillers — Using the transcript, detect and remove:
   - Filler sounds: "Aaaah", "Euuuh", "Beeen", "Hmm", etc.
   - Verbal tics: words/phrases the speaker repeats abnormally often
     (e.g., "du coup", "en fait") are treated as tics and removed.

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