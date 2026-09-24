"""The app's look: one palette (light or dark, following Windows' app theme), a small set of fonts, and flat
ttk styles built on the cross-platform "clam" theme — the native Windows theme can't be recoloured, which
is what made the old UI look like a stock 2005 dialog. No extra dependency: everything here is plain
ttk styling, so the PyInstaller build is unchanged.

Style names used by the GUI:
    frames   — TFrame (page background), Card.TFrame (raised surface), Sidebar.TFrame
    labels   — {,Card.,Sidebar.}{TLabel, Muted.TLabel, Title.TLabel}, Heading.TLabel, Danger.TLabel,
               Card.Danger.TLabel, Brand.TLabel, Pill.TLabel, RecPill.TLabel
    buttons  — TButton, Accent.TButton, Danger.TButton, Link.TButton, Nav.TButton, NavActive.TButton,
               Record.TButton, Stop.TButton
    other    — Card.TCheckbutton, Meter.Horizontal.TProgressbar, TNotebook, Treeview, TEntry, TCombobox
"""

from __future__ import annotations

import sys
import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import ttk

_PERSONALIZE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"


def windows_prefers_dark_apps(winreg=None) -> bool:
    """Whether Windows is set to dark mode for apps (Settings > Personalization > Colors). Light off
    Windows or if the setting can't be read. `winreg` is injectable for tests."""
    if winreg is None:
        if sys.platform != "win32":
            return False
        import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PERSONALIZE_KEY) as key:
            value, _type = winreg.QueryValueEx(key, "AppsUseLightTheme")
    except OSError:
        return False
    return value == 0


@dataclass(frozen=True)
class Palette:
    dark: bool
    background: str  # page
    surface: str  # cards, text areas
    sidebar: str
    border: str
    text: str
    muted: str
    accent: str
    accent_hover: str
    accent_soft: str  # selected rows, active nav item
    danger: str
    danger_hover: str
    danger_soft: str
    success: str
    field: str
    meter_trough: str


# Neutral surfaces close to Windows 11 / Teams' own, with Teams' indigo as the accent.
LIGHT = Palette(
    dark=False,
    background="#F4F5F7",
    surface="#FFFFFF",
    sidebar="#EBEDF2",
    border="#DDE1E7",
    text="#1F2328",
    muted="#667085",
    accent="#5B5FC7",
    accent_hover="#4F52B2",
    accent_soft="#E6E7F8",
    danger="#C4314B",
    danger_hover="#A72C40",
    danger_soft="#FBE9EC",
    success="#1A7F4B",
    field="#FFFFFF",
    meter_trough="#E7EAEE",
)
DARK = Palette(
    dark=True,
    background="#1D1E21",
    surface="#26282C",
    sidebar="#17181A",
    border="#383A40",
    text="#EDEEF0",
    muted="#9CA1AA",
    accent="#7F85F5",
    accent_hover="#9499F7",
    accent_soft="#33355C",
    danger="#E5627A",
    danger_hover="#EC8194",
    danger_soft="#43262D",
    success="#4CC38A",
    field="#1F2124",
    meter_trough="#34363B",
)


class Fonts:
    """Named Tk fonts, created once the root window exists."""

    def __init__(self, root: tk.Misc):
        windows = sys.platform == "win32"
        family = "Segoe UI" if windows else tkfont.nametofont("TkDefaultFont").actual("family")
        strong = "Segoe UI Semibold" if windows else family
        strong_weight = "normal" if windows else "bold"
        mono = "Cascadia Mono" if windows and "Cascadia Mono" in tkfont.families(root) else (
            "Consolas" if windows else "TkFixedFont"
        )
        self.body = tkfont.Font(root, family=family, size=10)
        self.small = tkfont.Font(root, family=family, size=9)
        self.strong = tkfont.Font(root, family=strong, size=10, weight=strong_weight)
        self.title = tkfont.Font(root, family=strong, size=12, weight=strong_weight)
        self.heading = tkfont.Font(root, family=strong, size=18, weight=strong_weight)
        self.brand = tkfont.Font(root, family=strong, size=13, weight=strong_weight)
        self.mono = tkfont.Font(root, family=mono, size=9)


def _use_dark_title_bar(root: tk.Tk) -> None:
    """Asks Windows 10 20H1+/11 to draw this window's title bar dark, to match a dark palette."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        hwnd = int(root.wm_frame(), 16)
        enabled = ctypes.c_int(1)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (and its pre-20H1 number)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)
            ) == 0:
                return
    except (AttributeError, OSError, ValueError, tk.TclError):
        pass


def apply_theme(root: tk.Tk) -> tuple[Palette, Fonts]:
    palette = DARK if windows_prefers_dark_apps() else LIGHT
    fonts = Fonts(root)
    p = palette
    style = ttk.Style(root)
    style.theme_use("clam")
    root.configure(bg=p.background)
    if p.dark:
        # The OS frame window only exists once Tk has mapped the root, so wait for that.
        def on_first_map(_event) -> None:
            root.unbind("<Map>", binding)
            _use_dark_title_bar(root)

        binding = root.bind("<Map>", on_first_map, add="+")

    # Pixel sizes below are for 96 DPI; Tk's own scaling tells us how far off that the display is.
    scale = max(1.0, float(root.tk.call("tk", "scaling")) / (96 / 72))

    def px(value: float) -> int:
        return round(value * scale)

    style.configure(
        ".",
        background=p.background,
        foreground=p.text,
        font=fonts.body,
        bordercolor=p.border,
        lightcolor=p.border,
        darkcolor=p.border,
        troughcolor=p.meter_trough,
        focuscolor=p.accent,
        selectbackground=p.accent_soft,
        selectforeground=p.text,
        insertcolor=p.text,
        relief="flat",
    )

    # Frames
    style.configure("TFrame", background=p.background)
    style.configure("Card.TFrame", background=p.surface, borderwidth=1, relief="solid")
    style.configure("Sidebar.TFrame", background=p.sidebar)

    # Labels — one set per surface they sit on, so their background matches it.
    for prefix, background in (("", p.background), ("Card.", p.surface), ("Sidebar.", p.sidebar)):
        style.configure(f"{prefix}TLabel", background=background, foreground=p.text, font=fonts.body)
        style.configure(f"{prefix}Muted.TLabel", background=background, foreground=p.muted, font=fonts.small)
        style.configure(f"{prefix}Title.TLabel", background=background, foreground=p.text, font=fonts.title)
    style.configure("Heading.TLabel", background=p.background, foreground=p.text, font=fonts.heading)
    style.configure("Danger.TLabel", background=p.background, foreground=p.danger, font=fonts.small)
    style.configure("Card.Danger.TLabel", background=p.surface, foreground=p.danger, font=fonts.small)
    style.configure("Card.Success.TLabel", background=p.surface, foreground=p.success, font=fonts.small)
    style.configure("Brand.TLabel", background=p.sidebar, foreground=p.accent, font=fonts.brand)
    style.configure(
        "Pill.TLabel", background=p.accent_soft, foreground=p.text, font=fonts.small, padding=(px(10), px(4))
    )
    style.configure(
        "RecPill.TLabel", background=p.danger_soft, foreground=p.danger, font=fonts.strong,
        padding=(px(10), px(4)),
    )
    style.configure(
        "Banner.TLabel", background=p.danger_soft, foreground=p.text, font=fonts.body, padding=(px(12), px(8))
    )

    # Buttons
    style.configure(
        "TButton",
        background=p.surface,
        foreground=p.text,
        bordercolor=p.border,
        lightcolor=p.surface,
        darkcolor=p.surface,
        borderwidth=1,
        padding=(px(12), px(5)),
        font=fonts.body,
        focusthickness=0,
    )
    style.map(
        "TButton",
        background=[("disabled", p.background), ("pressed", p.accent_soft), ("active", p.accent_soft)],
        lightcolor=[("disabled", p.background), ("active", p.accent_soft)],
        darkcolor=[("disabled", p.background), ("active", p.accent_soft)],
        foreground=[("disabled", p.muted)],
    )
    for name, color, hover, size, padding in (
        ("Accent.TButton", p.accent, p.accent_hover, fonts.strong, (px(14), px(6))),
        ("Danger.TButton", p.danger, p.danger_hover, fonts.strong, (px(14), px(6))),
        ("Record.TButton", p.accent, p.accent_hover, fonts.title, (px(22), px(10))),
        ("Stop.TButton", p.danger, p.danger_hover, fonts.title, (px(22), px(10))),
    ):
        style.configure(
            name, background=color, foreground="#FFFFFF", bordercolor=color, lightcolor=color, darkcolor=color,
            font=size, padding=padding,
        )
        style.map(
            name,
            background=[("disabled", p.border), ("pressed", hover), ("active", hover)],
            bordercolor=[("disabled", p.border), ("active", hover)],
            lightcolor=[("disabled", p.border), ("active", hover)],
            darkcolor=[("disabled", p.border), ("active", hover)],
            foreground=[("disabled", p.muted)],
        )
    style.configure(
        "Link.TButton", background=p.surface, foreground=p.accent, bordercolor=p.surface,
        lightcolor=p.surface, darkcolor=p.surface, padding=(px(8), px(4)), font=fonts.body, width=0,
    )
    style.map(
        "Link.TButton",
        background=[("active", p.accent_soft)],
        bordercolor=[("active", p.accent_soft)],
        lightcolor=[("active", p.accent_soft)],
        darkcolor=[("active", p.accent_soft)],
        foreground=[("disabled", p.muted)],
    )
    for name, background, foreground in (
        ("Nav.TButton", p.sidebar, p.text),
        ("NavActive.TButton", p.accent_soft, p.accent),
    ):
        style.configure(
            name, background=background, foreground=foreground, bordercolor=background, lightcolor=background,
            darkcolor=background, anchor="w", padding=(px(14), px(9)), font=fonts.strong,
        )
        style.map(
            name,
            background=[("active", p.accent_soft)],
            bordercolor=[("active", p.accent_soft)],
            lightcolor=[("active", p.accent_soft)],
            darkcolor=[("active", p.accent_soft)],
        )

    # Inputs
    field_options = dict(
        fieldbackground=p.field, foreground=p.text, bordercolor=p.border, lightcolor=p.field,
        darkcolor=p.field, padding=px(5), arrowcolor=p.muted, insertcolor=p.text,
    )
    style.configure("TEntry", **field_options)
    style.configure("TCombobox", **field_options, background=p.field)
    style.configure("Placeholder.TEntry", **{**field_options, "foreground": p.muted})
    for name in ("TEntry", "Placeholder.TEntry", "TCombobox"):
        style.map(
            name,
            bordercolor=[("focus", p.accent)],
            lightcolor=[("focus", p.accent)],
            fieldbackground=[("readonly", p.field), ("disabled", p.background)],
            foreground=[("disabled", p.muted)],
            background=[("active", p.field)],
        )
    root.option_add("*TCombobox*Listbox.background", p.surface)
    root.option_add("*TCombobox*Listbox.foreground", p.text)
    root.option_add("*TCombobox*Listbox.selectBackground", p.accent_soft)
    root.option_add("*TCombobox*Listbox.selectForeground", p.text)
    root.option_add("*TCombobox*Listbox.font", fonts.body)
    root.option_add("*TCombobox*Listbox.relief", "flat")

    # A rounded, filled checkbox instead of clam's bevelled box with an X in it.
    size = px(16)
    unchecked = _checkbox_image(root, size, fill=p.field, border=p.muted, check=None)
    checked = _checkbox_image(root, size, fill=p.accent, border=p.accent, check="#FFFFFF")
    root._checkbox_images = (unchecked, checked)  # keep references alive
    style.element_create(
        "Card.Checkbutton.indicator", "image", unchecked, ("selected", checked), width=size + px(8), sticky="w"
    )
    style.layout("Card.TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
        ("Card.Checkbutton.indicator", {"side": "left", "sticky": ""}),
        ("Checkbutton.label", {"side": "left", "sticky": "nswe"}),
    ]})])
    style.configure("Card.TCheckbutton", background=p.surface, foreground=p.text, font=fonts.body, padding=(0, px(2)))
    style.map("Card.TCheckbutton", background=[("active", p.surface)])

    style.configure(
        "Meter.Horizontal.TProgressbar", background=p.success, troughcolor=p.meter_trough,
        bordercolor=p.meter_trough, lightcolor=p.success, darkcolor=p.success, thickness=px(8),
    )

    # Notebook — flat tabs on the card surface.
    style.configure("TNotebook", background=p.surface, borderwidth=0, tabmargins=(0, 0, 0, 0))
    style.configure(
        "TNotebook.Tab", background=p.surface, foreground=p.muted, bordercolor=p.surface,
        lightcolor=p.surface, darkcolor=p.surface, padding=(px(11), px(6)), font=fonts.strong,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", p.accent_soft), ("active", p.background)],
        foreground=[("selected", p.accent)],
        lightcolor=[("selected", p.accent_soft)],
        bordercolor=[("selected", p.accent_soft)],
    )

    # Lists
    style.configure(
        "Treeview", background=p.surface, fieldbackground=p.surface, foreground=p.text, bordercolor=p.surface,
        lightcolor=p.surface, darkcolor=p.surface, rowheight=px(30), font=fonts.body, borderwidth=0,
    )
    style.map("Treeview", background=[("selected", p.accent_soft)], foreground=[("selected", p.text)])
    style.configure(
        "Treeview.Heading", background=p.surface, foreground=p.muted, bordercolor=p.surface,
        lightcolor=p.surface, darkcolor=p.surface, font=fonts.small, padding=(px(6), px(4)),
    )
    style.map("Treeview.Heading", background=[("active", p.surface)])
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    # Slim, arrowless scrollbars: just a thumb in a trough the colour of the surface behind it.
    for orient, sticky in (("Vertical", "ns"), ("Horizontal", "we")):
        style.layout(
            f"{orient}.TScrollbar",
            [(f"{orient}.Scrollbar.trough", {"sticky": sticky, "children": [
                (f"{orient}.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"}),
            ]})],
        )
        style.configure(
            f"{orient}.TScrollbar", background=p.border, troughcolor=p.surface, bordercolor=p.surface,
            lightcolor=p.border, darkcolor=p.border, gripcount=0, arrowsize=px(10),
        )
        style.map(f"{orient}.TScrollbar", background=[("active", p.muted)], lightcolor=[("active", p.muted)],
                  darkcolor=[("active", p.muted)])
    style.configure("TPanedwindow", background=p.background)
    style.configure("Sash", sashthickness=px(10), gripcount=0, background=p.background)
    style.configure("TSeparator", background=p.border)

    return palette, fonts


def _checkbox_image(root: tk.Misc, size: int, *, fill: str, border: str, check: str | None) -> tk.PhotoImage:
    """Draws a rounded checkbox (optionally ticked) pixel by pixel, with simple edge antialiasing."""

    def blend(color: str, onto: str, amount: float) -> str:
        a = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(onto[i:i + 2], 16) for i in (1, 3, 5)]
        return "#" + "".join(f"{round(x * amount + y * (1 - amount)):02x}" for x, y in zip(a, b))

    def rounded_coverage(x: float, y: float, inset: float) -> float:
        radius = size * 0.22
        lo, hi = inset + radius, size - inset - radius
        dx, dy = max(lo - x, 0, x - hi), max(lo - y, 0, y - hi)
        distance = (dx * dx + dy * dy) ** 0.5
        return max(0.0, min(1.0, radius - distance + 0.5))

    def segment_distance(px_: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
        vx, vy = bx - ax, by - ay
        t = max(0.0, min(1.0, ((px_ - ax) * vx + (py - ay) * vy) / (vx * vx + vy * vy)))
        cx, cy = ax + t * vx, ay + t * vy
        return ((px_ - cx) ** 2 + (py - cy) ** 2) ** 0.5

    image = tk.PhotoImage(master=root, width=size, height=size)
    stroke = max(1.5, size / 9)
    tick = [(0.27, 0.52), (0.44, 0.69), (0.74, 0.34)]
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            cx, cy = x + 0.5, y + 0.5
            outer = rounded_coverage(cx, cy, 0.5)
            inner = rounded_coverage(cx, cy, 0.5 + max(1.0, size / 16))
            color = blend(fill, border, inner)
            if check is not None:
                d = min(
                    segment_distance(cx, cy, *[v * size for v in tick[0] + tick[1]]),
                    segment_distance(cx, cy, *[v * size for v in tick[1] + tick[2]]),
                )
                color = blend(check, color, max(0.0, min(1.0, stroke / 2 - d + 0.5)))
            row.append(color if outer > 0 else None)
        rows.append(row)
    for y, row in enumerate(rows):
        for x, color in enumerate(row):
            if color is not None:
                image.put(color, (x, y))
    return image


def style_text(widget: tk.Text, palette: Palette, font, *, surface: str | None = None) -> None:
    """Gives a plain tk.Text the same flat look as the ttk widgets around it."""
    widget.configure(
        background=surface or palette.surface,
        foreground=palette.text,
        insertbackground=palette.text,
        selectbackground=palette.accent_soft,
        selectforeground=palette.text,
        inactiveselectbackground=palette.accent_soft,
        relief="flat",
        borderwidth=0,
        highlightthickness=0,
        padx=12,
        pady=10,
        font=font,
        spacing1=2,
        spacing3=2,
    )
