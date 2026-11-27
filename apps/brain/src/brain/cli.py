"""The brain's daemon entry points (`running`, `build_launchd_plist`, `run_self_check`, and `serve`)."""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Callable

from aiohttp import web
from loguru import logger

from brain.config import settings
from brain.db import BrainDB
from brain.reconciler import Reconciler
from brain.server import BrainServer
from brain.telemetry import TelemetryHub
from brain.upgrade import LAUNCHD_LABEL
from jarvis_core.devices import DeviceStore
from jarvis_core.home import JARVIS_HOME, rollback_if_last_good


def build_launchd_plist(serve_args: list[str] | None = None) -> str:
    """The LaunchAgent that runs this checkout itself: `.venv`'s Python, `-m jarvis.cli serve`, in the repo."""
    repo_root = settings.self_repo_dir
    log_dir = JARVIS_HOME / "logs"
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    home = Path.home()
    launch_path = ":".join(
        [
            str(home / ".local" / "bin"),
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
    )
    python = repo_root / ".venv" / "bin" / "python"
    program = str(python if python.is_file() else Path(sys.executable).absolute())
    args = "\n".join(f"        <string>{a}</string>" for a in ["-m", "jarvis.cli", "serve", *(serve_args or [])])
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{program}</string>
{args}
    </array>
    <key>WorkingDirectory</key>
    <string>{repo_root}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>{home}</string>
        <key>PATH</key>
        <string>{launch_path}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_dir / "jarvis.out.log"}</string>
    <key>StandardErrorPath</key>
    <string>{log_dir / "jarvis.err.log"}</string>
</dict>
</plist>
"""


def run_self_check() -> int:
    """Smoke canary run inside a candidate worktree before applying a self-upgrade."""
    with tempfile.TemporaryDirectory(prefix="sb-canary-") as tmp:
        tmp_path = Path(tmp)
        cfg = settings.model_copy(
            update={
                "home_dir": tmp_path / "home",
                "projects_dir": tmp_path / "Projects",
                "brain_api_key": "canary-key",
            }
        )
        db = BrainDB(
            db_path=cfg.db_path,
            allowed_repos=cfg.allowed_repo_set,
            default_mode=cfg.default_mode,
            daily_budget_usd=settings.daily_budget_usd,
        )
        try:
            reconciler = Reconciler(db, cfg)
            server = BrainServer(db, reconciler, cfg)
            assert server.app is not None
        finally:
            db.close()
    return 0


def _rollback_if_last_good() -> None:
    """`launchd` keeps respawning: never loop on a bad upgrade, roll back the checkout first."""
    if rolled_back := rollback_if_last_good(settings.db_path, settings.self_repo_dir):
        logger.error(f"Startup failed; rolled back {settings.self_repo_dir} to last_good_sha {rolled_back}")


@asynccontextmanager
async def running(
    *,
    on_restart: Callable[[], None] | None = None,
    extra_routes: list[web.RouteDef] | None = None,
    voice_snapshot: Callable[[], dict] | None = None,
    devices: DeviceStore | None = None,
) -> AsyncIterator[BrainServer]:
    """
    The daemon as a context manager: board, reconciler and the one HTTP/SSE/WS server in this process and loop.
    `jarvis serve` waits on it for launchd; `jarvis` runs the voice harness alongside it.
    """
    if not settings.brain_api_key.strip():
        raise ValueError(
            "BRAIN_API_KEY must be set before running the brain; `jarvis` makes one in the state home."
        )
    db = BrainDB(
        db_path=settings.db_path,
        allowed_repos=settings.allowed_repo_set,
        default_mode=settings.default_mode,
        daily_budget_usd=settings.daily_budget_usd,
    )
    hub = TelemetryHub()
    reconciler = Reconciler(db, settings, on_restart_requested=on_restart, hub=hub)
    server = BrainServer(
        db,
        reconciler,
        settings,
        hub=hub,
        devices=devices,
        voice_snapshot=voice_snapshot,
        extra_routes=extra_routes,
    )
    runner = web.AppRunner(server.app, shutdown_timeout=2.0)
    await runner.setup()
    site = web.TCPSite(runner, host=settings.brain_host, port=settings.brain_port)
    try:
        await site.start()
    except Exception:
        await runner.cleanup()
        db.close()
        raise
    reconciler.start()
    await server.start_background()
    logger.info(
        f"Jarvis server listening on {settings.endpoint_url} "
        f"(mode={db.get_state().mode}, home={settings.home_dir}, allowed_repos={sorted(settings.allowed_repo_set)})"
    )
    try:
        yield server
    finally:
        await server.stop_background()
        await reconciler.stop()
        await runner.cleanup()
        db.close()


async def _serve_async() -> None:
    stop_event = asyncio.Event()
    try:
        async with running(on_restart=stop_event.set):
            await stop_event.wait()
            logger.info("Restart requested; shutting down cleanly for launchd respawn.")
    except Exception:
        _rollback_if_last_good()
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brain")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="Run the HTTP/SSE/WS gateway and the reconciler without the voice")
    sub.add_parser("self-check", help="Run smoke canary verification on this checkout")

    launchd_p = sub.add_parser("launchd", help="Print or install the macOS LaunchAgent for this checkout")
    launchd_p.add_argument("--install", action="store_true")
    launchd_p.add_argument(
        "--remote", action="store_true", help="Deprecated and ignored: a paired Mac's mic and speaker are always served"
    )

    args = parser.parse_args(argv)
    if args.cmd == "serve":
        asyncio.run(_serve_async())
        return 0
    if args.cmd == "self-check":
        return run_self_check()
    if args.cmd == "launchd":
        plist_text = build_launchd_plist([])
        if args.install:
            target = Path(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist").expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(plist_text, encoding="utf-8")
            print(f"Wrote {target}")
        else:
            print(plist_text, end="")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
