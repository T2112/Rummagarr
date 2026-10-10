from __future__ import annotations

import asyncio
import time

import pytest

from desktop_app import CHECKED, UNCHECKED, DesktopApp, SettingsForm, format_status_line
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
    assert desktop.wanted.set(_row(desktop, kind="album", album="Hungover")[0], "mark") == UNCHECKED
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
    assert desktop.wanted.set(_row(desktop, kind="song", song="Cowboy")[0], "mark") == CHECKED


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


def test_status_line_matches_the_web_page_fields():
    text = format_status_line(
        {
            "run_state": "paused",
            "logged_in": True,
            "job": {"phase": "downloading album", "artist": "Ella Langley", "title": "Hungover"},
        }
    )
    assert text == "Paused · Soulseek connected · downloading album · Ella Langley — Hungover"
