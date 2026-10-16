from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from dataclasses import dataclass, field, fields as dataclass_fields, replace
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from activity import format_status_line
from changelog_notes import ChangelogError, changelog_section
from config_loader import (
    Config,
    ConfigError,
    app_version,
    bundled_root,
    default_config,
    default_media_dirs,
    desktop_config_path,
    normalize_theme,
    save_config,
    try_load_config,
)
from bridge_net import (
    advertise_host,
    bind_choices,
    host_for_url,
    is_loopback_bind,
    is_tailscale,
    list_ipv4_interfaces,
    url_host_is_local,
    windows_firewall_prompt,
)
from library_report import (
    delete_inside,
    format_summary,
    health_from_albums,
    queued_next,
    research_note,
    scan_folder,
    wanted_summary,
)
from lidarr_client import LidarrClient
from lidarr_register import LidarrRegisterError, register_with_lidarr
from notifications import deliver
from crash_bundle import crash_directory, install_crash_hook, open_folder, write_crash_bundle
from favorites import daily_room
from peer_limits import apply_limits, select_under_cap, share_file_rows
from stats_report import format_stats
from backup_data import create_backup, restore_backup
from setup_check import format_setup_report, lidarr_check, local_setup_checks
from user_browse import BrowseCache, ShareIndex, is_wanted, missing_needles, profile_text
from main import setup_logging
from status_ui import DEFAULT_RECENT_LIMIT, RECENT_LIMITS, normalize_recent_limit
from updater import (
    UpdateStatus,
    check_for_update,
    download_verified,
    launch_installer,
    load_skipped_update,
    prompt_kind,
    remember_skipped_update,
    versions_equal,
    wait_for_worker_stop,
)
from match import normalize
from search_order import (
    SEARCH_ORDERS,
    default_direction,
    direction_label,
    normalize_search_direction,
    normalize_search_order,
    order_label,
)
from sharing import group_matches
from search_model import (
    SearchFile,
    SearchFilters,
    SearchLink,
    choose_downloads,
    display_name,
    display_rows,
    file_spec,
    files_from_results,
    format_duration,
    format_size,
    format_slot,
    format_speed,
    link_from_wanted,
    manual_query,
    state_label,
)
from wanted import (
    VIEW_ALBUMS,
    VIEW_ARTISTS,
    VIEW_EVERYTHING,
    VIEW_SONGS,
    VIEWS,
    WantedItem,
    artist_token,
    children_by_artist,
    choice_updates,
    filter_view,
    is_enabled,
    item_checked,
    select_all_state,
    select_all_updates,
    sort_items,
    sync_wanted_catalog,
)
from worker import Worker

log = logging.getLogger("lidarr_slsk.desktop")

DONE_TRANSFER = {"COMPLETE", "FAILED", "ABORTED"}
# 200,000 pages × 250 albums. Enough for a multi-million wanted list.
# Sync can be cancelled before it finishes; pages already saved stay on disk.
WANTED_SYNC_PAGES = 200_000
WANTED_WINDOW_MIN = 12
# Small choice edits stay on the caller so tests see them immediately.
# A select-all of a huge view is written in one transaction off the UI thread.
_SYNC_CHOICE_LIMIT = 400


def enable_windows_dpi() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def hold_single_instance() -> None:
    """Let the installer see that the app is open (Windows only)."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.CreateMutexW(None, False, "LidarrSoulseekMutex")
    except Exception:
        log.exception("Could not create the single-instance mutex")


LIGHT_MODE_HINT = "Light mode: for beginners and people who enjoy staring into the sun."
DONATE_URL = "https://github.com/sponsors/T2112"


def ui_font() -> str:
    families = set(tkfont.families())
    for name in ("Inter", "Segoe UI", "DejaVu Sans", "Liberation Sans"):
        if name in families:
            return name
    return "TkDefaultFont"


def read_ui_scale() -> float:
    raw = os.environ.get("RUMMAGARR_UI_SCALE", "")
    try:
        value = float(raw) if raw else 1.0
    except ValueError:
        value = 1.0
    if value <= 0:
        return 1.0
    return value


def icon_png_path() -> Path:
    name = Path("packaging") / "rummagarr.png"
    bundled = bundled_root() / name
    if bundled.is_file():
        return bundled
    return Path(__file__).resolve().parent / name


@dataclass(frozen=True)
class Palette:
    bg: str
    sidebar: str
    sidebar_border: str
    main: str
    card: str
    card_border: str
    fg: str
    muted: str
    faint: str
    heading: str
    field: str
    field_border: str
    button: str
    button_fg: str
    button_active: str
    primary: str
    primary_fg: str
    primary_active: str
    danger_fg: str
    danger_border: str
    nav: str
    nav_on_bg: str
    nav_on_fg: str
    accent: str
    tree_heading: str
    ok: str
    bad: str
    check_off: str
    check_on: str
    check_border: str
    check_glyph: str
    glass: str
    dot_busy: str
    dot_idle: str
    menu_bg: str
    scale: float = 1.0


_DARK = Palette(
    bg="#121418",
    sidebar="#1c2128",
    sidebar_border="#2c333c",
    main="#171b21",
    card="#20262e",
    card_border="#313842",
    fg="#e7eaee",
    muted="#8b97a3",
    faint="#6e7b88",
    heading="#9aa6b1",
    field="#171b21",
    field_border="#3c4550",
    button="#2a313a",
    button_fg="#e7eaee",
    button_active="#323b46",
    primary="#1493a8",
    primary_fg="#041316",
    primary_active="#1aa8bf",
    danger_fg="#ffb4b4",
    danger_border="#6a3b3b",
    nav="#c5ced6",
    nav_on_bg="#26343a",
    nav_on_fg="#ffffff",
    accent="#3dccb4",
    tree_heading="#1a2129",
    ok="#7dcea0",
    bad="#e07a7a",
    check_off="#171b21",
    check_on="#1493a8",
    check_border="#8b97a3",
    check_glyph="#041316",
    glass="#3dccb4",
    dot_busy="#3dccb4",
    dot_idle="#3a4652",
    menu_bg="#1c2128",
)
_LIGHT = Palette(
    bg="#f3f5f7",
    sidebar="#ffffff",
    sidebar_border="#d7dee5",
    main="#e8eef3",
    card="#ffffff",
    card_border="#d5dde4",
    fg="#1a1f24",
    muted="#5c6b76",
    faint="#6e7b88",
    heading="#4d5b66",
    field="#ffffff",
    field_border="#c5d0d8",
    button="#ffffff",
    button_fg="#1a1f24",
    button_active="#e7eef3",
    primary="#0e7c8a",
    primary_fg="#ffffff",
    primary_active="#1493a8",
    danger_fg="#9b2c2c",
    danger_border="#e2b4b4",
    nav="#314049",
    nav_on_bg="#e5f4f6",
    nav_on_fg="#0b3a40",
    accent="#0e7c8a",
    tree_heading="#f4f7f9",
    ok="#1f7a45",
    bad="#b42323",
    check_off="#ffffff",
    check_on="#0e7c8a",
    check_border="#7d8b96",
    check_glyph="#ffffff",
    glass="#0e7c8a",
    dot_busy="#0e7c8a",
    dot_idle="#b7c3cc",
    menu_bg="#ffffff",
)
THEMES = {"dark": _DARK, "light": _LIGHT}


def _widget_chain(widget: tk.Misc):
    current: tk.Misc | None = widget
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = getattr(current, "master", None)


def _theme_name_of(widget: tk.Misc) -> str:
    for current in _widget_chain(widget):
        name = getattr(current, "_theme_name", None)
        if name:
            return normalize_theme(name)
    return "dark"


def _ui_scale_of(widget: tk.Misc) -> float:
    for current in _widget_chain(widget):
        scale = getattr(current, "ui_scale", None)
        if scale:
            return float(scale)
    return read_ui_scale()


def active_palette(widget: tk.Misc) -> Palette:
    return replace(THEMES[_theme_name_of(widget)], scale=_ui_scale_of(widget))


def _sized_font(palette: Palette, size: int, *, bold: bool = False):
    points = max(8, int(round(size * palette.scale)))
    if bold:
        return (ui_font(), points, "bold")
    return (ui_font(), points)


def _paint_role(widget: tk.Misc, role: str, palette: Palette) -> None:
    if role == "sidebar":
        widget.configure(bg=palette.sidebar)
    elif role == "sidebar_border":
        widget.configure(bg=palette.sidebar_border)
    elif role == "main":
        widget.configure(bg=palette.main)
    elif role == "card":
        widget.configure(bg=palette.card, highlightbackground=palette.card_border, highlightcolor=palette.card_border)
    elif role == "canvas":
        widget.configure(background=palette.card)
    elif role == "page":
        widget.configure(background=palette.main)
    elif role in {"field", "list"}:
        if isinstance(widget, (tk.Listbox,)):
            widget.configure(
                bg=palette.field,
                fg=palette.fg,
                selectbackground=palette.nav_on_bg,
                selectforeground=palette.nav_on_fg,
            )
        else:
            widget.configure(bg=palette.field, highlightbackground=palette.field_border, highlightcolor=palette.field_border)
    elif role == "group":
        widget.configure(bg=palette.sidebar, fg=palette.faint, font=_sized_font(palette, 8, bold=True))
    elif role == "brand":
        widget.configure(bg=palette.sidebar, fg=palette.fg, font=_sized_font(palette, 11, bold=True))
    elif role == "brand_muted":
        widget.configure(bg=palette.sidebar, fg=palette.muted, font=_sized_font(palette, 9))
    elif role == "foot":
        widget.configure(bg=palette.sidebar, fg=palette.faint, font=_sized_font(palette, 9))
    elif role == "menu" and isinstance(widget, tk.Menu):
        widget.configure(
            bg=palette.menu_bg,
            fg=palette.fg,
            activebackground=palette.nav_on_bg,
            activeforeground=palette.nav_on_fg,
        )


def _paint_chrome(widget: tk.Misc, palette: Palette) -> None:
    painter = getattr(widget, "_chrome_paint", None)
    if callable(painter):
        try:
            painter(palette)
        except tk.TclError:
            pass
    else:
        role = getattr(widget, "_chrome_role", None)
        if role:
            try:
                _paint_role(widget, role, palette)
            except tk.TclError:
                pass
    try:
        children = list(widget.winfo_children())
    except tk.TclError:
        return
    for child in children:
        _paint_chrome(child, palette)


def apply_theme(root: tk.Misc, name: str | None = None) -> None:
    name = normalize_theme(name or _theme_name_of(root))
    root._theme_name = name
    palette = replace(THEMES[name], scale=_ui_scale_of(root))
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    bg = palette.main
    fg = palette.fg
    pad = (max(8, int(round(12 * palette.scale))), max(4, int(round(6 * palette.scale))))
    root.configure(background=palette.bg)
    style.configure(".", background=bg, foreground=fg, font=_sized_font(palette, 10))
    style.configure("TFrame", background=bg)
    style.configure("Main.TFrame", background=bg)
    style.configure("Card.TFrame", background=palette.card)
    style.configure("TLabel", background=bg, foreground=fg, font=_sized_font(palette, 10))
    style.configure("Muted.TLabel", background=bg, foreground=palette.muted, font=_sized_font(palette, 10))
    style.configure("Title.TLabel", background=bg, foreground=fg, font=_sized_font(palette, 16, bold=True))
    style.configure("PageTitle.TLabel", background=bg, foreground=fg, font=_sized_font(palette, 20, bold=True))
    style.configure("Card.TLabel", background=palette.card, foreground=fg, font=_sized_font(palette, 10))
    style.configure("CardMuted.TLabel", background=palette.card, foreground=palette.muted, font=_sized_font(palette, 10))
    style.configure("CardTitle.TLabel", background=palette.card, foreground=palette.heading, font=_sized_font(palette, 9, bold=True))
    style.configure("Banner.TLabel", background=palette.card, foreground=fg, font=_sized_font(palette, 11, bold=True))
    style.configure(
        "TButton",
        background=palette.button,
        foreground=palette.button_fg,
        borderwidth=1,
        padding=pad,
        bordercolor=palette.field_border,
        lightcolor=palette.field_border,
        darkcolor=palette.field_border,
        focusthickness=0,
        focuscolor=palette.field_border,
    )
    style.map(
        "TButton",
        background=[("active", palette.button_active), ("disabled", palette.button)],
        foreground=[("disabled", palette.faint)],
    )
    style.configure(
        "Start.TButton",
        background=palette.primary,
        foreground=palette.primary_fg,
        bordercolor=palette.primary,
        lightcolor=palette.primary,
        darkcolor=palette.primary,
        padding=pad,
    )
    style.map(
        "Start.TButton",
        background=[("active", palette.primary_active), ("disabled", palette.button)],
        foreground=[("disabled", palette.faint)],
    )
    style.configure(
        "Stop.TButton",
        background=palette.button,
        foreground=palette.danger_fg,
        bordercolor=palette.danger_border,
        lightcolor=palette.danger_border,
        darkcolor=palette.danger_border,
        padding=pad,
    )
    style.map(
        "Stop.TButton",
        background=[("active", palette.button_active), ("disabled", palette.button)],
        foreground=[("disabled", palette.faint)],
    )
    style.configure("TCheckbutton", background=bg, foreground=fg)
    style.map("TCheckbutton", background=[("active", bg), ("disabled", bg)])
    style.configure("TRadiobutton", background=bg, foreground=fg)
    style.map("TRadiobutton", background=[("active", bg)])
    style.layout(
        "Segment.TRadiobutton",
        [("Radiobutton.padding", {"sticky": "nswe", "children": [("Radiobutton.label", {"sticky": "nswe"})]})],
    )
    style.configure(
        "Segment.TRadiobutton",
        background=palette.field,
        foreground=palette.muted,
        padding=(max(6, int(round(10 * palette.scale))), max(3, int(round(5 * palette.scale)))),
        font=_sized_font(palette, 9),
        borderwidth=0,
    )
    style.map(
        "Segment.TRadiobutton",
        background=[("selected", palette.nav_on_bg), ("active", palette.nav_on_bg)],
        foreground=[("selected", palette.nav_on_fg)],
    )
    style.configure("TNotebook", background=bg, borderwidth=0)
    # Search results and Settings still use a notebook. Equal padding and a
    # zero expand keep the selected tab from shrinking on Windows.
    tab_pad = (max(8, int(round(12 * palette.scale))), max(4, int(round(6 * palette.scale))))
    style.configure(
        "TNotebook.Tab",
        background=palette.sidebar,
        foreground=palette.muted,
        padding=tab_pad,
        font=_sized_font(palette, 10),
        borderwidth=0,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", palette.nav_on_bg), ("active", palette.button_active)],
        foreground=[("selected", palette.nav_on_fg), ("!selected", palette.muted)],
        font=[("selected", _sized_font(palette, 10, bold=True)), ("!selected", _sized_font(palette, 10))],
        expand=[("selected", [0, 0, 0, 0]), ("!selected", [0, 0, 0, 0])],
    )
    style.configure(
        "Treeview",
        background=palette.card,
        fieldbackground=palette.card,
        foreground=fg,
        borderwidth=0,
        rowheight=max(18, int(round(22 * palette.scale))),
        font=_sized_font(palette, 10),
    )
    # Both themes use clam. The tree area has to stick to every edge or the
    # list stops short of the window and leaves an empty band underneath.
    style.layout(
        "Treeview",
        [
            (
                "Treeview.field",
                {
                    "sticky": "nswe",
                    "border": "1",
                    "children": [
                        (
                            "Treeview.padding",
                            {
                                "sticky": "nswe",
                                "children": [("Treeview.treearea", {"sticky": "nswe"})],
                            },
                        )
                    ],
                },
            )
        ],
    )
    style.configure(
        "Treeview.Heading",
        background=palette.tree_heading,
        foreground=palette.heading,
        relief="flat",
        font=_sized_font(palette, 9, bold=True),
    )
    style.map("Treeview", background=[("selected", palette.nav_on_bg)], foreground=[("selected", palette.nav_on_fg)])
    style.map("Treeview.Heading", background=[("active", palette.tree_heading)])
    style.configure(
        "Vertical.TScrollbar",
        background=palette.button,
        troughcolor=palette.main,
        borderwidth=0,
        arrowcolor=palette.muted,
    )
    style.configure("TEntry", fieldbackground=palette.field, foreground=fg, insertcolor=fg, bordercolor=palette.field_border)
    # Clam paints a readonly combobox from its own map, so configure() alone
    # leaves light text on a light field in Dark. Map every state the Search
    # order menus use, with the same face and contrast as a settings label.
    combo_font = _sized_font(palette, 10)
    style.configure(
        "TCombobox",
        fieldbackground=palette.field,
        background=palette.button,
        foreground=fg,
        arrowcolor=fg,
        bordercolor=palette.field_border,
        font=combo_font,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("disabled", palette.field), ("readonly", palette.field)],
        foreground=[("disabled", palette.muted), ("readonly", fg)],
        selectbackground=[("readonly", palette.field)],
        selectforeground=[("readonly", fg)],
        background=[("readonly", palette.button), ("active", palette.button_active)],
        arrowcolor=[("disabled", palette.muted), ("readonly", fg)],
    )
    root.option_add("*TCombobox*Listbox.background", palette.field)
    root.option_add("*TCombobox*Listbox.foreground", fg)
    root.option_add("*TCombobox*Listbox.font", combo_font)
    root.option_add("*TCombobox*Listbox.selectBackground", palette.nav_on_bg)
    root.option_add("*TCombobox*Listbox.selectForeground", palette.nav_on_fg)
    _paint_chrome(root, palette)
    for menu in getattr(root, "_theme_menus", ()) or ():
        try:
            _paint_role(menu, "menu", palette)
        except tk.TclError:
            pass


def _merge_files(existing: list[SearchFile], extra: list[SearchFile]) -> list[SearchFile]:
    seen = {item.id for item in existing}
    merged = list(existing)
    for item in extra:
        if item.id in seen or not item.filename:
            continue
        seen.add(item.id)
        merged.append(item)
    return merged


def format_progress(percent) -> str:
    if percent is None:
        return ""
    try:
        value = max(0.0, min(100.0, float(percent)))
    except (TypeError, ValueError):
        return ""
    cells = 10
    filled = int(round(value / 100 * cells))
    return "█" * filled + "░" * (cells - filled) + f" {round(value)}%"


def format_bytes(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


def format_when(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        stamp = datetime.fromisoformat(iso)
    except ValueError:
        return str(iso)
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone()
    return stamp.strftime("%Y-%m-%d %H:%M")


def scrolled_tree(
    parent: tk.Misc,
    columns: tuple[tuple[str, str, int, bool], ...],
    *,
    frame_style: str = "TFrame",
) -> tuple[ttk.Frame, ttk.Treeview]:
    frame = ttk.Frame(parent, style=frame_style)
    names = [column[0] for column in columns]
    tree = ttk.Treeview(frame, columns=names, show="headings", selectmode="browse")
    scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.grid(row=0, column=0, sticky="nsew")
    scroll.grid(row=0, column=1, sticky="ns")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    scale = _ui_scale_of(parent)
    for name, heading, width, stretch in columns:
        tree.heading(name, text=heading)
        tree.column(name, width=max(40, int(round(width * scale))), minwidth=40, anchor="w", stretch=stretch)
    return frame, tree


@dataclass(frozen=True)
class BehaviorField:
    key: str
    label: str
    kind: str
    unit: str
    help: str
    low: float | None = None
    high: float | None = None
    restore: bool = True
    choices: tuple[tuple[str, str], ...] = ()


BEHAVIOR_FIELDS: tuple[BehaviorField, ...] = (
    BehaviorField("preferred_extensions", "Formats, in order", "csv", "", "Tried in this order. One format is used per album. Example: mp3, flac, wav."),
    BehaviorField(
        "artist_cooldown_days",
        "Artist retry period",
        "float",
        "days",
        "After a visit, skip that artist for this many days. 0 lets the worker pick them again on a later pass. This is not the short wait between artists.",
        0,
        90,
    ),
    BehaviorField(
        "cycle_seconds",
        "Wait before next artist",
        "int",
        "seconds",
        "How long to wait after one artist before starting the next artist that is ready. 0 starts that artist immediately, with no 'Waiting for retry period' notice. This is separate from Artist retry period.",
        0,
        7200,
    ),
    BehaviorField(
        "search_order",
        "Search order",
        "choice",
        "",
        "Which artists the worker searches first. The Wanted list uses the same order. Missing tracks, ascending, finishes small gaps before an artist missing hundreds of files.",
        choices=SEARCH_ORDERS,
    ),
    BehaviorField(
        "search_order_direction",
        "Search direction",
        "choice",
        "",
        "Ascending is A–Z, smallest, oldest, or least recently tried. Descending is Z–A, largest, newest, or most recently tried. Lidarr order descending keeps monitored and recently added artists first.",
        choices=(("asc", "Ascending"), ("desc", "Descending")),
    ),
    BehaviorField("convert_to_mp3", "Convert to MP3", "bool", "", "Convert FLAC and WAV to MP3 with ffmpeg before Lidarr imports them."),
    BehaviorField("convert_bitrate", "Convert bitrate", "int", "kbps", "Bitrate ffmpeg uses for the MP3.", 128, 320),
    BehaviorField(
        "confirm_download_all",
        "Ask before download all wanted",
        "bool",
        "",
        "Download all wanted opens a checklist first. Turn this off to queue those files without asking. Turn it back on to show the popup again.",
    ),
    BehaviorField(
        "max_albums_per_run",
        "Albums per run",
        "int",
        "albums",
        "Pause new album searches after this many real searches, until the next day. 0 does not cap the run. Skipped albums are not counted.",
        0,
        5000,
    ),
    BehaviorField(
        "wanted_tag",
        "Only this Lidarr tag",
        "text",
        "",
        "Leave blank to search every wanted artist. Otherwise only artists with this tag are searched. Sync Wanted again after changing tags in Lidarr.",
    ),
    BehaviorField("focus_one_artist", "One artist at a time", "bool", "", "Stay on a single artist, then move on."),
    BehaviorField("concurrent_albums", "Albums at once", "int", "", "How many albums to search or download together. 1 is one after another.", 1, 8),
    BehaviorField(
        "lidarr_track_concurrency",
        "Lidarr requests at once",
        "int",
        "",
        "How many Lidarr track lookups to run together during Sync. 1 waits for each request.",
        1,
        8,
    ),
    BehaviorField("search_wait_seconds", "Search wait", "float", "seconds", "How long to wait for Soulseek results before scoring them.", 1, 90),
    BehaviorField("max_albums_per_cycle", "Albums per pass", "int", "", "Album searches started in one pass.", 1, 20),
    BehaviorField("max_missing_pages", "Wanted pages per pass", "int", "pages", "Lidarr wanted/missing pages read each pass. Each page is 250 albums.", 1, 200),
    BehaviorField("fill_missing_tracks", "Fill missing songs", "bool", "", "Search Soulseek for individual songs on albums Lidarr already has in part."),
    BehaviorField("max_tracks_per_cycle", "Songs per pass", "int", "", "Individual song searches started in one pass. 0 turns song fill off for that pass.", 0, 50),
    BehaviorField("retry_hours", "Retry a miss after", "int", "hours", "How long to wait before searching again for an album or song that did not match.", 0, 168),
    BehaviorField("min_filename_ratio", "Filename match", "float", "0–1", "How close a filename must be to the Lidarr title. Higher is stricter.", 0.2, 0.95),
    BehaviorField("track_count_tolerance", "Track count slack", "int", "tracks", "How many extra or missing files an album folder may have.", 0, 15),
    BehaviorField("min_mp3_bitrate", "Minimum MP3 bitrate", "int", "kbps", "Reject MP3s below this bitrate when Soulseek reports one.", 96, 320),
    BehaviorField("delete_sources_after_import", "Delete source after import", "bool", "", "Remove the original FLAC or WAV from Incomplete after Lidarr is asked to import the MP3."),
    BehaviorField("clean_incomplete_on_fail", "Clean up failed downloads", "bool", "", "Delete leftover files in Incomplete when a download fails or is cancelled."),
    BehaviorField("incomplete_max_age_hours", "Delete old Incomplete files", "float", "hours", "Also delete Incomplete files older than this. 0 turns the sweep off.", 0, 168),
    BehaviorField("ignored_users", "Ignored Soulseek users", "csv", "", "Comma-separated usernames the worker will not download from. Leave blank to ignore nobody."),
    BehaviorField("download_timeout_minutes", "Download timeout", "int", "minutes", "Give up on an album download after this long.", 5, 720),
    BehaviorField("status_bind", "Status page address", "text", "", "Address the browser status page listens on. 127.0.0.1 is this PC only. Restart to apply.", restore=True),
    BehaviorField("status_port", "Status page port", "int", "", "Port for the browser status page. Restart to apply.", 1, 65535),
    BehaviorField("check_updates", "Check for updates on launch", "bool", "", "Look for a newer GitHub Release when the app opens."),
    BehaviorField("state_db", "Database file", "text", "", "SQLite file for progress and wanted checkboxes. Restart to apply.", restore=False),
    BehaviorField("log_file", "Log file", "text", "", "Where the worker writes its log. Restart to apply.", restore=False),
    BehaviorField(
        "follow_metadata_profile",
        "Follow Lidarr metadata profile",
        "bool",
        "",
        "When this is on, a live, compilation, single, or bootleg album is searched when that artist's Lidarr metadata profile allows it.",
    ),
    BehaviorField("allow_live", "Include live albums", "bool", "", "Search live albums even when the metadata profile does not list them."),
    BehaviorField("allow_compilation", "Include compilations", "bool", "", "Search compilation albums even when the metadata profile does not list them."),
    BehaviorField("allow_single", "Include singles", "bool", "", "Search singles even when the metadata profile does not list them."),
    BehaviorField("allow_bootleg", "Include bootlegs", "bool", "", "Search bootlegs even when the metadata profile does not list them."),
    BehaviorField(
        "crash_bundle",
        "Save a local crash bundle",
        "bool",
        "",
        "When this is on, an unexpected error writes a zip next to the log. The zip stays on this PC and is not uploaded. Passwords and API keys are removed.",
    ),
    BehaviorField(
        "suggest_favorites",
        "Suggest favorite users",
        "bool",
        "",
        "When this is on, a user who has finished 3 downloads is offered in one prompt. Add puts them in Priority users. Turn this off to stop those prompts. Don't ask again on the prompt turns this off.",
    ),
)

_PRIMARY_BEHAVIOR = frozenset(
    {
        "search_order",
        "search_order_direction",
        "preferred_extensions",
        "artist_cooldown_days",
        "cycle_seconds",
        "convert_to_mp3",
        "convert_bitrate",
        "confirm_download_all",
        "suggest_favorites",
    }
)


_RESTART_FIELDS = frozenset(
    {
        "lidarr_url",
        "lidarr_api_key",
        "lidarr_scan_path",
        "slsk_username",
        "slsk_password",
        "listen_port",
        "obfuscated_port",
        "enable_upnp",
        "download_dir",
        "complete_dir",
        "state_db",
        "log_file",
        "status_bind",
        "status_port",
    }
)
_LAUNCH_FIELDS = frozenset({"start_on_launch", "check_updates"})
_IMMEDIATE_FIELDS = frozenset(
    {"theme", "confirm_download_all", "search_order", "search_order_direction", "suggest_favorites"}
)
_BRIDGE_FIELDS = frozenset(
    {
        "bridge_enabled",
        "bridge_host",
        "bridge_port",
        "bridge_api_key",
        "bridge_username",
        "bridge_password",
        "bridge_category",
        "bridge_advertise_host",
        "bridge_remote_path",
    }
)
_SHARE_LIVE_FIELDS = frozenset(
    {
        "upload_slots",
        "max_queued_files_per_user",
        "max_queued_mb_per_user",
        "upload_speed_kbps",
        "per_user_speed_kbps",
        "auto_ban_over_queue",
        "require_peer_share",
        "min_peer_share_files",
        "priority_users",
        "ignored_users",
        "pause_uploads",
    }
)
_SHARE_RESCAN_FIELDS = frozenset({"share_dir", "share_dirs"})

SHARING_FIELDS: tuple[BehaviorField, ...] = (
    BehaviorField("upload_slots", "Upload slots", "int", "", "How many files can upload at the same time.", 1, 20),
    BehaviorField(
        "max_queued_files_per_user",
        "Queued files per user",
        "int",
        "files",
        "Refuse a new file when this user already has this many queued or uploading. Priority users get four times this.",
        1,
        10000,
    ),
    BehaviorField(
        "max_queued_mb_per_user",
        "Queued size per user",
        "int",
        "MB",
        "Refuse a new file when this user's queue would pass this size. 2048 MB is 2 GB. Priority users get four times this.",
        1,
        1048576,
    ),
    BehaviorField(
        "upload_speed_kbps",
        "Global upload speed",
        "int",
        "KB/s",
        "Cap for every upload together. 0 is unlimited.",
        0,
        1000000,
    ),
    BehaviorField(
        "per_user_speed_kbps",
        "Per-user upload speed",
        "int",
        "KB/s",
        "Cap for one user. 0 is unlimited. Extra uploads from that user pause until they are under the cap.",
        0,
        1000000,
    ),
    BehaviorField(
        "auto_ban_over_queue",
        "Ban users who exceed the queue",
        "bool",
        "",
        "A file past the queue limit is always refused. This also adds the user to the ignore list.",
    ),
    BehaviorField(
        "require_peer_share",
        "Only share with users who share files",
        "bool",
        "",
        "Refuse uploads to someone who reports fewer shared files than the minimum. Soulseek reports a file count, not a size in MB.",
    ),
    BehaviorField("min_peer_share_files", "Minimum files the peer shares", "int", "files", "Used when the option above is on.", 1, 10000000),
    BehaviorField(
        "priority_users",
        "Priority users",
        "csv",
        "",
        "These users are uploaded ahead of others and get a larger queue. They still have a limit.",
    ),
)


def _display_number(value) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _config_same(left, right) -> bool:
    if isinstance(left, Path) or isinstance(right, Path):
        return Path(left) == Path(right)
    if isinstance(left, set) or isinstance(right, set):
        return set(left) == set(right)
    return left == right


def settings_apply_message(old: Config, new: Config) -> str:
    """Tell the user which saved values wait for a restart."""
    changed = {
        item.name
        for item in dataclass_fields(Config)
        if not _config_same(getattr(old, item.name), getattr(new, item.name))
    }
    if not changed:
        return "Saved."
    parts = ["Saved."]
    if changed & _RESTART_FIELDS:
        parts.append("Stop and start the worker to apply connection, folder, and status-page changes.")
    if changed & _SHARE_LIVE_FIELDS:
        parts.append("Upload limits apply immediately.")
    if changed & _SHARE_RESCAN_FIELDS:
        parts.append("Shared folders apply after Rescan, or the next time the worker logs in.")
    if changed & _BRIDGE_FIELDS:
        parts.append("The Lidarr bridge uses the new listen address, port, and key now.")
    if changed & {"dry_run"}:
        parts.append("Dry run applies on the next search.")
    if changed & {"quality_upgrades", "quality_upgrade_hours"}:
        parts.append("Quality upgrades apply on the next pass.")
    skipped = (
        _RESTART_FIELDS
        | _LAUNCH_FIELDS
        | _SHARE_LIVE_FIELDS
        | _SHARE_RESCAN_FIELDS
        | _IMMEDIATE_FIELDS
        | _BRIDGE_FIELDS
        | {"dry_run", "quality_upgrades", "quality_upgrade_hours"}
    )
    if changed - skipped:
        parts.append("Search and download settings apply on the next pass.")
    if changed & {"search_order", "search_order_direction"}:
        parts.append("Search order applies to the next artist and the Wanted list.")
    if changed & _LAUNCH_FIELDS:
        parts.append("The update check and start-on-launch apply the next time the app opens.")
    return " ".join(parts)


class SettingsForm(ttk.Frame):
    """First-run and later edits of the Lidarr and Soulseek settings."""

    def __init__(
        self,
        master: tk.Misc,
        config_path: Path,
        cfg: Config | None,
        on_saved,
        on_cancel,
        store=None,
        worker=None,
        on_theme=None,
        heading: str = "Rummagarr setup",
    ) -> None:
        super().__init__(master, padding=16)
        self.config_path = config_path
        self.base = cfg
        self.on_saved = on_saved
        self.on_cancel = on_cancel
        self.store = store
        self.worker = worker
        self.on_theme = on_theme
        self.heading = heading
        source = cfg if cfg is not None else default_config()
        download, complete = default_media_dirs()
        self.url_var = tk.StringVar(value=(cfg.lidarr_url if cfg else "http://127.0.0.1:8686"))
        self.api_var = tk.StringVar(value=(cfg.lidarr_api_key if cfg else ""))
        self.user_var = tk.StringVar(value=(cfg.slsk_username if cfg else ""))
        self.password_var = tk.StringVar(value=(cfg.slsk_password if cfg else ""))
        self.download_var = tk.StringVar(value=str(cfg.download_dir if cfg else download))
        self.complete_var = tk.StringVar(value=str(cfg.complete_dir if cfg else complete))
        self.scan_var = tk.StringVar(value=(cfg.lidarr_scan_path if cfg else str(complete)))
        self.listen_var = tk.StringVar(value=str(cfg.listen_port if cfg else 2234))
        self.obfs_var = tk.StringVar(value=str(cfg.obfuscated_port if cfg else 2235))
        self.upnp_var = tk.BooleanVar(value=(cfg.enable_upnp if cfg else True))
        self.launch_var = tk.BooleanVar(value=(cfg.start_on_launch if cfg else True))
        self.fields: dict[str, tk.Variable] = {}
        for spec in BEHAVIOR_FIELDS:
            self.fields[spec.key] = self._variable(spec, getattr(source, spec.key))
        for spec in SHARING_FIELDS:
            self.fields[spec.key] = self._variable(spec, getattr(source, spec.key))
        self.updates_var = self.fields["check_updates"]
        if store is not None:
            saved_limit = store.get_meta("gui_recent_limit")
            self.history_var: tk.StringVar | None = tk.StringVar(
                value=str(normalize_recent_limit(saved_limit or DEFAULT_RECENT_LIMIT))
            )
        else:
            self.history_var = None
        self.theme_var = tk.StringVar(value=normalize_theme(getattr(source, "theme", "dark")))
        self.instances = [dict(item) for item in (getattr(source, "lidarr_instances", None) or [])]
        if not self.instances:
            self.instances = [
                {
                    "name": getattr(source, "lidarr_active", "") or "Home",
                    "url": self.url_var.get().strip().rstrip("/"),
                    "api_key": self.api_var.get().strip(),
                    "scan_path": self.scan_var.get().strip(),
                }
            ]
        active_name = getattr(source, "lidarr_active", "") or self.instances[0]["name"]
        if not any(item["name"] == active_name for item in self.instances):
            active_name = self.instances[0]["name"]
        self.instance_var = tk.StringVar(value=active_name)
        self._shown_instance = active_name
        self.bridge_enabled_var = tk.BooleanVar(value=bool(getattr(source, "bridge_enabled", False)))
        self.bridge_host_var = tk.StringVar(value=str(getattr(source, "bridge_host", "") or "127.0.0.1"))
        self.bridge_advertise_var = tk.StringVar(value=str(getattr(source, "bridge_advertise_host", "") or ""))
        self.bridge_remote_var = tk.StringVar(value=str(getattr(source, "bridge_remote_path", "") or ""))
        self.bridge_port_var = tk.StringVar(value=str(getattr(source, "bridge_port", 8788) or 8788))
        self.bridge_key_var = tk.StringVar(value=str(getattr(source, "bridge_api_key", "") or ""))
        self.bridge_user_var = tk.StringVar(value=str(getattr(source, "bridge_username", "") or "rummagarr"))
        self.bridge_pass_var = tk.StringVar(value=str(getattr(source, "bridge_password", "") or ""))
        self.bridge_category_var = tk.StringVar(value=str(getattr(source, "bridge_category", "") or "lidarr"))
        self.dry_run_var = tk.BooleanVar(value=bool(getattr(source, "dry_run", False)))
        self.upgrade_var = tk.BooleanVar(value=bool(getattr(source, "quality_upgrades", False)))
        self.upgrade_hours_var = tk.StringVar(value=str(getattr(source, "quality_upgrade_hours", 24) or 24))
        self.discord_var = tk.StringVar(value=str(getattr(source, "notify_discord_url", "") or ""))
        self.ntfy_var = tk.StringVar(value=str(getattr(source, "notify_ntfy_url", "") or ""))
        self.notify_import_var = tk.BooleanVar(value=bool(getattr(source, "notify_on_import", True)))
        self.notify_review_var = tk.BooleanVar(value=bool(getattr(source, "notify_on_review", True)))
        self.plex_url_var = tk.StringVar(value=str(getattr(source, "plex_url", "") or ""))
        self.plex_token_var = tk.StringVar(value=str(getattr(source, "plex_token", "") or ""))
        self.plex_section_var = tk.StringVar(value=str(getattr(source, "plex_section", "") or ""))
        self.jellyfin_url_var = tk.StringVar(value=str(getattr(source, "jellyfin_url", "") or ""))
        self.jellyfin_key_var = tk.StringVar(value=str(getattr(source, "jellyfin_api_key", "") or ""))
        self.message_var = tk.StringVar(value="")
        self._choice_display: dict[str, tk.StringVar] = {}
        self._behavior_canvas: tk.Canvas | None = None
        paths = []
        if source.share_dir:
            paths.append(source.share_dir)
        for extra in source.share_dirs:
            if extra and extra not in paths:
                paths.append(extra)
        self.share_paths = paths
        self.share_count_var = tk.StringVar(value=self._share_count_text())
        self._build()

    def _variable(self, spec: BehaviorField, value) -> tk.Variable:
        if spec.kind == "bool":
            return tk.BooleanVar(value=bool(value))
        if spec.kind == "csv":
            if spec.key in {"ignored_users", "priority_users"}:
                text = ", ".join(sorted(value))
            else:
                text = ", ".join(value)
            return tk.StringVar(value=text)
        if spec.kind in {"int", "float"}:
            return tk.StringVar(value=_display_number(value))
        if spec.kind == "choice":
            if spec.key == "search_order":
                return tk.StringVar(value=normalize_search_order(value))
            if spec.key == "search_order_direction":
                return tk.StringVar(value=normalize_search_direction(value))
            return tk.StringVar(value="" if value is None else str(value))
        return tk.StringVar(value="" if value is None else str(value))

    def _set_field(self, spec: BehaviorField, value) -> None:
        variable = self.fields[spec.key]
        if spec.kind == "bool":
            variable.set(bool(value))
            return
        if spec.kind == "csv":
            if spec.key in {"ignored_users", "priority_users"}:
                variable.set(", ".join(sorted(value)))
            else:
                variable.set(", ".join(value))
            return
        if spec.kind in {"int", "float"}:
            variable.set(_display_number(value))
            return
        if spec.kind == "choice":
            key = normalize_search_order(value) if spec.key == "search_order" else str(value or "")
            if spec.key == "search_order_direction":
                key = normalize_search_direction(value)
            variable.set(key)
            labels = dict(spec.choices)
            display = getattr(self, "_choice_display", {}).get(spec.key)
            if display is not None:
                display.set(labels.get(key, key))
            return
        variable.set("" if value is None else str(value))

    def _build(self) -> None:
        ttk.Label(self, text=self.heading, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            self,
            text="Connection holds the login and folders. Behavior holds search and conversion. Bridge is the Lidarr indexer. Sharing holds what other users can download.",
            style="Muted.TLabel",
            wraplength=900,
        ).grid(row=1, column=0, sticky="w", pady=(4, 8))
        theme = ttk.Frame(self)
        theme.grid(row=2, column=0, sticky="w", pady=(0, 8))
        ttk.Label(theme, text="Theme").pack(side="left")
        ttk.Radiobutton(
            theme, text="Dark", value="dark", variable=self.theme_var, command=self._on_theme_choice
        ).pack(side="left", padx=(12, 0))
        ttk.Radiobutton(
            theme, text="Light", value="light", variable=self.theme_var, command=self._on_theme_choice
        ).pack(side="left", padx=(12, 0))
        ttk.Label(theme, text=LIGHT_MODE_HINT, style="Muted.TLabel").pack(side="left", padx=(8, 0))
        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=3, column=0, sticky="nsew")
        connection = ttk.Frame(self.notebook, padding=12)
        behavior = ttk.Frame(self.notebook, padding=(4, 8, 4, 4))
        sharing = ttk.Frame(self.notebook, padding=12)
        blocked = ttk.Frame(self.notebook, padding=12)
        bridge = ttk.Frame(self.notebook, padding=(4, 8, 4, 4))
        self.notebook.add(connection, text="Connection")
        self.notebook.add(behavior, text="Behavior")
        self.notebook.add(sharing, text="Sharing")
        self.notebook.add(blocked, text="Blocklist")
        self.notebook.add(bridge, text="Bridge")
        self._build_connection(connection)
        self._build_behavior(behavior)
        self._build_sharing(sharing)
        self._build_blocklist(blocked)
        self._build_bridge(bridge)
        ttk.Label(self, textvariable=self.message_var, wraplength=900).grid(row=4, column=0, sticky="w", pady=(8, 4))
        buttons = ttk.Frame(self)
        buttons.grid(row=5, column=0, sticky="w")
        ttk.Button(buttons, text="Test Lidarr", command=self.test_lidarr).pack(side="left")
        ttk.Button(buttons, text="Save", style="Start.TButton", command=self.save).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Restore defaults", command=self.restore_defaults).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Cancel", command=self.on_cancel).pack(side="left", padx=(8, 0))
        ttk.Label(
            self,
            text="API key: Lidarr → Settings → General → Security. Restore defaults does not clear the login or save until you click Save.",
            style="Muted.TLabel",
            wraplength=900,
        ).grid(row=6, column=0, sticky="w", pady=(12, 0))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

    def _on_theme_choice(self) -> None:
        name = normalize_theme(self.theme_var.get())
        self.theme_var.set(name)
        if self.on_theme is not None:
            self.on_theme(name)
            return
        apply_theme(self.winfo_toplevel(), name)

    def _build_connection(self, parent: ttk.Frame) -> None:
        rows = (
            (0, "Lidarr URL", self.url_var, False),
            (1, "Lidarr API key", self.api_var, False),
            (2, "Soulseek username", self.user_var, False),
            (3, "Soulseek password", self.password_var, True),
        )
        for row, label, variable, secret in rows:
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(parent, textvariable=variable, width=52, show="*" if secret else "").grid(
                row=row, column=1, columnspan=2, sticky="ew", pady=4
            )
        self._path_row(parent, 4, "Incomplete folder", self.download_var)
        self._path_row(parent, 5, "Complete folder", self.complete_var)
        self._path_row(parent, 6, "Lidarr scan path", self.scan_var)
        ttk.Label(parent, text="Listen port").grid(row=7, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.listen_var, width=10).grid(row=7, column=1, sticky="w", pady=4)
        ttk.Label(parent, text="Soulseek listen port. Restart to apply.", style="Muted.TLabel").grid(
            row=8, column=1, columnspan=2, sticky="w"
        )
        ttk.Label(parent, text="Obfuscated port").grid(row=9, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.obfs_var, width=10).grid(row=9, column=1, sticky="w", pady=4)
        ttk.Label(parent, text="Must be different from the listen port. Restart to apply.", style="Muted.TLabel").grid(
            row=10, column=1, columnspan=2, sticky="w"
        )
        ttk.Checkbutton(parent, text="Try to open the listen ports with UPnP", variable=self.upnp_var).grid(
            row=11, column=0, columnspan=3, sticky="w", pady=(8, 2)
        )
        ttk.Checkbutton(parent, text="Start downloading when the app opens", variable=self.launch_var).grid(
            row=12, column=0, columnspan=3, sticky="w", pady=2
        )
        ttk.Label(
            parent,
            text="Formats, the artist retry period, the wait before the next artist, and conversion are at the top of Behavior. Everything else is under Advanced. Uploads are on Sharing.",
            style="Muted.TLabel",
            wraplength=760,
        ).grid(row=13, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Label(parent, text="Lidarr instance").grid(row=14, column=0, sticky="w", pady=(12, 4))
        names = [item["name"] for item in self.instances]
        self._instance_box = ttk.Combobox(parent, textvariable=self.instance_var, values=names, state="readonly", width=24)
        self._instance_box.grid(row=14, column=1, sticky="w", pady=(12, 4))
        self._instance_box.bind("<<ComboboxSelected>>", self._on_instance_picked)
        ttk.Button(parent, text="Add instance", command=self._add_instance).grid(row=14, column=2, sticky="w", padx=(8, 0))
        ttk.Label(
            parent,
            text="Each instance keeps its own URL, API key, and scan path. The selected one is the one Rummagarr uses.",
            style="Muted.TLabel",
            wraplength=760,
        ).grid(row=15, column=0, columnspan=3, sticky="w")
        parent.columnconfigure(1, weight=1)

    def _refresh_instance_names(self) -> None:
        box = getattr(self, "_instance_box", None)
        if box is not None:
            box.configure(values=[item["name"] for item in self.instances])

    def _store_instance(self, name: str) -> None:
        name = (name or "").strip() or "Home"
        row = {
            "name": name,
            "url": self.url_var.get().strip().rstrip("/"),
            "api_key": self.api_var.get().strip(),
            "scan_path": self.scan_var.get().strip(),
        }
        for item in self.instances:
            if item["name"] == name:
                item.update(row)
                return
        self.instances.append(row)

    def _load_instance(self, name: str) -> None:
        for item in self.instances:
            if item["name"] == name:
                self.url_var.set(item.get("url") or "")
                self.api_var.set(item.get("api_key") or "")
                self.scan_var.set(item.get("scan_path") or "")
                return

    def _on_instance_picked(self, _event=None) -> None:
        chosen = self.instance_var.get().strip()
        previous = getattr(self, "_shown_instance", "")
        if previous and previous != chosen:
            self._store_instance(previous)
        self._load_instance(chosen)
        self._shown_instance = chosen

    def _add_instance(self) -> None:
        self._store_instance(getattr(self, "_shown_instance", "") or self.instance_var.get())
        names = {item["name"] for item in self.instances}
        name = "Lidarr"
        number = 2
        while name in names:
            name = f"Lidarr {number}"
            number += 1
        self.instances.append(
            {
                "name": name,
                "url": self.url_var.get().strip().rstrip("/"),
                "api_key": self.api_var.get().strip(),
                "scan_path": self.scan_var.get().strip(),
            }
        )
        self._refresh_instance_names()
        self.instance_var.set(name)
        self._shown_instance = name

    def _build_behavior(self, parent: ttk.Frame) -> None:
        ttk.Label(
            parent,
            text="Each option is the same value config.toml already uses. A short note under the field says what it changes.",
            style="Muted.TLabel",
            wraplength=860,
        ).pack(anchor="w", pady=(0, 6))
        canvas = tk.Canvas(parent, highlightthickness=0, background=active_palette(self).main, borderwidth=0)
        canvas._chrome_role = "page"
        self._behavior_canvas = canvas
        scroll = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _fit(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(window_id, width=canvas.winfo_width())

        inner.bind("<Configure>", _fit)
        canvas.bind("<Configure>", _fit)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        row = 0
        if self.history_var is not None:
            ttk.Label(inner, text="Recent history").grid(row=row, column=0, sticky="w", pady=(2, 0))
            ttk.Combobox(
                inner,
                textvariable=self.history_var,
                values=[str(number) for number in RECENT_LIMITS],
                state="readonly",
                width=8,
            ).grid(row=row, column=1, sticky="w", pady=(2, 0))
            ttk.Label(inner, text="items", style="Muted.TLabel").grid(row=row, column=2, sticky="w", padx=(8, 0))
            row += 1
            ttk.Label(
                inner,
                text="How many finished downloads the Status tab lists. The browser page uses the same number.",
                style="Muted.TLabel",
                wraplength=720,
            ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 8))
            row += 1
        primary = [spec for spec in BEHAVIOR_FIELDS if spec.key in _PRIMARY_BEHAVIOR]
        advanced = [spec for spec in BEHAVIOR_FIELDS if spec.key not in _PRIMARY_BEHAVIOR]
        for spec in primary:
            self._behavior_row(inner, row, spec)
            row += 2
        self.advanced_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            inner,
            text="Advanced",
            variable=self.advanced_var,
            command=self._toggle_advanced,
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(12, 4))
        row += 1
        self._advanced_frame = ttk.Frame(inner)
        self._advanced_frame.grid(row=row, column=0, columnspan=3, sticky="ew")
        advanced_row = 0
        for spec in advanced:
            self._behavior_row(self._advanced_frame, advanced_row, spec)
            advanced_row += 2
        self._advanced_frame.columnconfigure(1, weight=1)
        self._advanced_frame.grid_remove()
        inner.columnconfigure(1, weight=1)
        # Bind the wheel on every field. Enter/Leave on the canvas unbinds as
        # soon as the pointer moves onto this inner frame, which is the whole
        # page, so Windows never scrolled.
        self._bind_behavior_wheel(parent)

    def _share_count_text(self) -> str:
        worker = self.worker
        if worker is None:
            return "File counts appear after a rescan while the worker is running."
        folders, files = getattr(worker, "share_counts", (0, 0))
        return f"Indexed {folders} folders and {files} files. Rescan after changing the list."

    def _build_blocklist(self, parent: ttk.Frame) -> None:
        ttk.Label(
            parent,
            text="Users and folders that failed, timed out, or looked like a fake FLAC. They are skipped on the next search.",
            style="Muted.TLabel",
            wraplength=860,
        ).pack(anchor="w")
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, pady=(8, 0))
        columns = ("user", "folder", "reason", "when")
        self.block_tree = ttk.Treeview(frame, columns=columns, show="headings", height=12)
        for key, heading, width in (
            ("user", "User", 160),
            ("folder", "Folder", 280),
            ("reason", "Reason", 280),
            ("when", "When", 180),
        ):
            self.block_tree.heading(key, text=heading)
            self.block_tree.column(key, width=width, anchor="w")
        self.block_tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.block_tree.yview)
        scroll.pack(side="right", fill="y")
        self.block_tree.configure(yscrollcommand=scroll.set)
        buttons = ttk.Frame(parent)
        buttons.pack(anchor="w", pady=(8, 0))
        ttk.Button(buttons, text="Remove selected", command=self._remove_block).pack(side="left")
        ttk.Button(buttons, text="Clear blocklist", command=self._clear_blocklist).pack(side="left", padx=(8, 0))
        self._fill_blocklist()

    def _fill_blocklist(self) -> None:
        tree = getattr(self, "block_tree", None)
        if tree is None or self.store is None:
            return
        tree.delete(*tree.get_children())
        for row in self.store.blocklist_rows():
            tree.insert(
                "",
                "end",
                iid=str(row["id"]),
                values=(row["username"], row["folder"], row["reason"], row["created_at"]),
            )

    def _remove_block(self) -> None:
        if self.store is None or not getattr(self, "block_tree", None):
            return
        for item in self.block_tree.selection():
            try:
                self.store.clear_block(int(item))
            except ValueError:
                continue
        self._fill_blocklist()

    def _clear_blocklist(self) -> None:
        if self.store is None:
            return
        if not messagebox.askyesno("Clear blocklist", "Remove every blocked user and folder?", parent=self):
            return
        self.store.clear_blocklist()
        self._fill_blocklist()

    def _build_sharing(self, parent: ttk.Frame) -> None:
        ttk.Label(
            parent,
            text=(
                "Enforced when a peer requests a file. Run open-firewall.ps1 and forward both listen ports if UPnP does not. "
                "Pause keeps sharing up. Stop closes it. Rescan reports the real file and folder counts to the Soulseek server."
            ),
            style="Muted.TLabel",
            wraplength=900,
        ).pack(anchor="w", pady=(0, 8))
        body = ttk.Frame(parent)
        body.pack(fill="both", expand=True)
        form = ttk.Frame(body)
        form.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        side = ttk.Frame(body)
        side.grid(row=0, column=1, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        row = 0
        for spec in SHARING_FIELDS:
            if spec.kind == "bool":
                ttk.Checkbutton(form, text=spec.label, variable=self.fields[spec.key]).grid(
                    row=row, column=0, columnspan=3, sticky="w", pady=(4, 0)
                )
            else:
                ttk.Label(form, text=spec.label).grid(row=row, column=0, sticky="w", pady=(4, 0), padx=(0, 8))
                width = 28 if spec.kind == "csv" else 8
                ttk.Entry(form, textvariable=self.fields[spec.key], width=width).grid(row=row, column=1, sticky="w", pady=(4, 0))
                if spec.unit:
                    ttk.Label(form, text=spec.unit, style="Muted.TLabel").grid(row=row, column=2, sticky="w", padx=(6, 0))
            row += 1
            ttk.Label(form, text=spec.help, style="Muted.TLabel", wraplength=460).grid(
                row=row, column=0, columnspan=3, sticky="w"
            )
            row += 1
        ttk.Label(side, text="Shared folders").pack(anchor="w")
        palette = active_palette(self)
        folder_box = tk.Listbox(
            side,
            height=6,
            background=palette.field,
            foreground=palette.fg,
            selectbackground=palette.nav_on_bg,
            selectforeground=palette.nav_on_fg,
            selectmode="browse",
            highlightthickness=0,
            bd=0,
        )
        folder_box._chrome_role = "list"
        folder_box.pack(fill="x", pady=(4, 0))
        self._folder_list = folder_box
        folder_buttons = ttk.Frame(side)
        folder_buttons.pack(anchor="w", pady=(4, 0))
        ttk.Button(folder_buttons, text="Add folder", command=self._add_share_folder).pack(side="left")
        ttk.Button(folder_buttons, text="Remove", command=self._remove_share_folder).pack(side="left", padx=(8, 0))
        ttk.Button(folder_buttons, text="Rescan", command=self._rescan_shares).pack(side="left", padx=(8, 0))
        ttk.Label(side, textvariable=self.share_count_var, style="Muted.TLabel", wraplength=420).pack(anchor="w", pady=(4, 8))
        ttk.Label(side, text="Ignored users").pack(anchor="w")
        ban_box = tk.Listbox(
            side,
            height=5,
            background=palette.field,
            foreground=palette.fg,
            selectbackground=palette.nav_on_bg,
            selectforeground=palette.nav_on_fg,
            selectmode="browse",
            highlightthickness=0,
            bd=0,
        )
        ban_box._chrome_role = "list"
        ban_box.pack(fill="x", pady=(4, 0))
        self._ban_list = ban_box
        ban_buttons = ttk.Frame(side)
        ban_buttons.pack(anchor="w", pady=(4, 0))
        self._ban_entry = tk.StringVar()
        ttk.Entry(ban_buttons, textvariable=self._ban_entry, width=18).pack(side="left")
        ttk.Button(ban_buttons, text="Ignore", command=self._add_ban).pack(side="left", padx=(8, 0))
        ttk.Button(ban_buttons, text="Remove", command=self._remove_ban).pack(side="left", padx=(8, 0))
        ttk.Label(
            side,
            text="Same list as Ignored Soulseek users. They cannot download from you, and you will not download from them.",
            style="Muted.TLabel",
            wraplength=420,
        ).pack(anchor="w", pady=(4, 0))
        self._refresh_ban_list()
        self._refresh_folder_list()

    def _refresh_ban_list(self) -> None:
        box = getattr(self, "_ban_list", None)
        if box is None:
            return
        box.delete(0, "end")
        names = sorted(
            part.strip().lower() for part in str(self.fields["ignored_users"].get()).split(",") if part.strip()
        )
        for name in names:
            box.insert("end", name)

    def _write_bans(self, names: list[str]) -> None:
        self.fields["ignored_users"].set(", ".join(names))
        self._refresh_ban_list()

    def _add_ban(self) -> None:
        name = self._ban_entry.get().strip().lower()
        if not name:
            return
        names = sorted(
            set(part.strip().lower() for part in str(self.fields["ignored_users"].get()).split(",") if part.strip())
            | {name}
        )
        self._ban_entry.set("")
        self._write_bans(names)

    def _remove_ban(self) -> None:
        box = self._ban_list
        selected = box.curselection()
        if not selected:
            return
        current = box.get(selected[0])
        names = [
            part.strip().lower()
            for part in str(self.fields["ignored_users"].get()).split(",")
            if part.strip() and part.strip().lower() != current
        ]
        self._write_bans(sorted(set(names)))

    def _refresh_folder_list(self) -> None:
        box = getattr(self, "_folder_list", None)
        if box is None:
            return
        box.delete(0, "end")
        for path in self.share_paths:
            box.insert("end", path)

    def _add_share_folder(self) -> None:
        chosen = filedialog.askdirectory(initialdir=str(Path.home()))
        if not chosen or chosen in self.share_paths:
            return
        self.share_paths.append(chosen)
        self._refresh_folder_list()

    def _remove_share_folder(self) -> None:
        box = self._folder_list
        selected = box.curselection()
        if not selected:
            return
        del self.share_paths[selected[0]]
        self._refresh_folder_list()

    def _rescan_shares(self) -> None:
        worker = self.worker
        loop = getattr(worker, "_loop", None) if worker is not None else None
        if worker is None or loop is None or not loop.is_running():
            self.message_var.set("Start the worker, then rescan. Sharing uses that Soulseek login.")
            return
        paths = list(self.share_paths)
        self.message_var.set("Scanning shared folders…")

        def work() -> None:
            try:
                counts = asyncio.run_coroutine_threadsafe(worker.rescan_shares(paths), loop).result(timeout=3600)
                text = f"Rescan finished: {counts[0]} folders, {counts[1]} files."
            except Exception as exc:
                text = f"Rescan failed: {exc}"

            def show() -> None:
                self.share_count_var.set(text)
                self.message_var.set(text)

            try:
                self.after(0, show)
            except tk.TclError:
                pass

        threading.Thread(target=work, name="share-rescan", daemon=True).start()

    def _behavior_row(self, parent: ttk.Frame, row: int, spec: BehaviorField) -> None:
        if spec.kind == "choice":
            ttk.Label(parent, text=spec.label).grid(row=row, column=0, sticky="w", pady=(8, 0), padx=(0, 12))
            labels = {key: label for key, label in spec.choices}
            reverse = {label: key for key, label in spec.choices}
            current = str(self.fields[spec.key].get())
            display = tk.StringVar(value=labels.get(current, current))
            self._choice_display[spec.key] = display
            box = ttk.Combobox(parent, textvariable=display, values=[label for _key, label in spec.choices], state="readonly", width=28)

            def choose(_event=None, spec=spec, display=display, reverse=reverse) -> None:
                self.fields[spec.key].set(reverse.get(display.get(), self.fields[spec.key].get()))
                if spec.key == "search_order":
                    direction = default_direction(self.fields["search_order"].get())
                    self._set_field(
                        next(item for item in BEHAVIOR_FIELDS if item.key == "search_order_direction"),
                        direction,
                    )

            box.bind("<<ComboboxSelected>>", choose)
            box.grid(row=row, column=1, sticky="w", pady=(8, 0))
        elif spec.kind == "bool":
            ttk.Checkbutton(parent, text=spec.label, variable=self.fields[spec.key]).grid(
                row=row, column=0, columnspan=3, sticky="w", pady=(8, 0)
            )
        else:
            ttk.Label(parent, text=spec.label).grid(row=row, column=0, sticky="w", pady=(8, 0), padx=(0, 12))
            width = 48 if spec.kind in {"text", "csv", "path"} else 12
            ttk.Entry(parent, textvariable=self.fields[spec.key], width=width).grid(
                row=row, column=1, sticky="ew" if spec.kind in {"text", "csv", "path"} else "w", pady=(8, 0)
            )
            if spec.kind == "path":
                ttk.Button(parent, text="Browse", command=lambda var=self.fields[spec.key]: self._browse(var)).grid(
                    row=row, column=2, padx=(8, 0), pady=(8, 0)
                )
            elif spec.unit:
                ttk.Label(parent, text=spec.unit, style="Muted.TLabel").grid(
                    row=row, column=2, sticky="w", padx=(8, 0), pady=(8, 0)
                )
        ttk.Label(parent, text=spec.help, style="Muted.TLabel", wraplength=720).grid(
            row=row + 1, column=0, columnspan=3, sticky="w", pady=(0, 2)
        )

    def _toggle_advanced(self) -> None:
        frame = getattr(self, "_advanced_frame", None)
        if frame is None:
            return
        if self.advanced_var.get():
            frame.grid()
        else:
            frame.grid_remove()
        canvas = self._behavior_canvas
        if canvas is not None:
            canvas.configure(scrollregion=canvas.bbox("all"))

    def _bind_behavior_wheel(self, widget: tk.Misc) -> None:
        if isinstance(widget, (tk.Scrollbar, ttk.Scrollbar)):
            return
        widget.bind("<MouseWheel>", self._wheel, add="+")
        widget.bind("<Button-4>", self._wheel, add="+")
        widget.bind("<Button-5>", self._wheel, add="+")
        for child in widget.winfo_children():
            self._bind_behavior_wheel(child)

    def _wheel(self, event) -> str:
        canvas = self._behavior_canvas
        if canvas is None:
            return "break"
        if getattr(event, "num", None) == 5 or getattr(event, "delta", 0) < 0:
            canvas.yview_scroll(1, "units")
        else:
            canvas.yview_scroll(-1, "units")
        return "break"

    def _path_row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=variable, width=52).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(parent, text="Browse", command=lambda var=variable: self._browse(var)).grid(
            row=row, column=2, padx=(8, 0)
        )

    def _browse(self, variable: tk.Variable) -> None:
        chosen = filedialog.askdirectory(initialdir=str(variable.get()) or str(Path.home()))
        if chosen:
            variable.set(chosen)

    def restore_defaults(self) -> None:
        fresh = default_config()
        for spec in (*BEHAVIOR_FIELDS, *SHARING_FIELDS):
            if not spec.restore:
                continue
            self._set_field(spec, getattr(fresh, spec.key))
        self.share_paths = []
        self._refresh_folder_list()
        self._refresh_ban_list()
        if self.history_var is not None:
            self.history_var.set(str(DEFAULT_RECENT_LIMIT))
        self.message_var.set(
            "Defaults restored for the Behavior options, except the share folder, database, and log file. Click Save to keep them."
        )

    def _bounded_number(self, spec: BehaviorField):
        raw = str(self.fields[spec.key].get()).strip()
        low = spec.low if spec.low is not None else 0
        high = spec.high if spec.high is not None else 0
        try:
            if spec.kind == "int":
                if not raw or any(ch in raw for ch in ".eE"):
                    raise ValueError
                value = int(raw)
            else:
                value = float(raw)
        except ValueError as exc:
            raise ValueError(f"{spec.label} must be a number from {low:g} to {high:g}.") from exc
        if value < low or value > high:
            raise ValueError(f"{spec.label} must be a number from {low:g} to {high:g}.")
        return value

    def _behavior_values(self) -> dict:
        values: dict = {}
        for spec in BEHAVIOR_FIELDS:
            if spec.kind == "bool":
                values[spec.key] = bool(self.fields[spec.key].get())
            elif spec.kind in {"int", "float"}:
                values[spec.key] = self._bounded_number(spec)
            elif spec.key == "search_order":
                values[spec.key] = normalize_search_order(self.fields[spec.key].get())
            elif spec.key == "search_order_direction":
                values[spec.key] = normalize_search_direction(self.fields[spec.key].get())
            elif spec.key == "preferred_extensions":
                parts = [part.strip().lower().lstrip(".") for part in str(self.fields[spec.key].get()).split(",")]
                parts = [part for part in parts if part]
                if not parts or any(not part.isalnum() for part in parts):
                    raise ValueError("Formats must be a comma-separated list such as mp3, flac, wav.")
                values[spec.key] = parts
            elif spec.key in {"ignored_users", "priority_users"}:
                values[spec.key] = {
                    part.strip().lower() for part in str(self.fields[spec.key].get()).split(",") if part.strip()
                }
            elif spec.key == "status_bind":
                text = str(self.fields[spec.key].get()).strip()
                if not text:
                    raise ValueError("Enter the status page address.")
                values[spec.key] = text
            elif spec.key in {"state_db", "log_file"}:
                text = str(self.fields[spec.key].get()).strip()
                if not text:
                    raise ValueError(f"Enter a {spec.label.lower()}.")
                values[spec.key] = Path(text)
            else:
                values[spec.key] = str(self.fields[spec.key].get()).strip()
        return values

    def _sharing_values(self) -> dict:
        values: dict = {}
        for spec in SHARING_FIELDS:
            if spec.kind == "bool":
                values[spec.key] = bool(self.fields[spec.key].get())
            elif spec.kind in {"int", "float"}:
                values[spec.key] = self._bounded_number(spec)
            elif spec.key == "priority_users":
                values[spec.key] = {
                    part.strip().lower() for part in str(self.fields[spec.key].get()).split(",") if part.strip()
                }
        values["share_dir"] = self.share_paths[0] if self.share_paths else ""
        values["share_dirs"] = list(self.share_paths[1:])
        return values

    def _bridge_values(self) -> dict:
        self._store_instance(getattr(self, "_shown_instance", "") or self.instance_var.get())
        host = self.bridge_host_var.get().strip() or "127.0.0.1"
        try:
            port = int(self.bridge_port_var.get().strip())
        except ValueError as exc:
            raise ValueError("Bridge port must be a whole number from 1 to 65535.") from exc
        if port < 1 or port > 65535:
            raise ValueError("Bridge port must be a whole number from 1 to 65535.")
        try:
            hours = int(self.upgrade_hours_var.get().strip())
        except ValueError as exc:
            raise ValueError("Quality upgrade hours must be a whole number of at least 1.") from exc
        if hours < 1:
            raise ValueError("Quality upgrade hours must be a whole number of at least 1.")
        if self.bridge_enabled_var.get() and not self.bridge_key_var.get().strip():
            raise ValueError("Generate an API key before enabling the Lidarr bridge.")
        active = self.instance_var.get().strip() or "Home"
        return {
            "lidarr_instances": [dict(item) for item in self.instances],
            "lidarr_active": active,
            "bridge_enabled": bool(self.bridge_enabled_var.get()),
            "bridge_host": host,
            "bridge_port": port,
            "bridge_advertise_host": self.bridge_advertise_var.get().strip(),
            "bridge_remote_path": self.bridge_remote_var.get().strip(),
            "bridge_api_key": self.bridge_key_var.get().strip(),
            "bridge_username": self.bridge_user_var.get().strip() or "rummagarr",
            "bridge_password": self.bridge_pass_var.get(),
            "bridge_category": self.bridge_category_var.get().strip() or "lidarr",
            "dry_run": bool(self.dry_run_var.get()),
            "quality_upgrades": bool(self.upgrade_var.get()),
            "quality_upgrade_hours": hours,
            "notify_discord_url": self.discord_var.get().strip(),
            "notify_ntfy_url": self.ntfy_var.get().strip(),
            "notify_on_import": bool(self.notify_import_var.get()),
            "notify_on_review": bool(self.notify_review_var.get()),
            "plex_url": self.plex_url_var.get().strip(),
            "plex_token": self.plex_token_var.get().strip(),
            "plex_section": self.plex_section_var.get().strip(),
            "jellyfin_url": self.jellyfin_url_var.get().strip(),
            "jellyfin_api_key": self.jellyfin_key_var.get().strip(),
        }

    def _build_bridge(self, parent: ttk.Frame) -> None:
        canvas = tk.Canvas(parent, highlightthickness=0, background=active_palette(self).main, borderwidth=0)
        canvas._chrome_role = "page"
        scroll = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _fit(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(window_id, width=canvas.winfo_width())

        inner.bind("<Configure>", _fit)
        canvas.bind("<Configure>", _fit)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        ttk.Label(
            inner,
            text=(
                "Rummagarr speaks Torznab and the qBittorrent Web API (2.11.2) on this port. "
                "Lidarr on another PC, including over Tailscale, cannot use 127.0.0.1. "
                "Listen on all interfaces or on the Tailscale address, and set the address Lidarr can reach. "
                "The standalone worker keeps running either way."
            ),
            style="Muted.TLabel",
            wraplength=760,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Checkbutton(inner, text="Enable the Lidarr indexer and download client", variable=self.bridge_enabled_var).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=2
        )
        ttk.Checkbutton(inner, text="Dry run: show what would be grabbed, and do not download", variable=self.dry_run_var).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=2
        )
        self._bind_choices = bind_choices(list_ipv4_interfaces())
        current_host = self.bridge_host_var.get().strip() or "127.0.0.1"
        if not any(host == current_host for host, _label in self._bind_choices):
            self._bind_choices.append((current_host, current_host))
        self.bridge_bind_label = tk.StringVar(
            value=next(label for host, label in self._bind_choices if host == current_host)
        )
        ttk.Label(inner, text="Listen on").grid(row=3, column=0, sticky="w", pady=3)
        bind_box = ttk.Combobox(
            inner,
            textvariable=self.bridge_bind_label,
            values=[label for _host, label in self._bind_choices],
            state="readonly",
            width=42,
        )
        bind_box.grid(row=3, column=1, columnspan=2, sticky="ew", pady=3)
        bind_box.bind("<<ComboboxSelected>>", self._on_bind_picked)
        ttk.Label(inner, text="Port").grid(row=4, column=0, sticky="w", pady=3)
        ttk.Entry(inner, textvariable=self.bridge_port_var, width=10).grid(row=4, column=1, sticky="w", pady=3)
        ttk.Label(inner, text="Address Lidarr uses").grid(row=5, column=0, sticky="w", pady=3)
        ttk.Entry(inner, textvariable=self.bridge_advertise_var, width=42).grid(row=5, column=1, columnspan=2, sticky="ew", pady=3)
        detected = advertise_host("0.0.0.0", "", list_ipv4_interfaces())
        if is_tailscale(detected):
            detected_text = f"Blank uses this PC's Tailscale address {detected} when listening on all interfaces."
        elif detected and not is_loopback_bind(detected):
            detected_text = f"No Tailscale address found. Blank uses {detected} when listening on all interfaces. Type a 100.x address if Lidarr uses Tailscale."
        else:
            detected_text = "No Tailscale address found. Type the 100.x address of this PC if Lidarr is on another machine."
        ttk.Label(inner, text=detected_text, style="Muted.TLabel", wraplength=760).grid(
            row=6, column=0, columnspan=3, sticky="w"
        )
        ttk.Label(inner, text="API key").grid(row=7, column=0, sticky="w", pady=3)
        ttk.Entry(inner, textvariable=self.bridge_key_var, width=42).grid(row=7, column=1, sticky="ew", pady=3)
        ttk.Button(inner, text="Generate key", command=self._generate_bridge_key).grid(row=7, column=2, padx=(8, 0))
        ttk.Label(inner, text="qBittorrent username").grid(row=8, column=0, sticky="w", pady=3)
        ttk.Entry(inner, textvariable=self.bridge_user_var, width=42).grid(row=8, column=1, sticky="ew", pady=3)
        ttk.Label(inner, text="qBittorrent password").grid(row=9, column=0, sticky="w", pady=3)
        ttk.Entry(inner, textvariable=self.bridge_pass_var, width=42, show="*").grid(row=9, column=1, sticky="ew", pady=3)
        ttk.Label(inner, text="Category").grid(row=10, column=0, sticky="w", pady=3)
        ttk.Entry(inner, textvariable=self.bridge_category_var, width=42).grid(row=10, column=1, sticky="ew", pady=3)
        ttk.Label(
            inner,
            text="When Listen on is not this PC only, Lidarr's download-client password is the API key. The version check Lidarr sends before login stays open. Every other request needs that key.",
            style="Muted.TLabel",
            wraplength=760,
        ).grid(row=11, column=0, columnspan=3, sticky="w", pady=(2, 6))
        ttk.Label(inner, text="Path Lidarr sees").grid(row=12, column=0, sticky="w", pady=3)
        ttk.Entry(inner, textvariable=self.bridge_remote_var, width=42).grid(row=12, column=1, columnspan=2, sticky="ew", pady=3)
        ttk.Label(
            inner,
            text=(
                "Leave blank when Lidarr runs on this PC. Otherwise set the path Lidarr uses for this same folder, "
                "such as a UNC share or a mapped drive. Files still land in the Complete folder. "
                "The qBittorrent save path uses this value, and the album folder name is added under it."
            ),
            style="Muted.TLabel",
            wraplength=760,
        ).grid(row=13, column=0, columnspan=3, sticky="w")
        ttk.Button(inner, text="Allow in Windows Firewall", command=self.allow_firewall).grid(
            row=14, column=0, columnspan=2, sticky="w", pady=(8, 4)
        )
        ttk.Checkbutton(inner, text="Re-search albums below the quality cutoff", variable=self.upgrade_var).grid(
            row=15, column=0, columnspan=2, sticky="w", pady=(10, 2)
        )
        ttk.Label(inner, text="Hours between cutoff checks").grid(row=16, column=0, sticky="w", pady=3)
        ttk.Entry(inner, textvariable=self.upgrade_hours_var, width=8).grid(row=16, column=1, sticky="w", pady=3)
        ttk.Label(
            inner,
            text="Lidarr replaces the old file when the better copy imports. Rummagarr does not delete it.",
            style="Muted.TLabel",
            wraplength=760,
        ).grid(row=17, column=0, columnspan=3, sticky="w")
        ttk.Label(inner, text="Discord webhook").grid(row=18, column=0, sticky="w", pady=(10, 3))
        ttk.Entry(inner, textvariable=self.discord_var, width=42).grid(row=18, column=1, columnspan=2, sticky="ew", pady=(10, 3))
        ttk.Label(inner, text="ntfy URL").grid(row=19, column=0, sticky="w", pady=3)
        ttk.Entry(inner, textvariable=self.ntfy_var, width=42).grid(row=19, column=1, columnspan=2, sticky="ew", pady=3)
        ttk.Checkbutton(inner, text="Notify when an import finishes", variable=self.notify_import_var).grid(
            row=20, column=0, columnspan=3, sticky="w"
        )
        ttk.Checkbutton(inner, text="Notify when files move to Review", variable=self.notify_review_var).grid(
            row=21, column=0, columnspan=3, sticky="w"
        )
        ttk.Label(inner, text="Plex URL").grid(row=22, column=0, sticky="w", pady=(8, 3))
        ttk.Entry(inner, textvariable=self.plex_url_var, width=42).grid(row=22, column=1, columnspan=2, sticky="ew")
        ttk.Label(inner, text="Plex token").grid(row=23, column=0, sticky="w", pady=3)
        ttk.Entry(inner, textvariable=self.plex_token_var, width=42, show="*").grid(row=23, column=1, columnspan=2, sticky="ew")
        ttk.Label(inner, text="Plex section id").grid(row=24, column=0, sticky="w", pady=3)
        ttk.Entry(inner, textvariable=self.plex_section_var, width=12).grid(row=24, column=1, sticky="w")
        ttk.Label(inner, text="Jellyfin URL").grid(row=25, column=0, sticky="w", pady=3)
        ttk.Entry(inner, textvariable=self.jellyfin_url_var, width=42).grid(row=25, column=1, columnspan=2, sticky="ew")
        ttk.Label(inner, text="Jellyfin API key").grid(row=26, column=0, sticky="w", pady=3)
        ttk.Entry(inner, textvariable=self.jellyfin_key_var, width=42, show="*").grid(row=26, column=1, columnspan=2, sticky="ew")
        ttk.Label(
            inner,
            text="Leave the section id blank to refresh every Plex library. A test sends the webhook and refreshes the libraries.",
            style="Muted.TLabel",
            wraplength=760,
        ).grid(row=27, column=0, columnspan=3, sticky="w", pady=(4, 8))
        buttons = ttk.Frame(inner)
        buttons.grid(row=28, column=0, columnspan=3, sticky="w")
        ttk.Button(buttons, text="Test notification", command=self.test_notification).pack(side="left")
        ttk.Button(buttons, text="Add to Lidarr", command=self.add_to_lidarr).pack(side="left", padx=(8, 0))
        inner.columnconfigure(1, weight=1)

    def _on_bind_picked(self, _event=None) -> None:
        label = self.bridge_bind_label.get()
        for host, choice in self._bind_choices:
            if choice == label:
                self.bridge_host_var.set(host)
                return

    def allow_firewall(self) -> None:
        try:
            port = int(self.bridge_port_var.get().strip())
        except ValueError:
            self.message_var.set("Bridge port must be a whole number from 1 to 65535.")
            return
        if port < 1 or port > 65535:
            self.message_var.set("Bridge port must be a whole number from 1 to 65535.")
            return
        if os.name != "nt":
            self.message_var.set("Windows Firewall rules are only added on Windows.")
            return
        if not messagebox.askyesno(
            "Windows Firewall",
            f"Windows will ask for approval to allow inbound TCP {port} for the Lidarr bridge.",
            parent=self.winfo_toplevel(),
        ):
            return
        ok, text = windows_firewall_prompt(port)
        if ok and self.store is not None:
            self.store.set_meta("firewall_bridge_port", str(port))
        self.message_var.set(text)

    def _maybe_firewall(self) -> str:
        """Ask once per port when Lidarr must reach this PC. A Yes is remembered."""
        if os.name != "nt" or is_loopback_bind(self.bridge_host_var.get().strip()):
            return ""
        try:
            port = int(self.bridge_port_var.get().strip())
        except ValueError:
            return ""
        if port < 1 or port > 65535:
            return ""
        if self.store is not None and (self.store.get_meta("firewall_bridge_port") or "") == str(port):
            return ""
        if not messagebox.askyesno(
            "Windows Firewall",
            f"Allow inbound TCP {port} so Lidarr on another PC can reach Rummagarr? Windows will ask for approval.",
            parent=self.winfo_toplevel(),
        ):
            return ""
        ok, text = windows_firewall_prompt(port)
        if ok and self.store is not None:
            self.store.set_meta("firewall_bridge_port", str(port))
        return text

    def _generate_bridge_key(self) -> None:
        self.bridge_key_var.set(secrets.token_hex(16))

    def test_notification(self) -> None:
        self.message_var.set("Sending a test notification…")

        def work() -> None:
            cfg = self._notify_preview()
            try:
                lines = deliver(cfg, "test", "Rummagarr test", "This is a test from Rummagarr.")
            except Exception as exc:
                text = f"Test notification failed: {exc}"
            else:
                text = " ".join(lines) or "Nothing to send. Add a Discord or ntfy URL, or Plex or Jellyfin."
            try:
                self.after(0, lambda text=text: self.message_var.set(text))
            except tk.TclError:
                pass

        threading.Thread(target=work, name="notify-test", daemon=True).start()

    def _notify_preview(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            notify_discord_url=self.discord_var.get().strip(),
            notify_ntfy_url=self.ntfy_var.get().strip(),
            notify_on_import=True,
            notify_on_review=True,
            plex_url=self.plex_url_var.get().strip(),
            plex_token=self.plex_token_var.get().strip(),
            plex_section=self.plex_section_var.get().strip(),
            jellyfin_url=self.jellyfin_url_var.get().strip(),
            jellyfin_api_key=self.jellyfin_key_var.get().strip(),
        )

    def add_to_lidarr(self) -> None:
        if not self.bridge_key_var.get().strip():
            self.message_var.set("Generate an API key first.")
            return
        self.bridge_enabled_var.set(True)
        if not self.save():
            return
        firewall_note = self._maybe_firewall()
        self.message_var.set("Adding Rummagarr to Lidarr…")

        def work() -> None:
            text = self._register_bridge()
            if firewall_note:
                text = f"{text} {firewall_note}"
            try:
                self.after(0, lambda text=text: self.message_var.set(text))
            except tk.TclError:
                pass

        threading.Thread(target=work, name="lidarr-register", daemon=True).start()

    def _register_bridge(self) -> str:
        worker = self.worker
        if worker is None:
            return "Save first, then open Rummagarr and use Add to Lidarr."
        if worker.bridge_port() <= 0:
            detail = getattr(worker, "_bridge_error", "") or "The bridge is not listening."
            return f"Could not add Rummagarr to Lidarr: {detail}"
        cfg = worker.cfg
        host = advertise_host(cfg.bridge_host, getattr(cfg, "bridge_advertise_host", ""))
        shown = host_for_url(host)
        port = worker.bridge_port()
        if is_loopback_bind(cfg.bridge_host):
            password = cfg.bridge_password or cfg.bridge_api_key
        else:
            password = cfg.bridge_api_key
        client = LidarrClient(cfg.lidarr_url, cfg.lidarr_api_key, timeout=30)
        try:
            text = register_with_lidarr(
                client,
                base_url=f"http://{shown}:{port}",
                api_key=cfg.bridge_api_key,
                host=shown,
                port=port,
                username=cfg.bridge_username or "rummagarr",
                password=password,
                category=cfg.bridge_category or "lidarr",
            )
        except LidarrRegisterError as exc:
            return f"Could not add Rummagarr to Lidarr: {exc}"
        except Exception as exc:
            return f"Could not add Rummagarr to Lidarr: {exc}"
        finally:
            client.close()
        if is_loopback_bind(host) and not url_host_is_local(cfg.lidarr_url):
            text += (
                " 127.0.0.1 is unreachable from that Lidarr."
                " Set Address Lidarr uses to this PC's Tailscale address."
            )
        return text

    def build_config(self) -> Config:
        url = self.url_var.get().strip().rstrip("/")
        api_key = self.api_var.get().strip()
        username = self.user_var.get().strip()
        password = self.password_var.get()
        download = self.download_var.get().strip()
        complete = self.complete_var.get().strip()
        scan = self.scan_var.get().strip()
        if not url:
            raise ValueError("Enter the Lidarr URL.")
        if not api_key or api_key == "CHANGEME":
            raise ValueError("Enter the Lidarr API key.")
        if not username or username == "CHANGEME":
            raise ValueError("Enter the Soulseek username.")
        if not password or password == "CHANGEME":
            raise ValueError("Enter the Soulseek password.")
        if not download or not complete or not scan:
            raise ValueError("Choose the Incomplete folder, Complete folder, and Lidarr scan path.")
        try:
            listen = int(self.listen_var.get().strip())
            obfs = int(self.obfs_var.get().strip())
        except ValueError as exc:
            raise ValueError("Listen port and obfuscated port must be whole numbers from 1 to 65535.") from exc
        if listen < 1 or listen > 65535 or obfs < 1 or obfs > 65535:
            raise ValueError("Listen port and obfuscated port must be whole numbers from 1 to 65535.")
        if listen == obfs:
            raise ValueError("Listen port and obfuscated port must be different.")
        fields = dict(
            lidarr_url=url,
            lidarr_api_key=api_key,
            lidarr_scan_path=scan,
            **self._bridge_values(),
            slsk_username=username,
            slsk_password=password,
            listen_port=listen,
            obfuscated_port=obfs,
            enable_upnp=bool(self.upnp_var.get()),
            download_dir=Path(download),
            complete_dir=Path(complete),
            start_on_launch=bool(self.launch_var.get()),
            theme=normalize_theme(self.theme_var.get()),
            **self._behavior_values(),
            **self._sharing_values(),
        )
        if self.base is None:
            return default_config(**fields)
        return replace(self.base, **fields)

    def test_lidarr(self) -> None:
        url = self.url_var.get().strip().rstrip("/")
        api_key = self.api_var.get().strip()
        if not url or not api_key:
            self.message_var.set("Enter the Lidarr URL and API key first.")
            return
        self.message_var.set("Contacting Lidarr…")

        def work() -> None:
            client = LidarrClient(url, api_key, timeout=10)
            try:
                status = client.ping()
                text = f"Lidarr OK ({status.get('version') or status.get('appName') or 'connected'})."
            except Exception as exc:
                text = f"Could not reach Lidarr: {exc}"
            finally:
                client.close()
            try:
                self.after(0, lambda text=text: self.message_var.set(text))
            except tk.TclError:
                pass

        threading.Thread(target=work, name="lidarr-test", daemon=True).start()

    def save(self) -> bool:
        try:
            cfg = self.build_config()
        except ValueError as exc:
            self.message_var.set(str(exc))
            return False
        try:
            save_config(self.config_path, cfg)
            loaded = try_load_config(self.config_path)
        except Exception as exc:
            self.message_var.set(str(exc))
            return False
        if self.store is not None and self.history_var is not None:
            self.store.set_meta("gui_recent_limit", str(normalize_recent_limit(self.history_var.get())))
        self.message_var.set("Saved.")
        if self.on_saved is not None:
            self.on_saved(loaded)
        return True


def _update_notes(status: UpdateStatus) -> str:
    """The changelog section for this release, without the installer instructions that follow it."""
    raw = (status.notes or "").strip()
    section = raw
    if status.latest and raw:
        try:
            section = changelog_section(raw, status.latest)
        except ChangelogError:
            section = raw
    kept = []
    for line in section.splitlines():
        if line.startswith("Windows installer for Rummagarr"):
            break
        kept.append(line)
    shown = "\n".join(kept).strip()
    return shown or "No release notes were published."


class UpdatePrompt(tk.Toplevel):
    """Newer release: install it, ask again later, or skip this version."""

    def __init__(self, master: tk.Misc, status: UpdateStatus, on_install, on_skip, on_later) -> None:
        super().__init__(master)
        self.title("Update Rummagarr")
        self.transient(master)
        self.resizable(True, True)
        self.minsize(480, 320)
        self.geometry("540x380")
        apply_theme(self)
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"Version {status.latest} is available", style="Title.TLabel").pack(anchor="w")
        if status.can_install:
            detail = (
                f"You are on {status.current}. Update now downloads the installer, stops the worker, "
                "and opens the new version when the upgrade finishes."
            )
        else:
            detail = (
                f"You are on {status.current}. This copy was started from source, so it will not run the installer. "
                "Open the release page to download it."
            )
        ttk.Label(frame, text=detail, style="Muted.TLabel", wraplength=490).pack(anchor="w", pady=(6, 8))
        ttk.Label(frame, text="What's new").pack(anchor="w")
        palette = active_palette(self)
        notes = tk.Text(
            frame,
            height=8,
            wrap="word",
            relief="flat",
            background=palette.field,
            foreground=palette.fg,
            insertbackground=palette.fg,
            highlightthickness=0,
        )
        notes.pack(fill="both", expand=True, pady=(4, 0))
        notes.insert("1.0", _update_notes(status))
        notes.configure(state="disabled")
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(12, 0))
        primary = "Update now" if status.can_install else "Open release page"
        ttk.Button(buttons, text=primary, style="Start.TButton", command=on_install).pack(side="left")
        ttk.Button(buttons, text="Later", command=on_later).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Skip this version", command=on_skip).pack(side="left", padx=(8, 0))
        self.protocol("WM_DELETE_WINDOW", on_later)
        self.bind("<Escape>", lambda _event: on_later())


class DownloadProgress(tk.Toplevel):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.title("Downloading update")
        self.transient(master)
        self.resizable(False, False)
        apply_theme(self)
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        self.label_var = tk.StringVar(value="Starting the download…")
        ttk.Label(frame, textvariable=self.label_var, wraplength=420).pack(anchor="w")
        self.bar = ttk.Progressbar(frame, length=420, mode="determinate", maximum=100)
        self.bar.pack(fill="x", pady=(10, 0))
        self.protocol("WM_DELETE_WINDOW", lambda: None)

    def report(self, done: int, total: int) -> None:
        if total > 0:
            self.bar.stop()
            self.bar.configure(mode="determinate", maximum=total, value=done)
            self.label_var.set(f"Downloaded {format_bytes(done)} of {format_bytes(total)}")
            return
        if str(self.bar.cget("mode")) != "indeterminate":
            self.bar.configure(mode="indeterminate")
            self.bar.start(12)
        self.label_var.set(f"Downloaded {format_bytes(done)}")


@dataclass
class SearchBoard:
    query: str
    link: SearchLink | None = None
    files: list[SearchFile] = field(default_factory=list)
    filters: SearchFilters = field(default_factory=SearchFilters)
    sort_column: str = ""
    sort_reverse: bool = False
    closed: bool = False
    running: bool = False
    outcome: str = ""
    frame: ttk.Frame | None = None
    tree: ttk.Treeview | None = None
    status: tk.StringVar | None = None
    filter_var: tk.StringVar | None = None
    flac_var: tk.IntVar | None = None
    mp3_var: tk.IntVar | None = None
    bitrate_var: tk.StringVar | None = None
    free_var: tk.IntVar | None = None
    row_files: dict[str, SearchFile] = field(default_factory=dict)
    row_users: dict[str, str] = field(default_factory=dict)


def _wanted_word(count: int) -> str:
    return "item" if count == 1 else "items"


def _attach_tip(widget: tk.Widget, text: str) -> None:
    """A small hover note. The Search tab uses it to say who Download all wanted covers."""
    tip: dict[str, tk.Toplevel | None] = {"window": None}

    def hide(_event=None) -> None:
        window = tip["window"]
        if window is not None:
            window.destroy()
            tip["window"] = None

    def show(_event=None) -> None:
        if tip["window"] is not None:
            return
        window = tk.Toplevel(widget)
        tip["window"] = window
        window.wm_overrideredirect(True)
        window.wm_geometry(f"+{widget.winfo_rootx() + 12}+{widget.winfo_rooty() + widget.winfo_height() + 4}")
        tk.Label(
            window,
            text=text,
            justify="left",
            background="#2a2a2a",
            foreground="#f2f2f2",
            padx=8,
            pady=4,
            wraplength=380,
        ).pack()

    widget.bind("<Enter>", show)
    widget.bind("<Leave>", hide)
    widget.bind("<ButtonPress>", hide)


def _release_tk_objects(widget: tk.Misc) -> None:
    """Drop Tcl variables and images on the thread that is closing the window.

    Tcl only accepts those objects from the thread that created them. If a
    later garbage collection runs on a worker, each one waits a second for a
    main loop that is not running, then raises. Clearing the instance dict
    runs the destructors here instead.
    """
    try:
        widget.__dict__.clear()
    except Exception:
        pass


class WantedPreview(tk.Toplevel):
    """Wanted files about to be queued. All start checked."""

    def __init__(self, master: tk.Misc, matches, on_queue, summary: str = "", on_dont_ask=None) -> None:
        super().__init__(master)
        self.title("Download wanted files")
        self.transient(master)
        self.geometry("760x520")
        self.minsize(640, 400)
        apply_theme(self)
        self.on_queue = on_queue
        self.on_dont_ask = on_dont_ask
        self.dont_ask = tk.BooleanVar(value=False)
        self.matches = list(matches)
        self._checked = {index: True for index in range(len(self.matches))}
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        intro = "These files match the Wanted list. Uncheck any you do not want, then queue them."
        if summary:
            intro = summary + " Uncheck any you do not want, then queue them."
        ttk.Label(
            frame,
            text=intro,
            style="Muted.TLabel",
            wraplength=700,
        ).pack(anchor="w", pady=(0, 8))
        tree_frame = ttk.Frame(frame)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=("size",), show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Album and file")
        self.tree.heading("size", text="Size")
        self.tree.column("#0", width=520, stretch=True)
        self.tree.column("size", width=100, stretch=False)
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self._images = {
            "on": self._box("on"),
            "off": self._box("off"),
        }
        self._rows: dict[str, int | None] = {}
        index_of = {id(match): index for index, match in enumerate(self.matches)}
        for label, group in group_matches(self.matches):
            parent = self.tree.insert("", "end", text=label, open=True, image=self._images["on"])
            self._rows[parent] = None
            for match in group:
                index = index_of[id(match)]
                child = self.tree.insert(
                    parent,
                    "end",
                    text=f"{match.song or match.filename}",
                    image=self._images["on"],
                    values=(format_bytes(match.size),),
                )
                self._rows[child] = index
        self.tree.bind("<Button-1>", self._click)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Queue checked", style="Start.TButton", command=self._queue).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(buttons, text="Don't ask again", variable=self.dont_ask).pack(side="left", padx=(16, 0))

    def _box(self, state: str) -> tk.PhotoImage:
        size = 16
        image = tk.PhotoImage(master=self, width=size, height=size)
        image.put("#d5d5d5", to=(0, 0, size, size))
        image.put("#3d8bfd" if state == "on" else "#2a2a2a", to=(2, 2, size - 2, size - 2))
        if state == "on":
            image.put("#ffffff", to=(4, 8, 12, 10))
        return image

    def _click(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if not row:
            return
        index = self._rows.get(row)
        if index is None:
            children = self.tree.get_children(row)
            indexes = [self._rows[child] for child in children if self._rows.get(child) is not None]
            turn_on = not all(self._checked.get(item, False) for item in indexes)
            for item in indexes:
                self._checked[item] = turn_on
            self.tree.item(row, image=self._images["on" if turn_on else "off"])
            for child in children:
                self.tree.item(child, image=self._images["on" if turn_on else "off"])
            return
        self._checked[index] = not self._checked.get(index, False)
        self.tree.item(row, image=self._images["on" if self._checked[index] else "off"])

    def destroy(self) -> None:
        try:
            super().destroy()
        except tk.TclError:
            pass
        _release_tk_objects(self)

    def _queue(self) -> None:
        picked = [match for index, match in enumerate(self.matches) if self._checked.get(index, False)]
        remember = bool(self.dont_ask.get())
        callback = self.on_queue
        skip = self.on_dont_ask
        self.destroy()
        if remember and skip is not None:
            skip()
        if picked:
            callback(picked)


class FavoritePrompt(tk.Toplevel):
    """One window for every favorite suggestion that is still waiting."""

    def __init__(self, master: tk.Misc, on_add, on_skip, on_never, on_hide) -> None:
        super().__init__(master)
        self.title("Favorite suggestions")
        self.transient(master)
        self.resizable(True, True)
        self.minsize(420, 280)
        self.on_add = on_add
        self.on_skip = on_skip
        self.on_never = on_never
        self.on_hide = on_hide
        self.events: list[dict] = []
        apply_theme(self)
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text=(
                "These users finished enough downloads to suggest as favorites. "
                "Add puts the selected user in Priority users. Nothing is added until you choose Add."
            ),
            wraplength=460,
        ).pack(anchor="w", pady=(0, 8))
        self.names = tk.Listbox(frame, height=6, activestyle="dotbox", exportselection=False)
        self.names.pack(fill="both", expand=True)
        self.names.bind("<<ListboxSelect>>", lambda _event: self._show_body())

        def paint(palette: Palette) -> None:
            self.names.configure(
                bg=palette.field,
                fg=palette.fg,
                selectbackground=palette.nav_on_bg,
                selectforeground=palette.nav_on_fg,
                font=_sized_font(palette, 10),
                highlightbackground=palette.field_border,
                highlightcolor=palette.field_border,
            )

        self.names._chrome_paint = paint
        paint(active_palette(self))
        self.detail = ttk.Label(frame, text="", wraplength=460, justify="left")
        self.detail.pack(anchor="w", fill="x", pady=(8, 0))
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Add", style="Start.TButton", command=self._add).pack(side="left")
        ttk.Button(buttons, text="Not now", command=self._skip).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Don't ask again", command=self._never).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self._hide)
        self.geometry(f"+{master.winfo_rootx() + 80}+{master.winfo_rooty() + 80}")

    def show(self, events: list[dict]) -> None:
        incoming = [
            (str(event.get("username") or ""), str(event.get("title") or ""), str(event.get("body") or ""))
            for event in events
        ]
        current = [
            (str(event.get("username") or ""), str(event.get("title") or ""), str(event.get("body") or ""))
            for event in self.events
        ]
        if incoming == current and self.names.size() == len(incoming):
            return
        previous = self._selected_name()
        grew = tuple(item[0] for item in incoming) != tuple(item[0] for item in current)
        self.events = list(events)
        self.names.delete(0, "end")
        for event in self.events:
            self.names.insert("end", str(event.get("title") or event.get("username") or ""))
        if not self.events:
            self.detail.configure(text="")
            return
        index = 0
        if previous:
            for position, event in enumerate(self.events):
                if event.get("username") == previous:
                    index = position
                    break
        self.names.selection_clear(0, "end")
        self.names.selection_set(index)
        self.names.activate(index)
        self.names.see(index)
        self._show_body()
        if grew:
            self.deiconify()
            self.lift()

    def _selected_name(self) -> str:
        if not self.events:
            return ""
        try:
            picked = self.names.curselection()
        except tk.TclError:
            return ""
        index = int(picked[0]) if picked else 0
        if index < 0 or index >= len(self.events):
            return ""
        return str(self.events[index].get("username") or "")

    def _show_body(self) -> None:
        name = self._selected_name()
        body = ""
        for event in self.events:
            if event.get("username") == name:
                body = str(event.get("body") or "").strip()
                break
        self.detail.configure(text=body)

    def _add(self) -> None:
        name = self._selected_name()
        if name:
            self.on_add(name)

    def _skip(self) -> None:
        name = self._selected_name()
        if name:
            self.on_skip(name)

    def _never(self) -> None:
        # Destroying the button inside its own click makes Tk raise. Close after the click returns.
        self.after(0, self._finish_never)

    def _finish_never(self) -> None:
        try:
            alive = bool(self.winfo_exists())
        except tk.TclError:
            alive = False
        if not alive:
            return
        callback = self.on_never
        self.destroy()
        callback()

    def _hide(self) -> None:
        names = tuple(str(event.get("username") or "") for event in self.events)
        callback = self.on_hide
        self.destroy()
        callback(names)

    def destroy(self) -> None:
        try:
            super().destroy()
        except tk.TclError:
            pass
        _release_tk_objects(self)


class PageHost:
    """Sidebar pages with the select() calls the old main notebook used."""

    def __init__(self, show) -> None:
        self._show = show
        self._current: tk.Misc | None = None

    def select(self, page: tk.Misc | None = None):
        if page is None:
            return "" if self._current is None else str(self._current)
        self._current = page
        self._show(page)
        return ""


class NavItem(tk.Frame):
    def __init__(self, master: tk.Misc, text: str, command, scale: float) -> None:
        height = max(28, int(round(34 * scale)))
        super().__init__(master, height=height, cursor="hand2", highlightthickness=0, bd=0)
        self.pack_propagate(False)
        self._selected = False
        self._bar = tk.Frame(self, width=max(3, int(round(3 * scale))), highlightthickness=0, bd=0)
        self._bar.pack(side="left", fill="y")
        self._bar.pack_propagate(False)
        self._label = tk.Label(
            self,
            text=text,
            anchor="w",
            padx=max(8, int(round(10 * scale))),
            cursor="hand2",
            bd=0,
            highlightthickness=0,
        )
        self._label.pack(side="left", fill="both", expand=True)
        for widget in (self, self._bar, self._label):
            widget.bind("<Button-1>", lambda _event: command())
        self._chrome_paint = self._paint
        self._paint(active_palette(self))

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self._paint(active_palette(self))

    def _paint(self, palette: Palette) -> None:
        bg = palette.nav_on_bg if self._selected else palette.sidebar
        fg = palette.nav_on_fg if self._selected else palette.nav
        bar = palette.accent if self._selected else bg
        self.configure(bg=bg)
        self._bar.configure(bg=bar)
        self._label.configure(bg=bg, fg=fg, font=_sized_font(palette, 10))

    def set_text(self, text: str) -> None:
        self._label.configure(text=text)


class DesktopApp(tk.Tk):
    def __init__(
        self,
        worker: Worker,
        config_path: Path,
        *,
        start_status_server: bool = False,
        honor_start_on_launch: bool = False,
        check_updates: bool = False,
        poll_ms: int = 2000,
    ) -> None:
        super().__init__()
        self.worker = worker
        self.config_path = config_path
        install_crash_hook(
            lambda: bool(getattr(self.worker.cfg, "crash_bundle", False)),
            lambda: crash_directory(self.worker.cfg.log_file),
            lambda: self.worker.cfg.log_file,
            lambda: self.config_path,
            app_version(),
        )
        self.poll_ms = poll_ms
        self._run_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._update_busy = False
        self._update_manual = False
        self._shutting_down = False
        self._update_prompt: UpdatePrompt | None = None
        self._download_progress: DownloadProgress | None = None
        self._syncing = False
        self._sync_cancel = threading.Event()
        self._updating_select_all = False
        self._row_items: dict[str, WantedItem] = {}
        self._wanted_rows: list[WantedItem] = []
        self._wanted_grouped: dict[str, list[WantedItem]] = {}
        self._wanted_offset = 0
        self._wanted_window = WANTED_WINDOW_MIN
        self._wanted_sort: tuple[str, bool] | None = None
        self._wanted_total = 0
        self._sql_mode = False
        self._cursors: list[tuple] = []
        self._shown: dict[str, str] = {}
        self._row_checks: dict[str, str] = {}
        self._check_images: dict[str, tk.PhotoImage] = {}
        self._choices_cache: dict[str, bool] | None = None
        self._choice_job = 0
        self._transfer_index: dict[str, int] = {}
        self.title(f"Rummagarr {app_version()}")
        self.ui_scale = read_ui_scale()
        if os.environ.get("RUMMAGARR_UI_SCALE"):
            try:
                self.tk.call("tk", "scaling", 1.0)
            except tk.TclError:
                pass
        self.geometry(f"{self.px(1280)}x{self.px(800)}")
        self.minsize(self.px(1024), self.px(640))
        self._glyph_size = max(16, int(round(16 * self.ui_scale)))
        self._settings_form: SettingsForm | None = None
        self._settings_baseline: dict[str, Config] | None = None
        self._nav_items: dict[str, NavItem] = {}
        self._theme_menus: list[tk.Menu] = []
        apply_theme(self, normalize_theme(getattr(worker.cfg, "theme", "dark")))
        saved_limit = worker.store.get_meta("gui_recent_limit")
        self.limit_var = tk.StringVar(value=str(normalize_recent_limit(saved_limit or DEFAULT_RECENT_LIMIT)))
        self.view_var = tk.StringVar(value=VIEW_EVERYTHING)
        self.search_var = tk.StringVar(value="")
        self.select_all_var = tk.IntVar(value=0)
        self.notice_var = tk.StringVar(value="")
        self.wanted_status = tk.StringVar(value="Click Sync to load the wanted list from Lidarr.")
        self._build()
        if start_status_server:
            worker.ensure_status_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(poll_ms, self._tick)
        self._favorite_prompt: FavoritePrompt | None = None
        self._favorite_prompt_key: tuple[str, ...] | None = None
        self.refresh_status()
        self.refresh_wanted()
        if honor_start_on_launch and worker.cfg.start_on_launch:
            self.after(300, self.on_start)
        if check_updates:
            self.after(400, lambda: self.check_for_updates(manual=False))
        self.after(700, self._maybe_setup_check)

    def destroy(self) -> None:
        cache = getattr(self, "_browse_cache_db", None)
        if cache is not None:
            try:
                cache.close()
            except Exception:
                pass
            self._browse_cache_db = None
        self._shutting_down = True
        self._drop_tk_cycles()
        try:
            super().destroy()
        except tk.TclError:
            pass
        _release_tk_objects(self)
        # Nav callbacks and icon images can sit in a cycle. Collect them here,
        # on the thread that created them, so a later worker collection does not
        # touch Tcl.
        import gc

        gc.collect()

    def _drop_tk_cycles(self) -> None:
        for item in list(getattr(self, "_nav_items", {}).values()):
            for widget in (item, getattr(item, "_label", None), getattr(item, "_bar", None)):
                if widget is None:
                    continue
                try:
                    widget.unbind("<Button-1>")
                except tk.TclError:
                    pass
        for label in list(getattr(self, "_finder_labels", []) or []):
            try:
                label.configure(image="")
            except tk.TclError:
                pass
        brand = getattr(self, "_brand_mark", None)
        if brand is not None:
            try:
                brand.configure(image="")
            except tk.TclError:
                pass
        self._icon_image = None
        self._brand_image = None
        self._finder_image = None
        self._check_images = {}
        self._finder_labels = []
        self._nav_items = {}

    def px(self, value: int) -> int:
        return max(1, int(round(value * self.ui_scale)))

    def _build(self) -> None:
        self._build_menu()
        shell = tk.Frame(self, highlightthickness=0, bd=0)
        shell._chrome_role = "main"
        shell.pack(fill="both", expand=True)
        self.sidebar = tk.Frame(shell, width=self.px(232), highlightthickness=0, bd=0)
        self.sidebar._chrome_role = "sidebar"
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        border = tk.Frame(shell, width=1, highlightthickness=0, bd=0)
        border._chrome_role = "sidebar_border"
        border.pack(side="left", fill="y")
        self.content = tk.Frame(shell, highlightthickness=0, bd=0)
        self.content._chrome_role = "main"
        self.content.pack(side="left", fill="both", expand=True)

        brand = tk.Frame(self.sidebar, highlightthickness=0, bd=0)
        brand._chrome_role = "sidebar"
        brand.pack(fill="x", padx=self.px(12), pady=(self.px(16), self.px(8)))
        self._load_window_icon()
        if getattr(self, "_brand_image", None) is not None:
            mark = tk.Label(brand, image=self._brand_image, bd=0, highlightthickness=0)
            mark._chrome_role = "sidebar"
            mark.pack(side="left")
            self._brand_mark = mark
        names = tk.Frame(brand, highlightthickness=0, bd=0)
        names._chrome_role = "sidebar"
        names.pack(side="left", padx=(self.px(10), 0))
        title = tk.Label(names, text="Rummagarr", anchor="w", bd=0, highlightthickness=0)
        title._chrome_role = "brand"
        title.pack(anchor="w")
        version = tk.Label(names, text=app_version(), anchor="w", bd=0, highlightthickness=0)
        version._chrome_role = "brand_muted"
        version.pack(anchor="w")

        self._nav_group(self.sidebar, "LIBRARY")
        pad = self.px(12)
        self.status_tab = ttk.Frame(self.content, style="Main.TFrame", padding=(self.px(22), self.px(18), self.px(22), self.px(14)))
        self.wanted_tab = ttk.Frame(self.content, style="Main.TFrame", padding=(self.px(22), self.px(18), self.px(22), self.px(14)))
        self.dashboard_tab = ttk.Frame(self.content, style="Main.TFrame", padding=(self.px(22), self.px(18), self.px(22), self.px(14)))
        self.stats_tab = ttk.Frame(self.content, style="Main.TFrame", padding=(self.px(22), self.px(18), self.px(22), self.px(14)))
        self.health_tab = ttk.Frame(self.content, style="Main.TFrame", padding=(self.px(22), self.px(18), self.px(22), self.px(14)))
        self.search_tab = ttk.Frame(self.content, style="Main.TFrame", padding=(self.px(22), self.px(18), self.px(22), self.px(14)))
        self.browse_tab = ttk.Frame(self.content, style="Main.TFrame", padding=(self.px(22), self.px(18), self.px(22), self.px(14)))
        self.messages_tab = ttk.Frame(self.content, style="Main.TFrame", padding=(self.px(22), self.px(18), self.px(22), self.px(14)))
        self.shared_tab = ttk.Frame(self.content, style="Main.TFrame", padding=(self.px(22), self.px(18), self.px(22), self.px(14)))
        self.settings_tab = ttk.Frame(self.content, style="Main.TFrame")
        self.notebook = PageHost(self._show_page)
        self._add_nav(self.sidebar, "Status", self.status_tab)
        self._add_nav(self.sidebar, "Wanted", self.wanted_tab)
        self._add_nav(self.sidebar, "Dashboard", self.dashboard_tab)
        self._add_nav(self.sidebar, "Stats", self.stats_tab)
        self._add_nav(self.sidebar, "Health", self.health_tab)
        self._add_nav(self.sidebar, "Search", self.search_tab)
        self._add_nav(self.sidebar, "Browse", self.browse_tab)
        self._add_nav(self.sidebar, "Messages", self.messages_tab)
        self._add_nav(self.sidebar, "Shared", self.shared_tab)
        self._nav_group(self.sidebar, "SYSTEM")
        self._add_nav(self.sidebar, "Settings", self.settings_tab)

        foot = tk.Frame(self.sidebar, highlightthickness=0, bd=0)
        foot._chrome_role = "sidebar"
        foot.pack(side="bottom", fill="x", padx=pad, pady=self.px(12))
        updates = tk.Label(foot, text="Help · Check for updates", anchor="w", cursor="hand2", bd=0, highlightthickness=0)
        updates._chrome_role = "foot"
        updates.pack(anchor="w")
        updates.bind("<Button-1>", lambda _event: self.check_for_updates(manual=True))
        about = tk.Label(foot, text="About Rummagarr", anchor="w", cursor="hand2", bd=0, highlightthickness=0)
        about._chrome_role = "foot"
        about.pack(anchor="w")
        about.bind("<Button-1>", lambda _event: self.show_about())

        self._boards: list[SearchBoard] = []
        self._search_menu: tk.Menu | None = None
        self._shared_rows: list[dict] = []
        self._build_status(self.status_tab)
        self._build_wanted(self.wanted_tab)
        self._build_dashboard(self.dashboard_tab)
        self._build_stats(self.stats_tab)
        self._build_health(self.health_tab)
        self._build_search(self.search_tab)
        self._build_browse(self.browse_tab)
        self._build_messages(self.messages_tab)
        self._build_shared(self.shared_tab)
        apply_theme(self, self._theme_name)
        self.notebook.select(self.status_tab)

    def _nav_group(self, parent: tk.Misc, text: str) -> None:
        label = tk.Label(parent, text=text, anchor="w", bd=0, highlightthickness=0, padx=self.px(18))
        label._chrome_role = "group"
        label.pack(fill="x", pady=(self.px(12), self.px(4)))

    def _add_nav(self, parent: tk.Misc, text: str, page: ttk.Frame) -> None:
        item = NavItem(parent, text, lambda page=page: self.notebook.select(page), self.ui_scale)
        item.pack(fill="x", padx=self.px(8), pady=1)
        self._nav_items[str(page)] = item

    def _show_page(self, page: tk.Misc) -> None:
        if page is self.settings_tab:
            self._ensure_settings()
        for frame in (
            self.status_tab,
            self.wanted_tab,
            self.dashboard_tab,
            self.stats_tab,
            self.health_tab,
            self.search_tab,
            self.browse_tab,
            self.messages_tab,
            self.shared_tab,
            self.settings_tab,
        ):
            if frame.winfo_manager():
                frame.pack_forget()
        page.pack(in_=self.content, fill="both", expand=True)
        if page is self.dashboard_tab:
            self.refresh_dashboard()
        elif page is self.stats_tab:
            self.refresh_stats()
        elif page is self.health_tab:
            self.refresh_health()
        elif page is self.messages_tab:
            self._message_signature = None
            self.refresh_messages()
        selected = str(page)
        for key, item in self._nav_items.items():
            item.set_selected(key == selected)

    def _load_window_icon(self) -> None:
        path = icon_png_path()
        if not path.is_file():
            return
        try:
            image = tk.PhotoImage(file=str(path))
        except tk.TclError:
            log.exception("Could not load the window icon")
            return
        self._icon_image = image
        try:
            self.iconphoto(True, image)
        except tk.TclError:
            log.exception("Could not set the window icon")
        target = self.px(36)
        factor = max(1, int(round(image.width() / target))) if image.width() else 1
        self._brand_image = image.subsample(factor, factor) if factor > 1 else image

    def _card(self, parent: tk.Misc, *, padding: tuple[int, int, int, int] | None = None) -> tk.Frame:
        shell = tk.Frame(parent, highlightthickness=1, bd=0)
        shell._chrome_role = "card"
        if padding is None:
            padding = (self.px(12), self.px(10), self.px(12), self.px(6))
        inner = ttk.Frame(shell, style="Card.TFrame", padding=padding)
        inner.pack(fill="both", expand=True)
        shell.inner = inner
        return shell

    def _page_header(self, parent: ttk.Frame, title: str, subtitle: str) -> None:
        head = ttk.Frame(parent, style="Main.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text=title, style="PageTitle.TLabel").pack(anchor="w")
        if subtitle:
            ttk.Label(head, text=subtitle, style="Muted.TLabel", wraplength=self.px(900)).pack(
                anchor="w", pady=(self.px(4), 0)
            )

    def _ensure_order_vars(self) -> None:
        if getattr(self, "search_order_label_var", None) is not None:
            return
        cfg = self.worker.cfg
        self.search_order_label_var = tk.StringVar(value=order_label(cfg.search_order))
        self.search_direction_label_var = tk.StringVar(value=direction_label(cfg.search_order_direction))

    def _pack_search_order(self, parent: ttk.Frame) -> None:
        """The same Search order control on Status and Wanted."""
        self._ensure_order_vars()
        row = ttk.Frame(parent, style="Main.TFrame")
        row.pack(fill="x", pady=(self.px(8), 0))
        ttk.Label(row, text="Search order").pack(side="left")
        order = ttk.Combobox(
            row,
            textvariable=self.search_order_label_var,
            values=[label for _key, label in SEARCH_ORDERS],
            state="readonly",
            width=24,
        )
        order.pack(side="left", padx=(self.px(8), 0))
        direction = ttk.Combobox(
            row,
            textvariable=self.search_direction_label_var,
            values=["Ascending", "Descending"],
            state="readonly",
            width=14,
        )
        direction.pack(side="left", padx=(self.px(8), 0))
        order.bind("<<ComboboxSelected>>", self._on_search_order_picked)
        direction.bind("<<ComboboxSelected>>", self._on_search_order_picked)

    def _sync_order_boxes(self) -> None:
        self._ensure_order_vars()
        cfg = self.worker.cfg
        self.search_order_label_var.set(order_label(cfg.search_order))
        self.search_direction_label_var.set(direction_label(cfg.search_order_direction))

    def _on_search_order_picked(self, _event=None) -> None:
        order = normalize_search_order(self.search_order_label_var.get())
        previous = normalize_search_order(self.worker.cfg.search_order)
        if order != previous:
            direction = default_direction(order)
            self.search_direction_label_var.set(direction_label(direction))
        else:
            direction = normalize_search_direction(self.search_direction_label_var.get())
        self._save_search_order(order, direction)

    def _save_search_order(self, order: str, direction: str) -> None:
        order = normalize_search_order(order)
        direction = normalize_search_direction(direction)
        self.worker.cfg = replace(self.worker.cfg, search_order=order, search_order_direction=direction)
        try:
            save_config(self.config_path, self.worker.cfg)
        except OSError:
            log.exception("Could not save the search order")
        self.worker.store.refresh_display_order(order, direction)
        self._wanted_sort = None
        form = getattr(self, "_settings_form", None)
        if form is not None:
            form._set_field(next(spec for spec in BEHAVIOR_FIELDS if spec.key == "search_order"), order)
            form._set_field(next(spec for spec in BEHAVIOR_FIELDS if spec.key == "search_order_direction"), direction)
            form.base = self.worker.cfg
        self._sync_order_boxes()
        if getattr(self, "wanted", None) is not None:
            self.refresh_wanted()

    def _ensure_settings(self) -> None:
        if self._settings_form is not None:
            return
        self._settings_baseline = {"cfg": self.worker.cfg}

        def on_saved(cfg: Config) -> None:
            baseline = self._settings_baseline or {"cfg": self.worker.cfg}
            message = settings_apply_message(baseline["cfg"], cfg)
            baseline["cfg"] = cfg
            self._settings_baseline = baseline
            previous = self.worker.cfg
            self.worker.cfg = cfg
            if (
                previous.lidarr_url,
                previous.lidarr_api_key,
                previous.lidarr_track_concurrency,
            ) != (cfg.lidarr_url, cfg.lidarr_api_key, cfg.lidarr_track_concurrency):
                self.worker.retarget_lidarr()
            self.worker.apply_bridge()
            if getattr(self.worker, "_bridge_error", ""):
                message = f"{message} {self.worker._bridge_error}"
            self.worker.store.refresh_display_order(cfg.search_order, cfg.search_order_direction)
            self._wanted_sort = None
            self._sync_order_boxes()
            if getattr(self, "wanted", None) is not None:
                self.refresh_wanted()
            self.worker.apply_share_settings(scan=False)
            saved_limit = self.worker.store.get_meta("gui_recent_limit")
            if saved_limit:
                self.limit_var.set(str(normalize_recent_limit(saved_limit)))
            self.notice_var.set(message)
            if self._settings_form is not None:
                self._settings_form.base = cfg
                self._settings_form.message_var.set(message)
            self.refresh_notices()

        def on_cancel() -> None:
            form = self._settings_form
            self._settings_form = None
            self._settings_baseline = None
            if form is not None:
                form.destroy()
            self.notebook.select(self.status_tab)

        form = SettingsForm(
            self.settings_tab,
            self.config_path,
            self.worker.cfg,
            on_saved,
            on_cancel,
            store=self.worker.store,
            worker=self.worker,
            on_theme=self.apply_live_theme,
            heading="Settings",
        )
        form.pack(fill="both", expand=True)
        self._settings_form = form

    def apply_live_theme(self, name: str) -> None:
        name = normalize_theme(name)
        apply_theme(self, name)
        self._restyle_glyphs()
        self.worker.cfg = replace(self.worker.cfg, theme=name)
        if self._settings_form is not None:
            self._settings_form.base = self.worker.cfg
        if self._settings_baseline is not None:
            self._settings_baseline["cfg"] = replace(self._settings_baseline["cfg"], theme=name)
        try:
            save_config(self.config_path, self.worker.cfg)
        except OSError:
            log.exception("Could not save the theme")

    def _build_menu(self) -> None:
        menu = tk.Menu(self, tearoff=0)
        help_menu = tk.Menu(menu, tearoff=0)
        help_menu.add_command(label="Check for updates", command=lambda: self.check_for_updates(manual=True))
        help_menu.add_command(label="About", command=self.show_about)
        help_menu.add_command(label="Setup check", command=self.show_setup_check)
        help_menu.add_command(label="Backup settings", command=self.backup_settings)
        help_menu.add_command(label="Restore settings", command=self.restore_settings)
        help_menu.add_command(label="Open crash folder", command=self.open_crash_folder)
        menu.add_cascade(label="Help", menu=help_menu)
        self.configure(menu=menu)
        self.help_menu = help_menu
        self._theme_menus.append(help_menu)
        self._theme_menus.append(menu)

    def report_callback_exception(self, exc, val, tb) -> None:
        try:
            if bool(getattr(self.worker.cfg, "crash_bundle", False)):
                import traceback

                write_crash_bundle(
                    crash_directory(self.worker.cfg.log_file),
                    version=app_version(),
                    error_text="".join(traceback.format_exception(exc, val, tb)),
                    log_path=self.worker.cfg.log_file,
                    config_path=self.config_path,
                    enabled=True,
                )
        except Exception:
            log.exception("Could not write the crash bundle")
        super().report_callback_exception(exc, val, tb)

    def open_crash_folder(self) -> None:
        note = open_folder(crash_directory(self.worker.cfg.log_file))
        if note and getattr(self, "notice_var", None) is not None:
            self.notice_var.set(note)

    def show_about(self) -> None:
        existing = getattr(self, "_about_window", None)
        if existing is not None:
            try:
                existing.destroy()
            except tk.TclError:
                pass
        window = tk.Toplevel(self)
        window.title("About Rummagarr")
        window.transient(self)
        window.resizable(False, False)
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"Rummagarr {app_version()}", style="PageTitle.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text="Downloads missing music from Soulseek into Lidarr.\nHelp → Check for updates looks for a newer release.",
            wraplength=420,
        ).pack(anchor="w", pady=(8, 12))
        ttk.Button(frame, text="Donate", command=self.open_donate).pack(anchor="w")
        ttk.Label(frame, text="Opens the GitHub Sponsors page.", style="Muted.TLabel").pack(anchor="w", pady=(8, 0))
        ttk.Button(frame, text="Close", command=window.destroy).pack(anchor="e", pady=(12, 0))
        self._about_window = window
        window.bind("<Destroy>", lambda _event: setattr(self, "_about_window", None), add="+")

    def open_donate(self) -> None:
        webbrowser.open(DONATE_URL)

    def show_setup_check(self) -> None:
        checks = local_setup_checks(self.worker.cfg)
        text = format_setup_report(checks)
        existing = getattr(self, "_setup_window", None)
        if existing is not None:
            try:
                existing.destroy()
            except tk.TclError:
                pass
        window = tk.Toplevel(self)
        window.title("Setup check")
        window.transient(self)
        window.geometry(f"{self.px(560)}x{self.px(420)}")
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Setup check", style="PageTitle.TLabel").pack(anchor="w")
        box = tk.Text(frame, wrap="word", height=14, bd=0, highlightthickness=0)
        box.pack(fill="both", expand=True, pady=(8, 8))
        box.insert("1.0", text)
        box.configure(state="disabled")
        self._setup_text = box

        def test_lidarr() -> None:
            cfg = self.worker.cfg

            def work() -> None:
                client = LidarrClient(cfg.lidarr_url, cfg.lidarr_api_key, timeout=15)
                try:
                    status = client.ping()
                    version = status.get("version") or "ok"
                    item = lidarr_check(True, f"Lidarr answered ({version}).")
                except Exception as exc:
                    item = lidarr_check(False, str(exc) or "Lidarr did not answer.")
                finally:
                    client.close()
                line = format_setup_report([item]).split("\n", 1)[0]

                def paint() -> None:
                    try:
                        box.configure(state="normal")
                        box.insert("end", "\n" + line)
                        box.configure(state="disabled")
                    except tk.TclError:
                        pass

                self._post(paint)

            threading.Thread(target=work, name="setup-check", daemon=True).start()

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Test Lidarr", command=test_lidarr).pack(side="left")
        ttk.Button(buttons, text="Close", command=window.destroy).pack(side="right")
        self._setup_window = window

    def _maybe_setup_check(self) -> None:
        try:
            checks = local_setup_checks(self.worker.cfg)
        except Exception:
            return
        if all(item.ok for item in checks):
            return
        if self.worker.store.get_meta("setup_check_seen") == "1":
            return
        self.worker.store.set_meta("setup_check_seen", "1")
        self.show_setup_check()

    def backup_settings(self) -> None:
        dest = filedialog.asksaveasfilename(
            parent=self,
            title="Backup Rummagarr settings",
            defaultextension=".zip",
            filetypes=[("Zip", "*.zip")],
            initialfile="Rummagarr-backup.zip",
        )
        if not dest:
            return
        cfg = self.worker.cfg
        try:
            names = create_backup(
                Path(dest),
                config_path=self.config_path,
                state_db=cfg.state_db,
                log_file=cfg.log_file,
            )
        except Exception as exc:
            messagebox.showerror("Backup", str(exc) or "Could not write the backup.", parent=self)
            return
        messagebox.showinfo("Backup", "Saved " + ", ".join(names) + f" to {dest}", parent=self)

    def restore_settings(self) -> None:
        src = filedialog.askopenfilename(
            parent=self,
            title="Restore Rummagarr settings",
            filetypes=[("Zip", "*.zip")],
        )
        if not src:
            return
        if not messagebox.askyesno(
            "Restore",
            "Replace the current settings and database with this backup? Rummagarr will close. Open it again to use the restored files.",
            parent=self,
        ):
            return
        cfg = self.worker.cfg
        try:
            if getattr(self.worker, "control", None) is not None:
                self.worker.control.stop()
            self.worker.close()
            names = restore_backup(
                Path(src),
                config_path=self.config_path,
                state_db=cfg.state_db,
                log_file=cfg.log_file,
            )
        except Exception as exc:
            messagebox.showerror("Restore", str(exc) or "Could not restore the backup.", parent=self)
            return
        messagebox.showinfo("Restore", "Restored " + ", ".join(names) + ". Rummagarr will close.", parent=self)
        self._shutting_down = True
        self.destroy()

    def _build_dashboard(self, parent: ttk.Frame) -> None:
        self._page_header(
            parent,
            "Dashboard",
            "Missing albums by artist, tag, and decade, plus how long the queue would take at the recent rate.",
        )
        bar = ttk.Frame(parent, style="Main.TFrame")
        bar.pack(fill="x", pady=(self.px(12), self.px(8)))
        ttk.Button(bar, text="Refresh", command=self.refresh_dashboard).pack(side="left")
        self.dashboard_text = tk.Text(parent, wrap="word", height=24, bd=0, highlightthickness=0)
        self.dashboard_text.pack(fill="both", expand=True)
        self.dashboard_text.configure(state="disabled")

    def refresh_dashboard(self) -> None:
        widget = getattr(self, "dashboard_text", None)
        if widget is None:
            return
        try:
            albums = self.worker.store.wanted_albums()
            finished = self.worker.store.recent_completed_downloads(400)
            nxt = queued_next(self.worker.store, self.worker.cfg, 5)
            text = format_summary(wanted_summary(albums, finished, nxt))
            notes = self.worker.store.dry_run_notes()
            if notes:
                text += "\n\nDry run\n" + "\n".join(f"  {line}" for line in notes[:12])
        except Exception as exc:
            log.exception("Could not build the wanted dashboard")
            text = f"Could not build the dashboard: {exc}"
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _build_stats(self, parent: ttk.Frame) -> None:
        self._page_header(
            parent,
            "Stats",
            "Finished downloads, album jobs, and files uploaded from this PC.",
        )
        bar = ttk.Frame(parent, style="Main.TFrame")
        bar.pack(fill="x", pady=(self.px(12), self.px(8)))
        ttk.Button(bar, text="Refresh", command=self.refresh_stats).pack(side="left")
        self.stats_text = tk.Text(parent, wrap="word", height=24, bd=0, highlightthickness=0)
        self.stats_text.pack(fill="both", expand=True)
        self.stats_text.configure(state="disabled")

    def refresh_stats(self) -> None:
        widget = getattr(self, "stats_text", None)
        if widget is None:
            return
        try:
            text = format_stats(
                self.worker.store.activity_summary(),
                self.worker.store.upload_totals(),
                self.worker.store.get_meta("last_browse_download") or "",
            )
        except Exception as exc:
            log.exception("Could not build the stats page")
            text = f"Could not build the stats: {exc}"
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _build_health(self, parent: ttk.Frame) -> None:
        self._page_header(
            parent,
            "Library health",
            "Missing tracks, edition titles that disagree, files sitting in the folder root, and leftovers in Review.",
        )
        bar = ttk.Frame(parent, style="Main.TFrame")
        bar.pack(fill="x", pady=(self.px(12), self.px(8)))
        ttk.Button(bar, text="Refresh", command=self.refresh_health).pack(side="left")
        ttk.Button(bar, text="Re-search", command=self._health_research).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Rescan", command=self._health_rescan).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Delete", command=self._health_delete).pack(side="left", padx=(8, 0))
        columns = ("kind", "artist", "album", "detail")
        self.health_tree = ttk.Treeview(parent, columns=columns, show="headings", selectmode="browse")
        self.health_tree.heading("kind", text="Kind")
        self.health_tree.heading("artist", text="Artist")
        self.health_tree.heading("album", text="Album")
        self.health_tree.heading("detail", text="Detail")
        self.health_tree.column("kind", width=self.px(110), stretch=False)
        self.health_tree.column("artist", width=self.px(180), stretch=False)
        self.health_tree.column("album", width=self.px(220), stretch=False)
        self.health_tree.column("detail", width=self.px(420))
        self.health_tree.pack(fill="both", expand=True)
        self._health_rows: list[dict] = []

    def _health_sources(self) -> list[dict]:
        rows = health_from_albums(self.worker.store.wanted_albums())
        roots = [self.worker.cfg.complete_dir]
        scan = (self.worker.cfg.lidarr_scan_path or "").strip()
        if scan:
            other = Path(scan)
            try:
                same = other.resolve() == Path(self.worker.cfg.complete_dir).resolve()
            except OSError:
                same = str(other) == str(self.worker.cfg.complete_dir)
            if not same:
                roots.append(other)
        for root in roots:
            rows.extend(scan_folder(root))
        return rows

    def refresh_health(self) -> None:
        tree = getattr(self, "health_tree", None)
        if tree is None:
            return
        try:
            rows = self._health_sources()
        except Exception:
            log.exception("Could not scan library health")
            rows = []
        self._health_rows = rows
        tree.delete(*tree.get_children())
        for index, row in enumerate(rows):
            tree.insert(
                "",
                "end",
                iid=str(index),
                values=(row.get("kind") or "", row.get("artist") or "", row.get("album") or "", row.get("detail") or ""),
            )

    def _selected_health(self) -> dict | None:
        tree = getattr(self, "health_tree", None)
        if tree is None:
            return None
        selected = tree.selection()
        if not selected:
            self.notice_var.set("Select a row first.")
            return None
        try:
            return self._health_rows[int(selected[0])]
        except (ValueError, IndexError):
            return None

    def _health_research(self) -> None:
        row = self._selected_health()
        if row is None:
            return
        note = research_note(self.worker.store, row)
        self.notice_var.set(note)
        self.refresh_health()

    def _health_rescan(self) -> None:
        row = self._selected_health() or {}
        path = str(row.get("path") or "")
        target = self.worker._import_folder()
        if path:
            candidate = Path(path)
            if candidate.is_dir():
                target = str(candidate)
            elif candidate.parent.is_dir():
                target = str(candidate.parent)
        self.notice_var.set(f"Asking Lidarr to rescan {target}")

        def work() -> None:
            try:
                self.worker.lidarr.scan_downloaded(target)
                text = f"Asked Lidarr to rescan {target}"
            except Exception as exc:
                text = f"Could not rescan: {exc}"
            self._post(lambda text=text: self.notice_var.set(text))

        threading.Thread(target=work, name="health-rescan", daemon=True).start()

    def _health_delete(self) -> None:
        row = self._selected_health()
        if row is None:
            return
        path = str(row.get("path") or "")
        if not path:
            self.notice_var.set("That row has no file to delete.")
            return
        if not messagebox.askyesno("Delete", f"Delete {path}?", parent=self):
            return
        root = self._health_root_for(Path(path))
        try:
            delete_inside(root, Path(path))
        except Exception as exc:
            self.notice_var.set(str(exc))
            return
        self.notice_var.set(f"Deleted {path}")
        self.refresh_health()

    def _health_root_for(self, path: Path) -> Path:
        roots = [Path(self.worker.cfg.complete_dir)]
        scan = (self.worker.cfg.lidarr_scan_path or "").strip()
        if scan:
            roots.append(Path(scan))
        resolved = path.resolve()
        for root in roots:
            try:
                base = root.resolve()
            except OSError:
                continue
            if resolved == base or base in resolved.parents:
                return root
        return Path(self.worker.cfg.complete_dir)

    def _build_status(self, parent: ttk.Frame) -> None:
        self._page_header(parent, "Status", "Soulseek worker for Lidarr wanted music")
        controls = ttk.Frame(parent, style="Main.TFrame")
        controls.pack(fill="x", pady=(self.px(14), 0))
        self.start_btn = ttk.Button(controls, text="Start", style="Start.TButton", command=self.on_start)
        self.pause_btn = ttk.Button(controls, text="Pause", command=self.on_pause)
        self.stop_btn = ttk.Button(controls, text="Stop", style="Stop.TButton", command=self.on_stop)
        self.skip_btn = ttk.Button(controls, text="Skip", command=self.on_skip)
        self.refresh_btn = ttk.Button(controls, text="Refresh", command=self.refresh_status)
        self.cancel_all_btn = ttk.Button(controls, text="Cancel all", command=self.on_cancel_all)
        for button in (self.start_btn, self.pause_btn, self.stop_btn, self.skip_btn):
            button.pack(side="left", padx=(0, self.px(8)))
        self.cancel_all_btn.pack(side="right")
        self.refresh_btn.pack(side="right", padx=(0, self.px(8)))

        banner = tk.Frame(parent, highlightthickness=1, bd=0)
        banner._chrome_role = "card"
        banner.pack(fill="x", pady=(self.px(12), 0))
        status_row = tk.Frame(banner, highlightthickness=0, bd=0)
        status_row._chrome_role = "card"
        status_row.pack(fill="x", padx=self.px(12), pady=self.px(10))
        dot = self.px(14)
        self.activity_dot = tk.Canvas(status_row, width=dot, height=dot, highlightthickness=0, bd=0)
        self.activity_dot._chrome_role = "canvas"
        self.activity_dot.pack(side="left", padx=(0, self.px(8)))
        self._pulse_on = False
        text_col = ttk.Frame(status_row, style="Card.TFrame")
        text_col.pack(side="left", fill="x", expand=True)
        self.status_line = ttk.Label(text_col, text="Stopped · Soulseek offline · Stopped", style="Banner.TLabel")
        self.status_line.pack(anchor="w", fill="x")
        self.details_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            status_row,
            text="Details",
            variable=self.details_var,
            command=self.refresh_status,
        ).pack(side="right")
        self.session_line = ttk.Label(text_col, text="", style="CardMuted.TLabel", wraplength=self.px(980), justify="left")
        self.session_line.pack(anchor="w", fill="x", pady=(self.px(2), 0))
        self.notice_line = ttk.Label(parent, textvariable=self.notice_var, style="Muted.TLabel")
        self.notice_line.pack(fill="x", pady=(self.px(6), 0))
        self.summary_var = tk.StringVar(value="")
        self.summary_line = ttk.Label(parent, textvariable=self.summary_var, style="Muted.TLabel")
        self.summary_line.pack(fill="x", pady=(self.px(2), 0))
        self.notices = ttk.Frame(parent, style="Main.TFrame")
        self.notices.pack(fill="x", pady=(self.px(4), 0))
        self._pack_search_order(parent)
        self.auto_import_var = tk.BooleanVar(value=bool(self.worker.cfg.auto_import_after_artist))
        self.auto_import = ttk.Checkbutton(
            parent,
            text="Auto-import to Lidarr after each artist",
            variable=self.auto_import_var,
            command=self.on_auto_import,
        )
        self.auto_import.pack(anchor="w", pady=(self.px(8), 0))

        grid = ttk.Frame(parent, style="Main.TFrame")
        grid.pack(fill="both", expand=True, pady=(self.px(10), 0))
        grid.columnconfigure(0, weight=3)
        grid.columnconfigure(1, weight=2)
        grid.rowconfigure(0, weight=1)
        downloads = self._card(grid)
        recent_card = self._card(grid)
        downloads.grid(row=0, column=0, sticky="nsew", padx=(0, self.px(8)))
        recent_card.grid(row=0, column=1, sticky="nsew")

        ttk.Label(downloads.inner, text="DOWNLOADS", style="CardTitle.TLabel").pack(anchor="w", pady=(0, self.px(6)))
        frame, self.transfers = scrolled_tree(
            downloads.inner,
            (
                ("file", "File", 280, True),
                ("user", "User", 120, False),
                ("state", "Status", 110, False),
                ("progress", "Progress", 150, False),
                ("action", "", 70, False),
            ),
            frame_style="Card.TFrame",
        )
        frame.pack(fill="both", expand=True)
        self._bind_filling_tree(self.transfers)
        palette = active_palette(self)
        self.transfers.tag_configure("ok", foreground=palette.ok)
        self.transfers.tag_configure("bad", foreground=palette.bad)
        self.transfers.bind("<Button-1>", self.on_transfer_click)

        recent_head = ttk.Frame(recent_card.inner, style="Card.TFrame")
        recent_head.pack(fill="x", pady=(0, self.px(6)))
        ttk.Label(recent_head, text="RECENT COMPLETED", style="CardTitle.TLabel").pack(side="left")
        self.limit_box = ttk.Combobox(
            recent_head,
            textvariable=self.limit_var,
            values=[str(number) for number in RECENT_LIMITS],
            state="readonly",
            width=6,
        )
        self.limit_box.pack(side="right")
        ttk.Label(recent_head, text="Show", style="CardMuted.TLabel").pack(side="right", padx=(0, self.px(6)))
        self.limit_box.bind("<<ComboboxSelected>>", self.on_limit)

        frame, self.recent = scrolled_tree(
            recent_card.inner,
            (
                ("when", "When", 130, False),
                ("album", "Artist / album", 180, True),
                ("file", "File", 160, True),
                ("user", "User", 100, False),
            ),
            frame_style="Card.TFrame",
        )
        frame.pack(fill="both", expand=True)
        self._bind_filling_tree(self.recent)
        port = self.worker.cfg.status_port
        host = self.worker.cfg.status_bind
        ttk.Label(
            parent,
            text=f"The browser page stays at http://{host}:{port} while the worker is running.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(self.px(8), 0))

    def _build_wanted(self, parent: ttk.Frame) -> None:
        self._page_header(
            parent,
            "Wanted",
            "Checked rows are the only ones the worker searches. New rows from Sync start checked. The list is kept on this computer and opens without another Sync.",
        )
        toolbar = ttk.Frame(parent, style="Main.TFrame")
        toolbar.pack(fill="x", pady=(self.px(14), self.px(8)))
        self.sync_button = ttk.Button(toolbar, text="Sync", style="Start.TButton", command=self.on_sync)
        self.sync_button.pack(side="left")
        self._pack_search_order(toolbar)
        ttk.Label(toolbar, text="View", style="Muted.TLabel").pack(side="left", padx=(self.px(14), self.px(6)))
        segments = tk.Frame(toolbar, highlightthickness=1, bd=0)
        segments._chrome_role = "field"
        segments.pack(side="left")
        for view in VIEWS:
            ttk.Radiobutton(
                segments,
                text=view,
                value=view,
                variable=self.view_var,
                command=self.on_view,
                style="Segment.TRadiobutton",
            ).pack(side="left")
        self.select_all = ttk.Checkbutton(
            toolbar,
            text="Select all",
            variable=self.select_all_var,
            command=self.on_select_all,
        )
        self.select_all.pack(side="right")
        search_row = ttk.Frame(parent, style="Main.TFrame")
        search_row.pack(fill="x", pady=(0, self.px(6)))
        ttk.Label(search_row, text="Artist", style="Muted.TLabel").pack(side="left")
        self.search_entry = ttk.Entry(search_row, textvariable=self.search_var)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(self.px(8), 0))
        self.search_entry.bind("<KeyRelease>", lambda _event: self.on_search())
        self.count_label = ttk.Label(parent, textvariable=self.wanted_status, style="Muted.TLabel")
        self.count_label.pack(anchor="w", pady=(0, self.px(6)))
        card = self._card(parent, padding=(0, 0, 0, 0))
        card.pack(fill="both", expand=True)
        frame = card.inner
        columns = (
            ("kind", "Kind", 80, False),
            ("artist", "Artist", 180, True),
            ("album", "Album", 220, True),
            ("song", "Song", 200, True),
            ("detail", "Detail", 180, True),
            ("find", "", 36, False),
        )
        names = [column[0] for column in columns]
        self._check_images = {state: self._checkbox_image(state) for state in ("on", "off", "mixed")}
        self._finder_image = self._magnifier_image()
        self._finder_labels: list[tk.Label] = []
        self.wanted = ttk.Treeview(frame, columns=names, show="tree headings", selectmode="browse")
        self.wanted_scroll = ttk.Scrollbar(frame, orient="vertical", command=self._wanted_scroll)
        self.wanted.grid(row=0, column=0, sticky="nsew")
        self.wanted_scroll.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.wanted.column("#0", width=self.px(44), minwidth=self.px(36), stretch=False, anchor="center")
        self.wanted.heading("#0", text="")
        for name, heading, width, stretch in columns:
            scaled = max(self.px(28), int(round(width * self.ui_scale)))
            if name == "find":
                self.wanted.heading(name, text="")
                self.wanted.column(name, width=scaled, minwidth=scaled, stretch=False, anchor="center")
                continue
            self.wanted.heading(name, text=heading, command=lambda column=name: self.on_wanted_sort(column))
            self.wanted.column(name, width=scaled, minwidth=self.px(40), anchor="w", stretch=stretch)
        self.wanted.bind("<Button-1>", self.on_wanted_click)
        self.wanted.bind("<space>", self.on_wanted_space)
        self.wanted.bind("<MouseWheel>", self._wanted_wheel)
        self.wanted.bind("<Button-4>", self._wanted_wheel)
        self.wanted.bind("<Button-5>", self._wanted_wheel)
        self.wanted.bind("<Up>", self._wanted_key)
        self.wanted.bind("<Down>", self._wanted_key)
        self.wanted.bind("<Configure>", self._wanted_resized)
        self.wanted.bind("<Configure>", lambda _event: self._place_finders(), add="+")
        self._bind_filling_tree(self.wanted)

    def _tick(self) -> None:
        if self._shutting_down:
            return
        try:
            if not self.winfo_exists():
                return
            self.refresh_status()
            self.refresh_shared()
            if self.worker.take_config_dirty():
                save_config(self.config_path, self.worker.cfg)
        except tk.TclError:
            return
        self.after(self.poll_ms, self._tick)

    def _on_close(self) -> None:
        self._shutting_down = True
        try:
            self.worker.control.stop()
        except Exception:
            log.exception("Could not stop the worker")
        try:
            self.worker.close()
        except Exception:
            log.exception("Could not close the worker")
        self.destroy()

    def open_settings(self) -> None:
        self.notebook.select(self.settings_tab)

    def on_start(self) -> None:
        if self._thread and self._thread.is_alive():
            if self.worker.control.is_paused():
                self.worker.control.resume()
            elif self.worker.control.is_stopped():
                self.notice_var.set("The worker is still stopping. Click Start again in a moment.")
            self.refresh_status()
            return
        if not self._run_lock.acquire(blocking=False):
            return
        self.worker.control.start()

        def run() -> None:
            try:
                asyncio.run(self.worker.run_forever(autostart=False))
            except Exception:
                log.exception("Worker thread failed")
            finally:
                self._run_lock.release()

        self._thread = threading.Thread(target=run, name="lidarr-soulseek", daemon=True)
        self._thread.start()
        self.refresh_status()

    def on_pause(self) -> None:
        if self.worker.control.is_paused():
            self.worker.control.resume()
        elif self.worker.control.is_running():
            self.worker.control.pause()
        self.refresh_status()

    def on_stop(self) -> None:
        self.worker.control.stop()
        self.refresh_status()

    def on_skip(self) -> None:
        if self.worker.control.is_stopped():
            return
        self.worker.control.arm_skip(self.worker.current_key)
        self.notice_var.set("Skipping the current item.")
        self.refresh_status()

    def on_limit(self, _event=None) -> None:
        limit = normalize_recent_limit(self.limit_var.get())
        self.limit_var.set(str(limit))
        self.worker.store.set_meta("gui_recent_limit", str(limit))
        self.refresh_status()

    def on_cancel_all(self) -> None:
        self._schedule_cancel(all_items=True, index=None)

    def on_transfer_click(self, event) -> None:
        row = self.transfers.identify_row(event.y)
        column = self.transfers.identify_column(event.x)
        if not row or column != "#5":
            return
        index = self._transfer_index.get(row)
        if index is None:
            return
        self._schedule_cancel(all_items=False, index=index)

    def _schedule_cancel(self, *, all_items: bool, index: int | None) -> None:
        loop = getattr(self.worker, "_loop", None)
        if loop is None or not loop.is_running():
            self.notice_var.set("Start the worker before cancelling a download.")
            return
        asyncio.run_coroutine_threadsafe(
            self.worker.cancel_downloads(all_items=all_items, index=index),
            loop,
        )

    def _set_button(self, button: ttk.Button, enabled: bool) -> None:
        button.state(["!disabled"] if enabled else ["disabled"])

    def on_auto_import(self) -> None:
        enabled = bool(self.auto_import_var.get())
        self.worker.cfg = replace(self.worker.cfg, auto_import_after_artist=enabled)
        try:
            save_config(self.config_path, self.worker.cfg)
        except OSError:
            log.exception("Could not save the auto-import setting")

    def refresh_status(self) -> None:
        limit = normalize_recent_limit(self.limit_var.get())
        snapshot = self.worker.ui_snapshot(recent_limit=limit)
        self.status_line.configure(text=format_status_line(snapshot))
        if getattr(self, "session_line", None) is not None:
            detail = ""
            if getattr(self, "details_var", None) is not None and self.details_var.get():
                detail = str(snapshot.get("soulseek_detail") or "")
            self.session_line.configure(text=detail)
        if getattr(self, "summary_var", None) is not None:
            self.summary_var.set(str(snapshot.get("run_summary") or ""))
        self._paint_activity_dot(bool(snapshot.get("busy")))
        self._refresh_open_search_states()
        state = snapshot.get("run_state") or "stopped"
        self._set_button(self.start_btn, state == "stopped")
        self._set_button(self.pause_btn, state in {"running", "paused"})
        self._set_button(self.stop_btn, state in {"running", "paused"})
        self._set_button(self.skip_btn, state in {"running", "paused"})
        self.pause_btn.configure(text="Resume" if state == "paused" else "Pause")
        if self.worker.cfg.convert_to_mp3 and shutil.which("ffmpeg") is None and not self.notice_var.get():
            self.notice_var.set("ffmpeg was not found. Lossless files cannot be converted until it is installed.")
        self._fill_transfers(snapshot.get("transfers") or [])
        self._fill_recent(snapshot.get("recent") or [], limit)
        self.refresh_notices()
        self.refresh_messages()

    def _fill_transfers(self, transfers: list[dict]) -> None:
        yview = self.transfers.yview()
        self.transfers.delete(*self.transfers.get_children())
        self._transfer_index.clear()
        if not transfers:
            self.transfers.insert("", "end", values=("No active downloads", "", "", "", ""))
            return
        for index, item in enumerate(transfers):
            state = item.get("state") or ""
            tag = "ok" if state == "COMPLETE" else ("bad" if state in {"FAILED", "ABORTED"} else "")
            action = "" if state in DONE_TRANSFER else "Cancel"
            iid = self.transfers.insert(
                "",
                "end",
                values=(
                    item.get("filename") or "file",
                    item.get("username") or "",
                    state,
                    format_progress(item.get("percent")),
                    action,
                ),
                tags=(tag,) if tag else (),
            )
            if action:
                self._transfer_index[iid] = index
        if yview:
            self.transfers.yview_moveto(yview[0])

    def _fill_recent(self, recent: list[dict], limit: int) -> None:
        yview = self.recent.yview()
        self.recent.delete(*self.recent.get_children())
        rows = list(recent)[:limit]
        if not rows:
            self.recent.insert("", "end", values=("", "Nothing finished yet this install", "", ""))
            return
        for item in rows:
            who = " — ".join(part for part in ((item.get("artist") or ""), (item.get("title") or "")) if part)
            self.recent.insert(
                "",
                "end",
                values=(
                    format_when(item.get("finished_at")),
                    who,
                    item.get("filename") or "",
                    item.get("username") or "",
                ),
            )
        if yview:
            self.recent.yview_moveto(yview[0])

    def on_view(self) -> None:
        self._wanted_offset = 0
        self.refresh_wanted()

    def on_search(self) -> None:
        self._wanted_offset = 0
        self.refresh_wanted()

    def _view_kinds(self) -> tuple[str, ...] | None:
        view = self.view_var.get() or VIEW_EVERYTHING
        if view == VIEW_ARTISTS:
            return ("artist",)
        if view == VIEW_ALBUMS:
            return ("album",)
        if view == VIEW_SONGS:
            return ("song",)
        return None

    def on_sync(self) -> None:
        if self._syncing:
            self._sync_cancel.set()
            self.wanted_status.set("Cancelling sync…")
            return
        self._syncing = True
        self._sync_cancel = threading.Event()
        self.sync_button.configure(text="Cancel")
        self.wanted_status.set("Loading albums from Lidarr…")
        cfg = self.worker.cfg
        cancel = self._sync_cancel
        last_paint = 0.0

        def progress(rows: int, albums: int) -> None:
            nonlocal last_paint
            now = time.monotonic()
            if now - last_paint < 0.15:
                return
            last_paint = now
            text = f"Syncing {rows:,} rows ({albums:,} albums)…"
            self._post(lambda text=text: self._on_sync_progress(text))

        def work() -> None:
            client = LidarrClient(
                cfg.lidarr_url,
                cfg.lidarr_api_key,
                track_concurrency=cfg.lidarr_track_concurrency,
            )
            try:
                count, exhausted, cancelled = sync_wanted_catalog(
                    client,
                    self.worker.store,
                    max_pages=WANTED_SYNC_PAGES,
                    on_progress=progress,
                    should_cancel=cancel.is_set,
                )
                self._post(lambda: self._sync_finished_db(None, count, exhausted, cancelled))
            except Exception as exc:
                self._post(lambda exc=exc: self._sync_finished_db(exc, 0, False, False))
            finally:
                client.close()

        threading.Thread(target=work, name="wanted-sync", daemon=True).start()

    def _on_sync_progress(self, text: str) -> None:
        self.wanted_status.set(text)
        if self.worker.store.wanted_catalog_ready():
            self._refresh_wanted_sql(counts=False)

    def _post(self, func) -> None:
        try:
            self.after(0, func)
        except tk.TclError:
            pass

    def _sync_finished_db(
        self,
        error: Exception | None,
        count: int,
        exhausted: bool,
        cancelled: bool,
    ) -> None:
        self._syncing = False
        try:
            self.sync_button.configure(text="Sync")
        except tk.TclError:
            return
        self._choices_cache = None
        self._wanted_offset = 0
        self.refresh_wanted()
        if cancelled:
            self.wanted_status.set("Sync cancelled. Rows already saved stay available.")
            return
        if error is not None:
            self.wanted_status.set(f"Sync failed: {error}")
            return
        more = "" if exhausted else " Sync again to continue."
        self.wanted_status.set(f"Synced {count:,} rows from Lidarr.{more}")

    def on_select_all(self) -> None:
        if self._updating_select_all:
            return
        if self.worker.store.wanted_catalog_ready():
            self.worker.store.set_view_enabled(self._view_kinds(), self.search_var.get(), bool(self.select_all_var.get()))
            self.refresh_wanted()
            return
        items = self.worker.catalog.items()
        visible = self._ordered_rows(items)
        enabled = bool(self.select_all_var.get())
        self._apply_choices(select_all_updates(visible, items, enabled))

    def checkbox_state(self, iid: str) -> str:
        return self._row_checks.get(iid, "on")

    def on_wanted_click(self, event) -> None:
        # identify_region needs both coordinates. Passing only y raises
        # TypeError, and Tk then drops the click, so the box never toggles.
        region = self.wanted.identify_region(event.x, event.y)
        if region not in {"cell", "tree"}:
            return
        row = self.wanted.identify_row(event.y)
        if not row or row not in self._row_items:
            return
        column = str(self.wanted.identify_column(event.x))
        if column and self.wanted.column(column, "id") == "find":
            self.wanted.selection_set(row)
            self.wanted.focus(row)
            self.search_wanted_row(row)
            return "break"
        if region != "tree" and column != "#0":
            return
        self.wanted.selection_set(row)
        self.wanted.focus(row)
        self.toggle_row(row)
        return "break"

    def on_wanted_space(self, _event) -> None:
        for iid in self.wanted.selection():
            self.toggle_row(iid)
        return "break"

    def toggle_row(self, iid: str) -> None:
        item = self._row_items.get(iid)
        if item is None or not item.key:
            return
        if self.worker.store.wanted_catalog_ready():
            enabled = self._shown.get(item.key, "on") != "on"
            if item.kind == "artist":
                self.worker.store.set_artist_enabled(artist_token(item.artist), enabled)
            else:
                self.worker.store.set_wanted_choices({item.key: enabled})
            self.refresh_wanted()
            return
        items = self.worker.catalog.items()
        grouped = children_by_artist(items)
        enabled = not item_checked(item, items, self._choices(), grouped)
        self._apply_choices(choice_updates(item, enabled, items, grouped))

    def _choices(self) -> dict[str, bool]:
        if self._choices_cache is None:
            self._choices_cache = self.worker.store.wanted_choices()
        return self._choices_cache

    def _apply_choices(self, updates: dict[str, bool]) -> None:
        if not updates:
            self.refresh_wanted()
            return
        self._choices().update(updates)
        if len(updates) <= _SYNC_CHOICE_LIMIT:
            self.worker.store.set_wanted_choices(updates)
            self.refresh_wanted()
            return
        self._choice_job += 1
        token = self._choice_job
        self.wanted_status.set(f"Saving {len(updates):,} selections…")
        self.refresh_wanted()

        def work() -> None:
            error = None
            try:
                self.worker.store.set_wanted_choices(updates)
            except Exception as exc:
                error = exc
            self._post(lambda: self._choices_saved(token, error))

        threading.Thread(target=work, name="wanted-choices", daemon=True).start()

    def _choices_saved(self, token: int, error: Exception | None) -> None:
        if token != self._choice_job:
            return
        if error is not None:
            self._choices_cache = None
            self.wanted_status.set(f"Could not save selections: {error}")
        self.refresh_wanted()

    def _ordered_rows(self, items: list[WantedItem] | None = None) -> list[WantedItem]:
        if items is None:
            items = self.worker.catalog.items()
        visible = filter_view(items, self.view_var.get() or VIEW_EVERYTHING)
        folded = normalize(self.search_var.get() or "")
        if folded:
            visible = [item for item in visible if folded in normalize(item.artist or "")]
        if not self._wanted_sort:
            from search_order import order_wanted_items

            return order_wanted_items(visible, self.worker.cfg.search_order, self.worker.cfg.search_order_direction)
        if self._wanted_sort:
            column, reverse = self._wanted_sort
            visible = sort_items(visible, column, reverse)
        return visible

    def on_wanted_sort(self, column: str) -> None:
        if self._wanted_sort and self._wanted_sort[0] == column:
            self._wanted_sort = (column, not self._wanted_sort[1])
        else:
            self._wanted_sort = (column, False)
        self._wanted_offset = 0
        self.refresh_wanted()

    def _tree_row_px(self) -> int:
        style = ttk.Style(self)
        raw = style.lookup("Treeview", "rowheight")
        try:
            return max(1, int(float(str(raw))))
        except (TypeError, ValueError):
            return max(18, int(round(22 * self.ui_scale)))

    def _rows_for_height(self, pixel_height: int, *, heading: int | None = None, row: int | None = None) -> int:
        """How many rows fit in a list of this pixel height, including a partial last row."""
        row_px = row or self._tree_row_px()
        head = row_px if heading is None else max(0, heading)
        body = max(row_px, int(pixel_height) - head)
        return max(1, (body + row_px - 1) // row_px)

    def _fit_tree(self, tree: ttk.Treeview | None) -> None:
        """Give a table enough rows to fill its parent, in either theme."""
        if tree is None or getattr(tree, "_fitting", False):
            return
        tree._fitting = True
        try:
            if not tree.winfo_exists():
                return
            available = tree.master.winfo_height()
            if available < 40:
                return
            rows = self._rows_for_height(available)
            if int(tree.cget("height")) != rows:
                tree.configure(height=rows)
        except (tk.TclError, ValueError):
            return
        finally:
            tree._fitting = False

    def _bind_filling_tree(self, tree: ttk.Treeview) -> None:
        def _on_configure(_event, current=tree) -> None:
            self._fit_tree(current)

        tree.master.bind("<Configure>", _on_configure, add="+")
        tree.bind("<Configure>", _on_configure, add="+")

    def _wanted_resized(self, event) -> None:
        self._fit_tree(self.wanted)
        row_px = self._tree_row_px()
        heading = row_px
        try:
            children = self.wanted.get_children()
            if children:
                box = self.wanted.bbox(children[0])
                if box and box[1] > 0:
                    heading = box[1]
                    if box[3] > 0:
                        row_px = box[3]
        except tk.TclError:
            pass
        window = max(WANTED_WINDOW_MIN, self._rows_for_height(int(event.height), heading=heading, row=row_px))
        if window == self._wanted_window:
            return
        self._wanted_window = window
        if self._sql_mode:
            self._refresh_wanted_sql(counts=False)
        elif self._wanted_rows:
            self._paint_wanted(self._choices(), self._wanted_grouped)

    def _wanted_span(self) -> int:
        if self._sql_mode:
            return self._wanted_total
        return len(self._wanted_rows)

    def _wanted_scroll(self, *args) -> None:
        total = self._wanted_span()
        window = max(1, self._wanted_window)
        if total <= window:
            return
        after = None
        before = None
        before_count = 0
        keyed = False
        if args[0] == "moveto":
            self._wanted_offset = int(float(args[1]) * total)
        elif args[0] == "scroll":
            step = int(args[1])
            if len(args) > 2 and args[2] == "pages":
                step *= window
            reverse = bool(self._wanted_sort and self._wanted_sort[1])
            cursors = self._cursors
            if self._sql_mode and not reverse and cursors and step > 0 and step <= len(cursors):
                after = cursors[step - 1]
                keyed = True
            elif (
                self._sql_mode
                and not reverse
                and cursors
                and step < 0
                and abs(step) <= len(cursors)
                and self._wanted_offset > 0
            ):
                before = cursors[0]
                before_count = abs(step)
                keyed = True
            self._wanted_offset += step
        self._wanted_offset = max(0, min(self._wanted_offset, total - window))
        if self._sql_mode:
            self._refresh_wanted_sql(
                counts=False,
                after=after if keyed else None,
                before=before if keyed else None,
                before_count=before_count if keyed else 0,
            )
        else:
            self._paint_wanted(self._choices(), self._wanted_grouped)

    def _wanted_wheel(self, event):
        if getattr(event, "num", None) == 5 or getattr(event, "delta", 0) < 0:
            self._wanted_scroll("scroll", 3, "units")
        else:
            self._wanted_scroll("scroll", -3, "units")
        return "break"

    def _wanted_key(self, event):
        children = self.wanted.get_children()
        selected = self.wanted.selection()
        if not children or not selected or selected[0] not in children:
            return
        index = children.index(selected[0])
        if event.keysym == "Down" and index == len(children) - 1 and self._wanted_offset + len(children) < self._wanted_span():
            self._wanted_scroll("scroll", 1, "units")
            last = self.wanted.get_children()
            if last:
                self.wanted.selection_set(last[-1])
                self.wanted.focus(last[-1])
            return "break"
        if event.keysym == "Up" and index == 0 and self._wanted_offset > 0:
            self._wanted_scroll("scroll", -1, "units")
            first = self.wanted.get_children()
            if first:
                self.wanted.selection_set(first[0])
                self.wanted.focus(first[0])
            return "break"

    def _refresh_wanted_sql(
        self,
        *,
        counts: bool = True,
        after: tuple | None = None,
        before: tuple | None = None,
        before_count: int = 0,
    ) -> None:
        self._sql_mode = True
        kinds = self._view_kinds()
        search = self.search_var.get()
        order = "search"
        reverse = False
        if self._wanted_sort:
            order, reverse = self._wanted_sort
        limit = max(1, self._wanted_window)
        keyed = after is not None or before is not None
        page, total = self.worker.store.wanted_page(
            kinds=kinds,
            search=search,
            order=order,
            reverse=reverse,
            limit=limit,
            offset=self._wanted_offset,
            after=after,
            before=before,
            before_count=before_count,
        )
        if not keyed:
            if total <= limit:
                self._wanted_offset = 0
            elif self._wanted_offset > total - limit:
                self._wanted_offset = max(0, total - limit)
                page, total = self.worker.store.wanted_page(
                    kinds=kinds,
                    search=search,
                    order=order,
                    reverse=reverse,
                    limit=limit,
                    offset=self._wanted_offset,
                )
        elif total <= limit:
            self._wanted_offset = 0
        else:
            self._wanted_offset = max(0, min(self._wanted_offset, max(0, total - max(1, len(page)))))
        self._wanted_total = total
        items: list[WantedItem] = []
        self._shown = {}
        self._cursors = []
        for key, kind, artist, album, song, detail, shown, cursor in page:
            items.append(WantedItem(kind=kind, key=key, artist=artist, album=album, song=song, detail=detail))
            self._shown[key] = shown if shown in {"on", "off", "mixed"} else ("on" if shown else "off")
            self._cursors.append(cursor)
        self._fill_tree(items, total)
        if not counts or self._syncing:
            return
        view_total, checked, state = self.worker.store.wanted_summary(kinds, search)
        self._updating_select_all = True
        try:
            self.select_all_var.set(1 if state == "all" and view_total else 0)
        finally:
            self._updating_select_all = False
        if not str(self.wanted_status.get()).startswith("Saving "):
            self.wanted_status.set(f"{checked:,} of {view_total:,} selected in this view.")

    def _paint_activity_dot(self, busy: bool) -> None:
        if not getattr(self, "activity_dot", None):
            return
        palette = active_palette(self)
        size = self.px(14)
        pad = max(1, self.px(2))
        self.activity_dot.configure(width=size, height=size, background=palette.card)
        self.activity_dot.delete("all")
        if busy:
            self._pulse_on = not self._pulse_on
            color = palette.dot_busy if self._pulse_on else palette.accent
        else:
            self._pulse_on = False
            color = palette.dot_idle
        self.activity_dot.create_oval(pad, pad, size - pad, size - pad, fill=color, outline=color)

    def _magnifier_image(self) -> tk.PhotoImage:
        """A drawn glass, so the Wanted column does not depend on a Unicode font."""
        palette = active_palette(self)
        image = tk.PhotoImage(master=self, width=16, height=16)
        image.put(palette.card, to=(0, 0, 16, 16))
        glass = palette.glass
        for y in range(16):
            for x in range(16):
                distance = (x - 6) ** 2 + (y - 6) ** 2
                if 10 <= distance <= 18:
                    image.put(glass, to=(x, y, x + 1, y + 1))
        for offset in range(5):
            image.put(glass, to=(9 + offset, 9 + offset, 11 + offset, 11 + offset))
        return self._zoom_glyph(image)

    def _place_finders(self) -> None:
        """One magnifier per visible row. The catalog itself is not walked."""
        if not getattr(self, "wanted", None):
            return
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        rows = [iid for iid in self.wanted.get_children() if iid in self._row_items]
        while len(self._finder_labels) < len(rows):
            label = tk.Label(
                self.wanted,
                image=self._finder_image,
                bg=active_palette(self).card,
                bd=0,
                cursor="hand2",
                takefocus=0,
            )
            self._finder_labels.append(label)
        for label in self._finder_labels[len(rows) :]:
            label.place_forget()
        for label, iid in zip(self._finder_labels, rows):
            box = self.wanted.bbox(iid, "find")
            if not box:
                label.place_forget()
                continue
            x, y, width, height = box
            size = self._glyph_size
            label.place(x=x + max(0, (width - size) // 2), y=y + max(0, (height - size) // 2), width=size, height=size)
            label.bind("<Button-1>", lambda _event, row=iid: self._finder_clicked(row))

    def _finder_clicked(self, iid: str) -> None:
        self.wanted.selection_set(iid)
        self.wanted.focus(iid)
        self.search_wanted_row(iid)

    def search_wanted_row(self, iid: str) -> None:
        item = self._row_items.get(iid)
        if item is None or not item.key:
            return
        query = manual_query(item.kind, item.artist, item.album, item.song)
        self.open_manual_search(query, link_from_wanted(item))

    def _build_browse(self, parent: ttk.Frame) -> None:
        self._page_header(
            parent,
            "Browse",
            "Look at one Soulseek user's files. The list is saved on this PC and opened one folder at a time. A folder that names a missing Lidarr album is marked Wanted.",
        )
        row = ttk.Frame(parent, style="Main.TFrame")
        row.pack(fill="x", pady=(self.px(14), 0))
        self.browse_user_var = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.browse_user_var)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda _event: self.load_browse(False))
        ttk.Button(row, text="Browse", command=lambda: self.load_browse(False)).pack(side="left", padx=(self.px(8), 0))
        ttk.Button(row, text="Refresh", command=lambda: self.load_browse(True)).pack(side="left", padx=(self.px(8), 0))
        ttk.Button(row, text="Download all missing", command=self.download_browse_missing).pack(side="left", padx=(self.px(8), 0))
        self.browse_status = tk.StringVar(value="Enter a username. Start the worker so Browse can ask Soulseek.")
        ttk.Label(parent, textvariable=self.browse_status, style="Muted.TLabel", wraplength=self.px(900)).pack(
            anchor="w", pady=(self.px(6), 0)
        )
        self.browse_profile = tk.Text(parent, wrap="word", height=6, bd=0, highlightthickness=0)
        self.browse_profile.pack(fill="x", pady=(self.px(8), 0))
        self.browse_profile.configure(state="disabled")
        card = self._card(parent, padding=(self.px(8), self.px(8), self.px(8), self.px(8)))
        card.pack(fill="both", expand=True, pady=(self.px(8), 0))
        self.browse_tree = ttk.Treeview(card.inner, columns=("size", "mark"), show="tree headings", selectmode="browse")
        self.browse_tree.heading("#0", text="Folder")
        self.browse_tree.heading("size", text="Size")
        self.browse_tree.heading("mark", text="Wanted")
        self.browse_tree.column("#0", width=self.px(420), stretch=True)
        self.browse_tree.column("size", width=self.px(90), stretch=False, anchor="e")
        self.browse_tree.column("mark", width=self.px(80), stretch=False)
        self.browse_tree.pack(fill="both", expand=True)
        self.browse_tree.bind("<<TreeviewOpen>>", self._browse_open)
        self._browse_index = ShareIndex()
        self._browse_needles: list[tuple[str, str]] = []
        self._browse_paths: dict[str, str] = {}
        self._browse_opened: set[str] = set()

    def _browse_cache(self) -> BrowseCache:
        cache = getattr(self, "_browse_cache_db", None)
        if cache is None:
            db = Path(self.worker.cfg.state_db)
            cache = BrowseCache(db.with_name(db.stem + "-browse.sqlite3"))
            self._browse_cache_db = cache
        return cache

    def load_browse(self, refresh: bool = False) -> None:
        username = self.browse_user_var.get().strip()
        if not username:
            self.browse_status.set("Enter a Soulseek username.")
            return
        cache = self._browse_cache()
        if not refresh:
            saved = cache.get(username)
            if saved is not None:
                self._show_browse(saved, cached=True)
                return
        loop = getattr(self.worker, "_loop", None)
        if loop is None or not loop.is_running():
            self.browse_status.set("Start the worker to browse. Browse uses that Soulseek login.")
            return
        self.browse_status.set(f"Browsing {username}…")
        future = asyncio.run_coroutine_threadsafe(self.worker.browse_user(username), loop)

        def done() -> None:
            try:
                payload = future.result(timeout=120)
            except Exception as exc:
                text = str(exc).strip() or "Browse failed."
                self._post(lambda text=text: self.browse_status.set(text))
                return

            def apply() -> None:
                saved = cache.put(payload["username"], payload["profile"], payload["directories"])
                self._show_browse(saved, cached=False)

            self._post(apply)

        threading.Thread(target=done, name="browse-user", daemon=True).start()

    def _show_browse(self, payload: dict, *, cached: bool) -> None:
        username = str(payload.get("username") or self.browse_user_var.get().strip())
        self.browse_profile.configure(state="normal")
        self.browse_profile.delete("1.0", "end")
        self.browse_profile.insert("1.0", profile_text(username, payload.get("profile") or {}))
        self.browse_profile.configure(state="disabled")
        self._browse_payload = payload
        self._browse_index = ShareIndex(payload.get("directories") or [])
        try:
            albums = self.worker.store.wanted_albums()
        except Exception:
            albums = []
        self._browse_needles = missing_needles(albums)
        self._browse_paths = {}
        self._browse_opened = set()
        tree = self.browse_tree
        tree.delete(*tree.get_children())
        tree.tag_configure("wanted", foreground=active_palette(self).ok)
        self._insert_browse_children("", "")
        when = str(payload.get("fetched_at") or "")
        if cached:
            self.browse_status.set(f"Saved copy from {when}. Refresh asks Soulseek again.")
        else:
            self.browse_status.set(f"Browsed {username}. Saved {when}.")

    def refresh_notices(self) -> None:
        frame = getattr(self, "notices", None)
        if frame is None:
            return
        enabled = bool(getattr(self.worker.cfg, "suggest_favorites", True))
        try:
            if enabled:
                self.worker.store.suggest_from_history(set(self.worker.cfg.priority_users))
            events = self.worker.store.recent_events(8, exclude_kind="favorite")
            favorites = self.worker.store.unseen_events("favorite") if enabled else []
        except Exception:
            log.exception("Could not load notices")
            return
        self._sync_favorite_prompt(favorites)
        signature = tuple(
            (event["id"], event["kind"], event["title"], event["body"], event["username"]) for event in events
        )
        if signature == getattr(self, "_notice_signature", None):
            return
        self._notice_signature = signature
        for child in list(frame.winfo_children()):
            child.destroy()
        for event in events:
            row = ttk.Frame(frame, style="Main.TFrame")
            row.pack(fill="x", pady=(0, self.px(4)))
            text = str(event.get("title") or "")
            body = str(event.get("body") or "").strip()
            if body and body != text:
                text = f"{text} {body}"
            ttk.Label(
                row,
                text=text,
                style="Muted.TLabel",
                wraplength=self.px(820),
                justify="left",
            ).pack(side="left", fill="x", expand=True)
            event_id = int(event["id"])
            ttk.Button(
                row,
                text="Dismiss",
                command=lambda event_id=event_id: self._dismiss_notice(event_id),
            ).pack(side="right")

    def _sync_favorite_prompt(self, events: list[dict]) -> None:
        """One prompt lists everyone waiting. A closed prompt stays closed until that list changes."""
        waiting = [event for event in events if str(event.get("username") or "").strip()]
        key = tuple(str(event.get("username") or "") for event in waiting)
        if not key:
            self._favorite_prompt_key = None
            self._close_favorite_prompt()
            return
        prompt = getattr(self, "_favorite_prompt", None)
        alive = False
        if prompt is not None:
            try:
                alive = bool(prompt.winfo_exists())
            except tk.TclError:
                alive = False
        if not alive:
            if key == getattr(self, "_favorite_prompt_key", None):
                return
            self._favorite_prompt = FavoritePrompt(
                self,
                self.add_suggested_favorite,
                self.dismiss_suggested_favorite,
                self.stop_favorite_suggestions,
                self._remember_closed_favorite_prompt,
            )
        self._favorite_prompt_key = None
        self._favorite_prompt.show(waiting)

    def _close_favorite_prompt(self) -> None:
        prompt = getattr(self, "_favorite_prompt", None)
        self._favorite_prompt = None
        if prompt is None:
            return
        try:
            if prompt.winfo_exists():
                prompt.destroy()
        except tk.TclError:
            pass

    def _remember_closed_favorite_prompt(self, names: tuple[str, ...]) -> None:
        self._favorite_prompt = None
        self._favorite_prompt_key = names

    def stop_favorite_suggestions(self) -> None:
        """Don't ask again. Later saves keep the checkbox off."""
        cfg = replace(self.worker.cfg, suggest_favorites=False)
        self.worker.cfg = cfg
        try:
            save_config(self.config_path, cfg)
        except OSError:
            log.exception("Could not save favorite suggestions")
        form = getattr(self, "_settings_form", None)
        if form is not None and "suggest_favorites" in form.fields:
            form.fields["suggest_favorites"].set(False)
            form.base = cfg
        if self._settings_baseline is not None:
            self._settings_baseline["cfg"] = replace(self._settings_baseline["cfg"], suggest_favorites=False)
        self._favorite_prompt_key = None
        self._close_favorite_prompt()

    def _dismiss_notice(self, event_id: int) -> None:
        self.worker.store.dismiss_event(event_id)
        self.refresh_notices()

    def add_suggested_favorite(self, username: str) -> None:
        """Priority users is the favorite list. Add is the only way in."""
        name = (username or "").strip().lower()
        if not name:
            return
        names = {item.strip().lower() for item in self.worker.cfg.priority_users if str(item).strip()}
        names.add(name)
        cfg = replace(self.worker.cfg, priority_users=names)
        self.worker.cfg = cfg
        save_config(self.config_path, cfg)
        form = getattr(self, "_settings_form", None)
        if form is not None and "priority_users" in form.fields:
            form.fields["priority_users"].set(", ".join(sorted(names)))
            form.base = cfg
        if self._settings_baseline is not None:
            self._settings_baseline["cfg"] = replace(self._settings_baseline["cfg"], priority_users=set(names))
        self.worker.store.dismiss_user_events(name, "favorite")
        self.refresh_notices()

    def dismiss_suggested_favorite(self, username: str) -> None:
        self.worker.store.dismiss_favorite(username)
        self.refresh_notices()

    def _saved_browse_caps(self) -> tuple[int, int, int]:
        cfg = self.worker.cfg
        return (
            max(1, int(getattr(cfg, "browse_max_files", 25) or 25)),
            max(1, int(getattr(cfg, "browse_max_mb", 2048) or 2048)),
            max(1, int(getattr(cfg, "browse_daily_files", 20) or 20)),
        )

    def _typed_cap(self, variable: tk.Variable, fallback: int) -> int:
        try:
            value = int(str(variable.get()).strip())
        except (TypeError, ValueError):
            return max(1, int(fallback))
        return max(1, value)

    def _commit_browse_limits(self) -> tuple[int, int, int]:
        saved = self._saved_browse_caps()
        files = self._typed_cap(self._limits_files, saved[0])
        mb = self._typed_cap(self._limits_mb, saved[1])
        daily = self._typed_cap(self._limits_daily, saved[2])
        cfg = replace(
            self.worker.cfg,
            browse_max_files=files,
            browse_max_mb=mb,
            browse_daily_files=daily,
        )
        self.worker.cfg = cfg
        save_config(self.config_path, cfg)
        form = getattr(self, "_settings_form", None)
        if form is not None:
            form.base = cfg
        if self._settings_baseline is not None:
            self._settings_baseline["cfg"] = replace(
                self._settings_baseline["cfg"],
                browse_max_files=files,
                browse_max_mb=mb,
                browse_daily_files=daily,
            )
        return files, mb, daily

    def _browse_download_plan(self, our_files: int, our_mb: int, daily: int):
        payload = getattr(self, "_browse_payload", None) or {}
        username = self.browse_user_var.get().strip()
        profile = payload.get("profile") or {}
        description = str(profile.get("description") or "")
        try:
            permission = int(profile.get("upload_permissions", -1))
        except (TypeError, ValueError):
            permission = -1
        try:
            queue_length = int(profile.get("queue_length") or 0)
        except (TypeError, ValueError):
            queue_length = 0
        limits = apply_limits(
            description,
            permission=permission,
            queue_length=queue_length,
            our_files=max(1, int(our_files)),
            our_mb=max(1, int(our_mb)),
        )
        daily_cap = max(1, int(daily))
        used = self.worker.store.user_day_files(username)
        room = daily_room(used, daily_cap)
        lines = list(limits.lines)
        lines.append(f"Used today: {used} of {daily_cap}.")
        rows = share_file_rows(username, payload.get("directories") or [])
        matches = self.worker.match_user_files(rows) if rows else []
        chosen: list = []
        skipped = 0
        if limits.allowed and room <= 0:
            lines.append(f"The daily cap for {username} is already used.")
            skipped = len(matches)
        elif limits.allowed:
            file_cap = min(limits.max_files, room)
            chosen, skipped = select_under_cap(
                matches,
                max_files=file_cap,
                max_bytes=limits.max_mb * 1024 * 1024,
            )
            if file_cap < limits.max_files:
                lines.append(f"{file_cap} files left today for {username}.")
            lines.append("")
            lines.append(f"{len(chosen)} missing file(s) will be queued from {username}.")
            if skipped:
                lines.append(f"{skipped} more match, and stay out because of the cap.")
            if not chosen:
                lines.append("Nothing in this share matches the wanted list.")
        return limits, chosen, lines

    def _set_limits_text(self, lines: list[str]) -> None:
        box = self._limits_text
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", "\n".join(lines))
        box.configure(state="disabled")

    def _set_download_button(self, show: bool) -> None:
        current = getattr(self, "_limits_download_btn", None)
        if current is not None:
            current.destroy()
            self._limits_download_btn = None
        if not show:
            return
        button = ttk.Button(self._limits_download_slot, text="Download", command=self._download_from_limits)
        button.pack(side="left")
        self._limits_download_btn = button

    def _save_limits_from_window(self) -> None:
        files, mb, daily = self._commit_browse_limits()
        _limits, chosen, lines = self._browse_download_plan(files, mb, daily)
        self._set_limits_text(lines)
        allowed = _limits.allowed and bool(chosen)
        self._set_download_button(allowed)

    def _download_from_limits(self) -> None:
        files, mb, daily = self._commit_browse_limits()
        limits, chosen, lines = self._browse_download_plan(files, mb, daily)
        if not limits.allowed or not chosen:
            self._set_limits_text(lines)
            self._set_download_button(False)
            return
        window = getattr(self, "_limits_window", None)
        if window is not None:
            window.destroy()
        self._start_browse_download(chosen, limits)

    def download_browse_missing(self) -> None:
        payload = getattr(self, "_browse_payload", None)
        username = self.browse_user_var.get().strip()
        if not payload or not username:
            self.browse_status.set("Browse a user first.")
            return
        self._show_limits(username)

    def _show_limits(self, username: str) -> None:
        existing = getattr(self, "_limits_window", None)
        if existing is not None:
            try:
                existing.destroy()
            except tk.TclError:
                pass
        files, mb, daily = self._saved_browse_caps()
        self._limits_files = tk.StringVar(value=str(files))
        self._limits_mb = tk.StringVar(value=str(mb))
        self._limits_daily = tk.StringVar(value=str(daily))
        limits, chosen, lines = self._browse_download_plan(files, mb, daily)
        window = tk.Toplevel(self)
        window.title("Download limits")
        window.transient(self)
        window.geometry(f"{self.px(560)}x{self.px(560)}")
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=username, style="PageTitle.TLabel").pack(anchor="w")
        box = tk.Text(frame, wrap="word", height=12, bd=0, highlightthickness=0)
        box.pack(fill="both", expand=True, pady=(8, 8))
        self._limits_text = box
        self._set_limits_text(lines)
        form = ttk.Frame(frame)
        form.pack(fill="x", pady=(0, 8))
        ttk.Label(
            form,
            text="Saved on this PC. A stricter number in their profile still wins.",
            style="Muted.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        fields = (
            (1, "Max files", self._limits_files),
            (2, "Total size (MB)", self._limits_mb),
            (3, "Files per user per day", self._limits_daily),
        )
        for row, label, variable in fields:
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(form, textvariable=variable, width=10).grid(row=row, column=1, sticky="w", pady=2)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        self._limits_buttons = buttons
        self._limits_download_slot = ttk.Frame(buttons)
        self._limits_download_slot.pack(side="left")
        self._limits_download_btn = None
        ttk.Button(buttons, text="Save limits", command=self._save_limits_from_window).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Close", command=window.destroy).pack(side="right")
        self._set_download_button(limits.allowed and bool(chosen))
        self._limits_window = window

    def _start_browse_download(self, chosen: list, limits) -> None:
        loop = getattr(self.worker, "_loop", None)
        if loop is None or not loop.is_running():
            self.browse_status.set("Start the worker to download. Downloads use that Soulseek login.")
            return
        specs = []
        for match in chosen:
            specs.append(
                {
                    "id": f"{match.username}\n{match.filename}",
                    "username": match.username,
                    "filename": match.filename,
                    "size": match.size,
                    "link": match.link(),
                }
            )
        self.browse_status.set(
            f"Downloading {len(specs)} file(s), {limits.files_at_once} at a time, {limits.pace_seconds}s apart."
        )
        future = asyncio.run_coroutine_threadsafe(
            self.worker.download_paced(
                specs,
                pace_seconds=limits.pace_seconds,
                files_at_once=limits.files_at_once,
            ),
            loop,
        )

        def done() -> None:
            try:
                text = future.result(timeout=max(60, limits.pace_seconds * max(1, len(specs)) + 120))
            except Exception as exc:
                text = str(exc).strip() or "Download failed."

            def apply(text=text) -> None:
                self.browse_status.set(text)
                self.refresh_notices()

            self._post(apply)

        threading.Thread(target=done, name="browse-missing", daemon=True).start()

    def _insert_browse_children(self, parent: str, path: str) -> None:
        if path in self._browse_opened:
            return
        self._browse_opened.add(path)
        tree = self.browse_tree
        if parent:
            for child in tree.get_children(parent):
                if not tree.item(child, "text"):
                    tree.delete(child)
        for row in self._browse_index.children(path):
            label = str(row.get("name") or "")
            blob = f"{row.get('path') or ''} {label}"
            wanted = is_wanted(blob, self._browse_needles)
            tags = ("wanted",) if wanted else ()
            mark = "Wanted" if wanted else ""
            if row.get("kind") == "dir":
                if row.get("locked"):
                    label = f"{label} (locked)"
                iid = tree.insert(parent, "end", text=label, values=("", mark), tags=tags)
                self._browse_paths[iid] = str(row.get("path") or "")
                tree.insert(iid, "end", text="")
            else:
                tree.insert(parent, "end", text=label, values=(self._size_text(row.get("size")), mark), tags=tags)

    def _browse_open(self, _event=None) -> None:
        tree = self.browse_tree
        iid = tree.focus()
        path = self._browse_paths.get(iid)
        if not path:
            return
        self._insert_browse_children(iid, path)

    def browse_selected_user(self) -> None:
        board = self._current_board()
        username = self._selected_username(board) if board is not None else ""
        if not username:
            if board is not None and board.status is not None:
                board.status.set("Select a file to browse that user.")
            return
        self.browse_user_var.set(username)
        self.notebook.select(self.browse_tab)
        self.load_browse(False)

    @staticmethod
    def _size_text(size) -> str:
        try:
            number = float(size or 0)
        except (TypeError, ValueError):
            return ""
        for unit in ("B", "KB", "MB", "GB"):
            if number < 1024 or unit == "GB":
                if unit == "B":
                    return f"{int(number)} B"
                return f"{number:.1f} {unit}"
            number /= 1024
        return ""

    def _build_search(self, parent: ttk.Frame) -> None:
        self._page_header(
            parent,
            "Search",
            "Uses the worker's Soulseek login. Pause stops the worker's own searches and leaves this page running. Download all wanted uses every result unless a user is selected.",
        )
        row = ttk.Frame(parent, style="Main.TFrame")
        row.pack(fill="x", pady=(self.px(14), 0))
        self.query_var = tk.StringVar()
        self.query_entry = ttk.Entry(row, textvariable=self.query_var)
        self.query_entry.pack(side="left", fill="x", expand=True)
        self.query_entry.bind("<Return>", lambda _event: self.submit_search())
        self.search_button = ttk.Button(row, text="Search", style="Start.TButton", command=self.submit_search)
        self.search_button.pack(side="left", padx=(self.px(8), 0))
        card = self._card(parent, padding=(self.px(8), self.px(8), self.px(8), self.px(8)))
        card.pack(fill="both", expand=True, pady=(self.px(10), 0))
        self.searches = ttk.Notebook(card.inner)
        self.searches.pack(fill="both", expand=True)
        self.searches.bind("<Button-1>", self._on_search_tab_press)
        self._search_menu = tk.Menu(self, tearoff=0)
        self._theme_menus.append(self._search_menu)
        self._search_menu.add_command(label="Download file(s)", command=self.download_selected_files)
        self._search_menu.add_command(label="Download folder", command=self.download_selected_folder)
        self._search_menu.add_command(label="Download all from this user", command=self.download_all_from_user)
        self._search_menu.add_command(label="Download all wanted from this user", command=self.download_all_wanted)
        self._search_menu.add_command(label="Browse folder", command=self.browse_selected_folder)
        self._search_menu.add_command(label="Browse this user", command=self.browse_selected_user)

    def submit_search(self) -> None:
        query = (self.query_var.get() or "").strip()
        if not query:
            return
        self.open_manual_search(query, link=None)

    def open_manual_search(self, query: str, link: SearchLink | None = None) -> SearchBoard:
        query = (query or "").strip()
        board = self._add_search_board(query, link)
        self.query_var.set(query)
        self.notebook.select(self.search_tab)
        if board.frame is not None:
            self.searches.select(board.frame)
        self._start_board_search(board)
        return board

    def _search_tab_title(self, board: SearchBoard) -> str:
        text = board.query or "Search"
        if len(text) > 22:
            text = text[:21] + "…"
        count = len(board.files)
        return f"{text} ({count})  ×"

    def _retitle(self, board: SearchBoard) -> None:
        if board.frame is None or board.closed:
            return
        try:
            self.searches.tab(board.frame, text=self._search_tab_title(board))
        except tk.TclError:
            pass

    def _add_search_board(self, query: str, link: SearchLink | None) -> SearchBoard:
        board = SearchBoard(query=query, link=link)
        frame = ttk.Frame(self.searches, padding=4)
        board.frame = frame
        filters = ttk.Frame(frame)
        filters.pack(fill="x", pady=(0, 4))
        board.filter_var = tk.StringVar()
        board.flac_var = tk.IntVar(value=0)
        board.mp3_var = tk.IntVar(value=0)
        board.bitrate_var = tk.StringVar()
        board.free_var = tk.IntVar(value=0)
        ttk.Label(filters, text="Filter").pack(side="left")
        filter_entry = ttk.Entry(filters, textvariable=board.filter_var, width=28)
        filter_entry.pack(side="left", padx=(6, 8))
        filter_entry.bind("<KeyRelease>", lambda _event, current=board: self._read_filters(current))
        ttk.Checkbutton(filters, text="FLAC", variable=board.flac_var, command=lambda current=board: self._read_filters(current)).pack(side="left")
        ttk.Checkbutton(filters, text="MP3", variable=board.mp3_var, command=lambda current=board: self._read_filters(current)).pack(side="left", padx=(6, 0))
        ttk.Label(filters, text="Min bitrate").pack(side="left", padx=(10, 4))
        bitrate = ttk.Entry(filters, textvariable=board.bitrate_var, width=6)
        bitrate.pack(side="left")
        bitrate.bind("<KeyRelease>", lambda _event, current=board: self._read_filters(current))
        ttk.Checkbutton(filters, text="Free slot", variable=board.free_var, command=lambda current=board: self._read_filters(current)).pack(side="left", padx=(8, 0))
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(0, 4))
        ttk.Button(actions, text="Download file(s)", command=self.download_selected_files).pack(side="left")
        ttk.Button(actions, text="Download folder", command=self.download_selected_folder).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Download all from this user", command=self.download_all_from_user).pack(side="left", padx=(8, 0))
        wanted_button = ttk.Button(actions, text="Download all wanted", command=self.download_all_wanted)
        wanted_button.pack(side="left", padx=(8, 0))
        _attach_tip(
            wanted_button,
            "With no user selected, this finds every wanted album and track for the searched artist "
            "across all results, keeps the best source for each, and queues them. "
            "Select a user, or one of their files, to limit it to that user.",
        )
        ttk.Button(actions, text="Browse folder", command=self.browse_selected_folder).pack(side="left", padx=(8, 0))
        board.status = tk.StringVar(value="")
        ttk.Label(actions, textvariable=board.status, style="Muted.TLabel").pack(side="left", padx=(12, 0))
        tree_frame = ttk.Frame(frame)
        tree_frame.pack(fill="both", expand=True)
        columns = ("size", "bitrate", "length", "format", "speed", "slot", "state")
        board.tree = ttk.Treeview(tree_frame, columns=columns, show="tree headings", selectmode="extended")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=board.tree.yview)
        board.tree.configure(yscrollcommand=scroll.set)
        board.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self._bind_filling_tree(board.tree)
        headings = (
            ("#0", "User / file", 360),
            ("size", "Size", 80),
            ("bitrate", "Bitrate", 70),
            ("length", "Length", 70),
            ("format", "Format", 70),
            ("speed", "Speed", 90),
            ("slot", "Slot", 90),
            ("state", "State", 110),
        )
        for name, heading, width in headings:
            column_id = "name" if name == "#0" else name
            board.tree.heading(name, text=heading, command=lambda column=column_id, current=board: self._sort_search(current, column))
            board.tree.column(name, width=width, minwidth=40, stretch=name == "#0", anchor="w")
        board.tree.tag_configure("user", font=(ui_font(), 10, "bold"))
        board.tree.tag_configure("downloaded", foreground="#7dce7d")
        board.tree.tag_configure("failed", foreground="#e07a7a")
        board.tree.bind("<Button-3>", self._search_right_click)
        board.tree.bind("<Double-Button-1>", lambda _event: self.download_selected_files())
        self.searches.add(frame, text=self._search_tab_title(board))
        self._boards.append(board)
        return board

    def _on_search_tab_press(self, event) -> str | None:
        try:
            index = self.searches.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return None
        box = self.searches.bbox(index)
        if not box:
            return None
        x, _y, width, _height = box
        if event.x < x + width - 18:
            return None
        self._close_search_index(index)
        return "break"

    def _close_search_index(self, index: int) -> None:
        try:
            frame_name = str(self.searches.tabs()[index])
        except (tk.TclError, IndexError):
            return
        board = next((item for item in self._boards if item.frame is not None and str(item.frame) == frame_name), None)
        if board is None or board.frame is None:
            return
        board.closed = True
        self.searches.forget(board.frame)
        self._boards = [item for item in self._boards if item is not board]

    def _current_board(self) -> SearchBoard | None:
        try:
            selected = str(self.searches.select())
        except tk.TclError:
            return None
        for board in self._boards:
            if board.frame is not None and str(board.frame) == selected and not board.closed:
                return board
        return None

    def _read_filters(self, board: SearchBoard) -> None:
        if board.closed:
            return
        try:
            bitrate = int((board.bitrate_var.get() if board.bitrate_var else "") or 0)
        except ValueError:
            bitrate = 0
        board.filters = SearchFilters(
            text=board.filter_var.get() if board.filter_var else "",
            flac=bool(board.flac_var.get()) if board.flac_var else False,
            mp3=bool(board.mp3_var.get()) if board.mp3_var else False,
            min_bitrate=max(0, bitrate),
            free_only=bool(board.free_var.get()) if board.free_var else False,
        )
        self._paint_search(board)

    def _sort_search(self, board: SearchBoard, column: str) -> None:
        if board.sort_column == column:
            board.sort_reverse = not board.sort_reverse
        else:
            board.sort_column = column
            board.sort_reverse = column not in {"name", "format", "state"}
        self._paint_search(board)

    def _paint_search(self, board: SearchBoard) -> None:
        tree = board.tree
        if tree is None or board.closed:
            return
        selected_files = {board.row_files[iid].id for iid in tree.selection() if iid in board.row_files}
        selected_users = {board.row_users[iid] for iid in tree.selection() if iid in board.row_users}
        tree.delete(*tree.get_children())
        board.row_files.clear()
        board.row_users.clear()
        visible = board.filters.apply(board.files)
        shown = visible[:2000]
        parent = ""
        for row in display_rows(shown, sort_column=board.sort_column, reverse=board.sort_reverse):
            if row.kind == "user":
                label = f"{row.username} ({row.file_count})"
                parent = tree.insert(
                    "",
                    "end",
                    text=label,
                    open=True,
                    values=("", "", "", "", format_speed(row.speed), format_slot(row.free_slot, row.queue), ""),
                    tags=("user",),
                )
                board.row_users[parent] = row.username
                if row.username in selected_users:
                    tree.selection_add(parent)
                continue
            item = row.file
            if item is None:
                continue
            tag = "downloaded" if item.state == "downloaded" else ("failed" if item.state == "failed" else "")
            iid = tree.insert(
                parent or "",
                "end",
                text=display_name(item),
                values=(
                    format_size(item.size),
                    f"{item.bitrate}" if item.bitrate else "",
                    format_duration(item.duration),
                    item.extension.upper(),
                    "",
                    "",
                    state_label(item.state),
                ),
                tags=(tag,) if tag else (),
            )
            board.row_files[iid] = item
            if item.id in selected_files:
                tree.selection_add(iid)
        if board.status is not None:
            extra = ""
            if board.files and len(visible) != len(board.files):
                extra = f" {len(visible)} match the filter."
            if board.running:
                board.status.set(f"Searching… {len(board.files)} results so far{extra}")
            elif board.outcome:
                board.status.set(f"{board.outcome}{extra}")
        self._retitle(board)

    def show_search_files(self, board: SearchBoard, files: list[SearchFile]) -> None:
        """Replace a tab's results. Tests and the live watcher both use this."""
        states = {item.id: item.state for item in board.files}
        merged: list[SearchFile] = []
        for item in files:
            previous = states.get(item.id) or self.worker.manual_state(item.id)
            if previous:
                item.state = previous
            merged.append(item)
        board.files = merged
        self._paint_search(board)

    def _start_board_search(self, board: SearchBoard) -> None:
        loop = getattr(self.worker, "_loop", None)
        if loop is None or not loop.is_running():
            if board.status is not None:
                board.status.set("Start the worker to search. Search uses that Soulseek login.")
            return
        board.running = True
        board.outcome = ""
        if board.status is not None:
            board.status.set("Searching…")

        def watch() -> None:
            request = None
            try:
                self._post(lambda: self._mark_search(board, "Sending the search to Soulseek…"))
                request = asyncio.run_coroutine_threadsafe(
                    self.worker.begin_manual_search(board.query), loop
                ).result(timeout=30)
                deadline = time.monotonic() + max(1.0, float(self.worker.cfg.search_wait_seconds))
                while time.monotonic() < deadline and not board.closed:
                    try:
                        files = files_from_results(getattr(request, "results", None))
                    except Exception:
                        log.exception("Could not read search results for %r", board.query)
                        files = list(board.files)
                    self._post(lambda files=files: None if board.closed else self.show_search_files(board, files))
                    time.sleep(0.4)
            except Exception as exc:
                self._post(lambda exc=exc: self._search_failed(board, exc))
                return
            self._post(lambda: self._finish_search(board, request, loop))

        threading.Thread(target=watch, name="manual-search", daemon=True).start()

    def _mark_search(self, board: SearchBoard, text: str) -> None:
        if board.closed or not board.running or board.status is None:
            return
        board.status.set(text)

    def _search_failed(self, board: SearchBoard, exc: Exception) -> None:
        board.running = False
        if isinstance(exc, TimeoutError):
            try:
                detail = self.worker.search_diagnostics()
            except Exception:
                detail = ""
            text = (
                "No results (0 peer replies). The search was not sent because "
                "Soulseek did not respond in time."
            )
            if detail:
                text = f"{text} {detail}"
        else:
            text = str(exc).strip() or type(exc).__name__
        board.outcome = text
        if board.status is not None and not board.closed:
            board.status.set(text)

    def _finish_search(self, board: SearchBoard, request, loop) -> None:
        board.running = False
        results = getattr(request, "results", None)
        try:
            replies, shown, _locked, outcome = self.worker.note_search_receipt(board.query, results)
        except Exception:
            log.exception("Could not record the search result for %r", board.query)
            replies, shown, outcome = (
                0,
                len(board.files),
                "No results (0 peer replies). Could not read the search replies.",
            )
        if shown:
            noun = "result" if shown == 1 else "results"
            reply_word = "reply" if replies == 1 else "replies"
            board.outcome = f"{shown} {noun} ({replies} peer {reply_word})"
        else:
            board.outcome = outcome
        if not board.closed:
            try:
                self.show_search_files(board, files_from_results(results))
            except Exception:
                log.exception("Could not list search results for %r", board.query)
        try:
            if loop is not None and loop.is_running():
                asyncio.run_coroutine_threadsafe(self._drop_request(request), loop)
        except Exception:
            pass

    async def _drop_request(self, request) -> None:
        client = self.worker.client
        if client is None:
            return
        try:
            client.searches.remove_request(request)
        except Exception:
            pass

    def _refresh_open_search_states(self) -> None:
        for board in list(getattr(self, "_boards", ())):
            if board.closed:
                continue
            changed = False
            for item in board.files:
                state = self.worker.manual_state(item.id)
                if state and state != item.state:
                    item.state = state
                    changed = True
            if changed:
                self._paint_search(board)

    def _selected_search_targets(self, board: SearchBoard) -> tuple[set[str], set[str]]:
        tree = board.tree
        if tree is None:
            return set(), set()
        file_ids = {board.row_files[iid].id for iid in tree.selection() if iid in board.row_files}
        users = {board.row_users[iid] for iid in tree.selection() if iid in board.row_users}
        return file_ids, users

    def _build_messages(self, parent: ttk.Frame) -> None:
        self._page_header(
            parent,
            "Messages",
            "Private messages only. Chat rooms stay off. A message leaves this PC only when you press Send in the box below.",
        )
        self.message_status = tk.StringVar(value="")
        ttk.Label(parent, textvariable=self.message_status, style="Muted.TLabel").pack(anchor="w", pady=(self.px(8), 0))
        body = ttk.Frame(parent, style="Main.TFrame")
        body.pack(fill="both", expand=True, pady=(self.px(8), 0))
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        self.message_threads = tk.Listbox(body, height=12, activestyle="none", exportselection=False)
        self.message_threads.grid(row=0, column=0, sticky="ns", padx=(0, self.px(8)))
        self.message_threads.bind("<<ListboxSelect>>", self._on_message_thread)
        self._message_names: list[str] = []
        self.message_history = tk.Text(body, wrap="word", height=12, bd=0, highlightthickness=0)
        self.message_history.grid(row=0, column=1, sticky="nsew")
        self.message_history.configure(state="disabled")
        compose = ttk.Frame(parent, style="Main.TFrame")
        compose.pack(fill="x", pady=(self.px(8), 0))
        self.message_user_var = tk.StringVar()
        ttk.Label(compose, text="To").pack(side="left")
        ttk.Entry(compose, textvariable=self.message_user_var, width=18).pack(side="left", padx=(self.px(6), self.px(8)))
        self.message_compose_var = tk.StringVar()
        entry = ttk.Entry(compose, textvariable=self.message_compose_var)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda _event: self._send_composed_message())
        self.message_compose = entry
        ttk.Button(compose, text="Send", command=self._send_composed_message).pack(side="left", padx=(self.px(8), 0))
        self._message_signature = None

    def _refresh_message_badge(self) -> None:
        item = self._nav_items.get(str(getattr(self, "messages_tab", None)))
        if item is None:
            return
        try:
            count = self.worker.store.unread_private_count()
        except Exception:
            log.exception("Could not count private messages")
            return
        item.set_text("Messages" if count <= 0 else f"Messages ({count})")

    def refresh_messages(self) -> None:
        self._refresh_message_badge()
        threads_box = getattr(self, "message_threads", None)
        if threads_box is None or not self.messages_tab.winfo_manager():
            return
        try:
            threads = self.worker.store.private_threads()
        except Exception:
            log.exception("Could not load private messages")
            return
        signature = tuple((row["username"], row["unread"], row["last_body"]) for row in threads)
        if signature != getattr(self, "_message_signature", None):
            self._message_signature = signature
            self._fill_message_threads(threads)
        self._fill_message_history()

    def _fill_message_threads(self, threads: list[dict]) -> None:
        box = self.message_threads
        current = self.message_user_var.get().strip().lower()
        box.delete(0, "end")
        self._message_names = []
        pick = None
        for index, thread in enumerate(threads):
            name = str(thread.get("username") or "")
            unread = int(thread.get("unread") or 0)
            label = f"{name} ({unread})" if unread else name
            box.insert("end", label)
            self._message_names.append(name)
            if name.lower() == current:
                pick = index
        if pick is not None:
            box.selection_set(pick)

    def _fill_message_history(self) -> None:
        username = self.message_user_var.get().strip()
        lines = []
        if username:
            for row in self.worker.store.private_history(username):
                who = "You" if row.get("direction") == "out" else row.get("username") or username
                lines.append(f"{who}: {row.get('body') or ''}")
        text = "\n".join(lines)
        box = self.message_history
        if box.get("1.0", "end").strip() == text:
            return
        box.configure(state="normal")
        box.delete("1.0", "end")
        if text:
            box.insert("1.0", text)
        box.configure(state="disabled")

    def _on_message_thread(self, _event=None) -> None:
        box = self.message_threads
        selected = box.curselection()
        if not selected:
            return
        try:
            name = self._message_names[selected[0]]
        except IndexError:
            return
        self.message_user_var.set(name)
        self.worker.store.mark_thread_read(name)
        self._message_signature = None
        self.refresh_messages()

    def _send_composed_message(self) -> None:
        username = self.message_user_var.get().strip()
        body = str(self.message_compose_var.get() or "")
        if not username:
            self.message_status.set("Enter a Soulseek username.")
            return
        if not body.strip():
            self.message_status.set("Type a message in the compose box.")
            return
        loop = getattr(self.worker, "_loop", None)
        if loop is None or not loop.is_running():
            self.message_status.set("Start the worker to send a private message.")
            return
        self.message_status.set(f"Sending to {username}…")
        future = asyncio.run_coroutine_threadsafe(self.worker.send_private_message(username, body), loop)

        def done() -> None:
            try:
                text = future.result(timeout=30)
            except Exception as exc:
                text = str(exc).strip() or "The message was not sent."

            def apply(text=text) -> None:
                self.message_status.set(text)
                if text.startswith("Sent"):
                    self.message_compose_var.set("")
                self._message_signature = None
                self.refresh_messages()

            self._post(apply)

        threading.Thread(target=done, name="private-message", daemon=True).start()

    def _build_shared(self, parent: ttk.Frame) -> None:
        self._page_header(parent, "Shared", "Uploads from other Soulseek users. Pause keeps sharing up. Stop closes it.")
        top = ttk.Frame(parent, style="Main.TFrame")
        top.pack(fill="x", pady=(self.px(14), 0))
        self.share_totals = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.share_totals).pack(side="left")
        self.pause_uploads_var = tk.BooleanVar(value=bool(self.worker.cfg.pause_uploads))
        ttk.Checkbutton(
            top,
            text="Pause all uploads",
            variable=self.pause_uploads_var,
            command=self._toggle_pause_uploads,
        ).pack(side="right")
        actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=(8, 8))
        ttk.Button(actions, text="Pause", command=lambda: self._upload_action("pause")).pack(side="left")
        ttk.Button(actions, text="Resume", command=lambda: self._upload_action("resume")).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Cancel", command=lambda: self._upload_action("cancel")).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Cancel all from this user", command=lambda: self._upload_action("cancel-user")).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(actions, text="Ban user", command=lambda: self._upload_action("ban")).pack(side="left", padx=(8, 0))
        card = self._card(parent, padding=(self.px(8), self.px(8), self.px(4), self.px(4)))
        card.pack(fill="both", expand=True)
        frame, tree = scrolled_tree(
            card.inner,
            (
                ("user", "User", 140, False),
                ("file", "File", 360, True),
                ("size", "Size", 90, False),
                ("progress", "Progress", 140, False),
                ("speed", "Speed", 90, False),
                ("status", "Status", 90, False),
                ("when", "Time", 140, False),
            ),
            frame_style="Card.TFrame",
        )
        frame.pack(fill="both", expand=True)
        self._bind_filling_tree(tree)
        self.shared_tree = tree
        tree.configure(selectmode="extended")
        self.refresh_shared()

    def refresh_shared(self) -> None:
        tree = getattr(self, "shared_tree", None)
        if tree is None:
            return
        snapshot = self.worker.upload_snapshot()
        if hasattr(self, "pause_uploads_var") and bool(self.pause_uploads_var.get()) != bool(snapshot["pause_all"]):
            self.pause_uploads_var.set(bool(snapshot["pause_all"]))
        self.share_totals.set(
            "Today {today_files} files, {today_bytes} · All time {all_files} files, {all_bytes} · Sharing {share_files} files in {share_folders} folders".format(
                today_files=snapshot["today_files"],
                today_bytes=format_bytes(int(snapshot["today_bytes"])),
                all_files=snapshot["all_files"],
                all_bytes=format_bytes(int(snapshot["all_bytes"])),
                share_files=snapshot["share_files"],
                share_folders=snapshot["share_folders"],
            )
        )
        selected = {tree.set(iid, "file") + "\n" + tree.set(iid, "user") for iid in tree.selection()}
        children = tree.get_children()
        if children:
            tree.delete(*children)
        self._shared_rows = list(snapshot["rows"]) + list(snapshot["history"])
        for row in self._shared_rows:
            status = str(row["status"])
            iid = tree.insert(
                "",
                "end",
                values=(
                    row["username"],
                    row["filename"],
                    format_bytes(int(row["size"] or 0)),
                    format_progress(row.get("progress")),
                    format_speed(int(row.get("speed") or 0)),
                    status,
                    format_when(row.get("when")) if not row.get("live") else (row.get("when") or ""),
                ),
            )
            if f"{row['filename']}\n{row['username']}" in selected:
                tree.selection_add(iid)

    def _selected_shared(self) -> list[dict]:
        tree = self.shared_tree
        chosen = []
        for iid in tree.selection():
            index = tree.index(iid)
            if 0 <= index < len(self._shared_rows):
                chosen.append(self._shared_rows[index])
        return chosen

    def _toggle_pause_uploads(self) -> None:
        self.worker.set_pause_uploads(bool(self.pause_uploads_var.get()))
        loop = getattr(self.worker, "_loop", None)
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(self.worker._enforce_upload_policy(), loop)

    def _upload_action(self, action: str) -> None:
        rows = self._selected_shared()
        if not rows:
            return
        loop = getattr(self.worker, "_loop", None)
        if action == "ban":
            for row in rows:
                self.worker.ban_user(str(row["username"]))
            if loop is not None and loop.is_running():
                for row in rows:
                    asyncio.run_coroutine_threadsafe(self.worker.cancel_user_uploads(str(row["username"])), loop)
            self.refresh_shared()
            return
        if loop is None or not loop.is_running():
            self.notice_var.set("Start the worker to manage uploads.")
            return
        for row in rows:
            username = str(row["username"])
            filename = str(row["filename"])
            if action == "pause":
                asyncio.run_coroutine_threadsafe(self.worker.pause_upload(username, filename), loop)
            elif action == "resume":
                asyncio.run_coroutine_threadsafe(self.worker.resume_upload(username, filename), loop)
            elif action == "cancel":
                asyncio.run_coroutine_threadsafe(self.worker.cancel_upload(username, filename), loop)
            elif action == "cancel-user":
                asyncio.run_coroutine_threadsafe(self.worker.cancel_user_uploads(username), loop)

    def _selected_username(self, board: SearchBoard) -> str:
        if board.tree is None:
            return ""
        file_ids, users = self._selected_search_targets(board)
        if users:
            return next(iter(users))
        for item in board.files:
            if item.id in file_ids:
                return item.username
        return ""

    def download_all_from_user(self) -> None:
        board = self._current_board()
        if board is None:
            return
        username = self._selected_username(board)
        if not username:
            if board.status is not None:
                board.status.set("Select a user or one of their files.")
            return
        chosen = [item for item in board.filters.apply(board.files) if item.username == username]
        if not chosen:
            if board.status is not None:
                board.status.set("That user has no files in the current results.")
            return
        self.queue_search_files(board, chosen)

    def _search_wanted_scope(self, board: SearchBoard) -> dict:
        link = board.link
        if link is not None and (link.artist or "").strip():
            kind = (link.kind or "artist").casefold()
            return {
                "artist": link.artist.strip(),
                "album": (link.album or "").strip() if kind in {"album", "song"} else "",
                "kind": kind,
                "album_id": link.album_id if kind in {"album", "song"} else None,
                "track_id": link.track_id if kind == "song" else None,
                "title": (link.title or "").strip() if kind == "song" else "",
            }
        return {
            "artist": (board.query or "").strip(),
            "album": "",
            "kind": "artist",
            "album_id": None,
            "track_id": None,
            "title": "",
        }

    def download_all_wanted(self) -> None:
        board = self._current_board()
        if board is None:
            return
        username = self._selected_username(board)
        if username:
            self._download_wanted_from_user(board, username)
            return
        self._download_wanted_from_everyone(board)

    def _download_wanted_from_user(self, board: SearchBoard, username: str) -> None:
        files = [item for item in board.files if item.username == username]
        matches = self.worker.match_user_files(files)
        if not matches:
            text = f"No wanted matches in {username}'s results."
            if board.status is not None:
                board.status.set(text)
            log.info("Download all wanted from %s for %r: %s", username, board.query, text)
            return
        text = f"{len(matches)} wanted {_wanted_word(len(matches))} matched from {username}."
        if board.status is not None:
            board.status.set(text)
        log.info("Download all wanted from %s for %r: %s", username, board.query, text)
        self._confirm_wanted(
            matches,
            lambda picked, board=board: self._queue_matches(board, picked),
            summary=text,
        )

    def _download_wanted_from_everyone(self, board: SearchBoard) -> None:
        scope = self._search_wanted_scope(board)
        artist = scope["artist"]
        if not artist:
            if board.status is not None:
                board.status.set("Search for an artist first.")
            return
        plan = self.worker.plan_wanted_downloads(board.files, **scope)
        text = plan.line()
        extra = ""
        if plan.skipped and plan.matched == 0 and plan.missing == 0:
            extra = f" {plan.skipped} already downloaded or queued."
        elif plan.matched == 0 and plan.missing == 0:
            extra = " No wanted rows for this artist."
        if board.status is not None:
            board.status.set(text + extra)
        log.info("Download all wanted for %r: %s%s", artist, text, extra)
        if not plan.matches:
            return
        self._confirm_wanted(
            plan.matches,
            lambda picked, board=board, text=text: self._queue_matches(board, picked, note=text),
            summary=text,
        )

    def _confirm_wanted(self, matches, on_queue, summary: str = "") -> None:
        """Ask before queueing Download all wanted, unless that popup was turned off."""
        if not bool(getattr(self.worker.cfg, "confirm_download_all", True)):
            on_queue(list(matches))
            return
        WantedPreview(self, matches, on_queue, summary=summary, on_dont_ask=self._disable_download_confirm)

    def _disable_download_confirm(self) -> None:
        self.worker.cfg = replace(self.worker.cfg, confirm_download_all=False)
        form = self._settings_form
        if form is not None and "confirm_download_all" in getattr(form, "fields", {}):
            form.fields["confirm_download_all"].set(False)
        try:
            save_config(self.config_path, self.worker.cfg)
        except OSError:
            log.exception("Could not save the download confirmation setting")

    def _queue_matches(self, board: SearchBoard, matches, note: str = "") -> None:
        by_id = {item.id: item for item in board.files}
        files = []
        specs = []
        for match in matches:
            file_id = f"{match.username}\n{match.filename}"
            item = by_id.get(file_id)
            if item is None:
                item = SearchFile(username=match.username, filename=match.filename, size=match.size)
                board.files.append(item)
            files.append(item)
            spec = file_spec(item)
            spec["link"] = match.link()
            specs.append(spec)
        self.queue_search_files(board, files, specs=specs, note=note)

    def download_selected_files(self) -> None:
        self._queue_current(folder=False)

    def download_selected_folder(self) -> None:
        self._queue_current(folder=True)

    def _queue_current(self, *, folder: bool) -> None:
        board = self._current_board()
        if board is None:
            return
        file_ids, users = self._selected_search_targets(board)
        chosen = choose_downloads(board.filters.apply(board.files), file_ids, folder=folder, usernames=users if folder else set())
        if not chosen and not folder:
            chosen = choose_downloads(board.filters.apply(board.files), file_ids, folder=False)
        if not chosen:
            if board.status is not None:
                board.status.set("Select a file first.")
            return
        self.queue_search_files(board, chosen)

    def queue_search_files(
        self,
        board: SearchBoard,
        files: list[SearchFile],
        specs: list[dict] | None = None,
        note: str = "",
    ) -> None:
        loop = getattr(self.worker, "_loop", None)
        if loop is None or not loop.is_running():
            if board.status is not None:
                board.status.set("Start the worker to download. Downloads use that Soulseek connection.")
            return
        for item in files:
            item.state = "queued"
        self._paint_search(board)
        if board.status is not None:
            message = f"Queued {len(files)} file(s)"
            if note:
                message = f"{message}. {note}"
            board.status.set(message)
            if note:
                log.info("%s", message)
        link = None
        if board.link is not None:
            link = {
                "kind": board.link.kind,
                "key": board.link.key,
                "artist": board.link.artist,
                "album": board.link.album,
                "title": board.link.title,
                "album_id": board.link.album_id,
                "track_id": board.link.track_id,
            }
        if specs is None:
            specs = [file_spec(item) for item in files]
            if link is not None:
                for spec in specs:
                    spec["link"] = link
        future = asyncio.run_coroutine_threadsafe(self.worker.download_manual(specs, link), loop)

        def done() -> None:
            try:
                future.result()
            except Exception as exc:
                self._post(lambda exc=exc: self._search_failed(board, exc))
                return
            self._post(lambda: self._refresh_open_search_states())

        threading.Thread(target=done, name="manual-download", daemon=True).start()

    def browse_selected_folder(self) -> None:
        board = self._current_board()
        if board is None or board.tree is None:
            return
        file_ids, users = self._selected_search_targets(board)
        username = ""
        folder = ""
        for item in board.files:
            if item.id in file_ids:
                username = item.username
                folder = item.folder
                break
        if not username and users:
            username = next(iter(users))
            for item in board.files:
                if item.username == username and item.folder:
                    folder = item.folder
                    break
        if not username or not folder:
            if board.status is not None:
                board.status.set("Select a file to browse its folder.")
            return
        loop = getattr(self.worker, "_loop", None)
        if loop is None or not loop.is_running():
            if board.status is not None:
                board.status.set("Start the worker to browse. Browse uses that Soulseek login.")
            return
        if board.status is not None:
            board.status.set(f"Browsing {username}…")
        future = asyncio.run_coroutine_threadsafe(self.worker.browse_folder(username, folder), loop)

        def done() -> None:
            try:
                records = future.result(timeout=20)
            except Exception as exc:
                self._post(lambda exc=exc: self._search_failed(board, exc))
                return

            def apply() -> None:
                if board.closed:
                    return
                extra = [
                    SearchFile(
                        username=str(record.get("username") or username),
                        filename=str(record.get("filename") or ""),
                        folder=str(record.get("folder") or folder),
                        size=int(record.get("size") or 0),
                        bitrate=int(record.get("bitrate") or 0),
                        duration=int(record.get("duration") or 0),
                        extension=str(record.get("extension") or ""),
                        free_slot=bool(record.get("free_slot")),
                        speed=int(record.get("speed") or 0),
                        queue=int(record.get("queue") or 0),
                    )
                    for record in records
                    if record.get("filename")
                ]
                self.show_search_files(board, _merge_files(board.files, extra))
                if board.status is not None:
                    board.status.set(f"Browsed {folder}")

            self._post(apply)

        threading.Thread(target=done, name="browse-folder", daemon=True).start()

    def _search_right_click(self, event) -> None:
        tree = event.widget
        row = tree.identify_row(event.y)
        if row and row not in tree.selection():
            tree.selection_set(row)
        if self._search_menu is not None:
            board = self._current_board()
            username = self._selected_username(board) if board is not None else ""
            label = "Download all wanted from this user" if username else "Download all wanted from every user"
            self._search_menu.entryconfig(3, label=label)
            self._search_menu.tk_popup(event.x_root, event.y_root)

    def _zoom_glyph(self, image: tk.PhotoImage) -> tk.PhotoImage:
        factor = max(1, int(round(self._glyph_size / 16)))
        if factor == 1:
            return image
        return image.zoom(factor, factor)

    def _restyle_glyphs(self) -> None:
        if not getattr(self, "wanted", None):
            return
        self._glyph_size = max(16, int(round(16 * self.ui_scale)))
        self._check_images = {state: self._checkbox_image(state) for state in ("on", "off", "mixed")}
        self._finder_image = self._magnifier_image()
        palette = active_palette(self)
        for iid, state in list(self._row_checks.items()):
            try:
                self.wanted.item(iid, image=self._check_images.get(state, self._check_images["on"]))
            except tk.TclError:
                pass
        for label in self._finder_labels:
            try:
                label.configure(image=self._finder_image, bg=palette.card)
            except tk.TclError:
                pass
        if getattr(self, "transfers", None) is not None:
            self.transfers.tag_configure("ok", foreground=palette.ok)
            self.transfers.tag_configure("bad", foreground=palette.bad)
        self._place_finders()

    def _checkbox_image(self, state: str) -> tk.PhotoImage:
        """A drawn box, so the control does not depend on a Unicode font."""
        palette = active_palette(self)
        image = tk.PhotoImage(master=self, width=16, height=16)
        border = palette.check_border
        if state == "off":
            fill = palette.check_off
            glyph = None
        else:
            fill = palette.check_on
            glyph = palette.check_glyph
        image.put(border, to=(0, 0, 16, 16))
        image.put(fill, to=(2, 2, 14, 14))
        if state == "on" and glyph:
            for x, y in (
                (3, 8), (4, 9), (5, 10), (6, 11), (7, 10), (8, 9), (9, 8), (10, 7), (11, 6), (12, 5),
                (4, 8), (5, 9), (6, 10), (7, 9), (8, 8), (9, 7), (10, 6), (11, 5),
            ):
                image.put(glyph, to=(x, y, x + 1, y + 1))
        elif state == "mixed" and glyph:
            image.put(glyph, to=(4, 7, 12, 10))
        return self._zoom_glyph(image)

    def _row_values(self, item: WantedItem) -> tuple:
        return (item.kind.title(), item.artist, item.album, item.song, item.detail, "")

    def _paint_check(self, iid: str, item: WantedItem, state: str) -> None:
        self.wanted.item(iid, text="", image=self._check_images[state], values=self._row_values(item))
        self._row_checks[iid] = state
        self._row_items[iid] = item

    def _fill_tree(self, items: list[WantedItem], total: int) -> None:
        self._row_items.clear()
        self._row_checks.clear()
        children = self.wanted.get_children()
        if not items:
            self.wanted.delete(*children)
            if self._syncing and total == 0:
                text = "Syncing…"
            elif (self.search_var.get() or "").strip():
                text = "No artists match that search."
            else:
                text = f"Nothing in {self.view_var.get() or VIEW_EVERYTHING}."
            self.wanted.insert("", "end", text="", values=("", text, "", "", "", ""))
            self._place_finders()
            self.wanted_scroll.set(0, 1)
            return

        if len(children) != len(items):
            self.wanted.delete(*children)
            children = [
                self.wanted.insert("", "end", text="", image=self._check_images["on"], values=self._row_values(item))
                for item in items
            ]
        for iid, item in zip(children, items):
            self._paint_check(iid, item, self._shown.get(item.key, "on"))
        self._place_finders()
        if total <= len(items):
            self.wanted_scroll.set(0, 1)
        else:
            self.wanted_scroll.set(self._wanted_offset / total, (self._wanted_offset + len(items)) / total)

    def refresh_wanted(self) -> None:
        if self.worker.store.wanted_catalog_ready():
            if not self._wanted_sort:
                cfg = self.worker.cfg
                wanted = (
                    f"{normalize_search_order(cfg.search_order)}|"
                    f"{normalize_search_direction(cfg.search_order_direction)}"
                )
                if self.worker.store.get_meta("display_order") != wanted:
                    self.worker.store.refresh_display_order(cfg.search_order, cfg.search_order_direction)
            self._refresh_wanted_sql()
            return
        self._sql_mode = False
        items = self.worker.catalog.items()
        visible = self._ordered_rows(items)
        choices = self._choices()
        grouped = children_by_artist(items)
        self._wanted_rows = visible
        self._wanted_grouped = grouped
        self._paint_wanted(choices, grouped)
        state = select_all_state(visible, items, choices, grouped)
        checked = sum(1 for item in visible if item_checked(item, items, choices, grouped))
        self._updating_select_all = True
        try:
            self.select_all_var.set(1 if state == "all" and visible else 0)
        finally:
            self._updating_select_all = False
        if items and not self._syncing and not str(self.wanted_status.get()).startswith("Saving "):
            self.wanted_status.set(f"{checked:,} of {len(visible):,} selected in this view.")

    def _paint_wanted(self, choices: dict[str, bool], grouped: dict[str, list[WantedItem]]) -> None:
        rows = self._wanted_rows
        window = max(1, self._wanted_window)
        total = len(rows)
        if total <= window:
            self._wanted_offset = 0
        else:
            self._wanted_offset = max(0, min(self._wanted_offset, total - window))
        shown = rows[self._wanted_offset : self._wanted_offset + window]
        self._row_items.clear()
        self._row_checks.clear()
        children = self.wanted.get_children()
        if not shown:
            self.wanted.delete(*children)
            if not self.worker.catalog.items():
                text = "Click Sync to load the wanted list from Lidarr."
            else:
                text = f"Nothing in {self.view_var.get() or VIEW_EVERYTHING}."
            self.wanted.insert("", "end", text="", values=("", text, "", "", "", ""))
            self._place_finders()
            self.wanted_scroll.set(0, 1)
            return
        if len(children) != len(shown):
            self.wanted.delete(*children)
            children = [
                self.wanted.insert("", "end", text="", image=self._check_images["on"], values=self._row_values(item))
                for item in shown
            ]
        for iid, item in zip(children, shown):
            self._paint_check(iid, item, self._memory_check_state(item, choices, grouped))
        self._place_finders()
        if total <= window:
            self.wanted_scroll.set(0, 1)
        else:
            self.wanted_scroll.set(self._wanted_offset / total, (self._wanted_offset + len(shown)) / total)

    def _memory_check_state(
        self,
        item: WantedItem,
        choices: dict[str, bool],
        grouped: dict[str, list[WantedItem]],
    ) -> str:
        if item.kind != "artist":
            return "on" if is_enabled(item.key, choices) else "off"
        children = grouped.get(artist_token(item.artist), [])
        if not children:
            return "on" if is_enabled(item.key, choices) else "off"
        flags = [is_enabled(row.key, choices) for row in children]
        if all(flags):
            return "on"
        if any(flags):
            return "mixed"
        return "off"


    def check_for_updates(self, manual: bool = True) -> None:
        if self._shutting_down:
            return
        if self._update_busy:
            if manual:
                self._update_manual = True
            return
        self._update_busy = True
        self._update_manual = manual
        if manual:
            self.notice_var.set("Checking for updates…")

        def work() -> None:
            try:
                skipped = load_skipped_update(self.worker.store)
                status = check_for_update(app_version(), skipped)
            except Exception as exc:
                log.exception("Update check failed")
                status = UpdateStatus(current=app_version(), error=f"Could not check for updates: {exc}")
            self._post(lambda: self._update_check_finished(status, self._update_manual))

        threading.Thread(target=work, name="update-check", daemon=True).start()

    def _update_check_finished(self, status: UpdateStatus, manual: bool) -> None:
        self._update_busy = False
        if self._shutting_down:
            return
        kind = prompt_kind(status, manual=manual)
        if kind == "quiet":
            if status.error and not self.notice_var.get():
                self.notice_var.set(status.error)
            elif status.error:
                log.info("Update check: %s", status.error)
            return
        if kind == "current":
            self.notice_var.set("")
            if status.latest and not versions_equal(status.latest, status.current):
                message = f"You're on {status.current}. The latest published release is {status.latest}."
            else:
                message = f"You're on {status.current}, which is the latest release."
            messagebox.showinfo("Rummagarr", message, parent=self)
            return
        if kind == "error":
            self.notice_var.set(status.error or "Could not check for updates.")
            messagebox.showerror("Rummagarr", status.error or "Could not check for updates.", parent=self)
            return
        self.notice_var.set("")
        self._show_update_prompt(status)

    def _show_update_prompt(self, status: UpdateStatus) -> None:
        if self._update_prompt is not None:
            try:
                if self._update_prompt.winfo_exists():
                    self._update_prompt.lift()
                    return
            except tk.TclError:
                self._update_prompt = None

        def later() -> None:
            self._close_update_prompt()

        def skip() -> None:
            if status.latest:
                try:
                    remember_skipped_update(self.worker.store, status.latest)
                except Exception:
                    log.exception("Could not remember the skipped version")
            self.notice_var.set(f"Skipped version {status.latest}.")
            self._close_update_prompt()

        def install() -> None:
            self._close_update_prompt()
            if status.can_install:
                self._begin_download(status)
                return
            webbrowser.open(status.page_url)

        self._update_prompt = UpdatePrompt(self, status, install, skip, later)

    def _close_update_prompt(self) -> None:
        prompt = self._update_prompt
        self._update_prompt = None
        if prompt is None:
            return
        try:
            prompt.destroy()
        except tk.TclError:
            pass

    def _begin_download(self, status: UpdateStatus) -> None:
        progress = DownloadProgress(self)
        self._download_progress = progress

        def on_progress(done: int, total: int) -> None:
            def paint() -> None:
                if self._shutting_down:
                    return
                try:
                    if progress.winfo_exists():
                        progress.report(done, total)
                except tk.TclError:
                    pass

            self._post(paint)

        def work() -> None:
            try:
                folder = Path(tempfile.mkdtemp(prefix="lidarr-soulseek-update-"))
                dest = folder / (status.asset_name or "Rummagarr-Setup.exe")
                download_verified(status, dest, on_progress=on_progress)
            except Exception as exc:
                log.exception("Update download failed")
                self._post(lambda: self._download_failed(exc))
                return
            self._post(lambda: self._download_ready(dest))

        threading.Thread(target=work, name="update-download", daemon=True).start()

    def _close_progress(self) -> None:
        progress = self._download_progress
        self._download_progress = None
        if progress is None:
            return
        try:
            progress.destroy()
        except tk.TclError:
            pass

    def _download_failed(self, exc: Exception) -> None:
        self._close_progress()
        if self._shutting_down:
            return
        message = str(exc) or "Could not download the update."
        self.notice_var.set(message)
        messagebox.showerror("Rummagarr", message, parent=self)

    def _download_ready(self, dest: Path) -> None:
        if self._shutting_down:
            return
        progress = self._download_progress
        if progress is not None:
            try:
                progress.label_var.set("Stopping the worker so the installer can replace the app…")
            except tk.TclError:
                pass

        def work() -> None:
            stopped = wait_for_worker_stop(self.worker.control, self._thread, timeout=25)
            if not stopped:
                log.warning("Worker was still stopping; the installer will close the app")
            self._post(lambda: self._launch_update(dest))

        threading.Thread(target=work, name="update-stop", daemon=True).start()

    def _launch_update(self, dest: Path) -> None:
        self._shutting_down = True
        try:
            launch_installer(dest)
        except OSError as exc:
            log.exception("Could not start the installer")
            self._shutting_down = False
            self._close_progress()
            messagebox.showerror("Rummagarr", f"Could not start the installer: {exc}", parent=self)
            return
        try:
            if self._thread is None or not self._thread.is_alive():
                self.worker.close()
        except Exception:
            log.exception("Could not close the worker")
        self._close_progress()
        self.destroy()


def run_setup(config_path: Path) -> Config | None:
    root = tk.Tk()
    root.title("Rummagarr setup")
    root.geometry("980x820")
    apply_theme(root)
    saved: dict[str, Config] = {}

    def on_saved(cfg: Config) -> None:
        saved["cfg"] = cfg
        root.destroy()

    def on_cancel() -> None:
        root.destroy()

    form = SettingsForm(root, config_path, None, on_saved, on_cancel)
    form.pack(fill="both", expand=True)
    root.protocol("WM_DELETE_WINDOW", on_cancel)
    root.mainloop()
    return saved.get("cfg")


def main() -> None:
    enable_windows_dpi()
    hold_single_instance()
    path = desktop_config_path()
    cfg: Config | None = None
    if path.exists():
        try:
            cfg = try_load_config(path)
        except ConfigError as exc:
            log.warning("Config needs attention: %s", exc)
            cfg = None
    if cfg is None:
        cfg = run_setup(path)
        if cfg is None:
            return
    setup_logging(cfg.log_file)
    worker = Worker(cfg)
    app = DesktopApp(
        worker,
        path,
        start_status_server=True,
        honor_start_on_launch=True,
        check_updates=cfg.check_updates,
    )
    app.mainloop()


if __name__ == "__main__":
    from process_launch import configure_child_processes

    configure_child_processes()
    main()
