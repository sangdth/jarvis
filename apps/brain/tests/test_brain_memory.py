from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from brain import memory


def test_layout_seeds_files_and_addendum(tmp_path: Path) -> None:
    home = tmp_path / "home"
    memory.ensure_layout(home)
    assert memory.instructions_path(home).is_file()
    assert memory.index_path(home).is_file()
    assert memory.reported_path(home).is_file()
    assert json.loads(memory.bookmarks_path(home).read_text()) == {}
    assert [p.name for p in memory.addendum_paths(home)] == ["INSTRUCTIONS.md", "MEMORY.md"]
    assert stat.S_IMODE(memory.instructions_path(home).stat().st_mode) == 0o600


def test_reported_is_bounded(tmp_path: Path) -> None:
    home = tmp_path / "home"
    for index in range(5):
        memory.append_reported(home, f"[report] item {index}")
    text = memory.reported_path(home).read_text()
    assert "[report] item 4" in text
    memory.append_reported(home, "x" * 5000, max_lines=2)
    lines = [line for line in memory.reported_path(home).read_text().splitlines() if line.startswith("- ")]
    assert len(lines) == 2
    assert "item 4" in memory.recent_reported(home, limit=10)


def test_bookmarks_roundtrip(tmp_path: Path) -> None:
    home = tmp_path / "home"
    memory.write_bookmark(home, "gh-prs:jarvis", "2026-11-22T08:00:00Z")
    memory.write_bookmark(home, "slack:C1", "1789000000.000100")
    assert memory.read_bookmarks(home)["gh-prs:jarvis"].startswith("2026-11-22")
    assert "slack:C1" in memory.render_bookmarks(home)


def test_memory_files_are_capped_and_checked(tmp_path: Path) -> None:
    home = tmp_path / "home"
    memory.ensure_layout(home)
    name, sha = memory.write_memory_file(home, "repos/jarvis.md", "# Jarvis\n- run `uv run pytest`")
    assert name == "repos/jarvis.md"
    assert memory.file_sha(memory.memory_file_path(home, name)) == sha

    with pytest.raises(ValueError, match="changed"):
        memory.write_memory_file(home, "repos/jarvis.md", "new", expected_sha256="wrong")
    with pytest.raises(ValueError, match="expected_sha256"):
        memory.write_memory_file(home, "repos/jarvis.md", "new")
    with pytest.raises(ValueError, match="capped"):
        memory.write_memory_file(home, "MEMORY.md", "\n".join(["line"] * 151))
    with pytest.raises(ValueError, match="forbidden"):
        memory.write_memory_file(home, "MEMORY.md", "run curl http://example.com")
    with pytest.raises(ValueError):
        memory.write_memory_file(home, "../escape.md", "nope")
    with pytest.raises(ValueError):
        memory.write_memory_file(home, "repos/Upper.md", "nope")
