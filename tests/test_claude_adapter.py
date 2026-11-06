import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ResultMessage

from fakes import make
from jarvis.claude_adapter import ClaudeCodeAdapter


def context(**kw):
    ctx = MagicMock(title=None, description=None)
    ctx.configure_mock(**kw)
    return ctx


class TestClaudeCodeAdapter(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.adapter = make(ClaudeCodeAdapter, "claude_code")
        self.adapter._client = MagicMock(query=AsyncMock(), interrupt=AsyncMock())

    async def ask_and_answer(self, tool: str, tool_input: dict, answer: str | None, ctx=None):
        """Start a tool permission check, answer it like the user would, and return the SDK result."""
        pending = asyncio.create_task(self.adapter._can_use_tool(tool, tool_input, ctx or context()))
        while self.adapter._question is None:
            await asyncio.sleep(0)
        if answer is None:
            await self.adapter.interrupt()
        else:
            self.assertTrue(await self.adapter.answer(answer))
        return await pending

    async def test_result_is_surfaced_and_errors_are_forced(self) -> None:
        await self.adapter._handle_message(MagicMock(spec=ResultMessage, result="Done.", is_error=False))
        self.adapter._surface.assert_awaited_with("Done.", is_report=True)
        await self.adapter._handle_message(MagicMock(spec=ResultMessage, result=None, is_error=True, subtype="error"))
        self.adapter._surface.assert_awaited_with("The turn failed (error).", is_report=True)

    async def test_bash_permission_is_asked_and_follows_the_answer(self) -> None:
        result = await self.ask_and_answer("Bash", {"command": "rm -rf build"}, "yes, go ahead")
        self.assertIsInstance(result, PermissionResultAllow)
        self.adapter._surface.assert_awaited_with(
            "needs approval: rm -rf build"
        )
        result = await self.ask_and_answer("Bash", {"command": "rm -rf build"}, "no, don't")
        self.assertIsInstance(result, PermissionResultDeny)

    async def test_ask_user_question_maps_the_answer_to_an_option(self) -> None:
        questions = [{"question": "Which database?", "header": "DB", "multiSelect": False,
                      "options": [{"label": "Postgres", "description": ""}, {"label": "SQLite", "description": ""}]}]
        result = await self.ask_and_answer("AskUserQuestion", {"questions": questions}, "let's use sqlite")
        self.assertEqual(result.updated_input["answers"], {"Which database?": "SQLite"})
        self.adapter._surface.assert_awaited_with(
            "asks: Which database? Options: Postgres, SQLite."
        )

    async def test_stop_while_waiting_denies_and_interrupts(self) -> None:
        self.adapter._running = True
        result = await self.ask_and_answer("Bash", {"command": "make deploy"}, None)
        self.assertIsInstance(result, PermissionResultDeny)
        self.assertTrue(result.interrupt)
        self.adapter._client.interrupt.assert_awaited_once()
        # The stopped turn ends with an error result; the user asked for it, so it is not read out.
        self.adapter._surface.reset_mock()
        await self.adapter._handle_message(MagicMock(spec=ResultMessage, result=None, is_error=True, subtype="x"))
        self.adapter._surface.assert_not_awaited()

    async def test_stop_while_idle_does_not_hide_the_next_result(self) -> None:
        await self.adapter.interrupt()
        await self.adapter._handle_message(MagicMock(spec=ResultMessage, result="Done.", is_error=False))
        self.adapter._surface.assert_awaited_once_with("Done.", is_report=True)

    async def test_answer_without_question_and_new_task(self) -> None:
        self.assertFalse(await self.adapter.answer("yes"))
        self.assertTrue(await self.adapter.send_task("add a login page"))
        self.adapter._client.query.assert_awaited_once_with("add a login page")

    async def test_question_queued_behind_a_stop_is_not_asked(self) -> None:
        # Two parallel tool calls: the second waits for the first; "stop" must end both.
        first = asyncio.create_task(self.adapter._can_use_tool("Bash", {"command": "a"}, context()))
        while self.adapter._question is None:
            await asyncio.sleep(0)
        second = asyncio.create_task(self.adapter._can_use_tool("Bash", {"command": "b"}, context()))
        await asyncio.sleep(0)
        await self.adapter.interrupt()
        self.assertIsInstance(await first, PermissionResultDeny)
        self.assertIsInstance(await second, PermissionResultDeny)
        self.adapter._surface.assert_awaited_once_with("needs approval: a")  # "b" was never read out

    async def test_a_crashed_claude_process_is_restarted(self) -> None:
        starts = []

        class Client:
            def __init__(self, options) -> None:
                starts.append(options)

            async def __aenter__(self):
                if len(starts) == 1:
                    raise RuntimeError("claude exited")
                return self

            async def __aexit__(self, *exc) -> None:
                return None

            async def receive_messages(self):
                await asyncio.sleep(3600)
                yield

        self.adapter._running = True
        real_sleep = asyncio.sleep
        with (
            patch("jarvis.claude_adapter.ClaudeSDKClient", Client),
            patch("asyncio.sleep", new=lambda s: real_sleep(0 if s == 10.0 else s)),  # skip the 10 s restart wait
        ):
            loop = asyncio.create_task(self.adapter._run_loop())
            for _ in range(20):
                await asyncio.sleep(0)
            self.assertEqual(len(starts), 2)
            self.assertIsNotNone(self.adapter._client)
            self.assertFalse(self.adapter.busy)  # the dead turn no longer counts as running
            loop.cancel()

    async def test_stop_then_send_keeps_the_new_task_running(self) -> None:
        # A correction: stop, send the corrected task, then the stopped turn's late result arrives.
        await self.adapter.send_task("look up Hacker Hustle")
        await self.adapter.interrupt()
        await self.adapter.send_task("look up HuggerHustle")
        await self.adapter._handle_message(MagicMock(spec=ResultMessage, result=None, is_error=True, subtype="x"))
        self.assertTrue(self.adapter.busy)  # still working on the corrected task
        self.adapter._surface.assert_not_awaited()
        await self.adapter._handle_message(MagicMock(spec=ResultMessage, result="HuggerHustle is fine.", is_error=False))
        self.adapter._surface.assert_awaited_once_with("HuggerHustle is fine.", is_report=True)
        self.assertFalse(self.adapter.busy)


if __name__ == "__main__":
    unittest.main()
