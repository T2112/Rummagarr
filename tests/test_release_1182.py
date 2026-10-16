"""Rummagarr 1.18.2: favorite suggestions can be turned off, and they share one prompt."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from changelog_notes import changelog_section
from config_loader import default_config, save_config, try_load_config
from tests.conftest import make_worker


class _Transfer:
    def __init__(self, username: str, filename: str) -> None:
        self.state = "COMPLETE"
        self.username = username
        self.filename = filename


def test_suggest_favorites_round_trips_and_stays_on_when_missing(tmp_path: Path):
    cfg = default_config(
        lidarr_api_key="abc123",
        slsk_username="souluser",
        slsk_password="soulpass",
        download_dir=tmp_path / "in",
        complete_dir=tmp_path / "out",
        suggest_favorites=False,
    )
    path = tmp_path / "config.toml"
    save_config(path, cfg)
    text = path.read_text(encoding="utf-8")
    assert 'suggest_favorites = false' in text
    assert try_load_config(path).suggest_favorites is False
    path.write_text(text.replace("suggest_favorites = false\n", ""), encoding="utf-8")
    assert try_load_config(path).suggest_favorites is True


def test_turning_suggestions_off_still_counts_the_day(tmp_path: Path):
    worker = make_worker(tmp_path)
    try:
        worker.cfg = replace(worker.cfg, suggest_favorites=False)
        finished = [_Transfer("Keeper", f"k{index}.mp3") for index in range(3)]
        worker.remember_completed(finished, "Ella", "Hungover", "album")
        assert worker.store.user_day_files("keeper") == 3
        assert not worker.store.unseen_events("favorite")
        assert "keeper" not in worker.cfg.priority_users
    finally:
        worker.close()


def test_changelog_mentions_the_favorite_suggestion_setting():
    section = changelog_section(Path("CHANGELOG.md").read_text(encoding="utf-8"), "1.18.2")
    assert "Suggest favorite users" in section
    assert "Don't ask again" in section
    assert "config.toml" in section
