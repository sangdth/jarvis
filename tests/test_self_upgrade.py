from __future__ import annotations

import asyncio
import stat
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from superbrain.cli import run_self_check
from superbrain.config import Settings
from superbrain.db import BrainDB
from superbrain.reconciler import Reconciler
from superbrain.server import BrainServer


async def _init_superbrain_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    bare_origin = tmp_path / "remotes" / "superbrain.git"
    bare_origin.parent.mkdir(parents=True, exist_ok=True)
    p = await asyncio.create_subprocess_exec(
        "git", "init", "--bare", "-b", "master", str(bare_origin)
    )
    await p.wait()

    projects_dir = tmp_path / "Projects"
    repo_dir = projects_dir / "superbrain"
    (repo_dir / "src" / "superbrain").mkdir(parents=True, exist_ok=True)
    for cmd in [
        ["git", "init", "-b", "master"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "remote", "add", "origin", str(bare_origin)],
    ]:
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(repo_dir))
        await proc.wait()

    (repo_dir / "brain_mod.py").write_text("FAST_ENGINE = False\n", encoding="utf-8")
    (repo_dir / "src" / "superbrain" / "sandbox.py").write_text("# original\n", encoding="utf-8")
    for cmd in [
        ["git", "add", "."],
        ["git", "commit", "-m", "initial brain"],
        ["git", "push", "-u", "origin", "master"],
    ]:
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(repo_dir))
        await proc.wait()

    proc = await asyncio.create_subprocess_exec(
        "git", "rev-parse", "HEAD", cwd=str(repo_dir), stdout=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    initial_sha = out.decode().strip()
    return projects_dir, repo_dir, initial_sha


def test_cli_self_check_canary_passes() -> None:
    assert run_self_check() == 0


async def test_voice_self_upgrade_builds_verifies_updates_repo_and_restarts(
    tmp_path: Path,
) -> None:
    projects_dir, repo_dir, initial_sha = await _init_superbrain_repo(tmp_path)
    fake_pi = tmp_path / "fake_pi_upgrade.py"
    fake_pi.write_text(
        """#!/usr/bin/env python3
import json, os, pathlib, socket

sock_path = os.environ["SUPERBRAIN_RPC_SOCK"]
worktree = os.environ.get("SUPERBRAIN_WORKTREE_DIR")

if not worktree:
    # Interactive Planner turn: queue ready self-upgrade task for repo='superbrain'
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    req = {
        "method": "upsert_task",
        "params": {
            "kind": "goal",
            "repo": "superbrain",
            "title": "Upgrade brain engine",
            "spec": "Enable FAST_ENGINE in brain_mod.py",
            "metric_target": "FAST_ENGINE is True",
            "verify_cmd": "python3 -c 'import brain_mod; assert brain_mod.FAST_ENGINE is True'",
            "ready": True,
        },
    }
    s.sendall((json.dumps(req) + "\\n").encode())
    resp = json.loads(s.recv(4096).decode().splitlines()[0])
    s.close()
    print(json.dumps({
        "role": "assistant",
        "content": f"Queued self-upgrade {resp['result']['id']}.",
        "cost_usd": 0.02,
    }), flush=True)
else:
    # Builder turn inside worktree
    wt = pathlib.Path(worktree)
    (wt / "brain_mod.py").write_text("FAST_ENGINE = True\\n", encoding="utf-8")
    print(json.dumps({
        "role": "assistant",
        "content": "Enabled FAST_ENGINE in brain_mod.py.",
        "cost_usd": 0.05,
    }), flush=True)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(fake_pi.stat().st_mode | stat.S_IEXEC)

    restarted = asyncio.Event()
    cfg = Settings(
        home_dir=tmp_path / "home",
        projects_dir=projects_dir,
        allowed_repos="jarvis",  # superbrain is automatically included in allowed_repo_set
        superbrain_api_key="secret",
        pi_bin=str(fake_pi),
        self_upgrade_verify_cmd="python3 -c 'import brain_mod; assert brain_mod.FAST_ENGINE'",
        sandbox_enabled=True,
    )
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    reconciler = Reconciler(db, cfg, on_restart_requested=restarted.set)
    server = BrainServer(db, reconciler, cfg)

    async with TestClient(TestServer(server.app)) as client:
        resp = await client.post(
            "/v1/ask",
            json={"input": "Upgrade your code of the brain by enabling FAST_ENGINE"},
            headers={"Authorization": "Bearer secret"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert "passed verification and was applied" in data["spoken"]
        assert "Restarting Superbrain daemon now." in data["spoken"]

        # Main ~/Projects/superbrain checkout was updated to the verified commit
        assert (repo_dir / "brain_mod.py").read_text(encoding="utf-8") == "FAST_ENGINE = True\n"
        # last_good_sha was recorded for automatic rollback protection
        assert db.get_state().last_good_sha == initial_sha

        # Delayed restart fires after HTTP response is flushed
        await asyncio.wait_for(restarted.wait(), timeout=2.0)

    await reconciler.stop()
    db.close()


async def test_failed_canary_blocks_self_upgrade_and_preserves_checkout(tmp_path: Path) -> None:
    projects_dir, repo_dir, initial_sha = await _init_superbrain_repo(tmp_path)
    fake_pi = tmp_path / "fake_pi_bad_upgrade.py"
    fake_pi.write_text(
        """#!/usr/bin/env python3
import json, pathlib
wt = pathlib.Path.cwd()
(wt / "brain_mod.py").write_text("FAST_ENGINE = 'broken'\\n", encoding="utf-8")
print(json.dumps({"role": "assistant", "content": "Edited brain_mod", "cost_usd": 0.03}), flush=True)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(fake_pi.stat().st_mode | stat.S_IEXEC)

    restarted = False

    def _on_restart() -> None:
        nonlocal restarted
        restarted = True

    cfg = Settings(
        home_dir=tmp_path / "home",
        projects_dir=projects_dir,
        pi_bin=str(fake_pi),
        default_mode="AUTONOMOUS",
        self_upgrade_verify_cmd="false",  # Simulate canary failure
        sandbox_enabled=True,
    )
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set, default_mode="AUTONOMOUS")
    reconciler = Reconciler(db, cfg, on_restart_requested=_on_restart)
    try:
        t = db.upsert_task(
            kind="problem",
            repo="superbrain",
            title="Bad self-fix",
            spec="Fix bug",
            verify_cmd="true",
            ready=True,
        )
        finished = await reconciler.tick()
        assert finished is not None and finished.id == t.id
        assert finished.status == "ready"
        assert finished.strike_count == 1
        assert restarted is False
        # Checkout was never touched
        assert (repo_dir / "brain_mod.py").read_text(encoding="utf-8") == "FAST_ENGINE = False\n"
        assert db.get_state().last_good_sha is None
        _ = initial_sha
    finally:
        await reconciler.stop()
        db.close()


async def test_self_upgrade_touching_protected_guardrail_requires_approval(
    tmp_path: Path,
) -> None:
    projects_dir, repo_dir, _ = await _init_superbrain_repo(tmp_path)
    fake_pi = tmp_path / "fake_pi_touch_sandbox.py"
    fake_pi.write_text(
        """#!/usr/bin/env python3
import json, pathlib
wt = pathlib.Path.cwd()
(wt / "src" / "superbrain" / "sandbox.py").write_text("# modified guardrail\\n", encoding="utf-8")
print(json.dumps({"role": "assistant", "content": "Updated sandbox.py", "cost_usd": 0.03}), flush=True)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(fake_pi.stat().st_mode | stat.S_IEXEC)

    restarted = False

    def _on_restart() -> None:
        nonlocal restarted
        restarted = True

    cfg = Settings(
        home_dir=tmp_path / "home",
        projects_dir=projects_dir,
        pi_bin=str(fake_pi),
        default_mode="AUTONOMOUS",
        self_upgrade_verify_cmd="true",
        sandbox_enabled=True,
    )
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set, default_mode="AUTONOMOUS")
    reconciler = Reconciler(db, cfg, on_restart_requested=_on_restart)
    try:
        db.upsert_task(
            kind="goal",
            repo="superbrain",
            title="Modify sandbox",
            spec="Change sandbox profile",
            metric_target="passes",
            verify_cmd="true",
            ready=True,
        )
        # First run: deferred for approval because sandbox.py is a protected path
        finished = await reconciler.tick()
        assert finished is not None
        assert finished.status == "draft"
        assert restarted is False
        assert (repo_dir / "src" / "superbrain" / "sandbox.py").read_text(
            encoding="utf-8"
        ) == "# original\n"

        _, pending_approval = db.drain_digest()
        assert pending_approval is not None
        assert "protected files" in pending_approval.summary_spoken

        # Owner approves -> task moves back to ready -> next tick applies upgrade & restarts
        db.resolve_approval(pending_approval.id, approved=True)
        finished2 = await reconciler.tick()
        assert finished2 is not None and finished2.status == "done"
        assert restarted is True
        assert (repo_dir / "src" / "superbrain" / "sandbox.py").read_text(
            encoding="utf-8"
        ) == "# modified guardrail\n"
    finally:
        await reconciler.stop()
        db.close()


async def test_held_out_baseline_tests_block_reward_hacking_goal_upgrade(
    tmp_path: Path,
) -> None:
    projects_dir, repo_dir, _ = await _init_superbrain_repo(tmp_path)
    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "test_invariant.py").write_text(
        "import brain_mod\ndef test_flag_is_bool():\n    assert isinstance(brain_mod.FAST_ENGINE, bool)\n",
        encoding="utf-8",
    )
    for cmd in [["git", "add", "."], ["git", "commit", "-m", "add baseline test"], ["git", "push"]]:
        p = await asyncio.create_subprocess_exec(*cmd, cwd=str(repo_dir))
        await p.wait()

    # Builder tries to reward-hack a `goal` upgrade by breaking FAST_ENGINE type AND
    # gutting tests/test_invariant.py inside its own worktree so worktree pytest passes.
    fake_pi = tmp_path / "fake_pi_reward_hack.py"
    fake_pi.write_text(
        """#!/usr/bin/env python3
import json, pathlib
wt = pathlib.Path.cwd()
(wt / "brain_mod.py").write_text("FAST_ENGINE = 'not-a-bool'\\n", encoding="utf-8")
(wt / "tests" / "test_invariant.py").write_text("def test_flag_is_bool(): assert True\\n", encoding="utf-8")
print(json.dumps({"role": "assistant", "content": "Hacked tests", "cost_usd": 0.02}), flush=True)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(fake_pi.stat().st_mode | stat.S_IEXEC)

    cfg = Settings(
        home_dir=tmp_path / "home",
        projects_dir=projects_dir,
        pi_bin=str(fake_pi),
        default_mode="AUTONOMOUS",
        self_upgrade_verify_cmd="pytest -q",
        sandbox_enabled=True,
    )
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set, default_mode="AUTONOMOUS")
    reconciler = Reconciler(db, cfg)
    try:
        task = db.upsert_task(
            kind="goal",
            repo="superbrain",
            title="Upgrade engine",
            spec="Improve engine",
            metric_target="passes",
            verify_cmd="pytest -q",
            ready=True,
        )
        finished = await reconciler.tick()
        assert finished is not None
        assert finished.status == "ready"
        assert finished.strike_count == 1
        attempts = db.list_attempts(task.id)
        assert attempts[0].outcome == "failed_verify"
        assert "Held-out baseline tests failed" in (attempts[0].verify_output or "")
        assert "sealed" in (attempts[0].verify_output or "")
        assert "test_flag_is_bool" not in (attempts[0].verify_output or "")
        assert (repo_dir / "brain_mod.py").read_text(encoding="utf-8") == "FAST_ENGINE = False\n"
    finally:
        await reconciler.stop()
        db.close()


async def test_post_upgrade_runtime_circuit_breaker_rolls_back_after_two_failures(
    tmp_path: Path,
) -> None:
    projects_dir, repo_dir, initial_sha = await _init_superbrain_repo(tmp_path)
    demo_dir = projects_dir / "demo"
    demo_dir.mkdir(parents=True, exist_ok=True)
    for cmd in [
        ["git", "init", "-b", "master"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
    ]:
        p = await asyncio.create_subprocess_exec(*cmd, cwd=str(demo_dir))
        await p.wait()
    (demo_dir / "x.py").write_text("X = 1\n", encoding="utf-8")
    for cmd in [["git", "add", "."], ["git", "commit", "-m", "init demo"]]:
        p = await asyncio.create_subprocess_exec(*cmd, cwd=str(demo_dir))
        await p.wait()

    fake_pi = tmp_path / "fake_pi_upgrade_then_regress.py"
    fake_pi.write_text(
        """#!/usr/bin/env python3
import json, pathlib
wt = pathlib.Path.cwd()
if (wt / "brain_mod.py").exists():
    (wt / "brain_mod.py").write_text("FAST_ENGINE = True\\n", encoding="utf-8")
print(json.dumps({"role": "assistant", "content": "Done", "cost_usd": 0.01}), flush=True)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(fake_pi.stat().st_mode | stat.S_IEXEC)

    restart_count = 0

    def _on_restart() -> None:
        nonlocal restart_count
        restart_count += 1

    cfg = Settings(
        home_dir=tmp_path / "home",
        projects_dir=projects_dir,
        allowed_repos="demo",
        pi_bin=str(fake_pi),
        default_mode="AUTONOMOUS",
        self_upgrade_verify_cmd="true",
        sandbox_enabled=True,
    )
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set, default_mode="AUTONOMOUS")
    reconciler = Reconciler(db, cfg, on_restart_requested=_on_restart)
    try:
        # 1. Apply valid self-upgrade
        db.upsert_task(
            kind="goal",
            repo="superbrain",
            title="Enable FAST_ENGINE",
            spec="Turn on FAST_ENGINE",
            metric_target="FAST_ENGINE True",
            verify_cmd="true",
            ready=True,
        )
        await reconciler.tick()
        assert (repo_dir / "brain_mod.py").read_text(encoding="utf-8") == "FAST_ENGINE = True\n"
        assert db.get_state().last_good_sha == initial_sha
        assert restart_count == 1
        reconciler.restart_pending = False

        # 2. Next 2 downstream task attempts fail -> automatic git rollback to initial_sha + restart
        db.upsert_task(
            kind="problem",
            repo="demo",
            title="Failing task",
            spec="Fails verify",
            verify_cmd="false",
            ready=True,
        )
        await reconciler.tick()
        assert (repo_dir / "brain_mod.py").read_text(encoding="utf-8") == "FAST_ENGINE = True\n"
        assert restart_count == 1

        await reconciler.tick()
        assert (repo_dir / "brain_mod.py").read_text(encoding="utf-8") == "FAST_ENGINE = False\n"
        assert db.get_state().last_good_sha is None
        assert restart_count == 2
        reports, _ = db.drain_digest()
        assert any("Automatically rolled back Superbrain" in r.summary_spoken for r in reports)
    finally:
        await reconciler.stop()
        db.close()
