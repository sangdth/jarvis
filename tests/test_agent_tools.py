import inspect
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

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
        self.orch._http = None  # no real OpenRouter call for the waiting lines
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



class TestSessionSwitch(unittest.IsolatedAsyncioTestCase):
    """`session` and `project` on send_agent_task, for the coding clients other than T3 Code."""

    def setUp(self) -> None:
        from pathlib import Path

        from jarvis.sessions import AgentSession, CodingSessionAdapter

        self.orch = AgentOrchestrator(state=MagicMock())
        self.orch._http = None
        agent = MagicMock(spec=CodingSessionAdapter)
        agent.config = MagicMock(id="claude")  # set in __init__, so not on the spec
        agent.busy, agent.connected, agent.cwd = False, False, "/p/jarvis"
        agent.send_task = AsyncMock(return_value=True)
        agent.new_session = AsyncMock(return_value=True)
        agent.pick_session = AsyncMock(return_value=True)
        self.login = AgentSession("s1", "Fix the login bug", "/p/web", 2.0)
        self.voice = AgentSession("s2", "Speaker verification", "/p/jarvis", 1.0)
        agent.list_sessions = AsyncMock(return_value=[self.login, self.voice])
        self.agent = agent
        self.orch.adapters["claude"] = agent
        self.handlers = agent_tool_handlers(self.orch)
        self.projects = {"web": Path("/p/web"), "jarvis": Path("/p/jarvis")}

    async def send(self, **arguments) -> dict:
        from unittest.mock import patch

        p = params({"agent_id": "claude", **arguments})
        with patch("jarvis.agent_tools.resolve_project", new=self.projects.get):
            await self.handlers["send_agent_task"](p)
        return p.result_callback.await_args.args[0]

    async def test_new_session_alone_is_ready_and_sends_nothing(self) -> None:
        self.assertEqual(await self.send(session="new"), {"status": "session_ready", "agent_id": "claude"})
        self.agent.new_session.assert_awaited_once_with(None)
        self.agent.send_task.assert_not_awaited()

    async def test_named_session_is_picked_then_the_task_goes_to_it(self) -> None:
        result = await self.send(session="the speaker verification one", task="lower the threshold")
        self.assertEqual(result["status"], "sent")
        self.agent.pick_session.assert_awaited_once_with(self.voice)
        self.agent.send_task.assert_awaited_once_with("lower the threshold")

    async def test_no_match_unknown_project_and_mid_turn_send_nothing(self) -> None:
        self.assertEqual((await self.send(session="billing", task="x"))["status"], "no_matching_session")
        self.assertEqual((await self.send(project="nope", task="x"))["status"], "unknown_project")
        self.agent.busy = True
        self.agent.new_session = AsyncMock(return_value=False)
        self.assertEqual((await self.send(session="new", task="x"))["status"], "busy")
        self.agent.send_task.assert_not_awaited()

    async def test_a_project_moves_to_its_newest_session_unless_already_there(self) -> None:
        await self.send(project="jarvis", task="run the tests")  # already in /p/jarvis
        self.agent.list_sessions.assert_not_awaited()
        self.agent.list_sessions = AsyncMock(return_value=[self.login])
        await self.send(project="web", task="fix it")
        self.agent.list_sessions.assert_awaited_once_with("/p/web", limit=50)
        self.agent.pick_session.assert_awaited_once_with(self.login)
        self.agent.list_sessions = AsyncMock(return_value=[])
        await self.send(project="web")
        self.agent.new_session.assert_awaited_once_with("/p/web")  # no session there yet: a new one

    async def test_connected_session_ignores_session_and_project(self) -> None:
        self.agent.connected = True
        self.assertEqual((await self.send(session="new", project="web", task="fix it"))["status"], "sent")
        self.agent.new_session.assert_not_awaited()
        self.agent.pick_session.assert_not_awaited()
        self.agent.send_task.assert_awaited_once_with("fix it")

    async def test_someone_else_in_a_meeting_cannot_answer_stop_or_replace(self) -> None:
        self.orch.state.room_turn = True
        for name, arguments in (("answer_agent", {"answer": "yes"}), ("stop_agent", {})):
            p = params({"agent_id": "claude", **arguments})
            await self.handlers[name](p)
            self.assertEqual(p.result_callback.await_args.args[0]["status"], "owner_only")
        self.assertEqual((await self.send(task="fix it", replace=True))["status"], "owner_only")
        self.agent.send_task.assert_not_awaited()

    async def test_guest_task_waits_for_the_masters_yes(self) -> None:
        from jarvis.config import settings

        with patch.object(settings, "meeting_guests", "ask"):
            self.orch.state.room_turn = True
            result = await self.send(task="fix it")
            self.assertEqual((result["status"], result["task"]), ("needs_owner_ok", "fix it"))
            self.agent.send_task.assert_not_awaited()
            self.orch.state.room_turn = False  # the master says yes; the model sends it again
            self.assertEqual((await self.send(task="fix it"))["status"], "sent")
        self.agent.send_task.assert_awaited_once_with("fix it")

    async def test_meeting_guests_allow_runs_guest_tasks(self) -> None:
        from jarvis.config import settings

        self.orch.state.room_turn = True
        with patch.object(settings, "meeting_guests", "allow"):
            self.assertEqual((await self.send(task="fix it"))["status"], "sent")
            self.assertEqual((await self.send(task="fix it", replace=True))["status"], "owner_only")


if __name__ == "__main__":
    unittest.main()
