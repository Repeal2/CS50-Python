"""SQLite storage for projects, meetings, transcript segments, and uploaded documents — the local,
searchable-by-browsing record of everything recorded, independent of whatever happens to the copy pushed
to Copilot Studio (see ai/copilot_push.py).
"""

from __future__ import annotations

import os
import random
import re
import sqlite3
import string
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    transcript_text TEXT,
    notes_markdown TEXT,
    manual_notes TEXT,
    attendees TEXT,
    meeting_code TEXT,
    minutes TEXT
);

CREATE TABLE IF NOT EXISTS transcript_segments (
    id INTEGER PRIMARY KEY,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('mic', 'system', 'screen_ocr')),
    timestamp_seconds REAL NOT NULL,
    text TEXT NOT NULL,
    speaker TEXT,
    engine TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    meeting_id INTEGER REFERENCES meetings(id) ON DELETE SET NULL,
    filename TEXT NOT NULL,
    content_text TEXT NOT NULL,
    source_path TEXT,
    added_at TEXT NOT NULL
);

-- Every transcription run of a meeting — after recording, a retry, or another engine afterwards — with
-- how it went and what it reported, for the Transcriptions page. A row still 'running' that no job in
-- this process owns was cut short by the app closing or crashing.
CREATE TABLE IF NOT EXISTS transcription_runs (
    id INTEGER PRIMARY KEY,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT NOT NULL DEFAULT 'running' CHECK (outcome IN ('running', 'done', 'failed')),
    detail TEXT,
    log TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_meetings_project ON meetings(project_id);
CREATE INDEX IF NOT EXISTS idx_runs_meeting ON transcription_runs(meeting_id);
CREATE INDEX IF NOT EXISTS idx_segments_meeting ON transcript_segments(meeting_id);
CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project_id);
"""


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """`CREATE TABLE IF NOT EXISTS` in SCHEMA only creates a fresh table for brand-new databases — it's
    a no-op against tables that already exist from before a column was added, so an existing user's
    database needs an explicit ALTER TABLE to catch up. There's no formal migration framework here; this
    is a deliberately minimal "add the column if it isn't already there" check."""
    meetings_columns = {row["name"] for row in conn.execute("PRAGMA table_info(meetings)")}
    if "manual_notes" not in meetings_columns:
        conn.execute("ALTER TABLE meetings ADD COLUMN manual_notes TEXT")
    if "attendees" not in meetings_columns:
        conn.execute("ALTER TABLE meetings ADD COLUMN attendees TEXT")
    if "meeting_code" not in meetings_columns:
        conn.execute("ALTER TABLE meetings ADD COLUMN meeting_code TEXT")
    if "minutes" not in meetings_columns:
        conn.execute("ALTER TABLE meetings ADD COLUMN minutes TEXT")

    documents_columns = {row["name"] for row in conn.execute("PRAGMA table_info(documents)")}
    if "source_path" not in documents_columns:
        conn.execute("ALTER TABLE documents ADD COLUMN source_path TEXT")

    segment_columns = {row["name"] for row in conn.execute("PRAGMA table_info(transcript_segments)")}
    if "speaker" not in segment_columns:
        conn.execute("ALTER TABLE transcript_segments ADD COLUMN speaker TEXT")
    if "engine" not in segment_columns:
        conn.execute("ALTER TABLE transcript_segments ADD COLUMN engine TEXT")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "project"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_MEETING_CODE_SUFFIX_ALPHABET = string.ascii_uppercase + string.digits


def _generate_meeting_code(conn: sqlite3.Connection) -> str:
    """A meetingID in the "YYYYMMDD-HHMM" format the Copilot push package's file names are keyed on
    (see ai/copilot_push.py) — local wall-clock time, since it's meant to be human-recognizable
    ("this is this morning's 10:30 meeting"), not a machine timestamp. Two meetings starting in the same
    minute would otherwise collide, so a random 3-character suffix is appended whenever that base code is
    already taken."""
    base = datetime.now().strftime("%Y%m%d-%H%M")
    existing = {
        row["meeting_code"]
        for row in conn.execute("SELECT meeting_code FROM meetings WHERE meeting_code IS NOT NULL")
    }
    if base not in existing:
        return base
    while True:
        suffix = "".join(random.choices(_MEETING_CODE_SUFFIX_ALPHABET, k=3))
        candidate = f"{base}-{suffix}"
        if candidate not in existing:
            return candidate


@dataclass(frozen=True)
class Project:
    id: int
    name: str
    slug: str
    created_at: str


@dataclass(frozen=True)
class Meeting:
    id: int
    project_id: int
    title: str
    started_at: str
    ended_at: str | None
    transcript_text: str | None
    notes_markdown: str | None
    manual_notes: str | None
    attendees: str | None
    meeting_code: str | None
    # The meeting's minutes — a summary written after the meeting, shown in the Library's Meeting Minutes
    # tab and, for a recurring meeting, on the Record page when its next occurrence starts.
    minutes: str | None = None


class Database:
    """One Database instance is shared across the GUI's main thread and the background worker threads
    that finish a meeting (transcribe, push, save) so the UI doesn't freeze while that runs.
    sqlite3 connections are tied to the thread that created them by default (`check_same_thread=True`
    raises "SQLite objects created in a thread can only be used in that same thread" otherwise) and
    aren't safe for concurrent use from multiple threads even with that check disabled — so this opens
    with `check_same_thread=False` and serializes every access with `self._lock`, since this instance is
    used across threads (never truly concurrently in practice, but a lock removes that as a requirement
    to reason about) rather than only within one.
    """

    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        _add_missing_columns(self._conn)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- Projects ---------------------------------------------------------

    def create_project(self, name: str) -> Project:
        slug = _slugify(name)
        created_at = _now()
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO projects (name, slug, created_at) VALUES (?, ?, ?)",
                (name, slug, created_at),
            )
            self._conn.commit()
            return Project(id=cur.lastrowid, name=name, slug=slug, created_at=created_at)

    def get_or_create_project(self, name: str) -> Project:
        existing = self.get_project_by_name(name)
        if existing is not None:
            return existing
        return self.create_project(name)

    def get_project_by_name(self, name: str) -> Project | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
        return Project(**dict(row)) if row else None

    def get_project(self, project_id: int) -> Project | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return Project(**dict(row)) if row else None

    def list_projects(self) -> list[Project]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
        return [Project(**dict(row)) for row in rows]

    # -- Meetings -----------------------------------------------------------

    def create_meeting(self, project_id: int, title: str, started_at: str | None = None) -> int:
        with self._lock, closing(self._conn.cursor()) as cur:
            meeting_code = _generate_meeting_code(self._conn)
            cur.execute(
                "INSERT INTO meetings (project_id, title, started_at, meeting_code) VALUES (?, ?, ?, ?)",
                (project_id, title, started_at or _now(), meeting_code),
            )
            self._conn.commit()
            return cur.lastrowid

    def add_transcript_segment(
        self,
        meeting_id: int,
        source: str,
        timestamp_seconds: float,
        text: str,
        speaker: str | None = None,
        engine: str | None = None,
    ) -> None:
        """`speaker` is a diarized line's label (see TranscriptLine.speaker) — None for everything else.
        `engine` says which transcriber produced a spoken line — "local" (Whisper on this PC) or "runpod"
        (see session.LOCAL_ENGINE/CLOUD_ENGINE). None for on-screen text, and for lines saved before this
        existed."""
        if not text.strip():
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO transcript_segments (meeting_id, source, timestamp_seconds, text, speaker, engine) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (meeting_id, source, timestamp_seconds, text, speaker, engine),
            )
            self._conn.commit()

    def finish_meeting(
        self,
        meeting_id: int,
        transcript_text: str,
        notes_markdown: str | None = None,
        ended_at: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE meetings SET transcript_text = ?, notes_markdown = ?, ended_at = ? WHERE id = ?",
                (transcript_text, notes_markdown, ended_at or _now(), meeting_id),
            )
            self._conn.commit()

    def set_manual_notes(self, meeting_id: int, manual_notes: str | None) -> None:
        """Overwrites the meeting's manual notes (typed by the user on the Record page, not generated by
        the Copilot Studio bridge). Called after every addition rather than only once at the end, so
        nothing is lost if the app closes mid-meeting."""
        with self._lock:
            self._conn.execute(
                "UPDATE meetings SET manual_notes = ? WHERE id = ?", (manual_notes, meeting_id)
            )
            self._conn.commit()

    def set_attendees(self, meeting_id: int, attendees: str | None) -> None:
        """Overwrites the meeting's attendee list, built from one or more OCR captures of an attendee
        panel taken during the meeting (see screen/capture.py's ocr_region)."""
        with self._lock:
            self._conn.execute(
                "UPDATE meetings SET attendees = ? WHERE id = ?", (attendees, meeting_id)
            )
            self._conn.commit()

    def set_minutes(self, meeting_id: int, minutes: str | None) -> None:
        """Overwrites the meeting's minutes."""
        with self._lock:
            self._conn.execute("UPDATE meetings SET minutes = ? WHERE id = ?", (minutes, meeting_id))
            self._conn.commit()

    def get_previous_occurrence(
        self, project_id: int, title: str, *, exclude_meeting_id: int | None = None
    ) -> Meeting | None:
        """The most recent meeting with this title in this project — the last occurrence of a recurring
        meeting — leaving out `exclude_meeting_id` (the one being recorded). None if there isn't one."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM meetings WHERE project_id = ? AND title = ? AND id IS NOT ? "
                "ORDER BY started_at DESC LIMIT 1",
                (project_id, title, exclude_meeting_id),
            ).fetchone()
        return Meeting(**dict(row)) if row else None

    def update_meeting_title(self, meeting_id: int, title: str) -> None:
        """Renames a meeting in place — the title can be edited any time up until the meeting ends,
        not just fixed at Start."""
        with self._lock:
            self._conn.execute("UPDATE meetings SET title = ? WHERE id = ?", (title, meeting_id))
            self._conn.commit()

    def move_meeting_to_project(self, meeting_id: int, project_id: int) -> None:
        """Reassigns which project a meeting belongs to — like update_meeting_title, editable any time up
        until the meeting ends, not just fixed at Start (see session.MeetingSession.set_project). A pure
        metadata change: the meeting's recording files are keyed by meeting id, not project (see
        Settings.meeting_dir), so nothing on disk needs to move."""
        with self._lock:
            self._conn.execute(
                "UPDATE meetings SET project_id = ? WHERE id = ?", (project_id, meeting_id)
            )
            self._conn.commit()

    def list_recent_meeting_titles(self, project_id: int) -> list[str]:
        """Distinct meeting titles used in this project, most recently used first — powers the Record
        tab's meeting-title suggestions for quick repeat meetings (e.g. "Weekly Client Meeting")."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT title, MAX(started_at) AS last_used FROM meetings WHERE project_id = ? "
                "GROUP BY title ORDER BY last_used DESC",
                (project_id,),
            ).fetchall()
        return [row["title"] for row in rows]

    def get_meeting(self, meeting_id: int) -> Meeting | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        return Meeting(**dict(row)) if row else None

    def list_meetings(self, project_id: int) -> list[Meeting]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM meetings WHERE project_id = ? ORDER BY started_at DESC", (project_id,)
            ).fetchall()
        return [Meeting(**dict(row)) for row in rows]

    def search_meetings(self, query: str) -> list[Meeting]:
        """Meetings in any project whose title, transcript, manual notes, attendee list or minutes contains
        `query` (case-insensitive), newest first — the Library tab's search box."""
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        columns = ("title", "transcript_text", "manual_notes", "attendees", "minutes")
        where = " OR ".join(f"{column} LIKE ? ESCAPE '\\'" for column in columns)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM meetings WHERE {where} ORDER BY started_at DESC", (pattern,) * len(columns)
            ).fetchall()
        return [Meeting(**dict(row)) for row in rows]

    def list_recent_meetings(self, limit: int = 200) -> list[Meeting]:
        """The newest meetings across every project, newest first — the Transcriptions page's log."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM meetings ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Meeting(**dict(row)) for row in rows]

    def transcribed_engines(self) -> dict[int, set[str]]:
        """{meeting id: the engines its spoken transcript came from} for every meeting with one — "local"
        and/or "runpod" (a line saved before lines were tagged by engine counts as "local")."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT meeting_id, COALESCE(engine, 'local') AS engine FROM transcript_segments "
                "WHERE source != 'screen_ocr'"
            ).fetchall()
        engines: dict[int, set[str]] = {}
        for row in rows:
            engines.setdefault(row["meeting_id"], set()).add(row["engine"])
        return engines

    def get_segments(self, meeting_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM transcript_segments WHERE meeting_id = ? ORDER BY timestamp_seconds",
                (meeting_id,),
            ).fetchall()

    def clear_transcript_segments(self, meeting_id: int, engine: str | None = None) -> None:
        """Deletes every transcript segment recorded for one meeting. Used when retrying a meeting's
        transcription (see session.retry_meeting_transcription) so that retrying twice — the first
        attempt having inserted segments before failing on a later step, like the Copilot push — doesn't
        leave the first attempt's segments sitting alongside a fresh set from the second.

        With `engine`, only the spoken lines that engine produced go — for replacing one of a meeting's
        two transcripts (see session.add_transcription). A spoken line saved before lines were tagged by
        engine counts as "local"; on-screen text is never touched."""
        with self._lock:
            if engine is None:
                self._conn.execute("DELETE FROM transcript_segments WHERE meeting_id = ?", (meeting_id,))
            else:
                self._conn.execute(
                    "DELETE FROM transcript_segments WHERE meeting_id = ? AND source != 'screen_ocr' "
                    "AND (engine = ? OR (engine IS NULL AND ? = 'local'))",
                    (meeting_id, engine, engine),
                )
            self._conn.commit()

    def delete_meeting(self, meeting_id: int) -> list[str]:
        """Deletes a meeting, its transcript and the documents attached to it — documents filed under
        its project alone stay. Returns the attached documents' stored copies (see add_document's
        `source_path`), for the caller to remove from disk; this only touches the database."""
        with self._lock:
            paths = [
                row["source_path"]
                for row in self._conn.execute(
                    "SELECT source_path FROM documents WHERE meeting_id = ? AND source_path IS NOT NULL", (meeting_id,)
                )
            ]
            self._conn.execute("DELETE FROM documents WHERE meeting_id = ?", (meeting_id,))
            self._conn.execute("DELETE FROM transcript_segments WHERE meeting_id = ?", (meeting_id,))
            self._conn.execute("DELETE FROM transcription_runs WHERE meeting_id = ?", (meeting_id,))
            self._conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
            self._conn.commit()
        return paths

    def set_transcript_text(self, meeting_id: int, transcript_text: str) -> None:
        """Replaces a finished meeting's saved transcript text, leaving when it ended alone."""
        with self._lock:
            self._conn.execute("UPDATE meetings SET transcript_text = ? WHERE id = ?", (transcript_text, meeting_id))
            self._conn.commit()

    # -- Documents ------------------------------------------------------------

    def add_document(
        self,
        project_id: int,
        filename: str,
        content_text: str,
        meeting_id: int | None = None,
        source_path: str | None = None,
    ) -> int:
        """`source_path` is where this document's original bytes are stashed on disk (see
        storage.documents.save_original_copy) — None for documents added without a real file behind
        them (e.g. in older tests/data). It's what the Copilot push package copies from when handing a
        meeting's reference documents off under their real filenames."""
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO documents (project_id, meeting_id, filename, content_text, source_path, "
                "added_at) VALUES (?, ?, ?, ?, ?, ?)",
                (project_id, meeting_id, filename, content_text, source_path, _now()),
            )
            self._conn.commit()
            return cur.lastrowid

    def rebase_document_paths(self, old_root: Path | str, new_root: Path | str) -> None:
        """Repoints stored document copies from under `old_root` to the same files under `new_root`,
        for after the data folder has been moved (see config.load_settings)."""
        old_prefix = os.path.join(str(old_root), "")
        new_prefix = os.path.join(str(new_root), "")
        with self._lock:
            self._conn.execute(
                "UPDATE documents SET source_path = ? || substr(source_path, ?) "
                "WHERE substr(source_path, 1, ?) = ?",
                (new_prefix, len(old_prefix) + 1, len(old_prefix), old_prefix),
            )
            self._conn.commit()

    def list_documents(self, project_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM documents WHERE project_id = ? ORDER BY added_at DESC", (project_id,)
            ).fetchall()

    def list_documents_for_meeting(self, meeting_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM documents WHERE meeting_id = ? ORDER BY added_at DESC", (meeting_id,)
            ).fetchall()

    # -- Transcription runs ------------------------------------------------

    def start_transcription_run(self, meeting_id: int, kind: str) -> int:
        """Records that a transcription of this meeting has started; `kind` says which (see
        transcription.jobs). Returns the run's id, for append_transcription_run_log and
        finish_transcription_run."""
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO transcription_runs (meeting_id, kind, started_at) VALUES (?, ?, ?)",
                (meeting_id, kind, _now()),
            )
            self._conn.commit()
            return cur.lastrowid

    def append_transcription_run_log(self, run_id: int, line: str) -> None:
        """Adds one line to a run's log as it happens, so a crash partway through keeps what came before."""
        with self._lock:
            self._conn.execute(
                "UPDATE transcription_runs SET log = log || ? || char(10) WHERE id = ?", (line, run_id)
            )
            self._conn.commit()

    def finish_transcription_run(self, run_id: int, outcome: str, detail: str | None = None) -> None:
        """Marks a run 'done' or 'failed'; `detail` is the error for a failed one."""
        with self._lock:
            self._conn.execute(
                "UPDATE transcription_runs SET outcome = ?, detail = ?, finished_at = ? WHERE id = ?",
                (outcome, detail, _now(), run_id),
            )
            self._conn.commit()

    def list_transcription_runs(self, meeting_id: int | None = None, limit: int = 500) -> list[sqlite3.Row]:
        """Transcription runs, newest first — one meeting's, or every meeting's."""
        with self._lock:
            if meeting_id is None:
                return self._conn.execute(
                    "SELECT * FROM transcription_runs ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
                ).fetchall()
            return self._conn.execute(
                "SELECT * FROM transcription_runs WHERE meeting_id = ? ORDER BY started_at DESC, id DESC LIMIT ?",
                (meeting_id, limit),
            ).fetchall()
