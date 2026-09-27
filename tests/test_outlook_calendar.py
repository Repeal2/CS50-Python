from datetime import datetime, timezone

from meeting_scribe import outlook_calendar
from meeting_scribe.outlook_calendar import CalendarEntry, display_name, invitees, pick_meeting


def _entry(subject, start, end, attendees=(), online=False):
    return CalendarEntry(subject, datetime(2026, 9, 7, *start), datetime(2026, 9, 7, *end), tuple(attendees), online)


STANDUP = _entry("Team Standup", (9, 0), (9, 15), ["Shah, Priya"], online=True)
RENEWAL = _entry("Acme Q3 Renewal", (10, 0), (11, 0), ["Priya Shah", "Tom Jones", "Board Room 4"], online=True)
FOCUS = _entry("Focus time", (10, 0), (12, 0))
LUNCH = _entry("Lunch", (12, 0), (13, 0))


def test_the_meeting_on_when_recording_started_is_picked():
    assert pick_meeting([STANDUP, RENEWAL, LUNCH], datetime(2026, 9, 7, 10, 5), "Weekly") is RENEWAL


def test_recording_a_few_minutes_early_still_finds_the_meeting():
    assert pick_meeting([STANDUP, RENEWAL], datetime(2026, 9, 7, 9, 50), None) is RENEWAL


def test_a_matching_title_beats_a_teams_meeting_which_beats_anything_else():
    at = datetime(2026, 9, 7, 10, 10)
    assert pick_meeting([FOCUS, RENEWAL], at, "Some call") is RENEWAL  # the Teams meeting, not a busy block
    assert pick_meeting([RENEWAL, FOCUS], at, "Focus Time") is FOCUS  # its title says so


def test_nothing_is_picked_when_nothing_was_on():
    assert pick_meeting([STANDUP, LUNCH], datetime(2026, 9, 7, 10, 30), None) is None


def test_invitees_are_people_by_name():
    entry = _entry("x", (1, 0), (2, 0), ["Shah, Priya", "tom.jones@acme.com", "Tom Jones", "Priya Shah", "  "])
    assert invitees(entry) == ["Priya Shah", "Tom Jones"]
    assert invitees(None) == []
    assert display_name("O'Neil, Mary-Jane") == "Mary-Jane O'Neil"


def test_find_meeting_is_nothing_off_windows(monkeypatch):
    monkeypatch.setattr(outlook_calendar.sys, "platform", "linux")
    assert outlook_calendar.find_meeting(datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc), "x") is None


def test_find_meeting_gives_up_on_an_outlook_that_never_answers(monkeypatch):
    import threading

    monkeypatch.setattr(outlook_calendar.sys, "platform", "win32")
    monkeypatch.setattr(outlook_calendar, "_TIMEOUT_SECONDS", 0.05)
    blocked = threading.Event()
    monkeypatch.setattr(outlook_calendar, "_read_entries", lambda start, end: blocked.wait(5) or [])

    try:
        assert outlook_calendar.find_meeting(datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc), "x") is None
    finally:
        blocked.set()


def test_find_meeting_matches_in_local_time_and_survives_outlook_errors(monkeypatch):
    monkeypatch.setattr(outlook_calendar.sys, "platform", "win32")
    local_ten = datetime(2026, 9, 7, 10, 5)
    started = local_ten.astimezone(timezone.utc)
    monkeypatch.setattr(outlook_calendar, "_read_entries", lambda start, end: [STANDUP, RENEWAL])
    assert outlook_calendar.find_meeting(started, None) is RENEWAL

    def broken(start, end):
        raise OSError("Outlook isn't running")

    monkeypatch.setattr(outlook_calendar, "_read_entries", broken)
    assert outlook_calendar.find_meeting(started, None) is None


def test_filter_dates_follow_the_users_date_format():
    moment = datetime(2026, 9, 7, 14, 5)
    assert outlook_calendar._format_short_date(moment, "dd/MM/yyyy") == "07/09/2026"
    assert outlook_calendar._format_short_date(moment, "M/d/yyyy") == "9/7/2026"
    assert outlook_calendar._format_short_date(moment, "yyyy-MM-dd") == "2026-09-07"
    assert outlook_calendar._format_short_date(moment, "dd'.'MM'.'yy") == "07.09.26"
