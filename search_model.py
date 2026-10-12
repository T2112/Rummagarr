"""Manual Soulseek searches: queries, grouped results, and what to queue.

The desktop draws these. Nothing here talks to the network, so tests can
build a result list without a Soulseek login.
"""

from __future__ import annotations

from dataclasses import dataclass

from match import basename, extension, normalize, parent_dir

try:
    from aioslsk.protocol.primitives import AttributeKey
except Exception:  # pragma: no cover - tests still pass attributes by name
    AttributeKey = None  # type: ignore


@dataclass
class SearchLink:
    """The Lidarr row a magnifier search belongs to, when there is one."""

    kind: str
    key: str
    artist: str
    album: str = ""
    title: str = ""
    album_id: int | None = None
    track_id: int | None = None


@dataclass
class SearchFile:
    username: str
    filename: str
    folder: str = ""
    size: int = 0
    bitrate: int = 0
    duration: int = 0
    extension: str = ""
    free_slot: bool = False
    speed: int = 0
    queue: int = 0
    state: str = ""

    @property
    def id(self) -> str:
        return f"{self.username}\n{self.filename}"


@dataclass
class DisplayRow:
    kind: str
    file: SearchFile | None
    username: str
    speed: int = 0
    queue: int = 0
    free_slot: bool = False
    file_count: int = 0


def manual_query(kind: str, artist: str, album: str = "", song: str = "") -> str:
    """Accent-folded Soulseek query for a Wanted row.

    An artist row searches the artist. An album row searches artist and album.
    A song row searches artist and title, which is what you would type by hand.
    """
    kind_name = (kind or "").casefold()
    if kind_name == "artist":
        return normalize(artist)
    if kind_name == "album":
        return normalize(f"{artist} {album}")
    return normalize(f"{artist} {song or album}")


def link_from_wanted(item) -> SearchLink:
    key = getattr(item, "key", "") or ""
    album_id = getattr(item, "album_id", None)
    track_id = getattr(item, "track_id", None)
    if album_id is None and key.startswith("album:"):
        album_id = _suffix_id(key)
    if track_id is None and key.startswith("song:"):
        track_id = _suffix_id(key)
    album = getattr(item, "album", "") or ""
    song = getattr(item, "song", "") or ""
    return SearchLink(
        kind=getattr(item, "kind", "") or "",
        key=key,
        artist=getattr(item, "artist", "") or "",
        album=album,
        title=song or album,
        album_id=album_id,
        track_id=track_id,
    )


def _suffix_id(key: str) -> int | None:
    text = key.split(":", 1)[-1]
    if text.isdigit():
        return int(text)
    return None


def _attr_value(attrs: dict, *names: object) -> int:
    for name in names:
        if name in attrs and attrs[name]:
            try:
                return int(attrs[name])
            except (TypeError, ValueError):
                continue
    return 0


def _attribute_map(item) -> dict:
    getter = getattr(item, "get_attribute_map", None)
    mapped: dict = {}
    if callable(getter):
        try:
            mapped = dict(getter() or {})
        except Exception:
            mapped = {}
    if mapped:
        return mapped
    raw = getattr(item, "attributes", None)
    if isinstance(raw, dict):
        return raw
    return mapped


def file_from_share(
    username: str,
    item,
    *,
    free_slot: bool = False,
    speed: int = 0,
    queue: int = 0,
    folder: str = "",
) -> SearchFile:
    filename = str(getattr(item, "filename", "") or "")
    attrs = _attribute_map(item)
    bitrate_keys: list[object] = ["bitrate", 0]
    duration_keys: list[object] = ["duration", 1]
    if AttributeKey is not None:
        bitrate_keys.insert(0, AttributeKey.BITRATE)
        duration_keys.insert(0, AttributeKey.DURATION)
    ext = str(getattr(item, "extension", "") or extension(filename) or "").lstrip(".").lower()
    return SearchFile(
        username=username,
        filename=filename,
        folder=folder or parent_dir(filename),
        size=int(getattr(item, "filesize", 0) or getattr(item, "size", 0) or 0),
        bitrate=_attr_value(attrs, *bitrate_keys),
        duration=_attr_value(attrs, *duration_keys),
        extension=ext,
        free_slot=bool(free_slot),
        speed=int(speed or 0),
        queue=int(queue or 0),
    )


def files_from_results(results) -> list[SearchFile]:
    """Flatten Soulseek search replies in arrival order, one file per path."""
    found: list[SearchFile] = []
    seen: set[str] = set()
    for result in list(results or []):
        username = str(getattr(result, "username", "") or "")
        free_slot = bool(getattr(result, "has_free_slots", False))
        speed = int(getattr(result, "avg_speed", 0) or 0)
        queue = int(getattr(result, "queue_size", 0) or 0)
        items = list(getattr(result, "shared_items", None) or [])
        for item in items:
            record = file_from_share(
                username,
                item,
                free_slot=free_slot,
                speed=speed,
                queue=queue,
            )
            if not record.filename or record.id in seen:
                continue
            seen.add(record.id)
            found.append(record)
    return found


def search_receipt(results) -> tuple[int, int, int]:
    """Peer replies received, files the Search tab can list, and locked files it hides."""
    rows = list(results or [])
    locked = 0
    for result in rows:
        locked += len(list(getattr(result, "locked_results", None) or []))
    return len(rows), len(files_from_results(rows)), locked


def counts_for_server(folders: int, files: int) -> tuple[int, int]:
    """Folder and file totals to send in SharedFoldersFiles.

    A client that reports files but zero folders is treated as misreporting, and
    other clients ignore its searches. Zero files stays zero: inventing a share
    is the same kind of misreport.
    """
    try:
        folder_count = max(0, int(folders))
    except (TypeError, ValueError):
        folder_count = 0
    try:
        file_count = max(0, int(files))
    except (TypeError, ValueError):
        file_count = 0
    if file_count > 0 and folder_count <= 0:
        folder_count = 1
    return folder_count, file_count


def explain_search(
    replies: int,
    shown: int,
    locked: int,
    *,
    logged_in: bool,
    server_connected: bool,
    listen_port: int,
    share_files: int | None,
    share_folders: int,
    parent_name: str,
    scanning: bool,
) -> str:
    """Why a search listed nothing, or how many files it listed.

    ``share_files`` is None when this process cannot see the share totals.
    """
    if shown > 0:
        noun = "file" if shown == 1 else "files"
        return f"Listed {shown} {noun}."
    if not logged_in:
        return "No results (0 peer replies). Soulseek is not logged in, so the search was not sent."
    if not server_connected:
        return "No results (0 peer replies). The server connection is down, so the search was not sent."
    if replies <= 0 and not listen_port:
        return "No results (0 peer replies). The listen port is closed, so peers cannot deliver results."
    if replies <= 0 and share_files is not None and share_files <= 0:
        if scanning:
            return (
                "No results (0 peer replies). The share scan is still running and Soulseek has been told "
                "this client shares 0 files. Other users often ignore that search until the scan reports files."
            )
        return (
            "No results (0 peer replies). This client is sharing 0 files. "
            "Other users often ignore searches from a client that shares nothing."
        )
    if replies <= 0:
        parent = parent_name or "none"
        files = "unknown" if share_files is None else str(share_files)
        port = listen_port or "closed"
        return (
            f"No results (0 peer replies). The search was sent. Listening on port {port}. "
            f"Sharing {files} files in {share_folders} folders. Distributed parent: {parent}."
        )
    reply_word = "reply" if replies == 1 else "replies"
    if locked:
        file_word = "file was" if locked == 1 else "files were"
        return (
            f"No results ({replies} peer {reply_word}, 0 files shown). "
            f"{locked} locked {file_word} hidden."
        )
    return f"No results ({replies} peer {reply_word}, 0 files shown)."


def neutral_search_outcome(replies: int, shown: int, locked: int) -> str:
    """Outcome when the connection state is not available to inspect."""
    if shown > 0:
        noun = "file" if shown == 1 else "files"
        return f"Listed {shown} {noun}."
    if replies <= 0:
        return "No results (0 peer replies). No peer answered."
    return explain_search(
        replies,
        shown,
        locked,
        logged_in=True,
        server_connected=True,
        listen_port=1,
        share_files=None,
        share_folders=0,
        parent_name="",
        scanning=False,
    )


def result_count(request) -> int:
    total = 0
    for result in list(getattr(request, "results", None) or []):
        total += len(list(getattr(result, "shared_items", None) or []))
    return total


def parse_terms(text: str) -> tuple[list[str], list[str]]:
    include: list[str] = []
    exclude: list[str] = []
    for raw in (text or "").split():
        if raw.startswith("-") and len(raw) > 1:
            exclude.append(raw[1:].casefold())
        else:
            include.append(raw.casefold())
    return include, exclude


def file_visible(
    item: SearchFile,
    text: str = "",
    *,
    formats: set[str] | None = None,
    min_bitrate: int = 0,
    free_only: bool = False,
) -> bool:
    include, exclude = parse_terms(text)
    blob = f"{item.filename} {item.folder} {item.username}".casefold()
    if any(term not in blob for term in include):
        return False
    if any(term in blob for term in exclude):
        return False
    if formats and item.extension not in formats:
        return False
    if min_bitrate and int(item.bitrate or 0) < int(min_bitrate):
        return False
    if free_only and not item.free_slot:
        return False
    return True


def filter_files(
    files: list[SearchFile],
    text: str = "",
    *,
    formats: set[str] | None = None,
    min_bitrate: int = 0,
    free_only: bool = False,
) -> list[SearchFile]:
    return [
        item
        for item in files
        if file_visible(item, text, formats=formats, min_bitrate=min_bitrate, free_only=free_only)
    ]


def _file_sort_value(item: SearchFile, column: str):
    if column == "name":
        return display_name(item).casefold()
    if column == "size":
        return item.size
    if column == "bitrate":
        return item.bitrate
    if column == "length":
        return item.duration
    if column == "format":
        return item.extension
    if column == "state":
        return item.state
    if column == "speed":
        return item.speed
    if column == "slot":
        return (1 if item.free_slot else 0, -item.queue)
    return 0


def display_rows(
    files: list[SearchFile],
    *,
    sort_column: str = "",
    reverse: bool = False,
) -> list[DisplayRow]:
    """Group files under their user, the way SoulseekQt shows a search."""
    groups: dict[str, list[SearchFile]] = {}
    order: list[str] = []
    for item in files:
        if item.username not in groups:
            groups[item.username] = []
            order.append(item.username)
        groups[item.username].append(item)

    def user_value(name: str):
        rows = groups[name]
        sample = rows[0]
        if sort_column == "name":
            return name.casefold()
        if sort_column == "speed":
            return sample.speed
        if sort_column == "slot":
            return (1 if sample.free_slot else 0, -sample.queue)
        if sort_column == "size":
            return sum(row.size for row in rows)
        return order.index(name)

    if sort_column in {"name", "speed", "slot", "size"}:
        order.sort(key=user_value, reverse=reverse)
    rows: list[DisplayRow] = []
    file_columns = {"name", "size", "bitrate", "length", "format", "state"}
    for name in order:
        items = groups[name]
        if sort_column in file_columns:
            items = sorted(items, key=lambda item: _file_sort_value(item, sort_column), reverse=reverse)
        sample = items[0]
        rows.append(
            DisplayRow(
                "user",
                None,
                name,
                speed=sample.speed,
                queue=sample.queue,
                free_slot=sample.free_slot,
                file_count=len(items),
            )
        )
        for item in items:
            rows.append(
                DisplayRow(
                    "file",
                    item,
                    name,
                    speed=item.speed,
                    queue=item.queue,
                    free_slot=item.free_slot,
                )
            )
    return rows


def choose_downloads(
    files: list[SearchFile],
    selected_ids: set[str],
    *,
    folder: bool = False,
    usernames: set[str] | None = None,
) -> list[SearchFile]:
    """Files to queue. Folder mode takes every file that shares a selected folder or user."""
    selected = [item for item in files if item.id in selected_ids]
    names = set(usernames or ())
    if folder:
        pairs = {(item.username, item.folder) for item in selected}
        if not pairs and not names:
            return []
        return [
            item
            for item in files
            if item.username in names or (item.username, item.folder) in pairs
        ]
    return selected


def display_name(item: SearchFile) -> str:
    if item.folder and item.folder not in item.filename:
        return item.folder + "\\" + basename(item.filename)
    return item.filename or basename(item.filename)


def format_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    if size:
        return f"{size} B"
    return ""


def format_duration(seconds: int) -> str:
    if not seconds:
        return ""
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes}:{sec:02d}"


def format_speed(speed: int) -> str:
    if speed <= 0:
        return ""
    if speed >= 1024 * 1024:
        return f"{speed / (1024 * 1024):.1f} MB/s"
    if speed >= 1024:
        return f"{speed / 1024:.0f} KB/s"
    return f"{speed} B/s"


def format_slot(free: bool, queue: int) -> str:
    if free:
        return "Free slot"
    if queue:
        return f"Queue {queue}"
    return "Busy"


def state_label(state: str) -> str:
    labels = {
        "queued": "Queued",
        "downloading": "Downloading",
        "downloaded": "Downloaded",
        "failed": "Failed",
    }
    return labels.get(state or "", "")


def file_spec(item: SearchFile) -> dict:
    return {
        "id": item.id,
        "username": item.username,
        "filename": item.filename,
        "folder": item.folder,
    }


# Kept so a board can store filter widgets without the GUI importing dataclasses twice.
@dataclass
class SearchFilters:
    text: str = ""
    flac: bool = False
    mp3: bool = False
    min_bitrate: int = 0
    free_only: bool = False

    def formats(self) -> set[str] | None:
        chosen = set()
        if self.flac:
            chosen.add("flac")
        if self.mp3:
            chosen.add("mp3")
        return chosen or None

    def apply(self, files: list[SearchFile]) -> list[SearchFile]:
        return filter_files(
            files,
            self.text,
            formats=self.formats(),
            min_bitrate=self.min_bitrate,
            free_only=self.free_only,
        )
