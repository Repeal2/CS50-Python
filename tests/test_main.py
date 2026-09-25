import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import faulthandler
import threading

import pytest

from meeting_scribe import main as main_module


@pytest.fixture(autouse=True)
def _no_real_crash_logging(request, monkeypatch):
    # _run_gui installs process-wide hooks (faulthandler, threading.excepthook) pointing at the real
    # data folder — not something a test run should do. The test for the hook itself opts back in.
    if "real_crash_logging" not in request.keywords:
        monkeypatch.setattr(main_module, "_install_crash_logging", lambda log_dir: None)


def test_show_fatal_startup_error_writes_a_log_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(tmp_path / "MeetingScribe"))
    monkeypatch.setattr(sys, "platform", "linux")  # skip the Win32 message box path

    main_module._show_fatal_startup_error("boom: something broke")

    log_path = tmp_path / "MeetingScribe" / "startup_error.log"
    assert log_path.exists()
    assert "boom: something broke" in log_path.read_text(encoding="utf-8")


def test_show_fatal_startup_error_tolerates_a_log_write_it_cannot_make(tmp_path, monkeypatch):
    # Points the data folder at a file (not a directory), so mkdir()/open() both fail — logging the error must
    # not itself raise, since this is already the last-resort path with nothing further to fall back to.
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    monkeypatch.setenv("MEETING_SCRIBE_DATA_DIR", str(blocked))
    monkeypatch.setattr(sys, "platform", "linux")

    main_module._show_fatal_startup_error("irrelevant")  # must not raise


def test_run_gui_returns_zero_on_a_clean_run(monkeypatch):
    fake_module = ModuleType("meeting_scribe.gui.app")

    class FakeApp:
        def __init__(self, settings):
            pass

        def mainloop(self):
            pass

    fake_module.MeetingScribeApp = FakeApp
    monkeypatch.setitem(sys.modules, "meeting_scribe.gui.app", fake_module)
    monkeypatch.setattr(main_module, "load_settings", lambda: SimpleNamespace(data_dir=Path("unused")))

    assert main_module._run_gui() == 0


def test_run_gui_reports_a_fatal_error_instead_of_letting_it_escape(monkeypatch):
    # Regression test for exactly the failure mode this exists to catch: a windowed build has no console,
    # so an exception escaping all the way out of _run_gui (e.g. MeetingScribeApp construction failing
    # because the database is locked by another running copy of the app) would otherwise vanish with no
    # trace at all — a "crash to desktop" with no error or warning shown anywhere.
    fake_module = ModuleType("meeting_scribe.gui.app")

    class ExplodingApp:
        def __init__(self, settings):
            raise RuntimeError("database is locked by another instance")

    fake_module.MeetingScribeApp = ExplodingApp
    monkeypatch.setitem(sys.modules, "meeting_scribe.gui.app", fake_module)
    monkeypatch.setattr(main_module, "load_settings", lambda: SimpleNamespace(data_dir=Path("unused")))

    reported = []
    monkeypatch.setattr(main_module, "_show_fatal_startup_error", reported.append)

    assert main_module._run_gui() == 1
    assert len(reported) == 1
    assert "database is locked by another instance" in reported[0]


def test_main_dispatches_the_gui_command_through_the_safety_net(monkeypatch):
    called = []
    monkeypatch.setattr(main_module, "_run_gui", lambda: called.append(True) or 0)

    assert main_module.main(["gui"]) == 0
    assert called == [True]


def test_main_defaults_to_gui_with_no_command_given(monkeypatch):
    called = []
    monkeypatch.setattr(main_module, "_run_gui", lambda: called.append(True) or 0)

    assert main_module.main([]) == 0
    assert called == [True]


@pytest.mark.real_crash_logging
def test_install_crash_logging_enables_faulthandler_and_logs_background_thread_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    was_enabled = faulthandler.is_enabled()
    try:
        main_module._install_crash_logging(tmp_path)
        assert faulthandler.is_enabled()
        assert (tmp_path / "crash.log").exists()

        def boom():
            raise ValueError("background thread blew up")

        thread = threading.Thread(target=boom, name="screen-watcher")
        thread.start()
        thread.join()

        logged = (tmp_path / "error.log").read_text(encoding="utf-8")
        assert "background thread blew up" in logged
        assert "screen-watcher" in logged
    finally:
        faulthandler.disable()
        if was_enabled:
            faulthandler.enable()
        if main_module._crash_log_file is not None:
            main_module._crash_log_file.close()
            main_module._crash_log_file = None


@pytest.mark.real_crash_logging
def test_install_crash_logging_never_raises_when_the_folder_is_unusable(tmp_path, monkeypatch):
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")

    main_module._install_crash_logging(blocked)  # must not raise
