"""Timing budgets for a Lidarr wanted list of about 100,000 tracks.

Measured on this machine before the index and virtual list, same shape
(2,500 artists, 10,000 albums, 100,000 tracks, 102,500 catalog rows):

- catalog_from_lidarr: 1.32s
- filter Everything: 0.014s
- item_checked on every row: 23.2s
- select-all of 50 artists: 0.47s (about 23s for all 2,500)
- remember_wanted_keys: 0.17s
- set_wanted_choices for every album and song: 0.13s
- inserting every row into a Treeview: 0.60s
- choose_focus_artist with nothing checked: still running after 10 minutes
"""

from __future__ import annotations

import resource
import time

from wanted import (
    VIEW_ARTISTS,
    VIEW_EVERYTHING,
    catalog_from_lidarr,
    children_by_artist,
    filter_view,
    item_checked,
    select_all_updates,
    sort_items,
)

TRACKS = 100_000
PER_ALBUM = 10
ALBUMS_PER_ARTIST = 4


class _Library:
    def __init__(self) -> None:
        self.n_albums = TRACKS // PER_ALBUM
        self.albums: list[dict] = []
        self.tracks: dict[int, list[dict]] = {}
        track_id = 1
        for index in range(self.n_albums):
            artist_index = index // ALBUMS_PER_ARTIST
            album_id = index + 1
            partial = index % 2 == 1
            files = 2 if partial else 0
            self.albums.append(
                {
                    "id": album_id,
                    "title": f"Album {album_id}",
                    "artistId": artist_index + 1,
                    "artist": {"id": artist_index + 1, "artistName": f"Artist {artist_index:05d}"},
                    "monitored": True,
                    "releaseDate": "2020-01-01",
                    "statistics": {
                        "trackFileCount": files,
                        "totalTrackCount": PER_ALBUM,
                        "trackCount": PER_ALBUM,
                    },
                }
            )
            rows = []
            for number in range(PER_ALBUM):
                rows.append(
                    {
                        "id": track_id,
                        "title": f"Track {number + 1}",
                        "monitored": True,
                        "hasFile": partial and number < files,
                        "trackNumber": number + 1,
                    }
                )
                track_id += 1
            self.tracks[album_id] = rows

    def wanted_missing(self, page_size=250, start_page=1, max_pages=20, on_page=None, should_cancel=None):
        page = max(1, int(start_page or 1))
        out: list[dict] = []
        fetched = 0
        exhausted = False
        while fetched < max(1, int(max_pages or 1)):
            if should_cancel is not None and should_cancel():
                return out, page, False
            chunk = self.albums[(page - 1) * page_size : (page - 1) * page_size + page_size]
            fetched += 1
            if not chunk:
                exhausted = True
                break
            out.extend(chunk)
            if on_page is not None:
                on_page(len(out))
            if len(chunk) < page_size:
                exhausted = True
                break
            page += 1
        return out, (1 if exhausted else page), exhausted

    def tracks_for_album(self, album_id: int):
        return self.tracks[int(album_id)]


def _rss_mb() -> float:
    # Linux reports kilobytes.
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def test_hundred_thousand_tracks_stay_within_budget(tmp_path):
    library = _Library()
    progress: list[tuple[int, int]] = []
    started = time.perf_counter()
    items, exhausted = catalog_from_lidarr(
        library,
        max_pages=10_000,
        on_progress=lambda done, total: progress.append((done, total)),
    )
    sync_s = time.perf_counter() - started
    assert exhausted is True
    assert len(items) > 100_000
    assert sum(1 for item in items if item.kind == "song") >= 80_000
    assert sync_s < 10
    assert progress
    assert any(total > 0 and done < total for done, total in progress)

    started = time.perf_counter()
    everything = filter_view(items, VIEW_EVERYTHING)
    artists = filter_view(items, VIEW_ARTISTS)
    assert len(everything) == len(items)
    assert len(artists) == library.n_albums // ALBUMS_PER_ARTIST
    assert time.perf_counter() - started < 0.5

    grouped = children_by_artist(items)
    started = time.perf_counter()
    for item in items:
        item_checked(item, items, {}, grouped)
    assert time.perf_counter() - started < 1.5

    started = time.perf_counter()
    updates = select_all_updates(artists, items, False)
    assert time.perf_counter() - started < 2
    assert len(updates) > 100_000

    started = time.perf_counter()
    ordered = sort_items(items, "song")
    assert len(ordered) == len(items)
    assert time.perf_counter() - started < 2

    from state import JobStore

    store = JobStore(tmp_path / "state.sqlite3")
    try:
        started = time.perf_counter()
        store.remember_wanted_keys([item.key for item in items])
        store.set_wanted_choices(updates)
        assert time.perf_counter() - started < 2
        assert store.wanted_enabled(artists[0].key) is False
    finally:
        store.close()

    from tests.conftest import make_worker

    worker = make_worker(tmp_path)
    try:
        worker.catalog.replace(items)
        worker.store.set_wanted_choices({item.key: False for item in items if item.kind != "artist"})
        started = time.perf_counter()
        assert worker.choose_focus_artist([], []) is None
        assert time.perf_counter() - started < 2
        worker.store.set_wanted_choices({artists[-1].key: True})
        # The last artist's rows stay unchecked, so focus still finds nobody
        # until one album or song is turned back on.
        song = next(item for item in reversed(items) if item.kind == "song")
        worker.store.set_wanted_choices({song.key: True})
        started = time.perf_counter()
        focus = worker.choose_focus_artist([], [])
        assert focus == song.artist
        assert time.perf_counter() - started < 2
        albums, holes = worker.catalog.work_for_artist(focus)
        assert albums
        assert all(row["artist"]["artistName"] == focus for row in albums)
        assert len(holes) < 100
    finally:
        worker.close()

    assert _rss_mb() < 800


def test_sync_cancel_returns_no_partial_catalog():
    library = _Library()
    calls = {"tracks": 0}

    def tracks_for_album(album_id: int):
        calls["tracks"] += 1
        return library.tracks_for_album(album_id)

    library.tracks_for_album = tracks_for_album
    started = time.perf_counter()
    items, exhausted = catalog_from_lidarr(library, max_pages=10_000, should_cancel=lambda: True)
    assert items == []
    assert exhausted is False
    assert calls["tracks"] == 0
    assert time.perf_counter() - started < 2
