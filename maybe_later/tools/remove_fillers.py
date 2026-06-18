#!/usr/bin/env python3
"""
Stage 5 — remove_fillers.py
Uses the word-level transcript to detect filler sounds and verbal tics and
DISABLE them (split the clip + <enabled>FALSE</enabled>) on every track.

This stage never ripples or deletes — it only flags candidates so the editor
can review and correct them in Premiere. Stage 6 (delete_enable_clip.py) commits
the surviving disables into the final cut. Timeline length is unchanged here.

Output: OUTPUT/05_fillers.xml
"""

import sys
import re
import json
import argparse
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

# Windows consoles default to cp1252 and mangle accented French (forêts -> for?ts).
# Force UTF-8 so detection previews and prompts are readable.
if getattr(sys.stdout, "encoding", "").lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import xml.etree.ElementTree as ET

# Reuse XML cut logic from remove_silence
sys.path.insert(0, str(Path(__file__).parent))
from remove_silence import apply_cuts, _next_id   # noqa

BASE_DIR   = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "OUTPUT"
FPS        = 25

# Fixed filler word patterns (French, case-insensitive)
FIXED_FILLERS = {
    r"^a+h+$",          # ah, aah, aaah, aaaah
    r"^e+u+h+$",        # euh, euuh, euuuh
    r"^h+m+$",          # hm, hmm
    r"^b+a+h+$",        # bah, baah
    r"^b+e+n+$",        # ben, benn, beeen
    r"^w+a+h+$",        # wah
    r"^m+m+$",          # mm, mmm
    r"^p+f+$",          # pff, pfff
}

FILLER_RE = [re.compile(p, re.IGNORECASE) for p in FIXED_FILLERS]

# French function words that must NEVER be flagged as tics. Without this filter,
# frequency-based detection just surfaces the most common words in the language
# (articles, prepositions, pronouns, auxiliaries) and the speaker's topic nouns.
# NOTE: genuine discourse markers (donc, alors, voilà, bref, quoi, genre...) are
# deliberately *kept out* of this list so they can still be discovered.
STOPWORDS = {
    # articles / determiners
    "le", "la", "les", "l", "un", "une", "des", "du", "de", "d", "au", "aux",
    "ce", "cet", "cette", "ces", "mon", "ma", "mes", "ton", "ta", "tes",
    "son", "sa", "ses", "notre", "nos", "votre", "vos", "leur", "leurs",
    # pronouns
    "je", "j", "tu", "il", "elle", "on", "nous", "vous", "ils", "elles",
    "me", "m", "te", "t", "se", "s", "lui", "y", "en", "moi", "toi", "soi",
    "ça", "cela", "ceci", "celui", "celle", "ceux", "celles",
    "qui", "que", "qu", "dont", "où",
    # prepositions
    "à", "dans", "par", "pour", "sur", "sous", "avec", "sans", "vers", "chez",
    "entre", "contre", "depuis", "pendant", "avant", "après", "selon",
    "parmi", "jusqu", "jusque",
    # conjunctions (NB: 'donc' intentionally excluded — it's a common tic)
    "et", "ou", "mais", "ni", "or", "car", "comme", "si", "quand", "lorsque",
    # être / avoir / aller / faire
    "est", "sont", "suis", "es", "sommes", "êtes", "était", "étaient", "être",
    "ai", "as", "a", "avons", "avez", "ont", "avait", "avaient", "avoir",
    "vais", "vas", "va", "allons", "allez", "vont", "allait",
    "fait", "fais", "faire", "font", "faisait",
    # negation / quantity adverbs
    "ne", "n", "pas", "plus", "moins", "très", "trop", "peu", "bien", "aussi",
    "tout", "tous", "toute", "toutes", "chaque", "même", "autre", "autres",
    "oui", "non", "ici",
}

# Genuine "crutch" filler phrases — usually safe-ish to thin out. Used only to
# *flag* candidates in the confirmation prompt (★); never auto-removed.
KNOWN_TICS = {
    "voilà", "voila", "bref", "genre", "quoi", "hein", "disons", "ouais",
    "vraiment", "effectivement", "du coup", "en fait", "en gros",
    "tu vois", "tu sais", "je veux dire",
}

# Grammatical connectives. These are NOT tics — they are sentence glue, and
# bulk-removing every instance is exactly what makes an edit sound choppy. They
# are flagged with ⚠ (not ★) and trigger a caution when selected.
CONNECTIVE_WORDS = {"donc", "alors", "mais", "puis", "ensuite", "enfin", "et"}
CONNECTIVES = {
    "donc", "alors", "mais", "puis", "et puis", "ensuite", "enfin", "et donc",
}


def is_known_tic(phrase: str) -> bool:
    """True if the phrase is (or contains) a recognised French crutch filler."""
    p = phrase.strip().lower()
    if p in KNOWN_TICS:
        return True
    return any(marker in p.split() for marker in KNOWN_TICS if " " not in marker)


def is_connective(phrase: str) -> bool:
    """True if the phrase is (or contains) a grammatical connective.

    Catches bare connectives ('donc', 'alors'), known connective phrases
    ('et donc', 'et puis'), and any bigram built around one ('donc les', 'donc la').
    """
    p = phrase.strip().lower()
    if p in CONNECTIVES:
        return True
    return any(w in CONNECTIVE_WORDS for w in p.split())


def is_fixed_filler(word: str) -> bool:
    """Return True if the word matches the fixed filler list (ah/euh/hmm/ben...).

    Note: an earlier version also flagged any short low-confidence word, but on
    real transcripts that was 100% false positives — Whisper assigns low
    probability to ordinary function words (et, la, le, à...). Removed.
    """
    w = word.strip().lower()
    if not w:
        return False
    return any(pattern.match(w) for pattern in FILLER_RE)


def detect_tics(words: list, total_duration_s: float,
                threshold_per_hour: float) -> list:
    """
    Detect verbal tics: unigrams and bigrams that appear more than
    threshold_per_hour times per hour of content.
    Returns list of (phrase, count, rate_per_hour) sorted by rate desc.
    """
    # Normalise words to lowercase stripped tokens
    tokens = [w["word"].strip().lower() for w in words if w["word"].strip()]

    hours = max(total_duration_s / 3600, 1 / 3600)

    # Count unigrams and bigrams, skipping pure function words so that only
    # content words and genuine discourse markers can surface as candidates.
    counts = Counter()
    for tok in tokens:
        if len(tok) > 2 and tok.isalpha() and tok not in STOPWORDS:
            counts[tok] += 1
    for i in range(len(tokens) - 1):
        a, b = tokens[i], tokens[i + 1]
        # Drop bigrams made entirely of stopwords ("de la", "et de", "à l"...).
        if a.isalpha() and b.isalpha() and not (a in STOPWORDS and b in STOPWORDS):
            counts[f"{a} {b}"] += 1

    # Filter to those above threshold
    tics = []
    for phrase, count in counts.items():
        rate = count / hours
        if rate >= threshold_per_hour:
            tics.append((phrase, count, rate))

    return sorted(tics, key=lambda x: -x[2])


def confirm_tics(tics: list) -> set:
    """
    Present detected tics to the user and return the confirmed set of phrases.
    """
    if not tics:
        return set()

    print("\n  Detected candidates  (★ = crutch filler, ⚠ = connective — choppy if bulk-removed):")
    for i, (phrase, count, rate) in enumerate(tics):
        if is_connective(phrase):
            mark, note = "⚠", "  (connective)"
        elif is_known_tic(phrase):
            mark, note = "★", ""
        else:
            mark, note = " ", ""
        print(f"    {mark} [{i+1}] \"{phrase}\"  —  {count} occurrences ({rate:.0f}/hr){note}")

    print("\n  Tip: connectives (⚠) are sentence glue — removing every instance sounds choppy.")
    print("  Enter numbers to REMOVE (comma-separated), or 'all' / 'none':")
    answer = input("  > ").strip().lower()

    if answer == "all":
        return {phrase for phrase, _, _ in tics}
    if answer in ("none", ""):
        return set()

    confirmed = set()
    for part in answer.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(tics):
                confirmed.add(tics[idx][0])
    return confirmed


def _norm(word: str) -> str:
    return word.strip().lower().strip(".,!?;:")


def detect_stutters(words: list, max_gap_frames: int = 15) -> list:
    """
    Detect disfluencies that survived transcription and return their frame
    regions (the redundant copy to remove):
      - exact consecutive duplicate words ("le le bois" → drop one "le")
      - back-to-back phrase restarts ("(a b)(a b)" → drop the first "a b")

    NB: Whisper large-v3 cleans most disfluencies during transcription, so this
    typically finds ≈0 on its output. Kept as cheap insurance for other speakers,
    recordings, or transcription settings that preserve stutters.
    """
    regions = []
    n = len(words)
    i = 0
    while i < n - 1:
        a = _norm(words[i]["word"])
        b = _norm(words[i + 1]["word"])

        # Back-to-back phrase restart: (a b)(a b)
        if (i < n - 3 and a and b and a.isalpha() and b.isalpha()
                and a == _norm(words[i + 2]["word"])
                and b == _norm(words[i + 3]["word"])):
            regions.append((words[i]["start_frame"], words[i + 1]["end_frame"]))
            i += 2
            continue

        # Exact consecutive duplicate
        gap = words[i + 1]["start_frame"] - words[i]["end_frame"]
        if a and a == b and a.isalpha() and gap <= max_gap_frames:
            regions.append((words[i]["start_frame"], words[i]["end_frame"]))

        i += 1
    return regions


def find_filler_cuts(words: list, tic_phrases: set,
                     extra_regions: list = None,
                     handle_frames: int = 2) -> list:
    """
    Return list of (start_frame, end_frame) for all words that should be cut.
    Consecutive filler words are merged into one cut region.
    `handle_frames`: frames to keep on each side of the cut.
    """
    raw_cuts = list(extra_regions) if extra_regions else []
    for w in words:
        token = w["word"].strip().lower()

        # Check fixed fillers
        if is_fixed_filler(token):
            raw_cuts.append((w["start_frame"], w["end_frame"]))
            continue

        # Check verbal tics (unigram lookup)
        if token in tic_phrases:
            raw_cuts.append((w["start_frame"], w["end_frame"]))

    # Check bigrams (consecutive word pairs)
    for i in range(len(words) - 1):
        a = words[i]["word"].strip().lower()
        b = words[i + 1]["word"].strip().lower()
        bigram = f"{a} {b}"
        if bigram in tic_phrases:
            raw_cuts.append((
                words[i]["start_frame"],
                words[i + 1]["end_frame"],
            ))

    if not raw_cuts:
        return []

    # Merge overlapping / adjacent cuts, add handles
    merged = sorted(set(raw_cuts))
    result = []
    cur_s, cur_e = merged[0]
    for s, e in merged[1:]:
        if s <= cur_e + handle_frames * 3:   # merge if very close
            cur_e = max(cur_e, e)
        else:
            s_with_handle = max(0, cur_s - handle_frames)
            e_with_handle = cur_e + handle_frames
            if e_with_handle > s_with_handle:
                result.append((s_with_handle, e_with_handle))
            cur_s, cur_e = s, e
    s_with_handle = max(0, cur_s - handle_frames)
    e_with_handle = cur_e + handle_frames
    if e_with_handle > s_with_handle:
        result.append((s_with_handle, e_with_handle))

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Stage 5: Disable filler words, stutters, and (opt-in) verbal tics "
                    "across all tracks. Disable-only — Stage 6 commits the cut."
    )
    parser.add_argument("--input",      default=str(OUTPUT_DIR / "03_silence.xml"))
    parser.add_argument("--transcript", default=str(OUTPUT_DIR / "04_transcript.json"))
    parser.add_argument("--output",     default=str(OUTPUT_DIR / "05_fillers.xml"))
    parser.add_argument("--tic-threshold", type=float, default=5.0,
                        metavar="N/hr",
                        help="Min occurrences per hour to flag as tic (default: 5)")
    parser.add_argument("--tics", default=None, metavar="PHRASES",
                        help="Comma-separated tic phrases to disable non-interactively "
                             "(e.g. \"et donc,alors,vraiment\"). Skips the interactive "
                             "tic-selection prompt.")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Skip the final 'Disable N regions?' confirmation.")
    parser.add_argument("--no-stutters", action="store_true",
                        help="Disable stutter/repetition detection (on by default).")
    args = parser.parse_args()

    input_path      = Path(args.input)
    transcript_path = Path(args.transcript)
    output_path     = Path(args.output)

    if not input_path.exists():
        sys.exit(f"[ERROR] Input not found: {input_path}")
    if not transcript_path.exists():
        sys.exit(f"[ERROR] Transcript not found — run create_transcript.py first")

    transcript = json.loads(transcript_path.read_text())
    words      = transcript["words"]
    print(f"[1/5] Loaded transcript: {len(words)} words")

    # Estimate total duration from last word's end frame
    if not words:
        sys.exit("[ERROR] Transcript is empty.")
    total_frames = words[-1]["end_frame"]
    total_s      = total_frames / FPS
    print(f"      Duration: {total_s:.1f}s ({total_s/60:.1f} min)")

    # ---- Fixed fillers + stutters (safe, on by default) ----
    print("\n[2/5] Detecting fixed fillers + stutters...")
    filler_words = [w for w in words if is_fixed_filler(w["word"].strip().lower())]
    print(f"      Found {len(filler_words)} fixed-filler word(s)")
    if filler_words[:5]:
        for w in filler_words[:5]:
            print(f"        [{w['start_frame']:6d}]  {w['word']!r}  "
                  f"(p={w['probability']:.2f})")
    stutter_regions = [] if args.no_stutters else detect_stutters(words)
    print(f"      Found {len(stutter_regions)} stutter/repetition region(s)"
          f"{' (detection off)' if args.no_stutters else ''}")

    # ---- Verbal tic detection (opt-in only) ----
    print(f"\n[3/5] Detecting verbal tic candidates (threshold: {args.tic_threshold}/hr)...")
    tics = detect_tics(words, total_s, threshold_per_hour=args.tic_threshold)
    print(f"      {len(tics)} candidate(s) found")

    if args.tics is not None:
        confirmed_tics = {p.strip().lower() for p in args.tics.split(",") if p.strip()}
        print(f"  Non-interactive selection: {confirmed_tics}")
    else:
        confirmed_tics = confirm_tics(tics)
    if confirmed_tics:
        print(f"  Confirmed for removal: {confirmed_tics}")
        selected_connectives = {t for t in confirmed_tics if is_connective(t)}
        if selected_connectives:
            print(f"  ⚠ CAUTION: {sorted(selected_connectives)} are connectives. "
                  f"Removing every instance can sound choppy — consider keeping most "
                  f"and trimming only awkward ones in Premiere.")

    # ---- Build region list ----
    print("\n[4/5] Building region list...")
    regions = find_filler_cuts(words, confirmed_tics, extra_regions=stutter_regions)
    total_region_frames = sum(e - s for s, e in regions)
    print(f"      {len(regions)} regions "
          f"({total_region_frames/FPS:.1f}s total)")

    if not regions:
        print("  Nothing to disable — copying input to output unchanged.")
        import shutil
        shutil.copy(input_path, output_path)
        print(f"[OK] Written: {output_path}")
        return

    answer = "y" if args.yes else input(f"\n  Disable {len(regions)} regions? [Y/n]: ").strip().lower()
    if answer in ("n", "no"):
        sys.exit("[ABORT] Cancelled by user.")

    # ---- Disable regions (mute = split + <enabled>FALSE</enabled>) ----
    # We never ripple/delete here: this stage only flags candidates as disabled so
    # the editor can review and correct them in Premiere. Stage 6 (delete_enable_clip.py)
    # commits the surviving disables into the final cut. Timeline length is unchanged.
    print("[5/5] Disabling regions in XML...")
    tree = ET.parse(input_path)
    seq  = tree.getroot().find("sequence")
    apply_cuts(seq, regions, mode="mute")

    OUTPUT_DIR.mkdir(exist_ok=True)
    tree.write(str(output_path), xml_declaration=True, encoding="UTF-8")
    print(f"[OK] Written: {output_path}")
    print("     Timeline length unchanged (disable-only). Review in Premiere, then run "
          "delete_enable_clip.py.")


if __name__ == "__main__":
    main()
