from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest

from brain.config import Settings
from brain.db import BrainDB
from brain.reconciler import Reconciler
from brain.upgrade import (
    GitState,
    classify_git_state,
)


# --------------------------------------------------------------------------------------
# Pure git-state classifier
# --------------------------------------------------------------------------------------
def test_classify_git_state_covers_every_branch() -> None:
    base = "a" * 40
    local = "b" * 40
    origin = "c" * 40

    assert classify_git_state(base, base, base) is GitState.UP_TO_DATE
    # origin/master ahead: local is the merge base
    assert classify_git_state(base, origin, base) is GitState.FAST_FORWARD
    # local ahead: origin/master is the merge base
    assert classify_git_state(local, base, base) is GitState.LOCAL_AHEAD
    # both sides have unique commits
    assert classify_git_state(local, origin, base) is GitState.DIVERGED
    # unrelated histories
    assert classify_git_state(local, origin, None) is GitState.DIVERGED
    # no remote-tracking branch to follow
    assert classify_git_state(local, None, None) is GitState.UP_TO_DATE
    # unborn local branch with a known remote
    assert classify_git_state(None, origin, None) is GitState.FAST_FORWARD


# --------------------------------------------------------------------------------------
# Scratch repositories
# --------------------------------------------------------------------------------------
def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _init_scratch_repo(tmp_path: Path) -> tuple[Path, Path]:
    bare = tmp_path / "remotes" / "brain.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--bare", "-b", "master", str(bare)],
        check=True,
        capture_output=True,
    )
    repo = tmp_path / "checkout"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    for cmd in [
        ["init", "-b", "master"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
        ["remote", "add", "origin", str(bare)],
    ]:
        _git(repo, *cmd)
    (repo / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname = 'scratch'\n", encoding="utf-8")
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "push", "-u", "origin", "master")
    return repo, bare


def _peer_commit(tmp_path: Path, bare: Path, files: dict[str, str], message: str = "peer") -> str:
    peer = tmp_path / "peer"
    if not peer.exists():
        subprocess.run(
            ["git", "clone", "-b", "master", str(bare), str(peer)],
            check=True,
            capture_output=True,
        )
        _git(peer, "config", "user.email", "test@example.com")
        _git(peer, "config", "user.name", "Test")
    for rel, content in files.items():
        target = peer / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(peer, "add", ".")
    _git(peer, "commit", "-m", message)
    _git(peer, "push", "origin", "master")
    return _git(peer, "rev-parse", "HEAD")


def _db(tmp_path: Path) -> BrainDB:
    return BrainDB(tmp_path / "state.db", allowed_repos={"brain"})


# --------------------------------------------------------------------------------------
# Self-upgrade reconciliation
# --------------------------------------------------------------------------------------
def _init_brain_repo(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    bare = tmp_path / "remotes" / "brain.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--bare", "-b", "master", str(bare)],
        check=True,
        capture_output=True,
    )
    projects_dir = tmp_path / "Projects"
    repo = projects_dir / "brain"
    (repo / "src" / "brain").mkdir(parents=True, exist_ok=True)
    for cmd in [
        ["init", "-b", "master"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
        ["remote", "add", "origin", str(bare)],
    ]:
        _git(repo, *cmd)
    (repo / "brain_mod.py").write_text("FAST_ENGINE = False\n", encoding="utf-8")
    (repo / "src" / "brain" / "sandbox.py").write_text("# original\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial brain")
    _git(repo, "push", "-u", "origin", "master")
    return projects_dir, repo, bare, _git(repo, "rev-parse", "HEAD")


def _write_fake_pi(path: Path, body: str) -> Path:
    path.write_text(f"#!/usr/bin/env python3\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _settings(tmp_path: Path, projects_dir: Path, fake_pi: Path) -> Settings:
    return Settings(
        _env_file=None,
        home_dir=tmp_path / "home",
        projects_dir=projects_dir,
        self_repo_dir=projects_dir / "brain",
        allowed_repos="jarvis",
        pi_bin=str(fake_pi),
        default_mode="AUTONOMOUS",
        self_upgrade_verify_cmd="python3 -c 'import brain_mod; assert brain_mod.FAST_ENGINE is True'",
        sandbox_enabled=False,
    )


async def test_self_upgrade_worktree_follows_origin_master(tmp_path: Path) -> None:
    projects_dir, repo, bare, initial_sha = _init_brain_repo(tmp_path)
    merged_sha = _peer_commit(
        tmp_path, bare, {"origin_only.txt": "merged from master\n"}, message="merged PR"
    )
    fake_pi = _write_fake_pi(
        tmp_path / "fake_pi_follow_master.py",
        """
import json, pathlib
wt = pathlib.Path.cwd()
assert (wt / "origin_only.txt").exists(), "worktree must be based on origin/master"
(wt / "brain_mod.py").write_text("FAST_ENGINE = True\\n", encoding="utf-8")
print(json.dumps({"role": "assistant", "content": "Enabled FAST_ENGINE", "cost_usd": 0.01}), flush=True)
""",
    )
    cfg = _settings(tmp_path, projects_dir, fake_pi)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set, default_mode="AUTONOMOUS")
    reconciler = Reconciler(db, cfg)
    try:
        task = db.upsert_task(
            kind="goal",
            repo="brain",
            title="Upgrade on top of merged master",
            spec="Enable FAST_ENGINE",
            metric_target="FAST_ENGINE True",
            verify_cmd="python3 -c 'import brain_mod; assert brain_mod.FAST_ENGINE is True'",
            ready=True,
        )
        finished = await reconciler.tick()
        assert finished is not None and finished.id == task.id
        assert finished.status == "done", db.list_attempts(task.id)
        # The checkout absorbed the merged commit instead of staying behind master.
        assert (repo / "origin_only.txt").exists()
        assert _git(repo, "rev-parse", "HEAD") == _git(bare, "rev-parse", "master")
        assert _git(bare, "rev-parse", "master") != merged_sha
        assert db.get_state().last_good_sha == initial_sha
    finally:
        await reconciler.stop()
        db.close()


async def test_self_upgrade_keeps_unpublished_local_commits(tmp_path: Path) -> None:
    projects_dir, repo, bare, _ = _init_brain_repo(tmp_path)
    (repo / "local_only.txt").write_text("unpublished\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "local unpublished")
    local_sha = _git(repo, "rev-parse", "HEAD")

    fake_pi = _write_fake_pi(
        tmp_path / "fake_pi_keep_local.py",
        """
import json, pathlib
wt = pathlib.Path.cwd()
assert (wt / "local_only.txt").exists(), "worktree must keep unpublished local commits"
(wt / "brain_mod.py").write_text("FAST_ENGINE = True\\n", encoding="utf-8")
print(json.dumps({"role": "assistant", "content": "Enabled FAST_ENGINE", "cost_usd": 0.01}), flush=True)
""",
    )
    cfg = _settings(tmp_path, projects_dir, fake_pi)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set, default_mode="AUTONOMOUS")
    reconciler = Reconciler(db, cfg)
    try:
        task = db.upsert_task(
            kind="goal",
            repo="brain",
            title="Upgrade on top of local commits",
            spec="Enable FAST_ENGINE",
            metric_target="FAST_ENGINE True",
            verify_cmd="python3 -c 'import brain_mod; assert brain_mod.FAST_ENGINE is True'",
            ready=True,
        )
        finished = await reconciler.tick()
        assert finished is not None and finished.status == "done", db.list_attempts(task.id)
        assert (repo / "local_only.txt").exists()
        # Local HEAD was the worktree base and the push fast-forwarded master to it.
        assert _git(bare, "rev-parse", "master") != local_sha
        assert _git(repo, "rev-parse", "HEAD") == _git(bare, "rev-parse", "master")
    finally:
        await reconciler.stop()
        db.close()


async def test_self_upgrade_refuses_divergence_and_queues_blocker(tmp_path: Path) -> None:
    projects_dir, repo, bare, _ = _init_brain_repo(tmp_path)
    (repo / "local_only.txt").write_text("unpublished\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "local unpublished")
    local_sha = _git(repo, "rev-parse", "HEAD")
    origin_sha = _peer_commit(tmp_path, bare, {"peer.py": "PEER = True\n"}, message="peer commit")
    marker = tmp_path / "pi-invoked.txt"
    fake_pi = _write_fake_pi(
        tmp_path / "fake_pi_diverged.py",
        f"""
import json, pathlib
pathlib.Path({str(marker)!r}).write_text("ran", encoding="utf-8")
print(json.dumps({{"role": "assistant", "content": "should not run", "cost_usd": 0.01}}), flush=True)
""",
    )
    cfg = _settings(tmp_path, projects_dir, fake_pi)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set, default_mode="AUTONOMOUS")
    reconciler = Reconciler(db, cfg)
    try:
        task = db.upsert_task(
            kind="goal",
            repo="brain",
            title="Must refuse divergence",
            spec="Enable FAST_ENGINE",
            metric_target="FAST_ENGINE True",
            verify_cmd="true",
            ready=True,
        )
        finished = await reconciler.tick()
        assert finished is not None and finished.status == "ready"
        assert finished.strike_count == 1
        attempts = db.list_attempts(task.id)
        assert attempts[-1].outcome == "errored"
        assert "diverged" in attempts[-1].summary
        assert not marker.exists()  # builder never ran
        # Checkout was not rewound to origin/master.
        assert _git(repo, "rev-parse", "HEAD") == local_sha
        assert (repo / "local_only.txt").exists()
        assert db.get_state().last_good_sha is None
        reports, _ = db.drain_digest()
        assert any(r.kind == "blocker" and "diverged" in r.summary_spoken for r in reports)
        assert _git(bare, "rev-parse", "master") == origin_sha
    finally:
        await reconciler.stop()
        db.close()


async def test_self_upgrade_push_rejection_queues_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_dir, repo, _, _ = _init_brain_repo(tmp_path)
    (repo / "brain_mod.py").write_text("FAST_ENGINE = True\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "candidate upgrade")
    verified_sha = _git(repo, "rev-parse", "HEAD")

    original_capture = Reconciler._git_capture

    async def fake_capture(cwd: Path, *args: str) -> tuple[int, str]:
        if args[:3] == ("push", "origin", "HEAD:master"):
            return 1, "! [rejected] master -> master (non-fast-forward)"
        return await original_capture(cwd, *args)

    monkeypatch.setattr(Reconciler, "_git_capture", staticmethod(fake_capture))

    cfg = _settings(tmp_path, projects_dir, _write_fake_pi(tmp_path / "unused_pi.py", "pass"))
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set, default_mode="AUTONOMOUS")
    db.upsert_task(
        task_id="SG-1",
        kind="goal",
        repo="brain",
        title="Push rejection",
        spec="Report a rejected push",
        metric_target="passes",
        verify_cmd="true",
        ready=False,
    )
    reconciler = Reconciler(db, cfg)
    try:
        applied = await reconciler._apply_self_upgrade(repo, verified_sha, "SG-1")
        assert applied is True
        assert reconciler.restart_pending is True
        assert _git(repo, "rev-parse", "HEAD") == verified_sha
        reports, _ = db.drain_digest()
        assert len(reports) == 1
        assert reports[0].kind == "report"
        assert "could not be pushed" in reports[0].summary_spoken
        details = json.loads(reports[0].details_json)
        assert details["reason"] == "push_rejected_non_fast_forward"
        assert "non-fast-forward" in details["git_error"]
    finally:
        await reconciler.stop()
        db.close()


async def test_self_upgrade_refuses_stale_candidate_when_master_advanced(tmp_path: Path) -> None:
    projects_dir, repo, bare, initial_sha = _init_brain_repo(tmp_path)
    # Candidate commit based on the old master, kept on a side branch (owner-approved pinned SHA).
    _git(repo, "checkout", "-b", "brain/SG-1")
    (repo / "brain_mod.py").write_text("FAST_ENGINE = True\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "candidate")
    candidate_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "master")
    _peer_commit(tmp_path, bare, {"merged.py": "MERGED = True\n"}, message="master moved")

    cfg = _settings(tmp_path, projects_dir, _write_fake_pi(tmp_path / "unused_pi2.py", "pass"))
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set, default_mode="AUTONOMOUS")
    db.upsert_task(
        task_id="SG-1",
        kind="goal",
        repo="brain",
        title="Stale candidate",
        spec="Refuse a candidate behind master",
        metric_target="passes",
        verify_cmd="true",
        ready=False,
    )
    reconciler = Reconciler(db, cfg)
    try:
        applied = await reconciler._apply_self_upgrade(repo, candidate_sha, "SG-1")
        assert applied is False
        # Checkout stayed on old master instead of rewinding to the stale candidate.
        assert _git(repo, "rev-parse", "HEAD") == initial_sha
        assert not (repo / "brain_mod.py").read_text(encoding="utf-8").startswith("FAST_ENGINE = True")
        reports, _ = db.drain_digest()
        assert any(
            r.kind == "blocker"
            and json.loads(r.details_json)["reason"] == "origin_master_advanced_past_candidate"
            for r in reports
        )
        assert db.get_state().last_good_sha is None
    finally:
        await reconciler.stop()
        db.close()


async def test_apply_self_upgrade_rolls_back_on_failed_uv_sync_and_rebuilds_connect_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    projects_dir, repo, _, initial_sha = _init_brain_repo(tmp_path)
    connect_dir = repo / "apps" / "connect"
    connect_dir.mkdir(parents=True)
    marker = tmp_path / "connect-built.txt"
    build_sh = connect_dir / "build.sh"
    build_sh.write_text(f"#!/bin/sh\necho ok > '{marker}'\n", encoding="utf-8")
    build_sh.chmod(0o755)
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "dep change")
    candidate_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "reset", "--hard", initial_sha)

    bad_uv = _write_fake_pi(tmp_path / "bad_uv.py", "import sys; sys.exit(2)")
    good_uv = _write_fake_pi(tmp_path / "good_uv.py", "import sys; sys.exit(0)")

    cfg = _settings(tmp_path, projects_dir, _write_fake_pi(tmp_path / "noop_pi.py", "pass"))
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set, default_mode="AUTONOMOUS")
    db.upsert_task(
        task_id="SG-1", kind="goal", repo="brain", title="Dep upgrade",
        spec="Upgrade deps", metric_target="passes", verify_cmd="true", ready=False,
    )
    reconciler = Reconciler(db, cfg)
    try:
        # 1. Failed `uv sync` aborts apply, resets HEAD back to initial_sha, and clears last_good_sha.
        monkeypatch.setattr(
            "brain.reconciler.shutil.which",
            lambda name: str(bad_uv) if name == "uv" else ("/usr/bin/true" if name == "swiftc" else None),
        )
        assert await reconciler._apply_self_upgrade(repo, candidate_sha, "SG-1") is False
        assert _git(repo, "rev-parse", "HEAD") == initial_sha
        assert db.get_state().last_good_sha is None
        assert reconciler.restart_pending is False

        # 2. Passing `uv sync` runs `apps/connect/build.sh` when `swiftc` is present.
        monkeypatch.setattr(
            "brain.reconciler.shutil.which",
            lambda name: str(good_uv) if name == "uv" else ("/usr/bin/true" if name == "swiftc" else None),
        )
        monkeypatch.setattr("brain.reconciler.sys.platform", "darwin")
        assert await reconciler._apply_self_upgrade(repo, candidate_sha, "SG-1") is True
        assert marker.is_file()
        _ = asyncio
    finally:
        await reconciler.stop()
        db.close()
