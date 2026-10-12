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

from match import AUDIO_EXT, basename, extension, normalize, parent_dir, score_track_item, similarity

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
    free_slot: bool = False
    speed: int = 0
    queue: int = 0
    state: str = ""
    folder: str = ""


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
class WantedPick:
    """Best files to queue, plus the counts the Search tab shows."""

    matches: list[WantedMatch] = field(default_factory=list)
    matched: int = 0
    users: int = 0
    missing: int = 0
    skipped: int = 0

    def line(self) -> str:
        """N wanted items matched from M users, and K wanted items not found."""
        return (
            f"{self.matched} wanted {_plural(self.matched, 'item')} matched from "
            f"{self.users} {_plural(self.users, 'user')}, and "
            f"{self.missing} wanted {_plural(self.missing, 'item')} not found."
        )


def _plural(count: int, word: str) -> str:
    return word if count == 1 else word + "s"


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


# A file the Search tab already grabbed or handed to the worker.
_DONE_STATES = {"downloaded", "queued", "downloading"}


class _WantedIndex:
    def __init__(self, tracks: list[WantedTrack]) -> None:
        self.by_artist: dict[str, list[WantedTrack]] = defaultdict(list)
        self.songs_for_album: set[tuple[str, str]] = set()
        self.exact: dict[tuple[str, str, str], WantedTrack] = {}
        self.countable: list[WantedTrack] = []
        seen_songs: set[tuple[str, str]] = set()
        for track in tracks:
            if track.kind not in {"song", "album"}:
                continue
            artist_fold = normalize(track.artist)
            if not artist_fold:
                continue
            self.by_artist[artist_fold].append(track)
            if track.song:
                seen_songs.add((artist_fold, str(track.album_id)))
                self.songs_for_album.add((artist_fold, str(track.album_id)))
                self.exact[(artist_fold, normalize(track.album), normalize(track.song))] = track
        for track in tracks:
            if track.kind not in {"song", "album"}:
                continue
            artist_fold = normalize(track.artist)
            if not artist_fold:
                continue
            if track.song or (artist_fold, str(track.album_id)) not in seen_songs:
                self.countable.append(track)


def _file_extension(remote: RemoteFile) -> str:
    return (remote.extension or extension(remote.filename) or "").lstrip(".").lower()


def _extension_rank(ext: str, preferred_extensions: list[str]) -> int:
    cleaned = (ext or "").lstrip(".").lower()
    for index, pref in enumerate(preferred_extensions):
        if pref.lstrip(".").lower() == cleaned:
            return len(preferred_extensions) - index
    return 0


def _source_rank(remote: RemoteFile, score: float, preferred_extensions: list[str]) -> tuple:
    """Free slot, then speed, then format and quality from the Behavior settings."""
    return (
        1 if remote.free_slot else 0,
        int(remote.speed or 0),
        _extension_rank(_file_extension(remote), preferred_extensions),
        int(remote.bitrate or 0),
        float(score),
        -int(remote.queue or 0),
    )


def _folder_of(remote: RemoteFile) -> str:
    return remote.folder or parent_dir(remote.filename)


def _album_identity(track: WantedTrack) -> tuple:
    artist = normalize(track.artist)
    if track.album_id is not None:
        return (artist, "id", int(track.album_id))
    return (artist, "name", normalize(track.album))


def _to_match(remote: RemoteFile, track: WantedTrack, score: float) -> WantedMatch:
    return WantedMatch(
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


def _choose_track(
    remote: RemoteFile,
    index: _WantedIndex,
    *,
    min_filename_ratio: float,
    preferred_extensions: list[str],
    min_mp3_bitrate: int,
) -> tuple[float, WantedTrack] | None:
    """Best wanted row for one remote file. None when the file is not a match."""
    ext = _file_extension(remote)
    if ext and ext not in AUDIO_EXT and ext not in set(preferred_extensions):
        return None
    parsed = parse_share_path(remote.filename)
    if not parsed.title:
        return None
    candidates: list[WantedTrack] = []
    seen: set[str] = set()
    for key in _artist_keys(parsed):
        for track in index.by_artist.get(key, []):
            if track.key in seen:
                continue
            seen.add(track.key)
            candidates.append(track)
    if not candidates:
        return None
    artist_keys = _artist_keys(parsed)
    artist_fold = normalize(parsed.artist) or artist_keys[0]
    album_fold = normalize(parsed.album)
    title_fold = normalize(parsed.title)
    chosen: tuple[float, WantedTrack] | None = None
    direct = index.exact.get((artist_fold, album_fold, title_fold))
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
                    if similarity(parsed.album, track.album) < 0.4 and not (
                        album_words & set(normalize(track.album).split())
                    ):
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
                if (normalize(track.artist), str(track.album_id)) in index.songs_for_album:
                    continue
                if not album_fold or similarity(parsed.album, track.album) < max(0.5, min_filename_ratio):
                    continue
                if ext == "mp3" and remote.bitrate and remote.bitrate < min_mp3_bitrate:
                    continue
                score = similarity(parsed.album, track.album) * 10
            if chosen is None or score > chosen[0]:
                chosen = (score, track)
    return chosen


def _file_matches(
    files: list[RemoteFile],
    tracks: list[WantedTrack],
    *,
    min_filename_ratio: float,
    preferred_extensions: list[str],
    min_mp3_bitrate: int,
) -> tuple[_WantedIndex, list[tuple[RemoteFile, WantedTrack, float]]]:
    index = _WantedIndex(tracks)
    found: list[tuple[RemoteFile, WantedTrack, float]] = []
    for remote in files:
        chosen = _choose_track(
            remote,
            index,
            min_filename_ratio=min_filename_ratio,
            preferred_extensions=preferred_extensions,
            min_mp3_bitrate=min_mp3_bitrate,
        )
        if chosen is None:
            continue
        score, track = chosen
        found.append((remote, track, score))
    return index, found


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
    _index, found = _file_matches(
        files,
        tracks,
        min_filename_ratio=min_filename_ratio,
        preferred_extensions=preferred_extensions,
        min_mp3_bitrate=min_mp3_bitrate,
    )
    best: dict[str, WantedMatch] = {}
    for remote, track, score in found:
        match = _to_match(remote, track, score)
        previous = best.get(track.key)
        if previous is None or match.score > previous.score:
            best[track.key] = match
    return list(best.values())


def narrow_wanted_tracks(
    tracks: list[WantedTrack],
    *,
    kind: str = "",
    album: str = "",
    album_id: int | None = None,
    track_id: int | None = None,
    title: str = "",
) -> list[WantedTrack]:
    """Keep the album or song a magnifier search belongs to.

    An artist search keeps every album and song. An album search keeps that
    album. A song search keeps that song.
    """
    kind_name = (kind or "").casefold()
    if kind_name == "song" and (track_id is not None or title):
        chosen: list[WantedTrack] = []
        title_fold = normalize(title)
        album_fold = normalize(album)
        for track in tracks:
            if track_id is not None and track.track_id == track_id:
                chosen.append(track)
                continue
            if title_fold and track.song and normalize(track.song) == title_fold:
                if album_fold and normalize(track.album) != album_fold:
                    continue
                chosen.append(track)
        return chosen
    if kind_name == "album" and (album_id is not None or album):
        chosen = []
        album_fold = normalize(album)
        for track in tracks:
            if album_id is not None and track.album_id == album_id:
                chosen.append(track)
            elif album_fold and normalize(track.album) == album_fold:
                chosen.append(track)
        return chosen
    return [track for track in tracks if track.kind in {"album", "song"}]


def _busy(remote: RemoteFile) -> bool:
    return (remote.state or "").casefold() in _DONE_STATES


def _prefer_copy(
    current: tuple[RemoteFile, WantedTrack, float],
    new: tuple[RemoteFile, WantedTrack, float],
    preferred_extensions: list[str],
) -> tuple[RemoteFile, WantedTrack, float]:
    """One path is enough. A copy already downloaded or queued wins, so it is not queued again."""
    if _busy(new[0]) and not _busy(current[0]):
        return new
    if _busy(current[0]) and not _busy(new[0]):
        return current
    if new[2] != current[2]:
        return new if new[2] > current[2] else current
    if _source_rank(new[0], new[2], preferred_extensions) > _source_rank(current[0], current[2], preferred_extensions):
        return new
    return current


def _best_of(
    candidates: list[tuple[RemoteFile, WantedTrack, float]],
    preferred_extensions: list[str],
) -> tuple[RemoteFile, WantedTrack, float]:
    return max(candidates, key=lambda item: _source_rank(item[0], item[2], preferred_extensions))


def _pick_album_songs(
    songs: list[WantedTrack],
    pool: list[tuple[RemoteFile, WantedTrack, float]],
    preferred_extensions: list[str],
) -> list[WantedMatch]:
    """One folder that has every available song, or the best user for each song that is missing."""
    by_song: dict[str, list[tuple[RemoteFile, WantedTrack, float]]] = defaultdict(list)
    folders: dict[tuple[str, str], dict[str, list[tuple[RemoteFile, WantedTrack, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for remote, track, score in pool:
        if not track.song:
            continue
        by_song[track.key].append((remote, track, score))
        folders[(remote.username.casefold(), _folder_of(remote).casefold())][track.key].append((remote, track, score))
    open_keys = [song.key for song in songs if song.key in by_song]
    if not open_keys:
        return []

    def folder_rank(song_map: dict[str, list[tuple[RemoteFile, WantedTrack, float]]]) -> tuple:
        bests = [_best_of(song_map[key], preferred_extensions) for key in open_keys if key in song_map]
        if len(bests) < len(open_keys):
            return tuple()
        ranks = [_extension_rank(_file_extension(remote), preferred_extensions) for remote, _track, _score in bests]
        return (
            1 if any(remote.free_slot for remote, _track, _score in bests) else 0,
            max(int(remote.speed or 0) for remote, _track, _score in bests),
            min(ranks) if ranks else 0,
            sum(int(remote.bitrate or 0) for remote, _track, _score in bests),
            sum(score for _remote, _track, score in bests),
            -min(int(remote.queue or 0) for remote, _track, _score in bests),
        )

    complete = [folder_id for folder_id, song_map in folders.items() if all(key in song_map for key in open_keys)]
    chosen: dict[str, tuple[RemoteFile, WantedTrack, float]] = {}
    if complete:
        winner = max(complete, key=lambda folder_id: folder_rank(folders[folder_id]))
        for key in open_keys:
            chosen[key] = _best_of(folders[winner][key], preferred_extensions)
    else:
        for key in open_keys:
            chosen[key] = _best_of(by_song[key], preferred_extensions)
    return [_to_match(*chosen[key]) for key in open_keys]


def _pick_album_folder(
    track: WantedTrack,
    pool: list[tuple[RemoteFile, WantedTrack, float]],
    preferred_extensions: list[str],
) -> list[WantedMatch]:
    """Album row with no songs: the fullest matching folder, one format."""
    folders: dict[tuple[str, str], list[tuple[RemoteFile, WantedTrack, float]]] = defaultdict(list)
    for remote, matched, score in pool:
        if matched.key != track.key:
            continue
        folders[(remote.username.casefold(), _folder_of(remote).casefold())].append((remote, matched, score))
    if not folders:
        return []

    def kept_files(
        candidates: list[tuple[RemoteFile, WantedTrack, float]],
    ) -> list[tuple[RemoteFile, WantedTrack, float]]:
        best_rank = max(_extension_rank(_file_extension(remote), preferred_extensions) for remote, _track, _score in candidates)
        return [
            item
            for item in candidates
            if _extension_rank(_file_extension(item[0]), preferred_extensions) == best_rank
        ]

    def folder_rank(candidates: list[tuple[RemoteFile, WantedTrack, float]]) -> tuple:
        kept = kept_files(candidates)
        best_rank = _extension_rank(_file_extension(kept[0][0]), preferred_extensions)
        return (
            len(kept),
            1 if any(remote.free_slot for remote, _track, _score in kept) else 0,
            max(int(remote.speed or 0) for remote, _track, _score in kept),
            best_rank,
            sum(score for _remote, _track, score in kept),
            -min(int(remote.queue or 0) for remote, _track, _score in kept),
        )

    _folder_id, candidates = max(folders.items(), key=lambda item: folder_rank(item[1]))
    return [_to_match(*item) for item in kept_files(candidates)]


def choose_wanted_downloads(
    files: list[RemoteFile],
    tracks: list[WantedTrack],
    *,
    min_filename_ratio: float,
    preferred_extensions: list[str],
    min_mp3_bitrate: int,
) -> WantedPick:
    """Best source for every wanted album and track across every user's files.

    A folder that contains every wanted song anyone is sharing for that album
    is taken from that one user. Free slot, then speed, then the configured
    format and quality break the tie. When nobody has the whole album, each
    missing song comes from the best other user. Files already downloaded or
    queued, and a repeated path, are not queued again.
    """
    index, found = _file_matches(
        files,
        tracks,
        min_filename_ratio=min_filename_ratio,
        preferred_extensions=preferred_extensions,
        min_mp3_bitrate=min_mp3_bitrate,
    )
    deduped: dict[tuple[str, str], tuple[RemoteFile, WantedTrack, float]] = {}
    for remote, track, score in found:
        identity = (remote.username.casefold(), remote.filename)
        current = deduped.get(identity)
        pair = (remote, track, score)
        if current is None:
            deduped[identity] = pair
        else:
            deduped[identity] = _prefer_copy(current, pair, preferred_extensions)

    satisfied: set[str] = set()
    available: list[tuple[RemoteFile, WantedTrack, float]] = []
    for remote, track, score in deduped.values():
        if _busy(remote):
            satisfied.add(track.key)
            continue
        available.append((remote, track, score))
    available = [item for item in available if item[1].key not in satisfied]

    by_album: dict[tuple, list[tuple[RemoteFile, WantedTrack, float]]] = defaultdict(list)
    for remote, track, score in available:
        by_album[_album_identity(track)].append((remote, track, score))

    albums: dict[tuple, list[WantedTrack]] = defaultdict(list)
    for track in index.countable:
        albums[_album_identity(track)].append(track)

    matches: list[WantedMatch] = []
    matched_keys: set[str] = set()
    for album_key, album_tracks in albums.items():
        songs = [track for track in album_tracks if track.song and track.key not in satisfied]
        album_only = [track for track in album_tracks if not track.song and track.key not in satisfied]
        pool = by_album.get(album_key, [])
        picked: list[WantedMatch] = []
        if songs:
            picked = _pick_album_songs(songs, pool, preferred_extensions)
        elif album_only:
            for track in album_only:
                picked.extend(_pick_album_folder(track, pool, preferred_extensions))
        for match in picked:
            if match.song and match.key in matched_keys:
                continue
            matches.append(match)
            matched_keys.add(match.key)

    missing = [track for track in index.countable if track.key not in matched_keys and track.key not in satisfied]
    users = {match.username.casefold() for match in matches if match.username}
    return WantedPick(
        matches=matches,
        matched=len(matched_keys),
        users=len(users),
        missing=len(missing),
        skipped=len(satisfied & {track.key for track in index.countable}),
    )


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
