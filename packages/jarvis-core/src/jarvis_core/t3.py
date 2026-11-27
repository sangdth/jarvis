"""T3 Code's own store: the thread snapshot `T3Runner` reads, and the user's T3 defaults."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

T3_HOME = Path.home() / ".t3"
# A thread in one of these is mid-turn, so it is never "the one to continue".
ACTIVE_STATUSES = frozenset({"preparing", "queued", "starting", "running", "waiting"})


def t3_defaults() -> dict[str, Any]:
    """The user's own T3 settings: the model and runtime mode a thread started in the T3 window gets.

    shortcut: ignores per-project overrides in T3 settings; read them here if Jarvis ever targets such a project.
    """
    try:
        return json.loads((T3_HOME / "userdata" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def find_project(projects: list[dict[str, Any]], cwd: str) -> dict[str, Any] | None:
    """The T3 project whose folder holds cwd; the deepest one when projects are nested."""
    folder = Path(cwd).expanduser().resolve()
    holding = [
        p for p in projects
        if p.get("workspaceRoot") and not p.get("archivedAt")
        and folder.is_relative_to(Path(p["workspaceRoot"]).expanduser().resolve())
    ]
    return max(holding, key=lambda p: len(p["workspaceRoot"]), default=None)


def recent_threads(snapshot: dict[str, Any], limit: int | None = 15) -> list[dict[str, Any]]:
    """Threads the user started in T3 (not imports or agent children), newest first, with their folder."""
    projects = {p["id"]: p for p in snapshot.get("projects", []) if p.get("workspaceRoot")}
    mine = [
        t for t in snapshot.get("threads", [])
        if t.get("createdBy") == "user" and not t.get("archivedAt") and t.get("projectId") in projects
    ]
    mine.sort(key=lambda t: t.get("updatedAt") or "", reverse=True)
    return [
        {
            "id": t["id"], "title": t.get("title") or "Untitled", "status": t.get("status") or "idle",
            "updatedAt": t.get("updatedAt") or "", "projectId": t["projectId"],
            "projectRoot": projects[t["projectId"]]["workspaceRoot"], "branch": t.get("branch") or "",
            "project": projects[t["projectId"]].get("title") or Path(projects[t["projectId"]]["workspaceRoot"]).name,
            "root": t.get("worktreePath") or projects[t["projectId"]]["workspaceRoot"],
            "worktree": bool(t.get("worktreePath")), "visitedAt": t.get("lastVisitedAt") or t.get("updatedAt") or "",
        }
        for t in mine[:limit]
    ]


def current_thread(snapshot: dict[str, Any], project_root: str, worktree_path: str | None) -> dict[str, Any] | None:
    """
    The thread a T3 project script runs in. T3 gives scripts only folders (T3CODE_PROJECT_ROOT, T3CODE_WORKTREE_PATH):
    a worktree belongs to its one thread; on the project root, take the thread viewed last, the one just clicked.
    """
    def same(a: str, b: str) -> bool:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()

    threads = recent_threads(snapshot, limit=None)
    if worktree_path:
        matches = [t for t in threads if t["worktree"] and same(t["root"], worktree_path)]
    else:
        matches = [t for t in threads if not t["worktree"] and same(t["root"], project_root)]
    return max(matches, key=lambda t: t["visitedAt"], default=None)
