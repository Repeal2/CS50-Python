"""SQLite storage for projects, meetings, transcript segments, and uploaded documents.

Everything a project owns is full-text indexed (SQLite FTS5) so `ai.search` can pull the passages
relevant to a question before handing them to Claude. FTS5 tables are "external content" tables kept in
sync with triggers, so callers never touch them directly.
"""

from __future__ import annotations

import re
import sqlite3
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
    notes_markdown TEXT
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

CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
    text, content='transcript_segments', content_rowid='id'
);
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    filename, content_text, content='documents', content_rowid='id'
);
CREATE VIRTUAL TABLE IF NOT EXISTS meetings_fts USING fts5(
    title, notes_markdown, content='meetings', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON transcript_segments BEGIN
    INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON transcript_segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(rowid, filename, content_text) VALUES (new.id, new.filename, new.content_text);
END;
CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, filename, content_text)
        VALUES ('delete', old.id, old.filename, old.content_text);
END;

CREATE TRIGGER IF NOT EXISTS meetings_ai AFTER INSERT ON meetings BEGIN
    INSERT INTO meetings_fts(rowid, title, notes_markdown)
        VALUES (new.id, new.title, coalesce(new.notes_markdown, ''));
END;
CREATE TRIGGER IF NOT EXISTS meetings_au AFTER UPDATE ON meetings BEGIN
    INSERT INTO meetings_fts(meetings_fts, rowid, title, notes_markdown)
        VALUES ('delete', old.id, old.title, coalesce(old.notes_markdown, ''));
    INSERT INTO meetings_fts(rowid, title, notes_markdown)
        VALUES (new.id, new.title, coalesce(new.notes_markdown, ''));
END;
CREATE TRIGGER IF NOT EXISTS meetings_ad AFTER DELETE ON meetings BEGIN
    INSERT INTO meetings_fts(meetings_fts, rowid, title, notes_markdown)
        VALUES ('delete', old.id, old.title, coalesce(old.notes_markdown, ''));
END;
"""


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


@dataclass(frozen=True)
class SearchHit:
    kind: str  # "segment" | "document" | "meeting"
    meeting_id: int | None
    label: str
    snippet: str
    rank: float


class Database:
    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
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
        with closing(self._conn.cursor()) as cur:
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
        row = self._conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
        return Project(**dict(row)) if row else None

    def list_projects(self) -> list[Project]:
        rows = self._conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
        return [Project(**dict(row)) for row in rows]

    # -- Meetings -----------------------------------------------------------

    def create_meeting(self, project_id: int, title: str, started_at: str | None = None) -> int:
        with closing(self._conn.cursor()) as cur:
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
        self._conn.execute(
            "UPDATE meetings SET transcript_text = ?, notes_markdown = ?, ended_at = ? WHERE id = ?",
            (transcript_text, notes_markdown, ended_at or _now(), meeting_id),
        )
        self._conn.commit()

    def get_meeting(self, meeting_id: int) -> Meeting | None:
        row = self._conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        return Meeting(**dict(row)) if row else None

    def list_meetings(self, project_id: int) -> list[Meeting]:
        rows = self._conn.execute(
            "SELECT * FROM meetings WHERE project_id = ? ORDER BY started_at DESC", (project_id,)
        ).fetchall()
        return [Meeting(**dict(row)) for row in rows]

    def get_segments(self, meeting_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM transcript_segments WHERE meeting_id = ? ORDER BY timestamp_seconds",
            (meeting_id,),
        ).fetchall()

    # -- Documents ------------------------------------------------------------

    def add_document(
        self, project_id: int, filename: str, content_text: str, meeting_id: int | None = None
    ) -> int:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO documents (project_id, meeting_id, filename, content_text, added_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (project_id, meeting_id, filename, content_text, _now()),
            )
            self._conn.commit()
            return cur.lastrowid

    def list_documents(self, project_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM documents WHERE project_id = ? ORDER BY added_at DESC", (project_id,)
        ).fetchall()

    # -- Search ----------------------------------------------------------------

    def search_project(self, project_id: int, query: str, limit: int = 8) -> list[SearchHit]:
        """Full-text search across a project's transcripts, documents, and meeting notes."""
        fts_query = _to_fts_query(query)
        if not fts_query:
            return []
        hits: list[SearchHit] = []

        for row in self._conn.execute(
            """
            SELECT ts.meeting_id, ts.source, ts.text, bm25(segments_fts) AS rank
            FROM segments_fts
            JOIN transcript_segments ts ON ts.id = segments_fts.rowid
            JOIN meetings m ON m.id = ts.meeting_id
            WHERE segments_fts MATCH ? AND m.project_id = ?
            ORDER BY rank LIMIT ?
            """,
            (fts_query, project_id, limit),
        ):
            hits.append(
                SearchHit(
                    kind="segment",
                    meeting_id=row["meeting_id"],
                    label=f"transcript ({row['source']})",
                    snippet=row["text"],
                    rank=row["rank"],
                )
            )

        for row in self._conn.execute(
            """
            SELECT d.meeting_id, d.filename, d.content_text, bm25(documents_fts) AS rank
            FROM documents_fts
            JOIN documents d ON d.id = documents_fts.rowid
            WHERE documents_fts MATCH ? AND d.project_id = ?
            ORDER BY rank LIMIT ?
            """,
            (fts_query, project_id, limit),
        ):
            hits.append(
                SearchHit(
                    kind="document",
                    meeting_id=row["meeting_id"],
                    label=f"document: {row['filename']}",
                    snippet=row["content_text"][:1000],
                    rank=row["rank"],
                )
            )

        for row in self._conn.execute(
            """
            SELECT mf.rowid AS meeting_id, m.title, m.notes_markdown, bm25(meetings_fts) AS rank
            FROM meetings_fts mf
            JOIN meetings m ON m.id = mf.rowid
            WHERE meetings_fts MATCH ? AND m.project_id = ?
            ORDER BY rank LIMIT ?
            """,
            (fts_query, project_id, limit),
        ):
            hits.append(
                SearchHit(
                    kind="meeting",
                    meeting_id=row["meeting_id"],
                    label=f"meeting notes: {row['title']}",
                    snippet=(row["notes_markdown"] or "")[:1000],
                    rank=row["rank"],
                )
            )

        hits.sort(key=lambda h: h.rank)
        return hits[:limit]


def _to_fts_query(raw: str) -> str:
    """Turn free text into a safe FTS5 MATCH query (OR of the individual terms)."""
    terms = re.findall(r"[A-Za-z0-9_]+", raw)
    if not terms:
        return ""
    return " OR ".join(f'"{t}"' for t in terms)
