import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from jarvis.prompts import LANGUAGES
from jarvis.waiting_lines import BATCH, WaitingLines, keep_line

BASE = LANGUAGES["en"]["waiting"]


def reply(lines: list[str]) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"content": "\n".join(lines)}}]}
    return resp


def batch(n: int) -> list[str]:
    return [f"Still on batch {n}, line {i}." for i in range(BATCH)]


class TestWaitingLines(unittest.IsolatedAsyncioTestCase):
    async def test_generated_lines_come_first_and_refill_after_four(self) -> None:
        http = MagicMock(post=AsyncMock(side_effect=[reply(batch(1)), reply(batch(2))]))
        lines = WaitingLines("check my calendar", http=http)
        lines.start()
        said = [await lines.next() for _ in range(4)]
        self.assertEqual(said, batch(1)[:4])
        await asyncio.sleep(0)  # the refill started at 4 of 6 runs in the background
        self.assertEqual(http.post.await_count, 2)
        said += [await lines.next() for _ in range(4)]
        self.assertEqual(said[4:], batch(1)[4:] + batch(2)[:2])  # never ran out, never used a base line
        already_said = http.post.await_args_list[1].kwargs["json"]["messages"][1]["content"]
        self.assertIn(batch(1)[3], already_said)  # the refill is told what not to repeat
        body = http.post.await_args_list[0].kwargs["json"]
        self.assertEqual((body["reasoning"], body["provider"]), ({"enabled": False}, {"sort": "latency"}))

    async def test_a_failed_call_uses_a_base_line_and_tries_again(self) -> None:
        http = MagicMock(post=AsyncMock(side_effect=[RuntimeError("down"), reply(batch(1))]))
        lines = WaitingLines("check my calendar", http=http)
        lines.start()
        self.assertIn(await lines.next(), BASE)
        self.assertEqual(await lines.next(), batch(1)[0])

    async def test_a_batch_with_nothing_usable_counts_as_failed(self) -> None:
        bad = ["I'm almost done. " * 10, "", "Still on it. Nearly there. Two more seconds."]
        lines = WaitingLines("x", http=MagicMock(post=AsyncMock(return_value=reply(bad))))
        lines.start()
        self.assertIn(await lines.next(), BASE)

    async def test_a_late_first_batch_is_waited_for(self) -> None:
        async def slow(*_: object, **__: object) -> MagicMock:
            await asyncio.sleep(0.05)
            return reply(batch(1))

        lines = WaitingLines("x", http=MagicMock(post=AsyncMock(side_effect=slow)))
        lines.start()
        self.assertEqual(await lines.next(), batch(1)[0])

    async def test_without_a_client_the_base_lines_never_repeat_back_to_back(self) -> None:
        lines = WaitingLines("x")
        said = [await lines.next() for _ in range(20)]
        self.assertTrue(set(said) <= set(BASE))
        self.assertTrue(all(a != b for a, b in zip(said, said[1:])))

    def test_keep_line(self) -> None:
        self.assertTrue(keep_line("Still working. Nothing back yet.", []))
        self.assertFalse(keep_line("One. Two. Three.", []))
        self.assertFalse(keep_line("x" * 91, []))
        self.assertFalse(keep_line("Still on it.", ["Still on it."]))


if __name__ == "__main__":
    unittest.main()
