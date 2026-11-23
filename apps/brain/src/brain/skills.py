from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from brain.db import AttemptRow, BrainDB, SkillRow
from brain.memory import FORBIDDEN_PATTERNS

ProposalAction = Literal["create", "update", "append", "retire"]

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class SkillProposal:
    action: ProposalAction
    slug: str
    lesson: str
    description: str = ""
    body_markdown: str = ""
    evidence_attempt_ids: tuple[int, ...] = ()
    expected_sha256: str | None = None
    retire_slug: str | None = None
    retire_reason: str | None = None


def render_skill_markdown(slug: str, description: str, body_markdown: str) -> str:
    clean_desc = " ".join(description.strip().splitlines()).replace('"', '\\"')
    clean_body = body_markdown.strip()
    return f'---\nname: {slug}\ndescription: "{clean_desc}"\n---\n\n{clean_body}\n'


def _extract_skill_body(content: str) -> str:
    if content.startswith("---\n"):
        end_idx = content.find("\n---\n", 4)
        if end_idx >= 0:
            return content[end_idx + 5 :].strip()
    return content.strip()


def validate_skill_proposal(
    db: BrainDB,
    proposal: SkillProposal,
    *,
    max_personal_skills: int = 10,
) -> tuple[list[int], str, str]:
    """
    Deterministic verification gate for self-improving skill proposals.
    Returns `(validated_attempt_ids, resolved_description, rendered_markdown)`.
    """
    slug = proposal.slug.strip()
    if not slug or len(slug) > 64 or not _SLUG_RE.fullmatch(slug):
        raise ValueError(
            f"Invalid skill slug {slug!r}: must be lowercase alphanumeric words separated by hyphens."
        )
    if proposal.action not in ("create", "update", "append", "retire"):
        raise ValueError(f"Unsupported proposal action: {proposal.action!r}")
    if not proposal.lesson.strip():
        raise ValueError("Skill proposal requires a non-empty lesson.")

    existing = db.get_skill(slug)
    if proposal.action == "retire":
        if existing is None or existing.status != "active":
            raise ValueError(f"Cannot retire non-active skill {slug!r}.")
        return [], "", ""

    known_attempt_ids = {a.id for a in db.list_attempts()}
    cited_ids = sorted({int(i) for i in proposal.evidence_attempt_ids if int(i) in known_attempt_ids})

    if proposal.action == "append":
        if existing is None or existing.status != "active":
            raise ValueError(f"Cannot append to non-existent or retired skill {slug!r}.")
        if len(cited_ids) < 1:
            raise ValueError("Appending to a skill requires >= 1 grounded attempt ID.")
        if not proposal.expected_sha256 or proposal.expected_sha256.strip() != existing.sha256:
            raise ValueError(
                f"Baseline sha256 mismatch for {slug!r}: expected {proposal.expected_sha256!r}, "
                f"current is {existing.sha256!r}."
            )
        addition = proposal.body_markdown.strip()
        if not addition:
            raise ValueError("Skill body_markdown must not be empty.")
        existing_body = _extract_skill_body(existing.content)
        if addition in existing_body:
            raise ValueError("Proposed rule is already present in skill body.")
        desc = proposal.description.strip() or existing.description
        body = f"{existing_body}\n{addition}" if existing_body else addition
    else:
        desc = proposal.description.strip()
        body = proposal.body_markdown.strip()

    if not desc or len(desc) > 1024:
        raise ValueError("Skill description must be 1..1024 characters.")
    if not body:
        raise ValueError("Skill body_markdown must not be empty.")
    if len(body.splitlines()) > 150:
        raise ValueError("Skill body_markdown exceeds 150-line limit.")

    rendered = render_skill_markdown(slug, desc, body)
    for pat in FORBIDDEN_PATTERNS:
        if pat.search(rendered):
            raise ValueError(f"Skill content rejected by safety guardrail ({pat.pattern}).")

    if proposal.action == "create":
        if existing is not None and existing.status == "active":
            raise ValueError(f"Skill {slug!r} already exists; use action='update' or 'append'.")
        if len(cited_ids) < 2:
            raise ValueError(
                f"Creating a new skill requires >= 2 grounded attempt IDs (got {cited_ids})."
            )
        active_skills = db.list_skills(status="active")
        if len(active_skills) >= max_personal_skills:
            retire_slug = (proposal.retire_slug or "").strip()
            retire_reason = (proposal.retire_reason or "").strip()
            if not retire_slug or not retire_reason:
                raise ValueError(
                    f"Skill library is at capacity ({max_personal_skills}). "
                    "To create a new skill, specify retire_slug and retire_reason."
                )
            retire_target = db.get_skill(retire_slug)
            if retire_target is None or retire_target.status != "active":
                raise ValueError(f"retire_slug {retire_slug!r} is not an active skill.")
    elif proposal.action == "update":
        if existing is None or existing.status != "active":
            raise ValueError(f"Cannot update non-existent or retired skill {slug!r}.")
        if len(cited_ids) < 1:
            raise ValueError("Updating a skill requires >= 1 grounded attempt ID.")
        if not proposal.expected_sha256 or proposal.expected_sha256.strip() != existing.sha256:
            raise ValueError(
                f"Baseline sha256 mismatch for {slug!r}: expected {proposal.expected_sha256!r}, "
                f"current is {existing.sha256!r}."
            )

    return cited_ids, desc, rendered


def apply_skill_proposal(
    db: BrainDB,
    skills_dir: Path,
    proposal: SkillProposal,
    *,
    max_personal_skills: int = 10,
) -> SkillRow:
    """Verifies and activates a skill proposal on disk and in SQLite."""
    cited_ids, resolved_desc, rendered = validate_skill_proposal(
        db, proposal, max_personal_skills=max_personal_skills
    )
    skills_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    if proposal.action == "retire":
        retired = db.retire_skill(proposal.slug.strip())
        _remove_skill_dir(skills_dir, retired.slug)
        db.queue_outbox(
            kind="report",
            summary_spoken=f"Retired personal skill {retired.slug}.",
            details={"action": "retire", "slug": retired.slug, "lesson": proposal.lesson},
        )
        return retired

    if proposal.action == "create" and proposal.retire_slug:
        retire_target = proposal.retire_slug.strip()
        if retire_target:
            db.retire_skill(retire_target)
            _remove_skill_dir(skills_dir, retire_target)

    saved = db.save_skill(
        slug=proposal.slug.strip(),
        name=proposal.slug.strip(),
        description=resolved_desc,
        content=rendered,
        lesson=proposal.lesson.strip(),
        evidence_attempt_ids=cited_ids,
    )
    _write_skill_file(skills_dir, saved.slug, saved.content)
    verb = "Created" if proposal.action == "create" else "Updated"
    db.queue_outbox(
        kind="report",
        summary_spoken=f"{verb} personal skill {saved.slug} (v{saved.version}): {saved.lesson}",
        details={
            "action": proposal.action,
            "slug": saved.slug,
            "version": saved.version,
            "sha256": saved.sha256,
            "evidence_attempt_ids": list(saved.evidence_attempt_ids),
        },
    )
    return saved


def sync_skills_on_disk(db: BrainDB, skills_dir: Path) -> None:
    """Reconciles `<skills_dir>/<slug>/SKILL.md` files with active skills in SQLite."""
    skills_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    active = {s.slug: s for s in db.list_skills(status="active")}
    for child in skills_dir.iterdir():
        if child.is_dir() and child.name not in active:
            shutil.rmtree(child, ignore_errors=True)
    for slug, skill in active.items():
        _write_skill_file(skills_dir, slug, skill.content)


def record_skill_outcome(
    db: BrainDB, skills_dir: Path, slugs: list[str], *, succeeded: bool
) -> list[SkillRow]:
    """
    Updates skill success/failure counters after an attempt and syncs disk if any skill
    was auto-rolled back or retired after 2 consecutive failures.
    """
    if not slugs:
        return []
    rolled_back = db.record_skill_usage(slugs, succeeded=succeeded)
    if rolled_back:
        sync_skills_on_disk(db, skills_dir)
        for skill in rolled_back:
            action_note = (
                f"rolled back to v{skill.version}"
                if skill.status == "active"
                else "retired"
            )
            db.queue_outbox(
                kind="report",
                summary_spoken=f"Skill {skill.slug} was automatically {action_note} after 2 failed attempts.",
                details={"slug": skill.slug, "status": skill.status, "version": skill.version},
            )
    return rolled_back


def inspect_attempt_trajectory(attempt: AttemptRow, *, diff_stat: str = "") -> dict[str, Any]:
    """
    Decodes a stored session `.jsonl` trajectory into a structured, sanitized evidence record
    with tool-call arguments, tool errors, Oracle efficiency signals, and verification output.
    """
    tool_calls: list[dict[str, str]] = []
    tool_errors: list[dict[str, str]] = []
    assistant_turns = 0
    if attempt.session_jsonl_path:
        path = Path(attempt.session_jsonl_path)
        if path.is_file():
            try:
                for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    if not raw_line.strip():
                        continue
                    entry = json.loads(raw_line)
                    msg = entry.get("message") if isinstance(entry.get("message"), dict) else entry
                    if not isinstance(msg, dict):
                        continue
                    role = msg.get("role")
                    content = msg.get("content")
                    if role == "assistant":
                        assistant_turns += 1
                        if isinstance(content, list):
                            for item in content:
                                if isinstance(item, dict) and item.get("type") == "toolCall":
                                    name = str(item.get("name") or "")
                                    args = item.get("arguments") or item.get("input") or {}
                                    args_str = json.dumps(args)[:200] if args else ""
                                    if name:
                                        tool_calls.append({"tool": name, "args": args_str})
                    elif role == "toolResult" and msg.get("isError"):
                        tool_name = str(msg.get("toolName") or "tool")
                        err_text = _extract_tool_result_text(content)[:400]
                        tool_errors.append({"tool": tool_name, "error": err_text})
            except Exception:
                pass

    adjacent_edit_bash_pairs = sum(
        1
        for i in range(len(tool_calls) - 1)
        if tool_calls[i]["tool"] in ("edit", "write") and tool_calls[i + 1]["tool"] == "bash"
    )
    err_counts = Counter(e["tool"] for e in tool_errors)
    repeated_error_tools = sorted(t for t, cnt in err_counts.items() if cnt >= 2)

    return {
        "attempt_id": attempt.id,
        "task_id": attempt.task_id,
        "role": attempt.role,
        "outcome": attempt.outcome,
        "metric_value": attempt.metric_value,
        "summary": attempt.summary[:1000],
        "diff_stat": diff_stat[:1000],
        "verify_output": (attempt.verify_output or "")[:4000],
        "skills_used": list(attempt.skills_used),
        "diagnostic": (attempt.diagnostic or "")[:800],
        "session_jsonl_path": attempt.session_jsonl_path,
        "oracle": {
            "assistant_turns": assistant_turns,
            "adjacent_edit_bash_pairs": adjacent_edit_bash_pairs,
            "repeated_error_tools": repeated_error_tools,
        },
        "tool_calls": tool_calls[:40],
        "tool_errors": tool_errors[:15],
        "created_at": attempt.created_at,
    }


def validate_evidence_receipt(root_dir: Path, raw_output: str) -> dict[str, Any]:
    """
    SoL-Pi Evidence-Preserving Reducer / Scout validator:
    Parses a scout's JSON response (`{"summary": ..., "uncertain": bool, "evidence": [{"file": ..., "quote": ...}]}`)
    and verifies every quoted snippet byte-for-byte against the resolved file under `root_dir`.
    Any quote that is missing on disk or escapes `root_dir` is rejected.
    """
    text = raw_output.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    start = text.find("{")
    end = text.rfind("}")
    parsed: dict[str, Any] = {}
    if start >= 0 and end > start:
        try:
            candidate = json.loads(text[start : end + 1])
            if isinstance(candidate, dict):
                parsed = candidate
        except Exception:
            parsed = {}

    summary = (
        str(parsed.get("summary") or "No summary provided.")[:600]
        if parsed
        else "Unverified scout output rejected."
    )
    uncertain = bool(parsed.get("uncertain", True)) if parsed else True
    raw_items = parsed.get("evidence")
    items = raw_items if isinstance(raw_items, list) else []

    resolved_root = root_dir.resolve()
    verified: list[dict[str, Any]] = []
    rejected_count = 0
    seen: set[tuple[str, str]] = set()
    file_cache: dict[Path, str | None] = {}

    for item in items[:12]:
        if not isinstance(item, dict):
            rejected_count += 1
            continue
        rel_file = str(item.get("file") or "").strip()
        quote = str(item.get("quote") or "")
        if not rel_file or not quote or len(quote) > 800:
            rejected_count += 1
            continue
        if (rel_file, quote) in seen:
            continue
        try:
            target = (resolved_root / rel_file).resolve()
            if target != resolved_root and not str(target).startswith(f"{resolved_root}{os.sep}"):
                rejected_count += 1
                continue
            if target not in file_cache:
                if not target.is_file() or target.stat().st_size > 2_000_000:
                    file_cache[target] = None
                else:
                    file_cache[target] = target.read_text(encoding="utf-8", errors="replace")
            cached_body = file_cache[target]
            if cached_body is None:
                rejected_count += 1
                continue
            file_body = cached_body
        except Exception:
            rejected_count += 1
            continue

        idx = file_body.find(quote)
        if idx < 0:
            rejected_count += 1
            continue
        line_no = file_body.count("\n", 0, idx) + 1
        quote_sha = hashlib.sha256(quote.encode("utf-8")).hexdigest()[:16]
        seen.add((rel_file, quote))
        verified.append(
            {
                "file": str(target.relative_to(resolved_root)),
                "line": line_no,
                "quote_sha256": quote_sha,
                "quote": quote,
            }
        )

    if items and not verified:
        uncertain = True
        summary = "All cited quotes failed byte-for-byte verification; summary withheld."

    receipt_lines = [
        "sol_pi_evidence_receipt_v1",
        f"uncertain={str(uncertain).lower()}",
        f"verified_count={len(verified)}",
        f"rejected_unverifiable_count={rejected_count}",
        f"summary={json.dumps(summary)}",
        "verified_evidence:",
    ]
    if verified:
        for v in verified:
            receipt_lines.append(
                f"- file={v['file']}:{v['line']} quote_sha256={v['quote_sha256']} quote={json.dumps(v['quote'])}"
            )
    else:
        receipt_lines.append("- none")

    return {
        "schema": "sol_pi_evidence_receipt_v1",
        "summary": summary,
        "uncertain": uncertain,
        "verified_evidence": verified,
        "rejected_count": rejected_count,
        "receipt_text": "\n".join(receipt_lines),
    }


def _surface_for_trace(record: dict[str, Any]) -> str | None:
    """Which harness component a failed attempt points at; None for a run that went fine."""
    outcome = str(record.get("outcome") or "")
    if outcome in ("pushed", "decomposed"):
        return None
    oracle = record.get("oracle") or {}
    if oracle.get("repeated_error_tools"):
        return "tool interface"
    if outcome == "failed_verify":
        return "verify_cmd / gate"
    if outcome in ("timed_out", "errored"):
        return "runtime / prompt"
    if record.get("skills_used"):
        return "skill guidance"
    return "prompt / plan"


def mine_failure_patterns(
    traces_dir: Path,
    feedback_path: Path | None = None,
    *,
    limit: int = 40,
) -> list[dict[str, Any]]:
    """Clusters failed attempt traces and speech-check corrections by harness surface.

    The Improver gets counts and a few examples per surface instead of every raw trace, which keeps the
    proposal context small and points each edit at one component (AHE's experience observability).
    """
    groups: dict[str, list[str]] = {}
    for path in sorted(traces_dir.glob("attempt-*.json"))[-limit:]:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        surface = _surface_for_trace(record)
        if surface is None:
            continue
        detail = str(record.get("diagnostic") or record.get("summary") or "").strip()[:160]
        groups.setdefault(surface, []).append(f"attempt #{record.get('attempt_id')}: {detail}")
    # shortcut: the feedback file is re-read whole each run; corrections are rare, split by day if it grows.
    if feedback_path is not None and feedback_path.is_file():
        try:
            lines = feedback_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines[-limit:]:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict) or not record.get("correction_of"):
                continue
            signal = str(record.get("signal") or "correction")
            groups.setdefault("speech check", []).append(
                f"correction {record.get('correction_of')}: {signal}"
            )
    patterns: list[dict[str, Any]] = [
        {"surface": surface, "count": len(items), "evidence": items[-5:]}
        for surface, items in groups.items()
    ]
    patterns.sort(key=lambda p: int(p["count"]), reverse=True)
    return patterns


def render_failure_patterns(patterns: list[dict[str, Any]]) -> str:
    """One line per surface, with its freshest evidence under it."""
    if not patterns:
        return "(no failure patterns recorded)"
    return "\n".join(
        f"- {p['surface']} x{p['count']}:\n" + "\n".join(f"    {item}" for item in p["evidence"])
        for p in patterns
    )


def write_attempt_trace(
    traces_dir: Path, attempt: AttemptRow, *, diff_stat: str = ""
) -> Path:
    """Persists a layered file-system trace artifact (`traces/attempt-<id>.json`) for on-demand inspection."""
    traces_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    trace_path = traces_dir / f"attempt-{attempt.id}.json"
    record = inspect_attempt_trajectory(attempt, diff_stat=diff_stat)
    trace_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.chmod(trace_path, 0o600)
    return trace_path


def _extract_tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            str(item.get("text") or "").strip()
            for item in content
            if isinstance(item, dict) and item.get("text")
        ]
        return "\n".join(p for p in parts if p)
    return ""


def _write_skill_file(skills_dir: Path, slug: str, content: str) -> Path:
    target_dir = skills_dir / slug
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    skill_md = target_dir / "SKILL.md"
    skill_md.write_text(content, encoding="utf-8")
    os.chmod(skill_md, 0o600)
    return skill_md


def _remove_skill_dir(skills_dir: Path, slug: str) -> None:
    target_dir = skills_dir / slug
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
