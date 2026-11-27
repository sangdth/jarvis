import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fakes import fake_adapter

from jarvis_voice.agent_adapter import WatchedAgentConfig
from jarvis_voice.brain_client import BrainClientAdapter
from jarvis_voice.orchestrator import AgentOrchestrator
from jarvis_voice.sessions import AgentSession, CodingSessionAdapter, match_sessions

AUTH = AgentSession("s1", "Refactor the auth service", "/p/platform", 100.0, "feat/jwt")
VOICE = AgentSession("s2", "Speaker verification threshold", "/p/jarvis", 200.0)
VOICE_OLD = AgentSession("s3", "speaker enrollment", "/p/jarvis", 50.0)


class _FakeCodingAdapter(CodingSessionAdapter):
    def __init__(self, agent_id: str = "a1", name: str = "Agent One") -> None:
        super().__init__(WatchedAgentConfig(id=agent_id, name=name, type="fake", cwd="/tmp"), state=MagicMock())
        self._session_id: str | None = None
        self.sessions: list[AgentSession] = []

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def list_sessions(self, cwd: str | None = None, limit: int = 20) -> list[AgentSession]:
        return self.sessions[:limit]

    async def pick_session(self, session: AgentSession, *, connected: bool = False) -> bool:
        self._session_id = session.id
        self.cwd = session.cwd
        self.connected = connected
        return True

    async def new_session(self, cwd: str | None = None) -> bool:
        self._session_id = None
        if cwd:
            self.cwd = cwd
        return True

    async def send_task(self, task: str) -> bool:
        return True

    async def answer(self, text: str) -> bool:
        return False

    async def interrupt(self) -> bool:
        return True

    async def _run_watcher(self) -> None:
        return None


class TestMatchSessions(unittest.TestCase):
    def test_most_shared_words_first_then_newest(self) -> None:
        self.assertEqual(match_sessions([AUTH, VOICE, VOICE_OLD], "the session about speaker verification"),
                         [VOICE, VOICE_OLD])
        self.assertEqual(match_sessions([AUTH, VOICE, VOICE_OLD], "the jarvis one"), [VOICE, VOICE_OLD])

    def test_branch_and_folder_count_and_filler_matches_nothing(self) -> None:
        self.assertEqual(match_sessions([AUTH, VOICE], "jwt"), [AUTH])
        self.assertEqual(match_sessions([AUTH, VOICE], "platform"), [AUTH])
        self.assertEqual(match_sessions([AUTH, VOICE], "continue the session from yesterday"), [])
        self.assertEqual(match_sessions([AUTH, VOICE], "open the thread in my section"), [])

    def test_short_words_and_word_starts_count(self) -> None:
        pr = AgentSession("s4", "Review PR 42 for the UI", "/p/web", 10.0)
        self.assertEqual(match_sessions([AUTH, VOICE, pr], "the pr one"), [pr])
        self.assertEqual(match_sessions([AUTH, VOICE, pr], "authentication"), [AUTH])  # "auth" starts it
        self.assertEqual(match_sessions([AUTH, VOICE, pr], "refactoring"), [AUTH])
        self.assertEqual(match_sessions([AUTH, VOICE, pr], "speak"), [VOICE])  # "speaker"


class TestConnectedDefault(unittest.TestCase):
    def test_a_connected_agent_is_the_default(self) -> None:
        orch = AgentOrchestrator(state=MagicMock())
        pi, brain = fake_adapter("pi"), MagicMock(spec=BrainClientAdapter)
        brain.connected = False
        orch.adapters.update(pi=pi, brain=brain)
        with patch("jarvis_voice.orchestrator.settings.default_coding_agent", "brain"):
            self.assertEqual(orch.default_agent_id, "brain")
            pi.connected = True  # `jarvis connect pi`
            self.assertEqual(orch.default_agent_id, "pi")


class TestConnectPicker(unittest.IsolatedAsyncioTestCase):
    async def test_connect_continues_the_session_picked_at_the_keyboard(self) -> None:
        from jarvis_voice import main

        orch = AgentOrchestrator(state=MagicMock())
        coder = _FakeCodingAdapter("a1", "Agent One")
        coder.sessions = [VOICE, AUTH]
        orch.adapters["a1"] = coder
        with patch.object(main, "choose", return_value=1):
            thread = await main.pick_connection(orch, "a1")
        self.assertEqual(thread, {"id": "s1", "title": AUTH.title, "project": "platform", "agent_id": "a1",
                                  "agent_name": "Agent One"})
        self.assertTrue(coder.connected)
        self.assertEqual((coder.session_id, coder.cwd), ("s1", "/p/platform"))

    async def test_connect_routes_named_runners_through_brain(self) -> None:
        from jarvis_voice import main

        orch = AgentOrchestrator(state=MagicMock())
        brain = MagicMock(spec=BrainClientAdapter)
        brain.config = WatchedAgentConfig(id="brain", name="Brain", type="brain", cwd="/tmp")
        brain.runner = "t3"
        brain.list_sessions = AsyncMock(return_value=[VOICE, AUTH])
        brain.pick_session = AsyncMock(return_value=True)
        orch.adapters["brain"] = brain

        with patch.object(main, "choose", return_value=1):
            picked = await main.pick_connection(orch, "claude")
        self.assertEqual(brain.runner, "claude")
        brain.pick_session.assert_awaited_once_with(AUTH, connected=True)
        self.assertEqual(picked["agent_id"], "brain")
        self.assertEqual(picked["id"], "s1")

    async def test_connect_to_an_agent_without_sessions_stops_with_a_reason(self) -> None:
        from jarvis_voice import main

        orch = AgentOrchestrator(state=MagicMock())
        orch.adapters["hermes"] = fake_adapter("hermes")
        with self.assertRaisesRegex(SystemExit, "not a coding agent"):
            await main.pick_connection(orch, "hermes")

    def test_thread_for_this_folder_prefers_worktree_over_project_root(self) -> None:
        from jarvis_voice import main

        with tempfile.TemporaryDirectory() as tmp:
            root = (Path(tmp) / "jarvis").resolve()
            worktree = (Path(tmp) / "wt-fix").resolve()
            root.mkdir()
            worktree.mkdir()
            sessions = [
                AgentSession("t-main", "Main", str(root), 10.0),
                AgentSession("t-wt", "Worktree fix", str(worktree), 20.0),
            ]
            with patch.dict(
                main.os.environ,
                {"T3CODE_PROJECT_ROOT": str(root), "T3CODE_WORKTREE_PATH": str(worktree)},
                clear=False,
            ):
                self.assertEqual(main._thread_for_this_folder(sessions), 1)

            with patch.dict(
                main.os.environ,
                {"T3CODE_PROJECT_ROOT": str(root), "T3CODE_WORKTREE_PATH": ""},
                clear=False,
            ):
                self.assertEqual(main._thread_for_this_folder(sessions), 0)
                older_updated_but_just_visited = AgentSession(
                    "t-visited", "Just clicked", str(root), 5.0, visited_at=500.0
                )
                self.assertEqual(
                    main._thread_for_this_folder([sessions[0], older_updated_but_just_visited]), 1
                )

    async def test_connect_attaches_worktree_thread_without_prompting(self) -> None:
        from jarvis_voice import main

        with tempfile.TemporaryDirectory() as tmp:
            root = (Path(tmp) / "jarvis").resolve()
            worktree = (Path(tmp) / "wt-fix").resolve()
            root.mkdir()
            worktree.mkdir()
            main_sess = AgentSession("t-main", "Main", str(root), 10.0)
            wt_sess = AgentSession("t-wt", "Worktree fix", str(worktree), 20.0)

            orch = AgentOrchestrator(state=MagicMock())
            brain = MagicMock(spec=BrainClientAdapter)
            brain.config = WatchedAgentConfig(id="brain", name="Brain", type="brain", cwd="/tmp")
            brain.runner = "t3"
            brain.list_sessions = AsyncMock(return_value=[main_sess, wt_sess])
            brain.pick_session = AsyncMock(return_value=True)
            orch.adapters["brain"] = brain
            with (
                patch.dict(
                    main.os.environ,
                    {"T3CODE_PROJECT_ROOT": str(root), "T3CODE_WORKTREE_PATH": str(worktree)},
                ),
                patch.object(main, "choose", side_effect=AssertionError("should not prompt")),
            ):
                picked = await main.pick_connection(orch, "t3")
            brain.pick_session.assert_awaited_once_with(wt_sess, connected=True)
            self.assertEqual(picked["id"], "t-wt")
            self.assertEqual(picked["agent_id"], "brain")


if __name__ == "__main__":
    unittest.main()
