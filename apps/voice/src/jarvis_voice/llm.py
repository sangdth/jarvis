"""The conversation model, with one side tap: what it was given, what it thought, and which tool it called.

OpenRouter streams a reasoning model's thinking as `delta.reasoning`, which Pipecat's OpenAI-compatible service
drops. Wrapping the chunk stream keeps that visible to `jarvis top` without copying Pipecat's processing loop or
changing a single frame the pipeline sees.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, AsyncIterator

from pipecat.services.openrouter.llm import OpenRouterLLMService

from jarvis_voice import telemetry


def _tool_names(raw_tools: Any) -> list[str]:
    items = getattr(raw_tools, "standard_tools", raw_tools)
    if not isinstance(items, (list, tuple)):
        return []
    names: list[str] = []
    for item in items:
        name = item.get("function", {}).get("name", "") if isinstance(item, dict) else getattr(item, "name", "")
        if name:
            names.append(str(name))
    return names


def summarize_context(context: Any) -> dict[str, Any]:
    """A few facts about the prepared request: how big it was, what the user said, which tools it can call."""
    messages = [m for m in context.get_messages() if isinstance(m, dict)]
    users = [m for m in messages if m.get("role") == "user"]
    tools = _tool_names(getattr(context, "tools", None))
    return {
        "messages": len(messages),
        "user_tail": telemetry.preview(users[-1].get("content") if users else "", 400),
        "tools": ", ".join(tools),
    }


async def _tee_reasoning(stream: AsyncIterator[Any]) -> AsyncIterator[Any]:
    """Passes every chunk through untouched, publishing coalesced reasoning text."""
    buf: list[str] = []
    try:
        async for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            delta = choices[0].delta if choices else None
            reasoning = getattr(delta, "reasoning", None) if delta is not None else None
            if reasoning:
                buf.append(str(reasoning))
            elif buf:
                telemetry.emit("voice.thinking", text="".join(buf))
                buf.clear()
            yield chunk
    finally:
        if buf:
            telemetry.emit("voice.thinking", text="".join(buf))
        close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
        if close is not None:
            res = close()
            if inspect.isawaitable(res):
                await res


class JarvisLLM(OpenRouterLLMService):
    """OpenRouter's service, with the prepared request and the model's reasoning on the telemetry bus."""

    def __init__(
        self,
        *args: Any,
        working_context: Any = None,
        enrollment_turn: Callable[[], tuple[str | None, bool]] | None = None,
        speaker_registry: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._working_context = working_context
        self._enrollment_turn = enrollment_turn
        self._speaker_registry = speaker_registry

    def build_chat_completion_params(self, params_from_context: Any) -> dict[str, Any]:  # type: ignore[override]
        notes: list[str] = []
        hide_tools = False
        if self._enrollment_turn is not None:
            note, hide_tools = self._enrollment_turn()
            if note:
                notes.append(note)
        if self._speaker_registry is not None and (
            roster := self._speaker_registry.format_for_prompt(can_rename=not hide_tools)
        ):
            notes.append(roster)
        if not hide_tools and self._working_context is not None and (held := self._working_context.format_for_prompt()):
            notes.append(held)
        if notes:
            msgs = list(params_from_context.get("messages") or [])
            idx = next(
                (i for i in range(len(msgs) - 1, -1, -1) if isinstance(msgs[i], dict) and msgs[i].get("role") == "user"),
                len(msgs) - 1 if msgs and isinstance(msgs[-1], dict) and msgs[-1].get("role") != "tool" else None,
            )
            if idx is not None:
                for offset, note in enumerate(notes):
                    msgs.insert(idx + offset, {"role": "system", "content": note})
                params_from_context = {**params_from_context, "messages": msgs}
        params = super().build_chat_completion_params(params_from_context)
        if hide_tools:
            params.pop("tools", None)
            params.pop("tool_choice", None)
        return params

    async def get_chat_completions(self, context: Any) -> AsyncIterator[Any]:  # type: ignore[override]
        try:
            telemetry.emit("voice.request", **summarize_context(context))
        except Exception:  # noqa: BLE001 - telemetry must never block an LLM turn
            pass
        stream = await super().get_chat_completions(context)
        return _tee_reasoning(stream)


def traced_tool(name: str, handler: Any) -> Any:
    """One agent tool, with the call and its result on the bus. The handler's behaviour is unchanged."""

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        telemetry.emit("voice.tool", tool=name, phase="call")
        result = handler(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        telemetry.emit("voice.tool", tool=name, phase="result", result=telemetry.preview(result, 300))
        return result

    return wrapped


__all__ = ["JarvisLLM", "summarize_context", "traced_tool"]
