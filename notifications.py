"""Discord, ntfy, and a library refresh for Plex or Jellyfin.

Empty URLs are skipped. A test send uses the same path as a real import.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx

log = logging.getLogger("rummagarr")


def deliver(cfg, kind: str, title: str, body: str, *, client: httpx.Client | None = None) -> list[str]:
    """Post the event. Returns one line per destination, including skips and errors."""
    kind = (kind or "test").casefold()
    if kind == "import" and not getattr(cfg, "notify_on_import", True):
        return []
    if kind == "review" and not getattr(cfg, "notify_on_review", True):
        return []
    text = f"{title}\n{body}".strip()
    own = client is None
    http = client or httpx.Client(timeout=10)
    lines: list[str] = []
    try:
        discord = str(getattr(cfg, "notify_discord_url", "") or "").strip()
        ntfy = str(getattr(cfg, "notify_ntfy_url", "") or "").strip()
        if discord:
            lines.append(_post_discord(http, discord, text[:1900]))
        if ntfy:
            lines.append(_post_ntfy(http, ntfy, title, body or title))
        if kind in {"import", "review", "test"}:
            lines.extend(_refresh_libraries(http, cfg))
    finally:
        if own:
            http.close()
    return [line for line in lines if line]


def _post_discord(http: httpx.Client, url: str, content: str) -> str:
    try:
        response = http.post(url, json={"content": content})
        response.raise_for_status()
    except Exception as exc:
        log.warning("Discord notification failed: %s", exc)
        return f"Discord failed: {exc}"
    return f"Discord {response.status_code}"


def _post_ntfy(http: httpx.Client, url: str, title: str, body: str) -> str:
    try:
        response = http.post(
            url,
            content=(body or title).encode("utf-8"),
            headers={"Title": title[:200], "Content-Type": "text/plain; charset=utf-8"},
        )
        response.raise_for_status()
    except Exception as exc:
        log.warning("ntfy notification failed: %s", exc)
        return f"ntfy failed: {exc}"
    return f"ntfy {response.status_code}"


def _refresh_libraries(http: httpx.Client, cfg) -> list[str]:
    lines = []
    plex = str(getattr(cfg, "plex_url", "") or "").strip().rstrip("/")
    token = str(getattr(cfg, "plex_token", "") or "").strip()
    if plex and token:
        section = str(getattr(cfg, "plex_section", "") or "").strip()
        path = f"/library/sections/{section}/refresh" if section else "/library/sections/all/refresh"
        lines.append(_get(http, f"{plex}{path}", params={"X-Plex-Token": token}, label="Plex"))
    jellyfin = str(getattr(cfg, "jellyfin_url", "") or "").strip().rstrip("/")
    jelly_key = str(getattr(cfg, "jellyfin_api_key", "") or "").strip()
    if jellyfin and jelly_key:
        lines.append(
            _post(
                http,
                f"{jellyfin}/Library/Refresh",
                headers={"X-Emby-Token": jelly_key},
                label="Jellyfin",
            )
        )
    return lines


def _get(http: httpx.Client, url: str, *, params: dict[str, Any], label: str) -> str:
    try:
        response = http.get(url, params=params)
        response.raise_for_status()
    except Exception as exc:
        log.warning("%s refresh failed: %s", label, exc)
        return f"{label} failed: {exc}"
    return f"{label} {response.status_code}"


def _post(http: httpx.Client, url: str, *, headers: dict[str, str], label: str) -> str:
    try:
        response = http.post(url, headers=headers)
        response.raise_for_status()
    except Exception as exc:
        log.warning("%s refresh failed: %s", label, exc)
        return f"{label} failed: {exc}"
    return f"{label} {response.status_code}"


def webhook_host(url: str) -> str:
    return urlparse(url).netloc or url
