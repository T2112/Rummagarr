"""Start child programs without a console window or a focus steal.

Windows 11 uses Windows Terminal as the default console. A console-subsystem
program such as ffmpeg then opens a terminal and takes the keyboard, even when
the parent is a windowed app. CREATE_NO_WINDOW stops that allocation.
SW_HIDE covers a window that was still created. Multiprocessing children must
use this same windowed executable, not python.exe.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Present on Windows. The numeric value is stable and used by the lint too.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def hidden_creation_flags(*extra: int) -> int:
    """CREATE_NO_WINDOW, plus any extra creation flags the caller needs."""
    flags = int(CREATE_NO_WINDOW)
    for item in extra:
        flags |= int(item)
    return flags


def hidden_startupinfo():
    """Ask Windows to start the child hidden. None off Windows."""
    if os.name != "nt":
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return info


def hidden_subprocess_kwargs(**overrides) -> dict:
    """Keyword arguments for subprocess that keep the child off the desktop."""
    kwargs = dict(overrides)
    if os.name != "nt":
        return kwargs
    extra = int(kwargs.pop("creationflags", 0) or 0)
    kwargs["creationflags"] = hidden_creation_flags(extra)
    kwargs["startupinfo"] = hidden_startupinfo()
    return kwargs


def run_hidden(args, **kwargs):
    """subprocess.run that does not open a console on Windows."""
    return subprocess.run(args, **hidden_subprocess_kwargs(**kwargs))


def popen_hidden(args, **kwargs):
    """subprocess.Popen that does not open a console on Windows."""
    return subprocess.Popen(args, **hidden_subprocess_kwargs(**kwargs))


def windowed_executable() -> str:
    """The GUI executable. A frozen build is already windowed; source uses pythonw when it exists."""
    if getattr(sys, "frozen", False):
        return sys.executable
    exe = Path(sys.executable)
    if os.name == "nt" and exe.name.lower() in {"python.exe", "python3.exe", "python3.12.exe"}:
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.is_file():
            return str(pythonw)
    return sys.executable


def configure_child_processes() -> None:
    """Frozen spawn support, and multiprocessing children launched as the windowed exe."""
    import multiprocessing

    multiprocessing.freeze_support()
    if os.name != "nt":
        return
    multiprocessing.set_executable(windowed_executable())
