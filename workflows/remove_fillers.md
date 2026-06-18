# Workflow: Stage 5 — Disable Filler Words & Verbal Tics

Use the word-level transcript to find filler sounds and verbal tics and **disable** them
(split the clip + `<enabled>FALSE</enabled>`) on every track. This stage never deletes —
it only flags candidates so the editor can review and correct them in Premiere. Stage 6
(`delete_enable_clip.py`) commits the surviving disables into the final cut.

---

## Objective

Produce `OUTPUT/05_fillers.xml` — `OUTPUT/03_silence.xml` with the detected fillers/tics
split out and marked `<enabled>FALSE</enabled>` across all tracks. **Timeline length is
unchanged** (success = same `<duration>` as `03_silence.xml`). The editor then imports this
into Premiere, fixes false positives, and exports `06_reviewed.xml` for Stage 6.

## Required Inputs

- `OUTPUT/03_silence.xml` — Stage 3 output, the silence-trimmed FCP XML. (`--input`.)
- `OUTPUT/04_transcript.json` — Stage 4 word-level transcript with `start_frame`/`end_frame`
  and `probability` per word. (`--transcript`.) Frames are aligned to the `03_silence.xml`
  timeline, so disabling preserves alignment.

## Tools Used

- `tools/remove_fillers.py` — filler/tic detection + disabling. Reuses `apply_cuts(mode="mute")`
  and `_next_id` from `tools/remove_silence.py`.

## Steps

1. Confirm `OUTPUT/03_silence.xml` and `OUTPUT/04_transcript.json` exist.
2. Run `python tools/remove_fillers.py`. It auto-detects fixed fillers + stutters (both
   always safe), then prints the tic candidates for **opt-in** selection.
3. In the candidate list: **★ = crutch filler** (safe-ish to thin), **⚠ = connective**
   (`donc`, `alors`, `et donc`… — sentence glue, choppy if bulk-removed), unmarked = topic
   nouns / other. Enter numbers to disable, or `none` (recommended default — let the editor
   cut selectively in Premiere). Selecting a ⚠ connective prints a caution.
4. Confirm `Disable N regions?` → the tool writes `05_fillers.xml`.
5. Import `05_fillers.xml` into Premiere and proceed to `workflows/delete_enable_clip.md`.

### Non-interactive / scriptable

- `--tics "du coup,en fait"` skips the prompt and disables exactly those phrases.
- `--no-stutters` turns off stutter/repetition detection (on by default).
- `-y` / `--yes` skips the final confirmation.
- `--tic-threshold N` sets the min occurrences/hour to surface a candidate (default 5).
- Example (conservative default — fillers + stutters only): `python tools/remove_fillers.py --tics "" --yes`

## Expected Outputs

- `OUTPUT/05_fillers.xml` — same `<duration>` as the input; flagged words split out with
  `<enabled>FALSE</enabled>` on all tracks. No ripple, no deletion.

## Best practice — don't over-cut

A few fillers are invisible to an audience; a cut every ~30s is exhausting and obvious, and
each mid-sentence removal leaves a seam. So the default is **conservative**: auto-disable only
the always-safe stuff (fixed fillers + stutters), and leave tic removal opt-in. Real smoothness
comes from (a) **not over-cutting**, (b) selective manual trims on the disable-only timeline in
Premiere, and (c) **camera-angle switching** to mask the cuts you do make (future Stage 7+ —
see `PLAN.md`). The first time we ran this, bulk-removing `et donc`/`alors`/`vraiment` produced
161 cuts (~1 / 30s) and sounded chopped; the conservative default produces ≈3.

## Edge Cases & Known Issues

- **Disable-only — no cut mode.** This stage deliberately does not ripple/delete. Removing
  content is Stage 6's job (`delete_enable_clip.py`), driven by the editor's reviewed
  enabled/disabled flags. This keeps tic over-detection low-stakes: a rough first guess is
  fine because you fix it visually in Premiere.
- **Whisper cleans disfluencies → fillers AND stutters are ≈0.** Whisper large-v3 is a
  language model and normalises `euh`/`ben`/stutters/false-starts away *during* transcription.
  On the test conference: **0** fixed-filler hits, **2** exact duplicate words, **1** phrase
  restart in 79 min. The fixed-filler regex and `detect_stutters` are kept as cheap insurance
  for other speakers/recordings (or transcription settings that preserve disfluencies) — don't
  be alarmed when they find nothing. The disfluencies are in the *audio*, not the transcript.
- **The old low-confidence heuristic was a footgun (removed).** An earlier version flagged
  any word ≤3 chars with `probability < 0.70` as filler — that was 375 false positives
  (ordinary function words `et`, `la`, `le`, `à`…) and 0 true fillers. Whisper assigns low
  probability to plenty of correctly-transcribed short words, so confidence is not a filler
  signal. Detection is now regex (fixed fillers) + frequency-over-stopwords (tics) only.
- **Tic detection needs the stopword filter.** Without `STOPWORDS`, frequency ranking just
  surfaces the most common words in French (`des`, `les`, `de la`…) plus the speaker's topic
  nouns. The blocklist removes function words; discourse markers are kept discoverable. The
  candidate list is marked: **★ `KNOWN_TICS`** = crutch fillers (`du coup`, `en fait`, `genre`,
  `voilà`…), **⚠ `CONNECTIVES`** = sentence glue. Nothing auto-selects.
- **Connectives are not tics (the over-cutting trap).** `donc` (247×), `alors` (40×), `mais`
  (58×) are grammatical glue, not tics — frequency can't distinguish a reflexive `donc` from a
  load-bearing one. They are flagged ⚠ and selecting one prints a caution. Prefer keeping most
  and trimming only the awkward ones by hand in Premiere; bulk-removing every instance is what
  sounds choppy.
- **Frame rate hardcoded to 25 fps.** Correct for this PAL P2 footage; revisit for 24/29.97.
- **UTF-8 console.** The tool forces UTF-8 stdout so accented French (`forêts`) prints
  correctly on Windows cp1252 terminals.

## Changelog

- 2026-06-18 (b) — **Conservative defaults** to stop over-cutting. Connectives (`donc`,
  `alors`, `et donc`…) moved out of `KNOWN_TICS` into `CONNECTIVES`, flagged ⚠ with a caution
  on selection; ★ now means crutch filler only. Added default-on `detect_stutters`
  (`--no-stutters` to disable). Default run = fixed fillers + stutters only (≈3 regions here vs
  161 before).
- 2026-06-18 (a) — Reworked to **disable-only** (dropped `--mode cut`; always `apply_cuts(mode="mute")`).
  Earlier same-day fixes: removed the low-confidence filler heuristic (100% false positives),
  added the French `STOPWORDS` tic filter + `KNOWN_TICS` ★-marking, UTF-8 stdout, and the
  `--tics` / `--yes` flags. Cutting moved to the new Stage 6 `delete_enable_clip.py`.
