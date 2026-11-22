from __future__ import annotations

import asyncio
import stat
from pathlib import Path

from brain.config import Settings
from brain.db import BrainDB
from brain.reconciler import Reconciler


async def _init_repo_with_origin(tmp_path: Path, name: str = "demo") -> tuple[Path, Path]:
    bare_origin = tmp_path / "remotes" / f"{name}.git"
    bare_origin.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec("git", "init", "--bare", "-b", "master", str(bare_origin))
    await proc.wait()

    projects_dir = tmp_path / "Projects"
    repo_dir = projects_dir / name
    repo_dir.mkdir(parents=True, exist_ok=True)
    for cmd in [
        ["git", "init", "-b", "master"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "remote", "add", "origin", str(bare_origin)],
    ]:
        p = await asyncio.create_subprocess_exec(*cmd, cwd=str(repo_dir))
        await p.wait()

    (repo_dir / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo_dir / "tests").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "test_app.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    for cmd in [
        ["git", "add", "app.py", "tests/test_app.py"],
        ["git", "commit", "-m", "initial"],
        ["git", "push", "-u", "origin", "master"],
    ]:
        p = await asyncio.create_subprocess_exec(*cmd, cwd=str(repo_dir))
        await p.wait()

    return projects_dir, bare_origin


def _write_fake_pi(script_path: Path, body: str) -> Path:
    script_path.write_text(f"#!/usr/bin/env python3\n{body}\n", encoding="utf-8")
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)
    return script_path


async def test_builder_worktree_verify_and_push_to_origin(tmp_path: Path) -> None:
    projects_dir, bare_origin = await _init_repo_with_origin(tmp_path, "demo")
    fake_pi = _write_fake_pi(
        tmp_path / "fake_pi.py",
        """
import json, pathlib, subprocess
wt = pathlib.Path.cwd()
(wt / "app.py").write_text("VALUE = 42\\n", encoding="utf-8")
subprocess.run(["git", "status"], cwd=str(wt), check=True, stdout=subprocess.DEVNULL)
print(json.dumps({"type": "session", "id": "sess-1"}), flush=True)
ev = {"type": "message_end", "message": {"role": "assistant", "content": "Set VALUE to 42."}, "cost_usd": 0.45}
print(json.dumps(ev), flush=True)
""",
    )
    home_dir = tmp_path / "home"
    cfg = Settings(
        home_dir=home_dir,
        projects_dir=projects_dir,
        allowed_repos="demo",
        pi_bin=str(fake_pi),
        default_mode="AUTONOMOUS",
        sandbox_enabled=True,
    )
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set, default_mode="AUTONOMOUS")
    reconciler = Reconciler(db, cfg)
    try:
        task = db.upsert_task(
            kind="problem",
            repo="demo",
            title="Fix VALUE",
            spec="VALUE must be 42",
            verify_cmd="python3 -c 'import app; assert app.VALUE == 42'",
            ready=True,
        )
        finished = await reconciler.tick()
        assert finished is not None
        assert finished.id == task.id
        assert finished.status == "done"

        # Worktree was cleaned up and main checkout was never dirtied
        assert not (cfg.worktrees_dir / task.id).exists()
        assert (projects_dir / "demo" / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"

        # Branch brain/PRB-1 was pushed to origin
        proc = await asyncio.create_subprocess_exec(
            "git",
            "--git-dir",
            str(bare_origin),
            "rev-parse",
            "--verify",
            f"refs/heads/brain/{task.id}",
            stdout=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        assert proc.returncode == 0
        assert len(out.decode().strip()) == 40
    finally:
        await reconciler.stop()
        db.close()


async def test_budget_kill_switch_stops_run_mid_stream(tmp_path: Path) -> None:
    projects_dir, _ = await _init_repo_with_origin(tmp_path, "demo")
    fake_pi = _write_fake_pi(
        tmp_path / "fake_pi_expensive.py",
        """
import json, time
ev = {"session_id": "sess-exp", "cost_usd": 25.0, "role": "assistant", "content": "Expensive step"}
print(json.dumps(ev), flush=True)
time.sleep(30)
""",
    )
    cfg = Settings(
        home_dir=tmp_path / "home",
        projects_dir=projects_dir,
        allowed_repos="demo",
        pi_bin=str(fake_pi),
        daily_budget_usd=5.0,
        default_mode="AUTONOMOUS",
        sandbox_enabled=False,
    )
    db = BrainDB(
        cfg.db_path, cfg.allowed_repo_set, default_mode="AUTONOMOUS", daily_budget_usd=5.0
    )
    reconciler = Reconciler(db, cfg)
    try:
        db.upsert_task(
            kind="problem",
            repo="demo",
            title="Expensive task",
            spec="Should be killed when cost > 5.0",
            verify_cmd="true",
            ready=True,
        )
        finished = await reconciler.tick()
        assert finished is not None
        assert finished.strike_count == 1
        attempts = db.list_attempts(finished.id)
        assert len(attempts) == 1
        assert attempts[0].outcome == "errored"
        assert "daily budget" in attempts[0].summary
    finally:
        await reconciler.stop()
        db.close()


async def test_macos_sandbox_blocks_writes_to_sibling_hamahakki(tmp_path: Path) -> None:
    projects_dir, _ = await _init_repo_with_origin(tmp_path, "demo")
    hamahakki_dir = projects_dir / "hamahakki"
    hamahakki_dir.mkdir(parents=True, exist_ok=True)
    secret_file = hamahakki_dir / "untouched.txt"
    secret_file.write_text("safe", encoding="utf-8")

    fake_pi = _write_fake_pi(
        tmp_path / "fake_pi_escape.py",
        f"""
import pathlib, sys
target = pathlib.Path({str(secret_file)!r})
try:
    target.write_text("hacked", encoding="utf-8")
    sys.exit(0)
except OSError as exc:
    print(f"Blocked by sandbox: {{exc}}", file=sys.stderr)
    sys.exit(2)
""",
    )
    cfg = Settings(
        home_dir=tmp_path / "home",
        projects_dir=projects_dir,
        allowed_repos="demo",
        pi_bin=str(fake_pi),
        default_mode="AUTONOMOUS",
        sandbox_enabled=True,
    )
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set, default_mode="AUTONOMOUS")
    reconciler = Reconciler(db, cfg)
    try:
        db.upsert_task(
            kind="problem",
            repo="demo",
            title="Escape attempt",
            spec="Try writing to sibling repo",
            verify_cmd="true",
            ready=True,
        )
        finished = await reconciler.tick()
        assert finished is not None
        assert secret_file.read_text(encoding="utf-8") == "safe"
        attempts = db.list_attempts(finished.id)
        assert attempts[0].outcome == "errored"
    finally:
        await reconciler.stop()
        db.close()


async def test_builder_blocked_from_upsert_rpc_and_deleting_tests(tmp_path: Path) -> None:
    projects_dir, _ = await _init_repo_with_origin(tmp_path, "demo")
    fake_pi = _write_fake_pi(
        tmp_path / "fake_pi_bad_builder.py",
        """
import json, os, pathlib, socket
sock_path = os.environ["BRAIN_RPC_SOCK"]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sock_path)
s.sendall((json.dumps({
    "method": "upsert_task",
    "params": {"kind": "problem", "repo": "demo", "title": "Hack", "spec": "x", "verify_cmd": "true"}
}) + "\\n").encode())
resp = json.loads(s.recv(4096).decode().splitlines()[0])
s.close()
assert resp["ok"] is False and "planner" in resp["error"]
(pathlib.Path.cwd() / "tests" / "test_app.py").unlink()
print(json.dumps({"role": "assistant", "content": "Deleted test", "cost_usd": 0.05}), flush=True)
""",
    )
    cfg = Settings(
        home_dir=tmp_path / "home",
        projects_dir=projects_dir,
        allowed_repos="demo",
        pi_bin=str(fake_pi),
        default_mode="AUTONOMOUS",
        sandbox_enabled=True,
    )
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set, default_mode="AUTONOMOUS")
    reconciler = Reconciler(db, cfg)
    try:
        task = db.upsert_task(
            kind="problem",
            repo="demo",
            title="Keep tests intact",
            spec="Fix bug without deleting tests",
            verify_cmd="true",
            ready=True,
        )
        finished = await reconciler.tick()
        assert finished is not None
        assert finished.id == task.id
        assert finished.strike_count == 1
        attempts = db.list_attempts(task.id)
        assert attempts[0].outcome == "failed_verify"
        assert "deleted test files" in (attempts[0].verify_output or "")
    finally:
        await reconciler.stop()
        db.close()
