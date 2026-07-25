"""Central place for paths and settings that the rest of the app reads from.

Everything lives under a single data directory so the whole app is relocatable/backup-able as one folder.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _default_data_dir() -> Path:
    override = os.environ.get("MEETING_SCRIBE_DATA_DIR")
    if override:
        return Path(override)
    # %APPDATA%\MeetingScribe on Windows; ~/.meeting_scribe_data elsewhere (dev/test).
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "MeetingScribe"
    return Path.home() / ".meeting_scribe_data"


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


def load_settings() -> Settings:
    data_dir = _default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        data_dir=data_dir,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        anthropic_model=os.environ.get("MEETING_SCRIBE_MODEL", "claude-opus-5"),
        whisper_model_size=os.environ.get("MEETING_SCRIBE_WHISPER_MODEL", "small"),
        tesseract_cmd=os.environ.get("MEETING_SCRIBE_TESSERACT_PATH"),
        screen_capture_interval_seconds=float(
            os.environ.get("MEETING_SCRIBE_SCREEN_INTERVAL", "3.0")
        ),
    )
