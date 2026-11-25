"""Local model checkpoints: where a promoted weight update lives, and how it is rolled back in one step.

A checkpoint is an immutable directory under `JARVIS_HOME/models/<target>/v<N>-<stamp>/`, reached through the
`current` symlink; the one it replaced stays as `previous`, so `rollback` is a symlink swap and a restart.
Nothing here trains anything: the pipelines live in `scripts/voice/train.py` and the `laya` repo.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

TARGETS = ("whisper-owner-lora", "laya-intent", "router-distill")
LAYA_CORRECTIONS_MIN = 25  # new corrections before the intent model is worth retraining
ROUTER_CLEAN_RUNS_MIN = 500  # clean router turns before a distilled router has anything to learn from
WHISPER_CLIPS_MIN = 20  # reviewed owner clips since the last checkpoint


def target_dir(home_dir: Path, target: str) -> Path:
    if target not in TARGETS:
        raise ValueError(f"unknown weight target {target!r}: expected one of {', '.join(TARGETS)}")
    return home_dir / "models" / target


def current(home_dir: Path, target: str) -> Path | None:
    link = target_dir(home_dir, target) / "current"
    return link.resolve() if link.is_symlink() else None


def promote(home_dir: Path, target: str, checkpoint: Path) -> Path:
    """Point `current` at a verified checkpoint, keeping whatever it replaced as `previous`."""
    folder = target_dir(home_dir, target)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not checkpoint.is_dir():
        raise ValueError(f"checkpoint {checkpoint} does not exist")
    link, previous = folder / "current", folder / "previous"
    if link.is_symlink():
        if previous.exists() or previous.is_symlink():
            previous.unlink()
        previous.symlink_to(link.resolve(), target_is_directory=True)
    tmp = folder / ".current.tmp"
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(checkpoint.resolve(), target_is_directory=True)
    tmp.replace(link)
    logger.info(f"[Weights] {target}: current -> {checkpoint.name}")
    return checkpoint


def rollback(home_dir: Path, target: str) -> Path | None:
    """Swap `current` back to `previous`; None when there is nothing to go back to."""
    folder = target_dir(home_dir, target)
    link, previous = folder / "current", folder / "previous"
    if not previous.is_symlink() or not previous.resolve().is_dir():
        return None
    restored = previous.resolve()
    replaced = link.resolve() if link.is_symlink() else None
    if replaced is not None:
        marker = folder / ".previous.tmp"
        if marker.exists() or marker.is_symlink():
            marker.unlink()
        marker.symlink_to(replaced, target_is_directory=True)
        marker.replace(previous)
    tmp = folder / ".current.tmp"
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(restored, target_is_directory=True)
    tmp.replace(link)
    logger.info(f"[Weights] {target}: rolled back to {restored.name}")
    return restored


def laya_intent_ready(
    home_dir: Path, *, bookmark_cursor: int = 0, feedback_path: Path | None = None
) -> tuple[bool, str]:
    """Whether the speech-check corrections accumulated enough new material to retrain `laya-intent`."""
    path = feedback_path or (home_dir / "feedback" / "intent.jsonl")
    if not path.is_file():
        return False, "no feedback log yet"
    fresh = 0
    with path.open(encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if index < bookmark_cursor:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("correction_of"):
                fresh += 1
    if fresh < LAYA_CORRECTIONS_MIN:
        return False, f"{fresh} new corrections, {LAYA_CORRECTIONS_MIN} needed"
    return True, f"{fresh} new corrections"


def write_manifest(checkpoint: Path, *, target: str, scores: dict[str, Any]) -> Path:
    """What the checkpoint scored, next to its weights: the record a rollback or an audit reads."""
    manifest = checkpoint / "manifest.json"
    payload = {
        "target": target,
        "promoted_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scores": scores,
    }
    manifest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    manifest.chmod(0o600)
    return manifest


def status(home_dir: Path) -> list[dict[str, Any]]:
    """One row per target for `jarvis models`: the current checkpoint name and whether a rollback exists."""
    rows: list[dict[str, Any]] = []
    for target in TARGETS:
        folder = target_dir(home_dir, target)
        active = current(home_dir, target)
        previous = folder / "previous"
        rows.append(
            {
                "target": target,
                "current": active.name if active else None,
                "previous": previous.resolve().name if previous.is_symlink() else None,
            }
        )
    return rows


def _assert_inside(path: Path, parent: Path) -> None:
    resolved, root = path.resolve(), parent.resolve()
    if resolved != root and not str(resolved).startswith(f"{root}/"):
        raise ValueError(f"{path} is outside {parent}")


def discard(home_dir: Path, target: str, checkpoint: Path) -> None:
    """Removes a rejected candidate, never the live or previous one."""
    folder = target_dir(home_dir, target)
    _assert_inside(checkpoint, folder)
    if checkpoint.resolve() in {p.resolve() for p in (current(home_dir, target),) if p}:
        raise ValueError("refusing to delete the live checkpoint")
    shutil.rmtree(checkpoint, ignore_errors=True)
