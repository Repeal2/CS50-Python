import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from meeting_scribe.config import DB_FILENAME, default_data_dir, load_settings
from meeting_scribe.storage.database import Database


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_SCRIBE_DATA_DIR", raising=False)
    home = tmp_path / "Users" / "someone"
    appdata = home / "AppData" / "Roaming"
    local_appdata = home / "AppData" / "Local"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    return SimpleNamespace(
        home=home,
        target=home / "MeetingScribe",
        appdata_dir=appdata / "MeetingScribe",
        store_dir=lambda version: (
            local_appdata / "Packages" / f"PythonSoftwareFoundation.Python.{version}_qbz5n2kfra8p0"
            / "LocalCache" / "Roaming" / "MeetingScribe"
        ),
    )


def _make_data_dir(path: Path, *, project: str, document_root: Path | None = None) -> None:
    """A data folder as an earlier version left it: a database with one project and, optionally, one
    attached document whose stored path is under `document_root`."""
    document_root = document_root or path
    (path / "documents").mkdir(parents=True)
    (path / "documents" / "abc.pdf").write_bytes(b"%PDF")
    with Database(path / DB_FILENAME) as db:
        project_id = db.create_project(project).id
        db.add_document(project_id, "Spec.pdf", "text", source_path=str(document_root / "documents" / "abc.pdf"))


def _projects(data_dir: Path) -> list[str]:
    with Database(data_dir / DB_FILENAME) as db:
        return [p.name for p in db.list_projects()]


def _document_paths(data_dir: Path) -> list[str]:
    with Database(data_dir / DB_FILENAME) as db:
        return [row["source_path"] for p in db.list_projects() for row in db.list_documents(p.id)]


def test_data_lives_directly_under_the_user_profile(profile):
    assert default_data_dir() == profile.home / "MeetingScribe"


def test_the_data_dir_env_var_wins(profile, tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path / "elsewhere"))
    assert default_data_dir() == tmp_path / "elsewhere"


def test_falls_back_to_home_without_a_user_profile(monkeypatch):
    monkeypatch.delenv("MEETING_SCRIBE_DATA_DIR", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    assert default_data_dir() == Path.home() / ".meeting_scribe_data"


def test_a_fresh_install_starts_in_the_new_folder(profile):
    settings = load_settings()

    assert settings.data_dir == profile.target
    assert settings.moved_from is None


def test_moves_data_from_appdata_and_repoints_documents(profile):
    _make_data_dir(profile.appdata_dir, project="Acme")

    settings = load_settings()

    assert settings.data_dir == profile.target
    assert settings.moved_from == Path(os.path.realpath(profile.appdata_dir))
    assert not profile.appdata_dir.exists()
    assert _projects(profile.target) == ["Acme"]
    assert _document_paths(profile.target) == [str(profile.target / "documents" / "abc.pdf")]
    assert (profile.target / "documents" / "abc.pdf").exists()


def test_moves_data_out_of_the_store_pythons_private_folder(profile):
    # Under the Store Python the app wrote to %APPDATA%\MeetingScribe as it saw it, so that's the prefix
    # stored for documents, while the files really sit in the package's LocalCache.
    store_dir = profile.store_dir("3.13")
    _make_data_dir(store_dir, project="Acme", document_root=profile.appdata_dir)

    settings = load_settings()

    assert settings.data_dir == profile.target
    assert not store_dir.exists()
    assert _projects(profile.target) == ["Acme"]
    assert _document_paths(profile.target) == [str(profile.target / "documents" / "abc.pdf")]


def test_the_most_recently_used_legacy_folder_wins(profile):
    old, recent = profile.store_dir("3.12"), profile.store_dir("3.13")
    _make_data_dir(old, project="Old")
    _make_data_dir(recent, project="Recent")
    os.utime(old / DB_FILENAME, (1_000_000, 1_000_000))

    load_settings()

    assert _projects(profile.target) == ["Recent"]
    assert old.exists()  # never deleted, just not the one moved


def test_existing_data_in_the_new_folder_is_never_replaced(profile):
    _make_data_dir(profile.target, project="Current")
    _make_data_dir(profile.appdata_dir, project="Legacy")

    settings = load_settings()

    assert settings.data_dir == profile.target
    assert settings.moved_from is None
    assert _projects(profile.target) == ["Current"]
    assert profile.appdata_dir.exists()


def test_keeps_using_the_legacy_folder_when_the_move_cannot_happen(profile):
    # Something unexpected already in the new folder: rather than start empty (and split meetings
    # across two folders), carry on with the old one and try again next launch.
    profile.target.mkdir(parents=True)
    (profile.target / "stray.txt").write_text("x")
    _make_data_dir(profile.appdata_dir, project="Legacy")

    settings = load_settings()

    assert settings.data_dir == Path(os.path.realpath(profile.appdata_dir))
    assert settings.moved_from is None
    assert _projects(settings.data_dir) == ["Legacy"]


def test_an_empty_new_folder_does_not_block_the_move(profile):
    profile.target.mkdir(parents=True)
    _make_data_dir(profile.appdata_dir, project="Legacy")

    assert load_settings().data_dir == profile.target
    assert _projects(profile.target) == ["Legacy"]


def test_rebase_document_paths_only_touches_paths_under_the_old_root(tmp_path):
    with Database(tmp_path / "test.db") as db:
        project_id = db.create_project("P").id
        old = tmp_path / "old"
        db.add_document(project_id, "a.pdf", "", source_path=str(old / "documents" / "a.pdf"))
        db.add_document(project_id, "b.pdf", "", source_path=str(tmp_path / "old-but-not-it" / "b.pdf"))
        db.add_document(project_id, "c.txt", "")

        db.rebase_document_paths(old, tmp_path / "new")

        paths = sorted(str(row["source_path"]) for row in db.list_documents(project_id))
    assert paths == sorted([
        str(tmp_path / "new" / "documents" / "a.pdf"),
        str(tmp_path / "old-but-not-it" / "b.pdf"),
        "None",
    ])
