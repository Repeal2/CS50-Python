"""System-wide keyboard shortcuts for starting/stopping a meeting without switching focus to this app's
own window — the whole point is hitting a combo while sitting in Teams, not alt-tabbing here first.

Windows-only, via the Win32 RegisterHotKey API reached through ctypes (no new dependency). A hotkey
registered with a NULL window handle is delivered as a WM_HOTKEY message to the *registering thread's*
message queue rather than any window's, so GlobalHotkeyListener runs its own dedicated thread with
nothing but a message loop — no window, no window class, no WNDPROC to write or risk conflicting with
Tk's own.

Every ctypes.windll access is deferred inside functions/methods (never at module level, and guarded by a
platform check) so this module stays importable — and its pure pieces (HotkeyCombo, format/parsing
helpers) testable — on any platform, the same convention audio.recorder and transcription.engine already
use for their own Windows-only calls.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from typing import Callable

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
# Delivers one WM_HOTKEY per physical press rather than repeating while the combo is held down.
# Requires Windows Vista or later, which is not a real constraint for anything this app targets.
MOD_NOREPEAT = 0x4000

_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012

# Virtual-key codes for the "main" (non-modifier) keys the Settings tab's capture UI accepts — letters,
# digits, and function keys cover every realistic choice for a meeting shortcut without needing a full
# Win32 virtual-key table. '0'-'9' and 'A'-'Z' happen to share ASCII's code points with their VK
# constants, which is what makes this table cheap to build.
_VK_NAMES: dict[int, str] = {
    **{0x30 + i: str(i) for i in range(10)},  # '0'..'9'
    **{0x41 + i: chr(0x41 + i) for i in range(26)},  # 'A'..'Z'
    **{0x70 + i: f"F{i + 1}" for i in range(12)},  # F1..F12
}

_MODIFIER_LABELS = ((MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift"), (MOD_WIN, "Win"))

# Tk KeyPress keysyms for a modifier key pressed on its own — not a complete combo yet, just the start of
# one. The capture UI keeps listening past these rather than treating one as the shortcut's "main" key.
_MODIFIER_KEYSYMS = frozenset(
    {
        "Control_L", "Control_R", "Shift_L", "Shift_R", "Alt_L", "Alt_R",
        "Win_L", "Win_R", "Super_L", "Super_R", "Meta_L", "Meta_R",
    }
)

_VK_CONTROL = 0x11
_VK_MENU = 0x12  # Alt
_VK_SHIFT = 0x10
_VK_LWIN = 0x5B
_VK_RWIN = 0x5C


@dataclass(frozen=True)
class HotkeyCombo:
    """One key combo, stored as the raw Win32 values RegisterHotKey itself takes — a MOD_* bitmask plus
    a virtual-key code — so nothing needs translating at registration time.

    Requires at least one modifier (see is_valid): a global hotkey with none would fire on every
    ordinary keypress anywhere on the system, not just the combo meant to trigger it."""

    modifiers: int
    vk: int

    @property
    def is_valid(self) -> bool:
        return self.modifiers != 0 and self.vk in _VK_NAMES

    @property
    def label(self) -> str:
        parts = [name for mod, name in _MODIFIER_LABELS if self.modifiers & mod]
        parts.append(_VK_NAMES.get(self.vk, f"VK_{self.vk:#04x}"))
        return "+".join(parts)


def key_name(vk: int) -> str | None:
    """The display name for a virtual-key code the capture UI can turn into a HotkeyCombo's "main" key,
    or None if this vk isn't one of the ones it accepts (letters, digits, function keys)."""
    return _VK_NAMES.get(vk)


def is_modifier_keysym(keysym: str) -> bool:
    """Whether a Tk KeyPress event's keysym is a modifier key on its own (Ctrl, Shift, Alt, the Windows
    key) rather than a combo's "main" key — see _MODIFIER_KEYSYMS."""
    return keysym in _MODIFIER_KEYSYMS


def current_modifiers() -> int:
    """Reads which modifier keys are held right now, as a MOD_* bitmask, via GetAsyncKeyState — used by
    the capture UI instead of trusting a Tk KeyPress event's own event.state, which is documented to be
    unreliable for Alt specifically on Windows (frequently reported through a menu-accelerator mode
    rather than as a plain modifier bit)."""
    if sys.platform != "win32":
        raise RuntimeError("Reading live modifier key state requires Windows (GetAsyncKeyState)")
    import ctypes

    def held(vk: int) -> bool:
        return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)

    modifiers = 0
    if held(_VK_CONTROL):
        modifiers |= MOD_CONTROL
    if held(_VK_MENU):
        modifiers |= MOD_ALT
    if held(_VK_SHIFT):
        modifiers |= MOD_SHIFT
    if held(_VK_LWIN) or held(_VK_RWIN):
        modifiers |= MOD_WIN
    return modifiers


class GlobalHotkeyListener:
    """Runs RegisterHotKey'd combos on a dedicated background thread and calls back into the app when one
    fires. Each combo is registered with hwnd=None, which posts WM_HOTKEY to *this thread's* message
    queue rather than any window's — so listening needs nothing but a plain message loop.

    Callbacks run ON THIS BACKGROUND THREAD — marshaling safely back to the Tk thread (self.after(0,
    ...), the same pattern used everywhere else in this app that crosses threads) is the caller's job.
    """

    def __init__(self, bindings: dict[int, tuple[HotkeyCombo, Callable[[], None]]]):
        """`bindings` maps an arbitrary, caller-chosen hotkey id to (combo, callback) — an id per combo is
        how Win32 tells WM_HOTKEY messages apart; this class doesn't care what the ids mean, only that
        they're distinct."""
        self._bindings = bindings
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self._failed: list[HotkeyCombo] = []
        self._started = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="global-hotkeys")

    def start(self) -> list[HotkeyCombo]:
        """Starts listening and blocks until every combo has been (attempted to be) registered, returning
        whichever ones failed — almost always because another running application already claimed that
        exact combo. Registration happens on the listener thread itself (RegisterHotKey's effect is
        thread-local), so this has to wait for it rather than just firing the thread and returning."""
        if sys.platform != "win32":
            raise RuntimeError("Global hotkeys require Windows (RegisterHotKey)")
        self._started = True
        self._thread.start()
        self._ready.wait()
        return list(self._failed)

    def stop(self) -> None:
        """Stops listening and unregisters every combo. Safe to call even if start() was never called or
        registration failed outright."""
        if not self._started:
            return
        if self._thread_id is not None:
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
        self._thread.join(timeout=5)

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        for hotkey_id, (combo, _callback) in self._bindings.items():
            if not user32.RegisterHotKey(None, hotkey_id, combo.modifiers | MOD_NOREPEAT, combo.vk):
                self._failed.append(combo)
        self._ready.set()
        try:
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == _WM_HOTKEY:
                    binding = self._bindings.get(msg.wParam)
                    if binding is not None:
                        binding[1]()
        finally:
            for hotkey_id in self._bindings:
                user32.UnregisterHotKey(None, hotkey_id)
