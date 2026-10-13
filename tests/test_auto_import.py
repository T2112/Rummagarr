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
        client.scan_downloaded(r"D:\Music\Seekarr")
    finally:
        client.close()
    assert seen["path"] == "/command"
    assert seen["payload"] == {
        "name": "DownloadedAlbumsScan",
        "path": r"D:\Music\Seekarr",
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
        assert worker.activity_view()[0] == "Importing Adele into Lidarr failed"
        assert "lidarr down" in caplog.text
        assert renamed == []
    finally:
        worker.close()
