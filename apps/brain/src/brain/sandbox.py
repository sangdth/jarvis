from __future__ import annotations

import shutil
import sys
from pathlib import Path


def _escape_sb_path(path: Path) -> str:
    resolved = str(path.resolve())
    return resolved.replace("\\", "\\\\").replace('"', '\\"')


def _resolve_worktree_gitdir(worktree_dir: Path) -> Path | None:
    dot_git = worktree_dir / ".git"
    if not dot_git.is_file():
        return None
    try:
        text = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if text.startswith("gitdir:"):
        raw = text.removeprefix("gitdir:").strip()
        return (worktree_dir / raw).resolve()
    return None


def build_sandbox_profile(
    worktree_dir: Path,
    session_dir: Path,
    notes_dir: Path,
    *,
    home_dir: Path | None = None,
    allow_read_traces: bool = False,
) -> str:
    """
    macOS Seatbelt (`sandbox-exec`) profile for Brain workers.
    Allows reading the system and executing tools, but restricts file-write* operations strictly to:
    - the isolated task worktree (`worktree_dir`) and its `.git/worktrees/<id>` metadata dir
    - the worker's session/socket/tmp directory (`session_dir`)
    - the progress notes directory (`notes_dir`)
    - `/tmp`, `/private/tmp`, and `/dev`
    Also blocks reading sensitive secrets (`~/.ssh`, `<home_dir>/.env`, `<home_dir>/state.db*`,
    and `<home_dir>/traces` unless `allow_read_traces=True` for the offline Improver role).
    Sibling repositories under ~/Projects (such as `hamahakki`) and `state.db` cannot be modified.
    """
    wt = _escape_sb_path(worktree_dir)
    sess = _escape_sb_path(session_dir)
    notes = _escape_sb_path(notes_dir)
    lines = [
        "(version 1)",
        "(allow default)",
        '(deny file-write* (subpath "/"))',
        '(allow file-write* (subpath "/dev"))',
        '(allow file-write* (subpath "/tmp"))',
        '(allow file-write* (subpath "/private/tmp"))',
        f'(allow file-write* (subpath "{wt}"))',
        f'(allow file-write* (subpath "{sess}"))',
        f'(allow file-write* (subpath "{notes}"))',
    ]
    wt_gitdir = _resolve_worktree_gitdir(worktree_dir)
    if wt_gitdir is not None:
        lines.append(f'(allow file-write* (subpath "{_escape_sb_path(wt_gitdir)}"))')
    ssh_dir = Path("~/.ssh").expanduser()
    if ssh_dir.exists():
        lines.append(f'(deny file-read-data (subpath "{_escape_sb_path(ssh_dir)}"))')
    if home_dir is not None:
        env_file = home_dir / ".env"
        lines.append(f'(deny file-read-data (literal "{_escape_sb_path(env_file)}"))')
        for db_name in ("state.db", "state.db-wal", "state.db-shm", "state.db-journal"):
            lines.append(f'(deny file-read-data (literal "{_escape_sb_path(home_dir / db_name)}"))')
        if not allow_read_traces:
            traces_dir = home_dir / "traces"
            lines.append(f'(deny file-read-data (subpath "{_escape_sb_path(traces_dir)}"))')
    lines.append("")
    return "\n".join(lines)


def wrap_with_sandbox(    cmd: list[str],
    *,
    worktree_dir: Path,
    session_dir: Path,
    notes_dir: Path,
    home_dir: Path | None = None,
    allow_read_traces: bool = False,
    enabled: bool = True,
) -> list[str]:
    """Wraps `cmd` in `sandbox-exec -f <profile.sb>` on macOS when enabled."""
    if not enabled or sys.platform != "darwin" or shutil.which("sandbox-exec") is None:
        return cmd
    session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile_path = session_dir / "sandbox.sb"
    profile_path.write_text(
        build_sandbox_profile(
            worktree_dir,
            session_dir,
            notes_dir,
            home_dir=home_dir,
            allow_read_traces=allow_read_traces,
        ),
        encoding="utf-8",
    )
    return ["sandbox-exec", "-f", str(profile_path), *cmd]


def build_ephemeral_sandbox_profile(work_dir: Path, *, home_dir: Path | None = None) -> str:
    """Seatbelt profile for a one-shot `pi` answer: writes only in its own temp dir, reads no secrets.

    It may read the repository and the network (a lookup needs both), but it cannot change anything outside
    `work_dir`; the temp dir is deleted as soon as the answer is in.
    """
    work = _escape_sb_path(work_dir)
    lines = [
        "(version 1)",
        "(allow default)",
        '(deny file-write* (subpath "/"))',
        '(allow file-write* (subpath "/dev"))',
        '(allow file-write* (subpath "/tmp"))',
        '(allow file-write* (subpath "/private/tmp"))',
        f'(allow file-write* (subpath "{work}"))',
    ]
    ssh_dir = Path("~/.ssh").expanduser()
    if ssh_dir.exists():
        lines.append(f'(deny file-read-data (subpath "{_escape_sb_path(ssh_dir)}"))')
    if home_dir is not None:
        lines.append(f'(deny file-read-data (literal "{_escape_sb_path(home_dir / ".env")}"))')
        for db_name in ("state.db", "state.db-wal", "state.db-shm", "state.db-journal"):
            lines.append(f'(deny file-read-data (literal "{_escape_sb_path(home_dir / db_name)}"))')
        lines.append(f'(deny file-read-data (subpath "{_escape_sb_path(home_dir / "traces")}"))')
    lines.append("")
    return "\n".join(lines)


def wrap_ephemeral(
    cmd: list[str],
    *,
    work_dir: Path,
    home_dir: Path | None = None,
    enabled: bool = True,
) -> list[str]:
    """Wrap a one-shot worker in a temp-dir-only Seatbelt profile on macOS."""
    if not enabled or sys.platform != "darwin" or shutil.which("sandbox-exec") is None:
        return cmd
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile_path = work_dir / "sandbox.sb"
    profile_path.write_text(
        build_ephemeral_sandbox_profile(work_dir, home_dir=home_dir),
        encoding="utf-8",
    )
    return ["sandbox-exec", "-f", str(profile_path), *cmd]
