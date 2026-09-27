"""Reads who was invited to a meeting from the Outlook calendar on this PC, to steer transcription towards
their names (see session._meeting_vocabulary).

Everything stays on the machine: this talks to the Outlook already running here over COM (comtypes, which
the app already uses for audio devices) and never starts Outlook itself or touches the network. It's off
unless ticked in Settings, because Outlook's "object model guard" can put up a "A program is trying to
access e-mail address information" prompt when reading a meeting's recipients on a PC whose antivirus
Outlook doesn't recognise — a prompt nobody should meet for the first time mid-meeting.

The meeting is matched by time: the calendar entry that was on when recording started (or started within
_EARLY_START of it), preferring one whose subject matches the meeting's title, then a Teams meeting, then
the one that started closest to it. Anything that goes wrong — Outlook not running, no match, COM refusing,
a prompt left unanswered past _TIMEOUT_SECONDS — gives no names, never an error.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

# Recording often starts a few minutes before the invite's start time.
_EARLY_START = timedelta(minutes=15)
_TIMEOUT_SECONDS = 20.0
# Outlook's Recipient.Type for a meeting: 1 required, 2 optional, 3 resource (a room or equipment).
_RESOURCE = 3


@dataclass(frozen=True)
class CalendarEntry:
    subject: str
    start: datetime  # local time, as Outlook stores it
    end: datetime
    attendees: tuple[str, ...]
    online: bool = False  # a Teams meeting


def display_name(raw: str) -> str | None:
    """A recipient as a person's name: "Shah, Priya" -> "Priya Shah"; an address with no name, a room or
    a blank -> None."""
    name = " ".join((raw or "").split()).strip("'\" ")
    if not name or "@" in name or not any(char.isalpha() for char in name):
        return None
    parts = [part.strip() for part in name.split(",")]
    if len(parts) == 2 and all(parts):
        name = f"{parts[1]} {parts[0]}"
    return name


def _normalized(text: str) -> str:
    return " ".join("".join(char if char.isalnum() else " " for char in text.casefold()).split())


def pick_meeting(entries: list[CalendarEntry], started_at: datetime, title: str | None) -> CalendarEntry | None:
    """The entry for a meeting that started recording at `started_at` (local time) — see the module
    docstring for the order of preference — or None if nothing on the calendar was on then."""
    candidates = [entry for entry in entries if entry.start - _EARLY_START <= started_at <= entry.end]
    if not candidates:
        return None
    wanted = _normalized(title or "")

    def preference(entry: CalendarEntry):
        subject = _normalized(entry.subject)
        matches_title = bool(wanted) and bool(subject) and (wanted in subject or subject in wanted)
        return (not matches_title, not entry.online, abs((entry.start - started_at).total_seconds()))

    return min(candidates, key=preference)


def invitees(entry: CalendarEntry | None) -> list[str]:
    names: list[str] = []
    for raw in entry.attendees if entry else ():
        name = display_name(raw)
        if name and name not in names:
            names.append(name)
    return names


def find_meeting(started_at: datetime, title: str | None) -> CalendarEntry | None:
    """The Outlook calendar entry for a meeting that started recording at `started_at` (an aware datetime,
    converted to local time here), or None. Windows only; never raises and never waits more than
    _TIMEOUT_SECONDS, however Outlook behaves."""
    if sys.platform != "win32":
        return None
    local_start = started_at.astimezone().replace(tzinfo=None)
    found: list[CalendarEntry] = []

    def work() -> None:
        try:
            found.extend(_read_entries(local_start - timedelta(hours=12), local_start + timedelta(hours=1)))
        except Exception:
            pass  # Outlook not running, COM refused, the guard prompt declined: no names, no error

    thread = threading.Thread(target=work, daemon=True, name="outlook-calendar")
    thread.start()
    thread.join(_TIMEOUT_SECONDS)
    if thread.is_alive():
        return None  # most likely a guard prompt nobody has answered; give up rather than hold up transcription
    return pick_meeting(found, local_start, title)


def _read_entries(window_start: datetime, window_end: datetime) -> list[CalendarEntry]:  # pragma: no cover - Windows
    """Calendar entries overlapping the window, recurring meetings expanded, from the running Outlook."""
    import comtypes
    import comtypes.client

    comtypes.CoInitialize()
    try:
        outlook = comtypes.client.GetActiveObject("Outlook.Application", dynamic=True)
        calendar = outlook.GetNamespace("MAPI").GetDefaultFolder(9)  # olFolderCalendar
        items = calendar.Items
        items.Sort("[Start]")
        items.IncludeRecurrences = True  # must be set after Sort and before Restrict to expand recurrences
        restricted = items.Restrict(
            f"[Start] <= '{_outlook_date(window_end)}' AND [End] >= '{_outlook_date(window_start)}'"
        )
        entries = []
        item = restricted.GetFirst()
        while item is not None and len(entries) < 200:
            if getattr(item, "Class", None) == 26:  # olAppointment
                entries.append(_entry(item))
            item = restricted.GetNext()
        return entries
    finally:
        comtypes.CoUninitialize()


def _entry(item) -> CalendarEntry:  # pragma: no cover - Windows
    recipients = item.Recipients
    names = []
    for index in range(1, int(recipients.Count) + 1):
        recipient = recipients.Item(index)
        if int(recipient.Type) != _RESOURCE:
            names.append(str(recipient.Name))
    location = str(item.Location or "")
    online = bool(getattr(item, "IsOnlineMeeting", False)) or "teams" in location.casefold()
    return CalendarEntry(
        subject=str(item.Subject or ""),
        start=_naive(item.Start),
        end=_naive(item.End),
        attendees=tuple(names),
        online=online,
    )


def _naive(value) -> datetime:  # pragma: no cover - Windows
    return value.replace(tzinfo=None) if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def _outlook_date(moment: datetime) -> str:
    """A date and time the way Outlook's [Start]/[End] filters want it: in the Windows user's own short
    date format (Outlook parses filter dates by the user's locale, so "09/10/2026" means different days
    in London and New York), with a 24-hour time."""
    return f"{_format_short_date(moment, _short_date_pattern())} {moment:%H:%M}"


def _short_date_pattern() -> str:  # pragma: no cover - Windows
    """The user's short date pattern from Windows ("dd/MM/yyyy", "M/d/yyyy", ...)."""
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(80)
        if ctypes.windll.kernel32.GetLocaleInfoEx(None, 0x1F, buffer, len(buffer)):  # LOCALE_SSHORTDATE
            return buffer.value
    except (AttributeError, OSError):
        pass
    return "M/d/yyyy"


def _format_short_date(moment: datetime, pattern: str) -> str:
    """Formats `moment` with a Windows date pattern: d/dd day, M/MM month, yy/yyyy year; anything else
    (separators, quoted text) is copied as it is."""
    tokens = {
        "yyyy": f"{moment.year:04d}",
        "yy": f"{moment.year % 100:02d}",
        "MM": f"{moment.month:02d}",
        "M": str(moment.month),
        "dd": f"{moment.day:02d}",
        "d": str(moment.day),
    }
    result, index = [], 0
    while index < len(pattern):
        if pattern[index] == "'":
            closing = pattern.find("'", index + 1)
            closing = len(pattern) if closing < 0 else closing
            result.append(pattern[index + 1:closing])
            index = closing + 1
            continue
        for token in ("yyyy", "yy", "MM", "M", "dd", "d"):
            if pattern.startswith(token, index):
                result.append(tokens[token])
                index += len(token)
                break
        else:
            result.append(pattern[index])
            index += 1
    return "".join(result)
