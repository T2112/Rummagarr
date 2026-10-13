"""Track lookups follow Lidarr's v1 routes and skip albums that did not change."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from lidarr_client import LidarrClient
from scripts.bench_network import PER_ARTIST, Catalog, Server, measure
from state import JobStore


def test_initial_sync_uses_one_artist_request_and_resync_skips_tracks(tmp_path):
    catalog = Catalog(8, delay=0)
    server = Server(catalog)
    store = JobStore(tmp_path / "state.sqlite3")
    client = LidarrClient(server.base_url, "test", track_concurrency=6)
    try:
        initial = measure(catalog, client, store)
        assert initial["wanted"] == 1
        assert initial["artist"] == 2
        assert initial["album"] == 0
        assert store.wanted_kind_count("album") == 8
        assert store.wanted_kind_count("song") > 0

        quiet = measure(catalog, client, store)
        assert quiet["wanted"] == 1
        assert quiet["artist"] == 0
        assert quiet["album"] == 0
        assert quiet["rows"] == initial["rows"]

        catalog.changed = {1, 5}
        changed = measure(catalog, client, store)
        assert changed["wanted"] == 1
        assert changed["artist"] == 0
        assert changed["album"] == 2
        renamed, _total = store.wanted_page(
            kinds=("album",), search="", order="album", reverse=False, limit=10, offset=0
        )
        assert any(row[3] == "Changed 1" for row in renamed)
    finally:
        client.close()
        store.close()
        server.close()


def test_parallel_artist_lookups_overlap(tmp_path):
    albums = PER_ARTIST * 6
    catalog = Catalog(albums, delay=0.05)
    server = Server(catalog)
    store = JobStore(tmp_path / "state.sqlite3")
    client = LidarrClient(server.base_url, "test", track_concurrency=6)
    try:
        initial = measure(catalog, client, store)
        assert initial["wanted"] == 1
        assert initial["artist"] == 6
        assert initial["album"] == 0
        assert initial["peak"] >= 4
        assert initial["seconds"] < 0.45
    finally:
        client.close()
        store.close()
        server.close()


def test_track_lookup_retries_a_503(monkeypatch, tmp_path):
    monkeypatch.setattr("lidarr_client.time.sleep", lambda _seconds: None)
    catalog = Catalog(4, delay=0)
    fails = {"left": 1}

    class Flaky(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path.endswith("/track") and fails["left"]:
                fails["left"] -= 1
                raw = b"{}"
                self.send_response(503)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if parsed.path.endswith("/wanted/missing"):
                body = {
                    "records": [
                        {
                            "id": index + 1,
                            "title": f"Album {index + 1}",
                            "artistId": 1,
                            "artist": {"id": 1, "artistName": "Artist"},
                            "monitored": True,
                            "statistics": {"trackCount": 1, "trackFileCount": 0, "totalTrackCount": 1},
                        }
                        for index in range(4)
                    ]
                }
            else:
                body = [
                    {
                        "id": album_id,
                        "albumId": album_id,
                        "title": "Song",
                        "monitored": True,
                        "hasFile": False,
                        "trackNumber": 1,
                    }
                    for album_id in range(1, 5)
                ]
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Flaky)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    store = JobStore(tmp_path / "state.sqlite3")
    client = LidarrClient(f"http://{host}:{port}", "test", track_concurrency=1)
    try:
        result = measure(catalog, client, store)
        assert fails["left"] == 0
        assert result["rows"] > 4
        assert store.wanted_kind_count("song") == 4
    finally:
        client.close()
        store.close()
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_album_at_a_time_baseline_requests_one_track_per_album(tmp_path):
    from scripts.bench_network import AlbumAtATime

    catalog = Catalog(3, delay=0)
    server = Server(catalog)
    store = JobStore(tmp_path / "state.sqlite3")
    client = AlbumAtATime(LidarrClient(server.base_url, "test", track_concurrency=1))
    try:
        initial = measure(catalog, client, store)
        assert initial["wanted"] == 1
        assert initial["album"] == 3
        assert initial["artist"] == 0
        assert initial["peak"] == 1
        started = time.perf_counter()
        quiet = measure(catalog, client, store)
        assert time.perf_counter() - started >= 0
        assert quiet["album"] == 0
        assert quiet["artist"] == 0
    finally:
        client.close()
        store.close()
        server.close()


def test_a_dead_host_is_lidarr_unreachable(monkeypatch):
    from lidarr_client import LidarrUnreachable

    monkeypatch.setattr("lidarr_client.time.sleep", lambda _seconds: None)
    client = LidarrClient("http://127.0.0.1:9", "test", timeout=1)
    try:
        try:
            client.ping()
        except LidarrUnreachable as exc:
            assert str(exc) == "Lidarr unreachable"
        else:
            raise AssertionError("expected Lidarr unreachable")
    finally:
        client.close()


def test_artist_id_uses_lookup_and_queue_failures_are_named():
    client = LidarrClient("http://127.0.0.1:9", "test", timeout=1)
    seen = {}

    def get(path, params=None):
        seen["path"] = path
        return [{"artist": {"id": 4, "artistName": "Ella Langley"}}]

    class Response:
        status_code = 200

        def json(self):
            return {
                "records": [
                    {
                        "title": "Ella Langley - Hungover",
                        "trackedDownloadStatus": "warning",
                        "statusMessages": [{"messages": ["Found a release but the track count does not match"]}],
                    }
                ]
            }

    client._get = get
    client._client.get = lambda *args, **kwargs: Response()
    try:
        assert client.artist_id_by_name("Ella Langley") == 4
        assert seen["path"] == "/search"
        failures = client.import_failures("Ella Langley")
        assert failures
        assert "track count" in failures[0].casefold()
    finally:
        client.close()
