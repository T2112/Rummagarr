"""Upload limits and matching a peer's files to the wanted catalog.

Nothing in this module opens a socket. The worker calls `admit` before it
accepts an upload, and the Search tab calls `match_files_to_wanted` before it
queues a download.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from match import AUDIO_EXT, basename, extension, normalize, score_track_item, similarity

# Folder names that show up on almost every share and are not artist names.
_SKIP_FOLDERS = {
    "music",
    "mp3",
    "flac",
    "wav",
    "downloads",
    "download",
    "incoming",
    "complete",
    "incomplete",
    "soulseek",
    "various",
    "various artists",
    "va",
}
_TRACK_PREFIX = re.compile(
    r"^(?:(?:cd|disc|disk|side)\s*\d+[\s._\-–—]+)?(?:\d{1,3}|[A-D]\d{1,2})[\s._\-–—]+",
    re.IGNORECASE,
)
# A zero wait must not spin the loop when every artist is still cooling down.
IDLE_FLOOR_SECONDS = 30


@dataclass
class SharePolicy:
    upload_slots: int = 2
    max_queued_files: int = 25
    max_queued_bytes: int = 2048 * 1024 * 1024
    upload_speed_kbps: int = 0
    per_user_speed_kbps: int = 0
    auto_ban: bool = True
    require_peer_share: bool = False
    min_peer_share_files: int = 1
    banned: set[str] = field(default_factory=set)
    priority: set[str] = field(default_factory=set)
    pause_all: bool = False


@dataclass
class QueuedUpload:
    username: str
    filename: str
    size: int
    status: str


@dataclass
class Decision:
    allow: bool
    reason: str = ""
    ban: bool = False


@dataclass
class LiveUpload:
    """One upload the speed limiter can see. `speed` is bytes per second."""

    key: str
    username: str
    speed: float
    status: str
    priority: bool = False


def _folded(name: str) -> str:
    return (name or "").casefold().strip()


def queue_caps(policy: SharePolicy, username: str) -> tuple[int, int]:
    """Priority users get a wider queue. The cap is still enforced."""
    files = max(1, int(policy.max_queued_files or 1))
    size = max(1, int(policy.max_queued_bytes or 1))
    if _folded(username) in policy.priority:
        return files * 4, size * 4
    return files, size


def admit(
    policy: SharePolicy,
    username: str,
    filename: str,
    size: int,
    queued: list[QueuedUpload],
    peer_share_files: int | None,
) -> Decision:
    """Whether this peer may take another file.

    Over the per-user queue always refuses the file. `auto_ban` also puts the
    user on the ignore list. A peer who does not report enough shared files is
    refused when that option is on. Soulseek reports a file count, not bytes.
    """
    name = _folded(username)
    if not name or not filename:
        return Decision(False, "missing user or file")
    if name in policy.banned:
        return Decision(False, "user is ignored", ban=False)
    if policy.require_peer_share:
        reported = int(peer_share_files or 0)
        minimum = max(1, int(policy.min_peer_share_files or 1))
        if reported < minimum:
            return Decision(False, f"user shares {reported} files, minimum is {minimum}")
    active = [
        item
        for item in queued
        if _folded(item.username) == name and item.status in {"queued", "uploading", "paused"}
    ]
    file_cap, byte_cap = queue_caps(policy, username)
    incoming = max(0, int(size or 0))
    if len(active) + 1 > file_cap:
        return Decision(False, f"queue is full ({file_cap} files)", ban=policy.auto_ban)
    used = sum(max(0, int(item.size or 0)) for item in active)
    if used + incoming > byte_cap:
        return Decision(False, "queue is over the size limit", ban=policy.auto_ban)
    return Decision(True)


def uploads_to_pause(policy: SharePolicy, uploads: list[LiveUpload]) -> set[str]:
    """Keys to pause so the global pause switch and the per-user cap hold.

    Priority users are paused last. The cap still applies to them.
    """
    if policy.pause_all:
        return {item.key for item in uploads if item.status in {"queued", "uploading", "paused"}}
    limit = int(policy.per_user_speed_kbps or 0) * 1024
    if limit <= 0:
        return set()
    by_user: dict[str, list[LiveUpload]] = defaultdict(list)
    for item in uploads:
        if item.status == "uploading":
            by_user[_folded(item.username)].append(item)
    paused: set[str] = set()
    for rows in by_user.values():
        ordered = sorted(rows, key=lambda item: (item.priority, -item.speed))
        total = sum(max(0.0, item.speed) for item in rows)
        for item in ordered:
            if total <= limit:
                break
            paused.add(item.key)
            total -= max(0.0, item.speed)
    return paused


@dataclass
class RemoteFile:
    username: str
    filename: str
    size: int = 0
    bitrate: int = 0
    extension: str = ""


@dataclass
class WantedTrack:
    artist: str
    album: str
    song: str
    key: str
    kind: str
    artist_key: str = ""
    album_id: int | None = None
    track_id: int | None = None


@dataclass
class WantedMatch:
    username: str
    filename: str
    size: int
    artist: str
    album: str
    song: str
    key: str
    kind: str
    album_id: int | None
    track_id: int | None
    score: float

    def link(self) -> dict:
        return {
            "kind": self.kind,
            "key": self.key,
            "artist": self.artist,
            "album": self.album,
            "title": self.song or self.album,
            "album_id": self.album_id,
            "track_id": self.track_id,
        }


@dataclass
class ParsedPath:
    artist: str
    album: str
    title: str
    parts: list[str]


def parse_share_path(filename: str) -> ParsedPath:
    """Pull artist, album, and title out of a Soulseek path.

    `Artist - Album\\01 Title.flac` and `Artist\\Album\\01 - Title.flac` both work.
    A leading track number is removed from the title.
    """
    parts = [part for part in (filename or "").replace("/", "\\").split("\\") if part and part not in {".", ".."}]
    file_part = parts[-1] if parts else (filename or "")
    stem = Path(file_part).stem
    title = _TRACK_PREFIX.sub("", stem).strip(" .-_") or stem
    parent = parts[-2] if len(parts) >= 2 else ""
    grand = parts[-3] if len(parts) >= 3 else ""
    artist = ""
    album = ""
    if " - " in parent:
        artist, album = parent.split(" - ", 1)
    elif grand and normalize(grand) not in _SKIP_FOLDERS:
        artist, album = grand, parent
    else:
        album = parent
    return ParsedPath(artist=artist.strip(), album=album.strip(), title=title.strip(), parts=parts)


def _artist_keys(parsed: ParsedPath) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        folded = normalize(text)
        if not folded or folded in _SKIP_FOLDERS or folded in seen or len(folded) < 2:
            return
        seen.add(folded)
        found.append(folded)

    add(parsed.artist)
    for part in parsed.parts[:-1]:
        if " - " in part:
            add(part.split(" - ", 1)[0])
        else:
            add(part)
    return found


class _ScoreItem:
    def __init__(self, filename: str, bitrate: int) -> None:
        self.filename = filename
        self._bitrate = int(bitrate or 0)

    def get_attribute_map(self) -> dict:
        if not self._bitrate:
            return {}
        try:
            from aioslsk.protocol.primitives import AttributeKey

            return {AttributeKey.BITRATE: self._bitrate}
        except Exception:
            return {0: self._bitrate}


def match_files_to_wanted(
    files: list[RemoteFile],
    tracks: list[WantedTrack],
    *,
    min_filename_ratio: float,
    preferred_extensions: list[str],
    min_mp3_bitrate: int,
) -> list[WantedMatch]:
    """Best wanted row for each remote file.

    Only tracks for artists named in the paths are considered. An exact
    accent-folded title hits first. Other titles are scored with the same
    threshold the auto-downloader uses, and only when a word overlaps, so a
    large share does not compare every file with every wanted row. One wanted
    row keeps the highest scoring file.
    """
    by_artist: dict[str, list[WantedTrack]] = defaultdict(list)
    songs_for_album: set[tuple[str, str]] = set()
    exact: dict[tuple[str, str, str], WantedTrack] = {}
    for track in tracks:
        if track.kind not in {"song", "album"}:
            continue
        artist_fold = normalize(track.artist)
        if not artist_fold:
            continue
        by_artist[artist_fold].append(track)
        if track.song:
            album_fold = normalize(track.album)
            songs_for_album.add((artist_fold, str(track.album_id)))
            exact[(artist_fold, album_fold, normalize(track.song))] = track

    best: dict[str, WantedMatch] = {}
    for remote in files:
        ext = (remote.extension or extension(remote.filename) or "").lstrip(".").lower()
        if ext and ext not in AUDIO_EXT and ext not in set(preferred_extensions):
            continue
        parsed = parse_share_path(remote.filename)
        if not parsed.title:
            continue
        candidates: list[WantedTrack] = []
        seen: set[str] = set()
        for key in _artist_keys(parsed):
            for track in by_artist.get(key, []):
                if track.key in seen:
                    continue
                seen.add(track.key)
                candidates.append(track)
        if not candidates:
            continue
        artist_fold = normalize(parsed.artist) or _artist_keys(parsed)[0]
        album_fold = normalize(parsed.album)
        title_fold = normalize(parsed.title)
        chosen: tuple[float, WantedTrack] | None = None
        direct = exact.get((artist_fold, album_fold, title_fold))
        if direct is not None:
            score, reason = score_track_item(
                _ScoreItem(remote.filename, remote.bitrate),
                remote.username,
                direct.artist,
                direct.album,
                direct.song,
                preferred_extensions,
                min_filename_ratio,
                min_mp3_bitrate,
            )
            if score > 0 and reason == "ok":
                chosen = (score + 5, direct)
        if chosen is None:
            title_words = set(title_fold.split())
            album_words = set(album_fold.split())
            for track in candidates:
                if track.song:
                    words = set(normalize(track.song).split())
                    if title_words and words and not (title_words & words):
                        continue
                    if album_fold and normalize(track.album):
                        if similarity(parsed.album, track.album) < 0.4 and not (album_words & set(normalize(track.album).split())):
                            continue
                    score, reason = score_track_item(
                        _ScoreItem(remote.filename, remote.bitrate),
                        remote.username,
                        track.artist,
                        track.album,
                        track.song,
                        preferred_extensions,
                        min_filename_ratio,
                        min_mp3_bitrate,
                    )
                    if score <= 0 or reason != "ok":
                        continue
                else:
                    if (normalize(track.artist), str(track.album_id)) in songs_for_album:
                        continue
                    if not album_fold or similarity(parsed.album, track.album) < max(0.5, min_filename_ratio):
                        continue
                    if ext == "mp3" and remote.bitrate and remote.bitrate < min_mp3_bitrate:
                        continue
                    score = similarity(parsed.album, track.album) * 10
                if chosen is None or score > chosen[0]:
                    chosen = (score, track)
        if chosen is None:
            continue
        score, track = chosen
        match = WantedMatch(
            username=remote.username,
            filename=remote.filename,
            size=int(remote.size or 0),
            artist=track.artist,
            album=track.album,
            song=track.song,
            key=track.key,
            kind=track.kind,
            album_id=track.album_id,
            track_id=track.track_id,
            score=score,
        )
        previous = best.get(track.key)
        if previous is None or match.score > previous.score:
            best[track.key] = match
    return list(best.values())


def group_matches(matches: list[WantedMatch]) -> list[tuple[str, list[WantedMatch]]]:
    groups: dict[str, list[WantedMatch]] = {}
    order: list[str] = []
    for match in matches:
        label = " – ".join(part for part in (match.artist, match.album) if part) or "Wanted"
        if label not in groups:
            groups[label] = []
            order.append(label)
        groups[label].append(match)
    return [(label, groups[label]) for label in order]


def candidate_names(files: list[RemoteFile]) -> tuple[list[str], list[str]]:
    """Artist strings and accent-folded forms to look up. Not every wanted row."""
    names: list[str] = []
    folds: list[str] = []
    seen_names: set[str] = set()
    seen_folds: set[str] = set()
    for remote in files:
        parsed = parse_share_path(remote.filename)
        raw_parts = []
        if parsed.artist:
            raw_parts.append(parsed.artist)
        for part in parsed.parts[:-1]:
            if " - " in part:
                raw_parts.append(part.split(" - ", 1)[0])
            else:
                raw_parts.append(part)
        for part in raw_parts:
            folded = normalize(part)
            if not folded or folded in _SKIP_FOLDERS or len(folded) < 2:
                continue
            token = part.casefold().strip()
            if token not in seen_names:
                seen_names.add(token)
                names.append(part.strip())
            if folded not in seen_folds:
                seen_folds.add(folded)
                folds.append(folded)
    return names, folds
