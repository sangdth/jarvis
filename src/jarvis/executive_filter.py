from __future__ import annotations

import asyncio

import httpx
from loguru import logger

from jarvis.config import settings
from jarvis.feedback import FeedbackLog
from jarvis.state import UserIntent, classify_fast_command, is_not_for_jarvis

_CONTEXT = (
    "Jarvis is a voice assistant that hands work to agents (coding, research, email, calendar, notes) "
    "and chats with its user. "
    "The text is one sentence transcribed from an always-on microphone in a home office. "
    "The user is usually alone and talks to Jarvis; other people are sometimes in the room. "
    "The sentence may be in English, Vietnamese or a mix of both."
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
        UserIntent.INTRODUCTION.value: (
            "introduces a new person or speaker to Jarvis so Jarvis can meet them or remember their voice "
            "(\"this is David\", \"meet my friend\", \"let me introduce someone\")"
        ),
        UserIntent.IGNORE_BACKGROUND.value: (
            "talks to another person: names them, continues a story or plan with them (meetings, sports, family, "
            "errands), or the words only fit someone in the room; or thinks out loud, or says only filler words"
        ),
    },
}


class ExecutiveFilter:
    """Decides whether a heard sentence is meant for Jarvis: Laya's jarvis-intent first, the OpenRouter decision
    model (FILTER_MODEL) as the fallback. Both are asked at once; Jev's answer is logged even when Laya's is used."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        laya_api_key: str | None = None,
        feedback: FeedbackLog | None = None,
    ) -> None:
        self.api_key = settings.openrouter_api_key if api_key is None else api_key
        self.model = model or settings.filter_model
        self.base_url = (base_url or settings.openrouter_base_url).rstrip("/")
        # shortcut: OpenRouter serves decision models on its alpha API next to /v1; revisit if it moves.
        self.decisions_url = self.base_url.removesuffix("/v1") + "/alpha/decisions"
        self.laya_api_key = settings.laya_api_key if laya_api_key is None else laya_api_key
        # LAYA_BASE_URL works with or without /v1, like OPENROUTER_BASE_URL.
        self.laya_url = settings.laya_base_url.rstrip("/").removesuffix("/v1") + "/v1/systemone"
        # Off unless given: the benchmark and the tests must not write to the user's feedback file.
        self.feedback = feedback
        # One long-lived client each keeps the TLS connection warm between calls.
        self._http = httpx.AsyncClient(
            timeout=12.0,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "HTTP-Referer": "https://github.com/sangdth/jarvis",
                "X-Title": "Jarvis",
            },
        )
        self._laya_http = httpx.AsyncClient(
            timeout=settings.laya_timeout_secs, headers={"Authorization": f"Bearer {self.laya_api_key}"}
        )
        self._pending: set[asyncio.Task] = set()  # Jev calls still running after Jarvis acted on Laya
        if not self.api_key and not self.laya_api_key:
            logger.warning("OPENROUTER_API_KEY is not set; only the fixed mute and unmute phrases are heard.")

    @property
    def name(self) -> str:
        return f"{settings.laya_model} (Laya, {self.model} as fallback)" if self.laya_api_key else self.model

    async def aclose(self) -> None:
        for task in list(self._pending):
            task.cancel()
        await self._http.aclose()
        await self._laya_http.aclose()

    async def _ask(self, http: httpx.AsyncClient, url: str, body: dict) -> dict:
        """One model's answer as {choice, probabilities}, or {error} on any failure (never raises)."""
        try:
            resp = await http.post(url, json=body)
            resp.raise_for_status()
            answer = resp.json()["answers"]["intent"]
            return {"choice": UserIntent(answer["choice"]).value, "probabilities": answer.get("probabilities")}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"[:200]}

    def _log(self, transcript: str, state: dict | None, intent: UserIntent, source: str, **answers: dict | None) -> str:
        return self.feedback.decision(transcript, state, intent, source, **answers) if self.feedback else ""

    async def classify_user_utterance(
        self,
        transcript: str,
        recent_jarvis_question: str | None = None,
    ) -> UserIntent:
        """Determine if an un-waked utterance is meant for Jarvis or should be ignored."""
        fast = classify_fast_command(transcript)
        if fast is not None:
            self._log(transcript, None, fast, "fast")
            return fast
        if is_not_for_jarvis(transcript):
            self._log(transcript, None, UserIntent.IGNORE_BACKGROUND, "not-you")
            return UserIntent.IGNORE_BACKGROUND

        state = {"document": transcript}
        if recent_jarvis_question:
            state["jarvis_last_said"] = recent_jarvis_question
        body = {"state": state, "questions": {"intent": INTENT_QUESTION}}
        jev = (
            asyncio.create_task(self._ask(self._http, self.decisions_url, {"model": self.model, **body}))
            if self.api_key
            else None
        )
        laya = (
            await self._ask(self._laya_http, self.laya_url, {"model": settings.laya_model, **body})
            if self.laya_api_key
            else None
        )

        if laya and "choice" in laya:
            intent = UserIntent(laya["choice"])
            decision_id = self._log(transcript, state, intent, "laya", laya=laya)
            if jev is not None:
                self._log_when_done(decision_id, jev)
            return intent

        if laya:
            logger.warning(f"Laya failed ({laya['error']}); using {self.model}.")
        answer = await jev if jev is not None else None
        if answer and "choice" in answer:
            intent = UserIntent(answer["choice"])
            self._log(transcript, state, intent, "jev", laya=laya, jev=answer)
            return intent
        if answer:
            logger.warning(f"Turn classifier failed ({answer['error']}); defaulting to IGNORE_BACKGROUND.")
        # No model answered (or no key): ignore background speech unless explicitly addressed.
        self._log(transcript, state, UserIntent.IGNORE_BACKGROUND, "none", laya=laya, jev=answer)
        return UserIntent.IGNORE_BACKGROUND

    def _log_when_done(self, decision_id: str, jev: asyncio.Task) -> None:
        self._pending.add(jev)

        def done(task: asyncio.Task) -> None:
            self._pending.discard(task)
            if self.feedback and decision_id and not task.cancelled():
                self.feedback.late(decision_id, task.result())

        jev.add_done_callback(done)
