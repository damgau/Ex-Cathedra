# Plan: Ex Cathedra — Video Editing Automation Pipeline

## Context
Solo conference recording with 2 cameras (MAIN CAM = active/primary, DIV CAM = wide-angle safety). Goal is to automate the repetitive editing work: syncing cameras, cutting silence, transcribing speech, and removing fillers. Pipeline starts from raw MXF files on disk — no XML input. Each stage produces an FCP XML that can be imported into a fresh Premiere Pro project. The editor validates each stage before running the next.

---

## Scope (BETA)
Each conference = its own cloned project folder. Paths are hardcoded to `INPUT/MAIN CAM/`, `INPUT/DIV CAM/`, and `OUTPUT/`. Configurable `--main-cam` / `--div-cam` path args are a post-beta enhancement.

## Architecture
Separate Python tools in `tools/`, one per stage. Each follows the WAT `_template.py` pattern (argparse, .env loading, output to `OUTPUT/`). Stages are run manually in sequence. Camera-angle switching (Stage 4–5 below) only *disables MAIN video* so the DIV angle shows; the editor reviews in Premiere and exports a reviewed XML, then Stage 6 commits it into the final cut.

**Set aside (see `maybe_later/`):** `create_transcript` (old Stage 4) and `remove_fillers` (old Stage 5)
are parked — Whisper strips disfluencies during transcription, so a transcript-driven filler remover has
nothing to act on. A future *audio-based* filler remover would replace them. Detail: `maybe_later/NOTES.md`.

**Stage flow (current):**
```
INPUT/MAIN CAM/ + INPUT/DIV CAM/
        ↓ create_xml.py
OUTPUT/01_create.xml
        ↓ sync_audios.py
OUTPUT/02_sync.xml
        ↓ remove_silence.py
OUTPUT/03_silence.xml
        ↓ detect_framing.py    (CV: is the speaker's face visible on MAIN?)
OUTPUT/main_framing.json
        ↓ switch_angles.py     (hard-cut to DIV where no face; pre-roll + snap-to-pause; only MAIN video disabled)
OUTPUT/04_angles.xml
        ↓ [import to Premiere → review, disable bad takes → export]
OUTPUT/06_reviewed.xml
        ↓ delete_enable_clip.py   (ripple-delete spans dead on all tracks; camera switches preserved)
OUTPUT/07_final.xml   ← final import into Premiere
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

## Parked (→ `maybe_later/`) — `create_transcript.py`  *(was Stage 4)*

> **Set aside 2026-06-18.** Whisper strips disfluencies during transcription, so the transcript→filler
> path is a dead end; see `maybe_later/NOTES.md`. Original design kept below for reference.

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

## Parked (→ `maybe_later/`) — `remove_fillers.py`  *(was Stage 5)*

> **Set aside 2026-06-18.** Useless/wrong output: its transcript input has no fillers because Whisper
> already removed them. A future *audio-based* remover would replace it; see `maybe_later/NOTES.md`.

**What it does:** Detects filler sounds and verbal tics and **disables** them on every track
(split clip + `<enabled>FALSE</enabled>`). Disable-only — it never ripples or deletes. The
editor reviews/corrects in Premiere; Stage 6 commits the result. Timeline length is unchanged.

**Conservative by default** — disfluencies are pre-cleaned by Whisper, so the auto pass only
flags always-safe targets (fixed fillers + stutters, both ≈0 in practice); tic removal is
opt-in. This avoids over-cutting (the first run bulk-removed connectives → 161 cuts, choppy).

**Algorithm:**
1. Parse `OUTPUT/03_silence.xml` + `OUTPUT/04_transcript.json`
2. **Fixed fillers:** regex interjection patterns (`ah`, `euh`, `hmm`, `bah`, `ben`, `wah`,
   `mm`, `pff`). NB Whisper large-v3 strips these during transcription, so usually a no-op;
   kept as cheap insurance. (The old low-confidence-short-word heuristic was removed — 100%
   false positives on ordinary function words.)
3. **Stutters/repetitions** (default-on, `--no-stutters` to disable): exact consecutive
   duplicate words + back-to-back phrase restarts. Also ≈0 on Whisper output (it cleans
   disfluencies); insurance for other speakers/recordings.
4. **Verbal tic detection (opt-in):**
   - Unigrams + bigrams **filtered through a French `STOPWORDS` blocklist** so function
     words/topic nouns don't surface.
   - Candidates flagged: **★ `KNOWN_TICS`** = crutch fillers (`du coup`, `en fait`, `genre`…),
     **⚠ `CONNECTIVES`** = sentence glue (`donc`, `alors`, `et donc`…). Nothing auto-selects;
     selecting a connective prints a caution.
   - Interactive selection or `--tics`.
5. `apply_cuts(mode="mute")` on ALL tracks → split + `<enabled>FALSE</enabled>` (no ripple)

**Output:** `OUTPUT/05_fillers.xml`

**CLI args:** `--input`, `--transcript`, `--output`, `--tic-threshold` (occurrences/hr, default 5), `--tics "phrase,phrase"` (non-interactive selection), `--no-stutters`, `-y/--yes` (skip confirm)

**Workflow:** `workflows/remove_fillers.md`

---

## Stage 6 — `tools/delete_enable_clip.py`

**What it does:** Commits an editor-reviewed timeline into the final cut by ripple-deleting
every span that is dead on **all** tracks. Multicam-safe: a span where only one video track is
disabled (camera switch) is preserved.

**Algorithm:**
1. Parse `OUTPUT/06_reviewed.xml` (the Premiere export after review)
2. Build the union of all *enabled* clip coverage across every track
3. `removable_spans` = complement of that union within `[0, timeline_end)` (gaps + all-disabled spans)
4. `apply_cuts(mode="cut")` to ripple-delete those spans across all tracks
5. Report spans removed (count + seconds), disabled clips deleted vs retained, old → new duration

**Output:** `OUTPUT/07_final.xml`

**CLI args:** `--input` (default `OUTPUT/06_reviewed.xml`), `--output` (default `OUTPUT/07_final.xml`)

**Workflow:** `workflows/delete_enable_clip.md`

**Caveat:** input is a Premiere export, not our generated XML — keys only off
`start`/`end`/`in`/`out`/`enabled`; validate against a real export before trusting it.

---

## Stage 4 — `tools/detect_framing.py` (camera switching, part 1)

**What it does:** Samples the MAIN video over the silence-trimmed timeline and runs OpenCV Haar face
detection to answer, per sampled instant, "is the speaker's face visible on MAIN?". Caches the signal so
the switch thresholds can be re-tuned without re-running the slow CV pass.

**Algorithm:**
1. Parse `OUTPUT/03_silence.xml`; find the MAIN **video** track by `file/pathurl` containing `MAIN%20CAM`.
2. Per MAIN clipitem, map timeline `[start,end]` → source `[in,out]` and resolve the P2 spanned chain
   (`remove_silence._resolve_p2_chain`) → physical `VIDEO/*.MXF` slices (H.264 at stream 0, decodable).
3. Extract frames at `--fps` (default 4), downscaled to `--scale` width (default 640), via an
   `ffmpeg … -f image2pipe -vcodec mjpeg pipe:1`, decoded with `cv2.imdecode`.
4. Detect the speaker with **frontal + profile Haar cascades** (profile on the frame and its mirror, from
   `cv2.data.haarcascades`); `face_present = any cascade fires` above a `--min-face` size floor. Profile
   detection is what keeps a head-turn counting as "face visible" under the conservative rule.

**Output:** `OUTPUT/main_framing.json` — `{fps, params, samples:[{timeline_frame, face_present}, …]}`.

**CLI:** `--input`, `--output`, `--fps` (4), `--scale` (640), `--min-face`, `--neighbors`/`--min-size`,
`--start`/`--end`/`--max-frames`.

---

## Stage 5 — `tools/switch_angles.py` (camera switching, part 2)

**What it does:** Turns the framing signal into a multicam timeline: stays on MAIN, hard-cuts to DIV
where no face is detected, cutting ~1 s early and snapping to pauses. **Only ever disables MAIN video** —
never DIV, never audio, never all tracks — so `delete_enable_clip.py` still reads each switch as
"preserve" (one video track disabled = camera switch). Reveals DIV by splitting + disabling the MAIN clip
via `remove_silence._apply_one_cut([main_video_track], s, e, mode="mute", id_counter)`.

**Decision logic (order matters):** face signal → **trigger** filter (no-face ≥ `--trigger` s qualifies a
run; face ≥ `--return` s ends it) → **pre/post-roll** (`--pre-roll` 1.0, `--post-roll` 0.75) → **snap**
each boundary to the nearest MAIN edit point (silence cut) within `--snap-tolerance` (0.75) → **merge**
overlaps → enforce **`--min-shot`** 1.0 s (merge windows separated by a sub-1 s MAIN gap) → split-and-
disable MAIN per window.

**Output:** `OUTPUT/04_angles.xml` (same `<duration>` as `03_silence.xml` — disable-only, no ripple).

**CLI:** `--input`, `--framing`, `--output`, `--trigger`, `--return`, `--pre-roll`, `--post-roll`,
`--snap-tolerance`, `--min-shot`, `-y/--yes`.

**Workflow:** `workflows/switch_angles.md`. **Test:** `tools/test_switch_angles.py`.

**Future polish (not beta):** cross-dissolve transitions (opacity ramp on MAIN); DIV punch-in (scale to
tighten the wide shot); CLAHE/equalized detection frames; eye/gaze-based framing instead of any-face.

**Invariant to protect:** the camera tools must only ever disable *video* tracks, never all tracks and
never audio. As long as that holds, removal (Stage 6) and angle-switching stay separable.

---

## Dependencies to add to `requirements.txt`
```
faster-whisper   # parked (maybe_later/) — only for create_transcript
librosa
scipy
numpy
lxml
ffmpeg-python
opencv-python    # Stage 4 detect_framing — Haar cascades ship in cv2.data.haarcascades
```
Note: `ffmpeg` binary must be installed on the system (`brew install ffmpeg`).
Note: `opencv-python` 4.13 confirmed importing on the project's Python 3.14.

---

## Shared utilities

**Extracted to a module:**
- Cut / mute engine — `tools/timeline.py` (`ripple_cut`, `mute_spans`, `disable_span`,
  `all_tracks`). The xmeml clipitem split/clone/shift/disable logic. Was inline in
  `remove_silence` and imported privately by `switch_angles` / `delete_enable_clip`;
  promoted to one named seam (2026-06-22). `disable_span` keeps ADR-0001 legible.

**Still inline (candidates to consolidate into `timeline.py` later — see `maybe_later/NOTES.md`):**
- P2 XML chain parser — reads `<Next>` / `<Top>` links from CLIP/*.XML
  (re-walked 3 ways: `remove_silence._resolve_p2_chain`, `sync_audios._walk_p2_audio_chain`,
  `create_xml.build_chain`)
- Camera-track + `pathurl` reader — "find the camera's track by %20-encoded label, read
  clipitem endpoints", duplicated across 4 tools with a divergent pathurl decoder
- Frame ↔ seconds ↔ ticks conversion at 25fps NDF — `FPS` / `TICKS_PER_FRAME` redefined
  across ~6 files
- Audio / frame extractor — thin ffmpeg wrappers for channel + frame extraction

---

## Verification (end-to-end test)
1. Run `create_xml.py` → open `OUTPUT/01_create.xml` in Premiere, confirm both cameras appear on V1/V2 with correct clip count and durations
2. Run `sync_audios.py` → open `OUTPUT/02_sync.xml`, scrub timeline, confirm clap/transient aligns across cameras
3. Run `remove_silence.py` → open `OUTPUT/03_silence.xml`, confirm dead air is gone, speech is unclipped
4. Run `detect_framing.py` → check `OUTPUT/main_framing.json`: printed face-coverage % and longest no-face stretch match what you know of the footage
5. Run `switch_angles.py` → open `OUTPUT/04_angles.xml` (same length as `03`), confirm it holds on MAIN and cuts to DIV ~1 s before the speaker leaves frame (on a pause), no shot < 1 s; review, disable bad takes, export `OUTPUT/06_reviewed.xml`
6. Run `delete_enable_clip.py` → open `OUTPUT/07_final.xml`, confirm dead air is gone, camera switches are preserved, and the timeline plays clean
