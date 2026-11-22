from __future__ import annotations

import asyncio
import json
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from brain.config import Settings
from brain.db import AttemptRow, BrainDB, check_metric_target, is_better_metric
from brain.reconciler import Reconciler, extract_metric_value
from brain.skills import inspect_attempt_trajectory, validate_evidence_receipt


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

    # Non-JSON raw scout output or all-fake quotes withhold unverified summary text
    non_json = validate_evidence_receipt(tmp_path, "LEAKED_SECRET=12345")
    assert non_json["uncertain"] is True
    assert "LEAKED_SECRET" not in non_json["summary"]
    assert "LEAKED_SECRET" not in non_json["receipt_text"]

    all_fake = validate_evidence_receipt(
        tmp_path,
        json.dumps(
            {
                "summary": "LEAKED_ENV_KEY=abc",
                "uncertain": False,
                "evidence": [{"file": "../secret.txt", "quote": "TOP_SECRET"}],
            }
        ),
    )
    assert all_fake["uncertain"] is True
    assert "LEAKED_ENV_KEY" not in all_fake["summary"]
    assert "LEAKED_ENV_KEY" not in all_fake["receipt_text"]


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
    # Arrow notation `->` must NOT be misparsed as `>`
    assert check_metric_target("latency 500 -> 200", 150.0) is True
    assert is_better_metric("latency 500 -> 200", 150.0, 500.0) is True
    assert is_better_metric("latency 500 -> 200", 900.0, 500.0) is False
    # `METRIC:` parser handles trailing units without dropping decimals
    assert extract_metric_value("METRIC: 12.5ms") == 12.5
    assert extract_metric_value("METRIC: 120ms") == 120.0
    assert extract_metric_value("METRIC=92.5%") == 92.5


def test_child_strategy_all_preserves_sequential_steps_while_any_handles_blocked_sibling(
    tmp_path: Path,
) -> None:
    db = BrainDB(tmp_path / "state.db", allowed_repos={"demo"}, default_mode="AUTONOMOUS")
    try:
        # 1. Sequential multi-step parent (`child_strategy='all'`): step 1 hitting metric must NOT skip step 2
        seq_parent = db.upsert_task(
            kind="goal",
            repo="demo",
            title="Build auth and billing",
            spec="Two sequential steps",
            metric_target="coverage >= 90%",
            child_strategy="all",
            ready=False,
        )
        s1 = db.upsert_task(
            task_id="SG-1.1",
            parent_id=seq_parent.id,
            kind="goal",
            repo="demo",
            title="Step 1: auth",
            spec="Auth",
            metric_target="coverage >= 90%",
            verify_cmd="pytest",
            ready=True,
        )
        s2 = db.upsert_task(
            task_id="SG-1.2",
            parent_id=seq_parent.id,
            kind="goal",
            repo="demo",
            title="Step 2: billing",
            spec="Billing",
            metric_target="coverage >= 90%",
            verify_cmd="pytest",
            ready=True,
        )
        db.claim_next_task(task_id=s1.id)
        db.finish_attempt(
            task_id=s1.id,
            role="builder",
            summary="Built auth",
            outcome="pushed",
            git_branch="brain/SG-1.1",
            git_sha="aaa1111",
            metric_value=95.0,
        )
        assert db.get_task(s2.id).status == "ready"
        assert db.get_task(seq_parent.id).status == "draft"

        # 2. Hypothesis tournament (`child_strategy='any'`): even if SG-2.1 is blocked, SG-2.2 winning completes SG-2
        bfs_parent = db.upsert_task(
            task_id="SG-2",
            kind="goal",
            repo="demo",
            title="Optimize kernel",
            spec="Try competing algorithms",
            metric_target="cycles <= 1200",
            verify_cmd="pytest",
            child_strategy="any",
            ready=False,
        )
        h1 = db.upsert_task(
            task_id="SG-2.1",
            parent_id=bfs_parent.id,
            kind="goal",
            repo="demo",
            title="Hyp 1",
            spec="Dead end",
            metric_target="cycles <= 1200",
            verify_cmd="pytest",
            ready=True,
        )
        h2 = db.upsert_task(
            task_id="SG-2.2",
            parent_id=bfs_parent.id,
            kind="goal",
            repo="demo",
            title="Hyp 2",
            spec="Winner",
            metric_target="cycles <= 1200",
            verify_cmd="pytest",
            ready=True,
        )
        for _ in range(3):
            db.claim_next_task(task_id=h1.id)
            db.finish_attempt(task_id=h1.id, role="builder", summary="Fail", outcome="failed_verify")
        assert db.get_task(h1.id).status == "blocked"

        db.claim_next_task(task_id=h2.id)
        db.finish_attempt(
            task_id=h2.id,
            role="builder",
            summary="Won",
            outcome="pushed",
            git_branch="brain/SG-2.2",
            git_sha="bbb2222",
            metric_value=1100.0,
        )
        done_parent = db.get_task(bfs_parent.id)
        assert done_parent.status == "done"
        assert done_parent.best_metric == 1100.0
        assert done_parent.best_branch == "brain/SG-2.2"
    finally:
        db.close()


async def test_research_scout_rpc_and_bfs_auto_research_hypothesis_tournament(
    tmp_path: Path,
) -> None:
    projects_dir = await _init_repo_with_origin(tmp_path, "demo")
    fake_pi = tmp_path / "fake_pi_autoresearch.py"
    fake_pi.write_text(
        r"""#!/usr/bin/env python3
import json, os, pathlib, socket, sys

if os.environ.get("BRAIN_RESEARCH_SCOUT") == "1":
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

sock_path = os.environ["BRAIN_RPC_SOCK"]
task_id = os.environ.get("BRAIN_TASK_ID", "")
worktree = os.environ.get("BRAIN_WORKTREE_DIR")

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
        assert updated_parent.status == "done"
        assert updated_parent.best_metric == 1100.0
        assert updated_parent.best_branch == "brain/SG-1.2"
        assert db.get_task("SG-1.3").status == "done"
        # Scout cost ($0.02) + planner ($0.05) + 3 builders (3 * $0.04) = $0.19 tracked in spent_today_usd
        assert db.get_state().spent_today_usd >= 0.19
    finally:
        await reconciler.stop()
        db.close()


async def test_scout_sandbox_blocks_env_secret_and_in_place_research_preserves_spec(
    tmp_path: Path,
) -> None:
    projects_dir = await _init_repo_with_origin(tmp_path, "demo")
    home_dir = tmp_path / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    secret_env = home_dir / ".env"
    secret_env.write_text("BRAIN_API_KEY=super-secret-value\n", encoding="utf-8")

    fake_pi = tmp_path / "fake_pi_scout_sandbox.py"
    fake_pi.write_text(
        f"""#!/usr/bin/env python3
import json, os, pathlib, socket, sys

if os.environ.get("BRAIN_RESEARCH_SCOUT") == "1":
    env_path = pathlib.Path({str(secret_env)!r})
    try:
        leaked = env_path.read_text(encoding="utf-8")
    except OSError:
        leaked = "BLOCKED_BY_SANDBOX"
    print(json.dumps({{
        "role": "assistant",
        "content": json.dumps({{
            "summary": f"env={{leaked}}",
            "uncertain": False,
            "evidence": [{{"file": "kernel.py", "quote": "CYCLES = 2000"}}],
        }}),
        "cost_usd": 0.01,
    }}), flush=True)
    sys.exit(0)

sock_path = os.environ["BRAIN_RPC_SOCK"]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sock_path)
s.sendall((json.dumps({{"method": "research_scout", "params": {{"question": "check"}}}}) + "\\n").encode())
scout_resp = json.loads(s.recv(16384).decode().splitlines()[0])
s.close()
assert "BLOCKED_BY_SANDBOX" in scout_resp["result"]["summary"], scout_resp
assert "super-secret-value" not in scout_resp["result"]["summary"]

# Refine SG-1 in place (no child tasks) with ready=True
s2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s2.connect(sock_path)
s2.sendall((json.dumps({{
    "method": "upsert_task",
    "params": {{
        "id": "SG-1",
        "kind": "goal",
        "repo": "demo",
        "title": "Optimize kernel",
        "spec": "Refined spec from Oracle",
        "metric_target": "cycles <= 1200",
        "verify_cmd": "true",
        "ready": True,
    }},
}}) + "\\n").encode())
_ = s2.recv(4096)
s2.close()
print(json.dumps({{"role": "assistant", "content": "Refined SG-1.", "cost_usd": 0.01}}), flush=True)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(fake_pi.stat().st_mode | stat.S_IEXEC)

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
        t = db.upsert_task(
            kind="goal",
            repo="demo",
            title="Optimize kernel",
            spec="Initial spec",
            metric_target="cycles <= 1200",
            verify_cmd="true",
            ready=False,
        )
        await reconciler.run_auto_research(t.id)
        refreshed = db.get_task(t.id)
        assert refreshed.spec == "Refined spec from Oracle"
        assert refreshed.status == "ready"

        # Running auto-research while task is in_progress fails fast before calling pi
        db.claim_next_task(task_id=t.id)
        with pytest.raises(ValueError, match="in_progress"):
            await reconciler.run_auto_research(t.id)
    finally:
        await reconciler.stop()
        db.close()


def test_board_ts_edit_and_run_action_fusion_end_to_end(tmp_path: Path) -> None:
    node_bin = shutil.which("node")
    pi_ver_file = Path("~/.pi/agent/install/current-version").expanduser()
    if not node_bin or not pi_ver_file.is_file():
        pytest.skip("Node or managed Pi installation not available")
    pi_ver = pi_ver_file.read_text(encoding="utf-8").strip()
    pi_nm = Path(f"~/.pi/agent/install/releases/{pi_ver}/node_modules").expanduser()
    if not (pi_nm / "@earendil-works" / "pi-coding-agent").is_dir():
        pytest.skip("pi-coding-agent package not found")

    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True)
    (worktree / "math_mod.py").write_text("X = 1\n", encoding="utf-8")

    harness_dir = tmp_path / "runner"
    harness_dir.mkdir(parents=True)
    (harness_dir / "node_modules").symlink_to(pi_nm)
    board_src = (
        Path(__file__).resolve().parents[1] / "src" / "brain" / "defaults" / "pi" / "board.ts"
    )
    shutil.copy2(board_src, harness_dir / "board.ts")
    driver = harness_dir / "run_test.mjs"
    driver.write_text(
        """
import registerBoard from "./board.ts";

const tools = new Map();
registerBoard({
  registerTool(def) { tools.set(def.name, def); },
  on() {},
});

const editAndRun = tools.get("edit_and_run");
const ctx = { cwd: process.env.BRAIN_WORKTREE_DIR };

// 1. Valid fused edit + command with null timeout_seconds -> succeeds in 1 turn
const okRes = await editAndRun.execute(
  "tc-1",
  {
    path: "math_mod.py",
    old_text: "X = 1",
    new_text: "X = 42",
    command: "python3 -c 'import math_mod; assert math_mod.X == 42; print(42)'",
    timeout_seconds: null,
  },
  undefined,
  undefined,
  ctx,
);
if (okRes.isError || !okRes.content[0].text.includes("[then_run:succeeded]")) {
  throw new Error("Expected succeeded: " + JSON.stringify(okRes));
}

// 2. Outside-worktree path -> blocked and command skipped
const escapeRes = await editAndRun.execute(
  "tc-2",
  {
    path: "../outside.py",
    old_text: "",
    new_text: "bad",
    command: "echo SHOULD_NOT_RUN",
  },
  undefined,
  undefined,
  ctx,
);
if (!escapeRes.isError || !escapeRes.content[0].text.includes("[then_run:skipped]")) {
  throw new Error("Expected skipped on escape: " + JSON.stringify(escapeRes));
}

// 3. Mismatched old_text -> skipped without running command
const missRes = await editAndRun.execute(
  "tc-3",
  {
    path: "math_mod.py",
    old_text: "DOES_NOT_EXIST",
    new_text: "X = 0",
    command: "echo SHOULD_NOT_RUN",
  },
  undefined,
  undefined,
  ctx,
);
if (!missRes.isError || !missRes.content[0].text.includes("[then_run:skipped]")) {
  throw new Error("Expected skipped on edit miss: " + JSON.stringify(missRes));
}
console.log("BOARD_TS_OK");
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [node_bin, str(driver)],
        cwd=str(harness_dir),
        env={
            **dict(
                PATH=f"{Path(node_bin).parent}:/usr/bin:/bin",
                HOME=str(tmp_path),
                BRAIN_WORKTREE_DIR=str(worktree),
                BRAIN_TASK_KIND="problem",
            )
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "BOARD_TS_OK" in proc.stdout
    assert (worktree / "math_mod.py").read_text(encoding="utf-8") == "X = 42\n"
