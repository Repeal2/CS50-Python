"""One-way handoff of a finished meeting to Copilot Studio: drop its file package into an "Inbox"
folder that OneDrive/SharePoint is already syncing, and stop there.

There's nothing to wait for and nothing to parse back out — Copilot Studio (or whatever processes that
folder on the other end) owns managing and parsing the information from that point on. This app's job is
to record and push, not to consume a synthesized result; the searchable local record of what was recorded
(projects, meetings, transcripts, manual notes) lives on regardless of what happens to the pushed copy.

The package follows a fixed file-naming contract the downstream automation depends on: every file for one
meeting is prefixed with that meeting's `meetingID` (see storage.database's meeting_code, format
"YYYYMMDD-HHMM", e.g. "20260728-1030"), and a `{meetingID}_done.json` manifest is written last, once
every other file has been fully saved — that manifest's arrival, and only its arrival, is what the
downstream workflow triggers on, so nothing else in the folder should look like a completed package to it.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

AUDIO_TRANSCRIPT_SUFFIX = "_transcript-audio.txt"
SCREEN_TRANSCRIPT_SUFFIX = "_transcript-screen.txt"
DONE_MANIFEST_SUFFIX = "_done.json"

_FILENAME_INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_title_for_filename(title: str) -> str:
    """Makes a meeting title safe to drop into a filename: swaps out characters Windows rejects
    (`<>:"/\\|?*` and control characters) for a space, collapses whitespace, and trims trailing dots/
    spaces (which Windows silently strips from a saved filename anyway, so leaving them in would make the
    file on disk not match what the manifest says was written)."""
    cleaned = _FILENAME_INVALID_CHARS_RE.sub(" ", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "Untitled"


@dataclass(frozen=True)
class ReferenceDocument:
    original_filename: str
    source_path: Path


@dataclass(frozen=True)
class TextReferenceDocument:
    """A reference-doc-style entry backed by text already in the database rather than an uploaded file
    on disk — the manual notes typed during the meeting, or the attendee list captured via OCR. Handed
    off the same way a real reference document is (same naming, same manifest shape), just written
    directly from `content` instead of copied from a `source_path`."""

    original_filename: str
    content: str


def _dedupe_filename(candidate: str, used_names: set[str]) -> str:
    """If `candidate` is already taken (two reference documents in this meeting shared an original
    filename), appends "-2", "-3", etc. before the extension until it finds one that isn't. The
    manifest's reference_docs entries already carry both `original_filename` and `saved_filename`, so a
    rename is visible there without needing a separate flag."""
    if candidate not in used_names:
        return candidate
    stem, suffix = Path(candidate).stem, Path(candidate).suffix
    n = 2
    while f"{stem}-{n}{suffix}" in used_names:
        n += 1
    return f"{stem}-{n}{suffix}"


def push_meeting_package(
    *,
    meeting_code: str,
    project_name: str,
    meeting_title: str,
    audio_transcript_text: str,
    screen_transcript_text: str,
    reference_documents: list[ReferenceDocument],
    text_reference_documents: list[TextReferenceDocument] = (),
    inbox_dir: Path,
) -> Path:
    """Writes one meeting's full file package to `inbox_dir` (created if it doesn't exist yet) and
    returns the manifest's path. Reference documents whose original file is no longer reachable on disk
    are skipped rather than failing the whole push — the transcripts and manifest still matter even if
    one attachment went missing. The manifest is written last, and only after everything else succeeded,
    so its appearance reliably means the whole package is ready to fetch.

    `text_reference_documents` (manual notes, the OCR'd attendee list) share one filename namespace with
    `reference_documents` — a real uploaded file happening to be named the same as one of these gets
    deduped against it the same way two uploaded files would be.

    The transcripts and `text_reference_documents` are this app's own generated output, so their filenames
    carry the meeting title between the meetingID and the description of what the file is (e.g.
    `20260728-1030_Kickoff_transcript-audio.txt`) for readability in the inbox folder. Uploaded
    `reference_documents` keep their original filename as-is — the title isn't inserted into those, since
    the point there is to keep the file recognizable as the thing the user attached."""
    inbox_dir.mkdir(parents=True, exist_ok=True)
    title_part = _sanitize_title_for_filename(meeting_title)

    audio_filename = f"{meeting_code}_{title_part}{AUDIO_TRANSCRIPT_SUFFIX}"
    screen_filename = f"{meeting_code}_{title_part}{SCREEN_TRANSCRIPT_SUFFIX}"
    (inbox_dir / audio_filename).write_text(
        audio_transcript_text or "(no audio transcript captured)", encoding="utf-8"
    )
    (inbox_dir / screen_filename).write_text(
        screen_transcript_text or "(no on-screen text captured)", encoding="utf-8"
    )

    used_names: set[str] = set()
    manifest_docs = []
    for text_document in text_reference_documents:
        candidate = f"{meeting_code}_{title_part}_{text_document.original_filename}"
        saved_filename = _dedupe_filename(candidate, used_names)
        used_names.add(saved_filename)
        (inbox_dir / saved_filename).write_text(text_document.content, encoding="utf-8")
        manifest_docs.append(
            {"original_filename": text_document.original_filename, "saved_filename": saved_filename}
        )

    for document in reference_documents:
        if not document.source_path.exists():
            continue  # original file no longer available locally; skip rather than fail the push
        candidate = f"{meeting_code}_{document.original_filename}"
        saved_filename = _dedupe_filename(candidate, used_names)
        used_names.add(saved_filename)
        shutil.copyfile(document.source_path, inbox_dir / saved_filename)
        manifest_docs.append(
            {"original_filename": document.original_filename, "saved_filename": saved_filename}
        )

    manifest = {
        "meetingID": meeting_code,
        "projectName": project_name,
        "meetingTitle": meeting_title,
        "files": {
            "transcript_audio": audio_filename,
            "transcript_screen": screen_filename,
            "reference_docs": manifest_docs,
        },
        "reference_count": len(manifest_docs),
        "timestamp_completed": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    manifest_path = inbox_dir / f"{meeting_code}{DONE_MANIFEST_SUFFIX}"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path
