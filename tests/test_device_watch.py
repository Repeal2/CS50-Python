import sys

import pytest

from meeting_scribe.audio.device_watch import device_signature


def test_device_signature_is_unknown_off_windows(monkeypatch):
    # "Can't tell" (None), never a value that could be mistaken for a change.
    monkeypatch.setattr(sys, "platform", "linux")
    assert device_signature() is None


@pytest.mark.skipif(sys.platform != "win32", reason="winmm is Windows-only")
def test_device_signature_is_stable_when_nothing_changes():
    assert device_signature() == device_signature()
