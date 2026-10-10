from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field

log = logging.getLogger("lidarr_slsk.wanted")

# "A1", "B12", "1a", "10". Lidarr sends trackNumber as text because of vinyl.
_POSITION = re.compile(r"^([A-Za-z]*)(\d*)(.*)$")

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


def optional_id(value) -> int | None:
    """Lidarr ids are integers. Anything else, including a track number like A1, is unusable."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text or not text.isdigit():
        return None
    return int(text)


def count_value(value) -> int:
    """A statistic that is missing or not a number counts as zero."""
    parsed = optional_id(value)
    if parsed is None:
        return 0
    return parsed


def position_sort_key(value, *, empty_last: bool = True) -> tuple:
    """Sort a Lidarr track or disc number without requiring it to be an int.

    ``2`` comes before ``10``. ``A1`` comes before ``A2`` and ``B12``. ``1``
    comes before ``1a``. An empty track number sorts last.
    """
    text = "" if value is None else str(value).strip()
    if not text:
        return (1, "", 0, "") if empty_last else (0, "", 0, "")
    side, digits, rest = _POSITION.match(text).groups()
    number = int(digits) if digits else 10**9
    if not side and not digits:
        return (0, rest.casefold(), number, "")
    return (0, side.casefold(), number, rest.casefold())


def track_number_label(track: dict) -> str:
    """Keep the Lidarr track number as text, including vinyl numbers like A1."""
    number = track.get("trackNumber")
    text = "" if number is None else str(number).strip()
    medium = track.get("mediumNumber")
    medium_text = "" if medium is None else str(medium).strip()
    show_medium = medium_text not in {"", "0", "1"}
    if show_medium and text:
        return f"Disc {medium_text} · Track {text}"
    if text:
        return f"Track {text}"
    if show_medium:
        return f"Disc {medium_text}"
    return "Missing song"


def is_enabled(key: str, choices: dict[str, bool]) -> bool:
    """Missing keys stay enabled so a worker with no saved choices searches everything."""
    if key not in choices:
        return True
    return bool(choices[key])


def _stats(album: dict) -> tuple[int, int]:
    stats = album.get("statistics") or {}
    if not isinstance(stats, dict):
        return 0, 0
    files = count_value(stats.get("trackFileCount"))
    total = count_value(stats.get("totalTrackCount") or stats.get("trackCount"))
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
            album_id = optional_id(album.get("id"))
            title = album.get("title") or "Unknown Album"
            if album_id is None:
                log.warning("Skipping album %s — %s: no usable id", artist, title)
                continue
            try:
                items.append(
                    WantedItem(
                        kind="album",
                        key=album_key(album_id),
                        artist=artist,
                        album=title,
                        album_id=album_id,
                        detail=album_detail(album),
                        payload=album,
                    )
                )
            except Exception:
                log.exception("Skipping album %s — %s", artist, title)
        for hole in sorted(artist_songs, key=_song_sort_key):
            try:
                item = _song_item(artist, hole)
            except Exception:
                log.exception("Skipping a missing song for %s", artist)
                continue
            if item is None:
                continue
            items.append(item)
    return items


def _song_sort_key(row: dict) -> tuple:
    try:
        track = row.get("track") or {}
        if not isinstance(track, dict):
            track = {}
        return (
            str(row.get("album_title") or "").casefold(),
            position_sort_key(track.get("mediumNumber"), empty_last=False),
            position_sort_key(track.get("trackNumber")),
            str(track.get("title") or "").casefold(),
        )
    except Exception:
        return ("", position_sort_key(None, empty_last=False), position_sort_key(None), "")


def _song_item(artist: str, hole: dict) -> WantedItem | None:
    track = hole.get("track") or {}
    album = hole.get("album") or {}
    if not isinstance(track, dict):
        log.warning("Skipping a missing song for %s that is not a track", artist)
        return None
    track_id = optional_id(track.get("id"))
    title = track.get("title") or "Unknown Track"
    if track_id is None:
        log.warning("Skipping %s — %s: no usable track id", artist, track.get("trackNumber") or title)
        return None
    album_id = optional_id(album.get("id")) if isinstance(album, dict) else None
    return WantedItem(
        kind="song",
        key=song_key(track_id),
        artist=artist,
        album=hole.get("album_title") or (album.get("title") if isinstance(album, dict) else "") or "",
        song=title,
        album_id=album_id,
        track_id=track_id,
        detail=track_number_label(track),
        payload=album or None,
        track=track,
    )


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
    selected = []
    for album in albums:
        album_id = optional_id(album.get("id") if isinstance(album, dict) else None)
        if album_id is None:
            continue
        if is_enabled(album_key(album_id), choices):
            selected.append(album)
    return selected


def holes_for_search(holes: list[dict], choices: dict[str, bool]) -> list[dict]:
    selected = []
    for hole in holes:
        if not isinstance(hole, dict):
            continue
        track = hole.get("track") or {}
        track_id = optional_id(track.get("id") if isinstance(track, dict) else None)
        if track_id is None:
            continue
        if is_enabled(song_key(track_id), choices):
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
        album_id = optional_id(album.get("id"))
        if album_id is None:
            log.warning("Skipping album %s: no usable id", album.get("title") or "?")
            continue
        try:
            tracks = client.tracks_for_album(album_id) or []
        except Exception:
            log.exception("Could not read tracks for %s", album.get("title") or album_id)
            tracks = []
        artist = artist_name(album)
        title = album.get("title") or "Unknown Album"
        if not isinstance(tracks, list):
            log.warning("Lidarr returned no track list for %s — %s", artist, title)
            continue
        for track in tracks:
            try:
                if not isinstance(track, dict):
                    log.warning("Skipping a track on %s — %s that is not an object", artist, title)
                    continue
                if not track.get("monitored", True) or track.get("hasFile"):
                    continue
                if optional_id(track.get("id")) is None:
                    log.warning(
                        "Skipping %s on %s — %s: no usable track id",
                        track.get("trackNumber") or track.get("title") or "?",
                        artist,
                        title,
                    )
                    continue
                holes.append(
                    {
                        "track": track,
                        "album": album,
                        "artist_name": artist,
                        "album_title": title,
                    }
                )
            except Exception:
                log.exception("Skipping a track on %s — %s", artist, title)
    return build_catalog(albums, holes), bool(exhausted)
