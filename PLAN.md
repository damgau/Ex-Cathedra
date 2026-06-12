# Plan: Ex Cathedra — Video Editing Automation Pipeline

## Context
Solo conference recording with 2 cameras (MAIN CAM = active/primary, DIV CAM = wide-angle safety). Goal is to automate the repetitive editing work: syncing cameras, cutting silence, transcribing speech, and removing fillers. Pipeline starts from raw MXF files on disk — no XML input. Each stage produces an FCP XML that can be imported into a fresh Premiere Pro project. The editor validates each stage before running the next.

---

## Scope (BETA)
Each conference = its own cloned project folder. Paths are hardcoded to `INPUT/MAIN CAM/`, `INPUT/DIV CAM/`, and `OUTPUT/`. Configurable `--main-cam` / `--div-cam` path args are a post-beta enhancement.

## Architecture
5 separate Python tools in `tools/`, one per stage. Each follows the WAT `_template.py` pattern (argparse, .env loading, output to `OUTPUT/`). Stages are run manually in sequence.

**Stage flow:**
```
INPUT/MAIN CAM/ + INPUT/DIV CAM/
        ↓ create_xml.py
OUTPUT/01_create.xml
        ↓ sync_audios.py
OUTPUT/02_sync.xml
        ↓ remove_silence.py
OUTPUT/03_silence.xml
        ↓ create_transcript.py
OUTPUT/04_transcript.json
        ↓ remove_fillers.py
OUTPUT/05_fillers.xml   ← final import into Premiere
```

---

## Stage 1 — `tools/create_xml.py`

**What it does:** Scans `INPUT/`, reads P2 chain metadata, and generates a base FCP XML with both cameras placed end-to-end on the timeline.

**Algorithm:**
1. Scan `INPUT/MAIN CAM/CLIP/*.XML` and `INPUT/DIV CAM/CLIP/*.XML`
2. For each camera, build clip order by following the P2 `<Next>` / `<Top>` chain links
3. Compute each clip's duration from P2 metadata (in frames @ 25fps)
4. Generate `<xmeml version="4">` sequence matching `tmp/sync_example.xml` structure:
   - V1 = DIV CAM (10 consecutive `<clipitem>` elements), V2 = MAIN CAM (10 consecutive)
   - 4 mono audio tracks per camera (A1–A4 = MAIN CAM, A5–A8 = DIV CAM)
   - **No motion effects** — the 1440x1080 `pixelaspectratio` (`HD-(1440x1080)`) is declared on each clip and Premiere handles fit-to-frame natively via PAR. No Scale or Distort effects in the XML.
   - `<pathurl>` = `file://localhost/` + absolute path to each MXF in `INPUT/*/VIDEO/`
   - Timecode: NDF, 25fps, `<frame>0</frame>` for each clip (offset handled in stage 2)
5. Both cameras start at timeline position 0 (unsynchronized — sync is stage 2's job)

**Efficiency note:** Conferences run 50 min–2 hrs. MXF files are large. All audio operations must use `ffmpeg` pipe extraction (never load full files into RAM). Silence detection runs on a streamed sliding window. P2 metadata gives exact clip durations — never read MXF headers for timing.

**Output:** `OUTPUT/01_create.xml`

**Reference:** `tmp/sync_example.xml` — exact structure to replicate for XML element names, attributes, link groups, and effect blocks.

---

## Stage 2 — `tools/sync_audios.py`

**What it does:** Detects which camera/channel has the clean audio, then finds the sync offset via cross-correlation and shifts one camera's timeline position.

**Algorithm:**
1. Parse `OUTPUT/01_create.xml` (or `--input`)
2. **Audio channel detection:**
   - Extract all 8 channels (4 per camera) from MXF files using `ffmpeg`
   - Analyze each channel: compute RMS level + spectral clarity (SNR estimate)
   - Rank channels, identify best candidate per camera, surface result:
     ```
     Detected clean audio: DIV CAM — Channel 2 (RMS: -18dBFS, SNR: 42dB)
     MAIN CAM best channel: Channel 1 (RMS: -31dBFS, SNR: 18dB)
     Proceed? [Y/n] or override: [camera] [channel]
     ```
   - Store selected channel index for downstream stages (written to `OUTPUT/audio_config.json`)
3. **Sync via cross-correlation:**
   - Extract mono audio from the confirmed channel of each camera (downmixed if needed)
   - Downsample to 8kHz for speed
   - Extract only a representative sample for correlation (e.g., 5 minutes from the start of whichever camera starts later) — do not load full audio into RAM
   - Compute cross-correlation using `scipy.signal.correlate`
   - Find peak lag → convert to frames (lag_samples / 8000 * 25)
   - **Either camera can be the master**: the camera that started recording first (lag < 0 means DIV CAM started first; lag > 0 means MAIN CAM started first). The earlier camera stays at position 0; the later camera's clipitems are all shifted by the detected offset.
4. Update all shifted camera's `<clipitem>` `<start>` / `<end>` values in the XML by the offset

**Output:** `OUTPUT/02_sync.xml`, `OUTPUT/audio_config.json`

**CLI args:** `--input`, `--output`, `--force-camera [main|div]`, `--force-channel [1-4]`

---

## Stage 3 — `tools/remove_silence.py`

**What it does:** Detects long pauses in the clean audio and cuts/mutes them on both camera tracks simultaneously.

**Algorithm:**
1. Parse `OUTPUT/02_sync.xml` + `OUTPUT/audio_config.json` (to know which channel to analyze)
2. Extract clean audio channel with `ffmpeg`
3. Detect silence: sliding RMS window, threshold = -40dBFS, minimum duration = 1.5s
4. For each silence region, apply 0.3s handles (keep 0.3s of audio on each side)
5. Apply cuts to XML for ALL tracks simultaneously (both cameras, all audio tracks):
   - `--mode cut` (default): split clipitems, remove gap, shift all subsequent items left
   - `--mode mute`: split clipitems, set `<enabled>FALSE</enabled>` on removed segment

**Output:** `OUTPUT/03_silence.xml`

**CLI args:** `--input`, `--output`, `--mode [cut|mute]`, `--silence-threshold` (dBFS, default -40), `--silence-min-duration` (seconds, default 1.5), `--silence-padding` (seconds, default 0.3)

---

## Stage 4 — `tools/create_transcript.py`

**What it does:** Generates a word-level French transcript from the clean audio using local Whisper.

**Algorithm:**
1. Parse `OUTPUT/03_silence.xml` + `OUTPUT/audio_config.json`
2. Reconstruct the post-cut audio timeline: extract only the kept segments, concatenate with `ffmpeg`
3. Run `faster-whisper` large-v3 on the concatenated audio:
   - Language: `fr`
   - Word timestamps: `word_timestamps=True`
4. Output a JSON with word-level timestamps aligned to the post-cut timeline

**Output:** `OUTPUT/04_transcript.json`

**Format:**
```json
{
  "words": [
    {"word": "Bonjour", "start": 0.52, "end": 0.91, "confidence": 0.98},
    ...
  ]
}
```

**CLI args:** `--input`, `--output`, `--model` (default `large-v3`)

---

## Stage 5 — `tools/remove_fillers.py`

**What it does:** Removes filler sounds and verbal tics from the timeline using transcript timestamps.

**Algorithm:**
1. Parse `OUTPUT/03_silence.xml` + `OUTPUT/04_transcript.json`
2. **Fixed fillers:** Match words against list: `aaaah`, `euuuh`, `beeen`, `hmm`, `euh`, `ah`, `bah` (case-insensitive, fuzzy match on confidence < 0.7)
3. **Verbal tic detection:**
   - Tokenize transcript into unigrams + bigrams
   - Count occurrences normalized per hour of content
   - Flag candidates appearing > 5× per hour (configurable)
   - Display for confirmation:
     ```
     Detected verbal tics:
       "du coup"   — 47 occurrences (31/hr)  [remove? Y/n]
       "en fait"   — 22 occurrences (14/hr)  [remove? Y/n]
       "voilà"     —  9 occurrences (6/hr)   [remove? Y/n]
     ```
4. Convert confirmed word timestamps → frame positions in the XML timeline
5. Apply cuts to ALL tracks simultaneously (same cut/mute logic as stage 3)

**Output:** `OUTPUT/05_fillers.xml`

**CLI args:** `--input`, `--transcript`, `--output`, `--mode [cut|mute]`, `--tic-threshold` (occurrences/hr, default 5)

---

## Dependencies to add to `requirements.txt`
```
faster-whisper
librosa
scipy
numpy
lxml
ffmpeg-python
```
Note: `ffmpeg` binary must be installed on the system (`brew install ffmpeg`).

---

## Shared utilities (inline, not a separate module)
- P2 XML chain parser — reads `<Next>` / `<Top>` links from CLIP/*.XML
- FCP XML builder — thin wrapper around `lxml` for creating/mutating xmeml elements
- Frame ↔ seconds conversion at 25fps NDF
- Audio extractor — thin ffmpeg-python wrapper for channel extraction

---

## Verification (end-to-end test)
1. Run `create_xml.py` → open `OUTPUT/01_create.xml` in Premiere, confirm both cameras appear on V1/V2 with correct clip count and durations
2. Run `sync_audios.py` → open `OUTPUT/02_sync.xml`, scrub timeline, confirm clap/transient aligns across cameras
3. Run `remove_silence.py` → open `OUTPUT/03_silence.xml`, confirm dead air is gone, speech is unclipped
4. Run `create_transcript.py` → open `OUTPUT/04_transcript.json`, spot-check 5–10 words against audio
5. Run `remove_fillers.py` → open `OUTPUT/05_fillers.xml`, scrub through known filler moments, confirm clean cuts
