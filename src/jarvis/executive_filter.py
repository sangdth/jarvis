from __future__ import annotations

import json
from loguru import logger
import httpx

from jarvis.config import settings
from jarvis.state import AgentTopic, UserIntent, classify_fast_command


EXECUTIVE_FILTER_SYSTEM_PROMPT = """You are Jarvis's Executive Attention Filter.
You monitor verbose raw output from coding agents (Claude Code, OpenCode) on behalf of a busy software architect.

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
   - Do not start with the agent's name; Jarvis announces it before your summary. Never call the agent by a role you guess (like "the auth agent").
   - Frame it clearly around the decision needed (e.g., "Finished the migration, and wants to know if we should drop the legacy session table now or keep it for backwards compatibility.")

Respond strictly in JSON matching:
{"should_interrupt": boolean, "spoken_summary": string}
"""

ADDRESSED_CLASSIFIER_SYSTEM_PROMPT = """You are the Always-On Turn Classifier for a voice assistant named Jarvis.
There is NO wake word required, so you receive everything the user says near their microphone.

Determine if the user is speaking to Jarvis (or answering a question Jarvis just asked about a coding agent), or if they are just muttering to themselves / talking to someone else on a call / reading code aloud.

Respond strictly with one word:
- `MUTE` if they want Jarvis to be quiet / shut up
- `UNMUTE` if they tell Jarvis it can talk again
- `ADDRESSED_TO_JARVIS` if they are asking Jarvis a question, asking Jarvis to code/build something, giving an instruction to an agent, or replying to Jarvis
- `IGNORE_BACKGROUND` if they are talking to someone else, thinking out loud ("hmm let me see..."), or saying filler words
"""


class ExecutiveFilter:
    """Distills verbose coding agent output into concise spoken summaries and classifies user turns."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.api_key = settings.openrouter_api_key if api_key is None else api_key
        self.model = model or settings.filter_model
        self.base_url = (base_url or settings.openrouter_base_url).rstrip("/")
        # One long-lived client keeps the TLS connection warm between calls.
        self._http = httpx.AsyncClient(
            timeout=12.0,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "HTTP-Referer": "https://github.com/sangdth/jarvis",
                "X-Title": "Jarvis",
            },
        )
        if not self.api_key:
            logger.warning("OPENROUTER_API_KEY is not set; executive filter runs on offline heuristics.")

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call_llm(
        self,
        system_prompt: str,
        user_content: str,
        max_tokens: int = 250,
        temperature: float = 0.0,
    ) -> str:
        """Call the filter model through OpenRouter chat/completions."""
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY is not configured.")
        resp = await self._http.post(
            f"{self.base_url}/chat/completions",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                "max_tokens": max_tokens,
                "temperature": temperature,
                # Thinking more than doubles latency; these are one-shot labels.
                "reasoning": {"enabled": False},
                "provider": {"sort": "latency"},
            },
        )
        resp.raise_for_status()
        return (resp.json()["choices"][0]["message"]["content"] or "").strip()

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

        # Fallback heuristic when no OpenRouter key is configured
        if not self.api_key:
            logger.debug("No OPENROUTER_API_KEY; using heuristic executive filter.")
            lower = cleaned.lower()
            triggers = ("?", "permission", "approve", "should i", "blocked", "error:", "completed")
            if force_decision or any(t in lower for t in triggers):
                short = cleaned.splitlines()[-1][:180]
                return AgentTopic(
                    agent_id=agent_id,
                    agent_name=agent_name,
                    summary=f"Needs your input: {short}",
                )
            return None

        try:
            prompt_content = (
                f"Agent Name: {agent_name} (id={agent_id})\n"
                f"Explicit Permission/Question Flag: {force_decision}\n"
                f"Raw Agent Output:\n{cleaned[-4000:]}"
            )
            content = await self._call_llm(
                system_prompt=EXECUTIVE_FILTER_SYSTEM_PROMPT,
                user_content=prompt_content,
                max_tokens=250,
                temperature=0.1,
            )
            # Extract JSON object if wrapped in code fences
            if "```" in content:
                content = content.split("```")[1].removeprefix("json").strip()
            parsed = json.loads(content)

            # A forced event is an agent waiting on the user; dropping it would leave the agent stuck.
            if not parsed.get("should_interrupt") and not force_decision:
                logger.debug(f"[{agent_id}] Filtered out routine output.")
                return None

            return AgentTopic(
                agent_id=agent_id,
                agent_name=agent_name,
                summary=parsed.get("spoken_summary", "Needs your attention."),
            )
        except Exception as exc:
            logger.warning(f"Executive filter LLM call failed ({exc}); falling back to heuristic.")
            if force_decision:
                return AgentTopic(
                    agent_id=agent_id,
                    agent_name=agent_name,
                    summary="Finished a step and is waiting for your decision.",
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
            # When running without an API key, ignore background speech unless explicitly addressed
            return UserIntent.IGNORE_BACKGROUND

        try:
            user_content = (
                f"Recent Jarvis Topic/Question: {recent_jarvis_question or 'None'}\n"
                f"User Utterance: {transcript}"
            )
            label = await self._call_llm(
                system_prompt=ADDRESSED_CLASSIFIER_SYSTEM_PROMPT,
                user_content=user_content,
                max_tokens=20,
                temperature=0.0,
            )
            label = label.upper()
            for intent in UserIntent:
                if intent.value in label:
                    return intent
        except Exception as exc:
            logger.warning(f"Turn classifier failed ({exc}); defaulting to IGNORE_BACKGROUND.")

        return UserIntent.IGNORE_BACKGROUND
