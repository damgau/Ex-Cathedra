# EX Cathedra — Video Editing Automation Pipeline

Automates the repetitive editing work for solo conference recordings shot with 2 cameras. Starting from raw MXF files, the pipeline syncs cameras, cuts silence, transcribes speech, and removes fillers — producing a Premiere Pro-ready FCP XML at each stage so you can validate before moving forward.

## Pipeline overview

```
INPUT/MAIN CAM/ + INPUT/DIV CAM/
        ↓ tools/create_xml.py       →  OUTPUT/01_create.xml
        ↓ tools/sync_audios.py      →  OUTPUT/02_sync.xml
        ↓ tools/remove_silence.py   →  OUTPUT/03_silence.xml
        ↓ tools/create_transcript.py→  OUTPUT/04_transcript.json
        ↓ tools/remove_fillers.py   →  OUTPUT/05_fillers.xml  ← import into Premiere
```

Each stage reads the previous stage's output. Run them in order, validate in Premiere, then proceed.

## Stages

| Stage | Script | What it does |
|-------|--------|-------------|
| 1 | `create_xml.py` | Scans P2 metadata, chains MXF clips in order, outputs base FCP XML with both cameras on V1/V2 |
| 2 | `sync_audios.py` | Detects the cleanest audio channel, cross-correlates cameras, shifts one track to align |
| 3 | `remove_silence.py` | Detects pauses ≥ 1.5s on the clean channel, cuts or mutes them across all tracks |
| 4 | `create_transcript.py` | Runs `faster-whisper` (large-v3, French) on post-cut audio, outputs word-level timestamps |
| 5 | `remove_fillers.py` | Removes fixed fillers (`euh`, `ah`, `bah` …) and detected verbal tics from the timeline |

## Setup

```bash
# Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

# Install dependencies
pip install -r requirements.txt

# ffmpeg must be installed on the system
# Windows: winget install ffmpeg   |   macOS: brew install ffmpeg
```

No `.env` is required for the current stages — all processing is local.

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
