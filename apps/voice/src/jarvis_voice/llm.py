"""The conversation model, with one side tap: what it was given, what it thought, and which tool it called.

OpenRouter streams a reasoning model's thinking as `delta.reasoning`, which Pipecat's OpenAI-compatible service
drops. Wrapping the chunk stream keeps that visible to `jarvis top` without copying Pipecat's processing loop or
changing a single frame the pipeline sees.
"""

from __future__ import annotations

import inspect
from typing import Any, AsyncIterator

from pipecat.services.openrouter.llm import OpenRouterLLMService

from jarvis_voice import telemetry


def summarize_context(context: Any) -> dict[str, Any]:
    """A few facts about the prepared request: how big it was, what the user said, which tools it can call."""
    messages = [m for m in context.get_messages() if isinstance(m, dict)]
    users = [m for m in messages if m.get("role") == "user"]
    tools = [t.get("function", {}).get("name", "") for t in (getattr(context, "tools", None) or [])]
    return {
        "messages": len(messages),
        "user_tail": telemetry.preview(users[-1].get("content") if users else "", 400),
        "tools": ", ".join(name for name in tools if name),
    }


async def _tee_reasoning(stream: AsyncIterator[Any]) -> AsyncIterator[Any]:
    """Passes every chunk through untouched, publishing the reasoning text as it arrives."""
    async for chunk in stream:
        choices = getattr(chunk, "choices", None) or []
        delta = choices[0].delta if choices else None
        reasoning = getattr(delta, "reasoning", None) if delta is not None else None
        if reasoning:
            telemetry.emit("voice.thinking", text=str(reasoning))
        yield chunk


class JarvisLLM(OpenRouterLLMService):
    """OpenRouter's service, with the prepared request and the model's reasoning on the telemetry bus."""

    async def get_chat_completions(self, context: Any) -> AsyncIterator[Any]:  # type: ignore[override]
        telemetry.emit("voice.request", **summarize_context(context))
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
