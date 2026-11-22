"""
The `jarvis` command. One entry point for the three apps in this repository:

- the voice harness (`apps/voice`, package `jarvis_voice`): `jarvis`, `jarvis connect`, `jarvis meeting`,
  `jarvis remote`, `jarvis enroll`;
- the brain daemon (`apps/brain`, package `brain`): `jarvis serve|status|add|research|improve|launchd`;
- the cockpit (`apps/tui`, Rust binary `jarvis-top`): `jarvis top`.

Production is installed from GitHub master (`uv tool install git+...`) and updated with `jarvis upgrade`;
a dev checkout runs its own code with `uv run jarvis`. One mic, so only one may listen at a time.
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

from jarvis_core.home import JARVIS_HOME, PACKAGE, SHARED_HOME, checkout_root, install_info, workspace_root

REPO_URL = "https://github.com/sangdth/jarvis"

USAGE = """usage: jarvis [command]
  (none)     listen and hand work to the agents; Brain starts with it unless it is already running
  remote     listen for a thin client on the network: another Mac's mic and speaker, every model and agent here
  meeting    sit in a call with you (hears the call, speaks into Google Meet through a Chrome extension); also `--meeting`
             after `connect`
  connect    continue the T3 Code thread this runs in (a T3 project script), or pick one from a list
  connect X  continue a session of agent X (claude, opencode or pi), picked from a list; for pi, a window open
             now (installs Jarvis's Pi extension the first time)
  top        open the cockpit: tasks, attempts, spend and live telemetry (the Rust TUI)
  status     the brain's mode, spend, tasks and active skills
  serve      run Brain on its own, as the 24/7 daemon (what `jarvis launchd --install` starts)
  add        queue a goal or problem: `jarvis add --repo <repo> --kind goal --title ... --spec ...`
  research   run the hypothesis fan-out on a task: `jarvis research SG-1`
  improve    run the brain's skill reflection loop on recent attempts
  launchd    print or install the brain's LaunchAgent (`--install`)
  brain X    any of the Brain commands above, spelled out (`jarvis brain status`)
  init       set up Jarvis's home (~/.config/jarvis; the checkout in dev): .env, agents.json, SOUL.md, brain, voiceprint
  enroll     record your voiceprint again
  upgrade    update Jarvis (an installed one from GitHub master; a dev checkout fast-forwarded to origin/master)
  uninstall  delete the state in ~/.config/jarvis (keys, voiceprint, memory, brain) and the installed jarvis command
  --version  show which Jarvis this is: master @ commit, or dev and its folder"""

BRAIN_COMMANDS = {"serve", "status", "add", "research", "improve", "launchd", "self-check"}


def version() -> str:
    """`master @ abc1234` for a git install, `dev (<folder>)` for a checkout (pip's direct_url.json says which)."""
    info = install_info()
    vcs = info.get("vcs_info")
    if vcs:
        return f"{vcs.get('requested_revision') or 'git'} @ {vcs.get('commit_id', '?')[:7]}"
    return f"dev ({checkout_root(info)})"


def upgrade(flags: list[str]) -> None:
    """`jarvis upgrade`: a checkout fast-forwards (the brain's divergence-safe upgrade, flags and all)."""
    if version().startswith("dev"):
        return brain(["upgrade", *flags])
    if flags:
        print("`jarvis upgrade` takes no flags for an installed Jarvis; updating from GitHub master.")
    update_installed()


def update_installed() -> None:
    """`uv tool upgrade` for the installed Jarvis; the cockpit is rebuilt if it is here."""
    before = version()
    uv = shutil.which("uv")
    if uv is None:
        sys.exit("`jarvis upgrade` needs uv on the PATH.")
    if subprocess.run([uv, "tool", "upgrade", PACKAGE]).returncode != 0:
        sys.exit("Update failed; the installed Jarvis is unchanged.")
    update_cockpit()
    # A fresh process: this one still has the old package's metadata loaded.
    after = subprocess.run([sys.executable, "-m", "jarvis.cli", "--version"], capture_output=True, text=True).stdout.strip()
    print(f"Jarvis is up to date: {after}." if after == before else f"Updated Jarvis: {before} -> {after}.")


def update_cockpit() -> None:
    """Rebuild the Rust cockpit in step with the Python side, when it was built and cargo is here."""
    if shutil.which("jarvis-top") is None or shutil.which("cargo") is None:
        return
    print("Rebuilding the cockpit ...")
    if subprocess.run(
        ["cargo", "install", "--git", REPO_URL, "--locked", "--bin", "jarvis-top", "--force"],
        capture_output=True,
    ).returncode != 0:
        print("The cockpit did not rebuild; `jarvis top` keeps working on the previous binary.")


def folder_size(path: Path) -> str:
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())
    return f"{total / 1e6:.0f} MB" if total >= 1e6 else f"{total / 1e3:.0f} KB"


def uninstall(home: Path = SHARED_HOME) -> None:
    """Delete the installed state, then the `jarvis` command; a dev checkout keeps its code and its own state."""
    installed = not version().startswith("dev")
    uv = shutil.which("uv")
    if installed and uv is None:
        sys.exit("`jarvis uninstall` needs uv on the PATH to remove the jarvis command; nothing was deleted.")
    lock: IO[str] | None = None
    try:
        if home.exists():
            lock = hold_lock(home)  # a listening Jarvis is still writing memory there
            print(f"This deletes {home} ({folder_size(home)}): API keys, SOUL.md, your voiceprint, models, memory,")
            print("and the brain's board, worktrees and skills.")
        if installed:
            print("It also removes the jarvis command from your PATH.")
        if not home.exists() and not installed:
            return print(f"Nothing to uninstall: {home} is gone, and a dev checkout's command is `uv run jarvis`.")
        if input("Delete it all? y/N: ").strip().lower() != "y":
            return print("Nothing was deleted.")
        if home.exists():
            shutil.rmtree(home)  # still holding the lock, so no Jarvis starts halfway through
            if lock is not None:
                lock.close()
            print(f"Deleted {home}.")
    finally:
        if lock is not None and not lock.closed:
            lock.close()
    if installed and uv and subprocess.run([uv, "tool", "uninstall", PACKAGE]).returncode != 0:
        sys.exit(f"Removing the jarvis command failed; run `uv tool uninstall {PACKAGE}` yourself.")
    print("Left in place, shared with other tools: uv, Whisper models in ~/.cache/huggingface, Ollama's bge-m3 "
          "(`ollama rm bge-m3`), and a Rust `target/` folder if you built the cockpit.")


def hold_lock(home: Path = SHARED_HOME) -> IO[str]:
    """One listening Jarvis per machine: two would share the mic and Qdrant's memory folder. Keep the file open."""
    home.mkdir(parents=True, exist_ok=True)
    lock = (home / "jarvis.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the OS drops it when the process ends, even on a crash
    except BlockingIOError:
        lock.seek(0)
        pid = lock.read().strip() or "?"
        lock.close()
        sys.exit(f"Jarvis is already running (pid {pid}); stop it first.")
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()
    return lock


def cockpit_binary() -> Path | None:
    """The built `jarvis-top`: PATH, or a cargo `target/` in this checkout (`cargo build --release`)."""
    if found := shutil.which("jarvis-top"):
        return Path(found)
    root = workspace_root()
    if root is None:
        return None
    candidates = [root / "target" / profile / "jarvis-top" for profile in ("release", "debug")]
    candidates += [root / "apps" / "tui" / "target" / profile / "jarvis-top" for profile in ("release", "debug")]
    return next((path for path in candidates if path.is_file()), None)


def run_cockpit(args: list[str]) -> int:
    binary = cockpit_binary()
    if binary is None:
        print("`jarvis top` needs the cockpit built first:", file=sys.stderr)
        print("  cargo build --release --manifest-path apps/tui/Cargo.toml", file=sys.stderr)
        return 1
    try:
        return subprocess.call([str(binary), *args])
    except KeyboardInterrupt:
        return 130


def brain(args: list[str]) -> None:
    """The brain's own commands, reached through the one `jarvis` command."""
    from brain.cli import main as brain_main

    prepare_brain()
    sys.exit(brain_main(args))


def prepare_brain() -> None:
    from jarvis.supervisor import prepare_environment

    prepare_environment()


def main() -> None:
    args = sys.argv[1:]
    if args == ["--version"]:
        return print(version())
    if args and args[0] == "brain":
        args = args[1:] or ["status"]
    if args and args[0] in BRAIN_COMMANDS:
        return brain(args)
    if args[:1] in (["upgrade"], ["update"]):
        return upgrade(args[1:])
    if args == ["uninstall"]:
        return uninstall()
    if args == ["init"]:
        from jarvis.init import main as init

        return init()
    if args and args[0] == "top":
        sys.exit(run_cockpit(args[1:]))
    if args == ["enroll"]:
        prepare_brain()
        from jarvis_voice.enroll import main as enroll  # imported here: loads the speaker model

        return enroll()
    flags = {"meeting": "meeting", "--meeting": "meeting", "remote": "remote"}
    given = [a for a in args if a in flags]
    kinds = {flags[a] for a in given}
    if len(given) != len(kinds) or len(kinds) > 1:
        sys.exit(USAGE)  # `meeting` with `remote` (the call is this Mac's) or a flag twice
    remote, meeting = "remote" in kinds, "meeting" in kinds
    rest = [a for a in args if a not in flags]
    if rest and (rest[0] != "connect" or len(rest) > 2):
        sys.exit(USAGE)
    if checkout_root() and not (JARVIS_HOME / ".env").exists():
        sys.exit(f"No .env in {JARVIS_HOME}: a dev checkout uses its own. Run the T3 setup script or `uv run jarvis init`.")
    prepare_brain()
    lock = hold_lock()  # noqa: F841 (held until exit)
    logger.info(f"Jarvis {version()}")
    from jarvis.supervisor import brain_running
    from jarvis_voice.main import run_pipecat_harness  # imported here: loads Pipecat

    connect = None if not rest else rest[1] if len(rest) == 2 else "t3"

    async def listen() -> None:
        async with brain_running():
            await run_pipecat_harness(connect=connect, meeting=meeting, remote=remote)

    try:
        asyncio.run(listen())
    except KeyboardInterrupt:
        logger.info("Jarvis stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
