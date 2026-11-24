from __future__ import annotations

import json
from pathlib import Path

from brain.config import Settings
from brain.db import BrainDB, TraceRunRow
from brain.evals import append_case, harvest_eval_cases, normalized, read_cases, split_of, suite_path
from brain.sandbox import build_sandbox_profile


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        home_dir=tmp_path / "home",
        projects_dir=tmp_path / "Projects",
        allowed_repos="jarvis",
        brain_api_key="secret-token",
        sandbox_enabled=False,
    )


def test_split_is_deterministic_and_about_one_in_five(tmp_path: Path) -> None:
    text = "fix the navbar on the opscom status page"
    assert split_of(text) == split_of(text.upper() + "!!!")  # words only, so punctuation cannot move a case
    seeds = [f"deploy service number {index} to production" for index in range(400)]
    sealed = sum(1 for seed in seeds if split_of(seed) == "held_out")
    assert 40 <= sealed <= 120, sealed


def test_append_case_dedupes_on_words(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    assert append_case(cfg.home_dir, "intent", {"input": "Build a login page", "intent": "addressed_to_jarvis"})
    assert not append_case(cfg.home_dir, "intent", {"input": "build a  LOGIN page!", "intent": "mute"})
    case = {"input": "Build a login page", "intent": "addressed_to_jarvis"}
    stored = read_cases(suite_path(cfg.home_dir, split_of("Build a login page"), "intent"))
    assert stored == [case]
    assert normalized("Build a login page!") == "build a login page"


def test_harvest_intent_reads_corrections_only(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    feedback = cfg.home_dir / "feedback" / "intent.jsonl"
    feedback.parent.mkdir(parents=True)
    feedback.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "d1",
                        "state": {"document": "lock the screen"},
                        "acted": {"intent": "ignore_background", "source": "laya"},
                    }
                ),
                json.dumps({"id": "d2", "state": {"document": "shut up"}, "acted": {"intent": "mute", "source": "fast"}}),
                json.dumps({"correction_of": "d1", "signal": "said again", "label": "addressed_to_jarvis"}),
                json.dumps({"correction_of": "d2", "signal": "undone mute", "not_label": "mute"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    added = harvest_eval_cases(db, cfg)
    assert added["intent"] == 1  # the fast-path mute is a rule, not a model miss
    cases = read_cases(suite_path(cfg.home_dir, split_of("lock the screen"), "intent"))
    assert cases == [{"input": "lock the screen", "intent": "addressed_to_jarvis"}]


def test_harvest_traces_learns_the_retry_and_the_rewrite(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    cfg.traces_dir.mkdir(parents=True)
    (cfg.traces_dir / "2026-11-24").mkdir()
    (cfg.traces_dir / "2026-11-24" / "trace-rewrite.json").write_text(
        json.dumps({"voice": {"stt": "fix the murb in opscom", "cleanup": "fix the merge in opscom"}}),
        encoding="utf-8",
    )
    common = dict(
        turn_id="t1",
        source="voice",
        runner="t3",
        task_id=None,
        attempt_id=None,
        latency_ms=900,
        ttfb_ms=None,
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
        git_sha="master @ abc1234",
        prompt_sha="deadbeef",
        skills_used=(),
        reflected=0,
    )
    db.upsert_trace_run(TraceRunRow(**dict(
        common, trace_id="1" * 32, surface="brain_router", repo="", outcome="friction",
        friction_kind="router_override", causal_sig="wrong_target||router_disambiguation",
        user_input="fix the login page", output_summary="wrong project", trace_path="",
        superseded_by="2" * 32)))
    db.upsert_trace_run(TraceRunRow(**dict(
        common, trace_id="2" * 32, surface="", repo="opscom/status", outcome="ok",
        friction_kind=None, causal_sig="", user_input="fix the login page in opscom",
        output_summary="sent", trace_path="", superseded_by=None)))
    db.upsert_trace_run(TraceRunRow(**dict(
        common, trace_id="3" * 32, surface="whisper_asr_data", repo="", outcome="ok",
        friction_kind="asr_rewrite", causal_sig="cleanup_rewrote|ratio=0.50|asr_domain_terms",
        user_input="fix the murb in opscom", output_summary="sent",
        trace_path="2026-11-24/trace-rewrite.json", superseded_by=None)))
    added = harvest_eval_cases(db, cfg)
    assert added["brain_route"] == 1
    assert added["voice_prompts"] == 1
    route = read_cases(suite_path(cfg.home_dir, split_of("fix the login page"), "brain_route"))[0]
    assert route["expected_tool"] == "delegate_project"
    assert route["project"] == "opscom/status"
    rewrite = read_cases(suite_path(cfg.home_dir, split_of("fix the murb in opscom"), "voice_prompts"))[0]
    assert rewrite["cleanup"] == "fix the merge in opscom"


def test_harvest_scrubs_a_secret(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-live-abcdefghijklmnop")
    cfg = _settings(tmp_path)
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    cfg.traces_dir.mkdir(parents=True)
    (cfg.traces_dir / "2026-11-24").mkdir()
    (cfg.traces_dir / "2026-11-24" / "trace-x.json").write_text(
        json.dumps({"voice": {"stt": "send it to sk-live-abcdefghijklmnop", "cleanup": "send it to the queue"}}),
        encoding="utf-8",
    )
    db.upsert_trace_run(
        TraceRunRow(
            trace_id="4" * 32, turn_id="t1", source="voice", surface="whisper_asr_data", runner="t3",
            repo="", task_id=None, attempt_id=None, outcome="ok", friction_kind="asr_rewrite",
            causal_sig="cleanup_rewrote|ratio=0.60|asr_domain_terms", latency_ms=900, ttfb_ms=None,
            tokens_in=0, tokens_out=0, cost_usd=0.0, git_sha="master @ abc1234", prompt_sha="deadbeef",
            skills_used=(), user_input="send it to sk-live-abcdefghijklmnop", output_summary="sent",
            trace_path="2026-11-24/trace-x.json", superseded_by=None, reflected=0,
        )
    )
    harvest_eval_cases(db, cfg)
    written = suite_path(cfg.home_dir, "held_in", "voice_prompts")
    if not written.is_file():
        written = suite_path(cfg.home_dir, "held_out", "voice_prompts")
    assert "sk-live" not in written.read_text(encoding="utf-8")


async def test_eval_split_rejects_a_held_out_regression(tmp_path: Path, monkeypatch) -> None:
    """A candidate that breaks a sealed case never lands, even when its own score looks fine."""
    from types import SimpleNamespace

    from brain.reconciler import Reconciler

    cfg = _settings(tmp_path)
    cfg.openrouter_api_key = "sk-test"
    held_out = cfg.home_dir / "evals" / "held_out"
    held_out.mkdir(parents=True)
    (held_out / "brain_route.jsonl").write_text('{"input": "x"}\n', encoding="utf-8")
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    rec = Reconciler(db, cfg)
    answers = iter(
        [
            ({"a": True, "b": True}, {}),  # baseline, held_out
            ({"a": True, "b": False}, {}),  # candidate, held_out: b regressed
        ]
    )

    async def fake_split(*args, **kwargs):
        return next(answers)

    monkeypatch.setattr(rec, "_run_eval_split", fake_split)
    task = SimpleNamespace(dedupe_key=None)
    ok, out = await rec._run_eval_splits(
        tmp_path / "repo", tmp_path / "worktree", task, {"apps/brain/src/brain/orchestrator.py"}, tmp_path
    )
    assert not ok
    assert "held-out" in out


async def test_a_harness_fix_must_fix_a_held_in_case(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    from brain.reconciler import Reconciler

    cfg = _settings(tmp_path)
    cfg.openrouter_api_key = "sk-test"
    held_out = cfg.home_dir / "evals" / "held_out"
    held_out.mkdir(parents=True)
    (held_out / "brain_route.jsonl").write_text('{"input": "x"}\n', encoding="utf-8")
    db = BrainDB(cfg.db_path, cfg.allowed_repo_set)
    rec = Reconciler(db, cfg)
    answers = iter(
        [
            ({"a": True}, {}),  # baseline held_out
            ({"a": True}, {}),  # candidate held_out: no regression
            ({"a": False}, {}),  # baseline held_in: the case the proposal promised to fix
            ({"a": False}, {}),  # candidate held_in: still broken
        ]
    )

    async def fake_split(*args, **kwargs):
        return next(answers)

    monkeypatch.setattr(rec, "_run_eval_split", fake_split)
    ok, out = await rec._run_eval_splits(
        tmp_path / "repo",
        tmp_path / "worktree",
        SimpleNamespace(dedupe_key="harness:brain_router"),
        {"apps/brain/src/brain/orchestrator.py"},
        tmp_path,
    )
    assert not ok
    assert "held-in" in out


def test_held_out_is_never_readable_by_a_worker(tmp_path: Path) -> None:
    profile = build_sandbox_profile(
        tmp_path / "worktree", tmp_path / "session", tmp_path / "notes", home_dir=tmp_path / "home"
    )
    assert "evals/held_out" in profile
    # Even the Improver, allowed to read traces, cannot open the set that measures it.
    improver = build_sandbox_profile(
        tmp_path / "worktree", tmp_path / "session", tmp_path / "notes",
        home_dir=tmp_path / "home", allow_read_traces=True,
    )
    assert "evals/held_out" in improver
