//! Plain data model mirroring `apps/brain/src/brain/schema.sql` plus dashboard-only view state.
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
    pub mode: String,
    pub repo: String,
    pub title: String,
    pub spec: String,
    pub metric_target: Option<String>,
    pub verify_cmd: Option<String>,
    pub status: String,
    pub priority: i64,
    pub strike_count: i64,
    pub child_strategy: String,
    pub runner: String,
    pub thread_id: Option<String>,
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
    pub thread_id: Option<String>,
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

/// One enrolled guest voice loaded from `JARVIS_HOME/speakers/speakers.json`.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct DiskSpeaker {
    pub id: String,
    pub name: String,
    pub clips: u64,
    pub speech_secs: f64,
    pub vouched: bool,
}

/// Speaker roster read off the UI thread from `SOUL.md`, `owner_voice.npy`, and `speakers/speakers.json`.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct DiskSpeakers {
    pub owner_name: Option<String>,
    pub owner_voiceprint: bool,
    pub guests: Vec<DiskSpeaker>,
}

/// One speaker row in `6 SPEAKERS` (saved disk roster merged with live `voice.stt` / `voice.speaker` telemetry).
#[derive(Debug, Clone, Default)]
pub struct Speaker {
    /// Canonical id: `"owner"`, `"voice_1"`, ..., or `"room"`.
    pub id: String,
    pub label: String,
    pub is_owner: bool,
    pub enrolled: bool,
    pub vouched: bool,
    pub known: bool,
    /// Latest Bayesian name-belief confidence (`0.0..=1.0`) from `voice.speaker`.
    pub confidence: Option<f64>,
    /// Utterances accepted by `voice.stt` (`passed: true`) in this daemon run.
    pub heard: u64,
    /// Turns that reached the executive gate (`voice.speaker`).
    pub turns: u64,
    /// Utterances rejected by the voice check (`voice.stt` `passed: false`).
    pub dropped: u64,
    /// Acoustic cosine similarity vs the owner voiceprint (`voice.stt`).
    pub last_sim: Option<f64>,
    pub min_sim: Option<f64>,
    pub max_sim: Option<f64>,
    /// Saved voiceprint metadata from `speakers.json`.
    pub clips: u64,
    pub speech_secs: f64,
    /// Daemon wall-clock unix timestamp (`ev.ts`) of the latest utterance.
    pub last_ts: Option<f64>,
}

impl Speaker {
    pub fn record_sim(&mut self, sim: f64) {
        self.last_sim = Some(sim);
        self.min_sim = Some(self.min_sim.map_or(sim, |m| m.min(sim)));
        self.max_sim = Some(self.max_sim.map_or(sim, |m| m.max(sim)));
    }

    /// Drops owner-cosine samples that were collected before this voice was identified: they measure
    /// similarity to the owner, which says nothing about a named guest.
    pub fn clear_sim(&mut self) {
        self.last_sim = None;
        self.min_sim = None;
        self.max_sim = None;
    }

    /// True once this voice has a name: enrolled on disk, confirmed by the tracker, or matched to one.
    pub fn is_identified(&self) -> bool {
        self.is_owner || self.enrolled || self.known
    }

    pub fn is_ephemeral(&self) -> bool {
        !self.is_owner && !self.enrolled && self.id != "room"
    }

    pub fn display_name(&self) -> &str {
        if !self.label.is_empty() {
            &self.label
        } else if self.is_owner {
            "Owner"
        } else if self.id == "room" {
            "Room"
        } else {
            &self.id
        }
    }

    pub fn status_label(&self, learning_owner: bool) -> String {
        if self.is_owner {
            if learning_owner {
                "learning".to_string()
            } else {
                "owner".to_string()
            }
        } else if self.id == "room" {
            "room".to_string()
        } else if self.enrolled {
            if self.vouched {
                "vouched".to_string()
            } else {
                "enrolled".to_string()
            }
        } else if self.known {
            "known".to_string()
        } else if let Some(conf) = self.confidence {
            if conf >= 0.50 {
                format!("{:.0}%?", conf * 100.0)
            } else {
                "guest".to_string()
            }
        } else {
            "guest".to_string()
        }
    }

    pub fn profile_label(&self) -> String {
        if self.is_owner {
            if self.enrolled {
                "voiceprint".to_string()
            } else {
                "unclaimed".to_string()
            }
        } else if self.enrolled && self.clips > 0 {
            format!("{}c·{:.1}s", self.clips, self.speech_secs)
        } else {
            self.id.clone()
        }
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
    pub speakers: DiskSpeakers,
    pub history: Vec<Trace>,
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

/// One pairing code waiting to be typed into a client, from `pair.requested` on the telemetry stream.
#[derive(Debug, Clone, Default)]
pub struct PendingPair {
    pub code: String,
    pub name: String,
    /// Unix epoch seconds on the daemon's clock; the cockpit runs on the same Mac.
    pub expires_at: f64,
}
impl PendingPair {
    /// `123 456`, the way `jarvis pair` prints it.
    pub fn spaced(&self) -> String {
        match self.code.len() {
            6 => format!("{} {}", &self.code[..3], &self.code[3..]),
            _ => self.code.clone(),
        }
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

/// How a stage of a turn is drawn: a normal step, a pass, the model's thinking, a return, or a rejection.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum StageTone {
    #[default]
    Info,
    Ok,
    Think,
    Return,
    Bad,
}

/// One stage of one voice turn, straight from a `voice.*` telemetry event.
#[derive(Debug, Clone, Default)]
pub struct TraceStage {
    /// Short label: STT, INTENT, CLEAN, MEM, PREP, THINK, TOOL, ROUTE, RETURN, RETELL, TTS.
    pub kind: String,
    pub tone: StageTone,
    pub ts: f64,
    pub text: String,
    pub detail: String,
}

impl TraceStage {
    pub fn new(kind: &str, tone: StageTone, ts: f64, text: impl Into<String>, detail: impl Into<String>) -> Self {
        Self {
            kind: kind.to_string(),
            tone,
            ts,
            text: text.into(),
            detail: detail.into(),
        }
    }
}

/// One turn from the user's sentence to Jarvis speaking, grouped by the `turn_id` the voice stamps.
#[derive(Debug, Clone, Default)]
pub struct Trace {
    pub id: String,
    pub started: f64,
    pub stages: Vec<TraceStage>,
    pub done: bool,
}

impl Trace {
    /// The sentence the user said, or the first thing the turn produced.
    pub fn headline(&self) -> String {
        self.stages
            .iter()
            .find(|s| s.kind == "STT")
            .or_else(|| self.stages.first())
            .map(|s| s.text.clone())
            .unwrap_or_else(|| "(no text)".to_string())
    }

    /// What was spoken back, if the turn got that far.
    pub fn spoken(&self) -> Option<&TraceStage> {
        self.stages.iter().rev().find(|s| s.kind == "TTS" && s.text != "(beep)")
    }

    pub fn stage(&self, kind: &str) -> Option<&TraceStage> {
        self.stages.iter().find(|s| s.kind == kind)
    }

    /// The cleaned sentence the conversation model read, else what Whisper heard.
    pub fn prepared_text(&self) -> String {
        self.stage("CLEAN")
            .or_else(|| self.stage("STT"))
            .map(|s| s.text.clone())
            .unwrap_or_default()
    }
}

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
