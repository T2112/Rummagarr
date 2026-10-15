"""The Stats page: finished downloads, library jobs, and uploads."""

from __future__ import annotations

_JOBS = (
    ("imported", "Imported"),
    ("import_failed", "Moved to Review"),
    ("failed", "Failed"),
    ("no_match", "No match"),
    ("skipped", "Skipped"),
    ("staged", "Waiting for Lidarr"),
    ("downloading", "Downloading"),
    ("not_sent", "Not sent"),
)


def _size(number: int) -> str:
    value = float(number or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{int(number or 0)} B"


def format_stats(activity: dict, uploads: dict, last_browse: str = "") -> str:
    lines = ["Downloads"]
    total = int(activity.get("download_total") or 0)
    lines.append(f"  {total} finished")
    users = list(activity.get("by_user") or [])
    if users:
        lines.append("  Top users")
        for index, (name, count) in enumerate(users, start=1):
            lines.append(f"    {index}. {name or 'unknown'} — {int(count)}")
    else:
        lines.append("  No finished downloads yet.")
    lines.append("")
    lines.append("Library jobs")
    jobs = dict(activity.get("jobs") or {})
    shown = False
    for key, label in _JOBS:
        count = int(jobs.get(key) or 0)
        if count:
            lines.append(f"  {label}: {count}")
            shown = True
    if not shown:
        lines.append("  No album jobs yet.")
    lines.append("")
    lines.append("Uploads")
    lines.append(
        f"  {int(uploads.get('all_files') or 0)} files, {_size(int(uploads.get('all_bytes') or 0))} all time"
    )
    lines.append(
        f"  {int(uploads.get('today_files') or 0)} files, {_size(int(uploads.get('today_bytes') or 0))} today"
    )
    note = (last_browse or "").strip()
    if note:
        lines.append("")
        lines.append("Last browse download")
        lines.append(f"  {note}")
    return "\n".join(lines)
