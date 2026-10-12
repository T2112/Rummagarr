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
    assert describe_step(Step(kind="retry", retry_seconds=0)) == "Starting the next artist"
    assert (
        describe_step(Step(kind="retry", retry_kind="artist", artist="Ella Langley", retry_seconds=172800))
        == "Waiting for artist retry period (Ella Langley in 2d)"
    )
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
        # The real remaining time, not the old 30s cap that restarted the countdown.
        assert seconds >= 2 * 86400 - 60
        worker._note_between_passes()
        text, _elapsed, _busy = worker.activity_view()
        assert text.startswith("Waiting for artist retry period")
        assert "Ella Langley" in text
        assert "5m" not in text
        assert "next artist in 5s" not in text
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
        _disarm(worker)
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
        _disarm(worker)


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
        assert "Ella Langley" in text
        assert "Idle" not in text
        assert busy
    finally:
        worker.close()


class _Session:
    session = object()


class _Library:
    def __init__(self, albums, tracks=None):
        self._albums = albums
        self._tracks = tracks or {}

    def wanted_missing(self, page_size=250, start_page=1, max_pages=1, on_page=None, should_cancel=None):
        return self._albums, 1, True

    def tracks_for_album(self, album_id: int):
        return list(self._tracks.get(int(album_id), []))


def _sync(worker, albums, tracks=None):
    from wanted import sync_wanted_catalog

    sync_wanted_catalog(_Library(albums, tracks), worker.store, max_pages=2)
    assert worker.store.wanted_catalog_ready()


def _arm_search(worker):
    """Record FileSearch queries and stop before a download."""
    from worker import SearchNotSent

    worker.control.start()
    worker.client = _Session()
    found: list[str] = []

    async def send(query):
        worker._cycle_attempted_search = True
        found.append(query)
        raise SearchNotSent("stub")

    worker._send_search = send

    class Lidarr:
        def tracks_for_album(self, album_id):
            return [{"id": int(album_id) * 10, "title": "Blue", "monitored": True, "hasFile": False, "trackNumber": 1}]

    worker._real_lidarr = worker.lidarr
    worker.lidarr = Lidarr()
    return found


def _disarm(worker) -> None:
    worker.client = None
    real = getattr(worker, "_real_lidarr", None)
    if real is not None:
        worker.lidarr = real
    worker.close()


def _one_pass(worker) -> int:
    worker._cycle_attempted_search = False

    async def run():
        await worker.cycle()
        return worker._pause_after_cycle()

    return asyncio.run(run())


def test_nothing_to_search_does_not_restart_the_five_second_wait(tmp_path):
    """An artist row with no albums still sorted first and looked ready.

    The pass sent no FileSearch and wrote no cooldown, so "next artist in 5s"
    started over forever and the artist who does have a missing album was never
    searched.
    """
    worker = make_worker(tmp_path)
    try:
        found = _arm_search(worker)
        worker.cfg.cycle_seconds = 5
        ella = album(2, "Ella Langley", "Hungover", files=0, total=4, artist_id=2)
        _sync(worker, [ella], {2: [{"id": 21, "title": "Blue", "monitored": True, "hasFile": False, "trackNumber": 1}]})
        worker.store._db.execute(
            """
            INSERT INTO wanted_artists(
                artist_key, artist, sort_artist, albums, songs,
                disabled_albums, disabled_songs, disabled_children, enabled
            ) VALUES ('aaa phantom', 'Aaa Phantom', 'aaa phantom', 0, 0, 0, 0, 0, 1)
            """
        )
        worker.store._db.commit()
        assert worker.store.next_wanted_artist() == "Ella Langley"
        pause = _one_pass(worker)
        assert found
        assert "ella langley" in found[0].casefold()
        assert "phantom" not in found[0].casefold()
        text, _elapsed, _busy = worker.activity_view()
        assert "next artist in 5s" not in text
        assert text.startswith("Waiting for artist retry period")
        assert "Ella Langley" in text
        assert pause > 3600
    finally:
        _disarm(worker)


def test_partial_album_without_songs_does_not_block_the_next_artist(tmp_path):
    worker = make_worker(tmp_path)
    try:
        found = _arm_search(worker)
        worker.cfg.cycle_seconds = 5
        partial = album(1, "Aaa First", "Half", files=3, total=10, artist_id=1)
        ella = album(2, "Ella Langley", "Hungover", files=0, total=4, artist_id=2)
        _sync(
            worker,
            [partial, ella],
            {2: [{"id": 21, "title": "Blue", "monitored": True, "hasFile": False, "trackNumber": 1}]},
        )
        pause = _one_pass(worker)
        assert found
        assert "ella langley" in found[0].casefold()
        assert "aaa first" not in found[0].casefold()
        assert "aaa first" not in worker.store.artist_cooldowns()
        text, _elapsed, _busy = worker.activity_view()
        assert "next artist in 5s" not in text
        assert "Aaa First" in text
        assert pause > 60
    finally:
        _disarm(worker)


def test_imported_album_is_not_marked_visited(tmp_path):
    worker = make_worker(tmp_path)
    try:
        found = _arm_search(worker)
        worker.cfg.cycle_seconds = 5
        done = album(1, "Aaa First", "Done", files=0, total=4, artist_id=1)
        ella = album(2, "Ella Langley", "Hungover", files=0, total=4, artist_id=2)
        _sync(
            worker,
            [done, ella],
            {
                1: [{"id": 11, "title": "Old", "monitored": True, "hasFile": False, "trackNumber": 1}],
                2: [{"id": 21, "title": "Blue", "monitored": True, "hasFile": False, "trackNumber": 1}],
            },
        )
        worker.store.upsert(1, "Aaa First", "Done", "imported", "already complete")
        pause = _one_pass(worker)
        assert found
        assert "ella langley" in found[0].casefold()
        assert "aaa first" not in worker.store.artist_cooldowns()
        text, _elapsed, _busy = worker.activity_view()
        assert "next artist in 5s" not in text
        assert pause != 5
    finally:
        _disarm(worker)


def test_legacy_artist_prefix_cooldowns_expire_on_the_real_clock(tmp_path):
    """Older builds stored "artist:" plus the name. Those keys never matched."""
    import json
    from datetime import datetime, timedelta, timezone

    worker = make_worker(tmp_path)
    try:
        found = _arm_search(worker)
        worker.cfg.cycle_seconds = 5
        first = album(1, "Aaa First", "One", files=0, total=4, artist_id=1)
        ella = album(2, "Ella Langley", "Hungover", files=0, total=4, artist_id=2)
        _sync(worker, [first, ella])
        until = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        worker.store.set_meta(
            "artist_cooldowns",
            json.dumps({"artist:aaa first": until, "artist:ella langley": until}),
        )
        assert worker._next_ready_artist() is None
        pause = _one_pass(worker)
        assert found == []
        assert pause >= 2 * 86400 - 60
        text, _elapsed, _busy = worker.activity_view()
        assert text.startswith("Waiting for artist retry period")
        assert "Aaa First" in text
        assert "next artist in 5s" not in text
        assert "5s" not in text
    finally:
        _disarm(worker)


def test_countdown_reaches_zero_then_searches_the_next_artist(tmp_path):
    import time

    worker = make_worker(tmp_path)
    try:
        found = _arm_search(worker)
        worker.cfg.cycle_seconds = 5
        first = album(1, "Aaa First", "One", files=0, total=4, artist_id=1)
        ella = album(2, "Ella Langley", "Hungover", files=0, total=4, artist_id=2)
        tracks = {
            1: [{"id": 11, "title": "Old", "monitored": True, "hasFile": False, "trackNumber": 1}],
            2: [{"id": 21, "title": "Blue", "monitored": True, "hasFile": False, "trackNumber": 1}],
        }
        _sync(worker, [first, ella], tracks)
        pause = _one_pass(worker)
        assert found and "aaa first" in found[0].casefold()
        assert pause == 5
        text, _elapsed, _busy = worker.activity_view()
        assert text.startswith("Waiting for retry period (next artist in ")
        assert text.endswith("5s)") or text.endswith("4s)")
        worker.step.retry_deadline = time.monotonic()
        text, _elapsed, _busy = worker.activity_view()
        assert text == "Starting the next artist"
        _one_pass(worker)
        assert any("ella langley" in query.casefold() for query in found)
    finally:
        _disarm(worker)


def test_same_artist_without_a_search_waits_instead_of_spinning(tmp_path):
    from sharing import IDLE_FLOOR_SECONDS

    worker = make_worker(tmp_path)
    try:
        worker.control.start()
        worker.cfg.cycle_seconds = 0

        async def offline():
            return False

        worker.ensure_soulseek = offline
        ella = album(2, "Ella Langley", "Hungover", files=0, total=4, artist_id=2)
        _sync(worker, [ella], {2: [{"id": 21, "title": "Blue", "monitored": True, "hasFile": False, "trackNumber": 1}]})
        pause = _one_pass(worker)
        assert pause == IDLE_FLOOR_SECONDS
        assert worker.store.artist_cooldowns() == {}
        text, _elapsed, _busy = worker.activity_view()
        assert text.startswith("Waiting for retry period (next artist in ")
        assert "Starting the next artist" != text
    finally:
        worker.close()
