"""Each project's action list: the "<Project> - Actions.json" file a separate weekly run keeps in the
project's folder next to its minutes, read and edited on the Actions page.

The file is the single source of truth, and the weekly run rewrites it — so this module never writes over
a version it didn't read: save_action_list re-reads the file first and raises ActionsConflict if it has
changed since it was loaded. merge_local_onto then carries the changes made here onto the newer list,
field by field and only on the actions that were actually changed, so a stale screen can't quietly undo
the run's work.

The format is shared with the browser tracker (Canford - Action tracker.html), which reads and writes the
same file, so the markers it uses are kept: `touched` on an action changed by hand, `edited` when its
title, owner or due date was, `user_added` on one added by hand (numbered from 900), and note-log entries
as {"date", "text"} objects (the weekly run writes "YYYY-MM-DD: text" strings instead — both are read).
Everything else in the file is kept as it was.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ACTIONS_SUFFIX = " - Actions.json"

STATUSES = ("open", "in_progress", "blocked", "closed")
STATUS_LABELS = {"open": "Open", "in_progress": "In progress", "blocked": "Blocked", "closed": "Done"}
PRIORITIES = ("flagged", "high", "normal", "low")
PRIORITY_LABELS = {"flagged": "Flagged", "high": "High", "normal": "Normal", "low": "Low"}

# Friendlier names for the categories the weekly run uses; any other category is shown title-cased.
CATEGORY_LABELS = {
    "electrical_generator_dno": "Electrical, generator & DNO",
    "flues_cremators": "Flues & cremators",
    "contract_commercial_procurement": "Contract & procurement",
    "programme_resourcing": "Programme & resourcing",
    "environmental_permitting": "Environmental",
    "documents_governance": "Documents & governance",
    "meetings_site_visits": "Meetings & site visits",
    "business_development": "Business development",
}

# The quick filters above the list.
FILTERS = ("all", "mine", "flagged", "blocked", "open")

USER_ID_BASE = 900  # actions added by hand are numbered from here, clear of the weekly run's

# "<base> - Actions<extra>.json": <extra> is empty for the real file, and something like "-DESKTOP-AB12" or
# " (1)" for a copy OneDrive left behind after a sync conflict.
_ACTIONS_FILENAME_RE = re.compile(r"^(?P<base>.+?) - Actions(?P<extra>.*)\.json$", re.IGNORECASE)
_LOG_DATE_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s*[:—-]\s*")
_LOG_PREFIX_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}\s*:\s*")


class ActionsConflict(Exception):
    """The file changed since it was loaded — the weekly run, most likely. Nothing was written."""

    def __init__(self, fresh: "LoadedActions"):
        super().__init__("The action list has changed since it was loaded")
        self.fresh = fresh


@dataclass(frozen=True)
class ActionsFile:
    project: str  # the project folder's name
    path: Path
    conflict_copy: bool = False  # a OneDrive conflict copy was found in place of the real file


@dataclass
class LoadedActions:
    data: dict  # the whole file, kept so saving round-trips everything this page doesn't show
    digest: str  # of the file's bytes as read — what save_action_list checks the file against

    @property
    def actions(self) -> list[dict]:
        return self.data["actions"]


# --- finding and reading the files ----------------------------------------------------------------


def _pick_actions_file(folder: Path) -> ActionsFile | None:
    real: list[Path] = []
    copies: list[Path] = []
    try:
        for path in folder.iterdir():
            match = _ACTIONS_FILENAME_RE.match(path.name)
            if match is None or not path.is_file():
                continue
            extra = match["extra"]
            if not extra:
                real.append(path)
            elif not extra.startswith(" - "):  # "… - Actions - SS.json" is a deliberate separate file
                copies.append(path)
    except OSError:
        return None
    if real:
        # "<folder> - Actions.json" if there's more than one.
        real.sort(key=lambda p: (_ACTIONS_FILENAME_RE.match(p.name)["base"].casefold() != folder.name.casefold(), p.name))
        return ActionsFile(folder.name, real[0])
    if copies:
        copies.sort(key=_modified, reverse=True)
        return ActionsFile(folder.name, copies[0], conflict_copy=True)
    return None


def _modified(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def find_action_files(root: Path) -> list[ActionsFile]:
    """One action list per project folder directly under `root` (the sync folder), by project name. A
    folder with no "… - Actions.json" — the Inbox, say — is left out, as is one that can't be listed."""
    try:
        folders = [child for child in root.iterdir() if child.is_dir()]
    except OSError:
        return []
    found = [picked for picked in map(_pick_actions_file, folders) if picked is not None]
    return sorted(found, key=lambda f: f.project.casefold())


def _parse(raw: bytes) -> LoadedActions:
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("actions"), list):
        raise ValueError("This isn't an action list — it has no list of actions")
    data["actions"] = [action for action in data["actions"] if isinstance(action, dict)]
    return LoadedActions(data, hashlib.sha256(raw).hexdigest())


def read_action_list(path: Path) -> LoadedActions:
    return _parse(path.read_bytes())


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _now_stamp(now: datetime | None = None) -> str:
    """The time as the browser tracker writes it into list.last_saved: UTC, milliseconds, Z-suffixed."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def last_saved(data: dict) -> str | None:
    listing = data.get("list")
    return listing.get("last_saved") if isinstance(listing, dict) else None


def save_action_list(path: Path, loaded: LoadedActions, *, now: datetime | None = None) -> LoadedActions:
    """Writes `loaded` back to `path` with list.last_saved set to now, and returns what's now on disk.
    Raises ActionsConflict, writing nothing, if the file isn't the one `loaded` was read from."""
    current = read_action_list(path)
    if current.digest != loaded.digest:
        raise ActionsConflict(current)
    data = dict(loaded.data)
    listing = dict(data["list"]) if isinstance(data.get("list"), dict) else {}
    listing["last_saved"] = _now_stamp(now)
    data["list"] = listing
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    # Written beside it and swapped in, so a crash or a full disk can't leave half a file for OneDrive to sync.
    temporary = path.with_name(path.name + ".saving")
    temporary.write_bytes(raw)
    os.replace(temporary, path)
    return LoadedActions(data, hashlib.sha256(raw).hexdigest())


def merge_local_onto(
    fresh: LoadedActions, local_actions: list[dict], changed_ids: "set | None" = None
) -> tuple[LoadedActions, int]:
    """The newer list with the changes made here reapplied, and how many actions carried a change. Only
    the actions changed here keep their status, priority, notes and note log — and their title, owner, due
    date, category and whose it is if those were edited; actions added here and not in the newer list are
    kept too. `changed_ids` says which actions were changed since the list was loaded; without it, every
    action ever marked `touched` or `user_added` counts, as in the browser tracker — including ones changed
    before the newer list was written, whose older values would then win."""
    if changed_ids is None:
        changed_ids = {a.get("id") for a in local_actions if a.get("touched") or a.get("user_added")}
    mine = {action.get("id"): action for action in local_actions if action.get("id") in changed_ids}
    merged = []
    for action in fresh.actions:
        base = {**action, "log": list(action.get("log") or [])}
        local = mine.get(action.get("id"))
        if local is not None:
            for field in ("status", "priority", "notes"):
                base[field] = local.get(field)
            if local.get("log"):
                base["log"] = list(local["log"])
            if local.get("edited"):
                for field in ("title", "owner", "due", "category", "is_mine"):
                    base[field] = local.get(field)
                base["edited"] = True
            base["touched"] = True
        merged.append(base)
    fresh_ids = {action.get("id") for action in fresh.actions}
    merged.extend(
        {**action, "log": list(action.get("log") or [])}
        for action in mine.values()
        if action.get("user_added") and action.get("id") not in fresh_ids
    )
    carried = sum(1 for action in merged if action.get("id") in changed_ids)
    return LoadedActions({**fresh.data, "actions": merged}, fresh.digest), carried


# --- showing and editing actions -------------------------------------------------------------------


def category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category) or (category or "Uncategorised").replace("_", " ").capitalize()


def categories(data: dict) -> list[str]:
    """The list's categories in the order its schema gives them, then any others its actions use."""
    schema = data.get("schema") if isinstance(data.get("schema"), dict) else {}
    ordered = [c for c in schema.get("category") or [] if isinstance(c, str)]
    for action in data.get("actions") or []:
        category = action.get("category")
        if isinstance(category, str) and category not in ordered:
            ordered.append(category)
    return ordered


def log_date(entry) -> str:
    if isinstance(entry, dict):
        return str(entry.get("date") or "")
    match = _LOG_DATE_RE.match(str(entry or ""))
    return match[1] if match else ""


def log_text(entry) -> str:
    if isinstance(entry, dict):
        return str(entry.get("text") or "")
    return _LOG_PREFIX_RE.sub("", str(entry or ""))


def matches(action: dict, *, category: str | None = None, quick_filter: str = "all", query: str = "") -> bool:
    """Whether an action shows under a category (None for all of them), quick filter and search text."""
    status, priority = action.get("status"), action.get("priority")
    if category is not None and action.get("category") != category:
        return False
    if quick_filter == "mine" and not action.get("is_mine"):
        return False
    if quick_filter == "flagged" and priority != "flagged":
        return False
    if quick_filter == "blocked" and status != "blocked":
        return False
    if quick_filter == "open" and status == "closed":
        return False
    query = query.strip().casefold()
    if query:
        source = action.get("source") if isinstance(action.get("source"), dict) else {}
        haystack = " ".join(
            str(part or "")
            for part in (action.get("title"), action.get("owner"), action.get("notes"), source.get("detail"),
                         *map(log_text, action.get("log") or []))
        )
        if query not in haystack.casefold():
            return False
    return True


def sort_key(action: dict) -> tuple:
    """Outstanding before done, then by priority, then by number — the order the list is shown in."""
    priority = action.get("priority")
    rank = PRIORITIES.index(priority) if priority in PRIORITIES else len(PRIORITIES)
    action_id = action.get("id")
    return (action.get("status") == "closed", rank, action_id if isinstance(action_id, (int, float)) else 0)


def is_due_soon(action: dict, today: date) -> bool:
    """Due within two days (or overdue) and not done yet."""
    due = action.get("due")
    if not due or action.get("status") == "closed":
        return False
    return str(due) <= (today + timedelta(days=2)).isoformat()


def next_user_id(actions: list[dict]) -> int:
    ids = [a["id"] for a in actions if isinstance(a.get("id"), int) and a["id"] >= USER_ID_BASE]
    return max(ids) + 1 if ids else USER_ID_BASE


def new_action(actions: list[dict], *, title: str, category: str, today: date, owner: str = "Unassigned") -> dict:
    return {
        "id": next_user_id(actions),
        "title": title,
        "owner": owner,
        "is_mine": True,
        "category": category,
        "priority": "normal",
        "status": "open",
        "due": None,
        "source": {"type": "added here", "date": today.isoformat(), "detail": "Added in Meeting Scribe"},
        "notes": "",
        "log": [],
        "user_added": True,
        "touched": True,
    }


def summary_counts(actions: list[dict]) -> dict[str, int]:
    outstanding = [a for a in actions if a.get("status") != "closed"]
    return {
        "total": len(actions),
        "outstanding": len(outstanding),
        "done": len(actions) - len(outstanding),
        "mine": sum(1 for a in outstanding if a.get("is_mine")),
        "flagged": sum(1 for a in outstanding if a.get("priority") == "flagged"),
        "blocked": sum(1 for a in actions if a.get("status") == "blocked"),
        "added": sum(1 for a in actions if a.get("user_added")),
    }


def record_text(data: dict) -> str:
    """The list's decisions, items closed this week and known gaps, as plain text."""
    sections = []
    decisions = [d for d in data.get("decisions_this_week") or [] if isinstance(d, dict)]
    if decisions:
        lines = ["DECISIONS RECORDED THIS WEEK", ""]
        for decision in decisions:
            source = decision.get("source") if isinstance(decision.get("source"), dict) else {}
            lines.append(f"• {decision.get('decision', '')}")
            if decision.get("rationale"):
                lines.append(f"    {decision['rationale']}")
            where = " · ".join(str(part) for part in (source.get("type"), source.get("date")) if part)
            if where:
                lines.append(f"    ({where})")
        sections.append("\n".join(lines))
    closed = [c for c in data.get("closed_this_week") or [] if isinstance(c, dict)]
    if closed:
        lines = ["CLOSED THIS WEEK", ""]
        for item in closed:
            lines.append(f"• {item.get('title', '')}")
            if item.get("evidence"):
                lines.append(f"    {item['evidence']}")
        sections.append("\n".join(lines))
    gaps = [str(g) for g in data.get("gaps") or []]
    if gaps:
        sections.append("\n".join(["GAPS IN THIS LIST", "", *(f"• {gap}" for gap in gaps)]))
    return "\n\n\n".join(sections) or "No decisions, closed items or gaps are recorded in this list."


def to_csv(actions: list[dict]) -> str:
    """The same columns the browser tracker exports."""
    out = io.StringIO()
    writer = csv.writer(out, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(["ID", "Action", "Owner", "Mine", "Added here", "Edited here", "Category", "Status",
                     "Priority", "Due", "Source", "Source date", "Notes", "Note log"])
    for action in actions:
        source = action.get("source") if isinstance(action.get("source"), dict) else {}
        log = " | ".join(
            (f"{log_date(entry)}: " if log_date(entry) else "") + log_text(entry) for entry in action.get("log") or []
        )
        writer.writerow([
            action.get("id", ""), action.get("title", ""), action.get("owner", ""),
            "Yes" if action.get("is_mine") else "No", "Yes" if action.get("user_added") else "No",
            "Yes" if action.get("edited") else "No", action.get("category", ""),
            STATUS_LABELS.get(action.get("status"), action.get("status") or ""),
            PRIORITY_LABELS.get(action.get("priority"), action.get("priority") or ""), action.get("due") or "",
            f"{source.get('type') or ''} {source.get('detail') or ''}".strip(), source.get("date") or "",
            action.get("notes") or "", log,
        ])
    return out.getvalue()
