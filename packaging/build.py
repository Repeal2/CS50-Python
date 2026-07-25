#!/usr/bin/env python3
"""Runs PyInstaller against meeting_scribe.spec, producing dist/MeetingScribe.exe on Windows.

Usage: python packaging/build.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    packaging_dir = Path(__file__).resolve().parent
    spec_path = packaging_dir / "meeting_scribe.spec"
    return subprocess.call(
        [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", str(spec_path)],
        cwd=packaging_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
