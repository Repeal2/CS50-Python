import sys

from meeting_scribe.config import resolve_tesseract_cmd


def test_explicit_path_wins(monkeypatch):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert resolve_tesseract_cmd("C:/custom/tesseract.exe") == "C:/custom/tesseract.exe"


def test_finds_bundled_tesseract_when_frozen(tmp_path, monkeypatch):
    bundled_dir = tmp_path / "tesseract"
    bundled_dir.mkdir()
    (bundled_dir / "tesseract.exe").write_text("stub")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert resolve_tesseract_cmd(None) == str(bundled_dir / "tesseract.exe")


def test_returns_none_when_not_frozen_and_no_explicit_path(monkeypatch):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert resolve_tesseract_cmd(None) is None


def test_returns_none_when_frozen_but_bundle_missing_tesseract(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)  # no tesseract/ subdir created
    assert resolve_tesseract_cmd(None) is None
