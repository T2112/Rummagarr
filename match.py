from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from aioslsk.protocol.primitives import AttributeKey

AUDIO_EXT = {".flac", ".mp3", ".wav", ".aiff", ".aif", ".ogg", ".opus", ".m4a", ".ape", ".wv"}
_MAX_ALIASES = 3

# Quotes and dashes that should compare equal before the rest of the punctuation is dropped.
_PUNCT = str.maketrans(
    "\u2018\u2019\u201a\u201b\u2032\u00b4`\u201c\u201d\u201e\u201f\u2013\u2014\u2015\u2212",
    "'''''''" + '""""' + "----",
)
# Letters NFKD does not split into a base letter plus a mark.
_LETTERS = str.maketrans(
    {
        "ø": "o",
        "ł": "l",
        "đ": "d",
        "ð": "d",
        "þ": "th",
        "æ": "ae",
        "œ": "oe",
        "ı": "i",
        "ß": "ss",
    }
)


def normalize(text: str) -> str:
    """Fold a name so Lidarr spelling and Soulseek filenames can be compared.

    Diacritics become their base letters (Mötley Crüe and Motley Crue). Quotes and
    dashes are unified, & becomes "and", and a letter standing in for a symbol
    (P!nk, Ke$ha) is put back. Other punctuation becomes a space.
    """
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().translate(_LETTERS).translate(_PUNCT)
    text = re.sub(r"(?<=\w)!(?=\w)", "i", text)
    text = re.sub(r"(?<=\w)\$(?=\w)", "s", text)
    text = text.replace("&", " and ")
    text = re.sub(r"[\[\(\{].*?[\]\)\}]", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _add_query(out: list[str], seen: set[str], query: str) -> None:
    query = re.sub(r"\s+", " ", (query or "").strip())
    if not query:
        return
    key = query.casefold()
    if key in seen:
        return
    seen.add(key)
    out.append(query)


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def basename(remote_path: str) -> str:
    return remote_path.replace("/", "\\").split("\\")[-1]


def parent_dir(remote_path: str) -> str:
    parts = remote_path.replace("/", "\\").split("\\")
    return "\\".join(parts[:-1]) if len(parts) > 1 else ""


def extension(remote_path: str) -> str:
    return Path(basename(remote_path)).suffix.lower().lstrip(".")


def bitrate_of(item) -> int:
    try:
        attrs = item.get_attribute_map()
        return int(attrs.get(AttributeKey.BITRATE) or 0)
    except Exception:
        return 0


@dataclass
class FolderCandidate:
    username: str
    remote_dir: str
    files: list = field(default_factory=list)
    score: float = 0.0
    reason: str = ""


def aliases_from_album(album: dict | None) -> list[str]:
    """Artist aliases already sitting on a Lidarr album. No extra request."""
    if not isinstance(album, dict):
        return []
    artist = album.get("artist") or {}
    if not isinstance(artist, dict):
        return []
    primary = artist.get("artistName") or album.get("artistName") or ""
    primary_key = normalize(str(primary))
    raw = artist.get("aliases") or []
    if isinstance(raw, str):
        raw = [raw]
    names: list[str] = []
    seen = set()
    for item in raw:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("aliasName") or "").strip()
        else:
            continue
        if not name:
            continue
        folded = normalize(name)
        token = name.casefold()
        if not folded or folded == primary_key or token in seen:
            continue
        seen.add(token)
        names.append(name)
        if len(names) >= _MAX_ALIASES:
            break
    return names


def build_queries(
    artist: str,
    album: str,
    year: str | None,
    aliases: list[str] | None = None,
) -> list[str]:
    """Folded ASCII search first, then the original spelling, then a few aliases.

    The worker stops once a query scores a useful hit, so the original spelling
    is only used when the folded query does not.
    """
    artist_q = normalize(artist)
    album_q = normalize(album)
    year_text = str(year) if year else ""
    use_year = bool(year_text.isdigit() and not year_text.startswith("2026"))
    artist_raw = _plain(artist)
    album_raw = _plain(album)
    seen: set[str] = set()
    out: list[str] = []

    def emit(artist_part: str, album_part: str, with_year: bool) -> None:
        parts = [artist_part, album_part]
        if with_year:
            parts.append(year_text)
        _add_query(out, seen, " ".join(part for part in parts if part))

    if artist_q or album_q:
        if use_year:
            emit(artist_q, album_q, True)
        emit(artist_q, album_q, False)
    original_differs = artist_raw.casefold() != artist_q or album_raw.casefold() != album_q
    if original_differs and (artist_raw or album_raw):
        if use_year:
            emit(artist_raw, album_raw, True)
        emit(artist_raw, album_raw, False)
    for alias in (aliases or [])[:_MAX_ALIASES]:
        alias_q = normalize(alias)
        if not alias_q or alias_q == artist_q:
            continue
        if use_year:
            emit(alias_q, album_q, True)
        emit(alias_q, album_q, False)
        alias_raw = _plain(alias)
        if alias_raw.casefold() != alias_q:
            emit(alias_raw, album_raw, False)
    return out


def score_folder(
    candidate: FolderCandidate,
    artist: str,
    album: str,
    track_titles: list[str],
    preferred_extensions: list[str],
    min_filename_ratio: float,
    track_count_tolerance: int,
    min_mp3_bitrate: int,
) -> FolderCandidate:
    audio = [f for f in candidate.files if extension(f.filename) in AUDIO_EXT or extension(f.filename) in preferred_extensions]
    if not audio:
        candidate.reason = "no audio"
        return candidate
    exts = {extension(f.filename) for f in audio}
    ext_rank = 0
    for i, pref in enumerate(preferred_extensions):
        if pref in exts:
            ext_rank = len(preferred_extensions) - i
            break
    if ext_rank == 0:
        candidate.reason = f"unwanted types {sorted(exts)}"
        return candidate
    if "mp3" in exts and "flac" not in exts:
        rates = [bitrate_of(f) for f in audio if extension(f.filename) == "mp3"]
        rates = [r for r in rates if r > 0]
        if rates and max(rates) < min_mp3_bitrate:
            candidate.reason = f"mp3 bitrate {max(rates)} < {min_mp3_bitrate}"
            return candidate
    expected = len(track_titles) or 0
    if expected and abs(len(audio) - expected) > track_count_tolerance:
        candidate.reason = f"track count {len(audio)} vs {expected}"
        return candidate
    names = [Path(basename(f.filename)).stem for f in audio]
    if track_titles:
        hits = 0
        for title in track_titles:
            if any(similarity(title, name) >= min_filename_ratio for name in names):
                hits += 1
        title_score = hits / max(len(track_titles), 1)
        if title_score < 0.4:
            candidate.reason = f"title match {title_score:.2f}"
            return candidate
    else:
        title_score = 0.5
    folder_blob = normalize(f"{candidate.remote_dir} {names[0] if names else ''}")
    album_hit = similarity(album, folder_blob)
    artist_hit = similarity(artist, folder_blob)
    if album_hit < 0.25 and artist_hit < 0.2 and title_score < 0.7:
        candidate.reason = "folder does not look like the album"
        return candidate
    candidate.files = audio
    candidate.score = ext_rank * 10 + title_score * 8 + album_hit * 3 + artist_hit * 2 + min(len(audio), expected or len(audio)) * 0.1
    candidate.reason = "ok"
    return candidate


def build_track_queries(
    artist: str,
    album: str,
    track: str,
    aliases: list[str] | None = None,
) -> list[str]:
    artist_q = normalize(artist)
    album_q = normalize(album)
    track_q = normalize(track)
    artist_raw = _plain(artist)
    album_raw = _plain(album)
    track_raw = _plain(track)
    differs = (
        artist_raw.casefold() != artist_q
        or album_raw.casefold() != album_q
        or track_raw.casefold() != track_q
    )
    seen: set[str] = set()
    out: list[str] = []
    if artist_q and track_q and album_q:
        _add_query(out, seen, f"{artist_q} {album_q} {track_q}")
    if differs and artist_raw and track_raw and album_raw:
        _add_query(out, seen, f"{artist_raw} {album_raw} {track_raw}")
    if artist_q and track_q:
        _add_query(out, seen, f"{artist_q} {track_q}")
    if differs and artist_raw and track_raw:
        _add_query(out, seen, f"{artist_raw} {track_raw}")
    for alias in (aliases or [])[:_MAX_ALIASES]:
        alias_q = normalize(alias)
        if not alias_q or alias_q == artist_q or not track_q:
            continue
        _add_query(out, seen, f"{alias_q} {track_q}")
    return out


def score_track_item(
    item, username: str, artist: str, album: str, track: str,
    preferred_extensions: list[str], min_filename_ratio: float, min_mp3_bitrate: int,
) -> tuple[float, str]:
    filename = getattr(item, "filename", "") or ""
    ext = extension(filename).lstrip(".")
    audio = {item.lstrip(".") for item in AUDIO_EXT}
    if ext not in audio:
        return 0.0, "not audio"
    if preferred_extensions and ext not in preferred_extensions:
        return 0.0, f"unwanted type {ext}"
    name = Path(basename(filename)).stem
    title_hit = similarity(track, name)
    if title_hit < min_filename_ratio:
        return 0.0, f"title match {title_hit:.2f}"
    path_blob = normalize(filename)
    artist_hit = similarity(artist, path_blob)
    album_hit = similarity(album, path_blob)
    if ext == "mp3":
        rate = bitrate_of(item)
        if rate and rate < min_mp3_bitrate:
            return 0.0, f"mp3 bitrate {rate} < {min_mp3_bitrate}"
    ext_rank = 0
    for i, pref in enumerate(preferred_extensions):
        if pref == ext:
            ext_rank = len(preferred_extensions) - i
            break
    return ext_rank * 10 + title_hit * 12 + album_hit * 3 + artist_hit * 2, "ok"


def group_results(results, ignored_users: set[str]) -> list[FolderCandidate]:
    buckets: dict[tuple[str, str], FolderCandidate] = {}
    for result in results:
        username = getattr(result, "username", "") or ""
        if username.lower() in ignored_users:
            continue
        items = list(getattr(result, "shared_items", None) or [])
        by_dir: dict[str, list] = defaultdict(list)
        for item in items:
            filename = getattr(item, "filename", "")
            if not filename:
                continue
            by_dir[parent_dir(filename)].append(item)
        for remote_dir, files in by_dir.items():
            key = (username, remote_dir)
            if key not in buckets:
                buckets[key] = FolderCandidate(username=username, remote_dir=remote_dir, files=[])
            buckets[key].files.extend(files)
    return list(buckets.values())
