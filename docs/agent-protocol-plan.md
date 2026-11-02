# Plan: talk to coding agents the way T3 Code does

Status: steps 1-4 are done (see `docs/architecture.md`); Pi support was removed instead of step 5.

Research source: `pingdotgg/t3code` at `701e13ce` (2026-11-02). Paths below are in that repo.

## What T3 Code does

| Agent    | T3 transport                                   | Jarvis today                          |
| -------- | ---------------------------------------------- | ------------------------------------- |
| Claude   | Agent SDK control protocol, one long process   | `claude -p` stdout, `acceptEdits`     |
| OpenCode | `/api` v2 SDK, creates its own sessions        | SSE only, learns session from events  |
| Pi       | `pi --mode rpc` (own NDJSON protocol)          | `pi --acp` (flag likely doesn't exist) |

- Claude: `apps/server/src/orchestration-v2/Adapters/ClaudeAdapterV2.ts`. Permissions, AskUserQuestion and
  ExitPlanMode all arrive through the `canUseTool` callback; the answer is `{behavior: "allow"|"deny"}`.
- OpenCode: `packages/provider-opencode/src/server/v2/adapter.ts`. Answers only go to an explicit
  permission/form reply tied to a request id; new text while a turn runs is a `steer` prompt.
- Pi: `packages/provider-pi/src/server/rpc.ts`. The ACP route is the separate npm bridge `pi-acp`.

## Steps

1. **Split "answer" from "new task" (all agents).** Today a pending OpenCode form or ACP permission takes
   the next thing you say as its answer, even "also add dark mode". Replace the one tool with
   `answer_agent(agent_id, answer)` and `send_agent_task(agent_id, task)`. The LLM picks; the adapter
   answers only through `answer_agent`, and `send_agent_task` always sends a prompt.
2. **Claude via the Python Agent SDK.** `claude -p` can't answer permission prompts, so we run it with
   `acceptEdits` and never hear about anything else. Use `claude-agent-sdk` (PyPI 0.2.176) with
   `ClaudeSDKClient`: `can_use_tool` turns a permission or AskUserQuestion into a spoken question and waits
   for `answer_agent`; `interrupt()` for stop; keep the session id to resume. Adds one dependency.
3. **OpenCode session and state.** Jarvis can't send a task until OpenCode emits an event for a session.
   Create a session at start with the agent's `cwd`, and drop events from other sessions. Clear the
   pending permission/form on `permission.replied`, `form.replied` and `form.cancelled`, so an answer
   given in the OpenCode UI doesn't leave Jarvis answering a dead form. Handle
   `session.execution.interrupted`. Endpoint paths get checked against the live server first.
4. **OpenCode stream health.** A stream that goes silent is never noticed. Treat 45 s with no bytes as
   dead and reconnect; after reconnect, re-list pending permissions and forms.
5. **Pi through `pi-acp`.** Change the `agents.json` command to `npx -y pi-acp` and keep the ACP adapter.
   Advertise `fs` and `terminal` as false in `initialize`, as T3 does. Untested: `pi` isn't installed.

## Skipped on purpose

- A native Pi RPC adapter: `pi-acp` covers it with zero new code.
- Spawning `opencode serve` from Jarvis: attaching to a server you started works today.
- "Always allow" rules, subagent tracking, token usage, compaction: not needed for voice.
