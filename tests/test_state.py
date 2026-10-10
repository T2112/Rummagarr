from __future__ import annotations

from state import COMPLETED_DOWNLOAD_LIMIT, JobStore
from status_ui import RECENT_LIMITS


def test_recent_history_keeps_the_largest_status_choice(tmp_path):
    assert COMPLETED_DOWNLOAD_LIMIT >= max(RECENT_LIMITS)
    store = JobStore(tmp_path / "state.sqlite3")
    try:
        for index in range(COMPLETED_DOWNLOAD_LIMIT + 10):
            store.add_completed_download("A", "B", f"file-{index}.mp3", "peer", "album")
        recent = store.recent_completed_downloads(COMPLETED_DOWNLOAD_LIMIT + 10)
        assert len(recent) == COMPLETED_DOWNLOAD_LIMIT
        assert recent[0]["filename"] == f"file-{COMPLETED_DOWNLOAD_LIMIT + 9}.mp3"
        assert store.recent_completed_downloads(10)[0]["filename"] == recent[0]["filename"]
    finally:
        store.close()


def test_wanted_choices_persist_and_unknown_keys_stay_enabled(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    try:
        assert store.wanted_enabled("album:5") is True
        store.remember_wanted_keys(["album:5", "song:8"])
        store.set_wanted_choices({"album:5": False})
        store.remember_wanted_keys(["album:5", "song:9"])
        assert store.wanted_choices()["album:5"] is False
        assert store.wanted_choices()["song:8"] is True
        assert store.wanted_choices()["song:9"] is True
        store.upsert(5, "A", "B", "skipped", "skipped")
        store.upsert_track(8, 5, "A", "T", "stopped", "stopped")
        assert store.should_skip(5, 12) is False
        assert store.should_skip_track(8, 12) is False
        store.upsert(5, "A", "B", "downloading", "mid")
        assert store.should_skip(5, 12) is True
    finally:
        store.close()
