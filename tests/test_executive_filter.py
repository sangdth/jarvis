import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from jarvis.executive_filter import ExecutiveFilter
from jarvis.state import UserIntent


def online_filter() -> ExecutiveFilter:
    return ExecutiveFilter(api_key="sk-or-v1-test", laya_api_key="")


class TestExecutiveFilter(unittest.IsolatedAsyncioTestCase):
    def test_initialization(self) -> None:
        filt = ExecutiveFilter(api_key="sk-or-v1-test", base_url="https://openrouter.ai/api/v1/", laya_api_key="")
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


def answer(choice: str) -> MagicMock:
    reply = MagicMock()
    reply.json.return_value = {"answers": {"intent": {"choice": choice, "probabilities": {choice: 0.9}}}}
    return reply


class TestLayaFirst(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.feedback = MagicMock()
        self.feedback.decision.return_value = "d1"
        self.filt = ExecutiveFilter(api_key="sk-or-v1-test", laya_api_key="laya_test", feedback=self.feedback)

    async def asyncTearDown(self) -> None:
        await self.filt.aclose()

    async def test_acts_on_laya_and_logs_jev_when_it_arrives(self) -> None:
        laya = AsyncMock(return_value=answer("IGNORE_BACKGROUND"))
        jev = AsyncMock(return_value=answer("ADDRESSED_TO_JARVIS"))
        with patch.object(self.filt._laya_http, "post", new=laya), patch.object(self.filt._http, "post", new=jev):
            intent = await self.filt.classify_user_utterance("Honey, dinner?")
            await asyncio.sleep(0)  # let the Jev task and its callback run
            await asyncio.sleep(0)
        self.assertEqual(intent, UserIntent.IGNORE_BACKGROUND)
        self.assertEqual(laya.call_args.args[0], "https://laya.happyvoxel.com/v1/systemone")
        self.assertEqual(laya.call_args.kwargs["json"]["model"], "jarvis-intent")
        _, _, logged_intent, source = self.feedback.decision.call_args.args
        self.assertEqual((logged_intent, source), (UserIntent.IGNORE_BACKGROUND, "laya"))
        jev_answer = {"choice": "ADDRESSED_TO_JARVIS", "probabilities": {"ADDRESSED_TO_JARVIS": 0.9}}
        self.feedback.late.assert_called_once_with("d1", jev_answer)

    async def test_laya_down_falls_back_to_jev(self) -> None:
        laya = AsyncMock(side_effect=httpx.ConnectTimeout("box down"))
        jev = AsyncMock(return_value=answer("ADDRESSED_TO_JARVIS"))
        with patch.object(self.filt._laya_http, "post", new=laya), patch.object(self.filt._http, "post", new=jev):
            intent = await self.filt.classify_user_utterance("Push the branch")
        self.assertEqual(intent, UserIntent.ADDRESSED_TO_JARVIS)
        kwargs = self.feedback.decision.call_args.kwargs
        self.assertEqual(self.feedback.decision.call_args.args[3], "jev")
        self.assertIn("ConnectTimeout", kwargs["laya"]["error"])
        self.feedback.late.assert_not_called()

    async def test_unknown_label_from_laya_counts_as_a_failure(self) -> None:
        laya = AsyncMock(return_value=answer("SOMETHING_ELSE"))
        jev = AsyncMock(return_value=answer("MUTE"))
        with patch.object(self.filt._laya_http, "post", new=laya), patch.object(self.filt._http, "post", new=jev):
            self.assertEqual(await self.filt.classify_user_utterance("hush now please"), UserIntent.MUTE)

    async def test_not_you_is_ignored_and_logged_without_a_model_call(self) -> None:
        laya = AsyncMock()
        with patch.object(self.filt._laya_http, "post", new=laya):
            intent = await self.filt.classify_user_utterance("Not you, Jarvis.")
        self.assertEqual(intent, UserIntent.IGNORE_BACKGROUND)
        laya.assert_not_awaited()
        self.assertEqual(self.feedback.decision.call_args.args[3], "not-you")


if __name__ == "__main__":
    unittest.main()
