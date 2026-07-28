"""One-way handoff of a finished meeting to Copilot Studio: write the merged transcript (and any manual
notes) to a file in an "Inbox" folder that OneDrive/SharePoint is already syncing, and stop there.

There's nothing to wait for and nothing to parse back out — Copilot Studio (or whatever processes that
folder on the other end) owns managing and parsing the information from that point on. This app's job is
to record and push, not to consume a synthesized result; the searchable local record of what was recorded
(projects, meetings, transcripts, manual notes) lives on regardless of what happens to the pushed copy.
"""

from __future__ import annotations

import uuid
from pathlib import Path


def format_meeting_for_push(
    *, project_name: str, title: str, transcript_text: str, manual_notes: str | None
) -> str:
    """Plain-text payload for one meeting: enough context (project, title) to be useful on its own once
    it lands wherever Copilot Studio's flow puts it, without requiring the reader to parse anything
    structured."""
    sections = [
        f"Project: {project_name}",
        f"Meeting: {title}",
        "",
        "--- TRANSCRIPT ---",
        "",
        transcript_text or "(no transcript captured)",
    ]
    if manual_notes:
        sections += ["", "--- MANUAL NOTES ---", "", manual_notes]
    return "\n".join(sections)


def push_meeting(content: str, *, inbox_dir: Path) -> Path:
    """Writes `content` to a uniquely-named file in `inbox_dir` (created if it doesn't exist yet) and
    returns its path. Fire-and-forget: there's no response to wait for, so this returns as soon as the
    file is written."""
    inbox_dir.mkdir(parents=True, exist_ok=True)
    path = inbox_dir / f"{uuid.uuid4().hex}__meeting.txt"
    path.write_text(content, encoding="utf-8")
    return path
