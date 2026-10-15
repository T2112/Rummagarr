"""Search order picks the next artist and arranges the Wanted list."""

from __future__ import annotations

from dataclasses import replace

from config_loader import save_config, try_load_config
from search_order import album_sort_key, default_direction
from tests.conftest import album, hole, make_config, make_worker
from wanted import album_key, build_catalog, items_to_rows


def _library(worker, albums, holes=None):
    catalog = build_catalog(albums, holes or [])
    worker.store.write_wanted_page(items_to_rows(catalog))
    return worker


def _names(worker, order, direction):
    worker.cfg = replace(worker.cfg, search_order=order, search_order_direction=direction)
    found = []
    skip: set[str] = set()
    while True:
        name = worker.store.next_ready_artist(
            skip,
            tag=worker.cfg.wanted_tag,
            order=order,
            direction=direction,
        )
        if not name:
            return found
        found.append(name)
        skip.add(name.casefold())


def _page(worker, order, direction):
    worker.store.refresh_display_order(order, direction)
    rows, _total = worker.store.wanted_page(
        kinds=None, search="", order="search", reverse=False, limit=50, offset=0
    )
    return [(kind, artist, album) for _key, kind, artist, album, _song, _detail, _state, _cursor in rows]


def test_missing_tracks_ascending_finishes_the_small_gap_first(tmp_path):
    worker = make_worker(tmp_path)
    try:
        tiny = album(1, "Tiny", "One", files=0, total=2)
        huge = album(2, "Huge", "Lots", files=0, total=600)
        _library(worker, [huge, tiny])
        assert _names(worker, "missing_tracks", "asc") == ["Tiny", "Huge"]
        assert _names(worker, "missing_tracks", "desc") == ["Huge", "Tiny"]
    finally:
        worker.close()


def test_name_tag_cooldown_and_least_recently_tried(tmp_path):
    worker = make_worker(tmp_path)
    try:
        kept = album(1, "Kept", "One", files=0, total=4)
        kept["tags"] = ["country"]
        other = album(2, "Other", "Two", files=0, total=4)
        _library(worker, [other, kept])
        assert _names(worker, "name", "asc") == ["Kept", "Other"]
        assert _names(worker, "name", "desc") == ["Other", "Kept"]

        worker.cfg = replace(worker.cfg, wanted_tag="country", search_order="name", search_order_direction="asc")
        assert worker.store.next_ready_artist(tag="country", order="name", direction="asc") == "Kept"
        assert worker._next_ready_artist() == "Kept"

        worker.cfg = replace(worker.cfg, wanted_tag="")
        worker.store.set_artist_cooldown("Kept", 2)
        assert worker._next_ready_artist() == "Other"

        worker.store.set_meta("artist_cooldowns", "{}")
        worker.store.upsert(1, "Kept", "One", "failed", "missed")
        worker.store.upsert(2, "Other", "Two", "failed", "missed")
        worker.store._db.execute(
            "UPDATE jobs SET updated_at = ? WHERE album_id = ?",
            ("2020-01-01T00:00:00+00:00", 1),
        )
        worker.store._db.execute(
            "UPDATE jobs SET updated_at = ? WHERE album_id = ?",
            ("2024-06-01T00:00:00+00:00", 2),
        )
        worker.store._db.commit()
        fresh = album(3, "Fresh", "New", files=0, total=4)
        _library(worker, [kept, other, fresh])
        assert _names(worker, "tried", "asc") == ["Fresh", "Kept", "Other"]
        assert _names(worker, "tried", "desc") == ["Other", "Kept", "Fresh"]
    finally:
        worker.close()


def test_release_date_and_recently_added(tmp_path):
    worker = make_worker(tmp_path)
    try:
        early = album(1, "Early", "Old", files=0, total=4)
        early["releaseDate"] = "1999-01-01"
        early["added"] = "2020-01-01T00:00:00Z"
        late = album(2, "Late", "New", files=0, total=4)
        late["releaseDate"] = "2024-08-01"
        late["added"] = "2024-09-01T00:00:00Z"
        early["monitored"] = False
        _library(worker, [late, early])
        assert _names(worker, "release", "asc") == ["Early", "Late"]
        assert _names(worker, "release", "desc") == ["Late", "Early"]
        assert _names(worker, "added", "desc") == ["Late", "Early"]
        assert _names(worker, "added", "asc") == ["Early", "Late"]
        assert _names(worker, "lidarr", "desc") == ["Late", "Early"]
    finally:
        worker.close()


def test_wanted_list_puts_the_smallest_album_and_its_songs_first(tmp_path):
    worker = make_worker(tmp_path)
    try:
        small = album(1, "Middle", "B Side", files=8, total=10)
        large = album(2, "Middle", "A Side", files=0, total=40)
        tiny = album(3, "Tiny", "One", files=0, total=2)
        huge = album(4, "Huge", "Lots", files=0, total=600)
        songs = [hole(11, 1, "Middle", "B Side", "Last")]
        _library(worker, [huge, large, tiny, small], songs)
        page = _page(worker, "missing_tracks", "asc")
        artists = [artist for kind, artist, _album in page if kind == "artist"]
        assert artists == ["Tiny", "Middle", "Huge"]
        middle = [(kind, album) for kind, artist, album in page if artist == "Middle"]
        assert middle == [
            ("artist", ""),
            ("album", "B Side"),
            ("song", "B Side"),
            ("album", "A Side"),
        ]
        assert _page(worker, "missing_tracks", "desc")[0][1] == "Huge"
    finally:
        worker.close()


def test_unchecking_an_album_drops_it_from_the_missing_count(tmp_path):
    worker = make_worker(tmp_path)
    try:
        little = album(1, "Wide", "Little", files=0, total=2)
        rest = album(2, "Wide", "Rest", files=0, total=80)
        medium = album(3, "Medium", "Ten", files=0, total=10)
        _library(worker, [little, rest, medium])
        assert _names(worker, "missing_tracks", "asc") == ["Medium", "Wide"]
        worker.store.set_wanted_choices({album_key(2): False})
        assert _names(worker, "missing_tracks", "asc") == ["Wide", "Medium"]
    finally:
        worker.close()


def test_albums_inside_one_artist_follow_the_gap(tmp_path):
    small = album(1, "Middle", "B Side", files=9, total=10)
    large = album(2, "Middle", "A Side", files=0, total=40)
    ordered = sorted([large, small], key=lambda row: album_sort_key(row, "missing_tracks", "asc"))
    assert [row["title"] for row in ordered] == ["B Side", "A Side"]
    assert default_direction("missing_tracks") == "asc"
    assert default_direction("lidarr") == "desc"
    assert default_direction("added") == "desc"


def test_search_order_round_trips_through_config(tmp_path):
    path = tmp_path / "config.toml"
    cfg = replace(make_config(tmp_path), search_order="missing_albums", search_order_direction="asc")
    save_config(path, cfg)
    loaded = try_load_config(path)
    assert loaded.search_order == "missing_albums"
    assert loaded.search_order_direction == "asc"
    text = path.read_text(encoding="utf-8")
    assert 'search_order = "missing_albums"' in text
    assert 'search_order_direction = "asc"' in text

    cfg.search_order = "not-a-real-order"
    save_config(path, cfg)
    assert try_load_config(path).search_order == "lidarr"


def test_in_memory_catalog_uses_the_same_artist_order(tmp_path):
    worker = make_worker(tmp_path)
    try:
        tiny = album(1, "Tiny", "One", files=0, total=2)
        huge = album(2, "Huge", "Lots", files=0, total=600)
        worker.catalog.replace(build_catalog([huge, tiny], []))
        worker.store._catalog_ready = False
        worker.cfg = replace(worker.cfg, search_order="missing_tracks", search_order_direction="asc")
        assert worker._next_ready_artist() == "Tiny"
        worker.cfg = replace(worker.cfg, search_order_direction="desc")
        assert worker._next_ready_artist() == "Huge"
    finally:
        worker.close()
