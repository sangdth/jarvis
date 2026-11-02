import httpx
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from jarvis.executive_filter import ExecutiveFilter
from jarvis.state import UserIntent


def online_filter() -> ExecutiveFilter:
    return ExecutiveFilter(api_key="sk-or-v1-test")


class TestExecutiveFilter(unittest.IsolatedAsyncioTestCase):
    def test_initialization(self) -> None:
        filt = ExecutiveFilter(api_key="sk-or-v1-test", base_url="https://openrouter.ai/api/v1/")
        self.assertEqual(filt.base_url, "https://openrouter.ai/api/v1")
        self.assertEqual(filt.decisions_url, "https://openrouter.ai/api/alpha/decisions")

    async def test_classify_user_utterance_asks_the_decision_model(self) -> None:
        filt = online_filter()
        reply = MagicMock()
        reply.json.return_value = {"answers": {"intent": {"type": "choice", "choice": "ADDRESSED_TO_JARVIS"}}}
        post = AsyncMock(return_value=reply)
        with patch.object(filt._http, "post", new=post):
            intent = await filt.classify_user_utterance("Use Postgres", recent_jarvis_question="Postgres or SQLite?")
        self.assertEqual(intent, UserIntent.ADDRESSED_TO_JARVIS)
        self.assertEqual(post.call_args.args[0], "https://openrouter.ai/api/alpha/decisions")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["model"], filt.model)
        self.assertEqual(body["state"], {"document": "Use Postgres", "jarvis_last_said": "Postgres or SQLite?"})
        self.assertEqual(set(body["questions"]["intent"]["criteria"]), {i.value for i in UserIntent})

    async def test_classifier_failure_ignores_speech(self) -> None:
        filt = online_filter()
        with patch.object(filt._http, "post", new=AsyncMock(side_effect=httpx.ConnectError("network down"))):
            intent = await filt.classify_user_utterance("Turn on the lights in the kitchen")
        self.assertEqual(intent, UserIntent.IGNORE_BACKGROUND)


if __name__ == "__main__":
    unittest.main()
