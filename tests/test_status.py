from __future__ import annotations

import asyncio
import json
import threading
import urllib.request

from status_ui import DEFAULT_RECENT_LIMIT, PAGE, RECENT_LIMITS, normalize_recent_limit, start_status_ui
from tests.conftest import make_worker


def test_recent_limit_choices_match_the_page():
    assert RECENT_LIMITS == (10, 25, 50, 100, 250)
    assert DEFAULT_RECENT_LIMIT == 10
    assert normalize_recent_limit("25") == 25
    assert normalize_recent_limit("15") == 10
    assert normalize_recent_limit(None) == 10
    for number in RECENT_LIMITS:
        assert f">{number}<" in PAGE


def test_status_page_limit_and_cancel_route(tmp_path):
    worker = make_worker(tmp_path)
    for index in range(30):
        worker.store.add_completed_download("A", "B", f"{index}.mp3", "peer", "album")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    threading.Thread(target=loop.run_forever, name="status-test-loop", daemon=True).start()
    server = start_status_ui(worker, "127.0.0.1", 0)
    port = server.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status?limit=25") as response:
            payload = json.load(response)
        assert len(payload["recent"]) == 25
        assert payload["recent"][0]["filename"] == "29.mp3"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as response:
            page = response.read().decode("utf-8")
        assert "Show" in page
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/cancel",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            assert json.load(response)["ok"] is True
        assert worker.cancel_requested is True
    finally:
        server.shutdown()
        server.server_close()
        loop.call_soon_threadsafe(loop.stop)
        worker.close()
