"""Peer search replies must show up for manual and automatic search."""

from __future__ import annotations

import asyncio
import socket
import time
from pathlib import Path

from aioslsk.shares.model import SharedItem
from search_model import files_from_results
from tests.conftest import make_worker
from tests.mock_soulseek import MockSoulseek
from worker import build_slsk_settings


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _point_at_mock(cfg, port: int):
    settings = build_slsk_settings(cfg)
    settings.network.server.hostname = "127.0.0.1"
    settings.network.server.port = port
    settings.network.upnp.enabled = False
    settings.network.server.reconnect.auto = False
    return settings


async def _login(tmp_path: Path, mock: MockSoulseek, monkeypatch):
    worker = make_worker(tmp_path)
    worker.cfg.listen_port = _free_port()
    worker.cfg.obfuscated_port = _free_port()
    worker.cfg.enable_upnp = False
    worker.cfg.search_wait_seconds = 3
    # These limit uploads. They must not hide search results.
    worker.cfg.require_peer_share = True
    worker.cfg.min_peer_share_files = 50_000
    worker.control.start()
    monkeypatch.setattr("worker.build_slsk_settings", lambda cfg: _point_at_mock(cfg, mock.port))
    await worker.start_soulseek()
    clear, obfuscated = worker.client.network.get_listening_ports()
    mock.listen_port = clear
    mock.obfuscated_port = obfuscated
    return worker


async def _wait_for_file(request, seconds: float = 3.0) -> list:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = files_from_results(getattr(request, "results", None))
        if found:
            return found
        await asyncio.sleep(0.05)
    return files_from_results(getattr(request, "results", None))


def test_peer_reply_is_listed_for_manual_and_automatic_search(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO", logger="lidarr_slsk")

    async def scenario():
        mock = MockSoulseek()
        await mock.start()
        worker = None
        try:
            worker = await _login(tmp_path, mock, monkeypatch)
            manual = await worker.begin_manual_search("ella langley hungover")
            manual_files = await _wait_for_file(manual)
            worker.note_search_receipt("ella langley hungover", manual.results)
            auto = await worker._send_search("ella langley hungover")
            auto_files = await _wait_for_file(auto)
            worker.note_search_receipt("ella langley hungover", auto.results)
            return manual_files, auto_files, manual.results
        finally:
            if worker is not None:
                await worker.stop_soulseek()
                worker.close()
            await mock.close()

    manual_files, auto_files, results = asyncio.run(scenario())
    assert [item.filename for item in manual_files] == [r"music\Ella Langley\Hungover\01 Blue.flac"]
    assert [item.filename for item in auto_files] == [r"music\Ella Langley\Hungover\01 Blue.flac"]
    assert "locked.mp3" not in " ".join(item.filename for item in manual_files)
    assert "1 peer replies, 1 files shown, 1 locked files not shown" in caplog.text
    assert len(results) == 1


def test_obfuscated_peer_reply_is_listed(tmp_path, monkeypatch):
    async def scenario():
        mock = MockSoulseek()
        mock.mode = "obfuscated"
        await mock.start()
        worker = None
        try:
            worker = await _login(tmp_path, mock, monkeypatch)
            request = await worker.begin_manual_search("ella langley hungover")
            return await _wait_for_file(request)
        finally:
            if worker is not None:
                await worker.stop_soulseek()
                worker.close()
            await mock.close()

    files = asyncio.run(scenario())
    assert [item.filename for item in files] == [r"music\Ella Langley\Hungover\01 Blue.flac"]


def test_share_scan_yields_so_a_peer_reply_is_not_dropped(tmp_path, monkeypatch):
    """A slow term-map build must not hold the loop until the reply is too late."""
    progress = {"n": 0}
    total = 500

    def fake_scan(shared_directory, children=None):
        found = set()
        for index in range(total):
            item = SharedItem(shared_directory, "", f"track-{index}.mp3", 1.0)
            item.attributes = []
            found.add(item)
        return found

    from aioslsk.shares.manager import SharesManager

    original = SharesManager._add_item_to_term_map

    def slow(self, item):
        time.sleep(0.002)
        progress["n"] += 1
        return original(self, item)

    monkeypatch.setattr("aioslsk.shares.manager.scan_directory", fake_scan)
    monkeypatch.setattr(SharesManager, "_add_item_to_term_map", slow)

    async def scenario():
        mock = MockSoulseek()
        await mock.start()
        worker = None
        try:
            share = tmp_path / "library"
            share.mkdir()
            worker = make_worker(tmp_path)
            worker.cfg.share_dir = str(share)
            worker.cfg.listen_port = _free_port()
            worker.cfg.obfuscated_port = _free_port()
            worker.cfg.enable_upnp = False
            worker.cfg.require_peer_share = True
            worker.cfg.min_peer_share_files = 50_000
            worker.control.start()
            monkeypatch.setattr("worker.build_slsk_settings", lambda cfg: _point_at_mock(cfg, mock.port))
            await worker.start_soulseek()
            clear, obfuscated = worker.client.network.get_listening_ports()
            mock.listen_port = clear
            mock.obfuscated_port = obfuscated
            request = await worker.begin_manual_search("ella langley hungover")

            async def watch() -> int:
                while not request.results:
                    await asyncio.sleep(0)
                return progress["n"]

            indexed_when_seen = await asyncio.wait_for(watch(), timeout=8)
            return request.results, indexed_when_seen
        finally:
            if worker is not None:
                await worker.stop_soulseek()
                worker.close()
            await mock.close()

    results, indexed_when_seen = asyncio.run(scenario())
    found = files_from_results(results)
    assert [item.filename for item in found] == [r"music\Ella Langley\Hungover\01 Blue.flac"]
    assert indexed_when_seen < total


def test_search_receipt_separates_no_answer_from_filtered(caplog):
    caplog.set_level("INFO", logger="lidarr_slsk")
    from tests.test_manual_search import Item, Result
    from worker import Worker

    class Quiet:
        def note(self, query, results):
            return Worker.note_search_receipt(self, query, results)

    host = Quiet()
    host.note("nobody", [])
    locked = type("Locked", (), {"filename": "secret.flac"})()
    host.note(
        "ella",
        [Result("vinylqueen", [Item(r"music\Ella\01.flac", 10, 900, 10, "flac")], free=True, speed=1, queue=0)],
    )
    # A reply whose only files are locked is not an empty search.
    reply = Result("vinylqueen", [], free=True, speed=1, queue=0)
    reply.locked_results = [locked]
    host.note("locked-only", [reply])
    text = caplog.text
    assert "Search 'nobody': 0 peer replies, 0 files shown, 0 locked files not shown" in text
    assert "Search 'ella': 1 peer replies, 1 files shown, 0 locked files not shown" in text
    assert "Search 'locked-only': 1 peer replies, 0 files shown, 1 locked files not shown" in text
