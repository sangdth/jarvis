"""
Where Jarvis keeps its code and runtime state.

The repository checkout (`REPO_ROOT`) is the single source of truth and live runtime: editing code
takes effect immediately on `jarvis start` with no wheel build or tool reinstall. Runtime state,
databases (`brain/state.db`, `memory/`), secrets (`.env`, `brain_token`, `remote_token`), logs, and
models stay in `JARVIS_HOME` (defaulting to `REPO_ROOT`, overridable via `$JARVIS_HOME`) and are
gitignored so they are never committed.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from pathlib import Path

# Machine-wide IPC directory for fixed short paths shared across tools/worktrees (the one-mic lock and Pi's sockets).
IPC_HOME = Path("~/.config/jarvis").expanduser()
# Pre-merge standalone Brain state directory; migrated into `JARVIS_HOME/brain` on setup if present.
LEGACY_BRAIN_HOME = Path("~/.config/superbrain").expanduser()
BRAIN_DIRNAME = "brain"
BRAIN_TOKEN_FILE = "brain_token"


def workspace_root(start: Path | None = None) -> Path | None:
    """The repository checkout that holds this code: nearest ancestor with a uv workspace `pyproject.toml`."""
    for parent in (start or Path(__file__).resolve()).parents:
        pyproject = parent / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            text = pyproject.read_text(encoding="utf-8")
        except OSError:
            continue
        if "[tool.uv.workspace]" in text:
            return parent
    return None


REPO_ROOT: Path = workspace_root() or Path(__file__).resolve().parents[4]


def resolve_home() -> Path:
    """Runtime state home: `$JARVIS_HOME` when explicitly set, otherwise `REPO_ROOT`."""
    explicit = os.environ.get("JARVIS_HOME", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    return REPO_ROOT


JARVIS_HOME: Path = resolve_home()
BRAIN_TOKEN_PATH: Path = JARVIS_HOME / BRAIN_TOKEN_FILE


def git_version(repo: Path | None = None) -> str:
    """Live git branch and short commit SHA of the running codebase (e.g. `master @ 0fbc724` or `+dirty`)."""
    root = repo or REPO_ROOT
    try:
        sha_proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short=7", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )
        sha = sha_proc.stdout.strip()
        if sha_proc.returncode != 0 or not sha:
            return f"live ({root})"
        branch_proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )
        branch = branch_proc.stdout.strip() or "HEAD"
        dirty_proc = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )
        dirty = "+dirty" if dirty_proc.returncode == 0 and dirty_proc.stdout.strip() else ""
        return f"{branch} @ {sha}{dirty}"
    except OSError:
        return f"live ({root})"


def shared_dir(path: Path, *, mode: int = 0o700) -> Path:
    """Create `path` and restrict permissions (leaving the repo root's own directory mode intact)."""
    path.mkdir(parents=True, exist_ok=True)
    if path != REPO_ROOT:
        try:
            path.chmod(mode)
        except OSError:
            pass
    return path


def brain_home() -> Path:
    """The brain daemon's state folder: `$BRAIN_HOME` (or `$JARVIS_BRAIN_HOME`), else `JARVIS_HOME/brain`."""
    explicit = os.environ.get("JARVIS_BRAIN_HOME") or os.environ.get("BRAIN_HOME")
    if explicit and explicit.strip():
        return Path(explicit.strip()).expanduser()
    return JARVIS_HOME / BRAIN_DIRNAME


def env_file_value(name: str, *paths: Path) -> str:
    """A `NAME=value` line from the first file that sets it (before full pydantic settings load)."""
    for path in paths or (JARVIS_HOME / ".env",):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("export "):
                stripped = stripped.removeprefix("export ").strip()
            key, sep, value = stripped.partition("=")
            if sep and key.strip() == name and value.strip():
                return value.strip().strip('"').strip("'")
    return ""


def brain_token(home: Path | None = None) -> str:
    """The bearer token shared by the server, voice harness, dashboard, and thin clients; created on first use (0600)."""
    base = home or JARVIS_HOME
    b_home = brain_home() if home is None else (base / BRAIN_DIRNAME)
    token_path = base / BRAIN_TOKEN_FILE

    explicit = os.environ.get("JARVIS_TOKEN", "").strip() or os.environ.get("BRAIN_API_KEY", "").strip()
    if not explicit:
        explicit = os.environ.get("SUPERBRAIN_API_KEY", "").strip()
    if not explicit:
        explicit = env_file_value("JARVIS_TOKEN", base / ".env")
    if not explicit:
        explicit = env_file_value("BRAIN_API_KEY", base / ".env", b_home / ".env")
    if not explicit:
        explicit = env_file_value("SUPERBRAIN_API_KEY", b_home / ".env")
    if explicit:
        return explicit
    if token_path.is_file():
        token = token_path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    shared_dir(token_path.parent)
    token_path.write_text(f"{token}\n", encoding="utf-8")
    token_path.chmod(0o600)
    return token


def migrate_legacy_brain_home(home: Path | None = None) -> Path | None:
    """Move `~/.config/superbrain` into `JARVIS_HOME/brain`; returns the new folder, or None if nothing moved."""
    if not LEGACY_BRAIN_HOME.is_dir():
        return None
    base = home or JARVIS_HOME
    target = base / BRAIN_DIRNAME
    if target.exists():
        return None
    shared_dir(base)
    shutil.move(str(LEGACY_BRAIN_HOME), str(target))
    return target
