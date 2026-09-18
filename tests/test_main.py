import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from meeting_scribe import main as main_module


def test_fallback_log_dir_uses_appdata(monkeypatch):
    monkeypatch.setenv("APPDATA", r"C:\Users\Test\AppData\Roaming")
    assert main_module._fallback_log_dir() == Path(r"C:\Users\Test\AppData\Roaming") / "MeetingScribe"


def test_fallback_log_dir_falls_back_to_home_without_appdata(monkeypatch):
    monkeypatch.delenv("APPDATA", raising=False)
    assert main_module._fallback_log_dir() == Path.home() / ".meeting_scribe_data"


def test_show_fatal_startup_error_writes_a_log_file(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "linux")  # skip the Win32 message box path

    main_module._show_fatal_startup_error("boom: something broke")

    log_path = tmp_path / "MeetingScribe" / "startup_error.log"
    assert log_path.exists()
    assert "boom: something broke" in log_path.read_text(encoding="utf-8")


def test_show_fatal_startup_error_tolerates_a_log_write_it_cannot_make(tmp_path, monkeypatch):
    # Points APPDATA at a file (not a directory), so mkdir()/open() both fail — logging the error must
    # not itself raise, since this is already the last-resort path with nothing further to fall back to.
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    monkeypatch.setenv("APPDATA", str(blocked))
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
    monkeypatch.setattr(main_module, "load_settings", lambda: SimpleNamespace())

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
    monkeypatch.setattr(main_module, "load_settings", lambda: SimpleNamespace())

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
