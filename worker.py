from __future__ import annotations

import asyncio
import logging
import re
import shutil
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

from aioslsk.client import SoulSeekClient
from aioslsk.exceptions import FileNotFoundError as SlskFileNotFoundError
from aioslsk.exceptions import FileNotSharedError
from aioslsk.settings import (
    CredentialsSettings,
    NetworkSettings,
    Settings,
    SharesSettings,
    SharedDirectorySettingEntry,
    ListeningSettings,
    NetworkLimitSettings,
    UpnpSettings,
    ServerSettings,
    ReconnectSettings,
    TransferLimitSettings,
    TransfersSettings,
)
from aioslsk.shares.model import DirectoryShareMode
from aioslsk.user.model import BlockingFlag

from activity import Step, describe_step, format_elapsed, step_from_legacy
from audio_check import folder_fake_reason
from audio_tags import tag_album_folder
from config_loader import Config
from favorites import GOOD_USER_MINIMUM, daily_room
from control import WorkerControl
from library_report import merge_upgrade_albums, upgrade_artist
from bridge_net import advertise_host, host_for_url, is_loopback_bind, mapped_folder, reported_root
from lidarr_bridge import BridgeService, GrabBook, bridge_settings
from lidarr_client import LidarrClient, LidarrUnreachable
from notifications import deliver
from search_model import (
    counts_for_server,
    explain_search,
    file_from_share,
    neutral_search_outcome,
    result_count,
    search_receipt,
)
from status_ui import normalize_recent_limit, start_status_ui
from wanted import (
    WantedCatalog,
    album_key,
    albums_for_search,
    artist_name,
    count_value,
    holes_for_search,
    is_enabled,
    items_to_rows,
    optional_id,
    song_key,
    wanted_from_library,
)
from match import (
    AUDIO_EXT,
    QualityGate,
    album_has_tag,
    aliases_from_album,
    build_queries,
    build_track_queries,
    gate_from_profile,
    group_results,
    release_skip_reason,
    normalize,
    score_folder,
    score_track_item,
    seconds_from_lidarr,
)
from process_launch import run_hidden
from search_order import album_sort_key, normalize_search_direction, normalize_search_order, order_wanted_items
from run_summary import RunSummary
from sharing import (
    IDLE_FLOOR_SECONDS,
    Decision,
    LiveUpload,
    QueuedUpload,
    RemoteFile,
    SharePolicy,
    WantedPick,
    WantedTrack,
    admit,
    candidate_names,
    choose_wanted_downloads,
    match_files_to_wanted,
    narrow_wanted_tracks,
    uploads_to_pause,
)
from state import JobStore

log = logging.getLogger("lidarr_slsk")


def safe_name(text: str) -> str:
    text = re.sub(r'[<>:"/\\|?*]', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:120] or "Unknown"


def album_year(album: dict) -> str | None:
    raw = album.get("releaseDate") or ""
    return raw[:4] if raw and raw[:4].isdigit() else None


def monitored_missing_tracks(tracks: list[dict]) -> list[dict]:
    out = []
    for track in tracks:
        if not track.get("monitored", True):
            continue
        if track.get("hasFile"):
            continue
        out.append(track)
    return out


def track_titles(tracks: list[dict]) -> list[str]:
    titles = []
    for t in tracks:
        title = t.get("title") or t.get("mediumNumber")
        if title:
            titles.append(str(title))
    return titles


def state_name(transfer) -> str:
    state = getattr(transfer, "state", None)
    if state is None:
        return ""
    for attr in ("NAME", "name"):
        val = getattr(state, attr, None)
        if isinstance(val, str):
            return val.upper()
    text = str(state).upper()
    for token in ("COMPLETE", "FAILED", "ABORTED", "INCOMPLETE", "DOWNLOADING", "QUEUED", "PAUSED"):
        if token in text:
            return token
    return text


def transfer_filename(transfer) -> str:
    raw = getattr(transfer, "filename", None) or getattr(transfer, "remote_path", None) or ""
    text = str(raw).replace("/", "\\")
    return text.split("\\")[-1] or text or "file"


def transfer_username(transfer) -> str:
    return getattr(transfer, "username", None) or getattr(getattr(transfer, "peer", None), "username", "") or ""


def transfer_percent(transfer) -> float | None:
    size = getattr(transfer, "filesize", None) or getattr(transfer, "size", None) or 0
    done = getattr(transfer, "bytes_transfered", None)
    if done is None:
        done = getattr(transfer, "bytes_transferred", None)
    try:
        size_i = int(size or 0)
        done_i = int(done or 0)
    except (TypeError, ValueError):
        return None
    if size_i <= 0:
        return None
    return min(100.0, done_i * 100.0 / size_i)


def _upload_key(transfer) -> str:
    return f"{transfer_username(transfer)}\n{getattr(transfer, 'remote_path', '') or ''}"


def _upload_status(transfer) -> str:
    name = state_name(transfer)
    if name in {"QUEUED", "VIRGIN", "INITIALIZING"}:
        return "queued"
    if name in {"UPLOADING", "DOWNLOADING", "INCOMPLETE"}:
        return "uploading"
    if name == "PAUSED":
        return "paused"
    if name == "COMPLETE":
        return "done"
    if name == "FAILED":
        return "failed"
    if name == "ABORTED":
        return "cancelled"
    return "queued"


async def abort_transfer(client: SoulSeekClient, transfer) -> None:
    manager = getattr(client, "transfers", None)
    if manager is None:
        return
    for name in ("abort", "cancel", "stop"):
        fn = getattr(manager, name, None)
        if fn is None:
            continue
        result = fn(transfer)
        if asyncio.iscoroutine(result):
            await result
        return
    abort_fn = getattr(transfer, "abort", None)
    if abort_fn:
        result = abort_fn()
        if asyncio.iscoroutine(result):
            await result


def effective_share_dirs(cfg: Config) -> list[str]:
    """share_dir plus any extra folders, without duplicates or blanks."""
    found: list[str] = []
    for raw in [cfg.share_dir, *list(cfg.share_dirs or [])]:
        text = str(raw or "").strip()
        if text and text not in found:
            found.append(text)
    return found


def policy_from_config(cfg: Config) -> SharePolicy:
    mb = max(1, int(cfg.max_queued_mb_per_user or 1))
    return SharePolicy(
        upload_slots=max(1, int(cfg.upload_slots or 1)),
        max_queued_files=max(1, int(cfg.max_queued_files_per_user or 1)),
        max_queued_bytes=mb * 1024 * 1024,
        upload_speed_kbps=max(0, int(cfg.upload_speed_kbps or 0)),
        per_user_speed_kbps=max(0, int(cfg.per_user_speed_kbps or 0)),
        auto_ban=bool(cfg.auto_ban_over_queue),
        require_peer_share=bool(cfg.require_peer_share),
        min_peer_share_files=max(1, int(cfg.min_peer_share_files or 1)),
        banned={name.casefold() for name in cfg.ignored_users},
        priority={name.casefold() for name in cfg.priority_users},
        pause_all=bool(cfg.pause_uploads),
    )


def build_slsk_settings(cfg: Config) -> Settings:
    directories = [
        SharedDirectorySettingEntry(path=path, share_mode=DirectoryShareMode.EVERYONE)
        for path in effective_share_dirs(cfg)
    ]
    speed = max(0, int(cfg.upload_speed_kbps or 0))
    return Settings(
        credentials=CredentialsSettings(username=cfg.slsk_username, password=cfg.slsk_password),
        network=NetworkSettings(
            server=ServerSettings(reconnect=ReconnectSettings(auto=True, timeout=15)),
            listening=ListeningSettings(
                port=cfg.listen_port,
                obfuscated_port=cfg.obfuscated_port,
            ),
            upnp=UpnpSettings(enabled=cfg.enable_upnp),
            limits=NetworkLimitSettings(upload_speed_kbps=speed),
        ),
        shares=SharesSettings(
            download=str(cfg.download_dir),
            directories=directories,
            # A long share scan can outlive the server socket. Scan after login.
            scan_on_start=False,
        ),
        transfers=TransfersSettings(
            limits=TransferLimitSettings(upload_slots=max(1, int(cfg.upload_slots or 1)))
        ),
    )


LOSSLESS_EXT = {".flac", ".wav", ".aiff", ".aif", ".wv", ".ape"}
# Yield this often so a large share scan cannot freeze the loop that reads search replies.
_SCAN_CHUNK = 25
# How long to wait for the server to accept a FileSearch before moving on.
_SEARCH_SEND_TIMEOUT = 20.0
_FREE_SLOT_GRACE = 3.0
_DOWNLOAD_ATTEMPTS = 3
_COMMAND_FAILED = {"failed", "aborted", "cancelled", "canceled", "timeout"}


def install_responsive_share_scan(client) -> None:
    """Let the event loop read peer search replies while a share index is built.

    aioslsk builds the term map, and schedules every attribute job, without an
    await. On a large library that blocks the loop for longer than Search wait,
    so replies sit in the socket until the Search tab drops the ticket.
    """
    shares = getattr(client, "shares", None)
    if shares is None or getattr(shares, "_lidarr_responsive_scan", False):
        return

    async def scan_directory_files(shared_directory):
        from aioslsk.exceptions import SharedDirectoryError
        from aioslsk.shares.manager import scan_directory as walk_share

        loop = asyncio.get_running_loop()
        if shared_directory not in shares._shared_directories:
            raise SharedDirectoryError("attempted to scan directory which was not added to the manager")
        try:
            shared_items = await loop.run_in_executor(
                shares.executor,
                partial(
                    walk_share,
                    shared_directory,
                    children=shares._get_child_directories(shared_directory),
                ),
            )
        except Exception:
            log.exception("exception scanning directory : %r", shared_directory)
        else:
            for item in shared_items:
                item.shared_directory = shared_directory
            shared_directory.items |= shared_items
            shared_directory.items -= shared_directory.items ^ shared_items
        items = list(shared_directory.items)
        for start in range(0, len(items), _SCAN_CHUNK):
            for item in items[start : start + _SCAN_CHUNK]:
                shares._add_item_to_term_map(item)
            await asyncio.sleep(0)
        shares._cleanup_term_map()
        await asyncio.sleep(0)

    async def scan_directory_file_attributes(shared_directory):
        from aioslsk.shares.manager import extract_attributes

        loop = asyncio.get_running_loop()
        futures: list[asyncio.Future] = []
        scheduled = 0
        for item in list(shared_directory.items):
            if item.attributes is None:
                future = loop.run_in_executor(
                    shares.executor,
                    partial(extract_attributes, item.get_absolute_path()),
                )
                future.add_done_callback(partial(shares._extract_attributes_callback, item))
                futures.append(future)
                scheduled += 1
                if scheduled % _SCAN_CHUNK == 0:
                    await asyncio.sleep(0)
        await asyncio.gather(*futures, return_exceptions=True)

    shares.scan_directory_files = scan_directory_files
    shares.scan_directory_file_attributes = scan_directory_file_attributes
    shares._lidarr_responsive_scan = True


# How many incoming share queries may be queued before new ones are dropped.
# Answering them is optional. Reading the replies to our own searches is not.
_QUERY_QUEUE = 8
_QUERY_SLOTS = 2


def install_nonblocking_share_queries(client) -> None:
    """Answer other people's searches off the loop that reads our own replies.

    aioslsk runs SharesManager.query inside the server and parent readers, with
    no await. Once a library is indexed, that call walks the term map. The
    server sends those searches continuously to a client that has no distributed
    parent yet, so the reader never gets back to accepting peer connections.
    Search replies sit unaccepted and the Search tab stays on Searching.
    """
    searches = getattr(client, "searches", None)
    if searches is None or getattr(searches, "_lidarr_async_query", False):
        return

    from aioslsk.events import SearchRequestReceivedEvent
    from aioslsk.protocol.messages import PeerSearchReply
    from aioslsk.search.model import ReceivedSearch
    from aioslsk.shares.utils import convert_items_to_file_data

    searches._lidarr_async_query = True
    searches._lidarr_query_sem = asyncio.Semaphore(_QUERY_SLOTS)
    searches._lidarr_query_tasks: set[asyncio.Task] = set()
    pending = {"n": 0}

    async def answer(ticket: int, username: str, query: str) -> None:
        try:
            session = searches._session
            if session is None:
                return
            if searches._settings.users.is_blocked(username, BlockingFlag.SEARCHES):
                return
            shares = searches._shares_manager
            loop = asyncio.get_running_loop()
            excluded = list(searches.excluded_search_phrases or [])
            try:
                async with searches._lidarr_query_sem:
                    visible, locked = await loop.run_in_executor(
                        getattr(shares, "executor", None),
                        partial(
                            shares.query,
                            query,
                            username=username,
                            excluded_search_phrases=excluded,
                        ),
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Could not query shares for %r", query)
                return
            if searches._session is None:
                return
            found = len(visible) + len(locked)
            searches.received_searches.append(
                ReceivedSearch(username=username, query=query, result_count=found)
            )
            await searches._event_bus.emit(
                SearchRequestReceivedEvent(username=username, query=query, result_count=found)
            )
            if found == 0:
                return
            task = asyncio.create_task(
                searches._network.send_peer_messages(
                    username,
                    PeerSearchReply.Request(
                        username=searches._session.user.name,
                        ticket=ticket,
                        results=convert_items_to_file_data(visible, use_full_path=True),
                        has_slots_free=searches._upload_info_provider.has_slots_free(),
                        avg_speed=int(searches._upload_info_provider.get_average_upload_speed()),
                        queue_size=searches._upload_info_provider.get_queue_size(),
                        locked_results=convert_items_to_file_data(locked, use_full_path=True),
                    ),
                ),
                name="search-reply",
            )
            task.add_done_callback(partial(searches._search_reply_task_callback, ticket, username, query))
            searches._search_reply_tasks.append(task)
        finally:
            pending["n"] -= 1

    async def query_shares_and_reply(ticket: int, username: str, query: str) -> None:
        # The server reader can already have several searches buffered. Returning
        # without an await would drain that buffer before any peer reply is read.
        if pending["n"] >= _QUERY_QUEUE:
            log.debug("Skipping share query %r from %s; %d already queued", query, username, pending["n"])
        else:
            pending["n"] += 1
            task = asyncio.create_task(answer(ticket, username, query))
            searches._lidarr_query_tasks.add(task)
            task.add_done_callback(searches._lidarr_query_tasks.discard)
        await asyncio.sleep(0)

    searches._query_shares_and_reply = query_shares_and_reply
    log.info("Incoming share queries run off the loop that reads search replies")


def install_share_count_reporting(client, worker) -> None:
    """Log the folder and file totals sent to the server, and correct a zero folder count."""
    shares = getattr(client, "shares", None)
    if shares is None or getattr(shares, "_lidarr_report", False):
        return
    original = shares.report_shares

    async def report() -> None:
        if not shares._session:
            return
        raw_folders, raw_files = shares.get_stats()
        folders, files = counts_for_server(raw_folders, raw_files)
        if (folders, files) != (max(0, int(raw_folders or 0)), max(0, int(raw_files or 0))):
            from aioslsk.protocol.messages import SharedFoldersFiles

            log.info(
                "Share count had %s folders for %s files; reporting 1 folder so searches are not ignored",
                raw_folders,
                raw_files,
            )
            await shares._network.send_server_messages(
                SharedFoldersFiles.Request(shared_folder_count=folders, shared_file_count=files)
            )
        else:
            await original()
        worker.share_counts = (folders, files)
        log.info("Reported share counts to Soulseek: %s folders, %s files", folders, files)
        if files <= 0 and effective_share_dirs(worker.cfg):
            log.info(
                "Share counts stay at 0 until the share scan finishes. Other users may ignore searches until then."
            )
        elif files <= 0:
            log.warning(
                "Soulseek was told this client shares 0 files. Other users often ignore searches from a client that shares nothing."
            )

    shares.report_shares = report
    shares._lidarr_report = True


def cancel_share_queries(client) -> None:
    searches = getattr(client, "searches", None)
    tasks = getattr(searches, "_lidarr_query_tasks", None) if searches is not None else None
    if not tasks:
        return
    for task in list(tasks):
        task.cancel()


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def convert_to_mp3(src: Path, dest_mp3: Path, bitrate: int) -> None:
    exe = ffmpeg_path()
    if not exe:
        raise RuntimeError(
            "ffmpeg not found on PATH. Install it (winget install Gyan.FFmpeg) and restart the worker."
        )
    dest_mp3.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        exe,
        "-y",
        "-i",
        str(src),
        "-codec:a",
        "libmp3lame",
        "-b:a",
        f"{bitrate}k",
        "-map_metadata",
        "0",
        "-id3v2_version",
        "3",
        str(dest_mp3),
    ]
    proc = run_hidden(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed for {src.name}: {proc.stderr[-500:]}")


def prune_empty_dirs(start: Path, stop_at: Path) -> None:
    current = start if start.is_dir() else start.parent
    stop_at = stop_at.resolve()
    while current.exists():
        try:
            resolved = current.resolve()
        except OSError:
            break
        if resolved == stop_at or stop_at not in resolved.parents:
            break
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def transfer_local_path(transfer) -> Path | None:
    local = getattr(transfer, "local_path", None)
    if not local:
        return None
    path = Path(local)
    return path if path.exists() else None


def cleanup_transfer_files(transfers, download_root: Path) -> int:
    removed = 0
    for transfer in transfers:
        src = transfer_local_path(transfer)
        if src is None:
            continue
        try:
            if src.is_file():
                src.unlink()
                removed += 1
                log.info("Removed leftover download %s", src)
                prune_empty_dirs(src.parent, download_root)
        except OSError:
            log.exception("Could not delete leftover %s", src)
    return removed


def sweep_incomplete(download_root: Path, max_age_hours: float, keep: set[Path] | None = None) -> int:
    if max_age_hours <= 0 or not download_root.exists():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    keep = {p.resolve() for p in (keep or set()) if p}
    removed = 0
    for path in download_root.rglob("*"):
        try:
            if not path.is_file():
                continue
            if path.resolve() in keep:
                continue
            if path.stat().st_mtime > cutoff:
                continue
            path.unlink()
            removed += 1
            log.info("Swept old incomplete file %s", path)
            prune_empty_dirs(path.parent, download_root)
        except OSError:
            log.exception("Could not sweep %s", path)
    return removed


def cleanup_sources(paths: list[Path], download_root: Path) -> None:
    for src in paths:
        try:
            if src.is_file():
                src.unlink()
                log.info("Deleted source file %s", src)
        except OSError:
            log.exception("Could not delete %s", src)
            continue
        prune_empty_dirs(src.parent, download_root)


def album_dest_folder(cfg: Config, artist: str, title: str, year: str | None) -> Path:
    stamp = f"{safe_name(artist)} - {safe_name(title)}"
    if year:
        stamp = f"{stamp} ({year})"
    dest = cfg.complete_dir / stamp
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def stage_one_file(cfg: Config, dest: Path, transfer) -> Path | None:
    local = getattr(transfer, "local_path", None)
    if not local:
        return None
    src = Path(local)
    if not src.is_file():
        return None
    ext = src.suffix.lower()
    if ext not in AUDIO_EXT:
        return None
    if cfg.convert_to_mp3 and ext in LOSSLESS_EXT:
        target = dest / (src.stem + ".mp3")
        if not target.exists():
            log.info("Converting %s -> MP3 %sk", src.name, cfg.convert_bitrate)
            convert_to_mp3(src, target, cfg.convert_bitrate)
        return src
    target = dest / src.name
    if not target.exists():
        shutil.copy2(src, target)
    return src


def stage_album_folder(cfg: Config, artist: str, title: str, year: str | None, transfers) -> tuple[Path, list[Path]]:
    dest = album_dest_folder(cfg, artist, title, year)
    removable: list[Path] = []
    for transfer in transfers:
        src = stage_one_file(cfg, dest, transfer)
        if src is not None:
            removable.append(src)
    return dest, removable


class SearchNotSent(RuntimeError):
    """The FileSearch was not accepted by the server in time."""


class Worker:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.lidarr = LidarrClient(
            cfg.lidarr_url,
            cfg.lidarr_api_key,
            track_concurrency=cfg.lidarr_track_concurrency,
        )
        self.store = JobStore(cfg.state_db)
        self.client: SoulSeekClient | None = None
        self.active_transfers: list = []
        self.manual_transfers: list = []
        self.manual_states: dict[str, str] = {}
        self._manual_lock = threading.Lock()
        self._search_lock: asyncio.Lock | None = None
        self.job_info: dict = {"phase": "idle", "artist": "", "title": ""}
        self.step = Step(kind="idle", started=time.monotonic())
        self.cancel_requested = False
        self._ui = None
        self.control = WorkerControl()
        self.catalog = WantedCatalog()
        self.current_key: str | None = None
        self._active_keys: set[str] = set()
        self._key_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_beat = 0.0
        self._heartbeat_task: asyncio.Task | None = None
        self._cycle_attempted_search = False
        self._cycle_unsent_search = False
        self._albums_this_run = 0
        self._quality_profiles: dict[int, dict] | None = None
        # Artists with nothing to search right now, and when to look at them again.
        self._not_ready_until: dict[str, datetime] = {}
        self._wait_artist = ""
        self._last_focus = ""
        self.share_counts: tuple[int, int] = (0, 0)
        self._share_task: asyncio.Task | None = None
        self._housekeeping_task: asyncio.Task | None = None
        self._recorded_uploads: set[tuple[str, str, str]] = set()
        self._auto_paused: set[str] = set()
        self._config_dirty = False
        self._import_lock = threading.Lock()
        self._import_artists: list[str] = []
        self._import_armed = False
        self._import_thread: threading.Thread | None = None
        self._import_notice = ""
        self._import_hold = False
        self._import_epoch = 0
        self._import_notice_epoch = 0
        self._visit_had_download = False
        self._import_phase = ""
        self._importing_artist = ""
        self._free_slot_grace = _FREE_SLOT_GRACE
        self._cap_until: datetime | None = None
        self._pass_note = ""
        self._metadata_profiles: dict[int, dict] | None = None
        self.run_summary = RunSummary()
        self._summary_path: Path | None = None
        self._bridge: BridgeService | None = None
        self._bridge_error = ""
        self._upgrade_cache: list[dict] = []
        self._upgrade_at = 0.0
        self._bridge_mutex: asyncio.Lock | None = None
        self.apply_bridge()

    def close(self) -> None:
        self.stop_bridge()
        if self._ui:
            try:
                self._ui.shutdown()
            except Exception:
                pass
        self.lidarr.close()
        self.store.close()

    def _queue_busy(self) -> bool:
        return bool(self.active_transfers or self.manual_transfers)

    def arm_auto_import(self, artist: str) -> None:
        """Remember an artist whose visit finished. One import runs when the queue is empty."""
        name = (artist or "").strip()
        if not name or not self.cfg.auto_import_after_artist:
            return
        with self._import_lock:
            if name.casefold() not in {item.casefold() for item in self._import_artists}:
                self._import_artists.append(name)
            self._import_armed = True
        self.maybe_auto_import()

    def maybe_auto_import(self) -> None:
        """Start one Lidarr import per time the queue is empty after an artist finishes.

        A later call while the queue stays empty does nothing. The scan runs on
        a side thread so the window stays responsive. The worker can search the
        next artist while Lidarr is still scanning, and waits only for the resync.
        """
        if not self.cfg.auto_import_after_artist:
            return
        if self._queue_busy():
            return
        with self._import_lock:
            if not self._import_armed or not self._import_artists:
                return
            if self._import_thread is not None and self._import_thread.is_alive():
                return
            artists = list(self._import_artists)
            self._import_artists.clear()
            self._import_armed = False
            thread = threading.Thread(
                target=self._run_auto_import,
                args=(artists,),
                name="lidarr-import",
                daemon=True,
            )
            self._import_thread = thread
            thread.start()

    def _publish_import(self, text: str, *, hold: bool) -> None:
        self._import_notice = text
        self._import_hold = hold
        self._import_notice_epoch = self._import_epoch

    def _import_folder(self) -> str:
        folder = (self.cfg.lidarr_scan_path or "").strip()
        if folder:
            return folder
        return str(self.cfg.complete_dir)

    def _import_already_renamed(self, result) -> bool:
        if isinstance(result, dict):
            body = result.get("body") if isinstance(result.get("body"), dict) else {}
            if result.get("renamed") or body.get("renamed"):
                return True
        checker = getattr(self.lidarr, "renames_on_import", None)
        if not callable(checker):
            return False
        try:
            flag = checker()
        except Exception:
            log.exception("Could not tell whether Lidarr already renamed on import")
            return False
        return bool(flag)

    def _finish_lidarr_command(self, result):
        finisher = getattr(self.lidarr, "finish_command", None)
        if not callable(finisher):
            return result if isinstance(result, dict) else {}
        try:
            return finisher(result)
        except Exception:
            log.exception("Could not wait for a Lidarr command")
            return {"status": "failed"}

    def _command_failed(self, result) -> bool:
        if not isinstance(result, dict):
            return False
        return str(result.get("status") or "").casefold() in _COMMAND_FAILED

    def _import_one_artist(self, artist: str, folder: str) -> None:
        self._publish_import(f"Importing {artist} into Lidarr...", hold=True)
        self._import_phase = "scan"
        self._importing_artist = artist
        scanned = False
        try:
            result = self.lidarr.scan_downloaded(folder)
            scanned = True
            waited = self._finish_lidarr_command(result)
            if self._command_failed(waited):
                detail = f"Lidarr scan {waited.get('status') or 'failed'}"
                self._park_failed_import(artist, detail)
                log.error("Importing %s into Lidarr failed: %s", artist, detail)
                self._publish_import(f"Importing {artist} into Lidarr failed: {detail}", hold=False)
                self._summary_review(artist, detail)
                return
            problems = self._import_problems(artist)
            if problems:
                detail = "; ".join(problems)
                self._park_failed_import(artist, detail)
                log.error("Importing %s into Lidarr failed: %s", artist, detail)
                self._publish_import(f"Importing {artist} into Lidarr failed: {detail}", hold=False)
                self._summary_review(artist, detail)
                return
            if not self._import_already_renamed(waited or result):
                artist_id = self.lidarr.artist_id_by_name(artist)
                if not artist_id:
                    raise RuntimeError(f"Lidarr has no artist named {artist}")
                renamed = self.lidarr.rename_artist_files(int(artist_id))
                self._finish_lidarr_command(renamed)
                log.info("Asked Lidarr to rename files for %s", artist)
            else:
                log.info("Lidarr already renames %s on import", artist)
            log.info("Imported %s into Lidarr from %s", artist, folder)
            self._publish_import(f"Imported {artist} into Lidarr", hold=False)
            self._summary_imported(artist)
            self._notify("import", f"Imported {artist}", f"Lidarr imported {artist} from {folder}")
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            log.exception("Importing %s into Lidarr failed", artist)
            self._publish_import(f"Importing {artist} into Lidarr failed: {detail}", hold=False)
            self._summary_failed(artist, detail)
        finally:
            if scanned:
                self._import_phase = "resync"
                self.resync_artist_from_lidarr(artist)
                self._release_unimported(artist)
            self._import_phase = ""
            self._importing_artist = ""

    def _import_problems(self, artist: str) -> list[str]:
        checker = getattr(self.lidarr, "import_failures", None)
        if not callable(checker):
            return []
        try:
            return [str(item) for item in (checker(artist) or []) if str(item).strip()]
        except Exception:
            log.exception("Could not read Lidarr's queue after importing %s", artist)
            return []

    def _park_failed_import(self, artist: str, detail: str) -> None:
        """Flag the album, skip that Soulseek user next time, and move files aside for review."""
        users = {
            (row.get("username") or "").lower()
            for row in self.store.recent_completed_downloads(80)
            if (row.get("artist") or "").casefold() == artist.casefold() and row.get("username")
        }
        if users:
            self.store.reject_usernames(users, artist=artist)
        self.store.mark_import_failed(artist, detail)
        moved = self._move_artist_to_review(artist)
        if moved:
            log.info("Moved %s to the review folder %s", artist, moved)
        self._notify("review", f"Review: {artist}", detail or "Import did not finish")

    def _move_artist_to_review(self, artist: str) -> str:
        token = (artist or "").casefold().strip()
        if not token:
            return ""
        roots = []
        for folder in (self.cfg.complete_dir, self.cfg.lidarr_scan_path):
            path = Path(folder) if folder else None
            if path is not None and path not in roots:
                roots.append(path)
        destination = ""
        for root in roots:
            if not root.is_dir():
                continue
            review = root / "Review"
            for child in list(root.iterdir()):
                if not child.is_dir() or child.name.casefold() == "review":
                    continue
                if token not in child.name.casefold():
                    continue
                review.mkdir(parents=True, exist_ok=True)
                target = review / child.name
                if target.exists():
                    target = review / f"{child.name}-{int(time.time())}"
                shutil.move(str(child), str(target))
                destination = str(review)
        return destination

    def _run_auto_import(self, artists: list[str]) -> None:
        folder = self._import_folder()
        try:
            for artist in artists:
                if not self.cfg.auto_import_after_artist:
                    break
                self._import_one_artist(artist, folder)
        finally:
            with self._import_lock:
                if self._import_thread is threading.current_thread():
                    self._import_thread = None
            self.maybe_auto_import()

    async def _wait_for_import(self) -> None:
        """Wait for the wanted resync. A scan that is still running does not block the next search."""
        while not self.control.is_stopped():
            self.maybe_auto_import()
            with self._import_lock:
                thread = self._import_thread
                phase = self._import_phase
                pending = bool(self._import_armed and self._import_artists)
            if thread is not None and thread.is_alive() and phase == "resync":
                await asyncio.sleep(0.2)
                continue
            if pending and not self._queue_busy() and self.cfg.auto_import_after_artist:
                await asyncio.sleep(0.05)
                continue
            return

    def resync_artist_from_lidarr(self, artist: str) -> None:
        """Refresh one artist's wanted rows and track files from Lidarr.

        Already-imported albums and tracks are marked imported so the next pass
        does not search them. A Lidarr that does not answer leaves the list as it was.
        """
        name = (artist or "").strip()
        if not name:
            return
        loader = getattr(self.lidarr, "artist_library", None)
        if not callable(loader):
            return
        try:
            library = loader(name)
        except Exception:
            log.exception("Could not resync %s from Lidarr", name)
            return
        if not library or library.get("artist_id") is None:
            log.info("Lidarr did not return a library for %s; the wanted list was left as-is", name)
            return
        items, imported_albums, imported_tracks = wanted_from_library(
            library.get("albums") or [],
            library.get("tracks") or [],
        )
        for album_id, album_artist, title in imported_albums:
            self.store.upsert(album_id, album_artist, title, "imported", "already in Lidarr")
        for track_id, album_id, track_artist, title in imported_tracks:
            self.store.upsert_track(track_id, album_id, track_artist, title, "imported", "already in Lidarr")
        rows = items_to_rows(items) if items else []
        self.store.replace_artist_wanted(name, rows)
        if self.catalog.synced:
            self.catalog.merge_artist(name, items)
        log.info(
            "Resynced %s from Lidarr: %s still wanted, %s album(s) already imported",
            name,
            sum(1 for item in items if item.kind != "artist"),
            len(imported_albums),
        )

    def _release_unimported(self, artist: str) -> None:
        """A staged album Lidarr still does not list is failed, not imported."""
        token = (artist or "").casefold().strip()
        if not token:
            return
        with self.store._lock:
            rows = self.store._db.execute(
                "SELECT album_id, artist, title, status FROM jobs WHERE lower(artist) = ?",
                (token,),
            ).fetchall()
        for album_id, name, title, status in rows:
            if status != "staged":
                continue
            self.store.upsert(
                int(album_id),
                name,
                title,
                "import_failed",
                "Lidarr did not show the files after the scan",
            )
            self._summary_failed(f"{name} – {title}", "Lidarr did not show the files after the scan")

    def _summary_dir(self) -> Path:
        return Path(self.cfg.log_file).resolve().parent

    def _write_summary(self) -> None:
        try:
            self._summary_path = self.run_summary.write(self._summary_dir())
        except Exception:
            log.exception("Could not write the run summary")

    def _summary_grabbed(self, label: str) -> None:
        self.run_summary.note_grabbed(label)
        self._write_summary()

    def _summary_imported(self, label: str) -> None:
        self.run_summary.note_imported(label)
        self._write_summary()

    def _summary_review(self, label: str, reason: str) -> None:
        self.run_summary.note_review(label, reason)
        self._write_summary()

    def _summary_failed(self, label: str, reason: str) -> None:
        self.run_summary.note_failed(label, reason)
        self._write_summary()

    def _note_skip(self, artist: str, album: str, reason: str) -> None:
        text = f"{artist} – {album}: {reason}" if album else f"{artist}: {reason}"
        self._pass_note = text
        log.info("Skip %s", text)

    def requeue_block(self, artist: str, title: str, year: str | None = None) -> str:
        """Why this album must not be queued again, or an empty string."""
        if self._album_queued(artist, title):
            return "already queued"
        if self._album_on_disk(artist, title, year):
            return "already in the download folder"
        return ""

    def track_requeue_block(self, artist: str, album_title: str, track_title: str, year: str | None = None) -> str:
        """Why this track must not be queued again, or an empty string."""
        if self._track_queued(track_title, artist, album_title):
            return "already queued"
        if self._track_on_disk(artist, album_title, track_title, year):
            return "already in the download folder"
        return ""

    def _album_stamps(self, artist: str, title: str, year: str | None) -> list[str]:
        base = f"{safe_name(artist)} - {safe_name(title)}"
        stamps = [base]
        if year:
            stamps.append(f"{base} ({year})")
        return stamps

    def _audio_in_dir(self, path: Path) -> list[Path]:
        try:
            if not path.is_dir():
                return []
            return [child for child in path.iterdir() if child.is_file() and child.suffix.lower() in AUDIO_EXT]
        except OSError:
            return []

    def _album_roots(self) -> list[Path]:
        roots = [self.cfg.complete_dir, self.cfg.download_dir]
        scan = (self.cfg.lidarr_scan_path or "").strip()
        if scan:
            roots.append(Path(scan))
        unique: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            key = str(root)
            if key in seen:
                continue
            seen.add(key)
            unique.append(root)
        return unique

    def _album_on_disk(self, artist: str, title: str, year: str | None) -> bool:
        for root in self._album_roots():
            for stamp in self._album_stamps(artist, title, year):
                if self._audio_in_dir(root / stamp):
                    return True
        return False

    def _track_on_disk(self, artist: str, album_title: str, track_title: str, year: str | None) -> bool:
        needle = (track_title or "").casefold().strip()
        if not needle:
            return False
        for root in self._album_roots():
            for stamp in self._album_stamps(artist, album_title, year):
                for path in self._audio_in_dir(root / stamp):
                    if needle in path.stem.casefold():
                        return True
        return False

    def _live_transfers(self) -> list:
        live = []
        for transfer in list(self.active_transfers) + list(self.manual_transfers):
            if state_name(transfer) in {"FAILED", "ABORTED", "COMPLETE"}:
                continue
            live.append(transfer)
        return live

    def _transfer_path(self, transfer) -> str:
        raw = getattr(transfer, "filename", None) or getattr(transfer, "remote_path", None) or ""
        return str(raw).replace("/", "\\").casefold()

    def _album_queued(self, artist: str, title: str) -> bool:
        artist_cf = (artist or "").casefold().strip()
        title_cf = (title or "").casefold().strip()
        if not artist_cf or not title_cf:
            return False
        for transfer in self._live_transfers():
            parts = [part.strip() for part in self._transfer_path(transfer).split("\\") if part.strip()]
            if any(artist_cf in part for part in parts) and any(title_cf in part for part in parts):
                return True
        return False

    def _track_queued(self, track_title: str, artist: str = "", album_title: str = "") -> bool:
        needle = (track_title or "").casefold().strip()
        if not needle:
            return False
        artist_cf = (artist or "").casefold().strip()
        album_cf = (album_title or "").casefold().strip()
        for transfer in self._live_transfers():
            path = self._transfer_path(transfer)
            base = path.split("\\")[-1]
            if needle not in base and needle not in path:
                continue
            if artist_cf and artist_cf not in path and album_cf and album_cf not in path:
                continue
            return True
        return False

    def _path_queued(self, filename: str) -> bool:
        wanted = (filename or "").replace("/", "\\").casefold()
        if not wanted:
            return False
        base = wanted.split("\\")[-1]
        for transfer in self._live_transfers():
            path = self._transfer_path(transfer)
            if path == wanted or (base and path.split("\\")[-1] == base and (len(base) > 8 or "\\" in wanted)):
                return True
        return False

    def _skip_queued_specs(self, specs: list[dict], link: dict | None) -> list[dict]:
        """Drop manual files that are already queued or already in the download folder."""
        kept = []
        link = link or {}
        for spec in specs:
            filename = str(spec.get("filename") or "")
            spec_link = spec.get("link") if isinstance(spec.get("link"), dict) else link
            artist = str((spec_link or {}).get("artist") or link.get("artist") or "")
            album = str((spec_link or {}).get("album") or link.get("album") or "")
            title = str((spec_link or {}).get("title") or "")
            kind = str((spec_link or {}).get("kind") or "").casefold()
            if filename and self._path_queued(filename):
                log.info("Skip %s: already queued", filename)
                continue
            if kind == "song" or (title and kind != "album"):
                reason = self.track_requeue_block(artist, album, title or Path(filename).stem, None) if title or filename else ""
                if reason:
                    log.info("Skip %s - %s - %s: %s", artist, album, title or filename, reason)
                    continue
            elif album:
                reason = self.requeue_block(artist, album, None)
                if reason:
                    log.info("Skip %s - %s: %s", artist, album, reason)
                    continue
            kept.append(spec)
        return kept

    def ask_lidarr_rename(self, album: dict) -> None:
        artist = album.get("artist") or {}
        artist_id = album.get("artistId") or artist.get("id")
        if not artist_id:
            return
        try:
            renamed = self.lidarr.rename_artist_files(int(artist_id))
            self._finish_lidarr_command(renamed)
            log.info("Asked Lidarr to rename files for artist id %s", artist_id)
        except Exception:
            log.exception("Lidarr rename command failed for artist id %s", artist_id)

    def discard_failed_downloads(self, transfers) -> None:
        if not self.cfg.clean_incomplete_on_fail:
            return
        removed = cleanup_transfer_files(transfers, self.cfg.download_dir)
        if removed:
            log.info("Cleaned %s leftover file(s) after a failed download", removed)

    def sweep_incomplete_folder(self) -> None:
        keep: set[Path] = set()
        for transfer in self.active_transfers:
            path = transfer_local_path(transfer)
            if path is not None:
                keep.add(path)
        removed = sweep_incomplete(
            self.cfg.download_dir,
            self.cfg.incomplete_max_age_hours,
            keep,
        )
        if removed:
            log.info("Swept %s old file(s) from Incomplete", removed)

    def _activate(self, key: str) -> None:
        with self._key_lock:
            self._active_keys.add(key)
            self.current_key = key

    def _deactivate(self, key: str) -> None:
        with self._key_lock:
            self._active_keys.discard(key)
            if self.current_key == key:
                self.current_key = next(iter(self._active_keys), None)

    def ensure_status_ui(self) -> None:
        if self._ui:
            return
        try:
            self._ui = start_status_ui(self, self.cfg.status_bind, self.cfg.status_port)
        except OSError as exc:
            if getattr(exc, "errno", None) in {98, 10048}:
                log.info("Status page already running on %s:%s", self.cfg.status_bind, self.cfg.status_port)
            else:
                log.exception("Could not bind status page on %s:%s", self.cfg.status_bind, self.cfg.status_port)

    def set_job(self, phase: str, artist: str = "", title: str = "") -> None:
        self.job_info = {"phase": phase, "artist": artist, "title": title}
        self.step = step_from_legacy(phase, artist, title)

    def set_step(self, step: Step) -> None:
        # The last import result stays until the next artist visit finishes.
        self._import_epoch += 1
        previous = self.step
        same = (
            previous.kind == step.kind
            and previous.artist == step.artist
            and previous.album == step.album
            and previous.title == step.title
            and             previous.query_index == step.query_index
            and previous.username == step.username
            and previous.retry_kind == step.retry_kind
        )
        if same and previous.started:
            step.started = previous.started
        elif not step.started:
            step.started = time.monotonic()
        self.step = step
        title = step.detail if step.kind == "error" else (step.album or step.title)
        self.job_info = {"phase": step.kind, "artist": step.artist, "title": title}

    def activity_view(self) -> tuple[str, int, bool]:
        if self._import_notice:
            return self._import_notice, 0, self._import_hold
        step = self.step
        elapsed = int(max(0.0, time.monotonic() - step.started)) if step.started else 0
        if self.control.is_stopped():
            if step.kind == "error":
                return describe_step(step), 0, False
            return "Stopped", 0, False
        if step.kind == "retry" and step.retry_deadline:
            remaining = max(0, int(round(step.retry_deadline - time.monotonic())))
            step = replace(step, retry_seconds=remaining)
        text = describe_step(step)
        active = step.kind not in {"idle", "stopped", "error"}
        busy = active and not self.control.is_paused()
        show_elapsed = active and step.kind not in {"waiting", "retry"}
        return text, elapsed if show_elapsed else 0, busy

    def manual_state(self, file_id: str) -> str:
        with self._manual_lock:
            return self.manual_states.get(file_id, "")

    def _set_manual_state(self, file_id: str, state: str) -> None:
        with self._manual_lock:
            self.manual_states[file_id] = state

    def _search_slot(self) -> asyncio.Lock:
        if self._search_lock is None:
            self._search_lock = asyncio.Lock()
        return self._search_lock

    async def _send_search(self, query: str, *, cycle: bool = True):
        """One search on the shared Soulseek session.

        The lock only covers sending the request. Waiting for results, and
        downloads, keep going so a manual search does not freeze the worker.
        """
        assert self.client is not None
        log.info("Sending search %r. %s", query, self.search_diagnostics())
        try:
            async with self._search_slot():
                request = await asyncio.wait_for(
                    self.client.searches.search(query),
                    timeout=_SEARCH_SEND_TIMEOUT,
                )
        except TimeoutError:
            detail = self.search_diagnostics()
            log.warning(
                "Search %r was not accepted within %.0fs. %s",
                query,
                _SEARCH_SEND_TIMEOUT,
                detail,
            )
            if cycle:
                self._cycle_unsent_search = True
            raise SearchNotSent(
                f"Soulseek did not accept the search within {int(_SEARCH_SEND_TIMEOUT)}s. {detail}"
            ) from None
        if cycle:
            self._cycle_attempted_search = True
        return request

    async def begin_manual_search(self, query: str):
        if not await self.ensure_soulseek():
            raise RuntimeError("Start the worker to search. Search uses that Soulseek login.")
        return await self._send_search(query, cycle=False)

    def search_outcome(self, replies: int, shown: int, locked: int) -> str:
        """Say why a search listed the files it did, using the live connection."""
        logged_in = self.soulseek_logged_in()
        server_connected = False
        listen_port = 0
        share_files: int | None = None
        share_folders = 0
        parent_name = ""
        scanning = self._share_task is not None and not self._share_task.done()
        client = self.client
        if client is not None:
            try:
                from aioslsk.network.connection import ConnectionState

                server = getattr(getattr(client, "network", None), "server_connection", None)
                server_connected = bool(server and server.state == ConnectionState.CONNECTED)
                ports = client.network.get_listening_ports()
                listen_port = int(ports[0] or 0)
            except Exception:
                log.debug("Could not read Soulseek listen state", exc_info=True)
            try:
                share_folders, share_files = client.shares.get_stats()
                share_folders = int(share_folders)
                share_files = int(share_files)
            except Exception:
                if self.share_counts:
                    share_folders, share_files = self.share_counts
            try:
                parent = getattr(getattr(client, "distributed_network", None), "parent", None)
                parent_name = str(getattr(parent, "username", "") or "")
            except Exception:
                parent_name = ""
        return explain_search(
            replies,
            shown,
            locked,
            logged_in=logged_in,
            server_connected=server_connected,
            listen_port=listen_port,
            share_files=share_files,
            share_folders=share_folders,
            parent_name=parent_name,
            scanning=scanning,
        )

    def note_search_receipt(self, query: str, results) -> tuple[int, int, int, str]:
        """Log raw peer replies against the files the Search tab will list."""
        replies, shown, locked = search_receipt(results)
        describer = getattr(self, "search_outcome", None)
        if callable(describer):
            outcome = describer(replies, shown, locked)
        else:
            outcome = neutral_search_outcome(replies, shown, locked)
        log.info(
            "Search %r: %d peer replies, %d files shown, %d locked files not shown. %s",
            query,
            replies,
            shown,
            locked,
            outcome,
        )
        return replies, shown, locked, outcome

    async def _await_results(
        self,
        request,
        key: str | None,
        seconds: float,
        artist: str,
        album: str,
        query_index: int,
        query_total: int,
        ready=None,
        grace: float = 0.0,
    ) -> str:
        started = time.monotonic()
        ready_at: float | None = None
        while True:
            if ready is not None:
                try:
                    if ready():
                        if grace <= 0:
                            log.info("A free-slot match is already in for %s – %s; scoring now", artist, album)
                            return "ok"
                        if ready_at is None:
                            ready_at = time.monotonic()
                            log.info(
                                "A free-slot match is in for %s – %s; waiting %.0fs for a stronger folder",
                                artist,
                                album,
                                grace,
                            )
                        elif time.monotonic() - ready_at >= grace:
                            return "ok"
                except Exception:
                    log.debug("Could not score results early", exc_info=True)
            waited = int(time.monotonic() - started)
            self.set_step(
                Step(
                    kind="waiting",
                    artist=artist,
                    album=album,
                    query_index=query_index,
                    query_total=query_total,
                    result_count=result_count(request),
                    waited_seconds=waited,
                )
            )
            remaining = float(seconds) - (time.monotonic() - started)
            if remaining <= 0:
                return "ok"
            signal = await self.control.gate(key, sleep=min(1.0, remaining))
            if signal != "ok":
                return signal

    def ui_snapshot(self, recent_limit: int = 10) -> dict:
        limit = normalize_recent_limit(recent_limit)
        transfers = []
        seen: set[int] = set()
        for item in list(self.active_transfers) + list(self.manual_transfers):
            marker = id(item)
            if marker in seen:
                continue
            seen.add(marker)
            transfers.append(
                {
                    "filename": transfer_filename(item),
                    "username": transfer_username(item),
                    "state": state_name(item) or "WAITING",
                    "percent": transfer_percent(item),
                }
            )
        activity, elapsed, busy = self.activity_view()
        responsive, connected_label = self.soulseek_link()
        return {
            "logged_in": responsive,
            "connected_label": connected_label,
            "soulseek_detail": self.search_diagnostics(),
            "job": dict(self.job_info),
            "activity": activity,
            "elapsed": elapsed,
            "elapsed_text": format_elapsed(elapsed) if elapsed else "",
            "busy": busy,
            "transfers": transfers,
            "recent": self.store.recent_completed_downloads(limit),
            "cancel_requested": self.cancel_requested,
            "run_state": self.control.state,
            "current_key": self.current_key or "",
            "run_summary": self.run_summary.line(),
            "run_summary_path": str(self._summary_path or ""),
        }

    def _mark_album(self, album_id: int, artist: str, title: str, signal: str) -> None:
        status = "skipped" if signal == "skip" else "stopped"
        self.store.upsert(album_id, artist, title, status, status)

    def _mark_track(self, hole: dict, signal: str) -> None:
        track = hole["track"]
        album = hole["album"]
        status = "skipped" if signal == "skip" else "stopped"
        self.store.upsert_track(
            int(track["id"]),
            int(album["id"]),
            hole["artist_name"],
            track.get("title") or "Unknown Track",
            status,
            status,
        )

    async def _abandon_transfers(self, transfers) -> None:
        if self.client:
            for transfer in list(transfers):
                try:
                    await abort_transfer(self.client, transfer)
                    log.info("Stopped %s", transfer_filename(transfer))
                except Exception:
                    log.exception("Could not abort %s", transfer_filename(transfer))
        if transfers:
            self.discard_failed_downloads(list(transfers))
        self.active_transfers = []
        self.cancel_requested = False

    def remember_completed(self, transfers, artist: str, title: str, kind: str) -> None:
        seen: set[int] = getattr(self, "_remembered_ids", set())
        fresh: list[str] = []
        for transfer in transfers:
            if state_name(transfer) != "COMPLETE":
                continue
            marker = id(transfer)
            if marker in seen:
                continue
            seen.add(marker)
            self.store.add_completed_download(
                artist,
                title,
                transfer_filename(transfer),
                transfer_username(transfer),
                kind,
            )
            fresh.append(transfer_username(transfer))
        self._remembered_ids = seen
        if fresh:
            self._count_finished(fresh)

    def _count_finished(self, usernames: list[str]) -> None:
        """One finished file counts toward today's cap and a favorite suggestion."""
        folded: dict[str, tuple[str, int]] = {}
        for name in usernames:
            text = (name or "").strip()
            if not text:
                continue
            key = text.lower()
            shown, count = folded.get(key, (text, 0))
            folded[key] = (shown, count + 1)
        favorites = {item.lower() for item in (self.cfg.priority_users or set())}
        for shown, count in folded.values():
            self.store.add_user_day(shown, count)
            self.store.note_good_user(shown, favorites, GOOD_USER_MINIMUM)

    def _past_daily_cap(self, username: str) -> bool:
        cap = max(0, int(getattr(self.cfg, "browse_daily_files", 20) or 0))
        return daily_room(self.store.user_day_files(username), cap) <= 0

    async def cancel_downloads(self, all_items: bool = False, index: int | None = None) -> None:
        self.cancel_requested = True
        targets = list(self.active_transfers)
        if not all_items and index is not None:
            try:
                targets = [self.active_transfers[int(index)]]
            except (IndexError, TypeError, ValueError):
                targets = []
        if not self.client:
            return
        for transfer in targets:
            try:
                await abort_transfer(self.client, transfer)
                log.info("Cancelled %s", transfer_filename(transfer))
            except Exception:
                log.exception("Could not abort %s", transfer_filename(transfer))

    def soulseek_logged_in(self) -> bool:
        return bool(self.client and getattr(self.client, "session", None))

    def loop_lag_seconds(self) -> float | None:
        """Seconds since the worker loop last had a turn. None before it starts."""
        if self._loop is None or not self._loop_beat:
            return None
        return max(0.0, time.monotonic() - self._loop_beat)

    def _server_state(self) -> tuple[str, int]:
        client = self.client
        if client is None:
            return "no client", 0
        try:
            from aioslsk.network.connection import ConnectionState

            server = getattr(getattr(client, "network", None), "server_connection", None)
            if server is None:
                return "no server connection", 0
            name = getattr(getattr(server, "state", None), "name", None) or str(getattr(server, "state", "unknown"))
            if server.state != ConnectionState.CONNECTED:
                port = 0
            else:
                ports = client.network.get_listening_ports()
                port = int(ports[0] or 0)
            return name, port
        except Exception:
            return "unreadable", 0

    def soulseek_link(self) -> tuple[bool, str]:
        """Whether the session can accept a search, and the Status label for that."""
        lag = self.loop_lag_seconds()
        if lag is not None and lag >= 5:
            return False, f"Soulseek not responding (loop {int(lag)}s behind)"
        if not self.soulseek_logged_in():
            return False, "Soulseek offline"
        state, _port = self._server_state()
        if state != "CONNECTED":
            return False, f"Soulseek session is down ({state})"
        return True, "Soulseek connected"

    def search_diagnostics(self) -> str:
        """Connection, loop, and share-scan state. Safe to call off the worker loop."""
        state, port = self._server_state()
        lag = self.loop_lag_seconds()
        lag_text = "not running" if lag is None else f"{lag:.1f}s"
        scanning = self._share_task is not None and not self._share_task.done()
        files = self.share_counts[1] if self.share_counts else 0
        parent = "none"
        client = self.client
        if client is not None:
            try:
                parent_obj = getattr(getattr(client, "distributed_network", None), "parent", None)
                parent = str(getattr(parent_obj, "username", "") or "none")
            except Exception:
                parent = "unreadable"
        lock = "busy" if self._search_lock is not None and self._search_lock.locked() else "free"
        logged = "yes" if self.soulseek_logged_in() else "no"
        return (
            f"login={logged} server={state} listen_port={port} loop_lag={lag_text} "
            f"share_scan={'running' if scanning else 'idle'} share_files={files} "
            f"parent={parent} search_lock={lock}"
        )

    async def _heartbeat(self) -> None:
        while not self.control.is_stopped():
            self._loop_beat = time.monotonic()
            await asyncio.sleep(0.5)
        self._loop_beat = time.monotonic()

    async def start_soulseek(self) -> None:
        self.client = SoulSeekClient(build_slsk_settings(self.cfg))
        # Install before login. The server reader starts at the end of login and
        # can deliver distributed searches before this function continues.
        install_responsive_share_scan(self.client)
        install_nonblocking_share_queries(self.client)
        install_share_count_reporting(self.client, self)
        await self.client.start()
        await self.client.login()
        signal = await self.control.gate(sleep=2)
        if signal == "stop":
            await self.stop_soulseek()
            return
        if not self.soulseek_logged_in():
            raise RuntimeError("Soulseek login returned but session is not active")
        log.info("Logged in to Soulseek as %s", self.cfg.slsk_username)
        self._install_upload_guard()
        self.apply_share_settings(scan=False)
        self._schedule_share_scan()
        self._schedule_housekeeping()

    async def ensure_soulseek(self) -> bool:
        if self.control.is_stopped():
            return False
        if self.soulseek_logged_in():
            return True
        log.warning("Soulseek session dropped; reconnecting")
        try:
            if self.client:
                await self.client.stop()
        except Exception:
            log.exception("Error while stopping dead Soulseek client")
        self.client = None
        signal = await self.control.gate(sleep=5)
        if signal == "stop" or self.control.is_stopped():
            return False
        await self.start_soulseek()
        return self.soulseek_logged_in() and not self.control.is_stopped()

    async def stop_soulseek(self) -> None:
        pending = [task for task in (self._share_task, self._housekeeping_task) if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._share_task = None
        self._housekeeping_task = None
        if self.client:
            cancel_share_queries(self.client)
            await self.client.stop()
            self.client = None

    async def run_forever(self, *, autostart: bool = True) -> None:
        if autostart and self.control.is_stopped():
            self.control.start()
        if self.control.is_stopped():
            return
        self._loop = asyncio.get_running_loop()
        self._loop_beat = time.monotonic()
        self._heartbeat_task = asyncio.create_task(self._heartbeat())
        self.run_summary = RunSummary()
        self._write_summary()
        self.set_step(Step(kind="connecting"))
        try:
            status = self.lidarr.ping()
            log.info("Lidarr OK: %s", status.get("version") or status.get("appName") or "connected")
            self.ensure_status_ui()
            await self.start_soulseek()
            if self.control.is_stopped():
                return
            if not self.soulseek_logged_in():
                raise RuntimeError("Soulseek login returned but session is not active")
            while not self.control.is_stopped():
                self._cycle_attempted_search = False
                self._cycle_unsent_search = False
                try:
                    await self.cycle()
                except LidarrUnreachable:
                    log.warning("Lidarr unreachable. %s", self.search_diagnostics())
                    self.set_step(Step(kind="error", detail="Lidarr unreachable"))
                except Exception:
                    log.exception("Cycle failed. %s", self.search_diagnostics())
                if self.control.is_stopped():
                    self.set_step(Step(kind="stopped"))
                    break
                pause = self._pause_after_cycle()
                signal = await self.control.gate(sleep=pause)
                if signal == "stop":
                    break
        except Exception as exc:
            log.exception("Worker stopped")
            self.set_job("error", "", str(exc)[:240])
            if not self.control.is_stopped():
                self.control.stop()
            raise
        finally:
            self._write_summary()
            if self._heartbeat_task is not None and not self._heartbeat_task.done():
                self._heartbeat_task.cancel()
                await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            self._heartbeat_task = None
            try:
                await self.stop_soulseek()
            except Exception:
                log.exception("Error while stopping Soulseek")

    def _same_artist(self, left: str, right: str) -> bool:
        return (left or "").casefold().strip() == (right or "").casefold().strip()

    def _cooled(self, artist: str, cooldowns: dict, now: datetime) -> bool:
        until = cooldowns.get((artist or "").casefold().strip())
        if not until:
            return False
        try:
            stamp = datetime.fromisoformat(until)
        except ValueError:
            return False
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return now < stamp

    def choose_focus_artist(self, missing: list[dict], holes: list[dict]) -> str | None:
        if not self.cfg.focus_one_artist:
            return None
        current = self.store.focus_artist()
        cooldowns = self.store.artist_cooldowns()
        now = datetime.now(timezone.utc)
        saved = self.store.wanted_catalog_ready() or self.catalog.synced
        if current and not self._cooled(current, cooldowns, now):
            if not saved or self._searchable_now(current):
                return current
            self._defer_artist(current, self._soonest_item_retry(current))
            log.info("Artist %s has nothing to search; picking the next one", current)
        elif current:
            log.info("Artist %s is on cooldown; picking the next one", current)
        if saved:
            chosen = self._next_ready_artist()
            self.store.set_focus_artist(chosen)
            if chosen:
                log.info("Focusing on artist: %s", chosen)
            return chosen
        for album in sorted(missing, key=self._album_sort_key):
            chosen = artist_name(album)
            if self._cooled(chosen, cooldowns, now):
                continue
            self.store.set_focus_artist(chosen)
            log.info("Focusing on artist: %s", chosen)
            return chosen
        for hole in sorted(holes, key=lambda h: (h["artist_name"] or "").casefold()):
            chosen = hole["artist_name"]
            if self._cooled(chosen, cooldowns, now):
                continue
            self.store.set_focus_artist(chosen)
            log.info("Focusing on artist: %s", chosen)
            return chosen
        self.store.set_focus_artist(None)
        return None

    def _finish_artist_visit(self, artist: str) -> None:
        if self._import_notice and not self._import_hold:
            self._import_notice = ""
        days = float(self.cfg.artist_cooldown_days or 0)
        if days <= 0 or not artist:
            return
        self.store.set_artist_cooldown(artist, days)
        self.store.set_focus_artist(None)
        log.info("Finished visit for %s; skipping them for %s days", artist, days)

    def _work_for_artist(
        self, artist: str, missing: list[dict], *, include_tracks: bool = True
    ) -> tuple[list[dict], list[dict]]:
        albums = [album for album in missing if self._same_artist(artist_name(album), artist)]
        holes: list[dict] = []
        if not include_tracks or not self.cfg.fill_missing_tracks:
            return albums, holes
        artist_id = None
        for album in albums:
            artist_id = album.get("artistId") or (album.get("artist") or {}).get("id")
            if artist_id:
                break
        if not artist_id:
            artist_id = self.lidarr.artist_id_by_name(artist)
        if not artist_id:
            return albums, holes
        for album in self.lidarr.albums_for_artist(int(artist_id)):
            if not album.get("monitored", True):
                continue
            stats = album.get("statistics") or {}
            files = count_value(stats.get("trackFileCount") if isinstance(stats, dict) else None)
            total = count_value(
                (stats.get("totalTrackCount") or stats.get("trackCount")) if isinstance(stats, dict) else None
            )
            if files <= 0 or total <= 0 or files >= total:
                continue
            for track in self.lidarr.tracks_for_album(int(album["id"])):
                if not track.get("monitored", True) or track.get("hasFile"):
                    continue
                holes.append(
                    {
                        "track": track,
                        "album": album,
                        "artist_name": artist,
                        "album_title": album.get("title") or "Unknown Album",
                    }
                )
        return albums, holes

    def _empty_albums(self, albums: list[dict]) -> list[dict]:
        truly_missing = []
        for album in albums:
            stats = album.get("statistics") or {}
            files = count_value(stats.get("trackFileCount") if isinstance(stats, dict) else None)
            if files > 0:
                continue
            truly_missing.append(album)
        return truly_missing

    def _artist_token(self, artist: str) -> str:
        return (artist or "").casefold().strip()

    def _defer_artist(self, artist: str, until: datetime | None) -> None:
        """Skip this artist until `until`. A pass that searches nothing must not spin on them."""
        token = self._artist_token(artist)
        if not token:
            return
        now = datetime.now(timezone.utc)
        if until is None or until <= now:
            until = now + timedelta(hours=max(1, int(self.cfg.retry_hours or 12)))
        current = self._not_ready_until.get(token)
        if current is None or until > current:
            self._not_ready_until[token] = until

    def _blocked_tokens(self) -> set[str]:
        now = datetime.now(timezone.utc)
        blocked = set(self._cooled_artist_keys())
        for token, until in self._not_ready_until.items():
            if until > now:
                blocked.add(token)
        importing = self._artist_token(self._importing_artist)
        if importing:
            blocked.add(importing)
        return blocked

    def _searchable_now(self, artist: str) -> bool:
        ready, _until = self._searchable_delay(artist)
        return ready

    def _soonest_item_retry(self, artist: str) -> datetime | None:
        _ready, until = self._searchable_delay(artist)
        return until

    def _searchable_delay(self, artist: str) -> tuple[bool, datetime | None]:
        """Whether this artist has an album or track to search, and the soonest retry otherwise."""
        if self.store.wanted_catalog_ready():
            albums, holes = self.store.artist_work(artist)
        elif self.catalog.synced:
            albums, holes = self.catalog.work_for_artist(artist)
        else:
            return True, None
        soonest: datetime | None = None
        covered: set[int] = set()
        for album in self._empty_albums(albums):
            album_id = album.get("id")
            if album_id is None:
                continue
            covered.add(int(album_id))
            if not self.store.should_skip(int(album_id), self.cfg.retry_hours):
                return True, None
            until = self._album_ready_at(int(album_id))
            if until is not None and (soonest is None or until < soonest):
                soonest = until
        for hole in holes:
            album = hole.get("album") or {}
            album_id = album.get("id") if isinstance(album, dict) else None
            # An empty album is searched as one album. Its songs are not a second search.
            if album_id is not None and int(album_id) in covered:
                continue
            track = hole.get("track") or {}
            track_id = track.get("id")
            if track_id is None:
                continue
            if not self.store.should_skip_track(int(track_id), self.cfg.retry_hours):
                return True, None
            until = self._track_ready_at(int(track_id))
            if until is not None and (soonest is None or until < soonest):
                soonest = until
        return False, soonest

    def _album_ready_at(self, album_id: int) -> datetime | None:
        job = self.store.get(album_id)
        if not job or job.get("status") not in {"failed", "no_match"}:
            return None
        return self._retry_stamp(str(job.get("updated_at") or ""))

    def _track_ready_at(self, track_id: int) -> datetime | None:
        job = self.store.get_track(track_id)
        if not job or job.get("status") not in {"failed", "no_match"}:
            return None
        return self._retry_stamp(str(job.get("updated_at") or ""))

    def _retry_stamp(self, updated_at: str) -> datetime | None:
        try:
            updated = datetime.fromisoformat(updated_at)
        except ValueError:
            return None
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return updated + timedelta(hours=max(0, int(self.cfg.retry_hours or 0)))

    def _soonest_blocked(self) -> tuple[int | None, str]:
        """Seconds until the earliest cooled or deferred artist, and that artist's name."""
        now = datetime.now(timezone.utc)
        best_at: datetime | None = None
        best_token = ""
        for token, stamp in self.store.artist_cooldowns().items():
            try:
                at = datetime.fromisoformat(str(stamp))
            except ValueError:
                continue
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            if at > now and (best_at is None or at < best_at):
                best_at = at
                best_token = token
        for token, at in self._not_ready_until.items():
            if at > now and (best_at is None or at < best_at):
                best_at = at
                best_token = token
        if best_at is None:
            return None, ""
        seconds = max(1, int((best_at - now).total_seconds()))
        return seconds, self._artist_display(best_token)

    def _artist_display(self, token: str) -> str:
        label = self.store.wanted_artist_label(token)
        if label and label != (token or "").casefold().strip():
            return label
        if self.catalog.synced:
            folded = (token or "").casefold().strip()
            for name in self.catalog.artist_labels():
                if name.casefold().strip() == folded:
                    return name
        return label or token

    def _soonest_cooldown_seconds(self) -> int | None:
        seconds, _artist = self._soonest_blocked()
        return seconds

    def _cooled_artist_keys(self) -> set[str]:
        now = datetime.now(timezone.utc)
        cooldowns = self.store.artist_cooldowns()
        # wanted_artists.artist_key is the casefolded name. Legacy "artist:" keys
        # are stripped by artist_cooldowns() so a saved cooldown matches that row.
        return {name for name in cooldowns if self._cooled(name, cooldowns, now)}

    def _has_enabled_wanted(self) -> bool:
        if self.store.wanted_catalog_ready():
            return self.store.next_wanted_artist(set(), tag=self.cfg.wanted_tag) is not None
        if self.catalog.synced:
            choices = self.store.wanted_choices()
            return any(self.catalog.has_enabled_work(name, choices) for name in self.catalog.artist_labels())
        return False

    def _next_saved_name(self, skip: set[str]) -> str | None:
        if self.store.wanted_catalog_ready():
            return self.store.next_wanted_artist(skip, tag=self.cfg.wanted_tag)
        if not self.catalog.synced:
            return None
        choices = self.store.wanted_choices()
        cooldowns = self.store.artist_cooldowns()
        now = datetime.now(timezone.utc)
        ordered = order_wanted_items(
            self.catalog.items(),
            self.cfg.search_order,
            self.cfg.search_order_direction,
        )
        for item in ordered:
            if getattr(item, "kind", "") != "artist":
                continue
            name = item.artist
            token = self._artist_token(name)
            if token in skip or self._cooled(name, cooldowns, now):
                continue
            if self.catalog.has_enabled_work(name, choices):
                return name
        return None

    def _album_sort_key(self, album: dict) -> tuple:
        return album_sort_key(
            album,
            normalize_search_order(self.cfg.search_order),
            normalize_search_direction(self.cfg.search_order_direction),
        )

    def _next_ready_artist(self) -> str | None:
        """The next artist who has an album or track that can be searched now."""
        if not (self.store.wanted_catalog_ready() or self.catalog.synced):
            return None
        skip = self._blocked_tokens()
        if self.store.wanted_catalog_ready():
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=max(0, int(self.cfg.retry_hours or 0)))
            ).isoformat()
            for _ in range(5000):
                name = self.store.next_ready_artist(
                    skip,
                    tag=self.cfg.wanted_tag,
                    retry_before=cutoff,
                    order=self.cfg.search_order,
                    direction=self.cfg.search_order_direction,
                )
                if not name:
                    return None
                ready, until = self._searchable_delay(name)
                if ready:
                    return name
                self._defer_artist(name, until)
                skip.add(self._artist_token(name))
            return None
        for _ in range(10000):
            name = self._next_saved_name(skip)
            if not name:
                return None
            ready, until = self._searchable_delay(name)
            if ready:
                return name
            self._defer_artist(name, until)
            skip.add(self._artist_token(name))
        return None

    def _roll_album_cap(self) -> None:
        if self._cap_until is None:
            return
        if datetime.now().astimezone() >= self._cap_until:
            self._albums_this_run = 0
            self._cap_until = None

    def _album_cap_reached(self) -> bool:
        cap = int(self.cfg.max_albums_per_run or 0)
        if cap <= 0:
            return False
        self._roll_album_cap()
        if self._albums_this_run < cap:
            return False
        if self._cap_until is None:
            now = datetime.now().astimezone()
            self._cap_until = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return True

    def _seconds_until_cap(self) -> int:
        self._album_cap_reached()
        if self._cap_until is None:
            return 1
        return max(1, int((self._cap_until - datetime.now().astimezone()).total_seconds()))

    def _cap_detail(self) -> str:
        cap = int(self.cfg.max_albums_per_run or 0)
        return f"Album cap of {cap} searches reached"

    def _note_day_pause(self) -> None:
        seconds = self._seconds_until_cap()
        self._apply_wait(seconds, "day")
        self._write_summary()

    def _claim_album_search(self) -> bool:
        """Count one real search. False when the daily cap is already full."""
        if self._album_cap_reached():
            return False
        self._albums_this_run += 1
        return True

    def between_pass_wait(self) -> tuple[int, str]:
        """Seconds to sleep, and which setting that wait comes from.

        ``pass`` is Wait before next artist (cycle_seconds). 0 starts the next
        ready artist immediately. ``artist`` is the real wait until the earliest
        artist can be searched again. That wait is not capped, so the Status
        line can count it down to zero and then search that artist.
        """
        cycle = max(0, int(self.cfg.cycle_seconds or 0))
        if self._album_cap_reached():
            self._wait_artist = ""
            return self._seconds_until_cap(), "day"
        catalog = self.store.wanted_catalog_ready() or self.catalog.synced
        if catalog and not self._has_enabled_wanted():
            self._wait_artist = ""
            return (cycle if cycle else IDLE_FLOOR_SECONDS), "idle"
        if self._next_ready_artist():
            self._wait_artist = ""
            return cycle, "pass"
        wait, artist = self._soonest_blocked()
        self._wait_artist = artist
        if wait:
            return max(1, wait), "artist"
        self._wait_artist = ""
        return (cycle if cycle else IDLE_FLOOR_SECONDS), "idle"

    def _apply_wait(self, seconds: int, kind: str) -> None:
        if kind == "day":
            now = time.monotonic()
            self.set_step(
                Step(
                    kind="retry",
                    retry_seconds=max(0, int(seconds)),
                    retry_kind="day",
                    retry_deadline=now + max(0, int(seconds)),
                    detail=self._cap_detail(),
                )
            )
            return
        if kind != "pass" and kind != "artist":
            self._wait_artist = ""
            self.set_step(Step(kind="idle"))
            return
        artist = self._wait_artist if kind == "artist" else ""
        now = time.monotonic()
        deadline = now + max(0, int(seconds))
        previous = self.step
        if (
            previous.kind == "retry"
            and previous.retry_kind == kind
            and (previous.artist or "") == artist
            and previous.retry_deadline
            and abs(previous.retry_deadline - deadline) < 1.5
        ):
            deadline = previous.retry_deadline
        self.set_step(
            Step(
                kind="retry",
                artist=artist,
                retry_seconds=max(0, int(seconds)),
                retry_kind=kind,
                retry_deadline=deadline,
                detail=self._cap_detail() if kind == "day" else (self._pass_note or ""),
            )
        )

    def _note_nothing_ready(self) -> None:
        seconds, kind = self.between_pass_wait()
        self._apply_wait(seconds, kind)

    def _note_between_passes(self) -> tuple[int, str]:
        if self.control.is_stopped():
            self.set_step(Step(kind="stopped"))
            return 0, "idle"
        seconds, kind = self.between_pass_wait()
        self._apply_wait(seconds, kind)
        return seconds, kind

    def _pause_after_cycle(self) -> int:
        """How long to sleep before the next pass.

        The short "wait before next artist" gap runs only after a search was
        sent. A pass that found nothing ready must not restart that gap, or the
        Status line stays on "next artist in 5s" and the next artist is never
        searched. When nobody can be searched, sleep until the earliest artist
        is actually ready.
        """
        searched = self._cycle_attempted_search
        pause, kind = self._note_between_passes()
        if kind == "day":
            return pause
        if searched:
            return pause
        if kind == "pass":
            ready = self._next_ready_artist()
            if ready and not self._same_artist(ready, self._last_focus):
                # The short gap is only after a search. A different artist is ready now.
                pause = 0
                self._apply_wait(0, "pass")
                return pause
            log.warning(
                "No Soulseek search was sent on this pass. Waiting %ss before retrying. %s",
                IDLE_FLOOR_SECONDS,
                self.search_diagnostics(),
            )
            pause = IDLE_FLOOR_SECONDS
            self._apply_wait(pause, "pass")
            return pause
        if pause <= 0:
            log.warning(
                "No Soulseek search was sent on this pass. Waiting %ss before retrying. %s",
                IDLE_FLOOR_SECONDS,
                self.search_diagnostics(),
            )
            pause = IDLE_FLOOR_SECONDS
            self._apply_wait(pause, "pass")
        return pause

    def stop_bridge(self) -> None:
        service = self._bridge
        self._bridge = None
        if service is None:
            return
        try:
            service.stop()
        except Exception:
            log.exception("Could not stop the Lidarr bridge")

    def bridge_port(self) -> int:
        service = self._bridge
        if service is None:
            return 0
        try:
            return int(service.port)
        except Exception:
            return 0

    def apply_bridge(self) -> None:
        """Start or stop the local Torznab and qBittorrent endpoint."""
        self.stop_bridge()
        self._bridge_error = ""
        if not self.cfg.bridge_enabled:
            return
        host = self.cfg.bridge_host or "127.0.0.1"
        port = int(self.cfg.bridge_port or 8788)
        shown = advertise_host(host, getattr(self.cfg, "bridge_advertise_host", ""))
        shown_url = host_for_url(shown)
        settings = bridge_settings(
            save_path=reported_root(self.cfg.complete_dir, getattr(self.cfg, "bridge_remote_path", "")),
            api_key=self.cfg.bridge_api_key,
            username=self.cfg.bridge_username,
            password=self.cfg.bridge_password,
            category=self.cfg.bridge_category,
            dry_run=self.cfg.dry_run,
            public_base=f"http://{shown_url}:{port}",
            local_only=is_loopback_bind(host),
        )
        book = GrabBook(Path(self.cfg.state_db).with_name(Path(self.cfg.state_db).stem + "-bridge.sqlite3"))
        try:
            service = BridgeService(host, port, book, settings, self.search_for_bridge, self.on_bridge_add)
        except OSError as exc:
            book.close()
            self._bridge_error = f"Could not listen on {host}:{port}: {exc}"
            log.error(self._bridge_error)
            return
        settings["public_base"] = f"http://{shown_url}:{service.port}"
        service.start()
        self._bridge = service
        log.info("Lidarr bridge listening on http://%s:%s", shown, service.port)

    def retarget_lidarr(self) -> None:
        """Open a new Lidarr client after the URL or API key changes."""
        try:
            self.lidarr.close()
        except Exception:
            log.exception("Could not close the Lidarr client")
        self.lidarr = LidarrClient(
            self.cfg.lidarr_url,
            self.cfg.lidarr_api_key,
            track_concurrency=self.cfg.lidarr_track_concurrency,
        )
        self._quality_profiles = None
        self._metadata_profiles = None
        self._upgrade_cache = []
        self._upgrade_at = 0.0

    def _notify(self, kind: str, title: str, body: str) -> None:
        try:
            deliver(self.cfg, kind, title, body)
        except Exception:
            log.exception("Notification failed")

    def search_for_bridge(self, artist: str, album: str, query: str) -> list[dict]:
        loop = self._loop
        if loop is None or not loop.is_running() or not self.soulseek_logged_in():
            raise RuntimeError("Soulseek is not logged in")
        future = asyncio.run_coroutine_threadsafe(self._bridge_search(artist, album, query), loop)
        timeout = min(28.0, max(8.0, float(self.cfg.search_wait_seconds or 12) + 6))
        return future.result(timeout=timeout)

    async def _bridge_search(self, artist: str, album: str, query: str) -> list[dict]:
        if self.client is None or not self.soulseek_logged_in():
            raise RuntimeError("Soulseek is not logged in")
        text = " ".join(part for part in (artist, album) if part).strip() or (query or "").strip()
        if not text:
            return []
        request = await self._send_search(text, cycle=False)
        wait = min(float(self.cfg.search_wait_seconds or 12), 15.0)
        signal = await self._await_results(request, "bridge", wait, artist or "Search", album or text, 1, 1)
        results = list(getattr(request, "results", None) or [])
        try:
            self.client.searches.remove_request(request)
        except Exception:
            pass
        if signal != "ok":
            raise RuntimeError("Soulseek search did not finish")
        releases = []
        label_artist = artist or "Unknown Artist"
        label_album = album or text
        for folder in group_results(results, self.cfg.ignored_users):
            scored = score_folder(
                folder,
                label_artist,
                label_album,
                [],
                self.cfg.preferred_extensions,
                self.cfg.min_filename_ratio,
                self.cfg.track_count_tolerance,
                self.cfg.min_mp3_bitrate,
            )
            if scored.score < 20:
                continue
            files = []
            size = 0
            for item in scored.files:
                filename = getattr(item, "filename", "") or ""
                length = int(getattr(item, "filesize", 0) or getattr(item, "size", 0) or 0)
                size += max(0, length)
                files.append({"filename": filename, "size": max(0, length)})
            releases.append(
                {
                    "artist": label_artist,
                    "album": label_album,
                    "username": scored.username,
                    "size": size or 1,
                    "files": files,
                    "score": int(scored.score),
                }
            )
        releases.sort(key=lambda item: item["score"], reverse=True)
        return releases[:8]

    def on_bridge_add(self, row: dict) -> None:
        name = row.get("name") or row.get("hash") or "album"
        if row.get("state") == "pausedDL" or self.cfg.dry_run:
            self.store.note_dry_run(f"dry run: would grab {name}")
            return
        loop = self._loop
        if loop is None or not loop.is_running():
            book = self._bridge.book if self._bridge is not None else None
            if book is not None:
                book.update(str(row.get("hash") or ""), state="error", message="Start the worker before Lidarr grabs")
            return
        asyncio.run_coroutine_threadsafe(self.download_bridge_grab(row), loop)

    async def download_bridge_grab(self, row: dict) -> None:
        digest = str(row.get("hash") or "").lower()
        book = self._bridge.book if self._bridge is not None else None
        if self.cfg.dry_run or row.get("state") == "pausedDL":
            self.store.note_dry_run(f"dry run: would grab {row.get('name') or digest}")
            return
        if self._bridge_mutex is None:
            self._bridge_mutex = asyncio.Lock()
        async with self._bridge_mutex:
            if book is not None:
                book.update(digest, state="downloading", progress=0)
            if self.client is None or not self.soulseek_logged_in():
                if book is not None:
                    book.update(digest, state="error", message="Soulseek is not logged in")
                return
            files = [item for item in (row.get("files") or []) if str(item.get("filename") or "")]
            username = str(row.get("username") or "")
            artist = str(row.get("artist") or "Unknown Artist")
            album = str(row.get("album") or "Unknown Album")
            dest = album_dest_folder(self.cfg, artist, album, None)
            done = 0
            try:
                for item in files:
                    transfer = await self.client.transfers.download(username, str(item.get("filename") or ""))
                    self.active_transfers = [transfer]
                    deadline = asyncio.get_running_loop().time() + min(int(self.cfg.download_timeout_minutes or 30), 30) * 60
                    while asyncio.get_running_loop().time() < deadline:
                        if state_name(transfer) in {"COMPLETE", "FAILED", "ABORTED"}:
                            break
                        await asyncio.sleep(0.5)
                    if state_name(transfer) != "COMPLETE":
                        raise RuntimeError(state_name(transfer) or "download failed")
                    await asyncio.to_thread(stage_one_file, self.cfg, dest, transfer)
                    done += 1
                    if book is not None and files:
                        book.update(digest, state="downloading", progress=done / len(files))
                if done == 0:
                    raise RuntimeError("no files downloaded")
                if book is not None:
                    remote = getattr(self.cfg, "bridge_remote_path", "")
                    book.update(
                        digest,
                        state="pausedUP",
                        progress=1,
                        eta=0,
                        content_path=mapped_folder(dest, self.cfg.complete_dir, remote),
                        save_path=reported_root(self.cfg.complete_dir, remote),
                    )
                self.store.add_completed_download(artist, album, dest.name, username, "bridge")
            except Exception as exc:
                log.exception("Bridge download failed for %s", digest)
                if book is not None:
                    book.update(digest, state="error", message=(str(exc).strip() or "download failed")[:240])
            finally:
                self.active_transfers = []

    def _upgrade_candidates(self) -> list[dict]:
        if not self.cfg.quality_upgrades:
            return []
        hours = max(1, int(self.cfg.quality_upgrade_hours or 24))
        now = time.time()
        if self._upgrade_at and now - self._upgrade_at < hours * 3600:
            return list(self._upgrade_cache)
        try:
            rows = self.lidarr.wanted_cutoff() or []
        except Exception:
            log.exception("Could not read albums below the quality cutoff")
            return list(self._upgrade_cache)
        self._upgrade_cache = [row for row in rows if isinstance(row, dict)]
        self._upgrade_at = now
        return list(self._upgrade_cache)

    def _upgrade_album(self, album: dict) -> bool:
        if not self.cfg.quality_upgrades:
            return False
        album_id = album.get("id")
        if album_id is None:
            return False
        return any(item.get("id") == album_id for item in self._upgrade_candidates())

    def _or_upgrade_focus(self, focus: str | None) -> str | None:
        if focus or not self.cfg.quality_upgrades:
            return focus
        artist = upgrade_artist(self._upgrade_candidates())
        if not artist:
            return None
        self.store.set_focus_artist(artist)
        log.info("Quality upgrade: %s is below the cutoff", artist)
        return artist

    def _with_upgrades(self, focus: str, missing: list[dict]) -> list[dict]:
        if not self.cfg.quality_upgrades or not focus:
            return missing
        return merge_upgrade_albums(focus, missing, self._upgrade_candidates())

    async def cycle(self) -> None:
        if self.control.is_stopped():
            return
        self._cycle_attempted_search = False
        self._cycle_unsent_search = False
        self._pass_note = ""
        try:
            await self._cycle_once()
        except LidarrUnreachable:
            log.warning("Lidarr unreachable. %s", self.search_diagnostics())
            self.set_step(Step(kind="error", detail="Lidarr unreachable"))

    async def _cycle_once(self) -> None:
        if self.control.is_stopped():
            return
        await self._wait_for_import()
        if self.control.is_stopped():
            return
        self.set_step(Step(kind="preparing"))
        # A Sync stores the wanted list in SQLite. Until that happens, keep
        # reading Lidarr 20 pages at a time and resume next cycle.
        if self.store.wanted_catalog_ready():
            log.info("Using saved wanted list (%s albums)", self.store.wanted_kind_count("album"))
            focus = self._or_upgrade_focus(self.choose_focus_artist([], []))
            if not focus:
                log.info("No artist ready (all cooled off or nothing missing)")
                self._last_focus = ""
                self._note_nothing_ready()
                self.sweep_incomplete_folder()
                return
            self._last_focus = focus
            artist_albums, artist_holes = self.store.artist_work(focus)
            truly_missing = self._empty_albums(artist_albums)
            checked_missing = truly_missing
            covered = {int(album["id"]) for album in checked_missing if album.get("id") is not None}
            holes = []
            for hole in artist_holes:
                album = hole.get("album") or {}
                album_id = album.get("id") if isinstance(album, dict) else None
                if album_id is not None and int(album_id) in covered:
                    continue
                holes.append(hole)
            choices = {}
        elif self.catalog.synced:
            cached = self.catalog.album_payloads() or []
            log.info("Using synced wanted list (%s albums)", len(cached))
            focus = self._or_upgrade_focus(self.choose_focus_artist([], []))
            if not focus:
                log.info("No artist ready (all cooled off or nothing missing)")
                self._last_focus = ""
                self._note_nothing_ready()
                self.sweep_incomplete_folder()
                return
            self._last_focus = focus
            choices = self.store.wanted_choices()
            artist_albums, artist_holes = self.catalog.work_for_artist(focus)
            truly_missing = self._empty_albums(artist_albums)
            checked_missing = albums_for_search(truly_missing, choices)
            covered = {int(album["id"]) for album in checked_missing if album.get("id") is not None}
            holes = []
            for hole in artist_holes:
                album = hole.get("album") or {}
                album_id = album.get("id") if isinstance(album, dict) else None
                if album_id is not None and int(album_id) in covered:
                    continue
                holes.append(hole)
            holes = holes_for_search(holes, choices)
        else:
            start_page = int(self.store.get_meta("missing_page") or "1")
            catalog, next_page, exhausted = self.lidarr.wanted_missing(
                start_page=start_page,
                max_pages=self.cfg.max_missing_pages,
            )
            self.store.set_meta("missing_page", str(next_page))
            log.info(
                "Wanted missing pages %s-%s (%s albums, %s)",
                start_page,
                start_page + self.cfg.max_missing_pages - 1,
                len(catalog),
                "end of list" if exhausted else f"next page {next_page}",
            )
            catalog.sort(key=self._album_sort_key)
            focus = self._or_upgrade_focus(self.choose_focus_artist(catalog, []))
            if not focus:
                log.info("No artist ready (all cooled off or nothing missing)")
                self._last_focus = ""
                self._note_nothing_ready()
                self.sweep_incomplete_folder()
                return
            self._last_focus = focus
            missing, api_holes = self._work_for_artist(focus, catalog, include_tracks=True)
            truly_missing = self._empty_albums(missing)
            choices = self.store.wanted_choices()
            checked_missing = albums_for_search(truly_missing, choices)
            holes = holes_for_search(api_holes, choices)
        hidden = len(truly_missing) - len(checked_missing)
        if hidden:
            log.info("Left out %s unchecked album(s)", hidden)
        missing = self._with_upgrades(focus, checked_missing)
        if not missing and not holes:
            log.info("Artist %s has nothing to search; moving on without a cooldown", focus)
            self._defer_artist(focus, None)
            self.store.set_focus_artist(None)
            self.sweep_incomplete_folder()
            return
        log.info("Artist %s: %s empty albums, %s partial tracks", focus, len(missing), len(holes))
        tag = (self.cfg.wanted_tag or "").strip()
        cap = int(self.cfg.max_albums_per_run or 0)
        batch = []
        for album in sorted(missing, key=self._album_sort_key):
            if len(batch) >= self.cfg.max_albums_per_cycle:
                break
            if cap and self._albums_this_run + len(batch) >= cap:
                break
            if tag and not album_has_tag(album, tag):
                self._note_skip(artist_name(album), album.get("title") or "", f"tag {tag} does not match")
                continue
            skipped = release_skip_reason(album, self.cfg, self._metadata_profile_map())
            if skipped:
                self._note_skip(artist_name(album), album.get("title") or "", skipped)
                continue
            if self.store.should_skip(int(album["id"]), self.cfg.retry_hours) and not self._upgrade_album(album):
                continue
            batch.append(album)
        if self._album_cap_reached() and not batch:
            log.info("Album cap of %s searches reached; pausing until the next day", cap)
            self._note_day_pause()
            self.sweep_incomplete_folder()
            return
        workers = max(1, int(self.cfg.concurrent_albums or 1))
        sem = asyncio.Semaphore(workers)
        log.info("Album batch %s (up to %s at once)", len(batch), workers)

        async def run_one(album: dict) -> None:
            album_id = int(album["id"])
            artist = artist_name(album)
            title = album.get("title") or "Unknown Album"
            if not is_enabled(album_key(album_id), choices):
                log.info("Skip unchecked album %s - %s", artist, title)
                return
            async with sem:
                if self.control.is_stopped():
                    return
                self.store.upsert(album_id, artist, title, "searching")
                self.set_step(Step(kind="searching", artist=artist, album=title))
                ok = await self.process_album(album)
                if not ok:
                    log.info("No usable result yet for %s - %s", artist, title)

        if batch:
            outcomes = await asyncio.gather(*(run_one(album) for album in batch), return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, LidarrUnreachable):
                    raise outcome
        if self.control.is_stopped():
            self.set_step(Step(kind="stopped"))
            return
        if self.cfg.fill_missing_tracks and holes:
            await self.cycle_tracks(holes)
        if self.control.is_stopped():
            self.set_step(Step(kind="stopped"))
            return
        self._close_focus(focus)
        self.sweep_incomplete_folder()
        self._note_between_passes()
        if self._visit_had_download:
            self.arm_auto_import(focus)
        self._visit_had_download = False
        await self._wait_for_import()

    def _close_focus(self, focus: str) -> None:
        if self._cycle_unsent_search:
            log.info(
                "A search for %s was not sent because Soulseek was disconnected; no cooldown",
                focus,
            )
            return
        if self._cycle_attempted_search:
            self._finish_artist_visit(focus)
            return
        still_ready, until = self._searchable_delay(focus)
        if still_ready:
            log.info("Artist %s was not searched; trying again without a cooldown", focus)
            return
        log.info("Artist %s had nothing new to search; not starting the retry cooldown", focus)
        self._defer_artist(focus, until)
        self.store.set_focus_artist(None)

    async def cycle_tracks(self, holes: list[dict] | None = None) -> None:
        if holes is None:
            try:
                holes = self.lidarr.missing_tracks_on_partial_albums()
            except Exception:
                log.exception("Could not list missing tracks")
                return
        holes = sorted(list(holes), key=lambda hole: album_sort_key((hole or {}).get("album") if isinstance(hole, dict) else {}, self.cfg.search_order, self.cfg.search_order_direction))
        log.info("Lidarr missing tracks on partial albums: %s", len(holes))
        use_rows = self.store.wanted_catalog_ready() or len(holes) <= 2000
        choices = {} if use_rows else self.store.wanted_choices()
        processed = 0
        for hole in holes:
            if self.control.is_stopped():
                break
            if processed >= self.cfg.max_tracks_per_cycle:
                break
            try:
                if not isinstance(hole, dict):
                    log.warning("Skipping a missing track that is not an object")
                    continue
                track = hole.get("track") or {}
                album = hole.get("album") or {}
                if not isinstance(track, dict) or not isinstance(album, dict):
                    log.warning("Skipping a missing track with no track or album")
                    continue
                track_id = optional_id(track.get("id"))
                album_id = optional_id(album.get("id"))
                title = track.get("title") or "Unknown Track"
                if track_id is None or album_id is None:
                    log.warning(
                        "Skipping %s: no usable id (track number %s)",
                        title,
                        track.get("trackNumber") or "",
                    )
                    continue
                artist = hole.get("artist_name") or "Unknown Artist"
                album_title = hole.get("album_title") or album.get("title") or "Unknown Album"
                if use_rows:
                    if not self.store.wanted_enabled(song_key(track_id)):
                        continue
                elif not is_enabled(song_key(track_id), choices):
                    continue
                if self.store.should_skip_track(track_id, self.cfg.retry_hours):
                    continue
                self.store.upsert_track(track_id, album_id, artist, title, "searching")
                self.set_step(Step(kind="searching", artist=artist, title=title, album=album_title))
                ok = await self.process_track(hole)
                processed += 1
                if not ok:
                    log.info("No usable track yet for %s - %s - %s", artist, album_title, title)
            except Exception:
                log.exception("Skipping a missing track")

    async def process_track(self, hole: dict) -> bool:
        track = hole["track"]
        key = song_key(int(track["id"]))
        self._activate(key)
        try:
            return await self._process_track(hole, key)
        finally:
            self._deactivate(key)

    async def _process_track(self, hole: dict, key: str) -> bool:
        if not await self.ensure_soulseek():
            if self.control.is_stopped():
                self._mark_track(hole, "stop")
            return False
        assert self.client is not None
        track = hole["track"]
        album = hole["album"]
        track_id = int(track["id"])
        album_id = int(album["id"])
        artist = hole["artist_name"]
        album_title = hole["album_title"]
        title = track.get("title") or "Unknown Track"
        year = album_year(album)
        blocked = self.track_requeue_block(artist, album_title, title, year)
        if blocked:
            self._note_skip(artist, title, blocked)
            status = "downloading" if blocked == "already queued" else "staged"
            self.store.upsert_track(track_id, album_id, artist, title, status, blocked)
            return True
        queries = build_track_queries(artist, album_title, title, aliases_from_album(album))
        log.info("Track search: %s", " | ".join(queries))

        best_item = None
        best_user = ""
        best_score = 0.0
        for index, query in enumerate(queries, start=1):
            signal = await self.control.gate(key)
            if signal != "ok":
                self._mark_track(hole, signal)
                return False
            self.set_step(
                Step(
                    kind="searching",
                    artist=artist,
                    album=album_title,
                    title=title,
                    query_index=index,
                    query_total=len(queries),
                )
            )
            try:
                request = await self._send_search(query)
            except SearchNotSent:
                self.store.upsert_track(track_id, album_id, artist, title, "not_sent", "search was not sent")
                return False
            signal = await self._await_results(
                request, key, self.cfg.search_wait_seconds, artist, album_title, index, len(queries)
            )
            self.note_search_receipt(query, getattr(request, "results", None))
            if signal != "ok":
                try:
                    self.client.searches.remove_request(request)
                except Exception:
                    pass
                self._mark_track(hole, signal)
                return False
            self.set_step(
                Step(
                    kind="matching",
                    artist=artist,
                    album=album_title,
                    title=title,
                    query_index=index,
                    query_total=len(queries),
                    result_count=result_count(request),
                )
            )
            for result in request.results:
                username = getattr(result, "username", "") or ""
                if username.lower() in self.cfg.ignored_users:
                    continue
                for item in list(getattr(result, "shared_items", None) or []):
                    score, reason = score_track_item(
                        item,
                        username,
                        artist,
                        album_title,
                        title,
                        self.cfg.preferred_extensions,
                        self.cfg.min_filename_ratio,
                        self.cfg.min_mp3_bitrate,
                        expected_duration=seconds_from_lidarr(track.get("duration")),
                        gate=self._quality_gate(album),
                    )
                    if reason != "ok":
                        continue
                    if score > best_score:
                        best_score = score
                        best_item = item
                        best_user = username
            try:
                self.client.searches.remove_request(request)
            except Exception:
                pass
            if best_item and best_score >= 18:
                break

        if best_item is None:
            self.store.upsert_track(track_id, album_id, artist, title, "no_match", "no file passed filters")
            return False

        log.info(
            "Picked track %s from %s :: %s (score %.1f)",
            title,
            best_user,
            getattr(best_item, "filename", ""),
            best_score,
        )
        self.store.upsert_track(track_id, album_id, artist, title, "downloading", best_user)
        if self.cfg.dry_run:
            detail = f"dry run: would grab 1 file(s) from {best_user}"
            self.store.upsert_track(track_id, album_id, artist, title, "skipped", detail)
            self.store.note_dry_run(f"{artist} – {title}: {detail}")
            log.info(detail)
            return False
        self.cancel_requested = False
        self.set_step(Step(kind="queued", artist=artist, album=album_title, title=title, file_total=1, username=best_user))
        signal = await self.control.gate(key)
        if signal != "ok":
            self._mark_track(hole, signal)
            return False
        transfer = await self.client.transfers.download(best_user, best_item.filename)
        self.active_transfers = [transfer]
        self._visit_had_download = True
        deadline = [asyncio.get_running_loop().time() + min(self.cfg.download_timeout_minutes, 30) * 60]
        while asyncio.get_running_loop().time() < deadline[0]:
            name = state_name(transfer)
            if name in {"COMPLETE", "FAILED", "ABORTED"}:
                break
            if self.cancel_requested:
                await abort_transfer(self.client, transfer)
                break
            self.set_step(
                Step(
                    kind="downloading",
                    artist=artist,
                    album=album_title,
                    title=title,
                    file_index=1,
                    file_total=1,
                    username=best_user,
                )
            )
            log.info("Track download %s: %s", title, name or "waiting")
            self.remember_completed([transfer], artist, title, "track")
            signal = await self.control.gate(key, sleep=2, deadline=deadline)
            if signal != "ok":
                await self._abandon_transfers([transfer])
                self._mark_track(hole, signal)
                return False

        self.remember_completed([transfer], artist, title, "track")
        self.active_transfers = []
        self.maybe_auto_import()
        if self.cancel_requested or state_name(transfer) != "COMPLETE":
            detail = "cancelled" if self.cancel_requested else (state_name(transfer) or "timeout")
            self.store.upsert_track(track_id, album_id, artist, title, "failed", detail)
            self.discard_failed_downloads([transfer])
            self.cancel_requested = False
            return False
        self.cancel_requested = False

        folder, sources = stage_album_folder(self.cfg, artist, album_title, year, [transfer])
        audio_count = sum(1 for p in folder.iterdir() if p.suffix.lower() in AUDIO_EXT)
        if audio_count == 0:
            self.store.upsert_track(track_id, album_id, artist, title, "failed", "no file staged")
            return False

        ready, reason = self._prepare_staged_folder(folder, album, [track])
        if not ready:
            self.store.block_source(best_user, "", reason)
            self.store.upsert_track(track_id, album_id, artist, title, "failed", reason)
            self._summary_failed(f"{artist} – {title}", reason)
            return False
        if self.cfg.delete_sources_after_import:
            cleanup_sources(sources, self.cfg.download_dir)
        self._summary_grabbed(f"{artist} – {title}")
        if not self.cfg.auto_import_after_artist:
            scanned, reason = self._scan_staged_folder(folder)
            if not scanned:
                self.store.upsert_track(track_id, album_id, artist, title, "failed", reason)
                self._summary_failed(f"{artist} – {title}", reason)
                return False
            self.ask_lidarr_rename(album)
        else:
            self._visit_had_download = True
        self.store.upsert_track(track_id, album_id, artist, title, "staged", str(folder))
        log.info("Staged missing track %s into %s", title, folder)
        return True

    def _load_quality_profiles(self) -> dict[int, dict]:
        if self._quality_profiles is not None:
            return self._quality_profiles
        rows = self.lidarr.quality_profiles()
        cached: dict[int, dict] = {}
        for row in rows or []:
            if isinstance(row, dict) and row.get("id") is not None:
                cached[int(row["id"])] = row
        self._quality_profiles = cached
        return cached

    def _quality_gate(self, album: dict) -> QualityGate:
        profile = None
        profile_id = album.get("qualityProfileId")
        if profile_id is not None:
            try:
                profile = self._load_quality_profiles().get(int(profile_id))
            except LidarrUnreachable:
                raise
            except Exception:
                log.exception("Could not read Lidarr quality profiles")
        return gate_from_profile(profile, self.cfg.preferred_extensions, self.cfg.min_mp3_bitrate)

    def _best_folder(self, results, artist, title, titles, durations, gate, rejected: set[str]):
        best = None
        for folder in group_results(results, self.cfg.ignored_users):
            if (folder.username or "").lower() in rejected:
                continue
            if self.store.source_blocked(folder.username, folder.remote_dir):
                continue
            scored = score_folder(
                folder,
                artist,
                title,
                titles,
                self.cfg.preferred_extensions,
                self.cfg.min_filename_ratio,
                self.cfg.track_count_tolerance,
                self.cfg.min_mp3_bitrate,
                durations=durations,
                gate=gate,
            )
            if scored.reason != "ok":
                continue
            if best is None or scored.score > best.score:
                best = scored
        return best

    def _free_slot_ready(self, request, artist, title, titles, durations, gate, rejected) -> bool:
        best = self._best_folder(getattr(request, "results", None), artist, title, titles, durations, gate, rejected)
        return bool(best and best.free_slot and best.score >= 20)

    async def process_album(self, album: dict) -> bool:
        album_id = int(album["id"])
        artist = artist_name(album)
        title = album.get("title") or "Unknown Album"
        key = album_key(album_id)
        self._activate(key)
        try:
            return await self._process_album(album, key, album_id, artist, title)
        finally:
            self._deactivate(key)

    async def _process_album(self, album: dict, key: str, album_id: int, artist: str, title: str) -> bool:
        if not await self.ensure_soulseek():
            if self.control.is_stopped():
                self._mark_album(album_id, artist, title, "stop")
            return False
        assert self.client is not None
        year = album_year(album)
        blocked = self.requeue_block(artist, title, year)
        if blocked:
            self._note_skip(artist, title, blocked)
            status = "downloading" if blocked == "already queued" else "staged"
            self.store.upsert(album_id, artist, title, status, blocked)
            if blocked != "already queued":
                self._visit_had_download = True
            return True
        tracks = self.store.saved_tracks(album_id)
        if not tracks:
            tracks = self.lidarr.tracks_for_album(album_id)
        needed_tracks = monitored_missing_tracks(tracks)
        if not needed_tracks:
            log.info("Skip %s - %s: Lidarr already has every monitored track", artist, title)
            self.store.upsert(album_id, artist, title, "imported", "already complete")
            return True
        if any(t.get("hasFile") for t in tracks if t.get("monitored", True)):
            log.info(
                "Skip full-album grab for %s - %s (%s missing tracks); track fill will handle it",
                artist,
                title,
                len(needed_tracks),
            )
            self.store.upsert(album_id, artist, title, "partial", "use track fill")
            self._note_skip(artist, title, "partial album, filling tracks")
            return False
        if self._album_cap_reached():
            self._note_day_pause()
            self.store.upsert(album_id, artist, title, "not_sent", "album cap")
            return False
        titles = track_titles(needed_tracks)
        durations = [seconds_from_lidarr(track.get("duration")) for track in needed_tracks]
        gate = self._quality_gate(album)
        rejected = self.store.rejected_usernames(album_id, artist)
        queries = build_queries(artist, title, year, aliases_from_album(album))
        log.info("Searching: %s", " | ".join(queries))

        best = None
        ranked = []
        counted = False
        for index, query in enumerate(queries, start=1):
            signal = await self.control.gate(key)
            if signal != "ok":
                self._mark_album(album_id, artist, title, signal)
                return False
            self.set_step(
                Step(kind="searching", artist=artist, album=title, query_index=index, query_total=len(queries))
            )
            try:
                request = await self._send_search(query)
            except SearchNotSent:
                self.store.upsert(album_id, artist, title, "not_sent", "search was not sent")
                return False
            if not counted:
                self._claim_album_search()
                counted = True
            signal = await self._await_results(
                request,
                key,
                self.cfg.search_wait_seconds,
                artist,
                title,
                index,
                len(queries),
                ready=lambda: self._free_slot_ready(request, artist, title, titles, durations, gate, rejected),
                grace=self._free_slot_grace,
            )
            self.note_search_receipt(query, getattr(request, "results", None))
            if signal != "ok":
                try:
                    self.client.searches.remove_request(request)
                except Exception:
                    pass
                self._mark_album(album_id, artist, title, signal)
                return False
            self.set_step(
                Step(
                    kind="matching",
                    artist=artist,
                    album=title,
                    query_index=index,
                    query_total=len(queries),
                    result_count=result_count(request),
                )
            )
            picked = self._best_folder(request.results, artist, title, titles, durations, gate, rejected)
            ranked.extend(self._scored_folders(request.results, artist, title, titles, durations, gate, rejected))
            if picked is not None and (best is None or picked.score > best.score):
                best = picked
            try:
                self.client.searches.remove_request(request)
            except Exception:
                pass
            if best and best.score >= 20:
                break

        candidates = ranked or ([best] if best is not None else [])
        return await self._download_candidates(
            candidates, album, key, album_id, artist, title, year, needed_tracks, rejected
        )

    async def _download_candidates(self, candidates, album, key, album_id, artist, title, year, needed_tracks, rejected) -> bool:
        """Download the best folder, then the next user when that download fails."""
        if not candidates:
            self.store.upsert(album_id, artist, title, "no_match", "no folder passed filters")
            self._summary_failed(f"{artist} – {title}", "no folder passed filters")
            return False
        tried: set[tuple[str, str]] = set()
        last = "no folder passed filters"
        capped_name = ""
        for _attempt in range(max(1, _DOWNLOAD_ATTEMPTS)):
            best = None
            for item in sorted(candidates, key=lambda row: row.score, reverse=True):
                user = (item.username or "").lower()
                pair = (user, item.remote_dir or "")
                if user in rejected or pair in tried or self.store.source_blocked(item.username, item.remote_dir):
                    continue
                if self._past_daily_cap(item.username):
                    capped_name = item.username or user
                    continue
                best = item
                break
            if best is None:
                if capped_name and not tried:
                    last = f"The daily cap for {capped_name} is already used."
                    self.store.add_event("daily", f"Daily cap for {capped_name}", last, capped_name)
                break
            tried.add(((best.username or "").lower(), best.remote_dir or ""))
            outcome, last = await self._download_one(best, album, key, album_id, artist, title, year, needed_tracks)
            if outcome == "ok":
                return True
            if outcome == "halt":
                return False
            self.store.block_source(best.username, best.remote_dir, last)
            self.store.reject_usernames({best.username}, album_id=album_id, artist=artist)
            rejected.add((best.username or "").lower())
            log.info("Trying another user for %s – %s after %s", artist, title, last)
        self.store.upsert(album_id, artist, title, "failed", last)
        self._summary_failed(f"{artist} – {title}", last)
        return False

    async def _download_one(self, best, album, key, album_id, artist, title, year, needed_tracks):
        """Download one user's folder. ``again`` means try the next user."""
        if self.cfg.dry_run:
            count = len(getattr(best, "files", []) or [])
            detail = f"dry run: would grab {count} file(s) from {best.username}"
            self.store.upsert(album_id, artist, title, "skipped", detail)
            self.store.note_dry_run(f"{artist} – {title}: {detail}")
            log.info(detail)
            return "halt", detail
        log.info(
            "Picked %s :: %s (%s files, score %.1f)",
            best.username,
            best.remote_dir,
            len(best.files),
            best.score,
        )
        self.store.upsert(album_id, artist, title, "downloading", best.username)
        self.cancel_requested = False
        self.set_step(
            Step(kind="queued", artist=artist, album=title, file_total=len(best.files), username=best.username)
        )
        transfers = []
        for item in best.files:
            signal = await self.control.gate(key)
            if signal != "ok":
                await self._abandon_transfers(transfers)
                self._mark_album(album_id, artist, title, signal)
                return "halt", signal
            transfer = await self.client.transfers.download(best.username, item.filename)
            transfers.append(transfer)
            self.set_step(
                Step(
                    kind="queued",
                    artist=artist,
                    album=title,
                    file_index=len(transfers),
                    file_total=len(best.files),
                    username=best.username,
                )
            )
            if self.cancel_requested:
                break
        self.active_transfers = transfers
        if transfers:
            self._visit_had_download = True
        dest = album_dest_folder(self.cfg, artist, title, year)
        staged: set[int] = set()
        sources: list[Path] = []

        async def stage_finished() -> None:
            for transfer in transfers:
                if id(transfer) in staged:
                    continue
                if state_name(transfer) != "COMPLETE":
                    continue
                staged.add(id(transfer))
                self.set_step(
                    Step(
                        kind="converting",
                        artist=artist,
                        album=title,
                        detail=Path(getattr(transfer, "local_path", "") or "").name,
                    )
                )
                src = await asyncio.to_thread(stage_one_file, self.cfg, dest, transfer)
                if src is not None:
                    sources.append(src)

        deadline = [asyncio.get_running_loop().time() + self.cfg.download_timeout_minutes * 60]
        while asyncio.get_running_loop().time() < deadline[0]:
            names = [state_name(t) for t in transfers]
            await stage_finished()
            done = all(n in {"COMPLETE", "FAILED", "ABORTED"} for n in names)
            if done:
                break
            if self.cancel_requested:
                for t in transfers:
                    if state_name(t) not in {"COMPLETE", "FAILED", "ABORTED"}:
                        await abort_transfer(self.client, t)
                break
            complete = sum(1 for n in names if n == "COMPLETE")
            self.set_step(
                Step(
                    kind="downloading",
                    artist=artist,
                    album=title,
                    file_index=min(len(transfers), complete + (0 if complete == len(transfers) else 1)),
                    file_total=len(transfers),
                    username=best.username,
                )
            )
            log.info("Download progress %s/%s complete", complete, len(transfers))
            self.remember_completed(transfers, artist, title, "album")
            signal = await self.control.gate(key, sleep=2, deadline=deadline)
            if signal != "ok":
                await self._abandon_transfers(transfers)
                self._mark_album(album_id, artist, title, signal)
                return "halt", signal

        await stage_finished()
        self.remember_completed(transfers, artist, title, "album")
        self.active_transfers = []
        self.maybe_auto_import()
        complete_transfers = [t for t in transfers if state_name(t) == "COMPLETE"]
        needed = max(1, len(best.files) - self.cfg.track_count_tolerance)
        if self.cancel_requested or len(complete_transfers) < needed:
            detail = "cancelled" if self.cancel_requested else f"finished {len(complete_transfers)}/{len(transfers)}"
            log.warning("Incomplete download for %s - %s (%s)", artist, title, detail)
            leftovers = [t for t in transfers if t not in complete_transfers]
            if self.cfg.delete_sources_after_import:
                leftovers = transfers
            self.discard_failed_downloads(leftovers)
            self.cancel_requested = False
            return "again", detail
        self.cancel_requested = False

        folder, leftover = stage_album_folder(self.cfg, artist, title, year, complete_transfers)
        for src in leftover:
            if src not in sources:
                sources.append(src)
        if not any(path.suffix.lower() in AUDIO_EXT for path in folder.iterdir()):
            return "again", "no files staged"
        ready, reason = self._prepare_staged_folder(folder, album, needed_tracks)
        if not ready:
            return "again", reason
        label = f"{artist} – {title}"
        self._summary_grabbed(f"{label} from {best.username}")
        self.store.upsert(album_id, artist, title, "staged", str(folder))
        if self.cfg.auto_import_after_artist:
            self._visit_had_download = True
            return "ok", ""
        scanned, reason = self._scan_staged_folder(folder)
        if not scanned:
            self._move_artist_to_review(artist)
            return "again", reason
        self.resync_artist_from_lidarr(artist)
        job = self.store.get(album_id)
        if job and job["status"] == "imported":
            self._summary_imported(label)
            return "ok", ""
        log.info("Asked Lidarr to import %s", folder)
        return "ok", ""

    def _prepare_staged_folder(self, folder: Path, album: dict, tracks: list) -> tuple[bool, str]:
        try:
            tag_album_folder(folder, album, tracks, min_ratio=self.cfg.min_filename_ratio)
        except Exception:
            log.exception("Could not write MusicBrainz tags in %s", folder)
        reason = folder_fake_reason(folder)
        if reason:
            log.warning("Rejected %s: %s", folder.name, reason)
            return False, reason
        return True, ""

    def _scan_staged_folder(self, folder: Path) -> tuple[bool, str]:
        scan_path = str(Path(self.cfg.lidarr_scan_path) / folder.name)
        try:
            result = self.lidarr.scan_downloaded(scan_path)
        except Exception as exc:
            try:
                result = self.lidarr.scan_downloaded(str(folder))
            except Exception:
                return False, f"scan failed: {exc}"
        waited = self._finish_lidarr_command(result)
        if self._command_failed(waited):
            return False, f"Lidarr scan {waited.get('status') or 'failed'}"
        return True, ""

    def _scored_folders(self, results, artist, title, titles, durations, gate, rejected) -> list:
        found = []
        for folder in group_results(results, self.cfg.ignored_users):
            if (folder.username or "").lower() in rejected:
                continue
            if self.store.source_blocked(folder.username, folder.remote_dir):
                continue
            scored = score_folder(
                folder,
                artist,
                title,
                titles,
                self.cfg.preferred_extensions,
                self.cfg.min_filename_ratio,
                self.cfg.track_count_tolerance,
                self.cfg.min_mp3_bitrate,
                durations=durations,
                gate=gate,
            )
            if scored.reason == "ok":
                found.append(scored)
        return found

    def _metadata_profile_map(self) -> dict[int, dict]:
        if self._metadata_profiles is not None:
            return self._metadata_profiles
        loader = getattr(self.lidarr, "metadata_profiles", None)
        cached: dict[int, dict] = {}
        if callable(loader):
            try:
                rows = loader() or []
            except Exception:
                log.exception("Could not read Lidarr metadata profiles")
                rows = []
            for row in rows:
                if isinstance(row, dict) and row.get("id") is not None:
                    cached[int(row["id"])] = row
        self._metadata_profiles = cached
        return cached

    def take_config_dirty(self) -> bool:
        dirty = self._config_dirty
        self._config_dirty = False
        return dirty

    def decide_upload(self, username: str, filename: str, size: int) -> Decision:
        policy = policy_from_config(self.cfg)
        queued = self._queued_uploads()
        return admit(policy, username, filename, size, queued, self._peer_share_files(username))

    def _peer_share_files(self, username: str) -> int | None:
        client = self.client
        if client is None:
            return None
        users = getattr(client, "users", None)
        if users is None:
            return None
        getter = getattr(users, "get_user_object", None)
        if not callable(getter):
            return None
        try:
            user = getter(username)
        except Exception:
            return None
        count = getattr(user, "shared_file_count", None)
        if count is None:
            return 0 if policy_from_config(self.cfg).require_peer_share else None
        try:
            return int(count)
        except (TypeError, ValueError):
            return 0

    def _queued_uploads(self) -> list[QueuedUpload]:
        client = self.client
        manager = getattr(client, "transfers", None) if client else None
        if manager is None:
            return []
        rows = []
        for transfer in list(manager.get_uploads()):
            status = _upload_status(transfer)
            if status not in {"queued", "uploading", "paused"}:
                continue
            rows.append(
                QueuedUpload(
                    username=transfer_username(transfer),
                    filename=str(getattr(transfer, "remote_path", "") or getattr(transfer, "filename", "") or ""),
                    size=int(getattr(transfer, "filesize", 0) or 0),
                    status=status,
                )
            )
        return rows

    def ban_user(self, username: str) -> None:
        name = (username or "").strip()
        if not name:
            return
        self.cfg.ignored_users.add(name.casefold())
        self._config_dirty = True
        client = self.client
        settings = getattr(client, "settings", None) if client is not None else None
        if settings is None:
            return
        blocked = dict(settings.users.blocked)
        blocked[name] = BlockingFlag.ALL
        client.settings.users.blocked = blocked

    def _install_upload_guard(self) -> None:
        client = self.client
        if client is None:
            return
        manager = client.transfers
        if getattr(manager, "_lidarr_guard", False):
            return
        original = manager._add_upload
        worker = self

        async def guarded(username: str, remote_path: str):
            try:
                item = await manager._shares_manager.get_shared_item(remote_path, username)
            except (SlskFileNotFoundError, FileNotFoundError, FileNotSharedError):
                raise
            try:
                size = int(await manager._shares_manager.get_filesize(item))
            except Exception:
                size = 0
            decision = worker.decide_upload(username, remote_path, size)
            if not decision.allow:
                if decision.ban:
                    worker.ban_user(username)
                log.info("Refused upload of %s to %s: %s", remote_path, username, decision.reason)
                raise FileNotSharedError(decision.reason)
            transfer = await original(username, remote_path)
            if worker.cfg.pause_uploads:
                try:
                    await transfer.state.pause()
                except Exception:
                    log.exception("Could not pause upload %s", remote_path)
                worker._auto_paused.add(_upload_key(transfer))
            return transfer

        manager._add_upload = guarded
        manager._lidarr_guard = True

    def apply_share_settings(self, *, scan: bool = False) -> None:
        """Push slots, speed, bans, and folders into a running client."""
        client = self.client
        if client is None:
            return
        policy = policy_from_config(self.cfg)
        client.settings.transfers.limits.upload_slots = policy.upload_slots
        client.settings.network.limits.upload_speed_kbps = policy.upload_speed_kbps
        network = getattr(client, "network", None)
        if network is not None and hasattr(network, "set_upload_speed_limit"):
            try:
                network.set_upload_speed_limit(policy.upload_speed_kbps)
            except Exception:
                log.exception("Could not set the upload speed limit")
        blocked = {
            name: flag
            for name, flag in dict(client.settings.users.blocked).items()
            if name.casefold() in policy.banned or flag != BlockingFlag.ALL
        }
        for name in policy.banned:
            blocked[name] = BlockingFlag.ALL
        client.settings.users.blocked = blocked
        client.settings.shares.directories = [
            SharedDirectorySettingEntry(path=path, share_mode=DirectoryShareMode.EVERYONE)
            for path in effective_share_dirs(self.cfg)
        ]
        try:
            client.shares.load_from_settings()
        except Exception:
            log.exception("Could not refresh shared folders")
        if scan:
            self._schedule_share_scan()

    def _schedule_share_scan(self) -> None:
        if self.client is None or not effective_share_dirs(self.cfg):
            self.share_counts = (0, 0)
            return
        loop = self._loop or asyncio.get_running_loop()
        if self._share_task is not None and not self._share_task.done():
            self._share_task.cancel()
        self._share_task = loop.create_task(self._run_share_scan())

    async def _run_share_scan(self) -> None:
        client = self.client
        if client is None:
            return
        install_responsive_share_scan(client)
        try:
            await client.shares.scan()
            self.share_counts = tuple(client.shares.get_stats())
            log.info("Share scan complete: %s folders, %s files", *self.share_counts)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Share scan failed")

    async def rescan_shares(self, paths: list[str] | None = None) -> tuple[int, int]:
        if paths is not None:
            cleaned = [str(path).strip() for path in paths if str(path).strip()]
            self.cfg.share_dir = cleaned[0] if cleaned else ""
            self.cfg.share_dirs = cleaned[1:]
            self._config_dirty = True
        if not self.soulseek_logged_in():
            raise RuntimeError("Start the worker before rescanning. Sharing uses that Soulseek login.")
        self.apply_share_settings(scan=False)
        await self._run_share_scan()
        return self.share_counts

    def _schedule_housekeeping(self) -> None:
        loop = self._loop or asyncio.get_running_loop()
        if self._housekeeping_task is not None and not self._housekeeping_task.done():
            return
        self._housekeeping_task = loop.create_task(self._share_housekeeping())

    async def _share_housekeeping(self) -> None:
        while self.client is not None and not self.control.is_stopped():
            try:
                await self._enforce_upload_policy()
                self._remember_finished_uploads()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Upload housekeeping failed")
            await asyncio.sleep(1)

    async def _enforce_upload_policy(self) -> None:
        client = self.client
        if client is None:
            return
        policy = policy_from_config(self.cfg)
        transfers = list(client.transfers.get_uploads())
        live: list[LiveUpload] = []
        by_key = {}
        for transfer in transfers:
            key = _upload_key(transfer)
            status = _upload_status(transfer)
            by_key[key] = transfer
            try:
                speed = float(transfer.get_speed() or 0)
            except Exception:
                speed = 0.0
            live.append(
                LiveUpload(
                    key=key,
                    username=transfer_username(transfer),
                    speed=speed,
                    status=status,
                    priority=transfer_username(transfer).casefold() in policy.priority,
                )
            )
        pause_keys = uploads_to_pause(policy, live)
        for key, transfer in by_key.items():
            status = _upload_status(transfer)
            if key in pause_keys and status in {"queued", "uploading"}:
                try:
                    await client.transfers.pause(transfer)
                    self._auto_paused.add(key)
                except Exception:
                    log.exception("Could not pause upload %s", key)
            elif key not in pause_keys and key in self._auto_paused and status == "paused":
                try:
                    await client.transfers.queue(transfer)
                except Exception:
                    log.exception("Could not resume upload %s", key)
                self._auto_paused.discard(key)

    def _remember_finished_uploads(self) -> None:
        client = self.client
        if client is None:
            return
        for transfer in list(client.transfers.get_uploads()):
            status = _upload_status(transfer)
            if status not in {"done", "failed", "cancelled"}:
                continue
            username = transfer_username(transfer)
            filename = str(getattr(transfer, "remote_path", "") or "")
            marker = (username, filename, status)
            if marker in self._recorded_uploads:
                continue
            self._recorded_uploads.add(marker)
            self.store.add_upload(username, filename, int(getattr(transfer, "filesize", 0) or 0), status)

    def upload_snapshot(self, history_limit: int = 200) -> dict:
        rows = []
        client = self.client
        manager = getattr(client, "transfers", None) if client else None
        if manager is not None:
            for transfer in list(manager.get_uploads()):
                status = _upload_status(transfer)
                if status in {"done", "failed", "cancelled"}:
                    continue
                started = getattr(transfer, "start_time", None)
                when = ""
                if started:
                    try:
                        when = datetime.fromtimestamp(float(started)).strftime("%Y-%m-%d %H:%M")
                    except (TypeError, ValueError, OSError):
                        when = ""
                try:
                    speed = float(transfer.get_speed() or 0)
                except Exception:
                    speed = 0.0
                rows.append(
                    {
                        "username": transfer_username(transfer),
                        "filename": str(getattr(transfer, "remote_path", "") or ""),
                        "size": int(getattr(transfer, "filesize", 0) or 0),
                        "progress": transfer_percent(transfer),
                        "speed": speed,
                        "status": status,
                        "when": when,
                        "live": True,
                    }
                )
        history = []
        for item in self.store.recent_uploads(history_limit):
            history.append(
                {
                    "username": item["username"],
                    "filename": item["filename"],
                    "size": int(item["size"] or 0),
                    "progress": 100.0 if item["status"] == "done" else None,
                    "speed": 0.0,
                    "status": item["status"],
                    "when": item["finished_at"],
                    "live": False,
                }
            )
        totals = self.store.upload_totals()
        return {
            "rows": rows,
            "history": history,
            "pause_all": bool(self.cfg.pause_uploads),
            "share_folders": self.share_counts[0],
            "share_files": self.share_counts[1],
            **totals,
        }

    async def pause_upload(self, username: str, filename: str) -> None:
        transfer = self._find_upload(username, filename)
        if transfer is None or self.client is None:
            return
        self._auto_paused.discard(_upload_key(transfer))
        await self.client.transfers.pause(transfer)

    async def resume_upload(self, username: str, filename: str) -> None:
        transfer = self._find_upload(username, filename)
        if transfer is None or self.client is None:
            return
        self._auto_paused.discard(_upload_key(transfer))
        await self.client.transfers.queue(transfer)

    async def cancel_upload(self, username: str, filename: str) -> None:
        transfer = self._find_upload(username, filename)
        if transfer is None or self.client is None:
            return
        await abort_transfer(self.client, transfer)

    async def cancel_user_uploads(self, username: str) -> None:
        if self.client is None:
            return
        target = (username or "").casefold()
        for transfer in list(self.client.transfers.get_uploads()):
            if transfer_username(transfer).casefold() != target:
                continue
            if _upload_status(transfer) in {"done", "failed", "cancelled"}:
                continue
            await abort_transfer(self.client, transfer)

    def set_pause_uploads(self, paused: bool) -> None:
        self.cfg.pause_uploads = bool(paused)
        self._config_dirty = True

    def _find_upload(self, username: str, filename: str):
        client = self.client
        if client is None:
            return None
        target_user = (username or "").casefold()
        target_file = filename or ""
        for transfer in client.transfers.get_uploads():
            if transfer_username(transfer).casefold() != target_user:
                continue
            remote = str(getattr(transfer, "remote_path", "") or "")
            if remote == target_file:
                return transfer
        return None

    def _remote_files(self, files: list) -> list[RemoteFile]:
        remotes: list[RemoteFile] = []
        for item in files:
            filename = str(getattr(item, "filename", "") or "")
            if not filename:
                continue
            remotes.append(
                RemoteFile(
                    username=str(getattr(item, "username", "") or ""),
                    filename=filename,
                    size=int(getattr(item, "size", 0) or 0),
                    bitrate=int(getattr(item, "bitrate", 0) or 0),
                    extension=str(getattr(item, "extension", "") or ""),
                    free_slot=bool(getattr(item, "free_slot", False)),
                    speed=int(getattr(item, "speed", 0) or 0),
                    queue=int(getattr(item, "queue", 0) or 0),
                    state=str(getattr(item, "state", "") or ""),
                    folder=str(getattr(item, "folder", "") or ""),
                )
            )
        return remotes

    def _wanted_tracks(self, names: list[str], folds: list[str]) -> list[WantedTrack]:
        if self.store.wanted_catalog_ready():
            rows = self.store.wanted_tracks_for(names, folds)
            return [
                WantedTrack(
                    artist=row["artist"],
                    album=row["album"],
                    song=row["song"],
                    key=row["key"],
                    kind=row["kind"],
                    artist_key=row["artist_key"],
                    album_id=row["album_id"],
                    track_id=row["track_id"],
                )
                for row in rows
            ]
        return self._tracks_from_memory(names, folds)

    def match_user_files(self, files: list) -> list:
        remotes = self._remote_files(files)
        names, folds = candidate_names(remotes)
        tracks = self._wanted_tracks(names, folds)
        return match_files_to_wanted(
            remotes,
            tracks,
            min_filename_ratio=float(self.cfg.min_filename_ratio),
            preferred_extensions=list(self.cfg.preferred_extensions),
            min_mp3_bitrate=int(self.cfg.min_mp3_bitrate),
        )

    def plan_wanted_downloads(
        self,
        files: list,
        *,
        artist: str,
        album: str = "",
        kind: str = "artist",
        album_id: int | None = None,
        track_id: int | None = None,
        title: str = "",
    ) -> WantedPick:
        """Wanted albums and tracks for this artist, best source across every result."""
        artist_name = (artist or "").strip()
        if not artist_name:
            return WantedPick()
        remotes = self._remote_files(files)
        tracks = narrow_wanted_tracks(
            self._wanted_tracks([artist_name], [normalize(artist_name)]),
            kind=kind,
            album=album,
            album_id=album_id,
            track_id=track_id,
            title=title,
        )
        return choose_wanted_downloads(
            remotes,
            tracks,
            min_filename_ratio=float(self.cfg.min_filename_ratio),
            preferred_extensions=list(self.cfg.preferred_extensions),
            min_mp3_bitrate=int(self.cfg.min_mp3_bitrate),
        )

    def _tracks_from_memory(self, names: list[str], folds: list[str]) -> list[WantedTrack]:
        from wanted import artist_key

        tokens = {artist_key(name) for name in names}
        fold_set = set(folds)
        catalog = self.catalog
        with catalog._lock:
            labels = list(catalog._artist_labels)
            grouped = {key: list(rows) for key, rows in catalog._grouped.items()}
        chosen = set()
        for label in labels:
            token = artist_key(label)
            if token in tokens or normalize(label) in fold_set:
                chosen.add(token)
        for token in tokens:
            if token in grouped:
                chosen.add(token)
        tracks: list[WantedTrack] = []
        choices = self.store.wanted_choices()
        for token in chosen:
            for item in grouped.get(token, []):
                if item.kind not in {"album", "song"}:
                    continue
                if not is_enabled(item.key, choices):
                    continue
                tracks.append(
                    WantedTrack(
                        artist=item.artist,
                        album=item.album,
                        song=item.song,
                        key=item.key,
                        kind=item.kind,
                        artist_key=token,
                        album_id=item.album_id,
                        track_id=item.track_id,
                    )
                )
        return tracks

    async def download_manual(self, specs: list[dict], link: dict | None = None) -> str:
        """Queue files from a manual search through the same download and import path.

        Manual searches share the worker's Soulseek login. This does not pause
        the worker's own searches; it only adds transfers on that connection.
        """
        if not specs:
            return ""
        if self.cfg.dry_run:
            username = str(specs[0].get("username") or "")
            detail = f"dry run: would grab {len(specs)} file(s) from {username}"
            self.store.note_dry_run(detail)
            log.info(detail)
            for spec in specs:
                self._set_manual_state(str(spec.get("id") or ""), "skipped")
            return detail
        specs = self._skip_queued_specs(specs, link)
        if not specs:
            log.info("Nothing new to queue; those files are already queued or in the download folder")
            return ""
        if not await self.ensure_soulseek():
            raise RuntimeError("Start the worker to download. Downloads use that Soulseek connection.")
        assert self.client is not None
        link = link or {}
        artist = str(link.get("artist") or "Manual")
        album = str(link.get("album") or "")
        title = str(link.get("title") or album or "Manual")
        folder_title = album or title or "Manual"
        username = str(specs[0].get("username") or "")
        announce = self.step.kind not in {"searching", "waiting", "matching", "downloading", "converting", "queued"}
        if announce:
            self.set_step(
                Step(kind="queued", artist=artist, album=folder_title, file_total=len(specs), username=username)
            )
        paired: list[tuple[str, object, dict]] = []
        for spec in specs:
            file_id = str(spec.get("id") or "")
            spec_link = spec.get("link") if isinstance(spec.get("link"), dict) else link
            self._set_manual_state(file_id, "queued")
            transfer = await self.client.transfers.download(str(spec["username"]), str(spec["filename"]))
            paired.append((file_id, transfer, spec_link or {}))
            self.manual_transfers.append(transfer)
            self._set_manual_state(file_id, "downloading")
        if announce:
            self.set_step(
                Step(
                    kind="downloading",
                    artist=artist,
                    album=folder_title,
                    file_index=1,
                    file_total=len(paired),
                    username=username,
                )
            )
        deadline = time.monotonic() + max(30, int(self.cfg.download_timeout_minutes or 1) * 60)
        while time.monotonic() < deadline:
            names = [state_name(item) for _, item, _link in paired]
            if names and all(name in {"COMPLETE", "FAILED", "ABORTED"} for name in names):
                break
            if self.control.is_stopped():
                for _, transfer, _link in paired:
                    if state_name(transfer) not in {"COMPLETE", "FAILED", "ABORTED"}:
                        await abort_transfer(self.client, transfer)
                break
            done = sum(1 for name in names if name == "COMPLETE")
            if announce and self.step.kind in {"queued", "downloading"}:
                self.set_step(
                    Step(
                        kind="downloading",
                        artist=artist,
                        album=folder_title,
                        file_index=min(len(paired), done + (0 if done == len(paired) else 1)),
                        file_total=len(paired),
                        username=username,
                    )
                )
            await asyncio.sleep(0.4)
        transfers = [item for _, item, _link in paired]
        groups: dict[tuple[str, str], list] = {}
        for _file_id, transfer, spec_link in paired:
            group_artist = str((spec_link or {}).get("artist") or artist)
            group_album = str((spec_link or {}).get("album") or "")
            group_title = str((spec_link or {}).get("title") or group_album or title)
            folder = group_album or group_title or "Manual"
            groups.setdefault((group_artist, folder), []).append(transfer)
        last_dest = album_dest_folder(self.cfg, artist, folder_title, None)
        for (group_artist, folder), group_transfers in groups.items():
            self.remember_completed(group_transfers, group_artist, folder, "manual")
            dest = album_dest_folder(self.cfg, group_artist, folder, None)
            last_dest = dest
            complete = [item for item in group_transfers if state_name(item) == "COMPLETE"]
            for transfer in complete:
                await asyncio.to_thread(stage_one_file, self.cfg, dest, transfer)
            if not complete:
                continue
            self._prepare_staged_folder(dest, {"title": folder, "artist": {"artistName": group_artist}}, [])
            scanned, reason = self._scan_staged_folder(dest)
            if not scanned:
                log.warning("Lidarr scan failed for manual download %s: %s", dest, reason)
        finished = {id(item) for item in transfers}
        self.manual_transfers = [item for item in self.manual_transfers if id(item) not in finished or state_name(item) not in {"COMPLETE", "FAILED", "ABORTED"}]
        self.maybe_auto_import()
        for file_id, transfer, _link in paired:
            self._set_manual_state(file_id, "downloaded" if state_name(transfer) == "COMPLETE" else "failed")
        if announce and self.step.kind in {"queued", "downloading", "converting"} and not self.control.is_stopped():
            self._note_between_passes()
        return str(last_dest)

    async def browse_folder(self, username: str, directory: str) -> list[dict]:
        """List one folder on a peer. One request, no second Soulseek login."""
        from aioslsk.commands import PeerGetDirectoryContentCommand

        if not self.soulseek_logged_in():
            raise RuntimeError("Soulseek is not connected")
        assert self.client is not None
        directories = await self.client.execute(
            PeerGetDirectoryContentCommand(username, directory),
            response=True,
            timeout=15,
        )
        records = []
        for entry in directories or []:
            folder = str(getattr(entry, "name", "") or directory)
            for item in list(getattr(entry, "files", None) or []):
                record = file_from_share(username, item, folder=folder)
                records.append(
                    {
                        "username": record.username,
                        "filename": record.filename,
                        "folder": record.folder or folder,
                        "size": record.size,
                        "bitrate": record.bitrate,
                        "duration": record.duration,
                        "extension": record.extension,
                        "free_slot": record.free_slot,
                        "speed": record.speed,
                        "queue": record.queue,
                    }
                )
        return records

    async def browse_user(self, username: str) -> dict:
        """Profile and share list for one peer. One Soulseek login, the worker's."""
        from aioslsk.commands import (
            GetUserStatsCommand,
            GetUserStatusCommand,
            PeerGetSharesCommand,
            PeerGetUserInfoCommand,
        )

        from user_browse import profile_from_reply, shares_from_reply

        name = (username or "").strip()
        if not name:
            raise RuntimeError("Enter a Soulseek username.")
        if not self.soulseek_logged_in():
            raise RuntimeError("Soulseek is not connected")
        assert self.client is not None
        client = self.client
        status = await client.execute(GetUserStatusCommand(name), response=True, timeout=15)
        stats = await client.execute(GetUserStatsCommand(name), response=True, timeout=15)
        info = await client.execute(PeerGetUserInfoCommand(name), response=True, timeout=20)
        shares = await client.execute(PeerGetSharesCommand(name), response=True, timeout=90)
        if isinstance(shares, tuple) and len(shares) == 2:
            directories, locked = shares
        else:
            directories, locked = shares, []
        return {
            "username": name,
            "profile": profile_from_reply(info, status, stats),
            "directories": shares_from_reply(directories, locked),
        }

    async def download_paced(
        self,
        specs: list[dict],
        *,
        pace_seconds: int = 5,
        files_at_once: int = 1,
    ) -> str:
        """Queue a browsed user's missing files one batch at a time, then wait.

        ``files_at_once`` is 1 unless a looser rule was chosen. The wait after
        each batch is ``pace_seconds``. A second call does not start until the
        first has finished.
        """
        if not specs:
            return "Nothing to download."
        if getattr(self, "_paced_busy", False):
            return "A download from a browsed user is already running."
        username = str(specs[0].get("username") or "")
        cap = max(0, int(getattr(self.cfg, "browse_daily_files", 20) or 0))
        room = daily_room(self.store.user_day_files(username), cap)
        if room <= 0:
            used = self.store.user_day_files(username)
            detail = f"The daily cap for {username} is already used ({used} of {cap})."
            self.store.add_event("daily", f"Daily cap for {username}", detail, username)
            self.store.set_meta("last_browse_download", detail)
            return detail
        if len(specs) > room:
            specs = list(specs[:room])
        if self.cfg.dry_run:
            detail = f"dry run: would grab {len(specs)} file(s) from {username}, {max(1, int(files_at_once or 1))} at a time"
            self.store.note_dry_run(detail)
            self.store.set_meta("last_browse_download", detail)
            log.info(detail)
            return detail
        self._paced_busy = True
        try:
            specs = self._skip_queued_specs(specs, None)
            if not specs:
                detail = "Nothing new to queue. Those files are already queued or downloaded."
                self.store.set_meta("last_browse_download", detail)
                return detail
            if not self.soulseek_logged_in():
                if not await self.ensure_soulseek():
                    raise RuntimeError("Start the worker to download. Downloads use that Soulseek connection.")
            if self.control.is_stopped():
                detail = "Start the worker to download. Downloads use that Soulseek connection."
                self.store.set_meta("last_browse_download", detail)
                return detail
            assert self.client is not None
            width = max(1, int(files_at_once or 1))
            pause = max(0, int(pace_seconds or 0))
            paired: list[tuple[str, object, dict]] = []
            per_file = max(30, int(self.cfg.download_timeout_minutes or 1) * 60)
            for start in range(0, len(specs), width):
                if self.control.is_stopped():
                    break
                chunk = specs[start : start + width]
                started: list[tuple[str, object, dict]] = []
                for spec in chunk:
                    file_id = str(spec.get("id") or "")
                    spec_link = spec.get("link") if isinstance(spec.get("link"), dict) else {}
                    self._set_manual_state(file_id, "queued")
                    transfer = await self.client.transfers.download(str(spec["username"]), str(spec["filename"]))
                    started.append((file_id, transfer, spec_link or {}))
                    self.manual_transfers.append(transfer)
                    self._set_manual_state(file_id, "downloading")
                deadline = time.monotonic() + per_file
                while time.monotonic() < deadline:
                    names = [state_name(item) for _, item, _link in started]
                    if names and all(name in {"COMPLETE", "FAILED", "ABORTED"} for name in names):
                        break
                    if self.control.is_stopped():
                        for _, transfer, _link in started:
                            if state_name(transfer) not in {"COMPLETE", "FAILED", "ABORTED"}:
                                await abort_transfer(self.client, transfer)
                        break
                    await asyncio.sleep(0.05)
                paired.extend(started)
                if start + width < len(specs) and pause and not self.control.is_stopped():
                    remaining = pause
                    while remaining > 0 and not self.control.is_stopped():
                        step = min(0.25, remaining)
                        await asyncio.sleep(step)
                        remaining -= step
            groups: dict[tuple[str, str], list] = {}
            for _file_id, transfer, spec_link in paired:
                artist = str((spec_link or {}).get("artist") or "Manual")
                album = str((spec_link or {}).get("album") or (spec_link or {}).get("title") or "Manual")
                groups.setdefault((artist, album), []).append(transfer)
            done = 0
            for (artist, album), transfers in groups.items():
                self.remember_completed(transfers, artist, album, "browse")
                dest = album_dest_folder(self.cfg, artist, album, None)
                complete = [item for item in transfers if state_name(item) == "COMPLETE"]
                done += len(complete)
                staged = False
                for transfer in complete:
                    if await asyncio.to_thread(stage_one_file, self.cfg, dest, transfer):
                        staged = True
                if not staged:
                    continue
                self._prepare_staged_folder(dest, {"title": album, "artist": {"artistName": artist}}, [])
                scanned, reason = self._scan_staged_folder(dest)
                if not scanned:
                    log.warning("Lidarr scan failed for browse download %s: %s", dest, reason)
            self.maybe_auto_import()
            detail = f"Downloaded {done} of {len(specs)} file(s) from {username}, {width} at a time, {pause}s apart."
            self.store.set_meta("last_browse_download", detail)
            self.store.add_event("browse", detail, detail, username)
            log.info(detail)
            return detail
        finally:
            self._paced_busy = False

