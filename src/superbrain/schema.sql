PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS system_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL DEFAULT 'ASSIST' CHECK (mode IN ('ASSIST', 'AUTONOMOUS')),
    daily_budget_usd REAL NOT NULL DEFAULT 20.0,
    spent_today_usd REAL NOT NULL DEFAULT 0.0,
    spend_date TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES tasks(id),
    kind TEXT NOT NULL CHECK (kind IN ('goal', 'problem')),
    repo TEXT NOT NULL,
    title TEXT NOT NULL,
    spec TEXT NOT NULL,
    metric_target TEXT,
    verify_cmd TEXT,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'ready', 'in_progress', 'done', 'blocked')),
    priority INTEGER NOT NULL DEFAULT 50,
    strike_count INTEGER NOT NULL DEFAULT 0,
    lease_until TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    role TEXT NOT NULL CHECK (role IN ('planner', 'builder')),
    pi_session_id TEXT,
    session_jsonl_path TEXT,
    summary TEXT NOT NULL,
    git_branch TEXT,
    git_sha TEXT,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    outcome TEXT NOT NULL CHECK (
        outcome IN ('pushed', 'decomposed', 'failed_verify', 'deferred_approval', 'timed_out', 'errored', 'reaped')
    ),
    verify_output TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
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
