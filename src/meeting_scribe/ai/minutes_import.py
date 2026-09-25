"""The return leg of the Copilot push: pick up the minutes Copilot Studio writes for each pushed meeting
and file them against that meeting, for the Library's Meeting Minutes tab.

The Power Automate flow that writes the minutes reads the pushed `{meetingID}_done.json` manifest (see
ai/copilot_push.py) and saves one text file per meeting to

    <sync folder>/<projectName>/DD-MM-YYYY HH.mm - <projectName> - <meetingTitle> - Minutes.txt

where the sync folder is the one the Inbox lives under (the flow writes to "Meeting minutes/<projectName>"
in OneDrive, and "Meeting minutes" is the folder chosen as the sync folder in Settings). The date comes from
the meetingID (its first eight characters, YYYYMMDD, reordered); HH.mm is UK time when the flow ran, not
when the meeting did, so it can't be matched exactly — it's only used to tell apart two meetings with the
same project and title on the same day (a file belongs to the latest of them that had started by then).

Files are matched on their name alone, so the project folder they're in doesn't matter. The project and
title are compared the way OneDrive would have had to store them — characters Windows rejects in a file
name replaced, case ignored — since the flow puts them into the name unaltered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from meeting_scribe.storage.database import Database, Meeting

MINUTES_SUFFIX = " - Minutes.txt"

# "DD-MM-YYYY HH.mm - <projectName> - <meetingTitle> - Minutes.txt"
_MINUTES_FILENAME_RE = re.compile(
    r"^(?P<day>\d{2})-(?P<month>\d{2})-(?P<year>\d{4}) (?P<hour>\d{2})\.(?P<minute>\d{2}) - (?P<rest>.+) - Minutes\.txt$",
    re.IGNORECASE,
)
# "YYYYMMDD-HHMM", optionally followed by a "-XYZ" suffix (see storage.database._generate_meeting_code).
_MEETING_CODE_RE = re.compile(r"^(?P<date>\d{8})-(?P<time>\d{4})")
_INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _normalize(text: str) -> str:
    cleaned = _INVALID_FILENAME_CHARS_RE.sub(" ", text)
    return re.sub(r"\s+", " ", cleaned).strip(" .").casefold()


@dataclass(frozen=True)
class MinutesFile:
    path: Path
    date: str  # YYYYMMDD, as in the meetingID
    time: str  # HHMM, when the flow wrote it
    name_key: str  # "<projectName> - <meetingTitle>", normalized


def parse_minutes_filename(path: Path) -> MinutesFile | None:
    """A minutes file's date, time and project/title, or None for any other file."""
    match = _MINUTES_FILENAME_RE.match(path.name)
    if match is None:
        return None
    return MinutesFile(
        path=path,
        date=match["year"] + match["month"] + match["day"],
        time=match["hour"] + match["minute"],
        name_key=_normalize(match["rest"]),
    )


def find_minutes_files(root: Path) -> list[MinutesFile]:
    """Every minutes file directly in `root` or one folder down (the per-project folders). A folder that
    can't be listed — OneDrive offline, say — is skipped."""
    found: list[MinutesFile] = []
    folders = [root]
    try:
        folders.extend(child for child in root.iterdir() if child.is_dir())
    except OSError:
        return found
    for folder in folders:
        try:
            paths = list(folder.glob("*" + MINUTES_SUFFIX))
        except OSError:
            continue
        found.extend(parsed for parsed in map(parse_minutes_filename, paths) if parsed is not None)
    return found


def match_minutes_to_meetings(
    files: list[MinutesFile], meetings: list[Meeting], project_names: dict[int, str]
) -> dict[int, MinutesFile]:
    """{meeting id: its minutes file}. A meeting whose flow ran more than once gets the latest file. When
    several meetings share a date, project and title, each file goes to the latest of them that had started
    by the time the file was written (the earliest, if none had — a clock or time-zone difference)."""
    by_key: dict[tuple[str, str], list[tuple[str, Meeting]]] = {}
    for meeting in meetings:
        code = _MEETING_CODE_RE.match(meeting.meeting_code or "")
        project_name = project_names.get(meeting.project_id)
        if code is None or project_name is None:
            continue
        key = (code["date"], _normalize(f"{project_name} - {meeting.title}"))
        by_key.setdefault(key, []).append((code["time"], meeting))

    matched: dict[int, MinutesFile] = {}
    for minutes_file in sorted(files, key=_file_order):
        candidates = sorted(by_key.get((minutes_file.date, minutes_file.name_key), []), key=lambda c: c[0])
        if not candidates:
            continue
        started = [meeting for time, meeting in candidates if time <= minutes_file.time]
        owner = started[-1] if started else candidates[0][1]
        matched[owner.id] = minutes_file  # sorted oldest first, so the latest file wins
    return matched


def _file_order(minutes_file: MinutesFile) -> tuple[str, float]:
    try:
        modified = minutes_file.path.stat().st_mtime
    except OSError:
        modified = 0.0
    return (minutes_file.time, modified)


def read_minutes(path: Path) -> str:
    """The file's text — UTF-8 (with or without a byte-order mark), else Windows-1252."""
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    return text.replace("\r\n", "\n").strip()


class MinutesImporter:
    """Files each meeting's minutes from `root` into the database. Remembers which version of each file
    it has already read, so checking again only opens files that are new or have changed — reading one
    OneDrive hasn't downloaded yet makes it download it."""

    def __init__(self, db: Database):
        self._db = db
        self._seen: dict[Path, tuple[float, int]] = {}

    def import_from(self, root: Path) -> list[int]:
        """Checks `root` and returns the ids of the meetings whose minutes were added or changed. A file
        that can't be read, or is still empty (the flow or OneDrive mid-write), is tried again next time."""
        files = find_minutes_files(root)
        if not files:
            return []
        projects = {project.id: project.name for project in self._db.list_projects()}
        meetings = [meeting for project_id in projects for meeting in self._db.list_meetings(project_id)]
        updated: list[int] = []
        for meeting_id, minutes_file in match_minutes_to_meetings(files, meetings, projects).items():
            try:
                stat = minutes_file.path.stat()
                version = (stat.st_mtime, stat.st_size)
                if self._seen.get(minutes_file.path) == version:
                    continue
                text = read_minutes(minutes_file.path)
            except OSError:
                continue
            if not text:
                continue
            self._seen[minutes_file.path] = version
            meeting = self._db.get_meeting(meeting_id)
            if meeting is not None and meeting.minutes != text:
                self._db.set_minutes(meeting_id, text)
                updated.append(meeting_id)
        return updated
