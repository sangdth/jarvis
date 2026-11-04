"""
The `jarvis` command. Production is installed from GitHub master (`uv tool install git+...`) and updated with
`jarvis update`; a dev checkout runs its own code with `uv run jarvis`. Both share ~/.config/jarvis, so only one
may listen at a time.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import IO

from loguru import logger

from jarvis.config import JARVIS_HOME

PACKAGE = "jarvis-harness"
USAGE = """usage: jarvis [command]
  (none)     listen and hand work to the agents
  connect    continue the T3 Code thread this runs in (a T3 project script), or pick one from a list
  init       set up ~/.config/jarvis: .env, agents.json, SOUL.md, then your voiceprint
  enroll     record your voiceprint again
  update     update the installed Jarvis to the latest GitHub master
  --version  show which Jarvis this is: master @ commit, or dev and its folder"""


def version() -> str:
    """`master @ abc1234` for a git install, `dev (<folder>)` for a checkout (pip's direct_url.json says which)."""
    try:
        info = json.loads(distribution(PACKAGE).read_text("direct_url.json") or "{}")
    except (PackageNotFoundError, ValueError):
        info = {}
    vcs = info.get("vcs_info")
    if vcs:
        return f"{vcs.get('requested_revision') or 'git'} @ {vcs.get('commit_id', '?')[:7]}"
    return f"dev ({info.get('url', '').removeprefix('file://') or Path(__file__).resolve().parents[2]})"


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


def hold_lock(home: Path = JARVIS_HOME) -> IO[str]:
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
    if args == ["init"]:
        from jarvis.soul import main as init

        return init()
    if args == ["enroll"]:
        from jarvis.enroll import main as enroll  # imported here: loads the speaker model

        return enroll()
    if args not in ([], ["connect"]):
        sys.exit(USAGE)
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
