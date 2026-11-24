"""Curation of the voice's Mem0 memories, called by the offline Improver.

Mem0 runs inside the voice pipeline, which shares this process with the brain (`supervisor.py`), so the live
client is attached rather than reopened: two embedded Qdrant handles on one folder would fight over its lock.

The namespace wall is code, not prompt text. A guest's memories (`speaker:<name>`) may be deduplicated or
dropped inside that guest's own namespace; only the owner's (`sang`) may be promoted into `memory/MEMORY.md`,
and no operation may cross from one namespace to another.
"""

from __future__ import annotations

from typing import Any

OWNER = "sang"


def is_namespace(user_id: str) -> bool:
    return user_id == OWNER or user_id.startswith("speaker:")


def _check_namespace(user_id: str) -> None:
    if not is_namespace(user_id):
        raise ValueError(f"unknown memory namespace {user_id!r}: expected {OWNER!r} or 'speaker:<name>'")


def _memory_of(client: Any, memory_id: str) -> dict[str, Any]:
    """Mem0's own row for one memory, so a caller cannot act on an id from another namespace."""
    try:
        found = client.get(memory_id=memory_id)
    except TypeError:  # older Mem0 releases take it positionally
        found = client.get(memory_id)
    row = found.get("results", found) if isinstance(found, dict) else found
    if isinstance(row, list):
        row = row[0] if row else None
    if not isinstance(row, dict):
        raise ValueError(f"memory {memory_id!r} not found")
    return row


def curate_voice_memory(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """One `delete` (a duplicate or a contradicted fact) or `promote` (an owner fact worth keeping forever)."""
    if client is None:
        raise ValueError("Voice memory is not running in this process; nothing to curate.")
    user_id = str(params.get("user_id") or "").strip()
    op = str(params.get("op") or "").strip()
    memory_id = str(params.get("memory_id") or "").strip()
    _check_namespace(user_id)
    if not memory_id:
        raise ValueError("curate_voice_memory needs a memory_id")
    row = _memory_of(client, memory_id)
    if str(row.get("user_id") or "") != user_id:
        raise ValueError(
            f"memory {memory_id!r} belongs to another namespace than {user_id!r}; curation never crosses them"
        )
    if op == "delete":
        client.delete(memory_id=memory_id)
        return {"op": "delete", "memory_id": memory_id}
    if op == "promote":
        if user_id != OWNER:
            raise ValueError("only the owner's memories may be promoted into the brain's index")
        return {"op": "promote", "memory_id": memory_id, "text": str(row.get("memory") or "")}
    raise ValueError("op must be 'delete' or 'promote'")
