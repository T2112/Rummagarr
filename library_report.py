"""Wanted counts, a clear-time estimate, and library health checks.

Health is what Rummagarr can see without guessing: partial albums in the
wanted list, a release title that disagrees with the album, audio files
sitting in the complete folder's root, and leftovers in Review.
"""

from __future__ import annotations

import shutil
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wanted import count_value

_AUDIO = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".alac", ".wma"}
_EDITION_WORDS = ("live", "deluxe", "remaster", "anniversary", "remix", "acoustic", "demo")


def album_gap(album: dict) -> tuple[int, int]:
    stats = album.get("statistics") if isinstance(album.get("statistics"), dict) else {}
    files = count_value(stats.get("trackFileCount"))
    total = count_value(stats.get("totalTrackCount") or stats.get("trackCount"))
    if total <= 0 and files <= 0:
        return 1, 1
    return max(0, total - files), (1 if files < total or total <= 0 else 0)


def _artist(album: dict) -> str:
    artist = album.get("artist") if isinstance(album.get("artist"), dict) else {}
    return str(artist.get("artistName") or album.get("artistName") or "Unknown Artist")


def _tags(album: dict) -> list[str]:
    raw = []
    artist = album.get("artist") if isinstance(album.get("artist"), dict) else {}
    for source in (album.get("tags"), artist.get("tags")):
        if isinstance(source, str):
            raw.append(source)
        elif isinstance(source, list):
            raw.extend(source)
    labels = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            labels.append(item.strip())
        elif isinstance(item, dict):
            label = str(item.get("label") or item.get("name") or "").strip()
            if label:
                labels.append(label)
    return labels


def _decade(album: dict) -> str:
    stamp = str(album.get("releaseDate") or "")[:4]
    if not stamp.isdigit():
        return "Unknown"
    year = int(stamp)
    return f"{(year // 10) * 10}s"


def edition_note(album: dict) -> str:
    """A release title or album type that does not match the album title."""
    title = str(album.get("title") or "")
    folded = title.casefold()
    releases = album.get("releases") if isinstance(album.get("releases"), list) else []
    for release in releases:
        if not isinstance(release, dict):
            continue
        release_title = str(release.get("title") or "")
        if not release_title or release_title.casefold() == folded:
            continue
        title_words = {word for word in _EDITION_WORDS if word in folded}
        release_words = {word for word in _EDITION_WORDS if word in release_title.casefold()}
        if title_words != release_words:
            return f"Album title “{title}” does not match release “{release_title}”"
    album_type = str(album.get("albumType") or "").casefold()
    secondary = {str(item).casefold() for item in (album.get("secondaryTypes") or []) if str(item).strip()}
    if "live" in folded and album_type in {"album", "studio"} and "live" not in secondary:
        return "Title says live and the album type does not"
    return ""


def wanted_summary(albums: list[dict], finished: list[dict], next_artists: list[str], *, now: datetime | None = None) -> dict:
    """Counts, an ETA from the last 7 days of finished downloads, and who is next."""
    moment = now or datetime.now(timezone.utc)
    by_artist: Counter[str] = Counter()
    by_tag: Counter[str] = Counter()
    by_decade: Counter[str] = Counter()
    missing_albums = 0
    missing_tracks = 0
    for album in albums:
        gap, missing = album_gap(album)
        if gap <= 0 and not missing:
            continue
        name = _artist(album)
        by_artist[name] += gap or 1
        missing_tracks += gap or 1
        missing_albums += 1 if missing else 0
        for tag in _tags(album):
            by_tag[tag] += 1
        by_decade[_decade(album)] += 1
    week_ago = moment - timedelta(days=7)
    recent = 0
    seen: set[tuple[str, str]] = set()
    for row in finished:
        stamp = _parse_time(str(row.get("finished_at") or ""))
        if stamp is None or stamp < week_ago:
            continue
        key = ((row.get("artist") or "").casefold(), (row.get("title") or "").casefold())
        if key in seen:
            continue
        seen.add(key)
        recent += 1
    per_day = recent / 7 if recent else 0
    eta_days = (missing_albums / per_day) if per_day else None
    return {
        "missing_albums": missing_albums,
        "missing_tracks": missing_tracks,
        "by_artist": by_artist.most_common(12),
        "by_tag": by_tag.most_common(12),
        "by_decade": sorted(by_decade.items()),
        "finished_albums_7d": recent,
        "eta_days": eta_days,
        "next_artists": list(next_artists),
    }


def format_summary(summary: dict) -> str:
    eta = summary.get("eta_days")
    if eta is None:
        eta_line = "ETA: not enough finished downloads in the last 7 days."
    else:
        eta_line = f"ETA: about {eta:.1f} days at the last 7 days' rate ({summary['finished_albums_7d']} albums)."
    lines = [
        f"Missing albums: {summary['missing_albums']}",
        f"Missing tracks: {summary['missing_tracks']}",
        eta_line,
        "",
        "Queued next",
    ]
    nxt = summary.get("next_artists") or []
    lines.extend(f"  {index}. {name}" for index, name in enumerate(nxt, start=1))
    if not nxt:
        lines.append("  Nobody is ready.")
    lines.append("")
    lines.append("By artist")
    lines.extend(f"  {name}: {count}" for name, count in summary.get("by_artist") or [])
    lines.append("")
    lines.append("By tag")
    tags = summary.get("by_tag") or []
    lines.extend(f"  {name}: {count}" for name, count in tags)
    if not tags:
        lines.append("  No tags on the wanted albums.")
    lines.append("")
    lines.append("By decade")
    lines.extend(f"  {name}: {count}" for name, count in summary.get("by_decade") or [])
    return "\n".join(lines)


def health_from_albums(albums: list[dict]) -> list[dict]:
    rows = []
    for album in albums:
        artist = _artist(album)
        title = str(album.get("title") or "Unknown Album")
        gap, missing = album_gap(album)
        album_id = album.get("id")
        if gap > 0:
            rows.append(
                {
                    "kind": "missing",
                    "artist": artist,
                    "album": title,
                    "album_id": album_id,
                    "detail": f"{gap} missing track(s)",
                    "path": "",
                }
            )
        note = edition_note(album)
        if note:
            rows.append(
                {
                    "kind": "edition",
                    "artist": artist,
                    "album": title,
                    "album_id": album_id,
                    "detail": note,
                    "path": "",
                }
            )
    return rows


def scan_folder(root: Path) -> list[dict]:
    """Audio files in the folder root, and anything left under Review."""
    rows = []
    if not root.is_dir():
        return rows
    for path in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        if path.is_file() and path.suffix.casefold() in _AUDIO:
            rows.append(
                {
                    "kind": "unmapped",
                    "artist": "",
                    "album": path.name,
                    "album_id": None,
                    "detail": "File is in the folder root, not an album folder",
                    "path": str(path),
                }
            )
    review = root / "Review"
    if review.is_dir():
        for path in sorted(review.iterdir(), key=lambda item: item.name.casefold()):
            rows.append(
                {
                    "kind": "review",
                    "artist": "",
                    "album": path.name,
                    "album_id": None,
                    "detail": "Left in Review",
                    "path": str(path),
                }
            )
    return rows


def delete_inside(root: Path, target: Path) -> None:
    """Delete a file or folder that lives inside root. Refuses anything else."""
    base = root.resolve()
    path = target.resolve()
    if path == base or base not in path.parents:
        raise ValueError("That path is outside the complete folder.")
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def merge_upgrade_albums(focus: str, missing: list[dict], upgrades: list[dict]) -> list[dict]:
    """Albums below the quality cutoff join the search for that artist."""
    seen = {item.get("id") for item in missing if item.get("id") is not None}
    extra = []
    wanted = (focus or "").casefold()
    for album in upgrades:
        if _artist(album).casefold() != wanted:
            continue
        album_id = album.get("id")
        if album_id is not None and album_id in seen:
            continue
        extra.append(album)
    return [*missing, *extra]


def queued_next(store, cfg, limit: int = 5) -> list[str]:
    """The next artists the worker can search, without deferring anyone."""
    skip: set[str] = set()
    names: list[str] = []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(0, int(getattr(cfg, "retry_hours", 0) or 0)))).isoformat()
    for _ in range(max(0, int(limit or 0))):
        name = store.next_ready_artist(
            skip,
            tag=getattr(cfg, "wanted_tag", "") or "",
            retry_before=cutoff,
            order=getattr(cfg, "search_order", "lidarr") or "lidarr",
            direction=getattr(cfg, "search_order_direction", "desc") or "desc",
        )
        if not name:
            break
        names.append(name)
        skip.add(name.casefold().strip())
    return names


def research_note(store, row: dict) -> str:
    """Clear the artist cooldown and mark the album to be searched again."""
    kind = str(row.get("kind") or "")
    artist = str(row.get("artist") or "")
    album = str(row.get("album") or "")
    if kind not in {"missing", "edition"}:
        return "Re-search applies to a missing album or a mismatched edition."
    if artist:
        store.clear_artist_cooldown(artist)
    album_id = row.get("album_id")
    if album_id is not None:
        store.upsert(int(album_id), artist or "Unknown Artist", album or "Album", "not_sent", "library health")
    label = " ".join(part for part in (artist, album) if part).strip() or "that album"
    return f"Will search {label} again."


def upgrade_artist(upgrades: list[dict]) -> str:
    if not upgrades:
        return ""
    return _artist(upgrades[0])


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)
