"""Rummagarr 1.15.0: browse a user, setup check, and backup."""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path
from types import SimpleNamespace

from changelog_notes import changelog_section
from config_loader import default_config
from backup_data import create_backup, restore_backup, safe_member
from setup_check import format_setup_report, local_setup_checks
from tests.conftest import album, make_config, make_worker
from user_browse import (
    BrowseCache,
    ShareIndex,
    is_wanted,
    missing_needles,
    permission_label,
    profile_text,
)


def _share() -> list[dict]:
    return [
        {
            "name": r"Music\Ella Langley\Hungover",
            "files": [{"filename": r"Music\Ella Langley\Hungover\01 Blue.flac", "size": 2048, "extension": "flac"}],
        },
        {"name": r"Music\Ella Langley\Still", "files": []},
        {"name": r"Music\Other", "locked": True, "files": []},
    ]


def test_share_tree_opens_one_level_and_marks_wanted():
    index = ShareIndex(_share())
    roots = index.children("")
    assert [row["name"] for row in roots] == ["Music"]
    assert index.children("Music")[0]["name"] == "Ella Langley"
    hungover = index.children(r"Music\Ella Langley")
    assert [row["name"] for row in hungover] == ["Hungover", "Still"]
    files = index.children(r"Music\Ella Langley\Hungover")
    assert files[0]["kind"] == "file"
    assert files[0]["name"] == "01 Blue.flac"
    locked = [row for row in index.children("Music") if row["name"] == "Other"]
    assert locked[0]["locked"] is True

    needles = missing_needles(
        [
            album(1, "Ella Langley", "Hungover", files=0, total=10),
            album(2, "Ella Langley", "Still", files=8, total=8),
            album(3, "Ella Langley", "No", files=0, total=1),
        ]
    )
    assert ("ella langley", "hungover") in needles
    assert all(title != "still" for _artist, title in needles)
    assert is_wanted(r"Music\Ella Langley\Hungover", needles)
    assert not is_wanted(r"Music\Ella Langley", needles)
    assert not is_wanted(r"Music\Other", needles)


def test_profile_card_and_cache_round_trip(tmp_path: Path):
    assert permission_label(0) == "Uploads off"
    assert permission_label(1) == "Uploads allowed"
    text = profile_text(
        "goodpeer",
        {
            "status": 2,
            "has_slots_free": True,
            "upload_slots": 2,
            "queue_length": 4,
            "upload_permissions": 0,
            "shared_file_count": 12,
            "shared_folder_count": 3,
            "avg_speed": 50,
            "description": "country flac",
            "has_picture": True,
        },
    )
    assert "goodpeer" in text
    assert "Online" in text
    assert "Uploads off" in text
    assert "Free slot" in text
    assert "country flac" in text

    cache = BrowseCache(tmp_path / "state-browse.sqlite3")
    try:
        assert cache.get("goodpeer") is None
        saved = cache.put("GoodPeer", {"status": 1}, _share())
        loaded = cache.get("goodpeer")
        assert loaded is not None
        assert loaded["profile"]["status"] == 1
        assert loaded["directories"][0]["name"].endswith("Hungover")
        assert loaded["fetched_at"] == saved["fetched_at"]
    finally:
        cache.close()


def test_browse_user_reads_profile_and_shares(tmp_path: Path):
    worker = make_worker(tmp_path)

    class FakeClient:
        session = object()

        async def execute(self, command, response=True, timeout=15):
            name = type(command).__name__
            if name == "GetUserStatusCommand":
                return SimpleNamespace(status=0, privileged=False)
            if name == "GetUserStatsCommand":
                return SimpleNamespace(avg_speed=8, uploads=1, shared_file_count=2, shared_folder_count=1)
            if name == "PeerGetUserInfoCommand":
                return SimpleNamespace(
                    description="hi",
                    picture=b"jpg",
                    has_slots_free=False,
                    upload_slots=1,
                    queue_length=2,
                    upload_permissions=0,
                )
            if name == "PeerGetSharesCommand":
                folder = SimpleNamespace(
                    name=r"Music\Ella Langley\Hungover",
                    files=[SimpleNamespace(filename="01.flac", filesize=9, extension="flac")],
                )
                locked = SimpleNamespace(name=r"Music\Secret", files=[])
                return [folder], [locked]
            raise AssertionError(name)

    try:
        worker.client = FakeClient()
        payload = asyncio.run(worker.browse_user("goodpeer"))
        assert payload["profile"]["status"] == 0
        assert payload["profile"]["upload_permissions"] == 0
        assert payload["profile"]["has_picture"] is True
        names = [row["name"] for row in payload["directories"]]
        assert r"Music\Ella Langley\Hungover" in names
        assert r"Music\Secret" in names
        assert "Offline" in profile_text("goodpeer", payload["profile"])
        assert "Uploads off" in profile_text("goodpeer", payload["profile"])
    finally:
        worker.close()


def test_setup_check_flags_placeholders_and_accepts_a_ready_config(tmp_path: Path):
    broken = default_config(
        lidarr_api_key="CHANGEME",
        slsk_username="",
        slsk_password="CHANGEME",
        listen_port=2234,
        obfuscated_port=2234,
        download_dir=tmp_path / "in",
        complete_dir=tmp_path / "out",
        lidarr_scan_path="",
        state_db=tmp_path / "state.sqlite3",
        log_file=tmp_path / "log.txt",
    )
    report = format_setup_report(local_setup_checks(broken))
    assert "Needs attention" in report
    assert "Lidarr API key" in report
    assert "Soulseek username" in report
    assert "Listen ports" in report

    ready = make_config(tmp_path)
    checks = local_setup_checks(ready)
    assert all(item.ok for item in checks)
    assert format_setup_report(checks).endswith("Setup looks ready.")


def test_backup_round_trip_ignores_paths_outside_the_zip(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text("enabled = false\n", encoding="utf-8")
    db = tmp_path / "state.sqlite3"
    db.write_bytes(b"")
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE meta (k TEXT)")
    conn.execute("INSERT INTO meta VALUES ('keep')")
    conn.commit()
    conn.close()
    log = tmp_path / "rummagarr.log"
    log.write_text("hello\n", encoding="utf-8")
    (tmp_path / "state-browse.sqlite3").write_bytes(b"browse")

    dest = tmp_path / "backups" / "Rummagarr-backup.zip"
    names = create_backup(dest, config_path=config, state_db=db, log_file=log)
    assert names == ["config.toml", "state.sqlite3", "state-browse.sqlite3", "rummagarr.log"]
    assert safe_member("../config.toml") is None
    assert safe_member("config.toml") == "config.toml"
    assert safe_member("lidarr-soulseek.log") == "lidarr-soulseek.log"

    poisoned = tmp_path / "poison.zip"
    outside = tmp_path / "escaped.txt"
    with zipfile.ZipFile(poisoned, "w") as archive:
        archive.writestr("../escaped.txt", "nope")
        archive.writestr("config.toml", "restored = true\n")
    restored_config = tmp_path / "restored" / "config.toml"
    written = restore_backup(poisoned, config_path=restored_config, state_db=tmp_path / "restored" / "state.sqlite3", log_file=tmp_path / "restored" / "rummagarr.log")
    assert written == ["config.toml"]
    assert restored_config.read_text(encoding="utf-8") == "restored = true\n"
    assert not outside.exists()

    again_log = tmp_path / "again" / "rummagarr.log"
    again = restore_backup(dest, config_path=tmp_path / "again" / "config.toml", state_db=tmp_path / "again" / "state.sqlite3", log_file=again_log)
    assert "state.sqlite3" in again
    assert "state-browse.sqlite3" in again
    assert "rummagarr.log" in again
    assert again_log.read_text(encoding="utf-8") == "hello\n"
    check = sqlite3.connect(tmp_path / "again" / "state.sqlite3")
    assert check.execute("SELECT k FROM meta").fetchone()[0] == "keep"
    check.close()

    legacy = tmp_path / "legacy.zip"
    with zipfile.ZipFile(legacy, "w") as archive:
        archive.writestr("lidarr-soulseek.log", "old log\n")
        archive.writestr("config.toml", "kept = true\n")
    legacy_log = tmp_path / "legacy" / "rummagarr.log"
    restored_legacy = restore_backup(
        legacy,
        config_path=tmp_path / "legacy" / "config.toml",
        state_db=tmp_path / "legacy" / "state.sqlite3",
        log_file=legacy_log,
    )
    assert "lidarr-soulseek.log" in restored_legacy
    assert legacy_log.read_text(encoding="utf-8") == "old log\n"


def test_changelog_mentions_browse_and_the_donate_placeholder():
    text = Path("CHANGELOG.md").read_text(encoding="utf-8")
    section = changelog_section(text, "1.15.0")
    assert "Browse" in section
    assert "Setup check" in section
    assert "Donate" in section
    assert "1.14.0" not in section
