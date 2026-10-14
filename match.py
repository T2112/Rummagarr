from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from aioslsk.protocol.primitives import AttributeKey

AUDIO_EXT = {".flac", ".mp3", ".wav", ".aiff", ".aif", ".ogg", ".opus", ".m4a", ".ape", ".wv"}
LOSSLESS_EXT = frozenset({"flac", "wav", "aiff", "aif", "ape", "wv", "alac"})
_LOSSY_EXT = frozenset({"mp3", "m4a", "aac", "ogg", "opus"})
_MAX_ALIASES = 3
# A Lidarr duration at or above this is milliseconds. Soulseek reports seconds.
_MS_DURATION = 10000
_EDITION = re.compile(r"\b(live|remix|remixed|rmx|concert)\b", re.IGNORECASE)
_RELEASE_WORDS = re.compile(
    r"\b(deluxe|expanded|anniversary|remaster(?:ed)?|bonus|instrumental|commentary)\b",
    re.IGNORECASE,
)
_SKIP_KINDS = ("live", "compilation", "single", "bootleg")

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


def duration_of(item) -> float:
    """Soulseek duration in seconds. 0 when the result does not say."""
    try:
        attrs = item.get_attribute_map()
        raw = float(attrs.get(AttributeKey.DURATION) or 0)
    except Exception:
        return 0.0
    if raw >= _MS_DURATION:
        return raw / 1000.0
    return raw if raw > 0 else 0.0


def seconds_from_lidarr(value) -> float:
    """Lidarr track length. Values of 10000 or more are milliseconds."""
    try:
        raw = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if raw >= _MS_DURATION:
        return raw / 1000.0
    return raw if raw > 0 else 0.0


def _bitrate_rank(bitrate: int) -> int:
    if bitrate >= 320:
        return 60
    if bitrate >= 256:
        return 50
    if bitrate >= 192:
        return 40
    if bitrate >= 160:
        return 30
    if bitrate >= 128:
        return 20
    if bitrate > 0:
        return 10
    return 0


def rank_from_quality_name(name: str) -> int:
    text = (name or "").casefold().replace(" ", "").replace("_", "")
    if any(word in text for word in ("flac", "alac", "wav", "aiff", "ape")):
        return 100
    if "320" in text or text.endswith("v0"):
        return 60
    if "256" in text or "v2" in text:
        return 50
    if "192" in text:
        return 40
    if "160" in text:
        return 30
    if "128" in text:
        return 20
    if "96" in text or "096" in text:
        return 10
    return 0


def file_quality_rank(item) -> int | None:
    """Rank of a file. None when an MP3 did not report a bitrate."""
    ext = extension(getattr(item, "filename", "") or "")
    if ext in LOSSLESS_EXT:
        return 100
    if ext == "mp3":
        rate = bitrate_of(item)
        if rate <= 0:
            return None
        return _bitrate_rank(rate)
    if ext in _LOSSY_EXT:
        return 40
    return 0


@dataclass(frozen=True)
class QualityGate:
    """Which files an album's Lidarr profile still allows."""

    name: str
    extensions: frozenset[str]
    cutoff_rank: int


def gate_from_profile(profile: dict | None, preferred: list[str], min_mp3_bitrate: int) -> QualityGate:
    """Any, Lossless, or Standard. Other profile names follow the cutoff.

    ``preferred`` is the user's format order. It picks the one format inside
    the extensions this profile still allows.
    """
    name = str((profile or {}).get("name") or "Any").strip()
    folded = name.casefold() or "any"
    audio = frozenset(ext.lstrip(".") for ext in AUDIO_EXT)
    cutoff = _cutoff_rank(profile)
    if folded == "lossless":
        extensions = LOSSLESS_EXT
        cutoff = max(cutoff, 100)
    elif folded == "standard":
        extensions = _LOSSY_EXT
        cutoff = max(cutoff, _bitrate_rank(int(min_mp3_bitrate or 0)) or 60)
    else:
        extensions = audio
        folded = "any"
    if cutoff >= 100:
        extensions = frozenset(ext for ext in extensions if ext in LOSSLESS_EXT) or LOSSLESS_EXT
    if not preferred and not extensions:
        extensions = audio
    return QualityGate(name=folded, extensions=extensions, cutoff_rank=cutoff)


def _cutoff_rank(profile: dict | None) -> int:
    if not isinstance(profile, dict):
        return 0
    cutoff_id = profile.get("cutoff")
    for item in profile.get("items") or []:
        if not isinstance(item, dict):
            continue
        quality = item.get("quality") if isinstance(item.get("quality"), dict) else {}
        if cutoff_id is not None and quality.get("id") == cutoff_id:
            return rank_from_quality_name(str(quality.get("name") or ""))
    return 0


def file_allowed(item, gate: QualityGate | None) -> bool:
    if gate is None:
        return True
    ext = extension(getattr(item, "filename", "") or "")
    if ext not in gate.extensions:
        return False
    rank = file_quality_rank(item)
    if gate.cutoff_rank <= 0:
        return True
    if rank is None:
        # An MP3 with no bitrate is below a lossless cutoff, and otherwise kept
        # for the bitrate check the caller already applies.
        return gate.cutoff_rank < 100
    return rank >= gate.cutoff_rank


def choose_one_format(files: list, preferred: list[str]) -> list:
    """One extension for the album. Preferred order, then the largest group."""
    grouped: dict[str, list] = defaultdict(list)
    for item in files:
        grouped[extension(getattr(item, "filename", "") or "")].append(item)
    if not grouped:
        return []
    for pref in preferred:
        token = pref.lower().lstrip(".")
        if grouped.get(token):
            return grouped[token]
    return max(grouped.values(), key=len)


def tag_labels(record: dict | None) -> list[str]:
    labels: list[str] = []
    if not isinstance(record, dict):
        return labels
    sources = [record.get("tags")]
    artist = record.get("artist")
    if isinstance(artist, dict):
        sources.append(artist.get("tags"))
    for source in sources:
        if isinstance(source, str):
            source = [source]
        if not isinstance(source, list):
            continue
        for item in source:
            if isinstance(item, str) and item.strip():
                labels.append(item.strip())
            elif isinstance(item, dict):
                label = str(item.get("label") or item.get("name") or "").strip()
                if label:
                    labels.append(label)
    return labels


def album_has_tag(album: dict, tag: str) -> bool:
    wanted = (tag or "").casefold().strip()
    if not wanted:
        return True
    return wanted in {label.casefold() for label in tag_labels(album)}


def album_priority_key(album: dict) -> tuple:
    """Monitored albums first, then the most recently added, then the name."""
    added = str(album.get("added") or "")
    inverted = "".join(chr(ord("9") - ord(ch)) if ch.isdigit() else ch for ch in added) if added else "~"
    artist = ""
    nested = album.get("artist")
    if isinstance(nested, dict):
        artist = str(nested.get("artistName") or "")
    artist = artist or str(album.get("artistName") or "")
    return (
        0 if album.get("monitored", True) else 1,
        inverted,
        artist.casefold(),
        str(album.get("title") or "").casefold(),
    )


def _duration_bonus(files: list, expected: list[float] | None) -> float:
    wanted = [seconds for seconds in (expected or []) if seconds and seconds > 0]
    if not wanted:
        return 0.0
    got = [duration_of(item) for item in files]
    got = [seconds for seconds in got if seconds > 0]
    if not got:
        return 0.0
    hits = 0
    for seconds in wanted:
        slack = max(8.0, seconds * 0.12)
        if any(abs(have - seconds) <= slack for have in got):
            hits += 1
    return 6.0 * hits / len(wanted)


def _studio_penalty(blob: str, wanted: str) -> float:
    if _EDITION.search(blob or "") and not _EDITION.search(wanted or ""):
        return 14.0
    return 0.0


@dataclass
class FolderCandidate:
    username: str
    remote_dir: str
    files: list = field(default_factory=list)
    score: float = 0.0
    reason: str = ""
    free_slot: bool = False


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
    use_year = _include_year(year_text)
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


def _include_year(year_text: str) -> bool:
    """Keep a real release year. A year after this one is Lidarr's placeholder."""
    if not year_text.isdigit() or len(year_text) != 4:
        return False
    year = int(year_text)
    return 1900 <= year <= datetime.now().year


def _edition_penalty(blob: str, wanted: str) -> float:
    """Deluxe, remaster, and the other edition words, unless the Lidarr title uses them."""
    wanted_cf = (wanted or "").casefold()
    penalty = 0.0
    seen: set[str] = set()
    for match in _RELEASE_WORDS.finditer(blob or ""):
        word = match.group(0).casefold()
        if word in seen or word in wanted_cf:
            continue
        seen.add(word)
        penalty += 12.0
    return penalty


def fill_uncovered_titles(primary: list, allowed: list, titles: list[str], min_ratio: float) -> list:
    """Keep the preferred format, and take a missing title from another format in the same folder."""
    if not titles or not allowed:
        return primary
    chosen = list(primary)
    used = {id(item) for item in chosen}
    names = [Path(basename(getattr(item, "filename", "") or "")).stem for item in chosen]

    def covered(title: str) -> bool:
        return any(similarity(title, name) >= min_ratio for name in names)

    for title in titles:
        if covered(title):
            continue
        for item in allowed:
            if id(item) in used:
                continue
            stem = Path(basename(getattr(item, "filename", "") or "")).stem
            if similarity(title, stem) < min_ratio:
                continue
            chosen.append(item)
            used.add(id(item))
            names.append(stem)
            break
    return chosen


def album_release_kinds(album: dict | None) -> set[str]:
    """Live, compilation, single, or bootleg when the album says so."""
    kinds: set[str] = set()
    if not isinstance(album, dict):
        return kinds
    blobs = [str(album.get("albumType") or "")]
    secondary = album.get("secondaryTypes") or []
    if isinstance(secondary, str):
        secondary = [secondary]
    if isinstance(secondary, list):
        for item in secondary:
            if isinstance(item, dict):
                blobs.append(str(item.get("name") or item.get("albumType") or ""))
            else:
                blobs.append(str(item))
    for blob in blobs:
        token = blob.casefold()
        if "live" in token:
            kinds.add("live")
        if "compilation" in token:
            kinds.add("compilation")
        if "single" in token:
            kinds.add("single")
        if "bootleg" in token:
            kinds.add("bootleg")
    return kinds


def _profile_type_allowed(profile: dict | None, kind: str) -> bool:
    if not isinstance(profile, dict):
        return False
    groups = ("secondaryAlbumTypes", "primaryAlbumTypes")
    if kind == "single":
        groups = ("primaryAlbumTypes", "secondaryAlbumTypes")
    for group in groups:
        for item in profile.get(group) or []:
            if not isinstance(item, dict):
                continue
            album_type = item.get("albumType") if isinstance(item.get("albumType"), dict) else {}
            name = str(album_type.get("name") or item.get("name") or "").casefold()
            if kind in name and item.get("allowed"):
                return True
    return False


def release_skip_reason(album: dict | None, cfg, profiles: dict | None = None) -> str:
    """Why this album is not a studio release we should search, or an empty string.

    Live, compilation, single, and bootleg are skipped unless Settings allows
    that type, or Follow Lidarr metadata profile is on and the profile allows it.
    """
    kinds = album_release_kinds(album) & set(_SKIP_KINDS)
    if not kinds:
        return ""
    profile = None
    if getattr(cfg, "follow_metadata_profile", True) and isinstance(album, dict):
        artist = album.get("artist") if isinstance(album.get("artist"), dict) else {}
        raw = album.get("metadataProfileId") or artist.get("metadataProfileId")
        try:
            profile_id = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            profile_id = None
        if profile_id is not None and isinstance(profiles, dict):
            profile = profiles.get(profile_id)
    for kind in _SKIP_KINDS:
        if kind not in kinds:
            continue
        if getattr(cfg, f"allow_{kind}", False):
            continue
        if _profile_type_allowed(profile, kind):
            continue
        return f"skipped {kind} release"
    return ""


def score_folder(
    candidate: FolderCandidate,
    artist: str,
    album: str,
    track_titles: list[str],
    preferred_extensions: list[str],
    min_filename_ratio: float,
    track_count_tolerance: int,
    min_mp3_bitrate: int,
    *,
    durations: list[float] | None = None,
    gate: QualityGate | None = None,
) -> FolderCandidate:
    prefs = [item.lower().lstrip(".") for item in preferred_extensions]
    known_audio = {item.lstrip(".") for item in AUDIO_EXT}
    audio = [item for item in candidate.files if extension(item.filename) in known_audio or extension(item.filename) in prefs]
    if not audio:
        candidate.reason = "no audio"
        candidate.score = 0
        return candidate
    if gate is None:
        allowed = [item for item in audio if extension(item.filename) in prefs] if prefs else audio
        rejected_reason = f"unwanted types {sorted({extension(item.filename) for item in audio})}"
    else:
        allowed = [item for item in audio if file_allowed(item, gate)]
        rejected_reason = "below quality cutoff"
    if not allowed:
        candidate.reason = rejected_reason
        candidate.score = 0
        return candidate
    # One user already owns this folder. Prefer one format, then fill a missing title
    # from another format in the same folder when that is the only complete copy.
    audio = choose_one_format(allowed, prefs)
    audio = fill_uncovered_titles(audio, allowed, track_titles, min_filename_ratio)
    exts = {extension(f.filename) for f in audio}
    ext_rank = 0
    for i, pref in enumerate(preferred_extensions):
        if pref.lower().lstrip(".") in exts:
            ext_rank = len(preferred_extensions) - i
            break
    if ext_rank == 0 and preferred_extensions:
        # The profile allowed a type the user did not list. Still take that one format.
        ext_rank = 1
    if "mp3" in exts:
        rates = [bitrate_of(f) for f in audio if extension(f.filename) == "mp3"]
        rates = [r for r in rates if r > 0]
        if rates and max(rates) < min_mp3_bitrate:
            candidate.reason = f"mp3 bitrate {max(rates)} < {min_mp3_bitrate}"
            candidate.score = 0
            return candidate
    expected = len(track_titles) or 0
    if expected and abs(len(audio) - expected) > track_count_tolerance:
        candidate.reason = f"track count {len(audio)} vs {expected}"
        candidate.score = 0
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
            candidate.score = 0
            return candidate
    else:
        title_score = 0.5
    folder_blob = normalize(f"{candidate.remote_dir} {names[0] if names else ''}")
    album_hit = similarity(album, folder_blob)
    artist_hit = similarity(artist, folder_blob)
    if album_hit < 0.25 and artist_hit < 0.2 and title_score < 0.7:
        candidate.reason = "folder does not look like the album"
        candidate.score = 0
        return candidate
    wanted = " ".join([album, *track_titles])
    blob = " ".join([candidate.remote_dir, *names])
    penalty = _studio_penalty(blob, wanted) + _edition_penalty(blob, wanted)
    bonus = _duration_bonus(audio, durations)
    if expected and len(audio) == expected:
        bonus += 5.0
    free = 4.0 if candidate.free_slot else 0.0
    candidate.files = audio
    candidate.score = max(
        1.0,
        ext_rank * 10 + title_score * 8 + album_hit * 3 + artist_hit * 2 + min(len(audio), expected or len(audio)) * 0.1 + bonus + free - penalty,
    )
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
    *,
    expected_duration: float = 0.0,
    gate: QualityGate | None = None,
) -> tuple[float, str]:
    filename = getattr(item, "filename", "") or ""
    ext = extension(filename).lstrip(".")
    audio = {item.lstrip(".") for item in AUDIO_EXT}
    if ext not in audio:
        return 0.0, "not audio"
    if gate is not None and not file_allowed(item, gate):
        return 0.0, "below quality cutoff"
    if gate is None and preferred_extensions and ext not in preferred_extensions:
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
        if pref.lower().lstrip(".") == ext:
            ext_rank = len(preferred_extensions) - i
            break
    if ext_rank == 0 and gate is not None:
        ext_rank = 1
    duration_bonus = 0.0
    if expected_duration and expected_duration > 0:
        got = duration_of(item)
        if got > 0:
            slack = max(8.0, expected_duration * 0.12)
            if abs(got - expected_duration) <= slack:
                duration_bonus = 6.0
            else:
                duration_bonus = -4.0
    penalty = _studio_penalty(f"{filename} {name}", f"{album} {track}")
    score = max(1.0, ext_rank * 10 + title_hit * 12 + album_hit * 3 + artist_hit * 2 + duration_bonus - penalty)
    return score, "ok"


def group_results(results, ignored_users: set[str]) -> list[FolderCandidate]:
    buckets: dict[tuple[str, str], FolderCandidate] = {}
    for result in results:
        username = getattr(result, "username", "") or ""
        if username.lower() in ignored_users:
            continue
        items = list(getattr(result, "shared_items", None) or [])
        locked_names = {
            getattr(item, "filename", "") or ""
            for item in (getattr(result, "locked_results", None) or [])
        }
        by_dir: dict[str, list] = defaultdict(list)
        for item in items:
            filename = getattr(item, "filename", "")
            if not filename or filename in locked_names:
                continue
            if getattr(item, "locked", False) or getattr(item, "is_locked", False):
                continue
            by_dir[parent_dir(filename)].append(item)
        free = bool(getattr(result, "has_free_slots", False) or getattr(result, "free_slot", False))
        for remote_dir, files in by_dir.items():
            key = (username, remote_dir)
            if key not in buckets:
                buckets[key] = FolderCandidate(username=username, remote_dir=remote_dir, files=[])
            buckets[key].files.extend(files)
            if free or any(getattr(item, "free_slot", False) for item in files):
                buckets[key].free_slot = True
    return list(buckets.values())
