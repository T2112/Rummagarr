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
from dataclasses import dataclass, fields as dataclass_fields, replace
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

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
from wanted import (
    VIEW_EVERYTHING,
    VIEWS,
    WantedItem,
    catalog_from_lidarr,
    children_by_artist,
    choice_updates,
    filter_view,
    item_checked,
    select_all_state,
    select_all_updates,
    sort_items,
)
from worker import Worker

log = logging.getLogger("lidarr_slsk.desktop")

CHECKED = "☑"
UNCHECKED = "☐"
DONE_TRANSFER = {"COMPLETE", "FAILED", "ABORTED"}
# 2,000 pages × 250 albums. Sync can be cancelled before it finishes.
WANTED_SYNC_PAGES = 2000
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
    style.configure("TNotebook.Tab", background="#1c1c1c", foreground=fg, padding=(16, 8))
    style.map("TNotebook.Tab", background=[("selected", "#2c2c2c")], foreground=[("selected", "#ffffff")])
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


def format_status_line(snapshot: dict) -> str:
    labels = {"running": "Running", "paused": "Paused", "stopped": "Stopped"}
    state = labels.get(snapshot.get("run_state") or "stopped", "Stopped")
    job = snapshot.get("job") or {}
    parts = [
        state,
        "Soulseek connected" if snapshot.get("logged_in") else "Soulseek offline",
        job.get("phase") or "idle",
    ]
    who = " — ".join(part for part in ((job.get("artist") or ""), (job.get("title") or "")) if part)
    if who:
        parts.append(who)
    return " · ".join(parts)


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
    BehaviorField("concurrent_albums", "Albums at once", "int", "", "How many albums to search or download together. 1 is one after another.", 1, 8),
    BehaviorField("search_wait_seconds", "Search wait", "float", "seconds", "How long to wait for Soulseek results before scoring them.", 1, 90),
    BehaviorField("max_albums_per_cycle", "Albums per pass", "int", "", "Album searches started in one pass.", 1, 20),
    BehaviorField("focus_one_artist", "One artist at a time", "bool", "", "Stay on a single artist, then move on."),
    BehaviorField("artist_cooldown_days", "Artist retry period", "float", "days", "After a visit, skip that artist for this many days. 0 retries every pass.", 0, 90),
    BehaviorField("max_missing_pages", "Wanted pages per pass", "int", "pages", "Lidarr wanted/missing pages read each pass. Each page is 250 albums.", 1, 200),
    BehaviorField("fill_missing_tracks", "Fill missing songs", "bool", "", "Search Soulseek for individual songs on albums Lidarr already has in part."),
    BehaviorField("max_tracks_per_cycle", "Songs per pass", "int", "", "Individual song searches started in one pass. 0 turns song fill off for that pass.", 0, 50),
    BehaviorField("cycle_seconds", "Pause between passes", "int", "seconds", "How long to wait after a pass before the next one.", 15, 7200),
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
    BehaviorField("share_dir", "Folder to share", "path", "", "Optional library folder Soulseek may share. Leave empty to share nothing. Restart to apply.", restore=False),
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
        "share_dir",
        "state_db",
        "log_file",
        "status_bind",
        "status_port",
    }
)
_LAUNCH_FIELDS = frozenset({"start_on_launch", "check_updates"})


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
    if changed - _RESTART_FIELDS - _LAUNCH_FIELDS:
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
    ) -> None:
        super().__init__(master, padding=16)
        self.config_path = config_path
        self.base = cfg
        self.on_saved = on_saved
        self.on_cancel = on_cancel
        self.store = store
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
        self._build()

    def _variable(self, spec: BehaviorField, value) -> tk.Variable:
        if spec.kind == "bool":
            return tk.BooleanVar(value=bool(value))
        if spec.kind == "csv":
            if spec.key == "ignored_users":
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
            if spec.key == "ignored_users":
                variable.set(", ".join(sorted(value)))
            else:
                variable.set(", ".join(value))
            return
        if spec.kind in {"int", "float"}:
            variable.set(_display_number(value))
            return
        variable.set("" if value is None else str(value))

    def _build(self) -> None:
        ttk.Label(self, text="Lidarr Soulseek setup", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            self,
            text="Connection holds the login and folders. Behavior holds the search, conversion, and update options.",
            style="Muted.TLabel",
            wraplength=900,
        ).grid(row=1, column=0, sticky="w", pady=(4, 8))
        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=2, column=0, sticky="nsew")
        connection = ttk.Frame(self.notebook, padding=12)
        behavior = ttk.Frame(self.notebook, padding=(4, 8, 4, 4))
        self.notebook.add(connection, text="Connection")
        self.notebook.add(behavior, text="Behavior")
        self._build_connection(connection)
        self._build_behavior(behavior)
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
            text="Search limits, conversion, the status page, and the update check are on the Behavior tab.",
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
        canvas.bind("<Enter>", self._wheel_on)
        canvas.bind("<Leave>", self._wheel_off)
        canvas.bind("<Destroy>", self._wheel_off)
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

    def _wheel_on(self, _event) -> None:
        canvas = self._behavior_canvas
        if canvas is None:
            return
        canvas.bind_all("<Button-4>", self._wheel)
        canvas.bind_all("<Button-5>", self._wheel)
        canvas.bind_all("<MouseWheel>", self._wheel)

    def _wheel_off(self, _event) -> None:
        canvas = self._behavior_canvas
        if canvas is None:
            return
        for sequence in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
            canvas.unbind_all(sequence)

    def _wheel(self, event) -> None:
        canvas = self._behavior_canvas
        if canvas is None:
            return
        if getattr(event, "num", None) == 5 or getattr(event, "delta", 0) < 0:
            canvas.yview_scroll(1, "units")
        else:
            canvas.yview_scroll(-1, "units")

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
        for spec in BEHAVIOR_FIELDS:
            if not spec.restore:
                continue
            self._set_field(spec, getattr(fresh, spec.key))
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
            elif spec.key == "ignored_users":
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
        self.title("Update Lidarr Soulseek")
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
        self._choices_cache: dict[str, bool] | None = None
        self._choice_job = 0
        self._transfer_index: dict[str, int] = {}
        self.title(f"Lidarr Soulseek {app_version()}")
        self.geometry("1100x720")
        self.minsize(860, 560)
        apply_theme(self)
        saved_limit = worker.store.get_meta("gui_recent_limit")
        self.limit_var = tk.StringVar(value=str(normalize_recent_limit(saved_limit or DEFAULT_RECENT_LIMIT)))
        self.view_var = tk.StringVar(value=VIEW_EVERYTHING)
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

    def _build(self) -> None:
        self._build_menu()
        header = ttk.Frame(self, padding=(16, 12, 16, 0))
        header.pack(fill="x")
        ttk.Label(header, text="Lidarr Soulseek", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text=app_version(), style="Muted.TLabel").pack(side="left", padx=(10, 0), pady=(4, 0))
        ttk.Button(header, text="Settings", command=self.open_settings).pack(side="right")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=12)
        self.status_tab = ttk.Frame(self.notebook, padding=12)
        self.wanted_tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.status_tab, text="Status")
        self.notebook.add(self.wanted_tab, text="Wanted")
        self._build_status(self.status_tab)
        self._build_wanted(self.wanted_tab)

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
            "About Lidarr Soulseek",
            f"Lidarr Soulseek {app_version()}\n\n"
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

        self.status_line = ttk.Label(parent, text="Stopped · Soulseek offline · idle", style="Muted.TLabel")
        self.status_line.pack(fill="x", pady=(10, 0))
        self.notice_line = ttk.Label(parent, textvariable=self.notice_var, style="Muted.TLabel")
        self.notice_line.pack(fill="x", pady=(2, 0))

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
            text="Checked rows are the only ones the worker searches. New rows from Sync start checked.",
            style="Muted.TLabel",
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
        self.count_label = ttk.Label(parent, textvariable=self.wanted_status, style="Muted.TLabel")
        self.count_label.pack(anchor="w", pady=(0, 6))
        frame = ttk.Frame(parent)
        columns = (
            ("mark", "", 46, False),
            ("kind", "Kind", 80, False),
            ("artist", "Artist", 180, True),
            ("album", "Album", 220, True),
            ("song", "Song", 200, True),
            ("detail", "Detail", 180, True),
        )
        names = [column[0] for column in columns]
        self.wanted = ttk.Treeview(frame, columns=names, show="headings", selectmode="browse")
        self.wanted_scroll = ttk.Scrollbar(frame, orient="vertical", command=self._wanted_scroll)
        self.wanted.grid(row=0, column=0, sticky="nsew")
        self.wanted_scroll.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        for name, heading, width, stretch in columns:
            if name == "mark":
                self.wanted.heading(name, text=heading)
            else:
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

    def _tick(self) -> None:
        if self._shutting_down:
            return
        try:
            if not self.winfo_exists():
                return
            self.refresh_status()
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
        window.title("Lidarr Soulseek settings")
        window.geometry("980x820")
        window.minsize(760, 640)
        window.transient(self)
        apply_theme(window)
        previous = self.worker.cfg

        def on_saved(cfg: Config) -> None:
            self.worker.cfg = cfg
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

    def refresh_status(self) -> None:
        limit = normalize_recent_limit(self.limit_var.get())
        snapshot = self.worker.ui_snapshot(recent_limit=limit)
        self.status_line.configure(text=format_status_line(snapshot))
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

        def progress(done: int, total: int) -> None:
            nonlocal last_paint
            now = time.monotonic()
            if total and done < total and now - last_paint < 0.15:
                return
            last_paint = now
            if total <= 0:
                text = f"Loading albums {done:,}…"
            else:
                text = f"Syncing {done:,} / {total:,}"
            self._post(lambda text=text: self.wanted_status.set(text))

        def work() -> None:
            client = LidarrClient(cfg.lidarr_url, cfg.lidarr_api_key)
            try:
                items, exhausted = catalog_from_lidarr(
                    client,
                    max_pages=WANTED_SYNC_PAGES,
                    on_progress=progress,
                    should_cancel=cancel.is_set,
                )
                if cancel.is_set():
                    self._post(lambda: self._sync_finished(None, [], True, cancelled=True))
                    return
                self.worker.store.remember_wanted_keys([item.key for item in items])
                self.worker.catalog.replace(items)
                choices = self.worker.store.wanted_choices()
                self._post(lambda: self._sync_finished(None, items, exhausted, choices=choices))
            except Exception as exc:
                self._post(lambda exc=exc: self._sync_finished(exc, [], True))
            finally:
                client.close()

        threading.Thread(target=work, name="wanted-sync", daemon=True).start()

    def _post(self, func) -> None:
        try:
            self.after(0, func)
        except tk.TclError:
            pass

    def _sync_finished(
        self,
        error: Exception | None,
        items: list[WantedItem],
        exhausted: bool,
        *,
        cancelled: bool = False,
        choices: dict[str, bool] | None = None,
    ) -> None:
        self._syncing = False
        try:
            self.sync_button.configure(text="Sync")
        except tk.TclError:
            return
        if cancelled:
            self.wanted_status.set("Sync cancelled.")
            return
        if error is not None:
            self.wanted_status.set(f"Sync failed: {error}")
            return
        if choices is not None:
            self._choices_cache = choices
        else:
            self._choices_cache = None
        self._wanted_offset = 0
        self.refresh_wanted()
        more = "" if exhausted else " Lidarr still has more pages; Sync again to continue, or raise the page cap."
        self.wanted_status.set(f"Synced {len(items):,} rows from Lidarr.{more}")

    def on_select_all(self) -> None:
        if self._updating_select_all:
            return
        items = self.worker.catalog.items()
        visible = self._ordered_rows(items)
        enabled = bool(self.select_all_var.get())
        self._apply_choices(select_all_updates(visible, items, enabled))

    def on_wanted_click(self, event) -> None:
        if self.wanted.identify_region(event.y) not in {"cell", "tree"}:
            return
        row = self.wanted.identify_row(event.y)
        column = self.wanted.identify_column(event.x)
        if not row or column != "#1":
            return
        self.toggle_row(row)
        return "break"

    def on_wanted_space(self, _event) -> None:
        selected = self.wanted.selection()
        if selected:
            self.toggle_row(selected[0])

    def toggle_row(self, iid: str) -> None:
        item = self._row_items.get(iid)
        if item is None:
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
        if self._wanted_rows:
            self._paint_wanted(self._choices(), self._wanted_grouped)

    def _wanted_scroll(self, *args) -> None:
        total = len(self._wanted_rows)
        window = max(1, self._wanted_window)
        if total <= window:
            return
        if args[0] == "moveto":
            self._wanted_offset = int(float(args[1]) * total)
        elif args[0] == "scroll":
            step = int(args[1])
            if len(args) > 2 and args[2] == "pages":
                step *= window
            self._wanted_offset += step
        self._wanted_offset = max(0, min(self._wanted_offset, total - window))
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
        if event.keysym == "Down" and index == len(children) - 1 and self._wanted_offset + len(children) < len(self._wanted_rows):
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

    def refresh_wanted(self) -> None:
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
        children = self.wanted.get_children()
        if not shown:
            self.wanted.delete(*children)
            if not self.worker.catalog.items():
                text = "Click Sync to load the wanted list from Lidarr."
            else:
                text = f"Nothing in {self.view_var.get() or VIEW_EVERYTHING}."
            self.wanted.insert("", "end", values=("", "", text, "", "", ""))
            self.wanted_scroll.set(0, 1)
            return
        if len(children) != len(shown):
            self.wanted.delete(*children)
            children = []
            for item in shown:
                children.append(self.wanted.insert("", "end", values=self._wanted_values(item, choices, grouped)))
        for iid, item in zip(children, shown):
            self.wanted.item(iid, values=self._wanted_values(item, choices, grouped))
            self._row_items[iid] = item
        if total <= window:
            self.wanted_scroll.set(0, 1)
        else:
            self.wanted_scroll.set(self._wanted_offset / total, (self._wanted_offset + len(shown)) / total)

    def _wanted_values(self, item: WantedItem, choices: dict[str, bool], grouped: dict[str, list[WantedItem]]) -> tuple:
        mark = CHECKED if item_checked(item, self._wanted_rows, choices, grouped) else UNCHECKED
        return (mark, item.kind.title(), item.artist, item.album, item.song, item.detail)


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
            messagebox.showinfo("Lidarr Soulseek", message, parent=self)
            return
        if kind == "error":
            self.notice_var.set(status.error or "Could not check for updates.")
            messagebox.showerror("Lidarr Soulseek", status.error or "Could not check for updates.", parent=self)
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
                dest = folder / (status.asset_name or "LidarrSoulseek-Setup.exe")
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
        messagebox.showerror("Lidarr Soulseek", message, parent=self)

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
            messagebox.showerror("Lidarr Soulseek", f"Could not start the installer: {exc}", parent=self)
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
    root.title("Lidarr Soulseek setup")
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
