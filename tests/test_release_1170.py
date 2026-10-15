"""Rummagarr 1.17.0: saved browse limits, daily caps, and favorite suggestions."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from changelog_notes import changelog_section
from config_loader import default_config, save_config, try_load_config
from favorites import daily_room, should_suggest
from tests.conftest import make_worker


class _Transfer:
    def __init__(self, username: str, filename: str) -> None:
        self.state = "COMPLETE"
        self.username = username
        self.filename = filename


class _Candidate:
    def __init__(self, username: str, score: int) -> None:
        self.username = username
        self.remote_dir = "music"
        self.score = score
        self.files = [object()]


def test_limits_round_trip_and_missing_section_keeps_defaults(tmp_path: Path):
    cfg = default_config(
        lidarr_api_key="abc123",
        slsk_username="souluser",
        slsk_password="soulpass",
        download_dir=tmp_path / "in",
        complete_dir=tmp_path / "out",
        browse_max_files=4,
        browse_max_mb=100,
        browse_daily_files=6,
    )
    path = tmp_path / "config.toml"
    save_config(path, cfg)
    loaded = try_load_config(path)
    assert loaded.browse_max_files == 4
    assert loaded.browse_max_mb == 100
    assert loaded.browse_daily_files == 6
    text = path.read_text(encoding="utf-8")
    assert "[limits]" in text
    path.write_text(text.split("[limits]", 1)[0], encoding="utf-8")
    kept = try_load_config(path)
    assert kept.browse_max_files == 25
    assert kept.browse_max_mb == 2048
    assert kept.browse_daily_files == 20


def test_daily_room_and_suggestion_rules():
    assert daily_room(18, 20) == 2
    assert daily_room(20, 20) == 0
    assert should_suggest("Keeper", 3, set(), set())
    assert not should_suggest("Keeper", 2, set(), set())
    assert not should_suggest("Keeper", 5, {"keeper"}, set())
    assert not should_suggest("Keeper", 5, set(), {"keeper"})


def test_finished_files_suggest_a_favorite_without_adding_one(tmp_path: Path):
    worker = make_worker(tmp_path)
    try:
        worker.cfg = replace(worker.cfg, priority_users={"friend"})
        friends = [_Transfer("friend", f"{index}.mp3") for index in range(3)]
        worker.remember_completed(friends, "Ella", "Hungover", "album")
        assert worker.store.user_day_files("friend") == 3
        assert not any(event["username"] == "friend" for event in worker.store.recent_events())

        keepers = [_Transfer("Keeper", f"k{index}.mp3") for index in range(3)]
        worker.remember_completed(keepers, "Ella", "Hungover", "album")
        assert worker.store.user_day_files("keeper") == 3
        assert "keeper" not in worker.cfg.priority_users
        favorites = [event for event in worker.store.recent_events() if event["kind"] == "favorite"]
        assert len(favorites) == 1
        assert favorites[0]["username"] == "keeper"
        worker.remember_completed([_Transfer("Keeper", "again.mp3")], "Ella", "Hungover", "album")
        again = [event for event in worker.store.recent_events() if event["kind"] == "favorite"]
        assert len(again) == 1
        worker.store.dismiss_favorite("Keeper")
        worker.remember_completed([_Transfer("Keeper", "later.mp3")], "Ella", "Hungover", "album")
        assert not any(event["kind"] == "favorite" for event in worker.store.recent_events())
        assert "keeper" not in worker.cfg.priority_users
    finally:
        worker.close()


def test_daily_cap_skips_a_user_without_blocklisting(tmp_path: Path):
    worker = make_worker(tmp_path)
    try:
        worker.cfg = replace(worker.cfg, browse_daily_files=1)
        worker.store.add_user_day("full", 1)
        calls: list[str] = []

        async def fake(best, *_args, **_kwargs):
            calls.append(best.username)
            return "ok", "done"

        worker._download_one = fake
        picked = asyncio.run(
            worker._download_candidates(
                [_Candidate("full", 9), _Candidate("open", 1)],
                {},
                "k",
                7,
                "Ella",
                "Hungover",
                None,
                [],
                set(),
            )
        )
        assert picked is True
        assert calls == ["open"]
        assert not worker.store.source_blocked("full", "music")

        blocked = asyncio.run(
            worker._download_candidates(
                [_Candidate("full", 9)],
                {},
                "k",
                8,
                "Ella",
                "Hungover",
                None,
                [],
                set(),
            )
        )
        assert blocked is False
        job = worker.store.get(8)
        assert job is not None
        assert job["status"] == "failed"
        assert "daily cap" in job["detail"].lower()
        assert not worker.store.source_blocked("full", "music")
        assert any(event["kind"] == "daily" for event in worker.store.recent_events())
    finally:
        worker.close()


def test_browse_download_stops_at_the_daily_cap(tmp_path: Path):
    worker = make_worker(tmp_path)
    try:
        worker.cfg = replace(worker.cfg, dry_run=True, browse_daily_files=2)
        specs = [{"username": "goodpeer", "filename": f"{index}.flac"} for index in range(4)]
        note = asyncio.run(worker.download_paced(specs, pace_seconds=0))
        assert "would grab 2" in note
        worker.store.add_user_day("goodpeer", 2)
        stopped = asyncio.run(worker.download_paced(specs, pace_seconds=0))
        assert "already used" in stopped
        assert any(event["kind"] == "daily" for event in worker.store.recent_events())
    finally:
        worker.close()


def test_changelog_mentions_saved_limits_and_favorites():
    section = changelog_section(Path("CHANGELOG.md").read_text(encoding="utf-8"), "1.17.0")
    assert "per day" in section
    assert "favorite" in section
    assert "blocklist" in section
    assert "1.16.0" not in section
