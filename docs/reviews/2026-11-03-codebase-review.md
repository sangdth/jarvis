# Codebase review, 2026-11-03

**Status:** all ten findings fixed on 2026-11-03, each with a test that fails on the code reviewed here.

Reviewed at commit `49e514e` ("feat: greet the user first when Jarvis starts"). Load assumed: one user, one Mac,
one Jarvis process, up to three agents working at once. Every finding was checked against the code; the line
numbers are from this commit.

**What the code does:** Jarvis listens all day, decides with a decision model whether a sentence is for it, and
either answers (DeepSeek) or hands the work to Claude Code, OpenCode or Hermes. Agent questions and approvals are
read out as they are; finished turns are retold. It remembers facts with local Mem0 and greets the user at start.

## Must fix

### 1. The startup greeting is saved as a memory and used as a memory search, every start
- **Where:** `src/jarvis/main.py:251-265` (`greet`), `src/jarvis/memory.py:56-70` (`LastExchangeMemory`).
- **What breaks:** `greet` sends the greeting note as a `user` message. Mem0 treats the newest `user` message as
  the user's words: it searches memory with the note as the query (so the context gets a second copy of the
  memories the note already holds) and sends the note to DeepSeek for fact extraction. Each start costs an
  extraction call and can store junk such as "Jarvis started at 01:35".
- **Fix:** In `LastExchangeMemory`, skip both the search and the save when the newest user message starts with
  `[Not the user speaking:`. One check in one place; the marker already exists.

### 2. An answered question keeps steering the speech check
- **Where:** `src/jarvis/state.py:138-139` (`notify_topic`), `src/jarvis/voice_gates.py:44-50`.
- **What breaks:** `active_agent_context` stores each question and approval, and the newest one is sent to the
  decision model as `jarvis_last_said` on every sentence. Nothing ever removes it: not `answer()`, not a reply
  made in the agent's own UI, not `interrupt()`, not the finished turn. After the first question of the day, the
  decision model is told for the rest of the day that Jarvis just asked "Postgres or SQLite?", which pulls
  unrelated sentences toward "addressed to Jarvis".
- **Fix:** `notify_topic` already gets the finished turn (`is_report=True`): pop the agent's entry there, and
  pop it in `answer()` and `interrupt()`. Three one-liners.

### 3. Stopping Hermes fails when a second task is queued behind a running one
- **Where:** `src/jarvis/agent_watchers.py:409-412` (`_run_loop`), `:470` (`send_task`).
- **What breaks:** `send_task` sets `_run_id` to the new run at once; `_run_loop` sets it to `None` when the
  *earlier* run finishes. Say "ask Hermes X", then "ask Hermes Y" while X runs: when X ends, `_run_id` is None
  and "stop Hermes" returns False while Y keeps running.
- **Fix:** Set `_run_id` in `_run_loop` when a run is taken from the queue, and clear it only there.

### 4. An answer that fails on the network loses the question
- **Where:** `src/jarvis/agent_watchers.py:477` (Hermes `answer`), OpenCode `answer` (`_pending_permission`,
  `_pending_form`).
- **What breaks:** Both clear the pending request *before* the HTTP reply arrives. If the POST raises, the
  request is gone: the agent still waits, and "yes" now returns `no_pending_question`.
- **Fix:** Clear the pending request after `resp.is_success`; on an exception leave it in place.

## Should fix

### 5. `answer_agent` without an id goes to the default coding agent
- **Where:** `src/jarvis/main.py:108-124` (`agent_tool_handlers`), `agent_watchers.py:548-554` (`find`).
- **What breaks:** `agent_id` is optional on all three tools. When the model omits it for an answer or a stop,
  `find(None)` returns `claude`. Hermes asks for approval, you say "yes", the model calls `answer_agent` with
  no id, and Claude answers `no_pending_question`.
- **Fix:** For `answer_agent` and `stop_agent`, when no id is given, pick the one agent with a pending request
  (answer) or a running turn (stop); fall back to the default only if there is none.

### 6. Stale words in the two prompts that steer behavior
- **Where:** `src/jarvis/main.py:21` (system prompt) and `src/jarvis/executive_filter.py:11` (`_CONTEXT`) still
  say Jarvis controls "coding agents (Claude Code, OpenCode)".
- **What breaks:** The speech check is told Jarvis is about coding, so "email Minh I'm late" is a weaker match
  for "addressed to Jarvis" than it should be. The benchmark passes today, so this is a drift risk, not a bug.
- **Fix:** Say "controls coding agents and Hermes (research, email, calendar, notes)". Both edits are gated by
  the benchmarks in `AGENTS.md`: run `bench_intent.py` and `bench_routing.py` before and after.

### 7. Tests missing where a break would be silent
- `JarvisIntentGate` (`voice_gates.py`) has no test: muted mode skipping the paid call, the pass-through of
  other frames, and the `jarvis_last_said` hand-off are unchecked.
- `HermesAdapter._watch` falling back to `GET /v1/runs/{id}` after a dropped stream (`agent_watchers.py:425-450`)
  is untested; a wrong status name there would make Jarvis poll forever.
- `test_unknown_default_stops_startup` (`tests/test_agent_watchers.py:286`) leaves its temp file behind.

## Nice to have

### 8. Option labels with punctuation never match a spoken answer
- **Where:** `src/jarvis/agent_watchers.py:43` (`form_answer`).
- **What breaks:** `\b` needs a letter-to-space edge. For a label like `C++` or `.env`, the edge after `+` or
  before `.` is not there, so the label is never matched and the raw sentence is sent instead.
- **Fix:** `(?<!\w)` and `(?!\w)` instead of `\b`.

### 9. Mute words inside ordinary sentences
- **Where:** `src/jarvis/state.py:40-47`.
- **What breaks:** "fix the silence threshold" mutes Jarvis at once, with no model check; "don't be quiet, you
  can talk" mutes because mute is checked first. Both are rare in practice.
- **Fix:** Drop bare `silence` from the mute list, and check unmute before mute.

### 10. Stale tool names in docs
- `docs/architecture.md:20` and `src/jarvis/voice_gates.py:14` say Faster-Whisper; Jarvis runs MLX Whisper.

## Decisions, not findings

These look like bugs but were chosen, with known limits. Revisit when the limit bites.

- **One pending request per agent** (OpenCode `_pending_permission` / `_pending_form`, Hermes
  `_pending_approval`). A second request before the first is answered replaces it. With one user answering by
  voice this has not happened; Claude Code serializes its own prompts with `_ask_lock`.
- **OpenCode events are not filtered by session** (`agent_watchers.py:301-303`). On purpose: a session started
  in OpenCode's own UI should still be heard. The cost is that `_session_id` follows whichever session spoke
  last, so a follow-up task may land in the wrong session when two are active.
- **Hermes approves its own commands.** Its guardian model ran `rm -rf` of a test folder without asking during
  the integration run. Only commands it flags reach Jarvis. Tightening is a Hermes setting
  (`approvals` in `~/.hermes/config.yaml`), not Jarvis code.
- **Two memories.** Hermes keeps its own; what you tell Hermes is not in Jarvis's Mem0, and the other way
  round.

## Verdict

Fix 1 to 4 first; together they are under 30 lines. 5 and 7 next.

Not checked: live behavior under a real dropped Hermes stream, and what Mem0 stores from a week of real
conversation (duplicates, drift).
