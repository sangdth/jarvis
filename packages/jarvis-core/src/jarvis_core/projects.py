"""The user's project folders: where coding runners may work, and how a spoken name maps to one.

This is the one place that answers "which folders are the user's projects?". It reads `projects_dir` from
`SOUL.md` (default `~/Projects`), lists git repositories one or two levels down, newest first, and resolves
a name the conversation model heard to a listed folder or nothing at all — a runner never picks an
arbitrary path.
"""

from __future__ import annotations

from pathlib import Path

from jarvis_core.soul import load_soul


def projects_dir() -> tuple[str, Path]:
    """The projects folder from SOUL.md, as written (for the model) and expanded (for the filesystem)."""
    label = load_soul().projects_dir
    return label, Path(label).expanduser()


def _last_touched(repo: Path) -> float:
    """When git last wrote the repo's index (commit, checkout, add, pull); a new repo has only `.git`."""
    git = repo / ".git"
    return (git / "index").stat().st_mtime if (git / "index").exists() else git.stat().st_mtime


def list_projects(root: Path | None = None, *, sort: bool = True) -> list[str]:
    """Git repos one or two folders under root, as relative paths ("HuggerHustle/platform-v2"), newest first."""
    base = projects_dir()[1] if root is None else root
    found: list[str] = []
    for top in sorted(base.iterdir()) if base.is_dir() else []:
        if top.name.startswith(".") or not top.is_dir():
            continue
        if (top / ".git").exists():
            found.append(top.name)
            continue
        try:
            found += [f"{top.name}/{sub.name}" for sub in sorted(top.iterdir()) if (sub / ".git").exists()]
        except OSError:
            continue  # unreadable folder: not a project a runner can open
    return sorted(found, key=lambda folder: _last_touched(base / folder), reverse=True) if sort else found


def is_listed_project(folder: Path) -> bool:
    """True when `folder` is one of the user's listed projects (or lives inside one)."""
    resolved = folder.expanduser().resolve()
    label, root = projects_dir()
    for rel in list_projects(root, sort=False):
        project = (root / rel).resolve()
        if resolved == project or resolved.is_relative_to(project):
            return True
    return False


def resolve_project(folder: str, root: Path | None = None) -> Path | None:
    """A listed project's folder for the name the model gave, else None.

    Case-blind, like the macOS file system, and tolerant of the `~/Projects/` prefix the model sometimes repeats.
    """
    label, default_root = projects_dir()
    base = default_root if root is None else root
    cleaned = folder.strip().strip("/")
    prefix = f"{label.strip('/')}/"
    if cleaned.lower().startswith(prefix.lower()):
        cleaned = cleaned[len(prefix):].strip("/")
    listed = {f.lower(): f for f in list_projects(base, sort=False)}.get(cleaned.lower())
    return base / listed if listed else None
