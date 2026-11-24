"""`~/.config/jarvis/SYSTEM.md` (the checkout's in dev), optional: each `## Name` section replaces that shipped prompt.

A missing file or section keeps the prompt in `prompts.py`, so prompt fixes still reach the user. Read once at startup.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from loguru import logger

from jarvis_voice.config import JARVIS_HOME
from jarvis_voice.prompts import AGENT_REPORT_PROMPT, NOT_THE_USER

SYSTEM_PATH = JARVIS_HOME / "SYSTEM.md"
KNOWN = ("cleanup", "retell", "waiting")  # the section names Jarvis reads, lower case


def parse_overrides(text: str) -> dict[str, str]:
    """`## Name` sections by lower-case name; empty and unknown ones are left out (unknown ones logged)."""
    overrides: dict[str, str] = {}
    warned = False
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
            warned = True
        name, lines = line[3:].strip().lower() or None, []
    if text.strip() and not overrides and not warned:
        logger.warning(f"SYSTEM.md has no non-empty '## Name' section (known: {', '.join(KNOWN)}); nothing overridden.")
    return overrides


MAX_SECTION_CHARS = 4000  # an override longer than this is not a prompt any more; keep the shipped one


def validate_overrides(overrides: dict[str, str]) -> dict[str, str]:
    """Drop a section that cannot be a prompt, so a bad SYSTEM.md edit never reaches the microphone."""
    kept: dict[str, str] = {}
    for name, body in overrides.items():
        if len(body) > MAX_SECTION_CHARS:
            logger.warning(f"SYSTEM.md: section {name!r} is {len(body)} chars; keeping the shipped prompt.")
            continue
        if name == "cleanup" and "<transcript>" not in body:
            logger.warning("SYSTEM.md: '## Cleanup' must keep the <transcript> tags; keeping the shipped prompt.")
            continue
        kept[name] = body
    return kept


def build_retell_prompt(overrides: dict[str, str]) -> str:
    """The retell prompt, with the marker that keeps an agent's own words out of the user's voice."""
    prompt = overrides.get("retell") or AGENT_REPORT_PROMPT
    if prompt.startswith(NOT_THE_USER):
        return prompt
    return f"{NOT_THE_USER} an agent finished its turn.] {prompt}"


def load_overrides(path: Path = SYSTEM_PATH) -> dict[str, str]:
    """The user's prompt overrides, or none when there is no file: Jarvis runs as shipped."""
    if not path.is_file():
        return {}
    overrides = validate_overrides(parse_overrides(path.read_text(encoding="utf-8")))
    for name, body in overrides.items():
        logger.info(f"SYSTEM.md overrides '{name}' (sha256 {hashlib.sha256(body.encode('utf-8')).hexdigest()[:12]}).")
    return overrides
