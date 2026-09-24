import pytest

# gui.meeting_prompt imports tkinter at module level, which isn't installed in the dev sandbox but is on
# the Windows CI runner — skip here, run for real there (same as test_gui_format).
meeting_prompt = pytest.importorskip("meeting_scribe.gui.meeting_prompt")


class _FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeWinreg:
    HKEY_CURRENT_USER = object()

    def __init__(self, apps_use_light_theme):
        self._value = apps_use_light_theme

    def OpenKey(self, _root, _path):
        if self._value is None:
            raise OSError("not found")
        return _FakeKey()

    def QueryValueEx(self, _key, _name):
        return self._value, 4  # REG_DWORD


def test_dark_app_mode_is_detected():
    assert meeting_prompt.windows_prefers_dark_apps(_FakeWinreg(0))


def test_light_app_mode_is_detected():
    assert not meeting_prompt.windows_prefers_dark_apps(_FakeWinreg(1))


def test_an_unreadable_theme_setting_falls_back_to_light():
    assert not meeting_prompt.windows_prefers_dark_apps(_FakeWinreg(None))
