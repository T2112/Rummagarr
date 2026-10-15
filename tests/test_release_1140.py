"""Rummagarr 1.14.0: Lidarr bridge, upgrades, health, notifications, and dry run."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx

from changelog_notes import changelog_section
from config_loader import default_config, save_config, try_load_config
from library_report import (
    delete_inside,
    edition_note,
    format_summary,
    health_from_albums,
    merge_upgrade_albums,
    queued_next,
    research_note,
    scan_folder,
    wanted_summary,
)
from lidarr_bridge import BridgeService, GrabBook, WEBAPI_VERSION, bridge_settings
from lidarr_register import register_with_lidarr
from notifications import deliver
from tests.conftest import album, make_worker
from wanted import build_catalog, items_to_rows


def _request(method: str, url: str, data: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _bridge(tmp_path: Path, *, dry_run: bool = False, local_only: bool = True):
    calls = []

    def search(artist, album, query):
        calls.append((artist, album, query))
        return [
            {
                "artist": artist or "Ella Langley",
                "album": album or "Hungover",
                "username": "goodpeer",
                "size": 2048,
                "files": [{"filename": "Ella/Hungover/01 Blue.flac", "size": 2048}],
                "score": 40,
            }
        ]

    added = []
    book = GrabBook(tmp_path / "grabs.sqlite3")
    settings = bridge_settings(
        save_path=str(tmp_path / "complete"),
        api_key="secret",
        username="rummagarr",
        password="bridge-pass",
        category="lidarr",
        dry_run=dry_run,
        public_base="http://127.0.0.1",
        local_only=local_only,
    )
    service = BridgeService("127.0.0.1", 0, book, settings, search, added.append)
    settings["public_base"] = f"http://127.0.0.1:{service.port}"
    service.start()
    return service, calls, added


def test_torznab_and_qbittorrent_contract(tmp_path: Path):
    service, calls, added = _bridge(tmp_path)
    base = f"http://127.0.0.1:{service.port}"
    try:
        status, _headers, body = _request("GET", f"{base}/api?t=caps&apikey=nope")
        assert status == 403
        assert b'code="100"' in body

        status, _headers, body = _request("GET", f"{base}/api?t=caps&apikey=secret")
        assert status == 200
        assert b'id="3010"' in body
        assert b"audio-search" in body

        status, _headers, body = _request(
            "GET", f"{base}/api?t=music&apikey=secret&artist=Ella%20Langley&album=Hungover"
        )
        assert status == 200
        assert calls == [("Ella Langley", "Hungover", "")]
        text = body.decode("utf-8")
        assert "magneturl" in text
        assert "infohash" in text
        assert "application/x-bittorrent" in text
        digest = text.split('name="infohash" value="', 1)[1].split('"', 1)[0]

        status, _headers, torrent = _request("GET", f"{base}/download/{digest}.torrent?apikey=secret")
        assert status == 200
        assert torrent.startswith(b"d")

        status, headers, body = _request(
            "POST",
            f"{base}/api/v2/auth/login",
            data=b"username=rummagarr&password=bridge-pass",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert status == 200
        assert body == b"Ok."
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        auth = {"Cookie": cookie}

        status, _headers, body = _request("GET", f"{base}/api/v2/app/webapiVersion")
        assert body.decode() == WEBAPI_VERSION

        status, _headers, body = _request("GET", f"{base}/api/v2/app/preferences", headers=auth)
        prefs = json.loads(body)
        assert prefs["dht"] is True
        assert prefs["max_ratio_act"] == 0

        status, _headers, body = _request(
            "POST",
            f"{base}/api/v2/torrents/add",
            data=f"urls=magnet:?xt=urn:btih:{digest}&category=lidarr".encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded", **auth},
        )
        assert body == b"Ok."
        assert added and added[0]["hash"] == digest

        status, _headers, body = _request("GET", f"{base}/api/v2/torrents/info?category=lidarr", headers=auth)
        rows = json.loads(body)
        assert rows[0]["state"] == "queuedDL"
        assert rows[0]["category"] == "lidarr"
        assert rows[0]["ratio_limit"] == 0
        assert rows[0]["seeding_time_limit"] == -2
        assert rows[0]["content_path"] != rows[0]["save_path"]

        status, _headers, body = _request("GET", f"{base}/api/v2/torrents/info?category=other", headers=auth)
        assert json.loads(body) == []

        service.book.update(digest, state="pausedUP", progress=1)
        status, _headers, body = _request("GET", f"{base}/api/v2/torrents/info?hash={digest.upper()}", headers=auth)
        finished = json.loads(body)[0]
        assert finished["state"] == "pausedUP"
        assert finished["progress"] == 1
        assert finished["ratio"] == 0

        status, _headers, body = _request(
            "POST",
            f"{base}/api/v2/torrents/add",
            data=b"urls=magnet:?xt=urn:btih:" + b"ab" * 20,
            headers={"Content-Type": "application/x-www-form-urlencoded", **auth},
        )
        assert body == b"Fails."

        boundary = "bound"
        payload = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="torrents"; filename="a.torrent"\r\n'
            "Content-Type: application/x-bittorrent\r\n\r\n"
        ).encode() + torrent + f"\r\n--{boundary}--\r\n".encode()
        status, _headers, body = _request(
            "POST",
            f"{base}/api/v2/torrents/add",
            data=payload,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}", **auth},
        )
        assert body == b"Ok."

        status, _headers, body = _request(
            "POST",
            f"{base}/api/v2/torrents/delete",
            data=f"hashes={digest}&deleteFiles=true".encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded", **auth},
        )
        assert body == b"Ok."
        status, _headers, body = _request("GET", f"{base}/api/v2/torrents/info", headers=auth)
        assert json.loads(body) == []
    finally:
        service.stop()


def test_dry_run_grab_stays_paused(tmp_path: Path):
    service, _calls, added = _bridge(tmp_path, dry_run=True)
    base = f"http://127.0.0.1:{service.port}"
    try:
        _status, _headers, body = _request("GET", f"{base}/api?t=search&apikey=secret&q=Ella%20Langley")
        digest = body.decode().split('name="infohash" value="', 1)[1].split('"', 1)[0]
        _status, headers, _body = _request(
            "POST",
            f"{base}/api/v2/auth/login",
            data=b"username=rummagarr&password=bridge-pass",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        _status, _headers, body = _request(
            "POST",
            f"{base}/api/v2/torrents/add",
            data=f"urls=magnet:?xt=urn:btih:{digest}".encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        assert body == b"Ok."
        assert added[0]["state"] == "pausedDL"
        _status, _headers, body = _request("GET", f"{base}/api/v2/torrents/info", headers={"Cookie": cookie})
        assert json.loads(body)[0]["state"] == "pausedDL"
    finally:
        service.stop()


def test_register_updates_an_existing_rummagarr_entry():
    seen = {}

    class Fake:
        def indexer_schema(self):
            return [
                {
                    "implementation": "Torznab",
                    "fields": [
                        {"name": "baseUrl", "value": ""},
                        {"name": "apiPath", "value": ""},
                        {"name": "apiKey", "value": ""},
                        {"name": "categories", "value": []},
                        {"name": "minimumSeeders", "value": 1},
                    ],
                }
            ]

        def download_client_schema(self):
            return [
                {
                    "implementation": "QBittorrent",
                    "fields": [
                        {"name": "host", "value": ""},
                        {"name": "port", "value": 0},
                        {"name": "username", "value": ""},
                        {"name": "password", "value": ""},
                        {"name": "musicCategory", "value": ""},
                    ],
                }
            ]

        def indexers(self):
            return [{"name": "Rummagarr", "id": 4}]

        def download_clients(self):
            return []

        def update_indexer(self, indexer_id, body):
            seen["indexer"] = (indexer_id, body)

        def add_indexer(self, body):
            raise AssertionError("existing indexer should be updated")

        def add_download_client(self, body):
            seen["client"] = body

        def update_download_client(self, client_id, body):
            raise AssertionError("no existing client")

    note = register_with_lidarr(
        Fake(),
        base_url="http://127.0.0.1:8788",
        api_key="secret",
        host="127.0.0.1",
        port=8788,
        username="rummagarr",
        password="bridge-pass",
        category="lidarr",
    )
    assert "updated the indexer" in note
    assert "added the download client" in note
    assert seen["indexer"][0] == 4
    assert seen["indexer"][1]["name"] == "Rummagarr"
    assert seen["indexer"][1]["enableInteractiveSearch"] is True
    assert seen["client"]["name"] == "Rummagarr"
    assert seen["client"]["removeCompletedDownloads"] is True
    names = {field["name"]: field["value"] for field in seen["client"]["fields"]}
    assert names["host"] == "127.0.0.1"
    assert names["musicCategory"] == "lidarr"


def test_notifications_post_and_skip_empty(tmp_path: Path):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.method, str(request.url), request.headers.get("Title"), request.content))
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = default_config(
        lidarr_api_key="key",
        slsk_username="user",
        slsk_password="pass",
        notify_discord_url="http://hooks.test/discord",
        notify_ntfy_url="http://ntfy.test/music",
        plex_url="http://plex.test",
        plex_token="token",
        plex_section="2",
        jellyfin_url="http://jellyfin.test",
        jellyfin_api_key="jelly",
        notify_on_import=False,
    )
    assert deliver(cfg, "import", "Imported", "Ella", client=client) == []
    lines = deliver(cfg, "test", "Rummagarr test", "hello", client=client)
    assert any(line.startswith("Discord") for line in lines)
    assert any(line.startswith("ntfy") for line in lines)
    assert any(line.startswith("Plex") for line in lines)
    urls = [item[1] for item in sent]
    assert any("/library/sections/2/refresh" in url for url in urls)
    assert any(url.endswith("/Library/Refresh") for url in urls)
    assert sent[1][2] == "Rummagarr test"
    client.close()


def test_health_scan_and_dashboard(tmp_path: Path):
    root = tmp_path / "library"
    root.mkdir()
    (root / "loose.mp3").write_bytes(b"id3")
    review = root / "Review"
    review.mkdir()
    (review / "Ella Langley").mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    rows = scan_folder(root)
    kinds = {row["kind"] for row in rows}
    assert kinds == {"unmapped", "review"}
    try:
        delete_inside(root, outside)
    except ValueError as exc:
        assert "outside" in str(exc)
    else:
        raise AssertionError("delete should refuse a path outside the folder")
    delete_inside(root, root / "loose.mp3")
    assert not (root / "loose.mp3").exists()

    albums = [
        album(1, "Ella Langley", "Hungover", files=0, total=10),
        album(2, "Ella Langley", "Live at Home", files=8, total=8),
    ]
    albums[0]["tags"] = ["country"]
    albums[0]["releaseDate"] = "2024-01-01"
    albums[1]["albumType"] = "Album"
    albums[1]["releases"] = [{"title": "Live at Home"}]
    health = health_from_albums(albums)
    assert any(row["kind"] == "missing" and "10 missing" in row["detail"] for row in health)
    assert edition_note(albums[1])
    assert any(row["kind"] == "edition" for row in health)

    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    finished = [
        {"artist": "Ella Langley", "title": "Hungover", "finished_at": "2026-10-14T12:00:00+00:00"},
        {"artist": "Ella Langley", "title": "Hungover", "finished_at": "2026-10-14T18:00:00+00:00"},
    ]
    summary = wanted_summary(albums, finished, ["Ella Langley"], now=now)
    assert summary["missing_albums"] >= 1
    assert summary["by_artist"][0][0] == "Ella Langley"
    assert summary["by_tag"][0][0] == "country"
    assert summary["by_decade"]
    assert summary["finished_albums_7d"] == 1
    assert summary["eta_days"] is not None
    text = format_summary(summary)
    assert "Queued next" in text
    assert "1. Ella Langley" in text

    worker = make_worker(tmp_path)
    try:
        catalog = build_catalog([album(1, "Ella Langley", "Hungover", files=0, total=10)], [])
        worker.store.write_wanted_page(items_to_rows(catalog))
        assert queued_next(worker.store, worker.cfg, 3)[0] == "Ella Langley"
        assert worker.store.wanted_albums()[0]["title"] == "Hungover"
        note = research_note(worker.store, {"kind": "missing", "artist": "Ella Langley", "album": "Hungover", "album_id": 1})
        assert "Ella Langley" in note
        assert worker.store.get(1)["status"] == "not_sent"
    finally:
        worker.close()


def test_config_round_trip_keeps_instances_bridge_and_dry_run(tmp_path: Path):
    home = str(tmp_path / "scan-a")
    cabin = str(tmp_path / "scan-b")
    cfg = default_config(
        lidarr_url="http://lidarr.home:8686",
        lidarr_api_key="home-key",
        lidarr_scan_path=home,
        slsk_username="souluser",
        slsk_password="soulpass",
        download_dir=tmp_path / "in",
        complete_dir=tmp_path / "out",
        state_db=tmp_path / "state.sqlite3",
        log_file=tmp_path / "lidarr-soulseek.log",
        lidarr_active="Cabin",
        lidarr_instances=[
            {"name": "Home", "url": "http://lidarr.home:8686", "api_key": "home-key", "scan_path": home},
            {"name": "Cabin", "url": "http://lidarr.cabin:8686", "api_key": "cabin-key", "scan_path": cabin},
        ],
        bridge_enabled=True,
        bridge_port=8791,
        bridge_api_key="bridge-key",
        bridge_password="bridge-pass",
        bridge_advertise_host="100.64.1.8",
        bridge_remote_path=r"D:\Media\Soulseek",
        dry_run=True,
        quality_upgrades=True,
        quality_upgrade_hours=12,
        notify_discord_url="http://hooks.test/discord",
        notify_on_review=False,
    )
    path = tmp_path / "config.toml"
    save_config(path, cfg)
    loaded = try_load_config(path)
    assert loaded.lidarr_active == "Cabin"
    assert loaded.lidarr_url == "http://lidarr.cabin:8686"
    assert loaded.lidarr_api_key == "cabin-key"
    assert [item["name"] for item in loaded.lidarr_instances] == ["Home", "Cabin"]
    assert loaded.bridge_enabled is True
    assert loaded.bridge_port == 8791
    assert loaded.bridge_api_key == "bridge-key"
    assert loaded.bridge_advertise_host == "100.64.1.8"
    assert loaded.bridge_remote_path == r"D:\Media\Soulseek"
    assert loaded.dry_run is True
    assert loaded.quality_upgrades is True
    assert loaded.quality_upgrade_hours == 12
    assert loaded.notify_discord_url == "http://hooks.test/discord"
    assert loaded.notify_on_review is False


def test_merge_upgrade_albums_and_worker_dry_run(tmp_path: Path):
    missing = [album(1, "Ella Langley", "Hungover")]
    upgrades = [album(9, "Ella Langley", "Better"), album(8, "Other", "Skip")]
    merged = merge_upgrade_albums("Ella Langley", missing, upgrades)
    assert [item["id"] for item in merged] == [1, 9]

    worker = make_worker(tmp_path)
    try:
        assert worker.bridge_port() == 0
        worker.cfg = replace(worker.cfg, dry_run=True)

        class Best:
            username = "peer"
            files = [object(), object()]

        outcome, detail = asyncio.run(worker._download_one(Best(), {"id": 1}, "k", 1, "Ella", "Hungover", None, []))
        assert outcome == "halt"
        assert "would grab 2" in detail
        assert worker.store.get(1)["status"] == "skipped"
        assert worker.store.dry_run_notes()

        worker.cfg = replace(worker.cfg, quality_upgrades=True, dry_run=False)
        worker.lidarr.wanted_cutoff = lambda: [album(9, "Ella Langley", "Better")]
        assert worker._or_upgrade_focus(None) == "Ella Langley"
        assert worker._with_upgrades("Ella Langley", [])[0]["id"] == 9
        worker.cfg = replace(worker.cfg, quality_upgrades=False)
        assert worker._or_upgrade_focus(None) is None
    finally:
        worker.close()


def test_changelog_mentions_the_qbittorrent_bridge():
    text = Path("CHANGELOG.md").read_text(encoding="utf-8")
    section = changelog_section(text, "1.14.0")
    assert "qBittorrent" in section
    assert "Torznab" in section
    assert "Tailscale" in section
    assert "1.13.0" not in section


def test_remote_bridge_requires_the_api_key(tmp_path: Path):
    service, _calls, _added = _bridge(tmp_path, local_only=False)
    base = f"http://127.0.0.1:{service.port}"
    try:
        status, _headers, body = _request("GET", f"{base}/api/v2/app/webapiVersion")
        assert status == 200
        assert body.decode() == WEBAPI_VERSION
        status, _headers, body = _request("GET", f"{base}/api/v2/app/version")
        assert status == 200

        status, _headers, body = _request("GET", f"{base}/api/v2/app/preferences")
        assert status == 403

        status, _headers, body = _request(
            "GET", f"{base}/api/v2/app/preferences", headers={"X-Api-Key": "secret"}
        )
        assert status == 200
        assert "save_path" in json.loads(body)

        status, _headers, body = _request(
            "POST",
            f"{base}/api/v2/auth/login",
            data=b"username=rummagarr&password=bridge-pass",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert body == b"Fails."

        status, headers, body = _request(
            "POST",
            f"{base}/api/v2/auth/login",
            data=b"username=rummagarr&password=secret",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert body == b"Ok."
        assert headers["Set-Cookie"].startswith("SID=")

        status, _headers, body = _request("GET", f"{base}/api/v2/torrents/info?apikey=secret")
        assert status == 200
        assert json.loads(body) == []
    finally:
        service.stop()


def test_advertise_host_tailscale_and_remote_path(tmp_path: Path):
    from bridge_net import (
        advertise_host,
        bind_choices,
        child_path,
        clean_host,
        firewall_powershell,
        is_tailscale,
        list_ipv4_interfaces,
        mapped_folder,
        reported_root,
        windows_firewall_prompt,
    )
    from lidarr_bridge import offer_from_release

    nics = [("eth0", "192.168.1.9"), ("tailscale0", "100.64.1.8"), ("lo", "127.0.0.1")]
    assert advertise_host("0.0.0.0", "http://100.1.2.3:8788/api", nics) == "100.1.2.3"
    assert advertise_host("0.0.0.0", "", nics) == "100.64.1.8"
    assert advertise_host("192.168.1.9", "", nics) == "192.168.1.9"
    assert advertise_host("127.0.0.1", "", nics) == "127.0.0.1"
    assert advertise_host("0.0.0.0", "", [("eth0", "10.1.1.5")]) == "10.1.1.5"
    assert is_tailscale("100.127.255.1")
    assert not is_tailscale("100.128.0.1")
    assert clean_host("http://100.1.2.3:8788") == "100.1.2.3"
    labels = dict(bind_choices(nics))
    assert labels["127.0.0.1"].startswith("This PC only")
    assert labels["0.0.0.0"] == "All interfaces"
    assert "Tailscale" in labels["100.64.1.8"]

    local = tmp_path / "Ella - Hungover"
    assert mapped_folder(local, tmp_path, r"D:\Media\Soulseek") == r"D:\Media\Soulseek\Ella - Hungover"
    assert mapped_folder(local, tmp_path, r"\\lidarr\incoming") == r"\\lidarr\incoming\Ella - Hungover"
    assert mapped_folder(local, tmp_path, "/srv/music") == f"/srv/music/{local.name}"
    assert mapped_folder(local, tmp_path, "") == str(local.resolve())
    root = reported_root(tmp_path, r"D:\Media\Soulseek")
    album_path = mapped_folder(local, tmp_path, r"D:\Media\Soulseek")
    assert album_path != root
    row, _torrent = offer_from_release(
        {"artist": "Ella", "album": "Hungover", "username": "peer", "size": 10, "files": []},
        save_path=r"D:\Media\Soulseek",
        category="lidarr",
    )
    assert row["content_path"] == child_path(r"D:\Media\Soulseek", "Ella - Hungover")
    assert row["content_path"] != row["save_path"]
    assert "\\" in row["content_path"]

    script = firewall_powershell(8788)
    assert "Rummagarr Bridge 8788" in script
    assert "localport=8788" in script
    assert "program=" in firewall_powershell(8788, r"C:\Program\Rummagarr.exe")
    opened, message = windows_firewall_prompt(8788)
    assert opened is False
    assert "Windows" in message

    firewall = Path("open-firewall.ps1").read_text(encoding="utf-8")
    assert "BridgePort" in firewall
    assert "Write-Error" not in firewall
    assert "Rummagarr.exe" in firewall
    installer = Path("packaging/LidarrSoulseek.iss").read_text(encoding="utf-8")
    assert "open-firewall.ps1" in installer
    assert "WizardIsTaskSelected('firewall')" in installer
    assert "PrivilegesRequired=lowest" in installer

    rows = list_ipv4_interfaces()
    assert isinstance(rows, list)
    assert all(len(item) == 2 and isinstance(item[0], str) and isinstance(item[1], str) for item in rows)
