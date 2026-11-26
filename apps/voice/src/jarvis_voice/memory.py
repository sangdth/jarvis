"""Long-term memory: Mem0 (open source) with Qdrant on disk and Ollama bge-m3 embeddings, all on this Mac.

Benchmarked 2026-11-03: bge-m3 on Ollama embeds in ~20 ms (OpenRouter's best ~400 ms), and a search over 212
memories takes ~22 ms. Fact extraction runs in the background on EXTRACTION_MODEL with thinking off; small local models were
too slow (gemma3:4b 27 s a save) or lost facts (gemma3:1b).
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from jarvis_voice import telemetry
from jarvis_voice.config import JARVIS_HOME, settings
from jarvis_voice.prompts import MEETING_TRANSCRIPT, NOT_THE_USER, ROOM, SPEAKER, USER

# Mem0 reports usage to PostHog unless this is set before it is imported.
os.environ.setdefault("MEM0_TELEMETRY", "False")

MEMORY_DIR = JARVIS_HOME / "memory"
USER_ID = "sang"  # one user; Mem0 needs an id to file memories under
# A confidently named guest keeps their own memories; a guess (`John? (70%)`) or the room keeps none.
_CONFIRMED_SPEAKER_RE = re.compile(r"^\[Speaker: ([^?\]]+)\]:", re.MULTILINE)


def memory_user(text: str) -> str | None:
    """The Mem0 namespace this turn belongs to: the owner, a confirmed guest, or None for room/guesses."""
    last = next((line.strip() for line in reversed(text.splitlines()) if line.strip()), "")
    if last.startswith(ROOM):
        return None
    match = _CONFIRMED_SPEAKER_RE.match(last)
    if match:
        return f"speaker:{match.group(1).strip().lower()}"
    if last.startswith(SPEAKER):
        return None
    return USER_ID


def _skip_text(text: str) -> bool:
    """True for turns that never enter long-term memory (Jarvis's notes, room, a guessed or owner-tagged line)."""
    return text.startswith((NOT_THE_USER, MEETING_TRANSCRIPT, ROOM, USER)) or (
        text.startswith(SPEAKER) and memory_user(text) is None
    )


def mem0_config(memory_dir: Path | None = None) -> dict[str, Any]:
    """Mem0 local config: memories and history stay in memory_dir; only fact extraction calls OpenRouter."""
    memory_dir = memory_dir or MEMORY_DIR  # read at call time, so a patched MEMORY_DIR (tests) is honored
    return {
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "path": str(memory_dir / "qdrant"),
                "collection_name": "jarvis",
                "embedding_model_dims": 1024,  # bge-m3
                "on_disk": True,
            },
        },
        "embedder": {"provider": "ollama", "config": {"model": "bge-m3", "ollama_base_url": "http://127.0.0.1:11434"}},
        "llm": {
            "provider": "openai",
            "config": {
                "model": settings.extraction_model,
                "api_key": settings.openrouter_api_key,
                "openai_base_url": settings.openrouter_base_url,
                # Extraction runs on every exchange, so it never thinks. Mem0 sends reasoning_effort only for a
                # "reasoning model"; "none" turns DeepSeek's thinking off.
                "is_reasoning_model": True,
                "reasoning_effort": "none",
            },
        },
        "history_db_path": str(memory_dir / "history.db"),
    }


def _ensure_ollama_model_exists(self: Any) -> None:
    """Require the Ollama embedding model to be pulled already (never freeze startup on a pull), and pin it in RAM."""
    local_models = self.client.list()["models"]
    target = self._normalize_model_name(self.config.model)
    if not any(
        self._normalize_model_name(model.get("name", "") or "") == target
        or self._normalize_model_name(model.get("model", "") or "") == target
        for model in local_models
    ):
        raise RuntimeError(f"Ollama model {self.config.model!r} is not pulled; run `ollama pull {self.config.model}`")
    embed = self.client.embed
    self.client.embed = lambda *a, **kw: embed(*a, **{"keep_alive": -1, **kw})
    self.client.embed(model=self.config.model, input=".")


def create_memory_service(*, is_meeting: Callable[[], bool] = lambda: False) -> Any | None:
    """The Pipecat memory processor, or None (Jarvis runs without memory) when it can't start."""
    if not settings.openrouter_api_key:
        logger.warning("No OPENROUTER_API_KEY: long-term memory is off (it needs a model to pick facts).")
        return None
    from mem0.embeddings.ollama import OllamaEmbedding
    from pipecat.services.mem0.memory import Mem0MemoryService

    OllamaEmbedding._ensure_model_exists = _ensure_ollama_model_exists  # type: ignore[method-assign]

    class LastExchangeMemory(Mem0MemoryService):
        """Pipecat saves the whole conversation after every turn; save only the newest exchange, once.

        Otherwise each save re-sends hours of talk to the extraction model, and agent reports (which start
        a reply without a new user message) would save the same exchange again.
        """

        _last_saved: list[Any] | None = None

        async def _enhance_context_with_memories(self, context, query):  # type: ignore[override]
            raw = list(context.get_messages())
            # Agent reports (system) and tool round-trips (tool) reuse the current turn's recall block without re-querying.
            if not raw or not isinstance(raw[-1], dict) or raw[-1].get("role") != "user":
                return
            # Strip any prior recall block first so a skipped turn (guest, non-user, meeting) never keeps owner memories.
            messages = [
                m
                for m in raw
                if not (isinstance(m, dict) and str(m.get("content", "")).startswith(self.system_prompt))
            ]
            if len(messages) != len(raw):
                context.set_messages(messages)
            # Meeting mode, Jarvis's own notes (the greeting), and meeting transcripts never query personal memories.
            if is_meeting() or query.startswith((NOT_THE_USER, MEETING_TRANSCRIPT, USER)):
                return
            user = memory_user(query)
            if user is None:
                return
            self.last_query = query
            started = time.monotonic()
            previous, self.user_id = self.user_id, user
            try:
                memories = await self._retrieve_memories(query)
            finally:
                self.user_id = previous
            telemetry.emit(
                "voice.memory",
                hits=len(memories),
                ms=round((time.monotonic() - started) * 1000),
                text="; ".join(str(m.get("memory", "")) for m in memories[:4]),
            )
            if not memories:
                return
            memory_text = self.system_prompt + "".join(f"{i}. {m.get('memory', '')}\n\n" for i, m in enumerate(memories, 1))
            memory_message = {
                "role": "developer" if self.add_as_system_message else "user",
                "content": memory_text,
            }
            # Place the new recall block right before the latest user message so earlier turns stay prefix-stable for caching.
            idx = next(
                (
                    i
                    for i in range(len(messages) - 1, -1, -1)
                    if isinstance(messages[i], dict) and messages[i].get("role") == "user"
                ),
                len(messages),
            )
            messages.insert(idx, memory_message)
            context.set_messages(messages)

        async def _store_messages(self, messages):  # type: ignore[override]
            # process_frame runs before the LLM responds, so a new user turn ends with "user"; agent reports do not.
            if is_meeting() or not messages or messages[-1].get("role") != "user":
                return
            if _skip_text(str(messages[-1].get("content", ""))):
                return
            if (
                len(messages) >= 3
                and messages[-2].get("role") == "assistant"
                and messages[-3].get("role") == "user"
                and _skip_text(str(messages[-3].get("content", "")))
            ):
                newest = messages[-1:]
            else:
                newest = messages[-2:]
            if newest == self._last_saved or any(
                m.get("role") == "user" and _skip_text(str(m.get("content", ""))) for m in newest
            ):
                return
            user_text = next(
                (str(m.get("content", "")) for m in reversed(newest) if m.get("role") == "user"), ""
            )
            user = memory_user(user_text)
            if user is None:
                return
            self._last_saved = newest
            previous, self.user_id = self.user_id, user
            try:
                await super()._store_messages(newest)
            finally:
                self.user_id = previous

    MEMORY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(MEMORY_DIR, 0o700)
    try:
        return LastExchangeMemory(
            local_config=mem0_config(),
            user_id=USER_ID,
        )
    except Exception as exc:
        # Usually Ollama is not running: the embedder checks for bge-m3 at startup.
        logger.warning(f"Long-term memory is off ({exc!r}). Start Ollama (with bge-m3) and restart Jarvis.")
        return None
