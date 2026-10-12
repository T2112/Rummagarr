"""What the worker is doing right now, in words the Status tab can show.

Idle is reserved for a running worker that has nothing selected. Every other
pause, search, and download has its own sentence.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class Step:
    kind: str = "idle"
    artist: str = ""
    album: str = ""
    title: str = ""
    query_index: int = 0
    query_total: int = 0
    result_count: int = 0
    waited_seconds: int = 0
    file_index: int = 0
    file_total: int = 0
    username: str = ""
    retry_seconds: int = 0
    retry_kind: str = "pass"
    detail: str = ""
    started: float = 0.0


def format_span(seconds: int) -> str:
    """A short wait: 12s, 5m, 2h 5m."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = (seconds + 59) // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours >= 48:
        days = (seconds + 86399) // 86400
        return f"{days}d"
    if minutes:
        return f"{hours}h {minutes}m"
    return f"{hours}h"


def format_elapsed(seconds: int) -> str:
    """Elapsed time for the current step, with seconds kept once it passes a minute."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _who(step: Step) -> str:
    album = step.album or ("" if step.kind == "error" else step.title)
    return " – ".join(part for part in (step.artist, album) if part)


def describe_step(step: Step) -> str:
    who = _who(step)
    if step.kind == "searching":
        bits = []
        if step.query_total:
            bits.append(f"query {step.query_index} of {step.query_total}")
        if step.result_count:
            bits.append(f"{step.result_count} results so far")
        extra = f" ({', '.join(bits)})" if bits else ""
        quoted = f'"{who}"' if who else "Soulseek"
        return f"Searching Soulseek: {quoted}{extra}"
    if step.kind == "waiting":
        text = f"Waiting for search results ({max(0, int(step.waited_seconds))}s)"
        return f"{text} · {who}" if who else text
    if step.kind == "matching":
        return f"Matching results · {who}" if who else "Matching results"
    if step.kind == "queued":
        user = step.username or "a user"
        count = step.file_total or step.file_index
        return f"Queued {count} files from user {user}"
    if step.kind == "downloading":
        if step.file_total:
            head = f"Downloading {step.file_index} of {step.file_total}"
        else:
            head = "Downloading"
        return f"{head} · {who}" if who else head
    if step.kind == "converting":
        name = step.detail or step.title
        head = f"Converting {name}" if name and name != step.album else "Converting"
        return f"{head} · {who}" if who else head
    if step.kind == "retry":
        # "pass" is the short wait between ready artists (cycle_seconds).
        # "artist" is the multi-day skip after a visit (artist_cooldown_days).
        if step.retry_kind == "artist":
            return f"Waiting for artist retry period (next artist in {format_span(step.retry_seconds)})"
        if int(step.retry_seconds or 0) <= 0:
            return "Starting the next artist"
        return f"Waiting for retry period (next artist in {format_span(step.retry_seconds)})"
    if step.kind == "preparing":
        return f"Choosing the next album · {who}" if who else "Choosing the next album"
    if step.kind == "connecting":
        return "Connecting to Soulseek"
    if step.kind == "stopped":
        return "Stopped"
    if step.kind == "error":
        return step.detail or step.title or "Error"
    if step.kind == "paused":
        return "Paused"
    return "Idle – nothing selected"


def step_from_legacy(phase: str, artist: str = "", title: str = "") -> Step:
    """Map the older phase string onto a step so existing callers still report activity."""
    text = (phase or "idle").casefold()
    if text == "idle":
        kind = "idle"
    elif text == "stopped":
        kind = "stopped"
    elif "error" in text:
        kind = "error"
    elif "convert" in text:
        kind = "converting"
    elif "download" in text:
        kind = "downloading"
    elif "match" in text:
        kind = "matching"
    elif "queue" in text:
        kind = "queued"
    elif "wait" in text:
        kind = "waiting"
    elif "search" in text or "connect" in text:
        kind = "connecting" if "connect" in text else "searching"
    else:
        kind = "idle"
    album = "" if "track" in text else title
    return Step(
        kind=kind,
        artist=artist,
        album=album,
        title=title,
        detail=title if kind == "error" else "",
        started=time.monotonic(),
    )


def format_status_line(snapshot: dict) -> str:
    labels = {"running": "Running", "paused": "Paused", "stopped": "Stopped"}
    state = labels.get(snapshot.get("run_state") or "stopped", "Stopped")
    connected = snapshot.get("connected_label") or (
        "Soulseek connected" if snapshot.get("logged_in") else "Soulseek offline"
    )
    if snapshot.get("activity"):
        activity = str(snapshot.get("activity"))
    else:
        job = snapshot.get("job") or {}
        activity = describe_step(
            step_from_legacy(str(job.get("phase") or "idle"), str(job.get("artist") or ""), str(job.get("title") or ""))
        )
        if (snapshot.get("run_state") or "stopped") == "stopped" and activity == "Idle – nothing selected":
            activity = "Stopped"
    parts = [state, connected, activity]
    elapsed = int(snapshot.get("elapsed") or 0)
    if elapsed > 0:
        parts.append(format_elapsed(elapsed))
    return " · ".join(part for part in parts if part)
