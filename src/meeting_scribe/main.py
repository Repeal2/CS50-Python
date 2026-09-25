"""Entry point. Defaults to launching the GUI; --cli subcommands are for scripting/testing without a
desktop session.
"""

from __future__ import annotations

import argparse
import faulthandler
import os
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

from meeting_scribe.config import default_data_dir, load_settings
from meeting_scribe.storage.database import Database


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
        log_dir = default_data_dir()
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


# Kept open for the life of the process: faulthandler writes to this file descriptor from inside a crash
# handler, where nothing can be opened anymore.
_crash_log_file = None


def _install_crash_logging(log_dir: Path) -> None:
    """Leaves evidence behind for the two kinds of failure nothing else in the app can report.

    A *native* crash (an access violation inside PortAudio, CTranslate2, Tk, ...) kills the process
    before any Python except/finally runs — the window just vanishes. faulthandler hooks the OS-level
    fault itself and writes every thread's Python stack to crash.log first, which is exactly what's
    needed to tell which subsystem was in flight (a capture thread mid-read, the transcriber, a Tk
    callback) when it happened.

    An exception escaping a *background thread* doesn't crash anything, but it silently ends whatever
    that thread was doing (a hotkey listener, the screen watcher) — Tk's report_callback_exception only
    covers the GUI thread. threading.excepthook sends those to error.log alongside Tk's.

    Never raises: failing to set up logging must not be what stops the app from starting."""
    global _crash_log_file
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        _crash_log_file = open(log_dir / "crash.log", "a", encoding="utf-8")
        _crash_log_file.write(f"\n--- process {os.getpid()} started {datetime.now().isoformat()} ---\n")
        _crash_log_file.flush()
        faulthandler.enable(file=_crash_log_file, all_threads=True)
    except (OSError, RuntimeError, ValueError):
        pass

    error_log = log_dir / "error.log"

    def _log_thread_exception(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        try:
            thread_name = args.thread.name if args.thread is not None else "unknown"
            with open(error_log, "a", encoding="utf-8") as f:
                f.write(f"\n--- {datetime.now().isoformat()} (thread {thread_name}) ---\n")
                traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback, file=f)
        except OSError:
            pass

    threading.excepthook = _log_thread_exception


def _run_gui() -> int:
    """Launches the desktop app with a safety net around everything up to and including mainloop() —
    see _show_fatal_startup_error for why this needs its own handling rather than relying on
    MeetingScribeApp.report_callback_exception."""
    try:
        # Settings first: load_settings may move the data folder, which crash.log (held open from here
        # on) would otherwise block.
        settings = load_settings()
        _install_crash_logging(settings.data_dir)
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
