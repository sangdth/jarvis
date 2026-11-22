"""Spoken answers mapped onto an agent's question, and questions made speakable.

Runner-agnostic: T3 Code's `user_input_request`, Claude Code's `AskUserQuestion` and OpenCode's form fields
all end up in the same small shape, so one answer path works for every client. Nothing here calls a model;
the user's words are mapped deterministically, because approvals and questions never go through an LLM.
"""

from __future__ import annotations

import re
from typing import Any

# Refusals are checked first, so "not sure" or "don't allow" never count as approval.
_REJECT_WORDS = re.compile(r"\b(no|not|don'?t|do not|never|reject|deny|denied|cancel|stop|wait)\b", re.IGNORECASE)
_APPROVE_WORDS = re.compile(r"\b(yes|yep|yeah|approve|approved|allow|go ahead|sure|do it|ok|okay|proceed)\b", re.IGNORECASE)
_OPTION_NEGATION = re.compile(
    r"\b(?:not|no|don'?t|do\s+not|never|skip|avoid)\s+(?:(?:use|pick|choose|do|take|select|with|the|option)\s+)*$"
)


def is_approval(instruction: str) -> bool:
    """True only when the user clearly approves and says nothing that sounds like a refusal."""
    if _REJECT_WORDS.search(instruction):
        return False
    return bool(_APPROVE_WORDS.search(instruction))


def question_fields(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Claude Code and T3 questions in the OpenCode form-field shape, so form_answer works for all three."""
    return [
        {
            "key": q.get("id") or q["question"],
            "description": q["question"],
            "type": "multiselect" if q.get("multiSelect") else "string",
            "options": [{"value": o.get("value") or o["label"], "label": o["label"]} for o in q.get("options", [])],
        }
        for q in questions
    ]


def _option_matched(label: str, said: str) -> bool:
    """True when the user named this option (with or without `(recommended)`) and did not negate it."""
    bare = re.sub(r"\s*\(recommended\)$", "", label, flags=re.IGNORECASE)
    candidates = [label] + ([bare] if bare and bare != label else [])
    for cand in candidates:
        # (?<!\w)/(?!\w), not \b: a label like "C++" or ".env" has no word edge at its punctuation.
        for match in re.finditer(rf"(?<!\w){re.escape(cand.lower())}(?!\w)", said):
            if not _OPTION_NEGATION.search(said[: match.start()]):
                return True
    return False


def form_answer(fields: list[dict[str, Any]], instruction: str) -> dict[str, Any]:
    """Map a spoken answer onto form fields: a named option, yes/no, or the user's own words."""
    said = instruction.lower()
    answer: dict[str, Any] = {}
    for field in fields:
        if field.get("hidden"):
            continue
        options = field.get("options") or []
        named = [o["value"] for o in options if _option_matched(o["label"], said)]
        if field.get("type") == "boolean":
            answer[field["key"]] = is_approval(instruction)
        elif field.get("type") == "multiselect":
            answer[field["key"]] = named or [instruction]
        else:
            answer[field["key"]] = named[0] if named else instruction
    return answer


def describe_fields(fields: list[dict[str, Any]]) -> str:
    """Speakable text for a list of questions: each question, then its options."""
    asks = []
    for field in fields:
        question = field.get("description") or field.get("title") or ""
        labels = [o["label"] for o in field.get("options") or []]
        asks.append(f"{question} Options: {', '.join(labels)}." if labels else question)
    return " ".join(asks)
