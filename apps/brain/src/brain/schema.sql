PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS system_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL DEFAULT 'ASSIST' CHECK (mode IN ('ASSIST', 'AUTONOMOUS')),
    daily_budget_usd REAL NOT NULL DEFAULT 200.0,
    spent_today_usd REAL NOT NULL DEFAULT 0.0,
    spend_date TEXT NOT NULL,
    last_good_sha TEXT,
    upgrade_runs INTEGER NOT NULL DEFAULT 0,
    upgrade_failures INTEGER NOT NULL DEFAULT 0,
    -- 7-day probation on a self-upgrade that edits the Improver itself: newest scorecard vs the recorded one
    improver_probation_until TEXT,
    improver_rollback_sha TEXT,
    improver_baseline_json TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES tasks(id),
    kind TEXT NOT NULL CHECK (kind IN ('goal', 'problem')),
    mode TEXT NOT NULL DEFAULT 'AUTONOMOUS' CHECK (mode IN ('ASSIST', 'AUTONOMOUS')),
    repo TEXT NOT NULL,
    title TEXT NOT NULL,
    spec TEXT NOT NULL,
    metric_target TEXT,
    verify_cmd TEXT,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'ready', 'in_progress', 'done', 'blocked')),
    priority INTEGER NOT NULL DEFAULT 50,
    strike_count INTEGER NOT NULL DEFAULT 0,
    child_strategy TEXT NOT NULL DEFAULT 'all' CHECK (child_strategy IN ('all', 'any')),
    -- which runner executes this task: the pi Builder (default), or a coding client the brain drives
    runner TEXT NOT NULL DEFAULT 'pi',
    thread_id TEXT,
    -- idempotency for timer/webhook/voice submissions; one active row per key
    dedupe_key TEXT,
    best_metric REAL,
    best_branch TEXT,
    best_sha TEXT,
    lease_until TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    role TEXT NOT NULL CHECK (role IN ('planner', 'builder', 'improver', 't3', 'ephemeral', 'claude', 'opencode')),
    pi_session_id TEXT,
    session_jsonl_path TEXT,
    -- the coding client's thread/session id, when a runner other than the pi Builder ran it
    thread_id TEXT,
    summary TEXT NOT NULL,
    git_branch TEXT,
    git_sha TEXT,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    outcome TEXT NOT NULL CHECK (
        outcome IN ('pushed', 'decomposed', 'failed_verify', 'deferred_approval', 'timed_out', 'errored', 'reaped')
    ),
    verify_output TEXT,
    metric_value REAL,
    skills_used TEXT NOT NULL DEFAULT '[]',
    diagnostic TEXT,
    reflected INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- One bound long-lived thread per repo/runner: the brain continues it instead of opening a new one per task.
CREATE TABLE IF NOT EXISTS threads (
    repo TEXT PRIMARY KEY,
    runner TEXT NOT NULL DEFAULT 't3',
    thread_id TEXT NOT NULL,
    project_id TEXT,
    title TEXT NOT NULL DEFAULT '',
    worktree_path TEXT,
    branch TEXT NOT NULL DEFAULT '',
    last_sequence INTEGER,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS skills (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
    content TEXT NOT NULL,
    previous_content TEXT,
    lesson TEXT NOT NULL DEFAULT '',
    evidence_attempt_ids TEXT NOT NULL DEFAULT '[]',
    loads_since_update INTEGER NOT NULL DEFAULT 0,
    successes_since_update INTEGER NOT NULL DEFAULT 0,
    failures_since_update INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('approval_request', 'morning_brief', 'blocker', 'report')),
    task_id TEXT REFERENCES tasks(id),
    resume_session_id TEXT,
    summary_spoken TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'spoken', 'approved', 'rejected')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- One row per voice turn, interactive run, timer check or board attempt: the experience the Improver mines.
CREATE TABLE IF NOT EXISTS trace_runs (
    trace_id TEXT PRIMARY KEY,
    turn_id TEXT,
    source TEXT NOT NULL DEFAULT 'voice',
    surface TEXT NOT NULL DEFAULT '',
    runner TEXT,
    repo TEXT,
    task_id TEXT,
    attempt_id INTEGER,
    outcome TEXT NOT NULL DEFAULT 'ok',
    friction_kind TEXT,
    causal_sig TEXT,
    latency_ms INTEGER,
    ttfb_ms INTEGER,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    git_sha TEXT,
    prompt_sha TEXT,
    skills_used TEXT NOT NULL DEFAULT '[]',
    user_input TEXT NOT NULL DEFAULT '',
    output_summary TEXT NOT NULL DEFAULT '',
    trace_path TEXT NOT NULL DEFAULT '',
    superseded_by TEXT,
    reflected INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_trace_runs_created ON trace_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trace_runs_surface ON trace_runs(surface, outcome, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trace_runs_sha ON trace_runs(git_sha, outcome);

CREATE VIRTUAL TABLE IF NOT EXISTS trace_fts USING fts5(
    trace_id UNINDEXED,
    user_input,
    output_summary,
    causal_sig,
    tokenize='porter unicode61'
);

-- One row per harness fix the Improver proposed: the manifesto, what the surface scored before, and what became
-- of it. This is the loop's own memory, so a rejected or falsified edit is never proposed again unchanged.
CREATE TABLE IF NOT EXISTS harness_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    surface TEXT NOT NULL,
    spec_hash TEXT NOT NULL,
    title TEXT NOT NULL,
    spec TEXT NOT NULL,
    predicted_fix TEXT NOT NULL DEFAULT '',
    at_risk_regressions TEXT NOT NULL DEFAULT '',
    baseline_fail_rate REAL NOT NULL DEFAULT 0.0,
    baseline_runs INTEGER NOT NULL DEFAULT 0,
    applied_at TEXT,
    applied_sha TEXT,
    rollback_sha TEXT,
    outcome TEXT NOT NULL DEFAULT 'proposed' CHECK (
        outcome IN ('proposed', 'pushed', 'failed_verify', 'rejected', 'rolled_back', 'falsified', 'verified')
    ),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_harness_proposals_surface ON harness_proposals(surface, created_at DESC);

-- Follow-ups and schedules. A due timer creates a board task (`action='task'`) or runs a read-only check
-- (`action='check'`) whose answer goes to the outbox. `next_run_at` is UTC; `timezone` is the IANA zone the
-- cron expression is read in, so a morning check stays at 08:00 across daylight-saving changes.
CREATE TABLE IF NOT EXISTS timers (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('followup', 'schedule')),
    action TEXT NOT NULL DEFAULT 'check' CHECK (action IN ('task', 'check')),
    cron_expr TEXT,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    next_run_at TEXT NOT NULL,
    repo TEXT,
    mode TEXT NOT NULL DEFAULT 'ASSIST' CHECK (mode IN ('ASSIST', 'AUTONOMOUS')),
    title TEXT NOT NULL,
    prompt_spec TEXT NOT NULL,
    verify_cmd TEXT,
    max_cost_usd REAL,
    created_by TEXT NOT NULL DEFAULT 'owner' CHECK (created_by IN ('owner', 'agent')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'firing', 'paused', 'done')),
    task_id TEXT REFERENCES tasks(id),
    chain_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
