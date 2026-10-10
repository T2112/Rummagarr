from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path


class ConfigError(Exception):
    """config.toml is missing a value or is not valid TOML."""


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
    start_on_launch: bool = False


def app_data_dir() -> Path:
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(root) / "Lidarr Soulseek"
    override = os.environ.get("LIDARR_SLSK_HOME")
    if override:
        return Path(override)
    return Path.home() / ".lidarr-soulseek"


def bundled_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


def app_version() -> str:
    for path in (bundled_root() / "VERSION", Path(__file__).resolve().parent / "VERSION"):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text
    return "0"


def desktop_config_path() -> Path:
    """Where the desktop app reads and writes config.toml."""
    override = os.environ.get("LIDARR_SLSK_CONFIG")
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        path = app_data_dir() / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return Path(__file__).resolve().parent / "config.toml"


def default_media_dirs() -> tuple[Path, Path]:
    root = Path.home() / "Music" / "Lidarr Soulseek"
    return root / "Incomplete", root / "Complete"


def default_config(**overrides) -> Config:
    download, complete = default_media_dirs()
    download = Path(overrides.pop("download_dir", download))
    complete = Path(overrides.pop("complete_dir", complete))
    cfg = Config(
        lidarr_url=str(overrides.pop("lidarr_url", "http://127.0.0.1:8686")),
        lidarr_api_key=str(overrides.pop("lidarr_api_key", "")),
        lidarr_scan_path=str(overrides.pop("lidarr_scan_path", complete)),
        slsk_username=str(overrides.pop("slsk_username", "")),
        slsk_password=str(overrides.pop("slsk_password", "")),
        listen_port=int(overrides.pop("listen_port", 2234)),
        obfuscated_port=int(overrides.pop("obfuscated_port", 2235)),
        enable_upnp=bool(overrides.pop("enable_upnp", True)),
        download_dir=download,
        complete_dir=complete,
        share_dir=str(overrides.pop("share_dir", "")),
        state_db=Path(overrides.pop("state_db", "state.sqlite3")),
        log_file=Path(overrides.pop("log_file", "lidarr-soulseek.log")),
        search_wait_seconds=float(overrides.pop("search_wait_seconds", 12)),
        max_albums_per_cycle=int(overrides.pop("max_albums_per_cycle", 1)),
        max_tracks_per_cycle=int(overrides.pop("max_tracks_per_cycle", 5)),
        fill_missing_tracks=bool(overrides.pop("fill_missing_tracks", True)),
        focus_one_artist=bool(overrides.pop("focus_one_artist", True)),
        artist_cooldown_days=float(overrides.pop("artist_cooldown_days", 7)),
        max_missing_pages=int(overrides.pop("max_missing_pages", 20)),
        cycle_seconds=int(overrides.pop("cycle_seconds", 300)),
        retry_hours=int(overrides.pop("retry_hours", 12)),
        min_filename_ratio=float(overrides.pop("min_filename_ratio", 0.5)),
        track_count_tolerance=int(overrides.pop("track_count_tolerance", 2)),
        preferred_extensions=list(overrides.pop("preferred_extensions", ["mp3", "flac", "wav"])),
        min_mp3_bitrate=int(overrides.pop("min_mp3_bitrate", 320)),
        convert_to_mp3=bool(overrides.pop("convert_to_mp3", True)),
        convert_bitrate=int(overrides.pop("convert_bitrate", 320)),
        delete_sources_after_import=bool(overrides.pop("delete_sources_after_import", True)),
        clean_incomplete_on_fail=bool(overrides.pop("clean_incomplete_on_fail", True)),
        incomplete_max_age_hours=float(overrides.pop("incomplete_max_age_hours", 12)),
        ignored_users=set(overrides.pop("ignored_users", set())),
        concurrent_albums=int(overrides.pop("concurrent_albums", 3)),
        download_timeout_minutes=int(overrides.pop("download_timeout_minutes", 90)),
        status_bind=str(overrides.pop("status_bind", "127.0.0.1")),
        status_port=int(overrides.pop("status_port", 8787)),
        start_on_launch=bool(overrides.pop("start_on_launch", True)),
    )
    if overrides:
        cfg = replace(cfg, **overrides)
    return cfg


def _toml_str(value: str) -> str:
    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


def save_config(path: Path, cfg: Config) -> None:
    """Write config.toml. The desktop app uses this instead of hand-editing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    extensions = ", ".join(_toml_str(item) for item in cfg.preferred_extensions)
    ignored = ", ".join(_toml_str(item) for item in sorted(cfg.ignored_users))
    text = "\n".join(
        [
            "# Written by Lidarr Soulseek. You can keep editing this file,",
            "# or change the same settings from the app.",
            "",
            "[lidarr]",
            f"url = {_toml_str(cfg.lidarr_url)}",
            f"api_key = {_toml_str(cfg.lidarr_api_key)}",
            f"scan_path = {_toml_str(cfg.lidarr_scan_path)}",
            "",
            "[soulseek]",
            f"username = {_toml_str(cfg.slsk_username)}",
            f"password = {_toml_str(cfg.slsk_password)}",
            f"listen_port = {int(cfg.listen_port)}",
            f"obfuscated_port = {int(cfg.obfuscated_port)}",
            f"enable_upnp = {_toml_bool(cfg.enable_upnp)}",
            "",
            "[paths]",
            f"download_dir = {_toml_str(str(cfg.download_dir))}",
            f"complete_dir = {_toml_str(str(cfg.complete_dir))}",
            f"share_dir = {_toml_str(cfg.share_dir)}",
            f"state_db = {_toml_str(str(cfg.state_db))}",
            f"log_file = {_toml_str(str(cfg.log_file))}",
            "",
            "[search]",
            f"search_wait_seconds = {cfg.search_wait_seconds}",
            f"max_albums_per_cycle = {int(cfg.max_albums_per_cycle)}",
            f"focus_one_artist = {_toml_bool(cfg.focus_one_artist)}",
            f"artist_cooldown_days = {cfg.artist_cooldown_days}",
            f"max_missing_pages = {int(cfg.max_missing_pages)}",
            f"fill_missing_tracks = {_toml_bool(cfg.fill_missing_tracks)}",
            f"max_tracks_per_cycle = {int(cfg.max_tracks_per_cycle)}",
            f"cycle_seconds = {int(cfg.cycle_seconds)}",
            f"retry_hours = {int(cfg.retry_hours)}",
            f"min_filename_ratio = {cfg.min_filename_ratio}",
            f"track_count_tolerance = {int(cfg.track_count_tolerance)}",
            f"preferred_extensions = [{extensions}]",
            f"min_mp3_bitrate = {int(cfg.min_mp3_bitrate)}",
            f"convert_to_mp3 = {_toml_bool(cfg.convert_to_mp3)}",
            f"convert_bitrate = {int(cfg.convert_bitrate)}",
            f"delete_sources_after_import = {_toml_bool(cfg.delete_sources_after_import)}",
            f"clean_incomplete_on_fail = {_toml_bool(cfg.clean_incomplete_on_fail)}",
            f"incomplete_max_age_hours = {cfg.incomplete_max_age_hours}",
            f"ignored_users = [{ignored}]",
            "",
            "[runtime]",
            f"concurrent_albums = {int(cfg.concurrent_albums)}",
            f"download_timeout_minutes = {int(cfg.download_timeout_minutes)}",
            f"status_bind = {_toml_str(cfg.status_bind)}",
            f"status_port = {int(cfg.status_port)}",
            f"start_on_launch = {_toml_bool(cfg.start_on_launch)}",
            "",
        ]
    )
    path.write_text(text, encoding="utf-8")


def load_config(path: Path) -> Config:
    try:
        return try_load_config(path)
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc


def try_load_config(path: Path) -> Config:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"Invalid TOML in {path}: {exc}\n"
            "Every text value must stay inside double quotes.\n"
            "Wrong:  api_key = 1a2b3c4d5e6f\n"
            'Right:  api_key = "1a2b3c4d5e6f"\n'
            "Same for url, username, password, and all folder paths.\n"
            'Windows paths use doubled backslashes: "D:\\\\Media\\\\Incoming\\\\Soulseek"'
        ) from exc
    try:
        lidarr = raw["lidarr"]
        slsk = raw["soulseek"]
        paths = raw["paths"]
        search = raw["search"]
    except KeyError as exc:
        raise ConfigError(f"config.toml is missing a required section or value: {exc}") from exc
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
            raise ConfigError(f"Edit config.toml: {key} is still CHANGEME")

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
        start_on_launch=bool(runtime.get("start_on_launch", False)),
    )
