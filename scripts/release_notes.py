"""Print the CHANGELOG.md section for a version. Exit 1 when it is missing."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from changelog_notes import ChangelogError, changelog_section


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--changelog", type=Path, default=ROOT / "CHANGELOG.md")
    args = parser.parse_args()
    try:
        text = args.changelog.read_text(encoding="utf-8")
        print(changelog_section(text, args.version))
    except (OSError, ChangelogError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
