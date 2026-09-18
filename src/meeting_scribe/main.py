"""Entry point. Defaults to launching the GUI; --cli subcommands are for scripting/testing without a
desktop session.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

from meeting_scribe.config import load_settings
from meeting_scribe.storage.database import Database


def _fallback_log_dir() -> Path:
    """Where to write a startup failure when we can't trust load_settings() to have succeeded (it's
    frequently the thing that failed) — the same %APPDATA%\\MeetingScribe convention config._default_data_dir
    uses, computed independently so this has no dependency on config.py actually working."""
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "MeetingScribe" if appdata else Path.home() / ".meeting_scribe_data"


def _show_fatal_startup_error(detail: str) -> None:
    """Last-resort reporting for a failure so early the rest of the app's own error handling can't help:
    MeetingScribeApp.report_callback_exception only covers exceptions raised inside Tkinter's own
    callback dispatch, which doesn't exist yet if construction itself fails (a locked/corrupt database
    from another running copy of the app, a permissions error creating the data directory, Tkinter itself
    failing to initialize). A windowed PyInstaller build (see packaging/meeting_scribe.spec) has no
    console, so an uncaught exception here would otherwise print a traceback nobody can see and the
    process would simply vanish — a "crash to desktop" with no error or warning at all. Writing to a log
    file first, then trying a raw Win32 message box (not Tkinter — Tkinter may be exactly what's
    broken) is the most robust fallback available, in that order because the log write is more likely to
    succeed than a message box is to be seen."""
    try:
        log_dir = _fallback_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "startup_error.log", "a", encoding="utf-8") as f:
            f.write(f"\n--- {datetime.now().isoformat()} ---\n{detail}\n")
    except OSError:
        pass  # logging the error shouldn't itself be able to crash the app

    if sys.platform == "win32":
        import ctypes

        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                "Meeting Scribe couldn't start. Details were written to startup_error.log next to its "
                "data folder.",
                "Meeting Scribe",
                0x10,  # MB_ICONERROR
            )
        except OSError:
            pass


def _run_gui() -> int:
    """Launches the desktop app with a safety net around everything up to and including mainloop() —
    see _show_fatal_startup_error for why this needs its own handling rather than relying on
    MeetingScribeApp.report_callback_exception."""
    try:
        settings = load_settings()
        from meeting_scribe.gui.app import MeetingScribeApp

        app = MeetingScribeApp(settings)
        app.mainloop()
        return 0
    except Exception:
        _show_fatal_startup_error(traceback.format_exc())
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="meeting-scribe")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("gui", help="Launch the desktop control panel (default)")
    subparsers.add_parser("list-projects", help="List saved projects")

    args = parser.parse_args(argv)
    command = args.command or "gui"

    if command == "gui":
        return _run_gui()

    # CLI subcommands are for scripting/testing, where a bare traceback on stderr is the right behavior —
    # not the GUI's safety net above, which would otherwise hide a real error behind a log file and a
    # message box nobody watching a terminal is looking for.
    settings = load_settings()
    with Database(settings.db_path) as db:
        if command == "list-projects":
            for project in db.list_projects():
                print(project.name)
            return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
