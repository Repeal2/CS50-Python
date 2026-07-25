import pytest

from meeting_scribe.screen.window_picker import get_window_region, list_capturable_windows


def test_list_capturable_windows_requires_windows():
    with pytest.raises(RuntimeError):
        list_capturable_windows()


def test_get_window_region_requires_windows():
    with pytest.raises(RuntimeError):
        get_window_region(12345)
