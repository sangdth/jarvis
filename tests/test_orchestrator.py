import unittest
from unittest.mock import AsyncMock, MagicMock, patch


from fakes import fake_adapter
from jarvis.orchestrator import WAITING_LINES, AgentOrchestrator
from jarvis.state import AgentTopic, JarvisState


class TestAgentRouting(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.orch = AgentOrchestrator(state=MagicMock())

    def test_roster_lists_exact_ids_and_default(self) -> None:
        from jarvis.main import agent_roster

        self.assertIn("No coding agents", agent_roster(self.orch))
        self.orch.adapters["opencode"] = fake_adapter("opencode")
        with patch("jarvis.orchestrator.settings.default_coding_agent", ""):
            roster = agent_roster(self.orch)
        self.assertIn("id `opencode` (DEFAULT for coding): Agent One", roster)
        self.assertIn("a coding task goes to `opencode`", roster)
        self.orch.adapters["hermes"] = fake_adapter("hermes", "Hermes", role="everything that is not coding")
        self.assertIn("id `hermes`: Hermes — everything that is not coding", agent_roster(self.orch))

    def test_default_agent_gets_unnamed_tasks_and_exact_ids_only(self) -> None:
        self.orch.adapters["claude"] = fake_adapter("claude")
        self.orch.adapters["opencode"] = fake_adapter("opencode")
        with patch("jarvis.orchestrator.settings.default_coding_agent", "opencode"):
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
            with patch("jarvis.orchestrator.settings.default_coding_agent", "opencod"):
                with self.assertRaisesRegex(ValueError, "opencod"):
                    self.orch.load_from_file(f.name)

    def test_answer_and_stop_without_id_find_the_right_agent(self) -> None:
        state = JarvisState()
        orch = AgentOrchestrator(state=state)
        claude, hermes = fake_adapter("claude"), fake_adapter("hermes")
        claude.busy, hermes.busy = False, True
        orch.adapters.update(claude=claude, hermes=hermes)
        with patch("jarvis.orchestrator.settings.default_coding_agent", "claude"):
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



class TestRemindWhileBusy(unittest.IsolatedAsyncioTestCase):
    async def test_fibonacci_waits_and_skips_while_agent_asks(self) -> None:
        from jarvis.state import JarvisMode

        state = JarvisState(on_speak=AsyncMock())
        orch = AgentOrchestrator(state=state)
        agent = MagicMock()
        agent.config.id, agent.config.name = "hermes", "Hermes"
        busy = iter([True, True, True, True, False])  # done at the 5th check
        type(agent).busy = property(lambda _: next(busy))
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

        with patch("jarvis.orchestrator.asyncio.sleep", fake_sleep):
            await orch._remind(agent)

        self.assertEqual(waits, [8, 13, 21, 34, 55])
        self.assertEqual(state.on_speak.await_count, 2)  # after 8s and after 34s
        first, second = (c.args[0] for c in state.on_speak.await_args_list)
        self.assertNotEqual(first, second)  # varied, never the same line twice in a row
        self.assertLessEqual({first, second}, {t.format(name="Hermes") for t in WAITING_LINES})


if __name__ == "__main__":
    unittest.main()
