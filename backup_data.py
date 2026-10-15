"""A zip of the settings and the local database. Media folders are not included.

Restore accepts only the names this app writes. A member with a folder, a
parent path, or an unknown name is skipped so a zip cannot write outside the
data folder.
"""

from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

_EXACT = {"config.toml", "state.sqlite3", "state.sqlite3-wal", "state.sqlite3-shm", "lidarr-soulseek.log"}


def safe_member(name: str) -> str | None:
    text = (name or "").replace("\\", "/").strip()
    if not text or text.endswith("/"):
        return None
    if "/" in text or text.startswith(".") or ".." in text.split("/"):
        return None
    if text in _EXACT:
        return text
    if text.endswith("-bridge.sqlite3") or text.endswith("-browse.sqlite3"):
        stem = text.rsplit("-", 1)[0]
        if stem and all(ch.isalnum() or ch in "._-" for ch in text):
            return text
    return None


def _snapshot_sqlite(src: Path, dest: Path) -> None:
    source = sqlite3.connect(src)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def create_backup(dest: Path, *, config_path: Path, state_db: Path, log_file: Path) -> list[str]:
    """Write a zip. Returns the member names that were included."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    members: list[tuple[str, Path]] = []
    if Path(config_path).is_file():
        members.append(("config.toml", Path(config_path)))
    db = Path(state_db)
    if db.is_file():
        members.append(("state.sqlite3", db))
        for extra_suffix in ("-bridge.sqlite3", "-browse.sqlite3"):
            extra = db.with_name(db.stem + extra_suffix)
            if extra.is_file():
                members.append((f"{db.stem}{extra_suffix}", extra))
    log = Path(log_file)
    if log.is_file():
        members.append(("lidarr-soulseek.log", log))
    if not members:
        raise ValueError("Nothing to back up yet. Save settings first.")
    written = []
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, path in members:
            if safe_member(name) != name:
                continue
            if path.suffix == ".sqlite3" or name.endswith(".sqlite3"):
                snapshot = dest.with_suffix(dest.suffix + ".tmp-sqlite")
                try:
                    _snapshot_sqlite(path, snapshot)
                    archive.write(snapshot, arcname=name)
                except sqlite3.Error:
                    archive.write(path, arcname=name)
                finally:
                    snapshot.unlink(missing_ok=True)
            else:
                archive.write(path, arcname=name)
            written.append(name)
    return written


def restore_backup(zip_path: Path, *, config_path: Path, state_db: Path, log_file: Path) -> list[str]:
    """Copy allowed members into the current data files. Returns what was written."""
    archive_path = Path(zip_path)
    if not archive_path.is_file():
        raise ValueError("Choose a Rummagarr backup zip.")
    targets = {
        "config.toml": Path(config_path),
        "state.sqlite3": Path(state_db),
        "state.sqlite3-wal": Path(str(state_db) + "-wal"),
        "state.sqlite3-shm": Path(str(state_db) + "-shm"),
        "lidarr-soulseek.log": Path(log_file),
    }
    db = Path(state_db)
    written = []
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            name = safe_member(info.filename)
            if name is None:
                continue
            if name in targets:
                dest = targets[name]
            elif name.endswith("-bridge.sqlite3") or name.endswith("-browse.sqlite3"):
                dest = db.with_name(name)
            else:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(archive.read(info))
            if name == "state.sqlite3":
                Path(str(dest) + "-wal").unlink(missing_ok=True)
                Path(str(dest) + "-shm").unlink(missing_ok=True)
            written.append(name)
    if not written:
        raise ValueError("That zip has no Rummagarr settings or database.")
    return written
