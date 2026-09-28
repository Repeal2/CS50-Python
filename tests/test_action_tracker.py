import json
from datetime import date, datetime, timezone

import pytest

from meeting_scribe import action_tracker as at


def _action(action_id, **fields):
    return {
        "id": action_id, "title": f"Action {action_id}", "owner": "Someone", "is_mine": False,
        "category": "flues_cremators", "priority": "normal", "status": "open", "due": None,
        "source": {"type": "minutes", "date": "2026-09-11", "detail": "Site meeting"}, "notes": "", "log": [],
        **fields,
    }


def _write_list(path, actions, **extra):
    data = {
        "project": {"name": "Canford Crematorium"},
        "list": {"generated": "2026-09-23", "last_saved": "2026-09-24T07:10:00.000Z"},
        "schema": {"category": ["electrical_generator_dno", "flues_cremators"]},
        "actions": actions,
        **extra,
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


# --- finding the files ---


def test_finds_one_list_per_project_folder_and_skips_folders_without_one(tmp_path):
    for name in ("Canford", "PSDS4", "Inbox"):
        (tmp_path / name).mkdir()
    _write_list(tmp_path / "Canford" / "Canford - Actions.json", [])
    _write_list(tmp_path / "PSDS4" / "PSDS4 - Actions.json", [])
    (tmp_path / "Inbox" / "20260728-1030_done.json").write_text("{}")
    (tmp_path / "Loose - Actions.json").write_text("{}")  # not in a project folder

    found = at.find_action_files(tmp_path)

    assert [(f.project, f.path.name) for f in found] == [
        ("Canford", "Canford - Actions.json"), ("PSDS4", "PSDS4 - Actions.json"),
    ]


def test_prefers_the_list_named_after_its_folder(tmp_path):
    folder = tmp_path / "Canford"
    folder.mkdir()
    _write_list(folder / "Another - Actions.json", [])
    _write_list(folder / "Canford - Actions.json", [])

    assert at.find_action_files(tmp_path)[0].path.name == "Canford - Actions.json"


def test_falls_back_to_the_newest_onedrive_conflict_copy_but_not_a_deliberate_sibling(tmp_path):
    folder = tmp_path / "Canford"
    folder.mkdir()
    old = folder / "Canford - Actions (1).json"
    new = folder / "Canford - Actions-DESKTOP-AB12.json"
    _write_list(old, [])
    _write_list(new, [])
    _write_list(folder / "Canford - Actions - SS.json", [])
    import os
    os.utime(old, (1_000, 1_000))
    os.utime(new, (2_000, 2_000))

    (found,) = at.find_action_files(tmp_path)

    assert found.path == new
    assert found.conflict_copy


def test_a_missing_sync_folder_finds_nothing(tmp_path):
    assert at.find_action_files(tmp_path / "missing") == []


def test_reading_something_that_is_not_an_action_list_raises(tmp_path):
    path = tmp_path / "x - Actions.json"
    path.write_text('{"project": {}}')
    with pytest.raises(ValueError):
        at.read_action_list(path)


# --- saving ---


def test_save_writes_everything_back_and_stamps_last_saved(tmp_path):
    path = tmp_path / "Canford - Actions.json"
    _write_list(path, [_action(1)], gaps=["A gap"])
    loaded = at.read_action_list(path)
    loaded.actions[0]["status"] = "closed"

    saved = at.save_action_list(path, loaded, now=datetime(2026, 9, 28, 9, 30, 5, 123456, tzinfo=timezone.utc))

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["actions"][0]["status"] == "closed"
    assert on_disk["gaps"] == ["A gap"]
    assert on_disk["list"]["generated"] == "2026-09-23"
    assert on_disk["list"]["last_saved"] == "2026-09-28T09:30:05.123Z"
    assert saved.digest == at.file_digest(path)
    assert not (tmp_path / "Canford - Actions.json.saving").exists()


def test_saving_twice_in_a_row_works(tmp_path):
    path = tmp_path / "Canford - Actions.json"
    _write_list(path, [_action(1)])
    loaded = at.save_action_list(path, at.read_action_list(path))
    loaded.actions[0]["notes"] = "Second"

    at.save_action_list(path, loaded)

    assert json.loads(path.read_text(encoding="utf-8"))["actions"][0]["notes"] == "Second"


def test_save_refuses_to_overwrite_a_file_that_changed_since_it_was_loaded(tmp_path):
    path = tmp_path / "Canford - Actions.json"
    _write_list(path, [_action(1)])
    loaded = at.read_action_list(path)
    loaded.actions[0]["status"] = "closed"
    _write_list(path, [_action(1), _action(2)])  # the weekly run
    before = path.read_bytes()

    with pytest.raises(at.ActionsConflict) as conflict:
        at.save_action_list(path, loaded)

    assert path.read_bytes() == before
    assert [a["id"] for a in conflict.value.fresh.actions] == [1, 2]


# --- merging ---


def _loaded(actions):
    return at.LoadedActions({"list": {}, "actions": actions}, "digest")


def test_merge_carries_only_the_actions_changed_here_onto_the_newer_list():
    local = [
        _action(1, status="closed", notes="Done it", touched=True),
        _action(2, status="blocked"),  # stale, untouched: the newer list's version wins
        _action(900, title="Mine", user_added=True, touched=True),
    ]
    fresh = _loaded([
        _action(1, notes="Run's note", title="Reworded by the run"),
        _action(2, status="closed"),
        _action(3),
    ])

    merged, carried = at.merge_local_onto(fresh, local)

    by_id = {a["id"]: a for a in merged.actions}
    assert by_id[1]["status"] == "closed" and by_id[1]["notes"] == "Done it"
    assert by_id[1]["title"] == "Reworded by the run"  # not edited here
    assert by_id[2]["status"] == "closed"
    assert 3 in by_id and by_id[900]["title"] == "Mine"
    assert carried == 2
    assert merged.digest == "digest"


def test_merge_given_this_sessions_changes_ignores_older_touched_marks():
    local = [
        _action(1, status="closed", touched=True),  # ticked in an earlier session, before the run
        _action(2, status="blocked", touched=True),  # changed now
        _action(901, user_added=True, touched=True),  # added earlier; the run has since dropped it
    ]
    fresh = _loaded([_action(1, status="in_progress"), _action(2)])

    merged, carried = at.merge_local_onto(fresh, local, changed_ids={2})

    assert [(a["id"], a["status"]) for a in merged.actions] == [(1, "in_progress"), (2, "blocked")]
    assert carried == 1


def test_merge_keeps_edited_titles_owners_and_dates():
    local = [_action(1, title="Mine now", owner="Me", due="2026-10-02", edited=True, touched=True)]
    merged, _ = at.merge_local_onto(_loaded([_action(1, title="Run's")]), local)
    assert merged.actions[0]["title"] == "Mine now"
    assert merged.actions[0]["owner"] == "Me"
    assert merged.actions[0]["due"] == "2026-10-02"


# --- showing and editing ---


def test_log_entries_are_read_in_both_shapes():
    assert at.log_date({"date": "2026-09-23", "text": "Chased"}) == "2026-09-23"
    assert at.log_text({"date": "2026-09-23", "text": "Chased"}) == "Chased"
    assert at.log_date("2026-09-23: DFW have not replied") == "2026-09-23"
    assert at.log_text("2026-09-23: DFW have not replied") == "DFW have not replied"
    assert at.log_date("No date here") == ""


def test_filters_and_search():
    mine_flagged = _action(1, is_mine=True, priority="flagged", log=["2026-09-23: chased DFW about flues"])
    done = _action(2, status="closed", category="electrical_generator_dno")
    blocked = _action(3, status="blocked", owner="Mo Eldeib")

    assert at.matches(mine_flagged, quick_filter="mine")
    assert not at.matches(done, quick_filter="mine")
    assert at.matches(mine_flagged, quick_filter="flagged")
    assert at.matches(blocked, quick_filter="blocked") and not at.matches(done, quick_filter="blocked")
    assert not at.matches(done, quick_filter="open")
    assert at.matches(done, category="electrical_generator_dno")
    assert not at.matches(mine_flagged, category="electrical_generator_dno")
    assert at.matches(mine_flagged, query="DFW")
    assert at.matches(blocked, query="eldeib")
    assert not at.matches(blocked, query="DFW")


def test_sort_puts_done_last_then_by_priority_then_number():
    actions = [_action(5, status="closed", priority="flagged"), _action(4, priority="low"), _action(9, priority="flagged"),
               _action(2)]
    assert [a["id"] for a in sorted(actions, key=at.sort_key)] == [9, 2, 4, 5]


def test_due_soon():
    today = date(2026, 9, 28)
    assert at.is_due_soon(_action(1, due="2026-09-30"), today)
    assert at.is_due_soon(_action(1, due="2026-09-01"), today)
    assert not at.is_due_soon(_action(1, due="2026-10-01"), today)
    assert not at.is_due_soon(_action(1, due="2026-09-29", status="closed"), today)
    assert not at.is_due_soon(_action(1), today)


def test_new_actions_are_numbered_from_900_and_marked_as_added_here():
    first = at.new_action([_action(7)], title="Call DFW", category="flues_cremators", today=date(2026, 9, 28))
    second = at.new_action([_action(7), first], title="Again", category="flues_cremators", today=date(2026, 9, 28))
    assert (first["id"], second["id"]) == (900, 901)
    assert first["user_added"] and first["touched"] and first["status"] == "open"
    assert first["source"]["date"] == "2026-09-28"


def test_categories_follow_the_schema_then_add_any_others_in_use():
    data = {"schema": {"category": ["b", "a"]}, "actions": [_action(1, category="a"), _action(2, category="c")]}
    assert at.categories(data) == ["b", "a", "c"]
    assert at.category_label("flues_cremators") == "Flues & cremators"
    assert at.category_label("site_logistics") == "Site logistics"


def test_summary_counts():
    counts = at.summary_counts([
        _action(1, is_mine=True, priority="flagged"), _action(2, status="closed", is_mine=True),
        _action(3, status="blocked"), _action(900, user_added=True),
    ])
    assert counts == {"total": 4, "outstanding": 3, "done": 1, "mine": 1, "flagged": 1, "blocked": 1, "added": 1}


def test_record_text_lists_decisions_closed_items_and_gaps():
    text = at.record_text({
        "decisions_this_week": [{"decision": "No Corten", "rationale": "A1 don't do it",
                                 "source": {"type": "minutes", "date": "2026-09-09"}}],
        "closed_this_week": [{"title": "Generator variation", "evidence": "Confirmed 8 Sept"}],
        "gaps": ["Minutes of 8 Sept unreadable"],
    })
    for expected in ("No Corten", "A1 don't do it", "minutes · 2026-09-09", "Generator variation", "Minutes of 8 Sept"):
        assert expected in text
    assert "No decisions" in at.record_text({})


def test_csv_export_has_the_browser_trackers_columns():
    text = at.to_csv([_action(1, is_mine=True, status="in_progress", log=["2026-09-23: chased", {"date": "2026-09-24", "text": "again"}])])
    header, row = text.strip().split("\r\n")
    assert header.startswith('"ID","Action","Owner","Mine"')
    assert '"In progress"' in row and '"Yes"' in row
    assert '"2026-09-23: chased | 2026-09-24: again"' in row
