"""SQLite storage for projects, meetings, transcript segments, and uploaded documents — the local,
searchable-by-browsing record of everything recorded, independent of whatever happens to the copy pushed
to Copilot Studio (see ai/copilot_push.py).
"""

from __future__ import annotations

import re
import sqlite3
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
    manual_notes TEXT
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
    added_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_meetings_project ON meetings(project_id);
CREATE INDEX IF NOT EXISTS idx_segments_meeting ON transcript_segments(meeting_id);
CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project_id);
"""


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """`CREATE TABLE IF NOT EXISTS` in SCHEMA only creates a fresh table for brand-new databases — it's
    a no-op against a `meetings` table that already exists from before a column was added, so an
    existing user's database needs an explicit ALTER TABLE to catch up. There's no formal migration
    framework here; this is a deliberately minimal "add the column if it isn't already there" check."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(meetings)")}
    if "manual_notes" not in existing:
        conn.execute("ALTER TABLE meetings ADD COLUMN manual_notes TEXT")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "project"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
            cur.execute(
                "INSERT INTO meetings (project_id, title, started_at) VALUES (?, ?, ?)",
                (project_id, title, started_at or _now()),
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
        self, project_id: int, filename: str, content_text: str, meeting_id: int | None = None
    ) -> int:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO documents (project_id, meeting_id, filename, content_text, added_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (project_id, meeting_id, filename, content_text, _now()),
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

