# PyInstaller spec for the standalone Windows build.
#
# Tesseract OCR is a separate native binary, not a Python package — PyInstaller can't bundle it
# automatically. Either require it installed on the target machine (README's default), or vendor a
# portable Tesseract build under `vendor/tesseract/` and add it to `datas` below, then point
# MEETING_SCRIBE_TESSERACT_PATH at the bundled `tesseract.exe` at runtime.
#
# faster-whisper downloads its model weights on first use (cached under the user's home directory) —
# they are intentionally not bundled into the exe to keep the initial download small.

import sys

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hidden_imports = (
    collect_submodules("faster_whisper")
    + collect_submodules("ctranslate2")
    + (["pyaudiowpatch"] if sys.platform == "win32" else [])
)

a = Analysis(
    ["../src/meeting_scribe/main.py"],
    pathex=["../src"],
    binaries=[],
    datas=[],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="MeetingScribe",
    console=False,
    onefile=True,
)
