# Session management for the coding clients (plan, 2026-11-08)

Status: proposed, not started. Reviewed against the code and the installed `claude_agent_sdk` on 2026-11-08.

## Why

T3 Code is the first-class client, but most users run Claude Code or OpenCode directly. Today Jarvis
gives them one fixed conversation each: Claude Code is one SDK process in the launch folder
(`claude_adapter.py:55-67`), OpenCode latches onto the latest session it sees or makes one in `cwd`
(`opencode_adapter.py:216-232`). Neither can list, pick or start a session, and a Claude crash restarts with
an empty context and says nothing. Pi can pick a window, but only through its own code path in `main.py`.

## Rules this plan keeps

1. **T3 Code keeps its own implementation.** Projects, threads, `open_project`, `attach`, `shell_snapshot`
   stay in `T3Adapter`. The rest of the code reaches T3 through one explicit `orchestrator.t3`, never through
   `hasattr(agent, "open_project")` (`agent_tools.py:98`, `prompts.py:155`) or `isinstance(a, T3Adapter)`
   (`main.py:35`).
2. **Claude Code, OpenCode and Pi share one session contract**, with different transports underneath.
3. **Hermes stays on the four-action base** (`send_task`, `answer`, `interrupt`, `busy`). It is not a coding
   client and has no sessions Jarvis should manage.
4. **Jarvis stays thin.** Sessions live in each client's own store. Jarvis lists, picks, creates and relays.

## How a voice tool runs (for the people touching `agent_tools.py`)

A Pipecat tool is a `FunctionSchema` (name, description, JSON properties) plus an `async` handler. `main.py:206-212`
puts the schemas in `LLMContext(tools=...)`, so OpenRouter sends them to `EXECUTIVE_MODEL` on every turn, and
registers each handler with `llm.register_function`. When the model emits a tool call, Pipecat calls the
handler with `params.arguments` and the handler reports back with `await params.result_callback({...})`;
Pipecat then runs the model once more so Jarvis speaks the outcome. Tools are plain functions; the only cost
of adding one is in routing accuracy, which is why `scripts/bench_routing.py` gates every schema or prompt change.

## The contract

One new module, `src/jarvis/sessions.py`:

```python
@dataclass(frozen=True)
class AgentSession:
    id: str          # Claude session uuid, OpenCode `ses_…`, Pi session id
    title: str       # custom title, summary or first prompt
    cwd: str         # absolute folder
    updated_at: float  # seconds; lists are newest first
    branch: str = ""   # when the client knows it (Claude Code does)


class CodingSessionAdapter(BaseAgentAdapter):
    session: AgentSession | None   # the one tasks go to now
    connected: bool                # picked with `jarvis connect`: never leave it this run

    async def list_sessions(self, cwd: str | None = None, limit: int = 15) -> list[AgentSession]: ...
    async def pick_session(self, session: AgentSession, connected: bool = False) -> bool: ...
    async def new_session(self, cwd: str | None = None) -> bool: ...
```

Not in the contract, on purpose:

- No `find_sessions` method. Picking by words is a lexical match over `title`, `branch` and the folder name of
  a `list_sessions` result (one helper in `sessions.py`). The lists are small (OpenCode returns 50, Claude is
  paginated) and every session has a title. See "RAG" below for when that stops being enough.
- No `agent_id`/`agent_name` on `AgentSession`; the adapter's `config` has them. No `project` field; it is
  `cwd` relative to `PROJECTS`, computed where it is shown.
- No `busy` on `AgentSession`. Claude's listing has no such field; the adapter's `busy` already answers it for
  the current session.
- `_running`, `_stopped`, `_spoken`, `_last_text` stay in each adapter. They look alike but mean different things
  per transport (OpenCode's `_spoken` is per request id over a resync, Claude's `_stopped` swallows one result).
  Merge them only after all three implement the contract and the duplication is visible, not before.

## What each client does

| Operation       | Claude Code (`claude_adapter.py`)                      | OpenCode (`opencode_adapter.py`)                      | Pi (`pi_adapter.py`)                        |
| :-------------- | :----------------------------------------------------- | :---------------------------------------------------- | :------------------------------------------ |
| `list_sessions` | `claude_agent_sdk.list_sessions(directory=cwd, limit)` | `GET /api/session` (+ `x-opencode-directory` header)  | `list_windows()`: open windows only         |
| `pick_session`  | restart the SDK client with `resume=id`, `cwd=s.cwd`   | set `_session_id`, `_directory`, `_ours`; `_resync()` | reconnect to that window's socket           |
| `new_session`   | restart the SDK client with `resume=None`, new `cwd`   | `POST /api/session` with `location.directory`         | not supported: the user opens a window      |

Facts checked on 2026-11-08: `ClaudeAgentOptions` has `resume` and `fork_session`; the SDK exports
`list_sessions`, `get_session_info`, `get_session_messages`, `rename_session`. `SDKSessionInfo` carries
`session_id`, `summary`, `custom_title`, `first_prompt`, `cwd`, `git_branch`, `last_modified` (ms). OpenCode's
`GET /api/session` returns `id`, `title`, `location.directory`, `time.updated` (ms), `time.busy`/`time.idle`.
The Pi extension (`defaults/pi/jarvis.ts:5-6`) accepts only `prompt` and `abort`; a `new` command does not exist,
and adding one is a Pi-side change, so Pi's `new_session` returns `False` with a log line.

Claude Code detail: `_run_loop` is one `while True` around `ClaudeSDKClient`. `pick_session`/`new_session` cancel
that task and start it again with new options; a turn in flight is lost, so both refuse while `busy` (return
`False`, the handler reports `busy`). A crash restart (`claude_adapter.py:75-80`) resumes the same session id
instead of starting a fresh one, and surfaces one line: "Claude Code restarted."

## Steps

### Phase 1: the contract, the keyboard picker, and T3 made explicit

1. `sessions.py`: `AgentSession`, `CodingSessionAdapter`, `match_sessions(sessions, words)` (lexical, recency
   as the tie-break).
2. `ClaudeCodeAdapter`, `OpenCodeSSEAdapter`, `PiAdapter` extend `CodingSessionAdapter` and implement the table.
3. `orchestrator.py`: `t3` property (the one `T3Adapter`, or `None`). `agent_tools.py:98`, `prompts.py:155`,
   `main.py:35` use it. `default_agent_id` returns the connected agent's id while one is connected, so
   `jarvis connect claude` sends tasks to Claude Code even when `DEFAULT_CODING_AGENT` says otherwise.
4. `cli.py`/`main.py`: `jarvis connect [t3-code|claude|opencode|pi]`. No argument means T3 (today's behavior).
   T3 keeps `pick_thread`. The other three share one `pick_session(orchestrator, agent_id)`: `list_sessions()`,
   `choose(...)`, `pick_session(chosen, connected=True)`. `pick_pi_window` folds into it; the `PiAdapter` is still
   built here, since Pi is not in `agents.json`. The return dict feeds `connected_thread` in `prompts.py` unchanged.
5. Tests: `tests/test_sessions.py` for `match_sessions`; in the three adapter tests, mock `list_sessions`/HTTP/
   sockets and check `pick_session` passes `resume=id`, OpenCode switches `_session_id` and resyncs, Pi reconnects.
   `uv run ruff check src tests scripts`, `uv run mypy`, the unittest command from `AGENTS.md`.

No prompt or tool schema changes in this phase, so no routing benchmark.

### Phase 2: sessions by voice

Keep three tools. Add one property to `send_agent_task` instead of a fourth tool:

- `session`: `"new"` to start a clean conversation, or a few words naming an earlier session to continue
  ("the one about speaker verification"). Omit to stay where we are.

The handler: for `orchestrator.t3` nothing changes (`open_project` as today). For a `CodingSessionAdapter`,
`"new"` calls `new_session(cwd)`, words call `match_sessions(await list_sessions(), words)` and `pick_session`
on the best hit; `project` resolves to a folder under `PROJECTS` the same way `T3Adapter.open_project` does
(listed folders only, case-blind), and becomes that `cwd`. While `connected`, `session` and `project` are ignored
with `nothing_to_send`, the same as T3 today.

The system prompt gets, per coding client, its five most recent sessions (title and project), the way
`project_list` lists projects. Questions about them ("what was I doing in OpenCode yesterday?") are answered
from that list with no tool, mirroring the projects rule in `AGENTS.md`.

Gate: `uv run python scripts/bench_routing.py` before and after; add cases for "start a new session", "continue
the session about X on OpenCode", and a plain task that must not set `session`. Stays at or above 95%.

### Phase 3: RAG over transcripts (only on a trigger)

Build this only when users report that titles are not enough to find a session, say, a session whose title is
its first prompt and the real work came later. Then: `get_session_messages` (Claude) and OpenCode's message
endpoint feed a `jarvis_sessions` Qdrant collection next to the memory one, embedded with the same Ollama
`bge-m3` as `memory.py` (about 20 ms per batch, local). The lexical match stays as the fallback when Ollama is
off. Not before: it is a second index to keep fresh, and the lists today have titles.

## Risks and counter-arguments

- **Overlap with T3.** T3 Code can run Claude Code and OpenCode as providers. If T3 adoption grows, Phase 2 and
  3 are work on a path fewer people use. Phase 1 is still worth it: it removes the duck-typing and gives direct
  users `jarvis connect`, the same feature T3 users already have.
- **Restarting Claude's process loses the turn.** Hence the `busy` refusal. A user saying "new session" mid-turn
  hears "it's busy", then "stop" and again. Acceptable; silently killing the turn is not.
- **Prompt growth.** Five sessions per client is up to 15 extra lines on every turn. Measure with
  `bench_routing.py`; if accuracy drops, list only the default coding agent's sessions.
- **OpenCode on the shared service serves every folder.** `list_sessions(cwd=None)` lists all of them; keep the
  `x-opencode-directory` header when a folder is given, so "my sessions in this project" stays scoped.
