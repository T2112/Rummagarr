from __future__ import annotations

import logging
import os
import shutil
import sys
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from search_order import normalize_search_direction, normalize_search_order

log = logging.getLogger("lidarr_slsk")

APP_NAME = "Rummagarr"
IMPORT_MODES = ("off", "album", "after_artist", "interval")
LEGACY_WINDOWS_DIR = "Lidarr Soulseek"
LEGACY_HOME_DIR = ".lidarr-soulseek"


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
    concurrent_albums: int = 3
    lidarr_track_concurrency: int = 6
    download_timeout_minutes: int = 90
    status_bind: str = "127.0.0.1"
    status_port: int = 8787
    start_on_launch: bool = False
    check_updates: bool = True
    upload_slots: int = 2
    max_queued_files_per_user: int = 25
    max_queued_mb_per_user: int = 2048
    upload_speed_kbps: int = 0
    per_user_speed_kbps: int = 0
    auto_ban_over_queue: bool = True
    require_peer_share: bool = False
    min_peer_share_files: int = 1
    priority_users: set[str] = field(default_factory=set)
    share_dirs: list[str] = field(default_factory=list)
    pause_uploads: bool = False
    auto_import_after_artist: bool = False
    import_mode: str = "album"
    import_interval_minutes: int = 30
    import_timeout_minutes: int = 10
    theme: str = "dark"
    confirm_download_all: bool = True
    max_albums_per_run: int = 40
    wanted_tag: str = ""
    search_order: str = "lidarr"
    search_order_direction: str = "desc"
    follow_metadata_profile: bool = True
    allow_live: bool = False
    allow_compilation: bool = False
    allow_single: bool = False
    allow_bootleg: bool = False
    lidarr_instances: list[dict] = field(default_factory=list)
    lidarr_active: str = "Home"
    bridge_enabled: bool = False
    bridge_host: str = "127.0.0.1"
    bridge_port: int = 8788
    bridge_api_key: str = ""
    bridge_username: str = "rummagarr"
    bridge_password: str = ""
    bridge_category: str = "lidarr"
    bridge_advertise_host: str = ""
    bridge_remote_path: str = ""
    dry_run: bool = False
    quality_upgrades: bool = False
    quality_upgrade_hours: int = 24
    notify_discord_url: str = ""
    notify_ntfy_url: str = ""
    notify_on_import: bool = True
    notify_on_review: bool = True
    plex_url: str = ""
    plex_token: str = ""
    plex_section: str = ""
    jellyfin_url: str = ""
    jellyfin_api_key: str = ""
    browse_max_files: int = 25
    browse_max_mb: int = 2048
    browse_daily_files: int = 20
    crash_bundle: bool = False
    suggest_favorites: bool = True


def normalize_theme(value) -> str:
    """Dark is the default. Anything else unrecognized stays dark."""
    text = str(value or "").strip().lower()
    if text in {"dark", "light"}:
        return text
    return "dark"


def app_data_dir() -> Path:
    """Settings, the database, and the log. An explicit home override is kept as-is."""
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(root) / APP_NAME
    override = os.environ.get("LIDARR_SLSK_HOME")
    if override:
        return Path(override)
    return Path.home() / ".rummagarr"


def legacy_data_dir() -> Path | None:
    """Previous data folder. None when the user already pointed at a folder."""
    if os.environ.get("LIDARR_SLSK_HOME"):
        return None
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(root) / LEGACY_WINDOWS_DIR
    return Path.home() / LEGACY_HOME_DIR


def _retarget_paths(text: str, source: Path, dest: Path) -> str:
    """Point copied settings at the new folder when they named the old one."""
    source_text = str(source)
    dest_text = str(dest)
    pairs = [
        (source_text.replace("\\", "\\\\"), dest_text.replace("\\", "\\\\")),
        (source_text.replace("/", "\\"), dest_text.replace("/", "\\")),
        (source_text.replace("\\", "/"), dest_text.replace("\\", "/")),
        (source_text, dest_text),
    ]
    pairs.sort(key=lambda item: len(item[0]), reverse=True)
    seen: set[str] = set()
    for old, new in pairs:
        if not old or old == new or old in seen:
            continue
        seen.add(old)
        text = text.replace(old, new)
    return text


def migrate_app_data(dest: Path | None = None, source: Path | None = None) -> Path:
    """Copy settings and data from the old folder when this one has none yet.

    The old folder is left in place so an older install can still read it.
    Paths inside config.toml that pointed at the old folder are rewritten.
    """
    dest = dest or app_data_dir()
    if source is None:
        source = legacy_data_dir()
    if source is None:
        return dest
    try:
        if source.resolve() == dest.resolve():
            return dest
    except OSError:
        return dest
    if not source.is_dir():
        return dest
    if (dest / "config.toml").exists():
        return dest
    try:
        names = list(source.iterdir())
    except OSError:
        log.exception("Could not read the previous data folder %s", source)
        return dest
    if not names:
        return dest
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for item in names:
            target = dest / item.name
            if target.exists():
                continue
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
        config = dest / "config.toml"
        if config.exists():
            text = config.read_text(encoding="utf-8")
            rewritten = _retarget_paths(text, source, dest)
            if rewritten != text:
                config.write_text(rewritten, encoding="utf-8")
        log.info("Copied settings and data from %s to %s", source, dest)
    except OSError:
        log.exception("Could not copy settings from %s to %s", source, dest)
    return dest


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
        migrate_app_data()
        path = app_data_dir() / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return Path(__file__).resolve().parent / "config.toml"


def default_media_dirs() -> tuple[Path, Path]:
    root = Path.home() / "Music" / APP_NAME
    return root / "Incomplete", root / "Complete"


def default_config(**overrides) -> Config:
    download, complete = default_media_dirs()
    download = Path(overrides.pop("download_dir", download))
    complete = Path(overrides.pop("complete_dir", complete))
    raw_mode = overrides.pop("import_mode", None)
    import_mode, auto_import = resolve_import_settings(
        raw_mode,
        bool(overrides.pop("auto_import_after_artist", False)),
        mode_was_set=raw_mode is not None and str(raw_mode).strip() != "",
    )
    import_interval = _bounded_minutes(overrides.pop("import_interval_minutes", 30), 30, 1, 1440)
    import_timeout = _bounded_minutes(overrides.pop("import_timeout_minutes", 10), 10, 1, 120)
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
        lidarr_track_concurrency=int(overrides.pop("lidarr_track_concurrency", 6)),
        download_timeout_minutes=int(overrides.pop("download_timeout_minutes", 90)),
        status_bind=str(overrides.pop("status_bind", "127.0.0.1")),
        status_port=int(overrides.pop("status_port", 8787)),
        start_on_launch=bool(overrides.pop("start_on_launch", True)),
        check_updates=bool(overrides.pop("check_updates", True)),
        upload_slots=int(overrides.pop("upload_slots", 2)),
        max_queued_files_per_user=int(overrides.pop("max_queued_files_per_user", 25)),
        max_queued_mb_per_user=int(overrides.pop("max_queued_mb_per_user", 2048)),
        upload_speed_kbps=int(overrides.pop("upload_speed_kbps", 0)),
        per_user_speed_kbps=int(overrides.pop("per_user_speed_kbps", 0)),
        auto_ban_over_queue=bool(overrides.pop("auto_ban_over_queue", True)),
        require_peer_share=bool(overrides.pop("require_peer_share", False)),
        min_peer_share_files=int(overrides.pop("min_peer_share_files", 1)),
        priority_users=set(overrides.pop("priority_users", set())),
        share_dirs=list(overrides.pop("share_dirs", [])),
        pause_uploads=bool(overrides.pop("pause_uploads", False)),
        auto_import_after_artist=auto_import,
        import_mode=import_mode,
        import_interval_minutes=import_interval,
        import_timeout_minutes=import_timeout,
        theme=normalize_theme(overrides.pop("theme", "dark")),
        confirm_download_all=bool(overrides.pop("confirm_download_all", True)),
        max_albums_per_run=int(overrides.pop("max_albums_per_run", 40)),
        wanted_tag=str(overrides.pop("wanted_tag", "")),
        search_order=normalize_search_order(overrides.pop("search_order", "lidarr")),
        search_order_direction=normalize_search_direction(overrides.pop("search_order_direction", "desc")),
        follow_metadata_profile=bool(overrides.pop("follow_metadata_profile", True)),
        allow_live=bool(overrides.pop("allow_live", False)),
        allow_compilation=bool(overrides.pop("allow_compilation", False)),
        allow_single=bool(overrides.pop("allow_single", False)),
        allow_bootleg=bool(overrides.pop("allow_bootleg", False)),
        lidarr_instances=list(overrides.pop("lidarr_instances", [])),
        lidarr_active=str(overrides.pop("lidarr_active", "Home") or "Home"),
        bridge_enabled=bool(overrides.pop("bridge_enabled", False)),
        bridge_host=str(overrides.pop("bridge_host", "127.0.0.1") or "127.0.0.1"),
        bridge_port=int(overrides.pop("bridge_port", 8788)),
        bridge_api_key=str(overrides.pop("bridge_api_key", "")),
        bridge_username=str(overrides.pop("bridge_username", "rummagarr") or "rummagarr"),
        bridge_password=str(overrides.pop("bridge_password", "")),
        bridge_category=str(overrides.pop("bridge_category", "lidarr") or "lidarr"),
        bridge_advertise_host=str(overrides.pop("bridge_advertise_host", "")),
        bridge_remote_path=str(overrides.pop("bridge_remote_path", "")),
        dry_run=bool(overrides.pop("dry_run", False)),
        quality_upgrades=bool(overrides.pop("quality_upgrades", False)),
        quality_upgrade_hours=max(1, int(overrides.pop("quality_upgrade_hours", 24))),
        notify_discord_url=str(overrides.pop("notify_discord_url", "")),
        notify_ntfy_url=str(overrides.pop("notify_ntfy_url", "")),
        notify_on_import=bool(overrides.pop("notify_on_import", True)),
        notify_on_review=bool(overrides.pop("notify_on_review", True)),
        plex_url=str(overrides.pop("plex_url", "")),
        plex_token=str(overrides.pop("plex_token", "")),
        plex_section=str(overrides.pop("plex_section", "")),
        jellyfin_url=str(overrides.pop("jellyfin_url", "")),
        jellyfin_api_key=str(overrides.pop("jellyfin_api_key", "")),
        browse_max_files=max(1, int(overrides.pop("browse_max_files", 25) or 25)),
        browse_max_mb=max(1, int(overrides.pop("browse_max_mb", 2048) or 2048)),
        browse_daily_files=max(1, int(overrides.pop("browse_daily_files", 20) or 20)),
        crash_bundle=bool(overrides.pop("crash_bundle", False)),
        suggest_favorites=bool(overrides.pop("suggest_favorites", True)),
    )
    if overrides:
        cfg = replace(cfg, **overrides)
    return cfg


def _lidarr_instances(lidarr: dict, scan_default: str) -> tuple[list[dict], str, dict]:
    """The saved instances, the active name, and the one the worker should use.

    A config with only url and api_key is one instance named Home.
    """
    raw = lidarr.get("instances") or []
    instances: list[dict] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip().rstrip("/")
            key = str(item.get("api_key") or "").strip()
            if not url or not key:
                continue
            instances.append(
                {
                    "name": str(item.get("name") or "Home").strip() or "Home",
                    "url": url,
                    "api_key": key,
                    "scan_path": str(item.get("scan_path") or scan_default),
                }
            )
    if not instances:
        instances.append(
            {
                "name": "Home",
                "url": str(lidarr["url"]).strip().rstrip("/"),
                "api_key": str(lidarr["api_key"]),
                "scan_path": str(lidarr.get("scan_path") or scan_default),
            }
        )
    active = str(lidarr.get("active") or "").strip()
    if not any(item["name"] == active for item in instances):
        active = instances[0]["name"]
    chosen = next(item for item in instances if item["name"] == active)
    return instances, active, chosen


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


def normalize_import_mode(value, *, legacy_auto: bool | None = None) -> str:
    """One of off, album, after_artist, interval.

    A blank value follows the old checkbox: on is after each artist, off is
    after each album. Off means nothing is imported until Retry import.
    """
    text = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "manual": "off",
        "none": "off",
        "disabled": "off",
        "after_each_artist": "after_artist",
        "artist": "after_artist",
        "each_artist": "after_artist",
        "timer": "interval",
        "timed": "interval",
        "after_each_album": "album",
        "each_album": "album",
        "per_album": "album",
    }
    text = aliases.get(text, text)
    if text in IMPORT_MODES:
        return text
    if legacy_auto is True:
        return "after_artist"
    return "album"


def _bounded_minutes(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return min(high, max(low, number))


def resolve_import_settings(mode, auto: bool, *, mode_was_set: bool) -> tuple[str, bool]:
    """Keep the import mode and the Status checkbox on the same choice.

    A saved mode wins. An older file with only the checkbox maps on to
    after each artist and off to after each album. The checkbox is on only
    for after each artist.
    """
    if not mode_was_set:
        chosen = "after_artist" if auto else "album"
    else:
        chosen = normalize_import_mode(mode, legacy_auto=auto)
        if auto and chosen == "album":
            chosen = "after_artist"
        if not auto and chosen == "after_artist":
            chosen = "album"
    return chosen, chosen == "after_artist"


def save_config(path: Path, cfg: Config) -> None:
    """Write config.toml. The desktop app uses this instead of hand-editing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    extensions = ", ".join(_toml_str(item) for item in cfg.preferred_extensions)
    ignored = ", ".join(_toml_str(item) for item in sorted(cfg.ignored_users))
    priority = ", ".join(_toml_str(item) for item in sorted(cfg.priority_users))
    share_dirs = ", ".join(_toml_str(item) for item in cfg.share_dirs)
    import_mode, auto_import = resolve_import_settings(
        cfg.import_mode,
        bool(cfg.auto_import_after_artist),
        mode_was_set=True,
    )
    text = "\n".join(
        [
            "# Written by Rummagarr. You can keep editing this file,",
            "# or change the same settings from the app.",
            "",
            "[lidarr]",
            f"url = {_toml_str(cfg.lidarr_url)}",
            f"api_key = {_toml_str(cfg.lidarr_api_key)}",
            f"scan_path = {_toml_str(cfg.lidarr_scan_path)}",
            f"auto_import_after_artist = {_toml_bool(auto_import)}",
            f"import_mode = {_toml_str(import_mode)}",
            f"import_interval_minutes = {_bounded_minutes(cfg.import_interval_minutes, 30, 1, 1440)}",
            f"import_timeout_minutes = {_bounded_minutes(cfg.import_timeout_minutes, 10, 1, 120)}",
            f"active = {_toml_str(cfg.lidarr_active or 'Home')}",
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
            f"max_albums_per_run = {int(cfg.max_albums_per_run)}",
            f"wanted_tag = {_toml_str(cfg.wanted_tag)}",
            f"search_order = {_toml_str(normalize_search_order(cfg.search_order))}",
            f"search_order_direction = {_toml_str(normalize_search_direction(cfg.search_order_direction))}",
            f"follow_metadata_profile = {_toml_bool(cfg.follow_metadata_profile)}",
            f"allow_live = {_toml_bool(cfg.allow_live)}",
            f"allow_compilation = {_toml_bool(cfg.allow_compilation)}",
            f"allow_single = {_toml_bool(cfg.allow_single)}",
            f"allow_bootleg = {_toml_bool(cfg.allow_bootleg)}",
            f"dry_run = {_toml_bool(cfg.dry_run)}",
            f"quality_upgrades = {_toml_bool(cfg.quality_upgrades)}",
            f"quality_upgrade_hours = {max(1, int(cfg.quality_upgrade_hours or 24))}",
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
            "[bridge]",
            f"enabled = {_toml_bool(cfg.bridge_enabled)}",
            f"host = {_toml_str(cfg.bridge_host or '127.0.0.1')}",
            f"port = {int(cfg.bridge_port or 8788)}",
            f"api_key = {_toml_str(cfg.bridge_api_key)}",
            f"username = {_toml_str(cfg.bridge_username or 'rummagarr')}",
            f"password = {_toml_str(cfg.bridge_password)}",
            f"category = {_toml_str(cfg.bridge_category or 'lidarr')}",
            f"advertise_host = {_toml_str(cfg.bridge_advertise_host)}",
            f"remote_path = {_toml_str(cfg.bridge_remote_path)}",
            "",
            "[notify]",
            f"discord_url = {_toml_str(cfg.notify_discord_url)}",
            f"ntfy_url = {_toml_str(cfg.notify_ntfy_url)}",
            f"on_import = {_toml_bool(cfg.notify_on_import)}",
            f"on_review = {_toml_bool(cfg.notify_on_review)}",
            f"plex_url = {_toml_str(cfg.plex_url)}",
            f"plex_token = {_toml_str(cfg.plex_token)}",
            f"plex_section = {_toml_str(cfg.plex_section)}",
            f"jellyfin_url = {_toml_str(cfg.jellyfin_url)}",
            f"jellyfin_api_key = {_toml_str(cfg.jellyfin_api_key)}",
            "",
            "[runtime]",
            f"concurrent_albums = {int(cfg.concurrent_albums)}",
            f"lidarr_track_concurrency = {int(cfg.lidarr_track_concurrency)}",
            f"download_timeout_minutes = {int(cfg.download_timeout_minutes)}",
            f"status_bind = {_toml_str(cfg.status_bind)}",
            f"status_port = {int(cfg.status_port)}",
            f"start_on_launch = {_toml_bool(cfg.start_on_launch)}",
            f"check_updates = {_toml_bool(cfg.check_updates)}",
            "",
            "[sharing]",
            f"upload_slots = {int(cfg.upload_slots)}",
            f"max_queued_files_per_user = {int(cfg.max_queued_files_per_user)}",
            f"max_queued_mb_per_user = {int(cfg.max_queued_mb_per_user)}",
            f"upload_speed_kbps = {int(cfg.upload_speed_kbps)}",
            f"per_user_speed_kbps = {int(cfg.per_user_speed_kbps)}",
            f"auto_ban_over_queue = {_toml_bool(cfg.auto_ban_over_queue)}",
            f"require_peer_share = {_toml_bool(cfg.require_peer_share)}",
            f"min_peer_share_files = {int(cfg.min_peer_share_files)}",
            f"priority_users = [{priority}]",
            f"share_dirs = [{share_dirs}]",
            f"pause_uploads = {_toml_bool(cfg.pause_uploads)}",
            "",
            "[ui]",
            f"theme = {_toml_str(normalize_theme(cfg.theme))}",
            f"confirm_download_all = {_toml_bool(cfg.confirm_download_all)}",
            f"crash_bundle = {_toml_bool(cfg.crash_bundle)}",
            f"suggest_favorites = {_toml_bool(cfg.suggest_favorites)}",
            "",
            "[limits]",
            f"browse_max_files = {int(cfg.browse_max_files)}",
            f"browse_max_mb = {int(cfg.browse_max_mb)}",
            f"browse_daily_files = {int(cfg.browse_daily_files)}",
            "",
        ]
    )
    extra = []
    for instance in cfg.lidarr_instances or []:
        extra.extend(
            [
                "[[lidarr.instances]]",
                f"name = {_toml_str(str(instance.get('name') or 'Home'))}",
                f"url = {_toml_str(str(instance.get('url') or ''))}",
                f"api_key = {_toml_str(str(instance.get('api_key') or ''))}",
                f"scan_path = {_toml_str(str(instance.get('scan_path') or ''))}",
                "",
            ]
        )
    if extra:
        text = text + "\n" + "\n".join(extra)
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
    sharing = raw.get("sharing", {})
    ui = raw.get("ui", {})
    bridge = raw.get("bridge", {})
    notify = raw.get("notify", {})
    limits = raw.get("limits", {})

    base = path.parent
    download_dir = Path(paths["download_dir"])
    complete_dir = Path(paths["complete_dir"])
    instances, active, chosen = _lidarr_instances(lidarr, str(complete_dir))
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
    import_mode, auto_import = resolve_import_settings(
        lidarr.get("import_mode", None),
        bool(lidarr.get("auto_import_after_artist", False)),
        mode_was_set="import_mode" in lidarr and str(lidarr.get("import_mode") or "").strip() != "",
    )

    return Config(
        lidarr_url=chosen["url"],
        lidarr_api_key=chosen["api_key"],
        lidarr_scan_path=chosen["scan_path"],
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
        max_albums_per_cycle=int(search.get("max_albums_per_cycle", 1)),
        max_albums_per_run=int(search.get("max_albums_per_run", 40)),
        wanted_tag=str(search.get("wanted_tag", "") or ""),
        search_order=normalize_search_order(search.get("search_order", "lidarr")),
        search_order_direction=normalize_search_direction(search.get("search_order_direction", "desc")),
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
        concurrent_albums=int(runtime.get("concurrent_albums", 3)),
        lidarr_track_concurrency=max(1, min(8, int(runtime.get("lidarr_track_concurrency", 6)))),
        download_timeout_minutes=int(runtime.get("download_timeout_minutes", 90)),
        status_bind=str(runtime.get("status_bind", "127.0.0.1")),
        status_port=int(runtime.get("status_port", 8787)),
        # Missing check_updates stays on, so an existing config starts checking.
        start_on_launch=bool(runtime.get("start_on_launch", False)),
        check_updates=bool(runtime.get("check_updates", True)),
        upload_slots=int(sharing.get("upload_slots", 2)),
        max_queued_files_per_user=int(sharing.get("max_queued_files_per_user", 25)),
        max_queued_mb_per_user=int(sharing.get("max_queued_mb_per_user", 2048)),
        upload_speed_kbps=int(sharing.get("upload_speed_kbps", 0)),
        per_user_speed_kbps=int(sharing.get("per_user_speed_kbps", 0)),
        auto_ban_over_queue=bool(sharing.get("auto_ban_over_queue", True)),
        require_peer_share=bool(sharing.get("require_peer_share", False)),
        min_peer_share_files=int(sharing.get("min_peer_share_files", 1)),
        priority_users={u.lower() for u in sharing.get("priority_users", []) if u},
        share_dirs=[str(item) for item in sharing.get("share_dirs", []) if str(item).strip()],
        pause_uploads=bool(sharing.get("pause_uploads", False)),
        auto_import_after_artist=auto_import,
        import_mode=import_mode,
        import_interval_minutes=_bounded_minutes(lidarr.get("import_interval_minutes", 30), 30, 1, 1440),
        import_timeout_minutes=_bounded_minutes(lidarr.get("import_timeout_minutes", 10), 10, 1, 120),
        theme=normalize_theme(ui.get("theme", "dark")),
        confirm_download_all=bool(ui.get("confirm_download_all", True)),
        follow_metadata_profile=bool(search.get("follow_metadata_profile", True)),
        allow_live=bool(search.get("allow_live", False)),
        allow_compilation=bool(search.get("allow_compilation", False)),
        allow_single=bool(search.get("allow_single", False)),
        allow_bootleg=bool(search.get("allow_bootleg", False)),
        lidarr_instances=instances,
        lidarr_active=active,
        bridge_enabled=bool(bridge.get("enabled", False)),
        bridge_host=str(bridge.get("host", "127.0.0.1") or "127.0.0.1"),
        bridge_port=int(bridge.get("port", 8788)),
        bridge_api_key=str(bridge.get("api_key", "") or ""),
        bridge_username=str(bridge.get("username", "rummagarr") or "rummagarr"),
        bridge_password=str(bridge.get("password", "") or ""),
        bridge_category=str(bridge.get("category", "lidarr") or "lidarr"),
        bridge_advertise_host=str(bridge.get("advertise_host", "") or ""),
        bridge_remote_path=str(bridge.get("remote_path", "") or ""),
        dry_run=bool(search.get("dry_run", False)),
        quality_upgrades=bool(search.get("quality_upgrades", False)),
        quality_upgrade_hours=max(1, int(search.get("quality_upgrade_hours", 24) or 24)),
        notify_discord_url=str(notify.get("discord_url", "") or ""),
        notify_ntfy_url=str(notify.get("ntfy_url", "") or ""),
        notify_on_import=bool(notify.get("on_import", True)),
        notify_on_review=bool(notify.get("on_review", True)),
        plex_url=str(notify.get("plex_url", "") or ""),
        plex_token=str(notify.get("plex_token", "") or ""),
        plex_section=str(notify.get("plex_section", "") or ""),
        jellyfin_url=str(notify.get("jellyfin_url", "") or ""),
        jellyfin_api_key=str(notify.get("jellyfin_api_key", "") or ""),
        browse_max_files=max(1, int(limits.get("browse_max_files", 25) or 25)),
        browse_max_mb=max(1, int(limits.get("browse_max_mb", 2048) or 2048)),
        browse_daily_files=max(1, int(limits.get("browse_daily_files", 20) or 20)),
        crash_bundle=bool(ui.get("crash_bundle", False)),
        suggest_favorites=bool(ui.get("suggest_favorites", True)),
    )
