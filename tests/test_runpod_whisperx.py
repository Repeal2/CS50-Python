import base64
import contextlib
import json
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from meeting_scribe.transcription import runpod_whisperx
from meeting_scribe.transcription.runpod_whisperx import (
    RunpodWhisperXError,
    RunpodWhisperXTranscriber,
    _HttpSession,
    _build_payload,
    _parse_segments,
    _raise_for_bad_response,
    is_configured,
)


def _write_wav(path, seconds, framerate=48000, channels=2):
    """A silent WAV in the loopback recorder's usual format. Compressing it (see speech_encoding) needs
    PyAV, which faster-whisper already depends on."""
    pytest.importorskip("av")
    with contextlib.closing(wave.open(str(path), "wb")) as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(framerate)
        wav_file.writeframes(b"\x00\x00" * channels * int(framerate * seconds))


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {}
        self.text = text

    def json(self):
        return self._json_body


class FakeRunpod:
    """Stands in for Runpod's API: each POST /run is given the next job id ("job-0", "job-1", ...);
    GET /status/<id> answers from that job's list of status bodies in turn, repeating the last one once
    they're used up; POST /cancel/<id> is recorded."""

    def __init__(self, *job_statuses):
        self._job_statuses = [list(statuses) for statuses in job_statuses]
        self.submitted_payloads: list[dict] = []
        self.cancelled: list[str] = []

    def post(self, url, headers=None, json=None, timeout=None):
        if url.endswith("/run"):
            self.submitted_payloads.append(json)
            return FakeResponse(json_body={"id": f"job-{len(self.submitted_payloads) - 1}"})
        self.cancelled.append(url.rsplit("/", 1)[1])
        return FakeResponse(json_body={})

    def get(self, url, headers=None, timeout=None):
        statuses = self._job_statuses[int(url.rsplit("-", 1)[1])]
        return FakeResponse(json_body=statuses.pop(0) if len(statuses) > 1 else statuses[0])


def _completed(*segments):
    return {
        "status": "COMPLETED",
        "output": {
            "segments": [{"start": start, "speaker": speaker, "text": text} for start, speaker, text in segments]
        },
    }


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


def test_is_configured_true_from_explicit_args_with_no_env_vars_set(monkeypatch):
    monkeypatch.delenv("MEETING_SCRIBE_RUNPOD_API_KEY", raising=False)
    monkeypatch.delenv("MEETING_SCRIBE_RUNPOD_ENDPOINT_ID", raising=False)
    assert is_configured(api_key="key", endpoint_id="endpoint")


def test_is_configured_false_when_explicit_args_are_none_and_no_env_vars_set(monkeypatch):
    monkeypatch.delenv("MEETING_SCRIBE_RUNPOD_API_KEY", raising=False)
    monkeypatch.delenv("MEETING_SCRIBE_RUNPOD_ENDPOINT_ID", raising=False)
    assert not is_configured(api_key=None, endpoint_id=None)


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



def test_transcribe_parts_sends_compressed_audio_and_parses_the_result(tmp_path, monkeypatch):
    _env(monkeypatch, hf_token="hf_abc")
    audio = tmp_path / "system.wav"
    _write_wav(audio, seconds=5.0)
    runpod = FakeRunpod([{"status": "IN_QUEUE"}, _completed((1.5, "SPEAKER_00", "hi"))])
    transcriber = RunpodWhisperXTranscriber(session=runpod, poll_seconds=0)

    lines = transcriber.transcribe_parts([audio], source="system")

    assert [(line.timestamp_seconds, line.source, line.speaker, line.text) for line in lines] == [
        (1.5, "system", "SPEAKER_00", "hi")
    ]
    (payload,) = runpod.submitted_payloads
    assert payload["input"]["huggingface_access_token"] == "hf_abc"
    audio_file = payload["input"]["audio_file"]
    assert audio_file.startswith("data:audio/ogg;base64,")
    sent = base64.b64decode(audio_file.split(",", 1)[1])
    assert sent[:4] == b"OggS"
    # Compressed far below the WAV it came from (5 s of 48 kHz stereo is ~940 KB).
    assert len(sent) < audio.stat().st_size / 20


def test_a_long_recording_is_split_into_chunks_placed_on_the_meetings_clock(tmp_path, monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(runpod_whisperx, "_MAX_CHUNK_SECONDS", 1.0)
    audio = tmp_path / "system.wav"
    _write_wav(audio, seconds=2.5)
    runpod = FakeRunpod(
        [_completed((0.2, "SPEAKER_00", "one"))],
        [_completed((0.3, "SPEAKER_00", "two"))],
        [_completed((0.4, "SPEAKER_01", "three"))],
    )
    transcriber = RunpodWhisperXTranscriber(session=runpod, poll_seconds=0)

    lines = transcriber.transcribe_parts([audio], source="system")

    assert len(runpod.submitted_payloads) == 3
    # Each chunk is diarized separately, so its labels are kept apart rather than merged as if the same
    # SPEAKER_00 in every chunk were one person.
    assert [(line.timestamp_seconds, line.speaker, line.text) for line in lines] == [
        (0.2, "SPEAKER_00 (part 1)", "one"),
        (1.3, "SPEAKER_00 (part 2)", "two"),
        (2.4, "SPEAKER_01 (part 3)", "three"),
    ]


def test_a_second_wav_part_is_offset_by_the_first_parts_duration(tmp_path, monkeypatch):
    _env(monkeypatch)
    first, second = tmp_path / "system.wav", tmp_path / "system.part2.wav"
    _write_wav(first, seconds=6.0)
    _write_wav(second, seconds=2.0)
    runpod = FakeRunpod(
        [_completed((2.0, "SPEAKER_00", "first part"))],
        [_completed((1.0, "SPEAKER_01", "second part"))],
    )
    transcriber = RunpodWhisperXTranscriber(session=runpod, poll_seconds=0)

    lines = transcriber.transcribe_parts([first, second], source="system")

    assert [(line.timestamp_seconds, line.text) for line in lines] == [(2.0, "first part"), (7.0, "second part")]


def test_progress_is_reported_at_each_step(tmp_path, monkeypatch):
    _env(monkeypatch)
    audio = tmp_path / "system.wav"
    _write_wav(audio, seconds=3.0)
    runpod = FakeRunpod([_completed((0.0, "SPEAKER_00", "hi"), (1.0, "SPEAKER_01", "hello"))])
    messages = []
    transcriber = RunpodWhisperXTranscriber(session=runpod, poll_seconds=0, on_progress=messages.append)

    transcriber.transcribe_parts([audio], source="system")

    assert messages[0] == "Compressing system audio for Runpod…"
    assert messages[1].startswith("Sending system audio to Runpod (")
    assert "3 s" in messages[1]
    assert "KB" in messages[1]  # a few seconds of audio is well under a megabyte
    assert messages[2:] == ["Runpod job queued.", "Diarized transcript received — 2 speakers."]


def test_a_failed_job_raises_and_cancels_the_others(tmp_path, monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(runpod_whisperx, "_MAX_CHUNK_SECONDS", 1.0)
    audio = tmp_path / "system.wav"
    _write_wav(audio, seconds=2.0)
    runpod = FakeRunpod(
        [{"status": "FAILED", "error": "out of memory"}],
        [{"status": "IN_PROGRESS"}],
    )
    transcriber = RunpodWhisperXTranscriber(session=runpod, poll_seconds=0)

    with pytest.raises(RunpodWhisperXError, match="out of memory"):
        transcriber.transcribe_parts([audio], source="system")
    assert "job-1" in runpod.cancelled


def test_an_error_returned_as_the_workers_output_raises(tmp_path, monkeypatch):
    # The worker reports bad input by returning {"error": ...} from a job Runpod marks COMPLETED.
    _env(monkeypatch)
    audio = tmp_path / "system.wav"
    _write_wav(audio, seconds=1.0)
    runpod = FakeRunpod([{"status": "COMPLETED", "output": {"error": "audio input: bad data"}}])
    transcriber = RunpodWhisperXTranscriber(session=runpod, poll_seconds=0)

    with pytest.raises(RunpodWhisperXError, match="bad data"):
        transcriber.transcribe_parts([audio], source="system")


def test_a_job_that_never_finishes_raises(tmp_path, monkeypatch):
    _env(monkeypatch)
    audio = tmp_path / "system.wav"
    _write_wav(audio, seconds=1.0)
    runpod = FakeRunpod([{"status": "IN_PROGRESS"}])
    transcriber = RunpodWhisperXTranscriber(session=runpod, poll_seconds=0, timeout_seconds=-1)

    with pytest.raises(RunpodWhisperXError, match="didn't finish"):
        transcriber.transcribe_parts([audio], source="system")
    assert runpod.cancelled == ["job-0"]


def test_a_network_error_becomes_a_runpod_error_so_the_caller_falls_back(tmp_path, monkeypatch):
    _env(monkeypatch)
    audio = tmp_path / "system.wav"
    _write_wav(audio, seconds=1.0)

    class Unreachable(FakeRunpod):
        def post(self, url, headers=None, json=None, timeout=None):
            raise ConnectionError("no route to host")

    transcriber = RunpodWhisperXTranscriber(session=Unreachable(), poll_seconds=0)

    with pytest.raises(RunpodWhisperXError, match="no route to host"):
        transcriber.transcribe_parts([audio], source="system")


def test_an_unexpected_response_shape_becomes_a_runpod_error(tmp_path, monkeypatch):
    _env(monkeypatch)
    audio = tmp_path / "system.wav"
    _write_wav(audio, seconds=1.0)
    runpod = FakeRunpod([{"status": "COMPLETED", "output": {"segments": [{"speaker": "SPEAKER_00", "text": "hi"}]}}])
    transcriber = RunpodWhisperXTranscriber(session=runpod, poll_seconds=0)

    with pytest.raises(RunpodWhisperXError):
        transcriber.transcribe_parts([audio], source="system")


def test_audio_still_too_large_after_compression_is_rejected_before_anything_is_sent(tmp_path, monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(runpod_whisperx, "_MAX_REQUEST_BYTES", 100)
    monkeypatch.setattr(runpod_whisperx, "_REQUEST_OVERHEAD_BYTES", 0)
    audio = tmp_path / "system.wav"
    _write_wav(audio, seconds=3.0)
    runpod = FakeRunpod([_completed()])
    transcriber = RunpodWhisperXTranscriber(session=runpod, poll_seconds=0)

    with pytest.raises(RunpodWhisperXError, match="even after compression"):
        transcriber.transcribe_parts([audio], source="system")
    assert runpod.submitted_payloads == []


def test_an_unreadable_wav_becomes_a_runpod_error(tmp_path, monkeypatch):
    pytest.importorskip("av")
    _env(monkeypatch)
    audio = tmp_path / "system.wav"
    audio.write_bytes(b"not audio at all")
    transcriber = RunpodWhisperXTranscriber(session=FakeRunpod(), poll_seconds=0)

    with pytest.raises(RunpodWhisperXError, match="couldn't compress"):
        transcriber.transcribe_parts([audio], source="system")


def test_no_audio_parts_means_no_jobs(monkeypatch):
    _env(monkeypatch)
    runpod = FakeRunpod()
    assert RunpodWhisperXTranscriber(session=runpod).transcribe_parts([], source="system") == []
    assert runpod.submitted_payloads == []


@pytest.fixture
def local_api(monkeypatch):
    """A real HTTP server on localhost standing in for Runpod, for exercising _HttpSession end to end.
    Returns its base URL and a list of (method, path, authorization, body) for each request received."""
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    received = []

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received.append(("POST", self.path, self.headers.get("Authorization"), body))
            self._reply(200, {"id": "job-0"} if self.path.endswith("/run") else {})

        def do_GET(self):
            received.append(("GET", self.path, self.headers.get("Authorization"), b""))
            if self.path.endswith("/missing"):
                self._reply(404, {"error": "no such job"})
            else:
                self._reply(200, {"status": "COMPLETED", "output": {"segments": [
                    {"start": 0.5, "speaker": "SPEAKER_00", "text": "over real HTTP"}
                ]}})

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", received
    server.shutdown()
    server.server_close()


def test_http_session_posts_json_and_reads_json_back(local_api):
    base, received = local_api

    response = _HttpSession().post(f"{base}/run", headers={"Authorization": "Bearer k"}, json={"a": 1}, timeout=5)

    assert response.status_code == 200
    assert response.json() == {"id": "job-0"}
    assert received == [("POST", "/run", "Bearer k", b'{"a": 1}')]


def test_http_session_returns_an_error_status_instead_of_raising(local_api):
    base, _ = local_api

    response = _HttpSession().get(f"{base}/status/missing", timeout=5)

    assert response.status_code == 404
    assert response.json() == {"error": "no such job"}


def test_http_session_post_without_a_body_sends_an_empty_one(local_api):
    base, received = local_api

    _HttpSession().post(f"{base}/cancel/job-0", timeout=5)

    assert received == [("POST", "/cancel/job-0", None, b"")]


def test_transcription_works_end_to_end_without_the_requests_package(tmp_path, monkeypatch, local_api):
    # The failure seen in the field: an install with no `requests` package. None in sys.modules makes
    # any `import requests` raise, so this proves nothing on the path still needs it.
    base, received = local_api
    monkeypatch.setitem(sys.modules, "requests", None)
    monkeypatch.setattr(RunpodWhisperXTranscriber, "_base_url", property(lambda self: base))
    audio = tmp_path / "system.wav"
    _write_wav(audio, seconds=1.0)

    transcriber = RunpodWhisperXTranscriber(api_key="k", endpoint_id="e", poll_seconds=0)
    lines = transcriber.transcribe_parts([audio], source="system")

    assert [(line.timestamp_seconds, line.speaker, line.text) for line in lines] == [
        (0.5, "SPEAKER_00", "over real HTTP")
    ]
    method, path, authorization, body = received[0]
    assert (method, path, authorization) == ("POST", "/run", "Bearer k")
    assert json.loads(body)["input"]["audio_file"].startswith("data:audio/ogg;base64,")
