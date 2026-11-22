"""The brain's durable memory: a small injected index, per-repo notes, reported history, and bookmarks.

Three files are read at the start of every worker run (`INSTRUCTIONS.md`, `memory/MEMORY.md` and the task's
progress note) and one is written by the outbox dispatcher (`memory/reported.md`). Workers can only write
their task's progress note; the memory index and repo notes are curated by the offline Improver, which is
the "dreaming" pass: it checks notes against traces and live files and keeps the index small.

The injected layer must stay small. `MEMORY.md` is capped at 150 lines; details belong in
`memory/repos/<slug>.md`, which a worker reads only when it needs them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path

MEMORY_MAX_LINES = 150
REPO_NOTES_MAX_LINES = 80
REPORTED_MAX_LINES = 100
_BOOKMARK_MAX_KEYS = 200
_REPO_NAME_RE = re.compile(r"^repos/[a-z0-9][a-z0-9-]*\.md$")
# Two timer checks can finish at once; every read-modify-write of the memory files takes this lock.
_MEMORY_LOCK = threading.RLock()

DEFAULT_INSTRUCTIONS = """# Standing instructions for the brain

- Verify before claiming: run the check, read the output, and quote what you actually saw.
- Keep edits inside the task worktree; never push to `master` or a protected branch.
- Ask before touching credentials, production data, or anything outside the task.
- Keep the memory index short. Put repository details in `memory/repos/<repo>.md`.
"""

DEFAULT_MEMORY = """# Brain memory index

Standing facts, verified commands, and pointers to `repos/<slug>.md`. Keep this under 150 lines.
"""

FORBIDDEN_PATTERNS = (
    re.compile(r"BRAIN_RPC_SOCK", re.IGNORECASE),
    re.compile(r"\bstate\.db\b", re.IGNORECASE),
    re.compile(r"\b(?:curl|wget|nc|netcat)\s+https?://", re.IGNORECASE),
    re.compile(r"\bbase64\s+(?:-d|--decode)\b", re.IGNORECASE),
    re.compile(r"\bgit\s+push\s+.*--force\b", re.IGNORECASE),
    re.compile(r"\brm\s+-[a-z]*r[a-z]*f\s+\/(?:\s|$)", re.IGNORECASE),
)


def memory_dir(home_dir: Path) -> Path:
    return home_dir / "memory"


def instructions_path(home_dir: Path) -> Path:
    return home_dir / "INSTRUCTIONS.md"


def index_path(home_dir: Path) -> Path:
    return memory_dir(home_dir) / "MEMORY.md"


def reported_path(home_dir: Path) -> Path:
    return memory_dir(home_dir) / "reported.md"


def bookmarks_path(home_dir: Path) -> Path:
    return memory_dir(home_dir) / "bookmarks.json"


def repos_dir(home_dir: Path) -> Path:
    return memory_dir(home_dir) / "repos"


def ensure_layout(home_dir: Path) -> None:
    """Create the memory folders and the two seed files once; never overwrite owner edits."""
    with _MEMORY_LOCK:
        for folder in (home_dir, memory_dir(home_dir), repos_dir(home_dir)):
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(folder, 0o700)
        for path, seed in (
            (instructions_path(home_dir), DEFAULT_INSTRUCTIONS),
            (index_path(home_dir), DEFAULT_MEMORY),
        ):
            if not path.exists():
                _atomic_write(path, seed)
        if not reported_path(home_dir).exists():
            _atomic_write(reported_path(home_dir), "# Reported to the owner\n\n")
        if not bookmarks_path(home_dir).exists():
            _atomic_write(bookmarks_path(home_dir), "{}\n")


def addendum_paths(home_dir: Path) -> list[Path]:
    """The files passed to `pi --append-system-prompt`, in order, only when they exist and have content."""
    ensure_layout(home_dir)
    return [path for path in (instructions_path(home_dir), index_path(home_dir)) if path.is_file()]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha(path: Path) -> str | None:
    return sha256_text(path.read_text(encoding="utf-8")) if path.is_file() else None


def _trim_lines(text: str, limit: int) -> str:
    lines = text.rstrip().splitlines()
    if len(lines) <= limit:
        return "\n".join(lines) + "\n"
    return "\n".join(lines[:limit]).rstrip() + "\n"


def append_reported(home_dir: Path, line: str, *, max_lines: int = REPORTED_MAX_LINES) -> None:
    """One line per outbox item the voice has taken; bounded so it stays readable in every run."""
    clean = " ".join(str(line).split())
    if not clean:
        return
    ensure_layout(home_dir)
    path = reported_path(home_dir)
    with _MEMORY_LOCK:
        existing = path.read_text(encoding="utf-8").splitlines()
        header = existing[:2] if existing and existing[0].startswith("#") else ["# Reported to the owner", ""]
        entries = [line for line in existing if line.startswith("- ")]
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
        entries.append(f"- {stamp} {clean[:400]}")
        entries = entries[-max(1, max_lines):]
        _atomic_write(path, "\n".join([*header, *entries]).rstrip() + "\n")


def read_bookmarks(home_dir: Path) -> dict[str, str]:
    ensure_layout(home_dir)
    try:
        raw = json.loads(bookmarks_path(home_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, (str, int, float))}


def write_bookmark(home_dir: Path, source: str, cursor: str) -> dict[str, str]:
    key = " ".join(str(source).split())[:120]
    value = " ".join(str(cursor).split())[:120]
    if not key or not value:
        raise ValueError("a bookmark needs a source and a cursor")
    with _MEMORY_LOCK:
        bookmarks = read_bookmarks(home_dir)
        bookmarks[key] = value
        if len(bookmarks) > _BOOKMARK_MAX_KEYS:
            bookmarks = dict(list(bookmarks.items())[-_BOOKMARK_MAX_KEYS:])
        _atomic_write(bookmarks_path(home_dir), json.dumps(bookmarks, indent=2, sort_keys=True) + "\n")
        return bookmarks


def render_bookmarks(home_dir: Path) -> str:
    bookmarks = read_bookmarks(home_dir)
    if not bookmarks:
        return "(no bookmarks yet)"
    return "\n".join(f"- {source}: {cursor}" for source, cursor in sorted(bookmarks.items()))


def recent_reported(home_dir: Path, limit: int = 20) -> str:
    """The last lines the voice already took, so a check does not report the same open item twice."""
    ensure_layout(home_dir)
    lines = [
        line for line in reported_path(home_dir).read_text(encoding="utf-8").splitlines()
        if line.startswith("- ")
    ]
    return "\n".join(lines[-max(1, limit):]) or "(nothing reported yet)"


def memory_file_path(home_dir: Path, name: str) -> Path:
    """`MEMORY.md` or `repos/<slug>.md`, and nothing else."""
    clean = name.strip()
    if clean == "MEMORY.md":
        return index_path(home_dir)
    if _REPO_NAME_RE.match(clean):
        return memory_dir(home_dir) / clean
    raise ValueError("memory name must be MEMORY.md or repos/<slug>.md")


def memory_line_limit(name: str) -> int:
    return MEMORY_MAX_LINES if name.strip() == "MEMORY.md" else REPO_NOTES_MAX_LINES


def validate_memory_text(name: str, content: str) -> str:
    """Reject secrets and escapes, and enforce the cap before anything reaches disk."""
    clean = content.strip()
    if not clean:
        raise ValueError("a memory update cannot be empty")
    for pattern in FORBIDDEN_PATTERNS:
        if pattern.search(clean):
            raise ValueError(f"memory update rejected: forbidden pattern {pattern.pattern!r}")
    limit = memory_line_limit(name)
    if len(clean.splitlines()) > limit:
        raise ValueError(f"{name} is capped at {limit} lines; move detail into repos/<slug>.md")
    return _trim_lines(clean, limit)


def write_memory_file(
    home_dir: Path,
    name: str,
    content: str,
    *,
    expected_sha256: str | None = None,
) -> tuple[str, str]:
    """Write one curated memory file; returns `(name, new_sha256)`.

    `expected_sha256` is optimistic concurrency: the Improver saw a version, and a mismatch means the file
    changed under it, so the write is refused rather than silently clobbering newer notes.
    """
    ensure_layout(home_dir)
    path = memory_file_path(home_dir, name)
    text = validate_memory_text(name, content)
    with _MEMORY_LOCK:
        current = file_sha(path)
        if current is not None and not expected_sha256:
            raise ValueError(f"{name} already exists; pass its expected_sha256 before overwriting it")
        if expected_sha256 and expected_sha256 != current:
            raise ValueError(f"{name} changed since it was read; re-read it before writing")
        _atomic_write(path, text)
    return name, sha256_text(text)


def memory_index(home_dir: Path) -> str:
    ensure_layout(home_dir)
    return index_path(home_dir).read_text(encoding="utf-8")


def repo_note_files(home_dir: Path) -> list[Path]:
    ensure_layout(home_dir)
    return sorted(repos_dir(home_dir).glob("*.md"))


def repo_slug(repo: str) -> str:
    """`HuggerHustle/platform-v2` -> `huggerhustle-platform-v2` (one safe file per repository)."""
    slug = re.sub(r"[^a-z0-9]+", "-", repo.lower()).strip("-")
    return slug or "unknown"
