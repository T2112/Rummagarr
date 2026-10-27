"""Rummagarr 1.19.1: Lidarr import frequency, timeout, and per-artist retry or skip."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

from changelog_notes import changelog_section
from config_loader import default_config, normalize_import_mode, save_config, try_load_config
from lidarr_client import LidarrClient
from tests.conftest import make_worker

ROOT = Path(__file__).resolve().parents[1]


def test_changelog_mentions_the_import_wait():
    section = changelog_section((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), "1.19.1")
    assert "Waiting on Lidarr import" in section
    assert "Retry import" in section
    assert "Off (manual)" in section
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Version 1.19.1" in text
    assert "Waiting on Lidarr import" in text


def test_import_settings_follow_the_old_checkbox_and_round_trip(tmp_path):
    assert normalize_import_mode("", legacy_auto=False) == "album"
    assert normalize_import_mode("", legacy_auto=True) == "after_artist"
    assert normalize_import_mode("manual") == "off"
    fresh = default_config(
        lidarr_api_key="abc123",
        slsk_username="souluser",
        slsk_password="soulpass",
        download_dir=tmp_path / "in",
        complete_dir=tmp_path / "out",
    )
    assert fresh.import_mode == "album"
    assert fresh.auto_import_after_artist is False
    assert fresh.import_interval_minutes == 30
    assert fresh.import_timeout_minutes == 10
    checked = default_config(
        lidarr_api_key="abc123",
        slsk_username="souluser",
        slsk_password="soulpass",
        download_dir=tmp_path / "in",
        complete_dir=tmp_path / "out",
        auto_import_after_artist=True,
    )
    assert checked.import_mode == "after_artist"
    path = tmp_path / "config.toml"
    save_config(
        path,
        replace(fresh, import_mode="interval", import_interval_minutes=15, import_timeout_minutes=10),
    )
    loaded = try_load_config(path)
    assert loaded.import_mode == "interval"
    assert loaded.auto_import_after_artist is False
    assert loaded.import_interval_minutes == 15
    assert loaded.import_timeout_minutes == 10
    text = path.read_text(encoding="utf-8")
    text = text.replace('import_mode = "interval"\n', "")
    path.write_text(text, encoding="utf-8")
    legacy = try_load_config(path)
    assert legacy.import_mode == "album"
    assert legacy.auto_import_after_artist is False


def test_finish_command_stops_when_cancelled_or_the_deadline_passes():
    client = LidarrClient("http://127.0.0.1:9", "key")
    client._get_once = lambda path, timeout=20.0: {"id": 4, "status": "started"}
    try:
        skipped = client.finish_command({"id": 4, "status": "queued"}, timeout=5, should_cancel=lambda: True)
        assert skipped["status"] == "skipped"
        timed = client.finish_command({"id": 4, "status": "queued"}, timeout=1)
    finally:
        client.close()
    assert timed["status"] == "timeout"


def test_a_timed_out_import_flags_the_artist_and_continues(tmp_path, caplog):
    import logging

    worker = make_worker(tmp_path)
    worker.cfg = replace(worker.cfg, auto_import_after_artist=True, import_mode="after_artist", import_timeout_minutes=10)
    scanned: list[str] = []
    waiting: list[str] = []

    def scan(path):
        scanned.append(path)
        return {"id": 1, "status": "started"}

    def finish(result, timeout=900, should_cancel=None, on_wait=None):
        if on_wait:
            on_wait(180, timeout)
            waiting.append(worker.activity_view()[0])
        return {"status": "timeout"}

    worker.lidarr.scan_downloaded = scan
    worker.lidarr.finish_command = finish
    worker.lidarr.renames_on_import = lambda: True
    folder = worker.cfg.complete_dir / "Adele - 21"
    folder.mkdir()
    (folder / "01.flac").write_bytes(b"flac")
    try:
        with caplog.at_level(logging.ERROR, logger="rummagarr"):
            worker.arm_auto_import("Adele")
            worker.arm_auto_import("Beyoncé")
            assert worker._import_thread is not None
            worker._import_thread.join(2)
            # The second artist starts after the first one leaves the thread.
            deadline = time.time() + 2
            while len(scanned) < 2 and time.time() < deadline:
                thread = worker._import_thread
                if thread is not None:
                    thread.join(0.2)
                else:
                    time.sleep(0.02)
            thread = worker._import_thread
            if thread is not None:
                thread.join(2)
        assert len(scanned) == 2
        assert "timed out" in caplog.text
        assert "Waiting on Lidarr import for Adele (3/10 min)" in waiting
        notice = worker.activity_view()[0]
        assert "Beyoncé" in notice or "timed out" in notice.casefold()
        assert worker._import_flags["adele"][1].startswith("Lidarr import timed out")
        assert folder.exists()
        assert not (worker.cfg.complete_dir / "Review").exists()
        assert worker._importing_artist == ""
    finally:
        worker.close()


def test_skip_stops_the_wait_and_retry_runs_when_import_is_off(tmp_path):
    worker = make_worker(tmp_path)
    worker.cfg = replace(worker.cfg, auto_import_after_artist=True, import_mode="after_artist")
    started = threading.Event()
    scanned: list[str] = []
    calls = {"n": 0}

    def scan(path):
        scanned.append(path)
        return {"id": 3, "status": "started"}

    def finish(result, timeout=900, should_cancel=None, on_wait=None):
        calls["n"] += 1
        if calls["n"] > 1:
            return {"name": "DownloadedAlbumsScan", "status": "completed"}
        if on_wait:
            on_wait(60, timeout)
        started.set()
        for _ in range(40):
            if should_cancel and should_cancel():
                return {"status": "skipped"}
            time.sleep(0.05)
        return {"status": "completed"}

    worker.lidarr.scan_downloaded = scan
    worker.lidarr.finish_command = finish
    worker.lidarr.renames_on_import = lambda: True
    try:
        worker.arm_auto_import("Adele")
        assert started.wait(2)
        assert "Waiting on Lidarr import" in worker.activity_view()[0]
        assert "(1/10 min)" in worker.activity_view()[0]
        worker.skip_import("Adele")
        assert worker._import_thread is not None
        worker._import_thread.join(2)
        assert worker.activity_view()[0] == "Skipped Lidarr import for Adele"
        assert worker.import_attention()[0]["artist"] == "Adele"
        worker.cfg = replace(worker.cfg, auto_import_after_artist=False, import_mode="off")
        worker.arm_auto_import("Adele")
        time.sleep(0.05)
        assert scanned == [str(worker.cfg.lidarr_scan_path)]
        worker.retry_import("Adele")
        deadline = time.time() + 2
        while len(scanned) < 2 and time.time() < deadline:
            thread = worker._import_thread
            if thread is not None:
                thread.join(0.2)
            else:
                time.sleep(0.02)
        assert len(scanned) == 2
        thread = worker._import_thread
        if thread is not None:
            thread.join(2)
    finally:
        worker.close()


def test_interval_import_waits_until_it_is_due(tmp_path):
    worker = make_worker(tmp_path)
    worker.cfg = replace(
        worker.cfg,
        auto_import_after_artist=False,
        import_mode="interval",
        import_interval_minutes=30,
    )
    scanned: list[str] = []
    worker.lidarr.scan_downloaded = lambda path: scanned.append(path) or {"name": "DownloadedAlbumsScan"}
    worker.lidarr.renames_on_import = lambda: True
    worker.lidarr.artist_id_by_name = lambda name: None
    worker.store.upsert(4, "Adele", "21", "staged", "folder")
    try:
        worker._last_interval_import = time.monotonic()
        worker.maybe_interval_import()
        time.sleep(0.05)
        assert scanned == []
        worker._last_interval_import = time.monotonic() - (31 * 60)
        worker.maybe_interval_import()
        deadline = time.time() + 2
        while not scanned and time.time() < deadline:
            thread = worker._import_thread
            if thread is not None:
                thread.join(0.2)
            else:
                time.sleep(0.02)
        assert scanned == [str(worker.cfg.lidarr_scan_path)]
    finally:
        worker.close()


def test_the_worker_returns_while_an_import_is_still_running(tmp_path):
    import asyncio

    worker = make_worker(tmp_path)
    worker.cfg = replace(worker.cfg, auto_import_after_artist=True, import_mode="after_artist")
    started = threading.Event()
    release = threading.Event()

    def scan(path):
        started.set()
        assert release.wait(2)
        return {"name": "DownloadedAlbumsScan"}

    worker.lidarr.scan_downloaded = scan
    worker.lidarr.renames_on_import = lambda: True
    worker.control.start()
    try:
        worker.arm_auto_import("Adele")
        assert started.wait(1)

        async def during() -> float:
            began = time.monotonic()
            await worker._wait_for_import()
            return time.monotonic() - began

        elapsed = asyncio.run(during())
        assert elapsed < 0.5
        assert worker._import_thread is not None and worker._import_thread.is_alive()
    finally:
        release.set()
        worker.close()
