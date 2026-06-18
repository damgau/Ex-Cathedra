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

- **`remove_fillers.py` import path breaks once moved.** It does
  `sys.path.insert(0, str(Path(__file__).parent)); from remove_silence import apply_cuts, _next_id`.
  Now that it lives in `maybe_later/tools/`, that import points at the wrong dir — repoint it at the real
  `tools/remove_silence.py` (or copy the two helpers) before running.
- **`create_transcript.py` needs the `.venv_whisper` Python 3.11 venv** with `faster-whisper`
  (faster-whisper had no 3.14 wheels). The main project Python is 3.14.
- Both still assume the staged `OUTPUT/03_silence.xml` + `OUTPUT/audio_config.json` inputs and 25 fps.

The detailed per-tool docs are preserved alongside the code: `maybe_later/workflows/create_transcript.md`
and `maybe_later/workflows/remove_fillers.md`.
