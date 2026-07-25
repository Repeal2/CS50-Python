"""Answers a question about a project by retrieving relevant passages (FTS5) and handing them, plus the
question, to the Copilot Studio file-drop bridge to synthesize an answer — see ai/copilot_bridge.py for
why this isn't a direct API call.
"""

from __future__ import annotations

from pathlib import Path

from meeting_scribe.ai.copilot_bridge import submit_and_wait
from meeting_scribe.storage.database import Database, SearchHit

ANSWER_SYSTEM_PROMPT = """You answer questions about a project's past meetings and documents. You are \
given retrieved passages — transcript excerpts, uploaded documents, and meeting notes — and a question. \
Answer using only the retrieved passages. If they don't contain the answer, say so plainly rather than \
guessing. Reference which meeting or document each part of your answer comes from."""


def ask(
    db: Database,
    project_id: int,
    question: str,
    *,
    inbox_dir: Path,
    outbox_dir: Path,
    poll_interval_seconds: float,
    timeout_seconds: float,
    max_hits: int = 8,
) -> str:
    hits = db.search_project(project_id, question, limit=max_hits)
    if not hits:
        return "I couldn't find anything in this project matching that question."

    prompt_text = (
        f"{ANSWER_SYSTEM_PROMPT}\n\n---RETRIEVED PASSAGES---\n\n{_render_hits(hits)}"
        f"\n\n---QUESTION---\n\n{question}"
    )
    return submit_and_wait(
        "search",
        prompt_text,
        inbox_dir=inbox_dir,
        outbox_dir=outbox_dir,
        poll_interval_seconds=poll_interval_seconds,
        timeout_seconds=timeout_seconds,
    )


def _render_hits(hits: list[SearchHit]) -> str:
    return "\n\n".join(
        f"[{i}] ({hit.label}, meeting_id={hit.meeting_id})\n{hit.snippet}"
        for i, hit in enumerate(hits, start=1)
    )
