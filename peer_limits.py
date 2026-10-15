"""Limits for downloading every missing file a browsed user has.

The profile text is read for a file cap, a size cap, a wait, a queue ceiling,
and "one at a time". Rummagarr's own caps are compared with those. The
stricter number is the one that is used. Upload permission is respected:
uploads that are off, or only for a list, are not queued.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_FILES = re.compile(
    r"(?:max(?:imum)?|no more than|limit(?:ed)? to|only)\s+(\d+)\s+files\b",
    re.IGNORECASE,
)
_FILES_TAIL = re.compile(r"\b(\d+)\s+files\s+max\b", re.IGNORECASE)
_SIZE = re.compile(
    r"(?:max(?:imum)?|no more than|limit(?:ed)? to|only)\s+(\d+)\s*(mb|gb)\b",
    re.IGNORECASE,
)
_WAIT = re.compile(
    r"(?:wait|pause)\s+(\d+)\s*(seconds|secs|second|minutes|mins|minute)\b",
    re.IGNORECASE,
)
_BETWEEN = re.compile(
    r"\b(\d+)\s*(seconds|secs|second|minutes|mins|minute)\s+between\b",
    re.IGNORECASE,
)
_ONE = re.compile(
    r"\b(?:one|1)\s+(?:(?:file|album|song|download)\s+)?at a time\b",
    re.IGNORECASE,
)
_QUEUE = re.compile(r"\bqueue\s+(?:under|below|max(?:imum)?)\s+(\d+)\b", re.IGNORECASE)


@dataclass
class ProfileLimits:
    max_files: int | None = None
    max_mb: int | None = None
    pace_seconds: int | None = None
    one_at_a_time: bool = False
    queue_under: int | None = None


@dataclass
class AppliedLimits:
    allowed: bool
    reason: str
    max_files: int
    max_mb: int
    files_at_once: int
    pace_seconds: int
    lines: list[str] = field(default_factory=list)


def parse_profile_limits(description: str) -> ProfileLimits:
    text = description or ""
    files = [int(item) for item in _FILES.findall(text) + _FILES_TAIL.findall(text)]
    sizes = []
    for number, unit in _SIZE.findall(text):
        amount = int(number)
        if unit.lower().startswith("g"):
            amount *= 1024
        sizes.append(amount)
    paces = []
    for number, unit in _WAIT.findall(text) + _BETWEEN.findall(text):
        seconds = int(number)
        if unit.lower().startswith("m"):
            seconds *= 60
        paces.append(seconds)
    queues = [int(item) for item in _QUEUE.findall(text)]
    return ProfileLimits(
        max_files=min(files) if files else None,
        max_mb=min(sizes) if sizes else None,
        pace_seconds=max(paces) if paces else None,
        one_at_a_time=bool(_ONE.search(text)),
        queue_under=min(queues) if queues else None,
    )


def apply_limits(
    description: str,
    *,
    permission: int,
    queue_length: int,
    our_files: int,
    our_mb: int,
    our_pace: int = 5,
    one_at_a_time: bool = True,
) -> AppliedLimits:
    """Combine this PC's caps with the profile. The stricter value wins."""
    profile = parse_profile_limits(description)
    our_files = max(1, int(our_files or 1))
    our_mb = max(1, int(our_mb or 1))
    our_pace = max(0, int(our_pace or 0))
    if int(permission) == 0:
        return AppliedLimits(False, "Uploads are off.", our_files, our_mb, 1, our_pace, ["Uploads are off. Nothing will be queued."])
    if int(permission) in {2, 3}:
        return AppliedLimits(
            False,
            "Uploads are only for a list.",
            our_files,
            our_mb,
            1,
            our_pace,
            ["This user only uploads to a list. Nothing will be queued."],
        )
    if profile.queue_under is not None and int(queue_length or 0) >= profile.queue_under:
        return AppliedLimits(
            False,
            "Their queue is already at the limit they asked for.",
            our_files,
            our_mb,
            1,
            our_pace,
            [f"Their queue is {int(queue_length or 0)}. They asked for a queue under {profile.queue_under}."],
        )
    max_files = our_files if profile.max_files is None else min(our_files, profile.max_files)
    max_mb = our_mb if profile.max_mb is None else min(our_mb, profile.max_mb)
    pace = our_pace if profile.pace_seconds is None else max(our_pace, profile.pace_seconds)
    at_once = 1 if one_at_a_time or profile.one_at_a_time else max(1, min(max_files, 4))
    lines = [
        _who("files", max_files, our_files, profile.max_files),
        _who("MB", max_mb, our_mb, profile.max_mb),
        _pace_line(pace, our_pace, profile.pace_seconds),
        _once_line(one_at_a_time, profile.one_at_a_time, at_once),
    ]
    if int(permission) < 0:
        lines.append("Upload permission was not reported. The download can still be queued.")
    return AppliedLimits(True, "", max_files, max_mb, at_once, pace, lines)


def select_under_cap(matches: list, *, max_files: int, max_bytes: int) -> tuple[list, int]:
    """Keep matches until the file count or the byte count is full."""
    chosen = []
    total = 0
    skipped = 0
    ceiling = max(0, int(max_bytes or 0))
    for match in matches:
        size = max(0, int(getattr(match, "size", 0) or 0))
        if len(chosen) >= max(0, int(max_files)):
            skipped += 1
            continue
        if size > ceiling or total + size > ceiling:
            skipped += 1
            continue
        chosen.append(match)
        total += size
    return chosen, skipped


def share_file_rows(username: str, directories: list[dict] | None) -> list[dict]:
    """Flat files from a saved browse. One row per file, not per parent folder."""
    rows = []
    seen = set()
    for entry in directories or []:
        folder = str(entry.get("name") or "")
        for item in entry.get("files") or []:
            filename = str(item.get("filename") or "")
            if not filename:
                continue
            key = filename.casefold()
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "username": username,
                    "filename": filename,
                    "folder": folder,
                    "size": int(item.get("size") or 0),
                    "extension": str(item.get("extension") or ""),
                }
            )
    return rows


def _who(unit: str, used: int, ours: int, theirs: int | None) -> str:
    if theirs is None:
        return f"{used} {unit} (your cap)."
    if used == theirs and used != ours:
        return f"{used} {unit} (their profile is stricter; your cap is {ours} {unit})."
    if used == ours and used != theirs:
        return f"{used} {unit} (your cap is stricter; their profile says {theirs} {unit})."
    return f"{used} {unit} (your cap and their profile)."


def _pace_line(used: int, ours: int, theirs: int | None) -> str:
    if theirs is None:
        return f"{used} seconds between files (your pause)."
    if used == theirs and used != ours:
        return f"{used} seconds between files (their profile is stricter; your pause is {ours} seconds)."
    if used == ours and used != theirs:
        return f"{used} seconds between files (your pause is stricter; their profile says {theirs} seconds)."
    return f"{used} seconds between files (your pause and their profile)."


def _once_line(ours: bool, theirs: bool, at_once: int) -> str:
    if at_once == 1 and ours and theirs:
        return "1 file at a time (your rule and their profile)."
    if at_once == 1 and ours:
        return "1 file at a time (your rule)."
    if at_once == 1 and theirs:
        return "1 file at a time (their profile)."
    return f"{at_once} files at a time."
