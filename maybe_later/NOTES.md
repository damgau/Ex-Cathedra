# Maybe Later — parked tools

Tools that were built but set aside. Kept (not deleted) so the work and its lessons survive.

---

## `create_transcript` (old Stage 4) + `remove_fillers` (old Stage 5)

**Parked: 2026-06-18.**

### Why parked

`remove_fillers` produced useless / wrong results in practice. The root cause is upstream:

> **Whisper (large-v3) removes filler words by default.** Disfluencies like `eeeeuh`, `beeeen`, `hmm`,
> `bah`, stutters and false starts are normalised away *during transcription* — they live in the
> **audio**, not in the transcript. So a transcript-driven filler remover has almost nothing to act on
> (on the test conference: ~0 fixed-filler hits, ~3 stutter regions in 79 min).

Because the transcript is the input to `remove_fillers`, removing fillers from the transcript can never
remove what isn't there. The whole transcript→filler path is therefore a dead end for this goal.

### Future idea

If we want to cut fillers, do it **from the audio waveform**, not the transcript — e.g. detect the
characteristic `euh`/`mmm` segments acoustically (energy + pitch/formant signature, or a small audio
classifier) and map those time ranges onto the timeline the same way `remove_silence` does. That makes
`create_transcript` unnecessary for this purpose too, which is why both are parked together.

### Revival caveats (if you bring these back)

- **`remove_fillers.py` import is doubly broken.** It does
  `sys.path.insert(0, str(Path(__file__).parent)); from remove_silence import apply_cuts, _next_id`.
  (1) Living in `maybe_later/tools/`, that points at the wrong dir; (2) as of 2026-06-22 the cut engine
  moved out of `remove_silence` into `tools/timeline.py` — `apply_cuts` no longer exists. Repoint it at
  `tools/timeline.py` and switch to the new interface: `apply_cuts(seq, cuts, mode="mute")` →
  `mute_spans(seq, all_tracks(seq), cuts)`; ids are now allocated internally, so drop `_next_id`.
- **`create_transcript.py` needs the `.venv_whisper` Python 3.11 venv** with `faster-whisper`
  (faster-whisper had no 3.14 wheels). The main project Python is 3.14.
- Both still assume the staged `OUTPUT/03_silence.xml` + `OUTPUT/audio_config.json` inputs and 25 fps.

The detailed per-tool docs are preserved alongside the code: `maybe_later/workflows/create_transcript.md`
and `maybe_later/workflows/remove_fillers.md`.

---

## Deferred architecture consolidation into `tools/timeline.py`

**Parked: 2026-06-22.** The cut/mute engine was extracted into `tools/timeline.py` (architecture review,
candidate 1). Two adjacent deepening candidates were identified at the same time and **deliberately left
inline for now** — captured here so a future review doesn't re-discover them from scratch.

**Open question to revisit:** should these fold into `timeline.py` too, making it the single home for
timeline knowledge?

- **Candidate 2 — camera-track + `pathurl` reader.** "Find the camera's video track by its `%20`-encoded
  label, then read each clipitem's `start`/`in`/`end`" is re-implemented in 4 tools
  (`remove_silence._get_cam_clips`, `switch_angles._find_video_track`,
  `detect_framing._main_clips`/`_clip_pathurl`, `sync_audios._mxf_files_from_xml`). There are also **3
  different `pathurl → Path` decoders** that have already drifted: `detect_framing._localize_pathurl`
  re-anchors on `INPUT/` (machine-portable, per `panasonic_p2_reference.md §5`); `remove_silence` and
  `sync_audios` only strip the Windows drive prefix.
- **Candidate 5 — timeline units.** `FPS = 25` (4 files + 2 inline literals) and `TICKS_PER_FRAME`
  (3 files, one now in `timeline.py`). A `units` floor (`frames_to_secs`, `secs_to_frames`, `ticks`).
- **Adjacent — Candidate 3, P2 chain.** `_resolve_p2_chain` is still imported privately from
  `remove_silence` by `detect_framing` and `benchmark_presence`; `sync_audios` and `create_xml` walk the
  same Next/Top chain independently. Its own module (`p2_chain`) is the natural next extraction.

**Review trigger:** revisit candidate 2 if the divergent `pathurl` decoders cause a "file not found" /
wrong-machine path bug, or whenever candidate 3 is picked up (they touch the same files). Candidate 5 is
low-leverage alone — fold it in as a rider when 2 or 3 lands, not on its own.

