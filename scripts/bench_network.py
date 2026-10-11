"""End-to-end wanted sync against a mock Lidarr with a fixed per-request delay.

The mock serves the v1 routes this sync uses:

- GET /api/v1/wanted/missing?page=&pageSize=
- GET /api/v1/track?albumId=        one album
- GET /api/v1/track?artistId=       every track for one artist

Lidarr's track route does not take a repeated albumId. The timed path uses
artistId when several albums by one artist need tracks.

Usage:
    python scripts/bench_network.py
    python scripts/bench_network.py --measure-albums 312195
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lidarr_client import LidarrClient
from state import JobStore
from wanted import sync_wanted_catalog

PER_ALBUM = 10
PER_ARTIST = 4
PAGE_SIZE = 250


class Catalog:
    def __init__(self, n_albums: int, delay: float) -> None:
        self.n_albums = n_albums
        self.delay = delay
        self.changed: set[int] = set()
        self.lock = threading.Lock()
        self.counts = {"wanted": 0, "album": 0, "artist": 0}
        self.inflight = 0
        self.peak = 0

    def reset_counts(self) -> None:
        with self.lock:
            self.counts = {"wanted": 0, "album": 0, "artist": 0}
            self.inflight = 0
            self.peak = 0

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return dict(self.counts) | {"peak": self.peak}


def artists_for(n_albums: int) -> int:
    full, extra = divmod(n_albums, PER_ARTIST)
    return full + (1 if extra else 0)


def pages_for(n_albums: int) -> int:
    return (n_albums + PAGE_SIZE - 1) // PAGE_SIZE


def album_record(catalog: Catalog, index: int) -> dict:
    album_id = index + 1
    artist_index = index // PER_ARTIST
    partial = index % 2 == 1
    files = 2 if partial else 0
    title = f"Changed {album_id}" if album_id in catalog.changed else f"Album {album_id}"
    return {
        "id": album_id,
        "title": title,
        "artistId": artist_index + 1,
        "artist": {"id": artist_index + 1, "artistName": f"Artist {artist_index:07d}"},
        "monitored": True,
        "releaseDate": "2020-01-01",
        "statistics": {
            "trackCount": PER_ALBUM,
            "trackFileCount": files,
            "totalTrackCount": PER_ALBUM,
            "sizeOnDisk": 0,
        },
    }


def tracks_for(catalog: Catalog, album_id: int) -> list[dict]:
    index = int(album_id) - 1
    if index < 0 or index >= catalog.n_albums:
        return []
    partial = index % 2 == 1
    files = 2 if partial else 0
    base = index * PER_ALBUM
    rows = []
    for number in range(PER_ALBUM):
        rows.append(
            {
                "id": base + number + 1,
                "albumId": int(album_id),
                "title": f"Track {number + 1}",
                "monitored": True,
                "hasFile": partial and number < files,
                "trackNumber": "A1" if number == 0 else number + 1,
            }
        )
    return rows


class Handler(BaseHTTPRequestHandler):
    catalog: Catalog
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        catalog = type(self).catalog
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        kind = "wanted"
        if parsed.path.endswith("/track"):
            kind = "artist" if "artistId" in query else "album"
        with catalog.lock:
            catalog.inflight += 1
            catalog.peak = max(catalog.peak, catalog.inflight)
            catalog.counts[kind] = catalog.counts.get(kind, 0) + 1
        try:
            if catalog.delay:
                time.sleep(catalog.delay)
            if parsed.path.endswith("/wanted/missing"):
                page = max(1, int(query.get("page", ["1"])[0]))
                page_size = max(1, int(query.get("pageSize", [str(PAGE_SIZE)])[0]))
                start = (page - 1) * page_size
                end = min(start + page_size, catalog.n_albums)
                records = [album_record(catalog, index) for index in range(start, end)] if start < catalog.n_albums else []
                body = {
                    "page": page,
                    "pageSize": page_size,
                    "totalRecords": catalog.n_albums,
                    "records": records,
                }
            elif parsed.path.endswith("/track") and "artistId" in query:
                artist_id = int(query["artistId"][0])
                first = (artist_id - 1) * PER_ARTIST
                body = []
                for index in range(first, min(first + PER_ARTIST, catalog.n_albums)):
                    body.extend(tracks_for(catalog, index + 1))
            elif parsed.path.endswith("/track") and "albumId" in query:
                body = tracks_for(catalog, int(query["albumId"][0]))
            else:
                self._send(404, {"error": "not found"})
                return
            self._send(200, body)
        finally:
            with catalog.lock:
                catalog.inflight -= 1

    def _send(self, code: int, body) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class QuickHTTPServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        super().server_bind()
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def get_request(self):
        sock, address = super().get_request()
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return sock, address


class Server:
    def __init__(self, catalog: Catalog) -> None:
        handler = type("BoundHandler", (Handler,), {"catalog": catalog})
        self.httpd = QuickHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()


class AlbumAtATime:
    """The previous sync: one album track request, and only one at a time."""

    def __init__(self, inner: LidarrClient) -> None:
        self._inner = inner
        self.track_concurrency = 1

    def wanted_missing(self, **kwargs):
        return self._inner.wanted_missing(**kwargs)

    def tracks_for_album(self, album_id: int):
        return self._inner.tracks_for_album(album_id)

    def close(self) -> None:
        self._inner.close()


def run_sync(client, store: JobStore) -> tuple[float, int, bool]:
    started = time.perf_counter()
    last = {"at": started}

    def progress(rows: int, albums: int) -> None:
        now = time.perf_counter()
        if now - last["at"] < 10:
            return
        last["at"] = now
        print(f"progress rows={rows} albums={albums} elapsed={now - started:.0f}s", flush=True)

    count, exhausted, cancelled = sync_wanted_catalog(
        client,
        store,
        max_pages=200_000,
        page_size=PAGE_SIZE,
        on_progress=progress,
    )
    elapsed = time.perf_counter() - started
    if cancelled or not exhausted:
        raise RuntimeError(f"sync did not finish: exhausted={exhausted} cancelled={cancelled} rows={count}")
    return elapsed, count, exhausted


def forget_fingerprints(store: JobStore) -> None:
    with store._lock:
        store._db.execute("UPDATE wanted_rows SET page_fingerprint = NULL WHERE kind = 'album'")
        store._db.commit()


def measure(catalog: Catalog, client, store: JobStore) -> dict:
    catalog.reset_counts()
    elapsed, rows, _exhausted = run_sync(client, store)
    counts = catalog.snapshot()
    return {
        "seconds": round(elapsed, 3),
        "rows": rows,
        "wanted": counts["wanted"],
        "album": counts["album"],
        "artist": counts["artist"],
        "requests": counts["wanted"] + counts["album"] + counts["artist"],
        "peak": counts["peak"],
    }


def print_result(label: str, result: dict) -> None:
    print(
        f"{label}\t{result['seconds']:.3f}s\trequests={result['requests']}"
        f"\twanted={result['wanted']}\talbum={result['album']}\tartist={result['artist']}"
        f"\tpeak={result['peak']}\trows={result['rows']}",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delay", type=float, default=0.03)
    parser.add_argument("--baseline-albums", type=int, default=400)
    parser.add_argument("--sample-albums", type=int, default=4000)
    parser.add_argument("--measure-albums", type=int, default=0, help="Also run the new path at this album count")
    args = parser.parse_args()

    print(f"delay_s={args.delay}", flush=True)
    baseline_catalog = Catalog(args.baseline_albums, args.delay)
    baseline_server = Server(baseline_catalog)
    sample_catalog = Catalog(args.sample_albums, args.delay)
    sample_server = Server(sample_catalog)
    try:
        with tempfile.TemporaryDirectory(prefix="lidarr-net-") as tmp:
            base_store = JobStore(Path(tmp) / "baseline.sqlite3")
            try:
                baseline = AlbumAtATime(
                    LidarrClient(baseline_server.base_url, "bench", track_concurrency=1)
                )
                initial = measure(baseline_catalog, baseline, base_store)
                print_result(f"baseline_initial albums={args.baseline_albums}", initial)
                forget_fingerprints(base_store)
                forced = measure(baseline_catalog, baseline, base_store)
                print_result(f"baseline_resync_every_album albums={args.baseline_albums}", forced)
                baseline.close()
            finally:
                base_store.close()

            sample_store = JobStore(Path(tmp) / "sample.sqlite3")
            try:
                client = LidarrClient(sample_server.base_url, "bench", track_concurrency=6)
                initial = measure(sample_catalog, client, sample_store)
                print_result(f"new_initial albums={args.sample_albums}", initial)
                quiet = measure(sample_catalog, client, sample_store)
                print_result(f"new_resync_unchanged albums={args.sample_albums}", quiet)
                sample_catalog.changed = {1, 5}
                changed = measure(sample_catalog, client, sample_store)
                print_result(f"new_resync_two_albums albums={args.sample_albums}", changed)
                sample_catalog.changed = set()
                client.close()
            finally:
                sample_store.close()

        if args.measure_albums:
            catalog = Catalog(args.measure_albums, args.delay)
            server = Server(catalog)
            try:
                with tempfile.TemporaryDirectory(prefix="lidarr-net-full-") as tmp:
                    store = JobStore(Path(tmp) / "full.sqlite3")
                    try:
                        client = LidarrClient(server.base_url, "bench", track_concurrency=6)
                        initial = measure(catalog, client, store)
                        print_result(f"new_initial albums={args.measure_albums}", initial)
                        quiet = measure(catalog, client, store)
                        print_result(f"new_resync_unchanged albums={args.measure_albums}", quiet)
                        catalog.changed = {1, 5}
                        changed = measure(catalog, client, store)
                        print_result(f"new_resync_two_albums albums={args.measure_albums}", changed)
                        client.close()
                    finally:
                        store.close()
            finally:
                server.close()
    finally:
        sample_server.close()
        baseline_server.close()


if __name__ == "__main__":
    main()
