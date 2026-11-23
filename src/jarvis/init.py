"""
`jarvis init`: set up Jarvis's home (~/.config/jarvis; the checkout in dev). Copies .env and agents.json
there, moves a Brain state folder from before the merge, makes the shared brain token, asks for the API
keys it is missing, writes SOUL.md (personality and a few settings) from a short interview, then offers to
record the voiceprint for the master check. The name stays "Jarvis"; the speech check and the mute phrases
depend on it.
"""

from __future__ import annotations

import os
import re
import shutil
from getpass import getpass
from pathlib import Path

from jarvis_core.home import (
    JARVIS_HOME,
    brain_home,
    brain_token,
    migrate_legacy_brain_home,
    shared_dir,
)
from jarvis_core.soul import DEFAULT_PROJECTS_DIR, SOUL_PATH, render_soul

ENV_PATH = JARVIS_HOME / ".env"
# Keys `jarvis init` asks for; the others (OpenCode, T3) belong to agents the user may not run.
API_KEYS = (
    ("OPENROUTER_API_KEY", "OpenRouter API key (https://openrouter.ai/keys), so Jarvis can think", ""),
    ("ELEVENLABS_API_KEY", "ElevenLabs API key (https://elevenlabs.io/app/settings/api-keys), so Jarvis can speak", "sk_"),
)
DEFAULTS = Path(__file__).parent / "defaults"  # ships in the package, so a git install has it too


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
        shared_dir(home)
        shutil.copyfile(source, target)
        target.chmod(0o600 if name == ".env" else 0o644)  # .env holds API keys
        copied.append(target)
    return copied


def setup_brain(home: Path = JARVIS_HOME) -> list[str]:
    """Give the brain its folder on the shared home and one bearer token; move a pre-merge Brain home."""
    notes = []
    moved = migrate_legacy_brain_home()
    if moved is not None:
        notes.append(f"Moved the old Brain state to {moved} (board, worktrees, skills and sessions are kept).")
    brain = brain_home()
    shared_dir(brain)
    token_file = home / "brain_token"
    had_token = token_file.is_file() and bool(token_file.read_text(encoding="utf-8").strip())
    brain_token()  # writes JARVIS_HOME/brain_token (0600) unless .env or the shell already names one
    if not had_token and token_file.is_file():
        notes.append(f"Made a brain token in {token_file}: the daemon, Jarvis and the cockpit share it.")
    return notes


def ask_api_keys(env: Path = ENV_PATH) -> None:
    """Fill each empty key in .env from a hidden prompt; a key set in .env or the shell is kept, Enter skips."""
    if not env.is_file():
        return
    for name, question, prefix in API_KEYS:
        if os.environ.get(name, "").strip():  # Settings reads the shell's value first, so .env needs no copy
            print(f"{name} is set in your shell; using it and leaving {env} empty. Keep it exported where Jarvis runs.")
            continue
        text = env.read_text(encoding="utf-8")
        line = re.search(rf"^{name}=(.*)$", text, re.MULTILINE)
        if line and line.group(1).strip():
            print(f"{name} is already set in {env}; keeping it.")
            continue
        key = getpass(f"{question} (hidden, Enter to skip): ").strip()
        if not key:
            print(f"Skipped {name}. Add it to {env} later.")
            continue
        if not re.fullmatch(r"[\w.\-]+", key):  # anything else would break the .env line
            print(f"That doesn't look like a key; skipped {name}. Add it to {env} later.")
            continue
        if prefix and not key.startswith(prefix):
            print(f"Note: {name} usually starts with {prefix}; saved it anyway.")
        set_env(name, key, env)
    env.chmod(0o600)  # .env holds API keys


def set_env(name: str, value: str, env: Path = ENV_PATH) -> None:
    """Set NAME=value in .env: replace its line, or add one at the end."""
    text = env.read_text(encoding="utf-8") if env.is_file() else ""
    line = re.search(rf"^{name}=(.*)$", text, re.MULTILINE)
    entry = f"{name}={value}"
    text = text[: line.start()] + entry + text[line.end():] if line else text.rstrip("\n") + f"\n{entry}\n"
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text(text, encoding="utf-8")
    env.chmod(0o600)  # .env holds API keys


def main() -> None:
    print("\n=== Jarvis setup ===\n")
    for path in copy_config():
        print(f"Created {path}. Check it before you run Jarvis.")
    for note in setup_brain():
        print(note)
    ask_api_keys()
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
        SOUL_PATH.chmod(0o600)
        print(f"\nSaved {SOUL_PATH}. Edit it by hand any time; Jarvis reads it at startup.\n")

    question = "Record your voice so Jarvis acts only on yours? (later: `jarvis enroll`) Y/n"
    if ask(question, "y").lower().startswith("y"):
        from jarvis.cli import paused_listening
        from jarvis_voice.enroll import main as enroll  # imported here: loads the speaker model

        with paused_listening():
            enroll()
    else:
        # The master check is on by default, and it refuses to start without a voiceprint: record the choice.
        set_env("MASTER_CHECK", "false", ENV_PATH)
        print("\nMaster check off for now: any voice can talk to Jarvis. `jarvis enroll` turns it on.\n")


if __name__ == "__main__":
    main()
