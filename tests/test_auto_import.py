from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace

from activity import Step
from lidarr_client import LidarrClient
from tests.conftest import make_worker


def test_downloaded_albums_scan_moves_the_folder():
    client = LidarrClient("http://127.0.0.1:8686", "test-key")
    seen: dict = {}

    def post(path, payload):
        seen["path"] = path
        seen["payload"] = payload
        return {"name": "DownloadedAlbumsScan"}

    client._post = post
    try:
        client.scan_downloaded(r"D:\Media\Incoming\Soulseek")
    finally:
        client.close()
    assert seen["path"] == "/command"
    assert seen["payload"] == {
        "name": "DownloadedAlbumsScan",
        "path": r"D:\Media\Incoming\Soulseek",
        "importMode": "Move",
    }


def _wire(worker, scanned, renamed, *, rename=False, artist_id=9, scan_result=None):
    worker.cfg = replace(worker.cfg, auto_import_after_artist=True)

    def scan(path):
        scanned.append(path)
        if isinstance(scan_result, Exception):
            raise scan_result
        if scan_result is not None:
            return scan_result
        return {"name": "DownloadedAlbumsScan"}

    worker.lidarr.scan_downloaded = scan
    worker.lidarr.renames_on_import = lambda: rename
    worker.lidarr.artist_id_by_name = lambda name: artist_id
    worker.lidarr.rename_artist_files = lambda artist: renamed.append(artist)


def test_auto_import_waits_for_an_empty_queue_and_runs_once(tmp_path):
    worker = make_worker(tmp_path)
    scanned: list[str] = []
    renamed: list[int] = []
    _wire(worker, scanned, renamed)
    try:
        worker.active_transfers = [object()]
        worker.arm_auto_import("Adele")
        assert scanned == []
        worker.active_transfers = []
        worker.maybe_auto_import()
        assert worker._import_thread is not None
        worker._import_thread.join(2)
        assert scanned == [str(worker.cfg.lidarr_scan_path)]
        assert renamed == [9]
        assert worker.activity_view()[0] == "Imported Adele into Lidarr"
        worker.maybe_auto_import()
        assert scanned == [str(worker.cfg.lidarr_scan_path)]
        worker.arm_auto_import("Beyoncé")
        assert worker._import_thread is not None
        worker._import_thread.join(2)
        assert len(scanned) == 2
        assert worker.activity_view()[0] == "Imported Beyoncé into Lidarr"
        worker.control.start()
        worker.set_step(Step(kind="searching", artist="Next", album="Album"))
        assert worker.activity_view()[0] == "Imported Beyoncé into Lidarr"
        worker._finish_artist_visit("Next")
        assert worker.activity_view()[0].startswith("Searching Soulseek")
    finally:
        worker.close()


def test_auto_import_stays_off_until_the_checkbox_is_on(tmp_path):
    worker = make_worker(tmp_path)
    scanned: list[str] = []
    worker.lidarr.scan_downloaded = lambda path: scanned.append(path)
    try:
        assert worker.cfg.auto_import_after_artist is False
        worker.arm_auto_import("Adele")
        worker.maybe_auto_import()
        time.sleep(0.05)
        assert scanned == []
        assert worker._import_thread is None
    finally:
        worker.close()


def test_auto_import_does_not_block_and_skips_rename_when_lidarr_already_renames(tmp_path):
    worker = make_worker(tmp_path)
    worker.cfg = replace(worker.cfg, auto_import_after_artist=True)
    started = threading.Event()
    release = threading.Event()
    renamed: list[int] = []

    def scan(path):
        started.set()
        assert release.wait(2)
        return {"name": "DownloadedAlbumsScan"}

    worker.lidarr.scan_downloaded = scan
    worker.lidarr.renames_on_import = lambda: True
    worker.lidarr.rename_artist_files = lambda artist: renamed.append(artist)
    try:
        began = time.monotonic()
        worker.arm_auto_import("Adele")
        assert time.monotonic() - began < 0.3
        assert started.wait(1)
        assert worker.activity_view()[0] == "Importing Adele into Lidarr..."
        worker.set_step(Step(kind="retry", artist="Adele", retry_seconds=5))
        assert worker.activity_view()[0] == "Importing Adele into Lidarr..."
        release.set()
        assert worker._import_thread is not None
        worker._import_thread.join(2)
        assert renamed == []
        assert worker.activity_view()[0] == "Imported Adele into Lidarr"
    finally:
        release.set()
        worker.close()


def test_a_failed_import_is_kept_and_moved_aside_for_review(tmp_path):
    worker = make_worker(tmp_path)
    scanned: list[str] = []
    renamed: list[int] = []
    _wire(worker, scanned, renamed, rename=True)
    worker.lidarr.import_failures = lambda artist: ["track count mismatch"]
    folder = worker.cfg.complete_dir / "Adele - 21"
    folder.mkdir()
    (folder / "01.flac").write_bytes(b"flac")
    worker.store.add_completed_download("Adele", "21", "01.flac", "vinylqueen", "album")
    worker.store.upsert(4, "Adele", "21", "imported", str(folder))
    try:
        worker.arm_auto_import("Adele")
        assert worker._import_thread is not None
        worker._import_thread.join(2)
        notice = worker.activity_view()[0]
        assert notice == "Importing Adele into Lidarr failed: track count mismatch"
        assert not folder.exists()
        assert (worker.cfg.complete_dir / "Review" / "Adele - 21" / "01.flac").is_file()
        assert "vinylqueen" in worker.store.rejected_usernames(artist="Adele")
        assert worker.store.should_skip(4, 12) is False
        assert worker.store.get(4)["status"] == "import_failed"
        worker.set_step(Step(kind="searching", artist="Next", album="Later"))
        assert worker.activity_view()[0] == notice
    finally:
        worker.close()


def test_import_resyncs_that_artist_before_the_next_search(tmp_path):
    from wanted import album_key, build_catalog, items_to_rows

    from tests.conftest import album, track

    worker = make_worker(tmp_path)
    missing = album(30, "Adele", "30", files=0, total=2, artist_id=9)
    done = album(21, "Adele", "21", files=2, total=2, artist_id=9)
    partial = album(40, "Adele", "25", files=1, total=2, artist_id=9)
    items = build_catalog(
        [missing, done, partial],
        [
            {
                "track": track(301, "Rolling"),
                "album": missing,
                "artist_name": "Adele",
                "album_title": "30",
            }
        ],
    )
    worker.store.write_wanted_page(items_to_rows(items))
    worker.catalog.replace(items)
    worker.store.set_wanted_choices({album_key(30): False})

    def library(name):
        assert name == "Adele"
        return {
            "artist_id": 9,
            "albums": [missing, done, partial],
            "tracks": [
                {"id": 211, "albumId": 21, "title": "Hello", "monitored": True, "hasFile": True},
                {"id": 212, "albumId": 21, "title": "Rolling in the Deep", "monitored": True, "hasFile": True},
                {"id": 301, "albumId": 30, "title": "Rolling", "monitored": True, "hasFile": False},
                {"id": 302, "albumId": 30, "title": "Easy", "monitored": True, "hasFile": False},
                {"id": 401, "albumId": 40, "title": "Daydreamer", "monitored": True, "hasFile": True},
                {"id": 402, "albumId": 40, "title": "Best for Last", "monitored": True, "hasFile": False},
            ],
        }

    worker.lidarr.artist_library = library
    scanned: list[str] = []
    renamed: list[int] = []
    _wire(worker, scanned, renamed, rename=True)
    try:
        worker.arm_auto_import("Adele")
        assert worker._import_thread is not None
        worker._import_thread.join(2)
        assert worker.activity_view()[0] == "Imported Adele into Lidarr"
        albums, holes = worker.store.artist_work("Adele")
        titles = {row.get("title") for row in albums}
        assert "21" not in titles
        assert "25" in titles
        stored = {
            title
            for (title,) in worker.store._db.execute(
                "SELECT album FROM wanted_rows WHERE artist_key = ? AND kind = 'album'",
                ("adele",),
            )
        }
        assert stored == {"30", "25"}
        kept = worker.store._db.execute(
            "SELECT enabled FROM wanted_rows WHERE key = ?",
            (album_key(30),),
        ).fetchone()
        assert kept is not None and kept[0] == 0
        assert worker.store.should_skip(21, 0) is True
        assert worker.store.get(21)["status"] == "imported"
        assert worker.store.should_skip_track(401, 0) is True
        assert {hole["track"]["title"] for hole in holes} == {"Best for Last"}
        cached, _cached_holes = worker.catalog.work_for_artist("Adele")
        assert "21" not in {row.get("title") for row in cached}
    finally:
        worker.close()


def test_the_worker_searches_during_the_scan_and_waits_for_the_resync(tmp_path):
    import asyncio

    worker = make_worker(tmp_path)
    started = threading.Event()
    release = threading.Event()
    resync_started = threading.Event()
    release_resync = threading.Event()
    worker.cfg = replace(worker.cfg, auto_import_after_artist=True)

    def library(name):
        resync_started.set()
        assert release_resync.wait(2)
        return {"artist_id": 9, "albums": [], "tracks": []}

    def scan(path):
        started.set()
        assert release.wait(2)
        return {"name": "DownloadedAlbumsScan"}

    worker.lidarr.artist_library = library
    worker.lidarr.scan_downloaded = scan
    worker.lidarr.renames_on_import = lambda: True
    worker.control.start()
    try:
        worker.arm_auto_import("Adele")
        assert started.wait(1)

        async def during_scan() -> None:
            task = asyncio.create_task(worker._wait_for_import())
            await asyncio.wait_for(task, 1)

        asyncio.run(during_scan())
        release.set()
        assert resync_started.wait(2)

        async def during_resync() -> None:
            task = asyncio.create_task(worker._wait_for_import())
            await asyncio.sleep(0.2)
            assert not task.done()
            release_resync.set()
            await asyncio.wait_for(task, 2)

        asyncio.run(during_resync())
        assert worker._import_thread is None or not worker._import_thread.is_alive()
    finally:
        release.set()
        release_resync.set()
        worker.close()


def test_files_already_queued_or_on_disk_are_not_downloaded_again(tmp_path):
    worker = make_worker(tmp_path)
    try:
        folder = worker.cfg.complete_dir / "Adele - 21"
        folder.mkdir()
        (folder / "01 Hello.mp3").write_bytes(b"mp3")
        assert worker.requeue_block("Adele", "21", None) == "already in the download folder"
        assert worker.track_requeue_block("Adele", "21", "Hello", None) == "already in the download folder"
        assert worker.track_requeue_block("Adele", "21", "Someone Like You", None) == ""
        incoming = worker.cfg.download_dir / "Adele - 30"
        incoming.mkdir()
        (incoming / "01.flac").write_bytes(b"flac")
        assert worker.requeue_block("Adele", "30", None) == "already in the download folder"
        queued = type("Transfer", (), {})()
        queued.filename = r"music\Adele\25\01.mp3"
        queued.state = type("State", (), {"name": "DOWNLOADING"})()
        worker.active_transfers = [queued]
        assert worker.requeue_block("Adele", "25", None) == "already queued"
        assert worker._path_queued(r"music\Adele\25\01.mp3") is True
        finished = type("Transfer", (), {})()
        finished.filename = r"music\Adele\19\01.mp3"
        finished.state = type("State", (), {"name": "COMPLETE"})()
        worker.manual_transfers = [finished]
        assert worker.requeue_block("Adele", "19", None) == ""
    finally:
        worker.close()


def test_auto_import_failure_is_logged_and_shown(tmp_path, caplog):
    worker = make_worker(tmp_path)
    scanned: list[str] = []
    renamed: list[int] = []
    _wire(worker, scanned, renamed, scan_result=RuntimeError("lidarr down"))
    try:
        with caplog.at_level(logging.ERROR, logger="lidarr_slsk"):
            worker.arm_auto_import("Adele")
            assert worker._import_thread is not None
            worker._import_thread.join(2)
        assert worker.activity_view()[0] == "Importing Adele into Lidarr failed: lidarr down"
        assert "lidarr down" in caplog.text
        assert renamed == []
    finally:
        worker.close()
