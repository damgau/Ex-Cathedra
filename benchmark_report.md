# Camera-Switch Benchmark & Tuning Report (Stages 4–5)

How to get **very good results, quickly** from the presence-based camera switch
(`detect_framing.py` → `switch_angles.py`). Data below comes from `tools/benchmark_presence.py`
on a 10-min slice (detector/decode comparison) plus the real validation on a movement-heavy
conference (14 user-marked walk-offs).

---

## TL;DR — the locked, validated config

| stage | setting | value | why |
|---|---|---|---|
| 4 decode | strategy | **raw pipe → numpy** | 2.6× faster than JPEG-to-disk |
| 4 detector | model | **MobileNet-SSD person** (`cv2.dnn`) | 100% present on still footage, 0 false cuts; catches gestures/profile/slide-facing |
| 4 | `--fps` | 4 | presence changes slowly; 4 is safe, 2 is the speed lever |
| 4 | `--scale` | 480 | DNN re-blobs to 300² internally, so scale ≈ decode-speed only |
| 4 | `--conf` | 0.50 | present/absent threshold (the main Stage-4 bias dial) |
| 5 | `--trigger` / `--return` | 1.0 / 1.0 s | absence/return must persist this long (kills blips) |
| 5 | `--pre-roll` / `--post-roll` | 1.0 / 0.75 s | cut ~1 s before he leaves, hold ~0.75 s after he's back |
| 5 | `--snap-tolerance` | 0.75 s | snap cuts to the nearest silence pause |
| 5 | `--min-shot` | 1.0 s | no MAIN/DIV shot shorter than this |

**Validated:** on the stress-test conference these caught **13/14 real walk-offs, 0 false cuts**.
On a normal (stationary-speaker) talk they fire **~never** — which is the goal.

---

## 1. Speed — where the time goes, and the levers

Full 73-min conference Stage 4: **844.7 s (~14 min)**, 17,495 samples, ~5.2× realtime at
`fps=4, scale=480`. The DNN itself is cheap (~95 frames/s); most wall-time is **ffmpeg decode/seek
across the 321 clip segments**.

Levers, biggest win first:

| lever | effect | cost |
|---|---|---|
| **Reuse the cached signal** | Re-tuning **any Stage-5 knob is instant** — `main_presence.json` is cached, so you only re-run the CV pass if you change `--conf/--fps/--scale`. | none |
| **Validate on a slice first** | `detect_framing --end 120` or `--max-frames 200` → seconds, not minutes. Lock settings, then do the full run once. | none |
| **`--fps 4 → 2`** | ~halves samples ⇒ ~halves Stage-4 runtime. Exits are ≥2.2 s, so 2 fps still gets ≥4 samples/exit. | re-validate once |
| **`--scale 480 → 320`** | cheaper decode; DNN accuracy ~unchanged (it resizes to 300² anyway). | minor |
| decode strategy | already raw-pipe (2.6× vs JPEG-to-temp-dir, and no disk thrash). | done |

Decode A/B (200 frames @480): JPEG-to-temp-dir **49.9 f/s** vs raw-pipe **128.1 f/s** → **2.57×**.

---

## 2. Accuracy — why MobileNet-SSD won

Detector sweep on a **still-speaker** 10-min slice (he never leaves frame → ideal present % is
**100%**; any "absent" is a false cut):

| detector | present % | false absent-runs | detect speed | verdict |
|---|---|---|---|---|
| **MobileNet-SSD (dnn)** | **100%** | **0** | 95 f/s | ✅ robust to gestures/profile/slide-facing; scale-independent |
| face (old rule) | 83% | 36–40 | 65–107 f/s | ❌ over-fires (face gone when he faces slides) |
| hog (people) | 95→82→1% (@640/480/320) | 7→4→16 | 53–474 f/s | ❌ scale-fragile, false-negatives a standing speaker |
| motion (MOG2) | 100% | 0 | ~900 f/s | ❌ no signal — a still speaker reads "present" forever |

On the **real** conference (14 exits) the DNN caught **13/14, 0 false positives**; the one miss was
a moment where he was edge-framed but still visible (correctly "present").

---

## 3. The tuning knobs, and which way to turn them

### Stage 4 — `detect_framing.py` (re-runs the CV pass; slice-test it)

- **`--conf` (0.5)** — the present/absent dial.
  - **Lower (0.35)** → person detected more easily → more "present" → **fewer/later DIV cuts** (more MAIN-biased; may keep a marginal exit on MAIN).
  - **Higher (0.65)** → stricter → marginal/edge/partly-occluded framing counts as **absent** → **more DIV cuts** (catches borderline exits, risks false cuts).
- **`--fps` / `--scale`** — speed levers (see §1); leave for accuracy unless tuning runtime.

### Stage 5 — `switch_angles.py` (instant; reuses the cached signal)

- **`--trigger` (1.0 s)** — absence must persist this long to switch. ↓ catches shorter exits (more cuts); ↑ only sustained absences (more MAIN).
- **`--return` (1.0 s)** — presence must persist to switch back. ↑ holds DIV through brief reappearances (kills flicker when he bobs in/out of frame).
- **`--pre-roll` (1.0 s)** — cut to DIV this long *before* he leaves. ↑ covers the walk-out earlier; ↓ stays on MAIN longer.
- **`--post-roll` (0.75 s)** — hold DIV after he returns.
- **`--snap-tolerance` (0.75 s)** — pull each cut onto the nearest silence pause so it doesn't land mid-syllable.
- **`--min-shot` (1.0 s)** — floor on MAIN/DIV shot length; merges/drops anything shorter.

---

## 4. Quick-tuning playbook (symptom → fix)

| symptom | fix |
|---|---|
| Too many DIV cuts / leaves MAIN too readily | `--trigger ↑` (1.5–2 s), `--conf ↓` (0.35), `--min-shot ↑` |
| Misses real exits (too short) | `--trigger ↓` (0.5–0.75 s) |
| Misses exits where he's edge-framed/half-out | `--conf ↑` (0.6–0.7) — re-runs Stage 4 |
| DIV flickers (rapid MAIN↔DIV) | `--return ↑`, `--min-shot ↑` (2 s) |
| Cut lands mid-sentence | `--snap-tolerance ↑` |
| Shows empty MAIN before the cut | `--pre-roll ↑` |
| Run is too slow | `--fps 2`, `--scale 320`, and slice-test (`--end`/`--max-frames`) first |
| Re-tuning timing only | change Stage-5 flags — **no CV re-run needed** |

---

## 5. Re-running the benchmark

```bash
# detector × scale × decode-strategy on a slice (writes .tmp/benchmark_presence/report.md
# + annotated "absent" frames per detector to eyeball)
python tools/benchmark_presence.py --input OUTPUT/<slice>.xml \
    --detectors dnn,face,hog,motion --scales 640,480,320 \
    --dnn-model tools/models/MobileNetSSD_deploy.caffemodel \
    --dnn-config tools/models/MobileNetSSD_deploy.prototxt

# quick smoke
python tools/benchmark_presence.py --input OUTPUT/<slice>.xml --max-frames 200 --scales 480
```

Read `present %` (higher = stays on MAIN through gestures/slide-facing), `absent runs` (should be
few, genuine exits), `detect f/s`, and the dumped frames. If you have ground-truth OUT/IN
timecodes, score recall/precision the way we did (`.tmp/score_presence.py`).
