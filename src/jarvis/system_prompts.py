"""`~/.config/jarvis/SYSTEM.md` (the checkout's in dev), optional: each `## Name` section replaces that shipped prompt.

A missing file or section keeps the prompt in `prompts.py`, so prompt fixes still reach the user. Read once at startup.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from jarvis.config import JARVIS_HOME

SYSTEM_PATH = JARVIS_HOME / "SYSTEM.md"
KNOWN = ("cleanup",)  # the section names Jarvis reads, lower case


def parse_overrides(text: str) -> dict[str, str]:
    """`## Name` sections by lower-case name; empty and unknown ones are left out (unknown ones logged)."""
    overrides: dict[str, str] = {}
    name: str | None = None
    lines: list[str] = []
    for line in [*text.splitlines(), "## "]:  # the sentinel heading closes the last section
        if not line.startswith("## "):
            lines.append(line)
            continue
        body = "\n".join(lines).strip()
        if name in KNOWN and body:
            overrides[name] = body
        elif name is not None and name not in KNOWN:
            logger.warning(f"SYSTEM.md: unknown section {name!r} is ignored; known: {', '.join(KNOWN)}.")
        name, lines = line[3:].strip().lower() or None, []
    return overrides


def load_overrides(path: Path = SYSTEM_PATH) -> dict[str, str]:
    """The user's prompt overrides, or none when there is no file: Jarvis runs as shipped."""
    return parse_overrides(path.read_text(encoding="utf-8")) if path.is_file() else {}
