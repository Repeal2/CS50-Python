"""Turns a finished meeting transcript into structured notes and action items via Claude."""

from __future__ import annotations

import anthropic

from meeting_scribe.config import DEFAULT_NOTES_SYSTEM_PROMPT


def generate_notes(
    transcript_text: str, *, api_key: str, model: str, system_prompt: str = DEFAULT_NOTES_SYSTEM_PROMPT
) -> str:
    """Calls Claude once to turn a transcript into Markdown notes + action items. `system_prompt` is
    user-editable (Settings tab) so defaults to the recommended prompt rather than being hardcoded."""
    if not transcript_text.strip():
        raise ValueError("Cannot generate notes from an empty transcript")

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=8192,
        system=system_prompt,
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content": transcript_text}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to generate notes for this transcript")
    return next(block.text for block in response.content if block.type == "text")
