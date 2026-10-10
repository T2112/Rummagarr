from __future__ import annotations

import asyncio

from tests.conftest import album, hole, make_worker
from wanted import album_key, build_catalog, song_key


class Transfer:
    def __init__(self, name: str, username: str, percent: float, state: str) -> None:
        self.filename = name
        self.username = username
        self.filesize = 1000
        self.bytes_transfered = int(percent * 10)
        self.state = type("State", (), {"name": state})()


def test_snapshot_respects_recent_limit_and_run_state(tmp_path):
    worker = make_worker(tmp_path)
    try:
        for index in range(30):
            worker.store.add_completed_download("Ella Langley", "Hungover", f"{index:02d}.mp3", "peer", "album")
        worker.active_transfers = [Transfer(r"album\01.flac", "vinylqueen", 45, "DOWNLOADING")]
        worker.set_job("downloading album", "Ella Langley", "Hungover")
        worker.control.start()
        worker._activate("album:1")
        snapshot = worker.ui_snapshot(recent_limit=25)
        assert len(snapshot["recent"]) == 25
        assert snapshot["recent"][0]["filename"] == "29.mp3"
        assert snapshot["run_state"] == "running"
        assert snapshot["current_key"] == "album:1"
        assert snapshot["transfers"][0]["percent"] == 45
        assert len(worker.ui_snapshot(recent_limit=10)["recent"]) == 10
        assert len(worker.ui_snapshot(recent_limit=999)["recent"]) == 10
    finally:
        worker.close()


def test_pause_leaves_saved_place_and_current_job(tmp_path):
    worker = make_worker(tmp_path)
    try:
        worker.store.upsert(3, "Ella Langley", "Hungover", "downloading", "midway")
        worker.store.set_focus_artist("Ella Langley")
        worker.store.set_meta("missing_page", "4")
        worker.store.set_wanted_choices({album_key(3): True, song_key(9): False})
        worker.set_job("downloading album", "Ella Langley", "Hungover")
        worker._activate(album_key(3))
        worker.control.start()
        worker.control.pause()
        assert worker.control.is_paused()
        assert worker.store.get(3)["detail"] == "midway"
        assert worker.store.focus_artist() == "Ella Langley"
        assert worker.store.get_meta("missing_page") == "4"
        assert worker.store.wanted_enabled(song_key(9)) is False
        assert worker.current_key == album_key(3)
        assert worker.job_info["title"] == "Hungover"
        worker.control.resume()
        assert worker.control.is_running()
        assert worker.store.get(3)["status"] == "downloading"
        assert worker.current_key == album_key(3)
    finally:
        worker.close()


def _patch_lidarr(worker, albums):
    worker.lidarr.wanted_missing = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should use the synced list"))
    worker.lidarr.albums_for_artist = lambda _artist_id: []
    worker.lidarr.tracks_for_album = lambda _album_id: []
    worker.lidarr.artist_id_by_name = lambda _name: None
    worker.cfg.max_albums_per_cycle = 5


def test_synced_worker_searches_only_checked_albums_and_songs(tmp_path):
    worker = make_worker(tmp_path)
    albums = [
        album(1, "Ella Langley", "Hungover", files=0, total=4),
        album(3, "Chris Stapleton", "Traveller", files=0, total=4, artist_id=2),
    ]
    holes = [hole(31, 3, "Chris Stapleton", "Traveller", "Tennessee Whiskey")]
    items = build_catalog(albums, holes)
    worker.control.start()
    worker.catalog.replace(items)
    worker.store.set_wanted_choices(
        {
            album_key(1): False,
            album_key(3): False,
            song_key(31): True,
        }
    )
    _patch_lidarr(worker, albums)
    seen_albums: list[int] = []
    seen_tracks: list[int] = []

    async def fake_album(row):
        seen_albums.append(int(row["id"]))
        return False

    async def fake_track(row):
        seen_tracks.append(int(row["track"]["id"]))
        return False

    worker.process_album = fake_album
    worker.process_track = fake_track
    try:
        asyncio.run(worker.cycle())
        assert seen_albums == []
        assert seen_tracks == [31]
        assert worker.store.focus_artist() is None
    finally:
        worker.close()


def test_checked_empty_album_is_grabbed_without_also_grabbing_its_songs(tmp_path):
    worker = make_worker(tmp_path)
    albums = [album(1, "Ella Langley", "Hungover", files=0, total=2)]
    holes = [hole(11, 1, "Ella Langley", "Hungover", "Blue")]
    worker.control.start()
    worker.catalog.replace(build_catalog(albums, holes))
    worker.store.remember_wanted_keys(["album:1", "song:11"])
    _patch_lidarr(worker, albums)
    seen_albums: list[int] = []
    seen_tracks: list[int] = []

    async def fake_album(row):
        seen_albums.append(int(row["id"]))
        return True

    async def fake_track(row):
        seen_tracks.append(int(row["track"]["id"]))
        return True

    worker.process_album = fake_album
    worker.process_track = fake_track
    try:
        asyncio.run(worker.cycle())
        assert seen_albums == [1]
        assert seen_tracks == []
    finally:
        worker.close()


def test_without_a_sync_the_worker_still_pages_lidarr(tmp_path):
    worker = make_worker(tmp_path)
    worker.control.start()
    seen = {}

    def wanted_missing(**kwargs):
        seen["kwargs"] = kwargs
        return [album(1, "Ada", "One", files=0, total=2)], 2, False

    worker.lidarr.wanted_missing = wanted_missing
    worker.lidarr.artist_id_by_name = lambda _name: None
    worker.lidarr.albums_for_artist = lambda _artist_id: []
    worker.lidarr.tracks_for_album = lambda _album_id: []
    worker.store.set_wanted_choices({album_key(1): False})
    seen_albums: list[int] = []

    async def fake_album(row):
        seen_albums.append(int(row["id"]))
        return False

    worker.process_album = fake_album
    try:
        asyncio.run(worker.cycle())
        assert seen["kwargs"]["max_pages"] == 20
        assert seen_albums == []
        assert worker.store.get_meta("missing_page") == "2"
        assert worker.store.focus_artist() is None
    finally:
        worker.close()
