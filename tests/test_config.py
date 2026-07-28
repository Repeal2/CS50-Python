import sys
from pathlib import Path

from meeting_scribe.config import (
    load_settings,
    resolve_tesseract_cmd,
    update_audio_devices,
    update_copilot_settings,
)


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


def test_load_settings_defaults_to_no_device_override(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))

    settings = load_settings()

    assert settings.mic_device_name is None
    assert settings.system_device_name is None


def test_update_audio_devices_persists_and_reloads(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))

    update_audio_devices(
        load_settings(), mic_device_name="USB Mic", system_device_name="Speakers (Realtek)"
    )

    reloaded = load_settings()
    assert reloaded.mic_device_name == "USB Mic"
    assert reloaded.system_device_name == "Speakers (Realtek)"


def test_update_audio_devices_can_reset_to_system_default(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))

    update_audio_devices(load_settings(), mic_device_name="USB Mic", system_device_name=None)
    cleared = update_audio_devices(load_settings(), mic_device_name=None, system_device_name=None)

    assert cleared.mic_device_name is None
    assert cleared.system_device_name is None


def test_load_settings_defaults_to_no_copilot_sync_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MEETING_SCRIBE_COPILOT_SYNC_DIR", raising=False)

    assert load_settings().copilot_sync_dir is None


def test_load_settings_uses_copilot_sync_dir_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEETING_SCRIBE_COPILOT_SYNC_DIR", str(tmp_path / "OneDrive" / "Bridge"))

    settings = load_settings()

    assert settings.copilot_sync_dir == tmp_path / "OneDrive" / "Bridge"


def test_copilot_inbox_dir_is_a_subfolder_of_the_sync_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))

    settings = update_copilot_settings(load_settings(), copilot_sync_dir=str(tmp_path / "Bridge"))

    assert settings.copilot_inbox_dir == tmp_path / "Bridge" / "Inbox"


def test_update_copilot_settings_persists_and_reloads(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))

    update_copilot_settings(load_settings(), copilot_sync_dir=str(tmp_path / "Bridge"))

    reloaded = load_settings()
    assert reloaded.copilot_sync_dir == Path(tmp_path / "Bridge")


def test_update_copilot_settings_can_clear_the_sync_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))

    update_copilot_settings(load_settings(), copilot_sync_dir=str(tmp_path / "Bridge"))
    cleared = update_copilot_settings(load_settings(), copilot_sync_dir=None)

    assert cleared.copilot_sync_dir is None
    assert load_settings().copilot_sync_dir is None


def test_update_copilot_settings_does_not_clobber_audio_devices(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))

    settings = update_audio_devices(load_settings(), mic_device_name="USB Mic", system_device_name=None)
    update_copilot_settings(settings, copilot_sync_dir=str(tmp_path / "Bridge"))

    reloaded = load_settings()
    assert reloaded.mic_device_name == "USB Mic"
    assert reloaded.copilot_sync_dir == tmp_path / "Bridge"
