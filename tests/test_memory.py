import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from jarvis import memory


class TestMemory(unittest.IsolatedAsyncioTestCase):
    def test_config_stays_on_this_mac_and_telemetry_is_off(self) -> None:
        self.assertEqual(os.environ.get("MEM0_TELEMETRY"), "False")
        cfg = memory.mem0_config(Path("/tmp/x"))
        self.assertEqual(cfg["vector_store"]["provider"], "qdrant")
        self.assertEqual(cfg["vector_store"]["config"]["path"], "/tmp/x/qdrant")
        self.assertEqual(cfg["vector_store"]["config"]["embedding_model_dims"], 1024)
        self.assertEqual(cfg["embedder"]["config"]["ollama_base_url"], "http://127.0.0.1:11434")
        self.assertEqual(cfg["history_db_path"], "/tmp/x/history.db")
        # A patched MEMORY_DIR must win, or a test writes into the user's real memory
        with patch.object(memory, "MEMORY_DIR", Path("/tmp/y")):
            self.assertEqual(memory.mem0_config()["vector_store"]["config"]["path"], "/tmp/y/qdrant")

    async def test_saves_only_the_newest_exchange_once(self) -> None:
        client = MagicMock()
        with (
            patch.object(memory.settings, "openrouter_api_key", "sk-test"),
            patch.object(memory, "MEMORY_DIR", Path("/tmp/jarvis-test-memory")),
            patch("mem0.Memory.from_config", return_value=client),
        ):
            service = memory.create_memory_service()
        assert service is not None
        talk = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "I'm moving the team to Bun."},
        ]
        await service._store_messages(talk)
        await service._store_messages(talk)  # an agent report re-runs the LLM with no new user words
        client.add.assert_called_once()
        self.assertEqual(client.add.call_args.kwargs["messages"], talk[-2:])
        self.assertEqual(client.add.call_args.kwargs["user_id"], "sang")

    async def test_greeting_note_is_neither_searched_nor_saved(self) -> None:
        client = MagicMock()
        with (
            patch.object(memory.settings, "openrouter_api_key", "sk-test"),
            patch.object(memory, "MEMORY_DIR", Path("/tmp/jarvis-test-memory")),
            patch("mem0.Memory.from_config", return_value=client),
        ):
            service = memory.create_memory_service()
        note = memory.NOT_THE_USER + " Jarvis just started. Greet the user."
        await service._enhance_context_with_memories(MagicMock(), note)
        await service._store_messages([{"role": "user", "content": note}])
        client.search.assert_not_called()
        client.add.assert_not_called()

    def test_no_memory_when_ollama_is_down(self) -> None:
        with (
            patch.object(memory.settings, "openrouter_api_key", "sk-test"),
            patch.object(memory, "MEMORY_DIR", Path("/tmp/jarvis-test-memory")),
            patch("mem0.Memory.from_config", side_effect=ConnectionError("ollama down")),
        ):
            self.assertIsNone(memory.create_memory_service())


if __name__ == "__main__":
    unittest.main()
