"""Rummagarr 1.18.3: sponsors link, a public README, and GitHub issue forms."""

from __future__ import annotations

import re
from pathlib import Path

from changelog_notes import changelog_section

ROOT = Path(__file__).resolve().parents[1]


def test_funding_file_points_at_the_github_sponsor():
    text = (ROOT / ".github" / "FUNDING.yml").read_text(encoding="utf-8")
    assert "github: T2112" in text


def test_issue_forms_ask_for_a_crash_bundle_and_a_feature():
    bug = (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").read_text(encoding="utf-8")
    feature = (ROOT / ".github" / "ISSUE_TEMPLATE" / "feature_request.yml").read_text(encoding="utf-8")
    assert "App version" in bug
    assert "Windows version" in bug
    assert "Steps" in bug
    assert "Help → Open crash folder" in bug
    assert "Feature request" in feature
    assert "What should Rummagarr do?" in feature


def test_readme_is_written_for_someone_new():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Version 1.19.1" in text
    assert "Only download music you have the rights to" in text
    assert "https://github.com/sponsors/T2112" in text
    assert "More info" in text
    assert "Run anyway" in text
    assert "Unblock-File" in text
    assert "https://github.com/T2112/lidarr-soulseek/issues" in text
    assert "Rummagarr is published in the repository T2112/lidarr-soulseek (it will move to T2112/rummagarr)." in text
    assert text.count("T2112/rummagarr") == 1
    assert "https://github.com/T2112/rummagarr" not in text
    head, legal = text.split("## Legal", 1)
    assert "Only download music you have the rights to" not in head
    assert "This worker is provided as-is for personal use." in legal
    assert "Key features" in text
    assert "docs/showcase.md" in text
    showcase = (ROOT / "docs" / "showcase.md").read_text(encoding="utf-8")
    assert "https://github.com/T2112/lidarr-soulseek" in showcase
    assert "https://github.com/T2112/rummagarr" not in showcase
    assert "LinkedIn" in showcase
    assert "personal project" in showcase.lower()
    assert "Soulseek username" in text
    assert "API key" in text
    for name in ("docs/status.png", "docs/wanted.png", "docs/settings.png"):
        assert name in text
        image = ROOT / name
        assert image.is_file()
        assert image.read_bytes().startswith(b"\x89PNG")
        assert image.stat().st_size > 10_000
    assert re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text) is None


def test_changelog_mentions_sponsors_and_the_crash_folder():
    section = changelog_section((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), "1.18.3")
    assert "sponsors/T2112" in section
    assert "Unblock-File" in section
    assert "Open crash folder" in section
