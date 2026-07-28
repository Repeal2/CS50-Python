"""Entry point. Defaults to launching the GUI; --cli subcommands are for scripting/testing without a
desktop session.
"""

from __future__ import annotations

import argparse

from meeting_scribe.config import load_settings
from meeting_scribe.storage.database import Database


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="meeting-scribe")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("gui", help="Launch the desktop control panel (default)")
    subparsers.add_parser("list-projects", help="List saved projects")

    args = parser.parse_args(argv)
    command = args.command or "gui"

    settings = load_settings()

    if command == "gui":
        from meeting_scribe.gui.app import MeetingScribeApp

        app = MeetingScribeApp(settings)
        app.mainloop()
        return 0

    with Database(settings.db_path) as db:
        if command == "list-projects":
            for project in db.list_projects():
                print(project.name)
            return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
