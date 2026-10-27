"""Rummagarr 1.19.0: bulk progress, one installer name, and the upcoming repository URL."""

from __future__ import annotations

from pathlib import Path

from changelog_notes import changelog_section, install_instructions
from config_loader import LEGACY_HOME_DIR, LEGACY_WINDOWS_DIR

ROOT = Path(__file__).resolve().parents[1]


def test_changelog_describes_the_progress_count_and_the_single_installer():
    section = changelog_section((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), "1.19.0")
    assert "120/480" in section
    assert "Cancel" in section
    assert "T2112/rummagarr" in section
    assert "T2112/lidarr-soulseek" in section
    assert "Rummagarr-Setup" in section
    assert "LidarrSoulseek-Setup" not in section


def test_release_notes_name_only_the_rummagarr_installer():
    notes = install_instructions("1.19.0")
    assert "Rummagarr-Setup-1.19.0.exe" in notes
    assert "LidarrSoulseek-Setup" not in notes


def test_build_and_workflow_do_not_publish_the_old_installer_name():
    workflow = (ROOT / ".github" / "workflows" / "windows-installer.yml").read_text(encoding="utf-8")
    script = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "LidarrSoulseek-Setup" not in workflow
    assert "LidarrSoulseek-Setup" not in script
    assert "LidarrSoulseek-Setup" not in readme
    assert "Rummagarr-Setup-*.exe" in workflow
    assert "Rummagarr-Setup-$version.exe" in script
    assert "https://github.com/T2112/Rummagarr" in readme
    assert "T2112/lidarr-soulseek" not in readme
    assert "Lidarr Soulseek" not in readme


def test_installer_id_and_old_data_folder_stay():
    installer = (ROOT / "packaging" / "Rummagarr.iss").read_text(encoding="utf-8")
    assert "AppId={{7C3E9A2B-5F14-4D8E-9A6C-2B1F0E4D8A77}" in installer
    assert "AppMutex=LidarrSoulseekMutex" in installer
    assert 'AppName={#MyAppName}' in installer
    assert 'MyAppName "Rummagarr"' in installer
    assert "PrivilegesRequired=lowest" in installer
    assert LEGACY_WINDOWS_DIR == "Lidarr Soulseek"
    assert LEGACY_HOME_DIR == ".lidarr-soulseek"
