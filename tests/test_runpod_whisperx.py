import contextlib
import wave

import pytest

from meeting_scribe.transcription import runpod_whisperx
from meeting_scribe.transcription.runpod_whisperx import (
    RunpodWhisperXError,
    RunpodWhisperXTranscriber,
    _build_payload,
    _parse_segments,
    _raise_for_bad_response,
    is_configured,
)


def _write_wav(path, seconds, framerate=16000):
    with contextlib.closing(wave.open(str(path), "wb")) as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(framerate)
        wav_file.writeframes(b"\x00\x00" * int(framerate * seconds))


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {}
        self.text = text

    def json(self):
        return self._json_body


class FakeSession:
    """Returns queued canned responses in call order — one per post()/get() the transcriber makes."""

    def __init__(self, responses):
        self._responses = iter(responses)
        self.requests: list[tuple] = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.requests.append(("POST", url, json))
        return next(self._responses)

    def get(self, url, headers=None, timeout=None):
        self.requests.append(("GET", url, None))
        return next(self._responses)


def _env(monkeypatch, *, api_key="key", endpoint_id="endpoint", hf_token=None):
    monkeypatch.setenv("MEETING_SCRIBE_RUNPOD_API_KEY", api_key)
    monkeypatch.setenv("MEETING_SCRIBE_RUNPOD_ENDPOINT_ID", endpoint_id)
    if hf_token:
        monkeypatch.setenv("MEETING_SCRIBE_RUNPOD_HF_TOKEN", hf_token)
    else:
        monkeypatch.delenv("MEETING_SCRIBE_RUNPOD_HF_TOKEN", raising=False)


def test_is_configured_false_with_nothing_set(monkeypatch):
    monkeypatch.delenv("MEETING_SCRIBE_RUNPOD_API_KEY", raising=False)
    monkeypatch.delenv("MEETING_SCRIBE_RUNPOD_ENDPOINT_ID", raising=False)
    assert not is_configured()


def test_is_configured_requires_both_api_key_and_endpoint_id(monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_RUNPOD_API_KEY", "key")
    monkeypatch.delenv("MEETING_SCRIBE_RUNPOD_ENDPOINT_ID", raising=False)
    assert not is_configured()


def test_is_configured_true_once_both_are_set(monkeypatch):
    _env(monkeypatch)
    assert is_configured()


def test_constructor_raises_without_configuration(monkeypatch):
    monkeypatch.delenv("MEETING_SCRIBE_RUNPOD_API_KEY", raising=False)
    monkeypatch.delenv("MEETING_SCRIBE_RUNPOD_ENDPOINT_ID", raising=False)

    with pytest.raises(RunpodWhisperXError):
        RunpodWhisperXTranscriber()


def test_build_payload_includes_huggingface_token_when_given():
    payload = _build_payload("https://example.com/a.wav", huggingface_token="hf_abc")
    assert payload["input"]["huggingface_access_token"] == "hf_abc"
    assert payload["input"]["audio_file"] == "https://example.com/a.wav"
    assert payload["input"]["diarization"] is True
    assert "model" not in payload["input"]  # not a recognized field on this worker's schema


def test_build_payload_omits_huggingface_token_when_not_given():
    payload = _build_payload("https://example.com/a.wav", huggingface_token=None)
    assert "huggingface_access_token" not in payload["input"]


def test_parse_segments_skips_blank_text_and_defaults_unknown_speaker():
    output = {
        "segments": [
            {"start": 0.0, "speaker": "SPEAKER_00", "text": "hello there"},
            {"start": 1.0, "speaker": "SPEAKER_01", "text": "   "},
            {"start": 2.0, "text": "no speaker key"},
        ]
    }

    segments = _parse_segments(output)

    assert [(s.start_seconds, s.speaker, s.text) for s in segments] == [
        (0.0, "SPEAKER_00", "hello there"),
        (2.0, "UNKNOWN", "no speaker key"),
    ]


def test_raise_for_bad_response_raises_on_http_error():
    with pytest.raises(RunpodWhisperXError):
        _raise_for_bad_response(FakeResponse(status_code=500, text="boom"))


def test_raise_for_bad_response_is_silent_on_success():
    _raise_for_bad_response(FakeResponse(status_code=200))  # must not raise


def test_transcribe_parts_submits_polls_and_parses_a_completed_job(tmp_path, monkeypatch):
    _env(monkeypatch, hf_token="hf_abc")
    audio = tmp_path / "system.wav"
    _write_wav(audio, seconds=5.0)

    session = FakeSession(
        [
            FakeResponse(json_body={"id": "job-1"}),  # POST /run
            FakeResponse(json_body={"status": "IN_QUEUE"}),  # GET /status (not done yet)
            FakeResponse(
                json_body={
                    "status": "COMPLETED",
                    "output": {"segments": [{"start": 1.5, "speaker": "SPEAKER_00", "text": "hi"}]},
                }
            ),
        ]
    )
    transcriber = RunpodWhisperXTranscriber(session=session, poll_seconds=0)

    lines = transcriber.transcribe_parts([audio], source="system")

    assert [(line.timestamp_seconds, line.source, line.speaker, line.text) for line in lines] == [
        (1.5, "system", "SPEAKER_00", "hi")
    ]
    # The huggingface token reached the actual request payload.
    _, _, run_payload = session.requests[0]
    assert run_payload["input"]["huggingface_access_token"] == "hf_abc"


def test_transcribe_parts_raises_on_a_failed_job(tmp_path, monkeypatch):
    _env(monkeypatch)
    audio = tmp_path / "system.wav"
    _write_wav(audio, seconds=1.0)

    session = FakeSession(
        [
            FakeResponse(json_body={"id": "job-1"}),
            FakeResponse(json_body={"status": "FAILED", "error": "out of memory"}),
        ]
    )
    transcriber = RunpodWhisperXTranscriber(session=session, poll_seconds=0)

    with pytest.raises(RunpodWhisperXError, match="out of memory"):
        transcriber.transcribe_parts([audio], source="system")


def test_transcribe_parts_raises_on_timeout(tmp_path, monkeypatch):
    _env(monkeypatch)
    audio = tmp_path / "system.wav"
    _write_wav(audio, seconds=1.0)

    session = FakeSession(
        [
            FakeResponse(json_body={"id": "job-1"}),
            FakeResponse(json_body={"status": "IN_PROGRESS"}),
        ]
    )
    transcriber = RunpodWhisperXTranscriber(session=session, poll_seconds=0, timeout_seconds=-1)

    with pytest.raises(RunpodWhisperXError, match="didn't finish"):
        transcriber.transcribe_parts([audio], source="system")


def test_transcribe_parts_offsets_a_second_part_by_the_first_parts_duration(tmp_path, monkeypatch):
    _env(monkeypatch)
    first, second = tmp_path / "system.wav", tmp_path / "system.part2.wav"
    _write_wav(first, seconds=60.0)
    _write_wav(second, seconds=10.0)

    session = FakeSession(
        [
            FakeResponse(json_body={"id": "job-1"}),
            FakeResponse(
                json_body={
                    "status": "COMPLETED",
                    "output": {"segments": [{"start": 2.0, "speaker": "SPEAKER_00", "text": "first part"}]},
                }
            ),
            FakeResponse(json_body={"id": "job-2"}),
            FakeResponse(
                json_body={
                    "status": "COMPLETED",
                    "output": {"segments": [{"start": 3.0, "speaker": "SPEAKER_01", "text": "second part"}]},
                }
            ),
        ]
    )
    transcriber = RunpodWhisperXTranscriber(session=session, poll_seconds=0)

    lines = transcriber.transcribe_parts([first, second], source="system")

    assert [(line.timestamp_seconds, line.text) for line in lines] == [
        (2.0, "first part"),
        (63.0, "second part"),
    ]


def test_resolve_audio_url_inlines_a_small_file_as_base64(tmp_path, monkeypatch):
    _env(monkeypatch)
    audio = tmp_path / "system.wav"
    audio.write_bytes(b"tiny-audio-bytes")
    transcriber = RunpodWhisperXTranscriber(session=FakeSession([]))

    url = transcriber._resolve_audio_url(audio)

    assert url.startswith("data:audio/wav;base64,")


def test_resolve_audio_url_raises_when_too_large_and_no_uploader(tmp_path, monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(runpod_whisperx, "_MAX_INLINE_AUDIO_BYTES", 4)
    audio = tmp_path / "system.wav"
    audio.write_bytes(b"way too big for the inline limit")
    transcriber = RunpodWhisperXTranscriber(session=FakeSession([]))

    with pytest.raises(RunpodWhisperXError, match="too large"):
        transcriber._resolve_audio_url(audio)


def test_resolve_audio_url_uses_the_upload_callable_when_given(tmp_path, monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(runpod_whisperx, "_MAX_INLINE_AUDIO_BYTES", 4)
    audio = tmp_path / "system.wav"
    audio.write_bytes(b"way too big for the inline limit")
    transcriber = RunpodWhisperXTranscriber(
        session=FakeSession([]), upload=lambda path: f"https://bucket.example.com/{path.name}"
    )

    assert transcriber._resolve_audio_url(audio) == "https://bucket.example.com/system.wav"
