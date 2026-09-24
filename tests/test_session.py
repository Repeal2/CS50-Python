import json
from pathlib import Path
from unittest.mock import patch

import pytest

from meeting_scribe.audio.recorder import RecordedAudio
from meeting_scribe.config import Settings
from meeting_scribe.screen.capture import ScreenTextEvent, SpeakerNameEvent
from meeting_scribe.storage.database import Database
from meeting_scribe.transcription.engine import TranscriptLine


def _settings(
    tmp_path: Path,
    mic_device_name=None,
    system_device_name=None,
    copilot_sync_dir=None,
    diarize_system_audio=False,
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
        diarize_system_audio=diarize_system_audio,
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

        def fake_transcribe(paths, source):
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


def test_session_wires_the_screen_watcher_to_collect_speaker_name_events(tmp_path):
    """ScreenWatcher is constructed with on_speaker_name pointed at _speaker_name_events, the same way
    on_text is pointed at the on-screen text log — so a badge sighting the watcher reports actually gets kept."""
    with (
        patch("meeting_scribe.session.Recorder"),
        patch("meeting_scribe.session.ScreenWatcher") as MockScreenWatcher,
        patch("meeting_scribe.session.WhisperTranscriber"),
    ):
        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(_settings(tmp_path), db, "Test Project", "Kickoff")

            on_speaker_name = MockScreenWatcher.call_args.kwargs["on_speaker_name"]
            on_speaker_name(SpeakerNameEvent(timestamp_seconds=1.5, name="Jonathan Arnold"))

            assert session._speaker_name_events == [
                SpeakerNameEvent(timestamp_seconds=1.5, name="Jonathan Arnold")
            ]


def test_session_uses_cloud_diarization_for_the_system_track_when_opted_in(tmp_path):
    """diarize_system_audio=True sends the system track to RunpodWhisperXTranscriber *and* transcribes it
    locally, saving both for comparison; the Runpod version is the one used for the meeting's own
    transcript. The mic track is only ever local — it's always a single speaker, so diarizing it can't
    identify anyone new."""
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
        MockTranscriber.return_value.transcribe_parts.side_effect = lambda paths, source: (
            [TranscriptLine(1.0, "mic", "let's get started")]
            if source == "mic"
            else [TranscriptLine(2.0, "system", "sounds could")]  # local Whisper, mishearing a little
        )
        MockRunpod.return_value.transcribe_parts.return_value = [
            TranscriptLine(2.0, "system", "sounds good", speaker="SPEAKER_00")
        ]

        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(
                _settings(
                    tmp_path,
                    diarize_system_audio=True,
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

        # Both versions of the system track are saved, tagged by engine, with Runpod's speaker labels —
        # the Projects tab rebuilds its side-by-side tabs from these.
        assert saved == [
            ("local", "mic", None, "let's get started"),
            ("local", "system", None, "sounds could"),
            ("runpod", "system", "SPEAKER_00", "sounds good"),
        ]
        assert "Transcribing system audio on this PC too, for comparison…" in progress
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
        MockRunpod.return_value.transcribe_parts.assert_called_once_with(
            recorder_instance.stop.return_value.system_paths, source="system"
        )
        # Both tracks went through local transcription too.
        assert MockTranscriber.return_value.transcribe_parts.call_args_list == [
            (((tmp_path / "mic.wav",),), {"source": "mic"}),
            (((tmp_path / "system.wav",),), {"source": "system"}),
        ]
        # The meeting's own transcript uses the Runpod version.
        assert "SPEAKER_00: sounds good" in result
        assert "sounds could" not in result


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

        def fake_transcribe(paths, source):
            if source == "mic":
                return [TranscriptLine(1.0, "mic", "let's get started")]
            return [TranscriptLine(2.0, "system", "sounds good")]

        MockTranscriber.return_value.transcribe_parts.side_effect = fake_transcribe

        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(_settings(tmp_path, diarize_system_audio=True), db, "Test Project", "Kickoff")
            session.start()

            progress_messages = []
            result = session.stop(on_progress=progress_messages.append)

        assert any("using the local transcript only" in message for message in progress_messages)
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

            MockTranscriber.return_value.transcribe_parts.assert_any_call(mic_paths, source="mic")


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

        def fake_transcribe(paths, source):
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
            MockTranscriber.return_value.transcribe_parts.assert_any_call(mic_paths, source="mic")


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

        def fake_transcribe(paths, source):
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
            session = MeetingSession(
                _settings(tmp_path, diarize_system_audio=True, runpod_api_key="k", runpod_endpoint_id="e"),
                db,
                "Test Project",
                "Kickoff",
            )
            session.start()
            progress = []
            result = session.stop(on_progress=progress.append)
            finished = db.get_meeting(session.meeting_id).ended_at is not None

    assert finished
    assert "You: hello" in result
    assert "System audio: system.wav has no audio in it, so that track is left out of the transcript." in progress
    # Neither transcriber was asked to read the empty file.
    assert MockTranscriber.return_value.transcribe_parts.call_args_list == [(((mic,),), {"source": "mic"})]
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
            lambda paths, source: [TranscriptLine(1.0, source, f"{source} speech")]
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


def test_on_screen_capture_can_follow_the_call_to_a_new_window(tmp_path):
    from meeting_scribe.screen.region_picker import WindowRegionTarget
    from meeting_scribe.screen.window_picker import WindowTarget

    with (
        patch("meeting_scribe.session.Recorder"),
        patch("meeting_scribe.session.ScreenWatcher") as MockScreenWatcher,
        patch("meeting_scribe.session.WhisperTranscriber"),
    ):
        from meeting_scribe.session import MeetingSession

        watcher = MockScreenWatcher.return_value
        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(_settings(tmp_path), db, "Test Project", "Kickoff")

            watcher.target = WindowTarget(hwnd=1, title="Meeting | Microsoft Teams")
            session.follow_screen_window(2)
            watcher.set_target.assert_called_with(WindowTarget(hwnd=2, title="Meeting | Microsoft Teams"))

            area = WindowRegionTarget(1, "Teams", 0.1, 0.8, 0.8, 0.1, 800, 60)
            watcher.target = area
            session.follow_screen_window(3, "Weekly sync")
            moved = watcher.set_target.call_args.args[0]
            assert (moved.hwnd, moved.window_title, moved.offset_top_frac) == (3, "Weekly sync", 0.8)
