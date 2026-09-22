"""Speaker-diarized transcription of the system-audio track via a Runpod-hosted WhisperX serverless
endpoint — opt-in (see config.Settings.diarize_system_audio), since it sends recorded meeting audio to
two third parties (Runpod's GPU host, and HuggingFace's hosted pyannote diarization models) rather than
keeping everything on this machine the way transcription.engine.WhisperTranscriber does. Only ever used
for the system track: the mic track is already just one person, so diarizing it can't identify anyone new
— see the module note in session.py for where this plugs in.

Needs a Runpod API key and the id of a deployed WhisperX-with-diarization serverless endpoint before it'll
do anything (see is_configured); a HuggingFace access token that has accepted pyannote's gated model terms
is also needed for diarization itself to work, though the underlying worker may fail with its own error
rather than this module catching that case specifically. None of these are secrets this app should ever
write to disk (unlike the rest of Settings), so — unlike whisper_model_size or the Copilot sync folder —
they're read from the environment only, never offered as a Settings-tab field or persisted to
settings.json.

_build_payload and _parse_segments are written against kodxana/whisperx-worker_v2's verified schema
(github.com/kodxana/whisperx-worker_v2, checked directly against its rp_handler.py/rp_schema.py, not just
its README) — the actively-maintained successor to kodxana/whisperx-worker, which its own README says has
moved and is now archived. Deploying a different worker image means checking its actual input/output shape
and adjusting those two functions to match; don't assume another template uses the same field names.
"""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from meeting_scribe.transcription.engine import TranscriptLine, wav_duration_seconds

_API_KEY_ENV = "MEETING_SCRIBE_RUNPOD_API_KEY"
_ENDPOINT_ID_ENV = "MEETING_SCRIBE_RUNPOD_ENDPOINT_ID"
_HF_TOKEN_ENV = "MEETING_SCRIBE_RUNPOD_HF_TOKEN"

# Base64-inlining the WAV into the job payload avoids needing any separate file host (S3/R2/etc.) to get
# started, but Runpod's serverless queue API caps how big one job's request body can be — kodxana/
# whisperx-worker_v2's README states this directly: 10MB for the async /run endpoint this module uses (20MB
# for /runsync, which this module doesn't use). Above this, a real upload target + presigned URL (see
# RunpodWhisperXTranscriber's `upload` argument) is the only option.
_MAX_INLINE_AUDIO_BYTES = 10 * 1024 * 1024


class RunpodWhisperXError(Exception):
    """Raised for anything that keeps a diarized transcript from coming back: missing configuration, a
    failed/timed-out Runpod job, or an audio file too large to send inline with no uploader configured.
    Always safe for a caller to catch and fall back to local (undiarized) transcription instead — see
    session._transcribe_system_track."""


def is_configured() -> bool:
    """Whether enough environment variables are set to even attempt a cloud diarization call. Checked
    up front so a meeting that opted into cloud diarization without finishing setup fails with one clear
    message instead of partway through a job submission."""
    return bool(os.environ.get(_API_KEY_ENV)) and bool(os.environ.get(_ENDPOINT_ID_ENV))


@dataclass(frozen=True)
class _DiarizedSegment:
    start_seconds: float
    speaker: str
    text: str


class RunpodWhisperXTranscriber:
    """Sends one audio track to a Runpod WhisperX serverless endpoint for transcription with speaker
    diarization. Mirrors WhisperTranscriber's transcribe_parts(audio_paths, source) shape so session.py
    can use either one interchangeably for the system track.

    `upload`, if given, turns a local WAV path into a URL the worker can fetch, instead of inlining the
    audio as base64 in the job payload — needed for anything past _MAX_INLINE_AUDIO_BYTES, i.e. most real
    meeting recordings. Left as None until there's somewhere to upload to; until then this only works for
    short recordings. `session` is an injected requests.Session-alike (get/post), mainly so tests don't
    need a real network call or even the `requests` package installed — defaults to a real one lazily, the
    same deferred-import approach WhisperTranscriber uses for faster_whisper.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint_id: str | None = None,
        huggingface_token: str | None = None,
        upload: Callable[[Path], str] | None = None,
        session=None,
        poll_seconds: float = 5.0,
        timeout_seconds: float = 1800.0,
    ):
        self._api_key = api_key or os.environ.get(_API_KEY_ENV)
        self._endpoint_id = endpoint_id or os.environ.get(_ENDPOINT_ID_ENV)
        self._huggingface_token = huggingface_token or os.environ.get(_HF_TOKEN_ENV)
        self._upload = upload
        self._session = session
        self._poll_seconds = poll_seconds
        self._timeout_seconds = timeout_seconds
        if not self._api_key or not self._endpoint_id:
            raise RunpodWhisperXError(
                f"Cloud speaker diarization is opted into but not configured — set {_API_KEY_ENV} and "
                f"{_ENDPOINT_ID_ENV} (and usually {_HF_TOKEN_ENV}, for diarization itself to work)."
            )

    def _ensure_session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    @property
    def _base_url(self) -> str:
        return f"https://api.runpod.ai/v2/{self._endpoint_id}"

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    def transcribe_parts(self, audio_paths: Sequence[Path], source: str) -> list[TranscriptLine]:
        """Transcribes one capture stream that may have been written as several WAV parts, with the same
        offset-stitching WhisperTranscriber.transcribe_parts does for a long meeting rolled over into
        multiple files (see its docstring) — except speaker ids are only consistent *within* one part:
        WhisperX diarizes each job independently, so "SPEAKER_00" in part 2 isn't guaranteed to be the
        same person as "SPEAKER_00" in part 1. Fine for the common case of a meeting that never rolls
        over; reconciling speaker identity across parts isn't attempted yet."""
        lines: list[TranscriptLine] = []
        offset_seconds = 0.0
        for audio_path in audio_paths:
            lines.extend(
                TranscriptLine(
                    timestamp_seconds=segment.start_seconds + offset_seconds,
                    source=source,
                    text=segment.text,
                    speaker=segment.speaker,
                )
                for segment in self._transcribe_one(audio_path)
            )
            offset_seconds += wav_duration_seconds(audio_path)
        return lines

    def _transcribe_one(self, audio_path: Path) -> list[_DiarizedSegment]:
        job_id = self._submit_job(self._resolve_audio_url(audio_path))
        output = self._wait_for_result(job_id)
        return _parse_segments(output)

    def _resolve_audio_url(self, audio_path: Path) -> str:
        if self._upload is not None:
            return self._upload(audio_path)
        audio_bytes = audio_path.read_bytes()
        if len(audio_bytes) > _MAX_INLINE_AUDIO_BYTES:
            raise RunpodWhisperXError(
                f"{audio_path.name} is {len(audio_bytes) / 1_048_576:.1f} MB, too large to send inline "
                "as base64 — configure an `upload` callable (e.g. to S3-compatible storage) to use cloud "
                "diarization on a meeting this long."
            )
        encoded = base64.b64encode(audio_bytes).decode("ascii")
        return f"data:audio/wav;base64,{encoded}"

    def _submit_job(self, audio_url: str) -> str:
        payload = _build_payload(audio_url, huggingface_token=self._huggingface_token)
        response = self._ensure_session().post(
            f"{self._base_url}/run", headers=self._headers, json=payload, timeout=30
        )
        _raise_for_bad_response(response)
        return response.json()["id"]

    def _wait_for_result(self, job_id: str) -> dict:
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            response = self._ensure_session().get(
                f"{self._base_url}/status/{job_id}", headers=self._headers, timeout=30
            )
            _raise_for_bad_response(response)
            body = response.json()
            status = body.get("status")
            if status == "COMPLETED":
                return body["output"]
            if status in ("FAILED", "CANCELLED"):
                raise RunpodWhisperXError(f"Runpod job {job_id} ended as {status}: {body.get('error')}")
            if time.monotonic() >= deadline:
                raise RunpodWhisperXError(
                    f"Runpod job {job_id} didn't finish within {self._timeout_seconds:.0f}s"
                )
            time.sleep(self._poll_seconds)


def _raise_for_bad_response(response) -> None:
    if response.status_code >= 400:
        raise RunpodWhisperXError(f"Runpod API call failed ({response.status_code}): {response.text[:500]}")


def _build_payload(audio_url: str, *, huggingface_token: str | None) -> dict:
    """kodxana/whisperx-worker_v2's input schema (verified against its rp_schema.py, not just its README):
    `audio_file` (a URL or base64 audio, not `audio`), `diarization`, and an optional
    `huggingface_access_token` that overrides its endpoint-side `HF_TOKEN` env var when given. Its schema
    validator rejects any key it doesn't recognize — there's no `model` parameter to pick a Whisper size,
    so don't add one. Adjust this function if a different worker image ever replaces it."""
    payload = {"input": {"audio_file": audio_url, "diarization": True}}
    if huggingface_token:
        payload["input"]["huggingface_access_token"] = huggingface_token
    return payload


def _parse_segments(output: dict) -> list[_DiarizedSegment]:
    return [
        _DiarizedSegment(
            start_seconds=segment["start"],
            speaker=segment.get("speaker", "UNKNOWN"),
            text=segment["text"].strip(),
        )
        for segment in output.get("segments", [])
        if segment.get("text", "").strip()
    ]
