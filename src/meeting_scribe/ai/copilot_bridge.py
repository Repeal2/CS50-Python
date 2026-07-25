"""Stands in for a direct LLM API call, for organizations whose governance policy allows Microsoft
Copilot Studio (data stays inside the org's Microsoft 365 tenant) but not a third-party AI API. There's
no supported way to call Copilot Studio directly from an unsigned desktop app's outbound HTTP, so instead
this hands a job to it over the filesystem:

1. Write the full prompt (system instructions + content, already merged into one block of text — no
   parsing required on the other end) to a file in an "Inbox" folder.
2. That folder is expected to be inside a location OneDrive/SharePoint is already syncing, so the file
   reaches the cloud without this app making any network call of its own.
3. A Power Automate flow (built and owned outside this codebase, in the Power Platform admin UI) watches
   that folder, feeds the file's content to an AI Builder "Create text with GPT" action or a Copilot
   Studio topic, and writes the result back to a matching file in a synced "Outbox" folder.
4. This module polls the Outbox folder for that file and returns its contents once it appears.

The request/response pair share a job id embedded in both file names (`{job_id}__{kind}.request.txt` /
`{job_id}__{kind}.response.txt`) so concurrent jobs (e.g. a meeting's notes and an unrelated "Ask" query)
never collide, and so whoever builds the flow can construct the output name by swapping the triggering
file's extension rather than needing to read the request body to correlate the two.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Callable


class CopilotResponseTimeout(Exception):
    """Raised when no response file shows up in the Outbox within the configured wait time — most
    likely the flow hasn't run yet (check Power Automate's run history) or isn't wired up at all."""


def submit_and_wait(
    kind: str,
    prompt_text: str,
    *,
    inbox_dir: Path,
    outbox_dir: Path,
    poll_interval_seconds: float,
    timeout_seconds: float,
    _sleep: Callable[[float], None] = time.sleep,
    _monotonic: Callable[[], float] = time.monotonic,
) -> str:
    """Drops `prompt_text` in the Inbox and blocks until a matching file appears in the Outbox, or
    `timeout_seconds` elapses. `kind` is just a label for the two file names (e.g. "notes", "search") —
    it has no effect on behavior. `_sleep`/`_monotonic` are only overridden by tests."""
    inbox_dir.mkdir(parents=True, exist_ok=True)
    outbox_dir.mkdir(parents=True, exist_ok=True)

    job_id = uuid.uuid4().hex
    request_path = inbox_dir / f"{job_id}__{kind}.request.txt"
    response_path = outbox_dir / f"{job_id}__{kind}.response.txt"
    request_path.write_text(prompt_text, encoding="utf-8")

    deadline = _monotonic() + timeout_seconds
    while True:
        if response_path.exists():
            return response_path.read_text(encoding="utf-8")
        if _monotonic() >= deadline:
            raise CopilotResponseTimeout(
                f"No response from Copilot Studio within {timeout_seconds:.0f}s — expected "
                f"{response_path.name} to appear in {outbox_dir}. Check the Power Automate flow's run "
                f"history for {request_path.name}."
            )
        _sleep(poll_interval_seconds)
