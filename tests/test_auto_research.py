from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

from superbrain.config import Settings
from superbrain.db import AttemptRow, BrainDB, check_metric_target, is_better_metric
from superbrain.reconciler import Reconciler
from superbrain.skills import inspect_attempt_trajectory, validate_evidence_receipt


async def _init_repo_with_origin(tmp_path: Path, name: str = "demo") -> Path:
    bare_origin = tmp_path / "remotes" / f"{name}.git"
    bare_origin.parent.mkdir(parents=True, exist_ok=True)
    p = await asyncio.create_subprocess_exec(
        "git", "init", "--bare", "-b", "master", str(bare_origin)
    )
    await p.wait()

    projects_dir = tmp_path / "Projects"
    repo_dir = projects_dir / name
    repo_dir.mkdir(parents=True, exist_ok=True)
    for cmd in [
        ["git", "init", "-b", "master"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "remote", "add", "origin", str(bare_origin)],
    ]:
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(repo_dir))
        await proc.wait()

    (repo_dir / "kernel.py").write_text("CYCLES = 2000\n", encoding="utf-8")
    for cmd in [
        ["git", "add", "kernel.py"],
        ["git", "commit", "-m", "initial"],
        ["git", "push", "-u", "origin", "master"],
    ]:
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(repo_dir))
        await proc.wait()

    return projects_dir


def test_evidence_preserving_receipt_validates_byte_for_byte_quotes(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "engine.py").write_text(
        "def compute():\n    return 42\n",
        encoding="utf-8",
    )
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("TOP_SECRET\n", encoding="utf-8")

    raw_scout_json = json.dumps(
        {
            "summary": "compute() returns 42 in src/engine.py.",
            "uncertain": False,
            "evidence": [
                {"file": "src/engine.py", "quote": "    return 42"},
                {"file": "src/engine.py", "quote": "    return 999"},  # Hallucinated quote
                {"file": "../secret.txt", "quote": "TOP_SECRET"},  # Path escape attempt
            ],
        }
    )
    receipt = validate_evidence_receipt(tmp_path, raw_scout_json)
    assert receipt["schema"] == "sol_pi_evidence_receipt_v1"
    assert receipt["uncertain"] is False
    assert len(receipt["verified_evidence"]) == 1
    assert receipt["rejected_count"] == 2
    ev = receipt["verified_evidence"][0]
    assert ev["file"] == "src/engine.py"
    assert ev["line"] == 2
    assert ev["quote"] == "    return 42"
    assert "sol_pi_evidence_receipt_v1" in receipt["receipt_text"]


def test_oracle_trajectory_metrics_count_adjacent_edit_bash_and_repeated_errors(
    tmp_path: Path,
) -> None:
    jsonl_path = tmp_path / "session.jsonl"
    events = [
        {
            "role": "assistant",
            "content": [{"type": "toolCall", "name": "edit", "arguments": {"path": "a.py"}}],
        },
        {"role": "toolResult", "toolName": "edit", "isError": False, "content": "ok"},
        {
            "role": "assistant",
            "content": [{"type": "toolCall", "name": "bash", "arguments": {"command": "pytest"}}],
        },
        {"role": "toolResult", "toolName": "bash", "isError": True, "content": "fail 1"},
        {
            "role": "assistant",
            "content": [{"type": "toolCall", "name": "bash", "arguments": {"command": "pytest"}}],
        },
        {"role": "toolResult", "toolName": "bash", "isError": True, "content": "fail 2"},
    ]
    jsonl_path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    attempt = AttemptRow(
        id=1,
        task_id="SG-1",
        role="builder",
        pi_session_id="s1",
        session_jsonl_path=str(jsonl_path),
        summary="Tried edit",
        git_branch="brain/SG-1",
        git_sha=None,
        cost_usd=0.1,
        outcome="failed_verify",
        verify_output="failed",
        created_at="2026-11-22T00:00:00Z",
        metric_value=1500.0,
    )
    rec = inspect_attempt_trajectory(attempt)
    assert rec["metric_value"] == 1500.0
    assert rec["oracle"]["assistant_turns"] == 3
    assert rec["oracle"]["adjacent_edit_bash_pairs"] == 1
    assert rec["oracle"]["repeated_error_tools"] == ["bash"]


def test_metric_target_comparators_and_direction() -> None:
    assert check_metric_target("cycles <= 1200", 1127.0) is True
    assert check_metric_target("cycles <= 1200", 1350.0) is False
    assert check_metric_target("coverage >= 90%", 92.5) is True
    assert check_metric_target("coverage >= 90%", 85.0) is False
    assert is_better_metric("cycles <= 1200", 1100.0, 1300.0) is True
    assert is_better_metric("coverage >= 90%", 95.0, 91.0) is True


async def test_research_scout_rpc_and_bfs_auto_research_hypothesis_tournament(
    tmp_path: Path,
) -> None:
    projects_dir = await _init_repo_with_origin(tmp_path, "demo")
    fake_pi = tmp_path / "fake_pi_autoresearch.py"
    fake_pi.write_text(
        r"""#!/usr/bin/env python3
import json, os, pathlib, socket, sys

if os.environ.get("SUPERBRAIN_RESEARCH_SCOUT") == "1":
    print(json.dumps({
        "role": "assistant",
        "content": json.dumps({
            "summary": "kernel.py defines CYCLES = 2000.",
            "uncertain": False,
            "evidence": [
                {"file": "kernel.py", "quote": "CYCLES = 2000"},
                {"file": "kernel.py", "quote": "NON_EXISTENT_LINE"}
            ]
        }),
        "cost_usd": 0.02,
    }), flush=True)
    sys.exit(0)

sock_path = os.environ["SUPERBRAIN_RPC_SOCK"]
task_id = os.environ.get("SUPERBRAIN_TASK_ID", "")
worktree = os.environ.get("SUPERBRAIN_WORKTREE_DIR")

def rpc(method, params):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    s.sendall((json.dumps({"method": method, "params": params}) + "\n").encode())
    resp = json.loads(s.recv(16384).decode().splitlines()[0])
    s.close()
    return resp

if not worktree:
    # Auto-Research Planner: first call research_scout, then fan out 3 competing BFS hypotheses
    scout_res = rpc("research_scout", {"question": "What is CYCLES set to in kernel.py?", "paths": ["kernel.py"]})
    assert scout_res["ok"] is True, scout_res
    receipt = scout_res["result"]
    assert len(receipt["verified_evidence"]) == 1
    assert receipt["rejected_count"] == 1

    verify_cmd = 'python3 -c "import kernel; print(\'METRIC:\', kernel.CYCLES)"'
    for idx, val in [(1, 1400), (2, 1100), (3, 950)]:
        r = rpc("upsert_task", {
            "id": f"SG-1.{idx}",
            "parent_id": "SG-1",
            "kind": "goal",
            "repo": "demo",
            "title": f"Hypothesis {idx}: CYCLES={val}",
            "spec": f"Set CYCLES = {val}",
            "metric_target": "cycles <= 1200",
            "verify_cmd": verify_cmd,
            "ready": True,
        })
        assert r["ok"] is True, r
    print(json.dumps({"role": "assistant", "content": "Spawned 3 BFS hypotheses.", "cost_usd": 0.05}), flush=True)
else:
    wt = pathlib.Path(worktree)
    if task_id == "SG-1":
        # Initial single-lineage attempt misses target (1600 > 1200)
        (wt / "kernel.py").write_text("CYCLES = 1600\n", encoding="utf-8")
    elif task_id == "SG-1.1":
        # Hypothesis 1 still misses target (1400 > 1200)
        (wt / "kernel.py").write_text("CYCLES = 1400\n", encoding="utf-8")
    elif task_id == "SG-1.2":
        # Hypothesis 2 beats target (1100 <= 1200)
        (wt / "kernel.py").write_text("CYCLES = 1100\n", encoding="utf-8")
    print(json.dumps({"role": "assistant", "content": f"Ran {task_id}", "cost_usd": 0.04}), flush=True)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(fake_pi.stat().st_mode | stat.S_IEXEC)

    cfg = Settings(
        home_dir=tmp_path / "home",
        projects_dir=projects_dir,
        allowed_repos="demo",
        pi_bin=str(fake_pi),
        default_mode="AUTONOMOUS",
        auto_research_after_strikes=1,
        sandbox_enabled=True,
    )
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set, default_mode="AUTONOMOUS")
    reconciler = Reconciler(db, cfg)
    try:
        parent = db.upsert_task(
            kind="goal",
            repo="demo",
            title="Optimize kernel cycles",
            spec="Reduce CYCLES below 1200",
            metric_target="cycles <= 1200",
            verify_cmd="python3 -c 'import kernel; print(f\"METRIC: {kernel.CYCLES}\")'",
            ready=True,
        )

        # Tick 1: SG-1 runs Builder, gets METRIC: 1600 (fails `cycles <= 1200`),
        # and automatically triggers Auto-Research Planner (`auto_research_after_strikes=1`),
        # which calls `research_scout` and fans out SG-1.1, SG-1.2, SG-1.3!
        t1 = await reconciler.tick()
        assert t1 is not None and t1.id == parent.id
        assert t1.status == "draft"
        children = [t for t in db.list_tasks() if t.parent_id == parent.id]
        assert [c.id for c in children] == ["SG-1.1", "SG-1.2", "SG-1.3"]

        # Tick 2: SG-1.1 runs Builder, gets METRIC: 1400 -> fails metric_target `cycles <= 1200`
        c1 = await reconciler.tick()
        assert c1 is not None and c1.id == "SG-1.1"
        assert c1.strike_count == 1
        c1_att = db.list_attempts("SG-1.1")[0]
        assert c1_att.outcome == "failed_verify"
        assert c1_att.metric_value == 1400.0

        # Set SG-1.1 to draft so SG-1.2 (higher priority / next hypothesis) runs on next tick
        db.upsert_task(
            task_id="SG-1.1",
            parent_id="SG-1",
            kind="goal",
            repo="demo",
            title=c1.title,
            spec=c1.spec,
            metric_target=c1.metric_target,
            verify_cmd=c1.verify_cmd,
            ready=False,
        )

        # Tick 3: SG-1.2 runs Builder, gets METRIC: 1100 (satisfies `cycles <= 1200`) ->
        # pushes branch, records best_metric=1100.0 on SG-1, and marks sibling SG-1.3 done!
        c2 = await reconciler.tick()
        assert c2 is not None and c2.id == "SG-1.2"
        assert c2.status == "done"
        assert c2.best_metric == 1100.0

        updated_parent = db.get_task("SG-1")
        assert updated_parent.best_metric == 1100.0
        assert updated_parent.best_branch == "brain/SG-1.2"
        assert db.get_task("SG-1.3").status == "done"
        # Scout cost ($0.02) + planner ($0.05) + 3 builders (3 * $0.04) = $0.19 tracked in spent_today_usd
        assert db.get_state().spent_today_usd >= 0.19
    finally:
        await reconciler.stop()
        db.close()
