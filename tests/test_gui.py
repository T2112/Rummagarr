from __future__ import annotations

import asyncio
import threading
import time
import tkinter as tk
from pathlib import Path

import pytest

from dataclasses import replace

from activity import Step
from config_loader import app_version, default_config
from desktop_app import (
    DesktopApp,
    SettingsForm,
    format_status_line,
    settings_apply_message,
)
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


def test_window_shows_version_and_update_actions(desktop):
    assert app_version() in desktop.title()
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
