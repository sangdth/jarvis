//! `jarvis-top` — a fullscreen Ratatui cockpit for Jarvis's brain (Brain) daemon.
//!
//! Data planes:
//!   * durable — `state.db` opened read-only, polled on a worker thread
//!   * durable — `sessions/**/*.jsonl` tailed incrementally by byte offset
//!   * live    — `GET /v1/telemetry/events` (SSE) on a reconnecting worker thread

mod app;
mod config;
mod db;
mod http;
mod model;
mod session;
mod ui;

use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use ratatui::backend::CrosstermBackend;
use ratatui::crossterm::cursor::{Hide, SetCursorStyle, Show};
use ratatui::crossterm::event::{self, Event, KeyEventKind};
use ratatui::crossterm::execute;
use ratatui::crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use ratatui::Terminal;

use app::{App, AppEvent, LedgerTab, Modal, Pane};
use config::Config;

const HELP: &str = "\
jarvis-top — fullscreen cockpit for Jarvis's brain (Brain)

USAGE
  jarvis-top

READS
  $JARVIS_HOME (default ~/.config/jarvis)
    .env         BRAIN_API_KEY, HOST, PORT
    logs/        jarvis.err.log, the daemon log tail
  $BRAIN_HOME / $JARVIS_BRAIN_HOME (default ~/.config/jarvis/brain)
    state.db     opened read-only (SQLite WAL)
    sessions/    live `pi --mode json` transcripts
    memory/progress/  handoff notes

LIVE
  GET /v1/telemetry/events   SSE (auto-reconnecting, Bearer auth)
  POST /v1/outbox/{id}/resolve, /v1/ask, /v1/runs/{id}/stop

KEYS
  q quit · ? help · Tab/1-4 focus · z zoom · j/k move · Enter inspect
  a/r approve-reject · m mode · x stop run · : ask · y copy
";

fn main() {
    if std::env::args().any(|a| a == "--help" || a == "-h") {
        print!("{HELP}");
        return;
    }
    if std::env::args().any(|a| a == "--version" || a == "-V") {
        println!("jarvis-top {}", env!("CARGO_PKG_VERSION"));
        return;
    }

    let previous_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        let _ = restore_terminal();
        previous_hook(info);
    }));

    if let Err(err) = run() {
        let _ = restore_terminal();
        eprintln!("jarvis-top: {err}");
        std::process::exit(1);
    }
}

fn run() -> std::io::Result<()> {
    let cfg = Config::load();
    if std::env::args().any(|a| a == "--render-check") {
        return render_check(cfg);
    }
    if let Some(position) = std::env::args().position(|a| a == "--selftest") {
        let seconds = std::env::args()
            .nth(position + 1)
            .and_then(|v| v.parse::<u64>().ok())
            .unwrap_or(3);
        return selftest(cfg, seconds);
    }
    let (tx, rx) = mpsc::channel::<AppEvent>();
    let stop = Arc::new(AtomicBool::new(false));
    let pin: Arc<Mutex<Option<PathBuf>>> = Arc::new(Mutex::new(None));

    spawn_workers(&cfg, &tx, &stop, &pin);

    let mut app = App::new(cfg, tx, pin);

    enable_raw_mode()?;
    let mut stdout = std::io::stdout();
    execute!(
        stdout,
        EnterAlternateScreen,
        Hide,
        SetCursorStyle::BlinkingBar
    )?;
    stdout.flush()?;

    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;
    terminal.clear()?;

    let result = event_loop(&mut terminal, &mut app, rx);

    stop.store(true, Ordering::Relaxed);
    restore_terminal()?;
    result
}

fn spawn_workers(
    cfg: &Config,
    tx: &Sender<AppEvent>,
    stop: &Arc<AtomicBool>,
    pin: &Arc<Mutex<Option<PathBuf>>>,
) {
    {
        let cfg = cfg.clone();
        let tx = tx.clone();
        let stop = stop.clone();
        std::thread::spawn(move || http::sse_loop(cfg, tx, stop));
    }
    {
        let cfg = cfg.clone();
        let tx = tx.clone();
        let stop = stop.clone();
        std::thread::spawn(move || snapshot_loop(cfg, tx, stop));
    }
    {
        let cfg = cfg.clone();
        let tx = tx.clone();
        let stop = stop.clone();
        let pin = pin.clone();
        std::thread::spawn(move || session::tailer_loop(cfg, pin, tx, stop));
    }
}

fn restore_terminal() -> std::io::Result<()> {
    let mut stdout = std::io::stdout();
    disable_raw_mode()?;
    execute!(
        stdout,
        Show,
        SetCursorStyle::DefaultUserShape,
        LeaveAlternateScreen
    )?;
    stdout.flush()
}

fn event_loop(
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    app: &mut App,
    rx: Receiver<AppEvent>,
) -> std::io::Result<()> {
    loop {
        while let Ok(event) = rx.try_recv() {
            app.apply(event);
        }
        if app.animating() || app.pending_pair.is_some() {
            app.dirty = true;
        }
        if app.dirty {
            terminal.draw(|frame| ui::draw(frame, app))?;
            app.dirty = false;
        }
        if app.should_quit {
            break;
        }

        let timeout = if app.animating() {
            Duration::from_millis(16)
        } else {
            Duration::from_millis(80)
        };
        if event::poll(timeout)? {
            match event::read()? {
                Event::Key(key) if key.kind == KeyEventKind::Press => app.handle_key(key),
                Event::Resize(_, _) => app.dirty = true,
                _ => {}
            }
        }
        app.tick = app.tick.wrapping_add(1);
    }
    Ok(())
}

/// Polls `state.db` on a dedicated thread with its own read-only connection.
fn snapshot_loop(cfg: Config, tx: Sender<AppEvent>, stop: Arc<AtomicBool>) {
    let mut conn = db::open_read_only(&cfg.db_path).ok();
    let mut warned = false;
    while !stop.load(Ordering::Relaxed) {
        if conn.is_none() {
            conn = db::open_read_only(&cfg.db_path).ok();
        }
        match conn.as_ref() {
            Some(connection) => match db::load_snapshot(connection, &cfg) {
                Ok(snapshot) => {
                    warned = false;
                    let _ = tx.send(AppEvent::Snapshot(Box::new(snapshot)));
                }
                Err(err) => {
                    if !warned {
                        let _ = tx.send(AppEvent::SnapshotError(format!("snapshot: {err}")));
                        warned = true;
                    }
                    conn = None;
                }
            },
            None => {
                if !warned {
                    let _ = tx.send(AppEvent::SnapshotError(format!(
                        "cannot open {} read-only",
                        cfg.db_path.display()
                    )));
                    warned = true;
                }
            }
        }
        std::thread::sleep(Duration::from_millis(700));
    }
}

/// Live integration check: runs the real worker threads for N seconds and prints what arrived.
fn selftest(cfg: Config, seconds: u64) -> std::io::Result<()> {
    let (tx, rx) = mpsc::channel::<AppEvent>();
    let stop = Arc::new(AtomicBool::new(false));
    let pin: Arc<Mutex<Option<PathBuf>>> = Arc::new(Mutex::new(None));
    spawn_workers(&cfg, &tx, &stop, &pin);

    let mut app = App::new(cfg.clone(), tx, pin);
    let deadline = std::time::Instant::now() + Duration::from_secs(seconds);
    while std::time::Instant::now() < deadline {
        if let Ok(event) = rx.recv_timeout(Duration::from_millis(150)) {
            app.apply(event);
        }
    }
    stop.store(true, Ordering::Relaxed);

    println!("home              {}", cfg.home.display());
    println!("daemon_connected  {}", app.daemon_connected);
    if let Some(err) = &app.daemon_error {
        println!("daemon_error      {err}");
    }
    if let Some(pid) = app.daemon_pid {
        println!("daemon_pid        {pid} (uptime {:.0}s)", app.daemon_uptime);
    }
    println!(
        "snapshot          mode={} tasks={} attempts={} skills={} outbox={} wal={}B",
        app.snapshot.state.mode,
        app.snapshot.counts.tasks,
        app.snapshot.counts.attempts,
        app.snapshot.counts.skills_total,
        app.snapshot.outbox.len(),
        app.snapshot.wal_bytes
    );
    println!("telemetry_events  {}", app.events.len());
    for event in app.events.iter().rev().take(8).rev() {
        println!("  #{} {}", event.seq, event.event);
    }
    println!(
        "session           {} turns from {}",
        app.turns.len(),
        app.session_path
            .as_ref()
            .map(|p| p.display().to_string())
            .unwrap_or_else(|| "—".to_string())
    );
    Ok(())
}

/// The turn `--render-check` paints: a real shape from the telemetry bus, so a layout change shows what it breaks.
fn demo_voice_turn() -> Vec<model::TelemetryEvent> {
    let values = vec![
        serde_json::json!({"seq": 1, "ts": 1.0, "event": "voice.ear", "local_audio": true, "relay": true,
                           "language": "en", "whisper": "turbo", "master_check": true, "meeting": false}),
        serde_json::json!({"seq": 2, "ts": 1.1, "event": "voice.stt", "turn_id": "t42", "text":
                           "uh check why the auth test is failing in platform v2", "score": 0.78,
                           "threshold": 0.35, "passed": true, "role": "owner", "ms": 188}),
        serde_json::json!({"seq": 3, "ts": 1.2, "event": "voice.intent", "turn_id": "t42",
                           "intent": "ADDRESSED_TO_JARVIS", "ms": 94, "speaker": "User", "owner": true}),
        serde_json::json!({"seq": 4, "ts": 1.3, "event": "voice.cleanup", "turn_id": "t42",
                           "text": "Check why the auth test is failing in platform-v2.",
                           "raw": "uh check why the auth test is failing in platform v2", "ms": 236,
                           "changed": true}),
        serde_json::json!({"seq": 5, "ts": 1.4, "event": "voice.memory", "turn_id": "t42", "hits": 2,
                           "ms": 22, "text": "platform-v2 uses pnpm test; auth tests hit local postgres"}),
        serde_json::json!({"seq": 6, "ts": 1.5, "event": "voice.request", "turn_id": "t42", "messages": 14,
                           "user_tail": "Check why the auth test is failing in platform-v2.",
                           "tools": "send_agent_task, answer_agent, stop_agent, list_sessions"}),
        serde_json::json!({"seq": 7, "ts": 1.6, "event": "voice.thinking", "turn_id": "t42",
                           "text": "Matches HuggerHustle/platform-v2 in the project list. Delegate to the brain with a clear English task and keep the spoken acknowledgement short."}),
        serde_json::json!({"seq": 8, "ts": 1.7, "event": "voice.tool", "turn_id": "t42",
                           "tool": "send_agent_task", "phase": "call"}),
        serde_json::json!({"seq": 9, "ts": 1.8, "event": "voice.dispatch", "turn_id": "t42",
                           "run_id": "r91f", "session": "HuggerHustle/platform-v2",
                           "text": "Check why the auth test is failing", "pinned": false}),
        serde_json::json!({"seq": 10, "ts": 1.9, "event": "brain.route", "turn_id": "t42", "phase": "route",
                           "run_id": "r91f", "choice": "delegate_project", "runner": "t3",
                           "project": "HuggerHustle/platform-v2"}),
        serde_json::json!({"seq": 11, "ts": 2.0, "event": "voice.return", "turn_id": "t42", "run_id": "r91f",
                           "chars": 840, "outcome": "completed",
                           "text": "Investigated apps/api/test/auth.spec.ts: the hardcoded JWT expired on 2026-10-01, causing 401s in three tests. Replaced it with a signed fixture; 14/14 tests pass."}),
        serde_json::json!({"seq": 12, "ts": 2.1, "event": "voice.retell", "turn_id": "t42", "chars": 74,
                           "text": "Found it — the JWT fixture expired; I updated it and the auth tests pass."}),
        serde_json::json!({"seq": 13, "ts": 2.2, "event": "voice.tts", "turn_id": "t42", "mode": "VOICE",
                           "text": "Found it — the JWT fixture expired; I updated it and the auth tests pass."}),
        serde_json::json!({"seq": 14, "ts": 2.3, "event": "voice.latency", "turn_id": "t42",
                           "text": "STT 188ms · LLM 410ms · TTS 105ms · total 1.06s"}),
        serde_json::json!({"seq": 15, "ts": 2.4, "event": "brain.route", "turn_id": "t42", "phase": "done",
                           "run_id": "r91f", "choice": "delegate_project", "outcome": "pushed",
                           "project": "HuggerHustle/platform-v2", "summary": "14/14 auth tests pass."}),
    ];
    values
        .into_iter()
        .map(model::TelemetryEvent::from_value)
        .collect()
}

/// Headless render: paints one full frame into a `TestBackend` and prints it.
/// Used by CI/smoke checks and to verify layout without a real terminal.
fn render_check(cfg: Config) -> std::io::Result<()> {
    use ratatui::backend::TestBackend;

    let snapshot = match db::open_read_only(&cfg.db_path) {
        Ok(conn) => db::load_snapshot(&conn, &cfg).ok(),
        Err(_) => None,
    };
    let (tx, _rx) = mpsc::channel::<AppEvent>();
    let mut app = App::new(cfg.clone(), tx, Arc::new(Mutex::new(None)));
    if let Some(snapshot) = snapshot {
        app.apply(AppEvent::Snapshot(Box::new(snapshot)));
    }
    app.apply(AppEvent::Daemon(true, None));
    app.phase = "pi_running".to_string();
    app.phase_role = Some("builder".to_string());
    app.phase_task = Some("SG-4.2".to_string());
    app.live_cost = 0.1832;
    app.live_elapsed = 43.0;
    app.daemon_pid = Some(4242);
    app.daemon_uptime = 9123.0;
    for event in demo_voice_turn() {
        app.apply(AppEvent::Telemetry(event));
    }
    if let Some(path) = session::newest_session(&cfg.sessions_dir) {
        if let Ok(text) = std::fs::read_to_string(&path) {
            let turns: Vec<model::Turn> = text
                .lines()
                .flat_map(session::parse_line)
                .collect();
            app.apply(AppEvent::SessionReset { path: path.clone() });
            app.apply(AppEvent::SessionAppend { path, turns });
        }
    }
    app.pulse("rec");
    app.pulse("gw");

    // Debug selectors so each pane can be rendered headlessly in CI.
    if let Ok(pane) = std::env::var("SB_DEMO_PANE") {
        app.focus = match pane.as_str() {
            "2" => Pane::Xray,
            "3" => Pane::Prompts,
            "4" => Pane::Logs,
            "5" => Pane::Tasks,
            "6" => Pane::Ledger,
            _ => Pane::Topology,
        };
    }
    if let Ok(tab) = std::env::var("SB_DEMO_TAB") {
        app.ledger_tab = match tab.as_str() {
            "2" => LedgerTab::Skills,
            "3" => LedgerTab::Outbox,
            "4" => LedgerTab::Notes,
            "5" => LedgerTab::Database,
            _ => LedgerTab::Attempts,
        };
    }
    if let Ok(spec) = std::env::var("SB_DEMO_MODAL") {
        app.modal = match spec.as_str() {
            "help" => Some(Modal::Help),
            other => {
                let (kind, value) = other.split_once(':').unwrap_or(("node", "2"));
                match kind {
                    "node" => Some(Modal::Node(value.parse::<usize>().unwrap_or(2))),
                    "attempt" => Some(Modal::Attempt(value.parse::<i64>().unwrap_or(1))),
                    "skill" => Some(Modal::Skill(value.to_string())),
                    "task" => Some(Modal::Task(value.to_string())),
                    _ => None,
                }
            }
        };
    }
    if std::env::var("SB_DEMO_ZOOM").is_ok() {
        app.zoomed = Some(app.focus);
    }
    if let Ok(code) = std::env::var("SB_DEMO_PAIR") {
        app.pending_pair = Some(model::PendingPair {
            code,
            name: "Sang's MacBook".to_string(),
            expires_at: app::now_unix() + 288.0,
        });
    }

    let backend = TestBackend::new(190, 52);
    let mut terminal = Terminal::new(backend).expect("in-memory terminal");
    terminal
        .draw(|frame| ui::draw(frame, &mut app))
        .expect("render frame");
    println!("{}", terminal.backend());
    Ok(())
}
