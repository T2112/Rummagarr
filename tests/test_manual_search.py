from __future__ import annotations

import asyncio
from pathlib import Path

from match import normalize
from search_model import (
    SearchFile,
    choose_downloads,
    display_rows,
    file_visible,
    files_from_results,
    link_from_wanted,
    manual_query,
)
from tests.conftest import make_worker
from wanted import WantedItem


class Item:
    def __init__(self, filename: str, size: int, bitrate: int, duration: int, extension: str) -> None:
        self.filename = filename
        self.filesize = size
        self.extension = extension
        self.attributes = {"bitrate": bitrate, "duration": duration}

    def get_attribute_map(self):
        return {}


class Result:
    def __init__(self, username: str, items: list[Item], *, free: bool, speed: int, queue: int) -> None:
        self.username = username
        self.shared_items = items
        self.has_free_slots = free
        self.avg_speed = speed
        self.queue_size = queue


def test_manual_query_uses_the_folded_spelling():
    assert manual_query("artist", "Mötley Crüe") == normalize("Mötley Crüe")
    assert manual_query("artist", "Mötley Crüe") == "motley crue"
    assert manual_query("album", "Blue Öyster Cult", "Fire of Unknown Origin") == "blue oyster cult fire of unknown origin"
    assert manual_query("song", "Sigur Rós", "Ágætis Byrjun", "Svefn-g-englar") == "sigur ros svefn g englar"
    item = WantedItem(kind="album", key="album:9", artist="Ella Langley", album="Hungover")
    link = link_from_wanted(item)
    assert link.album_id == 9
    assert link.artist == "Ella Langley"
    assert manual_query(link.kind, link.artist, link.album, link.title) == "ella langley hungover"


def test_results_group_filter_and_queue_a_folder():
    results = [
        Result(
            "vinylqueen",
            [Item(r"music\Ella\01.flac", 30_000_000, 900, 214, "flac"), Item(r"music\Ella\02.flac", 28_000_000, 900, 200, "flac")],
            free=True,
            speed=250_000,
            queue=0,
        ),
        Result(
            "busyuser",
            [Item(r"music\Ella\01.mp3", 8_000_000, 192, 214, "mp3")],
            free=False,
            speed=40_000,
            queue=6,
        ),
    ]
    files = files_from_results(results)
    assert [item.username for item in files] == ["vinylqueen", "vinylqueen", "busyuser"]
    assert files[0].bitrate == 900
    assert files[0].duration == 214
    assert files[0].free_slot is True
    rows = display_rows(files)
    assert [row.kind for row in rows] == ["user", "file", "file", "user", "file"]
    assert rows[0].username == "vinylqueen"
    assert rows[0].file_count == 2
    flac = [item for item in files if file_visible(item, "ella -mp3", formats={"flac"}, min_bitrate=320, free_only=True)]
    assert len(flac) == 2
    selected = {files[0].id}
    queued = choose_downloads(files, selected, folder=True)
    assert len(queued) == 2
    assert {item.filename for item in queued} == {files[0].filename, files[1].filename}
    one = choose_downloads(files, selected, folder=False)
    assert one == [files[0]]


class Transfer:
    def __init__(self, name: str, username: str, path: Path) -> None:
        self.filename = name
        self.username = username
        self.filesize = 1000
        self.bytes_transfered = 1000
        self.state = type("State", (), {"name": "COMPLETE"})()
        self.local_path = str(path)


def test_queueing_a_manual_result_imports_for_the_linked_album(tmp_path):
    worker = make_worker(tmp_path)
    source = tmp_path / "01.mp3"
    source.write_bytes(b"not-audio")
    scanned: list[str] = []
    downloaded: list[tuple[str, str]] = []

    class Searches:
        async def search(self, query: str):
            return type("Request", (), {"query": query, "results": []})()

        def remove_request(self, _request) -> None:
            return None

    class Transfers:
        async def download(self, username: str, filename: str):
            downloaded.append((username, filename))
            return Transfer(filename, username, source)

    class Client:
        session = object()
        searches = Searches()
        transfers = Transfers()

    worker.client = Client()
    worker.control.start()
    worker.lidarr.scan_downloaded = lambda path: scanned.append(path)
    file_id = "vinylqueen\nmusic\\Ella\\01.mp3"
    try:
        dest = asyncio.run(
            worker.download_manual(
                [{"id": file_id, "username": "vinylqueen", "filename": r"music\Ella\01.mp3", "folder": r"music\Ella"}],
                {"kind": "album", "key": "album:1", "artist": "Ella Langley", "album": "Hungover", "title": "Hungover", "album_id": 1},
            )
        )
        assert downloaded == [("vinylqueen", r"music\Ella\01.mp3")]
        assert worker.manual_state(file_id) == "downloaded"
        assert "Ella Langley" in dest and "Hungover" in dest
        assert scanned and "Hungover" in scanned[-1]
        assert (Path(dest) / "01.mp3").is_file()
    finally:
        worker.close()
