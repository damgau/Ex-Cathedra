#!/usr/bin/env python3
"""
Stage 4 — create_transcript.py
Extracts clean audio from the silence-trimmed sequence and runs local
Whisper large-v3 to produce a word-level transcript with timeline-frame
timestamps.

Must be run with .venv_whisper/bin/python (Python 3.11) because
faster-whisper requires packages that don't have Python 3.14 wheels yet:
    .venv_whisper/bin/python tools/create_transcript.py

Output: OUTPUT/04_transcript.json
"""

import sys
import json
import argparse
import subprocess
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

import xml.etree.ElementTree as ET

BASE_DIR    = Path(__file__).parent.parent
OUTPUT_DIR  = BASE_DIR / "OUTPUT"
FPS         = 25

# ---------------------------------------------------------------------------
# Audio reconstruction from post-silence XML
# ---------------------------------------------------------------------------

def _get_cam_clips_for_audio(seq: ET.Element, cam_label: str) -> list:
    """
    Return ordered list of:
      (timeline_start_frame, mxf_path, source_in_frame, duration_frames)
    for all clips of the given camera's video track.
    Only includes enabled clips (not muted by remove_silence in mute mode).
    """
    vid = seq.find("media/video")
    for track in vid.findall("track"):
        items = track.findall("clipitem")
        if not items:
            continue
        f = items[0].find("file/pathurl")
        if f is None or f.text is None:
            continue
        if cam_label.replace(" ", "%20") not in f.text:
            continue
        result = []
        for item in items:
            if item.find("enabled") is not None and item.find("enabled").text == "FALSE":
                continue
            furl = item.find("file/pathurl")
            if furl is not None and furl.text:
                p    = Path(furl.text.replace("file://localhost", "")
                                     .replace("%20", " "))
                t_s  = int(item.find("start").text)
                s_in = int(item.find("in").text)
                dur  = int(item.find("end").text) - t_s
                result.append((t_s, p, s_in, dur))
        return result
    return []


def build_audio_for_whisper(clips: list, channel: int,
                              tmp_dir: Path) -> tuple:
    """
    Extract each clip's audio and concat into one WAV file for Whisper.
    Returns (wav_path, segment_map) where segment_map is:
      [(audio_start_s, audio_end_s, timeline_start_frame, timeline_end_frame), ...]
    """
    segment_files = []
    segment_map   = []
    audio_cursor  = 0.0

    for i, (t_start, mxf, src_in, dur) in enumerate(clips):
        start_secs = src_in / FPS
        dur_secs   = dur    / FPS
        out_f      = tmp_dir / f"seg_{i:04d}.wav"

        cmd = [
            "ffmpeg", "-loglevel", "error",
            "-ss", str(start_secs), "-i", str(mxf),
            "-t", str(dur_secs),
            "-map", f"0:a:{channel - 1}",
            "-ar", "16000",          # Whisper expects 16 kHz
            "-ac", "1",
            str(out_f),
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0 or not out_f.exists():
            print(f"  [WARN] ffmpeg failed for clip {i}: "
                  f"{result.stderr[:80].decode()}")
            continue

        segment_files.append(out_f)
        t_end_frame = t_start + dur
        segment_map.append((
            audio_cursor,
            audio_cursor + dur_secs,
            t_start,
            t_end_frame,
        ))
        audio_cursor += dur_secs

    # Concatenate all segments into one WAV
    concat_wav = tmp_dir / "full_audio.wav"
    if len(segment_files) == 1:
        segment_files[0].rename(concat_wav)
    else:
        list_file = tmp_dir / "concat_list.txt"
        list_file.write_text(
            "\n".join(f"file '{f}'" for f in segment_files), encoding="utf-8"
        )
        subprocess.run([
            "ffmpeg", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c", "copy", str(concat_wav),
        ], check=True)

    return concat_wav, segment_map


def audio_time_to_frames(audio_time_s: float, segment_map: list) -> int:
    """
    Convert a time (seconds into the concatenated audio) to a timeline frame.
    """
    for (a_start, a_end, t_start, t_end) in segment_map:
        if a_start <= audio_time_s <= a_end:
            offset_s = audio_time_s - a_start
            return t_start + round(offset_s * FPS)
    # Clamp to last segment if past end
    if segment_map:
        _, _, _, t_end = segment_map[-1]
        return t_end
    return 0


# ---------------------------------------------------------------------------
# Whisper transcription
# ---------------------------------------------------------------------------

def transcribe(wav_path: Path, model_size: str = "large-v3") -> list:
    """
    Run faster-whisper on `wav_path` and return word-level results.
    Returns list of dicts: {word, start, end, probability}
    where start/end are seconds into the wav file.
    """
    from faster_whisper import WhisperModel

    print(f"  Loading Whisper {model_size}... (first run downloads model ~3 GB)")
    model = WhisperModel(model_size, device="auto", compute_type="int8")

    print(f"  Transcribing {wav_path.name} ...")
    segments, info = model.transcribe(
        str(wav_path),
        language="fr",
        word_timestamps=True,
        vad_filter=True,
    )
    print(f"  Detected language: {info.language} "
          f"(probability: {info.language_probability:.2f})")

    words = []
    for seg in segments:
        if seg.words is None:
            continue
        for w in seg.words:
            words.append({
                "word":        w.word.strip(),
                "start":       round(w.start, 3),
                "end":         round(w.end,   3),
                "probability": round(w.probability, 3),
            })
    return words


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Stage 4: Generate word-level transcript from silence-trimmed audio.\n"
            "NOTE: Run with .venv_whisper/bin/python (Python 3.11 required)."
        )
    )
    parser.add_argument("--input",        default=str(OUTPUT_DIR / "03_silence.xml"))
    parser.add_argument("--audio-config", default=str(OUTPUT_DIR / "audio_config.json"))
    parser.add_argument("--output",       default=str(OUTPUT_DIR / "04_transcript.json"))
    parser.add_argument("--model",        default="large-v3",
                        help="Whisper model size (default: large-v3)")
    args = parser.parse_args()

    input_path  = Path(args.input)
    config_path = Path(args.audio_config)
    output_path = Path(args.output)

    if not input_path.exists():
        sys.exit(f"[ERROR] Input not found: {input_path}")
    if not config_path.exists():
        sys.exit(f"[ERROR] audio_config.json not found — run sync_audios.py first")

    try:
        import faster_whisper  # noqa
    except ImportError:
        sys.exit(
            "[ERROR] faster-whisper not installed.\n"
            "Run:  python3.11 -m venv .venv_whisper && "
            ".venv_whisper/bin/pip install faster-whisper\n"
            "Then: .venv_whisper/bin/python tools/create_transcript.py"
        )

    cfg     = json.loads(config_path.read_text())
    cam     = cfg["camera"]
    channel = cfg["channel"]
    print(f"[1/4] Clean audio: {cam} CH{channel}")

    tree = ET.parse(input_path)
    seq  = tree.getroot().find("sequence")

    print("[2/4] Extracting audio segments from XML...")
    clips = _get_cam_clips_for_audio(seq, cam)
    if not clips:
        sys.exit(f"[ERROR] No clips found for {cam} in {input_path}")
    total_dur = sum(d for _, _, _, d in clips) / FPS
    print(f"      {len(clips)} segments — {total_dur:.1f}s total audio")

    OUTPUT_DIR.mkdir(exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="excat_whisper_") as tmp:
        tmp_dir = Path(tmp)

        print("[3/4] Assembling audio for Whisper...")
        wav_path, segment_map = build_audio_for_whisper(clips, channel, tmp_dir)
        print(f"      WAV: {wav_path.stat().st_size / 1e6:.1f} MB")

        print(f"\n[4/4] Transcribing with Whisper {args.model} (French)...")
        raw_words = transcribe(wav_path, model_size=args.model)

    print(f"      {len(raw_words)} words transcribed")

    # Map audio timestamps → timeline frames
    words_with_frames = []
    for w in raw_words:
        words_with_frames.append({
            "word":         w["word"],
            "start_frame":  audio_time_to_frames(w["start"], segment_map),
            "end_frame":    audio_time_to_frames(w["end"],   segment_map),
            "start_s":      w["start"],
            "end_s":        w["end"],
            "probability":  w["probability"],
        })

    result = {
        "model":    args.model,
        "language": "fr",
        "words":    words_with_frames,
    }

    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    print(f"[OK] Written: {output_path}")

    # Print first few words as sanity check
    print("\nFirst 10 words:")
    for w in words_with_frames[:10]:
        print(f"  [{w['start_frame']:6d}–{w['end_frame']:6d}]  {w['word']!r}  "
              f"({w['probability']:.2f})")


if __name__ == "__main__":
    main()
