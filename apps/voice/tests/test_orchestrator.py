import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fakes import fake_adapter

from jarvis_voice.orchestrator import AgentOrchestrator
from jarvis_voice.prompts import LANGUAGES
from jarvis_voice.state import AgentTopic, JarvisState
from jarvis_voice.waiting_lines import WaitingLines


class TestAgentRouting(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.orch = AgentOrchestrator(state=MagicMock())

    def test_roster_lists_exact_ids_and_default(self) -> None:
        from jarvis_voice.prompts import agent_roster

        self.assertIn("No coding agents", agent_roster(self.orch))
        self.orch.adapters["opencode"] = fake_adapter("opencode")
        with patch("jarvis_voice.orchestrator.settings.default_coding_agent", ""):
            roster = agent_roster(self.orch)
        self.assertIn("id `opencode` (DEFAULT for coding): Agent One", roster)
        self.assertIn("There is one agent: `opencode`", roster)
        self.orch.adapters["brain"] = fake_adapter("brain", "Brain", role="everything that needs doing")
        self.assertIn("id `brain`", agent_roster(self.orch))

    def test_default_agent_gets_unnamed_tasks_and_exact_ids_only(self) -> None:
        self.orch.adapters["claude"] = fake_adapter("claude")
        self.orch.adapters["opencode"] = fake_adapter("opencode")
        with patch("jarvis_voice.orchestrator.settings.default_coding_agent", "opencode"):
            self.assertIs(self.orch.find(None), self.orch.adapters["opencode"])
            self.assertIs(self.orch.find("claude"), self.orch.adapters["claude"])
            # A guessed id like "code" must not fall onto some agent
            self.assertIsNone(self.orch.find("code"))

    def test_unknown_default_stops_startup(self) -> None:
        import json
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
            json.dump({"agents": [{"id": "claude", "name": "Claude", "type": "claude_code"}]}, f)
            f.flush()
            with patch("jarvis_voice.orchestrator.settings.default_coding_agent", "opencod"):
                with self.assertRaisesRegex(ValueError, "opencod"):
                    self.orch.load_from_file(f.name)

    def test_answer_and_stop_without_id_find_the_right_agent(self) -> None:
        state = JarvisState()
        orch = AgentOrchestrator(state=state)
        claude, hermes = fake_adapter("claude"), fake_adapter("hermes")
        claude.busy, hermes.busy = False, True
        orch.adapters.update(claude=claude, hermes=hermes)
        with patch("jarvis_voice.orchestrator.settings.default_coding_agent", "claude"):
            self.assertIs(orch.find_waiting(None), claude)  # nobody asked: the default
            state.active_agent_context["hermes"] = AgentTopic("hermes", "Hermes", "needs approval: rm x")
            self.assertIs(orch.find_waiting(None), hermes)  # Hermes asked, so "yes" is for Hermes
            self.assertIs(orch.find_busy(None), hermes)  # Hermes is the one working
            self.assertIs(orch.find_waiting("claude"), claude)  # a named agent always wins

    async def test_agent_asking_again_counts_as_asked_last(self) -> None:
        state = JarvisState()
        orch = AgentOrchestrator(state=state)
        claude, hermes = fake_adapter("claude"), fake_adapter("hermes")
        orch.adapters.update(claude=claude, hermes=hermes)
        for agent_id in ("hermes", "claude", "hermes"):
            await state.notify_topic(AgentTopic(agent_id, agent_id, "needs approval: x"))
        self.assertIs(orch.find_waiting(None), hermes)

    async def test_start_all_with_connected_agent_skips_other_coding_watchers(self) -> None:
        from jarvis_voice.sessions import CodingSessionAdapter
        from jarvis_voice.t3_adapter import T3Adapter

        t3 = MagicMock(spec=T3Adapter, connected=True, start=AsyncMock())
        opencode = MagicMock(spec=CodingSessionAdapter, connected=False, start=AsyncMock())
        hermes = fake_adapter("hermes")
        hermes.start = AsyncMock()
        self.orch.adapters.update({"t3-code": t3, "opencode": opencode, "hermes": hermes})
        await self.orch.start_all()
        t3.start.assert_awaited_once()
        hermes.start.assert_awaited_once()
        opencode.start.assert_not_awaited()



class TestRemindWhileBusy(unittest.IsolatedAsyncioTestCase):
    async def test_fibonacci_waits_and_skips_while_agent_asks(self) -> None:
        from jarvis_voice.state import JarvisMode

        state = JarvisState(on_speak=AsyncMock())
        orch = AgentOrchestrator(state=state)
        agent = MagicMock()
        agent.config.id, agent.config.name = "hermes", "Hermes"
        agent.busy = True
        waits: list[int] = []

        async def fake_sleep(seconds: int) -> None:
            waits.append(seconds)
            if len(waits) == 2:  # agent asks the user something: no "still waiting" then
                state.active_agent_context["hermes"] = AgentTopic("hermes", "Hermes", "asks: ?")
            elif len(waits) == 3:
                state.active_agent_context.clear()
                state.mode = JarvisMode.BEEP_ONLY  # muted: no reminder
            elif len(waits) == 4:
                state.mode = JarvisMode.VOICE
            elif len(waits) == 5:
                agent.busy = False

        with patch("jarvis_voice.orchestrator.asyncio.sleep", fake_sleep):
            await orch._remind(agent, WaitingLines("check my calendar"))  # no client: the base lines

        self.assertEqual(waits, [8, 13, 21, 34, 55])
        self.assertEqual(state.on_speak.await_count, 2)  # after 8s and after 34s
        first, second = (c.args[0] for c in state.on_speak.await_args_list)
        self.assertNotEqual(first, second)  # varied, never the same line twice in a row
        self.assertLessEqual({first, second}, set(LANGUAGES["en"]["waiting"]))

    async def test_remind_skips_speaking_if_state_changes_during_lines_next(self) -> None:
        from jarvis_voice.state import JarvisMode

        for change in ("finish", "mute", "user_speaking"):
            with self.subTest(change=change):
                state = JarvisState(on_speak=AsyncMock())
                orch = AgentOrchestrator(state=state)
                agent = MagicMock()
                agent.config.id, agent.busy = "hermes", True
                lines = MagicMock(spec=WaitingLines)

                async def slow_next() -> str:
                    if change == "finish":
                        agent.busy = False
                    elif change == "user_speaking":
                        state.user_speaking = True
                    else:
                        state.mode = JarvisMode.BEEP_ONLY
                    return "Still on it."

                lines.next = AsyncMock(side_effect=slow_next)
                calls = 0

                async def fake_sleep(_: int) -> None:
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        agent.busy = False

                with patch("jarvis_voice.orchestrator.asyncio.sleep", fake_sleep):
                    await orch._remind(agent, lines)
                state.on_speak.assert_not_awaited()
                lines.stop.assert_called_once()

    async def test_stop_all_cancels_active_reminders(self) -> None:
        import asyncio

        orch = AgentOrchestrator(state=JarvisState())
        orch._http = None
        agent = fake_adapter("hermes")
        agent.stop = AsyncMock()
        orch.adapters["hermes"] = agent
        orch.remind_while_busy(agent, "check my calendar")
        task, lines = orch._reminders["hermes"]
        lines.stop = MagicMock(wraps=lines.stop)

        await orch.stop_all()
        await asyncio.sleep(0)

        self.assertEqual(orch._reminders, {})
        self.assertTrue(task.cancelled())
        self.assertGreaterEqual(lines.stop.call_count, 1)
        agent.stop.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
