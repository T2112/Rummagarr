"""Pull one version's section out of CHANGELOG.md.

The GitHub release body and the in-app update prompt both use this text.
A missing section is an error so a release cannot go out without notes.
"""

from __future__ import annotations

import re

_HEADING = re.compile(r"^## \[(\d+\.\d+\.\d+)\]", re.MULTILINE)


class ChangelogError(LookupError):
    """CHANGELOG.md has no section for this version."""


def changelog_section(text: str, version: str) -> str:
    """The ``## [version]`` section, including its heading, up to the next version."""
    wanted = (version or "").strip()
    if not wanted:
        raise ChangelogError("No version to look up in CHANGELOG.md")
    match = None
    for found in _HEADING.finditer(text or ""):
        if found.group(1) == wanted:
            match = found
            break
    if match is None:
        raise ChangelogError(f"CHANGELOG.md has no section for {wanted}")
    start = match.start()
    nxt = _HEADING.search(text, match.end())
    end = nxt.start() if nxt else len(text)
    section = text[start:end].strip()
    if not section:
        raise ChangelogError(f"CHANGELOG.md section for {wanted} is empty")
    return section
