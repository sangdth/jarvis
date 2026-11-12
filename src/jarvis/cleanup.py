"""Tidies a sentence meant for Jarvis before the conversation model reads it: punctuation, fillers, spoken fixes."""

from __future__ import annotations

import re

import httpx
from loguru import logger

from jarvis.config import settings
from jarvis.prompts import CLEANUP_PROMPT

TIMEOUT_SECS = 1.5  # past this the raw sentence goes on: a late cleanup is worse than none
SHORT_WORDS = 3  # "Stop it", "yes go ahead": nothing to tidy, so no call unless a cue is in it
_TAGS = re.compile(r"^\s*<transcript>\s*|\s*</transcript>\s*$")


def parse_cues(raw: str) -> tuple[str, ...]:
    return tuple(c.strip().lower() for c in raw.split(",") if c.strip())


def should_clean(transcript: str, cues: tuple[str, ...]) -> bool:
    """Short sentences skip the call, unless they hold a spoken fix ("42, sorry, 24")."""
    if len(transcript.split()) > SHORT_WORDS:
        return True
    text = transcript.lower()
    return any(re.search(rf"\b{re.escape(cue)}\b", text) for cue in cues)


def keep_cleaned(raw: str, cleaned: str) -> bool:
    """A cleaned sentence that is empty or far longer than what was said is an answer, not a cleanup."""
    return bool(cleaned) and len(cleaned) <= max(len(raw) * 2, len(raw) + 80)


class TranscriptCleaner:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        cues: str | None = None,
        prompt: str | None = None,
    ) -> None:
        self.api_key = settings.openrouter_api_key if api_key is None else api_key
        self.model = model or settings.cleanup_model or settings.executive_model
        self.cues = parse_cues(settings.cleanup_cues if cues is None else cues)
        cue_line = f"\nSelf-correction cues this user says: {', '.join(self.cues)}." if self.cues else ""
        self._system = (prompt or CLEANUP_PROMPT) + cue_line  # prompt: SYSTEM.md's `## Cleanup`
        # One long-lived client keeps the TLS connection warm between sentences.
        self._http = httpx.AsyncClient(
            base_url=(base_url or settings.openrouter_base_url).rstrip("/"),
            timeout=TIMEOUT_SECS,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "HTTP-Referer": "https://github.com/sangdth/jarvis",
                "X-Title": "Jarvis",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def clean(self, transcript: str, recent_jarvis_question: str | None = None) -> str:
        """The tidied sentence, or the sentence as heard on a skip, a failure or a bad answer (never raises)."""
        if not self.api_key or not should_clean(transcript, self.cues):
            return transcript
        context = f"<context>{recent_jarvis_question}</context>\n" if recent_jarvis_question else ""
        try:
            resp = await self._http.post(
                "/chat/completions",
                json={
                    "model": self.model,
                    "reasoning": {"enabled": False},
                    "provider": {"sort": "latency"},
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": self._system},
                        {"role": "user", "content": f"{context}<transcript>{transcript}</transcript>"},
                    ],
                },
            )
            resp.raise_for_status()
            cleaned = _TAGS.sub("", resp.json()["choices"][0]["message"]["content"] or "").strip()
        except Exception as exc:
            logger.warning(f"[Cleanup] failed ({type(exc).__name__}: {exc}); using the sentence as heard.")
            return transcript
        if not keep_cleaned(transcript, cleaned):
            logger.warning(f"[Cleanup] dropped a bad answer {cleaned[:120]!r}; using the sentence as heard.")
            return transcript
        return cleaned
