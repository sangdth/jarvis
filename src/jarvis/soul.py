"""
`jarvis init`: set up Jarvis's home (~/.config/jarvis; the checkout in dev). Copies .env and agents.json there, writes SOUL.md (personality and a
few settings) from a short interview, then records the voiceprint. The name stays "Jarvis"; the speech check and
the mute phrases depend on it.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from jarvis.config import JARVIS_HOME

SOUL_PATH = JARVIS_HOME / "SOUL.md"
DEFAULTS = Path(__file__).parent / "defaults"  # ships in the package, so a git install has it too
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


def load_soul(path: Path = SOUL_PATH) -> Soul:
    """The saved soul, or the defaults when there is none: Jarvis runs as shipped."""
    return parse_soul(path.read_text(encoding="utf-8")) if path.is_file() else Soul()


def render_soul(personality: str, user_name: str, projects_dir: str, extra: str) -> str:
    lines = [f"Personality: {personality}."]
    if user_name:
        lines.append(f'Call the user "{user_name}".')
    if extra:
        lines.append(extra)
    return f"---\nprojects_dir: {projects_dir}\n---\n" + "\n".join(lines) + "\n"


def ask(question: str, default: str = "") -> str:
    answer = input(f"{question} ({default or 'skip'}): ").strip()
    return answer or default


def copy_config(home: Path = JARVIS_HOME, defaults: Path = DEFAULTS) -> list[Path]:
    """Put the default .env and agents.json in home when missing; never overwrites the user's."""
    copied = []
    for name, source in ((".env", defaults / "env.example"), ("agents.json", defaults / "agents.json")):
        target = home / name
        if target.exists():
            continue
        home.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o600 if name == ".env" else 0o644)  # .env holds API keys
        copied.append(target)
    return copied


def main() -> None:
    print("\n=== Jarvis setup ===\n")
    for path in copy_config():
        print(f"Created {path}. Fill in or check it before you run Jarvis.")
    write = not SOUL_PATH.exists() or ask(f"{SOUL_PATH} exists. Replace it? y/N", "n").lower().startswith("y")
    if write:
        personality = ask("Jarvis's personality, in a few words", "calm, dry wit, concise").rstrip(".")
        user_name = ask("What should Jarvis call you?")
        projects_dir = ask("Where do you keep your projects?", DEFAULT_PROJECTS_DIR)
        if not Path(projects_dir).expanduser().is_dir():
            print(f"Note: {projects_dir} is not a folder yet; Jarvis will list no projects until it is.")
        extra = ask("Anything else about how Jarvis should talk to you?")
        SOUL_PATH.parent.mkdir(parents=True, exist_ok=True)
        SOUL_PATH.write_text(render_soul(personality, user_name, projects_dir, extra), encoding="utf-8")
        print(f"\nSaved {SOUL_PATH}. Edit it by hand any time; Jarvis reads it at startup.\n")

    if ask("Record your voice now? Jarvis won't start without it (later: `jarvis enroll`). Y/n", "y").lower().startswith("y"):
        from jarvis.enroll import main as enroll  # imported here: loads the speaker model

        enroll()


if __name__ == "__main__":
    main()
