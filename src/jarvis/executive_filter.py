from __future__ import annotations

import json
from loguru import logger
import httpx

from jarvis.config import settings
from jarvis.state import AgentTopic, UserIntent, classify_fast_command


EXECUTIVE_FILTER_SYSTEM_PROMPT = """You are Jarvis's Executive Attention Filter.
You monitor verbose raw output from coding agents (Claude Code, OpenCode, Pi) on behalf of a busy software architect.

Your job is to decide whether the agent's latest event requires the user's immediate verbal attention or a decision.

Rules:
1. Return `should_interrupt: false` if the agent is merely:
   - reading files, running routine tests, editing code, or making normal progress
   - reporting verbose stack traces or build logs that it is already attempting to fix on its own
2. Return `should_interrupt: true` ONLY if:
   - the agent hit a blocker or permission prompt that requires human approval
   - the agent asks an architectural / product trade-off question
   - the agent finished a major milestone and needs review or next steps
3. When `should_interrupt: true`, write `spoken_summary`:
   - MAXIMUM 2 short conversational sentences meant to be spoken aloud.
   - ZERO file paths, ZERO function names, ZERO code blocks, ZERO markdown, ZERO technical jargon unless essential.
   - Frame it clearly around the decision needed (e.g., "The auth agent finished the migration, but wants to know if we should drop the legacy session table now or keep it for backwards compatibility.")

Respond strictly in JSON matching:
{"should_interrupt": boolean, "requires_decision": boolean, "spoken_summary": string}
"""

ADDRESSED_CLASSIFIER_SYSTEM_PROMPT = """You are the Always-On Turn Classifier for a voice assistant named Jarvis.
There is NO wake word required, so you receive everything the user says near their microphone.

Determine if the user is speaking to Jarvis (or answering a question Jarvis just asked about a coding agent), or if they are just muttering to themselves / talking to someone else on a call / reading code aloud.

Respond strictly with one word:
- `MUTE` if they want Jarvis to be quiet / shut up
- `UNMUTE` if they tell Jarvis it can talk again
- `ADDRESSED_TO_JARVIS` if they are asking Jarvis a question, giving an instruction to an agent, or replying to Jarvis
- `IGNORE_BACKGROUND` if they are talking to someone else, thinking out loud ("hmm let me see..."), or saying filler words
"""


class ExecutiveFilter:
    """Distills verbose coding agent output into concise spoken summaries and classifies user turns."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or settings.anthropic_api_key
        self.model = model or settings.filter_model

    async def evaluate_agent_event(
        self,
        agent_id: str,
        agent_name: str,
        raw_event_text: str,
        force_decision: bool = False,
    ) -> AgentTopic | None:
        """Analyze raw output from a coding agent and return an AgentTopic only if worthy of attention."""
        cleaned = raw_event_text.strip()
        if not cleaned:
            return None

        # Fallback heuristic when no LLM API key is configured yet
        if not self.api_key:
            logger.debug("No ANTHROPIC_API_KEY configured; using heuristic executive filter.")
            lower = cleaned.lower()
            triggers = ("?", "permission", "approve", "should i", "blocked", "error:", "completed")
            if force_decision or any(t in lower for t in triggers):
                short = cleaned.splitlines()[-1][:180]
                return AgentTopic(
                    agent_id=agent_id,
                    agent_name=agent_name,
                    summary=f"Needs your input: {short}",
                    raw_context=cleaned,
                    requires_decision=True,
                )
            return None

        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "max_tokens": 250,
                        "system": EXECUTIVE_FILTER_SYSTEM_PROMPT,
                        "messages": [
                            {
                                "role": "user",
                                "content": (
                                    f"Agent Name: {agent_name} (id={agent_id})\n"
                                    f"Explicit Permission/Question Flag: {force_decision}\n"
                                    f"Raw Agent Output:\n{cleaned[-4000:]}"
                                ),
                            }
                        ],
                    },
                )
                resp.raise_for_status()
                content = resp.json()["content"][0]["text"].strip()
                # Extract JSON object if wrapped in code fences
                if "```" in content:
                    content = content.split("```")[1].removeprefix("json").strip()
                parsed = json.loads(content)

                if not parsed.get("should_interrupt"):
                    logger.debug(f"[{agent_id}] Filtered out routine output.")
                    return None

                return AgentTopic(
                    agent_id=agent_id,
                    agent_name=agent_name,
                    summary=parsed.get("spoken_summary", "Needs your attention."),
                    raw_context=cleaned,
                    requires_decision=bool(parsed.get("requires_decision", True)),
                )
        except Exception as exc:
            logger.warning(f"Executive filter LLM call failed ({exc}); falling back to heuristic.")
            if force_decision:
                return AgentTopic(
                    agent_id=agent_id,
                    agent_name=agent_name,
                    summary="Finished a step and is waiting for your decision.",
                    raw_context=cleaned,
                    requires_decision=True,
                )
            return None

    async def classify_user_utterance(
        self,
        transcript: str,
        recent_jarvis_question: str | None = None,
    ) -> UserIntent:
        """Determine if an un-waked utterance is meant for Jarvis or should be ignored."""
        fast = classify_fast_command(transcript)
        if fast is not None:
            return fast

        if not self.api_key:
            # Heuristic fallback when running offline
            return UserIntent.ADDRESSED_TO_JARVIS

        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "max_tokens": 20,
                        "system": ADDRESSED_CLASSIFIER_SYSTEM_PROMPT,
                        "messages": [
                            {
                                "role": "user",
                                "content": (
                                    f"Recent Jarvis Topic/Question: {recent_jarvis_question or 'None'}\n"
                                    f"User Utterance: {transcript}"
                                ),
                            }
                        ],
                    },
                )
                resp.raise_for_status()
                label = resp.json()["content"][0]["text"].strip().upper()
                for intent in UserIntent:
                    if intent.value in label:
                        return intent
        except Exception as exc:
            logger.warning(f"Turn classifier failed ({exc}); defaulting to ADDRESSED_TO_JARVIS.")

        return UserIntent.ADDRESSED_TO_JARVIS
