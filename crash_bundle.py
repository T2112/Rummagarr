"""An optional zip written on this PC when Rummagarr hits an unexpected error.

The zip is not uploaded. Passwords and API keys are removed before it is written.
The option is off until it is turned on in Settings.
"""

from __future__ import annotations

import re
import sys
import threading
import traceback
import zipfile
from datetime import datetime
from pathlib import Path

from process_launch import open_path

_SECRET = re.compile(
    r'(?i)([A-Za-z0-9_]*(?:password|api_key|token|secret)[A-Za-z0-9_]*\s*=\s*)(?:"[^"]*"|\'[^\']*\'|\S+)'
)
_STATE: dict = {"installed": False}


def redact(text: str) -> str:
    return _SECRET.sub(r'\1"[redacted]"', text or "")


def crash_directory(log_file: Path) -> Path:
    return Path(log_file).expanduser().resolve().parent / "crashes"


def write_crash_bundle(
    directory: Path,
    *,
    version: str,
    error_text: str,
    log_path: Path | None,
    config_path: Path | None,
    enabled: bool,
) -> Path | None:
    """Write one zip. Returns None when the option is off."""
    if not enabled:
        return None
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = folder / f"rummagarr-crash-{stamp}.zip"
    suffix = 2
    while dest.exists():
        dest = folder / f"rummagarr-crash-{stamp}-{suffix}.zip"
        suffix += 1
    note = "\n".join(
        [
            f"Rummagarr {version}",
            "This file stays on this PC. It is not uploaded.",
            "",
            error_text or "",
        ]
    )
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("crash.txt", note)
        archive.writestr("log-tail.txt", redact(_tail(log_path)))
        archive.writestr("config.toml", redact(_read(config_path)))
    return dest


def install_crash_hook(enabled, directory, log_path, config_path, version: str) -> None:
    """Call the previous hooks after the zip is written. Later calls retarget it."""
    _STATE["enabled"] = enabled
    _STATE["directory"] = directory
    _STATE["log_path"] = log_path
    _STATE["config_path"] = config_path
    _STATE["version"] = version
    if _STATE["installed"]:
        return
    _STATE["installed"] = True
    previous = sys.excepthook

    def _write(error_text: str) -> None:
        try:
            if not _STATE["enabled"]():
                return
            write_crash_bundle(
                _STATE["directory"](),
                version=_STATE["version"],
                error_text=error_text,
                log_path=_STATE["log_path"](),
                config_path=_STATE["config_path"](),
                enabled=True,
            )
        except Exception:
            return

    def hook(exc_type, exc, tb) -> None:
        _write("".join(traceback.format_exception(exc_type, exc, tb)))
        previous(exc_type, exc, tb)

    sys.excepthook = hook
    if hasattr(threading, "excepthook"):
        previous_thread = threading.excepthook

        def thread_hook(args) -> None:
            _write("".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
            previous_thread(args)

        threading.excepthook = thread_hook


def open_folder(path: Path) -> str:
    """Open a folder in the system file browser. The message is empty on success."""
    folder = Path(path)
    folder.mkdir(parents=True, exist_ok=True)
    try:
        open_path(folder)
    except Exception as exc:
        return f"Could not open {folder}: {exc}"
    return ""


def _read(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _tail(path: Path | None, lines: int = 200) -> str:
    text = _read(path)
    if not text:
        return ""
    return "\n".join(text.splitlines()[-lines:])
