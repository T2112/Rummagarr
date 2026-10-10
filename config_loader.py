from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    lidarr_url: str
    lidarr_api_key: str
    lidarr_scan_path: str
    slsk_username: str
    slsk_password: str
    listen_port: int
    obfuscated_port: int
    enable_upnp: bool
    download_dir: Path
    complete_dir: Path
    share_dir: str
    state_db: Path
    log_file: Path
    search_wait_seconds: float
    max_albums_per_cycle: int
    max_tracks_per_cycle: int
    fill_missing_tracks: bool
    focus_one_artist: bool
    artist_cooldown_days: float
    max_missing_pages: int
    cycle_seconds: int
    retry_hours: int
    min_filename_ratio: float
    track_count_tolerance: int
    preferred_extensions: list[str]
    min_mp3_bitrate: int
    convert_to_mp3: bool = True
    convert_bitrate: int = 320
    delete_sources_after_import: bool = True
    clean_incomplete_on_fail: bool = True
    incomplete_max_age_hours: float = 12.0
    ignored_users: set[str] = field(default_factory=set)
    concurrent_albums: int = 1
    download_timeout_minutes: int = 90
    status_bind: str = "127.0.0.1"
    status_port: int = 8787


def load_config(path: Path) -> Config:
    text = path.read_text(encoding="utf-8")
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(
            f"Invalid TOML in {path}: {exc}\n"
            "Every text value must stay inside double quotes.\n"
            "Wrong:  api_key = 1a2b3c4d5e6f\n"
            'Right:  api_key = "1a2b3c4d5e6f"\n'
            'Same for url, username, password, and all folder paths.\n'
            r'Windows paths use doubled backslashes: "D:\\Media\\Incoming\\Soulseek"'
        ) from exc
    lidarr = raw["lidarr"]
    slsk = raw["soulseek"]
    paths = raw["paths"]
    search = raw["search"]
    runtime = raw.get("runtime", {})

    base = path.parent
    download_dir = Path(paths["download_dir"])
    complete_dir = Path(paths["complete_dir"])
    state_db = Path(paths.get("state_db", "state.sqlite3"))
    log_file = Path(paths.get("log_file", "lidarr-soulseek.log"))
    if not state_db.is_absolute():
        state_db = base / state_db
    if not log_file.is_absolute():
        log_file = base / log_file

    for key, value in (
        ("lidarr.api_key", lidarr.get("api_key", "")),
        ("soulseek.username", slsk.get("username", "")),
        ("soulseek.password", slsk.get("password", "")),
    ):
        if not value or value == "CHANGEME":
            raise SystemExit(f"Edit config.toml: {key} is still CHANGEME")

    download_dir.mkdir(parents=True, exist_ok=True)
    complete_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        lidarr_url=lidarr["url"].rstrip("/"),
        lidarr_api_key=lidarr["api_key"],
        lidarr_scan_path=lidarr.get("scan_path", str(complete_dir)),
        slsk_username=slsk["username"],
        slsk_password=slsk["password"],
        listen_port=int(slsk.get("listen_port", 2234)),
        obfuscated_port=int(slsk.get("obfuscated_port", 2235)),
        enable_upnp=bool(slsk.get("enable_upnp", True)),
        download_dir=download_dir,
        complete_dir=complete_dir,
        share_dir=paths.get("share_dir", "") or "",
        state_db=state_db,
        log_file=log_file,
        search_wait_seconds=float(search.get("search_wait_seconds", 12)),
        max_albums_per_cycle=int(search.get("max_albums_per_cycle", 3)),
        max_tracks_per_cycle=int(search.get("max_tracks_per_cycle", 5)),
        fill_missing_tracks=bool(search.get("fill_missing_tracks", True)),
        focus_one_artist=bool(search.get("focus_one_artist", True)),
        artist_cooldown_days=float(search.get("artist_cooldown_days", 7)),
        max_missing_pages=int(search.get("max_missing_pages", 20)),
        cycle_seconds=int(search.get("cycle_seconds", 300)),
        retry_hours=int(search.get("retry_hours", 12)),
        min_filename_ratio=float(search.get("min_filename_ratio", 0.5)),
        track_count_tolerance=int(search.get("track_count_tolerance", 2)),
        preferred_extensions=[e.lower().lstrip(".") for e in search.get("preferred_extensions", ["mp3", "flac", "wav"])],
        min_mp3_bitrate=int(search.get("min_mp3_bitrate", 320)),
        convert_to_mp3=bool(search.get("convert_to_mp3", True)),
        convert_bitrate=int(search.get("convert_bitrate", 320)),
        delete_sources_after_import=bool(search.get("delete_sources_after_import", True)),
        clean_incomplete_on_fail=bool(search.get("clean_incomplete_on_fail", True)),
        incomplete_max_age_hours=float(search.get("incomplete_max_age_hours", 12)),
        ignored_users={u.lower() for u in search.get("ignored_users", []) if u},
        concurrent_albums=int(runtime.get("concurrent_albums", 1)),
        download_timeout_minutes=int(runtime.get("download_timeout_minutes", 90)),
        status_bind=str(runtime.get("status_bind", "127.0.0.1")),
        status_port=int(runtime.get("status_port", 8787)),
    )
