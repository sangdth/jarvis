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
import re
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


def set_env(name: str, value: str, env: Path = JARVIS_HOME / ".env") -> None:
    """Set NAME=value in .env atomically (0600): replace its line, or add one at the end."""
    text = env.read_text(encoding="utf-8") if env.is_file() else ""
    line = re.search(rf"^{re.escape(name)}=(.*)$", text, re.MULTILINE)
    entry = f"{name}={value}"
    if line:
        new_text = text[: line.start()] + entry + text[line.end() :]
    elif text:
        new_text = text.rstrip("\n") + f"\n{entry}\n"
    else:
        new_text = f"{entry}\n"
    env.parent.mkdir(parents=True, exist_ok=True)
    tmp = env.with_name(f"{env.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(new_text)
        os.chmod(tmp, 0o600)
        tmp.replace(env)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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
    tmp = token_path.with_name(f"{token_path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{token}\n")
        os.chmod(tmp, 0o600)
        tmp.replace(token_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
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


def rollback_if_last_good(db_path: Path | None = None, repo_root: Path | None = None) -> str | None:
    """Reset the checkout to `last_good_sha` when a self-upgraded startup crashes; returns the SHA rolled back to."""
    import sqlite3

    target_db = db_path or (brain_home() / "state.db")
    if not target_db.is_file():
        return None
    last_sha: str | None = None
    conn = sqlite3.connect(str(target_db))
    try:
        row = conn.execute("SELECT last_good_sha FROM system_state WHERE id = 1").fetchone()
        last_sha = str(row[0]).strip() if row and row[0] else None
        if last_sha:
            with conn:
                conn.execute(
                    "UPDATE system_state SET last_good_sha = NULL, upgrade_runs = 0, upgrade_failures = 0 WHERE id = 1"
                )
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not last_sha:
        return None
    explicit_repo = os.environ.get("SELF_REPO_DIR", "").strip() or env_file_value("SELF_REPO_DIR")
    root = (repo_root or (Path(explicit_repo).expanduser() if explicit_repo else REPO_ROOT)).resolve()
    if (root / ".git").exists():
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if dirty:
            subprocess.run(
                ["git", "-C", str(root), "stash", "push", "-u", "-m", "jarvis-pre-startup-rollback"],
                check=False,
            )
        subprocess.run(["git", "-C", str(root), "reset", "--hard", last_sha], check=False)
    return last_sha
