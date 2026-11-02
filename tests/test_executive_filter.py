import json
import unittest
from unittest.mock import AsyncMock, patch

from jarvis.config import Settings
from jarvis.executive_filter import ExecutiveFilter
from jarvis.state import UserIntent


class TestExecutiveFilter(unittest.IsolatedAsyncioTestCase):
    def test_openrouter_initialization(self) -> None:
        filt = ExecutiveFilter(
            api_key="sk-or-v1-testkey123",
            model="anthropic/claude-3.5-haiku",
            base_url="https://openrouter.ai/api/v1",
        )
        self.assertTrue(filt.is_openrouter)
        self.assertEqual(filt.model, "anthropic/claude-3.5-haiku")
        self.assertEqual(filt.base_url, "https://openrouter.ai/api/v1")

    def test_anthropic_direct_initialization(self) -> None:
        with patch("jarvis.executive_filter.settings", Settings(openrouter_api_key="")):
            filt = ExecutiveFilter(
                api_key="sk-ant-testkey123",
                model="claude-3-5-haiku-latest",
            )
            self.assertFalse(filt.is_openrouter)
            self.assertEqual(filt.model, "claude-3-5-haiku-latest")

    async def test_offline_heuristic_filter(self) -> None:
        filt = ExecutiveFilter(api_key="")
        # Routine log -> None
        topic = await filt.evaluate_agent_event(
            agent_id="test-agent",
            agent_name="Test Agent",
            raw_event_text="Reading file src/app.py... tests passed.",
        )
        self.assertIsNone(topic)

        # Blocker -> creates topic
        topic_blocked = await filt.evaluate_agent_event(
            agent_id="test-agent",
            agent_name="Test Agent",
            raw_event_text="Error: migration failed. Should I drop the table?",
        )
        self.assertNotNull = self.assertIsNotNone
        self.assertIsNotNone(topic_blocked)
        assert topic_blocked is not None
        self.assertIn("Needs your input", topic_blocked.summary)

    async def test_openrouter_evaluate_agent_event(self) -> None:
        filt = ExecutiveFilter(
            api_key="sk-or-v1-test",
            model="anthropic/claude-3.5-haiku",
        )
        fake_llm_response = json.dumps({
            "should_interrupt": True,
            "requires_decision": True,
            "spoken_summary": "OpenCode needs permission to run DB migration.",
        })
        with patch.object(filt, "_call_llm", new=AsyncMock(return_value=fake_llm_response)):
            topic = await filt.evaluate_agent_event(
                agent_id="opencode-1",
                agent_name="OpenCode",
                raw_event_text="Database lock detected...",
            )
            self.assertIsNotNone(topic)
            assert topic is not None
            self.assertEqual(topic.summary, "OpenCode needs permission to run DB migration.")
            self.assertTrue(topic.requires_decision)

    async def test_openrouter_classify_user_utterance(self) -> None:
        filt = ExecutiveFilter(
            api_key="sk-or-v1-test",
            model="anthropic/claude-3.5-haiku",
        )
        with patch.object(filt, "_call_llm", new=AsyncMock(return_value="ADDRESSED_TO_JARVIS")):
            intent = await filt.classify_user_utterance("Jarvis, what is the status of Claude Code?")
            self.assertEqual(intent, UserIntent.ADDRESSED_TO_JARVIS)

        with patch.object(filt, "_call_llm", new=AsyncMock(return_value="IGNORE_BACKGROUND")):
            intent = await filt.classify_user_utterance("Yeah I think the coffee is ready")
            self.assertEqual(intent, UserIntent.IGNORE_BACKGROUND)


if __name__ == "__main__":
    unittest.main()
