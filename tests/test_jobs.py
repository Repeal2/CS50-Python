import pytest

from meeting_scribe.storage.database import Database
from meeting_scribe.transcription import jobs
from meeting_scribe.transcription.jobs import JobBoard


def test_a_job_moves_through_its_stages_and_finishes_done():
    board = JobBoard()
    with board.run(1, "Kickoff", jobs.KIND_AFTER_RECORDING) as job:
        job.set_stage(jobs.LOCAL, 0.0)
        job.set_progress(0.4)
        job.log("Transcription complete.")
        (running,) = board.active()
        assert (running.stage, running.fraction, running.message) == (jobs.LOCAL, 0.4, "Transcription complete.")
        assert running.log[-1].endswith("Transcription complete.")

    assert board.active() == []
    (finished,) = board.snapshot()
    assert finished.state == jobs.DONE and finished.fraction == 1.0 and finished.eta_seconds is None


def test_a_job_that_raises_is_marked_failed_and_the_error_propagates():
    board = JobBoard()
    with pytest.raises(RuntimeError), board.run(1, "Kickoff", jobs.KIND_RETRY):
        raise RuntimeError("mkl_malloc: failed to allocate memory")

    (failed,) = board.snapshot()
    assert failed.state == jobs.FAILED
    assert failed.error == "mkl_malloc: failed to allocate memory"


def test_progress_is_clamped_to_zero_and_one():
    job = JobBoard().start(1, "Kickoff", jobs.KIND_RETRY)
    job.set_progress(1.7)
    assert job._snapshot(0.0).fraction == 1.0
    job.set_progress(-0.2)
    assert job._snapshot(0.0).fraction == 0.0


def test_the_estimate_extrapolates_from_progress_so_far(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(jobs.time, "monotonic", lambda: clock[0])
    job = JobBoard().start(1, "Kickoff", jobs.KIND_AFTER_RECORDING)
    job.set_stage(jobs.LOCAL, 0.0)
    clock[0] = 102.0
    job.set_progress(0.01)
    assert job._snapshot(clock[0]).eta_seconds is None  # too early to say

    clock[0] = 160.0
    job.set_progress(0.25)
    # A quarter done in 60 s: three more quarters to go.
    assert job._snapshot(clock[0]).eta_seconds == pytest.approx(180.0)


def test_there_is_no_estimate_without_a_percentage():
    job = JobBoard().start(1, "Kickoff", jobs.KIND_AGAIN_CLOUD)
    job.set_stage(jobs.CLOUD)
    assert job._snapshot(1e9).eta_seconds is None


def test_running_jobs_come_first_then_finished_ones_newest_first():
    board = JobBoard()
    first = board.start(1, "First", jobs.KIND_RETRY)
    second = board.start(2, "Second", jobs.KIND_RETRY)
    third = board.start(3, "Third", jobs.KIND_RETRY)
    first._finish(None)
    third._finish(None)
    assert [s.title for s in board.snapshot()] == ["Second", "Third", "First"]
    assert second  # still running


def test_only_the_newest_finished_jobs_are_kept_in_memory():
    board = JobBoard()
    for index in range(jobs._FINISHED_KEPT + 5):
        board.start(index, f"Meeting {index}", jobs.KIND_RETRY)._finish(None)
    board.start(999, "Latest", jobs.KIND_RETRY)
    assert len(board.snapshot()) == jobs._FINISHED_KEPT + 1


def test_each_job_is_recorded_as_a_transcription_run(tmp_path):
    with Database(tmp_path / "test.db") as db:
        meeting_id = db.create_meeting(db.create_project("Project").id, "Kickoff")
        board = JobBoard(db)
        with board.run(meeting_id, "Kickoff", jobs.KIND_AFTER_RECORDING) as job:
            job.log("Recording stopped.")
            (row,) = db.list_transcription_runs(meeting_id)
            assert row["outcome"] == "running" and row["finished_at"] is None
            assert row["log"].endswith("Recording stopped.\n")
        with pytest.raises(ValueError), board.run(meeting_id, "Kickoff", jobs.KIND_AGAIN_CLOUD):
            raise ValueError("no API key")

        failed, done = db.list_transcription_runs(meeting_id)
        assert (done["kind"], done["outcome"]) == (jobs.KIND_AFTER_RECORDING, "done")
        assert (failed["kind"], failed["outcome"], failed["detail"]) == (jobs.KIND_AGAIN_CLOUD, "failed", "no API key")
        assert failed["finished_at"] is not None


def test_a_database_error_never_fails_the_job(tmp_path):
    db = Database(tmp_path / "test.db")
    db.close()  # every write now raises
    board = JobBoard(db)
    with board.run(1, "Kickoff", jobs.KIND_RETRY) as job:
        job.log("still fine")
    assert board.snapshot()[0].state == jobs.DONE


def test_deleting_a_meeting_deletes_its_transcription_runs(tmp_path):
    with Database(tmp_path / "test.db") as db:
        meeting_id = db.create_meeting(db.create_project("Project").id, "Kickoff")
        db.start_transcription_run(meeting_id, jobs.KIND_RETRY)
        db.delete_meeting(meeting_id)
        assert db.list_transcription_runs() == []


def test_transcribed_engines_says_which_way_each_meeting_was_transcribed(tmp_path):
    with Database(tmp_path / "test.db") as db:
        project = db.create_project("Project")
        both, local_only, screen_only = (db.create_meeting(project.id, title) for title in ("Both", "Local", "Screen"))
        db.add_transcript_segment(both, "mic", 1.0, "hi", engine="local")
        db.add_transcript_segment(both, "system", 2.0, "hello", engine="runpod")
        db.add_transcript_segment(local_only, "mic", 1.0, "hi")  # from before lines were tagged
        db.add_transcript_segment(screen_only, "screen_ocr", 1.0, "Agenda")

        assert db.transcribed_engines() == {both: {"local", "runpod"}, local_only: {"local"}}
