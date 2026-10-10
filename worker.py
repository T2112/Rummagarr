from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from aioslsk.client import SoulSeekClient
from aioslsk.settings import (
    CredentialsSettings,
    NetworkSettings,
    Settings,
    SharesSettings,
    SharedDirectorySettingEntry,
    ListeningSettings,
    UpnpSettings,
    ServerSettings,
    ReconnectSettings,
)
from aioslsk.shares.model import DirectoryShareMode

from config_loader import Config
from control import WorkerControl
from lidarr_client import LidarrClient
from status_ui import normalize_recent_limit, start_status_ui
from wanted import (
    WantedCatalog,
    album_key,
    albums_for_search,
    artist_name,
    holes_for_search,
    holes_from_catalog,
    is_enabled,
    song_key,
)
from match import (
    AUDIO_EXT,
    build_queries,
    build_track_queries,
    group_results,
    score_folder,
    score_track_item,
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


def build_slsk_settings(cfg: Config) -> Settings:
    directories = []
    if cfg.share_dir:
        directories.append(
            SharedDirectorySettingEntry(
                path=cfg.share_dir,
                share_mode=DirectoryShareMode.EVERYONE,
            )
        )
    return Settings(
        credentials=CredentialsSettings(username=cfg.slsk_username, password=cfg.slsk_password),
        network=NetworkSettings(
            server=ServerSettings(reconnect=ReconnectSettings(auto=True, timeout=15)),
            listening=ListeningSettings(
                port=cfg.listen_port,
                obfuscated_port=cfg.obfuscated_port,
            ),
            upnp=UpnpSettings(enabled=cfg.enable_upnp),
        ),
        shares=SharesSettings(
            download=str(cfg.download_dir),
            directories=directories,
            # A long share scan can outlive the server socket. Scan later if needed.
            scan_on_start=False,
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
        self.lidarr = LidarrClient(cfg.lidarr_url, cfg.lidarr_api_key)
        self.store = JobStore(cfg.state_db)
        self.client: SoulSeekClient | None = None
        self.active_transfers: list = []
        self.job_info: dict = {"phase": "idle", "artist": "", "title": ""}
        self.cancel_requested = False
        self._ui = None
        self.control = WorkerControl()
        self.catalog = WantedCatalog()
        self.current_key: str | None = None
        self._active_keys: set[str] = set()
        self._key_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

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

    def ui_snapshot(self, recent_limit: int = 10) -> dict:
        limit = normalize_recent_limit(recent_limit)
        transfers = []
        for item in list(self.active_transfers):
            transfers.append(
                {
                    "filename": transfer_filename(item),
                    "username": transfer_username(item),
                    "state": state_name(item) or "WAITING",
                    "percent": transfer_percent(item),
                }
            )
        return {
            "logged_in": self.soulseek_logged_in(),
            "job": dict(self.job_info),
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
        if self.client:
            await self.client.stop()
            self.client = None

    async def run_forever(self, *, autostart: bool = True) -> None:
        if autostart and self.control.is_stopped():
            self.control.start()
        if self.control.is_stopped():
            return
        self._loop = asyncio.get_running_loop()
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
                    break
                signal = await self.control.gate(sleep=self.cfg.cycle_seconds)
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

    def choose_focus_artist(self, missing: list[dict], holes: list[dict]) -> str | None:
        if not self.cfg.focus_one_artist:
            return None
        current = self.store.focus_artist()
        if current and not self.store.artist_on_cooldown(current):
            return current
        if current:
            log.info("Artist %s is on cooldown; picking the next one", current)
        for album in sorted(missing, key=lambda a: artist_name(a).casefold()):
            chosen = artist_name(album)
            if self.store.artist_on_cooldown(chosen):
                continue
            if not self._artist_searchable(chosen):
                continue
            self.store.set_focus_artist(chosen)
            log.info("Focusing on artist: %s", chosen)
            return chosen
        for hole in sorted(holes, key=lambda h: (h["artist_name"] or "").casefold()):
            chosen = hole["artist_name"]
            if self.store.artist_on_cooldown(chosen):
                continue
            if not self._artist_searchable(chosen):
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

    def _artist_searchable(self, artist: str) -> bool:
        if not self.catalog.synced:
            return True
        choices = self.store.wanted_choices()
        related = [
            item
            for item in self.catalog.items()
            if item.kind in {"album", "song"} and self._same_artist(item.artist, artist)
        ]
        if not related:
            return True
        return any(is_enabled(item.key, choices) for item in related)

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
            files = int(stats.get("trackFileCount") or 0)
            total = int(stats.get("totalTrackCount") or stats.get("trackCount") or 0)
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

    async def cycle(self) -> None:
        if self.control.is_stopped():
            return
        # A Sync in the desktop app keeps the wanted list in memory. Until that
        # happens, keep reading Lidarr 20 pages at a time and resume next cycle.
        cached = self.catalog.album_payloads()
        if cached is not None:
            catalog = list(cached)
            log.info("Using synced wanted list (%s albums)", len(catalog))
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
            self.sweep_incomplete_folder()
            return
        missing, api_holes = self._work_for_artist(focus, catalog, include_tracks=not self.catalog.synced)
        truly_missing = []
        for album in missing:
            stats = album.get("statistics") or {}
            files = int(stats.get("trackFileCount") or 0)
            if files > 0:
                continue
            truly_missing.append(album)
        choices = self.store.wanted_choices()
        checked_missing = albums_for_search(truly_missing, choices)
        if self.catalog.synced:
            holes = [
                hole
                for hole in holes_from_catalog(self.catalog.items())
                if self._same_artist(hole["artist_name"], focus)
            ]
            covered = {int(album["id"]) for album in checked_missing}
            holes = [hole for hole in holes if int(hole["album"]["id"]) not in covered]
        else:
            holes = api_holes
        holes = holes_for_search(holes, choices)
        hidden = len(truly_missing) - len(checked_missing)
        if hidden:
            log.info("Left out %s unchecked album(s)", hidden)
        missing = checked_missing
        if not missing and not holes:
            log.info("Artist %s has no checked items; moving on without a cooldown", focus)
            self.store.set_focus_artist(None)
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
            if not is_enabled(album_key(album_id), self.store.wanted_choices()):
                log.info("Skip unchecked album %s - %s", artist, title)
                return
            async with sem:
                if self.control.is_stopped():
                    return
                self.store.upsert(album_id, artist, title, "searching")
                self.set_job("searching album", artist, title)
                ok = await self.process_album(album)
                if not ok:
                    log.info("No usable result yet for %s - %s", artist, title)

        if batch:
            await asyncio.gather(*(run_one(album) for album in batch), return_exceptions=True)
        self.set_job("idle")
        if self.control.is_stopped():
            self.set_job("stopped")
            return
        if self.cfg.fill_missing_tracks and holes:
            await self.cycle_tracks(holes)
        if self.control.is_stopped():
            self.set_job("stopped")
            return
        self._finish_artist_visit(focus)
        self.sweep_incomplete_folder()

    async def cycle_tracks(self, holes: list[dict] | None = None) -> None:
        if holes is None:
            try:
                holes = self.lidarr.missing_tracks_on_partial_albums()
            except Exception:
                log.exception("Could not list missing tracks")
                return
        log.info("Lidarr missing tracks on partial albums: %s", len(holes))
        processed = 0
        for hole in holes:
            if self.control.is_stopped():
                break
            if processed >= self.cfg.max_tracks_per_cycle:
                break
            track = hole["track"]
            track_id = int(track["id"])
            album = hole["album"]
            album_id = int(album["id"])
            artist = hole["artist_name"]
            album_title = hole["album_title"]
            title = track.get("title") or "Unknown Track"
            if not is_enabled(song_key(track_id), self.store.wanted_choices()):
                continue
            if self.store.should_skip_track(track_id, self.cfg.retry_hours):
                continue
            self.store.upsert_track(track_id, album_id, artist, title, "searching")
            self.set_job("searching track", artist, title)
            ok = await self.process_track(hole)
            self.set_job("idle")
            processed += 1
            if not ok:
                log.info("No usable track yet for %s - %s - %s", artist, album_title, title)

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
        queries = build_track_queries(artist, album_title, title)
        log.info("Track search: %s", " | ".join(queries))

        best_item = None
        best_user = ""
        best_score = 0.0
        for query in queries:
            signal = await self.control.gate(key)
            if signal != "ok":
                self._mark_track(hole, signal)
                return False
            request = await self.client.searches.search(query)
            signal = await self.control.gate(key, sleep=self.cfg.search_wait_seconds)
            if signal != "ok":
                try:
                    self.client.searches.remove_request(request)
                except Exception:
                    pass
                self._mark_track(hole, signal)
                return False
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
        self.set_job("downloading track", artist, title)
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
        queries = build_queries(artist, title, year)
        log.info("Searching: %s", " | ".join(queries))

        best = None
        for query in queries:
            signal = await self.control.gate(key)
            if signal != "ok":
                self._mark_album(album_id, artist, title, signal)
                return False
            request = await self.client.searches.search(query)
            signal = await self.control.gate(key, sleep=self.cfg.search_wait_seconds)
            if signal != "ok":
                try:
                    self.client.searches.remove_request(request)
                except Exception:
                    pass
                self._mark_album(album_id, artist, title, signal)
                return False
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
        self.set_job("downloading album", artist, title)
        transfers = []
        for item in best.files:
            signal = await self.control.gate(key)
            if signal != "ok":
                await self._abandon_transfers(transfers)
                self._mark_album(album_id, artist, title, signal)
                return False
            transfer = await self.client.transfers.download(best.username, item.filename)
            transfers.append(transfer)
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
                self.set_job("converting", artist, Path(getattr(transfer, "local_path", "") or "").name)
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
