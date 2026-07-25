"""Turns a finished meeting transcript into structured notes and action items via Claude."""

from __future__ import annotations

import anthropic

NOTES_SYSTEM_PROMPT = """You are a meeting assistant. Given a raw meeting transcript, produce concise, \
well-structured notes formatted as Markdown with these sections, in this order:

## Summary
2-4 sentences on what the meeting was about and its outcome.

## Key Discussion Points
Bullet list of the topics covered.

## Decisions
Bullet list of decisions made. Omit this section if none were made.

## Action Items
A markdown table with columns: Owner | Action | Due date (use "unspecified" when the transcript \
doesn't say). Omit this section if there are none.

Base everything strictly on the transcript. Do not invent names, dates, or commitments that aren't \
in it."""


def generate_notes(transcript_text: str, *, api_key: str, model: str) -> str:
    """Calls Claude once to turn a transcript into Markdown notes + action items."""
    if not transcript_text.strip():
        raise ValueError("Cannot generate notes from an empty transcript")

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=8192,
        system=NOTES_SYSTEM_PROMPT,
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content": transcript_text}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to generate notes for this transcript")
    return next(block.text for block in response.content if block.type == "text")
