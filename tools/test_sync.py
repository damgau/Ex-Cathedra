#!/usr/bin/env python3
"""
Regression test for sync_audios.find_sync_offset_frames.

Pins the cross-correlation SIGN CONVENTION so the sync direction can never
silently invert again (the bug where the wrong camera was shifted).

Convention under test:
  find_sync_offset_frames(main, div) > 0  →  MAIN started before DIV
                                              →  DIV is the later camera, shift DIV
  find_sync_offset_frames(main, div) < 0  →  DIV started before MAIN
                                              →  MAIN is the later camera, shift MAIN

Run:  python tools/test_sync.py
Exits non-zero on any mismatch.
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from sync_audios import find_sync_offset_frames, SAMPLE_RATE


def _make_pair(earlier: str, delay_samples: int):
    """
    Build (main, div) sharing the same noise event, where `earlier` camera
    started first by `delay_samples`. The camera that started earlier has the
    event sitting at a LARGER offset inside its own recording.
    """
    n = 80_000
    rng = np.random.default_rng(0)
    event = rng.standard_normal(5_000)

    main = np.zeros(n, dtype=np.float32)
    div  = np.zeros(n, dtype=np.float32)
    base = 1_000

    if earlier == "main":
        main[base + delay_samples: base + delay_samples + 5_000] = event
        div[base: base + 5_000] = event
    else:
        div[base + delay_samples: base + delay_samples + 5_000] = event
        main[base: base + 5_000] = event
    return main, div


def _decide_shift(main_lead_frames: int) -> str:
    """Mirror of main(): shift the later-starting camera."""
    return "DIV CAM" if main_lead_frames >= 0 else "MAIN CAM"


def main():
    delay_samples = 200
    expected_frames = round(delay_samples * 25 / SAMPLE_RATE)  # 1 frame at 8 kHz

    cases = [
        # (camera that started earlier, expected sign of return, camera to shift)
        ("main", +1, "DIV CAM"),
        ("div",  -1, "MAIN CAM"),
    ]

    failures = 0
    for earlier, expected_sign, expected_shift in cases:
        m, d = _make_pair(earlier, delay_samples)
        lead = find_sync_offset_frames(m, d)
        shift = _decide_shift(lead)

        ok_mag  = abs(lead) == expected_frames
        ok_sign = (lead > 0) == (expected_sign > 0)
        ok_cam  = shift == expected_shift
        status  = "PASS" if (ok_mag and ok_sign and ok_cam) else "FAIL"
        if status == "FAIL":
            failures += 1

        print(f"[{status}] {earlier.upper()} started earlier by {delay_samples} samples "
              f"(~{expected_frames} frame): lead={lead:+d} frames, shift={shift} "
              f"(expected lead sign {'+' if expected_sign > 0 else '-'}, shift {expected_shift})")

    if failures:
        print(f"\n{failures} test(s) FAILED — sync direction is wrong.")
        sys.exit(1)
    print("\nAll sync-direction tests passed.")


if __name__ == "__main__":
    main()
