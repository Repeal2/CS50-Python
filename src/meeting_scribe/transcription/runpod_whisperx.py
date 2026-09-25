"""Cloud transcription via a Runpod-hosted WhisperX serverless endpoint — speaker-diarized for the
system-audio track, plain for the microphone (only ever the meeting owner, so diarizing it can't identify
anyone new). Opt-in (see config.Settings.transcribe_in_cloud), since it sends recorded meeting audio to
two third parties (Runpod's GPU host, and HuggingFace's hosted pyannote diarization models) rather than
keeping everything on this machine the way transcription.engine.WhisperTranscriber does — see
session._transcribe for where this plugs in.

Needs a Runpod API key and the id of a deployed WhisperX-with-diarization serverless endpoint before it'll
do anything (see is_configured); a HuggingFace access token that has accepted pyannote's gated model terms
is also needed for diarization itself to work, though the underlying worker may fail with its own error
rather than this module catching that case specifically. These come from config.Settings
(runpod_api_key/runpod_endpoint_id/runpod_huggingface_token, entered on the Settings page and persisted to
settings.json like the app's other preferences) — the environment variables below are a fallback for
anyone who'd rather set them that way instead (or not set a HuggingFace token here at all, if HF_TOKEN was
set directly as an environment variable on the Runpod endpoint itself).

_build_payload and _parse_segments are written against kodxana/whisperx-worker_v2's verified schema
(github.com/kodxana/whisperx-worker_v2, checked directly against its rp_handler.py/rp_schema.py, not just
its README) — the actively-maintained successor to kodxana/whisperx-worker, which its own README says has
moved and is now archived. Deploying a different worker image means checking its actual input/output shape
and adjusting those two functions to match; don't assume another template uses the same field names.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from meeting_scribe.transcription.engine import TranscriptLine, part_start_offsets
from meeting_scribe.transcription.speech_encoding import EncodedChunk, encode_speech_chunks

_API_KEY_ENV = "MEETING_SCRIBE_RUNPOD_API_KEY"
_ENDPOINT_ID_ENV = "MEETING_SCRIBE_RUNPOD_ENDPOINT_ID"
_HF_TOKEN_ENV = "MEETING_SCRIBE_RUNPOD_HF_TOKEN"

# Runpod caps one /run request body at 10 MB (kodxana/whisperx-worker_v2's README states this directly;
# /runsync allows 20 MB, but this module doesn't use it). Audio goes inside the JSON body base64-encoded,
# which inflates it by a third, so it's the encoded size that's checked against this, less some room for
# the rest of the body.
_MAX_REQUEST_BYTES = 10 * 1024 * 1024
_REQUEST_OVERHEAD_BYTES = 16 * 1024
# 45 minutes of 16 kbps Opus (see speech_encoding) is about 5 MB, ~7 MB once base64-encoded — a wide margin
# under _MAX_REQUEST_BYTES even if a noisy recording makes Opus spend more than its target bitrate.
_MAX_CHUNK_SECONDS = 45 * 60
# Each chunk after the first repeats this much of the end of the one before it. Every chunk is diarized on
# its own, so "SPEAKER_00" in one needn't be "SPEAKER_00" in the next; whoever is talking in the stretch
# both chunks heard is the same person, which is what links the labels up (see _link_chunk_speakers).
# Two minutes is enough for a normal conversation to have more than one voice in it.
_CHUNK_OVERLAP_SECONDS = 120.0
# How many seconds of speech two labels must share in an overlap before they're taken to be one person.
_MIN_SHARED_SPEECH_SECONDS = 2.0


_TRACK_NAMES = {"system": "system audio", "mic": "microphone audio"}


class RunpodWhisperXError(Exception):
    """Raised for anything that keeps a diarized transcript from coming back: missing configuration, a
    failed/timed-out Runpod job, or audio that's still too large to send after compression. Always safe
    for a caller to catch and fall back to local (undiarized) transcription instead — see
    session._transcribe."""


def is_configured(*, api_key: str | None = None, endpoint_id: str | None = None) -> bool:
    """Whether enough is set — explicitly (e.g. from Settings) or via the environment variable fallback —
    to even attempt a cloud diarization call. Checked up front so a meeting that opted into cloud
    diarization without finishing setup fails with one clear message instead of partway through a job
    submission."""
    return bool(api_key or os.environ.get(_API_KEY_ENV)) and bool(
        endpoint_id or os.environ.get(_ENDPOINT_ID_ENV)
    )


@dataclass(frozen=True)
class _DiarizedSegment:
    start_seconds: float
    speaker: str
    text: str
    end_seconds: float | None = None


class _HttpResponse:
    def __init__(self, status_code: int, body: bytes):
        self.status_code = status_code
        self.text = body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text)


class _HttpSession:
    """The two kinds of call this module makes, over the standard library rather than the `requests`
    package — so there's nothing extra to install or bundle into the .exe. On Windows this also means
    certificates are checked against the Windows certificate store, which on a corporate network includes
    any root certificate IT has added for inspecting HTTPS traffic; `requests` uses its own bundled list
    instead and would reject those connections. Proxy settings come from the environment or, on Windows,
    the system's Internet settings. An HTTP error status is returned as a response, like `requests` does,
    rather than raised."""

    def post(self, url: str, headers: dict | None = None, json: dict | None = None, timeout: float | None = None):
        body = b"" if json is None else _json_dumps(json).encode("utf-8")
        return self._send(urllib.request.Request(url, data=body, headers=headers or {}, method="POST"), timeout)

    def get(self, url: str, headers: dict | None = None, timeout: float | None = None):
        return self._send(urllib.request.Request(url, headers=headers or {}, method="GET"), timeout)

    @staticmethod
    def _send(request: urllib.request.Request, timeout: float | None) -> _HttpResponse:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return _HttpResponse(response.status, response.read())
        except urllib.error.HTTPError as error:
            return _HttpResponse(error.code, error.read())


_json_dumps = json.dumps  # _HttpSession.post's `json` parameter (named to match requests) shadows the module


class RunpodWhisperXTranscriber:
    """Sends one audio track to a Runpod WhisperX serverless endpoint for transcription with speaker
    diarization. Mirrors WhisperTranscriber's transcribe_parts(audio_paths, source) shape so session.py
    can use either one interchangeably for the system track.

    The audio is compressed and split into chunks first (see speech_encoding), sent inline as base64 —
    one Runpod job per chunk. `on_progress`, if given, gets a short status line at each step, for the
    activity log. `session` is injectable (anything with _HttpSession's get/post) so tests don't make real
    network calls; it defaults to _HttpSession.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint_id: str | None = None,
        huggingface_token: str | None = None,
        on_progress: Callable[[str], None] | None = None,
        session=None,
        poll_seconds: float = 5.0,
        timeout_seconds: float = 1800.0,
    ):
        self._api_key = api_key or os.environ.get(_API_KEY_ENV)
        self._endpoint_id = endpoint_id or os.environ.get(_ENDPOINT_ID_ENV)
        self._huggingface_token = huggingface_token or os.environ.get(_HF_TOKEN_ENV)
        self._on_progress = on_progress
        self._session = session if session is not None else _HttpSession()
        self._poll_seconds = poll_seconds
        self._timeout_seconds = timeout_seconds
        if not self._api_key or not self._endpoint_id:
            raise RunpodWhisperXError(
                f"Cloud speaker diarization is opted into but not configured — set {_API_KEY_ENV} and "
                f"{_ENDPOINT_ID_ENV} (and usually {_HF_TOKEN_ENV}, for diarization itself to work)."
            )

    @property
    def _base_url(self) -> str:
        return f"https://api.runpod.ai/v2/{self._endpoint_id}"

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    def _report(self, message: str) -> None:
        if self._on_progress is not None:
            self._on_progress(message)

    def transcribe_parts(
        self,
        audio_paths: Sequence[Path],
        source: str,
        start_offsets: Mapping[Path, float] | None = None,
        *,
        diarize: bool = True,
    ) -> list[TranscriptLine]:
        """Transcribes one capture stream that may have been written as several WAV parts, each placed on
        the meeting's clock the same way WhisperTranscriber.transcribe_parts does (see
        engine.part_start_offsets). Each part is further split into chunks of _MAX_CHUNK_SECONDS, one
        Runpod job each, neighbouring chunks overlapping by _CHUNK_OVERLAP_SECONDS.

        Each job is diarized on its own, so a chunk's speaker labels are matched to the previous chunk's
        by who was speaking in the overlap (see _link_chunk_speakers). A speaker that can't be matched —
        someone who joined partway through, or anyone in a WAV part after a device change, which shares
        no audio with the part before — is suffixed with the chunk number ("SPEAKER_02 (part 2)") rather
        than presented as someone already heard.

        `diarize=False` transcribes without labelling speakers — for the microphone track, which is only
        ever the meeting owner — and its lines carry no speaker."""
        if not audio_paths:
            return []
        track = _TRACK_NAMES.get(source, source)
        # Takes about half a minute for an hour of audio, so say so rather than the log going quiet.
        self._report(f"Compressing {track} for Runpod…")
        chunks = self._encode(audio_paths, start_offsets)
        if not chunks:
            return []

        total_bytes = sum(len(chunk.data) for _, chunk in chunks)
        total_seconds = sum(chunk.duration_seconds - chunk.overlap_seconds for _, chunk in chunks)
        split_note = f", in {len(chunks)} parts" if len(chunks) > 1 else ""
        self._report(
            f"Sending {track} to Runpod ({_format_size(total_bytes)}, "
            f"{_format_duration(total_seconds)}{split_note})…"
        )
        job_ids: list[str] = []
        try:
            for _offset, chunk in chunks:
                job_ids.append(self._submit_job(_data_uri(chunk), diarize=diarize))
            self._report("Runpod job queued." if len(job_ids) == 1 else f"{len(job_ids)} Runpod jobs queued.")
            outputs = self._wait_for_results(job_ids)
            parsed = [_parse_segments(output) for output in outputs]
        except Exception as error:
            # Nothing will use the rest of the results now — stop paying for them.
            self._cancel_quietly(job_ids)
            if isinstance(error, RunpodWhisperXError):
                raise
            # A dropped connection, a certificate rejected by a corporate proxy, a response in an unexpected shape:
            # whatever it is, it has to surface as RunpodWhisperXError so the caller falls back to local
            # transcription instead of the meeting losing its transcript over it.
            raise RunpodWhisperXError(f"couldn't get a transcript from Runpod: {error!r}") from error

        placed = [
            [_replace_times(segment, offset_seconds) for segment in segments]
            for (offset_seconds, _chunk), segments in zip(chunks, parsed)
        ]
        if len(chunks) > 1:
            placed = _link_chunk_speakers([(offset, chunk.overlap_seconds) for offset, chunk in chunks], placed)
        lines = [
            TranscriptLine(
                timestamp_seconds=segment.start_seconds,
                source=source,
                text=segment.text,
                speaker=segment.speaker if diarize else None,
            )
            for segments in placed
            for segment in segments
        ]
        speakers = {line.speaker for line in lines}
        if not diarize:
            self._report(f"Cloud transcript of the {track} received.")
        elif len(chunks) == 1:
            self._report(f"Diarized transcript received — {_count(len(speakers), 'speaker')}.")
        else:
            self._report(
                f"Diarized transcript received — {len(chunks)} parts, {_count(len(speakers), 'speaker label')} "
                "after matching speakers across parts."
            )
        return lines

    def _encode(
        self, audio_paths: Sequence[Path], start_offsets: Mapping[Path, float] | None = None
    ) -> list[tuple[float, EncodedChunk]]:
        """Every chunk of every part, paired with where it starts on the meeting's clock. Checked against
        the request size limit before anything is sent, so a too-large chunk never leaves some jobs
        submitted and others not."""
        chunks: list[tuple[float, EncodedChunk]] = []
        for audio_path, part_offset in zip(audio_paths, part_start_offsets(audio_paths, start_offsets)):
            try:
                encoded = encode_speech_chunks(
                    audio_path, max_chunk_seconds=_MAX_CHUNK_SECONDS, overlap_seconds=_CHUNK_OVERLAP_SECONDS
                )
            except Exception as error:  # an unreadable/corrupt WAV part
                raise RunpodWhisperXError(f"couldn't compress {audio_path.name} for sending: {error}") from error
            for chunk in encoded:
                chunks.append((part_offset + chunk.start_seconds, chunk))

        limit = _MAX_REQUEST_BYTES - _REQUEST_OVERHEAD_BYTES
        for number, (_offset, chunk) in enumerate(chunks, start=1):
            encoded_size = _base64_length(len(chunk.data))
            if encoded_size > limit:
                raise RunpodWhisperXError(
                    f"part {number} is {encoded_size / 1_048_576:.1f} MB even after compression, over "
                    f"Runpod's {_MAX_REQUEST_BYTES // 1_048_576} MB request limit"
                )
        return chunks

    def _submit_job(self, audio_url: str, *, diarize: bool = True) -> str:
        payload = _build_payload(audio_url, huggingface_token=self._huggingface_token, diarize=diarize)
        response = self._session.post(
            f"{self._base_url}/run", headers=self._headers, json=payload, timeout=60
        )
        _raise_for_bad_response(response)
        return response.json()["id"]

    def _wait_for_results(self, job_ids: list[str]) -> list:
        """Polls every job until all have finished, returning their outputs in the same order. All are
        polled each round rather than one at a time, so a chunk that finishes early isn't left waiting —
        Runpod only keeps a finished job's result for a limited time."""
        deadline = time.monotonic() + self._timeout_seconds
        outputs: dict[str, object] = {}
        while True:
            for job_id in job_ids:
                if job_id in outputs:
                    continue
                response = self._session.get(
                    f"{self._base_url}/status/{job_id}", headers=self._headers, timeout=30
                )
                _raise_for_bad_response(response)
                body = response.json()
                status = body.get("status")
                if status == "COMPLETED":
                    output = body.get("output")
                    # The worker reports bad input by returning {"error": ...} as a completed job's
                    # output, not by failing the job.
                    if isinstance(output, dict) and output.get("error"):
                        raise RunpodWhisperXError(f"Runpod job {job_id} failed: {output['error']}")
                    outputs[job_id] = output
                    if len(job_ids) > 1:
                        self._report(f"Runpod finished part {job_ids.index(job_id) + 1} of {len(job_ids)}.")
                elif status in ("FAILED", "CANCELLED", "TIMED_OUT"):
                    raise RunpodWhisperXError(f"Runpod job {job_id} ended as {status}: {body.get('error')}")
            if len(outputs) == len(job_ids):
                return [outputs[job_id] for job_id in job_ids]
            if time.monotonic() >= deadline:
                raise RunpodWhisperXError(f"Runpod didn't finish within {self._timeout_seconds:.0f}s")
            time.sleep(self._poll_seconds)

    def _cancel_quietly(self, job_ids: list[str]) -> None:
        for job_id in job_ids:
            try:
                self._session.post(f"{self._base_url}/cancel/{job_id}", headers=self._headers, timeout=15)
            except Exception:
                pass  # best effort — the failure that got us here is the one worth reporting


def _data_uri(chunk: EncodedChunk) -> str:
    return f"data:{chunk.mime_type};base64,{base64.b64encode(chunk.data).decode('ascii')}"


def _base64_length(byte_count: int) -> int:
    return 4 * ((byte_count + 2) // 3)


def _format_size(byte_count: int) -> str:
    if byte_count < 1_048_576:
        return f"{max(1, round(byte_count / 1024))} KB"
    return f"{byte_count / 1_048_576:.1f} MB"


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{round(seconds)} s"
    return f"{round(seconds / 60)} min"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _raise_for_bad_response(response) -> None:
    if response.status_code >= 400:
        raise RunpodWhisperXError(f"Runpod API call failed ({response.status_code}): {response.text[:500]}")


def _build_payload(audio_url: str, *, huggingface_token: str | None, diarize: bool = True) -> dict:
    """kodxana/whisperx-worker_v2's input schema (verified against its rp_schema.py, not just its README):
    `audio_file` (a URL or base64 audio, not `audio`), `diarization`, and an optional
    `huggingface_access_token` that overrides its endpoint-side `HF_TOKEN` env var when given. Its schema
    validator rejects any key it doesn't recognize — there's no `model` parameter to pick a Whisper size,
    so don't add one. Adjust this function if a different worker image ever replaces it."""
    payload = {"input": {"audio_file": audio_url, "diarization": diarize}}
    if huggingface_token:
        payload["input"]["huggingface_access_token"] = huggingface_token
    return payload


def _parse_segments(output: dict) -> list[_DiarizedSegment]:
    """The worker's segments, each split wherever its words change speaker. WhisperX cuts segments by
    pauses, not by who's talking, so one segment often holds a question and its answer; the segment's
    own speaker is only whoever said most of it, while every word carries its own (see
    whisperx.assign_word_speakers)."""
    segments: list[_DiarizedSegment] = []
    for segment in output.get("segments", []):
        if not segment.get("text", "").strip():
            continue
        segments.extend(_split_by_word_speaker(segment))
    return segments


def _split_by_word_speaker(segment: dict) -> list[_DiarizedSegment]:
    whole = _DiarizedSegment(
        start_seconds=segment["start"],
        speaker=segment.get("speaker", "UNKNOWN"),
        text=segment["text"].strip(),
        end_seconds=segment.get("end"),
    )
    words = [word for word in segment.get("words") or [] if str(word.get("word", "")).strip()]
    groups: list[tuple[str, list[dict]]] = []
    for word in words:
        speaker = word.get("speaker") or (groups[-1][0] if groups else whole.speaker)
        if groups and groups[-1][0] == speaker:
            groups[-1][1].append(word)
        else:
            groups.append((speaker, [word]))
    if len(groups) < 2:
        return [whole]

    pieces: list[_DiarizedSegment] = []
    for speaker, group in groups:
        starts = [word["start"] for word in group if isinstance(word.get("start"), (int, float))]
        ends = [word["end"] for word in group if isinstance(word.get("end"), (int, float))]
        # A word WhisperX couldn't align (a number, say) has no times; the piece before it ends there.
        start = starts[0] if starts else (pieces[-1].end_seconds or pieces[-1].start_seconds) if pieces else whole.start_seconds
        pieces.append(
            _DiarizedSegment(
                start_seconds=start,
                speaker=speaker,
                text=" ".join(str(word["word"]).strip() for word in group),
                end_seconds=ends[-1] if ends else None,
            )
        )
    return pieces


def _replace_times(segment: _DiarizedSegment, offset_seconds: float) -> _DiarizedSegment:
    """`segment` moved from its chunk's own clock onto the meeting's."""
    return _DiarizedSegment(
        start_seconds=segment.start_seconds + offset_seconds,
        speaker=segment.speaker,
        text=segment.text,
        end_seconds=None if segment.end_seconds is None else segment.end_seconds + offset_seconds,
    )


def _link_chunk_speakers(
    chunk_spans: Sequence[tuple[float, float]], chunks: list[list[_DiarizedSegment]]
) -> list[list[_DiarizedSegment]]:
    """Relabels every chunk's speakers so the same person keeps one label across chunks, and drops the
    lines two overlapping chunks both transcribed. `chunk_spans` is each chunk's (start on the meeting's
    clock, seconds of overlap with the previous chunk); segments are already on the meeting's clock.

    The first chunk's labels are kept as they are. Each later chunk that overlaps the one before it has
    its labels matched to that chunk's final ones by how much speech they share in the overlap, most
    first, one to one. Anything unmatched (or a chunk with no overlap: the first chunk of a later WAV
    part) is suffixed with the chunk number. Of the lines in an overlap, the earlier chunk keeps those
    starting in its first half and the later chunk those in its second, so a sentence cut off at the end
    of one chunk comes from the chunk that heard all of it."""
    linked: list[list[_DiarizedSegment]] = []
    for number, ((start, overlap), segments) in enumerate(zip(chunk_spans, chunks), start=1):
        if number == 1:
            linked.append(list(segments))
            continue
        mapping: dict[str, str] = {}
        if overlap > 0:
            window = (start, start + overlap)
            midpoint = start + overlap / 2
            mapping = _match_speakers(linked[-1], segments, window)
            linked[-1] = [segment for segment in linked[-1] if segment.start_seconds < midpoint]
            segments = [segment for segment in segments if segment.start_seconds >= midpoint]
        linked.append(
            [
                _DiarizedSegment(
                    start_seconds=segment.start_seconds,
                    speaker=mapping.get(segment.speaker, f"{segment.speaker} (part {number})"),
                    text=segment.text,
                    end_seconds=segment.end_seconds,
                )
                for segment in segments
            ]
        )
    return linked


def _match_speakers(
    earlier: Sequence[_DiarizedSegment], later: Sequence[_DiarizedSegment], window: tuple[float, float]
) -> dict[str, str]:
    """{later chunk's label: earlier chunk's label} for the speakers heard in both within `window`."""

    def spans(segments):
        for index, segment in enumerate(segments):
            end = segment.end_seconds
            if end is None:  # no end time: assume it runs to the next line, but not for ever
                following = segments[index + 1].start_seconds if index + 1 < len(segments) else segment.start_seconds + 5
                end = min(following, segment.start_seconds + 30)
            low, high = max(segment.start_seconds, window[0]), min(end, window[1])
            if high > low:
                yield segment.speaker, low, high

    earlier_spans = list(spans(earlier))
    shared: dict[tuple[str, str], float] = {}
    for later_speaker, low, high in spans(later):
        for earlier_speaker, other_low, other_high in earlier_spans:
            common = min(high, other_high) - max(low, other_low)
            if common > 0:
                key = (later_speaker, earlier_speaker)
                shared[key] = shared.get(key, 0.0) + common
    mapping: dict[str, str] = {}
    taken: set[str] = set()
    for (later_speaker, earlier_speaker), seconds in sorted(shared.items(), key=lambda item: -item[1]):
        if seconds < _MIN_SHARED_SPEECH_SECONDS:
            break
        if later_speaker in mapping or earlier_speaker in taken:
            continue
        mapping[later_speaker] = earlier_speaker
        taken.add(earlier_speaker)
    return mapping
