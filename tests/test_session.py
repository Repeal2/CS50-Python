from pathlib import Path
from unittest.mock import patch

from meeting_scribe.audio.recorder import RecordedAudio
from meeting_scribe.config import Settings
from meeting_scribe.screen.capture import ScreenTextEvent
from meeting_scribe.storage.database import Database
from meeting_scribe.transcription.engine import TranscriptLine


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        anthropic_api_key=None,
        anthropic_model="claude-opus-5",
        whisper_model_size="tiny",
        tesseract_cmd=None,
        screen_capture_interval_seconds=3.0,
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

            # No API key configured -> falls back to the plain transcript text.
            assert "Slide: Agenda" in result
            assert "let's get started" in result
            assert "sounds good" in result

            meeting = db.get_meeting(session.meeting_id)
            assert meeting.transcript_text == result
            segments = db.get_segments(session.meeting_id)
            assert len(segments) == 3
