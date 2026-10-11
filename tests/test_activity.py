from __future__ import annotations

import asyncio

from activity import Step, describe_step, format_status_line
from tests.conftest import album, make_worker
from wanted import build_catalog


def test_status_phrases_cover_the_real_steps():
    searching = describe_step(
        Step(kind="searching", artist="Ella Langley", album="Hungover", query_index=2, query_total=3, result_count=47)
    )
    assert searching == 'Searching Soulseek: "Ella Langley – Hungover" (query 2 of 3, 47 results so far)'
    assert "Idle" not in searching
    waiting = describe_step(Step(kind="waiting", artist="Ella Langley", album="Hungover", waited_seconds=12))
    assert waiting == "Waiting for search results (12s) · Ella Langley – Hungover"
    assert describe_step(Step(kind="matching", artist="Ella Langley", album="Hungover")) == "Matching results · Ella Langley – Hungover"
    assert describe_step(Step(kind="queued", file_total=4, username="vinylqueen")) == "Queued 4 files from user vinylqueen"
    assert describe_step(Step(kind="downloading", artist="Ella Langley", album="Hungover", file_index=3, file_total=12)) == "Downloading 3 of 12 · Ella Langley – Hungover"
    assert describe_step(Step(kind="retry", retry_seconds=300)) == "Waiting for retry period (next artist in 5m)"
    assert describe_step(Step(kind="idle")) == "Idle – nothing selected"
    assert describe_step(Step(kind="stopped")) == "Stopped"
    paused = format_status_line(
        {"run_state": "paused", "logged_in": True, "activity": "Paused", "elapsed": 0, "busy": False}
    )
    assert paused.startswith("Paused · ")


def test_search_wait_is_not_reported_as_idle(tmp_path):
    worker = make_worker(tmp_path)
    try:
        worker.control.start()

        class Request:
            def __init__(self) -> None:
                self.results = [type("Result", (), {"shared_items": [1, 2, 3]})()]

        signal = asyncio.run(
            worker._await_results(Request(), "album:1", 0.05, "Ella Langley", "Hungover", 2, 3)
        )
        assert signal == "ok"
        text, _elapsed, busy = worker.activity_view()
        assert signal == "ok"
        assert busy
        assert text.startswith("Waiting for search results")
        assert "Ella Langley" in text
        assert "Idle" not in text
        worker.control.pause()
        paused, _elapsed, paused_busy = worker.activity_view()
        assert paused.startswith("Waiting for search results")
        assert paused_busy is False
        line = format_status_line(worker.ui_snapshot())
        assert line.startswith("Paused · ")
        assert "Idle" not in line
    finally:
        worker.close()


def test_nothing_selected_is_the_only_idle_state(tmp_path):
    worker = make_worker(tmp_path)
    try:
        worker.control.start()
        worker.store._catalog_ready = True
        asyncio.run(worker.cycle())
        text, _elapsed, busy = worker.activity_view()
        assert text == "Idle – nothing selected"
        assert busy is False

        items = build_catalog([album(1, "Ella Langley", "Hungover", files=0, total=4)], [])
        worker.catalog.replace(items)
        worker.catalog.synced = True
        worker.store._catalog_ready = False
        worker.store.set_artist_cooldown("Ella Langley", 1)
        asyncio.run(worker.cycle())
        text, _elapsed, busy = worker.activity_view()
        assert text.startswith("Waiting for retry period")
        assert "Idle" not in text
        assert busy
    finally:
        worker.close()
