from __future__ import annotations

import enum
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from superbrain.config import settings
from superbrain.db import BrainDB

LAUNCHD_LABEL = "com.sangdth.jarvis"
# Python deps: `uv sync`. Rust deps: the cockpit is rebuilt, but its absence never blocks the daemon.
DEPENDENCY_FILES = ("pyproject.toml", "uv.lock")
RUST_FILES = ("Cargo.toml", "Cargo.lock")
COCKPIT_MANIFEST = ("apps", "tui", "Cargo.toml")


class GitState(enum.Enum):
    """Relationship between the running checkout's HEAD and `origin/master`."""

    UP_TO_DATE = "up_to_date"
    FAST_FORWARD = "fast_forward"
    LOCAL_AHEAD = "local_ahead"
    DIVERGED = "diverged"


def classify_git_state(
    local_sha: str | None, origin_sha: str | None, merge_base: str | None
) -> GitState:
    """
    Pure git-topology classifier.

    `merge_base` is the output of `git merge-base HEAD origin/master` (or `None` when the two
    histories are unrelated / cannot be compared). Callers must pass full commit SHAs.
    """
    if origin_sha is None:
        # No remote-tracking branch to follow: there is nothing to pull.
        return GitState.UP_TO_DATE
    if local_sha is None:
        # Unborn local branch with a known remote: adopt `origin/master` wholesale.
        return GitState.FAST_FORWARD
    if local_sha == origin_sha:
        return GitState.UP_TO_DATE
    if merge_base is None:
        return GitState.DIVERGED
    if merge_base == local_sha:
        return GitState.FAST_FORWARD
    if merge_base == origin_sha:
        return GitState.LOCAL_AHEAD
    return GitState.DIVERGED


@dataclass(frozen=True)
class RepoSnapshot:
    """Read-only view of a checkout's git state relative to `origin/master`."""

    repo_dir: Path
    local_sha: str | None
    origin_sha: str | None
    merge_base: str | None
    ahead: int | None
    behind: int | None
    dirty_entries: tuple[str, ...]
    deps_changed: tuple[str, ...]

    @property
    def state(self) -> GitState:
        return classify_git_state(self.local_sha, self.origin_sha, self.merge_base)

    @property
    def fast_forward_possible(self) -> bool:
        return self.state is GitState.FAST_FORWARD


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _rev_parse(cwd: Path, ref: str) -> str | None:
    proc = _run_git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _short(sha: str | None) -> str:
    return sha[:12] if sha else "(missing)"


def read_snapshot(repo_dir: Path) -> RepoSnapshot:
    """Collects everything `--check` reports without mutating the checkout."""
    local_sha = _rev_parse(repo_dir, "HEAD")
    origin_sha = _rev_parse(repo_dir, "origin/master")
    merge_base: str | None = None
    ahead: int | None = None
    behind: int | None = None
    deps_changed: tuple[str, ...] = ()

    if local_sha and origin_sha:
        mb = _run_git(repo_dir, "merge-base", "HEAD", "origin/master")
        if mb.returncode == 0:
            merge_base = mb.stdout.strip() or None
        counts = _run_git(repo_dir, "rev-list", "--left-right", "--count", "HEAD...origin/master")
        if counts.returncode == 0:
            parts = counts.stdout.split()
            if len(parts) == 2:
                ahead, behind = int(parts[0]), int(parts[1])
        base = merge_base or local_sha
        diff = _run_git(
            repo_dir,
            "diff",
            "--name-only",
            base,
            origin_sha,
            "--",
            *DEPENDENCY_FILES,
            *RUST_FILES,
        )
        if diff.returncode == 0:
            deps_changed = tuple(line.strip() for line in diff.stdout.splitlines() if line.strip())

    dirty = _run_git(repo_dir, "status", "--porcelain")
    dirty_entries = tuple(
        line for line in dirty.stdout.splitlines() if line.strip()
    ) if dirty.returncode == 0 else ()

    return RepoSnapshot(
        repo_dir=repo_dir,
        local_sha=local_sha,
        origin_sha=origin_sha,
        merge_base=merge_base,
        ahead=ahead,
        behind=behind,
        dirty_entries=dirty_entries,
        deps_changed=deps_changed,
    )


def _print_report(snapshot: RepoSnapshot, *, refreshed: bool) -> None:
    state = snapshot.state
    if snapshot.fast_forward_possible:
        ff_note = "yes"
    elif state is GitState.UP_TO_DATE:
        ff_note = "no (already up to date)"
    elif state is GitState.LOCAL_AHEAD:
        ff_note = "no (local HEAD has unpublished commits)"
    else:
        ff_note = "no (diverged; manual resolution required)"

    origin_note = "refreshed just now" if refreshed else "as of last fetch; pass --fetch to refresh"
    ahead = snapshot.ahead if snapshot.ahead is not None else "-"
    behind = snapshot.behind if snapshot.behind is not None else "-"
    if snapshot.dirty_entries:
        tree = f"dirty ({len(snapshot.dirty_entries)} path(s))"
    else:
        tree = "clean"
    deps = ", ".join(snapshot.deps_changed) if snapshot.deps_changed else "none"

    print(f"Upgrade check for {snapshot.repo_dir}")
    print(f"  local HEAD:              {_short(snapshot.local_sha)}")
    print(f"  origin/master:           {_short(snapshot.origin_sha)} ({origin_note})")
    print(f"  ahead / behind:          {ahead} / {behind}")
    print(f"  working tree:            {tree}")
    print(f"  dependency files changed: {deps}")
    print(f"  fast-forward possible:   {ff_note}")
    print(f"  state:                   {state.value}")


def _divergence_message(snapshot: RepoSnapshot) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return (
        f"Local HEAD {_short(snapshot.local_sha)} and origin/master "
        f"{_short(snapshot.origin_sha)} have diverged: both contain unique commits.\n"
        "Refusing to merge, force, or reset the checkout; never forcing history. Inspect the two sides with:\n"
        f"  git -C {snapshot.repo_dir} log --oneline --left-right --graph HEAD...origin/master\n"
        "Suggested remedy (saves your local commits to a backup branch first):\n"
        f"  git -C {snapshot.repo_dir} branch backup/{stamp} HEAD\n"
        f"  git -C {snapshot.repo_dir} rebase origin/master\n"
        "Then re-run: uv run superbrain upgrade\n"
        "(or, if the local commits are disposable: git reset --hard origin/master)"
    )


def run_uv_sync(repo_dir: Path) -> int:
    """Runs `uv sync` after a dependency-file change; returns the process exit code."""
    uv_exe = shutil.which("uv")
    if uv_exe is None:
        print("Dependency files changed but `uv` is not on PATH; run `uv sync` manually before restarting.")
        return 127
    print("Dependency files changed; running `uv sync` ...")
    proc = subprocess.run([uv_exe, "sync"], cwd=str(repo_dir), check=False)
    return proc.returncode


def run_cargo_build(repo_dir: Path) -> int:
    """Rebuilds the cockpit after a Rust dependency change; a missing toolchain only warns."""
    cargo = shutil.which("cargo")
    if cargo is None:
        print("Rust dependency files changed, but `cargo` is not on PATH; the cockpit keeps its old build.")
        return 127
    print("Rust dependency files changed; rebuilding the cockpit ...")
    proc = subprocess.run(
        [cargo, "build", "--release", "--manifest-path", str(repo_dir.joinpath(*COCKPIT_MANIFEST))],
        cwd=str(repo_dir),
        check=False,
    )
    if proc.returncode != 0:
        logger.error(f"cargo build failed in {repo_dir} (exit {proc.returncode}); the daemon still restarts.")
        print("The cockpit build failed; `jarvis top` will use the previous binary. Fix it when you can.")
    return proc.returncode


def restart_daemon() -> int:
    """
    Restarts the launchd-managed daemon on macOS. Elsewhere (or when the agent is not
    loaded) prints the manual instruction and returns 0 because an exiting supervisor
    with `KeepAlive` respawns the process on its own.
    """
    if sys.platform != "darwin" or shutil.which("launchctl") is None:
        print(
            "Restart the daemon manually (stop the running process); launchd KeepAlive "
            "respawns it cleanly."
        )
        return 0
    domain = f"gui/{os.getuid()}/{LAUNCHD_LABEL}"
    loaded = subprocess.run(
        ["launchctl", "print", domain], capture_output=True, text=True, check=False
    )
    if loaded.returncode != 0:
        print(
            f"LaunchAgent {LAUNCHD_LABEL} is not loaded in {domain}; restart the daemon manually:\n"
            f"  uv run superbrain launchd --install   # or stop the running process for KeepAlive"
        )
        return 0
    kick = subprocess.run(
        ["launchctl", "kickstart", "-k", domain], capture_output=True, text=True, check=False
    )
    if kick.returncode != 0:
        logger.error(f"launchctl kickstart -k {domain} failed: {kick.stderr.strip()}")
        print(
            f"launchctl kickstart -k {domain} failed: {kick.stderr.strip()}\n"
            "Restart the daemon manually."
        )
        return 1
    print(f"Restarted {LAUNCHD_LABEL} via `launchctl kickstart -k {domain}`.")
    return 0


def run_upgrade(
    *,
    repo_dir: Path | None = None,
    check: bool = False,
    fetch: bool = False,
    stash: bool = False,
    restart: bool = True,
    db: BrainDB | None = None,
) -> int:
    """
    Fast-forwards this checkout to `origin/master`, syncs dependencies when they changed,
    and restarts the daemon. Refuses on a dirty tree (unless `stash=True`) and on divergence.
    `check=True` only reports; it never changes HEAD, the index, or the worktree.
    """
    root = (repo_dir or settings.self_repo_dir).resolve()
    if not (root / ".git").exists():
        logger.error(f"Not a git checkout: {root}")
        print(f"Not a git checkout: {root}")
        return 1

    remotes = _run_git(root, "remote")
    has_origin = remotes.returncode == 0 and "origin" in remotes.stdout.splitlines()

    if has_origin and (fetch or not check):
        fetched = _run_git(root, "fetch", "origin", "--prune")
        if fetched.returncode != 0:
            detail = (fetched.stderr or fetched.stdout).strip()
            logger.error(f"git fetch origin --prune failed in {root}")
            print(f"git fetch origin --prune failed in {root}: {detail}")
            return 1
    elif not has_origin:
        print(f"No `origin` remote in {root}; nothing to follow.")

    snapshot = read_snapshot(root)
    if check:
        _print_report(snapshot, refreshed=has_origin and fetch)
        return 0

    state = snapshot.state
    if state is GitState.UP_TO_DATE:
        print(f"Already up to date with origin/master ({_short(snapshot.local_sha)}).")
        return 0
    if state is GitState.LOCAL_AHEAD:
        print(
            f"Local HEAD {_short(snapshot.local_sha)} is ahead of origin/master "
            f"{_short(snapshot.origin_sha)} and has unpublished commits; nothing to update.\n"
            "Push or reset those commits deliberately; this command never rewrites history."
        )
        return 0
    if state is GitState.DIVERGED:
        logger.error(
            f"Refusing upgrade for {root}: local HEAD {_short(snapshot.local_sha)} and "
            f"origin/master {_short(snapshot.origin_sha)} have diverged."
        )
        print(_divergence_message(snapshot))
        return 1

    if snapshot.dirty_entries and not stash:
        listing = "\n".join(f"  {line}" for line in snapshot.dirty_entries)
        print(
            "Refusing to upgrade: the working tree has uncommitted changes:\n"
            f"{listing}\n"
            "Commit them, or re-run with --stash to auto-stash them "
            "(recover with `git stash list` / `git stash pop`)."
        )
        return 1

    owned_db = db is None
    active_db = db
    if active_db is None:
        active_db = BrainDB(
            db_path=settings.db_path,
            allowed_repos=settings.allowed_repo_set,
            default_mode=settings.default_mode,
            daily_budget_usd=settings.daily_budget_usd,
        )
    try:
        old_head = snapshot.local_sha
        if old_head is not None:
            # Record the rollback anchor before touching the worktree or HEAD.
            active_db.set_last_good_sha(old_head)

        if snapshot.dirty_entries:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            stash_message = f"superbrain-upgrade-{stamp}"
            stashed = _run_git(root, "stash", "push", "-u", "-m", stash_message)
            if stashed.returncode != 0:
                detail = (stashed.stderr or stashed.stdout).strip()
                active_db.clear_last_good_sha()
                logger.error(f"git stash push failed in {root}: {detail}")
                print(f"git stash push failed in {root}: {detail}")
                return 1
            print(
                f"Stashed local changes as {stash_message!r}; recover with "
                "`git stash list` / `git stash pop`."
            )

        merged = _run_git(root, "merge", "--ff-only", "origin/master")
        if merged.returncode != 0:
            detail = (merged.stderr or merged.stdout).strip()
            active_db.clear_last_good_sha()
            logger.error(f"git merge --ff-only origin/master failed in {root}: {detail}")
            print(f"git merge --ff-only origin/master failed in {root}: {detail}")
            return 1

        new_head = _rev_parse(root, "HEAD") or ""
        wanted = [*DEPENDENCY_FILES, *RUST_FILES]
        if old_head is None:
            deps = list(wanted)
        else:
            dep_diff = _run_git(root, "diff", "--name-only", old_head, new_head, "--", *wanted)
            deps = (
                [line.strip() for line in dep_diff.stdout.splitlines() if line.strip()]
                if dep_diff.returncode == 0
                else []
            )
        print(f"Fast-forwarded {_short(old_head)} -> {_short(new_head)}.")
        python_deps = [name for name in deps if name in DEPENDENCY_FILES]
        if python_deps:
            print(f"Python dependency files changed: {', '.join(python_deps)}")
            sync_rc = run_uv_sync(root)
            if sync_rc != 0:
                logger.error(f"uv sync failed in {root} (exit {sync_rc}); not restarting the daemon.")
                print(
                    f"uv sync failed (exit {sync_rc}); fix it and restart manually. "
                    f"Rollback anchor remains {_short(old_head)} in system_state.last_good_sha."
                )
                return 1
        else:
            print("Python dependency files unchanged; skipping `uv sync`.")
        if any(name in RUST_FILES for name in deps):
            run_cargo_build(root)

        if not restart:
            print("Updated checkout without restarting (--no-restart).")
            return 0
        return restart_daemon()
    finally:
        if owned_db:
            active_db.close()
