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

# Prepopulated into the Settings tab's system prompt field. Explains to the model what it's being handed
# (a merged, automated, imperfect transcript) and what happens to what it produces (saved as the permanent
# record of the meeting, later retrieved to answer questions across a project) so a user who edits this
# has a template for what information matters to include.
DEFAULT_NOTES_SYSTEM_PROMPT = """You are a meeting assistant. You will receive a single merged transcript \
of a meeting, combining three sources: audio from the user's own microphone, audio from the meeting's \
system/speaker output (everyone else on the call), and text OCR'd from the screen during the meeting \
(slides, captions, shared chat). Lines are in chronological order, but the transcription and OCR are \
automated and may contain errors, misattributions, or gaps.

Turn this into concise, well-structured notes formatted as Markdown with these sections, in this order:

## Summary
2-4 sentences on what the meeting was about and its outcome.

## Key Discussion Points
Bullet list of the topics covered.

## Decisions
Bullet list of decisions made. Omit this section if none were made.

## Action Items
A markdown table with columns: Owner | Action | Due date (use "unspecified" when the transcript doesn't \
say). Omit this section if there are none.

Base everything strictly on the transcript. Do not invent names, dates, or commitments that aren't in it.

These notes become the permanent record of this meeting: they're shown to the user directly, and later \
indexed so they (or a future search) can ask "what did we decide about X" across every meeting in the \
project. Write for that audience — someone who wasn't necessarily in the meeting and is relying on these \
notes to reconstruct what happened."""


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
    # None means "use whatever Windows currently considers the default" for that device — the
    # historical/simple behavior, and still the default until the user picks something explicit.
    mic_device_name: str | None = None
    system_device_name: str | None = None
    notes_system_prompt: str = DEFAULT_NOTES_SYSTEM_PROMPT

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
    """Persists the user-editable settings (API key, model, chosen audio devices) so they survive a
    restart without the user needing to set environment variables — the GUI's Settings tab calls this
    after Save."""
    path = _user_config_path(settings.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "anthropic_api_key": settings.anthropic_api_key,
        "anthropic_model": settings.anthropic_model,
        "mic_device_name": settings.mic_device_name,
        "system_device_name": settings.system_device_name,
        "notes_system_prompt": settings.notes_system_prompt,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def update_settings(settings: Settings, *, anthropic_api_key: str | None, anthropic_model: str) -> Settings:
    """Applies and persists an API key / model edit from the Settings tab."""
    updated = replace(settings, anthropic_api_key=anthropic_api_key or None, anthropic_model=anthropic_model)
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


def update_notes_system_prompt(settings: Settings, *, notes_system_prompt: str) -> Settings:
    """Applies and persists an edit to the notes system prompt from the Settings tab. A blank value
    resets to the recommended default rather than sending Claude an empty system prompt."""
    updated = replace(settings, notes_system_prompt=notes_system_prompt.strip() or DEFAULT_NOTES_SYSTEM_PROMPT)
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
        mic_device_name=user_config.get("mic_device_name"),
        system_device_name=user_config.get("system_device_name"),
        notes_system_prompt=user_config.get("notes_system_prompt") or DEFAULT_NOTES_SYSTEM_PROMPT,
    )
