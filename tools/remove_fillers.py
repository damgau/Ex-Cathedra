#!/usr/bin/env python3
"""
Stage 5 — remove_fillers.py
Uses the word-level transcript to detect and remove filler sounds and
verbal tics from the sequence.

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


def is_fixed_filler(word: str, low_confidence: bool = False) -> bool:
    """Return True if the word matches the fixed filler list."""
    w = word.strip().lower()
    if not w:
        return False
    for pattern in FILLER_RE:
        if pattern.match(w):
            return True
    # Low-confidence short words (likely filler mumbles)
    if low_confidence and len(w) <= 3 and w.isalpha():
        return True
    return False


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

    # Count unigrams and bigrams
    counts = Counter()
    for tok in tokens:
        if len(tok) > 2 and tok.isalpha():
            counts[tok] += 1
    for i in range(len(tokens) - 1):
        a, b = tokens[i], tokens[i + 1]
        if a.isalpha() and b.isalpha():
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

    print("\n  Detected verbal tics:")
    for i, (phrase, count, rate) in enumerate(tics):
        print(f"    [{i+1}] \"{phrase}\"  —  {count} occurrences ({rate:.0f}/hr)")

    print("\n  Enter numbers to REMOVE (comma-separated), or 'all' / 'none':")
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


def find_filler_cuts(words: list, tic_phrases: set,
                      handle_frames: int = 2) -> list:
    """
    Return list of (start_frame, end_frame) for all words that should be cut.
    Consecutive filler words are merged into one cut region.
    `handle_frames`: frames to keep on each side of the cut.
    """
    raw_cuts = []
    for w in words:
        low_conf = w["probability"] < 0.70
        token    = w["word"].strip().lower()

        # Check fixed fillers
        if is_fixed_filler(token, low_confidence=low_conf):
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
        description="Stage 5: Remove filler words and verbal tics from the sequence."
    )
    parser.add_argument("--input",      default=str(OUTPUT_DIR / "03_silence.xml"))
    parser.add_argument("--transcript", default=str(OUTPUT_DIR / "04_transcript.json"))
    parser.add_argument("--output",     default=str(OUTPUT_DIR / "05_fillers.xml"))
    parser.add_argument("--mode",       choices=["cut", "mute"], default="cut")
    parser.add_argument("--tic-threshold", type=float, default=5.0,
                        metavar="N/hr",
                        help="Min occurrences per hour to flag as tic (default: 5)")
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

    # ---- Fixed fillers ----
    print("\n[2/5] Detecting fixed fillers...")
    filler_words = [w for w in words if is_fixed_filler(w["word"].strip().lower(),
                                                          w["probability"] < 0.70)]
    print(f"      Found {len(filler_words)} filler words")
    if filler_words[:5]:
        for w in filler_words[:5]:
            print(f"        [{w['start_frame']:6d}]  {w['word']!r}  "
                  f"(p={w['probability']:.2f})")

    # ---- Verbal tic detection ----
    print(f"\n[3/5] Detecting verbal tics (threshold: {args.tic_threshold}/hr)...")
    tics = detect_tics(words, total_s, threshold_per_hour=args.tic_threshold)
    print(f"      {len(tics)} candidate tic(s) found")

    confirmed_tics = confirm_tics(tics)
    if confirmed_tics:
        print(f"  Confirmed for removal: {confirmed_tics}")

    # ---- Build cut list ----
    print("\n[4/5] Building cut list...")
    cuts = find_filler_cuts(words, confirmed_tics)
    total_cut_frames = sum(e - s for s, e in cuts)
    print(f"      {len(cuts)} cut regions "
          f"({total_cut_frames/FPS:.1f}s total, mode={args.mode})")

    if not cuts:
        print("  Nothing to cut — copying input to output unchanged.")
        import shutil
        shutil.copy(input_path, output_path)
        print(f"[OK] Written: {output_path}")
        return

    answer = input(f"\n  Apply {len(cuts)} cuts? [Y/n]: ").strip().lower()
    if answer in ("n", "no"):
        sys.exit("[ABORT] Cancelled by user.")

    # ---- Apply cuts ----
    print("[5/5] Applying cuts to XML...")
    tree = ET.parse(input_path)
    seq  = tree.getroot().find("sequence")
    apply_cuts(seq, cuts, mode=args.mode)

    OUTPUT_DIR.mkdir(exist_ok=True)
    tree.write(str(output_path), xml_declaration=True, encoding="UTF-8")
    print(f"[OK] Written: {output_path}")

    if args.mode == "cut":
        new_dur = int(seq.find("duration").text)
        print(f"     New duration: {new_dur/FPS:.1f}s ({new_dur/FPS/60:.1f} min)")


if __name__ == "__main__":
    main()
