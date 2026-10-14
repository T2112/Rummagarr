"""What one Start accomplished: grabbed, imported, moved to Review, or failed."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class RunSummary:
    started: str = ""
    grabbed: list[str] = field(default_factory=list)
    imported: list[str] = field(default_factory=list)
    review: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.started:
            self.started = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def note_grabbed(self, label: str) -> None:
        self.grabbed.append(label)

    def note_imported(self, label: str) -> None:
        self.imported.append(label)

    def note_review(self, label: str, reason: str = "") -> None:
        text = label if not reason else f"{label}: {reason}"
        self.review.append(text)

    def note_failed(self, label: str, reason: str) -> None:
        self.failed.append((label, reason))

    def line(self) -> str:
        return (
            f"This run: {len(self.grabbed)} grabbed, {len(self.imported)} imported, "
            f"{len(self.review)} in Review, {len(self.failed)} failed"
        )

    def text(self) -> str:
        lines = [
            f"Rummagarr run started {self.started}",
            self.line(),
            "",
            "Grabbed",
        ]
        lines.extend(f"- {item}" for item in self.grabbed)
        if not self.grabbed:
            lines.append("- none")
        lines.append("")
        lines.append("Imported")
        lines.extend(f"- {item}" for item in self.imported)
        if not self.imported:
            lines.append("- none")
        lines.append("")
        lines.append("Review")
        lines.extend(f"- {item}" for item in self.review)
        if not self.review:
            lines.append("- none")
        lines.append("")
        lines.append("Failed")
        lines.extend(f"- {label}: {reason}" for label, reason in self.failed)
        if not self.failed:
            lines.append("- none")
        lines.append("")
        return "\n".join(lines)

    def write(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        latest = directory / "run-summary.txt"
        stamp = self.started.replace(":", "").replace("-", "")
        dated = directory / f"run-{stamp}.txt"
        body = self.text()
        latest.write_text(body, encoding="utf-8")
        dated.write_text(body, encoding="utf-8")
        return latest
