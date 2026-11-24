"""
The `jarvis` command: a thin launcher for the one process that runs this checkout.

There is no build, wheel or tool install between the code and what runs: the repository *is* the runtime. An edit
plus `jarvis start` is live. The CLI only starts, stops and talks to that process:

- foreground: `jarvis` (this Mac's mic and speaker, or a paired Mac's), `jarvis meeting`, `jarvis connect [agent]`
- as a service: `jarvis start | stop | logs`
- over HTTP: `jarvis status | health | ask | add | board | tasks | mode | research | improve | timers | remind | schedule`
- once: `jarvis init | enroll | dashboard`

Everything network-shaped is one HTTP/SSE/WebSocket server on `BRAIN_PORT`, so a thin client on another machine
needs no `jarvis` install at all (`jarvis health --url http://<mac-mini>:8643`).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import IO, Any, Iterator

import httpx
from loguru import logger

from jarvis_core.home import IPC_HOME, JARVIS_HOME, REPO_ROOT, brain_home, brain_token, git_version, workspace_root

LAUNCHD_LABEL = "com.sangdth.jarvis"
PLIST_PATH = Path(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist").expanduser()

USAGE = """usage: jarvis [command]

foreground (a terminal holds it open):
  (none)          listen with this Mac's mic and speaker, or a paired Mac's when it connects
  meeting         sit in a call with you (hears the call, speaks into Google Meet)
  connect [X]     continue the T3 thread this runs in, or a session of claude | opencode | pi
  serve [flags]   the same stack, spelled out (`--meeting`, `--connect X`)

a paired Mac (no install, no mode to start):
  jarvis pair     show a one-time code; that Mac runs the thin client and owns the mic and speaker while connected
  jarvis devices  list them (`devices revoke <id>` cuts one off)

as a service (launchd keeps it up, 24/7):
  start           sync, build the dashboard and the mac app, (re)start it, then open the dashboard;
                  keeps the mode it already had
  stop            stop it
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
  traces          recent turns and runs (`--failed`, `--surface X`, `--search "words"`, `<id>`, diff A..B)
  timers          list follow-ups and schedules (`timers cancel <id>`)
  remind "text" in 30m
                  a one-shot check, spoken when it fires (also `at 08:00`)
  schedule --every "0 8 * * 1-5" --title "Morning" --prompt "check the PRs"
                  a recurring check; add `--action task` to queue board work instead

once:
  dashboard       open the dashboard (the Rust dashboard, built from this checkout)
  init            set up the state home, keys and personality
  enroll          learn a new owner voice through a short chat (pauses the running service)
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
# The service: launchd runs this checkout, and `jarvis start` is the only step an edit needs
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
        sys.exit(f"Jarvis is already running (pid {pid}); `jarvis start` replaces it.")
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()
    return lock


def lock_owner(home: Path = IPC_HOME) -> str | None:
    """The pid of the Jarvis holding the one-mic lock, or `None` when nobody is listening."""
    home.mkdir(parents=True, exist_ok=True)
    with (home / "jarvis.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.seek(0)
            return lock.read().strip() or "?"
        fcntl.flock(lock, fcntl.LOCK_UN)
    return None


@contextlib.contextmanager
def paused_listening() -> Iterator[None]:
    """`jarvis enroll` needs the mic to itself: a live Jarvis keeps answering every sentence it hears."""
    running = launchd_loaded()
    if running:
        subprocess.run(["launchctl", "bootout", launchd_domain()], capture_output=True)
        deadline = time.monotonic() + 10.0  # the job is gone, but the process may still be releasing the mic
        while lock_owner() is not None and time.monotonic() < deadline:
            time.sleep(0.2)
        print("Jarvis is paused while you record; it starts again when this ends.")
    elif (owner := lock_owner()) is not None:
        sys.exit(f"Jarvis is listening in another terminal (pid {owner}): stop it, then run `jarvis enroll` again.")
    try:
        yield
    finally:
        if running:
            launchd_start()
            print("Jarvis is listening again.")


def plist_serve_args() -> list[str]:
    """The mode the installed LaunchAgent already runs, so `jarvis start` never silently changes it."""
    import re

    try:
        text = PLIST_PATH.read_text(encoding="utf-8")
    except OSError:
        return []  # a fresh install: the mic and speaker are this Mac's, and a paired Mac's when it connects
    if "<string>serve</string>" not in text:
        return []
    tail = text.split("<string>serve</string>", 1)[1].split("</array>", 1)[0]
    values = re.findall(r"<string>(.*?)</string>", tail)
    flags: list[str] = []
    i = 0
    while i < len(values):
        arg = values[i]
        # `--remote` was its own mode once; the thin client is always served now, so an old plist is cleaned up here.
        if arg == "--remote":
            i += 1
            continue
        if arg.startswith("--"):
            flags.append(arg)
            if arg == "--connect" and i + 1 < len(values) and not values[i + 1].startswith("--"):
                flags.append(values[i + 1])  # the agent name, not a flag
                i += 1
        i += 1
    return flags


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


def launchd_start() -> None:
    boot = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST_PATH)], capture_output=True, text=True
    )
    if boot.returncode != 0:
        sys.exit(f"`launchctl bootstrap` failed: {boot.stderr.strip() or boot.stdout.strip()}")


def launchd_boot() -> None:
    subprocess.run(["launchctl", "bootout", launchd_domain()], capture_output=True)
    launchd_start()


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
        sys.exit("`uv sync` failed; the running code was not restarted. Fix it and run `jarvis start` again.")


def service_start(serve_args: list[str] | None = None) -> None:
    """`jarvis start`: sync, build what compiles, (re)start it, then show the dashboard."""
    if sys.platform != "darwin":
        sys.exit("`jarvis start` uses launchd; on another system run `jarvis serve` in a terminal.")
    sync_dependencies()
    build_dashboard()
    build_mac_app()
    write_plist(serve_args if serve_args is not None else plist_serve_args())
    if launchd_loaded():
        launchd_kick()
    else:
        launchd_boot()
    print(f"Started {LAUNCHD_LABEL} on {version()}. `jarvis status` checks it; `jarvis logs` follows the log.")
    run_dashboard([])


def service_stop() -> None:
    if not launchd_loaded():
        print(f"{LAUNCHD_LABEL} is not running.")
        return
    subprocess.run(["launchctl", "bootout", launchd_domain()], capture_output=True)
    print(f"Stopped {LAUNCHD_LABEL}.")


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
# The dashboard
# --------------------------------------------------------------------------------------------------------------
def build_dashboard() -> None:
    """Build `jarvis-dashboard` (`cargo build --release --workspace`) from this checkout."""
    import shutil

    root = workspace_root()
    if root is None or not (root / "Cargo.toml").is_file():
        return
    fallback = Path("~/.cargo/bin/cargo").expanduser()
    cargo = shutil.which("cargo") or (str(fallback) if fallback.is_file() else None)
    if cargo is None:
        print("`cargo` is not on PATH; skipping the dashboard build.")
        return
    proc = subprocess.run([cargo, "build", "--release", "--workspace"], cwd=str(root))
    if proc.returncode != 0:
        sys.exit("`cargo build --release --workspace` failed; the running code was not restarted.")


def build_mac_app() -> None:
    """Build the Connect app (`apps/connect`) so `/Jarvis.app.zip` serves this checkout's code. Needs `swiftc`."""
    import shutil

    script = REPO_ROOT / "apps" / "connect" / "build.sh"
    if not script.is_file():
        return
    if sys.platform != "darwin" or shutil.which("swiftc") is None:
        print("`swiftc` is not on PATH; skipping the mac client build (the gateway keeps the last bundle it has).")
        return
    proc = subprocess.run(["/bin/sh", str(script)], cwd=str(REPO_ROOT))
    if proc.returncode != 0:
        # A broken client must not stop the daemon from restarting: the app is a download, not the runtime.
        print("The mac client did not build; `/Jarvis.app.zip` keeps serving the last bundle.", file=sys.stderr)


def dashboard_binary() -> Path | None:
    """The built `jarvis-dashboard`: a cargo `target/` in this checkout, or PATH."""
    import shutil

    root = workspace_root()
    if root is not None:
        candidates = [root / "target" / profile / "jarvis-dashboard" for profile in ("release", "debug")]
        candidates += [root / "apps" / "dashboard" / "target" / profile / "jarvis-dashboard" for profile in ("release", "debug")]
        if built := next((path for path in candidates if path.is_file()), None):
            return built
    if found := shutil.which("jarvis-dashboard"):
        return Path(found)
    return None


def dashboard_stale(binary: Path, root: Path | None = None) -> bool:
    """True when `apps/dashboard` sources or workspace Cargo files are newer than the built `jarvis-dashboard`."""
    base = root if root is not None else workspace_root()
    if base is None or not binary.is_file():
        return False
    built_at = binary.stat().st_mtime
    inputs = [
        base / "Cargo.toml",
        base / "Cargo.lock",
        base / "apps" / "dashboard" / "Cargo.toml",
        *(base / "apps" / "dashboard").rglob("*.rs"),
    ]
    return any(p.is_file() and p.stat().st_mtime > built_at for p in inputs)


def run_dashboard(args: list[str]) -> int:
    binary = dashboard_binary()
    if binary is None:
        build_dashboard()
        binary = dashboard_binary()
    if binary is None:
        print("`jarvis dashboard` needs the dashboard built first:", file=sys.stderr)
        print("  cargo build --release --workspace", file=sys.stderr)
        return 1
    if not sys.stdout.isatty():
        # `jarvis start` over ssh: there is no terminal for a fullscreen dashboard, and a nonzero exit here would make a
        # restart that worked look like it failed.
        print("Jarvis is up. Run `jarvis dashboard` in a terminal to watch the board.")
        return 0
    if dashboard_stale(binary):
        build_dashboard()
        binary = dashboard_binary() or binary
    # The dashboard defaults to an installed home; hand it the one this process runs on, token included.
    env = dict(os.environ)
    env["JARVIS_HOME"] = str(JARVIS_HOME)
    env.setdefault("BRAIN_HOME", str(brain_home()))
    # The owner token, the same one this CLI sends: `jarvis dashboard` now reads the telemetry stream, and `.env` wins
    # over brain_token() there, so handing it brain_token() alone would be a 401 for a checkout with its own key.
    env["BRAIN_API_KEY"] = token()
    try:
        return subprocess.call([str(binary), *args], env=env)
    except KeyboardInterrupt:
        return 130


# --------------------------------------------------------------------------------------------------------------
# Talking to the running Jarvis
# --------------------------------------------------------------------------------------------------------------
def pair_device(args: list[str]) -> None:
    """`jarvis pair`: mint a one-time code and print exactly what to paste on the other Mac."""
    name = _take(args, "--name")
    body = call("POST", "/v1/pair/start", json={})
    code = str(body.get("code") or "")
    if not code:
        sys.exit("The running Jarvis did not return a pairing code.")
    minutes = max(1, int(float(body.get("expires_in") or 300) // 60))
    host = str(body.get("server") or "").split("://", 1)[-1] or endpoint().split("://", 1)[-1]
    named = f' --name "{name}"' if name else ""
    print(f"Pairing code: {code[:3]} {code[3:]}   (single use, {minutes} minutes)")
    print("\nOn the other Mac, pick one:")
    print(f"  curl -fsSL 'http://{host}/install-app' | sh      # the Jarvis app: one Connect button")
    print("  ... or, with nothing but uv (https://docs.astral.sh/uv/):")
    print(f"  curl -fsSL 'http://{host}/client.py' -o ~/.local/bin/jarvis-remote.py")
    print(f"  uv run ~/.local/bin/jarvis-remote.py --pair {code}{named}")
    print("\nEither way the code goes into that Mac once; it keeps its own secret after that.")
    print("`jarvis dashboard` shows the code too. `jarvis devices revoke <id>` ends one Mac without touching the others.")


def list_devices(args: list[str]) -> None:
    """`jarvis devices` and `jarvis devices revoke <id>`: who may talk to this Jarvis, and cutting one off."""
    if args and args[0] == "revoke":
        if len(args) < 2:
            sys.exit("usage: jarvis devices revoke <device-id>")
        body = call("POST", f"/v1/devices/{args[1]}/revoke", json={})
        device = body.get("device") or {}
        return print(f"Revoked {device.get('name')} ({device.get('id')}).")
    rows = call("GET", "/v1/devices").get("devices") or []
    if not rows:
        return print("No paired devices. `jarvis pair` adds one.")
    print(f"{'id':<14} {'status':<9} {'paired':<13} {'last seen':<13} name")
    for d in rows:
        print(
            f"{d.get('id', ''):<14} {'revoked' if d.get('revoked') else 'active':<9} "
            f"{_short_day(d.get('created_at')):<13} {_short_day(d.get('last_seen')) or 'never':<13} {d.get('name', '')}"
        )


def _short_day(stamp: Any) -> str:
    """`2026-11-22T16:42:00Z` as `Nov 22 16:42`, local time; empty stays empty."""
    if not isinstance(stamp, str) or not stamp:
        return ""
    from datetime import datetime, timezone

    try:
        moment = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return stamp
    return moment.astimezone().strftime("%b %d %H:%M")


def print_status(url: str = "") -> None:
    s = call("GET", "/v1/status", url=url)
    voice = s.get("voice") or {}
    print(f"Jarvis {s.get('version')}  (pid {s.get('pid')}, up {s.get('uptime_s')}s)")
    print(f"  home      {s.get('home')}")
    print(f"  mode      {s.get('mode')}   spent today ${s.get('spent_today_usd', 0):.2f} / "
          f"${s.get('daily_budget_usd', 0):.2f}")
    print(f"  tasks     {s.get('tasks')} ({s.get('tasks_ready')} ready, {s.get('tasks_running')} running)")
    print(f"  skills    {s.get('skills_active')} active    approvals waiting: {s.get('approvals_waiting')}")
    voice_line = f"  voice     {voice.get('state', 'unknown')}"
    if voice.get("client"):
        voice_line += f" (a paired Mac is the mic and speaker, port {voice.get('port')})"
    elif voice.get("local_audio") is False:
        voice_line += f" (no local mic; waiting for a paired Mac on port {voice.get('port')})"
    print(voice_line)


def dispatch(argv: list[str]) -> None:
    if not argv:
        return run_foreground()
    cmd, rest = argv[0], argv[1:]

    if cmd in ("-h", "--help", "help"):
        return print(USAGE)
    if cmd in ("-V", "--version", "version"):
        return print(version())
    if cmd == "dashboard":
        sys.exit(run_dashboard(rest))
    if cmd == "logs":
        return follow_logs()
    if cmd == "start":
        return service_start(local_or_remote(rest) or None)
    if cmd == "stop":
        return service_stop()
    if cmd == "health" or cmd == "probe":
        url = _take(rest, "--url")
        body = call("GET", "/health", url=url, timeout=5.0)
        return print(f"ok  Jarvis {body.get('version')}  (pid {body.get('pid')}, up {body.get('uptime_s')}s)")
    if cmd == "status":
        return print_status(_take(rest, "--url"))
    if cmd == "pair":
        return pair_device(rest)
    if cmd == "devices":
        return list_devices(rest)
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
        return print(
            f"[{body.get('outcome')}] {body.get('summary')} "
            f"(modified={body.get('modified') or []} memory={body.get('modified_memory') or []})"
        )
    if cmd == "traces":
        return traces_command(rest)
    if cmd == "add":
        return add_task(rest)
    if cmd == "timers":
        return timers_command(rest)
    if cmd == "remind":
        return remind_command(rest)
    if cmd == "schedule":
        return schedule_command(rest)
    if cmd == "init":
        from jarvis.init import main as init

        return init()
    if cmd == "enroll":
        from jarvis_voice.enroll import main as enroll

        with paused_listening():
            return enroll()

    # Foreground flags, in any order: `jarvis --meeting`, `jarvis connect pi`, `jarvis --remote` (an old no-op).
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


def traces_command(args: list[str]) -> None:
    """`jarvis traces` and the Meta-Harness-style drill-down: list, filter, show one, diff two commits."""
    rest = list(args)
    if rest and rest[0] == "diff":
        base = rest[1] if len(rest) > 1 else ""
        head = rest[2] if len(rest) > 2 else ""
        body = call("GET", "/v1/traces/diff", params={"base": base, "head": head})
        stats = body.get("stats") or []
        if not stats:
            return print("No trace history yet.")
        for s in stats:
            print(
                f"{str(s['git_sha'])[:28]:<28} runs={s['runs']:<4} ok={s['ok']:<4} "
                f"friction={s['friction']:<4} failed={s['failed']:<3} "
                f"p50={s['p50_latency_ms']}ms ${s['cost_usd']:.2f}"
            )
        return
    outcome = _take(rest, "--outcome")
    surface = _take(rest, "--surface")
    search = _take(rest, "--search")
    params: dict[str, Any] = {"limit": 30}
    if outcome:
        params["outcome"] = outcome
    if surface:
        params["surface"] = surface
    if search:
        params["q"] = search
    if rest:
        body = call("GET", f"/v1/traces/{rest[0]}")
        trace = body.get("trace") or {}
        print(
            f"{trace.get('created_at')} {trace.get('trace_id')} [{trace.get('outcome')}/"
            f"{trace.get('friction_kind') or 'ok'}] surface={trace.get('surface') or '-'} "
            f"runner={trace.get('runner') or '-'} latency={trace.get('latency_ms')}ms"
        )
        if trace.get("user_input"):
            print(f"  in:  {trace['user_input']}")
        if trace.get("output_summary"):
            print(f"  out: {trace['output_summary']}")
        if trace.get("causal_sig"):
            print(f"  sig: {trace['causal_sig']}")
        record = body.get("record") or {}
        for stage, value in (record.get("voice") or {}).items():
            print(f"  voice.{stage}: {value}")
        return
    body = call("GET", "/v1/traces", params=params)
    rows = body.get("traces") or []
    if not rows:
        return print("No traces.")
    for row in rows:
        print(
            f"{row['created_at']} [{row['outcome']}/{row['friction_kind'] or 'ok'}] "
            f"{row['surface'] or '-':<18} {row['runner'] or '-':<8} "
            f"{row['latency_ms'] or '-'}ms {row['trace_id']}  {(row['user_input'] or '')[:70]}"
        )
    return print(f"\n{len(rows)} trace(s).")


def timers_command(args: list[str]) -> None:
    if args and args[0] == "cancel":
        if len(args) < 2:
            sys.exit("usage: jarvis timers cancel <id>")
        body = call("POST", f"/v1/timers/{args[1]}/cancel")
        timer = body.get("timer") or {}
        return print(f"Cancelled {timer.get('id')} ({timer.get('title')}).")
    body = call("GET", "/v1/timers")
    rows = body.get("timers") or []
    if not rows:
        return print("No timers.")
    for t in rows:
        print(
            f"{t['id']:>11} [{t['kind']}/{t['action']}/{t['status']}] "
            f"next={t['next_run_at']} ({t['timezone']}): {t['title']}"
        )
    return print(f"\n{len(rows)} timer(s).")


def _split_when(args: list[str]) -> tuple[str, str, str]:
    """`text in 30m` or `text at 08:00`, with the flag kept out of the text."""
    for index, arg in enumerate(args):
        if arg in ("in", "at") and index + 1 < len(args):
            text = " ".join(args[:index]).strip()
            when = " ".join(args[index + 1 :]).strip()
            if text and when:
                return arg, text, when
    sys.exit('usage: jarvis remind "what to check" in 30m')


def remind_command(args: list[str]) -> None:
    flag, text, when = _split_when(args)
    body = call(
        "POST",
        "/v1/timers",
        json={"in" if flag == "in" else "at": when, "title": text[:80], "prompt": text, "action": "check"},
    )
    timer = body.get("timer") or {}
    print(f"Scheduled {timer.get('id')} for {timer.get('next_run_at')} ({timer.get('timezone')}): {text}")


def schedule_command(args: list[str]) -> None:
    payload: dict[str, Any] = {"action": "check", "mode": "ASSIST"}
    mapping = {
        "--every": "every",
        "--title": "title",
        "--prompt": "prompt",
        "--repo": "repo",
        "--mode": "mode",
        "--action": "action",
        "--verify": "verify",
        "--timezone": "timezone",
        "--max-cost": "max_cost_usd",
    }
    i = 0
    while i < len(args):
        key = mapping.get(args[i])
        if key is None:
            sys.exit(f"unknown flag {args[i]}\n\n{USAGE}")
        if i + 1 >= len(args):
            sys.exit(f"{args[i]} needs a value")
        payload[key] = args[i + 1]
        i += 2
    if not payload.get("every") or not payload.get("title"):
        sys.exit('usage: jarvis schedule --every "0 8 * * 1-5" --title "Morning" [--prompt ...] [--action task]')
    payload.setdefault("prompt", payload["title"])
    body = call("POST", "/v1/timers", json=payload)
    timer = body.get("timer") or {}
    print(f"Scheduled {timer.get('id')} ({timer.get('kind')}): next={timer.get('next_run_at')}")


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
# `remote` and `--remote` were a mode of their own; the thin client is always served now. The words are still
# accepted, and do nothing, so an installed LaunchAgent or shell history from before keeps working.
DEPRECATED_FLAGS = {"remote", "--remote"}


def parse_foreground(argv: list[str]) -> tuple[bool, str | None]:
    """`(meeting, connect)` from any order of the flags and the `connect` subcommand."""
    meeting = any(a in ("meeting", "--meeting") for a in argv)

    words = [a for a in argv if a not in DEPRECATED_FLAGS and a not in ("meeting", "--meeting")]
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
    return meeting, connect


def run_foreground(argv: list[str] | None = None) -> None:
    meeting, connect = parse_foreground(list(argv or []))
    from jarvis.supervisor import prepare_environment

    prepare_environment()
    if not (JARVIS_HOME / ".env").exists():
        sys.exit(f"No .env in {JARVIS_HOME}: run `jarvis init` first.")
    lock = hold_lock()  # noqa: F841 (held until exit)
    logger.info(f"Jarvis {version()}")

    from jarvis.supervisor import start_all

    try:
        asyncio.run(start_all(connect=connect, meeting=meeting))
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
