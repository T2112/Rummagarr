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


def test_zero_wait_starts_the_next_artist_without_the_five_minute_notice(tmp_path):
    worker = make_worker(tmp_path)
    try:
        worker.control.start()
        items = build_catalog([album(1, "Ella Langley", "Hungover", files=0, total=4)], [])
        worker.catalog.replace(items)
        worker.catalog.synced = True
        worker.store._catalog_ready = False
        worker.cfg.cycle_seconds = 0
        assert worker._next_ready_artist() == "Ella Langley"
        seconds, kind = worker.between_pass_wait()
        assert seconds == 0
        assert kind == "pass"
        worker._note_between_passes()
        text, _elapsed, _busy = worker.activity_view()
        assert text == "Starting the next artist"
        assert "Waiting for retry period" not in text
        assert "5m" not in text

        worker.store.set_artist_cooldown("Ella Langley", 2)
        seconds, kind = worker.between_pass_wait()
        assert kind == "artist"
        assert 1 <= seconds <= 30
        worker._note_between_passes()
        text, _elapsed, _busy = worker.activity_view()
        assert text.startswith("Waiting for artist retry period")
        assert "5m" not in text
    finally:
        worker.close()


def test_cooled_artists_are_not_ready_when_the_wait_is_zero(tmp_path):
    """A zero wait must not treat a cooled artist as the next one to search.

    The saved list keys artists by the casefolded name. Skipping "artist:" plus
    that name matches nobody, so a zero wait tight-loops and the FileSearch
    never gets a turn on the event loop.
    """
    from wanted import sync_wanted_catalog

    class Library:
        def wanted_missing(self, page_size=250, start_page=1, max_pages=1, on_page=None, should_cancel=None):
            return [album(1, "Ella Langley", "Hungover", files=0, total=4)], 1, True

        def tracks_for_album(self, album_id: int):
            return [{"id": 11, "title": "Blue", "monitored": True, "hasFile": False, "trackNumber": 1}]

    worker = make_worker(tmp_path)
    try:
        worker.control.start()
        sync_wanted_catalog(Library(), worker.store, max_pages=2)
        worker.cfg.cycle_seconds = 0
        assert worker.store.wanted_catalog_ready()
        assert worker._next_ready_artist() == "Ella Langley"
        worker.store.set_artist_cooldown("Ella Langley", 2)
        assert worker._next_ready_artist() is None
        seconds, kind = worker.between_pass_wait()
        assert kind == "artist"
        assert seconds >= 1
        asyncio.run(worker.cycle())
        text, _elapsed, _busy = worker.activity_view()
        assert text.startswith("Waiting for artist retry period")
        assert "Starting the next artist" not in text
    finally:
        worker.close()


def test_zero_wait_lets_another_task_run(tmp_path):
    worker = make_worker(tmp_path)

    async def scenario():
        worker.control.start()
        ran = asyncio.Event()

        async def other():
            ran.set()

        asyncio.create_task(other())
        for _ in range(20):
            await worker.control.gate(sleep=0)
        await asyncio.wait_for(ran.wait(), timeout=1)

    try:
        asyncio.run(scenario())
    finally:
        worker.close()


def test_search_send_timeout_reports_the_session(tmp_path, monkeypatch):
    import time

    from worker import SearchNotSent

    worker = make_worker(tmp_path)

    class Searches:
        async def search(self, query):
            await asyncio.sleep(30)

    class Client:
        session = object()
        searches = Searches()

    async def scenario():
        worker.client = Client()
        worker._loop = asyncio.get_running_loop()
        worker._loop_beat = time.monotonic()
        try:
            await worker._send_search("nsync")
        except SearchNotSent as exc:
            return str(exc)
        return ""

    monkeypatch.setattr("worker._SEARCH_SEND_TIMEOUT", 0.05)
    try:
        text = asyncio.run(scenario())
    finally:
        worker.client = None
        worker.close()
    assert "did not accept" in text
    assert "login=yes" in text
    assert "loop_lag=" in text


def test_connected_requires_a_live_server(tmp_path):
    from aioslsk.network.connection import ConnectionState

    worker = make_worker(tmp_path)

    class Server:
        def __init__(self, state):
            self.state = state

    class Network:
        def __init__(self, state):
            self.server_connection = Server(state)

        def get_listening_ports(self):
            return (2234, 2235)

    class Client:
        def __init__(self, state):
            self.session = object()
            self.network = Network(state)
            self.distributed_network = None

    try:
        worker._loop = None
        worker.client = Client(ConnectionState.CLOSED)
        responsive, label = worker.soulseek_link()
        assert responsive is False
        assert "session is down" in label
        snapshot = worker.ui_snapshot()
        assert snapshot["logged_in"] is False
        assert "login=yes" in snapshot["soulseek_detail"]
        assert "server=CLOSED" in snapshot["soulseek_detail"]
        worker.client = Client(ConnectionState.CONNECTED)
        responsive, label = worker.soulseek_link()
        assert responsive is True
        assert label == "Soulseek connected"
    finally:
        worker.client = None
        worker.close()


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
        assert text.startswith("Waiting for artist retry period")
        assert "Idle" not in text
        assert busy
    finally:
        worker.close()
