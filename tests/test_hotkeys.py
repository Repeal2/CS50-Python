import sys

import pytest

from meeting_scribe.hotkeys import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
    GlobalHotkeyListener,
    HotkeyCombo,
    current_modifiers,
    is_modifier_keysym,
    key_name,
)


def test_combo_label_orders_modifiers_control_alt_shift_win():
    combo = HotkeyCombo(modifiers=MOD_WIN | MOD_SHIFT | MOD_ALT | MOD_CONTROL, vk=0x53)  # 'S'
    assert combo.label == "Ctrl+Alt+Shift+Win+S"


def test_combo_label_with_a_single_modifier_and_a_digit():
    combo = HotkeyCombo(modifiers=MOD_CONTROL, vk=0x31)  # '1'
    assert combo.label == "Ctrl+1"


def test_combo_label_with_a_function_key():
    combo = HotkeyCombo(modifiers=MOD_CONTROL | MOD_ALT, vk=0x70)  # F1
    assert combo.label == "Ctrl+Alt+F1"


def test_combo_is_valid_requires_at_least_one_modifier():
    assert HotkeyCombo(modifiers=0, vk=0x53).is_valid is False
    assert HotkeyCombo(modifiers=MOD_CONTROL, vk=0x53).is_valid is True


def test_combo_is_valid_requires_a_recognized_key():
    # vk 0x08 is Backspace — not one of the letters/digits/function keys the capture UI supports.
    assert HotkeyCombo(modifiers=MOD_CONTROL, vk=0x08).is_valid is False


def test_key_name_recognizes_letters_digits_and_function_keys():
    assert key_name(0x41) == "A"  # 'A'
    assert key_name(0x5A) == "Z"  # 'Z'
    assert key_name(0x30) == "0"
    assert key_name(0x39) == "9"
    assert key_name(0x70) == "F1"
    assert key_name(0x7B) == "F12"


def test_key_name_rejects_keys_outside_the_supported_set():
    assert key_name(0x08) is None  # Backspace
    assert key_name(0x1B) is None  # Escape


def test_is_modifier_keysym_recognizes_ctrl_alt_shift_and_win():
    for keysym in ("Control_L", "Control_R", "Shift_L", "Alt_R", "Win_L", "Super_L", "Meta_R"):
        assert is_modifier_keysym(keysym) is True


def test_is_modifier_keysym_rejects_ordinary_keys():
    for keysym in ("s", "F1", "1", "Return"):
        assert is_modifier_keysym(keysym) is False


@pytest.mark.skipif(sys.platform == "win32", reason="the platform guard only triggers off Windows")
def test_current_modifiers_requires_windows():
    with pytest.raises(RuntimeError):
        current_modifiers()


@pytest.mark.skipif(sys.platform == "win32", reason="the platform guard only triggers off Windows")
def test_global_hotkey_listener_start_requires_windows():
    listener = GlobalHotkeyListener({})
    with pytest.raises(RuntimeError):
        listener.start()


@pytest.mark.skipif(sys.platform == "win32", reason="the platform guard only triggers off Windows")
def test_global_hotkey_listener_stop_is_a_no_op_if_never_started():
    listener = GlobalHotkeyListener({1: (HotkeyCombo(modifiers=MOD_CONTROL, vk=0x53), lambda: None)})
    listener.stop()  # must not raise, even though start() was never called
