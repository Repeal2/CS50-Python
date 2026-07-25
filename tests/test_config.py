import sys

from meeting_scribe.config import DEFAULT_MODEL, load_settings, resolve_tesseract_cmd, update_settings


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


def test_load_settings_falls_back_to_default_model_with_no_key_or_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("MEETING_SCRIBE_MODEL", raising=False)

    settings = load_settings()

    assert settings.anthropic_api_key is None
    assert settings.anthropic_model == DEFAULT_MODEL


def test_load_settings_uses_env_vars_when_no_saved_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    monkeypatch.setenv("MEETING_SCRIBE_MODEL", "claude-sonnet-5")

    settings = load_settings()

    assert settings.anthropic_api_key == "sk-from-env"
    assert settings.anthropic_model == "claude-sonnet-5"


def test_saved_settings_take_precedence_over_env_vars(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    monkeypatch.setenv("MEETING_SCRIBE_MODEL", "claude-sonnet-5")

    update_settings(load_settings(), anthropic_api_key="sk-from-settings-tab", anthropic_model="claude-haiku-4-5")

    reloaded = load_settings()
    assert reloaded.anthropic_api_key == "sk-from-settings-tab"
    assert reloaded.anthropic_model == "claude-haiku-4-5"


def test_update_settings_persists_and_returns_the_new_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))

    updated = update_settings(load_settings(), anthropic_api_key="sk-test", anthropic_model="claude-opus-5")

    assert updated.anthropic_api_key == "sk-test"
    assert updated.anthropic_model == "claude-opus-5"
    assert (tmp_path / "settings.json").exists()


def test_update_settings_treats_blank_key_as_cleared(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path))

    update_settings(load_settings(), anthropic_api_key="sk-test", anthropic_model=DEFAULT_MODEL)
    cleared = update_settings(load_settings(), anthropic_api_key="", anthropic_model=DEFAULT_MODEL)

    assert cleared.anthropic_api_key is None
