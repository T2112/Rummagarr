from __future__ import annotations

import gc
from pathlib import Path

import pytest

from config_loader import Config
from worker import Worker


@pytest.fixture(autouse=True)
def collect_tk_on_the_main_thread():
    """Free leftover Tk objects before the next test starts a worker thread.

    A StringVar or PhotoImage destroyed off the main thread waits one second
    for a Tk loop that is not running. The parallel Lidarr lookup is that thread.
    """
    yield
    gc.collect()


def album(album_id: int, artist: str, title: str, *, files: int = 0, total: int = 10, artist_id: int = 1) -> dict:
    return {
        "id": album_id,
        "title": title,
        "artistId": artist_id,
        "artist": {"id": artist_id, "artistName": artist},
        "monitored": True,
        "releaseDate": "2020-05-01",
        "statistics": {"trackFileCount": files, "totalTrackCount": total, "trackCount": total},
    }


def track(track_id: int, title: str, *, has_file: bool = False, number: int = 1) -> dict:
    return {
        "id": track_id,
        "title": title,
        "monitored": True,
        "hasFile": has_file,
        "trackNumber": number,
    }


def hole(track_id: int, album_id: int, artist: str, album_title: str, title: str, *, number: int = 1) -> dict:
    return {
        "track": track(track_id, title, number=number),
        "album": album(album_id, artist, album_title, files=1, total=8),
        "artist_name": artist,
        "album_title": album_title,
    }


def make_config(tmp_path: Path) -> Config:
    download = tmp_path / "incomplete"
    complete = tmp_path / "complete"
    download.mkdir()
    complete.mkdir()
    return Config(
        lidarr_url="http://127.0.0.1:8686",
        lidarr_api_key="test-key",
        lidarr_scan_path=str(complete),
        slsk_username="souluser",
        slsk_password="soulpass",
        listen_port=2234,
        obfuscated_port=2235,
        enable_upnp=False,
        download_dir=download,
        complete_dir=complete,
        share_dir="",
        state_db=tmp_path / "state.sqlite3",
        log_file=tmp_path / "rummagarr.log",
        search_wait_seconds=12,
        max_albums_per_cycle=5,
        max_tracks_per_cycle=5,
        fill_missing_tracks=True,
        focus_one_artist=True,
        artist_cooldown_days=7,
        max_missing_pages=20,
        cycle_seconds=300,
        retry_hours=12,
        min_filename_ratio=0.5,
        track_count_tolerance=2,
        preferred_extensions=["mp3", "flac"],
        min_mp3_bitrate=320,
        convert_to_mp3=False,
        start_on_launch=False,
    )


def make_worker(tmp_path: Path) -> Worker:
    return Worker(make_config(tmp_path))
