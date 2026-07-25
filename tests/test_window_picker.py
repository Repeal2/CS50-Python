import sys

import pytest

from meeting_scribe.screen.window_picker import WindowTarget, get_window_region, list_capturable_windows

# The functions guard on sys.platform, so what's testable differs by platform: off Windows we can only
# verify the guard raises; on Windows the guard is a no-op and we exercise the real win32gui calls.


@pytest.mark.skipif(sys.platform == "win32", reason="the platform guard only triggers off Windows")
def test_list_capturable_windows_requires_windows():
    with pytest.raises(RuntimeError):
        list_capturable_windows()


@pytest.mark.skipif(sys.platform == "win32", reason="the platform guard only triggers off Windows")
def test_get_window_region_requires_windows():
    with pytest.raises(RuntimeError):
        get_window_region(12345)


@pytest.mark.skipif(sys.platform != "win32", reason="exercises real win32gui calls")
def test_list_capturable_windows_returns_window_targets_on_windows():
    windows = list_capturable_windows()
    assert isinstance(windows, list)
    assert all(isinstance(w, WindowTarget) for w in windows)


@pytest.mark.skipif(sys.platform != "win32", reason="exercises real win32gui calls")
def test_get_window_region_returns_none_for_nonexistent_window_on_windows():
    # A window handle this large is essentially guaranteed not to exist.
    assert get_window_region(999_999_999) is None
