from __future__ import annotations

import logging
from typing import Any

import httpx

from wanted import count_value, optional_id

log = logging.getLogger("lidarr_slsk")


class LidarrClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 60.0) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        r = self._client.get(f"/api/v1{path}", params=params)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        r = self._client.post(f"/api/v1{path}", json=payload)
        r.raise_for_status()
        return r.json() if r.content else None

    def ping(self) -> dict[str, Any]:
        r = self._client.get("/api/v1/system/status")
        r.raise_for_status()
        return r.json()

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
        return self._get("/track", params={"albumId": album_id}) or []

    def albums(self) -> list[dict[str, Any]]:
        return self._get("/album") or []

    def albums_for_artist(self, artist_id: int) -> list[dict[str, Any]]:
        return self._get("/album", params={"artistId": artist_id}) or []

    def artist_id_by_name(self, name: str) -> int | None:
        wanted = (name or "").casefold().strip()
        if not wanted:
            return None
        for artist in self._get("/artist") or []:
            label = (artist.get("artistName") or "").casefold().strip()
            if label == wanted:
                return int(artist["id"])
        return None

    def missing_tracks_on_partial_albums(self) -> list[dict[str, Any]]:
        """Monitored tracks that have no file, on albums that already have at least one file."""
        holes: list[dict[str, Any]] = []
        for album in self.albums():
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

    def rename_artist_files(self, artist_id: int) -> Any:
        files = self._get("/trackFile", params={"artistId": artist_id}) or []
        ids = [int(f["id"]) for f in files if f.get("id")]
        if not ids:
            return None
        return self._post(
            "/command",
            {"name": "RenameFiles", "artistId": artist_id, "files": ids},
        )
