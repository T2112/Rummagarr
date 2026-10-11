from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
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
from config_loader import Config
from control import WorkerControl
from lidarr_client import LidarrClient
from search_model import file_from_share, result_count
from status_ui import normalize_recent_limit, start_status_ui
from wanted import (
    WantedCatalog,
    album_key,
    albums_for_search,
    artist_name,
    count_value,
    holes_for_search,
    is_enabled,
    optional_id,
    song_key,
)
from match import (
    AUDIO_EXT,
    aliases_from_album,
    build_queries,
    build_track_queries,
    group_results,
    normalize,
    score_folder,
    score_track_item,
)
from sharing import (
    IDLE_FLOOR_SECONDS,
    Decision,
    LiveUpload,
    QueuedUpload,
    RemoteFile,
    SharePolicy,
    WantedTrack,
    admit,
    candidate_names,
    match_files_to_wanted,
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
    proc = subprocess.run(cmd, capture_output=True, text=True)
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
        self.share_counts: tuple[int, int] = (0, 0)
        self._share_task: asyncio.Task | None = None
        self._housekeeping_task: asyncio.Task | None = None
        self._recorded_uploads: set[tuple[str, str, str]] = set()
        self._auto_paused: set[str] = set()
        self._config_dirty = False

    def close(self) -> None:
        if self._ui:
            try:
                self._ui.shutdown()
            except Exception:
                pass
        self.lidarr.close()
        self.store.close()

    def ask_lidarr_rename(self, album: dict) -> None:
        artist = album.get("artist") or {}
        artist_id = album.get("artistId") or artist.get("id")
        if not artist_id:
            return
        try:
            self.lidarr.rename_artist_files(int(artist_id))
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
        step = self.step
        elapsed = int(max(0.0, time.monotonic() - step.started)) if step.started else 0
        if self.control.is_stopped():
            if step.kind == "error":
                return describe_step(step), 0, False
            return "Stopped", 0, False
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

    async def _send_search(self, query: str):
        """One search on the shared Soulseek session.

        The lock only covers sending the request. Waiting for results, and
        downloads, keep going so a manual search does not freeze the worker.
        """
        assert self.client is not None
        async with self._search_slot():
            return await self.client.searches.search(query)

    async def begin_manual_search(self, query: str):
        if not await self.ensure_soulseek():
            raise RuntimeError("Start the worker to search. Search uses that Soulseek login.")
        return await self._send_search(query)

    async def _await_results(
        self,
        request,
        key: str | None,
        seconds: float,
        artist: str,
        album: str,
        query_index: int,
        query_total: int,
    ) -> str:
        started = time.monotonic()
        while True:
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
        return {
            "logged_in": self.soulseek_logged_in(),
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
        self._remembered_ids = seen

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

    async def start_soulseek(self) -> None:
        self.client = SoulSeekClient(build_slsk_settings(self.cfg))
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
            await self.client.stop()
            self.client = None

    async def run_forever(self, *, autostart: bool = True) -> None:
        if autostart and self.control.is_stopped():
            self.control.start()
        if self.control.is_stopped():
            return
        self._loop = asyncio.get_running_loop()
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
                try:
                    await self.cycle()
                except Exception:
                    log.exception("Cycle failed")
                if self.control.is_stopped():
                    self.set_step(Step(kind="stopped"))
                    break
                pause = self._note_between_passes()
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
        if current and not self._cooled(current, cooldowns, now):
            return current
        if current:
            log.info("Artist %s is on cooldown; picking the next one", current)
        if self.store.wanted_catalog_ready():
            skip = {token for token in cooldowns if self._cooled(token, cooldowns, now)}
            chosen = self.store.next_wanted_artist(skip)
            if not chosen:
                self.store.set_focus_artist(None)
                return None
            self.store.set_focus_artist(chosen)
            log.info("Focusing on artist: %s", chosen)
            return chosen
        if self.catalog.synced:
            choices = self.store.wanted_choices()
            for chosen in self.catalog.artist_labels():
                if self._cooled(chosen, cooldowns, now):
                    continue
                if not self.catalog.has_enabled_work(chosen, choices):
                    continue
                self.store.set_focus_artist(chosen)
                log.info("Focusing on artist: %s", chosen)
                return chosen
            self.store.set_focus_artist(None)
            return None
        for album in sorted(missing, key=lambda a: artist_name(a).casefold()):
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

    def _soonest_cooldown_seconds(self) -> int | None:
        now = datetime.now(timezone.utc)
        best: int | None = None
        for until in self.store.artist_cooldowns().values():
            try:
                stamp = datetime.fromisoformat(str(until))
            except ValueError:
                continue
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            delta = int((stamp - now).total_seconds())
            if delta > 0:
                best = delta if best is None else min(best, delta)
        return best

    def _cooled_artist_keys(self) -> set[str]:
        now = datetime.now(timezone.utc)
        cooldowns = self.store.artist_cooldowns()
        return {
            "artist:" + name
            for name in cooldowns
            if self._cooled(name, cooldowns, now)
        }

    def _has_enabled_wanted(self) -> bool:
        if self.store.wanted_catalog_ready():
            return self.store.next_wanted_artist(set()) is not None
        if self.catalog.synced:
            choices = self.store.wanted_choices()
            return any(self.catalog.has_enabled_work(name, choices) for name in self.catalog.artist_labels())
        return False

    def _next_ready_artist(self) -> str | None:
        if self.store.wanted_catalog_ready():
            return self.store.next_wanted_artist(self._cooled_artist_keys())
        if self.catalog.synced:
            choices = self.store.wanted_choices()
            cooldowns = self.store.artist_cooldowns()
            now = datetime.now(timezone.utc)
            for name in self.catalog.artist_labels():
                if self._cooled(name, cooldowns, now):
                    continue
                if self.catalog.has_enabled_work(name, choices):
                    return name
        return None

    def between_pass_wait(self) -> tuple[int, str]:
        """Seconds to sleep, and which setting that wait comes from.

        ``pass`` is Wait before next artist (cycle_seconds). 0 starts the next
        ready artist immediately. ``artist`` is Artist retry period, the
        multi-day skip after a visit. A zero pass wait still sleeps a short
        floor when nobody is ready, so the loop does not spin.
        """
        cycle = max(0, int(self.cfg.cycle_seconds or 0))
        catalog = self.store.wanted_catalog_ready() or self.catalog.synced
        if catalog and not self._has_enabled_wanted():
            return (cycle if cycle else IDLE_FLOOR_SECONDS), "idle"
        if self._next_ready_artist():
            return cycle, "pass"
        wait = self._soonest_cooldown_seconds()
        if wait:
            if cycle == 0:
                pause = min(wait, IDLE_FLOOR_SECONDS)
            else:
                pause = wait
            return max(1, pause), "artist"
        return (cycle if cycle else IDLE_FLOOR_SECONDS), "idle"

    def _apply_wait(self, seconds: int, kind: str) -> None:
        if kind == "artist":
            self.set_step(Step(kind="retry", retry_seconds=seconds, retry_kind="artist"))
            return
        if kind == "pass" and seconds <= 0:
            self.set_step(Step(kind="retry", retry_seconds=0, retry_kind="pass"))
            return
        if kind == "pass":
            self.set_step(Step(kind="retry", retry_seconds=seconds, retry_kind="pass"))
            return
        self.set_step(Step(kind="idle"))

    def _note_nothing_ready(self) -> None:
        seconds, kind = self.between_pass_wait()
        self._apply_wait(seconds, kind)

    def _note_between_passes(self) -> int:
        if self.control.is_stopped():
            self.set_step(Step(kind="stopped"))
            return 0
        seconds, kind = self.between_pass_wait()
        self._apply_wait(seconds, kind)
        return seconds

    async def cycle(self) -> None:
        if self.control.is_stopped():
            return
        self.set_step(Step(kind="preparing"))
        # A Sync stores the wanted list in SQLite. Until that happens, keep
        # reading Lidarr 20 pages at a time and resume next cycle.
        if self.store.wanted_catalog_ready():
            log.info("Using saved wanted list (%s albums)", self.store.wanted_kind_count("album"))
            focus = self.choose_focus_artist([], [])
            if not focus:
                log.info("No artist ready (all cooled off or nothing missing)")
                self._note_nothing_ready()
                self.sweep_incomplete_folder()
                return
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
            focus = self.choose_focus_artist([], [])
            if not focus:
                log.info("No artist ready (all cooled off or nothing missing)")
                self._note_nothing_ready()
                self.sweep_incomplete_folder()
                return
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
            catalog.sort(key=lambda a: (artist_name(a).casefold(), (a.get("title") or "").casefold()))
            focus = self.choose_focus_artist(catalog, [])
            if not focus:
                log.info("No artist ready (all cooled off or nothing missing)")
                self._note_nothing_ready()
                self.sweep_incomplete_folder()
                return
            missing, api_holes = self._work_for_artist(focus, catalog, include_tracks=True)
            truly_missing = self._empty_albums(missing)
            choices = self.store.wanted_choices()
            checked_missing = albums_for_search(truly_missing, choices)
            holes = holes_for_search(api_holes, choices)
        hidden = len(truly_missing) - len(checked_missing)
        if hidden:
            log.info("Left out %s unchecked album(s)", hidden)
        missing = checked_missing
        if not missing and not holes:
            log.info("Artist %s has no checked items; moving on without a cooldown", focus)
            self.store.set_focus_artist(None)
            self._note_between_passes()
            self.sweep_incomplete_folder()
            return
        log.info("Artist %s: %s empty albums, %s partial tracks", focus, len(missing), len(holes))
        batch = []
        for album in missing:
            if len(batch) >= self.cfg.max_albums_per_cycle:
                break
            if self.store.should_skip(int(album["id"]), self.cfg.retry_hours):
                continue
            batch.append(album)
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
            await asyncio.gather(*(run_one(album) for album in batch), return_exceptions=True)
        if self.control.is_stopped():
            self.set_step(Step(kind="stopped"))
            return
        if self.cfg.fill_missing_tracks and holes:
            await self.cycle_tracks(holes)
        if self.control.is_stopped():
            self.set_step(Step(kind="stopped"))
            return
        self._finish_artist_visit(focus)
        self.sweep_incomplete_folder()
        self._note_between_passes()

    async def cycle_tracks(self, holes: list[dict] | None = None) -> None:
        if holes is None:
            try:
                holes = self.lidarr.missing_tracks_on_partial_albums()
            except Exception:
                log.exception("Could not list missing tracks")
                return
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
            request = await self._send_search(query)
            signal = await self._await_results(
                request, key, self.cfg.search_wait_seconds, artist, album_title, index, len(queries)
            )
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
        self.cancel_requested = False
        self.set_step(Step(kind="queued", artist=artist, album=album_title, title=title, file_total=1, username=best_user))
        signal = await self.control.gate(key)
        if signal != "ok":
            self._mark_track(hole, signal)
            return False
        transfer = await self.client.transfers.download(best_user, best_item.filename)
        self.active_transfers = [transfer]
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

        scan_path = str(Path(self.cfg.lidarr_scan_path) / folder.name)
        try:
            self.lidarr.scan_downloaded(scan_path)
        except Exception as exc:
            try:
                self.lidarr.scan_downloaded(str(folder))
            except Exception:
                self.store.upsert_track(track_id, album_id, artist, title, "failed", f"scan failed: {exc}")
                raise
        if self.cfg.delete_sources_after_import:
            cleanup_sources(sources, self.cfg.download_dir)
        self.ask_lidarr_rename(album)
        self.store.upsert_track(track_id, album_id, artist, title, "imported", str(folder))
        log.info("Asked Lidarr to import missing track %s into %s", title, folder)
        return True

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
            return False
        titles = track_titles(needed_tracks)
        queries = build_queries(artist, title, year, aliases_from_album(album))
        log.info("Searching: %s", " | ".join(queries))

        best = None
        for index, query in enumerate(queries, start=1):
            signal = await self.control.gate(key)
            if signal != "ok":
                self._mark_album(album_id, artist, title, signal)
                return False
            self.set_step(
                Step(kind="searching", artist=artist, album=title, query_index=index, query_total=len(queries))
            )
            request = await self._send_search(query)
            signal = await self._await_results(
                request, key, self.cfg.search_wait_seconds, artist, title, index, len(queries)
            )
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
            folders = group_results(request.results, self.cfg.ignored_users)
            for folder in folders:
                scored = score_folder(
                    folder,
                    artist,
                    title,
                    titles,
                    self.cfg.preferred_extensions,
                    self.cfg.min_filename_ratio,
                    self.cfg.track_count_tolerance,
                    self.cfg.min_mp3_bitrate,
                )
                if scored.reason != "ok":
                    continue
                if best is None or scored.score > best.score:
                    best = scored
            try:
                self.client.searches.remove_request(request)
            except Exception:
                pass
            if best and best.score >= 20:
                break

        if best is None:
            self.store.upsert(album_id, artist, title, "no_match", "no folder passed filters")
            return False

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
                return False
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
                return False

        await stage_finished()
        self.remember_completed(transfers, artist, title, "album")
        self.active_transfers = []
        complete_transfers = [t for t in transfers if state_name(t) == "COMPLETE"]
        needed = max(1, len(best.files) - self.cfg.track_count_tolerance)
        if self.cancel_requested or len(complete_transfers) < needed:
            detail = "cancelled" if self.cancel_requested else f"finished {len(complete_transfers)}/{len(transfers)}"
            self.store.upsert(album_id, artist, title, "failed", detail)
            log.warning("Incomplete download for %s - %s (%s)", artist, title, detail)
            leftovers = [t for t in transfers if t not in complete_transfers]
            if self.cfg.delete_sources_after_import:
                leftovers = transfers
            self.discard_failed_downloads(leftovers)
            self.cancel_requested = False
            return False
        self.cancel_requested = False

        folder, leftover = stage_album_folder(self.cfg, artist, title, year, complete_transfers)
        for src in leftover:
            if src not in sources:
                sources.append(src)
        audio_count = sum(1 for p in folder.iterdir() if p.suffix.lower() in AUDIO_EXT)
        if audio_count == 0:
            self.store.upsert(album_id, artist, title, "failed", "no files staged")
            return False

        scan_path = str(Path(self.cfg.lidarr_scan_path) / folder.name)
        try:
            self.lidarr.scan_downloaded(scan_path)
        except Exception as exc:
            # fall back to the Windows path Lidarr may already see
            try:
                self.lidarr.scan_downloaded(str(folder))
            except Exception:
                self.store.upsert(album_id, artist, title, "failed", f"scan failed: {exc}")
                raise
        if self.cfg.delete_sources_after_import:
            cleanup_sources(sources, self.cfg.download_dir)
        self.ask_lidarr_rename(album)
        self.store.upsert(album_id, artist, title, "imported", str(folder))
        log.info("Asked Lidarr to import %s", folder)
        return True

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

    def match_user_files(self, files: list) -> list:
        remotes = [
            RemoteFile(
                username=str(getattr(item, "username", "") or ""),
                filename=str(getattr(item, "filename", "") or ""),
                size=int(getattr(item, "size", 0) or 0),
                bitrate=int(getattr(item, "bitrate", 0) or 0),
                extension=str(getattr(item, "extension", "") or ""),
            )
            for item in files
            if getattr(item, "filename", None)
        ]
        names, folds = candidate_names(remotes)
        if self.store.wanted_catalog_ready():
            rows = self.store.wanted_tracks_for(names, folds)
            tracks = [
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
        else:
            tracks = self._tracks_from_memory(names, folds)
        return match_files_to_wanted(
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
            scan_path = str(Path(self.cfg.lidarr_scan_path) / dest.name)
            try:
                self.lidarr.scan_downloaded(scan_path)
            except Exception:
                log.exception("Lidarr scan failed for %s; trying the staged folder", scan_path)
                try:
                    self.lidarr.scan_downloaded(str(dest))
                except Exception:
                    log.exception("Lidarr scan failed for manual download %s", dest)
        finished = {id(item) for item in transfers}
        self.manual_transfers = [item for item in self.manual_transfers if id(item) not in finished or state_name(item) not in {"COMPLETE", "FAILED", "ABORTED"}]
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
