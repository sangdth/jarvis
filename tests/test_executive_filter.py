import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from jarvis.executive_filter import ExecutiveFilter
from jarvis.state import UserIntent


def online_filter() -> ExecutiveFilter:
    return ExecutiveFilter(api_key="sk-or-v1-test", model="deepseek/deepseek-v4.1-flash")


class TestExecutiveFilter(unittest.IsolatedAsyncioTestCase):
    def test_initialization(self) -> None:
        filt = ExecutiveFilter(
            api_key="sk-or-v1-test", model="deepseek/deepseek-v4.1-flash", base_url="https://openrouter.ai/api/v1/"
        )
        self.assertEqual(filt.model, "deepseek/deepseek-v4.1-flash")
        self.assertEqual(filt.base_url, "https://openrouter.ai/api/v1")

    async def test_call_llm_sends_model_and_disables_thinking(self) -> None:
        filt = online_filter()
        response = MagicMock()
        response.json.return_value = {"choices": [{"message": {"content": "  IGNORE_BACKGROUND \n"}}]}
        post = AsyncMock(return_value=response)

        with patch.object(filt._http, "post", new=post):
            result = await filt._call_llm(system_prompt="sys", user_content="hello", max_tokens=20)

        self.assertEqual(result, "IGNORE_BACKGROUND")
        self.assertEqual(post.call_args.args[0], "https://openrouter.ai/api/v1/chat/completions")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["model"], "deepseek/deepseek-v4.1-flash")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual(body["reasoning"], {"enabled": False})
        self.assertEqual(body["provider"], {"sort": "latency"})

    async def test_offline_heuristic_filter(self) -> None:
        filt = ExecutiveFilter(api_key="")
        topic = await filt.evaluate_agent_event(
            agent_id="test-agent",
            agent_name="Test Agent",
            raw_event_text="Reading file src/app.py... tests passed.",
        )
        self.assertIsNone(topic)

        topic_blocked = await filt.evaluate_agent_event(
            agent_id="test-agent",
            agent_name="Test Agent",
            raw_event_text="Error: migration failed. Should I drop the table?",
        )
        assert topic_blocked is not None
        self.assertIn("Needs your input", topic_blocked.summary)

    async def test_evaluate_agent_event(self) -> None:
        filt = online_filter()
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
        assert topic is not None
        self.assertEqual(topic.summary, "OpenCode needs permission to run DB migration.")
        self.assertTrue(topic.requires_decision)

    async def test_classify_user_utterance(self) -> None:
        filt = online_filter()
        with patch.object(filt, "_call_llm", new=AsyncMock(return_value="ADDRESSED_TO_JARVIS")):
            intent = await filt.classify_user_utterance("Jarvis, what is the status of Claude Code?")
            self.assertEqual(intent, UserIntent.ADDRESSED_TO_JARVIS)

        with patch.object(filt, "_call_llm", new=AsyncMock(return_value="IGNORE_BACKGROUND")):
            intent = await filt.classify_user_utterance("Yeah I think the coffee is ready")
            self.assertEqual(intent, UserIntent.IGNORE_BACKGROUND)

    async def test_classifier_failure_ignores_speech(self) -> None:
        filt = online_filter()
        with patch.object(filt, "_call_llm", new=AsyncMock(side_effect=RuntimeError("network down"))):
            intent = await filt.classify_user_utterance("Turn on the lights in the kitchen")
        self.assertEqual(intent, UserIntent.IGNORE_BACKGROUND)


if __name__ == "__main__":
    unittest.main()
