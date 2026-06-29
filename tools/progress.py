#!/usr/bin/env python3
"""
progress.py — lightweight progress reporting for long-running stage tools.

Why this exists: stages like detect_framing / remove_silence run for minutes and
previously logged progress only to stdout. When such a stage is launched detached
(no attached terminal), that stream is invisible and the run becomes a black box
until the final output file appears. A ProgressReporter writes a small JSON
sidecar next to the stage's main output (e.g. OUTPUT/main_presence.progress.json)
that any watcher can poll — see watch_progress.py.

Design:
  - Atomic writes (tmp file + os.replace) so a reader never sees a half-written
    file, even mid-write.
  - Throttled: the hot loop may call update() every iteration; writes happen at
    most every `min_interval` seconds. start()/done()/fail() always write.
  - Pure stdlib, no third-party deps.

Usage:
    from progress import ProgressReporter
    rep = ProgressReporter(output_path, total=expected, label="detect_framing")
    for i, item in enumerate(items, 1):
        ...
        rep.update(i, message=f"clip {ci}/{nc}")
    rep.done(message=f"{i} samples")

  or as a context manager (auto fail() on exception, auto done() on clean exit):
    with ProgressReporter(output_path, total=expected, label="x") as rep:
        for i, item in enumerate(items, 1):
            rep.update(i)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def progress_path_for(output_path) -> Path:
    """Sidecar path for a stage's main output: replace the whole extension with
    '.progress.json'.  OUTPUT/main_presence.json -> OUTPUT/main_presence.progress.json
    ;  OUTPUT/03_silence.xml -> OUTPUT/03_silence.progress.json."""
    p = Path(output_path)
    return p.with_name(p.stem + ".progress.json")


class ProgressReporter:
    def __init__(self, output_path, total: int | None = None,
                 label: str = "", min_interval: float = 1.0):
        self.path = progress_path_for(output_path)
        self.total = total
        self.label = label
        self.min_interval = float(min_interval)
        self.pid = os.getpid()
        self.start_t = time.time()
        self._last_write = 0.0
        self.processed = 0
        self._phase = "starting"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write("starting", 0, "initialising")

    # -- public API ---------------------------------------------------------
    def update(self, processed: int, total: int | None = None,
               message: str | None = None) -> None:
        """Record progress. Throttled — only writes if min_interval elapsed."""
        self.processed = processed
        if total is not None:
            self.total = total
        if (time.time() - self._last_write) >= self.min_interval:
            self._write("running", processed, message)

    def note(self, message: str) -> None:
        """Force-write a status line without changing the counter (phase markers
        like 'extracting audio' / 'applying cuts')."""
        self._write("running", self.processed, message)

    def done(self, message: str | None = None) -> None:
        self._write("done", self.processed, message)

    def fail(self, message: str | None = None) -> None:
        self._write("failed", self.processed, message)

    # -- context manager ----------------------------------------------------
    def __enter__(self) -> "ProgressReporter":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            self.fail(str(exc)[:200])
        elif self._phase != "done":
            self.done()
        return False  # never suppress exceptions

    # -- internals ----------------------------------------------------------
    def _write(self, phase: str, processed: int, message: str | None) -> None:
        self._phase = phase
        now = time.time()
        elapsed = now - self.start_t
        rate = (processed / elapsed) if (elapsed > 0 and processed > 0) else 0.0
        pct = (processed / self.total) if (self.total and self.total > 0) else None
        eta = ((self.total - processed) / rate) if (
            phase == "running" and pct is not None and rate > 0) else None
        payload = {
            "label": self.label,
            "phase": phase,
            "processed": processed,
            "total": self.total,
            "pct": round(pct, 4) if pct is not None else None,
            "elapsed_s": round(elapsed, 1),
            "rate_per_s": round(rate, 2),
            "eta_s": round(eta, 1) if eta is not None else None,
            "message": message,
            "pid": self.pid,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        # Atomic: write a pid-scoped temp then replace, so concurrent readers
        # always see a complete file.
        tmp = self.path.with_name(f"{self.path.name}.{self.pid}.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, self.path)
        self._last_write = now
