# EX Cathedra — Video Editing Automation Pipeline

Automates the repetitive editing work for solo conference recordings shot with 2 cameras. Starting from raw MXF files, the pipeline syncs cameras, cuts silence, transcribes speech, and removes fillers — producing a Premiere Pro-ready FCP XML at each stage so you can validate before moving forward.

## Pipeline overview

```
INPUT/MAIN CAM/ + INPUT/DIV CAM/
        ↓ tools/create_xml.py        →  OUTPUT/01_create.xml
        ↓ tools/sync_audios.py       →  OUTPUT/02_sync.xml
        ↓ tools/remove_silence.py    →  OUTPUT/03_silence.xml
        ↓ tools/create_transcript.py →  OUTPUT/04_transcript.json
        ↓ tools/remove_fillers.py    →  OUTPUT/05_fillers.xml   (fillers/tics disabled, not cut)
        ↓ [review in Premiere → export] OUTPUT/06_reviewed.xml
        ↓ tools/delete_enable_clip.py→  OUTPUT/07_final.xml  ← final import into Premiere
```

Each stage reads the previous stage's output. Run them in order, validate in Premiere, then proceed. Stage 5 only *disables* fillers/tics so you can review and correct them in Premiere; Stage 6 ripple-deletes whatever you left disabled into the final cut.

## Stages

| Stage | Script | What it does |
|-------|--------|-------------|
| 1 | `create_xml.py` | Scans P2 metadata, chains MXF clips in order, outputs base FCP XML with both cameras on V1/V2 |
| 2 | `sync_audios.py` | Detects the cleanest audio channel, cross-correlates cameras, shifts one track to align |
| 3 | `remove_silence.py` | Detects pauses ≥ 1.5s on the clean channel, cuts or mutes them across all tracks |
| 4 | `create_transcript.py` | Runs `faster-whisper` (large-v3, French) on post-cut audio, outputs word-level timestamps |
| 5 | `remove_fillers.py` | *Disables* (no cut) fixed fillers + stutters by default; verbal tics are opt-in, with connectives (`donc`, `alors`…) demoted to avoid choppy over-cutting |
| 6 | `delete_enable_clip.py` | Ripple-deletes spans disabled on every track from the reviewed export; preserves multicam camera switches |

## Setup

```bash
# Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

# Install dependencies
pip install -r requirements.txt

# ffmpeg must be installed on the system
# Windows: winget install Gyan.FFmpeg   |   macOS: brew install ffmpeg
```

ffmpeg does **not** need to be on PATH: the tools auto-detect a winget install
(`Gyan.FFmpeg`). If yours lives elsewhere, set `FFMPEG_BIN` (and `FFPROBE_BIN`) in
`.env` to the full binary path. Otherwise no `.env` is required — all processing is local.

## Project layout

```
INPUT/
  MAIN CAM/   # Raw MXF files — primary/active camera
  DIV CAM/    # Raw MXF files — wide-angle safety camera
OUTPUT/        # FCP XMLs and transcript produced by each stage
tools/         # One Python script per pipeline stage
workflows/     # Markdown SOPs describing each stage in detail
tmp/           # Temporary files (disposable, auto-generated)
PLAN.md        # Full architecture and algorithm spec
```

## Per-project usage

Each conference gets its own clone of this folder. Drop the MXF files into `INPUT/MAIN CAM/` and `INPUT/DIV CAM/`, then run the stages in sequence.

Stages 2 and 5 are interactive — they ask for confirmation before applying detected sync offsets or verbal tic removals.
