"""Fakes shared by the adapter and orchestrator tests."""

from unittest.mock import AsyncMock, MagicMock

from jarvis.agent_adapter import WatchedAgentConfig


def make(adapter_cls, agent_type: str):
    cfg = WatchedAgentConfig(id="a1", name="Agent One", type=agent_type, cwd="/tmp")
    adapter = adapter_cls(cfg, state=MagicMock())
    adapter._surface = AsyncMock()
    return adapter


def fake_adapter(agent_id: str, name: str = "Agent One", role: str = "") -> MagicMock:
    adapter = MagicMock()
    adapter.config.id = agent_id
    adapter.config.name = name
    adapter.config.role = role
    return adapter


QUESTION_FORM = {
    "id": "frm_1",
    "sessionID": "ses_1",
    "title": "Questions",
    "fields": [
        {
            "key": "q0",
            "title": "Next step",
            "description": "Which should come next: tests or docs?",
            "type": "string",
            "options": [{"value": "Tests", "label": "Tests"}, {"value": "Docs", "label": "Docs"}],
            "custom": True,
        }
    ],
}
