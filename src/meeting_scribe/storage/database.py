"""SQLite storage for projects, meetings, transcript segments, and uploaded documents — the local,
searchable-by-browsing record of everything recorded, independent of whatever happens to the copy pushed
to Copilot Studio (see ai/copilot_push.py).
"""

from __future__ import annotations

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
    meeting_code TEXT
);

CREATE TABLE IF NOT EXISTS transcript_segments (
    id INTEGER PRIMARY KEY,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('mic', 'system', 'screen_ocr')),
    timestamp_seconds REAL NOT NULL,
    text TEXT NOT NULL
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

CREATE INDEX IF NOT EXISTS idx_meetings_project ON meetings(project_id);
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

    documents_columns = {row["name"] for row in conn.execute("PRAGMA table_info(documents)")}
    if "source_path" not in documents_columns:
        conn.execute("ALTER TABLE documents ADD COLUMN source_path TEXT")


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
        self, meeting_id: int, source: str, timestamp_seconds: float, text: str
    ) -> None:
        if not text.strip():
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO transcript_segments (meeting_id, source, timestamp_seconds, text) "
                "VALUES (?, ?, ?, ?)",
                (meeting_id, source, timestamp_seconds, text),
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
        """Overwrites the meeting's manual notes (typed by the user on the Record tab, not generated by
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

    def update_meeting_title(self, meeting_id: int, title: str) -> None:
        """Renames a meeting in place — the title can be edited any time up until the meeting ends,
        not just fixed at Start."""
        with self._lock:
            self._conn.execute("UPDATE meetings SET title = ? WHERE id = ?", (title, meeting_id))
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

    def get_segments(self, meeting_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM transcript_segments WHERE meeting_id = ? ORDER BY timestamp_seconds",
                (meeting_id,),
            ).fetchall()

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

