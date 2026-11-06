from __future__ import annotations

import httpx
from loguru import logger

from jarvis.config import settings
from jarvis.state import UserIntent, classify_fast_command

_CONTEXT = (
    "Jarvis is a voice assistant that hands work to agents (coding, research, email, calendar, notes) "
    "and chats with its user. "
    "The text is one sentence transcribed from an always-on microphone in a home office. "
    "The user is usually alone and talks to Jarvis; other people are sometimes in the room."
)
# One typed question for an OpenRouter decision model; the labels are the UserIntent values.
# Jarvis is the default listener, so small talk with no one else named counts as addressed (97% on the 2026-11-02 set).
INTENT_QUESTION = {
    "type": "choice",
    "instructions": _CONTEXT
    + " jarvis_last_said, when given, is what Jarvis said just before: a reply to it talks to Jarvis even when the"
    " words make little sense, because speech recognition garbles names and short answers."
    " What does the speaker want?",
    "criteria": {
        UserIntent.MUTE.value: (
            "tells Jarvis itself to be quiet: shut up, be quiet, hush, stop talking. A plain \"stop\", \"stop it\","
            " \"stop that\" or \"cancel that\" is not this: it stops the work an agent is doing"
        ),
        UserIntent.UNMUTE.value: "tells Jarvis it can talk again",
        UserIntent.ADDRESSED_TO_JARVIS.value: (
            "talks to Jarvis: small talk or a greeting, a question, a coding task or instruction for an agent, "
            "an answer, or tells it to stop or cancel the work an agent is doing. Pick this when no other person "
            "is named or clearly meant"
        ),
        UserIntent.IGNORE_BACKGROUND.value: (
            "talks to another person: names them, continues a story or plan with them (meetings, sports, family, "
            "errands), or the words only fit someone in the room; or thinks out loud, or says only filler words"
        ),
    },
}


class ExecutiveFilter:
    """Decides whether a heard sentence is meant for Jarvis, with an OpenRouter decision model."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.api_key = settings.openrouter_api_key if api_key is None else api_key
        self.model = model or settings.filter_model
        self.base_url = (base_url or settings.openrouter_base_url).rstrip("/")
        # shortcut: OpenRouter serves decision models on its alpha API next to /v1; revisit if it moves.
        self.decisions_url = self.base_url.removesuffix("/v1") + "/alpha/decisions"
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
            logger.warning("OPENROUTER_API_KEY is not set; only the fixed mute and unmute phrases are heard.")

    async def aclose(self) -> None:
        await self._http.aclose()

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

        state = {"document": transcript}
        if recent_jarvis_question:
            state["jarvis_last_said"] = recent_jarvis_question
        try:
            resp = await self._http.post(
                self.decisions_url,
                json={"model": self.model, "state": state, "questions": {"intent": INTENT_QUESTION}},
            )
            resp.raise_for_status()
            return UserIntent(resp.json()["answers"]["intent"]["choice"])
        except Exception as exc:
            logger.warning(f"Turn classifier failed ({exc}); defaulting to IGNORE_BACKGROUND.")

        return UserIntent.IGNORE_BACKGROUND
