"""Bind address, the host Lidarr can reach, and the folder path Lidarr sees.

Lidarr on another machine cannot use 127.0.0.1. Tailscale addresses sit in
100.64.0.0/10. When the bridge listens on every interface, that address is
the one registered with Lidarr unless a different host is typed in.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import sys
from pathlib import Path

_TAILSCALE = ipaddress.ip_network("100.64.0.0/10")


def is_loopback_bind(host: str) -> bool:
    text = (host or "").strip().lower()
    if text in {"", "localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def is_tailscale(host: str) -> bool:
    try:
        return ipaddress.ip_address((host or "").strip()) in _TAILSCALE
    except ValueError:
        return False


def clean_host(value: str) -> str:
    """Host only. A pasted URL or host:port loses the scheme, path, and port."""
    text = (value or "").strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].strip()
    if text.startswith("[") and "]" in text:
        return text[1:text.index("]")]
    if text.count(":") == 1:
        text = text.split(":", 1)[0]
    return text.strip()


def list_ipv4_interfaces() -> list[tuple[str, str]]:
    """(interface name, IPv4) for this PC. Empty when the list cannot be read."""
    try:
        if os.name == "nt":
            return _windows_interfaces()
        return _linux_interfaces()
    except Exception:
        return []


def bind_choices(interfaces: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    """(address to bind, label) including this PC, every interface, and each NIC."""
    choices = [
        ("127.0.0.1", "This PC only (127.0.0.1)"),
        ("0.0.0.0", "All interfaces"),
    ]
    seen = {"127.0.0.1", "0.0.0.0", "::", ""}
    for name, ip in interfaces or []:
        ip = (ip or "").strip()
        if not ip or ip in seen or is_loopback_bind(ip):
            continue
        seen.add(ip)
        if is_tailscale(ip):
            label = f"Tailscale ({ip})" if not name or name.lower() == "tailscale" else f"Tailscale {name} ({ip})"
        else:
            label = f"{name} ({ip})" if name else ip
        choices.append((ip, label))
    return choices


def advertise_host(bind_host: str, override: str = "", interfaces: list[tuple[str, str]] | None = None) -> str:
    """The host Lidarr should use. An override wins. All-interfaces prefers Tailscale."""
    chosen = clean_host(override)
    if chosen:
        return chosen
    bind_host = clean_host(bind_host) or "127.0.0.1"
    if bind_host not in {"0.0.0.0", "::"}:
        return bind_host
    rows = list(interfaces) if interfaces is not None else list_ipv4_interfaces()
    tailscale = [ip for _name, ip in rows if is_tailscale(ip)]
    if tailscale:
        return tailscale[0]
    for _name, ip in rows:
        if ip and not is_loopback_bind(ip):
            return ip
    return "127.0.0.1"


def url_host_is_local(url: str) -> bool:
    from urllib.parse import urlparse

    return is_loopback_bind(urlparse(url or "").hostname or "")


def host_for_url(host: str) -> str:
    """Bracket an IPv6 address so it can sit in http://host:port."""
    text = (host or "").strip()
    if ":" in text and not (text.startswith("[") and text.endswith("]")):
        return f"[{text}]"
    return text


def child_path(root: str, name: str) -> str:
    """root plus one folder. Windows paths, including UNC, keep backslashes."""
    folder = (name or "").strip() or "Album"
    text = (root or "").strip()
    if not text:
        return folder
    if _windows_style(text):
        return text.replace("/", "\\").rstrip("\\") + "\\" + folder
    return text.rstrip("/") + "/" + folder


def reported_root(complete: Path, remote: str) -> str:
    """save_path Lidarr reads. The remote path when Lidarr is on another PC."""
    text = (remote or "").strip()
    if not text:
        return str(Path(complete).resolve())
    if _windows_style(text):
        return text.replace("/", "\\").rstrip("\\")
    return text.rstrip("/")


def mapped_folder(local_dir: Path, complete_dir: Path, remote: str) -> str:
    """The album folder as Lidarr sees it. The local path when remote is blank."""
    local_dir = Path(local_dir)
    text = (remote or "").strip()
    if not text:
        return str(local_dir.resolve())
    try:
        rel = local_dir.resolve().relative_to(Path(complete_dir).resolve())
    except (ValueError, OSError):
        rel = Path(local_dir.name)
    if _windows_style(text):
        base = text.replace("/", "\\").rstrip("\\")
        tail = "\\".join(rel.parts)
        return f"{base}\\{tail}" if tail else base
    base = text.rstrip("/")
    tail = "/".join(rel.parts)
    return f"{base}/{tail}" if tail else base


def firewall_powershell(port: int, program: str = "") -> str:
    """Commands that allow the bridge port. The program rule is only for the installed exe."""
    number = int(port)
    if number < 1 or number > 65535:
        raise ValueError("Bridge port must be a whole number from 1 to 65535.")
    name = f"Rummagarr Bridge {number}"
    lines = [
        "$ErrorActionPreference = 'Stop'",
        f'netsh advfirewall firewall delete rule name="{name}" | Out-Null',
        f'netsh advfirewall firewall add rule name="{name}" dir=in action=allow protocol=TCP localport={number}',
        f'Write-Host "Allowed inbound TCP {number}"',
    ]
    exe = (program or "").strip()
    if exe:
        lines.extend(
            [
                'netsh advfirewall firewall delete rule name="Rummagarr" | Out-Null',
                f'netsh advfirewall firewall add rule name="Rummagarr" dir=in action=allow program="{exe}" enable=yes',
            ]
        )
    return "\n".join(lines) + "\n"


def frozen_program() -> str:
    if getattr(sys, "frozen", False):
        return sys.executable
    return ""


def windows_firewall_prompt(port: int, program: str = "") -> tuple[bool, str]:
    """Ask Windows for approval and add the rule. Other systems do not change a firewall."""
    if os.name != "nt":
        return False, "Windows Firewall rules are only added on Windows."
    import tempfile

    script = firewall_powershell(port, program or frozen_program())
    path = Path(tempfile.gettempdir()) / "rummagarr-firewall.ps1"
    path.write_text(script, encoding="utf-8")
    import ctypes

    params = f'-NoProfile -ExecutionPolicy Bypass -File "{path}"'
    result = int(ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe", params, None, 1))
    if result <= 32:
        return False, "Windows did not open the approval prompt."
    return True, f"Windows asked for approval to allow inbound TCP {int(port)}."


def _windows_style(text: str) -> bool:
    if "\\" in text or text.startswith("\\\\"):
        return True
    return len(text) >= 2 and text[1] == ":"


def _linux_interfaces() -> list[tuple[str, str]]:
    import fcntl
    import struct

    found = []
    root = Path("/sys/class/net")
    if not root.is_dir():
        return found
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for name in sorted(root.iterdir(), key=lambda item: item.name):
            try:
                packed = fcntl.ioctl(sock.fileno(), 0x8915, struct.pack("256s", name.name[:15].encode("utf-8")))
            except OSError:
                continue
            ip = socket.inet_ntoa(packed[20:24])
            found.append((name.name, ip))
    finally:
        sock.close()
    return found


def _windows_interfaces() -> list[tuple[str, str]]:
    from process_launch import run_hidden

    script = (
        "Get-NetIPAddress -AddressFamily IPv4 | "
        "ForEach-Object { $_.InterfaceAlias + \"`t\" + $_.IPAddress }"
    )
    completed = run_hidden(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    return parse_address_table(completed.stdout)


def parse_address_table(text: str) -> list[tuple[str, str]]:
    rows = []
    for line in (text or "").splitlines():
        name, sep, ip = line.partition("\t")
        ip = ip.strip()
        name = name.strip()
        if sep and ip and name:
            rows.append((name, ip))
    return rows
