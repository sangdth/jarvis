from __future__ import annotations

import shutil
import sys
from pathlib import Path


def _escape_sb_path(path: Path) -> str:
    resolved = str(path.resolve())
    return resolved.replace("\\", "\\\\").replace('"', '\\"')


def build_sandbox_profile(worktree_dir: Path, session_dir: Path, notes_dir: Path) -> str:
    """
    macOS Seatbelt (`sandbox-exec`) profile for Builder workers.
    Allows reading the system and executing tools, but restricts file-write* operations strictly to:
    - the isolated task worktree (`worktree_dir`)
    - the worker's session/socket directory (`session_dir`)
    - the progress notes directory (`notes_dir`)
    - temporary directories (`/tmp`, `/private/tmp`, `/private/var/folders`) and `/dev`
    Sibling repositories under ~/Projects (such as `hamahakki`) and `state.db` cannot be modified.
    """
    wt = _escape_sb_path(worktree_dir)
    sess = _escape_sb_path(session_dir)
    notes = _escape_sb_path(notes_dir)
    return "\n".join(
        [
            "(version 1)",
            "(allow default)",
            "(deny file-write*)",
            '(allow file-write* (subpath "/dev"))',
            '(allow file-write* (subpath "/tmp"))',
            '(allow file-write* (subpath "/private/tmp"))',
            '(allow file-write* (subpath "/private/var/folders"))',
            f'(allow file-write* (subpath "{wt}"))',
            f'(allow file-write* (subpath "{sess}"))',
            f'(allow file-write* (subpath "{notes}"))',
            "",
        ]
    )


def wrap_with_sandbox(
    cmd: list[str],
    *,
    worktree_dir: Path,
    session_dir: Path,
    notes_dir: Path,
    enabled: bool = True,
) -> list[str]:
    """Wraps `cmd` in `sandbox-exec -f <profile.sb>` on macOS when enabled."""
    if not enabled or sys.platform != "darwin" or shutil.which("sandbox-exec") is None:
        return cmd
    session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile_path = session_dir / "sandbox.sb"
    profile_path.write_text(
        build_sandbox_profile(worktree_dir, session_dir, notes_dir), encoding="utf-8"
    )
    return ["sandbox-exec", "-f", str(profile_path), *cmd]
