"""What's being transcribed right now, and how far along it is — the Transcriptions page's live view.

Each meeting finished, retried or transcribed again gets a TranscriptionJob, which the session code (see
session.py) moves through its stages: queued behind another local transcription, in the cloud, on this
PC (with a real percentage — see WhisperTranscriber.transcribe's on_progress), then saving. A JobBoard
holds this process's jobs, and, given a Database, records each one as a transcription run so the page's
history outlives the app.

Jobs are updated from the background threads doing the work and read from the GUI thread, so every
access goes through one lock, and readers only ever get frozen JobSnapshots.
"""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from meeting_scribe.storage.database import Database

# What a job is for — also its `kind` in the database.
KIND_AFTER_RECORDING = "After recording"
KIND_RETRY = "Retry"
KIND_AGAIN_LOCAL = "Again, on this PC"
KIND_AGAIN_CLOUD = "Again, in the cloud"

# Where a job is.
STARTING = "Starting"
QUEUED = "Queued — another meeting is being transcribed on this PC"
CLOUD = "Transcribing in the cloud"
LOCAL = "Transcribing on this PC"
SAVING = "Saving"

RUNNING, DONE, FAILED = "running", "done", "failed"

# Too early to extrapolate from: the first few percent include loading the model, so an estimate made
# then is wildly pessimistic and jumps around.
_ETA_MIN_FRACTION = 0.03
_ETA_MIN_SECONDS = 10.0
# Finished jobs kept in memory for the page's "Now" card; older ones are only in the database.
_FINISHED_KEPT = 20


@dataclass(frozen=True)
class JobSnapshot:
    id: int
    run_id: int | None
    meeting_id: int
    title: str
    kind: str
    state: str  # RUNNING | DONE | FAILED
    stage: str
    fraction: float | None  # 0..1 while transcribing on this PC; None when there's no way to know
    message: str  # the latest thing it reported
    elapsed_seconds: float
    eta_seconds: float | None
    error: str | None
    log: tuple[str, ...]

    @property
    def active(self) -> bool:
        return self.state == RUNNING


class TranscriptionJob:
    """One meeting's transcription, as the session code reports it. Safe to call from any thread."""

    def __init__(self, board: "JobBoard", job_id: int, meeting_id: int, title: str, kind: str, run_id: int | None):
        self._board = board
        self.id, self.meeting_id, self.title, self.kind, self.run_id = job_id, meeting_id, title, kind, run_id
        self._state = RUNNING
        self._stage = STARTING
        self._fraction: float | None = None
        self._message = ""
        self._error: str | None = None
        self._log: list[str] = []
        self._started = time.monotonic()
        self._finished: float | None = None
        # When the current percentage run began and where it started from — the base for the estimate.
        self._progress_started: tuple[float, float] | None = None

    def log(self, message: str) -> None:
        line = f"{datetime.now().strftime('%H:%M:%S')}  {message}"
        with self._board._lock:
            self._log.append(line)
            self._message = message
        if self.run_id is not None:
            self._board._record(lambda db: db.append_transcription_run_log(self.run_id, line))

    def set_stage(self, stage: str, fraction: float | None = None) -> None:
        with self._board._lock:
            self._stage = stage
            self._fraction = fraction
            self._progress_started = (time.monotonic(), fraction) if fraction is not None else None

    def set_progress(self, fraction: float) -> None:
        fraction = min(1.0, max(0.0, fraction))
        with self._board._lock:
            if self._progress_started is None:
                self._progress_started = (time.monotonic(), fraction)
            self._fraction = fraction

    def _finish(self, error: str | None) -> None:
        with self._board._lock:
            self._state = FAILED if error is not None else DONE
            self._error = error
            self._finished = time.monotonic()
            if error is None:
                self._fraction = 1.0 if self._fraction is not None else None
        if self.run_id is not None:
            self._board._record(lambda db: db.finish_transcription_run(self.run_id, self._state, error))

    def _snapshot(self, now: float) -> JobSnapshot:
        end = self._finished if self._finished is not None else now
        return JobSnapshot(
            id=self.id,
            run_id=self.run_id,
            meeting_id=self.meeting_id,
            title=self.title,
            kind=self.kind,
            state=self._state,
            stage=self._stage,
            fraction=self._fraction,
            message=self._message,
            elapsed_seconds=end - self._started,
            eta_seconds=self._eta(now) if self._state == RUNNING else None,
            error=self._error,
            log=tuple(self._log),
        )

    def _eta(self, now: float) -> float | None:
        if self._fraction is None or self._progress_started is None:
            return None
        began, from_fraction = self._progress_started
        done, spent = self._fraction - from_fraction, now - began
        if self._fraction < _ETA_MIN_FRACTION or done <= 0 or spent < _ETA_MIN_SECONDS:
            return None
        return spent / done * (1.0 - self._fraction)


class JobBoard:
    """This process's transcription jobs. With a Database, each job is also recorded as a transcription
    run (see Database.start_transcription_run) — best effort: a database error never fails the job."""

    def __init__(self, db: "Database | None" = None):
        self._db = db
        self._lock = threading.Lock()
        self._jobs: list[TranscriptionJob] = []
        self._next_id = 1

    @contextlib.contextmanager
    def run(self, meeting_id: int, title: str, kind: str) -> Iterator[TranscriptionJob]:
        """A job for the duration of the block: done if the block finishes, failed (and re-raised) if it
        raises."""
        job = self.start(meeting_id, title, kind)
        try:
            yield job
        except BaseException as exc:
            job._finish(str(exc) or type(exc).__name__)
            raise
        job._finish(None)

    def start(self, meeting_id: int, title: str, kind: str) -> TranscriptionJob:
        run_id = None
        if self._db is not None:
            with contextlib.suppress(Exception):
                run_id = self._db.start_transcription_run(meeting_id, kind)
        with self._lock:
            job = TranscriptionJob(self, self._next_id, meeting_id, title, kind, run_id)
            self._next_id += 1
            self._jobs.append(job)
            finished = [j for j in self._jobs if j._state != RUNNING]
            for old in finished[:-_FINISHED_KEPT]:
                self._jobs.remove(old)
        return job

    def snapshot(self) -> list[JobSnapshot]:
        """Every job this process knows of: the running ones in the order they started, then the finished
        ones, newest first."""
        now = time.monotonic()
        with self._lock:
            snapshots = [job._snapshot(now) for job in self._jobs]
        running = [s for s in snapshots if s.active]
        finished = sorted((s for s in snapshots if not s.active), key=lambda s: -s.id)
        return running + finished

    def active(self) -> list[JobSnapshot]:
        return [s for s in self.snapshot() if s.active]

    def _record(self, write) -> None:
        if self._db is None:
            return
        with contextlib.suppress(Exception):
            write(self._db)
