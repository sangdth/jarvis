"""Long-term memory: Mem0 (open source) with Qdrant on disk and Ollama bge-m3 embeddings, all on this Mac.

Benchmarked 2026-11-03: bge-m3 on Ollama embeds in ~20 ms (OpenRouter's best ~400 ms), and a search over 212
memories takes ~22 ms. Fact extraction runs in the background on EXTRACTION_MODEL with thinking off; small local models were
too slow (gemma3:4b 27 s a save) or lost facts (gemma3:1b).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from loguru import logger

from jarvis.config import JARVIS_HOME, settings
from jarvis.prompts import MEETING_TRANSCRIPT, NOT_THE_USER, ROOM, SPEAKER

# Mem0 reports usage to PostHog unless this is set before it is imported.
os.environ.setdefault("MEM0_TELEMETRY", "False")

MEMORY_DIR = JARVIS_HOME / "memory"
USER_ID = "sang"  # one user; Mem0 needs an id to file memories under


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


def create_memory_service() -> Any | None:
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
            # Strip any prior recall block first so a skipped turn (guest, non-user) never keeps the master's memories.
            raw = list(context.get_messages())
            messages = [
                m
                for m in raw
                if not (isinstance(m, dict) and str(m.get("content", "")).startswith(self.system_prompt))
            ]
            if len(messages) != len(raw):
                context.set_messages(messages)
            # Jarvis's own notes (the greeting) are not a memory query; someone else in a meeting or guest gets no memories.
            last_line = next((line for line in reversed(query.splitlines()) if line.strip()), "")
            if (
                query.startswith((NOT_THE_USER, ROOM, SPEAKER))
                or last_line.lstrip().startswith((ROOM, SPEAKER))
                or self.last_query == query
            ):
                return
            self.last_query = query
            memories = await self._retrieve_memories(query)
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
            if not messages or messages[-1].get("role") != "user":
                return
            newest = messages[-2:]
            if newest == self._last_saved or any(
                m.get("role") == "user"
                and str(m.get("content", "")).startswith((NOT_THE_USER, MEETING_TRANSCRIPT, ROOM, SPEAKER))
                for m in newest
            ):
                return
            self._last_saved = newest
            await super()._store_messages(newest)

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
