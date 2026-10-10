from __future__ import annotations

import threading
from dataclasses import dataclass, field

VIEW_EVERYTHING = "Everything"
VIEW_ARTISTS = "Incomplete artists"
VIEW_ALBUMS = "Incomplete albums"
VIEW_SONGS = "Incomplete songs"
VIEWS = (VIEW_EVERYTHING, VIEW_ARTISTS, VIEW_ALBUMS, VIEW_SONGS)


def album_key(album_id: int) -> str:
    return f"album:{int(album_id)}"


def song_key(track_id: int) -> str:
    return f"song:{int(track_id)}"


def artist_key(name: str) -> str:
    return "artist:" + (name or "").casefold().strip()


def same_artist(left: str, right: str) -> bool:
    return (left or "").casefold().strip() == (right or "").casefold().strip()


def artist_name(album: dict) -> str:
    artist = album.get("artist") or {}
    return artist.get("artistName") or album.get("artistName") or "Unknown Artist"


def is_enabled(key: str, choices: dict[str, bool]) -> bool:
    """Missing keys stay enabled so a worker with no saved choices searches everything."""
    if key not in choices:
        return True
    return bool(choices[key])


def _stats(album: dict) -> tuple[int, int]:
    stats = album.get("statistics") or {}
    files = int(stats.get("trackFileCount") or 0)
    total = int(stats.get("totalTrackCount") or stats.get("trackCount") or 0)
    return files, total


def album_detail(album: dict) -> str:
    files, total = _stats(album)
    if files <= 0 and total > 0:
        return f"Missing album · {total} songs"
    if files <= 0:
        return "Missing album"
    if total > files:
        return f"Incomplete · {files} of {total} songs"
    return "Incomplete"


@dataclass
class WantedItem:
    kind: str
    key: str
    artist: str
    album: str = ""
    song: str = ""
    album_id: int | None = None
    track_id: int | None = None
    detail: str = ""
    payload: dict | None = None
    track: dict | None = None


@dataclass
class WantedCatalog:
    """In-memory copy of the last Lidarr sync. Selections live in the job store."""

    _items: list[WantedItem] = field(default_factory=list)
    synced: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def replace(self, items: list[WantedItem]) -> None:
        with self._lock:
            self._items = list(items)
            self.synced = True

    def items(self) -> list[WantedItem]:
        with self._lock:
            return list(self._items)

    def album_payloads(self) -> list[dict] | None:
        with self._lock:
            if not self.synced:
                return None
            albums: list[dict] = []
            seen: set[int] = set()
            for item in self._items:
                if item.kind != "album" or item.album_id is None or not item.payload:
                    continue
                if item.album_id in seen:
                    continue
                seen.add(item.album_id)
                albums.append(item.payload)
            return albums


def build_catalog(albums: list[dict], holes: list[dict] | None = None) -> list[WantedItem]:
    """Group wanted albums and missing songs under their artists, A–Z."""
    holes = holes or []
    albums_by_artist: dict[str, list[dict]] = {}
    songs_by_artist: dict[str, list[dict]] = {}
    labels: dict[str, str] = {}
    order: list[str] = []

    def bucket(name: str) -> str:
        label = name or "Unknown Artist"
        key = label.casefold().strip()
        if key not in labels:
            labels[key] = label
            order.append(key)
        return key

    for album in albums:
        key = bucket(artist_name(album))
        albums_by_artist.setdefault(key, []).append(album)
    for hole in holes:
        name = hole.get("artist_name") or artist_name(hole.get("album") or {})
        key = bucket(name)
        songs_by_artist.setdefault(key, []).append(hole)

    order.sort()
    items: list[WantedItem] = []
    for key in order:
        artist = labels[key]
        artist_albums = albums_by_artist.get(key) or []
        artist_songs = songs_by_artist.get(key) or []
        bits: list[str] = []
        if artist_albums:
            bits.append(f"{len(artist_albums)} album" + ("s" if len(artist_albums) != 1 else ""))
        if artist_songs:
            bits.append(f"{len(artist_songs)} song" + ("s" if len(artist_songs) != 1 else ""))
        items.append(
            WantedItem(
                kind="artist",
                key=artist_key(artist),
                artist=artist,
                detail=", ".join(bits) or "Incomplete",
            )
        )
        for album in sorted(artist_albums, key=lambda row: (row.get("title") or "").casefold()):
            title = album.get("title") or "Unknown Album"
            items.append(
                WantedItem(
                    kind="album",
                    key=album_key(int(album["id"])),
                    artist=artist,
                    album=title,
                    album_id=int(album["id"]),
                    detail=album_detail(album),
                    payload=album,
                )
            )
        for hole in sorted(
            artist_songs,
            key=lambda row: (
                (row.get("album_title") or "").casefold(),
                int((row.get("track") or {}).get("trackNumber") or 0),
                ((row.get("track") or {}).get("title") or "").casefold(),
            ),
        ):
            track = hole.get("track") or {}
            album = hole.get("album") or {}
            if track.get("id") is None:
                continue
            number = track.get("trackNumber")
            album_id = album.get("id")
            items.append(
                WantedItem(
                    kind="song",
                    key=song_key(int(track["id"])),
                    artist=artist,
                    album=hole.get("album_title") or album.get("title") or "",
                    song=track.get("title") or "Unknown Track",
                    album_id=int(album_id) if album_id is not None else None,
                    track_id=int(track["id"]),
                    detail=f"Track {number}" if number else "Missing song",
                    payload=album or None,
                    track=track,
                )
            )
    return items


def filter_view(items: list[WantedItem], view: str) -> list[WantedItem]:
    if view == VIEW_ARTISTS:
        return [item for item in items if item.kind == "artist"]
    if view == VIEW_ALBUMS:
        return [item for item in items if item.kind == "album"]
    if view == VIEW_SONGS:
        return [item for item in items if item.kind == "song"]
    if view != VIEW_EVERYTHING:
        raise ValueError(f"Unknown wanted view: {view}")
    return list(items)


def item_checked(item: WantedItem, items: list[WantedItem], choices: dict[str, bool]) -> bool:
    if item.kind != "artist":
        return is_enabled(item.key, choices)
    children = [row for row in items if row.kind != "artist" and same_artist(row.artist, item.artist)]
    if not children:
        return is_enabled(item.key, choices)
    return all(is_enabled(row.key, choices) for row in children)


def choice_updates(item: WantedItem, enabled: bool, items: list[WantedItem]) -> dict[str, bool]:
    if item.kind != "artist":
        return {item.key: enabled}
    keys = [item.key]
    keys.extend(row.key for row in items if row.kind != "artist" and same_artist(row.artist, item.artist))
    return {key: enabled for key in keys}


def select_all_updates(visible: list[WantedItem], items: list[WantedItem], enabled: bool) -> dict[str, bool]:
    updates: dict[str, bool] = {}
    for item in visible:
        updates.update(choice_updates(item, enabled, items))
    return updates


def select_all_state(visible: list[WantedItem], items: list[WantedItem], choices: dict[str, bool]) -> str:
    if not visible:
        return "empty"
    flags = [item_checked(item, items, choices) for item in visible]
    if all(flags):
        return "all"
    if not any(flags):
        return "none"
    return "some"


def albums_for_search(albums: list[dict], choices: dict[str, bool]) -> list[dict]:
    return [album for album in albums if album.get("id") is not None and is_enabled(album_key(int(album["id"])), choices)]


def holes_for_search(holes: list[dict], choices: dict[str, bool]) -> list[dict]:
    selected = []
    for hole in holes:
        track = hole.get("track") or {}
        if track.get("id") is None:
            continue
        if is_enabled(song_key(int(track["id"])), choices):
            selected.append(hole)
    return selected


def holes_from_catalog(items: list[WantedItem]) -> list[dict]:
    holes = []
    for item in items:
        if item.kind != "song" or not item.track or not item.payload or item.payload.get("id") is None:
            continue
        holes.append(
            {
                "track": item.track,
                "album": item.payload,
                "artist_name": item.artist,
                "album_title": item.album,
            }
        )
    return holes


def catalog_from_lidarr(client, *, max_pages: int = 100, page_size: int = 250, on_progress=None) -> tuple[list[WantedItem], bool]:
    """Read Lidarr wanted/missing and the missing songs on those albums."""
    albums, _next_page, exhausted = client.wanted_missing(
        start_page=1,
        max_pages=max(1, int(max_pages)),
        page_size=page_size,
    )
    holes: list[dict] = []
    total = len(albums)
    for index, album in enumerate(albums):
        if on_progress is not None:
            on_progress(index, total)
        if album.get("id") is None:
            continue
        try:
            tracks = client.tracks_for_album(int(album["id"])) or []
        except Exception:
            tracks = []
        artist = artist_name(album)
        title = album.get("title") or "Unknown Album"
        for track in tracks:
            if not track.get("monitored", True) or track.get("hasFile") or track.get("id") is None:
                continue
            holes.append(
                {
                    "track": track,
                    "album": album,
                    "artist_name": artist,
                    "album_title": title,
                }
            )
    return build_catalog(albums, holes), bool(exhausted)
