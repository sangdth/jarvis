"""
The `jarvis` command: a thin launcher for the one process that runs this checkout.

There is no build, wheel or tool install between the code and what runs: the repository *is* the runtime. An edit
plus `jarvis restart` is live. The CLI only starts, stops and talks to that process:

- foreground: `jarvis` (local mic), `jarvis remote`, `jarvis meeting`, `jarvis connect [agent]`
- as a service: `jarvis start | stop | restart | logs`
- over HTTP: `jarvis status | health | ask | add | board | tasks | mode | research | improve`
- once: `jarvis init | enroll | top`

Everything network-shaped is one HTTP/SSE/WebSocket server on `BRAIN_PORT`, so a thin client on another machine
needs no `jarvis` install at all (`jarvis health --url http://<mac-mini>:8643`).
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import IO, Any

import httpx
from loguru import logger

from jarvis_core.home import IPC_HOME, JARVIS_HOME, REPO_ROOT, brain_token, git_version, workspace_root

LAUNCHD_LABEL = "com.sangdth.jarvis"
PLIST_PATH = Path(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist").expanduser()

USAGE = """usage: jarvis [command]

foreground (a terminal holds it open):
  (none)          listen with this Mac's mic and speaker
  remote          listen for a thin client: another Mac's mic and speaker, every model and agent here
  meeting         sit in a call with you (hears the call, speaks into Google Meet)
  connect [X]     continue the T3 thread this runs in, or a session of claude | opencode | pi
  serve [flags]   the same stack, spelled out (`--remote`, `--meeting`, `--connect X`)

as a service (launchd keeps it up, 24/7):
  start [flags]   install/refresh the LaunchAgent and start it; keeps the mode it already had
  stop            stop it
  restart         sync, refresh the LaunchAgent, restart: the only step an edit needs
  logs            tail the daemon's log

over the network (one HTTP/SSE/WebSocket server, one token):
  health          is a Jarvis answering here? (`--url http://<mac-mini>:8643` for another machine)
  status          version, uptime, mode, spend, tasks, voice
  ask "text"      one-shot request, answer printed
  board           the full board as JSON
  tasks           list the tasks
  add             queue a goal or problem (`--repo`, `--kind`, `--title`, `--spec`, `--metric`, `--ready`)
  mode ASSIST|AUTONOMOUS
  research ID     run the hypothesis fan-out on a task
  improve         run the skill reflection loop
  restart --remote  ask the running Jarvis to restart itself

once:
  top             open the cockpit (the Rust TUI, built from this checkout)
  init            set up the state home, then offer to record your voiceprint
  enroll          record your voiceprint again
  --version       the running checkout's branch and commit
"""


def version() -> str:
    """`master @ abc1234` for this checkout; the code that is running is the code on disk."""
    return git_version()


def endpoint() -> str:
    from brain.config import settings

    return settings.endpoint_url


def token() -> str:
    from brain.config import settings

    return settings.brain_api_key or brain_token()


def call(method: str, path: str, *, url: str = "", timeout: float = 30.0, **kwargs: Any) -> dict[str, Any]:
    """One request to the running Jarvis; a clear message instead of a traceback when it is not there."""
    base = (url or endpoint()).rstrip("/")
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.request(
                method, f"{base}{path}", headers={"Authorization": f"Bearer {token()}"}, **kwargs
            )
    except httpx.HTTPError as exc:
        sys.exit(f"No Jarvis answers at {base} ({exc.__class__.__name__}). Is it running? `jarvis start`.")
    if resp.status_code == 401:
        sys.exit(f"{base} refused the token. Is BRAIN_API_KEY / JARVIS_HOME/brain_token the same on both sides?")
    try:
        body = resp.json()
    except ValueError:
        body = {"text": resp.text}
    if resp.status_code >= 400:
        sys.exit(f"{base}{path} failed ({resp.status_code}): {json.dumps(body)[:400]}")
    return body if isinstance(body, dict) else {"value": body}


# --------------------------------------------------------------------------------------------------------------
# The service: launchd runs this checkout, and `restart` is the only step an edit needs
# --------------------------------------------------------------------------------------------------------------
def hold_lock(home: Path = IPC_HOME) -> IO[str]:
    """One listening Jarvis per machine: two would share the mic and the vector store. Keep the file open."""
    home.mkdir(parents=True, exist_ok=True)
    lock = (home / "jarvis.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the OS drops it when the process ends, even on a crash
    except BlockingIOError:
        lock.seek(0)
        pid = lock.read().strip() or "?"
        lock.close()
        sys.exit(f"Jarvis is already running (pid {pid}); `jarvis restart` replaces it.")
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()
    return lock


def plist_serve_args() -> list[str]:
    """The mode the installed LaunchAgent already runs, so `restart` never silently changes it."""
    import re

    try:
        text = PLIST_PATH.read_text(encoding="utf-8")
    except OSError:
        return ["--remote"]  # a Mac mini server with no one at the mic
    if "<string>serve</string>" not in text:
        return ["--remote"]
    tail = text.split("<string>serve</string>", 1)[1].split("</array>", 1)[0]
    return [flag for flag in re.findall(r"<string>(.*?)</string>", tail) if flag.startswith("--")]


def write_plist(serve_args: list[str]) -> None:
    from brain.cli import build_launchd_plist

    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(build_launchd_plist(serve_args), encoding="utf-8")
    print(f"Wrote {PLIST_PATH} (mode: {' '.join(serve_args) or 'local'}).")


def launchd_domain() -> str:
    return f"gui/{os.getuid()}/{LAUNCHD_LABEL}"


def launchd_loaded() -> bool:
    if sys.platform != "darwin":
        return False
    return subprocess.run(["launchctl", "print", launchd_domain()], capture_output=True).returncode == 0


def launchd_kick() -> None:
    kick = subprocess.run(["launchctl", "kickstart", "-k", launchd_domain()], capture_output=True, text=True)
    if kick.returncode != 0:
        sys.exit(f"`launchctl kickstart -k {launchd_domain()}` failed: {kick.stderr.strip() or kick.stdout.strip()}")


def launchd_boot() -> None:
    subprocess.run(["launchctl", "bootout", launchd_domain()], capture_output=True)
    boot = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST_PATH)], capture_output=True, text=True
    )
    if boot.returncode != 0:
        sys.exit(f"`launchctl bootstrap` failed: {boot.stderr.strip() or boot.stdout.strip()}")


def needs_sync() -> bool:
    """There is a `.venv` to keep in step, and the daemon runs it."""
    return (REPO_ROOT / ".venv").is_dir()


def sync_dependencies() -> None:
    """Make `.venv` match this checkout; it is what the daemon runs, so it is the only build step there is."""
    import shutil

    if not needs_sync():
        return
    uv = shutil.which("uv")
    if uv is None:
        print("`uv` is not on PATH; skipping the dependency sync (run it yourself if you changed pyproject.toml).")
        return
    proc = subprocess.run([uv, "sync", "--quiet"], cwd=str(REPO_ROOT))
    if proc.returncode != 0:
        sys.exit("`uv sync` failed; the running code was not restarted. Fix it and run `jarvis restart` again.")


def service_start(serve_args: list[str] | None = None) -> None:
    if sys.platform != "darwin":
        sys.exit("`jarvis start` uses launchd; on another system run `jarvis serve` in a terminal.")
    sync_dependencies()
    write_plist(serve_args if serve_args is not None else plist_serve_args())
    if launchd_loaded():
        launchd_kick()
    else:
        launchd_boot()
    print(f"Started {LAUNCHD_LABEL}. `jarvis status` once it is up; `jarvis logs` follows the log.")


def service_stop() -> None:
    if not launchd_loaded():
        print(f"{LAUNCHD_LABEL} is not running.")
        return
    subprocess.run(["launchctl", "bootout", launchd_domain()], capture_output=True)
    print(f"Stopped {LAUNCHD_LABEL}.")


def service_restart(serve_args: list[str] | None = None) -> None:
    """`jarvis restart`: best-effort sync, build cockpit, refresh the LaunchAgent, restart, and open `top`."""
    sync_dependencies()
    build_cockpit()
    write_plist(serve_args if serve_args is not None else plist_serve_args())
    if sys.platform != "darwin":
        sys.exit("`jarvis restart` uses launchd; on another system stop and re-run `jarvis serve`.")
    launchd_kick() if launchd_loaded() else launchd_boot()
    print(f"Restarted {LAUNCHD_LABEL} on {version()}.")
    run_cockpit([])


def follow_logs() -> None:
    log = JARVIS_HOME / "logs" / "jarvis.err.log"
    out = JARVIS_HOME / "logs" / "jarvis.out.log"
    files = [str(p) for p in (log, out) if p.is_file()]
    if not files:
        sys.exit(f"No log yet in {log.parent}. Start it with `jarvis start`.")
    try:
        subprocess.run(["tail", "-n", "100", "-f", *files])
    except KeyboardInterrupt:
        pass


# --------------------------------------------------------------------------------------------------------------
# The cockpit
# --------------------------------------------------------------------------------------------------------------
def build_cockpit() -> None:
    """Build `jarvis-top` (`cargo build --release --workspace`) from this checkout."""
    import shutil

    root = workspace_root()
    if root is None or not (root / "Cargo.toml").is_file():
        return
    cargo = shutil.which("cargo")
    if cargo is None:
        print("`cargo` is not on PATH; skipping the cockpit build.")
        return
    proc = subprocess.run([cargo, "build", "--release", "--workspace"], cwd=str(root))
    if proc.returncode != 0:
        sys.exit("`cargo build --release --workspace` failed; the running code was not restarted.")


def cockpit_binary() -> Path | None:
    """The built `jarvis-top`: a cargo `target/` in this checkout, or PATH."""
    import shutil

    root = workspace_root()
    if root is not None:
        candidates = [root / "target" / profile / "jarvis-top" for profile in ("release", "debug")]
        candidates += [root / "apps" / "tui" / "target" / profile / "jarvis-top" for profile in ("release", "debug")]
        if built := next((path for path in candidates if path.is_file()), None):
            return built
    if found := shutil.which("jarvis-top"):
        return Path(found)
    return None


def run_cockpit(args: list[str]) -> int:
    binary = cockpit_binary()
    if binary is None:
        print("`jarvis top` needs the cockpit built first:", file=sys.stderr)
        print("  cargo build --release --workspace", file=sys.stderr)
        return 1
    try:
        return subprocess.call([str(binary), *args])
    except KeyboardInterrupt:
        return 130


# --------------------------------------------------------------------------------------------------------------
# Talking to the running Jarvis
# --------------------------------------------------------------------------------------------------------------
def print_status(url: str = "") -> None:
    s = call("GET", "/v1/status", url=url)
    voice = s.get("voice") or {}
    print(f"Jarvis {s.get('version')}  (pid {s.get('pid')}, up {s.get('uptime_s')}s)")
    print(f"  home      {s.get('home')}")
    print(f"  mode      {s.get('mode')}   spent today ${s.get('spent_today_usd', 0):.2f} / "
          f"${s.get('daily_budget_usd', 0):.2f}")
    print(f"  tasks     {s.get('tasks')} ({s.get('tasks_ready')} ready, {s.get('tasks_running')} running)")
    print(f"  skills    {s.get('skills_active')} active    approvals waiting: {s.get('approvals_waiting')}")
    print(f"  voice     {voice.get('state', 'unknown')}"
          + (f" (relay client connected on port {voice.get('port')})" if voice.get("client") else ""))


def dispatch(argv: list[str]) -> None:
    if not argv:
        return run_foreground()
    cmd, rest = argv[0], argv[1:]

    if cmd in ("-h", "--help", "help"):
        return print(USAGE)
    if cmd in ("-V", "--version", "version"):
        return print(version())
    if cmd == "top":
        sys.exit(run_cockpit(rest))
    if cmd == "logs":
        return follow_logs()
    if cmd == "start":
        return service_start(local_or_remote(rest) or None)
    if cmd == "stop":
        return service_stop()
    if cmd in ("restart", "update", "upgrade"):
        flags = local_or_remote(rest)
        return service_restart(flags or None)
    if cmd == "health" or cmd == "probe":
        url = _take(rest, "--url")
        body = call("GET", "/health", url=url, timeout=5.0)
        return print(f"ok  Jarvis {body.get('version')}  (pid {body.get('pid')}, up {body.get('uptime_s')}s)")
    if cmd == "status":
        return print_status(_take(rest, "--url"))
    if cmd == "board":
        return print(json.dumps(call("GET", "/v1/board"), indent=2))
    if cmd == "tasks":
        body = call("GET", "/v1/tasks")
        rows = body.get("tasks") or []
        if not rows:
            return print("No tasks.")
        for t in rows:
            print(f"{t['id']:>8} [{t['kind']}/{t.get('mode', 'AUTONOMOUS')}/{t['status']}] repo={t['repo']}: {t['title']}")
        return print(f"\n{len(rows)} task(s).")
    if cmd == "ask":
        text = " ".join(rest).strip()
        if not text:
            sys.exit('usage: jarvis ask "what do you want"')
        body = call("POST", "/v1/ask", json={"input": text}, timeout=900.0)
        return print(body.get("spoken", ""))
    if cmd == "mode":
        if not rest:
            sys.exit("usage: jarvis mode ASSIST|AUTONOMOUS")
        body = call("POST", "/v1/mode", json={"mode": rest[0]})
        return print(f"Mode {body.get('mode')}, ${body.get('budget_remaining_usd', 0):.2f} left today.")
    if cmd == "research":
        if not rest:
            sys.exit("usage: jarvis research SG-1")
        body = call("POST", f"/v1/tasks/{rest[0]}/research", timeout=3600.0)
        return print(f"[{body.get('outcome')}] {body.get('summary')} (hypotheses={body.get('hypotheses') or []})")
    if cmd == "improve":
        body = call("POST", "/v1/skills/improve", timeout=3600.0)
        return print(f"[{body.get('outcome')}] {body.get('summary')} (modified={body.get('modified') or []})")
    if cmd == "add":
        return add_task(rest)
    if cmd == "init":
        from jarvis.init import main as init

        return init()
    if cmd == "enroll":
        from jarvis_voice.enroll import main as enroll

        return enroll()

    # Foreground modes, with the flags allowed in any order: `jarvis remote connect`, `jarvis --meeting`.
    return run_foreground(argv)


def _take(args: list[str], flag: str) -> str:
    """`--url X` or `--url=X`, removed from the list when present."""
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            value = args.pop(i + 1)
            args.pop(i)
            return value
        if arg.startswith(f"{flag}="):
            args.pop(i)
            return arg.split("=", 1)[1]
    return ""


def local_or_remote(args: list[str]) -> list[str]:
    """`serve` flags from the command line, normalized to what the LaunchAgent will store."""
    flags: list[str] = []
    if "--remote" in args or "remote" in args:
        flags.append("--remote")
    if "--meeting" in args or "meeting" in args:
        flags.append("--meeting")
    for i, arg in enumerate(args):
        if arg == "--connect":
            if i + 1 < len(args):
                flags += ["--connect", args[i + 1]]
            else:
                flags += ["--connect"]
            break
    return flags


def add_task(args: list[str]) -> None:
    payload: dict[str, Any] = {"kind": "goal", "repo": "jarvis", "priority": 50}
    mapping = {
        "--repo": "repo",
        "--kind": "kind",
        "--mode": "mode",
        "--title": "title",
        "--spec": "spec",
        "--metric": "metric",
        "--verify": "verify",
        "--priority": "priority",
        "--id": "id",
    }
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--ready":
            payload["ready"] = True
            i += 1
            continue
        key = mapping.get(arg)
        if key is None:
            sys.exit(f"unknown flag {arg}\n\n{USAGE}")
        if i + 1 >= len(args):
            sys.exit(f"{arg} needs a value")
        payload[key] = args[i + 1]
        i += 2
    if not payload.get("title") or not payload.get("spec"):
        sys.exit("usage: jarvis add --repo <repo> --kind goal|problem --title ... --spec ... [--metric ...] [--ready]")
    body = call("POST", "/v1/tasks", json=payload)
    task = body.get("task") or {}
    print(f"Upserted {task.get('id')} ({task.get('status')}) in {task.get('repo')}: {task.get('title')}")


# --------------------------------------------------------------------------------------------------------------
# Foreground: the same stack, held by a terminal
# --------------------------------------------------------------------------------------------------------------
FLAG_ALIASES = {"meeting": "meeting", "--meeting": "meeting", "remote": "remote", "--remote": "remote"}


def parse_foreground(argv: list[str]) -> tuple[bool, bool, str | None]:
    """`(remote, meeting, connect)` from any order of the flags and the `connect` subcommand."""
    kinds = {FLAG_ALIASES[a] for a in argv if a in FLAG_ALIASES}
    if len(kinds) > 1:
        sys.exit("`remote` and `meeting` do not go together (the call is this Mac's).")
    remote, meeting = "remote" in kinds, "meeting" in kinds

    words = [a for a in argv if a not in FLAG_ALIASES]
    connect: str | None = None
    if words and words[0] in ("serve", "connect"):
        sub, words = words[0], words[1:]
        if sub == "connect":
            connect = words.pop(0) if words and not words[0].startswith("-") else "t3"
    if words and words[0].startswith("--connect"):
        flag = words.pop(0)
        connect = flag.split("=", 1)[1] if "=" in flag else (words.pop(0) if words else "t3")
    if words:
        sys.exit(USAGE)
    return remote, meeting, connect


def run_foreground(argv: list[str] | None = None) -> None:
    remote, meeting, connect = parse_foreground(list(argv or []))
    from jarvis.supervisor import prepare_environment

    prepare_environment()
    if not (JARVIS_HOME / ".env").exists():
        sys.exit(f"No .env in {JARVIS_HOME}: run `jarvis init` first.")
    lock = hold_lock()  # noqa: F841 (held until exit)
    logger.info(f"Jarvis {version()}")

    from jarvis.supervisor import start_all

    try:
        asyncio.run(start_all(connect=connect, meeting=meeting, remote=remote))
    except KeyboardInterrupt:
        logger.info("Jarvis stopped.")
        sys.exit(0)


def main() -> None:
    try:
        dispatch(sys.argv[1:])
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
