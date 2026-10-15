"""A Soulseek user's profile and share tree.

The share list can be large. The tree answers one folder at a time so the
window inserts only the row that was opened. A saved copy is reused until
Refresh asks the peer again. Folders that name a missing Lidarr album are
marked so they stand out.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_PERMISSIONS = {
    -1: "Upload permission unknown",
    0: "Uploads off",
    1: "Uploads allowed",
    2: "Uploads for friends",
    3: "Uploads for a list",
}

_STATUS = {
    -1: "Status unknown",
    0: "Offline",
    1: "Away",
    2: "Online",
}


def permission_label(value) -> str:
    try:
        number = int(getattr(value, "value", value))
    except (TypeError, ValueError):
        return _PERMISSIONS[-1]
    return _PERMISSIONS.get(number, _PERMISSIONS[-1])


def status_label(value) -> str:
    try:
        number = int(getattr(value, "value", value))
    except (TypeError, ValueError):
        return _STATUS[-1]
    return _STATUS.get(number, _STATUS[-1])


def profile_text(username: str, info: dict | None) -> str:
    """The profile card. ``info`` is the saved profile dict, picture bytes left out."""
    data = dict(info or {})
    lines = [username or "Unknown user", status_label(data.get("status"))]
    slots = "Free slot" if data.get("has_slots_free") else "No free slot"
    lines.append(
        f"{slots}. {int(data.get('upload_slots') or 0)} upload slots. Queue {int(data.get('queue_length') or 0)}."
    )
    lines.append(permission_label(data.get("upload_permissions")))
    files = data.get("shared_file_count")
    folders = data.get("shared_folder_count")
    if files is not None and folders is not None:
        speed = int(data.get("avg_speed") or 0)
        lines.append(f"{int(files)} files in {int(folders)} folders. Average speed {speed}.")
    if data.get("has_picture"):
        lines.append("This user has a picture.")
    if data.get("privileged"):
        lines.append("Privileged.")
    description = str(data.get("description") or "").strip()
    if description:
        lines.append(description[:600])
    return "\n".join(lines)


def path_parts(path: str) -> list[str]:
    text = (path or "").replace("/", "\\").strip("\\")
    return [part for part in text.split("\\") if part and part not in {".", ".."}]


def missing_needles(albums: list[dict] | None) -> list[tuple[str, str]]:
    """Artist and album titles that still have missing tracks."""
    found: list[tuple[str, str]] = []
    for album in albums or []:
        artist = album.get("artist")
        if isinstance(artist, dict):
            artist_name = str(artist.get("artistName") or "")
        else:
            artist_name = str(artist or "")
        title = str(album.get("title") or "")
        stats = album.get("statistics") or {}
        try:
            have = int(stats.get("trackFileCount", album.get("files", 0)) or 0)
            total = int(stats.get("totalTrackCount", album.get("total", 0)) or 0)
        except (TypeError, ValueError):
            have, total = 0, 0
        if total > 0 and have >= total:
            continue
        if len(artist_name.strip()) < 2 or len(title.strip()) < 3:
            continue
        found.append((artist_name.casefold(), title.casefold()))
    return found


def is_wanted(text: str, needles: list[tuple[str, str]] | None) -> bool:
    folded = (text or "").casefold()
    if not folded:
        return False
    return any(artist in folded and album in folded for artist, album in needles or [])


class ShareIndex:
    """Folders and files from one browse, read one level at a time."""

    def __init__(self, directories: list[dict] | None = None) -> None:
        self._files: dict[str, list[dict]] = {}
        self._locked: set[str] = set()
        self._children: dict[str, set[str]] = {"": set()}
        for entry in directories or []:
            self.add_directory(entry)

    def add_directory(self, entry: dict) -> None:
        parts = path_parts(str(entry.get("name") or ""))
        if not parts:
            return
        acc: list[str] = []
        for part in parts:
            parent = "\\".join(acc)
            acc.append(part)
            current = "\\".join(acc)
            self._children.setdefault(parent, set()).add(current)
            self._children.setdefault(current, set())
        path = "\\".join(parts)
        if entry.get("locked"):
            self._locked.add(path)
        files = []
        for item in entry.get("files") or []:
            filename = str(item.get("filename") or "")
            tail = filename.replace("/", "\\").split("\\")[-1] or filename
            if not tail:
                continue
            files.append(
                {
                    "filename": filename or tail,
                    "name": tail,
                    "size": int(item.get("size") or 0),
                    "extension": str(item.get("extension") or "").lstrip(".").lower(),
                }
            )
        self._files[path] = files

    def children(self, path: str = "") -> list[dict]:
        """Immediate folders, then files. Does not walk the rest of the share."""
        key = "\\".join(path_parts(path)) if path else ""
        rows = []
        for child in sorted(self._children.get(key, ())):
            rows.append(
                {
                    "kind": "dir",
                    "path": child,
                    "name": child.split("\\")[-1],
                    "locked": child in self._locked,
                    "files": len(self._files.get(child) or []),
                }
            )
        for item in self._files.get(key) or []:
            rows.append({"kind": "file", "path": key, **item})
        return rows


class BrowseCache:
    """One saved browse per username, next to the state database."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS browse_users (
                username TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            )
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def get(self, username: str) -> dict | None:
        key = (username or "").casefold()
        if not key:
            return None
        row = self._db.execute(
            "SELECT payload, fetched_at FROM browse_users WHERE username = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        payload["fetched_at"] = row[1]
        payload["username"] = username
        return payload

    def put(self, username: str, profile: dict, directories: list[dict]) -> dict:
        key = (username or "").strip()
        if not key:
            raise ValueError("Enter a Soulseek username.")
        fetched = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        body = {"profile": profile, "directories": directories}
        self._db.execute(
            """
            INSERT INTO browse_users (username, payload, fetched_at)
            VALUES (?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET payload = excluded.payload, fetched_at = excluded.fetched_at
            """,
            (key.casefold(), json.dumps(body), fetched),
        )
        self._db.commit()
        return {"username": key, "profile": profile, "directories": directories, "fetched_at": fetched}


def shares_from_reply(directories, locked=None) -> list[dict]:
    """Turn aioslsk directory objects into the dicts the tree and the cache store."""
    locked_names = set()
    for entry in locked or []:
        name = str(getattr(entry, "name", "") or "")
        if name:
            locked_names.add(name)
    rows = []
    seen = set()
    for entry in list(directories or []) + list(locked or []):
        name = str(getattr(entry, "name", "") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        files = []
        for item in list(getattr(entry, "files", None) or []):
            filename = str(getattr(item, "filename", "") or "")
            if not filename:
                continue
            files.append(
                {
                    "filename": filename,
                    "size": int(getattr(item, "filesize", 0) or getattr(item, "size", 0) or 0),
                    "extension": str(getattr(item, "extension", "") or ""),
                }
            )
        rows.append({"name": name, "locked": name in locked_names, "files": files})
    return rows


def profile_from_reply(info, status, stats) -> dict:
    permissions = getattr(info, "upload_permissions", -1)
    state = getattr(status, "status", -1)
    return {
        "description": str(getattr(info, "description", "") or ""),
        "has_picture": bool(getattr(info, "picture", None)),
        "has_slots_free": bool(getattr(info, "has_slots_free", False)),
        "upload_slots": int(getattr(info, "upload_slots", 0) or 0),
        "queue_length": int(getattr(info, "queue_length", 0) or 0),
        "upload_permissions": _as_int(getattr(permissions, "value", permissions), -1),
        "status": _as_int(getattr(state, "value", state), -1),
        "privileged": bool(getattr(status, "privileged", False)),
        "avg_speed": int(getattr(stats, "avg_speed", 0) or 0),
        "uploads": int(getattr(stats, "uploads", 0) or 0),
        "shared_file_count": int(getattr(stats, "shared_file_count", 0) or 0),
        "shared_folder_count": int(getattr(stats, "shared_folder_count", 0) or 0),
    }


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
