import inspect
import unittest
from unittest.mock import AsyncMock, MagicMock

from pipecat.services.llm_service import FunctionCallParams

from jarvis.agent_tools import agent_tool_handlers, agent_tools
from jarvis.orchestrator import AgentOrchestrator


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
        self.assertIn("hermes", self.orch._reminders)  # a sent task starts the "still waiting" reminders
        p.result_callback.assert_awaited_once_with({"status": "sent", "agent_id": "hermes"})

        p = params({"agent_id": "hermes", "answer": "yes"})
        await self.handlers["answer_agent"](p)
        p.result_callback.assert_awaited_once_with({"status": "no_pending_question", "agent_id": "hermes"})

        p = params({"agent_id": "hermes"})
        await self.handlers["stop_agent"](p)
        p.result_callback.assert_awaited_once_with({"status": "stopped", "agent_id": "hermes"})

    async def test_stop_with_nothing_running_says_so(self) -> None:
        self.agent.busy = False
        p = params({"agent_id": "hermes"})
        await self.handlers["stop_agent"](p)
        self.agent.interrupt.assert_not_awaited()
        p.result_callback.assert_awaited_once_with({"status": "nothing_running", "agent_id": "hermes"})

    async def test_send_with_nothing_to_send_says_so(self) -> None:
        p = params({"agent_id": "hermes"})
        await self.handlers["send_agent_task"](p)
        self.agent.send_task.assert_not_awaited()
        self.assertNotIn("hermes", self.orch._reminders)  # nothing went out, so no "still waiting"
        p.result_callback.assert_awaited_once_with({"status": "nothing_to_send", "agent_id": "hermes"})

    async def test_project_alone_is_nothing_to_send_for_an_agent_that_opens_no_projects(self) -> None:
        agent = MagicMock(spec=["config", "busy", "send_task"])  # no open_project, unlike a bare MagicMock
        agent.config.id, agent.busy, agent.send_task = "claude", False, AsyncMock(return_value=True)
        self.orch.adapters["claude"] = agent
        p = params({"agent_id": "claude", "project": "jarvis"})
        await self.handlers["send_agent_task"](p)
        agent.send_task.assert_not_awaited()
        p.result_callback.assert_awaited_once_with({"status": "nothing_to_send", "agent_id": "claude"})

    async def test_replace_stops_a_busy_agent_before_sending(self) -> None:
        order: list[str] = []
        self.agent.interrupt = AsyncMock(side_effect=lambda: order.append("stop"))
        self.agent.send_task = AsyncMock(side_effect=lambda t: order.append(t) or True)
        self.agent.busy = True
        await self.handlers["send_agent_task"](params({"agent_id": "hermes", "task": "HuggerHustle", "replace": True}))
        self.assertEqual(order, ["stop", "HuggerHustle"])
        order.clear()
        await self.handlers["send_agent_task"](params({"agent_id": "hermes", "task": "and the README"}))
        self.assertEqual(order, ["and the README"])  # an extra task stops nothing
        self.agent.busy = False
        await self.handlers["send_agent_task"](params({"agent_id": "hermes", "task": "X", "replace": True}))
        self.assertEqual(order, ["and the README", "X"])  # nothing running: nothing to stop

    async def test_unknown_agent(self) -> None:
        p = params({"agent_id": "nope", "task": "x"})
        await self.handlers["send_agent_task"](p)
        p.result_callback.assert_awaited_once_with({"status": "agent_not_active", "agent_id": "nope"})


if __name__ == "__main__":
    unittest.main()
