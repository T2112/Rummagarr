from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, ttk

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
from wanted import (
    VIEW_EVERYTHING,
    VIEWS,
    WantedItem,
    catalog_from_lidarr,
    choice_updates,
    filter_view,
    item_checked,
    select_all_state,
    select_all_updates,
)
from worker import Worker

log = logging.getLogger("lidarr_slsk.desktop")

CHECKED = "☑"
UNCHECKED = "☐"
DONE_TRANSFER = {"COMPLETE", "FAILED", "ABORTED"}


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


class SettingsForm(ttk.Frame):
    """First-run and later edits of the Lidarr and Soulseek settings."""

    def __init__(
        self,
        master: tk.Misc,
        config_path: Path,
        cfg: Config | None,
        on_saved,
        on_cancel,
    ) -> None:
        super().__init__(master, padding=16)
        self.config_path = config_path
        self.base = cfg
        self.on_saved = on_saved
        self.on_cancel = on_cancel
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
        self.message_var = tk.StringVar(value="")
        self._build()

    def _build(self) -> None:
        ttk.Label(self, text="Lidarr Soulseek setup", style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(
            self,
            text="Enter the Lidarr address and API key, then the Soulseek account this PC should use.",
            style="Muted.TLabel",
            wraplength=620,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 12))
        rows = (
            (2, "Lidarr URL", self.url_var, False),
            (3, "Lidarr API key", self.api_var, False),
            (4, "Soulseek username", self.user_var, False),
            (5, "Soulseek password", self.password_var, True),
        )
        for row, label, variable, secret in rows:
            ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", pady=4)
            entry = ttk.Entry(self, textvariable=variable, width=52, show="*" if secret else "")
            entry.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        self._path_row(6, "Incomplete folder", self.download_var)
        self._path_row(7, "Complete folder", self.complete_var)
        self._path_row(8, "Lidarr scan path", self.scan_var)
        ttk.Label(self, text="Listen port").grid(row=9, column=0, sticky="w", pady=4)
        ttk.Entry(self, textvariable=self.listen_var, width=10).grid(row=9, column=1, sticky="w", pady=4)
        ttk.Label(self, text="Obfuscated port").grid(row=10, column=0, sticky="w", pady=4)
        ttk.Entry(self, textvariable=self.obfs_var, width=10).grid(row=10, column=1, sticky="w", pady=4)
        ttk.Checkbutton(self, text="Try to open the listen ports with UPnP", variable=self.upnp_var).grid(
            row=11, column=0, columnspan=3, sticky="w", pady=(8, 2)
        )
        ttk.Checkbutton(self, text="Start downloading when the app opens", variable=self.launch_var).grid(
            row=12, column=0, columnspan=3, sticky="w", pady=2
        )
        ttk.Label(self, textvariable=self.message_var, wraplength=620).grid(
            row=13, column=0, columnspan=3, sticky="w", pady=(8, 4)
        )
        buttons = ttk.Frame(self)
        buttons.grid(row=14, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(buttons, text="Test Lidarr", command=self.test_lidarr).pack(side="left")
        ttk.Button(buttons, text="Save", style="Start.TButton", command=self.save).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Cancel", command=self.on_cancel).pack(side="left", padx=(8, 0))
        self.columnconfigure(1, weight=1)
        ttk.Label(
            self,
            text="API key: Lidarr → Settings → General → Security. Folders are created if they are missing.",
            style="Muted.TLabel",
            wraplength=620,
        ).grid(row=15, column=0, columnspan=3, sticky="w", pady=(12, 0))

    def _path_row(self, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(self, textvariable=variable, width=52).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(self, text="Browse", command=lambda var=variable: self._browse(var)).grid(row=row, column=2, padx=(8, 0))

    def _browse(self, variable: tk.StringVar) -> None:
        chosen = filedialog.askdirectory(initialdir=variable.get() or str(Path.home()))
        if chosen:
            variable.set(chosen)

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
            raise ValueError("Listen port and obfuscated port must be numbers.") from exc
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
        self.message_var.set("Saved.")
        if self.on_saved is not None:
            self.on_saved(loaded)
        return True


class DesktopApp(tk.Tk):
    def __init__(
        self,
        worker: Worker,
        config_path: Path,
        *,
        start_status_server: bool = False,
        honor_start_on_launch: bool = False,
        poll_ms: int = 2000,
    ) -> None:
        super().__init__()
        self.worker = worker
        self.config_path = config_path
        self.poll_ms = poll_ms
        self._run_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._syncing = False
        self._updating_select_all = False
        self._row_items: dict[str, WantedItem] = {}
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

    def _build(self) -> None:
        header = ttk.Frame(self, padding=(16, 12, 16, 0))
        header.pack(fill="x")
        ttk.Label(header, text="Lidarr Soulseek", style="Title.TLabel").pack(side="left")
        ttk.Button(header, text="Settings", command=self.open_settings).pack(side="right")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=12)
        self.status_tab = ttk.Frame(self.notebook, padding=12)
        self.wanted_tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.status_tab, text="Status")
        self.notebook.add(self.wanted_tab, text="Wanted")
        self._build_status(self.status_tab)
        self._build_wanted(self.wanted_tab)

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
        frame, self.wanted = scrolled_tree(
            parent,
            (
                ("mark", "", 46, False),
                ("kind", "Kind", 80, False),
                ("artist", "Artist", 180, True),
                ("album", "Album", 220, True),
                ("song", "Song", 200, True),
                ("detail", "Detail", 180, True),
            ),
        )
        frame.pack(fill="both", expand=True)
        self.wanted.bind("<Button-1>", self.on_wanted_click)
        self.wanted.bind("<space>", self.on_wanted_space)

    def _tick(self) -> None:
        try:
            if not self.winfo_exists():
                return
            self.refresh_status()
        except tk.TclError:
            return
        self.after(self.poll_ms, self._tick)

    def _on_close(self) -> None:
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
        window.geometry("720x700")
        window.transient(self)
        apply_theme(window)

        def on_saved(cfg: Config) -> None:
            self.worker.cfg = cfg
            self.notice_var.set("Saved. Stop and start the worker to apply connection changes.")
            window.destroy()

        form = SettingsForm(window, self.config_path, self.worker.cfg, on_saved, window.destroy)
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
        self.refresh_wanted()

    def on_sync(self) -> None:
        if self._syncing:
            return
        self._syncing = True
        self.sync_button.state(["disabled"])
        self.wanted_status.set("Syncing from Lidarr…")
        cfg = self.worker.cfg

        def work() -> None:
            client = LidarrClient(cfg.lidarr_url, cfg.lidarr_api_key)
            try:
                def progress(index: int, total: int) -> None:
                    self._post(lambda i=index, n=total: self.wanted_status.set(f"Syncing songs {i + 1} of {n}…"))

                items, exhausted = catalog_from_lidarr(client, max_pages=100, on_progress=progress)
                self._post(lambda: self._sync_finished(None, items, exhausted))
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

    def _sync_finished(self, error: Exception | None, items: list[WantedItem], exhausted: bool) -> None:
        self._syncing = False
        try:
            self.sync_button.state(["!disabled"])
        except tk.TclError:
            return
        if error is not None:
            self.wanted_status.set(f"Sync failed: {error}")
            return
        self.worker.store.remember_wanted_keys([item.key for item in items])
        self.worker.catalog.replace(items)
        self.refresh_wanted()
        more = "" if exhausted else " Lidarr still has more pages; this pass kept the first 100."
        self.wanted_status.set(f"Synced {len(items)} rows from Lidarr. {self.wanted_status.get()}{more}")

    def on_select_all(self) -> None:
        if self._updating_select_all:
            return
        items = self.worker.catalog.items()
        visible = filter_view(items, self.view_var.get() or VIEW_EVERYTHING)
        enabled = bool(self.select_all_var.get())
        self.worker.store.set_wanted_choices(select_all_updates(visible, items, enabled))
        self.refresh_wanted()

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
        enabled = not item_checked(item, items, self.worker.store.wanted_choices())
        self.worker.store.set_wanted_choices(choice_updates(item, enabled, items))
        self.refresh_wanted()

    def refresh_wanted(self) -> None:
        items = self.worker.catalog.items()
        view = self.view_var.get() or VIEW_EVERYTHING
        visible = filter_view(items, view)
        choices = self.worker.store.wanted_choices()
        yview = self.wanted.yview()
        self.wanted.delete(*self.wanted.get_children())
        self._row_items.clear()
        if not visible:
            if not items:
                self.wanted.insert("", "end", values=("", "", "Click Sync to load the wanted list from Lidarr.", "", "", ""))
            else:
                self.wanted.insert("", "end", values=("", "", f"Nothing in {view}.", "", "", ""))
        else:
            for item in visible:
                mark = CHECKED if item_checked(item, items, choices) else UNCHECKED
                iid = self.wanted.insert(
                    "",
                    "end",
                    values=(mark, item.kind.title(), item.artist, item.album, item.song, item.detail),
                )
                self._row_items[iid] = item
        state = select_all_state(visible, items, choices)
        checked = sum(1 for item in visible if item_checked(item, items, choices))
        self._updating_select_all = True
        try:
            self.select_all_var.set(1 if state == "all" and visible else 0)
        finally:
            self._updating_select_all = False
        if items and not self._syncing:
            self.wanted_status.set(f"{checked} of {len(visible)} selected in this view.")
        if yview and visible:
            self.wanted.yview_moveto(yview[0])


def run_setup(config_path: Path) -> Config | None:
    root = tk.Tk()
    root.title("Lidarr Soulseek setup")
    root.geometry("740x700")
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
    )
    app.mainloop()


if __name__ == "__main__":
    main()
