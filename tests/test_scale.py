"""The wanted catalog lives in SQLite and stays usable at large sizes."""

from __future__ import annotations

import asyncio
import resource
import sqlite3
import time

import pytest

from state import JobStore
from tests.conftest import album, make_worker
from wanted import VIEW_ALBUMS, VIEW_ARTISTS, sync_wanted_catalog


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


class PageLibrary:
    """Album pages generated on the fly so the test does not hold every track."""

    def __init__(
        self,
        n_albums: int,
        *,
        per_album: int = 10,
        per_artist: int = 4,
        title: str = "Album",
        id_base: int = 0,
        artist_prefix: str = "Artist",
    ) -> None:
        self.n_albums = n_albums
        self.per_album = per_album
        self.per_artist = per_artist
        self.title = title
        self.id_base = id_base
        self.artist_prefix = artist_prefix
        self.calls = 0
        self.page_fetches = 0
        self.renamed: dict[int, str] = {}
        self.track_titles: dict[int, str] = {}
        self.file_overrides: dict[int, int] = {}

    def album(self, index: int) -> dict:
        album_id = self.id_base + index + 1
        artist_index = index // self.per_artist
        partial = index % 2 == 1
        files = self.file_overrides.get(album_id, 2 if partial else 0)
        title = self.renamed.get(album_id, f"{self.title} {album_id}")
        return {
            "id": album_id,
            "title": title,
            "artistId": artist_index + 1,
            "artist": {"id": artist_index + 1, "artistName": f"{self.artist_prefix} {artist_index:05d}"},
            "monitored": True,
            "releaseDate": "2020-01-01",
            "statistics": {"trackFileCount": files, "totalTrackCount": self.per_album, "trackCount": self.per_album},
        }

    def wanted_missing(self, page_size=250, start_page=1, max_pages=1, on_page=None, should_cancel=None):
        self.page_fetches += 1
        page = max(1, int(start_page or 1))
        if should_cancel is not None and should_cancel():
            return [], page, False
        start = (page - 1) * page_size
        if start >= self.n_albums:
            return [], 1, True
        end = min(start + page_size, self.n_albums)
        albums = [self.album(index) for index in range(start, end)]
        exhausted = end >= self.n_albums
        if on_page is not None:
            on_page(len(albums))
        return albums, (1 if exhausted else page + 1), exhausted

    def tracks_for_album(self, album_id: int):
        self.calls += 1
        index = int(album_id) - self.id_base - 1
        partial = index % 2 == 1
        files = self.file_overrides.get(int(album_id), 2 if partial else 0)
        base = (self.id_base + index) * self.per_album
        rows = []
        for number in range(self.per_album):
            rows.append(
                {
                    "id": base + number + 1,
                    "title": self.track_titles.get(base + number + 1, f"Track {number + 1}"),
                    "monitored": True,
                    "hasFile": partial and number < files,
                    "trackNumber": "A1" if number == 0 else number + 1,
                }
            )
        return rows


def test_old_database_keeps_choices_and_gains_catalog_tables(tmp_path):
    path = tmp_path / "state.sqlite3"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE wanted_choices (key TEXT PRIMARY KEY, enabled INTEGER NOT NULL)")
    raw.execute("INSERT INTO wanted_choices(key, enabled) VALUES ('album:5', 0)")
    raw.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    raw.commit()
    raw.close()

    store = JobStore(path)
    try:
        assert store.wanted_catalog_ready() is False
        assert store.wanted_enabled("album:5") is False
        assert store.wanted_enabled("song:9") is True
        library = PageLibrary(4)
        count, exhausted, cancelled = sync_wanted_catalog(library, store, max_pages=5)
        assert cancelled is False
        assert exhausted is True
        assert count > 4
        assert store.wanted_enabled("album:5") is False
        assert store.wanted_enabled("album:1") is True
        page, _total = store.wanted_page(
            kinds=None, search="", order="default", reverse=False, limit=20, offset=0
        )
        assert page
        assert page[0][1] == "artist"
    finally:
        store.close()


def test_sync_is_incremental_and_cancel_keeps_saved_rows(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    try:
        first = PageLibrary(4, title="Keep")
        count, exhausted, cancelled = sync_wanted_catalog(first, store, max_pages=5)
        assert exhausted and not cancelled and count > 0
        store.set_wanted_choices({"album:1": False})
        assert store.wanted_enabled("album:1") is False

        again = PageLibrary(4, title="Keep")
        again.renamed[2] = "Renamed"
        started = time.perf_counter()
        count2, exhausted, cancelled = sync_wanted_catalog(again, store, max_pages=5)
        assert time.perf_counter() - started < 5
        assert exhausted and not cancelled
        assert count2 == count
        assert store.wanted_enabled("album:1") is False
        renamed, _total = store.wanted_page(
            kinds=("album",), search="Artist 00000", order="album", reverse=False, limit=10, offset=0
        )
        assert any(row[3] == "Renamed" for row in renamed)
        calls_before = again.calls
        again.track_titles[1] = "Renamed Track"
        sync_wanted_catalog(again, store, max_pages=5)
        assert again.calls == calls_before
        songs, _total = store.wanted_page(
            kinds=("song",), search="Artist 00000", order="song", reverse=False, limit=20, offset=0
        )
        assert all(row[4] != "Renamed Track" for row in songs)
        again.file_overrides[1] = 1
        sync_wanted_catalog(again, store, max_pages=5)
        songs, _total = store.wanted_page(
            kinds=("song",), search="Artist 00000", order="song", reverse=False, limit=30, offset=0
        )
        assert any(row[4] == "Renamed Track" for row in songs)
        assert store.wanted_enabled("album:1") is False

        seen = {"pages": 0}

        def cancel_after_first(_rows, _albums):
            seen["pages"] += 1

        huge = PageLibrary(800, title="Other", id_base=50_000, artist_prefix="Other")

        def should_cancel():
            return seen["pages"] >= 1

        _count, exhausted, cancelled = sync_wanted_catalog(
            huge, store, max_pages=20, on_progress=cancel_after_first, should_cancel=should_cancel
        )
        assert cancelled is True
        assert exhausted is False
        kept, total = store.wanted_page(
            kinds=None, search="Artist 00000", order="default", reverse=False, limit=5, offset=0
        )
        assert total > 0
        assert kept
    finally:
        store.close()


def test_filters_search_select_all_and_next_artist_use_sql(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    try:
        library = PageLibrary(40)
        sync_wanted_catalog(library, store, max_pages=10)
        everything, total = store.wanted_page(
            kinds=None, search="", order="default", reverse=False, limit=30, offset=0
        )
        albums, album_total = store.wanted_page(
            kinds=("album",), search="", order="default", reverse=False, limit=10, offset=0
        )
        assert total > album_total > 0
        assert {row[1] for row in albums} == {"album"}
        found, found_total = store.wanted_page(
            kinds=None, search="artist 00003", order="default", reverse=False, limit=20, offset=0
        )
        assert found_total > 0
        assert found
        assert all("00003" in row[2] for row in found)
        summary_total, checked, state = store.wanted_summary(None, "")
        assert state == "all"
        assert checked == summary_total == total
        started = time.perf_counter()
        store.set_view_enabled(("album",), "", False)
        assert time.perf_counter() - started < 2
        assert store.wanted_enabled("album:1") is False
        assert store.wanted_enabled("song:1") is True
        _total, _checked, state = store.wanted_summary(("album",), "")
        assert state == "none"
        store.set_artist_enabled("artist 00000", False)
        assert store.wanted_enabled("song:1") is False
        assert store.next_wanted_artist() != "Artist 00000"
        focus = store.next_wanted_artist()
        assert focus
        albums_for, holes = store.artist_work(focus)
        assert albums_for or holes
        assert store.wanted_page(kinds=("artist",), search="nobody-zz", order="artist", reverse=False, limit=5, offset=0)[1] == 0
    finally:
        store.close()


def test_keyset_pages_match_offset_and_removed_albums_drop(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    try:
        count, exhausted, cancelled = sync_wanted_catalog(PageLibrary(40), store, max_pages=10)
        assert exhausted and not cancelled and count > 40
        wide, _total = store.wanted_page(
            kinds=None, search="", order="default", reverse=False, limit=15, offset=0
        )
        assert len(wide) == 15
        got, _total = store.wanted_page(
            kinds=None,
            search="",
            order="default",
            reverse=False,
            limit=5,
            offset=0,
            after=wide[9][-1],
        )
        assert [row[0] for row in got] == [row[0] for row in wide[10:15]]
        earlier, _total = store.wanted_page(
            kinds=None,
            search="",
            order="default",
            reverse=False,
            limit=5,
            offset=0,
            before=wide[10][-1],
            before_count=5,
        )
        assert [row[0] for row in earlier] == [row[0] for row in wide[5:10]]

        store.set_view_enabled(("album",), "", False)
        assert store.wanted_enabled("album:1") is False
        store.set_wanted_choices({"album:1": True})
        assert store.wanted_enabled("album:1") is True
        assert store.wanted_enabled("album:2") is False
        _total, checked, state = store.wanted_summary(("album",), "")
        assert checked == 1
        assert state == "some"

        spanning = PageLibrary(300)
        span_count, exhausted, cancelled = sync_wanted_catalog(spanning, store, max_pages=20)
        assert exhausted and not cancelled
        assert store._needs_delete is False
        quiet = PageLibrary(300)
        again, exhausted, cancelled = sync_wanted_catalog(quiet, store, max_pages=20)
        assert exhausted and not cancelled
        assert again == span_count
        assert store._needs_delete is False
        assert quiet.calls == 0

        shrunk, exhausted, cancelled = sync_wanted_catalog(PageLibrary(2), store, max_pages=20)
        assert exhausted and not cancelled
        assert shrunk < span_count
        albums, album_total = store.wanted_page(
            kinds=("album",), search="", order="default", reverse=False, limit=20, offset=0
        )
        assert album_total == 2
        assert {row[0] for row in albums} == {"album:1", "album:2"}
        assert store.wanted_enabled("album:1") is True
    finally:
        store.close()


def test_saved_catalog_reopens_without_lidarr_and_worker_queries_it(tmp_path):
    worker = make_worker(tmp_path)
    path = worker.cfg.state_db
    try:
        sync_wanted_catalog(PageLibrary(4), worker.store, max_pages=5)
        rows = worker.store.wanted_row_count()
        worker.store.set_wanted_choices({"album:1": False, "album:3": False})
        worker.control.start()

        def explode(**_kwargs):
            raise AssertionError("saved catalog must not page Lidarr")

        worker.lidarr.wanted_missing = explode
        seen_tracks: list[int] = []

        async def fake_track(row):
            seen_tracks.append(int(row["track"]["id"]))
            return False

        async def fake_album(_row):
            raise AssertionError("unchecked albums should not be searched")

        worker.process_track = fake_track
        worker.process_album = fake_album
        asyncio.run(worker.cycle())
        assert seen_tracks
        assert worker.catalog.items() == []
    finally:
        worker.close()

    started = time.perf_counter()
    store = JobStore(path)
    try:
        page, total = store.wanted_page(kinds=None, search="", order="default", reverse=False, limit=15, offset=0)
        assert time.perf_counter() - started < 0.5
        assert store.wanted_catalog_ready() is True
        assert total == rows
        assert page
    finally:
        store.close()


def test_twenty_thousand_rows_stay_within_budget(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    library = PageLibrary(2_000)
    try:
        started = time.perf_counter()
        count, exhausted, cancelled = sync_wanted_catalog(library, store, max_pages=100)
        elapsed = time.perf_counter() - started
        assert exhausted and not cancelled
        assert count > 20_000
        assert elapsed < 20
        started = time.perf_counter()
        _page, total = store.wanted_page(kinds=("song",), search="", order="song", reverse=False, limit=40, offset=0)
        assert total > 10_000
        assert time.perf_counter() - started < 0.5
        started = time.perf_counter()
        _found, found_total = store.wanted_page(
            kinds=None, search="Artist 00100", order="default", reverse=False, limit=20, offset=0
        )
        assert found_total > 0
        assert time.perf_counter() - started < 0.5
        started = time.perf_counter()
        store.set_view_enabled(None, "", False)
        assert time.perf_counter() - started < 3
        assert store.next_wanted_artist() is None
        store.set_view_enabled(None, "", True)
        assert store.next_wanted_artist() == "Artist 00000"
        assert _rss_mb() < 700
    finally:
        store.close()


def test_gui_reads_the_saved_catalog_and_can_search(tmp_path):
    pytest.importorskip("tkinter")
    import tkinter as tk

    from desktop_app import DesktopApp

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")
    root.destroy()

    worker = make_worker(tmp_path)

    class Tiny:
        def wanted_missing(self, page_size=250, start_page=1, max_pages=1, on_page=None, should_cancel=None):
            rows = [
                album(1, "Ella Langley", "Hungover", files=0, total=2),
                album(3, "Chris Stapleton", "Traveller", files=0, total=2, artist_id=2),
            ]
            return rows, 1, True

        def tracks_for_album(self, album_id: int):
            if int(album_id) == 1:
                return [
                    {"id": 11, "title": "Blue", "monitored": True, "hasFile": False, "trackNumber": "A1"},
                ]
            return [
                {"id": 31, "title": "Tennessee Whiskey", "monitored": True, "hasFile": False, "trackNumber": 1},
            ]

    sync_wanted_catalog(Tiny(), worker.store, max_pages=2)
    assert worker.catalog.items() == []
    app = DesktopApp(worker, tmp_path / "config.toml", start_status_server=False, poll_ms=60_000)
    try:
        app.update()
        kinds = {app.wanted.set(iid, "kind") for iid in app.wanted.get_children()}
        assert "Artist" in kinds
        assert len(app.wanted.get_children()) < 40
        app.search_var.set("chris")
        app.on_search()
        app.update()
        artists = {app.wanted.set(iid, "artist") for iid in app.wanted.get_children()}
        assert artists == {"Chris Stapleton"}
        album_iid = next(iid for iid in app.wanted.get_children() if app.wanted.set(iid, "kind") == "Album")
        app.toggle_row(album_iid)
        app.update()
        assert worker.store.wanted_enabled("album:3") is False
        assert app.checkbox_state(album_iid) == "off"
        app.view_var.set(VIEW_ARTISTS)
        app.on_view()
        app.update()
        assert {app.wanted.set(iid, "kind") for iid in app.wanted.get_children()} == {"Artist"}
        app.select_all_var.set(0)
        app.on_select_all()
        app.update()
        assert worker.store.wanted_enabled("song:31") is False
        app.search_var.set("")
        app.view_var.set(VIEW_ALBUMS)
        app.on_view()
        app.select_all_var.set(1)
        app.on_select_all()
        app.update()
        assert worker.store.wanted_enabled("album:1") is True
        assert worker.store.wanted_enabled("album:3") is True
        assert app.checkbox_state(app.wanted.get_children()[0]) == "on"
    finally:
        app.destroy()
        worker.close()


def test_cancelled_sync_resumes_pages_then_tracks(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    try:
        library = PageLibrary(600)
        seen = {"pages": 0}

        def on_progress(_rows, _albums):
            seen["pages"] += 1

        def should_cancel():
            return seen["pages"] >= 1

        _count, exhausted, cancelled = sync_wanted_catalog(
            library, store, max_pages=20, on_progress=on_progress, should_cancel=should_cancel
        )
        assert cancelled is True
        assert exhausted is False
        assert library.calls == 0
        assert library.page_fetches == 1
        assert store.wanted_kind_count("album") == 250

        again = PageLibrary(600)
        count, exhausted, cancelled = sync_wanted_catalog(again, store, max_pages=20)
        assert exhausted is True
        assert cancelled is False
        assert again.page_fetches == 2
        assert again.calls == 600
        assert store.wanted_kind_count("album") == 600
        assert count > 600
        assert store.track_queue_pending() is False
    finally:
        store.close()


def test_cancelled_track_fetch_resumes_without_another_page_walk(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    try:
        library = PageLibrary(40)

        def tracks_for_album(album_id: int):
            rows = PageLibrary.tracks_for_album(library, album_id)
            if library.calls >= 3:
                library.stop = True
            return rows

        library.stop = False
        library.tracks_for_album = tracks_for_album
        _count, exhausted, cancelled = sync_wanted_catalog(
            library, store, max_pages=5, should_cancel=lambda: library.stop
        )
        assert cancelled is True
        assert exhausted is False
        assert library.calls == 3
        assert library.page_fetches == 1
        assert store.wanted_kind_count("album") == 40
        songs_before = store.wanted_kind_count("song")
        assert 0 < songs_before < 40 * 10

        again = PageLibrary(40)
        count, exhausted, cancelled = sync_wanted_catalog(again, store, max_pages=5)
        assert exhausted is True
        assert cancelled is False
        assert again.page_fetches == 0
        assert again.calls == 37
        assert store.wanted_kind_count("song") > songs_before
        assert count > 40
    finally:
        store.close()


def test_failed_track_lookup_stays_queued_for_the_next_sync(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    try:
        library = PageLibrary(4)
        failed = {"album": 1}

        def tracks_for_album(album_id: int):
            if int(album_id) == failed["album"]:
                library.calls += 1
                raise RuntimeError("lidarr track lookup failed")
            return PageLibrary.tracks_for_album(library, album_id)

        library.tracks_for_album = tracks_for_album
        _count, exhausted, cancelled = sync_wanted_catalog(library, store, max_pages=5)
        assert cancelled is False
        assert exhausted is False
        assert store.track_queue_pending() is True
        assert store.wanted_kind_count("song") == 26

        failed["album"] = 0
        pages_before = library.page_fetches
        calls_before = library.calls
        _count, exhausted, cancelled = sync_wanted_catalog(library, store, max_pages=5)
        assert exhausted is True
        assert cancelled is False
        assert library.page_fetches == pages_before
        assert library.calls == calls_before + 1
        assert store.track_queue_pending() is False
        assert store.wanted_kind_count("song") == 36
    finally:
        store.close()


def test_artist_track_route_batches_albums_and_a_single_album_does_not(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    try:
        class ArtistBatch(PageLibrary):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.artist_calls = 0

            def tracks_for_artist(self, artist_id: int):
                self.artist_calls += 1
                rows = []
                for index in range(self.n_albums):
                    album = self.album(index)
                    if int(album["artistId"]) != int(artist_id):
                        continue
                    for track in PageLibrary.tracks_for_album(self, album["id"]):
                        copied = dict(track)
                        copied["albumId"] = album["id"]
                        rows.append(copied)
                self.calls -= self.per_artist
                return rows

        library = ArtistBatch(8)
        _count, exhausted, cancelled = sync_wanted_catalog(library, store, max_pages=5)
        assert exhausted and not cancelled
        assert library.artist_calls == 2
        assert library.calls == 0
        assert store.wanted_kind_count("song") > 0

        library.artist_calls = 0
        sync_wanted_catalog(library, store, max_pages=5)
        assert library.artist_calls == 0
        assert library.calls == 0

        lone = JobStore(tmp_path / "one.sqlite3")
        try:
            single = ArtistBatch(1, per_artist=1)
            sync_wanted_catalog(single, lone, max_pages=5)
            assert single.artist_calls == 0
            assert single.calls == 1
        finally:
            lone.close()

        class Unsplit(PageLibrary):
            def tracks_for_artist(self, _artist_id: int):
                return [{"id": 1, "title": "Orphan", "monitored": True, "hasFile": False}]

        fresh = JobStore(tmp_path / "unsplit.sqlite3")
        try:
            unsplittable = Unsplit(8)
            _count, exhausted, cancelled = sync_wanted_catalog(unsplittable, fresh, max_pages=5)
            assert cancelled is False
            assert exhausted is False
            assert unsplittable.calls == 0
            assert fresh.wanted_kind_count("song") == 0
            assert fresh.track_queue_pending() is True
        finally:
            fresh.close()
    finally:
        store.close()
