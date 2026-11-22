"""
Meeting comments: what people say that is worth keeping, without giving them any control.

In `jarvis meeting` anyone may add a comment ("note that we're going with Postgres"). It is written to
`JARVIS_HOME/meeting/<date>.md` with who said it and when; nothing runs, nothing is approved, and it never reaches
long-term memory. The notes are a plain Markdown file the user can read, edit or delete.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from jarvis_voice.config import JARVIS_HOME

NOTES_DIR = JARVIS_HOME / "meeting"


def notes_path(day: date | None = None, directory: Path | None = None) -> Path:
    """One Markdown file per day, so a meeting reads like a page, not a log stream."""
    day = day or date.today()
    return (directory or NOTES_DIR) / f"{day:%Y-%m-%d}.md"


def clean_speaker(label: str | None) -> str:
    """`Speaker: David` / `Room` / `User` as a name a person would write: `David`, `The room`, `You`."""
    text = (label or "").strip()
    if text.lower().startswith("speaker:"):
        return text.split(":", 1)[1].strip() or "Someone"
    return {"user": "You", "room": "The room"}.get(text.lower(), text) or "Someone"


def append_comment(
    text: str,
    speaker: str | None = None,
    *,
    day: date | None = None,
    directory: Path | None = None,
    now: datetime | None = None,
) -> tuple[Path, int]:
    """Add one comment to the day's notes; returns the file and how many comments it holds."""
    body = " ".join((text or "").split())
    if not body:
        raise ValueError("a comment needs text")
    path = notes_path(day, directory)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    when = (now or datetime.now()).strftime("%H:%M")
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8") as handle:
        if new:
            handle.write(f"# Meeting notes — {(day or date.today()):%A %d %B %Y}\n\n")
        handle.write(f"- **{when} · {clean_speaker(speaker)}:** {body}\n")
    return path, count_comments(path)


def count_comments(path: Path) -> int:
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("- **"))
    except OSError:
        return 0


def read_comments(day: date | None = None, *, directory: Path | None = None, limit: int = 60) -> str:
    """The day's notes as text for the model to summarize; empty string when nothing was noted."""
    path = notes_path(day, directory)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-limit:]).strip()
