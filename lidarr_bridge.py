"""Lidarr indexer and download client on one local port.

Lidarr already knows how to search a Torznab indexer and import a finished
album from qBittorrent. This process speaks those two APIs. A SABnzbd shim
would need a fake NZB for every grab. Synology Download Station's API is
smaller and Lidarr uses less of it, so a mistake there fails the import.
The qBittorrent Web API at version 2.11.2 is the path Lidarr's current
client tests: login, preferences, categories, add, the torrent list, files,
properties, and delete.

Search results are magnets whose info-hash points at a Soulseek folder.
Adding the magnet queues that folder. When the files are in the complete
folder the torrent is ``pausedUP`` with ``ratio_limit`` 0, which is the
state Lidarr moves (instead of copies) into the library.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import sqlite3
import threading
import time
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from bridge_net import child_path

log = logging.getLogger("lidarr_slsk")

WEBAPI_VERSION = "2.11.2"
APP_VERSION = "v4.6.0"
TRACKER = "udp://tracker.opentrackr.org:1337/announce"


def bencode(value) -> bytes:
    if isinstance(value, bool):
        raise TypeError("bool is not a bencode integer")
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii") + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode("ascii") + b":" + value
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return str(len(raw)).encode("ascii") + b":" + raw
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        parts = []
        for key in sorted(value):
            parts.append(bencode(str(key)))
            parts.append(bencode(value[key]))
        return b"d" + b"".join(parts) + b"e"
    raise TypeError(type(value))


def bdecode(data: bytes):
    value, end = _bdecode_at(data, 0)
    if end != len(data):
        raise ValueError("trailing data in torrent")
    return value


def _bdecode_at(data: bytes, index: int):
    kind = data[index:index + 1]
    if kind == b"i":
        end = data.index(b"e", index)
        return int(data[index + 1:end]), end + 1
    if kind == b"l":
        items = []
        index += 1
        while data[index:index + 1] != b"e":
            item, index = _bdecode_at(data, index)
            items.append(item)
        return items, index + 1
    if kind == b"d":
        mapping = {}
        index += 1
        while data[index:index + 1] != b"e":
            key, index = _bdecode_at(data, index)
            val, index = _bdecode_at(data, index)
            mapping[key.decode("utf-8") if isinstance(key, bytes) else key] = val
        return mapping, index + 1
    colon = data.index(b":", index)
    length = int(data[index:colon])
    start = colon + 1
    return data[start:start + length], start + length


def info_hash(info: dict) -> str:
    return hashlib.sha1(bencode(info)).hexdigest()


def release_torrent(name: str, size: int, token: str) -> tuple[str, bytes]:
    """A one-piece torrent whose hash is stable for this Soulseek folder."""
    piece = hashlib.sha1(b"rummagarr\0" + token.encode("utf-8")).digest()
    info = {
        "length": max(1, int(size or 1)),
        "name": name,
        "piece length": 16384,
        "pieces": piece,
        "rummagarr": token,
    }
    digest = info_hash(info)
    body = bencode({"announce": TRACKER, "info": info})
    return digest, body


def magnet_for(digest: str, name: str) -> str:
    return f"magnet:?xt=urn:btih:{digest}&dn={quote(name)}&tr={quote(TRACKER, safe='')}"


def torrent_hash(body: bytes) -> str:
    decoded = bdecode(body)
    info = decoded.get("info") if isinstance(decoded, dict) else None
    if not isinstance(info, dict):
        raise ValueError("torrent has no info dict")
    return info_hash(info)


def xml_error(code: int, description: str) -> bytes:
    text = html.escape(description)
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<error code="{int(code)}" description="{text}"/>\n'
    ).encode("utf-8")


def caps_xml() -> bytes:
    return """<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server version="1.1" title="Rummagarr" strapline="Soulseek for Lidarr"/>
  <limits max="100" default="25"/>
  <searching>
    <search available="yes" supportedParams="q"/>
    <audio-search available="yes" supportedParams="q,artist,album"/>
  </searching>
  <categories>
    <category id="3000" name="Audio">
      <subcat id="3010" name="Music"/>
    </category>
  </categories>
</caps>
""".encode("utf-8")


def feed_xml(items: list[dict], *, link_for) -> bytes:
    rows = []
    for item in items:
        title = html.escape(item["title"])
        digest = item["hash"]
        size = int(item.get("size") or 1)
        link = html.escape(link_for(digest), quote=True)
        magnet = html.escape(item["magnet"], quote=True)
        when = formatdate(time.time(), usegmt=True)
        rows.append(
            f"""    <item>
      <title>{title}</title>
      <guid isPermaLink="false">{digest}</guid>
      <pubDate>{when}</pubDate>
      <size>{size}</size>
      <link>{link}</link>
      <category>3000</category>
      <enclosure url="{link}" length="{size}" type="application/x-bittorrent"/>
      <torznab:attr name="category" value="3000"/>
      <torznab:attr name="size" value="{size}"/>
      <torznab:attr name="seeders" value="{int(item.get('seeders') or 1)}"/>
      <torznab:attr name="leechers" value="0"/>
      <torznab:attr name="peers" value="{int(item.get('seeders') or 1)}"/>
      <torznab:attr name="infohash" value="{digest}"/>
      <torznab:attr name="magneturl" value="{magnet}"/>
      <torznab:attr name="downloadvolumefactor" value="0"/>
      <torznab:attr name="uploadvolumefactor" value="1"/>
    </item>"""
        )
    body = "\n".join(rows)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <title>Rummagarr</title>
{body}
  </channel>
</rss>
""".encode("utf-8")


def preferences(save_path: str) -> dict:
    return {
        "save_path": save_path,
        "max_ratio_enabled": False,
        "max_ratio": 0,
        "max_seeding_time_enabled": False,
        "max_seeding_time": 0,
        "max_inactive_seeding_time_enabled": False,
        "max_inactive_seeding_time": 0,
        "max_ratio_act": 0,
        "queueing_enabled": True,
        "dht": True,
    }


def torrent_row(grab: dict) -> dict:
    return {
        "hash": grab["hash"],
        "name": grab["name"],
        "size": int(grab.get("size") or 1),
        "progress": float(grab.get("progress") or 0),
        "eta": int(grab.get("eta") or 0),
        "state": grab.get("state") or "downloading",
        "category": grab.get("category") or "lidarr",
        "ratio": 0,
        "ratio_limit": 0,
        "seeding_time_limit": -2,
        "inactive_seeding_time_limit": -2,
        "last_activity": int(grab.get("last_activity") or time.time()),
        "save_path": grab.get("save_path") or "",
        "content_path": grab.get("content_path") or "",
    }


class GrabBook:
    """Offers and grabs. Offers stay out of the qBittorrent list until Lidarr adds them."""

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, dict] = {}
        self._torrents: dict[str, bytes] = {}
        self._sid = ""
        self._db: sqlite3.Connection | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(path), check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS bridge_grabs (
                    hash TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    torrent BLOB
                )
                """
            )
            self._db.commit()
            self._load()

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def _load(self) -> None:
        assert self._db is not None
        for digest, payload, torrent in self._db.execute("SELECT hash, payload, torrent FROM bridge_grabs"):
            data = json.loads(payload)
            self._rows[digest] = data
            if torrent:
                self._torrents[digest] = torrent

    def _write(self, digest: str) -> None:
        if self._db is None:
            return
        row = self._rows.get(digest)
        if row is None:
            self._db.execute("DELETE FROM bridge_grabs WHERE hash = ?", (digest,))
        else:
            self._db.execute(
                """
                INSERT INTO bridge_grabs(hash, payload, torrent) VALUES(?, ?, ?)
                ON CONFLICT(hash) DO UPDATE SET payload = excluded.payload, torrent = excluded.torrent
                """,
                (digest, json.dumps(row), self._torrents.get(digest)),
            )
        self._db.commit()

    def offer(self, row: dict, torrent: bytes) -> None:
        digest = row["hash"]
        with self._lock:
            self._torrents[digest] = torrent
            current = self._rows.get(digest)
            if current is None or current.get("state") == "offered":
                self._rows[digest] = row
            self._write(digest)

    def get(self, digest: str) -> dict | None:
        with self._lock:
            row = self._rows.get((digest or "").lower())
            return None if row is None else dict(row)

    def torrent(self, digest: str) -> bytes | None:
        with self._lock:
            return self._torrents.get((digest or "").lower())

    def activate(self, digest: str, *, category: str, dry_run: bool) -> dict | None:
        with self._lock:
            row = self._rows.get((digest or "").lower())
            if row is None:
                return None
            if row.get("state") in {"pausedUP", "pausedDL", "downloading", "queuedDL", "error"}:
                return dict(row)
            row["category"] = category or row.get("category") or "lidarr"
            row["last_activity"] = int(time.time())
            if dry_run:
                row["state"] = "pausedDL"
                row["progress"] = 0
                row["message"] = "dry run: would download"
            else:
                row["state"] = "queuedDL"
                row["progress"] = 0
                row["message"] = ""
            self._rows[row["hash"]] = row
            self._write(row["hash"])
            return dict(row)

    def update(self, digest: str, **changes) -> None:
        with self._lock:
            row = self._rows.get((digest or "").lower())
            if row is None:
                return
            row.update(changes)
            row["last_activity"] = int(time.time())
            self._rows[row["hash"]] = row
            self._write(row["hash"])

    def listed(self, category: str = "") -> list[dict]:
        want = (category or "").strip()
        with self._lock:
            rows = []
            for row in self._rows.values():
                if row.get("state") == "offered":
                    continue
                if want and (row.get("category") or "") != want:
                    continue
                rows.append(dict(row))
        rows.sort(key=lambda item: item.get("added") or 0)
        return rows

    def delete(self, digest: str) -> None:
        key = (digest or "").lower()
        with self._lock:
            self._rows.pop(key, None)
            self._torrents.pop(key, None)
            if self._db is not None:
                self._db.execute("DELETE FROM bridge_grabs WHERE hash = ?", (key,))
                self._db.commit()

    def set_sid(self, sid: str) -> None:
        with self._lock:
            self._sid = sid

    def sid_ok(self, header: str) -> bool:
        with self._lock:
            expected = self._sid
        if not expected:
            return False
        for part in (header or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "SID" and value == expected:
                return True
        return False


def offer_from_release(release: dict, *, save_path: str, category: str) -> tuple[dict, bytes]:
    artist = str(release.get("artist") or "Unknown Artist")
    album = str(release.get("album") or "Unknown Album")
    username = str(release.get("username") or "")
    name = f"{artist} - {album}"
    files = list(release.get("files") or [])
    token = username + "\n" + "\n".join(str(item.get("filename") or "") for item in files)
    digest, torrent = release_torrent(name, int(release.get("size") or 1), token)
    folder = child_path(save_path, _safe_folder(name))
    row = {
        "hash": digest,
        "name": name,
        "title": name,
        "size": int(release.get("size") or 1),
        "progress": 0,
        "eta": 8640000,
        "state": "offered",
        "category": category or "lidarr",
        "save_path": save_path,
        "content_path": folder,
        "magnet": magnet_for(digest, name),
        "seeders": max(1, int(release.get("score") or 1)),
        "artist": artist,
        "album": album,
        "username": username,
        "files": files,
        "message": "",
        "added": time.time(),
        "last_activity": int(time.time()),
    }
    return row, torrent


def _safe_folder(name: str) -> str:
    cleaned = "".join(ch if ch not in '<>:"/\\|?*' else " " for ch in name).strip().rstrip(".")
    return cleaned or "Album"


def _parse_multipart(content_type: str, body: bytes) -> dict:
    marker = ""
    for part in content_type.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "boundary":
            marker = value.strip('"')
    found: dict[str, str | bytes] = {}
    if not marker:
        return found
    for chunk in body.split(b"--" + marker.encode("utf-8")):
        if b"Content-Disposition" not in chunk:
            continue
        header, _, payload = chunk.partition(b"\r\n\r\n")
        payload = payload.removesuffix(b"\r\n")
        if payload.endswith(b"--"):
            payload = payload[:-2]
        text = header.decode("utf-8", "replace")
        key = ""
        for piece in text.split(";"):
            left, _, right = piece.strip().partition("=")
            if left == "name":
                key = right.strip('"')
        if not key:
            continue
        if key == "torrents":
            found[key] = payload
        else:
            found[key] = payload.decode("utf-8", "replace").strip()
    return found


class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, book: GrabBook, settings: dict, search, on_add) -> None:
        self.book = book
        self.settings = settings
        self.search = search
        self.on_add = on_add
        super().__init__(server_address, BridgeHandler)


class BridgeHandler(BaseHTTPRequestHandler):
    server: BridgeHTTPServer

    def log_message(self, fmt: str, *args) -> None:
        log.debug("bridge " + fmt, *args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        if path == "/api":
            self._torznab(query)
            return
        if path.startswith("/download/") and path.endswith(".torrent"):
            self._torrent_file(path, query)
            return
        if path == "/api/v2/app/webapiVersion":
            self._text(WEBAPI_VERSION)
            return
        if path == "/api/v2/app/version":
            self._text(APP_VERSION)
            return
        if not self._qbit_auth(query):
            return
        if path == "/api/v2/app/preferences":
            self._json(preferences(self.server.settings["save_path"]))
            return
        if path == "/api/v2/torrents/categories":
            self._json(self._categories())
            return
        if path == "/api/v2/torrents/info":
            rows = [torrent_row(item) for item in self.server.book.listed(query.get("category", ""))]
            self._json(rows)
            return
        if path == "/api/v2/torrents/properties":
            self._properties(query.get("hash", ""))
            return
        if path == "/api/v2/torrents/files":
            self._files(query.get("hash", ""))
            return
        self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        form = self._form()
        if path == "/api/v2/auth/login":
            self._login(str(form.get("username") or ""), str(form.get("password") or ""))
            return
        if not self._qbit_auth(self._request_query()):
            return
        if path == "/api/v2/torrents/add":
            self._add(form)
            return
        if path == "/api/v2/torrents/delete":
            self._delete(str(form.get("hashes") or ""))
            return
        if path == "/api/v2/torrents/createCategory":
            name = str(form.get("category") or "").strip()
            if name:
                self.server.settings.setdefault("categories", {})[name] = ""
            self._text("Ok.")
            return
        if path in {
            "/api/v2/torrents/setCategory",
            "/api/v2/torrents/setShareLimits",
            "/api/v2/torrents/topPrio",
            "/api/v2/torrents/setForceStart",
        }:
            self._text("Ok.")
            return
        self.send_error(404)

    def _torznab(self, query: dict) -> None:
        if query.get("apikey") != self.server.settings["api_key"]:
            self._bytes(403, "application/xml", xml_error(100, "Missing or invalid API key"))
            return
        kind = (query.get("t") or "search").lower()
        if kind == "caps":
            self._bytes(200, "application/xml", caps_xml())
            return
        if kind not in {"search", "music"}:
            self._bytes(200, "application/xml", feed_xml([], link_for=self._download_link))
            return
        artist = query.get("artist") or ""
        album = query.get("album") or ""
        text = query.get("q") or ""
        if not artist and not album and text:
            artist, _, album = text.partition(" - ")
            if not album:
                artist, album = text, ""
        try:
            releases = self.server.search(artist.strip(), album.strip(), text.strip()) or []
        except Exception as exc:
            log.warning("Torznab search failed: %s", exc)
            self._bytes(200, "application/xml", xml_error(200, str(exc) or "Soulseek search failed"))
            return
        items = []
        for release in releases:
            row, torrent = offer_from_release(
                release,
                save_path=self.server.settings["save_path"],
                category=self.server.settings["category"],
            )
            self.server.book.offer(row, torrent)
            items.append(row)
        self._bytes(200, "application/xml", feed_xml(items, link_for=self._download_link))

    def _download_link(self, digest: str) -> str:
        base = self.server.settings["public_base"].rstrip("/")
        key = quote(self.server.settings["api_key"], safe="")
        return f"{base}/download/{digest}.torrent?apikey={key}"

    def _torrent_file(self, path: str, query: dict) -> None:
        if query.get("apikey") != self.server.settings["api_key"]:
            self._bytes(403, "text/plain", b"invalid api key")
            return
        digest = path.rsplit("/", 1)[-1].removesuffix(".torrent").lower()
        body = self.server.book.torrent(digest)
        if body is None:
            self.send_error(404)
            return
        self._bytes(200, "application/x-bittorrent", body)

    def _login(self, username: str, password: str) -> None:
        settings = self.server.settings
        key = settings.get("api_key") or ""
        # A bridge that is not limited to this PC accepts only the API key as the
        # qBittorrent password. Lidarr's version probe cannot send that key, so
        # webapiVersion stays open and every later call is behind this login.
        if settings.get("local_only", True):
            ok = username == settings["username"] and password in {settings["password"], key}
        else:
            ok = bool(key) and username == settings["username"] and password == key
        if not ok or not password:
            self._bytes(403, "text/plain", b"Fails.")
            return
        sid = hashlib.sha1(f"{username}:{time.time()}".encode()).hexdigest()
        self.server.book.set_sid(sid)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Set-Cookie", f"SID={sid}; Path=/")
        body = b"Ok."
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _request_query(self) -> dict:
        parsed = urlparse(self.path)
        return {key: values[-1] for key, values in parse_qs(parsed.query).items()}

    def _api_key_ok(self, query: dict) -> bool:
        """True when a non-local bridge was given the API key on this request."""
        if self.server.settings.get("local_only", True):
            return False
        key = self.server.settings.get("api_key") or ""
        if not key:
            return False
        if (query or {}).get("apikey") == key:
            return True
        return (self.headers.get("X-Api-Key") or "") == key

    def _qbit_auth(self, query: dict | None = None) -> bool:
        if self._api_key_ok(query or {}):
            return True
        if self.server.book.sid_ok(self.headers.get("Cookie") or ""):
            return True
        self._bytes(403, "text/plain", b"Forbidden")
        return False

    def _add(self, form: dict) -> None:
        urls = str(form.get("urls") or "")
        digest = ""
        if "btih:" in urls.lower():
            digest = urls.lower().split("btih:", 1)[1].split("&", 1)[0]
        elif isinstance(form.get("torrents"), bytes) and form.get("torrents"):
            try:
                digest = torrent_hash(form["torrents"])
            except Exception:
                self._text("Fails.")
                return
        digest = digest.strip().lower()
        if len(digest) != 40:
            self._text("Fails.")
            return
        category = str(form.get("category") or self.server.settings["category"])
        row = self.server.book.activate(digest, category=category, dry_run=bool(self.server.settings.get("dry_run")))
        if row is None:
            self._text("Fails.")
            return
        try:
            self.server.on_add(row)
        except Exception:
            log.exception("Could not start the Soulseek download for %s", digest)
            self.server.book.update(digest, state="error", message="Could not start the download")
        self._text("Ok.")

    def _delete(self, hashes: str) -> None:
        for digest in hashes.replace("|", "\n").split():
            self.server.book.delete(digest)
        self._text("Ok.")

    def _properties(self, digest: str) -> None:
        row = self.server.book.get(digest)
        if row is None or row.get("state") == "offered":
            self.send_error(404)
            return
        self._json({"hash": row["hash"], "save_path": row.get("save_path") or "", "seeding_time": 0})

    def _files(self, digest: str) -> None:
        row = self.server.book.get(digest)
        if row is None:
            self._json([])
            return
        folder = _safe_folder(row.get("name") or "Album")
        names = []
        for item in row.get("files") or []:
            filename = str(item.get("filename") or "track")
            tail = filename.replace("\\", "/").split("/")[-1]
            names.append({"name": f"{folder}/{tail}"})
        if not names:
            names.append({"name": f"{folder}/album.flac"})
        self._json(names)

    def _categories(self) -> dict:
        names = dict(self.server.settings.get("categories") or {})
        names.setdefault(self.server.settings["category"], "")
        return {name: {"name": name, "savePath": path} for name, path in names.items()}

    def _form(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type") or ""
        if "multipart/form-data" in ctype:
            return _parse_multipart(ctype, body)
        parsed = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    def _text(self, text: str) -> None:
        self._bytes(200, "text/plain; charset=utf-8", text.encode("utf-8"))

    def _json(self, payload) -> None:
        self._bytes(200, "application/json", json.dumps(payload).encode("utf-8"))

    def _bytes(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BridgeService:
    """The local Torznab and qBittorrent server. Search and download are callbacks."""

    def __init__(self, host: str, port: int, book: GrabBook, settings: dict, search, on_add) -> None:
        self._httpd = BridgeHTTPServer((host, int(port)), book, settings, search, on_add)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def book(self) -> GrabBook:
        return self._httpd.book

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="lidarr-bridge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._httpd.book.close()


def bridge_settings(
    *,
    save_path: str,
    api_key: str,
    username: str,
    password: str,
    category: str,
    dry_run: bool,
    public_base: str,
    local_only: bool = True,
) -> dict:
    """The dict the HTTP handler reads. ``password`` falls back to the API key."""
    key = api_key or ""
    return {
        "save_path": save_path,
        "api_key": key,
        "username": username or "rummagarr",
        "password": password or key,
        "category": category or "lidarr",
        "dry_run": bool(dry_run),
        "public_base": public_base,
        "local_only": bool(local_only),
        "categories": {},
    }
