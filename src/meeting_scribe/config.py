"""Central place for paths and settings that the rest of the app reads from.

Everything lives under a single data directory so the whole app is relocatable/backup-able as one folder.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path

USER_CONFIG_FILENAME = "settings.json"

# Offered as presets in the GUI's Settings tab; the field itself accepts any string so an advanced user
# can type a model ID that isn't listed here.
AVAILABLE_MODELS = ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]

DEFAULT_MODEL = "claude-opus-5"


def _default_data_dir() -> Path:
    override = os.environ.get("MEETING_SCRIBE_DATA_DIR")
    if override:
        return Path(override)
    # %APPDATA%\MeetingScribe on Windows; ~/.meeting_scribe_data elsewhere (dev/test).
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "MeetingScribe"
    return Path.home() / ".meeting_scribe_data"


def _bundled_tesseract_path() -> Path | None:
    """Locates the Tesseract install vendored into the .exe by packaging/vendor_tesseract.py, if any.

    PyInstaller's onefile build (see packaging/meeting_scribe.spec) extracts bundled data files to a
    temp directory at startup, exposed as `sys._MEIPASS`. That attribute only exists when running from
    a frozen build, so this is naturally a no-op in normal `python -m meeting_scribe.main` development.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass is None:
        return None
    candidate = Path(meipass) / "tesseract" / "tesseract.exe"
    return candidate if candidate.exists() else None


def resolve_tesseract_cmd(explicit: str | None) -> str | None:
    """Picks the Tesseract binary to use: an explicit override wins, then a bundled copy inside the
    .exe, otherwise None (pytesseract falls back to finding `tesseract` on PATH itself)."""
    if explicit:
        return explicit
    bundled = _bundled_tesseract_path()
    return str(bundled) if bundled else None


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    anthropic_api_key: str | None
    anthropic_model: str
    whisper_model_size: str
    tesseract_cmd: str | None
    screen_capture_interval_seconds: float

    @property
    def db_path(self) -> Path:
        return self.data_dir / "meeting_scribe.db"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    def project_dir(self, project_slug: str) -> Path:
        return self.projects_dir / project_slug

    def meeting_dir(self, project_slug: str, meeting_id: int) -> Path:
        return self.project_dir(project_slug) / "meetings" / str(meeting_id)


def _user_config_path(data_dir: Path) -> Path:
    return data_dir / USER_CONFIG_FILENAME


def _load_user_config(data_dir: Path) -> dict:
    """Reads the GUI Settings tab's saved API key/model, if any. Missing or corrupt is treated the
    same as "nothing saved yet" — this is a soft preference, not something to crash startup over."""
    path = _user_config_path(data_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_user_config(settings: Settings) -> None:
    """Persists the user-editable settings (API key, model) so they survive a restart without the user
    needing to set environment variables — the GUI's Settings tab calls this after Save."""
    path = _user_config_path(settings.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "anthropic_api_key": settings.anthropic_api_key,
        "anthropic_model": settings.anthropic_model,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def update_settings(settings: Settings, *, anthropic_api_key: str | None, anthropic_model: str) -> Settings:
    """Applies and persists a Settings tab edit, returning the new Settings to use from then on."""
    updated = replace(settings, anthropic_api_key=anthropic_api_key or None, anthropic_model=anthropic_model)
    save_user_config(updated)
    return updated


def load_settings() -> Settings:
    data_dir = _default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    user_config = _load_user_config(data_dir)

    # Precedence: a value saved via the Settings tab wins (it's an explicit, recent user action), then
    # the environment variable (useful for CI/scripting), then the hardcoded default.
    anthropic_api_key = user_config.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
    anthropic_model = (
        user_config.get("anthropic_model")
        or os.environ.get("MEETING_SCRIBE_MODEL")
        or DEFAULT_MODEL
    )

    return Settings(
        data_dir=data_dir,
        anthropic_api_key=anthropic_api_key,
        anthropic_model=anthropic_model,
        whisper_model_size=os.environ.get("MEETING_SCRIBE_WHISPER_MODEL", "small"),
        tesseract_cmd=resolve_tesseract_cmd(os.environ.get("MEETING_SCRIBE_TESSERACT_PATH")),
        screen_capture_interval_seconds=float(
            os.environ.get("MEETING_SCRIBE_SCREEN_INTERVAL", "3.0")
        ),
    )
