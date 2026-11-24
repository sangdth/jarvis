"""`~/.config/jarvis/SOUL.md` (the checkout's in dev): front matter holds settings, the body is personality."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from jarvis_core.home import JARVIS_HOME

SOUL_PATH = JARVIS_HOME / "SOUL.md"
DEFAULT_PROJECTS_DIR = "~/Projects"
_USER_NAME_RE = re.compile(r'The user\'s name is "([^"]+)"')


@dataclass(frozen=True)
class Soul:
    personality: str = ""  # free text, added to the system prompt as tone only
    projects_dir: str = DEFAULT_PROJECTS_DIR  # as the user wrote it, e.g. "~/Projects"


def parse_soul(text: str) -> Soul:
    """`---` front matter of `key: value` lines (settings), then the personality as plain text."""
    settings: dict[str, str] = {}
    body = text
    if text.startswith("---\n") and "\n---" in text[3:]:
        head, _, body = text[3:].partition("\n---")
        for line in head.splitlines():
            key, sep, value = line.partition(":")
            if sep:
                settings[key.strip()] = value.strip()
    return Soul(
        personality=body.strip(),
        projects_dir=settings.get("projects_dir") or DEFAULT_PROJECTS_DIR,
    )


def load_soul(path: Path = SOUL_PATH) -> Soul:
    """The saved soul, or the defaults when there is none: Jarvis runs as shipped."""
    return parse_soul(path.read_text(encoding="utf-8")) if path.is_file() else Soul()


def render_soul(personality: str, user_name: str, projects_dir: str, extra: str) -> str:
    lines = [f"Personality: {personality}."]
    if user_name:
        lines.append(f'The user\'s name is "{user_name}"; use it rarely, not in every reply.')
    if extra:
        lines.append(extra)
    return f"---\nprojects_dir: {projects_dir}\n---\n" + "\n".join(lines) + "\n"


def owner_name(personality: str) -> str | None:
    """The user's name written in `SOUL.md`, or None when not set yet."""
    m = _USER_NAME_RE.search(personality)
    return m.group(1).strip() if m else None


def strip_owner_name(personality: str) -> str:
    """Drop the stored owner-name line when the live speaker roster supplies names per turn."""
    lines = [line for line in personality.splitlines() if not _USER_NAME_RE.search(line)]
    return "\n".join(lines).strip()


def remember_owner_name(name: str, path: Path = SOUL_PATH, *, overwrite: bool = False) -> bool:
    """Record the owner's name in `SOUL.md` (replaces an existing name only when `overwrite=True`)."""
    first = next((line for line in name.splitlines() if line.strip()), "")
    cleaned = " ".join(first.replace('"', "").split())
    if not cleaned:
        return False
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if _USER_NAME_RE.search(existing):
        if not overwrite:
            return False
        text = _USER_NAME_RE.sub(lambda _: f'The user\'s name is "{cleaned}"', existing, count=1)
    else:
        line = f'The user\'s name is "{cleaned}"; use it rarely, not in every reply.'
        text = (
            f"{existing.rstrip(chr(10))}\n{line}\n"
            if existing.strip()
            else f"---\nprojects_dir: {DEFAULT_PROJECTS_DIR}\n---\n{line}\n"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return True
