import inspect
import unittest
from unittest.mock import AsyncMock, MagicMock

from pipecat.services.llm_service import FunctionCallParams

from jarvis.agent_watchers import AgentOrchestrator
from jarvis.main import agent_tool_handlers, agent_tools


def params(arguments: dict) -> FunctionCallParams:
    """The object Pipecat 1.x passes to a registered function handler."""
    return FunctionCallParams(
        function_name="x",
        tool_call_id="call_1",
        arguments=arguments,
        llm=MagicMock(),
        pipeline_worker=MagicMock(),
        context=MagicMock(),
        result_callback=AsyncMock(),
    )


class TestAgentToolHandlers(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.orch = AgentOrchestrator(state=MagicMock())
        agent = MagicMock()
        agent.config.id = "hermes"
        agent.send_task = AsyncMock(return_value=True)
        agent.answer = AsyncMock(return_value=False)
        agent.interrupt = AsyncMock(return_value=True)
        self.agent = agent
        self.orch.adapters["hermes"] = agent
        self.handlers = agent_tool_handlers(self.orch)

    def test_every_tool_has_a_one_argument_handler(self) -> None:
        names = {t.name for t in agent_tools()}
        self.assertEqual(set(self.handlers), names)
        for handler in self.handlers.values():
            self.assertEqual(len(inspect.signature(handler).parameters), 1)

    async def test_send_answer_stop_reach_the_agent(self) -> None:
        p = params({"agent_id": "hermes", "task": "check my calendar"})
        await self.handlers["send_agent_task"](p)
        self.agent.send_task.assert_awaited_once_with("check my calendar")
        p.result_callback.assert_awaited_once_with({"status": "sent", "agent_id": "hermes"})

        p = params({"agent_id": "hermes", "answer": "yes"})
        await self.handlers["answer_agent"](p)
        p.result_callback.assert_awaited_once_with({"status": "no_pending_question", "agent_id": "hermes"})

        p = params({"agent_id": "hermes"})
        await self.handlers["stop_agent"](p)
        p.result_callback.assert_awaited_once_with({"status": "stopped", "agent_id": "hermes"})

    async def test_unknown_agent(self) -> None:
        p = params({"agent_id": "nope", "task": "x"})
        await self.handlers["send_agent_task"](p)
        p.result_callback.assert_awaited_once_with({"status": "agent_not_active", "agent_id": "nope"})


if __name__ == "__main__":
    unittest.main()
