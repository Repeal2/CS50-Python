#!/usr/bin/env python3
"""Copies a local Tesseract OCR install into packaging/vendor/tesseract/ so meeting_scribe.spec bundles
it straight into the .exe — end users then don't need to install Tesseract themselves.

This copies a Tesseract install you already have rather than downloading one, so there's no build-time
dependency on a third-party download URL. Get Tesseract itself (once, on the machine you build on) from
https://github.com/UB-Mannheim/tesseract/wiki, then run this script before packaging.

Usage:
    python packaging/vendor_tesseract.py [--source "C:\\Program Files\\Tesseract-OCR"]

If --source is omitted, this looks for `tesseract` on PATH and vendors its install directory.
packaging/build.py bundles whatever it finds in packaging/vendor/tesseract/ and does nothing special
if that folder is absent, so re-running this script is the only thing needed to opt into bundling.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

VENDOR_DIR = Path(__file__).resolve().parent / "vendor" / "tesseract"


def find_default_source() -> Path | None:
    which = shutil.which("tesseract")
    if which:
        return Path(which).resolve().parent
    default = Path("C:/Program Files/Tesseract-OCR")
    return default if default.exists() else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--source", type=Path, help="Path to an existing Tesseract-OCR install directory")
    args = parser.parse_args()

    source = args.source or find_default_source()
    if source is None or not (source / "tesseract.exe").exists():
        print(
            "Couldn't find a Tesseract install to vendor (looked on PATH and in the default "
            "'C:/Program Files/Tesseract-OCR'). Install it from "
            "https://github.com/UB-Mannheim/tesseract/wiki, then re-run this script "
            "(pass --source if it's somewhere non-default).",
            file=sys.stderr,
        )
        return 1

    if VENDOR_DIR.exists():
        shutil.rmtree(VENDOR_DIR)
    shutil.copytree(source, VENDOR_DIR)
    print(f"Vendored Tesseract from {source} into {VENDOR_DIR}")
    print("Next: python packaging/build.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
