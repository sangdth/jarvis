//! Application state, event application, and keyboard-driven actions.

use std::collections::HashMap;
use std::io::Write;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::mpsc::Sender;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use ratatui::crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

use crate::config::Config;
use crate::http;
use crate::model::{
    Outbox, PendingPair, Snapshot, StageTone, Task, TelemetryEvent, Trace, TraceStage, Turn,
};

#[derive(Debug)]
pub enum AppEvent {
    Snapshot(Box<Snapshot>),
    SnapshotError(String),
    Daemon(bool, Option<String>),
    Telemetry(TelemetryEvent),
    SessionReset {
        path: PathBuf,
    },
    SessionAppend {
        path: PathBuf,
        turns: Vec<Turn>,
    },
    Toast(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Pane {
    Topology,
    Xray,
    Prompts,
    Logs,
    Tasks,
    Ledger,
}

impl Pane {
    pub fn label(self) -> &'static str {
        match self {
            Pane::Topology => "1 FLOW",
            Pane::Xray => "2 X-RAY",
            Pane::Prompts => "3 PAYLOAD",
            Pane::Logs => "4 LOGS",
            Pane::Tasks => "5 TASKS",
            Pane::Ledger => "6 LEDGER",
        }
    }

    pub fn next(self) -> Self {
        match self {
            Pane::Topology => Pane::Xray,
            Pane::Xray => Pane::Prompts,
            Pane::Prompts => Pane::Logs,
            Pane::Logs => Pane::Tasks,
            Pane::Tasks => Pane::Ledger,
            Pane::Ledger => Pane::Topology,
        }
    }

    pub fn from_digit(digit: char) -> Option<Self> {
        match digit {
            '1' => Some(Pane::Topology),
            '2' => Some(Pane::Xray),
            '3' => Some(Pane::Prompts),
            '4' => Some(Pane::Logs),
            '5' => Some(Pane::Tasks),
            '6' => Some(Pane::Ledger),
            _ => None,
        }
    }
}

/// Which half of a turn the payload pane is showing: what the model was given, what the runner did, or the retell.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PayloadTab {
    Voice,
    Runner,
    Retell,
}

impl PayloadTab {
    pub const ALL: [PayloadTab; 3] = [PayloadTab::Voice, PayloadTab::Runner, PayloadTab::Retell];

    pub fn label(self) -> &'static str {
        match self {
            PayloadTab::Voice => "Voice Ctx",
            PayloadTab::Runner => "Runner",
            PayloadTab::Retell => "Retell",
        }
    }

    pub fn next(self) -> Self {
        match self {
            PayloadTab::Voice => PayloadTab::Runner,
            PayloadTab::Runner => PayloadTab::Retell,
            PayloadTab::Retell => PayloadTab::Voice,
        }
    }
}

/// Which lines the log pane keeps: everything, the voice's turn, the brain's routing, or only what went wrong.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LogFilter {
    All,
    Voice,
    Brain,
    Errors,
}

impl LogFilter {
    pub fn label(self) -> &'static str {
        match self {
            LogFilter::All => "all",
            LogFilter::Voice => "voice",
            LogFilter::Brain => "brain",
            LogFilter::Errors => "errors",
        }
    }

    pub fn matches(self, event: &TelemetryEvent) -> bool {
        match self {
            LogFilter::All => true,
            LogFilter::Voice => event.event.starts_with("voice."),
            LogFilter::Brain => {
                event.event.starts_with("brain.") || event.event.starts_with("run.") || event.event == "http.inbound"
            }
            LogFilter::Errors => {
                event.event.contains("failed")
                    || event.bool("passed") == Some(false)
                    || !event.text("error").is_empty()
            }
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LedgerTab {
    Attempts,
    Skills,
    Outbox,
    Notes,
    Database,
}

impl LedgerTab {
    pub const ALL: [LedgerTab; 5] = [
        LedgerTab::Attempts,
        LedgerTab::Skills,
        LedgerTab::Outbox,
        LedgerTab::Notes,
        LedgerTab::Database,
    ];

    pub fn label(self) -> &'static str {
        match self {
            LedgerTab::Attempts => "Attempts",
            LedgerTab::Skills => "Skills",
            LedgerTab::Outbox => "Outbox",
            LedgerTab::Notes => "Notes",
            LedgerTab::Database => "DB & Logs",
        }
    }

    fn index(self) -> usize {
        LedgerTab::ALL.iter().position(|t| *t == self).unwrap_or(0)
    }

    fn step(self, delta: isize) -> Self {
        let len = LedgerTab::ALL.len() as isize;
        let idx = (self.index() as isize + delta).rem_euclid(len) as usize;
        LedgerTab::ALL[idx]
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Modal {
    Help,
    Node(usize),
    Attempt(i64),
    Skill(String),
    Task(String),
}

pub struct App {
    pub cfg: Config,
    pub tx: Sender<AppEvent>,
    pub pin: Arc<Mutex<Option<PathBuf>>>,

    pub snapshot: Snapshot,
    pub snapshot_age: Option<Instant>,
    pub snapshot_error: Option<String>,

    pub events: Vec<TelemetryEvent>,
    pub phase: String,
    pub phase_task: Option<String>,
    pub phase_role: Option<String>,
    pub live_cost: f64,
    pub live_elapsed: f64,

    pub daemon_connected: bool,
    pub daemon_error: Option<String>,
    pub daemon_pid: Option<i64>,
    pub daemon_uptime: f64,
    pub active_run: Option<String>,

    /// Turns from the voice's own telemetry: the newest is the one on screen.
    pub traces: Vec<Trace>,
    pub sel_trace: usize,
    pub payload_tab: PayloadTab,
    pub log_filter: LogFilter,
    /// Where the ear is (`local`, `paired only`, ...) and the speech-check setup, from `voice.ear`.
    pub ear: String,
    /// `VOICE` or `BEEP_ONLY`, from `voice.mode`.
    pub voice_mode: String,
    /// `master check` or `any voice`, from `voice.ear`.
    pub check_mode: String,

    pub session_path: Option<PathBuf>,
    pub turns: Vec<Turn>,
    pub follow: bool,
    pub expanded: Option<usize>,
    pub sel_prompts: usize,

    pub focus: Pane,
    pub zoomed: Option<Pane>,
    pub ledger_tab: LedgerTab,

    pub sel_topology: usize,
    pub sel_tasks: usize,
    pub sel_attempts: usize,
    pub sel_skills: usize,
    pub sel_outbox: usize,

    pub scroll_ledger: usize,

    pub modal: Option<Modal>,
    pub command: Option<String>,
    pub toast: Option<(String, Instant)>,
    pub pulses: HashMap<String, Instant>,
    /// A client asked to pair: the code stays on screen until it is used, expires, or too many tries.
    pub pending_pair: Option<PendingPair>,

    pub should_quit: bool,
    pub dirty: bool,
    pub tick: u64,
    pub notes_cache: HashMap<String, (SystemTime, String)>,
    pub logs_cache: Option<(SystemTime, String)>,
}

impl App {
    pub fn new(cfg: Config, tx: Sender<AppEvent>, pin: Arc<Mutex<Option<PathBuf>>>) -> Self {
        Self {
            cfg,
            tx,
            pin,
            snapshot: Snapshot::default(),
            snapshot_age: None,
            snapshot_error: None,
            events: Vec::new(),
            phase: "starting".to_string(),
            phase_task: None,
            phase_role: None,
            live_cost: 0.0,
            live_elapsed: 0.0,
            daemon_connected: false,
            daemon_error: None,
            daemon_pid: None,
            daemon_uptime: 0.0,
            active_run: None,
            traces: Vec::new(),
            sel_trace: 0,
            payload_tab: PayloadTab::Voice,
            log_filter: LogFilter::All,
            ear: "—".to_string(),
            voice_mode: "—".to_string(),
            check_mode: "—".to_string(),
            session_path: None,
            turns: Vec::new(),
            follow: true,
            expanded: None,
            sel_prompts: 0,
            focus: Pane::Topology,
            zoomed: None,
            ledger_tab: LedgerTab::Attempts,
            sel_topology: 0,
            sel_tasks: 0,
            sel_attempts: 0,
            sel_skills: 0,
            sel_outbox: 0,
            scroll_ledger: 0,
            modal: None,
            command: None,
            toast: None,
            pulses: HashMap::new(),
            pending_pair: None,
            should_quit: false,
            dirty: true,
            tick: 0,
            notes_cache: HashMap::new(),
            logs_cache: None,
        }
    }

    pub fn pulse(&mut self, node: &str) {
        self.pulses.insert(node.to_string(), Instant::now());
    }

    pub fn pulse_level(&self, node: &str) -> f64 {
        let Some(when) = self.pulses.get(node) else {
            return 0.0;
        };
        let elapsed = when.elapsed().as_secs_f64();
        if elapsed >= 1.5 {
            0.0
        } else {
            (1.0 - elapsed / 1.5).clamp(0.0, 1.0)
        }
    }

    pub fn animating(&self) -> bool {
        self.pulses.values().any(|when| when.elapsed() < Duration::from_millis(1500))
    }

    pub fn toast_active(&self) -> bool {
        self.toast
            .as_ref()
            .map(|(_, when)| when.elapsed() < Duration::from_secs(6))
            .unwrap_or(false)
    }

    pub fn toast_text(&self) -> Option<&str> {
        self.toast.as_ref().map(|(msg, _)| msg.as_str())
    }

    /// Seconds until the pending pairing code expires; `None` when nothing is pending, or it already has.
    pub fn pair_seconds_left(&self) -> Option<i64> {
        let pair = self.pending_pair.as_ref()?;
        let left = (pair.expires_at - now_unix()).round() as i64;
        (left > 0).then_some(left)
    }

    pub fn is_running(&self) -> bool {
        matches!(
            self.phase.as_str(),
            "claimed" | "prepare" | "pi_running" | "verifying" | "pushing" | "auto_research"
                | "skill_improver"
        )
    }

    pub fn active_worker(&self) -> Option<&str> {
        if self.phase == "pi_running" {
            self.phase_role.as_deref()
        } else if matches!(self.phase.as_str(), "verifying" | "pushing" | "prepare") {
            Some("builder")
        } else if self.phase == "auto_research" {
            Some("planner")
        } else if self.phase == "skill_improver" {
            Some("improver")
        } else {
            None
        }
    }

    pub fn node_active(&self, node: &str) -> bool {
        let pulsed = self.pulse_level(node) > 0.0;
        let worker = self.active_worker();
        match node {
            "db" => true,
            "rec" => self.is_running() || pulsed,
            "local" => self.ear.starts_with("local") || pulsed,
            "planner" => worker == Some("planner") || pulsed,
            "builder" => worker == Some("builder") || pulsed,
            "improver" => worker == Some("improver") || pulsed,
            "worktree" => matches!(self.phase.as_str(), "prepare" | "verifying" | "pushing") || pulsed,
            "verify" => self.phase == "verifying" || pulsed,
            "github" => self.phase == "pushing" || pulsed,
            "server" => self.daemon_connected || pulsed,
            _ => pulsed,
        }
    }

    /// The turn the x-ray pane and the payload pane are showing.
    pub fn selected_trace(&self) -> Option<&Trace> {
        self.traces.get(self.sel_trace)
    }

    /// The turn in progress, for the header's per-stage timings.
    pub fn newest_trace(&self) -> Option<&Trace> {
        self.traces.last()
    }

    /// The turn with this id, creating it on first sight. Old turns fall off so a day of talk cannot grow forever.
    fn trace_mut(&mut self, id: &str, ts: f64) -> &mut Trace {
        let idx = match self.traces.iter().rposition(|t| t.id == id) {
            Some(idx) => idx,
            None => {
                self.traces.push(Trace {
                    id: id.to_string(),
                    started: ts,
                    ..Trace::default()
                });
                if self.traces.len() > 40 {
                    self.traces.remove(0);
                }
                self.traces.len() - 1
            }
        };
        &mut self.traces[idx]
    }

    pub fn apply(&mut self, event: AppEvent) {
        self.dirty = true;
        match event {
            AppEvent::Snapshot(snapshot) => {
                self.snapshot = *snapshot;
                self.snapshot_age = Some(Instant::now());
                self.snapshot_error = None;
            }
            AppEvent::SnapshotError(err) => {
                self.snapshot_error = Some(err);
            }
            AppEvent::Daemon(ok, err) => {
                self.daemon_connected = ok;
                self.daemon_error = err;
                if ok {
                    self.pulse("server");
                }
            }
            AppEvent::Telemetry(ev) => self.apply_telemetry(ev),
            AppEvent::SessionReset { path } => {
                self.session_path = Some(path);
                self.turns.clear();
                self.expanded = None;
                self.sel_prompts = 0;
            }
            AppEvent::SessionAppend { path, turns } => {
                self.session_path = Some(path);
                for turn in turns {
                    self.turns.push(turn);
                }
                if self.turns.len() > 600 {
                    let drop = self.turns.len() - 600;
                    self.turns.drain(..drop);
                }
                if self.follow {
                    self.sel_prompts = self.turns.len().saturating_sub(1);
                }
            }
            AppEvent::Toast(msg) => {
                self.toast = Some((msg, Instant::now()));
            }
        }
    }

    fn apply_telemetry(&mut self, ev: TelemetryEvent) {
        match ev.event.as_str() {
            "http.inbound" => {
                let source = ev.text("source");
                if source.contains("iphone") {
                    self.pulse("iphone");
                } else {
                    self.pulse("macbook");
                }
                self.pulse("server");
            }
            "http.completed" => {
                self.pulse("server");
                self.active_run = None;
            }
            "run.status" => {
                if ev.text("status") == "running" {
                    self.active_run = ev.str("run_id").map(str::to_string);
                    self.pulse("server");
                } else {
                    self.active_run = None;
                }
            }
            "approval.request" => {
                self.pulse("server");
                self.pulse("board");
            }
            "approval.responded" => {
                self.pulse("board");
            }
            "reconciler.phase" => {
                self.phase = ev.text("phase");
                self.phase_task = ev.str("task_id").map(str::to_string);
                self.phase_role = ev.str("role").map(str::to_string);
                self.pulse("reconciler");
                match self.phase.as_str() {
                    "verifying" => {
                        self.pulse("verify");
                        self.pulse("builder");
                    }
                    "pushing" => {
                        self.pulse("github");
                        self.pulse("builder");
                    }
                    "prepare" => self.pulse("worktree"),
                    "auto_research" => self.pulse("planner"),
                    "skill_improver" => self.pulse("improver"),
                    "pi_running" => match self.phase_role.as_deref() {
                        Some("builder") => self.pulse("builder"),
                        Some("planner") => self.pulse("planner"),
                        Some("improver") => self.pulse("improver"),
                        _ => {}
                    },
                    _ => {}
                }
            }
            "board.rpc" => {
                self.pulse("board");
                match ev.text("method").as_str() {
                    "research_scout" => self.pulse("scout"),
                    "upsert_task" => self.pulse("planner"),
                    "propose_skill_change" => {
                        self.pulse("improver");
                        self.pulse("skills");
                    }
                    "defer_for_approval" => self.pulse("server"),
                    _ => {}
                }
            }
            "pi.progress" => {
                self.live_cost = ev.f64("cost_usd").unwrap_or(0.0);
                self.live_elapsed = ev.f64("elapsed_s").unwrap_or(0.0);
                match ev.text("role").as_str() {
                    "builder" => self.pulse("builder"),
                    "planner" => self.pulse("planner"),
                    "improver" => self.pulse("improver"),
                    _ => {}
                }
            }
            "attempt.finished" => {
                self.pulse("db");
                self.pulse("reconciler");
            }
            "heartbeat" => {
                self.daemon_connected = true;
                self.daemon_pid = ev.i64("pid");
                self.daemon_uptime = ev.f64("uptime_s").unwrap_or(0.0);
                return; // keepalive only: keep 5s beats from pushing turns out of the log pane
            }
            "pair.requested" => {
                let code = ev.text("code");
                let expires_at = ev.f64("expires_at").unwrap_or(0.0);
                // The stream replays its ring buffer, so an old code can arrive on connect: keep only a live one.
                if !code.is_empty() && expires_at > now_unix() {
                    if matches!(self.pending_pair.as_ref(), Some(pair) if pair.code == code) {
                        if let Some(pair) = self.pending_pair.as_mut() {
                            pair.expires_at = expires_at; // asked for again: the same digits stay on screen
                        }
                    } else {
                        let pair = PendingPair {
                            code,
                            name: ev.text("name"),
                            expires_at,
                        };
                        self.toast = Some((format!("Pairing code {}", pair.spaced()), Instant::now()));
                        self.pending_pair = Some(pair);
                    }
                    self.pulse("server");
                }
            }
            "pair.resolved" => {
                let ok = ev.bool("ok").unwrap_or(false);
                self.pending_pair = None;
                self.toast = Some((
                    if ok {
                        format!("Paired {}", ev.text("device"))
                    } else {
                        format!("Pairing reset: {}", ev.text("reason"))
                    },
                    Instant::now(),
                ));
                self.pulse("server");
            }
            // ------------------------------------------------------------ the voice's turn, stage by stage
            "voice.ear" => {
                let local = ev.bool("local_audio").unwrap_or(true);
                let relay = ev.bool("relay").unwrap_or(false);
                self.ear = match (local, relay) {
                    (true, true) => "local + paired".to_string(),
                    (true, false) => "local".to_string(),
                    (false, true) => "paired only".to_string(),
                    (false, false) => "none".to_string(),
                };
                self.check_mode = if ev.bool("master_check").unwrap_or(false) {
                    "master check".to_string()
                } else {
                    "any voice".to_string()
                };
                self.pulse("local");
            }
            "voice.stt" => {
                self.pulse("stt");
                let passed = ev.bool("passed").unwrap_or(true);
                let ms = ev.i64("ms").unwrap_or(0);
                let detail = match ev.f64("score") {
                    Some(score) => format!("spk {score:.2} · {ms}ms"),
                    None => format!("spk off · {ms}ms"),
                };
                let tone = if passed { StageTone::Ok } else { StageTone::Bad };
                let id = ev.text("turn_id");
                let text = ev.text("text");
                let ts = ev.ts;
                let trace = self.trace_mut(&id, ts);
                trace.stages.push(TraceStage::new("STT", tone, ts, text, detail));
                if !passed {
                    trace.done = true; // dropped by the voice check: no turn follows
                }
                if self.follow {
                    self.sel_trace = self.traces.len().saturating_sub(1);
                }
            }
            "voice.intent" => {
                self.pulse("intent");
                let intent = ev.text("intent");
                let tone = match intent.as_str() {
                    "ADDRESSED_TO_JARVIS" => StageTone::Ok,
                    "IGNORE_BACKGROUND" => StageTone::Bad,
                    _ => StageTone::Info,
                };
                let detail = format!("{}ms · {}", ev.i64("ms").unwrap_or(0), ev.text("speaker"));
                self.push_stage(&ev, "INTENT", tone, intent, detail);
            }
            "voice.fastcmd" => {
                self.pulse("intent");
                let command = ev.text("command");
                let text = ev.text("text");
                self.push_stage(&ev, "FAST", StageTone::Ok, command, text);
            }
            "voice.cleanup" => {
                self.pulse("intent");
                let detail = if ev.bool("changed").unwrap_or(false) {
                    format!("{}ms · changed", ev.i64("ms").unwrap_or(0))
                } else {
                    format!("{}ms · unchanged", ev.i64("ms").unwrap_or(0))
                };
                let text = ev.text("text");
                self.push_stage(&ev, "CLEAN", StageTone::Info, text, detail);
            }
            "voice.memory" => {
                self.pulse("mem0");
                let detail = format!(
                    "{} hit(s) · {}ms",
                    ev.i64("hits").unwrap_or(0),
                    ev.i64("ms").unwrap_or(0)
                );
                let text = ev.text("text");
                self.push_stage(&ev, "MEM", StageTone::Info, text, detail);
            }
            "voice.request" => {
                self.pulse("exec");
                let detail = format!(
                    "{} msgs · {}",
                    ev.i64("messages").unwrap_or(0),
                    ev.text("tools")
                );
                let text = ev.text("user_tail");
                self.push_stage(&ev, "PREP", StageTone::Info, text, detail);
            }
            "voice.thinking" => {
                self.pulse("exec");
                // Reasoning arrives token by token: keep it in one stage instead of one stage per chunk.
                let id = ev.text("turn_id");
                let text = ev.text("text");
                let ts = ev.ts;
                let trace = self.trace_mut(&id, ts);
                match trace.stages.last_mut() {
                    Some(last) if last.kind == "THINK" => {
                        last.text.push_str(&text);
                        if last.text.chars().count() > 2000 {
                            last.text = last.text.chars().take(2000).collect();
                        }
                    }
                    _ => trace
                        .stages
                        .push(TraceStage::new("THINK", StageTone::Think, ts, text, "")),
                }
            }
            "voice.tool" => {
                self.pulse("exec");
                let tool = ev.text("tool");
                let phase = ev.text("phase");
                let detail = if phase == "result" {
                    ev.text("result")
                } else {
                    "called".to_string()
                };
                self.push_stage(&ev, "TOOL", StageTone::Ok, format!("{tool} · {phase}"), detail);
            }
            "voice.latency" => {
                let text = ev.text("text");
                self.push_stage(&ev, "LAT", StageTone::Info, text, "");
            }
            "voice.dispatch" => {
                self.pulse("gw");
                self.pulse("route");
                let detail = format!("run {} → {}", ev.text("run_id"), ev.text("session"));
                let text = ev.text("text");
                self.push_stage(&ev, "DISPATCH", StageTone::Ok, text, detail);
            }
            "voice.return" => {
                self.pulse("runners");
                let detail = format!(
                    "{} chars · {}",
                    ev.i64("chars").unwrap_or(0),
                    ev.text("outcome")
                );
                let text = ev.text("text");
                self.push_stage(&ev, "RETURN", StageTone::Return, text, detail);
            }
            "voice.retell" => {
                self.pulse("exec");
                let detail = format!("{} chars", ev.i64("chars").unwrap_or(0));
                let text = ev.text("text");
                self.push_stage(&ev, "RETELL", StageTone::Return, text, detail);
            }
            "voice.tts" => {
                self.pulse("tts");
                let mode = ev.text("mode");
                let text = ev.text("text");
                let id = ev.text("turn_id");
                let ts = ev.ts;
                let trace = self.trace_mut(&id, ts);
                trace.stages.push(TraceStage::new("TTS", StageTone::Ok, ts, text, mode));
                trace.done = true;
            }
            "voice.mode" => {
                self.voice_mode = ev.text("mode");
            }
            "brain.route" => {
                self.pulse("route");
                let phase = ev.text("phase");
                let tone = if phase == "done" {
                    StageTone::Ok
                } else {
                    StageTone::Info
                };
                let runner = ev.text("runner");
                let choice = ev.text("choice");
                let text = if runner.is_empty() {
                    choice
                } else {
                    format!("{choice} · {runner}")
                };
                let detail = if phase == "done" {
                    format!("{} · {}", ev.text("outcome"), ev.text("summary"))
                } else {
                    ev.text("project")
                };
                self.push_stage(&ev, "ROUTE", tone, text, detail);
            }
            _ => {}
        }
        self.events.push(ev);
        if self.events.len() > 500 {
            self.events.remove(0);
        }
    }

    /// Appends one stage to the turn the event belongs to.
    fn push_stage(
        &mut self,
        ev: &TelemetryEvent,
        kind: &str,
        tone: StageTone,
        text: impl Into<String>,
        detail: impl Into<String>,
    ) {
        let id = ev.text("turn_id");
        let ts = ev.ts;
        let trace = self.trace_mut(&id, ts);
        trace.stages.push(TraceStage::new(kind, tone, ts, text, detail));
    }

    // ---------------------------------------------------------------- selections

    pub fn flat_task_rows(&self) -> Vec<(usize, usize)> {
        flat_tasks(&self.snapshot)
    }

    pub fn selected_task(&self) -> Option<&Task> {
        let rows = self.flat_task_rows();
        rows.get(self.sel_tasks)
            .and_then(|(idx, _)| self.snapshot.tasks.get(*idx))
    }

    pub fn selected_attempt(&self) -> Option<&crate::model::Attempt> {
        self.snapshot.attempts.get(self.sel_attempts)
    }

    pub fn selected_skill(&self) -> Option<&crate::model::Skill> {
        self.snapshot.skills.get(self.sel_skills)
    }

    pub fn selected_outbox(&self) -> Option<&Outbox> {
        self.snapshot.outbox.get(self.sel_outbox)
    }

    pub fn selected_turn(&self) -> Option<&Turn> {
        self.turns.get(self.sel_prompts)
    }

    // ---------------------------------------------------------------- keyboard

    pub fn handle_key(&mut self, key: KeyEvent) {
        self.dirty = true;
        if self.command.is_some() {
            self.handle_command_key(key);
            return;
        }
        if self.modal.is_some() {
            match key.code {
                KeyCode::Esc | KeyCode::Enter | KeyCode::Char('q') | KeyCode::Char('?') => {
                    self.modal = None;
                    self.scroll_ledger = 0;
                }
                KeyCode::Char('j') | KeyCode::Down | KeyCode::PageDown => {
                    self.scroll_ledger = self.scroll_ledger.saturating_add(6);
                }
                KeyCode::Char('k') | KeyCode::Up | KeyCode::PageUp => {
                    self.scroll_ledger = self.scroll_ledger.saturating_sub(6);
                }
                _ => {}
            }
            return;
        }

        match key.code {
            KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                self.should_quit = true;
            }
            KeyCode::Char('q') => self.should_quit = true,
            KeyCode::Char('?') => self.modal = Some(Modal::Help),
            KeyCode::Tab | KeyCode::BackTab => self.focus = self.focus.next(),
            KeyCode::Char(digit @ '1'..='6') => {
                if let Some(pane) = Pane::from_digit(digit) {
                    self.focus = pane;
                }
            }
            KeyCode::Char('p') => self.payload_tab = self.payload_tab.next(),
            KeyCode::Char('v') => self.log_filter = LogFilter::Voice,
            KeyCode::Char('b') => self.log_filter = LogFilter::Brain,
            KeyCode::Char('e') => self.log_filter = LogFilter::Errors,
            KeyCode::Char('A') => self.log_filter = LogFilter::All,
            KeyCode::Char('z') => {
                self.zoomed = if self.zoomed == Some(self.focus) {
                    None
                } else {
                    Some(self.focus)
                };
            }
            KeyCode::Char('j') | KeyCode::Down => self.move_selection(1),
            KeyCode::Char('k') | KeyCode::Up => self.move_selection(-1),
            KeyCode::Char('g') => self.select_edge(false),
            KeyCode::Char('G') => self.select_edge(true),
            KeyCode::PageDown => self.scroll(8),
            KeyCode::PageUp => self.scroll(-8),
            KeyCode::Char(']') => self.ledger_tab = self.ledger_tab.step(1),
            KeyCode::Char('[') => self.ledger_tab = self.ledger_tab.step(-1),
            KeyCode::Char('f') => {
                self.follow = !self.follow;
                if self.follow {
                    let _ = self.pin.lock().map(|mut p| *p = None);
                    self.sel_prompts = self.turns.len().saturating_sub(1);
                }
            }
            KeyCode::Char('a') => self.resolve_selected_approval(true),
            KeyCode::Char('r') => self.resolve_selected_approval(false),
            KeyCode::Char('m') => self.toggle_mode(),
            KeyCode::Char('x') => self.stop_active_run(),
            KeyCode::Char('y') => self.copy_selected(),
            KeyCode::Char(':') => self.command = Some(String::new()),
            KeyCode::Enter => self.open_selected(),
            KeyCode::Esc => self.zoomed = None,
            _ => {}
        }
    }

    fn handle_command_key(&mut self, key: KeyEvent) {
        let Some(buffer) = self.command.as_mut() else {
            return;
        };
        match key.code {
            KeyCode::Esc => self.command = None,
            KeyCode::Enter => {
                let input = buffer.trim().to_string();
                self.command = None;
                if !input.is_empty() {
                    let body = serde_json::json!({ "input": input }).to_string();
                    self.spawn_post("/v1/ask".to_string(), body);
                }
            }
            KeyCode::Backspace => {
                buffer.pop();
            }
            KeyCode::Char(c) => buffer.push(c),
            _ => {}
        }
    }

    fn move_selection(&mut self, delta: isize) {
        match self.focus {
            Pane::Topology => self.sel_topology = step(self.sel_topology, delta, crate::ui::topology::NODES.len()),
            Pane::Xray => {
                self.sel_trace = step(self.sel_trace, delta, self.traces.len());
                self.follow = false;
            }
            Pane::Tasks => {
                let len = self.flat_task_rows().len();
                self.sel_tasks = step(self.sel_tasks, delta, len);
            }
            Pane::Prompts => {
                self.sel_prompts = step(self.sel_prompts, delta, self.turns.len());
                self.follow = false;
            }
            Pane::Logs => self.scroll_ledger = step(self.scroll_ledger, delta, 10_000),
            Pane::Ledger => match self.ledger_tab {
                LedgerTab::Attempts => {
                    self.sel_attempts = step(self.sel_attempts, delta, self.snapshot.attempts.len());
                }
                LedgerTab::Skills => {
                    self.sel_skills = step(self.sel_skills, delta, self.snapshot.skills.len());
                }
                LedgerTab::Outbox => {
                    self.sel_outbox = step(self.sel_outbox, delta, self.snapshot.outbox.len());
                }
                LedgerTab::Notes | LedgerTab::Database => {
                    self.scroll_ledger = step(self.scroll_ledger, delta, 10_000);
                }
            },
        }
    }

    fn select_edge(&mut self, last: bool) {
        let pick = |len: usize| if last { len.saturating_sub(1) } else { 0 };
        match self.focus {
            Pane::Topology => self.sel_topology = pick(crate::ui::topology::NODES.len()),
            Pane::Xray => {
                self.sel_trace = pick(self.traces.len());
                self.follow = false;
            }
            Pane::Tasks => self.sel_tasks = pick(self.flat_task_rows().len()),
            Pane::Logs => self.scroll_ledger = if last { 10_000 } else { 0 },
            Pane::Prompts => {
                self.sel_prompts = pick(self.turns.len());
                self.follow = false;
            }
            Pane::Ledger => match self.ledger_tab {
                LedgerTab::Attempts => self.sel_attempts = pick(self.snapshot.attempts.len()),
                LedgerTab::Skills => self.sel_skills = pick(self.snapshot.skills.len()),
                LedgerTab::Outbox => self.sel_outbox = pick(self.snapshot.outbox.len()),
                _ => self.scroll_ledger = 0,
            },
        }
    }

    fn scroll(&mut self, delta: isize) {
        match self.focus {
            Pane::Prompts => {
                self.sel_prompts = step(self.sel_prompts, delta, self.turns.len());
                self.follow = false;
            }
            _ => self.scroll_ledger = step(self.scroll_ledger, delta, 10_000),
        }
    }

    fn open_selected(&mut self) {
        match self.focus {
            Pane::Topology => self.modal = Some(Modal::Node(self.sel_topology)),
            Pane::Xray => self.payload_tab = self.payload_tab.next(),
            Pane::Logs => self.log_filter = match self.log_filter {
                LogFilter::All => LogFilter::Voice,
                LogFilter::Voice => LogFilter::Brain,
                LogFilter::Brain => LogFilter::Errors,
                LogFilter::Errors => LogFilter::All,
            },
            Pane::Prompts => {
                self.expanded = if self.expanded == Some(self.sel_prompts) {
                    None
                } else {
                    Some(self.sel_prompts)
                };
            }
            Pane::Tasks => {
                if let Some(task) = self.selected_task() {
                    let id = task.id.clone();
                    self.modal = Some(Modal::Task(id));
                }
            }
            Pane::Ledger => match self.ledger_tab {
                LedgerTab::Attempts => {
                    let attempt = self.selected_attempt().cloned();
                    if let Some(attempt) = attempt {
                        if let Some(path) = attempt.session_jsonl_path.clone() {
                            let _ = self.pin.lock().map(|mut p| *p = Some(PathBuf::from(path)));
                            self.follow = false;
                            self.focus = Pane::Prompts;
                        } else {
                            self.modal = Some(Modal::Attempt(attempt.id));
                        }
                    }
                }
                LedgerTab::Skills => {
                    if let Some(skill) = self.selected_skill() {
                        self.modal = Some(Modal::Skill(skill.slug.clone()));
                    }
                }
                LedgerTab::Outbox => {
                    if let Some(item) = self.selected_outbox() {
                        if item.kind == "approval_request" && item.status == "queued" {
                            self.toast = Some((
                                "Approval selected — press `a` to approve, `r` to reject".to_string(),
                                Instant::now(),
                            ));
                        }
                    }
                }
                _ => {}
            },
        }
    }

    // ---------------------------------------------------------------- actions

    fn spawn_post(&self, path: String, body: String) {
        let cfg = self.cfg.clone();
        let tx = self.tx.clone();
        std::thread::spawn(move || {
            let message = match http::post_json(&cfg, &path, &body) {
                Ok(_) => format!("OK {path}"),
                Err(err) => format!("FAILED {path}: {err}"),
            };
            let _ = tx.send(AppEvent::Toast(message));
        });
    }

    fn resolve_selected_approval(&mut self, approved: bool) {
        let Some(item) = self.selected_outbox().cloned() else {
            self.toast = Some(("No outbox item selected".to_string(), Instant::now()));
            return;
        };
        if item.kind != "approval_request" || item.status != "queued" {
            self.toast = Some((
                format!("{} is {} (not a queued approval)", item.id, item.status),
                Instant::now(),
            ));
            return;
        }
        let body = serde_json::json!({ "approved": approved }).to_string();
        self.spawn_post(format!("/v1/outbox/{}/resolve", item.id), body);
        self.toast = Some((
            format!(
                "{} {}",
                if approved { "Approving" } else { "Rejecting" },
                item.id
            ),
            Instant::now(),
        ));
    }

    fn toggle_mode(&mut self) {
        let input = if self.snapshot.state.mode == "AUTONOMOUS" {
            "switch to assist mode"
        } else {
            "switch to autonomous mode"
        };
        let body = serde_json::json!({ "input": input }).to_string();
        self.spawn_post("/v1/ask".to_string(), body);
        self.toast = Some((format!("Requested: {input}"), Instant::now()));
    }

    fn stop_active_run(&mut self) {
        let Some(run_id) = self.active_run.clone() else {
            self.toast = Some(("No active run to stop".to_string(), Instant::now()));
            return;
        };
        self.spawn_post(format!("/v1/runs/{run_id}/stop"), "{}".to_string());
        self.toast = Some((format!("Stopping {run_id}"), Instant::now()));
    }

    fn copy_selected(&mut self) {
        let text = match self.focus {
            Pane::Xray => self
                .selected_trace()
                .map(|t| {
                    let mut out = format!("#{} {}\n", t.id, t.headline());
                    for stage in &t.stages {
                        out.push_str(&format!("{:<9} {} {}\n", stage.kind, stage.text, stage.detail));
                    }
                    out
                })
                .unwrap_or_default(),
            Pane::Logs => self
                .events
                .iter()
                .filter(|e| self.log_filter.matches(e))
                .map(|e| format!("{:.0} {}\n", e.ts, e.event))
                .collect(),
            Pane::Prompts => self
                .selected_turn()
                .map(|t| format!("{}\n{}", t.title, t.body))
                .unwrap_or_default(),
            Pane::Tasks => self
                .selected_task()
                .map(|t| format!("{} — {}\n{}", t.id, t.title, t.spec))
                .unwrap_or_default(),
            Pane::Topology => crate::ui::topology::node_text(self.sel_topology),
            Pane::Ledger => match self.ledger_tab {
                LedgerTab::Attempts => self
                    .selected_attempt()
                    .map(|a| {
                        format!(
                            "#{} {} {} {}\n{}\n{}",
                            a.id,
                            a.task_id,
                            a.role,
                            a.outcome,
                            a.summary,
                            a.verify_output.clone().unwrap_or_default()
                        )
                    })
                    .unwrap_or_default(),
                LedgerTab::Skills => self
                    .selected_skill()
                    .map(|s| s.content.clone())
                    .unwrap_or_default(),
                LedgerTab::Outbox => self
                    .selected_outbox()
                    .map(|o| format!("{} {} {}", o.id, o.kind, o.summary_spoken))
                    .unwrap_or_default(),
                _ => String::new(),
            },
        };
        if text.trim().is_empty() {
            self.toast = Some(("Nothing to copy".to_string(), Instant::now()));
            return;
        }
        match copy_to_clipboard(&text) {
            Ok(()) => self.toast = Some(("Copied to clipboard".to_string(), Instant::now())),
            Err(err) => self.toast = Some((format!("Copy failed: {err}"), Instant::now())),
        }
    }

    /// Reads (and caches by mtime) a progress note for the given task.
    pub fn note_for(&mut self, task_id: &str) -> String {
        let path = self.cfg.notes_dir.join(format!("{task_id}.md"));
        let mtime = std::fs::metadata(&path)
            .and_then(|m| m.modified())
            .unwrap_or(SystemTime::UNIX_EPOCH);
        if let Some((cached_mtime, text)) = self.notes_cache.get(task_id) {
            if *cached_mtime == mtime {
                return text.clone();
            }
        }
        let text = std::fs::read_to_string(&path).unwrap_or_default();
        self.notes_cache
            .insert(task_id.to_string(), (mtime, text.clone()));
        text
    }

    /// Reads (and caches by mtime) the tail of the daemon error log.
    pub fn daemon_log(&mut self) -> String {
        let path = self.cfg.logs_dir.join("jarvis.err.log");
        let mtime = std::fs::metadata(&path)
            .and_then(|m| m.modified())
            .unwrap_or(SystemTime::UNIX_EPOCH);
        if let Some((cached_mtime, text)) = &self.logs_cache {
            if *cached_mtime == mtime {
                return text.clone();
            }
        }
        let text = crate::db::tail_file(&path, 300);
        self.logs_cache = Some((mtime, text.clone()));
        text
    }
}

fn step(current: usize, delta: isize, len: usize) -> usize {
    if len == 0 {
        return 0;
    }
    let next = current as isize + delta;
    next.clamp(0, len as isize - 1) as usize
}

/// Unix epoch seconds; the daemon stamps `expires_at` with the same clock on the same Mac.
pub fn now_unix() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// Flattens the task DAG into `(snapshot_index, depth)` rows for tree rendering.
pub fn flat_tasks(snapshot: &Snapshot) -> Vec<(usize, usize)> {
    let mut children: HashMap<&str, Vec<usize>> = HashMap::new();
    let mut roots: Vec<usize> = Vec::new();
    for (idx, task) in snapshot.tasks.iter().enumerate() {
        match task.parent_id.as_deref() {
            Some(parent) if snapshot.tasks.iter().any(|t| t.id == parent) => {
                children.entry(parent).or_default().push(idx);
            }
            _ => roots.push(idx),
        }
    }
    let mut out = Vec::new();
    for root in roots {
        push_tree(root, 0, snapshot, &children, &mut out);
    }
    out
}

fn push_tree(
    idx: usize,
    depth: usize,
    snapshot: &Snapshot,
    children: &HashMap<&str, Vec<usize>>,
    out: &mut Vec<(usize, usize)>,
) {
    if depth > 12 || out.len() > 2000 {
        return;
    }
    out.push((idx, depth));
    if let Some(kids) = children.get(snapshot.tasks[idx].id.as_str()) {
        for &kid in kids {
            push_tree(kid, depth + 1, snapshot, children, out);
        }
    }
}

fn copy_to_clipboard(text: &str) -> Result<(), String> {
    let mut child = Command::new("pbcopy")
        .stdin(Stdio::piped())
        .spawn()
        .map_err(|e| e.to_string())?;
    if let Some(stdin) = child.stdin.as_mut() {
        stdin.write_all(text.as_bytes()).map_err(|e| e.to_string())?;
    }
    child.wait().map_err(|e| e.to_string())?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Task;

    fn test_app() -> App {
        let cfg = Config {
            home: PathBuf::from("/tmp"),
            db_path: PathBuf::from("/tmp/state.db"),
            sessions_dir: PathBuf::from("/tmp/sessions"),
            traces_dir: PathBuf::from("/tmp/traces"),
            skills_dir: PathBuf::from("/tmp/skills"),
            notes_dir: PathBuf::from("/tmp/notes"),
            logs_dir: PathBuf::from("/tmp/logs"),
            host: "127.0.0.1".to_string(),
            port: 1,
            api_key: "test".to_string(),
        };
        let (tx, _rx) = std::sync::mpsc::channel();
        App::new(cfg, tx, Arc::new(Mutex::new(None)))
    }

    fn pair_event(value: serde_json::Value) -> AppEvent {
        AppEvent::Telemetry(TelemetryEvent::from_value(value))
    }

    fn voice_event(value: serde_json::Value) -> AppEvent {
        AppEvent::Telemetry(TelemetryEvent::from_value(value))
    }

    #[test]
    fn a_pairing_code_stays_until_it_is_used() {
        let mut app = test_app();
        app.apply(pair_event(serde_json::json!({
            "event": "pair.requested",
            "code": "123456",
            "name": "MacBook",
            "expires_at": now_unix() + 120.0,
            "ttl_s": 120
        })));
        let pair = app.pending_pair.as_ref().expect("a live code is shown");
        assert_eq!(pair.spaced(), "123 456");
        assert!(app.pair_seconds_left().unwrap() > 100);

        app.apply(pair_event(serde_json::json!({
            "event": "pair.resolved", "ok": true, "device": "MacBook"
        })));
        assert!(app.pending_pair.is_none());
        assert_eq!(app.pair_seconds_left(), None);
    }

    #[test]
    fn a_replayed_expired_code_is_never_shown() {
        let mut app = test_app();
        app.apply(pair_event(serde_json::json!({
            "event": "pair.requested",
            "code": "654321",
            "name": "old",
            "expires_at": now_unix() - 5.0
        })));
        assert!(app.pending_pair.is_none());
    }

    #[test]
    fn asking_again_keeps_the_same_digits() {
        let mut app = test_app();
        for _ in 0..3 {
            app.apply(pair_event(serde_json::json!({
                "event": "pair.requested",
                "code": "111222",
                "name": "MacBook",
                "expires_at": now_unix() + 300.0
            })));
        }
        assert_eq!(app.pending_pair.as_ref().unwrap().code, "111222");
    }

    #[test]
    fn a_voice_turn_is_grouped_by_turn_id() {
        let mut app = test_app();
        app.apply(voice_event(serde_json::json!({
            "event": "voice.stt", "turn_id": "t7", "ts": 1.0, "text": "fix the bug",
            "passed": true, "score": 0.8, "ms": 120
        })));
        app.apply(voice_event(serde_json::json!({
            "event": "voice.tts", "turn_id": "t7", "ts": 2.0, "text": "Done.", "mode": "VOICE"
        })));
        assert_eq!(app.traces.len(), 1, "one sentence, one turn");
        let trace = &app.traces[0];
        assert!(trace.done);
        assert_eq!(trace.headline(), "fix the bug");
        assert_eq!(trace.spoken().map(|s| s.text.as_str()), Some("Done."));
    }

    #[test]
    fn a_dropped_sentence_shows_why_and_ends_the_turn() {
        let mut app = test_app();
        app.apply(voice_event(serde_json::json!({
            "event": "voice.stt", "turn_id": "t8", "ts": 1.0, "text": "hello tv",
            "passed": false, "score": 0.21, "ms": 90
        })));
        let trace = &app.traces[0];
        assert!(trace.done, "nothing follows a sentence the voice check dropped");
        assert_eq!(trace.stages[0].tone, StageTone::Bad);
    }

    #[test]
    fn reasoning_chunks_coalesce_into_one_stage() {
        let mut app = test_app();
        for chunk in ["Looking at the ", "project list, ", "then delegating."] {
            app.apply(voice_event(serde_json::json!({
                "event": "voice.thinking", "turn_id": "t9", "ts": 1.0, "text": chunk
            })));
        }
        let trace = &app.traces[0];
        let thinking: Vec<_> = trace.stages.iter().filter(|s| s.kind == "THINK").collect();
        assert_eq!(thinking.len(), 1, "one reasoning stage per turn, not one per token");
        assert_eq!(thinking[0].text, "Looking at the project list, then delegating.");
    }

    #[test]
    fn heartbeats_update_liveness_without_flooding_the_log() {
        let mut app = test_app();
        app.apply(voice_event(serde_json::json!({
            "event": "heartbeat", "pid": 99, "uptime_s": 42.5
        })));
        assert!(app.daemon_connected);
        assert_eq!(app.daemon_pid, Some(99));
        assert!(app.events.is_empty());
    }

    fn task(id: &str, parent: Option<&str>) -> Task {
        Task {
            id: id.to_string(),
            parent_id: parent.map(str::to_string),
            ..Default::default()
        }
    }

    #[test]
    fn flattens_nested_task_tree_depth_first() {
        let snapshot = Snapshot {
            tasks: vec![
                task("SG-1", None),
                task("SG-1.1", Some("SG-1")),
                task("SG-1.2", Some("SG-1")),
                task("SG-2", None),
            ],
            ..Default::default()
        };
        let rows = flat_tasks(&snapshot);
        let ids: Vec<&str> = rows
            .iter()
            .map(|(idx, _)| snapshot.tasks[*idx].id.as_str())
            .collect();
        assert_eq!(ids, vec!["SG-1", "SG-1.1", "SG-1.2", "SG-2"]);
        assert_eq!(rows[1].1, 1);
        assert_eq!(rows[3].1, 0);
    }

    #[test]
    fn step_clamps_within_bounds() {
        assert_eq!(step(0, -1, 5), 0);
        assert_eq!(step(4, 1, 5), 4);
        assert_eq!(step(0, 1, 5), 1);
        assert_eq!(step(0, 1, 0), 0);
    }
}
