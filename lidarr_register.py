"""Register Rummagarr's Torznab indexer and qBittorrent client with Lidarr.

The field names come from Lidarr's schema. Unknown fields are left alone, so
a newer Lidarr still accepts the body.
"""

from __future__ import annotations

import copy
from typing import Any


class LidarrRegisterError(RuntimeError):
    pass


def fill_fields(definition: dict, values: dict[str, Any]) -> dict:
    body = copy.deepcopy(definition)
    for field in body.get("fields") or []:
        name = field.get("name")
        if name in values:
            field["value"] = values[name]
    return body


def indexer_body(schema: dict, *, base_url: str, api_key: str) -> dict:
    body = fill_fields(
        schema,
        {
            "baseUrl": base_url.rstrip("/"),
            "apiPath": "/api",
            "apiKey": api_key,
            "categories": [3000, 3010],
            "minimumSeeders": 1,
        },
    )
    body["name"] = "Rummagarr"
    body["enableRss"] = False
    body["enableAutomaticSearch"] = True
    body["enableInteractiveSearch"] = True
    body["priority"] = 25
    return body


def client_body(schema: dict, *, host: str, port: int, username: str, password: str, category: str) -> dict:
    body = fill_fields(
        schema,
        {
            "host": host,
            "port": int(port),
            "useSsl": False,
            "urlBase": "",
            "username": username,
            "password": password,
            "musicCategory": category,
            "musicImportedCategory": "",
            "recentMusicPriority": 0,
            "olderMusicPriority": 0,
            "initialState": 0,
            "sequentialOrder": False,
            "firstAndLast": False,
            "contentLayout": 0,
        },
    )
    body["name"] = "Rummagarr"
    body["enable"] = True
    body["priority"] = 1
    body["removeCompletedDownloads"] = True
    body["removeFailedDownloads"] = True
    return body


def _named(rows: list[dict], name: str) -> dict | None:
    for row in rows:
        if str(row.get("name") or "") == name:
            return row
    return None


def _implementation(rows: list[dict], name: str) -> dict:
    for row in rows:
        if row.get("implementation") == name:
            return row
    raise LidarrRegisterError(f"Lidarr has no {name} entry in its schema.")


def register_with_lidarr(client, *, base_url: str, api_key: str, host: str, port: int, username: str, password: str, category: str) -> str:
    """Create or update both Lidarr entries. ``client`` is a LidarrClient."""
    indexers = client.indexer_schema()
    clients = client.download_client_schema()
    if not isinstance(indexers, list) or not isinstance(clients, list):
        raise LidarrRegisterError("Lidarr did not return indexer and download client schemas.")
    indexer = indexer_body(_implementation(indexers, "Torznab"), base_url=base_url, api_key=api_key)
    download = client_body(
        _implementation(clients, "QBittorrent"),
        host=host,
        port=port,
        username=username,
        password=password,
        category=category,
    )
    current_indexers = client.indexers() or []
    current_clients = client.download_clients() or []
    existing_indexer = _named(current_indexers, "Rummagarr")
    existing_client = _named(current_clients, "Rummagarr")
    if existing_indexer and existing_indexer.get("id"):
        indexer["id"] = existing_indexer["id"]
        client.update_indexer(int(existing_indexer["id"]), indexer)
        indexer_note = "updated the indexer"
    else:
        client.add_indexer(indexer)
        indexer_note = "added the indexer"
    if existing_client and existing_client.get("id"):
        download["id"] = existing_client["id"]
        client.update_download_client(int(existing_client["id"]), download)
        client_note = "updated the download client"
    else:
        client.add_download_client(download)
        client_note = "added the download client"
    return f"Lidarr {indexer_note} and {client_note}."
