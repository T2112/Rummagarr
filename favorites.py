"""Suggestions for users who already sent finished files.

A favorite is only a suggestion. Priority users stay the upload list the
person edits. Nothing here adds a name to that list.
"""

from __future__ import annotations

GOOD_USER_MINIMUM = 3


def daily_room(used: int, cap: int) -> int:
    """How many more files this user can finish today."""
    return max(0, int(cap or 0) - max(0, int(used or 0)))


def favorite_title(username: str) -> str:
    return f"Add {username} to favorites?"


def should_suggest(
    username: str,
    count: int,
    favorites: set[str],
    dismissed: set[str],
    minimum: int = GOOD_USER_MINIMUM,
) -> bool:
    """True when this user has enough finished files and was not decided yet."""
    name = (username or "").strip().lower()
    if not name:
        return False
    known = {item.strip().lower() for item in favorites if str(item).strip()}
    skipped = {item.strip().lower() for item in dismissed if str(item).strip()}
    if name in known or name in skipped:
        return False
    return int(count or 0) >= int(minimum)
