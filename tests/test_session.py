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


@pytest.fixture(autouse=True)
def _plenty_of_memory(monkeypatch):
    """Keeps transcription.engine's low-memory warning out of tests that aren't specifically exercising
    it. Without this, exact on_progress message-list assertions below would be at the mercy of how much
    RAM happens to be free on whatever machine actually runs the suite; tests that want to exercise the
    warning override this with a low value of their own."""
    monkeypatch.setattr("meeting_scribe.session.available_memory_mb", lambda: 1_000_000.0)


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
    meeting_dir = settings.meeting_dir(project.slug, meeting_id)
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
            meeting_dir = settings.meeting_dir(project.slug, meeting_id)
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
