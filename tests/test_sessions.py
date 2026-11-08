import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fakes import fake_adapter, make
from jarvis.claude_adapter import ClaudeCodeAdapter
from jarvis.orchestrator import AgentOrchestrator
from jarvis.sessions import AgentSession, match_sessions
from jarvis.t3_adapter import T3Adapter

AUTH = AgentSession("s1", "Refactor the auth service", "/p/platform", 100.0, "feat/jwt")
VOICE = AgentSession("s2", "Speaker verification threshold", "/p/jarvis", 200.0)
VOICE_OLD = AgentSession("s3", "speaker enrollment", "/p/jarvis", 50.0)


class TestMatchSessions(unittest.TestCase):
    def test_most_shared_words_first_then_newest(self) -> None:
        self.assertEqual(match_sessions([AUTH, VOICE, VOICE_OLD], "the session about speaker verification"),
                         [VOICE, VOICE_OLD])
        self.assertEqual(match_sessions([AUTH, VOICE, VOICE_OLD], "the jarvis one"), [VOICE, VOICE_OLD])

    def test_branch_and_folder_count_and_filler_matches_nothing(self) -> None:
        self.assertEqual(match_sessions([AUTH, VOICE], "jwt"), [AUTH])
        self.assertEqual(match_sessions([AUTH, VOICE], "platform"), [AUTH])
        self.assertEqual(match_sessions([AUTH, VOICE], "continue the session from yesterday"), [])


class TestConnectedAndT3(unittest.TestCase):
    def test_a_connected_agent_is_the_default_and_t3_is_found_by_type(self) -> None:
        orch = AgentOrchestrator(state=MagicMock())
        claude, t3 = fake_adapter("claude"), MagicMock(spec=T3Adapter)
        t3.connected = False
        orch.adapters.update(claude=claude, **{"t3-code": t3})
        self.assertIs(orch.t3, t3)
        with patch("jarvis.orchestrator.settings.default_coding_agent", "t3-code"):
            self.assertEqual(orch.default_agent_id, "t3-code")
            claude.connected = True  # `jarvis connect claude`
            self.assertEqual(orch.default_agent_id, "claude")
        orch.adapters.pop("t3-code")
        self.assertIsNone(orch.t3)


class TestConnectPicker(unittest.IsolatedAsyncioTestCase):
    async def test_connect_continues_the_session_picked_at_the_keyboard(self) -> None:
        from jarvis import main

        orch = AgentOrchestrator(state=MagicMock())
        claude = make(ClaudeCodeAdapter, "claude_code")
        orch.adapters["a1"] = claude
        claude.list_sessions = AsyncMock(return_value=[VOICE, AUTH])
        with patch.object(main, "choose", return_value=1):
            thread = await main.pick_connection(orch, "a1")
        self.assertEqual(thread, {"id": "s1", "title": AUTH.title, "project": "platform", "agent_id": "a1",
                                  "agent_name": "Agent One"})
        self.assertTrue(claude.connected)
        self.assertEqual((claude._session_id, claude.cwd), ("s1", "/p/platform"))  # resumed when start_all runs

    async def test_connect_to_an_agent_without_sessions_stops_with_a_reason(self) -> None:
        from jarvis import main

        orch = AgentOrchestrator(state=MagicMock())
        orch.adapters["hermes"] = fake_adapter("hermes")
        with self.assertRaisesRegex(SystemExit, "not a coding agent"):
            await main.pick_connection(orch, "hermes")


if __name__ == "__main__":
    unittest.main()
