import inspect
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from pipecat.services.llm_service import FunctionCallParams

from jarvis_voice.agent_tools import agent_tool_handlers, agent_tools
from jarvis_voice.orchestrator import AgentOrchestrator


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

    async def test_include_screen_sends_the_screenshot_path_with_the_task(self) -> None:
        from pathlib import Path

        from jarvis_voice.screen import ScreenCapture

        shot = ScreenCapture(Path("/home/.config/jarvis/screen/1.png"), "Terminal", "zsh")
        with patch("jarvis_voice.agent_tools.capture_front_window", AsyncMock(return_value=shot)):
            p = params({"agent_id": "hermes", "task": "Fix this error", "include_screen": True})
            await self.handlers["send_agent_task"](p)
        sent = self.agent.send_task.await_args.args[0]
        self.assertTrue(sent.startswith("Fix this error\n\n[Screenshot of the user's front window (Terminal: zsh): "))
        self.assertIn("/home/.config/jarvis/screen/1.png", sent)
        # The waiting lines are written by a model and spoken: they get the plain task, never the window title
        self.assertEqual(self.orch._reminders["hermes"][1]._task, "Fix this error")

    def coder(self, session: str | None) -> MagicMock:
        from jarvis_voice.sessions import CodingSessionAdapter

        agent = MagicMock(spec=CodingSessionAdapter)
        agent.config = MagicMock(id="brain")
        agent.busy, agent.connected, agent.cwd, agent.session_id, agent._fresh = False, False, "/p/jarvis", session, False
        agent.send_task = AsyncMock(return_value=True)
        self.orch.adapters["brain"] = agent
        return agent

    async def test_include_screen_reaches_a_project_that_is_already_open(self) -> None:
        from pathlib import Path

        from jarvis_voice.screen import ScreenCapture

        agent = self.coder("t1")
        shot = ScreenCapture(Path("/s/1.png"), "Safari", "Login")
        with (
            patch("jarvis_voice.agent_tools.resolve_project", return_value=Path("/p/jarvis")),
            patch("jarvis_voice.agent_tools.capture_front_window", AsyncMock(return_value=shot)),
        ):
            p = params({"agent_id": "brain", "project": "jarvis", "task": "Fix this layout", "include_screen": True})
            await self.handlers["send_agent_task"](p)
        self.assertIn("/s/1.png", agent.send_task.await_args.args[0])
        p.result_callback.assert_awaited_once_with({"agent_id": "brain", "status": "sent"})

    async def test_project_continues_its_newest_idle_session_and_says_which(self) -> None:
        from pathlib import Path

        from jarvis_voice.sessions import AgentSession

        agent = self.coder(None)
        running = AgentSession("t0", "Typed by hand", "/p/web", 3.0, running=True)
        login = AgentSession("t1", "Fix the login bug", "/p/web", 2.0)
        agent.list_sessions = AsyncMock(return_value=[running, login])
        agent.pick_session = AsyncMock(return_value=True)
        with patch("jarvis_voice.agent_tools.resolve_project", return_value=Path("/p/web")):
            p = params({"agent_id": "brain", "project": "web"})
            await self.handlers["send_agent_task"](p)
        agent.pick_session.assert_awaited_once_with(login)  # never the running one
        agent.send_task.assert_not_awaited()  # no task said: nothing goes out
        p.result_callback.assert_awaited_once_with(
            {"agent_id": "brain", "status": "session_ready", "session": "Fix the login bug", "project": "web"}
        )

    async def test_staged_new_session_is_preserved_on_next_turn(self) -> None:
        from pathlib import Path

        agent = self.coder(None)
        agent.cwd = "/p/web"
        agent._fresh = True
        agent.list_sessions = AsyncMock()
        agent.pick_session = AsyncMock()
        with patch("jarvis_voice.agent_tools.resolve_project", return_value=Path("/p/web")):
            p = params({"agent_id": "brain", "project": "web", "task": "Fix the login"})
            await self.handlers["send_agent_task"](p)
        agent.list_sessions.assert_not_awaited()
        agent.pick_session.assert_not_awaited()
        self.assertTrue(agent._fresh)
        agent.send_task.assert_awaited_once_with("Fix the login")

    async def test_list_sessions_formats_epoch_zero_as_empty(self) -> None:
        from jarvis_voice.sessions import AgentSession

        agent = self.coder("t1")
        agent.list_sessions = AsyncMock(return_value=[AgentSession("t1", "Fresh", "/p/jarvis", 0.0)])
        p = params({"agent_id": "brain"})
        await self.handlers["list_sessions"](p)
        result = p.result_callback.await_args.args[0]
        self.assertEqual(result["sessions"][0]["updated"], "")

    async def test_list_sessions_reads_titles_and_marks_the_current_one(self) -> None:
        from jarvis_voice.sessions import AgentSession

        agent = self.coder("t1")
        agent.list_sessions = AsyncMock(return_value=[
            AgentSession("t1", "Fix Jarvis tone", "/p/jarvis", 2.0, running=True), AgentSession("t2", "Login", "/p/web", 1.0)
        ])
        p = params({"agent_id": "brain"})
        await self.handlers["list_sessions"](p)
        result = p.result_callback.await_args.args[0]
        self.assertEqual((result["status"], result["count"]), ("listed", 2))
        first, second = result["sessions"]
        self.assertEqual((first["title"], first["project"], first["running"], first["current"]),
                         ("Fix Jarvis tone", "jarvis", True, True))
        self.assertEqual(set(second), {"title", "project", "updated"})
        agent.list_sessions.assert_awaited_once_with(None, limit=10)

        p = params({"agent_id": "hermes"})  # not a coding agent: no sessions to list
        await self.handlers["list_sessions"](p)
        self.assertEqual(p.result_callback.await_args.args[0]["status"], "no_sessions")
        self.orch.state.room_turn = True  # a meeting guest never hears the master's session titles
        p = params({"agent_id": "brain"})
        await self.handlers["list_sessions"](p)
        p.result_callback.assert_awaited_once_with({"status": "owner_only"})

    async def test_a_failed_capture_sends_nothing(self) -> None:
        from jarvis_voice.screen import ScreenError

        failed = AsyncMock(side_effect=ScreenError("screen_permission_missing"))
        with patch("jarvis_voice.agent_tools.capture_front_window", failed):
            p = params({"agent_id": "hermes", "task": "Fix this error", "include_screen": True})
            await self.handlers["send_agent_task"](p)
        self.agent.send_task.assert_not_awaited()
        p.result_callback.assert_awaited_once_with({"status": "screen_permission_missing", "agent_id": "hermes"})

    async def test_a_meeting_guest_never_sees_the_screen(self) -> None:
        self.orch.state.room_turn = True
        capture = AsyncMock()
        with patch("jarvis_voice.agent_tools.capture_front_window", capture):
            p = params({"agent_id": "hermes", "task": "Fix this", "include_screen": True})
            await self.handlers["send_agent_task"](p)
            p.result_callback.assert_awaited_once_with({"status": "owner_only", "agent_id": "hermes"})
            p = params({"question": "What is this?"})
            await self.handlers["look_at_screen"](p)
            p.result_callback.assert_awaited_once_with({"status": "owner_only"})
        capture.assert_not_awaited()

    async def test_look_at_screen_returns_the_answer_to_speak(self) -> None:
        self.orch._http = MagicMock()
        shot = MagicMock()
        with (
            patch("jarvis_voice.agent_tools.capture_front_window", AsyncMock(return_value=shot)),
            patch("jarvis_voice.agent_tools.ask_screen", AsyncMock(return_value="It asks to update macOS.")) as ask,
        ):
            p = params({"question": "What does this popup mean?"})
            await self.handlers["look_at_screen"](p)
        ask.assert_awaited_once_with(shot, "What does this popup mean?", self.orch._http)
        p.result_callback.assert_awaited_once_with({"status": "seen", "answer": "It asks to update macOS."})



class TestSessionSwitch(unittest.IsolatedAsyncioTestCase):
    """`session` and `project` on send_agent_task, for the coding clients other than T3 Code."""

    def setUp(self) -> None:
        from pathlib import Path

        from jarvis_voice.sessions import AgentSession, CodingSessionAdapter

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
        with patch("jarvis_voice.agent_tools.resolve_project", new=self.projects.get):
            await self.handlers["send_agent_task"](p)
        return p.result_callback.await_args.args[0]

    async def test_new_session_alone_is_ready_and_sends_nothing(self) -> None:
        self.assertEqual(await self.send(session="new"),
                         {"status": "session_ready", "agent_id": "claude", "session": "new", "project": "jarvis"})
        self.agent.new_session.assert_awaited_once_with(None)
        self.agent.send_task.assert_not_awaited()

    async def test_named_session_is_picked_then_the_task_goes_to_it(self) -> None:
        result = await self.send(session="the speaker verification one", task="lower the threshold")
        self.assertEqual((result["status"], result["session"], result["project"]), ("sent", "Speaker verification", "jarvis"))
        self.agent.pick_session.assert_awaited_once_with(self.voice)
        self.agent.send_task.assert_awaited_once_with("lower the threshold")

    async def test_no_match_unknown_project_and_mid_turn_send_nothing(self) -> None:
        missed = await self.send(session="billing", task="x")
        self.assertEqual(missed["status"], "no_matching_session")
        self.assertEqual(missed["sessions"], ["Fix the login bug", "Speaker verification"])  # titles to offer instead
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
        from jarvis_voice.config import settings

        with patch.object(settings, "meeting_guests", "ask"):
            self.orch.state.room_turn = True
            result = await self.send(task="fix it")
            self.assertEqual((result["status"], result["task"]), ("needs_owner_ok", "fix it"))
            self.agent.send_task.assert_not_awaited()
            self.orch.state.room_turn = False  # the master says yes; the model sends it again
            self.assertEqual((await self.send(task="fix it"))["status"], "sent")
        self.agent.send_task.assert_awaited_once_with("fix it")

    async def test_meeting_guests_allow_runs_guest_tasks(self) -> None:
        from jarvis_voice.config import settings

        self.orch.state.room_turn = True
        with patch.object(settings, "meeting_guests", "allow"):
            self.assertEqual((await self.send(task="fix it"))["status"], "sent")
            self.assertEqual((await self.send(task="fix it", replace=True))["status"], "owner_only")

    async def test_guest_cannot_switch_session_or_project_even_when_guests_allowed(self) -> None:
        from jarvis_voice.config import settings

        self.orch.state.room_turn = True
        with patch.object(settings, "meeting_guests", "allow"):
            self.assertEqual((await self.send(session="new", task="fix it"))["status"], "owner_only")
            self.assertEqual((await self.send(session="login", task="fix it"))["status"], "owner_only")
            self.assertEqual((await self.send(project="web", task="fix it"))["status"], "owner_only")
        self.agent.list_sessions.assert_not_awaited()
        self.agent.pick_session.assert_not_awaited()
        self.agent.new_session.assert_not_awaited()
        self.agent.send_task.assert_not_awaited()


class TestHoldContext(unittest.IsolatedAsyncioTestCase):
    """Multi-turn context scratchpad: explain -> lookup side question -> execute with full context."""

    def setUp(self) -> None:
        from pathlib import Path

        from jarvis_voice.sessions import AgentSession, CodingSessionAdapter
        from jarvis_voice.state import JarvisState

        self.state = JarvisState()
        self.orch = AgentOrchestrator(state=self.state)
        self.orch._http = None
        agent = MagicMock(spec=CodingSessionAdapter)
        agent.config = MagicMock(id="brain")
        agent.busy, agent.connected, agent.cwd, agent.session_id = False, False, "", None
        agent.send_task = AsyncMock(return_value=True)
        agent.pick_session = AsyncMock(return_value=True)
        agent.list_sessions = AsyncMock(return_value=[AgentSession("t1", "Settings bug", "/p/web", 1.0)])
        self.agent = agent
        self.orch.adapters["brain"] = agent
        self.handlers = agent_tool_handlers(self.orch)
        self.projects = {"web": Path("/p/web")}

    async def call(self, tool: str, **arguments) -> dict:
        p = params(arguments)
        with patch("jarvis_voice.agent_tools.resolve_project", new=self.projects.get):
            await self.handlers[tool](p)
        return p.result_callback.await_args.args[0]

    async def test_explain_then_side_lookup_then_fix_preserves_and_consumes_context(self) -> None:
        # 1. User starts explaining the bug across two turns: no agent work runs yet.
        r1 = await self.call("hold_context", project="web", note="Modal closes without saving on settings page")
        self.assertEqual(r1["status"], "held")
        self.assertEqual(r1["project"], "web")
        r2 = await self.call("hold_context", note="Repro: open profile settings, edit email, switch tabs -> hook crash")
        self.assertEqual(len(r2["notes"]), 2)
        self.agent.send_task.assert_not_awaited()

        # 2. User stops mid-explanation to ask the React version: side_question=True keeps notes open.
        lookup = await self.call(
            "send_agent_task", project="web", task="What version of React is used right now?", side_question=True
        )
        self.assertEqual((lookup["status"], lookup["held_notes"]), ("sent", 2))
        self.agent.send_task.assert_awaited_once_with("What version of React is used right now?")
        self.assertTrue(self.state.working_context.active())

        # 3. A failed send_task never loses the held notes.
        self.agent.send_task.reset_mock()
        self.agent.send_task.return_value = False
        failed = await self.call("send_agent_task", task="Fix the React 19 RC unmount crash")
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(self.state.working_context.active())

        # 4. User asks Jarvis to fix it (project omitted -> falls back to held project "web", notes prepended, cleared).
        self.agent.send_task.return_value = True
        done = await self.call("send_agent_task", task="Fix the React 19 RC unmount crash")
        self.assertEqual(done["status"], "sent")
        sent_prompt = self.agent.send_task.await_args.args[0]
        self.assertIn("Modal closes without saving on settings page", sent_prompt)
        self.assertIn("Repro: open profile settings, edit email, switch tabs -> hook crash", sent_prompt)
        self.assertIn("Fix the React 19 RC unmount crash", sent_prompt)
        self.assertFalse(self.state.working_context.active())

    async def test_clear_and_guest_isolation(self) -> None:
        from jarvis_voice.config import settings

        await self.call("hold_context", project="web", note="Draft bug note")
        self.state.room_turn = True
        self.assertEqual((await self.call("hold_context", note="Guest injection"))["status"], "owner_only")
        with patch.object(settings, "meeting_guests", "allow"):
            await self.call("send_agent_task", task="What time is the deploy?")
        # Guest task neither receives nor clears the owner's held notes.
        self.assertEqual(self.agent.send_task.await_args.args[0], "What time is the deploy?")
        self.assertEqual(self.state.working_context.notes, ["Draft bug note"])

        self.state.room_turn = False
        self.assertEqual((await self.call("hold_context", clear=True))["status"], "cleared")
        self.assertFalse(self.state.working_context.active())


class TestRenameSpeakerTool(unittest.IsolatedAsyncioTestCase):
    async def test_owner_and_guest_rename_rules_and_live_telemetry(self) -> None:
        import tempfile
        from pathlib import Path

        import numpy as np

        from jarvis_core.soul import load_soul, owner_name, remember_owner_name
        from jarvis_voice import telemetry
        from jarvis_voice.speaker import SpeakerRegistry
        from jarvis_voice.state import JarvisState

        events: list[dict] = []
        telemetry.bind(lambda kind, **fields: events.append({"kind": kind, **fields}))
        self.addCleanup(lambda: telemetry.bind(None))

        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            remember_owner_name("Song", soul_path)
            registry = SpeakerRegistry(folder=Path(tmp) / "speakers", owner_name="Song", soul_path=soul_path)
            p1 = registry.observe(np.array([0.0, 1.0], dtype=np.float32), threshold=0.5)
            p2 = registry.observe(np.array([1.0, 0.0], dtype=np.float32), threshold=0.5)
            assert p1 is not None and p2 is not None
            registry.link_name(p1.id, "Davd", enroll=True)

            state = JarvisState()
            orch = AgentOrchestrator(state=state)
            handlers = agent_tool_handlers(orch, registry=registry)

            async def rename(**kwargs) -> dict:
                p = params(kwargs)
                await handlers["rename_speaker"](p)
                return p.result_callback.await_args.args[0]

            # 1. Owner renames themselves -> SOUL.md + RAM + immediate telemetry with renamed=True.
            r = await rename(name="Sang")
            self.assertEqual(r, {"status": "renamed", "target": "owner", "previous": "Song", "name": "Sang"})
            self.assertEqual(owner_name(load_soul(soul_path).personality), "Sang")
            self.assertEqual(events[-1]["kind"], "voice.speaker")
            self.assertEqual((events[-1]["profile_id"], events[-1]["label"], events[-1]["renamed"]), ("owner", "Sang", True))

            # 2. Owner renames enrolled guest and ephemeral Speaker 2.
            r_guest = await rename(target="Davd", name="David")
            self.assertEqual(r_guest["status"], "renamed")
            self.assertEqual(registry.get("voice_1").name, "David")  # type: ignore[union-attr]
            r_spk2 = await rename(target="Speaker 2", name="Sarah")
            self.assertEqual(r_spk2["status"], "renamed")
            self.assertTrue(registry.get("voice_2").enrolled)  # type: ignore[union-attr]

            # 3. Guest trust boundaries: cannot rename owner, cannot rename another guest, cannot hijack another guest.
            state.room_turn = True
            state.current_speaker_id = "voice_1"
            state.current_speaker = "Speaker: David"
            self.assertEqual((await rename(target="owner", name="Hacker"))["status"], "owner_only")
            self.assertEqual((await rename(target="Sang", name="Hacker"))["status"], "owner_only")
            self.assertEqual((await rename(target="Sarah", name="Mallory"))["status"], "owner_only")
            self.assertEqual((await rename(name="Sang"))["status"], "owner_only")
            self.assertEqual((await rename(name="Sarah"))["status"], "owner_only")

            # 4. Enrolled guest CAN fix their own name; updates state.current_speaker and speakers.json.
            r_self = await rename(name="Daniel")
            self.assertEqual(r_self, {"status": "renamed", "target": "voice_1", "previous": "David", "name": "Daniel"})
            self.assertEqual(state.current_speaker, "Speaker: Daniel")
            self.assertEqual(SpeakerRegistry(folder=Path(tmp) / "speakers").get("voice_1").name, "Daniel")  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
