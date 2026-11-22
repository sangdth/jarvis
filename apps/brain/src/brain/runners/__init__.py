"""The execution runners the brain drives: one-shot `pi`, long-lived T3 threads, and the minimal clients."""

from brain.runners.base import EventCallback, Runner, RunRequest, RunResult
from brain.runners.claude import ClaudeRunner
from brain.runners.ephemeral_pi import EphemeralPiRunner
from brain.runners.opencode import OpenCodeRunner
from brain.runners.t3 import T3Runner

__all__ = [
    "ClaudeRunner",
    "EphemeralPiRunner",
    "EventCallback",
    "OpenCodeRunner",
    "RunRequest",
    "RunResult",
    "Runner",
    "T3Runner",
]
