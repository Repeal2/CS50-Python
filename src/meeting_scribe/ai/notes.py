"""Turns a finished meeting transcript into structured notes and action items via the Copilot Studio
file-drop bridge (see ai/copilot_bridge.py) — governance doesn't allow calling an LLM API directly, so
the actual generation happens in a Power Automate/Copilot Studio flow outside this app.
"""

from __future__ import annotations

from pathlib import Path

from meeting_scribe.ai.copilot_bridge import submit_and_wait
from meeting_scribe.config import DEFAULT_NOTES_SYSTEM_PROMPT


def generate_notes(
    transcript_text: str,
    *,
    inbox_dir: Path,
    outbox_dir: Path,
    poll_interval_seconds: float,
    timeout_seconds: float,
    system_prompt: str = DEFAULT_NOTES_SYSTEM_PROMPT,
) -> str:
    """Submits a transcript to the Copilot Studio bridge and blocks until notes come back (or the wait
    times out — see CopilotResponseTimeout). `system_prompt` is user-editable (Settings tab)."""
    if not transcript_text.strip():
        raise ValueError("Cannot generate notes from an empty transcript")

    prompt_text = f"{system_prompt}\n\n---TRANSCRIPT---\n\n{transcript_text}"
    return submit_and_wait(
        "notes",
        prompt_text,
        inbox_dir=inbox_dir,
        outbox_dir=outbox_dir,
        poll_interval_seconds=poll_interval_seconds,
        timeout_seconds=timeout_seconds,
    )
