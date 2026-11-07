"""The "still working" lines Jarvis says while an agent works: written fresh for each task from the base lines."""

from __future__ import annotations

import asyncio
import random
import re

import httpx
from loguru import logger

from jarvis.config import settings
from jarvis.prompts import LANGUAGES, WAITING_PROMPT

BATCH = 6  # lines per generation call
REFILL_AT = 4  # after this many of a batch are spoken, the next batch is made in the background
FIRST_WAIT = 5.0  # seconds a due reminder waits for a batch still being written, before it uses a base line
MAX_CHARS = 90  # a longer line is not a quick reminder
_SENTENCE_BREAK = re.compile(r"[.!?…]\s+\S")
_LIST_MARK = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")


def keep_line(line: str, spoken: list[str]) -> bool:
    """A generated line Jarvis may say: short, at most two sentences (like the base lines), not said already."""
    return 0 < len(line) <= MAX_CHARS and len(_SENTENCE_BREAK.findall(line)) <= 1 and line not in spoken


class WaitingLines:
    """Lines for one task. Generated ones come first; the base lines from languages.json only when generation fails."""

    def __init__(self, task: str, personality: str = "", http: httpx.AsyncClient | None = None) -> None:
        self._task = task
        self._personality = personality
        self._http = http  # None: no API key, so the base lines are all there is
        self._base = LANGUAGES[settings.language]["waiting"]
        self._pool: list[str] = []  # generated, not said yet
        self._spoken: list[str] = []
        self._refill: asyncio.Task | None = None

    def start(self) -> None:
        """Start the first batch now, so it is ready by the first reminder."""
        self._start_refill()

    def stop(self) -> None:
        if self._refill:
            self._refill.cancel()

    async def next(self) -> str:
        if not self._pool and self._refill and not self._refill.done():
            await asyncio.wait({self._refill}, timeout=FIRST_WAIT)  # waits without cancelling the call
        if self._pool:
            line = self._pool.pop(0)
            if len(self._pool) <= BATCH - REFILL_AT:
                self._start_refill()
        else:
            self._start_refill()  # that batch failed or is late: try again for the next reminder
            last = self._spoken[-1] if self._spoken else None
            line = random.choice([t for t in self._base if t != last])  # never the same twice in a row
        self._spoken.append(line)
        return line

    def _start_refill(self) -> None:
        if self._http and not (self._refill and not self._refill.done()):  # one call at a time
            self._refill = asyncio.create_task(self._generate())

    async def _generate(self) -> None:
        assert self._http is not None
        examples = "\n".join(f"- {t}" for t in self._base)
        said = "\n".join(f"- {t}" for t in self._spoken) or "(nothing yet)"
        try:
            resp = await self._http.post(
                "/chat/completions",
                json={
                    "model": settings.executive_model,
                    "reasoning": {"enabled": True},
                    "messages": [
                        {
                            "role": "system",
                            "content": WAITING_PROMPT.format(count=BATCH, personality=self._personality or "calm"),
                        },
                        {"role": "user", "content": f"Task: {self._task}\n\nExamples:\n{examples}\n\nAlready said:\n{said}"},
                    ],
                },
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"] or ""
        except Exception as exc:
            logger.warning(f"Writing waiting lines failed ({exc!r}); using the base lines.")
            return
        lines = [_LIST_MARK.sub("", raw).strip() for raw in text.splitlines()]
        kept: list[str] = []
        for line in lines:
            if keep_line(line, self._spoken + self._pool + kept):
                kept.append(line)
        if not kept:
            logger.warning(f"Writing waiting lines gave nothing usable ({text[:200]!r}); using the base lines.")
        self._pool.extend(kept[:BATCH])
