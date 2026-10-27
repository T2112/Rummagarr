"""Child processes on Windows must not open a console."""

from __future__ import annotations

import ast
from pathlib import Path

from process_launch import CREATE_NO_WINDOW, hidden_creation_flags

ROOT = Path(__file__).resolve().parents[1]
_LAUNCH = {"run", "Popen", "call", "check_call", "check_output"}
_OS_SPAWN = {"system", "startfile", "popen"}
_SKIP_DIRS = {".git", ".venv", "venv", "dist", "build", "__pycache__", ".pytest_cache"}


def _imported_launchers(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module != "subprocess":
            continue
        for alias in node.names:
            if alias.name in _LAUNCH:
                names.add(alias.asname or alias.name)
    return names


def launch_sites(tree: ast.AST) -> list[tuple[int, str]]:
    """subprocess and os launches. The helper module is the only allowed caller."""
    imported = _imported_launchers(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id == "subprocess" and func.attr in _LAUNCH:
                found.append((node.lineno, f"subprocess.{func.attr}"))
            elif func.value.id == "os" and func.attr in _OS_SPAWN:
                found.append((node.lineno, f"os.{func.attr}"))
        elif isinstance(func, ast.Name) and func.id in imported:
            found.append((node.lineno, func.id))
    return found


def _python_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        files.append(path)
    return files


def test_the_scanner_flags_a_bare_subprocess_call():
    bare = ast.parse("import subprocess\nsubprocess.run(['ffmpeg'])\n")
    assert launch_sites(bare) == [(2, "subprocess.run")]
    imported = ast.parse("from subprocess import check_call\ncheck_call(['ffmpeg'])\n")
    assert launch_sites(imported) == [(2, "check_call")]
    os_call = ast.parse("import os\nos.startfile('Rummagarr.exe')\n")
    assert launch_sites(os_call) == [(2, "os.startfile")]


def test_windows_child_flags_include_create_no_window():
    flags = hidden_creation_flags(0x8)
    assert flags & CREATE_NO_WINDOW
    assert flags & 0x8
    assert CREATE_NO_WINDOW == 0x08000000


def test_every_subprocess_goes_through_the_hidden_helper():
    helper = (ROOT / "process_launch.py").read_text(encoding="utf-8")
    for token in ("CREATE_NO_WINDOW", "STARTF_USESHOWWINDOW", "SW_HIDE", "set_executable"):
        assert token in helper
    tree = ast.parse(helper)
    helpers = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert {"run_hidden", "popen_hidden", "hidden_subprocess_kwargs", "configure_child_processes"} <= helpers

    offenders: list[str] = []
    for path in _python_files():
        if path.name == "process_launch.py":
            continue
        source = path.read_text(encoding="utf-8")
        try:
            parsed = ast.parse(source)
        except SyntaxError as exc:
            offenders.append(f"{path}: {exc}")
            continue
        for line, name in launch_sites(parsed):
            offenders.append(f"{path.relative_to(ROOT)}:{line} calls {name} without CREATE_NO_WINDOW")
    assert offenders == []


def test_the_windowed_build_and_entry_points_hide_child_consoles():
    spec = (ROOT / "packaging" / "rummagarr.spec").read_text(encoding="utf-8")
    assert "console=False" in spec
    assert "console=True" not in spec
    for name in ("desktop_app.py", "main.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "configure_child_processes()" in text
