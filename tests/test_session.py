import json
from pathlib import Path
from unittest.mock import patch

import pytest

from meeting_scribe.audio.recorder import RecordedAudio
from meeting_scribe.config import Settings
from meeting_scribe.screen.capture import ScreenTextEvent
from meeting_scribe.storage.database import Database
from meeting_scribe.transcription.engine import TranscriptLine


def _settings(
    tmp_path: Path,
    mic_device_name=None,
    system_device_name=None,
    copilot_sync_dir=None,
    transcribe_in_cloud=False,
    transcribe_locally=True,
    runpod_api_key=None,
    runpod_endpoint_id=None,
    runpod_huggingface_token=None,
) -> Settings:
    return Settings(
        data_dir=tmp_path,
        whisper_model_size="tiny",
        tesseract_cmd=None,
        screen_capture_interval_seconds=3.0,
        mic_device_name=mic_device_name,
        system_device_name=system_device_name,
        copilot_sync_dir=copilot_sync_dir,
        transcribe_in_cloud=transcribe_in_cloud,
        transcribe_locally=transcribe_locally,
        runpod_api_key=runpod_api_key,
        runpod_endpoint_id=runpod_endpoint_id,
        runpod_huggingface_token=runpod_huggingface_token,
    )


@pytest.fixture(autouse=True)
def _plenty_of_memory(monkeypatch):
    """Keeps transcription.engine's low-memory warning out of tests that aren't specifically exercising
    it. Without this, exact on_progress message-list assertions below would be at the mercy of how much
    RAM happens to be free on whatever machine actually runs the suite; tests that want to exercise the
    warning override this with a low value of their own."""
    monkeypatch.setattr("meeting_scribe.session.available_memory_mb", lambda: 1_000_000.0)


@pytest.fixture(autouse=True)
def _recordings_have_audio(monkeypatch):
    """Most tests here name WAV paths that don't exist (the transcriber is mocked, so nothing reads
    them). Tests of what happens to an empty or missing track use `_real_durations` to undo this."""
    monkeypatch.setattr("meeting_scribe.session.wav_duration_seconds", lambda path: 60.0)


@pytest.fixture(autouse=True)
def _no_meetings_in_progress():
    """Every test's database numbers its meetings from 1, so a session one test leaves unstopped would
    otherwise look like a meeting still in progress to the next."""
    from meeting_scribe import session

    session._meetings_in_progress.clear()
    yield
    session._meetings_in_progress.clear()


@pytest.fixture
def _real_durations(monkeypatch):
    from meeting_scribe.transcription.engine import wav_duration_seconds

    monkeypatch.setattr("meeting_scribe.session.wav_duration_seconds", wav_duration_seconds)


def test_session_merges_audio_and_screen_into_saved_transcript(tmp_path):
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher") as MockScreenWatcher,
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
    ):
        recorder_instance = MockRecorder.return_value
        recorder_instance.stop.return_value = RecordedAudio(
            mic_paths=(tmp_path / "mic.wav",),
            system_paths=(tmp_path / "system.wav",),
            started_at_monotonic=0.0,
        )

        screen_watcher_instance = MockScreenWatcher.return_value

        transcriber_instance = MockTranscriber.return_value

        def fake_transcribe(paths, source, start_offsets=None):
            if source == "mic":
                return [TranscriptLine(1.0, "mic", "let's get started")]
            return [TranscriptLine(2.0, "system", "sounds good")]

        transcriber_instance.transcribe_parts.side_effect = fake_transcribe

        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(_settings(tmp_path), db, "Test Project", "Kickoff")

            # Simulate the screen watcher having captured one slide during the meeting.
            on_text = MockScreenWatcher.call_args.kwargs["on_text"]
            on_text(ScreenTextEvent(timestamp_seconds=0.5, text="Slide: Agenda"))

            session.start()
            recorder_instance.start.assert_called_once()
            screen_watcher_instance.start.assert_called_once()

            result = session.stop()

            screen_watcher_instance.stop.assert_called_once()
            recorder_instance.stop.assert_called_once()

            assert "Slide: Agenda" in result
            assert "let's get started" in result
            assert "sounds good" in result

            meeting = db.get_meeting(session.meeting_id)
            assert meeting.transcript_text == result
            segments = db.get_segments(session.meeting_id)
            assert len(segments) == 3


def test_cloud_only_sends_both_tracks_to_runpod_and_nothing_is_transcribed_here(tmp_path):
    """The system track goes with its speakers labelled; the mic track (only ever the meeting owner)
    without."""
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
        patch("meeting_scribe.transcription.runpod_whisperx.RunpodWhisperXTranscriber") as MockRunpod,
    ):
        recorder_instance = MockRecorder.return_value
        recorder_instance.stop.return_value = RecordedAudio(
            mic_paths=(tmp_path / "mic.wav",),
            system_paths=(tmp_path / "system.wav",),
            started_at_monotonic=0.0,
        )
        MockRunpod.return_value.transcribe_parts.side_effect = lambda paths, source, **kwargs: (
            [TranscriptLine(1.0, "mic", "let's get started")]
            if source == "mic"
            else [TranscriptLine(2.0, "system", "sounds good", speaker="SPEAKER_00")]
        )

        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(
                _settings(
                    tmp_path,
                    transcribe_in_cloud=True,
                    transcribe_locally=False,
                    runpod_api_key="rp-key",
                    runpod_endpoint_id="rp-endpoint",
                    runpod_huggingface_token="hf-token",
                ),
                db,
                "Test Project",
                "Kickoff",
            )
            session.start()
            progress = []
            result = session.stop(on_progress=progress.append)
            saved = sorted(
                (row["engine"], row["source"], row["speaker"], row["text"])
                for row in db.get_segments(session.meeting_id)
            )

        assert saved == [
            ("runpod", "mic", None, "let's get started"),
            ("runpod", "system", "SPEAKER_00", "sounds good"),
        ]
        # Credentials come from Settings, not environment variables.
        kwargs = MockRunpod.call_args.kwargs
        assert (kwargs["api_key"], kwargs["endpoint_id"], kwargs["huggingface_token"]) == (
            "rp-key",
            "rp-endpoint",
            "hf-token",
        )
        # Its progress lines go to the same place as the rest of stop()'s — the activity log.
        kwargs["on_progress"]("Runpod job queued.")
        assert progress[-1] == "Runpod job queued."
        assert MockRunpod.return_value.transcribe_parts.call_args_list == [
            ((recorder_instance.stop.return_value.system_paths,), {"source": "system", "start_offsets": {}}),
            ((recorder_instance.stop.return_value.mic_paths,), {"source": "mic", "start_offsets": {}, "diarize": False}),
        ]
        MockTranscriber.return_value.transcribe_parts.assert_not_called()
        assert "You: let's get started" in result
        assert "SPEAKER_00: sounds good" in result


def test_with_both_chosen_the_meeting_keeps_both_transcripts(tmp_path):
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
        patch("meeting_scribe.transcription.runpod_whisperx.RunpodWhisperXTranscriber") as MockRunpod,
    ):
        MockRecorder.return_value.stop.return_value = RecordedAudio(
            mic_paths=(tmp_path / "mic.wav",), system_paths=(tmp_path / "system.wav",), started_at_monotonic=0.0
        )
        MockTranscriber.return_value.transcribe_parts.side_effect = lambda paths, source, start_offsets=None: [
            TranscriptLine(1.0, source, f"local {source}")
        ]
        MockRunpod.return_value.transcribe_parts.side_effect = lambda paths, source, **kwargs: [
            TranscriptLine(1.0, source, f"cloud {source}", speaker="SPEAKER_00" if source == "system" else None)
        ]

        from meeting_scribe.session import MeetingSession, meeting_transcripts

        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(
                _settings(tmp_path, transcribe_in_cloud=True, runpod_api_key="k", runpod_endpoint_id="e"),
                db, "Test Project", "Kickoff",
            )
            session.start()
            result = session.stop()
            transcripts = meeting_transcripts(db.get_segments(session.meeting_id))

    assert [line.text for line in transcripts.local] == ["local mic", "local system"]
    assert [line.text for line in transcripts.cloud] == ["cloud mic", "cloud system"]
    # Where only one fits — the saved transcript, the Copilot push — the cloud's, which names speakers.
    assert "SPEAKER_00: cloud system" in result and "local system" not in result


def test_a_cloud_failure_with_both_chosen_keeps_this_pcs_transcript(tmp_path):
    from meeting_scribe.transcription.runpod_whisperx import RunpodWhisperXError

    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
        patch(
            "meeting_scribe.transcription.runpod_whisperx.RunpodWhisperXTranscriber",
            side_effect=RunpodWhisperXError("not configured"),
        ),
    ):
        MockRecorder.return_value.stop.return_value = RecordedAudio(
            mic_paths=(tmp_path / "mic.wav",), system_paths=(tmp_path / "system.wav",), started_at_monotonic=0.0
        )
        MockTranscriber.return_value.transcribe_parts.side_effect = lambda paths, source, start_offsets=None: [
            TranscriptLine(1.0, source, f"local {source}")
        ]

        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(_settings(tmp_path, transcribe_in_cloud=True), db, "Test Project", "Kickoff")
            session.start()
            progress = []
            result = session.stop(on_progress=progress.append)

    assert "local system" in result
    assert any("keeping this PC's transcript only" in line for line in progress)
    assert MockTranscriber.return_value.transcribe_parts.call_count == 2  # once per track, not twice


def test_the_transcription_choice_in_force_when_stop_is_pressed_is_the_one_used(tmp_path):
    # The field case: the cloud was unticked in Settings mid-meeting, and the meeting still went to the
    # cloud — the session had kept the Settings it started with.
    from dataclasses import replace

    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
        patch("meeting_scribe.transcription.runpod_whisperx.RunpodWhisperXTranscriber") as MockRunpod,
    ):
        MockRecorder.return_value.stop.return_value = RecordedAudio(
            mic_paths=(tmp_path / "mic.wav",), system_paths=(tmp_path / "system.wav",), started_at_monotonic=0.0
        )
        MockTranscriber.return_value.transcribe_parts.return_value = []

        from meeting_scribe.session import MeetingSession

        started_with = _settings(
            tmp_path, transcribe_in_cloud=True, transcribe_locally=False, runpod_api_key="k", runpod_endpoint_id="e"
        )
        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(started_with, db, "Test Project", "Kickoff")
            session.start()
            session.stop(settings=replace(started_with, transcribe_in_cloud=False, transcribe_locally=True))

    MockRunpod.assert_not_called()
    assert MockTranscriber.return_value.transcribe_parts.call_count == 2


def test_session_falls_back_to_local_transcription_when_cloud_diarization_fails(tmp_path):
    from meeting_scribe.transcription.runpod_whisperx import RunpodWhisperXError

    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
        patch(
            "meeting_scribe.transcription.runpod_whisperx.RunpodWhisperXTranscriber",
            side_effect=RunpodWhisperXError("not configured"),
        ),
    ):
        recorder_instance = MockRecorder.return_value
        recorder_instance.stop.return_value = RecordedAudio(
            mic_paths=(tmp_path / "mic.wav",),
            system_paths=(tmp_path / "system.wav",),
            started_at_monotonic=0.0,
        )

        def fake_transcribe(paths, source, start_offsets=None):
            if source == "mic":
                return [TranscriptLine(1.0, "mic", "let's get started")]
            return [TranscriptLine(2.0, "system", "sounds good")]

        MockTranscriber.return_value.transcribe_parts.side_effect = fake_transcribe

        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(_settings(tmp_path, transcribe_in_cloud=True, transcribe_locally=False), db, "Test Project", "Kickoff")
            session.start()

            progress_messages = []
            result = session.stop(on_progress=progress_messages.append)

        assert any("transcribing on this PC instead" in message for message in progress_messages)
        assert "Others: sounds good" in result  # local transcriber's flat "Others" label, not a speaker id
        # Local transcription still ran for both tracks despite the cloud attempt failing.
        assert MockTranscriber.return_value.transcribe_parts.call_count == 2


def test_session_passes_chosen_devices_to_recorder(tmp_path):
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber"),
    ):
        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            settings = _settings(tmp_path, mic_device_name="USB Mic", system_device_name="Speakers")
            MeetingSession(settings, db, "Test Project", "Kickoff")

            _args, kwargs = MockRecorder.call_args
            assert kwargs["mic_device_name"] == "USB Mic"
            assert kwargs["system_device_name"] == "Speakers"


def test_session_audio_levels_reads_through_to_recorder(tmp_path):
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber"),
    ):
        from meeting_scribe.session import MeetingSession

        recorder_instance = MockRecorder.return_value
        recorder_instance.mic_level = 0.42
        recorder_instance.system_level = 0.13

        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(_settings(tmp_path), db, "Test Project", "Kickoff")
            assert session.audio_levels() == (0.42, 0.13)


def test_session_pushes_a_named_file_package_when_sync_dir_configured(tmp_path):
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
    ):
        recorder_instance = MockRecorder.return_value
        recorder_instance.stop.return_value = RecordedAudio(
            mic_paths=(tmp_path / "mic.wav",),
            system_paths=(tmp_path / "system.wav",),
            started_at_monotonic=0.0,
        )
        MockTranscriber.return_value.transcribe_parts.return_value = [TranscriptLine(1.0, "mic", "hello")]

        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            settings = _settings(tmp_path, copilot_sync_dir=tmp_path / "Bridge")
            session = MeetingSession(settings, db, "Test Project", "Kickoff")
            session.start()

            result = session.stop()

            assert result == db.get_meeting(session.meeting_id).transcript_text
            inbox_dir = tmp_path / "Bridge" / "Inbox"
            code = session.meeting_code
            audio_transcript = (inbox_dir / f"{code}_Kickoff_transcript-audio.txt").read_text(
                encoding="utf-8"
            )
            screen_transcript = (inbox_dir / f"{code}_Kickoff_transcript-screen.txt").read_text(
                encoding="utf-8"
            )
            assert "hello" in audio_transcript
            assert "no on-screen text" in screen_transcript
            manifest = json.loads((inbox_dir / f"{code}_done.json").read_text(encoding="utf-8"))
            assert manifest["meetingID"] == code
            assert manifest["projectName"] == "Test Project"
            assert manifest["meetingTitle"] == "Kickoff"
            assert manifest["files"]["reference_docs"] == []


def test_session_pushes_a_meetings_reference_documents_under_their_original_filename(tmp_path):
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
    ):
        recorder_instance = MockRecorder.return_value
        recorder_instance.stop.return_value = RecordedAudio(
            mic_paths=(tmp_path / "mic.wav",),
            system_paths=(tmp_path / "system.wav",),
            started_at_monotonic=0.0,
        )
        MockTranscriber.return_value.transcribe_parts.return_value = [TranscriptLine(1.0, "mic", "hello")]

        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            settings = _settings(tmp_path, copilot_sync_dir=tmp_path / "Bridge")
            session = MeetingSession(settings, db, "Test Project", "Kickoff")
            session.start()

            original = tmp_path / "invite.pdf"
            original.write_bytes(b"invite bytes")
            db.add_document(
                session.project.id,
                "invite.pdf",
                "invite text",
                meeting_id=session.meeting_id,
                source_path=str(original),
            )

            session.stop()

            inbox_dir = tmp_path / "Bridge" / "Inbox"
            saved = inbox_dir / f"{session.meeting_code}_invite.pdf"
            assert saved.read_bytes() == b"invite bytes"
            manifest = json.loads((inbox_dir / f"{session.meeting_code}_done.json").read_text())
            assert manifest["reference_count"] == 1
            assert manifest["files"]["reference_docs"] == [
                {"original_filename": "invite.pdf", "saved_filename": f"{session.meeting_code}_invite.pdf"}
            ]


def test_session_pushes_manual_notes_and_attendees_as_reference_docs(tmp_path):
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
    ):
        recorder_instance = MockRecorder.return_value
        recorder_instance.stop.return_value = RecordedAudio(
            mic_paths=(tmp_path / "mic.wav",),
            system_paths=(tmp_path / "system.wav",),
            started_at_monotonic=0.0,
        )
        MockTranscriber.return_value.transcribe_parts.return_value = [TranscriptLine(1.0, "mic", "hello")]

        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            settings = _settings(tmp_path, copilot_sync_dir=tmp_path / "Bridge")
            session = MeetingSession(settings, db, "Test Project", "Kickoff")
            session.start()
            db.set_manual_notes(session.meeting_id, "Follow up with legal.")
            db.set_attendees(session.meeting_id, "John Smith\nJane Doe")

            session.stop()

            inbox_dir = tmp_path / "Bridge" / "Inbox"
            code = session.meeting_code
            notes = (inbox_dir / f"{code}_Kickoff_meeting-notes.txt").read_text(encoding="utf-8")
            attendees = (inbox_dir / f"{code}_Kickoff_attendees.txt").read_text(encoding="utf-8")
            assert notes == "Follow up with legal."
            assert attendees == "John Smith\nJane Doe"

            manifest = json.loads((inbox_dir / f"{code}_done.json").read_text(encoding="utf-8"))
            assert manifest["reference_count"] == 2
            assert manifest["files"]["reference_docs"] == [
                {
                    "original_filename": "meeting-notes.txt",
                    "saved_filename": f"{code}_Kickoff_meeting-notes.txt",
                },
                {"original_filename": "attendees.txt", "saved_filename": f"{code}_Kickoff_attendees.txt"},
            ]


def test_session_omits_manual_notes_and_attendees_from_the_push_when_neither_was_recorded(tmp_path):
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
    ):
        recorder_instance = MockRecorder.return_value
        recorder_instance.stop.return_value = RecordedAudio(
            mic_paths=(tmp_path / "mic.wav",),
            system_paths=(tmp_path / "system.wav",),
            started_at_monotonic=0.0,
        )
        MockTranscriber.return_value.transcribe_parts.return_value = [TranscriptLine(1.0, "mic", "hello")]

        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            settings = _settings(tmp_path, copilot_sync_dir=tmp_path / "Bridge")
            session = MeetingSession(settings, db, "Test Project", "Kickoff")
            session.start()

            session.stop()

            inbox_dir = tmp_path / "Bridge" / "Inbox"
            manifest = json.loads((inbox_dir / f"{session.meeting_code}_done.json").read_text())
            assert manifest["files"]["reference_docs"] == []
            assert manifest["reference_count"] == 0


def test_session_pushes_nothing_without_a_copilot_sync_dir(tmp_path):
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
    ):
        recorder_instance = MockRecorder.return_value
        recorder_instance.stop.return_value = RecordedAudio(
            mic_paths=(tmp_path / "mic.wav",),
            system_paths=(tmp_path / "system.wav",),
            started_at_monotonic=0.0,
        )
        MockTranscriber.return_value.transcribe_parts.return_value = [TranscriptLine(1.0, "mic", "hello")]

        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(_settings(tmp_path), db, "Test Project", "Kickoff")
            session.start()
            result = session.stop()

            assert "hello" in result
            assert not (tmp_path / "Bridge").exists()


def test_session_stop_reports_progress_through_each_stage(tmp_path):
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
    ):
        recorder_instance = MockRecorder.return_value
        recorder_instance.stop.return_value = RecordedAudio(
            mic_paths=(tmp_path / "mic.wav",),
            system_paths=(tmp_path / "system.wav",),
            started_at_monotonic=0.0,
        )
        MockTranscriber.return_value.transcribe_parts.return_value = [TranscriptLine(1.0, "mic", "hello")]

        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            settings = _settings(tmp_path, copilot_sync_dir=tmp_path / "Bridge")
            session = MeetingSession(settings, db, "Test Project", "Kickoff")
            session.start()

            messages = []
            session.stop(on_progress=messages.append)

            assert messages == [
                "Recording stopped.",
                "Transcription complete.",
                "Pushed to Copilot Studio.",
                "Meeting saved.",
            ]


def test_stop_reports_a_low_memory_warning_before_transcribing(tmp_path, monkeypatch):
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
    ):
        MockRecorder.return_value.stop.return_value = RecordedAudio(
            mic_paths=(tmp_path / "mic.wav",),
            system_paths=(tmp_path / "system.wav",),
            started_at_monotonic=0.0,
        )
        MockTranscriber.return_value.transcribe_parts.return_value = []
        monkeypatch.setattr("meeting_scribe.session.available_memory_mb", lambda: 10.0)

        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(_settings(tmp_path), db, "Test Project", "Kickoff")
            session.start()

            messages = []
            session.stop(on_progress=messages.append)

            warnings = [m for m in messages if m.startswith("Low memory warning")]
            assert len(warnings) == 1
            # The warning has to land before transcription actually starts, not alongside or after it —
            # otherwise it's just a postmortem, not the heads-up it's meant to be.
            assert messages.index(warnings[0]) < messages.index("Transcription complete.")


def test_session_stop_reports_skip_message_without_a_copilot_sync_dir(tmp_path):
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
    ):
        recorder_instance = MockRecorder.return_value
        recorder_instance.stop.return_value = RecordedAudio(
            mic_paths=(tmp_path / "mic.wav",),
            system_paths=(tmp_path / "system.wav",),
            started_at_monotonic=0.0,
        )
        MockTranscriber.return_value.transcribe_parts.return_value = [TranscriptLine(1.0, "mic", "hello")]

        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(_settings(tmp_path), db, "Test Project", "Kickoff")
            session.start()

            messages = []
            session.stop(on_progress=messages.append)

            assert messages == [
                "Recording stopped.",
                "Transcription complete.",
                "Copilot sync folder not configured — nothing pushed.",
                "Meeting saved.",
            ]


def test_session_stop_works_without_an_on_progress_callback(tmp_path):
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
    ):
        recorder_instance = MockRecorder.return_value
        recorder_instance.stop.return_value = RecordedAudio(
            mic_paths=(tmp_path / "mic.wav",),
            system_paths=(tmp_path / "system.wav",),
            started_at_monotonic=0.0,
        )
        MockTranscriber.return_value.transcribe_parts.return_value = [TranscriptLine(1.0, "mic", "hello")]

        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(_settings(tmp_path), db, "Test Project", "Kickoff")
            session.start()
            result = session.stop()  # no on_progress passed at all

            assert "hello" in result


def test_session_exposes_its_title(tmp_path):
    with (
        patch("meeting_scribe.session.Recorder"),
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber"),
    ):
        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(_settings(tmp_path), db, "Test Project", "Daily Standup")
            assert session.title == "Daily Standup"


def test_session_set_project_moves_the_meeting_to_an_existing_project(tmp_path):
    with (
        patch("meeting_scribe.session.Recorder"),
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber"),
    ):
        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            other_project = db.create_project("Other Project")
            session = MeetingSession(_settings(tmp_path), db, "Test Project", "Daily Standup")

            session.set_project("Other Project")

            assert session.project.id == other_project.id
            assert db.get_meeting(session.meeting_id).project_id == other_project.id


def test_session_set_project_creates_a_new_project_if_the_name_is_new(tmp_path):
    with (
        patch("meeting_scribe.session.Recorder"),
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber"),
    ):
        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(_settings(tmp_path), db, "Unfiled", "Daily Standup")

            session.set_project("Real Project Name")

            assert session.project.name == "Real Project Name"
            reloaded = db.get_project_by_name("Real Project Name")
            assert reloaded is not None
            assert db.get_meeting(session.meeting_id).project_id == reloaded.id


def test_session_set_project_does_not_move_the_meetings_recording_directory(tmp_path):
    # The whole point of keying meeting storage by meeting id rather than project (see
    # config.Settings.meeting_dir) — moving projects is a pure database update, safe to call even while
    # a real Recorder still has these files open for writing, which this test can't exercise directly
    # (Recorder is mocked here) but the settled meeting_dir path not depending on the project at all is
    # exactly what makes that safe.
    with (
        patch("meeting_scribe.session.Recorder"),
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber"),
    ):
        from meeting_scribe.session import MeetingSession

        settings = _settings(tmp_path)
        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(settings, db, "Test Project", "Daily Standup")
            before = settings.meeting_dir(session.meeting_id)

            session.set_project("A Different Project")

            assert settings.meeting_dir(session.meeting_id) == before


def test_two_sessions_can_be_active_at_once_without_interfering(tmp_path):
    # Regression test for back-to-back meetings: starting a new MeetingSession while a previous one's
    # stop() is still running (transcribing, pushing to Copilot Studio) must not share any mutable state
    # between them — each has its own recorder/screen watcher/transcriber and meeting row.
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
    ):
        MockRecorder.return_value.stop.return_value = RecordedAudio(
            mic_paths=(tmp_path / "mic.wav",),
            system_paths=(tmp_path / "system.wav",),
            started_at_monotonic=0.0,
        )
        MockTranscriber.return_value.transcribe_parts.return_value = [TranscriptLine(1.0, "mic", "hello")]

        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            first = MeetingSession(_settings(tmp_path), db, "Test Project", "Meeting One")
            first.start()

            # "Stop" the first meeting (still finishing up, per the GUI's model) and immediately start a
            # second one before the first's stop() has been awaited.
            second = MeetingSession(_settings(tmp_path), db, "Test Project", "Meeting Two")
            second.start()

            assert first.meeting_id != second.meeting_id
            assert first.title != second.title

            first_result = first.stop()
            second_result = second.stop()

            assert "hello" in first_result
            assert "hello" in second_result
            assert db.get_meeting(first.meeting_id).transcript_text == first_result
            assert db.get_meeting(second.meeting_id).transcript_text == second_result


def test_session_reports_a_capture_stream_that_died_mid_meeting(tmp_path):
    # A recording that lost its microphone half way through produces a transcript that's genuinely
    # missing one side of the conversation — the activity log has to say so rather than leaving it
    # looking like a quiet meeting.
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
    ):
        MockRecorder.return_value.stop.return_value = RecordedAudio(
            mic_paths=(tmp_path / "mic.wav",),
            system_paths=(tmp_path / "system.wav",),
            started_at_monotonic=0.0,
            notices=("Microphone capture stopped early: OSError: device disconnected",),
        )
        MockTranscriber.return_value.transcribe_parts.return_value = []

        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(_settings(tmp_path), db, "Test Project", "Kickoff")
            session.start()

            progress: list[str] = []
            session.stop(on_progress=progress.append)

            assert "Microphone capture stopped early: OSError: device disconnected" in progress


def test_session_transcribes_every_recorded_part(tmp_path):
    # A meeting long enough to overflow a WAV header comes back as several part files per stream, and
    # all of them belong in the transcript.
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
    ):
        mic_paths = (tmp_path / "mic.wav", tmp_path / "mic.part2.wav")
        MockRecorder.return_value.stop.return_value = RecordedAudio(
            mic_paths=mic_paths,
            system_paths=(tmp_path / "system.wav",),
            started_at_monotonic=0.0,
        )
        MockTranscriber.return_value.transcribe_parts.return_value = []

        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(_settings(tmp_path), db, "Test Project", "Kickoff")
            session.start()
            session.stop()

            MockTranscriber.return_value.transcribe_parts.assert_any_call(mic_paths, source="mic", start_offsets={})


def _stuck_meeting(settings: Settings, db: Database, project_name="Test Project", title="Kickoff"):
    """Simulates a meeting whose recording finished but whose transcription didn't: a meeting row with no
    `ended_at`, plus real (if fake) audio files sitting exactly where MeetingSession.stop() would have
    left them — the state a `mkl_malloc: failed to allocate memory` crash mid-transcription leaves
    behind."""
    project = db.get_or_create_project(project_name)
    meeting_id = db.create_meeting(project.id, title)
    meeting_dir = settings.meeting_dir(meeting_id)
    meeting_dir.mkdir(parents=True, exist_ok=True)
    (meeting_dir / "mic.wav").write_bytes(b"fake mic audio")
    (meeting_dir / "system.wav").write_bytes(b"fake system audio")
    return project, meeting_id


def test_retry_transcribes_recorded_audio_and_finishes_the_meeting(tmp_path):
    with patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber:

        def fake_transcribe(paths, source, start_offsets=None):
            if source == "mic":
                return [TranscriptLine(1.0, "mic", "let's get started")]
            return [TranscriptLine(2.0, "system", "sounds good")]

        MockTranscriber.return_value.transcribe_parts.side_effect = fake_transcribe

        from meeting_scribe.session import retry_meeting_transcription

        with Database(tmp_path / "test.db") as db:
            settings = _settings(tmp_path)
            _project, meeting_id = _stuck_meeting(settings, db)

            result = retry_meeting_transcription(settings, db, meeting_id)

            assert "let's get started" in result
            assert "sounds good" in result
            meeting = db.get_meeting(meeting_id)
            assert meeting.transcript_text == result
            assert meeting.ended_at is not None
            assert len(db.get_segments(meeting_id)) == 2


def test_retry_raises_when_the_meeting_already_has_a_transcript(tmp_path):
    from meeting_scribe.session import retry_meeting_transcription

    with Database(tmp_path / "test.db") as db:
        settings = _settings(tmp_path)
        _project, meeting_id = _stuck_meeting(settings, db)
        db.finish_meeting(meeting_id, transcript_text="already done")

        with pytest.raises(ValueError):
            retry_meeting_transcription(settings, db, meeting_id)


def test_retry_raises_for_a_meeting_id_that_does_not_exist(tmp_path):
    from meeting_scribe.session import retry_meeting_transcription

    with Database(tmp_path / "test.db") as db:
        settings = _settings(tmp_path)

        with pytest.raises(ValueError):
            retry_meeting_transcription(settings, db, 999)


def test_retry_raises_when_no_audio_was_ever_recorded(tmp_path):
    from meeting_scribe.session import retry_meeting_transcription

    with Database(tmp_path / "test.db") as db:
        settings = _settings(tmp_path)
        project = db.get_or_create_project("Test Project")
        meeting_id = db.create_meeting(project.id, "Kickoff")  # no audio files written for it

        with pytest.raises(FileNotFoundError):
            retry_meeting_transcription(settings, db, meeting_id)


def test_retry_transcribes_every_recorded_part(tmp_path):
    # A meeting long enough to overflow a WAV header left several numbered part files behind — retry has
    # to rediscover all of them from disk, not just the plainly-named first one.
    with patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber:
        MockTranscriber.return_value.transcribe_parts.return_value = []

        from meeting_scribe.session import retry_meeting_transcription

        with Database(tmp_path / "test.db") as db:
            settings = _settings(tmp_path)
            project, meeting_id = _stuck_meeting(settings, db)
            meeting_dir = settings.meeting_dir(meeting_id)
            (meeting_dir / "mic.part2.wav").write_bytes(b"more fake mic audio")

            retry_meeting_transcription(settings, db, meeting_id)

            mic_paths = (meeting_dir / "mic.wav", meeting_dir / "mic.part2.wav")
            MockTranscriber.return_value.transcribe_parts.assert_any_call(mic_paths, source="mic", start_offsets={})


def test_retry_places_parts_where_the_recording_noted_they_started(tmp_path):
    with patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber:
        MockTranscriber.return_value.transcribe_parts.return_value = []

        from meeting_scribe.session import retry_meeting_transcription

        with Database(tmp_path / "test.db") as db:
            settings = _settings(tmp_path)
            project, meeting_id = _stuck_meeting(settings, db)
            meeting_dir = settings.meeting_dir(meeting_id)
            (meeting_dir / "mic.part2.wav").write_bytes(b"more fake mic audio")
            (meeting_dir / "mic.parts.json").write_text(
                '{"parts": [{"file": "mic.wav", "start_seconds": 0.0}, {"file": "mic.part2.wav", "start_seconds": 95.5}]}',
                encoding="utf-8",
            )

            retry_meeting_transcription(settings, db, meeting_id)

            mic_paths = (meeting_dir / "mic.wav", meeting_dir / "mic.part2.wav")
            MockTranscriber.return_value.transcribe_parts.assert_any_call(
                mic_paths,
                source="mic",
                start_offsets={meeting_dir / "mic.wav": 0.0, meeting_dir / "mic.part2.wav": 95.5},
            )


def test_session_passes_each_parts_recorded_start_time_to_transcription(tmp_path):
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
    ):
        mic_paths = (tmp_path / "mic.wav", tmp_path / "mic.part2.wav")
        offsets = {mic_paths[0]: 0.0, mic_paths[1]: 12.0}
        MockRecorder.return_value.stop.return_value = RecordedAudio(
            mic_paths=mic_paths,
            system_paths=(tmp_path / "system.wav",),
            started_at_monotonic=0.0,
            mic_offsets=offsets,
        )
        MockTranscriber.return_value.transcribe_parts.return_value = []

        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(_settings(tmp_path), db, "Test Project", "Kickoff")
            session.start()
            session.stop()

            MockTranscriber.return_value.transcribe_parts.assert_any_call(
                mic_paths, source="mic", start_offsets=offsets
            )


def test_retry_pushes_to_copilot_studio_when_a_sync_dir_is_configured(tmp_path):
    with patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber:
        MockTranscriber.return_value.transcribe_parts.return_value = [TranscriptLine(1.0, "mic", "hello")]

        from meeting_scribe.session import retry_meeting_transcription

        with Database(tmp_path / "test.db") as db:
            settings = _settings(tmp_path, copilot_sync_dir=tmp_path / "Bridge")
            _project, meeting_id = _stuck_meeting(settings, db)

            retry_meeting_transcription(settings, db, meeting_id)

            inbox_dir = tmp_path / "Bridge" / "Inbox"
            meeting_code = db.get_meeting(meeting_id).meeting_code
            manifest = json.loads((inbox_dir / f"{meeting_code}_done.json").read_text())
            assert manifest["meetingID"] == meeting_code


def test_retry_reports_progress(tmp_path):
    with patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber:
        MockTranscriber.return_value.transcribe_parts.return_value = [TranscriptLine(1.0, "mic", "hello")]

        from meeting_scribe.session import retry_meeting_transcription

        with Database(tmp_path / "test.db") as db:
            settings = _settings(tmp_path)
            _project, meeting_id = _stuck_meeting(settings, db)

            messages = []
            retry_meeting_transcription(settings, db, meeting_id, on_progress=messages.append)

            assert messages == [
                "Transcription complete.",
                "Copilot sync folder not configured — nothing pushed.",
                "Meeting saved.",
            ]


def test_retry_reports_a_low_memory_warning_before_transcribing(tmp_path, monkeypatch):
    with patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber:
        MockTranscriber.return_value.transcribe_parts.return_value = []
        monkeypatch.setattr("meeting_scribe.session.available_memory_mb", lambda: 10.0)

        from meeting_scribe.session import retry_meeting_transcription

        with Database(tmp_path / "test.db") as db:
            settings = _settings(tmp_path)
            _project, meeting_id = _stuck_meeting(settings, db)

            messages = []
            retry_meeting_transcription(settings, db, meeting_id, on_progress=messages.append)

            warnings = [m for m in messages if m.startswith("Low memory warning")]
            assert len(warnings) == 1
            assert messages.index(warnings[0]) < messages.index("Transcription complete.")


def test_retry_replaces_segments_left_by_an_earlier_partial_attempt(tmp_path):
    # Simulates a retry that transcribed fine but then failed later (e.g. pushing to Copilot Studio) —
    # its segments were already committed to the DB even though the meeting was never marked finished. A
    # second retry has to replace those, not add to them.
    with patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber:

        def fake_transcribe(paths, source, start_offsets=None):
            return [TranscriptLine(1.0, "mic", "hello")] if source == "mic" else []

        MockTranscriber.return_value.transcribe_parts.side_effect = fake_transcribe

        from meeting_scribe.session import retry_meeting_transcription

        with Database(tmp_path / "test.db") as db:
            settings = _settings(tmp_path)
            _project, meeting_id = _stuck_meeting(settings, db)
            db.add_transcript_segment(meeting_id, "mic", 0.0, "leftover from a failed earlier retry")

            retry_meeting_transcription(settings, db, meeting_id)

            segments = db.get_segments(meeting_id)
            assert [s["text"] for s in segments] == ["hello"]


def test_session_reads_input_health_through_to_the_recorder(tmp_path):
    # The Record tab polls these while a meeting runs, so a misconfigured input is caught during the
    # meeting rather than in the transcript afterwards.
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber"),
    ):
        from meeting_scribe.session import MeetingSession

        recorder_instance = MockRecorder.return_value
        recorder_instance.capture_notices.return_value = ("Microphone 'Jabra' isn't available",)
        recorder_instance.input_problems.return_value = ("Microphone: no signal at all",)

        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(_settings(tmp_path), db, "Test Project", "Kickoff")

            assert session.capture_notices() == ("Microphone 'Jabra' isn't available",)
            assert session.input_problems() == ("Microphone: no signal at all",)


def test_abandon_stops_capture_without_transcribing_and_leaves_the_meeting_retryable(tmp_path):
    # Closing the app mid-meeting: the audio must be finalized on disk, nothing transcribed, and the
    # meeting left unfinished so Retry can pick it up later.
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher") as MockWatcher,
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
    ):
        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(_settings(tmp_path), db, "Test Project", "Kickoff")
            session.start()
            session.abandon()

            MockWatcher.return_value.stop.assert_called_once()
            MockRecorder.return_value.stop.assert_called_once()
            MockTranscriber.return_value.transcribe_parts.assert_not_called()
            assert db.get_meeting(session.meeting_id).ended_at is None


# --- a track with no audio in it (e.g. a system-audio device that never delivered anything) ----------


def _write_wav(path: Path, seconds: float = 1.0) -> None:
    import contextlib
    import wave

    with contextlib.closing(wave.open(str(path), "wb")) as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(b"\x00\x00" * int(16000 * seconds))


def test_session_leaves_out_an_empty_system_track_instead_of_failing(tmp_path, _real_durations):
    # The field case: a 0-byte system.wav used to make local transcription raise "Invalid data found
    # when processing input", which failed the whole meeting — the mic transcript with it.
    mic, system = tmp_path / "mic.wav", tmp_path / "system.wav"
    _write_wav(mic)
    system.write_bytes(b"")
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
        patch("meeting_scribe.transcription.runpod_whisperx.RunpodWhisperXTranscriber") as MockRunpod,
    ):
        MockRecorder.return_value.stop.return_value = RecordedAudio(
            mic_paths=(mic,), system_paths=(system,), started_at_monotonic=0.0
        )
        MockTranscriber.return_value.transcribe_parts.return_value = [TranscriptLine(1.0, "mic", "hello")]

        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(_settings(tmp_path), db, "Test Project", "Kickoff")
            session.start()
            progress = []
            result = session.stop(on_progress=progress.append)
            finished = db.get_meeting(session.meeting_id).ended_at is not None

    assert finished
    assert "You: hello" in result
    assert "System audio: system.wav has no audio in it, so that track is left out of the transcript." in progress
    # Neither transcriber was asked to read the empty file.
    assert MockTranscriber.return_value.transcribe_parts.call_args_list == [(((mic,),), {"source": "mic", "start_offsets": {}})]
    MockRunpod.assert_not_called()


def test_retry_recovers_a_meeting_whose_system_track_is_empty(tmp_path, _real_durations):
    with patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber:
        MockTranscriber.return_value.transcribe_parts.return_value = [TranscriptLine(1.0, "mic", "hello")]
        from meeting_scribe.session import retry_meeting_transcription

        with Database(tmp_path / "test.db") as db:
            settings = _settings(tmp_path)
            _project, meeting_id = _stuck_meeting(settings, db)
            _write_wav(settings.meeting_dir(meeting_id) / "mic.wav")
            (settings.meeting_dir(meeting_id) / "system.wav").write_bytes(b"")

            progress = []
            result = retry_meeting_transcription(settings, db, meeting_id, on_progress=progress.append)

            assert "You: hello" in result
            assert db.get_meeting(meeting_id).ended_at is not None
            assert any(message.startswith("System audio: system.wav has no audio") for message in progress)


def test_retry_with_only_empty_tracks_says_so(tmp_path, _real_durations):
    from meeting_scribe.session import retry_meeting_transcription

    with Database(tmp_path / "test.db") as db:
        settings = _settings(tmp_path)
        _project, meeting_id = _stuck_meeting(settings, db)
        for name in ("mic.wav", "system.wav"):
            (settings.meeting_dir(meeting_id) / name).write_bytes(b"")

        with pytest.raises(FileNotFoundError, match="is empty"):
            retry_meeting_transcription(settings, db, meeting_id)


# --- a meeting still in progress can't be retried ------------------------------------------------------


def test_a_meeting_that_is_still_recording_cannot_be_retried(tmp_path):
    # The field case: the Projects tab offered Retry for the meeting being recorded (its row looks just
    # like one whose transcription failed), which sent the audio so far to Runpod mid-call and marked
    # the meeting finished with a partial transcript.
    with (
        patch("meeting_scribe.session.Recorder"),
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
    ):
        from meeting_scribe.session import MeetingSession, meeting_in_progress, retry_meeting_transcription

        with Database(tmp_path / "test.db") as db:
            settings = _settings(tmp_path)
            session = MeetingSession(settings, db, "Test Project", "Kickoff")
            session.start()
            (settings.meeting_dir(session.meeting_id) / "mic.wav").write_bytes(b"fake mic audio")

            assert meeting_in_progress(session.meeting_id)
            with pytest.raises(ValueError, match="still being recorded"):
                retry_meeting_transcription(settings, db, session.meeting_id)
            MockTranscriber.return_value.transcribe_parts.assert_not_called()
            assert db.get_meeting(session.meeting_id).ended_at is None

            MockTranscriber.return_value.transcribe_parts.return_value = []
            session.stop()
            assert not meeting_in_progress(session.meeting_id)


def test_a_meeting_is_released_even_when_finishing_it_fails(tmp_path):
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber"),
    ):
        from meeting_scribe.session import MeetingSession, meeting_in_progress

        MockRecorder.return_value.stop.side_effect = RuntimeError("boom")
        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(_settings(tmp_path), db, "Test Project", "Kickoff")
            session.start()
            with pytest.raises(RuntimeError):
                session.stop()
            assert not meeting_in_progress(session.meeting_id)


def test_a_meeting_that_failed_to_start_is_not_left_in_progress(tmp_path):
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber"),
    ):
        from meeting_scribe.session import MeetingSession, meeting_in_progress

        MockRecorder.return_value.start.side_effect = OSError("no microphone")
        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(_settings(tmp_path), db, "Test Project", "Kickoff")
            with pytest.raises(OSError):
                session.start()
            assert not meeting_in_progress(session.meeting_id)


# --- on-screen text is kept on disk as it's captured -----------------------------------------------------


def test_on_screen_text_is_written_to_disk_as_it_arrives(tmp_path):
    with (
        patch("meeting_scribe.session.Recorder"),
        patch("meeting_scribe.session.ScreenWatcher") as MockScreenWatcher,
        patch("meeting_scribe.session.WhisperTranscriber"),
    ):
        from meeting_scribe.session import MeetingSession, load_screen_text_events

        with Database(tmp_path / "test.db") as db:
            settings = _settings(tmp_path)
            session = MeetingSession(settings, db, "Test Project", "Kickoff")
            on_text = MockScreenWatcher.call_args.kwargs["on_text"]
            on_text(ScreenTextEvent(timestamp_seconds=1.5, text="Slide: Agenda"))
            on_text(ScreenTextEvent(timestamp_seconds=9.0, text="Q3 numbers\nRevenue up"))

            assert load_screen_text_events(settings.meeting_dir(session.meeting_id)) == [
                ScreenTextEvent(1.5, "Slide: Agenda"),
                ScreenTextEvent(9.0, "Q3 numbers\nRevenue up"),
            ]


def test_a_cut_off_last_line_of_on_screen_text_is_skipped(tmp_path):
    from meeting_scribe.session import SCREEN_TEXT_FILENAME, load_screen_text_events

    (tmp_path / SCREEN_TEXT_FILENAME).write_text('{"t": 1.0, "text": "kept"}\n{"t": 2.0, "te', encoding="utf-8")
    assert load_screen_text_events(tmp_path) == [ScreenTextEvent(1.0, "kept")]
    assert load_screen_text_events(tmp_path / "missing") == []


def test_retry_brings_back_the_on_screen_text_saved_during_the_meeting(tmp_path):
    with patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber:
        MockTranscriber.return_value.transcribe_parts.side_effect = (
            lambda paths, source, start_offsets=None: [TranscriptLine(1.0, source, f"{source} speech")]
        )
        from meeting_scribe.session import SCREEN_TEXT_FILENAME, retry_meeting_transcription

        with Database(tmp_path / "test.db") as db:
            settings = _settings(tmp_path)
            _project, meeting_id = _stuck_meeting(settings, db)
            (settings.meeting_dir(meeting_id) / SCREEN_TEXT_FILENAME).write_text(
                '{"t": 0.5, "text": "Slide: Agenda"}\n', encoding="utf-8"
            )

            result = retry_meeting_transcription(settings, db, meeting_id)

            assert "Slide: Agenda" in result
            assert {row["source"] for row in db.get_segments(meeting_id)} == {"mic", "system", "screen_ocr"}


def test_on_screen_reading_can_start_later_and_follow_the_box(tmp_path):
    from meeting_scribe.screen.region_picker import RegionTarget

    with (
        patch("meeting_scribe.session.Recorder"),
        patch("meeting_scribe.session.ScreenWatcher") as MockScreenWatcher,
        patch("meeting_scribe.session.WhisperTranscriber"),
    ):
        from meeting_scribe.session import MeetingSession

        watcher = MockScreenWatcher.return_value
        with Database(tmp_path / "test.db") as db:
            MeetingSession(_settings(tmp_path), db, "Test Project", "Kickoff")
            assert MockScreenWatcher.call_args.kwargs["reading"] is True

            session = MeetingSession(_settings(tmp_path), db, "Test Project", "Standup", read_screen=False)
            assert MockScreenWatcher.call_args.kwargs["reading"] is False

            session.set_screen_area({"left": -1200, "top": 40, "width": 800, "height": 120})
            watcher.set_target.assert_called_with(RegionTarget(left=-1200, top=40, width=800, height=120))
            session.set_screen_reading(True)
            watcher.set_reading.assert_called_with(True)


def test_a_meeting_where_ocr_was_never_started_says_so(tmp_path):
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher") as MockScreenWatcher,
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
    ):
        from meeting_scribe.session import MeetingSession

        MockRecorder.return_value.stop.return_value = RecordedAudio((), (), 0.0)
        MockTranscriber.return_value.transcribe_parts.return_value = []
        MockScreenWatcher.return_value.ever_read = False
        MockScreenWatcher.return_value.notices.return_value = ()
        messages = []
        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(_settings(tmp_path), db, "Test Project", "Kickoff", read_screen=False)
            session.start()
            session.stop(on_progress=messages.append)

        assert any("Start OCR was never pressed" in message for message in messages)


# --- a second transcription after the fact ----------------------------------------------------------------


def _finished_locally(settings, db):
    """A meeting transcribed on this PC only, with its audio still on disk."""
    project, meeting_id = _stuck_meeting(settings, db)
    db.add_transcript_segment(meeting_id, "screen_ocr", 0.5, "Agenda slide")
    db.add_transcript_segment(meeting_id, "mic", 1.0, "local mic", engine="local")
    db.add_transcript_segment(meeting_id, "system", 2.0, "local system", engine="local")
    db.finish_meeting(meeting_id, transcript_text="old")
    return meeting_id


def test_a_meeting_transcribed_here_can_be_sent_to_the_cloud_afterwards(tmp_path):
    with patch("meeting_scribe.transcription.runpod_whisperx.RunpodWhisperXTranscriber") as MockRunpod:
        MockRunpod.return_value.transcribe_parts.side_effect = lambda paths, source, **kwargs: [
            TranscriptLine(1.5, source, f"cloud {source}", speaker="SPEAKER_00" if source == "system" else None)
        ]
        from meeting_scribe.session import CLOUD_ENGINE, add_transcription, meeting_transcripts

        with Database(tmp_path / "test.db") as db:
            settings = _settings(tmp_path, runpod_api_key="k", runpod_endpoint_id="e")  # cloud not ticked
            meeting_id = _finished_locally(settings, db)

            result = add_transcription(settings, db, meeting_id, CLOUD_ENGINE)
            transcripts = meeting_transcripts(db.get_segments(meeting_id))
            meeting = db.get_meeting(meeting_id)

    assert [line.text for line in transcripts.local] == ["local mic", "local system"]
    assert [line.text for line in transcripts.cloud] == ["cloud mic", "cloud system"]
    assert [line.text for line in transcripts.screen] == ["Agenda slide"]
    assert result == "[00:01] You: cloud mic\n[00:01] SPEAKER_00: cloud system"
    # The saved transcript is now the cloud's, with the on-screen text still merged in.
    assert "SPEAKER_00: cloud system" in meeting.transcript_text and "Agenda slide" in meeting.transcript_text


def test_transcribing_again_replaces_only_that_engines_lines(tmp_path):
    with patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber:
        MockTranscriber.return_value.transcribe_parts.side_effect = lambda paths, source, start_offsets=None: [
            TranscriptLine(1.0, source, f"new local {source}")
        ]
        from meeting_scribe.session import LOCAL_ENGINE, add_transcription, meeting_transcripts

        with Database(tmp_path / "test.db") as db:
            settings = _settings(tmp_path)
            meeting_id = _finished_locally(settings, db)
            db.add_transcript_segment(meeting_id, "system", 3.0, "cloud system", speaker="SPEAKER_00", engine="runpod")

            add_transcription(settings, db, meeting_id, LOCAL_ENGINE)
            transcripts = meeting_transcripts(db.get_segments(meeting_id))

    assert [line.text for line in transcripts.local] == ["new local mic", "new local system"]
    assert [line.text for line in transcripts.cloud] == ["new local mic", "cloud system"]  # borrows the mic
    assert [line.text for line in transcripts.screen] == ["Agenda slide"]


def test_a_cloud_failure_after_the_fact_is_raised_not_papered_over(tmp_path):
    from meeting_scribe.transcription.runpod_whisperx import RunpodWhisperXError

    with (
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
        patch(
            "meeting_scribe.transcription.runpod_whisperx.RunpodWhisperXTranscriber",
            side_effect=RunpodWhisperXError("not configured"),
        ),
    ):
        from meeting_scribe.session import CLOUD_ENGINE, add_transcription, meeting_in_progress

        with Database(tmp_path / "test.db") as db:
            settings = _settings(tmp_path)
            meeting_id = _finished_locally(settings, db)

            with pytest.raises(RunpodWhisperXError):
                add_transcription(settings, db, meeting_id, CLOUD_ENGINE)
            remaining = [row["text"] for row in db.get_segments(meeting_id)]

    MockTranscriber.return_value.transcribe_parts.assert_not_called()
    assert remaining == ["Agenda slide", "local mic", "local system"]  # nothing lost
    assert not meeting_in_progress(meeting_id)


def test_an_unfinished_meeting_is_retried_rather_than_transcribed_again(tmp_path):
    from meeting_scribe.session import LOCAL_ENGINE, add_transcription

    with Database(tmp_path / "test.db") as db:
        settings = _settings(tmp_path)
        _project, meeting_id = _stuck_meeting(settings, db)

        with pytest.raises(ValueError, match="Retry"):
            add_transcription(settings, db, meeting_id, LOCAL_ENGINE)


def test_a_meeting_from_before_the_mic_went_to_the_cloud_shows_a_whole_cloud_transcript(tmp_path):
    from meeting_scribe.session import meeting_transcripts

    with Database(tmp_path / "test.db") as db:
        project = db.get_or_create_project("P")
        meeting_id = db.create_meeting(project.id, "Old")
        db.add_transcript_segment(meeting_id, "mic", 1.0, "you, locally", engine="local")
        db.add_transcript_segment(meeting_id, "system", 2.0, "them, in the cloud", speaker="SPEAKER_00", engine="runpod")
        db.add_transcript_segment(meeting_id, "mic", 3.0, "you, untagged")  # saved before lines were tagged

        transcripts = meeting_transcripts(db.get_segments(meeting_id))

    assert [line.text for line in transcripts.local] == ["you, locally", "you, untagged"]
    assert [line.text for line in transcripts.cloud] == ["you, locally", "them, in the cloud", "you, untagged"]
    assert transcripts.preferred == transcripts.cloud


# --- deleting a meeting -----------------------------------------------------------------------------------


def test_deleting_a_meeting_removes_its_record_audio_and_attached_documents(tmp_path):
    from meeting_scribe.session import delete_meeting

    with Database(tmp_path / "test.db") as db:
        settings = _settings(tmp_path)
        project, meeting_id = _stuck_meeting(settings, db, title="Test meeting")
        kept_id = db.create_meeting(project.id, "Real meeting")
        db.add_transcript_segment(meeting_id, "mic", 1.0, "testing testing")
        settings.documents_dir.mkdir(parents=True)
        attached, filed = settings.documents_dir / "a.pdf", settings.documents_dir / "b.pdf"
        attached.write_bytes(b"a")
        filed.write_bytes(b"b")
        outside = tmp_path / "elsewhere.pdf"
        outside.write_bytes(b"c")
        db.add_document(project.id, "a.pdf", "a", meeting_id=meeting_id, source_path=str(attached))
        db.add_document(project.id, "elsewhere.pdf", "c", meeting_id=meeting_id, source_path=str(outside))
        db.add_document(project.id, "b.pdf", "b", source_path=str(filed))  # the project's, not the meeting's

        left_behind = delete_meeting(settings, db, meeting_id)

        assert left_behind == []
        assert db.get_meeting(meeting_id) is None
        assert db.get_segments(meeting_id) == []
        assert [m.title for m in db.list_meetings(project.id)] == ["Real meeting"]
        assert db.get_meeting(kept_id) is not None
        assert [row["filename"] for row in db.list_documents(project.id)] == ["b.pdf"]
    assert not settings.meeting_dir(meeting_id).exists()
    assert not attached.exists()
    assert filed.exists()
    assert outside.exists()  # never deletes a file outside the app's own documents folder


def test_a_meeting_being_recorded_cannot_be_deleted(tmp_path):
    from meeting_scribe.session import _claim_meeting, _release_meeting, delete_meeting

    with Database(tmp_path / "test.db") as db:
        settings = _settings(tmp_path)
        _project, meeting_id = _stuck_meeting(settings, db)
        _claim_meeting(meeting_id)
        try:
            with pytest.raises(ValueError, match="still being recorded"):
                delete_meeting(settings, db, meeting_id)
        finally:
            _release_meeting(meeting_id)
        assert db.get_meeting(meeting_id) is not None
    assert settings.meeting_dir(meeting_id).exists()


def test_deleting_a_meeting_that_does_not_exist_says_so(tmp_path):
    from meeting_scribe.session import delete_meeting, meeting_in_progress

    with Database(tmp_path / "test.db") as db:
        with pytest.raises(ValueError, match="No meeting"):
            delete_meeting(_settings(tmp_path), db, 42)
    assert not meeting_in_progress(42)
