import os
from pathlib import Path

from meeting_scribe.ai.minutes_import import MinutesImporter, parse_minutes_filename, read_minutes
from meeting_scribe.storage.database import Database


def _meeting(db: Database, project_name: str, title: str, code: str) -> int:
    project = db.get_or_create_project(project_name)
    meeting_id = db.create_meeting(project.id, title)
    db._conn.execute("UPDATE meetings SET meeting_code = ? WHERE id = ?", (code, meeting_id))
    db._conn.commit()
    return meeting_id


def _write(folder: Path, name: str, text: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_minutes_filename_reads_the_flows_naming():
    parsed = parse_minutes_filename(Path("28-07-2026 11.42 - Acme - Weekly - Sync - Minutes.txt"))
    assert (parsed.date, parsed.time, parsed.name_key) == ("20260728", "1142", "acme - weekly - sync")


def test_parse_minutes_filename_ignores_other_files():
    assert parse_minutes_filename(Path("20260728-1030_done.json")) is None
    assert parse_minutes_filename(Path("20260728-1030_Kickoff_transcript-audio.txt")) is None


def test_minutes_are_filed_against_the_matching_meeting(tmp_path):
    with Database(tmp_path / "db.sqlite") as db:
        kickoff = _meeting(db, "Acme", "Kickoff", "20260728-1030")
        other = _meeting(db, "Acme", "Review", "20260728-1400")
        _write(tmp_path / "sync" / "Acme", "28-07-2026 11.42 - Acme - Kickoff - Minutes.txt", "# Minutes\n- agreed")

        assert MinutesImporter(db).import_from(tmp_path / "sync") == [kickoff]
        assert db.get_meeting(kickoff).minutes == "# Minutes\n- agreed"
        assert db.get_meeting(other).minutes is None


def test_project_and_title_match_despite_case_and_characters_windows_rejects(tmp_path):
    with Database(tmp_path / "db.sqlite") as db:
        meeting = _meeting(db, "Acme", "Budget: Q3?", "20260728-1030-AB1")
        _write(tmp_path / "sync" / "acme", "28-07-2026 11.00 - acme - Budget  Q3 - Minutes.txt", "notes")

        assert MinutesImporter(db).import_from(tmp_path / "sync") == [meeting]


def test_same_title_twice_in_a_day_goes_to_the_meeting_that_had_started(tmp_path):
    with Database(tmp_path / "db.sqlite") as db:
        morning = _meeting(db, "Acme", "Standup", "20260728-0900")
        afternoon = _meeting(db, "Acme", "Standup", "20260728-1500")
        folder = tmp_path / "sync" / "Acme"
        _write(folder, "28-07-2026 09.40 - Acme - Standup - Minutes.txt", "morning minutes")
        _write(folder, "28-07-2026 15.35 - Acme - Standup - Minutes.txt", "afternoon minutes")

        MinutesImporter(db).import_from(tmp_path / "sync")
        assert db.get_meeting(morning).minutes == "morning minutes"
        assert db.get_meeting(afternoon).minutes == "afternoon minutes"


def test_the_latest_file_wins_when_the_flow_ran_twice(tmp_path):
    with Database(tmp_path / "db.sqlite") as db:
        meeting = _meeting(db, "Acme", "Kickoff", "20260728-1030")
        folder = tmp_path / "sync" / "Acme"
        _write(folder, "28-07-2026 11.00 - Acme - Kickoff - Minutes.txt", "first go")
        _write(folder, "28-07-2026 11.20 - Acme - Kickoff - Minutes.txt", "second go")

        MinutesImporter(db).import_from(tmp_path / "sync")
        assert db.get_meeting(meeting).minutes == "second go"


def test_a_different_day_does_not_match(tmp_path):
    with Database(tmp_path / "db.sqlite") as db:
        meeting = _meeting(db, "Acme", "Kickoff", "20260728-1030")
        _write(tmp_path / "sync" / "Acme", "29-07-2026 11.00 - Acme - Kickoff - Minutes.txt", "x")

        assert MinutesImporter(db).import_from(tmp_path / "sync") == []
        assert db.get_meeting(meeting).minutes is None


def test_unchanged_files_are_not_reimported_but_edited_ones_are(tmp_path):
    with Database(tmp_path / "db.sqlite") as db:
        meeting = _meeting(db, "Acme", "Kickoff", "20260728-1030")
        path = _write(tmp_path / "sync" / "Acme", "28-07-2026 11.00 - Acme - Kickoff - Minutes.txt", "v1")
        importer = MinutesImporter(db)

        assert importer.import_from(tmp_path / "sync") == [meeting]
        assert importer.import_from(tmp_path / "sync") == []

        path.write_text("version two", encoding="utf-8")
        os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
        assert importer.import_from(tmp_path / "sync") == [meeting]
        assert db.get_meeting(meeting).minutes == "version two"


def test_an_empty_file_is_left_for_later(tmp_path):
    with Database(tmp_path / "db.sqlite") as db:
        meeting = _meeting(db, "Acme", "Kickoff", "20260728-1030")
        path = _write(tmp_path / "sync" / "Acme", "28-07-2026 11.00 - Acme - Kickoff - Minutes.txt", "")
        importer = MinutesImporter(db)

        assert importer.import_from(tmp_path / "sync") == []
        path.write_text("done now", encoding="utf-8")
        assert importer.import_from(tmp_path / "sync") == [meeting]


def test_a_missing_sync_folder_finds_nothing(tmp_path):
    with Database(tmp_path / "db.sqlite") as db:
        _meeting(db, "Acme", "Kickoff", "20260728-1030")
        assert MinutesImporter(db).import_from(tmp_path / "nowhere") == []


def test_read_minutes_handles_a_bom_and_windows_encodings(tmp_path):
    bom = tmp_path / "bom.txt"
    bom.write_bytes("﻿Line one\r\nLine two\r\n".encode("utf-8"))
    assert read_minutes(bom) == "Line one\nLine two"

    cp1252 = tmp_path / "cp1252.txt"
    cp1252.write_bytes("Cost £5".encode("cp1252"))
    assert read_minutes(cp1252) == "Cost £5"
