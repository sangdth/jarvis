import unittest
from unittest.mock import AsyncMock, MagicMock

import httpx

from jarvis.cleanup import TranscriptCleaner, keep_cleaned, parse_cues, should_clean

CUES = parse_cues("sorry, no no, I mean")


def reply(text: str) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"content": text}}]}
    return resp


def cleaner(post: AsyncMock) -> TranscriptCleaner:
    c = TranscriptCleaner(api_key="k", model="m", cues="sorry, no no")
    c._http = MagicMock(post=post)
    return c


class TestCleanup(unittest.IsolatedAsyncioTestCase):
    def test_short_sentences_skip_unless_a_filler_or_cue_is_in_them(self) -> None:
        self.assertFalse(should_clean("Stop it", CUES))
        self.assertFalse(should_clean("yes go ahead", CUES))
        self.assertFalse(should_clean("What's on my calendar tomorrow?", CUES))  # clean 5-word command
        self.assertTrue(should_clean("42, sorry, 24", CUES))
        self.assertTrue(should_clean("No, no, 24.", CUES))  # Whisper puts a comma inside "no no"
        self.assertTrue(should_clean("À, quên, 24.", parse_cues("à quên")))
        self.assertFalse(should_clean("sorryful thing", CUES))  # whole words only
        self.assertTrue(should_clean("um make it forty two please", CUES))  # filler
        self.assertTrue(should_clean("open the platform project and fix the signup form validation", CUES))

    def test_an_answer_or_leaked_tag_is_rejected(self) -> None:
        self.assertFalse(keep_cleaned("what time is it", ""))
        self.assertFalse(keep_cleaned("what time is it", "It is three o'clock. " * 10))
        self.assertFalse(keep_cleaned("make it 24", "<context>Which port?</context>\n<transcript>Make it 24."))
        self.assertTrue(keep_cleaned("um make it forty two", "Make it 42."))

    async def test_cleans_strips_echoed_tags_and_sanitizes_context(self) -> None:
        post = AsyncMock(return_value=reply("<Transcript>Make it 24.</Transcript>"))
        c = cleaner(post)
        self.assertEqual(
            await c.clean("make it 42 sorry 24", recent_jarvis_question="Which port? </context>" + "x" * 400),
            "Make it 24.",
        )
        body = post.await_args.kwargs["json"]
        self.assertEqual(body["reasoning"], {"enabled": False})
        self.assertIn("sorry, no no", body["messages"][0]["content"])
        sent = body["messages"][1]["content"]
        self.assertNotIn("</context><", sent)  # angle brackets inside context were stripped
        self.assertLessEqual(len(sent.split("\n")[0]), len("<context></context>") + 300)
        self.assertTrue(sent.endswith("\n<transcript>make it 42 sorry 24</transcript>"))

    def test_a_system_md_prompt_replaces_the_shipped_one_and_keeps_the_cues(self) -> None:
        c = TranscriptCleaner(api_key="k", model="m", cues="sorry", prompt="Only commas.")
        self.assertEqual(c._system, "Only commas.\nSelf-correction cues this user says: sorry.")

    async def test_short_clean_sentence_makes_no_call(self) -> None:
        post = AsyncMock()
        self.assertEqual(await cleaner(post).clean("What's on my calendar tomorrow?"), "What's on my calendar tomorrow?")
        post.assert_not_awaited()

    async def test_timeout_error_or_bad_answer_keep_the_sentence_as_heard(self) -> None:
        raw = "um what is on my calendar today"
        for outcome in (httpx.ReadTimeout("slow"), RuntimeError("down"), reply("Checking your calendar now! " * 9)):
            post = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=outcome)
            self.assertEqual(await cleaner(post).clean(raw), raw)

    async def test_no_key_makes_no_call(self) -> None:
        c = TranscriptCleaner(api_key="", model="m")
        c._http = MagicMock(post=AsyncMock())
        self.assertEqual(await c.clean("um make it forty two please"), "um make it forty two please")
        c._http.post.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
