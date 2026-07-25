import sys
from pathlib import Path

from meeting_scribe.config import (
    DEFAULT_COPILOT_POLL_INTERVAL_SECONDS,
    DEFAULT_COPILOT_TIMEOUT_SECONDS,
    DEFAULT_NOTES_SYSTEM_PROMPT,
    load_settings,
    resolve_tesseract_cmd,
    update_audio_devices,
    update_copilot_settings,
    update_notes_system_prompt,
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


def test_load_settings_defaults_to_the_recommended_notes_system_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))

    assert load_settings().notes_system_prompt == DEFAULT_NOTES_SYSTEM_PROMPT


def test_update_notes_system_prompt_persists_and_reloads(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))

    update_notes_system_prompt(load_settings(), notes_system_prompt="Custom prompt text.")

    assert load_settings().notes_system_prompt == "Custom prompt text."


def test_update_notes_system_prompt_treats_blank_as_reset_to_default(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))

    update_notes_system_prompt(load_settings(), notes_system_prompt="Custom prompt text.")
    cleared = update_notes_system_prompt(load_settings(), notes_system_prompt="   \n  ")

    assert cleared.notes_system_prompt == DEFAULT_NOTES_SYSTEM_PROMPT
    assert load_settings().notes_system_prompt == DEFAULT_NOTES_SYSTEM_PROMPT


def test_load_settings_defaults_to_no_copilot_sync_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MEETING_SCRIBE_COPILOT_SYNC_DIR", raising=False)

    settings = load_settings()

    assert settings.copilot_sync_dir is None
    assert settings.copilot_poll_interval_seconds == DEFAULT_COPILOT_POLL_INTERVAL_SECONDS
    assert settings.copilot_timeout_seconds == DEFAULT_COPILOT_TIMEOUT_SECONDS


def test_load_settings_uses_copilot_sync_dir_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEETING_SCRIBE_COPILOT_SYNC_DIR", str(tmp_path / "OneDrive" / "Bridge"))

    settings = load_settings()

    assert settings.copilot_sync_dir == tmp_path / "OneDrive" / "Bridge"


def test_copilot_inbox_and_outbox_are_subfolders_of_the_sync_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))

    settings = update_copilot_settings(
        load_settings(),
        copilot_sync_dir=str(tmp_path / "Bridge"),
        poll_interval_seconds=5,
        timeout_seconds=120,
    )

    assert settings.copilot_inbox_dir == tmp_path / "Bridge" / "Inbox"
    assert settings.copilot_outbox_dir == tmp_path / "Bridge" / "Outbox"


def test_update_copilot_settings_persists_and_reloads(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))

    update_copilot_settings(
        load_settings(),
        copilot_sync_dir=str(tmp_path / "Bridge"),
        poll_interval_seconds=10,
        timeout_seconds=600,
    )

    reloaded = load_settings()
    assert reloaded.copilot_sync_dir == Path(tmp_path / "Bridge")
    assert reloaded.copilot_poll_interval_seconds == 10
    assert reloaded.copilot_timeout_seconds == 600


def test_update_copilot_settings_can_clear_the_sync_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))

    update_copilot_settings(
        load_settings(), copilot_sync_dir=str(tmp_path / "Bridge"), poll_interval_seconds=5, timeout_seconds=60
    )
    cleared = update_copilot_settings(
        load_settings(), copilot_sync_dir=None, poll_interval_seconds=5, timeout_seconds=60
    )

    assert cleared.copilot_sync_dir is None
    assert load_settings().copilot_sync_dir is None


def test_update_copilot_settings_does_not_clobber_audio_devices(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))

    settings = update_audio_devices(load_settings(), mic_device_name="USB Mic", system_device_name=None)
    update_copilot_settings(
        settings, copilot_sync_dir=str(tmp_path / "Bridge"), poll_interval_seconds=5, timeout_seconds=60
    )

    reloaded = load_settings()
    assert reloaded.mic_device_name == "USB Mic"
    assert reloaded.copilot_sync_dir == tmp_path / "Bridge"
