# EX Cathedra — Video Editing Automation Pipeline

Automates the repetitive editing work for solo conference recordings shot with 2 cameras
(MAIN = primary/active, DIV = wide-angle safety). Starting from raw MXF files, the pipeline
syncs the cameras, cuts silence, and switches to the DIV angle when the speaker leaves
MAIN's frame — producing a Premiere Pro-ready FCP XML at each stage so you can validate
before moving forward. An optional slides side-chain places the speaker's deck full-screen
on a V3 track.

## Pipeline overview

```
INPUT/MAIN CAM/ + INPUT/DIV CAM/
        ↓ tools/create_xml.py        →  OUTPUT/01_create.xml
        ↓ tools/sync_audios.py       →  OUTPUT/02_sync.xml
        ↓ tools/remove_silence.py    →  OUTPUT/03_silence.xml
        ↓ tools/detect_framing.py    →  OUTPUT/main_presence.json   (CV: is the speaker in MAIN's frame?)
        ↓ tools/switch_angles.py     →  OUTPUT/04_angles.xml         ← reviewed in Premiere = final cut
```

Each stage reads the previous stage's output. Run them in order, validate in Premiere, then
proceed. `switch_angles` only *disables* MAIN's video across each DIV window (it never
ripples, and never touches DIV or audio), so you can re-enable or add a switch in Premiere
before the final export — the reviewed `04_angles.xml` is the deliverable.

### Slides track (optional side-chain)

Places the speaker's support deck full-screen on a new **V3** track, timed to the talk by
reading the locked DIV camera:

```
deck (PDF / PPTX / JPEG folder)
        ↓ tools/slides_to_tv.py      →  OUTPUT/slides/<deck>/      (1920×1080 PNGs + manifest.json)
OUTPUT/04_angles.xml
        ↓ tools/create_transcript.py →  OUTPUT/04_transcript.json  (Whisper pre-step, .venv_whisper)
        ↓ tools/place_slides.py      →  OUTPUT/05_slides.xml
```

## Stages

| Stage | Script | What it does |
|-------|--------|-------------|
| 1 | `create_xml.py` | Scans P2 metadata, chains MXF clips in order, outputs base FCP XML with both cameras on V1/V2 |
| 2 | `sync_audios.py` | Detects the cleanest audio channel, cross-correlates cameras, shifts the later one to align |
| 3 | `remove_silence.py` | Detects pauses ≥ 1.5s on the clean channel, cuts or mutes them across all tracks |
| 4 | `detect_framing.py` | Samples MAIN and runs MobileNet-SSD person detection — is the speaker present in MAIN's frame? Caches `main_presence.json` |
| 5 | `switch_angles.py` | Stays on MAIN; disables MAIN video (revealing DIV) where the speaker is out of frame, ~1s early, snapped to a pause |

**Slides side-chain (optional):** `slides_to_tv.py` converts a deck to broadcast 1920×1080
PNGs; `create_transcript.py` (Whisper) produces a word-level transcript; `place_slides.py`
reads the locked DIV camera and lays the slides onto V3 (`05_slides.xml`).

## Setup

```bash
# System Python 3.14 is the working interpreter (cv2 / numpy / scipy already installed).
pip install -r requirements.txt

# create_transcript only needs a separate Python 3.11 venv (faster-whisper has no 3.14 wheels):
#   .venv_whisper\Scripts\python.exe tools\create_transcript.py

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
OUTPUT/        # FCP XMLs, presence JSON, and slides produced by each stage
tools/         # One Python script per pipeline stage
workflows/     # Markdown SOPs describing each stage in detail
.tmp/          # Temporary files (disposable, auto-generated)
PLAN.md        # Full architecture and algorithm spec
```

## Per-project usage

Each conference gets its own clone of this folder. Drop the MXF files into `INPUT/MAIN CAM/`
and `INPUT/DIV CAM/`, then run the stages in sequence. The tools run **autonomously** — they
auto-apply the detected result (channel, sync offset, DIV windows) and expose `--force-*`
and tuning flags for overrides, so no stage blocks on a prompt.
