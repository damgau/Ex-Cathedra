---
name: macos-to-windows-portability
description: This project was authored on a Mac (LaCie drive) and now runs on Windows G:; venvs and XML paths break across the move
metadata:
  type: project
---

The EX Cathedra pipeline was originally developed/run on macOS (external drive `/Volumes/LaCie/MATELE/EX Cathedra/00_AI_project`) and later moved to Windows (`G:\MATELE\EX Cathedra\00_AI_project`). Two classes of breakage result:

1. **Virtualenvs don't survive the move.** `.venv_whisper` was committed as a macOS venv (Cygwin `XSym` symlink stubs for `bin/python`, Mach-O binaries, mac-native wheels). It must be rebuilt natively on Windows. Fixed on 2026-06-18: rebuilt with Python 3.11.9 (installed via `winget install Python.Python.3.11`) at `.venv_whisper\Scripts\python.exe`, `pip install faster-whisper python-dotenv`. Also untracked `.venv_whisper/` from git and added it to `.gitignore`.

2. **FCP/Premiere XML pathurls are absolute macOS paths.** `OUTPUT/*.xml` (01_create, 02_sync, 03_silence) embed `file://localhost/Volumes/LaCie/.../INPUT/...`. The decode logic in tools that only strips a leading slash for `/G:/...` paths cannot resolve these. Fix: re-anchor on the first `INPUT` path component and join to `BASE_DIR` (all media lives under `BASE_DIR/INPUT`). Applied in `tools/create_transcript.py` (`_localize_pathurl`). The same gotcha applies to any other tool that re-reads these XMLs on Windows, and the path-decode snippet in `panasonic_p2_reference.md` §5 only covers the Windows-drive case.

**Why:** explains otherwise-baffling "command not found" venv errors and ffmpeg "No such file or directory" on files that visibly exist on disk.
**How to apply:** when a tool reads an FCP XML pathurl, localize it via the `INPUT`-anchor approach; never trust the absolute path in the XML. Rebuild any venv on the local OS rather than relying on a committed one.
