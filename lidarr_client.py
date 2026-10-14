from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from wanted import count_value, optional_id

log = logging.getLogger("lidarr_slsk")

# A slow Tailscale link needs a long read. Retries cover a dropped packet.
_DEFAULT_TIMEOUT = 120.0
_RETRY_STATUS = {429, 502, 503, 504}


class LidarrUnreachable(RuntimeError):
    """Lidarr did not answer. The status line says Lidarr unreachable."""


class LidarrClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = _DEFAULT_TIMEOUT,
        track_concurrency: int = 6,
    ) -> None:
        self.track_concurrency = max(1, min(8, int(track_concurrency or 1)))
        self._client = httpx.Client(
            base_url=base_url,
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
            timeout=timeout,
            limits=httpx.Limits(
                max_connections=self.track_concurrency + 2,
                max_keepalive_connections=self.track_concurrency + 2,
            ),
        )

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, payload: dict[str, Any] | None = None) -> Any:
        delay = 0.4
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = self._client.request(method, f"/api/v1{path}", params=params, json=payload)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt == 3:
                    raise LidarrUnreachable("Lidarr unreachable") from exc
                log.warning("Lidarr %s %s failed (%s); retrying", method, path, exc.__class__.__name__)
                time.sleep(delay)
                delay = min(delay * 2, 8)
                continue
            if response.status_code in _RETRY_STATUS and attempt < 3:
                time.sleep(delay)
                delay = min(delay * 2, 8)
                continue
            response.raise_for_status()
            return response.json() if response.content else None
        raise LidarrUnreachable("Lidarr unreachable") from last_error

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def _get_once(self, path: str, params: dict[str, Any] | None = None, timeout: float = 5.0) -> Any:
        """One GET. A downed Lidarr returns None instead of the long retry used by Sync."""
        try:
            response = self._client.request(
                "GET",
                f"/api/v1{path}",
                params=params,
                timeout=httpx.Timeout(timeout, connect=1.0),
            )
        except httpx.TransportError:
            log.warning("Lidarr %s did not answer", path)
            return None
        if response.status_code >= 400:
            log.warning("Lidarr %s returned %s", path, response.status_code)
            return None
        if not response.content:
            return []
        try:
            return response.json()
        except ValueError:
            log.warning("Lidarr %s did not return JSON", path)
            return None

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        return self._request("POST", path, payload=payload)

    def ping(self) -> dict[str, Any]:
        status = self._get("/system/status")
        return status if isinstance(status, dict) else {}

    def wanted_missing(
        self,
        page_size: int = 250,
        start_page: int = 1,
        max_pages: int = 20,
        on_page=None,
        should_cancel=None,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        # Lidarr often reports totalPages=1 even when more exist. Page until a
        # short page, but only max_pages at a time so a huge wanted list does
        # not block the cycle.
        albums: list[dict[str, Any]] = []
        page = max(1, int(start_page or 1))
        fetched = 0
        exhausted = False
        limit = max(1, int(max_pages or 20))
        while fetched < limit:
            if should_cancel is not None and should_cancel():
                return albums, page, False
            data = self._get(
                "/wanted/missing",
                params={
                    "page": page,
                    "pageSize": page_size,
                    "monitored": True,
                    "includeArtist": True,
                    "sortKey": "releaseDate",
                    "sortDirection": "descending",
                },
            )
            records = data.get("records") or []
            fetched += 1
            if not records:
                exhausted = True
                break
            albums.extend(records)
            if on_page is not None:
                on_page(len(albums))
            if len(records) < page_size:
                exhausted = True
                break
            page += 1
        next_page = 1 if exhausted else page
        return albums, next_page, exhausted

    def tracks_for_album(self, album_id: int) -> list[dict[str, Any]]:
        payload = self._get("/track", params={"albumId": album_id}) or []
        return payload if isinstance(payload, list) else []

    def tracks_for_artist(self, artist_id: int) -> list[dict[str, Any]]:
        """Every track for one artist. Lidarr has no multi-album id on this route."""
        payload = self._get("/track", params={"artistId": int(artist_id)}) or []
        return payload if isinstance(payload, list) else []

    def albums(self) -> list[dict[str, Any]]:
        return self._get("/album") or []

    def albums_for_artist(self, artist_id: int) -> list[dict[str, Any]]:
        return self._get("/album", params={"artistId": artist_id}) or []

    def artist_id_by_name(self, name: str) -> int | None:
        """Local artist id from Lidarr's lookup, not the full artist list."""
        wanted = (name or "").casefold().strip()
        if not wanted:
            return None
        payload = self._get("/search", params={"term": name}) or []
        if isinstance(payload, dict):
            payload = payload.get("results") or payload.get("records") or []
        if not isinstance(payload, list):
            return None
        for item in payload:
            if not isinstance(item, dict):
                continue
            artist = item.get("artist") if isinstance(item.get("artist"), dict) else item
            label = (artist.get("artistName") or item.get("artistName") or "").casefold().strip()
            artist_id = artist.get("id") or item.get("artistId")
            if label == wanted and artist_id:
                return int(artist_id)
        return None

    def artist_library(self, name: str) -> dict[str, Any] | None:
        """Albums and tracks for one artist. None when Lidarr does not answer.

        Uses the lookup and that artist's albums and tracks, not the full artist list.
        Each request is a single attempt so a finished import is not held up by retries.
        """
        wanted = (name or "").casefold().strip()
        if not wanted:
            return None
        payload = self._get_once("/search", {"term": name})
        if payload is None:
            return None
        if isinstance(payload, dict):
            payload = payload.get("results") or payload.get("records") or []
        artist_id = None
        if isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue
                artist = item.get("artist") if isinstance(item.get("artist"), dict) else item
                label = (artist.get("artistName") or item.get("artistName") or "").casefold().strip()
                found = artist.get("id") or item.get("artistId")
                if label == wanted and found:
                    artist_id = int(found)
                    break
        if not artist_id:
            return {"artist_id": None, "albums": [], "tracks": []}
        albums = self._get_once("/album", {"artistId": artist_id})
        if not isinstance(albums, list):
            return None
        tracks = self._get_once("/track", {"artistId": artist_id})
        if not isinstance(tracks, list):
            return None
        return {"artist_id": artist_id, "albums": albums, "tracks": tracks}

    def quality_profiles(self) -> list[dict[str, Any]]:
        payload = self._get("/qualityprofile") or []
        return payload if isinstance(payload, list) else []

    def missing_tracks_on_partial_albums(self) -> list[dict[str, Any]]:
        """Monitored tracks that have no file, on albums that already have at least one file.

        Uses the paged wanted/missing list instead of downloading every album.
        """
        holes: list[dict[str, Any]] = []
        albums, _next, _exhausted = self.wanted_missing(max_pages=20)
        for album in albums:
            try:
                if not isinstance(album, dict) or not album.get("monitored", True):
                    continue
                album_id = optional_id(album.get("id"))
                if album_id is None:
                    log.warning("Skipping album %s: no usable id", album.get("title") or "?")
                    continue
                stats = album.get("statistics") or {}
                files = count_value(stats.get("trackFileCount") if isinstance(stats, dict) else None)
                total = count_value(
                    (stats.get("totalTrackCount") or stats.get("trackCount")) if isinstance(stats, dict) else None
                )
                if files <= 0 or total <= 0 or files >= total:
                    continue
                artist = album.get("artist") or {}
                artist_name = (
                    artist.get("artistName") or album.get("artistName") or "Unknown Artist"
                    if isinstance(artist, dict)
                    else album.get("artistName") or "Unknown Artist"
                )
                title = album.get("title") or "Unknown Album"
                for track in self.tracks_for_album(album_id) or []:
                    if not isinstance(track, dict):
                        log.warning("Skipping a track on %s — %s that is not an object", artist_name, title)
                        continue
                    if not track.get("monitored", True) or track.get("hasFile"):
                        continue
                    if optional_id(track.get("id")) is None:
                        log.warning(
                            "Skipping %s on %s — %s: no usable track id",
                            track.get("trackNumber") or track.get("title") or "?",
                            artist_name,
                            title,
                        )
                        continue
                    holes.append(
                        {
                            "track": track,
                            "album": album,
                            "artist_name": artist_name,
                            "album_title": title,
                        }
                    )
            except Exception:
                log.exception("Skipping album %s while listing missing tracks", album.get("title") if isinstance(album, dict) else "?")
        return holes

    def scan_downloaded(self, path: str) -> Any:
        return self._post(
            "/command",
            {"name": "DownloadedAlbumsScan", "path": path, "importMode": "Move"},
        )

    def renames_on_import(self) -> bool | None:
        """True when Lidarr's own import already renames tracks.

        None means the media-management setting could not be read.
        """
        try:
            payload = self._get("/config/mediamanagement")
        except Exception:
            log.exception("Could not read Lidarr's rename-on-import setting")
            return None
        if not isinstance(payload, dict) or "renameTracks" not in payload:
            return None
        return bool(payload.get("renameTracks"))

    def import_failures(self, artist: str) -> list[str]:
        """Queue and history lines for this artist: track-count mismatch or an unmatched release.

        One attempt each. A host that is down does not turn a finished scan into a failure.
        """
        wanted = (artist or "").casefold().strip()
        if not wanted:
            return []
        found: list[str] = []
        queries = (
            ("/queue", {"page": 1, "pageSize": 50, "includeUnknownArtistItems": "true"}),
            ("/history", {"page": 1, "pageSize": 40, "sortKey": "date", "sortDirection": "descending"}),
        )
        for path, params in queries:
            try:
                response = self._client.get(f"/api/v1{path}", params=params, timeout=20.0)
                if response.status_code >= 400:
                    continue
                payload = response.json()
            except Exception:
                log.info("Could not read Lidarr %s after import", path)
                continue
            found.extend(_import_problem_lines(payload, wanted))
        return found

    def rename_artist_files(self, artist_id: int) -> Any:
        files = self._get("/trackFile", params={"artistId": artist_id}) or []
        ids = [int(f["id"]) for f in files if f.get("id")]
        if not ids:
            return None
        return self._post(
            "/command",
            {"name": "RenameFiles", "artistId": artist_id, "files": ids},
        )


_PROBLEM_WORDS = ("mismatch", "unmatched", "not match", "track count", "different number of tracks", "no files found")


def _texts_from(record: dict) -> list[str]:
    lines: list[str] = []
    for key in ("errorMessage", "status", "trackedDownloadStatus", "eventType", "sourceTitle", "title"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            lines.append(value.strip())
    data = record.get("data")
    if isinstance(data, dict):
        for key in ("message", "reason", "status"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                lines.append(value.strip())
    for item in record.get("statusMessages") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if title:
            lines.append(title)
        for message in item.get("messages") or []:
            if isinstance(message, str) and message.strip():
                lines.append(message.strip())
    return lines


def _names_artist(record: dict, artist: str) -> bool:
    blob = " ".join(
        [
            str(record.get("title") or ""),
            str(record.get("sourceTitle") or ""),
            str((record.get("artist") or {}).get("artistName") or "") if isinstance(record.get("artist"), dict) else "",
            str(record.get("artistName") or ""),
        ]
    ).casefold()
    return artist in blob


def _import_problem_lines(payload: Any, artist: str) -> list[str]:
    records = payload
    if isinstance(payload, dict):
        records = payload.get("records") or payload.get("events") or []
    if not isinstance(records, list):
        return []
    found: list[str] = []
    for record in records:
        if not isinstance(record, dict) or not _names_artist(record, artist):
            continue
        lines = _texts_from(record)
        blob = " ".join(lines).casefold()
        event = str(record.get("eventType") or "").casefold()
        status = str(record.get("trackedDownloadStatus") or record.get("status") or "").casefold()
        failed = event in {"downloadfailed", "albumimportincomplete", "trackfileimportfailed"} or status in {"warning", "error", "failed"}
        if not failed and not any(word in blob for word in _PROBLEM_WORDS):
            continue
        message = next((line for line in lines if any(word in line.casefold() for word in _PROBLEM_WORDS)), "")
        if not message:
            message = next((line for line in lines if line.casefold() not in {artist, status, event}), "")
        if not message:
            message = "import failed"
        if message not in found:
            found.append(message)
    return found
