"""Tidies a sentence meant for Jarvis before the conversation model reads it: punctuation, fillers, spoken fixes."""

from __future__ import annotations

import asyncio
import re

import httpx
from loguru import logger

from jarvis.config import settings
from jarvis.prompts import CLEANUP_PROMPT

TIMEOUT_SECS = 1.5  # past this the raw sentence goes on: a late cleanup is worse than none
SHORT_WORDS = 18  # short, clean commands skip the call unless a filler, stutter or spoken fix is in them
MAX_CONTEXT_CHARS = 300
FILLERS = ("um", "uh", "uhm", "erm", "ờ", "ừm")
_STUTTER = re.compile(r"\b(\w+)\s+\1\b", re.IGNORECASE)
_TAGS = re.compile(r"^\s*<transcript>\s*|\s*</transcript>\s*$", re.IGNORECASE)
_LEAKED_TAG = re.compile(r"</?(?:transcript|context)\b", re.IGNORECASE)


def _words(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


def parse_cues(raw: str) -> tuple[str, ...]:
    return tuple(w for c in raw.split(",") if (w := _words(c)))


def should_clean(transcript: str, cues: tuple[str, ...]) -> bool:
    """Short sentences skip the call, unless they hold a filler, a repeated word, or a spoken fix ("No, no, 24")."""
    if len(transcript.split()) > SHORT_WORDS:
        return True
    words = _words(transcript)
    rest = words.partition(" ")[2]  # a one-word cue opening the sentence ("Actually, ...") stays, so no call for it
    return (
        _STUTTER.search(words) is not None
        or any(_has(f, words) for f in FILLERS)
        or any(_has(c, words if " " in c else rest) for c in cues)
    )


def _has(phrase: str, words: str) -> bool:
    return re.search(rf"\b{re.escape(phrase)}\b", words) is not None


def keep_cleaned(raw: str, cleaned: str) -> bool:
    """Not a cleanup: empty, far longer than said, leaked prompt tags, or mostly words the user never said (an answer)."""
    if not cleaned or len(cleaned) > max(len(raw) * 2, len(raw) + 80):
        return False
    if _LEAKED_TAG.search(cleaned) and not _LEAKED_TAG.search(raw):
        return False
    said, new = set(_words(raw).split()), _words(cleaned).split()
    return sum(w in said for w in new) * 2 >= len(new)


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
        safe_question = re.sub(r"[<>]", "", recent_jarvis_question)[:MAX_CONTEXT_CHARS].strip() if recent_jarvis_question else ""
        context = f"<context>{safe_question}</context>\n" if safe_question else ""
        try:
            # httpx's timeout is per phase (connect, each read); this caps the whole call.
            resp = await asyncio.wait_for(
                self._http.post(
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
                ),
                TIMEOUT_SECS,
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
