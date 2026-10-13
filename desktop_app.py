from __future__ import annotations

import asyncio
import logging
import os
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
from config_loader import (
    Config,
    ConfigError,
    app_version,
    default_config,
    default_media_dirs,
    desktop_config_path,
    save_config,
    try_load_config,
)
from lidarr_client import LidarrClient
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


def ui_font() -> str:
    families = set(tkfont.families())
    for name in ("Segoe UI", "DejaVu Sans", "Liberation Sans"):
        if name in families:
            return name
    return "TkDefaultFont"


def apply_theme(root: tk.Misc) -> None:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    font = ui_font()
    bg = "#111111"
    panel = "#1c1c1c"
    fg = "#eeeeee"
    muted = "#b5b5b5"
    root.configure(background=bg)
    style.configure(".", background=bg, foreground=fg, font=(font, 10))
    style.configure("TFrame", background=bg)
    style.configure("TLabel", background=bg, foreground=fg, font=(font, 10))
    style.configure("Muted.TLabel", background=bg, foreground=muted, font=(font, 10))
    style.configure("Title.TLabel", background=bg, foreground=fg, font=(font, 16, "bold"))
    style.configure("TButton", background="#333333", foreground=fg, borderwidth=0, padding=(12, 6))
    style.map("TButton", background=[("active", "#4a4a4a"), ("disabled", "#242424")], foreground=[("disabled", "#888888")])
    style.configure("Start.TButton", background="#1f6b3a", foreground="#ffffff", padding=(12, 6))
    style.map("Start.TButton", background=[("active", "#27864a"), ("disabled", "#1a3d28")])
    style.configure("Stop.TButton", background="#8b1e1e", foreground="#ffffff", padding=(12, 6))
    style.map("Stop.TButton", background=[("active", "#a52828"), ("disabled", "#3a2424")])
    style.configure("TCheckbutton", background=bg, foreground=fg)
    style.configure("TRadiobutton", background=bg, foreground=fg)
    style.configure("TNotebook", background=bg, borderwidth=0)
    # Clam's selected-state expand eats padding, so the active tab looked smaller.
    # Selected padding and font are larger. Unselected tabs stay compact.
    # expand on the selected state is zero so Windows keeps that padding.
    style.configure("TNotebook.Tab", background="#1a1a1a", foreground=muted, padding=(10, 4), font=(font, 9))
    style.map(
        "TNotebook.Tab",
        background=[("selected", "#243044"), ("active", "#2a2a2a")],
        foreground=[("selected", "#ffffff"), ("!selected", muted)],
        padding=[("selected", (22, 12, 22, 10)), ("!selected", (10, 4))],
        font=[("selected", (font, 12, "bold")), ("!selected", (font, 9))],
        expand=[("selected", [0, 0, 0, 0]), ("!selected", [0, 0, 0, 2])],
    )
    style.configure(
        "Treeview",
        background=panel,
        fieldbackground=panel,
        foreground=fg,
        borderwidth=0,
        rowheight=28,
        font=(font, 10),
    )
    style.configure("Treeview.Heading", background="#242424", foreground=fg, relief="flat", font=(font, 10, "bold"))
    style.map("Treeview", background=[("selected", "#243044")], foreground=[("selected", "#ffffff")])
    style.configure("TEntry", fieldbackground=panel, foreground=fg, insertcolor=fg)
    style.configure("TCombobox", fieldbackground=panel, background=panel, foreground=fg)
    root.option_add("*TCombobox*Listbox.background", panel)
    root.option_add("*TCombobox*Listbox.foreground", fg)
    root.option_add("*TCombobox*Listbox.selectBackground", "#243044")
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")


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


def scrolled_tree(parent: tk.Misc, columns: tuple[tuple[str, str, int, bool], ...]) -> tuple[ttk.Frame, ttk.Treeview]:
    frame = ttk.Frame(parent)
    names = [column[0] for column in columns]
    tree = ttk.Treeview(frame, columns=names, show="headings", selectmode="browse")
    scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.grid(row=0, column=0, sticky="nsew")
    scroll.grid(row=0, column=1, sticky="ns")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    for name, heading, width, stretch in columns:
        tree.heading(name, text=heading)
        tree.column(name, width=width, minwidth=40, anchor="w", stretch=stretch)
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


BEHAVIOR_FIELDS: tuple[BehaviorField, ...] = (
    BehaviorField("focus_one_artist", "One artist at a time", "bool", "", "Stay on a single artist, then move on."),
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
    BehaviorField("preferred_extensions", "Formats, in order", "csv", "", "Tried in this order. Example: mp3, flac, wav."),
    BehaviorField("min_mp3_bitrate", "Minimum MP3 bitrate", "int", "kbps", "Reject MP3s below this bitrate when Soulseek reports one.", 96, 320),
    BehaviorField("convert_to_mp3", "Convert to MP3", "bool", "", "Convert FLAC and WAV to MP3 with ffmpeg before Lidarr imports them."),
    BehaviorField("convert_bitrate", "Convert bitrate", "int", "kbps", "Bitrate ffmpeg uses for the MP3.", 128, 320),
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
    if changed - _RESTART_FIELDS - _LAUNCH_FIELDS - _SHARE_LIVE_FIELDS - _SHARE_RESCAN_FIELDS:
        parts.append("Search and download settings apply on the next pass.")
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
    ) -> None:
        super().__init__(master, padding=16)
        self.config_path = config_path
        self.base = cfg
        self.on_saved = on_saved
        self.on_cancel = on_cancel
        self.store = store
        self.worker = worker
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
        self.message_var = tk.StringVar(value="")
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
        variable.set("" if value is None else str(value))

    def _build(self) -> None:
        ttk.Label(self, text="Seekarr setup", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            self,
            text="Connection holds the login and folders. Behavior holds search and conversion. Sharing holds what other users can download.",
            style="Muted.TLabel",
            wraplength=900,
        ).grid(row=1, column=0, sticky="w", pady=(4, 8))
        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=2, column=0, sticky="nsew")
        connection = ttk.Frame(self.notebook, padding=12)
        behavior = ttk.Frame(self.notebook, padding=(4, 8, 4, 4))
        sharing = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(connection, text="Connection")
        self.notebook.add(behavior, text="Behavior")
        self.notebook.add(sharing, text="Sharing")
        self._build_connection(connection)
        self._build_behavior(behavior)
        self._build_sharing(sharing)
        ttk.Label(self, textvariable=self.message_var, wraplength=900).grid(row=3, column=0, sticky="w", pady=(8, 4))
        buttons = ttk.Frame(self)
        buttons.grid(row=4, column=0, sticky="w")
        ttk.Button(buttons, text="Test Lidarr", command=self.test_lidarr).pack(side="left")
        ttk.Button(buttons, text="Save", style="Start.TButton", command=self.save).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Restore defaults", command=self.restore_defaults).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Cancel", command=self.on_cancel).pack(side="left", padx=(8, 0))
        ttk.Label(
            self,
            text="API key: Lidarr → Settings → General → Security. Restore defaults does not clear the login or save until you click Save.",
            style="Muted.TLabel",
            wraplength=900,
        ).grid(row=5, column=0, sticky="w", pady=(12, 0))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

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
            text="Search limits, conversion, the status page, and the update check are on the Behavior tab. Uploads are on Sharing.",
            style="Muted.TLabel",
            wraplength=760,
        ).grid(row=13, column=0, columnspan=3, sticky="w", pady=(12, 0))
        parent.columnconfigure(1, weight=1)

    def _build_behavior(self, parent: ttk.Frame) -> None:
        ttk.Label(
            parent,
            text="Each option is the same value config.toml already uses. A short note under the field says what it changes.",
            style="Muted.TLabel",
            wraplength=860,
        ).pack(anchor="w", pady=(0, 6))
        canvas = tk.Canvas(parent, highlightthickness=0, background="#111111", borderwidth=0)
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
        for spec in BEHAVIOR_FIELDS:
            self._behavior_row(inner, row, spec)
            row += 2
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
        folder_box = tk.Listbox(side, height=6, background="#1c1c1c", foreground="#eeeeee", selectmode="browse")
        folder_box.pack(fill="x", pady=(4, 0))
        self._folder_list = folder_box
        folder_buttons = ttk.Frame(side)
        folder_buttons.pack(anchor="w", pady=(4, 0))
        ttk.Button(folder_buttons, text="Add folder", command=self._add_share_folder).pack(side="left")
        ttk.Button(folder_buttons, text="Remove", command=self._remove_share_folder).pack(side="left", padx=(8, 0))
        ttk.Button(folder_buttons, text="Rescan", command=self._rescan_shares).pack(side="left", padx=(8, 0))
        ttk.Label(side, textvariable=self.share_count_var, style="Muted.TLabel", wraplength=420).pack(anchor="w", pady=(4, 8))
        ttk.Label(side, text="Ignored users").pack(anchor="w")
        ban_box = tk.Listbox(side, height=5, background="#1c1c1c", foreground="#eeeeee", selectmode="browse")
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
        if spec.kind == "bool":
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
            slsk_username=username,
            slsk_password=password,
            listen_port=listen,
            obfuscated_port=obfs,
            enable_upnp=bool(self.upnp_var.get()),
            download_dir=Path(download),
            complete_dir=Path(complete),
            start_on_launch=bool(self.launch_var.get()),
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


class UpdatePrompt(tk.Toplevel):
    """Newer release: install it, ask again later, or skip this version."""

    def __init__(self, master: tk.Misc, status: UpdateStatus, on_install, on_skip, on_later) -> None:
        super().__init__(master)
        self.title("Update Seekarr")
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
        notes = tk.Text(
            frame,
            height=8,
            wrap="word",
            relief="flat",
            background="#1c1c1c",
            foreground="#eeeeee",
            insertbackground="#eeeeee",
            highlightthickness=0,
        )
        notes.pack(fill="both", expand=True)
        notes.insert("1.0", status.notes or "No release notes were published.")
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

    def __init__(self, master: tk.Misc, matches, on_queue, summary: str = "") -> None:
        super().__init__(master)
        self.title("Download wanted files")
        self.transient(master)
        self.geometry("760x520")
        self.minsize(640, 400)
        apply_theme(self)
        self.on_queue = on_queue
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
        callback = self.on_queue
        self.destroy()
        if picked:
            callback(picked)


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
        self.title(f"Seekarr {app_version()}")
        self.geometry("1100x720")
        self.minsize(860, 560)
        apply_theme(self)
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
        self.refresh_status()
        self.refresh_wanted()
        if honor_start_on_launch and worker.cfg.start_on_launch:
            self.after(300, self.on_start)
        if check_updates:
            self.after(400, lambda: self.check_for_updates(manual=False))

    def destroy(self) -> None:
        self._shutting_down = True
        try:
            super().destroy()
        except tk.TclError:
            pass
        _release_tk_objects(self)

    def _build(self) -> None:
        self._build_menu()
        header = ttk.Frame(self, padding=(16, 12, 16, 0))
        header.pack(fill="x")
        ttk.Label(header, text="Seekarr", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text=app_version(), style="Muted.TLabel").pack(side="left", padx=(10, 0), pady=(4, 0))
        ttk.Button(header, text="Settings", command=self.open_settings).pack(side="right")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=12)
        self.status_tab = ttk.Frame(self.notebook, padding=12)
        self.wanted_tab = ttk.Frame(self.notebook, padding=12)
        self.search_tab = ttk.Frame(self.notebook, padding=12)
        self.shared_tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.status_tab, text="Status")
        self.notebook.add(self.wanted_tab, text="Wanted")
        self.notebook.add(self.search_tab, text="Search")
        self.notebook.add(self.shared_tab, text="Shared")
        self._boards: list[SearchBoard] = []
        self._search_menu: tk.Menu | None = None
        self._shared_rows: list[dict] = []
        self._build_status(self.status_tab)
        self._build_wanted(self.wanted_tab)
        self._build_search(self.search_tab)
        self._build_shared(self.shared_tab)

    def _build_menu(self) -> None:
        menu = tk.Menu(self, tearoff=0)
        help_menu = tk.Menu(menu, tearoff=0)
        help_menu.add_command(label="Check for updates", command=lambda: self.check_for_updates(manual=True))
        help_menu.add_command(label="About", command=self.show_about)
        menu.add_cascade(label="Help", menu=help_menu)
        self.configure(menu=menu)
        self.help_menu = help_menu

    def show_about(self) -> None:
        messagebox.showinfo(
            "About Seekarr",
            f"Seekarr {app_version()}\n\n"
            "Downloads missing music from Soulseek into Lidarr.\n"
            "Help → Check for updates looks for a newer release.",
            parent=self,
        )

    def _build_status(self, parent: ttk.Frame) -> None:
        controls = ttk.Frame(parent)
        controls.pack(fill="x")
        self.start_btn = ttk.Button(controls, text="Start", style="Start.TButton", command=self.on_start)
        self.pause_btn = ttk.Button(controls, text="Pause", command=self.on_pause)
        self.stop_btn = ttk.Button(controls, text="Stop", style="Stop.TButton", command=self.on_stop)
        self.skip_btn = ttk.Button(controls, text="Skip", command=self.on_skip)
        self.refresh_btn = ttk.Button(controls, text="Refresh", command=self.refresh_status)
        self.cancel_all_btn = ttk.Button(controls, text="Cancel all", command=self.on_cancel_all)
        for button in (self.start_btn, self.pause_btn, self.stop_btn, self.skip_btn):
            button.pack(side="left", padx=(0, 8))
        self.cancel_all_btn.pack(side="right")
        self.refresh_btn.pack(side="right", padx=(0, 8))

        status_row = ttk.Frame(parent)
        status_row.pack(fill="x", pady=(10, 0))
        self.activity_dot = tk.Canvas(status_row, width=14, height=14, highlightthickness=0, bg="#111111")
        self.activity_dot.pack(side="left", padx=(0, 8))
        self._pulse_on = False
        self.status_line = ttk.Label(status_row, text="Stopped · Soulseek offline · Stopped", style="Muted.TLabel")
        self.status_line.pack(side="left", fill="x", expand=True)
        self.session_line = ttk.Label(parent, text="", style="Muted.TLabel", wraplength=920, justify="left")
        self.session_line.pack(fill="x", pady=(2, 0))
        self.notice_line = ttk.Label(parent, textvariable=self.notice_var, style="Muted.TLabel")
        self.notice_line.pack(fill="x", pady=(2, 0))
        self.auto_import_var = tk.BooleanVar(value=bool(self.worker.cfg.auto_import_after_artist))
        self.auto_import = ttk.Checkbutton(
            parent,
            text="Auto-import to Lidarr after each artist",
            variable=self.auto_import_var,
            command=self.on_auto_import,
        )
        self.auto_import.pack(anchor="w", pady=(8, 0))

        ttk.Label(parent, text="Downloads").pack(anchor="w", pady=(12, 4))
        frame, self.transfers = scrolled_tree(
            parent,
            (
                ("file", "File", 340, True),
                ("user", "User", 140, False),
                ("state", "Status", 110, False),
                ("progress", "Progress", 180, False),
                ("action", "", 80, False),
            ),
        )
        frame.pack(fill="both", expand=True)
        self.transfers.tag_configure("ok", foreground="#7dce7d")
        self.transfers.tag_configure("bad", foreground="#e07a7a")
        self.transfers.bind("<Button-1>", self.on_transfer_click)

        recent_head = ttk.Frame(parent)
        recent_head.pack(fill="x", pady=(12, 4))
        ttk.Label(recent_head, text="Recent completed").pack(side="left")
        self.limit_box = ttk.Combobox(
            recent_head,
            textvariable=self.limit_var,
            values=[str(number) for number in RECENT_LIMITS],
            state="readonly",
            width=6,
        )
        self.limit_box.pack(side="right")
        ttk.Label(recent_head, text="Show", style="Muted.TLabel").pack(side="right", padx=(0, 6))
        self.limit_box.bind("<<ComboboxSelected>>", self.on_limit)

        frame, self.recent = scrolled_tree(
            parent,
            (
                ("when", "When", 150, False),
                ("album", "Artist / album", 280, True),
                ("file", "File", 280, True),
                ("user", "User", 140, False),
            ),
        )
        frame.pack(fill="both", expand=True)
        port = self.worker.cfg.status_port
        host = self.worker.cfg.status_bind
        ttk.Label(
            parent,
            text=f"The browser page stays at http://{host}:{port} while the worker is running.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(8, 0))

    def _build_wanted(self, parent: ttk.Frame) -> None:
        ttk.Label(
            parent,
            text="Checked rows are the only ones the worker searches. New rows from Sync start checked. The list is kept on this computer and opens without another Sync.",
            style="Muted.TLabel",
            wraplength=980,
        ).pack(anchor="w")
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill="x", pady=(8, 8))
        self.sync_button = ttk.Button(toolbar, text="Sync", command=self.on_sync)
        self.sync_button.pack(side="left")
        ttk.Label(toolbar, text="View").pack(side="left", padx=(16, 6))
        for view in VIEWS:
            ttk.Radiobutton(toolbar, text=view, value=view, variable=self.view_var, command=self.on_view).pack(
                side="left", padx=(0, 8)
            )
        self.select_all = ttk.Checkbutton(
            toolbar,
            text="Select all",
            variable=self.select_all_var,
            command=self.on_select_all,
        )
        self.select_all.pack(side="right")
        search_row = ttk.Frame(parent)
        search_row.pack(fill="x", pady=(0, 6))
        ttk.Label(search_row, text="Artist").pack(side="left")
        self.search_entry = ttk.Entry(search_row, textvariable=self.search_var)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.search_entry.bind("<KeyRelease>", lambda _event: self.on_search())
        self.count_label = ttk.Label(parent, textvariable=self.wanted_status, style="Muted.TLabel")
        self.count_label.pack(anchor="w", pady=(0, 6))
        frame = ttk.Frame(parent)
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
        self.wanted.column("#0", width=44, minwidth=44, stretch=False, anchor="center")
        self.wanted.heading("#0", text="")
        for name, heading, width, stretch in columns:
            if name == "find":
                self.wanted.heading(name, text="")
                self.wanted.column(name, width=width, minwidth=width, stretch=False, anchor="center")
                continue
            self.wanted.heading(name, text=heading, command=lambda column=name: self.on_wanted_sort(column))
            self.wanted.column(name, width=width, minwidth=40, anchor="w", stretch=stretch)
        frame.pack(fill="both", expand=True)
        self.wanted.bind("<Button-1>", self.on_wanted_click)
        self.wanted.bind("<space>", self.on_wanted_space)
        self.wanted.bind("<MouseWheel>", self._wanted_wheel)
        self.wanted.bind("<Button-4>", self._wanted_wheel)
        self.wanted.bind("<Button-5>", self._wanted_wheel)
        self.wanted.bind("<Up>", self._wanted_key)
        self.wanted.bind("<Down>", self._wanted_key)
        self.wanted.bind("<Configure>", self._wanted_resized)
        self.wanted.bind("<Configure>", lambda _event: self._place_finders(), add="+")

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
        window = tk.Toplevel(self)
        window.title("Seekarr settings")
        window.geometry("980x820")
        window.minsize(760, 640)
        window.transient(self)
        apply_theme(window)
        previous = self.worker.cfg

        def on_saved(cfg: Config) -> None:
            self.worker.cfg = cfg
            self.worker.apply_share_settings(scan=False)
            saved_limit = self.worker.store.get_meta("gui_recent_limit")
            if saved_limit:
                self.limit_var.set(str(normalize_recent_limit(saved_limit)))
            self.notice_var.set(settings_apply_message(previous, cfg))
            window.destroy()

        form = SettingsForm(
            window,
            self.config_path,
            self.worker.cfg,
            on_saved,
            window.destroy,
            store=self.worker.store,
            worker=self.worker,
        )
        form.pack(fill="both", expand=True)

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
            self.session_line.configure(text=str(snapshot.get("soulseek_detail") or ""))
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

    def _wanted_resized(self, event) -> None:
        row_px = 28
        window = max(WANTED_WINDOW_MIN, int(event.height) // row_px or 1)
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
        order = "default"
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
        self.activity_dot.delete("all")
        if busy:
            self._pulse_on = not self._pulse_on
            color = "#3d8bfd" if self._pulse_on else "#8cbcff"
        else:
            self._pulse_on = False
            color = "#555555"
        self.activity_dot.create_oval(2, 2, 12, 12, fill=color, outline=color)

    def _magnifier_image(self) -> tk.PhotoImage:
        """A drawn glass, so the Wanted column does not depend on a Unicode font."""
        size = 16
        image = tk.PhotoImage(master=self, width=size, height=size)
        image.put("#1c1c1c", to=(0, 0, size, size))
        glass = "#d5d5d5"
        for y in range(size):
            for x in range(size):
                distance = (x - 6) ** 2 + (y - 6) ** 2
                if 10 <= distance <= 18:
                    image.put(glass, to=(x, y, x + 1, y + 1))
        for offset in range(5):
            image.put(glass, to=(9 + offset, 9 + offset, 11 + offset, 11 + offset))
        return image

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
                bg="#1c1c1c",
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
            label.place(x=x + max(0, (width - 16) // 2), y=y + max(0, (height - 16) // 2), width=16, height=16)
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

    def _build_search(self, parent: ttk.Frame) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x")
        self.query_var = tk.StringVar()
        self.query_entry = ttk.Entry(row, textvariable=self.query_var)
        self.query_entry.pack(side="left", fill="x", expand=True)
        self.query_entry.bind("<Return>", lambda _event: self.submit_search())
        self.search_button = ttk.Button(row, text="Search", command=self.submit_search)
        self.search_button.pack(side="left", padx=(8, 0))
        hint = ttk.Label(
            parent,
            text=(
                "Uses the worker's Soulseek login. Pause stops the worker's own searches and leaves this tab running. "
                "Download all wanted uses every result unless a user is selected."
            ),
            style="Muted.TLabel",
            wraplength=760,
        )
        hint.pack(anchor="w", pady=(6, 6))
        self.searches = ttk.Notebook(parent)
        self.searches.pack(fill="both", expand=True)
        self.searches.bind("<Button-1>", self._on_search_tab_press)
        self._search_menu = tk.Menu(self, tearoff=0)
        self._search_menu.add_command(label="Download file(s)", command=self.download_selected_files)
        self._search_menu.add_command(label="Download folder", command=self.download_selected_folder)
        self._search_menu.add_command(label="Download all from this user", command=self.download_all_from_user)
        self._search_menu.add_command(label="Download all wanted from this user", command=self.download_all_wanted)
        self._search_menu.add_command(label="Browse folder", command=self.browse_selected_folder)

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

    def _build_shared(self, parent: ttk.Frame) -> None:
        top = ttk.Frame(parent)
        top.pack(fill="x")
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
        frame, tree = scrolled_tree(
            parent,
            (
                ("user", "User", 140, False),
                ("file", "File", 360, True),
                ("size", "Size", 90, False),
                ("progress", "Progress", 140, False),
                ("speed", "Speed", 90, False),
                ("status", "Status", 90, False),
                ("when", "Time", 140, False),
            ),
        )
        frame.pack(fill="both", expand=True)
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
        WantedPreview(self, matches, lambda picked, board=board: self._queue_matches(board, picked), summary=text)

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
        WantedPreview(
            self,
            plan.matches,
            lambda picked, board=board, text=text: self._queue_matches(board, picked, note=text),
            summary=text,
        )

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

    def _checkbox_image(self, state: str) -> tk.PhotoImage:
        """A drawn box, so the control does not depend on a Unicode font."""
        size = 16
        image = tk.PhotoImage(master=self, width=size, height=size)
        border = "#d5d5d5"
        if state == "off":
            fill = "#2a2a2a"
            glyph = None
        else:
            fill = "#3d8bfd"
            glyph = "#ffffff"
        image.put(border, to=(0, 0, size, size))
        image.put(fill, to=(2, 2, size - 2, size - 2))
        if state == "on":
            for x, y in (
                (3, 8), (4, 9), (5, 10), (6, 11), (7, 10), (8, 9), (9, 8), (10, 7), (11, 6), (12, 5),
                (4, 8), (5, 9), (6, 10), (7, 9), (8, 8), (9, 7), (10, 6), (11, 5),
            ):
                image.put(glyph, to=(x, y, x + 1, y + 1))
        elif state == "mixed":
            image.put(glyph, to=(4, 7, 12, 10))
        return image

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
            messagebox.showinfo("Seekarr", message, parent=self)
            return
        if kind == "error":
            self.notice_var.set(status.error or "Could not check for updates.")
            messagebox.showerror("Seekarr", status.error or "Could not check for updates.", parent=self)
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
                dest = folder / (status.asset_name or "Seekarr-Setup.exe")
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
        messagebox.showerror("Seekarr", message, parent=self)

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
            messagebox.showerror("Seekarr", f"Could not start the installer: {exc}", parent=self)
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
    root.title("Seekarr setup")
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
    main()
