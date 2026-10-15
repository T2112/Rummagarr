from __future__ import annotations

import asyncio
import threading
import time
import tkinter as tk
from pathlib import Path

import pytest

from dataclasses import replace

from activity import Step
from config_loader import app_version, default_config, try_load_config
from desktop_app import (
    DesktopApp,
    LIGHT_MODE_HINT,
    PageHost,
    SettingsForm,
    format_status_line,
    settings_apply_message,
)
from sharing import WantedMatch
from updater import UpdateStatus
from status_ui import RECENT_LIMITS
from tests.conftest import album, hole, make_worker
from wanted import VIEW_ALBUMS, VIEW_ARTISTS, VIEW_SONGS, build_catalog


def _tk_or_skip():
    import tkinter as tk

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")
    root.destroy()


@pytest.fixture
def desktop(tmp_path):
    _tk_or_skip()
    worker = make_worker(tmp_path)
    items = build_catalog(
        [
            album(1, "Ella Langley", "Hungover", files=0, total=12),
            album(2, "Ella Langley", "Still", files=2, total=10),
            album(3, "Chris Stapleton", "Traveller", files=0, total=14, artist_id=2),
        ],
        [
            hole(21, 2, "Ella Langley", "Still", "Cowboy"),
            hole(31, 3, "Chris Stapleton", "Traveller", "Tennessee Whiskey"),
        ],
    )
    worker.catalog.replace(items)
    worker.store.remember_wanted_keys([item.key for item in items])
    for index in range(30):
        worker.store.add_completed_download("Ella Langley", "Hungover", f"{index:02d} Blue.mp3", "peer", "album")
    app = DesktopApp(worker, tmp_path / "config.toml", start_status_server=False, honor_start_on_launch=False, poll_ms=60_000)
    app.update()
    yield app
    app.destroy()
    worker.close()


def _row(app, **match):
    for iid, item in app._row_items.items():
        if all(getattr(item, field) == value for field, value in match.items()):
            return iid, item
    raise AssertionError(f"no wanted row matching {match}")


def test_auto_import_checkbox_persists(desktop):
    desktop.auto_import_var.set(1)
    desktop.on_auto_import()
    assert desktop.worker.cfg.auto_import_after_artist is True
    assert try_load_config(desktop.config_path).auto_import_after_artist is True
    desktop.auto_import_var.set(0)
    desktop.on_auto_import()
    assert try_load_config(desktop.config_path).auto_import_after_artist is False


def test_window_shows_version_and_update_actions(desktop):
    assert desktop.title().startswith("Rummagarr")
    assert app_version() in desktop.title()
    assert desktop.auto_import.cget("text") == "Auto-import to Lidarr after each artist"
    assert not desktop.auto_import_var.get()
    assert desktop.help_menu.entrycget(0, "label") == "Check for updates"
    assert desktop.help_menu.entrycget(1, "label") == "About"
    status = UpdateStatus(
        current="1.4.0",
        latest="1.5.0",
        notes="Stops cleanly before the installer runs.",
        newer=True,
        can_install=False,
        page_url="https://example.test/releases/v1.5.0",
    )
    desktop._show_update_prompt(status)
    desktop.update()
    prompt = desktop._update_prompt
    assert prompt is not None
    labels = [child.cget("text") for child in _buttons(prompt)]
    assert "Open release page" in labels
    assert "Later" in labels
    assert "Skip this version" in labels
    next(button for button in _buttons(prompt) if button.cget("text") == "Skip this version").invoke()
    desktop.update()
    assert desktop.worker.store.get_meta("skipped_update_version") == "1.5.0"
    assert desktop._update_prompt is None


def _buttons(widget):
    from tkinter import ttk

    found = []
    for child in widget.winfo_children():
        if isinstance(child, ttk.Button):
            found.append(child)
        found.extend(_buttons(child))
    return found


def test_sidebar_replaces_the_tab_strip(desktop):
    assert isinstance(desktop.notebook, PageHost)
    labels = [item._label.cget("text") for item in desktop._nav_items.values()]
    assert labels == ["Status", "Wanted", "Dashboard", "Stats", "Health", "Search", "Browse", "Messages", "Shared", "Settings"]
    assert desktop.notebook.select() == str(desktop.status_tab)
    assert desktop.status_tab.winfo_ismapped()
    desktop.notebook.select(desktop.wanted_tab)
    desktop.update_idletasks()
    assert desktop.notebook.select() == str(desktop.wanted_tab)
    assert desktop.wanted_tab.winfo_ismapped()
    assert not desktop.status_tab.winfo_ismapped()
    desktop.notebook.select(desktop.settings_tab)
    desktop.update()
    form = desktop._settings_form
    assert form is not None
    assert form.heading == "Settings"
    assert form.theme_var.get() == "dark"
    assert _label_named(form, LIGHT_MODE_HINT)
    desktop.notebook.select(desktop.dashboard_tab)
    desktop.update()
    assert "Missing albums" in desktop.dashboard_text.get("1.0", "end")
    desktop.notebook.select(desktop.health_tab)
    desktop.update()
    assert desktop.health_tree.winfo_ismapped()


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def _contrast_ratio(foreground: str, background: str) -> float:
    def channel(pair: str) -> float:
        value = int(pair, 16) / 255
        if value <= 0.04045:
            return value / 12.92
        return ((value + 0.055) / 1.055) ** 2.4

    def luminance(color: str) -> float:
        color = color.strip().lstrip("#")
        red, green, blue = channel(color[0:2]), channel(color[2:4]), channel(color[4:6])
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_search_order_text_is_high_contrast_in_both_themes(desktop):
    from tkinter import ttk

    from desktop_app import THEMES, apply_theme

    desktop.notebook.select(desktop.settings_tab)
    desktop.notebook.select(desktop.wanted_tab)
    desktop.update()
    labels = [
        widget
        for widget in _walk(desktop)
        if isinstance(widget, ttk.Label) and widget.cget("text") in {"Search order", "Search direction"}
    ]
    boxes = [
        widget
        for widget in _walk(desktop)
        if isinstance(widget, ttk.Combobox)
        and (
            "Ascending" in widget.cget("values")
            or "Lidarr order" in widget.cget("values")
        )
    ]
    assert len(labels) >= 4
    assert len(boxes) >= 4
    assert any(widget.cget("text") == "Search direction" for widget in labels)
    style = ttk.Style(desktop)
    for name in ("dark", "light"):
        apply_theme(desktop, name)
        desktop.update()
        palette = THEMES[name]
        for state in ((), ("readonly",), ("readonly", "focus")):
            field = style.lookup("TCombobox", "fieldbackground", state).lower()
            ink = style.lookup("TCombobox", "foreground", state).lower()
            assert field == palette.field
            assert ink == palette.fg
            assert _contrast_ratio(ink, field) >= 7
        assert style.lookup("TCombobox", "selectforeground", ("readonly",)).lower() == palette.fg
        assert style.lookup("TCombobox", "selectbackground", ("readonly",)).lower() == palette.field
        assert style.lookup("TCombobox", "arrowcolor", ("readonly",)).lower() == palette.fg
        assert style.lookup("TCombobox", "font") == style.lookup("TLabel", "font")
        for label in labels:
            assert "Muted" not in str(label.cget("style"))
            assert style.lookup("TLabel", "foreground").lower() == palette.fg
            assert _contrast_ratio(palette.fg, palette.main) >= 7
        for box in boxes:
            assert str(box.cget("state")) == "readonly"


def test_theme_toggle_applies_live_and_persists(desktop):
    from tkinter import ttk

    desktop.notebook.select(desktop.settings_tab)
    desktop.update()
    form = desktop._settings_form
    assert form is not None
    form.theme_var.set("light")
    form._on_theme_choice()
    desktop.update()
    assert desktop._theme_name == "light"
    assert desktop.worker.cfg.theme == "light"
    loaded = try_load_config(desktop.config_path)
    assert loaded.theme == "light"
    assert ttk.Style(desktop).lookup("TFrame", "background").lower() == "#e8eef3"
    form.theme_var.set("dark")
    form._on_theme_choice()
    desktop.update()
    assert desktop._theme_name == "dark"
    assert try_load_config(desktop.config_path).theme == "dark"
    assert ttk.Style(desktop).lookup("PageTitle.TLabel", "background").lower() == "#171b21"


def test_icon_file_covers_16_through_256():
    import struct

    path = Path(__file__).resolve().parents[1] / "packaging" / "rummagarr.ico"
    data = path.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert reserved == 0 and kind == 1
    widths = []
    for index in range(count):
        width = data[6 + index * 16]
        widths.append(256 if width == 0 else width)
    assert widths == [16, 24, 32, 48, 64, 128, 256]
    png = Path(__file__).resolve().parents[1] / "packaging" / "rummagarr.png"
    assert png.is_file() and png.read_bytes().startswith(b"\x89PNG")


def test_status_tab_shows_history_choices_and_controls(desktop):
    desktop.worker.control.start()
    desktop.worker.set_job("downloading album", "Ella Langley", "Hungover")
    desktop.refresh_status()
    assert "Running" in desktop.status_line.cget("text")
    assert "Hungover" in desktop.status_line.cget("text")
    assert list(desktop.limit_box.cget("values")) == [str(number) for number in RECENT_LIMITS]
    assert len(desktop.recent.get_children()) == 10
    desktop.limit_var.set("25")
    desktop.on_limit()
    assert len(desktop.recent.get_children()) == 25
    assert desktop.worker.store.get_meta("gui_recent_limit") == "25"
    assert "disabled" not in desktop.pause_btn.state()
    desktop.on_pause()
    assert desktop.worker.control.is_paused()
    assert desktop.pause_btn.cget("text") == "Resume"
    assert desktop.worker.job_info["title"] == "Hungover"
    desktop.on_pause()
    assert desktop.worker.control.is_running()
    desktop.worker._activate("album:1")
    desktop.on_skip()
    assert asyncio.run(desktop.worker.control.gate("album:1")) == "skip"
    desktop.on_stop()
    assert desktop.worker.control.is_stopped()
    desktop.refresh_status()
    assert "disabled" in desktop.pause_btn.state()


def test_wanted_checkboxes_views_and_select_all(desktop):
    kinds = {desktop.wanted.set(iid, "kind") for iid in desktop.wanted.get_children()}
    assert kinds == {"Artist", "Album", "Song"}
    _iid, hungover = _row(desktop, kind="album", album="Hungover")
    desktop.toggle_row(_iid)
    assert desktop.worker.store.wanted_enabled(hungover.key) is False
    assert desktop.checkbox_state(_row(desktop, kind="album", album="Hungover")[0]) == "off"
    desktop.view_var.set(VIEW_ALBUMS)
    desktop.on_view()
    assert {desktop.wanted.set(iid, "kind") for iid in desktop.wanted.get_children()} == {"Album"}
    desktop.select_all_var.set(0)
    desktop.on_select_all()
    assert desktop.worker.store.wanted_enabled("album:1") is False
    assert desktop.worker.store.wanted_enabled("album:3") is False
    assert desktop.worker.store.wanted_enabled("song:21") is True
    desktop.view_var.set(VIEW_ARTISTS)
    desktop.on_view()
    artist_iid, _artist = _row(desktop, kind="artist", artist="Chris Stapleton")
    desktop.toggle_row(artist_iid)
    assert desktop.worker.store.wanted_enabled("album:3") is True
    assert desktop.worker.store.wanted_enabled("song:31") is True
    desktop.toggle_row(_row(desktop, kind="artist", artist="Chris Stapleton")[0])
    assert desktop.worker.store.wanted_enabled("album:3") is False
    assert desktop.worker.store.wanted_enabled("song:31") is False
    desktop.view_var.set(VIEW_SONGS)
    desktop.on_view()
    assert all(desktop.wanted.set(iid, "kind") == "Song" for iid in desktop.wanted.get_children())
    desktop.select_all_var.set(1)
    desktop.on_select_all()
    assert desktop.worker.store.wanted_enabled("song:21") is True
    assert desktop.worker.store.wanted_enabled("song:31") is True
    assert desktop.checkbox_state(_row(desktop, kind="song", song="Cowboy")[0]) == "on"


def test_start_pause_stop_drive_the_worker_thread(desktop):
    worker = desktop.worker
    worker.store.set_meta("missing_page", "6")

    async def fake_run(*, autostart: bool = True):
        while not worker.control.is_stopped():
            await worker.control.gate(sleep=0.05)

    worker.run_forever = fake_run
    desktop.on_start()
    for _ in range(50):
        if worker.control.is_running():
            break
        time.sleep(0.02)
    assert worker.control.is_running()
    desktop.on_pause()
    time.sleep(0.1)
    assert worker.control.is_paused()
    assert worker.store.get_meta("missing_page") == "6"
    desktop.on_pause()
    assert worker.control.is_running()
    desktop.on_stop()
    for _ in range(50):
        if worker.control.is_stopped() and desktop._thread and not desktop._thread.is_alive():
            break
        time.sleep(0.02)
    assert worker.control.is_stopped()
    assert worker.store.get_meta("missing_page") == "6"


def test_settings_form_writes_a_loadable_config(tmp_path):
    _tk_or_skip()
    import tkinter as tk

    root = tk.Tk()
    path = tmp_path / "config.toml"
    saved = {}
    form = SettingsForm(root, path, None, lambda cfg: saved.setdefault("cfg", cfg), root.destroy)
    form.url_var.set("http://127.0.0.1:8686")
    form.api_var.set("secret-key")
    form.user_var.set("souluser")
    form.password_var.set('p@ss"word')
    form.download_var.set(str(tmp_path / "in"))
    form.complete_var.set(str(tmp_path / "out"))
    form.scan_var.set(r"D:\Media\Incoming")
    form.launch_var.set(True)
    assert form.save() is True
    assert saved["cfg"].slsk_password == 'p@ss"word'
    assert saved["cfg"].start_on_launch is True
    root.destroy()


def test_behavior_tab_saves_ranges_and_restore_defaults(tmp_path):
    _tk_or_skip()
    import tkinter as tk

    from tests.conftest import make_worker

    root = tk.Tk()
    worker = make_worker(tmp_path)
    path = tmp_path / "config.toml"
    saved = {}
    form = SettingsForm(
        root,
        path,
        worker.cfg,
        lambda cfg: saved.update(cfg=cfg),
        root.destroy,
        store=worker.store,
    )
    form.fields["concurrent_albums"].set("4")
    form.fields["artist_cooldown_days"].set("3")
    form.fields["preferred_extensions"].set("mp3, flac")
    form.updates_var.set(False)
    form.history_var.set("50")
    assert form.save() is True
    assert saved["cfg"].concurrent_albums == 4
    assert saved["cfg"].artist_cooldown_days == 3.0
    assert saved["cfg"].preferred_extensions == ["mp3", "flac"]
    assert saved["cfg"].check_updates is False
    assert saved["cfg"].slsk_password == "soulpass"
    assert worker.store.get_meta("gui_recent_limit") == "50"
    loaded = path.read_text(encoding="utf-8")
    assert "concurrent_albums = 4" in loaded
    assert "check_updates = false" in loaded

    form.fields["concurrent_albums"].set("0")
    assert form.save() is False
    assert "Albums at once" in form.message_var.get()
    assert "concurrent_albums = 4" in path.read_text(encoding="utf-8")

    form.restore_defaults()
    assert form.fields["concurrent_albums"].get() == "3"
    assert form.api_var.get() == "test-key"
    assert form.fields["state_db"].get() == str(worker.cfg.state_db)
    assert form.history_var.get() == "10"
    assert form.save() is True
    assert saved["cfg"].concurrent_albums == 3
    assert saved["cfg"].check_updates is True
    root.destroy()


def _widgets(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _widgets(child)


def _label_named(form, text: str):
    import tkinter.ttk as ttk

    matches = [widget for widget in _widgets(form) if isinstance(widget, ttk.Label) and widget.cget("text") == text]
    assert matches, text
    return matches[0]


def test_wait_before_next_artist_is_visible_and_the_behavior_tab_scrolls(tmp_path):
    _tk_or_skip()
    import tkinter as tk
    import tkinter.ttk as ttk

    from desktop_app import BEHAVIOR_FIELDS

    keys = [spec.key for spec in BEHAVIOR_FIELDS]
    assert keys.index("cycle_seconds") == keys.index("artist_cooldown_days") + 1
    assert keys.index("cycle_seconds") < 4

    root = tk.Tk()
    root.geometry("760x640")
    worker = make_worker(tmp_path)
    path = tmp_path / "config.toml"
    saved = {}
    form = SettingsForm(
        root,
        path,
        worker.cfg,
        lambda cfg: saved.update(cfg=cfg),
        root.destroy,
        store=worker.store,
    )
    form.pack(fill="both", expand=True)
    form.notebook.select(1)
    root.update_idletasks()
    root.update()

    canvas = form._behavior_canvas
    wait = _label_named(form, "Wait before next artist")
    retry = _label_named(form, "Artist retry period")
    assert retry.winfo_rooty() < wait.winfo_rooty()
    top = wait.winfo_rooty() - canvas.winfo_rooty()
    assert canvas.winfo_height() > 100
    assert 0 <= top < canvas.winfo_height()

    form.fields["cycle_seconds"].set("0")
    assert form.save() is True
    assert saved["cfg"].cycle_seconds == 0

    hidden = _label_named(form, "Albums at once")
    assert hidden.winfo_ismapped() == 0
    form.advanced_var.set(True)
    form._toggle_advanced()
    root.update()
    assert hidden.winfo_ismapped() == 1

    bbox = canvas.bbox("all")
    assert bbox is not None and bbox[3] > canvas.winfo_height()
    canvas.yview_moveto(0)
    root.update()
    entry = next(
        widget
        for widget in _widgets(form)
        if isinstance(widget, ttk.Entry) and str(widget.cget("textvariable")) == str(form.fields["cycle_seconds"])
    )
    entry.event_generate("<MouseWheel>", delta=-120, x=4, y=4)
    root.update()
    assert canvas.yview()[0] > 0
    entry.event_generate("<Button-4>", x=4, y=4)
    root.update()
    assert canvas.yview()[0] == 0
    root.destroy()


def test_wait_field_is_part_of_the_pyinstaller_script():
    root = Path(__file__).resolve().parents[1]
    spec = (root / "packaging" / "lidarr_soulseek.spec").read_text(encoding="utf-8")
    assert "desktop_app.py" in spec
    source = (root / "desktop_app.py").read_text(encoding="utf-8")
    assert '"cycle_seconds"' in source
    assert '"Wait before next artist"' in source


def test_settings_apply_message_says_when_a_restart_is_required():
    old = default_config(lidarr_api_key="key", slsk_username="user", slsk_password="pass")
    live = replace(old, concurrent_albums=4, artist_cooldown_days=2)
    live_text = settings_apply_message(old, live)
    assert "next pass" in live_text
    assert "Stop and start" not in live_text
    restarted = replace(old, status_port=8790, lidarr_url="http://127.0.0.1:8686")
    assert "Stop and start" in settings_apply_message(old, restarted)
    launch = replace(old, check_updates=False)
    assert "next time the app opens" in settings_apply_message(old, launch)


def test_wanted_list_draws_only_the_visible_rows(desktop):
    from tests.conftest import album
    from wanted import build_catalog

    albums = [album(index, f"Artist {index:04d}", f"Album {index}", artist_id=index) for index in range(1, 401)]
    items = build_catalog(albums, [])
    desktop.worker.catalog.replace(items)
    desktop.worker.store.remember_wanted_keys([item.key for item in items])
    desktop._choices_cache = None
    desktop._wanted_offset = 0
    desktop.refresh_wanted()
    desktop.update()
    shown = desktop.wanted.get_children()
    assert len(items) == 800
    assert 0 < len(shown) < 80
    first = desktop.wanted.set(shown[0], "artist")
    desktop._wanted_scroll("moveto", 1)
    desktop.update()
    shown = desktop.wanted.get_children()
    assert desktop.wanted.set(shown[-1], "artist") != first
    assert len(shown) < 80


def _click_cell(app, iid: str, column: str) -> None:
    app.notebook.select(app.wanted_tab)
    app.update_idletasks()
    box = app.wanted.bbox(iid, column)
    assert box and box[2] > 0 and box[3] > 0, box
    x, y, width, height = box
    app.wanted.event_generate("<Button-1>", x=x + width // 2, y=y + height // 2)
    app.update()


def test_checkbox_click_toggles_during_sync(tmp_path):
    _tk_or_skip()
    from tests.test_scale import PageLibrary
    from wanted import sync_wanted_catalog

    worker = make_worker(tmp_path)
    library = PageLibrary(4)
    sync_wanted_catalog(library, worker.store, max_pages=5)
    app = DesktopApp(worker, tmp_path / "config.toml", start_status_server=False, honor_start_on_launch=False, poll_ms=60_000)
    try:
        app.notebook.select(app.wanted_tab)
        app.update()
        album_iid, album = _row(app, kind="album", album="Album 1")
        artist_iid, _artist = _row(app, kind="artist", artist="Artist 00000")
        assert app.checkbox_state(album_iid) == "on"
        assert app.checkbox_state(artist_iid) == "on"

        _click_cell(app, album_iid, "#0")
        assert worker.store.wanted_enabled("album:1") is False
        artist_iid, _artist = _row(app, kind="artist", artist="Artist 00000")
        assert app.checkbox_state(artist_iid) == "mixed"

        _click_cell(app, artist_iid, "#0")
        assert worker.store.wanted_enabled("album:1") is True
        assert worker.store.wanted_enabled("album:2") is True
        artist_iid, _artist = _row(app, kind="artist", artist="Artist 00000")
        assert app.checkbox_state(artist_iid) == "on"
        _click_cell(app, artist_iid, "#0")
        assert worker.store.wanted_enabled("album:1") is False
        assert worker.store.wanted_enabled("song:1") is False
        artist_iid, _artist = _row(app, kind="artist", artist="Artist 00000")
        assert app.checkbox_state(artist_iid) == "off"

        album_iid, album = _row(app, kind="album", album="Album 2")
        _click_cell(app, album_iid, "album")
        assert worker.store.wanted_enabled("album:2") is False
        assert album_iid in app.wanted.selection()

        app.wanted.focus_set()
        app.wanted.event_generate("<space>")
        app.update()
        assert worker.store.wanted_enabled("album:2") is True

        app._syncing = True
        stop = threading.Event()

        def keep_syncing() -> None:
            while not stop.is_set():
                sync_wanted_catalog(PageLibrary(4), worker.store, max_pages=5)
                if stop.is_set():
                    return
                try:
                    app.after(0, lambda: None if stop.is_set() else app._on_sync_progress("Syncing rows…"))
                except (RuntimeError, tk.TclError):
                    return
                time.sleep(0.02)

        thread = threading.Thread(target=keep_syncing, daemon=True)
        thread.start()
        try:
            time.sleep(0.05)
            album_iid, _album = _row(app, kind="album", album="Album 2")
            _click_cell(app, album_iid, "#0")
            assert worker.store.wanted_enabled("album:2") is False
            time.sleep(0.15)
            app.update()
            assert worker.store.wanted_enabled("album:2") is False
            album_iid, _album = _row(app, kind="album", album="Album 2")
            assert app.checkbox_state(album_iid) == "off"
        finally:
            stop.set()
            thread.join(timeout=5)
        app._syncing = False
        app.refresh_wanted()
        app.update()
        assert worker.store.wanted_enabled("album:2") is False
        assert app.checkbox_state(_row(app, kind="album", album="Album 2")[0]) == "off"
        assert worker.store.wanted_enabled("album:1") is False
    finally:
        app.destroy()
        worker.close()


def test_status_line_matches_the_web_page_fields():
    text = format_status_line(
        {
            "run_state": "paused",
            "logged_in": True,
            "activity": "Downloading 3 of 12 · Ella Langley – Hungover",
            "elapsed": 8,
            "busy": False,
        }
    )
    assert text == "Paused · Soulseek connected · Downloading 3 of 12 · Ella Langley – Hungover · 8s"


def test_status_tab_shows_a_search_instead_of_idle(desktop):
    desktop.worker.control.start()
    desktop.worker.set_step(
        Step(
            kind="searching",
            artist="Ella Langley",
            album="Hungover",
            query_index=2,
            query_total=3,
            result_count=47,
        )
    )
    desktop.refresh_status()
    text = desktop.status_line.cget("text")
    assert "Searching Soulseek" in text
    assert "query 2 of 3" in text
    assert "47 results" in text
    assert "Idle" not in text
    desktop.worker.set_step(Step(kind="idle"))
    desktop.refresh_status()
    assert "Idle – nothing selected" in desktop.status_line.cget("text")


def test_empty_search_settles_instead_of_staying_on_searching(desktop):
    board = desktop._add_search_board("3 doors down", None)
    desktop.searches.select(board.frame)
    board.running = True
    board.status.set("Searching…")
    desktop.update()
    request = type("Request", (), {"results": []})()
    desktop._finish_search(board, request, None)
    desktop.update()
    text = board.status.get()
    assert board.running is False
    assert "Searching" not in text
    assert "0 peer replies" in text
    assert "not logged in" in text

    desktop._search_failed(board, TimeoutError())
    desktop.update()
    failed = board.status.get()
    assert "Searching" not in failed
    assert "was not sent" in failed


def test_magnifier_opens_a_folded_search(desktop):
    from search_model import SearchFile

    desktop.notebook.select(desktop.wanted_tab)
    desktop.update()
    album_iid, _album = _row(desktop, kind="album", album="Hungover")
    _click_cell(desktop, album_iid, "find")
    assert desktop.notebook.select() == str(desktop.search_tab)
    assert desktop.query_var.get() == "ella langley hungover"
    board = desktop._current_board()
    assert board is not None
    assert board.link is not None
    assert board.link.album_id == 1
    assert board.link.key == "album:1"
    song_iid, _song = _row(desktop, kind="song", song="Cowboy")
    desktop.search_wanted_row(song_iid)
    assert desktop.query_var.get() == "ella langley cowboy"
    song_board = desktop._current_board()
    assert song_board is not None and song_board.link is not None
    assert song_board.link.track_id == 21
    desktop.show_search_files(
        song_board,
        [
            SearchFile(
                username="vinylqueen",
                filename=r"music\Cowboy.flac",
                folder="music",
                size=20_000_000,
                bitrate=900,
                duration=180,
                extension="flac",
                free_slot=True,
                speed=200_000,
            )
        ],
    )
    desktop.update()
    assert song_board.tree is not None
    labels = [song_board.tree.item(iid, "text") for iid in song_board.tree.get_children()]
    assert any("vinylqueen" in label for label in labels)
    file_iid = next(iter(song_board.row_files))
    assert "Cowboy.flac" in song_board.tree.item(file_iid, "text")
    assert song_board.tree.set(file_iid, "format") == "FLAC"


def test_lists_fill_the_window_in_both_themes(desktop):
    from desktop_app import apply_theme

    desktop.notebook.select(desktop.wanted_tab)
    for theme in ("dark", "light"):
        apply_theme(desktop, theme)
        for height in (900, 640):
            desktop.geometry(f"1200x{height}")
            desktop.update_idletasks()
            desktop.update()
            tree = desktop.wanted
            assert tree.winfo_height() >= tree.master.winfo_height() - 8
            assert desktop._wanted_window + 1 >= desktop._rows_for_height(tree.winfo_height())
    desktop.geometry("1200x900")
    desktop.notebook.select(desktop.shared_tab)
    desktop.update()
    assert desktop.shared_tree.winfo_height() >= desktop.shared_tree.master.winfo_height() - 8
    desktop.notebook.select(desktop.status_tab)
    desktop.update()
    assert desktop.transfers.winfo_height() >= desktop.transfers.master.winfo_height() - 8
    assert desktop.recent.winfo_height() >= desktop.recent.master.winfo_height() - 8
    desktop.notebook.select(desktop.search_tab)
    board = desktop._add_search_board("Ella Langley", None)
    desktop.update()
    assert board.tree is not None
    assert board.tree.winfo_height() >= board.tree.master.winfo_height() - 8


def test_download_all_wanted_can_skip_the_confirmation(desktop):
    from desktop_app import WantedPreview
    from search_model import SearchFile
    from wanted import items_to_rows

    desktop.worker.store.write_wanted_page(items_to_rows(desktop.worker.catalog.items()))
    board = desktop._add_search_board("Ella Langley", None)
    desktop.searches.select(board.frame)
    desktop.show_search_files(
        board,
        [
            SearchFile(
                username="fast",
                filename=r"Ella Langley\Hungover\01 Blue.flac",
                extension="flac",
                size=1000,
                free_slot=True,
                speed=9_000,
            )
        ],
    )
    desktop.update()
    desktop.download_all_wanted()
    desktop.update()
    previews = [child for child in desktop.winfo_children() if isinstance(child, WantedPreview)]
    assert len(previews) == 1
    preview = previews[0]
    assert preview.dont_ask.get() is False
    preview.dont_ask.set(True)
    preview._queue()
    desktop.update()
    assert desktop.worker.cfg.confirm_download_all is False
    assert try_load_config(desktop.config_path).confirm_download_all is False
    assert not [child for child in desktop.winfo_children() if isinstance(child, WantedPreview)]

    desktop.download_all_wanted()
    desktop.update()
    assert not [child for child in desktop.winfo_children() if isinstance(child, WantedPreview)]
    assert "Start the worker" in board.status.get()

    desktop.notebook.select(desktop.settings_tab)
    desktop.update()
    form = desktop._settings_form
    assert form is not None
    assert form.fields["confirm_download_all"].get() is False
    form.fields["confirm_download_all"].set(True)
    assert form.save() is True
    assert try_load_config(desktop.config_path).confirm_download_all is True
    desktop.notebook.select(desktop.search_tab)
    desktop.searches.select(board.frame)
    desktop.download_all_wanted()
    desktop.update()
    again = [child for child in desktop.winfo_children() if isinstance(child, WantedPreview)]
    assert len(again) == 1
    again[0].destroy()


def test_download_all_wanted_without_a_selected_user(desktop, caplog):
    import logging

    from desktop_app import WantedPreview
    from search_model import SearchFile
    from wanted import items_to_rows

    desktop.worker.store.write_wanted_page(items_to_rows(desktop.worker.catalog.items()))
    board = desktop._add_search_board("Ella Langley", None)
    desktop.searches.select(board.frame)
    desktop.show_search_files(
        board,
        [
            SearchFile(username="slow", filename=r"Ella Langley\Hungover\01 Blue.flac", extension="flac", size=1000, speed=100),
            SearchFile(username="slow", filename=r"Ella Langley\Hungover\02 Red.flac", extension="flac", size=1000, speed=100),
            SearchFile(
                username="fast",
                filename=r"Ella Langley\Hungover\01 Blue.flac",
                extension="flac",
                size=1000,
                free_slot=True,
                speed=9_000,
            ),
            SearchFile(
                username="fast",
                filename=r"Ella Langley\Still\01 Cowboy.flac",
                extension="flac",
                size=1000,
                free_slot=True,
                speed=9_000,
            ),
            SearchFile(
                username="other",
                filename=r"Chris Stapleton\Traveller\01 Tennessee Whiskey.flac",
                extension="flac",
                size=1000,
                free_slot=True,
                speed=9_000,
            ),
            SearchFile(username="noise", filename=r"Noise\not wanted.mp3", extension="mp3", bitrate=128, size=10),
        ],
    )
    desktop.update()
    with caplog.at_level(logging.INFO, logger="lidarr_slsk.desktop"):
        desktop.download_all_wanted()
    desktop.update()
    summary = "2 wanted items matched from 2 users, and 0 wanted items not found."
    assert board.status.get() == summary
    assert summary in caplog.text
    previews = [child for child in desktop.winfo_children() if isinstance(child, WantedPreview)]
    assert len(previews) == 1
    queued = {(match.username, match.song or match.album) for match in previews[0].matches}
    assert ("slow", "Hungover") in queued
    assert ("fast", "Cowboy") in queued
    assert all(match.artist == "Ella Langley" for match in previews[0].matches)
    previews[0].destroy()
    desktop.update()

    noise = next(iid for iid, name in board.row_users.items() if name == "noise")
    board.tree.selection_set(noise)
    desktop.download_all_wanted()
    desktop.update()
    assert board.status.get() == "No wanted matches in noise's results."
    assert not [child for child in desktop.winfo_children() if isinstance(child, WantedPreview)]

    slow = next(iid for iid, name in board.row_users.items() if name == "slow")
    board.tree.selection_set(slow)
    desktop.download_all_wanted()
    desktop.update()
    assert board.status.get() == "1 wanted item matched from slow."
    selected = [child for child in desktop.winfo_children() if isinstance(child, WantedPreview)]
    assert len(selected) == 1
    assert {match.username for match in selected[0].matches} == {"slow"}
    assert all(not match.song for match in selected[0].matches)
    selected[0].destroy()


def test_closed_window_does_not_stall_another_thread(tmp_path):
    """Closing the window must not leave Tk objects for a worker to collect.

    Each one waits a second for a main loop. Enough of them make the parallel
    Lidarr lookup take about 89 seconds even though the requests overlap.
    """
    import gc
    import threading
    import time

    _tk_or_skip()
    gc.disable()
    try:
        worker = make_worker(tmp_path)
        items = build_catalog(
            [album(1, "Ella Langley", "Hungover", files=0, total=4)],
            [hole(21, 1, "Ella Langley", "Hungover", "Blue")],
        )
        worker.catalog.replace(items)
        app = DesktopApp(
            worker,
            tmp_path / "config.toml",
            start_status_server=False,
            honor_start_on_launch=False,
            poll_ms=60_000,
        )
        app.update()
        app.destroy()
        worker.close()
        del app, worker, items
        spent: dict[str, float] = {}

        def collect() -> None:
            started = time.perf_counter()
            gc.collect()
            spent["seconds"] = time.perf_counter() - started

        thread = threading.Thread(target=collect)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert spent["seconds"] < 1
    finally:
        gc.enable()
        gc.collect()


def test_browse_page_opens_the_saved_copy_one_folder_at_a_time(desktop):
    desktop.notebook.select(desktop.browse_tab)
    desktop.update()
    assert desktop.browse_tree.winfo_ismapped()
    desktop._browse_cache().put(
        "goodpeer",
        {
            "description": "hello",
            "status": 2,
            "has_slots_free": True,
            "upload_slots": 2,
            "queue_length": 1,
            "upload_permissions": 1,
            "shared_file_count": 3,
            "shared_folder_count": 1,
            "avg_speed": 10,
        },
        [
            {
                "name": r"Music\Ella Langley\Hungover",
                "files": [{"filename": "01 Blue.flac", "size": 2048, "extension": "flac"}],
            }
        ],
    )
    desktop.browse_user_var.set("goodpeer")
    desktop.load_browse(False)
    desktop.update()
    assert "Online" in desktop.browse_profile.get("1.0", "end")
    roots = desktop.browse_tree.get_children()
    assert [desktop.browse_tree.item(item, "text") for item in roots] == ["Music"]
    desktop.browse_tree.focus(roots[0])
    desktop._browse_open()
    children = [desktop.browse_tree.item(item, "text") for item in desktop.browse_tree.get_children(roots[0])]
    assert children == ["Ella Langley"]
    assert "Saved copy" in desktop.browse_status.get()


def test_about_donate_is_a_placeholder_and_setup_check_opens(desktop):
    desktop.show_about()
    desktop.update()
    window = desktop._about_window
    assert window is not None
    donate = next(button for button in _buttons(window) if button.cget("text") == "Donate")
    donate.invoke()
    desktop.update()
    texts = []

    def walk(widget):
        for child in widget.winfo_children():
            try:
                texts.append(child.cget("text"))
            except tk.TclError:
                pass
            walk(child)

    walk(window)
    assert "A donate link is not set yet." in texts
    window.destroy()

    desktop.show_setup_check()
    desktop.update()
    assert "Setup looks ready." in desktop._setup_text.get("1.0", "end")
    desktop._setup_window.destroy()


def test_stats_page_and_browse_limits_popup(desktop):
    desktop.notebook.select(desktop.stats_tab)
    desktop.update()
    assert "30 finished" in desktop.stats_text.get("1.0", "end")
    desktop._browse_payload = {
        "username": "goodpeer",
        "profile": {
            "description": "only 2 files. one album at a time. wait 30 seconds between downloads.",
            "upload_permissions": 1,
            "queue_length": 0,
        },
        "directories": [{"name": "Music", "files": [{"filename": "01 Blue.flac", "size": 100, "extension": "flac"}]}],
    }
    desktop.browse_user_var.set("goodpeer")
    desktop.worker.match_user_files = lambda files: [
        WantedMatch(
            username="goodpeer",
            filename="01 Blue.flac",
            size=100,
            artist="Ella Langley",
            album="Hungover",
            song="Blue",
            key="song:1",
            kind="song",
            album_id=1,
            track_id=1,
            score=1,
        )
    ]
    desktop.worker.cfg = replace(
        desktop.worker.cfg,
        max_queued_files_per_user=3,
        browse_max_files=25,
        browse_max_mb=2048,
        browse_daily_files=20,
    )
    desktop.download_browse_missing()
    desktop.update()
    window = desktop._limits_window
    text = desktop._limits_text.get("1.0", "end")
    assert "2 files" in text
    assert "30 seconds" in text
    assert "1 file at a time" in text
    assert desktop._limits_files.get() == "25"
    assert desktop._limits_mb.get() == "2048"
    assert desktop._limits_daily.get() == "20"
    assert any(button.cget("text") == "Download" for button in _buttons(window))
    assert any(button.cget("text") == "Save limits" for button in _buttons(window))
    desktop._browse_payload["profile"]["description"] = (
        "max 100 files. one album at a time. wait 30 seconds between downloads."
    )
    desktop.download_browse_missing()
    desktop.update()
    text = desktop._limits_text.get("1.0", "end")
    assert "25 files" in text
    assert "3 files" not in text
    desktop._limits_files.set("4")
    desktop._limits_mb.set("100")
    desktop._limits_daily.set("6")
    next(button for button in _buttons(desktop._limits_window) if button.cget("text") == "Save limits").invoke()
    desktop.update()
    assert desktop.worker.cfg.browse_max_files == 4
    assert desktop.worker.cfg.browse_max_mb == 100
    assert desktop.worker.cfg.browse_daily_files == 6
    loaded = try_load_config(desktop.config_path)
    assert loaded.browse_max_files == 4
    assert loaded.browse_max_mb == 100
    assert loaded.browse_daily_files == 6
    assert "4 files" in desktop._limits_text.get("1.0", "end")
    desktop.worker.store.add_user_day("goodpeer", 6)
    next(button for button in _buttons(desktop._limits_window) if button.cget("text") == "Save limits").invoke()
    desktop.update()
    assert "already used" in desktop._limits_text.get("1.0", "end")
    assert not any(button.cget("text") == "Download" for button in _buttons(desktop._limits_window))
    desktop._limits_window.destroy()

    desktop._browse_payload["profile"]["upload_permissions"] = 0
    desktop.download_browse_missing()
    desktop.update()
    assert "Uploads are off" in desktop._limits_text.get("1.0", "end")
    assert not any(button.cget("text") == "Download" for button in _buttons(desktop._limits_window))
    desktop._limits_window.destroy()


def test_favorite_suggestion_is_manual(desktop):
    desktop.notebook.select(desktop.status_tab)
    desktop.update()
    notice = " ".join(_widget_text(desktop.notices))
    assert "Add peer to favorites?" in notice
    assert "peer" not in desktop.worker.cfg.priority_users
    next(button for button in _buttons(desktop.notices) if button.cget("text") == "Add").invoke()
    desktop.update()
    assert "peer" in desktop.worker.cfg.priority_users
    assert "peer" in try_load_config(desktop.config_path).priority_users
    assert "Add peer to favorites?" not in " ".join(_widget_text(desktop.notices))

    for index in range(3):
        desktop.worker.store.add_completed_download("Ella Langley", "Hungover", f"nope-{index}.mp3", "nope", "album")
    desktop.refresh_status()
    desktop.update()
    assert "Add nope to favorites?" in " ".join(_widget_text(desktop.notices))
    assert "nope" not in desktop.worker.cfg.priority_users
    next(button for button in _buttons(desktop.notices) if button.cget("text") == "Not now").invoke()
    desktop.update()
    assert "nope" not in desktop.worker.cfg.priority_users
    desktop.refresh_status()
    desktop.update()
    assert "nope" not in " ".join(_widget_text(desktop.notices)).lower()


def _widget_text(widget) -> list[str]:
    found = []
    for child in widget.winfo_children():
        try:
            found.append(str(child.cget("text")))
        except tk.TclError:
            pass
        found.extend(_widget_text(child))
    return found


def test_private_messages_badge_and_compose_box(desktop):
    desktop.worker.store.add_private_message("peer", "hello", "in", soulseek_id=1)
    desktop.worker.store.add_private_message("peer", "again", "in", soulseek_id=2)
    desktop.refresh_status()
    desktop.update()
    labels = [item._label.cget("text") for item in desktop._nav_items.values()]
    assert labels[0] == "Status"
    assert "Messages (2)" in labels
    desktop.notebook.select(desktop.messages_tab)
    desktop.update()
    page = " ".join(_widget_text(desktop.messages_tab))
    assert "Chat rooms stay off" in page
    assert "Join room" not in page
    desktop.message_threads.selection_set(0)
    desktop._on_message_thread()
    desktop.update()
    history = desktop.message_history.get("1.0", "end")
    assert "hello" in history
    assert "again" in history
    assert desktop._nav_items[str(desktop.status_tab)]._label.cget("text") == "Status"
    assert desktop._nav_items[str(desktop.messages_tab)]._label.cget("text") == "Messages"
    desktop.message_compose_var.set("   ")
    desktop._send_composed_message()
    assert "compose box" in desktop.message_status.get()
    assert all(row["direction"] == "in" for row in desktop.worker.store.private_history("peer"))
    desktop.notebook.select(desktop.settings_tab)
    desktop.update()
    assert desktop._settings_form.fields["crash_bundle"].get() is False
