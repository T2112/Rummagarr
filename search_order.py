"""Which artist the worker searches next, and how the Wanted list is arranged.

Ascending is A–Z, the smallest gap, the oldest date, or the artist tried least
recently. Descending is the opposite. Lidarr order, descending, is monitored
artists first, then the most recently added.
"""

from __future__ import annotations

SEARCH_ORDERS: tuple[tuple[str, str], ...] = (
    ("lidarr", "Lidarr order"),
    ("name", "Artist name"),
    ("missing_tracks", "Missing tracks"),
    ("missing_albums", "Missing albums"),
    ("added", "Recently added"),
    ("release", "Album release date"),
    ("tried", "Least recently tried"),
)

_ORDER_KEYS = {key for key, _label in SEARCH_ORDERS}
_LABELS = {key: label for key, label in SEARCH_ORDERS}
_BY_LABEL = {label.casefold(): key for key, label in SEARCH_ORDERS}

# Sensible direction when someone picks an order and has not chosen one yet.
_DEFAULT_DIRECTION = {
    "lidarr": "desc",
    "name": "asc",
    "missing_tracks": "asc",
    "missing_albums": "asc",
    "added": "desc",
    "release": "asc",
    "tried": "asc",
}


def normalize_search_order(value) -> str:
    text = str(value or "").strip()
    if text in _ORDER_KEYS:
        return text
    return _BY_LABEL.get(text.casefold(), "lidarr")


def normalize_search_direction(value) -> str:
    text = str(value or "").strip().casefold()
    if text in {"asc", "ascending", "0", "false"}:
        return "asc"
    if text in {"desc", "descending", "1", "true"}:
        return "desc"
    return "desc"


def default_direction(order: str) -> str:
    return _DEFAULT_DIRECTION.get(normalize_search_order(order), "desc")


def order_label(order: str) -> str:
    return _LABELS.get(normalize_search_order(order), "Lidarr order")


def direction_label(direction: str) -> str:
    return "Ascending" if normalize_search_direction(direction) == "asc" else "Descending"


def artist_order_sql(order: str, direction: str) -> str:
    """ORDER BY for ``wanted_artists a`` joined to ``tried.tried``.

    The text is one of a fixed set of clauses. It is not built from the raw setting.
    """
    key = normalize_search_order(order)
    descending = normalize_search_direction(direction) == "desc"
    name = "a.sort_artist ASC"
    if key == "name":
        return "a.sort_artist DESC" if descending else "a.sort_artist ASC"
    if key == "missing_tracks":
        return f"a.missing_tracks {'DESC' if descending else 'ASC'}, {name}"
    if key == "missing_albums":
        return f"a.missing_albums {'DESC' if descending else 'ASC'}, {name}"
    if key == "added":
        blank = "CASE WHEN a.added = '' THEN 1 ELSE 0 END"
        if descending:
            return f"{blank}, a.added DESC, {name}"
        return f"{blank}, a.added ASC, {name}"
    if key == "release":
        column = "a.release_max" if descending else "a.release_min"
        blank = f"CASE WHEN {column} = '' THEN 1 ELSE 0 END"
        return f"{blank}, {column} {'DESC' if descending else 'ASC'}, {name}"
    if key == "tried":
        blank = "CASE WHEN tried.tried IS NULL OR tried.tried = '' THEN 1 ELSE 0 END"
        if descending:
            return f"{blank}, tried.tried DESC, {name}"
        # Never tried, then the oldest attempt.
        return f"CASE WHEN tried.tried IS NULL OR tried.tried = '' THEN 0 ELSE 1 END, tried.tried ASC, {name}"
    if descending:
        return "a.monitored DESC, CASE WHEN a.added = '' THEN 1 ELSE 0 END, a.added DESC, a.sort_artist ASC"
    return "a.monitored ASC, CASE WHEN a.added = '' THEN 1 ELSE 0 END, a.added ASC, a.sort_artist DESC"


def _invert_text(text: str) -> str:
    return "".join(chr(0x10FFFF - ord(char)) for char in text)


def _pad(number: int, *, descending: bool) -> str:
    count = max(0, int(number))
    if descending:
        count = 999_999_999 - min(count, 999_999_999)
    return f"{count:09d}"


def missing_counts(album: dict | None) -> tuple[int, int]:
    """Missing tracks, then 1 when the album itself is still incomplete."""
    if not isinstance(album, dict):
        return 0, 0
    stats = album.get("statistics") if isinstance(album.get("statistics"), dict) else {}
    try:
        files = int(stats.get("trackFileCount") or 0)
    except (TypeError, ValueError):
        files = 0
    raw_total = stats.get("totalTrackCount")
    if raw_total in (None, ""):
        raw_total = stats.get("trackCount")
    try:
        total = int(raw_total or 0)
    except (TypeError, ValueError):
        total = 0
    if total <= 0 and files <= 0:
        return 1, 1
    gap = max(0, total - files)
    return gap, (1 if files < total else 0)


def release_text(album: dict | None) -> str:
    if not isinstance(album, dict):
        return ""
    return str(album.get("releaseDate") or "")[:10]


def artist_display_key(
    *,
    order: str,
    direction: str,
    name: str,
    monitored: bool,
    added: str,
    missing_tracks: int,
    missing_albums: int,
    release_min: str,
    release_max: str,
    last_tried: str,
) -> str:
    """A string that sorts ascending into the requested artist order."""
    key = normalize_search_order(order)
    descending = normalize_search_direction(direction) == "desc"
    label = (name or "").casefold()
    added = added or ""
    release_min = release_min or ""
    release_max = release_max or ""
    last_tried = last_tried or ""
    if key == "name":
        return _invert_text(label) if descending else label
    if key == "missing_tracks":
        return f"{_pad(missing_tracks, descending=descending)}|{label}"
    if key == "missing_albums":
        return f"{_pad(missing_albums, descending=descending)}|{label}"
    if key == "added":
        if descending:
            return f"{0 if added else 1}|{_invert_text(added)}|{label}"
        return f"{0 if added else 1}|{added}|{label}"
    if key == "release":
        stamp = release_max if descending else release_min
        if descending:
            return f"{0 if stamp else 1}|{_invert_text(stamp)}|{label}"
        return f"{0 if stamp else 1}|{stamp}|{label}"
    if key == "tried":
        if descending:
            return f"{0 if last_tried else 1}|{_invert_text(last_tried)}|{label}"
        return f"{0 if not last_tried else 1}|{last_tried}|{label}"
    if descending:
        return f"{0 if monitored else 1}|{0 if added else 1}|{_invert_text(added)}|{label}"
    return f"{0 if not monitored else 1}|{0 if added else 1}|{added}|{_invert_text(label)}"


def album_sort_key(album: dict | None, order: str, direction: str) -> tuple:
    """Order of albums inside one artist. Missing counts put the smallest gap first when ascending."""
    row = album if isinstance(album, dict) else {}
    key = normalize_search_order(order)
    descending = normalize_search_direction(direction) == "desc"
    title = str(row.get("title") or "").casefold()
    tracks, albums = missing_counts(row)
    if key in {"missing_tracks", "missing_albums"}:
        gap = tracks if key == "missing_tracks" else albums
        return ((-gap if descending else gap), title)
    if key == "release":
        stamp = release_text(row)
        if descending:
            return (0 if stamp else 1, _invert_text(stamp), title)
        return (0 if stamp else 1, stamp, title)
    if key == "added":
        added = str(row.get("added") or "")
        if descending:
            return (0 if added else 1, _invert_text(added), title)
        return (0 if added else 1, added, title)
    if key == "name":
        return (_invert_text(title) if descending else title,)
    monitored = 0 if row.get("monitored", True) else 1
    added = str(row.get("added") or "")
    if descending:
        return (monitored, 0 if added else 1, _invert_text(added), title)
    return (1 - monitored, 0 if added else 1, added, _invert_text(title))


def order_wanted_items(items: list, order: str, direction: str) -> list:
    """Arrange a flat wanted list by artist, then by album inside that artist."""
    groups: dict[str, list] = {}
    order_of: list[str] = []
    for item in items:
        token = (getattr(item, "artist", "") or "").casefold()
        if token not in groups:
            order_of.append(token)
            groups[token] = []
        groups[token].append(item)
    ranked = []
    for token in order_of:
        rows = groups[token]
        albums = [row.payload for row in rows if getattr(row, "kind", "") == "album" and isinstance(row.payload, dict)]
        tracks = 0
        album_count = 0
        release_min = ""
        release_max = ""
        added = ""
        monitored = True
        name = ""
        for row in rows:
            if not name and getattr(row, "artist", ""):
                name = row.artist
            payload = row.payload if isinstance(getattr(row, "payload", None), dict) else {}
            if getattr(row, "kind", "") == "album":
                gap, missing_album = missing_counts(payload)
                tracks += gap
                album_count += missing_album
                stamp = release_text(payload)
                if stamp and missing_album:
                    if not release_min or stamp < release_min:
                        release_min = stamp
                    if stamp > release_max:
                        release_max = stamp
                added = max(added, str(payload.get("added") or ""))
                if payload.get("monitored", True) is False:
                    monitored = False
        ranked.append(
            (
                artist_display_key(
                    order=order,
                    direction=direction,
                    name=name,
                    monitored=monitored,
                    added=added,
                    missing_tracks=tracks,
                    missing_albums=album_count,
                    release_min=release_min,
                    release_max=release_max,
                    last_tried="",
                ),
                token,
            )
        )
    ranked.sort()
    ordered = []
    for _key, token in ranked:
        rows = groups[token]
        header = [row for row in rows if getattr(row, "kind", "") == "artist"]
        rest = [row for row in rows if getattr(row, "kind", "") != "artist"]
        by_album: dict[str, list] = {}
        for row in rest:
            by_album.setdefault(getattr(row, "album", "") or "", []).append(row)

        def group_key(album_name: str) -> tuple:
            sample = next(
                (
                    row.payload
                    for row in by_album[album_name]
                    if getattr(row, "kind", "") == "album" and isinstance(row.payload, dict)
                ),
                {"title": album_name},
            )
            return album_sort_key(sample, order, direction)

        ordered.extend(header)
        for album_name in sorted(by_album, key=group_key):
            block = by_album[album_name]
            block.sort(key=lambda row: (0 if getattr(row, "kind", "") == "album" else 1, getattr(row, "song", "") or ""))
            ordered.extend(block)
    return ordered


def within_display_key(
    *,
    kind: str,
    album: str,
    sort_song: str,
    payload: dict | None,
    order: str,
    direction: str,
    album_gap: int,
) -> str:
    """Keep an artist's rows together: the header, then albums, then that album's songs."""
    if kind == "artist":
        return ""
    key = normalize_search_order(order)
    descending = normalize_search_direction(direction) == "desc"
    name = (album or "").casefold()
    song = sort_song or ""
    if key in {"missing_tracks", "missing_albums"}:
        gap = album_gap
        if kind == "album" and isinstance(payload, dict):
            tracks, albums = missing_counts(payload)
            gap = tracks if key == "missing_tracks" else albums
        return f"{_pad(gap, descending=descending)}|{name}|{song}"
    if key == "release":
        stamp = release_text(payload if kind == "album" else None)
        if not stamp and isinstance(payload, dict):
            stamp = release_text(payload)
        prefix = f"{0 if stamp else 1}|{(_invert_text(stamp) if descending else stamp)}"
        return f"{prefix}|{name}|{song}"
    if key == "name" and descending:
        return f"{_invert_text(name)}|{song}"
    if key == "added":
        added = str((payload or {}).get("added") or "") if isinstance(payload, dict) else ""
        if descending:
            return f"{0 if added else 1}|{_invert_text(added)}|{name}|{song}"
        return f"{0 if added else 1}|{added}|{name}|{song}"
    return f"{name}|{song}"
