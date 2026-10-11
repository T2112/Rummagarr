from __future__ import annotations

import hashlib
import json
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
    _grouped: dict[str, list[WantedItem]] = field(default_factory=dict)
    _albums: list[dict] = field(default_factory=list)
    _albums_by_artist: dict[str, list[dict]] = field(default_factory=dict)
    _holes_by_artist: dict[str, list[dict]] = field(default_factory=dict)
    _artist_labels: list[str] = field(default_factory=list)

    def replace(self, items: list[WantedItem]) -> None:
        grouped: dict[str, list[WantedItem]] = {}
        albums: list[dict] = []
        albums_by_artist: dict[str, list[dict]] = {}
        holes_by_artist: dict[str, list[dict]] = {}
        labels: list[str] = []
        seen_albums: set[int] = set()
        for item in items:
            if item.kind == "artist":
                labels.append(item.artist)
                continue
            grouped.setdefault(artist_token(item.artist), []).append(item)
            if item.kind == "album" and item.album_id is not None and item.payload and item.album_id not in seen_albums:
                seen_albums.add(item.album_id)
                albums.append(item.payload)
                albums_by_artist.setdefault(artist_token(item.artist), []).append(item.payload)
            elif item.kind == "song" and item.track and item.payload and item.payload.get("id") is not None:
                holes_by_artist.setdefault(artist_token(item.artist), []).append(
                    {
                        "track": item.track,
                        "album": item.payload,
                        "artist_name": item.artist,
                        "album_title": item.album,
                    }
                )
        with self._lock:
            self._items = list(items)
            self._grouped = grouped
            self._albums = albums
            self._albums_by_artist = albums_by_artist
            self._holes_by_artist = holes_by_artist
            self._artist_labels = labels
            self.synced = True

    def items(self) -> list[WantedItem]:
        with self._lock:
            return list(self._items)

    def artist_labels(self) -> list[str]:
        with self._lock:
            return list(self._artist_labels)

    def has_enabled_work(self, artist: str, choices: dict[str, bool]) -> bool:
        with self._lock:
            children = self._grouped.get(artist_token(artist), [])
        if not children:
            return True
        return any(is_enabled(row.key, choices) for row in children)

    def work_for_artist(self, artist: str) -> tuple[list[dict], list[dict]]:
        token = artist_token(artist)
        with self._lock:
            return list(self._albums_by_artist.get(token, ())), list(self._holes_by_artist.get(token, ()))

    def album_payloads(self) -> list[dict] | None:
        with self._lock:
            if not self.synced:
                return None
            return list(self._albums)


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


def artist_token(name: str) -> str:
    return (name or "").casefold().strip()


def children_by_artist(items: list[WantedItem]) -> dict[str, list[WantedItem]]:
    """Albums and songs keyed by artist. Built once so checks are not O(n) per row."""
    grouped: dict[str, list[WantedItem]] = {}
    for row in items:
        if row.kind == "artist":
            continue
        grouped.setdefault(artist_token(row.artist), []).append(row)
    return grouped


def item_checked(
    item: WantedItem,
    items: list[WantedItem],
    choices: dict[str, bool],
    grouped: dict[str, list[WantedItem]] | None = None,
) -> bool:
    if item.kind != "artist":
        return is_enabled(item.key, choices)
    if grouped is None:
        grouped = children_by_artist(items)
    children = grouped.get(artist_token(item.artist), [])
    if not children:
        return is_enabled(item.key, choices)
    return all(is_enabled(row.key, choices) for row in children)


def choice_updates(
    item: WantedItem,
    enabled: bool,
    items: list[WantedItem],
    grouped: dict[str, list[WantedItem]] | None = None,
) -> dict[str, bool]:
    if item.kind != "artist":
        return {item.key: enabled}
    if grouped is None:
        grouped = children_by_artist(items)
    updates = {item.key: enabled}
    for row in grouped.get(artist_token(item.artist), []):
        updates[row.key] = enabled
    return updates


def select_all_updates(visible: list[WantedItem], items: list[WantedItem], enabled: bool) -> dict[str, bool]:
    grouped = children_by_artist(items)
    updates: dict[str, bool] = {}
    for item in visible:
        updates.update(choice_updates(item, enabled, items, grouped))
    return updates


def select_all_state(
    visible: list[WantedItem],
    items: list[WantedItem],
    choices: dict[str, bool],
    grouped: dict[str, list[WantedItem]] | None = None,
) -> str:
    if not visible:
        return "empty"
    if grouped is None:
        grouped = children_by_artist(items)
    flags = [item_checked(item, items, choices, grouped) for item in visible]
    if all(flags):
        return "all"
    if not any(flags):
        return "none"
    return "some"


_SORT_FIELDS = {
    "kind": lambda item: item.kind,
    "artist": lambda item: item.artist.casefold(),
    "album": lambda item: item.album.casefold(),
    "song": lambda item: item.song.casefold(),
    "detail": lambda item: item.detail.casefold(),
}


def sort_items(items: list[WantedItem], column: str, reverse: bool = False) -> list[WantedItem]:
    key = _SORT_FIELDS.get(column)
    if key is None:
        return list(items)
    return sorted(items, key=key, reverse=reverse)


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


def catalog_from_lidarr(
    client,
    *,
    max_pages: int = 100,
    page_size: int = 250,
    on_progress=None,
    should_cancel=None,
) -> tuple[list[WantedItem], bool]:
    """Read Lidarr wanted/missing and the missing songs on those albums.

    ``on_progress(done, total)`` is called as songs are read. ``total`` is 0
    while album pages are still loading. ``should_cancel`` stops the read
    without returning a partial catalog.
    """
    last_report = -10_000

    def report(done: int, total: int, *, force: bool = False) -> None:
        nonlocal last_report
        if on_progress is None:
            return
        if not force and done - last_report < 500:
            return
        last_report = done
        on_progress(done, total)

    def page_loaded(count: int) -> None:
        report(count, 0, force=True)

    albums, _next_page, exhausted = client.wanted_missing(
        start_page=1,
        max_pages=max(1, int(max_pages)),
        page_size=page_size,
        on_page=page_loaded,
        should_cancel=should_cancel,
    )
    if should_cancel is not None and should_cancel():
        return [], False
    expected = 0
    for album in albums:
        if not isinstance(album, dict):
            continue
        files, total = _stats(album)
        expected += max(total - files, 0)
    if expected <= 0:
        expected = max(len(albums), 1)
    holes: list[dict] = []
    seen = 0
    for album in albums:
        if should_cancel is not None and should_cancel():
            return [], False
        if not isinstance(album, dict):
            log.warning("Skipping an album that is not an object")
            continue
        album_id = optional_id(album.get("id"))
        if album_id is None:
            log.warning("Skipping album %s: no usable id", album.get("title") or "?")
            continue
        try:
            tracks = client.tracks_for_album(album_id) or []
        except Exception:
            log.exception("Could not read tracks for %s", album.get("title") or album_id)
            tracks = []
        found = missing_holes(album, tracks)
        holes.extend(found)
        seen += len(found)
        report(seen, expected)
    report(max(seen, expected), max(seen, expected), force=True)
    if should_cancel is not None and should_cancel():
        return [], False
    return build_catalog(albums, holes), bool(exhausted)


def missing_holes(album: dict, tracks) -> list[dict]:
    """Monitored tracks that still have no file. Bad rows are skipped."""
    holes: list[dict] = []
    if not isinstance(album, dict):
        return holes
    artist = artist_name(album)
    title = album.get("title") or "Unknown Album"
    if not isinstance(tracks, list):
        log.warning("Lidarr returned no track list for %s — %s", artist, title)
        return holes
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
    return holes


def _dump(value: dict | None) -> str | None:
    if not value:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def slim_album(album: dict) -> dict:
    artist = album.get("artist") if isinstance(album.get("artist"), dict) else {}
    slim_artist = {
        "id": artist.get("id") or album.get("artistId"),
        "artistName": artist_name(album),
    }
    aliases = artist.get("aliases")
    if aliases:
        slim_artist["aliases"] = aliases
    return {
        "id": album.get("id"),
        "title": album.get("title"),
        "artistId": album.get("artistId") or slim_artist.get("id"),
        "artist": slim_artist,
        "releaseDate": album.get("releaseDate"),
        "statistics": album.get("statistics") if isinstance(album.get("statistics"), dict) else {},
        "monitored": album.get("monitored", True),
    }


def slim_track(track: dict) -> dict:
    return {
        "id": track.get("id"),
        "title": track.get("title"),
        "trackNumber": track.get("trackNumber"),
        "mediumNumber": track.get("mediumNumber"),
        "monitored": track.get("monitored", True),
        "hasFile": bool(track.get("hasFile")),
    }


def _sort_position(value, *, empty_last: bool = True) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        return "1|" if empty_last else "0|"
    side, digits, rest = _POSITION.match(text).groups()
    number = int(digits) if digits else 10**9
    if not side and not digits:
        return f"0|{rest.casefold()}|{number:010d}|"
    return f"0|{side.casefold()}|{number:010d}|{rest.casefold()}"


_KIND_RANK = {"artist": 0, "album": 1, "song": 2}


def items_to_rows(items: list[WantedItem]) -> list[dict]:
    """Rows ready for the SQLite catalog. Artist details are filled by the store."""
    from match import normalize

    rows = []
    folds: dict[str, str] = {}
    for item in items:
        token = artist_token(item.artist)
        rank = _KIND_RANK.get(item.kind, 9)
        payload = None
        track_json = None
        detail = "" if item.kind == "artist" else (item.detail or "")
        if item.kind == "album":
            payload = _dump(slim_album(item.payload or {}))
        elif item.kind == "song" and item.track:
            track_json = _dump(slim_track(item.track))
        sort_artist = token
        sort_album = (item.album or "").casefold()
        if item.kind == "song":
            track = item.track or {}
            sort_song = "|".join(
                (
                    _sort_position(track.get("mediumNumber"), empty_last=False),
                    _sort_position(track.get("trackNumber")),
                    (item.song or "").casefold(),
                )
            )
        else:
            sort_song = (item.song or "").casefold()
        fold = folds.get(item.artist)
        if fold is None:
            fold = normalize(item.artist)
            folds[item.artist] = fold
        digest = hashlib.sha1(
            "\n".join(
                [
                    item.kind,
                    item.artist or "",
                    item.album or "",
                    item.song or "",
                    "" if item.album_id is None else str(item.album_id),
                    "" if item.track_id is None else str(item.track_id),
                    detail,
                    payload or "",
                    track_json or "",
                    sort_artist,
                    sort_album,
                    sort_song,
                    fold,
                ]
            ).encode()
        ).hexdigest()
        rows.append(
            {
                "key": item.key,
                "kind": item.kind,
                "artist": item.artist or "",
                "artist_key": token,
                "album": item.album or "",
                "song": item.song or "",
                "album_id": item.album_id,
                "track_id": item.track_id,
                "detail": detail,
                "enabled": 1,
                "sort_artist": sort_artist,
                "sort_album": sort_album,
                "sort_song": sort_song,
                "kind_rank": rank,
                "content_hash": digest,
                "payload": payload,
                "track_json": track_json,
                "fold": fold,
            }
        )
    return rows


def album_sync_digest(album: dict, holes: list[dict]) -> str:
    """Cheap fingerprint of one album and its missing tracks.

    A resync compares this to the stored value and skips rebuilding rows when
    nothing that would land in the catalog has changed.
    """
    bits = []
    for hole in holes:
        track = hole.get("track") or {}
        bits.append(
            (
                optional_id(track.get("id")),
                track.get("title") or "",
                "" if track.get("trackNumber") is None else str(track.get("trackNumber")),
                "" if track.get("mediumNumber") is None else str(track.get("mediumNumber")),
                1 if track.get("monitored", True) else 0,
                1 if track.get("hasFile") else 0,
                hole.get("album_title") or "",
            )
        )
    bits.sort()
    raw = "\n".join(
        [
            _dump(slim_album(album)) or "",
            artist_name(album),
            json.dumps(bits, separators=(",", ":"), ensure_ascii=False),
        ]
    )
    return hashlib.sha1(raw.encode()).hexdigest()


def _load_page(client, batch, should_cancel):
    cancelled = False
    albums: list[dict] = []
    holes: list[dict] = []
    for album in batch:
        if should_cancel is not None and should_cancel():
            cancelled = True
            break
        if not isinstance(album, dict):
            log.warning("Skipping an album that is not an object")
            continue
        album_id = optional_id(album.get("id"))
        if album_id is None:
            log.warning("Skipping album %s: no usable id", album.get("title") or "?")
            continue
        albums.append(album)
        try:
            tracks = client.tracks_for_album(album_id) or []
        except Exception:
            log.exception("Could not read tracks for %s", album.get("title") or album_id)
            tracks = []
        holes.extend(missing_holes(album, tracks))
    return albums, holes, cancelled


def _page_plan(albums: list[dict], holes: list[dict]):
    by_album: dict[int, list[dict]] = {}
    for hole in holes:
        album_id = optional_id((hole.get("album") or {}).get("id"))
        if album_id is not None:
            by_album.setdefault(album_id, []).append(hole)
    digests: dict[str, str] = {}
    song_counts: dict[str, int] = {}
    artists: dict[str, str] = {}
    for album in albums:
        album_id = optional_id(album.get("id"))
        if album_id is None:
            continue
        key = album_key(album_id)
        own = by_album.get(album_id, [])
        digests[key] = album_sync_digest(album, own)
        song_counts[key] = len(own)
        artists[key] = artist_key(artist_name(album))
    return digests, song_counts, artists


def _rows_for_changed(
    albums: list[dict],
    holes: list[dict],
    unchanged: set[str],
    digests: dict[str, str],
) -> list[dict]:
    changed_albums = []
    changed_ids: set[int] = set()
    for album in albums:
        album_id = optional_id(album.get("id"))
        if album_id is None or album_key(album_id) in unchanged:
            continue
        changed_albums.append(album)
        changed_ids.add(album_id)
    if not changed_albums:
        return []
    changed_holes = [
        hole for hole in holes if optional_id((hole.get("album") or {}).get("id")) in changed_ids
    ]
    rows = items_to_rows(build_catalog(changed_albums, changed_holes))
    for row in rows:
        if row["kind"] == "album":
            row["sync_digest"] = digests.get(row["key"])
    return rows


def _write_loaded_page(store, albums: list[dict], holes: list[dict]) -> int:
    digests, song_counts, artists = _page_plan(albums, holes)
    unchanged = store.unchanged_album_keys(digests) if digests else set()
    if unchanged:
        store.note_unchanged_keys(
            [artists[key] for key in unchanged],
            len(unchanged),
            sum(song_counts.get(key, 0) for key in unchanged),
        )
    rows = _rows_for_changed(albums, holes, unchanged, digests)
    if rows:
        store.write_wanted_page(rows)
    return len(unchanged) + sum(1 for row in rows if row["kind"] == "album")


def _note_loaded_page(store, albums: list[dict], holes: list[dict]) -> None:
    digests, _song_counts, artists = _page_plan(albums, holes)
    unchanged = store.unchanged_album_keys(digests) if digests else set()
    if unchanged:
        store.note_seen_unchanged(
            [int(key.split(":", 1)[1]) for key in unchanged],
            [artists[key] for key in unchanged],
        )
    rows = _rows_for_changed(albums, holes, unchanged, digests)
    if rows:
        store.note_seen_keys([row["key"] for row in rows])


def _delete_unseen(client, store, *, max_pages: int, page_size: int, should_cancel) -> int | None:
    """Second Lidarr walk, used only when the first walk proved rows were removed.

    Returns the new row count. Returns None when the walk is cancelled or does
    not reach the end, and leaves every saved row in place.
    """
    store.begin_delete_pass()
    try:
        page = 1
        pages = 0
        limit = max(1, int(max_pages or 1))
        exhausted = False
        while pages < limit:
            if should_cancel is not None and should_cancel():
                store.abort_delete_pass()
                return None
            batch, next_page, exhausted = client.wanted_missing(
                start_page=page,
                max_pages=1,
                page_size=page_size,
                should_cancel=should_cancel,
            )
            pages += 1
            if should_cancel is not None and should_cancel():
                store.abort_delete_pass()
                return None
            if not batch:
                exhausted = True
                break
            albums, holes, cancelled = _load_page(client, batch, should_cancel)
            if albums:
                _note_loaded_page(store, albums, holes)
            if cancelled:
                store.abort_delete_pass()
                return None
            if exhausted:
                break
            page = next_page if next_page else page + 1
        if not exhausted:
            store.abort_delete_pass()
            return None
        return store.finish_delete_pass()
    except Exception:
        try:
            store.abort_delete_pass()
        except Exception:
            log.exception("Could not abandon the wanted delete pass")
        raise


def sync_wanted_catalog(
    client,
    store,
    *,
    max_pages: int = 200_000,
    page_size: int = 250,
    on_progress=None,
    should_cancel=None,
) -> tuple[int, bool, bool]:
    """Stream Lidarr wanted/missing into the job store, one page at a time.

    Lidarr's wanted/missing API has no changed-since filter, so a resync still
    walks the list. Unchanged rows are not rewritten. Rows already saved stay
    visible while later pages are read. Cancel keeps those pages and does not
    drop anything that was not seen yet. Rows Lidarr no longer returns are
    removed only after a finished walk proves the key sets differ, which takes
    a second pass. Returns ``(row_count, exhausted, cancelled)``.
    """
    store.begin_wanted_sync()
    cancelled = False
    exhausted = False
    albums_seen = 0
    pages = 0
    page = 1
    limit = max(1, int(max_pages or 1))
    try:
        while pages < limit:
            if should_cancel is not None and should_cancel():
                cancelled = True
                break
            batch, next_page, exhausted = client.wanted_missing(
                start_page=page,
                max_pages=1,
                page_size=page_size,
                should_cancel=should_cancel,
            )
            pages += 1
            if should_cancel is not None and should_cancel():
                cancelled = True
                break
            if not batch:
                if not exhausted and should_cancel is not None and should_cancel():
                    cancelled = True
                else:
                    exhausted = True
                break
            albums, holes, batch_cancelled = _load_page(client, batch, should_cancel)
            if batch_cancelled:
                cancelled = True
            if albums:
                albums_seen += _write_loaded_page(store, albums, holes)
                if on_progress is not None:
                    on_progress(store.wanted_row_count(), albums_seen)
            if cancelled or exhausted:
                break
            page = next_page if next_page else page + 1
        count = store.finish_wanted_sync(complete=bool(exhausted) and not cancelled)
        if getattr(store, "_needs_delete", False) and exhausted and not cancelled:
            replaced = _delete_unseen(
                client,
                store,
                max_pages=limit,
                page_size=page_size,
                should_cancel=should_cancel,
            )
            if replaced is None:
                cancelled_now = should_cancel is not None and should_cancel()
                return count, not cancelled_now, cancelled_now
            count = replaced
        return count, bool(exhausted) and not cancelled, cancelled
    except Exception:
        try:
            store.finish_wanted_sync(complete=False)
        except Exception:
            log.exception("Could not close the wanted sync")
        raise
