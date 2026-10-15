"""Rummagarr 1.16.0: browse download limits and the stats page."""

from __future__ import annotations

import asyncio
from pathlib import Path

from changelog_notes import changelog_section
from peer_limits import apply_limits, select_under_cap, share_file_rows
from sharing import WantedMatch
from stats_report import format_stats
from tests.conftest import make_worker


def _match(name: str, size: int) -> WantedMatch:
    return WantedMatch(
        username="goodpeer",
        filename=name,
        size=size,
        artist="Ella Langley",
        album="Hungover",
        song="Blue",
        key="song:1",
        kind="song",
        album_id=1,
        track_id=1,
        score=0.9,
    )


def test_profile_limits_lose_when_rummagarr_is_stricter():
    text = "max 100 files. max 9 GB. wait 1 second between files."
    applied = apply_limits(text, permission=1, queue_length=0, our_files=25, our_mb=2048, our_pace=5)
    assert applied.allowed
    assert applied.max_files == 25
    assert applied.max_mb == 2048
    assert applied.pace_seconds == 5
    assert applied.files_at_once == 1
    assert any("your cap is stricter" in line for line in applied.lines)
    assert any("1 file at a time (your rule)" in line for line in applied.lines)


def test_profile_limits_win_when_they_are_stricter_and_uploads_can_block():
    text = "Please only 2 files. Max 500 MB. One album at a time. Wait 30 seconds between downloads."
    applied = apply_limits(text, permission=1, queue_length=1, our_files=25, our_mb=2048, our_pace=5)
    assert applied.allowed
    assert applied.max_files == 2
    assert applied.max_mb == 500
    assert applied.pace_seconds == 30
    assert applied.files_at_once == 1
    joined = "\n".join(applied.lines)
    assert "their profile is stricter" in joined
    assert "your rule and their profile" in joined

    off = apply_limits(text, permission=0, queue_length=0, our_files=25, our_mb=2048)
    assert not off.allowed
    assert "Uploads are off" in off.reason
    listed = apply_limits("", permission=2, queue_length=0, our_files=25, our_mb=2048)
    assert not listed.allowed
    assert "list" in listed.reason
    full = apply_limits("queue under 5", permission=1, queue_length=5, our_files=25, our_mb=2048)
    assert not full.allowed
    assert "queue" in full.reason.lower()


def test_file_cap_skips_the_rest_and_share_rows_are_flat():
    matches = [_match("a.flac", 100), _match("b.flac", 100), _match("big.flac", 5_000_000)]
    chosen, skipped = select_under_cap(matches, max_files=2, max_bytes=1000)
    assert [item.filename for item in chosen] == ["a.flac", "b.flac"]
    assert skipped == 1
    rows = share_file_rows(
        "goodpeer",
        [
            {"name": "Music", "files": [{"filename": "a.flac", "size": 10, "extension": "flac"}]},
            {"name": "Music\\Hungover", "files": [{"filename": "a.flac", "size": 10}, {"filename": "b.flac", "size": 4}]},
        ],
    )
    assert [row["filename"] for row in rows] == ["a.flac", "b.flac"]


def test_paced_download_is_one_at_a_time_and_stats_count_it(tmp_path: Path):
    worker = make_worker(tmp_path)
    calls = []

    class Transfers:
        async def download(self, username, filename):
            calls.append((username, filename))
            return type("Transfer", (), {"state": "COMPLETE", "filename": filename, "username": username})()

    class Client:
        session = object()
        transfers = Transfers()

    try:
        worker.client = Client()
        worker.cfg.dry_run = False
        worker.control.start()
        first = asyncio.run(
            worker.download_paced(
                [
                    {"username": "goodpeer", "filename": "a.flac", "link": {"artist": "Ella", "album": "Hungover", "kind": "song", "title": "Blue"}},
                    {"username": "goodpeer", "filename": "b.flac", "link": {"artist": "Ella", "album": "Hungover", "kind": "song", "title": "Still"}},
                ],
                pace_seconds=0,
                files_at_once=1,
            )
        )
        assert calls == [("goodpeer", "a.flac"), ("goodpeer", "b.flac")]
        assert "2 of 2" in first
        assert "1 at a time" in first
        worker._paced_busy = True
        busy = asyncio.run(worker.download_paced([{"username": "goodpeer", "filename": "c.flac"}], pace_seconds=0))
        assert "already running" in busy
        assert calls == [("goodpeer", "a.flac"), ("goodpeer", "b.flac")]
        worker._paced_busy = False
        summary = worker.store.activity_summary()
        assert summary["download_total"] >= 2
        assert summary["by_user"][0][0] == "goodpeer"
        text = format_stats(summary, worker.store.upload_totals(), worker.store.get_meta("last_browse_download") or "")
        assert "Downloads" in text
        assert "goodpeer" in text
        assert "Last browse download" in text
        worker.cfg.dry_run = True
        note = asyncio.run(worker.download_paced([{"username": "goodpeer", "filename": "d.flac"}], pace_seconds=0))
        assert note.startswith("dry run")
        assert calls == [("goodpeer", "a.flac"), ("goodpeer", "b.flac")]
    finally:
        worker.close()


def test_changelog_mentions_the_limits_popup_and_stats():
    section = changelog_section(Path("CHANGELOG.md").read_text(encoding="utf-8"), "1.16.0")
    assert "Download all missing" in section
    assert "stricter" in section
    assert "Stats" in section
    assert "1.15.0" not in section
