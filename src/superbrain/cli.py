from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import tempfile
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


def run_self_check() -> int:
    """Smoke canary run inside a candidate Superbrain worktree before applying a self-upgrade."""
    with tempfile.TemporaryDirectory(prefix="sb-canary-") as tmp:
        tmp_path = Path(tmp)
        cfg = settings.model_copy(
            update={
                "home_dir": tmp_path / "home",
                "projects_dir": tmp_path / "Projects",
                "superbrain_api_key": "canary-key",
            }
        )
        db = BrainDB(
            db_path=cfg.db_path,
            allowed_repos=cfg.allowed_repo_set,
            default_mode=cfg.default_mode,
            daily_budget_usd=cfg.daily_budget_usd,
        )
        try:
            reconciler = Reconciler(db, cfg)
            server = BrainServer(db, reconciler, cfg)
            assert server.app is not None
        finally:
            db.close()
    return 0


def _rollback_to_last_good_sha(last_good_sha: str | None) -> None:
    if not last_good_sha:
        return
    repo_root = Path(__file__).resolve().parents[2]
    if (repo_root / ".git").exists():
        logger.error(f"Startup failed; rolling back {repo_root} to last_good_sha {last_good_sha}")
        dirty = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if dirty:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_root),
                    "stash",
                    "push",
                    "-u",
                    "-m",
                    "superbrain-pre-startup-rollback",
                ],
                check=False,
            )
        subprocess.run(
            ["git", "-C", str(repo_root), "reset", "--hard", last_good_sha],
            check=False,
        )


async def _serve_async() -> None:
    if not settings.superbrain_api_key.strip():
        raise ValueError("SUPERBRAIN_API_KEY must be set before running `superbrain serve`.")
    db = BrainDB(
        db_path=settings.db_path,
        allowed_repos=settings.allowed_repo_set,
        default_mode=settings.default_mode,
        daily_budget_usd=settings.daily_budget_usd,
    )
    stop_event = asyncio.Event()
    reconciler = Reconciler(db, settings, on_restart_requested=stop_event.set)
    try:
        server = BrainServer(db, reconciler, settings)
        reconciler.start()

        runner = web.AppRunner(server.app)
        await runner.setup()
        site = web.TCPSite(runner, host=settings.host, port=settings.port)
        await site.start()
    except Exception:
        last_sha = db.get_state().last_good_sha
        if last_sha:
            _rollback_to_last_good_sha(last_sha)
            db.clear_last_good_sha()
        db.close()
        raise

    logger.info(
        f"Superbrain listening on http://{settings.host}:{settings.port} "
        f"(mode={db.get_state().mode}, allowed_repos={sorted(settings.allowed_repo_set)})"
    )
    try:
        await stop_event.wait()
        logger.info("Restart requested; shutting down cleanly for launchd respawn.")
    finally:
        await reconciler.stop()
        await runner.cleanup()
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="superbrain")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="Run the Superbrain HTTP/SSE server and reconciler loop")
    sub.add_parser("status", help="Show system state, tasks, and active personal skills")
    sub.add_parser("improve", help="Run the self-improving skill reflection loop on recent attempts")
    sub.add_parser("self-check", help="Run smoke canary verification on this checkout")

    res_p = sub.add_parser(
        "research",
        help="Run a SoL-Pi Oracle + BFS auto-research hypothesis fan-out on a task",
    )
    res_p.add_argument("task_id", help="Task ID (e.g. SG-1 or PRB-1)")

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
    if args.cmd == "self-check":
        return run_self_check()

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
            active_skills = db.list_skills(status="active")
            print(
                f"Mode: {st.mode} | Spend today ({st.spend_date}): "
                f"${st.spent_today_usd:.2f} / ${st.daily_budget_usd:.2f} | "
                f"Skills: {len(active_skills)} / {settings.max_personal_skills}"
            )
            for t in db.list_tasks():
                best_info = (
                    f", best={t.best_metric}@{t.best_branch}"
                    if t.best_metric is not None
                    else ""
                )
                print(
                    f"  {t.id} [{t.kind}/{t.status}] (strikes={t.strike_count}, repo={t.repo}{best_info}): {t.title}"
                )
            for s in active_skills:
                print(
                    f"  [skill:{s.slug} v{s.version}] loads={s.loads_since_update} "
                    f"ok={s.successes_since_update} fail={s.failures_since_update}: {s.description}"
                )
            return 0
        if args.cmd == "improve":
            reconciler = Reconciler(db, settings)
            res = asyncio.run(reconciler.run_skill_improver())
            print(f"[{res.outcome}] {res.summary} (modified={res.modified_skills or []})")
            return 0 if res.outcome != "errored" else 1
        if args.cmd == "research":
            reconciler = Reconciler(db, settings)
            res = asyncio.run(reconciler.run_auto_research(args.task_id))
            print(f"[{res.outcome}] {res.summary} (hypotheses={res.upserted_task_ids or []})")
            return 0 if res.outcome != "errored" else 1
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
