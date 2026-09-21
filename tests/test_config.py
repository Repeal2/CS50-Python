import sys
from pathlib import Path

from meeting_scribe.config import (
    load_settings,
    resolve_tesseract_cmd,
    update_audio_devices,
    update_copilot_settings,
    update_diarize_system_audio,
    update_meeting_hotkeys,
    update_whisper_model_size,
)
from meeting_scribe.hotkeys import MOD_ALT, MOD_CONTROL, MOD_SHIFT, HotkeyCombo


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


def test_documents_dir_is_a_subfolder_of_the_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))

    assert load_settings().documents_dir == tmp_path / "documents"


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


def test_load_settings_defaults_the_whisper_model_to_small(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MEETING_SCRIBE_WHISPER_MODEL", raising=False)

    assert load_settings().whisper_model_size == "small"


def test_load_settings_uses_the_whisper_model_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEETING_SCRIBE_WHISPER_MODEL", "medium")

    assert load_settings().whisper_model_size == "medium"


def test_update_whisper_model_size_persists_and_reloads(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))

    update_whisper_model_size(load_settings(), whisper_model_size="large-v3")

    assert load_settings().whisper_model_size == "large-v3"


def test_update_whisper_model_size_persisted_choice_wins_over_the_env_var(tmp_path, monkeypatch):
    # Once saved from the Settings tab, the persisted choice is the source of truth — otherwise a leftover
    # MEETING_SCRIBE_WHISPER_MODEL from an old launch shortcut would keep overriding a change made in the
    # GUI on every subsequent restart.
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEETING_SCRIBE_WHISPER_MODEL", "tiny")

    update_whisper_model_size(load_settings(), whisper_model_size="medium")

    assert load_settings().whisper_model_size == "medium"


def test_update_whisper_model_size_does_not_clobber_other_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))

    settings = update_audio_devices(load_settings(), mic_device_name="USB Mic", system_device_name=None)
    update_whisper_model_size(settings, whisper_model_size="medium")

    reloaded = load_settings()
    assert reloaded.mic_device_name == "USB Mic"
    assert reloaded.whisper_model_size == "medium"


def test_load_settings_defaults_meeting_hotkeys_to_unset(tmp_path, monkeypatch):
    # Off by default: a global hotkey that fires on every launch with no setup could collide with a
    # combo already claimed elsewhere on the user's PC, so this is opt-in from the Settings tab.
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))

    settings = load_settings()

    assert settings.start_meeting_hotkey is None
    assert settings.stop_meeting_hotkey is None


def test_update_meeting_hotkeys_persists_and_reloads(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))
    start = HotkeyCombo(modifiers=MOD_CONTROL | MOD_ALT, vk=0x53)  # Ctrl+Alt+S
    stop = HotkeyCombo(modifiers=MOD_CONTROL | MOD_ALT | MOD_SHIFT, vk=0x53)  # Ctrl+Alt+Shift+S

    update_meeting_hotkeys(load_settings(), start=start, stop=stop)

    reloaded = load_settings()
    assert reloaded.start_meeting_hotkey == start
    assert reloaded.stop_meeting_hotkey == stop


def test_update_meeting_hotkeys_can_clear_either_one(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))
    start = HotkeyCombo(modifiers=MOD_CONTROL, vk=0x53)

    update_meeting_hotkeys(load_settings(), start=start, stop=start)
    cleared = update_meeting_hotkeys(load_settings(), start=start, stop=None)

    assert cleared.start_meeting_hotkey == start
    assert cleared.stop_meeting_hotkey is None
    assert load_settings().stop_meeting_hotkey is None


def test_update_meeting_hotkeys_does_not_clobber_other_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))
    start = HotkeyCombo(modifiers=MOD_CONTROL, vk=0x53)

    settings = update_audio_devices(load_settings(), mic_device_name="USB Mic", system_device_name=None)
    update_meeting_hotkeys(settings, start=start, stop=None)

    reloaded = load_settings()
    assert reloaded.mic_device_name == "USB Mic"
    assert reloaded.start_meeting_hotkey == start


def test_load_settings_ignores_a_corrupt_hotkey_entry(tmp_path, monkeypatch):
    # A hand-edited or otherwise malformed settings.json shouldn't crash startup over one bad field —
    # same "soft preference" tolerance _load_user_config already documents for the whole file.
    import json

    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))
    (tmp_path / "settings.json").write_text(
        json.dumps({"start_meeting_hotkey": {"modifiers": "not-a-number"}}), encoding="utf-8"
    )

    assert load_settings().start_meeting_hotkey is None


def test_load_settings_defaults_diarize_system_audio_to_off(tmp_path, monkeypatch):
    # Off by default: turning this on sends meeting audio to third parties, so — like the meeting
    # hotkeys — it shouldn't happen without the user explicitly opting in from the Settings tab.
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))

    assert load_settings().diarize_system_audio is False


def test_update_diarize_system_audio_persists_and_reloads(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))

    update_diarize_system_audio(load_settings(), diarize_system_audio=True)

    assert load_settings().diarize_system_audio is True


def test_update_diarize_system_audio_does_not_clobber_other_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))

    settings = update_audio_devices(load_settings(), mic_device_name="USB Mic", system_device_name=None)
    update_diarize_system_audio(settings, diarize_system_audio=True)

    reloaded = load_settings()
    assert reloaded.mic_device_name == "USB Mic"
    assert reloaded.diarize_system_audio is True


def test_meeting_dir_is_keyed_by_meeting_id_alone(tmp_path, monkeypatch):
    # Regression test: meeting_dir used to take a project slug too (storage nested under
    # projects/<slug>/meetings/<id>) — moving a meeting to a different project would have meant moving
    # its recording files on disk, including possibly while the Recorder still had them open for
    # writing. Keying storage by meeting id alone makes a project reassignment a pure database update
    # (see Database.move_meeting_to_project) with nothing to move, at any time.
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))

    settings = load_settings()

    assert settings.meeting_dir(42) == tmp_path / "meetings" / "42"
    # No dependency on any project at all — the same meeting id always resolves to the same path.
    assert settings.meeting_dir(42) == settings.meeting_dir(42)
