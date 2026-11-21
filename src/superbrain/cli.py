from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from aiohttp import web
from loguru import logger

from superbrain.config import settings
from superbrain.db import BrainDB
from superbrain.reconciler import Reconciler
from superbrain.server import BrainServer


def build_launchd_plist() -> str:
    uv_bin = Path(sys.executable).absolute()
    repo_root = Path(__file__).resolve().parents[2]
    log_dir = settings.home_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.sangdth.superbrain</string>
    <key>ProgramArguments</key>
    <array>
        <string>{uv_bin}</string>
        <string>-m</string>
        <string>superbrain.cli</string>
        <string>serve</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{repo_root}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_dir / "superbrain.out.log"}</string>
    <key>StandardErrorPath</key>
    <string>{log_dir / "superbrain.err.log"}</string>
</dict>
</plist>
"""


async def _serve_async() -> None:
    if not settings.superbrain_api_key.strip():
        raise ValueError("SUPERBRAIN_API_KEY must be set before running `superbrain serve`.")
    db = BrainDB(
        db_path=settings.db_path,
        allowed_repos=settings.allowed_repo_set,
        default_mode=settings.default_mode,
        daily_budget_usd=settings.daily_budget_usd,
    )
    reconciler = Reconciler(db, settings)
    server = BrainServer(db, reconciler, settings)
    reconciler.start()

    runner = web.AppRunner(server.app)
    await runner.setup()
    site = web.TCPSite(runner, host=settings.host, port=settings.port)
    await site.start()
    logger.info(
        f"Superbrain listening on http://{settings.host}:{settings.port} "
        f"(mode={db.get_state().mode}, allowed_repos={sorted(settings.allowed_repo_set)})"
    )
    try:
        await asyncio.Event().wait()
    finally:
        await reconciler.stop()
        await runner.cleanup()
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="superbrain")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="Run the Superbrain HTTP/SSE server and reconciler loop")
    sub.add_parser("status", help="Show system state and tasks")

    add_p = sub.add_parser("add", help="Add or update a task on the board")
    add_p.add_argument("--id", dest="task_id", default=None)
    add_p.add_argument("--kind", choices=["goal", "problem"], required=True)
    add_p.add_argument("--repo", required=True)
    add_p.add_argument("--title", required=True)
    add_p.add_argument("--spec", required=True)
    add_p.add_argument("--metric", default=None)
    add_p.add_argument("--verify", default=None)
    add_p.add_argument("--priority", type=int, default=50)
    add_p.add_argument("--ready", action="store_true")

    launchd_p = sub.add_parser("launchd", help="Print or install the macOS LaunchAgent plist")
    launchd_p.add_argument("--install", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "serve":
        asyncio.run(_serve_async())
        return 0

    if args.cmd == "launchd":
        plist_text = build_launchd_plist()
        if args.install:
            target = Path("~/Library/LaunchAgents/com.sangdth.superbrain.plist").expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(plist_text, encoding="utf-8")
            print(f"Wrote {target}")
        else:
            print(plist_text, end="")
        return 0

    db = BrainDB(
        db_path=settings.db_path,
        allowed_repos=settings.allowed_repo_set,
        default_mode=settings.default_mode,
        daily_budget_usd=settings.daily_budget_usd,
    )
    try:
        if args.cmd == "status":
            st = db.get_state()
            print(
                f"Mode: {st.mode} | Spend today ({st.spend_date}): "
                f"${st.spent_today_usd:.2f} / ${st.daily_budget_usd:.2f}"
            )
            for t in db.list_tasks():
                print(
                    f"  {t.id} [{t.kind}/{t.status}] (strikes={t.strike_count}, repo={t.repo}): {t.title}"
                )
            return 0
        if args.cmd == "add":
            t = db.upsert_task(
                task_id=args.task_id,
                kind=args.kind,
                repo=args.repo,
                title=args.title,
                spec=args.spec,
                metric_target=args.metric,
                verify_cmd=args.verify,
                priority=args.priority,
                ready=True if args.ready else None,
            )
            print(f"Upserted {t.id} ({t.status}) in {t.repo}: {t.title}")
            return 0
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
