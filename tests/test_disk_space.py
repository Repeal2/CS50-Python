import pytest

from meeting_scribe.storage import disk_space
from meeting_scribe.storage.disk_space import LowDiskWatch, NotEnoughDiskSpace, check_room_to_record

GB = 1024**3


def _free(monkeypatch, *readings):
    """Makes free_bytes return each reading in turn (the last one from then on)."""
    remaining = list(readings)
    monkeypatch.setattr(disk_space, "free_bytes", lambda path: remaining.pop(0) if len(remaining) > 1 else remaining[0])


def test_starting_is_refused_below_the_floor(tmp_path, monkeypatch):
    _free(monkeypatch, 1 * GB)

    with pytest.raises(NotEnoughDiskSpace, match="1,024 MB free"):
        check_room_to_record(tmp_path)


def test_starting_is_allowed_with_room_or_when_space_cant_be_measured(tmp_path, monkeypatch):
    _free(monkeypatch, 5 * GB)
    check_room_to_record(tmp_path)
    _free(monkeypatch, None)
    check_room_to_record(tmp_path)  # a failed reading never blocks a meeting


def test_free_bytes_is_none_for_a_path_that_cant_be_measured(tmp_path):
    assert disk_space.free_bytes(tmp_path / "missing" / "deeper") is None
    assert disk_space.free_bytes(tmp_path) > 0


def test_watch_warns_while_low_and_clears_when_space_comes_back(tmp_path, monkeypatch):
    now = [0.0]
    _free(monkeypatch, 5 * GB, int(0.5 * GB), 3 * GB)
    watch = LowDiskWatch(tmp_path, clock=lambda: now[0], poll_seconds=10)

    assert watch.problem() is None
    now[0] = 10
    assert "512 MB left" in watch.problem()
    now[0] = 15
    assert watch.problem() is not None  # not re-measured until the poll interval has passed
    now[0] = 20
    assert watch.problem() is None
    assert "512 MB free at its lowest" in watch.summary()


def test_watch_keeps_its_last_answer_when_a_reading_fails(tmp_path, monkeypatch):
    now = [0.0]
    _free(monkeypatch, int(0.5 * GB), None)
    watch = LowDiskWatch(tmp_path, clock=lambda: now[0], poll_seconds=1)

    assert watch.problem() is not None
    now[0] = 5
    assert watch.problem() is not None  # a failed probe must never look like a recovery


def test_watch_has_no_summary_if_space_never_ran_low(tmp_path, monkeypatch):
    _free(monkeypatch, 5 * GB)
    watch = LowDiskWatch(tmp_path)

    watch.problem()

    assert watch.summary() is None
