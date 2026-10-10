from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path


class JobStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                album_id INTEGER PRIMARY KEY,
                artist TEXT,
                title TEXT,
                status TEXT NOT NULL,
                detail TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._db.commit()
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS track_jobs (
                track_id INTEGER PRIMARY KEY,
                album_id INTEGER,
                artist TEXT,
                title TEXT,
                status TEXT NOT NULL,
                detail TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._db.commit()
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        self._db.commit()
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS completed_downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                artist TEXT,
                title TEXT,
                filename TEXT,
                username TEXT,
                kind TEXT,
                finished_at TEXT NOT NULL
            )
            """
        )
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _execute(self, sql: str, params: tuple = ()):
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    def get(self, album_id: int) -> dict | None:
        row = self._db.execute(
            "SELECT album_id, artist, title, status, detail, updated_at FROM jobs WHERE album_id = ?",
            (album_id,),
        ).fetchone()
        if not row:
            return None
        keys = ("album_id", "artist", "title", "status", "detail", "updated_at")
        return dict(zip(keys, row))

    def upsert(self, album_id: int, artist: str, title: str, status: str, detail: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._db.execute(
            """
            INSERT INTO jobs(album_id, artist, title, status, detail, updated_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(album_id) DO UPDATE SET
                artist=excluded.artist,
                title=excluded.title,
                status=excluded.status,
                detail=excluded.detail,
                updated_at=excluded.updated_at
            """,
            (album_id, artist, title, status, detail, now),
        )
        self._db.commit()

    def should_skip(self, album_id: int, retry_hours: int) -> bool:
        job = self.get(album_id)
        if not job:
            return False
        if job["status"] in {"imported", "downloading"}:
            return True
        if job["status"] in {"failed", "no_match"}:
            try:
                updated = datetime.fromisoformat(job["updated_at"])
            except ValueError:
                return False
            return datetime.now(timezone.utc) - updated < timedelta(hours=retry_hours)
        return False

    def get_track(self, track_id: int) -> dict | None:
        row = self._db.execute(
            "SELECT track_id, album_id, artist, title, status, detail, updated_at FROM track_jobs WHERE track_id = ?",
            (track_id,),
        ).fetchone()
        if not row:
            return None
        keys = ("track_id", "album_id", "artist", "title", "status", "detail", "updated_at")
        return dict(zip(keys, row))

    def upsert_track(
        self, track_id: int, album_id: int, artist: str, title: str, status: str, detail: str = ""
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._db.execute(
            """
            INSERT INTO track_jobs(track_id, album_id, artist, title, status, detail, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(track_id) DO UPDATE SET
                album_id=excluded.album_id,
                artist=excluded.artist,
                title=excluded.title,
                status=excluded.status,
                detail=excluded.detail,
                updated_at=excluded.updated_at
            """,
            (track_id, album_id, artist, title, status, detail, now),
        )
        self._db.commit()

    def get_meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if not row else row[0]

    def set_meta(self, key: str, value: str | None) -> None:
        if value is None:
            self._db.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            self._db.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        self._db.commit()

    def focus_artist(self) -> str | None:
        value = self.get_meta("focus_artist")
        return value or None

    def set_focus_artist(self, artist: str | None) -> None:
        self.set_meta("focus_artist", artist)

    def artist_cooldowns(self) -> dict[str, str]:
        raw = self.get_meta("artist_cooldowns") or "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def artist_on_cooldown(self, artist: str) -> bool:
        key = (artist or "").casefold().strip()
        until = self.artist_cooldowns().get(key)
        if not until:
            return False
        try:
            stamp = datetime.fromisoformat(until)
        except ValueError:
            return False
        return datetime.now(timezone.utc) < stamp

    def set_artist_cooldown(self, artist: str, days: float) -> None:
        key = (artist or "").casefold().strip()
        if not key or days <= 0:
            return
        data = self.artist_cooldowns()
        until = datetime.now(timezone.utc) + timedelta(days=days)
        data[key] = until.isoformat()
        cutoff = datetime.now(timezone.utc)
        data = {
            name: stamp
            for name, stamp in data.items()
            if stamp and datetime.fromisoformat(stamp) > cutoff
        }
        self.set_meta("artist_cooldowns", json.dumps(data))

    def should_skip_track(self, track_id: int, retry_hours: int) -> bool:
        job = self.get_track(track_id)
        if not job:
            return False
        if job["status"] in {"imported", "downloading"}:
            return True
        if job["status"] in {"failed", "no_match"}:
            try:
                updated = datetime.fromisoformat(job["updated_at"])
            except ValueError:
                return False
            return datetime.now(timezone.utc) - updated < timedelta(hours=retry_hours)
        return False

    def add_completed_download(
        self, artist: str, title: str, filename: str, username: str, kind: str
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO completed_downloads(artist, title, filename, username, kind, finished_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (artist, title, filename, username, kind, now),
            )
            self._db.execute(
                """
                DELETE FROM completed_downloads
                WHERE id NOT IN (
                    SELECT id FROM completed_downloads ORDER BY id DESC LIMIT 50
                )
                """
            )
            self._db.commit()

    def recent_completed_downloads(self, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT artist, title, filename, username, kind, finished_at
                FROM completed_downloads
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        keys = ("artist", "title", "filename", "username", "kind", "finished_at")
        return [dict(zip(keys, row)) for row in rows]
