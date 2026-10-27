"""Write MusicBrainz album, release, and recording ids before Lidarr imports.

Lidarr's album id is the release group. The monitored release id is the
release. Each track's foreign recording id is the recording.
"""

from __future__ import annotations

import logging
from pathlib import Path

from match import AUDIO_EXT, similarity

log = logging.getLogger("rummagarr")


def release_ids(album: dict | None) -> tuple[str, str]:
    """Release id, then release-group id. Either may be blank."""
    if not isinstance(album, dict):
        return "", ""
    group = str(album.get("foreignAlbumId") or "").strip()
    release = ""
    releases = album.get("releases") if isinstance(album.get("releases"), list) else []
    monitored = [item for item in releases if isinstance(item, dict) and item.get("monitored", True)]
    pool = monitored or [item for item in releases if isinstance(item, dict)]
    if pool:
        release = str(pool[0].get("foreignReleaseId") or "").strip()
    if not release:
        release = str(album.get("foreignReleaseId") or "").strip()
    return release, group


def _recording_id(track: dict | None) -> str:
    if not isinstance(track, dict):
        return ""
    return str(track.get("foreignRecordingId") or track.get("foreignTrackId") or "").strip()


def _match_track(path: Path, tracks: list[dict], min_ratio: float) -> dict | None:
    stem = path.stem
    best = None
    best_score = 0.0
    for track in tracks:
        title = str(track.get("title") or "")
        if not title:
            continue
        score = similarity(title, stem)
        if score > best_score:
            best = track
            best_score = score
    if best is not None and best_score >= min_ratio:
        return best
    return None


def write_musicbrainz_tags(path: Path, *, release_id: str, album_id: str, recording_id: str) -> bool:
    """Write the three ids. False when the file type is unsupported or tags cannot be saved."""
    if not any((release_id, album_id, recording_id)):
        return False
    ext = path.suffix.lower().lstrip(".")
    try:
        if ext == "flac":
            return _write_vorbis(path, release_id, album_id, recording_id)
        if ext == "mp3":
            return _write_id3(path, release_id, album_id, recording_id)
        if ext in {"m4a", "mp4", "alac"}:
            return _write_mp4(path, release_id, album_id, recording_id)
        if ext in {"ogg", "opus"}:
            return _write_vorbis(path, release_id, album_id, recording_id)
    except Exception:
        log.exception("Could not write MusicBrainz tags on %s", path.name)
        return False
    return False


def tag_album_folder(folder: Path, album: dict | None, tracks: list[dict] | None, *, min_ratio: float = 0.5) -> int:
    """Tag audio files in one staged folder. Returns how many files were written."""
    release_id, album_id = release_ids(album)
    if not folder.is_dir() or not (release_id or album_id):
        return 0
    written = 0
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXT:
            continue
        track = _match_track(path, list(tracks or []), min_ratio)
        recording = _recording_id(track)
        if write_musicbrainz_tags(path, release_id=release_id, album_id=album_id, recording_id=recording):
            written += 1
    if written:
        log.info("Wrote MusicBrainz tags on %s file(s) in %s", written, folder.name)
    return written


def _assign(tags, key: str, value: str) -> None:
    if value:
        tags[key] = value


def _write_vorbis(path: Path, release_id: str, album_id: str, recording_id: str) -> bool:
    from mutagen.flac import FLAC
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis

    ext = path.suffix.lower()
    if ext == ".flac":
        audio = FLAC(path)
    elif ext == ".opus":
        audio = OggOpus(path)
    else:
        audio = OggVorbis(path)
    _assign(audio, "MUSICBRAINZ_ALBUMID", release_id)
    _assign(audio, "MUSICBRAINZ_RELEASEGROUPID", album_id)
    _assign(audio, "MUSICBRAINZ_TRACKID", recording_id)
    audio.save()
    return True


def _write_id3(path: Path, release_id: str, album_id: str, recording_id: str) -> bool:
    from mutagen.id3 import ID3, ID3NoHeaderError, TXXX
    from mutagen.mp3 import MP3

    audio = MP3(path)
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        audio.add_tags()
        tags = audio.tags
    if tags is None:
        audio.add_tags()
        tags = audio.tags

    def put(desc: str, value: str) -> None:
        if not value or tags is None:
            return
        tags.delall("TXXX:" + desc)
        tags.add(TXXX(encoding=3, desc=desc, text=[value]))

    put("MusicBrainz Album Id", release_id)
    put("MusicBrainz Release Group Id", album_id)
    put("MusicBrainz Track Id", recording_id)
    tags.save(path)
    return True


def _write_mp4(path: Path, release_id: str, album_id: str, recording_id: str) -> bool:
    from mutagen.mp4 import MP4

    audio = MP4(path)
    free = "----:com.apple.iTunes:"

    def put(name: str, value: str) -> None:
        if value:
            audio[free + name] = [value.encode("utf-8")]

    put("MusicBrainz Album Id", release_id)
    put("MusicBrainz Release Group Id", album_id)
    put("MusicBrainz Track Id", recording_id)
    audio.save()
    return True
