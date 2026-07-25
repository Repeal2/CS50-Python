"""Answers a question about a project by retrieving relevant passages (FTS5) and asking Claude to
synthesize an answer strictly from them — the "search my past meetings" feature.
"""

from __future__ import annotations

import anthropic

from meeting_scribe.storage.database import Database, SearchHit

ANSWER_SYSTEM_PROMPT = """You answer questions about a project's past meetings and documents. You are \
given retrieved passages — transcript excerpts, uploaded documents, and meeting notes — and a question. \
Answer using only the retrieved passages. If they don't contain the answer, say so plainly rather than \
guessing. Reference which meeting or document each part of your answer comes from."""


def ask(
    db: Database, project_id: int, question: str, *, api_key: str, model: str, max_hits: int = 8
) -> str:
    hits = db.search_project(project_id, question, limit=max_hits)
    if not hits:
        return "I couldn't find anything in this project matching that question."

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=ANSWER_SYSTEM_PROMPT,
        output_config={"effort": "medium"},
        messages=[
            {
                "role": "user",
                "content": f"Retrieved passages:\n\n{_render_hits(hits)}\n\nQuestion: {question}",
            }
        ],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to answer this question")
    return next(block.text for block in response.content if block.type == "text")


def _render_hits(hits: list[SearchHit]) -> str:
    return "\n\n".join(
        f"[{i}] ({hit.label}, meeting_id={hit.meeting_id})\n{hit.snippet}"
        for i, hit in enumerate(hits, start=1)
    )
