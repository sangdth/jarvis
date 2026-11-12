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

    def test_fact_extraction_runs_on_flash_with_thinking_off(self) -> None:
        from mem0.llms.openai import OpenAILLM

        cfg = memory.mem0_config(Path("/tmp/x"))["llm"]["config"]
        self.assertEqual(cfg["model"], memory.settings.extraction_model)
        # What Mem0 really sends: the request params carry reasoning_effort="none"
        llm = OpenAILLM({**cfg, "api_key": "sk-test"})
        self.assertEqual(llm._get_supported_params(messages=[])["reasoning_effort"], "none")

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
        # A spoken waiting line adds a second assistant message; never extract facts from assistant-only pairs
        await service._store_messages(
            [
                *talk,
                {"role": "assistant", "content": "On it."},
                {"role": "assistant", "content": "Still working on it."},
            ]
        )
        client.add.assert_called_once()
        self.assertEqual(client.add.call_args.kwargs["messages"], talk[-2:])
        self.assertEqual(client.add.call_args.kwargs["user_id"], "sang")

    async def test_recall_replaces_previous_block_before_latest_user_message(self) -> None:
        client = MagicMock()
        with (
            patch.object(memory.settings, "openrouter_api_key", "sk-test"),
            patch.object(memory, "MEMORY_DIR", Path("/tmp/jarvis-test-memory")),
            patch("mem0.Memory.from_config", return_value=client),
        ):
            service = memory.create_memory_service()
        assert service is not None

        messages: list[dict[str, str]] = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "What runtime do we use?"},
        ]
        ctx = MagicMock()
        ctx.get_messages.side_effect = lambda: list(messages)
        ctx.set_messages.side_effect = lambda updated: messages.__setitem__(slice(None), updated)

        client.search.return_value = {"results": [{"memory": "Team uses Bun"}]}
        await service._enhance_context_with_memories(ctx, "What runtime do we use?")
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "developer", "user"])
        self.assertIn("1. Team uses Bun", messages[2]["content"])
        self.assertEqual(messages[3]["content"], "What runtime do we use?")

        # Next turn: old recall is removed so the earlier prefix stays cache-stable, new recall sits before new user turn
        messages.extend(
            [
                {"role": "assistant", "content": "You use Bun."},
                {"role": "user", "content": "And which database?"},
            ]
        )
        client.search.return_value = {"results": [{"memory": "Team uses Postgres"}]}
        await service._enhance_context_with_memories(ctx, "And which database?")
        self.assertEqual(
            [(m["role"], m["content"]) for m in messages[:4]],
            [
                ("user", "Hi"),
                ("assistant", "Hello!"),
                ("user", "What runtime do we use?"),
                ("assistant", "You use Bun."),
            ],
        )
        self.assertEqual(messages[4]["role"], "developer")
        self.assertIn("1. Team uses Postgres", messages[4]["content"])
        self.assertNotIn("Team uses Bun", messages[4]["content"])
        self.assertEqual(messages[5], {"role": "user", "content": "And which database?"})

    async def test_greeting_note_is_neither_searched_nor_saved(self) -> None:
        client = MagicMock()
        with (
            patch.object(memory.settings, "openrouter_api_key", "sk-test"),
            patch.object(memory, "MEMORY_DIR", Path("/tmp/jarvis-test-memory")),
            patch("mem0.Memory.from_config", return_value=client),
        ):
            service = memory.create_memory_service()
        assert service is not None
        note = memory.NOT_THE_USER + " Jarvis just started. Greet the user."
        await service._enhance_context_with_memories(MagicMock(), note)
        await service._store_messages([{"role": "user", "content": note}])
        client.search.assert_not_called()
        client.add.assert_not_called()

    async def test_meeting_turns_are_not_saved_and_the_room_gets_no_memories(self) -> None:
        client = MagicMock()
        with (
            patch.object(memory.settings, "openrouter_api_key", "sk-test"),
            patch.object(memory, "MEMORY_DIR", Path("/tmp/jarvis-test-memory")),
            patch("mem0.Memory.from_config", return_value=client),
        ):
            service = memory.create_memory_service()
        assert service is not None
        room = memory.ROOM + " Jarvis, what does Sang have planned tonight?"
        guest = memory.SPEAKER + " David]: Jarvis, what does Sang have planned tonight?"
        await service._enhance_context_with_memories(MagicMock(), room)
        await service._enhance_context_with_memories(MagicMock(), guest)
        client.search.assert_not_called()
        for turn in (
            room,
            guest,
            memory.MEETING_TRANSCRIPT + "\n[Room]: Anna is on leave.]\n[User]: Jarvis, note that.",
            memory.MEETING_TRANSCRIPT + "\n[Room]: Anna is on leave.]\n[Speaker: David]: Jarvis, note that.",
        ):
            await service._store_messages([{"role": "user", "content": turn}, {"role": "assistant", "content": "Ok."}])
        client.add.assert_not_called()

    def test_no_memory_when_ollama_is_down(self) -> None:
        with (
            patch.object(memory.settings, "openrouter_api_key", "sk-test"),
            patch.object(memory, "MEMORY_DIR", Path("/tmp/jarvis-test-memory")),
            patch("mem0.Memory.from_config", side_effect=ConnectionError("ollama down")),
        ):
            self.assertIsNone(memory.create_memory_service())

    def test_ollama_embedder_never_pulls_and_pins_keep_alive(self) -> None:
        from mem0.configs.embeddings.base import BaseEmbedderConfig
        from mem0.embeddings.ollama import OllamaEmbedding

        with (
            patch.object(memory.settings, "openrouter_api_key", "sk-test"),
            patch.object(memory, "MEMORY_DIR", Path("/tmp/jarvis-test-memory")),
            patch("mem0.Memory.from_config", return_value=MagicMock()),
        ):
            memory.create_memory_service()

        ollama_client = MagicMock()
        ollama_client.list.return_value = {"models": []}
        with patch("mem0.embeddings.ollama.Client", return_value=ollama_client):
            with self.assertRaisesRegex(RuntimeError, "bge-m3"):
                OllamaEmbedding(BaseEmbedderConfig(model="bge-m3", ollama_base_url="http://127.0.0.1:11434"))
        ollama_client.pull.assert_not_called()

        ollama_client.list.return_value = {"models": [{"model": "bge-m3:latest"}]}
        raw_embed = ollama_client.embed
        raw_embed.return_value = {"embeddings": [[0.1]]}
        with patch("mem0.embeddings.ollama.Client", return_value=ollama_client):
            embedder = OllamaEmbedding(BaseEmbedderConfig(model="bge-m3", ollama_base_url="http://127.0.0.1:11434"))
            embedder.embed("hello")
        ollama_client.pull.assert_not_called()
        self.assertEqual(
            [c.kwargs for c in raw_embed.call_args_list],
            [{"keep_alive": -1, "model": "bge-m3", "input": "."}, {"keep_alive": -1, "model": "bge-m3", "input": "hello"}],
        )


if __name__ == "__main__":
    unittest.main()
