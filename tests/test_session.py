import json
from pathlib import Path
from unittest.mock import patch

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
) -> Settings:
    return Settings(
        data_dir=tmp_path,
        whisper_model_size="tiny",
        tesseract_cmd=None,
        screen_capture_interval_seconds=3.0,
        mic_device_name=mic_device_name,
        system_device_name=system_device_name,
        copilot_sync_dir=copilot_sync_dir,
    )


def test_session_merges_audio_and_screen_into_saved_transcript(tmp_path):
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher") as MockScreenWatcher,
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
    ):
        recorder_instance = MockRecorder.return_value
        recorder_instance.stop.return_value = RecordedAudio(
            mic_path=tmp_path / "mic.wav", system_path=tmp_path / "system.wav", started_at_monotonic=0.0
        )

        screen_watcher_instance = MockScreenWatcher.return_value

        transcriber_instance = MockTranscriber.return_value

        def fake_transcribe(path, source):
            if source == "mic":
                return [TranscriptLine(1.0, "mic", "let's get started")]
            return [TranscriptLine(2.0, "system", "sounds good")]

        transcriber_instance.transcribe.side_effect = fake_transcribe

        from meeting_scribe.session import MeetingSession

        with Database(tmp_path / "test.db") as db:
            session = MeetingSession(_settings(tmp_path), db, "Test Project", "Kickoff")

            # Simulate the screen watcher having captured one slide during the meeting.
            session._screen_events.append(ScreenTextEvent(timestamp_seconds=0.5, text="Slide: Agenda"))

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
            mic_path=tmp_path / "mic.wav", system_path=tmp_path / "system.wav", started_at_monotonic=0.0
        )
        MockTranscriber.return_value.transcribe.return_value = [TranscriptLine(1.0, "mic", "hello")]

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
            mic_path=tmp_path / "mic.wav", system_path=tmp_path / "system.wav", started_at_monotonic=0.0
        )
        MockTranscriber.return_value.transcribe.return_value = [TranscriptLine(1.0, "mic", "hello")]

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
            mic_path=tmp_path / "mic.wav", system_path=tmp_path / "system.wav", started_at_monotonic=0.0
        )
        MockTranscriber.return_value.transcribe.return_value = [TranscriptLine(1.0, "mic", "hello")]

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
            mic_path=tmp_path / "mic.wav", system_path=tmp_path / "system.wav", started_at_monotonic=0.0
        )
        MockTranscriber.return_value.transcribe.return_value = [TranscriptLine(1.0, "mic", "hello")]

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
            mic_path=tmp_path / "mic.wav", system_path=tmp_path / "system.wav", started_at_monotonic=0.0
        )
        MockTranscriber.return_value.transcribe.return_value = [TranscriptLine(1.0, "mic", "hello")]

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
            mic_path=tmp_path / "mic.wav", system_path=tmp_path / "system.wav", started_at_monotonic=0.0
        )
        MockTranscriber.return_value.transcribe.return_value = [TranscriptLine(1.0, "mic", "hello")]

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


def test_session_stop_reports_skip_message_without_a_copilot_sync_dir(tmp_path):
    with (
        patch("meeting_scribe.session.Recorder") as MockRecorder,
        patch("meeting_scribe.session.ScreenWatcher"),
        patch("meeting_scribe.session.WhisperTranscriber") as MockTranscriber,
    ):
        recorder_instance = MockRecorder.return_value
        recorder_instance.stop.return_value = RecordedAudio(
            mic_path=tmp_path / "mic.wav", system_path=tmp_path / "system.wav", started_at_monotonic=0.0
        )
        MockTranscriber.return_value.transcribe.return_value = [TranscriptLine(1.0, "mic", "hello")]

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
            mic_path=tmp_path / "mic.wav", system_path=tmp_path / "system.wav", started_at_monotonic=0.0
        )
        MockTranscriber.return_value.transcribe.return_value = [TranscriptLine(1.0, "mic", "hello")]

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
            mic_path=tmp_path / "mic.wav", system_path=tmp_path / "system.wav", started_at_monotonic=0.0
        )
        MockTranscriber.return_value.transcribe.return_value = [TranscriptLine(1.0, "mic", "hello")]

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
