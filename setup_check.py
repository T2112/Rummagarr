"""Checks that the first window can finish before Soulseek is asked to search."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SetupCheck:
    name: str
    ok: bool
    detail: str


def _present(value: str, blocked: str = "CHANGEME") -> bool:
    text = (value or "").strip()
    return bool(text) and text != blocked


def _port(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 1 or number > 65535:
        return None
    return number


def local_setup_checks(cfg) -> list[SetupCheck]:
    """Settings and folders. Lidarr itself is a separate check so this stays offline."""
    url = str(getattr(cfg, "lidarr_url", "") or "").strip()
    url_ok = url.startswith("http://") or url.startswith("https://")
    key = str(getattr(cfg, "lidarr_api_key", "") or "")
    user = str(getattr(cfg, "slsk_username", "") or "")
    password = str(getattr(cfg, "slsk_password", "") or "")
    listen = _port(getattr(cfg, "listen_port", 0))
    obfs = _port(getattr(cfg, "obfuscated_port", 0))
    checks = [
        SetupCheck(
            "Lidarr URL",
            url_ok,
            url if url_ok else "Enter the Lidarr URL, including http:// or https://.",
        ),
        SetupCheck(
            "Lidarr API key",
            _present(key),
            "Saved." if _present(key) else "Enter the key from Lidarr, Settings, General.",
        ),
        SetupCheck(
            "Soulseek username",
            _present(user),
            "Saved." if _present(user) else "Enter the Soulseek username.",
        ),
        SetupCheck(
            "Soulseek password",
            _present(password),
            "Saved." if _present(password) else "Enter the Soulseek password.",
        ),
        SetupCheck(
            "Listen ports",
            listen is not None and obfs is not None and listen != obfs,
            f"Listen {listen}, obfuscated {obfs}."
            if listen is not None and obfs is not None and listen != obfs
            else "Listen port and obfuscated port must be different numbers from 1 to 65535.",
        ),
    ]
    checks.append(_folder_check("Incomplete folder", getattr(cfg, "download_dir", "")))
    checks.append(_folder_check("Complete folder", getattr(cfg, "complete_dir", "")))
    scan = str(getattr(cfg, "lidarr_scan_path", "") or "").strip()
    if not scan:
        checks.append(SetupCheck("Lidarr scan path", False, "Enter the folder Lidarr imports from."))
    else:
        path = Path(scan)
        if path.is_dir():
            detail = str(path)
        elif path.exists():
            detail = "That path is a file. Lidarr needs a folder."
            checks.append(SetupCheck("Lidarr scan path", False, detail))
            return checks
        else:
            detail = "This path is not on this PC. That is expected when Lidarr uses another machine."
        checks.append(SetupCheck("Lidarr scan path", True, detail))
    return checks


def _folder_check(name: str, value) -> SetupCheck:
    text = str(value or "").strip()
    if not text:
        return SetupCheck(name, False, "Choose a folder.")
    path = Path(text)
    if path.is_dir():
        return SetupCheck(name, True, str(path))
    if path.exists():
        return SetupCheck(name, False, "That path is a file. Choose a folder.")
    return SetupCheck(name, True, "Not created yet. Rummagarr creates it when a download needs it.")


def format_setup_report(checks: list[SetupCheck]) -> str:
    lines = []
    for item in checks:
        mark = "OK" if item.ok else "Needs attention"
        lines.append(f"{mark}  {item.name}: {item.detail}")
    if any(not item.ok for item in checks):
        lines.append("Fix the rows that need attention in Settings, then run Setup check again.")
    else:
        lines.append("Setup looks ready.")
    return "\n".join(lines)


def lidarr_check(ok: bool, detail: str) -> SetupCheck:
    return SetupCheck("Lidarr connection", bool(ok), detail or ("Lidarr answered." if ok else "Lidarr did not answer."))
