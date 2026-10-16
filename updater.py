"""Check GitHub Releases and install a newer Windows setup program.

The installed app calls the public releases API with no token. A copy started
from source only reports that a newer release exists.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from config_loader import app_version
from process_launch import popen_hidden

# The repository is moving to T2112/rummagarr. Until that rename, the old URL
# still serves the release, so the check tries the new name and then the old one.
RELEASE_API_URLS = (
    "https://api.github.com/repos/T2112/rummagarr/releases/latest",
    "https://api.github.com/repos/T2112/lidarr-soulseek/releases/latest",
)
LATEST_RELEASE_URL = RELEASE_API_URLS[0]
RELEASES_PAGE_URL = "https://github.com/T2112/rummagarr/releases/latest"
SKIPPED_UPDATE_META = "skipped_update_version"
# New releases publish Rummagarr-Setup only. LidarrSoulseek-Setup is still
# accepted when an older release is the one GitHub returns.
INSTALLER_PREFIXES = ("Rummagarr-Setup-", "LidarrSoulseek-Setup-")
INSTALLER_PREFIX = INSTALLER_PREFIXES[0]


class UpdateError(Exception):
    """The check or the download could not be completed."""


@dataclass(frozen=True, order=True)
class AppVersion:
    """Numeric version. A pre-release sorts before the matching final release."""

    parts: tuple[int, ...]
    final: bool = True
    prerelease: tuple[tuple[int, int | str], ...] = ()

    def __str__(self) -> str:
        text = ".".join(str(part) for part in self.parts)
        if self.final:
            return text
        pre = ".".join(str(token[1]) for token in self.prerelease)
        return f"{text}-{pre}" if pre else text


def parse_version(value: str) -> AppVersion:
    """Parse ``1.4.0`` or ``v1.4.0``. ``1.10.0`` is newer than ``1.9.0``."""
    text = (value or "").strip()
    if text[:1] in {"v", "V"}:
        text = text[1:].strip()
    if not text:
        raise ValueError("Version is empty.")
    main, separator, pre = text.partition("-")
    if not separator:
        main, separator, pre = text.partition("+")
        pre = ""
    numbers: list[int] = []
    for piece in main.split("."):
        if not piece.isdigit():
            raise ValueError(f"Not a version: {value}")
        numbers.append(int(piece))
    if not numbers:
        raise ValueError(f"Not a version: {value}")
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    tokens: list[tuple[int, int | str]] = []
    if pre:
        for piece in pre.split("."):
            if not piece:
                continue
            if piece.isdigit():
                tokens.append((0, int(piece)))
            else:
                tokens.append((1, piece))
    return AppVersion(tuple(numbers), final=not tokens, prerelease=tuple(tokens))


def version_text(value: str) -> str:
    """``v1.4.0`` becomes ``1.4.0`` without changing the published spelling."""
    text = (value or "").strip()
    if text[:1] in {"v", "V"}:
        text = text[1:].strip()
    return text


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


def versions_equal(left: str, right: str) -> bool:
    try:
        return parse_version(left) == parse_version(right)
    except ValueError:
        return False


def installer_names_for(version: str) -> tuple[str, ...]:
    text = version_text(version)
    return tuple(f"{prefix}{text}.exe" for prefix in INSTALLER_PREFIXES)


def installer_name_for(version: str) -> str:
    return installer_names_for(version)[0]


def checksum_name_for(installer_name: str) -> str:
    return f"{installer_name}.sha256"


def running_installed() -> bool:
    """True when PyInstaller froze the desktop app. Source runs are not installed."""
    return bool(getattr(sys, "frozen", False))


def github_headers() -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"Rummagarr/{app_version()}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


@dataclass(frozen=True)
class UpdateStatus:
    current: str
    latest: str | None = None
    notes: str = ""
    page_url: str = RELEASES_PAGE_URL
    newer: bool = False
    skipped: bool = False
    error: str | None = None
    can_install: bool = False
    asset_name: str | None = None
    asset_url: str | None = None
    asset_size: int | None = None
    checksum_url: str | None = None


def prompt_kind(status: UpdateStatus, *, manual: bool) -> str:
    """Decide what to show after a check.

    ``prompt`` opens the update dialog. ``current`` and ``error`` are for a
    manual check. ``quiet`` means an automatic check should stay out of the way,
    including when the user already skipped this exact version.
    """
    if status.error:
        return "error" if manual else "quiet"
    if not status.newer:
        return "current" if manual else "quiet"
    if status.skipped and not manual:
        return "quiet"
    return "prompt"


def _asset_map(payload: dict) -> dict[str, dict]:
    assets = payload.get("assets") or []
    found: dict[str, dict] = {}
    if not isinstance(assets, list):
        return found
    for asset in assets:
        if isinstance(asset, dict) and asset.get("name"):
            found[str(asset["name"])] = asset
    return found


def _asset_url(asset: dict) -> str:
    url = asset.get("browser_download_url") or ""
    return str(url)


def _asset_size(asset: dict) -> int | None:
    try:
        size = int(asset.get("size"))
    except (TypeError, ValueError):
        return None
    if size < 0:
        return None
    return size


def parse_release(
    payload: dict,
    *,
    current: str,
    skipped: str | None,
    installed: bool,
    platform: str | None = None,
) -> UpdateStatus:
    if not isinstance(payload, dict):
        raise UpdateError("GitHub returned something that is not a release.")
    tag = str(payload.get("tag_name") or "").strip()
    if not tag:
        raise UpdateError("GitHub returned a release without a version tag.")
    try:
        latest_version = parse_version(tag)
        current_version = parse_version(current)
    except ValueError as exc:
        raise UpdateError(f"Could not compare versions: {exc}") from exc
    published = version_text(tag)
    page = str(payload.get("html_url") or f"https://github.com/T2112/rummagarr/releases/tag/{tag}")
    notes = str(payload.get("body") or "").strip()
    assets = _asset_map(payload)
    names = installer_names_for(published)
    name = names[0]
    installer = None
    for candidate in names:
        found = assets.get(candidate)
        if found:
            installer = found
            name = candidate
            break
    checksum = assets.get(checksum_name_for(name)) if installer else None
    newer = latest_version > current_version
    skipped_this = False
    if skipped and newer:
        try:
            skipped_this = parse_version(skipped) == latest_version
        except ValueError:
            skipped_this = False
    asset_url = _asset_url(installer) if installer else ""
    asset_size = _asset_size(installer) if installer else None
    system = platform or os.name
    error = None
    can_install = bool(installed and system == "nt" and installer and asset_url and asset_size is not None)
    if installed and newer and not can_install:
        if installer is None:
            error = f"Release {tag} has no {' or '.join(names)} to install."
        elif not asset_url or asset_size is None:
            error = f"Release {tag} is missing the download size for {name}."
        elif system != "nt":
            error = "Updates install only on Windows."
    return UpdateStatus(
        current=version_text(current) or current,
        latest=published,
        notes=notes,
        page_url=page,
        newer=newer,
        skipped=skipped_this,
        error=error,
        can_install=can_install,
        asset_name=name if installer else None,
        asset_url=asset_url or None,
        asset_size=asset_size,
        checksum_url=(_asset_url(checksum) or None) if checksum else None,
    )


def _rate_limited(status_code: int, body: str) -> bool:
    text = body.lower()
    if status_code == 429:
        return True
    return status_code == 403 and ("rate limit" in text or "secondary rate" in text)


def _error_status(current: str, message: str) -> UpdateStatus:
    try:
        parse_version(current)
        shown = version_text(current) or current
    except ValueError:
        shown = current
    return UpdateStatus(current=shown, error=message)


def check_for_update(
    current: str,
    skipped: str | None = None,
    *,
    client: httpx.Client | None = None,
    installed: bool | None = None,
    platform: str | None = None,
    url: str | None = None,
) -> UpdateStatus:
    """GET the latest release. Network and API failures become ``status.error``.

    The new repository name is tried first. A 404 there tries the previous
    repository. Any other failure stops, so a rate limit is not hidden.
    """
    if installed is None:
        installed = running_installed()
    urls = (url,) if url else RELEASE_API_URLS
    own_client = client is None
    if own_client:
        client = httpx.Client(follow_redirects=True, headers=github_headers(), timeout=20.0)
    try:
        last = _error_status(current, "No GitHub release has been published yet.")
        for index, candidate in enumerate(urls):
            status = _check_one_release(
                current,
                skipped,
                client=client,
                installed=installed,
                platform=platform,
                url=candidate,
            )
            last = status
            missing = bool(status.error) and status.error.startswith("No GitHub release")
            if missing and index < len(urls) - 1:
                continue
            return status
        return last
    finally:
        if own_client:
            client.close()


def _check_one_release(
    current: str,
    skipped: str | None,
    *,
    client: httpx.Client,
    installed: bool,
    platform: str | None,
    url: str,
) -> UpdateStatus:
    try:
        response = client.get(url, headers=github_headers(), timeout=20.0)
    except httpx.HTTPError:
        return _error_status(current, "Could not reach GitHub to check for updates.")
    body = response.text
    if response.status_code == 404:
        return _error_status(current, "No GitHub release has been published yet.")
    if _rate_limited(response.status_code, body):
        return _error_status(current, "GitHub rate limit reached. Try again in a little while.")
    if response.status_code != 200:
        return _error_status(current, f"GitHub returned HTTP {response.status_code} while checking for updates.")
    try:
        payload = response.json()
    except ValueError:
        return _error_status(current, "GitHub returned something that is not a release.")
    try:
        return parse_release(
            payload,
            current=current,
            skipped=skipped,
            installed=installed,
            platform=platform,
        )
    except UpdateError as exc:
        return _error_status(current, str(exc))


def parse_sha256_text(text: str) -> str:
    """Read a ``sha256sum`` line or a bare hex digest."""
    line = ""
    for candidate in text.splitlines():
        if candidate.strip():
            line = candidate.strip()
            break
    if line.lower().startswith("sha256:"):
        line = line.split(":", 1)[1].strip()
    token = line.split()[0] if line else ""
    digest = token.lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise UpdateError("The published checksum is not a SHA-256 hex digest.")
    return digest


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_installer(path: Path, expected_size: int, expected_sha256: str | None) -> None:
    size = path.stat().st_size
    if size != expected_size:
        raise UpdateError(f"Download is {size} bytes, but the release says {expected_size}.")
    if not expected_sha256:
        return
    actual = sha256_file(path)
    if actual != expected_sha256.lower():
        raise UpdateError("Download checksum does not match the release.")


def download_verified(
    status: UpdateStatus,
    dest: Path,
    *,
    client: httpx.Client | None = None,
    on_progress=None,
) -> Path:
    """Download the installer, then check its size and SHA-256 when one was published."""
    if not status.asset_url or not status.asset_name or status.asset_size is None:
        raise UpdateError("This release has no installer to download.")
    own_client = client is None
    if own_client:
        client = httpx.Client(follow_redirects=True, headers=github_headers(), timeout=httpx.Timeout(30.0, read=300.0))
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    try:
        _download(client, status.asset_url, partial, on_progress)
        checksum = None
        if status.checksum_url:
            checksum = _download_text(client, status.checksum_url)
            checksum = parse_sha256_text(checksum)
        verify_installer(partial, status.asset_size, checksum)
        partial.replace(dest)
        return dest
    except UpdateError:
        partial.unlink(missing_ok=True)
        raise
    except httpx.HTTPError as exc:
        partial.unlink(missing_ok=True)
        raise UpdateError("Could not download the installer.") from exc
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise UpdateError(f"Could not save the installer: {exc}") from exc
    finally:
        if own_client:
            client.close()


def _download(client: httpx.Client, url: str, dest: Path, on_progress) -> None:
    timeout = httpx.Timeout(30.0, read=300.0)
    with client.stream("GET", url, follow_redirects=True, timeout=timeout, headers=github_headers()) as response:
        if response.status_code != 200:
            body = response.read().decode("utf-8", "replace")
            if _rate_limited(response.status_code, body):
                raise UpdateError("GitHub rate limit reached. Try again in a little while.")
            raise UpdateError(f"GitHub returned HTTP {response.status_code} while downloading the installer.")
        total_header = response.headers.get("content-length")
        try:
            total = int(total_header) if total_header else 0
        except ValueError:
            total = 0
        done = 0
        with dest.open("wb") as handle:
            for chunk in response.iter_bytes(64 * 1024):
                handle.write(chunk)
                done += len(chunk)
                if on_progress is not None:
                    on_progress(done, total)


def _download_text(client: httpx.Client, url: str) -> str:
    response = client.get(url, follow_redirects=True, timeout=30.0, headers=github_headers())
    if response.status_code != 200:
        raise UpdateError("Could not download the release checksum. The update was not installed.")
    return response.text


def installer_launch_args(path: Path) -> list[str]:
    """Silent per-user upgrade. Inno Setup closes this app and starts the new one."""
    return [
        str(path),
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/SP-",
        "/CLOSEAPPLICATIONS",
    ]


def launch_installer(path: Path) -> None:
    kwargs: dict = {
        "close_fds": True,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        # DETACHED_PROCESS alone still lets Windows Terminal open a window.
        # popen_hidden adds CREATE_NO_WINDOW and SW_HIDE on top of these flags.
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        kwargs["creationflags"] = flags | breakaway
    popen_hidden(installer_launch_args(path), **kwargs)


def remember_skipped_update(store, version: str) -> None:
    store.set_meta(SKIPPED_UPDATE_META, version_text(version))


def load_skipped_update(store) -> str | None:
    value = store.get_meta(SKIPPED_UPDATE_META)
    return value or None


def wait_for_worker_stop(control, thread, timeout: float = 25.0) -> bool:
    """Ask the worker to stop and wait until its thread has saved state."""
    control.stop()
    if thread is not None and thread.is_alive():
        thread.join(timeout)
        return not thread.is_alive()
    return True
