from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from favorites import GOOD_USER_MINIMUM, favorite_title, should_suggest
from search_order import (
    artist_display_key,
    artist_order_sql,
    missing_counts,
    normalize_search_direction,
    normalize_search_order,
    release_text,
    within_display_key,
)


# The status page can ask for up to 250 recent files, so keep at least that many.
COMPLETED_DOWNLOAD_LIMIT = 250

# A row is checked when its own flag matches the current generation, otherwise
# it follows the generation default. Select all bumps the generation instead of
# rewriting every row.
_EFFECTIVE_ON = "((enabled_gen = ? AND enabled = 1) OR (enabled_gen != ? AND ? = 1))"


def _effective(enabled, enabled_gen, kind: str, modes: dict) -> int:
    gen, default = modes.get(kind, (0, 1))
    if int(enabled_gen or 0) == int(gen):
        return 1 if enabled else 0
    return 1 if int(default) else 0


def _chunks(items, size: int):
    items = list(items)
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _cooldown_stamp(stamp: str) -> datetime | None:
    try:
        at = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at


class JobStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(path)
        self._catalog_ready = False
        self._import_choices = False
        self._sync_start = 0
        self._sync_inserted = 0
        self._sync_seen = 0
        self._sync_streamed = 0
        self._sync_artists: set[str] = set()
        self._needs_delete = False
        self._syncing = False
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA cache_size=-65536")
        self._db.execute("PRAGMA mmap_size=67108864")
        self._db.execute("PRAGMA busy_timeout=5000")
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
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS wanted_choices (
                key TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL
            )
            """
        )
        self._db.commit()
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS upload_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                filename TEXT NOT NULL,
                size INTEGER NOT NULL,
                status TEXT NOT NULL,
                finished_at TEXT NOT NULL
            )
            """
        )
        self._db.commit()
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_days (
                username TEXT NOT NULL,
                day TEXT NOT NULL,
                files INTEGER NOT NULL,
                PRIMARY KEY (username, day)
            )
            """
        )
        self._db.commit()
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                username TEXT NOT NULL,
                created_at TEXT NOT NULL,
                seen INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._db.commit()
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS private_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                body TEXT NOT NULL,
                direction TEXT NOT NULL,
                soulseek_id INTEGER,
                sent_at TEXT NOT NULL,
                seen INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._db.commit()
        self._ensure_catalog_schema()
        self._load_catalog_flag()

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
        self._drop_tried_display()

    def should_skip(self, album_id: int, retry_hours: int) -> bool:
        job = self.get(album_id)
        if not job:
            return False
        if job["status"] in {"imported", "downloading", "partial", "staged"}:
            return True
        if job["status"] in {"not_sent", "import_failed"}:
            return False
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
        self._drop_tried_display()

    def _drop_tried_display(self) -> None:
        """A new attempt changes who was tried least recently."""
        current = self.get_meta("display_order") or ""
        if current.startswith("tried|"):
            self.set_meta("display_order", "")

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
        """Active cooldowns keyed by the casefolded artist name.

        Older builds stored ``artist:`` plus that name. Those rows never matched
        the wanted list, so they never expired and the artist still looked ready.
        The prefix is stripped here, and a timestamp that has passed is dropped.
        """
        raw = self.get_meta("artist_cooldowns") or "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        now = datetime.now(timezone.utc)
        merged: dict[str, tuple[datetime, str]] = {}
        for key, stamp in data.items():
            token = (str(key) or "").casefold().strip()
            if token.startswith("artist:"):
                token = token[len("artist:") :].strip()
            if not token or not stamp:
                continue
            at = _cooldown_stamp(str(stamp))
            if at is None or at <= now:
                continue
            current = merged.get(token)
            if current is None or at > current[0]:
                merged[token] = (at, str(stamp))
        return {token: stamp for token, (_at, stamp) in merged.items()}

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

    def clear_artist_cooldown(self, artist: str) -> None:
        key = (artist or "").casefold().strip()
        if not key:
            return
        data = self.artist_cooldowns()
        if key not in data:
            return
        data.pop(key, None)
        self.set_meta("artist_cooldowns", json.dumps(data))

    def note_dry_run(self, detail: str) -> None:
        text = (detail or "").strip()
        if not text:
            return
        notes = self.dry_run_notes()
        if notes and notes[0] == text:
            return
        notes.insert(0, text)
        self.set_meta("dry_run_notes", json.dumps(notes[:50]))

    def dry_run_notes(self) -> list[str]:
        raw = self.get_meta("dry_run_notes") or "[]"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        return [str(item) for item in data if str(item).strip()]

    def wanted_albums(self) -> list[dict]:
        """Album payloads saved at Sync, including ones that already have some files."""
        with self._lock:
            rows = self._db.execute(
                """
                SELECT payload FROM wanted_rows
                WHERE kind = 'album' AND payload IS NOT NULL AND payload != ''
                """
            ).fetchall()
        albums = []
        for (payload,) in rows:
            data = _loads(payload)
            if isinstance(data, dict):
                albums.append(data)
        return albums

    def should_skip_track(self, track_id: int, retry_hours: int) -> bool:
        job = self.get_track(track_id)
        if not job:
            return False
        if job["status"] in {"imported", "downloading", "staged"}:
            return True
        if job["status"] in {"not_sent", "import_failed"}:
            return False
        if job["status"] in {"failed", "no_match"}:
            try:
                updated = datetime.fromisoformat(job["updated_at"])
            except ValueError:
                return False
            return datetime.now(timezone.utc) - updated < timedelta(hours=retry_hours)
        return False

    def rejected_usernames(self, album_id: int | None = None, artist: str = "") -> set[str]:
        raw = self.get_meta("rejected_sources") or "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return set()
        if not isinstance(data, dict):
            return set()
        names: set[str] = set()
        if album_id is not None:
            names.update(str(item).lower() for item in data.get(str(album_id)) or [])
        token = (artist or "").casefold().strip()
        if token:
            names.update(str(item).lower() for item in data.get("artist:" + token) or [])
        return {item for item in names if item}

    def reject_usernames(self, usernames: set[str], *, album_id: int | None = None, artist: str = "") -> None:
        raw = self.get_meta("rejected_sources") or "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        keys = []
        if album_id is not None:
            keys.append(str(album_id))
        token = (artist or "").casefold().strip()
        if token:
            keys.append("artist:" + token)
        for key in keys:
            current = {str(item).lower() for item in data.get(key) or [] if item}
            current.update(name.lower() for name in usernames if name)
            data[key] = sorted(current)
        self.set_meta("rejected_sources", json.dumps(data))

    def mark_import_failed(self, artist: str, detail: str) -> None:
        token = (artist or "").casefold().strip()
        if not token:
            return
        with self._lock:
            rows = self._db.execute(
                "SELECT album_id, artist, title FROM jobs WHERE lower(artist) = ?",
                (token,),
            ).fetchall()
        for album_id, name, title in rows:
            job = self.get(int(album_id))
            if not job or job["status"] not in {"imported", "downloading", "searching", "staged"}:
                continue
            self.upsert(int(album_id), name, title, "import_failed", detail)

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
                    SELECT id FROM completed_downloads ORDER BY id DESC LIMIT ?
                )
                """,
                (COMPLETED_DOWNLOAD_LIMIT,),
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

    def add_upload(self, username: str, filename: str, size: int, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO upload_history(username, filename, size, status, finished_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (username, filename, int(size or 0), status, now),
            )
            self._db.commit()

    def recent_uploads(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT username, filename, size, status, finished_at
                FROM upload_history
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        keys = ("username", "filename", "size", "status", "finished_at")
        return [dict(zip(keys, row)) for row in rows]

    def upload_totals(self) -> dict:
        """Successful uploads. Today is the local calendar day."""
        start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = start.astimezone(timezone.utc).isoformat()
        with self._lock:
            all_row = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM upload_history WHERE status = 'done'"
            ).fetchone()
            today_row = self._db.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(size), 0) FROM upload_history
                WHERE status = 'done' AND finished_at >= ?
                """,
                (start_utc,),
            ).fetchone()
        return {
            "all_files": int(all_row[0] or 0),
            "all_bytes": int(all_row[1] or 0),
            "today_files": int(today_row[0] or 0),
            "today_bytes": int(today_row[1] or 0),
        }

    def activity_summary(self) -> dict:
        """Finished downloads and album job counts for the Stats page."""
        with self._lock:
            jobs = self._db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()
            users = self._db.execute(
                """
                SELECT username, COUNT(*) FROM completed_downloads
                GROUP BY username
                ORDER BY COUNT(*) DESC, username
                LIMIT 8
                """
            ).fetchall()
            total = self._db.execute("SELECT COUNT(*) FROM completed_downloads").fetchone()
        return {
            "jobs": {str(status): int(count) for status, count in jobs},
            "download_total": int(total[0] or 0),
            "by_user": [(str(name or ""), int(count)) for name, count in users],
        }

    def add_user_day(self, username: str, files: int = 1) -> None:
        """Count finished files for this user on the local calendar day."""
        name = (username or "").strip().lower()
        count = int(files or 0)
        if not name or count <= 0:
            return
        day = datetime.now().astimezone().date().isoformat()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO user_days(username, day, files) VALUES(?, ?, ?)
                ON CONFLICT(username, day) DO UPDATE SET files = files + excluded.files
                """,
                (name, day, count),
            )
            self._db.commit()

    def user_day_files(self, username: str) -> int:
        name = (username or "").strip().lower()
        if not name:
            return 0
        day = datetime.now().astimezone().date().isoformat()
        with self._lock:
            row = self._db.execute(
                "SELECT files FROM user_days WHERE username = ? AND day = ?",
                (name, day),
            ).fetchone()
        return int(row[0] or 0) if row else 0

    def download_count(self, username: str) -> int:
        name = (username or "").strip().lower()
        if not name:
            return 0
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM completed_downloads WHERE lower(username) = ?",
                (name,),
            ).fetchone()
        return int(row[0] or 0)

    def add_event(self, kind: str, title: str, body: str = "", username: str = "") -> None:
        """Record a notice. An unseen notice with the same title is kept as-is."""
        title = (title or "").strip()
        if not title:
            return
        kind = (kind or "note").strip() or "note"
        user = (username or "").strip().lower()
        with self._lock:
            if user:
                existing = self._db.execute(
                    """
                    SELECT 1 FROM events
                    WHERE seen = 0 AND kind = ? AND username = ?
                    LIMIT 1
                    """,
                    (kind, user),
                ).fetchone()
            else:
                existing = None
            if existing is None:
                existing = self._db.execute(
                    "SELECT 1 FROM events WHERE seen = 0 AND title = ? LIMIT 1",
                    (title,),
                ).fetchone()
            if existing:
                return
            now = datetime.now(timezone.utc).isoformat()
            self._db.execute(
                """
                INSERT INTO events(kind, title, body, username, created_at, seen)
                VALUES(?, ?, ?, ?, ?, 0)
                """,
                (kind, title, body or "", user, now),
            )
            self._db.commit()

    def recent_events(self, limit: int = 8) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT id, kind, title, body, username, created_at
                FROM events
                WHERE seen = 0
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        keys = ("id", "kind", "title", "body", "username", "created_at")
        return [dict(zip(keys, row)) for row in rows]

    def dismiss_event(self, event_id: int) -> None:
        with self._lock:
            self._db.execute("UPDATE events SET seen = 1 WHERE id = ?", (int(event_id),))
            self._db.commit()

    def dismiss_user_events(self, username: str, kind: str = "") -> None:
        user = (username or "").strip().lower()
        if not user:
            return
        with self._lock:
            if kind:
                self._db.execute(
                    "UPDATE events SET seen = 1 WHERE seen = 0 AND username = ? AND kind = ?",
                    (user, kind),
                )
            else:
                self._db.execute(
                    "UPDATE events SET seen = 1 WHERE seen = 0 AND username = ?",
                    (user,),
                )
            self._db.commit()

    def dismissed_favorites(self) -> set[str]:
        raw = self.get_meta("dismissed_favorites") or "[]"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return set()
        if not isinstance(data, list):
            return set()
        return {str(item).strip().lower() for item in data if str(item).strip()}

    def dismiss_favorite(self, username: str) -> None:
        """Remember Not now. This user is not suggested again."""
        name = (username or "").strip().lower()
        if not name:
            return
        current = self.dismissed_favorites()
        current.add(name)
        self.set_meta("dismissed_favorites", json.dumps(sorted(current)))
        self.dismiss_user_events(name, "favorite")

    def note_good_user(self, username: str, favorites: set[str], minimum: int | None = None) -> None:
        """Suggest a favorite after enough finished files. Never edits the favorite list."""
        name = (username or "").strip()
        if not name:
            return
        floor = GOOD_USER_MINIMUM if minimum is None else int(minimum)
        count = self.download_count(name)
        if not should_suggest(name, count, favorites, self.dismissed_favorites(), floor):
            return
        self.add_event(
            "favorite",
            favorite_title(name),
            (
                f"{name} has finished {count} downloads. "
                "Add puts them in Priority users. Nothing is added until you choose Add."
            ),
            name,
        )

    def suggest_from_history(self, favorites: set[str], minimum: int | None = None) -> None:
        floor = GOOD_USER_MINIMUM if minimum is None else int(minimum)
        with self._lock:
            rows = self._db.execute(
                """
                SELECT username, COUNT(*)
                FROM completed_downloads
                WHERE trim(username) != ''
                GROUP BY lower(username)
                """
            ).fetchall()
        for username, count in rows:
            if int(count or 0) >= floor:
                self.note_good_user(str(username or ""), favorites, floor)

    def add_private_message(
        self,
        username: str,
        body: str,
        direction: str,
        soulseek_id: int | None = None,
        seen: int | None = None,
    ) -> bool:
        """Store one private message. An incoming Soulseek id is kept once."""
        name = (username or "").strip()
        text = (body or "").strip()
        if not name or not text:
            return False
        outgoing = str(direction or "").lower().startswith("o")
        kind = "out" if outgoing else "in"
        if seen is None:
            seen = 1 if outgoing else 0
        with self._lock:
            if not outgoing and soulseek_id is not None:
                existing = self._db.execute(
                    """
                    SELECT 1 FROM private_messages
                    WHERE direction = 'in' AND soulseek_id = ? AND lower(username) = ?
                    """,
                    (int(soulseek_id), name.lower()),
                ).fetchone()
                if existing:
                    return False
            now = datetime.now(timezone.utc).isoformat()
            self._db.execute(
                """
                INSERT INTO private_messages(username, body, direction, soulseek_id, sent_at, seen)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    text,
                    kind,
                    None if soulseek_id is None else int(soulseek_id),
                    now,
                    1 if seen else 0,
                ),
            )
            self._db.commit()
        return True

    def private_history(self, username: str, limit: int = 500) -> list[dict]:
        name = (username or "").strip().lower()
        if not name:
            return []
        with self._lock:
            rows = self._db.execute(
                """
                SELECT username, body, direction, sent_at, seen
                FROM private_messages
                WHERE lower(username) = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (name, max(1, int(limit))),
            ).fetchall()
        keys = ("username", "body", "direction", "sent_at", "seen")
        return [dict(zip(keys, row)) for row in rows]

    def private_threads(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT username,
                       SUM(CASE WHEN direction = 'in' AND seen = 0 THEN 1 ELSE 0 END),
                       MAX(id)
                FROM private_messages
                GROUP BY lower(username)
                ORDER BY MAX(id) DESC
                """
            ).fetchall()
            found = []
            for username, unread, last_id in rows:
                last = self._db.execute(
                    "SELECT body, sent_at FROM private_messages WHERE id = ?",
                    (last_id,),
                ).fetchone()
                found.append(
                    {
                        "username": str(username or ""),
                        "unread": int(unread or 0),
                        "last_body": str(last[0] if last else ""),
                        "sent_at": str(last[1] if last else ""),
                    }
                )
        return found

    def mark_thread_read(self, username: str) -> None:
        name = (username or "").strip().lower()
        if not name:
            return
        with self._lock:
            self._db.execute(
                """
                UPDATE private_messages
                SET seen = 1
                WHERE lower(username) = ? AND direction = 'in' AND seen = 0
                """,
                (name,),
            )
            self._db.commit()

    def unread_private_count(self) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM private_messages WHERE direction = 'in' AND seen = 0"
            ).fetchone()
        return int(row[0] or 0)

    def wanted_tracks_for(self, names: list[str], folds: list[str]) -> list[dict]:
        """Enabled album and song rows for these artists only.

        `names` match `artist_key` (casefold). `folds` match the accent-folded
        artist stored on the row, so Motley Crue finds Mötley Crüe. Other
        artists are not read.
        """
        tokens = sorted({("artist:" + (name or "").casefold().strip()) for name in names if (name or "").strip()})
        fold_keys = sorted({(fold or "").strip() for fold in folds if (fold or "").strip()})
        if not tokens and not fold_keys:
            return []
        found: list[dict] = []
        with self._lock:
            modes = self._stat_modes_locked()
            clauses = []
            params: list[str] = []
            if tokens:
                marks = ",".join("?" * len(tokens))
                clauses.append(f"artist_key IN ({marks})")
                params.extend(tokens)
            if fold_keys:
                marks = ",".join("?" * len(fold_keys))
                clauses.append(f"fold IN ({marks})")
                params.extend(fold_keys)
            sql = f"""
                SELECT key, kind, artist, album, song, album_id, track_id, artist_key,
                       enabled, enabled_gen
                FROM wanted_rows
                WHERE kind IN ('album', 'song') AND ({' OR '.join(clauses)})
            """
            rows = self._db.execute(sql, params).fetchall()
        for key, kind, artist, album, song, album_id, track_id, artist_key, enabled, enabled_gen in rows:
            if not _effective(enabled, enabled_gen, kind, modes):
                continue
            found.append(
                {
                    "key": key,
                    "kind": kind,
                    "artist": artist,
                    "album": album or "",
                    "song": song or "",
                    "album_id": album_id,
                    "track_id": track_id,
                    "artist_key": artist_key,
                }
            )
        return found

    def wanted_enabled(self, key: str) -> bool:
        with self._lock:
            if self._catalog_ready:
                row = self._db.execute(
                    "SELECT enabled, enabled_gen, kind FROM wanted_rows WHERE key = ?",
                    (key,),
                ).fetchone()
                if row is not None:
                    return bool(_effective(row[0], row[1], row[2], self._stat_modes_locked()))
            row = self._db.execute("SELECT enabled FROM wanted_choices WHERE key = ?", (key,)).fetchone()
        if row is None:
            return True
        return bool(row[0])

    def wanted_choices(self) -> dict[str, bool]:
        with self._lock:
            rows = self._db.execute("SELECT key, enabled FROM wanted_choices").fetchall()
        return {key: bool(enabled) for key, enabled in rows}

    def set_wanted_choices(self, updates: dict[str, bool]) -> None:
        if not updates:
            return
        with self._lock:
            self._db.executemany(
                """
                INSERT INTO wanted_choices(key, enabled) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET enabled=excluded.enabled
                """,
                [(key, 1 if enabled else 0) for key, enabled in updates.items()],
            )
            if self._catalog_ready:
                self._overlay_choices_locked(updates)
            self._db.commit()

    def remember_wanted_keys(self, keys: list[str]) -> None:
        """Record newly seen wanted rows as checked. Existing choices are kept."""
        fresh = [(key,) for key in keys if key]
        if not fresh:
            return
        with self._lock:
            self._db.executemany(
                "INSERT INTO wanted_choices(key, enabled) VALUES(?, 1) ON CONFLICT(key) DO NOTHING",
                fresh,
            )
            self._db.commit()

    def database_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix) if suffix else self.path
            if candidate.exists():
                total += candidate.stat().st_size
        return total

    def checkpoint(self) -> None:
        with self._lock:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def wanted_catalog_ready(self) -> bool:
        return self._catalog_ready

    def wanted_row_count(self) -> int:
        with self._lock:
            return self._row_count_locked()

    def wanted_kind_count(self, kind: str) -> int:
        with self._lock:
            row = self._db.execute("SELECT total FROM wanted_stats WHERE kind = ?", (kind,)).fetchone()
        return int(row[0]) if row else 0

    def begin_wanted_sync(self) -> None:
        with self._lock:
            self._sync_start = self._row_count_locked()
            self._sync_inserted = 0
            self._sync_seen = 0
            self._sync_streamed = 0
            self._sync_artists = set()
            self._needs_delete = False
            self._import_choices = self._sync_start == 0
            self._syncing = True
            self._db.commit()

    def write_wanted_page(self, rows: list[dict]) -> None:
        if not rows:
            return
        with self._lock:
            unique: dict[str, dict] = {}
            for row in rows:
                unique[row["key"]] = row
            rows = list(unique.values())
            existing: dict[str, str] = {}
            keys = [row["key"] for row in rows]
            for chunk in _chunks(keys, 400):
                marks = ",".join("?" * len(chunk))
                for key, digest in self._db.execute(
                    f"SELECT key, content_hash FROM wanted_rows WHERE key IN ({marks})",
                    chunk,
                ):
                    existing[key] = digest
            fresh = [row for row in rows if row["key"] not in existing]
            changed = [row for row in rows if row["key"] in existing and existing[row["key"]] != row["content_hash"]]
            modes = self._stat_modes_locked()
            for row in fresh:
                keep = row.pop("keep_choice", None)
                row["enabled_gen"] = modes.get(row["kind"], (0, 1))[0]
                if keep is not None:
                    row["enabled"], row["enabled_gen"] = keep
            if fresh and self._import_choices:
                self._apply_saved_choices_locked(fresh)
            if fresh:
                self._db.executemany(_INSERT_WANTED, [_row_tuple(row) for row in fresh])
                self._note_new_rows_locked(fresh)
                self._sync_inserted += len(fresh)
            if changed:
                self._db.executemany(_UPDATE_WANTED, [_update_tuple(row) for row in changed])
            digest_rows = [
                (row.get("sync_digest"), row["key"])
                for row in rows
                if row["kind"] == "album" and row.get("sync_digest") and row["key"] in existing
            ]
            if digest_rows:
                self._db.executemany(
                    "UPDATE wanted_rows SET sync_digest = ? WHERE key = ?",
                    digest_rows,
                )
            # Album and song keys are unique. An artist row repeats when that
            # artist spans two Lidarr pages, so count each artist key once.
            streamed = 0
            for row in rows:
                if row["kind"] == "artist":
                    if row["key"] in self._sync_artists:
                        continue
                    self._sync_artists.add(row["key"])
                streamed += 1
            self._sync_streamed += streamed
            self._mark_catalog_locked()
            self._db.commit()

    def finish_wanted_sync(self, *, complete: bool) -> int:
        with self._lock:
            # Album removal is decided from wanted_sync_seen after a full page
            # walk. This flag stays clear so a resync does not start a second
            # Lidarr pass.
            self._needs_delete = False
            self._sync_artists = set()
            count = self._row_count_locked()
            self._set_meta_locked("wanted_row_count", str(count))
            if count:
                self._mark_catalog_locked()
            else:
                self._catalog_ready = False
                self._set_meta_locked("wanted_catalog", None)
            self._syncing = False
            self._db.commit()
            return count

    def begin_delete_pass(self) -> None:
        with self._lock:
            self._db.execute("CREATE TEMP TABLE IF NOT EXISTS sync_seen (key TEXT PRIMARY KEY)")
            self._db.execute("DELETE FROM sync_seen")
            self._db.commit()

    def note_seen_keys(self, keys: list[str]) -> None:
        if not keys:
            return
        with self._lock:
            self._db.executemany(
                "INSERT INTO sync_seen(key) VALUES (?) ON CONFLICT(key) DO NOTHING",
                [(key,) for key in keys if key],
            )
            self._db.commit()

    def finish_delete_pass(self) -> int:
        with self._lock:
            self._db.execute(
                """
                DELETE FROM wanted_rows
                WHERE NOT EXISTS (SELECT 1 FROM sync_seen s WHERE s.key = wanted_rows.key)
                """
            )
            self._rebuild_derived_locked()
            self._db.execute("DROP TABLE IF EXISTS sync_seen")
            self._needs_delete = False
            count = self._row_count_locked()
            self._set_meta_locked("wanted_row_count", str(count))
            if count:
                self._mark_catalog_locked()
            else:
                self._catalog_ready = False
                self._set_meta_locked("wanted_catalog", None)
            self._db.commit()
            return count

    def abort_delete_pass(self) -> None:
        with self._lock:
            self._db.execute("DROP TABLE IF EXISTS sync_seen")
            self._needs_delete = False
            self._db.commit()

    def prepare_catalog_sync(self) -> tuple[str, int]:
        """Choose a fresh walk, a resumed page walk, or a resumed track fetch.

        A finished sync starts again at page 1. A cancelled or crashed sync
        keeps the albums already stored and continues at the next page, or at
        the track queue when every page was already saved.
        """
        with self._lock:
            phase = self._meta_locked("wanted_sync_phase") or "done"
            raw_page = self._meta_locked("wanted_sync_next_page") or "1"
            try:
                page = max(1, int(raw_page))
            except ValueError:
                page = 1
            if phase == "tracks":
                self._begin_sync_counters_locked()
                self._db.commit()
                return "tracks", page
            if phase == "pages":
                self._begin_sync_counters_locked()
                self._db.commit()
                return "pages", page
            self._db.execute("DELETE FROM wanted_sync_seen")
            self._db.execute("DELETE FROM wanted_track_queue")
            self._set_meta_locked("wanted_sync_phase", "pages")
            self._set_meta_locked("wanted_sync_next_page", "1")
            self._set_meta_locked("wanted_sync_full", "1")
            self._begin_sync_counters_locked()
            self._db.commit()
            return "pages", 1

    def checkpoint_sync_page(self, next_page: int) -> None:
        with self._lock:
            self._set_meta_locked("wanted_sync_phase", "pages")
            self._set_meta_locked("wanted_sync_next_page", str(max(1, int(next_page))))
            self._db.commit()

    def mark_track_phase(self) -> None:
        with self._lock:
            self._set_meta_locked("wanted_sync_phase", "tracks")
            self._db.commit()

    def mark_sync_done(self) -> None:
        with self._lock:
            self._set_meta_locked("wanted_sync_phase", "done")
            self._set_meta_locked("wanted_sync_next_page", "1")
            self._db.execute("DELETE FROM wanted_sync_seen")
            self._db.execute("DELETE FROM wanted_track_queue")
            self._db.commit()

    def sync_walk_is_full(self) -> bool:
        with self._lock:
            return self._meta_locked("wanted_sync_full") == "1"

    def note_sync_albums(self, album_ids: list[int]) -> None:
        ids = [(int(album_id),) for album_id in album_ids if album_id is not None]
        if not ids:
            return
        with self._lock:
            self._db.executemany(
                "INSERT INTO wanted_sync_seen(album_id) VALUES (?) ON CONFLICT(album_id) DO NOTHING",
                ids,
            )
            self._db.commit()

    def queue_albums_for_tracks(self, rows: list[tuple[int, int | None, str]]) -> None:
        if not rows:
            return
        with self._lock:
            self._db.executemany(
                """
                INSERT INTO wanted_track_queue(album_id, artist_id, page_fingerprint)
                VALUES (?, ?, ?)
                ON CONFLICT(album_id) DO UPDATE SET
                    artist_id = excluded.artist_id,
                    page_fingerprint = excluded.page_fingerprint
                """,
                [(int(album_id), artist_id, fingerprint) for album_id, artist_id, fingerprint in rows],
            )
            self._db.commit()

    def matching_page_fingerprints(self, fingerprints: dict[str, str]) -> dict[str, int]:
        """Album keys whose stored wanted/missing fingerprint still matches.

        The value is the number of song rows already stored for that album.
        """
        if not fingerprints:
            return {}
        found: dict[str, int] = {}
        with self._lock:
            for chunk in _chunks(list(fingerprints), 400):
                marks = ",".join("?" * len(chunk))
                for key, stored, track_rows in self._db.execute(
                    f"""
                    SELECT key, page_fingerprint, track_rows
                    FROM wanted_rows
                    WHERE kind = 'album' AND key IN ({marks})
                    """,
                    chunk,
                ):
                    if stored and stored == fingerprints.get(key):
                        found[key] = int(track_rows or 0)
        return found

    def track_queue_pending(self) -> bool:
        with self._lock:
            row = self._db.execute("SELECT 1 FROM wanted_track_queue LIMIT 1").fetchone()
        return row is not None

    def next_queued_albums(
        self,
        artist_limit: int = 200,
        skip_artists: set[int] | None = None,
        include_unassigned: bool = True,
    ) -> list[dict]:
        """Albums waiting for a track lookup, grouped so one artist stays together."""
        with self._lock:
            if skip_artists:
                self._db.execute("CREATE TEMP TABLE IF NOT EXISTS track_skip (artist_id INTEGER PRIMARY KEY)")
                self._db.execute("DELETE FROM track_skip")
                self._db.executemany(
                    "INSERT INTO track_skip(artist_id) VALUES (?)",
                    [(int(artist_id),) for artist_id in skip_artists],
                )
                artist_rows = self._db.execute(
                    """
                    SELECT DISTINCT queue.artist_id
                    FROM wanted_track_queue AS queue
                    WHERE queue.artist_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM track_skip AS skip
                          WHERE skip.artist_id = queue.artist_id
                      )
                    LIMIT ?
                    """,
                    (max(1, int(artist_limit)),),
                ).fetchall()
            else:
                artist_rows = self._db.execute(
                    """
                    SELECT DISTINCT artist_id FROM wanted_track_queue
                    WHERE artist_id IS NOT NULL
                    LIMIT ?
                    """,
                    (max(1, int(artist_limit)),),
                ).fetchall()
            artist_ids = [row[0] for row in artist_rows]
            queued: list[tuple] = []
            if artist_ids:
                marks = ",".join("?" * len(artist_ids))
                queued.extend(
                    self._db.execute(
                        f"""
                        SELECT album_id, artist_id, page_fingerprint
                        FROM wanted_track_queue
                        WHERE artist_id IN ({marks})
                        """,
                        artist_ids,
                    ).fetchall()
                )
            elif include_unassigned:
                queued.extend(
                    self._db.execute(
                        """
                        SELECT album_id, artist_id, page_fingerprint
                        FROM wanted_track_queue
                        WHERE artist_id IS NULL
                        LIMIT ?
                        """,
                        (max(1, int(artist_limit)),),
                    ).fetchall()
                )
            if not queued:
                return []
            album_ids = [row[0] for row in queued]
            stored: dict[int, tuple] = {}
            for chunk in _chunks(album_ids, 400):
                marks = ",".join("?" * len(chunk))
                for album_id, artist, title, payload in self._db.execute(
                    f"""
                    SELECT album_id, artist, album, payload
                    FROM wanted_rows
                    WHERE kind = 'album' AND album_id IN ({marks})
                    """,
                    chunk,
                ):
                    stored[int(album_id)] = (artist, title, payload)
        albums = []
        for album_id, artist_id, fingerprint in queued:
            artist, title, payload = stored.get(int(album_id), ("", "", None))
            albums.append(
                {
                    "album_id": int(album_id),
                    "artist_id": None if artist_id is None else int(artist_id),
                    "fingerprint": fingerprint,
                    "artist": artist or "",
                    "title": title or "",
                    "payload": _loads(payload),
                }
            )
        return albums

    def replace_artist_wanted(self, artist: str, rows: list[dict]) -> None:
        """Replace one artist's saved wanted rows. Other artists stay.

        Does nothing until Sync has stored a catalog, so a one-artist refresh
        cannot replace the live Lidarr list with a single artist.
        """
        token = (artist or "").casefold().strip()
        if not token or not self._catalog_ready:
            return
        with self._lock:
            saved = {
                key: (int(enabled), int(enabled_gen or 0))
                for key, enabled, enabled_gen in self._db.execute(
                    "SELECT key, enabled, enabled_gen FROM wanted_rows WHERE artist_key = ?",
                    (token,),
                )
            }
            album_ids = [
                int(album_id)
                for (album_id,) in self._db.execute(
                    """
                    SELECT DISTINCT album_id FROM wanted_rows
                    WHERE artist_key = ? AND album_id IS NOT NULL
                    """,
                    (token,),
                )
            ]
            self._db.execute("DELETE FROM wanted_rows WHERE artist_key = ?", (token,))
            if album_ids:
                marks = ",".join("?" * len(album_ids))
                self._db.execute(
                    f"DELETE FROM wanted_track_queue WHERE album_id IN ({marks})",
                    album_ids,
                )
        prepared = []
        for row in rows:
            item = dict(row)
            previous = saved.get(item["key"])
            if previous is not None:
                item["keep_choice"] = previous
            prepared.append(item)
        if prepared:
            self.write_wanted_page(prepared)
        with self._lock:
            self._rebuild_derived_locked()
            self._db.commit()

    def replace_album_songs(self, album_id: int, rows: list[dict], fingerprint: str) -> None:
        """Replace one album's song rows and remember the wanted/missing fingerprint."""
        with self._lock:
            modes = self._stat_modes_locked()
            existing = self._db.execute(
                """
                SELECT key, artist_key, enabled, enabled_gen
                FROM wanted_rows
                WHERE kind = 'song' AND album_id = ?
                """,
                (int(album_id),),
            ).fetchall()
            saved = {
                key: (int(enabled), int(enabled_gen or 0))
                for key, _token, enabled, enabled_gen in existing
            }
            per_artist: dict[str, list[int]] = {}
            removed_on = 0
            for _key, token, enabled, enabled_gen in existing:
                on = _effective(enabled, enabled_gen, "song", modes)
                slot = per_artist.setdefault(token, [0, 0])
                slot[0] += 1
                if on:
                    removed_on += 1
                else:
                    slot[1] += 1
            if existing:
                self._db.execute(
                    "DELETE FROM wanted_rows WHERE kind = 'song' AND album_id = ?",
                    (int(album_id),),
                )
                # These songs were already in the sync's starting total. Drop them
                # from that base so the inserted replacements are not counted twice.
                self._sync_start = max(0, int(self._sync_start) - len(existing))
                self._mark_catalog_locked()
                self._db.execute(
                    """
                    UPDATE wanted_stats
                    SET total = MAX(0, total - ?), enabled = MAX(0, enabled - ?)
                    WHERE kind = 'song'
                    """,
                    (len(existing), removed_on),
                )
                for token, (songs, disabled) in per_artist.items():
                    self._db.execute(
                        """
                        UPDATE wanted_artists SET
                            songs = MAX(0, songs - ?),
                            disabled_songs = MAX(0, disabled_songs - ?),
                            disabled_children = MAX(0, disabled_children - ?)
                        WHERE artist_key = ?
                        """,
                        (songs, disabled, disabled, token),
                    )
            for row in rows:
                previous = saved.get(row["key"])
                if previous is not None:
                    row["keep_choice"] = previous
            if rows:
                self.write_wanted_page(rows)
            self._db.execute(
                """
                UPDATE wanted_rows
                SET page_fingerprint = ?, track_rows = ?
                WHERE kind = 'album' AND album_id = ?
                """,
                (fingerprint, len(rows), int(album_id)),
            )
            self._db.execute("DELETE FROM wanted_track_queue WHERE album_id = ?", (int(album_id),))
            token_row = self._db.execute(
                "SELECT artist_key FROM wanted_rows WHERE kind = 'album' AND album_id = ?",
                (int(album_id),),
            ).fetchone()
            if token_row is not None:
                self._refresh_artist_details_locked([token_row[0]])
            self._db.commit()

    def delete_albums_not_seen(self) -> int:
        """Drop albums missing from a finished page walk. No Lidarr requests."""
        with self._lock:
            missing = self._db.execute(
                """
                SELECT COUNT(*) FROM wanted_rows AS album
                WHERE album.kind = 'album'
                  AND album.album_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM wanted_sync_seen AS seen
                      WHERE seen.album_id = album.album_id
                  )
                """
            ).fetchone()
            if not missing or int(missing[0]) <= 0:
                self._db.commit()
                return 0
            self._db.execute(
                """
                DELETE FROM wanted_rows
                WHERE kind = 'song' AND album_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM wanted_sync_seen AS seen
                      WHERE seen.album_id = wanted_rows.album_id
                  )
                """
            )
            self._db.execute(
                """
                DELETE FROM wanted_rows
                WHERE kind = 'album' AND album_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM wanted_sync_seen AS seen
                      WHERE seen.album_id = wanted_rows.album_id
                  )
                """
            )
            self._db.execute(
                """
                DELETE FROM wanted_rows
                WHERE kind = 'artist'
                  AND artist_key NOT IN (
                      SELECT artist_key FROM wanted_rows WHERE kind IN ('album', 'song')
                  )
                """
            )
            self._rebuild_derived_locked()
            self._db.commit()
            return int(missing[0])

    def _begin_sync_counters_locked(self) -> None:
        self._sync_start = self._row_count_locked()
        self._sync_inserted = 0
        self._sync_seen = 0
        self._sync_streamed = 0
        self._sync_artists = set()
        self._needs_delete = False
        self._import_choices = self._sync_start == 0
        self._syncing = True

    def unchanged_album_keys(self, digests: dict[str, str]) -> set[str]:
        if not digests:
            return set()
        found: set[str] = set()
        with self._lock:
            for chunk in _chunks(list(digests), 400):
                marks = ",".join("?" * len(chunk))
                for key, digest in self._db.execute(
                    f"SELECT key, sync_digest FROM wanted_rows WHERE key IN ({marks})",
                    chunk,
                ):
                    if digest and digest == digests.get(key):
                        found.add(key)
        return found

    def note_unchanged_keys(self, artist_keys: list[str], album_count: int, song_count: int) -> None:
        with self._lock:
            extra = 0
            for key in artist_keys:
                if not key or key in self._sync_artists:
                    continue
                self._sync_artists.add(key)
                extra += 1
            self._sync_streamed += int(album_count) + int(song_count) + extra

    def note_seen_unchanged(self, album_ids: list[int], artist_keys: list[str]) -> None:
        if not album_ids and not artist_keys:
            return
        with self._lock:
            for chunk in _chunks(list(album_ids), 400):
                marks = ",".join("?" * len(chunk))
                self._db.execute(
                    f"""
                    INSERT INTO sync_seen(key)
                    SELECT key FROM wanted_rows
                    WHERE album_id IN ({marks}) AND kind IN ('album', 'song')
                    ON CONFLICT(key) DO NOTHING
                    """,
                    chunk,
                )
            unique_artists = [key for key in dict.fromkeys(artist_keys) if key]
            for chunk in _chunks(unique_artists, 400):
                marks = ",".join("?" * len(chunk))
                self._db.execute(
                    f"""
                    INSERT INTO sync_seen(key)
                    SELECT key FROM wanted_rows
                    WHERE key IN ({marks}) AND kind = 'artist'
                    ON CONFLICT(key) DO NOTHING
                    """,
                    chunk,
                )
            self._db.commit()

    def wanted_page(
        self,
        *,
        kinds: tuple[str, ...] | None,
        search: str,
        order: str,
        reverse: bool,
        limit: int,
        offset: int,
        after: tuple | None = None,
        before: tuple | None = None,
        before_count: int = 0,
    ) -> tuple[list[tuple], int]:
        with self._lock:
            columns = self._order_columns(order, kinds)
            if before is not None and before_count > 0 and not reverse:
                older = self._fetch_page_locked(
                    kinds, search, columns, reverse=True, limit=int(before_count), offset=0, cursor=before, op="<"
                )
                older.reverse()
                if older:
                    after = tuple(older[0][9 : 9 + len(columns)])
                    offset = 0
                else:
                    after = None
                    offset = 0
            cursor_op = None
            if after is not None and not reverse:
                cursor_op = ">=" if before is not None else ">"
                offset = 0
            fetched = self._fetch_page_locked(
                kinds,
                search,
                columns,
                reverse=reverse,
                limit=int(limit),
                offset=0 if cursor_op else int(offset),
                cursor=after if cursor_op else None,
                op=cursor_op or ">",
            )
            where, params = self._where_locked(kinds, search)
            total = self._count_locked(kinds, search, where, params)
            tokens = [row[8] for row in fetched if row[1] == "artist"]
            artists: dict[str, tuple] = {}
            modes = self._stat_modes_locked()
            if tokens:
                marks = ",".join("?" * len(tokens))
                for key, albums, songs, disabled, enabled in self._db.execute(
                    f"""
                    SELECT artist_key, albums, songs, disabled_children, enabled
                    FROM wanted_artists WHERE artist_key IN ({marks})
                    """,
                    tokens,
                ):
                    artists[key] = (albums, songs, disabled, enabled)
        page = []
        width = len(columns)
        for row in fetched:
            key, kind, artist, album, song, detail, enabled, enabled_gen, token = row[:9]
            cursor = tuple(row[9 : 9 + width])
            shown = _effective(enabled, enabled_gen, kind, modes)
            state = "on" if shown else "off"
            if kind == "artist":
                info = artists.get(token)
                if info is not None and info[0] + info[1] > 0:
                    disabled = int(info[2])
                    children = int(info[0]) + int(info[1])
                    if disabled <= 0:
                        state = "on"
                    elif disabled >= children:
                        state = "off"
                    else:
                        state = "mixed"
            page.append((key, kind, artist, album, song, detail or "", state, cursor))
        return page, total

    def wanted_summary(self, kinds: tuple[str, ...] | None, search: str) -> tuple[int, int, str]:
        with self._lock:
            where, params = self._where_locked(kinds, search)
            total = self._count_locked(kinds, search, where, params)
            if total <= 0:
                return 0, 0, "empty"
            checked = self._checked_locked(kinds, search)
        if checked >= total:
            return total, total, "all"
        if checked <= 0:
            return total, 0, "none"
        return total, checked, "some"

    def set_view_enabled(self, kinds: tuple[str, ...] | None, search: str, enabled: bool) -> None:
        flag = 1 if enabled else 0
        with self._lock:
            search_sql, search_params = self._search_sql(search)
            targets = ("artist", "album", "song") if kinds is None or kinds == ("artist",) else kinds
            if not search_sql:
                for kind in targets:
                    self._bump_kind_locked(kind, flag)
                if targets == ("artist", "album", "song") or set(targets) >= {"album", "song"}:
                    self._db.execute(
                        """
                        UPDATE wanted_artists SET
                            enabled = ?,
                            disabled_albums = CASE WHEN ? = 1 THEN 0 ELSE albums END,
                            disabled_songs = CASE WHEN ? = 1 THEN 0 ELSE songs END,
                            disabled_children = CASE WHEN ? = 1 THEN 0 ELSE albums + songs END
                        """,
                        (flag, flag, flag, flag),
                    )
                elif targets == ("album",):
                    self._db.execute(
                        """
                        UPDATE wanted_artists SET
                            disabled_children = MAX(0, disabled_children - disabled_albums + CASE WHEN ? = 1 THEN 0 ELSE albums END),
                            disabled_albums = CASE WHEN ? = 1 THEN 0 ELSE albums END
                        """,
                        (flag, flag),
                    )
                elif targets == ("song",):
                    self._db.execute(
                        """
                        UPDATE wanted_artists SET
                            disabled_children = MAX(0, disabled_children - disabled_songs + CASE WHEN ? = 1 THEN 0 ELSE songs END),
                            disabled_songs = CASE WHEN ? = 1 THEN 0 ELSE songs END
                        """,
                        (flag, flag),
                    )
                self._db.commit()
                return
            for kind in targets:
                self._set_matching_kind_locked(kind, flag, search_sql, search_params)
            self._db.commit()

    def set_artist_enabled(self, token: str, enabled: bool) -> None:
        flag = 1 if enabled else 0
        with self._lock:
            grouped = self._db.execute(
                "SELECT kind, enabled, COUNT(*) FROM wanted_rows WHERE artist_key = ? GROUP BY kind, enabled",
                (token,),
            ).fetchall()
            if not grouped:
                self._db.commit()
                return
            modes = self._stat_modes_locked()
            seen_kinds = {kind for kind, _enabled, _count in grouped}
            for kind in seen_kinds:
                gen, default = modes.get(kind, (0, 1))
                total = int(
                    self._db.execute(
                        "SELECT COUNT(*) FROM wanted_rows WHERE artist_key = ? AND kind = ?",
                        (token, kind),
                    ).fetchone()[0]
                )
                on = int(
                    self._db.execute(
                        f"""
                        SELECT COUNT(*) FROM wanted_rows
                        WHERE artist_key = ? AND kind = ? AND {_EFFECTIVE_ON}
                        """,
                        (token, kind, gen, gen, default),
                    ).fetchone()[0]
                )
                target = total if flag else 0
                if target != on:
                    self._db.execute(
                        "UPDATE wanted_stats SET enabled = MAX(0, enabled + ?) WHERE kind = ?",
                        (target - on, kind),
                    )
                self._db.execute(
                    "UPDATE wanted_rows SET enabled = ?, enabled_gen = ? WHERE artist_key = ? AND kind = ?",
                    (flag, gen, token, kind),
                )
            info = self._db.execute(
                "SELECT albums, songs FROM wanted_artists WHERE artist_key = ?",
                (token,),
            ).fetchone()
            albums = int(info[0]) if info else 0
            songs = int(info[1]) if info else 0
            self._db.execute(
                """
                UPDATE wanted_artists
                SET enabled = ?,
                    disabled_albums = ?,
                    disabled_songs = ?,
                    disabled_children = ?
                WHERE artist_key = ?
                """,
                (flag, 0 if flag else albums, 0 if flag else songs, 0 if flag else albums + songs, token),
            )
            self._db.execute(
                """
                INSERT INTO wanted_choices(key, enabled) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET enabled = excluded.enabled
                """,
                ("artist:" + token, flag),
            )
            self._db.commit()

    def next_wanted_artist(self, skip: set[str] | None = None, *, tag: str = "") -> str | None:
        """Next artist with work left. Monitored and recently added come first.

        ``tag`` keeps artists whose album payload lists that Lidarr tag. Blank keeps everyone.
        """
        skip = {(item or "").casefold().strip() for item in (skip or set())}
        wanted_tag = (tag or "").casefold().strip()
        with self._lock:
            rows = self._db.execute(
                """
                SELECT a.artist, a.artist_key,
                       IFNULL(MAX(json_extract(r.payload, '$.added')), '') AS added,
                       MAX(CASE
                           WHEN json_extract(r.payload, '$.monitored') IN (0, 'false') THEN 0
                           ELSE 1
                       END) AS monitored
                FROM wanted_artists a
                LEFT JOIN wanted_rows r ON r.artist_key = a.artist_key AND r.kind = 'album'
                WHERE a.disabled_children < (a.albums + a.songs)
                GROUP BY a.artist_key
                ORDER BY monitored DESC,
                         CASE WHEN added = '' THEN 1 ELSE 0 END,
                         added DESC,
                         a.sort_artist
                """
            ).fetchall()
            for artist, token, _added, _monitored in rows:
                if token in skip:
                    continue
                if wanted_tag and not self._artist_has_tag_locked(token, wanted_tag):
                    continue
                return artist
        return None

    def saved_tracks(self, album_id: int) -> list[dict] | None:
        """Track rows saved at Sync. None when this album has no saved tracks."""
        with self._lock:
            rows = self._db.execute(
                """
                SELECT track_json FROM wanted_rows
                WHERE album_id = ? AND kind = 'song' AND track_json IS NOT NULL AND track_json != ''
                """,
                (int(album_id),),
            ).fetchall()
        tracks = []
        for (payload,) in rows:
            data = _loads(payload)
            if isinstance(data, dict) and data.get("id") is not None:
                tracks.append(data)
        return tracks or None

    def next_ready_artist(
        self,
        skip: set[str] | None = None,
        *,
        tag: str = "",
        retry_before: str = "",
        order: str = "lidarr",
        direction: str = "desc",
    ) -> str | None:
        """The first artist who can be searched now, in one query.

        ``order`` and ``direction`` choose who comes first. The skip set is
        cooled or deferred artists. ``retry_before`` is the ISO timestamp before
        which a failed or no-match job may be searched again. The tag filter is
        unchanged.
        """
        self._ensure_artist_priority()
        order_sql = artist_order_sql(order, direction)
        skip_tokens = sorted({(item or "").casefold().strip() for item in (skip or set()) if item})
        wanted_tag = (tag or "").casefold().strip()
        cutoff = retry_before or "9999"
        with self._lock:
            self._db.execute("CREATE TEMP TABLE IF NOT EXISTS ready_skip (token TEXT PRIMARY KEY)")
            self._db.execute("DELETE FROM ready_skip")
            if skip_tokens:
                self._db.executemany(
                    "INSERT OR IGNORE INTO ready_skip(token) VALUES (?)",
                    [(token,) for token in skip_tokens],
                )
            row = self._db.execute(
                f"""
                SELECT a.artist
                FROM wanted_artists a
                LEFT JOIN (
                    SELECT lower(artist) AS token, MAX(updated_at) AS tried
                    FROM (
                        SELECT artist, updated_at FROM jobs
                        UNION ALL
                        SELECT artist, updated_at FROM track_jobs
                    )
                    GROUP BY lower(artist)
                ) tried ON tried.token = a.artist_key
                WHERE a.disabled_children < (a.albums + a.songs)
                  AND NOT EXISTS (SELECT 1 FROM ready_skip s WHERE s.token = a.artist_key)
                  AND (
                    EXISTS (
                      SELECT 1
                      FROM wanted_rows album
                      LEFT JOIN jobs j ON j.album_id = album.album_id
                      LEFT JOIN wanted_stats st ON st.kind = 'album'
                      WHERE album.artist_key = a.artist_key
                        AND album.kind = 'album'
                        AND album.album_id IS NOT NULL
                        AND IFNULL(CAST(json_extract(album.payload, '$.statistics.trackFileCount') AS INTEGER), 0) = 0
                        AND (
                          (album.enabled_gen = IFNULL(st.generation, 0) AND album.enabled = 1)
                          OR (album.enabled_gen != IFNULL(st.generation, 0) AND IFNULL(st.default_enabled, 1) = 1)
                        )
                        AND (
                          j.album_id IS NULL
                          OR j.status IN ('not_sent', 'import_failed', 'stopped', 'skipped', 'searching')
                          OR (j.status IN ('failed', 'no_match') AND j.updated_at < ?)
                        )
                    )
                    OR EXISTS (
                      SELECT 1
                      FROM wanted_rows album
                      LEFT JOIN wanted_stats st ON st.kind = 'album'
                      WHERE album.artist_key = a.artist_key
                        AND album.kind = 'album'
                        AND IFNULL(CAST(json_extract(album.payload, '$.statistics.trackFileCount') AS INTEGER), 0) > 0
                        AND IFNULL(CAST(json_extract(album.payload, '$.statistics.trackFileCount') AS INTEGER), 0)
                            < COALESCE(
                                CAST(json_extract(album.payload, '$.statistics.totalTrackCount') AS INTEGER),
                                CAST(json_extract(album.payload, '$.statistics.trackCount') AS INTEGER),
                                0
                            )
                        AND (
                          (album.enabled_gen = IFNULL(st.generation, 0) AND album.enabled = 1)
                          OR (album.enabled_gen != IFNULL(st.generation, 0) AND IFNULL(st.default_enabled, 1) = 1)
                        )
                    )
                    OR EXISTS (
                      SELECT 1
                      FROM wanted_rows song
                      LEFT JOIN track_jobs t ON t.track_id = song.track_id
                      LEFT JOIN wanted_rows album
                        ON album.album_id = song.album_id AND album.kind = 'album'
                      LEFT JOIN wanted_stats st ON st.kind = 'song'
                      WHERE song.artist_key = a.artist_key
                        AND song.kind = 'song'
                        AND song.track_id IS NOT NULL
                        AND IFNULL(CAST(json_extract(album.payload, '$.statistics.trackFileCount') AS INTEGER), 0) > 0
                        AND (
                          (song.enabled_gen = IFNULL(st.generation, 0) AND song.enabled = 1)
                          OR (song.enabled_gen != IFNULL(st.generation, 0) AND IFNULL(st.default_enabled, 1) = 1)
                        )
                        AND (
                          t.track_id IS NULL
                          OR t.status IN ('not_sent', 'import_failed', 'stopped', 'skipped', 'searching')
                          OR (t.status IN ('failed', 'no_match') AND t.updated_at < ?)
                        )
                    )
                  )
                  AND (
                    ? = ''
                    OR EXISTS (
                      SELECT 1
                      FROM wanted_rows tag_row, json_each(tag_row.payload, '$.tags') je
                      WHERE tag_row.artist_key = a.artist_key
                        AND tag_row.kind = 'album'
                        AND lower(je.value) = ?
                    )
                    OR EXISTS (
                      SELECT 1
                      FROM wanted_rows tag_row, json_each(tag_row.payload, '$.artist.tags') je
                      WHERE tag_row.artist_key = a.artist_key
                        AND tag_row.kind = 'album'
                        AND lower(je.value) = ?
                    )
                  )
                ORDER BY {order_sql}
                LIMIT 1
                """,
                (cutoff, cutoff, wanted_tag, wanted_tag, wanted_tag),
            ).fetchone()
        return None if not row else row[0]

    def _ensure_artist_priority(self) -> None:
        with self._lock:
            self._add_search_columns_locked()
            if self._meta_locked("artist_priority") == "1" and self._meta_locked("artist_search_stats") == "1":
                return
            self._refresh_artist_priority_locked()
            self._db.commit()

    def _add_search_columns_locked(self) -> None:
        self._add_column("wanted_artists", "monitored", "INTEGER NOT NULL DEFAULT 1")
        self._add_column("wanted_artists", "added", "TEXT NOT NULL DEFAULT ''")
        self._add_column("wanted_artists", "missing_tracks", "INTEGER NOT NULL DEFAULT 0")
        self._add_column("wanted_artists", "missing_albums", "INTEGER NOT NULL DEFAULT 0")
        self._add_column("wanted_artists", "release_min", "TEXT NOT NULL DEFAULT ''")
        self._add_column("wanted_artists", "release_max", "TEXT NOT NULL DEFAULT ''")
        self._add_column("wanted_rows", "sort_group", "TEXT NOT NULL DEFAULT ''")
        self._add_column("wanted_rows", "sort_within", "TEXT NOT NULL DEFAULT ''")

    def refresh_display_order(self, order: str, direction: str) -> None:
        """Write the Wanted list sort keys for this search order."""
        order = normalize_search_order(order)
        direction = normalize_search_direction(direction)
        with self._lock:
            self._add_search_columns_locked()
            self._refresh_artist_priority_locked()
            tried = {
                token: stamp or ""
                for token, stamp in self._db.execute(
                    """
                    SELECT lower(artist), MAX(updated_at)
                    FROM (
                        SELECT artist, updated_at FROM jobs
                        UNION ALL
                        SELECT artist, updated_at FROM track_jobs
                    )
                    GROUP BY lower(artist)
                    """
                )
            }
            artists = self._db.execute(
                """
                SELECT artist_key, sort_artist, monitored, added, missing_tracks, missing_albums, release_min, release_max
                FROM wanted_artists
                """
            ).fetchall()
            self._db.executemany(
                "UPDATE wanted_rows SET sort_group = ? WHERE artist_key = ?",
                [
                    (
                        artist_display_key(
                            order=order,
                            direction=direction,
                            name=sort_name or "",
                            monitored=bool(monitored),
                            added=added or "",
                            missing_tracks=int(missing_tracks or 0),
                            missing_albums=int(missing_albums or 0),
                            release_min=release_min or "",
                            release_max=release_max or "",
                            last_tried=tried.get(token, ""),
                        ),
                        token,
                    )
                    for token, sort_name, monitored, added, missing_tracks, missing_albums, release_min, release_max in artists
                ],
            )
            gaps: dict[tuple[str, str], int] = {}
            rows = self._db.execute(
                """
                SELECT key, artist_key, kind, album, sort_song, payload
                FROM wanted_rows
                WHERE kind != 'artist'
                """
            ).fetchall()
            payloads: dict[tuple[str, str], dict] = {}
            for key, token, kind, album, sort_song, payload in rows:
                data = _loads(payload) if kind == "album" else None
                if kind == "album" and isinstance(data, dict):
                    tracks, _albums = missing_counts(data)
                    gaps[(token, (album or "").casefold())] = tracks
                    payloads[(token, (album or "").casefold())] = data
            updates = []
            for key, token, kind, album, sort_song, payload in rows:
                data = _loads(payload) if payload else None
                album_name = album or ""
                parent = payloads.get((token, album_name.casefold()))
                updates.append(
                    (
                        within_display_key(
                            kind=kind,
                            album=album_name,
                            sort_song=sort_song or "",
                            payload=data if kind == "album" else parent,
                            order=order,
                            direction=direction,
                            album_gap=gaps.get((token, album_name.casefold()), 0),
                        ),
                        key,
                    )
                )
            self._db.executemany("UPDATE wanted_rows SET sort_within = ? WHERE key = ?", updates)
            self._db.execute("UPDATE wanted_rows SET sort_within = '' WHERE kind = 'artist'")
            self._set_meta_locked("display_order", f"{order}|{direction}")
            self._db.commit()

    def _refresh_artist_priority_locked(self) -> None:
        self._add_search_columns_locked()
        self._db.execute(
            """
            UPDATE wanted_artists
            SET monitored = IFNULL((
                    SELECT MAX(CASE
                        WHEN json_extract(r.payload, '$.monitored') IN (0, 'false', '0') THEN 0
                        ELSE 1
                    END)
                    FROM wanted_rows r
                    WHERE r.artist_key = wanted_artists.artist_key AND r.kind = 'album'
                ), 1),
                added = IFNULL((
                    SELECT MAX(json_extract(r.payload, '$.added'))
                    FROM wanted_rows r
                    WHERE r.artist_key = wanted_artists.artist_key AND r.kind = 'album'
                ), '')
            """
        )
        self._refresh_missing_counts_locked()
        self._set_meta_locked("artist_priority", "1")
        self._set_meta_locked("artist_search_stats", "1")

    def _refresh_missing_counts_locked(self) -> None:
        modes = self._stat_modes_locked()
        buckets: dict[str, dict] = {}
        for artist_key, payload, enabled, enabled_gen in self._db.execute(
            "SELECT artist_key, payload, enabled, enabled_gen FROM wanted_rows WHERE kind = 'album'"
        ):
            if not _effective(enabled, enabled_gen, "album", modes):
                continue
            data = _loads(payload) or {}
            tracks, albums = missing_counts(data if isinstance(data, dict) else None)
            stamp = release_text(data if isinstance(data, dict) else None)
            slot = buckets.setdefault(artist_key, {"tracks": 0, "albums": 0, "min": "", "max": ""})
            slot["tracks"] += tracks
            slot["albums"] += albums
            if stamp and albums:
                if not slot["min"] or stamp < slot["min"]:
                    slot["min"] = stamp
                if not slot["max"] or stamp > slot["max"]:
                    slot["max"] = stamp
        self._db.execute(
            "UPDATE wanted_artists SET missing_tracks = 0, missing_albums = 0, release_min = '', release_max = ''"
        )
        self._db.executemany(
            """
            UPDATE wanted_artists
            SET missing_tracks = ?, missing_albums = ?, release_min = ?, release_max = ?
            WHERE artist_key = ?
            """,
            [
                (slot["tracks"], slot["albums"], slot["min"], slot["max"], token)
                for token, slot in buckets.items()
            ],
        )

    def block_source(self, username: str, folder: str, reason: str) -> None:
        name = (username or "").strip()
        if not name:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO blocklist(username, folder, reason, created_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(username, folder) DO UPDATE SET
                    reason = excluded.reason,
                    created_at = excluded.created_at
                """,
                (name, folder or "", reason or "failed", now),
            )
            self._db.commit()

    def source_blocked(self, username: str, folder: str = "") -> bool:
        name = (username or "").casefold().strip()
        if not name:
            return False
        remote = (folder or "").casefold().strip()
        with self._lock:
            rows = self._db.execute(
                "SELECT username, folder FROM blocklist WHERE lower(username) = ?",
                (name,),
            ).fetchall()
        for _user, blocked_folder in rows:
            blocked = (blocked_folder or "").casefold().strip()
            if not blocked or blocked == remote or (remote and remote.startswith(blocked)):
                return True
        return False

    def blocklist_rows(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT id, username, folder, reason, created_at
                FROM blocklist
                ORDER BY created_at DESC, id DESC
                """
            ).fetchall()
        keys = ("id", "username", "folder", "reason", "created_at")
        return [dict(zip(keys, row)) for row in rows]

    def clear_block(self, row_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM blocklist WHERE id = ?", (int(row_id),))
            self._db.commit()

    def clear_blocklist(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM blocklist")
            self._db.commit()

    def _artist_has_tag_locked(self, token: str, tag: str) -> bool:
        rows = self._db.execute(
            """
            SELECT payload FROM wanted_rows
            WHERE artist_key = ? AND kind = 'album' AND payload IS NOT NULL
            LIMIT 12
            """,
            (token,),
        ).fetchall()
        for (payload,) in rows:
            try:
                data = json.loads(payload or "")
            except json.JSONDecodeError:
                continue
            labels = []
            if isinstance(data, dict):
                labels.extend(data.get("tags") or [])
                artist = data.get("artist") if isinstance(data.get("artist"), dict) else {}
                labels.extend(artist.get("tags") or [])
            for label in labels:
                if str(label).casefold().strip() == tag:
                    return True
        return False

    def wanted_artist_label(self, token: str) -> str:
        """Display name for a casefolded artist key. The key itself if it is unknown."""
        token = (token or "").casefold().strip()
        if token.startswith("artist:"):
            token = token[len("artist:") :].strip()
        if not token:
            return ""
        with self._lock:
            row = self._db.execute(
                "SELECT artist FROM wanted_artists WHERE artist_key = ?",
                (token,),
            ).fetchone()
        if row and row[0]:
            return str(row[0])
        return token

    def artist_work(self, artist: str) -> tuple[list[dict], list[dict]]:
        token = (artist or "").casefold().strip()
        with self._lock:
            modes = self._stat_modes_locked()
            albums = []
            by_id: dict[int, dict] = {}
            for album_id, payload, enabled, enabled_gen in self._db.execute(
                """
                SELECT album_id, payload, enabled, enabled_gen FROM wanted_rows
                WHERE artist_key = ? AND kind = 'album'
                """,
                (token,),
            ):
                album = _loads(payload) or {}
                if album_id is not None and "id" not in album:
                    album["id"] = album_id
                if album.get("id") is not None:
                    by_id[int(album["id"])] = album
                elif album_id is not None:
                    album.setdefault("id", album_id)
                    by_id[int(album_id)] = album
                if _effective(enabled, enabled_gen, "album", modes):
                    albums.append(album)
            holes = []
            for album_id, album_title, track_json, name, enabled, enabled_gen in self._db.execute(
                """
                SELECT album_id, album, track_json, artist, enabled, enabled_gen FROM wanted_rows
                WHERE artist_key = ? AND kind = 'song'
                """,
                (token,),
            ):
                if not _effective(enabled, enabled_gen, "song", modes):
                    continue
                track = _loads(track_json) or {}
                album = by_id.get(int(album_id)) if album_id is not None else None
                if album is None:
                    album = {
                        "id": album_id,
                        "title": album_title,
                        "artist": {"artistName": name},
                        "statistics": {},
                    }
                holes.append(
                    {
                        "track": track,
                        "album": album,
                        "artist_name": name,
                        "album_title": album_title,
                    }
                )
        return albums, holes

    def _ensure_catalog_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS wanted_rows (
                key TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                artist TEXT NOT NULL,
                artist_key TEXT NOT NULL,
                album TEXT NOT NULL DEFAULT '',
                song TEXT NOT NULL DEFAULT '',
                album_id INTEGER,
                track_id INTEGER,
                detail TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                sort_artist TEXT NOT NULL,
                sort_album TEXT NOT NULL DEFAULT '',
                sort_song TEXT NOT NULL DEFAULT '',
                kind_rank INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                payload TEXT,
                track_json TEXT,
                fold TEXT NOT NULL DEFAULT '',
                enabled_gen INTEGER NOT NULL DEFAULT 0,
                sync_digest TEXT,
                page_fingerprint TEXT,
                track_rows INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS wanted_artists (
                artist_key TEXT PRIMARY KEY,
                artist TEXT NOT NULL,
                sort_artist TEXT NOT NULL,
                albums INTEGER NOT NULL DEFAULT 0,
                songs INTEGER NOT NULL DEFAULT 0,
                disabled_albums INTEGER NOT NULL DEFAULT 0,
                disabled_songs INTEGER NOT NULL DEFAULT 0,
                disabled_children INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS wanted_stats (
                kind TEXT PRIMARY KEY,
                total INTEGER NOT NULL,
                enabled INTEGER NOT NULL,
                generation INTEGER NOT NULL DEFAULT 0,
                default_enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_wanted_order
                ON wanted_rows(sort_artist, kind_rank, sort_album, sort_song, key);
            CREATE INDEX IF NOT EXISTS idx_wanted_kind
                ON wanted_rows(kind, sort_artist, sort_album, sort_song, key);
            CREATE INDEX IF NOT EXISTS idx_wanted_artist
                ON wanted_rows(artist_key, kind, enabled);
            CREATE INDEX IF NOT EXISTS idx_wanted_kind_album
                ON wanted_rows(kind, sort_album, key);
            CREATE INDEX IF NOT EXISTS idx_wanted_kind_song
                ON wanted_rows(kind, sort_song, key);
            CREATE INDEX IF NOT EXISTS idx_wanted_kind_detail
                ON wanted_rows(kind, detail, key);
            CREATE INDEX IF NOT EXISTS idx_wanted_album_sort
                ON wanted_rows(sort_album, key);
            CREATE INDEX IF NOT EXISTS idx_wanted_song_sort
                ON wanted_rows(sort_song, key);
            CREATE INDEX IF NOT EXISTS idx_wanted_detail_sort
                ON wanted_rows(detail, key);
            CREATE INDEX IF NOT EXISTS idx_wanted_kind_artist_key
                ON wanted_rows(kind, artist_key);
            CREATE INDEX IF NOT EXISTS idx_wanted_artists_sort
                ON wanted_artists(sort_artist);
            CREATE INDEX IF NOT EXISTS idx_wanted_artist_fold
                ON wanted_rows(fold, artist_key) WHERE kind = 'artist';
            CREATE INDEX IF NOT EXISTS idx_wanted_album_id
                ON wanted_rows(album_id, kind);
            CREATE TABLE IF NOT EXISTS wanted_sync_seen (
                album_id INTEGER PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS wanted_track_queue (
                album_id INTEGER PRIMARY KEY,
                artist_id INTEGER,
                page_fingerprint TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS blocklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                folder TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(username, folder)
            );
            """
        )
        self._add_column("wanted_rows", "enabled_gen", "INTEGER NOT NULL DEFAULT 0")
        self._add_column("wanted_rows", "fold", "TEXT NOT NULL DEFAULT ''")
        self._add_column("wanted_rows", "sync_digest", "TEXT")
        self._add_column("wanted_rows", "page_fingerprint", "TEXT")
        self._add_column("wanted_rows", "track_rows", "INTEGER NOT NULL DEFAULT 0")
        self._add_column("wanted_artists", "disabled_albums", "INTEGER NOT NULL DEFAULT 0")
        self._add_column("wanted_artists", "disabled_songs", "INTEGER NOT NULL DEFAULT 0")
        self._add_column("wanted_stats", "generation", "INTEGER NOT NULL DEFAULT 0")
        self._add_column("wanted_stats", "default_enabled", "INTEGER NOT NULL DEFAULT 1")
        self._add_column("wanted_artists", "monitored", "INTEGER NOT NULL DEFAULT 1")
        self._add_column("wanted_artists", "added", "TEXT NOT NULL DEFAULT ''")
        self._add_column("wanted_artists", "missing_tracks", "INTEGER NOT NULL DEFAULT 0")
        self._add_column("wanted_artists", "missing_albums", "INTEGER NOT NULL DEFAULT 0")
        self._add_column("wanted_artists", "release_min", "TEXT NOT NULL DEFAULT ''")
        self._add_column("wanted_artists", "release_max", "TEXT NOT NULL DEFAULT ''")
        self._add_column("wanted_rows", "sort_group", "TEXT NOT NULL DEFAULT ''")
        self._add_column("wanted_rows", "sort_within", "TEXT NOT NULL DEFAULT ''")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_wanted_search ON wanted_rows(sort_group, sort_within, kind_rank, key)"
        )
        self._db.commit()

    def _add_column(self, table: str, column: str, declaration: str) -> None:
        have = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _load_catalog_flag(self) -> None:
        with self._lock:
            if self._meta_locked("wanted_catalog") == "1":
                self._catalog_ready = True
                return
            found = self._db.execute("SELECT 1 FROM wanted_rows LIMIT 1").fetchone()
            self._catalog_ready = found is not None
            if self._catalog_ready:
                self._set_meta_locked("wanted_catalog", "1")
                self._db.commit()

    def _row_count_locked(self) -> int:
        raw = self._meta_locked("wanted_row_count")
        if raw is not None and raw.isdigit():
            return int(raw)
        row = self._db.execute("SELECT COUNT(*) FROM wanted_rows").fetchone()
        return int(row[0]) if row else 0

    def _meta_locked(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if not row else row[0]

    def _set_meta_locked(self, key: str, value: str | None) -> None:
        if value is None:
            self._db.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            self._db.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def _mark_catalog_locked(self) -> None:
        self._catalog_ready = True
        self._set_meta_locked("wanted_catalog", "1")
        self._set_meta_locked("wanted_row_count", str(self._sync_start + self._sync_inserted))

    def _apply_saved_choices_locked(self, rows: list[dict]) -> None:
        keys = [row["key"] for row in rows]
        saved: dict[str, int] = {}
        for chunk in _chunks(keys, 400):
            marks = ",".join("?" * len(chunk))
            for key, enabled in self._db.execute(
                f"SELECT key, enabled FROM wanted_choices WHERE key IN ({marks})",
                chunk,
            ):
                saved[key] = int(enabled)
        for row in rows:
            if row["key"] in saved:
                row["enabled"] = saved[row["key"]]

    def _note_new_rows_locked(self, rows: list[dict]) -> None:
        artists: dict[str, dict] = {}
        stats: dict[str, list[int]] = {}
        for row in rows:
            enabled = 1 if row.get("enabled", 1) else 0
            row["enabled"] = enabled
            bucket = stats.setdefault(row["kind"], [0, 0])
            bucket[0] += 1
            bucket[1] += enabled
            slot = artists.setdefault(
                row["artist_key"],
                {
                    "artist": row["artist"],
                    "sort": row["sort_artist"],
                    "albums": 0,
                    "songs": 0,
                    "disabled": 0,
                    "disabled_albums": 0,
                    "disabled_songs": 0,
                    "enabled": 1,
                    "seen_artist": False,
                },
            )
            if row["kind"] == "artist":
                slot["artist"] = row["artist"]
                slot["sort"] = row["sort_artist"]
                slot["enabled"] = enabled
                slot["seen_artist"] = True
            elif row["kind"] == "album":
                slot["albums"] += 1
                if not enabled:
                    slot["disabled"] += 1
                    slot["disabled_albums"] += 1
            elif row["kind"] == "song":
                slot["songs"] += 1
                if not enabled:
                    slot["disabled"] += 1
                    slot["disabled_songs"] += 1
        self._db.executemany(
            """
            INSERT INTO wanted_artists(
                artist_key, artist, sort_artist, albums, songs,
                disabled_albums, disabled_songs, disabled_children, enabled
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artist_key) DO UPDATE SET
                artist = CASE WHEN excluded.artist != '' THEN excluded.artist ELSE wanted_artists.artist END,
                sort_artist = excluded.sort_artist,
                albums = wanted_artists.albums + excluded.albums,
                songs = wanted_artists.songs + excluded.songs,
                disabled_albums = wanted_artists.disabled_albums + excluded.disabled_albums,
                disabled_songs = wanted_artists.disabled_songs + excluded.disabled_songs,
                disabled_children = wanted_artists.disabled_children + excluded.disabled_children
            """,
            [
                (
                    token,
                    slot["artist"],
                    slot["sort"],
                    slot["albums"],
                    slot["songs"],
                    slot["disabled_albums"],
                    slot["disabled_songs"],
                    slot["disabled"],
                    slot["enabled"],
                )
                for token, slot in artists.items()
            ],
        )
        self._db.executemany(
            """
            INSERT INTO wanted_stats(kind, total, enabled) VALUES(?, ?, ?)
            ON CONFLICT(kind) DO UPDATE SET
                total = wanted_stats.total + excluded.total,
                enabled = wanted_stats.enabled + excluded.enabled
            """,
            [(kind, total, enabled) for kind, (total, enabled) in stats.items()],
        )
        self._refresh_artist_details_locked(list(artists))

    def _refresh_artist_details_locked(self, tokens: list[str] | None) -> None:
        detail = """
            (
                SELECT CASE
                    WHEN a.albums <= 0 AND a.songs <= 0 THEN 'Incomplete'
                    WHEN a.albums > 0 AND a.songs <= 0 THEN
                        a.albums || ' album' || CASE WHEN a.albums = 1 THEN '' ELSE 's' END
                    WHEN a.songs > 0 AND a.albums <= 0 THEN
                        a.songs || ' song' || CASE WHEN a.songs = 1 THEN '' ELSE 's' END
                    ELSE
                        a.albums || ' album' || CASE WHEN a.albums = 1 THEN '' ELSE 's' END
                        || ', ' ||
                        a.songs || ' song' || CASE WHEN a.songs = 1 THEN '' ELSE 's' END
                END
                FROM wanted_artists a
                WHERE a.artist_key = wanted_rows.artist_key
            )
        """
        if tokens is None:
            self._db.execute(f"UPDATE wanted_rows SET detail = {detail} WHERE kind = 'artist'")
            return
        for chunk in _chunks(tokens, 400):
            marks = ",".join("?" * len(chunk))
            self._db.execute(
                f"UPDATE wanted_rows SET detail = {detail} WHERE kind = 'artist' AND artist_key IN ({marks})",
                chunk,
            )

    def _rebuild_derived_locked(self) -> None:
        self._db.execute("DELETE FROM wanted_artists")
        self._db.execute(
            """
            INSERT INTO wanted_artists(
                artist_key, artist, sort_artist, albums, songs,
                disabled_albums, disabled_songs, disabled_children, enabled
            )
            SELECT r.artist_key,
                   COALESCE(MAX(CASE WHEN r.kind = 'artist' THEN r.artist END), MAX(r.artist)),
                   MIN(r.sort_artist),
                   SUM(CASE WHEN r.kind = 'album' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN r.kind = 'song' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN r.kind = 'album' AND NOT (
                        (r.enabled_gen = IFNULL(s.generation, 0) AND r.enabled = 1)
                        OR (r.enabled_gen != IFNULL(s.generation, 0) AND IFNULL(s.default_enabled, 1) = 1)
                   ) THEN 1 ELSE 0 END),
                   SUM(CASE WHEN r.kind = 'song' AND NOT (
                        (r.enabled_gen = IFNULL(s.generation, 0) AND r.enabled = 1)
                        OR (r.enabled_gen != IFNULL(s.generation, 0) AND IFNULL(s.default_enabled, 1) = 1)
                   ) THEN 1 ELSE 0 END),
                   SUM(CASE WHEN r.kind != 'artist' AND NOT (
                        (r.enabled_gen = IFNULL(s.generation, 0) AND r.enabled = 1)
                        OR (r.enabled_gen != IFNULL(s.generation, 0) AND IFNULL(s.default_enabled, 1) = 1)
                   ) THEN 1 ELSE 0 END),
                   COALESCE(MAX(CASE WHEN r.kind = 'artist' THEN
                        CASE WHEN (r.enabled_gen = IFNULL(s.generation, 0) AND r.enabled = 1)
                               OR (r.enabled_gen != IFNULL(s.generation, 0) AND IFNULL(s.default_enabled, 1) = 1)
                             THEN 1 ELSE 0 END
                   END), 1)
            FROM wanted_rows r
            LEFT JOIN wanted_stats s ON s.kind = r.kind
            GROUP BY r.artist_key
            """
        )
        self._rebuild_stats_locked()
        count = int(self._db.execute("SELECT COUNT(*) FROM wanted_rows").fetchone()[0])
        self._set_meta_locked("wanted_row_count", str(count))
        self._sync_start = count
        self._sync_inserted = 0
        self._refresh_artist_details_locked(None)
        self._set_meta_locked("artist_priority", "0")
        self._set_meta_locked("artist_search_stats", "0")
        self._set_meta_locked("display_order", "")

    def _rebuild_stats_locked(self) -> None:
        modes = self._stat_modes_locked()
        self._db.execute("DELETE FROM wanted_stats")
        for kind in ("artist", "album", "song"):
            gen, default = modes.get(kind, (0, 1))
            total, enabled = self._db.execute(
                f"""
                SELECT COUNT(*),
                       COALESCE(SUM(CASE WHEN {_EFFECTIVE_ON} THEN 1 ELSE 0 END), 0)
                FROM wanted_rows WHERE kind = ?
                """,
                (gen, gen, default, kind),
            ).fetchone()
            if int(total or 0) <= 0:
                continue
            self._db.execute(
                """
                INSERT INTO wanted_stats(kind, total, enabled, generation, default_enabled)
                VALUES(?, ?, ?, ?, ?)
                """,
                (kind, int(total), int(enabled or 0), gen, default),
            )
        count = int(self._db.execute("SELECT COALESCE(SUM(total), 0) FROM wanted_stats").fetchone()[0])
        self._set_meta_locked("wanted_row_count", str(count))

    def _search_sql(self, search: str) -> tuple[str, list]:
        from match import normalize

        term = normalize(search or "")
        if not term:
            return "", []
        return (
            "artist_key IN ("
            "SELECT artist_key FROM wanted_rows INDEXED BY idx_wanted_artist_fold "
            "WHERE kind = 'artist' AND instr(fold, ?) > 0"
            ")",
            [term],
        )

    def _where_locked(self, kinds: tuple[str, ...] | None, search: str) -> tuple[str, list]:
        parts = []
        params: list = []
        if kinds:
            marks = ",".join("?" * len(kinds))
            parts.append(f"kind IN ({marks})")
            params.extend(kinds)
        search_sql, search_params = self._search_sql(search)
        if search_sql:
            parts.append(search_sql)
            params.extend(search_params)
        if not parts:
            return "", []
        return " WHERE " + " AND ".join(parts), params

    def _count_locked(self, kinds, search: str, where: str, params: list) -> int:
        if not search:
            if not kinds:
                return self._row_count_locked()
            if len(kinds) == 1:
                row = self._db.execute("SELECT total FROM wanted_stats WHERE kind = ?", (kinds[0],)).fetchone()
                return int(row[0]) if row else 0
        row = self._db.execute(f"SELECT COUNT(*) FROM wanted_rows{where}", params).fetchone()
        return int(row[0]) if row else 0

    def _checked_locked(self, kinds: tuple[str, ...] | None, search: str) -> int:
        search_sql, search_params = self._search_sql(search)
        artist_where = search_sql or "1 = 1"
        fully = self._db.execute(
            f"""
            SELECT COUNT(*) FROM wanted_artists
            WHERE {artist_where}
              AND (
                    ((albums + songs) = 0 AND enabled = 1)
                 OR ((albums + songs) > 0 AND disabled_children = 0)
              )
            """,
            search_params,
        ).fetchone()
        full_artists = int(fully[0]) if fully else 0
        if kinds == ("artist",):
            return full_artists
        if kinds is None:
            if search_sql:
                children = self._effective_count_locked(("album", "song"), search_sql, search_params)
            else:
                row = self._db.execute(
                    """
                    SELECT COALESCE(SUM(enabled), 0) FROM wanted_stats
                    WHERE kind IN ('album', 'song')
                    """
                ).fetchone()
                children = int(row[0]) if row else 0
            return children + full_artists
        kind = kinds[0]
        if search_sql:
            return self._effective_count_locked((kind,), search_sql, search_params)
        row = self._db.execute("SELECT enabled FROM wanted_stats WHERE kind = ?", (kind,)).fetchone()
        return int(row[0]) if row else 0

    def _overlay_choices_locked(self, updates: dict[str, bool]) -> None:
        modes = self._stat_modes_locked()
        current: dict[str, tuple] = {}
        for chunk in _chunks(list(updates), 400):
            marks = ",".join("?" * len(chunk))
            for key, artist_key, kind, enabled, enabled_gen in self._db.execute(
                f"""
                SELECT key, artist_key, kind, enabled, enabled_gen
                FROM wanted_rows WHERE key IN ({marks})
                """,
                chunk,
            ):
                current[key] = (artist_key, kind, int(enabled), int(enabled_gen or 0))
        stat_delta: dict[str, int] = {}
        child_delta: dict[str, int] = {}
        album_delta: dict[str, int] = {}
        song_delta: dict[str, int] = {}
        artist_enabled: dict[str, int] = {}
        params = []
        for key, (token, kind, enabled, enabled_gen) in current.items():
            new = 1 if updates[key] else 0
            if new == _effective(enabled, enabled_gen, kind, modes):
                continue
            gen = modes.get(kind, (0, 1))[0]
            params.append((new, gen, key))
            stat_delta[kind] = stat_delta.get(kind, 0) + (1 if new else -1)
            if kind == "artist":
                artist_enabled[token] = new
            else:
                child_delta[token] = child_delta.get(token, 0) + (-1 if new else 1)
                if kind == "album":
                    album_delta[token] = album_delta.get(token, 0) + (-1 if new else 1)
                elif kind == "song":
                    song_delta[token] = song_delta.get(token, 0) + (-1 if new else 1)
        if params:
            self._db.executemany(
                "UPDATE wanted_rows SET enabled = ?, enabled_gen = ? WHERE key = ?",
                params,
            )
        for kind, delta in stat_delta.items():
            self._db.execute(
                "UPDATE wanted_stats SET enabled = MAX(0, enabled + ?) WHERE kind = ?",
                (delta, kind),
            )
        for token in set(child_delta) | set(album_delta) | set(song_delta):
            self._db.execute(
                """
                UPDATE wanted_artists
                SET disabled_children = MAX(0, disabled_children + ?),
                    disabled_albums = MAX(0, disabled_albums + ?),
                    disabled_songs = MAX(0, disabled_songs + ?)
                WHERE artist_key = ?
                """,
                (
                    child_delta.get(token, 0),
                    album_delta.get(token, 0),
                    song_delta.get(token, 0),
                    token,
                ),
            )
        for token, enabled in artist_enabled.items():
            self._db.execute(
                "UPDATE wanted_artists SET enabled = ? WHERE artist_key = ?",
                (enabled, token),
            )
        if child_delta or album_delta or song_delta or artist_enabled:
            self._set_meta_locked("artist_search_stats", "0")
            self._set_meta_locked("display_order", "")

    def _stat_modes_locked(self) -> dict[str, tuple[int, int]]:
        modes: dict[str, tuple[int, int]] = {}
        for kind, gen, default in self._db.execute(
            "SELECT kind, generation, default_enabled FROM wanted_stats"
        ):
            modes[kind] = (int(gen or 0), 1 if int(default if default is not None else 1) else 0)
        return modes

    def _order_columns(self, order: str, kinds) -> tuple[str, ...]:
        if order == "default" and kinds is not None and len(tuple(kinds)) == 1:
            return ("sort_artist", "sort_album", "sort_song", "key")
        return _ORDER_COLUMNS.get(order or "default", _ORDER_COLUMNS["default"])

    def _fetch_page_locked(
        self,
        kinds,
        search: str,
        columns: tuple[str, ...],
        *,
        reverse: bool,
        limit: int,
        offset: int,
        cursor: tuple | None,
        op: str,
    ) -> list[tuple]:
        if op not in {">", ">=", "<", "<="}:
            raise ValueError(op)
        if any(name not in _ALLOWED_COLUMNS for name in columns):
            raise ValueError(columns)
        where, params = self._where_locked(kinds, search)
        if cursor is not None:
            cols = ", ".join(columns)
            marks = ", ".join("?" * len(columns))
            clause = f"({cols}) {op} ({marks})"
            where = (where + " AND " + clause) if where else (" WHERE " + clause)
            params = [*params, *cursor]
        direction = "DESC" if reverse else "ASC"
        order_sql = ", ".join(f"{name} {direction}" for name in columns)
        sql = (
            "SELECT key, kind, artist, album, song, detail, enabled, enabled_gen, artist_key, "
            + ", ".join(columns)
            + f" FROM wanted_rows{where} ORDER BY {order_sql} LIMIT ? OFFSET ?"
        )
        return list(self._db.execute(sql, [*params, int(limit), int(offset)]))

    def _bump_kind_locked(self, kind: str, flag: int) -> None:
        row = self._db.execute(
            "SELECT total, generation FROM wanted_stats WHERE kind = ?",
            (kind,),
        ).fetchone()
        if row is None:
            return
        total, gen = int(row[0]), int(row[1] or 0)
        self._db.execute(
            """
            UPDATE wanted_stats
            SET generation = ?, default_enabled = ?, enabled = ?
            WHERE kind = ?
            """,
            (gen + 1, flag, total if flag else 0, kind),
        )

    def _set_matching_kind_locked(self, kind: str, flag: int, search_sql: str, search_params: list) -> None:
        gen, default = self._stat_modes_locked().get(kind, (0, 1))
        on = int(
            self._db.execute(
                f"""
                SELECT COUNT(*) FROM wanted_rows
                WHERE kind = ? AND {search_sql} AND {_EFFECTIVE_ON}
                """,
                (kind, *search_params, gen, gen, default),
            ).fetchone()[0]
        )
        total = int(
            self._db.execute(
                f"SELECT COUNT(*) FROM wanted_rows WHERE kind = ? AND {search_sql}",
                (kind, *search_params),
            ).fetchone()[0]
        )
        target = total if flag else 0
        if target != on:
            self._db.execute(
                "UPDATE wanted_stats SET enabled = MAX(0, enabled + ?) WHERE kind = ?",
                (target - on, kind),
            )
        self._db.execute(
            f"""
            UPDATE wanted_rows
            SET enabled = ?, enabled_gen = ?
            WHERE kind = ? AND {search_sql}
            """,
            (flag, gen, kind, *search_params),
        )
        self._recount_artists_locked(search_sql, search_params)

    def _recount_artists_locked(self, search_sql: str, search_params: list) -> None:
        effective = """
            (r.enabled_gen = IFNULL(s.generation, 0) AND r.enabled = 1)
            OR (r.enabled_gen != IFNULL(s.generation, 0) AND IFNULL(s.default_enabled, 1) = 1)
        """
        self._db.execute(
            f"""
            UPDATE wanted_artists SET
                disabled_albums = (
                    SELECT COUNT(*) FROM wanted_rows r
                    LEFT JOIN wanted_stats s ON s.kind = r.kind
                    WHERE r.artist_key = wanted_artists.artist_key
                      AND r.kind = 'album'
                      AND NOT ({effective})
                ),
                disabled_songs = (
                    SELECT COUNT(*) FROM wanted_rows r
                    LEFT JOIN wanted_stats s ON s.kind = r.kind
                    WHERE r.artist_key = wanted_artists.artist_key
                      AND r.kind = 'song'
                      AND NOT ({effective})
                ),
                disabled_children = (
                    SELECT COUNT(*) FROM wanted_rows r
                    LEFT JOIN wanted_stats s ON s.kind = r.kind
                    WHERE r.artist_key = wanted_artists.artist_key
                      AND r.kind != 'artist'
                      AND NOT ({effective})
                ),
                enabled = COALESCE((
                    SELECT CASE WHEN ({effective}) THEN 1 ELSE 0 END
                    FROM wanted_rows r
                    LEFT JOIN wanted_stats s ON s.kind = r.kind
                    WHERE r.artist_key = wanted_artists.artist_key AND r.kind = 'artist'
                ), enabled)
            WHERE {search_sql}
            """,
            search_params,
        )

    def _effective_count_locked(self, kinds: tuple[str, ...], search_sql: str, search_params: list) -> int:
        marks = ",".join("?" * len(kinds))
        row = self._db.execute(
            f"""
            SELECT COALESCE(SUM(
                CASE WHEN (r.enabled_gen = IFNULL(s.generation, 0) AND r.enabled = 1)
                       OR (r.enabled_gen != IFNULL(s.generation, 0) AND IFNULL(s.default_enabled, 1) = 1)
                     THEN 1 ELSE 0 END
            ), 0)
            FROM wanted_rows r
            LEFT JOIN wanted_stats s ON s.kind = r.kind
            WHERE r.kind IN ({marks}) AND {search_sql}
            """,
            (*kinds, *search_params),
        ).fetchone()
        return int(row[0]) if row else 0


_ORDER_COLUMNS = {
    "default": ("sort_artist", "kind_rank", "sort_album", "sort_song", "key"),
    # sort_within is before kind_rank so an album's songs stay under that album.
    "search": ("sort_group", "sort_within", "kind_rank", "key"),
    "kind": ("kind", "key"),
    "artist": ("sort_artist", "key"),
    "album": ("sort_album", "key"),
    "song": ("sort_song", "key"),
    "detail": ("detail", "key"),
}
_ALLOWED_COLUMNS = {name for columns in _ORDER_COLUMNS.values() for name in columns}

_INSERT_WANTED = """
INSERT INTO wanted_rows(
    key, kind, artist, artist_key, album, song, album_id, track_id, detail, enabled,
    sort_artist, sort_album, sort_song, kind_rank,     content_hash, payload, track_json, fold,
    enabled_gen, sync_digest
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPDATE_WANTED = """
UPDATE wanted_rows SET
    kind = ?, artist = ?, artist_key = ?, album = ?, song = ?, album_id = ?, track_id = ?,
    detail = ?, sort_artist = ?, sort_album = ?, sort_song = ?, kind_rank = ?,
    content_hash = ?, payload = ?, track_json = ?, fold = ?
WHERE key = ? AND content_hash != ?
"""


def _row_tuple(row: dict) -> tuple:
    return (
        row["key"],
        row["kind"],
        row["artist"],
        row["artist_key"],
        row.get("album") or "",
        row.get("song") or "",
        row.get("album_id"),
        row.get("track_id"),
        row.get("detail") or "",
        1 if row.get("enabled", 1) else 0,
        row["sort_artist"],
        row.get("sort_album") or "",
        row.get("sort_song") or "",
        int(row["kind_rank"]),
        row["content_hash"],
        row.get("payload"),
        row.get("track_json"),
        row.get("fold") or "",
        int(row.get("enabled_gen") or 0),
        row.get("sync_digest"),
    )


def _update_tuple(row: dict) -> tuple:
    return (
        row["kind"],
        row["artist"],
        row["artist_key"],
        row.get("album") or "",
        row.get("song") or "",
        row.get("album_id"),
        row.get("track_id"),
        row.get("detail") or "",
        row["sort_artist"],
        row.get("sort_album") or "",
        row.get("sort_song") or "",
        int(row["kind_rank"]),
        row["content_hash"],
        row.get("payload"),
        row.get("track_json"),
        row.get("fold") or "",
        row["key"],
        row["content_hash"],
    )


def _loads(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
