# PyInstaller spec for the standalone Windows build.
#
# Tesseract OCR is a separate native binary, not a Python package, so PyInstaller can't discover and
# bundle it the way it does for pure-Python dependencies. Run `python packaging/vendor_tesseract.py`
# once (it copies your local Tesseract install into vendor/tesseract/) before running this spec, and
# the block below folds it into the .exe automatically — `tesseract.exe`, its DLLs, and `tessdata/`
# all land under a `tesseract/` folder inside the onefile bundle. At runtime,
# meeting_scribe.config._bundled_tesseract_path() finds it via `sys._MEIPASS` (the temp directory
# PyInstaller's onefile mode extracts data files into) with no extra configuration needed. If you skip
# vendor_tesseract.py, the app still works as long as Tesseract is installed and on PATH on the target
# machine — see the README's "Prerequisites" note.
#
# faster-whisper downloads its model weights on first use (cached under the user's home directory) —
# they are intentionally not bundled into the exe to keep the initial download small.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hidden_imports = (
    collect_submodules("faster_whisper")
    + collect_submodules("ctranslate2")
    + (["pyaudiowpatch", "win32gui", "win32con"] if sys.platform == "win32" else [])
)

vendored_tesseract = Path(SPECPATH) / "vendor" / "tesseract"
datas = [(str(vendored_tesseract), "tesseract")] if vendored_tesseract.exists() else []

a = Analysis(
    ["../src/meeting_scribe/main.py"],
    pathex=["../src"],
    binaries=[],
    datas=datas,
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
