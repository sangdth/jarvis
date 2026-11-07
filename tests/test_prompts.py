import unittest
from datetime import datetime
from unittest.mock import MagicMock

from jarvis.prompts import JARVIS_EXECUTIVE_SYSTEM_PROMPT, greeting_prompt, system_prompt


class PromptTest(unittest.TestCase):
    def test_prompt_adds_personality_only_when_set(self):
        orchestrator = MagicMock(adapters={})
        self.assertNotIn("personality", system_prompt(orchestrator, ""))
        prompt = system_prompt(orchestrator, "Personality: grumpy.")
        self.assertTrue(prompt.startswith(JARVIS_EXECUTIVE_SYSTEM_PROMPT))
        self.assertIn("Personality: grumpy.", prompt)

    def test_prompt_names_the_connected_thread_only_when_connected(self):
        orchestrator = MagicMock(adapters={})
        self.assertNotIn("You are connected to", system_prompt(orchestrator, ""))
        thread = {"title": "Fix login", "project": "platform-v2", "agent_id": "t3-code", "agent_name": "T3 Code"}
        prompt = system_prompt(orchestrator, "", thread)
        self.assertIn('T3 Code session "Fix login" in platform-v2', prompt)
        self.assertIn("send_agent_task for `t3-code`", prompt)


class TestGreeting(unittest.TestCase):
    def test_greeting_has_time_and_memories(self) -> None:
        prompt = greeting_prompt(datetime(2026, 11, 6, 8, 5), ["Linh's recital is Saturday at 4pm"])
        self.assertIn("Friday 08:05", prompt)
        self.assertIn("Linh's recital is Saturday at 4pm", prompt)
        self.assertIn("Do not call any tool", prompt)
        self.assertNotIn("remember", greeting_prompt(datetime(2026, 11, 6, 8, 5), []))


if __name__ == "__main__":
    unittest.main()
