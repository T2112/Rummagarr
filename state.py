from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path


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


class JobStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(path)
        self._lock = threading.Lock()
        self._catalog_ready = False
        self._import_choices = False
        self._sync_start = 0
        self._sync_inserted = 0
        self._sync_seen = 0
        self._sync_streamed = 0
        self._sync_artists: set[str] = set()
        self._needs_delete = False
        self._syncing = False
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
                row["enabled_gen"] = modes.get(row["kind"], (0, 1))[0]
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
            # |seen| == |stored| + |inserted| means every stored key was seen.
            # inserted counts only new keys, so equality means nothing was removed.
            # A no-op resync then skips the seen-key table entirely.
            self._needs_delete = bool(
                complete and self._sync_streamed != self._sync_start + self._sync_inserted
            )
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
            if kind == "artist":
                info = artists.get(token)
                if info is not None and info[0] + info[1] > 0:
                    shown = 1 if info[2] == 0 else 0
            page.append((key, kind, artist, album, song, detail or "", bool(shown), cursor))
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

    def next_wanted_artist(self, skip: set[str] | None = None) -> str | None:
        skip = skip or set()
        with self._lock:
            if len(skip) > 500:
                offset = 0
                while True:
                    rows = self._db.execute(
                        """
                        SELECT artist, artist_key FROM wanted_artists
                        WHERE (albums + songs) = 0 OR disabled_children < (albums + songs)
                        ORDER BY sort_artist
                        LIMIT 200 OFFSET ?
                        """,
                        (offset,),
                    ).fetchall()
                    if not rows:
                        return None
                    for artist, token in rows:
                        if token not in skip:
                            return artist
                    offset += 200
            params: list = []
            sql = """
                SELECT artist FROM wanted_artists
                WHERE ((albums + songs) = 0 OR disabled_children < (albums + songs))
            """
            if skip:
                marks = ",".join("?" * len(skip))
                sql += f" AND artist_key NOT IN ({marks})"
                params.extend(sorted(skip))
            sql += " ORDER BY sort_artist LIMIT 1"
            row = self._db.execute(sql, params).fetchone()
        return None if row is None else row[0]

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
                sync_digest TEXT
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
            """
        )
        self._add_column("wanted_rows", "enabled_gen", "INTEGER NOT NULL DEFAULT 0")
        self._add_column("wanted_rows", "fold", "TEXT NOT NULL DEFAULT ''")
        self._add_column("wanted_rows", "sync_digest", "TEXT")
        self._add_column("wanted_artists", "disabled_albums", "INTEGER NOT NULL DEFAULT 0")
        self._add_column("wanted_artists", "disabled_songs", "INTEGER NOT NULL DEFAULT 0")
        self._add_column("wanted_stats", "generation", "INTEGER NOT NULL DEFAULT 0")
        self._add_column("wanted_stats", "default_enabled", "INTEGER NOT NULL DEFAULT 1")
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
