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
    whisper_model_size: str
    tesseract_cmd: str | None
    screen_capture_interval_seconds: float
    # None means "use whatever Windows currently considers the default" for that device — the
    # historical/simple behavior, and still the default until the user picks something explicit.
    mic_device_name: str | None = None
    system_device_name: str | None = None
    # Root of a folder synced by OneDrive/SharePoint — an Inbox subfolder lives under it (see
    # ai/copilot_push.py). None means nothing gets pushed anywhere: the meeting is still recorded and
    # saved locally, just not handed off.
    copilot_sync_dir: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "meeting_scribe.db"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def documents_dir(self) -> Path:
        """Where uploaded documents' original bytes are kept (under a synthetic, collision-free name —
        see storage.documents.save_original_copy), separate from the DB's extracted-text copy, so the
        original file is still available later for the Copilot push package (which hands off the
        original file under its own real filename, not the extracted text)."""
        return self.data_dir / "documents"

    @property
    def copilot_inbox_dir(self) -> Path:
        return self.copilot_sync_dir / "Inbox"

    def project_dir(self, project_slug: str) -> Path:
        return self.projects_dir / project_slug

    def meeting_dir(self, project_slug: str, meeting_id: int) -> Path:
        return self.project_dir(project_slug) / "meetings" / str(meeting_id)


def _user_config_path(data_dir: Path) -> Path:
    return data_dir / USER_CONFIG_FILENAME


def _load_user_config(data_dir: Path) -> dict:
    """Reads the GUI Settings tab's saved preferences, if any. Missing or corrupt is treated the same
    as "nothing saved yet" — this is a soft preference, not something to crash startup over."""
    path = _user_config_path(data_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_user_config(settings: Settings) -> None:
    """Persists the user-editable settings (Copilot sync folder, chosen audio devices) so they survive
    a restart without the user needing to set environment variables — the GUI's Settings tab calls this
    after Save."""
    path = _user_config_path(settings.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "mic_device_name": settings.mic_device_name,
        "system_device_name": settings.system_device_name,
        "copilot_sync_dir": str(settings.copilot_sync_dir) if settings.copilot_sync_dir else None,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def update_copilot_settings(settings: Settings, *, copilot_sync_dir: str | None) -> Settings:
    """Applies and persists the Copilot Studio push-folder edit from the Settings tab. A blank
    `copilot_sync_dir` disables pushing entirely — meetings are still recorded and saved locally."""
    updated = replace(settings, copilot_sync_dir=Path(copilot_sync_dir) if copilot_sync_dir else None)
    save_user_config(updated)
    return updated


def update_audio_devices(
    settings: Settings, *, mic_device_name: str | None, system_device_name: str | None
) -> Settings:
    """Applies and persists a microphone / system-audio device choice from the Settings tab. None means
    "use whatever Windows currently considers the default" for that device."""
    updated = replace(settings, mic_device_name=mic_device_name, system_device_name=system_device_name)
    save_user_config(updated)
    return updated


def load_settings() -> Settings:
    data_dir = _default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    user_config = _load_user_config(data_dir)

    copilot_sync_dir = user_config.get("copilot_sync_dir") or os.environ.get("MEETING_SCRIBE_COPILOT_SYNC_DIR")

    return Settings(
        data_dir=data_dir,
        whisper_model_size=os.environ.get("MEETING_SCRIBE_WHISPER_MODEL", "small"),
        tesseract_cmd=resolve_tesseract_cmd(os.environ.get("MEETING_SCRIBE_TESSERACT_PATH")),
        screen_capture_interval_seconds=float(
            os.environ.get("MEETING_SCRIBE_SCREEN_INTERVAL", "3.0")
        ),
        mic_device_name=user_config.get("mic_device_name"),
        system_device_name=user_config.get("system_device_name"),
        copilot_sync_dir=Path(copilot_sync_dir) if copilot_sync_dir else None,
    )
