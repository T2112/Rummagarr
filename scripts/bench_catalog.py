"""Time the SQLite wanted catalog. Network time is not included.

Usage:
    python scripts/bench_catalog.py --albums 975612

975,612 albums is about 10 million catalog rows in the same shape as the
100,000-track check: 10 tracks an album, 4 albums an artist, every other
album partial.
"""

from __future__ import annotations

import argparse
import resource
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from state import JobStore
from wanted import sync_wanted_catalog


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


class Generated:
    def __init__(self, n_albums: int, *, per_album: int = 10, per_artist: int = 4) -> None:
        self.n_albums = n_albums
        self.per_album = per_album
        self.per_artist = per_artist
        self.renamed: dict[int, str] = {}

    def _album(self, index: int) -> dict:
        album_id = index + 1
        artist_index = index // self.per_artist
        partial = index % 2 == 1
        files = 2 if partial else 0
        return {
            "id": album_id,
            "title": self.renamed.get(album_id, f"Album {album_id}"),
            "artistId": artist_index + 1,
            "artist": {"id": artist_index + 1, "artistName": f"Artist {artist_index:05d}"},
            "monitored": True,
            "releaseDate": "2020-01-01",
            "statistics": {
                "trackFileCount": files,
                "totalTrackCount": self.per_album,
                "trackCount": self.per_album,
            },
        }

    def wanted_missing(self, page_size=250, start_page=1, max_pages=1, on_page=None, should_cancel=None):
        page = max(1, int(start_page or 1))
        if should_cancel is not None and should_cancel():
            return [], page, False
        start = (page - 1) * page_size
        if start >= self.n_albums:
            return [], 1, True
        end = min(start + page_size, self.n_albums)
        albums = [self._album(index) for index in range(start, end)]
        exhausted = end >= self.n_albums
        return albums, (1 if exhausted else page + 1), exhausted

    def tracks_for_album(self, album_id: int):
        index = int(album_id) - 1
        partial = index % 2 == 1
        files = 2 if partial else 0
        base = index * self.per_album
        return [
            {
                "id": base + number + 1,
                "title": f"Track {number + 1}",
                "monitored": True,
                "hasFile": partial and number < files,
                "trackNumber": number + 1,
            }
            for number in range(self.per_album)
        ]


def _timed(label: str, func):
    started = time.perf_counter()
    value = func()
    elapsed = time.perf_counter() - started
    print(f"{label}={elapsed:.3f}s", flush=True)
    return value, elapsed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--albums", type=int, default=975_612)
    args = parser.parse_args()
    library = Generated(args.albums)
    with tempfile.TemporaryDirectory(prefix="wanted-bench-") as directory:
        path = Path(directory) / "state.sqlite3"
        store = JobStore(path)
        print(f"albums={args.albums} rss_before_mb={rss_mb():.1f}", flush=True)
        count, _elapsed = _timed(
            "initial_sync",
            lambda: sync_wanted_catalog(library, store, max_pages=args.albums)[0],
        )
        print(f"rows={count} rss_after_sync_mb={rss_mb():.1f}", flush=True)
        store.checkpoint()
        db_bytes = store.database_bytes()
        print(f"db_bytes={db_bytes} db_mib={db_bytes / 1024 / 1024:.1f}", flush=True)
        store.close()

        def launch():
            opened = JobStore(path)
            page, total = opened.wanted_page(
                kinds=None, search="", order="default", reverse=False, limit=40, offset=0
            )
            assert page and total == count
            return opened

        store, _elapsed = _timed("launch_first_page", launch)
        _timed(
            "filter_albums",
            lambda: store.wanted_page(kinds=("album",), search="", order="default", reverse=False, limit=40, offset=0),
        )
        _timed(
            "search_artist",
            lambda: store.wanted_page(
                kinds=None, search="Artist 01000", order="default", reverse=False, limit=20, offset=0
            ),
        )
        _timed("select_all_off", lambda: store.set_view_enabled(None, "", False))
        _timed("select_all_on", lambda: store.set_view_enabled(None, "", True))
        _timed("next_artist", lambda: store.next_wanted_artist())
        paged, _elapsed = _timed(
            "page_mid_list",
            lambda: store.wanted_page(
                kinds=None, search="", order="default", reverse=False, limit=40, offset=max(0, count // 2)
            ),
        )
        cursor = paged[0][0][-1] if paged[0] else None

        def keyset():
            return store.wanted_page(
                kinds=None,
                search="",
                order="default",
                reverse=False,
                limit=40,
                offset=0,
                after=cursor,
            )

        _timed("keyset_next", keyset)
        store.close()

        def resync():
            library.renamed[1] = "Renamed Album"
            library.renamed[2] = "Renamed Other"
            again = JobStore(path)
            try:
                sync_wanted_catalog(again_client, again, max_pages=args.albums)
            finally:
                again.close()

        again_client = library
        _timed("incremental_resync", resync)
        print(f"rss_peak_mb={rss_mb():.1f}", flush=True)


if __name__ == "__main__":
    main()
