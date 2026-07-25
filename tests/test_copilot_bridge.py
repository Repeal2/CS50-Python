import uuid
from unittest.mock import patch

import pytest

from meeting_scribe.ai.copilot_bridge import CopilotResponseTimeout, submit_and_wait


def test_submit_and_wait_writes_the_request_file_and_polls_until_the_response_appears(tmp_path):
    inbox_dir = tmp_path / "Inbox"
    outbox_dir = tmp_path / "Outbox"
    fixed_id = uuid.UUID(int=0)
    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        [request_path] = list(inbox_dir.iterdir())
        response_path = outbox_dir / request_path.name.replace(".request.txt", ".response.txt")
        response_path.write_text("the response", encoding="utf-8")

    with patch("meeting_scribe.ai.copilot_bridge.uuid.uuid4", return_value=fixed_id):
        result = submit_and_wait(
            "notes",
            "prompt body",
            inbox_dir=inbox_dir,
            outbox_dir=outbox_dir,
            poll_interval_seconds=1,
            timeout_seconds=10,
            _sleep=fake_sleep,
        )

    assert result == "the response"
    assert sleep_calls == [1]
    request_path = inbox_dir / f"{fixed_id.hex}__notes.request.txt"
    assert request_path.read_text(encoding="utf-8") == "prompt body"


def test_submit_and_wait_returns_immediately_if_the_response_is_already_there(tmp_path):
    inbox_dir = tmp_path / "Inbox"
    outbox_dir = tmp_path / "Outbox"
    outbox_dir.mkdir(parents=True)
    fixed_id = uuid.UUID(int=1)
    (outbox_dir / f"{fixed_id.hex}__search.response.txt").write_text("answer", encoding="utf-8")

    def fail_sleep(_seconds):
        raise AssertionError("should not have needed to poll at all")

    with patch("meeting_scribe.ai.copilot_bridge.uuid.uuid4", return_value=fixed_id):
        result = submit_and_wait(
            "search",
            "q",
            inbox_dir=inbox_dir,
            outbox_dir=outbox_dir,
            poll_interval_seconds=1,
            timeout_seconds=10,
            _sleep=fail_sleep,
        )

    assert result == "answer"


def test_submit_and_wait_times_out_without_a_response(tmp_path):
    fake_now = {"t": 0.0}

    def fake_monotonic():
        return fake_now["t"]

    def fake_sleep(seconds):
        fake_now["t"] += seconds

    with pytest.raises(CopilotResponseTimeout):
        submit_and_wait(
            "notes",
            "prompt",
            inbox_dir=tmp_path / "Inbox",
            outbox_dir=tmp_path / "Outbox",
            poll_interval_seconds=5,
            timeout_seconds=12,
            _sleep=fake_sleep,
            _monotonic=fake_monotonic,
        )
