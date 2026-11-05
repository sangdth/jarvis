"""
The `jarvis` command. Production is installed from GitHub master (`uv tool install git+...`) and updated with
`jarvis update` and keeps its state in ~/.config/jarvis; a dev checkout runs its own code with `uv run jarvis` and
keeps its state in the checkout. One mic, so only one may listen at a time.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import IO

from loguru import logger

from jarvis.config import JARVIS_HOME, PACKAGE, SHARED_HOME, checkout_root, install_info

USAGE = """usage: jarvis [command]
  (none)     listen and hand work to the agents
  connect    continue the T3 Code thread this runs in (a T3 project script), or pick one from a list
  init       set up Jarvis's home (~/.config/jarvis; the checkout in dev): .env, agents.json, SOUL.md, voiceprint
  enroll     record your voiceprint again
  update     update the installed Jarvis to the latest GitHub master
  uninstall  delete ~/.config/jarvis (keys, voiceprint, memory) and remove the installed jarvis command
  --version  show which Jarvis this is: master @ commit, or dev and its folder"""


def version() -> str:
    """`master @ abc1234` for a git install, `dev (<folder>)` for a checkout (pip's direct_url.json says which)."""
    info = install_info()
    vcs = info.get("vcs_info")
    if vcs:
        return f"{vcs.get('requested_revision') or 'git'} @ {vcs.get('commit_id', '?')[:7]}"
    return f"dev ({checkout_root(info)})"


def update() -> None:
    before = version()
    if before.startswith("dev"):
        sys.exit(f"This is a dev checkout, {before}: update it with git pull. `jarvis update` is for the installed one.")
    uv = shutil.which("uv")
    if uv is None:
        sys.exit("`jarvis update` needs uv on the PATH.")
    if subprocess.run([uv, "tool", "upgrade", PACKAGE]).returncode != 0:
        sys.exit("Update failed; the installed Jarvis is unchanged.")
    # A fresh process: this one still has the old package's metadata loaded.
    after = subprocess.run([sys.executable, "-m", "jarvis.cli", "--version"], capture_output=True, text=True).stdout.strip()
    print(f"Jarvis is up to date: {after}." if after == before else f"Updated Jarvis: {before} -> {after}.")


def folder_size(path: Path) -> str:
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())
    return f"{total / 1e6:.0f} MB" if total >= 1e6 else f"{total / 1e3:.0f} KB"


def uninstall(home: Path = SHARED_HOME) -> None:
    """Delete the installed state, then the `jarvis` command; a dev checkout keeps its code and its own state."""
    installed = not version().startswith("dev")
    uv = shutil.which("uv")
    if installed and uv is None:
        sys.exit("`jarvis uninstall` needs uv on the PATH to remove the jarvis command; nothing was deleted.")
    if home.exists():
        lock = hold_lock(home)  # a listening Jarvis is still writing memory there
        print(f"This deletes {home} ({folder_size(home)}): API keys, SOUL.md, your voiceprint, models and memory.")
    if installed:
        print("It also removes the jarvis command from your PATH.")
    if not home.exists() and not installed:
        return print(f"Nothing to uninstall: {home} is gone, and a dev checkout's command is `uv run jarvis`.")
    if input("Delete it all? y/N: ").strip().lower() != "y":
        return print("Nothing was deleted.")
    if home.exists():
        shutil.rmtree(home)  # still holding the lock, so no Jarvis starts halfway through
        lock.close()
        print(f"Deleted {home}.")
    if installed and subprocess.run([uv, "tool", "uninstall", PACKAGE]).returncode != 0:
        sys.exit(f"Removing the jarvis command failed; run `uv tool uninstall {PACKAGE}` yourself.")
    print("Left in place, shared with other tools: uv, Whisper models in ~/.cache/huggingface, Ollama's bge-m3 "
          "(`ollama rm bge-m3`).")


def hold_lock(home: Path = SHARED_HOME) -> IO[str]:
    """One listening Jarvis per machine: two would share the mic and Qdrant's memory folder. Keep the file open."""
    home.mkdir(parents=True, exist_ok=True)
    lock = (home / "jarvis.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the OS drops it when the process ends, even on a crash
    except BlockingIOError:
        lock.seek(0)
        sys.exit(f"Jarvis is already running (pid {lock.read().strip() or '?'}); stop it first.")
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()
    return lock


def main() -> None:
    args = sys.argv[1:]
    if args == ["--version"]:
        return print(version())
    if args == ["update"]:
        return update()
    if args == ["uninstall"]:
        return uninstall()
    if args == ["init"]:
        from jarvis.soul import main as init

        return init()
    if args == ["enroll"]:
        from jarvis.enroll import main as enroll  # imported here: loads the speaker model

        return enroll()
    if args not in ([], ["connect"]):
        sys.exit(USAGE)
    if checkout_root() and not (JARVIS_HOME / ".env").exists():
        sys.exit(f"No .env in {JARVIS_HOME}: a dev checkout uses its own. Run the T3 setup script or `uv run jarvis init`.")
    lock = hold_lock()  # noqa: F841 (held until exit)
    logger.info(f"Jarvis {version()}")
    from jarvis.main import run_pipecat_harness  # imported here: loads Pipecat

    try:
        asyncio.run(run_pipecat_harness(connect=args == ["connect"]))
    except KeyboardInterrupt:
        logger.info("Jarvis stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
