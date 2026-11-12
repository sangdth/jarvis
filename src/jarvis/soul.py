"""`~/.config/jarvis/SOUL.md` (the checkout's in dev): front matter holds settings, the body is personality."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jarvis.config import JARVIS_HOME

SOUL_PATH = JARVIS_HOME / "SOUL.md"
DEFAULT_PROJECTS_DIR = "~/Projects"


@dataclass(frozen=True)
class Soul:
    personality: str = ""  # free text, added to the system prompt as tone only
    projects_dir: str = DEFAULT_PROJECTS_DIR  # as the user wrote it, e.g. "~/Projects"


def parse_soul(text: str) -> Soul:
    """`---` front matter of `key: value` lines (settings), then the personality as plain text."""
    settings: dict[str, str] = {}
    body = text
    if text.startswith("---\n") and "\n---" in text[4:]:
        head, _, body = text[4:].partition("\n---")
        for line in head.splitlines():
            key, sep, value = line.partition(":")
            if sep:
                settings[key.strip()] = value.strip()
    return Soul(
        personality=body.strip(),
        projects_dir=settings.get("projects_dir") or DEFAULT_PROJECTS_DIR,
    )


_cache: dict[Path, tuple[int | None, Soul]] = {}


def load_soul(path: Path = SOUL_PATH) -> Soul:
    """The saved soul, or the defaults when there is none: Jarvis runs as shipped."""
    try:
        mtime: int | None = path.stat().st_mtime_ns
    except OSError:
        mtime = None
    cached = _cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    soul = parse_soul(path.read_text(encoding="utf-8")) if mtime is not None else Soul()
    _cache[path] = (mtime, soul)
    return soul


def render_soul(personality: str, user_name: str, projects_dir: str, extra: str) -> str:
    lines = [f"Personality: {personality}."]
    if user_name:
        lines.append(f'Call the user "{user_name}".')
    if extra:
        lines.append(extra)
    return f"---\nprojects_dir: {projects_dir}\n---\n" + "\n".join(lines) + "\n"
