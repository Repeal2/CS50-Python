"""Entry point. Defaults to launching the GUI; --cli subcommands are for scripting/testing without a
desktop session.
"""

from __future__ import annotations

import argparse
import sys

from meeting_scribe.config import load_settings
from meeting_scribe.storage.database import Database


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="meeting-scribe")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("gui", help="Launch the desktop control panel (default)")

    list_projects_parser = subparsers.add_parser("list-projects", help="List saved projects")

    ask_parser = subparsers.add_parser("ask", help="Ask a question about a project")
    ask_parser.add_argument("project", help="Project name")
    ask_parser.add_argument("question", help="Question to ask")

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

        if command == "ask":
            from meeting_scribe.ai.search import ask as ask_project

            if not settings.anthropic_api_key:
                print("Set ANTHROPIC_API_KEY to ask questions.", file=sys.stderr)
                return 1
            project = db.get_project_by_name(args.project)
            if project is None:
                print(f"No such project: {args.project}", file=sys.stderr)
                return 1
            answer = ask_project(
                db, project.id, args.question,
                api_key=settings.anthropic_api_key, model=settings.anthropic_model,
            )
            print(answer)
            return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
