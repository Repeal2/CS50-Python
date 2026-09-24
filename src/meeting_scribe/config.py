"""Central place for paths and settings that the rest of the app reads from.

Everything lives under a single data directory so the whole app is relocatable/backup-able as one folder.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from meeting_scribe.hotkeys import HotkeyCombo

USER_CONFIG_FILENAME = "settings.json"

# The sizes offered in the Settings tab's model dropdown, smallest/fastest first. faster-whisper also
# supports .en (English-only) and distil-* (distilled, English-only) variants, but those are left out
# here to keep the dropdown to one choice per accuracy/memory tradeoff rather than a long list most users
# won't need — MEETING_SCRIBE_WHISPER_MODEL can still be set to any faster-whisper model name directly for
# anyone who wants one of those. Larger sizes transcribe more accurately but need proportionally more
# memory and CPU time; see transcription.engine.WhisperTranscriber for how this is used.
WHISPER_MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3", "large-v3-turbo")


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
    # On by default: during a meeting, the microphone and system audio are whichever devices Teams has
    # open (see audio.call_devices). When that can't be told, system audio follows whichever output
    # device is actually playing, and the microphone is the headset whenever one is connected and live,
    # mic_device_name otherwise — see audio.recorder.Recorder. headset_microphone_name picks the headset; None recognizes one by name.
    auto_switch_audio_devices: bool = True
    headset_microphone_name: str | None = None
    # Root of a folder synced by OneDrive/SharePoint — an Inbox subfolder lives under it (see
    # ai/copilot_push.py). None means nothing gets pushed anywhere: the meeting is still recorded and
    # saved locally, just not handed off.
    copilot_sync_dir: Path | None = None
    # Global (system-wide) keyboard shortcuts for starting/stopping a meeting without switching focus to
    # this app — see hotkeys.py. Either can be None to leave that action mouse-only, which is the default:
    # a hotkey that fires on every launch with no setup could collide with a combo already in use
    # elsewhere, so this is opt-in from the Settings tab rather than assigned automatically.
    start_meeting_hotkey: HotkeyCombo | None = None
    stop_meeting_hotkey: HotkeyCombo | None = None
    # Opt-in: sends the system-audio track to a Runpod-hosted WhisperX endpoint for speaker diarization
    # instead of transcribing it locally — see transcription.runpod_whisperx. Off by default, same
    # reasoning as the meeting hotkeys above: turning this on sends meeting audio to third parties, so it
    # shouldn't happen without the user explicitly choosing it.
    diarize_system_audio: bool = False
    # Credentials for the above, entered on the Settings tab and persisted to settings.json like every
    # other preference here — plaintext either way, so a settings field isn't meaningfully less secure
    # than an environment variable would have been for a single-user desktop app; it's just easier to set.
    # None means "not configured yet"; runpod_huggingface_token can also be left unset if HF_TOKEN was
    # instead set directly as an environment variable on the Runpod endpoint itself (see
    # transcription.runpod_whisperx's module docstring).
    runpod_api_key: str | None = None
    runpod_endpoint_id: str | None = None
    runpod_huggingface_token: str | None = None
    # Where the on-screen OCR box was last left — (left, top, width, height) in screen pixels — so the
    # next meeting's box appears in the same place instead of needing to be set up again. None until
    # the box has been moved or resized once. See screen.ocr_box.
    ocr_area: tuple[int, int, int, int] | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "meeting_scribe.db"

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

    def meeting_dir(self, meeting_id: int) -> Path:
        """Where one meeting's recorded audio and screen-capture state live during and immediately after
        it. Keyed by meeting id alone, not by project — moving a meeting to a different project (see
        storage.database.Database.move_meeting_to_project) is a pure database update with nothing to move
        on disk, which matters because it needs to stay safe to do even while a Recorder still has these
        files open for writing."""
        return self.data_dir / "meetings" / str(meeting_id)


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


def _hotkey_to_json(combo: HotkeyCombo | None) -> dict | None:
    return {"modifiers": combo.modifiers, "vk": combo.vk} if combo is not None else None


def _hotkey_from_json(data: object) -> HotkeyCombo | None:
    if not isinstance(data, dict):
        return None
    try:
        return HotkeyCombo(modifiers=int(data["modifiers"]), vk=int(data["vk"]))
    except (KeyError, TypeError, ValueError):
        return None


def save_user_config(settings: Settings) -> None:
    """Persists the user-editable settings (Copilot sync folder, chosen audio devices, Whisper model
    size, meeting shortcuts) so they survive a restart without the user needing to set environment
    variables — the GUI's Settings tab calls this after Save."""
    path = _user_config_path(settings.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "mic_device_name": settings.mic_device_name,
        "system_device_name": settings.system_device_name,
        "auto_switch_audio_devices": settings.auto_switch_audio_devices,
        "headset_microphone_name": settings.headset_microphone_name,
        "copilot_sync_dir": str(settings.copilot_sync_dir) if settings.copilot_sync_dir else None,
        "whisper_model_size": settings.whisper_model_size,
        "start_meeting_hotkey": _hotkey_to_json(settings.start_meeting_hotkey),
        "stop_meeting_hotkey": _hotkey_to_json(settings.stop_meeting_hotkey),
        "diarize_system_audio": settings.diarize_system_audio,
        "runpod_api_key": settings.runpod_api_key,
        "runpod_endpoint_id": settings.runpod_endpoint_id,
        "runpod_huggingface_token": settings.runpod_huggingface_token,
        "ocr_area": list(settings.ocr_area) if settings.ocr_area else None,
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


def update_audio_automation(
    settings: Settings, *, auto_switch_audio_devices: bool, headset_microphone_name: str | None
) -> Settings:
    """Applies and persists the Settings tab's automatic device switching choices. Like the device
    choices themselves, these are read when a meeting starts."""
    updated = replace(
        settings,
        auto_switch_audio_devices=auto_switch_audio_devices,
        headset_microphone_name=headset_microphone_name or None,
    )
    save_user_config(updated)
    return updated


def update_diarize_system_audio(settings: Settings, *, diarize_system_audio: bool) -> Settings:
    """Applies and persists the Settings tab's cloud-speaker-diarization opt-in checkbox. Only affects
    meetings whose transcription hasn't started yet — like whisper_model_size, a MeetingSession reads this
    once at construction time (see session.py)."""
    updated = replace(settings, diarize_system_audio=diarize_system_audio)
    save_user_config(updated)
    return updated


def update_runpod_settings(
    settings: Settings,
    *,
    runpod_api_key: str | None,
    runpod_endpoint_id: str | None,
    runpod_huggingface_token: str | None,
) -> Settings:
    """Applies and persists the Settings tab's Runpod/HuggingFace credential fields. Blank fields are
    stored as None, same convention as update_copilot_settings' sync folder."""
    updated = replace(
        settings,
        runpod_api_key=runpod_api_key or None,
        runpod_endpoint_id=runpod_endpoint_id or None,
        runpod_huggingface_token=runpod_huggingface_token or None,
    )
    save_user_config(updated)
    return updated


def update_whisper_model_size(settings: Settings, *, whisper_model_size: str) -> Settings:
    """Applies and persists the Settings tab's Whisper model-size choice. Only affects meetings started
    after this is saved — a MeetingSession builds its own WhisperTranscriber once, at construction time
    (see session.py), so one already recording or finishing up keeps using whatever size was in effect
    when it started."""
    updated = replace(settings, whisper_model_size=whisper_model_size)
    save_user_config(updated)
    return updated


def update_meeting_hotkeys(
    settings: Settings, *, start: HotkeyCombo | None, stop: HotkeyCombo | None
) -> Settings:
    """Applies and persists the Settings tab's Start/Stop meeting shortcut choices. Either can be None to
    leave that action mouse-only."""
    updated = replace(settings, start_meeting_hotkey=start, stop_meeting_hotkey=stop)
    save_user_config(updated)
    return updated


def update_ocr_area(settings: Settings, *, ocr_area: tuple[int, int, int, int] | None) -> Settings:
    """Applies and persists where the OCR box was left (see Settings.ocr_area)."""
    updated = replace(settings, ocr_area=ocr_area)
    save_user_config(updated)
    return updated


def _ocr_area_from_json(data: object) -> tuple[int, int, int, int] | None:
    if not isinstance(data, (list, tuple)) or len(data) != 4:
        return None
    try:
        left, top, width, height = (int(value) for value in data)
    except (TypeError, ValueError):
        return None
    return (left, top, width, height) if width > 0 and height > 0 else None


def load_settings() -> Settings:
    data_dir = _default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    user_config = _load_user_config(data_dir)

    copilot_sync_dir = user_config.get("copilot_sync_dir") or os.environ.get("MEETING_SCRIBE_COPILOT_SYNC_DIR")
    whisper_model_size = user_config.get("whisper_model_size") or os.environ.get(
        "MEETING_SCRIBE_WHISPER_MODEL", "small"
    )

    return Settings(
        data_dir=data_dir,
        whisper_model_size=whisper_model_size,
        tesseract_cmd=resolve_tesseract_cmd(os.environ.get("MEETING_SCRIBE_TESSERACT_PATH")),
        screen_capture_interval_seconds=float(
            os.environ.get("MEETING_SCRIBE_SCREEN_INTERVAL", "3.0")
        ),
        mic_device_name=user_config.get("mic_device_name"),
        system_device_name=user_config.get("system_device_name"),
        auto_switch_audio_devices=bool(user_config.get("auto_switch_audio_devices", True)),
        headset_microphone_name=user_config.get("headset_microphone_name"),
        copilot_sync_dir=Path(copilot_sync_dir) if copilot_sync_dir else None,
        start_meeting_hotkey=_hotkey_from_json(user_config.get("start_meeting_hotkey")),
        stop_meeting_hotkey=_hotkey_from_json(user_config.get("stop_meeting_hotkey")),
        diarize_system_audio=bool(user_config.get("diarize_system_audio", False)),
        runpod_api_key=user_config.get("runpod_api_key"),
        runpod_endpoint_id=user_config.get("runpod_endpoint_id"),
        runpod_huggingface_token=user_config.get("runpod_huggingface_token"),
        ocr_area=_ocr_area_from_json(user_config.get("ocr_area")),
    )
