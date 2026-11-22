//! Plain data model mirroring `apps/superbrain/src/superbrain/schema.sql` plus dashboard-only view state.
//!
//! Not every schema column is rendered by every pane, so dead-code linting is disabled for the
//! module: keeping the structs complete makes new panes a pure UI change.
#![allow(dead_code)]

use serde_json::Value;

#[derive(Debug, Clone, Default)]
pub struct SystemState {
    pub mode: String,
    pub daily_budget_usd: f64,
    pub spent_today_usd: f64,
    pub spend_date: String,
    pub last_good_sha: Option<String>,
    pub upgrade_runs: i64,
    pub upgrade_failures: i64,
}

impl SystemState {
    pub fn budget_ratio(&self) -> f64 {
        if self.daily_budget_usd <= 0.0 {
            return 0.0;
        }
        (self.spent_today_usd / self.daily_budget_usd).clamp(0.0, 1.0)
    }

    pub fn remaining_usd(&self) -> f64 {
        (self.daily_budget_usd - self.spent_today_usd).max(0.0)
    }
}

#[derive(Debug, Clone, Default)]
pub struct Task {
    pub id: String,
    pub parent_id: Option<String>,
    pub kind: String,
    pub repo: String,
    pub title: String,
    pub spec: String,
    pub metric_target: Option<String>,
    pub verify_cmd: Option<String>,
    pub status: String,
    pub priority: i64,
    pub strike_count: i64,
    pub child_strategy: String,
    pub best_metric: Option<f64>,
    pub best_branch: Option<String>,
    pub best_sha: Option<String>,
    pub lease_until: Option<String>,
    pub created_at: String,
    pub updated_at: String,
}

impl Task {
    pub fn glyph(&self) -> &'static str {
        match self.status.as_str() {
            "in_progress" => "▶",
            "ready" => "●",
            "done" => "✓",
            "blocked" => "✗",
            _ => "○",
        }
    }

    pub fn is_goal(&self) -> bool {
        self.kind == "goal"
    }
}

#[derive(Debug, Clone, Default)]
pub struct Attempt {
    pub id: i64,
    pub task_id: String,
    pub role: String,
    pub pi_session_id: Option<String>,
    pub session_jsonl_path: Option<String>,
    pub summary: String,
    pub git_branch: Option<String>,
    pub git_sha: Option<String>,
    pub cost_usd: f64,
    pub outcome: String,
    pub verify_output: Option<String>,
    pub metric_value: Option<f64>,
    pub skills_used: Vec<String>,
    pub diagnostic: Option<String>,
    pub reflected: bool,
    pub created_at: String,
}

#[derive(Debug, Clone, Default)]
pub struct Skill {
    pub slug: String,
    pub name: String,
    pub description: String,
    pub version: i64,
    pub sha256: String,
    pub status: String,
    pub content: String,
    pub previous_content: Option<String>,
    pub lesson: String,
    pub evidence_attempt_ids: Vec<i64>,
    pub loads_since_update: i64,
    pub successes_since_update: i64,
    pub failures_since_update: i64,
    pub updated_at: String,
}

#[derive(Debug, Clone, Default)]
pub struct Outbox {
    pub id: String,
    pub kind: String,
    pub task_id: Option<String>,
    pub summary_spoken: String,
    pub details_json: String,
    pub status: String,
    pub created_at: String,
}

impl Outbox {
    pub fn details(&self) -> Value {
        serde_json::from_str(&self.details_json).unwrap_or(Value::Null)
    }

    pub fn detail_str(&self, key: &str) -> Option<String> {
        self.details()
            .get(key)
            .and_then(|v| v.as_str())
            .map(|v| v.to_string())
    }
}

#[derive(Debug, Clone, Default)]
pub struct Counts {
    pub tasks: i64,
    pub attempts: i64,
    pub skills_active: i64,
    pub skills_total: i64,
    pub outbox_queued: i64,
    pub approvals_queued: i64,
}

#[derive(Debug, Clone, Default)]
pub struct Snapshot {
    pub state: SystemState,
    pub has_state: bool,
    pub tasks: Vec<Task>,
    pub attempts: Vec<Attempt>,
    pub skills: Vec<Skill>,
    pub outbox: Vec<Outbox>,
    pub counts: Counts,
    pub db_bytes: u64,
    pub wal_bytes: u64,
    pub shm_bytes: u64,
    pub page_count: i64,
    pub freelist_count: i64,
    pub page_size: i64,
}

impl Snapshot {
    pub fn task(&self, id: &str) -> Option<&Task> {
        self.tasks.iter().find(|t| t.id == id)
    }

    pub fn queued_approvals(&self) -> impl Iterator<Item = &Outbox> {
        self.outbox
            .iter()
            .filter(|o| o.kind == "approval_request" && o.status == "queued")
    }

    pub fn task_attempts(&self, task_id: &str) -> Vec<&Attempt> {
        self.attempts
            .iter()
            .filter(|a| a.task_id == task_id)
            .collect()
    }
}

/// One transient telemetry event from `GET /v1/telemetry/events`.
#[derive(Debug, Clone, Default)]
pub struct TelemetryEvent {
    pub seq: u64,
    pub ts: f64,
    pub event: String,
    pub value: Value,
}

impl TelemetryEvent {
    pub fn from_value(value: Value) -> Self {
        let seq = value.get("seq").and_then(Value::as_u64).unwrap_or(0);
        let ts = value.get("ts").and_then(Value::as_f64).unwrap_or(0.0);
        let event = value
            .get("event")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        Self {
            seq,
            ts,
            event,
            value,
        }
    }

    pub fn str(&self, key: &str) -> Option<&str> {
        self.value.get(key).and_then(Value::as_str)
    }

    pub fn text(&self, key: &str) -> String {
        self.str(key).unwrap_or_default().to_string()
    }

    pub fn f64(&self, key: &str) -> Option<f64> {
        self.value.get(key).and_then(Value::as_f64)
    }

    pub fn i64(&self, key: &str) -> Option<i64> {
        self.value.get(key).and_then(Value::as_i64)
    }

    pub fn bool(&self, key: &str) -> Option<bool> {
        self.value.get(key).and_then(Value::as_bool)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TurnKind {
    User,
    Assistant,
    Tool,
    ToolResult,
    System,
    Meta,
}

/// One parsed `pi --mode json` session line, ready for rendering.
#[derive(Debug, Clone)]
pub struct Turn {
    pub kind: TurnKind,
    pub title: String,
    pub body: String,
    pub is_error: bool,
    pub cost: f64,
}

impl Turn {
    pub fn new(kind: TurnKind, title: impl Into<String>, body: impl Into<String>) -> Self {
        Self {
            kind,
            title: title.into(),
            body: body.into(),
            is_error: false,
            cost: 0.0,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn budget_ratio_is_clamped() {
        let state = SystemState {
            daily_budget_usd: 200.0,
            spent_today_usd: 250.0,
            ..Default::default()
        };
        assert_eq!(state.budget_ratio(), 1.0);
        assert_eq!(state.remaining_usd(), 0.0);
    }

    #[test]
    fn telemetry_accessors_tolerate_missing_fields() {
        let ev = TelemetryEvent::from_value(serde_json::json!({
            "seq": 7,
            "ts": 1.5,
            "event": "pi.progress",
            "cost_usd": 0.25,
            "task_id": "SG-1"
        }));
        assert_eq!(ev.seq, 7);
        assert_eq!(ev.event, "pi.progress");
        assert_eq!(ev.f64("cost_usd"), Some(0.25));
        assert_eq!(ev.text("task_id"), "SG-1");
        assert_eq!(ev.text("missing"), "");
    }

    #[test]
    fn outbox_details_parse_json() {
        let ob = Outbox {
            details_json: r#"{"approved_sha":"deadbeef"}"#.to_string(),
            ..Default::default()
        };
        assert_eq!(ob.detail_str("approved_sha").as_deref(), Some("deadbeef"));
    }
}
